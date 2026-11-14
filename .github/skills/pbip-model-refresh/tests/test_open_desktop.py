"""`open_desktop.py`: find the installed Desktop, start it, print its PID, wait for exact-PID readiness.

Nothing here starts Power BI Desktop or loads a CLR: the start call, the bridge helpers and the
pythonnet/clr modules are all fakes. The one subprocess test runs `--check` against a dummy exe.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
import open_desktop  # noqa: E402  # pylint: disable=wrong-import-position,import-error
import _desktop_details as details  # noqa: E402  # pylint: disable=wrong-import-position,import-error
import _desktop_observer as observer  # noqa: E402  # pylint: disable=wrong-import-position,import-error
import probe_desktop_query as pdq  # noqa: E402  # pylint: disable=wrong-import-position,import-error

PID = 4242
NOTICE = "OPEN_DESKTOP: PBI_DESKTOP_PATH is set but its file does not exist; ignored"
REAL_OPEN_ELSEWHERE = open_desktop._open_elsewhere  # pylint: disable=protected-access


def _exe(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / "PBIDesktop.exe"
    exe.write_bytes(b"")
    return exe


@pytest.fixture(name="installs")
def _installs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A pin, a Store install and a classic install, each switchable; nothing stored between runs."""
    pin = _exe(tmp_path / "pinned")
    store = tmp_path / "WindowsApps" / "Microsoft.MicrosoftPowerBIDesktop_2.160_x64"
    appx = _exe(store / "bin")
    program_files = tmp_path / "Program Files"
    classic = _exe(program_files / "Microsoft Power BI Desktop" / "bin")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("PBI_DESKTOP_PATH", raising=False)
    monkeypatch.setattr(open_desktop, "_store_location", lambda: store)
    return types.SimpleNamespace(pin=pin, store=store, appx=appx, classic=classic)


def test_lookup_order_is_pin_then_store_then_classic(monkeypatch, installs) -> None:
    monkeypatch.setenv("PBI_DESKTOP_PATH", str(installs.pin))
    assert open_desktop.find_desktop() == (installs.pin, "pin")

    monkeypatch.delenv("PBI_DESKTOP_PATH")
    assert open_desktop.find_desktop() == (installs.appx, "appx")

    installs.appx.unlink()
    assert open_desktop.find_desktop() == (installs.classic, "classic")

    monkeypatch.setattr(open_desktop, "_store_location", lambda: None)
    installs.classic.unlink()
    assert open_desktop.find_desktop() is None


def test_a_dead_pin_is_ignored_with_a_path_free_notice(monkeypatch, capsys, installs, tmp_path) -> None:
    """Every Desktop update deletes the versioned folder a pin names; that must not stop a launch."""
    dead = tmp_path / "Microsoft.MicrosoftPowerBIDesktop_2.159_x64" / "bin" / "PBIDesktop.exe"
    monkeypatch.setenv("PBI_DESKTOP_PATH", str(dead))

    assert open_desktop.find_desktop() == (installs.appx, "appx")
    out = capsys.readouterr().out
    assert out == f"{NOTICE}\n"
    assert str(tmp_path) not in out


@pytest.mark.parametrize("name", ["model.txt", "missing.pbip"])
def test_a_bad_argument_is_refused_before_anything_starts(monkeypatch, capsys, installs, tmp_path, name) -> None:
    (tmp_path / "model.txt").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(open_desktop, "_start", lambda *_a: pytest.fail("Desktop started"))

    assert open_desktop.main([str(tmp_path / name)]) == 2
    assert (
        capsys.readouterr().out == "OPEN_DESKTOP: ERROR the argument is not an existing .pbip file; nothing started\n"
    )


def test_no_installed_desktop_is_refused_before_anything_starts(monkeypatch, capsys, tmp_path) -> None:
    pbip = tmp_path / "model.pbip"
    pbip.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(open_desktop, "find_desktop", lambda: None)
    monkeypatch.setattr(open_desktop, "_start", lambda *_a: pytest.fail("Desktop started"))

    assert open_desktop.main([str(pbip)]) == 2
    assert capsys.readouterr().out == f"{open_desktop.NOT_FOUND}\n"


@pytest.mark.parametrize("argv", [[], ["model.pbip", "--check"]], ids=["neither", "both"])
def test_exactly_one_of_a_pbip_or_check(argv) -> None:
    with pytest.raises(SystemExit) as caught:
        open_desktop.main(argv)
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--observe", "1", "model.pbip"],
        ["--observe", "1", "--check"],
        ["--observe", "1", "--refresh"],
        ["--process-start", "5", "model.pbip"],
        ["--json", "--check"],
        ["--collect", "1", "--out", "x.txt"],
        ["--collect", "1", "--process-start", "5"],
        ["--collect", "1", "--process-start", "5", "--out", "x.txt", "--json"],
        ["--collect", "1", "--observe", "1", "--process-start", "5", "--out", "x.txt"],
        ["--observe", "1", "--out", "x.txt"],
    ],
)
def test_observe_is_its_own_mode(monkeypatch, argv) -> None:
    monkeypatch.setattr(open_desktop, "observe", lambda *_a, **_k: pytest.fail("observed"))
    with pytest.raises(SystemExit) as caught:
        open_desktop.main(argv)
    assert caught.value.code == 2


