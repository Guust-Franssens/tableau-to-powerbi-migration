"""Offline #690 controls: inspect invoked PBIP/argv, count SQL dispatches, keep the real audit.

The Desktop/connector boundary is a fixture, not a claim of live M/provider qualification.
Expected M dependency paths and child transcripts are independent of the production builders.
"""

# pylint: disable=protected-access,wrong-import-position

from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import credential_gate as gate  # noqa: E402
import probe_live_source as probe  # noqa: E402

TOKEN = "CONNECTION_OK_QUERY_UNVALIDATED"
MESSAGE = (
    "Power BI connected using Desktop credentials. Your custom SQL was not executed and remains "
    "unvalidated; the gate is still armed."
)
SENTINEL = "CUSTOM_SQL_MUST_NEVER_DISPATCH_690"
EXPENSIVE_SQL = (
    "SELECT SUM(dbo.non_folding_costly_function(f.payload))\r\n"
    "FROM huge_fact f CROSS JOIN huge_fact g -- " + SENTINEL + "\r\n"
)
SIDE_EFFECT_SQL = f"EXEC dbo.costly_operation '{SENTINEL}'; DROP TABLE private_output;"
NATIVE = re.compile(r"Value\.NativeQuery|\bQuery\s*=|\bSELECT\s+1\b", re.IGNORECASE)
CONNECTIONS = {
    "sqlserver": {
        "class": "sqlserver",
        "server": "https://sql.example/",
        "port": "1544",
        "database": "ExactDatabase",
        "schema": "dbo",
        "credential_scope": "desktop-fixture",
    },
    "databricks": {
        "class": "databricks",
        "server": "https://adb.example/",
        "http_path": "/sql/1.0/warehouses/ExactPath",
        "database": "ExactCatalog",
        "schema": "default",
        "credential_scope": "desktop-fixture",
    },
    "snowflake": {
        "class": "snowflake",
        "server": "https://account.snowflakecomputing.com/",
        "warehouse": "ExactWarehouse",
        "database": "ExactDatabase",
        "role": "ExactRole",
        "schema": "PUBLIC",
        "credential_scope": "desktop-fixture",
    },
}
HEADS = {
    "sqlserver": '    Source = Sql.Database("sql.example,1544", "ExactDatabase"),\n',
    "databricks": (
        '    Source = Databricks.Catalogs("adb.example", "/sql/1.0/warehouses/ExactPath", null),\n'
        '    db = Source{[Name="ExactCatalog",Kind="Database"]}[Data],\n'
    ),
    "snowflake": (
        '    Source = Snowflake.Databases("account.snowflakecomputing.com", "ExactWarehouse", [Role="ExactRole"]),\n'
        '    db = Source{[Name="ExactDatabase",Kind="Database"]}[Data],\n'
    ),
}


def _source(connector: str = "sqlserver", *, ordinary: bool = False, sql: str | None = EXPENSIVE_SQL) -> dict:
    table = (
        {"name": "Orders", "source_relation": "table"}
        if ordinary
        else {"name": "Custom query", "source_relation": "custom-sql", "custom_sql": sql}
    )
    return {"connection": dict(CONNECTIONS[connector]), "tables": [table]}


def _spec(root: Path, sources: list[dict]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    spec = root / "migration-spec.json"
    spec.write_text(json.dumps({"data_sources": sources}), encoding="utf-8")
    return spec


def _attempts(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / gate.AUDIT).read_text(encoding="utf-8").splitlines()]


@dataclass
class ConnectorResponse:
    """Independent source/child outcomes for the dispatch fixture."""

    reply: tuple[int, str] | None = None
    timeout: bool = False
    rows: int = 1
    deny_custom_query: bool = False


