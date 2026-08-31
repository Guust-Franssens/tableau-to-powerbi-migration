"""Offline tests for the Desktop credential-modal fast path and polling loop."""

from __future__ import annotations

# These tests deliberately monkeypatch module-internal seams so no Desktop, network, or Fabric
# dependency is required.
# pylint: disable=protected-access,too-few-public-methods,invalid-name

import threading
import time
import ctypes
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import pytest
import _credential_modal
import probe_desktop_query
import refresh_pbip_model
from _credential_modal import (
    CredentialDetection,
    CredentialModal,
    CredentialUnknownError,
    DESKTOP_MAIN_CLASS_PREFIX,
    DesktopGoneError,
    DesktopUnreadyError,
    DialogFinding,
    DialogFoundError,
    DesktopWindow,
    _enumerate_pid_windows_with_count,
    classify_dialog,
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


class _ParkedAdomd:
    """ADOMD stand-in whose command never returns, driven as a whole connection factory result.

    Distinct from the ``parked`` fixture: the finding-1 tests need to control the release event and to
    run with a fake progress monitor, so they wire the seams themselves.
    """

    CommandText = ""
    CommandTimeout = 0

    def __init__(self, released: threading.Event) -> None:
        self.released = released

    def Open(self) -> None:
        """Match the ADOMD API surface."""

    def CreateCommand(self):
        """This object is its own command."""
        return self

    def ExecuteNonQuery(self) -> None:
        """Block until the test releases it, so only the deadline can end the wait."""
        self.released.wait(timeout=600)

    def Close(self) -> None:
        """Match the ADOMD API surface."""


class _FakeProgressMonitor:
    """Minimal ``RefreshProgressMonitor`` stand-in so the progress-monitor wait branch is reachable.

    That branch needs an AMO trace, which needs pythonnet and a live server; without this the branch is
    untestable off Windows and its own copy of the poll loop went unexercised - which is how it kept a
    t=0 raise helper and discarded its latches (#400 review, finding 1).
    """

    def mark_refresh_started(self) -> None:
        """No-op."""

    def print_liveness_warning_if_due(self) -> None:
        """No-op."""

    def seconds_until_liveness_warning(self) -> float:
        """Never due, so the poll interval is what drives the loop."""
        return 999.0

    def print_evidence_heartbeat(self, elapsed: float, total: float) -> None:
        """No-op."""

    def close(self) -> None:
        """No-op."""

    def summary(self) -> str:
        """Match the monitor's reporting surface."""
        return "fake"


MAIN_HWND = 0x10001
DIALOG_HWND = 0x20002


def main_window(
    title: str = "sample-superstore",
    texts: tuple[str, ...] = ("Report",),
    *,
    minimized: bool = False,
    width: int = 2011,
    height: int = 1298,
) -> DesktopWindow:
    """The Power BI Desktop application FRAME: unowned, and the window a modal would disable."""
    return DesktopWindow(
        title,
        DESKTOP_MAIN_CLASS_PREFIX + ".app.0.33c0d9d",
        width,
        height,
        texts,
        minimized=minimized,
        hwnd=MAIN_HWND,
    )


def owned_dialog(
    texts: tuple[str, ...] = (),
    *,
    title: str = "",
    class_name: str = "WindowsForms10.Window.20008.app.0.33c0d9d",
    width: int = 702,
    height: int = 355,
    owner_enabled: bool | None = False,
    hwnd: int = DIALOG_HWND,
) -> DesktopWindow:
    """A dialog OWNED by :func:`main_window`.

    ``owner_enabled=False`` by default - the owner is disabled, which is what a modal does. That does
    not convict the window (Power BI's own refresh dialog disables the owner too); it only means the
    one-way enabled-owner exoneration does not apply, so the window has to be classified on its text.
    """
    return DesktopWindow(
        title,
        class_name,
        width,
        height,
        texts,
        hwnd=hwnd,
        owner_hwnd=MAIN_HWND,
        owner_enabled=owner_enabled,
    )


def modal() -> CredentialModal:
    """A credential dialog matching the measured incident window."""
    return CredentialModal(
        "Please specify how to connect to this data source",
        DesktopWindow("", "WindowsForms10.Window.20008", 702, 355, ("Please specify how to connect",)),
    )


def modal_state() -> CredentialDetection:
    """CredentialDetection containing the standard modal."""
    return CredentialDetection(modal=modal())


def unreadable_dialog_window() -> DesktopWindow:
    """The live SQL credential-dialog shape: visible, owned, empty title, NO readable text at all."""
    return owned_dialog()


def progress_dialog_window() -> DesktopWindow:
    """Power BI's own Refresh progress dialog: a caption AND content that positively reads as status.

    This is the shape the size-only rule reported as a hard stop (issue #376) - >= 100x100, non-main
    class, and therefore indistinguishable from a sign-in prompt to a detector that never read it.
    """
    return owned_dialog(("Refresh", "Evaluating...", "Cancel"), title="Refresh")


def dialog_finding(window: DesktopWindow | None = None) -> DialogFinding:
    """A real classification of ``window`` (default: the unreadable owned dialog)."""
    return classify_dialog(window or unreadable_dialog_window())


def dialog_state(window: DesktopWindow | None = None) -> CredentialDetection:
    """CredentialDetection carrying a real dialog finding."""
    return CredentialDetection(dialog=dialog_finding(window))


def minimized_main_windows() -> list[DesktopWindow]:
    """The measured minimized-owner shape: a small, iconic Desktop main window and nothing else."""
    return [main_window(title="Report", minimized=True, width=159, height=27)]


def restored_main_windows() -> list[DesktopWindow]:
    """The same owner restored: full-size, not iconic, and - measured - the dialog does NOT return."""
    return [main_window(title="Report")]


def visible_unreadable_dialog_windows() -> list[DesktopWindow]:
    """A visible owned dialog (empty title, unreadable) alongside the healthy main window."""
    return [owned_dialog(), main_window(title="Report")]


def visible_progress_dialog_windows() -> list[DesktopWindow]:
    """Power BI's own Refresh progress dialog alongside the healthy main window (issue #376)."""
    return [progress_dialog_window(), main_window(title="Report")]


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

    def detect(pid: int, **_kwargs) -> CredentialDetection:
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
            owned_dialog(("Enter your credentials",)),
            main_window(),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0, ()),
        ],
        222: [main_window(title="cached-model")],
    }
    hit = inspect_credential_modal(111, lambda pid: windows_by_pid[pid]).modal
    miss = inspect_credential_modal(222, lambda pid: windows_by_pid[pid])

    assert hit is not None
    assert hit.window.class_name.startswith("WindowsForms10.Window.20008")
    assert miss.modal is None
    assert miss.unknown_reason is None


def test_unreadable_owned_dialog_reports_unreadable_not_no_modal() -> None:
    """Live SQL dialog shape: visible owned dialog, empty title, no readable text.

    Absence of text is its own finding - ``DIALOG_UNREADABLE`` - and must never decay into "healthy".
    """
    windows_by_pid = {
        58104: [
            owned_dialog(),
            main_window(texts=("sample",)),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0, ()),
        ],
        46256: [main_window(title="cached", texts=("cached",))],
    }
    blocked = inspect_credential_modal(58104, lambda pid: windows_by_pid[pid])
    healthy = inspect_credential_modal(46256, lambda pid: windows_by_pid[pid])

    assert blocked.modal is None
    assert blocked.dialog is not None
    assert blocked.dialog.kind == "unreadable"
    assert blocked.dialog.verdict == "DIALOG_UNREADABLE"
    assert blocked.dialog.window.width == 702
    assert healthy.modal is None
    assert healthy.dialog is None


def test_an_unowned_zero_area_helper_window_does_not_block() -> None:
    """A window with NO owner and NO pixels blocks nothing - on two independent grounds, not on size.

    `Internet Explorer_Hidden` at 0x0 rides along on healthy instances. It is excluded because it has
    no owner to disable AND nothing to display, never because of its name or its geometry: the same
    class at 900x700, and the same 0x0 shape with an owner, are both classified (see the round-3
    tests below).
    """
    state = inspect_credential_modal(
        111,
        lambda _pid: [
            main_window(title="cached", texts=("cached",)),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0, ()),
        ],
    )

    assert state.modal is None
    assert state.dialog is None


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 EnumWindows callback is Windows-only")
def test_real_win32_enumeration_callback_runs_against_this_process() -> None:
    """Execute the real ctypes callback; it must not be an empty fallback that swallowed exceptions."""
    windows, visited = _enumerate_pid_windows_with_count(os.getpid())

    assert isinstance(windows, list)
    assert visited > 0, "EnumWindows callback did not run; an empty fallback would hide detector failures"


def test_minimized_owner_reports_unknown_not_no_dialog() -> None:
    """When the owner is minimized, Windows hides owned dialogs; absence is indeterminate."""
    state = inspect_credential_modal(111, lambda _pid: [main_window(minimized=True)])

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
    assert state.dialog is None
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
    assert state.dialog is None
    assert state.unknown_reason is None
    assert state.process_gone is not None
    assert "no longer running" in state.process_gone
    assert state.process_gone == harvested_desktop_gone_reason()


@pytest.mark.timing
def test_direct_refresh_returns_credential_missing_fast_at_t0(monkeypatch) -> None:
    """A t=0 credential modal must not wait for the XMLA deadline."""
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: modal_state())
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", explode("_load_adomd"))

    started = time.monotonic()
    with pytest.raises(CredentialMissingError):
        refresh(port=1234, tables=["Orders"], timeout_sec=5, desktop_pid=111, source_hint="Sql.Database(server)")
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"t=0 modal path waited {elapsed:.3f}s instead of returning immediately"


@pytest.mark.timing
def test_direct_refresh_stops_at_t0_for_a_dialog_it_could_not_read(monkeypatch) -> None:
    """An unreadable dialog present BEFORE we start anything must stop immediately, not wait for XMLA.

    t=0 is the one moment a dialog finding is acted on rather than latched: nothing of ours is running,
    the dialog is somebody else's, and stacking a refresh on an unclassified dialog is what the
    2026-08-28 field report had to unpick by hand.
    """
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: dialog_state())
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", explode("_load_adomd"))

    started = time.monotonic()
    with pytest.raises(DialogFoundError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=5, desktop_pid=111)
    elapsed = time.monotonic() - started

    assert excinfo.value.finding.verdict == "DIALOG_UNREADABLE"
    assert elapsed < 0.5


@pytest.mark.timing
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


@pytest.mark.timing
def test_refresh_poll_catches_late_modal(monkeypatch, parked) -> None:
    """A credential dialog appearing after XMLA starts is caught on the next poll."""
    calls = {"count": 0}

    def late_modal(_pid: int):
        calls["count"] += 1
        return modal() if calls["count"] >= 2 else None

    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda pid, **_kw: CredentialDetection(modal=late_modal(pid)),
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
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid, **_kw: CredentialDetection())
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
        lambda _pid, **_kw: CredentialDetection(unknown_reason=reason),
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

    def initial_unknown_then_healthy(_pid: int, **_kw) -> CredentialDetection:
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

    def initial_unready_then_healthy(_pid: int, **_kw) -> CredentialDetection:
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


