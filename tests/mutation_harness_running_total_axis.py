"""Mutation harness for the running-total axis gate (#218): prove its tests can actually fail.

    python tests/mutation_harness_running_total_axis.py

Not named ``test_*``, so pytest does not collect it -- it *drives* pytest. Each mutation breaks ONE
behaviour of ``scripts/check_running_total_axis.py`` or ``scripts/dax_grain.py`` by monkeypatching
it at interpreter start (injected as a pytest plugin), then re-runs the gate's own suite. A mutation
that produces a GREEN run is a HOLE in the tests, not a success.

Three false-green traps are guarded explicitly, all three measured in this repo:

1. **A plugin that fails to import** makes pytest exit non-zero before running a single test, and a
   naive harness scores that as CAUGHT. ``tests/mutation_harness.py`` reported 22/22 on exactly that
   bug. Guarded by refusing any run whose output mentions an import error, and by requiring pytest
   to have produced output at all -- a crash exits non-zero exactly like a test failure.
2. **A mutation that never applied.** A text-substitution harness elsewhere silently matched nothing
   on CRLF input and "passed" against unmutated code. Guarded harder than by inspection: every
   snippet appends its own name to a sentinel file **after** the patch, and additionally asserts the
   patched object actually changed identity. No sentinel line, no verdict - the run is an ERROR.
3. **A mutation that applied but never did what the table claims** -- the vacuity mode blind review
   found in round 2, and the subtlest of the three. The finding-2 mutation still referenced
   ``crta._DATE_TYPES`` after that symbol moved to ``dax_grain``; the patch applied, the identity
   assertion passed, the patched function then raised ``AttributeError`` on every call, and pytest
   reported the *expected* test as FAILED. A crash inside the mutant is indistinguishable from a
   catch by outcome alone. Guarded by a **plugin-time probe**: every mutation must ship a snippet
   that CALLS the patched object and asserts a result the unmutated code does not produce. The
   probe writes a second sentinel line; no probe line, no verdict.

Scoring rule, stated because the sibling harness got it wrong
--------------------------------------------------------------
Blind review of PR #405 found that ``tests/mutation_harness.py`` scored ``CAUGHT`` for *any*
non-zero pytest exit, including a collection error (4) or an interrupt (2) where **no test ran**.
This harness never does that. A mutation is ``CAUGHT`` only when the **named** test the table
claims for it appears in pytest's ``FAILED`` lines:

* non-zero exit with **no** named failure  -> ``ERROR (no test outcome)``, and the run fails;
* non-zero exit naming a **different** test -> reported as misattributed, and the run fails;
* an ``ERROR path::Thing`` line is a **collection** failure, not a catch, and is never read as one -
  only ``FAILED`` lines are scanned. A dying ``xdist`` worker is the mirror image: it emits
  ``FAILED path::test_name`` for a test that never ran, so its own markers are refused outright;
* the baseline must be **green first**, or nothing is run at all - so an already-failing test cannot
  be credited to every mutation after it.

Read the PR for #218 for the result table.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
TARGET = "tests/test_check_running_total_axis.py"
SENTINEL = ROOT / "tests" / "_mutation_applied.txt"

# Markers that mean pytest itself fell over rather than reporting a test outcome. `xdist` is listed
# because a dying worker prints `FAILED path::test_name` for a test that never ran, which is the one
# failure line shape this harness DOES trust.
_BROKEN_RUN_MARKERS = (
    "Error importing plugin",
    "INTERNALERROR",
    "crashed while running",
    "Replacing crashed worker",
    "node down",
)

# name -> (patch source, the test expected to catch it). The expectation is checked, not trusted:
# a mutation caught by a DIFFERENT test is reported as such, because that usually means the test
# named in the table is not the one carrying the behaviour.
MUTATIONS: dict[str, tuple[str, str]] = {
    "orderby-never-checked": (
        """
_orig = crta._judge_window
crta._judge_window = lambda c, v: crta._verdict("ok", "orderby_projected", "mutated")
assert crta._judge_window is not _orig
""",
        "test_s14_measured_table",
    ),
    "explicit-relation-not-detected": (
        """
_orig = dg._is_positional
dg._is_positional = lambda arg: True
assert dg._is_positional is not _orig
""",
        "test_explicit_relation_is_unassessable_not_a_mismatch",
    ),
    "measure-only-visual-flagged": (
        """
_orig = crta.VisualBinding.columns
crta.VisualBinding.columns = lambda self: _orig(self) or [crta.FieldRef("Column", "X", "Y", self.file)]
assert crta.VisualBinding.columns is not _orig
""",
        "test_measure_only_visual_is_unassessable",
    ),
    "hierarchy-projection-ignored": (
        """
_orig = crta.VisualBinding.has_hierarchy
crta.VisualBinding.has_hierarchy = lambda self: False
assert crta.VisualBinding.has_hierarchy is not _orig
""",
        "test_hierarchy_projection_is_unassessable",
    ),
    "every-column-treated-as-a-date": (
        """
