"""Tests for shared shipping-artifact discovery used by the check gates."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bundle_corpus  # noqa: E402  # pylint: disable=wrong-import-position


def test_shipping_reports_use_pbip_not_engine_baseline(tmp_path: Path) -> None:
    """Kills: adding a fifth copy that scans the pristine, non-shipping reports/ baseline."""
    shipping = tmp_path / "pbip" / "Book" / "Book.Report"
    baseline = tmp_path / "reports" / "Book.Report"
    shipping.mkdir(parents=True)
    baseline.mkdir(parents=True)

    assert bundle_corpus.shipping_reports(tmp_path) == [shipping.resolve()]


def test_shipping_models_exclude_standalone_by_default_and_can_include_it(tmp_path: Path) -> None:
    """Most gates skip semantic_models/; empty-model keeps datasource-only models measurable."""
    shipping = tmp_path / "pbip" / "Book" / "Book.SemanticModel"
    standalone = tmp_path / "semantic_models" / "Source.SemanticModel"
    shipping.mkdir(parents=True)
    standalone.mkdir(parents=True)

    assert bundle_corpus.shipping_models(tmp_path) == [shipping.resolve()]
    assert bundle_corpus.shipping_models(tmp_path, include_standalone=True) == [
        shipping.resolve(),
        standalone.resolve(),
    ]
