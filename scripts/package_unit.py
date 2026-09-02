"""
purpose: assemble ONE self-contained, no-flags handover package per migration unit (report or datasource).
usage:   python scripts/package_unit.py --bundle <bundle> --out <dir> [--unit NAME ...]
                                        [--oracle <dir>] [--assets <dir>] [--json <file>] [--quiet]

Issue #446: the three things an agent needs to start one report all exist and NOTHING assembles them.
They live in four naming schemes across two trees - the engine keys `pbip/`, `reports/` and
`handover/` by sanitized workbook name, the oracle keys renders and numbers by bare view LUID in a
flat directory outside the bundle, and the source asset is a LUID-prefixed filename in a third place.
So `check_reference_readiness.py` and `check_unit.py` both need `--source`/`--oracle` arguments that
cannot be derived from the unit path, and getting one wrong reads as "this unit is broken" rather
than "you did not tell me where the workbook is".

This script emits a folder both gates accept with NO flags:

    <out>/<Unit>/
        migration-spec.json          <- parse_tableau.py; check_unit.py's expected page set (#443)
        report.json                  <- the engine's own classification, SCOPED to this unit
        source-provenance.json       <- SCOPED; the only trusted route to a workbook LUID
        engine-output-receipt.json   <- what built this (version drift stays checkable)
        assets/<luid>_<Name>.twb(x)  <- the source, under the name resolve_source() already looks for
        fabric/<Name>.Report/        <- the engine WORKING COPY (`pbip/`), never the `reports/` baseline
        fabric/<Model>.SemanticModel/
        handover/<Unit>.json         <- the engine's per-workbook slice, verbatim
        handover.md                  <- flat, one-finding-per-line, emptied visuals FIRST
        oracle/
            oracle-manifest.json     <- THIS unit's views only, paths rewritten
            dashboards/{images,data}/<Object>.<ext>
            worksheets/{images,data}/<Object>.<ext>
            unknown/{images,data}/<Object>.<ext>   <- carried but MARKED, never filed as either kind
        package-manifest.json        <- what was packaged, and every omission with its reason
        README.md

Why `assets/` and not the `source/` the issue sketched: `check_reference_readiness.resolve_source`
already tries `<root>/assets/<basename>` (:544-545), and the handover slice's `workbook.source_id`
already carries that basename. Reusing the existing convention means this packaging needs ZERO
changes to either gate - the whole feature is arrangement, which is the only kind of fix that cannot
regress a verdict.

Attribution is FAIL-CLOSED, and by IDENTITY only - the one design rule
----------------------------------------------------------------------
A render this script cannot tie to a specific workbook **by LUID** is OMITTED and the reason recorded
in `package-manifest.json`; it is never copied in "because it was in the same capture" (issue #438 in
a new place), and never adopted because a display NAME happened to match.

There is exactly ONE admissible route: `oracle-manifest.json`'s `workbook_luid`, matched against the
LUID `source-provenance.json` records for the sha256 of the copied asset, cross-checked against the
asset filename's LUID prefix. A disagreement fails closed.

⚠️ **A display name is not an identity, and a name route was DELETED rather than guarded.** Two
projects can hold workbooks with the same name - the exact ambiguity `_runs/<NNN>-<slug>/` numbering
exists to avoid elsewhere in this repo. Issue #450 measured the consequence in a sibling gate:
`check_unit`'s workbook-attribution guard reads a field the capture does not write, is inert on
**360 of 360** real records, and therefore admits a foreign workbook's render as this unit's
evidence. This packager will not inherit that class. Measured cost of the deletion on the reference
estate: **zero** - the name route fired 0 times in 67 units.

Copying a render is NOT a claim that it is byte-faithful. `stamp_tableau_provenance.py` records
`origin.match: "name_only"` when the local and server bytes DIFFER, and the readiness gate refuses to
trust a LUID in that case (`check_reference_readiness._provenance_luid`). Such a unit still gets its
renders - an agent can look at them - and the gate still reports its pages BLIND. `handover.md`
carries `ORACLE_ATTRIBUTION ... match=name_only` so the difference is visible rather than inferred.

`view_type` is the ONLY type discriminator. `content_url` is `<wb>/sheets/<view>` for dashboards AND
worksheets, and `capture_tableau_oracle.py`'s type resolver is non-fatal by design, so `unknown` is a
legitimate value. An untyped render is filed under `unknown/` and named on its own `UNTYPED_RENDER`
line - never defaulted into either kind, because `reference_evidence._oracle_view_kind` treats absent
and `unknown` alike as "cannot satisfy any page".

Review contract
---------------
**Invariant.** Packaging RELOCATES and SCOPES; it never changes a page verdict. For every unit, the
entry gate run on the package must yield the same per-page readiness as the bundle-level run with
`--oracle`. Measured across the 67-unit reference estate: `pages_expected 220 / pages_ready 42 /
pages_blind 178`, identical both ways.

**Direction.** *Fail-open* (blocks merge): a render attributed to the wrong unit, crediting coverage
that does not exist. *Fail-closed* (residual, becomes an issue): a render that could have been
attributed is omitted, so a page reads BLIND - costs work, credits nothing false.

**Closed surface, N = 17 joins/transformations that can move the invariant**, plus 4 named residuals:

| # | join or transformation | key | how it is closed |
|---|---|---|---|
| 1 | `pbip/<Unit>/` -> `fabric/` | folder name | copied whole |
| 2 | report <-> model pairing | containment, NOT name | folder copied whole (a) |
| 3 | unit -> handover slice | file stem | exact |
| 4 | unit -> asset (handover) | `workbook.source_id` basename | run-root-relative, so basename only |
| 5 | unit -> asset (input manifest) | `Path(name).stem == unit` | exact, fallback only |
| 6 | asset -> workbook LUID | `input.sha256` | content-keyed; >1 LUID refuses |
| 7 | asset filename LUID | `<uuid>_` prefix | cross-check only, never a source (b) |
| 8 | workbook LUID -> oracle views | `workbook_luid` | the only route (#450) |
| 9 | view -> object kind | `view_type` | `unknown/`, marked, never defaulted |
| 10 | view -> filename | sanitized `view_name` | LUID-suffix disambiguation |
| 11 | leg -> bytes | recorded sha256/bytes/dims | verbatim copy, only `path` rewritten |
| 12 | leg claiming ok, file absent | — | status -> `omitted_by_packager` |
| 13 | unit -> engine classification | `report.json` name | exact; LISTS not sets, so duplicates show |
| 14 | unit universe | `report.json` U `pbip/` | neither side is a superset (c) |
| 15 | receipt artifacts | `pbip/<unit>/` prefix | re-rooted to `fabric/` |
| 16 | emptied visual -> page | visual id -> PBIR dir | directory lookup |
| 17 | package location -> gate discovery | `_default_dirs` scans the GRANDPARENT | shadowing refused, exit 2 |

(a) `byPath ../<Model>.SemanticModel` survives the copy, and 27 of 62 model names differ from their
unit's, so the pair is never re-established by name. (b) on a `.tds` the prefix is a DATASOURCE LUID,
a different identity namespace. (c) 4 workbooks ship no working copy, and 2 working copies are
unlisted because the engine disambiguated two same-named workbooks on disk.

**Residuals, named not guarded.** (R1) `<bundle>/reports/` is the engine BASELINE and is deliberately
never packaged - no model sits beside it, so a copy would not resolve `byPath`. (R2) issue #450 lives
in `check_unit._declared_workbook`, not here: the packaged manifest preserves `workbook_name`
verbatim and deliberately does **not** add the `workbook` key that would make that guard live, since
doing so would change a gate's verdict as a side effect of packaging - which is the invariant this
script exists to hold. (R3) `parse_tableau.py` can refuse a valid workbook (measured:
`World_Indicators`, a `quantiles` reference line outside its schema enum), so a unit may ship without
`migration-spec.json`; recorded as a `PACKAGE_NOTE`, never swallowed. (R4) an oracle capture is
default-view-state with no `?vf_` pinning, so `oracle/` is **layout/text grade only** regardless of
render leg.

Exit codes
----------
| 0 | every requested unit was packaged, engine output included |
| 1 | at least one unit has NO engine working copy under `pbip/`. It is still packaged - the source,
      the reference and the engine's own handover slice are all there - but there is nothing to build
      on, and `check_reference_readiness.py` reports it as a finding rather than a pass. |
| 2 | usage error (argparse) |

An omission INSIDE a package is not exit 1: a unit whose oracle genuinely has no render for a page is
the negative control, and it must package successfully and still report that page BLIND.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import read_handover  # noqa: E402  # pylint: disable=wrong-import-position
from object_identity import (  # noqa: E402  # pylint: disable=wrong-import-position
    KIND_DASHBOARD,
    KIND_UNKNOWN,
    KIND_WORKSHEET,
)

SCRIPT_DIR = Path(__file__).resolve().parent

#: The render legs an oracle view may claim, in the order `reference_evidence._oracle_leg` reads them.
RENDER_LEGS = ("image", "svg", "pdf")
#: Marks a leg the packager refused to copy. Anything other than "ok" makes the gate skip it.
OMITTED_STATUS = "omitted_by_packager"
KIND_DIRS = (KIND_DASHBOARD, KIND_WORKSHEET, KIND_UNKNOWN)
_LUID_PREFIX = re.compile(r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_")
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")
#: Well short of any filesystem limit: `check_path_ceiling.py` gates the WHOLE path, and these names
#: sit under `<out>/<Unit>/oracle/<kind>/images/`, which is already five segments deep.
_MAX_OBJECT_NAME = 60

KIND_WORKBOOK = "workbook"
KIND_DATASOURCE = "datasource"
KIND_UNCLASSIFIED = "unclassified"

#: `report.json` fields the packaged copy carries VERBATIM - engine identity, no estate content.
#: Measured on the 48-workbook reference bundle: `tool` is `"migrate_estate"` (16 bytes) and
#: `generated_at` an ISO timestamp (22 bytes). Widening this tuple is how the round-1 leak would
#: come back, so `test_package_unit.py` pins it against the real engine field set.
REPORT_VERBATIM_FIELDS = ("tool", "generated_at")
#: `report.json` fields filtered to this unit's entries. These two ARE the gate surface: both
#: `check_reference_readiness._engine_report` and `check_unit._is_engine_report` reject the file
#: unless `workbooks` is a list, and `._unit_names` reads the names of both.
REPORT_UNIT_LISTS = ("workbooks", "datasources")


# --------------------------------------------------------------------------------------------
# reading the bundle
# --------------------------------------------------------------------------------------------


def read_json(path: Path) -> Any:
    """Parse a JSON file, or return None when it is absent or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def write_json(path: Path, payload: Any) -> None:
    """Write pretty JSON, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256_of(path: Path | None) -> str | None:
    """sha256 of a file, or None when it is absent or cannot be read."""
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def discover_dir(bundle: Path, names: tuple[str, ...]) -> Path | None:
    """First existing directory named in ``names``, looked for beside and inside the bundle."""
    for base in (bundle, bundle.parent):
        for name in names:
            candidate = base / name
            if candidate.is_dir():
                return candidate.resolve()
    return None


def engine_unit_names(engine_report: Any) -> tuple[list[str], list[str]]:
    """`(workbook names, datasource names)` EXACTLY as the engine wrote them, as lists.

    Lists rather than sets because multiplicity is itself a finding: two genuinely distinct workbooks
    whose names differ only by whitespace must not collapse into one key
    (`check_reference_readiness._unit_names`).
    """
    report = engine_report if isinstance(engine_report, dict) else {}
    return (
        [str(x["name"]) for x in report.get("workbooks") or [] if isinstance(x, dict) and x.get("name")],
        [str(x["name"]) for x in report.get("datasources") or [] if isinstance(x, dict) and x.get("name")],
    )


def unit_kind(unit: str, workbooks: list[str], datasources: list[str]) -> str:
    """How the ENGINE classifies this unit. Never inferred from the filesystem.

    Every `pbip/<Unit>/` folder in a real 2.339.0 estate run carries BOTH a `.Report` and a
    `.SemanticModel` - measured on all 62, datasource-only units included - so the filesystem cannot
    answer this question and only `report.json` can. `check_reference_readiness._datasource_only`
    makes the same call for the same reason.
    """
    if unit in workbooks:
        return KIND_WORKBOOK
    if unit in datasources:
        return KIND_DATASOURCE
    return KIND_UNCLASSIFIED


def bundle_units(bundle: Path) -> list[str]:
    """Every unit this bundle is ACCOUNTABLE for - the engine's own lists PLUS its working copies.

    Deliberately NOT just `pbip/`. Measured on a real 2.339.0 estate run: `report.json` lists 48
    workbooks but only 44 have a `pbip/<Unit>/` working copy, and `check_reference_readiness` reports
    each of the other four as a FINDING - "the engine lists this workbook but no report ships for it".
    Deriving the unit list from the filesystem alone dropped all four silently, which is the same
    class of defect this packaging exists to remove. They still package usefully: all four have a
    handover slice, a source asset and oracle renders; what they lack is the engine output, and
    `packaged: false` says so.

    Conversely `pbip/` holds units `report.json` does not name at all (the engine disambiguated two
    workbooks that share a name onto `Seed_-_R_D_2` / `_3`), so neither source is a superset.
    """
    engine_report = read_json(bundle / "report.json")
    workbooks, datasources = engine_unit_names(engine_report)
    pbip = bundle / "pbip"
    folders = [path.name for path in pbip.iterdir() if path.is_dir()] if pbip.is_dir() else []
    return sorted(set(folders) | set(workbooks) | set(datasources))


# --------------------------------------------------------------------------------------------
# the source asset, and the workbook identity that attributes renders to it
# --------------------------------------------------------------------------------------------


def resolve_asset(bundle: Path, unit: str, handover: Any, assets_dir: Path | None) -> tuple[Path | None, str]:
    """`(asset path, how it was resolved)` for the Tableau source behind ``unit``.

    Order mirrors `check_reference_readiness.resolve_source`: the handover slice's
    `workbook.source_id` (a run-root-relative path, so only its basename is portable), then
    `input_manifest.json`'s staged asset whose stem matches the unit name.
    """
    workbook = handover.get("workbook") if isinstance(handover, dict) else None
    source_id = workbook.get("source_id") if isinstance(workbook, dict) else None
    if isinstance(source_id, str) and source_id.strip():
        name = Path(source_id).name
        for base in (assets_dir, bundle / "assets", bundle.parent / "assets"):
            if base is not None and (base / name).is_file():
                return (base / name), "handover.workbook.source_id"

    manifest = read_json(bundle / "input_manifest.json")
    staged_assets = manifest.get("assets") or [] if isinstance(manifest, dict) else []
    for asset in staged_assets:
        if not isinstance(asset, dict) or Path(str(asset.get("name") or "")).stem != unit:
            continue
        staged = asset.get("staged_input_path")
        candidates = [Path(str(staged))] if staged else []
        candidates += [
            base / str(asset.get("name"))
            for base in (assets_dir, bundle / "assets", bundle.parent / "assets")
            if base is not None
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate, "input_manifest.staged_input_path"
    return None, "unresolved"


def scope_provenance(provenance: Any, asset_sha: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(unit-scoped source-provenance.json, matching entries)` for one asset's sha256.

    Scoped by CONTENT, not by filename: `source-provenance.json` is keyed by `input.sha256` and
    `check_reference_readiness._provenance_luid` looks the unit up the same way, so a scoped copy that
    kept the wrong entry would hand the gate a LUID for a different workbook.
    """
    payload = provenance if isinstance(provenance, dict) else {}
    entries = [
        entry
        for entry in payload.get("inputs") or []
        if isinstance(entry, dict)
        and isinstance(entry.get("input"), dict)
        and asset_sha is not None
        and entry["input"].get("sha256") == asset_sha
    ]
    scoped = {key: value for key, value in payload.items() if key != "inputs"}
    scoped["inputs"] = entries
    scoped["input_count"] = len(entries)
    scoped["scoped_by"] = "package_unit.py: inputs filtered to this unit's asset sha256"
    return scoped, entries


