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
from typing import Any

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
ENGINE_DEPENDENCY_MARKER = "engine_dependency"
RUN_GUI = "--run-gui"
RUN_GUI_ENV = "T2P_RUN_GUI"
REQUIRE_ENGINE_TESTS_ENV = "T2P_REQUIRE_ENGINE_TESTS"
ENGINE_TESTS_NOT_CHECKED_REASON_ENV = "T2P_ENGINE_TESTS_NOT_CHECKED_REASON"
TRUTHY = {"1", "true", "yes", "on"}
EXPECTED_ENGINE_NOT_CHECKED_REASONS: frozenset[str] = frozenset(
    {
        "covered-by-pinned-engine-integration-job",
    }
)

# Exact expected skip reasons (issues #435, #436).
EXPECTED_EXACT_SKIP_REASONS: frozenset[str] = frozenset(
    {
        "deterministic tier not installed",
        "the TMDL oracle needs the .NET SDK; scripts/preflight.ps1 checks for it",
        "no PowerShell on PATH",
        "powerbi-report-author not installed (npm bridge CLI; absent on Linux CI)",
        "validator could not fetch the PBIR schema - schema checks did NOT run",
        "no real cache.abf on this machine (they are gitignored); set PBIP_REFRESH_REAL_ABF",
        "probe_desktop_credential.ps1 is a Windows-only UI Automation arbiter",
        "real Win32 EnumWindows callback is Windows-only",
        "PowerShell (pwsh.exe) not available in PATH",
        "Windows-only (relies on real PID binding against Power BI Desktop)",
        "no wind-energy example model in this repo",
        "no examples/*/fabric/*.SemanticModel corpus",
        "NTFS junction regression",
        "this git no longer reports every trailing-slash path as ignored",
        "pytest tmp_path is itself inside a git work tree on this machine",
        "not a git work tree (exported source?)",
        "pytest tmp_path sits inside a checkout on this machine",
        "the home directory is itself an unignored path inside a checkout on this machine",
        "Windows-specific path spelling",
        "the administrative share is not reachable on this machine",
        "8.3 name generation is disabled on this volume",
        "no free drive letter available for subst",
        "no unused drive letter to point at",
        "lineage check is Windows-only",
        "write-deny enforcement is an icacls ACL; the marker-only path cannot block a write",
        "this platform/account cannot create symlinks without elevation",
        "Windows filenames cannot hold undecodable bytes",
        "off-Windows behaviour of the registry read",
        "reads the Windows registry",
        "filesystem will not store a combining-character filename unchanged",
        "reproduces the WINDOWS half: Path resolves / against the current drive",
        "case-sensitive filesystem: 'FOO' and 'foo' are not the same deliverable",
        "canonical engine not installed, so its constants cannot be read",
    }
)

# Prefix-matched expected skip reasons.
EXPECTED_PREFIX_SKIP_REASONS: tuple[str, ...] = (
    "tests/fixtures/live/multi-source-live.twbx not present - generate it with scripts/make_live_source_fixture.py",
    "could not create junction: ",
    "could not create an NTFS junction: ",
    "subst failed: ",
    "canonical engine not installed, so its constants cannot be read:",
)

