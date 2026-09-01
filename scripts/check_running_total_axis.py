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
   **Invariant: every ORDERBY column must appear among the visual's own grouping columns.**
   This reproduces the measured table above exactly: axis `'Orders'[Order_Date]` projects the
   ordered column (correct); axis `'Date'[Date]` or `'Date'[Month Start]` does not (both wrong).

2. **As-of filter** - `CALCULATE(<agg>, FILTER(ALL(t[c], ...), t[c] <= MAX(t[c])))`. `ALL` clears
   only the columns it names; a coarser same-table date grain left on the visual survives, so the
   rows are restricted to that bucket and the "running total" becomes the bucket's own total.
   **Invariant: for EVERY as-of call in the measure, independently, a grouping column on the
   compared column's table must be cleared, or be the compared column itself.** Independently is
   load-bearing: a term clearing both `Order_Date` and `Order Date (Month)` must not supply the
   month clearance for a second term that clears neither, so the cleared sets are never unioned.
   The bound must be PROVEN to move with the visual - a `MAX`-like call reading the compared column,
   or a `VAR` resolving to one. A pinned `<= DATE(2024,12,31)` and a `MAXX(ALL(t), t[c])` whose
   removal covers the compared column's whole table are ordinary "through cutoff" measures whose
   per-bucket totals are the point; a foreign-date bound, or a removal naming only the compared
   column, is `unassessable`.

3. **Period-to-date** - `TOTALYTD`/`DATESYTD`/`DATESMTD`/`DATESQTD` and friends. Time intelligence
   auto-removes filters from the other columns of a table marked `dataCategory: Time`, which is what
   makes a month axis correct. Nothing removes a date grain on an **unmarked** table.
   **Invariant: for EVERY such call, every date grain projected by the visual must sit on the marked
   date table that owns ITS `<dates>` argument** - otherwise `unassessable`, never a pass.

THE rule that replaced "a classifier never stops at the first match"
--------------------------------------------------------------------
Rounds 1-4 found the same bug at eight sites - the first `WINDOW` call, the first qualifying
`FILTER`, the first `&&` conjunct, the first `ALL`/`REMOVEFILTERS`, the first reader in the dispatch
chain, the first `VAR`, the first period-to-date function in DICT order, and an unreadable window
call suppressing a readable one - three of them SILENT PASSES. Every mechanism became a LIST folded
through `_worst()`, where **mismatch > unassessable > ok**.

**Round 5 found that this was treating the symptom.** Every one of its findings was a regex
mis-reading DAX syntax rather than a fold picking the wrong candidate: a redundant paren around a
column made a measure vanish (exit 0); a `REMOVEFILTERS` in an UNREACHABLE `IF` branch acquitted a
broken running total (exit 0 / OK); and a legitimate TEXT measure whose string literal contained
DAX was reported MISMATCH (exit 1) - **wrong in both directions at once**. Semantic analysis of
arbitrary DAX with regexes does not terminate, so the grammar was cut to what the ENGINE emits:

* the **window family** and **period-to-date** are judged. Both are structural reads - "which
  columns does this call name?" - and the window family is exactly what `calc_to_dax.py:3548`
  emits for every cumulative measure the deterministic tier produces.
* the **as-of `FILTER(... <= ...)` family is DETECTED, never classified**: always `unassessable`,
  with the measure named and "probe it with EVALUATE". It never emits a mismatch, so it cannot be
  wrong in the direction that gets a gate switched off; it never emits `ok`, so it cannot grant
  false confidence. Measured: 0 of the 526 measures in the 16 committed `examples/` models carry
  an ordering comparison inside a `FILTER`, so this costs nothing on shipped bytes.
* `dax_grain.mask_noncode` blanks string literals and comments **before any regex sees the text**.

`_worst()` still exists and still folds, because the window family genuinely has several calls; the
list of things it folds is simply much shorter now. `_clause_bodies` reads EVERY `ORDERBY` in a
window call and refuses a second one rather than assuming there cannot be one.

