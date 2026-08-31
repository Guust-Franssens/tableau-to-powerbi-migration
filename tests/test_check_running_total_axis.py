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
# Round-2 review findings: four of the six were the gate being too AGGRESSIVE
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "bound"),
    [
        ("foreign date column", "MAX('Date'[Date])"),
        ("foreign end-of-period", "ENDOFMONTH('Date'[Date])"),
        ("ALLEXCEPT keeps some filters", "MAXX(ALLEXCEPT('Orders', 'Orders'[Region]), 'Orders'[Order_Date])"),
    ],
)
def test_an_as_of_bound_that_is_not_proven_to_move_is_unassessable(tmp_path: Path, label: str, bound: str) -> None:
    """Finding 1, a FALSE POSITIVE. `_classify_bound` called ANY MAX-like call containing ANY column
    reference context-dependent - it never consulted the compared column. A bound on a foreign date
    may well be an as-of date reached through a relationship, and may equally be something else, so
    the honest answer is `unassessable`. `ALLEXCEPT` keeps the filters on the columns it names, so
    the bound may still move: also unresolved, never a verdict."""
    expression = f"CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), 'Orders'[Order_Date] <= {bound}))"
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", expression))
    assert verdicts(report) == ["unassessable"], f"{label}: " + crta.render(report)
    assert report["mismatches"] == 0


@pytest.mark.parametrize(
    ("label", "bound"),
    [
        ("ALL removes every visual filter", "MAXX(ALL('Orders'), 'Orders'[Order_Date])"),
        ("REMOVEFILTERS is ALL by another name", "CALCULATE(MAX('Orders'[Order_Date]), REMOVEFILTERS('Orders'))"),
        ("ALLSELECTED ignores the visual's own row", "MAXX(ALLSELECTED('Orders'), 'Orders'[Order_Date])"),
    ],
)
def test_an_as_of_bound_that_removes_context_is_not_an_accumulation(tmp_path: Path, label: str, bound: str) -> None:
    """Finding 1, second half. `MAXX(ALL('Orders'), 'Orders'[Order_Date])` explicitly discards every
    visual filter, so it evaluates to ONE global constant and cannot move with the axis. It is a
    pinned cutoff wearing a `MAX`, and per-bucket totals are its point - not a running total at all."""
    expression = (
        f"VAR _asOf = {bound} RETURN CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
        "'Orders'[Order_Date] <= _asOf))"
    )
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", expression))
    assert report["status"] == crta.STATUS_NOT_APPLICABLE, f"{label}: " + crta.render(report)


@pytest.mark.parametrize(
    ("label", "predicate"),
    [
        ("<> is not <", "'Orders'[Order_Date] <> MAX('Orders'[Order_Date])"),
        (">= is not an upper bound", "'Orders'[Order_Date] >= MAX('Orders'[Order_Date])"),
        (
            "a nested < is not the predicate",
            "'Orders'[Region] = IF('Orders'[Order_Date] < MAX('Orders'[Order_Date]), \"a\", \"b\")",
        ),
        ("a < inside a string is text", "'Orders'[Region] = \"a < b\""),
        ("a < inside a quoted table name is a name", "'Orders'[Region] = 'a<b'[X]"),
    ],
)
def test_only_a_top_level_less_than_is_an_as_of_predicate(tmp_path: Path, label: str, predicate: str) -> None:
    """Finding 2, a FALSE POSITIVE. The comparison regex excluded only a following `=`, so it matched
    the `<` inside DAX's `<>` operator and every ordinary exclusion filter became a running total.
    Measured: `FILTER(ALL('Date'[Date]), 'Date'[Date] <> MAX('Date'[Date]))` on a month axis exited
    1. The operator is now parsed at depth 0, outside string literals, longest form first."""
    expression = f"CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), {predicate}))"
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", expression))
    assert report["status"] == crta.STATUS_NOT_APPLICABLE, f"{label}: " + crta.render(report)
    assert report["mismatches"] == 0


