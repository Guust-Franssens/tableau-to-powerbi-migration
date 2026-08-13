"""Per-model interprocess lock for ``refresh_pbip_model`` persistence (issue #114).

Persisting a cache is a multi-step transaction: snapshot ``database.tmdl``, provisionally raise its
``compatibilityLevel`` to the live level, write-and-swap ``cache.abf``, then commit or roll the bump
back. Two runs against the SAME model share two mutable resources across that transaction - the
staging file (which :func:`_abf._staging_path` now gives each run a private name) AND, the one unique
names cannot fix, the compatibility edit on ``database.tmdl``. A hostile interleaving of the compat
edit alone leaves the project declaring a level no present cache was written at - unopenable, with NO
visible Desktop error (the "CompatibilityLevel downgrade" brick). So the WHOLE transaction has to be
serialised, not merely the temp file.

This module is the serialiser: a best-effort, cross-platform advisory lock built on an
``O_CREAT | O_EXCL`` lock file, so it needs no third-party dependency and travels with the skill
bundle. A lock held by a process that has since DIED is RECLAIMED - a dead holder must never wedge the
tool forever - while a lock held by a LIVE process is waited for up to a timeout and then reported as
:class:`ModelLockTimeout` rather than blocked on indefinitely. The safe direction on any uncertainty
is to UNDER-reclaim (wait a little longer for a lock that is actually free) rather than OVER-reclaim
(yank a lock from a live persist and re-open the #114 race).

Self-contained: depends only on ``os``/``time``/``uuid``/``socket``/``contextlib`` (and ``ctypes`` on
Windows, for process-liveness), so the bundle stays copyable with no external requirement.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
import uuid
from pathlib import Path

# A persist writes ~114 KB and takes seconds, so a live concurrent holder should clear well within
# this; the timeout only bites a genuinely STUCK live holder (a dead one is reclaimed at once, never
# waited for).
_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_POLL_SECONDS = 0.05
# Fallback staleness for a lock we cannot pin to a live/dead PID (written by another host, or a torn
# lock file): far longer than any real persist, so it never reclaims a lock a live local holder is
# still using. Same-host liveness is checked first and takes precedence over this.
_DEFAULT_STALE_AFTER_SECONDS = 600.0

_HOSTNAME = socket.gethostname()


class ModelLockTimeout(RuntimeError):
    """The per-model persistence lock was held by a LIVE process past the acquisition timeout.

    Distinct from the dead-holder case (which is reclaimed, never raised): this means another run is
    genuinely persisting the SAME model right now. The caller must NOT force past it - doing so is
    exactly the #114 race - but back off, or fall back to a mechanism that does its own compatibility
    alignment (Desktop's UI Save).
    """


def _process_alive(pid: int) -> bool:
    """Is a process with ``pid`` running on THIS host? Conservative: on any uncertainty, assume alive.

    Stale-lock reclaim hinges on this, and the safe error is to UNDER-reclaim (wait longer for a lock
    that is actually free) rather than OVER-reclaim (steal a lock from a live persist and re-open the
    #114 race), so every ambiguous result returns True.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _process_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True  # unknown -> assume alive
    return True


def _process_alive_windows(pid: int) -> bool:
    """Windows process-liveness via ``OpenProcess``/``GetExitCodeProcess`` (no ``os.kill(pid, 0)``).

    ``os.kill`` on Windows cannot probe with signal 0 - it would TERMINATE the process - so this uses
    the Win32 API directly. A PID that no longer exists fails ``OpenProcess`` with
    ``ERROR_INVALID_PARAMETER``; ``ERROR_ACCESS_DENIED`` means it exists but is not ours (alive). The
    ``STILL_ACTIVE`` ambiguity (a process that genuinely exited with code 259) only ever makes a dead
    holder look alive, i.e. errs toward UNDER-reclaim, which is the safe direction.
    """
    import ctypes  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    from ctypes import wintypes  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == still_active
        return True
    finally:
        kernel32.CloseHandle(handle)


class _ModelLock:
    """A non-reentrant, cross-process advisory lock backed by a single ``O_CREAT | O_EXCL`` lock file.

    Non-reentrant on purpose: two persist transactions in one process (a nested/retried run) are two
    logical writers and must still contend, so a second acquire against a held lock blocks and then
    raises :class:`ModelLockTimeout` - it never silently re-enters.
    """

    def __init__(self, path: Path | os.PathLike | str, timeout: float, poll: float, stale_after: float) -> None:
        self._path = Path(path)
        self._timeout = float(timeout)
        self._poll = float(poll)
        self._stale_after = float(stale_after)
        self._held = False

    def __enter__(self) -> _ModelLock:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def acquire(self) -> _ModelLock:
        """Take the lock, reclaiming it from a dead holder, or raise `ModelLockTimeout`."""
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if self._holder_gone():
                    self._reclaim()
                    continue
                if time.monotonic() >= deadline:
                    raise ModelLockTimeout(
                        f"model persist lock {self._path} is held by {self._read_holder() or 'an unknown process'} "
                        f"and did not release within {self._timeout:.0f}s"
                    ) from None
                time.sleep(self._poll)
                continue
            try:
                os.write(fd, self._payload())
            finally:
                os.close(fd)
            self._held = True
            return self

    def release(self) -> None:
        """Drop the lock, but only if this process still owns it."""
        if not self._held:
            return
        self._held = False
        # Remove the lock only if it is still OURS: a peer that judged us stale may have reclaimed and
        # re-created it, and deleting that would free a lock we no longer own.
        with contextlib.suppress(OSError):
            if self._read_pid() == os.getpid():
                os.unlink(self._path)

    def _payload(self) -> bytes:
        return f"{os.getpid()}\n{_HOSTNAME}\n{time.time()}\n".encode()

    def _read_lines(self) -> list[str]:
        try:
            return self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def _read_pid(self) -> int | None:
        lines = self._read_lines()
        try:
            return int(lines[0].strip()) if lines else None
        except ValueError:
            return None

    def _read_host(self) -> str | None:
        lines = self._read_lines()
        return lines[1].strip() if len(lines) > 1 else None

    def _read_holder(self) -> str | None:
        pid, host = self._read_pid(), self._read_host()
        return f"pid {pid} on {host or '?'}" if pid is not None else None

    def _holder_gone(self) -> bool:
        """Is the current holder provably gone, so the lock may be reclaimed rather than waited on?"""
        pid, host = self._read_pid(), self._read_host()
        if pid is not None and host == _HOSTNAME:
            return not _process_alive(pid)
        # Cross-host or an unparseable/torn lock file: liveness is unknowable, so fall back to age -
        # only a lock far older than any real persist is treated as abandoned.
        try:
            age = time.time() - self._path.stat().st_mtime
        except OSError:
            return False
        return age > self._stale_after

    def _reclaim(self) -> None:
        """Atomically claim the right to delete a stale lock, then delete it. Racer-safe via rename.

        ``os.rename`` of the single lock file succeeds for exactly one racer; every other racer's
        rename fails (the source is already gone) and it simply retries the acquire loop, where it
        will either create the lock afresh or find a live holder to wait on. This closes the classic
        reclaim race where two processes both unlink-and-recreate and both believe they hold it.
        """
        claim = self._path.with_name(f"{self._path.name}.reclaim.{os.getpid()}.{uuid.uuid4().hex}")
        try:
            os.rename(self._path, claim)
        except OSError:
            return
        with contextlib.suppress(OSError):
            os.unlink(claim)


def model_lock(
    path: Path | os.PathLike | str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    poll: float = _DEFAULT_POLL_SECONDS,
    stale_after: float = _DEFAULT_STALE_AFTER_SECONDS,
) -> _ModelLock:
    """A per-model persistence lock as a context manager. See :class:`_ModelLock`.

    Acquire it around the ENTIRE persist transaction (snapshot -> align -> write -> commit/rollback),
    so a concurrent run cannot interleave with the shared compatibility edit.
    """
    return _ModelLock(path, timeout=timeout, poll=poll, stale_after=stale_after)