@dataclass
class OfflineDesktop:
    """Count what the child is asked to execute; never contact a database or Desktop."""

    queries: list[str] = field(default_factory=list)
    scaffold: list[str] = field(default_factory=list)
    argv: list[list[str]] = field(default_factory=list)
    lifts: list[list[str]] = field(default_factory=list)
    sql_executions: int = 0
    native_executions: int = 0
    response: ConnectorResponse = field(default_factory=ConnectorResponse)

    def open(self, pbip: Path) -> int:
        """Read the exact emitted one-table partition the production opener received."""
        tables = list(pbip.parent.glob("*.SemanticModel/definition/tables/*.tmdl"))
        assert len(tables) == 1
        text = tables[0].read_text(encoding="utf-8")
        assert "sourceColumn: ProbeOK" in text
        self.scaffold.extend(path.read_text(encoding="utf-8") for path in pbip.parent.rglob("*") if path.is_file())
        query = text.split("\t\tsource =\n", 1)[1]
        self.queries.append("\n".join(line.removeprefix("\t\t\t\t") for line in query.splitlines()))
        return 4242

    def refresh(self, argv: list[str], **_kwargs) -> subprocess.CompletedProcess:
        """A SQL sentinel dispatch is observable even if it later fails or returns one row."""
        self.argv.append(argv)
        assert Path(argv[1]) == probe.SKILL_SCRIPTS / "refresh_pbip_model.py"
        assert "--no-save" in argv and "--no-progress" in argv
        self.sql_executions += int(SENTINEL in self.queries[-1])
        self.native_executions += int(bool(NATIVE.search(self.queries[-1])))
        if self.response.timeout:
            raise subprocess.TimeoutExpired(argv, 7)
        table = argv[argv.index("--tables") + 1]
        if self.response.deny_custom_query and self.sql_executions:
            code, text = 1, "permission denied for custom query"
        elif self.response.reply is not None:
            code, text = self.response.reply
        elif not self.response.rows:
            code, text = 1, "REFRESH: NO_DATA"
        else:
            code, text = 0, f"REFRESH: TABLES_OK '{table}'"
        return subprocess.CompletedProcess(argv, code, stdout=text, stderr="")

    def lift(self, _root: Path, _what: str, sources: list[str]) -> bool:
        """A spy, not a replacement grant: new connection-only tests must never call it."""
        self.lifts.append(sources)
        return False


@pytest.fixture(name="desktop")
def desktop_fixture(monkeypatch: pytest.MonkeyPatch) -> OfflineDesktop:
    """Replace only external Desktop/network effects and observe attempted gate lifting."""
    desktop = OfflineDesktop()
    monkeypatch.setattr(probe, "_host_resolves", lambda _host: True)
    monkeypatch.setattr(probe, "_open_desktop", desktop.open)
    monkeypatch.setattr(probe, "_wait_for_catalog", lambda _pid: True)
    monkeypatch.setattr(probe, "_record_desktop_lifecycle", lambda *_args: {})
    monkeypatch.setattr(probe, "_close", lambda _pid, _pbip: True)
    monkeypatch.setattr(probe, "_network_fault_observed", lambda _conn: False)
    monkeypatch.setattr(probe, "_lift_gate", desktop.lift)
    monkeypatch.setattr(probe.subprocess, "run", desktop.refresh)
    return desktop


def _assert_connection_only(code: int, desktop: OfflineDesktop, root: Path) -> None:
    assert desktop.sql_executions == 0, "customer SQL executed in the default probe"
    assert desktop.native_executions == 0, "native SQL fallback entered the default probe"
    assert code == 1, "connection-only completion must exit 1"
    assert not desktop.lifts, "connection-only evidence must not request a gate lift"
    entries = _attempts(root)
    assert not any(entry["action"] == "probe-cleared" for entry in entries), (
        "connection observation cannot earn clearance"
    )
    assert not any("proved_names" in entry for entry in entries)
    observations = [
        entry for entry in entries if entry["action"] == "probe-error" and entry["detail"].startswith(TOKEN + ":")
    ]
    assert observations, "a keyed connection-only observation is required"
    assert gate._read_audit_trail(root)[1] is None, "the observation must preserve the closed audit wire protocol"
    assert all(
        entry["sources"] and all(key.startswith("source-key:") for key in entry["sources"]) for entry in observations
    )


