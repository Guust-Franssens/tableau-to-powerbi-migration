"""Regression tests for the custom-SQL probe path in `scripts/probe_live_source.py`.

A Tableau relation of `type='text'` is a hand-written SELECT that Tableau merely NAMES (e.g.
`Flight_Level_Query`). The parser records that as `custom_sql` on the table. That query is too
expensive and modal-prone to run automatically, so the probe must write the PBIP scaffold and return
a distinct non-zero OPERATOR_REQUIRED verdict instead of claiming DATA_OK or SKIPPED.

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


def _source(tables: list[dict]) -> dict:
    return {
        "connection": SNOWFLAKE,
        "tables": tables,
        "fields": [{"kind": "column", "internal_name": "[Col]"}],
    }


def test_custom_sql_scaffold_contains_the_sql_but_not_a_one_row_automatic_probe():
    m, note = probe_live_source.build_m_query(
        SNOWFLAKE, "Flight_Level_Query", "Col", custom_sql="SELECT a, b FROM raw.flights"
    )
    assert "Value.NativeQuery" in m
    assert "Table.FirstN(Value.NativeQuery" not in m
    assert 'Kind="Table"' not in m, "a custom-SQL relation has no table to navigate to"
    assert "SELECT a, b FROM raw.flights" in m
    assert "custom SQL" in note, "the operator must be told which path was scaffolded"


def test_databricks_custom_sql_scaffold_uses_native_query_without_automatic_probe():
    m, note = probe_live_source.build_m_query(
        DATABRICKS, "Flight_Level_Query", "Col", custom_sql="SELECT a, b FROM raw.flights"
    )
    assert "Databricks.Catalogs" in m
    assert "Value.NativeQuery" in m
    assert "Table.FirstN(Value.NativeQuery" not in m
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


def test_custom_sql_probe_writes_pbip_then_requires_desktop_operator(tmp_path, caplog, monkeypatch):
    def _unexpected_open(_pbip: Path) -> int:
        raise AssertionError("custom SQL must not be opened/refreshed automatically")

    monkeypatch.setattr(probe_live_source, "_open_desktop", _unexpected_open)
    caplog.set_level("INFO", logger="probe_live_source")

    rc, verdict = probe_live_source._probe_one_table(  # pylint: disable=protected-access
        tmp_path,
        SNOWFLAKE,
        ({"name": "Flight_Level_Query", "custom_sql": "SELECT a FROM raw.flights"}, "Col"),
        (1, False),
    )

    assert rc == probe_live_source.EXIT_OPERATOR_REQUIRED
    assert rc != 0
    assert verdict == "OPERATOR_REQUIRED"
    pbip = next(tmp_path.glob("_probe/run-*/Probe.pbip"))
    assert pbip.exists(), "operator handoff must include the probe PBIP"
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert f"PROBE: OPERATOR_REQUIRED {pbip.parent}" in messages
    assert f"Open {pbip} in Power BI Desktop" in messages
    assert "do NOT use a SQL client" in messages
    assert "Custom SQL refresh WILL run the full customer query" in messages
    assert "not a cheap row probe" in messages
    assert "isolated to one table and no report layer" in messages
    assert "native-query approval modal looks like a credential failure" in messages
    assert "gate stays armed until the operator reports the result" in messages
    assert "DBeaver" in messages and "Snowsight" in messages and "SSMS" in messages


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


def test_mixed_source_proves_credentials_but_still_requires_operator(tmp_path, caplog, monkeypatch):
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
        if table_spec.get("custom_sql"):
            return probe_live_source.EXIT_OPERATOR_REQUIRED, "OPERATOR_REQUIRED"
        return 0, "DATA_OK"

    monkeypatch.setattr(probe_live_source, "_host_resolves", lambda _server: True)
    monkeypatch.setattr(probe_live_source, "_probe_one_table", _probe_table)
    caplog.set_level("INFO", logger="probe_live_source")

    rc, verdict = probe_live_source._probe_one(tmp_path, [source], 0, 7, False)  # pylint: disable=protected-access

    assert rc == probe_live_source.EXIT_OPERATOR_REQUIRED
    assert verdict == "OPERATOR_REQUIRED"
    assert attempted == ["REAL_TABLE", "Q"]
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "source credentials were proven by table 'REAL_TABLE'" in messages
    assert "custom-SQL relation(s) still require Power BI Desktop operator refresh" in messages


def test_the_custom_sql_relation_is_still_probed_when_it_is_the_only_candidate():
    source = _source([{"name": "Q", "custom_sql": "SELECT 1"}])
    _, tables, _ = probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access
    assert [t["name"] for t in tables] == ["Q"]
    assert tables[0]["custom_sql"] == "SELECT 1"
