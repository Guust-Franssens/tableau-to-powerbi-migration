"""
purpose: read what a DAX measure ADDRESSES and what grain a model column IS - the model-side half
         of the running-total axis gate, with no knowledge of PBIR, reports or verdicts.
usage:   import dax_grain; dax_grain.classify(member); dax_grain.read_model_facts(model_dir)

internal-reason: library split out of `scripts/check_running_total_axis.py` (#218) when that module
crossed pylint's `max-module-lines` cap. The seam is real rather than arithmetic: everything here
answers "what does this DAX address, and what is this column?" from TMDL alone, and nothing here
can see a report.

THE RECOGNISED GRAMMAR: whole ENGINE templates, fully consumed, or nothing
--------------------------------------------------------------------------
Rounds 1-6 of blind review produced 5-6-2-0-3-4 findings and EVERY one of them was a regex reading
raw DAX text. Round 5 decided to narrow the grammar; round 6 measured that the decision had not
been executed - the gate still searched for call sites in text, and still returned definite
verdicts on input it had never consumed. Measured on the shipped code, all by exit code:

    | input                                                         | before | why it was wrong       |
    |---------------------------------------------------------------|--------|------------------------|
    | engine measure over a table named `'Orders--Archive'`          | 0 N/A  | truncated at `--`      |
    | `SUM('WINDOW()'[Sales])`                                       | 0 OK   | matched inside a NAME  |
    | `SUM('Orders'[WINDOW(1, ABS, 0, REL, ORDERBY(...))])`          | 1 MISM | matched inside a NAME  |
    | `VAR _dead = <a WINDOW> VAR _answer = SUM(...) RETURN _answer` | 1 MISM | the window is UNUSED   |
    | `ORDERBY(YEAR('Orders'[Order_Date]), ASC)`                     | 0 OK   | the YEAR() is unread   |
    | `SUMX(OFFSET(-1), CALCULATE(...))` on a measure-only visual    | 0 OK   | no grain to judge      |

So the reading layer is now a LEXER plus WHOLE-EXPRESSION TEMPLATES (`dax_tokens`), and this module
recognises exactly the closed set the deterministic tier emits - read out of
`skills/tableau-migration/scripts/calc_to_dax.py` (`_emit_table_calc` ~3466, `_orderby_clause`
~3236, `_partitionby_clause` ~3341, `translate_difference_to_dax` ~3738,
`translate_percent_difference_to_dax` ~3686), whose window calls deliberately omit `<relation>`
(`calc_to_dax.py:279`) so that the visual's own grain is the contract.

Three rules, in the order they run:

1. **LEX ONCE.** `dax_tokens.tokenize` is total, and quoted table names, bracketed column names and
   string literals are ATOMIC tokens. A `--` or a `WINDOW(` inside an identifier is identifier text
   and can never be seen as code. There is no second, identifier-unaware pass to get wrong.
2. **MATCH A WHOLE TEMPLATE, or do not judge.** A match is anchored at token 0 and must end on the
   last token, with every token either a template literal or inside a declared hole.
   `Cumulative.consumed` records that receipt, and
   `check_running_total_axis._enforce_consumption` refuses any `ok`/`mismatch` without it - so "no
   definite verdict on unconsumed input" is a property of the code, not a claim about coverage.
3. **EVERYTHING ELSE IS `UNASSESSABLE`.** An expression that carries an accumulation SIGNAL - a
   called window function, a called period-to-date function, or a `FILTER(...)` restricted by an
   ordering comparison - but matches no template is reported unassessable with the measure named.
   An expression with no signal at all is not an accumulation and is not reported.

What this deliberately gives up: any verdict on a hand-authored accumulation. The as-of family
(`FILTER(ALL(t[c]), t[c] <= MAX(t[c]))`) is DETECTED, never classified - rounds 4 and 5 measured
five spellings where classifying it was wrong in BOTH directions, and the engine emits none of it
(its only `FILTER(ALL(...))`, `calc_to_dax.py:2328`, is a FIXED LOD with an EQUALITY predicate; 0 of
the 526 measures in the 16 committed `examples/` models carry an ordering comparison inside a
`FILTER`).

The one idea worth carrying across from earlier rounds: a grain is decided from a PROPERTY, never a
proxy. The engine writes its coarse date bins as calculated TEXT columns - `Month = FORMAT('Date'
[Date], "MMM")`, `Quarter = "Q" & QUARTER('Date'[Date])` - carrying no `dataType` at all (95 such
columns in `_runs/estate-2.339.0-20260829`), and their filters survive exactly like a `dateTime`
bin's. So `ModelFacts.grain_of` reads declared type AND calculated lineage back to the anchor
column, and falls back to `unassessable` on a name-only hint rather than to "clean".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from check_relationship_health import _parse_column_census
from check_stub_measures import parse_model
from dax_tokens import (
    KIND_COLUMN,
    KIND_NAME,
    KIND_OP,
    KIND_TABLE,
    PERIOD_TO_DATE_FUNCTIONS,
    WINDOW_FUNCTIONS,
    ColumnRef,
    Template,
    Token,
    call_body,
    calls_named,
    has_ordering_comparison,
    match_any,
    tokenize,
)

# `ColumnRef` is this module's public vocabulary; consumers import it from here, not from the lexer.
__all__ = [
    "ENGINE_TEMPLATES",
    "GRAIN_DATE",
    "GRAIN_DERIVED",
    "GRAIN_SUSPECT",
    "GRAIN_UNRELATED",
    "PERIOD_TEMPLATES",
    "ColumnRef",
    "Cumulative",
    "ModelFacts",
    "PeriodToDateCall",
    "WindowCall",
    "classify",
    "column_references",
    "read_model_facts",
]

_FILTER = frozenset({"FILTER"})

# The frame every RUNNING_*/WINDOW_* aggregate folds. Spelled out in each template rather than
# nested, so a reader can check a template against the engine source line by line.
_FRAME = "WINDOW ( <num> , <mode> , <num> , <mode> , <spec> )"

# THE CLOSED SET. Each entry is one whole expression the deterministic tier emits, with its engine
# provenance. A measure that is not EXACTLY one of these is never judged.
ENGINE_TEMPLATES: tuple[Template, ...] = (
    # `_emit_table_calc`: RUNNING_*/WINDOW_*/COUNT/statistical aggregates folded over a frame.
    Template("window_frame_aggregate", f"<aggx> ( {_FRAME} , CALCULATE ( <agg> ) )"),
    # `_emit_table_calc`: WINDOW_PERCENTILE(<agg>, k).
    Template("window_percentile", f"PERCENTILEX.INC ( {_FRAME} , CALCULATE ( <agg> ) , <num> )"),
    # `_emit_table_calc`: LAST(). BEFORE window_size, whose template is a prefix of this one.
    Template("window_last", f"COUNTROWS ( {_FRAME} ) - ROWNUMBER ( <spec> )"),
    # `_emit_table_calc`: SIZE().
    Template("window_size", f"COUNTROWS ( {_FRAME} )"),
    # `_emit_table_calc`: FIRST().
    Template("window_first", "1 - ROWNUMBER ( <spec> )"),
    # `_emit_table_calc`: INDEX().
    Template("row_number", "ROWNUMBER ( <spec> )"),
    # `_emit_table_calc`: LOOKUP(<agg>, offset).
    Template("offset_lookup", "CALCULATE ( <agg> , OFFSET ( <num> , <spec> ) )"),
    # `translate_percent_difference_to_dax`.
    Template(
        "percent_difference",
        "DIVIDE ( ( <agg> ) - CALCULATE ( <agg> , OFFSET ( <num> , <spec> ) ) , "
        "ABS ( CALCULATE ( <agg> , OFFSET ( <num> , <spec> ) ) ) )",
    ),
    # `translate_difference_to_dax`.
    Template(
        "difference",
        "VAR _prev = CALCULATE ( <agg> , OFFSET ( <num> , <spec> ) ) RETURN "
        "IF ( ISBLANK ( _prev ) , BLANK ( ) , ( <agg> ) - _prev )",
    ),
)

# Period-to-date is NOT emitted by the engine (0 instances across 526 committed `examples/`
# measures), but a hand-authored one on an unmarked fact table is a real, measured trap - so it is
# judged, and ONLY in the shapes whose non-date argument is a bare aggregate. `<sagg>` rather than
# `<agg>` is load-bearing: the judged property here is whether a filter is REMOVED, and an arbitrary
# inner expression can remove filters itself - `TOTALYTD(CALCULATE(SUM(x),
# REMOVEFILTERS('Orders'[Month])), 'Date'[Date])` - which would be a verdict formed without reading
# the thing that decided it.
PERIOD_TEMPLATES: tuple[Template, ...] = tuple(
    Template("period_to_date", f"{func} ( <sagg> , <qcol> )") for func in ("TOTALYTD", "TOTALMTD", "TOTALQTD")
) + tuple(
    Template("period_to_date", f"CALCULATE ( <sagg> , {func} ( <qcol> ) )")
    for func in ("DATESYTD", "DATESMTD", "DATESQTD")
)

# The engine's own words when it could not translate a Tableau running total, plus the name shapes a
# hand-authored one uses. Used ONLY to surface a `BLANK()` stub as a former running total - never to
# judge one, because a stub has no DAX shape to judge.
_RUNNING_STUB_RE = re.compile(r"\bRUNNING[_ ]?(SUM|AVG|COUNT|MAX|MIN)\b|running|cumulative", re.IGNORECASE)

# Names that SUGGEST a date grain without proving one. Used only to route an otherwise-undecidable
# grouping column to `unassessable`, never to a mismatch: a guess must not fail a build.
_DATE_PART_NAME_RE = re.compile(
    r"(^|[\s_\-])(year|quarter|qtr|month|week|date|period|fiscal|semester|yyyy)([\s_\-]|$)", re.IGNORECASE
)

_DATE_TYPES = frozenset({"date", "datetime"})

# How a grouping column relates to the accumulation's anchor column. `DATE`/`DERIVED` are proven and
# may fail a build; `SUSPECT` is a name-only hint and may only reach `unassessable`.
GRAIN_DATE = "date"
GRAIN_DERIVED = "derived"
GRAIN_SUSPECT = "suspect"
GRAIN_UNRELATED = "unrelated"


@dataclass
class WindowCall:
    """ONE window-family call read out of a MATCHED template, judged entirely on its own.

    Every field here comes from a fully-consumed `<spec>` hole, so there is no "unreadable call"
    state left to represent: an unreadable call means the template did not match, and a measure
    whose template did not match is never judged at all.
    """

    func: str
    ordered_by: list[ColumnRef] = field(default_factory=list)
    partition_by: list[ColumnRef] = field(default_factory=list)


@dataclass
class PeriodToDateCall:
    """ONE `TOTALYTD`/`DATESYTD`-family call read out of a MATCHED template."""

    func: str
    anchor: ColumnRef


@dataclass
class Cumulative:
    """One measure whose DAX declares an accumulation grain, and how it was read.

    `consumed` is the receipt that a whole template matched every token. It is the single fact the
    gate checks before it is allowed to emit `ok` or `mismatch`.
    """

    table: str
    name: str
    shape: str
    tmdl: str
    line: int
    window_calls: list[WindowCall] = field(default_factory=list)
    period_calls: list[PeriodToDateCall] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    consumed: bool = False
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
        """The period-to-date anchor, for presentation and routing only."""
        return self.period_calls[0].anchor if self.period_calls else None


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
    calc_expressions: dict[tuple[str, str], list[ColumnRef]] = field(default_factory=dict)
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
        for found in self.calc_expressions.get(ref.key(), []):
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


def column_references(text: str) -> list[ColumnRef]:
    """Every `'Table'[Column]` / `Table[Column]` / `[Column]` reference in a fragment of DAX.

    Read from TOKENS, so a `[Column]` inside a string literal or a comment contributes nothing - the
    same reason `classify` never sees raw text. Used for calculated-column lineage, where a phantom
    reference out of `Bucket = IF(x, "[Date]", "other")` would otherwise invent a date grain.
    """
    code = tokenize(text).code
    refs: list[ColumnRef] = []
    for position, token in enumerate(code):
        if token.kind != KIND_COLUMN:
            continue
        previous = code[position - 1] if position else None
        table = previous.value if previous is not None and previous.kind in (KIND_TABLE, KIND_NAME) else None
        refs.append(ColumnRef(table=table, column=token.value))
    return refs


def read_model_facts(model_dir: Path) -> ModelFacts:
    """Read the column types, calculated-column lineage and date-table markings in one pass."""
    facts = ModelFacts(column_types=_parse_column_census(model_dir))
    for member in parse_model(model_dir):
        if member.kind == "column" and (member.expression or "").strip():
            key = (member.table.casefold(), member.name.casefold())
            facts.calc_expressions[key] = column_references(member.expression)
    definition = model_dir / "definition"
    root = definition if definition.is_dir() else model_dir
    for path in sorted(root.rglob("*.tmdl")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        table: str | None = None
        for line in text.splitlines():
            declaration = _TABLE_DECL_RE.match(line)
            if declaration:
                table = _unquote_tmdl(declaration.group("name"))
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


def _strip_outer_parens(code: list[Token]) -> list[Token]:
    """Drop parentheses that redundantly wrap the WHOLE token stream."""
    while len(code) >= 2 and code[0].kind == KIND_OP and code[0].text == "(":
        depth = 0
        closer = -1
        for position, token in enumerate(code):
            if token.kind == KIND_OP and token.text == "(":
                depth += 1
            elif token.kind == KIND_OP and token.text == ")":
                depth -= 1
                if depth == 0:
                    closer = position
                    break
        if closer != len(code) - 1:
            return code
        code = code[1:-1]
    return code


def _is_blank_stub(code: list[Token]) -> bool:
    """Whether the ENTIRE expression is one `BLANK()` call, read from tokens rather than text."""
    inner = _strip_outer_parens(code)
    return (
        len(inner) == 3
        and inner[0].kind == KIND_NAME
        and inner[0].uppercase == "BLANK"
        and inner[1].text == "("
        and inner[2].text == ")"
    )


def _signals(code: list[Token]) -> list[str]:
    """The accumulation mechanisms this expression MENTIONS, however it spells them.

    Detection is by CALLED name - a name token followed by `(` - so a table named `Index` or a
    column named `[WINDOW(...)]` raises nothing. A signal never produces a verdict; it decides only
    whether an unmatched expression is reported unassessable or is simply not an accumulation.
    """
    found: list[str] = []
    for index in calls_named(code, WINDOW_FUNCTIONS) + calls_named(code, PERIOD_TO_DATE_FUNCTIONS):
        found.append(f"a {code[index].uppercase}(...) call")
    if any(has_ordering_comparison(call_body(code, index)) for index in calls_named(code, _FILTER)):
        found.append("a FILTER(...) restricted by an ordering comparison (an as-of accumulation)")
    return sorted(set(found))


def _fill_from_template(base: Cumulative, code: list[Token]) -> bool:
    """Fill `base` from the ONE engine template that consumes every token, or return False."""
    window = match_any(ENGINE_TEMPLATES, code)
    if window is not None:
        base.shape = window.template
        base.consumed = True
        base.window_calls = [
            WindowCall(func=window.template, ordered_by=list(spec.ordered_by), partition_by=list(spec.partition_by))
            for spec in window.specs
        ]
        return True
    period = match_any(PERIOD_TEMPLATES, code)
    if period is not None and period.anchors:
        base.shape = period.template
        base.consumed = True
        base.period_calls = [PeriodToDateCall(func=period.template, anchor=period.anchors[0])]
        return True
    return False


def classify(member: Any) -> Cumulative | None:
    """Read one TMDL measure as an accumulation, or return None when it declares no grain.

    Deliberately shape-driven, never name-driven: a name match would both miss the engine's
    `Highlight Max`/`% Highlight Max` (real window measures) and fire on the five `Running Sum`
    measures in the estate that are `BLANK()` stubs with no grain at all. The one exception is the
    stub branch, which uses the name/annotation only to SURFACE the measure.

    Exactly three outcomes, and the middle one is what rounds 1-6 kept getting wrong:

    * a whole engine template consumed every token -> judged, `consumed=True`;
    * no template matched but an accumulation signal is present -> `assessable=False`, unassessable;
    * no signal at all -> None, this is not an accumulation.
    """
    lexed = tokenize(member.expression or "")
    code = lexed.code
    if not code:
        return None
    base = Cumulative(
        table=member.table,
        name=member.name,
        shape="unknown",
        tmdl=member.tmdl.as_posix(),
        line=member.line,
    )
    if _is_blank_stub(code):
        return _classify_stub(member, base)
    readable = not lexed.has_unknown and not lexed.unterminated
    if readable and _fill_from_template(base, code):
        return base
    signals = _signals(code)
    if not signals:
        return None
    base.shape = "unrecognised"
    base.signals = signals
    base.assessable = False
    base.reason = _unrecognised_reason(signals, lexed.unterminated)
    return base


def _unrecognised_reason(signals: list[str], unterminated: str) -> str:
    """Why an expression carrying an accumulation signal is refused rather than judged."""
    trailer = f", and {unterminated}" if unterminated else ""
    return (
        "the measure declares an accumulation - "
        + ", ".join(signals)
        + trailer
        + " - but its whole expression is not one of the "
        + f"{len(ENGINE_TEMPLATES) + len(PERIOD_TEMPLATES)} shapes this gate recognises, so nothing here reads "
        "which grain it accumulates over. Probe it with EVALUATE at every axis grain the emitted "
        "visuals bind"
    )


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