_orig = dg.ModelFacts.grain_of
dg.ModelFacts.grain_of = lambda self, ref, anchor: dg.GRAIN_DATE
assert dg.ModelFacts.grain_of is not _orig
""",
        "test_as_of_non_date_axis_is_deliberately_not_flagged",
    ),
    "cleared-columns-dropped": (
        """
_orig = dg._classify_as_of
def as_of(expr, base):
    out = _orig(expr, base)
    if out is not None:
        for call in out.as_of_calls:
            call.cleared_columns = []
            call.cleared_tables = []
    return out
dg._classify_as_of = as_of
assert dg._classify_as_of is not _orig
""",
        "test_as_of_clearing_the_coarser_column_too_is_clean",
    ),
    "compared-column-not-treated-as-cleared": (
        """
_orig = dg.ColumnRef.key
dg.ColumnRef.key = lambda self: ((self.table or "").casefold(), self.column.casefold(), id(self))
assert dg.ColumnRef.key is not _orig
""",
        "test_as_of_axis_is_the_compared_column",
    ),
    "every-blank-stub-claimed": (
        """
_orig = dg._classify_stub
def stub(member, base):
    base.shape = "stub"
    base.assessable = False
    base.reason = "mutated"
    return base
dg._classify_stub = stub
assert dg._classify_stub is not _orig
""",
        "test_an_ordinary_blank_stub_is_not_surfaced",
    ),
    "running-total-stub-not-surfaced": (
        """
_orig = dg._classify_stub
dg._classify_stub = lambda member, base: None
assert dg._classify_stub is not _orig
""",
        "test_running_total_stub_bound_to_a_visual_is_unassessable",
    ),
    "period-to-date-judgement-skipped": (
        """
_orig = crta._judge_period_to_date
crta._judge_period_to_date = lambda cumulative, visual, facts: crta._verdict("ok", "period_to_date", "mutated")
assert crta._judge_period_to_date is not _orig
""",
        "test_fact_table_period_to_date_on_a_coarse_axis_is_unassessable",
    ),
    "fixed-window-datesbetween-classified-as-accumulation": (
        """
_orig = dict(dg._PERIOD_TO_DATE_FUNCTIONS)
dg._PERIOD_TO_DATE_FUNCTIONS["DATESBETWEEN"] = 0
assert "DATESBETWEEN" in dg._PERIOD_TO_DATE_FUNCTIONS and "DATESBETWEEN" not in _orig
""",
        "test_a_fixed_window_comparison_is_still_not_an_accumulation",
    ),
    "unassessed-pair-masked-by-a-clean-one": (
        """
_orig = crta.merge
def merge(pairs, unresolved, root):
    out = _orig(pairs, unresolved, root)
    if out["status"] == crta.STATUS_UNASSESSABLE and out["assessed_clean"] and not out["unassessable"]:
        out["status"] = crta.STATUS_OK
    return out
crta.merge = merge
assert crta.merge is not _orig
""",
        "test_a_clean_pair_cannot_mask_an_unassessed_one",
    ),
    "unresolved-model-ignored": (
        """
_orig = crta.scan
def scan(root, model_override=None):
    out = _orig(root, model_override)
    out["reports_without_model"] = []
    if out["status"] == crta.STATUS_UNASSESSABLE and not out["unassessable"]:
        out["status"] = crta.STATUS_NOT_APPLICABLE
    return out
crta.scan = scan
assert crta.scan is not _orig
""",
        "test_report_without_a_model_is_unassessable",
    ),
    "unassessable-exits-zero": (
        """
_orig = crta._exit_code
crta._exit_code = lambda report, args: (
    crta.EXIT_MISMATCH if report["status"] == crta.STATUS_MISMATCH else crta.EXIT_OK)
assert crta._exit_code is not _orig
""",
        "test_unassessable_exit_is_three_and_strict_promotes_it",
    ),
    "json-never-written": (
        """
_orig = crta.main
def main(argv=None):
    args = crta._parse_args(argv)
    args.json = None
    return _orig([a for a in (argv or []) if not a.endswith(".json") and a != "--json"])
crta.main = main
assert crta.main is not _orig
""",
        "test_exit_codes_and_json_written_before_rendering",
    ),
    "unread-model-reads-as-not-applicable": (
        """
_orig = crta._tmdl_documents
crta._tmdl_documents = lambda model_dir: 1
assert crta._tmdl_documents is not _orig
""",
        "test_model_with_no_measures_is_not_applicable_but_an_unread_model_is_skipped",
    ),
    "window-relation-narrowed-to-the-axis": (
        """
_orig = crta.VisualBinding.columns
crta.VisualBinding.columns = lambda self: self.axis_columns()
assert crta.VisualBinding.columns is not _orig
""",
        "test_ordered_column_in_a_non_axis_role_still_acquits",
    ),
    "argument-splitting-is-naive": (
        """