GONE = {"process_gone": "process_gone", "complete": False, "reason": "process_gone"}
CHANGED = {"process_gone": "identity_changed", "complete": False, "reason": "process_gone"}
DIALOG = {"dialog": observer.DialogFinding(observer.DesktopWindow("Sign in", "#32770", 400, 300, hwnd=7), "Sign in")}


@pytest.mark.parametrize("as_json", [False, True], ids=["human", "json"])
@pytest.mark.parametrize(
    ("fields", "state", "code", "sentence"),
    [
        ({}, "READY", 0, "not verified as Power BI Desktop"),
        (DIALOG, "HUMAN_ACTION_REQUIRED", 3, 'dialog is open: "Sign in"'),
        ({"desktop_unready": "no_window", "reason": "no_window"}, "NO_WINDOW", 3, "no Desktop window found"),
        ({"unknown_reason": "timeout", "complete": False, "reason": "timeout"}, "UNKNOWN", 3, "could not be read"),
        (GONE, "PROCESS_GONE", 3, "nothing was observed"),
        (CHANGED, "PROCESS_GONE", 3, "gone=identity_changed"),
    ],
    ids=["ready", "dialog", "no_window", "unknown", "gone", "changed"],
)
def test_observe_mode_maps_each_observer_state(monkeypatch, capsys, as_json, fields, state, code, sentence) -> None:
    seen = {}

    def fake(pid, **kwargs):
        seen.update(pid=pid, **kwargs)
        return observer.Observation(pid=pid, process_started_at="456", **fields)

    monkeypatch.setattr(open_desktop, "observe", fake)

    assert open_desktop.main(["--observe", "123", "--process-start", "456", *(["--json"] if as_json else [])]) == code
    out, err = capsys.readouterr()
    assert seen == {"pid": "123", "expected_process_start": "456"}
    human = err if as_json else out
    assert f"state={state}" in human and sentence in human
    if as_json:
        assert [json.loads(line)["state"] for line in out.splitlines()] == [state]
        assert "Sign in" not in out, "no title may reach the structured output"
    assert not any(word in human for word in ("clean", "healthy")) or state == "READY"


@pytest.mark.parametrize("as_json", [False, True], ids=["human", "json"])
@pytest.mark.parametrize(("pid", "token"), [("abc", None), ("0", None), ("7", "007")])
def test_observe_mode_refuses_a_bad_identity_with_exit_2(capsys, pid, token, as_json) -> None:
    argv = ["--observe", pid, *(["--process-start", token] if token else []), *(["--json"] if as_json else [])]
    assert open_desktop.main(argv) == 2
    out, err = capsys.readouterr()
    assert "nothing observed" in (err if as_json else out)
    assert not as_json or out == "", "--json keeps stdout for the observation line only"


