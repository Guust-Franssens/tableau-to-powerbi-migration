"""Focused tests for the explicit UI route's child protocol and engine completion authority."""

# The test module intentionally exercises private orchestration seams; assertions belong in tests,
# and the fake reader mirrors ADOMD.NET's PascalCase API rather than a Python naming convention.
# pylint: disable=missing-function-docstring,protected-access,too-many-lines

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import ctypes
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import _desktop_actions as actions
import probe_desktop_query as query_probe
import refresh_pbip_model
from _desktop_observer import CredentialModal, DesktopWindow, DialogFinding, Observation
from _desktop_actions import (
    ActionResult,
    CompletionResult,
    DesktopActionError,
    PartitionSnapshot,
    SourcePartition,
    _read_action_output,
    _snapshot_from_connection,
    check_completion,
    invoke_desktop_action,
)
from refresh_pbip_model import UIRefreshContext

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Desktop UIA qualification is Windows-only")


BASELINE = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _ready_bridge() -> dict:
    return {
        "status": "ready",
        "instances": [
            {
                "pid": 123,
                "bridgeStatus": "connected",
                "currentFilePath": str(Path.cwd() / "model.pbip"),
                "hasUnsavedChanges": False,
            }
        ],
    }


def _busy_bridge(delay: object = 250) -> dict:
    return {
        "status": "error",
        "instances": [
            {
                "pid": 123,
                "bridgeStatus": "error",
                "error": {
                    "code": "BRIDGE_ERROR",
                    "message": "Another operation is already in progress",
                    "retryable": True,
                    "retryAfterMs": delay,
                },
            }
        ],
    }


@pytest.fixture(name="bridge_clock")
def _bridge_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [100.0]
    monkeypatch.setattr(refresh_pbip_model.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(refresh_pbip_model.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(refresh_pbip_model, "_ui_desktop_alive", lambda _pid: True)
    monkeypatch.setattr(refresh_pbip_model, "_ui_bridge_command", lambda: ["controlled-bridge"])
    return clock


@pytest.mark.parametrize("pending", [None, "not_connected", "busy"])
def test_bridge_readiness_positive_controls_and_no_blind_sleep(
    monkeypatch: pytest.MonkeyPatch, bridge_clock: list[float], pending: str | None
) -> None:
    payloads = [_ready_bridge()]
    if pending:
        payloads.insert(0, _busy_bridge() if pending == "busy" else {"status": "not_connected", "instances": []})
    calls = []

    def status(_command, pid, deadline):
        calls.append((pid, deadline))
        return payloads.pop(0)

    monkeypatch.setattr(refresh_pbip_model, "_ui_bridge_status", status)
    assert refresh_pbip_model._wait_ui_bridge(123, 160)["bridgeStatus"] == "connected"
    assert calls == [(123, 160)] * (2 if pending else 1)
    assert bridge_clock[0] == 100 + (0.25 if pending == "busy" else 0.5 if pending else 0)


@pytest.mark.parametrize("delay", [-1, None, True, "250", float("nan"), float("inf"), 10**400])
def test_bridge_readiness_refuses_invalid_busy_retry_metadata(delay: object) -> None:
    with pytest.raises(DesktopActionError):
        refresh_pbip_model._bridge_readiness_result(_busy_bridge(delay), 123)


@pytest.mark.parametrize(
    "change",
    ["top_error", "wrong_pid", "bool_pid", "wrong_field", "path", "relative", "duplicate", "instance_error"],
)
def test_bridge_readiness_requires_both_positive_fields_and_exact_pid_file(change: str) -> None:
    payload = _ready_bridge()
    instance = payload["instances"][0]
    if change == "top_error":
        payload["status"] = "error"
    elif change == "wrong_pid":
        instance["pid"] = 456
    elif change == "bool_pid":
        instance["pid"] = True
    elif change == "wrong_field":
        instance["bridgeStatus"] = "ready"
    elif change in ("path", "relative"):
        instance["currentFilePath"] = "" if change == "path" else "relative.pbip"
    elif change == "duplicate":
        payload["instances"].append(dict(instance))
    else:
        instance["error"] = {"code": "BRIDGE_ERROR"}
    with pytest.raises(DesktopActionError):
        refresh_pbip_model._bridge_readiness_result(payload, 123)


@pytest.mark.parametrize(
    "field,value",
    [
        ("retryable", False),
        ("retryable", 1),
        ("code", "CREDENTIAL_REQUIRED"),
        ("message", "Sign in"),
        ("message", "unknown error"),
    ],
)
def test_bridge_readiness_never_retries_terminal_or_unrecognized_errors(field: str, value: object) -> None:
    payload = _busy_bridge()
    payload["instances"][0]["error"][field] = value
    with pytest.raises(DesktopActionError):
        refresh_pbip_model._bridge_readiness_result(payload, 123)


@pytest.mark.parametrize("delay,expected", [(0, 0.05), (250, 0.25), (100000, 1.0)])
def test_bridge_busy_retry_delay_is_clipped(delay: int, expected: float) -> None:
    assert refresh_pbip_model._bridge_readiness_result(_busy_bridge(delay), 123) == (None, expected)


@pytest.mark.parametrize("late_ready", [False, True])
def test_bridge_readiness_deadline_never_resets_or_accepts_late_ready(
    monkeypatch: pytest.MonkeyPatch, bridge_clock: list[float], late_ready: bool
) -> None:
    calls = []

    def status(_command, pid, deadline):
        calls.append((pid, deadline))
        if late_ready:
            bridge_clock[0] = deadline
            return _ready_bridge()
        return _busy_bridge(100000)

    monkeypatch.setattr(refresh_pbip_model, "_ui_bridge_status", status)
    with pytest.raises(TimeoutError):
        refresh_pbip_model._wait_ui_bridge(123, 102.25)
    assert bridge_clock[0] == 102.25
    assert calls == [(123, 102.25)] * (1 if late_ready else 3)


def test_bridge_readiness_process_gone_never_calls_status(
    monkeypatch: pytest.MonkeyPatch, bridge_clock: list[float]
) -> None:
    monkeypatch.setattr(refresh_pbip_model, "_ui_desktop_alive", lambda _pid: False)
    monkeypatch.setattr(
        refresh_pbip_model, "_ui_bridge_status", lambda *_args: pytest.fail("status after process gone")
    )
    with pytest.raises(DesktopActionError, match="gone"):
        refresh_pbip_model._wait_ui_bridge(123, bridge_clock[0] + 60)


@pytest.mark.parametrize("outcome", ["ready", "timeout", "terminal"])
def test_ui_main_readiness_gate_precedes_cache_and_never_uses_legacy_bridge(
    monkeypatch: pytest.MonkeyPatch, outcome: str, capsys: pytest.CaptureFixture[str]
) -> None:
    context, _args, calls = _wire_ui_route(monkeypatch, no_save=True)
    instance = _ready_bridge()["instances"][0]
    sequence = []
    for name in ("_resolve_pid", "_instance", "_bridge_status", "_refresh_and_save", "save"):
        monkeypatch.setattr(refresh_pbip_model, name, lambda *_args: pytest.fail("legacy UI path"))

    def wait(pid, deadline):
        assert pid == 123 and deadline > 0
        sequence.append("readiness")
        if outcome == "timeout":
            raise TimeoutError
        if outcome == "terminal":
            raise DesktopActionError("private customer error must not print")
        return instance

    def cache(pid, **kwargs):
        assert pid == 123 and kwargs == {"instance": instance}
        assert sequence == ["readiness"]
        sequence.append("cache")
        return Path("missing-cache.abf")

    monkeypatch.setattr(refresh_pbip_model, "_wait_ui_bridge", wait)
    monkeypatch.setattr(
        refresh_pbip_model,
        "_ui_bridge_status",
        lambda *_args, **kwargs: (
            {"status": "ok", "pid": 123, "result": {"success": True}}
            if kwargs.get("operation") == "reload"
            else _ready_bridge()
        ),
    )
    monkeypatch.setattr(refresh_pbip_model, "cache_file", cache)
    monkeypatch.setattr(refresh_pbip_model, "source_hint_from_model", lambda _path: None)
    root = DesktopWindow("", "", 100, 100, hwnd=789, pid="123", process_started_at="456")
    monkeypatch.setattr(
        refresh_pbip_model,
        "_credential_state",
        lambda _pid, **_kwargs: Observation(pid="123", process_started_at="456", target_window=root),
    )
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda _pid: 49157)
    monkeypatch.setattr(refresh_pbip_model, "bind_desktop", lambda *_args, **_kwargs: context.bound)
    result = refresh_pbip_model.main(["--pid", "123", "--ui-refresh", "--no-save"])
    assert result == (0 if outcome == "ready" else 3)
    assert calls == (["Refresh", "canaries"] if outcome == "ready" else [])
    assert sequence == (["readiness", "cache"] if outcome == "ready" else ["readiness"])
    output = capsys.readouterr().out
    assert "private customer" not in output
    if outcome == "timeout":
        assert output.index("REFRESH: TIMEOUT") < output.index("bridge diagnostic")


def test_ui_refresh_requires_explicit_pid_before_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh_pbip_model, "_resolve_pid", lambda *_args: pytest.fail("legacy discovery"))
    monkeypatch.setattr(refresh_pbip_model, "_wait_ui_bridge", lambda *_args: pytest.fail("bridge discovery"))
    assert refresh_pbip_model.main(["--ui-refresh"]) == 2


@pytest.mark.parametrize("operation_seconds,expected", [(10, 110.0), (3600, 160.0)])
def test_ui_main_caps_readiness_at_sixty_seconds_and_original_operation_deadline(
    monkeypatch: pytest.MonkeyPatch, bridge_clock: list[float], operation_seconds: int, expected: float
) -> None:
    deadlines = []

    def wait(_pid, deadline):
        deadlines.append(deadline)
        raise TimeoutError

    monkeypatch.setattr(refresh_pbip_model, "_wait_ui_bridge", wait)
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: Observation())
    assert (
        refresh_pbip_model.main(
            ["--pid", "123", "--ui-refresh", "--refresh-absolute-timeout-seconds", str(operation_seconds)]
        )
        == 3
    )
    assert deadlines == [expected] and bridge_clock[0] == 100


def test_cache_file_consumes_snapshot_without_instance_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh_pbip_model, "_instance", lambda *_args: pytest.fail("legacy bridge"))
    model = Path.cwd() / "bound.SemanticModel"
    monkeypatch.setattr(refresh_pbip_model, "_resolve_pbip_binding", lambda _path: ("resolved", model))
    assert refresh_pbip_model.cache_file(123, instance=_ready_bridge()["instances"][0]) == model / ".pbi" / "cache.abf"


@pytest.fixture(name="clean_start_route")
def _clean_start_route(  # pylint: disable=too-many-locals
    monkeypatch: pytest.MonkeyPatch, bridge_clock: list[float]
):
    context, _args, actions_seen = _wire_ui_route(monkeypatch)
    root = DesktopWindow("", "", 100, 100, hwnd=789, pid="123", process_started_at="456")
    ready = Observation(pid="123", process_started_at="456", target_window=root)
    controls = {
        "initial": _ready_bridge()["instances"][0],
        "pre_observer": ready,
        "reload": {"status": "ok", "pid": 123, "result": {"success": True}},
        "post_status": _ready_bridge(),
        "post_observer": ready,
        "late": None,
        "deadline": 160.0,
    }
    events = []
    observers = iter(("pre_observer", "post_observer"))

    def phase(name):
        events.append(name)
        if controls["late"] == name:
            bridge_clock[0] = controls["deadline"]

    def wait(pid, deadline):
        assert pid == 123 and deadline == controls["deadline"]
        phase("initial")
        return controls["initial"]

    def observe(pid, **kwargs):
        name = next(observers)
        assert pid == 123 and 0 < kwargs["timeout_s"] <= 8
        if name == "post_observer":
            assert kwargs["expected_process_start"] == controls["pre_observer"].process_started_at
        else:
            assert "expected_process_start" not in kwargs
        phase(name)
        return controls[name]

    def bridge(command, pid, deadline, *, operation="status"):
        assert pid == 123 and deadline == controls["deadline"] and command == ["controlled-bridge"]
        phase("reload" if operation == "reload" else "post_status")
        response = controls["reload" if operation == "reload" else "post_status"]
        if isinstance(response, BaseException):
            raise response  # pylint: disable=raising-bad-type
        return response

    def cache(pid, *, instance):
        assert pid == 123 and instance is controls["post_status"]["instances"][0]
        phase("cache")
        return Path("missing-cache.abf")

    def port(pid):
        assert pid == 123
        phase("port")
        return 49157

    def bind(pid, *, supplied_port):
        assert pid == 123 and supplied_port == 49157
        phase("bind")
        return context.bound

    def identity(*_args):
        phase("identity")
        return True

    original_snapshot = refresh_pbip_model.read_partition_snapshot

    def snapshot(*args, **kwargs):
        phase("baseline")
        return original_snapshot(*args, **kwargs)

    for name in ("_resolve_pid", "_instance", "_bridge_status", "_refresh_and_save", "save"):
        monkeypatch.setattr(refresh_pbip_model, name, lambda *_args: pytest.fail("legacy UI path"))
    monkeypatch.setattr(refresh_pbip_model, "_wait_ui_bridge", wait)
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", observe)
    monkeypatch.setattr(refresh_pbip_model, "_ui_bridge_status", bridge)
    monkeypatch.setattr(refresh_pbip_model, "cache_file", cache)
    monkeypatch.setattr(refresh_pbip_model, "discover_port", port)
    monkeypatch.setattr(refresh_pbip_model, "bind_desktop", bind)
    monkeypatch.setattr(refresh_pbip_model, "_identity_gate", identity)
    monkeypatch.setattr(refresh_pbip_model, "read_partition_snapshot", snapshot)
    monkeypatch.setattr(refresh_pbip_model, "source_hint_from_model", lambda _path: None)
    return controls, events, actions_seen