_orig = dg._split_arguments
dg._split_arguments = lambda text: [part.strip() for part in text.split(",")]
assert dg._split_arguments is not _orig
""",
        "test_split_arguments_respects_nesting_and_strings",
    ),
    "column-matching-is-case-sensitive": (
        """
_orig = dg.ColumnRef.key
dg.ColumnRef.key = lambda self: (self.table or "", self.column)
assert dg.ColumnRef.key is not _orig
""",
        "test_case_only_difference_does_not_invent_a_mismatch",
    ),
    "gate-unwired-from-check-unit": (
        """
import check_unit
check_unit.GATES = tuple(g for g in check_unit.GATES if g.check_id != "running-total-axis")
assert all(g.check_id != "running-total-axis" for g in check_unit.GATES)
""",
        "test_gate_is_wired_into_check_unit",
    ),
    "partitionby-treated-as-an-ordering-key": (
        """
_orig = dg._classify_window
def window(expr, base):
    out = _orig(expr, base)
    if out is not None and out.partition_by:
        out.ordered_by = out.ordered_by + out.partition_by + [dg.ColumnRef("Nowhere", "Nothing")]
    return out
dg._classify_window = window
assert dg._classify_window is not _orig
""",
        "test_partitionby_legend_is_not_a_mismatch",
    ),
    # --- mutations that reinstate each of the five review findings ---------------------------
    "f1-axis-role-list-decides-safety": (
        """
_orig = crta.VisualBinding.columns
crta.VisualBinding.columns = lambda self: self.axis_columns() if self.roles.get("Columns") else _orig(self)
crta.AXIS_ROLES = ("Category", "Rows", "X")
assert crta.AXIS_ROLES == ("Category", "Rows", "X")
""",
        "test_every_projected_column_is_examined_not_a_curated_axis_list",
    ),
    "f1-empty-projection-reads-as-cleared": (
        """
_orig = crta._grouping_or_reason
crta._grouping_or_reason = lambda visual: (visual.columns(), None)
assert crta._grouping_or_reason is not _orig
""",
        "test_as_of_on_a_measure_only_visual_is_unassessable",
    ),
    "f1-hierarchy-ignored-in-as-of": (
        """
_orig = crta._hierarchy_caveat
crta._hierarchy_caveat = lambda visual: None
assert crta._hierarchy_caveat is not _orig
""",
        "test_as_of_with_a_hierarchy_projection_is_unassessable",
    ),
    "f2-declared-type-is-the-only-grain-signal": (
        """
_orig = dg.ModelFacts.grain_of
def grain_of(self, ref, anchor):
    if self.data_type(ref.table or "", ref.column) in dg._DATE_TYPES:
        return dg.GRAIN_DATE
    return dg.GRAIN_UNRELATED
dg.ModelFacts.grain_of = grain_of
assert dg.ModelFacts.grain_of is not _orig
""",
        "test_date_bins_derived_by_calculation_are_flagged_whatever_their_type",
    ),
    "f2-lineage-not-followed-transitively": (
        """
_orig = dg.ModelFacts.derives_from
def derives_from(self, ref, anchor):
    expression = self.calc_expressions.get(ref.key())
    if not expression:
        return False
    return any(dg.ColumnRef(f.table or ref.table, f.column).key() == anchor.key()
               for f in dg._column_refs(expression))
dg.ModelFacts.derives_from = derives_from
assert dg.ModelFacts.derives_from is not _orig
""",
        "test_date_bins_derived_by_calculation_are_flagged_whatever_their_type",
    ),
    "f2-date-named-column-waved-through": (
        """
_orig = dg.ModelFacts.grain_of
def grain_of(self, ref, anchor):
    out = _orig(self, ref, anchor)
    return dg.GRAIN_UNRELATED if out == dg.GRAIN_SUSPECT else out
dg.ModelFacts.grain_of = grain_of
assert dg.ModelFacts.grain_of is not _orig
""",
        "test_a_date_named_column_with_no_proof_is_unassessable_not_clean",
    ),
    "f3-any-less-than-is-a-running-total": (
        """
_orig = dg._classify_bound
dg._classify_bound = lambda bound, compared, variables, depth=0: dg.BOUND_CONTEXT
assert dg._classify_bound is not _orig
""",
        "test_a_pinned_cutoff_is_not_a_running_total",
    ),
    "f3-var-bound-never-followed": (
        """
_orig = dg._resolve_vars
dg._resolve_vars = lambda expr: {}
assert dg._resolve_vars is not _orig
""",
        "test_an_as_of_bound_hoisted_into_a_var_is_still_a_running_total",
    ),
    "f4-only-the-first-window-call": (
        """
_orig = dg._window_call_sites
dg._window_call_sites = lambda expr: _orig(expr)[:1]
assert dg._window_call_sites is not _orig
""",
        "test_every_window_call_is_assessed_not_just_the_first",
    ),
    "f5-period-to-date-excused-again": (
        """