@pytest.mark.parametrize("connector", CONNECTIONS)
@pytest.mark.parametrize("sql", [EXPENSIVE_SQL, SIDE_EFFECT_SQL, "", None, "-- only a comment"])
def test_default_never_dispatches_sql_or_clears(
    tmp_path: Path, desktop: OfflineDesktop, caplog: pytest.LogCaptureFixture, connector: str, sql: str | None
) -> None:
    """The public CLI retains exact spec bytes and sends no SQL or relation label downstream."""
    source = _source(connector, sql=sql)
    source["tables"][0]["name"] = SIDE_EFFECT_SQL
    spec = _spec(tmp_path, [source])
    original = spec.read_bytes()
    caplog.set_level("INFO", logger="probe_live_source")
    code = probe.main(["--spec", str(spec)])

    _assert_connection_only(code, desktop, tmp_path)
    assert spec.read_bytes() == original
    assert len(desktop.queries) == 1
    assert MESSAGE in caplog.text
    assert "PROBE: DATA_OK" not in caplog.text
    output = "\n".join(desktop.scaffold) + repr(desktop.argv) + caplog.text
    assert SENTINEL not in output and SIDE_EFFECT_SQL not in output
    assert desktop.argv[0][desktop.argv[0].index("--tables") + 1] == "ConnectionProbe"


@pytest.mark.parametrize("connector", CONNECTIONS)
def test_navigation_is_forced_on_the_loaded_partition(tmp_path: Path, desktop: OfflineDesktop, connector: str) -> None:
    """No unused let-binding or local success row may replace the real navigation dependency."""
    spec = _spec(tmp_path, [_source(connector)])
    assert probe.main(["--spec", str(spec)]) == 1
    navigation, column = ("Source", "Item") if connector == "sqlserver" else ("db", "Name")
    expected = (
        "let\n"
        + HEADS[connector]
        + f'    navigation = Table.Buffer(Table.SelectColumns({navigation}, {{"{column}"}}),\n'
        + "        [BufferMode=BufferMode.Eager]),\n"
        + f'    probe = Table.RenameColumns(Table.FirstN(navigation, 1), {{{{"{column}", "ProbeOK"}}}})\n'
        + "in\n    probe"
    )
    assert desktop.queries == [expected], "navigation must be eagerly evaluated on the output dependency path"
    assert "#table" not in expected and 'Kind="Table"' not in expected
    assert 'Kind="Schema"' not in expected and NATIVE.search(expected) is None


@pytest.mark.parametrize(
    ("server", "port", "expected", "dns"),
    [
        (r"sql.example\ExactInstance", None, r"sql.example\ExactInstance", "sql.example"),
        ("tcp:sql.example,1544", "1544", "tcp:sql.example,1544", "sql.example"),
        ("sql.example:1544", None, "sql.example:1544", "sql.example"),
        ("https://sql.example/", 1544, "sql.example,1544", "sql.example"),
    ],
)
def test_sql_server_scope_keeps_port_and_instance(server: str, port: str | int | None, expected: str, dns: str) -> None:
    """The SQL endpoint and DNS discriminator deliberately receive different strings."""
    conn = {**CONNECTIONS["sqlserver"], "server": server, "port": port}
    custom, _ = probe.build_m_query(conn, "ignored", "ignored", custom_sql=SIDE_EFFECT_SQL)
    ordinary, _ = probe.build_m_query(conn, "Orders", "ignored")
    assert f'Sql.Database("{expected}", "ExactDatabase")' in custom
    assert f'Sql.Database("{expected}", "ExactDatabase")' in ordinary
    assert probe._network_server(conn) == dns
    assert SENTINEL not in custom


@pytest.mark.parametrize("port", ["bad", "0", "65536", "1444"])
def test_conflicting_or_invalid_sql_port_refuses_before_desktop(
    tmp_path: Path, desktop: OfflineDesktop, port: str
) -> None:
    """A malformed exact scope is an error, never an excuse to select another endpoint."""
    source = _source()
    source["connection"].update(server="sql.example,1544", port=port)
    spec = _spec(tmp_path, [source])
    assert probe.main(["--spec", str(spec)]) == 1
    assert not desktop.queries
    assert _attempts(tmp_path)[-1]["action"] == "probe-error"


def test_connector_identifiers_are_literal_m_values() -> None:
    """Quotes, M escape sequences and line breaks cannot become executable connector text."""
    conn = {**CONNECTIONS["snowflake"], "database": 'DB"#(lf)\r\n', "role": 'Role"Exact'}
    query, _ = probe.build_m_query(conn, SIDE_EFFECT_SQL, "ignored", custom_sql=SIDE_EFFECT_SQL)
    assert 'Name="DB""#(#)(lf)#(cr)#(lf)"' in query
    assert 'Role="Role""Exact"' in query
    assert SENTINEL not in query


