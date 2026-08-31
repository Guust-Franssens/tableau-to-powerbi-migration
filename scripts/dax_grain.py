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

_TIME_INTELLIGENCE_RE = re.compile(
    r"\b(DATESYTD|DATESMTD|DATESQTD|TOTALYTD|TOTALMTD|TOTALQTD)\s*\(",
    re.IGNORECASE,
)
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
# Matching one of these is NECESSARY and not sufficient - see `_classify_moving_bound`.
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
    """

    compared: ColumnRef
    cleared_columns: list[ColumnRef] = field(default_factory=list)
    cleared_tables: list[str] = field(default_factory=list)
    assessable: bool = True
    reason: str = ""


@dataclass
class Cumulative:  # pylint: disable=too-many-instance-attributes
    """One measure whose DAX declares one or more accumulation grains, and how confidently we read
    each of them.

    THE RULE, arrived at the hard way over three review rounds: **a classifier never stops at the
    first match.** Every site that did - the first window call, the first qualifying `FILTER`, the
    first period-to-date function, the first reader in the dispatch chain, the first `VAR` a bound
    references, the first comparison in an `&&` chain, the first `ALL(...)` in a bound - produced
    the same class of bug, and three of them were SILENT PASSES rather than merely wrong verdicts.
    So each mechanism is a LIST of independent calls, and `check_running_total_axis._worst` picks
    the worst verdict across all of them.
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


def _clause(args: Iterable[str], name: str) -> str | None:
    """The body of the first `name(...)` clause among a window function's arguments."""
    prefix = re.compile(rf"^{name}\s*\(", re.IGNORECASE)
    for arg in args:
        if prefix.match(arg.strip()):
            bodies = _call_bodies(arg, name)
            if bodies:
                return bodies[0]
    return None


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
    # DAX permits at most ONE `ORDERBY`/`PARTITIONBY` per window call, so `_clause`'s first match is
    # the only match - audited, not assumed. Every other first-match site in this module is a list.
    order_body = _clause(args, "ORDERBY")
    if order_body is None:
        # No ORDERBY means this call orders by the relation's own columns, and the relation IS the
        # visual. There is no second grain that could disagree - a verified acquittal, not a guess.
        call.reason = f"{func} has no ORDERBY clause, so it orders by the visual's own grain"
        return call
    ordered = _column_refs(order_body)
    if not ordered:
        call.assessable = False
        call.reason = f"{func} ORDERBY names no resolvable column reference"
        return call
    if any(ref.table is None for ref in ordered):
        call.assessable = False
        call.reason = f"{func} ORDERBY uses an unqualified column, so its table is ambiguous"
        return call
    call.ordered_by = ordered
    partition_body = _clause(args, "PARTITIONBY")
    call.partition_by = _column_refs(partition_body) if partition_body else []
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


def _classify_moving_bound(text: str, compared: ColumnRef) -> str:
    """Does a MAX-like bound PROVE it reads the compared column under the visual's filter context?

    Matching `MAX(` is necessary and nowhere near sufficient (round 2 finding 1), and so is
    matching `ALL(` (round 3 finding 1). Three ways a MAX-like bound fails to move with the visual,
    and one way it looks like it fails but does not:

    * **the removal covers the compared column's whole table, or the whole model** - the bound is
      the global maximum, full stop, on any axis -> `constant`, i.e. a pinned cutoff wearing a MAX.
    * **the removal covers exactly the compared column** - `MAXX(ALL(t[c]), t[c])`. Whether other
      filters on `t` still restrict it is a genuinely subtle DAX question this gate does not model
      -> `unresolved`.
    * **`ALLEXCEPT`** - an inverted set -> `unresolved`.
    * **the removal names something else entirely** - it is NOT proof of pinning, so the bound is
      judged on its own merits below. That is the reviewer's `REMOVEFILTERS(Region)` case.

    A foreign column - `MAX('Date'[Date])` bounding `'Orders'[Order_Date]` - may be an as-of date
    reached through a relationship, and may equally be something else -> `unresolved`.
    """
    scopes = _removal_scope(text, compared)
    if REMOVAL_ALL in scopes or REMOVAL_TABLE in scopes:
        return BOUND_CONSTANT
    if REMOVAL_COLUMN in scopes or REMOVAL_INVERTED in scopes:
        return BOUND_UNRESOLVED
    for match in _CONTEXT_BOUND_RE.finditer(text):
        bodies = _call_bodies(text[match.start() :], match.group(1))
        if bodies and any(_same_column(ref, compared) for ref in _column_refs(bodies[0])):
            return BOUND_CONTEXT
    return BOUND_UNRESOLVED


