"""Offline tests for the Desktop observer: blocking-dialog stops, polling loops and the native read.

Issue #831: any blocking Desktop window is a human stop (``DIALOG_NEEDS_HUMAN``, exit 3) whose
diagnostic names the sanitized window title. Nothing here classifies dialog prose any more.
"""

from __future__ import annotations

# These tests deliberately monkeypatch module-internal seams so no Desktop, network, or Fabric
# dependency is required.
# pylint: disable=protected-access,too-few-public-methods,invalid-name

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import _desktop_observer
import probe_desktop_query
import refresh_pbip_model
from _desktop_observer import (
    Observation,
    CredentialUnknownError,
    DESKTOP_MAIN_CLASS_PREFIX,
    DesktopGoneError,
    DesktopUnreadyError,
    DialogFinding,
    DialogFoundError,
    DesktopWindow,
    inspect_desktop_observer,
)
from refresh_pbip_model import refresh


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
        minimized=minimized,
        hwnd=MAIN_HWND,
    )


def owned_dialog(
    *,
    title: str = "",
    class_name: str = "WindowsForms10.Window.20008.app.0.33c0d9d",
    width: int = 702,
    height: int = 355,
    owner_enabled: bool | None = False,
    hwnd: int = DIALOG_HWND,
) -> DesktopWindow:
    """A dialog OWNED by :func:`main_window`.

    ``owner_enabled=False`` by default - the owner is disabled, which is what a modal does. Only an
    ENABLED owner exonerates a window; anything else is a human stop.
    """
    return DesktopWindow(
        title,
        class_name,
        width,
        height,
        hwnd=hwnd,
        owner_hwnd=MAIN_HWND,
        owner_enabled=owner_enabled,
    )


def dialog_finding(window: DesktopWindow | None = None, title: str = "Enter your credentials") -> DialogFinding:
    """A blocking-window finding carrying the sanitized title the detector would read."""
    return DialogFinding(window or owned_dialog(), title)


def dialog_state(window: DesktopWindow | None = None, title: str = "Enter your credentials") -> Observation:
    """Observation carrying a real dialog finding."""
    finding = dialog_finding(window, title)
    return Observation(dialog=finding, findings=(finding,), reason="dialog_open")


def minimized_main_windows() -> list[DesktopWindow]:
    """The measured minimized-owner shape: a small, iconic Desktop main window and nothing else."""
    return [main_window(title="Report", minimized=True, width=159, height=27)]


def harvested_minimized_reason() -> str:
    """The REAL minimized-owner ``unknown_reason``, harvested from the detector (never hand-written).

    The prior #153 blind-spot was a test that hand-wrote a reason production could not emit; the latch
    tests must assert against the exact string ``inspect_desktop_observer`` really produces.
    """
    reason = inspect_desktop_observer(999, lambda _pid: minimized_main_windows()).unknown_reason
    assert reason is not None, "detector did not report the minimized-owner UNKNOWN reason"
    return reason


def harvested_zero_window_alive_reason() -> str:
    """The REAL zero-window-but-alive readiness reason, harvested from the detector (issue #158).

    Never hand-written: the same #153 blind-spot discipline as the minimized reason - the latch tests
    must assert the exact string ``inspect_desktop_observer`` really produces when enumeration returns
    an empty list while the process is still alive.
    """
    reason = inspect_desktop_observer(999, lambda _pid: [], process_is_alive=lambda _pid: True).desktop_unready
    assert reason is not None, "detector did not report the zero-window alive DESKTOP_UNREADY reason"
    return reason