Two words this module refuses to conflate: PROXY and PROPERTY
-------------------------------------------------------------
Blind review found four defects that were all one mistake - deciding "safe" from something merely
correlated with safety. Each is now decided from the property itself, and the proxies are gone:

* **a curated axis-role list; empty => "axis cleared"** -> EVERY column that GROUPS the query is
  examined, and an empty grouping set is `unassessable`, not clean. `AXIS_ROLES` survives as
  presentation only. Measured on the estate's Section 12 pivot: a `dateTime` bin under the real
  `Columns` role exited 0 while the identical bin under `Rows` exited 1.
* **declared `dataType` is not date => safe grain** -> declared type **or calculated lineage back to
  the anchor**. The engine writes its coarse bins as `Month = FORMAT('Date'[Date], "MMM")` and
  `Quarter = "Q" & QUARTER(...)`, with no `dataType` at all - 95 such columns in the 2026-08-29
  estate - and their filters survive exactly like a `dateTime` bin's.
* **period-to-date => "by design" => not assessed** -> judged against the date-table marking;
  anything unproven is `unassessable`.

And the mirror-image mistake, which round 2 found four times: deciding "UNSAFE" from something
merely correlated with a defect. A false positive is the more dangerous direction, because it is
what gets a gate switched off:

* **a projected column => a grouping column** -> only a projection whose `field` IS a `Column` or a
  `HierarchyLevel` groups the query. An `Aggregation` wrapping a column is a value computed per
  group; measured across the estate + `examples/`, 377 projections are exactly that, so an
  aggregated `MAX(Orders[Ship_Date])` tooltip was blocking a report grouped only by `Region`.
* **`MAX(` in the bound => the bound moves with the visual** -> the bound must reference **the
  compared column**.
* **`ALL(` in the bound => the bound is pinned** -> pinned *with respect to what*. Only a removal
  covering the compared column's whole table (or the whole model) proves it; measured,
  `REMOVEFILTERS('Orders'[Region])` cannot touch a month-axis date filter, and treating it as
  pinning dropped a genuine defect at exit 0.
* **any `<` => an upper-bound predicate** -> the operator is parsed at depth 0 and outside strings,
  so DAX's `<>` is no longer read as the `<` it begins with.
* **an uncleared same-table date grain => the bucket's own total** -> only when the addressed date
  is NOT itself grouping. When it IS, the accumulation runs along it and RESTARTS per bucket, which
  is a legitimate period-partitioned running total or an accidental legend - identical bytes either
  way, so `unassessable`, never `mismatch` and never a pass.

What it DELIBERATELY does not flag
----------------------------------
Every one of these is a place where a confident verdict would be a guess, so it is reported as
`unassessable` (exit 3, "needs a live EVALUATE") or as a named non-flag - never silently dropped
and never counted as clean:

* **Fixed-window and pinned-cutoff date filters** (`DATESBETWEEN`, `DATESINPERIOD`, and
  `FILTER(ALL(t[c]), t[c] <= <constant>)`). Not accumulations: the window is anchored by arguments,
  not by the visual's grain, so there is nothing for an axis to disagree with. This is the only
  class dropped in silence, and deliberately: it is not a running total, so listing it would be the
  same noise as listing every other non-cumulative measure in the model.
* **An unresolvable as-of bound** - a measure, a what-if parameter, a foreign date column, an
  `ALLEXCEPT`, a removal naming exactly the compared column, or a bound built from both a pinned and
  a moving `VAR`. Any of them may be an as-of date; nothing proves it either way -> `unassessable`.
* **An as-of bound whose removal covers the compared column's whole table** - `MAXX(ALL(t), t[c])`,
  `REMOVEFILTERS(t)`, `ALLSELECTED(t)`, or a bare `REMOVEFILTERS()`. It evaluates to one global
  value on any axis, so it is a pinned cutoff wearing a `MAX` and is dropped with the fixed-window
  class above. A removal naming something else - `REMOVEFILTERS(t[Region])` - is NOT proof of
  pinning and does not drop the measure.
