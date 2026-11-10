"""The refresh must bound itself, so no caller can inherit an unbounded wait.

Regression for a measured 956s hang (2026-08-05): a direct `refresh_pbip_model.py --pid` call
against a never-authenticated Azure SQL server parked on a Desktop sign-in modal. XMLA's
`CommandTimeout = 300` never fired, because the mashup engine waits synchronously on a dialog in
another process that the server cannot preempt. The retired `probe_live_source.py` survived only because it
wrapped the script in `subprocess.run(..., timeout=...)`; every direct caller had no bound at all.

The old docstring said "the caller must run its own clock". A rule an agent has to remember is not
a bound - it failed on this repo's own agent. These tests pin the bound into the function.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
import _desktop_observer
import refresh_pbip_model
from test_desktop_observer import (
    DIALOG_HWND,
    _FakeProgressMonitor,
    main_window,
    owned_dialog,
)
from refresh_pbip_model import (
    REFRESH_ABSOLUTE_TIMEOUT_SECONDS,
    REFRESH_TIMEOUT_SECONDS,
    REFRESH_WALL_CLOCK_GRACE_SECONDS,
    RefreshProgressMonitor,
    refresh,
)


class _ParkedConnection:
    """An ADOMD stand-in whose command never returns - a mashup engine parked on a modal."""

    def __init__(self, released: threading.Event) -> None:
        self._released = released
        self.closed = False

    def Open(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Match the ADOMD API surface."""

    def CreateCommand(self):  # noqa: N802  # pylint: disable=invalid-name
        """Return a command that blocks until the test releases it."""
        return _ParkedCommand(self._released)

    def Close(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Match the ADOMD API surface."""
        self.closed = True


class _ParkedCommand:  # pylint: disable=too-few-public-methods
    """A command that blocks forever, ignoring `CommandTimeout` exactly as the real one does."""

    def __init__(self, released: threading.Event) -> None:
        self._released = released
        self.CommandText = ""  # noqa: N815  # pylint: disable=invalid-name
        self.CommandTimeout = 0  # noqa: N815  # pylint: disable=invalid-name

    def ExecuteNonQuery(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Block until released - never honouring CommandTimeout, which is the whole point."""
        self._released.wait(timeout=600)


@pytest.fixture(name="parked")
def _parked(monkeypatch):
    """Point `refresh` at a connection whose command never returns.

    The grace is shrunk to keep the test fast. The parked command outlives it by a wide margin on
    purpose - an earlier version of this test wrongly passed because the fake happened to return
    just before the join expired, which measured the fake rather than the bound.
    """
    released = threading.Event()
    conn = _ParkedConnection(released)
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: conn)
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_WALL_CLOCK_GRACE_SECONDS", 2)
    yield conn, released
    released.set()


def test_a_command_that_never_returns_still_yields_a_verdict(parked) -> None:
    """The hang that cost 956s must now end at the wall clock, with a TimeoutError."""
    _conn, _released = parked
    started = time.monotonic()
    with pytest.raises(TimeoutError) as excinfo:
        refresh(port=1234, tables=["Orders"], timeout_sec=1, progress_enabled=False)
    elapsed = time.monotonic() - started

    assert elapsed < 15, f"refresh took {elapsed:.1f}s - the wall clock did not bound it"
    assert "cause unestablished" in str(excinfo.value).lower()
    assert "sign-in" not in str(excinfo.value).lower(), "elapsed time must not invent a dialog"


def test_the_worker_is_a_daemon_so_a_parked_engine_cannot_outlive_the_process(parked) -> None:
    """A non-daemon worker would keep the interpreter alive after the verdict, re-hanging the caller."""
    _conn, _released = parked
    with pytest.raises(TimeoutError):
        refresh(port=1234, tables=["Orders"], timeout_sec=1, progress_enabled=False)

    workers = [t for t in threading.enumerate() if t.name == "xmla-refresh"]
    assert workers, "expected the parked worker to still be running - that is the scenario"
    assert all(t.daemon for t in workers), "a parked worker must not block interpreter exit"


def test_the_bound_is_the_ceiling_plus_grace_not_a_replacement_for_it() -> None:
    """Keep 300s: cold starts are real (a 1-row probe against a suspended warehouse took 167s).

    The fix was always scope, never duration - shortening the ceiling would turn a cold start into a
    false TIMEOUT, which is the error this repo already made once at 90s.
    """
    assert REFRESH_TIMEOUT_SECONDS == 300
    assert REFRESH_WALL_CLOCK_GRACE_SECONDS > 0, "XMLA must get the chance to raise the better error first"


def test_default_refresh_type_is_full() -> None:
    """A DAX-only shortcut must stay opt-in; data-affecting edits need the full default."""
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "1"])
    assert args.refresh_type == "full"


