"""Repo-wide pytest configuration.

This file exists for exactly one reason: **`--dist loadfile` is load-bearing and nothing enforced
it.** Issue #387 measured the suite at 740s serial versus ~215s under `-n auto --dist loadfile` on
22 logical cores, with byte-identical pass/fail counts across repeated runs - but every one of those
runs carried `--dist loadfile`. Plain `-n auto` means `--dist load`, which spreads a single test
*file* across workers, so module-scoped fixtures, per-file caches and shared on-disk scratch race.
That configuration has never been measured here, and the failure it produces is a flaky suite, which
is worse than a slow one: a flaky suite poisons every agent's judgement at once.

`-n auto` is three characters shorter than `-n auto --dist loadfile` and will be typed. So the
prose warning in the issue is converted into a gate: a parallel run without `loadfile` is a
`UsageError` (exit 4) naming the two supported commands.

It lives at the repo ROOT, not in `tests/`, because pytest only loads a `conftest.py` for paths
underneath it. `pyproject.toml` declares `testpaths = ["tests", ".github/skills"]`, and the live
Power BI Desktop / UI-Automation tests - the ones the parallel tier is most dangerous for - are in
the second root. A `tests/conftest.py` would leave `pytest .github/skills/... -n 4` ungated, which
is precisely the run that needs gating.

The companion half is the `serial` marker registered in `pyproject.toml`; see
`docs/parallel-test-loop.md` for the two-tier loop that uses it.
"""

from __future__ import annotations

import pytest

REQUIRED_DIST = "loadfile"

# Tests that assert a sub-second WALL-CLOCK budget. They measure the machine as much as the code, so
# a box running 22 xdist workers (plus whatever else) fails them without anything being wrong.
#
# Measured (issue #387), eight whole-suite `-n auto --dist loadfile` runs against one serial run and
# 30 serial repetitions of the owning file (15 quiet, 15 beside a live 22-worker suite):
#   * exactly ONE node id ever disagreed, out of 2564;
#   * `test_refresh_main_returns_credential_missing_fast_at_t0` took 0.941s against its 0.5s budget
#     on worker gw9, in the slowest of the eight runs (305s vs a 160-217s median);
#   * it never failed in any serial repetition, loaded or quiet.
# So this is not a `--dist loadfile` isolation failure - no shared state raced, and the deselection
# below is not a substitute for one. It is CPU starvation, and the durable fix is in the tests: a
# budget assertion wants a monotonic-clock floor it controls, not a share of a contended machine.
# Until their owners change them, the parallel tier deselects them and the serial tier still runs
# them - that is what tier 2 is for.
#
# ⚠️ Marked by NODE ID, which a rename would silently break, and blind to a new budget test added
# elsewhere. `tests/test_parallel_test_loop.py` re-derives this set from the suite's own AST and
# fails on either drift, so the list cannot rot quietly.
TIMING_BUDGET_FILE = ".github/skills/pbip-model-refresh/tests/test_credential_modal_detection.py"

TIMING_BUDGET_TESTS = frozenset(
    f"{TIMING_BUDGET_FILE}::{name}"
    for name in (
        "test_direct_refresh_returns_credential_missing_fast_at_t0",
        "test_direct_refresh_returns_blocked_by_dialog_fast_at_t0",
        "test_direct_refresh_raises_desktop_gone_fast_at_t0",
        "test_refresh_poll_catches_late_modal",
        "test_refresh_main_returns_credential_missing_fast_at_t0",
        "test_probe_query_returns_credential_missing_fast_at_t0",
    )
)

WRONG_DIST_MESSAGE = (
    "pytest-xdist is active with --dist {dist!r}. This suite is only measured safe under "
    "--dist loadfile (issue #387).\n"
    '  fast loop   : pytest -q -n auto --dist loadfile -m "not (serial or timing)"\n'
    "  pre-PR gate : pytest -q\n"
    "loadfile keeps every test in one FILE on one worker, so file-scoped fixtures and shared "
    "scratch state do not race. To use another scheduler, change this guard deliberately and "
    "re-measure - see docs/parallel-test-loop.md."
)


def pytest_configure(config: pytest.Config) -> None:
    """Refuse a parallel run that silently dropped `--dist loadfile`.

    The early return on a falsy `numprocesses` is load-bearing in two directions, not one. It covers
    plain serial runs, where the option is unset - and it covers every xdist **worker**, which is not
    an obvious case. Measured with a probe conftest that logged its own options: the controller sees
    ``numprocesses=2 dist='loadfile'`` while each worker sees ``numprocesses=None dist='no'``. So a
    "simpler" guard that only compared `dist` would raise a UsageError inside every worker of a
    perfectly valid run. `test_a_parallel_run_with_loadfile_is_allowed` is what pins that down.

    `numprocesses` may still be the string ``"auto"`` depending on hook ordering, so this tests
    truthiness rather than comparing to an int.
    """
    if not getattr(config.option, "numprocesses", None):
        return
    dist = getattr(config.option, "dist", "no")
    if dist == REQUIRED_DIST:
        return
    raise pytest.UsageError(WRONG_DIST_MESSAGE.format(dist=dist))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply `timing` to the wall-clock-budget tests so `-m` can deselect them.

    A marker added here is only useful if it lands **before** pytest's own `-m` filtering, which
    `_pytest.mark` performs from its own `@hookimpl(tryfirst=True)`. Measured on pytest 9.1.1, both
    a `tryfirst` hook and a plain one deselect correctly, so `tryfirst` here is defensive rather than
    load-bearing on this version - removing it is an equivalent mutation and
    `test_the_timing_marker_actually_deselects_those_tests` does not distinguish it. That test does
    catch the failure that matters: a marker that never reaches the filter at all.

    Under xdist this runs inside every worker, and every worker applies the same set, so collection
    stays identical across them. Deselected counts stay visible in the summary line
    (``N passed, 6 deselected``) - the exclusion is never silent.
    """
    for item in items:
        if item.nodeid in TIMING_BUDGET_TESTS:
            item.add_marker(pytest.mark.timing)
