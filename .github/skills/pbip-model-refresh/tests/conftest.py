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

# The path above must be in place before the skill's own modules import, hence the E402 waiver.
from _credential_modal import CredentialDetection  # noqa: E402


def _healthy_desktop(_pid: int) -> CredentialDetection:
    """The baseline every CLI test implicitly assumes: a Desktop that is up, windowed and unblocked."""
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
