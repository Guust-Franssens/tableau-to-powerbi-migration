"""
purpose: roll up per-unit migration status across an entire run from observable repository and run artifacts.
usage:   python scripts/build_run_unit_status.py --run _runs/<NNN>-<slug> [--survey PATH]
                                                 [--bundle DIR] [--packages DIR] [--assets DIR]
                                                 [--migrations-root DIR] [--out-md FILE]
                                                 [--out-json FILE] [--json] [--quiet]
         python scripts/build_run_unit_status.py --survey <estate_survey.json> [--bundle DIR]
                                                 [--packages DIR] [--assets DIR]
                                                 [--migrations-root DIR] [--out-md FILE]
                                                 [--out-json FILE] [--json] [--quiet]

Why this exists
---------------
Operators and automation need a single, authoritative answer to: "across this whole estate or run,
how far has each workbook and published datasource progressed?"

This script derives a per-unit progress view without inventing a mutable event ledger or guessing
unrecorded state. It cross-references the survey's declared scope (from `fetch_order`,
`required_datasources`, `workbooks`, and `unresolved_dependencies`) against observable repository
and run artifacts:

  1. Deliverables on disk: `migrations/{workbooks,datasources}/<slug>/`
     - Verified: `promotion-record.json` exists, `check_unit.exit_code == 0`, `forced == False`.
     - Forced: `promotion-record.json` exists with `forced == True`.
     - Unverified: `promotion-record.json` exists with non-zero exit code.
     - Cannot assess: directory exists on disk without a valid `promotion-record.json` (directory
       presence alone is never credited as a verified or shipped outcome).
  2. Handover packages: `packages/<batch>/<Unit>/package-manifest.json` (or `packages/<Unit>/`).
  3. Conversion artifacts: `bundle/pbip/<Unit>` or `bundle/handover/<Unit>.json`.
  4. Harvested source assets: `assets/<luid>_<Name>.twb(x)` or `assets/<Name>.tds(x)`.
  5. Unresolved dependencies: unmapped / ambiguous datasource dependencies recorded in the survey.
  6. Not started: in scope in the survey, but no downstream artifacts exist yet.

Outputs
-------
Writes both JSON (`unit-status.json`) and Markdown (`unit-status.md`) with strict 1:1 parity and
explicit citations for every status verdict.

Exit codes
----------
  0  Roll-up successfully generated
  2  Usage error (invalid arguments or missing required survey / run)
  3  Cannot assess (survey missing, unreadable, or malformed JSON)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    # pylint: disable-next=no-member
    if _stream is not None and getattr(_stream, "encoding", None) and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

LOG = logging.getLogger("build_run_unit_status")

REPO_ROOT = Path(__file__).resolve().parent.parent

KIND_WORKBOOK = "workbook"
KIND_DATASOURCE = "datasource"

STATUS_SHIPPED_VERIFIED = "shipped_verified"
STATUS_SHIPPED_FORCED = "shipped_forced"
STATUS_SHIPPED_UNVERIFIED = "shipped_unverified"
STATUS_PACKAGED = "packaged"
STATUS_PACKAGED_EDITED = "packaged_edited"
STATUS_CONVERTED = "converted"
STATUS_HARVESTED = "harvested"
STATUS_UNRESOLVED_DEPENDENCY = "unresolved_dependency"
STATUS_NOT_STARTED = "not_started"
STATUS_CANNOT_ASSESS = "cannot_assess"

STATUS_LABELS: dict[str, str] = {
    STATUS_SHIPPED_VERIFIED: "Shipped (Verified)",
    STATUS_SHIPPED_FORCED: "Shipped (Forced)",
    STATUS_SHIPPED_UNVERIFIED: "Shipped (Unverified)",
    STATUS_PACKAGED: "Packaged",
    STATUS_PACKAGED_EDITED: "Packaged (Edited)",
    STATUS_CONVERTED: "Converted",
    STATUS_HARVESTED: "Harvested",
    STATUS_UNRESOLVED_DEPENDENCY: "Unresolved Dependency",
    STATUS_NOT_STARTED: "Not Started",
    STATUS_CANNOT_ASSESS: "Cannot Assess",
}

STATUS_RANKS: tuple[str, ...] = (
    STATUS_SHIPPED_VERIFIED,
    STATUS_SHIPPED_FORCED,
    STATUS_SHIPPED_UNVERIFIED,
    STATUS_PACKAGED_EDITED,
    STATUS_PACKAGED,
    STATUS_CONVERTED,
    STATUS_HARVESTED,
    STATUS_UNRESOLVED_DEPENDENCY,
    STATUS_NOT_STARTED,
    STATUS_CANNOT_ASSESS,
)

CEILING_STATEMENT = (
    "Derive-only status roll-up from verifiable promotion-record.json, "
    "package-manifest.json, bundle, and harvest artifacts. Does not rely on a mutable event ledger."
)

_MAX_SLUG_LEN = 30


class CannotAssess(Exception):
    """Raised when an input cannot be assessed or verified."""


def sanitize_slug(name: str) -> str:
    """Turn an arbitrary name into a path-safe slug."""
    if not name:
        return "unit"
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    if not slug:
        return "unit"
    return slug[:_MAX_SLUG_LEN].strip("-")


# pylint: disable=too-many-instance-attributes
@dataclass
class UnitScope:
    """One unit declared in the survey scope."""

    order: int
    kind: str
    name: str
    luid: str | None = None
    project: str | None = None
    slug: str = ""
    unresolved_status: str | None = None
    unresolved_candidates: list[dict[str, Any]] = field(default_factory=list)
    unresolved_workbook: str | None = None

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = sanitize_slug(self.name)
        if self.kind not in (KIND_WORKBOOK, KIND_DATASOURCE):
            self.kind = KIND_DATASOURCE if "datasource" in self.kind.lower() else KIND_WORKBOOK


# pylint: disable=too-many-instance-attributes
@dataclass
class UnitStatusRecord:
    """The derived outcome and evidence for one unit."""

    order: int
    kind: str
    name: str
    project: str | None
    luid: str | None
    slug: str
    status: str
    status_label: str
    recorded_at: str | None
    deliverable_path: str | None
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to a serializable dictionary."""
        return asdict(self)