def _real_detector(enumerator: _ScriptedEnumerator, process_is_alive=lambda _pid: True, *, in_flight: bool = False):
    """A detector that drives the REAL inspect_credential_modal through the scripted enumerator.

    ``process_is_alive`` is injected too (issue #158) so the zero-window liveness split is exercised
    deterministically without a live PID; it defaults to "alive" and is only consulted when the
    enumerator yields an empty window list. ``in_flight`` (issue #376) selects the variant
    ``join_with_credential_poll`` uses by default, which ignores a positively-read progress dialog.
    """
    if in_flight:
        return lambda pid: _credential_modal.inspect_credential_modal_in_flight(
            pid, enumerator, process_is_alive=process_is_alive
        )
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


def test_poll_loop_latches_unreadable_dialog_and_raises_at_the_deadline(monkeypatch) -> None:
    """#376: a dialog we could not read is LATCHED mid-wait and surfaced at the deadline, not raised.

    Before #376 this raised on the first poll, which meant a Power BI Refresh progress dialog aborted
    the very refresh it was reporting on. It must not abort a run that may still finish - but it also
    means "no modal appeared" is no longer established, so it cannot be erased by a quiet deadline
    either. ``enumerator.calls >= 2`` is the part that fails if the raise is restored.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_credential_modal, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([visible_unreadable_dialog_windows()])

    with pytest.raises(DialogFoundError) as excinfo:
        join_with_credential_poll(
            worker,
            pid=111,
            total_timeout=1.0,
            heartbeat_seconds=1000.0,
            poll_seconds=0.1,
            detector=_real_detector(enumerator),
        )

    assert excinfo.value.pid == 111
    assert excinfo.value.finding.verdict == "DIALOG_UNREADABLE"
    assert enumerator.calls >= 2, "a dialog finding is latch-and-wait, not terminal"


def test_poll_loop_ignores_our_own_refresh_progress_dialog(monkeypatch) -> None:
    """#376 core: Power BI's own progress dialog must not end the wait it is reporting on.

    The fails-before/passes-after test for the size-only rule. Master returned this >= 100x100 non-main
    window as a ``blocking_dialog`` and the loop raised at once; now its CONTENT is read, it classifies
    ``benign``, and the in-flight detector ignores it - so the loop reaches its ordinary deadline and
    returns ``False``.
    """
    clock = _FakeClock()
    monkeypatch.setattr(_credential_modal, "time", clock)
    worker = _ImmortalWorker(clock, step=0.4)
    enumerator = _ScriptedEnumerator([visible_progress_dialog_windows()])

    result = join_with_credential_poll(
        worker,
        pid=111,
        total_timeout=1.0,
        heartbeat_seconds=1000.0,
        poll_seconds=0.1,
        detector=_real_detector(enumerator, in_flight=True),
    )

    assert result is False
    assert enumerator.calls >= 2


def test_poll_loop_default_detector_is_the_in_flight_one() -> None:
    """The in-flight variant must be the DEFAULT, not merely available (issue #376).

    Every production caller relies on the default: `refresh()` passes no `detector`. A wiring test is
    the only thing that catches "the right function exists but nothing calls it" - the shape #390 hit
    when a payload validator was behaviourally tested but never pinned as wired in.
    """
    default = inspect.signature(join_with_credential_poll).parameters["detector"].default

    assert default is _credential_modal.inspect_credential_modal_in_flight
    assert default is not inspect_credential_modal


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


def test_refresh_and_save_wires_a_dialog_finding_to_exit_3(monkeypatch, capsys) -> None:
    """#376 wiring: a DialogFoundError must surface at exit 3, never in the exit-1 hard-stop band.

    Exit 1 is reserved for a matched credential signature. This module cannot establish that a dialog
    BLOCKS anything, so nothing it observes about one may enter that band - and ``probe_live_source``
    maps the exit-1 credential family to "you may NOT build; a person must sign in". The finding is a
    real classification and the verdict line is the real emitter's, so this pins raise -> emit -> exit
    code with no hand-written text.
    """

    def _raise_dialog(*_args, **_kwargs):
        raise DialogFoundError(111, dialog_finding())

    monkeypatch.setattr(refresh_pbip_model, "refresh", _raise_dialog)
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "111", "--tables", "Orders"])

    exit_code = refresh_pbip_model._refresh_and_save(111, 5000, None, args)
    out = capsys.readouterr().out

    assert exit_code == 3, "a dialog finding must not reuse the credential hard stop's exit 1"
    assert "REFRESH: DIALOG_UNREADABLE pid=111" in out
    assert "BLOCKED_BY_DIALOG" not in out


def test_refresh_main_reports_a_t0_dialog_at_exit_3_before_any_mutation(monkeypatch, tmp_path: Path, capsys) -> None:
    """#376: the MUTATING CLI stops at t=0 for a dialog it could not read - at exit 3, and before XMLA.

    Two properties in one, and both matter: it must not stack a refresh on somebody else's dialog
    (``discover_port`` explodes if it gets that far), and it must not call it a credential wall.
    """
    model_folder(tmp_path, "MyMigration")
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: dialog_state())
    monkeypatch.setattr(refresh_pbip_model, "discover_port", explode("discover_port"))

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3
    assert out.startswith("REFRESH: DIALOG_UNREADABLE")


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


@pytest.mark.timing
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


@pytest.mark.timing
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


def test_probe_query_stops_at_exit_3_for_a_dialog_it_could_not_read(monkeypatch, capsys) -> None:
    """#376: an unreadable dialog stops the GATE OF RECORD at exit 3, never in the exit-1 hard-stop band.

    Master emitted ``PREFLIGHT: BLOCKED_BY_DIALOG`` at exit 1 from a SIZE-ONLY test, which
    ``probe_live_source`` maps to ``NO_CREDENTIAL`` ("you may NOT build; a person must sign in"). Exit 3
    means "could not probe" and makes no claim about the source at all.
    """
    monkeypatch.setattr(probe_desktop_query, "_credential_state", lambda _pid, **_kw: dialog_state())
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3, "a dialog we could not read must not enter the exit-1 hard-stop band"
    assert out.startswith("PREFLIGHT: DIALOG_UNREADABLE")
    assert "BLOCKED_BY_DIALOG" not in out


def test_probe_query_does_not_stop_for_power_bi_s_own_progress_dialog(monkeypatch, capsys) -> None:
    """#376 acceptance 1: a Refresh progress dialog is not reported as a credential wall.

    Driven through the REAL detector so the classification and the CLI branch on it are proven as one
    chain. On master this window (>= 100x100, non-main class) produced ``BLOCKED_BY_DIALOG`` at exit 1;
    now its content is read, it classifies ``benign``, and at t=0 it reports ``REFRESH_IN_PROGRESS`` at
    exit 3 - Desktop is busy, so a one-row probe of a mid-refresh model would not be trustworthy anyway.
    """
    windows = visible_progress_dialog_windows()
    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda pid, **kw: inspect_credential_modal(pid, lambda _pid: windows, **kw),
    )
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3
    assert out.startswith("PREFLIGHT: REFRESH_IN_PROGRESS")
    assert "CREDENTIAL_MISSING" not in out
    assert "BLOCKED_BY_DIALOG" not in out


def test_probe_query_poll_ignores_a_progress_dialog_while_the_query_runs(monkeypatch, capsys) -> None:
    """#376: a progress dialog appearing DURING the read-only probe must not abort it.

    The poll runs with ``in_flight=True``, so a positively-read progress dialog is ignored and the DAX
    result - the gate of record - is what decides. On master the same window returned exit 1 on the
    first poll and the probe's own answer was never heard.
    """
    windows = visible_progress_dialog_windows()
    seen = {"in_flight": []}

    def state(pid: int, *, in_flight: bool = False) -> CredentialDetection:
        seen["in_flight"].append(in_flight)
        # Healthy at t=0; the progress dialog appears once the probe is already running.
        if not in_flight:
            return CredentialDetection()
        return inspect_credential_modal(pid, lambda _pid: windows, operation_in_flight=in_flight)

    def fake_probe(_port: int, _tables: list[str] | None, emit=print) -> int:
        time.sleep(0.15)
        emit("PREFLIGHT: DATA_OK")
        return 0

    monkeypatch.setattr(probe_desktop_query, "_credential_state", state)
    monkeypatch.setattr(probe_desktop_query, "PREFLIGHT_CREDENTIAL_POLL_SECONDS", 0.02)
    monkeypatch.setattr(probe_desktop_query, "discover_port", lambda _pid: 52001)
    monkeypatch.setattr(probe_desktop_query, "probe", fake_probe)

    exit_code = probe_desktop_query._probe_with_credential_poll(111, 52001, ["Orders"])
    out = capsys.readouterr().out

    assert exit_code == 0, "a progress dialog must not abort a probe that was going to succeed"
    assert "PREFLIGHT: DATA_OK" in out
    assert seen["in_flight"][1:], "the poll must have run at least once"
    assert all(seen["in_flight"][1:]), "every poll after t=0 must use the in-flight detector"
    assert seen["in_flight"][0] is False, "the t=0 check must NOT be in-flight"


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
        lambda pid, **_kw: CredentialDetection(modal=late_modal(pid)),
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
        lambda pid, **_kw: CredentialDetection(modal=late_modal(pid)),
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
# _credential_modal.classify_dialog - the PYTHON detector's own dialog classifiers (issue #376)
# ==================================================================================================
#
# The Python fast path had the same defect the PowerShell arbiter shed in #367, on a more dangerous
# route: `inspect_credential_modal` returned the FIRST visible non-main window >= 100x100 as a
# `blocking_dialog`, and both callers mapped that to `BLOCKED_BY_DIALOG` at exit 1 - which
# `probe_live_source` classifies `NO_CREDENTIAL`. It feeds `probe_desktop_query.py`, the gate of
# record, so a Power BI Refresh progress dialog could halt a migration and send an operator to a
# sign-in screen that was never on show.
#
# These drive the REAL classifier against synthesised windows: no Desktop, no Win32, every platform.


@pytest.mark.parametrize(
    ("texts", "title", "kind", "verdict"),
    [
        # 1. A progress dialog is NOT a credential wall. The whole point of the issue.
        (("Refresh", "Evaluating...", "Cancel"), "Refresh", "benign", "REFRESH_IN_PROGRESS"),
        (("Refresh", "1,204 rows loaded"), "Refresh", "benign", "REFRESH_IN_PROGRESS"),
        # 2. A genuine credential prompt still is - fixing the false positive must not break this.
        (("Please specify how to connect",), "", "credential", "CREDENTIAL_MISSING"),
        (("You aren't signed in",), "", "credential", "CREDENTIAL_MISSING"),
        # 3. Could-not-determine is its own state, never folded into either of the first two.
        ((), "", "unreadable", "DIALOG_UNREADABLE"),
        (("Refresh",), "Refresh", "benign-title-only", "DIALOG_UNREADABLE"),
        (("Save changes?", "Discard"), "Save changes?", "unrecognized", "DIALOG_UNRECOGNIZED"),
        # 4. Known human-blocking prompts that are NOT sign-in prompts get their own verdict, and
        #    outrank progress text in the same window (the round-3 defect in #390).
        (
            ("Permission is required to run this native database query",),
            "",
            "needs-human",
            "DIALOG_NEEDS_HUMAN",
        ),
        (
            ("Refresh", "Evaluating...", "Permission is required to run this native database query"),
            "Refresh",
            "needs-human",
            "DIALOG_NEEDS_HUMAN",
        ),
        # 5. Progress text cannot account for prose nobody explained.
        (
            ("Refresh", "Evaluating...", "Your workbook contains unsaved changes that will be lost"),
            "Refresh",
            "mixed-content",
            "DIALOG_UNRECOGNIZED",
        ),
    ],
)
def test_classify_dialog_reads_the_window_instead_of_measuring_it(texts, title, kind, verdict) -> None:
    """Every one of these windows is 702x355 and non-main, so SIZE cannot separate them - text must."""
    finding = classify_dialog(DesktopWindow(title, "WindowsForms10.Window.20008.app.0.x", 702, 355, texts))

    assert (finding.kind, finding.verdict) == (kind, verdict)


def test_only_the_credential_verdict_is_a_hard_stop() -> None:
    """#376's failure direction: nothing but a matched credential signature may reach exit 1.

    A false hard stop halts a migration and demands a human; a false clear lands on a verdict the repo
    already treats as untrustworthy on its own. One direction has a backstop, the other does not - so
    every non-credential kind is exit 3 by construction, not by each caller remembering to make it so.
    """
    hard_stop = {
        kind
        for kind, verdict in _credential_modal.DIALOG_KIND_VERDICTS.items()
        if verdict == _credential_modal.VERDICT_CREDENTIAL_MISSING
    }

    assert hard_stop == {"credential"}, f"a non-credential kind reaches the hard-stop verdict: {hard_stop}"


def test_a_caption_alone_can_never_dismiss_a_dialog() -> None:
    """#390's round-1 lesson, ported: benign reads CONTENT only, never the caption.

    Win32 child-HWND enumeration sees nothing inside a WPF dialog, so a caption is frequently all there
    is. If the caption could dismiss, an owned WPF modal captioned `Refresh` whose real content asks
    for a sign-in would be waved through in silence.
    """
    caption_only = classify_dialog(DesktopWindow("Refresh", "Cls", 702, 355, ("Refresh",)))
    with_content = classify_dialog(DesktopWindow("Refresh", "Cls", 702, 355, ("Refresh", "Evaluating...")))

    assert caption_only.kind == "benign-title-only"
    assert caption_only.verdict == "DIALOG_UNREADABLE"
    assert with_content.kind == "benign"


def test_the_credential_scan_is_not_gated_by_the_size_filter() -> None:
    """#376's silent false NEGATIVE: the 100x100 filter used to gate the HARD STOP as well.

    `match_credential_modal` was fed only `blocking_dialog_candidates(...)`, so a credential prompt in
    a smaller window produced NO finding at all - not even a dialog one. `Test-CredentialModal` in the
    PowerShell arbiter has always scanned every window; the two now agree.
    """
    small = owned_dialog(("Enter your credentials",), width=80, height=60)

    state = inspect_credential_modal(111, lambda _pid: [small, main_window()])

    assert state.modal is not None, "a credential prompt below the size filter must still be a hard stop"
    assert state.modal.window.width == 80


def test_benign_is_suppressed_only_while_our_own_operation_is_in_flight() -> None:
    """The one asymmetry: at t=0 a progress dialog is somebody ELSE's refresh, so it is reported.

    Stacking a second refresh on a running one is exactly what the 2026-08-28 field report had to
    unpick by hand. Once we have started our own, the same dialog is ours and must not end the wait.
    Nothing but `benign` is affected - an unreadable dialog surfaces either way.
    """
    windows = visible_progress_dialog_windows()

    at_t0 = inspect_credential_modal(111, lambda _pid: windows)
    in_flight = inspect_credential_modal(111, lambda _pid: windows, operation_in_flight=True)
    unreadable_in_flight = inspect_credential_modal(
        111, lambda _pid: visible_unreadable_dialog_windows(), operation_in_flight=True
    )

    assert at_t0.dialog is not None and at_t0.dialog.verdict == "REFRESH_IN_PROGRESS"
    assert in_flight.dialog is None
    assert unreadable_in_flight.dialog is not None, "in-flight must only ever dismiss a PROVEN-benign dialog"


def test_a_window_we_could_not_account_for_outranks_a_progress_dialog() -> None:
    """Precedence: `benign` carries the only positive evidence, so it must never mask another window.

    Two dialogs up at once - one plainly a progress dialog, one unreadable. If `benign` won, a single
    progress dialog would hide a real modal sitting beside it.
    """
    windows = [
        progress_dialog_window(),
        owned_dialog(hwnd=0x30003),
        main_window(),
    ]

    finding = _credential_modal.dialog_verdict(windows)

    assert finding is not None
    assert finding.verdict == "DIALOG_UNREADABLE"


def test_the_python_detector_cannot_emit_blocked_by_dialog_again(capsys) -> None:
    """Anti-regression: the size-only rule and its verdict token must not come back (issue #376).

    Checked BEHAVIOURALLY, not by grepping the source - the token is named in several docstrings on
    purpose, because a reader who meets it in an old transcript needs to find out why it went. So this
    drives every classifiable kind through both real emitters and asserts none of them prints it, and
    pins that the size-only helper and its field are gone from the module's namespace.
    """
    windows = {
        "unreadable": DesktopWindow("", "Cls", 702, 355, ()),
        "unrecognized": DesktopWindow("Save?", "Cls", 702, 355, ("Save?", "Discard")),
        "needs-human": DesktopWindow("", "Cls", 702, 355, ("Permission is required to run this native query",)),
        "benign": progress_dialog_window(),
        "benign-title-only": DesktopWindow("Refresh", "Cls", 702, 355, ("Refresh",)),
        "mixed-content": DesktopWindow(
            "Refresh", "Cls", 702, 355, ("Refresh", "Evaluating...", "This will discard all unsaved work")
        ),
    }
    for window in windows.values():
        finding = classify_dialog(window)
        refresh_pbip_model._emit_dialog_finding(111, finding)
        probe_desktop_query._emit_dialog_finding(111, finding)
    out = capsys.readouterr().out

    assert out.strip(), "the emitters printed nothing at all, so this test proved nothing"
    assert "BLOCKED_BY_DIALOG" not in out, "an emitter still prints the size-only hard-stop token"
    assert "BLOCKED_BY_DIALOG" not in set(_credential_modal.DIALOG_KIND_VERDICTS.values())
    assert not hasattr(_credential_modal, "blocking_dialog_candidates"), "the size-only detector came back"
    assert "blocking_dialog" not in CredentialDetection.__dataclass_fields__, "the size-only field came back"


def test_every_finding_kind_has_operator_guidance() -> None:
    """A verdict with no guidance is a stop with no next step - and every emitter prints this line."""
    reportable = set(_credential_modal.DIALOG_KIND_PRECEDENCE)
    documented = set(_credential_modal.DIALOG_KIND_GUIDANCE)

    assert reportable <= documented, f"kinds with no guidance: {sorted(reportable - documented)}"


# --------------------------------------------------------------------------------------------------
# Blind-review findings on PR #400. Each one is a case where "we could not establish it" was still
# collapsing into the clean bucket - the same defect class issue #376 exists to remove, found again on
# the code that removed it. Every test below fails on the PR-#400 build and passes on this one.
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "Please enter your password",  # 4 words - under the old MIN_PROMPT_WORDS amnesty
        "Password:",  # 2 words
        "Sign in",
        "Enter password",
    ],
)
def test_short_prompt_beside_progress_text_is_never_dismissed(prompt: str) -> None:
    """Finding 5 (HIGH, safety regression): a length heuristic is not evidence of harmlessness.

    ``MIN_PROMPT_WORDS = 5`` accepted every unmatched element under five words, so a real prompt
    sitting beside ``Evaluating...`` classified ``benign`` - and ``dialog_verdict`` then SUPPRESSED it
    entirely while our own operation was in flight. Measured on the PR-#400 build:
    ``inspect_credential_modal(..., operation_in_flight=True)`` returned ``dialog=None``, i.e. the same
    state as a healthy Desktop, for a window reading *"Please enter your password"*.

    In that shape the fix was worse than the bug: the repo's standing rule is that a credential modal
    is never worked around, and this path defeated it silently. None of these strings matches
    ``credential_modal_signature.regex``, so the all-window prepass does not rescue them either -
    which is exactly why an unexplained element must veto rather than be excused.
    """
    window = owned_dialog(("Refresh", "Evaluating...", prompt), title="Refresh")

    finding = classify_dialog(window)
    in_flight = _credential_modal.dialog_verdict([main_window(), window], operation_in_flight=True)

    assert finding.kind == "mixed-content", f"{prompt!r} was accounted for by nothing and must veto"
    assert finding.verdict == "DIALOG_UNRECOGNIZED"
    assert finding.evidence == prompt
    assert in_flight is not None, f"{prompt!r} was SUPPRESSED in flight - a prompt swallowed in silence"


def test_only_enumerated_chrome_is_excused_beside_progress_text() -> None:
    """The positive half of finding 5: dismissal needs a POSITIVE claim, not a short string.

    ``Cancel``/``OK``/``Close`` are whole-element, anchored control labels that carry no prompt, so a
    window whose only unexplained content is one of them still shows a human nothing to act on. A table
    name does NOT get that excuse - it is short, but shortness was never the property that mattered.
    """
    chrome = DesktopWindow("Refresh", "Cls", 702, 355, ("Refresh", "Evaluating...", "Cancel", "OK"))
    table_name = DesktopWindow("Refresh", "Cls", 702, 355, ("Refresh", "Evaluating...", "Orders"))

    assert classify_dialog(chrome).kind == "benign", "the benign path must stay reachable"
    assert classify_dialog(table_name).kind == "mixed-content"
    assert classify_dialog(table_name).verdict == "DIALOG_UNRECOGNIZED"


@pytest.mark.parametrize(
    ("width", "height", "texts", "kind"),
    [
        (80, 60, (), "unreadable"),
        (80, 60, ("Refresh",), "benign-title-only"),
        (40, 20, ("Continue?", "Yes"), "unrecognized"),
        (1, 1, (), "unreadable"),
        # Round 3, finding 3: a real native `WS_VISIBLE` OWNED 0x0 window with a DISABLED owner is a
        # genuinely blocking shape. Zero area alone must never suppress.
        (0, 0, (), "unreadable"),
        (0, 700, ("Password:",), "unrecognized"),
    ],
)
def test_a_small_dialog_is_still_classified(width: int, height: int, texts, kind: str) -> None:
    """Findings 3 (round 2) and 3 (round 3): geometry is not evidence, at any magnitude.

    Round 2 measured a visible 80x60 unreadable owned window returning
    ``modal=None, dialog=None, unknown_reason=None`` - byte-identical to a healthy Desktop. Round 3
    then built a real ``WS_VISIBLE`` owned **0x0** window with ``CreateWindowEx`` and disabled its
    owner (``owned-visible=True owner-win32-enabled=False rect=0x0``) and showed the follow-up fix
    still suppressed it - which on the unbounded query-poll path is a false clear that is also a hang.

    An arbitrary geometry threshold is not evidence of harmlessness, and neither is zero.
    """
    dialog = owned_dialog(texts, title=texts[0] if texts else "", width=width, height=height)

    state = inspect_credential_modal(111, lambda _pid: [dialog, main_window()])

    assert state.dialog is not None, "an owned dialog must not vanish into the healthy state"
    assert state.dialog.kind == kind
    assert state.dialog.window.width == width


def test_an_owned_dialog_sharing_its_owners_class_is_still_a_credential_wall() -> None:
    """Round 3, finding 2: a class PREFIX names a WinForms family, not one HWND.

    A native WinForms experiment showed an owner and its owned ``FixedDialog`` both reporting the exact
    class ``WindowsForms10.Window.8.app.0.2b2196a_r3_ad1``. The previous ``is_desktop_main_window``
    predicate treated any such window as the application, so this shape returned **no modal, no dialog
    finding, no unknown state** - a real credential dialog removed from the prepass AND from
    classification by the very fix that was meant to stop a report title fabricating one.
    """
    shared_class = "WindowsForms10.Window.8.app.0.2b2196a_r3_ad1"
    frame = DesktopWindow("sample - Power BI Desktop", shared_class, 2011, 1298, ("sample",), hwnd=MAIN_HWND)
    dialog = owned_dialog(("Enter your credentials",), class_name=shared_class)

    state = inspect_credential_modal(111, lambda _pid: [frame, dialog])

    assert state.modal is not None, "an owned dialog sharing its owner's class is still a dialog"
    assert state.modal.window is dialog
    assert state.modal.window.class_name == shared_class


def test_an_owned_dialog_sharing_its_owners_class_is_classified_when_unreadable() -> None:
    """The same shape without a signature match must still surface, not disappear."""
    shared_class = "WindowsForms10.Window.8.app.0.2b2196a_r3_ad1"
    frame = DesktopWindow("sample", shared_class, 2011, 1298, ("sample",), hwnd=MAIN_HWND)
    dialog = owned_dialog(class_name=shared_class)

    state = inspect_credential_modal(111, lambda _pid: [frame, dialog])

    assert state.modal is None
    assert state.dialog is not None
    assert state.dialog.verdict == "DIALOG_UNREADABLE"


@pytest.mark.parametrize(
    ("texts", "verdict"),
    [
        ((), "DIALOG_UNREADABLE"),
        (("Password:",), "DIALOG_UNRECOGNIZED"),
        (("Sign in",), "DIALOG_UNRECOGNIZED"),
        (("Continue?",), "DIALOG_UNRECOGNIZED"),
    ],
)
def test_the_aad_host_is_classified_by_modality_not_by_its_name(texts, verdict: str) -> None:
    """Round 3, finding 1: a NAME allowlist hid the AAD sign-in host whatever it displayed.

    ``Internet Explorer_Hidden`` was excluded from classification unconditionally, so keeping it in the
    credential prepass only ever rescued EXACT signature matches. Measured on the round-2 build: a
    visible 900x700 host reading ``Password:`` / ``Sign in`` / ``Continue?`` - or nothing at all -
    returned ``modal=None, dialog=None, unknown=None``.

    There is no name allowlist any more. This window is classified because it is OWNED and its owner is
    not proven enabled, which is what "blocking" means; its class is not consulted at all.
    """
    host = owned_dialog(texts, class_name="Internet Explorer_Hidden", width=900, height=700)

    state = inspect_credential_modal(111, lambda _pid: [main_window(), host])

    assert state.dialog is not None, "the AAD host must never collapse into the healthy state"
    assert state.dialog.verdict == verdict


def test_an_enabled_owner_is_the_only_thing_that_exonerates_a_dialog() -> None:
    """Modality is a ONE-WAY test, and it is the only suppression mechanism left.

    An ENABLED owner proves this window is blocking nothing, because a modal disables its owner. A
    DISABLED owner proves nothing either way - Power BI's refresh dialog disables the owner too - and
    ``None`` (no owner) means the test did not apply, which is not the same as passing it.
    """
    exonerated = owned_dialog(owner_enabled=True)
    disabled_owner = owned_dialog(owner_enabled=False)
    no_owner_test = DesktopWindow("", "Cls", 702, 355, (), hwnd=0x40004, owner_hwnd=MAIN_HWND, owner_enabled=None)

    assert _credential_modal.is_proven_non_blocking(exonerated)
    assert not _credential_modal.is_proven_non_blocking(disabled_owner)
    assert not _credential_modal.is_proven_non_blocking(no_owner_test)

    kept = _credential_modal.dialog_candidates([main_window(), exonerated, disabled_owner, no_owner_test])

    assert exonerated not in kept, "an enabled owner is positive proof this window blocks nothing"
    assert disabled_owner in kept
    assert no_owner_test in kept, "'the test did not apply' is not 'the test passed'"


def test_no_proxy_for_blocking_survives_in_the_candidate_rule() -> None:
    """Anti-regression: size, class prefix and a name allowlist each hid a real blocker. None returns.

    Three review rounds killed three correlates. This asserts the module has no surface for a fourth,
    and that the only exclusions left are the modality ones.

    ⚠️ **Asserted BEHAVIOURALLY, not by reading the source (#400 review round 5).** This used to grep
    ``inspect.getsource(dialog_candidates)`` for the tokens ``class_name`` and ``width``. That caught
    an inline mutation and nothing else: a helper called from that function could consult either
    without those tokens appearing in it, and the test would still pass. So instead the matrix below
    feeds windows that differ ONLY in class and size and requires candidacy to be identical - which no
    proxy anywhere behind :func:`dialog_candidates` can satisfy.
    """
    for gone in ("MIN_DIALOG_WIDTH", "MIN_DIALOG_HEIGHT", "HELPER_WINDOW_CLASSES", "is_desktop_main_window"):
        assert not hasattr(_credential_modal, gone), f"{gone} is a proxy for blocking and must stay deleted"

    classes = (
        DESKTOP_MAIN_CLASS_PREFIX + ".app.0.33c0d9d",  # the frame's own WinForms family (round 2)
        "Internet Explorer_Hidden",  # the AAD sign-in host's class (round 3)
        "#32770",
        "tooltips_class32",
    )
    # Every size is > 0 in both axes: `renders_nothing` is a MODALITY rule (unowned AND no pixels), not
    # a size test, so a genuinely zero-area unowned window is excluded on purpose and does not belong
    # in a matrix asserting that size is never consulted.
    sizes = ((2011, 1298), (900, 700), (80, 60), (1, 1))
    frame = main_window()

    verdicts: dict[tuple[bool, str, tuple[int, int]], bool] = {}
    for owned in (True, False):
        for class_name in classes:
            for width, height in sizes:
                window = DesktopWindow(
                    "",
                    class_name,
                    width,
                    height,
                    ("Enter your credentials",),
                    hwnd=0x80008,
                    owner_hwnd=MAIN_HWND if owned else 0,
                    owner_enabled=False if owned else None,
                )
                kept = _credential_modal.dialog_candidates([frame, window], frame=frame)
                verdicts[(owned, class_name, (width, height))] = window in kept

    assert all(verdicts.values()), (
        "candidacy must not vary with class or size; these combinations were dropped: "
        f"{sorted(key for key, kept in verdicts.items() if not kept)}"
    )


def test_the_frame_is_identified_by_ownership_before_any_convention() -> None:
    """`main_frame` names the frame from ownership evidence - and only when it is UNAMBIGUOUS.

    Ownership names the frame outright when a dialog is up, which is the case where getting it wrong
    removes a real blocker: the credential dialog's chain roots at the frame no matter where Z-order
    puts it in the enumeration.

    ⚠️ The decoy assertion INVERTED in round 5, and that is the fix rather than a regression. A second
    unowned window that renders pixels is itself a possible frame - a "decoy" and an unowned credential
    host are structurally the same window - so identity is AMBIGUOUS and nothing is excluded. Believing
    the ownership-derived root instead is exactly what crowned an unowned credential host that owned
    one tooltip.
    """
    frame = main_window()
    dialog = owned_dialog(("Enter your credentials",))
    decoy = DesktopWindow("decoy", "Cls", 300, 200, ("decoy",), hwnd=0x60006)

    # Ownership evidence wins even when the dialog is enumerated FIRST (Z-order puts a modal on top).
    assert _credential_modal.main_frame([dialog, frame]) is frame
    assert _credential_modal.main_frame([frame, dialog]) is frame
    # A second rendering unowned window is a second possible frame, so identity fails closed.
    assert _credential_modal.main_frame([dialog, decoy, frame]) is None
    assert _credential_modal.main_frame([decoy, frame, dialog]) is None
    # With nothing owned, one rendering unowned window is still unambiguous.
    lone = DesktopWindow("only", "Cls", 800, 600, ("only",), hwnd=0x50005)
    assert _credential_modal.main_frame([lone]) is lone
    assert _credential_modal.main_frame([]) is None


def test_an_unowned_window_that_renders_pixels_is_still_classified() -> None:
    """`renders_nothing` needs BOTH conjuncts: no owner AND no pixels. Neither alone suppresses.

    An unowned 900x700 window displays something to a human even though it disables nobody. Dropping
    the area conjunct would silently exclude every unowned window - including the AAD host in its
    unowned form - and would also empty ``main_frame``'s fallback candidate set.
    """
    frame = main_window()
    unowned_visible = DesktopWindow("", "Internet Explorer_Hidden", 900, 700, ("Password:",), hwnd=0x70007)

    assert not _credential_modal.renders_nothing(unowned_visible)

    kept = _credential_modal.dialog_candidates([frame, unowned_visible], frame=frame)
    assert unowned_visible in kept, "an unowned window with pixels must not vanish into healthy"

    state = inspect_credential_modal(111, lambda _pid: [frame, unowned_visible])

    assert state.dialog is not None
    assert state.dialog.verdict == "DIALOG_UNRECOGNIZED"


def test_ownership_is_followed_transitively_to_the_unowned_root() -> None:
    """Round 4: "first owner" is itself a proxy for "the root", and it hid a credential modal.

    An owned window can own another popup. With a Z-order of ``tooltip -> credential dialog -> frame``
    the first owner reached is the CREDENTIAL DIALOG, and whatever ``main_frame`` picks is excluded
    from the prepass AND from classification - so the modal disappeared. Measured on the round-3 build:
    the frame came back as the ``#32770`` credential dialog and no modal was reported.
    """
    frame_hwnd, cred_hwnd, tip_hwnd = 0xF001, 0xC002, 0x7003
    frame = DesktopWindow("sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("sample",), hwnd=frame_hwnd)
    cred = DesktopWindow(
        "", "#32770", 702, 355, ("Enter your credentials",), hwnd=cred_hwnd, owner_hwnd=frame_hwnd, owner_enabled=False
    )
    tip = DesktopWindow(
        "", "tooltips_class32", 120, 24, ("hint",), hwnd=tip_hwnd, owner_hwnd=cred_hwnd, owner_enabled=True
    )

    # Z-order puts the tooltip first, so a one-edge walk lands on the credential dialog.
    assert _credential_modal.main_frame([tip, cred, frame]) is frame
    state = inspect_credential_modal(111, lambda _pid: [tip, cred, frame])

    assert state.modal is not None, "a nested owner chain must not hide the credential dialog"
    assert state.modal.window is cred


def test_a_nested_chain_still_finds_the_modal_while_our_own_refresh_is_in_flight() -> None:
    """Round 4's third reproduction: the frame is busy with a benign refresh, so nothing else reports.

    With the frame misidentified this returned no modal, no dialog finding and no unknown state - the
    exact "unassessable input in the clean bucket" shape, at depth two.
    """
    frame_hwnd, cred_hwnd, tip_hwnd = 0xF001, 0xC002, 0x7003
    frame = DesktopWindow(
        "sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("sample", "Refresh", "Evaluating..."), hwnd=frame_hwnd
    )
    cred = DesktopWindow(
        "", "#32770", 702, 355, ("Enter your credentials",), hwnd=cred_hwnd, owner_hwnd=frame_hwnd, owner_enabled=False
    )
    tip = DesktopWindow(
        "", "tooltips_class32", 120, 24, ("hint",), hwnd=tip_hwnd, owner_hwnd=cred_hwnd, owner_enabled=True
    )

    state = inspect_credential_modal(111, lambda _pid: [tip, cred, frame], operation_in_flight=True)

    assert state.modal is not None
    assert state.modal.window is cred


def test_a_topmost_unowned_dialog_cannot_present_itself_as_the_application() -> None:
    """Round 4's second reproduction: the fallback must not crown the FIRST of several unowned windows.

    An unowned ``Internet Explorer_Hidden`` enumerated ahead of the real frame, reading
    ``Enter your credentials``, was selected as the frame and therefore skipped by the prepass. With no
    ownership evidence to settle it, identity is AMBIGUOUS and the rule fails closed: ``main_frame``
    returns ``None``, nothing is excluded, and the prompt is found.
    """
    frame = DesktopWindow("sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("sample",), hwnd=0xF001)
    aad = DesktopWindow("", "Internet Explorer_Hidden", 900, 700, ("Enter your credentials",), hwnd=0x9001)

    assert _credential_modal.main_frame([aad, frame]) is None, "ambiguous identity must fail closed"

    state = inspect_credential_modal(111, lambda _pid: [aad, frame])

    assert state.modal is not None, "a topmost unowned dialog must not skip the prepass"
    assert state.modal.window is aad


def test_an_unowned_credential_host_owning_a_tooltip_is_never_crowned_the_frame() -> None:
    """Round 5: collecting roots ONLY through ownership chains made a credential host the application.

    The reviewer's construction, and the fourth distinct topology to defeat frame identity. A real
    unowned frame shows ``Refresh`` / ``Evaluating...``; an unowned host shows
    ``Enter your credentials``; an ENABLED tooltip is owned by that host. The tooltip is the only owned
    window, so the only ownership-derived root is the CREDENTIAL HOST - which was then excluded from
    the prepass and from classification, while the real frame's progress text was suppressed in flight.

    Measured on the round-4 build, exactly the shape this module exists to prevent::

        main_frame selected: the credential host
        inspect_credential_modal -> modal=None dialog=None unknown_reason=None
                                    desktop_unready=None process_gone=None
    """
    frame_hwnd, cred_hwnd, tip_hwnd = 0xF001, 0x9001, 0x7003
    frame = DesktopWindow(
        "", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("Refresh", "Evaluating..."), hwnd=frame_hwnd
    )
    cred = DesktopWindow("", "Internet Explorer_Hidden", 900, 700, ("Enter your credentials",), hwnd=cred_hwnd)
    tip = DesktopWindow(
        "", "tooltips_class32", 120, 24, ("hint",), hwnd=tip_hwnd, owner_hwnd=cred_hwnd, owner_enabled=True
    )

    assert _credential_modal.main_frame([tip, cred, frame]) is None, "two rendering unowned windows are ambiguous"
    kept = _credential_modal.dialog_candidates([tip, cred, frame])
    assert cred in kept and frame in kept, "an ambiguous frame must exclude neither of them"

    for in_flight in (False, True):
        state = inspect_credential_modal(111, lambda _pid: [tip, cred, frame], operation_in_flight=in_flight)
        assert state.modal is not None, f"the prompt vanished with operation_in_flight={in_flight}"
        assert state.modal.window is cred


def test_an_unowned_host_with_unrecognised_text_still_surfaces_beside_our_own_refresh() -> None:
    """The same topology without a signature hit must be LOUD, not clean.

    Round 5's danger is not only the missed signature: with the credential host crowned as the frame,
    the only window left to classify was the real frame, whose benign progress text is suppressed while
    our own operation is in flight. Every unrecognised window in that shape therefore collapsed into
    the healthy state. This is the same construction with text nobody can account for.
    """
    frame_hwnd, host_hwnd, tip_hwnd = 0xF001, 0x9001, 0x7003
    frame = DesktopWindow(
        "", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("Refresh", "Evaluating..."), hwnd=frame_hwnd
    )
    host = DesktopWindow("", "Internet Explorer_Hidden", 900, 700, ("Something nobody enumerated",), hwnd=host_hwnd)
    tip = DesktopWindow(
        "", "tooltips_class32", 120, 24, ("hint",), hwnd=tip_hwnd, owner_hwnd=host_hwnd, owner_enabled=True
    )

    state = inspect_credential_modal(111, lambda _pid: [tip, host, frame], operation_in_flight=True)

    assert state.modal is None
    assert state.dialog is not None, "an unaccounted window must never collapse into the healthy state"
    assert state.dialog.verdict == "DIALOG_UNRECOGNIZED"
    assert state.dialog.window is host


def test_ambiguous_frame_identity_excludes_nothing() -> None:
    """Failing closed means EXCLUDING NOTHING - the cost is a loud exit 3, never a silent clear.

    Both halves matter, and the second is round 5's: ambiguity has to survive the PRESENCE of owned
    windows too. An ownership-derived root is not better evidence than a window sitting there rendering
    pixels - believing it was the whole defect - so a tooltip owned by either candidate must not
    promote its owner into the application.
    """
    frame = DesktopWindow("sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("sample",), hwnd=0xF001)
    other = DesktopWindow("second", "Cls", 800, 600, ("second",), hwnd=0x9001)

    assert _credential_modal.main_frame([frame, other]) is None
    kept = _credential_modal.dialog_candidates([frame, other])

    assert frame in kept and other in kept, "an ambiguous frame must not exclude a window"

    for owner_hwnd in (0xF001, 0x9001):
        tip = DesktopWindow("", "tooltips_class32", 120, 24, (), hwnd=0x7003, owner_hwnd=owner_hwnd, owner_enabled=True)
        windows = [tip, frame, other]
        assert _credential_modal.main_frame(windows) is None, f"an owned tooltip must not crown hwnd {owner_hwnd:#x}"
        kept = _credential_modal.dialog_candidates(windows)
        assert frame in kept and other in kept, "neither possible frame may be excluded"


def test_an_unresolvable_owner_chain_fails_closed() -> None:
    """An owner this enumeration never saw, or a cycle, leaves the root UNKNOWN - so guess nothing."""
    frame = DesktopWindow("sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, ("sample",), hwnd=0xF001)
    orphan = DesktopWindow("", "#32770", 702, 355, (), hwnd=0xC002, owner_hwnd=0xDEAD, owner_enabled=False)
    left = DesktopWindow("", "Cls", 400, 300, (), hwnd=0xA001, owner_hwnd=0xA002, owner_enabled=False)
    right = DesktopWindow("", "Cls", 400, 300, (), hwnd=0xA002, owner_hwnd=0xA001, owner_enabled=False)

    assert _credential_modal.main_frame([frame, orphan]) is None, "an unseen owner leaves the root unknown"
    assert _credential_modal.main_frame([left, right]) is None, "a cycle leaves the root unknown"


@pytest.mark.skipif(sys.platform != "win32", reason="creates real Win32 windows")
def test_the_win32_harvest_reads_real_ownership_and_owner_enabled_state() -> None:
    """The modality facts must come from Win32, not from a dataclass default (#400 review round 3).

    Every other test here feeds synthesised windows, so none of them can tell whether
    ``GetWindow(GW_OWNER)`` and ``IsWindowEnabled(owner)`` are actually wired into the harvest - a
    mutation that hard-codes ``owner_hwnd = 0`` or ``owner_enabled = True`` passes all of them. This
    builds the reviewer's own reproduction natively: a real ``WS_VISIBLE`` **owned 0x0** popup whose
    owner is disabled, which is a genuinely blocking shape, and asserts the harvest sees it and the
    candidate rule keeps it.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class _WndClass(ctypes.Structure):
        """Minimal ``WNDCLASSW``."""

        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc_type),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]  # fmt: skip
    # Signatures set explicitly for the same reason `_configure_user32` does it in production: a
    # default ctypes restype is a 32-bit `c_int`, so an untyped `GetModuleHandleW` TRUNCATES the
    # 64-bit HINSTANCE and `RegisterClassW` then faults on the garbage. That access violation showed
    # up as a `faulthandler` dump on every run of this test while it still reported a pass.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WndClass)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.DestroyWindow.argtypes = [wintypes.HWND]

    # `DefWindowProcW` is used DIRECTLY as the window procedure, cast to the callback type rather than
    # wrapped in a Python lambda. Windows calls a wndproc synchronously from inside `CreateWindowEx`
    # and `DestroyWindow`; keeping Python off the message path removes both the marshalling risk and
    # the need for a message pump.
    klass = _WndClass()
    klass.lpfnWndProc = ctypes.cast(user32.DefWindowProcW, wndproc_type)
    klass.hInstance = kernel32.GetModuleHandleW(None)
    klass.lpszClassName = f"T2PModalityProbe{os.getpid()}"
    assert user32.RegisterClassW(ctypes.byref(klass)), f"RegisterClassW failed: {ctypes.get_last_error()}"

    ws_overlappedwindow, ws_visible, ws_popup = 0x00CF0000, 0x10000000, 0x80000000
    frame_hwnd = user32.CreateWindowExW(
        0, klass.lpszClassName, "t2p frame", ws_overlappedwindow | ws_visible,
        10, 10, 400, 300, None, None, klass.hInstance, None,
    )  # fmt: skip
    owned_hwnd = user32.CreateWindowExW(
        0, klass.lpszClassName, "", ws_popup | ws_visible, 0, 0, 0, 0, frame_hwnd, None, klass.hInstance, None
    )
    try:
        assert frame_hwnd and owned_hwnd, "could not create the native probe windows"
        user32.EnableWindow(frame_hwnd, False)
        time.sleep(0.2)

        harvested, _visited = _enumerate_pid_windows_with_count(os.getpid())
        by_hwnd = {window.hwnd: window for window in harvested}
        frame = by_hwnd.get(frame_hwnd)
        owned = by_hwnd.get(owned_hwnd)

        assert frame is not None and owned is not None, "the harvest missed the native probe windows"
        assert frame.owner_hwnd == 0, "the frame is unowned"
        assert owned.owner_hwnd == frame_hwnd, "ownership must come from GetWindow(GW_OWNER)"
        assert owned.owner_enabled is False, "owner_enabled must come from IsWindowEnabled(owner)"
        assert (owned.width, owned.height) == (0, 0), "the probe window really is 0x0"
        assert not _credential_modal.renders_nothing(owned), "it HAS an owner, so zero area cannot suppress"
        assert not _credential_modal.is_proven_non_blocking(owned), "a disabled owner is not an exoneration"
        assert owned in _credential_modal.dialog_candidates(harvested, frame=frame)
    finally:
        user32.DestroyWindow(owned_hwnd)
        user32.DestroyWindow(frame_hwnd)
        user32.UnregisterClassW(klass.lpszClassName, klass.hInstance)


class _NativeWindowProbe:
    """A disposable set of REAL Win32 windows, for the topology tests that synthesised data cannot gate.

    Every signature is set explicitly, for the reason measured in round 3: a default ctypes ``restype``
    is a 32-bit ``c_int``, so an untyped ``GetModuleHandleW`` TRUNCATES the 64-bit ``HINSTANCE`` and
    ``RegisterClassW`` then faults on the garbage - an access violation that showed up as a
    ``faulthandler`` dump while the test still reported a pass.

    ``DefWindowProcW`` is used DIRECTLY as the window procedure, cast to the callback type rather than
    wrapped in a Python callable: Windows calls a wndproc synchronously from inside ``CreateWindowExW``
    and ``DestroyWindow``, so keeping Python off the message path removes both the marshalling risk and
    the need for a message pump.

    Nothing Windows-only runs at import time. ``ctypes.WINFUNCTYPE`` does not EXIST off Windows, and
    this module is imported (and mostly run) on the ubuntu CI leg as well, so the callback type and the
    ``WNDCLASSW`` structure are both built inside ``__init__``, behind the callers' ``skipif``.
    """

    WS_OVERLAPPEDWINDOW, WS_VISIBLE, WS_POPUP, WS_CHILD = 0x00CF0000, 0x10000000, 0x80000000, 0x40000000
    HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = -1, 0x0002, 0x0001, 0x0010

    def __init__(self, tag: str) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )

        class WndClass(ctypes.Structure):
            """Minimal ``WNDCLASSW``."""

            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", self.wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        self.wndclass_type = WndClass
        self._configure()
        self.klass = WndClass()
        self.klass.lpfnWndProc = ctypes.cast(self.user32.DefWindowProcW, self.wndproc_type)
        self.klass.hInstance = self.kernel32.GetModuleHandleW(None)
        self.klass.lpszClassName = f"T2P{tag}{os.getpid()}"
        assert self.user32.RegisterClassW(ctypes.byref(self.klass)), f"RegisterClassW: {ctypes.get_last_error()}"
        self.created: list[int] = []

    def _configure(self) -> None:
        """Type every call this probe makes."""
        self.user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.DefWindowProcW.restype = ctypes.c_longlong
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]  # fmt: skip
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(self.wndclass_type)]
        self.user32.RegisterClassW.restype = wintypes.ATOM
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user32.UnregisterClassW.restype = wintypes.BOOL
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL

    def top(self, style: int, x: int, y: int, width: int, height: int, owner: int | None = None) -> int:
        """Create a visible top-level window, optionally OWNED by ``owner``."""
        hwnd = self.user32.CreateWindowExW(
            0, self.klass.lpszClassName, "", style | self.WS_VISIBLE, x, y, width, height,
            owner, None, self.klass.hInstance, None,
        )  # fmt: skip
        assert hwnd, f"CreateWindowExW: {ctypes.get_last_error()}"
        self.created.append(hwnd)
        return hwnd

    def label(self, parent: int, text: str, y: int) -> int:
        """Create a real STATIC child, so the harvest reads ``text`` as CONTENT rather than a caption."""
        hwnd = self.user32.CreateWindowExW(
            0, "STATIC", text, self.WS_CHILD | self.WS_VISIBLE, 5, y, 260, 20,
            parent, None, self.klass.hInstance, None,
        )  # fmt: skip
        assert hwnd, f"CreateWindowExW(STATIC): {ctypes.get_last_error()}"
        return hwnd

    def raise_topmost(self, hwnd: int) -> None:
        """Put ``hwnd`` at the top of the TOPMOST band, which is where EnumWindows starts."""
        flags = self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE
        assert self.user32.SetWindowPos(hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, flags), (
            f"SetWindowPos: {ctypes.get_last_error()}"
        )
        time.sleep(0.2)

    def close(self) -> None:
        """Destroy every window this probe created, then unregister its class."""
        for hwnd in reversed(self.created):
            self.user32.DestroyWindow(hwnd)
        self.created.clear()
        self.user32.UnregisterClassW(self.klass.lpszClassName, self.klass.hInstance)