@pytest.fixture(name="launch")
def _launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A launch whose start, launch record and bridge helpers are fakes; returns the .pbip and a log."""
    pbip = tmp_path / "model.pbip"
    pbip.write_text("{}", encoding="utf-8")
    log: dict[str, list] = {"start": [], "frame": [], "status": []}
    monkeypatch.setattr(open_desktop, "find_desktop", lambda: (tmp_path / "bin" / "PBIDesktop.exe", "appx"))
    monkeypatch.setattr(open_desktop, "_start", lambda exe, model: log["start"].append((exe, model)) or PID)
    monkeypatch.setattr(open_desktop, "_launch_record", lambda pid: "/Date(1767225600000)/")
    monkeypatch.setattr(open_desktop, "process_start", lambda pid: f"start-{pid}")
    monkeypatch.setattr(open_desktop, "_ui_bridge_command", lambda: ["powerbi-desktop"])
    monkeypatch.setattr(open_desktop, "_ui_desktop_alive", lambda pid: True)
    monkeypatch.setattr(open_desktop, "_open_elsewhere", lambda model: None)
    clock = _Clock()
    monkeypatch.setattr(open_desktop, "time", clock)
    monkeypatch.setattr(open_desktop, "observe", lambda *_a, **_k: observer.Observation())
    monkeypatch.setattr(
        open_desktop,
        "_credential_state",
        lambda pid: types.SimpleNamespace(dialog=None, state="READY"),
    )
    return types.SimpleNamespace(pbip=pbip, log=log, clock=clock)


def test_success_prints_the_pid_first_fits_the_frame_and_ends_with_json(monkeypatch, capsys, launch) -> None:
    payloads = iter([{"pending": True}, {"ready": True}])
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", lambda command, pid, deadline: next(payloads))
    monkeypatch.setattr(
        open_desktop,
        "_bridge_readiness_result",
        lambda payload, pid: ({"pid": pid, "currentFilePath": str(launch.pbip)} if payload.get("ready") else None, 0.0),
    )

    def fit(pid, record):
        launch.log["frame"].append((pid, record))
        return types.SimpleNamespace(describe=lambda: "frame: fitted")

    monkeypatch.setattr(open_desktop, "fit_launched_frame", fit)

    assert open_desktop.main([str(launch.pbip)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [str(PID), f"OPEN_DESKTOP: process_start=start-{PID}"]
    assert "frame: fitted" in lines
    assert json.loads(lines[-1]) == {"pid": PID, "exe_source": "appx", "ready_seconds": 0}
    assert launch.log["start"] == [(launch.pbip.parent / "bin" / "PBIDesktop.exe", launch.pbip.resolve())]
    assert launch.log["frame"] == [(PID, "/Date(1767225600000)/")]


@pytest.mark.parametrize("ready", [True, False], ids=["ready", "not-ready"])
def test_open_pbip_returns_the_start_token_of_the_pid_it_started(monkeypatch, launch, ready) -> None:
    reused = []

    def status(*args):
        reused.append(True)
        if not ready:
            return _slice_timeout(*args)
        return {}

    # Once the readiness wait has polled, the PID is held by a stranger; the token must predate that.
    monkeypatch.setattr(open_desktop, "process_start", lambda pid: "stranger" if reused else f"start-{pid}")
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", status)
    monkeypatch.setattr(
        open_desktop,
        "_bridge_readiness_result",
        lambda payload, pid: ({"pid": pid, "currentFilePath": str(launch.pbip)}, 0.0),
    )
    monkeypatch.setattr(open_desktop, "fit_launched_frame", lambda *_a: types.SimpleNamespace(describe=lambda: ""))

    pid, record, token = open_desktop.open_pbip(launch.pbip)

    assert (pid, record is not None, token) == (PID, ready, f"start-{PID}")


def test_a_frame_fit_failure_is_never_fatal(monkeypatch, capsys, launch) -> None:
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", lambda *_a: {})
    monkeypatch.setattr(
        open_desktop,
        "_bridge_readiness_result",
        lambda payload, pid: ({"pid": pid, "currentFilePath": str(launch.pbip)}, 0.0),
    )
    monkeypatch.setattr(open_desktop, "fit_launched_frame", lambda *_a: (_ for _ in ()).throw(OSError("no frame")))

    assert open_desktop.main([str(launch.pbip)]) == 0
    assert "frame: not fitted (OSError)" in capsys.readouterr().out


def _raise(exc: Exception):
    def boom(*_args, **_kwargs):
        raise exc

    return boom


def _slow_raise(exc: Exception):
    """A bridge call that takes 1 s and raises `exc`, so a regression that retried would end at the deadline."""

    def boom(*_args):
        open_desktop.time.now += 1
        raise exc

    return boom


def _slice_timeout(_command, _pid, deadline):
    """A bridge status call that uses its whole bounded slice and then times out (the fixture clock moves)."""
    open_desktop.time.now = deadline
    raise TimeoutError


@pytest.mark.parametrize(
    ("patches", "seconds", "reason"),
    [
        ({"_ui_bridge_status": _slice_timeout}, open_desktop.READY_TIMEOUT_SECONDS, "timed out"),
        ({"_ui_desktop_alive": lambda pid: False}, 0, "Desktop process gone"),
        ({"_ui_bridge_status": _slow_raise(open_desktop.DesktopActionError("bridge refused"))}, 1, "bridge refused"),
        ({"_ui_bridge_status": _slow_raise(PermissionError("D:\\private\\secret.pbip"))}, 1, "PermissionError"),
    ],
    ids=["timeout", "gone", "bridge-error", "bridge-oserror"],
)
def test_the_pid_is_printed_when_readiness_fails(monkeypatch, capsys, launch, patches, seconds, reason) -> None:
    for name, value in patches.items():
        monkeypatch.setattr(open_desktop, name, value)
    monkeypatch.setattr(open_desktop, "fit_launched_frame", lambda *_a: pytest.fail("frame fitted"))

    assert open_desktop.main([str(launch.pbip)]) == 2
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == str(PID)
    assert "OPEN_DESKTOP: diagnostic: READY; inspect that Desktop locally" in lines
    assert lines[-2:] == [
        f"OPEN_DESKTOP: ERROR PID {PID} not ready after {seconds} seconds ({reason})",
        f"OPEN_DESKTOP: Desktop PID {PID} left open",
    ]


def test_a_dialog_is_reported_for_a_human_and_left_alone(monkeypatch, capsys, launch) -> None:
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", _slice_timeout)
    monkeypatch.setattr(
        open_desktop, "_credential_state", lambda pid: types.SimpleNamespace(dialog=object(), state="DIALOG")
    )
    monkeypatch.setattr(open_desktop, "dialog_stop_detail", lambda _finding: "A dialog is open. A human must act.")

    assert open_desktop.main([str(launch.pbip)]) == 2
    out = capsys.readouterr().out
    assert "OPEN_DESKTOP: diagnostic: A dialog is open. A human must act." in out
    assert out.splitlines()[0] == str(PID)


def test_a_pbip_already_open_in_another_desktop_is_refused_before_any_start(monkeypatch, capsys, launch) -> None:
    calls = []
    other = {"pid": 77, "currentFilePath": str(launch.pbip.parent / "elsewhere.pbip")}
    # The same file spelled with a detour through its own folder: it resolves identically on every host.
    detour = launch.pbip.parent / ".." / launch.pbip.parent.name / launch.pbip.name
    holder = {"pid": 99, "currentFilePath": str(detour)}

    def status(command, pid, deadline):
        calls.append(pid)
        return {"status": "ready", "instances": [other, holder]}

    monkeypatch.setattr(open_desktop, "_open_elsewhere", REAL_OPEN_ELSEWHERE)
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", status)
    # The fixture clock is frozen, so a regression that started Desktop would wait forever instead of failing.
    monkeypatch.setattr(open_desktop, "_start", lambda *_args: pytest.fail("Desktop started"))

    assert open_desktop.main([str(launch.pbip)]) == 2
    assert capsys.readouterr().out.splitlines() == [
        "OPEN_DESKTOP: ERROR this .pbip is already open in Desktop PID 99; nothing started"
    ]
    assert calls == [None]


@pytest.mark.parametrize(
    "noise",
    [
        {"pid": 1},
        {"pid": 2, "currentFilePath": None},
        {"pid": 3, "currentFilePath": ""},
        {"pid": 4, "currentFilePath": 7},
        "not a dict",
    ],
    ids=["missing", "none", "empty", "non-string", "non-dict"],
)
def test_the_already_open_check_skips_unusable_entries_and_ignores_the_same_name_elsewhere(
    monkeypatch, tmp_path, noise
) -> None:
    model = tmp_path / "here" / "model.pbip"
    namesake = {"pid": 5, "currentFilePath": str(tmp_path / "there" / "model.pbip")}
    holder = {"pid": 6, "currentFilePath": str(model)}
    listing = {"instances": [noise, namesake]}
    monkeypatch.setattr(open_desktop, "_ui_bridge_command", lambda: ["powerbi-desktop"])
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", lambda *_args: listing)

    assert REAL_OPEN_ELSEWHERE(model) is None
    listing["instances"].append(holder)
    assert REAL_OPEN_ELSEWHERE(model) is holder


def test_a_failed_instance_listing_starts_nothing(monkeypatch, capsys, launch) -> None:
    monkeypatch.setattr(open_desktop, "_open_elsewhere", REAL_OPEN_ELSEWHERE)
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", _raise(TimeoutError()))

    assert open_desktop.main([str(launch.pbip)]) == 2
    assert capsys.readouterr().out.splitlines() == [
        "OPEN_DESKTOP: ERROR cannot list the open Desktops (TimeoutError); nothing started"
    ]
    assert launch.log["start"] == []


def _ready(**instance) -> dict:
    """A "ready"/"connected" payload for PID; without `currentFilePath` it is the untitled startup canvas."""
    return {
        "status": "ready",
        "instances": [{"pid": PID, "bridgeStatus": "connected", "hasUnsavedChanges": False, **instance}],
    }


def _host_error(**error) -> dict:
    """A measured startup error payload (Desktop 2.160.579.0), with any error field overridable."""
    fields = {
        "message": "Host is not ready to accept operations",
        "code": "BRIDGE_ERROR",
        "jsonRpcCode": -32502,
        "retryable": True,
        "retryAfterMs": 2000,
        "details": {},
    }
    return {"status": "error", "instances": [{"pid": PID, "bridgeStatus": "error", "error": {**fields, **error}}]}


CANCELLED = _host_error(message="The task was cancelled.", jsonRpcCode=-32800, retryable=False, retryAfterMs=None)


class _Clock:
    """A monotonic clock that moves only when the code under test sleeps (or a fake call advances it)."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []
        self.slept_to: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self.slept_to.append(self.now)


