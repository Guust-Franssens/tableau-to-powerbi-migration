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

import json
import hashlib
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import _credential_modal
import refresh_pbip_model
import _operator_pause
from test_operator_pause import NATIVE, NATIVE_DLL, make_run, publish
from test_operator_pause import run_paths as _pause_run_paths_fixture  # noqa: F401
from test_credential_modal_detection import (
    DIALOG_HWND,
    MAIN_HWND,
    _FakeProgressMonitor,
    main_window,
    owned_dialog,
    visual_runtime as _visual_runtime_fixture,  # noqa: F401  (shared pytest fixture)
)
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
        refresh(port=1234, tables=["Orders"], timeout_sec=1, progress_enabled=False)
    elapsed = time.monotonic() - started

    assert elapsed < 15, f"refresh took {elapsed:.1f}s - the wall clock did not bound it"
    # The message must name the diagnosis, not just the number: an agent reading it has to be able to
    # tell "slow query" (retry smaller) from "parked on a modal" (a human must sign in).
    assert "modal" in str(excinfo.value).lower()


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
    assert executed
    assert json.loads(executed[0]) == {
        "refresh": {"type": "full", "objects": [{"database": "catalog-1", "table": "Orders"}]}
    }


def test_default_refresh_type_is_full() -> None:
    """A DAX-only shortcut must stay opt-in; data-affecting edits need the full default."""
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "1"])
    assert args.refresh_type == "full"


def _visual_refresh(
    monkeypatch,
    parked,
    *,
    progress: bool = False,
    timeout: float = 3.0,
    pid: int = 111,
    evidence_dir: Path | None = None,
    operator_pause: _operator_pause.OperatorPauseContext | None = None,
    cli_scratch: Path | None = None,
    initial_window=None,
):
    """Run the real refresh -> both wait branches -> detector -> acquisition callback chain."""
    _conn, released = parked
    window = {"value": initial_window or owned_dialog()}
    outcome = {}

    def state(pid, *, in_flight=False):
        if not in_flight:
            return _credential_modal.CredentialDetection()
        return _credential_modal.inspect_credential_modal(
            pid, lambda _pid: [window["value"], main_window()], operation_in_flight=True
        )

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", state)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.005)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_WALL_CLOCK_GRACE_SECONDS", 0)
    monkeypatch.setattr(refresh_pbip_model, "_start_refresh_progress_trace", lambda *_a, **_k: _FakeProgressMonitor())
    if cli_scratch is not None:
        monkeypatch.setattr(refresh_pbip_model, "_resolve_pid", lambda _pid: pid)
        monkeypatch.setattr(
            refresh_pbip_model, "cache_file", lambda _pid: operator_pause.model_dir / ".pbi" / "cache.abf"
        )
        monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda _pid: 1234)
        monkeypatch.setattr(refresh_pbip_model, "_identity_gate", lambda *_args: True)
        monkeypatch.setattr(refresh_pbip_model, "row_counts", lambda *_args: ([("control", 1)], True))
        monkeypatch.setattr(refresh_pbip_model, "REFRESH_TIMEOUT_SECONDS", timeout)

    def run():
        try:
            if cli_scratch is not None:
                outcome["result"] = refresh_pbip_model.main(
                    [
                        "--pid",
                        str(pid),
                        "--operator-pause-scratch",
                        str(cli_scratch),
                        "--no-save",
                        "--no-progress",
                    ]
                )
                return
            outcome["result"] = refresh(
                port=1234,
                tables=["Orders"],
                desktop_pid=pid,
                progress_enabled=progress,
                timeout_sec=timeout,
                absolute_timeout_sec=timeout,
                evidence_dir=evidence_dir,
                operator_pause=operator_pause,
            )
        except BaseException as exc:  # the assertion, not an unhandled thread warning, judges the result
            outcome["error"] = exc

    thread = threading.Thread(target=run, name="test-image-refresh", daemon=True)
    thread.start()
    return thread, released, outcome, window


@pytest.fixture
def retained_runtime(visual_runtime, monkeypatch):
    """Real retained filesystem lifecycle, existing synthetic GDI and acquisition-child boundary."""
    runtime = visual_runtime
    monkeypatch.delenv(_credential_modal.IMAGE_DIRECTORY_ENV)
    monkeypatch.setattr(
        _credential_modal.ctypes,
        "WinDLL",
        lambda name, **kwargs: (
            {"gdi32": runtime.api.gdi, "dwmapi": runtime.api.dwm}.get(name) or NATIVE_DLL(name, **kwargs)
        ),
    )
    # The separate canonical-run controls exercise Git; the acquisition Popen double owns subprocess here.
    monkeypatch.setattr(_operator_pause, "_ignored", lambda _path: None)
    context = _operator_pause.prepare_operator_pause(*make_run(runtime.root), os.getpid())
    runtime.api.pid = runtime.api.owner_pid = os.getpid()
    notices, cleanup = [], []
    acquired, cleaned = threading.Event(), threading.Event()
    original_print = _credential_modal.print

    def notice(message, **kwargs):
        original_print(message, **kwargs)
        if message.startswith("OPERATOR_PAUSE "):
            notices.append(json.loads(message.removeprefix("OPERATOR_PAUSE ")))
            acquired.set()
        if message.startswith("OPERATOR_PAUSE_CLEANUP "):
            cleanup.append(json.loads(message.removeprefix("OPERATOR_PAUSE_CLEANUP ")))
            cleaned.set()

    monkeypatch.setattr(_credential_modal, "print", notice)
    return SimpleNamespace(
        context=context,
        notices=notices,
        acquired=acquired,
        cleanup=cleanup,
        cleaned=cleaned,
        visual=runtime,
    )


def _run_retained(monkeypatch, parked, runtime, **kwargs):
    return _visual_refresh(
        monkeypatch,
        parked,
        pid=runtime.context.desktop_pid,
        operator_pause=runtime.context,
        **kwargs,
    )


