"""
purpose: ship one finished migration unit - the phase 2 -> phase 3 hop that had no tool (#458).
         Copies a `package_unit.py` package's `fabric/` working copy into the customer deliverable
         at `migrations/{workbooks,datasources}/<slug>/fabric/`, running `check_unit.py` first and
         REFUSING on a non-zero exit, rewriting `definition.pbir`'s `byPath` for the shared
         /published datasource shape, proving that reference resolves ON DISK, proving the shipped
         report and model have real CONTENT, and recording what promoted what.
usage:   python scripts/promote_unit.py --package <_runs/NNN-slug/packages/<batch>/<Unit>>
             --slug <slug> [--datasource-slug <ds-slug>] [--bundle <bundle>]
             [--dry-run] [--force] [--json <file>] [--migrations-root <dir>]

Why each guard exists (all of these are measured failures, not hypotheses)
--------------------------------------------------------------------------
* **Re-running `check_unit.py` here is deliberate duplication.** It costs under a second, and this
  is the hop where a defect stops being a working copy and becomes a deliverable. `--force`
  overrides the GATE (never the content checks), and the override plus the observed exit code are
  written into the record - an unchecked promotion must never look checked afterwards.
* **`byPath` is verified against the filesystem, not a schema.** `powerbi-report-author validate`
  returns `errorCount: 0` for a `.Report` whose `datasetReference.byPath.path` names a
  `.SemanticModel` that exists nowhere: shape, not target
  (`.github/skills/powerbi-report-gotchas/SKILL.md` §3). A wrong one opens as a report with NO
  MODEL. The target must be a real model, so "some directory holding a `definition/`" is not enough.
* **Content, not existence — and not a FILE COUNT either.** On a 46-asset estate a report folder
  that had passed a sign-off held only Desktop-local settings. Every PBIR document is parsed, at
  the SOURCE and again at the DESTINATION; unreadable input is `CANNOT_ASSESS`, never a count.
* **The slug must be a single safe path component.** `execute_plan` replaces its destination, so a
  slug carrying `..` is not a misfiling - it is a delete outside the migration root.
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
| 64 | usage error |

⚠️ 2 exists so "cannot assess" can never collapse into the clean bucket - this repo's most common
gate defect class. An unreadable package is a blocking state, not a silent success.
"""

from __future__ import annotations

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
RECORD_VERSION = 1

# A TMDL table declaration is a column-0 `table <name>` line; an indented one is a nested property.
_TMDL_TABLE_RE = re.compile(r"^table\s+\S", re.MULTILINE)

# Every double-quoted literal in a TMDL/M source block. M has no escaped quote inside a literal -
# it doubles them - so a non-greedy run between quotes is the right shape here.
_TMDL_STRING_RE = re.compile(r'"([^"\n]*)"')

# A run of adjacent string literals joined by M's `&` concatenation operator, possibly across
# lines: `"C:" & "\secret\data.csv"`. Judged as one value, because each fragment alone is harmless.
_TMDL_CONCAT_RE = re.compile(r'"[^"\n]*"(?:\s*&\s*"[^"\n]*")+')


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


@dataclass(frozen=True)
class PackageShape:
    """What one package's `fabric/` actually holds."""

    fabric: Path
    report: Path | None
    model: Path | None
    loose_files: tuple[Path, ...]

    @property
    def kind(self) -> str:
        """`workbook` when a report is present, else `datasource`."""
        return "workbook" if self.report is not None else "datasource"


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


def _count_files(path: Path) -> int:
    """Number of files at or under `path` (1 for a file)."""
    if path.is_file():
        return 1
    return sum(1 for item in path.rglob("*") if item.is_file())


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object, raising `CannotAssess` rather than returning a half-truth."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CannotAssess(f"{path.name} could not be read: {exc}") from exc
    if not isinstance(payload, dict):
        raise CannotAssess(f"{path.name} is not a JSON object")
    return payload


def _repo_relative(path: Path, repo_root: Path) -> tuple[str, bool]:
    """Return (displayable path, whether it is repo-relative).

    A record can be committed - `migrations/**/fabric/**` is NOT blanket-gitignored - so a path
    inside the repo is recorded relative to it, never as an absolute host path. A path outside the
    repo is recorded verbatim WITH the flag set; a customer migration is kept out of this public
    repo by prefixing the slug (`customer-<name>`), which is what the ignore rules key on.
    """
    try:
        return path.resolve().relative_to(repo_root).as_posix(), True
    except ValueError:
        return path.resolve().as_posix(), False