def filename_luid(asset: Path | None) -> str | None:
    """The LUID `harvest_estate_assets.py` prefixes onto a downloaded asset filename.

    ⚠️ **This is NOT usable as a workbook identity on its own, and is never used as one here.** The
    harvester prefixes a `.tds`/`.tdsx` with its **datasource** LUID, which lives in a different
    identity namespace from `oracle-manifest.json`'s `workbook_luid`. Measured on the 67-unit
    reference estate: **all 19** units that carry a filename LUID with no provenance entry are
    datasources. Promoting it would feed a datasource LUID into a workbook-LUID comparison - a
    category error that buys nothing (those 19 have no views) and fails OPEN if the namespaces ever
    collide.

    It is therefore only a CROSS-CHECK against a provenance LUID, and that comparison is structurally
    scoped to workbooks already: `stamp_tableau_provenance.py` stamps workbooks only, so a datasource
    never reaches it.
    """
    if asset is None:
        return None
    found = _LUID_PREFIX.match(asset.name)
    return found.group(1) if found else None


def workbook_identity(entries: list[dict[str, Any]], asset: Path | None) -> dict[str, Any]:
    """The workbook LUID this unit's renders may be attributed to, or a refusal naming why.

    Returns `{"luid", "match", "workbook_name", "reason"}`. `luid` is None whenever the identity is
    not established, and `reason` then says which precondition failed - which is the whole verdict,
    because a unit with no workbook LUID attributes nothing at all (see :func:`select_views`).

    One source, one cross-check: `source-provenance.json` keyed by the asset's **sha256**, checked
    against the asset filename's LUID prefix when there is one. A disagreement fails closed rather
    than picking whichever was read first.
    """
    stamped = filename_luid(asset)
    luids = {
        str(entry["origin"]["workbook_luid"])
        for entry in entries
        if isinstance(entry.get("origin"), dict) and entry["origin"].get("workbook_luid")
    }
    if len(luids) > 1:
        return _no_identity(f"source-provenance.json maps this asset's bytes onto {len(luids)} workbook LUIDs")
    if not luids:
        return _no_identity("no source-provenance.json entry for this asset's bytes")

    luid = next(iter(luids))
    if stamped and stamped.casefold() != luid.casefold():
        return _no_identity(
            f"asset filename declares LUID {stamped} but source-provenance.json records {luid} "
            "for these bytes - two identities that disagree are LESS evidence than none"
        )
    origin = next((entry["origin"] for entry in entries if isinstance(entry.get("origin"), dict)), {})
    return {
        "luid": luid,
        "match": origin.get("match"),
        "workbook_name": origin.get("workbook_name"),
        "reason": None,
    }