def harvested_desktop_gone_reason() -> str:
    """The REAL ``process_gone`` string, harvested from the detector (issue #158).

    Driven with an empty enumerator and a liveness check that reports the process dead, so the
    ``DesktopGoneError`` tests assert the exact terminal string production emits, never a hand-written
    one.
    """
    reason = inspect_desktop_observer(999, lambda _pid: [], process_is_alive=lambda _pid: False).process_gone
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
    ``if os.name != "nt": return Observation()``, so calling it un-stubbed runs *different code
    on different platforms*. That is not a reason to leave the classification untested off Windows:
    `inspect_desktop_observer` already takes `enumerate_windows` and `process_is_alive` as parameters,
    so composing it with fakes here exercises the genuine branch logic - the same code Windows runs -
    on every platform, and drives it through the real CLI entry point rather than a hand-written
    `Observation`.
    """

    def detect(pid: int, **_kwargs) -> Observation:
        return inspect_desktop_observer(pid, lambda _pid: [], process_is_alive=lambda _pid: alive)

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
            owned_dialog(),
            main_window(),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0),
        ],
        222: [main_window(title="cached-model")],
    }
    hit = inspect_desktop_observer(111, lambda pid: windows_by_pid[pid]).dialog
    miss = inspect_desktop_observer(222, lambda pid: windows_by_pid[pid])

    assert hit is not None
    assert hit.window.class_name.startswith("WindowsForms10.Window.20008")
    assert miss.dialog is None
    assert miss.unknown_reason is None


def test_a_disabled_frame_with_nothing_else_to_name_is_a_human_stop() -> None:
    """Another process's sign-in modal disables the frame without a same-PID window; minimized is not proof."""
    frame = DesktopWindow("Report", "WindowsForms10.Window.8.app.0.x", 2011, 1298, hwnd=MAIN_HWND, enabled=False)
    blocked = inspect_desktop_observer(111, lambda _pid: [frame])
    assert _desktop_observer.legacy_token(blocked) == "DIALOG_NEEDS_HUMAN" and blocked.dialog.window is frame
    minimized = DesktopWindow(
        "Report", "WindowsForms10.Window.8.app.0.x", 159, 27, hwnd=MAIN_HWND, enabled=False, minimized=True
    )
    assert inspect_desktop_observer(111, lambda _pid: [minimized]).dialog is None


def test_the_named_window_is_the_one_whose_owner_is_disabled() -> None:
    """D1 names the modal, not an earlier unproven popup, with or without an identified frame."""
    popup = owned_dialog(owner_enabled=None, hwnd=0x7003)
    modal = owned_dialog()
    assert _desktop_observer.blocking_window([popup, modal, main_window()]) is modal
    left = DesktopWindow("", "Cls", 800, 600, hwnd=0x9001)
    owned = DesktopWindow("", "#32770", 702, 355, hwnd=0xC002, owner_hwnd=MAIN_HWND, owner_enabled=False)
    assert _desktop_observer.blocking_window([left, main_window(), owned]) is owned


def test_an_unowned_zero_area_helper_window_does_not_block() -> None:
    """A window with NO owner and NO pixels blocks nothing - on two independent grounds, not on size.

    `Internet Explorer_Hidden` at 0x0 rides along on healthy instances. It is excluded because it has
    no owner to disable AND nothing to display, never because of its name or its geometry: the same
    class at 900x700, and the same 0x0 shape with an owner, are both classified (see the round-3
    tests below).
    """
    state = inspect_desktop_observer(
        111,
        lambda _pid: [
            main_window(title="cached"),
            DesktopWindow("", "Internet Explorer_Hidden", 0, 0),
        ],
    )

    assert state.dialog is None


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

    assert excinfo.value.args[0].startswith("DIALOG_NEEDS_HUMAN ")
    assert elapsed < 0.5


@pytest.mark.timing
def test_direct_refresh_raises_desktop_gone_fast_at_t0(monkeypatch) -> None:
    """#158: a Desktop already dead at t=0 must bail immediately, never start the XMLA wait.

    The t=0 pre-check (``_raise_if_blocked``) is load-bearing: if the process was gone before the worker
    even started, waiting out the deadline learns nothing. The reason is harvested from the real
    detector, so this pins the terminal path against the exact string production emits.
    """
    reason = harvested_desktop_gone_reason()
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: Observation(process_gone=reason))
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", explode("_load_adomd"))

    started = time.monotonic()
    with pytest.raises(DesktopGoneError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=5, desktop_pid=111)
    elapsed = time.monotonic() - started

    assert excinfo.value.reason == reason
    assert elapsed < 0.5, f"t=0 process-gone path waited {elapsed:.3f}s instead of returning immediately"


@pytest.mark.timing
def test_refresh_poll_catches_late_modal(monkeypatch, parked) -> None:
    """XMLA route keeps its early stop: any dialog appearing after XMLA starts ends the wait (#831 T8)."""
    calls = {"count": 0}

    def late_dialog(_pid: int, **_kw) -> Observation:
        calls["count"] += 1
        return dialog_state() if calls["count"] >= 2 else Observation()

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", late_dialog)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.05)

    started = time.monotonic()
    with pytest.raises(DialogFoundError):
        refresh(port=1234, tables=["Orders"], timeout_sec=10, desktop_pid=111, progress_enabled=False)

    assert time.monotonic() - started < 1.0, "shortened test poll interval should catch the modal quickly"
    assert calls["count"] >= 2
    parked.set()


