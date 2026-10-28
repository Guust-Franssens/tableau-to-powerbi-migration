"""
purpose: supervise bounded UIA actions and prove whole-model Import M processing from engine metadata
usage:   imported by refresh_pbip_model.py; not a user-facing command
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from probe_desktop_query import BoundDesktop, bound_call, is_auto_date_table_name

ACTION_SCRIPT = Path(__file__).resolve().with_name("_desktop_actions.ps1")
ACTION_CHILD_TIMEOUT_SECONDS = 20.0
ACTION_CHILD_CLEANUP_SECONDS = 5.0
CUSTOM_QUERY_RE = re.compile(
    r"\b(?:Value\.NativeQuery|(?:Sql|Odbc)\.Query)\s*\(|\bSql\.Database\s*\([^)]*?\[\s*Query\s*=",
    re.IGNORECASE,
)


class DesktopActionError(RuntimeError):
    """The bounded UIA action could not establish its requested result."""


@dataclass(frozen=True, slots=True)
class SourcePartition:
    """One exact Import M source partition and its typed engine baseline."""

    catalogue: str
    table_id: int
    partition_id: int
    source_kind: str
    effective_mode: str
    state: int
    refreshed_ticks: int

    @property
    def identity(self) -> tuple[str, int, int]:
        """The catalogue/table/partition identity used for exact-set comparison."""
        return (self.catalogue, self.table_id, self.partition_id)


@dataclass(frozen=True, slots=True)
class PartitionSnapshot:
    """A complete source-partition read from one held catalogue."""

    partitions: tuple[SourcePartition, ...]

    @property
    def identities(self) -> frozenset[tuple[str, int, int]]:
        """Unique source identities independent of DMV row ordering."""
        return frozenset(partition.identity for partition in self.partitions)


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """A complete engine predicate result; incomplete remains explicitly non-success."""

    complete: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Closed protocol result from one bounded child invocation."""

    action: Literal["Refresh", "Save"]
    invoked: bool
    reason: str
    focus_attempts: int
    action_started_ticks: int | None
    restore_requested: bool
    handback_state: Literal["verified", "not_requested", "failed"]

    @property
    def handback_verified(self) -> bool:
        """True only when no restore was needed or requested handback was verified."""
        return self.handback_state in {"verified", "not_requested"}


def _integer(value: object, field: str) -> int:
    """Require typed integral engine metadata instead of accepting display-string parsing."""
    if isinstance(value, (bool, float, str, bytes)):
        raise DesktopActionError(f"unsupported {field}")
    integral = value
    try:
        result = int(integral)
    except (TypeError, ValueError, OverflowError) as exc:
        try:
            integral = value.value__
            result = int(integral)
        except (AttributeError, TypeError, ValueError, OverflowError):
            raise DesktopActionError(f"unsupported {field}") from exc
    if result != integral:
        raise DesktopActionError(f"unsupported {field}")
    return result


def _dotnet_utc_ticks(value: object) -> int | None:
    """Convert a typed .NET DateTime to the engine UTC tick basis."""
    ticks = getattr(value, "Ticks", None)
    if ticks is None:
        return None
    kind = getattr(value, "Kind", None)
    kind_name = str(kind).casefold() if kind is not None else "unspecified"
    if kind_name.endswith("local"):
        converter = getattr(value, "ToUniversalTime", None)
        if converter is None:
            raise DesktopActionError("partition refresh time timezone is unknown")
        ticks = getattr(converter(), "Ticks", None)
    elif not (kind_name.endswith("utc") or kind_name.endswith("unspecified")):
        raise DesktopActionError("partition refresh time timezone is unknown")
    # TMSCHEMA_PARTITIONS.RefreshedTime is UTC; ADOMD may expose an unspecified-kind DateTime.
    return _integer(ticks, "partition refresh time")