def _no_identity(reason: str) -> dict[str, Any]:
    """No usable workbook identity, carrying the precondition that failed."""
    return {"luid": None, "match": None, "workbook_name": None, "reason": reason}


# --------------------------------------------------------------------------------------------
# the oracle subset
# --------------------------------------------------------------------------------------------


def select_views(manifest: Any, identity: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """`(this unit's views, route)` from a flat oracle manifest - or `([], reason)`.

    **ONE route: `workbook_luid`.** A display name is not an identity - two projects can hold
    workbooks with the same name, which is the exact ambiguity `_runs/<NNN>-<slug>/` numbering exists
    to avoid elsewhere in this repo (issue #450).

    ⚠️ There WAS a second route here - an exact `workbook_name == <unit>` match guarded by a
    single-owner check - added because `reference_evidence.Evidence.is_for` has the same fallback.
    It is deleted rather than further guarded. Two measurements decided it:

    * it fired **0 times in 67 units** on the reference estate (46 resolve by `workbook_luid`, 21
      attribute nothing), so it was untested-in-production surface with no measured benefit; and
    * a name route is the same class as #450, where `check_unit`'s workbook guard reads a field the
      capture does not write and is inert on **360 of 360** real records - failing OPEN, admitting a
      foreign workbook's render as this unit's evidence.

    Mirroring a fallback that a sibling gate is being fixed to distrust is not a reason to keep it.
    """
    views = [view for view in (manifest or {}).get("views") or [] if isinstance(view, dict)]
    if not views:
        return [], "no views in oracle manifest"

    luid = identity.get("luid")
    if not luid:
        return [], (
            f"no workbook LUID for this unit ({identity.get('reason')}), so no render can be "
            "attributed - a display name is not an identity (#450)"
        )
    picked = [view for view in views if str(view.get("workbook_luid") or "").casefold() == luid.casefold()]
    if not picked:
        return [], f"no oracle view carries workbook_luid {luid}"
    return picked, "workbook_luid"


def view_kind(view: dict[str, Any]) -> str:
    """`dashboard`/`worksheet` when the capture RESOLVED the type, else `unknown`.

    `content_url` is `<wb>/sheets/<view>` for both kinds and is not a discriminator;
    `capture_tableau_oracle.py`'s type resolver is non-fatal, so `unknown` is a real value that must
    be carried and marked rather than defaulted.
    """
    declared = view.get("view_type")
    if isinstance(declared, str) and declared.strip().casefold() in (KIND_DASHBOARD, KIND_WORKSHEET):
        return declared.strip().casefold()
    return KIND_UNKNOWN


def object_filename(name: str, luid: str, taken: set[str]) -> str:
    """A filesystem-safe, collision-free stem for one captured object."""
    cleaned = _UNSAFE.sub("_", str(name or "")).strip(" ._") or "view"
    cleaned = cleaned[:_MAX_OBJECT_NAME].strip(" ._") or "view"
    stem = cleaned
    if stem.casefold() in taken:
        stem = f"{cleaned}__{str(luid)[:8]}"
    suffix = 2
    while stem.casefold() in taken:
        stem = f"{cleaned}__{str(luid)[:8]}_{suffix}"
        suffix += 1
    taken.add(stem.casefold())
    return stem


def _copy_leg(
    source_dir: Path, dest_dir: Path, leg: Any, target: Path, rel_prefix: str
) -> tuple[dict[str, Any] | None, str | None]:
    """`(rewritten leg, omission reason)` for one render or data leg."""
    if not isinstance(leg, dict):
        return None, None
    if leg.get("status") != "ok" or not isinstance(leg.get("path"), str):
        return dict(leg), None
    origin = source_dir / leg["path"]
    if not origin.is_file():
        rewritten = dict(leg)
        rewritten["status"] = OMITTED_STATUS
        rewritten["packaging_reason"] = f"capture claims {leg['path']} but the file is absent"
        return rewritten, rewritten["packaging_reason"]
    destination = dest_dir / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origin, destination)
    rewritten = dict(leg)
    rewritten["path"] = f"{rel_prefix}/{target.name}"
    rewritten["packaged_from"] = leg["path"]
    return rewritten, None


