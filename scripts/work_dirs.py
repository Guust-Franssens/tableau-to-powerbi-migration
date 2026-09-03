"""
purpose: single source of truth for the canonical PRE-BUNDLE work layout - resolve, allocate and
         expose per-unit, per-run scratch/output paths so scripts stop inventing their own
         (`_assessment`, `_sweep`, `_oracle`, an ad hoc `_work/<name>`, or a stray `fabric/` from a
         CWD-relative path). See issue #291 (gaps 1 and 3) and issue #234, whose corrected design
         this module implements: `_runs/<NNN>-<slug>/{assessment,assets,bundle,oracle,packages,
         deliverables,scratch}/`.
usage:   python scripts/work_dirs.py <unit-name> [--repo-root PATH] [--json]
         from work_dirs import allocate_run, sanitize_unit_key, runs_root, list_runs

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
    the fact breaks refresh.
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

    `extra_manifest` is merged in FIRST, so a caller cannot accidentally clobber the reserved
    `run` / `unit_key` / `created` / `status` keys this function itself writes.
    """
    root = runs_root(repo_root)
    root.mkdir(parents=True, exist_ok=True)
    slug = sanitize_unit_key(unit_key)

    existing = _existing_run_numbers(root)
    candidate = (max(existing) + 1) if existing else 1
    for _ in range(_MAX_ALLOCATION_ATTEMPTS):
        width = max(3, len(str(candidate)))
        run_dir = root / f"{candidate:0{width}d}-{slug}"
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
        data.setdefault("root", str(child))
        manifests.append(data)
    manifests.sort(key=lambda m: m.get("run", 0))
    return manifests


def main() -> int:
    """CLI: allocate one run for `unit` and print its canonical paths."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("unit", help="unit name/key to allocate a run for (e.g. a workbook or site slug)")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="print the allocated paths as JSON")
    args = parser.parse_args()

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
