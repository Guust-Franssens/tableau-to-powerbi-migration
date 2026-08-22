"""
purpose: census the `= BLANK()` stub measures the deterministic engine could not translate - per
         table and model-wide, in the `64/89 (72%)` shape already in field use - and split them into
         ACTIONABLE (the original Tableau formula survived as `annotation TableauFormula`, so
         someone can translate it now) and DEAD END (nothing survived; recover it from the Tableau
         workbook). One scary number becomes a work queue plus a smaller escalation list.
usage:   python scripts/check_stub_measures.py <bundle-or-model-dir> [...]
         python scripts/check_stub_measures.py --model <x.SemanticModel>
                                               [--json <file>] [--quiet] [--verbose] [--strict]

Why this exists
---------------
When the engine cannot translate a Tableau calculated field it emits a placeholder whose whole body
is `BLANK()`, so the model still loads. On a live estate that is the single largest remaining body
of work - roughly 29.3% of ~1,067 measures - and it was tracked far less rigorously than field
binding correctness, which does have a tool (`check_field_bindings.py`, issue #236).

Detection rule: is the WHOLE expression a BLANK() call
------------------------------------------------------
A substring search for `BLANK()` is wrong, and wrong in the expensive direction. Legitimate authored
DAX mentions `BLANK()` constantly - `IF(ISBLANK([x]), BLANK(), [y])`, `DIVIDE(a, b, BLANK())` - and
an ad-hoc sweep that asked "does this text appear" reported an already-translated measure (ACMU
`Selected Measure`) as still stubbed. Nothing caught it but a manual re-read of the file. A census
whose numbers get quoted in a status report must not do that, so this asks a different question:

    strip comments -> collapse whitespace -> strip redundant outer parens -> FULL-match `BLANK ( )`

The match is anchored over the entire expression, so anything with a character outside that one call
- an `IF(`, a trailing `+ 1`, a second argument - can never match, whatever it contains.

Reading the expression is most of the work
------------------------------------------
TMDL serialises an expression three ways (Microsoft Learn, *Tabular Model Definition Language*,
"Expressions"), and a line-at-a-time scan gets two of them wrong:

1. inline - `measure 'X' = BLANK()`
2. indented block - `measure 'X' =` then body lines indented one level deeper than the object's
   PROPERTIES, ending at the first shallower line. Read line-by-line, the declaration looks empty
   and a body line reading `BLANK(),` inside a larger `IF(...)` looks like a stub.
3. fenced block - `measure 'X' = ``` ` then body, closed by ``` ` - Learn's escape hatch for
   preserving indentation or trailing whitespace.

The bias is deliberate: every ambiguity resolves towards NOT reporting a stub. Under-reporting costs
a stub that someone finds later anyway; over-reporting costs trust in the number, and the number is
the whole product.

Actionable vs dead end
----------------------
The engine preserves the source formula as `annotation TableauFormula = <original>` on every calc it
renders, translated or not (verified in the engine's own renderer - `tmdl_generate.py`
`generate_measure_tmdl` / `generate_calc_column_tmdl`, both calling
`tmdl_annotation_value("TableauFormula", formula)`). That annotation is what makes a stub tractable:

* ACTIONABLE - a non-empty `TableauFormula`. The work is a DAX translation, in place.
* DEAD END   - no usable formula. Note the engine ELIDES an empty annotation (TMDL has no valid
  empty-value form), so "annotation missing" is a real state, not a parse failure, and the fix is a
  trip back to the Tableau workbook. This is the escalation list, and it is usually much shorter.

`TranslationSuggestion` (the engine's unapproved candidate DAX) is surfaced alongside, because those
are the cheapest items in the queue to clear.

Exit codes - deliberately NOT a gate by default
-----------------------------------------------
| 0 | the scan ran. Stubs may exist: mid-migration that is the EXPECTED state, and a tool that fails
      the build for every healthy in-progress model gets muted, after which it reports nothing.
| 1 | `--strict` only: at least one stub. Opt-in gate mode, for a bundle claimed to be finished.
| 2 | usage error (argparse) - a path that does not exist never produces a verdict.
| 3 | SKIPPED: nothing was measured. Distinct from 0 on purpose - "no stubs" and "no model" must
      never print or exit the same way.

What it will NOT tell you
-------------------------
That the non-stub measures are CORRECT. A translated measure can be present, well-formed and wrong;
this counts placeholders, not fidelity. It also cannot see a stub that was replaced by wrong-but-
plausible DAX, cannot judge whether a report actually consumes a placeholder (that is
`check_blank_placeholders.py`, which correlates engine handover evidence with the PBIR that binds
it), and reads only committed files - never Desktop, the CLI or the engine.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bundle_corpus import shipping_models

REPORT_NAME = "stub-measure-check.json"

STATUS_OK = "OK"
STATUS_STUBS = "STUBS"
STATUS_SKIPPED = "SKIPPED"

EXIT_OK = 0
EXIT_STRICT = 1
EXIT_USAGE = 2
EXIT_SKIPPED = 3

FORMULA_ANNOTATION = "TableauFormula"
SUGGESTION_ANNOTATION = "TranslationSuggestion"

_FENCE = "```"
_TAB_WIDTH = 4

# TMDL member declarations. A name is bare or single-quoted, and an apostrophe INSIDE a quoted name
# is doubled (`measure 'Sondheim''s Work'`, real committed data in examples/broadway-stage-to-screen)
# - so a naive `'[^']*'` truncates it and mis-keys the queue an operator has to work from. The `=` is
# required: it is what separates a calculated object from a plain data column, which belongs in
# neither the numerator nor the denominator.
_NAME = r"'(?:[^']|'')*'|[^\s=]+"
_TABLE_RE = re.compile(rf"^table\s+(?P<name>{_NAME})\s*$")
_MEMBER_RE = re.compile(rf"^(?P<indent>[\t ]*)(?P<kind>measure|column)\s+(?P<name>{_NAME})\s*=(?P<rest>.*)$")
_ANNOTATION_RE = re.compile(r"^[\t ]*annotation\s+(?P<name>[^\s=]+)\s*=(?P<value>.*)$")
_BLANK_RE = re.compile(r"BLANK\s*\(\s*\)", re.IGNORECASE)


@dataclass
class Member:
    """One calculated object (measure or calculated column) as it appears in TMDL."""

    kind: str
    table: str
    name: str
    expression: str
    line: int
    tmdl: Path
    annotations: dict[str, str] = field(default_factory=dict)

    @property
    def formula(self) -> str:
        """The preserved Tableau source formula, or "" when it did not survive."""
        return self.annotations.get(FORMULA_ANNOTATION, "").strip()

    @property
    def suggestion(self) -> str:
        """The engine's unapproved candidate DAX, when it attached one."""
        return self.annotations.get(SUGGESTION_ANNOTATION, "").strip()