class SqlReadTrap(dict):
    """The default must not even request a custom_sql value for M generation."""

    def get(self, key: str, default: object = None) -> object:
        if key == "custom_sql":
            raise AssertionError("default path read the customer SQL payload")
        return super().get(key, default)


def test_default_does_not_read_the_sql_payload(tmp_path: Path, desktop: OfflineDesktop) -> None:
    """The source_relation discriminator is sufficient, even with an unreadable SQL payload."""
    source = _source()
    source["tables"] = [SqlReadTrap(source["tables"][0])]
    assert probe._probe_one(tmp_path, [source], 0, 7, False) == (1, TOKEN)
    _assert_connection_only(1, desktop, tmp_path)


@pytest.mark.parametrize("connector", CONNECTIONS)
@pytest.mark.parametrize("same_source", [True, False], ids=["one-leg", "earlier-source"])
def test_ordinary_data_ok_reuses_only_connection_evidence(
    tmp_path: Path, desktop: OfflineDesktop, connector: str, same_source: bool
) -> None:
    """Reusing a real row saves the extra connector operation, never validates a later query."""
    ordinary, custom = _source(connector, ordinary=True), _source(connector)
    sources = [ordinary, custom]
    if same_source:
        ordinary["tables"] = custom["tables"] + ordinary["tables"]
        sources = [ordinary]
    spec = _spec(tmp_path, sources)
    code = probe.main(["--spec", str(spec)])
    _assert_connection_only(code, desktop, tmp_path)
    assert len(desktop.queries) == 1
    assert "Table.ColumnNames(tbl)" in desktop.queries[0]
    assert [entry["action"] for entry in _attempts(tmp_path)] == [
        "probe-data_ok",
        "probe-error",
    ]
    assert _attempts(tmp_path)[-1]["detail"].startswith(TOKEN + ":")


@pytest.mark.parametrize(
    ("connector", "field_name", "changed"),
    [
        ("sqlserver", "database", "OtherDatabase"),
        ("sqlserver", "port", "1545"),
        ("sqlserver", "server", r"sql.example\OtherInstance"),
        ("databricks", "http_path", "/sql/1.0/warehouses/OtherPath"),
        ("databricks", "database", "OtherCatalog"),
        ("snowflake", "warehouse", "OtherWarehouse"),
        ("snowflake", "role", "OtherRole"),
        ("snowflake", "database", "OtherDatabase"),
        *[(connector, "credential_scope", "another-desktop-scope") for connector in CONNECTIONS],
        *[(connector, "authentication", {"kind": "other"}) for connector in CONNECTIONS],
        ("sqlserver", "username", "other-principal"),
        ("sqlserver", "future_session_hint", "different"),
    ],
)
def test_different_scope_cannot_borrow_an_ordinary_success(
    tmp_path: Path, desktop: OfflineDesktop, connector: str, field_name: str, changed: object
) -> None:
    """Include credential hints absent from _leg_key, not just connector navigation fields."""
    ordinary, custom = _source(connector, ordinary=True), _source(connector)
    custom["connection"][field_name] = changed
    spec = _spec(tmp_path, [ordinary, custom])
    _assert_connection_only(probe.main(["--spec", str(spec)]), desktop, tmp_path)
    assert len(desktop.queries) == 2, "different connector/credential scopes must not reuse evidence"
    assert "BufferMode.Eager" in desktop.queries[-1]


def test_scope_snapshot_and_invocation_lifetime(tmp_path: Path, desktop: OfflineDesktop) -> None:
    """Neither a mutable credential hint nor an earlier audit can become reusable proof."""
    source = _source()
    snapshot = probe._connection_scope(source["connection"])
    source["connection"]["credential_scope"] = "changed"
    assert snapshot != probe._connection_scope(source["connection"])
    sources = [_source(ordinary=True), _source()]
    spec = _spec(tmp_path, sources)
    assert probe.main(["--spec", str(spec)]) == 1
    assert len(desktop.queries) == 1
    assert probe.main(["--spec", str(spec), "--source-index", "1"]) == 1
    assert len(desktop.queries) == 2, "a prior invocation must not seed connection evidence"


