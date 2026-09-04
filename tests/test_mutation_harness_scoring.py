"""The mutation harness must credit a mutation only when a test actually observed it.

Two defects, both found by blind review, both from the same root cause -- **scraping pytest's
terminal output is a proxy for "did a test observe the mutation", and the proxy fails in both
directions.**

Round 1 (the original defect, on master since #331)::

    verdict = "CAUGHT " if rc != 0 else "SURVIVED"

``run()`` already extracted named FAILED lines; ``main()`` threw them away. Measured:
``pytest tests/does_not_exist.py -q`` exits **4** having run nothing, and scored CAUGHT.

Round 2 -- this file's first fix was still text-based, and was wrong three ways:

* a **collection** failure on a class emits ``ERROR path::TestName``, indistinguishable from a
  named test error, so it scored CAUGHT*;
* a dying **xdist** worker emits ``FAILED path::test_name`` for a test that never executed;
* ``PY_COLORS=1`` prefixes those tokens with ANSI escapes, so ``startswith`` failed and genuine
  detections became HARNESS-ERROR -- the opposite bug.

The verdict now comes from pytest's own lifecycle hooks (``pytest_runtest_logreport``,
``pytest_collectreport``, ``pytest_internalerror``, ``pytest_testnodedown``), recorded to JSON by
the injected plugin. ``report.when`` is the discriminator terminal text throws away: ``call``
means an assertion noticed, ``setup``/``teardown`` means a crash noticed, and neither exists when
pytest never ran.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutate_check_unit  # noqa: E402
import mutate_package_unit  # noqa: E402
import mutation_harness  # noqa: E402
from mutation_harness import (  # noqa: E402
    is_harness_error,
    observed_mutation,
    read_outcomes,
    session_ended_abnormally,
    session_is_trustworthy,
)


def record(**kwargs) -> dict:
    """A record for a COMPLETE session that ran at least one test, with the rest empty.

    The defaults matter: `recorded=True` alone proves only that the plugin imported, so a
    valid-looking record must also show a finished session, a real test report, and a
    verdict-bearing exit status before any verdict is issued.
    """
    base = {
        "call_failed": [],
        "setup_failed": [],
        "collect_error": [],
        "internal_error": False,
        "node_down": False,
        "session_finished": True,
        "exitstatus": 0,
        "saw_call_phase": True,
        "runtest_loop_completed": True,
        "runtest_loop_exception": None,
        "synthetic_failed": [],
        "process_returncode": 0,
        "recorded": True,
    }
    base.update(kwargs)
    return base


def test_an_assertion_failure_is_not_a_harness_error() -> None:
    """The strongest evidence: a test ran and its assertion noticed."""
    assert is_harness_error(record(call_failed=["test_x"])) is False


def test_a_setup_crash_is_not_a_harness_error() -> None:
    """Weaker evidence, but still a named test observing the mutation."""
    assert is_harness_error(record(setup_failed=["test_x"])) is False


def test_a_collection_error_is_a_harness_error_even_though_it_names_a_node() -> None:
    """Reviewer's reproduction: a class-level collection failure emits ``ERROR path::TestName``.

    Text parsing cannot tell that from a named test error. The lifecycle record can.
    """
    assert is_harness_error(record(collect_error=["tests/test_x.py::TestSynthetic"])) is True


def test_a_dead_worker_alone_cannot_prove_survival() -> None:
    """Round 2 asserted this record was a harness error outright; round 4 refined it.

    ``[gw0] node down`` then ``FAILED path::test_name`` -- that FAILED line names a test which
    never executed. But the fix is to filter the synthetic report **at record time** by its
    ``when="???"`` phase, not to veto every recorded failure whenever a worker died. So with
    no genuine report, this is still a harness error; the discriminating case is
    ``test_a_genuine_failure_survives_a_dead_sibling_worker``.
    """
    outcomes = record(node_down=True, synthetic_failed=["tests/x.py::test_never_ran::???"])

    assert observed_mutation(outcomes) is False
    assert session_is_trustworthy(outcomes) is False
    assert is_harness_error(outcomes) is True


def test_an_internal_error_is_a_harness_error() -> None:
    assert is_harness_error(record(internal_error=True)) is True


def test_no_record_at_all_is_a_harness_error() -> None:
    """If the plugin never wrote its file, pytest never started. Absence is not innocence."""
    assert is_harness_error(record(recorded=False)) is True


def test_the_generated_plugin_renders_and_parses(tmp_path: Path) -> None:
    """The hooks are a ``.format()`` TEMPLATE, so every literal brace must be doubled.

    Caught only by an end-to-end run: an f-string added to the template as
    ``f"{report.nodeid}"`` had its braces consumed by ``.format()``, raising
    ``KeyError: 'report'`` for every mutation. The predicate unit tests above cannot see this
    -- they never render the template -- so this is the test that would have.
    """
    src = mutation_harness.OUTCOME_HOOKS.format(outcome_path=str(tmp_path / "o.json"))

    ast.parse(src)
    assert "{report.nodeid}" in src, "literal braces must survive .format()"
    assert "{{" not in src, "no doubled brace should remain after rendering"


def test_every_hook_the_verdict_depends_on_is_present() -> None:
    """Each field the predicates read must actually be written by some hook."""
    src = mutation_harness.OUTCOME_HOOKS

    for hook in (
        "pytest_runtest_logreport",
        "pytest_collectreport",
        "pytest_internalerror",
        "pytest_testnodedown",
        "pytest_sessionfinish",
        "pytest_runtestloop",
    ):
        assert f"def {hook}(" in src, f"{hook} is read by a predicate but never defined"
    # xdist-only, and pytest rejects the whole plugin if it is not declared optional.
    assert "optionalhook=True" in src


def test_a_missing_outcome_file_reads_as_unrecorded(tmp_path: Path) -> None:
    """Absence is not innocence: no record means pytest never started."""
    outcomes = read_outcomes(tmp_path / "nope.json")

    assert outcomes["recorded"] is False
    assert is_harness_error(outcomes) is True


def test_an_unparseable_outcome_file_reads_as_unrecorded(tmp_path: Path) -> None:
    """A truncated write must not be mistaken for a clean run with no failures."""
    path = tmp_path / "outcomes.json"
    path.write_bytes(b'{"call_failed": [')

    outcomes = read_outcomes(path)

    assert outcomes["recorded"] is False
    assert is_harness_error(outcomes) is True


def test_a_clean_run_is_neither_caught_nor_a_harness_error() -> None:
    """The SURVIVED case: pytest ran, nothing failed. That is a hole in the suite."""
    outcomes = record()

    assert is_harness_error(outcomes) is False
    assert not outcomes["call_failed"]
    assert not outcomes["setup_failed"]


def test_a_session_that_exits_before_running_anything_is_a_harness_error() -> None:
    """Reviewer's reproduction: ``pytest.exit(returncode=0)`` from ``pytest_sessionstart``.

    Exit 0, a valid record written at plugin import, and **no test ever ran** -- which the
    previous version reported as SURVIVED, i.e. as a hole in the suite rather than as a run
    that never happened.
    """
    assert is_harness_error(record(saw_call_phase=False, session_finished=True, exitstatus=0)) is True


def test_an_interrupt_after_a_genuine_failure_still_counts_as_caught() -> None:
    """Detection is DURABLE - round 3 overturned round 2 on exactly this case.

    A call failure followed by ``KeyboardInterrupt`` in teardown exits **2**. Round 2 said
    treat an unexpected exit as a harness error "even when an earlier outcome was recorded";
    round 3 measured the cost -- a real assertion failure erased into a HARNESS-ERROR.

    A test that failed in its ``call`` phase noticed the mutation. Nothing later un-notices it.
    """
    outcomes = record(call_failed=["test_x"], exitstatus=2)

    assert observed_mutation(outcomes) is True
    assert is_harness_error(outcomes) is False
    # ...but the session is still not trustworthy, which is what SURVIVED would need.
    assert session_is_trustworthy(outcomes) is False


def test_an_unfinished_session_cannot_prove_survival_but_keeps_its_detection() -> None:
    """The asymmetry, stated directly: a partial run can prove CAUGHT, never SURVIVED."""
    partial = record(call_failed=["test_x"], session_finished=False)
    assert observed_mutation(partial) is True
    assert session_is_trustworthy(partial) is False
    assert is_harness_error(partial) is False

    silent = record(session_finished=False)
    assert observed_mutation(silent) is False
    assert is_harness_error(silent) is True


def test_a_dead_worker_failure_is_synthetic_and_never_counts() -> None:
    """The synthetic report is filtered AT RECORD TIME, not by a global flag.

    Round 4 showed a global `node_down` veto is too blunt: a genuine call failure emitted by a
    living worker -- or by the dying one before it died -- is still real evidence. xdist's
    fabricated crash report carries ``when="???"``, so it lands in ``synthetic_failed`` and
    never reaches ``call_failed``.
    """
    outcomes = record(node_down=True, synthetic_failed=["tests/x.py::test_never_ran::???"])

    assert observed_mutation(outcomes) is False
    assert is_harness_error(outcomes) is True


def test_a_genuine_failure_survives_a_dead_sibling_worker() -> None:
    """Round-4 finding: a real call failure must NOT be erased because some worker died."""
    outcomes = record(call_failed=["test_x"], node_down=True)

    assert observed_mutation(outcomes) is True
    assert is_harness_error(outcomes) is False
    assert session_is_trustworthy(outcomes) is False


def test_one_completed_call_phase_does_not_prove_the_suite_ran() -> None:
    """Round-4 finding: ``pytest.exit()`` from teardown after the FIRST passing test.

    Measured on the 14-test scoring file: ``1 passed`` then the Exit message, with
    ``session_finished``, ``saw_call_phase`` and ``exitstatus=0`` -- and it scored SURVIVED
    having run 1 of 14. Only ``runtest_loop_completed`` distinguishes it.
    """
    outcomes = record(runtest_loop_completed=False)

    assert session_is_trustworthy(outcomes) is False
    assert is_harness_error(outcomes) is True


def test_an_interrupted_loop_after_a_catch_is_not_annotated_as_abnormal() -> None:
    """Mutations run under ``-x``, so a CAUGHT always interrupts the loop.

    ``Interrupted`` is expected here and stays quiet; the narrowing is by **cause**, not by
    dropping the term, so an explicit ``Exit`` is still reported (next test).
    """
    caught_under_x = record(
        call_failed=["test_x"],
        runtest_loop_completed=False,
        runtest_loop_exception="Failed",
        exitstatus=1,
        process_returncode=1,
    )

    assert observed_mutation(caught_under_x) is True
    assert session_ended_abnormally(caught_under_x) is False
    assert is_harness_error(caught_under_x) is False


def test_an_explicit_pytest_exit_IS_annotated_even_when_the_mutation_was_caught() -> None:
    """``Exit`` means somebody cut the run short -- distinguishable from ``-x``'s Interrupted."""
    outcomes = record(
        call_failed=["test_x"],
        runtest_loop_completed=False,
        runtest_loop_exception="Exit",
        exitstatus=1,
        process_returncode=1,
    )

    assert observed_mutation(outcomes) is True
    assert session_ended_abnormally(outcomes) is True