def _typed_utc_ticks(value: object) -> int:
    """Read the typed DMV timestamp on its UTC 100-ns basis, never parse localized text."""
    if value is None or isinstance(value, (str, bytes)):
        raise DesktopActionError("partition refresh time is absent or not typed")

    result = _dotnet_utc_ticks(value)
    if result is None:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise DesktopActionError("partition refresh time is not a typed timezone-aware datetime")
        utc = value.astimezone(timezone.utc)
        epoch = datetime(1, 1, 1, tzinfo=timezone.utc)
        result = (utc - epoch).days * 864_000_000_000
        result += (utc - epoch).seconds * 10_000_000 + utc.microsecond * 10
    if result <= 0:
        raise DesktopActionError("partition refresh time is invalid")
    return result


def _mode_name(value: object) -> str | None:
    """Normalize documented engine enum values; unknown numbers never default to Import."""
    if value is None:
        return None
    raw = str(value).strip().casefold()
    return {
        "": None,
        "default": None,
        "unspecified": None,
        "null": None,
        "import": "Import",
        "0": "Import",
        "directquery": "DirectQuery",
        "direct_query": "DirectQuery",
        "1": "DirectQuery",
        "dual": "Dual",
        "2": "Dual",
        "directlake": "DirectLake",
        "direct_lake": "DirectLake",
        "3": "DirectLake",
    }.get(raw, "Unsupported")


def _partition_type(value: object) -> str:
    """Normalize supported source/calculated partition types from the engine DMV."""
    raw = str(value).strip().casefold()
    if raw in {"4", "m"}:
        return "M"
    if raw in {"2", "calculated"}:
        return "Calculated"
    return "Unsupported"


def _read_rows(connection, query: str, column_count: int) -> list[tuple[object, ...]]:
    """Read a complete DMV result set with its native typed values."""
    command = connection.CreateCommand()
    command.CommandText = query
    reader = command.ExecuteReader()
    rows = []
    try:
        while reader.Read():
            rows.append(
                tuple(None if reader.IsDBNull(index) else reader.GetValue(index) for index in range(column_count))
            )
    finally:
        reader.Close()
    return rows


def _read_model_mode(connection) -> str:
    """Resolve the model's actual default storage mode rather than guessing Import."""
    rows = _read_rows(connection, "SELECT [DefaultMode] FROM $SYSTEM.TMSCHEMA_MODEL", 1)
    if len(rows) != 1:
        raise DesktopActionError("model default mode is missing or ambiguous")
    mode = _mode_name(rows[0][0])
    if mode != "Import":
        raise DesktopActionError("unsupported or unresolved model default mode")
    return mode


def _read_table_modes(connection) -> dict[int, tuple[str, str | None]]:
    """Read the actual table storage modes used to resolve inherited partition mode."""
    model_mode = _read_model_mode(connection)
    rows = _read_rows(
        connection,
        "SELECT [ID], [Name], [StorageMode] FROM $SYSTEM.TMSCHEMA_TABLES",
        3,
    )
    table_by_id = {}
    for table_id_value, table_name_value, table_mode_value in rows:
        table_id = _integer(table_id_value, "table identity")
        if table_id in table_by_id:
            raise DesktopActionError("duplicate table identity")
        if not isinstance(table_name_value, str) or not table_name_value:
            raise DesktopActionError("table name is unresolved")
        table_mode = _mode_name(table_mode_value) or model_mode
        if table_mode != "Import":
            raise DesktopActionError("unsupported or mixed table storage mode")
        table_by_id[table_id] = (table_name_value, table_mode)
    return table_by_id


def _partition_rows(connection) -> list[tuple[object, ...]]:
    """Read engine source-partition identities and their typed state/timestamp fields."""
    return _read_rows(
        connection,
        "SELECT [ID], [TableID], [State], [Type], [Mode], [RefreshedTime], [QueryDefinition] "
        "FROM $SYSTEM.TMSCHEMA_PARTITIONS",
        7,
    )