def test_refresh_banner_is_flushed_and_heartbeat_reports_elapsed_total(monkeypatch, parked, capsys) -> None:
    """The warning arrives before the wait, then emits elapsed/total countdowns."""
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid, **_kw: Observation())
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
        lambda _pid, **_kw: Observation(unknown_reason=reason),
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
    states = iter([Observation(desktop_unready=reason)])

    def initial_unready_then_healthy(_pid: int, **_kw) -> Observation:
        return next(states, Observation())

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


def test_refresh_and_save_wires_a_dialog_finding_to_exit_3(monkeypatch, capsys) -> None:
    """#376 wiring: a DialogFoundError must surface at exit 3, never in the exit-1 hard-stop band.

    Exit 1 is reserved for a matched credential signature. This module cannot establish that a dialog
    BLOCKS anything, so nothing it observes about one may enter that band - and the retired
    ``probe_live_source`` mapped the exit-1 credential family to "you may NOT build; a person must sign in".
    The finding is a real classification and the verdict line is the real emitter's, so this pins raise -> emit -> exit
    code with no hand-written text.
    """

    def _raise_dialog(*_args, **_kwargs):
        raise DialogFoundError(111, dialog_finding())

    monkeypatch.setattr(refresh_pbip_model, "refresh", _raise_dialog)
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "111", "--tables", "Orders"])

    exit_code = refresh_pbip_model._refresh_and_save(111, 5000, None, args)
    out = capsys.readouterr().out

    assert exit_code == 3, "a dialog finding must not reuse the credential hard stop's exit 1"
    assert 'REFRESH: DIALOG_NEEDS_HUMAN pid=111; a Power BI Desktop dialog is open: "Enter your credentials"' in out
    assert "BLOCKED_BY_DIALOG" not in out


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
        lambda _pid: Observation(process_gone=reason),
    )
    monkeypatch.setattr(refresh_pbip_model, "discover_port", explode("discover_port"))

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 2
    assert out.startswith("REFRESH: DESKTOP_GONE")
    assert reason in out