@NATIVE
@pytest.mark.parametrize("progress", [False, True])
def test_retained_in_flight_success_cleans_only_this_invocations_pause(monkeypatch, parked, retained_runtime, progress):
    runtime = retained_runtime
    unrelated = publish(runtime.context)
    timers = []

    def unexpected_timer(*args, **_kwargs):
        timers.append(args)
        raise RuntimeError("retained expiry is not supported")

    monkeypatch.setattr(_credential_modal.threading, "Timer", unexpected_timer)
    thread, released, outcome, _window = _run_retained(monkeypatch, parked, runtime, progress=progress)
    try:
        assert runtime.acquired.wait(3), "retained in-flight acquisition never published"
        record = runtime.notices[0]
        assert (
            _operator_pause.load_operator_pause(
                runtime.context.run_scratch, record["pause_id"], record["image"]["sha256"]
            )
            == record
        )
        assert record["desktop"]["process_start"] == runtime.context.process_start
        assert not any(str(runtime.context.run_scratch) in json.dumps(value) for value in record.values())
        assert thread.is_alive(), "publication cannot end or classify the in-flight refresh"
        assert len(runtime.visual.children) == 1
        assert runtime.visual.children[0].argv[-2:] == ["prompt.png", runtime.context.process_start]
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True
    assert not timers, "retained evidence must have no expiry timer"
    assert runtime.cleaned.wait(3)
    assert runtime.cleanup == [
        {"pause_id": record["pause_id"], "sha256": record["image"]["sha256"], "status": "removed"}
    ]
    assert unrelated.image_path.is_file(), "successful refresh may not clean another pause or select newest"
    assert (
        _operator_pause.cleanup_operator_pause(
            runtime.context.run_scratch, unrelated.pause_id, unrelated.record["image"]["sha256"]
        )
        == "removed"
    )


@NATIVE
def test_retained_cli_reaches_actual_refresh_and_exact_model_binding(monkeypatch, parked, retained_runtime):
    runtime = retained_runtime
    thread, released, outcome, _window = _run_retained(
        monkeypatch,
        parked,
        runtime,
        cli_scratch=runtime.context.run_scratch,
    )
    try:
        assert runtime.acquired.wait(3), "the CLI did not reach the actual acquisition-backed refresh"
        record = runtime.notices[0]
        assert (
            record["model_path"] == runtime.context.model_dir.relative_to(runtime.context.run_scratch.parent).as_posix()
        )
        assert record["desktop"]["pid"] == str(runtime.context.desktop_pid)
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result") == 0, outcome
    assert runtime.cleaned.wait(3) and runtime.cleanup[-1]["status"] == "removed"


@NATIVE
@pytest.mark.parametrize("progress", [False, True])
def test_retained_unreadable_timeout_keeps_ready_pause_and_original_latch(
    monkeypatch, parked, retained_runtime, progress
):
    runtime = retained_runtime
    started = time.monotonic()
    thread, released, outcome, _window = _run_retained(monkeypatch, parked, runtime, progress=progress, timeout=0.75)
    try:
        assert runtime.acquired.wait(3), "positive retained publication was not exercised"
        thread.join(2)
        assert not thread.is_alive() and time.monotonic() - started >= 0.75
        assert isinstance(outcome.get("error"), _credential_modal.DialogFoundError)
        assert outcome["error"].finding.verdict == "DIALOG_UNREADABLE"
        record = runtime.notices[0]
        assert (
            _operator_pause.load_operator_pause(
                runtime.context.run_scratch, record["pause_id"], record["image"]["sha256"]
            )
            == record
        )
        assert runtime.cleanup == []
    finally:
        released.set()
        thread.join(3)


@NATIVE
def test_retained_worker_error_is_not_misread_as_success_cleanup(monkeypatch, parked, retained_runtime):
    runtime = retained_runtime
    _conn, released = parked

    def fail(_command):
        released.wait(5)
        raise RuntimeError("synthetic refresh failure")

    monkeypatch.setattr(_ParkedCommand, "ExecuteNonQuery", fail)
    thread, released, outcome, _window = _run_retained(monkeypatch, parked, runtime)
    try:
        assert runtime.acquired.wait(3)
    finally:
        released.set()
        thread.join(3)
    assert isinstance(outcome.get("error"), RuntimeError), outcome
    record = runtime.notices[0]
    assert (
        _operator_pause.load_operator_pause(runtime.context.run_scratch, record["pause_id"], record["image"]["sha256"])
        == record
    )
    assert runtime.cleanup == []


@NATIVE
@pytest.mark.timing
@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("outcome_kind", ["success", "timeout"])
@pytest.mark.parametrize("boundary", ["reserve", "metadata", "rename", "READY"])
def test_retained_slow_storage_cannot_hold_refresh_or_publish_after_close(
    monkeypatch,
    parked,
    retained_runtime,
    progress,
    outcome_kind,
    boundary,
):
    runtime = retained_runtime
    entered, finish = threading.Event(), threading.Event()
    original_reserve = _credential_modal.reserve_operator_pause
    original_open, original_rename = _operator_pause._open_file, _operator_pause._rename

    def stall():
        entered.set()
        assert finish.wait(5)

    def reserve(*args, **kwargs):
        if boundary == "reserve":
            stall()
        return original_reserve(*args, **kwargs)

    def open_file(path, **kwargs):
        stream = original_open(path, **kwargs)
        if kwargs.get("create") and path.name == {"metadata": "pause.json", "READY": "READY"}.get(boundary):
            stall()
        return stream

    def rename(source, target):
        original_rename(source, target)
        if boundary == "rename":
            stall()

    monkeypatch.setattr(_credential_modal, "reserve_operator_pause", reserve)
    monkeypatch.setattr(_operator_pause, "_open_file", open_file)
    monkeypatch.setattr(_operator_pause, "_rename", rename)
    thread, released, outcome, _window = _run_retained(
        monkeypatch,
        parked,
        runtime,
        progress=progress,
        timeout=0.45,
    )
    try:
        assert entered.wait(2), f"storage boundary {boundary} was never exercised"
        started = time.monotonic()
        if outcome_kind == "success":
            released.set()
        thread.join(1)
        assert not thread.is_alive() and time.monotonic() - started < 0.8, "optional storage held the refresh wait"
        if outcome_kind == "success":
            assert outcome.get("result", (False,))[0] is True, outcome
        else:
            assert isinstance(outcome.get("error"), _credential_modal.DialogFoundError), outcome
        assert not runtime.notices
    finally:
        finish.set()
        released.set()
        thread.join(3)
    until = time.monotonic() + 2
    while any(t.name == "window-image" and t.is_alive() for t in threading.enumerate()) and time.monotonic() < until:
        time.sleep(0.01)
    assert not runtime.notices, "late READY may not publish after the original deadline or close"
    assert not list(runtime.context.run_scratch.rglob("READY"))


