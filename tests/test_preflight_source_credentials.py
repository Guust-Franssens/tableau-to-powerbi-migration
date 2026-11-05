"""Regression tests for `scripts/preflight_source_credentials.classify_source`.

**Why this file exists.** `classify_source` used to hold a second, independently-maintained opinion
about which Tableau connection classes need a Power BI credential, and it disagreed with
`connection_target.powerbi_target` - the module whose docstring calls that mapping "the single most
consequential mapping decision in a migration". Both disagreements failed OPEN, which is the only
direction that matters for a gate:

1. `mode == "extract"` short-circuited to `no-creds` *before* the class was examined, so a Snowflake
   or Databricks extract was reported as needing no credential. A packaged `.hyper` is Tableau's
   CACHE of an upstream system; migrating onto it yields a model that can never refresh.
2. Liveness came from a DENY-list of database classes, so any class not on it fell through to
   "review". Measured 2026-08-04: `azure_sqldb` appeared nowhere in the repo, so a workbook joining
   Azure SQL + Snowflake + Databricks printed "No live sources: all extract/flat" and the credential
   gate never armed.

The fix was to delete the duplicate policy, not to extend the list - a deny-list of live systems is
incomplete by construction and every omission is a silent gate failure. These tests exist to keep it
deleted: `test_classify_source_agrees_with_connection_target` fails the moment a second opinion
reappears.
"""

from __future__ import annotations

import sys
import json
import copy
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
# pylint: disable=wrong-import-position,import-error
from connection_target import FLAT_FILE, LIVE_SOURCE, powerbi_target
from preflight_source_credentials import classify_source, cmd_classify
import preflight_source_credentials as pf

# (class, mode, expected verdict). The cases that used to be wrong are marked.
CASES = [
    # --- live systems, live mode -------------------------------------------------------------
    ("snowflake", "live", "needs-credential"),
    ("databricks", "live", "needs-credential"),
    # `azure_sqldb` is the real class Tableau writes for Azure SQL Database. It was absent from the
    # old deny-list, so this returned "review" and the gate stayed disarmed on a 100%-live workbook.
    ("azure_sqldb", "live", "needs-credential"),
    # --- live systems packaged AS AN EXTRACT: the case that looked like a file and isn't ---------
    # All three used to return "no-creds" because `mode == "extract"` was tested first.
    ("snowflake", "extract", "needs-credential"),
    ("databricks", "extract", "needs-credential"),
    ("azure_sqldb", "extract", "needs-credential"),
    # --- genuine flat files stay credential-free, in either mode ---------------------------------
    ("excel-direct", "extract", "no-creds"),
    ("textscan", "live", "no-creds"),
    ("ogr", "extract", "no-creds"),
    ("ogrdirect", "extract", "no-creds"),
]


@pytest.mark.parametrize(("klass", "mode", "expected"), CASES)
def test_classify_source_verdicts(klass: str, mode: str, expected: str) -> None:
    """The eight cases that pin the corrected behaviour. Six of these used to be wrong."""
    verdict, reason = classify_source({"class": klass, "mode": mode, "server": "host.example"})
    assert verdict == expected, f"{klass}/{mode}: expected {expected}, got {verdict} ({reason})"
    assert reason, "a verdict must always carry a reason"


@pytest.mark.parametrize(("klass", "mode", "_expected"), CASES)
def test_classify_source_agrees_with_connection_target(klass: str, mode: str, _expected: str) -> None:
    """The two modules must never diverge again.

    This is the structural guard: `classify_source` is required to be a thin translation of
    `powerbi_target`, so re-introducing any independent class policy in the preflight fails here
    rather than silently in a customer migration.
    """
    verdict, _ = classify_source({"class": klass, "mode": mode, "server": "host.example"})
    target, _ = powerbi_target(klass, mode)
    mapping = {LIVE_SOURCE: "needs-credential", FLAT_FILE: "no-creds"}
    assert verdict == mapping.get(target, "review"), (
        f"{klass}/{mode}: preflight says {verdict!r} but connection_target says {target!r} - "
        "the preflight must not hold a second opinion"
    )


