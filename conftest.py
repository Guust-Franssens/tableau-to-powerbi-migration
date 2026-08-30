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

The companion half is the `serial` and `timing` markers registered in `pyproject.toml`; the tests
that carry `timing` declare it themselves, in their own source, so it survives being copied out of
this repo. See `docs/parallel-test-loop.md` for the two-tier loop that uses them.
"""

from __future__ import annotations

import os

import pytest

REQUIRED_DIST = "loadfile"

# A test carrying either of these must not run under xdist. `serial` wants an exclusive singleton
# (the interactive desktop and its UIA provider); `timing` asserts a wall-clock budget that a
# saturated box blows. Both were MEASURED to fail under parallelism - see docs/parallel-test-loop.md.
CONTENDED_MARKERS = ("serial", "timing")

INCLUDE_CONTENDED = "--include-contended"

WRONG_DIST_MESSAGE = (
    "pytest-xdist is active with --dist {dist!r}. This suite is only measured safe under "
    "--dist loadfile (issue #387).\n"
    '  fast loop   : pytest -q -n auto --dist loadfile -m "not (serial or timing)"\n'
    "  pre-PR gate : pytest -q\n"
    "loadfile keeps every test in one FILE on one worker, so file-scoped fixtures and shared "
    "scratch state do not race. To use another scheduler, change this guard deliberately and "
    "re-measure - see docs/parallel-test-loop.md."
)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the deliberate opt-out from the automatic contended-test deselection."""
    parser.addoption(
        INCLUDE_CONTENDED,
        action="store_true",
        default=False,
        help=(
            "Run `serial`/`timing` tests under xdist anyway. They were measured to fail there; "
            "this exists for deliberately stress-testing that, not for normal use."
        ),
    )


def _xdist_is_active(config: pytest.Config) -> bool:
    """Whether this process is part of a distributed run - controller or worker.

    Both halves are needed and neither is redundant. The controller carries `numprocesses`; a
    **worker** does not - measured with a probe conftest, a worker sees ``numprocesses=None
    dist='no'`` - and the worker is where collection actually happens under xdist, so a check on
    `numprocesses` alone would deselect nothing in a real parallel run.
    """
    if getattr(config.option, "numprocesses", None):
        return True
    return hasattr(config, "workerinput") or bool(os.environ.get("PYTEST_XDIST_WORKER"))


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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect every contended test whenever xdist is active. This is the MECHANISM.

    The markers alone are a convention: they only protect anything if the caller remembers
    `-m "not (serial or timing)"`. Drop half of that - `-m "not timing"` - and all seven live
    WPF/UI-Automation tests are collected under xdist again, which is precisely the configuration
    measured to fail 3 times in 8 concurrent-pair runs (`harvest=INCOMPLETE`, 30.6s against 8.08s
    serially). A guard that validates only `--dist loadfile` cannot see that, and a documented
    command is not a mechanism.

    So the exclusion no longer depends on being asked for. `--include-contended` is the deliberate
    opt-out, for stress-testing this very behaviour; it is not for normal use.

    Deselection - rather than skipping - keeps the count visible in the summary line
    (``N passed, 14 deselected``) and keeps every worker's collection identical, since each applies
    the same rule to the same items.
    """
    if not _xdist_is_active(config) or config.getoption("include_contended"):
        return
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        target = dropped if any(item.get_closest_marker(name) for name in CONTENDED_MARKERS) else kept
        target.append(item)
    if dropped:
        items[:] = kept
        config.hook.pytest_deselected(items=dropped)
