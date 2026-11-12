"""The per-model lock serialises every Desktop Refresh/Save against one model, and refuses typed.

Two runs against one model must not interleave their Desktop actions (#114). The WHOLE UI
transaction is serialised by a per-model interprocess lock (`_lock.model_lock`) whose dead-holder
locks are reclaimed rather than wedging the tool forever. A lock that cannot be written refuses at
once, before any Desktop action; a lock held by a live peer refuses as NOT_PERSISTED.
"""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# `conftest.py` next to this file puts the skill's own `scripts/` on `sys.path`.
# ruff: noqa: E402  (the conftest-provided path must be in place before these imports)
import refresh_pbip_model
import _lock


def _model(root: Path, name: str = "MyMigration") -> Path:
    """A minimal `<Name>.SemanticModel` on disk; returns the `cache.abf` destination inside it."""
    definition = root / f"{name}.SemanticModel" / "definition"
    (definition / "tables").mkdir(parents=True)
    (definition / "tables" / "Orders.tmdl").write_text("table 'Orders'\n\n\tcolumn X\n", encoding="utf-8")
    return root / f"{name}.SemanticModel" / ".pbi" / "cache.abf"


def _reaped_pid() -> int:
    """A PID that is now dead: spawn a trivial process, wait for it to exit, return its PID."""
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    proc.wait()
    return proc.pid


def test_model_lock_is_exclusive_and_times_out_then_releases(tmp_path: Path) -> None:
    """The per-model lock is mutually exclusive, bounds the wait, and cleans up on release."""
    lock_path = tmp_path / "cache.abf.lock"
    with refresh_pbip_model.model_lock(lock_path, timeout=2.0, poll=0.02):
        assert lock_path.exists()
        with pytest.raises(refresh_pbip_model.ModelLockTimeout):
            with refresh_pbip_model.model_lock(lock_path, timeout=0.2, poll=0.02):
                raise AssertionError("a second acquisition must not succeed while the lock is held")
    assert not lock_path.exists(), "the lock file is removed on release"
    with refresh_pbip_model.model_lock(lock_path, timeout=2.0, poll=0.02):
        assert lock_path.exists(), "the lock is re-acquirable once released"


def test_model_lock_recovers_from_a_dead_holder(tmp_path: Path) -> None:
    """A lock left behind by a process that has since died must be reclaimable, not a permanent block."""
    lock_path = tmp_path / "cache.abf.lock"
    dead_pid = _reaped_pid()
    lock_path.write_text(f"{dead_pid}\n{socket.gethostname()}\n{time.time()}\n", encoding="utf-8")

    acquired = False
    with refresh_pbip_model.model_lock(lock_path, timeout=2.0, poll=0.02):
        acquired = True
        assert lock_path.read_text(encoding="utf-8").splitlines()[0].strip() == str(os.getpid()), (
            "the reclaimed lock must now record the live holder"
        )
    assert acquired, "a lock held by a dead process must be reclaimed, not blocked on"


def test_ui_route_refuses_before_any_desktop_action_when_a_live_peer_holds_the_model_lock(
    tmp_path, monkeypatch, capsys
) -> None:
    """A live peer holding the lock ends NOT_PERSISTED exit 1 with zero Desktop actions (#114)."""
    cache = _model(tmp_path)
    cache.parent.mkdir()
    actions: list[str] = []
    monkeypatch.setattr(refresh_pbip_model, "PERSIST_LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(refresh_pbip_model, "_ui_root", lambda *_args: 789)
    monkeypatch.setattr(
        refresh_pbip_model,
        "bind_desktop",
        lambda *_args, **_kwargs: SimpleNamespace(identity=SimpleNamespace(process_start="456")),
    )
    monkeypatch.setattr(refresh_pbip_model, "invoke_desktop_action", lambda action, **_kw: actions.append(action))
    initial = SimpleNamespace(
        complete=True,
        state="READY",
        process_started_at="456",
        target_window=SimpleNamespace(width=1280, height=800),
    )
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "123"])

    with refresh_pbip_model.model_lock(cache.with_name(cache.name + ".lock"), timeout=2.0, poll=0.02):
        result = refresh_pbip_model._ui_refresh_and_save(
            123, 49157, cache, args, initial, deadline=time.monotonic() + 60
        )
    assert result == 1
    assert "REFRESH: NOT_PERSISTED (model lock unavailable:" in capsys.readouterr().out
    assert actions == [], "no Refresh/Save may be invoked while a peer holds the model lock"


