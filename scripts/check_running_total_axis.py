"""
purpose: catch the CROSS-ARTIFACT defect no single-layer gate can see - a running-total measure
         whose addressed grain disagrees with the axis of the PBIR visual that binds it, so the
         line silently plots each bucket's OWN total instead of a cumulative one.
usage:   python scripts/check_running_total_axis.py <bundle-or-report-dir> [...]
         python scripts/check_running_total_axis.py --model <x.SemanticModel> --report <x.Report>
                                                    [--json <file>] [--quiet] [--warn-only]
                                                    [--strict]

Why this exists (issue #218, follow-up from #175 Defect 1)
----------------------------------------------------------
A running total is correct only at the grain it was ADDRESSED for, and nothing checked that the
grain matched the visual it was bound to. Measured, cold run S14, one emitted measure ordered by
`'Orders'[Order_Date]`:

    | visual axis              | emitted measure returned      | verdict                 |
    |--------------------------|-------------------------------|-------------------------|
    | `'Orders'[Order_Date]`   | 16.45 -> ... -> 2,297,200.86  | cumulative              |
    | `'Date'[Date]`           | 16.45, 288.06, 19.54 ...      | each day's own total    |
    | `'Date'[Month Start]`    | 14,236.89, 4,519.89 ...       | each month's own total  |

The emitted report put `'Date'[Month Start]` on the axis. Every structural gate passed: the DAX is
valid, the PBIR validates, and every field resolves. The defect lives BETWEEN the artifacts - the
DAX is in TMDL, the axis binding is in PBIR, and neither half is wrong on its own. That is why
`check_datamodel.py` (models only) and `check_pbir_valid.py` (reports only) structurally cannot see
it, and why `.github/skills/powerbi-semantic-model-gotchas/SKILL.md` section 4 recorded it as an
open hole: *"Until a cross-artifact report-axis check exists, run EVALUATE probes at every axis
grain"*. This is that check.

The two mechanisms, and the ONE invariant each
----------------------------------------------
1. **Window family** - `WINDOW`/`OFFSET`/`INDEX`/`RANK`/`ROWNUMBER` with `ORDERBY(<col>)` and no
   explicit relation argument. Their relation defaults to the visual's own shaped table, so
   `ORDERBY(<col>)` can only order what the visual actually projects.
   **Invariant: every ORDERBY column must appear among the visual's projections.**
   This reproduces the measured table above exactly: axis `'Orders'[Order_Date]` projects the
   ordered column (correct); axis `'Date'[Date]` or `'Date'[Month Start]` does not (both wrong).

2. **As-of filter** - `CALCULATE(<agg>, FILTER(ALL(t[c], ...), t[c] <= MAX(t[c])))`. `ALL` clears
   only the columns it names; a coarser same-table date column left on the axis survives, so the
   rows are restricted to that bucket and the "running total" becomes the bucket's own total.
   **Invariant: an axis column on the compared column's table must be cleared, or be the compared
   column itself.** Flagged only when that surviving axis column is `dataType: date`/`dateTime`,
   because that is the measured mechanism and a non-date axis is a legitimate partition.

What it DELIBERATELY does not flag
----------------------------------
Every one of these is a place where a confident verdict would be a guess, so it is reported as
`unassessable` (exit 3, "needs a live EVALUATE") or as a named non-flag - never silently dropped
and never counted as clean:

* **Period-to-date time intelligence** (`DATESYTD`, `TOTALYTD`, `DATESMTD`, `DATESQTD`, ...). On a
  table marked `dataCategory: Time` these auto-remove filters from the date table's other columns,
  so a month axis is CORRECT; on an unmarked table, or against a fact-table date column, it is not.
  Deciding that needs the date-table marking AND the relationship path, which is more inference
  than evidence. They are counted and NAMED as `not_assessed_by_design` on every run - including a
  clean one - but they never produce a per-visual finding and never move the exit code. Measured
  reason: an earlier revision reported them as `unassessable` and emitted **12 rows against one
  committed worked example** (`examples/superstore-sales-performance`), whose CP/PP measures are
  fixed-window `DATESBETWEEN` comparisons, not accumulations at all. A gate that noisy on shipping
  evidence gets muted, and muting it costs the two mechanisms below.
* **Fixed-window date filters** (`DATESBETWEEN`, `DATESINPERIOD`). Not accumulations: their window
  is anchored by arguments, not by the visual's grain, so there is nothing for an axis to disagree
  with. Not counted, not flagged.
* **An explicit relation argument** to a window function. The relation then decides the ordering
  domain, not the visual, and a table expression cannot be resolved statically -> `unassessable`.
* **A cross-table as-of filter.** Whether a `'Date'[Month Start]` axis reaches the fact table
  depends on the relationship graph and cross-filter direction -> `unassessable`.
* **A visual that projects no grouping column at all** (a card, a KPI). "The ORDERBY column is not
  projected" is true but says nothing about whether the single-row result is wrong -> `unassessable`.
* **A non-date axis on an as-of filter.** A running total partitioned by Region is an ordinary
  shape, not a defect -> reported as `ok` carrying `not_flagged`, so the decision is visible.
* **A stubbed running total** (`= BLANK()`). Real: all five `RUNNING_SUM` translations in the
  2026-08-29 estate are stubs. They have no grain to disagree with; `check_stub_measures.py` owns
  them, and they are surfaced here as a count so a reader is not told the model is clean. This is
  the ONE place a name/annotation signal is used, and only to SURFACE a measure, never to fail one:
  a stub carries no DAX shape, so the engine's own `TranslationStubReason = unsupported function
  RUNNING_SUM` is the only evidence there was a running total here at all.

What it will NOT tell you
-------------------------
That the number is RIGHT. A held invariant means "the addressed grain is present on the visual", not
"the value matches Tableau". Nothing here executes DAX; the oracle is still an `EVALUATE` probe at
the axis grain, which is exactly what the `unassessable` rows ask for.

Corpus reality, stated plainly
------------------------------
Measured on `_runs/estate-2.339.0-20260829` (58 models, 471 TMDL docs, 51 pbip projects) and on the
16 committed `examples/`: **no live instance exists**. The estate holds two real
`WINDOW(... ORDERBY(...))` measures (`HR Dashboard` -> `_Measures`: `Highlight Max`,
`% Highlight Max`) and neither is referenced anywhere in its report - not in `queryState`, not in a
filter, not in conditional formatting - so there is no binding to disagree with. Five more
"Running ..." measures are `BLANK()` stubs. So this gate is proven on the reproduced S14 fixture and
on synthetic corpora, and on real estate data it is proven only to stay silent. That is a real
limitation, not a clean bill of health.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from bundle_corpus import shipping_reports

# Deliberate reuse over reimplementation. `_walk`/`_source_scope` carry a measured fix (`From` is a
# SIBLING of `queryState`, so a walk started inside it resolves every aliased projection to None);
# re-deriving that here would re-open the bug in a second place. `_parse_column_census` is the
# existing TMDL column/dataType reader. Module-private by name, shared by intent - the same way
# `check_relationship_health` already borrows `check_field_bindings`' relationship components.
from check_field_bindings import FieldRef, _source_scope, _walk, model_for_report
from check_relationship_health import _parse_column_census
from check_stub_measures import is_stub_expression, parse_model, strip_comments

REPORT_NAME = "running-total-axis-check.json"
REPORT_VERSION = 1

STATUS_OK = "OK"
STATUS_MISMATCH = "MISMATCH"
STATUS_UNASSESSABLE = "UNASSESSABLE"
STATUS_SKIPPED = "SKIPPED"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2
EXIT_UNASSESSABLE = 3

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
# The engine's own words when it could not translate a Tableau running total, plus the name shapes
# a hand-authored one uses. Used ONLY to surface a `BLANK()` stub as a former running total - never
# to judge one, because a stub has no DAX shape to judge.
_RUNNING_STUB_RE = re.compile(r"\bRUNNING[_ ]?(SUM|AVG|COUNT|MAX|MIN)\b|running|cumulative", re.IGNORECASE)
_ALL_FUNCTION_RE = re.compile(r"^ALL(SELECTED|NOBLANKROW|CROSSFILTERED|EXCEPT)?$", re.IGNORECASE)

# The roles that put a column on the AXIS of a chart, measured across the estate + examples corpora
# (319 Category, 47 X, 40 Rows). `Series`/`Tooltips`/`Size` group the query too, but they are a
# legend or a hover detail rather than the accumulation axis, so they only ever ACQUIT.
AXIS_ROLES = ("Category", "Rows", "X")

# A measure-only visual has no grouping column at all, so "the ordered column is absent" is true but
# uninformative - the single-row result may be a perfectly good grand total.
_DATE_TYPES = frozenset({"date", "datetime"})


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
class VisualBinding:
    """One `visual.json` reduced to what this gate compares: its roles and their references."""

    file: Path
    visual: str
    visual_type: str
    roles: dict[str, list[FieldRef]]

    def columns(self) -> list[FieldRef]:
        """Every projected COLUMN, in any role - the window relation's candidate ordering keys."""
        return [ref for refs in self.roles.values() for ref in refs if ref.kind == "Column"]

    def axis_columns(self) -> list[FieldRef]:
        """Projected columns in a role that puts them on the accumulation axis."""
        return [ref for role in AXIS_ROLES for ref in self.roles.get(role, []) if ref.kind == "Column"]

    def measures(self) -> list[FieldRef]:
        """Every measure this visual binds, in any role."""
        return [ref for refs in self.roles.values() for ref in refs if ref.kind == "Measure"]

    def has_hierarchy(self) -> bool:
        """Whether any role projects a hierarchy level, which may expand to an unnamed column."""
        return any(ref.kind == "HierarchyLevel" for refs in self.roles.values() for ref in refs)


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


