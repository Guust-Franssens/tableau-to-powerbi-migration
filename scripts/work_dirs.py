"""
purpose: single source of truth for the canonical PRE-BUNDLE work layout - resolve, allocate and
         expose per-unit, per-run scratch/output paths so scripts stop inventing their own
         (`_assessment`, `_sweep`, `_oracle`, an ad hoc `_work/<name>`, or a stray `fabric/` from a
         CWD-relative path). See issue #291 (gaps 1 and 3) and issue #234, whose corrected design
         this module implements: `_runs/<NNN>-<slug>/{assessment,assets,bundle,oracle,packages,
         deliverables,scratch}/`. `deliverables/` is created lazily, on first access, not up
         front (issue #481) - see `LAZY_SUBDIRS`.
usage:   python scripts/work_dirs.py <unit-name> [--repo-root PATH] [--json]
         python scripts/work_dirs.py --verify [--repo-root PATH] [--json]
         from work_dirs import allocate_run, sanitize_unit_key, runs_root, list_runs
         from work_dirs import check_run_location

Scope of THIS module - deliberately narrow (see the PR that landed it, Refs #291)
----------------------------------------------------------------------------------
This lands the CONVENTION and the path-resolution primitive only. It does NOT:
  * migrate `assess_estate.py`, `harvest_estate_assets.py`, `capture_tableau_oracle.py` or
    `run_estate.py` onto it - their `_assessment*/` / `_sweep*/` / `_oracle*/` / `_bundle*/`
    defaults keep working exactly as documented in `AGENTS.md` and `docs/operator-runbook.md`
    today. Migrating them is real, separate follow-up work (tracked against issue #234's
    acceptance criteria) that would collide with in-flight work on those exact files.
  * generate `_runs/INDEX.md` (#234's agent-facing flat index) or a legacy-migration helper
    (#234 acceptance criterion 9) - both are follow-up.
  * reproduce `harvest_estate_assets.py`'s fail-closed `git check-ignore` output guard
    (#234 acceptance criterion 4) - that guard stays where it is today; sharing it is follow-up
    so this change does not touch a file it does not own.

Why the repo root is resolved from `__file__`, never from `Path.cwd()`
------------------------------------------------------------------------
A stray, empty `fabric/` was once written at the repo root because a script resolved a relative
output path against whatever the CWD happened to be when an agent invoked it, not against the
repo. Anchoring on this module's own location (`scripts/work_dirs.py` -> its parent) makes the
layout reachable from code regardless of CWD - a convention only documentation enforces gets
reinvented; one a script can import does not.

Why `_runs/`, not `_work/`
--------------------------
`.gitignore` already uses `**/_work/` for a DIFFERENT convention with the OPPOSITE retention
meaning: the re-runnable `*.py` transform scripts inside an existing `_work/` tree are deliberately
TRACKED. Per-run scratch is the reverse (nothing under it is tracked), so it lives under `_runs/`
instead - a root-anchored `/_*` rule already covers it (verified: `git check-ignore -v -- _runs`
reports `.gitignore:127:/_*`), same as every other top-level scratch root in this repo.

WARNING - the run NUMBER is identity: never renamed, never renumbered, never reused
------------------------------------------------------------------------------------
Renaming or renumbering an ALREADY-ALLOCATED run directory is destructive, not tidying. Generated
bundle output under `<run>/bundle/` embeds ABSOLUTE self-paths, so moving a run after the fact
breaks every bundle beneath it - PBIP refresh, the report's `byPath` model binding, and `_build/`
replay all resolve against a path that no longer exists. A reorg that renumbers 14 run directories
therefore breaks 14 bundles. That is not hypothetical: it happened on a real customer engagement
whose agent read THIS FILE, pattern-matched the naming rules below, and never opened the two
documents that carried the prohibition (issue #470).

**Granularity: one run per PIPELINE RUN, not one per workbook.** A 48-workbook estate sweep is ONE
run; its per-workbook units live inside that run's `bundle/pbip/`. `unit_key` names what the run is
*about* (a site, a project, or a single workbook when that is genuinely the whole job) - it is not a
promise that every workbook gets a run of its own.

Prevention is only advice, so this module also makes a rename DETECTABLE afterwards: `allocate_run`
records the directory name it allocated under the `allocated_dir_name` key and the absolute path
under `allocated_abs_path`, and `check_run_location` (surfaced by `list_runs` and by
`python scripts/work_dirs.py --verify`) reports `intact`, `moved` or `unverifiable`. `unverifiable`
is NOT a soft `intact` - see `check_run_location`.

`--verify` is a GATE, not a status query: exit 0 all intact / 1 at least one moved / 2 duplicate
number / 3 at least one unverifiable. It rests on ONE invariant, implemented in one discovery function and one
classification function: every child directory of an existing `_runs/` root is a candidate run, any
candidate whose identity cannot be POSITIVELY established from its own evidence is `unverifiable`,
and `intact` is only ever returned on positive proof. So a run directory it cannot read at all -
`run.json` missing, empty, truncated, malformed, locked, holding valid JSON that is not an object,
carrying a run number that contradicts its own directory, or recording a path that is not absolute
- is `unverifiable` and exits 3. It used to be skipped silently and counted in no bucket at all,
which reported a half-written run as a clean tree. `list_runs` still skips those, because it is
inspection rather than a gate.

Map: `docs/INDEX.md`. The same rule is stated in `docs/migration-phases.md` (Phase 1) and
`docs/operator-runbook.md` (section 6.4, Scratch).
"""

# ⚠️ This module docstring is argparse's `description`, and Windows defaults stdout to cp1252, so it
# must stay ASCII-ONLY or `--help` dies before printing anything - measured: adding one "warning
# sign + variation-selector-16" glyph to it turned
# `tests/test_scripts_help.py::test_help_exits_zero_on_a_cp1252_stdout[work_dirs.py]` red (F003 in
# `docs/dry-run-findings-2026-08-11.md`). Glyphs are fine HERE, in a comment, and in every other
# docstring in this file - only the module docstring is printed by `--help`.

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# The canonical pre-bundle subdirectories: issue #234's corrected design plus the `deliverables/`
# addition from issue #322 (operator-facing outputs - e.g. a connections.json/md naming real
# customer infrastructure - that must never land at an unignored repo-root path such as the
# `ses-prep/` near-miss). Order here is also display order everywhere these are printed/listed.
#   assessment/   assess_estate.py-shaped output (report.md, assessment.json, estate.db)
#   assets/       harvest_estate_assets.py-shaped downloads (.twbx / .tdsx)
#   bundle/       run_estate.py-shaped conversion output (report.json, pbip/, handover/)
#   oracle/       capture_tableau_oracle.py-shaped visual + numeric reference capture
#   packages/     package_unit.py-shaped per-unit, agent-facing handover packages (issue #446).
#                 ⚠️ `--out` must name a subdirectory INSIDE this one (one per packaging batch,
#                 e.g. `packages/<bundle-name>/<Unit>/`), never `packages/` itself:
#                 `package_unit.conflicting_evidence_dirs` refuses an `--out` whose parent holds
#                 `oracle/`, and this run root always does. Measured: `--out <run>/packages` exits
#                 2, `--out <run>/packages/<batch>` exits 0. See `tests/test_work_dirs.py`.
#   deliverables/ operator-facing outputs meant for the customer, never for git (issue #322).
#                 ⚠️ NOT eagerly created (issue #481) - see LAZY_SUBDIRS below. Nothing in this
#                 repo writes here today, so an eagerly-created copy is an always-empty folder
#                 that reads to an operator as "something failed", not as a signpost.
#   scratch/      disposable, run-owned - the only subdir a future `--prune` may ever delete
CANONICAL_SUBDIRS: tuple[str, ...] = (
    "assessment",
    "assets",
    "bundle",
    "oracle",
    "packages",
    "deliverables",
    "scratch",
)

