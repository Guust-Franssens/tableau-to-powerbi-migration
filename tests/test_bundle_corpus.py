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


def test_evidence_dirs_searches_target_and_up_to_three_ancestor_levels(tmp_path: Path) -> None:
    """Pins ANCESTOR_LEVELS = 3: searches target (level 0) and 3 ancestors, excluding level 4+."""
    assert bundle_corpus.ANCESTOR_LEVELS == 3

    run_root = tmp_path / "run"
    bundle_dir = run_root / "bundle"
    pbip_dir = bundle_dir / "pbip"
    target = pbip_dir / "Minimal"

    target.mkdir(parents=True)
    (target / "oracle").mkdir()
    (pbip_dir / "oracle").mkdir()
    (bundle_dir / "oracle").mkdir()
    (run_root / "oracle").mkdir()
    (tmp_path / "oracle").mkdir()  # 4th ancestor above target

    found = bundle_corpus.evidence_dirs(target, ("oracle",))
    assert found == [
        target / "oracle",
        pbip_dir / "oracle",
        bundle_dir / "oracle",
        run_root / "oracle",
    ]
    assert (tmp_path / "oracle") not in found


def test_evidence_dirs_stops_ancestor_walk_when_target_is_self_contained(tmp_path: Path) -> None:
    """A self-contained package carries package-manifest.json and stops the ancestor walk."""
    run_root = tmp_path / "run"
    packages_dir = run_root / "packages"
    target = packages_dir / "Minimal"

    (target / "oracle").mkdir(parents=True)
    (packages_dir / "oracle").mkdir()
    (run_root / "oracle").mkdir()

    # Before manifest: un-packaged / incomplete target searches ancestors
    assert bundle_corpus.is_self_contained(target) is False
    assert bundle_corpus.evidence_dirs(target, ("oracle",)) == [
        target / "oracle",
        packages_dir / "oracle",
        run_root / "oracle",
    ]

    # After manifest: self-contained target searches only itself
    (target / bundle_corpus.PACKAGE_MARKER).write_text("{}", encoding="utf-8")
    assert bundle_corpus.is_self_contained(target) is True
    assert bundle_corpus.evidence_dirs(target, ("oracle",)) == [target / "oracle"]