@pytest.mark.parametrize(
    "startup",
    [
        [_ready(), _ready(currentFilePath=None), _ready(currentFilePath=""), _host_error()],
        [{"status": "not_connected", "instances": []}, CANCELLED],
    ],
    ids=["cold-untitled-then-host-not-ready", "warm-task-cancelled"],
)
def test_startup_states_are_waited_through_until_ready_on_the_launched_file(
    monkeypatch, capsys, launch, startup
) -> None:
    """Both sequences measured on Desktop 2.160.579.0 end ready on the launched .pbip."""
    payloads = iter([*startup, _ready(currentFilePath=os.path.normcase(str(launch.pbip.resolve())))])
    clock = _Clock()
    monkeypatch.setattr(open_desktop, "time", clock)
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", lambda command, pid, deadline: next(payloads))
    monkeypatch.setattr(
        open_desktop, "fit_launched_frame", lambda *_a: types.SimpleNamespace(describe=lambda: "frame: fitted")
    )

    assert open_desktop.main([str(launch.pbip)]) == 0
    assert next(payloads, None) is None
    assert len(clock.sleeps) == len(startup)
    assert all(0 < seconds <= 2.0 for seconds in clock.sleeps)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == str(PID)
    assert json.loads(lines[-1])["pid"] == PID