_orig = dg._classify_period_to_date
dg._classify_period_to_date = lambda expr, base: None
assert dg._classify_period_to_date is not _orig
""",
        "test_fact_table_period_to_date_on_a_coarse_axis_is_unassessable",
    ),
    "f5-every-table-reads-as-a-marked-date-table": (
        """
_orig = dg.ModelFacts.is_time_table
dg.ModelFacts.is_time_table = lambda self, table: True
assert dg.ModelFacts.is_time_table is not _orig
""",
        "test_fact_table_period_to_date_on_a_coarse_axis_is_unassessable",
    ),
    "crash-grading-keyed-by-an-unhashable-fieldref": (
        """
_orig = crta._judge_as_of
def judge(cumulative, visual, facts):
    {ref: 1 for ref in visual.columns()}
    return _orig(cumulative, visual, facts)
crta._judge_as_of = judge
assert crta._judge_as_of is not _orig
""",
        "test_a_grouping_column_is_never_used_as_a_dict_key",
    ),
    # --- mutations that reinstate each of the six ROUND-2 review findings ----------------------
    "r1-any-max-bound-moves-with-the-visual": (
        """
_orig = dg._classify_moving_bound
dg._classify_moving_bound = lambda text, compared: dg.BOUND_CONTEXT
assert dg._classify_moving_bound is not _orig
""",
        "test_an_as_of_bound_that_is_not_proven_to_move_is_unassessable",
    ),
    "r1-context-removal-in-the-bound-ignored": (
        """
import re
_orig = dg._CONTEXT_REMOVAL_RE
dg._CONTEXT_REMOVAL_RE = re.compile(r"(?!x)x")
assert dg._CONTEXT_REMOVAL_RE is not _orig
""",
        "test_an_as_of_bound_that_removes_context_is_not_an_accumulation",
    ),
    "r2-not-equal-parsed-as-less-than": (
        """
import re
_orig = dg._top_level_comparison
def comparison(text):
    match = re.search(r"(.+?)(<=|<)(?!=)(.*)", text, re.DOTALL)
    return (match.group(1), match.group(2), match.group(3)) if match else None
dg._top_level_comparison = comparison
assert dg._top_level_comparison is not _orig
""",
        "test_only_a_top_level_less_than_is_an_as_of_predicate",
    ),
    "r3-only-the-first-as-of-call": (
        """
_orig = dg._classify_as_of
def as_of(expr, base):
    out = _orig(expr, base)
    if out is not None and out.as_of_calls:
        out.as_of_calls = out.as_of_calls[:1]
    return out
dg._classify_as_of = as_of
assert dg._classify_as_of is not _orig
""",
        "test_a_safe_as_of_call_cannot_excuse_a_defective_one_in_the_same_measure",
    ),
    "r3-cleared-sets-unioned-across-calls": (
        """
_orig = dg._classify_as_of
def as_of(expr, base):
    out = _orig(expr, base)
    if out is not None and len(out.as_of_calls) > 1:
        columns = [c for call in out.as_of_calls for c in call.cleared_columns]
        tables = [t for call in out.as_of_calls for t in call.cleared_tables]
        for call in out.as_of_calls:
            call.cleared_columns = list(columns)
            call.cleared_tables = list(tables)
    return out
dg._classify_as_of = as_of
assert dg._classify_as_of is not _orig
""",
        "test_a_safe_as_of_call_cannot_excuse_a_defective_one_in_the_same_measure",
    ),
    "r4-aggregated-columns-group-the-query": (
        """
_orig = crta._grouping_refs
def grouping(body, scope, path):
    refs = []
    crta._walk(body, scope, path, refs)
    return [r for r in refs if r.kind in crta._GROUPING_NODES]
crta._grouping_refs = grouping
assert crta._grouping_refs is not _orig
""",
        "test_an_aggregated_projection_does_not_group_the_query",
    ),
    "r5-a-partition-beside-the-anchor-is-a-mismatch": (
        """
_orig = crta._judge_same_table_survivors
def survivors(items, compared, call, facts, axis, anchor_projected):
    return _orig(items, compared, call, facts, axis, False)
crta._judge_same_table_survivors = survivors
assert crta._judge_same_table_survivors is not _orig
""",
        "test_a_partition_beside_the_addressed_date_is_unassessable_not_a_mismatch",
    ),
    "r5-every-same-table-survivor-reads-as-clean": (
        """
_orig = crta._judge_same_table_survivors
crta._judge_same_table_survivors = lambda items, compared, call, facts, axis, anchor_projected: None
assert crta._judge_same_table_survivors is not _orig
""",
        "test_as_of_coarser_same_table_date_axis_is_a_mismatch",
    ),
    "r6-a-mutation-may-name-a-dead-symbol": (
        """
import mutation_harness_running_total_axis as harness
harness.MUTATIONS["probe-of-the-guard"] = ("crta._DATE_TYPES\\n", "test_that_does_not_exist")
harness.PROBES["probe-of-the-guard"] = "pass"
assert "probe-of-the-guard" in harness.MUTATIONS
""",
        "test_every_mutation_in_the_harness_names_a_symbol_that_still_exists",
    ),
    "r6-a-mutation-may-ship-without-a-probe": (
        """
