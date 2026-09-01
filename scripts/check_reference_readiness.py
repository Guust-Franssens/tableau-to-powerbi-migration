"""
purpose: ENTRY gate - before an agent starts building, prove there is legible reference evidence of
         the Tableau source object behind every page the engine emitted, and name the grade.
usage:   python scripts/check_reference_readiness.py <bundle-or-unit> [...]
                [--source <workbook.twb|.twbx>] [--reference <dir>] [--oracle <dir>]
                [--require-validation-grade] [--json <file>] [--quiet] [--verbose] [--warn-only]

Why this exists
---------------
Every other gate in this toolkit is an EXIT gate. `check_unit.py`'s own header says it answers
"whether one migration unit is **done**", so the visual-evidence question is asked *after* the work
instead of before it. Issue #421: an agent started building with no picture of the source and nothing
stopped it.

A customer audit (SES) shipped a `columnChart` that should have been a `lineChart`, stacking five
airlines' 95/92/88/97/90% into one ~462% bar. Their conclusion is the argument for this gate:

    "This was only catchable because a Tableau reference image for that page happens to exist. The
    same class of bug on 'Availability Summary by Tail' would be completely invisible - there is
    nothing to compare against."

So wherever a capture gap exists, an equivalent fidelity bug is **structurally unfalsifiable**, not
merely unverified. This gate makes that gap visible up front, per page, with its grade.

Three questions, in order
------------------------
1. **Completeness** - does the emitted report have a page for every source object the engine's own
   rule says it should? A missing page is a *conversion* gap the agent must know about before it
   starts, not a fidelity gap discovered later.
2. **Evidence** - is there a reference render for each of those pages?
3. **Grade** - `validation-grade`, `layout/text only`, or unknown?

Why it does not reuse `check_unit.expected_pages()`
---------------------------------------------------
It cannot, three ways, all measured (and `check_unit.py` is owned by another change - not edited
here):

* its docstring says "dashboards only, never worksheets" (`check_unit.py:572`), but the engine emits
  a page per dashboard **and** a page per orphan worksheet (`twb_to_pbir.py:14040`). On the Meridian
  workbook - 0 dashboards, 3 worksheets - it expects 0 where the engine correctly emitted 3;
* it reads `migration-spec.json` via `_migration_spec` (`:285`), and no such file exists in an engine
  bundle, so it returns `None`;
* its consumer is then circular: `check_oracle_coverage:925` does
  `pages = expected_pages(target) or actual_pages(target)`, grading the output against itself, so a
  page the engine DROPPED cannot be counted as missing evidence.

This gate derives its own expectation from the source workbook and **never** falls back to what was
built. No expectation means `CANNOT_ESTABLISH`, which fails closed.

The engine's real page rule, and why a naive diff cries wolf
------------------------------------------------------------
"dashboards + orphan worksheets" names the *candidates*, not the emitted pages. `twb_to_pbir.py`
deliberately drops a page in three further cases, each with a recorded warning:

| `:14529` | a dashboard whose zones yield no supported visuals | "no supported visuals on this dashboard" |
| `:14558` | an orphan worksheet with `visual_type == VT_UNSUPPORTED` | "unsupported visual type" |
| `:14562` | an orphan worksheet whose query state is incomplete | "... no usable field bindings (skipped)" |

A gate that simply diffs candidates against emitted pages therefore raises a completeness finding on
every CORRECT bundle - the false-positive direction, and how a gate gets muted and stops protecting
anything. So drops are split:

* `dropped_explained`   - absent, and the engine said why (a matching `viz_fidelity[]` row exists)
* `dropped_unexplained` - absent with no engine explanation. This is the real conversion defect.

Both are reported and counted separately; only `dropped_unexplained` is a finding.

Identity, not name slug
-----------------------
`check_unit.py:265` matches on `_slug(view_name)` - lowercased alphanumerics. In Tableau a dashboard
routinely shares its name with its principal worksheet, so a worksheet render satisfies a dashboard
page. That false match is the NORMAL case, not an edge case, and it is live today:
`capture_tableau_reference.py:199` files `embedded_thumbnail` records - which are *worksheet* renders
(`extract_twb_thumbnails.py`: "Dashboards are not thumbnailed per se") - under the manifest's
`dashboards` key, where they are then slug-matched.

Two independent defences here:

* **Page identity is cryptographic.** The engine names pages `_sanitize("page-" + dashboard)` or
  `_sanitize("page-ws-" + worksheet)`, where `_sanitize` appends an md5 of the FULL prefixed string
  (`twb_to_pbir.py:748-761`). Reproduced in `engine_page_id()` and verified against the Meridian
  bundle: `Revenue by Region` -> `page-ws-Revenuebb7d27f78` as a worksheet but
  `page-RevenuebyRe2b117987` as a dashboard. Same name, different page, by construction.
* **Evidence carries a scope**, and it must match the page's kind. A worksheet-scope render can never
  satisfy a dashboard page. Scope that cannot be established (`unknown`) satisfies nothing.

Grade ceiling - stated in the output, not implied
--------------------------------------------------
`validation_grade` is today reachable only via `capture_tableau_reference.py --manual-validation-grade`
on a user-dropped screenshot; even a `reference/` capture records `"state": {}` with a live TODO to
pin parameter defaults, and an oracle capture is default-view-state with no `?vf_` filter pinning. So
in practice nearly everything is layout/text grade. The rendered verdict says so rather than letting
`READY` imply more evidence than exists.

Exit codes
----------
The 0/1/2/3 shape is `check_connection_fidelity.py:160-163`'s, adopted deliberately rather than
invented. Its comment at `:165` records issue #366 - "'nothing to compare here' and 'this unit could
not be examined' printed identically, and nine unexamined workbooks read as a clean bill of health" -
which is precisely the failure this gate exists to prevent.

| 0 | READY, or NOT_APPLICABLE: every expected page is emitted with evidence, or the unit
      legitimately has no pages. |
| 1 | FINDINGS: a page is blind (no evidence), its evidence's identity is unverifiable, it was
      dropped with no engine explanation, or its grade is below `--require-validation-grade`. |
| 2 | usage error (argparse) - a missing path never produces a verdict. |
| 3 | CANNOT_ESTABLISH: the expectation itself could not be derived, so this gate has no opinion
      and you must not read that as a pass. |

Precedence follows the sibling gate: findings outrank cannot-establish, and both counts are always
printed so neither hides the other. `NOT_APPLICABLE` is EARNED from the engine's own report
(`report.json` lists the unit under `datasources[]`, not `workbooks[]`) - never inferred from "I
found no pages", which is the fail-open shape this gate refuses.
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

from bundle_corpus import shipping_models, shipping_reports

REPORT_NAME = "reference-readiness-check.json"

STATUS_READY = "READY"
STATUS_FINDINGS = "FINDINGS"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUS_CANNOT_ESTABLISH = "CANNOT_ESTABLISH"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_CANNOT_ESTABLISH = 3

KIND_DASHBOARD = "dashboard"
KIND_WORKSHEET = "worksheet"
KIND_UNKNOWN = "unknown"

PAGE_EMITTED = "emitted"
PAGE_DROPPED_EXPLAINED = "dropped_explained"
PAGE_DROPPED_UNEXPLAINED = "dropped_unexplained"

READY = "ready"
BLIND = "blind"
UNVERIFIABLE = "unverifiable"

# Grade strings are `check_unit.py:868,906`'s, reused verbatim so one vocabulary describes evidence
# everywhere. Inventing a second spelling would make two gates disagree about the same artifact.
GRADE_VALIDATION = "validation-grade"
GRADE_ORACLE = "layout/text only (oracle capture, default view state)"
GRADE_UNKNOWN = "unknown"

CAP_VALIDATION = "validation_grade"

# What object a reference provider's output can possibly be a render OF. This is the scope join that
# replaces the name slug, and each entry is a structural fact about the provider, not a guess:
#
#   embedded_thumbnail - Tableau's `<thumbnail>` blocks are per-WORKSHEET renders. `extract_twb_
#                        thumbnails.py`: "Dashboards are not thumbnailed per se -- these are
#                        worksheet renders." This is the entry that makes the regression test bite.
#   public_playwright  - driven from the spec's dashboard list (`capture_tableau_reference.py:135`),
#                        so its records are dashboards by construction.
#   manual             - a user-dropped PNG. `_manual_capabilities` (`:261-266`) says the tool cannot
#                        know "even that it is a screenshot of this dashboard", so scope is UNKNOWN
#                        and it satisfies nothing on its own.
#   server_rest        - raises NotImplementedError today; listed so a future wiring is explicit.
PROVIDER_SCOPE = {
    "embedded_thumbnail": KIND_WORKSHEET,
    "public_playwright": KIND_DASHBOARD,
    "server_rest": KIND_DASHBOARD,
    "manual": KIND_UNKNOWN,
}

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
class Evidence:
    """One reference render, with the scope that decides what it may satisfy."""

    name: str
    kind: str
    grade: str
    origin: str
    provider: str | None = None
    path: str | None = None


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

    Copied rather than imported on purpose: the engine plugin is resolved through
    `engine_source.py` and may legitimately be absent (a bundle can be audited on a machine with no
    engine installed), and this gate must still be able to name the page an object maps to. The
    md5 over the FULL prefixed string is what makes a dashboard and a same-named worksheet land on
    different page ids, which is the whole identity join - so it is pinned by its own test.
    """
    base = re.sub(r"[^0-9A-Za-z_-]+", "", (text or "").replace(" ", ""))
    digest = hashlib.md5((text or "").encode("utf-8")).hexdigest()[:8]  # noqa: S324
    name = (base[:16] + digest) if base else ("v" + digest)
    return name[:24]


