"""
purpose: print a read-only, non-certifying status slice for one explicitly selected run.
cli:     python scripts/run_status.py --run <absolute-run-directory> [--json]

This command reports the selected run's recorded allocation status, post-conversion inventory,
retained package/generated work, recorded phase failures, and the next non-destructive action. It
never allocates, repairs, discovers another root, launches a process, calls the network, opens Power
BI, or writes a status artifact. A successful exit means this diagnostic collection was assessable;
it is NOT migration readiness, START_READY, or COMPLETE.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bundle_corpus import classify_target
from package_filesystem import verify_package
from package_unit import unit_name_problem
from work_dirs import CANONICAL_SUBDIRS, RUN_LOCATION_INTACT, check_run_location

PACKAGE_MANIFEST = "package-manifest.json"
# Flat packages are <run>/packages/<Unit>/package-manifest.json; documented batch packages add one
# grouping directory. Depth 3 matches check_migration_progress.py's bounded compatibility search
# without turning this read-only diagnostic into an unbounded package crawler.
PACKAGE_SEARCH_DEPTH = 3
KNOWN_KINDS = frozenset({"workbook", "datasource", "unknown"})
CERTIFICATION_NOT_CHECKED = "NOT_CHECKED"


@dataclass
class Finding:
    code: str
    message: str
    unit: str | None = None
    source: str | None = None

    def as_dict(self) -> dict[str, str]:
        row = {"code": self.code, "message": self.message}
        if self.unit is not None:
            row["unit"] = self.unit
        if self.source is not None:
            row["source"] = self.source
        return row


@dataclass
class Occurrence:
    source: str
    unit: str
    kind: str
    status: str = "observed"
    detail: str | None = None

    def as_dict(self) -> dict[str, str]:
        row = {"source": self.source, "unit": self.unit, "kind": self.kind, "status": self.status}
        if self.detail:
            row["detail"] = self.detail
        return row


@dataclass
class PackageObservation:
    relative_path: str
    unit: str | None
    kind: str | None
    scope: str
    integrity_status: str
    integrity_codes: list[str]
    construction_status: str | None = None
    self_contained: bool | None = None
    has_engine_working_copy: bool | None = None
    binding_state: str | None = None
    reference_status: str | None = None
    stored_readiness: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "relative_path": self.relative_path,
            "unit": self.unit,
            "kind": self.kind,
            "scope": self.scope,
            "integrity_status": self.integrity_status,
            "integrity_codes": self.integrity_codes,
        }
        for key in (
            "construction_status",
            "self_contained",
            "has_engine_working_copy",
            "binding_state",
            "reference_status",
            "stored_readiness",
        ):
            value = getattr(self, key)
            if value is not None:
                row[key] = value
        return row


@dataclass
class UnitStatus:
    unit: str
    kind: str
    scope: str = "scoped"
    occurrences: list[Occurrence] = field(default_factory=list)
    generated_working_copy: str = "missing"
    handover: str = "missing"
    package: PackageObservation | None = None
    current_certification: str = CERTIFICATION_NOT_CHECKED
    findings: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "unit": self.unit,
            "kind": self.kind,
            "scope": self.scope,
            "occurrences": [occurrence.as_dict() for occurrence in self.occurrences],
            "observations": {
                "generated_working_copy": self.generated_working_copy,
                "handover": self.handover,
                "current_certification": self.current_certification,
            },
        }
        if self.package is not None:
            row["package"] = self.package.as_dict()
        if self.findings:
            row["findings"] = [finding.as_dict() for finding in self.findings]
        return row


def _strict_json_file(path: Path) -> tuple[Any | None, str | None]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "unreadable"
    if stat.S_ISLNK(info.st_mode):
        return None, "unsafe_link"
    if not stat.S_ISREG(info.st_mode):
        return None, "not_regular"
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None, "not_utf8"
    except OSError:
        return None, "unreadable"
    try:
        return (
            json.loads(
                text,
                object_pairs_hook=_no_duplicate_keys,
                parse_constant=_no_constants,
                parse_float=_finite_float,
            ),
            None,
        )
    except (ValueError, RecursionError):
        return None, "invalid_json"


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_constants(value: str) -> None:
    raise ValueError(value)


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(value)
    return parsed


def _rel(path: Path, run: Path) -> str:
    try:
        return path.relative_to(run).as_posix()
    except ValueError:
        return "UNCONTAINED"


def _safe_existing_dir(path: Path) -> tuple[bool, str | None]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False, "missing"
    except (OSError, ValueError):
        return False, "unreadable"
    if stat.S_ISLNK(info.st_mode):
        return False, "unsafe_link"
    if not stat.S_ISDIR(info.st_mode):
        return False, "not_directory"
    return True, None


def _optional_regular(path: Path) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return "unassessable"
    if stat.S_ISLNK(info.st_mode):
        return "unsafe_link"
    if stat.S_ISREG(info.st_mode):
        return "present"
    return "not_regular"


def _canonical_dirs(run: Path) -> tuple[dict[str, str], list[Finding]]:
    states: dict[str, str] = {}
    findings: list[Finding] = []
    for name in CANONICAL_SUBDIRS:
        path = run / name
        ok, reason = _safe_existing_dir(path)
        if ok:
            states[name] = "present"
        elif reason == "missing":
            states[name] = "missing"
        else:
            states[name] = f"unassessable:{reason}"
            findings.append(Finding("UNSAFE_CANONICAL_SUBDIR", f"canonical subdir {name}/ is {reason}", source=name))
    return states, findings


def _report_occurrences(bundle: Path, findings: list[Finding]) -> tuple[str, list[Occurrence]]:
    report, reason = _strict_json_file(bundle / "report.json")
    if reason is not None:
        findings.append(
            Finding("ENGINE_REPORT_UNESTABLISHED", f"bundle/report.json is {reason}", source="bundle/report.json")
        )
        return "unestablished", []
    if not isinstance(report, dict):
        findings.append(
            Finding(
                "ENGINE_REPORT_UNESTABLISHED", "bundle/report.json is not a JSON object", source="bundle/report.json"
            )
        )
        return "unestablished", []
    occurrences: list[Occurrence] = []
    for key, kind in (("workbooks", "workbook"), ("datasources", "datasource")):
        value = report.get(key, [])
        if not isinstance(value, list):
            findings.append(
                Finding("ENGINE_REPORT_UNESTABLISHED", f"report {key} is not a list", source="bundle/report.json")
            )
            return "unestablished", []
        for index, row in enumerate(value):
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip():
                findings.append(
                    Finding(
                        "ENGINE_REPORT_UNESTABLISHED",
                        f"report {key}[{index}] has no usable name",
                        source="bundle/report.json",
                    )
                )
                return "unestablished", []
            name = row["name"]
            problem = unit_name_problem(name)
            if problem is not None:
                findings.append(
                    Finding(
                        "MALFORMED_UNIT_NAME",
                        f"report {key}[{index}] unit name is unsafe: {problem}",
                        source="bundle/report.json",
                    )
                )
                return "unestablished", []
            occurrences.append(Occurrence("engine_report", name, kind))
    return "established", occurrences


def _pbip_occurrences(run: Path, states: dict[str, str], findings: list[Finding]) -> list[Occurrence]:
    if states.get("bundle") != "present" or _safe_existing_dir(run / "bundle" / "pbip")[0] is False:
        return []
    pbip = run / "bundle" / "pbip"
    occurrences: list[Occurrence] = []
    for child in _safe_scandir(pbip, findings, "bundle/pbip"):
        path = pbip / child.name
        ok, reason = _safe_existing_dir(path)
        if not ok:
            findings.append(Finding("UNSAFE_WORKING_COPY", f"working-copy entry is {reason}", source="bundle/pbip"))
            continue
        problem = unit_name_problem(child.name)
        if problem is not None:
            findings.append(
                Finding("MALFORMED_WORKING_COPY", f"working-copy unit name is unsafe: {problem}", source="bundle/pbip")
            )
            continue
        occurrences.append(Occurrence("working_copy", child.name, "unknown"))
    return occurrences


def _safe_scandir(root: Path, findings: list[Finding], source: str) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(root) as entries:
            return sorted(list(entries), key=lambda entry: entry.name)
    except OSError:
        findings.append(Finding("UNREADABLE_DIRECTORY", "directory could not be listed", source=source))
        return []


def _discover_package_dirs(run: Path, states: dict[str, str], findings: list[Finding]) -> list[Path]:
    if states.get("packages") != "present":
        return []
    packages = run / "packages"
    found: list[Path] = []

    def walk(root: Path, depth: int) -> None:
        if _optional_regular(root / PACKAGE_MANIFEST) != "missing":
            found.append(root)
        if depth >= PACKAGE_SEARCH_DEPTH:
            return
        for child in _safe_scandir(root, findings, _rel(root, run)):
            path = root / child.name
            ok, reason = _safe_existing_dir(path)
            if ok:
                walk(path, depth + 1)
            elif reason != "missing":
                findings.append(
                    Finding("UNSAFE_PACKAGE_ENTRY", f"package search entry is {reason}", source=_rel(path.parent, run))
                )

    walk(packages, 0)
    return sorted(dict.fromkeys(found), key=lambda path: _rel(path, run))


def _package_manifest(path: Path) -> tuple[dict[str, object] | None, str | None]:
    manifest, reason = _strict_json_file(path / PACKAGE_MANIFEST)
    if reason is not None:
        return None, reason
    if not isinstance(manifest, dict):
        return None, "invalid_manifest"
    return manifest, None


def _binding_state(manifest: dict[str, object]) -> str | None:
    model_binding = manifest.get("model_binding")
    if isinstance(model_binding, dict) and isinstance(model_binding.get("state"), str):
        return model_binding["state"]
    data_sources = manifest.get("data_sources")
    if isinstance(data_sources, dict):
        binding = data_sources.get("binding")
        if isinstance(binding, dict) and isinstance(binding.get("state"), str):
            return binding["state"]
    return None


def _reference_status(manifest: dict[str, object]) -> str | None:
    oracle = manifest.get("oracle")
    if isinstance(oracle, dict):
        if oracle.get("reference_missing") is True:
            return "missing"
        if oracle.get("partial") is True:
            return "partial"
        if oracle.get("reference_required") is True and not oracle.get("complete", False):
            return "partial"
        if oracle.get("complete") is True:
            return "complete"
    return None


def _stored_readiness(manifest: dict[str, object]) -> dict[str, Any] | None:
    for key in ("dispatch_readiness", "start_ready", "complete"):
        value = manifest.get(key)
        if isinstance(value, dict):
            return {"source": key, "last_observed": value, "current_certification": CERTIFICATION_NOT_CHECKED}
        if isinstance(value, str) and value in {"START_READY", "COMPLETE"}:
            return {"source": key, "last_observed": value, "current_certification": CERTIFICATION_NOT_CHECKED}
    return None


def _package_observations(run: Path, states: dict[str, str], findings: list[Finding]) -> list[PackageObservation]:
    observations: list[PackageObservation] = []
    for package in _discover_package_dirs(run, states, findings):
        classification = classify_target(package)
        integrity = verify_package(package, classification)
        manifest, manifest_reason = _package_manifest(package)
        unit = manifest.get("unit") if isinstance(manifest, dict) else None
        kind = manifest.get("kind") if isinstance(manifest, dict) else None
        unit_text = unit if isinstance(unit, str) and unit_name_problem(unit) is None else None
        kind_text = kind if isinstance(kind, str) and kind in KNOWN_KINDS - {"unknown"} else None
        if manifest_reason is not None:
            findings.append(
                Finding(
                    "PACKAGE_MANIFEST_UNASSESSABLE", f"package manifest is {manifest_reason}", source=_rel(package, run)
                )
            )
        elif unit_text is None:
            findings.append(
                Finding(
                    "PACKAGE_IDENTITY_UNSCOPED", "package manifest has no safe unit identity", source=_rel(package, run)
                )
            )
        observation = PackageObservation(
            relative_path=_rel(package, run),
            unit=unit_text,
            kind=kind_text,
            scope="pending",
            integrity_status=integrity.status,
            integrity_codes=list(integrity.codes()),
        )
        if isinstance(manifest, dict):
            construction = manifest.get("construction_status")
            if isinstance(construction, str):
                observation.construction_status = construction
            contained = manifest.get("self_contained")
            if isinstance(contained, bool):
                observation.self_contained = contained
            working = manifest.get("has_engine_working_copy")
            if isinstance(working, bool):
                observation.has_engine_working_copy = working
            observation.binding_state = _binding_state(manifest)
            observation.reference_status = _reference_status(manifest)
            observation.stored_readiness = _stored_readiness(manifest)
        observations.append(observation)
    return observations


def _assemble_units(
    run: Path,
    report_scope: str,
    occurrences: list[Occurrence],
    packages: list[PackageObservation],
    findings: list[Finding],
) -> tuple[list[UnitStatus], list[PackageObservation]]:
    reported_kinds_by_name: dict[str, set[str]] = {}
    for occurrence in occurrences:
        if occurrence.source == "engine_report":
            reported_kinds_by_name.setdefault(occurrence.unit, set()).add(occurrence.kind)
    for occurrence in occurrences:
        if occurrence.source == "working_copy":
            reported_kinds = reported_kinds_by_name.get(occurrence.unit, set())
            if len(reported_kinds) == 1:
                occurrence.kind = next(iter(reported_kinds))
    by_key: dict[tuple[str, str], list[Occurrence]] = {}
    for occurrence in occurrences:
        by_key.setdefault((occurrence.kind, occurrence.unit), []).append(occurrence)
    ambiguous_keys = {
        key
        for key, values in by_key.items()
        if sum(1 for occurrence in values if occurrence.source == "engine_report") > 1
    }
    for key in sorted(by_key):
        if key in ambiguous_keys:
            findings.append(
                Finding("AMBIGUOUS_UNIT_IDENTITY", "multiple occurrences share the same kind and unit", unit=key[1])
            )
    units: dict[tuple[str, str], UnitStatus] = {}
    for occurrence in occurrences:
        key = (occurrence.kind, occurrence.unit)
        status = units.setdefault(key, UnitStatus(unit=occurrence.unit, kind=occurrence.kind))
        status.occurrences.append(occurrence)
        if key in ambiguous_keys:
            status.scope = "ambiguous"
    for package in packages:
        if package.unit is None or package.kind is None:
            package.scope = "UNSCOPED_PACKAGE"
            continue
        key = (package.kind, package.unit)
        matched_unit = units.get(key)
        if matched_unit is not None and key not in ambiguous_keys:
            if matched_unit.package is not None:
                package.scope = "UNSCOPED_PACKAGE"
                findings.append(
                    Finding(
                        "DUPLICATE_PACKAGE_ASSOCIATION",
                        "more than one package claims the same unambiguous unit; "
                        "preserve and inspect the extra package",
                        unit=package.unit,
                    )
                )
                continue
            package.scope = "associated"
            matched_unit.package = package
        else:
            package.scope = "UNSCOPED_PACKAGE"
            if matched_unit is None and report_scope == "established":
                code = "PACKAGE_ONLY_WORK"
            elif matched_unit is None:
                code = "PACKAGE_UNSCOPED_IN_UNESTABLISHED_INVENTORY"
            else:
                code = "AMBIGUOUS_PACKAGE_IDENTITY"
            findings.append(
                Finding(code, "package cannot be associated unambiguously; preserve and inspect it", unit=package.unit)
            )
    pbip = run / "bundle" / "pbip"
    handover = run / "bundle" / "handover"
    for unit in units.values():
        unit.generated_working_copy = _dir_state(pbip / unit.unit)
        unit.handover = _optional_regular(handover / f"{unit.unit}.json")
        unit.findings = [finding for finding in findings if finding.unit == unit.unit]
        if unit.package is not None and unit.package.stored_readiness is not None:
            unit.findings.append(
                Finding(
                    "RECORDED_READINESS_NOT_CURRENT",
                    "stored readiness is last-observed only; current certification remains NOT_CHECKED",
                    unit=unit.unit,
                )
            )
    scoped_paths = {unit.package.relative_path for unit in units.values() if unit.package is not None}
    unscoped = [package for package in packages if package.relative_path not in scoped_paths]
    return sorted(units.values(), key=lambda unit: (unit.kind, unit.unit)), sorted(
        unscoped, key=lambda package: package.relative_path
    )


def _dir_state(path: Path) -> str:
    ok, reason = _safe_existing_dir(path)
    return "present" if ok else f"unassessable:{reason}" if reason not in {"missing", None} else "missing"


def _recorded_failures(run: Path) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    provenance, reason = _strict_json_file(run / "bundle" / "source-provenance.json")
    if reason is None and isinstance(provenance, dict):
        phase = provenance.get("phase")
        if isinstance(phase, dict) and phase.get("status") not in {None, "success", "local_only"}:
            for error in phase.get("errors", []) if isinstance(phase.get("errors"), list) else []:
                if isinstance(error, dict):
                    code = error.get("code") if isinstance(error.get("code"), str) else "recorded-error"
                    operation = error.get("operation") if isinstance(error.get("operation"), str) else "phase"
                    failures.append({"source": "bundle/source-provenance.json", "phase": operation, "code": code})
            if not failures:
                failures.append(
                    {"source": "bundle/source-provenance.json", "phase": "provenance", "code": str(phase.get("status"))}
                )
    timings, timing_reason = _strict_json_file(run / "bundle" / "phase-timings.json")
    if timing_reason is None and isinstance(timings, dict) and isinstance(timings.get("phases"), list):
        for phase in timings["phases"]:
            if isinstance(phase, dict) and isinstance(phase.get("exit_code"), int) and phase["exit_code"] != 0:
                name = phase.get("phase") if isinstance(phase.get("phase"), str) else "phase"
                failures.append(
                    {"source": "bundle/phase-timings.json", "phase": name, "code": f"exit-{phase['exit_code']}"}
                )
    return failures


def _next_action(
    run_ok: bool,
    report_scope: str,
    units: list[UnitStatus],
    unscoped: list[PackageObservation],
    failures: list[dict[str, str]],
) -> dict[str, Any]:
    if not run_ok:
        return {
            "headline": "Confirm the explicitly selected run before reading child evidence.",
            "affected_units": [],
            "details": [],
        }
    if failures:
        return {
            "headline": "Inspect the recorded failed phase without rebuilding or deleting retained work.",
            "affected_units": [],
            "details": failures,
        }
    if unscoped:
        return {
            "headline": (
                "Preserve package-only or ambiguous package work and inspect its manifest before associating it."
            ),
            "affected_units": sorted({package.unit or package.relative_path for package in unscoped}),
            "details": [package.as_dict() for package in unscoped],
        }
    unbound = [unit.unit for unit in units if unit.package is not None and unit.package.binding_state == "unbound"]
    if unbound:
        return {
            "headline": "Bind retained packages through the existing package binding command; do not rebuild to bind.",
            "affected_units": sorted(unbound),
            "details": [],
        }
    partial_refs = [
        unit.unit
        for unit in units
        if unit.package is not None and unit.package.reference_status in {"partial", "missing"}
    ]
    if partial_refs:
        return {
            "headline": (
                "Consolidate existing reference batches or run "
                "`python scripts/check_reference_readiness.py <provider-package> <consumer-package>` "
                "with explicit package paths."
            ),
            "affected_units": sorted(partial_refs),
            "details": [],
        }
    if report_scope != "established":
        return {
            "headline": "Repair or inspect the engine report before treating inventory scope as complete.",
            "affected_units": [],
            "details": [],
        }
    return {
        "headline": "Run the actual public readiness check if certification is needed; this command did not check it.",
        "affected_units": sorted(unit.unit for unit in units),
        "details": [],
    }


def build_status(run: Path) -> tuple[dict[str, Any], int]:
    findings: list[Finding] = []
    if not run.is_absolute():
        return {"error": "--run must be an absolute path", "selected_run": str(run)}, 2
    ok, reason = _safe_existing_dir(run)
    if not ok:
        return {"error": f"selected run is {reason}", "selected_run": str(run)}, 1
    manifest, manifest_reason = _strict_json_file(run / "run.json")
    if manifest_reason is not None:
        return {"error": f"run.json is {manifest_reason}", "selected_run": str(run)}, 1
    location = check_run_location(manifest, run)
    if location.state != RUN_LOCATION_INTACT:
        status = {
            "schema_version": 1,
            "selected_run": str(run),
            "run_location": location.as_dict(),
            "current_certification": CERTIFICATION_NOT_CHECKED,
            "findings": [Finding("RUN_LOCATION_UNASSESSABLE", location.detail).as_dict()],
            "next_action": _next_action(False, "unestablished", [], [], []),
        }
        return status, 1
    states, dir_findings = _canonical_dirs(run)
    findings.extend(dir_findings)
    report_scope, report_occurrences = _report_occurrences(run / "bundle", findings)
    all_occurrences = [*report_occurrences, *_pbip_occurrences(run, states, findings)]
    packages = _package_observations(run, states, findings)
    units, unscoped = _assemble_units(run, report_scope, all_occurrences, packages, findings)
    failures = _recorded_failures(run)
    next_action = _next_action(True, report_scope, units, unscoped, failures)
    status = {
        "schema_version": 1,
        "selected_run": str(run),
        "diagnostic_contract": "run-status inventory only; not START_READY, COMPLETE, or migration readiness",
        "run_recorded_status": manifest.get("status") if isinstance(manifest, dict) else None,
        "run_recorded_status_semantics": "allocation metadata only; not proof of process liveness",
        "run_location": location.as_dict(),
        "canonical_subdirs": states,
        "inventory_scope": report_scope,
        "current_certification": CERTIFICATION_NOT_CHECKED,
        "units": [unit.as_dict() for unit in units],
        "unscoped_packages": [package.as_dict() for package in unscoped],
        "recorded_failures": failures,
        "findings": [finding.as_dict() for finding in findings],
        "next_action": next_action,
    }
    exit_code = 1 if report_scope != "established" or dir_findings else 0
    return status, exit_code


def render_human(status: dict[str, Any]) -> str:
    if "error" in status:
        return f"RUN STATUS: UNASSESSABLE\nselected_run: {status['selected_run']}\nerror: {status['error']}"
    lines = [
        "RUN STATUS: DIAGNOSTIC ONLY (NOT readiness)",
        f"selected_run: {status['selected_run']}",
        f"run_location: {status['run_location']['state']}",
        f"recorded_status: {status.get('run_recorded_status')} (allocation metadata only)",
        f"inventory_scope: {status.get('inventory_scope', 'unestablished')}",
        f"current_certification: {status.get('current_certification', CERTIFICATION_NOT_CHECKED)}",
        f"units: {len(status.get('units', []))}",
        f"unscoped_packages: {len(status.get('unscoped_packages', []))}",
        f"recorded_failures: {len(status.get('recorded_failures', []))}",
        f"NEXT ACTION: {status['next_action']['headline']}",
    ]
    for unit in status.get("units", []):
        obs = unit["observations"]
        lines.append(
            f"UNIT {unit['kind']} {unit['unit']}: "
            f"scope={unit['scope']} "
            f"working_copy={obs['generated_working_copy']} "
            f"handover={obs['handover']} "
            f"certification={obs['current_certification']}"
        )
        if "package" in unit:
            package = unit["package"]
            lines.append(
                f"  PACKAGE {package['relative_path']}: "
                f"construction={package.get('construction_status', 'unknown')} "
                f"integrity={package['integrity_status']} "
                f"binding={package.get('binding_state', 'unknown')} "
                f"reference={package.get('reference_status', 'unknown')}"
            )
    for package in status.get("unscoped_packages", []):
        lines.append(
            f"UNSCOPED_PACKAGE {package['relative_path']}: "
            f"unit={package.get('unit')} "
            f"kind={package.get('kind')} "
            f"integrity={package['integrity_status']} "
            "guidance=preserve-and-inspect"
        )
    for failure in status.get("recorded_failures", []):
        lines.append(f"RECORDED_FAILURE {failure['source']} phase={failure['phase']} code={failure['code']}")
    for finding in status.get("findings", []):
        unit = f" unit={finding['unit']}" if "unit" in finding else ""
        source = f" source={finding['source']}" if "source" in finding else ""
        lines.append(f"FINDING {finding['code']}{unit}{source}: {finding['message']}")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print read-only run-status diagnostics for one absolute run directory."
    )
    parser.add_argument("--run", required=True, type=Path, help="absolute _runs/<NNN>-<slug> directory to inspect")
    parser.add_argument("--json", action="store_true", help="print machine-readable observations")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    status, exit_code = build_status(args.run)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(render_human(status))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