def _combine_bound_kinds(kinds: set[str]) -> str:
    """Fold the kinds of every `VAR` a bound references into one answer.

    The `VAR` loop used to `return` on the FIRST declared name it found in the bound text, so
    `MIN(_cut, _asOf)` was classified from whichever of the two happened to be declared first.
    Measured: swapping two `VAR` lines with identical semantics flipped the gate between
    `NOT_APPLICABLE` (exit 0) and `MISMATCH` (exit 1). A bound built from both a pinned and a
    moving value is genuinely ambiguous, so it is `unresolved` - never silently dropped.
    """
    if len(kinds) == 1:
        return kinds.pop()
    return BOUND_UNRESOLVED


def _classify_bound(bound: str, compared: ColumnRef, variables: dict[str, str], depth: int = 0) -> str:
    """Does this upper bound MOVE with the visual's current date, or is it pinned?

    The distinction is the whole difference between a running total and an ordinary "sales through a
    cutoff" measure, whose per-bucket totals are INTENDED. Reading only the `<=` operator classified
    `'Orders'[Order_Date] <= DATE(2024, 12, 31)` as a running total and blocked it - a false positive
    on a perfectly ordinary measure, which is the one failure mode that gets a gate switched off.
    Reading only `MAX(` did the same thing to a foreign-date bound; reading only `ALL(` did it to a
    bound that removes an unrelated filter. See `_classify_moving_bound`.
    """
    text = bound.strip()
    if _CONTEXT_BOUND_RE.search(text):
        return _classify_moving_bound(text, compared)
    referenced = [body for name, body in variables.items() if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE)]
    if referenced and depth < _VAR_DEPTH:
        return _combine_bound_kinds({_classify_bound(body, compared, variables, depth + 1) for body in referenced})
    if _column_refs(text) or "[" in text:
        # A foreign column, a measure or a what-if parameter. It may well be an as-of date; nothing
        # here proves it either way, so the measure is carried as unassessable rather than guessed at.
        return BOUND_UNRESOLVED
    return BOUND_CONSTANT