import mutation_harness_running_total_axis as harness
harness.MUTATIONS["unprobed"] = ("pass\\n", "test_that_does_not_exist")
assert "unprobed" in harness.MUTATIONS and "unprobed" not in harness.PROBES
""",
        "test_every_mutation_declares_a_probe_that_proves_it_executes",
    ),
}

# name -> a snippet that CALLS the patched object and asserts a result the unmutated code does not
# produce. This is the guard for review finding 6: a mutation that crashes on a stale symbol raises
# `AttributeError` inside the mutant, pytest reports the expected test as FAILED, and the harness
# scores a catch it never earned. Every probe here would have raised instead - loudly, and before
# any verdict was written. Probes run at plugin import, after the patch, in the child interpreter.
PROBES: dict[str, str] = {
    "orderby-never-checked": """
assert crta._judge_window(kit.cumulative(ordered_by=[kit.ANCHOR]), kit.visual())["code"] == "orderby_projected"
""",
    "explicit-relation-not-detected": """
assert dg._is_positional("'Orders'") is True
""",
    "measure-only-visual-flagged": """
assert crta.VisualBinding.columns(kit.visual()) != []
""",
    "hierarchy-projection-ignored": """
level = {"Category": [kit.field_ref("Date", "Month", "HierarchyLevel")]}
assert crta.VisualBinding.has_hierarchy(kit.visual(grouping=level)) is False
""",
    "every-column-treated-as-a-date": """
assert kit.facts().grain_of(dg.ColumnRef("Orders", "Region"), kit.ANCHOR) == dg.GRAIN_DATE
""",
    "cleared-columns-dropped": """
out = dg._classify_as_of(kit.AS_OF, kit.cumulative())
assert out is not None and all(not c.cleared_columns and not c.cleared_tables for c in out.as_of_calls)
""",
    "compared-column-not-treated-as-cleared": """
first = dg.ColumnRef("Orders", "Order_Date")
second = dg.ColumnRef("Orders", "Order_Date")
assert first.key() != second.key() and len(first.key()) == 3
""",
    "every-blank-stub-claimed": """
out = dg._classify_stub(object(), kit.cumulative())
assert out is not None and out.reason == "mutated" and out.assessable is False
""",
    "running-total-stub-not-surfaced": """
assert dg._classify_stub(object(), kit.cumulative()) is None
""",
    "period-to-date-judgement-skipped": """
verdict = crta._judge_period_to_date(kit.cumulative(compared=kit.ANCHOR), kit.visual(), kit.facts())
assert verdict["verdict"] == "ok" and verdict["code"] == "period_to_date"
""",
    "fixed-window-datesbetween-classified-as-accumulation": """
expr = "CALCULATE(SUM('Orders'[Sales]), DATESBETWEEN('Date'[Date], MIN('Date'[Date]), MAX('Date'[Date])))"
out = dg._classify_period_to_date(expr, kit.cumulative())
assert out is not None and out.shape == "period_to_date"
""",
    "unassessed-pair-masked-by-a-clean-one": """
pairs = [
    {"status": crta.STATUS_OK, "report": "a", "findings": [{"verdict": "ok"}]},
    {"status": crta.STATUS_SKIPPED, "report": "b", "reason": "unread", "findings": []},
]
assert crta.merge(pairs, [], "root")["status"] == crta.STATUS_OK
""",
    "unresolved-model-ignored": """
from pathlib import Path
_shipping = crta.shipping_reports
crta.shipping_reports = lambda root: [Path("nowhere") / "X.Report"]
try:
    out = crta.scan(Path("."))
finally:
    crta.shipping_reports = _shipping
assert out["reports_without_model"] == [] and out["status"] == crta.STATUS_NOT_APPLICABLE
""",
    "unassessable-exits-zero": """
import argparse
args = argparse.Namespace(warn_only=False, strict=False)
assert crta._exit_code({"status": crta.STATUS_UNASSESSABLE}, args) == crta.EXIT_OK
""",
    "json-never-written": """
import tempfile
from pathlib import Path
scratch = Path(tempfile.mkdtemp())
target = scratch / "verdict.json"
crta.main(["--json", str(target), "--quiet", str(scratch)])
assert not target.exists()
""",
    "unread-model-reads-as-not-applicable": """
from pathlib import Path
assert crta._tmdl_documents(Path("nowhere-at-all")) == 1
""",
    "window-relation-narrowed-to-the-axis": """
tooltip = {"Tooltips": [kit.field_ref("Orders", "Order_Date")]}
assert crta.VisualBinding.columns(kit.visual(grouping=tooltip)) == []
""",
    "argument-splitting-is-naive": """
assert dg._split_arguments("ORDERBY('T'[A], ASC)") == ["ORDERBY('T'[A]", "ASC)"]
""",
    "column-matching-is-case-sensitive": """
