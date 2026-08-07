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
from refresh_pbip_model import REFRESH_TIMEOUT_SECONDS, REFRESH_WALL_CLOCK_GRACE_SECONDS, refresh


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
