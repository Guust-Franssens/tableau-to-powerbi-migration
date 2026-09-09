"""Capture receipt: resolve a package, read its PBIR inventory, and validate/write receipts.

Slice A of #363 — immutable capture authority.  A capture receipt is generated evidence recording
exactly what was captured and how, never an agent-authored verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "https://github.com/Guust-Franssens/tableau-to-powerbi-migration/schemas/capture-receipt/1.0.0"
RECEIPT_VERSION = "1.0.0"

# Canonical iteration directory name: exactly three zero-padded digits.
_ITERATION_RE = re.compile(r"^(\d{3})$")


# ---------------------------------------------------------------------------
# Package resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageIdentity:
    """Resolved identity of a PBIP package."""

    package_root: Path
    pbip_path: Path
    report_folder: Path
    definition_pbir: Path
    pbip_sha256: str
    definition_pbir_sha256: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_package(package_root: Path) -> PackageIdentity:
    """Find the unique ``.pbip``, its report folder, and ``definition.pbir``.

    Raises ``ValueError`` on zero, multiple, or malformed results.
    """
    fabric = package_root / "fabric"
    if not fabric.is_dir():
        raise ValueError(f"no fabric/ directory in package: {package_root}")
    pbips = sorted(fabric.glob("*.pbip"))
    if len(pbips) == 0:
        raise ValueError(f"no .pbip file found under {fabric}")
    if len(pbips) > 1:
        raise ValueError(f"multiple .pbip files found: {[p.name for p in pbips]}")
    pbip = pbips[0]
    try:
        pbip_doc = json.loads(pbip.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"cannot read {pbip}: {exc}") from exc
    artifacts = pbip_doc.get("artifacts") or []
    report_paths = [a["report"]["path"] for a in artifacts if "report" in a]
    if len(report_paths) != 1:
        raise ValueError(f"expected exactly one report artifact in {pbip.name}, got {len(report_paths)}")
    report_folder = fabric / report_paths[0]
    if not report_folder.is_dir():
        raise ValueError(f"report folder does not exist: {report_folder}")
    definition_pbir = report_folder / "definition.pbir"
    if not definition_pbir.is_file():
        raise ValueError(f"missing definition.pbir in {report_folder}")
    return PackageIdentity(
        package_root=package_root.resolve(),
        pbip_path=pbip.resolve(),
        report_folder=report_folder.resolve(),
        definition_pbir=definition_pbir.resolve(),
        pbip_sha256=_sha256(pbip),
        definition_pbir_sha256=_sha256(definition_pbir),
    )


# ---------------------------------------------------------------------------
# PBIR page + visual inventory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisualInfo:
    """Identity of a single visual in a page."""

    name: str


@dataclass(frozen=True)
class PageInfo:
    """Identity and visuals of a single PBIR page."""

    page_id: str
    display_name: str
    visuals: tuple[VisualInfo, ...]


def _read_visuals(visuals_dir: Path, page_id: str) -> list[VisualInfo]:
    """Read visual inventory for one page."""
    seen: set[str] = set()
    out: list[VisualInfo] = []
    if not visuals_dir.is_dir():
        return out
    for visual_json in sorted(visuals_dir.glob("*/visual.json")):
        try:
            vdoc = json.loads(visual_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"cannot read {visual_json}: {exc}") from exc
        vname = vdoc.get("name")
        if not isinstance(vname, str) or not vname:
            raise ValueError(f"missing or empty name in {visual_json}")
        if vname in seen:
            raise ValueError(f"duplicate visual id {vname!r} on page {page_id}")
        seen.add(vname)
        out.append(VisualInfo(name=vname))
    return out


def read_pbir_inventory(report_folder: Path) -> list[PageInfo]:
    """Read every page and its visuals from the PBIR definition on disk.

    Raises ``ValueError`` on unreadable, malformed, or duplicate-identity data.
    """
    page_root = report_folder / "definition" / "pages"
    if not page_root.is_dir():
        raise ValueError(f"no pages directory: {page_root}")
    pages_out: list[PageInfo] = []
    seen_page_ids: set[str] = set()
    for page_json in sorted(page_root.glob("*/page.json")):
        page_dir = page_json.parent
        page_id = page_dir.name
        if page_id in seen_page_ids:
            raise ValueError(f"duplicate page id: {page_id}")
        seen_page_ids.add(page_id)
        try:
            doc = json.loads(page_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"cannot read {page_json}: {exc}") from exc
        display_name = doc.get("displayName")
        if not isinstance(display_name, str) or not display_name:
            raise ValueError(f"missing or empty displayName in {page_json}")
        visuals = _read_visuals(page_dir / "visuals", page_id)
        pages_out.append(PageInfo(page_id=page_id, display_name=display_name, visuals=tuple(visuals)))
    if not pages_out:
        raise ValueError(f"no pages found under {page_root}")
    return pages_out


# ---------------------------------------------------------------------------
# Bridge status validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeStatus:
    """Parsed bridge status for one Desktop instance."""

    pid: int
    current_file_path: str


StatusQuerier = Any  # Callable[[str], tuple[int, str]]  — but kept loose for injection


def parse_bridge_status(raw_json: str) -> list[BridgeStatus]:
    """Parse the bridge ``status`` JSON into instance records."""
    try:
        idx_start = raw_json.index("{")
        idx_end = raw_json.rindex("}") + 1
        payload = json.loads(raw_json[idx_start:idx_end])
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse bridge status: {exc}") from exc
    instances = payload.get("instances") or payload.get("Instances") or []
    out: list[BridgeStatus] = []
    for inst in instances if isinstance(instances, list) else []:
        cfp = inst.get("currentFilePath") or inst.get("CurrentFilePath") or ""
        pid = inst.get("pid") or inst.get("Pid")
        if pid and cfp:
            out.append(BridgeStatus(pid=int(pid), current_file_path=str(cfp)))
    return out


def validate_bridge_open(
    pid: str,
    canonical_pbip: Path,
    status_instances: list[BridgeStatus],
) -> None:
    """Require that *exactly* the given PID has ``currentFilePath`` matching the canonical PBIP.

    Raises ``ValueError`` on mismatch, missing instance, or ambiguity.
    """
    target_pid = int(pid)
    target_path = str(canonical_pbip.resolve()).casefold()
    match = None
    for inst in status_instances:
        if inst.pid == target_pid:
            if str(inst.current_file_path).casefold() == target_path:
                match = inst
            else:
                raise ValueError(f"PID {pid} has currentFilePath {inst.current_file_path!r}, expected {canonical_pbip}")
    if match is None:
        raise ValueError(f"PID {pid} not found in bridge status")


# ---------------------------------------------------------------------------
# Iteration allocation
# ---------------------------------------------------------------------------


def allocate_iteration(package_root: Path) -> tuple[Path, str]:
    """Atomically allocate the next ``validation/iterations/<NNN>/`` directory.

    Returns ``(iteration_dir, iteration_id)`` where ``iteration_id`` is the
    zero-padded string like ``"001"``.  Rejects non-canonical names that may
    already exist (e.g. ``1`` beside ``001``).

    Raises ``ValueError`` if a non-canonical sibling is found, or
    ``OSError`` on a true filesystem race (mkdir-exclusive).
    """
    iterations_root = package_root / "validation" / "iterations"
    iterations_root.mkdir(parents=True, exist_ok=True)
    # Reject non-canonical siblings
    for entry in iterations_root.iterdir():
        if entry.is_dir() and not _ITERATION_RE.match(entry.name):
            raise ValueError(
                f"non-canonical iteration directory {entry.name!r} in {iterations_root}; "
                "remove it before allocating a new iteration"
            )
    # Find next number
    existing = sorted(
        int(m.group(1)) for d in iterations_root.iterdir() if d.is_dir() and (m := _ITERATION_RE.match(d.name))
    )
    next_num = (existing[-1] + 1) if existing else 1
    iteration_id = f"{next_num:03d}"
    iteration_dir = iterations_root / iteration_id
    # Atomic mkdir — fails if another process races us
    iteration_dir.mkdir()  # raises FileExistsError on race
    return iteration_dir, iteration_id


# ---------------------------------------------------------------------------
# Receipt construction and validation
# ---------------------------------------------------------------------------


@dataclass
class PageCapture:  # pylint: disable=too-many-instance-attributes
    """Capture evidence for one page."""

    page_id: str
    display_name: str
    visual_ids: list[str]
    screenshot_relative_path: str
    screenshot_sha256: str
    screenshot_bytes: int
    converged: bool
    frames: int
    elapsed_seconds: float


@dataclass
class CaptureReceipt:  # pylint: disable=too-many-instance-attributes
    """The complete capture receipt for one iteration."""

    schema: str = field(default=RECEIPT_SCHEMA)
    version: str = field(default=RECEIPT_VERSION)
    iteration_id: str = ""
    mode: str = ""  # "sign-off" | "triage" | "spot-check"
    scope: str = ""  # "all-pages" | "subset"
    package_root: str = ""
    pbip_path: str = ""
    pbip_sha256: str = ""
    report_folder: str = ""
    definition_pbir_sha256: str = ""
    current_file_path: str = ""
    tool_version: str = RECEIPT_VERSION
    timestamp: str = ""
    stable_seconds: float = 0.0
    poll_seconds: float = 0.0
    max_wait_seconds: float = 0.0
    pages: list[PageCapture] = field(default_factory=list)


def receipt_to_dict(receipt: CaptureReceipt) -> dict[str, Any]:
    """Serialize a receipt to a JSON-safe dict with no duplicate keys."""
    return {
        "$schema": receipt.schema,
        "version": receipt.version,
        "iteration_id": receipt.iteration_id,
        "mode": receipt.mode,
        "scope": receipt.scope,
        "package_root": receipt.package_root,
        "pbip_path": receipt.pbip_path,
        "pbip_sha256": receipt.pbip_sha256,
        "report_folder": receipt.report_folder,
        "definition_pbir_sha256": receipt.definition_pbir_sha256,
        "current_file_path": receipt.current_file_path,
        "tool_version": receipt.tool_version,
        "timestamp": receipt.timestamp,
        "stable_dwell": {
            "stable_seconds": receipt.stable_seconds,
            "poll_seconds": receipt.poll_seconds,
            "max_wait_seconds": receipt.max_wait_seconds,
        },
        "pages": [
            {
                "page_id": p.page_id,
                "display_name": p.display_name,
                "visual_ids": p.visual_ids,
                "screenshot": {
                    "relative_path": p.screenshot_relative_path,
                    "sha256": p.screenshot_sha256,
                    "bytes": p.screenshot_bytes,
                },
                "convergence": {
                    "converged": p.converged,
                    "frames": p.frames,
                    "elapsed_seconds": p.elapsed_seconds,
                },
            }
            for p in receipt.pages
        ],
    }


_REQUIRED_TOP_KEYS = {
    "$schema",
    "version",
    "iteration_id",
    "mode",
    "scope",
    "package_root",
    "pbip_path",
    "pbip_sha256",
    "report_folder",
    "definition_pbir_sha256",
    "current_file_path",
    "tool_version",
    "timestamp",
    "stable_dwell",
    "pages",
}

_REQUIRED_PAGE_KEYS = {"page_id", "display_name", "visual_ids", "screenshot", "convergence"}
_REQUIRED_SCREENSHOT_KEYS = {"relative_path", "sha256", "bytes"}
_REQUIRED_CONVERGENCE_KEYS = {"converged", "frames", "elapsed_seconds"}
_REQUIRED_DWELL_KEYS = {"stable_seconds", "poll_seconds", "max_wait_seconds"}


def _validate_sub_object(
    data: dict[str, Any],
    required: set[str],
    prefix: str,
    errors: list[str],
) -> None:
    extra = set(data.keys()) - required
    if extra:
        errors.append(f"{prefix} additional keys: {sorted(extra)}")
    missing = required - set(data.keys())
    if missing:
        errors.append(f"{prefix} missing keys: {sorted(missing)}")


def _validate_page(page: dict[str, Any], idx: int, errors: list[str]) -> None:
    prefix = f"pages[{idx}]"
    _validate_sub_object(page, _REQUIRED_PAGE_KEYS, prefix, errors)
    ss = page.get("screenshot")
    if isinstance(ss, dict):
        _validate_sub_object(ss, _REQUIRED_SCREENSHOT_KEYS, f"{prefix}.screenshot", errors)
        rp = ss.get("relative_path")
        if isinstance(rp, str) and (os.path.isabs(rp) or ".." in rp.split("/")):
            errors.append(f"{prefix}.screenshot.relative_path must be contained (no .. or absolute)")
        if "sha256" in ss and not isinstance(ss.get("sha256"), str):
            errors.append(f"{prefix}.screenshot.sha256 must be a string")
        if "bytes" in ss and not isinstance(ss.get("bytes"), int):
            errors.append(f"{prefix}.screenshot.bytes must be an integer")
    cv = page.get("convergence")
    if isinstance(cv, dict):
        _validate_sub_object(cv, _REQUIRED_CONVERGENCE_KEYS, f"{prefix}.convergence", errors)


def validate_receipt(data: dict[str, Any]) -> list[str]:
    """Return a list of validation errors (empty = valid)."""
    errors: list[str] = []
    _validate_sub_object(data, _REQUIRED_TOP_KEYS, "top-level", errors)
    for key in ("$schema", "version", "iteration_id", "mode", "scope", "timestamp", "tool_version"):
        if key in data and not isinstance(data[key], str):
            errors.append(f"{key} must be a string")
    dwell = data.get("stable_dwell")
    if isinstance(dwell, dict):
        _validate_sub_object(dwell, _REQUIRED_DWELL_KEYS, "stable_dwell", errors)
    elif dwell is not None:
        errors.append("stable_dwell must be an object")
    pages_list = data.get("pages")
    if not isinstance(pages_list, list):
        errors.append("pages must be an array")
    else:
        for i, page in enumerate(pages_list):
            if not isinstance(page, dict):
                errors.append(f"pages[{i}] must be an object")
                continue
            _validate_page(page, i, errors)
    return errors