class _LockOs:
    """`_lock`'s private view of `os`: only the lock file's create/write fail, everything else is real."""

    def __init__(self, lock_path: Path, open_errors=(), write_error: OSError | None = None, on_open=None) -> None:
        self.lock_path, self.open_errors, self.write_error, self.on_open = (
            lock_path,
            list(open_errors),
            write_error,
            on_open,
        )
        self.opens = 0

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, *args):
        if Path(path) == self.lock_path:
            self.opens += 1
            if self.on_open is not None:
                self.on_open(self.opens)
            if self.open_errors:
                raise self.open_errors.pop(0)
        return os.open(path, *args)

    def write(self, fd, data):
        if self.write_error is not None:
            raise self.write_error
        return os.write(fd, data)


def _denied(path: Path) -> PermissionError:
    return PermissionError(errno.EACCES, "Permission denied", str(path))


def _lock_failure(lock_path: Path, timeout: float = 30.0) -> BaseException:
    with pytest.raises(Exception) as caught:  # the exact type is asserted by each caller
        with refresh_pbip_model.model_lock(lock_path, timeout=timeout, poll=0.01):
            raise AssertionError("an unwritable lock must never be entered")
    return caught.value


@pytest.mark.parametrize(
    "error,reason",
    [
        (OSError(errno.EROFS, "Read-only file system", "/private/host/.pbi/cache.abf.lock"), "OSError [Errno 30]"),
    ],
    ids=["read-only-media"],
)
def test_model_lock_that_cannot_be_created_refuses_at_once_without_the_path(monkeypatch, tmp_path, error, reason):
    """No lock file and a refused create is a directory that will not take one: refuse now, typed."""
    lock_path = tmp_path / "cache.abf.lock"
    fake = _LockOs(lock_path, open_errors=[error] * 1000)
    monkeypatch.setattr(_lock, "os", fake)
    started = time.monotonic()
    failure = _lock_failure(lock_path)
    assert type(failure) is refresh_pbip_model.LockUnavailable, f"raw {type(failure).__name__} escaped the lock"
    assert not isinstance(failure, refresh_pbip_model.ModelLockTimeout), "nobody holds this lock"
    assert str(failure) == reason
    assert failure.__cause__ is None and failure.__suppress_context__
    assert fake.opens == 1 and time.monotonic() - started < 5, "a non-writable directory must not wait out 30s"
    assert not lock_path.exists()


@pytest.mark.parametrize("clears", [True], ids=["clears"])
def test_model_lock_waits_out_a_delete_pending_lock_file_within_its_deadline(monkeypatch, tmp_path, clears):
    """Windows reports EACCES for a present, delete-pending lock file; that is transient, so it is retried."""
    lock_path = tmp_path / "cache.abf.lock"
    lock_path.write_text("pending\n", encoding="utf-8")

    def finish_delete(attempt: int) -> None:
        if clears and attempt == 3:
            fake.open_errors.clear()
            lock_path.unlink()

    fake = _LockOs(lock_path, open_errors=[_denied(lock_path)] * 1000, on_open=finish_delete)
    monkeypatch.setattr(_lock, "os", fake)
    if clears:
        with refresh_pbip_model.model_lock(lock_path, timeout=30.0, poll=0.01):
            assert lock_path.read_text(encoding="utf-8").splitlines()[0] == str(os.getpid())
        assert fake.opens == 3 and not lock_path.exists()
    else:
        failure = _lock_failure(lock_path, timeout=0.2)
        assert type(failure) is refresh_pbip_model.LockUnavailable
        assert str(failure) == "PermissionError [Errno 13]" and fake.opens > 1