def test_desktop_exiting_after_a_waited_through_reply_stops_at_once(monkeypatch, capsys, launch) -> None:
    alive = iter([True, False])
    clock = _Clock()
    monkeypatch.setattr(open_desktop, "time", clock)
    monkeypatch.setattr(open_desktop, "_ui_desktop_alive", lambda pid: next(alive))
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", lambda command, pid, deadline: CANCELLED)
    monkeypatch.setattr(open_desktop, "fit_launched_frame", lambda *_a: pytest.fail("frame fitted"))

    assert open_desktop.main([str(launch.pbip)]) == 2
    assert clock.sleeps == [2.0]
    assert capsys.readouterr().out.splitlines()[-2] == (
        f"OPEN_DESKTOP: ERROR PID {PID} not ready after 2 seconds (Desktop process gone)"
    )


def test_ready_on_a_different_file_is_refused(monkeypatch, capsys, launch, tmp_path) -> None:
    payloads = iter([_ready(), _ready(currentFilePath=str(tmp_path / "other.pbip"))])
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", lambda command, pid, deadline: next(payloads))
    monkeypatch.setattr(open_desktop, "fit_launched_frame", lambda *_a: pytest.fail("frame fitted"))

    assert open_desktop.main([str(launch.pbip)]) == 2
    assert capsys.readouterr().out.splitlines()[-2] == (
        f"OPEN_DESKTOP: ERROR PID {PID} not ready after 2 seconds (bridge bound to a different file)"
    )


@pytest.mark.parametrize(
    "payload",
    [
        _host_error(retryable=False),
        _host_error(message="Model failed to load"),
        _host_error(code="INTERNAL_ERROR"),
        _ready(pid=PID + 1),
        {"status": "ready", "instances": _ready()["instances"] * 2},
        _ready(error={"message": "load failed"}),
        {**_ready(), "error": "load failed"},
        {"status": "starting"},
    ],
    ids=[
        "not-retryable",
        "other-message",
        "other-code",
        "other-pid",
        "two-instances",
        "instance-error",
        "error",
        "new",
    ],
)
def test_any_other_bridge_reply_is_waited_through_not_fatal(monkeypatch, capsys, launch, payload) -> None:
    """Startup replies vary by Desktop version; only ready on the launched file ends the wait."""
    payloads = iter([payload, _ready(currentFilePath=str(launch.pbip))])
    clock = _Clock()
    monkeypatch.setattr(open_desktop, "time", clock)
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", lambda *_a: next(payloads))
    monkeypatch.setattr(
        open_desktop, "fit_launched_frame", lambda *_a: types.SimpleNamespace(describe=lambda: "frame: fitted")
    )

    assert open_desktop.main([str(launch.pbip)]) == 0
    assert clock.sleeps == [open_desktop.STARTUP_POLL_SECONDS]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["pid"] == PID


def test_the_deadline_still_bounds_a_startup_that_never_settles(monkeypatch, capsys, launch) -> None:
    clock = _Clock()
    deadline = clock.now + open_desktop.READY_TIMEOUT_SECONDS
    states = itertools.cycle([_ready(), _host_error(), CANCELLED])
    polls: list[float] = []

    def status(_command, _pid, held_deadline):
        assert held_deadline == min(deadline, clock.now + open_desktop.BRIDGE_SLICE_SECONDS)
        if clock.now >= deadline:
            pytest.fail("polled at or after the deadline")
        polls.append(clock.now)
        clock.now += 0.75  # one status call takes time
        return next(states)

    monkeypatch.setattr(open_desktop, "time", clock)
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", status)
    monkeypatch.setattr(open_desktop, "fit_launched_frame", lambda *_a: pytest.fail("frame fitted"))

    assert open_desktop.main([str(launch.pbip)]) == 2
    assert len(polls) > 2
    assert max(clock.slept_to) <= deadline
    assert capsys.readouterr().out.splitlines()[-2] == (
        f"OPEN_DESKTOP: ERROR PID {PID} not ready after {open_desktop.READY_TIMEOUT_SECONDS} seconds (timed out)"
    )


def _modal(hwnd: int = 11, owner_hwnd: int = 9, owner_enabled: bool | None = False) -> observer.Observation:
    """What the observer names as the blocker; owned with a disabled owner is a real modal."""
    window = observer.DesktopWindow(
        "Unable to open document", "#32770", 400, 300, hwnd=hwnd, owner_hwnd=owner_hwnd, owner_enabled=owner_enabled
    )
    return observer.Observation(dialog=observer.DialogFinding(window, window.title))


