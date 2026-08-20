"""
purpose: catch the CROSS-LAYER defect neither layer's validator can see - a PBIR field reference
         that resolves to no field in the semantic model beside it, with case-only near-misses
         called out separately because that is the signature of a model-layer rename.
usage:   python scripts/check_field_bindings.py <bundle-or-report-dir> [...]
         python scripts/check_field_bindings.py --model <x.SemanticModel> --report <x.Report>
                                                [--json <file>] [--quiet] [--warn-only]

Why this exists
---------------
Normalising a shared table's column names at the MODEL layer (issue #236: folding Snowflake
identifiers with `Table.TransformColumnNames(..., Text.Upper(_))`) silently invalidates every PBIR
binding already written against the old casing. Both single-layer gates stay green:

    check_datamodel.py   -> the model is structurally fine
    check_pbir_valid.py  -> the report is structurally fine
    Power BI Desktop     -> "Fields that need to be fixed", per visual, at OPEN time

The inconsistency lives BETWEEN the layers, which is exactly where nothing looked. Measured in the
field on a 12-workbook estate: PBIR referenced `SLA_ACPU_Down_Duration` while the post-fix column
was `SLA_ACPU_DOWN_DURATION`; a second workbook showed the same shape across ~15 fields.

Why case-insensitive near-misses are their own category
-------------------------------------------------------
A reference that fails exactly but matches case-insensitively is almost never a missing field - it
is a rename that was applied to one layer only. Printing BOTH spellings turns a per-visual Desktop
modal into a mechanical find-and-replace, so this category is labelled and rendered separately from
a genuinely absent field, which is a different (and usually larger) problem.

Scope: `pbip/` only, and a report is only checked against ITS model
------------------------------------------------------------------
Like `check_pbir_valid.py`, a bundle is scanned through `<bundle>/pbip/` because only that ships;
`<bundle>/reports/` is the engine's reference-only baseline with no model beside it, so every
reference there would report unresolved and say nothing about the deliverable. The model for a
report is resolved from its own `definition.pbir` `datasetReference.byPath`, falling back to a
sibling `<name>.SemanticModel` - never "the first model found nearby", which would silently grade a
report against a model it does not ship with.

What it will NOT tell you
-------------------------
That the report is CORRECT. Names resolving is necessary, not sufficient: a binding can resolve to
the right name on the wrong table's twin column, aggregate the wrong way, or filter to nothing. It
also cannot see anything a name does not carry - data types, row counts, or whether the model loads
at all (`check_empty_model.py` is the gate for that).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPORT_NAME = "field-binding-check.json"

# TMDL member declarations. Names are either bare or single-quoted; a measure/calculated column
# carries `= <DAX>` on the same line, which is why the name group stops at `=`. An apostrophe INSIDE
# a quoted name is doubled (`column 'Sondheim''s Work'`, measured in
# examples/broadway-stage-to-screen), so a naive `'[^']*'` truncates the name and then reports the
# report's perfectly good binding as missing - a false positive on committed, shipping data.
_NAME = r"'(?:[^']|'')*'|[^\s=]+"
_TABLE_RE = re.compile(rf"^table\s+(?P<name>{_NAME})\s*$")
_MEMBER_RE = re.compile(rf"^(?P<indent>[\t ]+)(?P<kind>column|measure|hierarchy|level)\s+(?P<name>{_NAME})")

# PBIR reference nodes. `Column`/`Measure` carry `Property`; `HierarchyLevel` carries `Level` and
# wraps a `Hierarchy` node. Everything else (`Aggregation`, `FillRule`, `Subquery`, ...) merely
# nests one of these, so the walk below is generic rather than path-driven.
_SCALAR_KINDS = ("Column", "Measure")


@dataclass
class TableFields:
    """Every name a PBIR reference can legally resolve to on one table."""

    columns: set[str] = field(default_factory=set)
    measures: set[str] = field(default_factory=set)
    hierarchies: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class ModelFields:
    """The semantic model reduced to the only thing this gate compares: names."""

    tables: dict[str, TableFields] = field(default_factory=dict)

    def table(self, name: str) -> TableFields | None:
        """Exact-case lookup of one table."""
        return self.tables.get(name)

    def table_ci(self, name: str) -> tuple[str, TableFields] | None:
        """Case-insensitive lookup, returning the model's own spelling."""
        lowered = name.casefold()
        for actual, fields_ in self.tables.items():
            if actual.casefold() == lowered:
                return actual, fields_
        return None


