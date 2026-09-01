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
from typing import Any

import check_desktop_orphans as check_desktop_orphans_module
import read_handover
from bundle_corpus import shipping_models, shipping_reports
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


def _slug(text: str) -> str:
    """Lossy name normalization for matching Tableau/PBIR/oracle page labels."""
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


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


def expected_pages(target: Path) -> list[dict[str, str]] | None:
    """Expected Tableau pages: dashboards only, never worksheets."""
    spec_path = _migration_spec(target)
    if spec_path is None:
        return None
    try:
        payload = _read_json(spec_path)
    except (OSError, json.JSONDecodeError):
        return None
    pages = []
    for item in payload.get("dashboards", []):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("title") or item.get("id")
        if isinstance(name, str) and name.strip():
            pages.append({"id": str(item.get("id") or name), "name": name.strip()})
    return pages


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


def actual_pages(target: Path) -> list[dict[str, str]]:
    """PBIR pages from shipping reports, ordered by pages.json where available."""
    pages: list[dict[str, str]] = []
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


def _exempted(entries: list[dict[str, str]], check: str, item: str, aliases: set[str] | None = None) -> bool:
    wanted = {item, *(aliases or set())}
    normalized = {_slug(value) for value in wanted if value}
    return any(entry["check"] == check and _slug(entry["item"]) in normalized for entry in entries)


def _handover_workbooks(target: Path) -> tuple[list[tuple[Path, str, dict[str, Any]]], list[str]]:
    """Workbook payloads from handover slices/estate reports, using read_handover's resolver."""
    unit = _unit_dir(target)
    roots = []
    for candidate in (unit / "handover", target / "handover"):
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
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


