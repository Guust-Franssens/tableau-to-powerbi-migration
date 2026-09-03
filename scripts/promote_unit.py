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
* **Re-running `check_unit.py` here is deliberate duplication.** The agent may already have run it;
  re-running costs under a second and this is the hop where a defect stops being a working copy and
  becomes a deliverable. `--force` overrides, and both the override and the observed exit code are
  written into the promotion record - an unchecked promotion must never be indistinguishable from a
  checked one afterwards.
* **`byPath` is verified against the filesystem, not against a schema.** `powerbi-report-author
  validate` returns `errorCount: 0` for a `.Report` whose `datasetReference.byPath.path` names a
  `.SemanticModel` that exists nowhere - it checks reference *shape*, not *target*
  (`.github/skills/powerbi-report-gotchas/SKILL.md` §3). A wrong one opens as a report with NO
  MODEL, which reads like a binding defect and sends the next person debugging the wrong layer.
* **Content, not existence.** On a 46-asset estate a report folder that had already passed a
  sign-off held only Desktop-local settings: no pages, no visuals, no model - and every folder that
  was supposed to exist did exist. So this asserts real pages carrying real visuals and real tables,
  at the SOURCE (fail before shipping) and again at the DESTINATION (the last hop is the one nobody
  re-checks).
* **A model may not read data from OUTSIDE the tree it is promoted into (#461).** Measured across
  the 62 packaged units of estate run 408: **22 absolute machine-local `File.Contents` references
  across 17 units (27%)**, pointing into the originating bundle's `data/` - gitignored,
  machine-local, prunable. That deliverable is the same functionally-empty artifact by a different
  route, and NO existing gate sees it: `check_unit.py` returns its normal verdicts and
  `powerbi-report-author validate` is clean. The test is *absolute AND outside*, never a match on
  `_runs` or a drive letter, so an absolute path under the deliverable's own `data/` (the
  `set_data_folder.py` convention) still promotes.
* **The model-per-workbook copy is a CONTENTS copy.** `<Unit>/fabric/` holds `<Name>.Report/` and
  `<Model>.SemanticModel/` as siblings and the deliverable has the identical shape, so copying the
  folder itself would nest them wrongly - the folder is named for the WORKBOOK while the model
  inside is named for the DATASOURCE.

