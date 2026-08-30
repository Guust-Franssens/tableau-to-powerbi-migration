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
_orig = crta._is_positional
crta._is_positional = lambda arg: True
assert crta._is_positional is not _orig
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
_orig = crta._is_date_typed
crta._is_date_typed = lambda columns, table, column: True
assert crta._is_date_typed is not _orig
""",
        "test_as_of_non_date_axis_is_deliberately_not_flagged",
    ),
    "cleared-columns-dropped": (
        """
_orig = crta._column_refs
def refs(text):
    return _orig(text)
crta._classify_as_of_orig = crta._classify_as_of
def as_of(expr, base):
    out = crta._classify_as_of_orig(expr, base)
    if out is not None:
        out.cleared_columns = []
        out.cleared_tables = []
    return out
crta._classify_as_of = as_of
assert crta._classify_as_of is not crta._classify_as_of_orig
""",
        "test_as_of_clearing_the_coarser_column_too_is_clean",
    ),
    "compared-column-not-treated-as-cleared": (
        """
_orig = crta.ColumnRef.key
crta.ColumnRef.key = lambda self: ((self.table or "").casefold(), self.column.casefold(), id(self))
assert crta.ColumnRef.key is not _orig
""",
        "test_as_of_axis_is_the_compared_column",
    ),
    "every-blank-stub-claimed": (
        """
_orig = crta._classify_stub
def stub(member, base):
    base.shape = "stub"
    base.assessable = False
    base.reason = "mutated"
    return base
crta._classify_stub = stub
assert crta._classify_stub is not _orig
""",
        "test_an_ordinary_blank_stub_is_not_surfaced",
    ),
    "running-total-stub-not-surfaced": (
        """
_orig = crta._classify_stub
crta._classify_stub = lambda member, base: None
assert crta._classify_stub is not _orig
""",
        "test_running_total_stub_bound_to_a_visual_is_unassessable",
    ),
    "period-to-date-omission-hidden": (
        """
_orig = crta.period_to_date_measures
crta.period_to_date_measures = lambda measures: []
assert crta.period_to_date_measures is not _orig
""",
        "test_period_to_date_is_named_on_a_clean_run",
    ),
    "fixed-window-datesbetween-counted": (
        """
import re
_orig = crta._TIME_INTELLIGENCE_RE
crta._TIME_INTELLIGENCE_RE = re.compile(
    r"\\b(DATESYTD|DATESMTD|DATESQTD|DATESBETWEEN|DATESINPERIOD|TOTALYTD|TOTALMTD|TOTALQTD)\\s*\\(", re.IGNORECASE)
assert crta._TIME_INTELLIGENCE_RE is not _orig
""",
        "test_period_to_date_is_named_on_a_clean_run",
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
_orig = crta._split_arguments
crta._split_arguments = lambda text: [part.strip() for part in text.split(",")]
assert crta._split_arguments is not _orig
""",
        "test_split_arguments_respects_nesting_and_strings",
    ),
    "column-matching-is-case-sensitive": (
        """
_orig = crta.ColumnRef.key
crta.ColumnRef.key = lambda self: (self.table or "", self.column)
assert crta.ColumnRef.key is not _orig
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
_orig = crta._classify_window
def window(expr, base):
    out = _orig(expr, base)
    if out is not None and out.partition_by:
        out.ordered_by = out.ordered_by + out.partition_by + [crta.ColumnRef("Nowhere", "Nothing")]
    return out
crta._classify_window = window
assert crta._classify_window is not _orig
""",
        "test_partitionby_legend_is_not_a_mismatch",
    ),
}

# Patches that change NO observable behaviour. They must SURVIVE; if one is reported as caught, the
# harness is measuring something other than the mutation and its whole table is worthless.
CONTROLS: dict[str, str] = {
    "control-noop-rebind": """
crta._judge_window = crta._judge_window
crta._is_date_typed = crta._is_date_typed
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
        "import check_running_total_axis as crta\n"
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
    for name, (code, expected) in MUTATIONS.items():
        rc, failed, tail = run(name, code)
        if rc == 0:
            survivors.append(name)
            print(f"SURVIVED  {name:44s} -> {tail}")
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
    return 1 if survivors or misattributed else 0


if __name__ == "__main__":
    raise SystemExit(main())
