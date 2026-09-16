"""
purpose: print read-only, non-certifying observations for one explicitly selected local run.
usage:   python -B scripts/run_status.py --run <absolute-local-run-directory> [--json]

Exit 0 means the diagnostic inputs were assessable, NOT START_READY, COMPLETE, or process
liveness. Use Python's -B startup flag: even imports must not write bytecode. This command never
allocates, repairs, searches other runs, launches processes, or performs network operations.
"""

from __future__ import annotations

# pylint: disable=missing-class-docstring,missing-function-docstring,too-many-instance-attributes
# pylint: disable=too-many-return-statements,too-many-locals,too-many-branches

import argparse
import json
import math
import os
import stat
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

import package_filesystem as pfs
import stamp_tableau_provenance as prov
from bundle_corpus import PACKAGE_MARKER, classify_target, is_reparse_entry
from work_dirs import CANONICAL_SUBDIRS, RUN_LOCATION_INTACT, check_run_location

PACKAGE_SEARCH_DEPTH = 3  # Flat packages and the retained batch layouts; never an estate crawler.
CERTIFICATION_NOT_CHECKED = "NOT_CHECKED"
KNOWN_KINDS = {"workbook", "datasource"}
# Recorded vocabularies only, from package_unit and run_estate/stamp_tableau_provenance.
READINESS_STATUSES = {
    "NOT_EVALUATED",
    "NOT_CHECKED",
    "START_READY",
    "COMPLETE",
    "READY",
    "BLOCKED",
    "CANNOT_ESTABLISH",
    "NOT_APPLICABLE",
}
PHASE_STATUSES = {
    "success",
    "local_only",
    "partial",
    "failed",
    "empty",
    "publication_failed",
    "unknown",
    "ok",
    "over_ceiling",
    "unknown_paths",
    "no_paths",
    "cannot_establish",
}
FAILED_PHASE_STATUSES = PHASE_STATUSES - {"success", "local_only", "ok"}
PHASE_NAMES = {
    "engine_run",
    "engine_receipt",
    "provenance",
    "path_ceiling",
    "adjudicate",
    "slice_handovers",
    "slice_only_baseline_backfill",
}
OPERATIONS = set(prov.WORKER_OPERATIONS) | {
    "lookup-origin",
    "download-workbook",
    "build",
    "phase",
    "publish",
    prov.CONSISTENCY_OPERATION,
    "scrub-local-fields",
}
PHASE_CODES = set(prov.INVENTORY_ERROR_CODES) | {
    "collect-inputs-failed",
    "local-fingerprint-failed",
    "live-lookup-refused",
    "live-lookup-failed",
    prov.MSG_INVENTORY_FAILED,
    "content-unavailable",
    "scrub-failed",
    "sign-out-failed",
    "build-failed",
    "empty-input",
    prov.DEADLINE_CODE,
    prov.CANCELLED_CODE,
    "worker-crashed",
    "worker-protocol-invalid",
    "worker-reap-failed",
    "worker-start-failed",
    # stamp_tableau_provenance.consistency_faults emits these via normalize_result.
    "result-not-a-mapping",
    "inputs-not-a-list",
    "input-count-not-an-integer",
    "input-count-negative",
    "input-count-mismatch",
    "phase-status-unassessable",
    "success-without-inputs",
}
KNOWN_INTEGRITY_DAMAGE = {
    pfs.CODE_DIGEST_MISMATCH,
    pfs.CODE_FILE_MISSING,
    pfs.CODE_FILE_UNDECLARED,
}


@dataclass
class Finding:
    code: str
    source: str
    reason: str
    unit: str | None = None
    unassessable: bool = False


@dataclass
class Occurrence:
    source: str
    unit: str
    kind: str
    status: str = "observed"


