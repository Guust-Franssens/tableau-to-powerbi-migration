"""Offline tests for the Desktop credential-modal fast path and polling loop."""

from __future__ import annotations

# These tests deliberately monkeypatch module-internal seams so no Desktop, network, or Fabric
# dependency is required.
# pylint: disable=protected-access,too-few-public-methods,invalid-name

import threading
import time
import os
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
        refresh(port=1234, tables=["Orders"], timeout_sec=10, desktop_pid=111)

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
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111)

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
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111)

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
        refresh(port=1234, tables=["Orders"], timeout_sec=0.1, desktop_pid=111)

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
