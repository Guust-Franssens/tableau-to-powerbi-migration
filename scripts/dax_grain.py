"""
purpose: read what a DAX measure ADDRESSES and what grain a model column IS - the model-side half
         of the running-total axis gate, with no knowledge of PBIR, reports or verdicts.
usage:   import dax_grain; dax_grain.classify(member); dax_grain.read_model_facts(model_dir)

internal-reason: library split out of `scripts/check_running_total_axis.py` (#218) when that module
crossed pylint's `max-module-lines` cap. The seam is real rather than arithmetic: everything here
answers "what does this DAX address, and what is this column?" from TMDL alone, and nothing here
can see a report. That makes the lineage rules independently testable, and it is the half a second
gate would reuse first.

The one idea worth carrying across: a grain is decided from a PROPERTY, never a proxy. The engine
writes its coarse date bins as calculated TEXT columns - `Month = FORMAT('Date'[Date], "MMM")`,
`Quarter = "Q" & QUARTER('Date'[Date])` - carrying no `dataType` at all (95 such columns in
`_runs/estate-2.339.0-20260829`), and their filters survive exactly like a `dateTime` bin's. So
`ModelFacts.grain_of` reads declared type AND calculated lineage back to the anchor column, and
falls back to `unassessable` on a name-only hint rather than to "clean".

THE RECOGNISED GRAMMAR, and the two rules that make it safe
-----------------------------------------------------------
This module is a RECOGNISER, not a DAX parser, and rounds 1-4 of review all found the same shape of
bug: a spelling it did not recognise fell out of the analysis silently and the measure was reported
CLEAN. Round 4 measured three of them on one afternoon - a parenthesised predicate
`('Orders'[Order_Date] <= MAX('Orders'[Order_Date]))` exited **0** where the identical unwrapped
predicate exited 1; `MAX(t[c]) >= t[c]` exited 0; a six-hop `VAR` chain exited 0. So:

1. **Nothing may leave the recogniser silently.** Every reader consumes its input or records what it
   could not account for - the `residue` - and residue always lands in `assessable = False`, i.e.
   `UNASSESSABLE` / exit 3. "I did not understand this" is now a different value from "there is
   nothing here", and only the second one may be clean. `_read_conjunct` is where that is decided
   for a predicate; `WindowCall`/`AsOfCall`/`PeriodToDateCall` carry it for the three mechanisms.
2. **A classifier never stops at the first match.** Every candidate is folded - window calls,
   as-of calls, period-to-date calls, `VAR` hops, `&&` conjuncts, `ALL(...)` removals and, since
   round 4, every MAX-like call inside ONE bound. `_fold_bound_kinds` is the single fold for bound
   kinds, `check_running_total_axis._worst` for verdicts, and
   `tests/test_check_running_total_axis.py::test_no_fold_function_returns_from_inside_a_loop`
   enforces the rule mechanically rather than by convention.

What is recognised, exhaustively - a predicate conjunct is read only as an optionally
parenthesised, top-level `<bare column> <op> <expression>` (or its mirror image). Anything else that
could contain a comparison is residue. Declared NON-GOALS, deliberately not judged rather than
accidentally passed: `FILTER(<non-ALL relation>, ...)` with no moving bound (it clears nothing, so
the "cleared anchor, surviving axis" shape cannot arise; WITH a moving bound it is reported
`unassessable`, being degenerate on every axis); `MOVINGAVERAGE`/`RUNNINGSUM`, whose `<relation>` is
required so the visual never decides it; and boolean-function predicates (`ISONORAFTER`,
`CONTAINSROW`) carrying no comparison operator at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from check_relationship_health import _parse_column_census
from check_stub_measures import is_stub_expression, parse_model, strip_comments

# Window functions whose `<relation>` argument is OPTIONAL and, when omitted, defaults to the
# visual's own shaped table. That default is the whole reason the visual's axis is the contract.
# MOVINGAVERAGE/RUNNINGSUM are excluded on purpose: their relation is REQUIRED, so the visual never
# decides their ordering domain and this gate has no standing to judge them.
WINDOW_FUNCTIONS = ("WINDOW", "OFFSET", "INDEX", "RANK", "ROWNUMBER")

# Bare positional tokens a window function may legally carry that are NOT a relation. Anything else
# in a positional slot is treated as an explicit relation, which makes the call unassessable.
_POSITIONAL_KEYWORDS = frozenset(
    {
        "ABS",
        "REL",
        "ASC",
        "DESC",
        "DEFAULT",
        "DENSE",
        "SKIP",
        "KEEP",
        "NONE",
        "BLANK",
        "FIRST",
        "LAST",
        "TRUE",
        "FALSE",
    }
)
_CLAUSE_FUNCTIONS = ("ORDERBY", "PARTITIONBY", "MATCHBY")
_NUMBER_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")

# `'Table'[Column]`, `Table[Column]` or a bare `[Column]`. A `'` inside a quoted table name is
# doubled in DAX exactly as it is in TMDL, so the alternation mirrors `check_field_bindings._NAME`.
_COLUMN_REF_RE = re.compile(r"(?:'((?:[^']|'')*)'|([A-Za-z_][\w. ]*?))?\s*\[([^\]]+)\]")

# Which argument of a period-to-date call carries the <dates> column. `TOTALYTD(<expr>, <dates>)`
# puts it second; `DATESYTD(<dates>)` first.
_PERIOD_TO_DATE_FUNCTIONS = {
    "TOTALYTD": 1,
    "TOTALMTD": 1,
    "TOTALQTD": 1,
    "DATESYTD": 0,
    "DATESMTD": 0,
    "DATESQTD": 0,
}
# The engine's own words when it could not translate a Tableau running total, plus the name shapes
# a hand-authored one uses. Used ONLY to surface a `BLANK()` stub as a former running total - never
# to judge one, because a stub has no DAX shape to judge.
_RUNNING_STUB_RE = re.compile(r"\bRUNNING[_ ]?(SUM|AVG|COUNT|MAX|MIN)\b|running|cumulative", re.IGNORECASE)
_ALL_FUNCTION_RE = re.compile(r"^ALL(SELECTED|NOBLANKROW|CROSSFILTERED|EXCEPT)?$", re.IGNORECASE)

# An as-of bound that moves with the visual's current date. `MAX` is the canonical one; the
# end-of-period family behaves identically because each is evaluated in the current filter context.
# Matching one of these is NECESSARY and not sufficient - see `_own_bound_kinds`.
_CONTEXT_BOUND_RE = re.compile(
    r"\b(MAX|MAXX|LASTDATE|LASTNONBLANK|ENDOFMONTH|ENDOFQUARTER|ENDOFYEAR|SELECTEDVALUE)\s*\(",
    re.IGNORECASE,
)

# Context REMOVAL inside the bound itself. `MAXX(ALL('Orders'), 'Orders'[Order_Date])` reads the
# whole table with every visual filter discarded, so it evaluates to one global constant and cannot
# move with the axis - it is a pinned cutoff wearing a `MAX`. Longest alternatives first so
# `ALLSELECTED(` is never read as `ALL` + junk.
_CONTEXT_REMOVAL_RE = re.compile(
    r"\b(ALLEXCEPT|ALLSELECTED|ALLNOBLANKROW|ALLCROSSFILTERED|REMOVEFILTERS|ALL)\s*\(",
    re.IGNORECASE,
)

# Every comparison DAX spells with `<`, `>` or `=`, longest first so `<>` and `<=` are recognised
# BEFORE the `<` they both start with. Ordering is the whole fix for review finding 2: a regex that
# excluded only a following `=` matched the `<` inside `<>`, and every ordinary exclusion filter
# became a running total.
_COMPARISON_OPERATORS = ("<=", ">=", "<>", "==", "=", "<", ">")
_LOGICAL_OPERATORS = ("&&", "||")
_VAR_RE = re.compile(
    r"\bVAR\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<body>.*?)(?=\bVAR\b|\bRETURN\b)", re.IGNORECASE | re.DOTALL
)
# How many `VAR` hops an as-of bound may be hoisted through before the chase is abandoned.
_VAR_DEPTH = 4

# Names that SUGGEST a date grain without proving one. Used only to route an otherwise-undecidable
# grouping column to `unassessable`, never to a mismatch: a guess must not fail a build.
_DATE_PART_NAME_RE = re.compile(
    r"(^|[\s_\-])(year|quarter|qtr|month|week|date|period|fiscal|semester|yyyy)([\s_\-]|$)", re.IGNORECASE
)

# PRESENTATIONAL ONLY, and it lives in the GATE (`check_running_total_axis.AXIS_ROLES`) because it
# is about rendering a report finding, not about reading a model. Nothing in this module may filter
# by role: a curated axis-role list is exactly the proxy that let a `dateTime` grain under a
# pivotTable's `Columns` role pass silently.

_DATE_TYPES = frozenset({"date", "datetime"})

# How a grouping column relates to the accumulation's anchor column. `DATE`/`DERIVED` are proven and
# may fail a build; `SUSPECT` is a name-only hint and may only reach `unassessable`.
GRAIN_DATE = "date"
GRAIN_DERIVED = "derived"
GRAIN_SUSPECT = "suspect"
GRAIN_UNRELATED = "unrelated"


@dataclass(frozen=True)
class ColumnRef:
    """One `'Table'[Column]` reference, with the table left None when DAX did not qualify it."""

    table: str | None
    column: str

    def qualified(self) -> str:
        """The reference as a human would write it back into DAX."""
        return f"'{self.table}'[{self.column}]" if self.table else f"[{self.column}]"

    def key(self) -> tuple[str, str]:
        """Case-insensitive identity, so a casing-only difference does not invent a finding."""
        return ((self.table or "").casefold(), self.column.casefold())


@dataclass
class WindowCall:
    """ONE `WINDOW`/`OFFSET`/`INDEX`/`RANK`/`ROWNUMBER` call, judged entirely on its own.

    Round 2 fixed "the first window call decided the measure" by unioning every call's ORDERBY
    columns. That closed the false negative but left a second first-match: an UNREADABLE call
    (an explicit relation, an unqualified ORDERBY) returned early and made the whole measure
    `unassessable`, suppressing a readable call's mismatch. Measured: an explicit-relation call
    beside `WINDOW(... ORDERBY('Orders'[Region]))` on an `Order_Date` axis exited 3, where the
    defective call alone exits 1. Never a pass, but the wrong verdict - worst must win.
    """

    func: str
    ordered_by: list[ColumnRef] = field(default_factory=list)
    partition_by: list[ColumnRef] = field(default_factory=list)
    assessable: bool = True
    reason: str = ""


@dataclass
class PeriodToDateCall:
    """ONE `TOTALYTD`/`DATESYTD`-family call, judged entirely on its own.

    `_classify_period_to_date` returned after the first match found while walking
    `_PERIOD_TO_DATE_FUNCTIONS` - so not even the first in the TEXT, the first in dict order.
    Measured: `TOTALYTD(SUM('Orders'[Sales]), 'Date'[Date]) + CALCULATE(SUM('Orders'[Sales]),
    DATESYTD('Orders'[Order_Date]))` on a `'Date'[Month Start]` axis exited **0** (`date_table_marked`)
    while the `DATESYTD` term alone exits 3. A silent pass.
    """

    func: str
    anchor: ColumnRef | None = None
    assessable: bool = True
    reason: str = ""


@dataclass
class AsOfCall:
    """ONE `FILTER(ALL(...), <col> <= <moving bound>)` restriction, judged entirely on its own.

    A measure may carry several, and they are NOT interchangeable: each names its own compared
    column and its own cleared set. Folding them into the `Cumulative` - a single `compared` plus a
    UNION of every cleared column - is what let a safe first call excuse a defective later one
    (round 2 finding 3). The union is the specific trap: call A clearing `Order_Date` AND
    `Order Date (Month)` would supply the month clearance that call B, which clears only
    `Order_Date`, does not have.

    `compared` is optional because a call may exist while being unreadable: a predicate this module
    cannot fully account for is carried here with `assessable = False` rather than dropped (round 4
    finding 3 - a parenthesised predicate was dropped and the measure exited 0).

    `bound_removed_*` is the round-4 finding-1 fix. WHETHER THE BOUND MOVES IS NOT A PROPERTY OF THE
    DAX ALONE - it depends on the visual. `CALCULATE(MAX('Orders'[Order_Date]),
    REMOVEFILTERS('Orders'[Order Month Label]))` moves along `Order_Date` and is pinned along
    `Order Month Label`, so on a month-label axis it is an ordinary fixed-cutoff bucket measure -
    yet the gate reported MISMATCH (measured: exit 1, against exit 0 for the whole-table spelling).
    The removed references therefore survive classification, for the judge to compare with the
    visual's own grouping columns.
    """

    compared: ColumnRef | None = None
    cleared_columns: list[ColumnRef] = field(default_factory=list)
    cleared_tables: list[str] = field(default_factory=list)
    bound_removed_columns: list[ColumnRef] = field(default_factory=list)
    bound_removed_tables: list[str] = field(default_factory=list)
    assessable: bool = True
    reason: str = ""

    def pins(self, table: str, column: str) -> bool:
        """Whether the BOUND's own filter removal covers this grouping column.

        When it does, the cutoff cannot move from one of that column's buckets to the next, so the
        per-bucket value is a cutoff total by construction rather than a collapsed accumulation.
        """
        if table.casefold() in {name.casefold() for name in self.bound_removed_tables}:
            return True
        return ColumnRef(table, column).key() in {ref.key() for ref in self.bound_removed_columns}


@dataclass
class Cumulative:  # pylint: disable=too-many-instance-attributes
    """One measure whose DAX declares one or more accumulation grains, and how confidently we read
    each of them.

    Each mechanism is a LIST of independent calls and `check_running_total_axis._worst` picks the
    worst verdict across all of them - rule 2 in this module's docstring, arrived at the hard way
    over four review rounds at eight separate sites, three of which were SILENT PASSES.
    """

    table: str
    name: str
    shape: str
    tmdl: str
    line: int
    window_calls: list[WindowCall] = field(default_factory=list)
    as_of_calls: list[AsOfCall] = field(default_factory=list)
    period_calls: list[PeriodToDateCall] = field(default_factory=list)
    assessable: bool = True
    reason: str = ""

    @property
    def label(self) -> str:
        """`'Table'[Measure]`, the spelling that can be pasted into a DAX query."""
        return f"'{self.table}'[{self.name}]"

    @property
    def ordered_by(self) -> list[ColumnRef]:
        """Every ordering key across every window call. PRESENTATION ONLY - verdicts are per call."""
        return [ref for call in self.window_calls for ref in call.ordered_by]

    @property
    def partition_by(self) -> list[ColumnRef]:
        """Every partition key across every window call. PRESENTATION ONLY."""
        return [ref for call in self.window_calls for ref in call.partition_by]

    @property
    def compared(self) -> ColumnRef | None:
        """The first as-of or period-to-date anchor. PRESENTATION AND ROUTING ONLY.

        Deliberately not a verdict input: every judgement is formed per call, from the lists above.
        """
        for call in self.as_of_calls:
            if call.compared is not None:
                return call.compared
        for period in self.period_calls:
            if period.anchor is not None:
                return period.anchor
        return None


@dataclass
class ModelFacts:
    """Everything about the model this gate reasons from, read once per pair.

    The declared scalar type used to be the whole story, and that was a PROXY for the question that
    matters - *does this grouping column's filter survive?* Measured: the engine emits its coarse
    grains as `column Month = FORMAT('Date'[Date], "MMM")` and `column Quarter = "Q" & QUARTER(...)`,
    which carry no `dataType` at all (95 such columns in `_runs/estate-2.339.0-20260829`). Their
    filters survive exactly like a `dateTime` bin's. So lineage - "is this column computed FROM the
    anchor date?" - is read alongside the type, and it is a fact in the TMDL, not a heuristic.
    """

    column_types: dict[str, Any] = field(default_factory=dict)
    calc_expressions: dict[tuple[str, str], str] = field(default_factory=dict)
    time_tables: set[str] = field(default_factory=set)

    def data_type(self, table: str, column: str) -> str:
        """The declared `dataType` of one column, casefolded, or "" when undeclared."""
        for name, info in self.column_types.items():
            if name.casefold() != table.casefold():
                continue
            for col_name, col in info.columns.items():
                if col_name.casefold() == column.casefold():
                    return (col.data_type or "").casefold()
        return ""

    def is_time_table(self, table: str) -> bool:
        """Whether this table is marked `dataCategory: Time` (Power BI's "Mark as date table")."""
        return table.casefold() in self.time_tables

    def derives_from(self, ref: ColumnRef, anchor: ColumnRef) -> bool:
        """Whether `ref` is a calculated column computed, transitively, from `anchor`.

        `Month = FORMAT('Date'[Date], "MMM")` derives from `'Date'[Date]` directly;
        `Quarter = "Q" & QUARTER('Date'[Date])` likewise; a chain through `Month No` is followed too.
        An unqualified reference inside a calculated column resolves to that column's own table.
        """
        return self._derives(ref, anchor, set())

    def _derives(self, ref: ColumnRef, anchor: ColumnRef, seen: set[tuple[str, str]]) -> bool:
        if ref.key() in seen:
            return False
        seen.add(ref.key())
        expression = self.calc_expressions.get(ref.key())
        if not expression:
            return False
        for found in _column_refs(expression):
            target = ColumnRef(found.table or ref.table, found.column)
            if target.key() == anchor.key():
                return True
            if self._derives(target, anchor, seen):
                return True
        return False

    def grain_of(self, ref: ColumnRef, anchor: ColumnRef) -> str:
        """How a grouping column relates to the anchor date - the property the verdict turns on."""
        if self.derives_from(ref, anchor):
            return GRAIN_DERIVED
        if self.data_type(ref.table or "", ref.column) in _DATE_TYPES:
            return GRAIN_DATE
        if _DATE_PART_NAME_RE.search(ref.column):
            return GRAIN_SUSPECT
        return GRAIN_UNRELATED


def read_model_facts(model_dir: Path) -> ModelFacts:
    """Read the column types, calculated-column expressions and date-table markings in one pass."""
    facts = ModelFacts(column_types=_parse_column_census(model_dir))
    for member in parse_model(model_dir):
        if member.kind == "column" and (member.expression or "").strip():
            facts.calc_expressions[(member.table.casefold(), member.name.casefold())] = member.expression
    definition = model_dir / "definition"
    root = definition if definition.is_dir() else model_dir
    for path in sorted(root.rglob("*.tmdl")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        table: str | None = None
        for line in text.splitlines():
            match = _TABLE_DECL_RE.match(line)
            if match:
                table = _unquote_tmdl(match.group("name"))
            elif table and _TIME_CATEGORY_RE.match(line):
                facts.time_tables.add(table.casefold())
    return facts


_TABLE_DECL_RE = re.compile(r"^table\s+(?P<name>'(?:[^']|'')*'|[^\s=]+)\s*$")
_TIME_CATEGORY_RE = re.compile(r"^\s+dataCategory\s*:\s*Time\s*$", re.IGNORECASE)


def _unquote_tmdl(name: str) -> str:
    """Strip TMDL's single-quoting from an object name."""
    name = name.strip()
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        return name[1:-1].replace("''", "'")
    return name


def _split_arguments(text: str) -> list[str]:
    """Split a DAX argument list at depth 0, respecting strings, parens and brackets.

    `str.split(",")` cannot do this: `ORDERBY('T'[A], ASC)` is ONE argument of `WINDOW`, and a table
    name may legally contain a comma inside its quotes.
    """
    args: list[str] = []
    depth = 0
    in_string = False
    current: list[str] = []
    for char in text:
        if in_string:
            current.append(char)
            if char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            current.append(char)
        elif char in "([":
            depth += 1
            current.append(char)
        elif char in ")]":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail or args:
        args.append(tail)
    return args


def _balanced(text: str) -> bool:
    """Whether this fragment closes every `(`, `[`, `"` and `'` it opens, and opens every close.

    The precondition every other reader here assumes and none of them used to check. An unbalanced
    fragment means the caller has cut DAX in the wrong place, and a scanner run over it reports
    depths that are simply wrong - which is a residue case, never a clean one.
    """
    depth = 0
    in_string = False
    in_name = False
    for char in text:
        if in_string:
            in_string = char != '"'
        elif in_name:
            in_name = char != "'"
        elif char == '"':
            in_string = True
        elif char == "'":
            in_name = True
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string and not in_name


def _strip_enclosing_parens(text: str) -> str:
    """Remove the parentheses that wrap a WHOLE fragment, however many layers deep.

    Round 4 finding 3, and the highest-severity bug in this file's history: `_split_top_level` put
    every operator of `('Orders'[Order_Date] <= MAX('Orders'[Order_Date]))` at depth 1, so the
    predicate contained no recognised upper bound, the measure was dropped from the report entirely
    and the run exited **0** - where the identical unwrapped predicate exits 1. Parentheses are
    DAX's ordinary grouping operator, so this is not exotic input. `(a) && (b)` must NOT be
    stripped: the leading `(` closes before the end, which `_balanced` detects on the inner text.
    """
    text = text.strip()
    while len(text) >= 2 and text.startswith("(") and text.endswith(")"):
        inner = text[1:-1]
        if not _balanced(inner):
            break
        text = inner.strip()
    return text


def _contains_comparison(text: str) -> bool:
    """Whether ANY comparison operator appears at ANY depth, outside a string, name or bracket.

    Asked only of a fragment with no top-level comparison, to separate the two cases the old code
    conflated: `NOT('Orders'[Order_Date] > MAX(...))` hides an upper bound one level down and must
    be residue, while `ISBLANK('Orders'[Sales])` carries no comparison at all and therefore cannot
    be hiding one - it is fully accounted for, and not a bound.
    """
    in_string = False
    in_name = False
    in_bracket = False
    for index, char in enumerate(text):
        if in_string:
            in_string = char != '"'
        elif in_name:
            in_name = char != "'"
        elif in_bracket:
            in_bracket = char != "]"
        elif char == '"':
            in_string = True
        elif char == "'":
            in_name = True
        elif char == "[":
            in_bracket = True
        elif any(text.startswith(op, index) for op in _COMPARISON_OPERATORS):
            return True
    return False


def _call_bodies(expr: str, name: str) -> list[str]:
    """The argument text of every `name(...)` call in `expr`, at any nesting depth."""
    bodies: list[str] = []
    pattern = re.compile(rf"\b{re.escape(name)}\s*\(", re.IGNORECASE)
    for match in pattern.finditer(expr):
        depth = 0
        in_string = False
        for index in range(match.end() - 1, len(expr)):
            char = expr[index]
            if in_string:
                if char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    bodies.append(expr[match.end() : index])
                    break
    return bodies


def _column_refs(text: str) -> list[ColumnRef]:
    """Every column reference in a fragment of DAX, table-qualified where DAX qualified it."""
    refs: list[ColumnRef] = []
    for quoted, bare, column in _COLUMN_REF_RE.findall(text):
        table = quoted.replace("''", "'") if quoted else (bare.strip() or None)
        refs.append(ColumnRef(table=table, column=column.strip()))
    return refs


def _is_positional(arg: str) -> bool:
    """Whether a window-function argument is a bare literal/keyword rather than a relation."""
    token = arg.strip().rstrip(")").strip()
    return bool(_NUMBER_RE.match(token)) or token.upper() in _POSITIONAL_KEYWORDS


def _clause_bodies(args: Iterable[str], name: str) -> list[str]:
    """The body of EVERY `name(...)` clause among a window function's arguments.

    The documented grammars provide one `orderBy`/`partitionBy` slot per call, and a blind review
    confirmed that reading (A5): multiple keys belong inside the single clause. `_clause` used to
    take the first match and rely on that being the only match. It now VERIFIES it - a second
    clause means the call is not the grammar this module recognises, so it is residue.
    """
    prefix = re.compile(rf"^{name}\s*\(", re.IGNORECASE)
    bodies: list[str] = []
    for arg in args:
        if prefix.match(arg.strip()):
            bodies.extend(_call_bodies(arg, name)[:1])
    return bodies


def _window_call_sites(expr: str) -> list[tuple[str, list[str]]]:
    """Every window-family call in the expression, as (function name, split arguments).

    ALL of them, not the first. The invariant this gate states is that *every* ordered column must
    be projected; returning from inside the first call site quietly narrowed that to the first one,
    and a second `WINDOW(... ORDERBY(<unprojected>))` in the same measure passed (measured on an
    estate-shaped measure with two windows: exit 0 with the second ordering column absent).
    """
    sites: list[tuple[str, list[str]]] = []
    for func in WINDOW_FUNCTIONS:
        for body in _call_bodies(expr, func):
            sites.append((func, _split_arguments(body)))
    return sites


def _read_window_call(func: str, args: list[str]) -> WindowCall:
    """Read ONE window-family call, recording why its grain cannot be read rather than returning."""
    call = WindowCall(func=func)
    named = [a for a in args if any(re.match(rf"^{c}\s*\(", a.strip(), re.IGNORECASE) for c in _CLAUSE_FUNCTIONS)]
    positional = [a for a in args if a not in named]
    if any(not _is_positional(arg) for arg in positional):
        call.assessable = False
        call.reason = (
            f"{func} carries an explicit relation argument, so the ordering domain is that "
            "table expression rather than the visual"
        )
        return call
    order_bodies = _clause_bodies(args, "ORDERBY")
    partition_bodies = _clause_bodies(args, "PARTITIONBY")
    if len(order_bodies) > 1 or len(partition_bodies) > 1:
        # The grammars give ONE orderBy/partitionBy slot per call, so a second one means this is not
        # the shape read here. Verified rather than assumed - see `_clause_bodies`.
        call.assessable = False
        call.reason = f"{func} carries more than one ORDERBY/PARTITIONBY clause, which is not the grammar read here"
        return call
    if not order_bodies:
        # No ORDERBY means this call orders by the relation's own columns, and the relation IS the
        # visual. There is no second grain that could disagree - a verified acquittal, not a guess.
        call.reason = f"{func} has no ORDERBY clause, so it orders by the visual's own grain"
        return call
    ordered = _column_refs(order_bodies[0])
    if not ordered:
        call.assessable = False
        call.reason = f"{func} ORDERBY names no resolvable column reference"
        return call
    if any(ref.table is None for ref in ordered):
        call.assessable = False
        call.reason = f"{func} ORDERBY uses an unqualified column, so its table is ambiguous"
        return call
    call.ordered_by = ordered
    call.partition_by = _column_refs(partition_bodies[0]) if partition_bodies else []
    return call


def _classify_window(expr: str, base: Cumulative) -> Cumulative | None:
    """Read EVERY window-family call independently; none of them may silence another.

    Round 1 returned from inside the first call site, so a second `WINDOW(... ORDERBY(<unprojected>))`
    passed. Round 2 unioned the ordering keys, which fixed that but left an unreadable call still
    returning early and suppressing a readable call's mismatch. Both are the same bug; a list is
    the fix for both.
    """
    sites = _window_call_sites(expr)
    if not sites:
        return None
    base.window_calls.extend(_read_window_call(func, args) for func, args in sites)
    return base


BOUND_CONTEXT = "context"
BOUND_CONSTANT = "constant"
BOUND_UNRESOLVED = "unresolved"
# Two or more upper bounds could each be the accumulation's, or the predicate is a disjunction.
# Distinct from `unresolved` only so the reason text can say WHICH kind of doubt it is.
BOUND_AMBIGUOUS = "ambiguous"


def _resolve_vars(expr: str) -> dict[str, str]:
    """`VAR <name> = <body>` pairs declared in one measure, so a hoisted as-of date can be followed."""
    return {match.group("name").casefold(): match.group("body").strip() for match in _VAR_RE.finditer(expr)}


def _same_column(ref: ColumnRef, compared: ColumnRef) -> bool:
    """Whether two references name the same column, tolerating one of them being unqualified."""
    if ref.column.casefold() != compared.column.casefold():
        return False
    if ref.table is None or compared.table is None:
        return True
    return ref.table.casefold() == compared.table.casefold()


REMOVAL_ALL = "all"
REMOVAL_TABLE = "table"
REMOVAL_COLUMN = "column"
REMOVAL_UNRELATED = "unrelated"
REMOVAL_INVERTED = "inverted"


def _removal_scope(text: str, compared: ColumnRef) -> list[str]:
    """What EVERY filter-removal call in a bound removes, relative to the compared column.

    Round 2 made any `ALL`/`REMOVEFILTERS` mean "pinned"; round 3 found the missing half - **pinned
    with respect to WHAT**. Measured, the reviewer's expression:

        VAR _asOf = CALCULATE(MAX('Orders'[Order_Date]), REMOVEFILTERS('Orders'[Region]))

    `REMOVEFILTERS(Region)` cannot touch a month-axis filter, so `_asOf` is still the current
    month's maximum and the measure is a real running total - one that then hits the uncleared
    coarse-axis defect this gate exists to catch. Reading the first removal and stopping dropped
    the measure entirely: `classify() -> None`, zero cumulative measures, exit 0.
    """
    scopes: list[str] = []
    for match in _CONTEXT_REMOVAL_RE.finditer(text):
        func = match.group(1).upper()
        if func == "ALLEXCEPT":
            # ALLEXCEPT inverts the set: it keeps the filters on the columns it names, so the bound
            # may still move. This gate does not model an inverted set.
            scopes.append(REMOVAL_INVERTED)
            continue
        bodies = _call_bodies(text[match.start() :], match.group(1))
        arguments = [arg.strip() for arg in _split_arguments(bodies[0])] if bodies else []
        if not any(arguments):
            # `ALL()` / `REMOVEFILTERS()` with no argument clears the whole model.
            scopes.append(REMOVAL_ALL)
            continue
        columns = _column_refs(bodies[0])
        tables = [arg.strip("'").replace("''", "'") for arg in arguments if arg and "[" not in arg]
        if any(name.casefold() == (compared.table or "").casefold() for name in tables):
            scopes.append(REMOVAL_TABLE)
        elif any(_same_column(ref, compared) for ref in columns):
            scopes.append(REMOVAL_COLUMN)
        else:
            scopes.append(REMOVAL_UNRELATED)
    return scopes


@dataclass
class BoundReading:
    """What ONE upper-bound expression is, and what filters it removes on the way.

    `kind` is the fold of every independent judgement the bound supports; `removed_*` survives so
    the JUDGE can ask the question this module cannot - "does that removal cover the visual's own
    grouping column?" See `AsOfCall.pins`.
    """

    kind: str
    removed_columns: list[ColumnRef] = field(default_factory=list)
    removed_tables: list[str] = field(default_factory=list)


def _context_bound_kinds(text: str, compared: ColumnRef) -> list[str]:
    """One kind per MAX-like call in the bound - EVERY one of them, never the first.

    Round 4 finding 2, the SEVENTH first-match defect in this file. `_classify_moving_bound`
    returned `context` as soon as one MAX-like call named the compared column, so a sibling call on
    a foreign column was invisible. Measured, two semantically identical bounds:
    `MIN(MAX('Orders'[Order_Date]), MAX('Cutoff'[Date]))` exited **1** (MISMATCH) while the same
    thing hoisted into two `VAR`s exited **3** (UNASSESSABLE). DAX explicitly permits `MIN` over two
    scalar expressions, so both spellings are valid and they must agree.
    """
    kinds: list[str] = []
    for match in _CONTEXT_BOUND_RE.finditer(text):
        bodies = _call_bodies(text[match.start() :], match.group(1))
        if bodies and any(_same_column(ref, compared) for ref in _column_refs(bodies[0])):
            kinds.append(BOUND_CONTEXT)
        else:
            # A foreign column - `MAX('Date'[Date])` bounding `'Orders'[Order_Date]` - may be an
            # as-of date reached through a relationship, and may equally be something else.
            kinds.append(BOUND_UNRESOLVED)
    return kinds


def _bound_removals(text: str) -> tuple[list[ColumnRef], list[str]]:
    """Every column and table whose filter this bound's own `ALL`/`REMOVEFILTERS` calls discard.

    `ALLEXCEPT` is skipped on purpose: it KEEPS the filters on the columns it names, so listing
    them here would invert the meaning. It is separately routed to `unresolved`.
    """
    columns: list[ColumnRef] = []
    tables: list[str] = []
    for match in _CONTEXT_REMOVAL_RE.finditer(text):
        if match.group(1).upper() == "ALLEXCEPT":
            continue
        bodies = _call_bodies(text[match.start() :], match.group(1))
        if not bodies:
            continue
        columns.extend(_column_refs(bodies[0]))
        tables.extend(
            arg.strip().strip("'").replace("''", "'")
            for arg in _split_arguments(bodies[0])
            if arg.strip() and "[" not in arg
        )
    return columns, tables


def _own_bound_kinds(text: str, compared: ColumnRef) -> list[str]:
    """Every judgement the bound's OWN text supports, before any `VAR` is followed.

    Matching `MAX(` is necessary and nowhere near sufficient (round 2 finding 1), and so is matching
    `ALL(` (round 3 finding 1). How a filter removal inside the bound changes the reading:

    * **it covers the compared column's whole table, or the whole model** - the bound is the global
      maximum on any axis -> `constant`, a pinned cutoff wearing a MAX. That reading needs the
      removal to belong to a KNOWN call: with several MAX-like calls in one bound, which of them it
      encloses is not readable here, so it is `unresolved`.
    * **it covers exactly the compared column** (`MAXX(ALL(t[c]), t[c])`) or is an `ALLEXCEPT`
      inversion - genuinely subtle DAX this gate does not model -> `unresolved`.
    * **it names something else** - NOT proof of pinning, so every MAX-like call is judged on its
      own merits. Round 3's `REMOVEFILTERS(Region)` case, and round 4's
      `REMOVEFILTERS(<the visual's own grain>)` case, which the JUDGE finishes.
    """
    scopes = _removal_scope(text, compared)
    contexts = _context_bound_kinds(text, compared)
    if REMOVAL_ALL in scopes or REMOVAL_TABLE in scopes:
        return [BOUND_CONSTANT] if len(contexts) <= 1 else [BOUND_UNRESOLVED]
    if REMOVAL_COLUMN in scopes or REMOVAL_INVERTED in scopes:
        return [BOUND_UNRESOLVED]
    return contexts


def _fold_bound_kinds(kinds: Iterable[str]) -> str:
    """THE fold for bound kinds - every site that judges a bound comes through here.

    The `VAR` loop used to `return` on the FIRST declared name it found in the bound text, so
    `MIN(_cut, _asOf)` was classified from whichever of the two happened to be declared first.
    Measured: swapping two `VAR` lines with identical semantics flipped the gate between
    `NOT_APPLICABLE` (exit 0) and `MISMATCH` (exit 1). A bound built from both a pinned and a
    moving value is genuinely ambiguous, so it is `unresolved` - never silently dropped.
    """
    distinct = set(kinds)
    if len(distinct) == 1:
        return distinct.pop()
    return BOUND_UNRESOLVED


def _read_bound(bound: str, compared: ColumnRef, variables: dict[str, str], depth: int = 0) -> BoundReading:
    """Does this upper bound MOVE with the visual's current date, or is it pinned?

    The distinction is the whole difference between a running total and an ordinary "sales through a
    cutoff" measure, whose per-bucket totals are INTENDED. Reading only the `<=` operator classified
    `'Orders'[Order_Date] <= DATE(2024, 12, 31)` as a running total and blocked it - a false positive
    on a perfectly ordinary measure, which is the one failure mode that gets a gate switched off.

    The bound's own text and every `VAR` it references are folded TOGETHER; returning the direct
    reading before looking at the variables was the second half of round 4 finding 2.
    """
    text = bound.strip()
    kinds = list(_own_bound_kinds(text, compared))
    columns, tables = _bound_removals(text)
    referenced = [body for name, body in variables.items() if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE)]
    if referenced and depth >= _VAR_DEPTH:
        # The chase was ABANDONED, which is not the same as "there was nothing to chase". Falling
        # through to the constant branch below made a 6-hop VAR chain exit 0 - measured.
        kinds.append(BOUND_UNRESOLVED)
    for body in referenced if depth < _VAR_DEPTH else []:
        inner = _read_bound(body, compared, variables, depth + 1)
        kinds.append(inner.kind)
        columns.extend(inner.removed_columns)
        tables.extend(inner.removed_tables)
    if not kinds:
        # A foreign column, a measure or a what-if parameter. It may well be an as-of date; nothing
        # here proves it either way, so the measure is carried as unassessable rather than guessed at.
        kinds.append(BOUND_UNRESOLVED if (_column_refs(text) or "[" in text) else BOUND_CONSTANT)
    return BoundReading(kind=_fold_bound_kinds(kinds), removed_columns=columns, removed_tables=tables)


def _split_top_level(text: str, operators: tuple[str, ...]) -> list[tuple[str, str]]:
    """Cut `text` at every depth-0 occurrence of one of `operators`, outside string literals.

    Returns `(fragment, operator-that-ended-it)` pairs, the last operator being "". Written as a
    scanner rather than a regex because DAX nests: `IF('T'[A] <= MAX('T'[A]), 1, 0)` contains a
    comparison that is NOT the predicate's own, and a table name may contain any of these
    characters inside its quotes.

    Parentheses wrapping the WHOLE fragment are stripped first, and that is done HERE rather than at
    the two call sites on purpose - forgetting it at one of them is round 4 finding 3, where a
    parenthesised predicate put every operator at depth 1 and silently exited 0.
    """
    text = _strip_enclosing_parens(text)
    parts: list[tuple[str, str]] = []
    depth = 0
    in_string = False
    in_name = False
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            in_string = char != '"'
            index += 1
            continue
        if in_name:
            # A doubled `''` inside a quoted table name toggles out and straight back in, which is
            # the same as skipping it - so no special case is needed.
            in_name = char != "'"
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char == "'":
            in_name = True
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth == 0:
            found = next((op for op in operators if text.startswith(op, index)), None)
            if found is not None:
                parts.append((text[start:index], found))
                index += len(found)
                start = index
                continue
        index += 1
    parts.append((text[start:], ""))
    return parts


def _top_level_comparison(text: str) -> tuple[str, str, str] | None:
    """ONE fragment's OUTERMOST comparison, as `(left, operator, right)`, or None.

    Splitting on `&&`/`||` is `_read_predicate`'s job, so this never sees a conjunction and never
    has to choose between two of them. A CHAINED comparison (`a <= b <= c`, not valid DAX) returns
    None rather than the old silent operator-dropping join, and the caller turns that into residue.

    Three things this refuses to do, each of them a measured false positive or a latent one:
    a `<>` is never read as `<`; a comparison nested inside a call, a string literal or a quoted
    table name is never read as the fragment's own; and the operator is matched longest-first.
    """
    parts = _split_top_level(text, _COMPARISON_OPERATORS)
    if len(parts) != 2:
        return None
    return parts[0][0], parts[0][1], parts[1][0]


# `<=` and `>=` are the SAME predicate with the operands swapped, so both spellings are read and
# normalised to (bounded side, bounding side). Found while auditing round 4: `MAX(t[c]) >= t[c]` is
# a valid running total that the gate dropped in silence - measured, exit 0.
_UPPER_BOUND_OPERATORS = {"<": False, "<=": False, ">": True, ">=": True}


def _read_conjunct(fragment: str) -> tuple[tuple[str, str] | None, str]:
    """One predicate conjunct as an upper bound, plus the text it could NOT account for.

    The return is deliberately a PAIR, because three outcomes have to stay distinct and the old
    code collapsed two of them into "no bound here":

    * `(bound, "")` - a recognised `<bare column> <= <expression>` upper bound.
    * `(None, "")` - recognised in full, and not an upper bound: an `=`/`<>` filter, a lower bound,
      or a fragment carrying no comparison operator at all so it cannot be hiding one.
    * `(None, <text>)` - RESIDUE. Not fully accounted for, so the call is `unassessable` and the
      measure can never come back clean on it.

    The compared side must be a BARE column reference. `'Orders'[Order_Date] >= MAX(...)` normalises
    to a bounded side of `MAX(...)`, which is not one, so it stays what it is - a lower bound.
    """
    text = _strip_enclosing_parens(fragment)
    if not text:
        return None, ""
    if not _balanced(text):
        return None, text
    parsed = _top_level_comparison(text)
    if parsed is None:
        return (None, text) if _contains_comparison(text) else (None, "")
    left, operator, right = parsed
    swapped = _UPPER_BOUND_OPERATORS.get(operator)
    if swapped is None:
        return None, ""
    compared_side, bound_side = (right, left) if swapped else (left, right)
    if not _COLUMN_REF_RE.fullmatch(compared_side.strip()):
        return None, ""
    return (compared_side, bound_side), ""


def _read_predicate(text: str) -> tuple[list[tuple[str, str]], bool, list[str]]:
    """Every upper bound in the predicate, whether it is a disjunction, and every unread fragment.

    `_read_conjunct` reads ONE fragment. This reads them ALL, because the first conjunct in an `&&`
    chain is not privileged. Measured, round 3's expression
    `'Orders'[Order_Date] >= DATE(2024,1,1) && 'Orders'[Order_Date] <= MAX('Orders'[Order_Date])`
    returned no as-of call, while **reversing the two semantically equivalent conjuncts returned
    one**. Round 4 added the third return value: a conjunct this module cannot account for used to
    be dropped here, which is how a parenthesised predicate reached exit 0.
    """
    parts = _split_top_level(text, _LOGICAL_OPERATORS)
    disjunction = any(op == "||" for _, op in parts)
    bounds: list[tuple[str, str]] = []
    residue: list[str] = []
    for fragment, _ in parts:
        bound, unread = _read_conjunct(fragment)
        if unread:
            residue.append(unread.strip())
        elif bound is not None:
            bounds.append(bound)
    return bounds, disjunction, residue


@dataclass
class PredicateReading:
    """What a `FILTER` predicate is, with "unread" kept distinct from "nothing here".

    `accumulates` False + `residue` empty is the ONLY clean acquittal: every upper bound is pinned,
    or the predicate carries none.
    """

    accumulates: bool = False
    compared: ColumnRef | None = None
    bound: BoundReading | None = None
    residue: str = ""


def _as_of_predicate(args: list[str], variables: dict[str, str]) -> PredicateReading:
    """The compared column and the KIND of upper bound in an `<col> <= <bound>` as-of predicate."""
    if len(args) != 2:
        return PredicateReading(residue=f"FILTER takes a relation and one predicate, not {len(args)} argument(s)")
    bounds, disjunction, residue = _read_predicate(args[1])
    if residue:
        return PredicateReading(residue="; ".join(residue))
    readings = [
        (compared, _read_bound(right, compared, variables))
        for compared, right in ((_column_refs(left)[0], right) for left, right in bounds)
    ]
    moving = [(compared, reading) for compared, reading in readings if reading.kind != BOUND_CONSTANT]
    if not moving:
        # Every upper bound is pinned (or there is none): an ordinary "through cutoff" measure whose
        # per-bucket totals are the point, not an accumulation.
        return PredicateReading()
    compared, reading = moving[0]
    if disjunction or len(moving) > 1:
        return PredicateReading(accumulates=True, compared=compared, bound=BoundReading(kind=BOUND_AMBIGUOUS))
    return PredicateReading(accumulates=True, compared=compared, bound=reading)


def _read_as_of_call(body: str, variables: dict[str, str]) -> AsOfCall | None:
    """Read ONE `FILTER(<relation>, ...)` body as an as-of restriction.

    `None` means ONE thing only: this `FILTER` was read in full and is not an accumulation. Every
    other outcome comes back as a call - `assessable = False` when the predicate carried text this
    module could not account for. Round 4 finding 3 was exactly the missing distinction.
    """
    args = _split_arguments(body)
    if not args:
        return None
    head = re.match(r"^([A-Za-z]+)\s*\(", args[0].strip())
    predicate = _as_of_predicate(args, variables)
    if predicate.residue:
        return AsOfCall(
            assessable=False,
            reason=f"the FILTER predicate carries text this gate does not read: {predicate.residue}",
        )
    if not predicate.accumulates or predicate.compared is None or predicate.bound is None:
        return None
    if not head or not _ALL_FUNCTION_RE.match(head.group(1)):
        # A relation that removes no filter cannot produce the "cleared anchor, surviving axis"
        # shape - but with a MOVING bound it is degenerate on EVERY axis, which is not the same
        # statement as "nothing to see here". Reported, never judged.
        return AsOfCall(
            compared=predicate.compared,
            assessable=False,
            reason=(
                f"the accumulation on {predicate.compared.qualified()} filters a relation that removes no "
                "filter, so it is evaluated inside the visual's own bucket - probe it with EVALUATE"
            ),
        )
    compared, bound = predicate.compared, predicate.bound
    call = AsOfCall(
        compared=compared,
        bound_removed_columns=bound.removed_columns,
        bound_removed_tables=bound.removed_tables,
    )
    if head.group(1).upper() == "ALLEXCEPT":
        call.assessable = False
        call.reason = "ALLEXCEPT clears every column except the ones it names, which this gate does not model"
        return call
    cleared_body = _call_bodies(args[0], head.group(1))
    cleared_text = cleared_body[0] if cleared_body else ""
    call.cleared_columns = _column_refs(cleared_text)
    call.cleared_tables = [
        arg.strip().strip("'").replace("''", "'")
        for arg in _split_arguments(cleared_text)
        if arg.strip() and "[" not in arg
    ]
    if compared.table is None:
        call.assessable = False
        call.reason = "the as-of comparison uses an unqualified column, so its table is ambiguous"
    elif bound.kind == BOUND_AMBIGUOUS:
        call.assessable = False
        call.reason = (
            f"the predicate carries more than one upper bound on {compared.qualified()}, or joins them "
            "with OR, so which one the accumulation runs to cannot be read statically"
        )
    elif bound.kind == BOUND_UNRESOLVED:
        call.assessable = False
        call.reason = (
            f"the as-of bound on {compared.qualified()} is a measure, parameter, foreign column, a "
            "context-removing expression or a mix of several, so whether it moves with the visual's "
            "current date cannot be read statically"
        )
    return call


def _classify_as_of(expr: str, base: Cumulative) -> Cumulative | None:
    """Read EVERY `FILTER(ALL(...), t[c] <= ...)` running total in the measure, independently.

    Returning after the first qualifying call judged a multi-term measure solely from its first
    term (round 2 finding 3): a term clearing both `Order_Date` and `Order Date (Month)` printed OK
    while a second term clearing only `Order_Date` degenerated to monthly totals on the same axis.
    """
    variables = _resolve_vars(expr)
    calls = [call for body in _call_bodies(expr, "FILTER") if (call := _read_as_of_call(body, variables)) is not None]
    if not calls:
        return None
    base.as_of_calls.extend(calls)
    return base


def _read_period_call(func: str, index: int, body: str) -> PeriodToDateCall:
    """Read ONE period-to-date call, recording why its anchor cannot be read rather than returning.

    A malformed call - too few arguments, or a `<dates>` slot naming several columns - used to be
    dropped, and a dropped call is indistinguishable from "this model has no period-to-date measure".
    """
    call = PeriodToDateCall(func=func)
    args = _split_arguments(body)
    if len(args) <= index:
        call.assessable = False
        call.reason = f"{func} has no argument in the <dates> position, so its grain cannot be read"
        return call
    refs = _column_refs(args[index])
    distinct = {ref.key() for ref in refs}
    if not refs or refs[0].table is None:
        call.assessable = False
        call.reason = f"{func} names no table-qualified date column, so its grain cannot be read"
        return call
    if len(distinct) > 1:
        call.assessable = False
        call.reason = f"{func} names more than one column in its <dates> argument, so its anchor is ambiguous"
        return call
    call.anchor = refs[0]
    return call


def _classify_period_to_date(expr: str, base: Cumulative) -> Cumulative | None:
    """Read EVERY `TOTALYTD`/`DATESYTD`-family call and pin down the date column each accumulates.

    This used to be excluded from assessment entirely and merely LISTED as `not_assessed_by_design`.
    That bucket hid real defects: measured on the committed Superstore model,
    `TOTALYTD(SUM('Sample Superstore'[Sales]), 'Sample Superstore'[Order Date])` on a coarser
    same-table month-start axis exited 0 - and that fact table is not even the marked date table.
    Round 2 made it judged; round 3 found it still stopped at the first call FOUND WHILE WALKING
    `_PERIOD_TO_DATE_FUNCTIONS`, so not even the first in the text. Measured:
    `TOTALYTD(SUM('Orders'[Sales]), 'Date'[Date]) + CALCULATE(SUM('Orders'[Sales]),
    DATESYTD('Orders'[Order_Date]))` on a `'Date'[Month Start]` axis exited **0** while the
    `DATESYTD` term alone exits 3. Every call is now read.
    """
    for func, index in _PERIOD_TO_DATE_FUNCTIONS.items():
        for body in _call_bodies(expr, func):
            base.period_calls.append(_read_period_call(func, index, body))
    return base if base.period_calls else None


def _shape_of(base: Cumulative) -> str:
    """The measure's mechanism(s), joined - a measure may genuinely declare more than one."""
    names = []
    if base.window_calls:
        names.append(f"{base.window_calls[0].func.lower()}_orderby")
    if base.as_of_calls:
        names.append("as_of_filter")
    if base.period_calls:
        names.append("period_to_date")
    return "+".join(names) or "unknown"


def classify(member: Any) -> Cumulative | None:
    """Read one TMDL measure as an accumulation, or return None when it declares no grain.

    Deliberately shape-driven, never name-driven: a name match would both miss the engine's
    `Highlight Max`/`% Highlight Max` (real `WINDOW(... ORDERBY(...))` measures) and fire on the
    five `Running Sum` measures in the estate that are `BLANK()` stubs with no grain at all. The one
    exception is the stub branch below, which uses the name/annotation only to SURFACE the measure.

    EVERY reader runs. The chain used to return on the first that matched, so a measure declaring
    two mechanisms was judged on one of them - measured: a correct `WINDOW(... ORDERBY(...))` beside
    a defective as-of on another date column exited **0**, where that as-of alone exits 1. That is
    the same first-match bug as rounds 1-3, at the dispatcher rather than inside a reader.
    """
    expr = strip_comments(member.expression or "")
    if not expr.strip():
        return None
    base = Cumulative(
        table=member.table,
        name=member.name,
        shape="unknown",
        tmdl=member.tmdl.as_posix(),
        line=member.line,
    )
    if is_stub_expression(member.expression or ""):
        return _classify_stub(member, base)
    found = [reader(expr, base) is not None for reader in (_classify_window, _classify_as_of, _classify_period_to_date)]
    if not any(found):
        return None
    base.shape = _shape_of(base)
    return base


def _classify_stub(member: Any, base: Cumulative) -> Cumulative | None:
    """Surface a `BLANK()` stub only when the evidence says it WAS a running total."""
    haystack = " ".join(
        [
            member.name or "",
            member.annotations.get("TableauFormula", ""),
            member.annotations.get("TranslationStubReason", ""),
        ]
    )
    if not _RUNNING_STUB_RE.search(haystack):
        return None
    base.shape = "stub"
    base.assessable = False
    base.reason = "the measure is a BLANK() stub, so it has no grain yet (check_stub_measures.py owns it)"
    return base