assert dg.ColumnRef("Orders", "Order_Date").key() == ("Orders", "Order_Date")
""",
    "gate-unwired-from-check-unit": """
import check_unit
assert all(g.check_id != "running-total-axis" for g in check_unit.GATES)
""",
    "partitionby-treated-as-an-ordering-key": """
out = dg._classify_window(kit.PARTITIONED_WINDOW, kit.cumulative())
assert out is not None and dg.ColumnRef("Nowhere", "Nothing") in out.ordered_by
""",
    "f1-axis-role-list-decides-safety": """
assert crta.AXIS_ROLES == ("Category", "Rows", "X")
assert crta.VisualBinding.columns(kit.visual(grouping={"Columns": [kit.field_ref("Orders", "Order_Month")]})) == []
""",
    "f1-empty-projection-reads-as-cleared": """
assert crta._grouping_or_reason(kit.visual()) == ([], None)
""",
    "f1-hierarchy-ignored-in-as-of": """
level = {"Category": [kit.field_ref("Date", "Month", "HierarchyLevel")]}
assert crta._hierarchy_caveat(kit.visual(grouping=level)) is None
""",
    "f2-declared-type-is-the-only-grain-signal": """
assert kit.facts().grain_of(kit.DERIVED, kit.ANCHOR) == dg.GRAIN_UNRELATED
""",
    "f2-lineage-not-followed-transitively": """
assert kit.facts().derives_from(dg.ColumnRef("Orders", "Order Quarter"), kit.ANCHOR) is False
assert kit.facts().derives_from(kit.DERIVED, kit.ANCHOR) is True
""",
    "f2-date-named-column-waved-through": """
assert kit.facts().grain_of(dg.ColumnRef("Orders", "Fiscal Period"), kit.ANCHOR) == dg.GRAIN_UNRELATED
""",
    "f3-any-less-than-is-a-running-total": """
assert dg._classify_bound("DATE(2024, 12, 31)", kit.ANCHOR, {}) == dg.BOUND_CONTEXT
""",
    "f3-var-bound-never-followed": """
assert dg._resolve_vars("VAR _asOf = MAX('Orders'[Order_Date]) RETURN 1") == {}
""",
    "f4-only-the-first-window-call": """
assert len(dg._window_call_sites(kit.TWO_WINDOWS)) == 1
""",
    "f5-period-to-date-excused-again": """
assert dg._classify_period_to_date("TOTALYTD(SUM('Orders'[Sales]), 'Date'[Date])", kit.cumulative()) is None
""",
    "f5-every-table-reads-as-a-marked-date-table": """
assert dg.ModelFacts().is_time_table("no such table") is True
""",
    "crash-grading-keyed-by-an-unhashable-fieldref": """
bound = kit.visual(grouping={"Category": [kit.field_ref("Orders", "Order_Month")]})
try:
    crta._judge_as_of(kit.cumulative(as_of_calls=[kit.as_of_call()]), bound, kit.facts())
except TypeError:
    pass
else:
    raise AssertionError("the unhashable-key mutation did not raise TypeError")
""",
    "r1-any-max-bound-moves-with-the-visual": """
assert dg._classify_bound("MAX('Date'[Date])", kit.ANCHOR, {}) == dg.BOUND_CONTEXT
assert dg._classify_bound("MAXX(ALL('Orders'), 'Orders'[Order_Date])", kit.ANCHOR, {}) == dg.BOUND_CONTEXT
""",
    "r1-context-removal-in-the-bound-ignored": """
assert dg._classify_bound("MAXX(ALL('Orders'), 'Orders'[Order_Date])", kit.ANCHOR, {}) == dg.BOUND_CONTEXT
""",
    "r2-not-equal-parsed-as-less-than": """
assert dg._top_level_comparison("'Orders'[Order_Date] <> MAX('Orders'[Order_Date])")[1] == "<"
""",
    "r3-only-the-first-as-of-call": """
out = dg._classify_as_of(kit.TWO_AS_OF_CALLS, kit.cumulative())
assert out is not None and len(out.as_of_calls) == 1
""",
    "r3-cleared-sets-unioned-across-calls": """
out = dg._classify_as_of(kit.TWO_AS_OF_CALLS, kit.cumulative())
assert out is not None and len(out.as_of_calls) == 2
assert all(kit.COARSE.key() in {c.key() for c in call.cleared_columns} for call in out.as_of_calls)
""",
    "r4-aggregated-columns-group-the-query": """
from pathlib import Path
refs = crta._grouping_refs(kit.aggregated_role("Orders", "Order_Month"), {}, Path("visual.json"))
assert [(r.entity, r.prop) for r in refs] == [("Orders", "Order_Month")]
""",
    "r5-a-partition-beside-the-anchor-is-a-mismatch": """
survivor = [kit.field_ref("Orders", "Order Month Label")]
verdict = crta._judge_same_table_survivors(survivor, kit.ANCHOR, kit.as_of_call(), kit.facts(), "axis", True)
assert verdict is not None and verdict["verdict"] == "mismatch"
""",
    "r5-every-same-table-survivor-reads-as-clean": """
