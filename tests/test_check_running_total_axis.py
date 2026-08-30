"""Tests for scripts/check_running_total_axis.py - the cross-artifact running-total gate (#218).

The centrepiece is `test_s14_measured_table`, which reproduces the MEASURED table recorded in
`.github/skills/powerbi-semantic-model-gotchas/SKILL.md` section 4 as a fixture. One emitted measure
ordered by `'Orders'[Order_Date]`, three axes, three known outcomes:

    | visual axis              | emitted measure returned      | verdict                 |
    |--------------------------|-------------------------------|-------------------------|
    | `'Orders'[Order_Date]`   | 16.45 -> ... -> 2,297,200.86  | cumulative              |
    | `'Date'[Date]`           | 16.45, 288.06, 19.54 ...      | each day's own total    |
    | `'Date'[Month Start]`    | 14,236.89, 4,519.89 ...       | each month's own total  |

Everything else in this file exists to keep the gate from earning that catch by being trigger-happy:
the committed `examples/` corpus is asserted clean, and every deliberate non-flag has a test that
would fail if it started firing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_running_total_axis as crta  # noqa: E402  # pylint: disable=wrong-import-position
import dax_grain as dg  # noqa: E402  # pylint: disable=wrong-import-position

REPO_ROOT = Path(__file__).resolve().parents[1]

ORDERS_TMDL = """table Orders

	column Order_Date
		dataType: dateTime

	column Order_Month
		dataType: dateTime

	column 'Order Month Label' = FORMAT('Orders'[Order_Date], "yyyy-MM")

	column 'Order Quarter No' = QUARTER('Orders'[Order_Date])

	column 'Order Quarter' = "Q" & 'Orders'[Order Quarter No]

	column 'Fiscal Period' = 1

	column Region
		dataType: string

	column Sales
		dataType: double
"""

DATE_TMDL = """table Date
	dataCategory: Time

	column Date
		dataType: dateTime

	column 'Month Start'
		dataType: dateTime
