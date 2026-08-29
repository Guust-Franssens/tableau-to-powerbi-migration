"""
purpose: TMDL structural checks - the half of the model gate that reads TMDL rather than M.
usage:   imported by scripts/check_datamodel.py (which re-exports these names)
internal: true
internal-reason: a library with no CLI of its own. Agents reach every check here by running
                 `python scripts/check_datamodel.py`, which is the agent-facing entry point and is
                 already named in scripts/README.md.

Split out of check_datamodel.py so neither half needs a `too-many-lines` waiver. The two halves
share only the shape of their output: this module reports `TmdlFinding`, the M scanner reports
`Finding`, and `check_datamodel.main` prints both.

Scope stays deliberately narrow because a false positive is worse than a miss:

  * duplicate scalar TMDL properties within one object
  * measure/column name collisions within one table file
  * empty measure expressions
  * expression blocks whose LINE LAYOUT the TMDL parser cannot read as intended
  * direct CALCULATE/CALCULATETABLE compact filters that compare a column to a measure
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TmdlFinding:
    """One TMDL/data-model problem, located precisely enough to fix without hunting."""

    code: str
    message: str
    file: Path
    line: int

    def render(self, root: Path) -> str:
        """Format as `path:line CODE`, matching the M finding output style."""
        try:
            shown = self.file.relative_to(root)
        except ValueError:
            shown = self.file
        return f"  {shown}:{self.line}  {self.code}\n      {self.message}"


_OBJECT_RE = re.compile(
    r"^(?P<indent>\s*)(?P<kind>table|measure|column|partition|hierarchy|level|role|"
    r"relationship|culture|expression|calculationGroup|calculationItem|perspective|model|database)\b"
    r"(?P<rest>.*)$"
)
_PROPERTY_RE = re.compile(r"^(?P<indent>\s*)(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*:\s*(?P<value>.*)$")
_REPEATABLE_PROPERTIES = {"annotation", "extendedProperty", "changedProperty", "ref", "variation", "levels"}
_COLUMN_EQ_MEASURE_RE = re.compile(r"^'?[^'\[\]]+'?\s*\[[^\]]+\]\s*(?:=|<>|<=|>=|<|>)\s*\[[^\]]+\]$")
_CALCULATE_RE = re.compile(r"\bCALCULATE(?:TABLE)?\s*\(", re.IGNORECASE)


def _split_top_level_args(text: str) -> list[str]:
    """Split a function argument list on commas that are not nested inside another expression."""
    args: list[str] = []
    depth = 0
    start = 0
    in_string = False
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if ch == '"':
            if in_string and idx + 1 < len(text) and text[idx + 1] == '"':
                idx += 2
                continue
            in_string = not in_string
        elif not in_string:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                args.append(text[start:idx])
                start = idx + 1
        idx += 1
    args.append(text[start:])
    return args


def find_compact_filters(expression: str) -> bool:
    """Return True for direct CALCULATE filters like `'T'[Col] = [Measure]`.

    The same predicate is legal inside FILTER(...), so the check only inspects direct filter
    arguments after the first CALCULATE/CALCULATETABLE expression argument.
    """
    for match in _CALCULATE_RE.finditer(expression):
        depth = 1
        end: int | None = None
        in_string = False
        idx = match.end()
        while idx < len(expression):
            ch = expression[idx]
            if ch == '"':
                if in_string and idx + 1 < len(expression) and expression[idx + 1] == '"':
                    idx += 2
                    continue
                in_string = not in_string
            elif not in_string:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = idx
                        break
            idx += 1
        if end is None:
            continue
        for arg in _split_top_level_args(expression[match.end() : end])[1:]:
            if _COLUMN_EQ_MEASURE_RE.match(arg.strip()):
                return True
    return False


def _strip_tmdl_comment(line: str) -> str:
    """Remove a TMDL description/comment line so prose cannot be mistaken for a property."""
    return "" if line.lstrip().startswith("///") else line


def _object_name(rest: str) -> str | None:
    """Extract a TMDL object's name from the remainder of its header line."""
    head = rest.split("=", 1)[0].strip()
    if head.startswith("'"):
        end = head.find("'", 1)
        return head[1:end] if end > 0 else None
    parts = head.split()
    return parts[0] if parts else None


