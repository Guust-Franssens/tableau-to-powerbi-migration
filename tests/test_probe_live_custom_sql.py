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
    "powerbi_target": "live_source",
}
UNSUPPORTED = {"class": "oracle", "server": "oracle.example", "database": "DB", "powerbi_target": "live_source"}
CONNECTIONS = (SQLSERVER, DATABRICKS, SNOWFLAKE)
TOKEN = "CONNECTION_OK_QUERY_UNVALIDATED"
SENTINEL = "CUSTOM_SQL_MUST_NOT_RUN_690"
EXPENSIVE_SQL = f"SELECT dbo.{SENTINEL}(f.payload) FROM huge_fact f CROSS JOIN huge_fact g"
SIDE_EFFECT_SQL = f"EXEC dbo.{SENTINEL}; DROP TABLE private_fixture;"


def _source(tables: list[dict], fields: list[dict] | None = None) -> dict:
    for table in tables:
        if table.get("custom_sql") is not None:
            table["source_relation"] = "custom-sql"
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
    path = root / "migration-spec.json"
    path.write_text(json.dumps({"data_sources": sources}), encoding="utf-8")
    return path


def _audit(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / gate.AUDIT).read_text(encoding="utf-8").splitlines()]


@pytest.fixture(name="effects")
def effects_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Observe every operation while replacing only actual Desktop/network effects."""
    events: list[tuple[str, object]] = []
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
        return original_write(root, query, table, column)

    def open_desktop(pbip: Path) -> int:
        events.append(("desktop", str(pbip)))
        return 4242

    def refresh(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        events.append(("refresh", argv))
        assert Path(argv[1]) == probe_live_source.SKILL_SCRIPTS / "refresh_pbip_model.py"
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
    monkeypatch.setattr(probe_live_source.subprocess, "run", refresh)
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
    assert not (tmp_path / "_probe").exists()
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
    """Instrument the JSON-read boundary, not a replacement bundle loader or probe result."""
    original_load, original_dumps, original_hash = json.load, json.dumps, hashlib.sha256
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
        assert SENTINEL.encode() not in value, "the default path hashed custom SQL"
        return original_hash(value, **kwargs)

    monkeypatch.setattr(json, "load", load)
    monkeypatch.setattr(json, "dumps", dumps)
    monkeypatch.setattr(hashlib, "sha256", sha256)
    return loaded_sources


@pytest.mark.parametrize(
    "case",
    [
        (shape, composition, payload)
        for shape in ("spec", "engine", "engine-direct")
        for composition in ("custom-only", "mixed-leg", "earlier-source")
        for payload in ("object", "list", "string", "huge")
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
    """Nested/non-string/large SQL cannot abort CLI loading before a keyed terminal observation."""
    shape, composition, payload = case
    custom = _custom(
        SQLSERVER,
        {
            "object": {"data_sources": [_custom(UNSUPPORTED)], "binding_signal": {"published_ds_name": SENTINEL}},
            "list": [{"nested": [SIDE_EFFECT_SQL, {"custom_sql": EXPENSIVE_SQL}]}],
            "string": SIDE_EFFECT_SQL,
            "huge": SENTINEL + ("x" * 2_000_000),
        }[payload],
    )
    sources = [custom]
    if composition == "mixed-leg":
        custom["tables"].insert(0, {"name": "REAL_TABLE"})
    elif composition == "earlier-source":
        sources.insert(0, {"connection": dict(SQLSERVER), "tables": [{"name": "REAL_TABLE"}]})
    if shape == "spec":
        path = _spec(tmp_path, sources)
        args = ["--spec", str(path)]
    else:
        path = tmp_path / "report.json"
        path.write_text(
            json.dumps({"nested": sources if shape == "engine-direct" else {"data_sources": sources}}),
            encoding="utf-8",
        )
        args = ["--bundle", str(tmp_path)]
    original_bytes = path.read_bytes()
    _protect_loaded_sql(monkeypatch, memory_error=payload == "huge")
    caplog.set_level("INFO", logger="probe_live_source")
    try:
        code = probe_live_source.main(args)
    except MemoryError:
        pytest.fail("custom payload processing prevented a keyed verdict/audit")
    token = "OPERATOR_REQUIRED" if composition == "custom-only" else TOKEN
    _assert_terminal(tmp_path, code, token, effects)
    assert _audit(tmp_path)[-1]["sources"] == [probe_live_source._leg_key({}, 0, SQLSERVER)]
    assert [value for kind, value in effects if kind == "m"] == ([] if composition == "custom-only" else ["REAL_TABLE"])
    if composition == "custom-only":
        assert effects == [], "custom SQL must not perform automatic operations"
        assert not (tmp_path / "_probe").exists()
    assert path.read_bytes() == original_bytes
    assert SENTINEL not in caplog.text + (tmp_path / gate.AUDIT).read_text(encoding="utf-8") + repr(effects)


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
    source["tables"] = [SqlReadTrap(source["tables"][0])]
    assert probe_live_source._probe_one(tmp_path, [source], 0, 7, False) == (1, "OPERATOR_REQUIRED")
    key = probe_live_source._leg_key(source, 0, source["connection"])
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
    assert [entry["action"] for entry in _audit(tmp_path)] == ["probe-data_ok", "probe-error"]


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
        {"port": False},
        {"port": [1544]},
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
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
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
    assert "No connection claim was earned; the gate remains armed." in child.stderr
    assert "Traceback" not in child.stderr and SENTINEL not in child.stderr + child.stdout
    _assert_terminal(tmp_path, child.returncode, "OPERATOR_REQUIRED", [])


def test_customer_messages_match_the_two_evidence_states(caplog: pytest.LogCaptureFixture) -> None:
    """No connection assertion is permitted on the unprobed path."""
    probe_live_source._print_verdict_directive(TOKEN)
    assert [record.getMessage() for record in caplog.records] == [
        "Power BI reached this same connection scope through an ordinary table in this probe. "
        "Your custom SQL was not executed and remains unvalidated; the gate is still armed."
    ], "connection-only customer LogRecord must equal the approved message"
    caplog.clear()
    probe_live_source._print_verdict_directive("OPERATOR_REQUIRED")
    assert [record.getMessage() for record in caplog.records] == [
        "Your custom SQL was not executed. No safe automated connection-only operation is currently "
        "available without catalog enumeration or a native-query approval prompt. "
        "No connection claim was earned; the gate remains armed."
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
    tmp_path: Path, effects: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, reuse: bool
) -> None:
    """The unchanged gate verifies unvalidated artifacts remain blocked for either verdict."""
    sources = [_custom(SQLSERVER)]
    if reuse:
        sources.insert(0, {"connection": dict(SQLSERVER), "tables": [{"name": "REAL_TABLE"}]})
    spec = _spec(tmp_path, sources)
    artifact = tmp_path / "fabric" / "unfinished.pbip"
    artifact.parent.mkdir()
    artifact.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gate.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gate, "_user", lambda: "fixture")
    monkeypatch.setattr(gate, "_icacls", lambda _args: (0, "fixture ACL"))
    monkeypatch.setattr(gate, "_has_deny_ace", lambda root: (root / gate.MARKER).exists())
    gate.apply_block(tmp_path, [probe_live_source._leg_key({}, 0, SQLSERVER)])
    marker = (tmp_path / gate.MARKER).read_bytes()
    code = probe_live_source.main(["--spec", str(spec)])
    _assert_terminal(tmp_path, code, TOKEN if reuse else "OPERATOR_REQUIRED", effects)
    assert (tmp_path / gate.MARKER).read_bytes() == marker
    assert gate.status(tmp_path) == 1 and gate.verify(tmp_path) == 1
    assert not (tmp_path / gate.OVERRIDE).exists()
    probe_live_source._record_attempt(
        tmp_path, "OPERATOR_REQUIRED", "other existing producer", [probe_live_source._leg_key({}, 0, SQLSERVER)]
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
    source["tables"].append({"name": "REAL_TABLE"})
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
            "_audit(migration, action, what, sources=list(sources))",
            '_audit(migration, "probe-cleared", what, sources=list(sources))',
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
        code, _token = probe_live_source._custom_sql_stop(tmp_path, probe_live_source._leg_key({}, 0, SQLSERVER))
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
