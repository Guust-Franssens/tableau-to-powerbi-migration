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

THE RECOGNISED GRAMMAR: what the ENGINE emits, and nothing else
---------------------------------------------------------------
Rounds 1-5 of blind review produced 5-6-2-0-3-4 findings, and by round 5 the gate was wrong in BOTH
directions - it fired MISMATCH on a correct TEXT measure whose *string literal* contained DAX, and
it returned OK on a genuinely broken running total because a `REMOVEFILTERS` in an UNREACHABLE `IF`
branch was unioned into the acquittal. Every one of those findings was the same mistake: a regex
scanning raw DAX text and mis-reading its syntax. Semantic analysis of arbitrary DAX with regexes
does not terminate, so round 5 stopped trying.

**The recognised set is the engine's closed set.** Read out of the deterministic tier
(`skills/tableau-migration/scripts/calc_to_dax.py:3548`), every cumulative measure it emits is one
family, built on the window functions:

    RUNNING_SUM/AVG/MIN/MAX/COUNT(<agg>) -> <X>(WINDOW(1, ABS,  0, REL, <spec>), CALCULATE(<agg>))
    WINDOW_SUM/AVG/MIN/MAX/COUNT(<agg>)  -> <X>(WINDOW(1, ABS, -1, ABS, <spec>), CALCULATE(<agg>))
    SIZE()  -> COUNTROWS(WINDOW(1, ABS, -1, ABS, <spec>))     INDEX() -> ROWNUMBER(<spec>)

It emits **no** as-of `FILTER(ALL(t[c]), t[c] <= MAX(t[c]))` and **no** `TOTALYTD`/`DATESYTD`; its
only `FILTER(ALL(...))` (`calc_to_dax.py:2328`) is a cross-table FIXED LOD whose predicate is an
EQUALITY conjunction, never an ordering bound. Measured across the 16 committed `examples/` models,
526 measures: **114** `FILTER(ALL(...))` measures and **0** of them carrying any ordering
comparison. So the as-of classifier that produced round 4's F1/F2/F3 and round 5's F1/F2 never had
a single instance to classify in shipped bytes.

Three rules, in the order they run:

1. **LEX FIRST.** `mask_noncode` blanks string literals and `--`/`//`/`/* */` comments before any
   regex sees the text, preserving every offset. Quoted table names and bracketed column names are
   skipped ATOMICALLY, so a `"` inside a column name cannot mis-lex. Round 5 finding 3 is
   unfixable otherwise, and it is not hypothetical: the single `TOTALYTD` in `examples/` is inside
   a `///` comment (`superstore-sales-performance/.../Date.tmdl:6`).
2. **STRUCTURAL READS ONLY.** Both judged mechanisms answer "which columns does this call name?",
   never "what does this expression mean". Window: read `ORDERBY`'s columns, require the visual to
   project them. Period-to-date: read the `<dates>` column, compare grains using MODEL facts
   (declared type / calculated lineage), not DAX semantics.
3. **EVERYTHING ELSE IS `UNASSESSABLE`.** The as-of family is DETECTED, never classified: a
   `FILTER(...)` whose predicate carries an ordering comparison can only ever produce exit 3 with
   "probe it with EVALUATE". It cannot emit a mismatch, so it cannot be wrong in the direction that
   gets a gate switched off; it cannot emit `ok`, so it cannot grant false confidence.

What this deliberately gives up: a coarse-axis MISMATCH for a hand-authored as-of running total.
That capability was never exercised on a real artifact and was wrong in both directions.
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

