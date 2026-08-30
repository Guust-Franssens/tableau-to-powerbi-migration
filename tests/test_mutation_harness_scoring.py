"""The mutation harness must not credit a mutation for pytest failing to run.

This exists because it did. `main()` judged every mutation with

    verdict = "CAUGHT " if rc != 0 else "SURVIVED"

which awards CAUGHT to a collection error (exit 4), an internal error (exit 3) or an
interrupt (exit 2) -- none of which involve a test observing anything. Measured on master:
`pytest tests/does_not_exist.py -q` exits **4** with **zero** named FAILED lines, and that
expression scored it CAUGHT.

The same file already guarded ONE instance of this class, with a comment reading "the harness
would report a FALSE 'CAUGHT'", so the hazard was known and only partially closed.

Found by blind review of PR #405, whose own 14/14 mutation table was produced by this harness.
A surviving neutral control does not disprove the defect: a neutral change exits 0, so it only
exercises the SURVIVED direction.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mutation_harness import named_failures, named_outcomes  # noqa: E402


REAL_FAILURE = "FAILED tests/test_e2e_offline.py::test_something - AssertionError: boom"
NAMED_ERROR = "ERROR tests/test_e2e_offline.py::test_the_front_half_produced_real_artifacts_from_real_bytes"
COLLECTION_ERROR = "ERROR tests/does_not_exist.py\n!!!!! Interrupted: no tests ran !!!!!"


def test_a_named_failure_is_evidence() -> None:
    """An assertion observed the mutation - the strongest form."""
    assert named_outcomes(REAL_FAILURE) == (["test_something"], [])


def test_a_named_ERROR_is_also_evidence_but_kept_separate() -> None:
    """A setup/teardown crash in a NAMED test observed the mutation too, just not by asserting.

    Measured: the ``engine-stand-in-emits-no-pages`` mutation produces exactly this shape. An
    earlier version of this fix classified it as a harness error and would have discarded a real
    detection - the opposite bug to the one being fixed.
    """
    failed, errored = named_outcomes(NAMED_ERROR)

    assert failed == []
    assert errored == ["test_the_front_half_produced_real_artifacts_from_real_bytes"]


def test_a_collection_error_names_no_test_and_is_not_evidence() -> None:
    """Exit 4 with no test named means pytest never ran anything."""
    assert named_outcomes(COLLECTION_ERROR) == ([], [])


def test_empty_output_is_not_evidence() -> None:
    assert named_outcomes("") == ([], [])


def test_every_named_failure_is_returned_not_just_the_first() -> None:
    """``run()`` reports the first for brevity, but the verdict must see the whole set."""
    out = "\n".join(
        [
            "FAILED tests/test_a.py::test_one - AssertionError",
            "FAILED tests/test_a.py::test_two - AssertionError",
        ]
    )

    assert named_failures(out) == ["test_one", "test_two"]
