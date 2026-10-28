"""Focused tests for the explicit UI route's child protocol and engine completion authority."""

# The test module intentionally exercises private orchestration seams; assertions belong in tests,
# and the fake reader mirrors ADOMD.NET's PascalCase API rather than a Python naming convention.
# pylint: disable=missing-function-docstring,protected-access

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import ctypes
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import _desktop_actions as actions
import refresh_pbip_model
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


BASELINE = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


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
            return _Reader(self.connection.tables)
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
        partitions=[_partition(mode=None)],
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
    ("table_mode", "partition_mode", "kind", "refreshed", "query"),
    [
        (1, None, 4, BASELINE, "Source"),
        (0, 1, 4, BASELINE, "Source"),
        (99, None, 4, BASELINE, "Source"),
        (0, 0, 1, BASELINE, "Source"),
        (0, 0, 4, "2026-10-28T12:00:00Z", "Source"),
        (0, 0, 4, None, "Source"),
        (0, 0, 4, BASELINE, "Value.NativeQuery(Source, Query)"),
        (0, 0, 4, BASELINE, 'Sql.Database("server", "database", [Query="SELECT * FROM Orders"])'),
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
    for mode in (None, 1, 99):
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
            assert result.focus_attempts == 0
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
