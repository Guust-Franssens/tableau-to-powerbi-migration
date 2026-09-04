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
  * a measure name repeated in a DIFFERENT table (model-wide uniqueness - see issue #413: this is
    the one check ported from the drifted `examples/*/fabric/_validation/tmdl_validate/` copies
    - four of the five were pure redundant drift and were retired onto `tools/tmdl_oracle`; the
    fifth, `examples/quadruple-axis-charts`, also checks unresolved DAX [bracket] references and
    is kept for now since neither this module nor the oracle covers that yet. It deserializes
    clean but Desktop refuses the commit, which is exactly the class this module exists to catch
    cheaply, without AMO)
  * empty measure expressions
  * direct CALCULATE/CALCULATETABLE compact filters that compare a column to a measure
  * a `DIVIDE(a, b[, alt]) <op> <threshold>` comparison with no `ISBLANK` guard (issue #82),
    wherever the call appears in the expression (nested inside `IF(...)`, `CALCULATE(...)`, etc.)
    - flagged whenever the call's own arguments do not prove it can never return `BLANK()` (a bare
    column/measure reference is never assumed non-blank; only a literal, or `COALESCE(..., <that
    literal>)`, is), and only when `0` (what `BLANK()` coerces to) would itself satisfy the
    comparison. A generic "any threshold on any nullable column" check was deliberately NOT built:
    whether an arbitrary column/measure can be blank, and whether that blank should read as
    included or excluded, is a business decision this checker cannot see from the DAX text alone -
    see docs/tableau-dax-translation-guide.md, 'Threshold comparisons'. **ADVISORY ONLY**
    (`ADVISORY_TMDL_CODES`): a round-2 review measured that a text-only detector, however narrow,
    both misses real unsafe shapes (parenthesized, multiline, `VAR`-bound, measure-indirected DAX
    would all need a real DAX parser to prove) and flags some provably-safe ones, so
    `check_datamodel.py` keeps `UNGUARDED_BLANK_THRESHOLD` and `BLANK_THRESHOLD_CANNOT_ASSESS`
    (the latter for a multi-line expression, or a `DIVIDE(` this checker could not fully parse -
    "did not assess" must never look like "clean") out of its exit-1 total, printing them as an
    advisory that still requires manual verification instead.