SAFE_CALL = (
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date], 'Orders'[Order_Month]), "
    "'Orders'[Order_Date] <= MAX('Orders'[Order_Date])))"
)
DEFECTIVE_CALL = (
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
    "'Orders'[Order_Date] <= MAX('Orders'[Order_Date])))"
)


@pytest.mark.parametrize("order", ["safe first", "defective first"])
def test_a_safe_as_of_call_cannot_excuse_a_defective_one_in_the_same_measure(tmp_path: Path, order: str) -> None:
    """Finding 3. `_classify_as_of` returned after the FIRST qualifying `FILTER`, recreating exactly
    the order-dependent hole round 1 fixed for multiple `WINDOW` calls: a measure whose first term
    clears both `Order_Date` and `Order_Month` printed OK while its second term, clearing only
    `Order_Date`, degenerated to monthly totals on the same axis. Both orders must fail, because the
    defect is the second call's own - the cleared sets must NOT be unioned."""
    terms = [SAFE_CALL, DEFECTIVE_CALL] if order == "safe first" else [DEFECTIVE_CALL, SAFE_CALL]
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", " + ".join(terms)))
    assert verdicts(report) == ["mismatch"], f"{order}: " + crta.render(report)
    assert report["pairs"][0]["findings"][0]["judged_calls"] == 2


def _aggregated_projection(entity: str, prop: str, function: int = 3) -> dict:
    """A PBIR projection whose `field` IS an `Aggregation` - the shape 377 estate projections use."""
    return {
        "field": {
            "Aggregation": {
                "Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
                "Function": function,
            }
        },
        "queryRef": f"Max({entity}.{prop})",
    }