@pytest.mark.parametrize("no_save", [False, True])
def test_clean_start_precedes_all_model_binding_baselines_and_existing_ui_actions(
    clean_start_route, monkeypatch: pytest.MonkeyPatch, no_save: bool
) -> None:
    controls, events, actions_seen = clean_start_route
    before = controls["pre_observer"]
    after = replace(before, target_window=replace(before.target_window, hwnd=790))
    controls["post_observer"] = after
    downstream = refresh_pbip_model._ui_refresh_and_save

    def run(*args, **kwargs):
        assert args[4] is after, "stale pre-reload observer handed downstream"
        assert events == [
            "initial",
            "pre_observer",
            "reload",
            "post_status",
            "post_observer",
            "cache",
            "port",
            "identity",
        ]
        return downstream(*args, **kwargs)

    monkeypatch.setattr(refresh_pbip_model, "_ui_refresh_and_save", run)
    assert refresh_pbip_model.main(["--pid", "123", "--ui-refresh", *(["--no-save"] if no_save else [])]) == 0
    assert events[:10] == [
        "initial",
        "pre_observer",
        "reload",
        "post_status",
        "post_observer",
        "cache",
        "port",
        "identity",
        "bind",
        "identity",
    ]
    assert events.count("baseline") == 1
    assert events.count("reload") == 1 and events.count("post_status") == 1
    assert actions_seen == ["Refresh", "canaries", *([] if no_save else ["Save"])]


@pytest.mark.parametrize("stage", ["initial", "post_status"])
@pytest.mark.parametrize("dirty", [True, None, "missing", 0, 1, "", "false", [], {}])
def test_clean_start_requires_strict_false_before_and_after_reload(
    clean_start_route, stage: str, dirty: object
) -> None:
    controls, events, actions_seen = clean_start_route
    instance = controls["initial"] if stage == "initial" else controls["post_status"]["instances"][0]
    if dirty == "missing":
        instance.pop("hasUnsavedChanges")
    else:
        instance["hasUnsavedChanges"] = dirty
    assert refresh_pbip_model.main(["--pid", "123", "--ui-refresh"]) != 0, "clean-state gate accepted unsafe state"
    assert events == (["initial"] if stage == "initial" else ["initial", "pre_observer", "reload", "post_status"]), (
        "unsafe clean-state sequence"
    )
    assert actions_seen == [], "Refresh/Save must be withheld when clean state is not proven"


@pytest.mark.parametrize(
    "bad",
    [
        "error",
        "status",
        "pid",
        "pid_bool",
        "pid_float",
        "pid_missing",
        "result",
        "failed",
        "truthy_success",
        "missing_success",
        "result_error",
        "result_errors",
        "top_errors",
        "malformed",
        "timeout",
    ],
)
def test_clean_start_refuses_failed_or_malformed_reload_without_post_status_or_actions(
    clean_start_route, bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    controls, events, actions_seen = clean_start_route
    payload = controls["reload"]
    if bad == "error":
        payload["error"] = {"message": "PRIVATE RAW ERROR"}
    elif bad == "status":
        payload["status"] = "error"
    elif bad in {"pid", "pid_bool", "pid_float"}:
        payload["pid"] = {"pid": 456, "pid_bool": True, "pid_float": 123.0}[bad]
    elif bad == "pid_missing":
        payload.pop("pid")
    elif bad == "result":
        payload["result"] = []
    elif bad in {"failed", "truthy_success"}:
        payload["result"]["success"] = 1 if bad == "truthy_success" else False
    elif bad == "missing_success":
        payload["result"].pop("success")
    elif bad in {"result_error", "result_errors"}:
        payload["result"]["error" if bad == "result_error" else "errors"] = "PRIVATE RAW ERROR"
    elif bad == "top_errors":
        payload["errors"] = ["PRIVATE RAW ERROR"]
    else:
        controls["reload"] = TimeoutError() if bad == "timeout" else DesktopActionError("PRIVATE RAW ERROR")
    assert refresh_pbip_model.main(["--pid", "123", "--ui-refresh"]) != 0, "invalid reload was accepted"
    assert events == ["initial", "pre_observer", "reload"], "invalid reload was accepted"
    assert not actions_seen, "invalid reload must withhold Refresh/Save"
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad", ["pid", "pid_bool", "file", "relative", "status", "connected", "pending", "error", "duplicate"]
)
def test_clean_start_requires_one_post_status_same_exact_absolute_file_and_pid(clean_start_route, bad: str) -> None:
    controls, events, actions_seen = clean_start_route
    payload = controls["post_status"]
    instance = payload["instances"][0]
    if bad in {"pid", "pid_bool"}:
        instance["pid"] = 456 if bad == "pid" else True
    elif bad in {"file", "relative"}:
        instance["currentFilePath"] = str(Path.cwd() / "other.pbip") if bad == "file" else "model.pbip"
    elif bad == "status":
        payload["status"] = "error"
    elif bad == "connected":
        instance["bridgeStatus"] = "not_connected"
    elif bad == "pending":
        controls["post_status"] = {"status": "not_connected", "instances": []}
    elif bad == "error":
        instance["error"] = {"message": "PRIVATE RAW ERROR"}
    else:
        payload["instances"].append(dict(instance))
    assert refresh_pbip_model.main(["--pid", "123", "--ui-refresh"]) != 0, "post-status mismatch was accepted"
    assert events == ["initial", "pre_observer", "reload", "post_status"], "post status was not enforced"
    assert not actions_seen, "post status mismatch must withhold Refresh/Save"


@pytest.mark.parametrize("path", ["model.pbip", "", "\x00", None])
def test_clean_start_refuses_unestablished_initial_absolute_path(clean_start_route, path: object) -> None:
    controls, events, actions_seen = clean_start_route
    controls["initial"]["currentFilePath"] = path
    assert refresh_pbip_model.main(["--pid", "123", "--ui-refresh"]) != 0
    assert events == ["initial"] and not actions_seen


@pytest.mark.parametrize("stage", ["pre_observer", "post_observer"])
@pytest.mark.parametrize(
    "bad",
    [
        "credential",
        "prompt",
        "unknown",
        "incomplete",
        "progress",
        "pid",
        "start",
        "missing_start",
        "bad_start",
        "root_start",
    ],
)
def test_clean_start_observers_refuse_prompts_uncertainty_and_identity_changes(
    clean_start_route, stage: str, bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    controls, events, actions_seen = clean_start_route
    state = controls[stage]
    if bad in {"credential", "prompt", "unknown", "progress"}:
        window = DesktopWindow("PRIVATE UI TEXT", "", 100, 100, hwnd=790, owner_hwnd=789)
        if bad == "credential":
            state = replace(state, modal=CredentialModal("PRIVATE CREDENTIAL", window))
        else:
            kind, verdict = {
                "prompt": ("needs-human", "DIALOG_NEEDS_HUMAN"),
                "unknown": ("unrecognized", "DIALOG_UNRECOGNIZED"),
                "progress": ("benign", "REFRESH_IN_PROGRESS"),
            }[bad]
            state = replace(state, dialog=DialogFinding(kind, verdict, window))
    elif bad == "incomplete":
        state = replace(state, complete=False)
    elif bad == "pid":
        state = replace(state, pid="456")
    elif bad in {"start", "missing_start", "bad_start"}:
        state = replace(state, process_started_at={"start": "457", "missing_start": None, "bad_start": "0"}[bad])
        if stage == "pre_observer" and bad == "start":
            state = replace(state, target_window=replace(state.target_window, process_started_at="457"))
            # The post observation must match the pre-reload process, not merely its original PID.
            controls["post_observer"] = replace(controls["post_observer"], process_started_at="456")
    else:
        state = replace(state, target_window=replace(state.target_window, process_started_at="457"))
    controls[stage] = state
    result = refresh_pbip_model.main(["--pid", "123", "--ui-refresh"])
    assert result != 0, "unsafe observer was accepted"
    expected = ["initial", "pre_observer"]
    if stage == "post_observer" or (stage == "pre_observer" and bad == "start"):
        expected += ["reload", "post_status", "post_observer"]
    assert events == expected, "observer refusal did not stop the next phase"
    assert not actions_seen, "observer refusal must withhold Refresh/Save"
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    if bad == "prompt":
        assert "DIALOG_NEEDS_HUMAN" in output
    elif bad == "credential":
        assert result == 1 and "CREDENTIAL_MISSING" in output


@pytest.mark.parametrize("stage", ["initial", "pre_observer", "reload", "post_status", "post_observer"])
@pytest.mark.parametrize("overall_seconds", [10, 3600])
def test_clean_start_all_startup_phases_share_original_clipped_deadline(
    clean_start_route, stage: str, overall_seconds: int, capsys: pytest.CaptureFixture[str]
) -> None:
    controls, events, actions_seen = clean_start_route
    controls["late"] = stage
    controls["deadline"] = 110.0 if overall_seconds == 10 else 160.0
    result = refresh_pbip_model.main(
        ["--pid", "123", "--ui-refresh", "--refresh-absolute-timeout-seconds", str(overall_seconds)]
    )
    phases = ["initial", "pre_observer", "reload", "post_status", "post_observer"]
    assert result == 3
    assert events == phases[: phases.index(stage) + 1], "late result authorized the next startup phase"
    assert not actions_seen, "startup timeout must withhold Refresh/Save"
    assert "REFRESH: TIMEOUT" in capsys.readouterr().out


@pytest.mark.parametrize("operation", ["status", "reload"])
def test_bridge_status_uses_installed_only_exact_pid_cli_with_original_budget(
    monkeypatch: pytest.MonkeyPatch, bridge_clock: list[float], operation: str
) -> None:
    seen = []

    def spawn(arguments, **kwargs):
        seen.append((arguments, kwargs))
        bridge_clock[0] += 2
        return SimpleNamespace(
            returncode=0,
            stdout=None,
            communicate=lambda timeout: (
                (json.dumps(_ready_bridge()), None) if timeout == 8 else pytest.fail("budget reset")
            ),
        )

    monkeypatch.setattr(refresh_pbip_model.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        refresh_pbip_model,
        "_BridgeStatusJob",
        lambda: SimpleNamespace(start=lambda _child: None, close=lambda: None),
    )
    command = [str(Path.cwd() / "powerbi-desktop.cmd")] if os.name == "nt" else ["powerbi-desktop"]
    assert refresh_pbip_model._ui_bridge_status(command, 123, 110, operation=operation) == _ready_bridge()
    arguments, kwargs = seen[0]
    if os.name == "nt":
        assert '/d /v:off /s /c ""%PBIP_UI_BRIDGE_ARG_0%" ' in arguments
        rendered = [kwargs["env"][f"PBIP_UI_BRIDGE_ARG_{index}"] for index in range(6 if operation == "status" else 4)]
    else:
        rendered = arguments
    assert rendered == [*command, operation, "--pid", "123"] + (
        ["--wait-seconds", "10.0"] if operation == "status" else []
    )
    assert not kwargs.get("shell")


@pytest.fixture(name="installed_bridge", params=["bridge & (x) %PATH% !bang! ^ caret", "bridge&(%PATH%)!bang!^"])
def _installed_bridge(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if os.name != "nt":
        pytest.skip("real installed Windows batch launcher control")
    directory = tmp_path / request.param
    directory.mkdir()
    launcher = directory / "powerbi-desktop.cmd"
    launcher.write_text(f'@"{sys.executable}" "%~dp0bridge.py" %*\n', encoding="utf-8")
    script = directory / "bridge.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "with pathlib.Path(__file__).with_suffix('.calls').open('a', encoding='utf-8') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(os.environ['PBIP_TEST_BRIDGE_RESPONSE'] if sys.argv[1] == 'status' else "
        '\'{"status":"ok","pid":123,"result":{"success":true}}\')\n'
        "sys.exit(int(os.environ.get('PBIP_TEST_BRIDGE_EXIT', '0')))\n",
        encoding="utf-8",
    )
    fallback = directory / "fallback"
    (directory / "npx.cmd").write_text(f'@echo unexpected>"{fallback}"\n@exit /b 1\n', encoding="utf-8")
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.setenv("PBIP_TEST_BRIDGE_RESPONSE", json.dumps(_ready_bridge()))
    monkeypatch.setenv("PBIP_TEST_BRIDGE_EXIT", "0")
    monkeypatch.setattr(refresh_pbip_model, "_ui_desktop_alive", lambda _pid: True)
    return launcher, script.with_suffix(".calls"), fallback


def test_installed_launcher_drives_real_readiness_reload_and_post_status_without_npx(
    installed_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    _launcher, calls_path, fallback = installed_bridge
    deadline = time.monotonic() + 15
    instance = refresh_pbip_model._ui_bridge_preflight(123, deadline)
    assert instance is not None, "installed launcher failed exact-PID readiness"
    root = DesktopWindow("", "", 100, 100, hwnd=789, pid="123", process_started_at="456")
    state = Observation(pid="123", process_started_at="456", target_window=root)
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid, **_kwargs: state)
    assert refresh_pbip_model._ui_clean_start(123, instance, deadline) == (instance, state)
    calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    assert [call[:3] for call in calls] == [
        ["status", "--pid", "123"],
        ["reload", "--pid", "123"],
        ["status", "--pid", "123"],
    ]
    assert calls[1] == ["reload", "--pid", "123"]
    assert all(call[3] == "--wait-seconds" and 0 < float(call[4]) <= 15 for call in (calls[0], calls[2]))
    assert not fallback.exists(), "installed launcher must not use npx"


@pytest.mark.parametrize("refusal", ["exit", "semantic", "wrong_pid"])
def test_installed_launcher_refusal_does_not_retry_or_fall_back(
    installed_bridge, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    _launcher, calls_path, fallback = installed_bridge
    payload = _ready_bridge()
    if refusal == "exit":
        monkeypatch.setenv("PBIP_TEST_BRIDGE_EXIT", "1")
    elif refusal == "semantic":
        payload["error"] = {"code": "CREDENTIAL_REQUIRED"}
    else:
        payload["instances"][0]["pid"] = 456
    monkeypatch.setenv("PBIP_TEST_BRIDGE_RESPONSE", json.dumps(payload))
    with pytest.raises(DesktopActionError):
        refresh_pbip_model._wait_ui_bridge(123, time.monotonic() + 15)
    calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1 and calls[0][:3] == ["status", "--pid", "123"], "refusal retried"
    assert not fallback.exists(), "refusal selected a different launcher"


def test_absent_installed_launcher_refuses_without_spawning_npm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(refresh_pbip_model, "_ui_desktop_alive", lambda _pid: True)
    monkeypatch.setattr(
        refresh_pbip_model.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("absent launcher spawned")
    )
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda _pid: Observation())
    assert refresh_pbip_model._ui_bridge_preflight(123, time.monotonic() + 10) is None
    assert "UI_PREFLIGHT_REFUSED" in capsys.readouterr().out


@pytest.mark.parametrize("late_phase", ["launch", "output", pytest.param("cleanup", marks=WINDOWS_ONLY)])
@pytest.mark.parametrize("operation", ["status", "reload"])
def test_startup_transport_rejects_late_launch_output_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, bridge_clock: list[float], late_phase: str, operation: str
) -> None:
    def phase(name):
        if late_phase == name:
            bridge_clock[0] = 110

    def communicate(**_kwargs):
        phase("output")
        return json.dumps(_ready_bridge()), None

    child = SimpleNamespace(returncode=0, stdout=None, communicate=communicate, kill=lambda: None)

    def spawn(*_args, **_kwargs):
        phase("launch")
        return child

    monkeypatch.setattr(refresh_pbip_model.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        refresh_pbip_model,
        "_BridgeStatusJob",
        lambda: SimpleNamespace(start=lambda _child: None, close=lambda: phase("cleanup")),
    )
    with pytest.raises(TimeoutError):
        refresh_pbip_model._ui_bridge_status(["controlled-child"], 123, 110, operation=operation)


@pytest.mark.parametrize("operation", ["status", "reload"])
@pytest.mark.parametrize("output,exit_code", [("not json", 0), ("[]", 0), ('{"status":"ready"}', 1)])
def test_bridge_status_child_refuses_malformed_json_and_nonzero_exit(
    output: str, exit_code: int, operation: str
) -> None:
    script = f"import sys; print({output!r}); sys.exit({exit_code})"
    with pytest.raises(DesktopActionError):
        refresh_pbip_model._ui_bridge_status(
            [sys.executable, "-c", script], 123, time.monotonic() + 10, operation=operation
        )


def test_bridge_status_child_timeout_reaps_real_direct_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    children = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        # Ownership transfers to the product timeout handler, not this intercepting spy.
        child = original(*args, **kwargs)  # pylint: disable=consider-using-with
        children.append(child)
        return child

    monkeypatch.setattr(refresh_pbip_model.subprocess, "Popen", spawn)
    with pytest.raises(TimeoutError):
        refresh_pbip_model._ui_bridge_status(
            [sys.executable, "-c", "import time; time.sleep(120)"], 123, time.monotonic() + 0.25
        )
    assert children and all(child.poll() is not None for child in children)


@WINDOWS_ONLY
@pytest.mark.parametrize("failure", ["AssignProcessToJobObject", "CreateToolhelp32Snapshot", "ResumeThread"])
def test_bridge_status_containment_start_failure_reaps_suspended_child_before_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    job = refresh_pbip_model._BridgeStatusJob()
    original = subprocess.Popen
    children = []
    marker = tmp_path / "must-not-execute"

    def spawn(*args, **kwargs):
        assert kwargs["creationflags"] & 0x4
        child = original(*args, **kwargs)  # pylint: disable=consider-using-with
        children.append(child)
        return child

    def fail(*_args):
        ctypes.set_last_error(5)
        return (
            ctypes.c_void_p(-1).value
            if failure == "CreateToolhelp32Snapshot"
            else 0xFFFFFFFF
            if failure == "ResumeThread"
            else 0
        )

    monkeypatch.setattr(job.kernel, failure, fail)
    monkeypatch.setattr(refresh_pbip_model, "_BridgeStatusJob", lambda: job)
    monkeypatch.setattr(refresh_pbip_model.subprocess, "Popen", spawn)
    script = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('executed')"
    try:
        with pytest.raises(OSError):
            refresh_pbip_model._ui_bridge_status([sys.executable, "-c", script], 123, time.monotonic() + 10)
        assert len(children) == 1 and children[0].poll() is not None
        assert children[0].stdout.closed
        assert not marker.exists(), "uncontained launcher executed before assignment/resume"
    finally:
        job.close()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=2)