def _source_partition(
    row: tuple[object, ...],
    table_by_id: dict[int, tuple[str, str | None]],
    catalogue: str,
    seen: set[tuple[str, int, int]],
) -> SourcePartition | None:
    """Resolve one supported source row; exclude only calculated scaffolding/non-source tables."""
    table_id = _integer(row[1], "table identity")
    table = table_by_id.get(table_id)
    if table is None:
        raise DesktopActionError("partition table identity is unresolved")
    table_name, table_mode = table
    if is_auto_date_table_name(table_name):
        return None

    partition_type = _partition_type(row[3])
    effective_mode = _mode_name(row[4]) or table_mode
    if effective_mode is None or effective_mode == "Unsupported":
        raise DesktopActionError("partition effective mode is unresolved")
    if effective_mode != "Import":
        raise DesktopActionError("unsupported partition mode")
    if table_mode not in (None, "Import"):
        raise DesktopActionError("unsupported or mixed table storage mode")
    if partition_type == "Calculated":
        return None
    if partition_type != "M":
        raise DesktopActionError("unsupported partition source kind")
    if not isinstance(row[6], str) or CUSTOM_QUERY_RE.search(row[6]):
        raise DesktopActionError("custom-query source mode is unsupported")

    partition_id = _integer(row[0], "partition identity")
    identity = (catalogue, table_id, partition_id)
    if identity in seen:
        raise DesktopActionError("duplicate source partition identity")
    seen.add(identity)
    return SourcePartition(
        catalogue=catalogue,
        table_id=table_id,
        partition_id=partition_id,
        source_kind="M",
        effective_mode="Import",
        state=_integer(row[2], "partition state"),
        refreshed_ticks=_typed_utc_ticks(row[5]),
    )


def _snapshot_from_connection(connection, catalogue: str) -> PartitionSnapshot:
    """Read all partition and table storage metadata required to resolve the effective mode."""
    table_by_id = _read_table_modes(connection)
    rows = _partition_rows(connection)
    partitions: list[SourcePartition] = []
    seen: set[tuple[str, int, int]] = set()
    for row in rows:
        partition = _source_partition(row, table_by_id, catalogue, seen)
        if partition is not None:
            partitions.append(partition)
    if not partitions:
        raise DesktopActionError("no supported Import M source partitions")
    return PartitionSnapshot(tuple(sorted(partitions, key=lambda item: item.identity)))


def read_partition_snapshot(bound: BoundDesktop, *, timeout_seconds: float = 8.0) -> PartitionSnapshot:
    """Read a complete typed partition snapshot through the existing held-binding seam."""

    def read(connection):
        return _snapshot_from_connection(connection, bound.catalogue)

    return bound_call(bound, read, timeout_seconds=timeout_seconds)


def check_completion(
    baseline: PartitionSnapshot,
    current: PartitionSnapshot,
    action_started_ticks: int,
) -> CompletionResult:
    """Require every exact source partition to advance into READY after this action began."""
    reason = ""
    if not isinstance(action_started_ticks, int) or isinstance(action_started_ticks, bool) or action_started_ticks <= 0:
        reason = "invalid action start timestamp"
    elif baseline.identities != current.identities or len(current.partitions) != len(baseline.partitions):
        reason = "source partition identity set changed"
    else:
        previous = {partition.identity: partition for partition in baseline.partitions}
        for partition in current.partitions:
            before = previous[partition.identity]
            if partition.source_kind != before.source_kind or partition.effective_mode != before.effective_mode:
                reason = "source partition kind or mode changed"
            elif partition.state != 1:
                reason = "source partition is not READY"
            elif partition.refreshed_ticks <= before.refreshed_ticks:
                reason = "source partition timestamp did not advance"
            elif partition.refreshed_ticks < action_started_ticks:
                reason = "source partition timestamp predates the action"
            if reason:
                break
    return CompletionResult(not reason, reason or "all source partitions READY and refreshed after action start")


