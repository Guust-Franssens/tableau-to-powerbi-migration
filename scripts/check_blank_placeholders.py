"""
purpose: report Tableau calculated fields that the engine left as BLANK() placeholders, and block
         when a shipping PBIR report filter or visual field binding depends on them.
usage:   python scripts/check_blank_placeholders.py <bundle-dir> [...]
         python scripts/check_blank_placeholders.py --bundle <bundle-dir> [--bundle <bundle-dir>]
                                                    [--json <file>] [--quiet] [--warn-only]

Why this exists
---------------
The deterministic engine is allowed to refuse a Tableau calculation it cannot translate. Its safe
fallback is a calculated column or measure whose DAX body is exactly BLANK(), while the original
Tableau formula and the reason for refusal stay in the handover JSON. That is better than emitting
wrong DAX, but it is not harmless: if a PBIR report filters on that placeholder, no row can satisfy
the condition and the page can render empty while every structural validator still passes.

Detection is deliberately keyed on BOTH pieces of evidence:

* handover evidence: an entry with a name and fallback_reason, read from the engine's own
  ``report.json`` at ``workbooks[].model_translation_handoff``; and
* model evidence: a TMDL column or measure with the same name whose body is a bare BLANK().

A bare BLANK() by itself is not a finding. Hand-authored models can use it intentionally, and a scan
that flags those would train users to ignore the gate. The handover entry is what says "engine
placeholder"; the TMDL body is what says the placeholder survived into the model.

Why report.json and not handover/*.json
---------------------------------------
``<bundle>/handover/`` is NOT engine output. `run_estate.slice_handovers` writes it in phase 3, from
`report.json`, while this gate runs in phase 2 - so on a fresh run the folder does not exist yet and
a glob over it returns nothing. Reading the slices made the gate a silent no-op that only appeared
to work on a SECOND run over the same ``--output`` folder, and that second run then correlated the
PREVIOUS estate's slices against the CURRENT run's TMDL. `report.json` is written by the engine
itself, is present at phase 2, and is the source `slice_handovers` derives from, so it is the same
evidence one phase earlier and one estate fresher.

The slices remain a FALLBACK, used only when `report.json` cannot answer (absent, unreadable, or not
the engine's shape). A `report.json` that lists workbooks and no fallbacks is an authoritative "there
are none" and is never topped up from `handover/`, because that is exactly the stale-evidence path.

An input that cannot be read at all is COUNTED (``handover_unreadable``) and reported, never raised:
this check is one of four gates run by a single coordinator, and an exception here took the whole run
down before any gate printed a verdict.

Severity model
--------------
* OK: no correlated engine placeholders, and every workbook owner in report.json had handoff data.
* INCOMPLETE: no correlated engine placeholders were proven, but at least one workbook had a null or
  absent ``model_translation_handoff`` or a model owner with BLANK() bodies had no ``workbooks[]``
  entry. The standalone CLI exits 3 because this is an unmeasured region, not a clean bill of health.
* UNREFERENCED: correlated placeholders exist, but no shipping PBIR report consumes them. This is a
  documented migration gap and the standalone CLI exits 1 so automation can notice it, but
  run_estate.py does not refuse the bundle for this alone.
* REFERENCED: at least one placeholder is used by a report filter or visual field binding. This is a
  customer-facing rendering risk, so the standalone CLI exits 2 and run_estate.py blocks.

Offline limit: references are static PBIR references. This check does not prove the visual renders or
that a dependency is semantically correct after the placeholder is fixed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from bundle_corpus import shipping_reports

REPORT_VERSION = 1
REPORT_NAME = "blank-placeholder-check.json"

STATUS_OK = "OK"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_UNREFERENCED = "UNREFERENCED"
STATUS_REFERENCED = "REFERENCED"

EXIT_OK = 0
EXIT_UNREFERENCED = 1
EXIT_REFERENCED = 2
EXIT_INCOMPLETE = 3
EXIT_USAGE = 64

_OBJECT_HEAD = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kind>measure|column)\s+"
    r"(?P<name>'(?:[^']|'')+'|[^=]+?)\s*=\s*(?P<expr>[^\r\n]*)\s*$"
)

_ESCAPED_IDENT = re.compile(r"^'(?P<body>(?:[^']|'')*)'$")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_json_or_none(path: Path) -> Any | None:
    """Guarded read, exactly as `_page_names` and `_report_references` below already do it.

    An unreadable input is EVIDENCE, not an exception. Raising here aborted `run_estate.py` before
    any of its four gates printed a verdict, and the bare Python exit 1 means EXIT_ENGINE_FAILED
    ("the engine itself exited non-zero") in that script's vocabulary - an actively wrong diagnosis
    for one truncated JSON file.
    """
    try:
        return _read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


class HandoverEvidence(NamedTuple):
    """What the handover half of the correlation could, and could not, be read from."""

    entries: list[dict]
    unreadable: list[str]
    unchecked_workbooks: list[dict]
    report_workbook_owners: list[str]


def _unquote_tmdl_name(raw: str) -> str:
    """Return a TMDL identifier as Power BI displays it."""
    raw = raw.strip()
    match = _ESCAPED_IDENT.match(raw)
    if match:
        return match.group("body").replace("''", "'")
    return raw


def _owner(path: Path, root: Path) -> str:
    """Workbook/datasource owner derived from the bundle layout, not copied from handover text."""
    try:
        parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        return path.parent.name
    if len(parts) >= 2 and parts[0] == "pbip":
        return parts[1]
    if len(parts) >= 2 and parts[0] == "handover":
        return Path(parts[1]).stem
    return parts[0] if parts else path.name


def _handover_items(handoff: Any) -> list[dict]:
    """The canonical fallback list from one workbook's `model_translation_handoff`.

    The same fallback appears in two sections in current engine output: `needs_review` is the human
    triage list, while `requests` is the model-object request list and carries the target table. Use
    `requests` when present, falling back to `needs_review` for older bundles. Walking every dict in
    the payload double-counts every placeholder.
    """
    if not isinstance(handoff, dict):
        return []
    for key in ("requests", "needs_review"):
        value = handoff.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _candidate_entry(owner: str, item: dict, source: str) -> dict | None:
    """One correlation candidate, or None when the item does not claim a refused translation."""
    if not item.get("name") or not item.get("fallback_reason"):
        return None
    return {
        "owner": owner,
        "name": str(item["name"]),
        "role": str(item.get("role", "")),
        "target_table": item.get("target_table"),
        "fallback_reason": str(item["fallback_reason"]),
        "category": item.get("category"),
        "has_suggestion": item.get("has_suggestion"),
        "handover_path": source,
    }


def _workbook_owner(workbook: dict) -> str:
    """The ``pbip/<owner>/`` folder name this workbook built - what `_owner` keys TMDL objects on.

    `pbip_folder` is the engine's own answer to that question, so it is preferred over the workbook
    name. Measured across four bundles (`_bundle-208`, `_bundle-op`, `_sweep-coldrun`, `issue60`):
    present for every workbook that produced a PBIP, always shaped ``pbip/<dir>/<name>.pbip``, and
    its ``<dir>`` equalled the workbook name in all 62 cases. The name is the fallback for a bundle
    that predates the field, and for a workbook the engine never built (`pbip_folder: null`) there
    is no model to correlate against either way.
    """
    folder = workbook.get("pbip_folder")
    if isinstance(folder, str) and folder:
        parts = PurePosixPath(folder.replace("\\", "/")).parts
        if len(parts) >= 2 and parts[0] == "pbip":
            return parts[1]
    return str(workbook.get("name") or "")


def _report_json_candidates(root: Path) -> HandoverEvidence | None:
    """Candidates from ``<bundle>/report.json``, or None when it cannot answer.

    None is deliberately NOT the same as an empty candidate list: a report that lists workbooks whose
    handoff sections are present and empty is an authoritative "there are none", and must not be
    topped up from `handover/` - those slices can belong to a previous estate that reused this
    ``--output`` folder. A workbook whose handoff section is null or absent is different: that
    workbook was not checked, so it is reported as incomplete evidence instead of clean evidence.
    """
    path = root / "report.json"
    if not path.is_file():
        return None
    payload = _read_json_or_none(path)
    if payload is None:
        return HandoverEvidence([], [path.name], [], [])
    workbooks = payload.get("workbooks") if isinstance(payload, dict) else None
    if not isinstance(workbooks, list):
        return None
    entries: list[dict] = []
    unchecked: list[dict] = []
    owners: list[str] = []
    for workbook in workbooks:
        if not isinstance(workbook, dict):
            continue
        owner = _workbook_owner(workbook)
        owners.append(owner)
        if "model_translation_handoff" not in workbook:
            unchecked.append({"owner": owner, "workbook": workbook.get("name") or owner, "reason": "missing"})
            continue
        handoff = workbook.get("model_translation_handoff")
        if not isinstance(handoff, dict):
            unchecked.append({"owner": owner, "workbook": workbook.get("name") or owner, "reason": "null"})
            continue
        for item in _handover_items(handoff):
            entry = _candidate_entry(owner, item, path.name)
            if entry:
                entries.append(entry)
    return HandoverEvidence(entries, [], unchecked, owners)


def _handover_slice_candidates(root: Path) -> HandoverEvidence:
    """Candidates from the per-workbook slices `run_estate.slice_handovers` writes.

    A fallback only: these are written in phase 3, AFTER this gate runs in phase 2, so on a fresh
    run they do not exist yet. They still serve a bundle produced before the coordinator wrote a
    `report.json`, and a folder holding slices alone.
    """
    entries: list[dict] = []
    unreadable: list[str] = []
    handover = root / "handover"
    if not handover.is_dir():
        return HandoverEvidence(entries, unreadable, [], [])
    for path in sorted(handover.glob("*.json")):
        payload = _read_json_or_none(path)
        if payload is None:
            unreadable.append(path.relative_to(root).as_posix())
            continue
        workbook = payload.get("workbook") if isinstance(payload, dict) else None
        if not isinstance(workbook, dict) or "model_translation_handoff" not in workbook:
            unreadable.append(path.relative_to(root).as_posix())
            continue
        handoff = workbook.get("model_translation_handoff")
        if not isinstance(handoff, dict):
            unreadable.append(path.relative_to(root).as_posix())
            continue
        for item in _handover_items(handoff):
            entry = _candidate_entry(path.stem, item, path.relative_to(root).as_posix())
            if entry:
                entries.append(entry)
    return HandoverEvidence(entries, unreadable, [], [])


def handover_candidates(root: Path) -> HandoverEvidence:
    """Handover entries that say the engine could not translate a calculation.

    `report.json` is the primary source and `handover/*.json` the fallback; see the module docstring
    for why that order is load-bearing rather than a preference.
    """
    report_json = _report_json_candidates(root)
    if report_json is not None:
        if report_json.unreadable:
            slices = _handover_slice_candidates(root)
            return HandoverEvidence(
                slices.entries,
                [*report_json.unreadable, *slices.unreadable],
                slices.unchecked_workbooks,
                slices.report_workbook_owners,
            )
        return report_json
    return _handover_slice_candidates(root)


def _blank_objects_in_tmdl(tmdl: Path, model_dir: Path, root: Path) -> list[dict]:
    """Calculated columns/measures whose DAX body is exactly BLANK()."""
    objects: list[dict] = []
    text = tmdl.read_text(encoding="utf-8-sig", errors="replace")
    table = tmdl.stem
    for line_no, line in enumerate(text.splitlines(), 1):
        match = _OBJECT_HEAD.match(line)
        if not match or match.group("expr").strip().upper() != "BLANK()":
            continue
        objects.append(
            {
                "owner": _owner(model_dir, root),
                "model": model_dir.name,
                "table": table,
                "kind": match.group("kind"),
                "name": _unquote_tmdl_name(match.group("name")),
                "tmdl": tmdl.relative_to(root).as_posix(),
                "line": line_no,
            }
        )
    return objects


def blank_objects(root: Path) -> list[dict]:
    """Every BLANK()-only calculated object under the shipping PBIP models."""
    base = root / "pbip" if (root / "pbip").is_dir() else root
    objects: list[dict] = []
    for model_dir in sorted(p for p in base.rglob("*.SemanticModel") if p.is_dir()):
        for tmdl in sorted((model_dir / "definition" / "tables").glob("*.tmdl")):
            objects.extend(_blank_objects_in_tmdl(tmdl, model_dir, root))
    return objects


def _role_matches(entry: dict, obj: dict) -> bool:
    role = str(entry.get("role") or "").lower()
    if role == "measure":
        return obj["kind"] == "measure"
    if role:
        return obj["kind"] == "column"
    return True


def _entry_matches_object(entry: dict, obj: dict) -> bool:
    if entry["owner"] != obj["owner"] or entry["name"] != obj["name"] or not _role_matches(entry, obj):
        return False
    target = entry.get("target_table")
    return not target or target == obj["table"]


def correlated_placeholders(
    root: Path, evidence: HandoverEvidence | None = None, objects: list[dict] | None = None
) -> list[dict]:
    """The handover/TMDL pairs that together prove an engine placeholder survived.

    `evidence` and `objects` are accepted so callers that also need scope counts do not read either
    side twice.
    """
    if evidence is None:
        evidence = handover_candidates(root)
    if objects is None:
        objects = blank_objects(root)
    findings: list[dict] = []
    for entry in evidence.entries:
        for obj in objects:
            if not _entry_matches_object(entry, obj):
                continue
            findings.append({**obj, "handover": entry})
    return findings


def _page_names(report: Path) -> dict[str, str]:
    pages: dict[str, str] = {}
    for page_json in sorted((report / "definition" / "pages").glob("*/page.json")):
        try:
            page = _read_json(page_json)
        except (OSError, json.JSONDecodeError):
            continue
        pages[page_json.parent.name] = page.get("displayName") or page.get("name") or page_json.parent.name
    return pages


def _context_for(path: tuple[str, ...]) -> str | None:
    lowered = {part.lower() for part in path}
    if {"filter", "filters", "where", "condition"} & lowered:
        return "filter"
    if "projections" in lowered:
        return "visual_field"
    if {"sort", "sortdefinition"} & lowered:
        return "sort"
    return None


def _source_ref_entity(source_ref: dict, aliases: dict[str, str]) -> str | None:
    if "Entity" in source_ref:
        return source_ref["Entity"]
    if "Source" in source_ref:
        return aliases.get(source_ref["Source"])
    return None


def _field_reference(kind: str, payload: Any, aliases: dict[str, str]) -> tuple[str, str, str] | None:
    if not isinstance(payload, dict) or not payload.get("Property"):
        return None
    expr = payload.get("Expression")
    source_ref = expr.get("SourceRef") if isinstance(expr, dict) else None
    if not isinstance(source_ref, dict):
        return None
    entity = _source_ref_entity(source_ref, aliases)
    if not entity:
        return None
    object_kind = "measure" if kind == "Measure" else "column"
    return object_kind, str(entity), str(payload["Property"])


def _references_in_json(value: Any, aliases: dict[str, str] | None = None, path: tuple[str, ...] = ()) -> list[dict]:
    """Static semantic references in one PBIR JSON document."""
    aliases = dict(aliases or {})
    references: list[dict] = []
    if isinstance(value, dict):
        for item in value.get("From", []) if isinstance(value.get("From"), list) else []:
            if isinstance(item, dict) and item.get("Name") and item.get("Entity"):
                aliases[str(item["Name"])] = str(item["Entity"])
        for kind in ("Column", "Measure"):
            ref = _field_reference(kind, value.get(kind), aliases)
            if ref:
                object_kind, table, name = ref
                references.append(
                    {"kind": object_kind, "table": table, "name": name, "usage": _context_for(path) or "other"}
                )
        for key, child in value.items():
            references.extend(_references_in_json(child, aliases, (*path, str(key))))
    elif isinstance(value, list):
        for child in value:
            references.extend(_references_in_json(child, aliases, path))
    return references


def _report_context(path: Path, report: Path, page_names: dict[str, str]) -> dict:
    parts = path.relative_to(report).parts
    page_id = ""
    visual_id = ""
    if "pages" in parts:
        index = parts.index("pages")
        if len(parts) > index + 1:
            page_id = parts[index + 1]
    if "visuals" in parts:
        index = parts.index("visuals")
        if len(parts) > index + 1:
            visual_id = parts[index + 1]
    visual_type = None
    try:
        payload = _read_json(path)
        visual_type = payload.get("visual", {}).get("visualType") if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "report": report.name,
        "page": page_names.get(page_id, page_id),
        "visual": visual_id,
        "visual_type": visual_type,
        "file": path.relative_to(report.parent.parent).as_posix()
        if report.parent.parent in path.parents
        else str(path),
    }


def _report_references(report: Path) -> list[dict]:
    page_names = _page_names(report)
    references: list[dict] = []
    for path in sorted((report / "definition").rglob("*.json")):
        try:
            payload = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        context = _report_context(path, report, page_names)
        for ref in _references_in_json(payload):
            references.append({**ref, **context})
    return references


find_reports = shipping_reports


def _matches_finding(ref: dict, finding: dict) -> bool:
    return ref["kind"] == finding["kind"] and ref["table"] == finding["table"] and ref["name"] == finding["name"]


def attach_dependencies(root: Path, findings: list[dict]) -> None:
    """Mutate findings with static PBIR dependency lists."""
    references_by_owner: dict[str, list[dict]] = {}
    for report in find_reports(root):
        references_by_owner.setdefault(_owner(report, root), []).extend(_report_references(report))
    for finding in findings:
        deps = [ref for ref in references_by_owner.get(finding["owner"], []) if _matches_finding(ref, finding)]
        material = [d for d in deps if d["usage"] in {"filter", "visual_field"}]
        finding["dependencies"] = deps
        finding["material_dependencies"] = material
        finding["severity"] = STATUS_REFERENCED if material else STATUS_UNREFERENCED


def _owner_blank_summaries(objects: list[dict], owners: list[str]) -> list[dict]:
    """Per-owner BLANK() counts for owners the handover evidence did not cover."""
    summaries: list[dict] = []
    for owner in owners:
        blanks = [obj for obj in objects if obj["owner"] == owner]
        summaries.append({"owner": owner, "blank_objects": len(blanks)})
    return summaries


def scan(root: Path) -> dict:
    """Scan a bundle and return the machine-readable report."""
    root = root.resolve()
    evidence = handover_candidates(root)
    objects = blank_objects(root)
    findings = correlated_placeholders(root, evidence, objects)
    attach_dependencies(root, findings)
    referenced = [finding for finding in findings if finding["severity"] == STATUS_REFERENCED]
    unchecked_workbooks = []
    for workbook in evidence.unchecked_workbooks:
        blank_count = sum(1 for obj in objects if obj["owner"] == workbook["owner"])
        unchecked_workbooks.append({**workbook, "blank_objects": blank_count})
    skipped_owners = sorted({obj["owner"] for obj in objects} - set(evidence.report_workbook_owners))
    skipped_datasources = _owner_blank_summaries(objects, skipped_owners) if evidence.report_workbook_owners else []
    incomplete = bool(unchecked_workbooks or skipped_datasources)
    status = (
        STATUS_REFERENCED
        if referenced
        else (STATUS_UNREFERENCED if findings else (STATUS_INCOMPLETE if incomplete else STATUS_OK))
    )
    return {
        "version": REPORT_VERSION,
        "root": str(root),
        "status": status,
        "placeholders_found": len(findings),
        "placeholders_referenced": len(referenced),
        "handover_unreadable": len(evidence.unreadable),
        "handover_unreadable_paths": evidence.unreadable,
        "workbooks_reported": len(evidence.report_workbook_owners),
        "workbooks_unchecked": len(unchecked_workbooks),
        "workbooks_unchecked_blank_objects": sum(item["blank_objects"] for item in unchecked_workbooks),
        "workbooks_unchecked_details": unchecked_workbooks,
        "skipped_shared_datasources": len(skipped_datasources),
        "skipped_shared_datasource_blank_objects": sum(item["blank_objects"] for item in skipped_datasources),
        "skipped_shared_datasource_details": skipped_datasources,
        "findings": findings,
    }


def render(report: dict) -> str:
    """Human-readable verdict: which placeholders, why, and what depends on them."""
    lines = [f"BLANK-PLACEHOLDER CHECK: {report['status']} - {report['placeholders_found']} placeholder(s)"]
    unreadable = report.get("handover_unreadable_paths") or []
    if unreadable:
        # Printed in EVERY branch, including OK: an unreadable input means the verdict below was
        # reached without that evidence, so a bare "OK" would overstate what was actually checked.
        lines.append(
            f"  WARNING: {len(unreadable)} handover input(s) could not be read, so any placeholder "
            f"they name is UNCORRELATED here: {', '.join(unreadable)}"
        )
    unchecked = report.get("workbooks_unchecked_details") or []
    if unchecked:
        lines.append(
            f"  WARNING: {report['workbooks_unchecked']} of {report['workbooks_reported']} workbook(s) carry "
            "no model_translation_handoff - NOT CHECKED "
            f"({report['workbooks_unchecked_blank_objects']} BLANK() body/bodies in those workbook(s))."
        )
        for item in unchecked:
            lines.append(
                f"    - {item['owner']}: {item['blank_objects']} BLANK() body/bodies; handoff {item['reason']}"
            )
    skipped = report.get("skipped_shared_datasource_details") or []
    if skipped:
        lines.append(
            f"  WARNING: {report['skipped_shared_datasources']} owner(s) with no workbooks[] entry were "
            "outside this handover check - NOT CHECKED "
            f"({report['skipped_shared_datasource_blank_objects']} BLANK() body/bodies)."
        )
        for item in skipped:
            lines.append(f"    - {item['owner']}: {item['blank_objects']} BLANK() body/bodies")
    if report["status"] == STATUS_OK:
        lines.append("  OK - no handover-backed BLANK() placeholder survived into the model.")
        return "\n".join(lines)
    if report["status"] == STATUS_INCOMPLETE and not report["findings"]:
        lines.append(
            "  INCOMPLETE - no handover-backed placeholders were proven, but part of the estate was not checked."
        )
        return "\n".join(lines)
    lines.append(
        "  Severity model: unreferenced placeholders are documented gaps; references from filters or "
        "visual field bindings block because they can render a page or visual empty."
    )
    for finding in report["findings"]:
        handover = finding["handover"]
        lines.append(
            f"  - {finding['owner']}: {finding['kind']} '{finding['table']}'[{finding['name']}] ({finding['severity']})"
        )
        lines.append(f"      reason: {handover['fallback_reason']}")
        lines.append(
            f"      handover: {handover['handover_path']}  category={handover.get('category')} "
            f"has_suggestion={handover.get('has_suggestion')}"
        )
        lines.append(f"      tmdl: {finding['tmdl']}:{finding['line']}")
        if not finding["dependencies"]:
            lines.append("      dependencies: none found in shipping PBIR")
            continue
        for dep in finding["dependencies"]:
            marker = "BLOCKING" if dep["usage"] in {"filter", "visual_field"} else "not-blocking"
            where = f"page={dep.get('page') or '?'} visual={dep.get('visual') or '?'}"
            lines.append(f"      dependency ({marker}, {dep['usage']}): {where} file={dep['file']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="bundle folder(s)")
    parser.add_argument("--bundle", dest="bundles", action="append", type=Path, help="bundle folder (repeatable)")
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    parser.add_argument("--warn-only", action="store_true", help="always exit 0")
    args = parser.parse_args(argv)

    paths = [*(args.paths or []), *(args.bundles or [])]
    if not paths:
        parser.error("at least one bundle path is required")

    missing = [str(path) for path in paths if not path.is_dir()]
    if missing:
        print(f"ERROR: not a directory: {', '.join(missing)}", file=sys.stderr)
        return EXIT_USAGE

    reports = [scan(path) for path in paths]
    merged: dict = {
        "version": REPORT_VERSION,
        "status": STATUS_OK,
        "placeholders_found": 0,
        "placeholders_referenced": 0,
        "handover_unreadable": 0,
        "handover_unreadable_paths": [],
        "workbooks_reported": 0,
        "workbooks_unchecked": 0,
        "workbooks_unchecked_blank_objects": 0,
        "workbooks_unchecked_details": [],
        "skipped_shared_datasources": 0,
        "skipped_shared_datasource_blank_objects": 0,
        "skipped_shared_datasource_details": [],
        "findings": [],
    }
    for report in reports:
        merged["placeholders_found"] += report["placeholders_found"]
        merged["placeholders_referenced"] += report["placeholders_referenced"]
        merged["handover_unreadable"] += report["handover_unreadable"]
        merged["handover_unreadable_paths"].extend(report["handover_unreadable_paths"])
        merged["workbooks_reported"] += report["workbooks_reported"]
        merged["workbooks_unchecked"] += report["workbooks_unchecked"]
        merged["workbooks_unchecked_blank_objects"] += report["workbooks_unchecked_blank_objects"]
        merged["workbooks_unchecked_details"].extend(report["workbooks_unchecked_details"])
        merged["skipped_shared_datasources"] += report["skipped_shared_datasources"]
        merged["skipped_shared_datasource_blank_objects"] += report["skipped_shared_datasource_blank_objects"]
        merged["skipped_shared_datasource_details"].extend(report["skipped_shared_datasource_details"])
        merged["findings"].extend(report["findings"])
    incomplete = bool(merged["workbooks_unchecked"] or merged["skipped_shared_datasources"])
    merged["status"] = (
        STATUS_REFERENCED
        if merged["placeholders_referenced"]
        else (STATUS_UNREFERENCED if merged["placeholders_found"] else (STATUS_INCOMPLETE if incomplete else STATUS_OK))
    )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged))
    if args.warn_only or merged["status"] == STATUS_OK:
        return EXIT_OK
    if merged["status"] == STATUS_REFERENCED:
        return EXIT_REFERENCED
    if merged["status"] == STATUS_INCOMPLETE:
        return EXIT_INCOMPLETE
    return EXIT_UNREFERENCED


if __name__ == "__main__":
    raise SystemExit(main())
