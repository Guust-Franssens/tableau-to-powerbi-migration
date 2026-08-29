"""Offline tests for the Desktop credential-modal fast path and polling loop."""

from __future__ import annotations

# These tests deliberately monkeypatch module-internal seams so no Desktop, network, or Fabric
# dependency is required.
# pylint: disable=protected-access,too-few-public-methods,invalid-name

import threading
import time
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import _credential_modal
import probe_desktop_query
import refresh_pbip_model
from _credential_modal import (
    BlockingDialog,
    CredentialDetection,
    CredentialModal,
    CredentialUnknownError,
    DesktopGoneError,
    DesktopUnreadyError,
    DialogBlockedError,
    DesktopWindow,
    _enumerate_pid_windows_with_count,
    inspect_credential_modal,
    join_with_credential_poll,
)
from refresh_pbip_model import CredentialMissingError, refresh


class ParkedConnection:
    """ADOMD stand-in whose command never returns until the test releases it."""

    def __init__(self, released: threading.Event) -> None:
        self.released = released

    def Open(self) -> None:
        """Match the ADOMD API surface."""

    def CreateCommand(self):
        """Return a command that blocks until released."""
        return ParkedCommand(self.released)

    def Close(self) -> None:
        """Match the ADOMD API surface."""


class ParkedCommand:
    """Command stand-in that ignores CommandTimeout."""

    CommandText = ""
    CommandTimeout = 0

    def __init__(self, released: threading.Event) -> None:
        self.released = released

    def ExecuteNonQuery(self) -> None:
        """Block long enough that only the outer poll/deadline can end the test."""
        self.released.wait(timeout=600)


@pytest.fixture(name="parked")
def parked_fixture(monkeypatch):
    """Wire refresh() to a parked ADOMD connection."""
    released = threading.Event()
    conn = ParkedConnection(released)
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: conn)
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_WALL_CLOCK_GRACE_SECONDS", 0.15)
    yield released
    released.set()


def modal() -> CredentialModal:
    """A credential dialog matching the measured incident window."""
    return CredentialModal(
        "Please specify how to connect to this data source",
        DesktopWindow("", "WindowsForms10.Window.20008", 702, 355, ("Please specify how to connect",)),
    )


def modal_state() -> CredentialDetection:
    """CredentialDetection containing the standard modal."""
    return CredentialDetection(modal=modal())


def blocking_dialog() -> BlockingDialog:
    """Unreadable owned dialog matching the live SQL credential-dialog shape."""
    return BlockingDialog(DesktopWindow("", "WindowsForms10.Window.20008", 702, 355, ()))


def blocking_state() -> CredentialDetection:
    """CredentialDetection containing an unreadable blocking dialog."""
    return CredentialDetection(blocking_dialog=blocking_dialog())


def minimized_main_windows() -> list[DesktopWindow]:
    """The measured minimized-owner shape: a small, iconic Desktop main window and nothing else."""
    return [
        DesktopWindow("Report", "WindowsForms10.Window.8.app.0.1a2b3c", 159, 27, ("Report",), minimized=True),
    ]


def restored_main_windows() -> list[DesktopWindow]:
    """The same owner restored: full-size, not iconic, and - measured - the dialog does NOT return."""
    return [
        DesktopWindow("Report", "WindowsForms10.Window.8.app.0.1a2b3c", 2011, 1298, ("Report",)),
    ]


def visible_blocking_windows() -> list[DesktopWindow]:
    """A visible owned dialog (empty title, unreadable) alongside the healthy main window."""
    return [
        DesktopWindow("", "WindowsForms10.Window.20008.app.0.1a2b3c", 702, 355, ()),
        DesktopWindow("Report", "WindowsForms10.Window.8.app.0.1a2b3c", 2011, 1298, ("Report",)),
    ]


def harvested_minimized_reason() -> str:
    """The REAL minimized-owner ``unknown_reason``, harvested from the detector (never hand-written).

    The prior #153 blind-spot was a test that hand-wrote a reason production could not emit; the latch
    tests must assert against the exact string ``inspect_credential_modal`` really produces.
    """
    reason = inspect_credential_modal(999, lambda _pid: minimized_main_windows()).unknown_reason
    assert reason is not None, "detector did not report the minimized-owner UNKNOWN reason"
    return reason


def harvested_zero_window_alive_reason() -> str:
    """The REAL zero-window-but-alive readiness reason, harvested from the detector (issue #158).

    Never hand-written: the same #153 blind-spot discipline as the minimized reason - the latch tests
    must assert the exact string ``inspect_credential_modal`` really produces when enumeration returns
    an empty list while the process is still alive.
    """
    reason = inspect_credential_modal(999, lambda _pid: [], process_is_alive=lambda _pid: True).desktop_unready
    assert reason is not None, "detector did not report the zero-window alive DESKTOP_UNREADY reason"
    return reason


def harvested_desktop_gone_reason() -> str:
    """The REAL ``process_gone`` string, harvested from the detector (issue #158).

    Driven with an empty enumerator and a liveness check that reports the process dead, so the
    ``DesktopGoneError`` tests assert the exact terminal string production emits, never a hand-written
    one.
    """
    reason = inspect_credential_modal(999, lambda _pid: [], process_is_alive=lambda _pid: False).process_gone
    assert reason is not None, "detector did not report the process-gone terminal reason"
    return reason


def explode(name: str):
    """Return a stub that proves an operation was not reached."""

    def boom(*_args, **_kwargs):
        raise AssertionError(f"{name}() must not run once the credential modal is visible")

    return boom


def real_detector_for_zero_windows(*, alive: bool):
    """A `_credential_state` built from the REAL detector, with the Win32 primitives injected.

    The production `_credential_state` in both entry points opens with
    ``if os.name != "nt": return CredentialDetection()``, so calling it un-stubbed runs *different code
    on different platforms*. That is not a reason to leave the classification untested off Windows:
    `inspect_credential_modal` already takes `enumerate_windows` and `process_is_alive` as parameters,
    so composing it with fakes here exercises the genuine branch logic - the same code Windows runs -
    on every platform, and drives it through the real CLI entry point rather than a hand-written
    `CredentialDetection`.
    """

    def detect(pid: int) -> CredentialDetection:
        return inspect_credential_modal(pid, lambda _pid: [], process_is_alive=lambda _pid: alive)

    return detect


def model_folder(root: Path, name: str) -> Path:
    """Create a minimal PBIP sibling model and return its cache destination."""
    tables_dir = root / f"{name}.SemanticModel" / "definition" / "tables"
    tables_dir.mkdir(parents=True)
    (tables_dir / "Orders.tmdl").write_text(
        '/// doc\ntable \'Orders\'\n\n\tcolumn X\n\npartition p = m\n\tSource = Sql.Database("server", "db")\n',
        encoding="utf-8",
    )
    (root / f"{name}.pbip").write_text("{}", encoding="utf-8")
    return root / f"{name}.SemanticModel" / ".pbi" / "cache.abf"


def test_win32_fixture_finds_owned_dialog_and_keeps_instances_separate() -> None:
    """Faithful fixture: owned empty-title dialog on PID A, no dialog on concurrent PID B."""
    windows_by_pid = {
        111: [
            DesktopWindow("", "WindowsForms10.Window.20008.app.0.33c0d9d", 702, 355, ("Enter your credentials",)),
            DesktopWindow("sample-superstore", "WindowsForms10.Window.8.app.0.33c0d9d", 2011, 1298, ("Report",)),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0, ()),
        ],
        222: [DesktopWindow("cached-model", "WindowsForms10.Window.8.app.0.33c0d9d", 2011, 1298, ("Report",))],
    }
    hit = inspect_credential_modal(111, lambda pid: windows_by_pid[pid]).modal
    miss = inspect_credential_modal(222, lambda pid: windows_by_pid[pid])

    assert hit is not None
    assert hit.window.class_name.startswith("WindowsForms10.Window.20008")
    assert miss.modal is None
    assert miss.unknown_reason is None


def test_unreadable_owned_dialog_reports_blocked_not_no_modal() -> None:
    """Live SQL dialog shape: visible owned dialog, empty title, no readable text."""
    windows_by_pid = {
        58104: [
            DesktopWindow("", "WindowsForms10.Window.20008.app.0.33c0d9d", 702, 355, ()),
            DesktopWindow("sample-superstore", "WindowsForms10.Window.8.app.0.33c0d9d", 2011, 1298, ("sample",)),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0, ()),
        ],
        46256: [DesktopWindow("cached", "WindowsForms10.Window.8.app.0.33c0d9d", 2011, 1298, ("cached",))],
    }
    blocked = inspect_credential_modal(58104, lambda pid: windows_by_pid[pid])
    healthy = inspect_credential_modal(46256, lambda pid: windows_by_pid[pid])

    assert blocked.modal is None
    assert blocked.blocking_dialog is not None
    assert blocked.blocking_dialog.window.width == 702
    assert healthy.modal is None
    assert healthy.blocking_dialog is None


def test_zero_size_helper_window_does_not_block() -> None:
    """Internet Explorer_Hidden 0x0 is present on healthy instances and must be ignored."""
    state = inspect_credential_modal(
        111,
        lambda _pid: [
            DesktopWindow("cached", "WindowsForms10.Window.8.app.0.33c0d9d", 2011, 1298, ("cached",)),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0, ()),
        ],
    )

    assert state.modal is None
    assert state.blocking_dialog is None


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 EnumWindows callback is Windows-only")
def test_real_win32_enumeration_callback_runs_against_this_process() -> None:
    """Execute the real ctypes callback; it must not be an empty fallback that swallowed exceptions."""
    windows, visited = _enumerate_pid_windows_with_count(os.getpid())

    assert isinstance(windows, list)
    assert visited > 0, "EnumWindows callback did not run; an empty fallback would hide detector failures"


def test_minimized_owner_reports_unknown_not_no_dialog() -> None:
    """When the owner is minimized, Windows hides owned dialogs; absence is indeterminate."""
    state = inspect_credential_modal(
        111,
        lambda _pid: [
            DesktopWindow(
                "sample-superstore",
                "WindowsForms10.Window.8.app.0.33c0d9d",
                2011,
                1298,
                ("Report",),
                minimized=True,
            )
        ],
    )

    assert state.modal is None
    assert state.unknown_reason is not None
    assert "minimized" in state.unknown_reason


def test_zero_windows_alive_reports_desktop_unready_not_no_dialog() -> None:
    """#158: an empty enumeration while the process is ALIVE is a local readiness failure, not healthy.

    A live Desktop always owns at least its main window, so zero windows is never proof of health. When
    the process is still running (starting up or wedged) the detector must report ``desktop_unready``
    and must NOT set ``process_gone`` (the process is not gone) nor a modal/blocking dialog.
    """
    state = inspect_credential_modal(111, lambda _pid: [], process_is_alive=lambda _pid: True)

    assert state.modal is None
    assert state.blocking_dialog is None
    assert state.process_gone is None
    assert state.unknown_reason is None
    assert state.desktop_unready is not None
    assert "no windows" in state.desktop_unready
    assert state.desktop_unready == harvested_zero_window_alive_reason()


def test_zero_windows_dead_reports_process_gone_not_no_dialog() -> None:
    """#158: an empty enumeration while the process is DEAD is a definitive terminal state.

    Zero windows plus a confirmed-dead PID means Desktop exited or crashed before any dialog state
    could be observed. This is the distinct ``process_gone`` terminal outcome - it must NOT masquerade
    as ``none`` (healthy) and must NOT be reported as ``unknown_reason`` (indeterminate/latch), because
    a dead process is not going to recover and the data source was never contacted.
    """
    state = inspect_credential_modal(111, lambda _pid: [], process_is_alive=lambda _pid: False)

    assert state.modal is None
    assert state.blocking_dialog is None
    assert state.unknown_reason is None
    assert state.process_gone is not None
    assert "no longer running" in state.process_gone
    assert state.process_gone == harvested_desktop_gone_reason()


