"""Tests for scripts/check_blank_placeholders.py - handover-backed BLANK() placeholders.

The handover fixture shape is copied from `_bundle-208/handover/Admin_Insights_Starter.json`:
`workbook.model_translation_handoff.needs_review[]` entries carry `category`, `fallback_reason`,
`has_suggestion`, `name`, and `role`; column entries in that bundle do not always carry
`target_table`, so the checker must recover the table from TMDL.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_blank_placeholders as cbp  # noqa: E402  # pylint: disable=wrong-import-position


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "handover").mkdir(parents=True)
    return bundle


def _write_handover(bundle: Path, workbook: str, needs_review: list[dict]) -> None:
    payload = {"workbook": {"model_translation_handoff": {"needs_review": needs_review}}}
    (bundle / "handover" / f"{workbook}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_model(bundle: Path, workbook: str, table: str, body: str) -> None:
    model = bundle / "pbip" / workbook / f"{workbook}.SemanticModel" / "definition" / "tables"
    model.mkdir(parents=True, exist_ok=True)
    (model / f"{table}.tmdl").write_text(body, encoding="utf-8")


def _column_table(table: str = "Sales", name: str = "Flag", expr: str = "BLANK()") -> str:
    return (
        f"table {table}\n\n"
        f"\tcolumn '{name}' = {expr}\n"
        "\t\tlineageTag: 11111111-1111-1111-1111-111111111111\n"
        "\t\tsummarizeBy: none\n"
        "\t\tannotation TableauFormula = IF [x] THEN 1 ELSE 0 END\n"
    )


def _measure_table(name: str = "Group Sort", expr: str = "BLANK()") -> str:
    return (
        "table _Measures\n\n"
        f"\tmeasure '{name}' = {expr}\n"
        "\t\tlineageTag: 22222222-2222-2222-2222-222222222222\n"
        "\t\tannotation TableauFormula = CASE [Sort] WHEN 'x' THEN 1 END\n"
    )


def _write_page(report: Path, page_id: str = "page-1", display_name: str = "Overview") -> Path:
    page = report / "definition" / "pages" / page_id
    page.mkdir(parents=True, exist_ok=True)
    (page / "page.json").write_text(json.dumps({"name": page_id, "displayName": display_name}), encoding="utf-8")
    return page


def _write_filter_visual(bundle: Path, workbook: str, table: str = "Sales", column: str = "Flag") -> None:
    report = bundle / "pbip" / workbook / f"{workbook}.Report"
    visual_dir = _write_page(report) / "visuals" / "visual-filter"
    visual_dir.mkdir(parents=True)
    visual = {
        "name": "visual-filter",
        "visual": {"visualType": "tableEx"},
        "objects": {
            "general": [
                {
                    "properties": {
                        "filter": {
                            "filter": {
                                "Version": 2,
                                "From": [{"Name": "f", "Entity": table, "Type": 0}],
                                "Where": [
                                    {
                                        "Condition": {
                                            "Comparison": {
                                                "ComparisonKind": 0,
                                                "Left": {
                                                    "Column": {
                                                        "Expression": {"SourceRef": {"Source": "f"}},
                                                        "Property": column,
                                                    }
                                                },
                                                "Right": {"Literal": {"Value": "1L"}},
                                            }
                                        }
                                    }
                                ],
                            }
                        }
                    }
                }
            ]
        },
    }
    (visual_dir / "visual.json").write_text(json.dumps(visual, indent=2), encoding="utf-8")


def _write_binding_visual(bundle: Path, workbook: str, measure: str = "Group Sort") -> None:
    report = bundle / "pbip" / workbook / f"{workbook}.Report"
    visual_dir = _write_page(report, "page-2", "Group Drilldown") / "visuals" / "visual-binding"
    visual_dir.mkdir(parents=True)
    visual = {
        "name": "visual-binding",
        "visual": {
            "visualType": "clusteredBarChart",
            "query": {
                "queryState": {
                    "Tooltips": {
                        "projections": [
                            {
                                "field": {
                                    "Measure": {
                                        "Expression": {"SourceRef": {"Entity": "_Measures"}},
                                        "Property": measure,
                                    }
                                },
                                "queryRef": f"_Measures.{measure}",
                            }
                        ]
                    }
                }
            },
        },
    }
    (visual_dir / "visual.json").write_text(json.dumps(visual, indent=2), encoding="utf-8")


def test_intentional_hand_authored_blank_without_handover_is_not_flagged(tmp_path: Path) -> None:
    """A bare BLANK() is legitimate; the handover correlation prevents false positives."""
    bundle = _bundle(tmp_path)
    _write_model(bundle, "Workbook", "Sales", _column_table())

    report = cbp.scan(bundle)

    assert report["status"] == cbp.STATUS_OK
    assert report["placeholders_found"] == 0


def test_filter_dependency_escalates_above_an_unreferenced_placeholder(tmp_path: Path) -> None:
    """The exit-worthy case: a report filter depends on one placeholder; another is merely a gap."""
    bundle = _bundle(tmp_path)
    _write_handover(
        bundle,
        "Workbook",
        [
            {
                "category": "type_or_shape_mismatch",
                "fallback_reason": "IFNULL arguments return inconsistent types",
                "has_suggestion": False,
                "name": "Flag",
                "role": "dimension",
                "target_table": "Sales",
            },
            {
                "category": "type_or_shape_mismatch",
                "fallback_reason": "unsupported function WINDOW_SUM",
                "has_suggestion": False,
                "name": "Unused",
                "role": "dimension",
                "target_table": "Sales",
            },
        ],
    )
    _write_model(bundle, "Workbook", "Sales", _column_table() + "\n" + _column_table(name="Unused"))
    _write_filter_visual(bundle, "Workbook")

    report = cbp.scan(bundle)

    assert report["status"] == cbp.STATUS_REFERENCED
    by_name = {finding["name"]: finding for finding in report["findings"]}
    assert by_name["Flag"]["severity"] == cbp.STATUS_REFERENCED
    assert by_name["Flag"]["material_dependencies"][0]["usage"] == "filter"
    assert by_name["Unused"]["severity"] == cbp.STATUS_UNREFERENCED
    assert by_name["Unused"]["material_dependencies"] == []


def test_visual_field_binding_is_also_material(tmp_path: Path) -> None:
    """A BLANK() measure placed in a visual role is not just a model gap; the visual consumes it."""
    bundle = _bundle(tmp_path)
    _write_handover(
        bundle,
        "Admin_Insights_Starter",
        [
            {
                "category": "model_object_parameter",
                "fallback_reason": "bare row-level field [..] not valid in a measure",
                "has_suggestion": False,
                "name": "Group Sort",
                "role": "measure",
            }
        ],
    )
    _write_model(bundle, "Admin_Insights_Starter", "_Measures", _measure_table())
    _write_binding_visual(bundle, "Admin_Insights_Starter")

    report = cbp.scan(bundle)

    assert report["status"] == cbp.STATUS_REFERENCED
    assert report["findings"][0]["material_dependencies"][0]["usage"] == "visual_field"


def test_real_bundle_208_handover_shape_correlates_to_the_tmdl_table(tmp_path: Path) -> None:
    """The fixture is the real `_bundle-208` shape: no target_table, role=measure, nested path."""
    bundle = _bundle(tmp_path)
    _write_handover(
        bundle,
        "Admin_Insights_Starter",
        [
            {
                "category": "model_object_parameter",
                "fallback_reason": "bare row-level field [..] not valid in a measure",
                "has_suggestion": False,
                "name": "Group Sort",
                "role": "measure",
            }
        ],
    )
    _write_model(bundle, "Admin_Insights_Starter", "_Measures", _measure_table())

    report = cbp.scan(bundle)

    assert report["status"] == cbp.STATUS_UNREFERENCED
    assert report["findings"][0]["table"] == "_Measures"
    assert report["findings"][0]["handover"]["handover_path"] == "handover/Admin_Insights_Starter.json"


def test_real_handover_requests_and_needs_review_do_not_double_emit(tmp_path: Path) -> None:
    """Current handovers list the same fallback in `needs_review` and `requests`; count it once."""
    bundle = _bundle(tmp_path)
    _write_handover(
        bundle,
        "Admin_Insights_Starter",
        [
            {
                "category": "model_object_parameter",
                "fallback_reason": "bare row-level field [..] not valid in a measure",
                "has_suggestion": False,
                "name": "Group Sort",
                "role": "measure",
            }
        ],
    )
    payload = json.loads((bundle / "handover" / "Admin_Insights_Starter.json").read_text(encoding="utf-8"))
    payload["workbook"]["model_translation_handoff"]["requests"] = [
        {
            "category": "model_object_parameter",
            "fallback_reason": "bare row-level field [..] not valid in a measure",
            "has_suggestion": False,
            "name": "Group Sort",
            "role": "measure",
            "target_table": "_Measures",
            "formula": "CASE [Sort] WHEN 'x' THEN 1 END",
        }
    ]
    (bundle / "handover" / "Admin_Insights_Starter.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_model(bundle, "Admin_Insights_Starter", "_Measures", _measure_table())

    report = cbp.scan(bundle)
    distinct = {(finding["tmdl"], finding["name"], finding["table"]) for finding in report["findings"]}

    assert len(report["findings"]) == len(distinct) == 1
    assert report["placeholders_found"] == len(distinct)
    assert report["placeholders_referenced"] == 0
    assert report["findings"][0]["handover"]["target_table"] == "_Measures"


def test_handover_entry_without_blank_body_is_not_a_placeholder(tmp_path: Path) -> None:
    """The handover alone is not enough; the model must still carry the BLANK() body."""
    bundle = _bundle(tmp_path)
    _write_handover(
        bundle,
        "Workbook",
        [
            {
                "category": "type_or_shape_mismatch",
                "fallback_reason": "IFNULL arguments return inconsistent types",
                "has_suggestion": False,
                "name": "Flag",
                "role": "dimension",
                "target_table": "Sales",
            }
        ],
    )
    _write_model(bundle, "Workbook", "Sales", _column_table(expr="1"))

    assert cbp.scan(bundle)["status"] == cbp.STATUS_OK


def test_cli_exit_codes_distinguish_clean_gap_and_material_dependency(tmp_path: Path) -> None:
    """The process status is the gate: clean, documented gap, material PBIR dependency."""
    clean = _bundle(tmp_path / "clean")
    _write_model(clean, "Workbook", "Sales", _column_table())
    gap = _bundle(tmp_path / "gap")
    _write_handover(
        gap,
        "Workbook",
        [
            {
                "category": "type_or_shape_mismatch",
                "fallback_reason": "unsupported function INDEX",
                "has_suggestion": False,
                "name": "Flag",
                "role": "dimension",
                "target_table": "Sales",
            }
        ],
    )
    _write_model(gap, "Workbook", "Sales", _column_table())
    material = _bundle(tmp_path / "material")
    _write_handover(
        material,
        "Workbook",
        [
            {
                "category": "type_or_shape_mismatch",
                "fallback_reason": "IFNULL arguments return inconsistent types",
                "has_suggestion": False,
                "name": "Flag",
                "role": "dimension",
                "target_table": "Sales",
            }
        ],
    )
    _write_model(material, "Workbook", "Sales", _column_table())
    _write_filter_visual(material, "Workbook")

    assert cbp.main([str(clean), "--quiet"]) == cbp.EXIT_OK
    assert cbp.main([str(gap), "--quiet"]) == cbp.EXIT_UNREFERENCED
    assert cbp.main([str(material), "--quiet"]) == cbp.EXIT_REFERENCED
    assert cbp.main(["--bundle", str(clean), "--quiet"]) == cbp.EXIT_OK