survivor = [kit.field_ref("Orders", "Order Month Label")]
assert crta._judge_same_table_survivors(survivor, kit.ANCHOR, kit.as_of_call(), kit.facts(), "axis", False) is None
""",
    "r6-a-mutation-may-name-a-dead-symbol": """
import mutation_harness_running_total_axis as harness
assert harness.MUTATIONS["probe-of-the-guard"][0].strip() == "crta._DATE_TYPES"
assert not hasattr(crta, "_DATE_TYPES")
""",
    "r6-a-mutation-may-ship-without-a-probe": """
import mutation_harness_running_total_axis as harness
assert "unprobed" in harness.MUTATIONS and "unprobed" not in harness.PROBES
assert sorted(set(harness.MUTATIONS) - set(harness.PROBES)) == ["unprobed"]
""",
}

# Patches that change NO observable behaviour. They must SURVIVE; if one is reported as caught, the
# harness is measuring something other than the mutation and its whole table is worthless.
CONTROLS: dict[str, str] = {
    "control-noop-rebind": """
crta._judge_window = crta._judge_window
dg.ModelFacts.grain_of = dg.ModelFacts.grain_of
""",
    "control-identity-wrapper": """
_orig = crta._judge_window
crta._judge_window = lambda cumulative, visual: _orig(cumulative, visual)
assert crta._judge_window is not _orig
""",
}

# A control's "intended behaviour" is that behaviour did NOT change, so its probe asserts the
# UNMUTATED outcome. Without it a control that silently crashed on call would still be reported as
# "SURVIVED as required", which is the same vacuity as a mutation crashing and reading as CAUGHT.
CONTROL_PROBES: dict[str, str] = {
    name: """
assert crta._judge_window(kit.cumulative(ordered_by=[kit.ANCHOR]), kit.visual())["code"] == "no_grouping_column"
"""
    for name in CONTROLS
}

_PLUGIN_HEAD = """import sys

sys.path.insert(0, r"{scripts}")
sys.path.insert(0, r"{tests}")

import check_running_total_axis as crta
import dax_grain as dg
import _mutation_probe_kit as kit

_NAME = {name!r}
_SENTINEL = r"{sentinel}"


def _mark(suffix=""):
    with open(_SENTINEL, "a", encoding="utf-8") as handle:
        handle.write(_NAME + suffix + "\\n")

"""

_PLUGIN_TAIL = """
_mark()
_PROBE = {probe!r}
try:
    exec(compile(_PROBE, "<probe>", "exec"), globals())
except BaseException:  # noqa: BLE001 - a probe failure must be REPORTED, never re-raised
    import traceback

    traceback.print_exc()
else:
    _mark(":probe")
"""


def _named_failures(output: str) -> list[str]:
    """The tests pytest reported as FAILED, and ONLY those.

    Two look-alike lines are deliberately excluded, both measured elsewhere in this repo:

    * ``ERROR path::TestName`` is a **collection** failure - no test ran - and counting it is the
      exact bug blind review found in ``tests/mutation_harness.py``, which scored ``CAUGHT`` for any
      non-zero exit including a collection error (4) or an interrupt (2).
    * a dying ``xdist`` worker prints ``FAILED path::test_name`` for a test that never ran, which is
      indistinguishable HERE - so it is caught upstream by `_BROKEN_RUN_MARKERS` instead.
    """
    return sorted(
        {line.split("::")[-1].split("[")[0].split(" ")[0] for line in output.splitlines() if line.startswith("FAILED")}
    )


def probe_is_trivial(probe: str) -> str | None:
    """Why this probe proves nothing, or None when it at least calls something and asserts.

    A probe that never asserts is a sentinel generator, not evidence - measured directly: replacing
    a stale-symbol mutation's probe with `pass` made the harness score the mutation ``CAUGHT`` again,
    reinstating review finding 6 in one line. This cannot prove an assertion is *discriminating*
    (only reading it can), but it removes the trivially-empty case from the table.
    """
    try:
        tree = ast.parse(probe)
    except SyntaxError as exc:  # pragma: no cover - a broken probe is a broken build
        return f"does not parse ({exc.msg})"
    nodes = list(ast.walk(tree))
    if not any(isinstance(node, (ast.Assert, ast.Raise)) for node in nodes):
        return "asserts nothing, so it cannot tell a mutated result from an unmutated one"
    if not any(isinstance(node, ast.Call) for node in nodes):
        return "calls nothing, so it never executes the patched object"
    return None


def run(name: str, code: str, probe: str) -> tuple[int, list[str], str]:
    """Apply one mutation in a child interpreter, PROBE it, and re-run the gate's suite."""
    SENTINEL.unlink(missing_ok=True)
    plugin = ROOT / "tests" / "_mutation_plugin_rta.py"
    plugin.write_text(
        _PLUGIN_HEAD.format(scripts=ROOT / "scripts", tests=ROOT / "tests", name=name, sentinel=SENTINEL)
        + code
        + _PLUGIN_TAIL.format(probe=probe),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PY, "-m", "pytest", TARGET, "-q", "-p", "_mutation_plugin_rta", "--no-header", "--tb=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "tests"), str(ROOT / "scripts")])},
    )
    plugin.unlink(missing_ok=True)
    output = proc.stdout + proc.stderr
    broken = next((marker for marker in _BROKEN_RUN_MARKERS if marker in output), None)
    if broken is not None:
        raise SystemExit(f"{name}: pytest fell over ({broken!r}) - a FALSE 'CAUGHT' was about to be reported\n{output}")
    if not proc.stdout.strip():
        # A crash exits non-zero exactly like a test failure. The discriminator is that a crashing
        # run prints nothing on stdout - the same signal that caught the unhashable-FieldRef bug in
        # the gate itself.
        raise SystemExit(f"{name}: pytest produced NO stdout, so exit {proc.returncode} is a crash, not a verdict")
    applied = SENTINEL.read_text(encoding="utf-8").split() if SENTINEL.exists() else []
    SENTINEL.unlink(missing_ok=True)
    if name not in applied:
        raise SystemExit(f"{name}: the mutation did NOT apply - any verdict from this run would be a lie\n{output}")
    if f"{name}:probe" not in applied:
        raise SystemExit(
            f"{name}: the mutation applied but its probe did not reach the intended behaviour, so a "
            f"'CAUGHT' here would be the review-finding-6 vacuity mode - a crash inside the mutant "
            f"reported as a semantic catch\n{output}"
        )
    # ONLY `FAILED` lines - see `_named_failures`.
    failed = _named_failures(output)
    return proc.returncode, failed, output.strip().splitlines()[-1][:110]