def _unquote(name: str) -> str:
    """Strip TMDL's single-quoting from an object name."""
    name = name.strip()
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        return name[1:-1].replace("''", "'")
    return name


def _indent_width(line: str) -> int:
    """Indent depth in columns, so a tab-indented file and a space-indented one compare."""
    prefix = line[: len(line) - len(line.lstrip())]
    return len(prefix.expandtabs(_TAB_WIDTH))


def strip_comments(expr: str) -> str:
    """Remove DAX comments while respecting string literals.

    `//` and `--` both start a DAX line comment and `/* */` a block comment - but `"//host"` is a
    perfectly good string, and truncating there would corrupt authored DAX. That is the same class
    of error as the substring match this module exists to avoid, just pointing the other way.
    """
    out: list[str] = []
    i, n = 0, len(expr)
    while i < n:
        char = expr[i]
        if char == '"':
            i = _copy_string_literal(expr, i, out)
            continue
        nxt = expr[i + 1] if i + 1 < n else ""
        if (char == "/" and nxt == "/") or (char == "-" and nxt == "-"):
            while i < n and expr[i] != "\n":
                i += 1
            continue
        if char == "/" and nxt == "*":
            end = expr.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _copy_string_literal(expr: str, start: int, out: list[str]) -> int:
    """Copy one double-quoted DAX literal verbatim (`""` is an escaped quote); return the next index."""
    out.append(expr[start])
    i = start + 1
    while i < len(expr):
        char = expr[i]
        out.append(char)
        if char == '"':
            if i + 1 < len(expr) and expr[i + 1] == '"':
                out.append('"')
                i += 2
                continue
            return i + 1
        i += 1
    return i


