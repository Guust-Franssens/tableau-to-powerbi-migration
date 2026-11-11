"""Tests for scripts/build_synthetic_reference.py — the honestly-labeled synthetic reference chart."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
import build_synthetic_reference as bsr


def _fake_png(path: Path, width: int = 900, height: int = 400) -> None:
    """Write a minimal-but-valid PNG (per _png_dimensions/_is_valid_png) padded past the size floor."""
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + b"\x00" * 21000)


@pytest.fixture(autouse=True)
def _stub_screenshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real Playwright/node capture with a fake-PNG writer in every test in this file."""

    def _fake_screenshot(_html: str, out_path: Path) -> bool:
        _fake_png(out_path)
        return True

    monkeypatch.setattr(bsr, "screenshot_html", _fake_screenshot)


def _rows_file(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


REAL_ROWS = [
    {"label": "Northwind Traders", "value": 323450.02},
    {"label": "Litware Inc", "value": 196466.72},
    {"label": "Contoso Ltd", "value": 237711.08},
]


def test_render_html_sorts_descending_escapes_labels_and_keeps_the_value_inside_the_row() -> None:
    """The chart must sort by value descending, largest bar first, like a typical BI bar chart, and a label
    containing HTML-significant characters must not break out of its element."""
    html = bsr.render_html("Billing by Customer", "Overview", REAL_ROWS, "$")
    assert html.index("Northwind Traders") < html.index("Contoso Ltd") < html.index("Litware Inc")
    assert "$323,450.02" in html
    assert "position: absolute" not in html
    assert 'class="barwrap"><div class="bar" style="width:100.00%"></div></div><div class="value">' in html

    html = bsr.render_html("T", "D", [{"label": "A & B <script>", "value": 1.0}], "")
    assert "<script>" not in html
    assert "A &amp; B &lt;script&gt;" in html


def test_load_rows_rejects_a_non_list_or_rows_missing_keys(tmp_path: Path) -> None:
    """A malformed data file must fail loudly, not silently produce zero rows or crash in rendering."""
    for payload in ({"label": "x", "value": 1}, [{"label": "only label"}]):
        path = tmp_path / "rows.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SystemExit):
            bsr._load_rows(path)  # pylint: disable=protected-access


def test_build_writes_a_synthetic_manifest_but_replaces_a_real_capture_only_with_force(tmp_path: Path) -> None:
    """A fresh or structural-only (empty ``dashboards``) manifest has nothing real to lose; a manifest
    with real dashboard states must not be silently replaced, and --force is the named escape hatch."""
    slug_dir = tmp_path / "variant-x"
    slug_dir.mkdir()
    manifest_path = slug_dir / "reference" / "manifest.json"
    data_file = _rows_file(tmp_path, REAL_ROWS)

    def provider() -> str:
        return json.loads(manifest_path.read_text(encoding="utf-8"))["dashboards"][0]["states"][0]["provider"]

    assert bsr.build(_args(slug_dir, data_file)) == 0
    state = json.loads(manifest_path.read_text(encoding="utf-8"))["dashboards"][0]["states"][0]
    assert state["provider"] == bsr.PROVIDER_SYNTHETIC
    assert state["capabilities"] == []
    assert state["synthetic"] is True
    assert {r["label"] for r in state["numeric_oracle"]["rows"]} == {r["label"] for r in REAL_ROWS}

    manifest_path.write_text(json.dumps({"captured_at": "2026-01-01T00:00:00Z", "dashboards": []}), encoding="utf-8")
    assert bsr.build(_args(slug_dir, data_file)) == 0
    assert provider() == bsr.PROVIDER_SYNTHETIC

    real_manifest = {
        "captured_at": "2026-01-01T00:00:00Z",
        "source_workbook_sha256": "abc",
        "dashboards": [{"name": "Overview", "states": [{"provider": "public_playwright"}]}],
    }
    manifest_path.write_text(json.dumps(real_manifest), encoding="utf-8")
    assert bsr.build(_args(slug_dir, data_file)) == 1
    assert provider() == "public_playwright"
    assert bsr.build(_args(slug_dir, data_file, force=True)) == 0
    assert provider() == bsr.PROVIDER_SYNTHETIC


def _args(slug_dir: Path, data_file: Path, force: bool = False) -> argparse.Namespace:
    """argparse.Namespace is simpler and more transparent here than reaching into main()'s parser."""
    return argparse.Namespace(
        slug_dir=str(slug_dir),
        data_file=str(data_file),
        title="Billing by Customer",
        dashboard_name="Overview",
        oracle_source="test",
        unit="$",
        force=force,
    )