def test_direct_refresh_returns_credential_missing_fast_at_t0(monkeypatch) -> None:
    """A t=0 credential modal must not wait for the XMLA deadline."""
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: modal_state())
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", explode("_load_adomd"))

    started = time.monotonic()
    with pytest.raises(CredentialMissingError):
        refresh(port=1234, tables=["Orders"], timeout_sec=5, desktop_pid=111, source_hint="Sql.Database(server)")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"t=0 modal path waited {elapsed:.3f}s instead of returning immediately"


def test_direct_refresh_returns_blocked_by_dialog_fast_at_t0(monkeypatch) -> None:
    """Unreadable blocking dialog must stop immediately instead of waiting for XMLA."""
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: blocking_state())
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", explode("_load_adomd"))

    started = time.monotonic()
    with pytest.raises(DialogBlockedError):
        refresh(port=1234, tables=["Orders"], timeout_sec=5, desktop_pid=111)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5


def test_direct_refresh_raises_desktop_gone_fast_at_t0(monkeypatch) -> None:
    """#158: a Desktop already dead at t=0 must bail immediately, never start the XMLA wait.

    The t=0 pre-check (``_raise_if_blocked``) is load-bearing: if the process was gone before the worker
    even started, waiting out the deadline learns nothing. The reason is harvested from the real
    detector, so this pins the terminal path against the exact string production emits.
    """
    reason = harvested_desktop_gone_reason()
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: CredentialDetection(process_gone=reason))
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", explode("_load_adomd"))

    started = time.monotonic()
    with pytest.raises(DesktopGoneError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=5, desktop_pid=111)
    elapsed = time.monotonic() - started

    assert excinfo.value.reason == reason
    assert elapsed < 0.5, f"t=0 process-gone path waited {elapsed:.3f}s instead of returning immediately"


def test_refresh_poll_catches_late_modal(monkeypatch, parked) -> None:
    """A credential dialog appearing after XMLA starts is caught on the next poll."""
    calls = {"count": 0}

    def late_modal(_pid: int):
        calls["count"] += 1
        return modal() if calls["count"] >= 2 else None

    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda pid: CredentialDetection(modal=late_modal(pid)),
    )
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)

    started = time.monotonic()
    with pytest.raises(CredentialMissingError):
        refresh(port=1234, tables=["Orders"], timeout_sec=10, desktop_pid=111, progress_enabled=False)

    assert time.monotonic() - started < 1.0, "shortened test poll interval should catch the modal quickly"
    assert calls["count"] >= 2
    parked.set()


def test_refresh_banner_is_flushed_and_heartbeat_reports_elapsed_total(monkeypatch, parked, capsys) -> None:
    """The warning arrives before the wait, then emits elapsed/total countdowns."""
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: CredentialDetection())
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_HEARTBEAT_SECONDS", 0.05)
    printed: list[dict[str, object]] = []
    original_print = print

    def recording_print(*args, **kwargs):
        printed.append({"text": " ".join(str(arg) for arg in args), "flush": kwargs.get("flush")})
        original_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", recording_print)

    with pytest.raises(TimeoutError):
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111, progress_enabled=False)

    out = capsys.readouterr().out
    assert "No blocking dialog on PID 111. Refreshing, bounded at 0.1s XMLA + 0.15s grace" in out
    assert "DO NOT kill this process" in out
    assert "still refreshing," in out and "/ 0s" in out
    assert printed[0]["flush"] is True
    parked.set()


def test_unknown_refresh_banner_does_not_claim_no_dialog(monkeypatch, parked, capsys) -> None:
    """A minimized owner emits the UNKNOWN banner and, once latched, ends as CREDENTIAL_UNKNOWN.

    Before #154 this fell through to a bare ``TimeoutError`` (which the parent blamed on a slow
    source). The reason is harvested from the real detector, not hand-written (the #153 blind-spot).
    """
    reason = harvested_minimized_reason()
    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda _pid: CredentialDetection(unknown_reason=reason),
    )
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_HEARTBEAT_SECONDS", 0.05)

    with pytest.raises(CredentialUnknownError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111, progress_enabled=False)

    assert excinfo.value.reason == reason
    out = capsys.readouterr().out
    assert "Blocking-dialog check on PID 111 is UNKNOWN" in out
    assert "No blocking dialog on PID 111" not in out
    parked.set()


def test_refresh_latches_unknown_seen_only_by_initial_precheck(monkeypatch, parked) -> None:
    """#154: UNKNOWN at t=0 cannot disappear before the first poll."""
    reason = harvested_minimized_reason()
    states = iter([CredentialDetection(unknown_reason=reason)])

    def initial_unknown_then_healthy(_pid: int) -> CredentialDetection:
        return next(states, CredentialDetection())

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", initial_unknown_then_healthy)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)

    with pytest.raises(CredentialUnknownError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111, progress_enabled=False)

    assert excinfo.value.reason == reason
    parked.set()


def test_refresh_latches_desktop_unready_seen_only_by_initial_precheck(monkeypatch, parked) -> None:
    """#158's twin of the #154 latch: DESKTOP_UNREADY at t=0 must survive a healthy first poll.

    The untested twin of `test_refresh_latches_unknown_seen_only_by_initial_precheck`. It is reachable
    only through a direct ``refresh(desktop_pid=...)`` call - `main` terminates at exit 2 before this -
    but that is exactly why it needs pinning rather than skipping: without the initial-state carry, a
    t=0-only readiness failure decays into a bare ``TimeoutError``, which the parent classifier reads
    as a slow source. That wrong-verdict family is the whole reason this change exists, so the fact
    that the *unknown* half is tested and the *unready* half is not is a coverage hole, not a
    reachability argument.
    """
    reason = harvested_zero_window_alive_reason()
    states = iter([CredentialDetection(desktop_unready=reason)])

    def initial_unready_then_healthy(_pid: int) -> CredentialDetection:
        return next(states, CredentialDetection())

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", initial_unready_then_healthy)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)

    with pytest.raises(DesktopUnreadyError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111, progress_enabled=False)

    assert excinfo.value.reason == reason
    parked.set()


class _FakeClock:
    """A monotonic clock the test advances explicitly, so the poll deadline is deterministic."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current frozen time."""
        return self.now

    def advance(self, step: float) -> None:
        """Move time forward by ``step`` seconds."""
        self.now += step


class _ImmortalWorker:
    """A worker that never finishes; each ``join`` advances the injected clock toward the deadline."""

    def __init__(self, clock: _FakeClock, step: float) -> None:
        self._clock = clock
        self._step = step

    def is_alive(self) -> bool:
        """Always alive - this simulates the parked mashup engine that only the deadline can end."""
        return True

    def join(self, timeout: float | None = None) -> None:
        """Ignore the requested wait and advance the fake clock by a fixed step."""
        del timeout
        self._clock.advance(self._step)


class _ScriptedEnumerator:
    """Feed ``inspect_credential_modal`` a scripted sequence of window-lists; the last phase sticks."""

    def __init__(self, phases: list[list[DesktopWindow]]) -> None:
        self._phases = phases
        self.calls = 0

    def __call__(self, _pid: int) -> list[DesktopWindow]:
        phase = self._phases[min(self.calls, len(self._phases) - 1)]
        self.calls += 1
        return list(phase)


def _real_detector(enumerator: _ScriptedEnumerator, process_is_alive=lambda _pid: True):
    """A detector that drives the REAL inspect_credential_modal through the scripted enumerator.

    ``process_is_alive`` is injected too (issue #158) so the zero-window liveness split is exercised
    deterministically without a live PID; it defaults to "alive" and is only consulted when the
    enumerator yields an empty window list.
    """
    return lambda pid: inspect_credential_modal(pid, enumerator, process_is_alive=process_is_alive)


def test_poll_loop_latches_minimize_restore_and_raises_unknown(monkeypatch) -> None:
    """#154 core: the measured minimize -> restore -> deadline sequence must latch and raise UNKNOWN.

    Drives the REAL ``join_with_credential_poll`` and REAL ``inspect_credential_modal`` through an
    injected fake enumerator. Poll 1 sees the iconic owner (UNKNOWN); every later poll sees the
    restored owner with the dialog gone (``none``). The first observation must be latched so the loop
    raises :class:`CredentialUnknownError` at the deadline instead of returning ``False`` (a bare
    timeout the parent blames on a slow source). This is the fails-before/passes-after test.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_credential_modal, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([minimized_main_windows(), restored_main_windows()])

    with pytest.raises(CredentialUnknownError) as excinfo:
        join_with_credential_poll(
            worker,
            pid=111,
            total_timeout=1.0,
            heartbeat_seconds=1000.0,
            poll_seconds=0.1,
            detector=_real_detector(enumerator),
        )

    assert excinfo.value.pid == 111
    assert excinfo.value.reason == harvested_minimized_reason()
    assert enumerator.calls >= 2, "the loop must have kept polling after the owner was restored"


def test_poll_loop_without_unknown_returns_false(monkeypatch) -> None:
    """No-false-positive guard: a run that is healthy throughout returns False, never latching UNKNOWN.

    A never-minimized sequence keeps the loop on its normal deadline path (``False`` -> the caller's
    ordinary ``TimeoutError``), proving the latch cannot fire without a real UNKNOWN observation.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_credential_modal, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([restored_main_windows()])

    result = join_with_credential_poll(
        worker,
        pid=111,
        total_timeout=1.0,
        heartbeat_seconds=1000.0,
        poll_seconds=0.1,
        detector=_real_detector(enumerator),
    )

    assert result is False
    assert enumerator.calls >= 1


def test_poll_loop_raises_blocked_on_visible_dialog(monkeypatch) -> None:
    """A visible owned dialog is a hard block: the loop raises DialogBlockedError, not UNKNOWN.

    Latching UNKNOWN must not swallow a genuinely visible blocking dialog - that is still a distinct,
    immediately-raised verdict.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_credential_modal, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([visible_blocking_windows()])

    with pytest.raises(DialogBlockedError):
        join_with_credential_poll(
            worker,
            pid=111,
            total_timeout=1.0,
            heartbeat_seconds=1000.0,
            poll_seconds=0.1,
            detector=_real_detector(enumerator),
        )


def test_poll_loop_raises_desktop_gone_when_process_dead(monkeypatch) -> None:
    """#158 PRIMARY (fails-before/passes-after): a dead Desktop is TERMINAL, raised immediately.

    Drives the REAL ``join_with_credential_poll`` and REAL ``inspect_credential_modal`` through an
    injected empty enumerator plus a liveness check that reports the process dead. Zero windows on a
    gone PID is definitive - Desktop exited/crashed and the source was never contacted - so the loop
    must raise :class:`DesktopGoneError` on the FIRST poll, never run out the clock and never return
    ``False`` (the bare-timeout path the parent blames on a slow source).

    Before the #158 detector guard + poll-loop raise, the empty enumeration fell through to ``none`` and
    this loop returned ``False`` (``pytest.raises`` failure: DID NOT RAISE). This is the controlled test
    that fails before the change and passes after it.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_credential_modal, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([[]])

    with pytest.raises(DesktopGoneError) as excinfo:
        join_with_credential_poll(
            worker,
            pid=111,
            total_timeout=1.0,
            heartbeat_seconds=1000.0,
            poll_seconds=0.1,
            detector=_real_detector(enumerator, process_is_alive=lambda _pid: False),
        )

    assert excinfo.value.pid == 111
    assert excinfo.value.reason == harvested_desktop_gone_reason()
    assert enumerator.calls == 1, "process_gone is terminal - the loop must raise on the first poll, not wait"