def _expression_on_measure_header(rest: str) -> bool:
    """Whether a measure header carries a non-empty expression after `=`."""
    return "=" in rest and bool(rest.split("=", 1)[1].strip())


def _nearest_context(seen: dict[int, dict[str, int]], indent: int) -> int | None:
    """Return the nearest open TMDL object context for a property indent."""
    candidates = [depth for depth in seen if depth <= indent]
    return min(candidates, key=lambda depth: indent - depth) if candidates else None


def _empty_measure_finding(path: Path, pending_measure: tuple[str, int]) -> TmdlFinding:
    """Build the repeated empty-measure finding once so narrowing stays simple."""
    name, line = pending_measure
    return TmdlFinding("EMPTY_EXPRESSION", f"measure '{name}' has no expression after '='.", path, line)


def _append_name_collisions(
    findings: list[TmdlFinding], path: Path, table_name: str, measures: dict[str, int], columns: dict[str, int]
) -> None:
    """Report measure/column collisions for one table scope."""
    for name, line in measures.items():
        if name in columns:
            findings.append(
                TmdlFinding(
                    "NAME_COLLISION",
                    f"measure '{name}' in table '{table_name}' has the same name as a column in "
                    f"that table; Tabular names must be unique within a table. Column first seen "
                    f"at line {columns[name]}.",
                    path,
                    line,
                )
            )