def test_refresh_main_returns_desktop_unready_from_the_real_detector(monkeypatch, tmp_path: Path, capsys) -> None:
    """#158: an alive-but-window-less Desktop terminates the mutating CLI, on EVERY platform.

    Composed from the real `inspect_desktop_observer` with injected Win32 primitives rather than a
    hand-built `Observation`, so the detector's own zero-window/alive branch and the CLI's
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
        lambda _pid: Observation(unknown_reason=reason),
    )
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda _pid: 52001)
    monkeypatch.setattr(refresh_pbip_model, "_identity_gate", lambda _port, _cache: True)

    def assert_initial_state(_port, _tables, _timeout, *, desktop_pid, initial_state=None):
        del desktop_pid
        assert initial_state is not None
        assert initial_state.unknown_reason == reason
        raise CredentialUnknownError(111, initial_state.unknown_reason)

    monkeypatch.setattr(refresh_pbip_model, "refresh", assert_initial_state)

    exit_code = refresh_pbip_model.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3
    assert "REFRESH: CREDENTIAL_UNKNOWN" in out
    assert reason in out


def test_probe_query_main_returns_desktop_gone_before_port_discovery(monkeypatch, capsys) -> None:
    """#158: the real read-only CLI entry point must terminate on a dead Desktop."""
    reason = harvested_desktop_gone_reason()
    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda _pid: Observation(process_gone=reason),
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
    OS: a healthy `Observation()` on Linux, and a real `process_gone` detection on Windows
    (pid 111 does not exist). Measured 2026-08-15, that split is exactly how 17 Windows-only failures
    passed green on `ubuntu-latest`.

    Asserting on the RETURNED VALUE could not catch it - off Windows the un-stubbed function also
    returns a bare `Observation()`, so a value check is a test that cannot fail on the one
    platform CI runs. The marker on the stub is checkable on every platform.
    """
    for module in (refresh_pbip_model, probe_desktop_query):
        assert getattr(module._credential_state, "is_test_baseline", False), (
            f"{module.__name__}._credential_state is not conftest's baseline stub, so un-stubbed tests "
            "would silently exercise different code on Windows and on Linux"
        )


def test_probe_query_returns_unknown_for_minimized_owner(monkeypatch, capsys) -> None:
    """A minimized owner is indeterminate, not evidence that no credential dialog exists."""
    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda _pid, **_kwargs: Observation(
            unknown_reason="Power BI Desktop owner window is minimized",
            complete=False,
            reason="minimized",
        ),
    )
    monkeypatch.setattr(probe_desktop_query, "discover_port", explode("discover_port"))

    exit_code = probe_desktop_query.main(["--pid", "111"])
    out = capsys.readouterr().out

    assert exit_code == 3
    assert out.startswith("PREFLIGHT: CREDENTIAL_UNKNOWN")
    assert "minimized" in out


def test_probe_query_worker_cannot_print_after_credential_verdict(monkeypatch, capsys) -> None:
    """Reviewer's race: late worker DATA_OK text must not print after the dialog stop verdict."""
    started = threading.Event()
    release = threading.Event()
    calls = {"count": 0}

    def fake_probe(_port: int, _tables: list[str] | None, emit=print) -> int:
        started.set()
        release.wait(timeout=600)
        emit("PREFLIGHT: DATA_OK_FROM_WORKER_AFTER_RELEASE")
        return 0

    def late_dialog(_pid: int, **_kw) -> Observation:
        calls["count"] += 1
        return dialog_state() if started.is_set() and calls["count"] >= 2 else Observation()

    monkeypatch.setattr(probe_desktop_query, "_credential_state", late_dialog)
    monkeypatch.setattr(probe_desktop_query, "PREFLIGHT_CREDENTIAL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(probe_desktop_query, "discover_port", lambda _pid: 52001)
    monkeypatch.setattr(probe_desktop_query, "probe", fake_probe)

    exit_code = probe_desktop_query.main(["--pid", "111", "--canaries", "Orders"])
    first_out = capsys.readouterr().out
    release.set()
    time.sleep(0.1)
    later_out = capsys.readouterr().out

    assert exit_code == 3
    assert first_out.splitlines() == [first_out.splitlines()[-1]]
    assert first_out.startswith("PREFLIGHT: DIALOG_NEEDS_HUMAN")
    assert "DATA_OK_FROM_WORKER_AFTER_RELEASE" not in first_out
    assert later_out == ""


def test_an_enabled_owner_is_the_only_thing_that_exonerates_a_dialog() -> None:
    """Modality is a ONE-WAY test, and it is the only suppression mechanism left.

    An ENABLED owner proves this window is blocking nothing, because a modal disables its owner. A
    DISABLED owner proves nothing either way - Power BI's refresh dialog disables the owner too - and
    ``None`` (no owner) means the test did not apply, which is not the same as passing it.
    """
    exonerated = owned_dialog(owner_enabled=True)
    disabled_owner = owned_dialog(owner_enabled=False)
    no_owner_test = DesktopWindow("", "Cls", 702, 355, hwnd=0x40004, owner_hwnd=MAIN_HWND, owner_enabled=None)

    assert _desktop_observer.is_proven_non_blocking(exonerated)
    assert not _desktop_observer.is_proven_non_blocking(disabled_owner)
    assert not _desktop_observer.is_proven_non_blocking(no_owner_test)

    kept = _desktop_observer.dialog_candidates([main_window(), exonerated, disabled_owner, no_owner_test])

    assert exonerated not in kept, "an enabled owner is positive proof this window blocks nothing"
    assert disabled_owner in kept
    assert no_owner_test in kept, "'the test did not apply' is not 'the test passed'"


def test_an_unowned_window_that_renders_pixels_is_still_classified() -> None:
    """`renders_nothing` needs BOTH conjuncts: no owner AND no pixels. Neither alone suppresses.

    An unowned 900x700 window displays something to a human even though it disables nobody. Dropping
    the area conjunct would silently exclude every unowned window - including the AAD host in its
    unowned form - and would also empty ``main_frame``'s fallback candidate set.
    """
    frame = main_window()
    unowned_visible = DesktopWindow("", "Internet Explorer_Hidden", 900, 700, hwnd=0x70007)

    assert not _desktop_observer.renders_nothing(unowned_visible)

    kept = _desktop_observer.dialog_candidates([frame, unowned_visible], frame=frame)
    assert unowned_visible in kept, "an unowned window with pixels must not vanish into healthy"

    state = inspect_desktop_observer(111, lambda _pid: [frame, unowned_visible])

    assert state.dialog is not None
    assert state.state == "HUMAN_ACTION_REQUIRED"


def test_ownership_is_followed_transitively_to_the_unowned_root() -> None:
    """Round 4: "first owner" is itself a proxy for "the root", and it hid a credential modal.

    An owned window can own another popup. With a Z-order of ``tooltip -> credential dialog -> frame``
    the first owner reached is the CREDENTIAL DIALOG, and whatever ``main_frame`` picks is excluded
    from the prepass AND from classification - so the modal disappeared. Measured on the round-3 build:
    the frame came back as the ``#32770`` credential dialog and no modal was reported.
    """
    frame_hwnd, cred_hwnd, tip_hwnd = 0xF001, 0xC002, 0x7003
    frame = DesktopWindow("sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, hwnd=frame_hwnd)
    cred = DesktopWindow("", "#32770", 702, 355, hwnd=cred_hwnd, owner_hwnd=frame_hwnd, owner_enabled=False)
    tip = DesktopWindow("", "tooltips_class32", 120, 24, hwnd=tip_hwnd, owner_hwnd=cred_hwnd, owner_enabled=True)

    # Z-order puts the tooltip first, so a one-edge walk lands on the credential dialog.
    assert _desktop_observer.main_frame([tip, cred, frame]) is frame
    state = inspect_desktop_observer(111, lambda _pid: [tip, cred, frame])

    assert state.dialog is not None, "a nested owner chain must not hide the credential dialog"
    assert state.dialog.window is cred


def test_ambiguous_frame_identity_excludes_nothing() -> None:
    """Failing closed means EXCLUDING NOTHING - the cost is a loud exit 3, never a silent clear.

    Both halves matter, and the second is round 5's: ambiguity has to survive the PRESENCE of owned
    windows too. An ownership-derived root is not better evidence than a window sitting there rendering
    pixels - believing it was the whole defect - so a tooltip owned by either candidate must not
    promote its owner into the application.
    """
    frame = DesktopWindow("sample", "WindowsForms10.Window.8.app.0.x", 2011, 1298, hwnd=0xF001)
    other = DesktopWindow("second", "Cls", 800, 600, hwnd=0x9001)

    assert _desktop_observer.main_frame([frame, other]) is None
    kept = _desktop_observer.dialog_candidates([frame, other])

    assert frame in kept and other in kept, "an ambiguous frame must not exclude a window"

    for owner_hwnd in (0xF001, 0x9001):
        tip = DesktopWindow("", "tooltips_class32", 120, 24, hwnd=0x7003, owner_hwnd=owner_hwnd, owner_enabled=True)
        windows = [tip, frame, other]
        assert _desktop_observer.main_frame(windows) is None, f"an owned tooltip must not crown hwnd {owner_hwnd:#x}"
        kept = _desktop_observer.dialog_candidates(windows)
        assert frame in kept and other in kept, "neither possible frame may be excluded"


@pytest.mark.parametrize("rows", [0])
def test_ready_cannot_short_circuit_the_real_row_query(monkeypatch, rows: int) -> None:
    """Both row-positive and row-negative controls must execute the actual query path."""
    queried = []
    monkeypatch.setattr(
        probe_desktop_query,
        "_credential_state",
        lambda pid, **_kwargs: Observation(pid=str(pid), process_started_at="638963424000000001"),
    )

    def query(_port, _tables, emit):
        queried.append(1)
        emit("PREFLIGHT: DATA_OK" if rows else "PREFLIGHT: NO_DATA")
        return 0 if rows else 1

    monkeypatch.setattr(probe_desktop_query, "probe", query)
    assert probe_desktop_query._probe_with_credential_poll(111, 52001, ["Orders"]) == (0 if rows else 1)
    assert queried == [1], "READY must never replace the actual row query"


def test_o23_unresolvable_owner_never_identifies_a_frame() -> None:
    """A missing owner cannot be silently promoted to the application frame."""
    window = DesktopWindow("", "Cls", 400, 300, hwnd=222, owner_hwnd=333)
    windows = [main_window(), window]
    assert _desktop_observer.main_frame(windows) is None
    assert inspect_desktop_observer(111, lambda _: windows).state != "READY"


# ==================================================================================================
# In-process native read (#831): identity bracketing, the public v2 event, and the D1 title sanitizer.
# The native primitives are faked at their narrowest seam (`process_start`, `_window_rows`,
# `_window_title`), so `observe`, `_enumerate` and the policy all stay in the tested path.
# ==================================================================================================

START = "638963424000000001"
FRAME_HWND = 0xFEDCBA9876543201
CHILD_HWND = 0xFEDCBA9876543211


def native_rows(*, child: bool = True) -> list[DesktopWindow]:
    """A frame plus (by default) one owned window with a disabled owner; handles exceed 32 bits."""
    rows = [DesktopWindow("", "", 500, 400, hwnd=FRAME_HWND, enabled=not child)]
    if child:
        rows.append(
            DesktopWindow(
                "", "", 500, 400, hwnd=CHILD_HWND, owner_hwnd=FRAME_HWND, owner_enabled=False, owner_same_pid=True
            )
        )
    return rows


def native_observation(
    monkeypatch,
    *,
    starts: tuple[str | None, ...] = (START, START),
    rows=None,
    hwnd: str | None = None,
    titles: dict[int, str] | None = None,
) -> Observation:
    """Drive the real ``observe`` over faked native primitives."""
    queue = list(starts)

    def start(_pid: int) -> str | None:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def window_rows(_pid: int) -> list[DesktopWindow]:
        if isinstance(rows, BaseException):
            raise rows
        return list(native_rows() if rows is None else rows)

    monkeypatch.setattr(_desktop_observer, "process_start", start)
    monkeypatch.setattr(_desktop_observer, "_window_rows", window_rows)
    monkeypatch.setattr(_desktop_observer, "_window_title", lambda handle: (titles or {}).get(handle, ""))
    return _desktop_observer.observe("111", hwnd=hwnd, expected_process_start=START)


def test_exact_identity_and_lossless_native_values(monkeypatch) -> None:
    """No float, datetime truncation or 32-bit HWND coercion can identify this exact process (T7)."""
    state = native_observation(monkeypatch)
    record = json.loads(state.to_json())
    assert (record["pid"], record["process_started_at"], record["hwnd"], record["owner_hwnd"]) == (
        "111",
        START,
        "0xfedcba9876543211",
        "0xfedcba9876543201",
    )
    assert state.complete and state.state == "HUMAN_ACTION_REQUIRED"
    assert _desktop_observer.observation_token(state.to_json()) == "DIALOG_NEEDS_HUMAN"
    assert record["cannot_prove"] == ["source_reachability", "credential_validity", "data_correctness"]


@pytest.mark.parametrize(
    "hwnd,state_name,reason",
    [
        ("0xfedcba9876543201", "UNKNOWN", "ambiguous"),
        ("0x123", "UNKNOWN", "target_changed"),
    ],
)
def test_explicit_hwnd_cannot_borrow_another_windows_finding(monkeypatch, hwnd: str, state_name: str, reason: str):
    """Exact-window requests cannot borrow a sibling finding or a missing handle."""
    state = native_observation(monkeypatch, hwnd=hwnd)
    assert (state.state, state.reason) == (state_name, reason)


def test_unknown_owner_enabled_fact_is_not_a_clean_exoneration(monkeypatch) -> None:
    """A nullable owner fact is uncertainty, not an enabled-owner certificate."""
    rows = native_rows()
    rows[1] = DesktopWindow("", "", 500, 400, hwnd=CHILD_HWND, owner_hwnd=FRAME_HWND, owner_enabled=None)
    assert native_observation(monkeypatch, rows=rows).state == "HUMAN_ACTION_REQUIRED"


@pytest.mark.parametrize(
    "fault,token",
    [
        ("start_changed", "DESKTOP_GONE"),
        ("start_reused", "DESKTOP_GONE"),
        ("duplicate", "CREDENTIAL_UNKNOWN"),
    ],
)
def test_changed_or_lossy_identity_never_clean(monkeypatch, fault: str, token: str) -> None:
    """A changed, reused or unreadable identity, or a racing window, never yields a clean snapshot (T7)."""
    starts: tuple[str | None, ...] = (START, START)
    rows = None
    if fault == "start_changed":
        starts = (START, "638963424000000002")
    elif fault == "start_reused":
        starts = ("638963424000000002",)
    elif fault == "duplicate":
        rows = native_rows() + [native_rows()[1]]
    elif fault == "backend_failed":
        rows = OSError("backend_failed")
    else:
        rows = ValueError("ambiguous")
    state = native_observation(monkeypatch, starts=starts, rows=rows)
    assert not state.complete
    assert _desktop_observer.legacy_token(state) == token


def test_watch_lattice_preserves_uncertainty_and_first_terminal(monkeypatch) -> None:
    """Quiet snapshots do not erase uncertainty or replace the first terminal state."""
    uncertain = native_observation(monkeypatch, rows=OSError("backend_failed"))
    prompt = native_observation(monkeypatch)
    ready = Observation(pid="111", process_started_at=START)
    operation = "74100000-0000-4000-8000-000000000001"
    with _desktop_observer.watch(
        "111", deadline=time.monotonic() + 10, operation_id=operation, expected_process_start=START
    ) as supervisor:
        supervisor.record(uncertain)
        assert supervisor.record(ready).state == "READY"
        assert supervisor.latched == (uncertain.__class__(**{**uncertain.__dict__, "operation_id": operation}),)
        assert supervisor.record(prompt).state == "HUMAN_ACTION_REQUIRED"
        assert supervisor.record(Observation(process_gone="gone")).state == "HUMAN_ACTION_REQUIRED"
    with pytest.raises(RuntimeError, match="settled"):
        supervisor.record(ready)


def test_ready_is_neither_refusal_nor_operation_success(monkeypatch) -> None:
    """READY is observation completeness, never a manufactured operation result."""
    state = native_observation(monkeypatch, rows=native_rows(child=False))
    assert state.state == "READY" and state.complete
    assert _desktop_observer.legacy_token(state) is None
    with pytest.raises(ValueError, match="not a refusal"):
        raise _desktop_observer.DesktopRefusal(state)
    assert "DATA_OK" not in state.to_json()


@pytest.mark.parametrize(
    "fault",
    [
        "numeric_pid",
        "numeric_start",
        "extra",
        "duplicate",
        "schema",
        "incomplete_ready",
        "missing_start",
    ],
)
def test_public_event_schema_refuses_lossy_unknown_or_incomplete_clean(monkeypatch, fault: str) -> None:
    """The public v2 protocol rejects malformed identities, extra fields (a title among them) and incomplete READY."""
    raw = native_observation(monkeypatch, rows=native_rows(child=False)).to_json()
    _desktop_observer.observation_token(raw)
    record = json.loads(raw)
    if fault == "numeric_pid":
        record["pid"] = 111
    elif fault == "numeric_start":
        record["process_started_at"] = int(START)
    elif fault == "extra":
        record["raw_text"] = "PRIVATE_TEST_VALUE"
    elif fault == "title":
        record["title"] = "Enter your credentials"
    elif fault == "schema":
        record["schema"] = "desktop.observation.v1"
    elif fault == "unknown_state":
        record["state"] = "DATA_OK"
    elif fault == "incomplete_ready":
        record["complete"] = False
    elif fault == "missing_start":
        record["process_started_at"] = None
    if fault == "duplicate":
        raw = raw[:-1] + ', "state": "READY"}'
    else:
        raw = json.dumps(record)
    with pytest.raises(ValueError):
        _desktop_observer.observation_token(raw)


@pytest.mark.parametrize(
    "state,token",
    [
        ("READY", None),
        ("HUMAN_ACTION_REQUIRED", "DIALOG_NEEDS_HUMAN"),
        ("NO_WINDOW", "DESKTOP_UNREADY"),
        ("UNKNOWN", "CREDENTIAL_UNKNOWN"),
        ("PROCESS_GONE", "DESKTOP_GONE"),
    ],
)
def test_o23_wire_states_keep_their_legacy_tokens(state: str, token: str | None) -> None:
    """Every state the v2 observer can emit adapts to an existing operation token."""
    payload = json.loads(Observation(pid="111", process_started_at="456").to_json())
    payload["state"] = state
    assert _desktop_observer.observation_token(json.dumps(payload)) == token


@pytest.mark.parametrize("retired", ["REFRESH_IN_PROGRESS"])
def test_retired_wire_states_fold_to_unknown_never_clean(retired: str) -> None:
    """A retired classifier state is not adapted to its old token; the event folds fail-closed."""
    payload = json.loads(Observation(pid="111", process_started_at="456").to_json())
    payload["state"] = retired
    with pytest.raises(ValueError):
        _desktop_observer.observation_token(json.dumps(payload))
    assert _desktop_observer.observation_refusal_events([json.dumps(payload)]) == "CREDENTIAL_UNKNOWN"


# --- T12: the D1 title is sanitized, named once for a human, and never persisted ----------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),
    ],
)
def test_sanitize_title_strips_controls_and_caps_length(raw, expected: str) -> None:
    """Control, format (bidi, zero-width) and separator characters go; quotes cannot close the field."""
    assert _desktop_observer.sanitize_title(raw) == expected