def strip_outer_parens(expr: str) -> str:
    """Drop parentheses that wrap the WHOLE expression, e.g. `(BLANK())`.

    Only when the outermost `(` closes at the very last character - otherwise `([a]) + (BLANK())`
    would be chewed down to its last term, inventing a stub out of authored DAX.
    """
    while len(expr) > 1 and expr.startswith("(") and expr.endswith(")"):
        depth = 0
        for index, char in enumerate(expr):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(expr) - 1:
                    return expr
        expr = expr[1:-1].strip()
    return expr


def is_stub_expression(expr: str) -> bool:
    """True only when the ENTIRE expression is one `BLANK()` call.

    This is the whole contract of the module. `IF(ISBLANK([Sales]), BLANK(), [Sales])` is authored
    DAX and must never be reported; the full-match anchor is what guarantees that, because a single
    character outside the call defeats it no matter what the rest of the text says.
    """
    text = " ".join(strip_comments(expr).split())
    if '"' not in text:
        text = strip_outer_parens(text)
    return bool(_BLANK_RE.fullmatch(text))


def parse_tmdl(path: Path) -> list[Member]:
    """Every calculated measure/column declared in one TMDL file, with its full expression."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    table = path.stem
    members: list[Member] = []
    index = 0
    while index < len(lines):
        table_match = _TABLE_RE.match(lines[index])
        if table_match:
            table = _unquote(table_match.group("name"))
            index += 1
            continue
        member_match = _MEMBER_RE.match(lines[index])
        if not member_match:
            index += 1
            continue
        declared_at = index
        expression, annotations, index = _read_member(lines, index, member_match)
        members.append(
            Member(
                kind=member_match.group("kind"),
                table=table,
                name=_unquote(member_match.group("name")),
                expression=expression,
                line=declared_at + 1,
                tmdl=path,
                annotations=annotations,
            )
        )
    return members


def _read_member(lines: list[str], start: int, match: re.Match[str]) -> tuple[str, dict[str, str], int]:
    """Read one member's expression and property block; return them plus the next line index."""
    decl_indent = _indent_width(match.group("indent"))
    rest = match.group("rest").strip()
    index = start + 1
    if rest == _FENCE:
        expression, index = _read_fenced_block(lines, index)
    elif rest.startswith(_FENCE) and rest.endswith(_FENCE) and len(rest) > 2 * len(_FENCE):
        expression = rest[len(_FENCE) : -len(_FENCE)]
    elif rest:
        expression = rest
    else:
        expression, index = _read_indented_block(lines, index, decl_indent)
    annotations, index = _read_annotations(lines, index, decl_indent)
    return expression, annotations, index


def _read_fenced_block(lines: list[str], index: int) -> tuple[str, int]:
    """Body of a ``` ``` ```-enclosed expression, ending at its closing fence."""
    body: list[str] = []
    while index < len(lines):
        if lines[index].strip() == _FENCE:
            return "\n".join(body).strip(), index + 1
        body.append(lines[index].strip())
        index += 1
    return "\n".join(body).strip(), index


