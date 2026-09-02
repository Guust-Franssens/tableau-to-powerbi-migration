"""
purpose: ENTRY gate - before an agent starts building, prove there is legible, ATTRIBUTABLE reference
         evidence of the Tableau source object behind every page the engine emitted, and name the grade.
usage:   python scripts/check_reference_readiness.py <bundle-or-unit> [...]
                [--source <workbook.twb|.twbx>] [--reference <dir>] [--oracle <dir>]
                [--require-validation-grade] [--json <file>] [--quiet] [--verbose]

Every other gate in this toolkit is an EXIT gate. `check_unit.py`'s own header says it answers
"whether one migration unit is **done**", so the visual-evidence question was asked *after* the work
instead of before it (issue #421). Wherever a capture gap exists, an equivalent fidelity bug is
**structurally unfalsifiable**, not merely unverified - so this gate makes the gap visible up front,
per page, with its grade.

Three questions, in order: **completeness** (a page for every source object the engine's own rule says
should exist), **evidence** (a usable render provably OF that object, in THIS workbook, at THIS
revision), and **grade**.

Fail closed - the ONE design rule
----------------------------------
`blind`, `unverifiable` and `insufficient-grade` are all distinct from `ready`, and **none of
them exits 0**. A readiness gate that green-lights on absent or unattributable evidence is worse than
no gate: it launches an agent to build confidently against nothing.

The mechanism is that **unverified evidence is unrepresentable**. `Evidence` is only reachable
through `Evidence.build()`, which returns either a fully verified record or a `RejectedEvidence`
that can never be matched. Round-1 review of PR #428 found three fail-open paths (a zero-byte render,
an empty `capabilities` list, and evidence attributed to the wrong workbook) precisely because
validity was re-checked at three call sites instead of being a construction precondition. Rejections
are counted and printed, so a capture that does not count says why rather than vanishing.

Exit codes
----------
The 0/1/2/3 shape is `check_connection_fidelity.py:160-163`'s, adopted rather than invented; its
`:165` comment records issue #366, where nine unexamined workbooks read as a clean bill of health.

| 0 | READY, or NOT_APPLICABLE (a datasource-only unit with no Tableau views). |
| 1 | FINDINGS: a page is blind, unverifiable, stale, dropped with no engine explanation, below the
      required grade, or its workbook shipped no report at all. |
| 2 | usage error (argparse) - a missing path never produces a verdict. |
| 3 | CANNOT_ESTABLISH: the expectation or the page mapping could not be derived, so this gate has no
      opinion and you must NOT read that as a pass. |

WARNING: **There is deliberately NO `--warn-only`.** Every sibling gate has one; this gate had one until
round-1 review measured it returning exit 0 on a bundle whose own output said "CANNOT_ESTABLISH is
NOT a pass". An entry gate that can be asked to say yes is not an entry gate. Advisory consumers read
`--json`, whose `status` always carries the true verdict.

WARNING: `NOT_APPLICABLE` is EARNED from the engine's own `report.json` - never inferred from "I found
no pages" and never from "some semantic model exists", both of which were measured granting a clean
exit to a workbook whose report generation had FAILED.

**Full rationale, with every measured defect and its citation: docs/reference-readiness.md.** That
covers why `check_unit.expected_pages()` cannot be reused, why candidates are not emitted pages, why a
drop explanation must match in KIND as well as name, the cryptographic page-identity join and its
collision limit, the evidence scope table, and the grade ceiling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from bundle_corpus import shipping_reports
import object_identity as oid
from object_identity import AMBIGUOUS
from reference_evidence import (
    MANUAL_KIND_HINT,
    CAP_VALIDATION,
    GRADE_ORACLE,
    GRADE_UNKNOWN,
    GRADE_VALIDATION,
    KIND_DASHBOARD,
    KIND_UNKNOWN,
    KIND_WORKSHEET,
    MIN_RENDER_EDGE,
    PROVIDER_CEILING,
    Evidence,
    RejectedEvidence,
    UnitIdentity,
    json_object,
    oracle_evidence,
    reference_evidence,
    sha256_of,
)

# Re-exported so this module stays the single vocabulary surface a consumer imports: a caller should
# never have to know which half of the split owns a constant. `__all__` keeps the linters honest
# about the ones this file does not itself reference.
__all__ = [
    "AMBIGUOUS",
    "CAP_VALIDATION",
    "GRADE_ORACLE",
    "GRADE_UNKNOWN",
    "GRADE_VALIDATION",
    "KIND_DASHBOARD",
    "KIND_UNKNOWN",
    "KIND_WORKSHEET",
    "MIN_RENDER_EDGE",
    "PROVIDER_CEILING",
    "Evidence",
    "RejectedEvidence",
    "UnitIdentity",
    "main",
    "render",
    "scan",
]


REPORT_NAME = "reference-readiness-check.json"

STATUS_READY = "READY"
STATUS_FINDINGS = "FINDINGS"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUS_CANNOT_ESTABLISH = "CANNOT_ESTABLISH"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_CANNOT_ESTABLISH = 3


PAGE_EMITTED = "emitted"
PAGE_DROPPED_EXPLAINED = "dropped_explained"
PAGE_DROPPED_UNEXPLAINED = "dropped_unexplained"

READY = "ready"
BLIND = "blind"
UNVERIFIABLE = "unverifiable"
INSUFFICIENT_GRADE = "insufficient-grade"

# Engine `viz_fidelity[]` reasons that mean "this page was dropped ON PURPOSE". Matched as substrings
# of the recorded reason, which carries the engine's "manual attention required: " prefix
# (`twb_to_pbir.py:6428-6430`).
DELIBERATE_DROP_MARKERS = (
    "no supported visuals on this dashboard",
    "unsupported visual type",
    "no usable field bindings",
)


@dataclass(frozen=True)
class SourceObject:
    """One Tableau object the engine's rule says should become a page."""

    name: str
    kind: str

    @property
    def page_id(self) -> str:
        """The exact PBIR page id the engine would emit for this object."""
        prefix = "page-ws-" if self.kind == KIND_WORKSHEET else "page-"
        return engine_page_id(prefix + self.name)