def test_the_record_and_the_process_must_agree() -> None:
    """Round-5 finding: the record is the view from inside, the return code from outside.

    Measured: a ``trylast=True`` session-finish hook set ``session.exitstatus = 1`` AFTER the
    recorder ran, and an ``atexit`` handler called ``os._exit(1)`` after a clean session. Both
    left ``exitstatus=0`` recorded while the process returned 1, and both scored SURVIVED.
    """
    disagreeing = record(exitstatus=0, process_returncode=1)

    assert session_is_trustworthy(disagreeing) is False
    assert session_ended_abnormally(disagreeing) is True
    assert is_harness_error(disagreeing) is True


def test_exit_one_without_a_recorded_detection_cannot_prove_survival() -> None:
    """Exit 1 means tests failed. A failure we did not record cannot prove anything survived."""
    assert session_is_trustworthy(record(exitstatus=1)) is False


def test_a_setup_report_alone_does_not_prove_a_test_body_ran() -> None:
    """Reviewer's reproduction: ``pytest.exit(returncode=0)`` from ``pytest_runtest_call``.

    That yields ``session_finished``, ``exitstatus=0`` and a setup-phase report, while pytest's
    own stdout says ``no tests ran``. Hence ``saw_call_phase`` rather than "a report happened".
    """
    outcomes = record(saw_call_phase=False)

    assert session_is_trustworthy(outcomes) is False
    assert is_harness_error(outcomes) is True