@dataclass
class PackageObservation:
    relative_path: str
    unit: str | None = None
    kind: str | None = None
    scope: str = "UNSCOPED_PACKAGE"
    integrity_status: str = "unassessable"
    integrity_codes: list[str] = field(default_factory=list)
    construction_status: str = "not_recorded"
    self_contained: bool | None = None
    has_engine_working_copy: bool | None = None
    binding_state: str = "not_recorded"
    model_binding: dict[str, Any] = field(default_factory=dict)
    reference: dict[str, Any] = field(default_factory=lambda: {"status": "not_recorded"})
    stored_readiness: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class UnitStatus:
    unit: str
    kind: str
    scope: str = "scoped"
    occurrences: list[Occurrence] = field(default_factory=list)
    generated_working_copy: str = "missing"
    handover: str = "missing"
    handover_sources: list[str] = field(default_factory=list)
    package: PackageObservation | None = None
    current_certification: str = CERTIFICATION_NOT_CHECKED


def _problem(findings: list[Finding], source: str, reason: str, unit: str | None = None) -> None:
    findings.append(Finding("UNASSESSABLE_EVIDENCE", source, reason, unit, unassessable=True))


def _safe_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and "/" not in value
        and pfs.is_canonical_key(value)
        and not any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)
    )


def _entry_state(path: Path) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except NotADirectoryError:
        return "not_directory"
    except (OSError, ValueError):
        return "unreadable"
    if is_reparse_entry(info):
        return "unsafe_reparse"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "regular"
    return "special"


def _dir_state(path: Path) -> str:
    state = _entry_state(path)
    if state == "directory":
        return "present"
    if state in {"regular", "special"}:
        state = "not_directory"
    return "missing" if state == "missing" else f"unassessable:{state}"