@dataclass
class UnitResult:
    """Per-migration-unit readiness verdict."""

    unit: str
    status: str
    detail: str
    report_dir: str | None = None
    source: str | None = None
    pages: list[dict[str, Any]] = field(default_factory=list)


def engine_page_id(text: str) -> str:
    """Reproduce the engine's `twb_to_pbir._sanitize` (installed plugin, :748-761).

    Copied rather than imported on purpose: the engine plugin is resolved through `engine_source.py`
    and may legitimately be absent (a bundle can be audited on a machine with no engine installed),
    and this gate must still be able to name the page an object maps to. The md5 over the FULL
    prefixed string is what makes a dashboard and a same-named worksheet land on different page ids,
    which is the whole identity join - so it is pinned by its own test.
    """
    base = re.sub(r"[^0-9A-Za-z_-]+", "", (text or "").replace(" ", ""))
    digest = hashlib.md5((text or "").encode("utf-8")).hexdigest()[:8]  # noqa: S324
    name = (base[:16] + digest) if base else ("v" + digest)
    return name[:24]


def _workbook_xml(path: Path) -> str | None:
    """The `.twb` XML text, unpacking a `.twbx` archive when needed."""
    try:
        if path.suffix.lower() == ".twbx":
            with zipfile.ZipFile(path) as archive:
                members = [n for n in archive.namelist() if n.lower().endswith(".twb")]
                return archive.read(members[0]).decode("utf-8", "ignore") if members else None
        return path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def source_objects(path: Path) -> list[SourceObject] | None:
    """Every object the engine's rule makes a page candidate: dashboards + ORPHAN worksheets.

    Mirrors `twb_to_pbir.emit_pbir` (:14040-14041, :14557-14560): one page per dashboard, plus one
    per worksheet not placed on any dashboard. A worksheet is *placed* when any zone under any
    dashboard names it - including an author-HIDDEN zone, which the engine seeds into `placed`
    explicitly (:14116-14118) so a collapsed help panel does not reappear as its own page. Hidden
    zones are ordinary `<zone name=...>` elements in the XML (`hidden_skipped[].ref` is
    `zone.get("name")`, :6231), so scanning every named zone captures them without a second rule.

    Returns None - never an empty list - when the workbook cannot be read, so "no objects" and
    "could not look" stay distinct all the way to the exit code.
    """
    xml = _workbook_xml(path)
    if xml is None:
        return None
    try:
        root = ElementTree.fromstring(xml)  # noqa: S314 - local migration input, not hostile
    except ElementTree.ParseError:
        return None

    worksheets = [w.get("name") for w in root.findall("./worksheets/worksheet") if w.get("name")]
    dashboards = [d for d in root.findall("./dashboards/dashboard") if d.get("name")]
    worksheet_names = {name for name in worksheets if name}

    placed: set[str] = set()
    for dashboard in dashboards:
        for zone in dashboard.iter("zone"):
            if zone.get("name") in worksheet_names:
                placed.add(str(zone.get("name")))

    objects = [SourceObject(name=d.get("name", ""), kind=KIND_DASHBOARD) for d in dashboards]
    objects.extend(SourceObject(name=name, kind=KIND_WORKSHEET) for name in worksheets if name and name not in placed)
    return objects


def page_map(report_dir: Path) -> tuple[dict[str, str], list[str]]:
    """`({page id: displayName}, problems)` for a PBIR report.

    A page whose `page.json` cannot be read is a PROBLEM, not a page. The previous version fell back
    to the containing directory's name, so round-1 review measured forcing every read to fail and
    still getting three pages and a `READY` verdict - completeness passed with no readable mapping at
    all.

    WARNING: `pages.json` is REQUIRED and must declare a list `pageOrder`. Round-2 finding 4: the
    cross-check ran only when `pageOrder` happened to be a list, so an absent, unreadable,
    wrong-shaped or non-list `pages.json` reported no problem and every discovered `page.json` was
    trusted - measured, failing ONLY the `pages.json` reads still produced `READY 3/3`. It is the
    report's own statement of which pages exist; without it there is no mapping to check against.
    """
    pages_root = report_dir / "definition" / "pages"
    found: dict[str, str] = {}
    problems: list[str] = []
    if not pages_root.is_dir():
        return found, ["no definition/pages folder"]
    for page_json in sorted(pages_root.rglob("page.json")):
        payload = json_object(page_json)
        name = (payload or {}).get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"unreadable or nameless page definition: {page_json.parent.name}/page.json")
            continue
        if name in found:
            problems.append(f"two page definitions both declare the id {name!r}")
        found[name] = str((payload or {}).get("displayName") or name)
    order = (json_object(pages_root / "pages.json") or {}).get("pageOrder")
    if not isinstance(order, list):
        problems.append(
            "pages.json is missing, unreadable, or declares no list pageOrder, so the report states "
            "no page set to check the page definitions against"
        )
        return found, problems
    declared = {str(item) for item in order}
    problems.extend(
        f"pages.json lists {missing!r} but no readable page definition exists for it"
        for missing in sorted(declared - set(found))
    )
    problems.extend(f"page {extra!r} exists but pages.json does not list it" for extra in sorted(set(found) - declared))
    return found, problems


