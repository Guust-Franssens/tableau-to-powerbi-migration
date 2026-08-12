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


# --------------------------------------------------------------------------- review round: value leaks


@pytest.mark.parametrize(
    ("field", "value", "marker"),
    [
        ("server", "https://user:URLPW_MARKER@acct.example", "URLPW_MARKER"),
        ("server", "https://acct.example/?token=QUERY_MARKER", "QUERY_MARKER"),
        ("database", "Driver=X;PWD=ODBC_MARKER;", "ODBC_MARKER"),
        ("database", "db;api_key=APIKEY_MARKER", "APIKEY_MARKER"),
        ("schema", "s;secret=SCHEMA_MARKER", "SCHEMA_MARKER"),
    ],
)
def test_a_secret_hidden_inside_an_allowed_field_value_is_redacted(field, value, marker, tmp_path):
    """An allow-listed KEY is not a promise that its CONTENT is safe.

    Filtering keys alone let URL userinfo, credential query parameters and ODBC/JDBC property
    strings through into a document meant to be emailed. Found in review of #100.
    """
    sources = [{"name": "S", "connection": {"class": "snowflake", field: value}}]
    manifest = cm.build(_spec(sources, tmp_path))
    assert marker not in json.dumps(manifest)
    assert marker not in cm.render(manifest)


def test_a_secret_in_the_source_name_or_reason_or_workbook_is_redacted(tmp_path):
    """A secret reaches the same document by routes that are not connection fields at all."""
    bundle = tmp_path / "b"
    (bundle / "handover").mkdir(parents=True)
    (bundle / "migration-spec.json").write_text(
        json.dumps(
            {
                "data_sources": [
                    {
                        "name": "Sales pwd=NAME_MARKER",
                        "connection": {
                            "class": "snowflake",
                            "powerbi_target": "live_source",
                            "powerbi_target_reason": "reason token=REASON_MARKER",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (bundle / "handover" / "wb.json").write_text(
        json.dumps({"workbook": {"name": "WB secret=WB_MARKER", "bound_datasource": "Sales pwd=NAME_MARKER"}}),
        encoding="utf-8",
    )
    manifest = cm.build(bundle)
    blob = json.dumps(manifest) + cm.render(manifest)
    for marker in ("NAME_MARKER", "REASON_MARKER", "WB_MARKER"):
        assert marker not in blob


def test_the_json_is_checked_as_well_as_the_markdown(tmp_path):
    """The JSON carries fields the markdown never renders, so it is a separate leak surface."""
    sources = [{"name": "S", "connection": {"class": "snowflake", "schema": "s;pwd=JSONONLY_MARKER"}}]
    assert "JSONONLY_MARKER" not in json.dumps(cm.build(_spec(sources, tmp_path)))


# --------------------------------------------------------------------------- review round: real bundle shape


def test_the_canonical_database_field_is_emitted(tmp_path):
    """Both contracts normalise Tableau's `dbname` attribute onto `database`.

    Allow-listing `dbname` alone emitted ONLY the class for all 27 sources of a real bundle - a
    manifest that rendered fine and carried nothing a platform team could act on.
    """
    sources = [{"name": "S", "connection": {"class": "snowflake", "server": "acct", "database": "PROD"}}]
    rendered = cm.render(cm.build(_spec(sources, tmp_path)))
    assert "PROD" in rendered


def test_every_leg_of_a_federated_source_is_named(tmp_path):
    """The real targets of a multi-system source live in connection['connections']."""
    sources = [
        {
            "name": "Multi",
            "connection": {
                "class": "federated",
                "connections": [
                    {"class": "snowflake", "server": "sf.example", "database": "WH"},
                    {"class": "databricks", "server": "dbx.example", "database": "samples"},
                ],
            },
        }
    ]
    rendered = cm.render(cm.build(_spec(sources, tmp_path)))
    assert "sf.example" in rendered and "dbx.example" in rendered


def test_a_federated_source_with_one_live_leg_still_needs_a_credential(tmp_path):
    """Fail-safe: classifying from the top-level class alone can hide a live leg underneath."""
    sources = [
        {
            "name": "Mixed",
            "connection": {
                "class": "federated",
                "powerbi_target": "flat_file",
                "connections": [
                    {"class": "excel-direct", "mode": "extract"},
                    {"class": "snowflake", "server": "sf.example", "mode": "live"},
                ],
            },
        }
    ]
    assert cm.build(_spec(sources, tmp_path))["needs_credential"] == 1


def test_a_tableau_extract_engine_is_neither_connectable_nor_silently_fine(tmp_path):
    """`hyper`/`dataengine` IS the extract; the workbook never records what it came from.

    Naming it as a target tells a platform engineer to connect to a file path, and calling it a
    snapshot would promise no credential is needed. Neither is true, so it goes to review.
    """
    sources = [
        {
            "name": "Extracted",
            "connection": {"class": "federated", "connections": [{"class": "hyper", "database": "x.hyper"}]},
        }
    ]
    manifest = cm.build(_spec(sources, tmp_path))
    assert manifest["needs_review"] == 1
    assert "does not record what it was extracted FROM" in cm.render(manifest)


def test_dedupe_uses_the_full_connection_not_the_projected_one(tmp_path):
    """A field dropped for safety is invisible to a key built from the projection.

    Two genuinely different systems would then merge into one job and the second would vanish.
    """
    sources = [
        {"name": "Same", "connection": {"class": "postgres", "server": "s", "password": "a"}},
        {"name": "Same", "connection": {"class": "postgres", "server": "s", "password": "b"}},
    ]
    assert cm.build(_spec(sources, tmp_path))["total"] == 2