def check_tmdl_text(path: Path, text: str) -> list[TmdlFinding]:  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Structurally check one TMDL document."""
    findings: list[TmdlFinding] = []
    seen: dict[int, dict[str, int]] = {}
    context_line: dict[int, int] = {}
    current_table = ""
    measures: dict[str, int] = {}
    columns: dict[str, int] = {}
    pending_measure: tuple[str, int] | None = None

    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_tmdl_comment(raw)
        if not line.strip():
            continue

        if pending_measure and _OBJECT_RE.match(line):
            findings.append(_empty_measure_finding(path, pending_measure))
            pending_measure = None

        if find_compact_filters(line):
            findings.append(
                TmdlFinding(
                    "COMPACT_FILTER",
                    "direct CALCULATE filter compares a column to a measure; DAX requires a constant "
                    "on the right of a compact predicate. Hoist the measure into a VAR or wrap the "
                    "predicate in FILTER(...).",
                    path,
                    number,
                )
            )

        obj = _OBJECT_RE.match(line)
        if obj:
            indent = len(obj.group("indent").expandtabs(4))
            for depth in [depth for depth in seen if depth > indent]:
                del seen[depth]
            seen[indent + 1] = {}
            context_line[indent + 1] = number
            kind = obj.group("kind")
            rest = obj.group("rest")
            name = _object_name(rest)
            if kind == "table" and name:
                _append_name_collisions(findings, path, current_table, measures, columns)
                current_table = name
                measures = {}
                columns = {}
            elif kind == "measure" and name and current_table:
                measures.setdefault(name, number)
                pending_measure = None if _expression_on_measure_header(rest) else (name, number)
            elif kind == "column" and name and current_table:
                columns.setdefault(name, number)
            continue

        prop = _PROPERTY_RE.match(line)
        if prop:
            indent = len(prop.group("indent").expandtabs(4))
            name = prop.group("name")
            if name not in _REPEATABLE_PROPERTIES:
                bucket = _nearest_context(seen, indent)
                if bucket is not None:
                    if name in seen[bucket]:
                        findings.append(
                            TmdlFinding(
                                "DUPLICATE_PROPERTY",
                                f"'{name}' appears more than once in the object opened at line "
                                f"{context_line[bucket]}; TMDL rejects duplicated scalar properties. "
                                f"First seen at line {seen[bucket][name]}.",
                                path,
                                number,
                            )
                        )
                    else:
                        seen[bucket][name] = number
            if pending_measure:
                findings.append(_empty_measure_finding(path, pending_measure))
                pending_measure = None
            continue

        if pending_measure and line.strip():
            pending_measure = None

    if pending_measure:
        findings.append(_empty_measure_finding(path, pending_measure))

    _append_name_collisions(findings, path, current_table, measures, columns)
    return findings


# --- TMDL expression layout ------------------------------------------------------------------
#
# Measured 2026-08-29 against TmdlSerializer.DeserializeDatabaseFromFolder (AMO 19.84.1), the same
# parser Power BI Desktop uses. Multi-line DAX is LEGAL and blank lines inside it are LEGAL; what is
# fatal is starting the expression on the `=` line and then continuing it onto the next line, which
# raises `TMDL Format Error: Unexpected line type: Other!` and the model does not open at all.
#
# These checks enforce the DOCUMENTED contract rather than guessing from a list of property names:
#
#   "Multi-line expressions must be indented one level deeper to the parent object properties and
#    the entire expression must be within that indentation level."
#   -- https://learn.microsoft.com/en-us/analysis-services/tmdl/tmdl-overview
#
# An earlier revision decided absorption by matching a corpus-harvested set of property names, which
# both missed real properties (a bare `isHidden`, a documented `isKey:`) and fired on property-shaped
# text inside an M block comment. Indentation is the actual rule, so indentation is what is checked.

# Objects whose DEFAULT property is an expression. Their properties sit one level deeper than the
# header, so the expression must be deeper still. `partition` is deliberately absent: its `= m` /
# `= calculated` names a source TYPE, not an expression, and the M it carries lives in `source =`.
_OBJECT_EXPRESSION_KINDS = ("measure", "column", "calculationItem", "expression", "tablePermission")
# Properties whose VALUE is an expression. These already sit at the property indent, so the
# expression only has to be deeper than the property line itself.
_PROPERTY_EXPRESSIONS = frozenset(
    {
        "source",
        "expression",
        "formatStringDefinition",
        "detailRowsDefinition",
        "defaultDetailRowsDefinition",
        "defaultMember",
        "calculationGroupExpression",
        "filterExpression",
        "statusExpression",
        "targetExpression",
        "trendExpression",
    }
)
_OBJECT_EXPRESSION_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:" + "|".join(_OBJECT_EXPRESSION_KINDS) + r")\s+(?:'[^']*'|[^=]+?)\s*=(?P<tail>.*)$"
)
_PROPERTY_EXPRESSION_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z][A-Za-z0-9_]*)[ \t]*=(?!=)(?P<tail>.*)$")

# Line shapes TMDL accepts inside an object body. Colon-form takes ANY name, because `name: value`
# at the start of a line is TMDL property syntax that DAX and M do not produce. The other two shapes
# are allowlisted, because M produces them constantly: `Source = Csv.Document(...)` has the same
# shape as `annotation X = Y`, and a bare `in` has the same shape as a boolean shortcut property.
_BARE_WORD_RE = re.compile(r"^[ \t]*(?P<name>[A-Za-z][A-Za-z0-9_]*)[ \t]*$")
_KEYED_ENTRY_RE = re.compile(r"^[ \t]*(annotation|extendedProperty|changedProperty|ref|variation|levels?)\b")
# TMDL boolean properties are camelCase and carry one of these verbs; no M or DAX keyword does.
_BOOLEAN_PROPERTY_RE = re.compile(r"^(is|has|show|enable|exclude|discourage|include|allow)[A-Z]")
_ALWAYS_BOOLEAN = frozenset({"isHidden", "isKey", "isUnique", "isNullable", "isDefault", "isActive", "isPrivate"})

_CONTINUATION_HELP = (
    "Either collapse the expression onto the one line, or start it on the line AFTER '=' and indent "
    "every line deeper than the object's properties. Multi-line DAX is legal; an inline start with a "
    "continuation line is not."
)


def _indent_of(line: str) -> int:
    """Visual indent width of a line, with tabs expanded so tabs and spaces compare correctly."""
    stripped = line.lstrip(" \t")
    return len(line[: len(line) - len(stripped)].expandtabs(4))


def indent_unit(lines: list[str]) -> int:
    """One indentation level for this document, in expanded columns.

    The smallest indent the document uses, capped at one tab. The cap is load-bearing: a committed
    fixture writes `expression Source =` at column 0 with its `let` body two tabs in and no line
    between, so the smallest indent is 8 - and reading 8 as one level put the object's properties at
    column 8 too, reporting a file AMO opens cleanly as broken.

    TMDL is tab-indented in practice and a tab expands to four columns, so a level is never wider
    than that. Erring low costs a miss, never a false positive: it only moves the indent an
    expression must beat shallower.
    """
    indents = [_indent_of(line) for line in lines if line.strip() and _indent_of(line) > 0]
    return min([*indents, 4]) if indents else 4


def _is_body_line(line: str) -> bool:
    """Whether a line is TMDL object-body syntax rather than expression content."""
    if _PROPERTY_RE.match(line) or _KEYED_ENTRY_RE.match(line) or _OBJECT_RE.match(line):
        return True
    bare = _BARE_WORD_RE.match(line)
    if bare:
        name = bare.group("name")
        return name in _ALWAYS_BOOLEAN or bool(_BOOLEAN_PROPERTY_RE.match(name))
    named = _PROPERTY_EXPRESSION_RE.match(line)
    return bool(named and named.group("name") in _PROPERTY_EXPRESSIONS)


def _is_ignorable(line: str) -> bool:
    """Blank lines, which never terminate or continue an expression."""
    return not line.strip()


def _expression_header(line: str, unit: int) -> tuple[int, str] | None:
    """(property indent, text after `=`) when a line opens an expression, else None.

    The property indent is what the multi-line contract is measured against: for an object header
    the properties sit one level deeper, for a property-position expression they sit on that very
    line. Returning it here is what lets one rule cover both.
    """
    match = _OBJECT_EXPRESSION_RE.match(line)
    if match:
        return _indent_of(line) + unit, match.group("tail")
    match = _PROPERTY_EXPRESSION_RE.match(line)
    if match and match.group("name") in _PROPERTY_EXPRESSIONS:
        return _indent_of(line), match.group("tail")
    return None


def _skip_verbatim(path: Path, lines: list[str], start: int) -> tuple[list[TmdlFinding], int]:
    """Step over a ``` -enclosed expression, which TMDL reads verbatim so nothing here applies."""
    idx = start + 1
    while idx < len(lines):
        if lines[idx].strip() == "```":
            return [], idx + 1
        idx += 1
    # Unterminated: the rest of the file is unassessable, so say so rather than reporting it clean.
    return (
        [
            TmdlFinding(
                "TMDL_UNTERMINATED_EXPRESSION",
                "a ``` -enclosed expression is never closed, so the rest of this file could not be "
                "checked. TMDL requires the closing ``` on a line of its own.",
                path,
                start + 1,
            )
        ],
        idx,
    )


