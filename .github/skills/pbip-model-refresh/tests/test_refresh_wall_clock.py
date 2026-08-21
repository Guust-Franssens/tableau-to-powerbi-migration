"""The refresh must bound itself, so no caller can inherit an unbounded wait.

Regression for a measured 956s hang (2026-08-05): a direct `refresh_pbip_model.py --pid` call
against a never-authenticated Azure SQL server parked on a Desktop sign-in modal. XMLA's
`CommandTimeout = 300` never fired, because the mashup engine waits synchronously on a dialog in
another process that the server cannot preempt. `probe_live_source.py` survived only because it
wraps the script in `subprocess.run(..., timeout=...)`; every direct caller had no bound at all.

The old docstring said "the caller must run its own clock". A rule an agent has to remember is not
a bound - it failed on this repo's own agent. These tests pin the bound into the function.
"""

from __future__ import annotations

import threading
import time

import pytest
import refresh_pbip_model
from refresh_pbip_model import (
    REFRESH_ABSOLUTE_TIMEOUT_SECONDS,
    REFRESH_PROGRESS_LIVENESS_SECONDS,
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
        refresh(port=1234, tables=["Orders"], timeout_sec=1)
    elapsed = time.monotonic() - started

    assert elapsed < 15, f"refresh took {elapsed:.1f}s - the wall clock did not bound it"
    # The message must name the diagnosis, not just the number: an agent reading it has to be able to
    # tell "slow query" (retry smaller) from "parked on a modal" (a human must sign in).
    assert "modal" in str(excinfo.value).lower()


def test_the_worker_is_a_daemon_so_a_parked_engine_cannot_outlive_the_process(parked) -> None:
    """A non-daemon worker would keep the interpreter alive after the verdict, re-hanging the caller."""
    _conn, _released = parked
    with pytest.raises(TimeoutError):
        refresh(port=1234, tables=["Orders"], timeout_sec=1)

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


def test_a_normal_refresh_is_unaffected(monkeypatch) -> None:
    """The bound must be invisible on the happy path - it only ever adds an upper limit."""
    executed: list[str] = []

    class _Cmd:  # pylint: disable=too-few-public-methods,invalid-name
        """A command that records what it was asked to execute."""

        CommandText = ""  # noqa: N815
        CommandTimeout = 0  # noqa: N815

        def ExecuteNonQuery(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            """Record the TMSL instead of talking to a server."""
            executed.append(self.CommandText)

    class _Conn:
        """A connection that succeeds immediately."""

        def Open(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            """Match the ADOMD API surface."""

        def CreateCommand(self):  # noqa: N802  # pylint: disable=invalid-name
            """Match the ADOMD API surface."""
            return _Cmd()

        def Close(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            """Match the ADOMD API surface."""

    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: _Conn())
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")

    ok, message = refresh(port=1234, tables=["Orders"], timeout_sec=5)
    assert ok is True
    assert "Orders" in message
    assert executed and "refresh" in executed[0]


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


def test_progress_current_name_form_is_tolerated() -> None:
    """The real wire value is numeric, but name-form test doubles remain accepted."""
    lines: list[str] = []
    monitor = RefreshProgressMonitor(
        liveness_seconds=120,
        throttle_seconds=2,
        printer=lambda message, **_kwargs: lines.append(message),
        current_event_values={"ProgressReportCurrent", "7"},
    )
    monitor.mark_refresh_started()

    monitor.record_trace_event(
        {"EventClass": "ProgressReportCurrent", "ObjectName": "Flight Activity", "IntegerData": "10000"}
    )

    assert lines and "10,000 rows read" in lines[0]


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


def test_progress_trace_failure_degrades_to_the_legacy_refresh_path(monkeypatch, capsys) -> None:
    """A trace setup failure must not break refresh; it falls back to the old CommandTimeout path."""
    executed: list[tuple[str, int]] = []

    class _Cmd:  # pylint: disable=too-few-public-methods,invalid-name
        """A command that records text and timeout."""

        CommandText = ""  # noqa: N815
        CommandTimeout = 0  # noqa: N815

        def ExecuteNonQuery(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            """Record the command settings."""
            executed.append((self.CommandText, self.CommandTimeout))

    class _Conn:
        """A connection that succeeds immediately."""

        def Open(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            """Match the ADOMD API surface."""

        def CreateCommand(self):  # noqa: N802  # pylint: disable=invalid-name
            """Match the ADOMD API surface."""
            return _Cmd()

        def Close(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
            """Match the ADOMD API surface."""

    def trace_denied(*_args):
        raise RuntimeError("trace denied")

    monkeypatch.setattr(refresh_pbip_model, "_start_refresh_progress_trace", trace_denied)
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: _Conn())
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")

    ok, message = refresh(port=1234, tables=["Orders"], timeout_sec=5, progress_enabled=True)

    assert ok is True
    assert "Orders" in message
    assert executed and executed[0][1] == 5
    captured = capsys.readouterr()
    assert "[progress] unavailable" in captured.err
    assert "[progress] unavailable" not in captured.out


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


def test_progress_flags_are_exposed_with_safe_defaults() -> None:
    """The CLI and direct API default to progress, expose liveness, and keep an explicit opt-out."""
    parser = refresh_pbip_model._build_arg_parser()
    defaults = parser.parse_args([])
    custom = parser.parse_args(
        ["--no-progress", "--progress-liveness-seconds", "42", "--refresh-absolute-timeout-seconds", "900"]
    )

    assert defaults.no_progress is False
    assert refresh_pbip_model.inspect.signature(refresh).parameters["progress_enabled"].default is True
    assert defaults.progress_liveness_seconds == REFRESH_PROGRESS_LIVENESS_SECONDS
    assert defaults.refresh_absolute_timeout_seconds == REFRESH_ABSOLUTE_TIMEOUT_SECONDS
    assert custom.no_progress is True
    assert custom.progress_liveness_seconds == 42
    assert custom.refresh_absolute_timeout_seconds == 900
