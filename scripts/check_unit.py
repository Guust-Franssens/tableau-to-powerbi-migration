"""
purpose: answer whether one migration unit is done by aggregating existing gates without merging them.
usage:   python scripts/check_unit.py <unit-or-bundle> [--scope {model,report,integration,all}] [--json <file>]
         [--reference-dir <dir>] [--oracle-dir <dir>]

Exit codes are intentionally coarser than the native gates, while preserving each native exit in the
JSON payload:

| 0  | AUTOMATED_CHECKS_PASS: all automated checks in the selected scope are clean |
| 1  | at least one finding remains in the selected scope |
| 2  | one or more selected checks could not be fully checked (SKIPPED/ERROR/NOT_CHECKED) and no finding won |
| 4  | page-count parity precondition failed; page-level oracle checks are not meaningful |
| 64 | usage error |

The command is a facade, not a merge: the existing gates remain independently runnable and their
native statuses/exit codes are recorded under ``checks[].native_*``. Page parity and oracle coverage
live here because no existing gate owned those questions; path ceiling lives here because remembering
a nineteenth ``check_*.py`` by name is the problem this facade exists to remove.
"""

# Unit gate, brownfield inventory, and CLI rendering intentionally live together as one facade.
# pylint: disable=too-many-lines
from __future__ import annotations

import argparse
import json
import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

import check_desktop_orphans as check_desktop_orphans_module
import object_identity as oid
import read_handover
from bundle_corpus import evidence_dirs, shipping_models, shipping_reports
from check_field_bindings import model_for_report

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent

STATUS_PASS = "PASS"
STATUS_AUTOMATED_PASS = "AUTOMATED_CHECKS_PASS"
STATUS_FINDINGS = "FINDINGS"
STATUS_NOT_CHECKED = "NOT_CHECKED"
STATUS_PRECONDITION_FAILED = "PRECONDITION_FAILED"

# A model check that is deferred to another unit is neither a PASS nor a missing input. It is tagged
# so the summary bucket and _is_blocking_not_checked can tell "model lives elsewhere, by design" apart
# from "this unit has no model" - the two demand opposite actions (issue #317).
VERIFICATION_CLAIMED_ONLY = "CLAIMED_ONLY"
VERIFICATION_EXTERNAL = "EXTERNAL"

# Where a report unit's semantic model actually lives, resolved via definition.pbir byPath.
MODEL_LOC_LOCAL = "LOCAL"
MODEL_LOC_EXTERNAL = "EXTERNAL"
MODEL_LOC_BROKEN = "BROKEN"
MODEL_LOC_NONE = "NONE"

MODEL_REFERENCE_ID = "model-reference"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_NOT_CHECKED = 2
EXIT_PRECONDITION_FAILED = 4
EXIT_USAGE = 64

EXEMPTIONS_FILE = "unit-check-exemptions.json"
VALID_EXEMPTION_CHECKS = frozenset({"stub-measures", "page-parity", "scaffold-partitions"})

# The engine's ZERO-PAGE CRASH GUARD page. When every candidate is dropped, twb_to_pbir.py:14719-14727
# emits ONE synthetic, visual-less page - `_sanitize("page-empty")` with displayName
# `_EMPTY_REPORT_PAGE_NAME` (:13091) - because a PBIR with an empty `pageOrder` crashes Power BI
# Desktop on open. It is a DECLARED extra with no Tableau counterpart, so page parity must not count
# it as an unexplained extra page. Measured on a real 2.339.0 estate run: 2 of 44 workbooks ship one.
ENGINE_PLACEHOLDER_PAGE_NAME = "No visuals rebuilt"
ENGINE_PLACEHOLDER_PAGE_ID_PREFIX = "page-empty"

# Where a dropped page's declared reason is read from. NOT pbip_warnings[]: that list carries bare
# prefixed strings with no scope/name, so a warning there cannot be attributed to a page.
DROP_EXPLANATION_SOURCE = "handover viz_fidelity[]"

# The ONLY structured statement that a candidate produced no page, and it DIAGNOSES the omission -
# it never approves it. migrate_estate._fidelity_tier defines `empty` as "no faithful visual emitted";
# `degraded` is "a rendered visual whose warning is a genuine degradation" and therefore asserts the
# OPPOSITE. `status: "warned"` spans both and `evidence: "emitted+linted"` appears on 45 empty-tier
# rows in one estate run, so neither can decide this.
DROP_EVIDENCE_TIER = "empty"

# ...and an empty-tier row can only ever be a WORKSHEET. `_fidelity_tier` returns `empty` iff
# `visual_type in (None, "unsupported")`, and `_viz_fidelity` gives `visual_type` a real visual type
# only for rows it built from `ir["worksheets"]`; a DASHBOARD-scope row gets `visual_type:
# "dashboard"` and is never `empty`. Measured across a 2.339.0 estate run: all 46 empty-tier rows
# carry `visual_type: "unsupported"`, and no dashboard/filter/workbook-scope row ever does. Requiring
# BOTH is what stops a worksheet's evidence settling a same-named dashboard's absence.
DROP_EVIDENCE_VISUAL_TYPE = "unsupported"

# How an omission was accounted for. Only ACCEPTED_SIGNED lets the gate pass: engine evidence explains
# an omission, a human accepts one.
OMISSION_SIGNED = "accepted-signed"
OMISSION_SOURCE_EMPTY = "no-source-content"
OMISSION_DECLARED = "declared-by-engine-unsigned"
OMISSION_UNEXPLAINED = "unexplained"
OMISSION_AMBIGUOUS = "cannot-establish"

#: The dispositions that owe no Power BI output - and therefore no oracle evidence either. Named once
#: so page parity and the oracle denominator cannot disagree about it, which they did in round 4.
OMISSIONS_OWING_NOTHING = frozenset({OMISSION_SIGNED, OMISSION_SOURCE_EMPTY})

# Exemption dispositions. Only APPLIED is an accepted compromise; the other two did nothing and must
# not be counted as one.
EXEMPTION_APPLIED = "applied"
EXEMPTION_STALE = "stale"
EXEMPTION_AMBIGUOUS = "ambiguous"

# Oracle capture directory names, both of them documented and both real on disk: the tool's own
# `--out _oracle` convention (AGENTS.md "capture_tableau_oracle.py --out _oracle";
# docs/operator-runbook.md) and the canonical per-run layout `_runs/<NNN>-<slug>/oracle/`
# (scripts/work_dirs.py CANONICAL_SUBDIRS; AGENTS.md "Canonical work layout").
ORACLE_DIR_NAMES = ("_oracle", "oracle")

SCOPE_MODEL = "model"
SCOPE_REPORT = "report"
SCOPE_INTEGRATION = "integration"
SCOPE_ALL = "all"
SCOPES = (SCOPE_MODEL, SCOPE_REPORT, SCOPE_INTEGRATION, SCOPE_ALL)
MODEL_CHECK_IDS = frozenset(
    {
        "scaffold-partitions",
        "sqlproxy-connections",
        "relationship-health",
        "data-model",
        "empty-model",
        "stub-measures",
        "ai-descriptions",
        "ai-instructions",
        "cache-freshness",
    }
)
REPORT_CHECK_IDS = frozenset({"pbir-valid", "pbir-layout", "page-parity", "oracle-coverage", "occlusion"})
INTEGRATION_CHECK_IDS = frozenset({"blank-placeholders", "field-bindings", "connection-fidelity"})
ALL_ONLY_CHECK_IDS = frozenset(
    {"engine-receipt", "desktop-orphans", "path-ceiling", "visual-layer-done", "visual-comparison-done", "finalized"}
)

OWNER_HINTS = {
    "blank-placeholders": "integration (model placeholder referenced by report)",
    "field-bindings": "integration (report reference vs model field)",
    MODEL_REFERENCE_ID: "integration (report -> semantic model reference)",
    "connection-fidelity": "integration (spec connection target vs emitted model M)",
    "scaffold-partitions": "model",
    "sqlproxy-connections": "model",
    "relationship-health": "model",
    "data-model": "model",
    "empty-model": "model",
    "stub-measures": "model",
    "ai-descriptions": "model",
    "ai-instructions": "model",
    "cache-freshness": "model",
    "pbir-valid": "report",
    "pbir-layout": "report",
    "page-parity": "report",
    "oracle-coverage": "report/reference capture",
    "occlusion": "report",
    "engine-receipt": "orchestrator",
    "desktop-orphans": "orchestrator",
    "path-ceiling": "orchestrator (install root length + engine-side name duplication; not a layer defect)",
}


@dataclass(frozen=True)
class Gate:  # pylint: disable=too-many-instance-attributes
    """One existing script behind the unit facade."""

    check_id: str
    script: str
    args: tuple[str, ...]
    pass_statuses: frozenset[str]
    pass_exit_codes: frozenset[int]
    finding_statuses: frozenset[str]
    finding_exit_codes: frozenset[int]
    not_checked_statuses: frozenset[str] = frozenset({"SKIPPED", "ERROR"})
    not_checked_exit_codes: frozenset[int] = frozenset({2, 3})
    writes_json: bool = True


GATES = (
    Gate(
        "blank-placeholders",
        "check_blank_placeholders.py",
        (),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"REFERENCED", "UNREFERENCED"}),
        frozenset({1, 2}),
        frozenset({"INCOMPLETE"}),
        frozenset({3}),
    ),
    Gate(
        "field-bindings",
        "check_field_bindings.py",
        (),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"UNRESOLVED", "INCOHERENT"}),
        frozenset({1}),
        frozenset({"SKIPPED", "ERROR"}),
        frozenset({0, 2, 3}),
    ),
    Gate(
        "connection-fidelity",
        "check_connection_fidelity.py",
        (),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"DOWNGRADED"}),
        frozenset({1}),
        frozenset({"SKIPPED"}),
        frozenset({3}),
    ),
    Gate(
        "sqlproxy-connections",
        "check_sqlproxy_connections.py",
        (),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"SQLPROXY"}),
        frozenset({1}),
    ),
    Gate(
        "relationship-health",
        "check_relationship_health.py",
        (),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"MISSING_RELATIONSHIP"}),
        frozenset({1}),
    ),
    Gate(
        "data-model",
        "check_datamodel.py",
        (),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"FINDINGS"}),
        frozenset({1}),
        frozenset({"ERROR"}),
        frozenset({1}),
        False,
    ),
    Gate(
        "empty-model",
        "check_empty_model.py",
        (),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"EMPTY_MODELS"}),
        frozenset({1, 5}),
        frozenset({"SKIPPED"}),
        frozenset({3}),
    ),
    Gate(
        "pbir-valid",
        "check_pbir_valid.py",
        (),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"INVALID"}),
        frozenset({1}),
        frozenset({"SKIPPED", "ERROR"}),
        frozenset({0, 2, 3}),
    ),
    Gate(
        "pbir-layout",
        "check_pbir_layout.py",
        (),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"DISPLACED_MAIN_COLUMN"}),
        frozenset({1}),
    ),
    # Deliberate: the native census is non-gating by default for mid-migration use, but check_unit is
    # the completion facade. A unit claimed ready must not silently carry unresolved BLANK() stubs.
    Gate(
        "stub-measures",
        "check_stub_measures.py",
        ("--strict",),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"STUBS"}),
        frozenset({1}),
    ),
    # Whole-unit shippability, so `all` scope only (see ALL_ONLY_CHECK_IDS): the scan walks the entire
    # target tree and cannot be attributed to a layer - a model-scoped run would be judging report
    # paths and vice versa. Kept GATING because the native gate already exits 1 and the facade must
    # never be the one place a bundle Desktop cannot open reads as done; "not fixable by the persona
    # that hit it" is answered by the orchestrator owner hint, not by silence. Its statuses are
    # lowercase, unlike every other gate here - that is the native contract, not a typo.
    Gate(
        "path-ceiling",
        "check_path_ceiling.py",
        (),
        frozenset({"ok"}),
        frozenset({0}),
        frozenset({"over_ceiling"}),
        frozenset({1}),
        frozenset({"unknown_paths", "no_paths", "ERROR"}),
        frozenset({2, 3}),
    ),
)


_T = TypeVar("_T")


def _slug(text: str) -> str:
    """Lossy name normalization for matching Tableau/PBIR/oracle page labels.

    ⚠️ **Deliberately lossy, therefore never a decision on its own.** Every defect this gate has
    shipped across seven review rounds is the same shape: a lossy key settled a question without
    anyone asking *how many things answer to it*. Round 6 answered that with a census of `_slug`
    CALL SITES; round 7 proved the census vacuous, and named why in one sentence worth keeping:

        *a census that pins WHERE a function is called cannot prove HOW its result is used.*

    So the guarantee no longer lives in a test that inspects call sites - it lives in
    :class:`NormalizedIndex`, whose only accessor is a cardinality check. Call this function outside
    that class **only** for reporting text, and only from a site the census allowlists.
    """
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


class NormalizedIndex(Generic[_T]):
    """Candidates grouped by a lossy key, readable ONLY through a cardinality check.

    This is the "follow the value" fix. A syntactic audit of `_slug()` call sites could not prove
    the property it claimed: it accepted any unrelated ``== 1`` in the same function as a guard, it
    could not see a ``set()`` collapse one line later, and a reporting-only value could be promoted
    into a decision without touching a single pinned line. Rather than chase the value through
    arbitrary Python - undecidable in general - the value is made **inaccessible except through the
    guard**:

    * candidates are stored as a LIST per key, so multiplicity cannot be lost on the way in;
    * :meth:`unique` is the only way to get one out, and it returns ``None`` for 0 **and** for >1;
    * there is no accessor that returns the bucket, so a caller cannot take ``[0]`` itself.

    ``count`` exists for reporting a collision honestly ("2 producer records are named X"); it
    returns an ``int`` and so cannot be mistaken for the candidate.
    """

    def __init__(self) -> None:
        self.__buckets: dict[str, list[_T]] = {}

    def add(self, name: str, value: _T) -> None:
        """Record one candidate under ``name``'s lossy key, keeping earlier ones."""
        self.__buckets.setdefault(_slug(name), []).append(value)

    def add_spelling(self, name: str, value: _T) -> None:
        """Record ANOTHER spelling of a candidate already added.

        Idempotent per key, so several aliases of ONE finding never look like several candidates -
        while two genuinely different findings sharing a key still do. Without this the alias set
        of a single exemption target would contest itself.
        """
        bucket = self.__buckets.setdefault(_slug(name), [])
        if value not in bucket:
            bucket.append(value)

    def count(self, name: str) -> int:
        """How many candidates answer to ``name``'s lossy key. Reporting only."""
        return len(self.__buckets.get(_slug(name), []))

    def unique(self, name: str) -> _T | None:
        """The single candidate for ``name``'s lossy key, or ``None`` when 0 or more than 1."""
        matches = self.__buckets.get(_slug(name), [])
        return matches[0] if len(matches) == 1 else None


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _unit_dir(target: Path) -> Path:
    """Return the migration unit directory when ``target`` is its fabric folder or bundle."""
    target = target.resolve()
    return target.parent if target.name == "fabric" else target


def _migration_spec(target: Path) -> Path | None:
    """Find the parser spec that declares Tableau dashboards, if this target has one."""
    unit = _unit_dir(target)
    candidates = [unit / "migration-spec.json", target / "migration-spec.json"]
    return next((path for path in candidates if path.is_file()), None)


EXPECTED_BUNDLE_SHAPE = (
    "expected a migration unit or engine bundle shaped as one of:",
    "  - unit root: migration-spec.json plus fabric/<Name>.Report and/or fabric/<Name>.SemanticModel",
    "  - engine bundle root: report.json or engine-output-receipt.json plus "
    "{pbip,reports,semantic_models,handover,data}",
    "  - direct artifact: <Name>.Report/definition or <Name>.SemanticModel/definition",
    "  - partial unit: any subset of those phases, reported as evidenced rather than failed",
)


def _relative(path: Path, root: Path) -> str:
    """Display a target-local path where possible, preserving actual paths for action."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _is_under_named_dir(path: Path, root: Path, name: str) -> bool:
    try:
        return name in path.relative_to(root).parts[:-1]
    except ValueError:
        return False


def _json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_engine_report(path: Path) -> bool:
    payload = _json_object(path)
    return bool(isinstance(payload, dict) and isinstance(payload.get("workbooks"), list))


def _engine_version(path: Path) -> str | None:
    payload = _json_object(path)
    engine = payload.get("engine") if isinstance(payload, dict) else None
    if isinstance(engine, dict) and engine.get("version"):
        return str(engine["version"])
    return None


def _is_handover_slice(path: Path) -> bool:
    payload = _json_object(path)
    return bool(isinstance(payload, dict) and ("workbook" in payload or "workbooks" in payload or "estate" in payload))


def _artifact_dirs(target: Path, suffix: str) -> list[Path]:
    return sorted(
        {path.resolve() for path in target.rglob(f"*{suffix}") if path.is_dir() and (path / "definition").is_dir()},
        key=str,
    )


def _phase_row(name: str, paths: list[Path], target: Path, missing_text: str) -> dict[str, Any]:
    if paths:
        return {"phase": name, "status": "EVIDENCED", "paths": [_relative(path, target) for path in paths[:8]]}
    return {"phase": name, "status": "NOT_EVIDENCED", "detail": missing_text, "paths": []}


def _plan_target_for_artifact(target: Path, artifact: Path) -> Path:
    unit_name = artifact.parent.name if artifact.parent != target else artifact.stem
    return target / "pbip" / unit_name / artifact.name


def _brownfield_plan(target: Path, inventory: dict[str, list[Path]]) -> list[str]:
    lines: list[str] = []
    for marker in inventory["engine_reports"] + inventory["receipts"]:
        if marker.parent == target:
            continue
        destination = target / marker.name
        lines.append(f"engine truth: {marker} -> {destination}")
    for handover in inventory["handovers"]:
        if _is_under_named_dir(handover, target, "handover"):
            continue
        lines.append(f"handover queue: {handover} -> {target / 'handover' / handover.name}")
    for spec in inventory["specs"]:
        if spec.parent == target:
            continue
        lines.append(f"source intent: {spec} -> {target / 'migration-spec.json'}")
    for artifact in inventory["reports"] + inventory["models"]:
        if _is_under_named_dir(artifact, target, "pbip"):
            continue
        if _is_under_named_dir(artifact, target, "fabric"):
            continue
        lines.append(f"working copy: {artifact} -> {_plan_target_for_artifact(target, artifact)}")
    return lines


@dataclass(frozen=True)
class ModelLocation:
    """Where a report unit's semantic model actually lives, resolved via ``definition.pbir``."""

    state: str
    model_path: Path | None = None
    ds_unit: Path | None = None
    declared: str | None = None
    report: Path | None = None