def test_model_lock_write_failure_removes_its_file_and_refuses(monkeypatch, tmp_path):
    """A created-but-unwritten lock is torn: remove it (it is ours) and refuse rather than hold nothing."""
    lock_path = tmp_path / "cache.abf.lock"
    monkeypatch.setattr(_lock, "os", _LockOs(lock_path, write_error=OSError(errno.ENOSPC, "No space left")))
    failure = _lock_failure(lock_path)
    assert type(failure) is refresh_pbip_model.LockUnavailable and str(failure) == "OSError [Errno 28]"
    assert not lock_path.exists(), "a torn lock file would block every later run for the stale window"


def test_ui_route_refuses_before_any_desktop_action_when_the_model_lock_is_not_writable(
    tmp_path, monkeypatch, capsys
) -> None:
    """The UI route takes the lock before Refresh; an unwritable lock refuses with zero Desktop actions."""
    cache = _model(tmp_path)
    cache.parent.mkdir()
    lock_path = cache.with_name(cache.name + ".lock")
    monkeypatch.setattr(_lock, "os", _LockOs(lock_path, open_errors=[_denied(lock_path)] * 1000))
    actions: list[str] = []
    entered: list[str] = []
    locked = refresh_pbip_model._ui_refresh_locked

    def spy_locked(*args, **kwargs):
        entered.append("locked")
        return locked(*args, **kwargs)

    def invoke(action, **_kwargs):
        actions.append(action)
        return SimpleNamespace(invoked=False, action_started_ticks=None, reason="test", focus_attempts=0)

    bound = SimpleNamespace(identity=SimpleNamespace(process_start="456"))
    monkeypatch.setattr(refresh_pbip_model, "_ui_root", lambda *_args: 789)
    monkeypatch.setattr(refresh_pbip_model, "bind_desktop", lambda *_args, **_kwargs: bound)
    monkeypatch.setattr(refresh_pbip_model, "_ui_refresh_locked", spy_locked)
    monkeypatch.setattr(refresh_pbip_model, "_ui_observer_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(refresh_pbip_model, "_identity_gate", lambda *_args: True)
    monkeypatch.setattr(
        refresh_pbip_model, "read_partition_snapshot", lambda *_args, **_kwargs: SimpleNamespace(partitions=())
    )
    monkeypatch.setattr(refresh_pbip_model, "invoke_desktop_action", invoke)
    initial = SimpleNamespace(
        complete=True,
        state="READY",
        process_started_at="456",
        target_window=SimpleNamespace(width=1280, height=800),
    )
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "123"])

    result = refresh_pbip_model._ui_refresh_and_save(123, 49157, cache, args, initial, deadline=time.monotonic() + 60)
    out = capsys.readouterr().out
    assert result == 2
    assert "REFRESH: UI_PREFLIGHT_REFUSED (model lock not writable: PermissionError [Errno 13])" in out
    assert str(tmp_path) not in out
    assert entered == [] and actions == [], "no Refresh/Save may be invoked without the model lock"


def test_o23_structural_stop_withholds_canaries_and_save(monkeypatch, capsys) -> None:
    """A prompt after Invoke must not reach either data verification or persistence."""
    # pylint: disable=import-outside-toplevel
    from _desktop_observer import DesktopWindow, DialogFinding, Observation
    from test_desktop_actions import _wire_ui_route

    observer = refresh_pbip_model._ui_observer_state
    wait = refresh_pbip_model._wait_for_engine_completion
    context, args, calls = _wire_ui_route(monkeypatch)
    frame = DesktopWindow("", "", 1280, 800, hwnd=789)
    prompt = DesktopWindow("", "", 500, 300, hwnd=888, owner_hwnd=789, owner_enabled=False)
    ready = Observation(pid="123", process_started_at="456", target_window=frame)
    stopped = Observation(
        pid="123", process_started_at="456", target_window=frame, dialog=DialogFinding(prompt, "Sign in")
    )
    monkeypatch.setattr(refresh_pbip_model, "_ui_observer_state", observer)
    monkeypatch.setattr(refresh_pbip_model, "_wait_for_engine_completion", wait)
    monkeypatch.setattr(
        refresh_pbip_model, "_in_flight_credential_state", lambda *_a, **_k: stopped if calls else ready
    )
    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 3
    assert calls == ["Refresh"]
    assert (
        'REFRESH: DIALOG_NEEDS_HUMAN pid=123; a Power BI Desktop dialog is open: "Sign in"' in capsys.readouterr().out
    )
