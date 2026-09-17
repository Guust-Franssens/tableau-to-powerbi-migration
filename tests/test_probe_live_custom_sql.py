"""Custom SQL never starts a default probe; ordinary row probing retains its existing contract.

Only same-invocation, same-scope ordinary DATA_OK may supply connection evidence. All other
custom-only sources stop without M generation, sockets, a PBIP, Desktop, or a refresh.
"""

# pylint: disable=protected-access,wrong-import-position

from __future__ import annotations

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


def _custom(conn: dict, sql: str | None = EXPENSIVE_SQL) -> dict:
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
    expected = "probe-error" if token == TOKEN else "probe-operator_required"
    assert entries[-1]["action"] == expected
    assert entries[-1]["sources"] and all(key.startswith("source-key:") for key in entries[-1]["sources"])
    if token == TOKEN:
        assert entries[-1]["detail"].startswith(TOKEN + ":")
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
    assert _audit(tmp_path)[-1]["action"] == "probe-operator_required", "cross-scope proof must not be reused"
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


def test_operator_required_is_a_real_nonzero_process_exit(tmp_path: Path) -> None:
    """Run the real CLI main in a child with external-effect spies, never a live Desktop."""
    spec = _spec(tmp_path, [_custom(SQLSERVER)])
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(sys.argv[1]) / 'tests'))\n"
        "import pytest\n"
        "from test_probe_live_custom_sql import effects_fixture, probe_live_source\n"
        "with pytest.MonkeyPatch.context() as patch:\n"
        "    events = effects_fixture.__wrapped__(patch)\n"
        "    code = probe_live_source.main(['--spec', sys.argv[2]])\n"
        "    assert not events, 'custom SQL must not perform automatic operations'\n"
        "sys.exit(code)\n"
    )
    child = subprocess.run(
        [sys.executable, "-c", script, str(REPO), str(spec)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert child.returncode == 1
    assert "PROBE: OPERATOR_REQUIRED" in child.stderr
    assert "No connection claim was earned; the gate remains armed." in child.stderr
    assert "Traceback" not in child.stderr and SENTINEL not in child.stderr + child.stdout


def test_customer_messages_match_the_two_evidence_states(caplog: pytest.LogCaptureFixture) -> None:
    """No connection assertion is permitted on the unprobed path."""
    probe_live_source._print_verdict_directive(TOKEN)
    assert (
        "Power BI reached this same connection scope through an ordinary table in this probe. "
        "Your custom SQL was not executed and remains unvalidated; the gate is still armed."
    ) in caplog.text
    caplog.clear()
    probe_live_source._print_verdict_directive("OPERATOR_REQUIRED")
    assert (
        "Your custom SQL was not executed. No safe automated connection-only operation is currently "
        "available without catalog enumeration or a native-query approval prompt. "
        "No connection claim was earned; the gate remains armed."
    ) in caplog.text
    assert "Power BI reached" not in caplog.text and "SOURCE UNREACHABLE" not in caplog.text


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
    ],
    ids=["operator-exit-zero", "reuse-exit-zero", "probe-cleared"],
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