def discover_shape(package: Path) -> PackageShape:
    """Read `<package>/fabric/` and decide which of the two documented shapes this unit is.

    Raises `CannotAssess` for anything ambiguous - two reports, two models, or neither - because a
    guess here promotes the wrong tree into a customer deliverable.
    """
    if not package.is_dir():
        raise CannotAssess(f"package is not a directory: {package}")
    fabric = package / "fabric"
    if not fabric.is_dir():
        raise CannotAssess(f"package has no fabric/ working copy: {fabric}")
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

    loose = tuple(sorted(p for p in fabric.iterdir() if p.is_file()))
    return PackageShape(
        fabric=fabric,
        report=reports[0] if reports else None,
        model=models[0] if models else None,
        loose_files=loose,
    )


def _load_json_document(path: Path, label: str, check: ContentCheck) -> dict[str, Any] | None:
    """Parse one PBIR document, recording an UNASSESSABLE reason instead of guessing.

    ⚠️ A file that exists is not a document. Before this parsed anything, a zero-byte `visual.json`
    counted as a visual and the record then *asserted* `"visuals": 4`.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        check.unassessable.append(f"{label} is not readable JSON: {exc}")
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
    deliverable outside the root, and reported success. Judged purely lexically; `Path.resolve()`
    is never called on this untrusted value.
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
        (value.split(".")[0].upper() in _RESERVED_DEVICE_NAMES, "must not be a reserved Windows device name"),
    ):
        if failed:
            return message
    return None


def _assert_contained(paths: list[Path], root: Path, what: str) -> None:
    """Refuse any planned path that does not lie under the migrations root.

    Defence in depth behind `slug_problem`: a containment check that never fires is exactly the
    one you want here, because the one that DOES fire is the one nobody wrote.
    """
    resolved_root = root.resolve()
    for path in paths:
        normalized = Path(os.path.normpath(path))
        if not normalized.is_relative_to(resolved_root):
            raise CannotAssess(f"{what} would land outside the migrations root: {normalized.name}")


def check_report_content(report: Path) -> ContentCheck:
    """Assert `definition/pages/` enumerates real pages carrying real visuals.

    A folder count is not a content check - the precedent case had every expected folder present
    and nothing behind them. ⚠️ Neither is a FILE count: every document here is parsed, and
    `pages.json` (the manifest Power BI reads for page order) must be present and well-formed,
    because a report whose pages are not enumerated opens with none of them.
    """
    check = ContentCheck()
    pages_dir = report / "definition" / "pages"
    if not pages_dir.is_dir():
        check.findings.append(f"{report.name}: no definition/pages/ directory")
        return check

    manifest = pages_dir / "pages.json"
    if not manifest.is_file():
        check.unassessable.append(f"{report.name}: definition/pages/pages.json is missing - page order is unreadable")
    else:
        document = _load_json_document(manifest, f"{report.name}: pages.json", check)
        if document is not None and not (isinstance(document.get("pageOrder"), list) and document["pageOrder"]):
            check.unassessable.append(f"{report.name}: pages.json declares no non-empty pageOrder list")

    candidates = sorted(p for p in pages_dir.iterdir() if p.is_dir() and (p / "page.json").is_file())
    pages = [p for p in candidates if _page_is_well_formed(p, report.name, check)]
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
    """Assert `definition/tables/` holds real TMDL tables and the model has a `model.tmdl`."""
    check = ContentCheck()
    definition = model / "definition"
    tables_dir = definition / "tables"
    if not (definition / "model.tmdl").is_file():
        check.findings.append(f"{model.name}: no definition/model.tmdl")
    if not tables_dir.is_dir():
        check.findings.append(f"{model.name}: no definition/tables/ directory")
        return check
    tmdl = sorted(tables_dir.glob("*.tmdl"))
    real = [p for p in tmdl if _TMDL_TABLE_RE.search(p.read_text(encoding="utf-8", errors="replace"))]
    check.counts = {"table_files": len(tmdl), "tables": len(real)}
    if not real:
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
        text = tmdl.read_text(encoding="utf-8", errors="replace")
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
    fat-fingered slug is refused as `CANNOT_ASSESS` rather than escaping the migrations root.
    """
    plan = _build_plan_unchecked(shape, migrations_root, slug, datasource_slug)
    _assert_contained([step.destination for step in plan.steps], migrations_root, "a copy")
    _assert_contained(plan.stale_removals, migrations_root, "a stale removal")
    return plan