# ---------------------------------------------------------------------------------------------
# #480 round 4: a multi-anchor mutation must be observed by EVERY anchor it declares
# ---------------------------------------------------------------------------------------------

#: A throwaway module the probe tests read, so a mutation has something to patch that is bound as
#: a module ATTRIBUTE (patching a `from x import y` name would not reach the test's own binding).
PROBE_SUPPORT = "def value():\n    return 1\n"

#: Two tests: one reads the patched value, one does not. Under a single `-x` invocation pytest
#: stops at the first failure and the second never runs -- which is the whole defect.
PROBE_TESTS = (
    "import _harness_probe_support as s\n\n\n"
    "def test_reads_the_patched_value():\n    assert s.value() == 1\n\n\n"
    "def test_reads_nothing_of_the_kind():\n    assert 2 + 2 == 4\n"
)

#: Patches the probe module's function. The first probe test observes it; the second cannot.
PROBE_MUTATION = "import _harness_probe_support as s\ns.value = lambda: 99\n"


def _probe_files() -> tuple[Path, Path]:
    """Write the probe module and its test file into `tests/`, where the harness puts them on path."""
    here = Path(__file__).resolve().parent
    support = here / "_harness_probe_support.py"
    tests = here / "_harness_probe_tests.py"
    support.write_text(PROBE_SUPPORT, encoding="utf-8")
    tests.write_text(PROBE_TESTS, encoding="utf-8")
    return support, tests