#: Canonical subdirs `allocate_run` does NOT create up front. `deliverables/` is the one member
#: today (issue #481): a sweep of `scripts/*.py` for a `deliverables/` writer found none - only
#: this module defines the path, so every run produced a folder nothing ever filled. The concept
#: is still real (issue #322 - operator-facing, customer-bound output needs a home that is not
#: `scratch/`, because it has a different lifecycle), so this keeps the name in `CANONICAL_SUBDIRS`
#: and keeps `.subdir("deliverables")` / `.deliverables` valid - it only stops the FOLDER from
#: existing before anything has a reason to write into it. The `.deliverables` property below
#: creates it on access, so the directory appears exactly when something is about to use it, and
#: an absent folder in an unused run says nothing at all (never a false "this step ran and
#: produced nothing").
LAZY_SUBDIRS: frozenset[str] = frozenset({"deliverables"})

_RUN_DIR_RE = re.compile(r"^(\d+)(?:-.*)?$")
_RESERVATIONS_DIR_NAME = ".run-number-reservations"
# Matches the Fabric artifact-name ceiling used elsewhere in this org's conventions (table names
# under 60 chars) - the slug is decoration, not identity, so there is no reason to let it run long.
_MAX_UNIT_KEY_LEN = 60
_MAX_ALLOCATION_ATTEMPTS = 50  # generous; a genuine collision only ever needs one retry

#: Manifest key under which `allocate_run` records the directory NAME (`<NNN>-<slug>`) it allocated.
#: Reserved exactly like `run`/`unit_key`/`created`/`status`: written AFTER `extra_manifest` is
#: merged, so a caller can neither supply nor clobber it.
#:
#: The NAME is what a renumber/rename changes, and a mismatch here is an ESTABLISHED finding
#: (`moved`, exit 1) rather than an open question. It is not sufficient on its own - see
#: `RUN_PATH_KEY`.
RUN_LOCATION_KEY = "allocated_dir_name"

#: Manifest key under which `allocate_run` records the ABSOLUTE path it allocated. Reserved exactly
#: like `RUN_LOCATION_KEY`.
#:
#: ⚠️ This key exists because the NAME alone cannot answer the question the CLI claims to answer.
#: Measured on the production CLI before it was added: allocate `source\_runs\001-acme`, move that
#: one run to `moved\_runs\001-acme`, and the destination reported `INTACT 001-acme`, exit 0 - the
#: basename never changed, so a run-only move (which breaks every bundle beneath it) was reported as
#: healthy. Copying a run into a second `_runs/` root reported `INTACT` for the copy too.
#:
#: The earlier design note argued that recording an absolute path would be noise "on every clone".
#: That premise is FALSE and is checkable in one command: `_runs/` is git-ignored
#: (`git check-ignore -v -- _runs/001-acme/run.json` -> `.gitignore:162:/_*`), so a clone and a
#: fresh `git worktree` carry NO runs at all - this very worktree has no `_runs/`. The only
#: legitimate whole-tree relocation left is moving the checkout itself, and that breaks the absolute
#: self-paths embedded in `<run>/bundle/` output exactly as a run-only move does. So it is not noise
#: to report it; it is the same damage.
#:
#: What it deliberately does NOT do is guess which of the two happened. A relocated run reports
#: `unverifiable` (exit 3, cannot establish), never `intact` and never `moved` - see
#: `check_run_location`.
RUN_PATH_KEY = "allocated_abs_path"

#: The three states `check_run_location` reports. They are mutually exclusive and a caller must be
#: able to tell them apart: `UNVERIFIABLE` is NOT a soft `INTACT`. Collapsing unassessable input
#: into the clean bucket is this repo's dominant defect class, and it is precisely the defect issue
#: #470 is about - a renumbered run that read back as healthy.
RUN_LOCATION_INTACT = "intact"
RUN_LOCATION_MOVED = "moved"
RUN_LOCATION_DUPLICATE = "duplicate"
RUN_LOCATION_UNVERIFIABLE = "unverifiable"
RUN_LOCATION_STATES: tuple[str, ...] = (
    RUN_LOCATION_INTACT,
    RUN_LOCATION_MOVED,
    RUN_LOCATION_DUPLICATE,
    RUN_LOCATION_UNVERIFIABLE,
)

#: Values of `RunLocationCheck.derived_name_check` - a strictly SUBORDINATE hint, only ever set on an
#: `UNVERIFIABLE` run, and never able to promote one to `INTACT`. See `check_run_location`.
DERIVED_NAME_MATCHES = "matches"
DERIVED_NAME_MISMATCH = "mismatch"
DERIVED_NAME_UNAVAILABLE = "unavailable"
DERIVED_NAME_NOT_CONSULTED = "not_consulted"

#: Values of `RunLocationCheck.path_check`. Also subordinate: `matches` is the only one that permits
#: `INTACT`, and it never CAUSES it on its own (the recorded NAME must match first).
PATH_CHECK_MATCHES = "matches"
PATH_CHECK_DIFFERS = "differs"
PATH_CHECK_UNRECORDED = "unrecorded"
PATH_CHECK_NOT_CONSULTED = "not_consulted"

#: `manifest_status` on every entry `verify_runs` returns. The gate must be able to tell a run it
#: ASSESSED from one it could not read at all, because collapsing the second into the clean bucket
#: is this repo's dominant defect class.
#:
#:  * `ok`         - `run.json` parsed as a JSON object; `location_check` is a real verdict.
#:  * `invalid`    - it parsed as an object but is not a usable manifest (e.g. `"run": "two"`).
#:                   Retained, and forced to `unverifiable`.
#:  * `unreadable` - missing, empty, truncated, malformed, locked, or valid JSON that is not an
#:                   object. `list_runs` (inspection) omits these; `verify_runs` (the gate) MUST
#:                   NOT - a run directory it cannot read contributes `unverifiable`, not nothing.
MANIFEST_OK = "ok"
MANIFEST_INVALID = "invalid"
MANIFEST_UNREADABLE = "unreadable"