def package_oracle(  # pylint: disable=too-many-locals
    views: list[dict[str, Any]], manifest: Any, oracle_dir: Path, dest: Path
) -> dict[str, Any]:
    """Copy this unit's renders and numbers into `<dest>`, type-separated, and rewrite the manifest.

    Bytes are copied verbatim, so every `sha256`/`bytes` the capture recorded still verifies -
    `reference_evidence.render_facts` checks exactly those, and a re-encoded copy would be rejected.
    Only `path` changes.
    """
    taken: set[str] = set()
    packaged: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []

    for view in views:
        kind = view_kind(view)
        luid = str(view.get("view_luid") or "")
        stem = object_filename(str(view.get("view_name") or view.get("view_url_name") or ""), luid, taken)
        record = dict(view)
        images: list[str] = []

        for leg_name in RENDER_LEGS:
            suffix = Path(str((view.get(leg_name) or {}).get("path") or "")).suffix or f".{leg_name}"
            rewritten, reason = _copy_leg(
                oracle_dir, dest, view.get(leg_name), Path(kind) / "images" / f"{stem}{suffix}", f"{kind}/images"
            )
            if rewritten is not None:
                record[leg_name] = rewritten
            if reason:
                omissions.append({"view_luid": luid, "leg": leg_name, "reason": reason})
            elif rewritten is not None and rewritten.get("status") == "ok":
                images.append(str(rewritten["path"]))

        rewritten, reason = _copy_leg(
            oracle_dir, dest, view.get("data"), Path(kind) / "data" / f"{stem}.csv", f"{kind}/data"
        )
        if rewritten is not None:
            record["data"] = rewritten
        if reason:
            omissions.append({"view_luid": luid, "leg": "data", "reason": reason})
        numbers = (
            str(rewritten["path"]) if rewritten is not None and rewritten.get("status") == "ok" and not reason else None
        )

        record["packaged_object_stem"] = stem
        packaged.append(record)
        objects.append(
            {
                "name": view.get("view_name"),
                "view_luid": luid,
                "view_type": kind,
                "declared_view_type": view.get("view_type"),
                # Visual and numeric are kept APART, not merged into one file list. The operator asked
                # for both and they answer different questions - and merging them mislabels a
                # data-only view as a render, which `grep -c ^ORACLE_RENDER` would then over-report.
                "images": images,
                "data": numbers,
            }
        )

    scoped = {key: value for key, value in (manifest or {}).items() if key != "views"}
    scoped["views"] = packaged
    scoped["view_count"] = len(packaged)
    scoped["scoped_by"] = "package_unit.py: views filtered to this unit and re-filed by view_type"
    write_json(dest / "oracle-manifest.json", scoped)
    return {"objects": objects, "omissions": omissions}


