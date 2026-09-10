"""
purpose: shared discovery helpers for shipping Power BI artifacts in migration bundles.
usage:   import bundle_corpus; bundle_corpus.shipping_reports(Path("bundle"))

The check_* gates deliberately keep separate verdicts and exit codes, but they should not keep
separate copies of the same filesystem-discovery rules. This module is the single place for the
`pbip/`-first shipping-artifact convention.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath

#: A self-contained handover package writes this beside the unit (`scripts/package_unit.py`, #446).
PACKAGE_MARKER = "package-manifest.json"

#: The directory name that makes a target *lexically* package-shaped.
PACKAGES_DIR = "packages"

#: `FILE_ATTRIBUTE_REPARSE_POINT`. Named here because `stat` only exposes it on Windows builds, and
#: this predicate has to give the same answer for a junction on either host.
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)

# ---------------------------------------------------------------------------------------------
# Package-target classification (issue #562, split prerequisite)
# ---------------------------------------------------------------------------------------------
#
# ⚠️ **This runs BEFORE anything touches the filesystem in a following way.** The previous
# classification asked `(target / PACKAGE_MARKER).is_file()` and `target.resolve()`, and both
# DEREFERENCE: `is_file()` follows a marker symlink, so a package boundary could be declared by a
# file living anywhere on the host, and `resolve()` follows a junction, so the *placement* question
# ("am I under `packages/`?") was answered about the link's destination rather than about the path
# the caller actually handed us. A boundary check that follows links is not a boundary check.
#
# So classification uses exactly two primitives - **no-follow `os.lstat`** of the supplied root and
# of its root marker entry - plus **pure lexical** normalization of the supplied path. It never
# calls `resolve`, `is_file`, `is_dir`, `exists` or `rglob`, never opens or reads a byte, and never
# looks at a child other than the root marker entry.
#
# ⚠️ It is a **boundary classifier, not a manifest integrity verifier.** It answers "is this a
# package boundary, and is that boundary intact enough to reason about?". It deliberately does NOT
# parse the manifest, hash anything, or check declared contents - that is the next slice of #562.

#: An ordinary, non-package target: a bundle root or an un-packaged unit. Legacy behaviour.
TARGET_ORDINARY = "ordinary"
#: A package boundary in good order: a regular, non-reparse `package-manifest.json` at the root.
TARGET_PACKAGE = "package"
#: Package-shaped or explicitly package, with a boundary that is missing, reparse, non-regular or
#: unassessable. **Never non-package**, and never a reason to walk upward for evidence.
TARGET_PACKAGE_DAMAGED = "damaged_package"
#: The supplied root itself is a link/junction/reparse point, or could not be `lstat`-assessed.
TARGET_UNSAFE_ROOT = "unsafe_root"

#: Lexical placement of the supplied path.
PLACEMENT_NONE = "none"
PLACEMENT_FLAT = "flat"
PLACEMENT_NESTED = "nested"

#: Stable diagnostic codes. Consumers print these; they carry no host path and no file content.
CODE_ORDINARY_TARGET = "ordinary_target"
CODE_PACKAGE_BOUNDARY_OK = "package_boundary_ok"
CODE_PACKAGE_MARKER_MISSING = "package_marker_missing"
CODE_PACKAGE_MARKER_REPARSE = "package_marker_reparse"
CODE_PACKAGE_MARKER_NOT_REGULAR = "package_marker_not_regular_file"
CODE_PACKAGE_MARKER_UNASSESSABLE = "package_marker_unassessable"
CODE_PACKAGE_ROOT_MISSING = "package_root_missing"
CODE_PACKAGE_ROOT_NOT_DIRECTORY = "package_root_not_directory"
CODE_TARGET_ROOT_REPARSE = "target_root_reparse"
CODE_TARGET_ROOT_UNASSESSABLE = "target_root_unassessable"

#: Generic, package-relative wording per code. **No supplied path, no marker bytes, ever**: these
#: strings are printed into verdicts that get pasted into issues and shared with customers, and the
#: supplied target can itself be a secret-bearing absolute path.
_DETAILS = {
    CODE_ORDINARY_TARGET: "not a package boundary",
    CODE_PACKAGE_BOUNDARY_OK: f"package boundary declared by a regular {PACKAGE_MARKER}",
    CODE_PACKAGE_MARKER_MISSING: (
        f"the target is package-shaped but carries no {PACKAGE_MARKER}, so its boundary is "
        "unproven - it is NOT treated as an ordinary bundle"
    ),
    CODE_PACKAGE_MARKER_REPARSE: (
        f"{PACKAGE_MARKER} is a link/junction/reparse point, so the boundary would be declared by "
        "bytes outside the package"
    ),
    CODE_PACKAGE_MARKER_NOT_REGULAR: (
        f"{PACKAGE_MARKER} exists but is not a regular file (directory, FIFO, socket or device)"
    ),
    CODE_PACKAGE_MARKER_UNASSESSABLE: (
        f"{PACKAGE_MARKER} could not be assessed without following it, so the boundary is unknown"
    ),
    CODE_PACKAGE_ROOT_MISSING: "the package-shaped target does not exist",
    CODE_PACKAGE_ROOT_NOT_DIRECTORY: "the package-shaped target is not a directory",
    CODE_TARGET_ROOT_REPARSE: (
        "the supplied target is itself a link/junction/reparse point; this gate refuses to follow "
        "a caller-supplied alias rather than classify the wrong directory"
    ),
    CODE_TARGET_ROOT_UNASSESSABLE: "the supplied target could not be assessed without following it",
}


@dataclass(frozen=True)
class TargetClassification:
    """What kind of boundary a caller-supplied target is, decided without dereferencing anything.

    ``code`` and ``detail`` are the only strings a consumer may print: they are stable and generic.
    ``unit_name`` is the normalized final path component - the same value a report already prints as
    its unit - and never an absolute path.
    """

    kind: str
    code: str
    detail: str
    placement: str
    unit_name: str

    @property
    def is_package(self) -> bool:
        """The bool :func:`is_package_target` projects, defined **conservatively**.

        ⚠️ It is ``True`` for everything that is not an ORDINARY target - intact package, damaged
        package **and unsafe root alike** - because the only thing a caller can safely do with this
        bool is decide whether to walk upward for ancestor evidence, and ``False`` is the fail-OPEN
        answer. Round-1 review of PR #590: projecting an unsafe root as ``False`` made an
        indeterminate boundary indistinguishable from a proven ordinary bundle, which is exactly the
        confusion the classifier exists to remove. It is the strict complement of
        :attr:`inherits_ancestor_evidence`, so a caller reading either one gets the same answer.
        """
        return self.kind != TARGET_ORDINARY

    @property
    def declares_self_contained(self) -> bool:
        """A regular, non-reparse root marker is present. A followed link is NOT a declaration."""
        return self.kind == TARGET_PACKAGE

    @property
    def is_safe(self) -> bool:
        """Whether a consumer may continue into resolve/discovery. ``unassessable`` is never safe."""
        return self.kind in (TARGET_ORDINARY, TARGET_PACKAGE)

    @property
    def inherits_ancestor_evidence(self) -> bool:
        """Only an ordinary target walks upward.

        ⚠️ Damaged packages AND unsafe roots both stop the walk, for opposite-looking reasons that
        are the same reason: neither has established where its boundary is, and inheriting evidence
        is the fail-OPEN direction (it hands an agent renders nobody attributed to this unit).
        """
        return self.kind == TARGET_ORDINARY


def _classification(kind: str, code: str, placement: str, unit_name: str) -> TargetClassification:
    return TargetClassification(kind=kind, code=code, detail=_DETAILS[code], placement=placement, unit_name=unit_name)


def normalized_parts(target: PurePath) -> tuple[str, ...]:
    """The supplied path's components with ``.`` dropped and ``..`` collapsed **lexically**.

    ⚠️ **Lexical, deliberately, and not equivalent to `resolve()`.** `resolve()` would answer the
    placement question about a link's destination; the question is about the path the caller typed.
    Two consequences, both tested:

    * ``<...>/packages/../Unit`` normalizes to ``<...>/Unit`` and is therefore **NOT** package-shaped
      - it cannot become a package by being spelled deceptively. (The previous `parent.parent.name`
      check called it a *nested* package, because the literal parent component is ``..``.)
    * ``<...>/packages/batch/../Unit`` normalizes to ``<...>/packages/Unit`` and IS flat-shaped.

    ``..`` is never popped past an anchor, and a leading ``..`` on a relative path is preserved
    rather than silently eaten.
    """
    parts = list(target.parts)
    anchor_len = 1 if target.anchor and parts else 0
    out = parts[:anchor_len]
    for part in parts[anchor_len:]:
        if part == ".":
            continue
        if part == "..":
            if len(out) > anchor_len and out[-1] != "..":
                out.pop()
                continue
            if anchor_len:  # `C:\..` is `C:\`: an absolute path cannot climb above its anchor
                continue
        out.append(part)
    return tuple(out)


def package_placement(target: PurePath) -> str:
    """``flat`` for ``<...>/packages/<Unit>``, ``nested`` for ``<...>/packages/<batch>/<Unit>``.

    Judged on :func:`normalized_parts` only - no filesystem access, and no resolved-parent
    heuristic, so a target is package-shaped even when its marker is missing (which is exactly the
    case that must not fall back to legacy bundle handling).
    """
    parts = normalized_parts(target)
    if len(parts) >= 2 and parts[-2] == PACKAGES_DIR:
        return PLACEMENT_FLAT
    if len(parts) >= 3 and parts[-3] == PACKAGES_DIR:
        return PLACEMENT_NESTED
    return PLACEMENT_NONE


def is_reparse_entry(info: os.stat_result) -> bool:
    """Whether a no-follow ``lstat`` result describes a link, junction or other reparse point.

    Both halves are load-bearing. ``S_ISLNK`` covers POSIX symlinks and the name-surrogate reparse
    points Python reports as links; the Windows ``FILE_ATTRIBUTE_REPARSE_POINT`` bit covers the rest
    - a junction, a mount point, or an app-execution alias - which are NOT symlinks and which
    ``S_ISLNK`` alone would wave through.
    """
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def classify_target(target: Path) -> TargetClassification:
    """Classify a caller-supplied target **before** any resolve, traversal or discovery.

    Order is the invariant, not an implementation detail:

    1. lexical placement (no syscall at all);
    2. no-follow ``lstat`` of the **supplied root** - a link/junction/unassessable root is refused
       here, before the marker is even looked for, so a root alias can never smuggle in a valid
       package that lives somewhere else;
    3. no-follow ``lstat`` of the **root marker entry** only.

    ⚠️ **Fail-closed compatibility consequence, stated rather than hidden:** an *ordinary* bundle
    reached through a directory symlink or junction alias now classifies ``unsafe_root`` and is
    refused. That is intentional. The alternative is to follow it, which is the whole defect: a
    boundary decided about a directory the caller did not name. Operators who alias a bundle path
    should pass the real path; the refusal is loud, attributable and recoverable, where the
    fail-open direction is silent.
    """
    placement = package_placement(target)
    parts = normalized_parts(target)
    unit_name = parts[-1] if parts else ""

    try:
        root_info = os.lstat(target)
    except FileNotFoundError:
        # Definitively absent, not ambiguous. Package-shaped means the boundary is gone; an ordinary
        # missing path keeps legacy behaviour (a caller may be probing a path it has not built yet).
        if placement != PLACEMENT_NONE:
            return _classification(TARGET_PACKAGE_DAMAGED, CODE_PACKAGE_ROOT_MISSING, placement, unit_name)
        return _classification(TARGET_ORDINARY, CODE_ORDINARY_TARGET, placement, unit_name)
    except (OSError, ValueError):
        # ⚠️ No exception-shaped success. "I could not tell whether this is a reparse point" is
        # grouped with non-clean, never with clean, for EVERY placement - an unassessable ordinary
        # root is exactly as unknown as an unassessable package one.
        return _classification(TARGET_UNSAFE_ROOT, CODE_TARGET_ROOT_UNASSESSABLE, placement, unit_name)

    if is_reparse_entry(root_info):
        return _classification(TARGET_UNSAFE_ROOT, CODE_TARGET_ROOT_REPARSE, placement, unit_name)
    if placement != PLACEMENT_NONE and not stat.S_ISDIR(root_info.st_mode):
        return _classification(TARGET_PACKAGE_DAMAGED, CODE_PACKAGE_ROOT_NOT_DIRECTORY, placement, unit_name)

    return _classify_marker(target, placement, unit_name)


def _classify_marker(target: Path, placement: str, unit_name: str) -> TargetClassification:
    """Classify the root marker entry of an already-cleared root, with a single no-follow ``lstat``."""
    try:
        marker_info = os.lstat(target / PACKAGE_MARKER)
    except FileNotFoundError:
        if placement != PLACEMENT_NONE:
            return _classification(TARGET_PACKAGE_DAMAGED, CODE_PACKAGE_MARKER_MISSING, placement, unit_name)
        return _classification(TARGET_ORDINARY, CODE_ORDINARY_TARGET, placement, unit_name)
    except (OSError, ValueError):
        # Ambiguous rather than absent: a marker may be sitting there declaring a package boundary
        # we cannot read. Refusing is the fail-closed direction even outside `packages/`.
        return _classification(TARGET_PACKAGE_DAMAGED, CODE_PACKAGE_MARKER_UNASSESSABLE, placement, unit_name)

    if is_reparse_entry(marker_info):
        return _classification(TARGET_PACKAGE_DAMAGED, CODE_PACKAGE_MARKER_REPARSE, placement, unit_name)
    if not stat.S_ISREG(marker_info.st_mode):
        return _classification(TARGET_PACKAGE_DAMAGED, CODE_PACKAGE_MARKER_NOT_REGULAR, placement, unit_name)
    # A regular, non-reparse marker is an EXPLICIT package declaration wherever it sits, so a moved
    # package outside `packages/` is still recognized.
    return _classification(TARGET_PACKAGE, CODE_PACKAGE_BOUNDARY_OK, placement, unit_name)


def is_self_contained(target: Path) -> bool:
    """Whether ``target`` declares that it carries its own evidence and must inherit none.

    ⚠️ **This is what stops an evidence walk-up, in BOTH gates, and it has to be one rule.** Every
    gate here looks for `reference/`/`oracle/` beside the target AND beside its ancestors, and
    **unions** the hits - which is right for an un-packaged unit under `<bundle>/pbip/<Unit>/`, whose
    capture lives further up. `package_unit.py` writes a unit-scoped `oracle/oracle-manifest.json`
    holding THIS unit's views with rewritten paths, so a package assembled INSIDE a run directory
    matches every view twice and both gates then refuse the pair as an ambiguity:

    * `check_reference_readiness` reports *"2 records share this name once normalized"* and takes
      every page from ready to **unverifiable** (issue #451);
    * `check_unit` reports *"2 producer records are named X"* and reports **0 visual coverage** -
      measured on a synthetic package, the same defect one gate along.

    Both are silent, and both make packaging strictly WORSE than not packaging. The marker is the
    package's own declaration that it is complete, so it is taken at its word.

    ⚠️ Deliberately NOT "stop when the target has its own copy of this directory": a package that
    OMITTED a render because it could not attribute it would then pick that render up from the
    ancestor, undoing a fail-closed packaging decision at the consumer.

    ⚠️ **A followed link is not a declaration.** This used to be ``(target / PACKAGE_MARKER).is_file()``,
    which returns True for a marker symlink pointing anywhere on the host - so the package's boundary
    could be declared by bytes outside the package. It is now the ``package`` projection of
    :func:`classify_target`: a **regular, non-reparse** marker entry, judged by ``lstat``.
    """
    return classify_target(target).declares_self_contained


def is_package_target(target: Path) -> bool:
    """Whether ``target`` must be treated as a package boundary (flat, nested, or explicitly marked).

    The bool compatibility projection of :func:`classify_target`. **No production code reads it** -
    :func:`evidence_dirs` keys the walk on
    :attr:`TargetClassification.inherits_ancestor_evidence` - so it survives as a documented API for
    callers that only ever needed the one bit, and it never resolves.

    A package-shaped target (flat ``.../packages/<Unit>`` or nested
    ``.../packages/<batch>/<Unit>``) must evaluate only its own local evidence and must not inherit
    ancestor evidence even if incomplete (missing ``package-manifest.json`` or local evidence).
    ``fabric/`` alone is not a package signal: ordinary migration units and bundle roots carry it
    too and still need to discover run-level evidence.

    ⚠️ **Conservative by construction:** damaged, unsafe and otherwise indeterminate boundaries all
    project ``True``, because ``False`` is read as "ordinary, walk upward" and that is the fail-open
    direction. ``False`` is reserved for a target *proven* ordinary. A caller that needs to tell an
    intact package from a damaged one, or to refuse an unsafe root, calls :func:`classify_target`.
    """
    return classify_target(target).is_package


def shipping_reports(root: Path) -> list[Path]:
    """Return `.Report` folders that ship under ``root``.

    Engine bundles carry the editable/shipping copy under ``pbip/`` and the pristine engine baseline
    under ``reports/``. When ``pbip/`` exists, scan only it. Passing a `.Report` folder directly is an
    explicit override for targeted checks.
    """
    root = root.resolve()
    if root.name.endswith(".Report"):
        return [root]
    base = root / "pbip" if (root / "pbip").is_dir() else root
    return sorted({path.resolve() for path in base.rglob("*.Report") if path.is_dir()}, key=str)


def shipping_models(root: Path, *, include_standalone: bool = False) -> list[Path]:
    """Return `.SemanticModel` folders that ship under ``root``.

    Most artifact gates scan ``pbip/`` only when it exists, because ``semantic_models/`` is then the
    engine baseline. ``check_empty_model`` passes ``include_standalone=True`` because datasource-only
    migrations can legitimately ship a standalone model there.
    """
    root = root.resolve()
    if root.name.endswith(".SemanticModel"):
        return [root]
    pbip = root / "pbip"
    if not pbip.is_dir():
        return sorted({path.resolve() for path in root.rglob("*.SemanticModel") if path.is_dir()}, key=str)
    models = {path.resolve() for path in pbip.rglob("*.SemanticModel") if path.is_dir()}
    if include_standalone:
        standalone = root / "semantic_models"
        if standalone.is_dir():
            models.update(path.resolve() for path in standalone.rglob("*.SemanticModel") if path.is_dir())
    return sorted(models, key=str)


#: How far above a target an evidence directory may live.
#:
#: THREE, from the canonical layout rather than from taste: a capture is written to
#: ``_runs/<NNN>-<slug>/oracle/`` while an un-packaged unit sits at
#: ``_runs/<NNN>-<slug>/bundle/pbip/<Unit>/`` - exactly three ancestors below it. Round-1 review of
#: PR #454 measured the exit gate stopping at ONE, so that unit could not see the run's capture at
#: all; stopping at two still misses it for the ordinary engine-bundle shape.
#:
#: Widening discovery is safe in the direction that matters: every record found this way still has to
#: pass the identity join, so a foreign capture pulled in from a shared ancestor is refused and
#: counted, and two indistinguishable records make the page UNVERIFIABLE. Discovery adds candidates;
#: it never admits one.
ANCESTOR_LEVELS = 3


def evidence_dirs(target: Path, names: Sequence[str], *, also: Sequence[Path] = ()) -> list[Path]:
    """Existing evidence directories for ``target``: beside it, then up to three ancestors (``ANCESTOR_LEVELS``).

    WARNING: **The whole walk lives here, not just where it stops.** Round-1 review of PR #454:
    centralising only the *stop* condition left the two gates disagreeing about the *search* -
    `check_reference_readiness` looked two levels up while `check_unit` looked one, so a non-packaged
    unit at `<bundle>/pbip/<Unit>/` could not inherit the run's flat capture at all in the exit gate.
    A shared rule that covers half the behaviour is two rules wearing one name.

    The ancestor portion is skipped for anything that is not an ordinary target
    (:attr:`TargetClassification.inherits_ancestor_evidence`); the target itself, and any ``also``
    root a caller adds, are always searched. Results keep discovery order, are de-duplicated by
    resolved path, and are returned unresolved so a caller still sees the spelling it passed in.

    ⚠️ **A DAMAGED package does not become a reason to walk upward.** Missing, reparse or
    unassessable ``package-manifest.json`` under package placement stops the walk exactly as an
    intact package does - the fail-open direction here hands an agent ancestor renders that nobody
    attributed to this unit. An unsafe (link/junction/unassessable) root stops it too, for the same
    reason: its boundary was never established.
    """
    classification = classify_target(target)
    roots = [target, *also]
    if classification.inherits_ancestor_evidence:
        ancestor = target
        for _ in range(ANCESTOR_LEVELS):
            ancestor = ancestor.parent
            roots.append(ancestor)
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for name in names:
            candidate = root / name
            if not candidate.is_dir():
                continue
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(candidate)
    return found