def _check_controls() -> None:
    """Prove the harness can still report SURVIVED, before trusting a table full of CAUGHT.

    A harness that scores everything CAUGHT is indistinguishable from a broken one - measured, this
    repo's other harness once reported 22/22 where every catch was an import error. So two patches
    that change no observable behaviour are run FIRST and must survive. If either is "caught", the
    signal is noise and the run stops rather than printing a reassuring table.
    """
    for name, code in CONTROLS.items():
        rc, failed, tail = run(name, code, CONTROL_PROBES[name])
        if rc != 0:
            raise SystemExit(f"CONTROL {name} was 'caught' ({', '.join(failed)}) - the harness is unsound\n{tail}")
        print(f"CONTROL   {name:46s} -> SURVIVED as required ({tail})")
    print()


def main() -> int:
    """Baseline first, controls second, then every mutation; a survivor is a reported hole."""
    baseline = subprocess.run(
        [PY, "-m", "pytest", TARGET, "-q", "--no-header"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    print(f"BASELINE {TARGET} exit={baseline.returncode}  {baseline.stdout.strip().splitlines()[-1]}")
    if baseline.returncode != 0:
        raise SystemExit("baseline is not green; fix that before reading any mutation result")
    unprobed = sorted(set(MUTATIONS) - set(PROBES))
    if unprobed:
        raise SystemExit("every mutation needs an intended-behaviour probe; missing: " + ", ".join(unprobed))
    trivial = sorted(f"{n}: {why}" for n, p in PROBES.items() if (why := probe_is_trivial(p)) is not None)
    if trivial:
        raise SystemExit("a probe that proves nothing reinstates review finding 6: " + "; ".join(trivial))
    print()
    _check_controls()
    survivors: list[str] = []
    misattributed: list[str] = []
    errored: list[str] = []
    for name, (code, expected) in MUTATIONS.items():
        rc, failed, tail = run(name, code, PROBES[name])
        if rc == 0:
            survivors.append(name)
            print(f"SURVIVED  {name:46s} -> {tail}")
            continue
        if not failed:
            # The exact bug blind review found in tests/mutation_harness.py: a non-zero exit with no
            # test outcome (collection error 4, interrupt 2) is NOT a catch.
            errored.append(name)
            print(f"ERROR     {name:46s} -> exit {rc} with NO named test outcome: {tail}")
            continue
        if expected not in failed:
            misattributed.append(name)
            print(f"CAUGHT    {name:46s} -> {len(failed)} test(s), but NOT {expected}: {', '.join(failed[:3])}")
            continue
        others = f" (+{len(failed) - 1} more)" if len(failed) > 1 else ""
        print(f"CAUGHT    {name:46s} -> {expected}{others}")
    print()
    print(f"mutations: {len(MUTATIONS)}, each applied AND probed before its verdict was read")
    print("survivors (holes in the suite):", survivors or "none")
    print("caught by a different test than claimed:", misattributed or "none")
    print("non-zero exit with no test outcome (NOT a catch):", errored or "none")
    return 1 if survivors or misattributed or errored else 0


if __name__ == "__main__":
    raise SystemExit(main())