def test_every_custom_leg_is_observed_without_reusing_navigation_as_rows(
    tmp_path: Path, desktop: OfflineDesktop
) -> None:
    """A connection-only result is never inserted into the ordinary DATA_OK cache."""
    source = _source()
    source["connection"] = {"class": "federated", "connections": [dict(CONNECTIONS["sqlserver"])] * 2}
    spec = _spec(tmp_path, [source])
    _assert_connection_only(probe.main(["--spec", str(spec)]), desktop, tmp_path)
    assert len(desktop.queries) == 2
    assert len(_attempts(tmp_path)) == 2


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        ((1, "REFRESH: CREDENTIAL_MISSING"), "NO_CREDENTIAL"),
        ((1, "403 Unauthorized: authentication failed"), "ACCESS_DENIED"),
        ((1, "403 Forbidden: access token revoked"), "ACCESS_DENIED"),
        ((1, "permission denied for object"), "ACCESS_DENIED"),
        ((1, "REFRESH: DIALOG_NEEDS_HUMAN native query approval"), "ERROR"),
        ((1, "REFRESH: DIALOG_UNRECOGNIZED"), "ERROR"),
        ((1, "REFRESH: DIALOG_UNREADABLE"), "ERROR"),
        ((1, "REFRESH: TIMEOUT possible sign-in or stalled source"), "ERROR"),
        ((0, "no blocking dialog"), "ERROR"),
        ((1, "REFRESH: TABLES_OK 'ConnectionProbe'"), "ERROR"),
        ((0, "REFRESH: TABLES_OK 'WrongTable'"), "ERROR"),
        ((0, "REFRESH: TABLES_OK 'ConnectionProbe'\nREFRESH: DIALOG_NEEDS_HUMAN"), "ERROR"),
    ],
)
def test_navigation_preserves_existing_failure_verdicts(
    tmp_path: Path, desktop: OfflineDesktop, reply: tuple[int, str], verdict: str
) -> None:
    """An ambiguous/native dialog stays on the existing ERROR route pending #146/#687."""
    desktop.response.reply = reply
    spec = _spec(tmp_path, [_source()])
    assert probe.main(["--spec", str(spec)]) == 1
    assert _attempts(tmp_path)[-1]["action"] == f"probe-{verdict.lower()}"
    assert desktop.sql_executions == 0 and not desktop.lifts


def test_timeout_without_popup_is_error(tmp_path: Path, desktop: OfflineDesktop) -> None:
    """The parent subprocess deadline, with no observed network fault, is neutral evidence."""
    desktop.response.timeout = True
    spec = _spec(tmp_path, [_source()])
    assert probe.main(["--spec", str(spec), "--refresh-timeout-sec", "7"]) == 1
    assert _attempts(tmp_path)[-1]["action"] == "probe-error"
    assert not desktop.lifts


