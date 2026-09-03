"""
purpose: single source of truth for the canonical PRE-BUNDLE work layout - resolve, allocate and
         expose per-unit, per-run scratch/output paths so scripts stop inventing their own
         (`_assessment`, `_sweep`, `_oracle`, an ad hoc `_work/<name>`, or a stray `fabric/` from a
         CWD-relative path). See issue #291 (gaps 1 and 3) and issue #234, whose corrected design
         this module implements: `_runs/<NNN>-<slug>/{assessment,assets,bundle,oracle,packages,
         deliverables,scratch}/`.
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

⚠️ The run NUMBER is identity: never renamed, never renumbered, never reused
-----------------------------------------------------------------------------
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
records the directory name it allocated under the `allocated_dir_name` key, and `check_run_location`
(surfaced by `list_runs` and by `python scripts/work_dirs.py --verify`) reports `intact`, `moved` or
`unverifiable`. `unverifiable` is NOT a soft `intact` - see `check_run_location`.

Map: `docs/INDEX.md`. The same rule is stated in `docs/migration-phases.md` (Phase 1) and
`docs/operator-runbook.md` (§6.4 Scratch).
"""

from __future__ import annotations

import argparse
import json
import re
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
#   deliverables/ operator-facing outputs meant for the customer, never for git (issue #322)
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

_RUN_DIR_RE = re.compile(r"^(\d+)(?:-.*)?$")
# Matches the Fabric artifact-name ceiling used elsewhere in this org's conventions (table names
# under 60 chars) - the slug is decoration, not identity, so there is no reason to let it run long.
_MAX_UNIT_KEY_LEN = 60
_MAX_ALLOCATION_ATTEMPTS = 50  # generous; a genuine collision only ever needs one retry

#: Manifest key under which `allocate_run` records the directory NAME (`<NNN>-<slug>`) it allocated.
#: Reserved exactly like `run`/`unit_key`/`created`/`status`: written AFTER `extra_manifest` is
#: merged, so a caller can neither supply nor clobber it.
#:
#: Why the NAME and not an absolute path: the name is what a renumber/rename changes, and it is
#: invariant under every LEGITIMATE relocation - cloning the repo, moving the checkout, or working
#: in a `git worktree` all change the absolute path while the run keeps its identity. A check that
#: fired on a fresh clone would be noise, and a noisy check is one people learn to ignore. See
#: `check_run_location`.
RUN_LOCATION_KEY = "allocated_dir_name"

#: The three states `check_run_location` reports. They are mutually exclusive and a caller must be
#: able to tell them apart: `UNVERIFIABLE` is NOT a soft `INTACT`. Collapsing unassessable input
#: into the clean bucket is this repo's dominant defect class, and it is precisely the defect issue
#: #470 is about - a renumbered run that read back as healthy.
RUN_LOCATION_INTACT = "intact"
RUN_LOCATION_MOVED = "moved"
RUN_LOCATION_UNVERIFIABLE = "unverifiable"
RUN_LOCATION_STATES: tuple[str, ...] = (RUN_LOCATION_INTACT, RUN_LOCATION_MOVED, RUN_LOCATION_UNVERIFIABLE)

#: Values of `RunLocationCheck.derived_name_check` - a strictly SUBORDINATE hint, only ever set on an
#: `UNVERIFIABLE` run, and never able to promote one to `INTACT`. See `check_run_location`.
DERIVED_NAME_MATCHES = "matches"
DERIVED_NAME_MISMATCH = "mismatch"
DERIVED_NAME_UNAVAILABLE = "unavailable"
DERIVED_NAME_NOT_CONSULTED = "not_consulted"


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
        """Operator-facing outputs meant for the customer, never for git (issue #322)."""
        return self.subdir("deliverables")

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
    width = max(3, len(str(run_number)))
    return f"{run_number:0{width}d}-{unit_key}"


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

    `derived_name_check` is deliberately not part of `state`: it is a hint, it is only ever set on
    an `unverifiable` run, and a `matches` hint does NOT mean intact.
    """

    state: str
    actual_dir_name: str
    recorded_dir_name: str | None
    derived_name_check: str
    detail: str

    @property
    def is_intact(self) -> bool:
        """True only for a run whose RECORDED name matches where it actually sits."""
        return self.state == RUN_LOCATION_INTACT

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable form, as attached by `list_runs` and printed by `--verify --json`."""
        return {
            "state": self.state,
            "actual_dir_name": self.actual_dir_name,
            "recorded_dir_name": self.recorded_dir_name,
            "derived_name_check": self.derived_name_check,
            "detail": self.detail,
        }