def _scaffold_partition_rows(
    workbooks: list[tuple[Path, str, dict[str, Any]]], entries: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Classify scaffold rows plus workbook payloads whose key is missing/invalid."""
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    invalid: list[str] = []
    for path, workbook_name, workbook in workbooks:
        status, raw_rows = read_handover.partitions_needs_review_status(workbook)
        handover = f"{_display_path(path)}::{workbook_name}"
        if status == read_handover.PARTITION_REVIEW_MISSING:
            missing.append(handover)
        elif status == read_handover.PARTITION_REVIEW_INVALID:
            invalid.append(handover)
        elif status == read_handover.PARTITION_REVIEW_PRESENT:
            for row in raw_rows:
                item, aliases = _scaffold_row_identity(row, path)
                rows.append(
                    {
                        "handover": handover,
                        "table": str(row.get("table") or ""),
                        "reason": str(row.get("reason") or ""),
                        "item": item,
                        "exempted": _exempted(entries, "scaffold-partitions", item, aliases),
                    }
                )
    return rows, missing, invalid


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
    rows, missing, invalid = _scaffold_partition_rows(workbooks, exemptions["entries"])
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
        "missing_handover_keys": missing,
        "invalid_handover_keys": invalid,
    }


def check_page_parity(target: Path, exemptions: dict[str, Any]) -> dict[str, Any]:
    """Page-count parity precondition: expected Tableau dashboards vs emitted PBIR pages."""
    expected = expected_pages(target)
    actual = actual_pages(target)
    if expected is None:
        return {
            "id": "page-parity",
            "status": STATUS_NOT_CHECKED,
            "detail": "no migration-spec.json found, so expected Tableau dashboards are unknown",
            "expected_pages": None,
            "actual_pages": actual,
            "exemptions": [],
        }
    entries = exemptions["entries"]
    dropped = [page for page in expected if _exempted(entries, "page-parity", page["name"], {page["id"]})]
    effective_expected = [page for page in expected if page not in dropped]
    extra = max(0, len(actual) - len(effective_expected))
    extra_pages = actual[-extra:] if extra else []
    exempted_extra = [page for page in extra_pages if _exempted(entries, "page-parity", f"extra:{page['name']}")]
    unexempted_extra = [page for page in extra_pages if page not in exempted_extra]
    missing_count = max(0, len(effective_expected) - len(actual))
    missing_pages = effective_expected[-missing_count:] if missing_count else []
    unexempted_missing = [
        page for page in missing_pages if not _exempted(entries, "page-parity", page["name"], {page["id"]})
    ]
    status = STATUS_PASS if not unexempted_missing and not unexempted_extra else STATUS_PRECONDITION_FAILED
    return {
        "id": "page-parity",
        "status": status,
        "expected_count": len(expected),
        "effective_expected_count": len(effective_expected),
        "actual_count": len(actual),
        "expected_pages": expected,
        "actual_pages": actual,
        "exemptions": dropped + exempted_extra,
        "unexempted_missing": unexempted_missing,
        "unexempted_extra": unexempted_extra,
    }


def _reference_dirs(target: Path, explicit: Path | None) -> list[Path]:
    unit = _unit_dir(target)
    candidates = [explicit] if explicit else [unit / "reference", target / "reference"]
    return [path.resolve() for path in candidates if path and path.exists()]


def _oracle_dirs(target: Path, explicit: Path | None) -> list[Path]:
    unit = _unit_dir(target)
    candidates = [explicit] if explicit else [unit / "_oracle", target / "_oracle", target.parent / "_oracle"]
    return [path.resolve() for path in candidates if path and path.exists()]


def _existing_relative(base: Path, rel: str | None) -> bool:
    return bool(rel) and (base / rel).is_file()


def _reference_oracles(target: Path, reference_dir: Path | None) -> tuple[dict[str, dict[str, bool]], set[str]]:
    found: dict[str, dict[str, bool]] = {}
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
            name = str(dashboard.get("name") or "")
            key = _slug(name)
            entry = found.setdefault(key, {"visual": False, "numeric": False})
            for state in dashboard.get("states", []):
                if not isinstance(state, dict):
                    continue
                caps = {str(cap) for cap in state.get("capabilities", []) if isinstance(cap, str)}
                if caps:
                    grades.add("validation-grade" if "validation_grade" in caps else "/".join(sorted(caps)))
                entry["visual"] = entry["visual"] or _existing_relative(directory, state.get("image"))
                numeric = state.get("numeric_oracle")
                entry["numeric"] = entry["numeric"] or (
                    isinstance(numeric, str) and _existing_relative(directory, numeric)
                )
    return found, grades


def _oracle_capture_oracles(target: Path, oracle_dir: Path | None) -> tuple[dict[str, dict[str, bool]], set[str]]:
    found: dict[str, dict[str, bool]] = {}
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
            name = str(record.get("view_name") or record.get("view_url_name") or "")
            entry = found.setdefault(_slug(name), {"visual": False, "numeric": False})
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            image = record.get("image") if isinstance(record.get("image"), dict) else {}
            entry["numeric"] = (
                entry["numeric"] or data.get("status") == "ok" and _existing_relative(directory, data.get("path"))
            )
            entry["visual"] = (
                entry["visual"] or image.get("status") == "ok" and _existing_relative(directory, image.get("path"))
            )
            if entry["visual"] or entry["numeric"]:
                grades.add("layout/text only (oracle capture, default view state)")
    return found, grades


def _merge_oracle_maps(
    reference: dict[str, dict[str, bool]], oracle: dict[str, dict[str, bool]]
) -> dict[str, dict[str, bool]]:
    """Combine reference/ and _oracle coverage without losing either source."""
    combined: dict[str, dict[str, bool]] = {}
    for key in set(reference) | set(oracle):
        combined[key] = {
            "visual": reference.get(key, {}).get("visual", False) or oracle.get(key, {}).get("visual", False),
            "numeric": reference.get(key, {}).get("numeric", False) or oracle.get(key, {}).get("numeric", False),
        }
    return combined


def check_oracle_coverage(target: Path, reference_dir: Path | None, oracle_dir: Path | None) -> dict[str, Any]:
    """Per-page visual/numeric oracle coverage, with grade."""
    pages = expected_pages(target) or actual_pages(target)
    reference, reference_grades = _reference_oracles(target, reference_dir)
    oracle, oracle_grades = _oracle_capture_oracles(target, oracle_dir)
    if not pages:
        return {"id": "oracle-coverage", "status": STATUS_NOT_CHECKED, "detail": "no expected or actual pages found"}
    combined = _merge_oracle_maps(reference, oracle)
    rows = [{"page": page, **combined.get(_slug(page["name"]), {"visual": False, "numeric": False})} for page in pages]
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
        "grade": ", ".join(sorted(reference_grades | oracle_grades)) or "not checked (no oracle manifest found)",
        "rows": rows,
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


def _apply_stub_exemptions(check: dict[str, Any], exemptions: dict[str, Any]) -> dict[str, Any]:
    payload = check.get("payload") if isinstance(check.get("payload"), dict) else {}
    entries = exemptions["entries"]
    stubs = []
    for model in payload.get("models", []) if isinstance(payload.get("models"), list) else []:
        for finding in model.get("findings", []) if isinstance(model, dict) else []:
            canonical = f"{finding.get('kind')}:{finding.get('table')}[{finding.get('name')}]"
            aliases = {f"{finding.get('table')}[{finding.get('name')}]", str(finding.get("name") or "")}
            stubs.append(
                {
                    "canonical": canonical,
                    "finding": finding,
                    "exempted": _exempted(entries, "stub-measures", canonical, aliases),
                }
            )
    unexempted = [stub for stub in stubs if not stub["exempted"]]
    check["stub_exemptions"] = len(stubs) - len(unexempted)
    check["unexempted_stubs"] = len(unexempted)
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
    """Human-accepted deviations that should stay visible even when they do not fail the run."""
    declared = sum(_declared_downgrade_count(check) for check in report["checks"])
    return int(report.get("exemptions", {}).get("accepted", 0)) + declared


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
        return [
            f"  oracle coverage:  {check['visual_present']} of {pages} pages have a visual oracle"
            f"{_count_suffix(pages - check['visual_present'])}",
            f"                    {check['numeric_present']} of {pages} pages have a numeric oracle"
            f"{_count_suffix(pages - check['numeric_present'])}",
            f"                    grade: {check['grade']}  [{status}]",
        ]
    if check_id == "page-parity" and "expected_count" in check:
        lines = [
            f"  page-count parity: {check['actual_count']} PBIR page(s) for "
            f"{check['effective_expected_count']} expected Tableau dashboard(s)  [{status}]"
        ]
        if check.get("exemptions"):
            lines.append(f"                    exemptions accepted: {len(check['exemptions'])}")
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
    parser.add_argument("--oracle-dir", type=Path, help="override _oracle directory containing oracle-manifest.json")
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
