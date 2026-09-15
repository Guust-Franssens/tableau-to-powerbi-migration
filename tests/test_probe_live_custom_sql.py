"""Regression tests for the custom-SQL probe path in `scripts/probe_live_source.py`.

A Tableau relation of `type='text'` is a hand-written SELECT that Tableau merely NAMES (e.g.
`Flight_Level_Query`). The parser records that as `custom_sql` on the table. The probe runs that query
and projects a known constant, so it does not depend on optional enumerated columns.

Ordinary table navigation remains covered here; its runtime physical-column projection must not
change the custom-SQL path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import probe_live_source  # noqa: E402  # pylint: disable=wrong-import-position
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

PROBE_PROJECTION = (
    'without_probe = Table.RemoveColumns(one, {"ProbeOK"}, MissingField.Ignore),\n'
    '    probe = Table.SelectColumns(Table.AddColumn(without_probe, "ProbeOK", each 1), {"ProbeOK"})'
)


def _source(tables: list[dict], fields: list[dict] | None = None) -> dict:
    for table in tables:
        if table.get("custom_sql") is not None:
            table["source_relation"] = "custom-sql"
    return {
        "connection": SNOWFLAKE,
        "tables": tables,
        "fields": [{"kind": "column", "internal_name": "[Col]"}] if fields is None else fields,
    }


def test_custom_sql_scaffold_contains_the_sql_and_projects_a_probe_column():
    m, note = probe_live_source.build_m_query(
        SNOWFLAKE, "Flight_Level_Query", "Col", custom_sql="SELECT a, b FROM raw.flights"
    )
    assert "Value.NativeQuery" in m
    assert 'Table.FirstN(Value.NativeQuery(db, "SELECT a, b FROM raw.flights"), 1)' in m
    assert PROBE_PROJECTION in m
    assert 'Kind="Table"' not in m, "a custom-SQL relation has no table to navigate to"
    assert "SELECT a, b FROM raw.flights" in m
    assert "custom SQL" in note, "the operator must be told which path was scaffolded"


def test_databricks_custom_sql_scaffold_uses_native_query_without_automatic_probe():
    m, note = probe_live_source.build_m_query(
        DATABRICKS, "Flight_Level_Query", "Col", custom_sql="SELECT a, b FROM raw.flights"
    )
    assert "Databricks.Catalogs" in m
    assert "Value.NativeQuery" in m
    assert 'Table.FirstN(Value.NativeQuery(db, "SELECT a, b FROM raw.flights"), 1)' in m
    assert PROBE_PROJECTION in m
    assert 'Kind="Schema"' not in m
    assert "SELECT a, b FROM raw.flights" in m
    assert "custom SQL" in note


def test_sql_server_custom_sql_scaffold_selects_the_added_probe_column():
    m, _ = probe_live_source.build_m_query(SQLSERVER, "Q", "Col", custom_sql="SELECT a FROM t")

    assert PROBE_PROJECTION in m


def test_comment_only_custom_sql_is_cannot_assess_instead_of_table_navigation():
    with pytest.raises(ValueError, match="empty after removing comments"):
        probe_live_source.build_m_query(SNOWFLAKE, "Q", "Col", custom_sql="-- comment only")


def test_block_comment_only_custom_sql_is_cannot_assess():
    with pytest.raises(ValueError, match="empty after removing comments"):
        probe_live_source.build_m_query(SNOWFLAKE, "Q", "Col", custom_sql="/* comment only */")


def test_executable_sql_survives_a_block_comment():
    m, _ = probe_live_source.build_m_query(SNOWFLAKE, "Q", "Col", custom_sql="SELECT/* note */1")

    assert 'Value.NativeQuery(db, "SELECT 1")' in m


def test_block_comment_markers_inside_sql_string_survive():
    m, _ = probe_live_source.build_m_query(SNOWFLAKE, "Q", "Col", custom_sql="SELECT '/* data */'")

    assert "SELECT '/* data */'" in m


def test_parser_self_closing_text_relation_cannot_clear_as_a_physical_table(tmp_path, monkeypatch):
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

    monkeypatch.setattr(probe_live_source, "_host_resolves", lambda _server: True)
    monkeypatch.setattr(probe_live_source, "_open_desktop", lambda _pbip: 123)
    rc, verdict = probe_live_source._probe_one(tmp_path, [source], 0, 1, False)  # pylint: disable=protected-access

    assert (rc, verdict) == (1, "ERROR")


def test_empty_custom_sql_after_a_real_table_cannot_clear_the_source(tmp_path, monkeypatch):
    source = _source(
        [
            {"name": "REAL_TABLE", "custom_sql": None},
            {"name": "EMPTY_QUERY", "custom_sql": ""},
        ]
    )
    original_probe = probe_live_source._probe_one_table
    attempted = []

    def _probe_table(migration, leg_name, conn, target, opts):
        attempted.append(target[0]["name"])
        if target[0]["name"] == "REAL_TABLE":
            return 0, "DATA_OK"
        return original_probe(migration, leg_name, conn, target, opts)

    monkeypatch.setattr(probe_live_source, "_host_resolves", lambda _server: True)
    monkeypatch.setattr(probe_live_source, "_probe_one_table", _probe_table)

    rc, verdict = probe_live_source._probe_one(tmp_path, [source], 0, 1, False)  # pylint: disable=protected-access

    assert (rc, verdict) == (1, "ERROR")
    assert attempted == ["REAL_TABLE", "EMPTY_QUERY"]


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
    """No Tableau field token may become a guessed physical column on any ordinary branch."""
    internal_name = "[Tableau_Local_Column]"
    m, note = probe_live_source.build_m_query(conn, "FLIGHTS", internal_name.strip("[]"))

    assert internal_name.strip("[]") not in m
    assert m.startswith("let\n" + navigation + "    columns = Table.ColumnNames(tbl),\n")
    assert "Table.FirstN(Table.SelectColumns(tbl, {columns{0}}), 1)" in m
    assert "Table.RenameColumns(\n" in m
    assert '{{columns{0}, "ProbeOK"}})' in m
    assert m.endswith("in\n    probe")
    assert "Value.NativeQuery" not in m
    assert "custom SQL" not in note
    assert "try " not in m, "a missing table must still fail at the declared navigation key"


@pytest.mark.parametrize("conn", [SNOWFLAKE, DATABRICKS, SQLSERVER], ids=["snowflake", "databricks", "sqlserver"])
def test_ordinary_probe_projection_cannot_manufacture_rows_without_columns(conn: dict) -> None:
    """A columnless source is empty evidence; zero-row sources stay empty after projection."""
    m, _ = probe_live_source.build_m_query(conn, "FLIGHTS", "Tableau_Local_Column")
    projection = m[m.index("    columns =") :]

    assert projection == (
        "    columns = Table.ColumnNames(tbl),\n"
        '    probe = if List.IsEmpty(columns) then #table({"ProbeOK"}, {}) else\n'
        "        Table.RenameColumns(\n"
        "            Table.FirstN(Table.SelectColumns(tbl, {columns{0}}), 1),\n"
        '            {{columns{0}, "ProbeOK"}})\n'
        "in\n"
        "    probe"
    )


@pytest.mark.parametrize(
    ("conn", "head", "note"),
    [
        (
            SNOWFLAKE,
            '    Source = Snowflake.Databases("ORG-ACCOUNT.snowflakecomputing.com", "WH", null),\n'
            '    db = Source{[Name="DB",Kind="Database"]}[Data],\n'
            '    one = Table.FirstN(Value.NativeQuery(db, "SELECT a FROM t"), 1),\n',
            "Snowflake ORG-ACCOUNT.snowflakecomputing.com (WH) :: DB :: custom SQL 'Q'",
        ),
        (
            DATABRICKS,
            '    Source = Databricks.Catalogs("adb.example.azuredatabricks.net", "/sql/1.0/warehouses/abc", null),\n'
            '    db = Source{[Name="hive_metastore",Kind="Database"]}[Data],\n'
            '    one = Table.FirstN(Value.NativeQuery(db, "SELECT a FROM t"), 1),\n',
            "Databricks adb.example.azuredatabricks.net/sql/1.0/warehouses/abc :: hive_metastore :: custom SQL 'Q'",
        ),
        (
            SQLSERVER,
            '    Source = Sql.Database("sql.example.com", "DB", [Query="SELECT a FROM t"]),\n'
            "    one = Table.FirstN(Source, 1),\n",
            "SQL Server sql.example.com :: DB :: custom SQL 'Q'",
        ),
    ],
    ids=["snowflake", "databricks", "sqlserver"],
)
def test_custom_sql_query_and_note_remain_byte_for_byte_unchanged(conn: dict, head: str, note: str) -> None:
    """Pin the pre-647 custom-SQL output independently of the ordinary-table projection."""
    m, actual_note = probe_live_source.build_m_query(conn, "Q", "Col", custom_sql="SELECT a FROM t")

    assert m == "let\n" + head + "    " + PROBE_PROJECTION + "\nin\n    probe"
    assert actual_note == note


def test_real_table_probe_still_opens_refreshes_and_returns_data_ok(tmp_path, monkeypatch):
    events = []

    def _open(pbip: Path) -> int:
        tmdl = next(pbip.parent.glob("*.SemanticModel/definition/tables/*.tmdl")).read_text(encoding="utf-8")
        assert "column 'ProbeOK'" in tmdl
        assert "sourceColumn: ProbeOK" in tmdl
        assert "column 'Col'" not in tmdl
        events.append(("open", pbip.name))
        return 123

    def _refresh(pid: int, table: str, timeout_sec: int, network_fault_observed: bool) -> tuple[int, str]:
        events.append(("refresh", pid, table, timeout_sec, network_fault_observed))
        return 0, "DATA_OK"

    monkeypatch.setattr(probe_live_source, "_open_desktop", _open)
    monkeypatch.setattr(probe_live_source, "_wait_for_catalog", lambda _pid: True)
    monkeypatch.setattr(probe_live_source, "_network_fault_observed", lambda _conn: False)
    monkeypatch.setattr(probe_live_source, "_refresh_and_classify", _refresh)
    monkeypatch.setattr(probe_live_source, "_close", lambda _pid, _pbip: True)

    rc, verdict = probe_live_source._probe_one_table(  # pylint: disable=protected-access
        tmp_path,
        "source-key:0000000000000000",
        SNOWFLAKE,
        ({"name": "FLIGHTS", "custom_sql": None}, "Col"),
        (7, False),
    )

    assert rc == 0
    assert verdict == "DATA_OK"
    assert events == [("open", "Probe.pbip"), ("refresh", 123, "FLIGHTS", 7, False)]


def test_embedded_double_quotes_in_the_sql_are_escaped_for_m():
    m, _ = probe_live_source.build_m_query(SNOWFLAKE, "Q", "Col", custom_sql='SELECT "Tail Number" FROM t')
    assert '""Tail Number""' in m, "M escapes a quote by doubling it"


def test_a_line_comment_cannot_swallow_the_rest_of_the_collapsed_query():
    # The query is collapsed to one line so TMDL indentation cannot push tabs inside the literal.
    # A surviving `--` comment would then comment out everything after it.
    m, _ = probe_live_source.build_m_query(SNOWFLAKE, "Q", "Col", custom_sql="SELECT a -- the id\nFROM t")
    assert "--" not in m
    assert "SELECT a FROM t" in m
    assert "\\n" not in m and m.count("Value.NativeQuery") == 1


def test_custom_sql_probe_without_enumerated_columns_clears_when_refresh_returns_data(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_live_source, "_open_desktop", lambda _pbip: 123)
    monkeypatch.setattr(probe_live_source, "_wait_for_catalog", lambda _pid: True)
    monkeypatch.setattr(probe_live_source, "_network_fault_observed", lambda _conn: False)
    monkeypatch.setattr(probe_live_source, "_refresh_and_classify", lambda *_args: (0, "DATA_OK"))
    monkeypatch.setattr(probe_live_source, "_close", lambda _pid, _pbip: True)

    rc, verdict = probe_live_source._probe_one_table(  # pylint: disable=protected-access
        tmp_path,
        "source-key:0000000000000000",
        SNOWFLAKE,
        (
            {
                "name": "Flight_Level_Query",
                "source_relation": "custom-sql",
                "custom_sql": "SELECT a FROM raw.flights",
            },
            "ProbeOK",
        ),
        (1, False),
    )

    assert (rc, verdict) == (0, "DATA_OK")
    pbip = next(tmp_path.glob("_probe/run-*/Probe.pbip"))
    assert pbip.exists(), "operator handoff must include the probe PBIP"
    tmdl = next(pbip.parent.glob("*.SemanticModel/definition/tables/*.tmdl")).read_text()
    assert "column 'ProbeOK'" in tmdl
    assert "sourceColumn: ProbeOK" in tmdl


def test_custom_sql_without_enumerated_columns_is_resolvable():
    source = _source([{"name": "Q", "source_relation": "custom-sql", "custom_sql": "SELECT 1"}], fields=[])

    _, tables, column = probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access

    assert [table["name"] for table in tables] == ["Q"]
    assert column == "ProbeOK"


def test_unreachable_custom_sql_source_remains_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_live_source, "_host_resolves", lambda _server: False)

    rc, verdict = probe_live_source._probe_one(  # pylint: disable=protected-access
        tmp_path,
        [_source([{"name": "Q", "source_relation": "custom-sql", "custom_sql": "SELECT 1"}], fields=[])],
        0,
        1,
        False,
    )

    assert (rc, verdict) == (1, "UNREACHABLE")


@pytest.mark.parametrize("conn", [SNOWFLAKE, DATABRICKS, SQLSERVER], ids=["snowflake", "databricks", "sqlserver"])
@pytest.mark.parametrize("fields", [None, [], [{"kind": "column"}], [{"kind": "calculated", "internal_name": "[X]"}]])
def test_ordinary_source_without_enumerated_columns_is_resolvable(conn: dict, fields: list[dict] | None) -> None:
    """Source column discovery belongs to Power BI, not optional Tableau field enumeration."""
    source = _source([{"name": "REAL_TABLE", "custom_sql": None}], fields=[])
    source["connection"] = conn
    if fields is None:
        del source["fields"]
    else:
        source["fields"] = fields

    _, tables, column = probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access

    assert [table["name"] for table in tables] == ["REAL_TABLE"]
    assert column == "ProbeOK"


def test_ordinary_target_ignores_tableau_internal_field_names() -> None:
    """Even present Tableau fields do not provide a physical column identifier."""
    source = _source([{"name": "REAL_TABLE"}])
    _, _, column = probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access

    assert column == "ProbeOK"


@pytest.mark.parametrize("tables", [[], [{}], [{"name": ""}], [{"custom_sql": "SELECT 1"}]])
def test_source_without_a_named_table_or_custom_sql_relation_is_refused(tables: list[dict]) -> None:
    """Dropping field admission must not drop table admission or fabricate a source."""
    source = _source(tables)

    with pytest.raises(SystemExit) as raised:
        probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access

    assert raised.value.code == 1


@pytest.mark.parametrize(
    "text",
    [
        "[Expression.Error] The key didn't match any rows in the table.",
        "The key didn\u2019t match any rows in the table.",
    ],
)
def test_a_navigation_key_miss_classifies_as_bad_table_not_unclassified_error(text):
    verdict, _ = probe_live_source._classify_failure(text, False)  # pylint: disable=protected-access
    assert verdict == "BAD_TABLE", "a navigation miss proves the server answered - it is a spec error"


def test_real_tables_are_probed_before_custom_sql_relations():
    # Reachability is proven equally by either, but a custom SELECT can be arbitrarily expensive.
    source = _source(
        [
            {"name": "Q", "source_relation": "custom-sql", "custom_sql": "SELECT * FROM huge"},
            {"name": "REAL_TABLE", "custom_sql": None},
        ]
    )
    _, tables, _ = probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access
    assert [t["name"] for t in tables] == ["REAL_TABLE", "Q"]


def test_mixed_source_proves_credentials_and_custom_sql(tmp_path, caplog, monkeypatch):
    source = _source(
        [
            {"name": "Q", "source_relation": "custom-sql", "custom_sql": "SELECT * FROM huge.fact"},
            {"name": "REAL_TABLE", "custom_sql": None},
        ]
    )
    attempted = []

    def _probe_table(_migration: Path, _leg_name: str, _conn: dict, target: tuple[dict, str], _opts: tuple[int, bool]):
        table_spec, _column = target
        attempted.append(table_spec["name"])
        return 0, "DATA_OK"

    monkeypatch.setattr(probe_live_source, "_host_resolves", lambda _server: True)
    monkeypatch.setattr(probe_live_source, "_probe_one_table", _probe_table)
    caplog.set_level("INFO", logger="probe_live_source")

    rc, verdict = probe_live_source._probe_one(tmp_path, [source], 0, 7, False)  # pylint: disable=protected-access

    assert (rc, verdict) == (0, "DATA_OK")
    assert attempted == ["REAL_TABLE", "Q"]


def test_the_custom_sql_relation_is_still_probed_when_it_is_the_only_candidate():
    source = _source([{"name": "Q", "source_relation": "custom-sql", "custom_sql": "SELECT 1"}])
    _, tables, _ = probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access
    assert [t["name"] for t in tables] == ["Q"]
    assert tables[0]["custom_sql"] == "SELECT 1"
