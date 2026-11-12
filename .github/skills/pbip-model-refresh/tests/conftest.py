"""Make the skill's own `scripts/` importable, from wherever this folder was copied.

The whole point of bundling the tests next to the scripts is that the pair travels as one unit: copy
`pbip-model-refresh/` into a Qlik or Cognos migration repo (or a global skill location) and the tests
still run there. That only holds if the import path is resolved **relative to this file** - an
absolute path, or a walk up to a repo root, would silently re-bind the tests to a host repo's
`scripts/` folder and prove nothing about the copy.

It also pins the DEFAULT Desktop-inspection state every test runs against - see
:func:`healthy_desktop_state` for why that has to be explicit rather than inherited from the host OS.
"""

import sys
from pathlib import Path

import pytest

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))


def pytest_configure(config: pytest.Config) -> None:
    """Register `timing` HERE so the marker travels with the bundle.

    **`timing`** - a handful of tests in this folder assert a sub-second wall-clock budget on an
    operation that takes ~0.03s. Measured (issue #387) under 22 concurrent pytest-xdist workers, one
    of them took **0.941s against a 0.5s budget** - a 31x inflation of a very short measured window,
    which is scheduler starvation rather than proportional slowdown. Widening the bound is therefore
    not a fix; the host repo's parallel test tier deselects them with `-m "not timing"` instead.

    Registering it in this conftest rather than a host `pyproject.toml` is the whole point: copy
    this folder into another repo and `-m "not timing"` still works, with no unregistered-marker
    warning. (The `serial` and `gui` markers were retired with the UI Automation backend in #831.)
    """
    config.addinivalue_line(
        "markers",
        "timing: asserts a wall-clock budget, so a saturated box fails it",
    )


# The path above must be in place before the skill's own modules import, hence the E402 waiver.
from _desktop_observer import Observation  # noqa: E402


def _healthy_desktop(_pid: int, **_kwargs) -> Observation:
    """The baseline every CLI test implicitly assumes: a Desktop that is up, windowed and unblocked.

    ``**_kwargs`` swallows ``probe_desktop_query._credential_state``'s keyword-only ``in_flight``
    (issue #376) so ONE stub still stands in for both entry points' signatures.
    """
    return Observation(
        pid=str(_pid),
        process_started_at=_kwargs.get("expected_process_start") or "638963424000000001",
    )


# Read by `test_the_two_entry_points_get_an_explicit_desktop_state_baseline`, so the fixture below
# cannot silently stop applying. Asserting on the RETURNED VALUE could not do that job: on Linux the
# real `_credential_state` also returns a bare `Observation()`, so a value check is a test that
# cannot fail on the one platform CI runs.
_healthy_desktop.is_test_baseline = True


@pytest.fixture(autouse=True)
def healthy_desktop_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test an EXPLICIT, platform-independent Desktop-inspection state.

    Without this, `_credential_state` is whatever the host OS makes it, because both entry points open
    with ``if os.name != "nt": return Observation()``. A test that drives `main(["--pid","111"])`
    without stubbing it therefore exercises *different code on different platforms*: a benign healthy
    state on Linux, and - since pid 111 does not exist - a real `process_gone` detection on Windows.
    Measured 2026-08-15: that split let 17 tests pass green on `ubuntu-latest` CI while failing on
    Windows, the only platform this bundle supports (`SKILL.md`: "Windows only"), against a *correct*
    production change that started honouring `process_gone` at t=0.

    Removing the `os.name` guard instead would make it worse, not better: on Linux
    `_enumerate_pid_windows_with_count` raises `Win32EnumerationError`, which `inspect_desktop_observer`
    converts to `unknown_reason`, so every un-stubbed CLI test would exit 3 there. The guard is what
    keeps the module importable off Windows; the defect was the tests' reliance on its return value.

    The seam this uses already existed - `_credential_state` is a module-level name, and
    `inspect_desktop_observer` takes injectable `enumerate_windows` / `process_is_alive` callables - so
    a test that wants the REAL detector logic on any platform composes it explicitly (see
    `test_desktop_observer.py`'s injected-primitive tests). This fixture only fixes the
    default. Any test needing another state overrides it with its own `monkeypatch.setattr`, which wins
    because the test body runs after this fixture.
    """
    import probe_desktop_query  # pylint: disable=import-outside-toplevel
    import refresh_pbip_model  # pylint: disable=import-outside-toplevel

    for module in (refresh_pbip_model, probe_desktop_query):
        monkeypatch.setattr(module, "_credential_state", _healthy_desktop)


class _FakeAdomdConnection:  # pylint: disable=too-few-public-methods
    """What the upfront ADOMD load check returns by default: a type nothing in a unit test opens."""

    def __init__(self, *_args) -> None:
        raise AssertionError("a unit test opened a real ADOMD connection")


@pytest.fixture(autouse=True)
def offline_adomd_and_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never load the real ADOMD and never let the progress stream touch a real engine or window.

    The CLI's upfront `_load_adomd(port)` check is stubbed to succeed; the progress reader's
    connection fails (so it prints `process_jobs=?`) and its dialog field reads `none`. Tests of
    those seams override them with their own `monkeypatch.setattr`.
    """
    import refresh_pbip_model  # pylint: disable=import-outside-toplevel

    def no_connection(_context):
        raise OSError("no engine in a unit test")

    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda _port=None: _FakeAdomdConnection)
    monkeypatch.setattr(refresh_pbip_model, "_open_progress_connection", no_connection)
    monkeypatch.setattr(refresh_pbip_model, "_dialog_field", lambda _context: "none")