def test_a_mutation_only_ONE_declared_anchor_can_see_is_not_scored_as_caught() -> None:
    """#480 round 4, finding 2 -- and the campaign banner that claimed otherwise.

    The harness ran every declared anchor in ONE pytest invocation under `-x`, so it stopped at
    the first failure and credited the mutation to the whole anchor list. The measured shape was
    `BOTH ANCHORS WITH -x: first anchor failed` / `SECOND ANCHOR ALONE: 1 passed`, while the
    campaign printed *"each against its OWN anchor(s)"*. That sentence had already misled a human
    into trusting an anchor that never ran.
    """
    support, tests = _probe_files()
    observing = "tests/_harness_probe_tests.py::test_reads_the_patched_value"
    blind = "tests/_harness_probe_tests.py::test_reads_nothing_of_the_kind"
    try:
        _name, _rc, detail, outcomes = mutation_harness.run("probe", PROBE_MUTATION, (observing, blind))
    finally:
        support.unlink(missing_ok=True)
        tests.unlink(missing_ok=True)

    assert outcomes["targets"] == [observing, blind], "each declared anchor gets its own invocation"
    assert outcomes["targets_observing"] == [observing]
    assert observed_mutation(outcomes) is False, "one anchor out of two is not 'its OWN anchor(s)'"
    assert mutation_harness.anchors_that_missed(outcomes) == [blind]
    assert "only 1/2" in detail and "test_reads_nothing_of_the_kind" in detail


def test_a_mutation_EVERY_declared_anchor_sees_is_still_scored_as_caught() -> None:
    """Positive control. Requiring all anchors must not make a genuine multi-anchor kill unprovable."""
    support, tests = _probe_files()
    tests.write_text(PROBE_TESTS.replace("assert 2 + 2 == 4", "assert s.value() == 1"), encoding="utf-8")
    anchors = (
        "tests/_harness_probe_tests.py::test_reads_the_patched_value",
        "tests/_harness_probe_tests.py::test_reads_nothing_of_the_kind",
    )
    try:
        _name, _rc, detail, outcomes = mutation_harness.run("probe", PROBE_MUTATION, anchors)
    finally:
        support.unlink(missing_ok=True)
        tests.unlink(missing_ok=True)

    assert outcomes["targets_observing"] == list(anchors)
    assert observed_mutation(outcomes) is True
    assert mutation_harness.anchors_that_missed(outcomes) == []
    assert "only" not in detail


def test_a_record_with_no_per_target_history_keeps_the_any_evidence_rule() -> None:
    """Backwards compatibility, stated as a test: the hand-built records above have no `targets`.

    Narrowing `observed_mutation` must not invent per-target facts nobody recorded -- otherwise
    every older caller silently loses its detections.
    """
    assert observed_mutation(record(call_failed=["test_x"])) is True
    assert mutation_harness.anchors_that_missed(record(call_failed=["test_x"])) == []


def test_merging_two_sessions_keeps_evidence_and_demands_completeness_from_both() -> None:
    """The merge rule, stated directly: evidence concatenates, completeness must hold everywhere.

    A survival verdict is a claim about EVERY anchor, so one incomplete session must be enough to
    deny it -- while a detection anywhere is durable and survives the merge.
    """
    merged = mutation_harness.merge_target_outcomes(
        {"a": record(call_failed=["test_a"], exitstatus=1, process_returncode=1), "b": record()}
    )

    assert merged["call_failed"] == ["test_a"]
    assert merged["targets_observing"] == ["a"]
    assert session_is_trustworthy(merged) is False, "one session failed, so nothing survived"

    incomplete = mutation_harness.merge_target_outcomes({"a": record(), "b": record(runtest_loop_completed=False)})
    assert session_is_trustworthy(incomplete) is False
    assert session_is_trustworthy(mutation_harness.merge_target_outcomes({"a": record(), "b": record()})) is True


