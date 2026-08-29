"""Tests for scripts/connections_manifest.py.

The manifest is meant to be **safe to email**: it names connection targets so a platform team can
prepare, and never a credential. Most of these tests exist to keep that true, because the failure is
silent - a leaked value in a document nobody re-reads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import connections_manifest as cm  # noqa: E402  # pylint: disable=wrong-import-position

REPO_ROOT = Path(__file__).resolve().parents[1]


def _spec(sources: list[dict], tmp_path: Path) -> Path:
    """Write a minimal parser-contract spec the bundle loader accepts."""
    spec = tmp_path / "migration-spec.json"
    spec.write_text(json.dumps({"data_sources": sources}), encoding="utf-8")
    return spec


def _git_repo(tmp_path: Path, ignore_text: bytes | None = None) -> Path:
    """Create a git work tree carrying this repo's real ignore rules by default."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, text=True, check=True)
    if ignore_text is None:
        ignore_text = (REPO_ROOT / ".gitignore").read_bytes()
        assert b"/_connections_manifest" in ignore_text, "connections manifest ignore rule missing"
    (repo / ".gitignore").write_bytes(ignore_text)
    return repo


def _repo_spec(repo: Path) -> Path:
    """Write a minimal manifest input inside an isolated git repo."""
    spec = repo / "migration-spec.json"
    spec.write_text(
        json.dumps({"data_sources": [{"name": "S", "connection": {"class": "postgres", "server": "db"}}]}),
        encoding="utf-8",
    )
    return spec


# --------------------------------------------------------------------------- output safety


def test_default_output_location_is_gitignored_by_convention():
    """The no-argument path must be safe before the script writes customer server/database names."""
    assert cm.DEFAULT_OUT.name == "_connections_manifest"
    assert cm.unignored_output_paths(cm.DEFAULT_OUT) == []


def test_unignored_in_repo_output_is_refused_before_any_manifest_file_is_written(tmp_path):
    """The SES near-miss shape (`ses-prep/`) must fail closed, not merely warn."""
    repo = _git_repo(tmp_path)
    out = repo / "ses-prep"
    assert cm.main(["--bundle", str(_repo_spec(repo)), "--out", str(out)]) == 2
    assert not (out / "connections.md").exists()
    assert not (out / "connections.json").exists()


def test_ignored_in_repo_output_writes_the_manifest(tmp_path):
    """A deliberate ignored target is allowed, so the guard is not a blanket in-repo ban."""
    repo = _git_repo(tmp_path)
    out = repo / "_connections_manifest"
    assert cm.main(["--bundle", str(_repo_spec(repo)), "--out", str(out)]) == 0
    assert (out / "connections.md").is_file()
    assert (out / "connections.json").is_file()


def test_directory_only_ignore_rule_is_checked_against_artifacts_not_the_missing_out_dir(tmp_path):
    """The refusal suggests adding an ignore rule; a normal directory rule must be enough."""
    repo = _git_repo(tmp_path, ignore_text=b"/safe-connections/\n")
    out = repo / "safe-connections"
    assert cm.unignored_output_paths(out) == []
    assert cm.main(["--bundle", str(_repo_spec(repo)), "--out", str(out)]) == 0
    assert (out / "connections.md").is_file()


def test_literal_tilde_output_is_refused_before_the_raw_relative_path_can_leak(tmp_path, monkeypatch):
    """Quoted `~/manifest` is a literal repo-relative path on Windows unless the guard checks it."""
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    assert cm.main(["--bundle", str(_repo_spec(repo)), "--out", "~/manifest"]) == 2
    assert not (repo / "~" / "manifest" / "connections.md").exists()
    assert not (repo / "~" / "manifest" / "connections.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction regression")
def test_junction_output_is_checked_from_the_calling_worktree_root(tmp_path, monkeypatch):
    """A junction child runs git outside the repo unless the guard also anchors at the caller cwd."""
    repo = _git_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = repo / "linkdir"
    mklink = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if mklink.returncode != 0:
        pytest.skip(f"could not create junction: {mklink.stderr or mklink.stdout}")

    monkeypatch.chdir(repo)
    assert cm.main(["--bundle", str(_repo_spec(repo)), "--out=linkdir\\manifest2"]) == 2
    assert not (outside / "manifest2" / "connections.md").exists()