def _misplaced_description(path: Path, start: int, idx: int) -> TmdlFinding:
    """A `///` inside an object body, which TMDL rejects as an unexpected line."""
    return TmdlFinding(
        "TMDL_MISPLACED_DESCRIPTION",
        f"a '///' description sits inside the body of the object opened at line {start + 1}. TMDL "
        f"only accepts a description immediately BEFORE the object it documents, at that object's "
        f"own indent; here Desktop raises 'Unexpected line type: Other!' at open.",
        path,
        idx + 1,
    )


def _continuation(path: Path, start: int, idx: int) -> TmdlFinding:
    """A line the parser cannot read because the expression already ended on the `=` line."""
    return TmdlFinding(
        "TMDL_EXPRESSION_CONTINUATION",
        f"the expression opened at line {start + 1} is written on the '=' line, so TMDL reads it as "
        f"single-line and this line is neither a property nor a child object. Power BI Desktop "
        f"refuses to open the model ('TMDL Format Error: Unexpected line type: Other!', or 'The "
        f"keyword ... is neither a property nor an object in the current context'). {_CONTINUATION_HELP}",
        path,
        idx + 1,
    )


def _check_inline_expression(path: Path, lines: list[str], start: int, unit: int) -> tuple[list[TmdlFinding], int]:
    """After an expression written on the `=` line, every following line must be legal TMDL.

    Both directions matter. A DEEPER line is a continuation of an expression that already ended; a
    line at or above the header's own indent is only legal if it is itself a property, an object
    declaration or a description. `measure M = IF(1=1,` followed by `"a","b")` at the SAME indent is
    fatal, and an earlier revision ended its scan on any dedent and so reported nothing.

    A body line that OPENS its own expression (`formatStringDefinition =`, `detailRowsDefinition =`)
    ends this scan and is handed back to the caller, because its body belongs to that nested
    expression rather than to this one - reading it here reported a legal model as broken.
    """
    idx = start + 1
    head_indent = _indent_of(lines[start])
    while idx < len(lines):
        raw = lines[idx]
        if _is_ignorable(raw):
            idx += 1
            continue
        deeper = _indent_of(raw) > head_indent
        if raw.lstrip().startswith("///"):
            return ([_misplaced_description(path, start, idx)], idx + 1) if deeper else ([], idx)
        if not _is_body_line(raw):
            return [_continuation(path, start, idx)], idx + 1
        if not deeper or _expression_header(raw, unit) is not None:
            # A legal sibling, a parent line, or a nested expression: stop so the caller sees it.
            return [], idx
        idx += 1
    return [], idx


