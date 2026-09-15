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
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import _credential_modal
import refresh_pbip_model
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


def _visual_refresh(monkeypatch, parked, *, progress: bool = False, timeout: float = 3.0):
    """Run the real refresh -> both wait branches -> detector -> acquisition callback chain."""
    _conn, released = parked
    window = {"value": owned_dialog()}
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

    def run():
        try:
            outcome["result"] = refresh(
                port=1234,
                tables=["Orders"],
                desktop_pid=111,
                progress_enabled=progress,
                timeout_sec=timeout,
                absolute_timeout_sec=timeout,
            )
        except BaseException as exc:  # the assertion, not an unhandled thread warning, judges the result
            outcome["error"] = exc

    thread = threading.Thread(target=run, name="test-image-refresh", daemon=True)
    thread.start()
    return thread, released, outcome, window


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
        assert kwargs["flush"] is True
        path = visual_runtime.root / payload["path"]
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
    def fail_write(path, data):
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
        path.unlink()
        time.sleep(0.03)
        assert not path.exists()
        assert len(visual_runtime.children) == 1
    finally:
        released.set()
        thread.join(3)
    assert outcome.get("result", (False,))[0] is True
    assert all(record[0]["status"] == "ACQUIRED" for record in visual_runtime.records)


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


@pytest.mark.timing
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
    thread, released, outcome, _window = _visual_refresh(monkeypatch, parked)
    try:
        assert visual_runtime.noticed.wait(2)
        payload = visual_runtime.records[0][0]
        assert payload["status"] == "ACQUIRED"
        line = "LOCAL_IMAGE " + json.dumps(payload)
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
    child_type = _credential_modal.subprocess.Popen
    monkeypatch.setattr(_credential_modal, "IMAGE_CAPTURE_SECONDS", 0.02)

    def never_returns(child, timeout):
        assert not child.finished.wait(timeout)
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