@NATIVE
@pytest.mark.timing
def test_retained_cleanup_failure_is_loud_keeps_pause_and_never_changes_success(monkeypatch, parked, retained_runtime):
    runtime = retained_runtime
    entered, finish = threading.Event(), threading.Event()

    def delayed_cleanup(*_args):
        entered.set()
        assert finish.wait(5)
        return "cleanup_failed"

    monkeypatch.setattr(_credential_modal, "cleanup_operator_pause", delayed_cleanup)
    thread, released, outcome, _window = _run_retained(monkeypatch, parked, runtime)
    try:
        assert runtime.acquired.wait(3)
        started = time.monotonic()
        released.set()
        assert entered.wait(1)
        thread.join(0.5)
        assert not thread.is_alive() and time.monotonic() - started < 0.8, "cleanup is not a new refresh wait"
        assert outcome.get("result", (False,))[0] is True
        record = runtime.notices[0]
        assert (
            _operator_pause.load_operator_pause(
                runtime.context.run_scratch, record["pause_id"], record["image"]["sha256"]
            )
            == record
        )
    finally:
        finish.set()
        released.set()
        thread.join(3)
    assert runtime.cleaned.wait(2)
    assert runtime.cleanup[-1]["status"] == "cleanup_failed"
    assert outcome.get("result", (False,))[0] is True


@NATIVE
@pytest.mark.parametrize(
    "text",
    [
        ("Evaluating...",),
        ("Save changes?",),
        ("Please specify how to connect",),
        ("Permission is required to run this native database query",),
    ],
)
def test_retained_does_not_widen_eligibility_to_other_prompt_kinds(monkeypatch, parked, retained_runtime, text):
    runtime = retained_runtime
    thread, released, outcome, _window = _run_retained(
        monkeypatch,
        parked,
        runtime,
        initial_window=owned_dialog(text),
    )
    try:
        time.sleep(0.06)
    finally:
        released.set()
        thread.join(3)
    assert not runtime.visual.children and not runtime.notices
    assert not (runtime.context.run_scratch / "operator-pauses").exists()


@NATIVE
def test_retained_t0_is_unchanged_and_never_acquires(monkeypatch, parked, retained_runtime):
    runtime = retained_runtime
    state = _credential_modal.inspect_credential_modal(
        runtime.context.desktop_pid,
        lambda _pid: [owned_dialog(), main_window()],
    )
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda *_a, **_k: state)
    with pytest.raises(_credential_modal.DialogFoundError) as caught:
        refresh(1234, None, desktop_pid=runtime.context.desktop_pid, operator_pause=runtime.context)
    assert caught.value.finding.verdict == "DIALOG_UNREADABLE"
    assert runtime.visual.children == [] and runtime.notices == []
    assert not (runtime.context.run_scratch / "operator-pauses").exists()


@NATIVE
@pytest.mark.parametrize("configuration", ["explicit", "ambient", "empty-ambient"])
def test_retained_rejects_ephemeral_conflicts_before_operation(monkeypatch, run_paths, configuration):
    context = _operator_pause.prepare_operator_pause(*run_paths, os.getpid())
    directory = None
    if configuration == "explicit":
        directory = run_paths[0]
    else:
        monkeypatch.setenv(
            _credential_modal.IMAGE_DIRECTORY_ENV, "" if configuration == "empty-ambient" else str(run_paths[0])
        )
    with pytest.raises(_operator_pause.OperatorPauseUnavailable):
        refresh(1234, None, desktop_pid=os.getpid(), evidence_dir=directory, operator_pause=context)
    with pytest.raises(_operator_pause.OperatorPauseUnavailable):
        _credential_modal.ModalVisualEvidence(directory, operator_pause=context)
    assert not (run_paths[0] / "operator-pauses").exists()
    if configuration == "explicit":
        with pytest.raises(SystemExit) as caught:
            refresh_pbip_model._build_arg_parser().parse_args(
                [
                    "--evidence-dir",
                    str(directory),
                    "--operator-pause-scratch",
                    str(run_paths[0]),
                ]
            )
        assert caught.value.code == 2
    else:
        monkeypatch.setattr(refresh_pbip_model, "_resolve_pid", lambda _pid: pytest.fail("conflict reached Desktop"))
        assert refresh_pbip_model.main(["--operator-pause-scratch", str(run_paths[0])]) == 2


