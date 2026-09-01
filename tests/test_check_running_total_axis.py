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
import dax_tokens as dt  # noqa: E402  # pylint: disable=wrong-import-position

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


def test_a_window_without_an_orderby_is_not_an_engine_shape(tmp_path: Path) -> None:
    """ROUND 6 FINDING 3. `_judge_one_window` used to answer `ok`/`orders_by_visual_grain` for a
    call with no ORDERBY *before* asking whether the visual had a grain at all - measured,
    `SUMX(OFFSET(-1), CALCULATE(SUM('Orders'[Sales])))` on a card that projects no grouping column
    exited **0**. With no visual grain there is nothing to establish the relation against.

    Both halves of the fix are pinned here. Every engine template requires a `<spec>`, so a
    no-ORDERBY call is not a recognised shape on ANY visual; and the card case - the reviewer's
    exact reproduction - must not exit 0."""
    measures = _measures_tmdl(_measure("Prev", "SUMX(OFFSET(-1), CALCULATE(SUM('Orders'[Sales])))"))
    on_axis = build_bundle(
        tmp_path / "axis",
        measures,
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "Prev")]},
        },
    )
    assert verdicts(crta.scan(on_axis)) == ["unassessable"], crta.render(crta.scan(on_axis))

    card = build_bundle(
        tmp_path / "card",
        measures,
        {"Y": {"projections": [_measure_projection("_Measures", "Prev")]}},
        visual_type="card",
    )
    report = crta.scan(card)
    assert verdicts(report) == ["unassessable"], crta.render(report)
    assert crta.main([str(card), "--quiet"]) == crta.EXIT_UNASSESSABLE


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


# --- review findings #1-#3: the proxies that used to decide "safe" --------------------------


# --- review finding #4: every window call, not the first ------------------------------------


def test_two_window_calls_in_one_measure_are_refused_not_judged_on_one(tmp_path: Path) -> None:
    """Rounds 1-2 fixed "the first window call decided the measure" by folding every call. Round 6
    replaced the fold with a stronger guarantee: a SUM OF TWO windows is not one engine template, so
    it is never judged at all.

    That is a deliberate narrowing and it must not become a silent pass - the point of the original
    finding was that the second, defective window exited **0**. It still cannot: the whole measure
    is `unassessable`, exit 3, with the measure named."""
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
    assert verdicts(report) == ["unassessable"], crta.render(report)
    assert report["pairs"][0]["findings"][0]["measure"] == "'_Measures'[Two Windows]"
    assert crta.main([str(bundle), "--quiet"]) == crta.EXIT_UNASSESSABLE


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


# --- restored after round 5: behaviours the narrowing KEPT, whose covering tests were --------
# --- collateral damage of deleting the as-of section. Every one of these was a SURVIVED -----
# --- mutation until it came back, which is the harness earning its keep. ---------------------


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