# The as-of DETECTOR, and the whole of it. `FILTER(` plus an ordering comparison somewhere in the
# call is the shape of an accumulation restricted by a moving cutoff. It is deliberately shallow:
# it names a measure the gate REFUSES to judge, so over-detection costs an exit 3 and nothing else.
# `<>` is excluded because it is DAX's not-equal, not an ordering operator (round 2 finding 2).
_FILTER_CALL_RE = re.compile(r"\bFILTER\s*\(", re.IGNORECASE)
_ORDERING_COMPARISON_RE = re.compile(r"<=|>=|<(?![>=])|(?<!<)>(?!=)")

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
    """ONE `FILTER(...)` restricted by an ordering comparison - DETECTED, never classified.

    Round 5 retired the classifier this used to carry. Deciding whether an as-of bound *moves with
    the visual* means answering, from raw text, which `IF` branch is reachable, which operand a
    `REMOVEFILTERS` belongs to, and what a redundant paren around a column means. Rounds 4 and 5
    measured five separate ways that went wrong, in BOTH directions:
    `IF(TRUE(), MAX(d), CALCULATE(MAX(d), REMOVEFILTERS(<the axis>)))` returned **OK** on a broken
    measure because the unreachable branch's removal was unioned into the acquittal, while
    `('Orders'[Order_Date]) <= MAX(...)` - one redundant paren - exited **0**.

    The engine emits no measure of this shape at all (`calc_to_dax.py:2328` is a FIXED LOD with an
    EQUALITY predicate), and 0 of the 526 measures in the committed `examples/` corpus carry an
    ordering comparison inside a `FILTER`. So there was never anything here to classify, and the
    honest report is `unassessable` with the measure named.

    `assessable` is a field rather than a constant only so the judge reads every mechanism the same
    way; nothing ever sets it True.
    """

    predicate: str = ""
    assessable: bool = False
    reason: str = ""


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
        """The period-to-date anchor, for presentation and routing only.

        Deliberately not a verdict input: every judgement is formed per call, from the lists above.
        """
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
            # Masked for the same reason `classify` masks: a calculated column's own string literal
            # (`Bucket = IF(x, "[Date]", "other")`) would otherwise contribute a phantom lineage ref.
            facts.calc_expressions[(member.table.casefold(), member.name.casefold())] = mask_noncode(member.expression)
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