def test_a_stamped_target_is_preferred_over_recomputing() -> None:
    """The parser stamps `powerbi_target` onto every connection; honour it.

    Recomputing would silently ignore a decision the parser may have made with more context than the
    class string alone.
    """
    verdict, reason = classify_source(
        {
            "class": "some-future-connector",
            "mode": "live",
            "server": "host.example",
            "powerbi_target": LIVE_SOURCE,
            "powerbi_target_reason": "stamped by the parser",
        }
    )
    assert verdict == "needs-credential"
    assert "stamped by the parser" in reason


def test_an_unknown_class_is_never_silently_cleared() -> None:
    """An unrecognised class must never come back as `no-creds`.

    Under-connecting is far worse than over-asking for a credential: the model refreshes once from
    stale cached rows and then never again.
    """
    verdict, _ = classify_source({"class": "brand-new-warehouse-2031", "mode": "live"})
    assert verdict != "no-creds"


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def test_unkeyable_live_source_still_blocks_instead_of_clearing(tmp_path: Path, caplog) -> None:
    """A known-live source whose key cannot be derived must stay blocking, and nothing is written."""
    spec = tmp_path / "migration-spec.json"
    spec.write_text(
        json.dumps(
            {
                "data_sources": [
                    {
                        "name": "OnlyName",
                        "connection": {"class": "sqlserver", "mode": "live", "powerbi_target": LIVE_SOURCE},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    caplog.set_level("INFO", logger="preflight_source_credentials")

    assert cmd_classify(spec) == 1
    assert _files(tmp_path) == {"migration-spec.json"}
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "cannot derive a stable" in messages
    assert "PROOF REQUIRED - 1 live source(s)" in messages
    assert "No live sources" not in messages


def _717_source(identifier, **connection):
    return {
        "name": "Fictitious source",
        "connection": {
            "class": "databricks",
            "server": "adb.example",
            "http_path": "/sql/1.0/warehouses/fixture",
            "database": "samples",
            **connection,
        },
        "tables": [{"name": "Display Trips", "table": identifier}],
    }


@pytest.mark.parametrize(
    ("identifier", "connection", "schema", "table"),
    [
        ("trips", {"schema": "nyctaxi"}, "nyctaxi", "trips"),
        ("[trips]", {"schema": "default"}, "default", "trips"),
        ("nyctaxi.trips", {}, "nyctaxi", "trips"),
        ("[nyctaxi].[trips]", {"schema": "default"}, "nyctaxi", "trips"),
        ("samples.nyctaxi.trips", {}, "nyctaxi", "trips"),
        ("[samples].[nyctaxi].[trips]", {}, "nyctaxi", "trips"),
        ("[schema.with.dot].[Trips and O'Brien]", {}, "schema.with.dot", "Trips and O'Brien"),
        ("[dbo].[Orders]", {"class": "sqlserver", "database": "DB"}, "dbo", "Orders"),
        ("[PUBLIC].[X]", {"class": "snowflake", "database": "DB", "warehouse": "WH"}, "PUBLIC", "X"),
    ],
)
def test_717_scope_decodes_only_recorded_physical_identity(identifier, connection, schema, table):
    source = _717_source(identifier, **connection)
    before = copy.deepcopy(source)
    resolved, tables, error = pf.resolve_probe_scope(source, source["connection"])
    assert error is None
    assert resolved["schema"] == schema
    assert [item["name"] for item in tables] == [table]
    assert source == before


@pytest.mark.parametrize(
    ("identifier", "connection"),
    [
        ("trips", {}),
        ("[samples].[trips]", {}),
        ("[SAMPLES].[trips]", {"schema": "nyctaxi"}),
        ("nyctaxi.trips", {"database": None}),
        ("other.nyctaxi.trips", {}),
        ("samples.nyctaxi.trips", {"database": None}),
        ("[nyctaxi].[trips]", {"dbname": "different"}),
        (None, {}),
        ("", {}),
        (7, {}),
        (False, {}),
        ("[nyctaxi]..[trips]", {}),
        ("a.b.c.d", {}),
        ("[nyctaxi].[trips", {}),
        ("[nyctaxi]trips", {}),
        ("[nyctaxi].trips", {}),
        ('"nyctaxi"."trips"', {}),
        ("`nyctaxi`.`trips`", {}),
        ("[nyctaxi].[a]]b]", {}),
        ("[ nyctaxi].[trips]", {}),
        ("[nyctaxi ].[trips]", {}),
        ("[nyctaxi].[ trips]", {}),
        ("[nyctaxi].[a\nb]", {}),
        ("[nyctaxi].[a\x80b]", {}),
        ('[nyctaxi].[a"b]', {}),
        ("[nyctaxi].[a#(b)]", {}),
        ("trips", {"schema": 7}),
        ("trips", {"schema": " "}),
        ("trips", {"schema": 'bad"schema'}),
    ],
)
def test_717_scope_refuses_ambiguous_malformed_and_missing_identity(identifier, connection):
    source = _717_source(identifier, **connection)
    _, _, error = pf.resolve_probe_scope(source, source["connection"])
    assert error is not None, "unestablished identity must refuse rather than recover a plausible suffix"


def test_717_qualified_sibling_never_supplies_an_unqualified_tables_schema():
    source = _717_source("[nyctaxi].[trips]")
    source["tables"].append({"name": "Other", "table": "Other"})
    assert pf.resolve_probe_scope(source, source["connection"])[2] is not None


@pytest.mark.parametrize("reference", [None, "", "unknown", 7])
def test_717_binding_counts_flat_file_legs_and_refuses_missing_or_dangling_reference(reference):
    leg = _717_source("[nyctaxi].[trips]")["connection"]
    source = {
        "connection": {"connections": [{**leg, "name": "live"}, {"class": "textscan", "name": "file"}]},
        "tables": [{"name": "Alias", "table": "[nyctaxi].[trips]"}],
    }
    if reference is not None:
        source["tables"][0]["connection"] = reference
    assert pf.resolve_probe_scope(source, source["connection"]["connections"][0])[2] is not None


@pytest.mark.parametrize("name", ["same", "", 7])
def test_717_duplicate_or_invalid_leg_names_refuse(name):
    leg = _717_source("[nyctaxi].[trips]")["connection"]
    source = {
        "connection": {"connections": [{**leg, "name": name}, {**leg, "name": name}]},
        "tables": [{"name": "Alias", "table": "[nyctaxi].[trips]", "connection": name}],
    }
    assert pf.resolve_probe_scope(source, source["connection"]["connections"][0])[2] is not None


def test_717_no_target_identity_is_null_not_custom_only_or_valid(monkeypatch):
    identities = []
    real_dumps = pf.json.dumps

    def capture(value, *args, **kwargs):
        identities.append(value.copy())
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(pf.json, "dumps", capture)
    source = _717_source("[nyctaxi].[trips]")
    valid_key = pf._leg_key(source, 0, source["connection"])
    assert identities[-1]["ordinary_tables"] == ["trips"]
    source["tables"] = []
    no_target_key = pf._leg_key(source, 0, source["connection"])
    assert identities[-1]["ordinary_tables"] is None
    source["tables"] = [{"name": "Q", "source_relation": "custom-sql", "custom_sql": "SELECT 1"}]
    sql_key = pf._leg_key(source, 0, source["connection"])
    assert "ordinary_tables" not in identities[-1]
    assert len({valid_key, no_target_key, sql_key}) == 3

    leg = source["connection"]
    source["connection"] = {"connections": [{**leg, "name": "live"}, {"class": "textscan", "name": "file"}]}
    source["tables"] = [{"name": "File", "table": "[nyctaxi].[trips]", "connection": "file"}]
    assert pf._leg_key(source, 0, source["connection"]["connections"][0]) == no_target_key
    assert identities[-1]["ordinary_tables"] is None


def test_717_key_tracks_physical_case_but_not_alias_order_or_duplicates():
    source = _717_source("[nyctaxi].[trips]")
    source["tables"].append({"name": "Second Alias", "table": "[nyctaxi].[fares]"})
    key = pf._leg_key(source, 0, source["connection"])
    source["tables"].reverse()
    source["tables"][0]["name"] = "Renamed"
    source["tables"].append({"name": "Duplicate display", "table": "[nyctaxi].[trips]"})
    assert pf._leg_key(source, 99, source["connection"]) == key
    source["tables"][0]["table"] = "[nyctaxi].[Fares]"
    assert pf._leg_key(source, 0, source["connection"]) != key
    source["tables"][0]["table"] = "[nyctaxi].[fares]"
    for table in source["tables"]:
        table["table"] = table["table"].replace("[nyctaxi]", "[NYCTAXI]")
    assert pf._leg_key(source, 0, source["connection"]) != key


def test_717_resolution_preserves_manifest_configuration_and_handover_identity():
    from connections_manifest import _connection_identity, safe_connection  # noqa: PLC0415

    source = _717_source("[nyctaxi].[trips]", schema="default")
    original = copy.deepcopy(source)
    identity = _connection_identity(source["connection"])
    projection = safe_connection(source["connection"])
    resolved, _, error = pf.resolve_probe_scope(source, source["connection"])
    assert error is None and resolved["schema"] == "nyctaxi"
    assert source == original
    assert _connection_identity(source["connection"]) == identity
    assert safe_connection(source["connection"]) == projection
    assert projection["schema"] == "default"
    with_name = {**source["connection"], "name": "exact-reference"}
    assert _connection_identity(with_name) == identity
    assert safe_connection(with_name) == projection


def test_missing_class_does_not_launder_live_source_to_extract_only(tmp_path: Path, caplog) -> None:
    """HIGH 1: missing class is unprobeable/blocking, not review/no-live-sources."""
    spec = tmp_path / "migration-spec.json"
    spec.write_text(
        json.dumps(
            {
                "data_sources": [
                    {
                        "name": "OnlyName",
                        "connection": {"server": "sql.example", "mode": "live", "powerbi_target": LIVE_SOURCE},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    caplog.set_level("INFO", logger="preflight_source_credentials")

    assert cmd_classify(spec) == 1
    assert _files(tmp_path) == {"migration-spec.json"}
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "connection leg has no class" in messages
    assert "No live sources" not in messages


def _live_spec(tmp_path: Path) -> tuple[Path, dict]:
    source = {
        "name": "Orders",
        "connection": {
            "class": "sqlserver",
            "server": "preflight.example.invalid",
            "database": "fixture",
            "schema": "dbo",
            "powerbi_target": LIVE_SOURCE,
        },
        "tables": [{"name": "Orders", "table": "[dbo].[Orders]"}],
    }
    spec = tmp_path / "migration-spec.json"
    spec.write_text(json.dumps({"data_sources": [source]}), encoding="utf-8")
    return spec, source


def test_canonical_live_source_classification_is_read_only(tmp_path: Path) -> None:
    """A live source exits 1 and writes nothing; the leg identity is the classifier key, not a display."""
    spec, source = _live_spec(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*")}

    assert cmd_classify(spec) == 1
    assert {path: path.read_bytes() for path in tmp_path.rglob("*")} == before
    assert [leg[0] for leg in pf._classify_legs(source, 0)] == [pf._leg_key(source, 0, source["connection"])]


def _classifier_output(tmp_path: Path) -> str:
    spec, _source = _live_spec(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "preflight_source_credentials.py"), "--spec", str(spec)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    return (proc.stdout + proc.stderr).lower()


def test_the_classifier_sends_the_agent_to_the_refresh_instead_of_terminating(tmp_path: Path) -> None:
    """The classifier opens no socket, so it must hand off to the measurement, not issue a terminal stop.

    Measured 2026-08-02: an unconditional "STOP - A HUMAN MUST ACT / TERMINATE THE RUN NOW" made 10 of
    15 models skip the live measurement, and claude-opus-5 refused a fully credentialed, reachable
    warehouse. The measurement is now the pbip-model-refresh UI refresh of the shipped working copy.
    """
    out = _classifier_output(tmp_path)

    assert "refresh_pbip_model.py" in out and "--ui-refresh --canaries" in out
    assert "check_bundle_sources.py" in out
    forbidden = ["terminate the run", "a human must act", "you cannot fix this yourself", "probe_live_source"]
    present = [phrase for phrase in forbidden if phrase in out]
    assert not present, f"a socket-less classifier must not issue a terminal stop; found {present}"


def test_the_classifier_still_forbids_building(tmp_path: Path) -> None:
    """Paired control: softening the directive must not soften the actual prohibition."""
    out = _classifier_output(tmp_path)
    assert "may not build" in out
    assert "proof required" in out


def test_a_failed_classification_never_fails_the_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The parser's post-parse classification is a read-only print; its failure no longer exits 3."""
    import parse_tableau  # pylint: disable=import-outside-toplevel

    source = Path(__file__).parent / "fixtures" / "federated_multi_connection.twb"
    calls = []

    def _failed_classification(*args, **_kwargs):
        calls.append(args[0])
        return subprocess.CompletedProcess(args[0], 3)

    monkeypatch.setattr(parse_tableau.subprocess, "run", _failed_classification)
    monkeypatch.setattr(sys, "argv", ["parse_tableau.py", str(source), "-o", str(tmp_path / "parsed.json")])
    parse_tableau.main()
    assert (tmp_path / "parsed.json").is_file()
    assert len(calls) == 1 and calls[0][1].endswith("preflight_source_credentials.py")


def test_source_keys_survive_datasource_reordering() -> None:
    """Source keys are endpoint identities, not array positions."""
    first = [
        {
            "name": "A",
            "connection": {
                "class": "sqlserver",
                "server": "a.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
        },
        {
            "name": "B",
            "connection": {
                "class": "sqlserver",
                "server": "b.example",
                "database": "db",
                "powerbi_target": "live_source",
            },
        },
    ]
    swapped = [first[1], first[0]]

    first_keys = {
        source["name"]: pf._leg_key(source, index, source["connection"]) for index, source in enumerate(first)
    }
    swapped_keys = {
        source["name"]: pf._leg_key(source, index, source["connection"]) for index, source in enumerate(swapped)
    }

    assert first_keys == swapped_keys


def test_snowflake_role_changes_the_source_key() -> None:
    """A Snowflake role is part of the source identity."""
    base = {
        "class": "snowflake",
        "server": "acct.snowflakecomputing.com",
        "database": "DB",
        "schema": "PUBLIC",
        "warehouse": "WH",
    }
    analyst = {**base, "role": "ANALYST"}
    restricted = {**base, "role": "RESTRICTED"}

    tables = [{"name": "Orders", "table": "[Orders]"}]
    assert pf._leg_key({"connection": analyst, "tables": tables}, 0, analyst) != pf._leg_key(
        {"connection": restricted, "tables": tables}, 0, restricted
    )


def test_schema_changes_the_source_key_for_live_connectors() -> None:
    """The schema is part of the source identity for every keyed live connector."""
    cases = [
        {"class": "sqlserver", "server": "sql.example", "database": "DB"},
        {
            "class": "databricks",
            "server": "adb.example",
            "database": "DB",
            "http_path": "/sql/1.0/warehouses/x",
        },
        {"class": "snowflake", "server": "acct.snowflakecomputing.com", "database": "DB", "warehouse": "WH"},
    ]
    for base in cases:
        dbo = {**base, "schema": "dbo"}
        restricted = {**base, "schema": "restricted"}
        tables = [{"name": "Orders", "table": "[Orders]"}]
        assert pf._leg_key({"connection": dbo, "tables": tables}, 0, dbo) != pf._leg_key(
            {"connection": restricted, "tables": tables}, 0, restricted
        )


def test_missing_endpoint_identity_refuses_a_source_key() -> None:
    """No fallback index/name key when endpoint identity is unavailable."""
    with pytest.raises(ValueError):
        pf._leg_key({}, 0, {})
    with pytest.raises(ValueError):
        pf._leg_key({"name": "OnlyName"}, 0, {"class": "sqlserver"})