def _pending(_command, _pid, _deadline):
    """A bridge that answers at once with a startup reply (its own 0.5 s retry), never ready."""
    return {"status": "not_connected", "instances": []}


BRIDGES = pytest.mark.parametrize(
    ("bridge", "first_look"), [(_pending, 100.0), (_slice_timeout, 105.0)], ids=["pending", "slice-timeout"]
)


@BRIDGES
def test_a_held_modal_ends_the_launch_wait_early_and_leaves_desktop_open(
    monkeypatch, capsys, launch, bridge, first_look
) -> None:
    looks = []

    def look(pid, expected_process_start):
        looks.append(launch.clock.now)
        assert (pid, expected_process_start) == (PID, f"start-{PID}")
        return _modal()

    monkeypatch.setattr(open_desktop, "observe", look)
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", bridge)
    monkeypatch.setattr(open_desktop, "fit_launched_frame", lambda *_a: pytest.fail("frame fitted"))

    assert open_desktop.main([str(launch.pbip)]) == 2
    lines = capsys.readouterr().out.splitlines()
    stopped = round(first_look + 10 - 100)  # the plan's 10 s hold, counted from the first look
    assert lines[:2] == [str(PID), f"OPEN_DESKTOP: process_start=start-{PID}"]
    assert lines[-3:] == [
        "OPEN_DESKTOP: diagnostic: READY; inspect that Desktop locally",
        f"OPEN_DESKTOP: ERROR PID {PID} not ready after {stopped} seconds (a dialog stayed open)",
        f"OPEN_DESKTOP: Desktop PID {PID} left open",
    ]
    assert all(later - earlier >= open_desktop.LOOK_SECONDS for earlier, later in itertools.pairwise(looks))


@BRIDGES
@pytest.mark.parametrize(
    "looks",
    [
        lambda n: _modal(owner_hwnd=0),
        lambda n: _modal(owner_enabled=True),
        lambda n: _modal(owner_enabled=None),
        lambda n: _modal(hwnd=11 + n % 2),
        lambda n: observer.Observation(unknown_reason="minimized", complete=False, reason="minimized"),
        lambda n: (_ for _ in ()).throw(OSError("cannot enumerate")),
    ],
    ids=["unowned-splash", "owner-enabled", "owner-unknown", "handle-changes", "unknown", "observer-raises"],
)
def test_anything_but_one_held_modal_waits_to_the_deadline(monkeypatch, capsys, launch, bridge, first_look, looks):
    del first_look
    count = itertools.count()
    monkeypatch.setattr(open_desktop, "observe", lambda *_a, **_k: looks(next(count)))
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", bridge)

    assert open_desktop.main([str(launch.pbip)]) == 2
    assert capsys.readouterr().out.splitlines()[-2] == (
        f"OPEN_DESKTOP: ERROR PID {PID} not ready after {open_desktop.READY_TIMEOUT_SECONDS} seconds (timed out)"
    )


