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
| 3 | at least one requested unit already has a package carrying EDITS, and this packager replaces a
      package whole. Those units were left untouched; every other requested unit was still packaged.
      `--discard-package-edits` overwrites them deliberately. |

An omission INSIDE a package is not exit 1: a unit whose oracle genuinely has no render for a page is
the negative control, and it must package successfully and still report that page BLIND.
"""

from __future__ import annotations

# The assembler, its scoping helpers and the CLI intentionally live together: the module IS the
# packaging contract, and the numbered join table above only reads as one document while the code it
# describes is one file. Extracting the data-source localizer (#461) into a sibling module was
# considered and rejected on the same grounds - it is a step of the assembly, not a separate concern
# like `manifest_scope.py`'s allowlists.
# pylint: disable=too-many-lines

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import read_handover  # noqa: E402  # pylint: disable=wrong-import-position
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

#: The migration-spec CONTRACT, shipped INTO each package rather than described in its README.
#: Measured on the 2026-09-03 cold run: an agent given nothing but a package invented a plausible
#: `limitations_encountered` shape (`{id, category, objects, detail, owner, status}`) and
#: `validate_spec.py` rejected all six entries, because the real item is exactly
#: `item`/`issue`/`severity`/`stage` under `additionalProperties: false`. Learning that cost a trip
#: outside the package. Prose restating a schema is a copy that drifts; the schema itself cannot.
SPEC_SCHEMA = SCRIPT_DIR.parent / "docs" / "migration-spec.schema.json"

#: Every quoted ABSOLUTE path literal in a `.tmdl`, and the two enclosing shapes that carry one.
#: Scanning for `File.Contents` alone closed less than half of issue #461: re-measured across the 67
#: packaged units of estate run 408, `File.Contents` accounts for 22 of the 31 Windows/UNC literals,
#: and the other 9 are a FOLDER PARAMETER - `expression SourceFolder = "<bundle>\pbip\<Unit>\
#: <Unit>.Data"` with partitions doing `File.Contents(#"SourceFolder" & "\Sample - Superstore.xlsx")`.
#: Those 9 are every datasource-only unit in the estate, which the narrower scan missed entirely
#: (17 units -> 25). The defect is "an absolute path escaping the package", not "a `File.Contents`
#: call", so the general shape is what is targeted.
ABSOLUTE_LITERAL_RE = re.compile(r'"((?:[A-Za-z]:[\\/]|\\\\|/)[^"]*)"')
FILE_CONTENTS_RE = re.compile(r'File\.Contents\(\s*"([^"]*)"\s*\)')
FOLDER_PARAM_RE = re.compile(r'(?P<prefix>expression\s+(?P<name>#"[^"]+"|[^\s=]+)\s*=\s*")(?P<value>[^"]*)(?P<quote>")')
EXPRESSION_NAME_RE = re.compile(r'expression\s+(#"[^"]+"|[^\s=]+)\s*=')
WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
UNC_PATH_RE = re.compile(r"^\\\\")

#: M record fields whose value is a SERVICE ROUTE and can never be a file-system path, so a literal
#: found in one is definitively a non-path rather than something this packager cannot classify.
#:
#: Only `HttpPath` is listed, because it is the only such field MEASURED in the estate: run 408's
#: `"/sql/1.0/warehouses/<id>"` in three units is a Databricks SQL-warehouse endpoint, passed as
#: `Databricks.Catalogs(server, httpPath, ...)` / `[HttpPath="..."]`. Shape alone cannot tell it from
#: `/mnt/lake/warehouse`, and that is the point of the distinction: the ROLE can. Adding a field here
#: on speculation would widen the silent bucket, which is the defect class this module is built
#: against - so a new entry needs a literal that was actually observed in a packaged model.
SERVICE_ROUTE_RE = re.compile(r'\bHttpPath\s*=\s*"([^"]*)"', re.IGNORECASE)

#: The three verdicts :func:`_path_verdict` may return. The third one is the point: a literal this
#: packager cannot classify is neither shipped nor cleared, and collapsing it into "not a path" is
#: how `SourceFolder = "/Users/<person>/Data/"` survived packaging unchanged with NO omission
#: recorded at all (blind-review finding 5). Unassessable input gets its own bucket and its own
#: recorded reason; it never joins the clean one.
PATH_LITERAL = "path"
NOT_A_PATH = "not-a-path"
UNCLASSIFIED = "unclassified"
UNCLASSIFIED_REASON = (
    "could not be classified as a file-system path or as a non-path, so it was neither shipped "
    "nor cleared - check it by hand"
)

#: How a folder PARAMETER is read by the model, which decides what may be copied out of the folder
#: it names. Anything other than the first two is a refusal - see :func:`_parameter_usages`.
NAMED_FILES = "named-files"
WHOLE_FOLDER = "whole-folder"
UNKNOWN_USAGE = "unknown-usage"
NO_USAGE = "no-usage"

#: Where a shipped source lands inside the package, and the M parameter a rewritten `File.Contents`
#: literal reads its folder from. Both the parameter's `meta [...]` tail and the trailing-separator
#: value shape are copied from this repo's OWN committed, Desktop-verified models
#: (`examples/*/fabric/*.SemanticModel/definition/expressions.tmdl`) rather than invented.
#:
#: The name matters twice over. `File.Contents` does **not** accept a relative path - Power Query
#: rejects it outright, so "rewrite it to a relative path" would produce a model that refreshes
#: NOWHERE, which is worse than one that refreshes on a single machine. A parameter is the
#: documented workaround. And `DataFolder` is one of the two names `scripts/set_data_folder.py`
#: already localizes, sanitizes and CI-gates, so a unit promoted to `migrations/<slug>/fabric/` is
#: covered by the existing privacy gate with no new code and no new script.
DATA_DIR = "data"
DATA_FOLDER_PARAM = "DataFolder"
FALLBACK_DATA_FOLDER_PARAM = "PackageDataFolder"
EXPRESSIONS_TMDL = "expressions.tmdl"
#: The name of the manifest that records what a package contains, INCLUDING the per-file digest that
#: makes an agent's edit to the canonical `fabric/` tree detectable on the next run. Excluded from
#: its own digest, because it is written last and would otherwise never match itself.
MANIFEST_NAME = "package-manifest.json"

#: Refuse to copy a single source larger than this, rather than silently turning a handover folder
#: into a data lake. Measured on estate run 408 the largest referenced extract is 1.33 MB and the
#: whole estate is 11.2 MB, so nothing observed comes close - the ceiling exists so that an
#: unbounded case becomes a LOUD, recorded omission instead of an unnoticed multi-GB copy.
MAX_DATA_BYTES = 256 * 1024 * 1024

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


class PackagingError(RuntimeError):
    """An invariant this packager holds was violated, so nothing is shipped rather than something wrong.

    Every use is a TRIPWIRE behind a rule that already prevents the condition - two sources landing
    on one packaged path, or a parameter declared twice. Prevention is the fix; this exists so that
    a future edit which re-opens one fails loudly at packaging time instead of shipping a package
    whose partitions read another table's rows or whose model AMO refuses to load.
    """


class PackageEditsRefused(PackagingError):
    """Repackaging would discard edits made in the package - the tree this packager calls canonical.

    Carries the changed paths (or the reason they could not be established) so the CLI can name them
    rather than saying "something changed".
    """

    def __init__(self, unit: str, package: Path, changed: list[str], reason: str | None) -> None:
        self.unit, self.package, self.changed, self.reason = unit, package, changed, reason
        detail = reason or (
            f"{len(changed)} file(s) differ from what packaging wrote: {', '.join(changed[:5])}"
            + (" ..." if len(changed) > 5 else "")
        )
        super().__init__(
            f"refusing to repackage {unit}: {package} is the canonical place to edit, and {detail}. "
            "Re-run with --discard-package-edits to overwrite it, or move the package aside first."
        )


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
    if candidate.is_absolute() or candidate.drive or declared.startswith(("\\\\", "/")):
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
    """`(rewritten leg, omission reason)` for one render or data leg."""
    if not isinstance(leg, dict):
        return None, None
    if leg.get("status") != "ok" or not isinstance(leg.get("path"), str):
        return dict(leg), None
    origin, refusal = _resolve_capture_file(source_dir, leg["path"])
    if origin is None:
        rewritten = dict(leg)
        rewritten["status"] = OMITTED_STATUS
        rewritten["packaging_reason"] = refusal or "capture path unusable"
        rewritten["path"] = REFUSED_PATH
        return rewritten, rewritten["packaging_reason"]
    destination = dest_dir / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origin, destination)
    rewritten = dict(leg)
    rewritten["path"] = f"{rel_prefix}/{target.name}"
    # Normalised and capture-RELATIVE, never the declared string: the declared form is attacker-
    # controlled and was how an absolute host path reached the packaged manifest.
    rewritten["packaged_from"] = origin.resolve().relative_to(source_dir.resolve()).as_posix()
    return rewritten, None


def package_oracle(  # pylint: disable=too-many-locals
    views: list[dict[str, Any]], manifest: Any, oracle_dir: Path, dest: Path, unit: str = ""
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
    narrowed["views"] = packaged
    scoped, dropped = project(narrowed, ORACLE_MANIFEST_ALLOW)

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
    return stamp_scope(scoped, unit, dropped, "oracle-manifest.json views filtered to this unit, counts recomputed")


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

Self-contained handover package for one migration unit ({kind}). Both entry and exit gates take
THIS FOLDER'S PATH as their only argument - a bare unit name is an argparse error (exit 2), never a
verdict:

    python scripts/check_reference_readiness.py <path-to-this-folder>
    python scripts/check_unit.py <path-to-this-folder>

A page counts as REBUILT only when its `displayName` EXACTLY equals an expected object's name AND it
ships at least one visual; one that pairs by name with no visual is reported `blank` and FAILS. The
expected set is every dashboard PLUS every worksheet not placed on one.

| path | what it is |
|---|---|
| `handover.md` | every engine finding, one per line, emptied visuals first. **Start here.** |
| `handover/{unit}.json` | the engine's slice for THIS workbook; `python scripts/read_handover.py handover/{unit}.json --viz`. Estate-wide sections are not shipped; absolute host paths are redacted. |
| `fabric/` | the engine WORKING COPY - **edit here**, and when you work from a package THIS tree is canonical; `<bundle>/pbip/` never promotes over it. Re-running `package_unit.py` into this folder REFUSES (exit 3) rather than discarding what you changed - `--discard-package-edits` overrides. Declared-edit tooling (`declare_generated_edit.py`, `--tamper`) is bundle-only. |
| `assets/` | the Tableau source this was built from |
| `data/` | the rows the model imports, shipped with it (#461). A folder PARAMETER in `expressions.tmdl` names this directory by ABSOLUTE path, because Power Query rejects a relative one - so **moving this folder breaks refresh until you re-point it**: `python scripts/set_data_folder.py --package <path-to-this-folder>` rewrites every such parameter to this package's own `data/` and fails if the directory it then names does not exist. Absent when nothing was shipped - either the model imports nothing, or every source it names was unavailable when this was packaged; `package-manifest.json`'s `data_sources.omissions` says which, one line per source, and `handover.md` repeats it as a `PACKAGE_NOTE`. |
| `migration-spec.json` | the parsed source; the expected page set both gates grade against |
| `migration-spec.schema.json` | the CONTRACT `validate_spec.py` enforces. Read it before appending a `limitations_encountered` entry: exactly `item`/`issue`/`severity`/`stage`, `additionalProperties: false`, so one invented field rejects every entry. |
| `oracle/` | this unit's Tableau reference, split `dashboard/` vs `worksheet/` vs `unknown/` (**singular** - the directory is the object kind, not a plural). **`oracle/*/data/*.csv` is the NUMERIC oracle** - exact labels and figures, no OCR and no judgement. Read it first. |
| `report.json` | **gate input, and readable.** The engine's classification of THIS unit - workbook vs datasource - which is what earns a datasource-only unit `NOT_APPLICABLE` instead of a finding. Scoped to this unit. |
| `source-provenance.json` | **gate input.** The only trusted route from this package's asset to a Tableau workbook LUID, keyed by the asset's sha256; `origin.match` decides whether a render can be trusted - see UNFIXABLE below. An entry ships only when attribution was NOT refused (`scope.suppressed_reason`). |
| `engine-output-receipt.json` | **read `engine.version` when a result looks wrong** - it establishes which engine built this, so version drift stays checkable months later. Install paths are not shipped. |
| `package-manifest.json` | what was packaged, and every omission with its reason. Its `contents.files` digest is how a re-run knows this package has been edited and refuses to overwrite it. |

`oracle/` images are **layout/text grade only**: a capture is taken in the view's default state with
no `?vf_` filter pinning, so a visual PASS signed off on one alone is overstated, and it is no claim
of byte-faithfulness - see `ORACLE_ATTRIBUTION ... match=` in `handover.md`, and log the ceiling in
`limitations_encountered`. The `.png` is the only leg you can LOOK at; the `.svg` carries labels and
values as greppable `<text>` elements, except where labels render as paths - zero text is not zero
content.

## UNFIXABLE FROM THIS PACKAGE

`source-provenance.json` can report `origin.match: "name_only"` - local and server bytes may DIFFER,
so an oracle render may depict a different build than `assets/`. Re-stamping needs
`stamp_tableau_provenance.py`, Tableau Server credentials AND the fields `scope.dropped_fields`
strips here: `origin.remote_sha256`, `origin.server`, `origin.site`. Measured consequence: every
emitted page then reads `UNVERIFIABLE - REVISION NOT ESTABLISHED`, so `check_reference_readiness.py`
can NEVER exit 0 from this package alone. Log it and build anyway.
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


def _model_tmdl(dest: Path, model_name: str | None) -> list[Path]:
    """Every `.tmdl` document of the packaged model, or an empty list when there is no model."""
    if not model_name:
        return []
    definition = dest / "fabric" / model_name / "definition"
    return sorted(definition.rglob("*.tmdl")) if definition.is_dir() else []


def _packaged_data_target(source: str, taken: dict[str, str], *, keep_leaf_only: bool = False) -> str:
    """A stable, package-relative home for one referenced source, unique within this package.

    Readable first - `data/<parent folder>/<file name>` keeps a handover folder browsable - but two
    different sources can share both, and the engine's extract paths are exactly that shape
    (`.../<table>/federated_<hash>/Extract_Extract.csv`). A collision is therefore resolved by
    digesting the FULL original path, never by overwriting: two sources landing on one file would
    silently repoint one partition at the other's rows.

    ``keep_leaf_only`` is the folder-parameter case, where the literal already names a directory and
    its own leaf is the meaningful name (`<Unit>.Data`).

    ⚠️ **Uniqueness is judged over the DESTINATION TREE, not over the reservation string** - a
    folder claims everything beneath it. Blind-review finding 1: a folder source containing
    `same/x.csv` reserved `same`, a bare file whose readable home was `same/x.csv` reserved
    `same/x.csv`, the two strings differed so neither looked taken, and the second copy overwrote
    the first on disk. The package then exited 0 with TWO manifest entries for one path and one
    partition reading another table's rows - which `check_datamodel.py` cannot see, because the
    model is structurally perfect. Comparing whole strings closed the same-shape case only; the
    ancestor test closes every combination of the two shapes.
    """
    original = PurePosixPath(source.replace("\\", "/").rstrip("/"))
    name = _UNSAFE.sub("_", original.name)[:_MAX_OBJECT_NAME] or "data"
    if keep_leaf_only:
        candidate = name
    else:
        parent = _UNSAFE.sub("_", original.parent.name)[:_MAX_OBJECT_NAME] or "source"
        candidate = f"{parent}/{name}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    width = 8
    while _destination_taken(candidate, source, taken):
        if width > len(digest):
            raise PackagingError(f"no free packaged destination for {_leaf(source)}")
        candidate = f"{digest[:width]}/{name}"
        width += 8
    taken[candidate] = source
    return candidate


def _destination_taken(candidate: str, source: str, taken: dict[str, str]) -> bool:
    """Whether ``candidate`` collides with a destination another source already claimed.

    A collision is not only an equal path: a FOLDER destination owns its whole subtree, so
    `same/x.csv` collides with `same` in both directions. Compared case-insensitively because the
    package is routinely assembled and opened on Windows, where `Same/X.csv` and `same/x.csv` are
    one file - a case-sensitive dict would have called them distinct and let one overwrite the other.
    """
    key = candidate.casefold()
    for existing, owner in taken.items():
        if owner == source:
            continue
        claimed = existing.casefold()
        if key == claimed or key.startswith(f"{claimed}/") or claimed.startswith(f"{key}/"):
            return True
    return False


def _assert_distinct_destinations(record: dict[str, Any]) -> None:
    """Tripwire: two shipped entries naming one path means a partition reads another table's rows.

    :func:`_packaged_data_target` makes this unreachable. It is asserted anyway because the failure
    it guards is invisible downstream - the model loads, `check_datamodel.py` exits 0, and only the
    NUMBERS are wrong - so a regression must stop packaging rather than be discovered by a customer.
    """
    seen: list[str] = [row["path"].casefold() for row in record["shipped"]]
    duplicates = sorted({row["path"] for row in record["shipped"] if seen.count(row["path"].casefold()) > 1})
    if duplicates:
        raise PackagingError(
            "two sources were packaged onto the same destination, so a partition would read another "
            f"table's rows: {', '.join(duplicates)}"
        )


def _path_verdict(value: str) -> str:
    """`PATH_LITERAL` / `NOT_A_PATH` / `UNCLASSIFIED` for one absolute quoted literal.

    A Windows drive or UNC prefix is unambiguous. A POSIX-absolute literal mostly is NOT: measured on
    estate run 408, 9 POSIX-absolute literals appear and 8 are false positives - a Databricks
    `HttpPath = "/sql/1.0/warehouses/<id>"` in three units, and a bare `"/"` inside a
    `TableauFormula` annotation in two more. A file suffix keeps the one genuine hit (a macOS `.xlsx`
    baked into a source workbook); a TRAILING SEPARATOR is the directory convention and keeps the
    other genuine shape, a folder parameter such as `"/Users/<person>/Data/"`.

    ⚠️ **What is left over is UNCLASSIFIED, never clean.** Requiring a suffix and calling everything
    else "not a path" is what let `SourceFolder = "/Users/<person>/Data/"` through packaging
    unchanged, with no folder shipped and NO omission recorded (blind-review finding 5). The
    remaining shape - POSIX-absolute, no suffix, no trailing separator - genuinely cannot be told
    from a service path without probing, so it gets its own verdict and its own recorded reason.

    ⚠️ **This answers the question from the STRING ALONE, so it can only ever return UNCLASSIFIED
    for the Databricks shape above.** The role a literal plays in the surrounding M is a second,
    stronger source of evidence, and callers that have the document text consult it first - see
    :data:`SERVICE_ROUTE_RE` and :func:`_service_routes`. Do not fold that into this function by
    pattern-matching `/sql/`: what makes the endpoint a non-path is the field it is assigned to,
    not the letters in it.
    """
    if WINDOWS_PATH_RE.match(value) or UNC_PATH_RE.match(value):
        return PATH_LITERAL
    if not value.startswith("/"):
        return NOT_A_PATH
    if value.strip() == "/":
        return NOT_A_PATH
    if PurePosixPath(value.rstrip("/")).suffix or value.endswith("/"):
        return PATH_LITERAL
    return UNCLASSIFIED


def _is_path_literal(value: str) -> bool:
    """Whether an absolute literal is a FILE SYSTEM path this packager should ACT on.

    ⚠️ **False here means "do not act on it", NOT "definitely not a path".** It collapses
    :data:`NOT_A_PATH` and :data:`UNCLASSIFIED` into one answer because the two callers that use it
    both ask the same narrow question - "should I ship this?" - and the answer is no either way. A
    caller that has to distinguish *reported* from *silent* must call :func:`_path_verdict`; reading
    a False here as a clean bill of health is exactly how the unassessable bucket gets emptied.
    """
    return _path_verdict(value) == PATH_LITERAL


def _service_routes(text: str) -> set[str]:
    """Literals in ``text`` whose ROLE proves they are not file-system paths.

    Shape-only classification cannot separate a Databricks warehouse endpoint from a mount point:
    `"/sql/1.0/warehouses/<id>"` and `"/mnt/lake/warehouse"` are the same string shape, so
    :func:`_path_verdict` returns UNCLASSIFIED for both, and the packager would ask a human to check
    an endpoint by hand in three of estate run 408's units. The field it is assigned to settles it -
    `HttpPath` takes a route and cannot take a path - so this is evidence, not a guess, and it is
    read from the document the literal actually lives in.
    """
    return {match.group(1) for match in SERVICE_ROUTE_RE.finditer(text)}


def _inside(root: Path, value: str) -> bool:
    """Whether an absolute literal points INSIDE ``root``, judged LEXICALLY.

    ⚠️ **Never `Path.resolve()` one of these literals.** A UNC literal naming a host that does not
    exist blocks on SMB name resolution: measured by PR #462, that took one test module from 30
    seconds to **52 minutes** and starved a subprocess into its 600 s timeout. `normpath` answers the
    containment question without touching the network, and containment is a question about the
    STRING, not about what happens to be mounted.
    """
    try:
        candidate = PureWindowsPath(os.path.normpath(value))
        return candidate == PureWindowsPath(os.path.normpath(str(root))) or candidate.is_relative_to(
            PureWindowsPath(os.path.normpath(str(root)))
        )
    except (TypeError, ValueError):
        return False


def _ceiling_refusal(size: int) -> str | None:
    """The refusal for a source over the package ceiling, or None. ONE comparison site on purpose.

    Both shapes - a bare file and the selected members of a folder - are measured against it here, so
    the ceiling cannot be enforced for one and forgotten for the other.
    """
    if size > MAX_DATA_BYTES:
        return f"{size / 1048576:.1f} MB exceeds the {MAX_DATA_BYTES / 1048576:.0f} MB package ceiling"
    return None


def _classify_source(value: str, *, expect_dir: bool = False) -> tuple[Path | None, str | None]:
    """`(readable path, refusal)` for one absolute literal, WITHOUT ever probing a UNC host.

    The UNC carve-out is the same hazard as :func:`_inside`: `Path.is_file()` on `\\\\nowhere\\share`
    blocks on SMB name resolution for minutes, and packaging must not be able to hang. A UNC source
    is therefore refused unprobed and recorded, which is loud and instant; the promotion gate
    (`promote_unit.py`, exit 5) refuses such a model anyway.

    A directory is only checked for EXISTENCE here. Its size is measured over the members a
    partition actually names (:func:`_relocate_folder`), because those are the only bytes the
    package ships - weighing the whole tree would refuse a 300 MB folder for one 4 KB CSV.
    """
    if UNC_PATH_RE.match(value):
        return None, "a UNC path is not probed, because resolving an absent host can block for minutes"
    path = Path(value)
    if expect_dir:
        if not path.is_dir():
            return None, "the folder it names is not present on the packaging machine"
        return path, None
    if not path.is_file():
        return None, "not present on the packaging machine, so its bytes could not be shipped"
    refusal = _ceiling_refusal(path.stat().st_size)
    return (None, refusal) if refusal else (path, None)


def _declared_expressions(documents: list[Path]) -> set[str]:
    """Every M expression name the model already declares, case-folded and unquoted."""
    return {
        _bare_name(match.group(1))
        for document in documents
        for match in EXPRESSION_NAME_RE.finditer(document.read_text(encoding="utf-8"))
    }


def _bare_name(token: str) -> str:
    """`#"Source Folder"` / `SourceFolder` -> the identifier itself."""
    token = token.strip()
    return token[2:-1] if token.startswith('#"') and token.endswith('"') else token


def _data_folder_param(documents: list[Path]) -> str:
    """A parameter name the model does NOT already declare.

    ⚠️ **Both preferred names can be taken, and the old check could not even see one of them.**
    Blind-review finding 4: a model already declaring `DataFolder` AND `PackageDataFolder` loads
    fine (`check_datamodel.py` exit 0), packaging appended a SECOND `PackageDataFolder`, exited 0,
    and AMO then refused the model - packaging turned a loadable model into an unloadable one. The
    substring test made it worse than it looks: `"DataFolder" in text` is TRUE for a model that
    declares only `PackageDataFolder`, so that model got a duplicate too. Names are now read as
    DECLARATIONS and the fallback is numbered, so a free name always exists.
    """
    declared = {name.casefold() for name in _declared_expressions(documents)}
    for candidate in (DATA_FOLDER_PARAM, FALLBACK_DATA_FOLDER_PARAM):
        if candidate.casefold() not in declared:
            return candidate
    suffix = 2
    while f"{FALLBACK_DATA_FOLDER_PARAM}{suffix}".casefold() in declared:
        suffix += 1
    return f"{FALLBACK_DATA_FOLDER_PARAM}{suffix}"


def _write_data_folder_expression(dest: Path, final: Path, model_name: str, parameter: str) -> None:
    """Declare the folder parameter, in the exact shape this repo's committed models already use.

    ⚠️ The value is the FINAL package path, not ``dest``. Assembly runs in a
    ``.<unit>.staging`` directory that `replace_dir` renames afterwards, so writing ``dest`` here
    bakes a path that stops existing the moment packaging succeeds - and it would still LOOK right
    in the file.

    Appended rather than overwritten: `expressions.tmdl` is a list of expression objects, and an
    engine model that grows one later must not have it silently replaced. The `lineageTag` is a
    uuid5 of the model and parameter names so repackaging the same unit is byte-stable.
    """
    path = dest / "fabric" / model_name / "definition" / EXPRESSIONS_TMDL
    lineage = uuid.uuid5(uuid.NAMESPACE_URL, f"package_unit:{model_name}:{parameter}")
    block = (
        f'expression {parameter} = "{final}\\{DATA_DIR}\\" '
        'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n'
        f"\tlineageTag: {lineage}\n\n"
        "\tannotation PBI_ResultType = Text\n\n"
    )
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    separator = "\n" if existing and not existing.endswith("\n\n") else ""
    path.write_text(existing + separator + block, encoding="utf-8")


def _localize_data_sources(dest: Path, final: Path, model_name: str | None) -> dict[str, Any]:
    """Ship every externally-referenced source INTO the package and repoint the model at it (#461).

    The defect: `_copy_fabric` copies the engine's working copy verbatim, and the engine writes
    absolute machine-local paths into the model, pointing back into the originating bundle. So a
    "self-contained" package (#446) carried none of its own rows, opened empty in Desktop with no
    `.pbi/cache.abf` to fall back on, could not refresh on any other machine, and embedded a real
    username in a public repo's deliverable-to-be.

    TWO shapes, because one of them is every datasource-only unit in the estate (see
    :data:`ABSOLUTE_LITERAL_RE`): a bare `File.Contents("<file>")`, and a folder PARAMETER whose
    value is a directory the partitions concatenate a file name onto. They are repaired
    differently - the first is repointed at a new parameter, the second keeps its own parameter and
    has only its VALUE moved - but both end with every literal resolving inside the package.

    What this does NOT do, deliberately: rewrite a literal to a relative path. Power Query rejects a
    relative `File.Contents` argument outright, so that would produce a model refreshing NOWHERE -
    strictly worse than one refreshing on a single machine. A folder PARAMETER is the documented
    workaround and is already this repo's committed convention; see :data:`DATA_FOLDER_PARAM`.

    Every reference ends in exactly one of two recorded states - shipped, or an omission naming its
    reason. There is no third, silent one: a source that cannot be copied keeps its original literal
    (so it still resolves wherever it did before) and is reported. That rule now covers the literals
    this packager cannot even CLASSIFY (:data:`UNCLASSIFIED_REASON`), which used to be the silent
    third state. Findings carry the LEAF name only, never the absolute path, which embeds a username
    in a public repo (convention adopted from #462).
    """
    record: dict[str, Any] = {"parameter": None, "shipped": [], "omissions": [], "bytes": 0}
    documents = _model_tmdl(dest, model_name)
    if not documents:
        return record
    taken: dict[str, str] = {}
    accounted: set[str] = set()
    _localize_folder_parameters(documents, dest, final, record, taken, accounted)
    _localize_file_literals(documents, dest, final, record, taken, model_name, accounted)
    record["omissions"].extend(_external_after_rewrite(_model_tmdl(dest, model_name), final, accounted))
    _assert_distinct_destinations(record)
    return record


def _localize_folder_parameters(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    documents: list[Path],
    dest: Path,
    final: Path,
    record: dict[str, Any],
    taken: dict[str, str],
    accounted: set[str],
) -> None:
    """Move a folder parameter's VALUE into the package, carrying the files a partition NAMES.

    Measured shape, 9 of the estate's 31 external literals and every datasource-only unit::

        expression SourceFolder = "<bundle>\\pbip\\<Unit>\\<Unit>.Data" meta [IsParameterQuery=...]
        ... File.Contents(#"SourceFolder" & "\\Sample - Superstore.xlsx")

    The parameter is reused rather than replaced, and the value keeps the original's separator shape,
    because the partitions' concatenation was written against it.

    ⚠️ **Only the members the model reads are copied.** Blind-review finding 2: this used to
    `copytree` the source folder, so an `unreferenced-secret.txt` sitting beside the extract was
    copied, listed in the manifest and shipped, exit 0. A package exists to be handed to someone
    else, so that is a data-leak shape rather than untidiness - and the folder is very often a
    customer's own working directory. The set of members is derived from the M that reads the
    parameter (:func:`_parameter_usages`), so it is evidence, not a guess; when the M cannot be
    enumerated, nothing is copied and the reason is recorded.
    """
    texts = [document.read_text(encoding="utf-8") for document in documents]
    for document, text in zip(documents, texts, strict=True):
        rewritten = text
        for match in FOLDER_PARAM_RE.finditer(text):
            value = match.group("value")
            verdict = _path_verdict(value)
            if verdict == NOT_A_PATH or _inside(final, value):
                continue
            accounted.add(value)
            if verdict == UNCLASSIFIED:
                record["omissions"].append({"file": _leaf(value), "reason": UNCLASSIFIED_REASON})
                continue
            moved = _relocate_folder(match.group("name"), value, texts, dest, final, record, taken)
            if moved is None:
                continue
            rewritten = rewritten.replace(
                f"{match.group('prefix')}{value}{match.group('quote')}", f'{match.group("prefix")}{moved}"'
            )
        if rewritten != text:
            document.write_text(rewritten, encoding="utf-8")


def _relocate_folder(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    name: str,
    value: str,
    texts: list[str],
    dest: Path,
    final: Path,
    record: dict[str, Any],
    taken: dict[str, str],
) -> str | None:
    """`the parameter's new value`, or None when nothing could be shipped for it (reason recorded)."""
    mode, tails, reason = _parameter_usages(texts, _bare_name(name))
    if reason is not None:
        record["omissions"].append({"file": _leaf(value), "reason": reason})
        return None
    readable, refusal = _classify_source(value, expect_dir=True)
    if readable is None:
        record["omissions"].append({"file": _leaf(value), "reason": refusal})
        return None
    members = _shippable_members(readable, mode, tails, value, record)
    if members is None:
        return None
    relative = _packaged_data_target(value, taken, keep_leaf_only=True)
    _ship_folder(readable, dest / DATA_DIR / relative, relative, record, members)
    return _moved_folder_value(final, relative, value)


def _shippable_members(
    readable: Path, mode: str, tails: set[str], value: str, record: dict[str, Any]
) -> list[Path] | None:
    """The files to ship out of a referenced folder, or None when none may be (reason recorded).

    Both refusals live here rather than at the call site so that the ceiling is measured over the
    SAME list that is copied - a folder whose members are selected in one place and weighed in
    another is how a size gate stops covering what it was written for.
    """
    if mode == WHOLE_FOLDER:
        members, problems = sorted(path for path in readable.rglob("*") if path.is_file()), []
    else:
        members, problems = _folder_members(readable, tails)
    record["omissions"].extend(problems)
    if not members:
        record["omissions"].append(
            {"file": _leaf(value), "reason": "no file this parameter names could be shipped from it"}
        )
        return None
    ceiling = _ceiling_refusal(sum(member.stat().st_size for member in members))
    if ceiling is not None:
        record["omissions"].append({"file": _leaf(value), "reason": ceiling})
        return None
    return members


def _parameter_usages(texts: list[str], bare: str) -> tuple[str, set[str], str | None]:
    """`(mode, literal tails, refusal)` - what the model actually reads through a folder parameter.

    Three answers, and the third is why this exists rather than a `copytree`:

    * :data:`NAMED_FILES` - every use is `<param> & "<literal>"`, so the members are enumerable and
      only those are shipped;
    * :data:`WHOLE_FOLDER` - a `Folder.Files`/`Folder.Contents` call reads the directory itself, so
      the whole tree genuinely IS referenced and copying it is evidenced rather than assumed;
    * a refusal - the parameter is used in a way this cannot enumerate (a computed file name), or is
      never read at all. Nothing is copied, the literal is left resolving where it did before, and
      the reason is recorded. Guessing "copy everything" there is exactly the leak.

    Every occurrence of the name is accounted for, not just the ones that match a known shape: an
    unexplained occurrence is what makes the answer a refusal.
    """
    quoted = re.escape(bare)
    reference = rf'#"{quoted}"|(?<![A-Za-z0-9_]){quoted}(?![A-Za-z0-9_])'
    token = re.compile(reference)
    concat = re.compile(rf'(?:{reference})\s*&\s*"([^"]*)"')
    whole = re.compile(rf"Folder\.(?:Files|Contents)\s*\(\s*(?:{reference})\s*[,)]")
    declaration = re.compile(rf"expression\s+(?:{reference})\s*=")
    tails: set[str] = set()
    whole_folder = False
    unexplained = 0
    for text in texts:
        spans = [match.span() for match in declaration.finditer(text)]
        for match in concat.finditer(text):
            tails.add(match.group(1))
            spans.append(match.span())
        for match in whole.finditer(text):
            whole_folder = True
            spans.append(match.span())
        for match in token.finditer(text):
            if not any(start <= match.start() and match.end() <= end for start, end in spans):
                unexplained += 1
    if unexplained:
        return (
            UNKNOWN_USAGE,
            tails,
            "the model reads this folder in a way the packager cannot enumerate, so shipping it "
            "would mean copying every file in it - nothing was shipped",
        )
    if whole_folder:
        return WHOLE_FOLDER, tails, None
    if tails:
        return NAMED_FILES, tails, None
    return NO_USAGE, tails, "no partition reads a file through this parameter, so nothing was shipped for it"


def _folder_members(readable: Path, tails: set[str]) -> tuple[list[Path], list[dict[str, str]]]:
    """`(files a partition names, omissions)` for the literal tails read through a folder parameter."""
    members: list[Path] = []
    problems: list[dict[str, str]] = []
    for tail in sorted(tails):
        parts = [part for part in re.split(r"[\\/]+", tail) if part not in ("", ".")]
        if not parts or ".." in parts:
            problems.append(
                {"file": _leaf(tail) or tail, "reason": "the name a partition builds escapes the folder it reads from"}
            )
            continue
        candidate = readable.joinpath(*parts)
        if candidate.is_file():
            members.append(candidate)
        else:
            problems.append({"file": parts[-1], "reason": "named by a partition but absent from the folder it reads"})
    return members, problems


def _moved_folder_value(final: Path, relative: str, original: str) -> str:
    """The parameter's new value, keeping the ORIGINAL's trailing-separator convention.

    Partitions concatenate onto this value - `File.Contents(#"SourceFolder" & "\\Sample -
    Superstore.xlsx")` - so adding or dropping a separator here silently breaks every path built
    from it, in a way no structural check can see.
    """
    trailing = "\\" if original.endswith(("\\", "/")) else ""
    return f"{final}\\{DATA_DIR}\\{relative.replace('/', chr(92))}{trailing}"


def _ship_folder(readable: Path, target: Path, relative: str, record: dict[str, Any], members: list[Path]) -> None:
    """Copy the NAMED members of a referenced folder into the package and record each one."""
    for member in members:
        sub = member.relative_to(readable)
        landing = target / sub
        landing.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(member, landing)
        record["shipped"].append({"path": f"{DATA_DIR}/{relative}/{sub.as_posix()}", "bytes": landing.stat().st_size})
        record["bytes"] += landing.stat().st_size


def _localize_file_literals(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    documents: list[Path],
    dest: Path,
    final: Path,
    record: dict[str, Any],
    taken: dict[str, str],
    model_name: str | None,
    accounted: set[str],
) -> None:
    """Ship each bare `File.Contents("<absolute file>")` source and repoint it at a new parameter."""
    parameter = _data_folder_param(documents)
    shipped: dict[str, str] = {}
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for source in sorted({match.group(1) for match in FILE_CONTENTS_RE.finditer(text)}):
            if not _is_path_literal(source) or _inside(final, source) or source in shipped:
                continue
            readable, refusal = _classify_source(source)
            accounted.add(source)
            if readable is None:
                record["omissions"].append({"file": _leaf(source), "reason": refusal})
                continue
            relative = _packaged_data_target(source, taken)
            _ship_file(readable, dest / DATA_DIR / relative, relative, record)
            shipped[source] = relative

    if not shipped:
        return
    _rewrite_partitions(documents, shipped, parameter)
    _write_data_folder_expression(dest, final, str(model_name), parameter)
    _assert_declared_once(_model_tmdl(dest, model_name), parameter)
    record["parameter"] = parameter


def _assert_declared_once(documents: list[Path], parameter: str) -> None:
    """Tripwire: the parameter this packager introduced must be declared exactly once.

    :func:`_data_folder_param` makes a duplicate unreachable. It is asserted anyway because the
    consequence is invisible here and fatal later: a model with two `expression <name> =` blocks is
    written happily, packaging exits 0, and AMO refuses to load it (`check_datamodel.py` exit 1) on
    someone else's machine.
    """
    declared = sum(
        1
        for document in documents
        for match in EXPRESSION_NAME_RE.finditer(document.read_text(encoding="utf-8"))
        if _bare_name(match.group(1)).casefold() == parameter.casefold()
    )
    if declared != 1:
        raise PackagingError(
            f"the packaged model declares `expression {parameter}` {declared} times; a duplicate makes "
            "the model unloadable, so nothing is shipped"
        )


def _ship_file(readable: Path, target: Path, relative: str, record: dict[str, Any]) -> None:
    """Copy one referenced file into the package and record it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(readable, target)
    record["shipped"].append({"path": f"{DATA_DIR}/{relative}", "bytes": target.stat().st_size})
    record["bytes"] += target.stat().st_size


def _leaf(value: str) -> str:
    """The last segment of a path literal - all a finding may carry.

    An absolute path embeds a real username and this repo is public, so artifacts get the leaf and
    nothing else. Split lexically for the same reason :func:`_inside` is lexical: no probing.
    """
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or value


def _rewrite_partitions(documents: list[Path], shipped: dict[str, str], parameter: str) -> None:
    """Point each shipped reference at the package's own copy, through the folder parameter."""
    for document in documents:
        text = document.read_text(encoding="utf-8")

        def _sub(match: re.Match[str], _shipped: dict[str, str] = shipped) -> str:
            relative = _shipped.get(match.group(1))
            if relative is None:
                return match.group(0)
            return f'File.Contents({parameter} & "{relative.replace("/", chr(92))}")'

        rewritten = FILE_CONTENTS_RE.sub(_sub, text)
        if rewritten != text:
            document.write_text(rewritten, encoding="utf-8")


def _external_after_rewrite(documents: list[Path], final: Path, accounted: set[str]) -> list[dict[str, str]]:
    """Read the WRITTEN files back and report every literal still pointing outside the package.

    This is the verification step, and it is deliberately the general question - "is any absolute
    path escaping the package?" - rather than "did my rewrite fire?". Scoping it to the constructs
    the rewriter understands is exactly how the first version of this fix closed less than half of
    #461: it could not see what it did not already look for. ``accounted`` holds the literals the
    repairs already reported with a SPECIFIC reason, so a known-unshippable source is named once
    rather than twice.

    ⚠️ An absolute path UNDER the package is legitimate and must not be reported. That is
    `set_data_folder.py`'s existing convention and it is what both repairs above produce; the rule is
    "absolute AND not under the destination", never "absolute" (finding from PR #462).

    ⚠️ **A literal this cannot classify is reported too, in its own words.** Silence was reserved
    for "definitely not a path", and an unclassifiable literal fell into it - so the escaping
    `"/Users/<person>/Data/"` of blind-review finding 5 left no trace anywhere. Each distinct
    literal is reported once however many documents carry it.

    ⚠️ **A literal whose ROLE proves it is not a path is silent, and only that.** A Databricks
    `HttpPath` endpoint is definitively a non-path (:func:`_service_routes`), so reporting it would
    put three of estate run 408's units into "check it by hand" for something no hand-check can
    change. That is the narrow carve-out: role-proven non-paths leave, shape-unassessable literals
    stay reported.
    """
    findings: list[dict[str, str]] = []
    seen: set[str] = set()
    for document in documents:
        text = document.read_text(encoding="utf-8")
        routes = _service_routes(text)
        for match in ABSOLUTE_LITERAL_RE.finditer(text):
            value = match.group(1)
            if value in accounted or value in seen or value in routes or _inside(final, value):
                continue
            verdict = _path_verdict(value)
            if verdict == PATH_LITERAL:
                seen.add(value)
                findings.append({"file": _leaf(value), "reason": "still points outside the package after packaging"})
            elif verdict == UNCLASSIFIED:
                seen.add(value)
                findings.append({"file": _leaf(value), "reason": UNCLASSIFIED_REASON})
    return findings


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


def _write_spec_schema(dest: Path) -> tuple[str | None, str | None]:
    """`(relative schema path, failure note)` - the spec CONTRACT, shipped rather than described.

    Copied verbatim from `docs/migration-spec.schema.json` so it can never drift from the schema
    `validate_spec.py` actually enforces; see :data:`SPEC_SCHEMA` for what an extract cost.
    """
    if not SPEC_SCHEMA.is_file():
        return None, f"no {SPEC_SCHEMA.name}: the spec contract could not be shipped from {SPEC_SCHEMA.parent.name}/"
    shutil.copy2(SPEC_SCHEMA, dest / SPEC_SCHEMA.name)
    return SPEC_SCHEMA.name, None


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


def package_unit(  # pylint: disable=too-many-arguments
    bundle: Path,
    unit: str,
    out_root: Path,
    *,
    oracle_dir: Path | None,
    assets_dir: Path | None,
    discard_edits: bool = False,
) -> dict[str, Any]:
    """Assemble one unit's package. Returns the record written to `package-manifest.json`.

    ⚠️ **Refuses rather than overwriting a package that has been edited.** This packager declares
    `<package>/fabric/` the canonical place to work, and `replace_dir` replaces the package whole -
    so re-running the same command silently deleted an agent's TMDL (blind-review finding 6). Silent
    loss is the one unacceptable outcome; refusing costs a flag and names the files. ``discard_edits``
    (`--discard-package-edits`) is the deliberate override.
    """
    existing = out_root / unit
    if existing.is_dir() and not discard_edits:
        changed, reason = package_edits(existing)
        if reason is not None or changed:
            raise PackageEditsRefused(unit, existing, changed, reason)
    staging = out_root / f".{sanitize_staging_name(unit)}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        result = _assemble_unit(
            bundle, unit, staging, final=out_root / unit, oracle_dir=oracle_dir, assets_dir=assets_dir
        )
        replace_dir(staging, out_root / unit)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return result


def package_contents(root: Path) -> dict[str, str]:
    """`{package-relative path: sha256}` for every file in a package except the manifest itself.

    The manifest is excluded because it is written last and CARRIES this map - including it would
    make every package differ from its own record.
    """
    return {
        path.relative_to(root).as_posix(): sha256_of(path) or ""
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() != MANIFEST_NAME
    }


def package_edits(root: Path) -> tuple[list[str], str | None]:
    """`(paths that differ from what packaging wrote, reason it could not be established)`.

    Whole-package, not just `fabric/`: the package README also tells an agent to append
    `limitations_encountered` entries to `migration-spec.json`, and losing those silently is the same
    defect wearing different clothes.

    A package with no recorded digest returns a REASON, never an empty change list. "I cannot tell
    whether this was edited" is not "it was not edited" - collapsing the two is how unassessable
    input ends up in the clean bucket, which is the defect class this whole review round is about.
    """
    manifest = read_json(root / MANIFEST_NAME)
    recorded = ((manifest or {}).get("contents") or {}).get("files") if isinstance(manifest, dict) else None
    if not isinstance(recorded, dict):
        return [], (
            f"it carries no {MANIFEST_NAME} content digest, so whether anything was edited in it cannot be established"
        )

    current = package_contents(root)
    changed = set(recorded) ^ set(current)
    changed |= {path for path in set(recorded) & set(current) if recorded[path] != current[path]}
    return sorted(changed), None


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


def _assemble_unit(  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    bundle: Path, unit: str, dest: Path, *, final: Path, oracle_dir: Path | None, assets_dir: Path | None
) -> dict[str, Any]:
    """Build one unit's package into ``dest``, which is always a fresh, empty directory.

    ``final`` is where ``dest`` will be renamed to. Anything that must record its OWN location -
    only the data-folder parameter today - has to use it, because ``dest`` stops existing the
    moment packaging succeeds.
    """
    engine_report = read_json(bundle / "report.json")
    workbooks, datasources = engine_unit_names(engine_report)
    dest.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    report_name, model_name = _copy_fabric(bundle, unit, dest)
    if report_name is None and model_name is None:
        notes.append(f"no engine working copy at pbip/{unit} - nothing to build on")
    data_sources = _localize_data_sources(dest, final, model_name)
    for omission in data_sources["omissions"]:
        notes.append(f"data source {omission['file']} not shipped: {omission['reason']}")

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
    schema, schema_note = _write_spec_schema(dest)
    if schema_note:
        notes.append(schema_note)
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
            "migration_spec_schema": schema,
            "asset": f"assets/{asset.name}" if asset else None,
            "asset_route": asset_route,
            "report": f"fabric/{report_name}" if report_name else None,
            "model": f"fabric/{model_name}" if model_name else None,
            "handover": f"handover/{unit}.json" if isinstance(handover, dict) else None,
        },
        "workbook_identity": identity,
        "data_sources": data_sources,
        "oracle": oracle,
        "notes": notes,
    }
    report_dir = dest / "fabric" / report_name if report_name else None
    workbook = _handover_workbook(handover, unit, dest)
    (dest / "handover.md").write_text(render_handover(result, workbook, visual_pages(report_dir)), encoding="utf-8")
    (dest / "README.md").write_text(README.format(unit=unit, kind=result["kind"]), encoding="utf-8")
    result["contents"] = {"files": package_contents(dest)}
    write_json(dest / MANIFEST_NAME, result)
    return result


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def render(results: list[dict[str, Any]], out_root: Path, refused: list[PackageEditsRefused] | None = None) -> str:
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
    for refusal in sorted(refused or [], key=lambda item: item.unit):
        lines.append(f"  KEPT {refusal.unit} - not repackaged, the existing package carries edits")
    packaged = sum(1 for result in results if result["packaged"])
    with_oracle = sum(1 for result in results if result["oracle"].get("objects"))
    lines.append(f"packaged {packaged}/{len(results)}; {with_oracle} carry oracle evidence")
    starved = sorted(result["unit"] for result in results if not result["packaged"])
    if starved:
        lines.append(
            f"WARN: {len(starved)} unit(s) have NO engine working copy under pbip/ - packaged for their "
            f"source, reference and handover only, with nothing to build on: {', '.join(starved)}"
        )
    if refused:
        lines.append(
            f"REFUSED: {len(refused)} unit(s) already carry edits in the package, which is the canonical "
            "place to work - they were left untouched. Re-run with --discard-package-edits to overwrite."
        )
    return "\n".join(lines)


def _package_each(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    units: list[str],
    bundle: Path,
    out_root: Path,
    oracle_dir: Path | None,
    assets_dir: Path | None,
    discard_edits: bool,
    results: list[dict[str, Any]],
    refused: list[PackageEditsRefused],
) -> None:
    """Package each unit, collecting refusals instead of stopping at the first one.

    One unit's edits must not stop the rest of the estate being packaged; `main` still returns 3 for
    any refusal, so this cannot pass unnoticed.
    """
    for unit in units:
        try:
            results.append(
                package_unit(
                    bundle,
                    unit,
                    out_root,
                    oracle_dir=oracle_dir,
                    assets_dir=assets_dir,
                    discard_edits=discard_edits,
                )
            )
        except PackageEditsRefused as refusal:
            print(str(refusal), file=sys.stderr)
            refused.append(refusal)


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
    parser.add_argument(
        "--discard-package-edits",
        action="store_true",
        help="overwrite an existing package even though it carries edits made since it was written",
    )
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
    results: list[dict[str, Any]] = []
    refused: list[PackageEditsRefused] = []
    _package_each(sorted(units), bundle, out_root, oracle_dir, assets_dir, args.discard_package_edits, results, refused)

    payload = {
        "id": "package-unit",
        "bundle": str(bundle),
        "out": str(out_root),
        "oracle": str(oracle_dir) if oracle_dir else None,
        "assets": str(assets_dir) if assets_dir else None,
        "units": results,
        "refused": [
            {"unit": refusal.unit, "changed": refusal.changed, "reason": refusal.reason} for refusal in refused
        ],
    }
    if args.json:
        write_json(args.json, payload)
    if not args.quiet:
        print(render(results, out_root, refused))
    if refused:
        return 3
    return 0 if all(result["packaged"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
