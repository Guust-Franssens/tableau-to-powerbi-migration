"""Make the skill's own `scripts/` importable, from wherever this folder was copied.

The whole point of bundling the tests next to the scripts is that the pair travels as one unit: copy
`pbip-model-refresh/` into a Qlik or Cognos migration repo (or a global skill location) and the tests
still run there. That only holds if the import path is resolved **relative to this file** - an
absolute path, or a walk up to a repo root, would silently re-bind the tests to a host repo's
`scripts/` folder and prove nothing about the copy.

It also pins the DEFAULT Desktop-inspection state every test runs against - see
:func:`healthy_desktop_state` for why that has to be explicit rather than inherited from the host OS,
and it owns the **`gui` exclusion** for exactly the same portability reason - see
:func:`pytest_collection_modifyitems`.
"""

import os
import sys
from pathlib import Path

import pytest

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

# Kept in step with the host repo's root `conftest.py` BY DUPLICATION, on purpose: this folder is
# copied out of the repo and run with the repo unimportable, so it cannot import a shared helper.
GUI_MARKER = "gui"
RUN_GUI = "--run-gui"
RUN_GUI_ENV = "T2P_RUN_GUI"
TRUTHY = {"1", "true", "yes", "on"}


def pytest_configure(config: pytest.Config) -> None:
    """Register `timing`, `serial` and `gui` HERE so the markers travel with the bundle.

    **`timing`** - a handful of tests in this folder assert a sub-second wall-clock budget on an
    operation that takes ~0.03s. Measured (issue #387) under 22 concurrent pytest-xdist workers, one
    of them took **0.941s against a 0.5s budget** - a 31x inflation of a very short measured window,
    which is scheduler starvation rather than proportional slowdown. Widening the bound is therefore
    not a fix; the host repo's parallel test tier deselects them with `-m "not timing"` instead.

    **`serial`** - seven tests here launch a real WPF application and drive it through UI Automation.
    The interactive desktop and its UIA provider are a singleton, so two of these running at once
    degrade each other. Measured (issue #387) across three rounds of two whole-suite parallel runs
    started concurrently: `test_credential_text_beyond_the_element_cap_convicts_when_the_cap_allows_it`
    failed in **3 of 6** runs, every time with `harvest=INCOMPLETE` and `VERDICT: DIALOG_UNREADABLE` -
    the probe degrading safely, and the test's stricter assertion correctly refusing it. Not one of
    them failed in seven runs that were not concurrently paired, so this needs the pairing.

    **`gui`** - ten tests here spawn a real top-level window (seven a WPF app, three native
    `CreateWindowExW` windows), which steals focus from whoever is at the keyboard; issue #447 is an
    operator watching that happen for two days mid-demo. Registering it here is not cosmetic:
    measured before this existed, the copied-out run emitted seven `PytestUnknownMarkWarning`s, and
    an unregistered marker is one `--strict-markers` away from being an error in someone else's repo.

    Registering them in this conftest rather than a host `pyproject.toml` is the whole point: copy
    this folder into another repo and `-m "not (serial or timing)"` still works, with no
    unregistered-marker warning.
    """
    config.addinivalue_line(
        "markers",
        "timing: asserts a wall-clock budget, so a saturated box fails it",
    )
    config.addinivalue_line(
        "markers",
        "serial: contends for a singleton external resource; must not run beside another such test",
    )
    config.addinivalue_line(
        "markers",
        "gui: spawns a real top-level window; needed for UI-Automation coverage, opt-in only",
    )