@BRIDGES
def test_bridge_ready_in_the_pass_that_would_stop_still_wins(monkeypatch, capsys, launch, bridge, first_look) -> None:
    stop = first_look + 10
    ready = _ready(currentFilePath=str(launch.pbip))

    def status(command, pid, deadline):
        if launch.clock.now >= stop:
            return ready
        try:
            return bridge(command, pid, deadline)
        except TimeoutError:
            if launch.clock.now >= stop:
                return ready  # this slice's reply arrived at its end, in the pass that would have stopped
            raise

    monkeypatch.setattr(open_desktop, "observe", lambda *_a, **_k: _modal())
    monkeypatch.setattr(open_desktop, "_ui_bridge_status", status)
    monkeypatch.setattr(open_desktop, "fit_launched_frame", lambda *_a: types.SimpleNamespace(describe=lambda: ""))

    assert open_desktop.main([str(launch.pbip)]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["pid"] == PID


@pytest.fixture(name="collect")
def _collect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A collect whose observer and identity check are fakes and whose helper is a tiny real script."""
    state = types.SimpleNamespace(observation=_modal(hwnd=77), token="456", out=tmp_path / "details.txt")
    helper = tmp_path / "helper.py"
    monkeypatch.setattr(open_desktop, "DETAILS_HELPER", helper)
    monkeypatch.setattr(open_desktop, "observe", lambda *_a, **_k: state.observation)
    monkeypatch.setattr(open_desktop, "_start_token", lambda pid: state.token)

    def helper_prints(result: dict | None = None, body: str = "") -> None:
        clicked = tmp_path / "clicked.json"
        line = f"print({json.dumps(json.dumps(result))})" if result is not None else ""
        helper.write_text(
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(clicked)!r}).write_text(json.dumps(sys.argv[1:]))\n{body}\n{line}\n",
            encoding="utf-8",
        )
        state.clicked = clicked

    state.helper_prints = helper_prints
    helper_prints({})
    return state


COPIED = (
    "Feedback Type:\r\nFrown (Error)\r\n\r\nPowerBINonFatalError_ErrorCode:\r\n\r\n"
    "InnerException0.PowerBINonFatalError_ErrorCode:\r\nModelSchemaValidationFailed\r\nUser: caf\u00e9\r\n"
)
CLICKED = {"message": "Unable to open document", "text": COPIED, "sequence_changed": True, "owner_pid": 123}


def test_collect_writes_the_copied_text_byte_for_byte(capsys, collect) -> None:
    collect.helper_prints(CLICKED)

    assert open_desktop.main(["--collect", "123", "--process-start", "456", "--out", str(collect.out)]) == 0
    assert collect.out.read_bytes() == COPIED.encode("utf-8")
    assert json.loads(collect.clicked.read_text(encoding="utf-8")) == ["123", "77"]
    assert capsys.readouterr().out.splitlines() == [
        f'OPEN_DESKTOP: collect pid=123 title="Unable to open document" error_code=ModelSchemaValidationFailed'
        f" out={collect.out}",
        "message: Unable to open document",
    ]


GONE_OBSERVATION = observer.Observation(**GONE)
MINIMIZED = observer.Observation(unknown_reason="minimized", complete=False, reason="minimized")


@pytest.mark.parametrize(
    ("observation", "code", "message"),
    [
        (observer.Observation(), 3, "OPEN_DESKTOP: no dialog to collect (state=READY)"),
        (observer.Observation(desktop_unready="no_window"), 3, "OPEN_DESKTOP: no dialog to collect (state=NO_WINDOW)"),
        (GONE_OBSERVATION, 3, "OPEN_DESKTOP: no dialog to collect (state=PROCESS_GONE)"),
        (
            observer.Observation(**CHANGED),
            2,
            "OPEN_DESKTOP: ERROR the PID is not the Desktop that token names; nothing collected",
        ),
        (
            MINIMIZED,
            2,
            "OPEN_DESKTOP: ERROR could not read the windows (minimized); nothing collected;"
            " restore the Desktop window and retry",
        ),
        (
            observer.Observation(unknown_reason="timeout", complete=False, reason="timeout"),
            2,
            "OPEN_DESKTOP: ERROR could not read the windows (timeout); nothing collected",
        ),
    ],
    ids=["ready", "no-window", "exited", "identity-changed", "minimized", "unknown"],
)
def test_collect_without_a_readable_dialog_clicks_nothing(capsys, collect, observation, code, message) -> None:
    collect.observation = observation

    assert open_desktop.main(["--collect", "123", "--process-start", "456", "--out", str(collect.out)]) == code
    assert capsys.readouterr().out.splitlines() == [message]
    assert not collect.clicked.exists() and not collect.out.exists()


@pytest.mark.parametrize(
    ("result", "token", "message"),
    [
        (
            {**CLICKED, "sequence_changed": False},
            "456",
            "OPEN_DESKTOP: ERROR clipboard was not written by this Desktop; nothing written",
        ),
        (
            {**CLICKED, "owner_pid": 999},
            "456",
            "OPEN_DESKTOP: ERROR clipboard was not written by this Desktop; nothing written",
        ),
        (CLICKED, "789", "OPEN_DESKTOP: ERROR the Desktop changed during collect; nothing written"),
        ({"error": "no IE control"}, "456", "OPEN_DESKTOP: ERROR could not collect (no IE control)"),
        (None, "456", "OPEN_DESKTOP: ERROR could not collect (collector failed)"),
    ],
    ids=["sequence-unchanged", "other-owner", "identity-changed-after-click", "helper-refused", "helper-crashed"],
)
def test_collect_writes_nothing_it_cannot_attribute_to_this_desktop(capsys, collect, result, token, message) -> None:
    collect.helper_prints(result)
    collect.token = token

    assert open_desktop.main(["--collect", "123", "--process-start", "456", "--out", str(collect.out)]) == 2
    assert capsys.readouterr().out.splitlines() == [message]
    assert collect.clicked.exists() and not collect.out.exists()


def test_a_hung_collector_is_killed_at_the_timeout(monkeypatch, capsys, collect) -> None:
    collect.helper_prints(CLICKED, body="import time\ntime.sleep(60)")
    monkeypatch.setattr(open_desktop, "COLLECT_TIMEOUT_SECONDS", 1)
    started = time.monotonic()

    assert open_desktop.main(["--collect", "123", "--process-start", "456", "--out", str(collect.out)]) == 2
    assert time.monotonic() - started < 30
    assert capsys.readouterr().out.splitlines() == ["OPEN_DESKTOP: ERROR collector timed out; Desktop left as is"]
    assert not collect.out.exists()


def test_collect_refuses_a_missing_out_folder_before_looking(capsys, collect) -> None:
    out = collect.out.parent / "missing" / "details.txt"

    assert open_desktop.main(["--collect", "123", "--process-start", "456", "--out", str(out)]) == 2
    assert capsys.readouterr().out.splitlines() == [
        "OPEN_DESKTOP: ERROR the --out folder does not exist; nothing collected"
    ]
    assert not collect.clicked.exists()


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (
            "PowerBINonFatalError_ErrorCode:\nTopLevel\nInnerException0.PowerBINonFatalError_ErrorCode:\nInner\n",
            "TopLevel",
        ),
        ("PowerBINonFatalError_ErrorCode:\nInnerException0.PowerBINonFatalError_ErrorCode:\nInner\n", "Inner"),
        ("  PowerBINonFatalError_ErrorCode:  \r\n\r\n  NextLine  \r\n", "NextLine"),
        ("Feedback Type:\nFrown (Error)\nPowerBINonFatalError_ErrorCode:\n", None),
    ],
    ids=["top-level", "inner-after-empty-top", "value-on-a-later-line", "none"],
)
def test_the_error_code_is_the_first_non_empty_labelled_value(text, code) -> None:
    assert open_desktop.error_code(text) == code


def test_the_details_helper_imports_and_refuses_bad_arguments_without_windows_apis(capsys) -> None:
    """Every test host imports it (ubuntu CI included): Windows APIs bind only once a dialog is named."""
    assert details.main(["not-a-pid"]) == 0
    assert json.loads(capsys.readouterr().out) == {"error": "ValueError"}


@pytest.fixture(name="fake_netfx")
def _fake_netfx(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A fresh process whose (fake) CLR records references and reports file versions."""
    monkeypatch.setattr(pdq, "_ADOMD_BY_PORT", {})
    monkeypatch.setattr(pdq, "_ADOMD_LOADED", {})
    references: list[str] = []
    versions = {"PBIDesktop.exe": "2.160.579.0", pdq.ADOMD_CLIENT_DLL: "17.0.97.16"}
    pythonnet = types.ModuleType("pythonnet")
    pythonnet.load = lambda _runtime: None
    clr = types.ModuleType("clr")
    clr.AddReference = references.append
    adomd = types.ModuleType("Microsoft.AnalysisServices.AdomdClient")
    adomd.AdomdConnection = object()
    diagnostics = types.ModuleType("System.Diagnostics")
    diagnostics.FileVersionInfo = types.SimpleNamespace(
        GetVersionInfo=lambda path: types.SimpleNamespace(FileVersion=versions[Path(path).name])
    )
    modules = {
        "pythonnet": pythonnet,
        "clr": clr,
        "Microsoft": types.ModuleType("Microsoft"),
        "Microsoft.AnalysisServices": types.ModuleType("Microsoft.AnalysisServices"),
        "Microsoft.AnalysisServices.AdomdClient": adomd,
        "System": types.ModuleType("System"),
        "System.Diagnostics": diagnostics,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return references


def test_check_loads_the_bundled_client_and_prints_versions(monkeypatch, capsys, installs, fake_netfx) -> None:
    (installs.appx.parent / pdq.ADOMD_CLIENT_DLL).write_bytes(b"")
    monkeypatch.setattr(open_desktop, "_start", lambda *_a: pytest.fail("Desktop started"))

    assert open_desktop.main(["--check"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "exe_source": "appx",
        "desktop_version": "2.160.579.0",
        "adomd_version": "17.0.97.16",
    }
    assert fake_netfx == [str(installs.appx.parent / pdq.ADOMD_CLIENT_DLL)]


def test_check_failure_lines(monkeypatch, capsys, installs, fake_netfx) -> None:
    assert open_desktop.main(["--check"]) == 2
    assert capsys.readouterr().out == (
        f"OPEN_DESKTOP: ERROR ADOMD_UNAVAILABLE {pdq.ADOMD_CLIENT_DLL} is not beside the Power BI Desktop executable\n"
    )
    assert fake_netfx == []

    monkeypatch.setattr(open_desktop, "find_desktop", lambda: None)
    assert open_desktop.main(["--check"]) == 2
    assert capsys.readouterr().out == f"{open_desktop.NOT_FOUND}\n"


def test_check_fails_cleanly_in_a_real_process(tmp_path) -> None:
    """A pinned exe with no client beside it: exact line, exit 2, no traceback, nothing started."""
    exe = _exe(tmp_path / "bin")
    result = subprocess.run(
        [sys.executable, str(SKILL_SCRIPTS / "open_desktop.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "PBI_DESKTOP_PATH": str(exe)},
    )

    assert result.returncode == 2
    assert result.stdout == (
        f"OPEN_DESKTOP: ERROR ADOMD_UNAVAILABLE {pdq.ADOMD_CLIENT_DLL} is not beside the Power BI Desktop executable\n"
    )
    assert "Traceback" not in result.stdout + result.stderr