def check_run_location(manifest: dict[str, Any], run_dir: Path) -> RunLocationCheck:
    """Compare where a run says it was allocated against where it actually sits. Pure: reads a
    manifest dict and a path, touches no disk, mutates nothing, and never raises (issue #470).

    Three states, and the third is the point:

    * `intact` - the recorded `<NNN>-<slug>` name equals the directory's actual name.
    * `moved` - they differ. The run has been renamed or renumbered since allocation, so generated
      bundle output beneath it embeds absolute self-paths that no longer resolve. Both names are
      reported so the damage is locatable.
    * `unverifiable` - the manifest records no usable name. EVERY run allocated before this key
      existed lands here, including the live `_runs/408-*` on the machine that shipped this change.
      It is not a pass and it is not a failure; it means the question cannot be answered from the
      evidence, which is a different thing from answering it "fine".

    Comparison is on the directory NAME only, never on an absolute path: a repository that has been
    cloned, moved or checked out as a `git worktree` is legitimately somewhere else and must not
    report `moved` (see `RUN_LOCATION_KEY`).
    """
    actual = run_dir.name
    recorded = manifest.get(RUN_LOCATION_KEY)

    if isinstance(recorded, str) and recorded:
        if recorded == actual:
            return RunLocationCheck(
                state=RUN_LOCATION_INTACT,
                actual_dir_name=actual,
                recorded_dir_name=recorded,
                derived_name_check=DERIVED_NAME_NOT_CONSULTED,
                detail=f"allocated as {recorded!r} and still there",
            )
        return RunLocationCheck(
            state=RUN_LOCATION_MOVED,
            actual_dir_name=actual,
            recorded_dir_name=recorded,
            derived_name_check=DERIVED_NAME_NOT_CONSULTED,
            detail=(
                f"allocated as {recorded!r} but found as {actual!r} - renamed or renumbered since "
                "allocation; generated bundle output embeds absolute self-paths, so treat every "
                "bundle under this run as broken until re-checked"
            ),
        )

    if recorded is None:
        missing = f"no {RUN_LOCATION_KEY!r} recorded (allocated before this key existed)"
    else:
        missing = f"{RUN_LOCATION_KEY!r} is present but is not a usable directory name ({recorded!r})"

    derived = _derived_dir_name(manifest)
    if derived is None:
        hint, why = DERIVED_NAME_UNAVAILABLE, "and its own run/unit_key cannot reconstruct one either"
    elif derived == actual:
        hint, why = DERIVED_NAME_MATCHES, f"its run/unit_key imply {derived!r}, which matches - but that is INFERENCE"
    else:
        hint, why = (
            DERIVED_NAME_MISMATCH,
            f"its run/unit_key imply {derived!r}, which does NOT match {actual!r} - likely renamed or "
            "renumbered, but that is INFERENCE, not a record",
        )
    return RunLocationCheck(
        state=RUN_LOCATION_UNVERIFIABLE,
        actual_dir_name=actual,
        recorded_dir_name=None,
        derived_name_check=hint,
        detail=f"{missing}; {why}",
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
    never read-the-max-then-write: two processes racing for the same number cannot both believe
    they got it, because the loser's `mkdir` raises `FileExistsError` and it moves on to the next
    candidate. The directory name is `<NNN>-<slug>`, zero-padded to at least 3 digits; the slug is
    decoration only (never parsed back - see `sanitize_unit_key`), the number is the identity.

    ⚠️ **The directory this returns must never be renamed, renumbered or reused.** Generated bundle
    output under `<run>/bundle/` embeds ABSOLUTE self-paths, so renaming a run afterwards breaks
    every bundle beneath it (refresh, `byPath` report-to-model binding, `_build/` replay). A real
    customer reorg renumbered 14 run directories on exactly this misunderstanding - see the module
    docstring, `docs/migration-phases.md` (Phase 1) and `docs/operator-runbook.md` (§6.4), and issue
    #470. Allocate a NEW run instead; numbering is cheap and gaps are expected.

    One run per PIPELINE RUN, not one per workbook: a 48-workbook estate sweep is a single run whose
    per-workbook units live inside its `bundle/pbip/`.

    So that a rename is at least DETECTABLE afterwards, the directory name is also written into the
    manifest under `RUN_LOCATION_KEY`; `check_run_location` compares the two.

    `extra_manifest` is merged in FIRST, so a caller cannot accidentally clobber the reserved
    `run` / `unit_key` / `created` / `status` / `allocated_dir_name` keys this function itself
    writes.
    """
    root = runs_root(repo_root)
    root.mkdir(parents=True, exist_ok=True)
    slug = sanitize_unit_key(unit_key)

    existing = _existing_run_numbers(root)
    candidate = (max(existing) + 1) if existing else 1
    for _ in range(_MAX_ALLOCATION_ATTEMPTS):
        run_dir = root / _run_dir_name(candidate, slug)
        try:
            run_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            candidate += 1
            continue

        run = RunPaths(root=run_dir, run_number=candidate, unit_key=slug)
        if create_subdirs:
            for sub in CANONICAL_SUBDIRS:
                run.subdir(sub).mkdir(parents=True, exist_ok=True)

        manifest: dict[str, Any] = dict(extra_manifest or {})
        manifest.update(
            {
                "run": candidate,
                "unit_key": slug,
                "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "active",
                RUN_LOCATION_KEY: run_dir.name,
            }
        )
        run.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return run

    raise RuntimeError(
        f"could not allocate a run directory for {unit_key!r} under {root} after "
        f"{_MAX_ALLOCATION_ATTEMPTS} attempts - is something else allocating concurrently?"
    )


def list_runs(repo_root: Path | None = None) -> list[dict[str, Any]]:
    """Read back every run's manifest, sorted by run number. Pure inspection - never mutates, and
    never raises on a hand-created or half-written run directory; it just skips one with no
    readable `run.json` rather than crashing a status query over it.

    Two keys are DERIVED and overwrite anything of the same name in the manifest, because both
    describe where the run is *now* and a stale recorded value silently presented as current is the
    failure this whole function is being audited for:

    * `root` - the directory it was actually found in.
    * `location_check` - `check_run_location(...).as_dict()`, i.e. `intact` / `moved` /
      `unverifiable`. Read `location_check["state"]`; never infer health from `root` alone, which is
      derived from wherever the directory sits and so is true by construction (issue #470).
    """
    root = runs_root(repo_root)
    if not root.is_dir():
        return []
    manifests: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        manifest_path = child / "run.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # A `run.json` holding valid JSON that is not an object (a list, a bare string, a number) is
        # not a manifest. Skipping it keeps the "never raises" promise literally true - `.get` and
        # `.setdefault` on a list raise `AttributeError`, which used to escape this function.
        if not isinstance(data, dict):
            continue
        data["root"] = str(child)
        data["location_check"] = check_run_location(data, child).as_dict()
        manifests.append(data)
    manifests.sort(key=lambda m: m.get("run", 0))
    return manifests


def verify_runs(repo_root: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every run plus a count per `RUN_LOCATION_STATES` entry. Pure inspection, like `list_runs`."""
    runs = list_runs(repo_root)
    counts = {state: 0 for state in RUN_LOCATION_STATES}
    for run in runs:
        state = run.get("location_check", {}).get("state", RUN_LOCATION_UNVERIFIABLE)
        counts[state] = counts.get(state, 0) + 1
    return runs, counts


def verify_exit_code(counts: dict[str, int]) -> int:
    """Gate semantics, matching `check_reference_readiness.py`: 0 clean, 1 findings, 3 cannot
    establish - and neither 1 nor 3 is a pass. A `moved` run outranks an `unverifiable` one because
    it is an established finding rather than an unanswered question.
    """
    if counts.get(RUN_LOCATION_MOVED):
        return 1
    if counts.get(RUN_LOCATION_UNVERIFIABLE):
        return 3
    return 0


def _print_verify_text(runs: list[dict[str, Any]], counts: dict[str, int], root: Path) -> None:
    """Human-readable `--verify` report. The STATE is printed in caps and first on every line, so a
    subordinate `derived name check` hint can never be mistaken for the verdict.
    """
    print(f"_runs/ location check: {root}")
    if not runs:
        print("  (no runs with a readable run.json)")
    for run in runs:
        check = run.get("location_check", {})
        print(f"  {check.get('state', '?').upper():<13} {check.get('actual_dir_name', '?')}")
        print(f"                {check.get('detail', '')}")
    summary = ", ".join(f"{counts.get(state, 0)} {state}" for state in RUN_LOCATION_STATES)
    print(f"{len(runs)} run(s): {summary}")
    if counts.get(RUN_LOCATION_UNVERIFIABLE):
        print("  'unverifiable' is NOT 'intact' - those runs predate path recording and cannot be checked.")


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
            "Exit 0 all intact / 1 at least one moved / 3 at least one unverifiable"
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