def test_poll_loop_latches_desktop_unready_when_process_alive_zero_windows(monkeypatch) -> None:
    """#158: an alive-but-window-less Desktop latches as a local readiness failure.

    Empty enumeration with the process still alive is a startup/wedged state, not a crash: the loop must
    keep waiting (a startup that grows a window and finishes is never overridden), latch the UNKNOWN
    observation, and raise :class:`DesktopUnreadyError` at the deadline - never ``process_gone``,
    credential UNKNOWN, or a bare timeout.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_credential_modal, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([[]])

    with pytest.raises(DesktopUnreadyError) as excinfo:
        join_with_credential_poll(
            worker,
            pid=111,
            total_timeout=1.0,
            heartbeat_seconds=1000.0,
            poll_seconds=0.1,
            detector=_real_detector(enumerator, process_is_alive=lambda _pid: True),
        )

    assert excinfo.value.pid == 111
    assert excinfo.value.reason == harvested_zero_window_alive_reason()
    assert enumerator.calls >= 2, "alive zero-window is latch-and-wait, not terminal"


def test_refresh_and_save_wires_credential_unknown_to_exit_3(monkeypatch, capsys) -> None:
    """#154 wiring: a latched CredentialUnknownError must surface as the DISTINCT exit 3.

    Exit 3 (UNKNOWN) must never collapse into exit 1 (blocked) or a healthy path - the four exit
    states are load-bearing for the parent's taxonomy (probe_desktop_query uses exit 3 for UNKNOWN,
    exit 1 for blocked). The reason is harvested from the real detector, and the verdict line is the
    real emitter's, so this pins the whole raise -> emit -> exit-code path with no hand-written text.
    """
    reason = harvested_minimized_reason()

    def _raise_unknown(*_args, **_kwargs):
        raise CredentialUnknownError(111, reason)

    monkeypatch.setattr(refresh_pbip_model, "refresh", _raise_unknown)
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "111", "--tables", "Orders"])

    exit_code = refresh_pbip_model._refresh_and_save(111, 5000, None, args)
    out = capsys.readouterr().out

    assert exit_code == 3, "CREDENTIAL_UNKNOWN must be exit 3, distinct from blocked's exit 1"
    assert "REFRESH: CREDENTIAL_UNKNOWN pid=111" in out
    assert reason in out


def test_refresh_and_save_wires_desktop_gone_to_exit_2(monkeypatch, capsys) -> None:
    """#158 wiring: a DesktopGoneError must surface as the ERROR-family exit 2, distinct from UNKNOWN.

    A dead Desktop is a local-environment failure, not an indeterminate credential state: it must NOT
    reuse exit 3 (CREDENTIAL_UNKNOWN) nor exit 1 (blocked). Exit 2 is the ERROR family the parent maps
    to ``ERROR`` (source not implicated). The reason is harvested from the real detector and the verdict
    line is the real emitter's, so this pins the whole raise -> emit -> exit-code path with no
    hand-written text.
    """
    reason = harvested_desktop_gone_reason()

    def _raise_gone(*_args, **_kwargs):
        raise DesktopGoneError(111, reason)

    monkeypatch.setattr(refresh_pbip_model, "refresh", _raise_gone)
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "111", "--tables", "Orders"])

    exit_code = refresh_pbip_model._refresh_and_save(111, 5000, None, args)
    out = capsys.readouterr().out

    assert exit_code == 2, "DESKTOP_GONE must be the ERROR-family exit 2, distinct from UNKNOWN's exit 3"
    assert "REFRESH: DESKTOP_GONE pid=111" in out
    assert reason in out


def test_refresh_main_returns_credential_missing_fast_at_t0(monkeypatch, tmp_path: Path, capsys) -> None:
    """refresh_pbip_model.main stops before port discovery, identity checks, refresh, or row counts."""
    model_folder(tmp_path, "MyMigration")
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: modal_state())
    monkeypatch.setattr(refresh_pbip_model, "discover_port", explode("discover_port"))

    started = time.monotonic()
    exit_code = refresh_pbip_model.main(["--pid", "111"])
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out

    assert exit_code == 1
    assert elapsed < 0.5, f"t=0 credential verdict waited {elapsed:.3f}s"
    assert "REFRESH: CREDENTIAL_MISSING" in out
    assert "Sql.Database(server)" in out


def test_refresh_main_returns_desktop_gone_before_port_discovery(monkeypatch, tmp_path: Path, capsys) -> None:
    """#158: the real mutating CLI entry point must terminate on a dead Desktop."""
    model_folder(tmp_path, "MyMigration")
    reason = harvested_desktop_gone_reason()
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda _pid: CredentialDetection(process_gone=reason),
    )
    monkeypatch.setattr(refresh_pbip_model, "discover_port", explode("discover_port"))

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert out.startswith("REFRESH: DESKTOP_GONE")
    assert reason in out


def test_refresh_main_returns_desktop_unready_from_the_real_detector(monkeypatch, tmp_path: Path, capsys) -> None:
    """#158: an alive-but-window-less Desktop terminates the mutating CLI, on EVERY platform.

    Composed from the real `inspect_credential_modal` with injected Win32 primitives rather than a
    hand-built `CredentialDetection`, so the detector's own zero-window/alive branch and the CLI's
    branch on it are proven as one chain - and proven identically on Linux CI and on Windows, which the
    un-stubbed `_credential_state` cannot do (its `os.name` guard returns a healthy state off Windows).
    """
    model_folder(tmp_path, "MyMigration")
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", real_detector_for_zero_windows(alive=True))
    monkeypatch.setattr(refresh_pbip_model, "discover_port", explode("discover_port"))

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 2, "DESKTOP_UNREADY is a local ERROR-family exit 2, not UNKNOWN's exit 3"
    assert out.startswith("REFRESH: DESKTOP_UNREADY")
    assert harvested_zero_window_alive_reason() in out
    assert "minimiz" not in out.lower(), "a window-less Desktop must never be reported as minimized"


def test_refresh_main_latches_unknown_from_its_own_precheck(monkeypatch, tmp_path: Path, capsys) -> None:
    """#154: main's first UNKNOWN survives port and identity work before refresh starts."""
    model_folder(tmp_path, "MyMigration")
    reason = harvested_minimized_reason()
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda _pid: CredentialDetection(unknown_reason=reason),
    )
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda _pid: 52001)
    monkeypatch.setattr(refresh_pbip_model, "_identity_gate", lambda _port, _cache: True)

    def assert_initial_state(_port, _tables, _timeout, *, desktop_pid, source_hint, initial_state=None):
        del desktop_pid, source_hint
        assert initial_state is not None
        assert initial_state.unknown_reason == reason
        raise CredentialUnknownError(111, initial_state.unknown_reason)

    monkeypatch.setattr(refresh_pbip_model, "refresh", assert_initial_state)

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3
    assert "REFRESH: CREDENTIAL_UNKNOWN" in out
    assert reason in out


def test_probe_query_returns_credential_missing_fast_at_t0(monkeypatch, capsys) -> None:
    """probe_desktop_query.main stops before port discovery or DAX when the modal is already open."""
    monkeypatch.setattr(probe_desktop_query, "_credential_state", lambda _pid: modal_state())
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    started = time.monotonic()
    exit_code = probe_desktop_query.main(["--pid", "111"])
    elapsed = time.monotonic() - started
    out = capsys.readouterr().out

    assert exit_code == 1
    assert elapsed < 0.5, f"t=0 credential verdict waited {elapsed:.3f}s"
    assert "PREFLIGHT: CREDENTIAL_MISSING" in out


def test_probe_query_main_returns_desktop_gone_before_port_discovery(monkeypatch, capsys) -> None:
    """#158: the real read-only CLI entry point must terminate on a dead Desktop."""
    reason = harvested_desktop_gone_reason()
    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda _pid: CredentialDetection(process_gone=reason),
    )
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert out.startswith("PREFLIGHT: DESKTOP_GONE")
    assert reason in out


def test_probe_query_main_returns_desktop_unready_from_the_real_detector(monkeypatch, capsys) -> None:
    """#158: the read-only CLI terminates on an alive-but-window-less Desktop, on EVERY platform.

    The read-only twin of the refresh test above, and composed the same way: the real detector with
    injected Win32 primitives, so Linux CI exercises the branch Windows actually takes.
    """
    monkeypatch.setattr(probe_desktop_query, "_credential_state", real_detector_for_zero_windows(alive=True))
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert out.startswith("PREFLIGHT: DESKTOP_UNREADY")
    assert harvested_zero_window_alive_reason() in out


def test_the_two_entry_points_get_an_explicit_desktop_state_baseline() -> None:
    """`conftest`'s autouse baseline must really be in force on BOTH entry points.

    17 tests drive `main(["--pid", "111", ...])` without naming a Desktop state. If the baseline stops
    applying they fall back to the production `_credential_state`, whose result is decided by the host
    OS: a healthy `CredentialDetection()` on Linux, and a real `process_gone` detection on Windows
    (pid 111 does not exist). Measured 2026-08-15, that split is exactly how 17 Windows-only failures
    passed green on `ubuntu-latest`.

    Asserting on the RETURNED VALUE could not catch it - off Windows the un-stubbed function also
    returns a bare `CredentialDetection()`, so a value check is a test that cannot fail on the one
    platform CI runs. The marker on the stub is checkable on every platform.
    """
    for module in (refresh_pbip_model, probe_desktop_query):
        assert getattr(module._credential_state, "is_test_baseline", False), (
            f"{module.__name__}._credential_state is not conftest's baseline stub, so un-stubbed tests "
            "would silently exercise different code on Windows and on Linux"
        )


def test_probe_query_returns_blocked_by_dialog_fast_at_t0(monkeypatch, capsys) -> None:
    """probe_desktop_query stops for an unreadable blocking dialog."""
    monkeypatch.setattr(probe_desktop_query, "_credential_state", lambda _pid: blocking_state())
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert out.startswith("PREFLIGHT: BLOCKED_BY_DIALOG")


def test_probe_query_returns_unknown_for_minimized_owner(monkeypatch, capsys) -> None:
    """A minimized owner is indeterminate, not evidence that no credential dialog exists."""
    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda _pid: CredentialDetection(unknown_reason="Power BI Desktop owner window is minimized"),
    )
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3
    assert out.startswith("PREFLIGHT: UNKNOWN")
    assert "minimized" in out