@WINDOWS_ONLY
@pytest.mark.parametrize("failure", ["TerminateJobObject", "QueryInformationJobObject"])
def test_bridge_status_never_accepts_ready_json_when_job_cleanup_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, failure: str, capsys: pytest.CaptureFixture[str]
) -> None:
    job = refresh_pbip_model._BridgeStatusJob()

    def fail(*_args):
        ctypes.set_last_error(5)
        return 0

    monkeypatch.setattr(job.kernel, failure, fail)
    monkeypatch.setattr(refresh_pbip_model, "_BridgeStatusJob", lambda: job)
    script = f"print({json.dumps(_ready_bridge())!r})"
    with pytest.raises(DesktopActionError, match="cleanup"):
        refresh_pbip_model._ui_bridge_status([sys.executable, "-c", script], 123, time.monotonic() + 10)
    assert "cleanup unconfirmed" in capsys.readouterr().out


@WINDOWS_ONLY
def test_bridge_status_job_does_not_terminate_unassigned_sibling() -> None:
    with subprocess.Popen(
        [sys._base_executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as sibling:
        try:
            script = f"print({json.dumps(_ready_bridge())!r})"
            assert (
                refresh_pbip_model._ui_bridge_status([sys.executable, "-c", script], 123, time.monotonic() + 10)
                == _ready_bridge()
            )
            assert sibling.poll() is None, "unassigned process was terminated"
        finally:
            sibling.kill()
            sibling.wait(timeout=2)


def test_bridge_status_cleanup_failure_refuses_without_unbounded_pipe_close(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def blocked(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("owned-test-child", 1)

    child = SimpleNamespace(
        pid=123,
        returncode=None,
        communicate=blocked,
        kill=lambda: None,
        stdout=SimpleNamespace(close=lambda: pytest.fail("unbounded close of blocked reader")),
    )
    monkeypatch.setattr(refresh_pbip_model.subprocess, "Popen", lambda *_args, **_kwargs: child)
    job = SimpleNamespace(start=lambda _child: None, close=blocked)
    monkeypatch.setattr(refresh_pbip_model, "_BridgeStatusJob", lambda: job)
    with pytest.raises(DesktopActionError, match="cleanup"):
        refresh_pbip_model._ui_bridge_status(["controlled-child"], 123, time.monotonic() + 10)
    assert "cleanup unconfirmed" in capsys.readouterr().out


@WINDOWS_ONLY
@pytest.mark.parametrize("launcher_exits", [False, True], ids=["living-launcher", "exited-launcher"])
@pytest.mark.parametrize("operation", ["status", "reload"])
def test_bridge_status_timeout_kills_owned_launcher_and_descendant_without_gui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, launcher_exits: bool, operation: str
) -> None:
    # Independent held handles establish both the setup state and actual termination.
    # pylint: disable=too-many-statements,too-many-locals
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    marker = tmp_path / "owned-worker.pid"
    launcher_marker = tmp_path / "owned-launcher.pid"
    # Existence is the setup signal: publish the complete PID atomically, never an empty open file.
    worker_script = (
        f"import os,pathlib,time; marker=pathlib.Path({str(marker)!r});"
        "staged=marker.with_suffix('.ready'); staged.write_text(str(os.getpid()));"
        "staged.replace(marker); time.sleep(120)"
    )
    script = (
        "import os,pathlib,subprocess,sys,time;"
        f"pathlib.Path({str(launcher_marker)!r}).write_text(str(os.getpid()));"
        f"subprocess.Popen([sys.executable,'-c',{worker_script!r}]);"
        + ("sys.exit(0)" if launcher_exits else "time.sleep(120)")
    )
    original = subprocess.Popen
    launchers = []
    handles = []

    def spawn(arguments, **kwargs):
        # Ownership transfers to the product timeout handler, not this intercepting spy.
        child = original(arguments, **kwargs)  # pylint: disable=consider-using-with
        if arguments[0] != sys.executable:
            return child
        launchers.append(child)
        communicate = child.communicate

        def synchronized(timeout=None):
            if not handles:
                limit = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < limit:
                    threading.Event().wait(0.01)
                assert marker.exists(), "controlled descendant did not start"
                worker = kernel.OpenProcess(0x00100001, False, int(marker.read_text(encoding="utf-8")))
                assert worker, "cannot hold controlled worker identity"
                handles.append(worker)
                assert kernel.WaitForSingleObject(worker, 0) == 258, "worker not alive at setup"
                launcher = kernel.OpenProcess(0x00100000, False, int(launcher_marker.read_text(encoding="utf-8")))
                if launcher:
                    try:
                        assert kernel.WaitForSingleObject(launcher, 2000 if launcher_exits else 0) == (
                            0 if launcher_exits else 258
                        ), "controlled launcher did not reach requested state"
                    finally:
                        kernel.CloseHandle(launcher)
                else:
                    assert launcher_exits and ctypes.get_last_error() == 87
                timeout = 0.5  # The worker is alive and the requested launcher state is now established.
            return communicate(timeout=timeout)

        child.communicate = synchronized
        return child

    monkeypatch.setattr(refresh_pbip_model.subprocess, "Popen", spawn)
    try:
        with pytest.raises((TimeoutError, DesktopActionError)) as caught:
            refresh_pbip_model._ui_bridge_status(
                [sys.executable, "-c", script], 123, time.monotonic() + 15, operation=operation
            )
        assert len(launchers) == 1 and launchers[0].poll() is not None
        assert len(handles) == 1
        assert kernel.WaitForSingleObject(handles[0], 2000) == 0, "owned worker survived timeout cleanup"
        assert isinstance(caught.value, TimeoutError)
    finally:
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                kernel.TerminateProcess(handle, 1)
                assert kernel.WaitForSingleObject(handle, 2000) == 0
            kernel.CloseHandle(handle)
        for child in launchers:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=2)


@pytest.mark.parametrize("value", [True, 1.5, Decimal("1.5"), "1"])
def test_engine_integer_fields_refuse_nonintegral_or_display_values(value: object) -> None:
    with pytest.raises(DesktopActionError):
        actions._integer(value, "partition identity")


class _Reader:  # pylint: disable=invalid-name,too-few-public-methods
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.index = -1

    def Read(self) -> bool:
        self.index += 1
        return self.index < len(self.rows)

    def IsDBNull(self, index: int) -> bool:
        return self.rows[self.index][index] is None

    def GetValue(self, index: int) -> object:
        return self.rows[self.index][index]

    def Close(self) -> None:
        return None


class _Command:  # pylint: disable=invalid-name,too-few-public-methods
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.CommandText = ""

    def ExecuteReader(self) -> _Reader:
        if "TMSCHEMA_MODEL" in self.CommandText:
            return _Reader([(self.connection.default_mode,)])
        if "TMSCHEMA_TABLES" in self.CommandText:
            assert "[StorageMode]" not in self.CommandText, "StorageMode is not a TMSCHEMA_TABLES column"
            return _Reader([row[:2] for row in self.connection.tables])
        if "TMSCHEMA_PARTITIONS" in self.CommandText:
            return _Reader(self.connection.partitions)
        raise AssertionError(f"unexpected metadata query: {self.CommandText}")


class _Connection:  # pylint: disable=invalid-name,too-few-public-methods
    def __init__(
        self,
        tables: list[tuple[object, ...]],
        partitions: list[tuple[object, ...]],
        default_mode: object = 0,
    ) -> None:
        self.tables = tables
        self.partitions = partitions
        self.default_mode = default_mode
        self.Database = "00000000-0000-0000-0000-000000000001"

    def CreateCommand(self) -> _Command:
        return _Command(self)


def _partition(  # pylint: disable=too-many-arguments
    partition_id: int = 59,
    *,
    table_id: int = 10,
    state: int = 1,
    kind: int = 4,
    mode: object = 0,
    refreshed: object = BASELINE,
    query: object = "let Source = Csv.Document(...) in Source",
) -> tuple[object, ...]:
    return (partition_id, table_id, state, kind, mode, refreshed, query)


def _snapshot(
    *,
    partition_id: int = 59,
    table_id: int = 10,
    ticks: int = 1,
    state: int = 1,
) -> PartitionSnapshot:
    return PartitionSnapshot(
        (
            SourcePartition(
                catalogue="catalog-1",
                table_id=table_id,
                partition_id=partition_id,
                source_kind="M",
                effective_mode="Import",
                state=state,
                refreshed_ticks=ticks,
            ),
        )
    )


def _action_payload(**changes: object) -> str:
    payload: dict[str, object] = {
        "schema": "pbip.desktop-action.v1",
        "status": "invoked",
        "action": "Refresh",
        "desktop_pid": "123",
        "process_started_at": "456",
        "root_hwnd": "789",
        "invoked": True,
        "reason": "invoke_returned",
        "focus_attempts": 2,
        "action_started_ticks": "638972496000000000",
        "restore_requested": True,
        "restore_verified": True,
        "handback_ok": True,
    }
    payload.update(changes)
    return json.dumps(payload)


def test_snapshot_resolves_inherited_mode_from_table_metadata() -> None:
    connection = _Connection(
        tables=[(10, "Orders", None)],
        partitions=[_partition(mode=2)],
        default_mode=0,
    )
    result = _snapshot_from_connection(connection, "catalog-1")

    assert result.partitions == (
        SourcePartition(
            catalogue="catalog-1",
            table_id=10,
            partition_id=59,
            source_kind="M",
            effective_mode="Import",
            state=1,
            refreshed_ticks=(BASELINE.toordinal() - 1) * 864_000_000_000
            + (BASELINE.hour * 3600 + BASELINE.minute * 60) * 10_000_000,
        ),
    )


@pytest.mark.parametrize(
    "query",
    [
        "Value.NativeQuery(Source, Query)",
        'Sql.Database("server","database",[CreateNavigationProperties=false,Query="SELECT * FROM Orders"])',
        'Sql.Database("server","database",[CommandTimeout=#duration(0,0,1,0),Query="SELECT 1"])',
        'Sql.Database("server","database",[CreateNavigationProperties=false,Query /* option */ = "SELECT 1"])',
        'let options = [CreateNavigationProperties=false, #"Query"="SELECT 1"] '
        'in Sql.Database("server","database", options)',
    ],
)
@pytest.mark.parametrize("no_save", [False, True])
def test_explicit_ui_refresh_executes_current_m_including_custom_sql(
    monkeypatch: pytest.MonkeyPatch, query: str, no_save: bool
) -> None:
    context, args, calls = _wire_ui_route(monkeypatch, no_save=no_save)
    connection = _Connection([(10, "Orders", 0)], [_partition(query=query)])
    monkeypatch.setattr(
        refresh_pbip_model,
        "read_partition_snapshot",
        lambda *_args, **_kwargs: _snapshot_from_connection(connection, "catalog-1"),
    )

    requested = refresh_pbip_model._build_arg_parser().parse_args(["--ui-refresh", *(["--no-save"] if no_save else [])])
    assert requested.ui_refresh
    args.no_save = requested.no_save
    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 0
    assert calls == ["Refresh", "canaries"] + ([] if no_save else ["Save"])


@pytest.mark.parametrize("final_state", ["READY", "REFRESH_IN_PROGRESS", "UNKNOWN", "PROMPT"])
def test_completion_awaits_real_bound_reader_progress_without_suppressing_refusals(
    monkeypatch: pytest.MonkeyPatch, final_state: str
) -> None:
    identity = query_probe.DesktopIdentity(123, "456", 124, "457", 49157)
    bound = query_probe.BoundDesktop(identity, "00000000-0000-0000-0000-000000000001")
    root = DesktopWindow("", "", 100, 100, hwnd=789, pid="123", process_started_at="456")
    ready = Observation(pid="123", process_started_at="456", target_window=root)
    progress = Observation(
        pid="123",
        process_started_at="456",
        target_window=root,
        dialog=DialogFinding("benign", "REFRESH_IN_PROGRESS", root),
    )
    reads = []
    connection = _Connection([(10, "Orders", 0)], [_partition()])
    monkeypatch.setattr(connection, "Open", lambda: reads.append("open"), raising=False)
    monkeypatch.setattr(connection, "Close", lambda: None, raising=False)
    monkeypatch.setattr(query_probe, "desktop_identity", lambda *_args: identity)
    monkeypatch.setattr(query_probe, "catalogue_id", lambda *_args, **_kwargs: bound.catalogue)
    monkeypatch.setattr(query_probe, "_load_adomd", lambda: lambda *_args: connection)
    # Keep the real bound_call/_observation_call reader, preflight and error translation.
    # Only the OS observation transport and ADOMD connection are synthetic.
    observed = []

    def observe(*_args, **_kwargs):
        observed.append("poll")
        if len(observed) == 1 or final_state == "REFRESH_IN_PROGRESS":
            return progress
        if final_state == "UNKNOWN":
            return Observation(complete=False, unknown_reason="incomplete")
        if final_state == "PROMPT":
            return Observation(dialog=DialogFinding("needs-human", "DIALOG_NEEDS_HUMAN", root))
        return ready

    monkeypatch.setattr(query_probe, "_credential_state", observe)
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", lambda *_args, **_kwargs: ready)
    context = UIRefreshContext(123, 49157, bound, 789, None)
    baseline = _snapshot_from_connection(connection, bound.catalogue)
    connection.partitions = [_partition(refreshed=datetime(2026, 10, 28, 12, 1, tzinfo=timezone.utc))]
    if final_state in {"UNKNOWN", "PROMPT"}:
        with pytest.raises(query_probe.ObservationUnavailable) as failure:
            refresh_pbip_model._wait_for_engine_completion(
                context, baseline, baseline.partitions[0].refreshed_ticks + 1, time.monotonic() + 2.5
            )
        assert str(failure.value) == ("DIALOG_UNREADABLE" if final_state == "UNKNOWN" else "DIALOG_NEEDS_HUMAN")
        assert not reads
    else:
        complete, _ = refresh_pbip_model._wait_for_engine_completion(
            context, baseline, baseline.partitions[0].refreshed_ticks + 1, time.monotonic() + 2.5
        )
        assert complete is (final_state == "READY")
        assert bool(reads) is complete
    assert len(observed) >= (2 if final_state != "REFRESH_IN_PROGRESS" else 1)


@pytest.mark.parametrize("phase", ["initial", "poll"])
@pytest.mark.parametrize("finding_kind", ["unreadable", "unrecognized", "needs-human", "no_finding", "progress"])
def test_ui_completion_reports_held_finding_through_real_bound_reader(  # pylint: disable=too-many-locals,too-many-statements
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    finding_kind: str,
) -> None:
    """Keep the caller, completion wait and supervised bound reads; replace only external I/O."""
    identity = query_probe.DesktopIdentity(123, "456", 124, "457", 49157)
    bound = query_probe.BoundDesktop(identity, "00000000-0000-0000-0000-000000000001")
    root = DesktopWindow("", "", 100, 100, hwnd=789, pid="123", process_started_at="456")
    ready = Observation(pid="123", process_started_at="456", target_window=root)
    window = DesktopWindow("", "", 100, 100, hwnd=888, owner_hwnd=789, complete=False, reason="patterns_incomplete")
    tokens = {
        "unreadable": "DIALOG_UNREADABLE",
        "unrecognized": "DIALOG_UNRECOGNIZED",
        "needs-human": "DIALOG_NEEDS_HUMAN",
    }
    finding = DialogFinding(finding_kind, tokens.get(finding_kind, "REFRESH_IN_PROGRESS"), window)
    blocked = Observation(
        pid="123",
        process_started_at="456",
        target_window=root,
        dialog=None if finding_kind == "no_finding" else finding,
        complete=False,
        unknown_reason="backend_failed" if finding_kind == "no_finding" else None,
    )
    progress = Observation(
        pid="123",
        process_started_at="456",
        target_window=root,
        dialog=DialogFinding("benign", "REFRESH_IN_PROGRESS", root),
    )
    calls = []
    completion_polls = []
    read_released = threading.Event()
    diagnostics = []
    connection = _Connection([(10, "Orders", 0)], [_partition()])

    def open_connection() -> None:
        if "Refresh" in calls and phase == "poll":
            assert read_released.wait(2), "completion observer never released the controlled engine read"

    def observe(*_args, **_kwargs):
        if "Refresh" not in calls:
            return ready
        completion_polls.append("poll")
        if phase == "poll" and len(completion_polls) == 1:
            return ready
        read_released.set()
        if finding_kind == "progress":
            return progress if len(completion_polls) == 1 else ready
        return blocked

    def invoke(action, **_kwargs):
        calls.append(action)
        # Every partition really advances on the mock engine, even in the refusal cases.
        connection.partitions = [_partition(refreshed=datetime(2026, 10, 28, 12, 1, tzinfo=timezone.utc))]
        return ActionResult(action, True, "invoke_returned", 1, baseline_ticks + 1, False, "not_requested")

    baseline_ticks = actions._typed_utc_ticks(BASELINE)
    monkeypatch.setattr(connection, "Open", open_connection, raising=False)
    monkeypatch.setattr(connection, "Close", lambda: None, raising=False)
    monkeypatch.setattr(query_probe, "desktop_identity", lambda *_args: identity)
    monkeypatch.setattr(query_probe, "catalogue_id", lambda *_args, **_kwargs: bound.catalogue)
    monkeypatch.setattr(query_probe, "_load_adomd", lambda: lambda *_args: connection)
    monkeypatch.setattr(query_probe, "_credential_state", lambda *_args, **_kwargs: ready)
    monkeypatch.setattr(refresh_pbip_model, "_credential_state", observe)
    monkeypatch.setattr(refresh_pbip_model, "REFRESH_CREDENTIAL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(refresh_pbip_model, "_identity_gate", lambda *_args: True)
    monkeypatch.setattr(refresh_pbip_model, "invoke_desktop_action", invoke)
    monkeypatch.setattr(
        refresh_pbip_model,
        "probe_observations",
        lambda *_args, **_kwargs: calls.append("canaries") or [SimpleNamespace(table="Orders", returned_rows=1)],
    )
    monkeypatch.setattr(refresh_pbip_model, "_emit_data_verdict", lambda *_args: 0)
    monkeypatch.setattr(
        sys.modules["_desktop_observer"],
        "print_dialog_diagnostic",
        lambda pid, target: diagnostics.append((pid, target)),
    )
    args = SimpleNamespace(refresh_absolute_timeout_seconds=4, no_save=False, canaries=["Orders"], tables=None)
    result = refresh_pbip_model._ui_refresh_locked(
        UIRefreshContext(123, 49157, bound, 789, None), Path("missing-cache.abf"), args
    )
    output = capsys.readouterr().out
    if finding_kind == "progress":
        assert result == 0, output
        assert calls == ["Refresh", "canaries", "Save"], output
        assert "engine completion: all source partitions READY" in output
        assert not diagnostics
    else:
        assert result == 3, output
        assert calls == ["Refresh"], output
        token = tokens.get(finding_kind, "CREDENTIAL_UNKNOWN")
        assert f"REFRESH: {token} pid=123;" in output
        assert diagnostics == ([] if finding_kind == "no_finding" else [(123, window)])
        assert "to the deadline" not in output
        if finding_kind == "no_finding":
            assert "Completion observation unavailable" in output
            assert "credential state unestablished" in output
        assert "engine completion:" not in output


@pytest.mark.parametrize("change", ["foreground", "identity", "none"])
@WINDOWS_ONLY
def test_final_action_acquisition_rechecks_focus_and_identity_without_windows(change: str) -> None:
    source = actions.ACTION_SCRIPT.read_text(encoding="utf-8")
    body = source[
        source.rindex("  $targetControl = Get-ActionControl $candidates") : source.index("  $invoked = $true")
    ]
    body = body.replace("[System.Windows.Automation.InvokePattern]::Pattern", "'Invoke'")
    body = body.replace("[DesktopActionNative]::GetForegroundWindow()", "$script:foreground")
    comparison_start = body.index("  $comparison =")
    comparison_end = body.index("    $reason = 'selected_control_changed'", comparison_start)
    body = body[:comparison_start] + "  if (-not $true) {\n" + body[comparison_end:]
    script = r"""
$ErrorActionPreference = 'Stop'
$script:foreground = 789
$targetHwnd = 789
$script:identity = $true
$script:count = 0
$selectedOwnership = 'owned'
function Get-ControlOwnership { param($element) return 'owned' }
function Test-Target {
  if (-not $script:identity) { $script:targetFailureReason = 'process_identity_changed'; return $false }
  return $true
}
function Get-ActionControl {
  param($root)
  $control = [pscustomobject]@{}
  $control | Add-Member ScriptMethod TryGetCurrentPattern {
    param($pattern, $output)
    if ('CHANGE' -eq 'foreground') { $script:foreground = 999 }
    if ('CHANGE' -eq 'identity') { $script:identity = $false }
    $invoke = [pscustomobject]@{}
    $invoke | Add-Member ScriptMethod Invoke { $script:count++ }
    $output.Value = $invoke
    return $true
  }
  return [pscustomobject]@{Element=$control;Ownership='owned'}
}
try {
BODY
} catch {}
Write-Output $script:count
""".replace("CHANGE", change).replace("BODY", body)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("1" if change == "none" else "0")


@WINDOWS_ONLY
def test_selected_control_comparison_uses_real_supported_uia_element_api() -> None:
    source = actions.ACTION_SCRIPT.read_text(encoding="utf-8")
    start = source.index("  $comparison =")
    expression = source[start : source.index("    $reason = 'selected_control_changed'", start)]
    script = r"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
$signature = [type[]]@([System.Windows.Automation.AutomationElement],[System.Windows.Automation.AutomationElement])
$method = [System.Windows.Automation.Automation].GetMethod('Compare', $signature)
if ($null -eq $method) { throw 'element comparison overload absent' }
$selected = [System.Windows.Automation.AutomationElement]$null
$target = [System.Windows.Automation.AutomationElement]$null
try {
EXPRESSION
    throw 'unexpected null comparison result'
  }
} catch {
  if ($_.Exception.GetBaseException() -isnot [ArgumentNullException]) { throw }
  'real_element_api_executed'
}
""".replace("EXPRESSION", expression)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "real_element_api_executed"


@pytest.mark.parametrize("elapsed", [20.0, 3.0])
def test_cli_preflight_consumes_original_ui_deadline_before_any_action(
    monkeypatch: pytest.MonkeyPatch, elapsed: float
) -> None:
    context, _args, calls = _wire_ui_route(monkeypatch, no_save=True)
    clock = [100.0]
    monkeypatch.setattr(refresh_pbip_model.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(refresh_pbip_model, "_resolve_pid", lambda _pid: 123)
    monkeypatch.setattr(refresh_pbip_model, "_ui_bridge_preflight", lambda *_args: _ready_bridge()["instances"][0])
    monkeypatch.setattr(
        refresh_pbip_model,
        "_ui_bridge_status",
        lambda *_args, **kwargs: (
            {"status": "ok", "pid": 123, "result": {"success": True}}
            if kwargs.get("operation") == "reload"
            else _ready_bridge()
        ),
    )
    monkeypatch.setattr(refresh_pbip_model, "cache_file", lambda _pid, **_kwargs: Path("missing-cache.abf"))
    monkeypatch.setattr(refresh_pbip_model, "source_hint_from_model", lambda _path: None)
    root = DesktopWindow("", "", 100, 100, hwnd=789, pid="123", process_started_at="456")
    ready = Observation(pid="123", process_started_at="456", target_window=root)

    def observe(*_args, **_kwargs):
        clock[0] += elapsed
        return ready

    monkeypatch.setattr(refresh_pbip_model, "_credential_state", observe)
    monkeypatch.setattr(refresh_pbip_model, "discover_port", lambda _pid: 49157)
    monkeypatch.setattr(refresh_pbip_model, "bind_desktop", lambda *_args, **_kwargs: context.bound)
    received = []
    locked = refresh_pbip_model._ui_refresh_locked

    def run_locked(*args, deadline):
        received.append(deadline)
        return locked(*args, deadline=deadline)

    monkeypatch.setattr(refresh_pbip_model, "_ui_refresh_locked", run_locked)
    result = refresh_pbip_model.main(
        ["--pid", "123", "--ui-refresh", "--no-save", "--refresh-absolute-timeout-seconds", "10"]
    )
    if elapsed == 20.0:
        assert result != 0
        assert not calls and not received
    else:
        assert result == 0
        assert received == [110.0]
        assert calls == ["Refresh", "canaries"]


@pytest.mark.parametrize(
    "case", ["child", "cached", "prepared_occluded", "stale_hit", "wrong_root", "changed_start", "geometry"]
)
@WINDOWS_ONLY
def test_native_renderer_ownership_without_window_operations(case: str) -> None:
    source = actions.ACTION_SCRIPT.read_text(encoding="utf-8")
    body = source[source.index("function Get-ControlOwnership") : source.index("function Get-ProcessStart")]
    body = body.replace("[DesktopActionNative]", "[OwnershipFake]")
    body = body.replace("[DesktopActionNative+Rect]::new()", "[pscustomobject]@{}")
    body = body.replace("[DesktopActionNative+Point]::new()", "[pscustomobject]@{X=0;Y=0}")
    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type @'
using System;
public static class OwnershipFake {
  public static bool WrongRoot, Stale;
  public static bool BelongsTo(IntPtr h, uint p) { return p == 200 && (h == (IntPtr)55 || h == (IntPtr)56); }
  public static bool IsChild(IntPtr p, IntPtr h) {
    return !WrongRoot && ((p == (IntPtr)789 && h == (IntPtr)55) || (p == (IntPtr)55 && h == (IntPtr)56));
  }
  public static bool GetWindowRect(IntPtr h, out object r) {
    r = new { Left=0, Top=0, Right=100, Bottom=100 }; return true;
  }
  public static IntPtr WindowFromPoint(object p) { return Stale ? (IntPtr)99 : (IntPtr)56; }
}
'@
$Action = 'Refresh'
$targetHwnd = [IntPtr]789
$script:chainReads = 0
function Get-ProviderChain {
  param($provider)
  $script:chainReads++
  if ('CASE' -eq 'changed_start' -and $script:chainReads -gt 1) { return 'changed' }
  return '200/20;100/10'
}
[OwnershipFake]::WrongRoot = 'CASE' -eq 'wrong_root'
[OwnershipFake]::Stale = 'CASE' -in @('stale_hit', 'prepared_occluded')
$bounds = [pscustomobject]@{IsEmpty=$false;Left=10;Top=10;Right=30;Bottom=30;Width=20;Height=20}
if ('CASE' -eq 'geometry') { $bounds.Right = 101 }
$element = [pscustomobject]@{Current=[pscustomobject]@{
  Name='Refresh';IsEnabled=$true;IsOffscreen=$false;ProcessId=200;NativeWindowHandle=55;BoundingRectangle=$bounds
}}
BODY
if ('CASE' -eq 'prepared_occluded') {
  if (-not (Get-ControlOwnership $element @{} -Prepare)) { throw 'occluded background preparation refused' }
  $binding=$null
  if (Get-ControlOwnership $element -PreparedBinding ([ref]$binding)) { throw 'occluded foreground resolution admitted' }
  if ($binding -cne '200/20;100/10|55') { throw 'structural binding lost on a native hit refusal' }
  'owned'
} elseif ('CASE' -eq 'cached') {
  $cache = @{}
  $first = Get-ControlOwnership $element $cache
  $second = Get-ControlOwnership $element $cache
  if (-not $first -or -not $second -or $script:chainReads -ne 1) { throw 'cache not scan-local' }
  [OwnershipFake]::Stale = $true
  if (Get-ControlOwnership $element $cache) { throw 'native ownership improperly cached' }
  if ($script:chainReads -ne 1) { throw 'provider chain not cached' }
  'owned'
} elseif (Get-ControlOwnership $element) { 'owned' } else { 'refused' }
""".replace("CASE", case).replace("BODY", body)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("owned" if case in {"child", "cached", "prepared_occluded"} else "refused")


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "wrong_start",
        "newer_parent",
        "cycle",
        "native_mismatch",
        "reused_provider",
        "reused_parent",
        "exited",
        "denied",
    ],
)
@WINDOWS_ONLY
def test_provider_process_start_ancestry_is_required(case: str) -> None:
    source = actions.ACTION_SCRIPT.read_text(encoding="utf-8")
    body = source[source.index("function Get-ProviderChain") : source.index("function Get-ControlOwnership")]
    script = r"""
$ErrorActionPreference='Stop'
$DesktopPid='100'; $ExpectedProcessStart='10'
$script:processChains=@{}; $script:cimReads=0; $script:changed=$false; $script:nativeReads=@()
Add-Type @'
using System;
public static class ChainFake {
  public static string Case;
  public static bool Changed;
  public static string GetProcessStart(uint p) {
    if (Case=="native_mismatch") return "19";
    if (Changed && (Case=="exited" || Case=="denied")) return null;
    if (Changed && Case=="reused_provider" && p==200) return "21";
    if (Changed && Case=="reused_parent" && p==100) return "11";
    if (p==100) return Case=="wrong_start" ? "11" : Case=="newer_parent" ? "30" : "10";
    return "20";
  }
}
'@
[ChainFake]::Case='CASE'
function Get-CimInstance {
  param($Class, $Filter)
  $script:cimReads++
  $id = [int]($Filter.Split('=')[1])
  $start = if ($id -eq 100) { 10 } else { 20 }
  $parent = 100
  if ('CASE' -eq 'wrong_start' -and $id -eq 100) { $start=11 }
  if ('CASE' -eq 'newer_parent' -and $id -eq 100) { $start=30 }
  if ('CASE' -eq 'cycle') { $parent=200 }
  return [pscustomobject]@{CreationDate=[datetime]::new($start,[DateTimeKind]::Utc);ParentProcessId=$parent}
}
BODY
$first=Get-ProviderChain 200
if ('CASE' -in @('valid','reused_provider','reused_parent','exited','denied') -and
    $first -cne '200/20;100/10') { throw 'positive ancestry setup failed' }
[ChainFake]::Changed=$true
$second=Get-ProviderChain 200
if ('CASE' -eq 'valid' -and $script:cimReads -ne 2) { throw "ancestry re-queried: $script:cimReads" }
if ($second) { 'verified' } else { 'refused' }
""".replace("CASE", case).replace("BODY", body.replace("[DesktopActionNative]", "[ChainFake]"))
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("verified" if case == "valid" else "refused")