def drop_explanations(handover: dict[str, Any] | None) -> dict[tuple[str, str], str]:
    """`{(kind, EXACT name): engine reason}` for pages the engine dropped ON PURPOSE.

    Read from the handover's `workbook.viz_fidelity[]`, the engine's structured per-object disclosure
    channel, which covers BOTH scopes: worksheet rows carry the worksheet name, while dashboard-scope
    warnings are appended as rows whose `worksheet` holds the dashboard name and whose `visual_type`
    holds the scope string (`migrate_estate.py:1201-1204`).

    WARNING: keyed by kind AND by the EXACT name, and that pair is the third and final form of one
    recurring defect - one object's excuse covering another's drop. It was fixed at the ROUTING level
    (`viz_fidelity[]` over `pbip_warnings[]`), then at the MATCHING level (`name` -> `(kind, name)`),
    and round-2 review found it again at the NORMALIZATION level: `_norm` collapses case and repeated
    whitespace, so `Ops  Summary` and `Ops Summary` - which the engine gives DIFFERENT page ids -
    shared a key, and one drop warning marked the other page accounted-for.

    Both sides of this join are engine/source artifacts and are byte-exact: `viz_fidelity[].worksheet`
    is the IR's own object name and `SourceObject.name` comes from the same workbook XML. So there is
    no normalization to do here, and adding a third key component would just move the boundary again.
    """
    explained: dict[tuple[str, str], str] = {}
    workbook = (handover or {}).get("workbook")
    rows = workbook.get("viz_fidelity") if isinstance(workbook, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("status") != "warned":
            continue
        name = row.get("worksheet")
        if not isinstance(name, str):
            continue
        kind = KIND_DASHBOARD if row.get("visual_type") == KIND_DASHBOARD else KIND_WORKSHEET
        for reason in [row.get("reason"), *(row.get("additional_reasons") or [])]:
            if isinstance(reason, str) and any(marker in reason for marker in DELIBERATE_DROP_MARKERS):
                explained.setdefault((kind, name), reason)
                break
    return explained


def match_evidence(obj: SourceObject, evidence: list[Evidence]) -> tuple[Evidence | str | None, list[Any]]:
    """(match, lookalikes) for one object, against evidence ALREADY scoped to its unit.

    The join runs through object_identity.CandidateIndex, which is what makes ambiguity
    unrepresentable rather than checked here: Resolution.value() RAISES unless exactly one match
    exists, ool(resolution) ALWAYS raises so a resolution can never be used as a condition, and
    the matches are private so "take the first candidate" has nothing to index. Round 4 measured why
    each of those has to RAISE rather than merely be absent - an object with no __bool__ is truthy,
    and a public tuple is indexable.

    Evidence names come from external producers whose spelling this repo does not control, so they go
    in a CandidateIndex, which refuses an ObjectIdentity BY TYPE - keeping engine names out of
    the lossy key table.

    A record whose producer declared no object type is indexed under KIND_UNKNOWN and resolves
    against nothing: "I cannot tell what this depicts" must not satisfy a page of either kind. The
    returned lookalikes carry descriptions only, never evidence objects, so a caller cannot quietly
    promote one into a match.
    """
    key = oid.ObjectIdentity.from_engine(obj.kind, obj.name)
    if key is None:
        return None, []
    index: oid.CandidateIndex[Evidence] = oid.CandidateIndex()
    for item in evidence:
        index.add(item.candidate(), item)
    resolution = index.resolve(key)
    if resolution.outcome == oid.UNIQUE:
        return resolution.value(), []
    lookalikes = oid.name_lookalikes(key, [item.candidate() for item in evidence])
    return (AMBIGUOUS if resolution.outcome == oid.AMBIGUOUS else None), lookalikes


def _render_key(match: Evidence) -> str:
    """A canonical identity for a render, so exclusivity compares CONTENT and not strings.

    Round 5: the previous version keyed on filesystem identity (`st_dev`/`st_ino`) and **fell back
    to a resolved, case-folded path** when `stat()` failed or `st_ino` was zero. That fallback
    reopened the very defect round 4 closed - it cannot identify hard links, mapped-drive vs UNC
    aliases, or any other distinct path to one physical file. Measured on this machine:
    `C:\\Windows\\System32\\notepad.exe` and `C:\\Windows\\notepad.exe` are hard links whose resolved
    case-folded paths differ, so on any filesystem without stable inodes one render would satisfy
    several pages again. Worse, the hard-link test SKIPS on exactly those filesystems, so the
    fallback was not merely unproven - it was untestable where it mattered.

    The digest is the conservative answer and needs no fallback: it is already VERIFIED against the
    bytes on disk by `Evidence.build`, which refuses a record whose recorded `sha256` does not match
    what the file actually contains. Two paths to one physical file always agree. Two genuinely
    different files that happen to be byte-identical also agree - and that direction is safe, because
    it makes exclusivity fire and the pages fail closed.
    """
    return match.render_digest


def _enforce_exclusivity(rows: list[dict[str, Any]]) -> None:
    """Invalidate every claim on a render credited to more than one page, ACROSS ALL UNITS.

    Identity is not enough on its own - evidence must also be EXCLUSIVE. Round 3 measured one image
    making two worksheets ready; round 4 measured the same render satisfying one page in EACH of two
    units, because the check ran independently inside each. It is applied once, over every row.
    """
    keys = [row["render_key"] for row in rows if row.get("render_key")]
    contested = {key for key in keys if keys.count(key) > 1}
    for row in rows:
        if row.get("render_key") in contested:
            row.update(
                {
                    "evidence": "unverifiable",
                    "readiness": UNVERIFIABLE,
                    "grade": GRADE_UNKNOWN,
                    "matched_by": (
                        f"one render ({row.get('evidence_path')}) is claimed by more than one page, "
                        "so no page can own it"
                    ),
                }
            )


def _page_row(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    obj: SourceObject,
    page_status: str,
    evidence: list[Evidence],
    reason: str | None,
    require_validation_grade: bool,
) -> dict[str, Any]:
    """One page's readiness row: what it came from, whether it exists, and what proves it."""
    row: dict[str, Any] = {
        "source_object": obj.name,
        "source_type": obj.kind,
        "page_id": obj.page_id,
        "page_status": page_status,
        "grade": GRADE_UNKNOWN,
        "matched_by": "none",
        "evidence": "absent",
    }
    if reason:
        row["engine_reason"] = reason
    if page_status != PAGE_EMITTED:
        # A page that does not exist cannot be judged on evidence. An explained drop is accounted
        # for; an unexplained one is the conversion gap the agent must know about before starting.
        row["readiness"] = READY if page_status == PAGE_DROPPED_EXPLAINED else BLIND
        return row
    match, name_only = match_evidence(obj, evidence)
    if match is AMBIGUOUS:
        row.update(
            {
                "evidence": "unverifiable",
                "matched_by": (
                    f"{len(name_only)} records share this name once normalized "
                    f"({', '.join(sorted({item.name for item in name_only}))}) - "
                    "picking one would be a guess"
                ),
                "readiness": UNVERIFIABLE,
            }
        )
        return row
    if isinstance(match, Evidence):
        insufficient = require_validation_grade and match.grade != GRADE_VALIDATION
        row.update(
            {
                "evidence": "present",
                "grade": match.grade,
                "matched_by": f"{match.origin}:{match.provider or 'unknown'} (scope={match.kind})",
                "evidence_path": match.path,
                "render_key": _render_key(match),
                "readiness": INSUFFICIENT_GRADE if insufficient else READY,
            }
        )
        return row
    if name_only:
        scopes = sorted({item.kind for item in name_only})
        row.update(
            {
                "evidence": "unverifiable",
                "matched_by": f"name only; scope {'/'.join(scopes)} cannot satisfy a {obj.kind} page",
                "readiness": UNVERIFIABLE,
            }
        )
        return row
    row["readiness"] = BLIND
    return row


def _engine_report(root: Path) -> dict[str, Any] | None:
    """The engine's own `report.json`, which is what classifies a unit as workbook vs datasource."""
    payload = json_object(root / "report.json")
    return payload if isinstance((payload or {}).get("workbooks"), list) else None


def _unit_names(engine_report: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """`(workbook names, datasource names)` the engine says this bundle produced - EXACT, as LISTS.

    Round-3 finding 2: these were normalized `set`s, so two genuinely distinct workbooks
    (`Ops  Summary` and `Ops Summary`) collapsed to one key and the collision was permanently
    discarded. `_units_without_reports` then reported no missing workbook, and a bundle that shipped
    NOTHING for the second one read READY.

    Both sides of every join built on this are engine artifacts written by the same run - the names
    in `report.json` and the `<name>.Report` folder names - so they are byte-exact and there is
    nothing to normalize. Lists rather than sets because multiplicity is itself a finding.
    """
    report = engine_report or {}
    workbooks = [
        str(item["name"]) for item in report.get("workbooks") or [] if isinstance(item, dict) and item.get("name")
    ]
    datasources = [
        str(item["name"]) for item in report.get("datasources") or [] if isinstance(item, dict) and item.get("name")
    ]
    return workbooks, datasources


def _handover(root: Path, unit: str) -> dict[str, Any] | None:
    return json_object(root / "handover" / f"{unit}.json")


def _provenance_luid(root: Path, source_sha: str) -> str | None:
    """The published workbook LUID for this source - ONLY when provenance is byte-confirmed.

    Round-2 finding 3: this returned a LUID without consulting `origin.match`.
    `stamp_tableau_provenance.py:191-192` records `"sha256"` when local and server bytes agree and
    `"name_only"` when they DIFFER, and its own docstring says figures will not reproduce in that
    case - yet that LUID was making server oracle evidence ready. The repo's provenance is 26
    `sha256` / 15 `name_only` / 6 unmatched, so trusting it blindly is the common case.

    Both halves are required: the stamped input hash must be THIS file, and the server comparison
    must have agreed. Anything else leaves the name as the only usable identity, which `is_for`
    falls back to.
    """
    payload = json_object(root / "source-provenance.json")
    for record in (payload or {}).get("inputs") or []:
        if not isinstance(record, dict):
            continue
        stamped = record.get("input") if isinstance(record.get("input"), dict) else {}
        origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
        if stamped.get("sha256") != source_sha:
            continue
        if origin.get("match") != "sha256":
            return None
        luid = origin.get("workbook_luid")
        return str(luid) if isinstance(luid, str) and luid else None
    return None


def resolve_source(root: Path, unit: str, handover: dict[str, Any] | None, explicit: Path | None) -> Path | None:
    """Locate the Tableau workbook this unit was built from.

    Order: an explicit `--source`; the handover's `workbook.source_id` (a run-root-relative path such
    as `_runs\\406-...\\assets\\Book.twb`, so it is tried against the bundle, its parent and its
    grandparent); then `input_manifest.json`'s staged asset whose stem matches the unit name. Returns
    None rather than guessing, which becomes CANNOT_ESTABLISH.
    """
    if explicit is not None:
        return explicit if explicit.is_file() else None
    workbook = (handover or {}).get("workbook")
    source_id = workbook.get("source_id") if isinstance(workbook, dict) else None
    if isinstance(source_id, str) and source_id.strip():
        raw = Path(source_id)
        candidates = [raw, *(base / raw for base in (root, root.parent, root.parent.parent))]
        # A run-root-relative id also resolves once its leading `_runs/<run>/` segment is consumed by
        # the base we join it to, so try the bare filename against the sibling assets folder too.
        candidates.extend(base / "assets" / raw.name for base in (root, root.parent))
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    manifest = json_object(root / "input_manifest.json")
    for asset in (manifest or {}).get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if Path(name).stem == unit:
            staged = asset.get("staged_input_path")
            for candidate in (Path(str(staged)) if staged else None, root.parent / "assets" / name):
                if candidate is not None and candidate.is_file():
                    return candidate
    return None


def _default_dirs(root: Path, name: str) -> list[Path]:
    """Conventional evidence locations, mirroring `check_unit.py:830-839`."""
    seen: list[Path] = []
    for candidate in (root / name, root.parent / name, root.parent.parent / name):
        if candidate.is_dir() and candidate.resolve() not in {path.resolve() for path in seen}:
            seen.append(candidate)
    return seen


def _cannot(unit: str, detail: str, report_dir: Path | None = None, source: Path | None = None) -> UnitResult:
    """A unit this gate could not form an opinion about. Never a pass."""
    return UnitResult(
        unit=unit,
        status=STATUS_CANNOT_ESTABLISH,
        detail=detail,
        report_dir=str(report_dir) if report_dir else None,
        source=str(source) if source else None,
    )


def _identify(root: Path, unit: str, source: Path) -> UnitIdentity | None:
    """Unit identity, or None when the source cannot be hashed (so evidence cannot be attributed)."""
    digest = sha256_of(source)
    if digest is None:
        return None
    return UnitIdentity(
        name=unit, source_path=source, source_sha256=digest, workbook_luid=_provenance_luid(root, digest)
    )


def _page_rows(
    objects: list[SourceObject],
    emitted: dict[str, str],
    explained: dict[tuple[str, str], str],
    scoped: list[Evidence],
    require_validation_grade: bool,
) -> list[dict[str, Any]]:
    """One readiness row per expected page, splitting explained from unexplained drops."""
    rows = []
    for obj in objects:
        key = (obj.kind, obj.name)
        if obj.page_id in emitted:
            status, reason = PAGE_EMITTED, None
        elif key in explained:
            status, reason = PAGE_DROPPED_EXPLAINED, explained[key]
        else:
            status, reason = PAGE_DROPPED_UNEXPLAINED, None
        rows.append(_page_row(obj, status, scoped, reason, require_validation_grade))
    return rows


def _expectation(unit: str, report_dir: Path, source: Path) -> list[SourceObject] | UnitResult:
    """The expected page set for a unit, or the `CANNOT_ESTABLISH` result explaining why not."""
    objects = source_objects(source)
    if not objects:
        return _cannot(
            unit,
            f"source workbook could not be parsed: {source}"
            if objects is None
            else f"{source.name} declares no dashboards and no worksheets, so no page expectation exists "
            "- this gate has no opinion and that is NOT a pass",
            report_dir,
            source,
        )
    ids = [obj.page_id for obj in objects]
    duplicates = sorted({page_id for page_id in ids if ids.count(page_id) > 1})
    if duplicates:
        return _cannot(
            unit,
            f"{len(duplicates)} page id(s) are claimed by more than one source object "
            f"({', '.join(duplicates)}) - the engine keeps only 8 md5 digits, so one physical page "
            "would satisfy two expectations and neither could be attributed",
            report_dir,
            source,
        )
    # Round-2 finding 5: `Ops  Summary` and `Ops Summary` get DIFFERENT page ids but collapse to one
    # normalized key, so a single evidence record marked both ready. External providers spell names
    # in ways this repo does not control, so a normalized fallback is unavoidable there - which makes
    # a collision among the EXPECTED objects unresolvable by construction. Refuse the unit rather
    # than resolve it. `object_oid.collisions` preserves multiplicity throughout.
    identities = [oid.ObjectIdentity.from_engine(obj.kind, obj.name) for obj in objects]
    if any(key is None for key in identities):
        return _cannot(unit, "a source object has no usable identity, so no page can be attributed", report_dir, source)
    merged = oid.collisions([key for key in identities if key is not None])
    if merged:
        shown = "; ".join(", ".join(repr(name) for name in group) for group in merged)
        return _cannot(
            unit,
            f"{len(merged)} source object name(s) differ only by case or repeated whitespace "
            f"({shown}) - they take different page ids but one evidence record would match both, so "
            "no capture could be attributed to either",
            report_dir,
            source,
        )
    return objects


def _datasource_only(unit: str, report_dir: Path, engine_report: dict[str, Any] | None) -> UnitResult | None:
    """`NOT_APPLICABLE` when the engine itself classifies this unit as datasource-only, else None.

    EARNED from the engine's own `report.json`. Round-1 review measured the fail-open alternatives:
    inferring it from an empty page list, or from the mere existence of a semantic model, both gave a
    clean exit 0 to a workbook whose report generation had FAILED.
    """
    workbooks, datasources = _unit_names(engine_report)
    if engine_report is None or unit not in datasources or unit in workbooks:
        return None
    return UnitResult(
        unit=unit,
        status=STATUS_NOT_APPLICABLE,
        detail="datasource-only unit: the engine lists it under datasources[], so it has no Tableau views",
        report_dir=str(report_dir),
    )


def _readiness_result(unit: str, report_dir: Path, source: Path, rows: list[dict[str, Any]]) -> UnitResult:
    """Fold per-page rows into the unit verdict."""
    findings = [row for row in rows if row["readiness"] != READY]
    return UnitResult(
        unit=unit,
        status=STATUS_FINDINGS if findings else STATUS_READY,
        detail=f"{len(rows) - len(findings)}/{len(rows)} expected page(s) ready",
        report_dir=str(report_dir),
        source=str(source),
        pages=rows,
    )


def _emitted_pages(unit: str, report_dir: Path, source: Path) -> dict[str, str] | UnitResult:
    """The report's readable page ids, or the `CANNOT_ESTABLISH` result explaining why not."""
    emitted, problems = page_map(report_dir)
    if problems:
        return _cannot(
            unit,
            f"the report's page mapping is not readable, so completeness cannot be judged: {'; '.join(problems[:4])}",
            report_dir,
            source,
        )
    return emitted


def assess_unit(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
    root: Path,
    report_dir: Path,
    engine_report: dict[str, Any] | None,
    evidence: list[Evidence],
    explicit_source: Path | None,
    require_validation_grade: bool,
) -> UnitResult:
    """Readiness for one shipping report."""
    unit = report_dir.name[: -len(".Report")]
    exempt = _datasource_only(unit, report_dir, engine_report)
    if exempt is not None:
        return exempt

    handover = _handover(root, unit)
    source = resolve_source(root, unit, handover, explicit_source)
    if source is None:
        return _cannot(
            unit,
            "no Tableau source workbook could be resolved, so the expected page set cannot be derived "
            "- pass --source, or run this against the bundle whose assets/ holds it",
            report_dir,
        )
    identity = _identify(root, unit, source)
    if identity is None:
        return _cannot(unit, f"source workbook could not be hashed, so evidence cannot be attributed: {source}")

    objects = _expectation(unit, report_dir, source)
    if isinstance(objects, UnitResult):
        return objects
    emitted = _emitted_pages(unit, report_dir, source)
    if isinstance(emitted, UnitResult):
        return emitted

    scoped = [item for item in evidence if item.is_for(identity)]
    rows = _page_rows(objects, emitted, drop_explanations(handover), scoped, require_validation_grade)
    return _readiness_result(unit, report_dir, source, rows)


def _units_without_reports(engine_report: dict[str, Any] | None, reports: list[Path]) -> list[UnitResult]:
    """A workbook the engine says it produced, with no shipping report, is a FINDING.

    Round-1 review measured the fail-open shape this replaces: any semantic model anywhere granted
    `NOT_APPLICABLE` and exit 0, so a workbook whose report generation FAILED read as legitimately
    reference-free. `NOT_APPLICABLE` must be earned from the datasource classification, never from
    the mere absence of a report.

    Round-3 finding 2: the comparison was normalized and set-based, so `Ops  Summary` and
    `Ops Summary` collapsed to one key and one shipping report covered both - the second workbook
    shipped nothing and the bundle read READY. Exact names, and a duplicate in the engine's own list
    is reported rather than deduplicated away.
    """
    shipped = [path.name[: -len(".Report")] for path in reports]
    workbooks, _ = _unit_names(engine_report)
    missing = [
        UnitResult(
            unit=name,
            status=STATUS_FINDINGS,
            detail=(
                "the engine lists this workbook but no report ships for it - conversion did not "
                "produce a report, so there is nothing to reference and nothing to build on"
            ),
        )
        for name in sorted(set(workbooks) - set(shipped))
    ]
    missing.extend(
        _cannot(
            name,
            f"the engine's report.json lists the workbook name {name!r} more than once, so a "
            "shipping report cannot be attributed to either entry",
        )
        for name in oid.duplicates(workbooks)
    )
    return missing


def _empty_bundle_unit(root: Path, engine_report: dict[str, Any] | None) -> UnitResult:
    """The verdict for a bundle that shipped no report at all."""
    _, datasources = _unit_names(engine_report)
    if datasources:
        return UnitResult(
            unit=root.name,
            status=STATUS_NOT_APPLICABLE,
            detail="the engine produced datasource migrations only, so there are no Tableau views to reference",
        )
    return _cannot(root.name, "the engine report lists neither workbooks nor datasources - nothing was measured")


def _collect_evidence(
    root: Path, reference_dir: Path | None, oracle_dir: Path | None
) -> tuple[list[Evidence], list[RejectedEvidence]]:
    """Every candidate render under ``root``, split into verified evidence and rejections."""
    reference_dirs = [reference_dir] if reference_dir else _default_dirs(root, "reference")
    oracle_dirs = [oracle_dir] if oracle_dir else _default_dirs(root, "_oracle") + _default_dirs(root, "oracle")
    ref_ok, ref_bad = reference_evidence(reference_dirs)
    orc_ok, orc_bad = oracle_evidence(oracle_dirs)
    return ref_ok + orc_ok, ref_bad + orc_bad


def scan(
    root: Path,
    *,
    explicit_source: Path | None = None,
    reference_dir: Path | None = None,
    oracle_dir: Path | None = None,
    require_validation_grade: bool = False,
) -> dict[str, Any]:
    """Assess every shipping report under ``root``."""
    root = root.resolve()
    evidence, rejected = _collect_evidence(root, reference_dir, oracle_dir)
    engine_report = _engine_report(root)
    reports = shipping_reports(root)

    if not reports and engine_report is None:
        detail = "no shipping report and no engine report.json found - nothing was measured"
        return _merge(root, [_cannot(root.name, detail)], evidence, rejected)
    units = [
        assess_unit(root, report, engine_report, evidence, explicit_source, require_validation_grade)
        for report in reports
    ]
    units.extend(_units_without_reports(engine_report, reports))
    # Exclusivity is enforced ONCE, across every unit's rows. Round-4 finding: running it inside each
    # unit let the same render satisfy one page in each of two units and report READY 2/2.
    _enforce_exclusivity([row for unit in units for row in unit.pages])
    for unit in units:
        if unit.status == STATUS_READY and any(row["readiness"] != READY for row in unit.pages):
            unit.status = STATUS_FINDINGS
            ready = sum(1 for row in unit.pages if row["readiness"] == READY)
            unit.detail = f"{ready}/{len(unit.pages)} expected page(s) ready"
    return _merge(root, units or [_empty_bundle_unit(root, engine_report)], evidence, rejected)


def _merge(
    root: Path, units: list[UnitResult], evidence: list[Evidence], rejected: list[RejectedEvidence]
) -> dict[str, Any]:
    """Roll per-unit verdicts into one report, keeping every count visible."""
    pages = [page for unit in units for page in unit.pages]
    findings = [page for page in pages if page["readiness"] != READY]
    evidenced = [page for page in pages if page["evidence"] == "present"]
    graded = {page["grade"] for page in evidenced}
    if any(unit.status == STATUS_FINDINGS for unit in units):
        status = STATUS_FINDINGS
    elif any(unit.status == STATUS_CANNOT_ESTABLISH for unit in units):
        status = STATUS_CANNOT_ESTABLISH
    elif units and all(unit.status == STATUS_NOT_APPLICABLE for unit in units):
        status = STATUS_NOT_APPLICABLE
    else:
        status = STATUS_READY
    return {
        "id": "reference-readiness",
        "status": status,
        "target": str(root),
        "units_scanned": len(units),
        "units_ready": sum(1 for unit in units if unit.status == STATUS_READY),
        "units_not_applicable": sum(1 for unit in units if unit.status == STATUS_NOT_APPLICABLE),
        "units_cannot_establish": sum(1 for unit in units if unit.status == STATUS_CANNOT_ESTABLISH),
        "pages_expected": len(pages),
        "pages_ready": len(pages) - len(findings),
        "pages_blind": sum(1 for page in pages if page["readiness"] == BLIND),
        "pages_unverifiable": sum(1 for page in pages if page["readiness"] == UNVERIFIABLE),
        "pages_insufficient_grade": sum(1 for page in pages if page["readiness"] == INSUFFICIENT_GRADE),
        "pages_emitted": sum(1 for page in pages if page["page_status"] == PAGE_EMITTED),
        "pages_dropped_explained": sum(1 for page in pages if page["page_status"] == PAGE_DROPPED_EXPLAINED),
        "pages_dropped_unexplained": sum(1 for page in pages if page["page_status"] == PAGE_DROPPED_UNEXPLAINED),
        "evidence_records": len(evidence),
        "evidence_untyped": sum(1 for item in evidence if item.kind == KIND_UNKNOWN),
        "evidence_untyped_names": sorted({item.name for item in evidence if item.kind == KIND_UNKNOWN}),
        "evidence_rejected": [
            {"name": item.name, "origin": item.origin, "path": item.path, "reason": item.reason} for item in rejected
        ],
        "grades_present": sorted(graded),
        # True only when EVERY evidenced page is validation-grade. `any` would let one good capture
        # silence the ceiling warning for every other page - round-1 finding 7.
        "all_evidence_validation_grade": bool(evidenced) and graded == {GRADE_VALIDATION},
        "units": [
            {
                "unit": unit.unit,
                "status": unit.status,
                "detail": unit.detail,
                "report": unit.report_dir,
                "source": unit.source,
                "pages": unit.pages,
            }
            for unit in units
        ],
    }


GRADE_CEILING_NOTE = (
    "  GRADE CEILING: not every page's evidence carries `validation_grade`. In practice that is the "
    "normal state, and it is a PROVIDER limit rather than an oversight - the capture routes in this "
    "toolkit top out at: embedded_thumbnail=layout only, public_playwright/oracle_capture=layout+text "
    "(default view state, no `?vf_` filter pinning). The ONLY route to validation grade is a render "
    "you captured yourself, dropped in `reference/` as `tableau-<object>.png`, and recorded with "
    "`capture_tableau_reference.py --manual-validation-grade` - which is your assertion, logged as "
    "such. Treat READY as 'a legible picture of the source exists', not as signed-off fidelity."
)


def render(report: dict[str, Any], *, verbose: bool = False) -> str:
    """Human-readable verdict, matching the sibling offline gates."""
    lines = [
        f"REFERENCE READINESS: {report['status']} - {report['pages_ready']}/{report['pages_expected']} "
        f"expected page(s) ready across {report['units_scanned']} unit(s); "
        f"{report['pages_blind']} blind, {report['pages_unverifiable']} unverifiable, "
        f"{report['pages_insufficient_grade']} below the required grade, "
        f"{report['pages_dropped_unexplained']} dropped with no engine explanation "
        f"({report['pages_dropped_explained']} explained)."
    ]
    for unit in report["units"]:
        lines.append(f"  [{unit['status']}] {unit['unit']}: {unit['detail']}")
        lines.extend(_render_page(page) for page in unit["pages"] if verbose or page["readiness"] != READY)
    lines.extend(
        f"  [REJECTED EVIDENCE] {item['name']!r} ({item['origin']}): {item['reason']}"
        for item in report["evidence_rejected"]
    )
    if report["evidence_untyped"]:
        # Round-3 finding 1: rather than let a grade flag stand in for an object type, say plainly
        # that these records were read and why they cannot count. A route that works by guessing is
        # worse than one that is honestly unavailable and says how to make it available.
        lines.append(
            f"  [UNTYPED EVIDENCE] {report['evidence_untyped']} render(s) carry no object type "
            f"({', '.join(report['evidence_untyped_names'][:4])}) - {MANUAL_KIND_HINT}"
        )
    if report["status"] == STATUS_CANNOT_ESTABLISH:
        lines.append(
            "  CANNOT_ESTABLISH is NOT a pass: this gate formed no opinion, so an agent starting "
            "here would be building blind with nothing to compare against."
        )
    if report["pages_expected"] and not report["all_evidence_validation_grade"]:
        lines.append(GRADE_CEILING_NOTE)
    if not verbose and report["pages_ready"]:
        lines.append("  Run with --verbose to list the pages that ARE ready and their grades.")
    return "\n".join(lines)


def _render_page(page: dict[str, Any]) -> str:
    """One page line: source object, its type, and what is (or is not) known about it."""
    label = f"    - {page['source_type']} {page['source_object']!r} -> {page['page_id']}"
    if page["page_status"] == PAGE_DROPPED_UNEXPLAINED:
        return f"{label}: NO PAGE EMITTED and the engine gave no reason - conversion gap"
    if page["page_status"] == PAGE_DROPPED_EXPLAINED:
        return f"{label}: no page, accounted for ({page.get('engine_reason', '')})"
    if page["readiness"] == BLIND:
        return f"{label}: BLIND - no reference render, so a fidelity bug here is unfalsifiable"
    if page["readiness"] == UNVERIFIABLE:
        return f"{label}: UNVERIFIABLE - {page['matched_by']}"
    if page["readiness"] == INSUFFICIENT_GRADE:
        return f"{label}: INSUFFICIENT GRADE - [{page['grade']}] below the required validation-grade bar"
    return f"{label}: ready [{page['grade']}] via {page['matched_by']}"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="engine bundle folder(s) or migration unit(s)")
    parser.add_argument("--source", type=Path, help="the Tableau .twb/.twbx this bundle was built from")
    parser.add_argument("--reference", type=Path, help="reference/ folder holding manifest.json")
    parser.add_argument("--oracle", type=Path, help="oracle folder holding oracle-manifest.json")
    parser.add_argument(
        "--require-validation-grade",
        action="store_true",
        help="treat anything below validation_grade as a finding (default: layout/text grade is enough to start)",
    )
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    parser.add_argument("--verbose", action="store_true", help="also list the pages that are ready")
    args = parser.parse_args(argv)

    if not args.paths:
        parser.error("give a bundle or migration-unit path")
    for path in args.paths:
        if not path.is_dir():
            parser.error(f"{path} is not a directory")
    if args.source is not None and not args.source.is_file():
        parser.error(f"--source {args.source} is not a file")

    reports = [
        scan(
            path,
            explicit_source=args.source,
            reference_dir=args.reference,
            oracle_dir=args.oracle,
            require_validation_grade=args.require_validation_grade,
        )
        for path in args.paths
    ]
    merged = reports[0] if len(reports) == 1 else _merge_scans(reports)
    if args.json:
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged, verbose=args.verbose))
    # There is deliberately no --warn-only: see the module docstring. An entry gate that can be asked
    # to say yes is not an entry gate, and the flag was measured returning 0 on CANNOT_ESTABLISH.
    if merged["status"] == STATUS_FINDINGS:
        return EXIT_FINDINGS
    if merged["status"] == STATUS_CANNOT_ESTABLISH:
        return EXIT_CANNOT_ESTABLISH
    return EXIT_OK