@pytest.mark.parametrize("progress", [False, True])
def test_visual_notice_is_once_flushed_readable_in_flight_and_deleted_on_success(
    monkeypatch, parked, visual_runtime, progress
) -> None:
    """The positive oracle is an independent PNG decoder and a still-parked real production wait."""
    image_decoder = pytest.importorskip("PIL.Image", reason="independent PNG decoder is a repo dev extra")
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked, progress=progress)
    try:
        assert visual_runtime.noticed.wait(2), "the in-flight callback never published evidence"
        payload, kwargs = visual_runtime.records[0]
        assert payload["status"] == "ACQUIRED"
        assert payload["schema"] == "pbip.window-image.v1"
        assert payload["desktop_pid"] == "111"
        assert payload["main_hwnd"] == payload["owner_hwnd"] == str(MAIN_HWND)
        assert payload["dialog_hwnd"] == str(DIALOG_HWND)
        assert payload["ownership_checks"] == {"before": True, "after_render": True, "after_write": True}
        assert payload["dimensions"] == {"width": "2", "height": "2"}
        assert payload["capture_success"] is True
        assert payload["cleanup_state"] == "pending"
        assert payload["classification_provenance"] is None
        start = datetime.fromisoformat(payload["captured_at_utc"])
        end = datetime.fromisoformat(payload["expires_at_utc"])
        assert (end - start).total_seconds() == 60
        assert kwargs["flush"] is True
        path = visual_runtime.root / payload["path"]
        assert payload["image_basename"] == payload["path"]
        assert payload["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert thread.is_alive(), "the image must be inspectable BEFORE the refresh command returns"
        with image_decoder.open(path) as image:
            assert image.size == (2, 2)
            assert [image.getpixel((x, y)) for y in range(2) for x in range(2)] == (
                [(0, 0, 0)] * 2 + [(255, 255, 255)] * 2
            )
        time.sleep(0.04)  # several production polls must see the SAME HWND without recapturing it
        assert len(visual_runtime.children) == 1, "repeated polls must not launch repeated acquisition"
        assert visual_runtime.api.captures == [(DIALOG_HWND, 31, 2)], "capture only the detected HWND"
        assert len(visual_runtime.records) == 1, "exactly one flushed acquisition notice per HWND"
    finally:
        released.set()
        thread.join(3)
    assert not thread.is_alive()
    assert outcome.get("result", (False,))[0] is True, f"image evidence changed the refresh verdict: {outcome}"
    assert not list(visual_runtime.root.glob("_ui-image-*.png")), "normal exit must remove every image"
    cleanup = visual_runtime.records[-1][0]
    assert cleanup["status"] == "CLEANED"
    assert cleanup["capture_id"] == payload["capture_id"]
    assert cleanup["sha256"] == payload["sha256"]
    assert cleanup["cleanup_state"] == "removed_on_exit"


@pytest.mark.parametrize(
    "change,phase,status,capture_count,write_count",
    [
        (("pid", 222), "before", "TARGET_CHANGED", 0, 0),
        (("owner_pid", 222), "before", "TARGET_CHANGED", 0, 0),
        (("owner", MAIN_HWND + 7), "before", "TARGET_CHANGED", 0, 0),
        (("visible", False), "before", "TARGET_CHANGED", 0, 0),
        (("thread", 0), "before", "TARGET_CHANGED", 0, 0),
        (("owner_enabled", True), "before", "TARGET_CHANGED", 0, 0),
        (("pid", 222), "after_render", "TARGET_CHANGED", 1, 0),
        (("owner_pid", 222), "after_render", "TARGET_CHANGED", 1, 0),
        (("owner", MAIN_HWND + 7), "after_render", "TARGET_CHANGED", 1, 0),
        (("visible", False), "after_render", "TARGET_CHANGED", 1, 0),
        (("thread", 0), "after_render", "TARGET_CHANGED", 1, 0),
        (("extent", (3, 2)), "after_render", "TARGET_CHANGED", 1, 0),
        (("pid", 222), "after_write", "TARGET_CHANGED", 1, 1),
        (("render_mode", "false"), "before", "CAPTURE_FAILED", 1, 0),
        (("render_mode", "blank"), "before", "BLANK_OR_INCOMPLETE", 1, 0),
        (("render_mode", "partial"), "before", "BLANK_OR_INCOMPLETE", 1, 0),
        (("render_mode", "noop"), "before", "BLANK_OR_INCOMPLETE", 1, 0),
        (("cleanup_ok", False), "before", "CAPTURE_CLEANUP_FAILED", 1, 0),
        (("extent", (0, 0)), "before", "BLANK_OR_INCOMPLETE", 0, 0),
        (("extent", (4001, 1000)), "before", "BLANK_OR_INCOMPLETE", 0, 0),
    ],
)
def test_visual_failure_never_changes_the_worker_result(
    monkeypatch, parked, visual_runtime, change, phase, status, capture_count, write_count
) -> None:
    """Identity is checked on both sides of acquisition; no rejected target may reach a later phase."""

    def alter():
        setattr(visual_runtime.api, *change)

    if phase == "before":
        alter()
    else:
        setattr(visual_runtime.api, phase, alter)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2), "acquisition failed silently"
        assert visual_runtime.records[0][0]["status"] == status
        assert len(visual_runtime.api.captures) == capture_count, "pre-check must reject BEFORE PrintWindow"
        assert len(visual_runtime.api.writes) == write_count, "post-check must reject BEFORE writing pixels"
        assert all(call[0] == DIALOG_HWND for call in visual_runtime.api.captures), "never capture an alternate HWND"
        assert not list(visual_runtime.root.glob("_ui-image-*.png")), "failed acquisition left a private image"
        assert thread.is_alive(), "an image failure must not abort a healthy in-flight refresh"
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True, f"non-semantic evidence failure changed the result: {outcome}"


def test_visual_write_failure_is_detail_free_and_does_not_change_the_worker(
    monkeypatch, parked, visual_runtime, capsys
) -> None:
    def fail_write(path, data, **_kwargs):
        path.write_bytes(data[:20])  # a partial output must be cleaned as well
        raise OSError("PRIVATE_DIALOG_TEXT authentication 10054 C:\\private\\source")

    monkeypatch.setattr(_credential_modal, "_write_private_image", fail_write)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
        assert visual_runtime.records[0][0]["status"] == "WRITE_FAILED"
        assert not list(visual_runtime.root.glob("_ui-image-*.png"))
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True
    output = capsys.readouterr()
    assert "PRIVATE_DIALOG_TEXT" not in output.out + output.err
    assert "authentication" not in output.out + output.err
    assert "C:\\private" not in output.out + output.err


