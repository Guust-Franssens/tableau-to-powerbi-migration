"""Mutation harness for the running-total axis gate (#218): prove its tests can actually fail.

    python tests/mutation_harness_running_total_axis.py

Not named ``test_*``, so pytest does not collect it -- it *drives* pytest. Each mutation breaks ONE
behaviour of ``scripts/check_running_total_axis.py`` by monkeypatching it at interpreter start
(injected as a pytest plugin), then re-runs the gate's own suite. A mutation that produces a GREEN
run is a HOLE in the tests, not a success.

Two false-green traps are guarded explicitly, both of them measured in this repo:

1. **A plugin that fails to import** makes pytest exit non-zero before running a single test, and a
   naive harness scores that as CAUGHT. ``tests/mutation_harness.py`` reported 22/22 on exactly that
   bug. Guarded by refusing any run whose output mentions an import error.
2. **A mutation that never applied.** A text-substitution harness elsewhere silently matched nothing
   on CRLF input and "passed" against unmutated code. Guarded harder than by inspection: every
   snippet appends its own name to a sentinel file **after** the patch, and additionally asserts the
   patched object actually changed identity. No sentinel line, no verdict - the run is an ERROR.

Scoring rule, stated because the sibling harness got it wrong
--------------------------------------------------------------
Blind review of PR #405 found that ``tests/mutation_harness.py`` scored ``CAUGHT`` for *any*
non-zero pytest exit, including a collection error (4) or an interrupt (2) where **no test ran**.
This harness never does that. A mutation is ``CAUGHT`` only when the **named** test the table
claims for it appears in pytest's ``FAILED`` lines:

* non-zero exit with **no** named failure  -> ``ERROR (no test outcome)``, and the run fails;
* non-zero exit naming a **different** test -> reported as misattributed, and the run fails;
* the baseline must be **green first**, or nothing is run at all - so an already-failing test cannot
  be credited to every mutation after it.

Read the PR for #218 for the result table.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
TARGET = "tests/test_check_running_total_axis.py"
SENTINEL = ROOT / "tests" / "_mutation_applied.txt"

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
_orig = dg._column_refs
def refs(text):
    return _orig(text)
dg._classify_as_of_orig = dg._classify_as_of
def as_of(expr, base):
    out = dg._classify_as_of_orig(expr, base)
    if out is not None:
        out.cleared_columns = []
        out.cleared_tables = []
    return out
dg._classify_as_of = as_of
assert dg._classify_as_of is not dg._classify_as_of_orig
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
    if self.data_type(ref.table or "", ref.column) in crta._DATE_TYPES:
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


def run(name: str, code: str) -> tuple[int, list[str], str]:
    """Apply one mutation in a child interpreter and re-run the gate's suite."""
    SENTINEL.unlink(missing_ok=True)
    plugin = ROOT / "tests" / "_mutation_plugin_rta.py"
    plugin.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{ROOT / 'scripts'}')\n"
        f"sys.path.insert(0, r'{ROOT / 'tests'}')\n"
        "import check_running_total_axis as crta\nimport dax_grain as dg\n"
        + code
        + f"\nopen(r'{SENTINEL}', 'a', encoding='utf-8').write({name!r} + '\\n')\n",
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
    if "Error importing plugin" in output or "INTERNALERROR" in output:
        raise SystemExit(f"{name}: the plugin never imported - a FALSE 'CAUGHT' was about to be reported\n{output}")
    applied = SENTINEL.read_text(encoding="utf-8").split() if SENTINEL.exists() else []
    SENTINEL.unlink(missing_ok=True)
    if name not in applied:
        raise SystemExit(f"{name}: the mutation did NOT apply - any verdict from this run would be a lie\n{output}")
    failed = sorted(
        {line.split("::")[-1].split("[")[0].split(" ")[0] for line in output.splitlines() if line.startswith("FAILED")}
    )
    return proc.returncode, failed, output.strip().splitlines()[-1][:110]


def _check_controls() -> None:
    """Prove the harness can still report SURVIVED, before trusting a table full of CAUGHT.

    A harness that scores everything CAUGHT is indistinguishable from a broken one - measured, this
    repo's other harness once reported 22/22 where every catch was an import error. So two patches
    that change no observable behaviour are run FIRST and must survive. If either is "caught", the
    signal is noise and the run stops rather than printing a reassuring table.
    """
    for name, code in CONTROLS.items():
        rc, failed, tail = run(name, code)
        if rc != 0:
            raise SystemExit(f"CONTROL {name} was 'caught' ({', '.join(failed)}) - the harness is unsound\n{tail}")
        print(f"CONTROL   {name:44s} -> SURVIVED as required ({tail})")
    print()


def main() -> int:
    """Baseline first, controls second, then every mutation; a survivor is a reported hole."""
    baseline = subprocess.run(
        [PY, "-m", "pytest", TARGET, "-q", "--no-header"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    print(f"BASELINE {TARGET} exit={baseline.returncode}  {baseline.stdout.strip().splitlines()[-1]}")
    if baseline.returncode != 0:
        raise SystemExit("baseline is not green; fix that before reading any mutation result")
    print()
    _check_controls()
    survivors: list[str] = []
    misattributed: list[str] = []
    errored: list[str] = []
    for name, (code, expected) in MUTATIONS.items():
        rc, failed, tail = run(name, code)
        if rc == 0:
            survivors.append(name)
            print(f"SURVIVED  {name:44s} -> {tail}")
            continue
        if not failed:
            # The exact bug blind review found in tests/mutation_harness.py: a non-zero exit with no
            # test outcome (collection error 4, interrupt 2) is NOT a catch.
            errored.append(name)
            print(f"ERROR     {name:44s} -> exit {rc} with NO named test outcome: {tail}")
            continue
        if expected not in failed:
            misattributed.append(name)
            print(f"CAUGHT    {name:44s} -> {len(failed)} test(s), but NOT {expected}: {', '.join(failed[:3])}")
            continue
        others = f" (+{len(failed) - 1} more)" if len(failed) > 1 else ""
        print(f"CAUGHT    {name:44s} -> {expected}{others}")
    print()
    print("survivors (holes in the suite):", survivors or "none")
    print("caught by a different test than claimed:", misattributed or "none")
    print("non-zero exit with no test outcome (NOT a catch):", errored or "none")
    return 1 if survivors or misattributed or errored else 0


if __name__ == "__main__":
    raise SystemExit(main())
