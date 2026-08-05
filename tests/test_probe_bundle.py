"""Tests for `scripts/probe_bundle.py`.

The script had NO tests, which is how its central defect survived: the receipt asserted
"credential is bound in Power BI" and "a row can be returned" at file-rewrite time, having executed
nothing. `test_build_makes_no_connectivity_claim` is the regression guard for exactly that, and is
the most important test in this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_bundle  # noqa: E402  # pylint: disable=wrong-import-position

CONNECTIVITY_WORDS = ("credential", "connector resolves", "row can be returned", "readable")

PARTITION_TMDL = """\
table 'Sales'
\tcolumn 'Amount'
\t\tdataType: double
\t\tsourceColumn: Amount

\tpartition 'Sales' = m
\t\tmode: directQuery
\t\tsource =
\t\t\tlet
\t\t\t\tSource = Sql.Database(#"Server", #"Database"),
\t\t\t\tData = Source{[Schema="dbo",Item="Sales"]}[Data]
\t\t\tin
\t\t\t\tData
"""

EXPRESSIONS_TMDL = """\
expression 'Server' = "myserver.database.windows.net" meta [IsParameterQuery=true]
expression 'Database' = "sales" meta [IsParameterQuery=true]
"""


@pytest.fixture(name="bundle")
def bundle_fixture(tmp_path: Path) -> Path:
    """A minimal emitted bundle: one DirectQuery table plus its two M parameters."""
    model = tmp_path / "bundle" / "Demo.SemanticModel" / "definition"
    (model / "tables").mkdir(parents=True)
    (model / "tables" / "Sales.tmdl").write_text(PARTITION_TMDL, encoding="utf-8")
    (model / "expressions.tmdl").write_text(EXPRESSIONS_TMDL, encoding="utf-8")
    return tmp_path / "bundle"


def test_build_makes_no_connectivity_claim(bundle: Path, tmp_path: Path) -> None:
    """Building a probe copies and rewrites files. It must not claim anything about the source.

    This is the regression guard for the original defect. If it fails, the script is once again
    certifying a credential it never tested.
    """
    receipt = probe_bundle.build_probe(bundle, tmp_path / "probe", rows=1, keep_dax=False)

    assert receipt["status"] == probe_bundle.STATUS_BUILT
    assert not receipt["proves"]
    assert receipt["refresh"] is None

    blob = json.dumps(receipt["proves"]).lower()
    for word in CONNECTIVITY_WORDS:
        assert word not in blob, f"build-time receipt claims {word!r} without executing anything"

    # every connectivity claim must appear under does_not_prove instead
    negative = json.dumps(receipt["does_not_prove"]).lower()
    assert "credential is bound" in negative
    assert "a row can be returned" in negative


def test_build_wraps_partitions_and_forces_import(bundle: Path, tmp_path: Path) -> None:
    """A DirectQuery partition cannot be refreshed, so the probe flips it and limits it to 1 row."""
    receipt = probe_bundle.build_probe(bundle, tmp_path / "probe", rows=1, keep_dax=False)

    assert receipt["stats"]["partitions_wrapped"] == 1
    assert receipt["stats"]["dq_flipped"] == 1

    text = (tmp_path / "probe" / "Demo.SemanticModel" / "definition" / "tables" / "Sales.tmdl").read_text(
        encoding="utf-8"
    )
    assert "Table.FirstN(Data, 1) /*PROBE*/" in text
    assert "mode: directQuery" not in text


def test_recording_data_ok_is_what_grants_the_claim(bundle: Path, tmp_path: Path) -> None:
    """Only an executed DATA_OK refresh may write a connectivity claim into the receipt."""
    probe = tmp_path / "probe"
    probe_bundle.build_probe(bundle, probe, rows=1, keep_dax=False)

    receipt = probe_bundle.record_refresh_result(
        probe, probe_bundle.OUTCOME_DATA_OK, detail="1 row from Sales", elapsed_sec=12.34
    )

    assert receipt["status"] == probe_bundle.STATUS_EXECUTED
    assert receipt["refresh"]["outcome"] == "DATA_OK"
    assert receipt["refresh"]["elapsed_sec"] == 12.3
    blob = json.dumps(receipt["proves"]).lower()
    assert "credential is bound in power bi" in blob
    assert "at least one row can be returned" in blob
    # it must still disclaim the full load
    assert "full load" in json.dumps(receipt["does_not_prove"]).lower()


@pytest.mark.parametrize(
    "outcome",
    ["NO_DATA", "TIMEOUT", "CREDENTIAL_REQUIRED", "ERROR"],
)
def test_a_failed_refresh_proves_nothing(bundle: Path, tmp_path: Path, outcome: str) -> None:
    """Only DATA_OK supports a claim. A TIMEOUT in particular is uninformative, not a soft pass."""
    probe = tmp_path / "probe"
    probe_bundle.build_probe(bundle, probe, rows=1, keep_dax=False)

    receipt = probe_bundle.record_refresh_result(probe, outcome)

    assert receipt["proves"] == []
    assert receipt["refresh"]["outcome"] == outcome
    assert "nothing" in json.dumps(receipt["does_not_prove"]).lower()


def test_data_ok_is_downgraded_when_a_table_returned_no_rows(bundle: Path, tmp_path: Path) -> None:
    """A mixed model must not pass on the strength of the table that DID load.

    Regression guard for a measured 2.5-hour run that produced a full model, a 62 KB cache.abf and a
    green refresh while the Databricks warehouse never left STOPPED: the flat file had refreshed,
    both live tables were empty, and a model-level verdict called that success.
    """
    probe = tmp_path / "probe"
    probe_bundle.build_probe(bundle, probe, rows=1, keep_dax=False)

    receipt = probe_bundle.record_refresh_result(
        probe,
        probe_bundle.OUTCOME_DATA_OK,
        table_rows={"LocalCsv": 1, "DatabricksOrders": 0, "DatabricksShipment": 0},
    )

    refresh = receipt["refresh"]
    assert refresh["outcome"] == probe_bundle.OUTCOME_PARTIAL
    assert refresh["downgraded_from"] == probe_bundle.OUTCOME_DATA_OK
    assert refresh["tables_without_rows"] == ["DatabricksOrders", "DatabricksShipment"]

    # the claim must be per-table, and must NOT assert the unproven ones
    proves = json.dumps(receipt["proves"])
    assert "LocalCsv" in proves
    assert "DatabricksOrders" not in proves
    assert "credential" not in proves.lower()

    negative = json.dumps(receipt["does_not_prove"])
    assert "DatabricksOrders" in negative and "DatabricksShipment" in negative


def test_data_ok_survives_when_every_table_returned_rows(bundle: Path, tmp_path: Path) -> None:
    """The downgrade must not fire on a genuinely complete refresh."""
    probe = tmp_path / "probe"
    probe_bundle.build_probe(bundle, probe, rows=1, keep_dax=False)

    receipt = probe_bundle.record_refresh_result(
        probe, probe_bundle.OUTCOME_DATA_OK, table_rows={"Sales": 1, "Orders": 1}
    )

    assert receipt["refresh"]["outcome"] == probe_bundle.OUTCOME_DATA_OK
    assert receipt["refresh"]["downgraded_from"] is None
    assert "all 2 table(s)" in json.dumps(receipt["proves"])


DATE_TABLE_TMDL = """\
table Date
\tlineageTag: b8ad2503

\tcolumn Date
\t\tdataType: dateTime
\t\tsourceColumn: [Date]

\t\tannotation SummarizationSetBy = Automatic

\tcolumn Year = YEAR('Date'[Date])
\t\tdataType: int64
\t\tisDataTypeInferred

\t\tannotation SummarizationSetBy = Automatic

\tcolumn Quarter = "Q" & QUARTER('Date'[Date])
\t\tdataType: string

\t\tannotation SummarizationSetBy = Automatic

\thierarchy Calendar
\t\tlevel Year
\t\t\tcolumn: Year

\tpartition Date = m
\t\tmode: import
\t\tsource = let Source = Sql.Database("s", "d") in Source

\tannotation PBI_Id = Date
"""


DATE_TABLE_TMDL_CALCULATED = """\
table Date
\tlineageTag: b8ad2503
\tdataCategory: Time

\tcolumn Date
\t\tdataType: dateTime
\t\tsourceColumn: [Date]

\t\tannotation SummarizationSetBy = Automatic

\tcolumn Year = YEAR('Date'[Date])
\t\tdataType: int64

\t\tannotation SummarizationSetBy = Automatic

\thierarchy Calendar
\t\tlevel Year
\t\t\tcolumn: Year

\tpartition Date = calculated
\t\tmode: import
\t\tsource = CALENDAR(DATE(2015, 1, 1), DATE(2035, 12, 31))

\tannotation PBI_Id = Date
"""


def test_stripping_dax_does_not_orphan_annotations() -> None:
    """Regression guard: Power BI Desktop REFUSES to open a project with duplicated annotations.

    Measured 2026-08-05 against Desktop 2.157 on a real Databricks bundle - stripping 11 calculated
    columns left all 12 `SummarizationSetBy` annotations stacked on the first column and the project
    would not open:

        TMDL objects cannot be merged because both declare the same property: value

    Every static check passed while this was broken, so only a test at this level catches it.
    """
    stripped, count = probe_bundle.strip_dax_objects(DATE_TABLE_TMDL)

    assert count == 2, "both calculated columns should be removed"

    # the source column and its single annotation survive, exactly once each
    assert stripped.count("column Date\n") == 1
    assert stripped.count("annotation SummarizationSetBy") == 1, (
        f"orphaned annotations would crash Desktop:\n{stripped}"
    )

    # the calculated columns and their DAX are gone
    assert "YEAR(" not in stripped
    assert "QUARTER(" not in stripped

    # structures that are NOT dax objects are untouched
    assert "partition Date = m" in stripped
    assert "annotation PBI_Id = Date" in stripped, "a TABLE-level annotation must not be swallowed"
    # `hierarchy Calendar`'s only level pointed at the stripped `Year`, so it is correctly removed
    assert "hierarchy Calendar" not in stripped


def test_stripping_the_last_object_keeps_the_table_annotation() -> None:
    """A calculated column at the END of a table must not consume the table's own annotation.

    This is the case a naive 'also swallow annotations' fix breaks: the table annotation sits at the
    SAME indent as the column, so only an indentation-aware rule keeps it.
    """
    tmdl = "table T\n\tcolumn C = 1 + 1\n\t\tdataType: int64\n\n\tannotation PBI_Id = T\n"

    stripped, count = probe_bundle.strip_dax_objects(tmdl)

    assert count == 1
    assert "column C" not in stripped
    assert "annotation PBI_Id = T" in stripped


def test_calculated_table_columns_are_not_stripped() -> None:
    """A calculated table's columns ARE the table - and its hierarchy depends on them.

    Regression guard for Desktop 2.157 refusing to open with:
        Property Column of object "level Year in hierarchy Calendar in table Date" refers to an
        object which cannot be found
    """
    stripped, count = probe_bundle.strip_dax_objects(DATE_TABLE_TMDL_CALCULATED)

    assert count == 0, "nothing should be stripped from a calculated table"
    assert "column Year = YEAR" in stripped
    assert "hierarchy Calendar" in stripped
    assert "level Year" in stripped


def test_dangling_hierarchy_levels_are_repaired() -> None:
    """On an M-backed table a stripped column must not leave a hierarchy level pointing at it."""
    tmdl = (
        "table Sales\n"
        "\tcolumn Region\n"
        "\t\tsourceColumn: Region\n"
        "\n"
        "\tcolumn Year = YEAR('Sales'[Date])\n"
        "\t\tdataType: int64\n"
        "\n"
        "\thierarchy Geo\n"
        "\t\tlevel Region\n"
        "\t\t\tcolumn: Region\n"
        "\n"
        "\t\tlevel Year\n"
        "\t\t\tcolumn: Year\n"
        "\n"
        "\tpartition Sales = m\n"
        "\t\tmode: import\n"
        "\t\tsource = let Source = 1 in Source\n"
    )

    stripped, count = probe_bundle.strip_dax_objects(tmdl)

    assert count == 1
    assert "column Year = YEAR" not in stripped
    assert "level Year" not in stripped, "the level referencing the removed column must go"
    assert "level Region" in stripped, "levels on surviving columns must stay"
    assert "hierarchy Geo" in stripped


def test_a_hierarchy_that_loses_every_level_is_removed() -> None:
    """An empty hierarchy is not valid TMDL, so it must go with its last level."""
    tmdl = (
        "table Sales\n"
        "\tcolumn Year = YEAR('Sales'[Date])\n"
        "\t\tdataType: int64\n"
        "\n"
        "\thierarchy Geo\n"
        "\t\tlevel Year\n"
        "\t\t\tcolumn: Year\n"
        "\n"
        "\tpartition Sales = m\n"
        "\t\tmode: import\n"
        "\t\tsource = let Source = 1 in Source\n"
    )

    stripped, _ = probe_bundle.strip_dax_objects(tmdl)

    assert "hierarchy Geo" not in stripped
    assert "level Year" not in stripped
    assert "partition Sales = m" in stripped


def test_sort_by_column_referencing_a_removed_column_is_dropped() -> None:
    """`sortByColumn` is the other reference that dangles after a strip."""
    tmdl = (
        "table Sales\n"
        "\tcolumn MonthName\n"
        "\t\tsourceColumn: MonthName\n"
        "\t\tsortByColumn: MonthNo\n"
        "\n"
        "\tcolumn MonthNo = MONTH('Sales'[Date])\n"
        "\t\tdataType: int64\n"
        "\n"
        "\tpartition Sales = m\n"
        "\t\tmode: import\n"
        "\t\tsource = let Source = 1 in Source\n"
    )

    stripped, count = probe_bundle.strip_dax_objects(tmdl)

    assert count == 1
    assert "sortByColumn: MonthNo" not in stripped
    assert "column MonthName" in stripped


def test_empty_m_parameter_is_detected(bundle: Path) -> None:
    """A parameter that is DEFINED but blank is exactly as fatal as a missing one.

    Measured 2026-08-05 on a real Snowflake `.tdsx` whose `<connection warehouse=''>` was blank.
    The emitter honestly wrote `expression Warehouse = ""` with a TODO; our check looked only for
    the NAME and reported "all referenced parameters are defined", exit 0 - a false green on a
    bundle that provably cannot refresh.
    """
    expressions = bundle / "Demo.SemanticModel" / "definition" / "expressions.tmdl"
    expressions.write_text(
        "expression 'Server' = \"s\" meta [IsParameterQuery=true]\n"
        "expression 'Database' = \"\" meta [IsParameterQuery=true, IsParameterQueryRequired=true]\n",
        encoding="utf-8",
    )

    params = probe_bundle.check_m_parameters(probe_bundle.find_model_dir(bundle))

    assert params["undefined"] == [], "the name IS defined - that is the trap"
    assert params["empty"] == ["Database"]


def test_a_populated_parameter_is_not_flagged_empty(bundle: Path) -> None:
    """Guard the other direction, so a normal bundle is not blocked."""
    params = probe_bundle.check_m_parameters(probe_bundle.find_model_dir(bundle))

    assert params["empty"] == []
    assert params["undefined"] == []


def test_unknown_outcome_is_rejected(bundle: Path, tmp_path: Path) -> None:
    """An unrecognised outcome must raise rather than silently write an unclassified receipt."""
    probe = tmp_path / "probe"
    probe_bundle.build_probe(bundle, probe, rows=1, keep_dax=False)
    with pytest.raises(ValueError, match="unknown outcome"):
        probe_bundle.record_refresh_result(probe, "PROBABLY_FINE")


def test_recording_requires_a_probe_variant(tmp_path: Path) -> None:
    """Guard against recording a result against a directory that was never built as a probe."""
    plain = tmp_path / "not-a-probe"
    plain.mkdir()
    with pytest.raises(FileNotFoundError):
        probe_bundle.record_refresh_result(plain, probe_bundle.OUTCOME_DATA_OK)


def test_unwrap_restores_the_original_expression(bundle: Path, tmp_path: Path) -> None:
    """The probe must be exactly reversible, or a probe bundle could ship with one row per table."""
    probe = tmp_path / "probe"
    probe_bundle.build_probe(bundle, probe, rows=1, keep_dax=False)
    tmdl = probe / "Demo.SemanticModel" / "definition" / "tables" / "Sales.tmdl"

    restored, count = probe_bundle.unwrap_partitions(tmdl.read_text(encoding="utf-8"))

    assert count == 1
    assert probe_bundle.PROBE_MARKER not in restored
    assert "Table.FirstN" not in restored
    assert restored.rstrip().endswith("Data")


def test_undefined_m_parameter_is_detected(bundle: Path) -> None:
    """The static check that neither `validate` nor the emitter's own self-check performs."""
    expressions = bundle / "Demo.SemanticModel" / "definition" / "expressions.tmdl"
    expressions.write_text("expression 'Server' = \"x\" meta [IsParameterQuery=true]\n", encoding="utf-8")

    params = probe_bundle.check_m_parameters(probe_bundle.find_model_dir(bundle))

    assert params["undefined"] == ["Database"]