# --------------------------------------------------------------------------------------------
# the greppable handover
# --------------------------------------------------------------------------------------------


def visual_pages(report_dir: Path | None) -> dict[str, str]:
    """`{visual id: page id}` from the PBIR tree, so an emptied visual can name its page.

    The engine's `pbip_ref_drops[]` rows carry only `visual` - measured, the three keys are
    `dropped`, `emptied`, `visual` across all 28 rows of a real estate run - and a bare
    `v-page-Dashboard06ca9874` is not something an operator can act on.
    """
    if report_dir is None or not report_dir.is_dir():
        return {}
    pages = report_dir / "definition" / "pages"
    return {
        visual.name: page.name
        for page in sorted(pages.iterdir() if pages.is_dir() else [])
        if (page / "visuals").is_dir()
        for visual in sorted((page / "visuals").iterdir())
        if visual.is_dir()
    }


def _field(value: Any) -> str:
    """One `key=value` field's value: single-line, never empty, so a line always has all its fields."""
    text = " ".join(str(value).split()) if value not in (None, "") else "-"
    return text.replace("|", "/")


def handover_lines(workbook: dict[str, Any], pages: dict[str, str]) -> list[str]:
    """The flat, one-finding-per-line body, EMPTIED VISUALS FIRST.

    `read_handover.py` documents why they lead: an emptied visual renders blank on a report that
    validates clean, and nothing else in the toolkit surfaces them - 15 sat unremarked beside a
    170-item worklist. Every line is `PREFIX key=value ...` so `grep '^EMPTIED_VISUAL'` is the whole
    interface.
    """
    lines: list[str] = []
    for drop in read_handover._emptied_visuals(workbook):  # pylint: disable=protected-access
        visual = str(drop.get("visual") or "")
        dropped = "; ".join(str(item) for item in drop.get("dropped") or []) or "-"
        lines.append(
            f"EMPTIED_VISUAL page={_field(pages.get(visual, 'unknown'))} visual={_field(visual)} "
            f"dropped={_field(dropped)}"
        )

    for request in read_handover.requests_of(workbook):
        lines.append(
            f"STUB_MEASURE table={_field(request.get('target_table'))} name={_field(request.get('name'))} "
            f"role={_field(request.get('role'))} category={_field(request.get('category'))} "
            f"blocked_by={_field(', '.join(str(x) for x in request.get('blocked_by') or []) or '-')} "
            f"formula={_field(request.get('formula'))}"
        )

    for item in read_handover.report_items_of(workbook):
        lines.append(
            f"WORKLIST severity={_field(item.get('severity'))} category={_field(item.get('category'))} "
            f"page={_field(item.get('page_display') or item.get('page'))} visual={_field(item.get('visual'))} "
            f"worksheet={_field(item.get('worksheet'))} reason={_field(item.get('reason'))} "
            f"remediation={_field(item.get('remediation'))}"
        )

    for row in workbook.get("visuals_projecting_stub_measures") or []:
        if isinstance(row, dict):
            lines.append(
                f"STUB_PROJECTED page={_field(row.get('page'))} visual={_field(row.get('visual'))} "
                f"measure={_field(row.get('measure'))}"
            )

    status, warnings = read_handover.pbip_warning_status(workbook)
    for warning in warnings:
        lines.append(f"PBIP_WARNING text={_field(warning)}")
    if status not in (read_handover.PBIP_WARNING_PRESENT, read_handover.PBIP_WARNING_NONE):
        lines.append(f"PBIP_WARNING_UNRECORDED status={_field(status)}")

    for row in workbook.get("viz_fidelity") or []:
        if isinstance(row, dict) and row.get("evidence") != "emitted+linted":
            lines.append(
                f"FIDELITY evidence={_field(row.get('evidence'))} tier={_field(row.get('tier'))} "
                f"worksheet={_field(row.get('worksheet'))} visual_type={_field(row.get('visual_type'))} "
                f"reason={_field(row.get('reason'))}"
            )
    return lines


def render_handover(result: dict[str, Any], workbook: dict[str, Any] | None, pages: dict[str, str]) -> str:
    """The whole `handover.md`: a header an agent can read, then one finding per line."""
    identity = result["workbook_identity"]
    head = [
        f"# handover: {result['unit']}",
        "#",
        "# One finding per line, `PREFIX key=value ...`. Grep a prefix; do not parse this as prose.",
        "# Prefixes, in the order they appear: EMPTIED_VISUAL (blank on a report that validates "
        "clean - fix first), STUB_MEASURE, WORKLIST, STUB_PROJECTED, PBIP_WARNING, FIDELITY,",
        "#   then the reference inventory: ORACLE_ATTRIBUTION, ORACLE_RENDER, ORACLE_NO_RENDER, "
        "UNTYPED_RENDER, ORACLE_OMISSION, PACKAGE_NOTE.",
        "#",
        f"UNIT name={_field(result['unit'])} kind={_field(result['kind'])} engine={_field(result.get('engine'))}",
        f"PACKAGE spec={_field(result['artifacts'].get('migration_spec'))} "
        f"source={_field(result['artifacts'].get('asset'))} "
        f"report={_field(result['artifacts'].get('report'))} model={_field(result['artifacts'].get('model'))}",
    ]
    body = handover_lines(workbook, pages) if workbook else ["PACKAGE_NOTE text=no handover slice for this unit"]

    oracle = result["oracle"]
    tail = [
        f"ORACLE_ATTRIBUTION route={_field(oracle.get('route'))} luid={_field(identity.get('luid'))} "
        f"match={_field(identity.get('match'))} views={_field(len(oracle.get('objects') or []))} "
        f"reason={_field(identity.get('reason') or oracle.get('reason'))}"
    ]
    for obj in oracle.get("objects") or []:
        # Three prefixes, not one, and keyed on the IMAGE legs alone: a selected view with no usable
        # render must not be greppable as a render. `grep -c ^ORACLE_RENDER` is the inventory an agent
        # will trust, and counting a data-only view into it over-reports the reference they think they
        # have. `unknown` wins over both, because "I cannot tell what this is a picture of" is the
        # louder fact.
        if obj["view_type"] == KIND_UNKNOWN:
            prefix = "UNTYPED_RENDER"
        else:
            prefix = "ORACLE_RENDER" if obj["images"] else "ORACLE_NO_RENDER"
        tail.append(
            f"{prefix} type={_field(obj['view_type'])} object={_field(obj['name'])} "
            f"luid={_field(obj['view_luid'])} images={_field(', '.join(obj['images']) or 'none')} "
            f"data={_field(obj['data'] or 'none')}"
        )
    for omission in oracle.get("omissions") or []:
        tail.append(
            f"ORACLE_OMISSION view={_field(omission.get('view_luid'))} leg={_field(omission.get('leg'))} "
            f"reason={_field(omission.get('reason'))}"
        )
    for note in result.get("notes") or []:
        tail.append(f"PACKAGE_NOTE text={_field(note)}")
    return "\n".join(head + [""] + body + [""] + tail) + "\n"


