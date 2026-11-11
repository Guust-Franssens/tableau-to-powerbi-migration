"""`classify_source` must stay a thin translation of `connection_target.powerbi_target`."""

from __future__ import annotations

import sys
import json
import copy
import importlib
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

CASES = [
    ("snowflake", "live", "needs-credential"),
    ("azure_sqldb", "live", "needs-credential"),
    # A live system packaged as an extract still needs its upstream credential.
    ("databricks", "extract", "needs-credential"),
    ("excel-direct", "extract", "no-creds"),
    ("textscan", "live", "no-creds"),
]


def test_classify_source_verdicts_agree_with_connection_target() -> None:
    mapping = {LIVE_SOURCE: "needs-credential", FLAT_FILE: "no-creds"}
    for klass, mode, expected in CASES:
        verdict, reason = classify_source({"class": klass, "mode": mode, "server": "host.example"})
        assert verdict == expected and reason, (klass, mode, verdict, reason)
        assert verdict == mapping.get(powerbi_target(klass, mode)[0], "review"), (klass, mode)


def test_a_stamped_target_is_preferred_over_recomputing() -> None:
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
        ("samples.nyctaxi.trips", {}, "nyctaxi", "trips"),
        ("[schema.with.dot].[Trips and O'Brien]", {}, "schema.with.dot", "Trips and O'Brien"),
        ("[dbo].[Orders]", {"class": "sqlserver", "database": "DB"}, "dbo", "Orders"),
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
        ("trips", {}),  # unqualified, no schema
        ("trips", {"schema": " "}),  # unqualified, unsafe schema
        ("[SAMPLES].[trips]", {"schema": "nyctaxi"}),  # two-part equal to the database
        ("other.nyctaxi.trips", {}),  # three-part conflicting database
        ("nyctaxi.trips", {"database": None}),  # qualified without a recorded database
        ("[nyctaxi].[trips]", {"dbname": "different"}),  # database and dbname disagree
        (None, {}),
        ("a.b.c.d", {}),  # too many parts
        ("[nyctaxi].[trips", {}),  # unclosed bracket
        ("[nyctaxi].trips", {}),  # mixed quoting
        ("[ nyctaxi].[trips]", {}),  # padded component
        ("[nyctaxi].[a\x80b]", {}),  # C1 control
        ('[nyctaxi].[a"b]', {}),  # M string delimiter
        ("[nyctaxi].[a#(b)]", {}),  # M escape
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


@pytest.mark.parametrize("reference", [None, "", "unknown"])
def test_717_binding_counts_flat_file_legs_and_refuses_missing_or_dangling_reference(reference):
    leg = _717_source("[nyctaxi].[trips]")["connection"]
    source = {
        "connection": {"connections": [{**leg, "name": "live"}, {"class": "textscan", "name": "file"}]},
        "tables": [{"name": "Alias", "table": "[nyctaxi].[trips]"}],
    }
    if reference is not None:
        source["tables"][0]["connection"] = reference
    assert pf.resolve_probe_scope(source, source["connection"]["connections"][0])[2] is not None


@pytest.mark.parametrize("name", ["same", ""])
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


def test_the_classifier_hands_off_to_the_refresh_and_still_forbids_building(tmp_path: Path) -> None:
    """A socket-less classifier routes to the live measurement instead of issuing a terminal stop."""
    spec, _source = _live_spec(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "preflight_source_credentials.py"), "--spec", str(spec)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    out = (proc.stdout + proc.stderr).lower()
    assert "refresh_pbip_model.py" in out and "--ui-refresh --canaries" in out
    assert "check_bundle_sources.py" in out
    for phrase in ("terminate the run", "a human must act", "you cannot fix this yourself", "probe_live_source"):
        assert phrase not in out
    assert "may not build" in out and "proof required" in out


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


@pytest.fixture(
    name="refresh_guidance",
    params=[("_print_stop_directive", (1,)), ("_print_remediation", ())],
    ids=["directive", "reminder"],
)
def _refresh_guidance(request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture) -> str:
    """Capture each independently readable next-action message."""
    printer, arguments = request.param
    getattr(pf, printer)(*arguments)
    return caplog.text


@pytest.mark.parametrize("mismatch", [False, True], ids=["matching", "mismatched"])
def test_refresh_guidance_enforces_automatic_source_scope(  # pylint: disable=too-many-locals
    refresh_guidance: str,
    mismatch: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Parse the printed command and exercise real admission over the existing fake live model."""
    for phrase in ("pbip-model-refresh", "Automatic callers", "trusted snapshot of the engine emission"):
        assert phrase in refresh_guidance
    assert "never from the live model" in refresh_guidance and "operator-only" in refresh_guidance
    # pylint: disable=protected-access
    bundle = SCRIPTS.parent / ".github" / "skills" / "pbip-model-refresh"
    monkeypatch.syspath_prepend(str(bundle / "scripts"))
    monkeypatch.syspath_prepend(str(bundle / "tests"))
    controls = importlib.import_module("test_desktop_actions")
    refresh = controls.refresh_pbip_model
    assert Path(refresh.__file__).resolve().is_relative_to(bundle)

    sources = controls._sources()
    if mismatch:
        sources[0]["database"] = "DifferentExpectedDatabase"
    context, scoped_args, calls = controls._scope_route(monkeypatch, tmp_path, sources=sources)
    command = next(line.strip() for line in refresh_guidance.splitlines() if line.strip().startswith("--pid "))
    replacements = {"<pid>": "123", "<expected-sources.json>": scoped_args.automatic_source_scope}
    arguments = [replacements.get(token, token) for token in command.replace("<table> [<table> ...]", "Orders").split()]
    parsed = refresh._build_arg_parser().parse_args(arguments)

    result = refresh._ui_refresh_locked(context, Path("missing-cache.abf"), parsed)

    assert result == (2 if mismatch else 0)
    assert calls == ([] if mismatch else ["Refresh", "canaries", "Save"])
    if mismatch:
        assert "automatic source scope: expected sources: Orders: database" in capsys.readouterr().out
    assert parsed.automatic_source_scope == scoped_args.automatic_source_scope
    assert parsed.ui_refresh and parsed.pid == 123 and parsed.canaries == ["Orders"]
