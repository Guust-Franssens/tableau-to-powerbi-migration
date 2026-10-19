"""Custom SQL never starts a default probe; ordinary row probing retains its existing contract.

Only same-invocation, same-scope ordinary DATA_OK may supply connection evidence. All other
custom-only sources stop without M generation, sockets, a PBIP, Desktop, or a refresh.
"""

# pylint: disable=protected-access,wrong-import-position

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import credential_gate as gate  # noqa: E402
import preflight_source_credentials as preflight  # noqa: E402
import probe_live_source  # noqa: E402
from parse_tableau import parse_workbook  # noqa: E402

SNOWFLAKE = {
    "class": "snowflake",
    "server": "https://ORG-ACCOUNT.snowflakecomputing.com/",
    "warehouse": "WH",
    "database": "DB",
    "schema": "PUBLIC",
    "powerbi_target": "live_source",
}
DATABRICKS = {
    "class": "databricks",
    "server": "https://adb.example.azuredatabricks.net/",
    "http_path": "/sql/1.0/warehouses/abc",
    "database": "hive_metastore",
    "schema": "default",
    "powerbi_target": "live_source",
}
SQLSERVER = {
    "class": "sqlserver",
    "server": "sql.example.com",
    "database": "DB",
    "schema": "dbo",
    "powerbi_target": "live_source",
}
UNSUPPORTED = {"class": "oracle", "server": "oracle.example", "database": "DB", "powerbi_target": "live_source"}
CONNECTIONS = (SQLSERVER, DATABRICKS, SNOWFLAKE)
TOKEN = "CONNECTION_OK_QUERY_UNVALIDATED"
SENTINEL = "CUSTOM_SQL_MUST_NOT_RUN_690"
EXPENSIVE_SQL = f"SELECT dbo.{SENTINEL}(f.payload) FROM huge_fact f CROSS JOIN huge_fact g"
SIDE_EFFECT_SQL = f"EXEC dbo.{SENTINEL}; DROP TABLE private_fixture;"