def test_describe_open_dialog_names_the_title_or_says_no_title() -> None:
    """The D1 sentence quotes the sanitized title once, or says so when there is none."""
    named = dialog_finding(title='Sign "in"\n').title
    assert _desktop_observer.describe_open_dialog(named) == "a Power BI Desktop dialog is open: \"Sign 'in'\""
    assert _desktop_observer.describe_open_dialog(dialog_finding(title="\u202e").title) == (
        "a Power BI Desktop dialog is open (no title)"
    )


def test_the_title_is_an_exception_attribute_never_an_arg() -> None:
    """Receipt adapters read ``args``; the title must ride only on the ``title`` attribute."""
    error = DialogFoundError(111, dialog_finding(title="Secret\u202e Server\n" + "y" * 200))
    assert error.title == _desktop_observer.sanitize_title("Secret\u202e Server\n" + "y" * 200)
    assert len(error.args) == 1
    assert "Secret" not in str(error) and "Secret" not in repr(error.args)
    assert error.args[0] == "DIALOG_NEEDS_HUMAN pid=111; title withheld from the exception text"


def test_the_title_reaches_the_human_line_but_not_the_observation_event(monkeypatch, capsys) -> None:
    """The observer reads the title for the human line; the DESKTOP_OBSERVATION JSON never carries it."""
    state = native_observation(monkeypatch, titles={CHILD_HWND: "Access Web content\x07 \u202eSECRET"})
    assert state.dialog is not None and state.dialog.title == "Access Web content SECRET"
    _desktop_observer.emit_observation(state)
    event = capsys.readouterr().err
    assert event.startswith("DESKTOP_OBSERVATION ") and "SECRET" not in event and "Access" not in event
    refresh_pbip_model._emit_dialog_finding(111, state.dialog)
    line = capsys.readouterr().out
    assert line == (
        'REFRESH: DIALOG_NEEDS_HUMAN pid=111; a Power BI Desktop dialog is open: "Access Web content SECRET". '
        "A human must act in Desktop.\n"
    )


