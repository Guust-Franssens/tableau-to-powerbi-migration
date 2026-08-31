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


# --- review findings #1-#3: the proxies that used to decide "safe" --------------------------


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
    """THE round-5 contract, and the reason six spellings share one test: their VERDICTS no longer
    depend on anything this module reads out of the DAX. Every one of them used to disagree with at
    least one other - `('Orders'[Order_Date]) <= MAX(...)` exited 0 against the bare form's 1, and
    `IF(TRUE(), MAX(d), CALCULATE(MAX(d), REMOVEFILTERS(<the axis>)))` exited 0/OK on a genuinely
    broken measure. There is nothing left to disagree about: an as-of restriction is DISCLOSED."""
    bundle = _as_of_bundle(tmp_path, R5_AXIS[0], R5_AXIS[1], expression)
    report = crta.scan(bundle)
    assert report["pairs"][0]["cumulative_measures"] == 1, f"{label}: " + crta.render(report)
    assert verdicts(report) == ["unassessable"], f"{label}: " + crta.render(report)
    assert codes(report) == ["as_of_filter"]
    assert report["mismatches"] == 0
    assert crta.main([str(bundle), "--quiet"]) == crta.EXIT_UNASSESSABLE


def test_an_as_of_measure_is_disclosed_not_dropped(tmp_path: Path) -> None:
    """`unassessable` is only worth anything if the measure REACHES the report. A detector that
    returns nothing produces NOT_APPLICABLE / exit 0, which reads as a clean bill - the silent-drop
    failure every round of this review has been about."""
    report = crta.scan(_as_of_bundle(tmp_path, R5_AXIS[0], R5_AXIS[1], R5_BARE))
    finding = report["pairs"][0]["findings"][0]
    assert finding["measure"] == "'_Measures'[Running Sales]"
    assert "not judge a hand-authored as-of bound" in finding["detail"]
    assert "EVALUATE" in finding["detail"]
    assert finding["predicate"].startswith("ALL('Orders'[Order_Date])")


@pytest.mark.parametrize(
    ("label", "expression"),
    [("a text measure quoting a FILTER", R5_TEXT_FILTER), ("a text measure quoting a WINDOW", R5_TEXT_WINDOW)],
)
def test_a_string_literal_is_never_executed_as_dax(tmp_path: Path, label: str, expression: str) -> None:
    """Finding 3, VERBATIM, and the one that was wrong in the OTHER direction: a legitimate TEXT
    measure whose literal contains DAX was classified a running total and reported MISMATCH, exit 1.
    `_call_bodies` cannot fix this itself - it regex-matches a function name over raw text and only
    then starts tracking quotes - so `mask_noncode` runs once at `classify`'s entry and every reader
    sees masked text. Bound as a Tooltip, exactly as the reviewer measured it."""
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


def test_the_lexer_masks_what_it_must_and_keeps_what_it_must() -> None:
    """`mask_noncode` unit-tested directly, because every claim above rests on it.

    The quotes are blanked WITH their contents, not left standing: `_split_arguments` tracks `"`
    state itself, so a half-masked literal - contents gone, delimiters kept - would be worse than
    either extreme. The two identifier cases are not decoration either: a `"` inside a legal column
    name would open a phantom literal and mask the rest of the expression, and the column
    references this module exists to read live inside exactly the brackets a naive mask would blank.
    """
    assert dg.mask_noncode('A & "FILTER(x)" & B') == "A &             & B"
    assert dg.mask_noncode('A & "he said ""hi""" & B') == "A &                  & B"
    assert dg.mask_noncode("A -- FILTER(x)\nB") == "A             \nB"
    assert dg.mask_noncode("A /* FILTER(x) */ B") == "A                 B"
    assert dg.mask_noncode("A // FILTER(x)") == "A             "
    # identifiers survive, contents intact, including a quote inside a column name
    kept = "'T'[He said \"hi\"] <= MAX('T'[He said \"hi\"])"
    assert dg.mask_noncode(kept) == kept
    assert len(dg.mask_noncode('x"abc"y')) == len('x"abc"y')


def test_an_operator_inside_an_identifier_is_not_a_comparison(tmp_path: Path) -> None:
    """A table may legally be named `'a<b'`. The detector reads operator PRESENCE only, so it gets
    the stricter `_mask_identifiers`, which blanks identifier contents that `mask_noncode` must
    keep. Without it this ordinary equality filter would be disclosed as an accumulation."""
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


# The functions whose whole job is to FOLD every candidate. The list is much shorter than round 4's
# because round 5 deleted the machinery most of it named.
#
# ⚠️ **What this test canNOT see, stated because round 5 caught it claiming more than it delivers:**
# blind review showed the finding-2 class - candidates DISCARDED or UNIONED inside a loop, with no
# `return` anywhere - is structurally invisible to it. It pins one narrow habit; it is not evidence
# that a fold is correct, and it never was.
_FOLD_FUNCTIONS = (
    "_clause_bodies",
    "_window_call_sites",
    "_detect_as_of",
    "_classify_period_to_date",
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


def test_no_fold_function_returns_from_inside_a_loop() -> None:
    """Rule 2, enforced instead of asserted. Both halves matter: the real module must be clean, AND
    the checker must be able to fail - an AST check that never flags anything is the "assertion in a
    branch the fixture never enters" vacuity mode, and it would sit here reading as coverage."""
    import ast  # pylint: disable=import-outside-toplevel

    source = (REPO_ROOT / "scripts" / "dax_grain.py").read_text(encoding="utf-8")
    declared = {n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)}
    assert set(_FOLD_FUNCTIONS) <= declared, "the fold list names a function that no longer exists"
    assert _returns_from_inside_a_loop(source, _FOLD_FUNCTIONS) == []

    planted = "def _detect_as_of(expr):\n    for part in expr:\n        return part\n    return None\n"
    assert _returns_from_inside_a_loop(planted, _FOLD_FUNCTIONS) == ["_detect_as_of"]


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