@dataclass
class StatusContext:
    """Artifact roots and execution environment context."""

    repo_root: Path
    run_root: Path | None = None
    packages_root: Path | None = None
    bundle_root: Path | None = None
    assets_root: Path | None = None
    migrations_root: Path = field(default_factory=lambda: REPO_ROOT / "migrations")


def _safe_json_load(path: Path) -> Any:
    """Read and parse JSON from a path, raising CannotAssess if unreadable."""
    if not path.is_file():
        raise CannotAssess(f"file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CannotAssess(f"could not read JSON from {path}: {exc}") from exc


def _match_existing_unit(
    unit_item: UnitScope,
    existing: UnitScope,
) -> bool:
    """Check if a new survey scope item matches an already-recorded scope unit."""
    if existing.kind != unit_item.kind:
        return False
    if existing.luid and unit_item.luid and existing.luid.lower() == unit_item.luid.lower():
        return True
    if existing.name.lower() == unit_item.name.lower():
        luid_ok = not existing.luid or not unit_item.luid or existing.luid.lower() == unit_item.luid.lower()
        proj_ok = not existing.project or not unit_item.project or existing.project.lower() == unit_item.project.lower()
        return luid_ok and proj_ok
    return False


def _append_survey_item(
    units: list[UnitScope],
    unit_item: UnitScope,
) -> None:
    """Append a survey unit scope item if not previously seen, or enrich existing unit."""
    clean_name = (unit_item.name or "").strip()
    if not clean_name:
        return

    for existing in units:
        if _match_existing_unit(unit_item, existing):
            if not existing.luid and unit_item.luid:
                existing.luid = unit_item.luid
            if not existing.project and unit_item.project:
                existing.project = unit_item.project
            return

    unit_item.order = len(units) + 1
    unit_item.name = clean_name
    unit_item.slug = sanitize_slug(clean_name)
    units.append(unit_item)


def _load_fetch_order(
    raw_fetch_order: Any,
    units: list[UnitScope],
) -> None:
    """Parse fetch_order list from survey."""
    if not isinstance(raw_fetch_order, list):
        return
    for item in raw_fetch_order:
        if isinstance(item, dict):
            kind = item.get("kind") or item.get("type")
            if not kind:
                kind = KIND_DATASOURCE if ("datasource_name" in item or "ds_name" in item) else KIND_WORKBOOK
            name = item.get("name") or item.get("datasource_name") or item.get("workbook_name") or ""
            luid = item.get("luid") or item.get("id")
            project = item.get("project") or item.get("projectName")
            scope_item = UnitScope(order=0, kind=kind, name=name, luid=luid, project=project)
            _append_survey_item(units, scope_item)
        elif isinstance(item, str):
            scope_item = UnitScope(order=0, kind=KIND_WORKBOOK, name=item)
            _append_survey_item(units, scope_item)


def _load_named_list(
    raw_items: Any,
    default_kind: str,
    units: list[UnitScope],
) -> None:
    """Parse list of datasources or workbooks from survey."""
    if not isinstance(raw_items, list):
        return
    name_keys = ("datasource_name", "workbook_name", "name")
    for item in raw_items:
        if isinstance(item, dict):
            name = ""
            for k in name_keys:
                if k in item and item[k]:
                    name = item[k]
                    break
            luid = item.get("luid") or item.get("id")
            project = item.get("project") or item.get("projectName")
            scope_item = UnitScope(order=0, kind=default_kind, name=name, luid=luid, project=project)
            _append_survey_item(units, scope_item)
        elif isinstance(item, str):
            scope_item = UnitScope(order=0, kind=default_kind, name=item)
            _append_survey_item(units, scope_item)


def _load_unresolved_dependencies(
    raw_unresolved: Any,
    units: list[UnitScope],
) -> None:
    """Parse unresolved_dependencies from survey."""
    if not isinstance(raw_unresolved, list):
        return
    for unres in raw_unresolved:
        if not isinstance(unres, dict):
            continue
        ds_name = (unres.get("datasource_name") or unres.get("name") or "").strip()
        status = unres.get("status") or "not_found"
        candidates = unres.get("candidates") or []
        wb_name = unres.get("workbook")
        matched = False
        for unit in units:
            if unit.kind == KIND_DATASOURCE and unit.name.lower() == ds_name.lower():
                if not unit.unresolved_status:
                    unit.unresolved_status = status
                    unit.unresolved_candidates = candidates
                    unit.unresolved_workbook = wb_name
                matched = True
                break
        if not matched and ds_name:
            scope_item = UnitScope(
                order=0,
                kind=KIND_DATASOURCE,
                name=ds_name,
                unresolved_status=status,
                unresolved_candidates=candidates,
                unresolved_workbook=wb_name,
            )
            _append_survey_item(units, scope_item)
            units[-1].unresolved_status = status
            units[-1].unresolved_candidates = candidates
            units[-1].unresolved_workbook = wb_name


def load_survey_scope(survey_path: Path) -> tuple[list[UnitScope], dict[str, Any]]:
    """Enumerate all units in scope from `estate_survey.json` preserving order and identity."""
    payload = _safe_json_load(survey_path)
    if not isinstance(payload, dict):
        raise CannotAssess(f"survey payload at {survey_path} is not a JSON object")

    units: list[UnitScope] = []

    _load_fetch_order(payload.get("fetch_order"), units)
    _load_named_list(payload.get("required_datasources"), KIND_DATASOURCE, units)
    _load_named_list(payload.get("workbooks"), KIND_WORKBOOK, units)
    _load_unresolved_dependencies(payload.get("unresolved_dependencies"), units)

    for idx, unit in enumerate(units, start=1):
        unit.order = idx

    return units, payload


def _find_package_dir(packages_root: Path, unit: UnitScope) -> Path | None:
    """Find the package directory for a given unit under packages_root."""
    if not packages_root.is_dir():
        return None

    candidates: list[Path] = [m.parent for m in packages_root.glob("**/package-manifest.json")]

    if unit.luid:
        for cand in candidates:
            prov_file = cand / "source-provenance.json"
            if prov_file.is_file():
                try:
                    prov = json.loads(prov_file.read_text(encoding="utf-8"))
                    if isinstance(prov, dict) and prov.get("workbook_luid") == unit.luid:
                        return cand
                except (OSError, json.JSONDecodeError):
                    pass

    for cand in candidates:
        cand_name = cand.name.lower()
        if cand_name in (unit.name.lower(), unit.slug.lower(), sanitize_slug(unit.name)):
            return cand
        manifest_path = cand / "package-manifest.json"
        try:
            m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(m_data, dict):
                m_unit = (m_data.get("unit") or "").strip().lower()
                if m_unit in (unit.name.lower(), unit.slug.lower()):
                    return cand
        except (OSError, json.JSONDecodeError):
            pass

    return None


def _find_bundle_unit(bundle_root: Path, unit: UnitScope) -> tuple[Path | None, str | None]:
    """Check if PBIP or handover slice exists for a given unit under bundle_root."""
    if not bundle_root.is_dir():
        return None, None

    pbip_dir = bundle_root / "pbip"
    if pbip_dir.is_dir():
        for child in pbip_dir.iterdir():
            if child.is_dir() and child.name.lower() in (
                unit.name.lower(),
                unit.slug.lower(),
                sanitize_slug(unit.name),
            ):
                return child, f"bundle/pbip/{child.name}"

    handover_dir = bundle_root / "handover"
    if handover_dir.is_dir():
        for child in handover_dir.glob("*.json"):
            if child.stem.lower() in (unit.name.lower(), unit.slug.lower(), sanitize_slug(unit.name)):
                return child, f"bundle/handover/{child.name}"

    return None, None


def _find_harvested_asset(assets_root: Path, unit: UnitScope) -> tuple[Path | None, str | None]:
    """Check if a downloaded asset exists for a given unit under assets_root."""
    if not assets_root.is_dir():
        return None, None

    if unit.luid:
        for asset in assets_root.iterdir():
            if asset.is_file() and asset.name.lower().startswith(unit.luid.lower()):
                return asset, f"assets/{asset.name}"

    unit_slug = unit.slug.lower()
    unit_name_norm = sanitize_slug(unit.name)
    for asset in assets_root.iterdir():
        if not asset.is_file():
            continue
        asset_lower = asset.name.lower()
        if unit_slug in asset_lower or unit_name_norm in asset_lower:
            return asset, f"assets/{asset.name}"

    return None, None


def _evaluate_promotion_record(
    record_data: Any,
    unit: UnitScope,
    rel_deliv: str,
) -> tuple[str, str, str | None, str]:
    """Evaluate promotion record dictionary into a status tuple."""
    if not isinstance(record_data, dict):
        return (
            STATUS_CANNOT_ASSESS,
            STATUS_LABELS[STATUS_CANNOT_ASSESS],
            None,
            f"promotion-record.json at {rel_deliv} is not a JSON object",
        )

    rec_kind = record_data.get("kind")
    if rec_kind and rec_kind.lower() != unit.kind.lower():
        return (
            STATUS_CANNOT_ASSESS,
            STATUS_LABELS[STATUS_CANNOT_ASSESS],
            None,
            f"promotion-record.json claims kind={rec_kind!r} which contradicts survey kind {unit.kind!r}",
        )

    promoted_at = record_data.get("promoted_at")
    forced = bool(record_data.get("forced"))
    check_unit_data = record_data.get("check_unit")
    exit_code = check_unit_data.get("exit_code") if isinstance(check_unit_data, dict) else None
    check_passed = check_unit_data.get("passed") if isinstance(check_unit_data, dict) else None
    gate_status = (
        check_unit_data.get("status")
        if isinstance(check_unit_data, dict) and check_unit_data.get("status")
        else (f"exit_{exit_code}" if exit_code is not None else "unrecorded")
    )

    if not forced and (check_passed is True or exit_code == 0):
        return (
            STATUS_SHIPPED_VERIFIED,
            STATUS_LABELS[STATUS_SHIPPED_VERIFIED],
            promoted_at,
            f"promotion-record.json (check_unit: {gate_status}, unforced)",
        )
    if forced:
        return (
            STATUS_SHIPPED_FORCED,
            STATUS_LABELS[STATUS_SHIPPED_FORCED],
            promoted_at,
            f"promotion-record.json (forced=true, check_unit: {gate_status})",
        )
    return (
        STATUS_SHIPPED_UNVERIFIED,
        STATUS_LABELS[STATUS_SHIPPED_UNVERIFIED],
        promoted_at,
        f"promotion-record.json (check_unit: {gate_status})",
    )


def _check_deliverable(
    migrations_root: Path,
    unit: UnitScope,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Inspect deliverable folder in migrations/{workbooks,datasources}/<slug>/.

    Returns: (status, status_label, recorded_at, deliverable_path, evidence) or (None, ...)
    """
    kind_subdir = "datasources" if unit.kind == KIND_DATASOURCE else "workbooks"
    deliv_dir = migrations_root / kind_subdir / unit.slug
    if not deliv_dir.exists():
        deliv_dir_alt = migrations_root / kind_subdir / unit.name
        if deliv_dir_alt.exists():
            deliv_dir = deliv_dir_alt
        else:
            return None, None, None, None, None

    rel_deliv = f"migrations/{kind_subdir}/{deliv_dir.name}"
    record_path = deliv_dir / "promotion-record.json"

    if not record_path.is_file():
        return (
            STATUS_CANNOT_ASSESS,
            STATUS_LABELS[STATUS_CANNOT_ASSESS],
            None,
            rel_deliv,
            f"directory exists at {rel_deliv} but promotion-record.json is missing "
            "(directory presence alone is unverified)",
        )

    try:
        record_data = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return (
            STATUS_CANNOT_ASSESS,
            STATUS_LABELS[STATUS_CANNOT_ASSESS],
            None,
            rel_deliv,
            f"promotion-record.json at {rel_deliv} is unreadable or malformed JSON: {exc}",
        )

    status, label, promoted_at, evidence = _evaluate_promotion_record(record_data, unit, rel_deliv)
    return status, label, promoted_at, rel_deliv, evidence


def _check_package(
    unit: UnitScope,
    ctx: StatusContext,
) -> UnitStatusRecord | None:
    """Check package directory under packages_root."""
    if ctx.packages_root is None or not ctx.packages_root.exists():
        return None

    package_dir = _find_package_dir(ctx.packages_root, unit)
    if package_dir is None:
        return None

    manifest_file = package_dir / "package-manifest.json"
    rel_pkg = (
        package_dir.relative_to(ctx.run_root).as_posix()
        if ctx.run_root and package_dir.is_relative_to(ctx.run_root)
        else package_dir.relative_to(ctx.repo_root).as_posix()
        if package_dir.is_relative_to(ctx.repo_root)
        else package_dir.as_posix()
    )

    if not manifest_file.is_file():
        err_msg = f"package directory found at {rel_pkg} but package-manifest.json is missing"
        return UnitStatusRecord(
            order=unit.order,
            kind=unit.kind,
            name=unit.name,
            project=unit.project,
            luid=unit.luid,
            slug=unit.slug,
            status=STATUS_CANNOT_ASSESS,
            status_label=STATUS_LABELS[STATUS_CANNOT_ASSESS],
            recorded_at=None,
            deliverable_path="not-started",
            evidence=err_msg,
        )

    try:
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        err_msg = f"package-manifest.json at {rel_pkg} is unreadable or malformed JSON: {exc}"
        return UnitStatusRecord(
            order=unit.order,
            kind=unit.kind,
            name=unit.name,
            project=unit.project,
            luid=unit.luid,
            slug=unit.slug,
            status=STATUS_CANNOT_ASSESS,
            status_label=STATUS_LABELS[STATUS_CANNOT_ASSESS],
            recorded_at=None,
            deliverable_path="not-started",
            evidence=err_msg,
        )

    if isinstance(manifest_data, dict):
        m_kind = manifest_data.get("kind")
        if m_kind and m_kind.lower() != unit.kind.lower():
            err_msg = f"package-manifest.json claims kind={m_kind!r} which contradicts survey kind {unit.kind!r}"
            pkg_status = STATUS_CANNOT_ASSESS
            pkg_evidence = err_msg
            pkg_time = None
        else:
            pkg_status = STATUS_PACKAGED
            pkg_evidence = f"package assembled at {rel_pkg}"
            pkg_time = manifest_data.get("packaged_at") or manifest_data.get("created_at")

        return UnitStatusRecord(
            order=unit.order,
            kind=unit.kind,
            name=unit.name,
            project=unit.project,
            luid=unit.luid,
            slug=unit.slug,
            status=pkg_status,
            status_label=STATUS_LABELS[pkg_status],
            recorded_at=pkg_time,
            deliverable_path="not-started",
            evidence=pkg_evidence,
        )

    return None


def derive_unit_status(
    unit: UnitScope,
    ctx: StatusContext,
) -> UnitStatusRecord:
    """Derive the status of one unit from observable repository and run artifacts."""
    # 1. Check Deliverables
    status, status_label, recorded_at, deliverable_path, evidence = _check_deliverable(
        ctx.migrations_root, unit
    )
    if status is not None:
        return UnitStatusRecord(
            order=unit.order,
            kind=unit.kind,
            name=unit.name,
            project=unit.project,
            luid=unit.luid,
            slug=unit.slug,
            status=status,
            status_label=status_label or STATUS_LABELS.get(status, status),
            recorded_at=recorded_at,
            deliverable_path=deliverable_path,
            evidence=evidence or "",
        )

    # 2. Check Packages
    pkg_record = _check_package(unit, ctx)
    if pkg_record is not None:
        return pkg_record

    # 3. Check Conversion / Bundle
    if ctx.bundle_root is not None and ctx.bundle_root.exists():
        _, bundle_evidence = _find_bundle_unit(ctx.bundle_root, unit)
        if bundle_evidence is not None:
            return UnitStatusRecord(
                order=unit.order,
                kind=unit.kind,
                name=unit.name,
                project=unit.project,
                luid=unit.luid,
                slug=unit.slug,
                status=STATUS_CONVERTED,
                status_label=STATUS_LABELS[STATUS_CONVERTED],
                recorded_at=None,
                deliverable_path="not-started",
                evidence=f"converted artifact in {bundle_evidence}",
            )

    # 4. Check Harvested Assets
    if ctx.assets_root is not None and ctx.assets_root.exists():
        _, asset_evidence = _find_harvested_asset(ctx.assets_root, unit)
        if asset_evidence is not None:
            return UnitStatusRecord(
                order=unit.order,
                kind=unit.kind,
                name=unit.name,
                project=unit.project,
                luid=unit.luid,
                slug=unit.slug,
                status=STATUS_HARVESTED,
                status_label=STATUS_LABELS[STATUS_HARVESTED],
                recorded_at=None,
                deliverable_path="not-started",
                evidence=f"harvested source asset in {asset_evidence}",
            )

    # 5. Check Unresolved Dependency or Not Started
    if unit.unresolved_status:
        wb_info = f" for workbook {unit.unresolved_workbook!r}" if unit.unresolved_workbook else ""
        u_status = STATUS_UNRESOLVED_DEPENDENCY
        u_evidence = f"unresolved dependency in estate_survey.json (status: {unit.unresolved_status}{wb_info})"
    else:
        u_status = STATUS_NOT_STARTED
        u_evidence = f"in scope in estate_survey.json (order #{unit.order}); no run or repository artifacts found"

    return UnitStatusRecord(
        order=unit.order,
        kind=unit.kind,
        name=unit.name,
        project=unit.project,
        luid=unit.luid,
        slug=unit.slug,
        status=u_status,
        status_label=STATUS_LABELS[u_status],
        recorded_at=None,
        deliverable_path="not-started",
        evidence=u_evidence,
    )


def _disambiguate_duplicate_names(
    records: list[UnitStatusRecord],
    units: list[UnitScope],
) -> list[UnitStatusRecord]:
    """Flag ambiguous cases where multiple units share a name with insufficient disambiguation."""
    name_counts: dict[tuple[str, str], list[int]] = {}
    for idx, unit in enumerate(units):
        key = (unit.kind, unit.name.lower())
        name_counts.setdefault(key, []).append(idx)

    for indices in name_counts.values():
        if len(indices) <= 1:
            continue
        delivered = [
            i
            for i in indices
            if records[i].status in (STATUS_SHIPPED_VERIFIED, STATUS_SHIPPED_FORCED, STATUS_SHIPPED_UNVERIFIED)
        ]
        unique_paths = {records[i].deliverable_path for i in delivered if records[i].deliverable_path}
        is_ambiguous = (0 < len(delivered) < len(indices)) or (len(unique_paths) < len(delivered))
        if is_ambiguous:
            for i in indices:
                rec = records[i]
                if rec.status not in (STATUS_SHIPPED_VERIFIED, STATUS_SHIPPED_FORCED, STATUS_SHIPPED_UNVERIFIED):
                    continue
                records[i] = UnitStatusRecord(
                    order=rec.order,
                    kind=rec.kind,
                    name=rec.name,
                    project=rec.project,
                    luid=rec.luid,
                    slug=rec.slug,
                    status=STATUS_CANNOT_ASSESS,
                    status_label=STATUS_LABELS[STATUS_CANNOT_ASSESS],
                    recorded_at=rec.recorded_at,
                    deliverable_path=rec.deliverable_path,
                    evidence=(
                        "ambiguous match: deliverable matches multiple scope units "
                        f"sharing display name {rec.name!r}"
                    ),
                )

    return records


def build_status_rollup(
    survey_path: Path,
    ctx: StatusContext,
) -> dict[str, Any]:
    """Assemble the full status roll-up payload."""
    units, _ = load_survey_scope(survey_path)

    raw_records = [derive_unit_status(unit=unit, ctx=ctx) for unit in units]
    records = _disambiguate_duplicate_names(raw_records, units)

    by_status: dict[str, int] = {st: 0 for st in STATUS_RANKS}
    by_kind: dict[str, int] = {KIND_DATASOURCE: 0, KIND_WORKBOOK: 0}

    for rec in records:
        by_status[rec.status] = by_status.get(rec.status, 0) + 1
        by_kind[rec.kind] = by_kind.get(rec.kind, 0) + 1

    run_display = ctx.run_root.name if ctx.run_root else "standalone"
    survey_rel = (
        survey_path.relative_to(ctx.repo_root).as_posix()
        if survey_path.is_relative_to(ctx.repo_root)
        else survey_path.as_posix()
    )

    summary = {
        "total_units": len(records),
        "datasources": by_kind[KIND_DATASOURCE],
        "workbooks": by_kind[KIND_WORKBOOK],
        "by_status": by_status,
    }

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run": run_display,
        "survey_path": survey_rel,
        "ceiling": CEILING_STATEMENT,
        "summary": summary,
        "units": [rec.to_dict() for rec in records],
    }


def _format_unit_table_row(unit_dict: dict[str, Any]) -> str:
    """Format one unit row for Markdown table output."""
    order = unit_dict.get("order", "")
    kind = unit_dict.get("kind", "")
    name = unit_dict.get("name", "").replace("|", "\\|")
    project = (unit_dict.get("project") or "—").replace("|", "\\|")
    status_label = unit_dict.get("status_label") or STATUS_LABELS.get(
        unit_dict.get("status", ""), unit_dict.get("status", "")
    )
    recorded = f"`{unit_dict.get('recorded_at')}`" if unit_dict.get("recorded_at") else "—"
    deliv = (
        f"`{unit_dict.get('deliverable_path')}`"
        if unit_dict.get("deliverable_path") and unit_dict.get("deliverable_path") != "not-started"
        else "not-started"
    )
    evidence = (unit_dict.get("evidence") or "").replace("|", "\\|")
    return f"| {order} | {kind} | {name} | {project} | {status_label} | {recorded} | {deliv} | {evidence} |"


def render_markdown_report(rollup: dict[str, Any]) -> str:
    """Format the roll-up payload as human-readable Markdown."""
    run_name = rollup.get("run", "unknown")
    generated_at = rollup.get("generated_at", "")
    survey_path = rollup.get("survey_path", "")
    summary = rollup.get("summary", {})
    ceiling = rollup.get("ceiling", CEILING_STATEMENT)

    lines: list[str] = [
        f"# Run Unit Status: {run_name}",
        "",
        f"- **Generated at:** `{generated_at}`",
        f"- **Survey source:** `{survey_path}`",
        f"- **Total units:** {summary.get('total_units', 0)} "
        f"({summary.get('datasources', 0)} datasources, {summary.get('workbooks', 0)} workbooks)",
        "",
        f"> **Ceiling:** {ceiling}",
        "",
        "## Summary",
        "",
        "| Status | Total |",
        "|---|---|",
    ]

    by_status: dict[str, int] = summary.get("by_status", {})
    for st in STATUS_RANKS:
        lines.append(f"| {STATUS_LABELS.get(st, st)} | {by_status.get(st, 0)} |")

    lines.extend(
        [
            "",
            "## Units",
            "",
            "| # | Kind | Name | Project | Status | Recorded At | Deliverable | Evidence |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )

    for u in rollup.get("units", []):
        lines.append(_format_unit_table_row(u))

    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Roll up per-unit migration status across an entire run from observable artifacts."
    )
    parser.add_argument("--run", type=Path, help="path to _runs/<NNN>-<slug> run directory")
    parser.add_argument("--survey", type=Path, help="path to estate_survey.json")
    parser.add_argument("--bundle", type=Path, help="path to bundle/ directory")
    parser.add_argument("--packages", type=Path, help="path to packages/ directory")
    parser.add_argument("--assets", type=Path, help="path to assets/ directory")
    parser.add_argument("--migrations-root", type=Path, help="path to migrations/ deliverables root")
    parser.add_argument("--out-md", type=Path, help="path to write unit-status.md")
    parser.add_argument("--out-json", type=Path, help="path to write unit-status.json")
    parser.add_argument("--json", action="store_true", help="print JSON payload to stdout")
    parser.add_argument("--quiet", action="store_true", help="suppress info output")

    args = parser.parse_args(argv)
    if not args.run and not args.survey:
        parser.error("at least one of --run or --survey is required")
    return args


def _resolve_output_paths(args: argparse.Namespace, run_dir: Path | None) -> tuple[Path | None, Path | None]:
    """Resolve Markdown and JSON output destination paths."""
    out_md = (
        (args.out_md if args.out_md.is_absolute() else (REPO_ROOT / args.out_md).resolve())
        if args.out_md
        else (run_dir / "unit-status.md" if run_dir else None)
    )
    out_json = (
        (args.out_json if args.out_json.is_absolute() else (REPO_ROOT / args.out_json).resolve())
        if args.out_json
        else (run_dir / "unit-status.json" if run_dir else None)
    )
    return out_md, out_json


def _resolve_context(args: argparse.Namespace, run_dir: Path | None) -> StatusContext:
    """Build StatusContext from CLI arguments."""
    packages_dir = (
        (args.packages if args.packages.is_absolute() else (REPO_ROOT / args.packages).resolve())
        if args.packages
        else (run_dir / "packages" if run_dir and (run_dir / "packages").is_dir() else None)
    )
    bundle_dir = (
        (args.bundle if args.bundle.is_absolute() else (REPO_ROOT / args.bundle).resolve())
        if args.bundle
        else (run_dir / "bundle" if run_dir and (run_dir / "bundle").is_dir() else None)
    )
    assets_dir = (
        (args.assets if args.assets.is_absolute() else (REPO_ROOT / args.assets).resolve())
        if args.assets
        else (run_dir / "assets" if run_dir and (run_dir / "assets").is_dir() else None)
    )
    migrations_root = (
        (args.migrations_root if args.migrations_root.is_absolute() else (REPO_ROOT / args.migrations_root).resolve())
        if args.migrations_root
        else (REPO_ROOT / "migrations")
    )
    return StatusContext(
        repo_root=REPO_ROOT,
        run_root=run_dir,
        packages_root=packages_dir,
        bundle_root=bundle_dir,
        assets_root=assets_dir,
        migrations_root=migrations_root,
    )


def _write_outputs(
    rollup: dict[str, Any],
    out_md: Path | None,
    out_json: Path | None,
    print_json: bool,
    quiet: bool,
) -> None:
    """Write outputs to disk and/or stdout."""
    md_text = render_markdown_report(rollup)
    json_text = json.dumps(rollup, indent=2) + "\n"

    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md_text, encoding="utf-8")
        if not quiet:
            LOG.info("Wrote %s", out_md)

    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json_text, encoding="utf-8")
        if not quiet:
            LOG.info("Wrote %s", out_json)

    if print_json:
        print(json_text, end="")
    elif not quiet and not out_md and not out_json:
        print(md_text)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)

    run_dir: Path | None = None
    if args.run:
        run_dir = args.run if args.run.is_absolute() else (REPO_ROOT / args.run).resolve()
        if not run_dir.is_dir():
            LOG.error("run directory not found: %s", run_dir)
            return 2

    out_md, out_json = _resolve_output_paths(args, run_dir)
    ctx = _resolve_context(args, run_dir)

    survey_file: Path | None = (
        (args.survey if args.survey.is_absolute() else (REPO_ROOT / args.survey).resolve())
        if args.survey
        else (ctx.run_root / "assessment" / "estate_survey.json" if ctx.run_root else None)
    )

    if not survey_file or not survey_file.is_file():
        LOG.error("survey file not found: %s", survey_file)
        return 3

    try:
        rollup = build_status_rollup(survey_path=survey_file, ctx=ctx)
    except CannotAssess as exc:
        LOG.error("cannot assess run status: %s", exc)
        return 3

    _write_outputs(rollup, out_md, out_json, print_json=args.json, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