def test_an_aggregated_projection_does_not_group_the_query(tmp_path: Path) -> None:
    """Finding 4, a FALSE POSITIVE. The reused generic `_walk` extracts the source `Column` nested
    inside an `Aggregation`, and `VisualBinding.columns()` then treated it as a grouping column - so
    a visual grouped only by `Region`, with `MAX(Orders[Order_Month])` as an aggregated TOOLTIP,
    exited 1. An aggregated value collapses; it does not add a GROUP BY. Measured across the estate
    and `examples/`: 709 visuals, 1248 role bodies, 377 top-level `Aggregation` nodes, EVERY one of
    them wrapping a `Column`."""
    bundle = build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Running Sales", AS_OF)),
        {
            "Category": {"projections": [_column_projection("Orders", "Region")]},
            "Tooltips": {"projections": [_aggregated_projection("Orders", "Order_Month")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["ok"], crta.render(report)
    assert codes(report) == ["axis_not_a_date_grain"]
    assert report["pairs"][0]["findings"][0]["not_flagged"] == ["'Orders'[Region]"]


def test_a_visual_whose_only_column_is_aggregated_has_no_grouping_column(tmp_path: Path) -> None:
    """The conservative half of finding 4: removing a column from the grouping set must not turn an
    unassessable visual into a clean one. With nothing left to group by, the honest verdict is the
    same `no_grouping_column` a card gets - exit 3, never exit 0."""
    bundle = build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Running Sales", AS_OF)),
        {
            "Tooltips": {"projections": [_aggregated_projection("Orders", "Order_Month")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert codes(report) == ["no_grouping_column"]
    assert report["status"] == crta.STATUS_UNASSESSABLE


@pytest.mark.parametrize("partition", ["Order_Month", "Order Quarter", "Order Month Label"])
def test_a_partition_beside_the_addressed_date_is_unassessable_not_a_mismatch(tmp_path: Path, partition: str) -> None:
    """Finding 5, a FALSE POSITIVE. Every same-table date-derived survivor was called a mismatch,
    even when the addressed date ITSELF is projected. Measured on the estate's unmarked fact model:
    the canonical as-of on `Orders.csv[Order_Date]` with `Order Date (Year)` as the series
    accumulates `10 -> 30`, resets, then `100 -> 300` - it does NOT become each bucket's own total,
    so `mismatch` states something false. It is not provably RIGHT either (a year-restarting running
    total is a deliberate Tableau shape and an accidental legend produces identical bytes), so the
    verdict is `unassessable`: exit 3, never a pass."""
    bundle = build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Running Sales", AS_OF)),
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Date")]},
            "Series": {"projections": [_column_projection("Orders", partition)]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["unassessable"], crta.render(report)
    assert codes(report) == ["axis_partitions_accumulation"]
    assert report["mismatches"] == 0


def test_the_addressed_date_must_actually_GROUP_to_earn_the_partition_reading(tmp_path: Path) -> None:
    """The seam between findings 4 and 5, and the one way finding 5's fix could reopen a false
    negative: an AGGREGATED `Order_Date` is projected but does not group, so the axis is still
    coarser-only and the accumulation really does collapse to the bucket's own total. Mismatch."""
    bundle = build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Running Sales", AS_OF)),
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Month")]},
            "Tooltips": {"projections": [_aggregated_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["mismatch"], crta.render(report)
    assert codes(report) == ["axis_grain_not_cleared"]


def test_every_mutation_in_the_harness_names_a_symbol_that_still_exists() -> None:
    """Finding 6, and the reason it is a test rather than a comment. After the module split the
    finding-2 mutation still referenced `crta._DATE_TYPES`, which had moved to `dax_grain`. The
    patched function raised `AttributeError`, pytest reported the expected test as FAILED, and the
    harness scored CAUGHT - a named test failure indistinguishable from a genuine catch, without
    the intended mutation ever running. A stale symbol is now a build failure here."""
    import ast  # pylint: disable=import-outside-toplevel
    import check_unit  # pylint: disable=import-outside-toplevel

    import _mutation_probe_kit as kit  # pylint: disable=import-outside-toplevel
    import mutation_harness_running_total_axis as harness  # pylint: disable=import-outside-toplevel

    modules = {"crta": crta, "dg": dg, "check_unit": check_unit, "kit": kit, "harness": harness}
    snippets = {name: code for name, (code, _) in harness.MUTATIONS.items()}
    snippets.update(harness.CONTROLS)
    snippets.update({f"{name}:probe": probe for name, probe in harness.PROBES.items()})
    stale: list[str] = []
    for name, code in snippets.items():
        tree = ast.parse(code)
        # An attribute a snippet CREATES is legitimate; only a READ of a symbol that never existed
        # is the finding-6 defect.
        created = {
            f"{target.value.id}.{target.attr}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            module = modules.get(node.value.id)
            reference = f"{node.value.id}.{node.attr}"
            if module is not None and reference not in created and not hasattr(module, node.attr):
                stale.append(f"{name}: {reference}")
    assert not stale, "mutation snippets reference symbols that no longer exist: " + "; ".join(sorted(set(stale)))


def test_every_mutation_declares_a_probe_that_proves_it_executes() -> None:
    """The other half of finding 6: an identity assertion proves a patch was APPLIED, never that the
    patched code RUNS or does what the table claims. Every mutation must ship a plugin-time probe,
    and that probe must at least CALL something and ASSERT something.

    Measured why the second half matters: replacing the stale-symbol mutation's probe with a bare
    `pass` made the harness score it CAUGHT again - one line reinstated the whole finding."""
    import mutation_harness_running_total_axis as harness  # pylint: disable=import-outside-toplevel

    missing = sorted(set(harness.MUTATIONS) - set(harness.PROBES))
    assert not missing, "mutations with no intended-behaviour probe: " + ", ".join(missing)
    trivial = sorted(
        f"{name}: {why}" for name, probe in harness.PROBES.items() if (why := harness.probe_is_trivial(probe))
    )
    assert not trivial, "probes that prove nothing: " + "; ".join(trivial)
    for name, probe in harness.CONTROL_PROBES.items():
        assert harness.probe_is_trivial(probe) is None, f"control probe {name} proves nothing"


@pytest.mark.parametrize(
    ("label", "output", "expected"),
    [
        ("a real failure", "FAILED tests/t.py::test_a - AssertionError", ["test_a"]),
        ("a parametrised failure", "FAILED tests/t.py::test_a[Order Quarter] - X", ["test_a"]),
        ("a COLLECTION error is not a catch", "ERROR tests/t.py::TestThing", []),
        ("an error line plus a summary", "ERROR tests/t.py\n2 errors in 0.1s", []),
        ("a non-anchored mention is not a catch", "  see FAILED tests/t.py::test_a", []),
    ],
)
def test_only_anchored_FAILED_lines_count_as_a_catch(label: str, output: str, expected: list[str]) -> None:
    """`ERROR path::TestName` is a COLLECTION failure - no test ran - and scoring it as a catch is
    the exact bug blind review found in `tests/mutation_harness.py`, which credited any non-zero
    pytest exit. Its mirror image, a dying `xdist` worker printing `FAILED path::test_name` for a
    test that never ran, is refused a level up by the harness's broken-run markers instead, because
    at this line it is genuinely indistinguishable from a real failure."""
    import mutation_harness_running_total_axis as harness  # pylint: disable=import-outside-toplevel

    assert harness._named_failures(output) == expected, label  # pylint: disable=protected-access
    assert any(marker in "worker gw0 crashed while running 'x'" for marker in harness._BROKEN_RUN_MARKERS)  # pylint: disable=protected-access


# --------------------------------------------------------------------------------------------
# Round-3 review findings: "the first match decides", for the third time
# --------------------------------------------------------------------------------------------

# The reviewer's expression, VERBATIM. `REMOVEFILTERS(Region)` cannot touch a month-axis filter, so
# `_asOf` is still the current month's maximum date - a real running total, which then hits the
# uncleared coarse-axis defect this gate exists to catch.
R3_UNRELATED_REMOVAL = (
    "VAR _asOf = CALCULATE(MAX('Orders'[Order_Date]), REMOVEFILTERS('Orders'[Region])) "
    "RETURN CALCULATE(SUM('Orders'[Sales]), "
    "FILTER(ALL('Orders'[Order_Date]), 'Orders'[Order_Date] <= _asOf))"
)
# The reviewer's second expression, VERBATIM, wrapped in the FILTER it was quoted from.
R3_START_THEN_ASOF = (
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
    "'Orders'[Order_Date] >= DATE(2024,1,1) "
    "&& 'Orders'[Order_Date] <= MAX('Orders'[Order_Date])))"
)
R3_ASOF_THEN_START = (
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
    "'Orders'[Order_Date] <= MAX('Orders'[Order_Date]) "
    "&& 'Orders'[Order_Date] >= DATE(2024,1,1)))"
)


def test_an_unrelated_filter_removal_does_not_hide_a_moving_cutoff(tmp_path: Path) -> None:
    """Finding 1, VERBATIM. Round 2 correctly made `ALL`/`REMOVEFILTERS` mean "pinned"; the missing
    half was *pinned with respect to WHAT*. `_classify_moving_bound` took the FIRST removal it found
    and never read its arguments, so `REMOVEFILTERS('Orders'[Region])` - which cannot touch a
    month-axis date filter - dropped the measure entirely: `classify() -> None`, zero cumulative
    measures, `NOT_APPLICABLE`, exit 0, with the bucket-total defect intact."""
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", R3_UNRELATED_REMOVAL))
    assert report["pairs"][0]["cumulative_measures"] == 1, crta.render(report)
    assert verdicts(report) == ["mismatch"], crta.render(report)
    assert codes(report) == ["axis_grain_not_cleared"]


@pytest.mark.parametrize(
    ("label", "removal", "expected"),
    [
        ("the whole table is a global maximum", "REMOVEFILTERS('Orders')", crta.STATUS_NOT_APPLICABLE),
        ("no argument clears the whole model", "REMOVEFILTERS()", crta.STATUS_NOT_APPLICABLE),
        ("the compared column itself is subtle", "REMOVEFILTERS('Orders'[Order_Date])", crta.STATUS_UNASSESSABLE),
        ("an unrelated column proves nothing", "REMOVEFILTERS('Orders'[Region])", crta.STATUS_MISMATCH),
    ],
)
def test_a_removal_is_only_pinning_when_it_covers_the_compared_column_or_its_table(
    tmp_path: Path, label: str, removal: str, expected: str
) -> None:
    """The scope of the removal, not its presence, is what decides. All four are the SAME measure
    with one argument changed, so nothing but the removal's reach can explain the difference."""
    expression = (
        f"VAR _asOf = CALCULATE(MAX('Orders'[Order_Date]), {removal}) "
        "RETURN CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
        "'Orders'[Order_Date] <= _asOf))"
    )
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", expression))
    assert report["status"] == expected, f"{label}: " + crta.render(report)


@pytest.mark.parametrize(
    ("order", "expression"),
    [("start bound first", R3_START_THEN_ASOF), ("as-of bound first", R3_ASOF_THEN_START)],
)
def test_conjunct_order_cannot_decide_whether_a_running_total_is_detected(
    tmp_path: Path, order: str, expression: str
) -> None:
    """Finding 2, VERBATIM and in BOTH orders - a test that exercised one order would prove nothing.
    `_top_level_comparison` returned the FIRST comparison in an `&&` chain and `_as_of_predicate`
    then rejected the whole predicate because that first one was `>=`. Measured: start-first
    returned no as-of call at all (exit 0); reversing the two semantically equivalent conjuncts
    returned one (exit 1). A running total from a fixed start date was invisible."""
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", expression))
    assert verdicts(report) == ["mismatch"], f"{order}: " + crta.render(report)
    assert codes(report) == ["axis_grain_not_cleared"]


def test_both_conjunct_orders_reach_the_identical_verdict(tmp_path: Path) -> None:
    """The invariant behind finding 2, stated directly: two semantically equivalent spellings must
    not disagree. This is the assertion a single-order test structurally cannot make."""
    first = crta.scan(_as_of_bundle(tmp_path / "a", "Orders", "Order_Month", R3_START_THEN_ASOF))
    second = crta.scan(_as_of_bundle(tmp_path / "b", "Orders", "Order_Month", R3_ASOF_THEN_START))
    assert (first["status"], codes(first)) == (second["status"], codes(second))


@pytest.mark.parametrize("order", ["pinned VAR declared first", "moving VAR declared first"])
def test_a_bound_built_from_two_vars_is_read_from_both_of_them(tmp_path: Path, order: str) -> None:
    """Audit, same shape: `_classify_bound` returned on the FIRST declared `VAR` whose name appeared
    in the bound, so `MIN(_cut, _asOf)` was classified from whichever happened to be declared first.
    Measured before the fix: swapping the two `VAR` lines flipped the gate between `NOT_APPLICABLE`
    (exit 0) and `MISMATCH` (exit 1) on identical semantics. A bound built from both a pinned and a
    moving value is genuinely ambiguous - `unassessable`, and the same either way round."""
    declarations = (
        "VAR _cut = DATE(2024,12,31) VAR _asOf = MAX('Orders'[Order_Date]) "
        if order == "pinned VAR declared first"
        else "VAR _asOf = MAX('Orders'[Order_Date]) VAR _cut = DATE(2024,12,31) "
    )
    expression = (
        declarations + "RETURN CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
        "'Orders'[Order_Date] <= MIN(_cut, _asOf)))"
    )
    report = crta.scan(_as_of_bundle(tmp_path, "Orders", "Order_Month", expression))
    assert verdicts(report) == ["unassessable"], f"{order}: " + crta.render(report)
    assert report["mismatches"] == 0


def test_an_unreadable_window_call_cannot_suppress_a_readable_one(tmp_path: Path) -> None:
    """Audit, same shape. Round 2 unioned every window call's ORDERBY columns, which closed the
    false negative but left an UNREADABLE call returning early: an explicit-relation `WINDOW`
    beside `WINDOW(... ORDERBY('Orders'[Region]))` exited 3 where the defective call alone exits 1.
    Never a pass, but the wrong verdict - worst must win, not first."""
    relation_first = _measures_tmdl(
        _measure(
            "Mixed",
            "MAXX(WINDOW(1, ABS, 0, REL, ALLSELECTED('Orders'), ORDERBY('Orders'[Order_Date], ASC)), 1) "
            "+ MAXX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Region], ASC)), 1)",
        )
    )
    bundle = build_bundle(
        tmp_path,
        relation_first,
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Mixed")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["mismatch"], crta.render(report)
    assert "'Orders'[Region]" in report["pairs"][0]["findings"][0]["detail"]


def test_every_period_to_date_call_is_judged_not_the_first_in_dict_order(tmp_path: Path) -> None:
    """Audit, same shape, and a SILENT PASS. `_classify_period_to_date` returned after the first
    match found while walking `_PERIOD_TO_DATE_FUNCTIONS` - so not even the first in the text.
    Measured: a safe `TOTALYTD` on the marked date table beside a defective fact-table `DATESYTD`
    exited **0** (`date_table_marked`), while the `DATESYTD` term alone exits 3."""
    both = _measures_tmdl(
        _measure(
            "Two Periods",
            "TOTALYTD(SUM('Orders'[Sales]), 'Date'[Date]) "
            "+ CALCULATE(SUM('Orders'[Sales]), DATESYTD('Orders'[Order_Date]))",
        )
    )
    bundle = build_bundle(
        tmp_path,
        both,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "Two Periods")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["unassessable"], crta.render(report)
    assert codes(report) == ["period_to_date_grain_unproven"]


def test_a_correct_window_cannot_mask_a_defective_as_of_in_the_same_measure(tmp_path: Path) -> None:
    """Audit, the WORST of them: the first-match bug at the DISPATCHER. `classify` returned after
    the first reader that matched, so a measure declaring two mechanisms was judged on one. Measured:
    a correct `WINDOW(... ORDERBY('Orders'[Order_Date]))` beside an as-of comparing `Order_Month` -
    which the visual does not project - exited **0** (`orderby_projected`), where that as-of alone
    exits 1. Every reader now runs and every verdict is folded through `_worst`."""
    mixed = _measures_tmdl(
        _measure(
            "Window Plus As Of",
            "MAXX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC)), 1) "
            "+ CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Month]), "
            "'Orders'[Order_Month] <= MAX('Orders'[Order_Month])))",
        )
    )
    bundle = build_bundle(
        tmp_path,
        mixed,
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Window Plus As Of")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["mismatch"], crta.render(report)
    assert report["pairs"][0]["findings"][0]["shape"] == "window_orderby+as_of_filter"
    assert report["pairs"][0]["findings"][0]["judged_calls"] == 2


def test_worst_verdict_wins_is_one_shared_rule_not_four_copies(tmp_path: Path) -> None:
    """The rule itself, asserted directly, because three rounds of the same bug is evidence that
    re-deriving precedence per mechanism does not hold."""
    assert crta._VERDICT_PRECEDENCE == ("mismatch", "unassessable", "ok")  # pylint: disable=protected-access
    mismatch = crta._verdict("mismatch", "m", "")  # pylint: disable=protected-access
    unassessable = crta._verdict("unassessable", "u", "")  # pylint: disable=protected-access
    clean = crta._verdict("ok", "o", "")  # pylint: disable=protected-access
    for order in ([clean, unassessable, mismatch], [mismatch, clean, unassessable], [unassessable, mismatch, clean]):
        assert crta._worst(order)["verdict"] == "mismatch"  # pylint: disable=protected-access
    assert crta._worst([clean, unassessable])["verdict"] == "unassessable"  # pylint: disable=protected-access
    assert crta._worst([clean])["code"] == "o"  # pylint: disable=protected-access
    assert crta._worst([])["code"] == "unreadable_grain"  # pylint: disable=protected-access
    assert tmp_path.exists()


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