def _dotnet_main_window_handle(pid: int) -> int:
    """.NET ``System.Diagnostics.Process.MainWindowHandle`` for ``pid``, read from ANOTHER process.

    Deliberately out-of-process: reading it in-process through pythonnet would add a dependency this
    detector does not have, and the question being asked is what the AUTHORITY says, not what a
    convenient reimplementation of it says.
    """
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-Command", f"[int64](Get-Process -Id {pid}).MainWindowHandle"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Get-Process failed: {proc.stderr.strip()}"
    return int((proc.stdout or "0").strip() or 0)


@pytest.mark.skipif(sys.platform != "win32", reason="creates real Win32 windows")
def test_a_native_unowned_credential_host_owning_a_tooltip_is_not_the_application() -> None:
    """Round 5's reproduction, built natively - the fourth topology to defeat frame identity.

    The synthesised twin of this test proves the RULE; this one proves the rule is applied to what
    Win32 actually reports. Three real windows: an unowned frame whose only content is progress status,
    an unowned host reading ``Enter your credentials``, and an ENABLED tooltip owned by that host. The
    tooltip is the only owned window, so ownership alone yields exactly one root - the credential host -
    and believing it returned ``modal=None dialog=None unknown_reason=None`` while our own refresh was
    in flight.
    """
    probe = _NativeWindowProbe("Round5Probe")
    try:
        frame_hwnd = probe.top(probe.WS_OVERLAPPEDWINDOW, 10, 10, 400, 300)
        probe.label(frame_hwnd, "Refresh", 10)
        probe.label(frame_hwnd, "Evaluating...", 40)
        cred_hwnd = probe.top(probe.WS_POPUP, 500, 10, 900, 700)
        probe.label(cred_hwnd, "Enter your credentials", 10)
        tip_hwnd = probe.top(probe.WS_POPUP, 520, 40, 120, 24, owner=cred_hwnd)
        time.sleep(0.2)

        harvested, _visited = _enumerate_pid_windows_with_count(os.getpid())
        ours = [window for window in harvested if window.hwnd in {frame_hwnd, cred_hwnd, tip_hwnd}]
        by_hwnd = {window.hwnd: window for window in ours}

        assert set(by_hwnd) == {frame_hwnd, cred_hwnd, tip_hwnd}, "the harvest missed a native probe window"
        assert by_hwnd[frame_hwnd].owner_hwnd == 0, "the real frame is unowned"
        assert by_hwnd[cred_hwnd].owner_hwnd == 0, "the credential host is unowned - that is the whole shape"
        assert by_hwnd[tip_hwnd].owner_hwnd == cred_hwnd, "the tooltip is owned BY the credential host"
        assert by_hwnd[tip_hwnd].owner_enabled is True, "an enabled owner exonerates the tooltip itself"
        assert by_hwnd[frame_hwnd].texts == ("Refresh", "Evaluating..."), "the frame reads as pure progress status"

        assert _credential_modal.main_frame(ours) is None, "two rendering unowned windows cannot name one frame"
        kept = _credential_modal.dialog_candidates(ours)
        assert by_hwnd[cred_hwnd] in kept and by_hwnd[frame_hwnd] in kept, "ambiguity must exclude neither"

        state = inspect_credential_modal(os.getpid(), lambda _pid: ours, operation_in_flight=True)

        assert state.modal is not None, "the credential prompt vanished from a real three-window harvest"
        assert state.modal.window.hwnd == cred_hwnd
    finally:
        probe.close()


