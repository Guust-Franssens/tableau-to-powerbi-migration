"""
Resolve canonical package/PBIP/report/model/PBIR identity for a PID-scoped Desktop instance.

Internal module consumed by ``capture_powerbi_pages.py``.  Returns typed evidence or an explicit
non-clean refusal.  Writes no iteration, screenshot, receipt, comparison or sign-off artifact.

Usage (internal)::

    identity = resolve_target(fabric_dir, pid=1234, bridge_runner=run_bridge_status)
"""

# pylint: disable=too-many-return-statements,too-few-public-methods,too-many-locals

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Strict JSON parsing — reject duplicate keys
# ---------------------------------------------------------------------------


class _DuplicateKeyError(ValueError):
    """Raised when a JSON object contains duplicate keys."""


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def strict_json_loads(text: str) -> Any:
    """``json.loads`` that rejects duplicate keys."""
    return json.loads(text, object_pairs_hook=_strict_object_pairs)


def _read_strict_json(path: Path) -> dict[str, Any] | _DuplicateKeyError | Exception:
    """Read and parse a JSON file with strict duplicate-key checking.  Returns the doc or an error."""
    try:
        return strict_json_loads(path.read_text(encoding="utf-8"))
    except (_DuplicateKeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return exc


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageIdentity:
    """One PBIR page."""

    page_dir: str  # package-relative page dir
    page_id: str  # folder name
    display_name: str
    visual_ids: tuple[str, ...]  # sorted folder names of visuals


@dataclass(frozen=True)
class TargetIdentity:
    """Canonical identity of the PBIP package open in a Desktop instance."""

    pbip_path: str  # package-relative, e.g. "MyReport.pbip"
    report_dir: str  # package-relative, e.g. "MyReport.Report"
    model_binding: str  # byPath target, e.g. "../MyReport.SemanticModel"
    pages: tuple[PageIdentity, ...]
    revision_digest: str  # SHA-256 over all PBIR definition files
    definition_files: tuple[str, ...]  # sorted package-relative paths in the digest
    _bridge_file_path: str = field(repr=False)  # local-only — never share


@dataclass(frozen=True)
class TargetIdentityRefusal:
    """Explicit non-clean refusal with a reason."""

    reason: str


# ---------------------------------------------------------------------------
# Bridge status protocol
# ---------------------------------------------------------------------------


class BridgeRunner(Protocol):
    """Callable that returns (returncode, stdout+stderr) for ``status --pid <pid>``."""

    def __call__(self, pid: int) -> tuple[int, str]: ...


def default_bridge_runner(pid: int) -> tuple[int, str]:
    """Run ``powerbi-desktop status --pid <pid>`` through the bridge CLI."""
    try:
        proc = subprocess.run(
            ["powerbi-desktop", "status", "--pid", str(pid)],
            capture_output=True,
            text=True,
            shell=True,
            check=False,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return (1, "bridge status timed out")
    return (proc.returncode, proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _is_contained(rel: str) -> bool:
    """True if *rel* is a relative path that stays within its root."""
    if not rel or rel.startswith("/") or rel.startswith("\\"):
        return False
    return ".." not in PurePosixPath(rel.replace("\\", "/")).parts


# ---------------------------------------------------------------------------
# Internal resolution steps
# ---------------------------------------------------------------------------


def _resolve_pbip(fabric_dir: Path) -> tuple[Path, dict[str, Any]] | TargetIdentityRefusal:
    """Find and parse exactly one .pbip file."""
    pbip_files = sorted(fabric_dir.glob("*.pbip"))
    if not pbip_files:
        return TargetIdentityRefusal("no .pbip file found in " + str(fabric_dir))
    if len(pbip_files) > 1:
        return TargetIdentityRefusal(
            f"multiple .pbip files: {', '.join(p.name for p in pbip_files)}"
        )
    pbip_path = pbip_files[0]
    doc = _read_strict_json(pbip_path)
    if isinstance(doc, Exception):
        return TargetIdentityRefusal(f"malformed .pbip JSON: {doc}")
    return pbip_path, doc


def _resolve_report_dir(
    pbip_doc: dict[str, Any], fabric_dir: Path
) -> tuple[str, Path] | TargetIdentityRefusal:
    """Resolve the report artifact path from the .pbip doc."""
    artifacts = pbip_doc.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return TargetIdentityRefusal(".pbip has no artifacts list")
    report_entries = [a for a in artifacts if isinstance(a, dict) and "report" in a]
    if len(report_entries) != 1:
        return TargetIdentityRefusal(
            f"expected exactly 1 report artifact, found {len(report_entries)}"
        )
    report_rel = report_entries[0]["report"].get("path", "")
    if not isinstance(report_rel, str) or not report_rel:
        return TargetIdentityRefusal("report artifact has no path")
    if not _is_contained(report_rel):
        return TargetIdentityRefusal(
            f"report path is absolute, traversing, or foreign: {report_rel!r}"
        )
    if not report_rel.endswith(".Report"):
        return TargetIdentityRefusal(f"report path does not end with .Report: {report_rel!r}")
    report_dir = fabric_dir / report_rel
    if not report_dir.is_dir():
        return TargetIdentityRefusal(f"report directory does not exist: {report_rel}")
    return report_rel, report_dir


def _resolve_model_binding(
    report_dir: Path, fabric_dir: Path
) -> str | TargetIdentityRefusal:
    """Parse definition.pbir and resolve the semantic-model binding."""
    pbir_path = report_dir / "definition.pbir"
    if not pbir_path.is_file():
        return TargetIdentityRefusal(
            f"definition.pbir not found in {report_dir.name}"
        )
    doc = _read_strict_json(pbir_path)
    if isinstance(doc, Exception):
        return TargetIdentityRefusal(f"malformed definition.pbir JSON: {doc}")
    dataset_ref = doc.get("datasetReference")
    if not isinstance(dataset_ref, dict):
        return TargetIdentityRefusal("definition.pbir has no datasetReference")
    by_path = dataset_ref.get("byPath")
    by_conn = dataset_ref.get("byConnection")
    count = sum(1 for x in (by_path, by_conn) if x is not None)
    if count == 0:
        return TargetIdentityRefusal(
            "definition.pbir has no model binding (byPath or byConnection)"
        )
    if count > 1:
        return TargetIdentityRefusal("definition.pbir has multiple model bindings")
    if by_path is not None:
        return _validate_by_path(by_path, report_dir, fabric_dir)
    return _format_by_connection(by_conn)


def _validate_by_path(
    by_path: Any, report_dir: Path, fabric_dir: Path
) -> str | TargetIdentityRefusal:
    path_str = by_path.get("path", "") if isinstance(by_path, dict) else ""
    if not isinstance(path_str, str) or not path_str:
        return TargetIdentityRefusal("byPath binding has no path")
    target = (report_dir / path_str).resolve()
    if not target.is_dir():
        return TargetIdentityRefusal(f"byPath target does not exist: {path_str!r}")
    try:
        target.relative_to(fabric_dir)
    except ValueError:
        return TargetIdentityRefusal(f"byPath target is outside the package: {path_str!r}")
    return path_str


def _format_by_connection(by_conn: Any) -> str:
    if isinstance(by_conn, dict):
        name = by_conn.get("connectionString", "") or by_conn.get("name", "")
    else:
        name = ""
    return f"byConnection:{name}" if name else "byConnection"


def _enumerate_page(
    entry: Path, report_rel: str
) -> PageIdentity | TargetIdentityRefusal:
    """Enumerate one page directory and its visuals."""
    page_id = entry.name
    page_json = entry / "page.json"
    if not page_json.is_file():
        return TargetIdentityRefusal(f"page.json missing in page {page_id}")
    doc = _read_strict_json(page_json)
    if isinstance(doc, Exception):
        return TargetIdentityRefusal(f"malformed page.json in {page_id}: {doc}")
    display_name = doc.get("displayName", page_id)
    visual_ids: list[str] = []
    seen: set[str] = set()
    visuals_dir = entry / "visuals"
    if visuals_dir.is_dir():
        for v_entry in sorted(visuals_dir.iterdir()):
            if not v_entry.is_dir():
                continue
            vid = v_entry.name
            if vid in seen:
                return TargetIdentityRefusal(
                    f"duplicate visual directory in page {page_id}: {vid}"
                )
            seen.add(vid)
            vj = v_entry / "visual.json"
            if not vj.is_file():
                return TargetIdentityRefusal(
                    f"visual.json missing in {page_id}/visuals/{vid}"
                )
            vdoc = _read_strict_json(vj)
            if isinstance(vdoc, Exception):
                return TargetIdentityRefusal(
                    f"malformed visual.json in {page_id}/visuals/{vid}: {vdoc}"
                )
            visual_ids.append(vid)
    page_rel = str(PurePosixPath(report_rel) / "definition" / "pages" / page_id)
    return PageIdentity(page_rel, page_id, display_name, tuple(visual_ids))


def _enumerate_pages(
    report_dir: Path, report_rel: str
) -> tuple[tuple[PageIdentity, ...], Path] | TargetIdentityRefusal:
    """Enumerate all pages and validate report.json."""
    defn = report_dir / "definition"
    if not defn.is_dir():
        return TargetIdentityRefusal(f"definition/ directory missing in {report_rel}")
    rj = defn / "report.json"
    if not rj.is_file():
        return TargetIdentityRefusal("report.json missing in definition/")
    doc = _read_strict_json(rj)
    if isinstance(doc, Exception):
        return TargetIdentityRefusal(f"malformed report.json: {doc}")
    pages_dir = defn / "pages"
    if not pages_dir.is_dir():
        return TargetIdentityRefusal("pages/ directory missing in definition/")
    results: list[PageIdentity] = []
    seen: set[str] = set()
    for entry in sorted(pages_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in seen:
            return TargetIdentityRefusal(f"duplicate page directory: {entry.name}")
        seen.add(entry.name)
        page = _enumerate_page(entry, report_rel)
        if isinstance(page, TargetIdentityRefusal):
            return page
        results.append(page)
    if not results:
        return TargetIdentityRefusal("no page directories found under definition/pages/")
    return tuple(results), defn


def _compute_revision_digest(
    report_rel: str, definition_dir: Path, fabric_dir: Path
) -> tuple[str, tuple[str, ...]]:
    """Compute a deterministic revision digest over all PBIR definition files."""
    def_files: list[str] = []

    def collect(base: Path, prefix: str) -> None:
        for child in sorted(base.iterdir()):
            rel = f"{prefix}/{child.name}" if prefix else child.name
            if child.is_file() and child.suffix == ".json":
                def_files.append(rel)
            elif child.is_dir():
                collect(child, rel)

    collect(definition_dir, "")
    pkg_files = sorted(
        [str(PurePosixPath(report_rel) / "definition.pbir")]
        + [str(PurePosixPath(report_rel) / "definition" / f) for f in def_files]
    )
    digest = hashlib.sha256()
    for rel in pkg_files:
        full = fabric_dir / rel.replace("/", os.sep)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(full.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest(), tuple(pkg_files)


def _validate_bridge_status(
    pid: int, canonical_pbip: str, bridge_runner: BridgeRunner
) -> str | TargetIdentityRefusal:
    """Validate bridge status for *pid* and return the bridge file path."""
    rc, output = bridge_runner(pid)
    if rc != 0:
        return TargetIdentityRefusal(
            f"bridge status --pid {pid} failed (rc={rc}): {output.strip()[:200]}"
        )
    try:
        start = output.index("{")
        end = output.rindex("}") + 1
        status_doc = json.loads(output[start:end])
    except (ValueError, json.JSONDecodeError) as exc:
        return TargetIdentityRefusal(f"bridge status returned non-JSON: {exc}")
    instances = status_doc.get("instances") or status_doc.get("Instances") or []
    if not isinstance(instances, list):
        return TargetIdentityRefusal("bridge status has no instances array")
    matches = [
        inst
        for inst in instances
        if isinstance(inst, dict) and int(inst.get("pid") or inst.get("Pid") or -1) == pid
    ]
    if not matches:
        return TargetIdentityRefusal(f"bridge status has no instance for pid {pid}")
    if len(matches) > 1:
        return TargetIdentityRefusal(
            f"bridge status has {len(matches)} entries for pid {pid}"
        )
    bridge_file = str(
        matches[0].get("currentFilePath") or matches[0].get("CurrentFilePath") or ""
    )
    if not bridge_file:
        return TargetIdentityRefusal(
            f"bridge status for pid {pid} has no currentFilePath"
        )
    if sys.platform == "win32":
        ok = os.path.normcase(bridge_file) == os.path.normcase(canonical_pbip)
    else:
        ok = bridge_file == canonical_pbip
    if not ok:
        return TargetIdentityRefusal(
            f"bridge currentFilePath does not match canonical PBIP "
            f"(bridge={bridge_file!r}, canonical={canonical_pbip!r})"
        )
    return bridge_file


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_target(
    fabric_dir: Path,
    *,
    pid: int,
    bridge_runner: BridgeRunner = default_bridge_runner,
) -> TargetIdentity | TargetIdentityRefusal:
    """Resolve canonical target identity or return an explicit refusal."""
    fabric_dir = fabric_dir.resolve()

    pbip_result = _resolve_pbip(fabric_dir)
    if isinstance(pbip_result, TargetIdentityRefusal):
        return pbip_result
    pbip_path, pbip_doc = pbip_result

    report_result = _resolve_report_dir(pbip_doc, fabric_dir)
    if isinstance(report_result, TargetIdentityRefusal):
        return report_result
    report_rel, report_dir = report_result

    model = _resolve_model_binding(report_dir, fabric_dir)
    if isinstance(model, TargetIdentityRefusal):
        return model

    pages_result = _enumerate_pages(report_dir, report_rel)
    if isinstance(pages_result, TargetIdentityRefusal):
        return pages_result
    page_ids, definition_dir = pages_result

    digest, def_files = _compute_revision_digest(
        report_rel, definition_dir, fabric_dir
    )

    bridge = _validate_bridge_status(pid, str(pbip_path), bridge_runner)
    if isinstance(bridge, TargetIdentityRefusal):
        return bridge

    return TargetIdentity(
        pbip_path=pbip_path.name,
        report_dir=report_rel,
        model_binding=model,
        pages=page_ids,
        revision_digest=digest,
        definition_files=def_files,
        _bridge_file_path=bridge,
    )