def _display_path(path: Path | None) -> str:
    """Repo-relative, POSIX-style rendering so paths outside the target stay portable in output."""
    if path is None:
        return "<unknown>"
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _declared_by_path(report_dir: Path) -> str | None:
    """The declared ``datasetReference.byPath.path`` string, read WITHOUT resolving it on disk.

    Resolution (does the path point at a real model folder?) is left to
    ``check_field_bindings.model_for_report`` - the single resolver that already works. This reads only
    whether a reference was *declared*, which is what tells a dangling reference apart from no model.
    """
    payload = _json_object(report_dir / "definition.pbir")
    if payload is None:
        return None
    by_path = payload.get("datasetReference", {}).get("byPath", {})
    rel = by_path.get("path") if isinstance(by_path, dict) else None
    return rel.strip() if isinstance(rel, str) and rel.strip() else None


def _ds_unit_for_model(model_path: Path) -> Path:
    """The migration unit that owns a model: the parent of its ``fabric/`` folder, else its parent."""
    parent = model_path.parent
    return parent.parent if parent.name == "fabric" else parent


def _model_location(target: Path) -> ModelLocation:
    """Classify where this unit's model is: local, external (shared datasource), broken, or absent.

    A local model (the ordinary per-workbook or datasource-only shape) short-circuits to ``LOCAL`` so
    nothing downstream changes for it. Otherwise each shipping report is resolved through
    ``model_for_report``; a model that resolves OUTSIDE the target is ``EXTERNAL`` (correctly-split
    shared datasource), a declared byPath that resolves to nothing is ``BROKEN`` (a genuine defect),
    and a report with no reference and no sibling contributes ``NONE``.
    """
    target = target.resolve()
    if shipping_models(target, include_standalone=True):
        return ModelLocation(MODEL_LOC_LOCAL)
    external: tuple[Path, Path] | None = None
    broken: tuple[str, Path] | None = None
    for report in shipping_reports(target):
        resolved = model_for_report(report)
        if resolved is not None:
            if not _path_within(resolved, target):
                external = external or (resolved.resolve(), report)
            continue
        declared = _declared_by_path(report)
        if declared is not None:
            broken = broken or (declared, report)
    if external is not None:
        model_path, report = external
        return ModelLocation(
            MODEL_LOC_EXTERNAL, model_path=model_path, ds_unit=_ds_unit_for_model(model_path), report=report
        )
    if broken is not None:
        return ModelLocation(MODEL_LOC_BROKEN, declared=broken[0], report=broken[1])
    return ModelLocation(MODEL_LOC_NONE)


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _external_next_command(loc: ModelLocation) -> str:
    unit = loc.ds_unit or (loc.model_path.parent if loc.model_path else None)
    return f"python scripts/check_unit.py {_display_path(unit)} --scope model"


def _external_headline(loc: ModelLocation) -> str:
    where = _display_path(loc.model_path) if loc.model_path else (loc.declared or "?")
    return f"model is EXTERNAL (shared datasource) at {where} - check it with: {_external_next_command(loc)}"


def _external_model_check(check_id: str, loc: ModelLocation) -> dict[str, Any]:
    """A model check deferred to the datasource unit: not a pass, not a missing input (issue #317)."""
    return {
        "id": check_id,
        "status": STATUS_NOT_CHECKED,
        "verification": VERIFICATION_EXTERNAL,
        "detail": _external_headline(loc),
    }


def _model_reference_check(loc: ModelLocation) -> dict[str, Any]:
    """One row that states the report -> model reference verdict for a unit with no local model."""
    if loc.state == MODEL_LOC_EXTERNAL:
        return {
            "id": MODEL_REFERENCE_ID,
            "status": STATUS_NOT_CHECKED,
            "verification": VERIFICATION_EXTERNAL,
            "detail": _external_headline(loc),
            "model_path": _display_path(loc.model_path),
            "report": _display_path(loc.report),
        }
    return {
        "id": MODEL_REFERENCE_ID,
        "status": STATUS_FINDINGS,
        "detail": (
            f"report definition.pbir byPath does not resolve: '{loc.declared}' "
            f"(report {_display_path(loc.report)}) - fix the reference or migrate the datasource first"
        ),
        "declared": loc.declared,
        "report": _display_path(loc.report),
    }


def inspect_brownfield(target: Path) -> dict[str, Any]:
    """Read-only inventory for migration output whose layout may not match this toolkit."""
    target = target.resolve()
    engine_reports = [path for path in sorted(target.rglob("report.json"), key=str) if _is_engine_report(path)]
    receipts = sorted(target.rglob("engine-output-receipt.json"), key=str)
    specs = sorted(target.rglob("migration-spec.json"), key=str)
    handovers = [
        path
        for path in sorted(target.rglob("*.json"), key=str)
        if path.parent.name == "handover" and _is_handover_slice(path)
    ]
    inventory = {
        "engine_reports": engine_reports,
        "receipts": receipts,
        "specs": specs,
        "handovers": handovers,
        "reports": _artifact_dirs(target, ".Report"),
        "models": _artifact_dirs(target, ".SemanticModel"),
    }
    found_count = sum(len(paths) for paths in inventory.values())
    canonical_markers = [path for path in engine_reports + receipts if path.parent == target]
    canonical_pbip = any(
        _is_under_named_dir(path, target, "pbip") for path in inventory["reports"] + inventory["models"]
    )
    direct_artifact = target.name.endswith((".Report", ".SemanticModel")) and (target / "definition").is_dir()
    recognized = bool(
        canonical_markers or canonical_pbip or (target / "migration-spec.json").is_file() or direct_artifact
    )
    model_loc = _model_location(target)
    return {
        "expected_shape": list(EXPECTED_BUNDLE_SHAPE),
        "recognized_target_shape": recognized,
        "found_count": found_count,
        "engine_versions": sorted({version for path in receipts if (version := _engine_version(path))}),
        "phases": [
            _phase_row("source intent", specs, target, "no migration-spec.json found"),
            _phase_row(
                "engine bundle marker", engine_reports + receipts, target, "no engine report.json or receipt found"
            ),
            _phase_row("handover queue", handovers, target, "no handover/*.json slices found"),
            _phase_row("PBIR reports", inventory["reports"], target, "no *.Report/definition folders found"),
            _semantic_model_phase(inventory["models"], target, model_loc),
        ],
        "plan": _brownfield_plan(target, inventory),
    }


def _semantic_model_phase(models: list[Path], target: Path, model_loc: ModelLocation) -> dict[str, Any]:
    """Report a locally-shipped model as EVIDENCED, an external one as EVIDENCED (external) (issue #317)."""
    if models:
        return _phase_row("semantic models", models, target, "no *.SemanticModel/definition folders found")
    if model_loc.state == MODEL_LOC_EXTERNAL:
        return {
            "phase": "semantic models",
            "status": "EVIDENCED (external)",
            "paths": [_display_path(model_loc.model_path)],
        }
    return _phase_row("semantic models", models, target, "no *.SemanticModel/definition folders found")


def _zone_worksheet_ids(zone: Any, found: set[str]) -> None:
    """Collect every worksheet id a dashboard zone tree references, at any nesting depth.

    ``parse_tableau._parse_zone`` stamps ``zone["worksheet_id"]`` from ``{ws["name"]: ws["id"]}``
    (scripts/parse_tableau.py:1027 + :1096), so the value joins directly to a worksheet's ``id``.
    """
    if isinstance(zone, dict):
        worksheet_id = zone.get("worksheet_id")
        if isinstance(worksheet_id, str) and worksheet_id:
            found.add(worksheet_id)
        _zone_worksheet_ids(zone.get("children"), found)
    elif isinstance(zone, list):
        for child in zone:
            _zone_worksheet_ids(child, found)


def _source_content_shape() -> tuple[frozenset[str], frozenset[str]] | None:
    """``(worksheet keys, encoding channels)`` the COMMITTED spec schema declares, or None.

    Read from ``docs/migration-spec.schema.json`` rather than enumerated here. An open enumeration
    inside the gate is what makes "is this worksheet empty?" undecidable: a channel nobody listed is
    silently ignored, and a partial structure reads as proof of emptiness. Deriving the closed set
    from the schema means a channel added there tightens this rule automatically instead of opening
    a hole in it.
    """
    try:
        schema = _read_json(REPO_ROOT / "docs" / "migration-spec.schema.json")
    except (OSError, json.JSONDecodeError):
        return None
    worksheet = ((schema.get("properties") or {}).get("worksheets") or {}).get("items") or {}
    worksheet_keys = worksheet.get("properties") or {}
    encodings = (worksheet_keys.get("encodings") or {}).get("properties") or {}
    if not worksheet_keys or not encodings:
        return None
    return frozenset(worksheet_keys), frozenset(encodings)


#: Worksheet properties that describe the sheet rather than anything it draws. Everything else the
#: schema declares is treated as CONTENT, so a property added upstream makes this rule stricter, not
#: looser - a new channel is content until someone deliberately says otherwise.
NON_CONTENT_WORKSHEET_KEYS = frozenset({"id", "name", "mark_type", "data_source_ids", "encodings"})

#: The spec version this classification was derived against. A different version cannot be classified.
SOURCE_EMPTY_SPEC_VERSION = "1.0"


def _source_empty(item: dict[str, Any], shape: tuple[frozenset[str], frozenset[str]] | None) -> bool:
    """Whether a spec worksheet PROVES it declares nothing to render.

    Proof needs the structure to be COMPLETE, not merely to look empty where it was populated. Round
    4 measured the weaker predicate accepting three things it should not: a Text worksheet whose
    ``title_text`` reads "Important instructions" (a visible channel it never inspected), a partial
    ``encodings: {"rows": []}``, and a worksheet with no ``filters`` key at all. Because this
    disposition needs no signature, every false positive was a silent PASS.

    So: the worksheet must carry exactly the schema's key set, ``encodings`` must carry exactly the
    schema's channels, and every content-bearing key must be falsy. Anything else - an unknown key, a
    missing one, an unreadable schema - is NOT proof, and the omission stays a rebuild gap.

    Measured against the estate: both genuinely-empty sheets (``Meridian Multi-Source (3 systems)``
    ``/Probe Sheet``, ``vishnu_dashboard/Sheet 3``) carry all eleven schema properties with every
    content channel falsy, so completeness costs nothing on real parser output.
    """
    if shape is None:
        return False
    worksheet_keys, encoding_keys = shape
    if set(item) != worksheet_keys:
        return False
    encodings = item.get("encodings")
    if not isinstance(encodings, dict) or set(encodings) != encoding_keys or any(encodings.values()):
        return False
    return not any(item[key] for key in worksheet_keys - NON_CONTENT_WORKSHEET_KEYS)