@dataclass
class FieldRef:
    """One field reference found in PBIR, with enough context to fix it by hand."""

    kind: str
    entity: str
    prop: str
    file: Path
    hierarchy: str | None = None


def _unquote(name: str) -> str:
    """Strip TMDL's single-quoting from an object name."""
    name = name.strip()
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        return name[1:-1].replace("''", "'")
    return name


def parse_model(model_dir: Path) -> ModelFields:
    """Collect table columns, measures and hierarchy levels from a `.SemanticModel`'s TMDL.

    Members are recognised by the block's own minimum indent, so a multi-line DAX expression that
    happens to contain the word `measure` deeper in its body cannot be mistaken for a declaration.
    """
    model = ModelFields()
    definition = model_dir / "definition"
    root = definition if definition.is_dir() else model_dir
    for path in sorted(root.rglob("*.tmdl")):
        _parse_tmdl_file(path, model)
    return model


def _parse_tmdl_file(path: Path, model: ModelFields) -> None:
    """Fold one TMDL file's table blocks into `model`."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    lines = text.splitlines()
    blocks: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in lines:
        table = _TABLE_RE.match(line)
        if table:
            current = []
            blocks.append((_unquote(table.group("name")), current))
        elif current is not None:
            current.append(line)
    for name, body in blocks:
        _parse_table_block(name, body, model)


def _parse_table_block(name: str, body: list[str], model: ModelFields) -> None:
    """Record the members declared at the block's own top level."""
    fields_ = model.tables.setdefault(name, TableFields())
    matches = [m for m in (_MEMBER_RE.match(line) for line in body) if m]
    if not matches:
        return
    member_indent = min(len(m.group("indent").expandtabs(4)) for m in matches)
    last_hierarchy: str | None = None
    for match in matches:
        if len(match.group("indent").expandtabs(4)) != member_indent:
            if match.group("kind") == "level" and last_hierarchy is not None:
                fields_.hierarchies[last_hierarchy].add(_unquote(match.group("name")))
            continue
        kind = match.group("kind")
        member = _unquote(match.group("name"))
        if kind == "column":
            fields_.columns.add(member)
        elif kind == "measure":
            fields_.measures.add(member)
        elif kind == "hierarchy":
            last_hierarchy = member
            fields_.hierarchies.setdefault(member, set())


def iter_references(report_dir: Path) -> list[FieldRef]:
    """Every field reference in a `.Report`, from any JSON the report definition ships.

    Visual query projections are only the common case: filters (including nested subqueries), sort
    definitions, conditional-formatting `FillRule` inputs, data-point selectors and page/report
    level filters all carry the same `Column`/`Measure` node, so the walk is shape-driven.
    """
    refs: list[FieldRef] = []
    definition = report_dir / "definition"
    root = definition if definition.is_dir() else report_dir
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        _walk(payload, {}, path, refs)
    return refs


def _source_scope(node: dict[str, Any], scope: dict[str, str]) -> dict[str, str]:
    """Extend the alias->entity map with this query's `From` clause.

    A filter's `Where`/`OrderBy` refers to tables by the alias its own `From` declares
    (`SourceRef.Source`), and a `Subquery` opens a nested scope. Without this, every aliased
    reference would be reported as an unknown table - a gate that cries wolf on valid PBIR.
    """
    entries = node.get("From")
    if not isinstance(entries, list):
        return scope
    nested = dict(scope)
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("Name"), str) and isinstance(entry.get("Entity"), str):
            nested[entry["Name"]] = entry["Entity"]
    return nested