* **Two or more upper bounds on the same predicate, or a top-level `||`.** Which one the
  accumulation runs to cannot be read statically -> `unassessable`, never a silent drop.
* **A `<>`, `>=` or nested comparison.** Only a top-level `<`/`<=` is an as-of predicate. An
  exclusion filter is not an accumulation, and reading `<>` as `<` blocked one (measured). Its
  POSITION in an `&&` chain decides nothing.
* **An aggregated projection.** `MAX(t[c])` in a tooltip or a value slot does not group the query,
  so it is not an axis and cannot survive an `ALL`.
* **An explicit relation argument** to a window function. The relation then decides the ordering
  domain, not the visual, and a table expression cannot be resolved statically -> `unassessable`
  **for that call only**; a sibling call's mismatch still wins.
* **A cross-table as-of filter.** Whether a `'Date'[Month Start]` axis reaches the fact table
  depends on the relationship graph and cross-filter direction -> `unassessable`.
* **A period-partitioned running total** - the addressed date is grouping AND an uncleared coarser
  same-table grain is too. The accumulation runs and restarts per bucket; whether the restart was
  asked for is not in the artifacts -> `unassessable`.
* **A visual that projects no grouping column at all** (a card, a KPI, or one whose only column is
  aggregated) -> `unassessable`, for every shape. "The ordered column is not projected" is true
  there but says nothing about whether the single-row result is wrong.
* **A grouping column named like a date part but proven to be neither** (no date type, no lineage to
  the anchor) -> `unassessable`. A name is not evidence enough to fail a build, but it is too much
  to wave through.
* **A non-date grouping column on an as-of filter.** A running total partitioned by Region is an
  ordinary shape, not a defect -> reported as `ok` carrying `not_flagged`, so the decision is visible.
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
16 committed `examples/`: **no live axis mismatch exists**. The estate holds two real
`WINDOW(... ORDERBY(...))` measures (`HR Dashboard` -> `_Measures`: `Highlight Max`,
`% Highlight Max`) and neither is referenced anywhere in its report - not in `queryState`, not in a
filter, not in conditional formatting - so there is no binding to disagree with. Five more
"Running ..." measures are `BLANK()` stubs, two of them bound to a `pivotTable`. So this gate is
proven on the reproduced S14 fixture and on real engine bytes with a binding injected, and on real
estate data as shipped it is proven only to stay silent. That is a real limitation, not a clean bill
of health.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from bundle_corpus import shipping_reports

# Deliberate reuse over reimplementation. `_walk`/`_source_scope` carry a measured fix (`From` is a
# SIBLING of `queryState`, so a walk started inside it resolves every aliased projection to None);
# re-deriving that here would re-open the bug in a second place. Module-private by name, shared by
# intent - the same way `check_relationship_health` already borrows `check_field_bindings`'
# relationship components. `dax_grain` is this gate's own model-side half, split out at the seam
# where a module stops knowing anything about a report.
from check_field_bindings import FieldRef, _source_scope, _walk, model_for_report
from check_stub_measures import parse_model
from dax_grain import (
    GRAIN_UNRELATED,
    ColumnRef,
    Cumulative,
    ModelFacts,
    PeriodToDateCall,
    WindowCall,
    classify,
    read_model_facts,
)

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

# PRESENTATIONAL ONLY - the roles named in a finding's "Axis is ..." sentence. This list must NEVER
# gate whether a column is examined. It used to: a curated axis-role list omitted the pivotTable's
# real `Columns` role, and an empty result was then read as "the axis is cleared", so a `dateTime`
# grain under `Columns` passed silently (measured on the estate's Section 12 pivot: exit 1 under
# `Rows`, exit 0 under `Columns`, same measure, same column). Safety is now decided from EVERY
# projected column - `VisualBinding.columns()` - because every projected column groups the query.
AXIS_ROLES = ("Category", "Rows", "Columns", "X", "Series", "Group", "Details")