def _norm(text: str | None) -> str:
    """Whitespace/case-normalized name, for comparing two spellings of the same object."""
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _json_object(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or None when it is absent, unreadable or not an object."""
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _workbook_xml(path: Path) -> str | None:
    """The `.twb` XML text, unpacking a `.twbx` archive when needed."""
    try:
        if path.suffix.lower() == ".twbx":
            with zipfile.ZipFile(path) as archive:
                members = [n for n in archive.namelist() if n.lower().endswith(".twb")]
                if not members:
                    return None
                return archive.read(members[0]).decode("utf-8", "ignore")
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
            zone_name = zone.get("name")
            if zone_name in worksheet_names:
                placed.add(zone_name)

    objects = [SourceObject(name=d.get("name", ""), kind=KIND_DASHBOARD) for d in dashboards]
    objects.extend(SourceObject(name=name, kind=KIND_WORKSHEET) for name in worksheets if name and name not in placed)
    return objects


def actual_page_ids(report_dir: Path) -> dict[str, str]:
    """`{page id: displayName}` for every page in a PBIR report."""
    pages_root = report_dir / "definition" / "pages"
    found: dict[str, str] = {}
    if not pages_root.is_dir():
        return found
    for page_json in sorted(pages_root.rglob("page.json")):
        payload = _json_object(page_json)
        page_id = str((payload or {}).get("name") or page_json.parent.name)
        found[page_id] = str((payload or {}).get("displayName") or page_id)
    return found


def drop_explanations(handover: dict[str, Any] | None) -> dict[str, str]:
    """`{normalized object name: engine reason}` for pages the engine dropped ON PURPOSE.

    Read from the handover's `workbook.viz_fidelity[]`, which is the engine's structured per-object
    disclosure channel and covers BOTH scopes: worksheet rows carry the worksheet name, while
    dashboard-scope warnings are appended as rows whose `worksheet` holds the dashboard name and
    whose `visual_type` holds the scope (`migrate_estate.py:1201-1204`).

    `pbip_warnings[]` is deliberately NOT used for this: it is a flat list of reason strings, and
    `_warn("dashboard", name, "no supported visuals on this dashboard")` produces a reason that does
    not contain the dashboard's name (`twb_to_pbir.py:6428-6430`). Matching on it would attribute one
    dashboard's explanation to every dropped dashboard in the workbook.
    """
    explained: dict[str, str] = {}
    workbook = (handover or {}).get("workbook")
    rows = workbook.get("viz_fidelity") if isinstance(workbook, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("status") != "warned":
            continue
        name = row.get("worksheet")
        reasons = [row.get("reason"), *(row.get("additional_reasons") or [])]
        for reason in reasons:
            if isinstance(reason, str) and any(marker in reason for marker in DELIBERATE_DROP_MARKERS):
                explained.setdefault(_norm(name), reason)
                break
    return explained


def _entry_scope(entry: dict[str, Any], provider: str | None) -> str:
    """What kind of object a reference entry is a render of.

    An explicit `view_type`/`object_type` on the entry wins, so a manifest enriched with the oracle's
    view-type join (PR #422) is honoured without a code change here. Otherwise the provider decides.
    Anything unrecognised is UNKNOWN, which satisfies nothing - never a guess at either type.
    """
    declared = entry.get("view_type") or entry.get("object_type")
    if isinstance(declared, str) and declared.strip().casefold() in (KIND_DASHBOARD, KIND_WORKSHEET):
        return declared.strip().casefold()
    return PROVIDER_SCOPE.get(str(provider or ""), KIND_UNKNOWN)


def _reference_grade(capabilities: list[str]) -> str:
    """Grade for a `reference/manifest.json` state, in `check_unit.py:868`'s vocabulary."""
    caps = {str(cap) for cap in capabilities if isinstance(cap, str)}
    if not caps:
        return GRADE_UNKNOWN
    return GRADE_VALIDATION if CAP_VALIDATION in caps else "/".join(sorted(caps))


def reference_evidence(reference_dirs: list[Path]) -> list[Evidence]:
    """Evidence declared by `reference/manifest.json` files.

    Note the manifest's top-level key is `dashboards`, but `capture_tableau_reference.py:199` files
    WORKSHEET thumbnails there too. The key is therefore not evidence of scope; the provider is.
    """
    found: list[Evidence] = []
    for directory in reference_dirs:
        payload = _json_object(directory / "manifest.json")
        entries = (payload or {}).get("dashboards")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            for state in entry.get("states") or []:
                if not isinstance(state, dict):
                    continue
                image = state.get("image")
                if not (isinstance(image, str) and (directory / image).is_file()):
                    continue
                provider = state.get("provider")
                found.append(
                    Evidence(
                        name=name,
                        kind=_entry_scope({**entry, **state}, provider),
                        grade=_reference_grade(state.get("capabilities") or []),
                        origin="reference",
                        provider=str(provider) if provider else None,
                        path=str(directory / image),
                    )
                )
    return found


def oracle_evidence(oracle_dirs: list[Path]) -> list[Evidence]:
    """Evidence declared by `_oracle/oracle-manifest.json` files.

    `view_type` comes from PR #422's Metadata-API join and is consumed if present. It fails closed by
    design there (a disabled Metadata API yields `unknown` for everything), and it fails closed here
    too: absent or `unknown` means this record cannot satisfy any page, rather than being allowed to
    satisfy either kind. An oracle capture is default-view-state with no `?vf_` pinning, so its grade
    is layout/text only regardless of render leg.
    """
    found: list[Evidence] = []
    for directory in oracle_dirs:
        payload = _json_object(directory / "oracle-manifest.json")
        for record in (payload or {}).get("views") or []:
            if not isinstance(record, dict):
                continue
            legs = [record.get(leg) for leg in ("image", "svg", "pdf")]
            rendered = next(
                (
                    leg
                    for leg in legs
                    if isinstance(leg, dict)
                    and leg.get("status") == "ok"
                    and isinstance(leg.get("path"), str)
                    and (directory / leg["path"]).is_file()
                ),
                None,
            )
            if rendered is None:
                continue
            declared = record.get("view_type")
            kind = (
                declared.strip().casefold()
                if isinstance(declared, str) and declared.strip().casefold() in (KIND_DASHBOARD, KIND_WORKSHEET)
                else KIND_UNKNOWN
            )
            for label in (record.get("view_name"), record.get("view_url_name")):
                if isinstance(label, str) and label.strip():
                    found.append(
                        Evidence(
                            name=label,
                            kind=kind,
                            grade=GRADE_ORACLE,
                            origin="oracle",
                            provider="oracle_capture",
                            path=str(directory / rendered["path"]),
                        )
                    )
    return found


def match_evidence(obj: SourceObject, evidence: list[Evidence]) -> tuple[Evidence | None, list[Evidence]]:
    """Return `(match, name_only)` for one source object.

    A match requires BOTH the normalized name and the scope to agree - this is the rule that stops a
    worksheet render satisfying a dashboard page. `name_only` carries entries that share the name but
    whose scope does not match or could not be established; they are reported as UNVERIFIABLE rather
    than silently ignored, because "a picture exists but I cannot prove what it is of" is a different
    operator action from "no picture exists".
    """
    named = [item for item in evidence if _norm(item.name) == _norm(obj.name)]
    matched = [item for item in named if item.kind == obj.kind]
    if matched:
        best = next((item for item in matched if item.grade == GRADE_VALIDATION), matched[0])
        return best, []
    return None, named


def _page_row(obj: SourceObject, page_status: str, evidence: list[Evidence], reason: str | None) -> dict[str, Any]:
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
    if match is not None:
        row.update(
            {
                "evidence": "present",
                "grade": match.grade,
                "matched_by": f"{match.origin}:{match.provider or 'unknown'} (scope={match.kind})",
                "evidence_path": match.path,
                "readiness": READY,
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


def _unit_names(engine_report: dict[str, Any] | None) -> tuple[set[str], set[str]]:
    """`(workbook names, datasource names)` the engine says this bundle produced."""
    report = engine_report or {}
    workbooks = {
        _norm(item.get("name")) for item in report.get("workbooks") or [] if isinstance(item, dict) and item.get("name")
    }
    datasources = {
        _norm(item.get("name"))
        for item in report.get("datasources") or []
        if isinstance(item, dict) and item.get("name")
    }
    return workbooks, datasources


def _engine_report(root: Path) -> dict[str, Any] | None:
    """The engine's own `report.json`, which is what classifies a unit as workbook vs datasource."""
    payload = _json_object(root / "report.json")
    return payload if isinstance((payload or {}).get("workbooks"), list) else None


def _handover(root: Path, unit: str) -> dict[str, Any] | None:
    return _json_object(root / "handover" / f"{unit}.json")


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
    manifest = _json_object(root / "input_manifest.json")
    for asset in (manifest or {}).get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if Path(name).stem and _norm(Path(name).stem) == _norm(unit):
            staged = asset.get("staged_input_path")
            for candidate in (Path(str(staged)) if staged else None, root.parent / "assets" / name):
                if candidate is not None and candidate.is_file():
                    return candidate
    return None


def _default_dirs(root: Path, name: str) -> list[Path]:
    """Conventional evidence locations, mirroring `check_unit.py:830-839`."""
    candidates = [root / name, root.parent / name, root.parent.parent / name]
    seen: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if candidate.is_dir() and resolved not in {path.resolve() for path in seen}:
            seen.append(candidate)
    return seen


def _classify_unit(unit: str, engine_report: dict[str, Any] | None) -> str | None:
    """`NOT_APPLICABLE` when the engine itself says this unit has no Tableau views, else None.

    EARNED from the engine's own `report.json` classification. Deriving it from "I found no pages"
    instead would be fail-open: a workbook whose report failed to emit would read as a clean pass.
    """
    workbooks, datasources = _unit_names(engine_report)
    if engine_report is not None and _norm(unit) in datasources and _norm(unit) not in workbooks:
        return STATUS_NOT_APPLICABLE
    return None


def _page_rows(
    objects: list[SourceObject],
    report_dir: Path,
    handover: dict[str, Any] | None,
    evidence: list[Evidence],
) -> list[dict[str, Any]]:
    """One readiness row per expected page, splitting explained from unexplained drops."""
    emitted = actual_page_ids(report_dir)
    explained = drop_explanations(handover)
    rows = []
    for obj in objects:
        if obj.page_id in emitted:
            status, reason = PAGE_EMITTED, None
        elif _norm(obj.name) in explained:
            status, reason = PAGE_DROPPED_EXPLAINED, explained[_norm(obj.name)]
        else:
            status, reason = PAGE_DROPPED_UNEXPLAINED, None
        rows.append(_page_row(obj, status, evidence, reason))
    return rows


def _findings(rows: list[dict[str, Any]], require_validation_grade: bool) -> list[dict[str, Any]]:
    """Rows that block a start: not ready, or ready only at a grade below the requested bar."""
    findings = [row for row in rows if row["readiness"] != READY]
    if require_validation_grade:
        findings.extend(
            row
            for row in rows
            if row["readiness"] == READY and row["page_status"] == PAGE_EMITTED and row["grade"] != GRADE_VALIDATION
        )
    return findings


def assess_unit(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    root: Path,
    report_dir: Path,
    engine_report: dict[str, Any] | None,
    evidence: list[Evidence],
    explicit_source: Path | None,
    require_validation_grade: bool,
) -> UnitResult:
    """Readiness for one shipping report."""
    unit = report_dir.name[: -len(".Report")]
    if _classify_unit(unit, engine_report) == STATUS_NOT_APPLICABLE:
        return UnitResult(
            unit=unit,
            status=STATUS_NOT_APPLICABLE,
            detail="datasource-only unit: the engine lists it under datasources[], so it has no Tableau views",
            report_dir=str(report_dir),
        )

    handover = _handover(root, unit)
    source = resolve_source(root, unit, handover, explicit_source)
    if source is None:
        return UnitResult(
            unit=unit,
            status=STATUS_CANNOT_ESTABLISH,
            detail=(
                "no Tableau source workbook could be resolved, so the expected page set cannot be "
                "derived - pass --source, or run this against the bundle whose assets/ holds it"
            ),
            report_dir=str(report_dir),
        )

    objects = source_objects(source)
    if not objects:
        detail = (
            f"source workbook could not be parsed: {source}"
            if objects is None
            else (
                f"{source.name} declares no dashboards and no worksheets, so no page expectation "
                "exists - this gate has no opinion and that is NOT a pass"
            )
        )
        return UnitResult(
            unit=unit,
            status=STATUS_CANNOT_ESTABLISH,
            detail=detail,
            report_dir=str(report_dir),
            source=str(source),
        )

    rows = _page_rows(objects, report_dir, handover, evidence)
    findings = _findings(rows, require_validation_grade)
    status = STATUS_FINDINGS if findings else STATUS_READY
    ready_count = len(rows) - len(findings)
    return UnitResult(
        unit=unit,
        status=status,
        detail=f"{ready_count}/{len(rows)} expected page(s) ready",
        report_dir=str(report_dir),
        source=str(source),
        pages=rows,
    )


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
    reference_dirs = [reference_dir] if reference_dir else _default_dirs(root, "reference")
    oracle_dirs = [oracle_dir] if oracle_dir else _default_dirs(root, "_oracle") + _default_dirs(root, "oracle")
    evidence = reference_evidence(reference_dirs) + oracle_evidence(oracle_dirs)
    engine_report = _engine_report(root)
    reports = shipping_reports(root)

    if not reports:
        models = shipping_models(root, include_standalone=True)
        detail = (
            "no shipping report found; only semantic model(s) ship here, so there are no pages to reference"
            if models
            else "no shipping report and no semantic model found - nothing was measured"
        )
        status = STATUS_NOT_APPLICABLE if models else STATUS_CANNOT_ESTABLISH
        return _merge(root, [UnitResult(unit=root.name, status=status, detail=detail)], evidence)

    units = [
        assess_unit(root, report, engine_report, evidence, explicit_source, require_validation_grade)
        for report in reports
    ]
    return _merge(root, units, evidence)


def _merge(root: Path, units: list[UnitResult], evidence: list[Evidence]) -> dict[str, Any]:
    """Roll per-unit verdicts into one report, keeping every count visible."""
    pages = [page for unit in units for page in unit.pages]
    findings = [page for page in pages if page["readiness"] != READY]
    graded = {page["grade"] for page in pages if page["evidence"] == "present"}
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
        "pages_emitted": sum(1 for page in pages if page["page_status"] == PAGE_EMITTED),
        "pages_dropped_explained": sum(1 for page in pages if page["page_status"] == PAGE_DROPPED_EXPLAINED),
        "pages_dropped_unexplained": sum(1 for page in pages if page["page_status"] == PAGE_DROPPED_UNEXPLAINED),
        "evidence_records": len(evidence),
        "grades_present": sorted(graded),
        "validation_grade_present": GRADE_VALIDATION in graded,
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
    "  GRADE CEILING: no evidence here carries `validation_grade`. In practice that is the normal "
    "state - it is reachable only via `capture_tableau_reference.py --manual-validation-grade`, and "
    "both an oracle capture and a reference capture record the DEFAULT view state (no `?vf_` filter "
    "pinning). Treat READY as 'a legible picture of the source exists', not as signed-off fidelity."
)


def render(report: dict[str, Any], *, verbose: bool = False) -> str:
    """Human-readable verdict, matching the sibling offline gates."""
    head = (
        f"REFERENCE READINESS: {report['status']} - {report['pages_ready']}/{report['pages_expected']} "
        f"expected page(s) ready across {report['units_scanned']} unit(s); "
        f"{report['pages_blind']} blind, {report['pages_unverifiable']} unverifiable, "
        f"{report['pages_dropped_unexplained']} dropped with no engine explanation "
        f"({report['pages_dropped_explained']} explained)."
    )
    lines = [head]
    for unit in report["units"]:
        lines.append(f"  [{unit['status']}] {unit['unit']}: {unit['detail']}")
        for page in unit["pages"]:
            if page["readiness"] == READY and not verbose:
                continue
            lines.append(_render_page(page))
    if report["status"] == STATUS_CANNOT_ESTABLISH:
        lines.append(
            "  CANNOT_ESTABLISH is NOT a pass: this gate formed no opinion, so an agent starting "
            "here would be building blind with nothing to compare against."
        )
    if report["pages_expected"] and not report["validation_grade_present"]:
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
    parser.add_argument("--warn-only", action="store_true", help="always exit 0 after a successful scan")
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
    if args.warn_only:
        return EXIT_OK
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
    for key in (
        "units_scanned",
        "units_ready",
        "units_not_applicable",
        "units_cannot_establish",
        "pages_expected",
        "pages_ready",
        "pages_blind",
        "pages_unverifiable",
        "pages_emitted",
        "pages_dropped_explained",
        "pages_dropped_unexplained",
        "evidence_records",
    ):
        merged[key] = sum(report[key] for report in reports)
    grades = sorted({grade for report in reports for grade in report["grades_present"]})
    merged["grades_present"] = grades
    merged["validation_grade_present"] = GRADE_VALIDATION in grades
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