@pytest.mark.skipif(sys.platform != "win32", reason="creates real Win32 windows and shells out to PowerShell")
def test_the_process_main_window_handle_is_a_z_order_answer_not_an_identity() -> None:
    """MEASURED, because "ask the authority" was the obvious fix and it does not work (#400 round 5).

    .NET's ``Process.MainWindowHandle`` looks like independent evidence about which window is the
    application. It is not: ``ProcessManager.MainWindowFinder`` runs ``EnumWindows`` and stops at the
    first VISIBLE, UNOWNED window of the pid - :func:`main_frame`'s own fallback convention, evaluated
    in another process, minus the :func:`renders_nothing` guard. This test pins that with the two
    experiments that decide it:

    * **it follows Z-order.** The same two windows are raised in turn to ``HWND_TOPMOST``, and the
      answer follows whichever was raised last. An identity does not change when a human clicks a
      window;
    * **it will name a window that renders nothing.** An unowned **0x0** window created last is
      returned as the "main window" - a window that cannot show a human anything, and one this
      module's own :func:`renders_nothing` refuses as a possible frame.

    Measured across six runs of the round-5 topology while writing this: the authority named the
    CREDENTIAL HOST in five of them and the real frame in one - so it is not merely wrong, it is
    unstable. That is why nothing in this module consults it, as primary evidence or as a tie-breaker.
    """
    probe = _NativeWindowProbe("AuthorityProbe")
    try:
        frame_hwnd = probe.top(probe.WS_OVERLAPPEDWINDOW, 10, 10, 400, 300)
        cred_hwnd = probe.top(probe.WS_POPUP, 500, 10, 900, 700)
        time.sleep(0.2)

        probe.raise_topmost(frame_hwnd)
        frame_on_top = _dotnet_main_window_handle(os.getpid())
        probe.raise_topmost(cred_hwnd)
        cred_on_top = _dotnet_main_window_handle(os.getpid())

        assert {frame_on_top, cred_on_top} <= {frame_hwnd, cred_hwnd}, (
            f"expected the authority to name one of our two unowned windows, got {frame_on_top}/{cred_on_top}"
        )
        assert frame_on_top != cred_on_top, (
            "the same two windows produced one answer, so this run cannot show Z-order dependence; "
            f"both reads returned {frame_on_top}"
        )
        assert frame_on_top == frame_hwnd and cred_on_top == cred_hwnd, (
            "the authority is expected to name whichever window was raised last"
        )

        ghost_hwnd = probe.top(probe.WS_POPUP, 0, 0, 0, 0)
        probe.raise_topmost(ghost_hwnd)
        harvested, _visited = _enumerate_pid_windows_with_count(os.getpid())
        ghost = next(window for window in harvested if window.hwnd == ghost_hwnd)

        assert (ghost.width, ghost.height) == (0, 0), "the ghost really is 0x0"
        assert _credential_modal.renders_nothing(ghost), "this module refuses a 0x0 unowned window as a frame"
        assert _dotnet_main_window_handle(os.getpid()) == ghost_hwnd, (
            "the authority names a window that can show a human nothing"
        )

        # The load-bearing consequence: whatever the authority says, identity here stays ambiguous.
        ours = [window for window in harvested if window.hwnd in {frame_hwnd, cred_hwnd}]
        assert _credential_modal.main_frame(ours) is None
    finally:
        probe.close()


