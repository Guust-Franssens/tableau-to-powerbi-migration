"""
purpose: supervise bounded UIA actions and prove whole-model Import M processing from engine metadata
usage:   imported by refresh_pbip_model.py; not a user-facing command
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from _source_scope import SourceScopeError, check_source_scope, load_expected_sources
from probe_desktop_query import BoundDesktop, bound_call, is_auto_date_table_name

ACTION_SCRIPT = Path(__file__).resolve().with_name("_desktop_actions.ps1")
ACTION_CHILD_TIMEOUT_SECONDS = 20.0
ACTION_CHILD_CLEANUP_SECONDS = 5.0


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
    action_started_ticks: int | None


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


def _mode_name(value: object) -> str:
    """Decode TOM ModeType (Partition.Mode / Model.DefaultMode), not table storage enums."""
    raw = str(value).strip().casefold()
    return {
        "default": "Default",
        "2": "Default",
        "import": "Import",
        "0": "Import",
        "directquery": "DirectQuery",
        "direct_query": "DirectQuery",
        "1": "DirectQuery",
        "dual": "Dual",
        "4": "Dual",
        "directlake": "DirectLake",
        "direct_lake": "DirectLake",
        "5": "DirectLake",
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


def _read_table_modes(connection) -> dict[int, tuple[str, str]]:
    """Bind table identities and the model default used by ModeType.Default partitions."""
    model_mode = _read_model_mode(connection)
    rows = _read_rows(
        connection,
        "SELECT [ID], [Name] FROM $SYSTEM.TMSCHEMA_TABLES",
        2,
    )
    table_by_id = {}
    for table_id_value, table_name_value in rows:
        table_id = _integer(table_id_value, "table identity")
        if table_id in table_by_id:
            raise DesktopActionError("duplicate table identity")
        if not isinstance(table_name_value, str) or not table_name_value:
            raise DesktopActionError("table name is unresolved")
        # TMSCHEMA_TABLES has no StorageMode column. Never decode its hypothetical
        # representation as ModeType; the actual partition modes below detect mixed models.
        table_by_id[table_id] = (table_name_value, model_mode)
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
    table_by_id: dict[int, tuple[str, str]],
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
    partition_mode = _mode_name(row[4])
    effective_mode = table_mode if partition_mode == "Default" else partition_mode
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
    """Read all partition/table identities and actual model/partition ModeType metadata."""
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


def _scope_reads(connection) -> tuple[list[tuple[str, str, object]], dict[str, object]]:
    """Read partition M, shared expressions and refresh policies; any unexpected shape refuses."""
    table_by_id = _read_table_modes(connection)
    partitions = []
    for row in _partition_rows(connection):
        table = table_by_id.get(_integer(row[1], "table identity"))
        if table is None:
            raise DesktopActionError("partition table identity is unresolved")
        partitions.append((table[0], _partition_type(row[3]), row[6]))
    expressions: dict[str, object] = {}
    for name, text in _read_rows(connection, "SELECT [Name], [Expression] FROM $SYSTEM.TMSCHEMA_EXPRESSIONS", 2):
        if not isinstance(name, str) or not name or name in expressions:
            raise DesktopActionError("shared expression name is unresolved")
        expressions[name] = text
    if _read_rows(connection, "SELECT [TableID] FROM $SYSTEM.TMSCHEMA_REFRESH_POLICIES", 1):
        raise DesktopActionError("a refresh policy is present")
    return partitions, expressions


def check_automatic_source_scope(
    bound: BoundDesktop, expected_path: str, canaries: list[str], *, timeout_seconds: float
) -> None:
    """Refuse (#780) unless every live partition is an exact connector read of the caller's expected set."""
    try:
        expected = load_expected_sources(expected_path)
        partitions, expressions = bound_call(bound, _scope_reads, timeout_seconds=timeout_seconds)
        check_source_scope(partitions, expressions, expected, canaries)
    except (SourceScopeError, DesktopActionError) as exc:
        raise DesktopActionError(f"automatic source scope: {exc}") from None
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # A driver error may echo connection details, so only the failure class is reported.
        raise DesktopActionError("automatic source scope: metadata read failed") from None


def check_baseline(snapshot: PartitionSnapshot) -> None:
    """Admit a pre-action snapshot only when every source partition is READY (1) or never processed (3).

    A never-processed M partition (TOM ObjectState NoData) reports State 3 with a typed 1699-12-31
    RefreshedTime; localization re-points SourceFolder and returns every M partition to it, so no
    cache may be assumed. Every other state, error and in-progress included, refuses.
    """
    if not snapshot.partitions:
        raise DesktopActionError("initial source partition set is empty")
    refused = {
        partition.state if type(partition.state) is int else "non-integer"  # pylint: disable=unidiomatic-typecheck
        for partition in snapshot.partitions
        if type(partition.state) is not int or partition.state not in (1, 3)  # pylint: disable=unidiomatic-typecheck
    }
    if refused:
        states = ", ".join(sorted(map(str, refused)))
        raise DesktopActionError(f"initial source partition state is not READY or never-processed (state {states})")


def _comparable_refusal(baseline: PartitionSnapshot, current: PartitionSnapshot, action_started_ticks: int) -> str:
    """Why two snapshots cannot be compared partition by partition, or "" when they can."""
    if not isinstance(action_started_ticks, int) or isinstance(action_started_ticks, bool) or action_started_ticks <= 0:
        return "invalid action start timestamp"
    if baseline.identities != current.identities or len(current.partitions) != len(baseline.partitions):
        return "source partition identity set changed"
    return ""


def _partition_refusal(before, partition, action_started_ticks: int) -> str:
    """The one per-partition predicate: "" when ``partition`` advanced into READY after the action began."""
    if partition.source_kind != before.source_kind or partition.effective_mode != before.effective_mode:
        return "source partition kind or mode changed"
    if partition.state != 1:
        return "source partition is not READY"
    if partition.refreshed_ticks <= before.refreshed_ticks:
        return "source partition timestamp did not advance"
    if partition.refreshed_ticks < action_started_ticks:
        return "source partition timestamp predates the action"
    return ""


def check_completion(
    baseline: PartitionSnapshot,
    current: PartitionSnapshot,
    action_started_ticks: int,
) -> CompletionResult:
    """Require every exact source partition to advance into READY after this action began."""
    reason = _comparable_refusal(baseline, current, action_started_ticks)
    if not reason:
        previous = {partition.identity: partition for partition in baseline.partitions}
        for partition in current.partitions:
            reason = _partition_refusal(previous[partition.identity], partition, action_started_ticks)
            if reason:
                break
    return CompletionResult(not reason, reason or "all source partitions READY and refreshed after action start")


def count_refreshed(
    baseline: PartitionSnapshot,
    current: PartitionSnapshot,
    action_started_ticks: int,
) -> int | None:
    """How many source partitions already satisfy the completion predicate; None when not comparable."""
    if _comparable_refusal(baseline, current, action_started_ticks):
        return None
    previous = {partition.identity: partition for partition in baseline.partitions}
    return sum(
        not _partition_refusal(previous[partition.identity], partition, action_started_ticks)
        for partition in current.partitions
    )


def _action_result(payload: dict[str, object], action: Literal["Refresh", "Save"]) -> ActionResult:
    """Validate the typed action outcome."""
    invoked = payload.get("invoked")
    if not isinstance(invoked, bool):
        raise DesktopActionError("UIA child protocol is incomplete")
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
    return ActionResult(action, invoked, reason, ticks)


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
    if not isinstance(payload, dict) or payload.get("schema") != "pbip.desktop-action.v2":
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