def _build_plan_unchecked(
    shape: PackageShape,
    migrations_root: Path,
    slug: str,
    datasource_slug: str | None,
) -> PromotionPlan:
    """The shape-specific half of `build_plan`, before containment is enforced."""
    if shape.report is None:
        if shape.model is None:  # unreachable: discover_shape refuses "neither", but never guess
            raise CannotAssess("package holds neither a report nor a model")
        model_dest_root = migrations_root / "datasources" / slug / "fabric"
        return PromotionPlan(
            shape=shape,
            steps=[CopyStep(shape.model, model_dest_root / shape.model.name, "model")],
            report_destination=None,
            model_destination=model_dest_root / shape.model.name,
            bypath=None,
            migrations_root=migrations_root,
        )

    report_dest_root = migrations_root / "workbooks" / slug / "fabric"
    steps = [CopyStep(shape.report, report_dest_root / shape.report.name, "report")]
    steps.extend(CopyStep(f, report_dest_root / f.name, "loose") for f in shape.loose_files)

    if shape.model is None:
        return PromotionPlan(
            shape=shape,
            steps=steps,
            report_destination=report_dest_root / shape.report.name,
            model_destination=None,
            bypath=None,
            migrations_root=migrations_root,
        )

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
                shutil.copytree(step.source, step.destination)
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
        raise PromotionFailed(f"{report.name}: definition.pbir could not be read: {exc}") from exc
    reference = payload.get("datasetReference")
    if not isinstance(reference, dict) or not isinstance(reference.get("byPath"), dict):
        raise PromotionFailed(f"{report.name}: definition.pbir has no datasetReference.byPath to rewrite")
    previous = reference["byPath"].get("path")
    if previous == bypath:
        return {"previous": previous, "written": bypath, "changed": False}
    reference["byPath"]["path"] = bypath
    pbir.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"previous": previous, "written": bypath, "changed": True}


def verify_bypath(report: Path, migrations_root: Path) -> dict[str, Any]:
    """Resolve `definition.pbir`'s `byPath` relative to the `.Report` folder, and prove the target
    is a REAL semantic model.

    ⚠️ `powerbi-report-author validate` returns `errorCount: 0` for a `.Report` whose `byPath`
    names a `.SemanticModel` that exists nowhere - shape, not target. ⚠️ *"A directory containing
    some `definition/`"* is not enough either: a report-only package promoted at exit **0** against
    a hand-made folder holding an empty `definition/`. So the target must carry `.SemanticModel`,
    lie inside the migrations root, and pass the SAME content check criterion 5 applies to a
    shipped model - reused, not re-implemented. Resolution is **lexical**.
    """
    pbir = report / "definition.pbir"
    if not pbir.is_file():
        raise PromotionFailed(f"{report.name}: shipped report has no definition.pbir")
    try:
        payload = json.loads(pbir.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionFailed(f"{report.name}: shipped definition.pbir could not be read: {exc}") from exc
    reference = payload.get("datasetReference")
    by_path = reference.get("byPath") if isinstance(reference, dict) else None
    if not isinstance(by_path, dict) or not isinstance(by_path.get("path"), str):
        raise PromotionFailed(f"{report.name}: shipped definition.pbir has no datasetReference.byPath.path string")
    declared = by_path["path"]
    target = Path(os.path.normpath(report / declared))
    _refuse_bad_model_target(report.name, declared, target, migrations_root)
    return {"path": declared, "resolves": True, "target": target.name}


def _refuse_bad_model_target(report_name: str, declared: str, target: Path, migrations_root: Path) -> None:
    """Raise unless `target` is a semantic model, inside the tree, with real content."""
    reason = None
    if not target.is_relative_to(migrations_root.resolve()):
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
            f"{report_name}: byPath {declared!r} {reason} "
            f"(a wrong byPath opens as a report with NO MODEL and validates clean)"
        )


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


def _engine_version(package: Path) -> str | None:
    """The engine version from the package's own receipt, or None when it carries no answer."""
    receipt = package / "engine-output-receipt.json"
    if not receipt.is_file():
        return None
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    engine = payload.get("engine")
    version = engine.get("version") if isinstance(engine, dict) else None
    return version if isinstance(version, str) else None