def test_visual_unsupported_platform_is_loud_nonsemantic_once(monkeypatch, parked, visual_runtime) -> None:
    monkeypatch.setattr(_credential_modal.sys, "platform", "linux")
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
        time.sleep(0.03)
        assert [record[0]["status"] for record in visual_runtime.records] == ["UNSUPPORTED"]
        assert visual_runtime.children == [], "unsupported platforms must not invoke any capture"
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True


def test_visual_expiry_deletes_without_waiting_for_refresh_exit(monkeypatch, parked, visual_runtime) -> None:
    monkeypatch.setattr(_credential_modal, "IMAGE_LIFETIME_SECONDS", 0.08)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
        path = visual_runtime.root / visual_runtime.records[0][0]["path"]
        assert path.is_file()
        deadline = time.monotonic() + 2
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not path.exists(), "expiry must delete the image while refresh remains running"
        assert thread.is_alive(), "image expiry cannot end a healthy refresh"
        assert len(visual_runtime.children) == 1, "expiry must not rearm the same HWND"
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True


def test_visual_inspector_can_delete_early(monkeypatch, parked, visual_runtime) -> None:
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
        path = visual_runtime.root / visual_runtime.records[0][0]["path"]
        visual_runtime.noticed.clear()
        path.unlink()
        assert visual_runtime.noticed.wait(2), "the observer must acknowledge external deletion while refresh is alive"
        assert thread.is_alive()
        assert not path.exists()
        assert len(visual_runtime.children) == 1
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True
    assert [record[0]["status"] for record in visual_runtime.records] == ["ACQUIRED", "CLEANED"]
    assert visual_runtime.records[-1][0]["cleanup_state"] == "removed_externally"


def test_visual_cleanup_failure_is_loud_preserves_result_and_expiry_retries(
    monkeypatch, parked, visual_runtime, capsys
) -> None:
    monkeypatch.setattr(_credential_modal, "IMAGE_LIFETIME_SECONDS", 0.15)
    unlink = Path.unlink
    attempts = []

    def locked_once(path, **kwargs):
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError("PRIVATE_OS_DETAIL authentication")
        return unlink(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_once)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True, "cleanup must never replace the original result"
    assert "CLEANUP_FAILED" in [record[0]["status"] for record in visual_runtime.records]
    deadline = time.monotonic() + 2
    while list(visual_runtime.root.glob("_ui-image-*.png")) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not list(visual_runtime.root.glob("_ui-image-*.png")), "expiry must retry failed exit cleanup"
    assert len(attempts) >= 2
    output = capsys.readouterr()
    assert "PRIVATE_OS_DETAIL" not in output.out + output.err


@pytest.mark.parametrize("progress", [False, True])
def test_visual_capture_keeps_unreadable_deadline_and_never_becomes_a_verdict(
    monkeypatch, parked, visual_runtime, progress
) -> None:
    started = time.monotonic()
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked, progress=progress, timeout=0.25)
    try:
        assert visual_runtime.noticed.wait(2)
        thread.join(2)
        assert not thread.is_alive()
        assert isinstance(outcome.get("error"), _credential_modal.DialogFoundError), (
            f"acquisition is NOT a semantic verdict: {outcome}"
        )
        assert outcome["error"].finding.verdict == "DIALOG_UNREADABLE"
        assert time.monotonic() - started >= 0.25, "capture may not shorten the original wait"
        assert len(visual_runtime.children) == 1
        assert not list(visual_runtime.root.glob("_ui-image-*.png")), "error exit must remove the image"
    finally:
        released.set()
        thread.join(3)


def test_visual_new_hwnd_is_independently_captured_once(monkeypatch, parked, visual_runtime) -> None:
    thread, released, outcome, window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
        visual_runtime.noticed.clear()
        window["value"] = owned_dialog(hwnd=DIALOG_HWND + 9)
        assert visual_runtime.noticed.wait(2), "a new HWND needs its own guarded acquisition"
        time.sleep(0.03)
        assert [call[0] for call in visual_runtime.api.captures] == [DIALOG_HWND, DIALOG_HWND + 9]
        assert len(visual_runtime.children) == 2
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True
    assert not list(visual_runtime.root.glob("_ui-image-*.png"))


def test_visual_does_not_mask_later_positive_semantic_text(monkeypatch, parked, visual_runtime) -> None:
    thread, released, outcome, window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
        window["value"] = owned_dialog(("Please specify how to connect",))
        thread.join(2)
        assert not thread.is_alive()
        assert isinstance(outcome.get("error"), _credential_modal.CredentialMissingError)
        assert not list(visual_runtime.root.glob("_ui-image-*.png"))
    finally:
        released.set()
        thread.join(3)


def test_visual_notice_and_path_cannot_trip_the_real_parent_text_classifier(
    monkeypatch, parked, visual_runtime
) -> None:
    source = Path(__file__).resolve()
    repo_scripts = next(
        (
            parent / "scripts"
            for parent in source.parents
            if source == parent.joinpath(".github", "skills", "pbip-model-refresh", "tests", source.name)
        ),
        None,
    )
    if repo_scripts is None or not (repo_scripts / "probe_live_source.py").is_file():
        pytest.skip("parent transcript classifier is host-repo-only; portable skill has no such consumer")
    monkeypatch.syspath_prepend(str(repo_scripts))
    import probe_live_source  # pylint: disable=import-outside-toplevel

    private_cwd = visual_runtime.root / "authentication-10054-oauth"
    private_cwd.mkdir()
    monkeypatch.chdir(private_cwd)
    monkeypatch.setattr(_credential_modal.uuid, "uuid4", lambda: SimpleNamespace(hex="1005403abcdef" + "0" * 20))
    visual_runtime.api.pid = visual_runtime.api.owner_pid = 10054
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked, pid=10054)
    try:
        assert visual_runtime.noticed.wait(2)
        payload = visual_runtime.records[0][0]
        assert payload["status"] == "ACQUIRED"
        assert payload["desktop_pid"] == "10054", "escaping must preserve the decoded identifier"
        line = visual_runtime.wires[0]
        assert not any(marker in line.lower() for marker in probe_live_source.CREDENTIAL_MARKERS)
        assert probe_live_source._classify_failure(line, False)[0] == "ERROR"
        assert "authentication-10054-oauth" not in line
        assert str(private_cwd) not in line
        assert "/" not in payload["path"] and "\\" not in payload["path"]
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True
    assert not list(private_cwd.glob("_ui-image-*.png"))