@pytest.mark.parametrize("case", ["valid", "denied", "exited_before", "exited_after", "wait_failed", "times_failed"])
@WINDOWS_ONLY
def test_native_process_time_preserves_cim_precision_and_closes_limited_handle(case: str) -> None:
    source = actions.ACTION_SCRIPT.read_text(encoding="utf-8")
    start = source.index("  public static string GetProcessStart(")
    method = source[start : source.index('  [DllImport("user32.dll")]', start)]
    script = r"""
$ErrorActionPreference='Stop'
Add-Type @'
using System;
public static class ProcessTimeFake {
  public static string Case;
  public static int Closed, Waits, Reads;
  public static IntPtr OpenProcess(uint access,bool inherit,uint pid) {
    if(access!=0x101000 || inherit || pid!=51552) throw new Exception("unexpected process rights/target");
    return Case=="denied" ? IntPtr.Zero : (IntPtr)1;
  }
  public static uint WaitForSingleObject(IntPtr h,uint milliseconds) {
    if(h!=(IntPtr)1 || milliseconds!=0) throw new Exception("blocking or wrong handle");
    Waits++;
    if(Case=="wait_failed") return 0xffffffff;
    if(Case=="exited_before" || (Case=="exited_after" && Waits==2)) return 0;
    return 258;
  }
  public static bool GetProcessTimes(IntPtr h,out long c,out long e,out long k,out long u) {
    Reads++; c=new DateTime(639288946506233534,DateTimeKind.Utc).ToFileTimeUtc(); e=k=u=0;
    return Case!="times_failed";
  }
  public static bool CloseHandle(IntPtr h) { Closed++; return true; }
METHOD
}
'@
[ProcessTimeFake]::Case='CASE'
$actual=[ProcessTimeFake]::GetProcessStart(51552)
[pscustomobject]@{value=$actual;closed=[ProcessTimeFake]::Closed;waits=[ProcessTimeFake]::Waits
  reads=[ProcessTimeFake]::Reads} | ConvertTo-Json -Compress
""".replace("CASE", case).replace("METHOD", method)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    # Independently measured retained PID: native 639288946506233534 -> CIM 639288946506233530.
    assert facts["value"] == ("639288946506233530" if case == "valid" else None), result.stdout
    assert facts["closed"] == (0 if case == "denied" else 1), result.stdout
    assert facts["reads"] == (0 if case in {"denied", "exited_before", "wait_failed"} else 1), result.stdout
    expected_waits = 0 if case == "denied" else 2 if case in {"valid", "exited_after"} else 1
    assert facts["waits"] == expected_waits, result.stdout