ENGINE_SKIP_REASONS: tuple[str, ...] = (
    "deterministic tier not installed",
    "canonical engine not installed, so its constants cannot be read:",
    "canonical engine not installed, so its constants cannot be read",
)
EXPECTED_ENGINE_SKIP_REASONS_BY_NODEID: dict[str, str] = {
    "tests/test_dax_oracle_server.py::test_extract_scalar_reads_our_row_shape": "deterministic tier not installed",
    "tests/test_dax_oracle_server.py::test_our_oracle_conforms_to_the_engines_own_contract": (
        "deterministic tier not installed"
    ),
    "tests/test_dax_oracle_server.py::test_reconcile_reaches_BOTH_verified_and_mismatch_through_us": (
        "deterministic tier not installed"
    ),
    "tests/test_dax_oracle_server.py::test_the_full_wiring_works_over_a_real_subprocess": (
        "deterministic tier not installed"
    ),
    "tests/test_harvest_download_watchdog.py::test_the_ceiling_constants_match_the_INSTALLED_engine": (
        "canonical engine not installed, so its constants cannot be read"
    ),
    "tests/test_issue_424_chart_type_pin.py::test_permanent_invariant_survives_any_future_fix["
    "issue-424-b-continuous-date-trunc]": "deterministic tier not installed",
    "tests/test_issue_424_chart_type_pin.py::test_permanent_invariant_survives_any_future_fix["
    "issue-424-c-explicit-line-mark]": "deterministic tier not installed",
    "tests/test_issue_424_chart_type_pin.py::test_permanent_invariant_survives_any_future_fix["
    "issue-424-d-explicit-bar-mark]": "deterministic tier not installed",
    "tests/test_issue_424_chart_type_pin.py::test_permanent_invariant_survives_any_future_fix["
    "issue-424-h-non-date-dimension]": "deterministic tier not installed",
    "tests/test_issue_424_chart_type_pin.py::test_the_engine_reports_the_rebuilt_line_cleanly": (
        "deterministic tier not installed"
    ),
    "tests/test_issue_424_chart_type_pin.py::test_the_fixture_set_discriminates_between_candidate_remedies": (
        "deterministic tier not installed"
    ),
    "tests/test_issue_424_chart_type_pin.py::test_the_shared_visual_id_is_input_derived_not_an_engine_inconsistency": (
        "deterministic tier not installed"
    ),
    "tests/test_issue_424_chart_type_pin.py::test_variant_a_emits_a_line_and_keeps_the_reproduction_binding": (
        "deterministic tier not installed"
    ),
    "tests/test_upstream_repro_pins.py::test_issue_166_pins_negative_custom_sql_binding_shape": (
        "deterministic tier not installed"
    ),
    "tests/test_upstream_repro_pins.py::test_issue_168_pins_partial_dispatcher_with_disclosure": (
        "deterministic tier not installed"
    ),
    "tests/test_upstream_repro_pins.py::test_issue_171_pins_partial_measure_names_parameter_gap": (
        "deterministic tier not installed"
    ),
}


def is_expected_skip_reason(reason: str) -> bool:
    """Whether a skip reason is in the repository's baseline of expected skips (issue #436)."""
    clean = reason.strip()
    if clean in EXPECTED_EXACT_SKIP_REASONS:
        return True
    return any(clean.startswith(prefix) for prefix in EXPECTED_PREFIX_SKIP_REASONS)


def is_engine_skip(reason: str) -> bool:
    """Whether a skip reason is an engine-dependent test skip (issue #435)."""
    clean = reason.strip()
    return any(clean == r or clean.startswith(r) for r in ENGINE_SKIP_REASONS)


def engine_tests_are_required() -> bool:
    """Whether this run must execute engine-dependent tests rather than reporting NOT_CHECKED."""
    return os.environ.get(REQUIRE_ENGINE_TESTS_ENV, "").strip().lower() in TRUTHY


def engine_tests_not_checked_reason() -> str | None:
    """The explicit allowed reason this run is allowed to report engine tests as NOT_CHECKED."""
    reason = os.environ.get(ENGINE_TESTS_NOT_CHECKED_REASON_ENV, "").strip()
    return reason or None


def _set_engine_ci_options(config: pytest.Config) -> None:
    """Snapshot CI-only engine env vars, then keep them out of nested pytest runs."""
    setattr(config, "_t2p_require_engine_tests", engine_tests_are_required())
    setattr(config, "_t2p_engine_tests_not_checked_reason", engine_tests_not_checked_reason())
    os.environ.pop(REQUIRE_ENGINE_TESTS_ENV, None)
    os.environ.pop(ENGINE_TESTS_NOT_CHECKED_REASON_ENV, None)


def _engine_tests_are_required(config: pytest.Config) -> bool:
    """Whether this pytest session must execute engine-dependent tests."""
    return bool(getattr(config, "_t2p_require_engine_tests", False))


def _engine_tests_not_checked_reason(config: pytest.Config) -> str | None:
    """The NOT_CHECKED reason captured for this pytest session, if any."""
    return getattr(config, "_t2p_engine_tests_not_checked_reason", None)


def _engine_dependency_map(config: pytest.Config) -> dict[str, str]:
    """Collected engine-dependent tests and the exact skip reason each declares."""
    return getattr(config, "_t2p_engine_dependency_reasons", {})


def _set_engine_dependency_map(config: pytest.Config, reasons: dict[str, str]) -> None:
    """Store collected engine-dependent tests on the config for terminal-summary validation."""
    setattr(config, "_t2p_engine_dependency_reasons", reasons)