def test_visual_requires_explicit_scratch_and_never_falls_back_to_cwd(monkeypatch, parked, visual_runtime) -> None:
    monkeypatch.delenv(_credential_modal.IMAGE_DIRECTORY_ENV)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
        record = visual_runtime.records[0][0]
        assert record["status"] == "EVIDENCE_DIR_REQUIRED"
        assert record["capture_success"] is False
        assert record["path"] is None
        assert record["sha256"] is None
        assert record["cleanup_state"] == "not_created"
        assert visual_runtime.children == []
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True
    assert not list(visual_runtime.root.glob("_ui-image-*.png"))


def test_visual_explicit_api_scratch_overrides_environment(monkeypatch, parked, visual_runtime) -> None:
    explicit = visual_runtime.root / "scratch"
    explicit.mkdir()
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked, evidence_dir=explicit)
    try:
        assert visual_runtime.noticed.wait(2)
        record = visual_runtime.records[0][0]
        assert record["status"] == "ACQUIRED"
        assert (explicit / record["path"]).is_file()
        assert not (visual_runtime.root / record["path"]).exists()
        assert str(explicit) not in visual_runtime.wires[0], "only a relative locator belongs in the record"
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True
    assert not list(explicit.glob("_ui-image-*.png"))


@pytest.mark.parametrize("bad_field", ["hash", "dimensions", "boolean_check", "missing_check"])
def test_visual_rejects_incomplete_or_unbound_child_metadata(monkeypatch, parked, visual_runtime, bad_field) -> None:
    acquire = _credential_modal._capture_exact_image

    def damaged(*args, **kwargs):
        result = acquire(*args, **kwargs)
        if bad_field == "hash":
            result["sha256"] = "f" * 64
        elif bad_field == "dimensions":
            result["dimensions"]["width"] = 1
        elif bad_field == "boolean_check":
            result["ownership_checks"]["before"] = 1
        else:
            result["ownership_checks"].pop("after_write")
        return result

    monkeypatch.setattr(_credential_modal, "_capture_exact_image", damaged)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
        record = visual_runtime.records[0][0]
        assert record["status"] == "CAPTURE_FAILED", "unbound metadata cannot publish successful acquisition"
        assert record["capture_success"] is False
        assert not list(visual_runtime.root.glob("_ui-image-*.png"))
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True


def test_visual_cli_scratch_is_forwarded_to_the_refresh_api(monkeypatch, tmp_path) -> None:
    seen = []

    def invoked(_port, _tables, _timeout, *, evidence_dir=None):
        seen.append(evidence_dir)
        return True, "synthetic refresh"

    args = refresh_pbip_model._build_arg_parser().parse_args(["--no-save", "--evidence-dir", str(tmp_path)])
    monkeypatch.setattr(refresh_pbip_model, "refresh", invoked)
    refresh_pbip_model._refresh_and_save(111, 1234, None, args)
    assert seen == [tmp_path], "the CLI flag must reach the production refresh API, not just its parser"


def test_visual_parent_classifier_is_optional_in_a_shallow_copy(monkeypatch, parked, visual_runtime) -> None:
    """A portable copy must skip host-only coverage without assuming four parent directories."""
    test = test_visual_notice_and_path_cannot_trip_the_real_parent_text_classifier
    shallow = Path(Path.cwd().anchor) / "portable" / "tests" / "test_refresh_wall_clock.py"
    monkeypatch.setitem(test.__globals__, "__file__", str(shallow))
    with pytest.raises(pytest.skip.Exception, match="parent transcript classifier is host-repo-only"):
        test(monkeypatch, parked, visual_runtime)
    assert visual_runtime.children == []


def test_visual_acquisition_timeout_kills_only_its_child_and_preserves_refresh(
    monkeypatch, parked, visual_runtime
) -> None:
    """Inject the child timeout after startup; the separate startup control owns the real watchdog."""
    child_type = _credential_modal.subprocess.Popen

    def never_returns(child, timeout):
        assert 0 < timeout <= _credential_modal.IMAGE_CAPTURE_SECONDS
        assert not child.finished.is_set()
        raise subprocess.TimeoutExpired(child.argv, timeout)

    monkeypatch.setattr(child_type, "communicate", never_returns)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2), "the acquisition needs its own bounded failure notice"
        assert visual_runtime.records[0][0]["status"] == "CAPTURE_TIMEOUT"
        assert thread.is_alive(), "acquisition timeout must not terminate the refresh"
        assert len(visual_runtime.children) == 1
        assert visual_runtime.children[0].killed
        assert not list(visual_runtime.root.glob("_ui-image-*.png"))
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True


def test_visual_capture_in_flight_is_cancelled_when_worker_finishes(monkeypatch, parked, visual_runtime) -> None:
    entered = threading.Event()
    child_type = _credential_modal.subprocess.Popen

    def wait_for_exit(child, timeout):
        entered.set()
        assert child.finished.wait(timeout), "wait teardown must stop its still-running acquisition child"
        return b"CAPTURE_FAILED", None

    monkeypatch.setattr(child_type, "communicate", wait_for_exit)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert entered.wait(2)
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True
    assert visual_runtime.children[0].killed
    assert visual_runtime.records == [], "a cancelled acquisition must not publish after refresh exit"
    assert not list(visual_runtime.root.glob("_ui-image-*.png"))