@pytest.mark.parametrize(
    "report_name",
    ["Account Key", "Personal Access Token", "Databricks Client Credentials"],
)
def test_a_report_title_cannot_fabricate_a_credential_prompt(report_name: str, capsys) -> None:
    """Finding 4 (MEDIUM): the all-window prepass read the Desktop MAIN window too.

    Measured on the PR-#400 build with a production-shaped main window titled
    ``Account Key - Power BI Desktop``: both consumers emitted the exit-1 hard stop. A report is
    allowed to be called that. Only the identified main window is excluded - every real dialog is
    still scanned at every size and in every class, so an unusual modal class stays detectable.
    """
    title = f"{report_name} - Power BI Desktop"
    main = DesktopWindow(title, "WindowsForms10.Window.8.app.0.x", 2011, 1298, (title,))

    state = inspect_credential_modal(111, lambda _pid: [main])
    verdict = probe_desktop_query._credential_verdict(111, state)

    assert state.modal is None, f"a report named {report_name!r} is not a credential prompt"
    assert state.dialog is None
    assert verdict is None, "the probe must continue"
    assert capsys.readouterr().out == ""


def test_an_unusual_modal_class_is_still_scanned_for_the_credential_signature() -> None:
    """The other half of finding 4: excluding the frame must not narrow real dialog coverage."""
    odd = owned_dialog(("Enter your credentials",), class_name="HwndWrapper[PBIDesktop.exe;;guid]", width=30, height=20)

    state = inspect_credential_modal(111, lambda _pid: [main_window(), odd])

    assert state.modal is not None
    assert state.modal.window.class_name.startswith("HwndWrapper")


