"""
purpose: ship one finished migration unit - the phase 2 -> phase 3 hop that had no tool (#458).
         Copies a `package_unit.py` package's `fabric/` working copy into the customer deliverable
         at `migrations/{workbooks,datasources}/<slug>/fabric/`, running `check_unit.py` first and
         REFUSING on a non-zero exit, rewriting `definition.pbir`'s `byPath` for the shared
         /published datasource shape, proving that reference resolves ON DISK, proving the shipped
         report and model have real CONTENT, and recording what promoted what.
usage:   python scripts/promote_unit.py --package <_runs/NNN-slug/packages/<batch>/<Unit>>
             --slug <slug> [--datasource-slug <ds-slug>] [--kind workbook|datasource]
             [--bundle <bundle>] [--dry-run] [--force] [--json <file>] [--migrations-root <dir>]

Why each guard exists (all of these are measured failures, not hypotheses)
--------------------------------------------------------------------------
* **Re-running `check_unit.py` here is deliberate duplication.** It costs under a second, and this
  is the hop where a defect stops being a working copy and becomes a deliverable. `--force`
  overrides **the `check_unit.py` gate and NOTHING else**, and the override plus the observed exit
  code are written into the record - an unchecked promotion must never look checked afterwards.
* **NO artifact may carry an absolute host path.** `migrations/**` is not blanket-gitignored, this
  repo is public, and a customer package path embeds their server, project and operator names as
  surely as `C:\\Users\\<username>\\…` embeds a real user. Every path that reaches the record, the
  `--json` envelope or a finding is repo-relative, deliverable-relative or an opaque marker; a
  path-bearing exception is rendered without its `filename`. **The same rule governs every BYTE
  that ships**, not merely model TMDL: `.pbi/` (Desktop's per-machine state) is excluded from the
  shipment outright, and every other shipped file is scanned as text and refused at exit 6 if it
  names a machine - measured, a `.Report/.pbi/localSettings.json` shipped a customer path at exit 0
  under `--force`, and the git-TRACKED `.pbi/unappliedChanges.json`, `.pbip` and `report.json` are
  the same defect in a file that really would reach the public repo. Because the tool cannot
  rewrite a customer's M query, it cannot SANITIZE a model that reads an absolute outside path
  either - so `--force` does not ship one, it refuses (exit 5). Sanitize with
  `scripts/set_data_folder.py` or carry the extract into the package.
* **The unit's kind comes from `package-manifest.json`, never from the filesystem.**
  `package_unit.py:unit_kind` is explicit: every `pbip/<Unit>/` in a real 2.339.0 estate run
  carries BOTH a `.Report` and a `.SemanticModel` - all 62, datasource-only units included - so
  "it has a report, therefore it is a workbook" promotes real published datasources as workbooks.
  A missing or `unclassified` kind is CANNOT_ASSESS, and `--kind` fills that gap explicitly (it
  can never contradict a manifest that does declare one).
* **`byPath` is verified against the filesystem, not a schema.** `powerbi-report-author validate`
  returns `errorCount: 0` for a `.Report` whose `datasetReference.byPath.path` names a
  `.SemanticModel` that exists nowhere: shape, not target
  (`.github/skills/powerbi-report-gotchas/SKILL.md` §3). A wrong one opens as a report with NO
  MODEL. The target must be a real model, so "some directory holding a `definition/`" is not enough.
* **Content, not existence — and not a FILE COUNT either.** On a 46-asset estate a report folder
  that had passed a sign-off held only Desktop-local settings. Every PBIR document is parsed, at
  the SOURCE and again at the DESTINATION; unreadable input is `CANNOT_ASSESS`, never a count.
* **The slug must be a single safe path component.** `execute_plan` replaces its destination, so a
  slug carrying `..` is not a misfiling - it is a delete outside the migration root. ⚠️ And the
  containment check behind it resolves both sides: measured, a `migrations/workbooks/<slug>`
  JUNCTION pointing outside the root passed a lexical check and shipped the whole deliverable, plus
  its record, outside the tree at exit 0.
* **A model may not read data from OUTSIDE the tree it is promoted into (#461).** Measured across
  run 408's 62 packaged units: **32 absolute machine-local references across 26 units (42%)**,
  pointing into the bundle's gitignored, prunable `data/`. NO existing gate sees it. The test is
  *absolute AND outside*, never a match on `_runs` or a drive letter, so an absolute path under the
  deliverable's own `data/` (the `set_data_folder.py` convention) still promotes.
* **The model-per-workbook copy is a CONTENTS copy.** The source folder is named for the WORKBOOK
  while the model inside is named for the DATASOURCE, so copying the folder nests them wrongly.

Promoting FROM the package is settled (#460): the package's `fabric/` is a `shutil.copytree` of
`<bundle>/pbip/<unit>/`, not a link. `--bundle` diffs the originating tree as a drift REPORT -
reported, never fatal, and it never claims which side is authoritative. The package records no
origin, so `--bundle` is the only route and its absence is `not_checked`, never "no drift".

Exit codes
----------
| 0  | PROMOTED, or a clean `--dry-run` plan |
| 1  | REFUSED_BY_GATE: `check_unit.py` exited non-zero and `--force` was not given |
| 2  | CANNOT_ASSESS: the package cannot be read or its shape is ambiguous - never a pass |
| 3  | REFUSED_CONTENT: the source is structurally present but functionally empty |
| 4  | PROMOTION_FAILED: the copy, the `byPath` rewrite, or a post-copy verification failed |
| 5  | REFUSED_EXTERNAL_DATA_PATH: the model reads data from outside the deliverable (#461) |
| 6  | REFUSED_HOST_PATH: some file this would ship names an absolute host path |
| 64 | usage error |

⚠️ 5 is the verdict for that condition wherever it is caught. Both scans - the source one before
anything is copied, and the shipped one after - raise the SAME `ExternalDataPath`, because
automation must not get a different routing answer depending only on which scan saw it first. 6
works the same way, and is a SEPARATE code rather than a second meaning for 5 because the remedies
differ: 5 is fixed with `set_data_folder.py` or by carrying the extract in, 6 by deleting or
sanitizing a file that is often not a model file at all.

⚠️ 2 exists so "cannot assess" can never collapse into the clean bucket - this repo's most common
gate defect class. An unreadable package is a blocking state, not a silent success.
"""

from __future__ import annotations

# pylint: disable=too-many-lines
# Same waiver as the sibling gates it runs beside (`check_unit.py`, `deploy_estate.py`,
# `run_estate.py` and four others): the length here is the WHY, not the logic. Nearly every guard
# in this file records the measured failure that put it there, because a guard whose reason is
# undocumented is the one a later change deletes as redundant. Splitting the module to satisfy the
# 1200-line metric would put those reasons one import away from the code they justify, which is the
# outcome the metric exists to prevent.

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
from work_dirs import REPO_ROOT

EXIT_OK = 0
EXIT_REFUSED_BY_GATE = 1
EXIT_CANNOT_ASSESS = 2
EXIT_REFUSED_CONTENT = 3
EXIT_PROMOTION_FAILED = 4
EXIT_REFUSED_EXTERNAL_PATH = 5
EXIT_REFUSED_HOST_PATH = 6
EXIT_USAGE = 64

# check_unit.py's own documented exits, kept here so the record says what the number MEANT rather
# than only what it was. Anything unlisted is reported verbatim as an unknown code.
CHECK_UNIT_EXITS = {
    0: "AUTOMATED_CHECKS_PASS",
    1: "FINDINGS",
    2: "NOT_FULLY_CHECKED",
    4: "PAGE_PARITY_PRECONDITION_FAILED",
    64: "USAGE_ERROR",
}

CHECK_UNIT_TIMEOUT_SECONDS = 600

# Windows reserved device names: a destination called `NUL` is a device, not a directory.
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{n}" for n in range(1, 10)} | {f"LPT{n}" for n in range(1, 10)}
)
_UNSAFE_SLUG_CHARS = ':*?"<>|'

REPORT_SUFFIX = ".Report"
MODEL_SUFFIX = ".SemanticModel"
RECORD_NAME = "promotion-record.json"
MANIFEST_NAME = "package-manifest.json"
RECORD_VERSION = 1

# `package_unit.py:unit_kind`'s own vocabulary. `unclassified` is a real value it emits and is NOT
# promotable on its own - it means the engine's report.json named this unit in neither list.
KIND_WORKBOOK = "workbook"
KIND_DATASOURCE = "datasource"
DECLARABLE_KINDS = (KIND_WORKBOOK, KIND_DATASOURCE)

# What an artifact says instead of an absolute host path. Never a truncation of the real one.
OUTSIDE_REPO = "<outside-repo>"
UNDER_MIGRATIONS_ROOT = "<migrations-root>"

# A TMDL table declaration is a column-0 `table <name>` line; an indented one is a nested property.
_TMDL_TABLE_RE = re.compile(r"^table\s+\S", re.MULTILINE)

# The `model <name>` declaration every `model.tmdl` opens with. A zero-byte file satisfies
# `is_file()` and shipped before this.
_TMDL_MODEL_RE = re.compile(r"^model\s+\S", re.MULTILINE)

# Every double-quoted literal in a TMDL/M source block. M has no escaped quote inside a literal -
# it doubles them - so a non-greedy run between quotes is the right shape here.
_TMDL_STRING_RE = re.compile(r'"([^"\n]*)"')

# A run of adjacent string literals joined by M's `&` concatenation operator, possibly across
# lines: `"C:" & "\secret\data.csv"`. Judged as one value, because each fragment alone is harmless.
_TMDL_CONCAT_RE = re.compile(r'"[^"\n]*"(?:\s*&\s*"[^"\n]*")+')

# Power BI Desktop's per-machine state inside an artifact folder. NOT copied - see
# `_shipped_files`. `.gitignore:169-172` already calls `.pbi/cache.abf`, `.pbi/localSettings.json`
# and `.pbi/editorSettings.json` "machine-specific, regenerated automatically on open", which is
# the whole argument: they are not part of the PBIP definition, `localSettings.json` records the
# OPERATOR's local model and data paths, and `cache.abf` is a multi-hundred-MB binary no text scan
# can inspect. Excluding them removes the leak at its source instead of detecting it afterwards -
# and it is not enough on its own, because `.gitignore` names only those three FILES: a sibling
# `.pbi/unappliedChanges.json` (also Desktop-written) is git-TRACKED, so it would reach the public
# repo. Measured with `git check-ignore`, 2026-09-04.
DESKTOP_LOCAL_EXCLUSIONS = frozenset({".pbi"})