def _classify_window(expr: str, base: Cumulative) -> Cumulative | None:
    """Read a window-family call, or record why its grain cannot be read."""
    for func in WINDOW_FUNCTIONS:
        for body in _call_bodies(expr, func):
            args = _split_arguments(body)
            named = [
                a for a in args if any(re.match(rf"^{c}\s*\(", a.strip(), re.IGNORECASE) for c in _CLAUSE_FUNCTIONS)
            ]
            positional = [a for a in args if a not in named]
            base.shape = f"{func.lower()}_orderby"
            if any(not _is_positional(arg) for arg in positional):
                base.assessable = False
                base.reason = (
                    f"{func} carries an explicit relation argument, so the ordering domain is that "
                    "table expression rather than the visual"
                )
                return base
            order_body = _clause(args, "ORDERBY")
            if order_body is None:
                # No ORDERBY means the window orders by the relation's own columns, and the relation
                # IS the visual. There is no second grain that could disagree - a verified acquittal,
                # not a guess.
                base.reason = f"{func} has no ORDERBY clause, so it orders by the visual's own grain"
                return base
            base.ordered_by = _column_refs(order_body)
            partition_body = _clause(args, "PARTITIONBY")
            if partition_body:
                base.partition_by = _column_refs(partition_body)
            if not base.ordered_by:
                base.assessable = False
                base.reason = f"{func} ORDERBY names no resolvable column reference"
            elif any(ref.table is None for ref in base.ordered_by):
                base.assessable = False
                base.reason = f"{func} ORDERBY uses an unqualified column, so its table is ambiguous"
            return base
    return None