def _gui_is_opted_in(config: pytest.Config) -> bool:
    """Whether this run explicitly asked for the window-spawning tests.

    `config.getoption(..., default=False)` rather than a bare lookup, because `--run-gui` is
    registered by the HOST repo's root `conftest.py` and that file does not travel with this folder.
    In the host repo the flag answers; in a copied-out bundle only the environment variable does.
    """
    if os.environ.get(RUN_GUI_ENV, "").strip().lower() in TRUTHY:
        return True
    return bool(config.getoption(RUN_GUI, default=False))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect the window-spawning tests unless this run asked for them. Travels with the bundle.

    ⚠️ **The host repo's copy of this rule provably does not reach here.** `tests/test_skills.py`
    copies this folder to a temp directory and runs it as a subprocess with `cwd` outside the repo
    and `PYTEST_ADDOPTS` cleared, precisely so the copy proves its own portability - so the root
    `conftest.py` and `pyproject.toml` are both absent by construction. Measured with every spawn
    site instrumented to raise: that nested run reached **all ten** of them (`10 failed, 279 passed`),
    while the outer suite's own summary reported **zero** deselections. Invisible, which is worse
    than loud.

    A hook rather than a marker expression, for the same reason as in the host repo: a command-line
    `-m` REPLACES an ini expression instead of composing with it, so a `-m "not slow"` anywhere
    upstream would put every window back. A hook runs after pytest has applied the caller's `-m`.

    Opt in with `T2P_RUN_GUI=1` (portable), or `--run-gui` inside the host repo.
    """
    if _gui_is_opted_in(config):
        return
    kept = [item for item in items if item.get_closest_marker(GUI_MARKER) is None]
    dropped = [item for item in items if item.get_closest_marker(GUI_MARKER) is not None]
    if dropped:
        items[:] = kept
        config.hook.pytest_deselected(items=dropped)


# The path above must be in place before the skill's own modules import, hence the E402 waiver.
from _credential_modal import CredentialDetection  # noqa: E402


def _healthy_desktop(_pid: int, **_kwargs) -> CredentialDetection:
    """The baseline every CLI test implicitly assumes: a Desktop that is up, windowed and unblocked.

    ``**_kwargs`` swallows ``probe_desktop_query._credential_state``'s keyword-only ``in_flight``
    (issue #376) so ONE stub still stands in for both entry points' signatures.
    """
    return CredentialDetection()


# Read by `test_the_two_entry_points_get_an_explicit_desktop_state_baseline`, so the fixture below
# cannot silently stop applying. Asserting on the RETURNED VALUE could not do that job: on Linux the
# real `_credential_state` also returns a bare `CredentialDetection()`, so a value check is a test that
# cannot fail on the one platform CI runs.
_healthy_desktop.is_test_baseline = True


@pytest.fixture(autouse=True)
def healthy_desktop_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test an EXPLICIT, platform-independent Desktop-inspection state.

    Without this, `_credential_state` is whatever the host OS makes it, because both entry points open
    with ``if os.name != "nt": return CredentialDetection()``. A test that drives `main(["--pid","111"])`
    without stubbing it therefore exercises *different code on different platforms*: a benign healthy
    state on Linux, and - since pid 111 does not exist - a real `process_gone` detection on Windows.
    Measured 2026-08-15: that split let 17 tests pass green on `ubuntu-latest` CI while failing on
    Windows, the only platform this bundle supports (`SKILL.md`: "Windows only"), against a *correct*
    production change that started honouring `process_gone` at t=0.

    Removing the `os.name` guard instead would make it worse, not better: on Linux
    `_enumerate_pid_windows_with_count` raises `Win32EnumerationError`, which `inspect_credential_modal`
    converts to `unknown_reason`, so every un-stubbed CLI test would exit 3 there. The guard is what
    keeps the module importable off Windows; the defect was the tests' reliance on its return value.

    The seam this uses already existed - `_credential_state` is a module-level name, and
    `inspect_credential_modal` takes injectable `enumerate_windows` / `process_is_alive` callables - so
    a test that wants the REAL detector logic on any platform composes it explicitly (see
    `test_credential_modal_detection.py`'s injected-primitive tests). This fixture only fixes the
    default. Any test needing another state overrides it with its own `monkeypatch.setattr`, which wins
    because the test body runs after this fixture.
    """
    import probe_desktop_query  # pylint: disable=import-outside-toplevel
    import refresh_pbip_model  # pylint: disable=import-outside-toplevel

    for module in (refresh_pbip_model, probe_desktop_query):
        monkeypatch.setattr(module, "_credential_state", _healthy_desktop)