def test_package_unit_campaign_reports_partial_anchor_when_only_one_anchor_fails(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(cmd, **_kwargs):
        anchor = cmd[cmd.index("-k") + 1]
        calls.append(anchor)
        stdout = "1 failed" if anchor == "test_observes" else "1 passed"
        return subprocess.CompletedProcess(cmd, 1 if anchor == "test_observes" else 0, stdout=stdout, stderr="")

    monkeypatch.setattr(mutate_package_unit.subprocess, "run", fake_run)

    verdict, note = mutate_package_unit.run_anchor(["test_observes", "test_misses"])

    assert calls == ["test_observes", "test_misses"]
    assert verdict == "PARTIAL-ANCHOR"
    assert note == "caught: test_observes; missed: test_misses"


def test_package_unit_campaign_treats_failed_plus_error_output_as_broken(monkeypatch) -> None:
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="1 failed, 1 error", stderr="")

    monkeypatch.setattr(mutate_package_unit.subprocess, "run", fake_run)

    verdict, note = mutate_package_unit.run_one_anchor("test_observes")

    assert verdict == "BROKEN"
    assert note == "1 failed, 1 error"


def test_package_unit_campaign_treats_internalerror_plus_failed_output_as_broken(monkeypatch) -> None:
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 3, stdout="INTERNALERROR> boom\n1 failed", stderr="")

    monkeypatch.setattr(mutate_package_unit.subprocess, "run", fake_run)

    verdict, note = mutate_package_unit.run_one_anchor("test_observes")

    assert verdict == "BROKEN"
    assert note == "1 failed"


def test_package_unit_restored_anchor_declaration_runs_each_anchor_independently(monkeypatch) -> None:
    label = "README: send the agent back to the bundle to edit (issue #460's silent-discard shape)"
    names = next(
        names for mutation_label, _target, _old, _new, names in mutate_package_unit.MUTATIONS if mutation_label == label
    )
    calls: list[str] = []

    def fake_run(cmd, **_kwargs):
        anchor = cmd[cmd.index("-k") + 1]
        calls.append(anchor)
        return subprocess.CompletedProcess(cmd, 1, stdout="1 failed", stderr="")

    monkeypatch.setattr(mutate_package_unit.subprocess, "run", fake_run)

    verdict, note = mutate_package_unit.run_anchor(names)

    assert names == [
        "test_AGENTS_md_and_the_package_readme_agree_on_where_an_agent_edits",
        "test_declared_edit_tooling_is_scoped_to_bundle_work_in_BOTH_documents",
    ]
    assert calls == names
    assert verdict == "CAUGHT (2 anchors)"
    assert note == ""


def test_check_unit_campaign_requires_every_anchor_to_fail(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(cmd, **_kwargs):
        anchor = cmd[cmd.index("-k") + 1]
        calls.append(anchor)
        return subprocess.CompletedProcess(cmd, 1, stdout="1 failed", stderr="")

    monkeypatch.setattr(mutate_check_unit.subprocess, "run", fake_run)

    verdict, note = mutate_check_unit.run_anchor(["test_first", "test_second"])

    assert calls == ["test_first", "test_second"]
    assert verdict == "CAUGHT (2 anchors)"
    assert note == ""


def test_check_unit_campaign_treats_failed_plus_error_output_as_broken(monkeypatch) -> None:
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="1 failed, 1 error", stderr="")

    monkeypatch.setattr(mutate_check_unit.subprocess, "run", fake_run)

    verdict, note = mutate_check_unit.run_one_anchor("test_observes")

    assert verdict == "BROKEN"
    assert note == "1 failed, 1 error"


def test_check_unit_campaign_treats_internalerror_plus_failed_output_as_broken(monkeypatch) -> None:
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 3, stdout="INTERNALERROR> boom\n1 failed", stderr="")

    monkeypatch.setattr(mutate_check_unit.subprocess, "run", fake_run)

    verdict, note = mutate_check_unit.run_one_anchor("test_observes")

    assert verdict == "BROKEN"
    assert note == "1 failed"


def test_campaign_main_treats_partial_anchor_as_non_success(monkeypatch) -> None:
    partial_results = [
        ("real mutation", "PARTIAL-ANCHOR", "caught: test_a; missed: test_b"),
        (mutate_check_unit.NEGATIVE_CONTROL, "SURVIVED", ""),
    ]
    monkeypatch.setattr(mutate_check_unit, "run_campaign", lambda _selected: partial_results)
    monkeypatch.setattr(mutate_package_unit, "run_campaign", lambda _selected: partial_results)

    assert mutate_check_unit.main([]) == 1
    assert mutate_package_unit.main([]) == 1
