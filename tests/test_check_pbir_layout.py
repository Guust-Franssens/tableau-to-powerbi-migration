"""Tests for scripts/check_pbir_layout.py - the narrow PBIR displacement gate from #278.

Fixtures use the real emitted PBIR shape (`page.json` plus `visual.json` with top-level `position`).
The gate is intentionally column-specific: a full-height sidebar can hide the Y-range that the main
content vacated, so a page-wide max-Y check is not enough.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_pbir_layout as cpl  # noqa: E402  # pylint: disable=wrong-import-position


def _visual(name: str, x: float, y: float, width: float, height: float, visual_type: str = "shape") -> dict:
    """A minimal emitted-style PBIR visual container."""
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.9.0/schema.json",
        "name": name,
        "position": {"x": x, "y": y, "z": 1000, "width": width, "height": height, "tabOrder": 1000},
        "visual": {"visualType": visual_type},
    }


def _write_report(root: Path, visuals: list[dict], *, under_pbip: bool = False, height: float = 5011.0) -> Path:
    """Write one `.Report` fixture and return it."""
    base = root / "pbip" / "Book" if under_pbip else root
    report = base / "Book.Report"
    page = report / "definition" / "pages" / "p1"
    page.mkdir(parents=True)
    (page / "page.json").write_text(
        json.dumps(
            {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/1.4.0/schema.json",
                "name": "p1",
                "displayName": "Main",
                "displayOption": "FitToPage",
                "width": 1600,
                "height": height,
            }
        ),
        encoding="utf-8",
    )
    for index, visual in enumerate(visuals):
        folder = page / "visuals" / f"v{index:02d}"
        folder.mkdir(parents=True)
        (folder / "visual.json").write_text(json.dumps(visual), encoding="utf-8")
    return report


def _displaced_visuals() -> list[dict]:
    """Customer-shaped layout: sidebar fills the vacated top range, main column starts lower."""
    visuals = [_visual("sidebar", 0, 0, 260, 5011, "image")]
    visuals.extend(_visual(f"main{index}", 330, 851 + index * 210, 1040, 120, "cardVisual") for index in range(7))
    return visuals


def test_displaced_dense_main_column_fails_even_with_full_height_sidebar(tmp_path, capsys) -> None:
    """Kills: checking only page max-Y, which the sidebar satisfies."""
    report = _write_report(tmp_path, _displaced_visuals())
    result = cpl.scan(report)
    assert result["status"] == cpl.STATUS_DISPLACED
    finding = result["reports"][0]["findings"][0]
    assert finding["main_visuals"] == 7
    assert finding["leading_y"] == 851
    assert finding["sidebar"]["name"] == "sidebar"

    assert cpl.main([str(report)]) == cpl.EXIT_DISPLACED
    out = capsys.readouterr().out
    assert "DISPLACED_MAIN_COLUMN" in out and "y=851.00" in out


def test_bottom_dead_zone_alone_is_out_of_scope_and_exits_zero(tmp_path) -> None:
    """Kills: designing around the unresolved first #278 measurement instead of the correction."""
    visuals = [_visual(f"main{index}", 300, 80 + index * 240, 1000, 120, "cardVisual") for index in range(7)]
    report = _write_report(tmp_path, visuals, height=5011)
    result = cpl.scan(report)
    assert result["status"] == cpl.STATUS_OK
    assert result["reports"][0]["pages_scanned"] == 1


def test_single_spaced_visual_is_not_displacement(tmp_path) -> None:
    """A large deliberate gap before one visual is legitimate design, not the reported signature."""
    report = _write_report(
        tmp_path,
        [_visual("sidebar", 0, 0, 260, 5011, "image"), _visual("hero", 330, 900, 1040, 500, "cardVisual")],
    )
    assert cpl.scan(report)["status"] == cpl.STATUS_OK


def test_many_low_visuals_without_sidebar_is_not_this_defect(tmp_path) -> None:
    """The full-height separate sidebar is what makes a page-height check miss the defect."""
    visuals = [_visual(f"main{index}", 330, 851 + index * 210, 1040, 120, "cardVisual") for index in range(7)]
    report = _write_report(tmp_path, visuals)
    assert cpl.scan(report)["status"] == cpl.STATUS_OK


def test_bundle_scope_is_pbip_only_not_engine_baseline(tmp_path) -> None:
    """Kills: scanning `<bundle>/reports`, which is the non-shipping engine baseline."""
    _write_report(tmp_path, [_visual("ok", 100, 60, 900, 300)], under_pbip=True)
    baseline = tmp_path / "reports" / "Book.Report" / "definition" / "pages" / "p1"
    baseline.mkdir(parents=True)
    (baseline / "page.json").write_text(json.dumps({"name": "p1", "width": 1600, "height": 5011}), encoding="utf-8")
    for index, visual in enumerate(_displaced_visuals()):
        folder = baseline / "visuals" / f"v{index:02d}"
        folder.mkdir(parents=True)
        (folder / "visual.json").write_text(json.dumps(visual), encoding="utf-8")
    result = cpl.scan(tmp_path)
    assert result["status"] == cpl.STATUS_OK
    assert result["reports_scanned"] == 1


def test_json_and_warn_only_keep_machine_readable_finding(tmp_path) -> None:
    """The suppressing mode still writes the detected page/column."""
    report = _write_report(tmp_path, _displaced_visuals())
    out = tmp_path / "layout.json"
    assert cpl.main([str(report), "--json", str(out), "--quiet", "--warn-only"]) == cpl.EXIT_OK
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == cpl.STATUS_DISPLACED
    assert payload["reports"][0]["findings"][0]["display_name"] == "Main"


def test_missing_path_is_usage_error_not_a_verdict(tmp_path, capsys) -> None:
    """Kills: `rglob` over a typo producing OK or SKIPPED."""
    with pytest.raises(SystemExit) as exc:
        cpl.main([str(tmp_path / "missing")])
    assert exc.value.code == cpl.EXIT_USAGE
    assert "OK" not in capsys.readouterr().out


def test_no_reports_is_skipped_not_ok(tmp_path, capsys) -> None:
    """An affirmative verdict requires at least one positioned visual to be measured."""
    assert cpl.main([str(tmp_path)]) == cpl.EXIT_SKIPPED
    assert "SKIPPED" in capsys.readouterr().out