def test_probe_query_poll_catches_late_modal(monkeypatch, capsys) -> None:
    """probe_desktop_query catches a modal that appears after the read-only query starts."""
    started = threading.Event()
    release = threading.Event()
    calls = {"count": 0}

    def fake_probe(_port: int, _tables: list[str] | None, emit=print) -> int:
        del emit
        started.set()
        release.wait(timeout=600)
        return 0

    def late_modal(_pid: int):
        calls["count"] += 1
        return modal() if started.is_set() and calls["count"] >= 2 else None

    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda pid: CredentialDetection(modal=late_modal(pid)),
    )
    monkeypatch.setattr(probe_desktop_query, "PREFLIGHT_CREDENTIAL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(probe_desktop_query, "discover_port", lambda _pid: 52001)
    monkeypatch.setattr(probe_desktop_query, "probe", fake_probe)

    try:
        exit_code = probe_desktop_query.main(["--pid", "111", "--canaries", "Orders"])
    finally:
        release.set()
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "PREFLIGHT: CREDENTIAL_MISSING" in out
    assert calls["count"] >= 2


def test_probe_query_worker_cannot_print_after_credential_verdict(monkeypatch, capsys) -> None:
    """Reviewer's race: late worker DATA_OK text must not print after CREDENTIAL_MISSING."""
    started = threading.Event()
    release = threading.Event()
    calls = {"count": 0}

    def fake_probe(_port: int, _tables: list[str] | None, emit=print) -> int:
        started.set()
        release.wait(timeout=600)
        emit("PREFLIGHT: DATA_OK_FROM_WORKER_AFTER_RELEASE")
        return 0

    def late_modal(_pid: int):
        calls["count"] += 1
        return modal() if started.is_set() and calls["count"] >= 2 else None

    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda pid: CredentialDetection(modal=late_modal(pid)),
    )
    monkeypatch.setattr(probe_desktop_query, "PREFLIGHT_CREDENTIAL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(probe_desktop_query, "discover_port", lambda _pid: 52001)
    monkeypatch.setattr(probe_desktop_query, "probe", fake_probe)

    exit_code = probe_desktop_query.main(["--pid", "111", "--canaries", "Orders"])
    first_out = capsys.readouterr().out
    release.set()
    time.sleep(0.1)
    later_out = capsys.readouterr().out

    assert exit_code == 1
    assert first_out.splitlines() == [first_out.splitlines()[-1]]
    assert first_out.startswith("PREFLIGHT: CREDENTIAL_MISSING")
    assert "DATA_OK_FROM_WORKER_AFTER_RELEASE" not in first_out
    assert later_out == ""


# ==================================================================================================
# probe_desktop_credential.ps1 - the PowerShell arbiter's own dialog classifiers (issue #367)
# ==================================================================================================
#
# The arbiter used to decide "blocking" from GEOMETRY alone: `Test-BlockingDialog` returned the first
# visible non-main window >= 100x100 as a blocking dialog, and the caller printed
# `VERDICT: BLOCKED_BY_DIALOG`, exit 1 - the same hard-stop band as a real credential wall. A Power BI
# Refresh progress dialog satisfies that trivially, and on 2026-08-28 a field report caught it doing
# exactly that under three concurrent refreshes: the "credential wall" was an ordinary progress dialog
# stalled behind a Snowflake cold start.
#
# These tests drive the SHIPPED classifiers directly, through the `-LoadDetectorsOnly` dot-source seam,
# with synthesised window objects. That seam exists so the part that decides a hard stop is testable on
# a machine with no Power BI Desktop; everything past it (UI Automation, the Win32 shim, the refresh
# invoke) still needs a live Desktop and is NOT covered here.

PROBE_PS1 = Path(__file__).resolve().parents[1] / "scripts" / "probe_desktop_credential.ps1"
CREDENTIAL_SIGNATURE = PROBE_PS1.parent / "credential_modal_signature.regex"
BENIGN_SIGNATURE = PROBE_PS1.parent / "benign_dialog_signature.regex"
BLOCKING_SIGNATURE = PROBE_PS1.parent / "blocking_prompt_signature.regex"

_HARNESS = r"""
param(
  [Parameter(Mandatory = $true)][string]$Probe,
  [string]$WindowsJson,
  [string]$PayloadJson,
  [int]$PayloadExit = 0,
  [switch]$RefreshInFlight
)
$ErrorActionPreference = 'Stop'
. $Probe -LoadDetectorsOnly
if ($PayloadJson) {
  $payload = $null
  try { $payload = ConvertFrom-Json (Get-Content -LiteralPath $PayloadJson -Raw) } catch { $payload = $null }
  $result = ConvertTo-HarvestResult -Payload $payload -ExitCode $PayloadExit
  $accepted = ($null -ne $result)
  $shaped = [ordered]@{
    accepted = $accepted
    complete = $(if ($accepted) { [bool]$result.Complete } else { $false })
    items    = $(if ($accepted) { @($result.Items).Count } else { 0 })
  }
  Write-Output ('<<<PROBE-JSON>>>' + (ConvertTo-Json $shaped -Compress -Depth 4))
  return
}
$parsed = ConvertFrom-Json (Get-Content -LiteralPath $WindowsJson -Raw)
$windows = @($parsed)
$verdict = Get-DialogVerdict -Windows $windows -RefreshInFlight:$RefreshInFlight
$credential = Test-CredentialModal -Windows $windows
$payload = [ordered]@{
  credential = $credential
  verdict    = $(if ($null -eq $verdict) { $null } else { [string]$verdict.Verdict })
  kind       = $(if ($null -eq $verdict) { $null } else { [string]$verdict.Kind })
  exit_code  = $(if ($null -eq $verdict) { 0 } else { [int]$verdict.ExitCode })
  evidence   = $(if ($null -eq $verdict) { $null } else { [string]$verdict.Evidence })
  candidates = @(Select-DialogCandidate -Windows $windows).Count
}
Write-Output ('<<<PROBE-JSON>>>' + (ConvertTo-Json $payload -Compress -Depth 4))
"""


def _powershell() -> str:
    """Path to Windows PowerShell, or skip - this arbiter is Windows-only by construction."""
    if os.name != "nt":
        pytest.skip("probe_desktop_credential.ps1 is a Windows-only UI Automation arbiter")
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("no PowerShell interpreter on PATH")
    return exe


def _window(**overrides) -> dict:
    """A synthesised Desktop window, shaped exactly like `ConvertTo-ProbeWindow`'s output."""
    window = {
        "Title": "",
        "ClassName": "#32770",
        "Width": 600,
        "Height": 400,
        "Texts": [],
        "Minimized": False,
        # `None` = the window has no owner, so the modality test could not be applied at all. That is
        # NOT the same as `False` (owner disabled), and the classifier must not treat it as such.
        "OwnerEnabled": None,
        # Texts belonging to interactive controls. Excluded from the PROSE JOIN only - an interposed
        # `Cancel` button between `Enter your` and `credentials` defeated a naive whole-window join.
        "InteractiveTexts": [],
        # Whether the UIA harvest ran to completion: not truncated by the element cap, not timed out,
        # no pattern read that threw. Defaults true here because most fixtures model a complete read;
        # the fail-safe treatment of every non-Boolean value is pinned by its own test.
        "HarvestComplete": True,
    }
    window.update(overrides)
    return window


def classify(tmp_path: Path, windows: list[dict], *, refresh_in_flight: bool = False) -> dict:
    """Run the SHIPPED classifiers over ``windows`` and return their verdict as a dict.

    Deliberately routed through files and `-File` rather than `-Command`: a quoting slip in an inline
    command is how a mutation run in this repo scored a false pass. A non-zero exit (a renamed or
    deleted function, a parse error) raises here rather than degrading to an empty result, so a
    mutation cannot pass by breaking the harness.
    """
    exe = _powershell()
    harness = tmp_path / "classify.ps1"
    harness.write_text(_HARNESS, encoding="utf-8")
    payload = tmp_path / "windows.json"
    payload.write_text(json.dumps(windows), encoding="utf-8")
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness), "-Probe", str(PROBE_PS1)]
    argv += ["-WindowsJson", str(payload)]
    if refresh_in_flight:
        argv.append("-RefreshInFlight")
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, f"harness failed ({done.returncode}):\n{done.stdout}\n{done.stderr}"
    marker = "<<<PROBE-JSON>>>"
    assert marker in done.stdout, f"harness produced no result payload:\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.split(marker, 1)[1].strip().splitlines()[0])