def _under_indented(path: Path, lines: list[str], start: int, idx: int, property_indent: int) -> TmdlFinding:
    """The documented contract, violated: the expression is not deeper than the properties."""
    if _indent_of(lines[idx]) <= _indent_of(lines[start]):
        return TmdlFinding(
            "TMDL_EXPRESSION_UNINDENTED",
            f"the expression opened at line {start + 1} continues at an indent that is not deeper "
            f"than its own declaration, so TMDL reads this as a sibling and raises 'Invalid "
            f"indentation was detected!' at open. {_CONTINUATION_HELP}",
            path,
            idx + 1,
        )
    return TmdlFinding(
        "TMDL_EXPRESSION_ABSORBS_PROPERTY",
        f"the multi-line expression opened at line {start + 1} starts at the same indent as the "
        f"object's properties (column {property_indent + 1}), so EVERY property the object declares "
        f"after it is read as part of the DAX/M instead of being set. TMDL requires a multi-line "
        f"expression to be indented one level deeper than the parent object's properties. This "
        f"parses CLEANLY - the properties are simply lost - so nothing else reports it.",
        path,
        idx + 1,
    )


def _bad_terminator(path: Path, lines: list[str], idx: int, start: int) -> list[TmdlFinding]:
    """The line that ENDS a multi-line expression must itself be legal TMDL, or the parser errors.

    This is what catches a fragment that dedents out of the expression mid-way - including a blank
    line inside a string literal, whose next line lands back at column 0.
    """
    while idx < len(lines) and (_is_ignorable(lines[idx]) or lines[idx].lstrip().startswith("///")):
        idx += 1
    if idx >= len(lines) or _is_body_line(lines[idx]):
        return []
    return [
        TmdlFinding(
            "TMDL_EXPRESSION_UNINDENTED",
            f"this line ends the expression opened at line {start + 1} by dedenting out of it, but it "
            f"is not a property or a child object, so Power BI Desktop raises 'Invalid indentation was "
            f"detected!' and the model does not open. {_CONTINUATION_HELP}",
            path,
            idx + 1,
        )
    ]