def test_the_credential_prepass_reads_windows_that_classification_skips() -> None:
    """An exoneration says "not blocking" - it does NOT say "carries no credential text".

    ``is_proven_non_blocking`` removes a window from CLASSIFICATION, because an enabled owner proves it
    is blocking nobody. The credential prepass still reads it: recall on the hard-stop path is the one
    thing this module never trades away, and a sign-in prompt whose owner happens to be enabled is
    still a sign-in prompt a human has to deal with.
    """
    exonerated = owned_dialog(("Enter your credentials",), owner_enabled=True)
    frame = main_window()

    assert exonerated not in _credential_modal.dialog_candidates([frame, exonerated]), "not classified"

    state = inspect_credential_modal(111, lambda _pid: [frame, exonerated])

    assert state.modal is not None, "an exonerated window is still scanned for the credential signature"
    assert state.modal.window is exonerated


@pytest.mark.parametrize("progress_enabled", [False, True])
def test_a_dialog_we_could_not_read_does_not_abort_a_refresh_that_completes(monkeypatch, progress_enabled) -> None:
    """Latch, do not raise: an unreadable dialog must not kill a refresh that was going to succeed.

    Both wait branches have to behave this way, and the progress-monitor branch did not - it called the
    t=0 raise helper, so any mid-flight finding ended the run on its first poll (#400 review, finding
    1). The behavioural statement is the assertion: the worker finishes, so the refresh succeeds.
    """
    windows = visible_unreadable_dialog_windows()
    calls = {"n": 0}
    finished = threading.Event()

    def state(pid: int, **kwargs) -> CredentialDetection:
        calls["n"] += 1
        if calls["n"] == 1:
            return CredentialDetection()
        return inspect_credential_modal(pid, lambda _pid: windows, operation_in_flight=bool(kwargs.get("in_flight")))

    class _SlowThenDone:
        """A connection whose command returns after a couple of polls."""

        CommandText = ""
        CommandTimeout = 0

        def Open(self) -> None:
            """Match the ADOMD API surface."""

        def CreateCommand(self):
            """This object is its own command."""
            return self

        def ExecuteNonQuery(self) -> None:
            """Return once the poll loop has had a chance to observe the dialog."""
            time.sleep(0.15)
            finished.set()

        def Close(self) -> None:
            """Match the ADOMD API surface."""

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", state)
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: _SlowThenDone())
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.02)
    monkeypatch.setattr(refresh_pbip_model, "_start_refresh_progress_trace", lambda *_a, **_k: _FakeProgressMonitor())

    ok, _message = refresh(
        port=1234,
        tables=["Orders"],
        timeout_sec=30,
        desktop_pid=111,
        progress_enabled=progress_enabled,
        absolute_timeout_sec=30.0,
    )

    assert finished.is_set(), "the worker never ran, so this proves nothing about aborting it"
    assert ok is True, "an unreadable dialog aborted a refresh that completed"
    assert calls["n"] >= 2, "the poll never observed the dialog, so the latch was not exercised"