"""


def _measures_tmdl(*declarations: str) -> str:
    return "table _Measures\n\n" + "\n\n".join(declarations) + "\n"


def _measure(name: str, expression: str, annotations: dict[str, str] | None = None) -> str:
    lines = [f"\tmeasure '{name}' = {expression}", "\t\tlineageTag: 0000-test"]
    for key, value in (annotations or {}).items():
        lines.append(f"\t\tannotation {key} = {value}")
    return "\n".join(lines)


def _column_projection(entity: str, prop: str) -> dict:
    return {
        "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}",
    }


def _measure_projection(entity: str, prop: str) -> dict:
    return {
        "field": {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}",
    }


def _hierarchy_projection(entity: str, hierarchy: str, level: str) -> dict:
    return {
        "field": {
            "HierarchyLevel": {
                "Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": entity}}, "Hierarchy": hierarchy}},
                "Level": level,
            }
        },
        "queryRef": f"{entity}.{hierarchy}.{level}",
    }


def build_bundle(
    tmp_path: Path,
    measures_tmdl: str,
    query_state: dict,
    *,
    visual_type: str = "lineChart",
    tables: tuple[str, ...] = (ORDERS_TMDL, DATE_TMDL),
) -> Path:
    """A minimal but REAL bundle: `pbip/` shape, `definition.pbir` byPath, one page, one visual."""
    unit = tmp_path / "bundle" / "pbip" / "WB"
    model = unit / "WB.SemanticModel" / "definition" / "tables"
    model.mkdir(parents=True)
    for index, table in enumerate(tables):
        (model / f"t{index}.tmdl").write_text(table, encoding="utf-8")
    (model / "_Measures.tmdl").write_text(measures_tmdl, encoding="utf-8")

    report = unit / "WB.Report"
    (report / "definition" / "pages" / "page1" / "visuals" / "v1").mkdir(parents=True)
    (report / "definition.pbir").write_text(
        json.dumps({"datasetReference": {"byPath": {"path": "../WB.SemanticModel"}}}), encoding="utf-8"
    )
    (report / "definition" / "pages" / "page1" / "visuals" / "v1" / "visual.json").write_text(
        json.dumps({"name": "v1", "visual": {"visualType": visual_type, "query": {"queryState": query_state}}}),
        encoding="utf-8",
    )
    return tmp_path / "bundle"


def verdicts(report: dict) -> list[str]:
    return [finding["verdict"] for pair in report["pairs"] for finding in pair["findings"]]


def codes(report: dict) -> list[str]:
    return [finding["code"] for pair in report["pairs"] for finding in pair["findings"]]


# --------------------------------------------------------------------------------------------
# The measured table, reproduced
# --------------------------------------------------------------------------------------------

WINDOW_MEASURE = _measures_tmdl(
    _measure(
        "Running Sales",
        "SUMX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC)), CALCULATE(SUM('Orders'[Sales])))",
    )
)


@pytest.mark.parametrize(
    ("axis_entity", "axis_property", "expected"),
    [
        ("Orders", "Order_Date", "ok"),
        ("Date", "Date", "mismatch"),
        ("Date", "Month Start", "mismatch"),
    ],
)
def test_s14_measured_table(tmp_path: Path, axis_entity: str, axis_property: str, expected: str) -> None:
    """The three measured axes, against one measure ordered by `'Orders'[Order_Date]`."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {
            "Category": {"projections": [_column_projection(axis_entity, axis_property)]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == [expected]
    assert report["status"] == (crta.STATUS_MISMATCH if expected == "mismatch" else crta.STATUS_OK)


def test_mismatch_names_the_ordered_column_and_the_axis(tmp_path: Path) -> None:
    """The finding has to be actionable without opening either file."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    finding = crta.scan(bundle)["pairs"][0]["findings"][0]
    assert finding["code"] == "orderby_not_on_axis"
    assert "'Orders'[Order_Date]" in finding["detail"]
    assert "'Date'[Month Start]" in finding["detail"]
    assert finding["measure"] == "'_Measures'[Running Sales]"
    assert finding["line"] > 0


# --------------------------------------------------------------------------------------------
# Window family: the deliberate acquittals
# --------------------------------------------------------------------------------------------


def test_partitionby_legend_is_not_a_mismatch(tmp_path: Path) -> None:
    """PARTITIONBY on a legend column is the intended shape, not a disagreement."""
    measures = _measures_tmdl(
        _measure(
            "Running By Region",
            "SUMX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC), "
            "PARTITIONBY('Orders'[Region])), CALCULATE(SUM('Orders'[Sales])))",
        )
    )
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Date")]},
            "Series": {"projections": [_column_projection("Orders", "Region")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running By Region")]},
        },
    )
    assert verdicts(crta.scan(bundle)) == ["ok"]


def test_explicit_relation_is_unassessable_not_a_mismatch(tmp_path: Path) -> None:
    """With a relation argument the visual no longer decides the ordering domain."""
    measures = _measures_tmdl(
        _measure(
            "Running Sales",
            "SUMX(WINDOW(1, ABS, 0, REL, ALLSELECTED('Orders'), ORDERBY('Orders'[Order_Date], ASC)), "
            "CALCULATE(SUM('Orders'[Sales])))",
        )
    )
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["unassessable"]
    assert report["status"] == crta.STATUS_UNASSESSABLE


def test_no_orderby_orders_by_the_visual_grain(tmp_path: Path) -> None:
    """Without ORDERBY the window follows the relation - which is the visual - so it cannot disagree."""
    measures = _measures_tmdl(_measure("Prev", "SUMX(OFFSET(-1), CALCULATE(SUM('Orders'[Sales])))"))
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "Prev")]},
        },
    )
    assert codes(crta.scan(bundle)) == ["orders_by_visual_grain"]


def test_unqualified_orderby_column_is_unassessable(tmp_path: Path) -> None:
    """A bare `[Col]` names no table, so "is it projected?" has no honest answer."""
    measures = _measures_tmdl(
        _measure(
            "Running Sales", "SUMX(WINDOW(1, ABS, 0, REL, ORDERBY([Order_Date], ASC)), CALCULATE(SUM('Orders'[Sales])))"
        )
    )
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    assert verdicts(crta.scan(bundle)) == ["unassessable"]


def test_measure_only_visual_is_unassessable(tmp_path: Path) -> None:
    """A card projects no grouping column; "the ordered column is absent" says nothing there."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {"Data": {"projections": [_measure_projection("_Measures", "Running Sales")]}},
        visual_type="cardVisual",
    )
    assert codes(crta.scan(bundle)) == ["no_grouping_column"]