def _entity_of(expression: Any, scope: dict[str, str]) -> str | None:
    """Resolve a `SourceRef` to a table name, through an alias when necessary."""
    if not isinstance(expression, dict):
        return None
    source_ref = expression.get("SourceRef")
    if not isinstance(source_ref, dict):
        return None
    entity = source_ref.get("Entity")
    if isinstance(entity, str):
        return entity
    alias = source_ref.get("Source")
    if isinstance(alias, str):
        return scope.get(alias)
    return None


def _walk(node: Any, scope: dict[str, str], path: Path, refs: list[FieldRef]) -> None:
    """Depth-first walk collecting reference nodes under the alias scope in force."""
    if isinstance(node, list):
        for item in node:
            _walk(item, scope, path, refs)
        return
    if not isinstance(node, dict):
        return
    scope = _source_scope(node, scope)
    for kind in _SCALAR_KINDS:
        inner = node.get(kind)
        if isinstance(inner, dict) and isinstance(inner.get("Property"), str):
            entity = _entity_of(inner.get("Expression"), scope)
            if entity:
                refs.append(FieldRef(kind=kind, entity=entity, prop=inner["Property"], file=path))
    level = node.get("HierarchyLevel")
    if isinstance(level, dict) and isinstance(level.get("Level"), str):
        hierarchy = level.get("Expression", {}).get("Hierarchy") if isinstance(level.get("Expression"), dict) else None
        if isinstance(hierarchy, dict) and isinstance(hierarchy.get("Hierarchy"), str):
            entity = _entity_of(hierarchy.get("Expression"), scope)
            if entity:
                refs.append(
                    FieldRef(
                        kind="HierarchyLevel",
                        entity=entity,
                        prop=level["Level"],
                        file=path,
                        hierarchy=hierarchy["Hierarchy"],
                    )
                )
    for value in node.values():
        _walk(value, scope, path, refs)


def _candidates(fields_: TableFields, ref: FieldRef) -> set[str]:
    """The model names a reference of this kind may legally resolve to.

    A measure is matched against measures AND columns on purpose: PBIR distinguishes the two, but a
    model-layer rename is the defect being hunted, and reporting "this exists, as a column" is far
    more actionable than "missing" when the name is right.
    """
    if ref.kind == "HierarchyLevel":
        return set(fields_.hierarchies.get(ref.hierarchy or "", set()))
    return fields_.columns | fields_.measures


def _finding(ref: FieldRef, status: str, detail: str, model_spelling: str | None = None) -> dict[str, Any]:
    """One machine-readable finding."""
    entry: dict[str, Any] = {
        "status": status,
        "kind": ref.kind,
        "entity": ref.entity,
        "property": ref.prop,
        "report_spelling": f"{ref.entity}[{ref.prop}]",
        "file": str(ref.file),
        "detail": detail,
    }
    if ref.hierarchy:
        entry["hierarchy"] = ref.hierarchy
    if model_spelling is not None:
        entry["model_spelling"] = model_spelling
    return entry


def resolve_reference(model: ModelFields, ref: FieldRef) -> dict[str, Any]:
    """Grade one reference: `resolved`, `near_miss` (case-only) or `missing`."""
    fields_ = model.table(ref.entity)
    entity_spelling = ref.entity
    entity_exact = fields_ is not None
    if fields_ is None:
        found = model.table_ci(ref.entity)
        if found is None:
            return _finding(ref, "missing", f"no table named '{ref.entity}' in the model")
        entity_spelling, fields_ = found

    names = _candidates(fields_, ref)
    if ref.prop in names and entity_exact:
        return _finding(ref, "resolved", "exact match")

    prop_spelling = ref.prop
    if ref.prop not in names:
        lowered = ref.prop.casefold()
        matches = sorted(n for n in names if n.casefold() == lowered)
        if not matches:
            where = f"'{entity_spelling}'"
            if ref.kind == "HierarchyLevel":
                where = f"hierarchy '{ref.hierarchy}' on {where}"
            return _finding(ref, "missing", f"no field named '{ref.prop}' on {where}")
        prop_spelling = matches[0]

    return _finding(
        ref,
        "near_miss",
        "case-only mismatch: the model spells this differently",
        model_spelling=f"{entity_spelling}[{prop_spelling}]",
    )