Promoting FROM the package is settled (#460): the package's `fabric/` is the working copy the agent
edits, and it is a `shutil.copytree` of `<bundle>/pbip/<unit>/`, not a link. Pass `--bundle` and the
originating tree is diffed as a drift REPORT: identical means the choice did not matter yet, and a
difference means someone edited one of them. ⚠️ It is reported, never fatal, and never claims which
side is authoritative - a divergence proves divergence and nothing more (a deletion-only edit adds
no insertions; a replacement yields equal counts). The package does not record where it came from -
`package_unit.py` descopes host paths out of every shipped manifest on purpose - so `--bundle` is
the only route and its absence is recorded as `not_checked`, never as "no drift".

Exit codes
----------
| 0  | PROMOTED, or a clean `--dry-run` plan |
| 1  | REFUSED_BY_GATE: `check_unit.py` exited non-zero and `--force` was not given |
| 2  | CANNOT_ASSESS: the package cannot be read or its shape is ambiguous - never a pass |
| 3  | REFUSED_CONTENT: the source is structurally present but functionally empty |
| 4  | PROMOTION_FAILED: the copy, the `byPath` rewrite, or a post-copy verification failed |
| 5  | REFUSED_EXTERNAL_DATA_PATH: the model reads data from outside the deliverable (#461) |
| 64 | usage error |

⚠️ 2 exists so "cannot assess" can never collapse into the clean bucket, which is this repo's most
common gate defect class. An unreadable package is a blocking state, not a silent success.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
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

REPORT_SUFFIX = ".Report"
MODEL_SUFFIX = ".SemanticModel"
RECORD_NAME = "promotion-record.json"
RECORD_VERSION = 1

# A TMDL table declaration is a column-0 `table <name>` line; an indented one is a nested property.
_TMDL_TABLE_RE = re.compile(r"^table\s+\S", re.MULTILINE)

# Every double-quoted literal in a TMDL/M source block. M has no escaped quote inside a literal -
# it doubles them - so a non-greedy run between quotes is the right shape here.
_TMDL_STRING_RE = re.compile(r'"([^"\n]*)"')


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
    """The content verdict for one artifact - counts plus every reason it was refused."""

    counts: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the artifact carries real content."""
        return not self.findings


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
    stale_removals: list[Path] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        """How many files the plan copies."""
        return sum(_count_files(step.source) for step in self.steps)

    @property
    def deliverable_root(self) -> Path:
        """The `<slug>/` directory the promoted MODEL lands under.

        This, not `migrations/`, is the containment boundary for a model's data references: a
        deliverable may legitimately carry its own `data/` (the `set_data_folder.py` convention
        writes `<REPO_ROOT>\\<tree>\\<slug>\\data\\`), but it may not read out of another unit's.
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
    inside the repo is recorded relative to it and never as an absolute host path. A path outside
    the repo cannot be relativised without lying about it, so it is recorded verbatim WITH the flag
    set; a customer migration is kept out of this public repo by prefixing the slug
    (`customer-<name>`), which is what the ignore rules key on.
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


def check_report_content(report: Path) -> ContentCheck:
    """Assert `definition/pages/` enumerates real pages carrying real visuals.

    A folder count is not a content check: the precedent case had every expected folder present and
    nothing behind them.
    """
    check = ContentCheck()
    pages_dir = report / "definition" / "pages"
    if not pages_dir.is_dir():
        check.findings.append(f"{report.name}: no definition/pages/ directory")
        return check
    pages = sorted(p for p in pages_dir.iterdir() if p.is_dir() and (p / "page.json").is_file())
    visuals = [v for p in pages for v in p.glob("visuals/*/visual.json")]
    check.counts = {
        "pages": len(pages),
        "pages_with_visuals": sum(1 for p in pages if any(p.glob("visuals/*/visual.json"))),
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
    """Every double-quoted literal in a TMDL/M source block."""
    return _TMDL_STRING_RE.findall(text)


def _is_local_filesystem_path(candidate: str) -> bool:
    """Whether a literal names a LOCAL filesystem location rather than a server, a URL or a formula.

    Deliberately not keyed on a drive letter: a UNC share and a macOS path are the same defect on a
    different machine — measured on the reference estate, one workbook's model carries
    `/Users/<someone>/…/Global Superstore.xlsx`, so a Windows-only rule would have missed a real one.

    ⚠️ **A POSIX-absolute literal is ambiguous in a way a drive-absolute one is not**, and taking it
    at face value produced real false positives on the same estate: a Databricks
    `HttpPath = "/sql/1.0/warehouses/<id>"` (a live connection, perfectly promotable) and a bare
    `"/"` inside a `TableauFormula` annotation — 8 of 9 POSIX-only hits. So a POSIX candidate must
    also carry a file suffix. ❌ Named residual: a POSIX *folder* parameter is therefore missed;
    its Windows equivalent (`SourceFolder = "C:\\…\\data\\"`, which the estate does carry) is not.

    ⚠️ There is no URL guard **on purpose**. A `https://…` / `abfss://…` literal is absolute in
    neither flavour, so an explicit `"://" in candidate` early return killed no mutation: it was
    dead code that read like protection.
    """
    if PureWindowsPath(candidate).is_absolute():  # `C:\…`, `C:/…`, `\\server\share\…`
        return True
    if not candidate.startswith("/") or candidate.startswith("//"):
        return False
    return bool(PurePosixPath(candidate).suffix)


def _redact_path(candidate: str) -> str:
    """Keep only the leaf, because an absolute path embeds a real USERNAME.

    ⚠️ `migrations/**` is not blanket-gitignored and this repo is public, so the promotion record
    and the `--json` envelope must not carry the full string - `scripts/set_data_folder.py --check`
    exists to keep exactly that out of tracked files. The operator still gets the whole path on
    stderr, where it is a terminal line rather than an artifact.
    """
    leaf = PureWindowsPath(candidate.replace("/", "\\")).name
    return f"<absolute-path-redacted>\\{leaf}" if leaf else "<absolute-path-redacted>"


def external_data_paths(model: Path, allowed_roots: tuple[Path, ...]) -> list[dict[str, str]]:
    """Absolute data references in a model's TMDL that resolve OUTSIDE every allowed root.

    Issue #461, measured across the 62 packaged units of estate run 408: **22 absolute
    machine-local `File.Contents` references across 17 units (27%)**, all pointing into the
    originating bundle's `data/` - which is gitignored, machine-local and prunable. A deliverable
    whose model reads from there is the *"structurally present and functionally EMPTY"* artifact
    `powerbi-report-gotchas` §3 records from a real 46-asset estate: every folder exists, nothing
    loads.

    ⚠️ **No existing gate sees this.** `check_unit.py` returns its page-parity and oracle verdicts
    on such a unit and `powerbi-report-author validate` is clean on it, so this is new coverage
    rather than a second opinion. It is judged as *absolute AND not under the destination*, never
    by matching `_runs` or a drive letter - an absolute path under the deliverable's own `data/`
    (the `set_data_folder.py` convention) is legitimate and must keep promoting.
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
    such as `\\\\fileserver\\share\\x.csv` blocks on SMB name resolution for a host that does not
    exist; measured here, that turned a millisecond check into a multi-minute stall. It would also
    make the answer depend on what happens to be mounted, when the question is only *where does
    this model point*. A path that does not exist is still outside, which is the fail-closed answer
    a pruned bundle `data/` needs.
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
    """Decide every copy, the `byPath` value, and which stale artifact a re-run must clear."""
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
        stale_removals=[stale] if stale.exists() else [],
    )


def execute_plan(plan: PromotionPlan) -> None:
    """Perform the copies. Idempotent: an existing destination artifact is replaced wholesale."""
    for stale in plan.stale_removals:
        shutil.rmtree(stale)
    for step in plan.steps:
        step.destination.parent.mkdir(parents=True, exist_ok=True)
        if step.source.is_dir():
            if step.destination.exists():
                shutil.rmtree(step.destination)
            shutil.copytree(step.source, step.destination)
        else:
            shutil.copy2(step.source, step.destination)


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


def verify_bypath(report: Path) -> dict[str, Any]:
    """Resolve `definition.pbir`'s `byPath` ON DISK, relative to the `.Report` folder.

    ⚠️ Not optional, and not covered by anything else: `powerbi-report-author validate` returns
    `errorCount: 0` for a `.Report` whose `byPath` names a `.SemanticModel` that exists nowhere. It
    checks reference shape, not target. A target is only accepted when it is a directory holding a
    `definition/` - an empty folder of the right name is not a model.
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
    target = (report / declared).resolve()
    resolves = target.is_dir() and (target / "definition").is_dir()
    if not resolves:
        raise PromotionFailed(
            f"{report.name}: byPath {declared!r} does not resolve to a semantic model on disk "
            f"(a wrong byPath opens as a report with NO MODEL and validates clean)"
        )
    return {"path": declared, "resolves": True}


def tree_drift(package_fabric: Path, bundle_unit: Path | None) -> dict[str, Any]:
    """Report whether the package's working copy has diverged from its originating bundle tree.

    ⚠️ Divergence ONLY. This never says which side is authoritative and never fails the promotion:
    a deletion-only edit produces no extra insertions and a replacement produces equal counts, so
    no count here identifies the edited side. `not_checked` when no `--bundle` was given, which is
    deliberately distinct from "no drift".
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
        "engine_version": _engine_version(args.package),
        "check_unit": gate,
        "forced": bool(args.force),
        "copied": copied,
        **extra,
    }


def write_records(record: dict[str, Any], plan: PromotionPlan) -> list[Path]:
    """Write the record beside every deliverable this promotion touched.

    Both halves of a split promotion get one, because either can be found on its own months later
    and each has to be able to answer the question by itself.
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
    if not args.slug.strip():
        parser.error("--slug must not be empty")
    if args.datasource_slug is not None and not args.datasource_slug.strip():
        parser.error("--datasource-slug must not be empty")
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
    can legitimately be copied or renamed (the reference estate has an `e2e-<Unit>` working copy),
    and keying the lookup on the folder name silently reports `not_checked` for it.
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
    """Do the copy and everything that must be true afterwards."""
    execute_plan(plan)
    extra: dict[str, Any] = {}
    if plan.report_destination is not None and plan.bypath is not None:
        extra["bypath_rewrite"] = rewrite_bypath(plan.report_destination, plan.bypath)
    if plan.report_destination is not None:
        extra["bypath_verified"] = verify_bypath(plan.report_destination)

    shipped = content_checks(plan.report_destination, plan.model_destination, "shipped")
    if not shipped.ok:
        raise PromotionFailed("; ".join(shipped.findings))
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

    Order is deliberate: shape, then content, then the gate. An unreadable or ambiguous package is
    reported as `CANNOT_ASSESS` rather than as a gate finding, because "I could not tell" and "I
    checked and it failed" need different responses from whoever reads the exit code.
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
    if not source.ok:
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

    Scanned at the SOURCE so a refusal ships nothing at all, and again after the copy in
    `_promote` - verifying a fix in the working copy proves nothing about `migrations/**/fabric/`.
    Both the package and the deliverable count as allowed roots: after the packaging half of #461
    lands, the extract travels INSIDE the package, and `set_data_folder.py`'s convention puts a
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
        "A promoted model that reads from a gitignored, machine-local, prunable location is the "
        "structurally-present/functionally-EMPTY deliverable (#461). An absolute path also embeds a real "
        "USERNAME, and this repo gates against that in tracked files (scripts/set_data_folder.py --check). "
        "Fix the packaging (carry the extract INTO the package) or re-run with --force, which is recorded."
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
    plan = build_plan(shape, args.migrations_root, args.slug, args.datasource_slug)
    envelope["planned_files"] = plan.file_count

    external = _check_external_paths(args, shape, plan, envelope)
    if external != EXIT_OK:
        return external

    if args.dry_run:
        return _dry_run(args, plan, envelope)

    try:
        return _promote(args, plan, envelope, gate)
    except PromotionFailed as exc:
        return _refuse(envelope, args, "PROMOTION_FAILED", [str(exc)], EXIT_PROMOTION_FAILED)
    except OSError as exc:
        return _refuse(envelope, args, "PROMOTION_FAILED", [f"copy failed: {exc}"], EXIT_PROMOTION_FAILED)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_USAGE)