def test_visual_cleanup_preserves_an_exception_from_the_refresh_worker(monkeypatch, parked, visual_runtime) -> None:
    failure = ValueError("synthetic worker failure")

    def fail_after_release(command):
        command._released.wait(3)
        raise failure

    monkeypatch.setattr(_ParkedCommand, "ExecuteNonQuery", fail_after_release)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("error") is failure, "image teardown cannot replace the worker's original exception"
    assert not list(visual_runtime.root.glob("_ui-image-*.png"))


@pytest.mark.parametrize("mode", ["legacy", "progress", "observation"])
@pytest.mark.parametrize("completed_at", [0.5, 1.0, 2.0], ids=["before-deadline", "at-deadline", "after-deadline"])
@pytest.mark.parametrize("unreadable", [False, True], ids=["healthy", "unreadable"])
def test_wait_completion_must_be_observed_before_the_original_deadline(
    monkeypatch, mode, completed_at, unreadable
) -> None:
    """A delayed observation/join must not launder a late completion into success in either branch."""
    clock = _FakeClock()
    monkeypatch.setattr(refresh_pbip_model, "time", clock)
    monkeypatch.setattr(_credential_modal, "time", clock)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.1)
    state = _credential_modal.CredentialDetection()
    if unreadable:
        state = _credential_modal.inspect_credential_modal(111, lambda _pid: [owned_dialog(), main_window()])

    class Worker:
        def is_alive(self):
            return clock.now < completed_at

        def join(self, timeout):
            clock.now += min(timeout, 0.1)

    def delayed_inspection(_pid):
        clock.now = completed_at
        return state

    monkeypatch.setattr(refresh_pbip_model, "_in_flight_credential_state", delayed_inspection)
    arguments = dict(
        desktop_pid=111,
        source_hint=None,
        initial_state=_credential_modal.CredentialDetection(),
        total_timeout=1.0,
        progress_monitor=_FakeProgressMonitor() if mode == "progress" else None,
        observation_mode=mode == "observation",
    )
    if unreadable and completed_at >= 1.0:
        with pytest.raises(_credential_modal.DialogFoundError) as error:
            refresh_pbip_model._join_refresh_worker(Worker(), **arguments)
        assert error.value.finding.verdict == "DIALOG_UNREADABLE"
    else:
        completed = refresh_pbip_model._join_refresh_worker(Worker(), **arguments)
        assert completed is (completed_at < 1.0), "completion at/after the original deadline is not success"
    assert clock.now == completed_at


@pytest.mark.parametrize("progress", [False, True])
def test_evidence_worker_construction_cannot_restart_the_refresh_deadline(monkeypatch, progress) -> None:
    clock = _FakeClock()
    evidence_type = _credential_modal.ModalVisualEvidence
    monkeypatch.setattr(refresh_pbip_model, "time", clock)
    monkeypatch.setattr(_credential_modal, "time", clock)

    def delayed_construction(*args, **kwargs):
        clock.now = 2.0
        return evidence_type(*args, **kwargs)

    monkeypatch.setattr(refresh_pbip_model, "ModalVisualEvidence", delayed_construction)
    worker = SimpleNamespace(is_alive=lambda: clock.now < 1.5)
    completed = refresh_pbip_model._join_refresh_worker(
        worker,
        desktop_pid=111,
        source_hint=None,
        initial_state=_credential_modal.CredentialDetection(),
        total_timeout=1.0,
        progress_monitor=_FakeProgressMonitor() if progress else None,
    )
    assert completed is False, "background-worker construction must consume, not reset, the original budget"


def test_no_pid_join_cannot_accept_a_completion_after_its_deadline(monkeypatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(refresh_pbip_model, "time", clock)

    def delayed_join(_timeout):
        clock.now = 2.0

    worker = SimpleNamespace(is_alive=lambda: clock.now < 1.5, join=delayed_join)
    completed = refresh_pbip_model._join_refresh_worker(
        worker,
        desktop_pid=None,
        source_hint=None,
        initial_state=None,
        total_timeout=1.0,
        progress_monitor=None,
    )
    assert completed is False, "the uninspected branch must enforce the same original deadline"


@pytest.mark.parametrize("completed", [False, True], ids=["missed-deadline", "timely-completion"])
def test_refresh_honors_the_wait_verdict_even_if_the_worker_finishes_during_teardown(
    monkeypatch, parked, completed
) -> None:
    _conn, released = parked

    def finish_during_teardown(worker, **_kwargs):
        released.set()
        worker.join(2)
        assert not worker.is_alive(), "control requires completion after the wait made its decision"
        return completed

    monkeypatch.setattr(refresh_pbip_model, "_wait_refresh_worker", finish_during_teardown)
    if completed:
        assert refresh(port=1234, tables=["Orders"], progress_enabled=False)[0] is True
    else:
        with pytest.raises(TimeoutError, match="refresh did not return within"):
            refresh(port=1234, tables=["Orders"], progress_enabled=False)


@pytest.mark.parametrize("progress", [False, True])
@pytest.mark.parametrize("finishes", [False, True], ids=["deadline", "healthy-completion"])
def test_delayed_observer_never_blocks_refresh_wait_or_teardown(monkeypatch, parked, progress, finishes) -> None:
    entered, resume, observed = threading.Event(), threading.Event(), threading.Event()
    seen = []

    def delayed_observation(_self, pid, state, **_kwargs):
        seen.append((pid, state))
        entered.set()
        resume.wait(5)
        observed.set()

    monkeypatch.setattr(_credential_modal.ModalVisualEvidence, "observe", delayed_observation)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked, progress=progress, timeout=0.5)
    try:
        assert entered.wait(2), "the observer never received the queued target"
        if finishes:
            released.set()
        thread.join(2)
        assert not thread.is_alive(), "the refresh wait/teardown blocked behind evidence observation"
        assert not observed.is_set(), "control requires the evidence observer still to be delayed"
        assert seen[0][0] == 111 and seen[0][1].dialog.window.hwnd == DIALOG_HWND
        if finishes:
            assert outcome.get("result", (False,))[0] is True, "optional evidence must not reject a timely refresh"
        else:
            assert isinstance(outcome.get("error"), _credential_modal.DialogFoundError), outcome
            assert outcome["error"].finding.verdict == "DIALOG_UNREADABLE"
    finally:
        released.set()
        resume.set()
        thread.join(3)
        assert observed.wait(2), "release the background control before undoing its monkeypatches"