def _as_of_predicate(args: list[str]) -> ColumnRef | None:
    """The compared column of an `<col> <= <bound>` as-of predicate, if this FILTER has one."""
    if len(args) < 2:
        return None
    predicate = args[1]
    match = re.search(r"(.+?)(<=|<)(?!=)", predicate, re.DOTALL)
    if not match:
        return None
    left = _column_refs(match.group(1))
    return left[-1] if left else None


def _classify_as_of(expr: str, base: Cumulative) -> Cumulative | None:
    """Read a `FILTER(ALL(...), t[c] <= ...)` running total, or record why it cannot be read."""
    for body in _call_bodies(expr, "FILTER"):
        args = _split_arguments(body)
        if not args:
            continue
        head = re.match(r"^([A-Za-z]+)\s*\(", args[0].strip())
        if not head or not _ALL_FUNCTION_RE.match(head.group(1)):
            continue
        compared = _as_of_predicate(args)
        if compared is None:
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
    window = _classify_window(expr, base)
    if window is not None:
        return window
    return _classify_as_of(expr, base)


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
    """Period-to-date measures this gate deliberately does not assess, named so the omission shows."""
    return sorted(
        f"'{m.table}'[{m.name}]" for m in measures if _TIME_INTELLIGENCE_RE.search(strip_comments(m.expression or ""))
    )