def _collected_nodeids(config: pytest.Config) -> set[str]:
    """All collected tests, used to decide whether this run covers the engine denominator."""
    return getattr(config, "_t2p_collected_nodeids", set())


def _set_collected_nodeids(config: pytest.Config, nodeids: set[str]) -> None:
    """Store all collected node ids on the config for terminal-summary validation."""
    setattr(config, "_t2p_collected_nodeids", nodeids)


def _map_delta_message(label: str, expected: dict[str, str], actual: dict[str, str]) -> str:
    """Human-readable exact-map mismatch for engine dependency checks."""
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(nodeid for nodeid, reason in actual.items() if nodeid in expected and expected[nodeid] != reason)
    return f"{label} missing={missing}, extra={extra}, changed={changed}"


def _engine_denominator_failure(config: pytest.Config, collected_engine_dependencies: dict[str, str]) -> str | None:
    """Why the collected engine-dependent set is not the reviewed denominator, or None."""
    if not (_engine_tests_are_required(config) or _engine_tests_not_checked_reason(config) is not None):
        return None
    if not set(EXPECTED_ENGINE_SKIP_REASONS_BY_NODEID) & _collected_nodeids(config):
        return None
    if collected_engine_dependencies == EXPECTED_ENGINE_SKIP_REASONS_BY_NODEID:
        return None
    return _map_delta_message(
        "engine-dependent test collection does not match the reviewed denominator.",
        EXPECTED_ENGINE_SKIP_REASONS_BY_NODEID,
        collected_engine_dependencies,
    )


def _engine_skip_failure(config: pytest.Config, engine_skips: dict[str, str]) -> str | None:
    """Why skipped engine-dependent tests fail this run, or None when they are explicit NOT_CHECKED."""
    if not engine_skips:
        return None
    not_checked_reason = _engine_tests_not_checked_reason(config)
    if _engine_tests_are_required(config):
        return (
            f"{REQUIRE_ENGINE_TESTS_ENV}=1, so engine-dependent tests must execute; "
            "skipping them is a CI/test-environment failure."
        )
    if not_checked_reason not in EXPECTED_ENGINE_NOT_CHECKED_REASONS:
        allowed = ", ".join(sorted(EXPECTED_ENGINE_NOT_CHECKED_REASONS))
        return (
            f"engine-dependent tests skipped without an allowed {ENGINE_TESTS_NOT_CHECKED_REASON_ENV}. "
            f"Set one of: {allowed}."
        )
    if engine_skips != EXPECTED_ENGINE_SKIP_REASONS_BY_NODEID:
        return _map_delta_message(
            "engine-dependent tests are NOT_CHECKED only when the exact node-id-to-reason map matches.",
            EXPECTED_ENGINE_SKIP_REASONS_BY_NODEID,
            engine_skips,
        )
    return None


def _write_engine_skip_summary(
    terminalreporter: Any, config: pytest.Config, engine_skips: dict[str, str], failure: str | None
) -> None:
    """Render engine-dependent skips as either explicit NOT_CHECKED or a failing skip block."""
    terminalreporter.write_sep(
        "!" if failure else "=",
        (
            f"ENGINE-DEPENDENT TESTS SKIPPED ({len(engine_skips)} test cases did not run)"
            if failure
            else f"ENGINE-DEPENDENT TESTS NOT_CHECKED ({len(engine_skips)} test cases did not run)"
        ),
        yellow=not failure,
        red=bool(failure),
        bold=True,
    )
    lines = [
        "The deterministic conversion engine plugin (tableau-fabric-skills) is not installed.",
        f"{len(engine_skips)} test cases requiring the engine plugin were skipped under @requires_engine:",
        *[f"  - {nodeid}: {engine_skips[nodeid]}" for nodeid in sorted(engine_skips)],
    ]
    lines.append(failure or f"NOT_CHECKED reason: {_engine_tests_not_checked_reason(config)}")
    terminalreporter.write_line("\n".join(lines), yellow=not failure, red=bool(failure))
    terminalreporter.write_sep("!" if failure else "=", yellow=not failure, red=bool(failure))


def _write_engine_denominator_failure(terminalreporter: Any, failure: str) -> None:
    """Render an engine-dependent denominator mismatch when no engine test skipped."""
    terminalreporter.write_sep(
        "!",
        "ENGINE-DEPENDENT TEST DENOMINATOR MISMATCH",
        red=True,
        bold=True,
    )
    terminalreporter.write_line(failure, red=True)
    terminalreporter.write_sep("!", red=True)


