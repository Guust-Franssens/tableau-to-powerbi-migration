"""Regression tests for the custom-SQL probe path in `scripts/probe_live_source.py`.

A Tableau relation of `type='text'` is a hand-written SELECT that Tableau merely NAMES (e.g.
`Flight_Level_Query`). The parser records that as `custom_sql` on the table. The probe runs that query
and projects a known constant, so it does not depend on optional enumerated columns.

Ordinary table navigation remains covered here because that proven cheap path must not move while the
custom-SQL path changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import probe_live_source  # noqa: E402  # pylint: disable=wrong-import-position

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


def _source(tables: list[dict], fields: list[dict] | None = None) -> dict:
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
    assert '"ProbeOK"' in m
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
    assert '"ProbeOK"' in m
    assert 'Kind="Schema"' not in m
    assert "SELECT a, b FROM raw.flights" in m
    assert "custom SQL" in note


def test_a_real_table_still_navigates_and_is_unchanged_by_the_custom_sql_work():
    m, note = probe_live_source.build_m_query(SNOWFLAKE, "FLIGHTS", "Col")
    expected = (
        "let\n"
        '    Source = Snowflake.Databases("ORG-ACCOUNT.snowflakecomputing.com", "WH", null),\n'
        '    db = Source{[Name="DB",Kind="Database"]}[Data],\n'
        '    sch = db{[Name="PUBLIC",Kind="Schema"]}[Data],\n'
        '    tbl = sch{[Name="FLIGHTS",Kind="Table"]}[Data],\n'
        '    one = Table.FirstN(Table.SelectColumns(tbl, {"Col"}), 1)\n'
        "in\n"
        "    one"
    )
    assert m == expected
    assert "Value.NativeQuery" not in m
    assert "custom SQL" not in note


def test_real_table_probe_still_opens_refreshes_and_returns_data_ok(tmp_path, monkeypatch):
    events = []

    def _open(pbip: Path) -> int:
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
        SNOWFLAKE,
        ({"name": "Flight_Level_Query", "custom_sql": "SELECT a FROM raw.flights"}, "ProbeOK"),
        (1, False),
    )

    assert (rc, verdict) == (0, "DATA_OK")
    pbip = next(tmp_path.glob("_probe/run-*/Probe.pbip"))
    assert pbip.exists(), "operator handoff must include the probe PBIP"
    tmdl = next(pbip.parent.glob("*.SemanticModel/definition/tables/*.tmdl")).read_text()
    assert "column 'ProbeOK'" in tmdl
    assert "sourceColumn: ProbeOK" in tmdl


def test_custom_sql_without_enumerated_columns_is_resolvable():
    source = _source([{"name": "Q", "custom_sql": "SELECT 1"}], fields=[])

    _, tables, column = probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access

    assert [table["name"] for table in tables] == ["Q"]
    assert column == "ProbeOK"


def test_unreachable_custom_sql_source_remains_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_live_source, "_host_resolves", lambda _server: False)

    rc, verdict = probe_live_source._probe_one(  # pylint: disable=protected-access
        tmp_path, [_source([{"name": "Q", "custom_sql": "SELECT 1"}], fields=[])], 0, 1, False
    )

    assert (rc, verdict) == (1, "UNREACHABLE")


def test_ordinary_source_without_columns_is_cannot_assess():
    source = _source([{"name": "REAL_TABLE", "custom_sql": None}], fields=[])

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
            {"name": "Q", "custom_sql": "SELECT * FROM huge"},
            {"name": "REAL_TABLE", "custom_sql": None},
        ]
    )
    _, tables, _ = probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access
    assert [t["name"] for t in tables] == ["REAL_TABLE", "Q"]


def test_mixed_source_proves_credentials_and_custom_sql(tmp_path, caplog, monkeypatch):
    source = _source(
        [
            {"name": "Q", "custom_sql": "SELECT * FROM huge.fact"},
            {"name": "REAL_TABLE", "custom_sql": None},
        ]
    )
    attempted = []

    def _probe_table(_migration: Path, _conn: dict, target: tuple[dict, str], _opts: tuple[int, bool]):
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
    source = _source([{"name": "Q", "custom_sql": "SELECT 1"}])
    _, tables, _ = probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access
    assert [t["name"] for t in tables] == ["Q"]
    assert tables[0]["custom_sql"] == "SELECT 1"