def _engine_provenance(package: Path) -> dict[str, Any]:
    """The engine version plus WHERE it came from, so `null` is never ambiguous.

    A missing `engine-output-receipt.json` is a provenance gap, not a correctness one, so it does
    not block - but a bare `"engine_version": null` cannot be told apart from a receipt that
    carried no version, so the source is recorded beside it and stays explicit.
    """
    receipt = package / "engine-output-receipt.json"
    version = _engine_version(package)
    if version is not None:
        return {"engine_version": version, "engine_version_source": "engine-output-receipt.json"}
    if not receipt.is_file():
        return {"engine_version": None, "engine_version_source": "UNAVAILABLE: no engine-output-receipt.json"}
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
    copied = []
    for step in plan.steps:
        destination, _ = _repo_relative(step.destination, repo_root)
        copied.append({"what": step.what, "destination": destination, "files": _count_files(step.source)})
    return {
        "record_version": RECORD_VERSION,
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "promoted_by": "scripts/promote_unit.py",
        "source_package": source,
        "source_package_is_repo_relative": source_is_repo_relative,
        "unit": args.package.name,
        "slug": args.slug,
        "datasource_slug": args.datasource_slug,
        "shape": "shared_datasource" if args.datasource_slug else f"model_per_{plan.shape.kind}",
        **_engine_provenance(args.package),
        "check_unit": gate,
        "forced": bool(args.force),
        "copied": copied,
        **extra,
    }


