"""Tests for scripts/connections_manifest.py.

The manifest is meant to be **safe to email**: it names connection targets so a platform team can
prepare, and never a credential. Most of these tests exist to keep that true, because the failure is
silent - a leaked value in a document nobody re-reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import connections_manifest as cm  # noqa: E402  # pylint: disable=wrong-import-position


def _spec(sources: list[dict], tmp_path: Path) -> Path:
    """Write a minimal parser-contract spec the bundle loader accepts."""
    spec = tmp_path / "migration-spec.json"
    spec.write_text(json.dumps({"data_sources": sources}), encoding="utf-8")
    return spec


# --------------------------------------------------------------------------- no secrets, ever


@pytest.mark.parametrize(
    "field",
    ["password", "Password", "pat_secret", "api_key", "apiKey", "token", "sas_token", "credential", "pwd", "auth_key"],
)
def test_no_credential_shaped_field_reaches_the_manifest(field):
    """The allow-list is the control; this proves the alarm agrees with it."""
    connection = {"class": "snowflake", "server": "acct.snowflakecomputing.com", field: "SUPER_SECRET_VALUE"}
    assert "SUPER_SECRET_VALUE" not in json.dumps(cm.safe_connection(connection))


def test_the_rendered_document_cannot_contain_a_secret(tmp_path):
    """End to end: a secret in the source connection must not survive into the markdown."""
    sources = [
        {
            "name": "Sales",
            "connection": {
                "class": "snowflake",
                "server": "acct.snowflakecomputing.com",
                "dbname": "PROD",
                "password": "hunter2-should-never-appear",
                "token": "tok-should-never-appear",
            },
        }
    ]
    manifest = cm.build(_spec(sources, tmp_path))
    rendered = cm.render(manifest)
    assert "hunter2-should-never-appear" not in rendered
    assert "tok-should-never-appear" not in rendered
    assert "hunter2-should-never-appear" not in json.dumps(manifest)
    # ...while still telling the platform team where to point.
    assert "acct.snowflakecomputing.com" in rendered
    assert "PROD" in rendered


def test_an_unknown_connection_field_is_dropped_rather_than_passed_through(tmp_path):
    """Fail-closed: a future field carrying a token must not reach the manifest by default."""
    sources = [{"name": "S", "connection": {"class": "snowflake", "some_new_field": "LEAKED"}}]
    assert "LEAKED" not in json.dumps(cm.build(_spec(sources, tmp_path)))


# --------------------------------------------------------------------------- classification


def test_a_live_source_is_reported_as_needing_a_credential(tmp_path):
    sources = [{"name": "S", "connection": {"class": "snowflake", "server": "acct", "mode": "live"}}]
    manifest = cm.build(_spec(sources, tmp_path))
    assert manifest["needs_credential"] == 1
    assert manifest["connections"][0]["status"] == cm.NEEDS_CREDENTIAL


def test_a_snowflake_extract_still_needs_a_credential(tmp_path):
    """The trap this whole classification exists for: an extract of a LIVE system is not a file.

    A packaged .hyper is Tableau's cache of an upstream system, so the upstream is still what must be
    connected. Reporting it as needing nothing is the fail-open direction.
    """
    sources = [{"name": "S", "connection": {"class": "snowflake", "server": "acct", "mode": "extract"}}]
    assert cm.build(_spec(sources, tmp_path))["needs_credential"] == 1


def test_a_flat_file_is_reported_as_a_snapshot_not_as_a_missing_credential(tmp_path):
    """Customers read 'no connection' as broken. Saying 'snapshot' stops a hunt for a credential."""
    sources = [{"name": "S", "connection": {"class": "excel-direct", "mode": "extract"}}]
    manifest = cm.build(_spec(sources, tmp_path))
    assert manifest["snapshots"] == 1
    assert "snapshot" in manifest["connections"][0]["status"]
    assert "will not refresh" in cm.render(manifest)


def test_a_published_datasource_says_where_its_upstream_actually_lives(tmp_path):
    """`sqlproxy` is Tableau's own front end; telling someone to connect to it is meaningless."""
    sources = [{"name": "Published", "connection": {"class": "sqlproxy"}}]
    manifest = cm.build(_spec(sources, tmp_path))
    assert manifest["connections"][0]["published_datasource"] is True
    assert "upstream defined in Tableau" in cm.render(manifest)
    assert "`sqlproxy`" not in cm.render(manifest)


# --------------------------------------------------------------------------- shape and ordering


def test_identical_sources_are_collapsed_into_one_job(tmp_path):
    """A datasource repeats once per consuming workbook; two rows read as two pieces of work."""
    connection = {"class": "postgres", "server": "db.example", "dbname": "app"}
    sources = [{"name": "Same", "connection": connection}, {"name": "Same", "connection": dict(connection)}]
    assert cm.build(_spec(sources, tmp_path))["total"] == 1


def test_same_name_different_server_is_not_collapsed(tmp_path):
    """Two systems that merely share a name are two jobs, and merging them would hide one."""
    sources = [
        {"name": "Sales", "connection": {"class": "postgres", "server": "prod.example"}},
        {"name": "Sales", "connection": {"class": "postgres", "server": "dev.example"}},
    ]
    assert cm.build(_spec(sources, tmp_path))["total"] == 2


def test_sources_are_ordered_by_blast_radius(tmp_path):
    """A source feeding twelve reports is a different task from one feeding an archived report."""
    bundle = tmp_path / "b"
    (bundle / "handover").mkdir(parents=True)
    (bundle / "migration-spec.json").write_text(
        json.dumps(
            {
                "data_sources": [
                    {"name": "Small", "connection": {"class": "postgres", "server": "s"}},
                    {"name": "Big", "connection": {"class": "postgres", "server": "b"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    for i, name in enumerate(["wb1", "wb2", "wb3"]):
        source = "Big" if i < 2 else "Small"
        (bundle / "handover" / f"{name}.json").write_text(
            json.dumps({"workbook": {"name": name, "bound_datasource": source}}), encoding="utf-8"
        )
    manifest = cm.build(bundle)
    assert [e["name"] for e in manifest["connections"]] == ["Big", "Small"]
    assert manifest["connections"][0]["used_by_count"] == 2


def test_an_absent_blast_radius_is_reported_as_unknown_not_as_zero(tmp_path):
    """A source with no KNOWN consumers is not a source with none - conflating them hides work."""
    manifest = cm.build(_spec([{"name": "S", "connection": {"class": "postgres"}}], tmp_path))
    assert manifest["blast_radius_known"] is False
    assert "Impact column unavailable" in cm.render(manifest)


def test_a_malformed_handover_slice_does_not_abort_the_manifest(tmp_path):
    """One unreadable slice must cost its own workbook, not the whole document."""
    bundle = tmp_path / "b"
    (bundle / "handover").mkdir(parents=True)
    (bundle / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"name": "S", "connection": {"class": "postgres"}}]}), encoding="utf-8"
    )
    (bundle / "handover" / "broken.json").write_text("{ not json", encoding="utf-8")
    (bundle / "handover" / "ok.json").write_text(
        json.dumps({"workbook": {"name": "ok", "bound_datasource": "S"}}), encoding="utf-8"
    )
    manifest = cm.build(bundle)
    assert manifest["connections"][0]["used_by"] == ["ok"]


def test_the_document_states_that_it_holds_no_credentials(tmp_path):
    """It is meant to be forwarded; the reader should not have to infer that it is safe to."""
    rendered = cm.render(cm.build(_spec([{"name": "S", "connection": {"class": "postgres"}}], tmp_path)))
    assert "never credentials" in rendered