def _named_spec_pages(
    items: Any, collection: str, kind: str, shape: tuple[frozenset[str], frozenset[str]] | None
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Normalize a required migration-spec collection to identified page rows, or refuse it.

    ``docs/migration-spec.schema.json`` requires ``dashboards`` and ``worksheets`` as ARRAYS whose
    entries each carry ``id`` and ``name``. A malformed shape is refused rather than skipped: quietly
    narrowing a required collection produces a SMALLER expected set that the rest of the gate then
    trusts, which is the circular-denominator defect one layer earlier (a spec whose ``worksheets``
    was an object graded PASS with ``grade=validation-grade``).

    Each row carries its KIND, because a dashboard and a worksheet can share a name and evidence for
    one must never settle the other.
    """
    if not isinstance(items, list):
        return None, (
            f"migration-spec.json '{collection}' is {type(items).__name__}, not the array the schema "
            "requires (docs/migration-spec.schema.json), so the expected page set cannot be derived"
        )
    pages: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            return None, f"migration-spec.json '{collection}' entry #{index} is not an object"
        name = item.get("name") or item.get("title") or item.get("id")
        identity = oid.ObjectIdentity.from_engine(kind, name if isinstance(name, str) else None)
        if identity is None:
            return None, f"migration-spec.json '{collection}' entry #{index} has no usable name"
        pages.append(
            {
                "id": str(item.get("id") or identity.name),
                "name": identity.name,
                "kind": kind,
                "source_empty": kind == oid.KIND_WORKSHEET and _source_empty(item, shape),
            }
        )
    return pages, None


def _spec_payload(target: Path) -> tuple[dict[str, Any] | None, str | None]:
    """``(spec object, refusal reason)`` - the file exists, parses, and declares both required arrays."""
    spec_path = _migration_spec(target)
    if spec_path is None:
        return None, (
            "no migration-spec.json found, so the expected Tableau page set is unknown; "
            "produce one with scripts/parse_tableau.py <workbook> -o <unit>/migration-spec.json"
        )
    try:
        payload = _read_json(spec_path)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"migration-spec.json could not be read ({exc})"
    if not isinstance(payload, dict):
        return None, "migration-spec.json is not a JSON object"
    missing = [name for name in ("dashboards", "worksheets") if name not in payload]
    if missing:
        return None, (
            f"migration-spec.json has no {' or '.join(repr(name) for name in missing)} array; the "
            "schema requires it, so this file cannot be used as the expected page set"
        )
    return payload, None


def _placed_worksheet_ids(dashboards: list[Any]) -> tuple[set[str], str | None]:
    """Worksheet ids any dashboard zone tree places, or a refusal when a tree cannot be walked."""
    placed: set[str] = set()
    for index, dashboard in enumerate(dashboards, 1):
        zones = dashboard.get("zones")
        if zones is not None and not isinstance(zones, (dict, list)):
            return placed, (
                f"migration-spec.json dashboard entry #{index} has a '{type(zones).__name__}' zone "
                "tree, which cannot be walked, so placed worksheets cannot be identified"
            )
        _zone_worksheet_ids(zones, placed)
    return placed, None


def _spec_pages(target: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    """``(candidate pages, refusal reason)`` from the unit's migration spec.

    Exactly one of the two is ever set. Every refusal path is explicit, because a partial answer here
    silently becomes a confident verdict downstream.
    """
    payload, error = _spec_payload(target)
    if payload is None:
        return None, error
    shape = _source_content_shape() if payload.get("migration_spec_version") == SOURCE_EMPTY_SPEC_VERSION else None
    dashboards, error = _named_spec_pages(payload["dashboards"], "dashboards", oid.KIND_DASHBOARD, shape)
    worksheets, worksheet_error = _named_spec_pages(payload["worksheets"], "worksheets", oid.KIND_WORKSHEET, shape)
    error = error or worksheet_error
    if error is not None:
        return None, error
    placed, error = _placed_worksheet_ids(payload["dashboards"])
    if error is not None:
        return None, error
    orphans = [page for page in (worksheets or []) if page["id"] not in placed]
    candidates = (dashboards or []) + orphans
    if not candidates:
        return None, (
            "migration-spec.json declares no dashboards and no worksheets, so there is no expected "
            "page set to grade against"
        )
    repeated = oid.duplicates([page["id"] for page in candidates])
    if repeated:
        return None, (
            f"migration-spec.json reuses page id(s) {', '.join(repeated)}. An id is the ONLY way to "
            "sign one of two objects that share a name, so a colliding id makes every signature "
            "unattributable and the expected set cannot be graded"
        )
    return candidates, None


def expected_pages(target: Path) -> list[dict[str, Any]] | None:
    """Pages the engine could emit: every Tableau dashboard PLUS every ORPHAN worksheet.

    "Dashboards only" was wrong. ``twb_to_pbir.py`` (engine 2.339.0) emits one page per dashboard
    (:14549 ``_emit_page`` / :14551 ``page_order.append``) and then walks ``ir["worksheets"]`` a
    second time, emitting a page for each sheet NOT already placed on a dashboard (:14557-14558
    ``if ws["name"] in placed ... continue`` -> :14709 ``page_order.append``). Measured across the
    43 workbooks of a real 2.339.0 estate run, 19 have zero dashboards and would have produced an
    EMPTY expected set here, which is what made ``check_oracle_coverage`` grade an artifact against
    itself (issue #432, defect 2).

    This is the CANDIDATE set, not the emitted set: the engine legitimately drops a candidate in
    three declared cases (:14529 dashboard with no supported visuals, :14558 unsupported visual
    type, :14562 no usable field bindings). Attribution of those drops lives in
    :func:`page_drop_explanations`; this function deliberately reports what Tableau had.

    ``None`` means the candidate set could not be established - a missing spec, an unreadable one, or
    any malformed required collection. It NEVER means "no pages"; see :func:`_spec_pages` for the
    specific reason.
    """
    return _spec_pages(target)[0]


def _unit_workbook_keys(target: Path) -> tuple[set[str], set[str]]:
    """``(exact stems, slugged stems)`` of the Tableau workbooks this unit ships artifacts for.

    The engine names a report folder ``<workbook name>.Report`` (verified on all 44 workbooks of a
    real 2.339.0 estate run), so an artifact stem is a usable binding key back to a handover slice.

    Two collections, because the filesystem is allowed to have changed the spelling: an EXACT match
    binds, and a lossy match is the fallback for a name a filesystem sanitised.

    ⚠️ The lossy side is a :class:`NormalizedIndex`, not a set of keys. Collapsing it lost the
    collision it exists to detect: a unit shipping both ``Bo ok.Report`` and ``Bo-ok.Report`` produced
    one key ``book``, and a handover workbook ``Book`` was accepted even though either artifact could
    own it. The uniqueness guard has to hold on BOTH sides of the join. Indexing DISTINCT stems is
    equally load-bearing in the other direction: a report and its model share a stem and are one name,
    not two competing owners, and counting the raw files made every ordinary unit look collided.
    """
    stems: set[str] = set()
    for report in shipping_reports(target):
        stems.add(report.name.removesuffix(".Report"))
    for model in shipping_models(target):
        stems.add(model.name.removesuffix(".SemanticModel"))
    stems = {stem for stem in stems if stem}
    index: NormalizedIndex[str] = NormalizedIndex()
    for stem in sorted(stems):
        index.add(stem, stem)
    return stems, index


def page_drop_explanations(target: Path) -> dict[str, Any]:
    """Engine statements that a specific object of a specific KIND produced no page.

    Three independent guards, because a page-drop excuse is exactly the kind of evidence that gets
    borrowed. Each closed a measured fail-open on this PR:

    **Workbook identity.** A handover slice explains pages only for a workbook whose artifacts this
    unit ships (:func:`_unit_workbook_keys`). Without it, a row from ``"Different Workbook"`` excused
    a page missing from *this* one.

    **Object identity, kind included.** Evidence is indexed as an :class:`object_identity
    .ObjectIdentity`, so a WORKSHEET row can never settle a same-named DASHBOARD candidate - measured:
    a workbook with dashboard ``Sales`` and worksheet ``Sales`` had the worksheet's row excuse the
    dashboard's absence. The index is built ``normalized=False``: this is an engine-to-engine join,
    so there is deliberately no lossy key table for it to fall back into.

    **Structured non-emission evidence: ``tier == "empty"`` AND ``visual_type == "unsupported"``.**
    ``migrate_estate._fidelity_tier`` returns ``empty`` only for ``visual_type in (None,
    "unsupported")``, and ``_viz_fidelity`` gives a real visual type only to rows built from
    ``ir["worksheets"]`` - a dashboard-scope row carries ``visual_type: "dashboard"`` and is never
    ``empty``. Measured on a 2.339.0 estate run: all 46 empty-tier rows carry ``unsupported`` and no
    dashboard/filter/workbook-scope row ever does. So an empty-tier row identifies a WORKSHEET, and
    requiring both fields is what stops a forged or mis-scoped row claiming otherwise. Neither
    ``status: "warned"`` (it spans both outcomes) nor ``evidence: "emitted+linted"`` (present on all
    45 of those empty rows) nor reason text may decide this.

    ⚠️ This function DIAGNOSES an omission. It never approves one - see :func:`check_page_parity`.
    """
    exact_stems, slugged_stems = _unit_workbook_keys(target)
    workbooks, _unreadable = _handover_workbooks(target)
    index: oid.EngineIndex[str] = oid.EngineIndex()
    described: dict[str, list[str]] = {}
    bound: list[str] = []
    unbound: list[str] = []
    for name, workbook in _bindable_workbooks(workbooks, exact_stems, slugged_stems):
        if workbook is None:
            unbound.append(name)
            continue
        bound.append(name)
        _collect_drop_rows(workbook.get("viz_fidelity"), index, described)
    return {
        "index": index,
        "described": described,
        "bound_workbooks": sorted(set(bound)),
        "unbound_workbooks": sorted(set(unbound)),
        "available": bool(bound),
        "source": DROP_EXPLANATION_SOURCE,
    }


def _bindable_workbooks(
    workbooks: list[tuple[Path, str, dict[str, Any]]], exact_stems: set[str], stem_index: NormalizedIndex[str]
) -> list[tuple[str, dict[str, Any] | None]]:
    """``(name, payload-or-None)`` per handover workbook: None means it does not bind to this unit.

    An EXACT stem match binds. A lossy match is the fallback for a name the filesystem sanitised, and
    it binds only when it is unique on BOTH sides - exactly one handover workbook AND exactly one
    shipped artifact carry that key. Either collision alone makes the binding a coin toss, and the
    target side was unguarded: a unit shipping ``Bo ok.Report`` and ``Bo-ok.Report`` accepted a single
    handover ``Book`` that either artifact could have owned.

    Both sides are :class:`NormalizedIndex`es rather than key lists, so neither uniqueness check can
    be quietly dropped or collapsed downstream - the bucket is unreachable except through ``unique``.
    """
    names = [str(workbook.get("name") or slice_name) for _source, slice_name, workbook in workbooks]
    handover_index: NormalizedIndex[str] = NormalizedIndex()
    for name in names:
        handover_index.add(name, name)
    bound: list[tuple[str, dict[str, Any] | None]] = []
    for (_source, _slice_name, workbook), name in zip(workbooks, names, strict=True):
        exact = name in exact_stems
        loose = handover_index.unique(name) is not None and stem_index.unique(name) is not None
        bound.append((name, workbook if exact or loose else None))
    return bound


def _collect_drop_rows(rows: Any, index: oid.EngineIndex[str], described: dict[str, list[str]]) -> None:
    """Index one workbook's proof-of-non-emission rows; record every other row for reporting only."""
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = row.get("worksheet")
        if not isinstance(name, str) or not name.strip():
            continue
        reason = row.get("reason")
        text = reason.strip() if isinstance(reason, str) and reason.strip() else "(no reason recorded)"
        identity = _drop_evidence_identity(row)
        if identity is None:
            described.setdefault(_slug(name), []).append(f"tier={row.get('tier')!r}, type={row.get('visual_type')!r}")
            continue
        index.add(identity, text)


def _candidate_index(candidates: list[dict[str, Any]]) -> oid.EngineIndex[dict[str, Any]]:
    """Expected pages, keyed by their exact ``(kind, name)`` identity."""
    index: oid.EngineIndex[dict[str, Any]] = oid.EngineIndex()
    for page in candidates:
        identity = _candidate_identity(page)
        if identity is not None:
            index.add(identity, page)
    return index


@dataclass(frozen=True)
class NameClaim:
    """Which expected pages a bare, KIND-LESS name could be referring to.

    A PBIR page's display name, an exemption's ``item`` and an oracle manifest entry all name an
    object without saying what KIND it is, so none of them can be turned into an
    :class:`object_identity.ObjectIdentity` by its producer. This is the ONE adapter that crosses
    that gap: it asks the identity index for every identifiable kind and refuses as soon as more than
    one expected page answers.

    Keeping it in one place is the point. Measured before it existed: a workbook with dashboard
    ``Sales`` and worksheet ``Sales`` had one rendered ``Sales`` page satisfy BOTH (PASS, one oracle
    row counted as 2/2 coverage), and one exemption named ``Sales`` sign BOTH omissions for a single
    recorded compromise.
    """

    name: str
    count: int
    page: dict[str, Any] | None

    @property
    def outcome(self) -> str:
        """``ABSENT``, ``UNIQUE`` or ``AMBIGUOUS`` - the same vocabulary as a ``Resolution``."""
        if not self.count:
            return oid.ABSENT
        return oid.UNIQUE if self.count == 1 else oid.AMBIGUOUS


def _claim(index: oid.EngineIndex[dict[str, Any]], name: Any) -> NameClaim:
    """Resolve a bare name against the expected pages, across every identifiable kind.

    ``Resolution.value()`` stays the only reader of a match and still raises unless that kind resolved
    uniquely; the total across kinds decides whether the NAME as a whole is attributable. Nothing here
    reads a resolution's truthiness - the shared type now raises on that, deliberately.
    """
    if not isinstance(name, str) or not name.strip():
        return NameClaim(name=str(name), count=0, page=None)
    total = 0
    unique: dict[str, Any] | None = None
    for kind in sorted(oid.IDENTIFIABLE_KINDS):
        identity = oid.ObjectIdentity.from_engine(kind, name)
        if identity is None:
            continue
        resolution = index.resolve(identity)
        total += resolution.count
        if resolution.outcome == oid.UNIQUE:
            unique = resolution.value()
    return NameClaim(name=name, count=total, page=unique if total == 1 else None)


def _drop_evidence_identity(row: dict[str, Any]) -> oid.ObjectIdentity | None:
    """The object a row PROVES produced no page, or None when it proves nothing.

    Built only through ``ObjectIdentity.from_engine``, never the dataclass constructor, so a row can
    never inject a kind the type refuses.
    """
    if row.get("tier") != DROP_EVIDENCE_TIER or row.get("visual_type") != DROP_EVIDENCE_VISUAL_TYPE:
        return None
    name = row.get("worksheet")
    return oid.ObjectIdentity.from_engine(oid.KIND_WORKSHEET, name if isinstance(name, str) else None)


def _candidate_identity(page: dict[str, Any]) -> oid.ObjectIdentity | None:
    """A candidate page's identity, again only via ``from_engine``."""
    return oid.ObjectIdentity.from_engine(str(page.get("kind") or ""), page.get("name"))


def _declared_omission(page: dict[str, str], explanations: dict[str, Any]) -> str | None:
    """The engine's proof that this exact object produced no page, or None.

    Reads the resolution through ``.value()`` inside ``try``: the outcome check and the raise are
    two independent refusals of a non-unique match, and neither reads the object's truthiness -
    ``Resolution`` has no ``__bool__``, so ``if resolution:`` would be True for ABSENT as well.
    """
    identity = _candidate_identity(page)
    if identity is None:
        return None
    resolution = explanations["index"].resolve(identity)
    if resolution.outcome != oid.UNIQUE:
        return None
    try:
        return resolution.value()
    except oid.AmbiguousIdentity:
        return None


def page_expectation(target: Path) -> dict[str, Any]:
    """Every expected page, classified as SATISFIED or as an OMISSION, by IDENTITY throughout.

    ``candidates`` = dashboards + orphan worksheets, each carrying its KIND. Pairing runs through the
    same :class:`object_identity.EngineIndex` as the drop evidence, via :func:`_claim`: a rendered
    page names an object without saying what kind it is, so a name claimed by more than one expected
    page attributes to NEITHER. Measured before this, with dashboard ``Sales`` and worksheet
    ``Sales`` both expected, one rendered ``Sales`` page satisfied both and one oracle row was
    counted as 2-of-2 coverage.

    A candidate is satisfied only by a RENDERED page - one carrying at least one visual - whose exact
    name it uniquely claims. ``attribution_ambiguous`` is the rename guard: while a rendered page
    matches no expected page it might BE a renamed candidate, so no absent candidate is genuinely
    absent and no name-only signature may be applied.

    ``assessable`` is False when the expected set cannot be established at all. It never degrades
    into the emitted pages: grading an artifact against itself reports a perfect score regardless of
    what is missing.
    """
    actual = actual_pages(target)
    rendered = [page for page in actual if page.get("visuals", 0) > 0]
    candidates, refusal = _spec_pages(target)
    explanations = page_drop_explanations(target)
    if candidates is None:
        return {
            "assessable": False,
            "reason": refusal,
            "candidates": None,
            "index": None,
            "actual": actual,
            "rendered": rendered,
            "omissions": [],
            "unmatched_rendered": [],
            "contested_names": [],
            "attribution_ambiguous": False,
            "explanations": explanations,
        }
    index = _candidate_index(candidates)
    rendered_names = [page["name"] for page in rendered]
    satisfied: set[int] = set()
    unmatched: list[dict[str, Any]] = []
    contested: set[str] = set()
    for page in rendered:
        claim = _claim(index, page["name"])
        if claim.outcome == oid.ABSENT:
            unmatched.append(page)
        elif claim.outcome == oid.AMBIGUOUS or rendered_names.count(page["name"]) != 1:
            contested.add(claim.name)
        else:
            satisfied.add(id(claim.page))
    absent = [page for page in candidates if id(page) not in satisfied]
    return {
        "assessable": True,
        "reason": None,
        "candidates": candidates,
        "index": index,
        "actual": actual,
        "rendered": rendered,
        "omissions": [{**page, "declared_reason": _declared_omission(page, explanations)} for page in absent],
        "unmatched_rendered": unmatched,
        "contested_names": sorted(contested),
        "explanations": explanations,
    }


def _why_unexplained(page: dict[str, Any], explanations: dict[str, Any]) -> str:
    """Name the specific gap, so an operator knows whether to fetch evidence or sign an exemption."""
    described = explanations["described"].get(_slug(page["name"]))
    if described:
        return (
            f"the bound handover mentions this name but proves nothing about a {page.get('kind')} "
            f"(needs tier={DROP_EVIDENCE_TIER!r} + visual_type={DROP_EVIDENCE_VISUAL_TYPE!r}; "
            f"got {'; '.join(described[:2])})"
        )
    if not explanations["available"]:
        if explanations["unbound_workbooks"]:
            return (
                "no handover slice binds to this unit's artifacts; "
                f"found instead: {', '.join(explanations['unbound_workbooks'][:3])}"
            )
        return f"no handover slice was readable, so {explanations['source']} could not be consulted"
    return "the bound handover records nothing about this page"


def _page_order(report_dir: Path) -> list[str]:
    pages_file = report_dir / "definition" / "pages" / "pages.json"
    if not pages_file.is_file():
        return []
    try:
        payload = _read_json(pages_file)
    except (OSError, json.JSONDecodeError):
        return []
    order = payload.get("pageOrder") if isinstance(payload, dict) else None
    return [str(item) for item in order] if isinstance(order, list) else []


def _page_visual_count(page_json: Path) -> int:
    """How many visuals a PBIR page actually carries.

    A page with zero ``visuals/**/visual.json`` files renders nothing. That is load-bearing: it is
    the only thing that distinguishes the engine's crash-guard placeholder from a rebuilt page, and
    without it renaming an empty page to an expected page's title certified it as rebuilt.
    """
    visuals_root = page_json.parent / "visuals"
    if not visuals_root.is_dir():
        return 0
    return sum(1 for _ in visuals_root.rglob("visual.json"))


def actual_pages(target: Path) -> list[dict[str, Any]]:
    """PBIR pages from shipping reports, ordered by pages.json where available.

    Each row carries ``visuals``, the page's own visual count, so callers can tell a rebuilt page
    from one that renders nothing.
    """
    pages: list[dict[str, Any]] = []
    for report in shipping_reports(target):
        pages_root = report / "definition" / "pages"
        ordered = _page_order(report)
        page_dirs = {path.parent.name: path for path in pages_root.rglob("page.json")} if pages_root.is_dir() else {}
        names = ordered or sorted(page_dirs)
        for page_id in names:
            page_json = page_dirs.get(page_id)
            if page_json is None:
                continue
            try:
                payload = _read_json(page_json)
            except (OSError, json.JSONDecodeError):
                continue
            display = payload.get("displayName") if isinstance(payload, dict) else None
            internal = payload.get("name") if isinstance(payload, dict) else None
            pages.append(
                {
                    "id": str(internal or page_id),
                    "name": str(display or internal or page_id),
                    "report": str(report),
                    "path": str(page_json),
                    "visuals": _page_visual_count(page_json),
                }
            )
    return pages


def load_exemptions(target: Path) -> dict[str, Any]:
    """Read and validate the explicit documented-why-not sidecar."""
    path = _unit_dir(target) / EXEMPTIONS_FILE
    if not path.is_file():
        return {"path": str(path), "entries": [], "invalid": []}
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(path), "entries": [], "invalid": [{"item": str(path), "reason": f"unreadable: {exc}"}]}
    raw_entries = payload.get("exemptions") if isinstance(payload, dict) else payload
    if not isinstance(raw_entries, list):
        return {
            "path": str(path),
            "entries": [],
            "invalid": [{"item": str(path), "reason": "expected a list or exemptions[]"}],
        }
    entries: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    for index, entry in enumerate(raw_entries, 1):
        if not isinstance(entry, dict):
            invalid.append({"item": f"#{index}", "reason": "entry is not an object"})
            continue
        check = str(entry.get("check") or "").strip()
        item = str(entry.get("item") or "").strip()
        reason = str(entry.get("reason") or "").strip()
        decided_by = str(entry.get("decided_by") or entry.get("owner") or "").strip()
        if check not in VALID_EXEMPTION_CHECKS or not item or not reason or not decided_by:
            valid = ", ".join(sorted(VALID_EXEMPTION_CHECKS))
            invalid.append(
                {
                    "item": item or f"#{index}",
                    "reason": f"requires check in {{{valid}}}, item, reason, and decided_by",
                }
            )
            continue
        entries.append({"check": check, "item": item, "reason": reason, "decided_by": decided_by})
    return {"path": str(path), "entries": entries, "invalid": invalid}


def resolve_exemptions(
    entries: list[dict[str, str]], check: str, findings: list[tuple[str, str, set[str]]]
) -> tuple[set[str], list[str]]:
    """``(exempted finding keys, entries that matched several findings)``.

    ``findings`` is ``(key, item, aliases)`` per finding. Each raw entry is resolved ONCE against all
    of them: an exact name or alias match wins outright, and the lossy key is consulted only when no
    finding matches exactly. An entry matching more than one finding applies to none - measured, a
    single ``scaffold-partitions`` signature named ``A-B`` exempted both table ``A-B`` and table
    ``A B`` and flipped the gate to PASS while reporting two exemptions from one signature.

    The lossy half goes through a :class:`NormalizedIndex` keyed by *finding*, so a name and an alias
    of the SAME finding cannot look like two candidates while two different findings still do.
    """
    lossy: NormalizedIndex[str] = NormalizedIndex()
    for key, name, aliases in findings:
        for value in sorted({name, *aliases}):
            if value:
                lossy.add_spelling(value, key)
    exempted: set[str] = set()
    contested: list[str] = []
    for entry in entries:
        if entry.get("check") != check:
            continue
        item = entry["item"]
        exact = [key for key, name, aliases in findings if item == name or item in aliases]
        if len(exact) == 1:
            exempted.add(exact[0])
        elif exact:
            contested.append(item)
        elif (match := lossy.unique(item)) is not None:
            exempted.add(match)
        elif lossy.count(item):
            contested.append(item)
    return exempted, contested


def _handover_workbooks(target: Path) -> tuple[list[tuple[Path, str, dict[str, Any]]], list[str]]:
    """Workbook payloads from handover slices/estate reports, using read_handover's resolver.

    ⚠️ Candidate roots are RESOLVED before they are de-duplicated. ``_unit_dir`` resolves its return
    value while ``target`` keeps whatever spelling the caller passed, so with the documented relative
    CLI invocation the two candidates compared unequal, the same directory was scanned twice, and
    every evidence row was indexed twice - turning each into an AMBIGUOUS resolution and making its
    declared reason vanish. Absolute invocations happened to work, which is how it survived review.
    """
    roots: list[Path] = []
    for candidate in (_unit_dir(target) / "handover", target / "handover"):
        if not candidate.is_dir():
            continue
        resolved = candidate.resolve()
        if resolved not in roots:
            roots.append(resolved)
    workbooks: list[tuple[Path, str, dict[str, Any]]] = []
    unreadable: list[str] = []
    for root in roots:
        for path in sorted(root.glob("*.json"), key=str):
            payload = _json_object(path)
            if payload is not None and _is_handover_slice(path):
                try:
                    resolved = read_handover._workbooks_from_payload(payload, path)  # pylint: disable=protected-access
                except read_handover.HandoverError:
                    unreadable.append(_display_path(path))
                    continue
                if not resolved:
                    unreadable.append(_display_path(path))
                    continue
                for name, workbook, source in resolved:
                    workbooks.append((source, name, workbook))
    return workbooks, unreadable


def _scaffold_row_identity(row: dict[str, Any], handover: Path) -> tuple[str, set[str]]:
    """Canonical exemption identity for one unresolved M-partition scaffold."""
    table = str(row.get("table") or row.get("name") or "").strip()
    reason = str(row.get("reason") or "").strip()
    item = table or reason or handover.stem
    aliases = {table, reason, f"{handover.stem}:{table}", f"{handover.stem}:{reason}"}
    return item, {alias for alias in aliases if alias}


def _scaffold_rows_for_workbook(
    path: Path, handover: str, raw_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[tuple[str, str, set[str]]]]:
    """``(rows, findings)`` for one handover workbook; the key ties a row to its finding."""
    rows: list[dict[str, Any]] = []
    findings: list[tuple[str, str, set[str]]] = []
    for index, row in enumerate(raw_rows):
        item, aliases = _scaffold_row_identity(row, path)
        key = f"{handover}#{index}"
        findings.append((key, item, aliases))
        rows.append(
            {
                "handover": handover,
                "table": str(row.get("table") or ""),
                "reason": str(row.get("reason") or ""),
                "item": item,
                "key": key,
                "exempted": False,
            }
        )
    return rows, findings


def _scaffold_scan(
    workbooks: list[tuple[Path, str, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str], list[str], list[tuple[str, str, set[str]]]]:
    """``(rows, missing, invalid, findings)`` across every handover workbook, before any signature."""
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    invalid: list[str] = []
    findings: list[tuple[str, str, set[str]]] = []
    buckets = {read_handover.PARTITION_REVIEW_MISSING: missing, read_handover.PARTITION_REVIEW_INVALID: invalid}
    for path, workbook_name, workbook in workbooks:
        status, raw_rows = read_handover.partitions_needs_review_status(workbook)
        handover = f"{_display_path(path)}::{workbook_name}"
        if status in buckets:
            buckets[status].append(handover)
        elif status == read_handover.PARTITION_REVIEW_PRESENT:
            found = _scaffold_rows_for_workbook(path, handover, raw_rows)
            rows.extend(found[0])
            findings.extend(found[1])
    return rows, missing, invalid, findings


def _scaffold_partition_rows(
    workbooks: list[tuple[Path, str, dict[str, Any]]], entries: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    """Classify scaffold rows plus workbook payloads whose key is missing/invalid.

    Signatures are resolved GLOBALLY across every row before any row is marked exempt, so a raw entry
    naming two rows exempts neither and is returned as contested.
    """
    rows, missing, invalid, findings = _scaffold_scan(workbooks)
    exempted, contested = resolve_exemptions(entries, "scaffold-partitions", findings)
    for row in rows:
        row["exempted"] = row["key"] in exempted
    return rows, missing, invalid, contested


def check_scaffold_partitions(target: Path, exemptions: dict[str, Any]) -> dict[str, Any] | None:
    """Gate engine-recorded empty M-partition scaffolds from handover slices.

    No handover slice means there is nothing new for this gate to read; the brownfield section already
    reports that phase as absent. A present handover with a missing key is different and stays
    NOT_CHECKED rather than being conflated with "zero scaffolds".
    """
    workbooks, unreadable = _handover_workbooks(target)
    if not workbooks:
        if not unreadable:
            return None
        return {
            "id": "scaffold-partitions",
            "status": STATUS_FINDINGS,
            "detail": "handover JSON could not be read as a slice or estate report",
            "scaffolds": [],
            "unexempted_scaffolds": 0,
            "scaffold_exemptions": 0,
            "missing_handover_keys": [],
            "invalid_handover_keys": unreadable,
        }
    rows, missing, invalid, contested = _scaffold_partition_rows(workbooks, exemptions["entries"])
    invalid.extend(unreadable)
    unexempted = [row for row in rows if not row["exempted"]]
    if unexempted or invalid:
        status = STATUS_FINDINGS
    elif missing:
        status = STATUS_NOT_CHECKED
    else:
        status = STATUS_PASS
    detail = None
    if unexempted:
        detail = f"{len(unexempted)} partition scaffold(s) need manual completion"
    elif invalid:
        detail = "partition scaffold status has invalid shape in handover"
    elif missing:
        detail = "partition scaffold status not recorded in handover; this is not a zero-deferral signal"
    return {
        "id": "scaffold-partitions",
        "status": status,
        "detail": detail,
        "scaffolds": rows,
        "unexempted_scaffolds": len(unexempted),
        "scaffold_exemptions": len(rows) - len(unexempted),
        "contested_exemptions": contested,
        "missing_handover_keys": missing,
        "invalid_handover_keys": invalid,
    }


def check_page_parity(target: Path, exemptions: dict[str, Any]) -> dict[str, Any]:
    """Page precondition: every expected Tableau page is either rebuilt or SIGNED OFF as omitted.

    The rule this gate got wrong twice, stated plainly: **evidence of an absence is not acceptance of
    an absence.** ``tier: "empty"`` proves the ENGINE emitted no faithful visual for an object; it
    says nothing about whether a human accepted shipping without that page. So an engine-declared
    omission is reported with its proof and still fails the gate until an
    ``unit-check-exemptions.json`` entry names it. Only that signature makes it an accepted
    compromise; measured before this, a unit missing a page returned PASS from parity AND
    validation-grade oracle coverage with ``compromises=0``.

    "Rebuilt" means a rendered page - one carrying at least one visual - bearing the candidate's
    exact name. Zero-visual pages are split: the engine's crash-guard placeholder is expected; any
    other is reported as a blank page and fails.

    Exemptions are dispositioned rather than applied blindly. One naming a page that is present is
    ``stale``; one naming an omission while a rendered page is unaccounted for - so the "omission"
    may be a rename - is ``ambiguous`` and is NOT applied. Only ``applied`` counts as a compromise.
    """
    expectation = page_expectation(target)
    actual = expectation["actual"]
    if not expectation["assessable"]:
        return {
            "id": "page-parity",
            "status": STATUS_NOT_CHECKED,
            "detail": expectation["reason"],
            "expected_pages": None,
            "actual_pages": actual,
            "exemptions": [],
            "applied_exemptions": [],
            "unapplied_exemptions": [],
        }
    entries = exemptions["entries"]
    signatures = _resolve_signatures(expectation, entries)
    placeholders, blank = _zero_visual_pages(actual, expectation["rendered"])
    unaccounted_extra = [
        page for page in expectation["unmatched_rendered"] if id(page) not in signatures.accounted_extra
    ]
    ambiguous = _attribution_ambiguous(expectation, signatures)
    omissions = _disposition_omissions(expectation, signatures, ambiguous)
    applied = [row for row in omissions if row["disposition"] == OMISSION_SIGNED]
    unsigned = [row for row in omissions if row["disposition"] not in OMISSIONS_OWING_NOTHING]
    unapplied = _unapplied_exemptions(expectation, signatures, ambiguous)
    status = (
        STATUS_PASS
        if not unsigned and not unaccounted_extra and not blank and not expectation["contested_names"]
        else STATUS_PRECONDITION_FAILED
    )
    return {
        "id": "page-parity",
        "status": status,
        "detail": _page_parity_detail(status, unsigned, unaccounted_extra, blank, expectation),
        "expected_count": len(expectation["candidates"]),
        "emitted_count": len(expectation["rendered"]),
        "actual_count": len(actual),
        "expected_pages": expectation["candidates"],
        "actual_pages": actual,
        "omissions": omissions,
        "unsigned_omissions": unsigned,
        "source_empty_omissions": [row for row in omissions if row["disposition"] == OMISSION_SOURCE_EMPTY],
        "blank_pages": blank,
        "engine_placeholder_pages": placeholders,
        "unaccounted_extra_pages": unaccounted_extra,
        "contested_names": expectation["contested_names"],
        "attribution_ambiguous": ambiguous,
        "exemptions": applied,
        "applied_exemptions": applied,
        "unapplied_exemptions": unapplied,
        "drop_explanations": {key: value for key, value in expectation["explanations"].items() if key not in {"index"}},
    }


def _disposition_omissions(
    expectation: dict[str, Any], signatures: SignatureResolution, ambiguous: bool
) -> list[dict[str, Any]]:
    """Classify every omission. Only a signature that can be attributed accepts one.

    ``source-empty`` comes first and is the one disposition that is not a compromise: a worksheet
    with no encodings and no filters renders blank in Tableau too, so it owes no Power BI page. That
    is established from the SPEC (see :func:`_source_empty`) and never from an engine tier.
    """
    rows = []
    for page in expectation["omissions"]:
        signature = signatures.signature_for(page)
        if page.get("source_empty"):
            disposition = OMISSION_SOURCE_EMPTY
        elif signature == SIGNATURE_UNIQUE and not ambiguous:
            disposition = OMISSION_SIGNED
        elif ambiguous or signature == SIGNATURE_CONTESTED:
            disposition = OMISSION_AMBIGUOUS
        elif page["declared_reason"]:
            disposition = OMISSION_DECLARED
        else:
            disposition = OMISSION_UNEXPLAINED
        rows.append(
            {**page, "disposition": disposition, "why": _omission_why(page, disposition, expectation, signature)}
        )
    return rows


SIGNATURE_UNIQUE = "unique"
SIGNATURE_CONTESTED = "contested"

#: Prefix marking a signature that declares an EMITTED page the source never had.
EXTRA_SIGNATURE_PREFIX = "extra:"


def _page_key(page: dict[str, Any]) -> tuple[str, str, str]:
    """A candidate's exact identity as a hashable key: ``(kind, id, name)``.

    Used wherever pages must be compared or removed as a set. ⚠️ Never the display name alone - that
    is the flattening this gate keeps re-introducing, most recently in the oracle denominator, where
    one accepted ``Sales`` removed BOTH a dashboard and a worksheet called ``Sales``.
    """
    return (str(page.get("kind")), str(page.get("id")), str(page.get("name")))


def _page_parity_items(entries: list[dict[str, str]]) -> tuple[list[str], list[str]]:
    """``(plain items, extra: names)`` from the signed file, as EXACT strings, multiplicity kept.

    ⚠️ Never through :func:`resolve_exemptions`. That helper falls back to slugs, which is right for
    its own item vocabularies but wrong for an engine identity: one entry named ``A-B`` signed both
    ``A-B`` and ``A B``, because punctuation vanishes in a slug. A signature applies to an engine
    object or it does not. Lists, not sets, because two identical entries are two claims.
    """
    items = [entry["item"] for entry in entries if entry.get("check") == "page-parity"]
    return (
        [item for item in items if not item.startswith(EXTRA_SIGNATURE_PREFIX)],
        [item[len(EXTRA_SIGNATURE_PREFIX) :] for item in items if item.startswith(EXTRA_SIGNATURE_PREFIX)],
    )


@dataclass(frozen=True)
class SignatureResolution:
    """Every page-parity signature resolved ONCE, GLOBALLY, before any page is judged.

    The previous shape asked, per page, whether an item equalled *that* page's id or name. Exactness
    without globality still lets one item match two objects, because nothing ever asked how many
    things the item matches in total: an item equal to page A's ID and page B's NAME signed both, and
    one ``extra:Bonus`` accepted two distinct rendered pages both called ``Bonus``.

    So each raw item is resolved against the whole unit and counted by DISTINCT OBJECT - across the
    id and name namespaces together, so a cross-namespace collision is contested rather than lucky.
    An item matching one object applies; an item matching several applies to none.
    """

    applied: dict[tuple[str, str, str], str]
    contested_items: tuple[str, ...]
    accounted_extra: frozenset[int]
    contested_extra_items: tuple[str, ...]

    def signature_for(self, page: dict[str, Any]) -> str | None:
        """``SIGNATURE_UNIQUE`` when this page is signed, ``SIGNATURE_CONTESTED`` when a signature
        names it but cannot be attributed, else ``None``."""
        if _page_key(page) in self.applied:
            return SIGNATURE_UNIQUE
        if any(item in {page["id"], page["name"]} for item in self.contested_items):
            return SIGNATURE_CONTESTED
        return None


def _resolve_signatures(expectation: dict[str, Any], entries: list[dict[str, str]]) -> SignatureResolution:
    """Resolve every signature against the whole unit exactly once."""
    plain, extra = _page_parity_items(entries)
    applied: dict[tuple[str, str, str], str] = {}
    contested: list[str] = []
    for item in plain:
        matched = {id(page): page for page in expectation["candidates"] if item in {page["id"], page["name"]}}
        if len(matched) == 1:
            applied[_page_key(next(iter(matched.values())))] = item
        elif matched:
            contested.append(item)
    accounted: set[int] = set()
    contested_extra: list[str] = []
    for item in extra:
        matched = {id(page): page for page in expectation["rendered"] if page["name"] == item}
        if len(matched) == 1:
            accounted.add(next(iter(matched)))
        elif matched:
            contested_extra.append(item)
    return SignatureResolution(
        applied=applied,
        contested_items=tuple(contested),
        accounted_extra=frozenset(accounted),
        contested_extra_items=tuple(contested_extra),
    )


def _omission_why(page: dict[str, Any], disposition: str, expectation: dict[str, Any], signature: str | None) -> str:
    """One actionable sentence per omission - what it is, and what would resolve it."""
    if disposition == OMISSION_SIGNED:
        return "accepted by a signed page-parity exemption"
    if disposition == OMISSION_SOURCE_EMPTY:
        return (
            "the source worksheet has no encodings and no filters, so it renders blank in Tableau too "
            "and owes no Power BI page"
        )
    if disposition == OMISSION_AMBIGUOUS:
        if signature == SIGNATURE_CONTESTED:
            return (
                f"a signature naming {page['name']!r} cannot be attributed - more than one expected "
                "page carries that name; sign the page's id instead"
            )
        names = ", ".join(row["name"] for row in expectation["unmatched_rendered"][:3])
        contested = ", ".join(expectation["contested_names"][:3])
        blocker = f"emitted page(s) {names} match no expected page" if names else f"name(s) {contested} are contested"
        return (
            f"cannot establish whether this page was dropped or renamed while {blocker}; "
            "sign 'extra:<page>' to account for them first"
        )
    if disposition == OMISSION_DECLARED:
        return (
            f"the engine declared it produced no visual ({page['declared_reason']}) - that explains "
            "the omission but does not accept it; rebuild the page or sign a page-parity exemption"
        )
    return _why_unexplained(page, expectation["explanations"])


def _unapplied_exemptions(
    expectation: dict[str, Any], signatures: SignatureResolution, ambiguous: bool
) -> list[dict[str, Any]]:
    """Page-parity signatures that accepted NOTHING, each carrying why.

    A signature naming a page that is present accepted nothing (``stale``); one that could not be
    attributed accepted nothing either (``ambiguous``) - whether because a rendered page is
    unaccounted for, because more than one expected page carries the name it uses, or because the
    item itself matched several objects. Neither may count as a compromise, and both are reported so
    a reader can see the file promised more than it delivered.

    ⚠️ Omitted pages are compared by :func:`_page_key`, not by display name. A name set here meant a
    present page whose name matched an omitted one was never reported stale.
    """
    omitted = {_page_key(page) for page in expectation["omissions"]}
    rows = [
        {**page, "disposition": EXEMPTION_STALE}
        for page in expectation["candidates"]
        if _page_key(page) not in omitted and signatures.signature_for(page) is not None
    ]
    rows += [
        {**page, "disposition": EXEMPTION_AMBIGUOUS}
        for page in expectation["omissions"]
        if (signature := signatures.signature_for(page)) is not None and (ambiguous or signature == SIGNATURE_CONTESTED)
    ]
    return rows + [
        {"id": item, "name": item, "kind": "extra", "disposition": EXEMPTION_AMBIGUOUS}
        for item in signatures.contested_extra_items
    ]


def _zero_visual_pages(
    actual: list[dict[str, Any]], emitted: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the pages that render nothing into the engine's declared placeholder and the rest.

    A zero-visual page is never evidence that a candidate was rebuilt. Exactly one such page is
    EXPECTED - the crash-guard placeholder - and every other one is a page that ships and renders
    nothing, which is a finding in its own right.

    Only zero-visual pages are asked about at all: a page carrying visuals is a rebuilt page by the
    ``rendered`` split and can never be a placeholder. Enforcing that HERE rather than as a third
    clause inside :func:`_is_engine_placeholder_page` keeps every clause observable - as a predicate
    clause it changed no verdict, because a page with visuals is neither blank nor an extra.
    """
    blankish = [page for page in actual if page not in emitted]
    placeholders = [page for page in blankish if _is_engine_placeholder_page(page)]
    return placeholders, [page for page in blankish if page not in placeholders]


def _is_engine_placeholder_page(page: dict[str, Any]) -> bool:
    """Whether a zero-visual page is the engine's crash-guard placeholder rather than a blank page.

    Both engine constants are required (:13091, :14720) and each has its own reproduction: a real
    page can be titled "No visuals rebuilt", and a report can carry a ``page-empty*`` id whose author
    later gave it a different title. Either alone hides a page that ships and renders nothing.
    """
    return page.get("name") == ENGINE_PLACEHOLDER_PAGE_NAME and str(page.get("id", "")).startswith(
        ENGINE_PLACEHOLDER_PAGE_ID_PREFIX
    )


def _page_parity_detail(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    status: str,
    unsigned: list[dict[str, Any]],
    unaccounted_extra: list[dict[str, Any]],
    blank: list[dict[str, Any]],
    expectation: dict[str, Any],
) -> str | None:
    """Name what failed, and for each omission exactly what would resolve it."""
    if status == STATUS_PASS:
        return None
    parts = []
    if unsigned:
        parts.append(f"{len(unsigned)} expected page(s) omitted without a signed exemption")
    if unaccounted_extra:
        names = ", ".join(page["name"] for page in unaccounted_extra[:3])
        parts.append(f"{len(unaccounted_extra)} emitted page(s) match no expected page: {names}")
    if blank:
        names = ", ".join(page["name"] for page in blank[:3])
        parts.append(f"{len(blank)} page(s) carry no visuals and so render nothing: {names}")
    for page in unsigned[:3]:
        parts.append(f"{page['kind']} {page['name']!r} [{page['disposition']}] - {page['why']}")
    _ = expectation
    return "; ".join(parts) or None


def _resolved_unique(candidates: list[Path | None]) -> list[Path]:
    """Existing directories, RESOLVED first and then deduplicated.

    Order matters and this is the third place it has: round 3 measured handover discovery indexing
    every evidence row twice because ``target`` and ``_unit_dir(target)`` are the same directory
    under two spellings, and deduplicating before resolving cannot see that. ``_reference_dirs`` was
    left behind - harmless while records were merged into a ``{slug: bool}`` map, and immediately
    visible once multiplicity was preserved: one reference manifest produced two records for every
    dashboard, and the pair then refused itself as "2 producer records are named X".
    """
    dirs: list[Path] = []
    for path in candidates:
        if not path or not path.exists():
            continue
        resolved = path.resolve()
        if resolved not in dirs:
            dirs.append(resolved)
    return dirs


def _reference_dirs(target: Path, explicit: Path | None) -> list[Path]:
    unit = _unit_dir(target)
    candidates = [explicit] if explicit else [unit / "reference", target / "reference"]
    return _resolved_unique(candidates)


def _oracle_dirs(target: Path, explicit: Path | None) -> list[Path]:
    """Oracle capture directories, under BOTH documented names.

    ``capture_tableau_oracle.py`` is run with ``--out _oracle`` (AGENTS.md; docs/operator-runbook.md
    - ``--out`` is required, it has no default), but the canonical per-run layout puts the same
    capture at ``_runs/<NNN>-<slug>/oracle/`` (``scripts/work_dirs.py`` ``CANONICAL_SUBDIRS``;
    AGENTS.md "Canonical work layout"). Looking for only one of the two meant a real capture beside
    a bundle at ``_runs/<NNN>-<slug>/bundle`` was invisible and oracle-coverage reported "no oracle
    manifest found" for evidence that existed.

    ⚠️ The walk itself - how far up, and where it stops - is :func:`bundle_corpus.evidence_dirs`,
    shared with the entry gate. This function used to stop one level short of it, so a non-packaged
    unit at ``<bundle>/pbip/<Unit>/`` could not reach the run's flat capture at all (round-1 review
    of PR #454).
    """
    if explicit:
        return _resolved_unique([explicit])
    unit = _unit_dir(target)
    return evidence_dirs(unit, ORACLE_DIR_NAMES, also=[target])


def _existing_relative(base: Path, rel: str | None) -> bool:
    return bool(rel) and (base / rel).is_file()


@dataclass(frozen=True)
class OracleRecord:
    """One producer's claim about one Tableau object, with its identity intact.

    ⚠️ Deliberately NOT collapsed into a ``{slug: bool}`` map. That collapse - which is what this gate
    did for six rounds - destroys multiplicity, exact spelling and the producing WORKBOOK before
    coverage is evaluated, and it is what let reference evidence named ``A B`` give a validation-grade
    PASS to an expected page ``A-B``, and an oracle record for ``Revenue`` belonging to
    ``"Different Workbook"`` satisfy this unit's ``Revenue`` page. Drop evidence was bound to its
    workbook in round 2; oracle evidence was never bound at all.

    ``kind`` is ``dashboard``/``worksheet`` when the producer establishes it, and ``None`` when it
    does not. ⚠️ ``None`` is **cannot establish**, never "either" - a record whose kind is unknown may
    not certify a page. A Tableau dashboard routinely shares its name with its principal worksheet,
    so a kind-less name match accepts one visual as evidence for a whole page, and that is the
    ORDINARY case rather than an edge one (``scripts/tableau_view_types.py``). Measured before this:
    a record explicitly carrying ``view_type: "worksheet"`` gave a full oracle PASS to the DASHBOARD
    of the same name - the kind was present in the manifest and thrown away here.

    ``workbook`` is an :class:`object_identity.WorkbookIdentity`, shared with
    ``check_reference_readiness`` - see issue #450, where this gate read a ``workbook`` key that no
    producer writes while the sibling gate read a LUID it then distrusted, and one disagreement about
    "what identifies a workbook" produced a fail-open defect here and a fail-closed one there.

    ⚠️ There is deliberately **no** ``unit_local`` flag. There was: a `reference/manifest.json` found
    inside the unit skipped the workbook guard entirely, on the argument that its location proved
    ownership. Round-1 review of PR #454 measured what that bought - a record whose join returned
    ``unknown`` was admitted anyway (``admitted_evidence=1``, visual AND numeric certified), and so
    was one whose ``source_workbook_sha256`` named a **different workbook**. Location controls
    DISCOVERY; it never substitutes for identity. The manifest's ``source_workbook_sha256`` is the
    identity it actually carries, and it is now compared against the unit's own hashed source asset.
    """

    name: str
    kind: str | None
    workbook: oid.WorkbookIdentity
    visual: bool
    numeric: bool


def _declared_kind(record: Any) -> str | None:
    """``dashboard``/``worksheet`` from an oracle view record, or None when it cannot be established.

    ``capture_tableau_oracle.py`` writes ``view_type`` from the Metadata API (#402) and uses the
    literal ``"unknown"`` when the API could not be reached or exposed no LUID. That value is a
    refusal, not a third kind, so it maps to ``None`` exactly like an absent key - and so does any
    value outside the vocabulary, because an unrecognised string is equally unestablished.
    """
    if not isinstance(record, dict):
        return None
    value = record.get("view_type")
    return value if value in {"dashboard", "worksheet"} else None


def _reference_oracles(target: Path, reference_dir: Path | None) -> tuple[list[OracleRecord], set[str]]:
    """Reference-capture records, one per dashboard entry, multiplicity preserved.

    Every entry is a DASHBOARD by construction: ``capture_tableau_reference.py`` builds this array
    from the migration spec's ``dashboards`` (``_dashboard_names``), so the kind is structurally known
    here and does not depend on #402 landing.

    A `reference/manifest.json` declares no producing workbook NAME - it records
    ``source_workbook_sha256`` (`capture_tableau_reference.py:234`), which is a machine identity and
    is checked as one against :func:`_unit_source_sha256`. It is deliberately NOT trusted for sitting
    inside the unit; see :class:`OracleRecord`.
    """
    records: list[OracleRecord] = []
    grades: set[str] = set()
    for directory in _reference_dirs(target, reference_dir):
        manifest = directory / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            payload = _read_json(manifest)
        except (OSError, json.JSONDecodeError):
            continue
        for dashboard in payload.get("dashboards", []) if isinstance(payload, dict) else []:
            if not isinstance(dashboard, dict):
                continue
            visual, numeric, caps = _reference_states(directory, dashboard)
            grades |= caps
            records.append(
                OracleRecord(
                    name=str(dashboard.get("name") or ""),
                    kind="dashboard",
                    workbook=_declared_workbook(dashboard, payload, sha256=payload.get("source_workbook_sha256")),
                    visual=visual,
                    numeric=numeric,
                )
            )
    return records, grades


def _reference_states(directory: Path, dashboard: dict[str, Any]) -> tuple[bool, bool, set[str]]:
    """``(visual, numeric, grades)`` across one dashboard entry's captured states."""
    visual = numeric = False
    grades: set[str] = set()
    for state in dashboard.get("states", []):
        if not isinstance(state, dict):
            continue
        caps = {str(cap) for cap in state.get("capabilities", []) if isinstance(cap, str)}
        if caps:
            grades.add("validation-grade" if "validation_grade" in caps else "/".join(sorted(caps)))
        visual = visual or _existing_relative(directory, state.get("image"))
        oracle = state.get("numeric_oracle")
        numeric = numeric or (isinstance(oracle, str) and _existing_relative(directory, oracle))
    return visual, numeric, grades


def _is_within(directory: Path, base: Path) -> bool:
    """Whether ``directory`` sits inside ``base``. Both are already resolved by their finders."""
    try:
        return directory.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


def _declared_workbook(payload: Any, container: Any = None, *, sha256: Any = None) -> oid.WorkbookIdentity:
    """The producing workbook a manifest entry declares, on whichever axes it wrote.

    ⚠️ **Issue #450 was exactly this function reading a key nobody writes.** It read ``workbook``;
    ``capture_tableau_oracle.py`` writes ``workbook_luid`` and ``workbook_name`` per view, and
    ``capture_tableau_reference.py`` writes ``source_workbook_sha256`` on the manifest. Measured on a
    real 360-view capture, every single record therefore arrived ownerless, and the foreign-workbook
    guard that this gate advertises had never once fired. ``workbook`` is still read, first, because
    it is the documented override a hand-written manifest may carry.
    """
    entry = payload if isinstance(payload, dict) else {}
    outer = container if isinstance(container, dict) else {}
    return oid.WorkbookIdentity.of(
        luid=entry.get("workbook_luid") or outer.get("workbook_luid"),
        name=entry.get("workbook") or outer.get("workbook") or entry.get("workbook_name") or outer.get("workbook_name"),
        sha256=sha256,
    )


def _oracle_capture_oracles(target: Path, oracle_dir: Path | None) -> tuple[list[OracleRecord], set[str]]:
    """Tableau Server oracle-capture records, one per view, multiplicity preserved."""
    records: list[OracleRecord] = []
    grades: set[str] = set()
    for directory in _oracle_dirs(target, oracle_dir):
        manifest = directory / "oracle-manifest.json"
        if not manifest.is_file():
            continue
        try:
            payload = _read_json(manifest)
        except (OSError, json.JSONDecodeError):
            continue
        for record in payload.get("views", []) if isinstance(payload, dict) else []:
            if not isinstance(record, dict):
                continue
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            numeric = data.get("status") == "ok" and _existing_relative(directory, data.get("path"))
            # Any RENDER leg is visual evidence, not just the PNG. `--reference-best` now normally
            # selects SVG on Tableau Cloud (issue #403), and reading only `image` meant a run whose
            # reference was a vector SVG counted as having no visual oracle at all.
            visual = False
            for leg in ("image", "svg", "pdf"):
                leg_entry = record.get(leg) if isinstance(record.get(leg), dict) else {}
                visual = visual or (
                    leg_entry.get("status") == "ok" and _existing_relative(directory, leg_entry.get("path"))
                )
            if visual or numeric:
                grades.add("layout/text only (oracle capture, default view state)")
            records.append(
                OracleRecord(
                    name=str(record.get("view_name") or record.get("view_url_name") or ""),
                    kind=_declared_kind(record),
                    workbook=_declared_workbook(record, payload),
                    visual=bool(visual),
                    numeric=bool(numeric),
                )
            )
    return records, grades


@dataclass(frozen=True)
class OracleEvidence:
    """Producer records resolved against the expected pages, refusing every ambiguity.

    Four guards, one per measured defect:

    * **Workbook.** A record must be tied to THIS unit's workbook by identity - LUID first, exact
      display name second, and a lossy name only when the key resolves uniquely on **BOTH** sides.
      Measured before that last guard: records declaring ``Bo ok`` and ``Bo-ok`` were both admitted to
      unit workbook ``Book``, because uniqueness was checked among unit workbooks only. A record
      whose workbook CANNOT be established certifies nothing and is counted in ``unattributed`` -
      see :func:`_admissible_oracle_records`, where issue #450 lived.
    * **Kind.** A record may only satisfy a page of the SAME kind, and a record whose kind cannot be
      established satisfies nothing. See :class:`OracleRecord`.
    * **Exact spelling.** A view name and a page name are BOTH source-owned - they come from the same
      Tableau workbook with no filesystem in between - so nothing legitimately re-spells one into the
      other and there is no lossy fallback on names at all. (Round 6 permitted a uniqueness-guarded
      one; round 7 removed it, because a guard on a fallback that has no mechanism behind it only
      narrows an unjustified match.)
    * **Multiplicity.** Two records answering to one page name settle nothing, and say so.

    ⚠️ **The KIND half of issue #438 is CLOSED here**, so the runtime caveat that disclosed it while
    it was open is gone with it - a disclosure that outlives its gap manufactures doubt exactly as
    falsely as a missing one manufactures confidence.
    [#450](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/450) is closed too:
    :func:`_declared_workbook` read a ``workbook`` key no producer writes, so on a live capture every
    record arrived ownerless (measured 360 of 360) and was **admitted anyway**. It now reads the
    fields the producers do write - ``workbook_luid``/``workbook_name`` for an oracle capture,
    ``source_workbook_sha256`` for a reference one - and an unestablished workbook is a refusal.
    ``loosely_attributed`` and :func:`_oracle_caveats` remain, because the lossy NAME route is still
    reachable for a unit whose LUID cannot be established.
    """

    by_exact: dict[tuple[str, str], list[OracleRecord]]
    unattributed: int
    kindless: int
    admitted: int
    #: Records admitted on a LOSSY workbook key rather than an exact one (issue #450).
    loosely_attributed: list[str]
    foreign: tuple[str, ...]

    def evidence_for(self, page: dict[str, Any]) -> tuple[OracleRecord | None, str | None]:
        """``(record, refusal)`` for one expected page. At most one of the two is ever set."""
        exact = self.by_exact.get((str(page.get("kind")), page["name"]), [])
        if len(exact) == 1:
            return exact[0], None
        if exact:
            return None, f"{len(exact)} producer records are named {page['name']!r}"
        return None, None


def _unit_source_claims(target: Path) -> tuple[list[str], list[str]]:
    """``(recorded path/name claims, LUID claims)`` about the Tableau source this unit was built from.

    Two independent producers record it, and both are read: the handover slice's
    ``workbook.source_id`` (a run-root-relative path) and ``migration-spec.json``'s
    ``source.file_name`` (a bare basename). Every LUID claim must AGREE - see
    :func:`object_identity.agreed_luid`.

    ⚠️ Both are parsed with :func:`object_identity.persisted_stem`, never ``Path(...).stem``. A real
    slice records ``_runs\\407-...\\assets\\<luid>_HR_Dashboard.twbx``; on POSIX those backslashes are
    ordinary characters, so ``Path`` returns the whole string, the LUID prefix is invisible and the
    unit ends up with no machine identity - on Linux CI only. Round-1 review of PR #454 measured
    exactly that divergence, which is the worst possible shape for a safety guard.
    """
    names: list[str] = []
    for _path, _name, payload in _handover_workbooks(target)[0]:
        if isinstance(payload, dict) and payload.get("source_id"):
            names.append(str(payload["source_id"]))
    spec = _migration_spec(target)
    declared = (_json_object(spec) or {}).get("source") if spec is not None else None
    if isinstance(declared, dict) and declared.get("file_name"):
        names.append(str(declared["file_name"]))
    return names, [oid.harvest_luid(oid.persisted_stem(name)) or "" for name in names]


def _unit_source_sha256(target: Path) -> str | None:
    """sha256 of this unit's Tableau source asset, or None when it cannot be located.

    This is the machine identity a `reference/manifest.json` actually carries
    (``source_workbook_sha256``), so without it such a record could only be admitted on something
    weaker - which is precisely what round-1 review of PR #454 refused. Returning None is therefore
    fail-CLOSED by design: an unlocatable source means a sha-bearing record certifies nothing, and
    the refusal is counted in ``unattributed_evidence`` rather than dropped.
    """
    unit = _unit_dir(target)
    bases = [unit, target, unit.parent, unit.parent.parent]
    for recorded in _unit_source_claims(target)[0]:
        basename = oid.persisted_name(recorded)
        if not basename:
            continue
        candidates = [Path(recorded)]
        candidates += [base / sub / basename for base in bases for sub in ("assets", ".")]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return hashlib.sha256(candidate.read_bytes()).hexdigest()
            except OSError:
                continue
    return None


def _unit_workbook_identities(target: Path) -> list[oid.WorkbookIdentity]:
    """This unit's workbook, once per artifact stem, on every axis it can establish.

    The LUID comes from ``harvest_estate_assets.py``'s ``<luid>_<sanitized-name>`` filename - this
    repo's own record of which published workbook it downloaded - as recorded by the handover slice
    AND the migration spec. Every claim must agree (:func:`object_identity.agreed_luid`): a unit
    whose records name two different source workbooks has no single workbook identity, and picking
    the first would be the #438 coin toss.

    The sha256 is the unit's own source bytes, so a `reference/manifest.json`'s
    ``source_workbook_sha256`` is CHECKED rather than believed - or, when the source cannot be
    located, refused.

    One identity PER STEM rather than one identity with several names, so the machine axes are
    consulted first for each of them. That ordering is the guard: a record whose LUID disagrees is
    foreign against every stem, and cannot be re-admitted by a stem whose display name happens to
    match.
    """
    stems, _ = _unit_workbook_keys(target)
    luid = oid.agreed_luid(*_unit_source_claims(target)[1])
    sha = _unit_source_sha256(target)
    return [oid.WorkbookIdentity(luid=luid, name=stem, sha256=sha) for stem in sorted(stems)] or [
        oid.WorkbookIdentity(luid=luid, sha256=sha)
    ]


def _attribute_record(unit_ids: list[oid.WorkbookIdentity], record: OracleRecord) -> oid.Attribution:
    """How ``record`` ties to this unit: the first admission, else the strongest refusal.

    A unit may ship artifacts under several stems, so every candidate identity is tried. Ordering the
    result - admission, then ``foreign``, then ``unknown`` - matters because ``unknown`` against one
    stem must never mask a ``foreign`` verdict reached against another: a record we could compare and
    reject is a stronger statement than one we could not compare at all.
    """
    verdicts = [identity.attribute(record.workbook) for identity in unit_ids]
    if not verdicts:
        verdicts = [oid.WorkbookIdentity().attribute(record.workbook)]
    return next(
        (verdict for verdict in verdicts if verdict.admitted),
        next((verdict for verdict in verdicts if verdict.route == oid.WB_FOREIGN), verdicts[0]),
    )


def _admissible_oracle_records(
    records: list[OracleRecord], unit_ids: list[oid.WorkbookIdentity]
) -> tuple[list[OracleRecord], list[str], int, int, list[str]]:
    """``(admissible, foreign, unattributed, kindless, loosely_attributed)`` after both guards."""
    unit_index: NormalizedIndex[str] = NormalizedIndex()
    for identity in unit_ids:
        if identity.name:
            unit_index.add(identity.name, identity.name)
    producer_index: NormalizedIndex[str] = NormalizedIndex()
    for declared in sorted({record.workbook.name for record in records if record.workbook.name}):
        producer_index.add(declared, declared)

    admissible: list[OracleRecord] = []
    foreign: list[str] = []
    loosely_attributed: list[str] = []
    unattributed = kindless = 0
    for record in records:
        verdict = _attribute_record(unit_ids, record)
        if not verdict.admitted:
            if verdict.route == oid.WB_UNKNOWN:
                # ⚠️ Issue #450: this used to `unattributed += 1` and then FALL THROUGH to
                # `admissible`, so a record whose producing workbook could not be established was
                # admitted anyway - and because the field this gate read was one no producer writes,
                # that was every record on a real capture (360 of 360). Round-1 review of PR #454
                # found the same shape once more, wearing a different hat: a `reference/` manifest
                # inside the unit skipped this guard entirely on the strength of its LOCATION.
                # Refusing is the fix in both cases; the count stays, because a refusal nobody can
                # see is not a guard.
                unattributed += 1
                continue
            rescued = _lossy_workbook_rescue(verdict, record, unit_index, producer_index)
            if rescued is None:
                foreign.append(record.workbook.describe())
                continue
            loosely_attributed.append(rescued)
        if record.kind is None:
            kindless += 1
            continue
        admissible.append(record)
    return admissible, foreign, unattributed, kindless, loosely_attributed


def _lossy_workbook_rescue(
    verdict: oid.Attribution,
    record: OracleRecord,
    unit_index: NormalizedIndex[str],
    producer_index: NormalizedIndex[str],
) -> str | None:
    """The declared name a LOSSY workbook key may still admit, or None to refuse.

    The unit side is a filesystem-sanitised artifact stem, so a lossy fallback is legitimate on the
    NAME axis - but only when the key resolves uniquely on **BOTH** sides. Measured before that:
    records declaring ``Bo ok`` and ``Bo-ok`` were both admitted to unit workbook ``Book``, because
    uniqueness was checked among unit workbooks only. Uniqueness of a lossy key on one side is not
    identity.

    ⚠️ **Only a NAME disagreement is rescuable.** If a MACHINE axis - the LUID or the source hash -
    was compared and disagreed, or was claimed by the record and unanswerable by the unit, this
    returns None unconditionally: a stronger axis that has already answered must not be overridden by
    a weaker one, which is the identity-loss join
    (:meth:`object_identity.WorkbookIdentity.attribute`) that both halves of #450 grew out of.
    """
    name = record.workbook.name
    if verdict.route != oid.WB_FOREIGN or verdict.axis != oid.WB_NAME or not name:
        return None
    if unit_index.unique(name) is None or producer_index.unique(name) is None:
        return None
    return name


def _resolve_oracle_evidence(
    records: list[OracleRecord], candidates: list[dict[str, Any]], unit_ids: list[oid.WorkbookIdentity]
) -> OracleEvidence:
    """Index producer records against the expected pages without losing a collision."""
    _ = candidates
    admissible, foreign, unattributed, kindless, loosely = _admissible_oracle_records(records, unit_ids)
    by_exact: dict[tuple[str, str], list[OracleRecord]] = {}
    for record in admissible:
        by_exact.setdefault((str(record.kind), record.name), []).append(record)
    return OracleEvidence(
        by_exact=by_exact,
        unattributed=unattributed,
        kindless=kindless,
        admitted=len(admissible),
        loosely_attributed=loosely,
        foreign=tuple(sorted(set(foreign))),
    )


def check_oracle_coverage(target: Path, reference_dir: Path | None, oracle_dir: Path | None) -> dict[str, Any]:
    """Per-page visual/numeric oracle coverage, with grade.

    The denominator is the expected TABLEAU page set, never the emitted PBIR pages. It used to read
    ``expected_pages(target) or actual_pages(target)``: whenever the expected set came back
    empty/falsy - which, before :func:`expected_pages` counted orphan worksheets, was every workbook
    with no dashboards (19 of 43 in a real 2.339.0 estate run) and is still every engine bundle,
    since a bundle carries no ``migration-spec.json`` - the denominator silently became the very
    artifact being graded and coverage reported a perfect score regardless of what was missing.
    There is no fallback now: an expected set that cannot be established is a blocking
    ``NOT_CHECKED``, never a PASS.

    Only a page whose omission a HUMAN accepted is removed from the denominator. An engine-declared
    omission is not: ``tier: "empty"`` reports what the engine did, and letting it shrink this
    denominator too meant a unit missing a page reported validation-grade coverage of everything it
    still had. A signed page-parity exemption is the only thing that takes a page out.

    An oracle entry names an object without saying what KIND it is, so it may only satisfy a page
    whose name exactly ONE expected page claims. Measured before this: with a dashboard ``Sales`` and
    a worksheet ``Sales`` both expected, one reference row was counted as 2-of-2 coverage.
    """
    expectation = page_expectation(target)
    reference, reference_grades = _reference_oracles(target, reference_dir)
    oracle, oracle_grades = _oracle_capture_oracles(target, oracle_dir)
    if not expectation["assessable"]:
        return _oracle_not_assessable(f"cannot assess oracle coverage: {expectation['reason']}")
    accepted = _oracle_excluded_omissions(expectation, load_exemptions(target)["entries"])
    excluded_keys = {_page_key(page) for page in accepted}
    pages = [page for page in expectation["candidates"] if _page_key(page) not in excluded_keys]
    if not pages:
        return _oracle_not_assessable(
            "cannot assess oracle coverage: every expected Tableau page is a signed omission or owes "
            "no output, so there is no page left to hold against a reference",
            excluded=accepted,
        )
    evidence = _resolve_oracle_evidence(reference + oracle, pages, _unit_workbook_identities(target))
    rows = [_oracle_row(page, expectation["index"], evidence) for page in pages]
    visual_missing = [row["page"] for row in rows if not row["visual"]]
    numeric_missing = [row["page"] for row in rows if not row["numeric"]]
    return {
        "id": "oracle-coverage",
        "status": STATUS_NOT_CHECKED if visual_missing or numeric_missing else STATUS_PASS,
        "pages": len(rows),
        "visual_present": len(rows) - len(visual_missing),
        "numeric_present": len(rows) - len(numeric_missing),
        "visual_missing": visual_missing,
        "numeric_missing": numeric_missing,
        "contested_names": [row["page"]["name"] for row in rows if row["contested"]],
        "refused_evidence": [row["refusal"] for row in rows if row["refusal"]],
        "foreign_workbook_evidence": list(evidence.foreign),
        "unattributed_evidence": evidence.unattributed,
        "kindless_evidence": evidence.kindless,
        # ⚠️ Reported because the kind guard is otherwise UNKILLABLE: dropping the record would also
        # be masked by the `(kind, name)` lookup key never matching a kind-less record, so a test
        # could not tell the guard from its sibling. Vacuity mode 3 - pin it independently or delete
        # it. This is the independent pin, and it is useful in its own right: an operator can tell
        # "nobody captured that page" from "the capture could not say what it was a picture of".
        "admitted_evidence": evidence.admitted,
        "excluded_omissions": accepted,
        "grade": _oracle_grade(reference_grades | oracle_grades, evidence),
        "known_gap_caveats": _oracle_caveats(rows, evidence),
        "rows": rows,
    }


def _oracle_caveats(rows: list[dict[str, Any]], evidence: OracleEvidence) -> list[str]:
    """What this oracle verdict CANNOT establish, naming the objects it applies to (issue #450).

    ⚠️ Written to answer "which PASS should I distrust", not "is this tool imperfect". A general
    disclaimer on every run is noise an operator learns to skip, and it would fire hardest on the runs
    where nothing was certified and therefore nothing is at risk. So each caveat is emitted **only
    when a page actually took the evidence it describes**, and it names those objects.

    ⚠️ **The kind caveat that used to head this list is GONE, because its gap is closed.** Evidence
    now carries ``dashboard``/``worksheet`` and a record whose kind cannot be established certifies
    nothing (:class:`OracleRecord`), so printing "kind not established" would manufacture doubt
    exactly as falsely as omitting it once manufactured confidence. A disclosure is only honest while
    its gap is open; retiring it is part of the fix, not a separate cleanup.

    What remains is [#450](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/450)'s
    residual: a record admitted through the LOSSY workbook name is a weaker join than an exact match
    or a LUID. Both sides of that key are checked for uniqueness, so it is not the coin toss #438
    described - but it is the route a unit takes whenever its own workbook LUID cannot be
    established, and a reader deciding which PASS to distrust should be told which ones rest on it.
    """
    certified = [row["page"]["name"] for row in rows if row["visual"] or row["numeric"]]
    caveats: list[str] = []
    if evidence.loosely_attributed and certified:
        workbooks = ", ".join(repr(name) for name in sorted(set(evidence.loosely_attributed)))
        caveats.append(
            f"⚠️ #450 WORKBOOK MATCHED LOOSELY: {len(evidence.loosely_attributed)} record(s) were "
            f"admitted on a normalized workbook name rather than a LUID or an exact name, so "
            f"{len(certified)} certified page(s) rest on a lossier join than an exact workbook "
            f"match: {workbooks}"
        )
    return caveats


def _oracle_grade(grades: set[str], evidence: OracleEvidence) -> str:
    """The grade string, saying plainly when evidence could not be tied to a workbook OR to a kind.

    Both caveats are printed rather than inferred. A discarded record is invisible in the coverage
    numbers - the page simply has no evidence - so without this the reader cannot tell "nobody
    captured that page" from "the capture could not say what it was a picture of".
    """
    grade = ", ".join(sorted(grades)) or "not checked (no oracle manifest found)"
    if evidence.unattributed:
        grade += (
            f" (⚠️ {evidence.unattributed} record(s) establish no producing workbook and certify "
            "nothing; a capture must carry workbook_luid, or the manifest a source_workbook_sha256)"
        )
    if evidence.kindless:
        grade += (
            f" (⚠️ {evidence.kindless} record(s) establish no dashboard/worksheet kind and "
            "certify nothing; re-capture with view typing - scripts/tableau_view_types.py)"
        )
    return grade


def _oracle_row(page: dict[str, Any], index: Any, evidence: OracleEvidence) -> dict[str, Any]:
    """Coverage for one expected page. A contested name or an ambiguous match takes no evidence."""
    contested = _claim(index, page["name"]).outcome != oid.UNIQUE
    record, refusal = (None, None) if contested else evidence.evidence_for(page)
    return {
        "page": page,
        "contested": contested,
        "refusal": refusal,
        "visual": bool(record and record.visual),
        "numeric": bool(record and record.numeric),
    }


def _attribution_ambiguous(expectation: dict[str, Any], signatures: SignatureResolution) -> bool:
    """Whether any omission can be attributed at all, computed ONCE for both halves of the gate.

    Page parity subtracted accounted-for ``extra:`` pages before deciding this; the oracle side did
    not, so the same unit was ambiguous in one half and not in the other and a signed omission was
    excluded from one denominator but not the other. One predicate, one answer.
    """
    unaccounted = [page for page in expectation["unmatched_rendered"] if id(page) not in signatures.accounted_extra]
    return bool(unaccounted) or bool(expectation["contested_names"])


def _oracle_excluded_omissions(expectation: dict[str, Any], entries: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Omissions that owe no oracle evidence - the SAME dispositions page parity does not fail on.

    Computed from :func:`_disposition_omissions`, deliberately, so the two checks cannot drift apart
    again. Round 4 measured them disagreeing: page parity accepted a ``no-source-content`` omission
    while this denominator still demanded a visual and a numeric oracle for it, so a unit could PASS
    parity and be NOT_CHECKED here for the same page. A page declared to owe no output owes no
    picture of that output either.

    ⚠️ The caller removes these by :func:`_page_key`, never by display name. Round 5 measured the
    name set: a SIGNED dashboard ``Sales`` removed the UNSIGNED worksheet ``Sales`` from the
    denominator too, so the check reported ``pages=0`` and claimed every expected page was accounted
    for while listing only one exclusion.

    An engine-DECLARED omission is still not here: ``tier: "empty"`` reports what the engine did, not
    what anyone agreed to ship.
    """
    ambiguous = _attribution_ambiguous(expectation, signatures := _resolve_signatures(expectation, entries))
    return [
        row
        for row in _disposition_omissions(expectation, signatures, ambiguous)
        if row["disposition"] in OMISSIONS_OWING_NOTHING
    ]


def _oracle_not_assessable(detail: str, excluded: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Blocking 'cannot assess' row: an unestablished expected set never reads as coverage.

    ``excluded`` is carried through so the reason a denominator emptied stays visible. Without it the
    early return dropped the list, and a page correctly accepted by BOTH halves of the gate looked
    like a disagreement between them - found by the estate cross-check, not by a test.
    """
    return {
        "id": "oracle-coverage",
        "status": STATUS_NOT_CHECKED,
        "detail": detail,
        "pages": 0,
        "visual_present": 0,
        "numeric_present": 0,
        "visual_missing": [],
        "numeric_missing": [],
        "contested_names": [],
        "excluded_omissions": excluded or [],
        "grade": "not checked (expected page set could not be established)",
        "rows": [],
    }


def _gate_command(gate: Gate, target: Path, json_path: Path | None = None) -> list[str]:
    """Native checker command for reruns and subprocess execution."""
    argv = [sys.executable, str(SCRIPT_DIR / gate.script), str(target), *gate.args]
    if json_path is not None:
        argv.extend(["--json", str(json_path), "--quiet"])
    return argv


def _command_text(argv: list[str]) -> str:
    """Human-rerunnable command text."""
    return " ".join(f'"{part}"' if " " in part else part for part in argv)


def _gate_result(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    gate: Gate,
    target: Path,
    proc: subprocess.CompletedProcess[str],
    payload: dict[str, Any],
    native_status: str,
    detail: str | None = None,
) -> dict[str, Any]:
    """Normalize one native checker result without fail-open fallthroughs."""
    if native_status in gate.pass_statuses and proc.returncode in gate.pass_exit_codes:
        status = STATUS_PASS
    elif native_status in gate.finding_statuses and proc.returncode in gate.finding_exit_codes:
        status = STATUS_FINDINGS
    elif native_status in gate.not_checked_statuses or proc.returncode in gate.not_checked_exit_codes:
        status = STATUS_NOT_CHECKED
    elif not gate.writes_json and proc.returncode in gate.finding_exit_codes:
        status = STATUS_FINDINGS
    else:
        status = STATUS_NOT_CHECKED
        detail = detail or f"unexpected native status/exit combination: {native_status}/{proc.returncode}"
    return {
        "id": gate.check_id,
        "status": status,
        "native_status": native_status,
        "native_exit": proc.returncode,
        "native_command": _command_text(_gate_command(gate, target)),
        "detail": detail,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "payload": payload,
    }


def _run_cli_gate(gate: Gate, target: Path, output_dir: Path) -> dict[str, Any]:  # pylint: disable=too-many-return-statements
    json_path = output_dir / f"{gate.check_id}.json" if gate.writes_json else None
    argv = _gate_command(gate, target, json_path)
    try:
        proc = _run_simple(argv)
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(argv, 124, exc.stdout or "", exc.stderr or "")
        return _gate_result(gate, target, proc, {"status": "ERROR"}, "ERROR", "native checker timed out")
    if not gate.writes_json:
        if proc.returncode == 0:
            return _gate_result(gate, target, proc, {}, "OK")
        detail = "native checker exited nonzero without JSON"
        if "NOTHING CHECKED" in f"{proc.stdout}\n{proc.stderr}":
            return _gate_result(gate, target, proc, {"status": "ERROR"}, "ERROR", detail)
        return _gate_result(gate, target, proc, {}, "FINDINGS", detail)
    if json_path is None or not json_path.is_file():
        proc_status = "ERROR" if proc.returncode != 0 else "UNKNOWN"
        return _gate_result(gate, target, proc, {"status": proc_status}, proc_status, "native JSON output missing")
    try:
        payload = _read_json(json_path)
    except (OSError, json.JSONDecodeError) as exc:
        return _gate_result(
            gate,
            target,
            proc,
            {"status": "ERROR", "reason": f"unreadable JSON output: {exc}"},
            "ERROR",
            "native JSON output unreadable",
        )
    if not isinstance(payload, dict):
        return _gate_result(gate, target, proc, {"status": "ERROR"}, "ERROR", "native JSON output is not an object")
    native_status = str(payload.get("status") or "UNKNOWN")
    return _gate_result(gate, target, proc, payload, native_status)


def _stub_findings(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[str, str, set[str]]]]:
    """``(stubs, findings)`` from a stub-measure gate payload; the key ties a stub to its finding."""
    stubs: list[dict[str, Any]] = []
    findings: list[tuple[str, str, set[str]]] = []
    models = payload.get("models", []) if isinstance(payload.get("models"), list) else []
    for model_index, model in enumerate(models):
        for finding_index, finding in enumerate(model.get("findings", []) if isinstance(model, dict) else []):
            canonical = f"{finding.get('kind')}:{finding.get('table')}[{finding.get('name')}]"
            aliases = {f"{finding.get('table')}[{finding.get('name')}]", str(finding.get("name") or "")}
            key = f"{model_index}#{finding_index}"
            findings.append((key, canonical, aliases))
            stubs.append({"canonical": canonical, "finding": finding, "key": key, "exempted": False})
    return stubs, findings


def _apply_stub_exemptions(check: dict[str, Any], exemptions: dict[str, Any]) -> dict[str, Any]:
    payload = check.get("payload") if isinstance(check.get("payload"), dict) else {}
    stubs, findings = _stub_findings(payload)
    exempted, contested = resolve_exemptions(exemptions["entries"], "stub-measures", findings)
    for stub in stubs:
        stub["exempted"] = stub["key"] in exempted
    unexempted = [stub for stub in stubs if not stub["exempted"]]
    check["stub_exemptions"] = len(stubs) - len(unexempted)
    check["unexempted_stubs"] = len(unexempted)
    check["contested_exemptions"] = contested
    if check["native_status"] == "STUBS":
        check["status"] = STATUS_FINDINGS if unexempted else STATUS_PASS
    return check


def _path_ceiling_verdict_clause(payload: dict[str, Any], budget: Any, root_length: Any) -> str:
    """The status-aware half of the summary: which direction this bundle breaks in.

    A passing scan and a breaching one need OPPOSITE sentences. "A shorter install root may pass" is
    true only of a breach; said of a pass it is vacuous, and it leaves the fact that a LONGER root
    will breach unstated - which is exactly the risk a passing row has to carry.
    """
    if not isinstance(budget, int):
        return "root budget unknown, so relocation safety is unknown"
    if budget < 0:
        return f"NO installation root can hold this tree - its path tails alone exceed the ceiling by {-budget}"
    if payload.get("status") == "ok":
        return (
            f"fits here with {budget} characters of headroom: any installation root LONGER than "
            f"{budget} characters WILL breach"
        )
    return (
        f"needs an installation root of at most {budget} characters and this one is {root_length}, "
        f"so a shorter installation root may pass"
    )


def _annotate_path_ceiling(check: dict[str, Any]) -> dict[str, Any]:
    """Carry the numbers that make a path-ceiling verdict judgeable into the facade row.

    The native gate prints them; with ``--quiet`` (how the facade runs every gate) it prints only a
    verdict line, so without this the row is a bare verdict with no way to tell a genuinely fragile
    bundle from a deep checkout. ``root_budget`` is the portable number - the longest installation
    root this tree still tolerates - and the verdict is measured against THIS checkout root, which is
    stated rather than left for the reader to infer.

    The numbers matter MOST on a PASS, which is why ``root_budget`` is also surfaced in the row
    headline (``_path_ceiling_budget_note``): ``_render_actionable_detail`` renders ``detail`` for
    non-clean rows only, so a passing bundle that breaks the moment it is relocated would otherwise
    print nothing but "PASS" and exit 0. Measured on a byte-identical tree: root length 65 -> ``ok``
    with budget 79; root length 94 -> ``over_ceiling``.
    """
    payload = check.get("payload")
    if not isinstance(payload, dict):
        return check
    counted = payload.get("counted")
    if not isinstance(counted, dict):
        # No census means the scan never ran (a usage error, an unwritable JSON). Synthesizing
        # "0 of 0 paths over ceiling" here would read as reassurance for a check that failed.
        return check
    longest = payload.get("longest") if isinstance(payload.get("longest"), dict) else {}
    budget = payload.get("root_budget")
    root_length = payload.get("root_length", "unknown")
    parts = [
        f"{counted.get('over_ceiling', 0)} of {counted.get('measured', 0)} paths over ceiling",
        f"longest {longest.get('length', 'unknown')}",
        f"root budget {budget if budget is not None else 'unknown'} at root length {root_length}",
    ]
    if counted.get("unknown"):
        parts.append(f"{counted['unknown']} unmeasurable")
    summary = "; ".join(parts) + " - " + _path_ceiling_verdict_clause(payload, budget, root_length)
    existing = check.get("detail")
    check["detail"] = f"{existing}; {summary}" if existing else summary
    check["root_budget"] = budget
    check["root_budget_is_tight"] = bool(payload.get("root_budget_is_tight"))
    check["shipping_root_budget_advisory"] = payload.get("shipping_root_budget_advisory")
    return check


def _path_ceiling_budget_note(check: dict[str, Any]) -> str:
    """The headline clause for a path-ceiling row, rendered at EVERY status including PASS.

    Always shown rather than only when ``root_budget_is_tight``, for three measured reasons.
    (1) For this gate the budget IS the result: PASS means "it fits HERE", and the budget is the only
    portable number in the row, so suppressing it makes the row say less than the scan found.
    (2) The tight flag would not have fired on the case that motivated this: a tree measured at root
    length 65 passed with budget 79 (``root_budget_is_tight`` False, advisory threshold 40) and
    breached at root length 94 - a 29-character relocation. A threshold tuned for "alarming" cannot
    also mean "safe to relocate".
    (3) A conditional number is unreadable in its absence: nothing printed cannot be told apart from
    a comfortable budget, a dropped annotation, or a gate that never ran. A number always present is
    self-verifying. The cost is one clause on one row of one gate, in ``all`` scope only.
    """
    budget = check.get("root_budget")
    if not isinstance(budget, int):
        return ""
    if budget < 0:
        return f"root budget {budget} - NO installation root can hold this tree"
    tight = " TIGHT" if check.get("root_budget_is_tight") else ""
    return f"root budget {budget}{tight} - breaches above a {budget}-char installation root"


def _run_simple(argv: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run a native checker in a fresh process and capture its exact exit code."""
    return subprocess.run(
        argv,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )  # noqa: S603


def _read_occlusion_payload(out: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Read detect_occlusion.py output, returning an error instead of guessing clean."""
    if not out.is_file():
        return [], "native JSON output missing"
    try:
        loaded = _read_json(out)
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"native JSON output unreadable: {exc}"
    if not isinstance(loaded, list):
        return [], "native JSON output is not a list"
    return loaded, None


def check_occlusion(target: Path, output_dir: Path) -> dict[str, Any]:
    """Run detect_occlusion.py over every shipping report, preserving per-report exits."""
    reports = shipping_reports(target)
    if not reports:
        return {"id": "occlusion", "status": STATUS_NOT_CHECKED, "detail": "no shipping report found"}
    findings = []
    native = []
    for index, report in enumerate(reports):
        out = output_dir / f"occlusion-{index}.json"
        argv = [sys.executable, str(SCRIPT_DIR / "detect_occlusion.py"), str(report), "--json", str(out)]
        try:
            proc = _run_simple(argv)
        except subprocess.TimeoutExpired as exc:
            proc = subprocess.CompletedProcess(argv, 124, exc.stdout or "", exc.stderr or "")
            payload, error = [], "native checker timed out"
        else:
            payload, error = _read_occlusion_payload(out)
        if proc.returncode not in {0, 1} and error is None and not payload:
            error = f"unexpected native exit without findings: {proc.returncode}"
        native.append(
            {
                "report": str(report),
                "native_exit": proc.returncode,
                "findings": payload,
                "error": error,
                "stderr": proc.stderr.strip(),
            }
        )
        findings.extend(payload)
    errors = [item for item in native if item["error"]]
    status = STATUS_FINDINGS if findings else (STATUS_NOT_CHECKED if errors else STATUS_PASS)
    return {
        "id": "occlusion",
        "status": status,
        "native_status": "OCCLUDED" if findings else ("ERROR" if errors else "OK"),
        "native_exit": max(item["native_exit"] for item in native),
        "native_command": f"{sys.executable} {SCRIPT_DIR / 'detect_occlusion.py'} <report.Report> --json <out.json>",
        "reports": native,
        "findings": len(findings),
        "detail": errors[0]["error"] if errors else None,
    }


def _load_ai_readiness() -> Any:
    """Load the skill-owned readiness checker without copying its logic here."""
    path = REPO_ROOT / ".github" / "skills" / "powerbi-ai-readiness" / "scripts" / "check_ai_readiness.py"
    spec = importlib.util.spec_from_file_location("unit_check_ai_readiness", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_ai_descriptions(target: Path) -> dict[str, Any]:
    """Reuse the description/domain readiness checker per discovered model."""
    models = shipping_models(target, include_standalone=True)
    if not models:
        return {"id": "ai-descriptions", "status": STATUS_NOT_CHECKED, "detail": "no semantic model found"}
    checker = _load_ai_readiness()
    rows = []
    for model in models:
        result = checker.audit_model(model)
        counts = result["counts"]
        total = sum(count["total"] for count in counts.values())
        described = sum(count["described"] for count in counts.values())
        gaps = result["categorical_gaps"]
        rows.append(
            {
                "model": str(model),
                "total": total,
                "described": described,
                "categorical_gaps": gaps,
                "status": "SKIPPED" if total == 0 else ("OK" if described == total and not gaps else "FINDINGS"),
            }
        )
    failing = [row for row in rows if row["status"] == "FINDINGS"]
    skipped = [row for row in rows if row["status"] == "SKIPPED"]
    if failing:
        status, native_status, native_exit = STATUS_FINDINGS, "FINDINGS", 1
    elif skipped:
        status, native_status, native_exit = STATUS_NOT_CHECKED, "SKIPPED", 3
    else:
        status, native_status, native_exit = STATUS_PASS, "OK", 0
    return {
        "id": "ai-descriptions",
        "status": status,
        "native_status": native_status,
        "native_exit": native_exit,
        "detail": "nothing measured (no tables, columns, or measures found)" if skipped and not failing else None,
        "models": rows,
    }


def check_ai_instructions(target: Path) -> dict[str, Any]:
    """Check CustomInstructions/qnaEnabled per model; no model is NOT_CHECKED, not PASS."""
    models = shipping_models(target, include_standalone=True)
    if not models:
        return {"id": "ai-instructions", "status": STATUS_NOT_CHECKED, "detail": "no semantic model found"}
    results = []
    for model in models:
        proc = _run_simple(
            [
                sys.executable,
                str(SCRIPT_DIR / "set_ai_instructions.py"),
                "--check",
                "--strict",
                "--model",
                str(model),
            ]
        )
        results.append(
            {
                "model": str(model),
                "native_exit": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        )
    failing = [result for result in results if result["native_exit"] != 0]
    return {
        "id": "ai-instructions",
        "status": STATUS_FINDINGS if failing else STATUS_PASS,
        "native_status": "FINDINGS" if failing else "OK",
        "native_exit": max(result["native_exit"] for result in results),
        "models": results,
    }


def check_cache_freshness(target: Path) -> dict[str, Any]:
    """Partial cache validity: file-backed cache existence + mtime only, never live-row proof."""
    models = shipping_models(target, include_standalone=True)
    if not models:
        return {
            "id": "cache-freshness",
            "status": STATUS_NOT_CHECKED,
            "verification": "PARTIAL",
            "detail": "no semantic model found",
        }
    rows = []
    for model in models:
        cache = model / ".pbi" / "cache.abf"
        tmdls = list((model / "definition").rglob("*.tmdl")) if (model / "definition").is_dir() else []
        newest = max((path.stat().st_mtime for path in tmdls), default=0.0)
        if not cache.is_file():
            state = "NO_CACHE"
        elif cache.stat().st_mtime < newest:
            state = "STALE"
        else:
            state = "FRESH_BY_MTIME"
        rows.append({"model": str(model), "status": state})
    stale = [row for row in rows if row["status"] == "STALE"]
    missing = [row for row in rows if row["status"] == "NO_CACHE"]
    status = STATUS_FINDINGS if stale else (STATUS_NOT_CHECKED if missing else STATUS_PASS)
    detail = "mtime-only partial check; PASS means fresh by mtime only, not proven data validity"
    return {
        "id": "cache-freshness",
        "status": status,
        "verification": "PARTIAL",
        "detail": detail,
        "models": rows,
        "stale": len(stale),
        "missing": len(missing),
    }


def claimed_only_checks() -> list[dict[str, Any]]:
    """Phases #271 says are not machine-verifiable today."""
    return [
        {
            "id": "visual-layer-done",
            "status": STATUS_NOT_CHECKED,
            "verification": "CLAIMED_ONLY",
            "detail": "no machine-readable completion artifact exists",
        },
        {
            "id": "visual-comparison-done",
            "status": STATUS_NOT_CHECKED,
            "verification": "CLAIMED_ONLY",
            "detail": "validator judgement is not a gate artifact today",
        },
        {
            "id": "finalized",
            "status": STATUS_NOT_CHECKED,
            "verification": "CLAIMED_ONLY",
            "detail": "sign-off is not represented by a verifiable artifact today",
        },
    ]


def check_desktop_orphans(target: Path) -> dict[str, Any]:
    """Fail only on run-owned Desktop instances left live; unrelated instances are ignored."""
    result = check_desktop_orphans_module.audit_target(target)
    native_status = str(result.get("status") or check_desktop_orphans_module.STATUS_ERROR)
    if native_status == check_desktop_orphans_module.STATUS_OK:
        status, native_exit = STATUS_PASS, check_desktop_orphans_module.EXIT_OK
    elif native_status == check_desktop_orphans_module.STATUS_ORPHANS:
        status, native_exit = STATUS_FINDINGS, check_desktop_orphans_module.EXIT_ORPHANS
    else:
        status, native_exit = STATUS_NOT_CHECKED, check_desktop_orphans_module.EXIT_ERROR
    return {
        "id": "desktop-orphans",
        "status": status,
        "native_status": native_status,
        "native_exit": native_exit,
        "native_command": _command_text([sys.executable, str(SCRIPT_DIR / "check_desktop_orphans.py"), str(target)]),
        **result,
    }


def check_engine_receipt(target: Path) -> dict[str, Any]:
    """Engine receipt presence and drift; check_engine_receipts.py remains the drift authority."""
    receipt = target / "engine-output-receipt.json"
    if not receipt.is_file():
        return {"id": "engine-receipt", "status": STATUS_NOT_CHECKED, "detail": "no engine-output-receipt.json"}
    proc = _run_simple([sys.executable, str(SCRIPT_DIR / "check_engine_receipts.py"), "--root", str(target)], 120)
    try:
        payload = _read_json(receipt)
    except (OSError, json.JSONDecodeError) as exc:
        return {"id": "engine-receipt", "status": STATUS_FINDINGS, "detail": f"unreadable receipt: {exc}"}
    status = STATUS_PASS if proc.returncode == 0 else STATUS_FINDINGS
    return {
        "id": "engine-receipt",
        "status": status,
        "native_status": "OK" if proc.returncode == 0 else "WARN",
        "native_exit": proc.returncode,
        "engine": payload.get("engine") if isinstance(payload, dict) else None,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _temp_json_dir(target: Path) -> Path:
    digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:16]
    path = REPO_ROOT / ".check-unit-scratch" / digest
    path.mkdir(parents=True, exist_ok=True)
    return path


def _remove_json_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    parent = path.parent
    try:
        parent.rmdir()
    except OSError:
        pass


def _all_check_ids() -> frozenset[str]:
    """Every check registered in the facade."""
    return MODEL_CHECK_IDS | REPORT_CHECK_IDS | INTEGRATION_CHECK_IDS | ALL_ONLY_CHECK_IDS


def _scope_check_ids(scope: str) -> frozenset[str]:
    """Checks that belong to one requested layer scope."""
    if scope == SCOPE_MODEL:
        return MODEL_CHECK_IDS | INTEGRATION_CHECK_IDS
    if scope == SCOPE_REPORT:
        return REPORT_CHECK_IDS | INTEGRATION_CHECK_IDS
    if scope == SCOPE_INTEGRATION:
        return INTEGRATION_CHECK_IDS
    return _all_check_ids()


def _in_scope(check_id: str, scope: str) -> bool:
    return check_id in _scope_check_ids(scope)


def _omitted_checks(scope: str) -> list[str]:
    """Checks not run in the requested scope, named so a scoped pass cannot look complete."""
    if scope == SCOPE_ALL:
        return []
    return sorted(_all_check_ids() - _scope_check_ids(scope))


def _append_cli_checks(
    checks: list[dict[str, Any]],
    target: Path,
    exemptions: dict[str, Any],
    scope: str,
    model_loc: ModelLocation,
) -> None:
    """Append native CLI-backed checks for the selected scope.

    When the unit's model is EXTERNAL (a shared datasource, checked in its own unit), the model-owned
    gates are recorded as EXTERNAL instead of being run against an absent model - otherwise eight gates
    would each report the model missing while it demonstrably resolves (issue #317). Following the hop
    is deliberately NOT the default: a model shared by N reports would be re-checked N times.
    """
    external = model_loc.state == MODEL_LOC_EXTERNAL
    cli_gates = [gate for gate in GATES if _in_scope(gate.check_id, scope)]
    if not cli_gates and not _in_scope("occlusion", scope):
        return
    output_dir = _temp_json_dir(target)
    try:
        for gate in cli_gates:
            if external and gate.check_id in MODEL_CHECK_IDS:
                checks.append(_external_model_check(gate.check_id, model_loc))
                continue
            check = _run_cli_gate(gate, target, output_dir)
            if gate.check_id == "stub-measures":
                check = _apply_stub_exemptions(check, exemptions)
            if gate.check_id == "path-ceiling":
                check = _annotate_path_ceiling(check)
            checks.append(check)
        if _in_scope("occlusion", scope):
            checks.append(check_occlusion(target, output_dir))
    finally:
        _remove_json_dir(output_dir)


def _append_model_readiness_checks(
    checks: list[dict[str, Any]], target: Path, scope: str, model_loc: ModelLocation
) -> None:
    """Append model readiness checks for the selected scope (EXTERNAL model is deferred, not missing)."""
    external = model_loc.state == MODEL_LOC_EXTERNAL
    for check_id, check_func in (
        ("ai-descriptions", check_ai_descriptions),
        ("ai-instructions", check_ai_instructions),
        ("cache-freshness", check_cache_freshness),
    ):
        if not _in_scope(check_id, scope):
            continue
        if external and check_id in MODEL_CHECK_IDS:
            checks.append(_external_model_check(check_id, model_loc))
        else:
            checks.append(check_func(target))


def run_all(
    target: Path,
    reference_dir: Path | None = None,
    oracle_dir: Path | None = None,
    scope: str = SCOPE_ALL,
) -> dict[str, Any]:
    """Run checks for one persona-owned scope, returning a normalized envelope."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope}")
    target = target.resolve()
    exemptions = load_exemptions(target)
    model_loc = _model_location(target)
    checks: list[dict[str, Any]] = []
    if exemptions["invalid"]:
        checks.append(
            {
                "id": "exemptions",
                "status": STATUS_FINDINGS,
                "invalid": exemptions["invalid"],
                "path": exemptions["path"],
            }
        )
    if _in_scope("page-parity", scope):
        page = check_page_parity(target, exemptions)
        checks.append(page)
        if page["status"] == STATUS_PRECONDITION_FAILED:
            return _finalize(target, checks, exemptions, scope=scope, stopped_after="page-parity")
    if _in_scope("oracle-coverage", scope):
        checks.append(check_oracle_coverage(target, reference_dir, oracle_dir))
    if _in_scope("engine-receipt", scope):
        checks.append(check_engine_receipt(target))
    if _in_scope("scaffold-partitions", scope):
        scaffold_check = check_scaffold_partitions(target, exemptions)
        if scaffold_check is not None:
            checks.append(scaffold_check)
    if _in_scope("field-bindings", scope) and model_loc.state in (MODEL_LOC_EXTERNAL, MODEL_LOC_BROKEN):
        checks.append(_model_reference_check(model_loc))
    _append_cli_checks(checks, target, exemptions, scope, model_loc)
    _append_model_readiness_checks(checks, target, scope, model_loc)
    if _in_scope("desktop-orphans", scope):
        checks.append(check_desktop_orphans(target))
    if scope == SCOPE_ALL:
        checks.extend(claimed_only_checks())
    return _finalize(target, checks, exemptions, scope=scope)


def _is_blocking_not_checked(check: dict[str, Any]) -> bool:
    """Whether a NOT_CHECKED row blocks exit 0 for the selected scope."""
    return check["status"] == STATUS_NOT_CHECKED and check.get("verification") != "CLAIMED_ONLY"


def _finalize(
    target: Path,
    checks: list[dict[str, Any]],
    exemptions: dict[str, Any],
    scope: str,
    stopped_after: str | None = None,
) -> dict[str, Any]:
    statuses = [check["status"] for check in checks]
    if STATUS_PRECONDITION_FAILED in statuses:
        status = STATUS_PRECONDITION_FAILED
        exit_code = EXIT_PRECONDITION_FAILED
    elif STATUS_FINDINGS in statuses:
        status = STATUS_FINDINGS
        exit_code = EXIT_FINDINGS
    elif any(_is_blocking_not_checked(check) for check in checks):
        status = STATUS_NOT_CHECKED
        exit_code = EXIT_NOT_CHECKED
    else:
        status = STATUS_AUTOMATED_PASS
        exit_code = EXIT_OK
    return {
        "version": 1,
        "target": str(target),
        "scope": scope,
        "omitted_checks": _omitted_checks(scope),
        "status": status,
        "exit_code": exit_code,
        "stopped_after": stopped_after,
        "exemptions": {
            "path": exemptions["path"],
            "accepted": len(exemptions["entries"]),
            "invalid": len(exemptions["invalid"]),
        },
        "checks": checks,
        "brownfield": inspect_brownfield(target),
    }


def _compact_identity(item: Any) -> str | None:
    """Best-effort one-line identity for a native finding payload object."""
    if not isinstance(item, dict):
        return None
    parts = []
    for key in ("severity", "status", "kind", "category", "entity", "property", "table", "name", "reason"):
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"{key}={value}")
    for key in ("path", "report", "model", "file"):
        value = item.get(key)
        if value not in (None, "", []):
            parts.append(f"evidence={value}")
            break
    return "; ".join(parts) if parts else None


def _payload_findings(payload: Any, limit: int = 5) -> list[str]:
    """Extract concrete finding identities from heterogeneous native JSON payloads."""
    findings: list[str] = []

    def walk(value: Any) -> None:
        if len(findings) >= limit:
            return
        if isinstance(value, dict):
            identity = _compact_identity(value)
            if identity and any(key in value for key in ("severity", "kind", "category", "reason", "path")):
                findings.append(identity)
            for child_key in ("findings", "unresolved", "skipped", "models", "reports", "worst_offenders"):
                child = value.get(child_key)
                if isinstance(child, list):
                    walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)
                if len(findings) >= limit:
                    break

    walk(payload)
    return findings


def _render_actionable_detail(check: dict[str, Any]) -> list[str]:
    """Human details for a non-clean check row."""
    if check["status"] not in {STATUS_FINDINGS, STATUS_NOT_CHECKED, STATUS_PRECONDITION_FAILED}:
        return []
    lines = [f"      suspected owner: {OWNER_HINTS.get(check['id'], 'unknown')}"]
    if check.get("detail"):
        lines.append(f"      detail: {check['detail']}")
    if check.get("native_command"):
        lines.append(f"      rerun: {check['native_command']}")
    for finding in _payload_findings(check.get("payload")):
        lines.append(f"      finding: {finding}")
    for path in check.get("invalid_handover_keys", [])[:3]:
        lines.append(f"      unreadable handover: {path}")
    if check.get("stderr"):
        lines.append(f"      stderr: {check['stderr'][:500]}")
    return lines


def _count_suffix(missing: int) -> str:
    return f"   [{missing} MISSING]" if missing else ""


def _declared_downgrade_count(check: dict[str, Any]) -> int:
    """Declared connection downgrades are accepted compromises, not indistinguishable clean passes."""
    if check.get("id") != "connection-fidelity":
        return 0
    payload = check.get("payload")
    if not isinstance(payload, dict):
        return 0
    declared = payload.get("declared_sources")
    return declared if isinstance(declared, int) and declared > 0 else 0


def _compromise_not_evaluated_count(report: dict[str, Any]) -> int:
    """Accepted-decision channels selected but not measured; unknown is not zero."""
    total = 0
    seen_connection_fidelity = False
    for check in report["checks"]:
        if check.get("id") != "connection-fidelity":
            continue
        seen_connection_fidelity = True
        if check.get("status") == STATUS_NOT_CHECKED:
            total += 1
    if (
        report.get("stopped_after")
        and not seen_connection_fidelity
        and _in_scope("connection-fidelity", report["scope"])
    ):
        total += 1
    return total


def _compromise_count(report: dict[str, Any]) -> int:
    """Human-accepted deviations that should stay visible even when they do not fail the run.

    A signature that accepted NOTHING is not a compromise. Page-parity dispositions its exemptions
    (``stale`` names a page that is present, ``ambiguous`` cannot be attributed while a rendered page
    is unaccounted for), and neither is applied - so both are subtracted here. Measured before this:
    a stale exemption was correctly not applied and the CLI still printed ``compromises=1``.
    """
    declared = sum(_declared_downgrade_count(check) for check in report["checks"])
    unapplied = sum(len(check.get("unapplied_exemptions") or []) for check in report["checks"])
    accepted = int(report.get("exemptions", {}).get("accepted", 0))
    return max(0, accepted - unapplied) + declared


def _blocking_count(report: dict[str, Any]) -> int:
    """Rows that prevent calling the selected scope complete."""
    return sum(
        1
        for check in report["checks"]
        if check["status"] in {STATUS_FINDINGS, STATUS_PRECONDITION_FAILED}
        or (_is_blocking_not_checked(check) and check.get("verification") != VERIFICATION_EXTERNAL)
    )


def _summary_counts(report: dict[str, Any]) -> tuple[dict[str, int], int, int, int]:
    """Counts behind the stable summary line and shape-guidance trigger.

    NOT_CHECKED rows split three ways so a deferred-to-another-unit model no longer inflates
    ``missing_input`` (issue #317): structural (claimed-only, not machine-verifiable), external (this
    unit's model lives elsewhere, by design), and missing_input (a genuinely absent input).
    """
    owner_findings: dict[str, int] = {}
    structural = 0
    missing_input = 0
    external = 0
    for check in report["checks"]:
        owner = OWNER_HINTS.get(check["id"], "unknown")
        if check["status"] in {STATUS_FINDINGS, STATUS_PRECONDITION_FAILED}:
            owner_findings[owner] = owner_findings.get(owner, 0) + 1
        if check["status"] == STATUS_NOT_CHECKED:
            verification = check.get("verification")
            if verification == VERIFICATION_CLAIMED_ONLY:
                structural += 1
            elif verification == VERIFICATION_EXTERNAL:
                external += 1
            else:
                missing_input += 1
    return owner_findings, structural, missing_input, external


def _summary_line(report: dict[str, Any]) -> str:
    """Stable one-line aggregate for comparing repeated gate runs."""
    owner_findings, structural, missing_input, external = _summary_counts(report)
    findings = ",".join(f"{owner}={count}" for owner, count in sorted(owner_findings.items())) or "none"
    return (
        "SUMMARY: "
        f"blockers={_blocking_count(report)}; "
        f"compromises={_compromise_count(report)}; "
        f"compromises_not_evaluated={_compromise_not_evaluated_count(report)}; "
        f"findings_by_owner={findings}; "
        f"not_checked_structural={structural}; "
        f"not_checked_external={external}; "
        f"not_checked_missing_input={missing_input}; "
        f"ladder={report['status']} exit={report['exit_code']}"
    )


def _render_brownfield(report: dict[str, Any]) -> list[str]:
    """Actionable read-only guidance for non-canonical or partial migration folders."""
    brownfield = report.get("brownfield") or {}
    _, _, missing_input, _ = _summary_counts(report)
    if brownfield.get("recognized_target_shape") and not brownfield.get("plan") and missing_input == 0:
        return []
    if missing_input == 0 and not brownfield.get("found_count"):
        return []
    lines = ["", "BROWNFIELD DISCOVERY (read-only):"]
    lines.extend(f"  {line}" for line in brownfield.get("expected_shape", []))
    if brownfield.get("engine_versions"):
        lines.append(f"  engine version(s) found: {', '.join(brownfield['engine_versions'])}")
    lines.append("  found instead:")
    for phase in brownfield.get("phases", []):
        status = phase.get("status")
        paths = phase.get("paths") or []
        if paths:
            lines.append(f"    - {phase['phase']}: {status}")
            lines.extend(f"        {path}" for path in paths)
        else:
            lines.append(f"    - {phase['phase']}: {status} ({phase.get('detail')})")
    if brownfield.get("plan"):
        lines.append("  reorganisation plan (not applied):")
        lines.extend(f"    - {item}" for item in brownfield["plan"])
    else:
        lines.append("  reorganisation plan (not applied): no recognised artifacts to place")
    return lines


# One guard-clause per bespoke row format; 8 formats means 8 returns, and the flat chain reads better
# than the nested elif it replaced (same rationale as _run_cli_gate above).
def _render_check_headline(check: dict[str, Any]) -> list[str]:  # pylint: disable=too-many-return-statements
    """The one-or-more headline lines for a single check row.

    Extracted from ``render`` because the per-gate special cases are a growing if/elif chain: adding
    the path-ceiling row pushed ``render`` to 13 branches against pylint's limit of 12 (R0912). This
    keeps each gate's bespoke headline in one named place instead of buying a waiver.
    """
    check_id = check["id"]
    status = check["status"]
    if check_id == "oracle-coverage" and "pages" in check:
        pages = check["pages"]
        lines = [
            f"  oracle coverage:  {check['visual_present']} of {pages} pages have a visual oracle"
            f"{_count_suffix(pages - check['visual_present'])}",
            f"                    {check['numeric_present']} of {pages} pages have a numeric oracle"
            f"{_count_suffix(pages - check['numeric_present'])}",
            f"                    grade: {check['grade']}  [{status}]",
        ]
        # A payload key nobody prints is not a disclosure. These say WHICH pages' PASS to distrust
        # and why; see issue #438 and :func:`_oracle_caveats`.
        lines.extend(f"                    {caveat}" for caveat in check.get("known_gap_caveats", []))
        return lines
    if check_id == "page-parity" and "expected_count" in check:
        lines = [
            f"  page pairing:      {check['emitted_count']} rebuilt PBIR page(s) for "
            f"{check['expected_count']} expected Tableau page(s)  [{status}]"
        ]
        for row in check.get("unsigned_omissions", [])[:5]:
            lines.append(f"                    OMITTED {row['kind']} {row['name']!r} [{row['disposition']}]")
        if check.get("applied_exemptions"):
            lines.append(f"                    signed omissions accepted: {len(check['applied_exemptions'])}")
        for row in check.get("unapplied_exemptions", [])[:3]:
            lines.append(f"                    exemption for {row['name']!r} accepted nothing [{row['disposition']}]")
        return lines
    native = f"native {check.get('native_status')} exit {check.get('native_exit')}"
    if check_id == "stub-measures" and "stub_exemptions" in check:
        return [
            f"  {check_id}: {status} ({native}; "
            f"{check['stub_exemptions']} exempted, {check['unexempted_stubs']} unexempted)"
        ]
    if check_id == "connection-fidelity" and _declared_downgrade_count(check):
        return [
            f"  {check_id}: {status} ({native}; {_declared_downgrade_count(check)} declared downgrade compromise(s))"
        ]
    if check_id == "scaffold-partitions" and "scaffold_exemptions" in check:
        return [
            f"  {check_id}: {status} ({check['scaffold_exemptions']} exempted, "
            f"{check['unexempted_scaffolds']} unexempted)"
        ]
    if check_id == "path-ceiling" and _path_ceiling_budget_note(check):
        return [f"  {check_id}: {status} ({native}; {_path_ceiling_budget_note(check)})"]
    if "native_exit" in check:
        return [f"  {check_id}: {status} ({native})"]
    detail = f" - {check['detail']}" if check.get("detail") else ""
    return [f"  {check_id}: {status}{detail}"]


def render(report: dict[str, Any]) -> str:
    """Human-readable unit verdict."""
    scope = report.get("scope", SCOPE_ALL)
    lines = [f"UNIT CHECK ({scope} scope): {report['status']} - {report['target']}"]
    if report.get("omitted_checks"):
        skipped = ", ".join(report["omitted_checks"])
        lines.append(f"  scoped automated checks only; omitted checks: {skipped}")
        lines.append("  scoped PASS is not unit completion or cross-layer sign-off")
    if report.get("stopped_after"):
        lines.append(f"  stopped after failed precondition: {report['stopped_after']}")
    for check in report["checks"]:
        lines.extend(_render_check_headline(check))
        lines.extend(_render_actionable_detail(check))
    ex = report["exemptions"]
    if ex["accepted"] or ex["invalid"]:
        lines.append(f"  documented why-not exemptions: {ex['accepted']} accepted, {ex['invalid']} invalid")
    lines.append(_summary_line(report))
    lines.extend(_render_brownfield(report))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path, help="migration unit, fabric folder, or engine bundle")
    parser.add_argument("--scope", choices=SCOPES, default=SCOPE_ALL, help="persona layer to check (default: all)")
    parser.add_argument("--json", type=Path, help="write the normalized unit-check envelope here")
    parser.add_argument("--reference-dir", type=Path, help="override reference/ directory containing manifest.json")
    parser.add_argument(
        "--oracle-dir",
        type=Path,
        help="override the oracle capture directory holding oracle-manifest.json "
        "(auto-discovered as _oracle/ or oracle/ beside the unit, target, or its parent)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress human-readable output")
    args = parser.parse_args(argv)

    if not args.target.is_dir():
        print(f"ERROR: not a directory: {args.target}", file=sys.stderr)
        return EXIT_USAGE
    report = run_all(args.target, reference_dir=args.reference_dir, oracle_dir=args.oracle_dir, scope=args.scope)
    if args.json:
        _write_json(args.json, report)
    if not args.quiet:
        print(render(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
