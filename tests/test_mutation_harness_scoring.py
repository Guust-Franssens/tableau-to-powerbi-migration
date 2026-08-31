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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_harness import (  # noqa: E402
    is_harness_error,
    observed_mutation,
    read_outcomes,
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


def test_a_dead_xdist_worker_is_a_harness_error_despite_a_failed_line() -> None:
    """Reviewer's reproduction: ``[gw0] node down`` then ``FAILED path::test_name``.

    That FAILED line names a test which never executed, so the terminal summary is actively
    misleading here -- ``call_failed`` is populated AND the run is still a harness error.
    """
    assert is_harness_error(record(node_down=True, call_failed=["test_never_ran"])) is True


def test_an_internal_error_is_a_harness_error() -> None:
    assert is_harness_error(record(internal_error=True)) is True


def test_no_record_at_all_is_a_harness_error() -> None:
    """If the plugin never wrote its file, pytest never started. Absence is not innocence."""
    assert is_harness_error(record(recorded=False)) is True


def test_a_missing_outcome_file_reads_as_unrecorded(tmp_path: Path) -> None:
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
    """The one exception, and it is not an inconsistency.

    A dying xdist worker emits ``FAILED path::test_name`` for a test that **never executed**,
    so that ``call_failed`` entry is fabricated rather than observed.
    """
    outcomes = record(node_down=True, call_failed=["test_never_ran"])

    assert observed_mutation(outcomes) is False
    assert is_harness_error(outcomes) is True


def test_a_setup_report_alone_does_not_prove_a_test_body_ran() -> None:
    """Reviewer's reproduction: ``pytest.exit(returncode=0)`` from ``pytest_runtest_call``.

    That yields ``session_finished``, ``exitstatus=0`` and a setup-phase report, while pytest's
    own stdout says ``no tests ran``. Hence ``saw_call_phase`` rather than "a report happened".
    """
    outcomes = record(saw_call_phase=False)

    assert session_is_trustworthy(outcomes) is False
    assert is_harness_error(outcomes) is True