# Suffixes a text scan cannot inspect. Deliberately the same vocabulary as
# `scripts/set_data_folder.py:_check`, plus the image types PBIR carries in
# `StaticResources/RegisteredResources/`. A file with an UNLISTED suffix that will not decode is
# `CannotAssess` (blocking), never silently skipped - see `shipment_host_paths`.
UNSCANNABLE_BINARY_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pbix", ".abf", ".hyper", ".twbx", ".twb", ".xlsx", ".zip"}
)

# An absolute host path ANYWHERE in a shipped text file - quoted, bare, or JSON-escaped. This is
# the whole-artifact counterpart of `_TMDL_STRING_RE`, which only ever saw quoted literals in model
# TMDL and therefore could not see `.pbi/localSettings.json`, `report.json` or a `.pbip`.
#
# Vocabulary reused from `scripts/set_data_folder.py:ABSOLUTE_USER_PATH_RE` rather than reinvented:
# `C:\x`, `C:/x`, JSON-escaped `C:\\x`, UNC `\\host\x` (and JSON-escaped `\\\\host\\x`), POSIX
# `/Users/<x>` and `/home/<x>`. The POSIX branch is restricted to those two roots on purpose - matching
# every `/…` in a PBIR document produced false positives on JSON pointers and Databricks
# `HttpPath` values, which is the same residual `_is_local_filesystem_path` documents.
#
# ⚠️ Two guards keep a URL scheme out, and BOTH are needed: the lookbehind rejects a multi-letter
# scheme (`https:`), and `(?!/)` rejects the `//` that follows one. The trailing `+` (not `*`)
# means a bare `C:\` in prose is not a match.
_ABSOLUTE_PATH_IN_TEXT_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/](?!/)"
    r"|\\{2,4}[^\\/\s\"'<>|]+[\\/]{1,2}"
    r"|(?<![\w.])/(?:Users|home)/)"
    r"[^\s\"'<>|]+"
)


class UsageErrorParser(argparse.ArgumentParser):
    """`argparse` exits 2 on a bad command line, and 2 is CANNOT_ASSESS here.

    Letting that stand would make "you typed the flag wrong" indistinguishable from "this package
    could not be assessed" - two states with completely different responses. Usage errors exit 64,
    matching `check_unit.py`.
    """

    def error(self, message: str) -> Any:
        """Exit 64 rather than argparse's default 2."""
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


class CannotAssess(Exception):
    """The package cannot be read, or its shape is ambiguous. Blocking, never a pass."""


class PromotionFailed(Exception):
    """A copy or a post-copy verification failed after work had started."""


class ExternalDataPath(Exception):
    """The model reads data from outside the deliverable (#461).

    Its own class, not a `PromotionFailed`, because the two scans that can raise it sit either side
    of the copy and used to route to DIFFERENT exit codes for one condition - 5 from the source
    scan, 4 from the shipped one. Automation reads the code, so the condition owns the class.
    """


class HostPathLeak(Exception):
    """A file this promotion would SHIP carries an absolute host path.

    Its own class and its own exit code (6), not a second meaning for `ExternalDataPath` (5),
    because the two conditions have different remedies and automation routes on the code. 5 says
    *the model READS from a location the deliverable does not contain* - remedied by
    `scripts/set_data_folder.py` or by carrying the extract into the package. 6 says *a shipped
    byte NAMES a machine* - remedied by deleting or sanitizing that file, which may not be a model
    file at all.
    """


@dataclass(frozen=True)
class PackageShape:
    """What one package's `fabric/` actually holds, and what the MANIFEST says it is."""

    fabric: Path
    report: Path | None
    model: Path | None
    loose_files: tuple[Path, ...]
    kind: str
    kind_source: str