@pytest.mark.parametrize("progress_enabled", [False, True])
def test_terminal_diagnostic_runs_once_after_either_refresh_wait_branch(
    monkeypatch, parked, capsys, progress_enabled: bool
) -> None:
    """A structural stop prints one D1 line naming the dialog in either wait branch (#831)."""
    # The tests deliberately reach bundled internals; pylint may instead resolve the root CLI shim.
    # pylint: disable=protected-access,no-member
    terminal = []
    finding = _desktop_observer.DialogFinding(owned_dialog(hwnd=DIALOG_HWND), "Sign in to SQL Server")

    def state(_pid: int, *, in_flight: bool = False, **_kwargs):
        return _desktop_observer.Observation(dialog=finding) if in_flight else _desktop_observer.Observation()

    original_join = refresh_pbip_model._join_refresh_worker

    def join(*args, **kwargs):
        try:
            return original_join(*args, **kwargs)
        finally:
            terminal.append(True)

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", state)
    monkeypatch.setattr(refresh_pbip_model, "_join_refresh_worker", join)
    monkeypatch.setattr(refresh_pbip_model, "_start_refresh_progress_trace", lambda *_a, **_kw: _FakeProgressMonitor())
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_WALL_CLOCK_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.02)
    argv = ["--pid", "111", "--no-save", "--refresh-absolute-timeout-seconds", "1"]
    if not progress_enabled:
        argv.append("--no-progress")
    args = refresh_pbip_model._build_arg_parser().parse_args(argv)
    assert refresh_pbip_model._refresh_and_save(111, 1234, None, args) == 3
    out = capsys.readouterr().out
    line = 'REFRESH: DIALOG_NEEDS_HUMAN pid=111; a Power BI Desktop dialog is open: "Sign in to SQL Server"'
    assert out.count("DIALOG_NEEDS_HUMAN") == 1 and line in out
    assert terminal
    assert not parked[0].closed, "the worker should remain parked until fixture cleanup"


def test_timeout_diagnostic_does_not_invent_a_dialog_or_source_state(monkeypatch, capsys) -> None:
    """An unverified timeout names no dialog and tells no two-cause narrative."""
    # pylint: disable=protected-access,no-member

    def timeout(*_args, **_kwargs):
        raise TimeoutError("PRIVATE_TEST_VALUE sign in 403")

    monkeypatch.setattr(refresh_pbip_model, "refresh", timeout)
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "111", "--no-save"])
    assert refresh_pbip_model._refresh_and_save(111, 1234, None, args) == 3
    out = capsys.readouterr().out
    assert "REFRESH: TIMEOUT" in out and "cause unestablished" in out
    assert "DIALOG_NEEDS_HUMAN" not in out and "dialog is open" not in out
    assert "PRIVATE_TEST_VALUE" not in out and "sign in" not in out and "two" not in out


def test_structural_stop_precedes_a_later_exception_from_the_refresh_worker(monkeypatch, parked) -> None:
    """The XMLA route still stops at once on a dialog; a later worker failure cannot replace that verdict."""
    _conn, released = parked
    failure = ValueError("synthetic worker failure")

    def fail_after_release(command):
        command._released.wait(3)
        raise failure

    def state(pid, *, in_flight=False, **_kwargs):
        if not in_flight:
            return _desktop_observer.Observation()
        return _desktop_observer.inspect_desktop_observer(
            pid, lambda _pid: [owned_dialog(), main_window()], read_title=lambda _hwnd: "Sign in"
        )

    monkeypatch.setattr(_ParkedCommand, "ExecuteNonQuery", fail_after_release)
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", state)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.005)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_WALL_CLOCK_GRACE_SECONDS", 0)
    started = time.monotonic()
    try:
        with pytest.raises(_desktop_observer.DialogFoundError) as stop:
            refresh(port=1234, tables=["Orders"], desktop_pid=111, progress_enabled=False, timeout_sec=3)
        assert time.monotonic() - started < 2.5, "the XMLA route must stop early on a dialog, not at its deadline"
    finally:
        released.set()
    assert stop.value.finding.title == "Sign in"