def test_network_failure_stays_unreachable(
    tmp_path: Path, desktop: OfflineDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DNS failure refuses before Desktop, without inventing a credential or connection success."""
    monkeypatch.setattr(probe, "_host_resolves", lambda _host: False)
    assert probe.main(["--spec", str(_spec(tmp_path, [_source()]))]) == 1
    assert not desktop.queries
    assert _attempts(tmp_path)[-1]["action"] == "probe-unreachable"


def test_shell_success_and_later_query_denial_prove_nothing(
    tmp_path: Path, desktop: OfflineDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hypothetical ODBC success and a later query denial cannot upgrade connection evidence."""
    monkeypatch.setenv("SQLCMD_SUCCESS", "1")
    monkeypatch.setenv("ODBC_SUCCESS", "1")
    desktop.response.deny_custom_query = True
    spec = _spec(tmp_path, [_source()])
    _assert_connection_only(probe.main(["--spec", str(spec)]), desktop, tmp_path)
    assert probe._classify_failure("permission denied for custom query", False)[0] == "ACCESS_DENIED"
    desktop.response.reply = (1, "REFRESH: CREDENTIAL_MISSING")
    assert probe.main(["--spec", str(spec)]) == 1
    assert _attempts(tmp_path)[-1]["action"] == "probe-no_credential"
    assert all(Path(argv[1]).name == "refresh_pbip_model.py" for argv in desktop.argv)


def test_audit_and_verifier_keep_unvalidated_artifacts_blocked(
    tmp_path: Path, desktop: OfflineDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use real gate/audit logic, with a virtual ACL, not a fake verifier success."""
    source = _source()
    spec = _spec(tmp_path, [source])
    artifact = tmp_path / "fabric" / "unfinished.pbip"
    artifact.parent.mkdir()
    artifact.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gate.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gate, "_user", lambda: "fixture")
    monkeypatch.setattr(gate, "_icacls", lambda _args: (0, "fixture ACL"))
    monkeypatch.setattr(gate, "_has_deny_ace", lambda root: (root / gate.MARKER).exists())
    key = probe._leg_key(source, 0, source["connection"])
    gate.apply_block(tmp_path, [key])
    marker = (tmp_path / gate.MARKER).read_bytes()
    _assert_connection_only(probe.main(["--spec", str(spec)]), desktop, tmp_path)
    assert (tmp_path / gate.MARKER).read_bytes() == marker
    assert gate.status(tmp_path) == 1
    assert gate.verify(tmp_path) == 1
    assert not (tmp_path / gate.OVERRIDE).exists()
    artifact.unlink()
    assert gate.verify(tmp_path) == 0, "an enforced empty gate is compliant, not cleared"
    assert gate.status(tmp_path) == 1 and (tmp_path / gate.MARKER).exists()


@pytest.mark.parametrize("ordinary", [True, False], ids=["ordinary", "navigation"])
def test_empty_results_are_not_replaced_by_a_success_row(
    tmp_path: Path, desktop: OfflineDesktop, ordinary: bool
) -> None:
    """DATA_EMPTY is separately scoped; this change preserves the existing no-row refusal."""
    desktop.response.rows = 0
    source = _source(ordinary=ordinary)
    assert probe._probe_one(tmp_path, [source], 0, 7, False) == (1, "ERROR")
    assert _attempts(tmp_path)[-1]["action"] == "probe-error"
    assert not desktop.lifts


def test_ordinary_success_and_non_live_skip_remain_unchanged(tmp_path: Path, desktop: OfflineDesktop) -> None:
    """Ordinary DATA_OK is still a real-row verdict and SKIPPED still opens nothing."""
    assert probe._probe_one(tmp_path, [_source(ordinary=True)], 0, 7, False) == (0, "DATA_OK")
    source = {"connection": {"class": "textscan"}, "tables": [{"name": "file"}]}
    assert probe.main(["--spec", str(_spec(tmp_path, [source]))]) == 0
    assert len(desktop.queries) == 1


def test_no_exact_mode_is_implemented(tmp_path: Path, desktop: OfflineDesktop) -> None:
    """Exact original-query execution must not arrive implicitly with this safety slice."""
    spec = _spec(tmp_path, [_source()])
    with pytest.raises(SystemExit) as raised:
        probe.main(["--spec", str(spec), "--custom-sql-mode", "exact"])
    assert raised.value.code == 2 and not desktop.queries


def test_connection_only_main_exits_nonzero_in_a_process(tmp_path: Path) -> None:
    """Exercise a real child exit with offline Desktop injection, not a forged result code."""
    spec = _spec(tmp_path, [_source(sql=SIDE_EFFECT_SQL)])
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(sys.argv[1]) / 'tests'))\n"
        "import pytest\n"
        "from test_probe_custom_sql_connection import desktop_fixture, probe\n"
        "with pytest.MonkeyPatch.context() as patch:\n"
        "    desktop_fixture.__wrapped__(patch)\n"
        "    code = probe.main(['--spec', sys.argv[2]])\n"
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
    assert MESSAGE in child.stderr, child.stdout + child.stderr
    assert SENTINEL not in child.stdout + child.stderr
    assert "Traceback" not in child.stderr


def test_failure_directives_do_not_invent_authentication_history(caplog: pytest.LogCaptureFixture) -> None:
    """ACCESS_DENIED also covers revoked tokens; NO_CREDENTIAL says nothing about past logins."""
    caplog.set_level("INFO", logger="probe_live_source")
    probe._print_verdict_directive("NO_CREDENTIAL")
    assert "has never authenticated" not in caplog.text
    assert "source is REACHABLE" not in caplog.text
    caplog.clear()
    probe._print_verdict_directive("ACCESS_DENIED")
    assert "authenticated identity" not in caplog.text and "until permissions change" not in caplog.text
    assert "revoked/expired token" in caplog.text


@pytest.mark.parametrize(
    "mutation",
    [
        (
            "_probe_one_table",
            'pbip = _write_probe_model(migration, m_query, table, "ProbeOK")',
            'm_query = f\'let Source = Sql.Database("sql.example", "DB", [Query="{table_spec.get("custom_sql")}"]) '
            'in Source\'\n            pbip = _write_probe_model(migration, m_query, table, "ProbeOK")',
            "customer SQL executed",
        ),
        (
            "_probe_one_table",
            'pbip = _write_probe_model(migration, m_query, table, "ProbeOK")',
            'm_query = \'let Source = Sql.Database("sql.example", "DB", [Query="SELECT 1"]) in Source\'\n'
            '            pbip = _write_probe_model(migration, m_query, table, "ProbeOK")',
            "native SQL fallback",
        ),
        (
            "_probe_one_table",
            'pbip = _write_probe_model(migration, m_query, table, "ProbeOK")',
            'm_query = \'let Source = Sql.Database("sql.example", "DB"), '
            'probe = Value.NativeQuery(Source, "SELECT 1") in probe\'\n'
            '            pbip = _write_probe_model(migration, m_query, table, "ProbeOK")',
            "native SQL fallback",
        ),
        (
            "run_probe",
            "if connection_only:\n        _print_verdict_directive(CONNECTION_ONLY)\n        return 1",
            "if connection_only:\n        _print_verdict_directive(CONNECTION_ONLY)\n        return 0",
            "connection-only completion must exit 1",
        ),
        (
            "_record_attempt",
            "_audit(migration, action, what, sources=list(sources))",
            '_audit(migration, "probe-cleared", what, sources=list(sources))',
            "connection observation cannot earn clearance",
        ),
    ],
    ids=["customer-sql", "select-one-query-option", "select-one-native-query", "exit-zero", "probe-cleared"],
)
def test_mutations_fail_the_intended_safety_assertion(
    tmp_path: Path,
    desktop: OfflineDesktop,
    monkeypatch: pytest.MonkeyPatch,
    mutation: tuple[str, str, str, str],
) -> None:
    """Mutate production code in memory; syntax/import errors are never counted as kills."""
    function, old, new, assertion = mutation
    original = inspect.getsource(getattr(probe, function))
    assert original.count(old) == 1, "mutation must match exactly one production boundary"
    namespace = dict(vars(probe))
    exec(compile(original.replace(old, new), "<issue690-mutation>", "exec"), namespace)  # pylint: disable=exec-used
    monkeypatch.setattr(probe, function, namespace[function])
    spec = _spec(tmp_path, [_source()])
    code = probe.main(["--spec", str(spec)])
    with pytest.raises(AssertionError, match=assertion):
        _assert_connection_only(code, desktop, tmp_path)


def test_cross_scope_mutation_is_caught_at_reuse_boundary(
    tmp_path: Path, desktop: OfflineDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting credential/session fields must fail the actual two-source dispatch assertion."""
    monkeypatch.setattr(probe, "_connection_scope", lambda conn: probe.normalize_host(conn.get("server") or ""))
    with pytest.raises(AssertionError, match="different connector/credential scopes must not reuse"):
        test_different_scope_cannot_borrow_an_ordinary_success(tmp_path, desktop, "snowflake", "role", "DifferentRole")


def test_lazy_local_row_mutation_is_caught(
    tmp_path: Path, desktop: OfflineDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh reporting one row is insufficient if the emitted M never touches navigation."""
    monkeypatch.setattr(
        probe,
        "_navigation_m",
        lambda *_args: ('let probe = #table({"ProbeOK"}, {{"local"}}) in probe', "mutant"),
    )
    with pytest.raises(AssertionError, match="navigation must be eagerly evaluated"):
        test_navigation_is_forced_on_the_loaded_partition(tmp_path, desktop, "sqlserver")
