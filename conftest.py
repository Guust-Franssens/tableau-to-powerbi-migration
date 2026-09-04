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

This file also owns the **`gui`** exclusion (issue #447): a `gui` test spawns a real top-level window
and steals focus, so it is deselected in every tier unless `--run-gui` / `T2P_RUN_GUI=1` asks for it.
That lives here, in a collection hook, rather than in `addopts = "-m 'not gui'"` - because a
command-line `-m` REPLACES an ini marker expression instead of composing with it, so
`pytest -m "not slow"` (a command this repo's own docs recommend) put all of them back.
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

# A `gui` test spawns a REAL top-level window and steals focus on whatever machine runs it. Unlike
# `serial`/`timing` this is excluded from EVERY tier, not just the parallel one, so the exclusion
# below is unconditional and the opt-in has to be explicit.
GUI_MARKER = "gui"
RUN_GUI = "--run-gui"
RUN_GUI_ENV = "T2P_RUN_GUI"
TRUTHY = {"1", "true", "yes", "on"}

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
    parser.addoption(
        RUN_GUI,
        action="store_true",
        default=False,
        help=(
            "Run `gui` tests, which spawn a real top-level window and steal focus. CI opts in with "
            f"this; so does anyone deliberately working on the credential probe. `{RUN_GUI_ENV}=1` "
            "does the same thing for a nested pytest that cannot be given a flag."
        ),
    )


def gui_is_opted_in(config: pytest.Config) -> bool:
    """Whether this run was explicitly asked for the window-spawning tests.

    Two spellings, and both are load-bearing. The FLAG is what a human or a CI step types. The ENV
    VAR is for a **nested** pytest that nobody can hand a flag to - `tests/test_skills.py` copies the
    `pbip-model-refresh` bundle to a temp directory and runs it as a subprocess there, outside this
    repo, where neither this file nor `pyproject.toml` exists (measured: the copied run reached all
    ten spawn sites while the outer summary reported zero deselections).
    """
    if os.environ.get(RUN_GUI_ENV, "").strip().lower() in TRUTHY:
        return True
    return bool(config.getoption(RUN_GUI, default=False))


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
    """Deselect the tests that must not run in this configuration. This is the MECHANISM.

    Two independent exclusions, deliberately in one pass so the summary reports one honest count.

    **`serial`/`timing`, whenever xdist is active.** The markers alone are a convention: they only
    protect anything if the caller remembers `-m "not (serial or timing)"`. Drop half of that -
    `-m "not timing"` - and all seven live WPF/UI-Automation tests are collected under xdist again,
    which is precisely the configuration measured to fail 3 times in 8 concurrent-pair runs
    (`harvest=INCOMPLETE`, 30.6s against 8.08s serially). A guard that validates only
    `--dist loadfile` cannot see that, and a documented command is not a mechanism.
    `--include-contended` is the deliberate opt-out, for stress-testing this very behaviour.

    **`gui`, always, unless opted in.** ⚠️ This used to be `addopts = "-m 'not gui'"` in
    `pyproject.toml`, and that was FAIL-OPEN: a command-line `-m` **replaces** the ini expression
    rather than composing with it. Measured on the bundle's 309 tests - default `302/309`,
    `-m gui` `7/309`, and `-m "not slow"` **`309/309`, every window-spawning test back**. Our own
    `docs/offline-mock-harness.md` documents `pytest -q -m "not slow"`, so following the repo's
    documentation re-opened the windows. A collection hook COMPOSES with `-m` instead: pytest applies
    the caller's expression first, and whatever survives it still passes through here.

    Deselection - rather than skipping - keeps the count visible in the summary line
    (``N passed, 14 deselected``) and keeps every worker's collection identical, since each applies
    the same rule to the same items.
    """
    drop_gui = not gui_is_opted_in(config)
    drop_contended = _xdist_is_active(config) and not config.getoption("include_contended")
    if not (drop_gui or drop_contended):
        return
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        unwanted = (drop_gui and item.get_closest_marker(GUI_MARKER) is not None) or (
            drop_contended and any(item.get_closest_marker(name) for name in CONTENDED_MARKERS)
        )
        (dropped if unwanted else kept).append(item)
    if dropped:
        items[:] = kept
        config.hook.pytest_deselected(items=dropped)
