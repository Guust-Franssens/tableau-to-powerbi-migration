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

* handover evidence: an entry with a name and fallback_reason from handover/*.json; and
* model evidence: a TMDL column or measure with the same name whose body is a bare BLANK().

A bare BLANK() by itself is not a finding. Hand-authored models can use it intentionally, and a scan
that flags those would train users to ignore the gate. The handover entry is what says "engine
placeholder"; the TMDL body is what says the placeholder survived into the model.

Severity model
--------------
* OK: no correlated engine placeholders.
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
from pathlib import Path
from typing import Any

REPORT_VERSION = 1
REPORT_NAME = "blank-placeholder-check.json"

STATUS_OK = "OK"
STATUS_UNREFERENCED = "UNREFERENCED"
STATUS_REFERENCED = "REFERENCED"

EXIT_OK = 0
EXIT_UNREFERENCED = 1
EXIT_REFERENCED = 2
EXIT_USAGE = 64

_OBJECT_HEAD = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kind>measure|column)\s+"
    r"(?P<name>'(?:[^']|'')+'|[^=]+?)\s*=\s*(?P<expr>[^\r\n]*)\s*$"
)

_ESCAPED_IDENT = re.compile(r"^'(?P<body>(?:[^']|'')*)'$")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


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


def _handover_items(payload: Any) -> list[dict]:
    """The canonical handover fallback list from one workbook handover JSON.

    The same fallback appears in two sections in current engine output: `needs_review` is the human
    triage list, while `requests` is the model-object request list and carries the target table. Use
    `requests` when present, falling back to `needs_review` for older bundles. Walking every dict in
    the file double-counts every placeholder.
    """
    handoff = payload.get("workbook", {}).get("model_translation_handoff") if isinstance(payload, dict) else None
    if not isinstance(handoff, dict):
        return []
    for key in ("requests", "needs_review"):
        value = handoff.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def handover_candidates(root: Path) -> list[dict]:
    """Handover entries that say the engine could not translate a calculation."""
    entries: list[dict] = []
    handover = root / "handover"
    if not handover.is_dir():
        return entries
    for path in sorted(handover.glob("*.json")):
        payload = _read_json(path)
        for item in _handover_items(payload):
            if not item.get("name") or not item.get("fallback_reason"):
                continue
            entries.append(
                {
                    "owner": path.stem,
                    "name": str(item["name"]),
                    "role": str(item.get("role", "")),
                    "target_table": item.get("target_table"),
                    "fallback_reason": str(item["fallback_reason"]),
                    "category": item.get("category"),
                    "has_suggestion": item.get("has_suggestion"),
                    "handover_path": path.relative_to(root).as_posix(),
                }
            )
    return entries


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


def correlated_placeholders(root: Path) -> list[dict]:
    """The handover/TMDL pairs that together prove an engine placeholder survived."""
    objects = blank_objects(root)
    findings: list[dict] = []
    for entry in handover_candidates(root):
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


def find_reports(root: Path) -> list[Path]:
    """Shipping report folders under pbip/, or a direct .Report target."""
    root = root.resolve()
    if root.name.endswith(".Report"):
        return [root]
    base = root / "pbip" if (root / "pbip").is_dir() else root
    return sorted({p.resolve() for p in base.rglob("*.Report") if p.is_dir()})


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


def scan(root: Path) -> dict:
    """Scan a bundle and return the machine-readable report."""
    root = root.resolve()
    findings = correlated_placeholders(root)
    attach_dependencies(root, findings)
    referenced = [finding for finding in findings if finding["severity"] == STATUS_REFERENCED]
    status = STATUS_REFERENCED if referenced else (STATUS_UNREFERENCED if findings else STATUS_OK)
    return {
        "version": REPORT_VERSION,
        "root": str(root),
        "status": status,
        "placeholders_found": len(findings),
        "placeholders_referenced": len(referenced),
        "findings": findings,
    }


def render(report: dict) -> str:
    """Human-readable verdict: which placeholders, why, and what depends on them."""
    lines = [f"BLANK-PLACEHOLDER CHECK: {report['status']} - {report['placeholders_found']} placeholder(s)"]
    if report["status"] == STATUS_OK:
        lines.append("  OK - no handover-backed BLANK() placeholder survived into the model.")
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
        "findings": [],
    }
    for report in reports:
        merged["placeholders_found"] += report["placeholders_found"]
        merged["placeholders_referenced"] += report["placeholders_referenced"]
        merged["findings"].extend(report["findings"])
    merged["status"] = (
        STATUS_REFERENCED
        if merged["placeholders_referenced"]
        else (STATUS_UNREFERENCED if merged["placeholders_found"] else STATUS_OK)
    )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged))
    if args.warn_only or merged["status"] == STATUS_OK:
        return EXIT_OK
    return EXIT_REFERENCED if merged["status"] == STATUS_REFERENCED else EXIT_UNREFERENCED


if __name__ == "__main__":
    raise SystemExit(main())