def harvest_result(tmp_path: Path, payload, *, exit_code: int = 0) -> dict:
    """Run the SHIPPED payload validator over a raw child payload and return what it accepted."""
    exe = _powershell()
    harness = tmp_path / "classify.ps1"
    harness.write_text(_HARNESS, encoding="utf-8")
    blob = tmp_path / "payload.json"
    blob.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness), "-Probe", str(PROBE_PS1)]
    argv += ["-PayloadJson", str(blob), "-PayloadExit", str(exit_code)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, f"harness failed ({done.returncode}):\n{done.stdout}\n{done.stderr}"
    marker = "<<<PROBE-JSON>>>"
    assert marker in done.stdout, f"harness produced no result payload:\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout.split(marker, 1)[1].strip().splitlines()[0])


REFRESH_PROGRESS = _window(
    Title="Refresh",
    ClassName="HwndWrapper[PBIDesktop.exe;;refresh]",
    Texts=["Refresh", "Orders", "1,204 rows loaded", "Cancel"],
    InteractiveTexts=["Cancel"],
    # A Power BI refresh dialog DOES disable its owner, so modality alone cannot tell it from a
    # credential modal. Pinned false on purpose: the fix must not lean on the owner test.
    OwnerEnabled=False,
)
CREDENTIAL_MODAL = _window(
    Title="",
    Texts=["Please specify how to connect", "Edit Credentials"],
    OwnerEnabled=False,
)
# The review defect of 2026-08-29, as the collector actually produced it. A WPF modal renders its
# whole visual tree into ONE HWND, so `EnumChildWindows` harvested nothing and only the caption
# survived - a caption that matches the progress signature.
WPF_CREDENTIAL_MODAL_SEEN_AS_CAPTION_ONLY = _window(
    Title="Refresh",
    ClassName="HwndWrapper[PBIDesktop.exe;;dialog]",
    Texts=["Refresh"],
    OwnerEnabled=False,
    HarvestComplete=False,
)
# The second round's defect: a `Cancel` button was enough to satisfy the old "we read SOME text"
# proxy, so a benign CAPTION cleared the window while the credential text stayed unread.
WPF_CREDENTIAL_MODAL_WITH_ONLY_A_BUTTON_READ = _window(
    Title="Refresh",
    ClassName="HwndWrapper[PBIDesktop.exe;;dialog]",
    Texts=["Refresh", "Cancel"],
    InteractiveTexts=["Cancel"],
    OwnerEnabled=False,
    HarvestComplete=True,
)
# The same window once UI Automation supplies the visual text the Win32 collector could not see.
WPF_CREDENTIAL_MODAL_WITH_UIA_TEXT = _window(
    Title="Refresh",
    ClassName="HwndWrapper[PBIDesktop.exe;;dialog]",
    Texts=["Refresh", "Enter your credentials", "OK", "Cancel"],
    InteractiveTexts=["OK", "Cancel"],
    OwnerEnabled=False,
    HarvestComplete=True,
)


def test_a_refresh_progress_dialog_is_not_reported_as_a_credential_block(tmp_path: Path) -> None:
    """AC1. The field-reported false positive: an ordinary progress dialog is not a hard stop.

    Two separate claims, and both matter. It must not be classified as a credential prompt, AND its
    verdict must not land in the exit-1 hard-stop band - `BLOCKED_BY_DIALOG` was exit 1, the same code
    a genuine credential wall uses, which is what made the false positive halt an unattended run.
    """
    result = classify(tmp_path, [REFRESH_PROGRESS])

    assert result["candidates"] == 1, "the size pre-filter must still SELECT it; only the verdict changed"
    assert result["credential"] is None
    assert result["kind"] == "benign"
    assert result["verdict"] != "BLOCKED_BY_DIALOG"
    assert result["exit_code"] != 1, "a progress dialog must never reach the credential hard-stop band"


def test_a_refresh_progress_dialog_during_our_own_refresh_is_ignored_entirely(tmp_path: Path) -> None:
    """AC1/AC3. Once THIS script invoked the refresh, the progress dialog it caused is not a finding.

    The distinction is the whole point of `-RefreshInFlight`: the same window means "somebody else is
    refreshing, do not stack a second one" at t=0, and "your own refresh is running" in the poll loop.
    """
    assert classify(tmp_path, [REFRESH_PROGRESS], refresh_in_flight=True)["verdict"] is None


def test_a_progress_dialog_already_up_at_t0_reports_refresh_in_progress(tmp_path: Path) -> None:
    """AC3. Concurrent multi-instance refresh, as a first-class condition rather than setup noise.

    The 2026-08-28 report ran three refreshes at once and had to cancel a stale duplicate by hand. A
    progress dialog already open before this probe triggers anything is exactly that state, and the
    verdict has to say so: not a credential wall (exit 1), not "all clear" (exit 0), but "Desktop is
    busy, I could not probe" (exit 3).
    """
    result = classify(tmp_path, [REFRESH_PROGRESS])

    assert result["verdict"] == "REFRESH_IN_PROGRESS"
    assert result["exit_code"] == 3


def test_a_genuine_credential_modal_is_still_detected(tmp_path: Path) -> None:
    """AC2. The true positive must survive the fix for the false positive.

    This is the "moved the boundary" guard: silencing the progress dialog by loosening the credential
    path would pass every other test in this section and destroy the arbiter's only reason to exist.
    """
    result = classify(tmp_path, [CREDENTIAL_MODAL])

    assert result["credential"] == "Please specify how to connect"
    assert result["verdict"] == "CREDENTIAL_MISSING"
    assert result["exit_code"] == 1


def test_a_credential_modal_is_still_detected_behind_a_concurrent_refresh_dialog(tmp_path: Path) -> None:
    """AC2/AC3. A benign classification must never mask a real modal on another window.

    Ordering matters here and is easy to get wrong: iterate windows and return the first classification,
    and a progress dialog enumerated first swallows the credential modal enumerated second. Run with
    `refresh_in_flight` so the progress dialog is in its most-ignorable state.
    """
    result = classify(tmp_path, [REFRESH_PROGRESS, CREDENTIAL_MODAL], refresh_in_flight=True)

    assert result["verdict"] == "CREDENTIAL_MISSING"
    assert result["exit_code"] == 1


def test_an_unreadable_dialog_is_a_third_verdict_not_one_of_the_other_two(tmp_path: Path) -> None:
    """A window exposing no text at all: "we could not read it" is its own answer.

    Absent is not empty. It must not collapse into `CREDENTIAL_MISSING` (we have no evidence of a
    credential prompt) nor into "nothing here" (we have no evidence of health either).
    """
    result = classify(tmp_path, [_window(Texts=[], OwnerEnabled=False)])

    assert result["kind"] == "unreadable"
    assert result["verdict"] == "DIALOG_UNREADABLE"
    assert result["exit_code"] == 3
    assert result["credential"] is None


def test_a_readable_but_unrecognized_dialog_is_distinct_from_an_unreadable_one(tmp_path: Path) -> None:
    """The two ambiguous states are NOT the same state, and the verdict has to distinguish them.

    "We read this window and it is not a credential prompt" is strictly more knowledge than "we could
    not read this window at all". Collapsing them loses the only fact that tells an operator whether
    looking at the screen will help.
    """
    unrecognized = classify(tmp_path, [_window(Title="Whoops", Texts=["Whoops", "Something went wrong"])])
    unreadable = classify(tmp_path, [_window(Texts=[])])

    assert unrecognized["kind"] == "unrecognized"
    assert unrecognized["verdict"] == "DIALOG_UNRECOGNIZED"
    assert unrecognized["exit_code"] == 3
    assert unrecognized["verdict"] != unreadable["verdict"], "the two ambiguous states must not collapse"
    assert unrecognized["evidence"], "the readable case must carry the text it read, or it proves nothing"


def test_an_unclassifiable_window_outranks_a_benign_one(tmp_path: Path) -> None:
    """Precedence. `benign` is the only classification carrying positive evidence of harmlessness.

    So it must never outrank a window nobody could classify - otherwise one progress dialog is enough
    to hide every other window Desktop has open.

    Asserted at t=0 FIRST, and that ordering is load-bearing: in flight the benign branch is skipped
    anyway, so an in-flight-only assertion passes even when the precedence is reversed. Caught by
    mutation - moving the benign return above the unreadable one went undetected until t=0 was covered.
    """
    windows = [REFRESH_PROGRESS, _window(Texts=[])]

    assert classify(tmp_path, windows)["verdict"] == "DIALOG_UNREADABLE"
    assert classify(tmp_path, windows, refresh_in_flight=True)["verdict"] == "DIALOG_UNREADABLE"


def test_a_readable_unrecognized_window_outranks_a_benign_one(tmp_path: Path) -> None:
    """Same precedence rule for the other ambiguous state, at t=0 where both are live."""
    windows = [REFRESH_PROGRESS, _window(Title="Whoops", Texts=["Whoops", "Something went wrong"])]

    assert classify(tmp_path, windows)["verdict"] == "DIALOG_UNRECOGNIZED"


def test_an_enabled_owner_window_exonerates_a_dialog(tmp_path: Path) -> None:
    """Modality is used ONE WAY: it can exonerate a window, never convict one.

    A modal dialog disables its owner, so an enabled owner proves this window blocks nothing. The
    converse does not hold - `REFRESH_PROGRESS` above pins `OwnerEnabled=False` precisely because Power
    BI's own progress dialog also disables the owner - so a disabled owner is never treated as evidence.
    """
    exonerated = classify(tmp_path, [_window(Texts=["Something else"], OwnerEnabled=True)])
    not_exonerated = classify(tmp_path, [_window(Texts=["Something else"], OwnerEnabled=False)])
    no_owner = classify(tmp_path, [_window(Texts=["Something else"], OwnerEnabled=None)])

    assert exonerated["verdict"] is None
    assert not_exonerated["verdict"] == "DIALOG_UNRECOGNIZED"
    assert no_owner["verdict"] == "DIALOG_UNRECOGNIZED", "no owner means the test did not apply, not that it passed"


def test_the_main_window_and_tiny_helper_windows_are_still_not_candidates(tmp_path: Path) -> None:
    """The class and size pre-filters survive; they were never the defect, only their promotion to a verdict."""
    main_window = _window(
        Title="MyReport - Power BI Desktop",
        ClassName="WindowsForms10.Window.8.app.0.141b42a_r6_ad1",
        Width=1920,
        Height=1080,
        Texts=["MyReport - Power BI Desktop", "Refresh"],
    )
    tiny = _window(Width=10, Height=10)

    result = classify(tmp_path, [main_window, tiny])

    assert result["candidates"] == 0
    assert result["verdict"] is None


def test_the_arbiter_no_longer_derives_a_blocking_verdict_from_size_alone() -> None:
    """Regression pin on the shipped file: the size-only detector and its exit-1 verdict are gone.

    The behavioural tests above prove the classifiers are right; this proves the CALLERS were rewired to
    them. A revert that restored `Test-BlockingDialog` while leaving the new functions in place would
    pass every behavioural test in this section and still ship the defect.
    """
    body = PROBE_PS1.read_text(encoding="utf-8")

    assert "function Test-BlockingDialog" not in body, "the size-only detector must not come back"
    emitted = re.findall(r'VERDICT:\s*\{?0?\}?\s*"|VERDICT: ([A-Z_]+)', body)
    assert "BLOCKED_BY_DIALOG" not in [name for name in emitted if name], (
        "this arbiter must not emit BLOCKED_BY_DIALOG: it cannot establish that a dialog is blocking"
    )
    assert "exit $blocker.ExitCode" in body, "the t=0 branch must exit with the classifier's code, not a literal 1"


def test_the_benign_signature_can_never_shadow_a_credential_prompt() -> None:
    """The one way this fix could fail OPEN: a benign pattern broad enough to match a real modal.

    In the poll loop a `benign` classification is IGNORED, so a credential prompt that matched the
    benign signature would end the probe at `CREDENTIAL_PRESENT` - the fail-open the script's own header
    calls worse than no arbiter. Platform-independent by design: it reads the two shipped resources, so
    it gates on Linux CI too, where the PowerShell tests above skip.
    """
    benign = re.compile(BENIGN_SIGNATURE.read_text(encoding="utf-8").strip(), re.IGNORECASE)
    credential_phrases = [
        "You aren't signed in",
        "Personal Access Token",
        "Databricks Client Credentials",
        "Please specify how to connect",
        "Account Key",
        "Enter your credentials",
        "Permission is required to run this native database query",
    ]

    for phrase in credential_phrases:
        assert not benign.search(phrase), f"benign signature must not match a credential prompt: {phrase!r}"


def test_the_benign_signature_matches_real_refresh_progress_text() -> None:
    """The progress vocabulary the arbiter is meant to recognise, pinned as data.

    ⚠️ INFERRED from Power BI Desktop's refresh UI, not captured from a live dialog - so treat a miss as
    a specificity gap, not a correctness one: an unmatched progress dialog degrades to
    `DIALOG_UNRECOGNIZED`, which is still exit 3 and still not a credential wall.
    """
    benign = re.compile(BENIGN_SIGNATURE.read_text(encoding="utf-8").strip(), re.IGNORECASE)

    for phrase in ["Refresh", "Evaluating", "1,204 rows loaded", "Waiting for other queries", "Loading data"]:
        assert benign.search(phrase), f"benign signature should match progress text: {phrase!r}"
    assert not benign.search("Refresh failed"), "'Refresh' is anchored so it cannot swallow arbitrary sentences"


# --------------------------------------------------------------------------------------------------
# WPF content harvest (blind review, 2026-08-29)
# --------------------------------------------------------------------------------------------------
#
# The Win32 collector reads a window's caption plus its CHILD HWND text. WPF renders its whole visual
# tree into ONE HWND, so a WPF dialog contributes nothing but its caption. A modal titled `Refresh`
# whose content read `Enter your credentials` was therefore classified benign from the caption alone,
# suppressed in-flight, and the probe exited 0 with `CREDENTIAL_PRESENT`.
#
# Measured here on an owned WPF modal (WinForms owner so it is excluded by class, WPF `TextBlock`
# child, `ShowDialog()` disabling the owner, raised 0.8s after Refresh is invoked):
#
#   commit 0aa3767 : refresh invoked: True / no credential modal within 12s / CREDENTIAL_PRESENT  exit 0
#   fixed          : refresh invoked: True / credential modal detected: 'Enter your credentials'  exit 1
#
# That is a SILENT false negative on a hard stop - strictly worse than the loud false positive #367
# set out to remove. Two independent guards now cover it: the UIA harvest (below), and the
# `ContentRead` gate that keeps a caption-only benign match indeterminate even if the harvest fails.

CREDENTIAL_ALTERNATIVES = [
    "You aren't signed in",
    "Personal Access Token",
    "Databricks Client Credentials",
    "specify how to connect",
    "Account Key",
    "Enter your credentials",
    "Please specify how to connect",
]

_FAKE_DESKTOP_APP = r"""
param([Parameter(Mandatory = $true)][string]$ReadyFile)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase
Add-Type -AssemblyName WindowsFormsIntegration
[System.Windows.Forms.Application]::EnableVisualStyles()

# WinForms owner: its class starts with WindowsForms10.Window.8, so the probe excludes it exactly as
# it excludes the real Power BI Desktop main window.
$script:form = New-Object System.Windows.Forms.Form
$script:form.Text = 'Fake Desktop'
$script:form.Width = 640
$script:form.Height = 480

# A WPF button hosted in the form. A plain WinForms Button surfaces to UI Automation as
# ControlType.Pane with NO patterns (measured), so the probe's `Name -eq 'Refresh'` + Invoke scan
# would never fire and the run would end at the not-invoked guard instead of testing anything.
$button = New-Object System.Windows.Controls.Button
$button.Content = 'Refresh'
$hostControl = New-Object System.Windows.Forms.Integration.ElementHost
$hostControl.Width = 140
$hostControl.Height = 48
$hostControl.Child = $button
$script:form.Controls.Add($hostControl)

# Raised on a timer so the click handler returns immediately - InvokePattern.Invoke() must not be
# left waiting on a modal message loop.
$script:timer = New-Object System.Windows.Forms.Timer
$script:timer.Interval = 800
$script:timer.Add_Tick({
    $script:timer.Stop()
    $modal = New-Object System.Windows.Window
    $modal.Title = 'Refresh'
    $modal.Width = 520
    $modal.Height = 320
    $panel = New-Object System.Windows.Controls.StackPanel
    $block = New-Object System.Windows.Controls.TextBlock
    $block.Text = 'Enter your credentials'
    $null = $panel.Children.Add($block)
    $modal.Content = $panel
    $helper = New-Object System.Windows.Interop.WindowInteropHelper($modal)
    $helper.Owner = $script:form.Handle
    $null = $modal.ShowDialog()
  })
$button.Add_Click({ $script:timer.Start() })
$script:form.Add_Shown({ Set-Content -LiteralPath $ReadyFile -Value 'ready' -Encoding ascii })
[System.Windows.Forms.Application]::Run($script:form)
"""


def test_an_owned_wpf_credential_modal_titled_refresh_is_a_hard_stop(tmp_path: Path) -> None:
    """Live regression for the review defect: the whole script, against a real owned WPF modal.

    The offline tests below pin the classifier's half of this. This one pins the COLLECTOR's half -
    that the probe actually harvests text a WPF window only exposes through UI Automation - and it is
    the only test here that exercises the real Win32 + UIA + refresh-invoke path end to end.
    """
    exe = _powershell()
    app_script = tmp_path / "fake_desktop.ps1"
    app_script.write_text(_FAKE_DESKTOP_APP, encoding="utf-8")
    ready = tmp_path / "ready.txt"
    app = subprocess.Popen(  # pylint: disable=consider-using-with
        [exe, "-Sta", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(app_script), "-ReadyFile", str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            if ready.exists():
                break
            time.sleep(0.5)
        if not ready.exists():
            pytest.skip("the WPF fixture app never showed a window (no interactive desktop?)")
        done = subprocess.run(
            [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PROBE_PS1), "-DesktopPid", str(app.pid)]
            + ["-TimeoutSec", "12"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if "refresh invoked: True" not in done.stdout:
            pytest.skip(f"the fixture app exposed no invokable Refresh to UI Automation:\n{done.stdout}")

        assert "Enter your credentials" in done.stdout, (
            "the probe must harvest WPF visual text; child-HWND enumeration alone sees only the caption"
        )
        assert "VERDICT: CREDENTIAL_MISSING" in done.stdout
        assert done.returncode == 1, (
            f"a real credential modal must be a hard stop, not exit {done.returncode}:\n{done.stdout}"
        )
    finally:
        app.kill()
        app.wait(timeout=30)


def test_a_caption_only_benign_match_stays_indeterminate(tmp_path: Path) -> None:
    """The collector's half can fail; this is the guard that holds when it does.

    A window whose CONTENT was never read cannot be evidence of harmlessness, however reassuring its
    caption. `absent != empty`, applied one level down - so a `Refresh` caption over unread content is
    reported as indeterminate rather than suppressed.
    """
    result = classify(tmp_path, [WPF_CREDENTIAL_MODAL_SEEN_AS_CAPTION_ONLY])

    assert result["kind"] == "benign-title-only"
    assert result["verdict"] == "DIALOG_UNREADABLE"
    assert result["exit_code"] == 3


def test_a_caption_only_benign_match_is_not_suppressed_in_flight(tmp_path: Path) -> None:
    """The exact step that produced exit 0: in-flight suppression of a caption-only benign match.

    In the poll loop a genuine `benign` window is ignored, because it is our own refresh. A
    caption-only match must NOT get that treatment - nothing was read, so nothing was established, and
    a suppressed observation leaves the deadline free to print CREDENTIAL_PRESENT.
    """
    result = classify(tmp_path, [WPF_CREDENTIAL_MODAL_SEEN_AS_CAPTION_ONLY], refresh_in_flight=True)

    assert result["verdict"] == "DIALOG_UNREADABLE", "an unread window must stay latchable in flight"
    assert result["exit_code"] == 3


def test_uia_harvested_text_turns_the_same_window_into_a_credential_stop(tmp_path: Path) -> None:
    """With the visual text merged in, the caption stops mattering: credential beats benign."""
    result = classify(tmp_path, [WPF_CREDENTIAL_MODAL_WITH_UIA_TEXT], refresh_in_flight=True)

    assert result["kind"] == "credential"
    assert result["verdict"] == "CREDENTIAL_MISSING"
    assert result["exit_code"] == 1
    assert result["credential"] == "Enter your credentials"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("__absent__", id="field-missing"),
        pytest.param(None, id="null"),
        pytest.param(False, id="false"),
        pytest.param(0, id="int-0"),
        pytest.param(1, id="int-1"),
        pytest.param("true", id="str-true"),
        pytest.param("false", id="str-false"),
        pytest.param("", id="empty-string"),
        pytest.param([], id="empty-list"),
    ],
)
def test_only_a_real_boolean_true_can_authorise_suppression(tmp_path: Path, value) -> None:
    """`-eq $true` is COERCIVE, and review proved it: integer `1` and the string `"true"` both cleared.

    Anything that is not a Boolean is an unknown window shape - a future collector change, a caller
    that predates the field, a JSON round-trip that widened a type - and an unknown shape must never be
    able to authorise suppression. Only `$true` itself counts.
    """
    window = _window(Title="Refresh", Texts=["Refresh", "1,204 rows loaded"], OwnerEnabled=False)
    if value == "__absent__":
        del window["HarvestComplete"]
    else:
        window["HarvestComplete"] = value

    result = classify(tmp_path, [window], refresh_in_flight=True)

    assert result["kind"] == "benign-unverified", f"HarvestComplete={value!r} must not count as complete"
    assert result["verdict"] == "DIALOG_UNREADABLE"
    assert result["exit_code"] == 3


def test_a_real_boolean_true_does_authorise_suppression(tmp_path: Path) -> None:
    """The positive control for the test above - otherwise it could pass by refusing everything."""
    window = _window(Title="Refresh", Texts=["Refresh", "1,204 rows loaded"], OwnerEnabled=False)
    window["HarvestComplete"] = True

    assert classify(tmp_path, [window], refresh_in_flight=True)["verdict"] is None


def test_reading_only_a_button_does_not_authorise_a_benign_caption(tmp_path: Path) -> None:
    """Round 2's defect: "we harvested SOME text" was a proxy for "we read the credential content".

    A `Cancel` button satisfied it. Under the inverted rule the harvest being complete is necessary but
    not sufficient - the BENIGN SIGNATURE ITSELF must match content, and `Cancel` does not. So the
    three separate exploits that beat the old proxy (TextPattern-only content, content past the element
    cap, a split defeated by an interposed element) all stop being silent clears at once: none of them
    ever produced benign content to read.
    """
    result = classify(tmp_path, [WPF_CREDENTIAL_MODAL_WITH_ONLY_A_BUTTON_READ], refresh_in_flight=True)

    assert result["kind"] != "benign", "a button label is not evidence that a dialog is harmless"
    assert result["verdict"] == "DIALOG_UNREADABLE"
    assert result["exit_code"] == 3


def test_a_benign_caption_over_unreadable_content_is_never_suppressed(tmp_path: Path) -> None:
    """Whatever the collector missed, a caption alone cannot clear a window - in flight or at t=0."""
    for in_flight in (False, True):
        result = classify(tmp_path, [WPF_CREDENTIAL_MODAL_SEEN_AS_CAPTION_ONLY], refresh_in_flight=in_flight)
        assert result["kind"] == "benign-title-only", f"in_flight={in_flight}"
        assert result["verdict"] == "DIALOG_UNREADABLE"
        assert result["exit_code"] == 3


def test_benign_content_from_a_truncated_harvest_is_not_benign(tmp_path: Path) -> None:
    """Truncated must never read as "complete and clean" - the element cap was independently exploitable.

    450 ordinary elements followed by the credential `TextBlock` cleared the window because the cap
    silently stopped short. The cap still exists (it bounds the work), but hitting it now withholds the
    right to suppress instead of quietly granting it.
    """
    window = _window(
        Title="Refresh",
        Texts=["Refresh", "Evaluating"],
        OwnerEnabled=False,
        HarvestComplete=False,
    )

    result = classify(tmp_path, [window], refresh_in_flight=True)

    assert result["kind"] == "benign-unverified"
    assert result["exit_code"] == 3


def test_a_credential_signature_split_across_wpf_elements_still_convicts(tmp_path: Path) -> None:
    """WPF splits one sentence across visual elements, so the signature must see the joined text.

    Both readers are asserted: `Get-DialogVerdict` (the classifier) and `Test-CredentialModal` (the
    all-windows scan the main body runs first). They are separate call sites and either one losing the
    joined text re-opens the gap on its own.
    """
    window = _window(Title="Refresh", Texts=["Refresh", "Enter your", "credentials"], OwnerEnabled=False)

    result = classify(tmp_path, [window], refresh_in_flight=True)

    assert result["verdict"] == "CREDENTIAL_MISSING"
    assert result["exit_code"] == 1
    assert result["credential"] == "Refresh Enter your credentials", (
        "Test-CredentialModal must search the joined text too, not just the classifier"
    )


def test_the_benign_signature_is_never_matched_against_joined_text(tmp_path: Path) -> None:
    """The join is asymmetric on purpose, and the asymmetry is the safety property.

    Joining can manufacture a phrase from adjacent fragments. On the credential path that yields a LOUD
    false stop a human resolves by looking at the screen; on the benign path it would yield a SILENT
    false clear. `Loading` + `data` must therefore not join into the benign signature `Loading data`.
    """
    window = _window(Title="Loading", Texts=["Loading", "data"], OwnerEnabled=False)

    result = classify(tmp_path, [window])

    assert result["kind"] == "unrecognized", "benign must read individual elements only, never the join"


@pytest.mark.parametrize("phrase", CREDENTIAL_ALTERNATIVES)
def test_every_credential_signature_alternative_is_still_a_hard_stop(tmp_path: Path, phrase: str) -> None:
    """The full true-positive set, re-proved after the text pipeline was rewritten.

    Fixing a false positive by eroding the true positives is the failure mode this whole change is
    most exposed to, so the signature's alternatives are asserted one by one rather than sampled.
    """
    window = _window(Title="Refresh", Texts=["Refresh", phrase], OwnerEnabled=False)

    result = classify(tmp_path, [window], refresh_in_flight=True)

    assert result["verdict"] == "CREDENTIAL_MISSING", f"{phrase!r} must convict even under a benign caption"
    assert result["exit_code"] == 1


def test_the_pinned_alternative_list_still_covers_the_shipped_signature() -> None:
    """If someone adds an alternative to the signature file, the parametrised set above must grow too.

    Without this, the "all alternatives" claim quietly becomes "the alternatives that existed in 2026".
    """
    alternatives = [alt for alt in CREDENTIAL_SIGNATURE.read_text(encoding="utf-8").strip().split("|") if alt]
    signature = re.compile(CREDENTIAL_SIGNATURE.read_text(encoding="utf-8").strip(), re.IGNORECASE)

    assert len(alternatives) == len(CREDENTIAL_ALTERNATIVES), (
        f"signature has {len(alternatives)} alternatives but {len(CREDENTIAL_ALTERNATIVES)} are exercised"
    )
    for phrase in CREDENTIAL_ALTERNATIVES:
        assert signature.search(phrase), f"pinned phrase no longer matches the shipped signature: {phrase!r}"


def test_the_probe_harvests_window_text_through_ui_automation() -> None:
    """Regression pin on the collector: the harvest, its patterns, and its BOUNDS stay wired in.

    The behavioural tests above take `Texts`/`HarvestComplete` as inputs. Only the live tests prove
    they are produced, and those skip where there is no interactive desktop - so this keeps the wiring
    gated everywhere, including on Linux CI.
    """
    body = PROBE_PS1.read_text(encoding="utf-8")

    assert "function Get-AutomationHarvest" in body
    assert "AutomationElement]::FromHandle" in body, "the harvest must start from the window handle"
    assert "TextPattern]::Pattern" in body, (
        "TextPattern is not optional: a read-only RichTextBox with an empty Name exposes its text only there"
    )
    assert "ValuePattern]::Pattern" in body
    assert "$truncated = $true" in body, "hitting the element cap must be recorded, not silently absorbed"
    assert "PatternsIncomplete" in body, "a pattern read that threw must withhold the right to suppress"
    assert "ConvertTo-ProbeWindow" in body and "-Enrich:$isCandidate" in body, (
        "Get-PidWindows must enrich candidate windows, or the classifiers only ever see the caption"
    )
    assert "function Get-BoundedAutomationHarvest" in body and "WaitForExit" in body and "Kill()" in body, (
        "the harvest must run in a killable child process: a hung UIA provider cannot be cancelled in-process"
    )
    assert "ConvertTo-HarvestResult -Payload" in body and "-ExitCode $child.ExitCode" in body, (
        "the collector must route the child payload through the validator, carrying the child's exit code - "
        "the validator is behaviourally tested on its own, but nothing else pins that it is actually WIRED IN"
    )


def test_the_harvest_child_process_is_this_same_script() -> None:
    """The bounded harvest re-invokes THIS file, so the bundle stays copyable as one folder.

    A second script would have to be copied too, and `SKILL.md` promises that copying this one folder
    takes the whole procedure with it.
    """
    body = PROBE_PS1.read_text(encoding="utf-8")

    assert "$PSCommandPath" in body, "the child must be this script, not a sibling file"
    assert "ParameterSetName -eq 'Harvest'" in body
    assert "'HARVEST:'" in body, "the child's payload needs a marker the parent can find in its output"


# --------------------------------------------------------------------------------------------------
# Live regressions for the round-2 exploits (blind review, 2026-08-29)
# --------------------------------------------------------------------------------------------------
#
# Each of these beat the previous `ContentRead` proxy and ended at CREDENTIAL_PRESENT, exit 0. They are
# kept as LIVE tests, not synthesised ones, because every one of them lived in the COLLECTOR - the part
# a synthesised window object cannot exercise by construction.

_WPF_APP_PREAMBLE = r"""
param([Parameter(Mandatory = $true)][string]$ReadyFile)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase
Add-Type -AssemblyName WindowsFormsIntegration
[System.Windows.Forms.Application]::EnableVisualStyles()
$script:form = New-Object System.Windows.Forms.Form
$script:form.Text = 'Fake Desktop'
$script:form.Width = 640
$script:form.Height = 480
$button = New-Object System.Windows.Controls.Button
$button.Content = 'Refresh'
$hostControl = New-Object System.Windows.Forms.Integration.ElementHost
$hostControl.Width = 140
$hostControl.Height = 48
$hostControl.Child = $button
$script:form.Controls.Add($hostControl)
$script:timer = New-Object System.Windows.Forms.Timer
$script:timer.Interval = 800
"""

_WPF_APP_EPILOGUE = r"""
$button.Add_Click({ $script:timer.Start() })
$script:form.Add_Shown({ Set-Content -LiteralPath $ReadyFile -Value 'ready' -Encoding ascii })
[System.Windows.Forms.Application]::Run($script:form)
"""

# Credential text in a read-only RichTextBox: reachable ONLY through TextPattern, with an empty Name,
# and a Cancel button alongside it so the old "we harvested some text" proxy would have been satisfied.
_MODAL_TEXTPATTERN_ONLY = r"""
$script:timer.Add_Tick({
    $script:timer.Stop()
    $modal = New-Object System.Windows.Window
    $modal.Title = 'Refresh'
    $modal.Width = 520
    $modal.Height = 320
    $panel = New-Object System.Windows.Controls.StackPanel
    $rich = New-Object System.Windows.Controls.RichTextBox
    $rich.IsReadOnly = $true
    $paragraph = New-Object System.Windows.Documents.Paragraph
    $paragraph.Inlines.Add((New-Object System.Windows.Documents.Run('Enter your credentials')))
    $rich.Document = New-Object System.Windows.Documents.FlowDocument($paragraph)
    $null = $panel.Children.Add($rich)
    $cancel = New-Object System.Windows.Controls.Button
    $cancel.Content = 'Cancel'
    $null = $panel.Children.Add($cancel)
    $modal.Content = $panel
    $helper = New-Object System.Windows.Interop.WindowInteropHelper($modal)
    $helper.Owner = $script:form.Handle
    $null = $modal.ShowDialog()
  })
"""

# 450 ordinary elements ahead of the credential text: the old 400-element cap truncated silently and
# the result was indistinguishable from "read it all, found nothing".
_MODAL_PAST_THE_ELEMENT_CAP = r"""
$script:timer.Add_Tick({
    $script:timer.Stop()
    $modal = New-Object System.Windows.Window
    $modal.Title = 'Refresh'
    $modal.Width = 520
    $modal.Height = 320
    $panel = New-Object System.Windows.Controls.StackPanel
    for ($i = 0; $i -lt 450; $i++) {
      $filler = New-Object System.Windows.Controls.TextBlock
      $filler.Text = "row $i"
      $null = $panel.Children.Add($filler)
    }
    $block = New-Object System.Windows.Controls.TextBlock
    $block.Text = 'Enter your credentials'
    $null = $panel.Children.Add($block)
    $scroller = New-Object System.Windows.Controls.ScrollViewer
    $scroller.Content = $panel
    $modal.Content = $scroller
    $helper = New-Object System.Windows.Interop.WindowInteropHelper($modal)
    $helper.Owner = $script:form.Handle
    $null = $modal.ShowDialog()
  })
"""

# `Enter your` / `Cancel` / `credentials`: an interactive element interposed between the two halves of
# the signature, which defeated a naive whole-window join.
_MODAL_INTERPOSED_SPLIT = r"""
$script:timer.Add_Tick({
    $script:timer.Stop()
    $modal = New-Object System.Windows.Window
    $modal.Title = 'Refresh'
    $modal.Width = 520
    $modal.Height = 320
    $panel = New-Object System.Windows.Controls.StackPanel
    $first = New-Object System.Windows.Controls.TextBlock
    $first.Text = 'Enter your'
    $null = $panel.Children.Add($first)
    $cancel = New-Object System.Windows.Controls.Button
    $cancel.Content = 'Cancel'
    $null = $panel.Children.Add($cancel)
    $second = New-Object System.Windows.Controls.TextBlock
    $second.Text = 'credentials'
    $null = $panel.Children.Add($second)
    $modal.Content = $panel
    $helper = New-Object System.Windows.Interop.WindowInteropHelper($modal)
    $helper.Owner = $script:form.Handle
    $null = $modal.ShowDialog()
  })
"""

# A modal that WEDGES its own UI thread. UI Automation's FindAll then blocks cross-process, which is
# what held a `-TimeoutSec 1` probe for 15.1s and produced no verdict at all.
_MODAL_WEDGED_UI_THREAD = r"""
$script:timer.Add_Tick({
    $script:timer.Stop()
    $modal = New-Object System.Windows.Window
    $modal.Title = 'Refresh'
    $modal.Width = 520
    $modal.Height = 320
    $block = New-Object System.Windows.Controls.TextBlock
    $block.Text = 'Enter your credentials'
    $modal.Content = $block
    $helper = New-Object System.Windows.Interop.WindowInteropHelper($modal)
    $helper.Owner = $script:form.Handle
    $modal.Add_ContentRendered({ Start-Sleep -Seconds 120 })
    $null = $modal.ShowDialog()
  })
"""


def _run_probe_against_wpf_modal(tmp_path: Path, modal_body: str, extra_args: list[str] | None = None):
    """Launch the fake-Desktop app with ``modal_body``, run the probe, return the completed process.

    ⚠️ **These live tests are CONTENTION-SENSITIVE, and that is not flakiness.** They drive a real GUI
    process and a real cross-process UI Automation harvest, so running them alongside the whole skills
    suite can slow the fixture enough that it never exposes an invokable `Refresh` (skip below), or slow
    the harvest enough that it hits its own timeout and the verdict degrades to `DIALOG_UNREADABLE`.
    Both are the fail-safe working: every degraded path lands in the exit-3 band, never a clear.
    Measured 2026-08-29: a clean run is 100 passed; the same suite under a concurrent full-suite run
    skipped one live test. Prefer asserting on the BAND (never exit 0, never suppressed) and reserve
    exact-verdict assertions for the cases that are stable.
    """
    exe = _powershell()
    app_script = tmp_path / "fake_desktop.ps1"
    app_script.write_text(_WPF_APP_PREAMBLE + modal_body + _WPF_APP_EPILOGUE, encoding="utf-8")
    ready = tmp_path / "ready.txt"
    app = subprocess.Popen(  # pylint: disable=consider-using-with
        [exe, "-Sta", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(app_script), "-ReadyFile", str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            if ready.exists():
                break
            time.sleep(0.5)
        if not ready.exists():
            pytest.skip("the WPF fixture app never showed a window (no interactive desktop?)")
        argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PROBE_PS1)]
        argv += ["-DesktopPid", str(app.pid)]
        extra = extra_args or []
        if "-TimeoutSec" not in extra:
            argv += ["-TimeoutSec", "12"]
        argv += extra
        started = time.monotonic()
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
        done.elapsed = time.monotonic() - started  # type: ignore[attr-defined]
        if "refresh invoked: True" not in done.stdout:
            pytest.skip(f"the fixture app exposed no invokable Refresh to UI Automation:\n{done.stdout}")
        return done
    finally:
        app.kill()
        app.wait(timeout=30)


def test_credential_text_reachable_only_through_textpattern_is_a_hard_stop(tmp_path: Path) -> None:
    """Exploit 1. `Name` + `ValuePattern` alone miss a read-only RichTextBox's content entirely."""
    done = _run_probe_against_wpf_modal(tmp_path, _MODAL_TEXTPATTERN_ONLY)

    assert "VERDICT: CREDENTIAL_MISSING" in done.stdout, done.stdout
    assert done.returncode == 1


def test_credential_text_beyond_the_element_cap_never_reads_as_clean(tmp_path: Path) -> None:
    """Exploit 2. Truncation must not be indistinguishable from a complete, clean read.

    Two acceptable outcomes and one forbidden one: read it all and convict (exit 1), or report the
    harvest incomplete (exit 3). Never exit 0.
    """
    done = _run_probe_against_wpf_modal(tmp_path, _MODAL_PAST_THE_ELEMENT_CAP, ["-HarvestMaxElements", "400"])

    assert done.returncode != 0, f"a truncated harvest must never clear a window:\n{done.stdout}"
    assert "CREDENTIAL_PRESENT" not in done.stdout


def test_credential_text_beyond_the_element_cap_convicts_when_the_cap_allows_it(tmp_path: Path) -> None:
    """The same window with the shipped cap: read in full, and convicted."""
    done = _run_probe_against_wpf_modal(tmp_path, _MODAL_PAST_THE_ELEMENT_CAP)

    assert "VERDICT: CREDENTIAL_MISSING" in done.stdout, done.stdout
    assert done.returncode == 1


def test_a_signature_split_by_an_interposed_button_is_a_hard_stop(tmp_path: Path) -> None:
    """Exploit 3. The prose join must skip interactive elements, or `Cancel` breaks the sentence."""
    done = _run_probe_against_wpf_modal(tmp_path, _MODAL_INTERPOSED_SPLIT)

    assert "VERDICT: CREDENTIAL_MISSING" in done.stdout, done.stdout
    assert done.returncode == 1


def test_a_wedged_uia_provider_still_produces_a_verdict(tmp_path: Path) -> None:
    """The MEDIUM finding: an uncapped UIA walk let a 1s probe run 15s+ and emit nothing at all.

    `FindAll` is a synchronous cross-process call, so neither `try/catch` nor a background thread can
    interrupt it - only killing a child process can.

    The bound is what this test is FOR, and it has to be tight enough to fail. Measured here against a
    modal that sleeps 120s on its own UI thread, with `-TimeoutSec 1 -HarvestTimeoutSec 2`:

        bounded child process : 6.2s   exit 3  VERDICT: DIALOG_UNREADABLE
        in-process harvest    : 70.5s  exit 3  VERDICT: DIALOG_UNREADABLE

    Note both eventually print the SAME verdict - the defect is purely the time budget, so an assertion
    on the verdict alone cannot see it. A first draft used `elapsed < 120` and the mutation walked
    straight through at 70.5s. 25s sits ~4x above the bounded case and ~3x below the unbounded one.
    """
    done = _run_probe_against_wpf_modal(
        tmp_path, _MODAL_WEDGED_UI_THREAD, ["-TimeoutSec", "1", "-HarvestTimeoutSec", "2"]
    )

    assert "VERDICT:" in done.stdout, f"a wedged provider must not swallow the verdict:\n{done.stdout}"
    assert "CREDENTIAL_PRESENT" not in done.stdout, "an unreadable window is not a clean bill of health"
    assert done.returncode == 3
    assert done.elapsed < 25, (
        f"the probe inherited the provider's wedge instead of bounding it (took {done.elapsed:.1f}s)"
    )


# --------------------------------------------------------------------------------------------------
# Round 3: one benign element must not erase the rest of the window, and a payload must be validated
# --------------------------------------------------------------------------------------------------
#
# Both findings are the SAME root cause, for the third time on this branch: missing evidence read as
# good evidence. Round 1 let a caption stand in for content; round 2 let "we read something" stand in
# for "we read the thing that matters"; round 3 let the FIRST benign element stand in for the whole
# window, and a MISSING JSON property stand in for a completed harvest.

NATIVE_QUERY_PROMPT = "Permission is required to run this native database query"


def test_a_native_query_approval_beside_progress_text_is_not_suppressed(tmp_path: Path) -> None:
    """The finding this branch must not ship without: one `Evaluating` erased a known blocking prompt.

    `Permission is required to run this native database query` is Power BI Desktop's native-database-
    query approval modal. It is not hypothetical - `SKILL.md` documents it at length, notes that
    migrated custom-SQL sources emit exactly the shape that triggers it, and instructs the reader to
    check for it *before* concluding anything about credentials. A probe that suppressed it would make
    this bundle contradict its own documentation.

    Exit 3, not 1: a human must act, but the remedy is an approval, not a sign-in - so it must not be
    reported in the band whose documented meaning is "sign in once".
    """
    window = _window(
        Title="Refresh",
        Texts=["Refresh", "Evaluating", NATIVE_QUERY_PROMPT],
        OwnerEnabled=False,
    )

    result = classify(tmp_path, [window], refresh_in_flight=True)

    assert result["kind"] == "needs-human"
    assert result["verdict"] == "DIALOG_NEEDS_HUMAN"
    assert result["exit_code"] == 3
    assert NATIVE_QUERY_PROMPT in (result["evidence"] or ""), "the verdict must carry the prompt it found"


def test_a_native_query_approval_outranks_an_enabled_owner(tmp_path: Path) -> None:
    """Modality exonerates a window we know nothing about, not one we have identified as blocking."""
    window = _window(Title="Refresh", Texts=["Refresh", NATIVE_QUERY_PROMPT], OwnerEnabled=True)

    assert classify(tmp_path, [window], refresh_in_flight=True)["kind"] == "needs-human"


def test_authentication_required_alongside_loading_data_is_not_suppressed(tmp_path: Path) -> None:
    """The second round-3 case: `\\bLoading data\\b` matched as a substring of a real prompt.

    Two independent guards now stop it: the benign expressions are WHOLE-ELEMENT status patterns, so
    `Loading data requires authentication` is not progress text at all; and `Authentication required`
    is itself a known human-blocking prompt.
    """
    window = _window(
        Title="Authentication required",
        Texts=["Authentication required", "Loading data requires authentication"],
        OwnerEnabled=False,
    )

    result = classify(tmp_path, [window], refresh_in_flight=True)

    assert result["exit_code"] == 3
    assert result["verdict"] != "REFRESH_IN_PROGRESS"


def test_unaccounted_prose_beside_progress_text_is_not_suppressed(tmp_path: Path) -> None:
    """The backstop for a blocking prompt in NEITHER signature.

    A window is a progress dialog only when its content is ACCOUNTED FOR: recognised status text, or
    short enough that it cannot be a human-directed prompt. A sentence nobody can explain sitting
    beside `Evaluating` means the progress dialog does not explain this window.
    """
    window = _window(
        Title="Refresh",
        Texts=["Refresh", "Evaluating", "Something entirely unknown is blocking this refresh now"],
        OwnerEnabled=False,
    )

    result = classify(tmp_path, [window], refresh_in_flight=True)

    assert result["kind"] == "mixed-content"
    assert result["verdict"] == "DIALOG_UNRECOGNIZED"
    assert result["exit_code"] == 3


def test_short_data_labels_beside_progress_text_do_not_block_suppression(tmp_path: Path) -> None:
    """Positive control. Without this, "scan everything" could degenerate into "never suppress".

    A real refresh dialog lists table names and row counts next to its status text. Those are short
    data labels, not prose, and they must not veto - otherwise the benign path is unreachable and the
    probe can never return CREDENTIAL_PRESENT while Desktop shows its own refresh dialog.
    """
    window = _window(
        Title="Refresh",
        Texts=["Refresh", "Orders", "Customers", "1,204 rows loaded", "Evaluating", "Cancel"],
        InteractiveTexts=["Cancel"],
        OwnerEnabled=False,
    )

    assert classify(tmp_path, [window], refresh_in_flight=True)["verdict"] is None
    assert classify(tmp_path, [window])["verdict"] == "REFRESH_IN_PROGRESS"


def test_the_benign_signature_matches_whole_elements_not_substrings() -> None:
    """Platform-independent gate on the resource itself: no broad substring alternatives.

    `\\bLoading data\\b` matched inside `Loading data requires authentication`. Every alternative is now
    anchored, so a status word buried in a sentence is not a status.
    """
    benign = re.compile(BENIGN_SIGNATURE.read_text(encoding="utf-8").strip(), re.IGNORECASE)

    for status in ["Refresh", "Evaluating", "Evaluating...", "Loading data", "1,204 rows loaded"]:
        assert benign.search(status), f"a whole-element status must still match: {status!r}"
    for sentence in [
        "Loading data requires authentication",
        "Refresh failed",
        "Evaluating whether you have permission to sign in",
        "Waiting for other queries and then a sign-in prompt",
    ]:
        assert not benign.search(sentence), f"a status word inside a sentence is not a status: {sentence!r}"


def test_the_blocking_prompt_signature_covers_the_documented_native_query_modal() -> None:
    """The prompt `SKILL.md` documents must actually be in the resource that recognises it."""
    blocking = re.compile(BLOCKING_SIGNATURE.read_text(encoding="utf-8").strip(), re.IGNORECASE)

    for phrase in [
        NATIVE_QUERY_PROMPT,
        "This native database query requires your approval",
        "Authentication required",
        "Authentication is required",
    ]:
        assert blocking.search(phrase), f"blocking signature must recognise: {phrase!r}"
    for benign_status in ["Refresh", "Evaluating", "1,204 rows loaded", "Orders"]:
        assert not blocking.search(benign_status), f"progress text must not be a blocking prompt: {benign_status!r}"


@pytest.mark.parametrize(
    "payload,exit_code,why",
    [
        pytest.param({"Items": []}, 0, "both flags missing", id="no-flags"),
        pytest.param({"Items": [], "Truncated": False}, 0, "PatternsIncomplete missing", id="one-flag"),
        pytest.param({"Items": [], "PatternsIncomplete": False}, 0, "Truncated missing", id="other-flag"),
        pytest.param({"Items": [], "Truncated": None, "PatternsIncomplete": None}, 0, "null flags", id="null-flags"),
        pytest.param({"Items": [], "Truncated": 0, "PatternsIncomplete": 0}, 0, "integer flags", id="int-flags"),
        pytest.param(
            {"Items": [], "Truncated": "false", "PatternsIncomplete": "false"}, 0, "string flags", id="str-flags"
        ),
        pytest.param(
            {"Items": [], "Truncated": False, "PatternsIncomplete": False}, 4, "child failed", id="nonzero-exit"
        ),
    ],
)
def test_a_structurally_incomplete_child_payload_is_never_complete(tmp_path: Path, payload, exit_code, why) -> None:
    """A missing property is `$null`, and `-not $null` is `$true` - so absence computed to "complete".

    That coercion happened UPSTREAM of the strict `Test-HarvestComplete` guard, which is why the guard
    did not save it: by the time it ran, the malformed value was already a real Boolean `$true`.
    """
    result = harvest_result(tmp_path, payload, exit_code=exit_code)

    assert result["complete"] is False, f"payload must not be complete ({why})"


def test_a_well_formed_child_payload_is_accepted_as_complete(tmp_path: Path) -> None:
    """Positive control for the validator - otherwise it could pass by rejecting everything."""
    payload = {
        "Items": [{"Text": "Evaluating", "Interactive": False}],
        "Truncated": False,
        "PatternsIncomplete": False,
    }

    result = harvest_result(tmp_path, payload)

    assert result["accepted"] is True
    assert result["complete"] is True
    assert result["items"] == 1


def test_a_malformed_payloads_items_are_still_read_but_never_authorise_suppression(tmp_path: Path) -> None:
    """Keeping the text costs nothing and can only raise credential recall; `Complete` still stays false."""
    payload = {"Items": [{"Text": "Enter your credentials", "Interactive": False}], "Truncated": False}

    result = harvest_result(tmp_path, payload)

    assert result["items"] == 1, "unread text lowers credential recall, so salvage what parsed"
    assert result["complete"] is False, "but a schema-incomplete payload can never authorise benign"


def test_invalid_json_and_flagged_incompleteness_are_rejected(tmp_path: Path) -> None:
    """The two shapes that were already safe, pinned so they stay that way."""
    assert harvest_result(tmp_path, "{not json", exit_code=0)["accepted"] is False
    truncated = {"Items": [], "Truncated": True, "PatternsIncomplete": False}
    assert harvest_result(tmp_path, truncated)["complete"] is False
    patterns = {"Items": [], "Truncated": False, "PatternsIncomplete": True}
    assert harvest_result(tmp_path, patterns)["complete"] is False


# `Evaluating` and the native-query approval prompt in ONE fully-harvested modal - the round-3 High,
# reproduced end to end rather than only at the classifier.
_MODAL_PROGRESS_PLUS_NATIVE_QUERY = r"""
$script:timer.Add_Tick({
    $script:timer.Stop()
    $modal = New-Object System.Windows.Window
    $modal.Title = 'Refresh'
    $modal.Width = 560
    $modal.Height = 320
    $panel = New-Object System.Windows.Controls.StackPanel
    $status = New-Object System.Windows.Controls.TextBlock
    $status.Text = 'Evaluating'
    $null = $panel.Children.Add($status)
    $prompt = New-Object System.Windows.Controls.TextBlock
    $prompt.Text = 'Permission is required to run this native database query'
    $null = $panel.Children.Add($prompt)
    $ok = New-Object System.Windows.Controls.Button
    $ok.Content = 'Cancel'
    $null = $panel.Children.Add($ok)
    $modal.Content = $panel
    $helper = New-Object System.Windows.Interop.WindowInteropHelper($modal)
    $helper.Owner = $script:form.Handle
    $null = $modal.ShowDialog()
  })
"""


def test_a_native_query_prompt_beside_progress_text_is_live_reported(tmp_path: Path) -> None:
    """Round 3's High, end to end: a fully-harvested mixed window must not clear.

    ⚠️ The live tests in this module drive a real GUI process and a real UI Automation harvest, so they
    are CONTENTION-SENSITIVE: running them alongside the whole skills suite can slow the harvest enough
    that it times out and the verdict degrades to `DIALOG_UNREADABLE`. That is the fail-safe working as
    designed, not flakiness - every degraded path still lands in the exit-3 band. Assertions here are
    therefore written against the BAND (never exit 0, never suppressed), with the exact verdict checked
    only where it is stable.
    """
    done = _run_probe_against_wpf_modal(tmp_path, _MODAL_PROGRESS_PLUS_NATIVE_QUERY)

    assert done.returncode == 3, f"a native-query approval must never clear or be a credential stop:\n{done.stdout}"
    assert "CREDENTIAL_PRESENT" not in done.stdout
    assert "REFRESH_IN_PROGRESS" not in done.stdout, "one progress element must not suppress the whole window"