@dataclass
class VisualBinding:
    """One `visual.json` reduced to what this gate compares: its roles and their references.

    Two reference sets, deliberately, because they answer different questions:

    * `roles` - EVERY reference anywhere in the role, however deeply nested. "Does this visual bind
      the measure?" is a reachability question, so it must see a reference wrapped in an
      `Aggregation`, a `FillRule` or a `Subquery`.
    * `grouping` - only the references that GROUP the generated query: a projection whose `field`
      IS a `Column` or a `HierarchyLevel`. An aggregated projection does not group. Measured across
      the estate + `examples/`: 709 visuals, 1248 role bodies, **377** top-level `Aggregation`
      nodes, every one of them wrapping a `Column` - so reusing the generic walk here counted 377
      aggregated values as grouping columns and blocked correct reports (review finding 4).
    """

    file: Path
    visual: str
    visual_type: str
    roles: dict[str, list[FieldRef]]
    grouping: dict[str, list[FieldRef]] = dataclass_field(default_factory=dict)

    def columns(self) -> list[FieldRef]:
        """Every GROUPING column, in any role - the window relation's candidate ordering keys."""
        return [ref for refs in self.grouping.values() for ref in refs if ref.kind == "Column"]

    def axis_columns(self) -> list[FieldRef]:
        """Grouping columns in a role that puts them on the accumulation axis."""
        return [ref for role in AXIS_ROLES for ref in self.grouping.get(role, []) if ref.kind == "Column"]

    def measures(self) -> list[FieldRef]:
        """Every measure this visual binds, in any role and at any nesting depth."""
        return [ref for refs in self.roles.values() for ref in refs if ref.kind == "Measure"]

    def has_hierarchy(self) -> bool:
        """Whether any role GROUPS by a hierarchy level, which may expand to an unnamed column."""
        return any(ref.kind == "HierarchyLevel" for refs in self.grouping.values() for ref in refs)


# The only two `field` nodes that put a column in the query's GROUP BY. Everything else at the top
# of a projection - `Aggregation`, `Measure`, `NativeVisualCalculation`, `Arithmetic` - is a value
# computed per group. Measured shape of the corpus: `Column` 740, `Aggregation` 377, `Measure` 372,
# `NativeVisualCalculation` 6, and NO role body without a `projections` list, so nothing real is
# lost by reading projections rather than walking the role wholesale.
_GROUPING_NODES = ("Column", "HierarchyLevel")


def _grouping_refs(body: Any, scope: dict[str, str], path: Path) -> list[FieldRef]:
    """The references a role's projections put in the query's GROUP BY, and nothing else."""
    refs: list[FieldRef] = []
    projections = body.get("projections") if isinstance(body, dict) else None
    if not isinstance(projections, list):
        return refs
    for projection in projections:
        field = projection.get("field") if isinstance(projection, dict) else None
        if not isinstance(field, dict):
            continue
        for kind in _GROUPING_NODES:
            if isinstance(field.get(kind), dict):
                _walk({kind: field[kind]}, scope, path, refs)
                break
    return refs