def write_records(record: dict[str, Any], plan: PromotionPlan) -> list[Path]:
    """Write the record beside every deliverable this promotion touched.

    Both halves of a split promotion get one: either can be found on its own months later.
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
    parser.add_argument("--dry-run", action="store_true", help="print the plan and file count; change nothing")
    parser.add_argument(
        "--force",
        action="store_true",
        help="promote even though check_unit.py failed; the override and the exit code are recorded",
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
        destination, _ = _repo_relative(step.destination, repo_root)
        print(f"  {step.what:<6} {step.source.name} -> {destination} ({_count_files(step.source)} files)")
    for stale in plan.stale_removals:
        destination, _ = _repo_relative(stale, repo_root)
        print(f"  remove stale {destination}")
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


def _promote(args: argparse.Namespace, plan: PromotionPlan, envelope: dict[str, Any], gate: dict[str, Any]) -> int:
    """Do the copy and everything that must be true afterwards.

    The copy is rolled back if ANY later step fails, so a refused promotion never leaves a
    half-shipped deliverable behind - one that looks promoted and was never verified.
    """
    applied = execute_plan(plan)
    try:
        result = _verify_and_record(args, plan, envelope, gate)
    except (PromotionFailed, OSError):
        applied.rollback()
        raise
    applied.commit()
    return result


def _verify_and_record(
    args: argparse.Namespace, plan: PromotionPlan, envelope: dict[str, Any], gate: dict[str, Any]
) -> int:
    """Everything that must be true about the SHIPPED tree, then the record."""
    extra: dict[str, Any] = {}
    if plan.report_destination is not None and plan.bypath is not None:
        extra["bypath_rewrite"] = rewrite_bypath(plan.report_destination, plan.bypath)
    if plan.report_destination is not None:
        extra["bypath_verified"] = verify_bypath(plan.report_destination, plan.migrations_root)

    shipped = content_checks(plan.report_destination, plan.model_destination, "shipped")
    if not shipped.ok:
        raise PromotionFailed("; ".join(shipped.findings + shipped.unassessable))
    extra["shipped_content"] = shipped.counts

    shipped_external = (
        external_data_paths(plan.model_destination, (plan.deliverable_root,)) if plan.model_destination else []
    )
    if shipped_external and not args.force:
        raise PromotionFailed(
            "EXTERNAL_DATA_PATH in the SHIPPED model: "
            + "; ".join(f"{item['file']} reads {item['redacted']}" for item in shipped_external)
        )
    extra["external_data_paths"] = {
        "source": envelope.get("external_data_paths", {}).get("source", []),
        "shipped": [{"file": item["file"], "path": item["redacted"]} for item in shipped_external],
        "forced": bool(shipped_external) and bool(args.force),
    }
    extra["drift"] = envelope["drift"]

    record = build_record(args, plan, REPO_ROOT, gate, extra)
    written = write_records(record, plan)
    envelope["status"] = "PROMOTED"
    envelope["exit_code"] = EXIT_OK
    envelope["record"] = record
    envelope["record_paths"] = [_repo_relative(p, REPO_ROOT)[0] for p in written]
    _emit(envelope, args)
    print(f"PROMOTE: PROMOTED {args.package.name} -> {args.slug} ({plan.file_count} files)")
    if shipped_external:
        print(
            f"  ⚠️ FORCED past {len(shipped_external)} EXTERNAL_DATA_PATH finding(s) (#461): the shipped model reads "
            "data from outside the deliverable and will not load elsewhere. Recorded in the promotion record.",
            file=sys.stderr,
        )
        for item in shipped_external:
            print(f"     {item['file']} -> {item['redacted']}", file=sys.stderr)
    for path in written:
        print(f"  record  {_repo_relative(path, REPO_ROOT)[0]}")
    return EXIT_OK


def assess(args: argparse.Namespace, envelope: dict[str, Any]) -> tuple[PackageShape | None, dict[str, Any], int]:
    """Everything that must hold BEFORE anything is written.

    Order is deliberate: shape, then content, then the gate. `--force` reaches only the GATE - the
    content checks above it are mandatory and cannot be overridden.
    """
    try:
        shape = discover_shape(args.package)
    except CannotAssess as exc:
        return None, {}, _refuse(envelope, args, "CANNOT_ASSESS", [str(exc)], EXIT_CANNOT_ASSESS)

    envelope["shape"] = "shared_datasource" if args.datasource_slug else f"model_per_{shape.kind}"
    if args.datasource_slug is not None and shape.model is None:
        findings = ["--datasource-slug given but the package holds no .SemanticModel to share"]
        return None, {}, _refuse(envelope, args, "CANNOT_ASSESS", findings, EXIT_CANNOT_ASSESS)

    source = content_checks(shape.report, shape.model, "source")
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
    """
    if shape.model is None:
        return EXIT_OK
    found = external_data_paths(shape.model, (args.package, plan.deliverable_root))
    envelope["external_data_paths"] = {
        "source": [{"file": item["file"], "path": item["redacted"]} for item in found],
        "forced": bool(found) and bool(args.force),
    }
    if not found or args.force:
        return EXIT_OK
    findings = [
        f"EXTERNAL_DATA_PATH: {item['file']} reads {item['redacted']}, which is absolute and resolves "
        f"OUTSIDE both the package and the deliverable"
        for item in found
    ]
    findings.append(
        "A promoted model reading from a gitignored, machine-local, prunable location is the "
        "structurally-present/functionally-EMPTY deliverable (#461). An absolute path also embeds a real "
        "USERNAME (scripts/set_data_folder.py --check gates that). Carry the extract INTO the package, or "
        "re-run with --force, which is recorded."
    )
    code = _refuse(envelope, args, "REFUSED_EXTERNAL_DATA_PATH", findings, EXIT_REFUSED_EXTERNAL_PATH)
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
    except CannotAssess as exc:
        return _refuse(envelope, args, "CANNOT_ASSESS", [str(exc)], EXIT_CANNOT_ASSESS)
    envelope["planned_files"] = plan.file_count

    external = _check_external_paths(args, shape, plan, envelope)
    if external != EXIT_OK:
        return external

    if args.dry_run:
        return _dry_run(args, plan, envelope)
    return _run_promotion(args, plan, envelope, gate)


def _run_promotion(
    args: argparse.Namespace, plan: PromotionPlan, envelope: dict[str, Any], gate: dict[str, Any]
) -> int:
    """Promote, mapping every failure mode onto its own exit code."""
    try:
        return _promote(args, plan, envelope, gate)
    except CannotAssess as exc:
        return _refuse(envelope, args, "CANNOT_ASSESS", [str(exc)], EXIT_CANNOT_ASSESS)
    except PromotionFailed as exc:
        return _refuse(envelope, args, "PROMOTION_FAILED", [str(exc)], EXIT_PROMOTION_FAILED)
    except OSError as exc:
        return _refuse(envelope, args, "PROMOTION_FAILED", [f"copy failed: {exc}"], EXIT_PROMOTION_FAILED)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_USAGE)