@dataclass
class ContentCheck:
    """The content verdict for one artifact - counts, refusals, and unassessable input.

    `findings` and `unassessable` are separate on purpose: *"I read this and it is empty"* and
    *"I could not read this"* need different exit codes, and collapsing the second into the first
    (or worse, into a pass) is this repo's most common gate defect.
    """

    counts: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    unassessable: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the artifact carries real content and could be read at all."""
        return not self.findings and not self.unassessable


@dataclass(frozen=True)
class CopyStep:
    """One source -> destination artifact copy in the plan."""

    source: Path
    destination: Path
    what: str


@dataclass
class PromotionPlan:
    """Everything promotion will do, decided before anything is written."""

    shape: PackageShape
    steps: list[CopyStep]
    report_destination: Path | None
    model_destination: Path | None
    bypath: str | None
    migrations_root: Path
    stale_removals: list[Path] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        """How many files the plan copies."""
        return sum(_count_files(step.source) for step in self.steps)

    @property
    def deliverable_root(self) -> Path:
        """The `<slug>/` directory the promoted MODEL lands under.

        This, not `migrations/`, is the containment boundary for a model's data references: a
        deliverable may carry its own `data/`, but not read out of another unit's.
        """
        anchor = self.model_destination or self.report_destination
        if anchor is None:  # unreachable: a plan always promotes at least one artifact
            raise CannotAssess("plan has no destination")
        return anchor.parent.parent

    @property
    def deliverable_roots(self) -> tuple[Path, ...]:
        """EVERY `<slug>/` this promotion writes into - one, or two for a shared datasource.

        `deliverable_root` deliberately answers a narrower question (where the MODEL lands, which
        is the boundary #461 judges its data references against). The host-path scan covers the
        report as well, and for a shared datasource the two halves stop being siblings, so a report
        that legitimately names its own deliverable would be judged against the datasource's root.
        """
        roots: list[Path] = []
        for destination in (self.report_destination, self.model_destination):
            if destination is not None and destination.parent.parent not in roots:
                roots.append(destination.parent.parent)
        return tuple(roots)


def _shipped_files(path: Path) -> list[Path]:
    """Every file at or under `path` that a promotion actually COPIES.

    ⚠️ The single definition of "what ships", used by the file count, by `execute_plan`'s copy and
    by the host-path scan. Three answers to that question is how a scan and a copy disagree, which
    is the shape of finding 1: the count and the copy covered the whole tree while the scan covered
    model TMDL only.
    """
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and not any(part in DESKTOP_LOCAL_EXCLUSIONS for part in item.relative_to(path).parts)
    )


def _count_files(path: Path) -> int:
    """Number of files at or under `path` that will be copied (1 for a file)."""
    return len(_shipped_files(path))


def _resolved_for_containment(path: Path) -> Path:
    """`path` with every symlink and junction resolved, ready to be judged against a root.

    ⚠️ **A lexical containment check is not a containment check.** Measured: with
    `migrations/workbooks/<slug>` made a junction to a directory outside `migrations/`, every
    planned destination was *lexically* inside the root, and the promotion wrote the report, the
    model and the promotion record OUTSIDE it at exit **0**.

    `resolve()` is non-strict on purpose. The destination does not exist yet - a strict resolve
    would raise on every first promotion - and a non-strict one is exactly right here because
    Windows' `realpath` resolves the longest EXISTING prefix, which is where a junction has to
    live, and appends the rest lexically. Both sides of the comparison go through this function, so
    a migrations root that is itself reached through a junction still contains its own children.
    """
    return Path(os.path.normpath(path)).resolve()


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object, raising `CannotAssess` rather than returning a half-truth."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CannotAssess(f"{path.name} could not be read: {_safe_error(exc)}") from exc
    if not isinstance(payload, dict):
        raise CannotAssess(f"{path.name} is not a JSON object")
    return payload


def _safe_error(exc: BaseException) -> str:
    """Render an exception WITHOUT the absolute host path `OSError` carries in `filename`.

    ⚠️ Measured: `PermissionError: [Errno 13] Permission denied: 'C:\\Users\\<username>\\…'` is the
    default `str()` of a failed read, and that string went straight into a `--json` envelope and a
    committable finding. `strerror` plus `errno` says everything the operator needs and names no
    path; the artifact already records WHICH file, relative to the package.
    """
    if isinstance(exc, OSError):
        detail = exc.strerror or type(exc).__name__
        return f"{type(exc).__name__}: {detail}" + (f" (errno {exc.errno})" if exc.errno is not None else "")
    # ⚠️ The fallback is REDACTED rather than trusted. Fixing only the one site that interpolated a
    # raw `OSError` would move the boundary instead of removing it: `ValueError` from
    # `Path.relative_to` renders BOTH absolute paths, `subprocess.TimeoutExpired` renders the whole
    # command line, and the broad `except Exception` in `_run_promotion` renders whatever arrives.
    # Redacting here means a NEW path-bearing exception type cannot reintroduce the defect.
    return redact_host_paths(f"{type(exc).__name__}: {exc}")


def _repo_relative(path: Path, repo_root: Path) -> tuple[str, bool]:
    """Return (displayable path, whether it is repo-relative).

    A record can be committed - `migrations/**/fabric/**` is NOT blanket-gitignored - so a path
    inside the repo is recorded relative to it. ⚠️ A path OUTSIDE the repo used to be recorded
    VERBATIM with a flag, which put `C:\\Users\\<username>\\…` into both the record and the `--json`
    envelope on every promotion whose package or migrations root sat elsewhere. The flag is not a
    redaction: nothing downstream acted on it. Only the leaf survives now, and the leaf is the unit
    name the record already carries in `unit`.
    """
    try:
        return path.resolve().relative_to(repo_root).as_posix(), True
    except ValueError:
        return f"{OUTSIDE_REPO}/{path.name}" if path.name else OUTSIDE_REPO, False


def _destination_display(path: Path, plan: PromotionPlan, repo_root: Path) -> str:
    """A promoted path, said WITHOUT an absolute host path.

    Repo-relative when the deliverable really is in this repo (the normal case, and the most
    useful); otherwise relative to the migrations root, which containment already guarantees every
    destination lies under. Both are actionable; neither names a machine.
    """
    display, is_repo_relative = _repo_relative(path, repo_root)
    if is_repo_relative:
        return display
    try:
        return f"{UNDER_MIGRATIONS_ROOT}/{path.resolve().relative_to(plan.migrations_root.resolve()).as_posix()}"
    except ValueError:
        return display


def _read_manifest(manifest: Path) -> dict[str, Any]:
    """`package-manifest.json` as an object, or a BLOCKING refusal that names the remedy.

    ⚠️ `--kind` deliberately does NOT rescue a manifest that will not parse, and the message says
    so. It fills a gap the engine left (`kind` absent, or `unclassified`); a manifest that is
    unreadable or is not an object is evidence the PACKAGE may be damaged, and declaring a kind
    over it would be a guess about which tree a customer deliverable lands in. A blocking verdict
    still has to be actionable, though - "package-manifest.json is not a JSON object" with no
    remedy is where an operator stops.
    """
    try:
        return _read_json(manifest)
    except CannotAssess as exc:
        raise CannotAssess(
            f"{exc}, so the unit's kind is unknown - the filesystem cannot answer it (a datasource unit "
            f"also ships a .Report). Re-run package_unit.py to regenerate {MANIFEST_NAME}; --kind does not "
            f"rescue a manifest that will not parse, because that is evidence about the whole package"
        ) from exc


def declared_kind(package: Path, override: str | None) -> tuple[str, str]:
    """The unit's kind and where it came from - the MANIFEST, never the filesystem.

    ⚠️ `package_unit.py:unit_kind` is explicit that the filesystem cannot answer this: every
    `pbip/<Unit>/` folder in a real 2.339.0 estate run carries BOTH a `.Report` and a
    `.SemanticModel` - measured on all 62, datasource-only units included. Inferring "has a report,
    therefore workbook" promoted real published datasources into `migrations/workbooks/`.

    `--kind` fills a gap; it can never contradict a manifest that DOES declare one, because an
    operator overruling the engine's own classification is a defect report, not a flag.
    """
    manifest = package / MANIFEST_NAME
    if not manifest.is_file():
        if override is not None:
            return override, f"--kind (no {MANIFEST_NAME})"
        raise CannotAssess(
            f"package has no {MANIFEST_NAME}, so its kind is unknown - the filesystem cannot answer it "
            f"(a datasource unit also ships a .Report). Re-run package_unit.py, or pass --kind."
        )
    payload = _read_manifest(manifest)
    value = payload.get("kind")
    if isinstance(value, str) and value.strip() in DECLARABLE_KINDS:
        if override is not None and override != value.strip():
            raise CannotAssess(
                f"--kind {override!r} contradicts {MANIFEST_NAME}, which declares {value.strip()!r}; "
                f"--kind fills a gap, it does not overrule the engine's own classification"
            )
        return value.strip(), MANIFEST_NAME
    if override is not None:
        return override, f"--kind ({MANIFEST_NAME} declares {value!r})"
    raise CannotAssess(
        f"{MANIFEST_NAME} declares kind {value!r}, which is not one of {DECLARABLE_KINDS} - the engine "
        f"classified this unit as neither. Pass --kind to declare it explicitly; it is recorded."
    )


def discover_shape(package: Path, kind: str, kind_source: str) -> PackageShape:
    """Read `<package>/fabric/` and prove it matches the kind the manifest declared.

    Raises `CannotAssess` for anything ambiguous - two reports, two models, or an artifact the
    declared kind requires and the package does not have - because a guess here promotes the wrong
    tree into a customer deliverable.
    """
    if not package.is_dir():
        raise CannotAssess("package is not a directory")
    fabric = package / "fabric"
    if not fabric.is_dir():
        raise CannotAssess("package has no fabric/ working copy")
    reports = sorted(p for p in fabric.iterdir() if p.is_dir() and p.name.endswith(REPORT_SUFFIX))
    models = sorted(p for p in fabric.iterdir() if p.is_dir() and p.name.endswith(MODEL_SUFFIX))
    strays = sorted(
        p.name for p in fabric.iterdir() if p.is_dir() and not p.name.endswith((REPORT_SUFFIX, MODEL_SUFFIX))
    )
    if strays:
        raise CannotAssess(
            f"fabric/ holds unrecognised director{'ies' if len(strays) > 1 else 'y'}: {', '.join(strays)}"
        )
    if len(reports) > 1:
        raise CannotAssess(f"fabric/ holds {len(reports)} .Report folders; a unit is exactly one report")
    if len(models) > 1:
        raise CannotAssess(f"fabric/ holds {len(models)} .SemanticModel folders; a unit is exactly one model")
    if not reports and not models:
        raise CannotAssess("fabric/ holds neither a .Report nor a .SemanticModel - nothing to promote")
    if models and (reports or kind == KIND_DATASOURCE):
        loose = tuple(sorted(p for p in fabric.iterdir() if p.is_file()))
        return PackageShape(
            fabric=fabric,
            report=reports[0] if reports else None,
            model=models[0],
            loose_files=loose,
            kind=kind,
            kind_source=kind_source,
        )
    if models:
        raise CannotAssess(f"package declares kind {kind!r} but fabric/ holds no {REPORT_SUFFIX} to promote")
    # ⚠️ No model. A workbook's `byPath` had nothing of its own to point at, so it was verified
    # against whatever happened to be in the DESTINATION - and a model left there by an earlier run
    # turned "this package is missing its model" into exit 0 with `bypath_verified: true`.
    raise CannotAssess(
        f"package declares kind {kind!r} but fabric/ holds no .SemanticModel; a promotion must ship the "
        f"model its byPath resolves to, never bind to whatever a previous run left in the destination"
    )


def _load_json_document(path: Path, label: str, check: ContentCheck) -> dict[str, Any] | None:
    """Parse one PBIR document, recording an UNASSESSABLE reason instead of guessing.

    ⚠️ A file that exists is not a document. Before this parsed anything, a zero-byte `visual.json`
    counted as a visual and the record then *asserted* `"visuals": 4`.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        check.unassessable.append(f"{label} is not readable JSON: {_safe_error(exc)}")
        return None
    if not isinstance(payload, dict):
        check.unassessable.append(f"{label} is valid JSON but not an object")
        return None
    return payload


def _page_is_well_formed(page_dir: Path, report_name: str, check: ContentCheck) -> bool:
    """A page must be a parseable document that names itself."""
    document = _load_json_document(page_dir / "page.json", f"{report_name}: {page_dir.name}/page.json", check)
    if document is None:
        return False
    if not isinstance(document.get("name"), str) or not document["name"].strip():
        check.unassessable.append(f"{report_name}: {page_dir.name}/page.json declares no page name")
        return False
    return True


def _visual_is_well_formed(visual_json: Path, report_name: str, check: ContentCheck) -> bool:
    """A visual must be a parseable document that actually DECLARES a visual.

    Measured across the 677 visuals of the reference estate: every one carries a `visual` object
    with a `visualType` string. `visualGroup` is the schema's other legal shape and is accepted.
    """
    label = f"{report_name}: {visual_json.parent.name}/visual.json"
    document = _load_json_document(visual_json, label, check)
    if document is None:
        return False
    if isinstance(document.get("visualGroup"), dict):
        return True
    visual = document.get("visual")
    if (
        not isinstance(visual, dict)
        or not isinstance(visual.get("visualType"), str)
        or not visual["visualType"].strip()
    ):
        check.unassessable.append(f"{label} declares no visual (no visual.visualType and no visualGroup)")
        return False
    return True


def slug_problem(value: str) -> str | None:
    """Why `value` is not a safe single path component, or None if it is one.

    ⚠️ **The highest-severity guard in the file.** `execute_plan` REPLACES its destination, so a
    slug carrying `..` or a drive letter is not a misfiling - it can destroy a directory outside
    the migration root. Measured pre-guard: `--slug ..\\..\\escaped` exited **0**, wrote the
    deliverable outside the root, and reported success. A TRAILING DOT is the same defect one
    layer down: Windows normalises `foo.` to `foo`, so `--slug foo.` exited 0 and silently
    destroyed the deliverable at `foo`. Judged purely lexically; `Path.resolve()` is never called
    on this untrusted value, and `_refuse_aliased_root` is the on-disk backstop for the aliasing
    (case-insensitivity) a lexical test cannot see.
    """
    windows = PureWindowsPath(value)
    for failed, message in (
        (not value or not value.strip(), "must not be empty or whitespace"),
        (value != value.strip(), "must not have leading or trailing whitespace"),
        ("/" in value or "\\" in value, "must be a single path component (no '/' or '\\')"),
        (set(value) == {"."}, "must not be a relative path component ('.' or '..')"),
        (
            bool(windows.drive) or windows.is_absolute() or value.startswith("/"),
            "must not be absolute or carry a drive letter",
        ),
        (
            any(char in value for char in _UNSAFE_SLUG_CHARS) or any(ord(char) < 32 for char in value),
            "must not contain a path-reserved or control character",
        ),
        (
            value.endswith("."),
            "must not end in a dot (Windows normalises 'foo.' to 'foo', so it would REPLACE the deliverable at 'foo')",
        ),
        (value.split(".")[0].upper() in _RESERVED_DEVICE_NAMES, "must not be a reserved Windows device name"),
    ):
        if failed:
            return message
    return None


def _assert_contained(paths: list[Path], root: Path, what: str) -> None:
    """Refuse any planned path that does not lie under the migrations root.

    Defence in depth behind `slug_problem`: a containment check that never fires is exactly the
    one you want here, because the one that DOES fire is the one nobody wrote.

    ⚠️ Judged on RESOLVED paths, both sides. A lexical comparison passed a `migrations/workbooks/
    <slug>` junction pointing outside the root and the promotion shipped there at exit 0 - the
    check ran, agreed, and was measuring the wrong thing. See `_resolved_for_containment`.
    """
    resolved_root = _resolved_for_containment(root)
    for path in paths:
        normalized = _resolved_for_containment(path)
        if not normalized.is_relative_to(resolved_root):
            raise CannotAssess(
                f"{what} would land outside the migrations root: {normalized.name} "
                f"(the destination resolves, through a symlink or junction, outside the tree it was asked for)"
            )


def _read_tmdl(path: Path, label: str, check: ContentCheck) -> str | None:
    """Read one TMDL file STRICTLY, recording an unassessable reason instead of guessing.

    ⚠️ Two defects in one line before this. `errors="replace"` turned undecodable bytes into `\\ufffd`
    and read on, so a mis-encoded model was silently *assessable* and could pass; and an `OSError`
    propagated out of `main()` as a traceback - no exit contract at all, and its message carries the
    absolute path it failed on.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        check.unassessable.append(f"{label} could not be read: {_safe_error(exc)}")
        return None


def _check_pbir(report: Path, check: ContentCheck) -> None:
    """`definition.pbir` must name the model this report binds to, at the SOURCE as well.

    ⚠️ Unusable input is CANNOT_ASSESS (2), not a promotion that got as far as failing (4): the two
    route to different responses, and this used to surface only as a `PromotionFailed` from the
    rewrite - after the copy - because nothing looked at it beforehand.
    """
    label = f"{report.name}: definition.pbir"
    pbir = report / "definition.pbir"
    if not pbir.is_file():
        check.unassessable.append(f"{label} is missing - the model this report binds to is unreadable")
        return
    document = _load_json_document(pbir, label, check)
    if document is None:
        return
    reference = document.get("datasetReference")
    by_path = reference.get("byPath") if isinstance(reference, dict) else None
    if not isinstance(by_path, dict) or not isinstance(by_path.get("path"), str) or not by_path["path"].strip():
        check.unassessable.append(f"{label} declares no datasetReference.byPath.path string")


def _check_page_order(manifest: Path, pages: list[Path], report_name: str, check: ContentCheck) -> None:
    """`pageOrder` must name pages that are actually THERE.

    A non-empty list said nothing about whether its ids exist. Power BI opens the pages the manifest
    orders, so a manifest that orders only absent ids opens a report with none of the real ones -
    structurally present, functionally empty, and it passed. Matched against the page DIRECTORY name
    or the `name` the page declares, because engine output uses the first and the schema the second.

    ⚠️ **An ordered page that is not there is a FINDING (3), not `unassessable` (2)**, and getting
    that backwards is this file's own named defect class pointing the other way. `ContentCheck`
    draws the line at *"I read this and it is empty"* versus *"I could not read this"*: every input
    here was read successfully and the verdict is definitive - `pages.json` parsed, `pages/` was
    enumerated, and the ordered ids are provably absent. Nothing is ambiguous, so exit 3's
    *"structurally present but functionally empty"* is exactly the sentence. Classifying it 2
    DOWNGRADED an already-specific verdict: with every page removed, `... enumerates no page` (3)
    was swallowed by this one and automation stopped being told the report was empty at all. The
    genuinely unreadable shapes above - a missing, unparseable, or `pageOrder`-less manifest - stay
    `unassessable`, and a page whose own `page.json` will not parse is still recorded as
    unassessable by `_page_is_well_formed`, which outranks any finding in `assess`.
    """
    document = _load_json_document(manifest, f"{report_name}: pages.json", check)
    if document is None:
        return
    order = document.get("pageOrder")
    if not (isinstance(order, list) and order):
        check.unassessable.append(f"{report_name}: pages.json declares no non-empty pageOrder list")
        return
    present: set[str] = set()
    for page_dir in pages:
        present.add(page_dir.name)
        declared = _load_json_document(page_dir / "page.json", f"{report_name}: {page_dir.name}/page.json", check)
        if isinstance(declared, dict) and isinstance(declared.get("name"), str):
            present.add(declared["name"])
    missing = [str(name) for name in order if str(name) not in present]
    if missing:
        check.findings.append(
            f"{report_name}: pages.json orders {len(missing)} page(s) that are not in definition/pages/ "
            f"({', '.join(missing[:5])}) - Power BI opens the ordered pages, so these open as nothing"
        )


def check_report_content(report: Path) -> ContentCheck:
    """Assert `definition/pages/` enumerates real pages carrying real visuals.

    A folder count is not a content check - the precedent case had every expected folder present
    and nothing behind them. ⚠️ Neither is a FILE count: every document here is parsed, and
    `pages.json` (the manifest Power BI reads for page order) must be present, well-formed, and
    ORDER PAGES THAT EXIST, because a report whose pages are not enumerated opens with none of them.
    """
    check = ContentCheck()
    _check_pbir(report, check)
    pages_dir = report / "definition" / "pages"
    if not pages_dir.is_dir():
        check.findings.append(f"{report.name}: no definition/pages/ directory")
        return check

    candidates = sorted(p for p in pages_dir.iterdir() if p.is_dir() and (p / "page.json").is_file())
    pages = [p for p in candidates if _page_is_well_formed(p, report.name, check)]

    manifest = pages_dir / "pages.json"
    if not manifest.is_file():
        check.unassessable.append(f"{report.name}: definition/pages/pages.json is missing - page order is unreadable")
    else:
        _check_page_order(manifest, pages, report.name, check)

    visuals = [
        v
        for p in pages
        for v in sorted(p.glob("visuals/*/visual.json"))
        if _visual_is_well_formed(v, report.name, check)
    ]
    with_visuals = {v.parent.parent.parent for v in visuals}
    check.counts = {
        "pages": len(pages),
        "pages_with_visuals": len(with_visuals),
        "visuals": len(visuals),
    }
    if not pages:
        check.findings.append(f"{report.name}: definition/pages/ enumerates no page (no page.json anywhere)")
    elif not visuals:
        check.findings.append(
            f"{report.name}: {len(pages)} page(s) but ZERO visuals - structurally present, functionally empty"
        )
    return check


def check_model_content(model: Path) -> ContentCheck:
    """Assert `definition/tables/` holds real TMDL tables and `model.tmdl` declares a model.

    ⚠️ `model.tmdl` is checked for a `model <name>` DECLARATION, not for existence: a zero-byte
    file satisfies `is_file()`, and one shipped.
    """
    check = ContentCheck()
    definition = model / "definition"
    tables_dir = definition / "tables"
    model_file = definition / "model.tmdl"
    if not model_file.is_file():
        check.findings.append(f"{model.name}: no definition/model.tmdl")
    else:
        text = _read_tmdl(model_file, f"{model.name}: definition/model.tmdl", check)
        if text is not None and not _TMDL_MODEL_RE.search(text):
            check.findings.append(f"{model.name}: definition/model.tmdl declares no model - {len(text)} byte(s)")
    if not tables_dir.is_dir():
        check.findings.append(f"{model.name}: no definition/tables/ directory")
        return check
    tmdl = sorted(tables_dir.glob("*.tmdl"))
    texts = {p: _read_tmdl(p, f"{model.name}: definition/tables/{p.name}", check) for p in tmdl}
    real = [p for p, text in texts.items() if text is not None and _TMDL_TABLE_RE.search(text)]
    check.counts = {"table_files": len(tmdl), "tables": len(real)}
    if not real and not check.unassessable:
        check.findings.append(f"{model.name}: definition/tables/ holds {len(tmdl)} file(s) but ZERO table declarations")
    return check


def _quoted_strings(text: str) -> list[str]:
    """Every candidate path VALUE in a TMDL/M source block: each double-quoted literal on its own,
    plus the **joined** value of every run of adjacent literals concatenated with `&`.

    ⚠️ `File.Contents("C:" & "\\secret\\data.csv")` classifies as two harmless fragments when each
    literal is judged alone - `"C:"` is a drive with no root, `"\\secret\\data.csv"` a root with no
    drive - and the whole reference then ships. Joining the run closes that without an M parser.

    ⚠️ **Identifier concatenation (`SourceFolder & "\\x.csv"`) is deliberately NOT evaluated, and
    need not be**: an M parameter is itself `expression <Name> = "<value>"` in `expressions.tmdl`,
    which this scan reads like any other file, so an absolute parameter is caught at its DEFINITION
    site whatever the partition does with it - measured, that is how 9 of the 32 estate findings
    surfaced. The residual is a path built from a runtime value, which is not a fixed machine path.
    """
    values = _TMDL_STRING_RE.findall(text)
    for run in _TMDL_CONCAT_RE.findall(text):
        values.append("".join(_TMDL_STRING_RE.findall(run)))
    return values


def _is_local_filesystem_path(candidate: str) -> bool:
    """Whether a literal names a LOCAL filesystem location rather than a server, URL or formula.

    Not keyed on a drive letter: a UNC share and a macOS path are the same defect on a different
    machine - measured, one estate model carries `/Users/<someone>/…/Global Superstore.xlsx`.

    ⚠️ **A POSIX-absolute literal is ambiguous where a drive-absolute one is not.** Taken at face
    value it produced real false positives - a Databricks `HttpPath = "/sql/1.0/warehouses/<id>"`
    and a bare `"/"` in a `TableauFormula` annotation, 8 of 9 POSIX-only hits - so a POSIX
    candidate must also carry a file suffix. ❌ Residual: a POSIX *folder* parameter is missed. No
    URL guard on purpose - a URL is absolute in neither flavour, so one killed no mutation.
    """
    if PureWindowsPath(candidate).is_absolute():  # `C:\…`, `C:/…`, `\\server\share\…`
        return True
    if not candidate.startswith("/") or candidate.startswith("//"):
        return False
    return bool(PurePosixPath(candidate).suffix)


def _redact_path(candidate: str) -> str:
    """Keep only the leaf, because an absolute path embeds a real USERNAME.

    ⚠️ `migrations/**` is not blanket-gitignored and this repo is public, so no artifact may carry
    the full string (`scripts/set_data_folder.py --check` gates exactly that). The operator still
    gets the whole path on stderr, where it is a terminal line rather than an artifact.
    """
    leaf = PureWindowsPath(candidate.replace("/", "\\")).name
    return f"<absolute-path-redacted>\\{leaf}" if leaf else "<absolute-path-redacted>"


def external_data_paths(model: Path, allowed_roots: tuple[Path, ...]) -> list[dict[str, str]]:
    """Absolute data references in a model's TMDL that lie OUTSIDE every allowed root.

    Issue #461, measured across run 408's 62 packaged units: **32 absolute machine-local references
    across 26 units (42%)**, pointing into the originating bundle's `data/` - gitignored,
    machine-local and prunable. ⚠️ **No existing gate sees this** - `check_unit.py` returns its
    normal verdicts and `powerbi-report-author validate` is clean - so this is new coverage, not a
    second opinion. Judged as *absolute AND outside*, never by matching `_runs` or a drive letter,
    so the `set_data_folder.py` convention's absolute `<slug>\\data\\` keeps promoting.
    """
    definition = model / "definition"
    if not definition.is_dir():
        return []
    roots = [root.resolve() for root in allowed_roots]
    found: list[dict[str, str]] = []
    for tmdl in sorted(definition.rglob("*.tmdl")):
        try:
            text = tmdl.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CannotAssess(
                f"{tmdl.relative_to(model).as_posix()} could not be read, so its data references "
                f"cannot be scanned: {_safe_error(exc)}"
            ) from exc
        for candidate in _quoted_strings(text):
            if not _is_local_filesystem_path(candidate) or _inside_any(candidate, roots):
                continue
            found.append(
                {
                    "file": tmdl.relative_to(model).as_posix(),
                    "redacted": _redact_path(candidate),
                    "full": candidate,
                }
            )
    return found


def _inside_any(candidate: str, roots: list[Path]) -> bool:
    """Whether an absolute literal lies under one of the allowed roots.

    ⚠️ **Purely lexical - this must never touch the filesystem.** `Path.resolve()` on a UNC literal
    naming a host that does not exist blocks on SMB name resolution: measured here, that turned a
    millisecond check into a multi-minute stall. A path that does not exist is still outside, the
    fail-closed answer a pruned bundle `data/` needs.
    """
    normalized = Path(os.path.normpath(candidate))
    return any(normalized.is_relative_to(root) for root in roots)


def redact_host_paths(text: str) -> str:
    """Every absolute host path in `text`, replaced by its redaction. Never a truncation."""
    return _ABSOLUTE_PATH_IN_TEXT_RE.sub(lambda match: _redact_path(match.group(0)), text)


def shipment_host_paths(roots: tuple[Path, ...], allowed_roots: tuple[Path, ...]) -> list[dict[str, str]]:
    """Absolute host paths in ANY file this promotion would ship, outside every allowed root.

    ⚠️ **The invariant is "no shipped file carries an absolute host path", so the scan is keyed on
    the SHIPMENT, not on a file extension.** `external_data_paths` reads a model's
    `definition/**/*.tmdl` and nothing else, which is the right shape for the question it asks
    (*where does this model READ from*) and the wrong shape for this one. Measured 2026-09-04:
    a `.Report/.pbi/localSettings.json` carrying `C:\\Users\\<operator>\\ServerA\\source.csv`
    promoted at exit **0** under `--force`, and `git check-ignore` confirms the sibling
    `.pbi/unappliedChanges.json`, the `.pbip` and `definition/report.json` are all git-TRACKED, so
    the same leak reaches a PUBLIC repository.

    Two halves, and each closes something the other cannot:

    * **`.pbi/` is not shipped at all** (`DESKTOP_LOCAL_EXCLUSIONS`). It is Desktop's per-machine
      state, `.gitignore:169-172` already says so, and `cache.abf` is a binary no text scan could
      read - removing it beats detecting it.
    * **every other shipped file is scanned as text**, with a suffix allowlist for the genuinely
      binary ones and `CannotAssess` for anything unlisted that will not decode. Silently skipping
      an undecodable file would be a fail-open in the one place this repo can least afford one.

    Judged *absolute AND outside* - the same rule as #461, deliberately - so a localized absolute
    path under the deliverable's own `data/` (the `set_data_folder.py` convention) still promotes.
    ❌ Residual: that localized path is still an `ABSOLUTE_USER_PATH_RE` hit at COMMIT time; the
    existing `scripts/set_data_folder.py --check` gate owns that, and `--sanitize` is the remedy.
    """
    allowed = [root.resolve() for root in allowed_roots]
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        for item in _shipped_files(root):
            label = root.name if item == root else f"{root.name}/{item.relative_to(root).as_posix()}"
            if item.suffix.lower() in UNSCANNABLE_BINARY_SUFFIXES:
                continue
            try:
                text = item.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise CannotAssess(
                    f"{label} could not be read as text, so it cannot be scanned for host paths "
                    f"({_safe_error(exc)}); a file that ships unscanned is not a file that shipped clean"
                ) from exc
            for candidate in _ABSOLUTE_PATH_IN_TEXT_RE.findall(text):
                if _inside_any(candidate, allowed) or (label, candidate) in seen:
                    continue
                seen.add((label, candidate))
                found.append({"file": label, "redacted": _redact_path(candidate), "full": candidate})
    return found


def content_checks(shape_report: Path | None, shape_model: Path | None, where: str) -> ContentCheck:
    """Run both content checks over one location and merge them into a single verdict."""
    merged = ContentCheck()
    for artifact, checker in ((shape_report, check_report_content), (shape_model, check_model_content)):
        if artifact is None:
            continue
        result = checker(artifact)
        merged.counts[artifact.name] = result.counts
        merged.findings.extend(f"{where}: {finding}" for finding in result.findings)
        merged.unassessable.extend(f"{where}: {reason}" for reason in result.unassessable)
    return merged


def run_check_unit(package: Path, repo_root: Path) -> dict[str, Any]:
    """Run `check_unit.py` on the package and report its exit code and what that code means.

    A timeout is a REFUSAL, not a pass: it is recorded with `exit_code: null` and a status the
    caller treats exactly like a finding.
    """
    command = [sys.executable, str(Path(__file__).resolve().parent / "check_unit.py"), str(package), "--quiet"]
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            check=False,
            capture_output=True,
            timeout=CHECK_UNIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "ran": True,
            "exit_code": None,
            "status": "TIMEOUT",
            "timeout_seconds": CHECK_UNIT_TIMEOUT_SECONDS,
            "passed": False,
        }
    code = completed.returncode
    return {
        "ran": True,
        "exit_code": code,
        "status": CHECK_UNIT_EXITS.get(code, f"UNKNOWN_EXIT_{code}"),
        "passed": code == 0,
    }


def build_plan(
    shape: PackageShape,
    migrations_root: Path,
    slug: str,
    datasource_slug: str | None,
) -> PromotionPlan:
    """Decide every copy, the `byPath` value, and which stale artifact a re-run must clear.

    Every destination is checked for containment before the plan is returned, so a hostile or
    fat-fingered slug is refused as `CANNOT_ASSESS` rather than escaping the migrations root, and
    every deliverable root it would REPLACE is checked against what is actually on disk there.
    """
    plan = _build_plan_unchecked(shape, migrations_root, slug, datasource_slug)
    _assert_contained([step.destination for step in plan.steps], migrations_root, "a copy")
    _assert_contained(plan.stale_removals, migrations_root, "a stale removal")
    for destination in (plan.report_destination, plan.model_destination):
        if destination is not None:
            _refuse_aliased_root(destination.parent.parent)
    return plan


def _refuse_aliased_root(root: Path) -> None:
    """Refuse a deliverable root that the filesystem resolves to a DIFFERENTLY-NAMED existing one.

    The lexical slug guard cannot see this: `--slug Foo` is a perfectly legal component, and on a
    case-insensitive filesystem it addresses the existing `foo` deliverable, which promotion would
    then replace wholesale. Purely a comparison of what was asked for against what is on disk, so
    on a case-sensitive filesystem (where they really are two deliverables) it never fires.
    """
    if not root.exists() or not root.parent.is_dir():
        return
    with suppress(OSError):
        if root.name in {entry.name for entry in root.parent.iterdir()}:
            return
    raise CannotAssess(
        f"deliverable {root.name!r} already exists on disk under a different spelling, so promoting it "
        f"would REPLACE another unit's deliverable; promote under the name it already has"
    )


def _build_plan_unchecked(
    shape: PackageShape,
    migrations_root: Path,
    slug: str,
    datasource_slug: str | None,
) -> PromotionPlan:
    """The shape-specific half of `build_plan`, before containment is enforced.

    ⚠️ Keyed on the manifest's declared `kind`, never on "does `fabric/` hold a `.Report`". A
    datasource unit's engine output legitimately holds one, and inferring from it promoted real
    published datasources into `migrations/workbooks/`. Its report is deliberately NOT copied: the
    deliverable for a datasource is the model, and the loose `.pbip` beside it names a report that
    would not be there.
    """
    if shape.model is None:  # unreachable: discover_shape refuses a package with no model
        raise CannotAssess("package holds no model to promote")
    if shape.kind == KIND_DATASOURCE:
        model_dest_root = migrations_root / "datasources" / slug / "fabric"
        return PromotionPlan(
            shape=shape,
            steps=[CopyStep(shape.model, model_dest_root / shape.model.name, "model")],
            report_destination=None,
            model_destination=model_dest_root / shape.model.name,
            bypath=None,
            migrations_root=migrations_root,
        )

    if shape.report is None:  # unreachable: discover_shape refuses a workbook with no report
        raise CannotAssess("package declares a workbook but holds no report")
    report_dest_root = migrations_root / "workbooks" / slug / "fabric"
    steps = [CopyStep(shape.report, report_dest_root / shape.report.name, "report")]
    steps.extend(CopyStep(f, report_dest_root / f.name, "loose") for f in shape.loose_files)

    if datasource_slug is None:
        # Model per workbook: already siblings, and the deliverable has the identical shape.
        steps.append(CopyStep(shape.model, report_dest_root / shape.model.name, "model"))
        return PromotionPlan(
            shape=shape,
            steps=steps,
            report_destination=report_dest_root / shape.report.name,
            model_destination=report_dest_root / shape.model.name,
            bypath=f"../{shape.model.name}",
            migrations_root=migrations_root,
        )

    # Shared/published datasource: the halves split up and stop being siblings.
    model_dest = migrations_root / "datasources" / datasource_slug / "fabric" / shape.model.name
    steps.append(CopyStep(shape.model, model_dest, "model"))
    stale = report_dest_root / shape.model.name
    return PromotionPlan(
        shape=shape,
        steps=steps,
        report_destination=report_dest_root / shape.report.name,
        model_destination=model_dest,
        bypath=f"../../../../datasources/{datasource_slug}/fabric/{shape.model.name}",
        migrations_root=migrations_root,
        stale_removals=[stale] if stale.exists() else [],
    )


@dataclass
class AppliedCopies:
    """What a copy actually did on disk, so it can be undone.

    ⚠️ Without this, a failure AFTER the first copy left a half-shipped deliverable: an injected
    `copy2` failure exited 4 with the report already written and no record beside it - an artifact
    that looks promoted and was never verified. A replaced destination is moved aside rather than
    deleted, so rollback restores the previous deliverable instead of merely removing the new one.
    """

    created: list[Path] = field(default_factory=list)
    replaced: list[tuple[Path, Path]] = field(default_factory=list)

    def rollback(self) -> None:
        """Undo everything, best effort - a failed rollback must not mask the original failure."""
        for destination in reversed(self.created):
            with suppress(OSError):
                _remove(destination)
        for destination, backup in reversed(self.replaced):
            with suppress(OSError):
                _remove(destination)
                backup.rename(destination)

    def commit(self) -> None:
        """Drop the backups once everything that had to be true afterwards is true."""
        for _, backup in self.replaced:
            with suppress(OSError):
                _remove(backup)


def _remove(path: Path) -> None:
    """Delete a file or a whole directory, whichever it is."""
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _stash(destination: Path, applied: AppliedCopies) -> None:
    """Move an existing destination aside (recording it) or note that we are creating it."""
    if not destination.exists():
        applied.created.append(destination)
        return
    backup = destination.with_name(f"{destination.name}.promote-backup-{os.getpid()}")
    _remove(backup)
    destination.rename(backup)
    applied.replaced.append((destination, backup))


def execute_plan(plan: PromotionPlan) -> AppliedCopies:
    """Perform the copies. Idempotent: an existing destination artifact is replaced wholesale.

    Returns what it did, so `_promote` can roll the whole promotion back if any later verification
    fails. A failure DURING the copy rolls itself back before re-raising.
    """
    _assert_contained(
        [step.destination for step in plan.steps] + plan.stale_removals,
        plan.migrations_root,
        "a copy",
    )
    applied = AppliedCopies()
    try:
        for stale in plan.stale_removals:
            _stash(stale, applied)
        for step in plan.steps:
            step.destination.parent.mkdir(parents=True, exist_ok=True)
            _stash(step.destination, applied)
            if step.source.is_dir():
                shutil.copytree(
                    step.source,
                    step.destination,
                    ignore=shutil.ignore_patterns(*DESKTOP_LOCAL_EXCLUSIONS),
                )
            else:
                shutil.copy2(step.source, step.destination)
    except OSError:
        applied.rollback()
        raise
    return applied


def rewrite_bypath(report: Path, bypath: str) -> dict[str, Any]:
    """Point `definition.pbir`'s `datasetReference.byPath.path` at `bypath`."""
    pbir = report / "definition.pbir"
    if not pbir.is_file():
        raise PromotionFailed(f"{report.name}: no definition.pbir to rewrite")
    try:
        payload = json.loads(pbir.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionFailed(f"{report.name}: definition.pbir could not be read: {_safe_error(exc)}") from exc
    reference = payload.get("datasetReference")
    if not isinstance(reference, dict) or not isinstance(reference.get("byPath"), dict):
        raise PromotionFailed(f"{report.name}: definition.pbir has no datasetReference.byPath to rewrite")
    previous = reference["byPath"].get("path")
    if previous == bypath:
        return {"previous": previous, "written": bypath, "changed": False}
    reference["byPath"]["path"] = bypath
    pbir.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"previous": previous, "written": bypath, "changed": True}


def verify_bypath(report: Path, migrations_root: Path, expected_model: Path) -> dict[str, Any]:
    """Resolve `definition.pbir`'s `byPath` relative to the `.Report` folder, and prove the target
    is a REAL semantic model - specifically, the model THIS promotion copied.

    ⚠️ `powerbi-report-author validate` returns `errorCount: 0` for a `.Report` whose `byPath`
    names a `.SemanticModel` that exists nowhere - shape, not target. ⚠️ *"A directory containing
    some `definition/`"* is not enough either: a report-only package promoted at exit **0** against
    a hand-made folder holding an empty `definition/`. ⚠️ Nor is *"a real model at that path"*: a
    model left in the destination by an EARLIER run made a package that was missing its own model
    verify clean. So the target must BE `expected_model`, carry `.SemanticModel`, lie inside the
    migrations root, and pass the SAME content check criterion 5 applies to a shipped model -
    reused, not re-implemented. Resolution is **lexical**.
    """
    pbir = report / "definition.pbir"
    if not pbir.is_file():
        raise PromotionFailed(f"{report.name}: shipped report has no definition.pbir")
    try:
        payload = json.loads(pbir.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionFailed(f"{report.name}: shipped definition.pbir could not be read: {_safe_error(exc)}") from exc
    reference = payload.get("datasetReference")
    by_path = reference.get("byPath") if isinstance(reference, dict) else None
    if not isinstance(by_path, dict) or not isinstance(by_path.get("path"), str):
        raise PromotionFailed(f"{report.name}: shipped definition.pbir has no datasetReference.byPath.path string")
    declared = by_path["path"]
    target = Path(os.path.normpath(report / declared))
    _refuse_bad_model_target(report.name, declared, target, migrations_root, expected_model)
    return {"path": declared, "resolves": True, "target": target.name}


def _refuse_bad_model_target(
    report_name: str, declared: str, target: Path, migrations_root: Path, expected_model: Path
) -> None:
    """Raise unless `target` is the model this promotion copied, inside the tree, with real content.

    ⚠️ **Identity is deliberately tested FIRST**, ahead of containment, and an escaping `byPath`
    therefore reports "not the model this promotion copied" rather than "outside the migrations
    root". Both are true of such a reference; identity is the one an operator can act on. It names
    the root cause - *this report is not bound to the model we shipped* - while containment names a
    consequence of the same wrong binding, and identity is also the only branch that survives the
    realistic case where the wrong target is a real, valid, contained model left by an earlier run.
    Containment is not dead behind it: it fires whenever the model the promotion copied itself lies
    outside the root, which is the plan-construction bug `_assert_contained` exists to stop.
    """
    reason = None
    if Path(os.path.normpath(target)) != Path(os.path.normpath(expected_model)):
        reason = f"resolves to {target.name!r}, which is not the {expected_model.name!r} this promotion copied"
    elif not _resolved_for_containment(target).is_relative_to(_resolved_for_containment(migrations_root)):
        reason = "resolves outside the migrations root"
    elif not target.is_dir():
        reason = "does not resolve to a directory on disk"
    elif not target.name.endswith(MODEL_SUFFIX):
        reason = f"resolves to {target.name!r}, which is not a {MODEL_SUFFIX} folder"
    else:
        content = check_model_content(target)
        if not content.ok:
            reason = "resolves to a folder that is not a working semantic model: " + "; ".join(
                content.findings + content.unassessable
            )
    if reason is not None:
        raise PromotionFailed(
            f"{report_name}: byPath {_safe_reference(declared)!r} {reason} "
            f"(a wrong byPath opens as a report with NO MODEL and validates clean)"
        )


def _safe_reference(value: str) -> str:
    """A `byPath` as it may be recorded: redacted if someone put an absolute path in it."""
    return _redact_path(value) if _is_local_filesystem_path(value) else value


def tree_drift(package_fabric: Path, bundle_unit: Path | None) -> dict[str, Any]:
    """Report whether the package's working copy has diverged from its originating bundle tree.

    ⚠️ Divergence ONLY. This never says which side is authoritative and never fails the promotion.
    `not_checked` when no `--bundle` was given, deliberately distinct from "no drift".
    """
    if bundle_unit is None:
        return {"status": "not_checked", "reason": "no --bundle given; the package records no origin"}
    if not bundle_unit.is_dir():
        return {"status": "not_checked", "reason": f"bundle unit not found: {bundle_unit.name}"}

    def relative_files(root: Path) -> dict[str, Path]:
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}

    left, right = relative_files(package_fabric), relative_files(bundle_unit)
    only_package = sorted(set(left) - set(right))
    only_bundle = sorted(set(right) - set(left))
    differing = sorted(name for name in set(left) & set(right) if left[name].read_bytes() != right[name].read_bytes())
    if not (only_package or only_bundle or differing):
        return {"status": "identical", "reason": "the two candidate sources are byte-identical"}
    return {
        "status": "diverged",
        "only_in_package": only_package,
        "only_in_bundle": only_bundle,
        "differing": differing,
        "reason": "divergence only - this does NOT establish which side is authoritative",
    }


def _engine_provenance(package: Path) -> dict[str, Any]:
    """The engine version plus WHERE it came from, so `null` is never ambiguous.

    A missing `engine-output-receipt.json` is a provenance gap, not a correctness one, so it does
    not block - but a bare `"engine_version": null` cannot be told apart from a receipt that
    carried no version, so the source is recorded beside it and stays explicit.

    ⚠️ Read BEFORE anything is copied, and tolerant of every malformed shape. A receipt holding
    valid JSON that is not an object (`[]`) raised `AttributeError` out of `_engine_version` AFTER
    the copy, which rollback did not cover - a shipped, unverified deliverable with no record.
    """
    receipt = package / "engine-output-receipt.json"
    if not receipt.is_file():
        return {"engine_version": None, "engine_version_source": "UNAVAILABLE: no engine-output-receipt.json"}
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "engine_version": None,
            "engine_version_source": f"UNAVAILABLE: receipt unreadable ({_safe_error(exc)})",
        }
    if not isinstance(payload, dict):
        return {"engine_version": None, "engine_version_source": "UNAVAILABLE: receipt is not a JSON object"}
    engine = payload.get("engine")
    version = engine.get("version") if isinstance(engine, dict) else None
    if isinstance(version, str):
        return {"engine_version": version, "engine_version_source": "engine-output-receipt.json"}
    return {"engine_version": None, "engine_version_source": "UNAVAILABLE: receipt declares no engine.version"}