def _read_indented_block(lines: list[str], index: int, decl_indent: int) -> tuple[str, int]:
    """Body of an indentation-delimited expression.

    Per Learn: the body sits one level deeper than the object's PROPERTIES and ends at the first
    shallower line, which is how `formatString:` and the annotations stay out of the expression. A
    blank line is part of the expression, so it must not terminate the block.
    """
    body: list[str] = []
    body_indent: int | None = None
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            body.append("")
            index += 1
            continue
        width = _indent_width(line)
        if body_indent is None:
            if width <= decl_indent:
                break
            body_indent = width
        if width < body_indent:
            break
        body.append(line.strip())
        index += 1
    return "\n".join(body).strip(), index


def _read_annotations(lines: list[str], index: int, decl_indent: int) -> tuple[dict[str, str], int]:
    """Annotations declared on this member, stopping at the next same-or-shallower declaration."""
    annotations: dict[str, str] = {}
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if _indent_width(line) <= decl_indent:
            break
        match = _ANNOTATION_RE.match(line)
        if match:
            annotations.setdefault(_unquote(match.group("name")), match.group("value").strip())
        index += 1
    return annotations, index


def parse_model(model_dir: Path) -> list[Member]:
    """Every calculated measure/column in one `.SemanticModel`."""
    definition = model_dir / "definition"
    root = definition if definition.is_dir() else model_dir
    members: list[Member] = []
    for path in sorted(root.rglob("*.tmdl")):
        members.extend(parse_tmdl(path))
    return members


def ratio(part: int, whole: int) -> str:
    """The `64/89 (72%)` shape the customer's own field notes already use."""
    if not whole:
        return f"{part}/{whole} (n/a)"
    return f"{part}/{whole} ({round(100 * part / whole)}%)"


def _finding(member: Member, model_dir: Path) -> dict[str, Any]:
    """One stub, with everything needed to act on it without opening the file."""
    try:
        tmdl = member.tmdl.resolve().relative_to(model_dir.resolve().parent).as_posix()
    except ValueError:
        tmdl = member.tmdl.as_posix()
    return {
        "kind": member.kind,
        "table": member.table,
        "name": member.name,
        "actionable": bool(member.formula),
        "tableau_formula": member.formula,
        "suggestion": member.suggestion,
        "tmdl": tmdl,
        "line": member.line,
    }


def _table_rows(measures: list[Member]) -> list[dict[str, Any]]:
    """Per-table measure ratios, worst first - a 75%-stubbed table hides inside a 20% model."""
    rows: dict[str, dict[str, Any]] = {}
    for member in measures:
        row = rows.setdefault(
            member.table,
            {"table": member.table, "measures": 0, "stubs": 0, "actionable": 0, "dead_end": 0},
        )
        row["measures"] += 1
        if not is_stub_expression(member.expression):
            continue
        row["stubs"] += 1
        row["actionable" if member.formula else "dead_end"] += 1
    return sorted(rows.values(), key=lambda r: (-r["stubs"], r["table"]))


def census_model(model_dir: Path) -> dict[str, Any]:
    """Grade ONE semantic model.

    A model that yields no calculated object at all is SKIPPED, never OK: `check_field_bindings`
    learned the same lesson the hard way - an affirmative verdict has to mean something was actually
    measured, or a mistyped path reads as a clean bill of health.
    """
    members = parse_model(model_dir)
    if not members:
        return {
            "model": model_dir.name,
            "path": str(model_dir),
            "status": STATUS_SKIPPED,
            "reason": "no measure or calculated column parsed from this model",
        }
    measures = [m for m in members if m.kind == "measure"]
    columns = [m for m in members if m.kind == "column"]
    stubs = [m for m in members if is_stub_expression(m.expression)]
    findings = [_finding(m, model_dir) for m in stubs]
    measure_stubs = sum(1 for m in stubs if m.kind == "measure")
    return {
        "model": model_dir.name,
        "path": str(model_dir),
        "status": STATUS_STUBS if stubs else STATUS_OK,
        "measures": len(measures),
        "measure_stubs": measure_stubs,
        "calculated_columns": len(columns),
        "column_stubs": len(stubs) - measure_stubs,
        "actionable": sum(1 for f in findings if f["actionable"]),
        "dead_end": sum(1 for f in findings if not f["actionable"]),
        "suggested": sum(1 for f in findings if f["suggestion"]),
        "tables": _table_rows(measures),
        "findings": findings,
    }