@pytest.mark.parametrize("progress_enabled", [False, True])
def test_a_latched_dialog_surfaces_at_the_deadline_in_both_wait_branches(monkeypatch, progress_enabled) -> None:
    """The other half of the latch: what is latched must be RAISED, not quietly dropped.

    The dialog here appears mid-refresh and is GONE again before the deadline - the #154 shape. A
    fresh end-of-run check cannot see it, so only a real latch can report it; without one the run ends
    on a bare ``TimeoutError``, which the parent classifier blames on a slow source.

    Keeping the dialog on screen throughout is what made the missing latch invisible: ``refresh()``'s
    own final check re-observed it and raised anyway. A defence-in-depth path masking a missing latch
    is exactly the shape that makes a test pass for the wrong reason.
    """
    released = threading.Event()
    calls = {"n": 0}
    healthy = [DesktopWindow("Report", "WindowsForms10.Window.8.app.0.1a2b3c", 2011, 1298, ("Report",))]

    def state(pid: int, **kwargs) -> CredentialDetection:
        calls["n"] += 1
        # 1 = the t=0 pre-check, 2-3 = the dialog is up, 4+ = it has gone again.
        windows = visible_unreadable_dialog_windows() if calls["n"] in (2, 3) else healthy
        return inspect_credential_modal(pid, lambda _pid: windows, operation_in_flight=bool(kwargs.get("in_flight")))

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", state)
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: _ParkedAdomd(released))
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.02)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_WALL_CLOCK_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(refresh_pbip_model, "_start_refresh_progress_trace", lambda *_a, **_k: _FakeProgressMonitor())

    try:
        with pytest.raises(DialogFoundError) as excinfo:
            refresh(
                port=1234,
                tables=["Orders"],
                timeout_sec=1,
                desktop_pid=111,
                progress_enabled=progress_enabled,
                absolute_timeout_sec=1.0,
            )
    finally:
        released.set()

    assert excinfo.value.finding.verdict == "DIALOG_UNREADABLE"
    assert calls["n"] >= 5, "the dialog must have been gone for several polls before the deadline"