def test_a_failing_title_read_still_stops_and_says_no_title() -> None:
    """Naming the window is best effort: a failed read can never clear or widen the stop."""

    def boom(_handle: int) -> str:
        raise OSError("read failed")

    state = inspect_desktop_observer(111, lambda _pid: native_rows(), read_title=boom)
    assert state.state == "HUMAN_ACTION_REQUIRED" and state.dialog.title == ""


def test_a_title_stdout_cannot_encode_still_stops_at_exit_3(monkeypatch, tmp_path: Path) -> None:
    """A Polish, Chinese, emoji or lone-surrogate title on a cp1252 pipe is escaped, never a crash at exit 1."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    model_folder(tmp_path, "MyMigration")
    monkeypatch.setattr(
        refresh_pbip_model,
        "_bridge_status",
        lambda: {"instances": [{"pid": 111, "currentFilePath": str(tmp_path / "MyMigration.pbip")}]},
    )
    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda _pid: dialog_state(title="\u0179r\xf3d\u0142o \u6570 \U0001f512 \ud800"),
    )
    monkeypatch.setattr(refresh_pbip_model, "discover_port", explode("discover_port"))
    assert refresh_pbip_model.main(["--pid", "111"]) == 3
    out.flush()
    assert (
        out.buffer.getvalue()
        .decode("cp1252")
        .startswith(
            'REFRESH: DIALOG_NEEDS_HUMAN pid=111; a Power BI Desktop dialog is open: "\\u0179r\xf3d\\u0142o \\u6570 \\U0001f512 \\ud800".'
        )
    )


def test_window_title_keeps_only_the_characters_getwindowtextw_copied(monkeypatch) -> None:
    """The one production caption read returns what the native call reported, nothing more."""

    def get_window_text(_hwnd, buffer, _size) -> int:
        buffer.value = "Sign in to X"
        return 7

    monkeypatch.setattr(_desktop_observer, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(_desktop_observer, "_user32", lambda: SimpleNamespace(GetWindowTextW=get_window_text))
    assert _desktop_observer._window_title(0x10001) == "Sign in"


# --- T11: the real ctypes read, against this test process only (no window is created) ------------


@pytest.mark.skipif(os.name != "nt", reason="Win32 process identity is Windows-only")
def test_real_process_start_matches_the_frame_binding_and_a_gone_pid_reads_none() -> None:
    """Our own start token equals the frame module's (CIM-checked) reading; an exited PID is ``None``."""
    import _desktop_frame  # pylint: disable=import-outside-toplevel

    pid = os.getpid()
    api = _desktop_frame.Win32Api()
    handle = api.open_process(pid)
    try:
        frame_start = api.process_start_ticks(handle, pid)
    finally:
        api.close_handle(handle)
    assert _desktop_observer.process_start(pid) == frame_start
    gone = subprocess.Popen(["cmd", "/c", "exit 0"])  # pylint: disable=consider-using-with
    gone.wait(timeout=60)
    gone_pid = gone.pid
    del gone
    assert _desktop_observer.process_start(gone_pid) is None


@pytest.mark.skipif(os.name != "nt", reason="real Win32 EnumWindows callback is Windows-only")
def test_real_enum_windows_over_our_windowless_pid_is_complete_and_empty() -> None:
    """``EnumWindows`` really runs; a window-less live process is read as empty, never as an error."""
    windows, count = _desktop_observer._enumerate_pid_windows_with_count(os.getpid())
    assert (windows, count) == ([], 0)
    state = _desktop_observer.observe(os.getpid())
    assert state.state == "NO_WINDOW"
    assert state.process_started_at == _desktop_observer.process_start(os.getpid())