def _strict_json_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Only called beneath an accepted directory; never follows a rejected parent or marker."""
    state = _entry_state(path)
    if state != "regular":
        return None, "not_regular" if state == "directory" else state
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None, "not_utf8"
    except (OSError, ValueError):
        return None, "unreadable"
    try:
        return pfs.parse_manifest_text(text), None
    except pfs._ManifestError as exc:  # pylint: disable=protected-access
        return None, "not_object" if exc.code == pfs.CODE_MANIFEST_NOT_OBJECT else "invalid_json"


def _rel(path: Path, run: Path) -> str:
    return path.relative_to(run).as_posix()


def _directory(path: Path, run: Path, findings: list[Finding]) -> str:
    state = _dir_state(path)
    if state.startswith("unassessable:"):
        _problem(findings, _rel(path, run), state.removeprefix("unassessable:"))
    return state


def _entries(path: Path, run: Path, findings: list[Finding]) -> list[Path] | None:
    try:
        with os.scandir(path) as entries:
            return [path / entry.name for entry in sorted(entries, key=lambda entry: entry.name)]
    except (OSError, ValueError):
        _problem(findings, _rel(path, run), "directory_unreadable")
        return None


def _local_path_problem(raw: str) -> str | None:
    # Inspect the spelling BEFORE Path can normalize it and before any filesystem call.
    spelling = raw.replace("\\", "/")
    if spelling.startswith("//") or spelling.lower().startswith(
        ("/??/", "/device/", "/global??/", "/globalroot/", "/dosdevices/")
    ):
        return "network_or_device_path"
    path = Path(raw)
    if not path.is_absolute():
        return "absolute_local_path_required"
    if os.name == "nt" and (len(path.drive) != 2 or not path.drive[0].isalpha() or path.drive[1] != ":"):
        return "network_or_device_path"
    if any(not _safe_name(part) for part in path.parts[1:]):
        return "unsafe_path_component"
    # A Windows device/drive spelling is not a local POSIX path either.
    if os.name != "nt" and PureWindowsPath(raw).drive:
        return "network_or_device_path"
    return None


def _known(value: Any, allowed: set[str], findings: list[Finding], source: str, unit: str | None = None) -> str | None:
    if isinstance(value, str) and value in allowed:
        return value
    _problem(findings, source, "unknown_recorded_value", unit)
    return None


def _display_known(value: Any, allowed: set[str], findings: list[Finding], source: str, unit: str | None = None) -> str:
    """The display fallback is an observation state, never an accepted identity."""
    return _known(value, allowed, findings, source, unit) or "unknown"


def _flag(value: Any, findings: list[Finding], source: str, unit: str | None) -> bool | None:
    if isinstance(value, bool):
        return value
    _problem(findings, source, "invalid_boolean", unit)
    return None


def _timestamp(value: Any, findings: list[Finding], source: str, unit: str | None = None) -> str | None:
    if isinstance(value, str) and not any(char.isspace() for char in value):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            pass
    _problem(findings, source, "invalid_timestamp", unit)
    return None


def _report_occurrences(bundle: Path, findings: list[Finding]) -> tuple[str, list[Occurrence]]:
    report, reason = _strict_json_file(bundle / "report.json")
    if reason is not None:
        _problem(findings, "bundle/report.json", reason)
        return "unestablished", []
    occurrences: list[Occurrence] = []
    scope = "established"
    for key, kind in (("workbooks", "workbook"), ("datasources", "datasource")):
        rows = report.get(key)
        if not isinstance(rows, list):
            _problem(findings, f"bundle/report.json#{key}", "scope_collection_missing_or_invalid")
            scope = "unestablished"
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not _safe_name(row.get("name")):
                _problem(findings, f"bundle/report.json#{key}[{index}]", "invalid_unit_identity")
                scope = "unestablished"
                continue
            occurrences.append(Occurrence("engine_report", row["name"], kind))
    return scope, occurrences


def _pbip_occurrences(run: Path, findings: list[Finding]) -> tuple[str, list[Occurrence]]:
    pbip = run / "bundle" / "pbip"
    state = _directory(pbip, run, findings)
    occurrences: list[Occurrence] = []
    if state == "present":
        children = _entries(pbip, run, findings)
        if children is None:
            return "unassessable:directory_unreadable", []
        for index, child in enumerate(children):
            if not _safe_name(child.name):
                _problem(findings, f"bundle/pbip#entry[{index}]", "invalid_unit_identity")
                continue
            child_state = _directory(child, run, findings)
            occurrences.append(Occurrence("working_copy", child.name, "unknown", child_state))
    return state, occurrences


def _handovers(run: Path, findings: list[Finding]) -> tuple[str, dict[str, list[str]]]:
    root = run / "bundle" / "handover"
    state = _directory(root, run, findings)
    identities: dict[str, list[str]] = {}
    if state != "present":
        return state, identities
    children = _entries(root, run, findings)
    if children is None:
        return "unassessable:directory_unreadable", identities
    for path in children:
        entry = _entry_state(path)
        if entry not in {"regular", "directory"}:
            _problem(findings, _rel(path, run), entry)
            state = "unassessable:handover_inventory"
            continue
        if entry != "regular" or path.suffix.lower() != ".json":
            continue
        document, reason = _strict_json_file(path)
        if reason is not None:
            _problem(findings, _rel(path, run), reason)
            state = "unassessable:handover_inventory"
            continue
        workbook = document.get("workbook")
        if not isinstance(workbook, dict) or not _safe_name(workbook.get("name")):
            _problem(findings, _rel(path, run), "invalid_handover_identity")
            state = "unassessable:handover_inventory"
            continue
        identities.setdefault(workbook["name"], []).append(_rel(path, run))
    for name, paths in identities.items():
        if len(paths) > 1:
            _problem(findings, "bundle/handover", "ambiguous_handover_identity", name)
    return state, identities


def _discover_package_dirs(run: Path, findings: list[Finding]) -> list[Path]:
    found: list[Path] = []

    def walk(root: Path, depth: int) -> None:
        if _entry_state(root / PACKAGE_MARKER) != "missing":
            found.append(root)
            return  # Even a malformed marker is a boundary, never a grouping directory.
        if depth >= PACKAGE_SEARCH_DEPTH:
            return
        for path in _entries(root, run, findings) or []:
            state = _entry_state(path)
            if state == "directory":
                walk(path, depth + 1)
            elif state != "regular":
                _problem(findings, _rel(path, run), state)

    walk(run / "packages", 0)
    return found


def _bindings(manifest: dict[str, Any], observation: PackageObservation, findings: list[Finding]) -> None:
    source, unit = observation.relative_path, observation.unit
    model = manifest.get("model_binding")
    if "model_binding" in manifest:
        if not isinstance(model, dict) or not ({"kind", "state"} & model.keys()):
            _problem(findings, source, "invalid_model_binding", unit)
        else:
            for key, allowed in (
                ("kind", {"no_report", "unreadable", "absent", "byConnection", "byPath"}),
                ("state", {"bound", "unbound"}),
            ):
                if key in model:
                    observation.model_binding[key] = _display_known(model[key], allowed, findings, source, unit)
            if "resolves_in_package" in model:
                observation.model_binding["resolves_in_package"] = _flag(
                    model["resolves_in_package"], findings, source, unit
                )
    # Data-folder binding is the producer's state; model_binding.kind describes a different join.
    if "data_sources" in manifest:
        data = manifest["data_sources"]
        if not isinstance(data, dict) or "binding" not in data:
            _problem(findings, source, "invalid_data_binding", unit)
            observation.binding_state = "unknown"
        elif data["binding"] is None:
            observation.binding_state = "not_required"
        elif isinstance(data["binding"], dict):
            observation.binding_state = _display_known(
                data["binding"].get("state"), {"bound", "unbound"}, findings, source, unit
            )
        else:
            _problem(findings, source, "invalid_data_binding", unit)
            observation.binding_state = "unknown"
    elif "state" in observation.model_binding:
        observation.binding_state = observation.model_binding["state"]  # Recorded legacy shape only.


def _oracle_object_counts(obj: Any) -> tuple[int, int] | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("images"), list) or "data" not in obj:
        return None
    if not all(isinstance(image, str) and pfs.is_canonical_key(image) for image in obj["images"]):
        return None
    if obj["data"] is not None and not (isinstance(obj["data"], str) and pfs.is_canonical_key(obj["data"])):
        return None
    return len(obj["images"]), int(obj["data"] is not None)


def _reference(manifest: dict[str, Any], observation: PackageObservation, findings: list[Finding]) -> dict[str, Any]:
    if "oracle" not in manifest:
        return {"status": "not_recorded"}
    oracle = manifest["oracle"]
    source, unit = observation.relative_path, observation.unit
    if not isinstance(oracle, dict) or not all(isinstance(oracle.get(key), list) for key in ("objects", "omissions")):
        _problem(findings, source, "invalid_oracle_collections", unit)
        return {"status": "unassessable"}
    images = data = 0
    for obj in oracle["objects"]:
        counts = _oracle_object_counts(obj)
        if counts is None:
            _problem(findings, source, "invalid_oracle_object", unit)
            return {"status": "unassessable"}
        images += counts[0]
        data += counts[1]
    if not all(isinstance(row, dict) for row in oracle["omissions"]) or not all(
        oracle.get(key) is None or isinstance(oracle[key], str) for key in ("route", "reason")
    ):
        _problem(findings, source, "invalid_oracle_summary", unit)
        return {"status": "unassessable"}
    omissions = len(oracle["omissions"])
    has_reason = bool(oracle.get("reason"))
    status = "recorded_present" if images or data else "recorded_missing"
    if (images or data) and (omissions or has_reason):
        status = "recorded_partial"
    artifacts = manifest.get("artifacts")
    no_report = isinstance(artifacts, dict) and "report" in artifacts and artifacts["report"] is None
    if (
        observation.kind == "datasource"
        and observation.model_binding.get("kind") == "no_report"
        and no_report
        and not oracle["objects"]
        and not omissions
    ):
        status = "not_applicable"
    return {
        "status": status,
        "objects": len(oracle["objects"]),
        "images": images,
        "data": data,
        "omissions": omissions,
        "reason": "recorded_omission" if has_reason else "not_recorded",
        "semantics": "recorded presence only; content, coverage and fidelity NOT_CHECKED",
    }


def _stored_readiness(manifest: dict[str, Any], observation: PackageObservation, findings: list[Finding]) -> None:
    for key in ("dispatch_readiness", "start_ready", "complete"):
        if key not in manifest:
            continue
        raw = manifest[key]
        source, unit = observation.relative_path, observation.unit
        value = raw if isinstance(raw, dict) else {"status": raw}
        last = {"status": _display_known(value.get("status"), READINESS_STATUSES, findings, source, unit)}
        if "availability" in value:
            last["availability"] = _display_known(
                value["availability"], {"UNAVAILABLE", "AVAILABLE"}, findings, source, unit
            )
        for time_key in ("recorded_at", "checked_at"):
            if time_key in value:
                last[time_key] = _timestamp(value[time_key], findings, source, unit)
        # Prose, commands, tokens and arbitrary nested dictionaries are deliberately never copied.
        observation.stored_readiness.append(
            {
                "source": key,
                "last_observed": last,
                "current_certification": CERTIFICATION_NOT_CHECKED,
            }
        )


def _package_observations(run: Path, findings: list[Finding]) -> list[PackageObservation]:
    observations: list[PackageObservation] = []
    for path in _discover_package_dirs(run, findings):
        observation = PackageObservation(_rel(path, run))
        observations.append(observation)
        classification = classify_target(path)
        if not classification.declares_self_contained:
            observation.integrity_codes = [classification.code]
            _problem(findings, observation.relative_path, classification.code)
            continue
        manifest, reason = _strict_json_file(path / PACKAGE_MARKER)
        if reason is not None:
            _problem(findings, observation.relative_path, reason)
            observation.integrity_codes = [f"manifest_{reason}"]
            continue
        integrity = pfs.verify_package(path, classification)
        observation.integrity_status = integrity.status
        observation.integrity_codes = list(integrity.codes())
        if _safe_name(manifest.get("unit")):
            observation.unit = manifest["unit"]
        else:
            _problem(findings, observation.relative_path, "invalid_package_identity")
        observation.kind = _known(
            manifest.get("kind"), KNOWN_KINDS, findings, observation.relative_path, observation.unit
        )
        if integrity.status != "clean":
            findings.append(
                Finding(
                    "PACKAGE_INTEGRITY",
                    observation.relative_path,
                    "inspect_retained_bytes",
                    observation.unit,
                    unassessable=integrity.status == "unassessable"
                    or bool(set(integrity.codes()) - KNOWN_INTEGRITY_DAMAGE),
                )
            )
        if "construction_status" in manifest:
            observation.construction_status = _display_known(
                manifest["construction_status"],
                {"ASSEMBLED", "BLOCKED"},
                findings,
                observation.relative_path,
                observation.unit,
            )
        for key in ("self_contained", "has_engine_working_copy"):
            if key in manifest:
                setattr(observation, key, _flag(manifest[key], findings, observation.relative_path, observation.unit))
        _bindings(manifest, observation, findings)
        observation.reference = _reference(manifest, observation, findings)
        _stored_readiness(manifest, observation, findings)
    return observations


def _assemble_units(  # pylint: disable=too-many-arguments
    occurrences: list[Occurrence],
    packages: list[PackageObservation],
    findings: list[Finding],
    *,
    pbip_state: str,
    handover_state: str,
    handovers: dict[str, list[str]],
) -> tuple[list[UnitStatus], list[PackageObservation]]:
    # This join uses already-read observations only. In particular, it never rebuilds a path from
    # a raw unit name, and cannot re-enter a directory an earlier reader rejected.
    kinds: dict[str, set[str]] = {}
    counts: dict[tuple[str, str], int] = {}
    for row in occurrences:
        if row.source == "engine_report":
            kinds.setdefault(row.unit, set()).add(row.kind)
            key = row.kind, row.unit
            counts[key] = counts.get(key, 0) + 1
    ambiguous = {name for name, values in kinds.items() if len(values) > 1}
    ambiguous.update(name for (_kind, name), count in counts.items() if count > 1)
    units: dict[tuple[str, str], UnitStatus] = {}
    for row in occurrences:
        if row.source == "working_copy" and len(kinds.get(row.unit, set())) == 1:
            row.kind = next(iter(kinds[row.unit]))
        key = row.kind, row.unit
        unit = units.setdefault(key, UnitStatus(row.unit, row.kind))
        unit.occurrences.append(row)
        if row.unit in ambiguous:
            unit.scope = "ambiguous"
        elif row.unit not in kinds:
            unit.scope = "working_copy_only"
    for name in sorted(ambiguous):
        findings.append(Finding("AMBIGUOUS_UNIT_IDENTITY", "inventory", "duplicate_or_cross_kind_identity", name))
    for unit in units.values():
        working = [row.status for row in unit.occurrences if row.source == "working_copy"]
        unit.generated_working_copy = working[0] if working else ("missing" if pbip_state == "present" else pbip_state)
        unit.handover_sources = handovers.get(unit.unit, []) if unit.kind == "workbook" else []
        if unit.kind == "datasource":
            unit.handover = "not_applicable"
        elif len(unit.handover_sources) > 1 or (unit.handover_sources and unit.unit in ambiguous):
            unit.handover = "ambiguous"
        elif handover_state.startswith("unassessable:"):
            unit.handover = handover_state
        elif unit.handover_sources:
            unit.handover = "present"
        else:
            unit.handover = "missing" if handover_state == "present" else handover_state
    for name, paths in handovers.items():
        if ("workbook", name) not in units:
            findings.extend(Finding("UNSCOPED_HANDOVER", path, "preserve_and_inspect_identity", name) for path in paths)
    package_counts: dict[tuple[str | None, str | None], int] = {}
    for package in packages:
        key = package.kind, package.unit
        package_counts[key] = package_counts.get(key, 0) + 1
    for package in packages:
        key = package.kind, package.unit
        if package.kind in KNOWN_KINDS and key in units and package.unit not in ambiguous and package_counts[key] == 1:
            units[key].package = package
            package.scope = "associated"
        else:
            findings.append(
                Finding("UNSCOPED_PACKAGE", package.relative_path, "preserve_and_inspect_association", package.unit)
            )
    return sorted(units.values(), key=lambda unit: (unit.kind, unit.unit)), [
        package for package in packages if package.scope == "UNSCOPED_PACKAGE"
    ]


def _phase_record(row: Any, source: str, findings: list[Finding]) -> dict[str, Any]:
    if not isinstance(row, dict):
        _problem(findings, source, "invalid_phase")
        return {"source": source, "last_observed": {"status": "unknown"}, "failed": False}
    last: dict[str, Any] = {}
    if "status" in row:
        last["status"] = _display_known(row["status"], PHASE_STATUSES, findings, source)
    failed = last.get("status") in FAILED_PHASE_STATUSES
    if "exit_code" in row:
        code = row["exit_code"]
        if isinstance(code, int) and not isinstance(code, bool) and -(2**31) <= code < 2**32:
            last["exit_code"] = code
            failed |= code != 0
        else:
            _problem(findings, source, "invalid_exit_code")
    for key in ("elapsed_sec", "started_wall"):
        if key not in row:
            continue
        value = row[key]
        if type(value) not in {int, float} or not 0 <= value < 1e12 or not math.isfinite(value):
            _problem(findings, source, "invalid_phase_time")
            continue
        if key == "started_wall":
            try:
                last["started_at"] = datetime.fromtimestamp(value, timezone.utc).isoformat()
            except (ValueError, OSError, OverflowError):
                _problem(findings, source, "invalid_phase_time")
        else:
            last[key] = value
    phase = _display_known(row.get("phase"), PHASE_NAMES, findings, source)
    return {"source": source, "phase": phase, "last_observed": last, "failed": failed}


def _recorded_phases(run: Path, findings: list[Finding]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    evidence: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for name in ("source-provenance.json", "phase-timings.json"):
        source = f"bundle/{name}"
        document, reason = _strict_json_file(run / "bundle" / name)
        evidence[source] = reason or "recorded"
        if reason is not None:
            if reason != "missing":
                _problem(findings, source, reason)
            continue
        if name == "phase-timings.json":
            phases = document.get("phases")
            if not isinstance(phases, list):
                _problem(findings, source, "invalid_phase_collection")
                continue
            records.extend(
                _phase_record(row, f"{source}#phases[{index}]", findings) for index, row in enumerate(phases)
            )
            continue
        phase = document.get("phase")
        if not isinstance(phase, dict) or "status" not in phase or not isinstance(phase.get("errors"), list):
            _problem(findings, source, "invalid_provenance_phase")
            continue
        record = _phase_record({**phase, "phase": "provenance"}, source, findings)
        errors = []
        for index, error in enumerate(phase["errors"]):
            error_source = f"{source}#errors[{index}]"
            if not isinstance(error, dict):
                _problem(findings, error_source, "invalid_phase_error")
                errors.append({"code": "unknown", "operation": "unknown"})
                continue
            errors.append(
                {
                    "code": _display_known(error.get("code"), PHASE_CODES, findings, error_source),
                    "operation": _display_known(error.get("operation"), OPERATIONS, findings, error_source),
                }
            )
        record["last_observed"]["errors"] = errors
        record["failed"] |= bool(errors)
        records.append(record)
    return evidence, records


def _next_action(  # pylint: disable=too-many-arguments
    run_ok: bool,
    report_scope: str,
    units: list[UnitStatus],
    packages: list[PackageObservation],
    failures: list[dict[str, Any]],
    *,
    findings: list[Finding],
) -> dict[str, Any]:
    affected = sorted({unit.unit for unit in units} | {package.unit or package.relative_path for package in packages})
    details: list[Any] = []
    if not run_ok:
        headline = "Confirm the explicitly selected local run; child evidence was not read."
    elif any(finding.unassessable for finding in findings):
        headline = "Inspect unassessable evidence in place; preserve retained work before deciding what to do."
        details = [asdict(finding) for finding in findings if finding.unassessable]
    elif failures:
        headline = "Inspect the recorded failed phase; preserve retained work."
        details = failures
    elif any(package.scope == "UNSCOPED_PACKAGE" for package in packages):
        headline = (
            "Preserve package-only or ambiguous package work and inspect its exact identity before associating it."
        )
        details = [asdict(package) for package in packages if package.scope == "UNSCOPED_PACKAGE"]
    elif any(package.integrity_status != "clean" for package in packages):
        headline = "Inspect retained package changes against the recorded manifest; keep the existing bytes and hashes."
        details = [
            {"source": package.relative_path, "codes": package.integrity_codes}
            for package in packages
            if package.integrity_codes
        ]
    elif any(package.binding_state == "unbound" for package in packages):
        headline = "Bind the retained package with `python scripts/set_data_folder.py --package <absolute-package>`."
        affected = sorted(package.unit for package in packages if package.unit and package.binding_state == "unbound")
    elif any(
        package.reference["status"] in {"recorded_partial", "recorded_missing", "not_recorded"} for package in packages
    ):
        headline = (
            "Inspect or consolidate retained references, then use "
            "`python scripts/check_reference_readiness.py <provider-package> <consumer-package>` with explicit paths."
        )
        affected = sorted(
            package.unit
            for package in packages
            if package.unit and package.reference["status"] in {"recorded_partial", "recorded_missing", "not_recorded"}
        )
    elif report_scope != "established":
        headline = "Inspect the engine report before treating inventory scope as established."
    else:
        headline = "Use the actual public readiness check if certification is needed; this command did not check it."
    return {"headline": headline, "affected_units": affected, "details": details}


def build_status(run: Path | str) -> tuple[dict[str, Any], int]:
    raw = str(run)
    findings: list[Finding] = []
    status: dict[str, Any] = {
        "schema_version": 2,
        "selected_run": raw,
        "diagnostic_contract": "inventory only; not START_READY, COMPLETE, migration readiness or process liveness",
        "current_certification": CERTIFICATION_NOT_CHECKED,
    }
    reason = _local_path_problem(raw)
    path = Path(raw)
    if reason is None:
        # lstat(target) alone still traverses ancestors. Prove them top-down, stopping at the first
        # rejected component; no descendant syscall may pass that boundary.
        for ancestor in [*reversed(path.parents), path]:
            state = _dir_state(ancestor)
            if state != "present":
                reason = f"run_boundary_{state}"
                break
    if reason is not None:
        _problem(findings, "selected_run", reason)
        status["findings"] = [asdict(finding) for finding in findings]
        status["next_action"] = _next_action(False, "unestablished", [], [], [], findings=findings)
        return status, 2 if reason in {
            "absolute_local_path_required",
            "network_or_device_path",
            "unsafe_path_component",
        } else 1
    manifest, reason = _strict_json_file(path / "run.json")
    if reason is not None:
        _problem(findings, "run.json", reason)
    else:
        location = check_run_location(manifest, path)
        # The shared location reader's detail contains manifest text. Only its fixed verdicts leave here.
        status["run_location"] = {
            "state": location.state,
            "derived_name_check": location.derived_name_check,
            "path_check": location.path_check,
        }
        if location.state != RUN_LOCATION_INTACT:
            _problem(findings, "run.json", "run_location_unestablished")
    if findings:
        status["findings"] = [asdict(finding) for finding in findings]
        status["next_action"] = _next_action(False, "unestablished", [], [], [], findings=findings)
        return status, 1

    status["run_recorded_status"] = (
        _display_known(manifest["status"], {"active"}, findings, "run.json") if "status" in manifest else "not_recorded"
    )
    status["run_recorded_status_semantics"] = "allocation metadata only; not proof of process liveness"
    states = {name: _directory(path / name, path, findings) for name in CANONICAL_SUBDIRS}
    report_scope, reported, working, handovers, records = "unestablished", [], [], {}, []
    pbip_state = handover_state = "not_read"
    phase_evidence = {"bundle/source-provenance.json": "not_read", "bundle/phase-timings.json": "not_read"}
    if states["bundle"] == "present":
        report_scope, reported = _report_occurrences(path / "bundle", findings)
        pbip_state, working = _pbip_occurrences(path, findings)
        handover_state, handovers = _handovers(path, findings)
        phase_evidence, records = _recorded_phases(path, findings)
    else:
        _problem(findings, "bundle/report.json", "parent_boundary_not_established")
    packages = _package_observations(path, findings) if states["packages"] == "present" else []
    units, unscoped = _assemble_units(
        [*reported, *working],
        packages,
        findings,
        pbip_state=pbip_state,
        handover_state=handover_state,
        handovers=handovers,
    )
    failures = [record for record in records if record["failed"]]
    status.update(
        {
            "canonical_subdirs": states,
            "inventory_scope": report_scope,
            "child_evidence": {"bundle/pbip": pbip_state, "bundle/handover": handover_state, **phase_evidence},
            "units": [asdict(unit) for unit in units],
            "unscoped_packages": [asdict(package) for package in unscoped],
            "recorded_phases": records,
            "recorded_failures": failures,
            "findings": [asdict(finding) for finding in findings],
            "next_action": _next_action(True, report_scope, units, packages, failures, findings=findings),
        }
    )
    return status, int(report_scope != "established" or any(finding.unassessable for finding in findings))


def render_human(status: dict[str, Any]) -> str:
    """One rendering of the SAME normalized records, including duplicates and action details."""
    lines = ["RUN STATUS: DIAGNOSTIC ONLY (NOT readiness)"]
    for key, value in status.items():
        label = "NEXT ACTION" if key == "next_action" else key
        if isinstance(value, list):
            lines.append(f"{label}: {len(value)}")
            lines.extend(f"  {json.dumps(row, sort_keys=True, ensure_ascii=True)}" for row in value)
        else:
            lines.append(f"{label}: {json.dumps(value, sort_keys=True, ensure_ascii=True)}")
    return "\n".join(lines)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default diagnostic interpolates arbitrary argv, including control characters.
        del message
        super().error("invalid arguments; use --help")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = _ArgumentParser(
        prog="run_status.py", description="Print read-only run-status diagnostics for one absolute local run."
    )
    parser.add_argument(
        "--run", required=True, help="absolute local run directory; UNC, devices and reparse aliases refused"
    )
    parser.add_argument("--json", action="store_true", help="print the same normalized observations as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    status, exit_code = build_status(args.run)
    print(json.dumps(status, indent=2, sort_keys=True, ensure_ascii=True) if args.json else render_human(status))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
