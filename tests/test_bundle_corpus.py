"""Tests for shared shipping-artifact discovery used by the check gates."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bundle_corpus  # noqa: E402  # pylint: disable=wrong-import-position


# ---------------------------------------------------------------------------------------------
# Package-target classification (issue #562)
# ---------------------------------------------------------------------------------------------
#
# The invariant under test: the boundary question is answered from LEXICAL placement plus a
# no-follow `lstat` of the supplied root and its root marker entry - before any `resolve()`, any
# child traversal and any source/evidence discovery. Every control below is independent: each names
# one damaged/unsafe shape and asserts the specific classification it must produce.

#: Every following primitive the classifier is forbidden to use. Patched to explode, not to count,
#: so a violation is a hard failure at the exact call site rather than a soft assertion afterwards.
_FOLLOWING_PRIMITIVES = ("resolve", "is_file", "is_dir", "exists", "rglob", "stat", "open")


class _Followed(Exception):
    """Raised at the exact call site where classification dereferenced the supplied path.

    ⚠️ Deliberately NOT `AssertionError`, and deliberately caught inside the patched window (see
    :func:`_classify_without_following`). `Path.exists` is patched here, and **pytest itself calls it
    while formatting a failure traceback** - so letting the failure escape with the patch still
    installed turns a genuine kill into an `INTERNALERROR`, which reports as infrastructure breakage
    rather than as this test failing. Measured while mutation-testing this file's own controls.
    """


def _forbid_following(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every link-dereferencing `Path` primitive raise, plus `open`."""

    def boom(*_args: object, **_kwargs: object) -> object:
        raise _Followed("classification dereferenced the supplied path")

    for name in _FOLLOWING_PRIMITIVES:
        monkeypatch.setattr(Path, name, boom, raising=True)
    monkeypatch.setattr("builtins.open", boom, raising=True)


