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
            dashboard/{images,data}/<Object>.<ext>   <- SINGULAR: the directory is object_identity's
            worksheet/{images,data}/<Object>.<ext>      KIND_* value verbatim, never a pluralised copy
            unknown/{images,data}/<Object>.<ext>     <- carried but MARKED, never filed as either kind
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

# Packaging, the evidence walk, path containment and the CLI deliberately live together: this module
# IS the per-unit handover boundary, and splitting it would put the containment predicates a hop away
# from the copy that must obey them - which is exactly how `retained_path` escaped the `path` guard.
# Nine sibling scripts carry the same waiver (`check_unit.py`, `run_estate.py`, `parse_tableau.py`, ...).
# ⚠️ Revisit when issue #497 restructures staging; that work reopens this file anyway.
# pylint: disable=too-many-lines
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import read_handover  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_oracle_manifest  # noqa: E402  # pylint: disable=wrong-import-position
from manifest_scope import (  # noqa: E402  # pylint: disable=wrong-import-position
    ORACLE_MANIFEST_ALLOW,
    project,
    scope_handover,
    scope_receipt,
    scope_report,
    shippable_provenance,
    stamp_scope,
)
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
#: What replaces a refused leg's `path`. The declared string is attacker-controlled - the oracle
#: manifest is written by a separate tool against a live server - so it is never echoed back into the
#: packaged manifest or into `handover.md`, which would re-open the exfiltration channel one level
#: down: the bytes would not be copied, but the absolute path would still ship.
REFUSED_PATH = "<refused-by-packager>"
KIND_DIRS = (KIND_DASHBOARD, KIND_WORKSHEET, KIND_UNKNOWN)
_LUID_PREFIX = re.compile(r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_")
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")
#: Well short of any filesystem limit: `check_path_ceiling.py` gates the WHOLE path, and these names
#: sit under `<out>/<Unit>/oracle/<kind>/images/`, which is already five segments deep.
_MAX_OBJECT_NAME = 60

KIND_WORKBOOK = "workbook"
KIND_DATASOURCE = "datasource"
KIND_UNCLASSIFIED = "unclassified"


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

    ⚠️ **Round-3 finding: this was still a denylist**, copying every top-level key except `inputs`
    and every field inside a retained entry - so `future_scan_root` and
    `inputs[].origin.future_source_path` both shipped. It now goes through the same `project()` as
    every other manifest, against a spec that is exactly the three fields the gate reads
    (`input.sha256`, `origin.match`, `origin.workbook_luid`). `workbook_name` and `project` are
    deliberately NOT carried: on a foreign entry they are the identity channel itself.

    The RETURNED entries are the unprojected ones, because `workbook_identity` adjudicates on
    `origin.workbook_name` before deciding whether anything may be attributed. What ships is the
    projected, adjudicated subset - see `shippable_provenance`.
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
    return payload, entries


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


def _declares_non_relative(declared: str) -> bool:
    """True when a DECLARED string names an absolute, drive-relative or UNC location.

    Extracted so the containment rule has exactly ONE definition. It was inline in
    :func:`_resolve_capture_file`, keyed to the `path` field, and that is precisely how round-4
    review walked around it: `withhold_uncertified_evidence` renames `path` to `retained_path`
    BEFORE the packager looks, so a legacy record's absolute host path met no check at all.
    """
    candidate = Path(declared)
    return candidate.is_absolute() or bool(candidate.drive) or declared.startswith(("\\\\", "/"))


def _declares_unsafe_path(declared: str) -> bool:
    """True when a declared string may not ship in the packaged manifest under ANY field name.

    Non-relative (a host path, which names the customer's server, project and operator) or carrying
    a `..` component (which claims a location outside the capture). Deliberately a predicate over a
    VALUE, not a list of blessed field names: a second field name was the round-4 defect, and a
    third one must not reopen it.
    """
    return _declares_non_relative(declared) or ".." in Path(declared).parts


def _contain_unsafe_strings(value: Any, prefix: str = "") -> tuple[Any, list[str]]:
    """`(value with EVERY unsafe declared string refused, the JSON paths refused)` - recursive.

    ⚠️ **Round-5 finding.** Rounds 3 and 4 each moved the boundary instead of removing it: round 3
    guarded the `path` NAME, so `retained_path` walked around it; round 4 made the check a predicate
    over a VALUE but iterated `leg.items()` only, so a string inside a retained CONTAINER walked
    around that. Measured on the tip, one absolute host path per shape::

        image.retry_reasons[0]   -> shipped verbatim   (allowlisted SCALAR_LIST)
        image.dimensions_px[0]   -> shipped verbatim   (allowlisted SCALAR_LIST)
        views[].flags[0]         -> shipped verbatim   (view level, never reached the leg guard)
        views[].view_name        -> shipped verbatim   (view level, plain scalar)

    So the invariant is neither a field name nor a top-level value: **no string reachable from a
    shipped document may declare a non-relative or escaping path, wherever it sits.** This walks
    strings in dicts, lists, tuples and any nesting of them, which is why a fourth allowlisted
    container, a nested dict, or a list of dicts cannot reopen it - there is no level left that is
    not visited. Non-string scalars are returned untouched, so counts, verdicts and flags survive.

    ⚠️ **An ALREADY-refused string reports its path too** (round 6). Containment now happens once at
    the source, so by the time a later reader walks the same value the sentinel is all that is left -
    and a walk that reported nothing would silently retire two things this package needs: the leg's
    `ORACLE_OMISSION` diagnosis, and `scope.refused_fields`. Reporting the sentinel makes the walk
    idempotent: running it twice refuses the same set, so containment can move earlier without any
    downstream reader losing what it could previously see.
    """
    if isinstance(value, str):
        if value == REFUSED_PATH:
            return value, [prefix or "."]
        return (REFUSED_PATH, [prefix or "."]) if _declares_unsafe_path(value) else (value, [])
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        refused: list[str] = []
        for key, item in value.items():
            cleaned, hit = _contain_unsafe_strings(item, f"{prefix}.{key}" if prefix else str(key))
            kept[key] = cleaned
            refused.extend(hit)
        return kept, refused
    if isinstance(value, (list, tuple)):
        rows: list[Any] = []
        refused = []
        for index, item in enumerate(value):
            cleaned, hit = _contain_unsafe_strings(item, f"{prefix}[{index}]")
            rows.append(cleaned)
            refused.extend(hit)
        return (tuple(rows) if isinstance(value, tuple) else rows), refused
    return value, []


def _contain_declared_paths(leg: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """`(leg with every unsafe declared string refused, diagnosis)` - the field-AGNOSTIC guard.

    ⚠️ **Round-4 finding 1.** `_resolve_capture_file` protects the `path` field only, so it is a
    guard on one NAME rather than on the manifest. `withhold_uncertified_evidence` demotes an
    uncertified data leg's `path` to `retained_path` before packaging (correctly - the bytes are not
    evidence), and the packager then shipped that value verbatim, allowlisted by `manifest_scope`.
    The measured differential, one certified and one legacy record declaring the SAME absolute path:

        certified_data.path=<refused-by-packager>      # guarded
        legacy_data.retained_path=C:\\Users\\...\\oracle.csv   # shipped

    That is #461's class - a customer package carrying an absolute host path - and this repo is
    public. So the check runs over every string the manifest declared, whatever it called the field.
    Values this packager writes itself (`path`, `packaged_from`) are already normalised and
    capture-relative, so re-checking them is free and keeps the rule unconditional.
    """
    contained, refused = _contain_unsafe_strings(leg)
    if not refused:
        return leg, None
    fields = ", ".join(f"'{name}'" for name in sorted(refused))
    return contained, f"capture declares a non-relative or escaping path in {fields} ({REFUSED_PATH}) - refused"


def _resolve_capture_file(oracle_root: Path, declared: str) -> tuple[Path | None, str | None]:
    """`(resolved file, refusal reason)` for a capture-relative path the MANIFEST asked us to copy.

    ⚠️ **The oracle manifest is UNTRUSTED INPUT.** It is written by a separate tool against a live
    Tableau server, and this function is the boundary where that matters. Round-3 review measured
    both exploits against the previous `source_dir / leg["path"]`:

    * `"../outside-secret.png"` - copied byte-identically into `oracle/worksheet/images/Sales.png`;
    * an absolute path - copied, AND written verbatim into the packaged manifest.

    So the check is containment, not sanitisation of the string: reject an absolute or drive-relative
    path outright, resolve **strictly** (which follows symlinks and normalises `..`), and require the
    result to stay under the resolved capture root. Resolving both sides is what closes the symlink
    route - a link inside the capture pointing outside it normalises to an outside path, and
    comparing unresolved strings would not see that.
    """
    if not declared:
        return None, "capture declares an empty path"
    candidate = Path(declared)
    if _declares_non_relative(declared) or declared == REFUSED_PATH:
        # The sentinel means source containment (round 6) already refused this string, so the
        # declared form no longer exists here. It is diagnosed as the refusal it is, rather than
        # left to `resolve(strict=True)` to report as a missing file - which would read to an
        # operator as a capture defect instead of a refusal this packager made deliberately.
        return None, f"capture declares a non-relative path ({REFUSED_PATH}) - refused"
    try:
        root = oracle_root.resolve(strict=True)
        resolved = (root / candidate).resolve(strict=True)
    except OSError:
        return None, "capture path does not resolve to a file"
    if not resolved.is_relative_to(root):
        return None, f"capture path escapes the capture root ({REFUSED_PATH}) - refused"
    if not resolved.is_file():
        return None, "capture path does not resolve to a file"
    return resolved, None


def _copy_leg(
    source_dir: Path, dest_dir: Path, leg: Any, target: Path, rel_prefix: str
) -> tuple[dict[str, Any] | None, str | None]:
    """`(rewritten leg, omission reason)` for one render or data leg.

    ⚠️ Every branch returns through :func:`_contain_declared_paths`, including the ones that copy
    nothing. A leg the packager declines to copy is not a leg it may echo verbatim - that was
    round-4 finding 1, where an uncertified data leg's `retained_path` reached the packaged manifest
    without meeting any check.
    """
    if not isinstance(leg, dict):
        return None, None
    if leg.get("status") != "ok" or not isinstance(leg.get("path"), str):
        return _contain_declared_paths(_state_withheld_bytes(dict(leg)))
    origin, refusal = _resolve_capture_file(source_dir, leg["path"])
    if origin is None:
        rewritten = dict(leg)
        rewritten["status"] = OMITTED_STATUS
        rewritten["packaging_reason"] = refusal or "capture path unusable"
        rewritten["path"] = REFUSED_PATH
        contained, _ = _contain_declared_paths(rewritten)
        return contained, rewritten["packaging_reason"]
    destination = dest_dir / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origin, destination)
    rewritten = dict(leg)
    rewritten["path"] = f"{rel_prefix}/{target.name}"
    # Normalised and capture-RELATIVE, never the declared string: the declared form is attacker-
    # controlled and was how an absolute host path reached the packaged manifest.
    rewritten["packaged_from"] = origin.resolve().relative_to(source_dir.resolve()).as_posix()
    return _contain_declared_paths(rewritten)


def _state_withheld_bytes(leg: dict[str, Any]) -> dict[str, Any]:
    """Say, in the packaged leg itself, that withheld bytes are NOT in this package.

    ⚠️ Round-4 finding 1, second half: `retained_path` survives packaging while the bytes it names
    deliberately do not, so read against the package the reference dangles. The bytes cannot be
    copied - they are uncertified, and shipping them under `<kind>/data/` is the fail-open #480
    exists to close - and dropping the pointer would lose the operator's only route back to them in
    the capture they still hold. So the reference is KEPT and disambiguated: it is capture-relative,
    and the leg says so rather than leaving a consumer to discover it by a failed open.

    Keyed on `tableau_oracle_manifest`'s own constant, not on a name this module invents, so the
    vocabulary stays owned by the module that writes it.
    """
    if not isinstance(leg.get(tableau_oracle_manifest.RETAINED_PATH_KEY), str):
        return leg
    return {
        **leg,
        "packaging_reason": (
            "uncertified bytes are retained in the CAPTURE and are deliberately not packaged; "
            f"'{tableau_oracle_manifest.RETAINED_PATH_KEY}' is relative to that capture, not to this package"
        ),
    }


def package_oracle(  # pylint: disable=too-many-locals
    views: list[dict[str, Any]], manifest: Any, oracle_dir: Path, dest: Path, unit: str = ""
) -> dict[str, Any]:
    """Copy this unit's renders and numbers into `<dest>`, type-separated, and rewrite the manifest.

    Bytes are copied verbatim, so every `sha256`/`bytes` the capture recorded still verifies -
    `reference_evidence.render_facts` checks exactly those, and a re-encoded copy would be rejected.
    Only `path` changes.

    ⚠️ An UNASSESSABLE data leg is normalised BEFORE anything is copied (#480). `_copy_leg` keys on
    `path`, so a leg that still names uncertified bytes there - which is every such leg in a capture
    written before the rule - would be copied into `<kind>/data/<stem>.csv` and shipped to the
    consumer as numbers. Demoting first means those bytes simply have no `path` to copy, and the
    packaged manifest carries `retained_path` plus the reason instead.
    ⚠️ **Round 6 contains the view at the SOURCE, not at each consumer.** Rounds 3, 4 and 5 each
    contained a value at the reader that had just leaked - a field name, a top-level value, then the
    scoped `oracle-manifest.json` document - and each was followed by a reader nobody had enumerated.
    Measured on the tip of round 5, with `view_luid` and `view_type` declaring host paths::

        oracle-manifest view_luid:                     <refused-by-packager>   # swept
        package-manifest oracle.objects[0].view_luid:  <drive>:\\Users\\...    # built beside it
        handover.md: UNTYPED_RENDER ... luid=<drive>:\\Users\\...              # rendered from it

    So the view is contained ONCE here, before any field of it is read, and `objects`, `omissions`,
    the packaged record, the filename stem and every artifact derived from them are contained BY
    CONSTRUCTION. A consumer added later inherits that rather than needing a sixth guard. Legs keep
    their own diagnosis because :func:`_contain_unsafe_strings` reports an already-refused string as
    refused, so the walk is idempotent and `_copy_leg` still says WHICH field it refused and why.
    """
    views = tableau_oracle_manifest.withhold_uncertified_evidence(views)
    taken: set[str] = set()
    packaged: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []

    for raw_view in views:
        view = _contain_unsafe_strings(raw_view)[0]
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
        # `.get`, not `[...]`: an UNASSESSABLE leg is `status: ok` with NO `path` (#480), and that is
        # exactly the record that must ship no numbers. Subscripting it raised `KeyError` here, which
        # is fail-closed but crashes the packager on a capture it is meant to handle.
        numbers = (
            str(rewritten.get("path"))
            if rewritten is not None and rewritten.get("status") == "ok" and rewritten.get("path") and not reason
            else None
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

    scoped = _scope_oracle_manifest(manifest, packaged, objects, unit)
    write_json(dest / "oracle-manifest.json", scoped)
    return {"objects": objects, "omissions": omissions}


def _scope_oracle_manifest(
    manifest: Any, packaged: list[dict[str, Any]], objects: list[dict[str, Any]], unit: str
) -> dict[str, Any]:
    """The capture manifest rebuilt for ONE unit: allowlisted, and every count RECOMPUTED.

    ⚠️ **Round-2 finding: this was a denylist too** - it copied every manifest key except `views`,
    so a package holding 23 views shipped **22 fields byte-identical to the 360-view estate
    manifest**. `view_count` was rewritten to 23 while `view_types` still totalled 360
    (`dashboard: 60, worksheet: 300`), beside `captured_complete: 312` and `failed: 47`. A consumer
    reading those numbers is reading the estate and being told it is this unit.

    Two different remedies, because the fields fail differently:

    * **counts are RECOMPUTED from the packaged views**, not dropped - `view_count`, `view_types` and
      the per-leg `*_ok` tallies all describe what actually shipped, and the leg tallies are taken
      AFTER copying, so a leg the packager refused is not counted as present.
    * **estate-run and foreign-identity fields are DROPPED** - `elapsed_sec`, `total_retries`,
      `total_reauths` describe the whole capture run; `captured_complete`, `failed`, `data_empty`,
      `credential_blocked`, `reference_missing`, `reference_required` and
      `credential_scrubbed_at_sink` encode capture-time semantics this packager cannot reconstruct
      faithfully, and inventing a per-unit definition under an existing name would be worse than
      omitting it.
    """
    narrowed = dict(manifest if isinstance(manifest, dict) else {})
    # ⚠️ DERIVED here, not merely carried (#480). A capture written by a current run already flags
    # its own views, but an OLDER `oracle-manifest.json` predates the flag entirely -- and that is
    # exactly the input the review's reproduction used: a `status: ok` data leg with no `row_count`,
    # shipped with `flags` absent, which is what a clean capture looks like. The estate-wide COUNTS
    # stay dropped because this packager cannot reconstruct them, but the per-view rule needs no
    # estate context, so it is applied from the SHARED predicate rather than re-implemented.
    narrowed["views"] = tableau_oracle_manifest.flag_empty(packaged)
    scoped, dropped = project(narrowed, ORACLE_MANIFEST_ALLOW)
    # ⚠️ #480 round 5. Containment is a property of the DOCUMENT, not of a leg. `_copy_leg` guards
    # the four legs it copies, so a string the allowlist retains anywhere ELSE - `views[].flags[]`
    # and `views[].view_name` both measured shipping an absolute host path - never met a check at
    # all. Sweeping the scoped document closes every level at once, including any field a future
    # allowlist entry adds, and the refusals are RECORDED rather than silently scrubbed.
    scoped, refused = _contain_unsafe_strings(scoped)

    shipped = scoped.get("views") or []
    counts = dict.fromkeys(KIND_DIRS, 0)
    for obj in objects:
        counts[obj["view_type"]] = counts.get(obj["view_type"], 0) + 1
    scoped["view_count"] = len(shipped)
    scoped["view_types"] = counts
    for leg in (*RENDER_LEGS, "data"):
        scoped[f"{leg}_ok"] = sum(
            1 for view in shipped if isinstance(view.get(leg), dict) and view[leg].get("status") == "ok"
        )
    stamped = stamp_scope(scoped, unit, dropped, "oracle-manifest.json views filtered to this unit, counts recomputed")
    if refused:
        stamped["scope"]["refused_fields"] = sorted(refused)
    return stamped


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
| `handover/{unit}.json` | the engine's slice for THIS workbook; `python scripts/read_handover.py handover/{unit}.json --viz`. The estate-wide `estate` section is not shipped, and any absolute host path in it is redacted. |
| `fabric/` | the engine WORKING COPY - edit here. The pristine baseline stays in `<bundle>/reports/`. |
| `assets/` | the Tableau source this was built from |
| `migration-spec.json` | the parsed source; the expected page set both gates grade against |
| `oracle/` | this unit's Tableau reference, split `dashboard/` vs `worksheet/` vs `unknown/` (**singular** - the directory is the object kind, not a plural) |
| `report.json` | **gate input, and readable.** The engine's own classification of THIS unit - workbook vs datasource - which is what earns a datasource-only unit its `NOT_APPLICABLE` instead of a finding. Scoped: it names this unit and no other. |
| `source-provenance.json` | **gate input.** The only trusted route from this package's asset to a Tableau workbook LUID, keyed by the asset's sha256. Read `origin.match` before trusting a render: `sha256` means local and server bytes agree, `name_only` means they DIFFER. Descoped to those three fields; an entry is shipped only when attribution was NOT refused - see `scope.suppressed_reason`. |
| `engine-output-receipt.json` | **read `engine.version` when a result looks wrong.** It establishes which engine built this, so version drift stays checkable months later. Descoped to that: `artifacts[]` and the engine's installation paths are not shipped, because nothing reads them here. |
| `package-manifest.json` | what was packaged, and every omission with its reason |

`oracle/` is **layout/text grade only**: an oracle capture is taken in the view's default state with
no `?vf_` filter pinning, so a visual PASS signed off on it alone is overstated. Renders present here
are not a claim of byte-faithfulness either - see `ORACLE_ATTRIBUTION ... match=` in `handover.md`,
and record the ceiling in `limitations_encountered`.

⚠️ **A PNG and an SVG of the same object are DIFFERENT EVIDENCE, not duplicates - do not pick one.**

* the **`.png` is the visual oracle**: the only leg an agent can actually look at to judge layout,
  colour and chart type.
* the **`.svg` is a greppable DATA oracle**: its `<text>` elements carry the real label and value
  strings, so exact figures are readable with no OCR and no judgement. Measured on this estate's
  `HR | Summary` dashboard - **122 `<text>` elements**, including `Human Resources Dashboard`,
  `Active Employees` and `7,984`.
* ⚠️ but an SVG is not universally a data oracle: a chart whose labels render as paths carries
  **zero** `<text>` elements. Measured on the same workbook, **four** of its worksheets do -
  `Hired By Year`, `Terminated By Year`, `Age Groups` and `Education Levels`. Absence of text is not
  absence of content - fall back to the PNG.
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


def _attach_oracle(oracle_dir: Path | None, identity: dict[str, Any], dest: Path, unit: str = "") -> dict[str, Any]:
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
    oracle.update(package_oracle(views, manifest, oracle_dir, dest / "oracle", unit))
    oracle["route"] = route
    return oracle


def _handover_workbook(handover: Any, unit: str, dest: Path) -> dict[str, Any] | None:
    """The workbook payload inside a handover slice, via read_handover's own resolver."""
    if not isinstance(handover, dict):
        return None
    found = read_handover._workbooks_from_payload(handover, dest)  # pylint: disable=protected-access
    return next((wb for name, wb, _ in found if name == unit), found[0][1] if found else None)


def package_unit(
    bundle: Path, unit: str, out_root: Path, *, oracle_dir: Path | None, assets_dir: Path | None
) -> dict[str, Any]:
    """Assemble one unit's package. Returns the record written to `package-manifest.json`."""
    staging = out_root / f".{sanitize_staging_name(unit)}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        result = _assemble_unit(bundle, unit, staging, oracle_dir=oracle_dir, assets_dir=assets_dir)
        replace_dir(staging, out_root / unit)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return result


def sanitize_staging_name(unit: str) -> str:
    """A filesystem-safe stem for this unit's staging directory."""
    return _UNSAFE.sub("_", unit)[:_MAX_OBJECT_NAME] or "unit"


def replace_dir(staged: Path, final: Path) -> None:
    """Put ``staged`` at ``final``, REPLACING whatever was there - never merging into it.

    ⚠️ **Round-2 blocker: packaging used to merge into an existing `<out>/<unit>`**, because every
    write was `mkdir(exist_ok=True)` / `copytree(dirs_exist_ok=True)` and nothing ever removed a file
    the new input no longer produced. Reproduced end to end: package a unit with a 4-view oracle
    (entry gate READY, 4 ready / 0 blind), then re-run the documented CLI into the SAME `--out` with
    an EMPTY oracle directory. Both runs exit 0, the new `package-manifest.json` correctly reports
    zero oracle objects and `"no oracle-manifest.json found"` - and the PREVIOUS
    `oracle/oracle-manifest.json` survives, so the entry gate still returns **READY, 4 ready / 0
    blind**. An agent then builds against evidence that no longer exists, with a gate agreeing.
    Stale `assets/` and `fabric/` files persisted the same way.

    Replace-not-merge is the fix, staged so a crash mid-build cannot leave a half-package in place of
    a good one. The retired directory is moved aside before the swap and only deleted once the new
    one has landed, and it is restored if the rename fails - on Windows a directory rename onto an
    existing target fails outright, so the move-aside is required rather than defensive.
    """
    final.parent.mkdir(parents=True, exist_ok=True)
    if not final.exists():
        _rename_retrying(staged, final)
        return
    retired = final.with_name(f".{final.name}.replaced")
    shutil.rmtree(retired, ignore_errors=True)
    _rename_retrying(final, retired)
    try:
        _rename_retrying(staged, final)
    except OSError:
        _rename_retrying(retired, final)
        raise
    shutil.rmtree(retired, ignore_errors=True)


#: Windows denies a directory rename while anything still holds a handle inside it, and a scanner
#: routinely does for a moment after a large write. Measured: renaming a freshly-assembled
#: `HR_Dashboard` staging tree (337 entries, 51 MB of renders) failed `WinError 5` once and succeeded
#: on the first attempt when retried. So this is a race, not a defect - but it is one a user would
#: hit, so it is retried on a BOUNDED budget (2 s) rather than either ignored or waited on forever.
_SWAP_ATTEMPTS = 10
_SWAP_BACKOFF_SEC = 0.2


def _rename_retrying(src: Path, dst: Path) -> None:
    """Rename, retrying a transient Windows lock on a bounded budget before giving up."""
    for attempt in range(1, _SWAP_ATTEMPTS + 1):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt == _SWAP_ATTEMPTS:
                raise
            time.sleep(_SWAP_BACKOFF_SEC)


def _assemble_unit(  # pylint: disable=too-many-locals
    bundle: Path, unit: str, dest: Path, *, oracle_dir: Path | None, assets_dir: Path | None
) -> dict[str, Any]:
    """Build one unit's package into ``dest``, which is always a fresh, empty directory."""
    engine_report = read_json(bundle / "report.json")
    workbooks, datasources = engine_unit_names(engine_report)
    dest.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    report_name, model_name = _copy_fabric(bundle, unit, dest)
    if report_name is None and model_name is None:
        notes.append(f"no engine working copy at pbip/{unit} - nothing to build on")

    handover = read_json(bundle / "handover" / f"{unit}.json")
    (dest / "handover").mkdir(parents=True, exist_ok=True)
    redactions: list[str] = []
    if isinstance(handover, dict):
        cleaned, redactions = scope_handover(handover, unit)
        write_json(dest / "handover" / f"{unit}.json", cleaned)
        handover = cleaned
    else:
        notes.append(f"no handover slice at handover/{unit}.json")

    asset, asset_route = resolve_asset(bundle, unit, handover, assets_dir)
    if asset is not None:
        (dest / "assets").mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset, dest / "assets" / asset.name)
        asset = dest / "assets" / asset.name
    else:
        notes.append(f"source asset unresolved ({asset_route}); both gates will report CANNOT_ESTABLISH")

    _payload, entries = scope_provenance(read_json(bundle / "source-provenance.json"), sha256_of(asset))
    # #480 round 6, the SECOND untrusted document, contained at its own intake for the same reason as
    # the oracle capture. Its `origin.workbook_luid` is server-supplied, and three consumers read it
    # before anything sweeps it: `shippable_provenance` ships it, `render_handover` interpolates it
    # into `ORACLE_ATTRIBUTION luid=`, and `workbook_identity`'s conflict diagnosis quotes it INTO a
    # sentence - which `absolute_host_paths` cannot see, because the value is no longer a value.
    # Measured before this line existed: `scope.suppressed_reason` and `handover.md` both carried
    # `<drive>:\Users\<account>\private\leak.log`. Containing at intake means every one of them is
    # fed a contained value, exactly as `package_oracle` is.
    entries = _contain_unsafe_strings(entries)[0]
    identity = workbook_identity(entries, asset)
    write_json(dest / "source-provenance.json", shippable_provenance(entries, identity, unit))
    write_json(dest / "report.json", scope_report(engine_report, unit))
    receipt = scope_receipt(read_json(bundle / "engine-output-receipt.json"), unit)
    if receipt is not None:
        write_json(dest / "engine-output-receipt.json", receipt)

    oracle = _attach_oracle(oracle_dir, identity, dest, unit)
    spec, spec_note = _write_spec(asset, dest)
    if spec_note:
        notes.append(spec_note)
    if redactions:
        notes.append(
            f"redacted {len(redactions)} absolute host path(s) from the handover slice: {', '.join(redactions[:5])}"
        )

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