find_models = shipping_models


def scan(root: Path) -> dict[str, Any]:
    """Census every shipping semantic model under `root`."""
    return merge([census_model(model_dir) for model_dir in find_models(root)])


def merge(models: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-model censuses into one verdict, keeping ungraded models out of the totals."""
    graded = [m for m in models if m["status"] != STATUS_SKIPPED]
    skipped = [
        {"model": m["model"], "path": m["path"], "reason": m["reason"]} for m in models if m["status"] == STATUS_SKIPPED
    ]
    total = _totals(graded)
    if not graded:
        status = STATUS_SKIPPED
    else:
        status = STATUS_STUBS if total["stubs"] else STATUS_OK
    return {
        "status": status,
        "models_scanned": len(graded),
        **total,
        "models": graded,
        "skipped": skipped,
    }


def _totals(graded: list[dict[str, Any]]) -> dict[str, Any]:
    """Estate-wide counters, plus the exact float ratio a whole-percent string cannot carry."""
    measures = sum(m["measures"] for m in graded)
    measure_stubs = sum(m["measure_stubs"] for m in graded)
    column_stubs = sum(m["column_stubs"] for m in graded)
    return {
        "measures": measures,
        "measure_stubs": measure_stubs,
        "calculated_columns": sum(m["calculated_columns"] for m in graded),
        "column_stubs": column_stubs,
        "stubs": measure_stubs + column_stubs,
        "stub_ratio": (measure_stubs / measures) if measures else 0.0,
        "actionable": sum(m["actionable"] for m in graded),
        "dead_end": sum(m["dead_end"] for m in graded),
        "suggested": sum(m["suggested"] for m in graded),
    }


def render(report: dict[str, Any], *, verbose: bool = False) -> str:
    """Human-readable census, in the shape the sibling gates use."""
    if report["status"] == STATUS_SKIPPED:
        reasons = "; ".join(s["reason"] for s in report.get("skipped", [])) or "no semantic model found"
        return f"STUB MEASURE CHECK: SKIPPED - nothing measured ({reasons})"
    headline = (
        f"STUB MEASURE CHECK: {report['status']} - {ratio(report['measure_stubs'], report['measures'])} "
        f"measure(s) across {report['models_scanned']} model(s) are = BLANK() placeholders"
    )
    if report["status"] == STATUS_OK:
        return headline + _skipped_tail(report)
    split = f"  {report['actionable']} actionable, {report['dead_end']} dead end" + (
        f" ({report['suggested']} carry an engine TranslationSuggestion)" if report["suggested"] else ""
    )
    lines = [headline, split]
    if report["column_stubs"]:
        lines.append(
            f"  plus {ratio(report['column_stubs'], report['calculated_columns'])} calculated column(s) stubbed"
        )
    labels = _labels(report["models"])
    for model in report["models"]:
        lines += _render_model(model, labels[model["path"]], verbose=verbose)
    lines.append(
        "  ACTIONABLE = the Tableau formula survived as `annotation TableauFormula`; translate it in place.\n"
        "  DEAD END   = nothing survived; recover the formula from the Tableau workbook before translating."
    )
    return "\n".join(lines) + _skipped_tail(report)


def _labels(models: list[dict[str, Any]]) -> dict[str, str]:
    """Display name per model, disambiguated by its parent folder when the name is not unique.

    An estate really does ship the same model name from several workbooks (measured on a 38-workbook
    bundle: two `Meridian Sales (Live Snowflake).SemanticModel`, with DIFFERENT ratios). Rendering
    both rows under one label invites the reader to treat one as a typo of the other.
    """
    seen: dict[str, int] = {}
    for model in models:
        seen[model["model"]] = seen.get(model["model"], 0) + 1
    return {
        m["path"]: (m["model"] if seen[m["model"]] == 1 else f"{Path(m['path']).parent.name}/{m['model']}")
        for m in models
    }


def _render_model(model: dict[str, Any], label: str, *, verbose: bool) -> list[str]:
    """One model's block: its ratios, its worst tables, and its escalation list.

    BOTH ratios are printed when the model has calculated columns, because the actionable/dead-end
    split covers every stub: printing `27/51` beside `actionable 60` reads as broken arithmetic when
    the missing 33 are stubbed calculated columns.
    """
    if model["status"] == STATUS_OK:
        return []
    ratios = f"measures {ratio(model['measure_stubs'], model['measures'])}"
    if model["calculated_columns"]:
        ratios += f", calc columns {ratio(model['column_stubs'], model['calculated_columns'])}"
    lines = [f"  {label}  {ratios}   -> actionable {model['actionable']}, dead end {model['dead_end']}"]
    for row in model["tables"]:
        if row["stubs"]:
            lines.append(f"    {row['table']}  {ratio(row['stubs'], row['measures'])}")
    dead = [f for f in model["findings"] if not f["actionable"]]
    if dead:
        lines.append("    DEAD END (no TableauFormula annotation - recover from the Tableau workbook):")
        lines += [f"      - {f['table']}[{f['name']}]  ({f['kind']}, {f['tmdl']}:{f['line']})" for f in dead]
    if verbose:
        actionable = [f for f in model["findings"] if f["actionable"]]
        if actionable:
            lines.append("    ACTIONABLE (translate in place):")
            lines += [f"      - {f['table']}[{f['name']}]  <- {f['tableau_formula']}" for f in actionable]
    return lines


def _skipped_tail(report: dict[str, Any]) -> str:
    """Name the models that were NOT graded, so a partial sweep cannot read as a full one."""
    skipped = report.get("skipped") or []
    if not skipped:
        return ""
    names = ", ".join(f"{s['model']} ({s['reason']})" for s in skipped)
    return f"\n  {len(skipped)} model(s) SKIPPED, not measured: {names}"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="bundle folder(s) or .SemanticModel folder(s)")
    parser.add_argument("--model", type=Path, help="explicit .SemanticModel folder")
    parser.add_argument("--json", type=Path, help="write the machine-readable census here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered census")
    parser.add_argument("--verbose", action="store_true", help="also list the actionable work queue")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any stub is found (gate mode)")
    args = parser.parse_args(argv)

    targets = [*args.paths, *([args.model] if args.model else [])]
    if not targets:
        parser.error("give a bundle/model path, or --model")
    # A path that does not exist must NEVER produce a verdict: `rglob` over a missing folder yields
    # nothing, and "0 stubs" for a folder that was never opened is the one output that would make
    # this census worse than not running it.
    for path in targets:
        if not path.is_dir():
            parser.error(f"{path} is not a directory")

    scans = [scan(path) for path in targets]
    merged = merge([one for report in scans for one in report["models"] + _skipped_of(report)])

    if args.json:
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged, verbose=args.verbose))
    if merged["status"] == STATUS_SKIPPED:
        return EXIT_SKIPPED
    if args.strict and merged["stubs"]:
        return EXIT_STRICT
    return EXIT_OK


def _skipped_of(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-shape a scan's skipped models so they survive a merge across several paths."""
    return [{**s, "status": STATUS_SKIPPED} for s in report.get("skipped", [])]


if __name__ == "__main__":
    sys.exit(main())