@pytest.mark.parametrize("role", ["Category", "Rows", "Columns", "Series", "Details"])
def test_every_projected_column_is_examined_not_a_curated_axis_list(tmp_path: Path, role: str) -> None:
    """A curated axis-role list is a PROXY for "does this column group the query", and it omitted
    the pivotTable's real `Columns` role: measured on the estate's Section 12 pivot, the identical
    `dateTime` bin exited 1 under `Rows` and 0 under `Columns`. Safety is decided from EVERY
    projected column. `AXIS_ROLES` survives for the finding's prose only."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {
            role: {"projections": [_column_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["ok"], f"{role}: " + crta.render(report)
    assert codes(report) == ["orderby_projected"]


def test_an_aggregated_projection_does_not_group_the_query(tmp_path: Path) -> None:
    """`Max('Orders'[Order_Date])` on a visual is a VALUE, not a grouping key, so it cannot satisfy
    an ORDERBY. Measured across the estate + `examples/`: 709 visuals carry **377** top-level
    `Aggregation` nodes, every one wrapping a `Column`, so reusing the generic reference walk here
    counted 377 aggregated values as grouping columns and blocked correct reports."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {
            "Category": {"projections": [_column_projection("Orders", "Region")]},
            "Tooltips": {"projections": [_aggregated_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["mismatch"], crta.render(report)
    assert "'Orders'[Order_Date]" in report["pairs"][0]["findings"][0]["detail"]


def test_a_visual_whose_only_column_is_aggregated_has_no_grouping_column(tmp_path: Path) -> None:
    """The other half: with nothing left that groups, the honest answer is `unassessable`, never
    "the axis is cleared". An empty projection list read as safety is how a real defect passed."""
    bundle = build_bundle(
        tmp_path,
        WINDOW_MEASURE,
        {
            "Category": {"projections": [_aggregated_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Running Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert codes(report) == ["no_grouping_column"], crta.render(report)


def test_an_unreadable_window_call_cannot_be_judged_on_its_readable_sibling(tmp_path: Path) -> None:
    """Round 2's finding, under the round-6 contract. An explicit-relation `WINDOW` beside a
    readable one used to exit 3 where the defective call alone exits 1 - "never a pass, but the
    wrong verdict". A whole-expression match cannot produce either: neither call is judged, so the
    measure is refused with its name printed, and there is no fold left to pick the wrong winner."""
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
    assert verdicts(report) == ["unassessable"], crta.render(report)
    assert report["assessed_clean"] == 0


def test_a_second_orderby_clause_is_unassessable_rather_than_assumed_away(tmp_path: Path) -> None:
    """The documented grammars give ONE `ORDERBY` slot per window call. Round 5 VERIFIED that rather
    than relying on it; round 6 gets the same guarantee for free, because a second clause is an
    extra token the template never accounts for."""
    measures = _measures_tmdl(
        _measure(
            "Two Orderings",
            "MAXX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC), ORDERBY('Orders'[Region], ASC)), 1)",
        )
    )
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Orders", "Order_Date")]},
            "Y": {"projections": [_measure_projection("_Measures", "Two Orderings")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["unassessable"], crta.render(report)
    assert codes(report) == ["unrecognised"]


def test_worst_verdict_wins_is_one_shared_rule_not_four_copies(tmp_path: Path) -> None:
    """The rule itself, asserted directly, because rounds of the same bug are evidence that
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


# --- review finding #5: period-to-date is judged, not excused -------------------------------


def test_two_period_to_date_calls_in_one_measure_are_refused(tmp_path: Path) -> None:
    """Round 3's finding, under the round-6 contract. A safe `TOTALYTD` on the marked date table
    beside a defective fact-table `DATESYTD` used to exit **0** (`date_table_marked`), because the
    classifier stopped at the first function found while walking a dict. A sum of two calls is not
    one template, so neither is judged - and the measure is still named at exit 3, never cleared."""
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
    assert codes(report) == ["unrecognised"]
    assert report["assessed_clean"] == 0


@pytest.mark.parametrize(
    ("label", "expression"),
    [
        ("no <dates> argument at all", "TOTALYTD(SUM('Orders'[Sales]))"),
        (
            "several columns in <dates>",
            "CALCULATE(SUM('Orders'[Sales]), DATESYTD(DATESBETWEEN('Date'[Date], "
            "MIN('Orders'[Order_Date]), MAX('Orders'[Order_Date]))))",
        ),
        (
            "a filter-modifying inner expression",
            "TOTALYTD(CALCULATE(SUM('Orders'[Sales]), REMOVEFILTERS('Orders'[Order Month Label])), 'Date'[Date])",
        ),
    ],
)
def test_a_period_to_date_call_that_cannot_be_read_is_not_dropped(tmp_path: Path, label: str, expression: str) -> None:
    """A dropped call is indistinguishable from "this model has no period-to-date measure" - the
    same silence every round of this review has been about, one mechanism over.

    The third case is why the period-to-date templates take `<sagg>` (a bare aggregate over one
    column) rather than the window family's `<agg>`. The judged property here is whether a filter is
    REMOVED, and an arbitrary inner expression can remove filters itself - so admitting one would be
    a verdict formed without reading the thing that decided it."""
    bundle = build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Ytd", expression)),
        {
            "Category": {"projections": [_column_projection("Date", "Month Start")]},
            "Y": {"projections": [_measure_projection("_Measures", "Ytd")]},
        },
    )
    report = crta.scan(bundle)
    assert report["status"] == crta.STATUS_UNASSESSABLE, f"{label}: " + crta.render(report)
    assert codes(report) == ["unrecognised"], f"{label}: " + crta.render(report)


def test_a_date_named_column_with_no_proof_is_unassessable_not_clean(tmp_path: Path) -> None:
    """`ModelFacts.grain_of`'s third rung, which only period-to-date still consumes: a column named
    like a date part, with neither a declared date type nor calculated lineage back to the anchor,
    is a GUESS. A guess may reach `unassessable`; it may never reach a mismatch, and it may never
    reach clean."""
    measures = _measures_tmdl(_measure("YTD Sales", "TOTALYTD(SUM('Orders'[Sales]), 'Orders'[Order_Date])"))
    bundle = build_bundle(
        tmp_path,
        measures,
        {
            "Category": {"projections": [_column_projection("Orders", "Fiscal Period")]},
            "Y": {"projections": [_measure_projection("_Measures", "YTD Sales")]},
        },
    )
    report = crta.scan(bundle)
    assert verdicts(report) == ["unassessable"], crta.render(report)
    assert report["mismatches"] == 0


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


def test_the_lexer_is_total_and_identifiers_are_atomic() -> None:
    """The lexer, unit-tested directly, because every claim in this file now rests on it.

    ROUND 6 FINDING 1, both directions. `'Orders--Archive'` is ONE token, so no comment scanner can
    truncate it; `'WINDOW()'` and `[WINDOW(...)]` are ONE token each, so no function search can find
    a call inside a name. And `<>` is one token, so DAX's not-equal is not the `<` it begins with.
    """
    kinds = [(t.kind, t.text) for t in dt.tokenize("SUM('Orders--Archive'[Sales])").code]
    assert kinds == [
        ("name", "SUM"),
        ("op", "("),
        ("table", "'Orders--Archive'"),
        ("column", "[Sales]"),
        ("op", ")"),
    ]
    assert [t.kind for t in dt.tokenize("SUM('WINDOW()'[Sales])").code] == ["name", "op", "table", "column", "op"]
    assert [t.text for t in dt.tokenize("a <> b").code if t.kind == "op"] == ["<>"]
    assert [t.text for t in dt.tokenize("a <= b").code if t.kind == "op"] == ["<="]
    # comments and string literals are lexed, then dropped or kept as atoms - never re-scanned
    assert [t.text for t in dt.tokenize("A -- FILTER(x)\nB").code] == ["A", "B"]
    assert [t.text for t in dt.tokenize("A /* FILTER(x) */ B").code] == ["A", "B"]
    assert [t.kind for t in dt.tokenize('A & "FILTER(x)"').code] == ["name", "op", "string"]
    # a `"` inside a legal column name must not open a phantom literal
    survives = "'T'[He said \"hi\"] <= MAX('T'[He said \"hi\"])"
    assert [t.kind for t in dt.tokenize(survives).code] == [
        "table",
        "column",
        "op",
        "name",
        "op",
        "table",
        "column",
        "op",
    ]
    # totality: every character of the input is covered by exactly one token, in order
    for sample in ("SUM('a'[b]) -- x", 'A & "q" /* c */ + 1.5e3', "!!weird??"):
        lexed = dt.tokenize(sample)
        assert "".join(t.text for t in lexed.tokens) == sample
        assert [t.start for t in lexed.tokens] == [0] + [t.end for t in lexed.tokens][:-1]


def test_an_unterminated_identifier_is_reported_not_guessed() -> None:
    """An expression that cannot be lexed cannot be judged. `classify` refuses it rather than
    matching a template against a token stream it knows is wrong."""
    lexed = dt.tokenize("SUMX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders[Order_Date], ASC)), 1)")
    assert lexed.unterminated
    assert lexed.has_unknown


def test_column_references_are_read_from_tokens_not_text() -> None:
    refs = dg.column_references("'Sample Superstore'[Order Date], Orders[Sales], [Bare]")
    assert [(r.table, r.column) for r in refs] == [
        ("Sample Superstore", "Order Date"),
        ("Orders", "Sales"),
        (None, "Bare"),
    ]
    # a reference inside a string literal or a comment is not a reference
    assert dg.column_references('IF(x, "[Date]", "other")') == []
    assert dg.column_references("x -- 'Date'[Date]") == []


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


def test_the_running_total_harness_scores_from_pytests_lifecycle_not_from_text() -> None:
    """Round 5, and a REPLACEMENT rather than a repair. This test used to pin a text rule -
    "only anchored FAILED lines count" - and #409 retired that rule for the whole repo: a
    call-phase `NameError` inside a mutant emits a named `FAILED` line, so text scoring credits a
    crash-kill as a semantic catch. The running-total harness now delegates to the shared module's
    lifecycle record, and this asserts the delegation rather than re-implementing the check that
    `tests/test_mutation_harness_scoring.py` already owns.

    The negative half is the load-bearing one: a private text scanner reappearing here is exactly
    how the retired rule would come back, silently, in a file nobody re-reads."""
    import inspect  # pylint: disable=import-outside-toplevel

    import mutation_harness as shared  # pylint: disable=import-outside-toplevel
    import mutation_harness_running_total_axis as harness  # pylint: disable=import-outside-toplevel

    assert harness.shared is shared
    source = inspect.getsource(harness.run)
    for delegated in ("shared.OUTCOME_HOOKS", "shared.read_outcomes", "shared.observed_mutation"):
        assert delegated in source, f"the harness no longer delegates {delegated}"
    assert "shared.session_is_trustworthy" in source, "SURVIVED would be unearned without a complete session"
    assert "shared.VERDICT_BEARING_EXITS" in source, "an outcome beside exit 2/3/4/5 is not a verdict"
    assert not hasattr(harness, "_named_failures"), "the retired text rule is back"
    assert not hasattr(harness, "_BROKEN_RUN_MARKERS"), "the retired text rule's helper is back"

    # The shared record shapes, exercised here so this test cannot pass on delegation alone.
    crash_kill = {"call_failed": ["tests/t.py::test_a"], "setup_failed": []}
    assert shared.observed_mutation(crash_kill) is True
    assert shared.observed_mutation({"call_failed": [], "setup_failed": []}) is False
    assert 2 not in shared.VERDICT_BEARING_EXITS and 0 in shared.VERDICT_BEARING_EXITS


# --------------------------------------------------------------------------------------------
# Round-3 review findings: "the first match decides", for the third time
# --------------------------------------------------------------------------------------------
# Round 5: the grammar is the ENGINE's set; everything else is UNASSESSABLE
# --------------------------------------------------------------------------------------------

R5_AXIS = ("Orders", "Order Month Label")
R5_BARE = (
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
    "'Orders'[Order_Date] <= MAX('Orders'[Order_Date])))"
)
# Finding 1: residue stopped at the predicate, not the OPERAND, so a redundant paren around the
# COLUMN made the measure vanish. Measured before the fix: exit 0, against the bare form's exit 1.
R5_PAREN_OPERAND = R5_BARE.replace("'Orders'[Order_Date] <=", "('Orders'[Order_Date]) <=")
R5_EQ_TRUE = (
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
    "('Orders'[Order_Date] <= MAX('Orders'[Order_Date])) = TRUE()))"
)
# Finding 2, the worst of the round: a REMOVEFILTERS in an UNREACHABLE branch was unioned into the
# acquittal and returned OK on a broken measure. Measured before the fix: exit 0 / OK.
R5_UNREACHABLE_BRANCH = (
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
    "'Orders'[Order_Date] <= IF(TRUE(), MAX('Orders'[Order_Date]), "
    "CALCULATE(MAX('Orders'[Order_Date]), REMOVEFILTERS('Orders'[Order Month Label])))))"
)
R5_MIN_INLINE = (
    "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
    "'Orders'[Order_Date] <= MIN(MAX('Orders'[Order_Date]), DATE(2024,12,31))))"
)
R5_MIN_VARS = (
    "VAR _asOf = MAX('Orders'[Order_Date]) VAR _cut = DATE(2024,12,31) "
    "RETURN CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Order_Date]), "
    "'Orders'[Order_Date] <= MIN(_asOf, _cut)))"
)
# Finding 3: a legitimate TEXT measure whose STRING LITERAL contains DAX. Measured before the fix:
# exit 1 / MISMATCH - the gate firing on correct DAX.
R5_TEXT_FILTER = "\"Formula: FILTER(ALL('Orders'[Order_Date]), 'Orders'[Order_Date] <= MAX('Orders'[Order_Date]))\""
R5_TEXT_WINDOW = "\"See WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC)) for details\""


def _r5_exit(tmp_path: Path, slug: str, expression: str, axis: tuple[str, str] = R5_AXIS) -> int:
    """The FULL CLI verdict path for one expression, judged the way callers judge it."""
    bundle = _as_of_bundle(tmp_path / slug, axis[0], axis[1], expression)
    return crta.main([str(bundle), "--quiet"])


@pytest.mark.parametrize(
    ("label", "expression"),
    [
        ("bare", R5_BARE),
        ("a redundant paren around the column", R5_PAREN_OPERAND),
        ("wrapped in = TRUE()", R5_EQ_TRUE),
        ("a bound in an unreachable IF branch", R5_UNREACHABLE_BRANCH),
        ("MIN over two bounds, inlined", R5_MIN_INLINE),
        ("MIN over two bounds, via VARs", R5_MIN_VARS),
    ],
)
def test_an_as_of_measure_can_only_ever_be_unassessable(tmp_path: Path, label: str, expression: str) -> None:
    """THE round-5 contract, kept by round 6's construction rather than by a detector's discipline:
    no as-of shape is an engine template, so none of them can be judged.

    Six spellings share one test because their VERDICTS no longer depend on anything read out of
    the DAX. Every one used to disagree with at least one other - `('Orders'[Order_Date]) <= MAX(...)`
    exited 0 against the bare form's 1, and `IF(TRUE(), MAX(d), CALCULATE(MAX(d),
    REMOVEFILTERS(<the axis>)))` exited 0/OK on a genuinely broken measure."""
    bundle = _as_of_bundle(tmp_path, R5_AXIS[0], R5_AXIS[1], expression)
    report = crta.scan(bundle)
    assert report["pairs"][0]["cumulative_measures"] == 1, f"{label}: " + crta.render(report)
    assert verdicts(report) == ["unassessable"], f"{label}: " + crta.render(report)
    assert codes(report) == ["unrecognised"]
    assert report["mismatches"] == 0
    assert crta.main([str(bundle), "--quiet"]) == crta.EXIT_UNASSESSABLE


def test_an_as_of_measure_is_disclosed_not_dropped(tmp_path: Path) -> None:
    """`unassessable` is only worth anything if the measure REACHES the report. A detector that
    returns nothing produces NOT_APPLICABLE / exit 0, which reads as a clean bill - the silent-drop
    failure every round of this review has been about.

    This is also the standing answer to "does the gate still fire on the `ALL(t[Date])` month-axis
    case?". It is not a MISMATCH and has not been one since round 5; it is detected, named, and
    never cleared - exit 3, or exit 1 under `--strict`."""
    bundle = _as_of_bundle(tmp_path, R5_AXIS[0], R5_AXIS[1], R5_BARE)
    report = crta.scan(bundle)
    finding = report["pairs"][0]["findings"][0]
    assert finding["measure"] == "'_Measures'[Running Sales]"
    assert "an as-of accumulation" in finding["detail"]
    assert "EVALUATE" in finding["detail"]
    assert crta.main([str(bundle), "--quiet"]) == crta.EXIT_UNASSESSABLE
    assert crta.main([str(bundle), "--quiet", "--strict"]) == crta.EXIT_MISMATCH


@pytest.mark.parametrize(
    ("label", "expression"),
    [("a text measure quoting a FILTER", R5_TEXT_FILTER), ("a text measure quoting a WINDOW", R5_TEXT_WINDOW)],
)
def test_a_string_literal_is_never_executed_as_dax(tmp_path: Path, label: str, expression: str) -> None:
    """Finding 3 of round 5, VERBATIM, and the one that was wrong in the OTHER direction: a
    legitimate TEXT measure whose literal contains DAX was classified a running total and reported
    MISMATCH, exit 1. The lexer makes a string literal one atomic token, so nothing downstream can
    look inside it. Bound as a Tooltip, exactly as the reviewer measured it."""
    bundle = build_bundle(
        tmp_path,
        _measures_tmdl(_measure("Formula Note", expression)),
        {
            "Category": {"projections": [_column_projection(*R5_AXIS)]},
            "Tooltips": {"projections": [_measure_projection("_Measures", "Formula Note")]},
        },
    )
    report = crta.scan(bundle)
    assert report["status"] == crta.STATUS_NOT_APPLICABLE, f"{label}: " + crta.render(report)
    assert report["mismatches"] == 0
    assert crta.main([str(bundle), "--quiet"]) == crta.EXIT_OK


def test_an_operator_inside_an_identifier_is_not_a_comparison(tmp_path: Path) -> None:
    """A table may legally be named `'a<b'`. The lexer makes that ONE token, so the as-of signal -
    which reads operator PRESENCE only - cannot see a comparison inside a name. Without that, this
    ordinary equality filter would be disclosed as an accumulation."""
    expression = "CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Region]), 'Orders'[Region] = 'a<b'[X]))"
    report = crta.scan(_as_of_bundle(tmp_path, R5_AXIS[0], R5_AXIS[1], expression))
    assert report["status"] == crta.STATUS_NOT_APPLICABLE, crta.render(report)


@pytest.mark.parametrize(
    ("label", "predicate"),
    [
        ("<> is not an ordering operator", "'Orders'[Order_Date] <> MAX('Orders'[Order_Date])"),
        ("a plain equality filter", "'Orders'[Region] = \"West\""),
        ("the engine's FIXED-LOD conjunction", "'Orders'[Region] = _r && 'Orders'[Order_Date] = _d"),
    ],
)
def test_an_equality_only_filter_is_not_an_accumulation(tmp_path: Path, label: str, predicate: str) -> None:
    """The measured reason this detector is not merely `FILTER(`: **114 of the 526 measures in the
    committed `examples/` corpus are `FILTER(ALL(...))` calls and NOT ONE carries an ordering
    comparison** - they are the engine's cross-table FIXED LOD, whose predicate is an equality
    conjunction (`calc_to_dax.py:2328`). Disclosing those would be 114 exit-3s for nothing."""
    expression = (
        f"VAR _r = 1 VAR _d = 2 RETURN CALCULATE(SUM('Orders'[Sales]), FILTER(ALL('Orders'[Region]), {predicate}))"
    )
    report = crta.scan(_as_of_bundle(tmp_path, R5_AXIS[0], R5_AXIS[1], expression))
    assert report["status"] == crta.STATUS_NOT_APPLICABLE, f"{label}: " + crta.render(report)


def test_the_engines_own_running_total_shape_is_still_judged(tmp_path: Path) -> None:
    """The narrowing must not cost the gate its reason to exist. This is the byte shape
    `calc_to_dax.py:3548` documents for `RUNNING_SUM(<agg>)`, on the three axes of the measured S14
    table - and it is the ONLY family the engine emits for a cumulative measure."""
    engine_shape = "SUMX(WINDOW(1, ABS, 0, REL, ORDERBY('Orders'[Order_Date], ASC)), CALCULATE(SUM('Orders'[Sales])))"
    outcomes = {}
    for slug, axis in (("anchor", ("Orders", "Order_Date")), ("coarse", R5_AXIS), ("other", ("Date", "Date"))):
        outcomes[slug] = _r5_exit(tmp_path, slug, engine_shape, axis)
    assert outcomes == {"anchor": crta.EXIT_OK, "coarse": crta.EXIT_MISMATCH, "other": crta.EXIT_MISMATCH}


# The functions whose whole job is to FOLD every candidate, or to CONSUME every token. Round 6
# deleted most of what round 5 named here, because whole-expression matching removed the folds
# rather than making them careful.
#
# WARNING - **what this test canNOT see, stated because round 5 caught it claiming more than it
# delivers:** blind review showed the finding-2 class - candidates DISCARDED or UNIONED inside a
# loop, with no `return` anywhere - is structurally invisible to it. It pins one narrow habit; it is
# not evidence that a fold is correct, and it never was.
_FOLD_FUNCTIONS = (
    "_signals",
    "column_references",
)
_LEXER_FOLD_FUNCTIONS = (
    "tokenize",
    "calls_named",
)


def _returns_from_inside_a_loop(source: str, names: tuple[str, ...]) -> list[str]:
    """Every named function in `source` that returns from inside one of its own loops."""
    import ast  # pylint: disable=import-outside-toplevel

    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef) or node.name not in names:
            continue
        for loop in [n for n in ast.walk(node) if isinstance(n, (ast.For, ast.While))]:
            if any(isinstance(inner, ast.Return) for inner in ast.walk(loop)):
                offenders.append(node.name)
    return sorted(set(offenders))


@pytest.mark.parametrize(
    ("module", "names"),
    [("dax_grain.py", _FOLD_FUNCTIONS), ("dax_tokens.py", _LEXER_FOLD_FUNCTIONS)],
)
def test_no_fold_function_returns_from_inside_a_loop(module: str, names: tuple[str, ...]) -> None:
    """Rule 2, enforced instead of asserted. Both halves matter: the real module must be clean, AND
    the checker must be able to fail - an AST check that never flags anything is the "assertion in a
    branch the fixture never enters" vacuity mode, and it would sit here reading as coverage."""
    import ast  # pylint: disable=import-outside-toplevel

    source = (REPO_ROOT / "scripts" / module).read_text(encoding="utf-8")
    declared = {n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)}
    assert set(names) <= declared, f"{module}: the fold list names a function that no longer exists"
    assert _returns_from_inside_a_loop(source, names) == []

    planted = f"def {names[0]}(expr):\n    for part in expr:\n        return part\n    return None\n"
    assert _returns_from_inside_a_loop(planted, names) == [names[0]]


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