@pytest.mark.parametrize("phase", ["directory", "lease", "process"])
def test_delayed_evidence_startup_cannot_hold_the_deadline_or_publish_after_exit(
    monkeypatch, parked, visual_runtime, phase
) -> None:
    entered, resume, observed = threading.Event(), threading.Event(), threading.Event()
    target, name = {
        "directory": (_credential_modal, "_evidence_directory"),
        "lease": (_credential_modal, "_open_private_image"),
        "process": (_credential_modal.subprocess, "Popen"),
    }[phase]
    original = getattr(target, name)
    observe = _credential_modal.ModalVisualEvidence.observe

    def delayed_startup(*args, **kwargs):
        entered.set()
        resume.wait(5)
        return original(*args, **kwargs)

    def observe_until_clean(self, *args, **kwargs):
        try:
            return observe(self, *args, **kwargs)
        finally:
            observed.set()

    monkeypatch.setattr(target, name, delayed_startup)
    monkeypatch.setattr(_credential_modal.ModalVisualEvidence, "observe", observe_until_clean)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked, timeout=0.25)
    try:
        assert entered.wait(2), "the startup control never reached the requested boundary"
        thread.join(2)
        assert not thread.is_alive(), "evidence startup held the refresh deadline"
        assert isinstance(outcome.get("error"), _credential_modal.DialogFoundError), outcome
        assert outcome["error"].finding.verdict == "DIALOG_UNREADABLE"
        assert not observed.is_set(), "control requires startup still to be delayed at refresh exit"
    finally:
        released.set()
        resume.set()
        thread.join(3)
        assert observed.wait(2)
    assert visual_runtime.api.captures == [], "a startup returning after exit must never acquire pixels"
    assert all(child.killed for child in visual_runtime.children), "even a late process handle must be reclaimed"
    assert not list(visual_runtime.root.glob("_ui-image-*.png"))
    assert all(record[0]["status"] != "ACQUIRED" for record in visual_runtime.records)


def test_process_startup_uses_the_acquisition_budget_without_stopping_refresh(
    monkeypatch, parked, visual_runtime
) -> None:
    entered, resume, observed = threading.Event(), threading.Event(), threading.Event()
    popen = _credential_modal.subprocess.Popen
    observe = _credential_modal.ModalVisualEvidence.observe
    monkeypatch.setattr(_credential_modal, "IMAGE_CAPTURE_SECONDS", 0.1)

    def delayed_process(*args, **kwargs):
        entered.set()
        resume.wait(5)
        return popen(*args, **kwargs)

    def observe_until_clean(self, *args, **kwargs):
        try:
            return observe(self, *args, **kwargs)
        finally:
            observed.set()

    monkeypatch.setattr(_credential_modal.subprocess, "Popen", delayed_process)
    monkeypatch.setattr(_credential_modal.ModalVisualEvidence, "observe", observe_until_clean)
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert entered.wait(2)
        assert visual_runtime.noticed.wait(2), "startup must be inside the acquisition watchdog, not before it"
        assert visual_runtime.records[0][0]["status"] == "CAPTURE_TIMEOUT"
        assert thread.is_alive(), "an acquisition timeout must not end a healthy refresh"
        assert not observed.is_set() and not list(visual_runtime.root.glob("_ui-image-*.png"))
        resume.set()
        assert observed.wait(2)
        assert len(visual_runtime.children) == 1 and visual_runtime.children[0].killed
        assert visual_runtime.api.captures == []
    finally:
        resume.set()
        released.set()
        thread.join(3)
        assert observed.wait(2)
    assert outcome.get("result", (False,))[0] is True
    assert not list(visual_runtime.root.glob("_ui-image-*.png"))


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


def test_amo_trace_import_failure_keeps_the_absolute_backstop(monkeypatch, capsys) -> None:
    """AMO trace loss removes progress evidence, not the long timeout that replaced the 300s ceiling."""
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
        raise ImportError("No module named Microsoft.AnalysisServices")

    monkeypatch.setattr(refresh_pbip_model, "_start_refresh_progress_trace", trace_denied)
    monkeypatch.setattr(refresh_pbip_model, "_load_adomd", lambda: lambda _dsn: _Conn())
    monkeypatch.setattr(refresh_pbip_model, "_catalog_id", lambda _conn: "catalog-1")

    ok, message = refresh(port=1234, tables=["Orders"], progress_enabled=True, absolute_timeout_sec=17.2)

    assert ok is True
    assert "Orders" in message
    assert executed and executed[0][1] == 17
    assert executed[0][1] != REFRESH_TIMEOUT_SECONDS
    captured = capsys.readouterr()
    assert "[progress] unavailable" in captured.err
    assert "Row counts and the liveness warning are OFF" in captured.err
    assert "you cannot tell a slow refresh from a stuck one" in captured.err
    assert "The 17s absolute backstop still applies" in captured.err
    assert "restore the AMO package (Microsoft.AnalysisServices.NetCore.retail.amd64)" in captured.err
    assert "operator refresh strategy" in captured.err
    assert "legacy" not in captured.err
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