@pytest.fixture(autouse=True)
def _canonical_marker_only_probe_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom-SQL verdicts stay independent of Windows ACL tooling."""
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gate, "_ancestry", lambda: [])


def _source(tables: list[dict], fields: list[dict] | None = None) -> dict:
    for table in tables:
        if table.get("custom_sql") is not None:
            table["source_relation"] = "custom-sql"
        elif table.get("name") and table.get("source_relation") != "custom-sql":
            table.setdefault("table", f"[{table['name']}]")
    return {
        "connection": dict(SNOWFLAKE),
        "tables": tables,
        "fields": [{"kind": "column", "internal_name": "[Col]"}] if fields is None else fields,
    }


def _custom(conn: dict, sql: object = EXPENSIVE_SQL) -> dict:
    return {
        "connection": dict(conn),
        "tables": [{"name": "Q", "source_relation": "custom-sql", "custom_sql": sql}],
    }


def _spec(root: Path, sources: list[dict]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for source in sources:
        for table in source.get("tables", []):
            if table.get("source_relation") != "custom-sql" and table.get("name"):
                schema = source["connection"].get("schema")
                table.setdefault("table", f"[{schema}].[{table['name']}]")
    path = root / "migration-spec.json"
    path.write_text(json.dumps({"data_sources": sources}), encoding="utf-8")
    return path


def _persist_source(root: Path, source: dict) -> str:
    """Persist the exact current fact whose canonical key the probe writer records."""
    _spec(root, [source])
    return preflight._leg_key(source, 0, source["connection"])


def _audit(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / gate.AUDIT).read_text(encoding="utf-8").splitlines()]


def _assess_sources(root: Path, sources: list[dict], *, authorized: bool = False) -> gate.DataAccessAssessment:
    return gate.assess_data_access(
        root,
        package_spec={"data_sources": sources},
        package_data_sources={
            "self_contained": True,
            "omissions": [],
            "neutralized": [],
            "retained_network": [],
            "shipped": [],
            "binding": None,
            "parameter": None,
            "bytes": 0,
        },
        fallback_authorization="model_only_unvalidated" if authorized else "stop",
        requested_scope="model_only" if authorized else "model_and_report",
    )


@pytest.fixture(name="effects")
def effects_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Observe every operation while replacing only actual Desktop/network effects."""
    events: list[tuple[str, object]] = []
    models: list[Path] = []
    original_build = probe_live_source.build_m_query
    original_write = probe_live_source._write_probe_model

    def build(conn: dict, table: str, column: str, custom_sql: str | None = None) -> tuple[str, str]:
        events.append(("m", table))
        return original_build(conn, table, column, custom_sql=custom_sql)

    def observe(kind: str, value: object = "", result: bool = True) -> bool:
        events.append((kind, value))
        return result

    def write(root: Path, query: str, table: str, column: str) -> Path:
        events.append(("pbip", query))
        model = original_write(root, query, table, column)
        tmdl = next(model.glob("*.SemanticModel/definition/tables/*.tmdl")).read_text(encoding="utf-8")
        assert "column 'ProbeOK'" in tmdl and "sourceColumn: ProbeOK" in tmdl
        assert "column 'Col'" not in tmdl
        models.append(model)
        return model

    def open_desktop(pbip: Path) -> int:
        events.append(("desktop", str(pbip)))
        return 4242

    def refresh(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        events.append(("refresh", argv))
        assert Path(argv[1]) == probe_live_source.SKILL_SCRIPTS / "refresh_pbip_model.py"
        model = models[-1]
        if any(SENTINEL in path.read_text(encoding="utf-8") for path in model.rglob("*.tmdl")):
            events.append(("customer-sql-executed", 1))
        table = argv[argv.index("--tables") + 1]
        return subprocess.CompletedProcess(argv, 0, stdout=f"REFRESH: TABLES_OK '{table}'", stderr="")

    def lift(_root: Path, _what: str, names: list[str]) -> bool:
        events.append(("lift", names))
        return False

    monkeypatch.setattr(probe_live_source, "build_m_query", build)
    monkeypatch.setattr(probe_live_source, "_write_probe_model", write)
    monkeypatch.setattr(probe_live_source, "_host_resolves", lambda host: observe("dns", host))
    monkeypatch.setattr(probe_live_source, "_network_fault_observed", lambda _conn: observe("network", result=False))
    monkeypatch.setattr(probe_live_source, "_open_desktop", open_desktop)
    monkeypatch.setattr(probe_live_source, "_wait_for_catalog", lambda _pid: observe("catalog"))
    monkeypatch.setattr(probe_live_source, "_record_desktop_lifecycle", lambda *_args: {})
    monkeypatch.setattr(probe_live_source, "_close", lambda _pid, _path: observe("close"))
    monkeypatch.setattr(probe_live_source, "_lift_gate", lift)
    monkeypatch.setattr(probe_live_source, "_run_refresh", refresh)
    return events


def _assert_terminal(root: Path, code: int, token: str, effects: list[tuple[str, object]]) -> None:
    assert code == 1, "custom-SQL verdicts must exit 1"
    assert not any(kind == "lift" for kind, _value in effects), "custom SQL must not request a gate lift"
    entries = _audit(root)
    assert not any(entry["action"] in ("probe-cleared", "authorize") for entry in entries), (
        "custom SQL must not earn clearance or authorization"
    )
    assert not any("proved_names" in entry for entry in entries)
    assert entries[-1]["action"] == "probe-error", "custom outcomes must use the keyed probe-error envelope"
    assert entries[-1]["sources"] and all(key.startswith("source-key:") for key in entries[-1]["sources"])
    assert entries[-1]["detail"].startswith(token + ":"), "custom outcome detail must preserve its verdict"
    assert gate._read_audit_trail(root)[1] is None


@pytest.mark.parametrize(
    "conn", [*CONNECTIONS, UNSUPPORTED], ids=["sqlserver", "databricks", "snowflake", "unsupported"]
)
@pytest.mark.parametrize("sql", [EXPENSIVE_SQL, SIDE_EFFECT_SQL, "-- only a comment\n", "/* comment */", "", None])
def test_custom_only_has_no_automatic_operation(
    tmp_path: Path, effects: list[tuple[str, object]], caplog: pytest.LogCaptureFixture, conn: dict, sql: str | None
) -> None:
    """The real CLI must stop before even DNS/M generation, regardless of payload or connector."""
    source = _custom(conn, sql)
    source["tables"][0]["name"] = SIDE_EFFECT_SQL
    spec = _spec(tmp_path, [source])
    before = spec.read_bytes()
    caplog.set_level("INFO", logger="probe_live_source")
    code = probe_live_source.main(["--spec", str(spec)])
    assert effects == [], "custom SQL must not perform automatic operations"
    assert sum(SENTINEL in str(value) for kind, value in effects if kind in ("pbip", "refresh")) == 0
    _assert_terminal(tmp_path, code, "OPERATOR_REQUIRED", effects)
    assert spec.read_bytes() == before
    assert not list((tmp_path / "_probe").rglob("*.pbip"))
    assert SENTINEL not in caplog.text + (tmp_path / gate.AUDIT).read_text(encoding="utf-8")
    assert "PROBE: OPERATOR_REQUIRED" in caplog.text
    assert "Power BI reached" not in caplog.text and "PROBE: DATA_OK" not in caplog.text


@pytest.mark.parametrize("conn", [*CONNECTIONS, UNSUPPORTED])
@pytest.mark.parametrize("sql", [EXPENSIVE_SQL, SIDE_EFFECT_SQL, "-- comment", "/* comment */", ""])
def test_custom_sql_cannot_enter_the_m_builder(conn: dict, sql: str) -> None:
    """The old native-query/normalization assertions are replaced by a stronger no-M boundary."""
    with pytest.raises(ValueError, match="custom SQL requires operator validation; no automatic M query"):
        probe_live_source.build_m_query(conn, "Q", "Col", custom_sql=sql)


class SqlReadTrap(dict):
    """Make any attempted default-path SQL payload read fail before a formatter can hide it."""

    def get(self, key: str, default: object = None) -> object:
        if key == "custom_sql":
            raise AssertionError("the default path read the custom SQL payload")
        return super().get(key, default)

    def __getitem__(self, key: str) -> object:
        if key == "custom_sql":
            raise AssertionError("the default path read the custom SQL payload")
        return super().__getitem__(key)

    def items(self) -> object:
        raise AssertionError("the default path traversed or serialized the custom SQL payload")

    def values(self) -> object:
        raise AssertionError("the default path traversed the custom SQL payload")

    def copy(self) -> object:
        raise AssertionError("the default path copied the custom SQL payload")


def _protect_loaded_sql(monkeypatch: pytest.MonkeyPatch, *, memory_error: bool = False) -> list[dict]:
    """Trap probe loading, then restore JSON-native canonical reads before the real writer."""
    original_load, original_dumps, original_hash = json.load, json.dumps, hashlib.sha256
    original_record = probe_live_source._record_attempt
    loaded_sources: list[dict] = []
    protected: list[object] = []

    def load(handle: object, **kwargs: object) -> dict:
        document = original_load(handle, **kwargs)
        nested = document.get("nested", {})
        sources = document.get("data_sources", nested if isinstance(nested, list) else nested.get("data_sources", []))
        loaded_sources.extend(sources)
        for source in sources:
            for index, table in enumerate(source.get("tables", [])):
                if table.get("source_relation") == "custom-sql":
                    source["tables"][index] = SqlReadTrap(table)
                    protected.extend((document, sources, source, source["tables"]))
        return document

    def dumps(value: object, *args: object, **kwargs: object) -> str:
        if any(value is source for source in protected):
            if memory_error:
                raise MemoryError("custom SQL serialization exhausted memory before its verdict")
            raise AssertionError("the default loader serialized a custom SQL source")
        return original_dumps(value, *args, **kwargs)

    def sha256(value: bytes = b"", **kwargs: object) -> object:
        assert SENTINEL.encode() not in value, "the probe loader hashed custom SQL"
        return original_hash(value, **kwargs)

    def record(*args: object, **kwargs: object) -> None:
        monkeypatch.setattr(json, "load", original_load)
        monkeypatch.setattr(json, "dumps", original_dumps)
        monkeypatch.setattr(hashlib, "sha256", original_hash)
        original_record(*args, **kwargs)

    monkeypatch.setattr(json, "load", load)
    monkeypatch.setattr(json, "dumps", dumps)
    monkeypatch.setattr(hashlib, "sha256", sha256)
    monkeypatch.setattr(probe_live_source, "_record_attempt", record)
    return loaded_sources


@pytest.mark.parametrize(
    "case",
    [
        (shape, composition, payload)
        for shape in ("spec", "engine", "engine-direct")
        for composition in ("custom-only", "mixed-leg", "earlier-source")
        for payload in ("string", "null", "huge")
    ],
    ids="-".join,
)
def test_cli_loader_leaves_payload_opaque(
    tmp_path: Path,
    effects: list[tuple[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: tuple[str, str, str],
) -> None:
    """Probe loading is opaque; canonical JSON identity comparison is independently permitted."""
    shape, composition, payload = case
    custom = _custom(
        SQLSERVER,
        {
            "string": SIDE_EFFECT_SQL,
            "null": None,
            "huge": SENTINEL + ("x" * 2_000_000),
        }[payload],
    )
    sources = [custom]
    if composition == "mixed-leg":
        custom["tables"].insert(0, {"name": "REAL_TABLE", "table": "[dbo].[REAL_TABLE]"})
    elif composition == "earlier-source":
        sources.insert(
            0, {"connection": dict(SQLSERVER), "tables": [{"name": "REAL_TABLE", "table": "[dbo].[REAL_TABLE]"}]}
        )
    if shape == "spec":
        path = _spec(tmp_path, sources)
        args = ["--spec", str(path)]
    else:
        path = tmp_path / "report.json"
        path.write_text(
            json.dumps({"nested": sources if shape == "engine-direct" else {"data_sources": sources}}),
            encoding="utf-8",
        )
        (tmp_path / gate.ENGINE_RECEIPT).write_text("{}", encoding="utf-8")
        args = ["--bundle", str(tmp_path)]
    original_bytes = path.read_bytes()
    _protect_loaded_sql(monkeypatch, memory_error=payload == "huge")
    caplog.set_level("INFO", logger="probe_live_source")
    try:
        code = probe_live_source.main(args)
    except MemoryError:
        pytest.fail("the probe loader serialized a custom source before the supported writer")
    token = "OPERATOR_REQUIRED" if composition == "custom-only" else TOKEN
    _assert_terminal(tmp_path, code, token, effects)
    assert _audit(tmp_path)[-1]["sources"] == [preflight._leg_key(sources[-1], 0, SQLSERVER)]
    assert [value for kind, value in effects if kind == "m"] == ([] if composition == "custom-only" else ["REAL_TABLE"])
    if composition == "custom-only":
        assert effects == [], "custom SQL must not perform automatic operations"
        assert not list((tmp_path / "_probe").rglob("*.pbip"))
    assert path.read_bytes() == original_bytes
    assert SENTINEL not in caplog.text + (tmp_path / gate.AUDIT).read_text(encoding="utf-8") + repr(effects)


@pytest.mark.parametrize(
    "case",
    [(shape, payload) for shape in ("spec", "engine") for payload in ("boolean", "list", "object")],
    ids="-".join,
)
def test_malformed_json_payloads_never_execute_or_earn_authority(
    tmp_path: Path,
    effects: list[tuple[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: tuple[str, str],
) -> None:
    """Non-schema JSON is a robustness negative, not a supported custom-query positive."""
    shape, payload = case
    source = _custom(
        SQLSERVER,
        {
            "boolean": True,
            "list": [SIDE_EFFECT_SQL, {"custom_sql": EXPENSIVE_SQL}],
            "object": {"data_sources": [_custom(UNSUPPORTED)], "binding_signal": {"published_ds_name": SENTINEL}},
        }[payload],
    )
    path = _spec(tmp_path, [source]) if shape == "spec" else tmp_path / "report.json"
    if shape == "engine":
        path.write_text(json.dumps({"data_sources": [source]}), encoding="utf-8")
        (tmp_path / gate.ENGINE_RECEIPT).write_text("{}", encoding="utf-8")
    before = path.read_bytes()
    _protect_loaded_sql(monkeypatch)
    caplog.set_level("INFO", logger="probe_live_source")
    code = probe_live_source.main(
        ["--spec" if shape == "spec" else "--bundle", str(path if shape == "spec" else tmp_path)]
    )
    assert code != 0 and effects == [], "malformed custom SQL must not execute or create an automatic operation"
    entries = _audit(tmp_path) if (tmp_path / gate.AUDIT).exists() else []
    assert not any(row["action"] in ("probe-data_ok", "probe-cleared", "authorize") for row in entries)
    assert not (tmp_path / gate.OVERRIDE).exists()
    result = _assess_sources(tmp_path, [source])
    assert result.state in ("blocked", "cannot_establish")
    assert TOKEN not in caplog.text and "PROBE: DATA_OK" not in caplog.text
    assert SENTINEL not in caplog.text + json.dumps(entries) + repr(effects)
    assert path.read_bytes() == before


@pytest.mark.parametrize("same_query", [True, False], ids=["exact-mirror", "distinct-query-collision"])
def test_engine_query_identity_distinguishes_exact_mirrors_from_collisions(
    tmp_path: Path, effects: list[tuple[str, object]], caplog: pytest.LogCaptureFixture, same_query: bool
) -> None:
    """Real canonical root facts compare full JSON sources, not only their projected gate keys."""
    first = _custom(SQLSERVER)
    second = _custom(SQLSERVER, EXPENSIVE_SQL if same_query else SIDE_EFFECT_SQL)
    key = preflight._leg_key(first, 0, SQLSERVER)
    assert key == preflight._leg_key(second, 1, SQLSERVER)
    report = tmp_path / "report.json"
    handover = tmp_path / "handover" / "fixture.json"
    handover.parent.mkdir()
    report.write_text(json.dumps({"data_sources": [first]}), encoding="utf-8")
    handover.write_text(json.dumps({"data_sources": [second]}), encoding="utf-8")
    (tmp_path / gate.ENGINE_RECEIPT).write_text("{}", encoding="utf-8")
    before = (report.read_bytes(), handover.read_bytes())
    current, refusal = gate._gate_root_live_keys(tmp_path)
    assert (current, refusal) == ((frozenset({key}), None) if same_query else (frozenset(), "source-key-invalid")), (
        "distinct queries must not collapse into one canonical root fact"
    )
    caplog.set_level("INFO", logger="probe_live_source")
    code = probe_live_source.main(["--bundle", str(tmp_path)])
    assert effects == [], "custom SQL must not perform automatic operations"
    if same_query:
        _assert_terminal(tmp_path, code, "OPERATOR_REQUIRED", effects)
        assert _audit(tmp_path)[-1]["sources"] == [key]
        assert gate.status(tmp_path) != 0
    else:
        assert code == 3 and "CANNOT_ESTABLISH" in caplog.text
        assert not (tmp_path / gate.AUDIT).exists()
        assert not (tmp_path / gate.MARKER).exists()
        assert not (tmp_path / gate.OVERRIDE).exists()
        assert "PROBE: OPERATOR_REQUIRED" not in caplog.text and TOKEN not in caplog.text
    entries = _audit(tmp_path) if (tmp_path / gate.AUDIT).exists() else []
    assert SENTINEL not in caplog.text + json.dumps(entries)
    assert (report.read_bytes(), handover.read_bytes()) == before


@pytest.mark.parametrize("shape", ["spec", "engine"])
def test_loader_preserves_occurrences_metadata_and_ordinary_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """Opaque custom occurrences must not collapse after erasing SQL from a dedupe key."""
    ordinary = {"name": "ordinary", "connection": dict(SQLSERVER), "tables": [{"name": "REAL_TABLE"}]}
    custom = _custom({**SQLSERVER, "instance": "REPORTING", "session": {"principal": "fixture"}})
    custom["occurrence"] = {"workbook": "fixture", "source_id": "source-42", "lineage": [3, 7]}
    sources = [ordinary, custom, _custom(custom["connection"], SIDE_EFFECT_SQL), ordinary, custom]
    if shape == "spec":
        path = _spec(tmp_path, sources)
    else:
        path = tmp_path
        (path / "report.json").write_text(json.dumps({"nested": {"data_sources": sources}}), encoding="utf-8")
    loaded = _protect_loaded_sql(monkeypatch)
    bundle = probe_live_source.load_bundle(path)
    expected = [loaded[index] for index in (0, 1, 2, 4)]
    assert len(bundle.data_sources) == 4, "only ordinary duplicates may be coalesced"
    assert all(actual is source for actual, source in zip(bundle.data_sources, expected, strict=True))
    assert bundle.migration_dir == tmp_path
    assert bundle.data_sources[1]["occurrence"] == custom["occurrence"]
    assert bundle.data_sources[1]["connection"] == custom["connection"]


def test_custom_payload_is_not_read_and_direct_table_entry_is_also_closed(
    tmp_path: Path, effects: list[tuple[str, object]]
) -> None:
    """Both public resolution and the lower-level table entry honor the relation discriminator."""
    source = _custom(SQLSERVER)
    key = _persist_source(tmp_path, source)
    source["tables"] = [SqlReadTrap(source["tables"][0])]
    assert probe_live_source._probe_one(tmp_path, [source], 0, 7, False) == (1, "OPERATOR_REQUIRED")
    assert probe_live_source._probe_one_table(
        tmp_path, key, source["connection"], (source["tables"][0], "ProbeOK"), (7, False)
    ) == (1, "OPERATOR_REQUIRED")
    assert effects == [], "custom SQL must not perform automatic operations"


def test_parser_self_closing_text_relation_cannot_clear_as_a_physical_table(
    tmp_path: Path, effects: list[tuple[str, object]]
) -> None:
    """A missing SQL payload stays custom-only; it must not become physical-table navigation."""
    twb = tmp_path / "empty-custom-sql.twb"
    twb.write_text(
        '<workbook version="2024.1"><datasources><datasource name="ds.test" caption="Test">'
        '<connection class="snowflake" server="x" warehouse="WH">'
        '<relation type="text" name="Q" /></connection></datasource></datasources></workbook>',
        encoding="utf-8",
    )
    source = parse_workbook(twb)["data_sources"][0]
    assert source["tables"][0]["source_relation"] == "custom-sql"
    assert source["tables"][0]["custom_sql"] is None
    _persist_source(tmp_path, source)
    assert probe_live_source._probe_one(tmp_path, [source], 0, 1, False) == (1, "OPERATOR_REQUIRED")
    assert effects == [], "custom SQL must not perform automatic operations"


@pytest.mark.parametrize("conn", CONNECTIONS)
@pytest.mark.parametrize("same_source", [True, False], ids=["same-leg", "earlier-source"])
@pytest.mark.parametrize("sql", [EXPENSIVE_SQL, "", None])
def test_same_scope_ordinary_data_ok_reuses_only_connection_evidence(
    tmp_path: Path, effects: list[tuple[str, object]], conn: dict, same_source: bool, sql: str | None
) -> None:
    """Drive actual ordinary M/PBIP/child-verdict code, not a stubbed DATA_OK probe result."""
    ordinary = {"connection": dict(conn), "tables": [{"name": "REAL_TABLE"}]}
    custom = _custom(conn, sql)
    sources = [ordinary, custom]
    if same_source:
        ordinary["tables"] = custom["tables"] + ordinary["tables"]
        sources = [ordinary]
    code = probe_live_source.main(["--spec", str(_spec(tmp_path, sources))])
    _assert_terminal(tmp_path, code, TOKEN, effects)
    assert sum(kind == "desktop" for kind, _value in effects) == 1
    assert sum(kind == "refresh" for kind, _value in effects) == 1
    assert [value for kind, value in effects if kind == "m"] == ["REAL_TABLE"]
    query = next(value for kind, value in effects if kind == "pbip")
    assert "Table.ColumnNames(tbl)" in query and "Value.NativeQuery" not in query
    assert SENTINEL not in repr(effects)
    assert [entry["action"] for entry in _audit(tmp_path) if entry["action"].startswith("probe-")] == [
        "probe-data_ok",
        "probe-error",
    ]
    assert not any(kind == "customer-sql-executed" for kind, _value in effects)


@pytest.mark.parametrize("reuse", [False, True], ids=["operator-required", "connection-only"])
def test_current_human_permission_stays_unvalidated_without_query_execution(
    tmp_path: Path, effects: list[tuple[str, object]], caplog: pytest.LogCaptureFixture, reuse: bool
) -> None:
    """Permission is established by current writers, not inferred from a nonfinal measurement."""
    sources = [_custom(SQLSERVER)]
    if reuse:
        sources.insert(0, {"connection": dict(SQLSERVER), "tables": [{"name": "REAL_TABLE"}]})
    spec = _spec(tmp_path, sources)
    keys, refusal = gate._gate_root_live_keys(tmp_path)
    assert refusal is None
    assert gate.apply_block(tmp_path, sorted(keys)) == 0
    assert gate.authorize(tmp_path, "Fixture Human") == 0
    assert _assess_sources(tmp_path, sources, authorized=True).state == "authorized_model_only"
    before = _audit(tmp_path)
    permission = (tmp_path / gate.OVERRIDE).read_bytes()
    code = probe_live_source.main(["--spec", str(spec)])
    assert code == 1
    assert sum(value for kind, value in effects if kind == "customer-sql-executed") == 0, (
        "the original slow query must never execute"
    )
    assert sum(kind == "refresh" for kind, _value in effects) == int(reuse)
    assert [value for kind, value in effects if kind == "m"] == (["REAL_TABLE"] if reuse else [])
    if not reuse:
        assert effects == [], "custom SQL must not perform automatic operations"
    rows = _audit(tmp_path)
    assert rows[: len(before)] == before
    assert not any(row["action"] in ("authorize", "probe-cleared") for row in rows[len(before) :])
    assert rows[-1]["action"] == "probe-error"
    assert rows[-1]["detail"].startswith((TOKEN if reuse else "OPERATOR_REQUIRED") + ":")
    assert (tmp_path / gate.OVERRIDE).read_bytes() == permission
    assert not (tmp_path / gate.MARKER).exists() and gate.status(tmp_path) == 0
    result = _assess_sources(tmp_path, sources, authorized=True)
    assert (result.state, result.validation, result.max_phase2_claim) == (
        "authorized_model_only",
        "unvalidated",
        "structural_only",
    )
    assert SENTINEL not in caplog.text + json.dumps(rows) + repr(effects)


def test_original_query_execution_mutation_fails_the_execution_counter(
    tmp_path: Path,
    effects: list[tuple[str, object]],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The independent stub's counter detects execution, not a setup/import/generic-exit failure."""
    original = probe_live_source._custom_sql_stop

    def execute(root: Path, key: str) -> tuple[int, str]:
        probe_live_source._write_probe_model(
            root, f'let q = Value.NativeQuery(source, "{EXPENSIVE_SQL}") in q', "Custom", "ProbeOK"
        )
        assert probe_live_source._refresh_and_classify(4242, "Custom", 7, False) == (0, "DATA_OK")
        return original(root, key)

    monkeypatch.setattr(probe_live_source, "_custom_sql_stop", execute)
    with pytest.raises(AssertionError, match="the original slow query must never execute"):
        test_current_human_permission_stays_unvalidated_without_query_execution(tmp_path, effects, caplog, False)
    assert sum(value for kind, value in effects if kind == "customer-sql-executed") == 1


@pytest.mark.parametrize(
    ("conn", "field", "value"),
    [
        (SQLSERVER, "server", "other.example"),
        (SQLSERVER, "server", r"sql.example.com\OTHER_INSTANCE"),
        (SQLSERVER, "port", "1544"),
        (SQLSERVER, "database", "other_database"),
        (DATABRICKS, "http_path", "/sql/1.0/warehouses/other"),
        (DATABRICKS, "database", "other_catalog"),
        (SNOWFLAKE, "warehouse", "OTHER_WH"),
        (SNOWFLAKE, "role", "OTHER_ROLE"),
        (SNOWFLAKE, "database", "OTHER_DB"),
        *[(conn, "schema", "other_schema") for conn in CONNECTIONS],
        *[(conn, "credential_scope", "different-principal") for conn in CONNECTIONS],
        *[(conn, "authentication", {"kind": "different"}) for conn in CONNECTIONS],
        (SQLSERVER, "class", "oracle"),
        (SQLSERVER, "session_options", {"new-option": "different"}),
    ],
)
def test_scope_mismatch_does_not_borrow_an_ordinary_success(
    tmp_path: Path, effects: list[tuple[str, object]], conn: dict, field: str, value: object
) -> None:
    """All declared scope/session fields survive the key, including ones absent from _leg_key."""
    ordinary = {"connection": dict(conn), "tables": [{"name": "REAL_TABLE"}]}
    custom = _custom(conn)
    custom["connection"][field] = value
    code = probe_live_source.main(["--spec", str(_spec(tmp_path, [ordinary, custom]))])
    assert _audit(tmp_path)[-1]["detail"].startswith("OPERATOR_REQUIRED:"), "cross-scope proof must not be reused"
    _assert_terminal(tmp_path, code, "OPERATOR_REQUIRED", effects)
    assert sum(kind == "refresh" for kind, _value in effects) == 1


def test_scope_snapshot_is_immutable_and_reuse_does_not_survive_an_invocation(
    tmp_path: Path, effects: list[tuple[str, object]]
) -> None:
    """A changed session hint or a past audit does not seed today's proof cache."""
    conn = {**SQLSERVER, "session": {"principal": "first"}}
    snapshot = probe_live_source._connection_scope(conn)
    conn["session"]["principal"] = "second"
    assert snapshot != probe_live_source._connection_scope(conn)
    ordinary = {"connection": dict(SQLSERVER), "tables": [{"name": "REAL_TABLE"}]}
    spec = _spec(tmp_path, [ordinary, _custom(SQLSERVER)])
    assert probe_live_source.main(["--spec", str(spec)]) == 1
    effects.clear()
    code = probe_live_source.main(["--spec", str(spec), "--source-index", "1"])
    assert effects == [], "custom SQL must not perform automatic operations"
    _assert_terminal(tmp_path, code, "OPERATOR_REQUIRED", effects)


def test_qualified_physical_schema_cannot_borrow_declared_scope(
    tmp_path: Path, effects: list[tuple[str, object]]
) -> None:
    """#647's real physical schema, not a conflicting declared hint, constrains connection reuse."""
    ordinary = {
        "connection": dict(SQLSERVER),
        "tables": [{"name": "display", "table": "[private_schema].[REAL_TABLE]"}],
    }
    sources = [ordinary, _custom(SQLSERVER)]
    code = probe_live_source.main(["--spec", str(_spec(tmp_path, sources))])
    _assert_terminal(tmp_path, code, "OPERATOR_REQUIRED", effects)
    query = next(value for kind, value in effects if kind == "pbip")
    assert '[Schema="private_schema",Item="REAL_TABLE"]' in query
    assert sum(kind == "refresh" for kind, _value in effects) == 1


@pytest.mark.parametrize("same_source", [True, False], ids=["same-leg", "earlier-source"])
@pytest.mark.parametrize(
    "endpoint",
    [
        {"port": "1544"},
        {"port": 1544},
        {"instance": "REPORTING"},
        {"port": "1544", "instance": "REPORTING"},
        {"port": ""},
        {"port": None},
        {"port": "not-a-port"},
        {"port": -1},
        {"port": 70000},
        {"instance": ""},
        {"instance": None},
        {"instance": 42},
        {"instance": r"OTHER\REPORTING"},
        {"instance": {"name": "REPORTING"}},
    ],
)
def test_unexercised_endpoint_cannot_supply_reuse(
    tmp_path: Path, effects: list[tuple[str, object]], same_source: bool, endpoint: dict
) -> None:
    """Pin the actual ordinary M endpoint, then refuse to reuse its incomplete endpoint proof."""
    conn = {**SQLSERVER, **endpoint}
    ordinary = {"connection": conn, "tables": [{"name": "REAL_TABLE"}]}
    custom = _custom(conn)
    sources = [ordinary, custom]
    if same_source:
        ordinary["tables"].extend(custom["tables"])
        sources = [ordinary]
    code = probe_live_source.main(["--spec", str(_spec(tmp_path, sources))])
    query = next(value for kind, value in effects if kind == "pbip")
    assert query.splitlines()[1] == '    Source = Sql.Database("sql.example.com", "DB"),'
    assert query == probe_live_source.build_m_query(SQLSERVER, "REAL_TABLE", "ProbeOK")[0]
    assert _audit(tmp_path)[-1]["detail"].startswith("OPERATOR_REQUIRED:"), (
        "unexercised endpoint proof must not be reused"
    )
    _assert_terminal(tmp_path, code, "OPERATOR_REQUIRED", effects)
    assert sum(kind == "refresh" for kind, _value in effects) == 1


@pytest.mark.parametrize("port", [False, [1544]], ids=["boolean", "list"])
def test_malformed_scope_cannot_become_ordinary_evidence(
    tmp_path: Path, effects: list[tuple[str, object]], caplog: pytest.LogCaptureFixture, port: object
) -> None:
    """Current canonical input refusal is not a positive ordinary DATA_OK fixture."""
    spec = _spec(tmp_path, [_custom({**SQLSERVER, "port": port})])
    code = probe_live_source.main(["--spec", str(spec)])
    assert code == 3 and effects == []
    assert "CANNOT_ESTABLISH" in caplog.text
    assert not (tmp_path / gate.AUDIT).exists() and not (tmp_path / gate.OVERRIDE).exists()
    assert _assess_sources(tmp_path, [_custom({**SQLSERVER, "port": port})]).state == "cannot_establish"


@pytest.mark.parametrize("server", ["sql.example.com", r"sql.example.com\REPORTING", "sql.example.com,1544"])
def test_server_endpoint_is_exercised_before_reuse(
    tmp_path: Path, effects: list[tuple[str, object]], server: str
) -> None:
    """Existing server-string connector semantics remain unchanged; no endpoint syntax is invented."""
    conn = {**SQLSERVER, "server": server}
    ordinary = {"connection": conn, "tables": [{"name": "REAL_TABLE"}]}
    code = probe_live_source.main(["--spec", str(_spec(tmp_path, [ordinary, _custom(conn)]))])
    query = next(value for kind, value in effects if kind == "pbip")
    assert query.splitlines()[1] == f'    Source = Sql.Database("{server}", "DB"),'
    _assert_terminal(tmp_path, code, TOKEN, effects)


def test_shell_success_and_no_popup_do_not_supply_connection_evidence(
    tmp_path: Path, effects: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no shell proof import, socket test, popup watcher, or timeout on this path."""
    monkeypatch.setenv("SQLCMD_SUCCESS", "1")
    monkeypatch.setenv("ODBC_SUCCESS", "1")
    code = probe_live_source.main(["--spec", str(_spec(tmp_path, [_custom(SQLSERVER)]))])
    assert effects == [], "custom SQL must not perform automatic operations"
    _assert_terminal(tmp_path, code, "OPERATOR_REQUIRED", effects)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("invalid object name", "OPERATOR_REQUIRED"),
        ("REFRESH: CREDENTIAL_MISSING", "NO_CREDENTIAL"),
        ("permission denied", "ACCESS_DENIED"),
        ("REFRESH: NO_DATA", "ERROR"),
        ("REFRESH: TIMEOUT", "ERROR"),
    ],
)
def test_ordinary_failures_never_seed_custom_reuse(
    tmp_path: Path,
    effects: list[tuple[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    detail: str,
    expected: str,
) -> None:
    """Only an actual DATA_OK earns reuse; BAD_TABLE may fall through to a no-operation stop."""
    source = _custom(SQLSERVER)
    source["tables"].insert(0, {"name": "REAL_TABLE"})
    _persist_source(tmp_path, source)
    monkeypatch.setattr(
        probe_live_source,
        "_run_refresh",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, stdout=detail, stderr=""),
    )
    assert probe_live_source._probe_one(tmp_path, [source], 0, 7, False) == (1, expected)
    assert [value for kind, value in effects if kind == "m"] == ["REAL_TABLE"]
    assert sum(kind == "desktop" for kind, _value in effects) == 1
    assert not any(entry["detail"].startswith(TOKEN + ":") for entry in _audit(tmp_path))


def test_no_exact_mode_is_implemented(tmp_path: Path, effects: list[tuple[str, object]]) -> None:
    """#692 exact execution cannot be reached accidentally through a new flag."""
    with pytest.raises(SystemExit) as raised:
        probe_live_source.main(["--spec", str(_spec(tmp_path, [_custom(SQLSERVER)])), "--custom-sql-mode", "exact"])
    assert raised.value.code == 2 and effects == []


@pytest.mark.parametrize("payload", ["object", "huge"])
def test_operator_required_is_a_real_nonzero_process_exit(tmp_path: Path, payload: str) -> None:
    """Run the real CLI main in a child with external-effect spies, never a live Desktop."""
    sql = {"nested": [SIDE_EFFECT_SQL, {"query": EXPENSIVE_SQL}]} if payload == "object" else SENTINEL * 100_000
    spec = _spec(tmp_path, [_custom(SQLSERVER, sql)])
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(sys.argv[1]) / 'tests'))\n"
        "import pytest\n"
        "from test_probe_live_custom_sql import effects_fixture, probe_live_source, _protect_loaded_sql\n"
        "with pytest.MonkeyPatch.context() as patch:\n"
        "    import credential_gate as gate\n"
        "    patch.setattr(gate.platform, 'system', lambda: 'Linux')\n"
        "    events = effects_fixture.__wrapped__(patch)\n"
        "    _protect_loaded_sql(patch, memory_error=sys.argv[3] == 'huge')\n"
        "    code = probe_live_source.main(['--spec', sys.argv[2]])\n"
        "    assert not events, 'custom SQL must not perform automatic operations'\n"
        "sys.exit(code)\n"
    )
    child = subprocess.run(
        [sys.executable, "-c", script, str(REPO), str(spec), payload],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert child.returncode == 1
    assert "PROBE: OPERATOR_REQUIRED" in child.stderr
    assert "No connection claim was earned." in child.stderr
    assert "Traceback" not in child.stderr and SENTINEL not in child.stderr + child.stdout
    _assert_terminal(tmp_path, child.returncode, "OPERATOR_REQUIRED", [])


def test_customer_messages_match_the_two_evidence_states(caplog: pytest.LogCaptureFixture) -> None:
    """No connection assertion is permitted on the unprobed path."""
    probe_live_source._print_verdict_directive(TOKEN)
    assert [record.getMessage() for record in caplog.records] == [
        "Power BI reached this same connection scope through an ordinary table in this probe. "
        "Your custom SQL was not executed and remains unvalidated. Without existing exact-key human "
        "build-only permission, the gate stays armed; this result grants no permission."
    ], "connection-only customer LogRecord must equal the approved message"
    caplog.clear()
    probe_live_source._print_verdict_directive("OPERATOR_REQUIRED")
    assert [record.getMessage() for record in caplog.records] == [
        "Your custom SQL was not executed. No safe automated connection-only operation is currently "
        "available without catalog enumeration or a native-query approval prompt. "
        "No connection claim was earned. Without existing exact-key human build-only permission, "
        "the gate stays armed; this result grants no permission."
    ], "operator-required customer LogRecord must equal the approved message"
    assert "Power BI reached" not in caplog.text and "SOURCE UNREACHABLE" not in caplog.text


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (
            "NO_CREDENTIAL",
            "\n"
            "################################################################\n"
            "#  STOP - NO POWER BI CREDENTIAL. A HUMAN MUST ACT.\n"
            "################################################################\n"
            "  The source is REACHABLE but Power BI has never authenticated to it.\n"
            "\n"
            "  DIRECTIVE - NOT ADVISORY, NOT A WARNING:\n"
            "    1. You may NOT build the semantic model or the report. The gate stays armed.\n"
            "    2. You CANNOT fix this yourself. There is no flag, retry, tool or workaround.\n"
            "       The credential lives behind a modal sign-in dialog that only a HUMAN sitting\n"
            "       at this machine can fill in. Retrying cannot conjure one.\n"
            "    3. Report it and TERMINATE THE RUN NOW - if your runtime has a 'blocked' or\n"
            "       task-complete exit, use it. Do NOT merely pause: measured 2026-08-01, three\n"
            "       runs announced this stop correctly and then talked themselves past it a few\n"
            "       turns later ('applying the non-interactive fallback'). A stop that does not\n"
            "       terminate gets re-prompted until it erodes. Stopping IS your completed task\n"
            "       here; report it as such, not as an unfinished one.\n"
            "\n"
            "  A non-interactive / autopilot / unattended session is NOT authorization. Having\n"
            "  nobody to answer is not permission to proceed - end the run with the question\n"
            "  unanswered. That is the correct, successful outcome here.\n"
            "\n"
            "  Tell the user to sign in interactively in Power BI Desktop (or supply a PAT/key),\n"
            "  then re-run. Name the server and warehouse in your message.\n"
            "################################################################",
        ),
        (
            "ACCESS_DENIED",
            "\n"
            "################################################################\n"
            "#  STOP - ACCESS DENIED. A PERMISSION OWNER MUST ACT.\n"
            "################################################################\n"
            "  Power BI reached the source, but the authenticated identity is not allowed to\n"
            "  read the requested object. This is final until permissions change.\n"
            "\n"
            "    1. You may NOT build the semantic model or the report. The gate stays armed.\n"
            "    2. Do NOT retry unchanged, and do NOT send the user to fix a hostname.\n"
            "    3. Ask the source owner to grant the Power BI identity access to the server,\n"
            "       database/schema, warehouse, or table named in the probe output.\n"
            "################################################################",
        ),
    ],
    ids=["no-credential", "access-denied"],
)
def test_ordinary_directives_remain_base_exact(caplog: pytest.LogCaptureFixture, verdict: str, expected: str) -> None:
    """#690 cannot silently change ordinary credential/permission guidance."""
    probe_live_source._print_verdict_directive(verdict)
    assert [record.getMessage() for record in caplog.records] == [expected], (
        "ordinary customer directive must remain base-identical"
    )


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (
            Path("docs") / "credential-gate.md",
            "| `NO_CREDENTIAL` | Positive authentication evidence: Power BI has no credential, "
            "or the one it has was rejected. | STOP; ask a human to sign in. No retry conjures a credential. |",
        ),
        (
            Path(".github") / "skills" / "live-source-reachability" / "SKILL.md",
            "| `NO_CREDENTIAL` | Power BI lacks or rejects a credential. | Hard stop after one attempt; "
            "ask for Desktop sign-in or human build-only authorization. |",
        ),
    ],
    ids=["docs", "skill"],
)
def test_ordinary_credential_docs_remain_base_exact(relative: Path, expected: str) -> None:
    """Ordinary wording revisions need their own issue rather than this custom-SQL safety slice."""
    rows = [
        line
        for line in (REPO / relative).read_text(encoding="utf-8").splitlines()
        if line.startswith("| `NO_CREDENTIAL`")
    ]
    assert rows == [expected], "ordinary credential documentation must remain base-identical"


@pytest.mark.parametrize("reuse", [False, True], ids=["operator-required", "connection-only"])
def test_both_custom_outcomes_keep_the_real_audit_and_gate_armed(
    tmp_path: Path, effects: list[tuple[str, object]], reuse: bool
) -> None:
    """The unchanged gate verifies unvalidated artifacts remain blocked for either verdict."""
    sources = [_custom(SQLSERVER)]
    if reuse:
        sources.insert(0, {"connection": dict(SQLSERVER), "tables": [{"name": "REAL_TABLE"}]})
    spec = _spec(tmp_path, sources)
    artifact = tmp_path / "fabric" / "unfinished.pbip"
    artifact.parent.mkdir()
    artifact.write_text("{}", encoding="utf-8")
    keys, refusal = gate._gate_root_live_keys(tmp_path)
    assert refusal is None and gate.apply_block(tmp_path, sorted(keys)) == 0
    assert gate.status(tmp_path) == 1
    code = probe_live_source.main(["--spec", str(spec)])
    _assert_terminal(tmp_path, code, TOKEN if reuse else "OPERATOR_REQUIRED", effects)
    assert set(json.loads((tmp_path / gate.MARKER).read_text(encoding="utf-8"))["sources"]) == keys
    with gate.gate_transaction(tmp_path):
        assert set(gate._transaction_pending(tmp_path)) == keys
    assert gate.status(tmp_path) == 1 and gate.verify(tmp_path) == 1
    assert not (tmp_path / gate.OVERRIDE).exists()
    probe_live_source._record_attempt(
        tmp_path, "OPERATOR_REQUIRED", "other existing producer", [preflight._leg_key(sources[-1], 0, SQLSERVER)]
    )
    assert _audit(tmp_path)[-1]["action"] == "probe-operator_required", "other operator producers must remain unchanged"


@pytest.mark.parametrize(
    ("conn", "navigation"),
    [
        (
            SNOWFLAKE,
            '    Source = Snowflake.Databases("ORG-ACCOUNT.snowflakecomputing.com", "WH", null),\n'
            '    db = Source{[Name="DB",Kind="Database"]}[Data],\n'
            '    sch = db{[Name="PUBLIC",Kind="Schema"]}[Data],\n'
            '    tbl = sch{[Name="FLIGHTS",Kind="Table"]}[Data],\n',
        ),
        (
            DATABRICKS,
            '    Source = Databricks.Catalogs("adb.example.azuredatabricks.net", "/sql/1.0/warehouses/abc", null),\n'
            '    db = Source{[Name="hive_metastore",Kind="Database"]}[Data],\n'
            '    sch = db{[Name="default",Kind="Schema"]}[Data],\n'
            '    tbl = sch{[Name="FLIGHTS",Kind="Table"]}[Data],\n',
        ),
        *[
            (
                {**SQLSERVER, "class": klass, "schema": "dbo"},
                '    Source = Sql.Database("sql.example.com", "DB"),\n'
                '    tbl = Source{[Schema="dbo",Item="FLIGHTS"]}[Data],\n',
            )
            for klass in ("sqlserver", "azure_sqldb", "azure_sql_dw", "azuresqldw")
        ],
    ],
    ids=["snowflake", "databricks", "sqlserver", "azure_sqldb", "azure_sql_dw", "azuresqldw"],
)
def test_ordinary_table_selects_a_runtime_column_without_changing_navigation(conn: dict, navigation: str) -> None:
    """No Tableau field token may become a guessed physical column identifier."""
    internal_name = "[Tableau_Local_Column]"
    m, note = probe_live_source.build_m_query(conn, "FLIGHTS", internal_name.strip("[]"))
    assert internal_name.strip("[]") not in m
    assert m.startswith("let\n" + navigation + "    columns = Table.ColumnNames(tbl),\n")
    assert "Table.FirstN(Table.SelectColumns(tbl, {columns{0}}), 1)" in m
    assert "Table.RenameColumns(\n" in m and '{{columns{0}, "ProbeOK"}})' in m
    assert m.endswith("in\n    probe")
    assert "Value.NativeQuery" not in m and "custom SQL" not in note
    assert "try " not in m


@pytest.mark.parametrize("conn", CONNECTIONS, ids=["sqlserver", "databricks", "snowflake"])
def test_ordinary_probe_projection_cannot_manufacture_rows_without_columns(conn: dict) -> None:
    """Keep the original physical-column and zero-row behavior byte-for-byte."""
    m, _ = probe_live_source.build_m_query(conn, "FLIGHTS", "Tableau_Local_Column")
    assert m[m.index("    columns =") :] == (
        "    columns = Table.ColumnNames(tbl),\n"
        '    probe = if List.IsEmpty(columns) then #table({"ProbeOK"}, {}) else\n'
        "        Table.RenameColumns(\n"
        "            Table.FirstN(Table.SelectColumns(tbl, {columns{0}}), 1),\n"
        '            {{columns{0}, "ProbeOK"}})\n'
        "in\n"
        "    probe"
    )


def test_real_table_probe_still_opens_refreshes_and_returns_data_ok(
    tmp_path: Path, effects: list[tuple[str, object]]
) -> None:
    """Exercise ordinary M, PBIP and real child-verdict parsing with only external I/O stubbed."""
    source = {"connection": dict(SNOWFLAKE), "tables": [{"name": "FLIGHTS"}]}
    _persist_source(tmp_path, source)
    assert probe_live_source._probe_one(tmp_path, [source], 0, 7, False) == (0, "DATA_OK")
    assert [kind for kind, _value in effects] == [
        "dns",
        "m",
        "pbip",
        "desktop",
        "catalog",
        "network",
        "refresh",
        "close",
    ]
    assert _audit(tmp_path)[-1]["action"] == "probe-data_ok"


@pytest.mark.parametrize("conn", CONNECTIONS)
@pytest.mark.parametrize("fields", [None, [], [{"kind": "column"}], [{"kind": "calculated", "internal_name": "[X]"}]])
def test_ordinary_source_without_enumerated_columns_is_resolvable(conn: dict, fields: list[dict] | None) -> None:
    """Source column discovery remains independent of optional Tableau field enumeration."""
    source = _source([{"name": "REAL_TABLE", "custom_sql": None}], fields=[])
    source["connection"] = conn
    if fields is None:
        del source["fields"]
    else:
        source["fields"] = fields
    _, tables, column = probe_live_source._resolve_probe_target([source], 0)
    assert [table["name"] for table in tables] == ["REAL_TABLE"]
    assert column == "ProbeOK"


def test_ordinary_target_ignores_tableau_internal_field_names() -> None:
    """Even present Tableau fields do not provide a physical column identifier."""
    _, _, column = probe_live_source._resolve_probe_target([_source([{"name": "REAL_TABLE"}])], 0)
    assert column == "ProbeOK"


@pytest.mark.parametrize("tables", [[], [{}], [{"name": ""}], [{"custom_sql": "SELECT 1"}]])
def test_source_without_a_named_table_or_custom_sql_relation_is_refused(tables: list[dict]) -> None:
    """The unchanged source-identity admission still refuses missing table names."""
    with pytest.raises(SystemExit) as raised:
        probe_live_source._resolve_probe_target([_source(tables)], 0)
    assert raised.value.code == 1


@pytest.mark.parametrize(
    "text",
    [
        "[Expression.Error] The key didn't match any rows in the table.",
        "The key didn\u2019t match any rows in the table.",
    ],
)
def test_a_navigation_key_miss_classifies_as_bad_table_not_unclassified_error(text: str) -> None:
    """Ordinary navigation errors retain their current distinct verdict."""
    assert probe_live_source._classify_failure(text, False)[0] == "BAD_TABLE"


def test_real_tables_are_ordered_before_custom_sql_without_reading_the_query() -> None:
    """Resolution preserves the custom relation and raw bytes but schedules ordinary evidence first."""
    source = _custom(SNOWFLAKE)
    source["tables"].append({"name": "REAL_TABLE", "table": "[PUBLIC].[REAL_TABLE]"})
    _, tables, column = probe_live_source._resolve_probe_target([source], 0)
    assert [table["name"] for table in tables] == ["REAL_TABLE", "Q"]
    assert tables[-1]["custom_sql"] == EXPENSIVE_SQL and column == "ProbeOK"


@pytest.mark.parametrize(
    "mutation",
    [
        (
            "_custom_sql_stop",
            'return 1, "OPERATOR_REQUIRED"',
            'return 0, "OPERATOR_REQUIRED"',
            "custom-SQL verdicts must exit 1",
        ),
        (
            "run_probe",
            "if connection_only:\n        _print_verdict_directive(CONNECTION_ONLY)\n        return 1",
            "if connection_only:\n        _print_verdict_directive(CONNECTION_ONLY)\n        return 0",
            "custom-SQL verdicts must exit 1",
        ),
        (
            "_record_attempt",
            "record_probe_attempt(migration, verdict, what, sources=list(sources))",
            'record_probe_attempt(migration, "DATA_OK", what, sources=list(sources))\n'
            "    from credential_gate import clear_block\n"
            '    assert clear_block(migration, "probe-cleared: mutation-injected DATA_OK", '
            "earned=True, sources=list(sources)) == 0",
            "custom SQL must not earn clearance or authorization",
        ),
        (
            "_custom_sql_stop",
            '"ERROR",',
            '"OPERATOR_REQUIRED",',
            "custom outcomes must use the keyed probe-error envelope",
        ),
    ],
    ids=["operator-exit-zero", "reuse-exit-zero", "probe-cleared", "retained-operator-audit"],
)
def test_verdict_mutations_fail_the_intended_assertion(
    tmp_path: Path,
    effects: list[tuple[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    mutation: tuple[str, str, str, str],
) -> None:
    """Compile mutations outside the assertion catch; nonzero/import failures are not kills."""
    function, old, new, message = mutation
    original = inspect.getsource(getattr(probe_live_source, function))
    assert original.count(old) == 1
    namespace = dict(vars(probe_live_source))
    exec(compile(original.replace(old, new), "<issue690-mutation>", "exec"), namespace)  # pylint: disable=exec-used
    monkeypatch.setattr(probe_live_source, function, namespace[function])
    if function == "_custom_sql_stop":
        key = _persist_source(tmp_path, _custom(SQLSERVER))
        code, _token = probe_live_source._custom_sql_stop(tmp_path, key)
        with pytest.raises(AssertionError, match=message):
            _assert_terminal(tmp_path, code, "OPERATOR_REQUIRED", effects)
    else:
        sources = [{"connection": dict(SQLSERVER), "tables": [{"name": "REAL_TABLE"}]}, _custom(SQLSERVER)]
        code = probe_live_source.main(["--spec", str(_spec(tmp_path, sources))])
        with pytest.raises(AssertionError, match=message):
            _assert_terminal(tmp_path, code, TOKEN, effects)


@pytest.mark.parametrize(
    "query",
    [
        f'let q = Value.NativeQuery(source, "{EXPENSIVE_SQL}") in q',
        'let q = Table.Buffer(Sql.Database("fixture.example", "db")) in q',
        'let q = Sql.Database("fixture.example", "db", [Query="SELECT 1"]) in q',
        'let q = Value.NativeQuery(source, "SELECT 1") in q',
    ],
    ids=["customer-sql", "navigation", "query-constant", "native-constant"],
)
def test_reintroduced_custom_operations_are_detected(
    tmp_path: Path, effects: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """Any attempt to scaffold a custom connection operation must fail the no-operation oracle."""
    original = probe_live_source._custom_sql_stop

    def mutated(root: Path, key: str) -> tuple[int, str]:
        probe_live_source._write_probe_model(root, query, "Custom", "ProbeOK")
        return original(root, key)

    monkeypatch.setattr(probe_live_source, "_custom_sql_stop", mutated)
    probe_live_source.main(["--spec", str(_spec(tmp_path, [_custom(SQLSERVER)]))])
    with pytest.raises(AssertionError, match="custom SQL must not perform automatic operations"):
        assert effects == [], "custom SQL must not perform automatic operations"


def test_cross_scope_reuse_mutation_is_detected(
    tmp_path: Path, effects: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-only key must fail the real public two-source reuse decision."""
    monkeypatch.setattr(probe_live_source, "_connection_scope", lambda conn: conn.get("server"))
    with pytest.raises(AssertionError, match="cross-scope proof must not be reused"):
        test_scope_mismatch_does_not_borrow_an_ordinary_success(tmp_path, effects, SQLSERVER, "database", "other")