def model_for_report(report_dir: Path) -> Path | None:
    """Resolve the model a report actually ships with, via `definition.pbir` then a sibling."""
    pbir = report_dir / "definition.pbir"
    if pbir.is_file():
        try:
            payload = json.loads(pbir.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        by_path = payload.get("datasetReference", {}).get("byPath", {}) if isinstance(payload, dict) else {}
        rel = by_path.get("path") if isinstance(by_path, dict) else None
        if isinstance(rel, str):
            candidate = (report_dir / rel).resolve()
            if candidate.is_dir():
                return candidate
    sibling = report_dir.parent / f"{report_dir.name[: -len('.Report')]}.SemanticModel"
    return sibling if sibling.is_dir() else None


def find_reports(root: Path) -> list[Path]:
    """The `.Report` folders that SHIP under `root` - `pbip/` only for a bundle."""
    root = root.resolve()
    if root.name.endswith(".Report"):
        return [root]
    pbip = root / "pbip"
    base = pbip if pbip.is_dir() else root
    return sorted({p.resolve() for p in base.rglob("*.Report") if p.is_dir()})


def check_pair(report_dir: Path, model_dir: Path) -> dict[str, Any]:
    """Grade every reference in ONE report against ONE model.

    A pair that yields NOTHING to compare - no model tables, or no field reference anywhere in the
    report - is `SKIPPED`, never `OK`. Review finding: with `--model` and `--report` transposed (a
    one-keystroke slip, both paths perfectly real) the old code parsed no model and found no
    references, then printed "every PBIR field reference resolves" and exited 0 for a report it had
    never opened. An affirmative verdict must mean something was actually checked.
    """
    model = parse_model(model_dir)
    findings = [resolve_reference(model, ref) for ref in iter_references(report_dir)]
    unresolved = [f for f in findings if f["status"] != "resolved"]
    if not findings or not model.tables:
        reason = "no tables parsed from the model" if not model.tables else "no field reference found in the report"
        return {
            "report": str(report_dir),
            "model": str(model_dir),
            "status": "SKIPPED",
            "reason": reason,
            "references": len(findings),
            "near_misses": 0,
            "missing": 0,
            "findings": [],
        }
    return {
        "report": str(report_dir),
        "model": str(model_dir),
        "status": "UNRESOLVED" if unresolved else "OK",
        "references": len(findings),
        "near_misses": sum(1 for f in findings if f["status"] == "near_miss"),
        "missing": sum(1 for f in findings if f["status"] == "missing"),
        "findings": _dedupe(unresolved),
    }


def _dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per distinct defect, carrying the files it was seen in.

    A renamed column is referenced by every visual that used it, so the raw list is dominated by
    repeats of one fix. Collapsing them keeps the verdict readable without losing the locations.
    """
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for finding in findings:
        key = (finding["status"], finding["kind"], finding["entity"], finding["property"])
        entry = merged.get(key)
        if entry is None:
            entry = {k: v for k, v in finding.items() if k != "file"}
            entry["files"] = []
            entry["occurrences"] = 0
            merged[key] = entry
        entry["occurrences"] += 1
        if finding["file"] not in entry["files"]:
            entry["files"].append(finding["file"])
    return list(merged.values())


def scan(root: Path) -> dict[str, Any]:
    """Check every shipping report under `root` against the model it ships with."""
    pairs = []
    skipped = []
    for report_dir in find_reports(root):
        model_dir = model_for_report(report_dir)
        if model_dir is None:
            skipped.append({"report": str(report_dir), "reason": "no semantic model beside this report"})
            continue
        pairs.append(check_pair(report_dir, model_dir))
    return _merge(pairs, skipped)


def _merge(pairs: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-report results into one verdict, keeping ungraded pairs out of the pass count."""
    graded = [p for p in pairs if p["status"] != "SKIPPED"]
    skipped = list(skipped) + [
        {"report": p["report"], "model": p["model"], "reason": p["reason"]} for p in pairs if p["status"] == "SKIPPED"
    ]
    unresolved = [p for p in graded if p["status"] == "UNRESOLVED"]
    if not graded:
        status = "SKIPPED"
    else:
        status = "UNRESOLVED" if unresolved else "OK"
    return {
        "status": status,
        "reports_scanned": len(graded),
        "reports_unresolved": len(unresolved),
        "near_misses": sum(p["near_misses"] for p in graded),
        "missing": sum(p["missing"] for p in graded),
        "reports": graded,
        "skipped": skipped,
    }


def render(report: dict[str, Any]) -> str:
    """Human-readable verdict, in the shape the sibling gates use."""
    if report["status"] == "SKIPPED":
        reasons = "; ".join(s["reason"] for s in report.get("skipped", [])) or "no report found"
        return f"FIELD BINDING CHECK: SKIPPED - nothing to check ({reasons})"
    scanned = report["reports_scanned"]
    if report["status"] == "OK":
        return f"FIELD BINDING CHECK: OK - every PBIR field reference in {scanned} report(s) resolves in its model."
    lines = [
        f"FIELD BINDING CHECK: UNRESOLVED - {report['reports_unresolved']} of {scanned} report(s) "
        f"reference fields their model does not have "
        f"({report['near_misses']} case-only near-miss(es), {report['missing']} missing)",
    ]
    for one in report["reports"]:
        if one["status"] != "UNRESOLVED":
            continue
        lines.append(f"  {Path(one['report']).name}  (model: {Path(one['model']).name})")
        lines += _render_findings(one["findings"], "near_miss")
        lines += _render_findings(one["findings"], "missing")
    lines.append(
        "  A CASE-ONLY near-miss is a model-layer rename that never reached the report: rewrite the\n"
        "  PBIR spelling to the model's, do NOT rename the model back - the fold was deliberate."
    )
    return "\n".join(lines)


def _render_findings(findings: list[dict[str, Any]], status: str) -> list[str]:
    """Render one category, printing BOTH spellings for a near-miss."""
    label = "NEAR-MISS (case only)" if status == "near_miss" else "MISSING"
    lines = []
    for finding in findings:
        if finding["status"] != status:
            continue
        detail = f"report: {finding['report_spelling']}"
        if finding.get("model_spelling"):
            detail += f"   model: {finding['model_spelling']}"
        lines.append(f"    - {label}: {detail}  [{finding['kind']} x{finding['occurrences']}]")
    return lines


def _pair_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Grade the explicitly-named `--model` / `--report` pair."""
    return _merge([check_pair(args.report.resolve(), args.model.resolve())], [])


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="bundle folder(s) or .Report folder(s)")
    parser.add_argument("--model", type=Path, help="explicit .SemanticModel folder (use with --report)")
    parser.add_argument("--report", type=Path, help="explicit .Report folder (use with --model)")
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    parser.add_argument("--warn-only", action="store_true", help="always exit 0")
    args = parser.parse_args(argv)

    if bool(args.model) != bool(args.report):
        parser.error("--model and --report must be given together")
    if not args.paths and not args.model:
        parser.error("give a bundle/report path, or --model with --report")
    # A path that does not exist must NEVER produce a verdict. `rglob` on a missing folder yields
    # nothing, so without this a typo'd `--report` reads as "0 references, all resolved" and the
    # gate prints OK and exits 0 for a report it never opened - the one failure mode that would
    # make this check worse than not running it.
    for label, path in (("--model", args.model), ("--report", args.report), *(("path", p) for p in args.paths)):
        if path is not None and not path.is_dir():
            parser.error(f"{label} {path} is not a directory")

    if args.model:
        merged = _pair_from_args(args)
    else:
        scans = [scan(path) for path in args.paths]
        merged = _merge(
            [one for s in scans for one in s["reports"]],
            [one for s in scans for one in s["skipped"]],
        )

    if args.json:
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged))
    if args.warn_only or merged["status"] != "UNRESOLVED":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