def _merge_scans(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine several bundle scans, preserving the per-unit rows and the status precedence."""
    merged = dict(reports[0])
    merged["target"] = ", ".join(report["target"] for report in reports)
    merged["units"] = [unit for report in reports for unit in report["units"]]
    merged["evidence_rejected"] = [item for report in reports for item in report["evidence_rejected"]]
    merged["evidence_untyped_names"] = sorted({n for r in reports for n in r["evidence_untyped_names"]})
    for key in (
        "units_scanned",
        "units_ready",
        "units_not_applicable",
        "units_cannot_establish",
        "pages_expected",
        "pages_ready",
        "pages_blind",
        "pages_unverifiable",
        "pages_insufficient_grade",
        "pages_emitted",
        "pages_dropped_explained",
        "pages_dropped_unexplained",
        "evidence_records",
        "evidence_untyped",
    ):
        merged[key] = sum(report[key] for report in reports)
    merged["grades_present"] = sorted({grade for report in reports for grade in report["grades_present"]})
    merged["all_evidence_validation_grade"] = all(report["all_evidence_validation_grade"] for report in reports)
    statuses = {report["status"] for report in reports}
    if STATUS_FINDINGS in statuses:
        merged["status"] = STATUS_FINDINGS
    elif STATUS_CANNOT_ESTABLISH in statuses:
        merged["status"] = STATUS_CANNOT_ESTABLISH
    elif statuses == {STATUS_NOT_APPLICABLE}:
        merged["status"] = STATUS_NOT_APPLICABLE
    else:
        merged["status"] = STATUS_READY
    return merged


if __name__ == "__main__":
    sys.exit(main())