README = """# {unit}

Self-contained handover package for one migration unit ({kind}). Both entry and exit gates run
against this folder with **no flags**:

    python scripts/check_reference_readiness.py {unit}
    python scripts/check_unit.py {unit}

| path | what it is |
|---|---|
| `handover.md` | every engine finding, one per line, emptied visuals first. **Start here.** |
| `handover/{unit}.json` | the engine's full slice; `python scripts/read_handover.py handover/{unit}.json --viz` |
| `fabric/` | the engine WORKING COPY - edit here. The pristine baseline stays in `<bundle>/reports/`. |
| `assets/` | the Tableau source this was built from |
| `migration-spec.json` | the parsed source; the expected page set both gates grade against |
| `oracle/` | this unit's Tableau reference, split `dashboards/` vs `worksheets/` vs `unknown/` |
| `package-manifest.json` | what was packaged, and every omission with its reason |

`oracle/` is **layout/text grade only**: an oracle capture is taken in the view's default state with
no `?vf_` filter pinning, so a visual PASS signed off on it alone is overstated. Renders present here
are not a claim of byte-faithfulness either - see `ORACLE_ATTRIBUTION ... match=` in `handover.md`,
and record the ceiling in `limitations_encountered`.
"""


# --------------------------------------------------------------------------------------------
# packaging one unit
# --------------------------------------------------------------------------------------------


def conflicting_evidence_dirs(out_root: Path) -> list[Path]:
    """Evidence directories that would SHADOW every package written under ``out_root``.

    `check_reference_readiness._collect_evidence` looks for `reference/`, `_oracle/` and `oracle/`
    beside the target, beside its parent AND beside its grandparent - so a package at
    `<out>/<Unit>/` also picks up anything at `<out>/` and `<out>/../`. Writing packages inside the
    run directory therefore lets the gate see the packaged subset AND the original flat capture at
    `_runs/<NNN>/oracle/`.

    Measured while writing this file's own fixture: with both visible, every view is matched twice,
    the gate refuses ("2 records share this name once normalized") and all four pages go from
    **ready** to **unverifiable**. That is strictly worse than not packaging at all, and it is
    silent, so it is refused up front rather than documented.
    """
    names = ("reference", "oracle", "_oracle")
    return [base / name for base in (out_root, out_root.parent) for name in names if (base / name).is_dir()]


def _copy_fabric(bundle: Path, unit: str, dest: Path) -> tuple[str | None, str | None]:
    """Copy the engine WORKING COPY into `<dest>/fabric/`; `(report name, model name)`.

    `pbip/<Unit>/` is copied whole so `definition.pbir`'s `byPath` - measured as
    `../<Model>.SemanticModel`, and a model name that differs from the unit name in 27 of 62 units -
    keeps resolving. `reports/` is the engine BASELINE and is never shipped: no model sits beside it.
    """
    source = bundle / "pbip" / unit
    if not source.is_dir():
        return None, None
    shutil.copytree(source, dest / "fabric", dirs_exist_ok=True)
    report = next((path.name for path in sorted((dest / "fabric").iterdir()) if path.name.endswith(".Report")), None)
    model = next(
        (path.name for path in sorted((dest / "fabric").iterdir()) if path.name.endswith(".SemanticModel")), None
    )
    return report, model


def _scope_definition_of_done(dod: Any, unit: str) -> dict[str, Any] | None:
    """`definition_of_done` narrowed to this unit's own row, or None when the engine wrote none.

    Its `workbooks[]` is a per-workbook DoD row carrying `workbook`, `pbip_folder`, `bound_model`,
    `report_bound`, `status` and the failure `reason`. Measured on the reference bundle it holds
    **48** rows - so it is a second copy of the estate, and copying it whole leaks exactly what
    filtering `workbooks[]` was meant to stop.

    The estate counters beside it (`status`, `reports_bound`, `reports_failed`, `reports_warned`) are
    DROPPED rather than carried, and that is a correctness fix as much as a scoping one: measured,
    the estate's `status` is `"failed"` because 18 of 48 reports failed, and in a one-unit package
    that reads as this unit's verdict. The unit's real verdict is on its own row.
    """
    if not isinstance(dod, dict):
        return None
    scoped: dict[str, Any] = {
        "workbooks": [
            row for row in dod.get("workbooks") or [] if isinstance(row, dict) and row.get("workbook") == unit
        ]
    }
    if "applicable" in dod:
        scoped["applicable"] = dod["applicable"]
    scoped["scoped_by"] = (
        "package_unit.py: workbooks[] filtered to this unit; the estate's own status/reports_* "
        "counters dropped, because in a one-unit package they read as this unit's verdict"
    )
    return scoped


