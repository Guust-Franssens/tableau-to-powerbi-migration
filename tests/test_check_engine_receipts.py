"""Advisory migration-bundle engine receipt drift detection."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_engine_receipts as receipts  # noqa: E402  # pylint: disable=wrong-import-position


@pytest.mark.parametrize(
    ("receipt", "installed", "stale"),
    [("2.141.0", "2.260.0", True), ("2.72.0", "2.113.0", True), ("2.260.0", "2.260.0", False)],
    ids=["stale", "numeric-not-lexicographic", "current-is-silent"],
)
def test_receipt_older_than_installed_engine_warns(tmp_path: Path, monkeypatch, receipt, installed, stale) -> None:
    """A nested stale receipt names the bundle and both versions; a current one stays silent."""
    bundle = tmp_path / "nested" / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "engine-output-receipt.json").write_text(json.dumps({"engine": {"version": receipt}}), encoding="utf-8")
    monkeypatch.setattr(receipts, "engine_root", lambda: tmp_path / "canonical-engine")
    monkeypatch.setattr(receipts, "engine_version", lambda _: installed)
    expected = f"{bundle}: receipt engine.version {receipt} is older than installed canonical engine {installed}"
    assert receipts.check_receipts(tmp_path) == ([expected] if stale else [])
