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
_CONTEXT_BOUND_RE = re.compile(
    r"\b(MAX|MAXX|LASTDATE|LASTNONBLANK|ENDOFMONTH|ENDOFQUARTER|ENDOFYEAR|SELECTEDVALUE)\s*\(",
    re.IGNORECASE,
)
_VAR_RE = re.compile(
    r"\bVAR\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<body>.*?)(?=\bVAR\b|\bRETURN\b)", re.IGNORECASE | re.DOTALL
)

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
class Cumulative:  # pylint: disable=too-many-instance-attributes
    """One measure whose DAX declares an accumulation grain, plus how confidently we read it.

    Wide on purpose: the two mechanisms address a grain in structurally different ways (an ORDERBY
    list versus a cleared-column set plus a compared column), and collapsing them into a shared
    field would force every reader to remember which shape reused which slot.
    """

    table: str
    name: str
    shape: str
    tmdl: str
    line: int
    ordered_by: list[ColumnRef] = field(default_factory=list)
    partition_by: list[ColumnRef] = field(default_factory=list)
    cleared_columns: list[ColumnRef] = field(default_factory=list)
    cleared_tables: list[str] = field(default_factory=list)
    compared: ColumnRef | None = None
    assessable: bool = True
    reason: str = ""

    @property
    def label(self) -> str:
        """`'Table'[Measure]`, the spelling that can be pasted into a DAX query."""
        return f"'{self.table}'[{self.name}]"


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


def _classify_window(expr: str, base: Cumulative) -> Cumulative | None:
    """Read EVERY window-family call, or record why the grain of one of them cannot be read."""
    sites = _window_call_sites(expr)
    if not sites:
        return None
    base.shape = f"{sites[0][0].lower()}_orderby"
    saw_orderby = False
    for func, args in sites:
        named = [a for a in args if any(re.match(rf"^{c}\s*\(", a.strip(), re.IGNORECASE) for c in _CLAUSE_FUNCTIONS)]
        positional = [a for a in args if a not in named]
        if any(not _is_positional(arg) for arg in positional):
            base.assessable = False
            base.reason = (
                f"{func} carries an explicit relation argument, so the ordering domain is that "
                "table expression rather than the visual"
            )
            return base
        order_body = _clause(args, "ORDERBY")
        if order_body is None:
            continue
        saw_orderby = True
        ordered = _column_refs(order_body)
        if not ordered:
            base.assessable = False
            base.reason = f"{func} ORDERBY names no resolvable column reference"
            return base
        if any(ref.table is None for ref in ordered):
            base.assessable = False
            base.reason = f"{func} ORDERBY uses an unqualified column, so its table is ambiguous"
            return base
        for ref in ordered:
            if ref not in base.ordered_by:
                base.ordered_by.append(ref)
        partition_body = _clause(args, "PARTITIONBY")
        for ref in _column_refs(partition_body) if partition_body else []:
            if ref not in base.partition_by:
                base.partition_by.append(ref)
    if not saw_orderby:
        # No ORDERBY anywhere means every window orders by the relation's own columns, and the
        # relation IS the visual. There is no second grain that could disagree - a verified
        # acquittal, not a guess.
        base.reason = f"{sites[0][0]} has no ORDERBY clause, so it orders by the visual's own grain"
    return base


BOUND_CONTEXT = "context"
BOUND_CONSTANT = "constant"
BOUND_UNRESOLVED = "unresolved"


def _resolve_vars(expr: str) -> dict[str, str]:
    """`VAR <name> = <body>` pairs declared in one measure, so a hoisted as-of date can be followed."""
    return {match.group("name").casefold(): match.group("body").strip() for match in _VAR_RE.finditer(expr)}


def _classify_bound(bound: str, compared: ColumnRef, variables: dict[str, str], depth: int = 0) -> str:
    """Does this upper bound MOVE with the visual's current date, or is it pinned?

    The distinction is the whole difference between a running total and an ordinary "sales through a
    cutoff" measure, whose per-bucket totals are INTENDED. Reading only the `<=` operator classified
    `'Orders'[Order_Date] <= DATE(2024, 12, 31)` as a running total and blocked it - a false positive
    on a perfectly ordinary measure, which is the one failure mode that gets a gate switched off.
    """
    text = bound.strip()
    if _CONTEXT_BOUND_RE.search(text) and _column_refs(text):
        return BOUND_CONTEXT
    if depth < 4:
        for name, body in variables.items():
            if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
                return _classify_bound(body, compared, variables, depth + 1)
    if _column_refs(text) or "[" in text:
        # A foreign column, a measure or a what-if parameter. It may well be an as-of date; nothing
        # here proves it either way, so the measure is carried as unassessable rather than guessed at.
        return BOUND_UNRESOLVED
    return BOUND_CONSTANT