def test_the_trailing_slash_trap_is_real_and_the_guard_avoids_it(tmp_path):
    """Appending a slash can make git report an empty matched pattern for an unignored path."""
    repo = _git_repo(tmp_path)
    stamp = subprocess.run(
        ["git", "check-ignore", "-q", "--", f"{repo / 'definitely-not-ignored'}/"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if stamp.returncode != 0:
        pytest.skip("this git no longer reports every trailing-slash path as ignored")
    assert cm.refuse_unignored_output(repo / "ses-prep") is True


def test_an_unanswerable_git_is_treated_as_unsafe(tmp_path, monkeypatch):
    """A git failure while checking an in-repo target must fail closed, not silently proceed."""
    repo = _git_repo(tmp_path)

    def fake_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
        if args[:1] == ["rev-parse"]:
            return subprocess.CompletedProcess(args, 0, stdout="true\n", stderr="")
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: no answer")

    monkeypatch.setattr(cm, "_git", fake_git)
    with pytest.raises(cm.OutputPathNotIgnoredError):
        cm.unignored_output_paths(repo / "ses-prep")
    assert cm.refuse_unignored_output(repo / "ses-prep") is True


def test_a_missing_git_binary_does_not_silently_disable_the_guard(tmp_path, monkeypatch):
    """If a .git checkout is in scope but git cannot run, the safe answer is refusal."""
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(cm, "_git", lambda _args, _cwd: None)
    with pytest.raises(cm.OutputPathNotIgnoredError):
        cm.unignored_output_paths(repo / "ses-prep")
    assert cm.refuse_unignored_output(repo / "ses-prep") is True


def test_guard_proceeds_outside_any_git_work_tree(tmp_path):
    """A real out-of-checkout target is still allowed."""
    if (
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        == "true"
    ):
        pytest.skip("pytest tmp_path is itself inside a git work tree on this machine")
    assert cm.unignored_output_paths(tmp_path / "anything-at-all") == []
    assert cm.refuse_unignored_output(tmp_path / "anything-at-all") is False


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


def test_structured_allowed_values_are_sanitized_without_python_repr_leaks(tmp_path):
    """A dict/list in an allowed field must be walked, not stringified into a leaking repr."""
    sources = [
        {
            "name": "S",
            "connection": {
                "class": "snowflake",
                "warehouse": {"password": "DICT_MARKER", "name": "WH"},
                "service": [{"api_key": "LIST_DICT_MARKER", "name": "svc"}],
                "powerbi_target_reason": {"token": "DICT_REASON_MARKER"},
            },
        }
    ]
    manifest = cm.build(_spec(sources, tmp_path))
    blob = json.dumps(manifest) + cm.render(manifest)
    for marker in ("DICT_MARKER", "LIST_DICT_MARKER", "DICT_REASON_MARKER"):
        assert marker not in blob
    assert "WH" in blob and "svc" in blob


@pytest.mark.parametrize(
    ("value", "marker"),
    [
        ('PassWord = "QUOTED SPACE MARKER"', "QUOTED SPACE MARKER"),
        ("token='SINGLE_QUOTED_MARKER'", "SINGLE_QUOTED_MARKER"),
        ("user:SCHEMELESS_MARKER@host", "SCHEMELESS_MARKER"),
    ],
)
def test_quoted_and_schemeless_credentials_are_redacted(value, marker, tmp_path):
    """Review #104 found quoted assignments and bare userinfo still reached JSON/Markdown."""
    sources = [{"name": "S", "connection": {"class": "snowflake", "server": value}}]
    manifest = cm.build(_spec(sources, tmp_path))
    blob = json.dumps(manifest) + cm.render(manifest)
    assert marker not in blob


def test_sanitizer_does_not_redact_credential_words_inside_legitimate_values(tmp_path):
    """Over-redaction is also wrong: these are target names, not credentials."""
    sources = [
        {
            "name": "S",
            "connection": {"class": "postgres", "server": "passwordless.example.com", "database": "monkey_key"},
        }
    ]
    rendered = cm.render(cm.build(_spec(sources, tmp_path)))
    assert "passwordless.example.com" in rendered
    assert "monkey_key" in rendered


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


def test_same_named_sources_use_handover_identity_for_consumers(tmp_path):
    """Two distinct sources with one display name must not inherit each other's reports."""
    bundle = tmp_path / "b"
    (bundle / "handover").mkdir(parents=True)
    (bundle / "migration-spec.json").write_text(
        json.dumps(
            {
                "data_sources": [
                    {
                        "name": "World Indicators",
                        "connection": {
                            "class": "federated",
                            "connections": [{"class": "dataengine", "database": "Data/World.tde", "schema": "Extract"}],
                        },
                    },
                    {
                        "name": "World Indicators",
                        "connection": {
                            "class": "federated",
                            "connections": [{"class": "hyper", "database": "Data/World.hyper", "schema": "Extract"}],
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    handovers = {
        "RESTAPISample": {"connection_class": "dataengine", "database": "Data/World.tde", "schema": "Extract"},
        "World_Indicators": {"connection_class": "hyper", "database": "Data/World.hyper", "schema": "Extract"},
    }
    for workbook, connection in handovers.items():
        (bundle / "handover" / f"{workbook}.json").write_text(
            json.dumps(
                {
                    "workbook": {
                        "name": workbook,
                        "bound_datasource": "World Indicators",
                        "embedded_datasources": [
                            {
                                "caption": "World Indicators",
                                "connection_class": connection["connection_class"],
                                "connections": [connection],
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
    entries = cm.build(bundle)["connections"]
    assert {entry["legs"][0]["class"]: entry["used_by"] for entry in entries} == {
        "dataengine": ["RESTAPISample"],
        "hyper": ["World_Indicators"],
    }
    assert sorted(entry["used_by_count"] for entry in entries) == [1, 1]


def test_same_named_sources_without_identity_are_ambiguous_not_union_counted(tmp_path):
    """If handover only says a name shared by several connections, '?' is honest and '2' is not."""
    bundle = tmp_path / "b"
    (bundle / "handover").mkdir(parents=True)
    (bundle / "migration-spec.json").write_text(
        json.dumps(
            {
                "data_sources": [
                    {"name": "Sales", "connection": {"class": "postgres", "server": "prod.example"}},
                    {"name": "Sales", "connection": {"class": "postgres", "server": "dev.example"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    for workbook in ("prod_report", "dev_report"):
        (bundle / "handover" / f"{workbook}.json").write_text(
            json.dumps({"workbook": {"name": workbook, "bound_datasource": "Sales"}}), encoding="utf-8"
        )
    manifest = cm.build(bundle)
    assert [entry["used_by_count"] for entry in manifest["connections"]] == [None, None]
    assert all(not entry["blast_radius_known"] for entry in manifest["connections"])
    assert "| ? |" in cm.render(manifest)