Expression LAYOUT - the class where a property is silently swallowed into the preceding DAX/M, or
where the document does not parse at all - is deliberately NOT here. Two hand-written grammars for
it (name matching, then the documented indentation contract) each shipped false negatives AND false
positives; `scripts/tmdl_oracle.py` answers those questions with the real parser instead.
"""

from __future__ import annotations

import codecs
import operator
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
# TMDL's own quoting grammar: a quoted name is closed only by an apostrophe NOT followed by
# another apostrophe (the doubled-apostrophe escape) - same rule check_field_bindings.py's
# `_NAME`/`_unquote` use for the identical reason (a name like 'O''Brien Sales' is one token).
_QUOTED_NAME_RE = re.compile(r"'(?:[^']|'')*'")
_DIVIDE_CALL_RE = re.compile(r"\bDIVIDE\s*\(", re.IGNORECASE)
_COALESCE_CALL_RE = re.compile(r"^\s*COALESCE\s*\(", re.IGNORECASE)
_LEADING_NUMERIC_CMP_RE = re.compile(r"^\s*(?P<op><=|>=|<>|<|>|=)\s*(?P<value>-?\d+(?:\.\d+)?)")
_NUMERIC_LITERAL_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_STRING_LITERAL_RE = re.compile(r'^"(?:[^"]|"")*"$')
_THRESHOLD_COMPARATORS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "=": operator.eq,
    "<>": operator.ne,
}

# The BLANK()-threshold family (issue #82) is advisory, not a structural error: round 2 of its
# review found the narrow text-only detector both misses real unsafe shapes (parenthesized,
# multiline, VAR-bound, measure-indirected DAX - proving those needs a real DAX parser, which this
# module deliberately does not build) and flags some provably-safe ones. `check_datamodel.py`
# reads this set to keep these findings out of its exit-1 total; they are printed as an advisory
# instead. See docs/tableau-dax-translation-guide.md, 'Threshold comparisons'.
ADVISORY_TMDL_CODES = frozenset({"UNGUARDED_BLANK_THRESHOLD", "BLANK_THRESHOLD_CANNOT_ASSESS"})


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


def _matching_close_paren(text: str, open_idx: int) -> int | None:
    """Return the index of the ')' matching the '(' at open_idx, string-aware."""
    depth = 0
    in_string = False
    idx = open_idx
    while idx < len(text):
        ch = text[idx]
        if ch == '"':
            if in_string and idx + 1 < len(text) and text[idx + 1] == '"':
                idx += 2
                continue
            in_string = not in_string
        elif not in_string:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return idx
        idx += 1
    return None


def _masked_positions(text: str) -> list[bool]:
    """Mark positions inside a DAX string literal or `//`/`/* */` comment.

    Not a DAX parser - just enough to stop a `DIVIDE(...)` pattern that only appears as prose
    inside a string literal or comment from being mistaken for a real call. Existing structural
    helpers (`_matching_close_paren`, `_split_top_level_args`) already track string state
    themselves when walking real code, so this is used only to filter candidate DIVIDE(...) match
    *start* positions before any of that runs.
    """
    masked = [False] * len(text)
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if ch == '"':
            start = idx
            idx += 1
            while idx < length:
                if text[idx] == '"':
                    if idx + 1 < length and text[idx + 1] == '"':
                        idx += 2
                        continue
                    idx += 1
                    break
                idx += 1
            for i in range(start, min(idx, length)):
                masked[i] = True
            continue
        if text[idx : idx + 2] == "//":
            for i in range(idx, length):
                masked[i] = True
            break
        if text[idx : idx + 2] == "/*":
            end = text.find("*/", idx + 2)
            end = length if end == -1 else end + 2
            for i in range(idx, end):
                masked[i] = True
            idx = end
            continue
        idx += 1
    return masked


def find_incomplete_divide_calls(expression: str) -> list[int]:
    """Positions of `DIVIDE(` calls this checker could not fully parse (no matching `)`).

    Either the DAX is genuinely malformed, or the expression continues past the single line of
    TMDL text handed in here - either way, whether it is guarded against `BLANK()` was NOT
    assessed, and that must be visible rather than silently read as "clean" (issue #82 round 2).
    """
    masked = _masked_positions(expression)
    positions: list[int] = []
    for match in _DIVIDE_CALL_RE.finditer(expression):
        start = match.start()
        if masked[start]:
            continue
        if start > 0 and (expression[start - 1].isalnum() or expression[start - 1] == "_"):
            continue
        if _matching_close_paren(expression, match.end() - 1) is None:
            positions.append(start)
    return positions


def find_unguarded_divide_thresholds(expression: str) -> list[tuple[str, float, int]]:
    """Detect every `DIVIDE(a, b[, alt]) <op> <literal>` comparison with no `BLANK()` guard (#82).

    `DIVIDE`'s **numerator** can make the call return `BLANK()` regardless of how many arguments
    it has - measured live in Desktop (Power BI Desktop 2.157.828.0, 2026-09-03; an earlier,
    unverified note here cited 2.140.x):

        DIVIDE(BLANK(), 1, 0)    < 0.05  ->  true   -- 3rd-argument "alt" does NOT catch this
        DIVIDE(1, 0, BLANK())    < 0.05  ->  true   -- ... nor does a blank "alt" itself

    So a 3-argument `DIVIDE` is only provably safe when BOTH the numerator and the alternate
    result are themselves provably non-blank (a numeric/string literal, or `COALESCE(..., <that
    literal>)`); it is never safe just because it has a 3rd argument at all - a real regression in
    the first cut of this check, which excluded every 3-argument call outright.

    Scope stays otherwise narrow, matching `find_compact_filters` in this module:

      * a `DIVIDE(...)` call is only in scope when the operator immediately following its closing
        `)` is a threshold comparator with a numeric literal on the right - `DIVIDE(...) * 100`
        or `DIVIDE(...) + [x]` are different shapes this does not reason about.
      * `DIVIDE(...)` is flagged wherever it appears in the expression - nested inside `IF(...)`,
        `CALCULATE(...FILTER(...))`, or anywhere else - not only when it is the entire expression.
        A call is excluded only when the exact same `DIVIDE(...)` text is wrapped in `ISBLANK(...)`
        earlier in the same expression (the guard pattern this repo recommends), or when its own
        arguments prove it cannot return `BLANK()` (see above).
      * only fires when `0` actually satisfies the comparison against the literal threshold (see
        the direction table in docs/tableau-dax-translation-guide.md) - the safe direction, where a
        blank operand reads `FALSE`, is never flagged.

    Returns a list of `(operator, threshold, position)` for every unguarded occurrence found. A
    `DIVIDE(...)` call that could not be fully parsed (no matching `)`) is NOT reported here - see
    `find_incomplete_divide_calls` - and neither is one whose match position falls inside a string
    literal or comment (see `_masked_positions`).
    """
    masked = _masked_positions(expression)
    findings: list[tuple[str, float, int]] = []
    for match in _DIVIDE_CALL_RE.finditer(expression):
        start = match.start()
        if masked[start]:
            continue  # e.g. a DIVIDE(...)-shaped example inside a string literal or comment
        if start > 0 and (expression[start - 1].isalnum() or expression[start - 1] == "_"):
            continue  # e.g. a hypothetical "SUBDIVIDE(" - not a DIVIDE call
        open_idx = match.end() - 1
        close = _matching_close_paren(expression, open_idx)
        if close is None:
            continue  # unparsable - reported separately by find_incomplete_divide_calls
        args = _split_top_level_args(expression[match.end() : close])
        if len(args) not in (2, 3):
            continue
        cmp_match = _LEADING_NUMERIC_CMP_RE.match(expression[close + 1 :])
        if not cmp_match:
            continue
        op = cmp_match.group("op")
        value = float(cmp_match.group("value"))
        if not _THRESHOLD_COMPARATORS[op](0, value):
            continue  # safe direction - a blank operand would read FALSE here
        if not _divide_call_can_be_blank(args):
            continue  # provably non-blank, e.g. DIVIDE(1, 1) or DIVIDE(COALESCE(x, 0), 1)
        call_text = expression[start : close + 1]
        guard = re.compile(r"ISBLANK\s*\(\s*" + re.escape(call_text) + r"\s*\)", re.IGNORECASE)
        if guard.search(expression[:start]):
            continue  # the same call is tested with ISBLANK(...) earlier in this expression
        findings.append((op, value, start))
    return findings


def _is_definitely_non_blank(arg: str) -> bool:
    """Whether a DAX argument text is a literal, or a COALESCE(...) defaulting to one.

    Deliberately conservative: a bare column/measure reference, or any other function call, is
    treated as "could be blank" even when it plausibly cannot be, because proving that in general
    needs real type/nullability information this text-only checker does not have.
    """
    text = arg.strip()
    if _NUMERIC_LITERAL_RE.match(text) or _STRING_LITERAL_RE.match(text):
        return True
    coalesce = _COALESCE_CALL_RE.match(text)
    if not coalesce:
        return False
    close = _matching_close_paren(text, coalesce.end() - 1)
    if close is None or text[close + 1 :].strip():
        return False  # COALESCE(...) is not the entire argument
    inner_args = _split_top_level_args(text[coalesce.end() : close])
    return bool(inner_args) and _is_definitely_non_blank(inner_args[-1])


def _is_definitely_nonzero_literal(arg: str) -> bool:
    """Whether a DAX argument is a numeric literal that is not `0`."""
    text = arg.strip()
    return bool(_NUMERIC_LITERAL_RE.match(text)) and float(text) != 0


def _divide_call_can_be_blank(args: list[str]) -> bool:
    """Whether a parsed `DIVIDE(...)` call could return `BLANK()`, from its argument text alone.

    2-argument `DIVIDE(a, b)`: blank whenever `a` is blank, OR `b` is `0`/blank - so it is
    provably non-blank only when `a` is non-blank AND `b` is a non-blank, non-zero literal.

    3-argument `DIVIDE(a, b, alt)`: `alt` only substitutes when `b` is `0`/blank - a blank `a`
    still propagates through untouched (measured above), and `alt` itself can be blank - so it is
    provably non-blank only when BOTH `a` and `alt` are non-blank (regardless of `b`, since every
    remaining branch is then covered: a valid `b` divides a non-blank `a`, an invalid `b` returns
    the non-blank `alt`).
    """
    if len(args) == 2:
        numerator, denominator = args
        return not (
            _is_definitely_non_blank(numerator)
            and _is_definitely_non_blank(denominator)
            and _is_definitely_nonzero_literal(denominator)
        )
    numerator, _denominator, alt = args
    return not (_is_definitely_non_blank(numerator) and _is_definitely_non_blank(alt))


def _strip_tmdl_comment(line: str) -> str:
    """Remove a TMDL description/comment line so prose cannot be mistaken for a property."""
    return "" if line.lstrip().startswith("///") else line


def _object_name(rest: str) -> str | None:
    """Extract a TMDL object's name from the remainder of its header line, unescaped."""
    head = rest.split("=", 1)[0].strip()
    if head.startswith("'"):
        match = _QUOTED_NAME_RE.match(head)
        return match.group(0)[1:-1].replace("''", "'") if match else None
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


def _threshold_findings(path: Path, kind: str, name: str, rest: str, number: int) -> list[TmdlFinding]:
    """Wrap `find_unguarded_divide_thresholds`/`find_incomplete_divide_calls` into `TmdlFinding`s.

    Advisory only (see `ADVISORY_TMDL_CODES`) - `check_datamodel.py` keeps these out of its exit-1
    total, printing them separately with a note that manual verification is still required.
    """
    expression = rest.split("=", 1)[1]
    findings = []
    for op, value, _position in find_unguarded_divide_thresholds(expression):
        threshold = f"{value:g}"
        findings.append(
            TmdlFinding(
                "UNGUARDED_BLANK_THRESHOLD",
                f"{kind} '{name}' compares DIVIDE(...) directly to `{op} {threshold}` with no ISBLANK "
                "guard (or a numerator/alternate-result that isn't provably non-blank). BLANK() "
                "coerces to 0 in this comparison - so a blank ratio silently satisfies "
                f"`{op} {threshold}` here, even inside an IF/CALCULATE, and even with a 3rd DIVIDE "
                "argument (which only substitutes for a 0/blank denominator, not a blank numerator). "
                f"Guard it: IF(ISBLANK(DIVIDE(...)), BLANK(), DIVIDE(...) {op} {threshold}), or make "
                "sure both the numerator and the alternate result are provably non-blank. ADVISORY "
                "ONLY - manual verification is still required. See "
                "docs/tableau-dax-translation-guide.md, 'Threshold comparisons'.",
                path,
                number,
            )
        )
    for _position in find_incomplete_divide_calls(expression):
        findings.append(
            TmdlFinding(
                "BLANK_THRESHOLD_CANNOT_ASSESS",
                f"{kind} '{name}' contains a DIVIDE(...) call this checker could not fully parse "
                "(no matching closing parenthesis on this line) - whether it is guarded against "
                "BLANK() was NOT assessed, and this is not a pass for that expression. Verify it "
                "manually. See docs/tableau-dax-translation-guide.md, 'Threshold comparisons'.",
                path,
                number,
            )
        )
    return findings


def _deferred_expression_header(rest: str) -> bool:
    """Whether an object header declares `=` but defers the expression to the following lines."""
    return "=" in rest and not rest.split("=", 1)[1].strip()


def _cannot_assess_finding(path: Path, kind: str, name: str, line: int) -> TmdlFinding:
    """A measure/column whose expression spans multiple lines - out of scope for this text checker."""
    return TmdlFinding(
        "BLANK_THRESHOLD_CANNOT_ASSESS",
        f"{kind} '{name}' has a multi-line expression; whether any DIVIDE(...) threshold comparison "
        "in it is guarded against BLANK() was NOT assessed (this checker only reads the header "
        "line). This is not a pass for that expression - verify it manually. See "
        "docs/tableau-dax-translation-guide.md, 'Threshold comparisons'.",
        path,
        line,
    )


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
    pending_deferred_expr: tuple[str, str, int] | None = None  # (kind, name, line) for BLANK_THRESHOLD_CANNOT_ASSESS

    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_tmdl_comment(raw)
        if not line.strip():
            continue

        if pending_measure and _OBJECT_RE.match(line):
            findings.append(_empty_measure_finding(path, pending_measure))
            pending_measure = None
        if pending_deferred_expr and _OBJECT_RE.match(line):
            pending_deferred_expr = None  # genuinely empty - EMPTY_EXPRESSION (if any) covers it

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
                if _expression_on_measure_header(rest):
                    pending_measure = None
                    findings.extend(_threshold_findings(path, "measure", name, rest, number))
                else:
                    pending_measure = (name, number)
                    if _deferred_expression_header(rest):
                        pending_deferred_expr = ("measure", name, number)
            elif kind == "column" and name and current_table:
                columns.setdefault(name, number)
                if _expression_on_measure_header(rest):
                    findings.extend(_threshold_findings(path, "column", name, rest, number))
                elif _deferred_expression_header(rest):
                    pending_deferred_expr = ("column", name, number)
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
            pending_deferred_expr = None  # a property line with no prior content: genuinely empty
            continue

        if pending_measure and line.strip():
            pending_measure = None
        if pending_deferred_expr and line.strip():
            findings.append(_cannot_assess_finding(path, *pending_deferred_expr))
            pending_deferred_expr = None

    if pending_measure:
        findings.append(_empty_measure_finding(path, pending_measure))

    _append_name_collisions(findings, path, current_table, measures, columns)
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


def _iter_measure_declarations(text: str):
    """Yield (line, table, measure name) for every measure header in one document.

    Deliberately independent of `check_tmdl_text`'s own per-table bookkeeping, which resets at each
    table boundary and is not visible outside that function - model-wide uniqueness needs every
    measure name from every file, not just the last table seen in each.
    """
    current_table = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_tmdl_comment(raw)
        if not line.strip():
            continue
        obj = _OBJECT_RE.match(line)
        if not obj:
            continue
        kind = obj.group("kind")
        name = _object_name(obj.group("rest"))
        if kind == "table" and name:
            current_table = name
        elif kind == "measure" and name and current_table:
            yield number, current_table, name


def _duplicate_measure_finding(
    first_seen: dict[str, tuple[Path, int, str]], tmdl: Path, line: int, table: str, name: str
) -> TmdlFinding | None:
    """Record `name`'s first sighting, or report it as a model-wide duplicate on a later one.

    Tabular measure names are case-insensitive and unique across the WHOLE model, not merely
    within a table - unlike the same-table NAME_COLLISION check above, this fires across
    DIFFERENT tables and DIFFERENT files.
    """
    key = name.casefold()
    seen = first_seen.get(key)
    if seen is None:
        first_seen[key] = (tmdl, line, table)
        return None
    first_file, first_line, first_table = seen
    return TmdlFinding(
        "MEASURE_NAME_DUPLICATE",
        f"measure '{name}' in table '{table}' repeats the model-wide name already used "
        f"by table '{first_table}' ({first_file.name}:{first_line}); Tabular measure "
        "names must be unique across the WHOLE model, not just within one table - "
        "Desktop refuses to load/commit this even though it deserializes clean.",
        tmdl,
        line,
    )


def check_tmdl_model(model_dir: Path) -> tuple[list[TmdlFinding], int]:
    """Every TMDL document in one .SemanticModel."""
    findings: list[TmdlFinding] = []
    definition = model_dir / "definition"
    files = sorted(definition.rglob("*.tmdl")) if definition.exists() else []
    first_seen: dict[str, tuple[Path, int, str]] = {}
    for tmdl in files:
        text, problems = _read_tmdl(tmdl)
        findings.extend(problems)
        if text is None:
            continue
        findings.extend(check_tmdl_text(tmdl, text))
        for line, table, name in _iter_measure_declarations(text):
            finding = _duplicate_measure_finding(first_seen, tmdl, line, table, name)
            if finding is not None:
                findings.append(finding)
    return findings, len(files)