def build_record(
    args: argparse.Namespace,
    plan: PromotionPlan,
    repo_root: Path,
    gate: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the promotion record: what promoted this, from what, and was it checked."""
    source, source_is_repo_relative = _repo_relative(args.package, repo_root)
    copied = [
        {
            "what": step.what,
            "destination": _destination_display(step.destination, plan, repo_root),
            "files": _count_files(step.source),
        }
        for step in plan.steps
    ]
    return {
        "record_version": RECORD_VERSION,
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "promoted_by": "scripts/promote_unit.py",
        "source_package": source,
        "source_package_is_repo_relative": source_is_repo_relative,
        "unit": args.package.name,
        "slug": args.slug,
        "datasource_slug": args.datasource_slug,
        "kind": plan.shape.kind,
        "kind_source": plan.shape.kind_source,
        "shape": "shared_datasource" if args.datasource_slug else f"model_per_{plan.shape.kind}",
        "check_unit": gate,
        "forced": bool(args.force),
        "copied": copied,
        **extra,
    }


def write_records(record: dict[str, Any], plan: PromotionPlan, applied: AppliedCopies) -> list[Path]:
    """Write the record beside every deliverable this promotion touched.

    Both halves of a split promotion get one: either can be found on its own months later.

    ⚠️ Written THROUGH `applied`, so the records are part of the same transaction as the report and
    the model. Measured otherwise: an `OSError` between the first and second record exited 4 having
    left a record claiming a promotion beside NO promoted artifacts.
    """
    roots: list[Path] = []
    for destination in (plan.report_destination, plan.model_destination):
        if destination is None:
            continue
        root = destination.parent.parent  # <...>/<slug>/fabric/<Artifact> -> <...>/<slug>
        if root not in roots:
            roots.append(root)
    written = []
    for root in roots:
        path = root / RECORD_NAME
        _stash(path, applied)
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and normalise the command line."""
    parser = UsageErrorParser(
        description="Promote one packaged migration unit to its migrations/ deliverable (phase 2 -> phase 3).",
    )
    parser.add_argument("--package", required=True, type=Path, help="the phase-2 package directory to promote FROM")
    parser.add_argument("--slug", required=True, help="deliverable slug under migrations/workbooks (or /datasources)")
    parser.add_argument(
        "--datasource-slug",
        default=None,
        help="shared/published datasource: land the model once under migrations/datasources/<ds-slug>/fabric "
        "and rewrite the report's byPath to reach it",
    )
    parser.add_argument("--bundle", type=Path, default=None, help="originating engine bundle, for a drift REPORT only")
    parser.add_argument(
        "--kind",
        choices=DECLARABLE_KINDS,
        default=None,
        help=f"declare the unit's kind when {MANIFEST_NAME} does not (missing, or 'unclassified'); it is "
        "recorded, and it can never contradict a manifest that does declare one",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and file count; change nothing")
    parser.add_argument(
        "--force",
        action="store_true",
        help="promote even though check_unit.py failed; the override and the exit code are recorded. It "
        "overrides THAT GATE ONLY - not the content checks, and not the external-data-path refusal (#461), "
        "which has no sanitized artifact to ship",
    )
    parser.add_argument("--json", dest="json_path", type=Path, default=None, help="write the machine-readable envelope")
    parser.add_argument(
        "--migrations-root",
        type=Path,
        default=None,
        help="override the migrations/ root (tests and rehearsals); defaults to <repo>/migrations",
    )
    args = parser.parse_args(argv)
    args.package = args.package.expanduser().resolve()
    args.migrations_root = (args.migrations_root or (REPO_ROOT / "migrations")).expanduser().resolve()
    if args.bundle is not None:
        args.bundle = args.bundle.expanduser().resolve()
    for name, value in (("--slug", args.slug), ("--datasource-slug", args.datasource_slug)):
        if value is None:
            continue
        problem = slug_problem(value)
        if problem is not None:
            parser.error(f"{name} {problem}")
    return args


def _emit(envelope: dict[str, Any], args: argparse.Namespace) -> None:
    """Write the `--json` envelope when one was asked for."""
    if args.json_path is None:
        return
    args.json_path.parent.mkdir(parents=True, exist_ok=True)
    args.json_path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")


def _bundle_unit(args: argparse.Namespace) -> Path | None:
    """Where the originating working copy would live inside `--bundle`, if one was given.

    The unit key is the package manifest's own `unit`, not the package DIRECTORY name: a package
    can legitimately be copied or renamed, and keying on the folder name silently reports
    `not_checked` for it.
    """
    if args.bundle is None:
        return None
    unit = args.package.name
    manifest = args.package / "package-manifest.json"
    if manifest.is_file():
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8")).get("unit")
        except (OSError, json.JSONDecodeError):
            declared = None
        if isinstance(declared, str) and declared.strip():
            unit = declared
    return args.bundle / "pbip" / unit


def _print_plan(plan: PromotionPlan, repo_root: Path) -> None:
    """Print the human-readable plan."""
    for step in plan.steps:
        destination = _destination_display(step.destination, plan, repo_root)
        print(f"  {step.what:<6} {step.source.name} -> {destination} ({_count_files(step.source)} files)")
    for stale in plan.stale_removals:
        print(f"  remove stale {_destination_display(stale, plan, repo_root)}")
    if plan.bypath:
        print(f"  byPath       {plan.bypath}")


def _refuse(envelope: dict[str, Any], args: argparse.Namespace, status: str, findings: list[str], code: int) -> int:
    """Record a refusal, print it, and return its exit code."""
    envelope["status"] = status
    envelope["findings"] = findings
    envelope["exit_code"] = code
    _emit(envelope, args)
    print(f"PROMOTE: {status}", file=sys.stderr)
    for finding in findings:
        print(f"  - {finding}", file=sys.stderr)
    return code


@dataclass(frozen=True)
class PreCopyFacts:
    """Everything established BEFORE a single byte was written, carried past the copy as one value.

    The two members travel together because they are the same kind of thing - a fact about the
    package that the promotion must not re-derive from the tree it has already modified. Reading
    the engine receipt AFTER the copy is what produced the measured failure `_promote`'s rollback
    exists for, and bundling them keeps `_verify_and_record` at an argument count the linter and a
    reader both accept.
    """

    gate: dict[str, Any]
    provenance: dict[str, Any]


def _promote(args: argparse.Namespace, plan: PromotionPlan, envelope: dict[str, Any], gate: dict[str, Any]) -> int:
    """Do the copy and everything that must be true afterwards.

    ⚠️ The copy is rolled back if ANY later step fails - not merely `PromotionFailed` and `OSError`.
    Measured: an ordinary `AttributeError` raised after the copy left a shipped, unverified
    deliverable behind, one that looks promoted and never was. A `finally` that commits only on the
    success path cannot be narrowed by accident the way an exception tuple can.

    Metadata is read BEFORE the copy for the same reason: reading it afterwards is what created the
    class of failure that had to be rolled back.
    """
    facts = PreCopyFacts(gate=gate, provenance=_engine_provenance(args.package))
    applied = execute_plan(plan)
    committed = False
    try:
        result = _verify_and_record(args, plan, envelope, facts, applied)
        committed = True
    finally:
        if committed:
            applied.commit()
        else:
            applied.rollback()
    return result


def _verify_and_record(
    args: argparse.Namespace,
    plan: PromotionPlan,
    envelope: dict[str, Any],
    facts: PreCopyFacts,
    applied: AppliedCopies,
) -> int:
    """Everything that must be true about the SHIPPED tree, then the record."""
    extra: dict[str, Any] = dict(facts.provenance)
    if plan.report_destination is not None and plan.bypath is not None:
        extra["bypath_rewrite"] = rewrite_bypath(plan.report_destination, plan.bypath)
    if plan.report_destination is not None:
        extra["bypath_verified"] = verify_bypath(plan.report_destination, plan.migrations_root, plan.model_destination)

    shipped = content_checks(plan.report_destination, plan.model_destination, "shipped")
    if not shipped.ok:
        raise PromotionFailed("; ".join(shipped.findings + shipped.unassessable))
    extra["shipped_content"] = shipped.counts

    shipped_external = (
        external_data_paths(plan.model_destination, (plan.deliverable_root,)) if plan.model_destination else []
    )
    if shipped_external:
        # ⚠️ NOT overridable by `--force`. The tool cannot rewrite a customer's M query, so it
        # cannot ship a SANITIZED artifact here - forcing would put the raw absolute path, with its
        # server and user names, into a committable TMDL. Same exception, and so the same exit
        # code, as the source scan: one condition, one verdict.
        raise ExternalDataPath(
            "EXTERNAL_DATA_PATH in the SHIPPED model: "
            + "; ".join(f"{item['file']} reads {item['redacted']}" for item in shipped_external)
        )
    extra["external_data_paths"] = {
        "source": envelope.get("external_data_paths", {}).get("source", []),
        "shipped": [],
    }

    # ⚠️ **The PACKAGE is deliberately NOT an allowed root here, and the source scan's allowance
    # of it is not an inconsistency.** The whole point of promotion is that the deliverable stops
    # depending on the phase-2 package, so a shipped file naming `…\packages\<Unit>\…` is BOTH a
    # host-path leak and a deliverable that cannot refresh on any other machine - #461's defect,
    # measured across run 408's 62 packaged units as 32 absolute references in 26 of them.
    # Measured 2026-09-04, before this fix:
    # an absolute `<package>\data\CustomerServer\source.csv` in a shipped `Wb.pbip` promoted at
    # `exit=0 status=PROMOTED`, source findings empty, with the package path still in the shipped
    # file. `_build_plan_unchecked` copies `fabric/` only, so that `data/` never travels - the
    # reference is dangling as well as leaking. Same allowed-root rule the SHIPPED external-path
    # scan above already applies (`(plan.deliverable_root,)`); this scan was the outlier.
    shipped_host = shipment_host_paths(
        tuple(step.destination for step in plan.steps),
        plan.deliverable_roots,
    )
    if shipped_host:
        # ⚠️ REFUSED, never rewritten. A deliverable-relative rewrite would point at a file the
        # plan does not copy, trading a loud refusal for a silently broken deliverable - which is
        # the #461 defect itself. NOT overridable by `--force` either: the tool cannot sanitize a
        # customer's `localSettings.json` or `.pbip`, so there is no clean artifact for a force to
        # ship. Same exception, same exit code as the source scan - one condition, one verdict,
        # whichever scan saw it first.
        raise HostPathLeak(
            "HOST_PATH in the SHIPPED tree: "
            + "; ".join(f"{item['file']} carries {item['redacted']}" for item in shipped_host)
            + "; a path resolving outside the deliverable it was promoted into cannot refresh on "
            "any other machine, and one pointing back at the phase-2 package publishes that "
            "package's location as well. Remedy: delete or sanitize the reference, or carry the "
            "data into the deliverable with scripts/set_data_folder.py."
        )
    extra["host_paths"] = {"source": envelope.get("host_paths", {}).get("source", []), "shipped": []}
    extra["drift"] = envelope["drift"]

    record = build_record(args, plan, REPO_ROOT, facts.gate, extra)
    written = write_records(record, plan, applied)
    envelope["status"] = "PROMOTED"
    envelope["exit_code"] = EXIT_OK
    envelope["record"] = record
    envelope["record_paths"] = [_destination_display(p, plan, REPO_ROOT) for p in written]
    _emit(envelope, args)
    print(f"PROMOTE: PROMOTED {args.package.name} -> {args.slug} ({plan.file_count} files)")
    for path in written:
        print(f"  record  {_destination_display(path, plan, REPO_ROOT)}")
    return EXIT_OK


def assess(args: argparse.Namespace, envelope: dict[str, Any]) -> tuple[PackageShape | None, dict[str, Any], int]:
    """Everything that must hold BEFORE anything is written.

    Order is deliberate: kind, then shape, then content, then the gate. `--force` reaches only the
    GATE - everything above it is mandatory and cannot be overridden.
    """
    try:
        kind, kind_source = declared_kind(args.package, args.kind)
        shape = discover_shape(args.package, kind, kind_source)
        _refuse_incompatible_arguments(args, shape)
    except CannotAssess as exc:
        return None, {}, _refuse(envelope, args, "CANNOT_ASSESS", [str(exc)], EXIT_CANNOT_ASSESS)

    envelope["kind"] = shape.kind
    envelope["kind_source"] = shape.kind_source
    envelope["shape"] = "shared_datasource" if args.datasource_slug else f"model_per_{shape.kind}"

    source = content_checks(shape.report if shape.kind == KIND_WORKBOOK else None, shape.model, "source")
    envelope["source_content"] = source.counts
    if source.unassessable:
        # ⚠️ Unreadable content is CANNOT_ASSESS, never "refused on content" and never a pass. A
        # malformed `visual.json` used to COUNT as a visual, and the record then asserted it.
        return None, {}, _refuse(envelope, args, "CANNOT_ASSESS", source.unassessable, EXIT_CANNOT_ASSESS)
    if source.findings:
        return None, {}, _refuse(envelope, args, "REFUSED_CONTENT", source.findings, EXIT_REFUSED_CONTENT)

    gate = run_check_unit(args.package, REPO_ROOT)
    envelope["check_unit"] = gate
    if not gate["passed"] and not args.force:
        findings = [f"check_unit.py exited {gate['exit_code']} ({gate['status']}); --force overrides and is recorded"]
        return None, gate, _refuse(envelope, args, "REFUSED_BY_GATE", findings, EXIT_REFUSED_BY_GATE)
    return shape, gate, EXIT_OK


def _refuse_incompatible_arguments(args: argparse.Namespace, shape: PackageShape) -> None:
    """Refuse an argument that contradicts the kind the manifest declared.

    `--datasource-slug` splits a WORKBOOK's halves and rewrites its report's `byPath`. A declared
    datasource has no report to rewrite and its model already lands under `datasources/<slug>`, so
    accepting the flag there would silently do something else than what was asked.
    """
    if args.datasource_slug is not None and shape.kind == KIND_DATASOURCE:
        raise CannotAssess(
            f"--datasource-slug splits a workbook's report from its model, but {MANIFEST_NAME} declares this "
            f"unit a datasource; its model already lands under migrations/datasources/<--slug>"
        )


def _dry_run(args: argparse.Namespace, plan: PromotionPlan, envelope: dict[str, Any]) -> int:
    """Print the plan and its file count; change nothing."""
    envelope["status"] = "DRY_RUN"
    envelope["exit_code"] = EXIT_OK
    _emit(envelope, args)
    print(f"PROMOTE: DRY_RUN {args.package.name} -> {args.slug} ({plan.file_count} files)")
    _print_plan(plan, REPO_ROOT)
    return EXIT_OK


def _check_external_paths(args: argparse.Namespace, shape: PackageShape, plan: PromotionPlan, envelope: dict) -> int:
    """Refuse a model that reads data from outside the tree it is being promoted into (#461).

    Scanned at the SOURCE so a refusal ships nothing, and again after the copy in `_promote`.
    Both the package and the deliverable count as allowed roots: after the packaging half of #461
    lands the extract travels INSIDE the package, and `set_data_folder.py`'s convention puts a
    legitimate absolute path under the deliverable's own `data/`.

    ⚠️ **`--force` does not reach this.** It overrides the `check_unit.py` gate; it cannot rewrite a
    customer's M query, so there is no sanitized artifact for it to ship - forcing put the raw
    absolute path, server and user names included, into a committable TMDL under
    `migrations/**`, which is not blanket-gitignored and where nothing downstream would have caught
    it. Remedy: `scripts/set_data_folder.py`, or carry the extract into the package.
    """
    if shape.model is None:
        return EXIT_OK
    found = external_data_paths(shape.model, (args.package, plan.deliverable_root))
    envelope["external_data_paths"] = {
        "source": [{"file": item["file"], "path": item["redacted"]} for item in found],
    }
    if not found:
        return EXIT_OK
    findings = [
        f"EXTERNAL_DATA_PATH: {item['file']} reads {item['redacted']}, which is absolute and resolves "
        f"OUTSIDE both the package and the deliverable"
        for item in found
    ]
    findings.append(
        "A promoted model reading from a gitignored, machine-local, prunable location is the "
        "structurally-present/functionally-EMPTY deliverable (#461). An absolute path also embeds a real "
        "USERNAME (scripts/set_data_folder.py --check gates that), so this is NOT forceable - there is no "
        "sanitized artifact to ship. Carry the extract INTO the package, or rewrite the reference with "
        "scripts/set_data_folder.py."
    )
    code = _refuse(envelope, args, "REFUSED_EXTERNAL_DATA_PATH", findings, EXIT_REFUSED_EXTERNAL_PATH)
    for item in found:
        print(f"  full path (not recorded, terminal only): {item['full']}", file=sys.stderr)
    return code


def _check_host_paths(args: argparse.Namespace, plan: PromotionPlan, envelope: dict) -> int:
    """Refuse to ship ANY file carrying an absolute host path, whatever its extension (finding 1).

    Scanned at the SOURCE, over exactly the files the plan will copy, so a refusal ships nothing;
    scanned again over the shipped tree in `_verify_and_record`. `--force` does not reach this, for
    the same reason it does not reach #461: there is no sanitized artifact to ship.

    ⚠️ **The two scans use DIFFERENT allowed roots on purpose, and the asymmetry is measured, not
    an oversight.** Here the package IS an allowed root: the artifact legitimately still lives in
    it, and one shipped file's content is REWRITTEN on the way out - a source `definition.pbir`
    whose `byPath` is the absolute `<package>\\fabric\\<Model>.SemanticModel` is replaced by
    `rewrite_bypath` with `../<Model>.SemanticModel` and ships clean (measured 2026-09-04:
    `exit=0`, shipped byPath `'../Model.SemanticModel'`). Refusing that here would refuse a real
    delivery, which is a worse defect than the one being fixed. The SHIPPED scan judges the same
    files after every rewrite, where the package is no longer defensible as a root - see
    `_verify_and_record`. So a package-local path that is NOT rewritten costs a copy and a
    rollback rather than an early exit; nothing ships either way.
    """
    found = shipment_host_paths(
        tuple(step.source for step in plan.steps),
        (args.package, *plan.deliverable_roots),
    )
    envelope["host_paths"] = {"source": [{"file": item["file"], "path": item["redacted"]} for item in found]}
    if not found:
        return EXIT_OK
    findings = [
        f"HOST_PATH: {item['file']} carries {item['redacted']}, an absolute path that resolves OUTSIDE "
        f"both the package and the deliverable"
        for item in found
    ]
    findings.append(
        "migrations/** is not blanket-gitignored and this repo is PUBLIC, so a shipped absolute path "
        "publishes a real username, and a customer package path publishes their server, project and "
        "operator names. Power BI Desktop's own .pbi/ state is excluded from the shipment already; "
        "anything left here is a file that must be deleted or sanitized before it can be promoted."
    )
    code = _refuse(envelope, args, "REFUSED_HOST_PATH", findings, EXIT_REFUSED_HOST_PATH)
    for item in found:
        print(f"  full path (not recorded, terminal only): {item['full']}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    envelope: dict[str, Any] = {
        "tool": "scripts/promote_unit.py",
        "package": _repo_relative(args.package, REPO_ROOT)[0],
        "slug": args.slug,
        "datasource_slug": args.datasource_slug,
        "dry_run": bool(args.dry_run),
        "forced": bool(args.force),
    }

    shape, gate, code = assess(args, envelope)
    if shape is None:
        return code

    envelope["drift"] = tree_drift(shape.fabric, _bundle_unit(args))
    try:
        plan = build_plan(shape, args.migrations_root, args.slug, args.datasource_slug)
        envelope["planned_files"] = plan.file_count
        external = _check_external_paths(args, shape, plan, envelope)
        host = _check_host_paths(args, plan, envelope) if external == EXIT_OK else EXIT_OK
    except CannotAssess as exc:
        return _refuse(envelope, args, "CANNOT_ASSESS", [str(exc)], EXIT_CANNOT_ASSESS)
    if external != EXIT_OK:
        return external
    if host != EXIT_OK:
        return host

    if args.dry_run:
        return _dry_run(args, plan, envelope)
    return _run_promotion(args, plan, envelope, gate)


def _run_promotion(
    args: argparse.Namespace, plan: PromotionPlan, envelope: dict[str, Any], gate: dict[str, Any]
) -> int:
    """Promote, mapping every failure mode onto its own exit code.

    ⚠️ The final `Exception` clause is deliberate and is not a swallow: everything it catches has
    already been rolled back by `_promote`'s `finally`, and this turns it into a recorded exit code
    instead of a traceback. A traceback is not merely untidy here - it has no exit contract for
    automation to route on, and its rendering carries the absolute host paths this tool exists to
    keep out of artifacts.
    """
    try:
        return _promote(args, plan, envelope, gate)
    except CannotAssess as exc:
        return _refuse(envelope, args, "CANNOT_ASSESS", [str(exc)], EXIT_CANNOT_ASSESS)
    except ExternalDataPath as exc:
        return _refuse(envelope, args, "REFUSED_EXTERNAL_DATA_PATH", [str(exc)], EXIT_REFUSED_EXTERNAL_PATH)
    except HostPathLeak as exc:
        return _refuse(envelope, args, "REFUSED_HOST_PATH", [str(exc)], EXIT_REFUSED_HOST_PATH)
    except PromotionFailed as exc:
        return _refuse(envelope, args, "PROMOTION_FAILED", [str(exc)], EXIT_PROMOTION_FAILED)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return _refuse(
            envelope, args, "PROMOTION_FAILED", [f"promotion failed: {_safe_error(exc)}"], EXIT_PROMOTION_FAILED
        )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_USAGE)
