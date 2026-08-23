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
live here because no existing gate owned those questions.
"""

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
from bundle_corpus import shipping_models, shipping_reports

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent

STATUS_PASS = "PASS"
STATUS_AUTOMATED_PASS = "AUTOMATED_CHECKS_PASS"
STATUS_FINDINGS = "FINDINGS"
STATUS_NOT_CHECKED = "NOT_CHECKED"
STATUS_PRECONDITION_FAILED = "PRECONDITION_FAILED"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_NOT_CHECKED = 2
EXIT_PRECONDITION_FAILED = 4
EXIT_USAGE = 64

EXEMPTIONS_FILE = "unit-check-exemptions.json"
VALID_EXEMPTION_CHECKS = frozenset({"stub-measures", "page-parity"})

SCOPE_MODEL = "model"
SCOPE_REPORT = "report"
SCOPE_INTEGRATION = "integration"
SCOPE_ALL = "all"
SCOPES = (SCOPE_MODEL, SCOPE_REPORT, SCOPE_INTEGRATION, SCOPE_ALL)
MODEL_CHECK_IDS = frozenset(
    {
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
INTEGRATION_CHECK_IDS = frozenset({"blank-placeholders", "field-bindings"})
ALL_ONLY_CHECK_IDS = frozenset(
    {"engine-receipt", "desktop-orphans", "visual-layer-done", "visual-comparison-done", "finalized"}
)

OWNER_HINTS = {
    "blank-placeholders": "integration (model placeholder referenced by report)",
    "field-bindings": "integration (report reference vs model field)",
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
            invalid.append(
                {
                    "item": item or f"#{index}",
                    "reason": "requires check in {page-parity, stub-measures}, item, reason, and decided_by",
                }
            )
            continue
        entries.append({"check": check, "item": item, "reason": reason, "decided_by": decided_by})
    return {"path": str(path), "entries": entries, "invalid": invalid}


def _exempted(entries: list[dict[str, str]], check: str, item: str, aliases: set[str] | None = None) -> bool:
    wanted = {item, *(aliases or set())}
    normalized = {_slug(value) for value in wanted if value}
    return any(entry["check"] == check and _slug(entry["item"]) in normalized for entry in entries)


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


def _append_cli_checks(checks: list[dict[str, Any]], target: Path, exemptions: dict[str, Any], scope: str) -> None:
    """Append native CLI-backed checks for the selected scope."""
    cli_gates = [gate for gate in GATES if _in_scope(gate.check_id, scope)]
    if not cli_gates and not _in_scope("occlusion", scope):
        return
    output_dir = _temp_json_dir(target)
    try:
        for gate in cli_gates:
            check = _run_cli_gate(gate, target, output_dir)
            if gate.check_id == "stub-measures":
                check = _apply_stub_exemptions(check, exemptions)
            checks.append(check)
        if _in_scope("occlusion", scope):
            checks.append(check_occlusion(target, output_dir))
    finally:
        _remove_json_dir(output_dir)


def _append_model_readiness_checks(checks: list[dict[str, Any]], target: Path, scope: str) -> None:
    """Append model readiness checks for the selected scope."""
    for check_id, check_func in (
        ("ai-descriptions", check_ai_descriptions),
        ("ai-instructions", check_ai_instructions),
        ("cache-freshness", check_cache_freshness),
    ):
        if _in_scope(check_id, scope):
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
    _append_cli_checks(checks, target, exemptions, scope)
    _append_model_readiness_checks(checks, target, scope)
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
            for child_key in ("findings", "unresolved", "skipped", "models", "reports"):
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
    if check.get("stderr"):
        lines.append(f"      stderr: {check['stderr'][:500]}")
    return lines


def _count_suffix(missing: int) -> str:
    return f"   [{missing} MISSING]" if missing else ""


def _summary_line(report: dict[str, Any]) -> str:
    """Stable one-line aggregate for comparing repeated gate runs."""
    owner_findings: dict[str, int] = {}
    structural = 0
    missing_input = 0
    for check in report["checks"]:
        owner = OWNER_HINTS.get(check["id"], "unknown")
        if check["status"] in {STATUS_FINDINGS, STATUS_PRECONDITION_FAILED}:
            owner_findings[owner] = owner_findings.get(owner, 0) + 1
        if check["status"] == STATUS_NOT_CHECKED:
            if check.get("verification") == "CLAIMED_ONLY":
                structural += 1
            else:
                missing_input += 1
    findings = ",".join(f"{owner}={count}" for owner, count in sorted(owner_findings.items())) or "none"
    return (
        "SUMMARY: "
        f"findings_by_owner={findings}; "
        f"not_checked_structural={structural}; "
        f"not_checked_missing_input={missing_input}; "
        f"ladder={report['status']} exit={report['exit_code']}"
    )


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
        check_id = check["id"]
        status = check["status"]
        if check_id == "oracle-coverage" and "pages" in check:
            pages = check["pages"]
            visual_missing = pages - check["visual_present"]
            numeric_missing = pages - check["numeric_present"]
            lines.append(
                f"  oracle coverage:  {check['visual_present']} of {pages} pages have a visual oracle"
                f"{_count_suffix(visual_missing)}"
            )
            lines.append(
                f"                    {check['numeric_present']} of {pages} pages have a numeric oracle"
                f"{_count_suffix(numeric_missing)}"
            )
            lines.append(f"                    grade: {check['grade']}  [{status}]")
        elif check_id == "page-parity" and "expected_count" in check:
            lines.append(
                f"  page-count parity: {check['actual_count']} PBIR page(s) for "
                f"{check['effective_expected_count']} expected Tableau dashboard(s)  [{status}]"
            )
            if check.get("exemptions"):
                lines.append(f"                    exemptions accepted: {len(check['exemptions'])}")
        elif check_id == "stub-measures" and "stub_exemptions" in check:
            lines.append(
                f"  {check_id}: {status} (native {check['native_status']} exit {check['native_exit']}; "
                f"{check['stub_exemptions']} exempted, {check['unexempted_stubs']} unexempted)"
            )
        elif "native_exit" in check:
            lines.append(f"  {check_id}: {status} (native {check['native_status']} exit {check['native_exit']})")
        else:
            detail = f" - {check['detail']}" if check.get("detail") else ""
            lines.append(f"  {check_id}: {status}{detail}")
        lines.extend(_render_actionable_detail(check))
    ex = report["exemptions"]
    if ex["accepted"] or ex["invalid"]:
        lines.append(f"  documented why-not exemptions: {ex['accepted']} accepted, {ex['invalid']} invalid")
    lines.append(_summary_line(report))
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