#: Keys `verify_runs`/`list_runs` DERIVE onto every entry, alongside `root` and `location_check`.
MANIFEST_STATUS_KEY = "manifest_status"
MANIFEST_DETAIL_KEY = "manifest_detail"


def sanitize_unit_key(name: str) -> str:
    """Turn an arbitrary unit name (a Tableau workbook/site/project display name) into a
    filesystem- and path-safe slug: lowercase ASCII, `-`-separated, never empty, capped at
    `_MAX_UNIT_KEY_LEN` characters.

    A display name is NEVER load-bearing for identity (issue #234, rule 2 - two projects or two
    workbooks can legitimately share a name; `tests/test_harvest_project_scope.py` already fixtures
    exactly that collision). This slug is decoration on the path for human navigation only -
    nothing may parse it back into the original name; the run NUMBER is the actual key.
    """
    if not name:
        return "unit"
    # NFKD-normalize so accented characters degrade to an ASCII base instead of vanishing outright
    # (e.g. "Depot Genève" -> "geneve", not "depot").
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    if not slug:
        return "unit"
    return slug[:_MAX_UNIT_KEY_LEN].strip("-")


def runs_root(repo_root: Path | None = None) -> Path:
    """The one `_runs/` root every pre-bundle stage allocates under."""
    return (repo_root or REPO_ROOT) / "_runs"


@dataclass(frozen=True)
class RunPaths:
    """Everything about one allocated run. `root` is the ONLY thing on disk that identifies it -
    every other accessor is derived, never stored twice. Never rename `root` after allocation
    (issue #234, rule 1): generated bundle output embeds absolute self-paths, so moving a run after
    the fact breaks refresh. `check_run_location` is what detects it if someone does anyway.
    """

    root: Path
    run_number: int
    unit_key: str

    @property
    def manifest_path(self) -> Path:
        """Path to this run's `run.json` - the one authoritative description of the run."""
        return self.root / "run.json"

    def subdir(self, name: str) -> Path:
        """Path to one canonical subdir. Raises on anything not in `CANONICAL_SUBDIRS` rather than
        silently accepting an ad hoc name - that guessing is exactly what created the original mess.
        """
        if name not in CANONICAL_SUBDIRS:
            raise ValueError(f"{name!r} is not a canonical subdir; choose from {CANONICAL_SUBDIRS}")
        return self.root / name

    @property
    def assessment(self) -> Path:
        """`assess_estate.py`-shaped output for this run (report.md, assessment.json, estate.db)."""
        return self.subdir("assessment")

    @property
    def assets(self) -> Path:
        """`harvest_estate_assets.py`-shaped downloads for this run (.twbx / .tdsx)."""
        return self.subdir("assets")

    @property
    def bundle(self) -> Path:
        """`run_estate.py`-shaped conversion output for this run (report.json, pbip/, handover/)."""
        return self.subdir("bundle")

    @property
    def oracle(self) -> Path:
        """`capture_tableau_oracle.py`-shaped visual + numeric reference capture for this run."""
        return self.subdir("oracle")

    @property
    def packages(self) -> Path:
        """`package_unit.py`-shaped per-unit, agent-facing handover packages (issue #446).

        Point `--out` at a subdirectory of this, never at this directory itself - see the
        `CANONICAL_SUBDIRS` comment for the measured reason.
        """
        return self.subdir("packages")

    @property
    def deliverables(self) -> Path:
        """Operator-facing outputs meant for the customer, never for git (issue #322).

        Created lazily, on first access (issue #481): unlike the other six canonical subdirs,
        `allocate_run` does not create this one up front - see `LAZY_SUBDIRS`. Accessing this
        property is what brings the directory into being, so it appears exactly when something is
        about to write into it rather than sitting empty in every run that never uses it.
        """
        path = self.subdir("deliverables")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def scratch(self) -> Path:
        """Disposable, run-owned scratch - the only subdir a future `--prune` may ever delete."""
        return self.subdir("scratch")


def _existing_run_numbers(root: Path) -> list[int]:
    """Every run number already allocated under `root`, regardless of unit. Numbering is GLOBAL
    across `_runs/` (issue #234's own worked example numbers `001-entdash`, `002-shipping`
    consecutively across two different units), never per-unit.
    """
    if not root.is_dir():
        return []
    numbers = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        match = _RUN_DIR_RE.match(child.name)
        if match:
            numbers.append(int(match.group(1)))
    return numbers


def _run_dir_name(run_number: int, unit_key: str) -> str:
    """The one place the `<NNN>-<slug>` directory name is formed. `allocate_run` uses it to create
    the directory and `_derived_dir_name` uses it to reconstruct one, so the zero-padding rule
    cannot drift between the two.
    """
    return f"{_run_number_dir_name(run_number)}-{unit_key}"


def _run_number_dir_name(run_number: int) -> str:
    """The number-only directory name used to reserve a run number before adding its decorative slug."""
    width = max(3, len(str(run_number)))
    return f"{run_number:0{width}d}"


def _reservations_root(root: Path) -> Path:
    """The private directory holding permanent, number-only allocation reservations."""
    return root / _RESERVATIONS_DIR_NAME


def _derived_dir_name(manifest: dict[str, Any]) -> str | None:
    """Reconstruct the directory name a manifest's own `run`/`unit_key` IMPLY, or None if they
    cannot. This is inference, never evidence - it depends on `_run_dir_name`'s padding rule holding
    for the life of the run, which a recorded name does not. Its only job is to give a LEGACY
    manifest (one written before `RUN_LOCATION_KEY` existed) a subordinate hint; it may never
    promote such a run out of `unverifiable`.
    """
    run_number = manifest.get("run")
    unit_key = manifest.get("unit_key")
    # `isinstance(True, int)` is True in Python, so booleans have to be excluded explicitly.
    if isinstance(run_number, bool) or not isinstance(run_number, int) or run_number < 0:
        return None
    if not isinstance(unit_key, str) or not unit_key:
        return None
    return _run_dir_name(run_number, unit_key)


