"""Tests for advisory migration-bundle engine receipt drift detection."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_engine_receipts as receipts  # noqa: E402  # pylint: disable=wrong-import-position


def _write_receipt(bundle: Path, version: str) -> None:
    bundle.mkdir(parents=True)
    (bundle / "engine-output-receipt.json").write_text(
        json.dumps({"engine": {"version": version}}),
        encoding="utf-8",
    )


def test_a_stale_synthetic_bundle_names_the_bundle_and_both_engine_versions(tmp_path: Path, monkeypatch) -> None:
    """A receipt behind the installed plugin stays visible without blocking preflight."""
    bundle = tmp_path / "nested" / "stale-bundle"
    _write_receipt(bundle, "2.141.0")
    monkeypatch.setattr(receipts, "engine_root", lambda: tmp_path / "canonical-engine")
    monkeypatch.setattr(receipts, "engine_version", lambda _: "2.260.0")

    warnings = receipts.check_receipts(tmp_path)

    assert warnings == [f"{bundle}: receipt engine.version 2.141.0 is older than installed canonical engine 2.260.0"]


def test_version_order_uses_numeric_segments_not_lexicographic_strings(tmp_path: Path, monkeypatch) -> None:
    """2.113.0 is newer than 2.72.0 even though string ordering says the opposite."""
    bundle = tmp_path / "stale-bundle"
    _write_receipt(bundle, "2.72.0")
    monkeypatch.setattr(receipts, "engine_root", lambda: tmp_path / "canonical-engine")
    monkeypatch.setattr(receipts, "engine_version", lambda _: "2.113.0")

    warnings = receipts.check_receipts(tmp_path)

    assert warnings == [f"{bundle}: receipt engine.version 2.72.0 is older than installed canonical engine 2.113.0"]


def test_matching_receipts_are_silent(tmp_path: Path, monkeypatch) -> None:
    """Current bundles must not turn the advisory check into noise."""
    _write_receipt(tmp_path / "current-bundle", "2.260.0")
    monkeypatch.setattr(receipts, "engine_root", lambda: tmp_path / "canonical-engine")
    monkeypatch.setattr(receipts, "engine_version", lambda _: "2.260.0")

    assert receipts.check_receipts(tmp_path) == []
