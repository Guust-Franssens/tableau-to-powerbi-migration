"""Regression tests for the custom-SQL probe path in `scripts/probe_live_source.py`.

A Tableau relation of `type='text'` is a hand-written SELECT that Tableau merely NAMES (e.g.
`Flight_Level_Query`). The parser records that as `custom_sql` on the table, but the probe used to
discard everything except the name and then navigate `{[Name="Flight_Level_Query",Kind="Table"]}`
against a source where no such table exists. That failed 100% of the time, for a reason that had
nothing to do with reachability - the exact misdiagnosis class this script exists to prevent.

Two independent defects are covered here, because fixing either alone still leaves a bad outcome:

* the M query has to actually RUN the SQL (otherwise the probe cannot succeed at all), and
* `The key didn't match any rows in the table.` has to classify as BAD_TABLE (otherwise every
  navigation miss - custom SQL or a plain typo - lands in the unclassified-ERROR bucket, which
  tells the reader nothing about what to fix).
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


def _source(tables: list[dict]) -> dict:
    return {
        "connection": SNOWFLAKE,
        "tables": tables,
        "fields": [{"kind": "column", "internal_name": "[Col]"}],
    }


def test_custom_sql_runs_the_sql_instead_of_navigating_to_a_table_that_cannot_exist():
    m, note = probe_live_source.build_m_query(
        SNOWFLAKE, "Flight_Level_Query", "Col", custom_sql="SELECT a, b FROM raw.flights"
    )
    assert "Value.NativeQuery" in m
    assert 'Kind="Table"' not in m, "a custom-SQL relation has no table to navigate to"
    assert "SELECT a, b FROM raw.flights" in m
    assert "custom SQL" in note, "the operator must be told which path ran"


def test_a_real_table_still_navigates_and_is_unchanged_by_the_custom_sql_work():
    m, note = probe_live_source.build_m_query(SNOWFLAKE, "FLIGHTS", "Col")
    assert 'sch{[Name="FLIGHTS",Kind="Table"]}[Data]' in m
    assert "Value.NativeQuery" not in m
    assert "custom SQL" not in note


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


def test_the_custom_sql_relation_is_still_probed_when_it_is_the_only_candidate():
    source = _source([{"name": "Q", "custom_sql": "SELECT 1"}])
    _, tables, _ = probe_live_source._resolve_probe_target([source], 0)  # pylint: disable=protected-access
    assert [t["name"] for t in tables] == ["Q"]
    assert tables[0]["custom_sql"] == "SELECT 1"
