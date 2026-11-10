"""check_pbir_layout: the column-specific PBIR displacement gate (#278)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_pbir_layout as cpl  # noqa: E402  # pylint: disable=wrong-import-position


def _visual(name: str, x: float, y: float, width: float, height: float, visual_type: str = "shape") -> dict:
    """A minimal emitted-style PBIR visual container."""
    position = {"x": x, "y": y, "z": 1000, "width": width, "height": height, "tabOrder": 1000}
    return {"name": name, "position": position, "visual": {"visualType": visual_type}}


def _write_report(root: Path, visuals: list[dict], *, folder: str | None = None) -> Path:
    """Write one `.Report` fixture (optionally under `<root>/<folder>/Book`) and return it."""
    report = (root / folder / "Book" if folder else root) / "Book.Report"
    page = report / "definition" / "pages" / "p1"
    page.mkdir(parents=True)
    meta = {"name": "p1", "displayName": "Main", "displayOption": "FitToPage", "width": 1600, "height": 5011}
    (page / "page.json").write_text(json.dumps(meta), encoding="utf-8")
    for index, visual in enumerate(visuals):
        (page / "visuals" / f"v{index:02d}").mkdir(parents=True)
        (page / "visuals" / f"v{index:02d}" / "visual.json").write_text(json.dumps(visual), encoding="utf-8")
    return report


SIDEBAR = _visual("sidebar", 0, 0, 260, 5011, "image")
LOW_COLUMN = [_visual(f"main{i}", 330, 851 + i * 210, 1040, 120, "cardVisual") for i in range(7)]


def test_displaced_dense_main_column_fails_even_with_full_height_sidebar(tmp_path, capsys) -> None:
    """Kills: checking only page max-Y, which the sidebar satisfies."""
    report = _write_report(tmp_path, [SIDEBAR, *LOW_COLUMN])
    result = cpl.scan(report)
    assert result["status"] == cpl.STATUS_DISPLACED
    finding = result["reports"][0]["findings"][0]
    assert (finding["main_visuals"], finding["leading_y"], finding["sidebar"]["name"]) == (7, 851, "sidebar")
    assert cpl.main([str(report)]) == cpl.EXIT_DISPLACED
    out = capsys.readouterr().out
    assert "DISPLACED_MAIN_COLUMN" in out and "y=851.00" in out
    payload_path = tmp_path / "layout.json"
    assert cpl.main([str(report), "--json", str(payload_path), "--quiet", "--warn-only"]) == cpl.EXIT_OK
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["status"] == cpl.STATUS_DISPLACED
    assert payload["reports"][0]["findings"][0]["display_name"] == "Main"


@pytest.mark.parametrize(
    "visuals",
    [
        pytest.param([_visual(f"m{i}", 300, 80 + i * 240, 1000, 120) for i in range(7)], id="bottom-dead-zone"),
        pytest.param([SIDEBAR, _visual("hero", 330, 900, 1040, 500)], id="single-spaced-visual"),
        pytest.param(LOW_COLUMN, id="no-sidebar"),
    ],
)
def test_non_displacement_shapes_are_ok(tmp_path, visuals) -> None:
    """Dead zone below content, one deliberate gap, or no sidebar are not the #278 signature."""
    result = cpl.scan(_write_report(tmp_path, visuals))
    assert result["status"] == cpl.STATUS_OK and result["reports"][0]["pages_scanned"] == 1


def test_bundle_scope_is_pbip_only_not_engine_baseline(tmp_path) -> None:
    """Kills: scanning `<bundle>/reports`, which is the non-shipping engine baseline."""
    _write_report(tmp_path, [_visual("ok", 100, 60, 900, 300)], folder="pbip")
    _write_report(tmp_path / "reports", [SIDEBAR, *LOW_COLUMN])
    result = cpl.scan(tmp_path)
    assert result["status"] == cpl.STATUS_OK and result["reports_scanned"] == 1


def test_missing_path_is_usage_and_no_reports_is_skipped(tmp_path, capsys) -> None:
    """Neither a typo nor an empty folder may produce an affirmative verdict."""
    with pytest.raises(SystemExit) as exc:
        cpl.main([str(tmp_path / "missing")])
    assert exc.value.code == cpl.EXIT_USAGE
    assert "OK" not in capsys.readouterr().out
    assert cpl.main([str(tmp_path)]) == cpl.EXIT_SKIPPED
    assert "SKIPPED" in capsys.readouterr().out