def _as_of_predicate(args: list[str], variables: dict[str, str]) -> tuple[ColumnRef, str] | None:
    """The compared column and the KIND of upper bound in an `<col> <= <bound>` as-of predicate."""
    if len(args) < 2:
        return None
    match = re.search(r"(.+?)(<=|<)(?!=)(.*)", args[1], re.DOTALL)
    if not match:
        return None
    left = _column_refs(match.group(1))
    if not left:
        return None
    compared = left[-1]
    return compared, _classify_bound(match.group(3), compared, variables)


def _classify_as_of(expr: str, base: Cumulative) -> Cumulative | None:
    """Read a `FILTER(ALL(...), t[c] <= ...)` running total, or record why it cannot be read."""
    variables = _resolve_vars(expr)
    for body in _call_bodies(expr, "FILTER"):
        args = _split_arguments(body)
        if not args:
            continue
        head = re.match(r"^([A-Za-z]+)\s*\(", args[0].strip())
        if not head or not _ALL_FUNCTION_RE.match(head.group(1)):
            continue
        predicate = _as_of_predicate(args, variables)
        if predicate is None:
            continue
        compared, bound = predicate
        if bound == BOUND_CONSTANT:
            # A pinned cutoff is NOT an accumulation - its per-bucket totals are the point. Keep
            # looking: a later FILTER in the same measure may still be a real as-of.
            continue
        base.shape = "as_of_filter"
        base.compared = compared
        if head.group(1).upper() == "ALLEXCEPT":
            base.assessable = False
            base.reason = "ALLEXCEPT clears every column except the ones it names, which this gate does not model"
            return base
        cleared_body = _call_bodies(args[0], head.group(1))
        cleared_text = cleared_body[0] if cleared_body else ""
        base.cleared_columns = _column_refs(cleared_text)
        base.cleared_tables = [
            arg.strip().strip("'").replace("''", "'")
            for arg in _split_arguments(cleared_text)
            if arg.strip() and "[" not in arg
        ]
        if compared.table is None:
            base.assessable = False
            base.reason = "the as-of comparison uses an unqualified column, so its table is ambiguous"
        elif bound == BOUND_UNRESOLVED:
            base.assessable = False
            base.reason = (
                f"the as-of bound on {compared.qualified()} is a measure, parameter or foreign column, "
                "so whether it moves with the visual's current date cannot be read statically"
            )
        return base
    return None


def _classify_period_to_date(expr: str, base: Cumulative) -> Cumulative | None:
    """Read a `TOTALYTD`/`DATESYTD` family call and pin down the date column it accumulates over.

    This used to be excluded from assessment entirely and merely LISTED as `not_assessed_by_design`.
    That bucket hid real defects: measured on the committed Superstore model,
    `TOTALYTD(SUM('Sample Superstore'[Sales]), 'Sample Superstore'[Order Date])` on a coarser
    same-table month-start axis exited 0 - and that fact table is not even the marked date table
    (the relationship runs from `'Sample Superstore'[Order Date 2017]` to `Date[Date]`). It is now
    judged, and anything short of a proof of safety is `unassessable`, never a pass.
    """
    for func, index in _PERIOD_TO_DATE_FUNCTIONS.items():
        for body in _call_bodies(expr, func):
            args = _split_arguments(body)
            if len(args) <= index:
                continue
            base.shape = "period_to_date"
            refs = _column_refs(args[index])
            if not refs or refs[0].table is None:
                base.assessable = False
                base.reason = f"{func} names no table-qualified date column, so its grain cannot be read"
                return base
            base.compared = refs[0]
            return base
    return None


def classify(member: Any) -> Cumulative | None:
    """Read one TMDL measure as an accumulation, or return None when it declares no grain.

    Deliberately shape-driven, never name-driven: a name match would both miss the engine's
    `Highlight Max`/`% Highlight Max` (real `WINDOW(... ORDERBY(...))` measures) and fire on the
    five `Running Sum` measures in the estate that are `BLANK()` stubs with no grain at all. The one
    exception is the stub branch below, which uses the name/annotation only to SURFACE the measure.
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
    for reader in (_classify_window, _classify_as_of, _classify_period_to_date):
        found = reader(expr, base)
        if found is not None:
            return found
    return None


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