def _extract_skip_reason(rep: Any) -> str:
    """Extract normalized skip reason from a TestReport object."""
    if isinstance(rep.longrepr, tuple) and len(rep.longrepr) >= 3:
        reason = str(rep.longrepr[2])
    elif isinstance(rep.longrepr, str):
        reason = rep.longrepr
    elif hasattr(rep, "longreprtext") and rep.longreprtext:
        reason = rep.longreprtext
    else:
        reason = str(rep.longrepr or "")
    if reason.startswith("Skipped: "):
        reason = reason[len("Skipped: ") :]
    return reason.strip()


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
    """Refuse a parallel run that silently dropped `--dist loadfile`, and ensure failures/errors remain visible.

    The early return on a falsy `numprocesses` is load-bearing in two directions, not one. It covers
    plain serial runs, where the option is unset - and it covers every xdist **worker**, which is not
    an obvious case. Measured with a probe conftest that logged its own options: the controller sees
    ``numprocesses=2 dist='loadfile'`` while each worker sees ``numprocesses=None dist='no'``. So a
    "simpler" guard that only compared `dist` would raise a UsageError inside every worker of a
    perfectly valid run. `test_a_parallel_run_with_loadfile_is_allowed` is what pins that down.

    `numprocesses` may still be the string ``"auto"`` depending on hook ordering, so this tests
    truthiness rather than comparing to an int.
    """
    _set_engine_ci_options(config)
    # Augment reportchars so 'f' and 'E' are always included even if caller passes -rs (issue #436).
    reportchars = getattr(config.option, "reportchars", "")
    for char in ("f", "E"):
        if char not in reportchars:
            reportchars += char
    config.option.reportchars = reportchars
    config.addinivalue_line(
        "markers",
        f"{ENGINE_DEPENDENCY_MARKER}(expected_skip_reason): test depends on the deterministic engine",
    )

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
    _set_collected_nodeids(config, {item.nodeid for item in items})
    engine_dependencies: dict[str, str] = {}
    for item in items:
        marker = item.get_closest_marker(ENGINE_DEPENDENCY_MARKER)
        if marker is None:
            continue
        reason = str(marker.kwargs.get("expected_skip_reason", "")).strip()
        engine_dependencies[item.nodeid] = reason
    _set_engine_dependency_map(config, engine_dependencies)

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


def pytest_terminal_summary(
    terminalreporter: Any,
    exitstatus: int,  # pylint: disable=unused-argument
    config: pytest.Config,
) -> None:
    """Assert skip reasons match expected baseline and report engine skips loudly (issues #435, #436)."""
    skipped_reports = terminalreporter.stats.get("skipped", [])

    unexpected_skips: list[tuple[str, str]] = []
    engine_skips: dict[str, str] = {}
    collected_engine_dependencies = _engine_dependency_map(config)

    for rep in skipped_reports:
        reason = _extract_skip_reason(rep)
        if rep.nodeid in collected_engine_dependencies:
            engine_skips[rep.nodeid] = reason
        if not is_expected_skip_reason(reason):
            unexpected_skips.append((rep.nodeid, reason))

    engine_skip_failure = _engine_skip_failure(config, engine_skips) or _engine_denominator_failure(
        config, collected_engine_dependencies
    )
    if engine_skips:
        _write_engine_skip_summary(terminalreporter, config, engine_skips, engine_skip_failure)
    elif engine_skip_failure:
        _write_engine_denominator_failure(terminalreporter, engine_skip_failure)

    # Fail the run if any unexpected skip reason is encountered (issue #436)
    if engine_skip_failure or unexpected_skips:
        if engine_skip_failure:
            # pylint: disable=protected-access
            terminalreporter._session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if unexpected_skips:
        terminalreporter.write_sep(
            "!",
            f"UNEXPECTED TEST SKIPS DETECTED ({len(unexpected_skips)} tests)",
            red=True,
            bold=True,
        )
        terminalreporter.write_line(
            "Every skipped test must match a known per-reason expected-skip baseline (issue #436).\n"
            "The following tests were skipped with unexpected or unregistered reasons:\n"
            + "\n".join(f"  - {nodeid}: {reason!r}" for nodeid, reason in unexpected_skips),
            red=True,
        )
        terminalreporter.write_sep("!", red=True)
        # pylint: disable=protected-access
        terminalreporter._session.exitstatus = pytest.ExitCode.TESTS_FAILED