def iter_visuals(report_dir: Path) -> list[VisualBinding]:
    """Every `visual.json`'s query projections, kept PER ROLE.

    `check_field_bindings.iter_visual_queries` flattens roles away because it only asks whether a
    reference resolves. Here the role is the finding: `Category` is the accumulation axis and
    `Y`/`Tooltips` are not, so the two cannot share a reader.
    """
    visuals: list[VisualBinding] = []
    definition = report_dir / "definition"
    root = definition if definition.is_dir() else report_dir
    for path in sorted(root.rglob("visual.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        visual = payload.get("visual") if isinstance(payload, dict) else None
        if not isinstance(visual, dict):
            continue
        query = visual.get("query")
        state = query.get("queryState") if isinstance(query, dict) else None
        if not isinstance(state, dict):
            continue
        roles: dict[str, list[FieldRef]] = {}
        for role, body in state.items():
            refs: list[FieldRef] = []
            _walk(body, _source_scope(query, {}), path, refs)
            roles[role] = refs
        if not any(roles.values()):
            continue
        name = payload.get("name") if isinstance(payload.get("name"), str) else path.parent.name
        visual_type = visual.get("visualType") if isinstance(visual.get("visualType"), str) else "unknown"
        visuals.append(VisualBinding(file=path, visual=name, visual_type=visual_type, roles=roles))
    return visuals


def _verdict(kind: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    """One machine-readable judgement about a (measure, visual) pair."""
    return {"verdict": kind, "code": code, "detail": detail, **extra}


def _judge_window(cumulative: Cumulative, visual: VisualBinding) -> dict[str, Any]:
    """Window family: every ORDERBY column must be among the visual's own projections."""
    projected = {ColumnRef(ref.entity, ref.prop).key() for ref in visual.columns()}
    absent = [ref for ref in cumulative.ordered_by if ref.key() not in projected]
    if not absent:
        return _verdict("ok", "orderby_projected", "every ORDERBY column is projected by this visual")
    if not projected:
        return _verdict(
            "unassessable",
            "no_grouping_column",
            f"{visual.visual_type} projects no grouping column, so whether the window degenerates "
            "depends on the query Power BI generates - probe it with EVALUATE",
        )
    if visual.has_hierarchy():
        return _verdict(
            "unassessable",
            "hierarchy_projection",
            "the visual projects a hierarchy level, which may expand to the ordered column",
        )
    return _verdict(
        "mismatch",
        "orderby_not_on_axis",
        "ordered by "
        + ", ".join(ref.qualified() for ref in absent)
        + ", which this visual does not project; the window degenerates to each bucket's own value. "
        + "Axis is "
        + (", ".join(f"'{r.entity}'[{r.prop}]" for r in visual.axis_columns()) or "(no axis role)"),
        ordered_by=[ref.qualified() for ref in cumulative.ordered_by],
        projected=[f"'{r.entity}'[{r.prop}]" for r in visual.columns()],
    )


def _judge_as_of(cumulative: Cumulative, visual: VisualBinding, columns: dict[str, Any]) -> dict[str, Any]:
    """As-of filter: a surviving same-table date column on the axis is the measured defect."""
    compared = cumulative.compared
    assert compared is not None  # guarded by classify(); an unqualified compare is unassessable
    cleared = {ref.key() for ref in cumulative.cleared_columns} | {compared.key()}
    cleared_tables = {name.casefold() for name in cumulative.cleared_tables}
    survivors = [
        ref
        for ref in visual.axis_columns()
        if ColumnRef(ref.entity, ref.prop).key() not in cleared and ref.entity.casefold() not in cleared_tables
    ]
    if not survivors:
        return _verdict(
            "ok", "axis_cleared", "every axis column is cleared by the as-of filter or is the compared column"
        )
    same_table = [ref for ref in survivors if ref.entity.casefold() == (compared.table or "").casefold()]
    dated = [ref for ref in same_table if _is_date_typed(columns, ref.entity, ref.prop)]
    if dated:
        return _verdict(
            "mismatch",
            "axis_grain_not_cleared",
            "axis column(s) "
            + ", ".join(f"'{r.entity}'[{r.prop}]" for r in dated)
            + f" sit on {compared.qualified()}'s table and are NOT cleared, so the surviving filter "
            "restricts the rows to that bucket and the running total becomes the bucket's own total",
            compared=compared.qualified(),
            cleared=[ref.qualified() for ref in cumulative.cleared_columns],
        )
    cross_table = [ref for ref in survivors if ref not in same_table]
    if cross_table:
        return _verdict(
            "unassessable",
            "cross_table_axis",
            "axis column(s) "
            + ", ".join(f"'{r.entity}'[{r.prop}]" for r in cross_table)
            + f" are on another table than {compared.qualified()}; whether their filter reaches the "
            "aggregated rows depends on the relationship graph - probe it with EVALUATE",
        )
    return _verdict(
        "ok",
        "axis_not_a_date_grain",
        "the surviving axis column(s) are not date-typed; a running total partitioned by a "
        "non-date column is a legitimate shape",
        not_flagged=[f"'{r.entity}'[{r.prop}]" for r in survivors],
    )


def _is_date_typed(columns: dict[str, Any], table: str, column: str) -> bool:
    """Whether TMDL declares this column `dataType: date`/`dateTime`.

    Strictly the declared type, never the name heuristic `ColumnInfo.is_date_like` also accepts: a
    column called `Updated` is not evidence enough to fail a build on.
    """
    for name, info in columns.items():
        if name.casefold() != table.casefold():
            continue
        for col_name, col in info.columns.items():
            if col_name.casefold() == column.casefold():
                return (col.data_type or "").casefold() in _DATE_TYPES
    return False


def _tmdl_documents(model_dir: Path) -> int:
    """How many TMDL documents this model actually has.

    The discriminator between "this model declares no measure" (a complete answer: nothing can be a
    running total) and "nothing was read at all" (a wrong path, an unreadable tree). Without it, a
    model with zero measures and a mistyped model path produce the same verdict, and one of those is
    a clean bill of health that was never earned.
    """
    definition = model_dir / "definition"
    root = definition if definition.is_dir() else model_dir
    return sum(1 for _ in root.rglob("*.tmdl"))


def check_pair(report_dir: Path, model_dir: Path) -> dict[str, Any]:
    """Grade every cumulative measure the report binds, against the visual that binds it."""
    measures = [m for m in parse_model(model_dir) if m.kind == "measure"]
    cumulatives = {}
    for member in measures:
        found = classify(member)
        if found is not None:
            cumulatives[(member.table.casefold(), member.name.casefold())] = found
    base = {
        "report": str(report_dir),
        "model": str(model_dir),
        "measures_parsed": len(measures),
        "cumulative_measures": len(cumulatives),
        "not_assessed_by_design": period_to_date_measures(measures),
    }
    if not measures and not _tmdl_documents(model_dir):
        return {
            **base,
            "status": STATUS_SKIPPED,
            "reason": f"no TMDL document read under {model_dir}",
            "findings": [],
        }
    if not cumulatives:
        return {
            **base,
            "status": STATUS_NOT_APPLICABLE,
            "reason": "no running-total or window measure in this model, so no grain can disagree with an axis",
            "findings": [],
        }
    columns = _parse_column_census(model_dir)
    visuals = iter_visuals(report_dir)
    findings, bound = _grade_visuals(visuals, cumulatives, columns)
    unbound = sorted(c.label for key, c in cumulatives.items() if key not in bound)
    stubs = sorted(c.label for c in cumulatives.values() if c.shape == "stub")
    return {
        **base,
        **_pair_status(findings, unbound, stubs, len(visuals)),
        "visuals_scanned": len(visuals),
        "unbound_cumulative_measures": unbound,
        "stubbed_cumulative_measures": stubs,
        "findings": findings,
    }


def _grade_visuals(
    visuals: list[VisualBinding],
    cumulatives: dict[tuple[str, str], Cumulative],
    columns: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """Judge every (cumulative measure, binding visual) pair, and report which measures were bound."""
    findings: list[dict[str, Any]] = []
    bound: set[tuple[str, str]] = set()
    for visual in visuals:
        for ref in visual.measures():
            key = (ref.entity.casefold(), ref.prop.casefold())
            cumulative = cumulatives.get(key)
            if cumulative is None:
                continue
            bound.add(key)
            findings.append(
                {
                    "measure": cumulative.label,
                    "shape": cumulative.shape,
                    "tmdl": cumulative.tmdl,
                    "line": cumulative.line,
                    "visual": visual.visual,
                    "visual_type": visual.visual_type,
                    "visual_file": str(visual.file),
                    **_judge(cumulative, visual, columns),
                }
            )
    return findings, bound


def _judge(cumulative: Cumulative, visual: VisualBinding, columns: dict[str, Any]) -> dict[str, Any]:
    """Route one pair to the invariant its DAX shape actually declares."""
    if not cumulative.assessable:
        return _verdict("unassessable", cumulative.shape, cumulative.reason)
    if cumulative.ordered_by:
        return _judge_window(cumulative, visual)
    if cumulative.shape.endswith("_orderby"):
        return _verdict("ok", "orders_by_visual_grain", cumulative.reason)
    if cumulative.compared is not None:
        return _judge_as_of(cumulative, visual, columns)
    return _verdict("unassessable", "unreadable_grain", "the accumulation grain could not be read from the DAX")


def _pair_status(
    findings: list[dict[str, Any]], unbound: list[str], stubs: list[str], visual_count: int
) -> dict[str, Any]:
    """The one-pair verdict, keeping 'nothing was assessed' distinct from 'nothing was wrong'."""
    if any(f["verdict"] == "mismatch" for f in findings):
        return {"status": STATUS_MISMATCH}
    if any(f["verdict"] == "unassessable" for f in findings):
        return {"status": STATUS_UNASSESSABLE}
    if findings:
        return {"status": STATUS_OK}
    reason = (
        f"{len(unbound)} cumulative measure(s) exist but no visual binds them"
        if unbound
        else f"no visual among {visual_count} binds a cumulative measure"
    )
    if stubs and len(stubs) == len(unbound):
        reason += "; all of them are BLANK() stubs (check_stub_measures.py owns those)"
    return {"status": STATUS_SKIPPED, "reason": reason}


def scan(root: Path, model_override: Path | None = None) -> dict[str, Any]:
    """Grade every shipping report under `root` against the model it actually ships with."""
    pairs: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for report_dir in shipping_reports(root):
        model_dir = model_override or model_for_report(report_dir)
        if model_dir is None:
            unresolved.append(str(report_dir))
            continue
        pairs.append(check_pair(report_dir, model_dir))
    return merge(pairs, unresolved, str(root))


def merge(pairs: list[dict[str, Any]], unresolved: list[str], root: str) -> dict[str, Any]:
    """Fold per-pair verdicts into one, never letting an unassessed report read as clean.

    Pair-level `SKIPPED` is folded into the top-level `UNASSESSABLE` rather than ranked below `OK`.
    A bundle of 20 reports where 19 are clean and one could not be read is NOT a clean bundle, and
    ranking `OK` above `SKIPPED` is precisely how the twentieth disappears. Top-level `SKIPPED`
    therefore means *every* pair was skipped; both exit 3 either way, so nothing is lost but the
    masking.
    """
    statuses = {pair["status"] for pair in pairs}
    findings = [f for pair in pairs for f in pair.get("findings", [])]
    unassessed = [
        {"report": pair["report"], "reason": pair.get("reason", "")}
        for pair in pairs
        if pair["status"] == STATUS_SKIPPED
    ]
    if not pairs and not unresolved:
        status = STATUS_SKIPPED
    elif STATUS_MISMATCH in statuses:
        status = STATUS_MISMATCH
    elif statuses == {STATUS_SKIPPED} and not unresolved:
        status = STATUS_SKIPPED
    elif STATUS_UNASSESSABLE in statuses or unassessed or unresolved:
        status = STATUS_UNASSESSABLE
    elif STATUS_OK in statuses:
        status = STATUS_OK
    else:
        status = STATUS_NOT_APPLICABLE
    return {
        "version": REPORT_VERSION,
        "root": root,
        "status": status,
        "pairs_scanned": len(pairs),
        "reports_without_model": unresolved,
        "unassessed_pairs": unassessed,
        "mismatches": sum(1 for f in findings if f["verdict"] == "mismatch"),
        "unassessable": sum(1 for f in findings if f["verdict"] == "unassessable"),
        "assessed_clean": sum(1 for f in findings if f["verdict"] == "ok"),
        "unbound_cumulative_measures": sorted({m for p in pairs for m in p.get("unbound_cumulative_measures", [])}),
        "stubbed_cumulative_measures": sorted({m for p in pairs for m in p.get("stubbed_cumulative_measures", [])}),
        "not_assessed_by_design": sorted({m for p in pairs for m in p.get("not_assessed_by_design", [])}),
        "pairs": pairs,
    }


def _render_findings(report: dict[str, Any], verdict: str, header: str) -> list[str]:
    """The per-finding block for one verdict class, or nothing when that class is empty."""
    rows = [f for pair in report.get("pairs", []) for f in pair.get("findings", []) if f["verdict"] == verdict]
    if not rows:
        return []
    lines = [header]
    for row in rows:
        lines.append(f"  - {row['measure']} on {row['visual_type']} {row['visual']} [{row['code']}]")
        lines.append(f"      {row['detail']}")
    return lines


def _render_notes(report: dict[str, Any]) -> list[str]:
    """The lines that must print at EVERY status, including a clean one.

    An affirmative verdict has to say what it did NOT look at, or it reads as a full clearance. The
    period-to-date list in particular is printed on a PASS for the same reason `check_path_ceiling`
    prints `root_budget` on a PASS: a number shown only sometimes cannot be told from a dropped one.
    """
    lines: list[str] = []
    for entry in report.get("unassessed_pairs", []):
        lines.append(f"  UNASSESSED - {entry['report']}: {entry['reason']}")
    for path in report.get("reports_without_model", []):
        lines.append(f"  UNASSESSED - no semantic model resolved for {path}")
    if report.get("unbound_cumulative_measures"):
        lines.append(
            "  note: cumulative measure(s) bound to NO visual, so no axis could be compared: "
            + ", ".join(report["unbound_cumulative_measures"])
        )
    if report.get("stubbed_cumulative_measures"):
        lines.append(
            "  note: BLANK() stub(s) with no grain yet (check_stub_measures.py owns these): "
            + ", ".join(report["stubbed_cumulative_measures"])
        )
    if report.get("not_assessed_by_design"):
        names = report["not_assessed_by_design"]
        lines.append(
            f"  NOT ASSESSED by this gate ({len(names)} period-to-date measure(s)): "
            + ", ".join(names)
            + "\n      their grain depends on date-table marking and the relationship path - probe with EVALUATE"
        )
    return lines


def render(report: dict[str, Any]) -> str:
    """Human-readable verdict, in the shape the sibling gates use."""
    status = report.get("status")
    head = f"RUNNING-TOTAL AXIS CHECK: {status}"
    if status == STATUS_NOT_APPLICABLE:
        first = f"{head} - no running-total or window measure in any shipping model; nothing can disagree with an axis."
        return "\n".join([first] + _render_notes(report))
    if status == STATUS_SKIPPED:
        first = f"{head} - nothing was assessed across {report.get('pairs_scanned', 0)} report/model pair(s)"
        return "\n".join([first] + (_render_notes(report) or ["  no report/model pair found"]))
    lines = [
        f"{head} - {report.get('mismatches', 0)} mismatch, {report.get('unassessable', 0)} unassessable, "
        f"{report.get('assessed_clean', 0)} clean across {report.get('pairs_scanned', 0)} report/model pair(s)"
    ]
    lines += _render_findings(report, "mismatch", "  MISMATCH - the measure disagrees with its own visual:")
    lines += _render_findings(report, "unassessable", "  UNASSESSABLE - needs a live EVALUATE at the axis grain:")
    lines += _render_notes(report)
    if status == STATUS_MISMATCH:
        lines.append(
            "  Fix at the layer that owns it: re-address the measure to the axis grain (model), or\n"
            "  put the addressed column on the axis (report). Do NOT delete the visual."
        )
    return "\n".join(lines)


def _emit(text: str, stream) -> None:
    """Print one line, degrading only the characters this stream cannot encode."""
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="backslashreplace").decode(encoding), file=stream)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """CLI surface, mirroring the sibling cross-artifact gate."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="bundle folder(s) or .Report folder(s)")
    parser.add_argument("--model", type=Path, help="explicit .SemanticModel to grade against")
    parser.add_argument("--report", type=Path, help="explicit .Report to grade")
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    parser.add_argument("--warn-only", action="store_true", help="always exit 0")
    parser.add_argument("--strict", action="store_true", help="treat an unassessable grain as a finding (exit 1)")
    return parser.parse_args(argv)


def _explicit_report(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str]:
    """The `--model`/`--report` path, where the pair is named rather than discovered."""
    if not (args.report and args.model):
        return None, "--model and --report must be given together"
    if not args.report.is_dir() or not args.model.is_dir():
        return None, "--model and --report must both be directories"
    return merge([check_pair(args.report.resolve(), args.model.resolve())], [], str(args.report)), ""


def _scanned_report(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str]:
    """The positional path, where every shipping report is paired with its own model."""
    if not args.paths:
        return None, "give a bundle/report path, or --model with --report"
    missing = [str(path) for path in args.paths if not path.is_dir()]
    if missing:
        return None, f"not a directory: {', '.join(missing)}"
    scanned = [scan(path.resolve()) for path in args.paths]
    if len(scanned) == 1:
        return scanned[0], ""
    return (
        merge(
            [pair for one in scanned for pair in one["pairs"]],
            [path for one in scanned for path in one["reports_without_model"]],
            "; ".join(one["root"] for one in scanned),
        ),
        "",
    )


def _build_report(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str]:
    """Resolve the CLI's targets into ONE report, or return the usage error that stopped it."""
    if args.report or args.model:
        return _explicit_report(args)
    return _scanned_report(args)


def _exit_code(report: dict[str, Any], args: argparse.Namespace) -> int:
    """The contract callers gate on. `SKIPPED` and `UNASSESSABLE` share exit 3 deliberately: both
    mean "this was not established", and only `--strict` promotes that to a refusal."""
    if args.warn_only:
        return EXIT_OK
    if report["status"] == STATUS_MISMATCH:
        return EXIT_MISMATCH
    if report["status"] in (STATUS_UNASSESSABLE, STATUS_SKIPPED):
        return EXIT_MISMATCH if args.strict else EXIT_UNASSESSABLE
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _parse_args(argv)
    report, usage_error = _build_report(args)
    if report is None:
        _emit(f"ERROR: {usage_error}", sys.stderr)
        return EXIT_USAGE

    # The machine-readable artifact is written BEFORE anything is printed, so a console that cannot
    # encode a table name cannot destroy the report an automated consumer asked for.
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        _emit(render(report), sys.stdout)
    return _exit_code(report, args)


if __name__ == "__main__":
    raise SystemExit(main())