def _check_multiline_expression(
    path: Path, lines: list[str], start: int, property_indent: int
) -> tuple[list[TmdlFinding], int]:
    """A multi-line expression must be indented past the object's properties, or it eats them."""
    idx = start + 1
    while idx < len(lines) and _is_ignorable(lines[idx]):
        idx += 1
    if idx >= len(lines):
        return [], idx
    baseline = _indent_of(lines[idx])
    if baseline <= property_indent:
        return [_under_indented(path, lines, start, idx, property_indent)], idx + 1
    while idx < len(lines) and not (lines[idx].strip() and _indent_of(lines[idx]) < baseline):
        idx += 1
    return _bad_terminator(path, lines, idx, start), idx


def check_tmdl_expressions(path: Path, text: str) -> list[TmdlFinding]:
    """Report TMDL expression blocks whose line layout the parser cannot read as intended."""
    findings: list[TmdlFinding] = []
    lines = text.splitlines()
    unit = indent_unit(lines)
    idx = 0
    while idx < len(lines):
        raw = lines[idx]
        header = None if raw.lstrip().startswith("///") else _expression_header(raw, unit)
        if header is None:
            idx += 1
            continue
        property_indent, tail = header
        tail = tail.strip()
        if tail.startswith("```"):
            found, idx = _skip_verbatim(path, lines, idx)
        elif tail:
            found, idx = _check_inline_expression(path, lines, idx, unit)
        else:
            found, idx = _check_multiline_expression(path, lines, idx, property_indent)
        findings.extend(found)
    return findings


def _read_tmdl(path: Path) -> tuple[str | None, list[TmdlFinding]]:
    """Decode one TMDL document, reporting what makes it unopenable rather than normalising it.

    Two distinct failures hide here, and both used to read as "clean":

      * undecodable bytes - `errors="replace"` turns a file this checker cannot actually read into
        a reassuring pass, so decoding is strict;
      * a UTF-8 BOM - AMO tolerates it, but Power BI Desktop's project reader does NOT
        (`UTF8EncodingThrowOnBOM.CheckBom` -> "Only text with UTF8 encoding without BOM is
        supported") and the file simply does not open. Stripping it in memory, as this function
        used to, made a broken deliverable pass a gate whose whole purpose is to catch that.
    """
    findings: list[TmdlFinding] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, [TmdlFinding("TMDL_UNREADABLE", f"could not be read ({type(exc).__name__}).", path, 1)]
    if raw.startswith(codecs.BOM_UTF8):
        findings.append(
            TmdlFinding(
                "TMDL_BOM",
                "starts with a UTF-8 BOM. Power BI Desktop rejects it outright ('Only text with "
                "UTF8 encoding without BOM is supported') and the project does not open. Rewrite "
                "the file as UTF-8 without a BOM (Python encoding='utf-8', PowerShell utf8NoBOM).",
                path,
                1,
            )
        )
        raw = raw[len(codecs.BOM_UTF8) :]
    try:
        return raw.decode("utf-8"), findings
    except UnicodeDecodeError:
        findings.append(
            TmdlFinding(
                "TMDL_UNREADABLE",
                "could not be decoded as UTF-8, so its contents were NOT checked. This is not a "
                "pass. Power BI requires TMDL as UTF-8 without a BOM.",
                path,
                1,
            )
        )
        return None, findings


def check_tmdl_model(model_dir: Path) -> tuple[list[TmdlFinding], int]:
    """Every TMDL document in one .SemanticModel."""
    findings: list[TmdlFinding] = []
    definition = model_dir / "definition"
    files = sorted(definition.rglob("*.tmdl")) if definition.exists() else []
    for tmdl in files:
        text, problems = _read_tmdl(tmdl)
        findings.extend(problems)
        if text is None:
            continue
        findings.extend(check_tmdl_text(tmdl, text))
        findings.extend(check_tmdl_expressions(tmdl, text))
    return findings, len(files)