def _classify_without_following(
    target: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[bundle_corpus.TargetClassification | None, str]:
    """Classify with every follower armed, then disarm before any assertion can escape."""
    _forbid_following(monkeypatch)
    try:
        return bundle_corpus.classify_target(target), ""
    except _Followed as exc:
        return None, str(exc)
    finally:
        monkeypatch.undo()


def _link_directory(link: Path, target: Path) -> None:
    """A junction (Windows) or a directory symlink (POSIX) - a reparse point either way."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        completed = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if completed.returncode != 0:
            pytest.skip(f"could not create junction: {completed.stderr.decode(errors='replace').strip()}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege-dependent
        pytest.skip("this platform/account cannot create symlinks without elevation")


def _link_file(link: Path, target: Path) -> None:
    """A file symlink. Needs elevation on Windows, so it skips with the registered reason there."""
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform/account cannot create symlinks without elevation")


def _package(root: Path, *, marker: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fabric").mkdir(exist_ok=True)
    if marker:
        (root / bundle_corpus.PACKAGE_MARKER).write_text("{}\n", encoding="utf-8")
    return root


def test_classification_never_dereferences_the_supplied_path(tmp_path: Path, monkeypatch) -> None:
    """Kills: restoring `resolve()`/`is_file()` classification. Every follower raises here."""
    target = _package(tmp_path / "run" / "packages" / "Unit")

    result, followed = _classify_without_following(target, monkeypatch)

    assert followed == "", followed
    assert result is not None
    assert result.kind == bundle_corpus.TARGET_PACKAGE
    assert result.code == bundle_corpus.CODE_PACKAGE_BOUNDARY_OK


@pytest.mark.parametrize(
    ("spelling", "placement"),
    [
        (("run", "packages", "Unit"), bundle_corpus.PLACEMENT_FLAT),
        (("run", "packages", "batch1", "Unit"), bundle_corpus.PLACEMENT_NESTED),
        (("run", "bundle", "pbip", "Unit"), bundle_corpus.PLACEMENT_NONE),
        (("run", "packages"), bundle_corpus.PLACEMENT_NONE),
    ],
)
def test_placement_is_lexical_and_needs_no_filesystem(spelling: tuple[str, ...], placement: str) -> None:
    """Flat and nested are recognized from the normalized components alone, marker or no marker."""
    assert bundle_corpus.package_placement(Path("C:/root").joinpath(*spelling)) == placement


def test_a_clean_flat_and_nested_package_classify_as_intact_packages(tmp_path: Path) -> None:
    """Control: the ordinary good cases must stay clean, or the gate is unusable."""
    flat = _package(tmp_path / "run" / "packages" / "Flat")
    nested = _package(tmp_path / "run" / "packages" / "batch1" / "Nested")

    for target, placement in ((flat, bundle_corpus.PLACEMENT_FLAT), (nested, bundle_corpus.PLACEMENT_NESTED)):
        result = bundle_corpus.classify_target(target)
        assert (result.kind, result.placement) == (bundle_corpus.TARGET_PACKAGE, placement)
        assert result.is_safe and result.is_package and result.declares_self_contained
        assert not result.inherits_ancestor_evidence


def test_a_moved_package_with_a_regular_marker_outside_packages_is_still_recognized(tmp_path: Path) -> None:
    """A regular, non-reparse marker is an explicit declaration wherever the package was moved to."""
    moved = _package(tmp_path / "somewhere" / "else" / "Unit")

    result = bundle_corpus.classify_target(moved)

    assert result.kind == bundle_corpus.TARGET_PACKAGE
    assert result.placement == bundle_corpus.PLACEMENT_NONE
    assert bundle_corpus.is_package_target(moved) is True
    assert bundle_corpus.is_self_contained(moved) is True


def test_an_ordinary_unpackaged_unit_stays_non_package_and_keeps_ancestor_evidence(tmp_path: Path) -> None:
    """Compatibility control: the un-packaged shape must be untouched by the classifier."""
    run_root = tmp_path / "run"
    target = run_root / "bundle" / "pbip" / "Unit"
    target.mkdir(parents=True)
    (run_root / "oracle").mkdir()

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_ORDINARY
    assert result.is_safe and result.inherits_ancestor_evidence
    assert bundle_corpus.is_package_target(target) is False
    assert bundle_corpus.evidence_dirs(target, ("oracle",)) == [run_root / "oracle"]


@pytest.mark.parametrize("spelling", [("packages", "Unit"), ("packages", "batch1", "Unit")])
def test_a_package_shaped_target_with_no_marker_is_damaged_not_ordinary(
    tmp_path: Path, spelling: tuple[str, ...]
) -> None:
    """Kills: turning a damaged package back into a non-package that falls into bundle handling."""
    target = tmp_path.joinpath("run", *spelling)
    target.mkdir(parents=True)

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
    assert result.code == bundle_corpus.CODE_PACKAGE_MARKER_MISSING
    assert result.is_package and not result.is_safe
    assert not result.inherits_ancestor_evidence
    assert not result.declares_self_contained


def test_a_marker_that_is_a_directory_is_damaged_never_non_package(tmp_path: Path) -> None:
    """`is_file()` says False for a directory marker, which the old code read as 'not a package'."""
    target = tmp_path / "run" / "packages" / "Unit"
    (target / bundle_corpus.PACKAGE_MARKER).mkdir(parents=True)

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
    assert result.code == bundle_corpus.CODE_PACKAGE_MARKER_NOT_REGULAR
    assert bundle_corpus.is_package_target(target) is True
    assert bundle_corpus.is_self_contained(target) is False


def test_a_marker_directory_outside_packages_is_damaged_rather_than_ordinary(tmp_path: Path) -> None:
    """An ambiguous marker entry is refused wherever it sits, not only under `packages/`."""
    target = tmp_path / "moved" / "Unit"
    (target / bundle_corpus.PACKAGE_MARKER).mkdir(parents=True)

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
    assert result.placement == bundle_corpus.PLACEMENT_NONE


def test_a_symlinked_marker_is_damaged_and_the_boundary_is_never_declared_from_outside(tmp_path: Path) -> None:
    """Kills: reading the marker with `is_file()`, which follows the link to bytes outside."""
    outside = tmp_path / "outside" / "real-manifest.json"
    outside.parent.mkdir(parents=True)
    outside.write_text("{}\n", encoding="utf-8")
    target = tmp_path / "run" / "packages" / "Unit"
    target.mkdir(parents=True)
    _link_file(target / bundle_corpus.PACKAGE_MARKER, outside)

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
    assert result.code == bundle_corpus.CODE_PACKAGE_MARKER_REPARSE
    assert bundle_corpus.is_self_contained(target) is False


def test_a_broken_symlink_marker_is_damaged_rather_than_missing(tmp_path: Path) -> None:
    """A dangling marker link is still a reparse point: `lstat` sees the link, not the void."""
    target = tmp_path / "run" / "packages" / "Unit"
    target.mkdir(parents=True)
    _link_file(target / bundle_corpus.PACKAGE_MARKER, tmp_path / "never-existed.json")

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
    assert result.code == bundle_corpus.CODE_PACKAGE_MARKER_REPARSE


def test_a_junction_marker_is_damaged(tmp_path: Path) -> None:
    """The Windows half of the reparse class: a junction is not a symlink to `S_ISLNK` alone."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "run" / "packages" / "Unit"
    target.mkdir(parents=True)
    _link_directory(target / bundle_corpus.PACKAGE_MARKER, outside)

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
    assert result.code in {
        bundle_corpus.CODE_PACKAGE_MARKER_REPARSE,
        bundle_corpus.CODE_PACKAGE_MARKER_NOT_REGULAR,
    }


class _FakeStat:
    """A minimal `lstat` result. Both fields are exactly what :func:`is_reparse_entry` reads."""

    def __init__(self, st_mode: int, st_file_attributes: int = 0) -> None:
        self.st_mode = st_mode
        self.st_file_attributes = st_file_attributes


@pytest.mark.parametrize(
    ("label", "info"),
    [
        ("posix symlink", _FakeStat(stat.S_IFLNK | 0o777)),
        ("windows junction", _FakeStat(stat.S_IFREG | 0o666, bundle_corpus.FILE_ATTRIBUTE_REPARSE_POINT)),
    ],
)
def test_a_reparse_marker_that_READS_as_a_regular_file_is_damaged(
    tmp_path: Path, monkeypatch, label: str, info: _FakeStat
) -> None:
    """Kills: typing the marker with `is_file()`, which follows the link and says True.

    ⚠️ **This is the control the real-link fixtures cannot supply on Windows.** A file symlink needs
    elevation, and a junction can only point at a DIRECTORY - so on this host every creatable reparse
    marker also fails `is_file()`, and an `is_file()`-based classifier survives them all. Here the
    marker on disk IS a regular file and only the no-follow `lstat` reports the reparse, which is
    precisely the discriminating case.

    The junction row additionally kills an `S_ISLNK`-only predicate: its mode says regular file.
    """
    target = tmp_path / "run" / "packages" / "Unit"
    target.mkdir(parents=True)
    marker = target / bundle_corpus.PACKAGE_MARKER
    marker.write_text("{}\n", encoding="utf-8")
    assert marker.is_file(), "the fixture must be a following-visible regular file, or this is vacuous"
    real_lstat = os.lstat

    def fake(path, *args, **kwargs):
        if Path(path) == marker:
            return info
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(bundle_corpus.os, "lstat", fake)

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED, label
    assert result.code == bundle_corpus.CODE_PACKAGE_MARKER_REPARSE, label
    assert bundle_corpus.is_self_contained(target) is False, label


def test_the_reparse_predicate_covers_both_the_link_mode_and_the_windows_attribute() -> None:
    """Both halves are load-bearing; either alone waves through half the reparse class."""
    assert bundle_corpus.is_reparse_entry(_FakeStat(stat.S_IFLNK | 0o777)) is True
    assert (
        bundle_corpus.is_reparse_entry(_FakeStat(stat.S_IFREG | 0o666, bundle_corpus.FILE_ATTRIBUTE_REPARSE_POINT))
        is True
    )
    assert bundle_corpus.is_reparse_entry(_FakeStat(stat.S_IFREG | 0o666)) is False
    assert bundle_corpus.is_reparse_entry(_FakeStat(stat.S_IFDIR | 0o777)) is False
    assert bundle_corpus.FILE_ATTRIBUTE_REPARSE_POINT == 0x0400


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only")
def test_a_fifo_marker_is_damaged_and_is_never_opened(tmp_path: Path) -> None:
    """A FIFO marker would BLOCK a reader forever; `lstat` types it without opening it."""
    target = tmp_path / "run" / "packages" / "Unit"
    target.mkdir(parents=True)
    os.mkfifo(target / bundle_corpus.PACKAGE_MARKER)  # pylint: disable=no-member

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
    assert result.code == bundle_corpus.CODE_PACKAGE_MARKER_NOT_REGULAR


def test_a_linked_root_is_refused_before_the_marker_is_even_looked_for(tmp_path: Path) -> None:
    """Kills: checking the marker before the root.

    The link's destination holds a **valid** package, so a classifier that followed the alias would
    happily report an intact boundary for a directory the caller never named.
    """
    destination = _package(tmp_path / "real" / "packages" / "Unit")
    alias = tmp_path / "alias" / "packages" / "Unit"
    _link_directory(alias, destination)

    result = bundle_corpus.classify_target(alias)

    assert result.kind == bundle_corpus.TARGET_UNSAFE_ROOT
    assert result.code == bundle_corpus.CODE_TARGET_ROOT_REPARSE
    assert not result.is_safe
    assert not result.inherits_ancestor_evidence
    # ⚠️ Round-1 review of #590: the bool projection is CONSERVATIVE, not "honest". An unsafe root
    # is indeterminate, and `False` here is read as "ordinary, walk upward" - the fail-open answer.
    assert bundle_corpus.is_package_target(alias) is True


def test_an_ordinary_bundle_alias_fails_CLOSED_and_that_is_intentional(tmp_path: Path) -> None:
    """The documented compatibility cost: an aliased ORDINARY bundle is refused, not followed.

    Stated rather than hidden. Following it is the defect (the boundary would be decided about a
    directory the caller did not name); refusing it is loud, attributable and recoverable.
    """
    run_root = tmp_path / "run"
    real = run_root / "bundle" / "pbip" / "Unit"
    real.mkdir(parents=True)
    (run_root / "oracle").mkdir()
    alias = tmp_path / "alias-unit"
    _link_directory(alias, real)

    result = bundle_corpus.classify_target(alias)

    assert result.kind == bundle_corpus.TARGET_UNSAFE_ROOT
    assert not result.is_safe
    assert bundle_corpus.evidence_dirs(alias, ("oracle",)) == [], "an unsafe root inherits nothing"
    # The unaliased spelling keeps working, which is the recovery.
    assert bundle_corpus.evidence_dirs(real, ("oracle",)) == [run_root / "oracle"]


def test_an_unassessable_root_is_non_clean_and_never_exception_shaped_success(tmp_path: Path, monkeypatch) -> None:
    """A permission/OS error on the root `lstat` may not be swallowed into 'ordinary'."""
    target = tmp_path / "run" / "packages" / "Unit"
    target.mkdir(parents=True)
    real_lstat = os.lstat

    def deny(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(bundle_corpus.os, "lstat", deny)

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_UNSAFE_ROOT
    assert result.code == bundle_corpus.CODE_TARGET_ROOT_UNASSESSABLE
    assert not result.is_safe


def test_an_unassessable_marker_under_package_placement_is_damaged(tmp_path: Path, monkeypatch) -> None:
    """'I could not tell whether a marker is there' is grouped with non-clean, never with clean."""
    target = tmp_path / "run" / "packages" / "Unit"
    target.mkdir(parents=True)
    marker = target / bundle_corpus.PACKAGE_MARKER
    real_lstat = os.lstat

    def deny(path, *args, **kwargs):
        if Path(path) == marker:
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(bundle_corpus.os, "lstat", deny)

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
    assert result.code == bundle_corpus.CODE_PACKAGE_MARKER_UNASSESSABLE
    assert not result.is_safe


def test_a_missing_ordinary_root_stays_ordinary_but_a_missing_package_root_is_damaged(tmp_path: Path) -> None:
    """Absent is not ambiguous - but an absent package BOUNDARY still is not a bundle."""
    ordinary = bundle_corpus.classify_target(tmp_path / "bundle" / "pbip" / "NeverBuilt")
    packaged = bundle_corpus.classify_target(tmp_path / "run" / "packages" / "NeverBuilt")

    assert ordinary.kind == bundle_corpus.TARGET_ORDINARY
    assert packaged.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
    assert packaged.code == bundle_corpus.CODE_PACKAGE_ROOT_MISSING


def test_a_package_shaped_root_that_is_a_regular_file_is_damaged(tmp_path: Path) -> None:
    """A package boundary has to be a directory; a file wearing the name is not one."""
    target = tmp_path / "run" / "packages" / "Unit"
    target.parent.mkdir(parents=True)
    target.write_text("not a package\n", encoding="utf-8")

    result = bundle_corpus.classify_target(target)

    assert result.kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
    assert result.code == bundle_corpus.CODE_PACKAGE_ROOT_NOT_DIRECTORY


def test_a_deceptive_dot_dot_spelling_cannot_become_a_package_and_normalizes_lexically() -> None:
    """`packages/../Unit` is `Unit`. Defined lexically, so no dereference can change the answer.

    ⚠️ This is a deliberate behaviour change: the previous `parent.parent.name` check read the
    literal `..` component and called this a NESTED package.
    """
    deceptive = Path("C:/run/packages/../Unit")
    assert bundle_corpus.normalized_parts(deceptive)[-2:] == ("run", "Unit")
    assert "packages" not in bundle_corpus.normalized_parts(deceptive)
    assert bundle_corpus.package_placement(deceptive) == bundle_corpus.PLACEMENT_NONE

    # ...while a `..` INSIDE the package shape collapses to the flat shape it really is.
    assert bundle_corpus.package_placement(Path("C:/run/packages/batch1/../Unit")) == bundle_corpus.PLACEMENT_FLAT
    # A relative path may not eat its own leading `..`.
    assert bundle_corpus.normalized_parts(Path("../packages/Unit")) == ("..", "packages", "Unit")
    assert bundle_corpus.package_placement(Path("../packages/Unit")) == bundle_corpus.PLACEMENT_FLAT


def test_diagnostics_never_echo_the_supplied_path_or_marker_bytes(tmp_path: Path) -> None:
    """Codes and wording are stable and generic: these strings get pasted into shared verdicts."""
    secret = tmp_path / "customer-secret-server" / "packages" / "Unit"
    marker = secret / bundle_corpus.PACKAGE_MARKER
    marker.mkdir(parents=True)
    (marker / "SECRET-TOKEN.txt").write_text("SECRET-TOKEN\n", encoding="utf-8")

    result = bundle_corpus.classify_target(secret)
    missing = bundle_corpus.classify_target(tmp_path / "customer-secret-server" / "packages" / "Other")

    assert result.code == bundle_corpus.CODE_PACKAGE_MARKER_NOT_REGULAR
    for classification in (result, missing):
        assert "customer-secret-server" not in classification.detail
        assert str(tmp_path) not in classification.detail
        assert "SECRET-TOKEN" not in classification.detail


def test_evidence_dirs_skips_ancestors_for_every_damaged_package_shape(tmp_path: Path) -> None:
    """The shared walk: a damaged boundary is not a licence to walk up.

    ⚠️ This exercises `evidence_dirs` directly. It is NOT a claim that `check_unit.py` is protected -
    that gate reaches this walk through `_unit_dir()`, which resolves first (see the residual in
    `docs/migration-phases.md`).
    """
    run_root = tmp_path / "run"
    (run_root / "oracle").mkdir(parents=True)
    missing = run_root / "packages" / "NoMarker"
    missing.mkdir(parents=True)
    dir_marker = run_root / "packages" / "DirMarker"
    (dir_marker / bundle_corpus.PACKAGE_MARKER).mkdir(parents=True)

    for target in (missing, dir_marker):
        assert bundle_corpus.classify_target(target).kind == bundle_corpus.TARGET_PACKAGE_DAMAGED
        assert bundle_corpus.evidence_dirs(target, ("oracle",)) == []


@pytest.mark.parametrize("shape", [("packages", "Unit"), ("packages", "batch1", "Unit")])
def test_the_bool_projection_never_calls_an_indeterminate_boundary_ordinary(
    tmp_path: Path, monkeypatch, shape: tuple[str, ...]
) -> None:
    """Kills: projecting damaged/unsafe/unassessable boundaries as an ordinary ``False``.

    Round-1 review of #590. ``False`` is read by a caller as "ordinary, walk upward", so it is the
    fail-OPEN answer and must be reserved for a target *proven* ordinary. Flat and nested, missing /
    permission-denied / reparse, root side and marker side.
    """
    base = tmp_path.joinpath("run", *shape)

    missing_marker = base.with_name(base.name + "-missing")
    missing_marker.mkdir(parents=True)

    reparse_marker = base.with_name(base.name + "-reparse")
    reparse_marker.mkdir(parents=True)
    marker = reparse_marker / bundle_corpus.PACKAGE_MARKER
    marker.write_text("{}\n", encoding="utf-8")

    denied_root = base.with_name(base.name + "-denied-root")
    denied_root.mkdir(parents=True)
    denied_marker = base.with_name(base.name + "-denied-marker")
    denied_marker.mkdir(parents=True)

    real_lstat = os.lstat

    def fake(path, *args, **kwargs):
        if Path(path) == marker:
            return _FakeStat(stat.S_IFREG | 0o666, bundle_corpus.FILE_ATTRIBUTE_REPARSE_POINT)
        if Path(path) in (denied_root, denied_marker / bundle_corpus.PACKAGE_MARKER):
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(bundle_corpus.os, "lstat", fake)

    for target in (missing_marker, reparse_marker, denied_root, denied_marker):
        classification = bundle_corpus.classify_target(target)
        assert classification.kind != bundle_corpus.TARGET_ORDINARY
        assert bundle_corpus.is_package_target(target) is True, classification.code
        # The bool is the strict complement of the property the walk actually keys on, so a caller
        # reading either one cannot be told two different things.
        assert bundle_corpus.is_package_target(target) is not classification.inherits_ancestor_evidence


def test_the_bool_projection_stays_false_for_a_target_proven_ordinary(tmp_path: Path) -> None:
    """The vacuity control: a conservative projection that said True for everything is useless."""
    unpackaged = tmp_path / "run" / "bundle" / "pbip" / "Unit"
    unpackaged.mkdir(parents=True)
    never_built = tmp_path / "run" / "bundle" / "pbip" / "NeverBuilt"

    for target in (unpackaged, never_built):
        assert bundle_corpus.classify_target(target).kind == bundle_corpus.TARGET_ORDINARY
        assert bundle_corpus.is_package_target(target) is False
        assert bundle_corpus.classify_target(target).inherits_ancestor_evidence is True


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