def test_hierarchy_projection_is_unassessable(tmp_path: Path) -> None:
    """A hierarchy level may expand to the ordered column, so the absence is not proof."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {
            "Category": {
                "projections": [
                    _column_projection("Orders", "Region"),
                    _hierarchy_projection("Date", "Calendar", "Month"),
                ]
            },
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    assert codes(crta.scan(bundle)) == ["hierarchy_projection"]


def test_ordered_column_in_a_non_axis_role_still_acquits(tmp_path: Path) -> None:
    """The window relation is the whole shaped table, so a tooltip column orders it too."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {
            "Category": {"projections": [_column_projection("Orders", "Region")]},
            "Tooltips": {"projections": [_column_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    assert verdicts(crta.scan(bundle)) == ["ok"]


def test_case_only_difference_does_not_invent_a_mismatch(tmp_path: Path) -> None:
    """PBIR and TMDL can disagree on casing; that is a rename symptom `check_field_bindings.py`
    already reports as a near-miss, not evidence that the addressed grain is absent."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {
            "Category": {"projections": [_column_projection("orders", "order_date")]},
            "Y": {"projections": [_measure_projection("_measures", "running sales")]},
        },
    )
    assert verdicts(crta.scan(bundle)) == ["ok"]


# --------------------------------------------------------------------------------------------
# As-of filter family
# --------------------------------------------------------------------------------------------

AS_OF = "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), 'Orders'[Order_Date] <= MAX('Orders'[Order_Date])))"


def _as_of_bundle(tmp_path: Path, axis_entity: str, axis_property: str, expression: str = AS_OF) -> Path:
    return build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Running Sales", expression)),
        {
            "Category": {"projections": [_column_projection(axis_entity, axis_property)]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )


def test_as_of_axis_is_the_compared_column(tmp_path: Path) -> None:
    """CALCULATE overwrites the compared column's filter, so that axis is correct."""
    assert codes(crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Date"))) == ["axis_cleared"]


def test_as_of_coarser_same_table_date_axis_is_a_mismatch(tmp_path: Path) -> None:
    """The measured mechanism: an uncleared same-table date bin survives and truncates the rows."""
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month"))
    assert verdicts(report) == ["mismatch"]
    assert codes(report) == ["axis_grain_not_cleared"]


def test_as_of_clearing_the_coarser_column_too_is_clean(tmp_path: Path) -> None:
    """The documented fix - clear every same-table date-ish column the visual can filter."""
    fixed = (
        "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date], 'Orders'[Order_Month]), "
        "'Orders'[Order_Date] <= MAX('Orders'[Order_Date])))"
    )
    assert codes(crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", fixed))) == ["axis_cleared"]


def test_as_of_non_date_axis_is_deliberately_not_flagged(tmp_path: Path) -> None:
    """A running total partitioned by Region is an ordinary shape, and the decision is recorded."""
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Region"))
    assert verdicts(report) == ["ok"]
    finding = report["pairs"][0]["findings"][0]
    assert finding["code"] == "axis_not_a_date_grain"
    assert finding["not_flagged"] == ["'Orders'[Region]"]


def test_as_of_cross_table_axis_is_unassessable(tmp_path: Path) -> None:
    """Whether a Date-table filter reaches Orders depends on the relationship graph."""
    assert codes(crta.scan(_as_of_bundle(tmp_path, "Date", "Month Start"))) == ["cross_table_axis"]


# --- review findings #1-#3: the proxies that used to decide "safe" --------------------------


@pytest.mark.parametrize("role", ["Category", "Rows", "Columns", "Series", "Tooltips"])
def test_every_projected_column_is_examined_not_a_curated_axis_list(tmp_path: Path, role: str) -> None:
    """Finding 1. A `dateTime` bin under the pivotTable's real `Columns` role used to be INVISIBLE:
    the axis-role list omitted it, the survivor list came back empty, and empty was read as
    "the axis is cleared". Measured on the estate's Section 12 pivot - exit 1 under `Rows`, exit 0
    under `Columns`, same measure, same column. Every projected column groups the query."""
    bundle = build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Running Sales", AS_OF)),
        {
            role: {"projections": [_column_projection("Orders", "Order_Month")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
        visual_type="pivotTable",
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["mismatch"], crta.render(report)


def test_as_of_on_a_measure_only_visual_is_unassessable(tmp_path: Path) -> None:
    """Finding 1, second half. A card has no grouping column to clear, so "cleared" is not a fact
    about it. This returned `ok`/exit 0 while the module's own contract promised exit 3."""
    bundle = build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Running Sales", AS_OF)),
        {"Data": {"projections": [_measure_projection("_Measures", "Running Sales")]}},
        visual_type="cardVisual",
    )
    report = crta.scan(bundle)
    assert codes(report) == ["no_grouping_column"]
    assert report["status"] == crta.STATUS_UNASSESSABLE


def test_as_of_with_a_hierarchy_projection_is_unassessable(tmp_path: Path) -> None:
    """A hierarchy level may expand to a date grain, so a clean as-of verdict is not honest."""
    bundle = build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Running Sales", AS_OF)),
        {
            "Category": {
                "projections": [
                    _column_projection("Orders", "Order_Date"),
                    _hierarchy_projection("Date", "Calendar", "Month"),
                ]
            },
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    assert codes(crta.scan(bundle)) == ["hierarchy_projection"]


@pytest.mark.parametrize("column", ["Order Month Label", "Order Quarter"])
def test_date_bins_derived_by_calculation_are_flagged_whatever_their_type(tmp_path: Path, column: str) -> None:
    """Finding 2. The engine writes its coarse grains as TEXT calculated columns -
    `Month = FORMAT('Date'[Date], "MMM")`, `Quarter = "Q" & QUARTER(...)` - carrying no `dataType`
    at all (95 such columns in the 2026-08-29 estate). Their filters survive exactly like a
    `dateTime` bin's, so lineage decides, not the declared scalar type. `Order Quarter` also proves
    the chain is followed transitively (via `Order Quarter No`)."""
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", column))
    assert verdicts(report) == ["mismatch"], crta.render(report)
    assert codes(report) == ["axis_grain_not_cleared"]


def test_a_date_named_column_with_no_proof_is_unassessable_not_clean(tmp_path: Path) -> None:
    """A name is not evidence enough to fail a build, but it is too much to wave through."""
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Fiscal Period"))
    assert codes(report) == ["axis_grain_unresolved"]
    assert report["status"] == crta.STATUS_UNASSESSABLE


def test_a_pinned_cutoff_is_not_a_running_total(tmp_path: Path) -> None:
    """Finding 3, a FALSE POSITIVE. `<= DATE(2024,12,31)` is an ordinary "sales through cutoff"
    measure whose per-bucket totals are INTENDED. Reading only the `<=` operator blocked it."""
    fixed = (
        "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), 'Orders'[Order_Date] <= DATE(2024, 12, 31)))"
    )
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", fixed))
    assert report["status"] == crta.STATUS_NOT_APPLICABLE, crta.render(report)
    assert report["mismatches"] == 0


def test_an_as_of_bound_hoisted_into_a_var_is_still_a_running_total(tmp_path: Path) -> None:
    """The documented fix for the filter form hoists the as-of date into a VAR; following it is what
    keeps the pinned-cutoff exclusion from also excusing the real thing."""
    hoisted = (
        "VAR _asOf = MAX('Orders'[Order_Date]) "
        "RETURN CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
        "'Orders'[Order_Date] <= _asOf))"
    )
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", hoisted))
    assert verdicts(report) == ["mismatch"], crta.render(report)


def test_an_unresolvable_as_of_bound_is_unassessable(tmp_path: Path) -> None:
    """`<= [As Of Date]` may well be a running total; nothing static proves it either way."""
    by_measure = (
        "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), 'Orders'[Order_Date] <= [As Of Date]))"
    )
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", by_measure))
    assert verdicts(report) == ["unassessable"], crta.render(report)


def test_a_grouping_column_is_never_used_as_a_dict_key(tmp_path: Path) -> None:
    """Regression: grading survivors in a dict keyed by `check_field_bindings.FieldRef` raised
    `TypeError: unhashable type` at runtime. A crash exits 1 - indistinguishable from a mismatch to
    anything reading only the exit code, and the reproduction harness scored it as a pass."""
    bundle = build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Running Sales", AS_OF)),
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Month")]},
            "Rows": {"projections": [_column_projection("Orders", "Order Quarter")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["mismatch"]
    assert "Order_Month" in report["pairs"][0]["findings"][0]["detail"]


# --- review finding #4: every window call, not the first ------------------------------------


def test_every_window_call_is_assessed_not_just_the_first(tmp_path: Path) -> None:
    """Finding 4. `_classify_window` returned from inside the first call site, so a second
    `WINDOW(... ORDERBY(<unprojected>))` in the same measure was ignored - measured exit 0 on an
    estate-shaped measure with two windows."""
    two = _measures_tmdl(
        _measure(
            "Two Windows",
            "MAXX(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Order_Date], ASC)), CALCULATE(SUM('Orders'[Sales]))) "
            "+ MAXX(WINDOW(1, ABS, -1, ABS, ORDERBY('Orders'[Region], ASC)), CALCULATE(SUM('Orders'[Sales])))",
        )
    )
    bundle = build_bundle(
        tmp_path,
        two,
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Two Windows")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["mismatch"], crta.render(report)
    assert "'Orders'[Region]" in report["pairs"][0]["findings"][0]["detail"]


def test_a_relation_on_a_LATER_window_call_still_makes_it_unassessable(tmp_path: Path) -> None:
    """The conservative half of the same fix: one unreadable call makes the measure unreadable."""
    mixed = _measures_tmdl(
        _measure(
            "Mixed Windows",
            "MAXX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC)), CALCULATE(SUM('Orders'[Sales]))) "
            "+ MAXX(WINDOW(1, ABS, 0, REL, ALLSELECTED('Orders'), ORDERBY('Orders'[Region], ASC)), "
            "CALCULATE(SUM('Orders'[Sales])))",
        )
    )
    bundle = build_bundle(
        tmp_path,
        mixed,
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Mixed Windows")]},
        },
    )
    assert verdicts(crta.scan(bundle)) == ["unassessable"]


# --- review finding #5: period-to-date is judged, not excused -------------------------------


def test_fact_table_period_to_date_on_a_coarse_axis_is_unassessable(tmp_path: Path) -> None:
    """Finding 5. Excluding period-to-date and merely LISTING it under `not_assessed_by_design`
    returned `NOT_APPLICABLE`/exit 0 for the known-bad fact-table case. Reproduced on the committed
    Superstore model, whose relationship runs from `[Order Date 2017]`, not `[Order Date]`."""
    measures = _measures_tmdl(_measure("YTD Sales", "TOTALYTD(SUM('Orders'[Sales]), 'Orders'[Order_Date])"))
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Month")]},
            "Y": {"projections": [_measure_projection("_Measures", "YTD Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert codes(report) == ["period_to_date_grain_unproven"], crta.render(report)
    assert report["status"] == crta.STATUS_UNASSESSABLE
    assert "not_assessed_by_design" not in report


def test_period_to_date_on_a_marked_date_table_is_clean(tmp_path: Path) -> None:
    """The common, correct shape must stay silent, or the gate gets muted: a `dataCategory: Time`
    table's other columns ARE auto-removed by time intelligence."""
    measures = _measures_tmdl(_measure("YTD Sales", "TOTALYTD(SUM('Orders'[Sales]), 'Date'[Date])"))
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Series": {"projections": [_column_projection("Orders", "Region")]},
            "Y": {"projections": [_measure_projection("_Measures", "YTD Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert codes(report) == ["date_table_marked"], crta.render(report)
    assert report["status"] == crta.STATUS_OK


def test_period_to_date_with_no_date_grain_on_the_visual_is_clean(tmp_path: Path) -> None:
    """A YTD measure on a pure Region bar chart has no coarser date grain to be truncated by."""
    measures = _measures_tmdl(_measure("YTD Sales", "TOTALYTD(SUM('Orders'[Sales]), 'Orders'[Order_Date])"))
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Orders", "Region")]},
            "Y": {"projections": [_measure_projection("_Measures", "YTD Sales")]},
        },
        visual_type="clusteredColumnChart",
    )
    assert codes(crta.scan(bundle)) == ["no_date_grain_on_axis"]


def test_a_fixed_window_comparison_is_still_not_an_accumulation(tmp_path: Path) -> None:
    """`DATESBETWEEN` is anchored by its arguments. The committed Superstore model's whole CP/PP
    family is this shape, and reporting it once cost 12 rows against shipping evidence."""
    measures = _measures_tmdl(
        _measure(
            "Prior Period",
            "CALCULATE(SUM('Orders'[Sales]), DATESBETWEEN('Date'[Date], MIN('Date'[Date]), MAX('Date'[Date])))",
        )
    )
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "Prior Period")]},
        },
    )
    report = crta.scan(bundle)
    assert report["status"] == crta.STATUS_NOT_APPLICABLE
    assert "Prior Period" not in crta.render(report)


def test_as_of_allexcept_is_unassessable(tmp_path: Path) -> None:
    """ALLEXCEPT inverts the cleared set, which this gate deliberately does not model."""
    expression = (
        "CALCULATE(SUM('Orders'[Sales]), FILTER(ALLEXCEPT('Orders', 'Orders'[Region]), "
        "'Orders'[Order_Date] <= MAX('Orders'[Order_Date])))"
    )
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", expression))
    assert verdicts(report) == ["unassessable"]


def test_as_of_clearing_the_whole_table_is_clean(tmp_path: Path) -> None:
    """`ALL('Orders')` clears every column of the table, including the axis bin."""
    expression = (
        "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'), 'Orders'[Order_Date] <= MAX('Orders'[Order_Date])))"
    )
    assert codes(crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", expression))) == ["axis_cleared"]


# --------------------------------------------------------------------------------------------
# What is surfaced rather than judged
# --------------------------------------------------------------------------------------------


def test_running_total_stub_bound_to_a_visual_is_unassessable(tmp_path: Path) -> None:
    """The engine's own annotation is the only evidence a `BLANK()` was ever a running total."""
    measures = _measures_tmdl(
        _measure(
            "Calculation2",
            "BLANK()",
            {
                "TableauFormula": "RUNNING_SUM(SUM([Sales]))",
                "TranslationStubReason": "unsupported function RUNNING_SUM",
            },
        )
    )
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Orders", "Region")]},
            "Y": {"projections": [_measure_projection("_Measures", "Calculation2")]},
        },
    )
    report = crta.scan(bundle)
    assert codes(report) == ["stub"]
    assert report["stubbed_cumulative_measures"] == ["'_Measures'[Calculation2]"]


def test_an_ordinary_blank_stub_is_not_surfaced(tmp_path: Path) -> None:
    """Every other stub belongs to check_stub_measures.py; claiming them would drown this gate."""
    measures = _measures_tmdl(_measure("Profit Ratio", "BLANK()", {"TableauFormula": "SUM([Profit])/SUM([Sales])"}))
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Orders", "Region")]},
            "Y": {"projections": [_measure_projection("_Measures", "Profit Ratio")]},
        },
    )
    report = crta.scan(bundle)
    assert report["status"] == crta.STATUS_NOT_APPLICABLE
    assert report["stubbed_cumulative_measures"] == []


def test_period_to_date_is_judged_not_listed_and_fixed_windows_stay_out(tmp_path: Path) -> None:
    """The `not_assessed_by_design` bucket is GONE - it hid the fact-table case above. What remains
    true is that a fixed window is not an accumulation at all."""
    measures = _measures_tmdl(
        _measure("YTD Sales", "TOTALYTD(SUM('Orders'[Sales]), 'Date'[Date])"),
        _measure(
            "Prior Period",
            "CALCULATE(SUM('Orders'[Sales]), DATESBETWEEN('Date'[Date], MIN('Date'[Date]), MAX('Date'[Date])))",
        ),
    )
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "YTD Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert "not_assessed_by_design" not in report
    assert codes(report) == ["date_table_marked"]
    assert "Prior Period" not in crta.render(report)


def test_unbound_cumulative_measure_is_reported_not_cleared(tmp_path: Path) -> None:
    """The real estate shape: two WINDOW measures that no visual binds."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {"Category": {"projections": [_column_projection("Orders", "Region")]}},
        visual_type="clusteredColumnChart",
    )
    report = crta.scan(bundle)
    assert report["status"] == crta.STATUS_SKIPPED
    assert report["unbound_cumulative_measures"] == ["'_Measures'[Running Sales]"]
    assert "Running Sales" in crta.render(report)


def test_model_with_no_measures_is_not_applicable_but_an_unread_model_is_skipped(tmp_path: Path) -> None:
    """ "No measure declared" is a complete answer; "nothing was read" is not, and they differ."""
    bundle = build_bundle(
        tmp_path,
        "table _Measures\n",
        {"Category": {"projections": [_column_projection("Orders", "Region")]}},
    )
    assert crta.scan(bundle)["status"] == crta.STATUS_NOT_APPLICABLE

    report_dir = bundle / "pbip" / "WB" / "WB.Report"
    assert crta.check_pair(report_dir, report_dir)["status"] == crta.STATUS_SKIPPED


def test_report_without_a_model_is_unassessable(tmp_path: Path) -> None:
    """An unresolved model must never fold into a clean verdict."""
    import shutil  # pylint: disable=import-outside-toplevel

    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {"Category": {"projections": [_column_projection("Orders", "Region")]}},
    )
    (bundle / "pbip" / "WB" / "WB.Report" / "definition.pbir").write_text("{}", encoding="utf-8")
    shutil.rmtree(bundle / "pbip" / "WB" / "WB.SemanticModel")
    report = crta.scan(bundle)
    assert report["status"] == crta.STATUS_UNASSESSABLE
    assert report["reports_without_model"]
    assert "no semantic model resolved" in crta.render(report)


def test_a_clean_pair_cannot_mask_an_unassessed_one(tmp_path: Path) -> None:
    """19 clean reports plus one that could not be read is not a clean bundle."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    assert crta.scan(bundle)["status"] == crta.STATUS_OK

    second = bundle / "pbip" / "WB2"
    (second / "WB2.SemanticModel" / "definition").mkdir(parents=True)
    (second / "WB2.Report" / "definition").mkdir(parents=True)
    (second / "WB2.Report" / "definition.pbir").write_text(
        json.dumps({"datasetReference": {"byPath": {"path": "../WB2.SemanticModel"}}}), encoding="utf-8"
    )
    report = crta.scan(bundle)
    assert report["status"] == crta.STATUS_UNASSESSABLE
    assert report["assessed_clean"] == 1
    assert len(report["unassessed_pairs"]) == 1
    assert "UNASSESSED" in crta.render(report)


# --------------------------------------------------------------------------------------------
# DAX reading primitives
# --------------------------------------------------------------------------------------------


def test_split_arguments_respects_nesting_and_strings() -> None:
    assert dg._split_arguments("1, ABS, 0, REL, ORDERBY('T'[A], ASC)") == [
        "1",
        "ABS",
        "0",
        "REL",
        "ORDERBY('T'[A], ASC)",
    ]
    assert dg._split_arguments('SUM(a), "x, y"') == ["SUM(a)", '"x, y"']


def test_column_refs_read_quoted_and_bare_tables() -> None:
    refs = dg._column_refs("'Sample Superstore'[Order Date], Orders[Sales], [Bare]")
    assert [(r.table, r.column) for r in refs] == [
        ("Sample Superstore", "Order Date"),
        ("Orders", "Sales"),
        (None, "Bare"),
    ]


# --------------------------------------------------------------------------------------------
# CLI contract - exit codes, not printed text
# --------------------------------------------------------------------------------------------


def test_exit_codes_and_json_written_before_rendering(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    out = tmp_path / "nested" / "verdict.json"
    assert crta.main([str(bundle), "--json", str(out), "--quiet"]) == crta.EXIT_MISMATCH
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == crta.STATUS_MISMATCH
    assert payload["mismatches"] == 1
    capsys.readouterr()

    assert crta.main([str(bundle), "--warn-only", "--quiet"]) == crta.EXIT_OK
    capsys.readouterr()


def test_unassessable_exit_is_three_and_strict_promotes_it(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    measures = _measures_tmdl(
        _measure(
            "Running Sales", "SUMX(WINDOW(1, ABS, 0, REL, ORDERBY([Order_Date], ASC)), CALCULATE(SUM('Orders'[Sales])))"
        )
    )
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    assert crta.main([str(bundle), "--quiet"]) == crta.EXIT_UNASSESSABLE
    assert crta.main([str(bundle), "--quiet", "--strict"]) == crta.EXIT_MISMATCH
    capsys.readouterr()


def test_usage_errors_exit_two(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert crta.main([]) == crta.EXIT_USAGE
    assert crta.main(["--model", str(tmp_path)]) == crta.EXIT_USAGE
    assert crta.main([str(tmp_path / "nope")]) == crta.EXIT_USAGE
    capsys.readouterr()


# --------------------------------------------------------------------------------------------
# The false-positive net: committed, shipping evidence
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "example", sorted(p.name for p in (REPO_ROOT / "examples").iterdir() if (p / "fabric").is_dir())
)
def test_committed_examples_raise_no_mismatch(example: str) -> None:
    """16 worked examples ship in this repo. A single mismatch here is a false positive."""
    report = crta.scan(REPO_ROOT / "examples" / example / "fabric")
    assert report["mismatches"] == 0, crta.render(report)
    assert report["status"] in {crta.STATUS_OK, crta.STATUS_NOT_APPLICABLE, crta.STATUS_SKIPPED}


def test_gate_is_wired_into_check_unit() -> None:
    """One command has to keep answering "is it done"; a 21st gate to remember is not that."""
    import check_unit  # pylint: disable=import-outside-toplevel

    gate = next(g for g in check_unit.GATES if g.check_id == "running-total-axis")
    assert gate.script == "check_running_total_axis.py"
    assert crta.STATUS_MISMATCH in gate.finding_statuses
    assert crta.EXIT_MISMATCH in gate.finding_exit_codes
    assert crta.STATUS_UNASSESSABLE in gate.not_checked_statuses
    assert crta.EXIT_UNASSESSABLE in gate.not_checked_exit_codes
    assert "running-total-axis" in check_unit.INTEGRATION_CHECK_IDS
    assert "running-total-axis" in check_unit.OWNER_HINTS