@dataclass(frozen=True)
class RunLocationCheck:
    """Whether a run directory is still where it was allocated. Read `state` - never `detail`.

    `derived_name_check` and `path_check` are deliberately not part of `state`: they are hints, and
    neither a `matches` derived name nor anything else can promote a run to `intact`.
    """

    state: str
    actual_dir_name: str
    recorded_dir_name: str | None
    derived_name_check: str
    path_check: str
    detail: str

    @property
    def is_intact(self) -> bool:
        """True only for a run whose RECORDED name and RECORDED path both match where it sits."""
        return self.state == RUN_LOCATION_INTACT

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable form, as attached by `list_runs` and printed by `--verify --json`."""
        return {
            "state": self.state,
            "actual_dir_name": self.actual_dir_name,
            "recorded_dir_name": self.recorded_dir_name,
            "derived_name_check": self.derived_name_check,
            "path_check": self.path_check,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------------------------
# THE ONE INVARIANT - and the two functions that implement it
# --------------------------------------------------------------------------------------------
# Every child DIRECTORY of an existing `_runs/` root is a candidate run. Any candidate whose
# identity cannot be POSITIVELY established from its own evidence is `unverifiable`. `intact` is
# only ever returned on positive proof.
#
# It is implemented at exactly TWO places on purpose, and adding a third is how the defect class
# comes back:
#
#   * `_discover_run_dirs`  decides what a CANDIDATE is. It filters on nothing except "is this
#     child a directory", and it never turns an error into an empty list.
#   * `check_run_location`  decides the VERDICT. It is the only function in this module that can
#     construct `RUN_LOCATION_INTACT`, and it does so on its last line, after every requirement
#     below has held. Everything it calls computes a FACT (a normalized path, a derived name, a
#     reason string) and chooses no state.
#
# ⚠️ Why the structure rather than more checks. Round 1 of PR #477's review fixed one fail-open
# site (`list_runs` was the gate's only input, so an unreadable run counted as nothing). Round 2
# then found the SAME defect class at three MORE sites - the path comparison accepted a relative
# recorded value completed against the verifier's own CWD; discovery dropped any directory whose
# name no longer matched `<NNN>` and had no `run.json`, and turned an `OSError` on the root into
# zero runs; and the run-number check never compared the number against the directory it sits in.
# Three more local guards would have left a fourth site to find. `tests/test_work_dirs.py` pins the
# collapse structurally: `intact` may be CONSTRUCTED at exactly one site in this file.


#: Two unrelated anchors used to decide whether a RECORDED path names a fixed location at all.
#: BOTH are required, because neither alone is sufficient on Windows: the driveless `\wd-anchor`
#: absorbs a drive-relative `\_runs\001-acme` (`ntpath.join` keeps the second operand's root), while
#: the drive-bearing `Z:\wd-anchor` completes it and so exposes it. On POSIX the first is simply a
#: relative anchor and the second is `/wd-anchor`; both reject a relative value and accept an
#: absolute one, so the pair needs no `os.name` branch.
_LOCATION_ANCHORS: tuple[str, ...] = ("Z:" + os.sep + "wd-anchor", os.sep + "wd-anchor")


def _is_location_independent(value: str) -> bool:
    """True only when `value` names the SAME place no matter where the checking process stands.

    ⚠️ This is NOT `os.path.isabs`, and the difference is a measured fail-open defect (PR #477,
    review round 2, finding 1). `allocated_abs_path` is *required* to be absolute, but the
    comparison ran the recorded value through `abspath()`, which happily completes a RELATIVE
    value against the verifier's current directory. Recording
    `_review477_round2_attacks\\relative-path\\_runs\\001-acme` and running the CLI from the right
    directory produced `INTACT 001-acme`, exit 0 - a location verdict that depended on where the
    operator happened to be standing, in the one module that exists to be CWD-independent.

    ⚠️ `isabs` is a VERSION-DEPENDENT predicate here, which is the reason not to use it - it is not
    that `isabs` is wrong about every drive-relative shape. Measured, `ntpath.isabs`:

        value                    CPython 3.11.10    CPython 3.13.2
        '\\_runs\\001-acme'        True  <- the miss   False
        'C:001-acme'             False              False

    So on 3.13 `isabs` alone would have been enough here, and on 3.11 it would have accepted a path
    that still resolves against whatever drive the process is on. Exactly one shape, on the older
    interpreter - an earlier version of this note claimed two, and overstating a justification is
    how the next reader over-trusts the next claim.

    The property is therefore tested directly, as two facts that are both needed and both
    interpreter-independent: the value must be rooted within whatever drive it names (`C:001-acme`
    is not), and it must name a drive or root of its own, established by joining it onto unrelated
    anchors and demanding it come back unchanged. Anything a CWD, or a current drive, would
    complete fails one of the two.
    """
    try:
        # Fact 1 - the path must be rooted WITHIN whatever drive it names. `C:001-acme` carries a
        # drive and is still completed against that drive's current directory, and it survives the
        # anchor test below (`ntpath.join` returns a different-drive operand untouched), so the two
        # facts are both load-bearing and neither alone is this predicate.
        _drive, rest = os.path.splitdrive(value)
        separators = (os.sep,) if os.altsep is None else (os.sep, os.altsep)
        if not rest.startswith(separators):
            return False
        # Fact 2 - and it must name a drive/root of its own. A driveless `\_runs\001-acme` passes
        # fact 1 and still resolves against the CURRENT drive; joining it onto a drive-bearing
        # anchor changes it, joining an absolute path onto any anchor does not.
        plain = os.path.normcase(os.path.normpath(value))
        return all(
            os.path.normcase(os.path.normpath(os.path.join(anchor, value))) == plain for anchor in _LOCATION_ANCHORS
        )
    except (OSError, ValueError):  # a NUL byte or an otherwise unrepresentable path
        return False


def _normalized_path(path: Any) -> str | None:
    """One comparable spelling of a path, or None if it cannot be formed.

    `abspath` (not `resolve`) on purpose: it normalizes separators, `..` and case-folding without
    reading the filesystem, which keeps `check_run_location` pure. Returning None on a path that
    cannot be normalized is fail-safe: None never compares equal to anything, including itself, so
    a broken value can only ever produce `unverifiable`, never a false `intact`.

    ⚠️ The two sides are NOT symmetrical, and pretending they were is finding 1. Completing the
    ACTUAL run directory against the CWD is correct - it is a live path this process just
    enumerated from a root the caller named, so it really is there. Completing the RECORDED value
    the same way is not: it was written by a different process, at a different time, from a
    different directory, and only an absolute value carries any meaning across that gap. The
    recorded side therefore has to clear `_is_location_independent` BEFORE it reaches this
    function - see `_recorded_path_problem`.
    """
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except (OSError, ValueError):  # pragma: no cover - a NUL byte or an unrepresentable path
        return None


def _same_path(recorded: str, actual: Path) -> bool:
    """True only when both sides normalize AND are equal. Never true on an unusable value."""
    left = _normalized_path(recorded)
    right = _normalized_path(actual)
    return left is not None and right is not None and left == right


def _recorded_path_problem(value: Any) -> str | None:
    """Why `manifest[RUN_PATH_KEY]` cannot serve as evidence of where a run was allocated, or None.

    A fact, not a verdict - `check_run_location` decides what to do with it.
    """
    if not isinstance(value, str) or not value:
        return f"no usable {RUN_PATH_KEY!r} was recorded ({value!r})"
    if not _is_location_independent(value):
        return (
            f"its recorded {RUN_PATH_KEY!r} ({value!r}) is not an absolute path, so it names no fixed "
            "location - it would be completed against the VERIFIER's current directory, which makes the "
            "verdict depend on where the check was run from rather than on where the run is"
        )
    return None


def _run_number_problem(manifest: dict[str, Any]) -> str | None:
    """Why `manifest['run']` is not a usable run number, or None if it is.

    The run NUMBER is the identity of a run, so a manifest that does not carry a usable one is not
    assessable, however healthy the rest of it looks. It is also what `_collect_runs` sorts on:
    sorting an unvalidated value raised `TypeError: '<' not supported between instances of 'str'
    and 'int'` on a tree holding both `{"run": 1}` and `{"run": "two"}`, which broke the documented
    "never raises" contract and stopped any `unverifiable` verdict from ever being reached.
    """
    if "run" not in manifest:
        return "it records no 'run' number"
    run_number = manifest.get("run")
    # `isinstance(True, int)` is True in Python, so booleans have to be excluded explicitly.
    if isinstance(run_number, bool) or not isinstance(run_number, int) or run_number < 0:
        return f"its 'run' is not a usable run number ({run_number!r})"
    return None


def _name_agreement(manifest: dict[str, Any], actual: str) -> tuple[str, str | None]:
    """`(derived_name_check, problem_or_None)` for "does this manifest's own identity match the
    directory it sits in?".

    ⚠️ This is finding 3 of PR #477's second review round, and it is why `_derived_dir_name` is
    consulted on the healthy path at all. `_run_number_problem` only ever checked that `run` was a
    non-negative integer, never that it AGREED with the `<NNN>-<slug>` directory. Changing nothing
    but `"run"` in a freshly allocated `001-acme` - to `2`, or to `10**100` - was reported `1
    intact`, exit 0. The run number IS the identity in this convention (issue #234), so a manifest
    claiming a number its own directory does not carry is CONTRADICTORY evidence: `unverifiable`,
    not healthy, and not `moved` either, because which of the two is wrong is not established.
    """
    derived = _derived_dir_name(manifest)
    if derived is None:
        return DERIVED_NAME_UNAVAILABLE, "its own 'run'/'unit_key' cannot reconstruct a directory name to check against"
    if derived != actual:
        return DERIVED_NAME_MISMATCH, (
            f"its own 'run'/'unit_key' name {derived!r}, which CONTRADICTS the directory {actual!r} it "
            "sits in - the run number is the identity of a run, so a manifest claiming a different one "
            "is contradictory evidence, not a healthy run"
        )
    return DERIVED_NAME_MATCHES, None


def _legacy_name_hint(manifest: dict[str, Any], actual: str, recorded: Any) -> tuple[str, str]:
    """`(derived_name_check, detail)` for a manifest that records no usable directory NAME - every
    run allocated before `RUN_LOCATION_KEY` existed, plus any hand-edited or corrupted value.

    A subordinate hint reconstructs the name the manifest's own `run`/`unit_key` IMPLY. It can never
    promote the run out of `unverifiable`: an inference is not a record, and the caller below does
    not offer it that option.
    """
    if recorded is None:
        missing = f"no {RUN_LOCATION_KEY!r} recorded (allocated before this key existed)"
    else:
        missing = f"{RUN_LOCATION_KEY!r} is present but is not a usable directory name ({recorded!r})"

    hint, problem = _name_agreement(manifest, actual)
    if hint == DERIVED_NAME_UNAVAILABLE:
        why = "and its own run/unit_key cannot reconstruct one either"
    elif hint == DERIVED_NAME_MATCHES:
        why = f"its run/unit_key imply {_derived_dir_name(manifest)!r}, which matches - but that is INFERENCE"
    else:
        why = f"{problem} - but that is INFERENCE, not a record"
    return hint, f"{missing}; {why}"


def _unverifiable(
    run_dir: Path,
    detail: str,
    *,
    recorded_dir_name: str | None = None,
    derived_name_check: str = DERIVED_NAME_NOT_CONSULTED,
    path_check: str = PATH_CHECK_NOT_CONSULTED,
) -> RunLocationCheck:
    """Construct the fail-closed verdict. Every failing branch of `check_run_location` comes here,
    so "could not establish" is the DEFAULT shape of a verdict in this module and `intact` is the
    single exception that has to be earned."""
    return RunLocationCheck(
        state=RUN_LOCATION_UNVERIFIABLE,
        actual_dir_name=run_dir.name,
        recorded_dir_name=recorded_dir_name,
        derived_name_check=derived_name_check,
        path_check=path_check,
        detail=detail,
    )


def _unassessable(run_dir: Path, detail: str) -> RunLocationCheck:
    """The verdict for a run directory whose manifest could not be read or parsed at all."""
    return _unverifiable(
        run_dir,
        detail,
        derived_name_check=DERIVED_NAME_UNAVAILABLE,
        path_check=PATH_CHECK_UNRECORDED,
    )


# pylint: disable-next=too-many-return-statements
def check_run_location(manifest: Any, run_dir: Path) -> RunLocationCheck:
    """Compare where a run says it was allocated against where it actually sits. Pure: reads a
    manifest dict and a path, touches no disk, mutates nothing, and never raises (issue #470).

    ⚠️ **This is the module's single classification path, and the eight returns below are one
    ordered ladder of REQUIREMENTS, not eight independent guards.** Splitting them across helpers
    that each decided a state is what let three further fail-open sites survive round 1 of PR
    #477's review (see the invariant block above); keeping the whole sequence here is deliberate,
    and so is the `too-many-return-statements` waiver. Everything it calls returns a fact.

    Three states, and the third is the point:

    * `intact` - EVERY requirement held: `run.json` is an object, it records a `<NNN>-<slug>` name
      equal to this directory's own, it carries a usable run number, that number and `unit_key`
      reconstruct the directory it sits in, and it records an absolute allocation path that is
      where the directory actually is. Reached on the last line and nowhere else.
    * `moved` - the recorded name and the actual name differ. The run has been renamed or
      renumbered since allocation, so generated bundle output beneath it embeds absolute self-paths
      that no longer resolve. Both names are reported so the damage is locatable. It is the ONLY
      failure that is an established finding rather than an unanswered question.
    * `unverifiable` - the question cannot be answered from the evidence. Not a pass and not a
      failure: the answer is unknown, which is a different thing from answering it "fine".

    `manifest` is typed `Any` on purpose - a hand-written `run.json` can hold a list, a string or a
    number, and this function must return a verdict for it rather than raise.
    """
    actual = run_dir.name

    # Requirement 0 - it is a manifest at all.
    if not isinstance(manifest, dict):
        return _unassessable(
            run_dir,
            f"run.json holds {type(manifest).__name__}, not a JSON object, so it records "
            "nothing that could place this run",
        )

    recorded = manifest.get(RUN_LOCATION_KEY)

    # The one ESTABLISHED finding: a recorded name that differs from the directory's own.
    if isinstance(recorded, str) and recorded and recorded != actual:
        return RunLocationCheck(
            state=RUN_LOCATION_MOVED,
            actual_dir_name=actual,
            recorded_dir_name=recorded,
            derived_name_check=DERIVED_NAME_NOT_CONSULTED,
            path_check=PATH_CHECK_NOT_CONSULTED,
            detail=(
                f"allocated as {recorded!r} but found as {actual!r} - renamed or renumbered since "
                "allocation; generated bundle output embeds absolute self-paths, so treat every "
                "bundle under this run as broken until re-checked"
            ),
        )

    # Requirement 1 - a recorded directory name to compare at all.
    if not isinstance(recorded, str) or not recorded:
        hint, detail = _legacy_name_hint(manifest, actual, recorded)
        return _unverifiable(run_dir, detail, derived_name_check=hint)

    # Requirement 2 - a usable run number, because the number is the identity.
    problem = _run_number_problem(manifest)
    if problem is not None:
        return _unverifiable(
            run_dir,
            f"its recorded name {recorded!r} matches where it sits, but {problem} - so this is not an "
            "assessable manifest and its location cannot be confirmed",
            recorded_dir_name=recorded,
        )

    # Requirement 3 - that number and `unit_key` must reconstruct THIS directory (finding 3).
    hint, disagreement = _name_agreement(manifest, actual)
    if disagreement is not None:
        return _unverifiable(
            run_dir,
            f"its recorded name {recorded!r} matches where it sits, but {disagreement}",
            recorded_dir_name=recorded,
            derived_name_check=hint,
        )

    # Requirement 4 - an absolute allocation path, because a basename survives a move and a copy.
    recorded_path = manifest.get(RUN_PATH_KEY)
    path_problem = _recorded_path_problem(recorded_path)
    if path_problem is not None:
        return _unverifiable(
            run_dir,
            f"allocated as {recorded!r} and still named that, but {path_problem} - a basename survives a "
            "run-only move and a copy, so 'still there' cannot be confirmed",
            recorded_dir_name=recorded,
            derived_name_check=hint,
            path_check=PATH_CHECK_UNRECORDED,
        )

    # Requirement 5 - that path is where the directory actually sits. A relocation is `unverifiable`
    # rather than `moved`: whether the whole checkout moved (legitimate) or this run alone was moved
    # or copied (corruption) cannot be told apart from one run's evidence, and both break the
    # absolute self-paths embedded under `<run>/bundle/` anyway.
    if not _same_path(recorded_path, run_dir):
        return _unverifiable(
            run_dir,
            f"allocated at {recorded_path} but found at {os.path.abspath(str(run_dir))} - the name is "
            "unchanged, so this is a RELOCATION, not a rename. Moving the whole checkout and moving/copying "
            "this run alone are indistinguishable from here, and BOTH break the absolute self-paths embedded "
            "under bundle/, so re-check every bundle under this run rather than reading it as healthy",
            recorded_dir_name=recorded,
            derived_name_check=hint,
            path_check=PATH_CHECK_DIFFERS,
        )

    # Every requirement held. This is the ONLY `intact` in the module - see the invariant block.
    return RunLocationCheck(
        state=RUN_LOCATION_INTACT,
        actual_dir_name=actual,
        recorded_dir_name=recorded,
        derived_name_check=hint,
        path_check=PATH_CHECK_MATCHES,
        detail=f"allocated as {recorded!r} at {recorded_path} and still there",
    )


def allocate_run(
    unit_key: str,
    *,
    repo_root: Path | None = None,
    extra_manifest: dict[str, Any] | None = None,
    create_subdirs: bool = True,
) -> RunPaths:
    """Atomically allocate the next numbered run directory for `unit_key` under `_runs/`.

    Allocation is `mkdir`-exclusive with retry on collision (issue #234 acceptance criterion 1),
    never read-the-max-then-write: a number-only directory is reserved permanently before its
    decorative slug is added, so two processes racing with different slugs cannot both believe they
    got the same number.
    The directory name is `<NNN>-<slug>`, zero-padded to at least 3 digits; the slug is decoration
    only (never parsed back - see `sanitize_unit_key`), the number is the identity.

    When `create_subdirs` is true, every canonical subdir EXCEPT those in `LAZY_SUBDIRS` is
    created immediately. `deliverables/` is the one member today (issue #481): it has no writer
    yet, so creating it up front produced an always-empty folder that read to an operator as a
    failed or skipped step. Its `RunPaths.deliverables` accessor still creates it on first access.

    ⚠️ **The directory this returns must never be renamed, renumbered or reused.** Generated bundle
    output under `<run>/bundle/` embeds ABSOLUTE self-paths, so renaming a run afterwards breaks
    every bundle beneath it (refresh, `byPath` report-to-model binding, `_build/` replay). A real
    customer reorg renumbered 14 run directories on exactly this misunderstanding - see the module
    docstring, `docs/migration-phases.md` (Phase 1) and `docs/operator-runbook.md` (§6.4), and issue
    #470. Allocate a NEW run instead; numbering is cheap and gaps are expected.

    One run per PIPELINE RUN, not one per workbook: a 48-workbook estate sweep is a single run whose
    per-workbook units live inside its `bundle/pbip/`.

    So that a rename is at least DETECTABLE afterwards, the directory name is also written into the
    manifest under `RUN_LOCATION_KEY` and its absolute path under `RUN_PATH_KEY`;
    `check_run_location` compares both. The name alone is not enough - a basename is unchanged by a
    run-only move or a copy, which is exactly how a moved run used to read back as `intact`.

    `extra_manifest` is merged in FIRST, so a caller cannot accidentally clobber the reserved
    `run` / `unit_key` / `created` / `status` / `allocated_dir_name` / `allocated_abs_path` keys
    this function itself writes.
    """
    root = runs_root(repo_root)
    root.mkdir(parents=True, exist_ok=True)
    reservation_root = _reservations_root(root)
    reservation_root.mkdir(parents=False, exist_ok=True)
    slug = sanitize_unit_key(unit_key)

    existing = _existing_run_numbers(root) + _existing_run_numbers(reservation_root)
    candidate = (max(existing) + 1) if existing else 1
    for _ in range(_MAX_ALLOCATION_ATTEMPTS):
        reservation_dir = reservation_root / _run_number_dir_name(candidate)
        run_dir = root / _run_dir_name(candidate, slug)
        try:
            reservation_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            candidate += 1
            continue
        run_dir.mkdir(parents=False, exist_ok=False)

        run = RunPaths(root=run_dir, run_number=candidate, unit_key=slug)
        if create_subdirs:
            for sub in CANONICAL_SUBDIRS:
                if sub in LAZY_SUBDIRS:
                    continue  # issue #481 - e.g. deliverables/: created on first access, not here
                run.subdir(sub).mkdir(parents=True, exist_ok=True)

        manifest: dict[str, Any] = dict(extra_manifest or {})
        manifest.update(
            {
                "run": candidate,
                "unit_key": slug,
                "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "active",
                RUN_LOCATION_KEY: run_dir.name,
                RUN_PATH_KEY: os.path.abspath(str(run_dir)),
            }
        )
        run.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return run

    raise RuntimeError(
        f"could not allocate a run directory for {unit_key!r} under {root} after "
        f"{_MAX_ALLOCATION_ATTEMPTS} attempts - is something else allocating concurrently?"
    )


def _discover_run_dirs(root: Path) -> tuple[list[Path], str | None]:
    """`(candidates, root_problem)` - the module's one discovery site. Never raises.

    Every child directory of an existing `_runs/` root except this module's private
    `_RESERVATIONS_DIR_NAME` metadata directory is a candidate run. There is no name pattern and no
    "has a `run.json`" precondition, because both were fail-open filters and both
    are finding 2 of PR #477's second review round: a real allocated run whose directory was
    renamed to `acme-without-number` and whose manifest was deleted matched neither test, so the
    CLI printed `(no run directories found)`, `0 run(s): 0 intact, 0 moved, 0 unverifiable`, exit
    0. Round 1 had stopped `list_runs` from dropping runs it could not read; discovery was dropping
    them one layer earlier. Deciding whether a directory is a run is exactly the question
    `check_run_location` exists to answer from evidence - so discovery must not pre-answer it.

    ⚠️ `root_problem` is the other half, and it is why this returns a pair rather than a list. An
    `OSError` from enumerating the root used to become an empty list, i.e. "zero runs, exit 0",
    while the runs underneath it still existed and simply could not be assessed. That is NOT the
    accepted "the run was deleted entirely" residual: the tree is there. The caller turns a
    `root_problem` into an `unverifiable` entry, so the gate exits 3.

    Only a root that genuinely does not exist yields `([], None)` - nothing has been allocated, and
    a tree with no runs in it is legitimately clean.
    """
    try:
        root_mode = os.stat(root).st_mode
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], (
            f"the _runs root at {root} exists but could not be examined "
            f"({exc.__class__.__name__}: {exc}) - any run beneath it is undiscoverable, which is "
            "not the same as there being none"
        )
    if not stat.S_ISDIR(root_mode):
        return [], f"the _runs root at {root} exists but is not a directory, so no run under it can be discovered"

    try:
        children = sorted(root.iterdir())
    except OSError as exc:
        return [], (
            f"the _runs root at {root} could not be listed ({exc.__class__.__name__}: {exc}) - any "
            "run beneath it is undiscoverable, which is not the same as there being none"
        )

    found: list[Path] = []
    for child in children:
        if child.name == _RESERVATIONS_DIR_NAME:
            continue
        try:
            child_mode = os.stat(child).st_mode
        except OSError:
            # Cannot establish that this is NOT a run directory, so it is assessed rather than
            # skipped. `Path.is_dir()` is unusable here: it swallows OSError and answers False,
            # which is the fail-open shape - "I could not look" reported as "it is not one".
            found.append(child)
            continue
        if stat.S_ISDIR(child_mode):
            found.append(child)
    return found, None


def _read_run_manifest(run_dir: Path) -> tuple[dict[str, Any] | None, str, str]:
    """`(manifest_or_None, status, detail)` for one run directory. Never raises.

    `None` means "not readable as a manifest object" - missing, empty, truncated, malformed, locked
    (a Windows `FileShare.None` handle raises `OSError`), or valid JSON that is not an object. Each
    of those used to be a silent `continue`, which is how a run directory contributed zero to every
    bucket of a GATE.
    """
    manifest_path = run_dir / "run.json"
    try:
        if not manifest_path.is_file():
            return (
                None,
                MANIFEST_UNREADABLE,
                (
                    "run.json is missing - a directory exists but nothing describes it (allocation "
                    "interrupted after mkdir, or a hand-made directory)"
                ),
            )
        raw = manifest_path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        # `ValueError` covers `UnicodeDecodeError` - a `run.json` holding bytes that are not UTF-8
        # raised it straight out of `read_text`, past a guard that named only `OSError`.
        return None, MANIFEST_UNREADABLE, f"run.json could not be read ({exc.__class__.__name__}: {exc})"

    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # `ValueError`, not `json.JSONDecodeError`: a JSON integer with more than 4300 digits makes
        # `json.loads` raise a bare `ValueError` from CPython's int/str conversion limit, which
        # escaped the narrower guard and exited 1 with a traceback instead of reporting
        # `unverifiable`. `RecursionError` is the same shape for deeply nested arrays. Both are
        # fail-CLOSED, but this function documents that it never raises, so it must not.
        empty = " (the file is empty)" if not raw.strip() else ""
        return None, MANIFEST_UNREADABLE, f"run.json is not valid JSON{empty} ({exc.__class__.__name__}: {exc})"

    # Valid JSON that is not an object is not a manifest. `.get`/`.setdefault` on a list raise
    # `AttributeError`, which used to escape the `JSONDecodeError`/`OSError` guard entirely.
    if not isinstance(data, dict):
        return None, MANIFEST_UNREADABLE, f"run.json holds valid JSON that is not an object ({type(data).__name__})"

    problem = _run_number_problem(data)
    if problem is not None:
        return data, MANIFEST_INVALID, f"run.json parsed, but {problem}"
    return data, MANIFEST_OK, "run.json parsed and carries a usable run number"


def _run_entry(run_dir: Path) -> dict[str, Any]:
    """One entry for `list_runs`/`verify_runs`: the manifest (when there is one) plus the derived
    `root`, `manifest_status`, `manifest_detail` and `location_check` keys."""
    data, status, detail = _read_run_manifest(run_dir)
    entry: dict[str, Any] = dict(data) if isinstance(data, dict) else {}
    entry["root"] = str(run_dir)
    entry[MANIFEST_STATUS_KEY] = status
    entry[MANIFEST_DETAIL_KEY] = detail
    check = check_run_location(data, run_dir) if data is not None else _unassessable(run_dir, detail)
    entry["location_check"] = check.as_dict()
    return entry


def _run_sort_key(entry: dict[str, Any]) -> tuple[int, int, str]:
    """Type-stable ordering: usable run numbers first and in numeric order, everything else after
    them by directory name. Sorting on the raw `run` value raised `TypeError` on a tree holding both
    `{"run": 1}` and `{"run": "two"}` - see `_run_number_problem`.
    """
    name = str(entry.get("location_check", {}).get("actual_dir_name", ""))
    run_number = entry.get("run")
    if isinstance(run_number, bool) or not isinstance(run_number, int):
        return (1, 0, name)
    return (0, run_number, name)


def _root_problem_entry(root: Path, problem: str) -> dict[str, Any]:
    """An `unverifiable` entry for the `_runs/` ROOT itself, when the root exists but its children
    cannot be enumerated. Without it, `_discover_run_dirs`'s `root_problem` would have nowhere to
    go and the gate would report an unreadable tree as an empty one - exit 0 (finding 2)."""
    return {
        "root": str(root),
        MANIFEST_STATUS_KEY: MANIFEST_UNREADABLE,
        MANIFEST_DETAIL_KEY: problem,
        "location_check": _unassessable(root, problem).as_dict(),
    }


def _collect_runs(repo_root: Path | None = None) -> list[dict[str, Any]]:
    """EVERY candidate run under `_runs/`, assessable or not, sorted by `_run_sort_key`.

    This is the discovery primitive both `list_runs` (inspection, which filters) and `verify_runs`
    (the gate, which must not) are built from. Splitting them is the fix for the fail-open defect:
    inspection is entitled to skip a directory it cannot read, a gate never is.
    """
    root = runs_root(repo_root)
    candidates, root_problem = _discover_run_dirs(root)
    entries = [_run_entry(run_dir) for run_dir in candidates]
    if root_problem is not None:
        entries.append(_root_problem_entry(root, root_problem))
    entries.sort(key=_run_sort_key)
    return entries


def list_runs(repo_root: Path | None = None) -> list[dict[str, Any]]:
    """Read back every run's manifest, sorted by run number. Pure INSPECTION - never mutates, and
    never raises on a hand-created or half-written run directory; it just skips one with no
    readable `run.json` rather than crashing a status query over it.

    ⚠️ That skip is why this function must never be a gate's only input. A run directory it omits is
    invisible, not clean. `verify_runs` deliberately does NOT go through here - it reports every
    discovered directory, and classifies an unreadable one `unverifiable`. If you are deciding
    whether a tree is healthy, call `verify_runs`.

    Four keys are DERIVED and overwrite anything of the same name in the manifest, because all four
    describe the run *now* and a stale recorded value silently presented as current is the failure
    this whole function is being audited for:

    * `root` - the directory it was actually found in.
    * `manifest_status` / `manifest_detail` - `MANIFEST_OK` or `MANIFEST_INVALID` here (an
      unreadable manifest never survives the filter).
    * `location_check` - `check_run_location(...).as_dict()`, i.e. `intact` / `moved` /
      `unverifiable`. Read `location_check["state"]`; never infer health from `root` alone, which is
      derived from wherever the directory sits and so is true by construction (issue #470).
    """
    return [entry for entry in _collect_runs(repo_root) if entry[MANIFEST_STATUS_KEY] != MANIFEST_UNREADABLE]


def verify_runs(repo_root: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every run DIRECTORY plus a count per `RUN_LOCATION_STATES` entry. Read-only, but a GATE.

    ⚠️ It does not filter, and that is the whole point. Built on `list_runs` it inherited
    inspection's right to skip: a `_runs/001-acme/` whose `run.json` was missing, empty, truncated,
    malformed, locked, or valid-JSON-but-not-an-object contributed ZERO to every bucket, and the CLI
    printed `0 run(s): 0 intact, 0 moved, 0 unverifiable`, exit 0 - the `unverifiable` bucket
    sitting at 0 precisely when something was unverifiable. Every discovered directory is counted
    now, and one that cannot be assessed counts as `unverifiable` (exit 3). Runs whose directory
    names carry the same number are `duplicate` (exit 2), not intact.

    ⚠️ "Discovered" means what `_discover_run_dirs` returns, which is EVERY child directory of the
    root plus the root itself when the root cannot be enumerated. A directory renamed out of the
    `<NNN>-` pattern with its manifest deleted, and an `OSError` on the root, both used to produce
    `0 run(s)`, exit 0, with the tree still sitting on disk.
    """
    runs = _collect_runs(repo_root)
    numbers: dict[int, list[dict[str, Any]]] = {}
    for run in runs:
        name = run.get("location_check", {}).get("actual_dir_name", "")
        match = _RUN_DIR_RE.match(name)
        if match:
            numbers.setdefault(int(match.group(1)), []).append(run)
    for number, duplicates in numbers.items():
        if len(duplicates) < 2:
            continue
        for run in duplicates:
            check = run["location_check"]
            if check["state"] == RUN_LOCATION_INTACT:
                check["state"] = RUN_LOCATION_DUPLICATE
                check["detail"] = (
                    f"run number {number} is used by {len(duplicates)} directories - run numbers are identities "
                    "and must never be reused"
                )
    counts = {state: 0 for state in RUN_LOCATION_STATES}
    for run in runs:
        state = run.get("location_check", {}).get("state", RUN_LOCATION_UNVERIFIABLE)
        counts[state] = counts.get(state, 0) + 1
    return runs, counts


def verify_exit_code(counts: dict[str, int]) -> int:
    """Gate semantics: 0 clean, 1 moved, 2 duplicate identities, 3 cannot establish - and no
    nonzero exit is a pass. A moved run outranks duplicate identities and an unanswered question.
    """
    if counts.get(RUN_LOCATION_MOVED):
        return 1
    if counts.get(RUN_LOCATION_DUPLICATE):
        return 2
    if counts.get(RUN_LOCATION_UNVERIFIABLE):
        return 3
    return 0


def _print_verify_text(runs: list[dict[str, Any]], counts: dict[str, int], root: Path) -> None:
    """Human-readable `--verify` report. The STATE is printed in caps and first on every line, so a
    subordinate `derived name check` or `path check` hint can never be mistaken for the verdict.
    """
    print(f"_runs/ location check: {root}")
    if not runs:
        print("  (no run directories found)")
    for run in runs:
        check = run.get("location_check", {})
        print(f"  {check.get('state', '?').upper():<13} {check.get('actual_dir_name', '?')}")
        print(f"                {check.get('detail', '')}")
    summary = ", ".join(f"{counts.get(state, 0)} {state}" for state in RUN_LOCATION_STATES)
    print(f"{len(runs)} run(s): {summary}")
    if counts.get(RUN_LOCATION_UNVERIFIABLE):
        print(
            "  'unverifiable' is NOT 'intact' - those runs could not be assessed (no readable "
            "run.json, no recorded allocation path, or relocated since allocation)."
        )


def main() -> int:
    """CLI: allocate one run for `unit` and print its canonical paths, or `--verify` the tree."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("unit", nargs="?", help="unit name/key to allocate a run for (e.g. a workbook or site slug)")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="print the allocated paths as JSON")
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "allocate nothing; report whether every run is still where it was allocated. "
            "Exit 0 all intact / 1 at least one moved / 2 duplicate number / 3 at least one unverifiable"
        ),
    )
    args = parser.parse_args()

    if args.verify:
        runs, counts = verify_runs(args.repo_root)
        if args.json:
            print(json.dumps({"runs_root": str(runs_root(args.repo_root)), "counts": counts, "runs": runs}, indent=2))
        else:
            _print_verify_text(runs, counts, runs_root(args.repo_root))
        return verify_exit_code(counts)

    if not args.unit:
        parser.error("unit is required unless --verify is given")

    run = allocate_run(args.unit, repo_root=args.repo_root)
    subdirs = {name: str(run.subdir(name)) for name in CANONICAL_SUBDIRS}
    if args.json:
        print(
            json.dumps(
                {"root": str(run.root), "run_number": run.run_number, "unit_key": run.unit_key, **subdirs}, indent=2
            )
        )
    else:
        print(f"allocated run {run.run_number} for {run.unit_key!r} at {run.root}")
        for name, path in subdirs.items():
            print(f"  {name}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