def _scope_report(engine_report: Any, unit: str) -> dict[str, Any]:
    """A `report.json` BUILT for this unit - never the estate's with two lists filtered.

    ⚠️ **This is an ALLOWLIST, and the direction is the whole point.** It previously copied every
    key it did not explicitly filter, which is fail-open by construction: any field the engine adds
    later arrives in every package unnoticed. Round-1 blind review measured the consequence on
    `HR_Dashboard` in the 48-workbook reference bundle - `workbooks[]` was filtered to 1 entry while
    **11 of the 13 top-level fields were byte-identical to the whole-estate report**:
    `input_manifest.assets` still listed **67** assets with absolute staged paths,
    `openable_outputs` still listed **62** units with absolute `pbip`/`report_folder`/`model_folder`
    paths, `definition_of_done.workbooks` still held **48** rows, and `Superstore`,
    `World_Indicators` and `Groups` were all still greppable in a package advertised as one unit.
    A handover package is a customer deliverable; it must not name another customer's workbooks.

    So an unknown field is now DROPPED, not carried, and its NAME (never its value - names are engine
    schema, not customer data) is recorded in `scope.dropped_fields` so the omission is discoverable
    rather than silent. That trade is deliberate: the leak blocks a merge, while a gate that one day
    wants a dropped field fails closed and says so in the package itself.

    Over-trimming is the opposite failure and is bounded by measurement, not by taste. The gate
    surface of `report.json` is exactly two fields, read at four sites:
    `check_reference_readiness._engine_report` (:461-462) rejects the whole file unless `workbooks`
    is a **list**, `._unit_names` (:478-483) reads `workbooks[].name`/`datasources[].name`, and
    `check_unit._is_engine_report` (:378-380) again requires `workbooks` to be a list. Both are
    always written, as lists, even when empty - a datasource unit whose `workbooks` went missing
    would lose `_datasource_only`'s earned `NOT_APPLICABLE` and read as a broken workbook.

    Entries are kept WHOLE rather than reduced to a `{"name": ...}` stub: the gates read only the
    name, but the entry is the engine's own account of what it did with *this* unit.
    """
    payload = engine_report if isinstance(engine_report, dict) else {}
    scoped: dict[str, Any] = {key: payload[key] for key in REPORT_VERBATIM_FIELDS if key in payload}
    for collection in REPORT_UNIT_LISTS:
        scoped[collection] = [
            entry for entry in payload.get(collection) or [] if isinstance(entry, dict) and entry.get("name") == unit
        ]
    dod = _scope_definition_of_done(payload.get("definition_of_done"), unit)
    if dod is not None:
        scoped["definition_of_done"] = dod
    kept = set(scoped)
    scoped["scoped_by"] = "package_unit.py: rebuilt for this unit from an allowlist, not filtered"
    scoped["scope"] = {
        "unit": unit,
        "kept_fields": sorted(kept),
        "dropped_fields": sorted(set(payload) - kept),
        "reason": (
            "estate-wide: a one-unit handover package must not carry another unit's names, paths or "
            "status. Field NAMES are listed (engine schema); values are not."
        ),
    }
    return scoped


def _scope_receipt(receipt: Any, unit: str) -> dict[str, Any] | None:
    """The engine receipt, narrowed to the artifacts this package actually contains.

    Copying the bundle receipt verbatim would be 780 KB per unit attesting to 3,138 artifacts, 3,135
    of which are not here. Scoped, it still answers `check_engine_receipts.py`'s only question -
    `engine.version` - and its `artifacts[]` hashes now name real files in the package, with the
    `pbip/<unit>/` prefix rewritten to `fabric/`.

    It deliberately does NOT become a credential-gate exemption: `credential_gate._receipt_matches_bundle`
    additionally requires `report_sha256`/`input_manifest_sha256` to hash the files beside it, and the
    package's `report.json` is scoped, so that check fails closed exactly as it should.
    """
    if not isinstance(receipt, dict):
        return None
    prefix = f"pbip/{unit}/"
    artifacts = [
        {**entry, "path": f"fabric/{entry['path'][len(prefix) :]}"}
        for entry in receipt.get("artifacts") or []
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"].startswith(prefix)
    ]
    scoped = {key: value for key, value in receipt.items() if key != "artifacts"}
    scoped["artifacts"] = artifacts
    scoped["scoped_by"] = f"package_unit.py: artifacts[] filtered to pbip/{unit}/ and re-rooted at fabric/"
    return scoped


def _write_spec(asset: Path | None, dest: Path) -> tuple[str | None, str | None]:
    """`(relative spec path, failure note)` - `check_unit.py` cannot grade a unit without one (#443)."""
    if asset is None:
        return None, "no migration-spec.json: the source asset could not be resolved"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT_DIR / "parse_tableau.py"), str(asset), "-o", str(dest / "migration-spec.json")],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if proc.returncode != 0 or not (dest / "migration-spec.json").is_file():
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, f"parse_tableau.py failed (exit {proc.returncode}): {detail[-1] if detail else 'no output'}"
    return "migration-spec.json", None


def _attach_oracle(oracle_dir: Path | None, identity: dict[str, Any], dest: Path) -> dict[str, Any]:
    """This unit's slice of the flat capture, or an empty slice carrying the refusal reason."""
    oracle: dict[str, Any] = {"objects": [], "omissions": [], "route": None, "reason": None}
    manifest = read_json(oracle_dir / "oracle-manifest.json") if oracle_dir else None
    if manifest is None:
        oracle["reason"] = "no oracle-manifest.json found" if oracle_dir else "no oracle capture supplied"
        return oracle
    views, route = select_views(manifest, identity)
    if not views:
        oracle["reason"] = route
        return oracle
    oracle.update(package_oracle(views, manifest, oracle_dir, dest / "oracle"))
    oracle["route"] = route
    return oracle


def _handover_workbook(handover: Any, unit: str, dest: Path) -> dict[str, Any] | None:
    """The workbook payload inside a handover slice, via read_handover's own resolver."""
    if not isinstance(handover, dict):
        return None
    found = read_handover._workbooks_from_payload(handover, dest)  # pylint: disable=protected-access
    return next((wb for name, wb, _ in found if name == unit), found[0][1] if found else None)