@pytest.mark.parametrize(
    "dialog,completed_at,mode",
    [
        (True, 1.0, "progress"),
        (False, 0.5, "observation"),
    ],
    ids=["dialog-at-deadline-progress", "healthy-before-deadline-observation"],
)
def test_wait_completion_must_be_observed_before_the_original_deadline(monkeypatch, mode, completed_at, dialog) -> None:
    """A delayed observation/join must not launder a late completion into success in either branch."""
    clock = _FakeClock()
    monkeypatch.setattr(refresh_pbip_model, "time", clock)
    monkeypatch.setattr(_desktop_observer, "time", clock)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.1)
    state = _desktop_observer.Observation()
    if dialog:
        state = _desktop_observer.Observation(dialog=_desktop_observer.DialogFinding(owned_dialog(), "Sign in"))

    class Worker:
        def is_alive(self):
            return clock.now < completed_at

        def join(self, timeout):
            clock.now += min(timeout, 0.1)

    def delayed_inspection(_pid, **_kwargs):
        clock.now = completed_at
        return state

    monkeypatch.setattr(refresh_pbip_model, "_in_flight_credential_state", delayed_inspection)
    arguments = dict(
        desktop_pid=111,
        initial_state=_desktop_observer.Observation(),
        total_timeout=1.0,
        progress_monitor=_FakeProgressMonitor() if mode == "progress" else None,
        observation_mode=mode == "observation",
    )
    if dialog and completed_at < 1.0:
        # The XMLA route keeps its early stop: a dialog seen before the deadline is named at once.
        with pytest.raises(_desktop_observer.DialogFoundError) as error:
            refresh_pbip_model._join_refresh_worker(Worker(), **arguments)
        assert error.value.finding.title == "Sign in"
    elif dialog:
        # Seen only at/after the deadline, it is no longer credited as a finding: the wait is unestablished.
        with pytest.raises(_desktop_observer.CredentialUnknownError):
            refresh_pbip_model._join_refresh_worker(Worker(), **arguments)
    else:
        completed = refresh_pbip_model._join_refresh_worker(Worker(), **arguments)
        assert completed is (completed_at < 1.0), "completion at/after the original deadline is not success"
    assert clock.now == completed_at


def test_no_pid_join_cannot_accept_a_completion_after_its_deadline(monkeypatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(refresh_pbip_model, "time", clock)

    def delayed_join(_timeout):
        clock.now = 2.0

    worker = SimpleNamespace(is_alive=lambda: clock.now < 1.5, join=delayed_join)
    completed = refresh_pbip_model._join_refresh_worker(
        worker,
        desktop_pid=None,
        initial_state=None,
        total_timeout=1.0,
        progress_monitor=None,
    )
    assert completed is False, "the uninspected branch must enforce the same original deadline"


def test_calculate_only_aliases_select_tmsl_calculate(monkeypatch) -> None:
    """Both documented flag names send TMSL type 'calculate', not a full source re-read."""
    executed: list[str] = []

    class _Cmd:  # pylint: disable=too-few-public-methods,invalid-name
        CommandText = ""  # noqa: N815
        CommandTimeout = 0  # noqa: N815

        def ExecuteNonQuery(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            executed.append(self.CommandText)

    class _Conn:
        def Open(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            pass

        def CreateCommand(self):  # noqa: N802  # pylint: disable=invalid-name
            return _Cmd()

        def Close(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            pass

    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: _Conn())
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")

    for flag in ("--calculate-only", "--measures-only"):
        args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "1", flag])
        ok, message = refresh(port=1234, tables=None, timeout_sec=5, refresh_type=args.refresh_type)
        assert ok is True
        assert message.startswith("calculated entire database")

    assert [json.loads(command)["refresh"]["type"] for command in executed] == ["calculate", "calculate"]
    assert all(json.loads(command)["refresh"]["objects"] == [{"database": "catalog-1"}] for command in executed)


def test_refresh_rejects_unknown_refresh_type_before_xmla(monkeypatch) -> None:
    """The mode guard must fire before any ADOMD connection is opened."""
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: pytest.fail("XMLA should not be opened"))
    with pytest.raises(ValueError, match="unsupported refresh type"):
        refresh(port=1234, tables=None, timeout_sec=5, refresh_type="Calculate")