def mask_noncode(text: str) -> str:
    """Blank every string literal and comment, preserving length so all offsets still line up.

    **This runs before any regex sees DAX, and round 5 finding 3 is unfixable otherwise.** Measured:
    a legitimate TEXT measure whose literal contained
    `"FILTER(ALL('Orders'[Order_Date]), 'Orders'[Order_Date] <= MAX('Orders'[Order_Date]))"`, bound
    as a tooltip, was classified a running total and reported **MISMATCH, exit 1** - the gate firing
    on correct DAX, which is the failure mode that gets a gate switched off. `_call_bodies` cannot
    fix this itself: it regex-matches a function name over raw text and only then starts tracking
    quotes, so the lexical context is already lost by the time it looks.

    Not hypothetical, and not only about measures: the single `TOTALYTD` anywhere in the committed
    `examples/` corpus is inside a `///` documentation comment
    (`superstore-sales-performance/.../Date.tmdl:6`).

    Quoted table names and bracketed column names are skipped ATOMICALLY rather than masked, for
    two different reasons: their contents must stay readable (they carry the column references this
    module exists to read), and a `"` inside a column name - `'T'[He said "hi"]` is a legal DAX
    identifier - would otherwise open a phantom string literal and mask the rest of the expression.
    Verified: that expression round-trips through `mask_noncode` unchanged.
    """
    out = list(text)
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "'":  # a quoted table name is atomic - it may legally contain " and --
            end = text.find("'", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char == "[":  # a bracketed column name is atomic, for the same reason
            end = text.find("]", index + 1)
            index = length if end < 0 else end + 1
            continue
        if char == '"':
            index = _mask_string(text, out, index)
            continue
        if text.startswith("--", index) or text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end < 0 else end
            _blank(out, index, end)
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            _blank(out, index, end)
            index = end
            continue
        index += 1
    return "".join(out)


def _blank(out: list[str], start: int, end: int) -> None:
    """Overwrite a span with spaces, keeping every later offset where it was."""
    for position in range(start, end):
        out[position] = " "


def _mask_string(text: str, out: list[str], start: int) -> int:
    """Blank one `"..."` literal, honouring DAX's doubled-quote escape, and return the next index."""
    index = start + 1
    length = len(text)
    while index < length:
        if text[index] == '"':
            if index + 1 < length and text[index + 1] == '"':
                index += 2
                continue
            break
        index += 1
    end = min(index + 1, length)
    _blank(out, start, end)
    return end


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


def _detect_as_of(expr: str) -> list[AsOfCall]:
    """DETECT `FILTER(...)` restricted by an ordering comparison. Never classify it.

    This replaces ~450 lines of bound classification - `_read_bound`, `_own_bound_kinds`,
    `_context_bound_kinds`, `_bound_removals`, `_removal_scope`, `_fold_bound_kinds`,
    `_read_conjunct`, `_read_predicate`, `_as_of_predicate`, `_split_top_level`,
    `_top_level_comparison`, `_strip_enclosing_parens`, `_contains_comparison`, `_resolve_vars` -
    all deleted in round 5, with the `AsOfCall.pins` acquittal and
    `check_running_total_axis._judge_same_table_survivors` that consumed them.

    Why the whole thing rather than a sixth patch. Deciding whether an as-of bound MOVES WITH THE
    VISUAL requires answering, from raw text, which `IF` branch is reachable, which operand a
    `REMOVEFILTERS` belongs to, and whether a redundant paren around a column changes its meaning.
    Measured, rounds 4 and 5, all by exit code:

    | spelling                                                          | before |
    |-------------------------------------------------------------------|--------|
    | `('Orders'[Order_Date]) <= MAX(...)` - one redundant paren         | 0      |
    | `(d <= MAX(d)) = TRUE()`                                           | 0      |
    | `IF(TRUE(), MAX(d), CALCULATE(MAX(d), REMOVEFILTERS(<the axis>)))` | 0 / OK |
    | `MIN(MAX(d), DATE(...))` inline vs the same via two `VAR`s         | 1 vs 3 |

    The third is the worst: a genuinely broken running total ACQUITTED, because a removal in an
    unreachable branch was unioned into the acquittal. A gate wrong in both directions is worse
    than no gate.

    And there was never anything to classify. The engine emits no measure of this shape - its only
    `FILTER(ALL(...))` (`calc_to_dax.py:2328`) is a cross-table FIXED LOD with an EQUALITY
    predicate - and **0 of the 526 measures in the 16 committed `examples/` models** carry an
    ordering comparison inside a `FILTER`. So this detector's measured noise on shipped bytes is
    zero, and every verdict it can produce is exit 3 with the measure named.

    Deliberately shallow, because shallow is what makes it safe: `FILTER(` plus an ordering
    operator anywhere in the call. Over-detection costs an `unassessable`; it can never invent a
    mismatch, and it can never return `ok`.
    """
    calls: list[AsOfCall] = []
    for body in _call_bodies(expr, "FILTER"):
        if _ORDERING_COMPARISON_RE.search(body):
            calls.append(
                AsOfCall(
                    predicate=" ".join(body.split())[:160],
                    reason=(
                        "the measure restricts a FILTER with an ordering comparison - an as-of "
                        "accumulation. This gate reads the ENGINE's window-function shapes; it does "
                        "not judge a hand-authored as-of bound, because whether that bound moves "
                        "with the visual is not decidable from the DAX text. Probe it with EVALUATE "
                        "at every axis grain the emitted visuals bind"
                    ),
                )
            )
    return calls


def _classify_as_of(expr: str, base: Cumulative) -> Cumulative | None:
    """Attach every detected as-of restriction, or None when the measure carries none."""
    base.as_of_calls.extend(_detect_as_of(expr))
    return base if base.as_of_calls else None


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

    **`mask_noncode` runs here, ONCE, and every reader below sees only masked text.** That is the
    round-5 finding-3 fix and the reason it is at the entry point rather than inside each reader: a
    reader that forgets it re-opens the hole, and a TEXT measure whose literal contains DAX was
    measured reporting MISMATCH / exit 1.

    EVERY reader runs. The chain used to return on the first that matched, so a measure declaring
    two mechanisms was judged on one of them - measured: a correct `WINDOW(... ORDERBY(...))` beside
    a defective as-of on another date column exited **0**, where that as-of alone exits 1.
    """
    expr = mask_noncode(strip_comments(member.expression or ""))
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