def _split_top_level(text: str, operators: tuple[str, ...]) -> list[tuple[str, str]]:
    """Cut `text` at every depth-0 occurrence of one of `operators`, outside string literals.

    Returns `(fragment, operator-that-ended-it)` pairs, the last operator being "". Written as a
    scanner rather than a regex because DAX nests: `IF('T'[A] <= MAX('T'[A]), 1, 0)` contains a
    comparison that is NOT the predicate's own, and a table name may contain any of these
    characters inside its quotes.
    """
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

    Splitting on `&&`/`||` is `_upper_bound_comparisons`' job, so this never sees a conjunction and
    never has to choose between two of them.

    Three things this refuses to do, each of them a measured false positive or a latent one:
    a `<>` is never read as `<`; a comparison nested inside a call, a string literal or a quoted
    table name is never read as the fragment's own; and the operator is matched longest-first.
    """
    parts = _split_top_level(text, _COMPARISON_OPERATORS)
    if len(parts) < 2:
        return None
    return parts[0][0], parts[0][1], "".join(part for part, _ in parts[1:])


def _upper_bound_comparisons(text: str) -> tuple[list[tuple[str, str]], bool]:
    """Every top-level `<`/`<=` comparison in the predicate, and whether it is a disjunction.

    `_top_level_comparison` reads ONE fragment. This reads them ALL, because the first conjunct in
    an `&&` chain is not privileged. Measured, the reviewer's expression:

        'Orders'[Order_Date] >= DATE(2024,1,1) && 'Orders'[Order_Date] <= MAX('Orders'[Order_Date])

    returned no as-of call, while **reversing the two semantically equivalent conjuncts returned
    one**. So a running total from a fixed start date was invisible - and it still suffers the
    uncleared coarse-axis defect. A `>=` lower bound is not an upper bound and is simply not
    collected; what matters is that its POSITION no longer decides anything.
    """
    parts = _split_top_level(text, _LOGICAL_OPERATORS)
    disjunction = any(op == "||" for _, op in parts)
    bounds: list[tuple[str, str]] = []
    for fragment, _ in parts:
        parsed = _top_level_comparison(fragment)
        if parsed is not None and parsed[1] in ("<", "<="):
            bounds.append((parsed[0], parsed[2]))
    return bounds, disjunction


def _as_of_predicate(args: list[str], variables: dict[str, str]) -> tuple[ColumnRef, str] | None:
    """The compared column and the KIND of upper bound in an `<col> <= <bound>` as-of predicate.

    Returns None only when NO upper bound in the predicate could be an accumulation - every one of
    them pinned, or none present at all. Anything conflicting or unreadable comes back as a bound
    kind that makes the call `unassessable`; silently dropping it is what round 3 finding 2 was.
    """
    if len(args) < 2:
        return None
    bounds, disjunction = _upper_bound_comparisons(args[1])
    candidates: list[tuple[ColumnRef, str]] = []
    for left, right in bounds:
        refs = _column_refs(left)
        if not refs:
            continue
        compared = refs[-1]
        candidates.append((compared, _classify_bound(right, compared, variables)))
    moving = [candidate for candidate in candidates if candidate[1] != BOUND_CONSTANT]
    if not moving:
        # Every upper bound is pinned (or there is none): an ordinary "through cutoff" measure whose
        # per-bucket totals are the point, not an accumulation.
        return None
    if disjunction:
        return moving[0][0], BOUND_AMBIGUOUS
    if len(moving) > 1:
        return moving[0][0], BOUND_AMBIGUOUS
    return moving[0]


def _read_as_of_call(body: str, variables: dict[str, str]) -> AsOfCall | None:
    """Read ONE `FILTER(ALL(...), ...)` body as an as-of restriction, or None when it is not one."""
    args = _split_arguments(body)
    if not args:
        return None
    head = re.match(r"^([A-Za-z]+)\s*\(", args[0].strip())
    if not head or not _ALL_FUNCTION_RE.match(head.group(1)):
        return None
    predicate = _as_of_predicate(args, variables)
    if predicate is None:
        return None
    compared, bound = predicate
    if bound == BOUND_CONSTANT:
        # A pinned cutoff is NOT an accumulation - its per-bucket totals are the point.
        return None
    call = AsOfCall(compared=compared)
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
    elif bound == BOUND_AMBIGUOUS:
        call.assessable = False
        call.reason = (
            f"the predicate carries more than one upper bound on {compared.qualified()}, or joins them "
            "with OR, so which one the accumulation runs to cannot be read statically"
        )
    elif bound == BOUND_UNRESOLVED:
        call.assessable = False
        call.reason = (
            f"the as-of bound on {compared.qualified()} is a measure, parameter, foreign column or a "
            "context-removing expression, so whether it moves with the visual's current date cannot "
            "be read statically"
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


def _read_period_call(func: str, index: int, body: str) -> PeriodToDateCall | None:
    """Read ONE period-to-date call, recording why its anchor cannot be read rather than returning."""
    args = _split_arguments(body)
    if len(args) <= index:
        return None
    call = PeriodToDateCall(func=func)
    refs = _column_refs(args[index])
    if not refs or refs[0].table is None:
        call.assessable = False
        call.reason = f"{func} names no table-qualified date column, so its grain cannot be read"
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
            call = _read_period_call(func, index, body)
            if call is not None:
                base.period_calls.append(call)
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


def period_to_date_measures(measures: list[Any]) -> list[str]:
    """Period-to-date measures in a model, by name. Diagnostic only - they are JUDGED, not excused.

    Kept as a public helper because "how many of these does this model have?" is a useful question,
    but nothing in the verdict path calls it any more. It used to feed a `not_assessed_by_design`
    bucket, and that bucket was where a real defect hid.
    """
    return sorted(
        f"'{m.table}'[{m.name}]" for m in measures if _TIME_INTELLIGENCE_RE.search(strip_comments(m.expression or ""))
    )