def test_calculate_uses_legacy_banner_not_progress_row_count_liveness(monkeypatch, capsys) -> None:
    """Calculate emits few/no row-count events, so it must not arm the progress-liveness trace."""
    executed: list[str] = []

    class _Cmd:  # pylint: disable=too-few-public-methods,invalid-name
        CommandText = ""  # noqa: N815
        CommandTimeout = 0  # noqa: N815

        def ExecuteNonQuery(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            executed.append(self.CommandText)

    class _Conn:
        def Open(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            pass

        def CreateCommand(self):  # noqa: N802  # pylint: disable=invalid-name
            return _Cmd()

        def Close(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            pass

    monkeypatch.setattr(refresh_pbip_model, "_start_refresh_progress_trace", lambda *_args: pytest.fail("no trace"))
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: _Conn())
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")

    ok, message = refresh(
        port=1234,
        tables=None,
        timeout_sec=5,
        refresh_type="calculate",
        desktop_pid=111,
        progress_enabled=True,
    )

    assert ok is True
    assert message.startswith("calculated entire database")
    out = capsys.readouterr().out
    assert "Calculate in progress" in out
    assert "without reading source rows" in out
    assert "progress liveness" not in out
    assert "no progress event" not in out
    assert json.loads(executed[0])["refresh"]["type"] == "calculate"


def test_an_error_from_the_worker_reaches_the_caller_unchanged(monkeypatch) -> None:
    """Running on a thread must not swallow or re-wrap a real failure - main classifies on it."""

    class _Boom:  # pylint: disable=too-few-public-methods
        """A connection that fails the way an unreachable host does."""

        def Open(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            """Fail on connect - the failure must survive the thread hop."""
            raise ValueError("connection refused")

    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: _Boom())

    with pytest.raises(ValueError, match="connection refused"):
        refresh(port=1234, tables=["Orders"], timeout_sec=5)


class _FakeClock:
    """Manual monotonic clock for progress throttling tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current fake time."""
        return self.now


def test_progress_current_prints_row_counts_without_percentages() -> None:
    """ProgressReportCurrent renders an honest row counter, not a fabricated percent/ETA."""
    clock = _FakeClock()
    lines: list[str] = []

    def record(message: str, **_kwargs) -> None:
        lines.append(message)

    monitor = RefreshProgressMonitor(
        liveness_seconds=120,
        throttle_seconds=2,
        clock=clock.monotonic,
        printer=record,
        current_event_values={"ProgressReportCurrent", "7"},
    )
    monitor.mark_refresh_started()
    monitor.record_trace_event({"EventClass": "7", "ObjectName": "Flight Activity", "IntegerData": "240000"})
    clock.now = 1.0
    monitor.record_trace_event({"EventClass": "7", "ObjectName": "Flight Activity", "IntegerData": "250000"})
    clock.now = 2.1
    monitor.record_trace_event({"EventClass": "7", "ObjectName": "Flight Activity", "IntegerData": "260000"})

    assert lines == [
        "[progress] Flight Activity: 240,000 rows read (0.0s)",
        "[progress] Flight Activity: 260,000 rows read (2.1s)",
    ]
    assert all("%" not in line and "ETA" not in line for line in lines)


def test_progress_event_values_include_the_numeric_wire_value() -> None:
    """Production derives the numeric EventClass value from TraceEventClass."""

    class _TraceEventClass:  # pylint: disable=too-few-public-methods
        ProgressReportCurrent = 7

    assert refresh_pbip_model._progress_event_values(_TraceEventClass, "ProgressReportCurrent") == {
        "ProgressReportCurrent",
        "7",
    }


def test_progress_liveness_warns_but_does_not_kill_a_quiet_refresh(monkeypatch, capsys) -> None:
    """A slow first row can be healthy; liveness reports silence but only the backstop kills."""
    executed: list[tuple[str, int]] = []

    class _SlowCmd:  # pylint: disable=too-few-public-methods,invalid-name
        """A command that finishes after the liveness window without emitting trace events."""

        CommandText = ""  # noqa: N815
        CommandTimeout = 0  # noqa: N815

        def ExecuteNonQuery(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            """Sleep past liveness, then succeed."""
            time.sleep(0.25)
            executed.append((self.CommandText, self.CommandTimeout))

    class _Conn:
        """A connection with one slow successful command."""

        def Open(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            """Match the ADOMD API surface."""

        def CreateCommand(self):  # noqa: N802  # pylint: disable=invalid-name
            """Match the ADOMD API surface."""
            return _SlowCmd()

        def Close(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            """Match the ADOMD API surface."""

    monitor = RefreshProgressMonitor(liveness_seconds=0.1, throttle_seconds=2)
    monkeypatch.setattr(refresh_pbip_model, "_start_refresh_progress_trace", lambda *_args: monitor)
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: _Conn())
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")

    ok, message = refresh(port=1234, tables=["Orders"], timeout_sec=1, progress_liveness_sec=0.1)

    assert ok is True
    assert "Orders" in message
    assert executed and executed[0][1] == REFRESH_ABSOLUTE_TIMEOUT_SECONDS
    assert "[progress] no progress event" in capsys.readouterr().out


def test_traced_refresh_supersedes_the_old_elapsed_only_heartbeat(monkeypatch, parked, capsys) -> None:
    """Default progress uses trace evidence/warnings, not the old identical 'still refreshing' signal."""
    monitor = RefreshProgressMonitor(liveness_seconds=0.05, throttle_seconds=0.05)
    monkeypatch.setattr(refresh_pbip_model, "_start_refresh_progress_trace", lambda *_args: monitor)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_HEARTBEAT_SECONDS", 0.05)

    with pytest.raises(TimeoutError, match="did not return within"):
        refresh(port=1234, tables=["Orders"], timeout_sec=5, progress_liveness_sec=0.05, absolute_timeout_sec=0.2)

    out = capsys.readouterr().out
    assert "[progress] no progress event" in out
    assert "still refreshing" not in out
    parked[1].set()