@pytest.mark.parametrize("progress_enabled", [False, True])
def test_the_production_refresh_path_polls_with_the_in_flight_detector(monkeypatch, progress_enabled: bool) -> None:
    """Finding 1 (HIGH): production overrode the very default the other test was checking.

    ``_join_refresh_worker`` passed ``detector=_credential_state`` (the t=0 form) in the no-trace
    branch and called the t=0 ``_raise_if_blocked`` in the progress-monitor branch, so Power BI's own
    progress dialog stopped the refresh it belonged to. Measured on the PR-#400 build, BOTH branches:
    ``DialogFoundError(REFRESH_IN_PROGRESS)`` on the first poll, with the detector receiving no
    ``in_flight`` argument at all.

    This drives the REAL ``refresh()`` through both branches and asserts on production's CALL SITE -
    the argument the detector actually receives - not on a helper's default. A mutation that reverts
    the call site fails here; a mutation that only changes the helper's signature is caught by
    ``test_poll_loop_default_detector_is_the_in_flight_one``. Both are needed.
    """
    seen: list[object] = []
    calls = {"n": 0}
    windows = visible_progress_dialog_windows()
    released = threading.Event()

    def state(pid: int, **kwargs) -> CredentialDetection:
        calls["n"] += 1
        seen.append(kwargs.get("in_flight", "<not passed>"))
        if calls["n"] == 1:
            return CredentialDetection()
        return inspect_credential_modal(pid, lambda _pid: windows, operation_in_flight=bool(kwargs.get("in_flight")))

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", state)
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: _ParkedAdomd(released))
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.02)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_WALL_CLOCK_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(refresh_pbip_model, "_start_refresh_progress_trace", lambda *_a, **_k: _FakeProgressMonitor())

    try:
        with pytest.raises(TimeoutError):
            refresh(
                port=1234,
                tables=["Orders"],
                timeout_sec=1,
                desktop_pid=111,
                progress_enabled=progress_enabled,
                absolute_timeout_sec=1.0,
            )
    finally:
        released.set()

    polls = seen[1:]
    assert polls, "the refresh never polled, so this test proved nothing"
    assert all(flag is True for flag in polls), (
        f"production must poll with in_flight=True after t=0; detector saw {polls}"
    )
    assert seen[0] is False or seen[0] == "<not passed>", "the t=0 check must NOT be in-flight"


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
CHROME_SIGNATURE = PROBE_PS1.parent / "benign_chrome_signature.regex"
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
    # Every element here is ACCOUNTED FOR: `1,204 rows loaded` is recognised progress status and
    # `Cancel` is enumerated chrome. It used to carry a bare `Orders` too, which issue #406 turned into
    # a veto - see REFRESH_PROGRESS_WITH_TABLE_NAMES, which pins that reversal as its own named cost
    # rather than letting it silently rewrite what these precedence tests are about.
    Texts=["Refresh", "1,204 rows loaded", "Cancel"],
    InteractiveTexts=["Cancel"],
    # A Power BI refresh dialog DOES disable its owner, so modality alone cannot tell it from a
    # credential modal. Pinned false on purpose: the fix must not lean on the owner test.
    OwnerEnabled=False,
)
# The same dialog with a bare table name in it - the shape the removed length amnesty used to excuse.
REFRESH_PROGRESS_WITH_TABLE_NAMES = _window(
    Title="Refresh",
    ClassName="HwndWrapper[PBIDesktop.exe;;refresh]",
    Texts=["Refresh", "Orders", "1,204 rows loaded", "Cancel"],
    InteractiveTexts=["Cancel"],
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


@pytest.mark.serial
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


@pytest.mark.serial
def test_credential_text_reachable_only_through_textpattern_is_a_hard_stop(tmp_path: Path) -> None:
    """Exploit 1. `Name` + `ValuePattern` alone miss a read-only RichTextBox's content entirely."""
    done = _run_probe_against_wpf_modal(tmp_path, _MODAL_TEXTPATTERN_ONLY)

    assert "VERDICT: CREDENTIAL_MISSING" in done.stdout, done.stdout
    assert done.returncode == 1


@pytest.mark.serial
def test_credential_text_beyond_the_element_cap_never_reads_as_clean(tmp_path: Path) -> None:
    """Exploit 2. Truncation must not be indistinguishable from a complete, clean read.

    Two acceptable outcomes and one forbidden one: read it all and convict (exit 1), or report the
    harvest incomplete (exit 3). Never exit 0.
    """
    done = _run_probe_against_wpf_modal(tmp_path, _MODAL_PAST_THE_ELEMENT_CAP, ["-HarvestMaxElements", "400"])

    assert done.returncode != 0, f"a truncated harvest must never clear a window:\n{done.stdout}"
    assert "CREDENTIAL_PRESENT" not in done.stdout


@pytest.mark.serial
def test_credential_text_beyond_the_element_cap_convicts_when_the_cap_allows_it(tmp_path: Path) -> None:
    """The same window with the shipped cap: read in full, and convicted."""
    done = _run_probe_against_wpf_modal(tmp_path, _MODAL_PAST_THE_ELEMENT_CAP)

    assert "VERDICT: CREDENTIAL_MISSING" in done.stdout, done.stdout
    assert done.returncode == 1


@pytest.mark.serial
def test_a_signature_split_by_an_interposed_button_is_a_hard_stop(tmp_path: Path) -> None:
    """Exploit 3. The prose join must skip interactive elements, or `Cancel` breaks the sentence."""
    done = _run_probe_against_wpf_modal(tmp_path, _MODAL_INTERPOSED_SPLIT)

    assert "VERDICT: CREDENTIAL_MISSING" in done.stdout, done.stdout
    assert done.returncode == 1


@pytest.mark.serial
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

    A window is a progress dialog only when its content is ACCOUNTED FOR: recognised status text, or an
    element of the enumerated chrome allowlist. A sentence nobody can explain sitting beside
    `Evaluating` means the progress dialog does not explain this window.
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


def test_short_prompts_beside_progress_text_are_never_suppressed_by_the_arbiter(tmp_path: Path) -> None:
    """Issue #406, AC1+AC2. The arbiter's half of the length-amnesty hole PR #400 closed in Python.

    Measured on the pre-fix build through this same harness (``$MinPromptWords = 5``)::

        Refresh + Evaluating + "Please enter your password"  -> benign / REFRESH_IN_PROGRESS
                                                             -> in flight: verdict None, exit 0
        Refresh + Evaluating + "Password:"                   -> identical
        Refresh + Evaluating + "Please enter your password now"  (5 words) -> DIALOG_UNRECOGNIZED

    Only the word count separated row 1 from row 3, and none of these strings matches
    ``credential_modal_signature.regex`` - so ``Test-CredentialModal`` did not rescue them either. A
    real prompt was swallowed in silence, on the code path issue #367 added to stop exactly that.

    Both columns are asserted deliberately. The t=0 column alone cannot see the defect that mattered:
    at t=0 ``benign`` still reports ``REFRESH_IN_PROGRESS`` at exit 3, which looks loud enough. It is
    ``-RefreshInFlight`` that turns ``benign`` into NOTHING AT ALL, and that is where the prompt
    vanished.
    """
    for prompt in ["Please enter your password", "Password:", "Sign in", "Enter password"]:
        window = _window(Title="Refresh", Texts=["Refresh", "Evaluating", prompt], OwnerEnabled=False)

        at_t0 = classify(tmp_path, [window])
        in_flight = classify(tmp_path, [window], refresh_in_flight=True)

        assert at_t0["kind"] == "mixed-content", f"{prompt!r} is accounted for by nothing and must veto"
        assert at_t0["verdict"] == "DIALOG_UNRECOGNIZED"
        assert at_t0["evidence"] == prompt, "the verdict must carry the element it could not explain"
        assert in_flight["verdict"] == "DIALOG_UNRECOGNIZED", (
            f"{prompt!r} was SUPPRESSED in flight - a prompt swallowed in silence"
        )
        assert in_flight["exit_code"] == 3


def test_only_enumerated_chrome_is_excused_by_the_arbiter(tmp_path: Path) -> None:
    """Issue #406, the positive half: dismissal needs a POSITIVE claim, not a short string.

    Without this the fix could degenerate into "never suppress", which would make the arbiter answer
    exit 3 for every model and destroy the only reason it exists. ``Cancel``/``OK``/``Close`` are
    whole-element, anchored control labels that carry no prompt, so a window whose only unexplained
    content is one of them still shows a human nothing to act on. A table name gets no such excuse - it
    is short, but shortness was never the property that mattered.
    """
    chrome = _window(
        Title="Refresh",
        Texts=["Refresh", "Evaluating", "Cancel", "OK", "Close"],
        InteractiveTexts=["Cancel", "OK", "Close"],
        OwnerEnabled=False,
    )
    table_name = _window(Title="Refresh", Texts=["Refresh", "Evaluating", "Orders"], OwnerEnabled=False)

    assert classify(tmp_path, [chrome])["kind"] == "benign", "the benign path must stay reachable"
    assert classify(tmp_path, [chrome], refresh_in_flight=True)["verdict"] is None
    assert classify(tmp_path, [table_name])["kind"] == "mixed-content"
    assert classify(tmp_path, [table_name])["verdict"] == "DIALOG_UNRECOGNIZED"


def test_a_table_name_beside_progress_text_now_vetoes_suppression(tmp_path: Path) -> None:
    """Issue #406, AC3. THE REVERSED DECISION, pinned as a cost rather than deleted quietly.

    This test replaces ``test_short_data_labels_beside_progress_text_do_not_block_suppression``, which
    asserted the OPPOSITE: that ``Orders``/``Customers`` beside ``Evaluating`` still suppressed, so that
    ``CREDENTIAL_PRESENT`` stayed reachable while Desktop showed its own refresh dialog. That decision
    was reviewed and deliberate, and removing it is the substance of #406 rather than a side effect.

    Why reversing it is right, in order of weight:

    * **The capability it protected is secondary and already untrusted.** ``SKILL.md`` and
      ``docs/data-source-credentials.md`` both say the one-row data probe (``probe_desktop_query.py``)
      is the gate of record and that ``CREDENTIAL_PRESENT`` must not be trusted alone against a
      serverless source - it returned a false ``CREDENTIAL_PRESENT`` three times in the field.
    * **The costs are asymmetric.** Losing it costs an extra exit 3: loud, recoverable, a human looks at
      the screen. Keeping the amnesty cost a silently suppressed password prompt, in direct breach of
      the standing rule that a credential modal is never worked around.
    * **The capability rests on an INFERENCE; the defect was MEASURED.** No Desktop in this corpus has
      ever confirmed that Power BI's refresh dialog exposes bare table names -
      ``benign_dialog_signature.regex`` says its own provenance is inferred, and ``SKILL.md`` records
      the same gap. ``Password:`` being suppressed was measured through this harness. An inferred
      capability does not outrank a measured hole.

    The one thing that must NOT happen is the benign path becoming unreachable altogether; that is
    ``test_only_enumerated_chrome_is_excused_by_the_arbiter``, and it is why this cost is bounded.
    """
    at_t0 = classify(tmp_path, [REFRESH_PROGRESS_WITH_TABLE_NAMES])
    in_flight = classify(tmp_path, [REFRESH_PROGRESS_WITH_TABLE_NAMES], refresh_in_flight=True)

    assert at_t0["kind"] == "mixed-content"
    assert at_t0["verdict"] == "DIALOG_UNRECOGNIZED"
    assert at_t0["evidence"] == "Orders"
    assert in_flight["verdict"] == "DIALOG_UNRECOGNIZED", "the reachability cost is exit 3, and it is intended"
    assert in_flight["exit_code"] == 3


def test_the_arbiter_and_the_python_detector_share_one_vocabulary(tmp_path: Path) -> None:
    """The structural fix for #406's real cause: two detectors answering one question, unchecked.

    Issue #376 was fixed in ``_credential_modal.py`` and the arbiter kept the hole for a week, because
    nothing compared them. The chosen seam is SHARED RESOURCES, PORTED CONTROL FLOW - both read the
    same four ``*_signature.regex`` files, and this test fails if either stops doing so or if their
    verdicts diverge on the corpus that matters.

    It is deliberately NOT a whole-classifier equivalence test: the two are documented as divergent
    (the arbiter has a prose join, a ``benign-unverified`` kind and ``HarvestComplete``; the Python
    detector has none of those and has strictly less evidence). Only the benign/chrome decision - the
    one that authorises a dismissal - has to agree.
    """
    assert CHROME_SIGNATURE.read_text(encoding="utf-8").strip(), "the arbiter's chrome allowlist must exist"
    assert _credential_modal.BENIGN_CHROME_SIGNATURE_PATH == CHROME_SIGNATURE, (
        "the Python detector must read the arbiter's own chrome allowlist, not a copy"
    )
    assert _credential_modal.BENIGN_SIGNATURE_PATH == BENIGN_SIGNATURE
    assert _credential_modal.SIGNATURE_PATH == CREDENTIAL_SIGNATURE
    assert _credential_modal.BLOCKING_SIGNATURE_PATH == BLOCKING_SIGNATURE

    corpus = [
        ["Refresh", "Evaluating", "Password:"],
        ["Refresh", "Evaluating", "Please enter your password"],
        ["Refresh", "Evaluating", "Orders"],
        ["Refresh", "Evaluating", "Cancel"],
        ["Refresh", "Evaluating", NATIVE_QUERY_PROMPT],
        ["Refresh", "1,204 rows loaded", "Cancel"],
    ]
    for texts in corpus:
        arbiter = classify(tmp_path, [_window(Title="Refresh", Texts=texts, OwnerEnabled=False)])
        detector = classify_dialog(DesktopWindow("Refresh", "Cls", 702, 355, tuple(texts)))

        assert arbiter["kind"] == detector.kind, f"the two detectors disagree on {texts!r}"
        assert arbiter["verdict"] == detector.verdict


def test_the_arbiter_has_no_length_amnesty_left_on_disk() -> None:
    """A resource-level gate: the amnesty must be GONE, not lowered by one word.

    #406's acceptance says the fix must not be another threshold, and a behavioural test cannot tell a
    deleted rule from one whose constant moved - every prompt shorter than the new number would still
    pass. So this greps the shipped script for the MECHANISM rather than the number: a statement-level
    word-count constant, and the ``-split '\\s+'`` that counted words.

    Anchored at statement level on purpose. A first draft matched the bare string and failed on the
    script's own comment explaining the removed constant - a gate that cannot tell code from prose
    would be silenced by rewording the comment.
    """
    source = PROBE_PS1.read_text(encoding="utf-8")

    assert not re.search(r"(?m)^\s*\$\w*(?:Words|Length|MinPrompt)\w*\s*=", source), (
        "a length amnesty is back at statement level - it must be deleted, not tuned"
    )
    assert "-split '\\s+'" not in source, "word-splitting is the amnesty's mechanism; nothing here needs it"
    assert "benign_chrome_signature.regex" in source, "the arbiter must read the shared chrome allowlist"


def test_the_chrome_allowlist_stays_an_enumeration_not_a_catch_all() -> None:
    """The one file that can now excuse an unexplained element - so it has to stay tiny and anchored.

    Every alternative added here is a string that can never again veto a dismissal.
    """
    chrome = re.compile(CHROME_SIGNATURE.read_text(encoding="utf-8").strip(), re.IGNORECASE)

    for label in ["Cancel", "OK", "Close", "&Cancel"]:
        assert chrome.search(label), f"a known chrome label must still be excused: {label!r}"
    for prompt in [
        "Password:",
        "Please enter your password",
        "Orders",
        "Sign in",
        "Cancel the sign-in",
        "OK to sign in",
    ]:
        assert not chrome.search(prompt), f"the chrome allowlist must not excuse: {prompt!r}"


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


@pytest.mark.serial
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