@pytest.mark.parametrize("count", [0, 1, 2])
@pytest.mark.parametrize("identity", ["stable", "changed", "missing"])
@WINDOWS_ONLY
def test_selector_requires_one_owned_invoke_and_ignores_dropdowns(count: int, identity: str) -> None:
    source = actions.ACTION_SCRIPT.read_text(encoding="utf-8")
    body = source[source.index("function Get-ActionCandidates") : source.index("$attempts = 0")]
    body = body.replace("[System.Windows.Automation.AutomationElement]$RootElement", "$RootElement")
    body = body.replace(
        "$membershipComparison.Invoke($null, [object[]]@($element, $candidate.Element))",
        "[object]::ReferenceEquals($element, $candidate.Element)",
    )
    body = body.replace("[System.Windows.Automation.InvokePattern]::Pattern", "'Invoke'")
    body = body.replace("[System.Windows.Automation.ExpandCollapsePattern]::Pattern", "'Expand'")
    script = (
        r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
$Action = 'Save'
function Get-ControlOwnership {
  param($e, $cache, [switch]$Prepare, $PreparedBinding=$null)
  if (-not $e.Owned) { return $null }
  $cache[[uint32]200] = '200/20;100/10'
  if ($null -ne $PreparedBinding) { $PreparedBinding.Value='owned' }
  return 'owned'
}
function Get-ProviderChain {
  param($provider)
  if ('IDENTITY' -eq 'changed') { return '200/21;100/10' }
  if ('IDENTITY' -eq 'missing') { return $null }
  return '200/20;100/10'
}
function New-Control($owned, $dropdown) {
  $e = [pscustomobject]@{Owned=$owned;Dropdown=$dropdown;Current=[pscustomobject]@{
    Name='Save';IsEnabled=$true;IsOffscreen=$false;ProcessId=200
  }}
  $e | Add-Member ScriptMethod TryGetCurrentPattern {
    param($pattern, $out)
    return ($pattern -eq 'Invoke' -or ($pattern -eq 'Expand' -and $this.Dropdown))
  }
  return $e
}
$controls = @((New-Control $true $true), (New-Control $false $false))
for ($i=0; $i -lt COUNT; $i++) { $controls += New-Control $true $false }
$root = [pscustomobject]@{}
$root | Add-Member ScriptMethod FindAll { param($scope,$condition) return $controls }
BODY
$invokes = 0
try {
  $candidates = Get-ActionCandidates $root
  $null = Get-ActionControl $candidates
  $invokes++
  'selected'
} catch { $script:reason }
if ('IDENTITY' -ne 'stable' -and COUNT -gt 0 -and $invokes -ne 0) { throw 'identity change admitted Invoke' }
""".replace("COUNT", str(count))
        .replace("IDENTITY", identity)
        .replace("BODY", body)
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    expected = {0: "named_control_missing", 1: "selected", 2: "ambiguous_named_control"}[count]
    if identity != "stable":
        expected = "provider_identity_changed"
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "case",
    ["normal", "disabled_root", "initial_disabled", "owned_popup", "human_switch", "target_reused", "prior_reused"],
)
@WINDOWS_ONLY
def test_native_identity_hot_path_and_disabled_root_handback_without_windows(case: str) -> None:
    """Run the real focus/target/main bodies against fake native calls, never real windows."""
    source = actions.ACTION_SCRIPT.read_text(encoding="utf-8")
    functions = source[source.index("function Get-ProcessStart") : source.index("function Get-ActionCandidates")]
    functions = functions.replace("[System.Windows.Automation.AutomationElement]::FromHandle($root)", "$script:root")
    main = source[source.index("$attempts = 0") :]
    main = main.replace(
        "$comparison.Invoke($null, [object[]]@($selected, $target))", "[object]::ReferenceEquals($selected, $target)"
    )
    script = (
        r"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type @'
using System;
public static class HotPathFake {
  public static string Case;
  public static bool Invoked, Enabled=true;
  public static IntPtr Foreground=(IntPtr)456;
  public static int NativeReads, ForegroundReads, Attach, Detach;
  public static string GetProcessStart(uint p) {
    NativeReads++;
    if (Invoked && ((Case=="target_reused" && p==100) || (Case=="prior_reused" && p==300))) return "11";
    return "10";
  }
  public static bool IsWindow(IntPtr h) { return true; }
  public static bool BelongsTo(IntPtr h,uint p) { return h==(IntPtr)789 && p==100; }
  public static bool IsWindowVisible(IntPtr h) { return true; }
  public static bool IsWindowEnabled(IntPtr h) { return Enabled; }
  public static bool IsIconic(IntPtr h) { return false; }
  public static IntPtr GetWindow(IntPtr h,uint command) { return IntPtr.Zero; }
  public static IntPtr GetForegroundWindow() { ForegroundReads++; return Foreground; }
  public static uint GetCurrentThreadId() { return 1; }
  public static uint GetWindowThreadProcessId(IntPtr h,out uint p) {
    p=h==(IntPtr)789 || h==(IntPtr)888 ? 100u : 300u; return h==(IntPtr)789 ? 2u : 3u;
  }
  public static bool AttachThreadInput(uint a,uint b,bool attach) {
    if(attach) Attach++; else Detach++; return true;
  }
  public static bool BringWindowToTop(IntPtr h) { Foreground=h; return true; }
  public static bool SetForegroundWindow(IntPtr h) { Foreground=h; return true; }
  public static IntPtr SetThreadDpiAwarenessContext(IntPtr c) { return (IntPtr)1; }
}
'@
[HotPathFake]::Case='CASE'
if ('CASE' -eq 'initial_disabled') { [HotPathFake]::Enabled=$false }
$Action='Refresh'; $DesktopPid='100'; $ExpectedProcessStart='10'; $RootHandle='789'
$script:cimReads=0; $script:invokes=0; $script:postInvokeActionabilityReads=0
function Get-CimInstance {
  param($Class,$Filter)
  $script:cimReads++
  $id=[int]($Filter.Split('=')[1])
  $ticks=10
  if ([HotPathFake]::Invoked -and
      (('CASE' -eq 'target_reused' -and $id -eq 100) -or
       ('CASE' -eq 'prior_reused' -and $id -eq 300))) { $ticks=11 }
  return [pscustomobject]@{CreationDate=[datetime]::new($ticks,[DateTimeKind]::Utc)}
}
$script:root=[pscustomobject]@{Current=[pscustomobject]@{ProcessId=100;NativeWindowHandle=789}}
$script:control=[pscustomobject]@{}
$script:control | Add-Member ScriptMethod TryGetCurrentPattern {
  param($pattern,$output)
  $invoke=[pscustomobject]@{}
  $invoke | Add-Member ScriptMethod Invoke {
    $script:invokes++; [HotPathFake]::Invoked=$true
    if ('CASE' -eq 'disabled_root') { [HotPathFake]::Enabled=$false }
    if ('CASE' -eq 'owned_popup') { [HotPathFake]::Foreground=[IntPtr]888 }
    if ('CASE' -eq 'human_switch') { [HotPathFake]::Foreground=[IntPtr]999 }
  }
  $output.Value=$invoke; return $true
}
function Get-ActionCandidates { param($root) return @{} }
function Get-ActionControl {
  param($candidates) return [pscustomobject]@{Element=$script:control;Ownership='owned'}
}
function Get-ControlOwnership { param($target) return 'owned' }
function Start-Sleep { param($Milliseconds) }
FUNCTIONS
MAIN
[pscustomobject]@{invokes=$script:invokes;cim=$script:cimReads;native=[HotPathFake]::NativeReads
  foreground=[HotPathFake]::Foreground.ToInt64();attach=[HotPathFake]::Attach;detach=[HotPathFake]::Detach
} | ConvertTo-Json -Compress
""".replace("CASE", case)
        .replace("FUNCTIONS", functions)
        .replace("MAIN", main)
    )
    script = script.replace("[DesktopActionNative]", "[HotPathFake]")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    action_result, facts = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert facts["invokes"] == (0 if case == "initial_disabled" else 1), result.stdout
    assert action_result["invoked"] is (case != "initial_disabled"), result.stdout
    assert facts["attach"] == facts["detach"], result.stdout
    assert facts["cim"] == 0, result.stdout
    assert facts["native"] > 0, result.stdout
    expected_foreground = {"owned_popup": 888, "human_switch": 999}.get(case, 789)
    if case in {"normal", "disabled_root"}:
        expected_foreground = 456
    if case == "initial_disabled":
        expected_foreground = 456
        assert action_result["focus_attempts"] == 0
    assert facts["foreground"] == expected_foreground, result.stdout
    assert action_result["handback_ok"] is (case in {"normal", "disabled_root"}), result.stdout


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "ambiguous",
        "disabled",
        "newly_enabled",
        "provider_changed",
        "renderer_changed",
        "no_invoke",
        "dropdown",
        "stale",
        "second_pass_changed",
        "added_at_focus",
        "renamed_at_focus",
        "added_after_selection",
        "renamed_after_selection",
        "replaced_at_focus",
    ],
)
@WINDOWS_ONLY
def test_prepared_action_rediscovers_current_named_set_and_refuses_changed_candidates(case: str) -> None:
    """Execute the selection/action body with only fake controls, processes and native APIs."""
    source = actions.ACTION_SCRIPT.read_text(encoding="utf-8")
    selection = source[source.index("function New-Result") : source.index("$attempts = 0")]
    selection = selection.replace("[System.Windows.Automation.AutomationElement]$RootElement", "$RootElement")
    selection = selection.replace(
        "$membershipComparison.Invoke($null, [object[]]@($element, $candidate.Element))",
        "[object]::ReferenceEquals($element, $candidate.Element)",
    )
    main = source[source.index("$attempts = 0") :]
    main = main.replace("[DesktopActionNative]", "[PreparedActionFake]")
    main = main.replace(
        "$comparison.Invoke($null, [object[]]@($selected, $target))",
        "[object]::ReferenceEquals($selected, $target)",
    )
    script = (
        r"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type @'
using System;
public static class PreparedActionFake {
  public static IntPtr Foreground = (IntPtr)456;
  public static IntPtr GetForegroundWindow() { return Foreground; }
  public static IntPtr SetThreadDpiAwarenessContext(IntPtr c) { return (IntPtr)1; }
}
'@
$Action='Refresh'; $DesktopPid='100'; $ExpectedProcessStart='10'; $RootHandle='789'
$script:scans=0; $script:invokes=0; $script:providerReads=0; $script:events=@()
function Get-ProviderChain {
  param($provider)
  $script:providerReads++
  $script:events += "provider:$([PreparedActionFake]::Foreground)"
  if ([PreparedActionFake]::Foreground -eq 789 -and 'CASE' -eq 'provider_changed') { return '200/21;100/10' }
  return '200/20;100/10'
}
function Get-ControlOwnership {
  param($e, $cache=$null, [switch]$Prepare, $PreparedBinding=$null)
  if (-not $e.Current.IsEnabled) { return $null }
  if ($null -eq $cache) { $chain=Get-ProviderChain 200 } else {
    if (-not $cache.ContainsKey([uint32]200)) { $cache[[uint32]200]=Get-ProviderChain 200 }
    $chain=$cache[[uint32]200]
  }
  if ([PreparedActionFake]::Foreground -eq 789 -and 'CASE' -eq 'stale') { throw 'stale element' }
  $binding="$chain|$($e.Renderer)"
  if ($null -ne $PreparedBinding) { $PreparedBinding.Value=$binding }
  if ($Prepare) { return $binding }
  if ([PreparedActionFake]::Foreground -ne 789) { throw 'background hit test' }
  $hit=$e.Hit
  if ([object]::ReferenceEquals($e,$script:second) -and 'CASE' -eq 'second_pass_changed') { $e.Hit=$true }
  if ([object]::ReferenceEquals($e,$script:first) -and -not $script:changedAfterSelection) {
    if ('CASE' -eq 'added_after_selection') { $script:controls += New-Control $true }
    if ('CASE' -eq 'renamed_after_selection') { $script:second.Current.Name='Refresh'; $script:second.Hit=$true }
    $script:changedAfterSelection=$true
  }
  if ($hit) { return "$binding|56" }
  return $null
}
function New-Control($hit) {
  $e=[pscustomobject]@{Hit=$hit;Renderer=55;Invoke=$true;Dropdown=$false;Current=[pscustomobject]@{
    Name='Refresh';IsEnabled=$true;IsOffscreen=$false;ProcessId=200
  }}
  $e | Add-Member ScriptMethod TryGetCurrentPattern {
    param($pattern,$output)
    if ($pattern -eq [System.Windows.Automation.InvokePattern]::Pattern) {
      $invoke=[pscustomobject]@{}
      $invoke | Add-Member ScriptMethod Invoke { $script:invokes++ }
      $output.Value=$invoke
      return $this.Invoke
    }
    return $this.Dropdown
  }
  return $e
}
$script:first=New-Control $true
$script:second=New-Control $false
$script:controls=@($script:first,$script:second)
if ('CASE' -in @('renamed_at_focus','renamed_after_selection')) { $script:second.Current.Name='Other' }
if ('CASE' -eq 'newly_enabled') { $script:second.Current.IsEnabled=$false }
$script:root=[pscustomobject]@{}
$script:root | Add-Member ScriptMethod FindAll {
  param($scope,$condition)
  $script:scans++
  $script:events += "scan:$([PreparedActionFake]::Foreground)"
  if ($condition -is [System.Windows.Automation.AndCondition]) {
    $terms=$condition.GetConditions()
    if ($terms[0].Property -ne [System.Windows.Automation.AutomationElement]::NameProperty -or
        $terms[0].Value -cne 'Refresh' -or
        $terms[1] -isnot [System.Windows.Automation.OrCondition]) { throw 'query not narrow' }
    $types=$terms[1].GetConditions()
    if ($scope -ne [System.Windows.Automation.TreeScope]::Descendants -or $types.Count -ne 2 -or
        $types[0].Property -ne [System.Windows.Automation.AutomationElement]::ControlTypeProperty -or
        $types[1].Property -ne [System.Windows.Automation.AutomationElement]::ControlTypeProperty -or
        $types[0].Value -ne [System.Windows.Automation.ControlType]::Button.Id -or
        $types[1].Value -ne [System.Windows.Automation.ControlType]::SplitButton.Id) { throw 'wrong control types' }
    return @($script:controls | Where-Object { $_.Current.Name -ceq 'Refresh' })
  }
  if ($condition.Value -eq [System.Windows.Automation.ControlType]::Button.Id) {
    return $script:controls
  }
  return @()
}
function Test-Target { return $script:root }
function Get-WindowIdentity { param($h) return '100/10' }
function Set-VerifiedForeground {
  param($destination,$permitted,$identity)
  if ($destination -eq 789) {
    $script:events += 'focus'
    if ('CASE' -eq 'ambiguous') { $script:second.Hit=$true }
    if ('CASE' -eq 'disabled') { $script:first.Current.IsEnabled=$false }
    if ('CASE' -eq 'newly_enabled') { $script:second.Current.IsEnabled=$true; $script:second.Hit=$true }
    if ('CASE' -eq 'renderer_changed') { $script:first.Renderer=57 }
    if ('CASE' -eq 'no_invoke') { $script:first.Invoke=$false }
    if ('CASE' -eq 'dropdown') { $script:first.Dropdown=$true }
    if ('CASE' -eq 'added_at_focus') { $script:controls += New-Control $true }
    if ('CASE' -eq 'renamed_at_focus') { $script:second.Current.Name='Refresh'; $script:second.Hit=$true }
    if ('CASE' -eq 'replaced_at_focus') { $script:controls=@((New-Control $true),$script:second) }
  }
  [PreparedActionFake]::Foreground=$destination
  return $true
}
function Start-Sleep { param($Milliseconds) }
SELECTION
MAIN
[pscustomobject]@{
  scans=$script:scans; invokes=$script:invokes; events=$script:events
  foreground=[PreparedActionFake]::Foreground.ToInt64()
  eligible=@($script:controls | Where-Object {
    $_.Current.Name -ceq 'Refresh' -and $_.Current.IsEnabled -and $_.Hit -and $_.Invoke -and -not $_.Dropdown
  }).Count
} | ConvertTo-Json -Compress
""".replace("CASE", case)
        .replace("SELECTION", selection)
        .replace("MAIN", main)
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    action_result, facts = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if case.startswith(("added_", "renamed_")):
        assert facts["eligible"] == 2, result.stdout
    elif case in {"valid", "replaced_at_focus"}:
        assert facts["eligible"] == 1, result.stdout
    assert facts["foreground"] == 456
    assert facts["invokes"] == (1 if case == "valid" else 0), result.stdout
    assert action_result["invoked"] is (case == "valid")
    assert action_result["handback_ok"] is True
    assert facts["events"][:3] == ["scan:456", "provider:456", "focus"], result.stdout
    assert facts["events"][3] == "scan:789", result.stdout
    if case == "valid":
        assert facts["scans"] == 3
        assert facts["events"].count("provider:789") == 3
    elif case in {"ambiguous", "second_pass_changed"}:
        assert action_result["reason"] == "ambiguous_named_control"
    elif case == "provider_changed":
        assert action_result["reason"] == "provider_identity_changed"
    elif case not in {"valid", "stale"}:
        assert action_result["reason"] == "selected_control_changed"


@pytest.mark.parametrize("human_switch", [False, True])
@WINDOWS_ONLY
def test_selector_refusal_handback_preserves_human_foreground(human_switch: bool) -> None:
    source = actions.ACTION_SCRIPT.read_text(encoding="utf-8")
    start = source.index("  $foregroundAfter =")
    body = source[start : source.index("  if ($dpiContext", start)]
    body = body.replace("[DesktopActionNative]::GetForegroundWindow()", "$script:foreground")
    body = body.replace("[DesktopActionNative]::IsWindow($priorForeground)", "$true")
    body = body.replace(
        "[DesktopActionNative]::SetForegroundWindow($priorForeground)",
        "(& { $script:foreground = $priorForeground; $script:restores++; $true })",
    )
    script = r"""
$attempts=1; $invoked=$false; $targetHwnd=789; $priorForeground=456
$restoreRequested=$false; $restoreVerified=$null; $handbackOk=$false
$priorIdentity='456/10'
function Get-WindowIdentity { param($h) return '456/10' }
function Set-VerifiedForeground {
  param($destination,$permitted,$identity)
  $script:foreground=$destination; $script:restores++; return $true
}
$reason='ambiguous_named_control'; $script:restores=0; $script:foreground=FOREGROUND
BODY
"$script:restores|$restoreRequested|$restoreVerified|$reason"
""".replace("FOREGROUND", "999" if human_switch else "789").replace("BODY", body)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "0|False||ambiguous_named_control" if human_switch else "1|True|True|ambiguous_named_control"
    )


@pytest.mark.parametrize("case", ["success", "attach_failure", "human_switch", "bring_switch", "identity"])
@WINDOWS_ONLY
def test_thread_attachment_focus_body_detaches_and_preserves_human_switch(case: str) -> None:
    source = actions.ACTION_SCRIPT.read_text(encoding="utf-8")
    start = source.index("function Set-VerifiedForeground")
    body = source[start : source.index("function Test-Target", start)]
    body = body.replace("[DesktopActionNative]", "[FocusFake]")
    script = r"""
$ErrorActionPreference='Stop'
Add-Type @'
using System;
public static class FocusFake {
  public static string Case;
  public static int Attach, Detach, Bring, Set;
  public static IntPtr Foreground = (IntPtr)456;
  public static uint GetCurrentThreadId() { return 1; }
  public static bool IsWindow(IntPtr h) { return true; }
  public static uint GetWindowThreadProcessId(IntPtr h, out uint pid) { pid=100; return h==(IntPtr)456 ? 2u : 3u; }
  public static IntPtr GetForegroundWindow() { return Foreground; }
  public static bool AttachThreadInput(uint a,uint b,bool attach) {
    if (!attach) { Detach++; return true; }
    Attach++;
    if (Case=="attach_failure" && Attach==2) return false;
    if (Case=="human_switch") Foreground=(IntPtr)999;
    return true;
  }
  public static bool BringWindowToTop(IntPtr h) { Bring++; if(Case=="bring_switch") Foreground=(IntPtr)999; return true; }
  public static bool SetForegroundWindow(IntPtr h) { Set++; Foreground=h; return true; }
}
'@
[FocusFake]::Case='CASE'
function Test-Target { return $true }
function Get-WindowIdentity { param($h) if ('CASE' -eq 'identity') { return 'changed' }; return '100/10' }
BODY
$ok = Set-VerifiedForeground ([IntPtr]789) ([IntPtr]456) '100/10'
"$ok|$([FocusFake]::Attach)|$([FocusFake]::Detach)|$([FocusFake]::Bring)|$([FocusFake]::Set)"
""".replace("CASE", case).replace("BODY", body)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    expected = {
        "success": "True|2|2|1|1",
        "attach_failure": "False|2|1|0|0",
        "human_switch": "False|1|1|0|0",
        "bring_switch": "False|2|2|1|0",
        "identity": "False|0|0|0|0",
    }
    assert result.stdout.strip() == expected[case]


@pytest.mark.parametrize(
    ("table_mode", "partition_mode", "kind", "refreshed", "query"),
    [
        (0, 4, 4, BASELINE, "Source"),
        (0, 1, 4, BASELINE, "Source"),
        (0, 99, 4, BASELINE, "Source"),
        (0, None, 4, BASELINE, "Source"),
        (0, 3, 4, BASELINE, "Source"),
        (0, 5, 4, BASELINE, "Source"),
        (0, 0, 1, BASELINE, "Source"),
        (0, 0, 4, "2026-10-28T12:00:00Z", "Source"),
        (0, 0, 4, None, "Source"),
        (0, 0, 4, datetime(2026, 10, 28, 12, 0), "Source"),
    ],
)
def test_snapshot_refuses_unresolved_or_unsupported_source_metadata(
    table_mode: object, partition_mode: object, kind: int, refreshed: object, query: object
) -> None:
    connection = _Connection(
        tables=[(10, "Orders", table_mode)],
        partitions=[_partition(kind=kind, mode=partition_mode, refreshed=refreshed, query=query)],
    )

    with pytest.raises(DesktopActionError):
        _snapshot_from_connection(connection, "catalog-1")


def test_snapshot_refuses_duplicate_source_identity_and_empty_source_set() -> None:
    duplicate = _Connection(
        tables=[(10, "Orders", 0)],
        partitions=[_partition(), _partition()],
    )
    empty = _Connection(tables=[(10, "Orders", 0)], partitions=[])

    with pytest.raises(DesktopActionError, match="duplicate source"):
        _snapshot_from_connection(duplicate, "catalog-1")
    with pytest.raises(DesktopActionError, match="no supported"):
        _snapshot_from_connection(empty, "catalog-1")


def test_snapshot_refuses_unresolved_or_directquery_model_default() -> None:
    for mode in (None, 1, 2, 3, 4, 5, 99):
        connection = _Connection(
            tables=[(10, "Orders", None)],
            partitions=[_partition(mode=None)],
            default_mode=mode,
        )
        with pytest.raises(DesktopActionError, match="model default"):
            _snapshot_from_connection(connection, "catalog-1")


def test_completion_requires_same_import_m_set_all_ready_and_strictly_newer_after_action() -> None:
    before = _snapshot(ticks=100)
    after = _snapshot(ticks=200)

    assert check_completion(before, after, 150).complete
    assert not check_completion(before, before, 100).complete
    assert not check_completion(before, _snapshot(ticks=100), 99).complete
    assert not check_completion(before, _snapshot(ticks=200, state=0), 150).complete
    assert not check_completion(before, _snapshot(ticks=149), 150).complete
    assert not check_completion(before, _snapshot(partition_id=60, ticks=200), 150).complete
    assert not check_completion(before, _snapshot(table_id=11, ticks=200), 150).complete
    assert not check_completion(before, after, 0).complete


def test_completion_requires_every_source_partition_to_advance() -> None:
    before = PartitionSnapshot((*_snapshot(ticks=100).partitions, *_snapshot(partition_id=60, ticks=100).partitions))
    after = PartitionSnapshot((*_snapshot(ticks=200).partitions, *_snapshot(partition_id=60, ticks=100).partitions))

    result = check_completion(before, after, 150)

    assert not result.complete
    assert result.reason == "source partition timestamp did not advance"


def test_action_protocol_requires_exact_target_and_truthful_handback() -> None:
    result = _read_action_output(
        _action_payload(),
        "Refresh",
        pid=123,
        expected_process_start="456",
        root_hwnd=789,
    )
    failed_handback = _read_action_output(
        _action_payload(restore_verified=False, handback_ok=False, reason="handback_not_verified"),
        "Refresh",
        pid=123,
        expected_process_start="456",
        root_hwnd=789,
    )

    assert result.invoked and result.focus_attempts == 2 and result.handback_verified
    assert failed_handback.invoked and not failed_handback.handback_verified
    with pytest.raises(DesktopActionError, match="did not match"):
        _read_action_output(
            _action_payload(root_hwnd="999"),
            "Refresh",
            pid=123,
            expected_process_start="456",
            root_hwnd=789,
        )


def test_action_protocol_refuses_invoke_without_typed_start_or_named_status() -> None:
    with pytest.raises(DesktopActionError, match="did not establish"):
        _read_action_output(
            _action_payload(action_started_ticks=None),
            "Refresh",
            pid=123,
            expected_process_start="456",
            root_hwnd=789,
        )
    with pytest.raises(DesktopActionError, match="ambiguous"):
        _read_action_output(
            _action_payload(status="error", invoked=False),
            "Refresh",
            pid=123,
            expected_process_start="456",
            root_hwnd=789,
        )


def test_action_child_timeout_is_killed_and_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TimedOutChild:
        returncode = None
        stdout = SimpleNamespace(close=lambda: None)
        calls = 0
        killed = False
        waited = False

        def __enter__(self) -> "_TimedOutChild":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def communicate(self, timeout: float):  # pylint: disable=unused-argument
            self.calls += 1
            assert timeout > 0
            if self.calls == 1:
                raise subprocess.TimeoutExpired("powershell.exe", timeout)
            return ("", "")

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float) -> int:  # pylint: disable=unused-argument
            self.waited = True
            return self.returncode

    child = _TimedOutChild()
    monkeypatch.setattr(actions, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(actions.subprocess, "Popen", lambda *_args, **_kwargs: child)

    with pytest.raises(DesktopActionError, match="bounded deadline"):
        invoke_desktop_action(
            "Refresh",
            pid=123,
            expected_process_start="456",
            root_hwnd=789,
            timeout_seconds=0.1,
        )
    assert child.killed and child.waited and child.calls == 1


def test_expired_action_deadline_refuses_before_starting_a_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(actions.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("child started"))

    with pytest.raises(DesktopActionError, match="deadline has already expired"):
        invoke_desktop_action(
            "Refresh",
            pid=123,
            expected_process_start="456",
            root_hwnd=789,
            timeout_seconds=0,
        )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--ui-refresh", "--verify-only"],
        ["--ui-refresh", "--tables"],
        ["--ui-refresh", "--tables", "Orders"],
        ["--ui-refresh", "--calculate-only"],
        ["--ui-refresh", "--measures-only"],
    ],
)
def test_ui_refresh_incompatible_arguments_refuse_before_instance_discovery(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(refresh_pbip_model, "_resolve_pid", lambda _pid: pytest.fail("instance discovery ran"))

    assert refresh_pbip_model.main(["--pid", "123", *arguments]) == 2
    assert "cannot be combined" in capsys.readouterr().out


def test_default_and_verify_only_paths_do_not_enable_ui_refresh() -> None:
    parser = refresh_pbip_model._build_arg_parser()

    assert parser.parse_args(["--pid", "123"]).ui_refresh is False
    assert parser.parse_args(["--pid", "123", "--verify-only"]).ui_refresh is False


def test_ui_refresh_help_warns_about_operator_source_execution_and_no_clearance() -> None:
    help_text = " ".join(refresh_pbip_model._build_arg_parser().format_help().split())

    assert "operator-only opt-in commissions execution of ALL current M, including native/custom SQL" in help_text
    assert "potential source-side costs" in help_text
    assert "--no-save prevents Save, NOT source execution" in help_text
    assert "No automatic credential-gate use or earned clearance" in help_text
    assert "#780 (linked #692)" in help_text


def test_action_result_handback_flag_is_not_inferred_from_invoke() -> None:
    action = ActionResult("Save", True, "foreground_changed_after_invoke", 1, 10, False, "failed")

    assert action.invoked
    assert not action.handback_verified


def _wire_ui_route(monkeypatch: pytest.MonkeyPatch, *, no_save: bool = False, rows: int = 1):
    baseline = _snapshot(ticks=100)
    identity = SimpleNamespace(pid=123, port=49157, process_start="456")
    context = UIRefreshContext(123, 49157, SimpleNamespace(identity=identity), 789, None)
    calls: list[str] = []
    refresh_action = ActionResult("Refresh", True, "invoke_returned", 1, 200, False, "not_requested")
    save_action = ActionResult("Save", True, "invoke_returned", 1, 300, False, "not_requested")

    monkeypatch.setattr(refresh_pbip_model, "_ui_bridge_command", lambda: ["controlled-bridge"])
    monkeypatch.setattr(
        refresh_pbip_model, "_ui_observer_state", lambda *_args, **_kwargs: SimpleNamespace(state="READY")
    )
    monkeypatch.setattr(refresh_pbip_model, "bound_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(refresh_pbip_model, "_identity_gate", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(refresh_pbip_model, "read_partition_snapshot", lambda *_args, **_kwargs: baseline)
    monkeypatch.setattr(
        refresh_pbip_model,
        "invoke_desktop_action",
        lambda action, **_kwargs: calls.append(action) or (refresh_action if action == "Refresh" else save_action),
    )
    monkeypatch.setattr(
        refresh_pbip_model,
        "_wait_for_engine_completion",
        lambda *_args, **_kwargs: (True, CompletionResult(True, "all partitions advanced")),
    )

    def probe(*_args, **_kwargs):
        calls.append("canaries")
        return [SimpleNamespace(table="Orders", returned_rows=rows)]

    monkeypatch.setattr(refresh_pbip_model, "probe_observations", probe)
    monkeypatch.setattr(refresh_pbip_model, "_emit_data_verdict", lambda *_args, **_kwargs: 0)
    args = SimpleNamespace(
        refresh_absolute_timeout_seconds=10,
        no_save=no_save,
        canaries=["Orders"],
        tables=None,
        verify_only=False,
    )
    return context, args, calls


def test_positive_bound_canaries_invoke_one_ui_save_without_a_dirty_flag_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, args, calls = _wire_ui_route(monkeypatch)

    result = refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args)

    assert result == 0
    assert calls == ["Refresh", "canaries", "Save"]


def test_no_save_and_failed_completion_never_invoke_ui_save(monkeypatch: pytest.MonkeyPatch) -> None:
    context, args, calls = _wire_ui_route(monkeypatch, no_save=True)
    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 0
    assert calls == ["Refresh", "canaries"]

    context, args, calls = _wire_ui_route(monkeypatch)
    monkeypatch.setattr(
        refresh_pbip_model,
        "_wait_for_engine_completion",
        lambda *_args, **_kwargs: (False, None),
    )
    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 3
    assert calls == ["Refresh"]


@pytest.mark.parametrize("late_stage", ["observer", "binding", "identity", "snapshot", "save_binding", "save_identity"])
def test_ui_deadline_rejects_late_preflight_and_save_rechecks(monkeypatch: pytest.MonkeyPatch, late_stage: str) -> None:
    context, args, calls = _wire_ui_route(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(refresh_pbip_model.time, "monotonic", lambda: clock[0])
    stages = {"binding": 0, "identity": 0}
    original_snapshot = refresh_pbip_model.read_partition_snapshot

    def run_stage(stage):
        if stage in stages:
            stages[stage] += 1
        current = f"save_{stage}" if stages.get(stage, 0) == 2 else stage
        if current == late_stage:
            clock[0] = 111.0

    def observe(*_args, **_kwargs):
        run_stage("observer")
        return SimpleNamespace(state="READY")

    def bound_read(*_args, **_kwargs):
        run_stage("binding")

    def identity(*_args, **_kwargs):
        run_stage("identity")
        return True

    def snapshot(*_args, **_kwargs):
        run_stage("snapshot")
        return original_snapshot(context.bound)

    monkeypatch.setattr(refresh_pbip_model, "_ui_observer_state", observe)
    monkeypatch.setattr(refresh_pbip_model, "bound_call", bound_read)
    monkeypatch.setattr(refresh_pbip_model, "_identity_gate", identity)
    monkeypatch.setattr(refresh_pbip_model, "read_partition_snapshot", snapshot)

    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) != 0
    assert calls == (["Refresh", "canaries"] if late_stage.startswith("save_") else [])


def test_ui_read_allowances_shrink_and_completion_uses_same_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    context, args, calls = _wire_ui_route(monkeypatch)
    args.refresh_absolute_timeout_seconds = 40
    clock = [100.0]
    allowances = []
    monkeypatch.setattr(refresh_pbip_model.time, "monotonic", lambda: clock[0])
    original_snapshot = refresh_pbip_model.read_partition_snapshot

    def bound_read(*_args, timeout_seconds):
        allowances.append(timeout_seconds)
        clock[0] += 7

    def snapshot(*_args, timeout_seconds):
        allowances.append(timeout_seconds)
        clock[0] += 7
        return original_snapshot(context.bound)

    def completion(_context, _baseline, _started, deadline):
        assert deadline == 140.0
        clock[0] += 7
        return True, CompletionResult(True, "all advanced")

    def probe(*_args, timeout_seconds):
        allowances.append(timeout_seconds)
        clock[0] += 7
        calls.append("canaries")
        return [SimpleNamespace(table="Orders", returned_rows=1)]

    monkeypatch.setattr(refresh_pbip_model, "bound_call", bound_read)
    monkeypatch.setattr(refresh_pbip_model, "read_partition_snapshot", snapshot)
    monkeypatch.setattr(refresh_pbip_model, "_wait_for_engine_completion", completion)
    monkeypatch.setattr(refresh_pbip_model, "probe_observations", probe)
    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 0
    assert allowances == [30.0, 30.0, 19.0, 12.0]
    assert calls == ["Refresh", "canaries", "Save"]
    clock[0] = 140.0
    with pytest.raises(TimeoutError):
        refresh_pbip_model._ui_read_budget(140.0)


def test_ui_failed_metadata_read_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    context, args, calls = _wire_ui_route(monkeypatch)
    reads = []

    def snapshot(*_args, **_kwargs):
        reads.append("snapshot")
        raise query_probe.ObservationUnavailable("TIMEOUT")

    monkeypatch.setattr(refresh_pbip_model, "read_partition_snapshot", snapshot)
    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) != 0
    assert reads == ["snapshot"]
    assert not calls


def test_empty_data_or_failed_binding_never_authorizes_ui_save(monkeypatch: pytest.MonkeyPatch) -> None:
    context, args, calls = _wire_ui_route(monkeypatch, rows=0)
    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 0
    assert calls == ["Refresh", "canaries"]

    context, args, calls = _wire_ui_route(monkeypatch)
    monkeypatch.setattr(refresh_pbip_model, "_identity_gate", lambda *_args, **_kwargs: False)
    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 2
    assert not calls


def test_unknown_observer_after_invoke_prevents_canary_and_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, args, calls = _wire_ui_route(monkeypatch)
    observed = 0

    def observe(*_args, **_kwargs):
        nonlocal observed
        observed += 1
        if observed == 1:
            return SimpleNamespace(state="READY")
        raise refresh_pbip_model.CredentialUnknownError(123, "observer_incomplete")

    monkeypatch.setattr(refresh_pbip_model, "_ui_observer_state", observe)

    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 3
    assert calls == ["Refresh"]


def test_identity_recheck_refuses_save_and_save_handback_failure_is_not_persisted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    context, args, calls = _wire_ui_route(monkeypatch)
    gates = iter((True, False))
    monkeypatch.setattr(refresh_pbip_model, "_identity_gate", lambda *_args, **_kwargs: next(gates))
    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 2
    assert calls == ["Refresh", "canaries"]

    context, args, calls = _wire_ui_route(monkeypatch)
    failed_save = ActionResult("Save", True, "foreground_changed_after_invoke", 1, 300, False, "failed")
    monkeypatch.setattr(
        refresh_pbip_model,
        "invoke_desktop_action",
        lambda action, **_kwargs: (
            calls.append(action)
            or (
                ActionResult("Refresh", True, "ok", 1, 200, False, "not_requested")
                if action == "Refresh"
                else failed_save
            )
        ),
    )
    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 1
    assert calls == ["Refresh", "canaries", "Save"]
    assert "NOT_PERSISTED" in capsys.readouterr().out


def test_failed_refresh_handback_never_authorizes_save(monkeypatch: pytest.MonkeyPatch) -> None:
    context, args, calls = _wire_ui_route(monkeypatch)
    failed_refresh = ActionResult("Refresh", True, "foreground_changed_after_invoke", 1, 200, False, "failed")
    monkeypatch.setattr(
        refresh_pbip_model,
        "invoke_desktop_action",
        lambda action, **_kwargs: (
            calls.append(action)
            or (
                failed_refresh
                if action == "Refresh"
                else ActionResult("Save", True, "ok", 1, 300, False, "not_requested")
            )
        ),
    )

    assert refresh_pbip_model._ui_refresh_locked(context, Path("missing-cache.abf"), args) == 1
    assert calls == ["Refresh", "canaries"]


@pytest.mark.gui
@pytest.mark.serial
@pytest.mark.parametrize(
    ("control_type", "duplicate"),
    [("Button", False), ("SplitButton", False), ("Button", True)],
)
def test_synthetic_control_is_invoked_once_and_ambiguity_refused(control_type: str, duplicate: bool) -> None:  # pylint: disable=too-many-locals
    powershell = shutil.which("powershell.exe")
    assert os.name == "nt" and powershell, "the native action qualification requires Windows PowerShell"
    script = (
        "Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, "
        "UIAutomationProvider, System.Xaml; "
        "$references = @([System.Windows.Window].Assembly.Location, "
        "[System.Windows.UIElement].Assembly.Location, "
        "[System.Windows.Threading.DispatcherObject].Assembly.Location, "
        "[System.Windows.Automation.Peers.ButtonAutomationPeer].Assembly.Location, "
        "[System.Windows.Automation.Provider.IInvokeProvider].Assembly.Location, "
        "[System.Windows.Markup.IQueryAmbient].Assembly.Location); "
        "$peerCode = @'\n"
        "using System.Windows.Automation.Peers;\n"
        "using System.Windows.Controls;\n"
        "public sealed class SyntheticSplitButtonPeer : ButtonAutomationPeer {\n"
        "  public SyntheticSplitButtonPeer(Button owner) : base(owner) { }\n"
        "  protected override AutomationControlType GetAutomationControlTypeCore() "
        "{ return AutomationControlType.SplitButton; }\n"
        "}\n"
        "public sealed class SyntheticSplitButton : Button {\n"
        "  protected override AutomationPeer OnCreateAutomationPeer() "
        "{ return new SyntheticSplitButtonPeer(this); }\n"
        "}\n"
        "'@\n"
        "Add-Type -TypeDefinition $peerCode -ReferencedAssemblies $references; "
        "$form = [System.Windows.Window]::new(); $form.Title = 'Synthetic UIA'; "
        "$form.Width = 360; $form.Height = 180; "
        f"$button = if ('{control_type}' -eq 'SplitButton') "
        "{ [SyntheticSplitButton]::new() } else { [System.Windows.Controls.Button]::new() }; "
        "$button.Content = 'Refresh'; "
        "$button.Name = 'Refresh'; "
        "[System.Windows.Automation.AutomationProperties]::SetName($button, 'Refresh'); "
        "$button.Add_Click({ $form.Title = 'Synthetic invoked' }); "
        "$panel = [System.Windows.Controls.StackPanel]::new(); $null = $panel.Children.Add($button); "
        f"if ({'$true' if duplicate else '$false'}) {{ "
        "$second = [System.Windows.Controls.Button]::new(); $second.Content = 'Refresh'; "
        "$second.Name = 'Refresh'; "
        "[System.Windows.Automation.AutomationProperties]::SetName($second, 'Refresh'); "
        "$second.Add_Click({ $form.Title = 'Synthetic invoked' }); $null = $panel.Children.Add($second) "
        "}; $form.Content = $panel; "
        "$form.Add_ContentRendered({ "
        "$process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $PID); "
        "$handle = [System.Windows.Interop.WindowInteropHelper]::new($form).Handle; "
        "[Console]::Out.WriteLine(('READY|{0}|{1}' -f $handle.ToInt64(), "
        "$process.CreationDate.ToUniversalTime().Ticks)); [Console]::Out.Flush() "
        "}); $null = $form.ShowDialog()"
    )
    child = subprocess.Popen(  # pylint: disable=consider-using-with
        [powershell, "-NoProfile", "-STA", "-Command", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    lines: queue.Queue[str] = queue.Queue()

    def wait_for_ready() -> None:
        for line in child.stdout:
            if line.startswith("READY|"):
                lines.put(line)
                return

    reader = threading.Thread(target=wait_for_ready, daemon=True)
    reader.start()
    try:
        try:
            ready_line = lines.get(timeout=10).strip()
        except queue.Empty:
            child.terminate()
            child.wait(timeout=5)
            detail = child.stderr.read() if child.stderr is not None else ""
            pytest.fail(f"synthetic window did not publish its identity: {detail}")
        if not ready_line:
            child.terminate()
            child.wait(timeout=5)
            detail = child.stderr.read() if child.stderr is not None else ""
            pytest.fail(f"synthetic window did not publish its identity: {detail}")
        ready = ready_line.split("|")
        assert len(ready) == 3 and ready[0] == "READY", "synthetic window did not publish its identity"
        root_hwnd, process_start = int(ready[1]), ready[2]
        time.sleep(0.25)
        result = invoke_desktop_action(
            "Refresh",
            pid=child.pid,
            expected_process_start=process_start,
            root_hwnd=root_hwnd,
        )
        title = ctypes.create_unicode_buffer(256)
        deadline = time.monotonic() + 3
        if duplicate:
            assert not result.invoked and result.reason == "ambiguous_named_control"
            assert 1 <= result.focus_attempts <= 3
            ctypes.windll.user32.GetWindowTextW(root_hwnd, title, len(title))
            assert title.value == "Synthetic UIA", "ambiguous controls must not be activated"
        else:
            assert result.invoked and result.action_started_ticks is not None
            assert 1 <= result.focus_attempts <= 3
            assert isinstance(result.handback_verified, bool)
            while time.monotonic() < deadline:
                ctypes.windll.user32.GetWindowTextW(root_hwnd, title, len(title))
                if title.value == "Synthetic invoked":
                    break
                time.sleep(0.05)
            assert title.value == "Synthetic invoked", "the named action control was not activated"
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        reader.join(timeout=1)
