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


def test_is_package_target_recognizes_flat_nested_and_marked_packages(tmp_path: Path) -> None:
    """Package paths and explicit markers are recognized without treating fabric/ as sufficient."""
    flat_target = tmp_path / "run" / "packages" / "Minimal"
    nested_target = tmp_path / "run" / "packages" / "batch1" / "Minimal"
    unpackaged_unit = tmp_path / "run" / "bundle" / "pbip" / "Minimal"
    marked_target = tmp_path / "isolated" / "Minimal"
    (marked_target / "fabric").mkdir(parents=True)
    (marked_target / bundle_corpus.PACKAGE_MARKER).write_text("{}\n", encoding="utf-8")
    (unpackaged_unit / "fabric").mkdir(parents=True)

    assert bundle_corpus.is_package_target(flat_target) is True
    assert bundle_corpus.is_package_target(nested_target) is True
    assert bundle_corpus.is_package_target(marked_target) is True
    assert bundle_corpus.is_package_target(unpackaged_unit) is False


def test_evidence_dirs_prohibits_ancestor_evidence_for_flat_and_nested_packages(tmp_path: Path) -> None:
    """Flat and nested package targets never search ancestors, even without package-manifest.json."""
    run_root = tmp_path / "run"
    packages_dir = run_root / "packages"
    flat_target = packages_dir / "FlatUnit"
    nested_target = packages_dir / "batch1" / "NestedUnit"

    flat_target.mkdir(parents=True)
    nested_target.mkdir(parents=True)
    (run_root / "oracle").mkdir()

    # Without package-manifest.json and without local evidence: no ancestor evidence is inherited
    assert bundle_corpus.is_self_contained(flat_target) is False
    assert bundle_corpus.is_package_target(flat_target) is True
    assert not bundle_corpus.evidence_dirs(flat_target, ("oracle",))

    assert bundle_corpus.is_self_contained(nested_target) is False
    assert bundle_corpus.is_package_target(nested_target) is True
    assert not bundle_corpus.evidence_dirs(nested_target, ("oracle",))

    # When local evidence is present, only local evidence is returned
    (flat_target / "oracle").mkdir()
    assert bundle_corpus.evidence_dirs(flat_target, ("oracle",)) == [flat_target / "oracle"]


def test_evidence_dirs_searches_ancestors_only_for_unpackaged_units(tmp_path: Path) -> None:
    """An unpackaged unit under bundle/pbip/<Unit> still inherits run-level ancestor evidence."""
    run_root = tmp_path / "run"
    target = run_root / "bundle" / "pbip" / "Minimal"
    target.mkdir(parents=True)
    (run_root / "oracle").mkdir()

    assert bundle_corpus.is_package_target(target) is False
    assert bundle_corpus.evidence_dirs(target, ("oracle",)) == [run_root / "oracle"]
