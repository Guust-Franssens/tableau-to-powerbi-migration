"""check_blank_placeholders: handover-backed BLANK() placeholders.

`report.json` (`workbooks[].model_translation_handoff`) is the PRIMARY source and is on disk when the
gate runs (phase 2); the `handover/*.json` slices are written in phase 3 and are only a fallback.
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


def _write_handover(bundle: Path, workbook: str, needs_review: list[dict], **extra) -> None:
    payload = {"workbook": {"model_translation_handoff": {"needs_review": needs_review, **extra}}}
    (bundle / "handover" / f"{workbook}.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_report_json(bundle: Path, workbooks: list[dict]) -> None:
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "report.json").write_text(json.dumps({"workbooks": workbooks}), encoding="utf-8")


def _write_model(bundle: Path, workbook: str, table: str, body: str) -> None:
    model = bundle / "pbip" / workbook / f"{workbook}.SemanticModel" / "definition" / "tables"
    model.mkdir(parents=True, exist_ok=True)
    (model / f"{table}.tmdl").write_text(body, encoding="utf-8")


def _column_table(table: str = "Sales", name: str = "Flag", expr: str = "BLANK()") -> str:
    return (
        f"table {table}\n\n\tcolumn '{name}' = {expr}\n\t\tsummarizeBy: none\n"
        "\t\tannotation TableauFormula = IF [x] THEN 1 ELSE 0 END\n"
    )


MEASURE_TABLE = "table _Measures\n\n\tmeasure 'Group Sort' = BLANK()\n\t\tannotation TableauFormula = CASE [Sort] WHEN 'x' THEN 1 END\n"


def _refused(name: str = "Flag", table: str | None = "Sales", role: str = "dimension") -> dict:
    entry = {"category": "type_or_shape_mismatch", "fallback_reason": "IFNULL", "has_suggestion": False}
    entry |= {"name": name, "role": role}
    return entry | ({"target_table": table} if table else {})


def _write_visual(bundle: Path, workbook: str, page_id: str, visual: dict) -> None:
    page = bundle / "pbip" / workbook / f"{workbook}.Report" / "definition" / "pages" / page_id
    (page / "visuals" / "v").mkdir(parents=True)
    (page / "page.json").write_text(json.dumps({"name": page_id, "displayName": page_id}), encoding="utf-8")
    (page / "visuals" / "v" / "visual.json").write_text(json.dumps({"name": "v", **visual}), encoding="utf-8")


def _write_filter_visual(bundle: Path, workbook: str) -> None:
    left = {"Column": {"Expression": {"SourceRef": {"Source": "f"}}, "Property": "Flag"}}
    where = {"Condition": {"Comparison": {"ComparisonKind": 0, "Left": left, "Right": {"Literal": {"Value": "1L"}}}}}
    flt = {"Version": 2, "From": [{"Name": "f", "Entity": "Sales", "Type": 0}], "Where": [where]}
    visual = {
        "visual": {"visualType": "tableEx"},
        "objects": {"general": [{"properties": {"filter": {"filter": flt}}}]},
    }
    _write_visual(bundle, workbook, "page-1", visual)


def _write_binding_visual(bundle: Path, workbook: str) -> None:
    field = {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}}, "Property": "Group Sort"}}
    state = {"Tooltips": {"projections": [{"field": field, "queryRef": "_Measures.Group Sort"}]}}
    visual = {"visual": {"visualType": "clusteredBarChart", "query": {"queryState": state}}}
    _write_visual(bundle, workbook, "page-2", visual)


def test_correlation_requires_both_a_handover_entry_and_a_blank_body(tmp_path: Path) -> None:
    """A hand-authored BLANK() without a handover, or a handover without a BLANK() body, is OK."""
    bare = _bundle(tmp_path / "bare")
    _write_model(bare, "Workbook", "Sales", _column_table())
    assert cbp.scan(bare)["status"] == cbp.STATUS_OK and cbp.scan(bare)["placeholders_found"] == 0
    assert cbp.main([str(bare), "--quiet"]) == cbp.EXIT_OK
    assert cbp.main(["--bundle", str(bare), "--quiet"]) == cbp.EXIT_OK
    no_body = _bundle(tmp_path / "nobody")
    _write_handover(no_body, "Workbook", [_refused()])
    _write_model(no_body, "Workbook", "Sales", _column_table(expr="1"))
    assert cbp.scan(no_body)["status"] == cbp.STATUS_OK


def test_filter_dependency_escalates_above_an_unreferenced_placeholder(tmp_path: Path) -> None:
    """A filter on one placeholder is material; another placeholder is merely a documented gap."""
    bundle = _bundle(tmp_path)
    _write_handover(bundle, "Workbook", [_refused(), _refused(name="Unused")])
    _write_model(bundle, "Workbook", "Sales", _column_table() + "\n" + _column_table(name="Unused"))
    _write_filter_visual(bundle, "Workbook")
    report = cbp.scan(bundle)
    assert report["status"] == cbp.STATUS_REFERENCED
    by_name = {finding["name"]: finding for finding in report["findings"]}
    assert by_name["Flag"]["severity"] == cbp.STATUS_REFERENCED
    assert by_name["Flag"]["material_dependencies"][0]["usage"] == "filter"
    assert (by_name["Unused"]["severity"], by_name["Unused"]["material_dependencies"]) == (cbp.STATUS_UNREFERENCED, [])
    assert cbp.main([str(bundle), "--quiet"]) == cbp.EXIT_REFERENCED


def test_real_bundle_208_shape_correlates_dedupes_and_counts_a_visual_binding(tmp_path: Path) -> None:
    """No target_table (table from TMDL), the same fallback in needs_review AND requests counts once."""
    bundle = _bundle(tmp_path)
    request = _refused("Group Sort", "_Measures", "measure") | {"formula": "CASE [Sort] WHEN 'x' THEN 1 END"}
    _write_handover(bundle, "Admin", [_refused("Group Sort", None, "measure")], requests=[request])
    _write_model(bundle, "Admin", "_Measures", MEASURE_TABLE)
    report = cbp.scan(bundle)
    assert report["status"] == cbp.STATUS_UNREFERENCED
    assert (report["placeholders_found"], report["placeholders_referenced"]) == (1, 0)
    finding = report["findings"][0]
    assert finding["table"] == "_Measures" and finding["handover"]["handover_path"] == "handover/Admin.json"
    assert finding["handover"]["target_table"] == "_Measures"
    assert cbp.main([str(bundle), "--quiet"]) == cbp.EXIT_UNREFERENCED
    _write_binding_visual(bundle, "Admin")
    report = cbp.scan(bundle)
    assert report["status"] == cbp.STATUS_REFERENCED
    assert report["findings"][0]["material_dependencies"][0]["usage"] == "visual_field"


def test_report_json_is_primary_and_names_the_pbip_folder_owner(tmp_path: Path) -> None:
    """A fresh phase-2 bundle has no `handover/`; `pbip_folder` keys the TMDL side, not the name."""
    bundle = tmp_path / "fresh"
    _write_report_json(bundle, [{"name": "Workbook", "model_translation_handoff": {"requests": [_refused()]}}])
    _write_model(bundle, "Workbook", "Sales", _column_table())
    _write_filter_visual(bundle, "Workbook")
    report = cbp.scan(bundle)
    assert not (bundle / "handover").exists()
    assert (report["status"], report["placeholders_found"]) == (cbp.STATUS_REFERENCED, 1)
    assert report["findings"][0]["handover"]["handover_path"] == "report.json"

    renamed = tmp_path / "renamed"
    entry = {"name": "Workbook (Live)", "pbip_folder": "pbip/Workbook_Live_/Workbook_Live_.pbip"}
    _write_report_json(renamed, [entry | {"model_translation_handoff": {"requests": [_refused()]}}])
    _write_model(renamed, "Workbook_Live_", "Sales", _column_table())
    report = cbp.scan(renamed)
    assert report["placeholders_found"] == 1 and report["findings"][0]["owner"] == "Workbook_Live_"


def test_slices_are_a_fallback_only_when_report_json_is_absent_or_unreadable(tmp_path: Path) -> None:
    """Stale slices cannot outlive a report.json that says "none"; an unreadable one degrades loudly."""
    stale = _bundle(tmp_path / "stale")
    _write_handover(stale, "Workbook", [_refused()])
    _write_report_json(stale, [{"name": "Workbook", "model_translation_handoff": {"requests": []}}])
    _write_model(stale, "Workbook", "Sales", _column_table())
    _write_filter_visual(stale, "Workbook")
    assert cbp.scan(stale)["status"] == cbp.STATUS_OK and cbp.scan(stale)["placeholders_found"] == 0

    legacy = _bundle(tmp_path / "legacy")
    _write_handover(legacy, "Workbook", [_refused()])
    _write_model(legacy, "Workbook", "Sales", _column_table())
    report = cbp.scan(legacy)
    assert report["status"] == cbp.STATUS_UNREFERENCED
    assert report["findings"][0]["handover"]["handover_path"] == "handover/Workbook.json"

    (legacy / "report.json").write_text('{"workbooks": [', encoding="utf-8")
    report = cbp.scan(legacy)
    assert (report["placeholders_found"], report["handover_unreadable_paths"]) == (1, ["report.json"])
    assert "report.json" in cbp.render(report)


def test_a_truncated_slice_is_counted_rather_than_raised(tmp_path: Path) -> None:
    """A corrupt input is visible evidence, never an exception and never an invented placeholder."""
    bundle = _bundle(tmp_path)
    (bundle / "handover" / "Truncated.json").write_text('{"workbook": {', encoding="utf-8")
    _write_model(bundle, "Workbook", "Sales", _column_table())
    report = cbp.scan(bundle)
    assert (report["status"], report["handover_unreadable"]) == (cbp.STATUS_OK, 1)
    assert cbp.main([str(bundle), "--quiet"]) == cbp.EXIT_OK
    _write_handover(bundle, "Workbook", [_refused()])
    report = cbp.scan(bundle)
    assert report["placeholders_found"] == 1
    assert report["handover_unreadable_paths"] == ["handover/Truncated.json"]
    assert "Truncated.json" in cbp.render(report)


def test_unchecked_scope_is_incomplete_not_clean(tmp_path: Path) -> None:
    """A null handoff (#276), a shared-datasource owner, or a cache-only model is NOT CHECKED."""
    null = tmp_path / "null"
    _write_report_json(null, [{"name": "Workbook", "model_translation_handoff": None}])
    _write_model(null, "Workbook", "Sales", _column_table())
    report = cbp.scan(null)
    assert report["status"] == cbp.STATUS_INCOMPLETE and report["placeholders_found"] == 0
    assert (report["workbooks_unchecked"], report["workbooks_unchecked_blank_objects"]) == (1, 1)
    assert "NOT CHECKED" in cbp.render(report)
    assert cbp.main([str(null), "--quiet"]) == cbp.EXIT_INCOMPLETE

    shared = tmp_path / "shared"
    _write_report_json(shared, [{"name": "Workbook", "model_translation_handoff": {"requests": []}}])
    _write_model(shared, "Workbook", "Sales", _column_table(expr="1"))
    _write_model(shared, "DS_Flight_Level", "Flights", _column_table(table="Flights"))
    report = cbp.scan(shared)
    assert report["status"] == cbp.STATUS_INCOMPLETE
    assert (report["skipped_shared_datasources"], report["skipped_shared_datasource_blank_objects"]) == (1, 1)
    assert report["skipped_shared_datasource_details"] == [{"owner": "DS_Flight_Level", "blank_objects": 1}]
    assert "outside this handover check - NOT CHECKED" in cbp.render(report)

    cache_only = tmp_path / "CacheOnly.SemanticModel"
    cache_only.mkdir()
    report = cbp.scan(cache_only)
    assert report["status"] == cbp.STATUS_INCOMPLETE and report["nothing_measured"] is True
    assert cbp.main([str(cache_only), "--quiet"]) == cbp.EXIT_INCOMPLETE