def _read_visual(path: Path) -> VisualBinding | None:
    """One `visual.json` as a binding, or None when it declares no query to compare against."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    visual = payload.get("visual") if isinstance(payload, dict) else None
    if not isinstance(visual, dict):
        return None
    query = visual.get("query")
    state = query.get("queryState") if isinstance(query, dict) else None
    if not isinstance(state, dict):
        return None
    scope = _source_scope(query, {})
    roles: dict[str, list[FieldRef]] = {}
    grouping: dict[str, list[FieldRef]] = {}
    for role, body in state.items():
        refs: list[FieldRef] = []
        _walk(body, scope, path, refs)
        roles[role] = refs
        grouping[role] = _grouping_refs(body, scope, path)
    if not any(roles.values()):
        return None
    name = payload.get("name") if isinstance(payload.get("name"), str) else path.parent.name
    kind = visual.get("visualType") if isinstance(visual.get("visualType"), str) else "unknown"
    return VisualBinding(file=path, visual=name, visual_type=kind, roles=roles, grouping=grouping)


def iter_visuals(report_dir: Path) -> list[VisualBinding]:
    """Every `visual.json`'s query projections, kept PER ROLE and split by whether they group.

    `check_field_bindings.iter_visual_queries` flattens roles away because it only asks whether a
    reference resolves. Here the role is the finding: `Category` is the accumulation axis and
    `Y`/`Tooltips` are not, so the two cannot share a reader.
    """
    definition = report_dir / "definition"
    root = definition if definition.is_dir() else report_dir
    found = (_read_visual(path) for path in sorted(root.rglob("visual.json")))
    return [visual for visual in found if visual is not None]


def _verdict(kind: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    """One machine-readable judgement about a (measure, visual) pair."""
    return {"verdict": kind, "code": code, "detail": detail, **extra}


def _grouping_or_reason(visual: VisualBinding) -> tuple[list[FieldRef], dict[str, Any] | None]:
    """The columns this visual groups by, or the single reason no verdict can be formed from them.

    ONE place answers "was there a recognised grouping to reason about?", for all three shapes. The
    bug this replaces was structural: each judge inferred safety from its own empty list, so an
    unsupported role, a card and a hierarchy level all silently became "the axis is cleared".
    """
    grouping = visual.columns()
    if not grouping:
        return grouping, _verdict(
            "unassessable",
            "no_grouping_column",
            f"{visual.visual_type} projects no grouping column, so whether the accumulation "
            "degenerates depends on the query Power BI generates - probe it with EVALUATE",
        )
    return grouping, None


def _hierarchy_caveat(visual: VisualBinding) -> dict[str, Any] | None:
    """A hierarchy level may expand to a grain this gate cannot name, so no clean verdict is honest."""
    if not visual.has_hierarchy():
        return None
    return _verdict(
        "unassessable",
        "hierarchy_projection",
        "the visual projects a hierarchy level, which may expand to a grain this gate cannot resolve",
    )


def _axis_text(visual: VisualBinding) -> str:
    """The human-readable "Axis is ..." clause. Presentational; it never gates a verdict."""
    return ", ".join(f"'{r.entity}'[{r.prop}]" for r in visual.axis_columns()) or "(no axis role)"


def _judge_window(cumulative: Cumulative, visual: VisualBinding) -> dict[str, Any]:
    """Window family: judge EVERY window call independently, worst verdict wins."""
    return _worst([_judge_one_window(call, visual) for call in cumulative.window_calls])


def _judge_one_window(call: WindowCall, visual: VisualBinding) -> dict[str, Any]:
    """One window call: every ORDERBY column must be among the visual's own grouping columns.

    **The visual's grouping is established BEFORE any acquittal**, which is round 6 finding 3.
    `_judge_one_window` used to answer `ok`/`orders_by_visual_grain` for a call with no ORDERBY
    clause without ever asking whether the visual had a grain to order by - measured,
    `SUMX(OFFSET(-1), CALCULATE(SUM('Orders'[Sales])))` on a card that projects no grouping column
    at all exited **0**. With no visual grain there is nothing to establish the relation against.

    A no-ORDERBY call can no longer reach this function either way: `dax_grain`'s templates all
    require a `<spec>`, so `ordered_by` is never empty here. The empty case is still handled rather
    than asserted away, because an empty ordering key list would otherwise acquit vacuously.
    """
    grouping, blocked = _grouping_or_reason(visual)
    if blocked is not None:
        return blocked
    if not call.ordered_by:
        return _verdict(
            "unassessable",
            "no_ordering_key",
            f"the {call.func} call names no ordering column, so there is no grain to compare with "
            "this visual's - probe it with EVALUATE",
        )
    projected = {ColumnRef(ref.entity, ref.prop).key() for ref in grouping}
    absent = [ref for ref in call.ordered_by if ref.key() not in projected]
    if not absent:
        return _verdict("ok", "orderby_projected", "every ORDERBY column is projected by this visual")
    caveat = _hierarchy_caveat(visual)
    if caveat is not None:
        return caveat
    return _verdict(
        "mismatch",
        "orderby_not_on_axis",
        "ordered by "
        + ", ".join(ref.qualified() for ref in absent)
        + ", which this visual does not project; the window degenerates to each bucket's own value. "
        + "Axis is "
        + _axis_text(visual),
        ordered_by=[ref.qualified() for ref in call.ordered_by],
        projected=[f"'{r.entity}'[{r.prop}]" for r in grouping],
    )


_VERDICT_PRECEDENCE = ("mismatch", "unassessable", "ok")


def _worst(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """**Worst verdict wins, never the first.**

    Rounds 1-4 produced the same bug at eight sites - the first window call, the first qualifying
    `FILTER`, the first period-to-date function, the first reader in the dispatch chain, the first
    `VAR` a bound references, the first comparison in an `&&` chain, the first `ALL(...)` in a
    bound, and an unreadable window call suppressing a readable one. Three were SILENT PASSES.
    Every mechanism is a list of independent calls and every list is folded here.

    Round 5 shortened the list of things there are to fold - the as-of bound classifier is gone -
    without weakening the rule: a measure may still carry several window calls, several
    period-to-date calls, and several disclosed as-of restrictions at once.
    """
    if not verdicts:
        return _verdict("unassessable", "unreadable_grain", "the accumulation grain could not be read from the DAX")
    for kind in _VERDICT_PRECEDENCE:
        for verdict in verdicts:
            if verdict["verdict"] == kind:
                return {**verdict, "judged_calls": len(verdicts)} if len(verdicts) > 1 else verdict
    return _verdict("unassessable", "unreadable_grain", "the accumulation grain could not be read from the DAX")


def _judge_period_to_date(cumulative: Cumulative, visual: VisualBinding, facts: ModelFacts) -> dict[str, Any]:
    """Period-to-date: judge EVERY `TOTALYTD`/`DATESYTD` call independently, worst verdict wins."""
    return _worst([_judge_one_period_to_date(call, visual, facts) for call in cumulative.period_calls])


def _judge_one_period_to_date(call: PeriodToDateCall, visual: VisualBinding, facts: ModelFacts) -> dict[str, Any]:
    """One period-to-date call: safe ONLY when every date grain on the visual sits on ITS marked
    date table.

    Time intelligence auto-removes filters from the OTHER columns of a table marked
    `dataCategory: Time`, which is what makes `TOTALYTD` correct on a month axis. Nothing removes a
    date grain that lives on an unmarked table - a fact table, typically - so that case is exactly
    the trap. It is reported `unassessable` rather than `mismatch` because the auto-removal rules
    have more inputs than this gate reads; what is NOT acceptable is calling it a pass.
    """
    anchor = call.anchor
    grouping, blocked = _grouping_or_reason(visual)
    if blocked is not None:
        return blocked
    caveat = _hierarchy_caveat(visual)
    if caveat is not None:
        return caveat
    grains = [
        ref
        for ref in grouping
        if facts.grain_of(ColumnRef(ref.entity, ref.prop), anchor) != GRAIN_UNRELATED
        and ColumnRef(ref.entity, ref.prop).key() != anchor.key()
    ]
    if not grains:
        return _verdict(
            "ok",
            "no_date_grain_on_axis",
            f"no projected column is a date grain of {anchor.qualified()}, so nothing coarser survives",
        )
    unmarked = [
        ref
        for ref in grains
        if ref.entity.casefold() != (anchor.table or "").casefold() or not facts.is_time_table(ref.entity)
    ]
    if not unmarked:
        return _verdict(
            "ok",
            "date_table_marked",
            f"every date grain sits on '{anchor.table}', which is marked dataCategory: Time, so time "
            "intelligence removes those filters",
        )
    return _verdict(
        "unassessable",
        "period_to_date_grain_unproven",
        "date grain(s) "
        + ", ".join(f"'{r.entity}'[{r.prop}]" for r in unmarked)
        + f" are not on a table marked dataCategory: Time together with {anchor.qualified()}, so "
        "nothing here proves their filter is removed and the period-to-date value may be the "
        "bucket's own total - probe it with EVALUATE",
        anchor=anchor.qualified(),
        marked_date_tables=sorted(facts.time_tables),
    )


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
            "reason": "no running-total, window or period-to-date measure in this model, so no grain "
            "can disagree with an axis",
            "findings": [],
        }
    facts = read_model_facts(model_dir)
    visuals = iter_visuals(report_dir)
    findings, bound = _grade_visuals(visuals, cumulatives, facts)
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
    facts: ModelFacts,
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
                    **_judge(cumulative, visual, facts),
                }
            )
    return findings, bound


def _judge(cumulative: Cumulative, visual: VisualBinding, facts: ModelFacts) -> dict[str, Any]:
    """Grade one pair against every invariant its DAX declares, and let the worst verdict win.

    This used to route to a single invariant, because `classify` used to return after the first
    reader that matched. A measure can genuinely declare more than one mechanism, and when it did,
    the unread half was invisible - measured, a correct `WINDOW(... ORDERBY(...))` beside a
    defective as-of on another date column exited **0** where that as-of alone exits 1.
    """
    if not cumulative.assessable:
        return _verdict("unassessable", cumulative.shape, cumulative.reason)
    verdicts = []
    if cumulative.window_calls:
        verdicts.append(_judge_window(cumulative, visual))
    if cumulative.period_calls:
        verdicts.append(_judge_period_to_date(cumulative, visual, facts))
    return _enforce_consumption(cumulative, _worst(verdicts))


def _enforce_consumption(cumulative: Cumulative, verdict: dict[str, Any]) -> dict[str, Any]:
    """**No definite verdict on input the tokeniser did not fully consume.** The one hard rule.

    Rounds 1-6 of blind review each found the same defect in a new spelling, and the common shape
    was always a verdict formed from a FRAGMENT: a call site found by searching text, a clause read
    without checking what surrounded it, an expression judged on the part that was recognised while
    the rest went unread. Measured round 6: `VAR _dead = <a WINDOW> VAR _answer = SUM('Orders'
    [Sales]) RETURN _answer` reported MISMATCH (exit 1) although the returned value is a plain sum.

    `dax_grain` answers this at the source - `Cumulative.consumed` is set only when one whole
    template matched from the first token to the last - and this function is the enforcement, sited
    where the verdict is actually emitted so that no future reader can route around it. A gate that
    cannot say what it read has not established anything, and `unassessable` is what that is called.
    """
    if verdict["verdict"] in ("ok", "mismatch") and not cumulative.consumed:
        return _verdict(
            "unassessable",
            "unconsumed_expression",
            f"{cumulative.label} was graded without a whole-expression template match, so the "
            "verdict would rest on a fragment of its DAX - probe it with EVALUATE at the axis grain",
        )
    return verdict


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

    An affirmative verdict has to say what it did NOT look at, or it reads as a full clearance -
    which is why an unassessed pair, an unresolved model and an unbound cumulative measure all print
    here rather than only on a failure.

    There is deliberately no "not assessed by design" list any more. Period-to-date measures used to
    be listed here and excluded from judgement, and a blind review proved the bucket was hiding real
    cases rather than disclosing them: a fact-table `TOTALYTD` on a coarser same-table axis exited 0
    with its name printed under that heading. Naming something you refused to check is not the same
    as checking it. They are now judged, and land in `unassessable` when safety cannot be proven.
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
    return lines


def render(report: dict[str, Any]) -> str:
    """Human-readable verdict, in the shape the sibling gates use."""
    status = report.get("status")
    head = f"RUNNING-TOTAL AXIS CHECK: {status}"
    if status == STATUS_NOT_APPLICABLE:
        first = (
            f"{head} - no running-total, window or period-to-date measure in any shipping model; "
            "nothing can disagree with an axis."
        )
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