def package_unit(  # pylint: disable=too-many-locals
    bundle: Path, unit: str, out_root: Path, *, oracle_dir: Path | None, assets_dir: Path | None
) -> dict[str, Any]:
    """Assemble one unit's package. Returns the record written to `package-manifest.json`."""
    engine_report = read_json(bundle / "report.json")
    workbooks, datasources = engine_unit_names(engine_report)
    dest = out_root / unit
    dest.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    report_name, model_name = _copy_fabric(bundle, unit, dest)
    if report_name is None and model_name is None:
        notes.append(f"no engine working copy at pbip/{unit} - nothing to build on")

    handover = read_json(bundle / "handover" / f"{unit}.json")
    (dest / "handover").mkdir(parents=True, exist_ok=True)
    if isinstance(handover, dict):
        shutil.copy2(bundle / "handover" / f"{unit}.json", dest / "handover" / f"{unit}.json")
    else:
        notes.append(f"no handover slice at handover/{unit}.json")

    asset, asset_route = resolve_asset(bundle, unit, handover, assets_dir)
    if asset is not None:
        (dest / "assets").mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset, dest / "assets" / asset.name)
        asset = dest / "assets" / asset.name
    else:
        notes.append(f"source asset unresolved ({asset_route}); both gates will report CANNOT_ESTABLISH")

    scoped_provenance, entries = scope_provenance(read_json(bundle / "source-provenance.json"), sha256_of(asset))
    write_json(dest / "source-provenance.json", scoped_provenance)
    write_json(dest / "report.json", _scope_report(engine_report, unit))
    receipt = _scope_receipt(read_json(bundle / "engine-output-receipt.json"), unit)
    if receipt is not None:
        write_json(dest / "engine-output-receipt.json", receipt)

    identity = workbook_identity(entries, asset)
    oracle = _attach_oracle(oracle_dir, identity, dest)
    spec, spec_note = _write_spec(asset, dest)
    if spec_note:
        notes.append(spec_note)

    result = {
        "unit": unit,
        "kind": unit_kind(unit, workbooks, datasources),
        "engine": ((receipt or {}).get("engine") or {}).get("version"),
        "packaged": report_name is not None or model_name is not None,
        "artifacts": {
            "migration_spec": spec,
            "asset": f"assets/{asset.name}" if asset else None,
            "asset_route": asset_route,
            "report": f"fabric/{report_name}" if report_name else None,
            "model": f"fabric/{model_name}" if model_name else None,
            "handover": f"handover/{unit}.json" if isinstance(handover, dict) else None,
        },
        "workbook_identity": identity,
        "oracle": oracle,
        "notes": notes,
    }
    report_dir = dest / "fabric" / report_name if report_name else None
    workbook = _handover_workbook(handover, unit, dest)
    (dest / "handover.md").write_text(render_handover(result, workbook, visual_pages(report_dir)), encoding="utf-8")
    (dest / "README.md").write_text(README.format(unit=unit, kind=result["kind"]), encoding="utf-8")
    write_json(dest / "package-manifest.json", result)
    return result


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def render(results: list[dict[str, Any]], out_root: Path) -> str:
    """The human verdict: one line per unit, then the totals that make an omission visible."""
    lines = [f"package_unit: {len(results)} unit(s) -> {out_root}"]
    for result in sorted(results, key=lambda item: item["unit"]):
        oracle = result["oracle"]
        objects = oracle.get("objects") or []
        untyped = sum(1 for obj in objects if obj["view_type"] == KIND_UNKNOWN)
        detail = f"{len(objects)} oracle object(s) via {oracle.get('route')}" if objects else "no oracle evidence"
        lines.append(
            f"  {'OK  ' if result['packaged'] else 'MISS'} {result['unit']} [{result['kind']}] - {detail}"
            + (f", {untyped} untyped" if untyped else "")
            + (f"; {len(result['notes'])} note(s)" if result["notes"] else "")
        )
    packaged = sum(1 for result in results if result["packaged"])
    with_oracle = sum(1 for result in results if result["oracle"].get("objects"))
    lines.append(f"packaged {packaged}/{len(results)}; {with_oracle} carry oracle evidence")
    starved = sorted(result["unit"] for result in results if not result["packaged"])
    if starved:
        lines.append(
            f"WARN: {len(starved)} unit(s) have NO engine working copy under pbip/ - packaged for their "
            f"source, reference and handover only, with nothing to build on: {', '.join(starved)}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Package the requested units and report what each one carries."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("Attribution", maxsplit=1)[0])
    parser.add_argument("--bundle", type=Path, required=True, help="engine bundle root (holds pbip/, handover/)")
    parser.add_argument("--out", type=Path, required=True, help="directory to write <Unit>/ packages into")
    parser.add_argument("--unit", action="append", default=[], help="package only this unit (repeatable)")
    parser.add_argument("--oracle", type=Path, help="oracle capture holding oracle-manifest.json")
    parser.add_argument("--assets", type=Path, help="directory holding the harvested .twb/.twbx/.tds assets")
    parser.add_argument("--json", type=Path, help="write the machine-readable packaging report here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered summary")
    args = parser.parse_args(argv)

    bundle = args.bundle.resolve()
    if not bundle.is_dir():
        parser.error(f"--bundle {args.bundle} is not a directory")
    oracle_dir = args.oracle.resolve() if args.oracle else discover_dir(bundle, ("oracle", "_oracle"))
    assets_dir = args.assets.resolve() if args.assets else discover_dir(bundle, ("assets",))

    available = bundle_units(bundle)
    units = args.unit or available
    unknown = [unit for unit in units if unit not in available]
    if unknown:
        parser.error(f"the bundle's report.json and pbip/ know nothing of: {', '.join(sorted(unknown))}")

    out_root = args.out.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    shadowing = conflicting_evidence_dirs(out_root)
    if shadowing:
        parser.error(
            f"--out {args.out} sits beside evidence the gates also scan "
            f"({', '.join(str(path) for path in shadowing)}). A package there is matched against BOTH "
            "its own oracle and that one, and every page becomes 'unverifiable' rather than ready. "
            "Choose an --out outside the capture tree."
        )
    results = [
        package_unit(bundle, unit, out_root, oracle_dir=oracle_dir, assets_dir=assets_dir) for unit in sorted(units)
    ]

    payload = {
        "id": "package-unit",
        "bundle": str(bundle),
        "out": str(out_root),
        "oracle": str(oracle_dir) if oracle_dir else None,
        "assets": str(assets_dir) if assets_dir else None,
        "units": results,
    }
    if args.json:
        write_json(args.json, payload)
    if not args.quiet:
        print(render(results, out_root))
    return 0 if all(result["packaged"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