def _action_result(payload: dict[str, object], action: Literal["Refresh", "Save"]) -> ActionResult:
    """Validate the typed action outcome and honest foreground handback fields."""
    invoked = payload.get("invoked")
    requested = payload.get("restore_requested")
    verified = payload.get("restore_verified")
    handback_ok = payload.get("handback_ok")
    attempts = payload.get("focus_attempts")
    if not isinstance(invoked, bool) or not isinstance(requested, bool) or not isinstance(handback_ok, bool):
        raise DesktopActionError("UIA child protocol is incomplete")
    if verified is not None and not isinstance(verified, bool):
        raise DesktopActionError("UIA child handback result is invalid")
    if requested != (verified is not None):
        raise DesktopActionError("UIA child handback protocol is inconsistent")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 <= attempts <= 3:
        raise DesktopActionError("UIA child focus result is invalid")
    ticks_value = payload.get("action_started_ticks")
    if ticks_value is not None and (
        not isinstance(ticks_value, str) or not ticks_value.isascii() or not ticks_value.isdecimal()
    ):
        raise DesktopActionError("UIA child action start timestamp is invalid")
    ticks = None if ticks_value is None else int(ticks_value)
    if invoked and (ticks is None or ticks <= 0 or payload.get("status") != "invoked"):
        raise DesktopActionError("UIA child did not establish the Invoke")
    if not invoked and payload.get("status") != "refused":
        raise DesktopActionError("UIA child refusal is ambiguous")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason:
        raise DesktopActionError("UIA child reason is missing")
    handback_state = (
        "verified" if requested and verified and handback_ok else "not_requested" if handback_ok else "failed"
    )
    return ActionResult(action, invoked, reason, attempts, ticks, requested, handback_state)


def _read_action_output(
    output: str,
    action: Literal["Refresh", "Save"],
    *,
    pid: int,
    expected_process_start: str,
    root_hwnd: int,
) -> ActionResult:
    """Validate the closed JSON protocol and reject ambiguous or partial child output."""
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DesktopActionError("UIA child returned invalid protocol data") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "pbip.desktop-action.v1":
        raise DesktopActionError("UIA child returned an unsupported protocol")
    if (
        payload.get("action") != action
        or payload.get("desktop_pid") != str(pid)
        or payload.get("process_started_at") != expected_process_start
        or payload.get("root_hwnd") != str(root_hwnd)
    ):
        raise DesktopActionError("UIA child action did not match request")
    return _action_result(payload, action)


def invoke_desktop_action(
    action: Literal["Refresh", "Save"],
    *,
    pid: int,
    expected_process_start: str,
    root_hwnd: int,
    timeout_seconds: float = ACTION_CHILD_TIMEOUT_SECONDS,
) -> ActionResult:
    """Run one non-interactive, killable PowerShell action child and reap it on every path."""
    if os.name != "nt":
        raise DesktopActionError("UIA actions require Windows")
    if timeout_seconds <= 0:
        raise DesktopActionError("UIA action deadline has already expired")
    executable = "powershell.exe"
    command = [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ACTION_SCRIPT),
        "-Action",
        action,
        "-DesktopPid",
        str(pid),
        "-ExpectedProcessStart",
        expected_process_start,
        "-RootHandle",
        str(root_hwnd),
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        # A Popen context manager waits without a timeout on exit; explicit cleanup below keeps
        # the child deadline bounded and still reaps the process handle.
        child = subprocess.Popen(  # pylint: disable=consider-using-with
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise DesktopActionError("could not start bounded UIA child") from exc

    try:
        try:
            stdout, _ = child.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                child.kill()
            except OSError:
                pass
            try:
                child.wait(timeout=ACTION_CHILD_CLEANUP_SECONDS)
            except subprocess.TimeoutExpired as cleanup_exc:
                raise DesktopActionError("timed-out UIA child could not be reaped") from cleanup_exc
            raise DesktopActionError("UIA child exceeded its bounded deadline") from exc
        if child.returncode != 0:
            raise DesktopActionError("UIA child failed before establishing its result")
    finally:
        if child.stdout is not None:
            child.stdout.close()
    return _read_action_output(
        stdout,
        action,
        pid=pid,
        expected_process_start=expected_process_start,
        root_hwnd=root_hwnd,
    )
