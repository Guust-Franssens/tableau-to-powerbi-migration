"""
purpose: Refresh a local PBIP model through Power BI Desktop's own Refresh, persist it through
         Desktop's own guarded Save, and prove the data is really there, so the next agent (and the
         next Desktop open) sees real data instead of an empty model.
usage:   python .github/skills/pbip-model-refresh/scripts/refresh_pbip_model.py --pid <pbidesktop-pid>
             [--canaries "A" "B"] [--no-save] [--automatic-source-scope EXPECTED_SOURCES_JSON]
             [--verify-only | --save-only]
         (ships inside the `pbip-model-refresh` skill; run it by its path from wherever the folder
          was copied. `scripts/refresh_pbip_model.py` in this repo is a forwarding shim. To open,
          refresh, Save and close in one step use `open_desktop.py <pbip> --refresh` or `t2p refresh`.)

WARNING: the default refresh executes ALL current M of the model, including native/custom SQL, with
possible source-side cost. `--no-save` prevents Save, NOT source execution. Automatic callers add
`--automatic-source-scope <file>` together with `--canaries`.

How it works
------------
- `--pid` is a Desktop that is already open: it must have no unsaved changes (they may be a human's
  work), and it stays open afterwards. `open_desktop.py <pbip> --refresh` runs this same refresh on a
  Desktop it has just opened, where that flag is ignored, then closes it.
- Default: check the bridge, the observer and the model identity, invoke Desktop's own Refresh through
  UI Automation, wait for typed engine completion (every source partition refreshed after the Invoke),
  run the canaries, then Desktop's own guarded Save, then print one `REFRESH:` verdict. `--no-save`
  skips Save only. A dialog is never clicked: it stops the run for a human (`DIALOG_NEEDS_HUMAN`).
- Progress: `REFRESH_STATUS` lines stream on every field change and at least every 30 seconds, read
  from a separate ADOMD connection that stays readable while Desktop's refresh popup is up. Progress
  never decides the result. `REFRESH_SUMMARY` precedes the data verdict; on a refusal it follows the
  refusal line.
- `--save-only` (after an MCP Calculate of MCP-made DAX edits): the canaries on what is loaded now,
  then Desktop's guarded Save, then the model check against the freshly written TMDL. Zero-row
  canaries or a binding refusal stop before Save. It never refreshes and earns `REFRESH: SAVE_OK`,
  never a claim of fresh source data.
- `--verify-only`: read-only identity and canary checks; no Refresh and no Save.

Cache invalidation (important)
------------------------------
Desktop discards `cache.abf` when the model **definition** is newer than the cache. Verified: a
model whose `definition/*.tmdl` were touched a week after the cache was written opened with
`NO_DATA` despite a 113 KB cache sitting right there. So: make ALL model edits FIRST, then refresh,
then save. Anything that rewrites TMDL afterwards - including the host repo's sanitize step (here,
`scripts/set_data_folder.py --sanitize`, which you must run before committing) - invalidates it.

Binding to the right instance (parallel batches)
------------------------------------------------
The destination cache is resolved from the file Desktop has open for `--pid`, but the DATA comes from
whatever Analysis Services instance answers on the port. Two defences run before any Refresh or Save:
`--port`, if given, must equal the port derived from `--pid` (a mismatch aborts, so it can never bypass
pid-based discovery), and `same_model()` compares the connected model's tables, columns and measures
with the TMDL that owns the destination cache and aborts on any difference with `REFRESH: WRONG_MODEL`.
`--save-only` runs `same_model()` after Save instead, because MCP DAX edits legitimately differ from the
TMDL on disk; before its Save the PID, port and catalogue binding hold, and Desktop's own Save writes
only its own model to its own file.
File metadata cannot tell you whose rows are in the cache; only the model's own contents can.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
from probe_desktop_query import (
    AdomdUnavailable,
    BoundDesktop,
    DesktopIdentity,
    ObservationUnavailable,
    _load_adomd,
    bind_desktop,
    bound_call,
    column_names,
    discover_port,
    first_table,
    is_auto_date_table_name,
    measure_names,
    probe_observations,
    table_names,
)
from _desktop_actions import (
    ACTION_CHILD_TIMEOUT_SECONDS,
    ActionResult,
    CompletionResult,
    DesktopActionError,
    PartitionSnapshot,
    _scope_reads,
    check_automatic_source_scope,
    check_baseline,
    check_completion,
    invoke_desktop_action,
    read_partition_snapshot,
)
from _source_scope import SourceScopeError, check_source_scope, load_expected_sources

# Per-model interprocess lock that serializes one model's UI Refresh/Save, in its own module;
# re-exported so callers and tests reach it via refresh_pbip_model.
from _lock import (  # noqa: F401  # pylint: disable=unused-import
    LockUnavailable,
    ModelLockTimeout,
    model_lock,
)

# The verdict/reporting layer lives in _verdict to keep this module under its line cap; re-exported
# here so callers and tests reach it via refresh_pbip_model.
from _verdict import (  # noqa: F401  # pylint: disable=unused-import
    _canary_tables,
    _emit_data_verdict,
    derive_data_verdict,
)
from _desktop_observer import (
    Observation,
    CredentialUnknownError,
    DesktopGoneError,
    DesktopUnreadyError,
    DIALOG_NEEDS_HUMAN,
    DialogFinding,
    DialogFoundError,
    observe,
    emit_observation,
    raise_terminal_detection,
    print_operation_refusal,
    dialog_stop_detail,
    main_frame,
    POLL_INTERVAL_SECONDS,
)


@dataclass(frozen=True, slots=True)
class RefreshObservation:
    """Desktop's own whole-model Refresh, completed per typed partition evidence; rows are not asserted."""

    catalogue: str
    refresh_type: Literal["full", "calculate"]
    scope: Literal["database", "tables"]
    tables: tuple[str, ...]
    identity: DesktopIdentity


@dataclass(frozen=True, slots=True)
class SavedCache:
    """The cache file Desktop's own Save left on disk, read back once; durability stays unestablished."""

    sha256: str
    size: int
    commitment: Literal["UNESTABLISHED"] = "UNESTABLISHED"


@dataclass(frozen=True, slots=True)
class SaveObservation:
    """Desktop's own guarded Save of the bound model and one checked readback of its cache."""

    catalogue: str
    cache: SavedCache
    identity: DesktopIdentity
    method: Literal["DESKTOP_UI_SAVE"] = "DESKTOP_UI_SAVE"


@dataclass(frozen=True, slots=True)
class UIRefreshContext:
    """The held Desktop/model identity shared by one UI refresh or save operation."""

    pid: int
    port: int
    bound: BoundDesktop
    root_hwnd: int


# How long a UI Refresh/Save waits for a CONCURRENT run on the same model to release the per-model
# lock (issue #114). A dead holder's lock is reclaimed at once, never waited on. On timeout the CLI
# refuses with `NOT_PERSISTED` and the API raises `ModelLockTimeout`, before any Desktop action.
PERSIST_LOCK_TIMEOUT_SECONDS = 120.0
# The whole-operation deadline. It must be comfortably above measured 700-750s customer Snowflake
# refreshes.
REFRESH_ABSOLUTE_TIMEOUT_SECONDS = 3600.0
REFRESH_CREDENTIAL_POLL_SECONDS = POLL_INTERVAL_SECONDS
REFRESH_HEARTBEAT_SECONDS = 30.0
# After a UI Refresh completes or a UI Save returns, Desktop's own popup can linger briefly. The UI
# route waits at most this long (inside the operation deadline) for READY before judging what is left.
UI_SETTLE_SECONDS = 60.0
# Bound-reader refusals that Desktop's own refresh popup produces. The completion reader retries on
# these; the outer observer still stops at once on credential, needs-human, session or process loss.
UI_TRANSIENT_READ_TOKENS = frozenset({"DIALOG_NEEDS_HUMAN"})

# A TMDL table declaration sits at column 0 of `definition/tables/<Name>.tmdl`; the name is quoted
# only when it needs to be (spaces, punctuation), so both forms have to be accepted.
TABLE_DECL_RE = re.compile(r"^table\s+(?:'([^']+)'|(\S+))", re.MULTILINE)
# A column/measure declaration sits INDENTED under its table, and is quoted only when it must be.
# The unquoted branch stops at whitespace OR `=`, so a calculated column/measure (`column 'X' = expr`
# or `measure Total = SUM(...)`) yields just the name, never the expression that follows.
COLUMN_DECL_RE = re.compile(r"^\s*column\s+(?:'([^']+)'|([^=\s]+))", re.MULTILINE)
MEASURE_DECL_RE = re.compile(r"^\s*measure\s+(?:'([^']+)'|([^=\s]+))", re.MULTILINE)


def _credential_state(
    pid: int,
    *,
    in_flight: bool = False,
    expected_process_start: str | None = None,
    timeout_s: float = 8.0,
) -> Observation:
    """Return one factual snapshot; in-flight progress policy belongs to the caller."""
    # Phase remains with the caller; the shared observer never suppresses factual progress.
    # pylint: disable=unused-argument
    state = observe(pid, expected_process_start=expected_process_start, timeout_s=timeout_s)
    emit_observation(state)
    return state


def _in_flight_credential_state(
    pid: int,
    *,
    expected_process_start: str | None = None,
    timeout_s: float = 8.0,
) -> Observation:
    """Single-argument in-flight detector for the two wait loops (#376 review, finding 1).

    Deliberately looks ``_credential_state`` up through the module global on every call, so a test that
    monkeypatches ``_credential_state`` still takes effect through this indirection - and so production
    and the tests exercise one function rather than two.
    """
    return _credential_state(
        pid,
        in_flight=True,
        expected_process_start=expected_process_start,
        timeout_s=timeout_s,
    )


def _raise_if_blocked(pid: int, state: Observation) -> None:
    """Any blocking dialog, a gone process or a window-less Desktop stops the operation before it starts."""
    raise_terminal_detection(pid, state)
    if state.desktop_unready is not None:
        raise DesktopUnreadyError(pid, state.desktop_unready)


def _emit_dialog_finding(pid: int, finding: DialogFinding) -> None:
    """Name the open dialog for the human who must act (#831); the title is never persisted."""
    print_operation_refusal("REFRESH", pid, DIALOG_NEEDS_HUMAN, finding=finding)


def _emit_credential_unknown(pid: int, reason: str) -> None:
    """Retain the existing deadline-uncertainty token without inventing its cause."""
    # pylint: disable=unused-argument
    print_operation_refusal("REFRESH", pid, "CREDENTIAL_UNKNOWN")


def _emit_ui_observation_unknown(pid: int, phase: str) -> None:
    """Same token for the UI route, without claiming the uncertainty lasted to the deadline."""
    print(
        f"REFRESH: CREDENTIAL_UNKNOWN pid={pid}; {phase} observation unavailable; "
        "credential state unestablished. Inspect Desktop before retrying.",
        flush=True,
    )


def _emit_desktop_gone(pid: int, reason: str) -> None:
    """Local process failure is not a source conclusion; never reflect supplied prose."""
    # pylint: disable=unused-argument
    print_operation_refusal("REFRESH", pid, "DESKTOP_GONE")


def _emit_desktop_unready(pid: int, reason: str) -> None:
    """Use the shared local window/session diagnostic, retaining the caller's exit band."""
    # pylint: disable=unused-argument
    print_operation_refusal("REFRESH", pid, "DESKTOP_UNREADY")


def _ui_desktop_alive(pid: int) -> bool:
    """Check only the held PID without querying its engine or enumerating sibling windows."""
    if os.name != "nt":
        raise DesktopActionError("bridge readiness requires Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, not mutation access
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: PID does not exist
            return False
        raise DesktopActionError("Desktop process state unavailable")
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state not in (0, 258):
            raise DesktopActionError("Desktop process state unavailable")
        return state == 258  # WAIT_TIMEOUT: process is still running
    finally:
        kernel.CloseHandle(handle)


class _BridgeStatusJob:
    """Contain only a suspended status launcher and its descendants, including after launcher exit."""

    class Limits(ctypes.Structure):  # pylint: disable=too-few-public-methods
        """JOBOBJECT_EXTENDED_LIMIT_INFORMATION, including native pointer-sized alignment."""

        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", ctypes.c_uint32),
            ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_limit", ctypes.c_uint32),
            ("affinity", ctypes.c_size_t),
            ("priority", ctypes.c_uint32),
            ("scheduling", ctypes.c_uint32),
            ("io_counters", ctypes.c_uint64 * 6),
            ("memory_limits", ctypes.c_size_t * 4),
        ]

    class Threads(ctypes.Structure):  # pylint: disable=too-few-public-methods
        """THREADENTRY32 for locating the still-suspended launcher's sole initial thread."""

        _fields_ = [(name, ctypes.c_uint32) for name in ("size", "usage", "tid", "pid", "base", "delta", "flags")]

    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, dword, pointer = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p
        for name, arguments, result in (
            ("CreateJobObjectW", [pointer, ctypes.c_wchar_p], handle),
            ("SetInformationJobObject", [handle, ctypes.c_int, pointer, dword], ctypes.c_int),
            ("AssignProcessToJobObject", [handle, handle], ctypes.c_int),
            ("TerminateJobObject", [handle, dword], ctypes.c_int),
            ("QueryInformationJobObject", [handle, ctypes.c_int, pointer, dword, pointer], ctypes.c_int),
            ("CreateToolhelp32Snapshot", [dword, dword], handle),
            ("Thread32First", [handle, pointer], ctypes.c_int),
            ("Thread32Next", [handle, pointer], ctypes.c_int),
            ("OpenThread", [dword, ctypes.c_int, dword], handle),
            ("ResumeThread", [handle], dword),
            ("CloseHandle", [handle], ctypes.c_int),
        ):
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = self.Limits()
        limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; the handle is never inherited.
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise ctypes.WinError(error)

    def start(self, child: subprocess.Popen) -> None:
        """Assign before allowing any launcher instruction or descendant creation."""
        if not self.kernel.AssignProcessToJobObject(self.handle, int(child._handle)):  # pylint: disable=protected-access
            error = ctypes.get_last_error()
            child.kill()  # Still suspended: there cannot yet be a descendant outside the job.
            child.wait(timeout=2.0)
            raise ctypes.WinError(error)
        snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = self.Threads(size=ctypes.sizeof(self.Threads))
            found = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.pid == child.pid:
                    thread = self.kernel.OpenThread(2, False, entry.tid)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self.kernel.ResumeThread(thread) != 1:
                            raise OSError("status launcher's initial thread was not suspended exactly once")
                        return
                    finally:
                        self.kernel.CloseHandle(thread)
                found = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("status launcher's suspended initial thread unavailable")
        finally:
            self.kernel.CloseHandle(snapshot)

    def close(self) -> None:
        """Terminate the entire owned job and positively verify no active processes remain."""
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        try:
            if not self.kernel.TerminateJobObject(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            deadline = time.monotonic() + 2.0
            # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: four LARGE_INTEGERs then four DWORDs.
            accounting = (ctypes.c_uint64 * 6)()
            while True:
                if not self.kernel.QueryInformationJobObject(handle, 1, accounting, ctypes.sizeof(accounting), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                active = ctypes.c_uint32.from_buffer(accounting, 40).value
                if active == 0:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("owned status job did not empty")
                time.sleep(0.01)
        finally:
            if not self.kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


def _ui_bridge_command() -> list[str]:
    """Resolve the already-installed CLI; never ask npm to acquire a package."""
    launcher = shutil.which("powerbi-desktop")
    if launcher is None:
        raise DesktopActionError("installed Desktop bridge launcher unavailable")
    return [str(Path(launcher).resolve())]


def _ui_bridge_status(  # pylint: disable=too-many-statements,too-many-locals,too-many-branches
    command: list[str], pid: int | None, deadline: float
) -> dict:
    """One contained status RPC under the caller's deadline: exact PID, or (pid None) every instance at once."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    # Without --pid the bridge waits the whole --wait-seconds when nothing is open, so a listing never waits.
    target = ["--wait-seconds", "0"] if pid is None else ["--pid", str(pid), "--wait-seconds", str(remaining)]
    arguments = [*command, "status", *target]
    environment = None
    if os.name == "nt" and Path(command[0]).suffix.lower() in {".cmd", ".bat"}:
        if any(any(character in argument for character in '"\r\n\x00') for argument in arguments):
            raise DesktopActionError("invalid batch launcher argument")
        # Expand values once, inside quotes, with delayed expansion disabled. Passing the values
        # through the environment preserves literal %, !, ^ and shell operators in installed paths.
        environment = os.environ.copy()
        names = [f"PBIP_UI_BRIDGE_ARG_{index}" for index in range(len(arguments))]
        environment.update(zip(names, arguments))
        batch = '"' + " ".join(f'"%{name}%"' for name in names) + '"'
        # cmd.exe does not implement the backslash-quote escaping used for argv lists.
        arguments = (
            subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/v:off", "/s", "/c"]) + " " + batch
        )
    job = _BridgeStatusJob() if os.name == "nt" else None
    child = None
    pipe_settled = False
    try:
        child = subprocess.Popen(  # pylint: disable=consider-using-with
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | (0x4 if job else 0),  # CREATE_SUSPENDED
        )
        if job is not None:
            job.start(child)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 0)
            output, _ = child.communicate(timeout=remaining)
            pipe_settled = True
        except subprocess.TimeoutExpired as exc:
            try:
                if job is not None:
                    owned_job, job = job, None
                    owned_job.close()
                else:
                    child.kill()
                child.communicate(timeout=2.0)
                pipe_settled = True
            except (OSError, subprocess.TimeoutExpired) as cleanup_exc:
                print("  bridge : owned status-child cleanup unconfirmed; refresh remains withheld", flush=True)
                raise DesktopActionError("bridge status cleanup unavailable") from cleanup_exc
            raise TimeoutError from exc
        if time.monotonic() >= deadline:
            raise TimeoutError
        if child.returncode != 0:
            raise DesktopActionError("bridge status command failed")
    finally:
        try:
            if job is not None:
                job.close()
                if child is not None and not pipe_settled:
                    child.communicate(timeout=2.0)
                    pipe_settled = True
        except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
            print("  bridge : owned status-child cleanup unconfirmed; refresh remains withheld", flush=True)
            raise DesktopActionError("bridge status cleanup unavailable") from exc
        finally:
            # Launcher exit alone cannot establish that an inherited-pipe reader has settled.
            if child is not None and child.stdout is not None and pipe_settled:
                child.stdout.close()
    if time.monotonic() >= deadline:
        raise TimeoutError
    try:
        payload = json.loads(output)
    except (ValueError, TypeError) as exc:
        raise DesktopActionError("malformed bridge status") from exc
    if not isinstance(payload, dict):
        raise DesktopActionError("malformed bridge status")
    return payload


def _bridge_readiness_result(payload: dict, pid: int) -> tuple[dict | None, float]:
    """Only positive application-state readiness or the narrowly recognized pending states."""
    instances = payload.get("instances")
    if not isinstance(instances, list):
        raise DesktopActionError("malformed bridge instances")
    if payload.get("error"):
        raise DesktopActionError("bridge application error")
    if payload.get("status") == "not_connected" and instances == []:
        if (
            payload.get("diagnostics")
            or payload.get("error")
            or payload.get("lastError") not in (None, f"No Power BI Desktop Bridge pipe found for pid {pid}.")
        ):
            raise DesktopActionError("bridge discovery refused")
        return None, 0.5
    if len(instances) != 1 or not isinstance(instances[0], dict):
        raise DesktopActionError("ambiguous bridge instance")
    instance = instances[0]
    if type(instance.get("pid")) is not int or instance["pid"] != pid:  # pylint: disable=unidiomatic-typecheck
        raise DesktopActionError("bridge PID mismatch")
    if payload.get("status") == "ready" and instance.get("bridgeStatus") == "connected":
        path = instance.get("currentFilePath")
        if instance.get("error") or payload.get("error"):
            raise DesktopActionError("bridge application error")
        if not isinstance(path, str) or not path.strip() or "\x00" in path or not Path(path).is_absolute():
            raise DesktopActionError("bridge file binding unavailable")
        return instance, 0.0
    error = instance.get("error")
    if (
        payload.get("status") == "error"
        and instance.get("bridgeStatus") == "error"
        and isinstance(error, dict)
        and error.get("code") == "BRIDGE_ERROR"
        and error.get("message") == "Another operation is already in progress"
    ):
        if error.get("retryable") is not True:
            raise DesktopActionError("bridge busy retry not authorized")
        delay = error.get("retryAfterMs")
        if (
            isinstance(delay, (int, float))
            and not isinstance(delay, bool)
            and 0 <= delay <= 2**53
            and math.isfinite(delay)
        ):
            return None, min(1.0, max(0.05, delay / 1000))
    raise DesktopActionError("bridge not ready or terminal error")


def _wait_ui_bridge(pid: int, deadline: float) -> dict:
    """A UI-only readiness loop; no refresh authority and no reset of the held deadline."""
    command = _ui_bridge_command()
    print("  bridge : waiting for exact-PID application readiness (at most 60 seconds)", flush=True)
    pending_reported = False
    while time.monotonic() < deadline:
        if not _ui_desktop_alive(pid):
            raise DesktopActionError("Desktop process gone")
        payload = _ui_bridge_status(command, pid, deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError
        instance, delay = _bridge_readiness_result(payload, pid)
        if time.monotonic() >= deadline:
            raise TimeoutError
        if instance is not None:
            print("  bridge : ready; existing observer and model checks still required", flush=True)
            return instance
        if not pending_reported:
            print("  bridge : pending; refresh remains withheld", flush=True)
            pending_reported = True
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
    raise TimeoutError


def _ui_bridge_preflight(pid: int, deadline: float) -> dict | None:
    """Latch failure before one existing read-only diagnostic; it cannot clear readiness refusal."""
    try:
        return _wait_ui_bridge(pid, deadline)
    except TimeoutError:
        print("REFRESH: TIMEOUT - bridge readiness not established; no action invoked; cause unknown", flush=True)
    except (DesktopActionError, OSError):
        print("REFRESH: UI_PREFLIGHT_REFUSED (bridge readiness unavailable; no action invoked)", flush=True)
    try:
        state = _credential_state(pid)
        if state.dialog is not None:
            print(f"  bridge diagnostic: {dialog_stop_detail(state.dialog)}", flush=True)
        else:
            print(f"  bridge diagnostic: {state.state}; inspect the held Desktop locally", flush=True)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print("  bridge diagnostic: CANNOT_READ; inspect the held Desktop locally", flush=True)
    return None


def _looks_like_semantic_model(path: Path) -> bool:
    """A resolved `byPath` target that is really an on-disk `.SemanticModel` folder.

    Requires the `.SemanticModel` suffix AND an existing `definition/` dir, so a binding pointing at a
    deleted/renamed model is rejected here and the caller then fails closed.
    """
    return path.is_dir() and path.name.endswith(".SemanticModel") and (path / "definition").is_dir()


def _resolve_pbip_binding(pbip: Path) -> tuple[str, Path | None]:
    """Resolve the `.pbip` -> report -> `definition.pbir` `byPath` binding to a model, TRI-STATE.

    Returns exactly one of:
      ("resolved", model_dir) - one binding target that is a real `.SemanticModel` (possibly OUTSIDE
                                this `.pbip`'s folder). Use it.
      ("unresolvable", None)  - a binding EXISTS but names no single real model (missing/renamed, or
                                several reports disagree). FAIL CLOSED.
      ("absent", None)        - no `byPath` anywhere; a name/count heuristic MAY now apply.

    "Absent" and "present-but-unresolvable" are deliberately DIFFERENT results: the first permits a
    heuristic, the second forbids it. Collapsing them (round-2) let a dangling or outside-the-folder
    binding fall through to `models[0]` - a wrong-project write reachable even with a single adjacent
    model. The target is resolved DIRECTLY from `byPath`, NOT intersected with the pbip folder's
    siblings, so an outside-the-folder binding still resolves and a lone decoy cannot shadow it
    (round-3 mandate, points 2-3).
    """
    try:
        pbip_data = json.loads(pbip.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        pbip_data = None
    report_dirs: list[Path] = []
    if isinstance(pbip_data, dict):
        for artifact in pbip_data.get("artifacts", []) or []:
            report = artifact.get("report") if isinstance(artifact, dict) else None
            rel = report.get("path") if isinstance(report, dict) else None
            if rel:
                report_dirs.append(pbip.parent / rel)
    if not report_dirs:
        report_dirs = [pbir.parent for pbir in pbip.parent.glob("*.Report")]

    saw_binding = False
    targets: set[Path] = set()
    for report_dir in report_dirs:
        pbir = report_dir / "definition.pbir"
        try:
            data = json.loads(pbir.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        by_path = (((data.get("datasetReference") or {}).get("byPath")) or {}).get("path")
        if not by_path:
            continue
        saw_binding = True
        target = (pbir.parent / by_path).resolve()
        if _looks_like_semantic_model(target):
            targets.add(target)
    if not saw_binding:
        return "absent", None
    if len(targets) == 1:
        return "resolved", next(iter(targets))
    return "unresolvable", None


def _cache_from_absent_binding(pbip: Path) -> Path | None:
    """Name/count heuristic for the owning model - reached ONLY when no `byPath` binding exists.

    Kept in its own function so no branch of it can precede the authoritative binding lookup in
    :func:`cache_file` (the early-return shape that failed review twice). One model -> it; otherwise
    the single sibling whose stem matches the `.pbip`; else None (ambiguous, fail closed).
    """
    models = sorted(pbip.parent.glob("*.SemanticModel"))
    if not models:
        return None
    if len(models) == 1:
        return models[0] / ".pbi" / "cache.abf"
    stem = pbip.stem.lower()
    by_name = [model for model in models if model.stem.lower() == stem]
    if len(by_name) == 1:
        return by_name[0] / ".pbi" / "cache.abf"
    return None


def cache_file(pid: int, *, instance: dict) -> Path | None:
    """Where `<Name>.SemanticModel/.pbi/cache.abf` belongs for the model this instance has open.

    Resolves the DESTINATION, not an existing file (on a first build there is no cache yet). There is
    exactly ONE resolution order, with the authoritative binding consulted FIRST and UNCONDITIONALLY -
    no early return can precede it. This failed review twice with the same shape (round-1 a stem
    heuristic ran first; round-2 a `len(models)==1` shortcut), so the binding is now tri-state
    (round-3 mandate): resolved -> use it (even OUTSIDE this folder); unresolvable -> None (fail
    closed); absent -> and only then a name/count heuristic may run.
    """
    if instance.get("pid") != pid or not instance.get("currentFilePath"):
        return None
    pbip = Path(instance["currentFilePath"])
    state, bound = _resolve_pbip_binding(pbip)
    if state == "resolved":
        return bound / ".pbi" / "cache.abf"
    if state == "unresolvable":
        return None
    return _cache_from_absent_binding(pbip)


def tmdl_tables(model_dir: Path) -> set[str]:
    """Table names declared by the TMDL on disk - the fingerprint of the model that owns the cache.

    `utf-8-sig` and the auto date-table filter both matter to the *gate*, not just to tidiness:
    Desktop writes TMDL with a BOM, and a BOM immediately followed by the declaration would make
    `^table` miss every file - leaving an empty fingerprint, which `same_model` treats as
    unverifiable and (now) REFUSES. Auto date tables are serialized into `tables/` when auto
    date/time is (or ever was) on, and they are filtered out of the live side, so leaving them here
    would make every such model look like a stranger.
    """
    definition = model_dir / "definition"
    names: set[str] = set()
    for tmdl in sorted(definition.glob("tables/*.tmdl")):
        text = tmdl.read_text(encoding="utf-8-sig", errors="replace")
        names.update(quoted or bare for quoted, bare in TABLE_DECL_RE.findall(text))
    return {name for name in names if not is_auto_date_table_name(name)}


def _live_tables(port: int) -> set[str]:
    """Table names in the model currently served on `port` (hidden included, auto date tables not)."""
    adomd_connection = _load_adomd(port)
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        return set(table_names(conn, include_hidden=True))
    finally:
        conn.Close()


def tmdl_columns_measures(model_dir: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """`(table, column)` and `(table, measure)` pairs declared by the TMDL on disk.

    The finer half of the identity fingerprint: two models can share table names but differ in their
    columns or measures, and a table-only match would confirm the wrong one (#114, round-2 blocker
    3). Each `tables/<Name>.tmdl` declares exactly one table, so its name scopes every column/measure
    in the file; auto date-table files are skipped to mirror the live-side filter.
    """
    definition = model_dir / "definition"
    columns: set[tuple[str, str]] = set()
    measures: set[tuple[str, str]] = set()
    for tmdl in sorted(definition.glob("tables/*.tmdl")):
        text = tmdl.read_text(encoding="utf-8-sig", errors="replace")
        decl = TABLE_DECL_RE.search(text)
        if not decl:
            continue
        table = decl.group(1) or decl.group(2)
        if is_auto_date_table_name(table):
            continue
        columns.update((table, quoted or bare) for quoted, bare in COLUMN_DECL_RE.findall(text))
        measures.update((table, quoted or bare) for quoted, bare in MEASURE_DECL_RE.findall(text))
    return columns, measures


def _live_columns_measures(port: int) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """`(table, column)` and `(table, measure)` pairs in the model currently served on `port`.

    Auto-generated columns (RowNumber, and the auto date/time scaffolding) are filtered inside
    `column_names`/`measure_names` so the live side compares like-for-like with the TMDL on disk.
    """
    adomd_connection = _load_adomd(port)
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        return column_names(conn), measure_names(conn)
    finally:
        conn.Close()


def _first_schema_difference(kind: str, on_disk: set[tuple[str, str]], live: set[tuple[str, str]]) -> str | None:
    """Compare two `(table, name)` sets case-insensitively; return a message if they differ, else None.

    Pure and .NET-free so the exact-match rule is unit-testable. Names are compared case-folded (the
    engine and TMDL can disagree only on case), and the message names a few offenders from each side
    so a refusal is diagnosable rather than a bare mismatch.
    """
    disk_fold = {(table.casefold(), name.casefold()) for table, name in on_disk}
    live_fold = {(table.casefold(), name.casefold()) for table, name in live}
    missing = sorted(
        f"{table}[{name}]" for table, name in on_disk if (table.casefold(), name.casefold()) not in live_fold
    )
    extra = sorted(f"{table}[{name}]" for table, name in live if (table.casefold(), name.casefold()) not in disk_fold)
    if not missing and not extra:
        return None
    return (
        f"{len(missing)} TMDL {kind}(s) absent from the connected model (e.g. {missing[:3]}), "
        f"{len(extra)} connected {kind}(s) not in the TMDL (e.g. {extra[:3]})"
    )


def _first_table_difference(on_disk: set[str], live: set[str]) -> str | None:
    """Compare table-name sets case-insensitively; return the WRONG_MODEL reason, or None if equal."""
    disk_fold = {name.casefold() for name in on_disk}
    live_fold = {name.casefold() for name in live}
    missing = sorted(name for name in on_disk if name.casefold() not in live_fold)
    extra = sorted(name for name in live if name.casefold() not in disk_fold)
    if not missing and not extra:
        return None
    return (
        f"{len(missing)} TMDL table(s) absent from the connected model (e.g. {missing[:3]}), "
        f"{len(extra)} connected table(s) not in the TMDL (e.g. {extra[:3]}) - "
        "an exact table-for-table match is required"
    )


def same_model(port: int, cache_path: Path | None) -> tuple[bool, str]:
    """Is the model served on `port` the one that owns `cache_path`? Returns (ok, message).

    The only check that can catch a wrong-instance bind. The destination path is resolved from the
    pid so it is always right; the DATA comes from the port, so a widened/mistyped port silently
    writes a sibling migration's model into this migration's cache.abf - and every other signal
    (file exists, non-empty, mtime advanced, row count) agrees with it, because they all read the
    same wrong source. Comparing the model's own tables is what breaks that self-consistency.

    **Fails CLOSED.** Whenever identity cannot be established - no model folder resolved, or no TMDL
    tables to fingerprint - this returns False, because "I could not verify" and "it is fine" are
    not the same answer, and this gate guards a write into another migration's project. It used to
    return True ("unverified") in both cases and let the write through.

    **The fingerprint is EXACT, not a subset, and reaches columns and measures.** The connected
    model's tables must equal the TMDL's table-for-table, AND its columns and measures must match too.
    A table-only check confirmed a sibling that shared table names but differed in columns or measures
    - and once `cache_file`'s binding is authoritative (round-2 blocker 3a) that sibling is reachable
    on a widened/wrong port, so tables alone are not enough (round-2 blocker 3). Auto-generated
    columns (the RowNumber index, the auto date/time scaffolding) are FILTERED, not compared, so a
    legitimate model never fails its own gate over engine-only artifacts. The previous check also only
    required the TMDL tables to be PRESENT in the engine, so a SUPERSET sibling passed; an exact match
    now refuses it.
    """
    if cache_path is None:
        return False, "identity UNVERIFIED (no model folder resolved for this pid) - refusing (fail closed)"
    model_dir = cache_path.parent.parent
    on_disk = tmdl_tables(model_dir)
    if not on_disk:
        return False, (
            f"identity UNVERIFIED (no TMDL tables under {model_dir / 'definition'}) - refusing (fail closed)"
        )
    table_diff = _first_table_difference(on_disk, _live_tables(port))
    if table_diff is not None:
        return False, f"port {port} does NOT serve {model_dir.name}: {table_diff}"
    disk_columns, disk_measures = tmdl_columns_measures(model_dir)
    live_columns, live_measures = _live_columns_measures(port)
    for kind, on_disk_set, live_set in (
        ("column", disk_columns, live_columns),
        ("measure", disk_measures, live_measures),
    ):
        difference = _first_schema_difference(kind, on_disk_set, live_set)
        if difference is not None:
            return False, f"port {port} does NOT serve {model_dir.name}: {difference} - an exact match is required"
    return True, (
        f"{model_dir.name} confirmed on port {port} "
        f"({len(on_disk)} table(s), {len(disk_columns)} column(s), {len(disk_measures)} measure(s), exact match)"
    )


def row_counts(port: int, tables: list[str] | None) -> tuple[list[tuple[str, int]], bool]:
    """Row counts per canary table - the gate of record. Returns (results, implicit).

    A refresh that "succeeded" but returns no rows is not a refresh; only data proves the source was
    reachable, the credential worked and the M was valid. With an explicit canary set (one per
    distinct source), every source is asserted. With none, the first queryable table is probed and
    ``implicit`` is True - that is a single-table probe, NOT a model-level guarantee: a static
    parameter/CSV table can return rows while a live source never loaded. Whether an all-non-zero
    result earns a MODEL-level verdict is not decided here: it also depends on whether the refresh
    covered the whole database (see :func:`_emit_data_verdict`).
    """
    adomd_connection = _load_adomd(port)
    conn = adomd_connection(f"Data Source=localhost:{port}")
    conn.Open()
    try:
        implicit = not tables
        targets = list(tables) if tables else [first_table(conn)]
        results: list[tuple[str, int]] = []
        for table in targets:
            cmd = conn.CreateCommand()
            cmd.CommandText = f"EVALUATE ROW(\"n\", COUNTROWS('{table}'))"
            reader = cmd.ExecuteReader()
            rows = 0
            while reader.Read():
                value = reader.GetValue(0)
                rows = int(value) if value is not None else 0
            reader.Close()
            results.append((table, rows))
        return results, implicit
    finally:
        conn.Close()


def _ui_root(state: Observation, pid: int, expected_start: str) -> int:
    """Require one complete READY observer snapshot and return its exact main-window handle."""
    if not state.complete or state.state != "READY" or state.process_started_at != expected_start:
        raise DesktopActionError("initial observer state is not complete READY for the held process")
    frame = state.target_window or main_frame(state.windows)
    if frame is None or not isinstance(frame.hwnd, int) or frame.hwnd <= 0:
        raise DesktopActionError("observer did not establish one actionable root window")
    if frame.pid is not None and frame.pid != str(pid):
        raise DesktopActionError("observer root belongs to another process")
    if frame.process_started_at is not None and frame.process_started_at != expected_start:
        raise DesktopActionError("observer root process identity changed")
    return frame.hwnd


def _watch_ui_identity(context: UIRefreshContext, state: Observation) -> Observation:
    """While Desktop works, judge only the held process and frame identity (#831 D2).

    Owned windows and a disabled frame are NOT judged here: Desktop's own Refresh popup is one, and a
    sign-in dialog is relayed by title instead of ending the wait.
    """
    if state.process_gone is not None:
        raise DesktopGoneError(context.pid, state.process_gone)
    if state.desktop_unready is not None:
        raise DesktopUnreadyError(context.pid, state.desktop_unready)
    if state.complete and state.windows and not any(window.hwnd == context.root_hwnd for window in state.windows):
        raise DesktopUnreadyError(context.pid, "window_identity_changed")
    frame = state.target_window or main_frame(state.windows)
    if frame is not None and frame.hwnd != context.root_hwnd:
        raise DesktopUnreadyError(context.pid, "window_identity_changed")
    if (
        frame is not None
        and frame.process_started_at is not None
        and frame.process_started_at != context.bound.identity.process_start
    ):
        raise DesktopGoneError(context.pid, "process_identity_changed")
    return state


def _require_ui_ready(context: UIRefreshContext, state: Observation) -> Observation:
    """Strict judgement: any dialog stops for a human; anything short of READY on the held root refuses."""
    raise_terminal_detection(context.pid, state)
    if state.desktop_unready is not None:
        raise DesktopUnreadyError(context.pid, state.desktop_unready)
    if not state.complete or state.state != "READY":
        raise CredentialUnknownError(context.pid, state.unknown_reason or state.reason or "observer_not_ready")
    frame = state.target_window or main_frame(state.windows)
    if frame is None or frame.hwnd != context.root_hwnd:
        raise DesktopUnreadyError(context.pid, "window_identity_changed")
    if frame.process_started_at is not None and frame.process_started_at != context.bound.identity.process_start:
        raise DesktopGoneError(context.pid, "process_identity_changed")
    return state


def _ui_observer_state(
    context: UIRefreshContext,
    *,
    watching: bool = False,
    timeout_seconds: float = 8.0,
) -> Observation:
    """One observation of the held Desktop; READY is always judged strictly.

    ``watching`` returns a non-READY snapshot after the identity checks only, so a dialog is relayed
    rather than ending the wait; otherwise anything short of READY refuses.
    """
    state = _in_flight_credential_state(
        context.pid,
        expected_process_start=context.bound.identity.process_start,
        timeout_s=max(0.01, min(8.0, timeout_seconds)),
    )
    _watch_ui_identity(context, state)
    return state if watching and state.state != "READY" else _require_ui_ready(context, state)


def _ui_read_budget(deadline: float, cap: float = 30.0) -> float:
    """Clip one whole UI-route bound read to the held deadline, never restart its budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("UI refresh operation deadline exhausted")
    return min(cap, remaining)


def _refuse_unsettled_ui(context: UIRefreshContext, state: Observation | None) -> None:
    """Judge the last watched snapshot once its wait has ended; only READY passes."""
    if state is None or state.state == "READY":
        return
    raise_terminal_detection(context.pid, state)
    if state.desktop_unready is not None:
        raise DesktopUnreadyError(context.pid, state.desktop_unready)
    raise CredentialUnknownError(context.pid, state.unknown_reason or state.reason or "observer_incomplete")


def _ui_wait_ready(context: UIRefreshContext, deadline: float) -> Observation | None:
    """Wait, inside the held deadline and at most UI_SETTLE_SECONDS, for a complete READY root.

    Identity loss still stops at once. A dialog left open at the limit is DIALOG_NEEDS_HUMAN with its
    title; ``None`` means the limit passed before any observation could be taken.
    """
    limit = min(deadline, time.monotonic() + UI_SETTLE_SECONDS)
    last: Observation | None = None
    while (remaining := limit - time.monotonic()) > 0:
        last = _ui_observer_state(context, watching=True, timeout_seconds=remaining)
        if last.state == "READY":
            return last
        time.sleep(max(0.0, min(REFRESH_CREDENTIAL_POLL_SECONDS, limit - time.monotonic())))
    _refuse_unsettled_ui(context, last)
    return None


def _ui_refresh_preflight(  # pylint: disable=too-many-return-statements,too-many-branches,too-many-statements
    pid: int, instance: dict, deadline: float, *, require_clean: bool = True
) -> tuple[dict, Observation] | int:
    """Require an exact-file bridge instance (clean unless `--save-only`) and a complete READY observer."""
    try:
        _ui_read_budget(deadline)
        # A missing/false-like value is not permission to discard changes.
        if require_clean and instance.get("hasUnsavedChanges") is not False:
            raise DesktopActionError("initial clean state unavailable")
        path = instance.get("currentFilePath")
        if not isinstance(path, str) or not path.strip() or "\x00" in path or not Path(path).is_absolute():
            raise DesktopActionError("initial absolute file binding unavailable")
        state = _credential_state(pid, timeout_s=_ui_read_budget(deadline, 8.0))
        _raise_if_blocked(pid, state)
        _ui_read_budget(deadline)
        start = state.process_started_at
        # Observer/process binding uses lossless .NET UTC CreationDate.Ticks, not a timestamp
        # conversion or a rounded display value.
        if not isinstance(start, str) or not start.isascii() or not start.isdecimal() or int(start) <= 0:
            raise DesktopActionError("process start identity unavailable")
        if state.pid != str(pid):
            raise DesktopActionError("observer PID mismatch")
        _ui_root(state, pid, start)
        return instance, state
    except DialogFoundError as exc:
        _emit_dialog_finding(exc.pid, exc.finding)
        return 3
    except (DesktopGoneError, DesktopUnreadyError) as exc:
        if isinstance(exc, DesktopGoneError):
            _emit_desktop_gone(exc.pid, exc.reason)
        else:
            _emit_desktop_unready(exc.pid, exc.reason)
        return 2
    except TimeoutError:
        print("REFRESH: TIMEOUT - UI refresh startup deadline exhausted; Refresh and Save withheld", flush=True)
        return 3
    except DesktopActionError as exc:
        print(f"REFRESH: UI_PREFLIGHT_REFUSED ({exc}; Refresh and Save withheld)", flush=True)
        return 3
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print("REFRESH: UI_PREFLIGHT_REFUSED (startup checks unavailable; Refresh and Save withheld)", flush=True)
        return 3


def _read_completion_snapshot(context: UIRefreshContext, deadline: float) -> PartitionSnapshot | None:
    """One bound partition read; ``None`` when Desktop's own popup made the reader refuse.

    bound_call correctly refuses reads while Desktop's popup is up. Retry on that closed set
    only; every other refusal keeps its prompt or binding authority.
    """
    try:
        return read_partition_snapshot(context.bound, timeout_seconds=_ui_read_budget(deadline))
    except ObservationUnavailable as exc:
        if len(exc.args) != 1 or exc.args[0] not in UI_TRANSIENT_READ_TOKENS:
            raise
        return None


def _wait_for_engine_completion(
    context: UIRefreshContext,
    baseline: PartitionSnapshot,
    action_started_ticks: int,
    deadline: float,
) -> tuple[bool, CompletionResult | None]:
    """Poll exact typed partition evidence; only identity loss or the deadline stop the wait.

    A dialog does NOT end this wait (#831 D2): the progress stream relays its title so the agent can
    ask the human. Completion authority stays with ``check_completion``.
    """
    _ui_read_budget(deadline, float("inf"))
    cancelled = threading.Event()
    outcome: dict[str, object] = {}

    def watch_ui() -> Observation:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("observer deadline expired")
        return _ui_observer_state(context, watching=True, timeout_seconds=remaining)

    def poll() -> None:
        try:
            while not cancelled.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("engine completion deadline expired")
                current = _read_completion_snapshot(context, deadline)
                if current is None:
                    cancelled.wait(min(1.0, max(0.0, deadline - time.monotonic())))
                    continue
                completed_at = time.monotonic()
                if completed_at >= deadline:
                    raise TimeoutError("engine completion result arrived after deadline")
                result = check_completion(baseline, current, action_started_ticks)
                if result.complete:
                    outcome["result"] = result
                    outcome["completed_at"] = completed_at
                    return
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            raise TimeoutError("engine completion wait was cancelled")
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            outcome["error"] = exc
            outcome["completed_at"] = time.monotonic()

    last = watch_ui()
    _ui_read_budget(deadline)
    worker = threading.Thread(target=poll, name="ui-refresh-engine-completion", daemon=True)
    worker.start()
    try:
        while worker.is_alive() and (remaining := deadline - time.monotonic()) > 0:
            worker.join(timeout=min(REFRESH_CREDENTIAL_POLL_SECONDS, remaining))
            if worker.is_alive():
                try:
                    last = watch_ui()
                except TimeoutError:
                    break
    finally:
        cancelled.set()
        worker.join(timeout=8.5)
    if not worker.is_alive():
        result = outcome.get("result")
        completed_at = outcome.get("completed_at")
        if result is not None and isinstance(completed_at, float) and completed_at < deadline:
            return True, result  # type: ignore[return-value]
        error = outcome.get("error")
        if error is not None and not isinstance(error, TimeoutError):
            raise error  # type: ignore[misc]
    _refuse_unsettled_ui(context, last)
    return False, None


# Captured at import: tests patch `time.monotonic` to drive deadlines, which must not skew progress.
_progress_clock = time.monotonic


def _open_progress_connection(context: UIRefreshContext):
    """A plain ADOMD connection pinned to the bound catalogue, separate from every `bound_call` read.

    Desktop names each catalogue with a GUID, so another engine on that port cannot answer for it;
    that pin is the identity proof. It never relaxes `bound_call`'s dialog refusal.
    """
    port = context.bound.identity.port
    connection = _load_adomd(port)(
        f"Data Source=localhost:{port};Initial Catalog={context.bound.catalogue};Connect Timeout=5"
    )
    connection.Open()
    return connection


def _process_jobs(connection) -> int:
    """Count `$SYSTEM.DISCOVER_JOBS` rows whose JOB_DESCRIPTION is `Process`; other job types are noise."""
    command = connection.CreateCommand()
    command.CommandTimeout = 5
    command.CommandText = "SELECT [JOB_DESCRIPTION] FROM $SYSTEM.DISCOVER_JOBS"
    reader = command.ExecuteReader()
    try:
        count = 0
        while reader.Read():
            count += str(reader.GetValue(0)) == "Process"
        return count
    finally:
        reader.Close()


def _dialog_field(context: UIRefreshContext) -> str:
    """The title of a Desktop-owned dialog the observer reports, `none`, or `?` when unobserved."""
    try:
        state = observe(context.pid, expected_process_start=context.bound.identity.process_start, timeout_s=8.0)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return "?"
    if state.dialog is not None:
        return f'"{state.dialog.title}"'
    return "none" if state.complete else "?"


class _Progress:  # pylint: disable=too-many-instance-attributes
    """Always-on `REFRESH_STATUS` lines for every phase; progress never decides anything.

    A failed read prints `?` and reconnects on the next poll, so no value is ever carried forward.
    """

    def __init__(self, context: UIRefreshContext, phase: str = "invoke") -> None:
        self._context = context
        self._started = self._phase_started = _progress_clock()
        self._phase = phase
        self._spent: dict[str, float] = {}
        self._connection = None
        self._printed: tuple[str, str, str] | None = None
        self._due = 0.0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._muted = False
        self._finished = False
        self._thread = threading.Thread(target=self._run, name="refresh-status", daemon=True)

    def __enter__(self) -> _Progress:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if not self._finished:
            # A failure exit still gets its timing summary; it is informational and returns nothing.
            with contextlib.suppress(Exception):
                print(self.finish(), flush=True)
        self._muted = True
        self._close()

    def phase(self, name: str) -> None:
        """Switch phase; the new phase is shown on the next tick, which runs at once."""
        with self._lock:
            now = _progress_clock()
            self._spent[self._phase] = self._spent.get(self._phase, 0.0) + now - self._phase_started
            self._phase, self._phase_started = name, now
        self._wake.set()

    def finish(self) -> str:
        """Show the `done` phase, stop streaming and return the summary line."""
        self.phase("done")
        self._close()
        self._muted = self._finished = True
        spent = self._spent
        refresh = sum(spent.get(name, 0.0) for name in ("invoke", "refreshing", "committed"))
        save = f"{spent['saving']:.0f}s" if "saving" in spent else "skipped"
        return (
            f"REFRESH_SUMMARY total={_progress_clock() - self._started:.0f}s refresh={refresh:.0f}s "
            f"canaries={spent.get('canaries', 0.0):.0f}s save={save}"
        )

    def _close(self) -> None:
        self._stop = True
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10.0)

    def _run(self) -> None:
        try:
            while not self._stop:
                self._wake.clear()
                self._tick()
                self._wake.wait(POLL_INTERVAL_SECONDS)
            self._tick()
        finally:
            self._disconnect()

    def _jobs(self) -> str:
        try:
            if self._connection is None:
                self._connection = _open_progress_connection(self._context)
            return str(_process_jobs(self._connection))
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            self._disconnect()
            return "?"

    def _disconnect(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.Close()

    def _tick(self) -> None:
        jobs, dialog = self._jobs(), _dialog_field(self._context)
        with self._lock:
            now = _progress_clock()
            shown = (self._phase, jobs, dialog)
            if self._muted or (shown == self._printed and now < self._due):
                return
            print(
                f"REFRESH_STATUS elapsed={now - self._started:.0f}s phase={shown[0]} "
                f"process_jobs={jobs} dialog={dialog}",
                flush=True,
            )
            self._printed, self._due = shown, now + REFRESH_HEARTBEAT_SECONDS


def _ui_stop(exc: Exception, stage: str | None, other: str, code: int) -> int:
    """Print the one refusal line for an exception raised inside a UI step; return its exit code."""
    if isinstance(exc, DialogFoundError):
        _emit_dialog_finding(exc.pid, exc.finding)
        return 3
    if isinstance(exc, CredentialUnknownError):
        if stage is None:
            _emit_credential_unknown(exc.pid, exc.reason)
        else:
            _emit_ui_observation_unknown(exc.pid, stage)
        return 3
    if isinstance(exc, DesktopGoneError):
        _emit_desktop_gone(exc.pid, exc.reason)
        return 2
    if isinstance(exc, DesktopUnreadyError):
        _emit_desktop_unready(exc.pid, exc.reason)
        return 2
    print(f"REFRESH: {other}")
    return code


def _model_lock(cache: Path):
    """The per-model lock beside the cache; a model without a `.pbi` folder yet has nothing to guard."""
    if not cache.parent.exists():
        return contextlib.nullcontext()
    return model_lock(cache.with_name(cache.name + ".lock"), timeout=PERSIST_LOCK_TIMEOUT_SECONDS)


def _invoke(context: UIRefreshContext, name: Literal["Refresh", "Save"], deadline: float) -> ActionResult:
    """One named Desktop action; TimeoutError means it was NOT invoked because the deadline had passed."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"UI {name} was not invoked after the operation deadline")
    return invoke_desktop_action(
        name,
        pid=context.pid,
        expected_process_start=context.bound.identity.process_start,
        root_hwnd=context.root_hwnd,
        timeout_seconds=min(ACTION_CHILD_TIMEOUT_SECONDS, remaining),
    )


def _no_read(_connection) -> None:
    return None


def _before_save(context: UIRefreshContext, deadline: float) -> None:
    """Binding only: READY with no dialog on the held root, then the held identity and catalogue."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("UI Save was not invoked after the operation deadline")
    _ui_observer_state(context, timeout_seconds=remaining)
    bound_call(context.bound, _no_read, timeout_seconds=_ui_read_budget(deadline))
    _ui_read_budget(deadline)


def _after_save(context: UIRefreshContext, cache: Path, deadline: float) -> bool:
    """After a Save Invoke: READY, the held binding, a clean bridge and an unchanged file binding.

    False means Desktop did not settle inside the deadline; every other failure raises.
    """
    if _ui_wait_ready(context, deadline) is None:
        return False
    bound_call(context.bound, _no_read, timeout_seconds=_ui_read_budget(deadline))
    payload = _ui_bridge_status(_ui_bridge_command(), context.pid, deadline)
    _ui_read_budget(deadline)
    instance, _delay = _bridge_readiness_result(payload, context.pid)
    if instance is None or instance.get("hasUnsavedChanges") is not False:
        raise DesktopActionError("post-Save bridge clean state unestablished")
    resolved_cache = cache_file(context.pid, instance=instance)
    if resolved_cache is None or resolved_cache.resolve() != cache.resolve():
        raise DesktopActionError("post-Save file binding changed")
    _ui_read_budget(deadline)
    return True


def _cli_save(  # pylint: disable=too-many-return-statements
    context: UIRefreshContext, cache: Path, deadline: float, *, model_gate: bool
) -> int | None:
    """The CLI's guarded Desktop Save; returns the refusal exit code, or None once Save is verified."""
    try:
        _before_save(context, deadline)
        if model_gate and not _identity_gate(context.port, cache):
            return 2
        action = _invoke(context, "Save", deadline)
    except TimeoutError:
        print("REFRESH: TIMEOUT - UI Save was not invoked after the operation deadline")
        return 3
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return _ui_stop(exc, None, f"NOT_PERSISTED (UI Save was not established: {type(exc).__name__})", 1)
    if not action.invoked:
        print(f"REFRESH: NOT_PERSISTED (UI Save refused: {action.reason})")
        return 1
    print("  UI Save: one named Invoke returned; persistence remains unconfirmed")
    try:
        if not _after_save(context, cache, deadline):
            print("REFRESH: NOT_PERSISTED (Desktop did not return to READY within the settle window after Save)")
            return 1
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        reason = f"NOT_PERSISTED (post-Save verification unavailable: {type(exc).__name__})"
        return _ui_stop(exc, "Post-Save", reason, 1)
    return None


def _ui_refresh_and_save(  # pylint: disable=too-many-arguments
    pid: int,
    port: int,
    cache: Path | None,
    args: argparse.Namespace,
    initial_state: Observation,
    *,
    deadline: float,
) -> int:
    """Bind the held Desktop, take the per-model lock, then run the refresh or `--save-only` route."""
    if cache is None:
        print("REFRESH: ERROR no model cache path resolved; refusing the UI route")
        return 2
    if not initial_state.complete or initial_state.state != "READY":
        print("REFRESH: UI_PREFLIGHT_REFUSED (the initial observer snapshot was not complete READY)")
        return 3
    try:
        root_hwnd = _ui_root(initial_state, pid, initial_state.process_started_at or "")
        frame = initial_state.target_window or main_frame(initial_state.windows)
        print(f"  frame  : observed {frame.width}x{frame.height}")
        _ui_read_budget(deadline)
        bound = bind_desktop(pid, supplied_port=port)
        _ui_read_budget(deadline)
        if bound.identity.process_start != initial_state.process_started_at:
            raise DesktopActionError("bound process identity differs from the initial observer")
        context = UIRefreshContext(pid=pid, port=port, bound=bound, root_hwnd=root_hwnd)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"REFRESH: UI_PREFLIGHT_REFUSED ({type(exc).__name__}: {exc})")
        return 2
    route = _ui_save_only_locked if getattr(args, "save_only", False) else _ui_refresh_locked
    try:
        with _model_lock(cache):
            return route(context, cache, args, deadline=deadline)
    except ModelLockTimeout as exc:
        print(f"REFRESH: NOT_PERSISTED (model lock unavailable: {exc})")
        return 1
    except LockUnavailable as exc:
        # Raised only while entering the lock, so no Desktop action has been invoked.
        print(f"REFRESH: UI_PREFLIGHT_REFUSED (model lock not writable: {exc})")
        return 2


def _ui_refresh_locked(
    context: UIRefreshContext,
    cache: Path,
    args: argparse.Namespace,
    *,
    deadline: float | None = None,
) -> int:
    """The default route: Desktop's own Refresh, typed completion, canaries, then the guarded Save."""
    deadline = time.monotonic() + args.refresh_absolute_timeout_seconds if deadline is None else deadline
    with _Progress(context) as progress:
        return _ui_refresh_steps(context, cache, args, deadline, progress)


def _ui_refresh_steps(  # pylint: disable=too-many-return-statements,too-many-branches,too-many-locals,too-many-statements
    context: UIRefreshContext,
    cache: Path,
    args: argparse.Namespace,
    deadline: float,
    progress: _Progress,
) -> int:
    """The default route's steps, streaming progress; each refusal prints one `REFRESH:` line."""
    before_stamp = cache.stat().st_mtime if cache.exists() else 0.0
    try:
        _ui_observer_state(context, timeout_seconds=_ui_read_budget(deadline, 8.0))
        # No bound no-op read here: the baseline read below repeats its identity, catalogue and
        # observer checks after the model gate, so a separate one only added observations (#793).
        _ui_read_budget(deadline)
        if not _identity_gate(context.port, cache):
            return 2
        baseline = read_partition_snapshot(context.bound, timeout_seconds=_ui_read_budget(deadline))
        _ui_read_budget(deadline)
        scope = getattr(args, "automatic_source_scope", None)
        if scope is not None:
            check_automatic_source_scope(
                context.bound, scope, _canary_tables(args), timeout_seconds=_ui_read_budget(deadline)
            )
            _ui_read_budget(deadline)
        # After the scope check: admitting never-processed State 3 must never let an out-of-scope model reach Refresh.
        check_baseline(baseline)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"REFRESH: UI_PREFLIGHT_REFUSED ({type(exc).__name__}: {exc})")
        return 2

    try:
        action = _invoke(context, "Refresh", deadline)
    except TimeoutError:
        print("REFRESH: TIMEOUT - UI refresh deadline expired before action")
        return 3
    except DesktopActionError as exc:
        print(f"REFRESH: UI_ACTION_FAILED ({exc})")
        return 2
    if not action.invoked or action.action_started_ticks is None:
        print(f"REFRESH: UI_ACTION_REFUSED ({action.reason})")
        if action.reason == "named_control_missing":
            print("  hint   : a small frame collapses the Home ribbon; maximize the Desktop window")
        return 2
    progress.phase("refreshing")
    print("  UI Refresh: one named Invoke returned; engine completion remains unestablished")

    try:
        complete, completion = _wait_for_engine_completion(context, baseline, action.action_started_ticks, deadline)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return _ui_stop(exc, "Completion", f"COMPLETION_UNKNOWN ({type(exc).__name__}: {exc})", 3)
    if not complete or completion is None:
        print("REFRESH: TIMEOUT - engine completion not established before the bounded deadline")
        return 3
    progress.phase("committed")
    print(f"  engine completion: {completion.reason}")

    try:
        if _ui_wait_ready(context, deadline) is None:
            print("REFRESH: COMPLETION_UNKNOWN (Desktop observer did not return to READY within the settle window)")
            return 3
        progress.phase("canaries")
        canaries = _canary_tables(args)
        implicit = not canaries
        if deadline - time.monotonic() <= 0:
            print("REFRESH: TIMEOUT - data checks did not begin before the operation deadline")
            return 3
        targets = canaries or [bound_call(context.bound, first_table, timeout_seconds=_ui_read_budget(deadline))]
        observations = probe_observations(context.bound, targets, timeout_seconds=_ui_read_budget(deadline))
        if time.monotonic() >= deadline:
            print("REFRESH: TIMEOUT - data-check result arrived after the operation deadline")
            return 3
        results = [(item.table, item.returned_rows) for item in observations]
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return _ui_stop(exc, "Completion", f"DATA_CHECK_FAILED ({type(exc).__name__}: {exc})", 2)

    if not args.no_save and all(rows > 0 for _, rows in results):
        progress.phase("saving")
        refused = _cli_save(context, cache, deadline, model_gate=True)
        if refused is not None:
            return refused
    print(progress.finish())
    return _emit_data_verdict(cache, before_stamp, args, results, implicit)


def _ui_save_only_locked(
    context: UIRefreshContext,
    cache: Path,
    args: argparse.Namespace,
    *,
    deadline: float,
) -> int:
    """`--save-only`: canaries on what is loaded now, then Desktop's guarded Save, then the model check.

    Never Refresh. Everything that can refuse before Save does: the canaries read the in-memory model,
    and `_cli_save` rechecks the binding (PID and start, its sole msmdsrv child and port, the bound
    catalogue, the held root window). That binding is the pre-Save identity check: Save is Desktop's
    own Save on the held window, so it writes that Desktop's model to its own file, never a sibling's.
    `same_model` cannot run before Save, because MCP DAX edits (measures, calculated columns or tables)
    legitimately differ from the TMDL on disk; it runs after Save against the TMDL Save just wrote.
    """
    before_stamp = cache.stat().st_mtime if cache.exists() else 0.0
    print("  disclosure: Save persists everything loaded in this Desktop, including edits not made by this run")
    with _Progress(context, "canaries") as progress:
        try:
            observations = probe_observations(
                context.bound, list(args.canaries), timeout_seconds=_ui_read_budget(deadline)
            )
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return _ui_stop(exc, "Pre-Save", f"DATA_CHECK_FAILED ({type(exc).__name__}: {exc})", 2)
        results = [(item.table, item.returned_rows) for item in observations]
        if all(rows > 0 for _, rows in results):
            progress.phase("saving")
            refused = _cli_save(context, cache, deadline, model_gate=False)
            if refused is not None:
                return refused
            if not _identity_gate(context.port, cache):
                return 2
        print(progress.finish())
    return _emit_data_verdict(cache, before_stamp, args, results, False)


def _api_refusal(exc: Exception) -> Exception:
    """Map a programmatic-call failure onto the closed observation vocabulary (no new codes)."""
    if isinstance(exc, (ObservationUnavailable, ModelLockTimeout, SourceScopeError)):
        return exc
    for kind, code in (
        (DialogFoundError, DIALOG_NEEDS_HUMAN),
        (DesktopGoneError, "DESKTOP_GONE"),
        (DesktopUnreadyError, "DESKTOP_UNREADY"),
        (TimeoutError, "TIMEOUT"),
        (CredentialUnknownError, "CREDENTIAL_UNKNOWN"),
    ):
        if isinstance(exc, kind):
            return ObservationUnavailable(code)
    return ObservationUnavailable("TOOL_UNAVAILABLE")


def _api_context(bound: BoundDesktop, cache: Path, deadline: float, *, refresh: bool) -> UIRefreshContext:
    """READY with no dialog, a ready bridge for this PID that owns `cache`, and (before a refresh) clean."""
    pid, start = bound.identity.pid, bound.identity.process_start
    state = _credential_state(pid, expected_process_start=start, timeout_s=_ui_read_budget(deadline, 8.0))
    raise_terminal_detection(pid, state)
    if state.desktop_unready is not None:
        raise DesktopUnreadyError(pid, state.desktop_unready)
    if not state.complete or state.state != "READY":
        raise CredentialUnknownError(pid, state.unknown_reason or state.reason or "observer_not_ready")
    context = UIRefreshContext(pid, bound.identity.port, bound, _ui_root(state, pid, start))
    try:
        instance = _wait_ui_bridge(pid, min(deadline, time.monotonic() + 60.0))
    except TimeoutError:
        if time.monotonic() >= deadline:
            raise
        raise DesktopActionError("bridge readiness not established") from None
    if refresh and instance.get("hasUnsavedChanges") is not False:
        raise DesktopUnreadyError(pid, "unsaved_changes")
    resolved = cache_file(pid, instance=instance)
    if resolved is None or resolved.resolve() != cache.resolve():
        raise ObservationUnavailable("IDENTITY_UNESTABLISHED")
    return context


def ui_refresh_observed(  # pylint: disable=too-many-locals
    bound: BoundDesktop,
    cache: Path,
    *,
    deadline_seconds: float = REFRESH_ABSOLUTE_TIMEOUT_SECONDS,
    automatic_source_scope: str | Path | None = None,
    canaries: tuple[str, ...] = (),
) -> RefreshObservation:
    """Refresh every source of the bound model through Desktop's own Refresh, for a programmatic caller.

    Pinned contract (the capture caller codes against it):

    - ``bound`` comes from ``bind_desktop`` for the Desktop that opened the model owning ``cache``
      (``<model>/.pbi/cache.abf``). Identity, catalogue and file binding are rechecked before Invoke.
    - Holds the per-model lock for the whole call; a concurrent holder raises ``ModelLockTimeout``
      before any Desktop action.
    - With ``automatic_source_scope`` the trusted expected-source file and ``canaries`` are checked
      first, as the CLI's ``--automatic-source-scope`` does; without it no scope check runs.
    - Streams flushed ``REFRESH_STATUS`` lines to stdout; progress never decides the result.
    - Returns ``RefreshObservation(bound.catalogue, "full", "database", (), bound.identity)`` only when
      the engine reports every partition of this model refreshed within ``deadline_seconds``.
    - Every refusal raises ``ObservationUnavailable(code)`` with a code from the observation
      vocabulary (for example ``DIALOG_NEEDS_HUMAN``, ``DESKTOP_GONE``, ``PID_REUSED``, ``TIMEOUT``,
      ``TOOL_UNAVAILABLE``). A dialog is a human stop and is never clicked. A scope mismatch raises
      ``SourceScopeError``.
    - Never runs canaries, never Saves and never prints a verdict: the caller owns those steps.
    """
    deadline = time.monotonic() + deadline_seconds
    cache = Path(cache)
    try:
        with _model_lock(cache):
            context = _api_context(bound, cache, deadline, refresh=True)
            with _Progress(context) as progress:
                if not same_model(context.port, cache)[0]:
                    raise ObservationUnavailable("IDENTITY_UNESTABLISHED")
                if automatic_source_scope is not None:
                    expected = load_expected_sources(automatic_source_scope)
                    partitions, expressions = bound_call(bound, _scope_reads, timeout_seconds=_ui_read_budget(deadline))
                    check_source_scope(partitions, expressions, expected, list(canaries))
                baseline = read_partition_snapshot(bound, timeout_seconds=_ui_read_budget(deadline))
                check_baseline(baseline)
                action = _invoke(context, "Refresh", deadline)
                if not action.invoked or action.action_started_ticks is None:
                    if action.reason == "named_control_missing":
                        raise DesktopUnreadyError(context.pid, action.reason)
                    raise DesktopActionError(f"Refresh refused: {action.reason}")
                progress.phase("refreshing")
                complete, _completion = _wait_for_engine_completion(
                    context, baseline, action.action_started_ticks, deadline
                )
                if not complete:
                    raise TimeoutError("engine completion not established before the deadline")
                progress.phase("committed")
                if _ui_wait_ready(context, deadline) is None:
                    raise TimeoutError("Desktop did not settle before the deadline")
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        raise _api_refusal(exc) from None
    return RefreshObservation(bound.catalogue, "full", "database", (), bound.identity)


def ui_save_observed(
    bound: BoundDesktop,
    cache: Path,
    *,
    deadline_seconds: float = REFRESH_ABSOLUTE_TIMEOUT_SECONDS,
) -> SaveObservation:
    """Persist the bound model through Desktop's own guarded Save and read its cache back once.

    Pinned contract (the capture caller codes against it):

    - Same binding precondition and per-model lock as ``ui_refresh_observed``.
    - Refuses before Save unless the bound Desktop is READY with no dialog and still serves this
      model's catalogue from the file that owns ``cache``.
    - After Save it waits for READY, rechecks the binding, requires the bridge's ``hasUnsavedChanges``
      to be exactly ``False`` and Desktop's cache path to equal ``cache``, then returns a
      ``SaveObservation`` with the cache's sha256 and byte size. Cold-reopen durability stays
      ``UNESTABLISHED``.
    - Refusals raise ``ObservationUnavailable(code)`` as in ``ui_refresh_observed``; a concurrent
      holder raises ``ModelLockTimeout``. Never prints a verdict.
    """
    deadline = time.monotonic() + deadline_seconds
    cache = Path(cache)
    try:
        with _model_lock(cache):
            context = _api_context(bound, cache, deadline, refresh=False)
            with _Progress(context, "saving"):
                _before_save(context, deadline)
                action = _invoke(context, "Save", deadline)
                if not action.invoked:
                    raise DesktopActionError(f"Save refused: {action.reason}")
                if not _after_save(context, cache, deadline):
                    raise TimeoutError("Desktop did not settle after Save before the deadline")
            data = cache.read_bytes()
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        raise _api_refusal(exc) from None
    return SaveObservation(bound.catalogue, SavedCache(hashlib.sha256(data).hexdigest(), len(data)), bound.identity)


def _identity_gate(port: int, cache_path: Path | None) -> bool:
    """Print and enforce `same_model`. False means: do not refresh, query or persist this instance."""
    try:
        ok, message = same_model(port, cache_path)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        print(f"REFRESH: ERROR could not read the model on port {port}: {type(exc).__name__}: {exc}")
        return False
    print(f"  model  : {message}")
    if not ok:
        print("REFRESH: WRONG_MODEL (the connected model does not match the TMDL that owns this cache)")
    return ok


def _build_arg_parser() -> argparse.ArgumentParser:
    """The CLI parser, built in one place so a test can assert the documented default matches it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--pid",
        type=int,
        required=True,
        help="Power BI Desktop process id (`powerbi-desktop status` maps pid -> open file)",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Local AS port. Derived from --pid; if given it MUST equal the pid-discovered port "
        "(a mismatch aborts - it cannot silently point a read or Save at another instance)",
    )
    parser.add_argument(
        "--canaries",
        nargs="*",
        help="Tables to VERIFY after the refresh, one per distinct live source. A refresh whose "
        "canaries all return rows earns the model-level DATA_OK; with none, the first queryable table "
        "is probed and the verdict is downgraded to name that single table",
    )
    parser.add_argument("--save", action="store_true", help="Accepted no-op: Save is the default")
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Prevent Save, NOT source execution: the refresh still runs current M/native/custom SQL and "
        "may incur source-side costs; the refreshed data stays in memory only",
    )
    parser.add_argument("--verify-only", action="store_true", help="Skip Refresh and Save; just report the state")
    parser.add_argument(
        "--save-only",
        action="store_true",
        help="After an MCP Calculate of MCP-made edits: canaries on what is loaded now, then Desktop's guarded "
        "Save, then the model check. Never refreshes; requires --canaries; earns SAVE_OK",
    )
    parser.add_argument(
        "--ui-refresh",
        action="store_true",
        help="Accepted for compatibility; UI refresh is the default. WARNING: the default refresh executes "
        "ALL current M, including native/custom SQL, with possible source-side cost; --no-save prevents "
        "Save, NOT source execution",
    )
    parser.add_argument(
        "--automatic-source-scope",
        nargs="?",
        const="",
        metavar="EXPECTED_SOURCES_JSON",
        help="Automatic callers (#780): before any Refresh, refuse unless every partition is an exact "
        "engine connector read (no custom SQL, files or code) whose identity equals this file. "
        "Requires --canaries",
    )
    parser.add_argument(
        "--refresh-absolute-timeout-seconds",
        type=float,
        default=REFRESH_ABSOLUTE_TIMEOUT_SECONDS,
        help=f"Deadline for the whole operation (default: {REFRESH_ABSOLUTE_TIMEOUT_SECONDS:.0f})",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> bool:
    """Reject invalid combinations before process discovery or any Desktop contact."""
    scope = args.automatic_source_scope
    if not 0 < args.pid < 2**32:
        print("REFRESH: ERROR --pid must be a valid process id")
        return False
    if scope is not None and not (scope and args.canaries):
        print("REFRESH: ERROR --automatic-source-scope requires a file value and --canaries")
        return False
    if args.verify_only and scope is not None:
        print("REFRESH: ERROR --verify-only cannot be combined with --automatic-source-scope")
        return False
    if args.save_only:
        clashes = [
            flag
            for flag, given in (
                ("--no-save", args.no_save),
                ("--automatic-source-scope", scope is not None),
                ("--verify-only", args.verify_only),
            )
            if given
        ]
        if clashes:
            print(f"REFRESH: ERROR --save-only cannot be combined with {', '.join(clashes)}")
            return False
        if not args.canaries:
            print("REFRESH: ERROR --save-only requires --canaries")
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: Desktop's own Refresh and guarded Save by default, and proof the data is there."""
    args = _build_arg_parser().parse_args(argv)
    if not _validate_args(args):
        return 2
    return _run(args, args.pid, require_clean=not args.save_only)


def _run(  # pylint: disable=too-many-return-statements,too-many-locals,too-many-branches
    args: argparse.Namespace, pid: int, *, require_clean: bool
) -> int:
    """Refresh (or `--save-only`/`--verify-only`) the Desktop `pid`; `require_clean` refuses unsaved changes."""
    entry = time.monotonic()
    deadline = entry + args.refresh_absolute_timeout_seconds
    startup_deadline = min(entry + 60.0, deadline)

    instance = _ui_bridge_preflight(pid, startup_deadline)
    if instance is None:
        return 3
    if args.verify_only:
        state = _credential_state(pid)
    else:
        preflight = _ui_refresh_preflight(pid, instance, startup_deadline, require_clean=require_clean)
        if isinstance(preflight, int):
            return preflight
        instance, state = preflight
    if state.dialog is not None:
        _emit_dialog_finding(pid, state.dialog)
        return 3
    if state.process_gone is not None:
        _emit_desktop_gone(pid, state.process_gone)
        return 2
    if state.desktop_unready is not None:
        _emit_desktop_unready(pid, state.desktop_unready)
        return 2
    if state.unknown_reason:
        print(f"  credential-check: UNKNOWN ({state.unknown_reason})")
    cache = cache_file(pid, instance=instance)

    # The port is DERIVED from the pid. A stray --port must never bypass that: the destination cache is
    # resolved from the pid, so a --port pointing at another instance would read a sibling's model.
    discovered = discover_port(pid)
    if args.port is not None and args.port != discovered:
        print(
            f"REFRESH: ERROR --port {args.port} does not match the port {discovered} discovered from "
            f"pid {pid} - refusing to read or write across instances (drop --port; it is derived from --pid)"
        )
        return 2
    port = discovered
    try:
        _load_adomd(port)
    except AdomdUnavailable as exc:
        print(f"REFRESH: ERROR ADOMD_UNAVAILABLE {exc.detail}")
        return 2

    if args.verify_only:
        before_stamp = cache.stat().st_mtime if cache and cache.exists() else 0.0
        if not _identity_gate(port, cache):
            return 2
        results, implicit = row_counts(port, _canary_tables(args))
        return _emit_data_verdict(cache, before_stamp, args, results, implicit)
    # Gate on identity before Refresh: refreshing a sibling's model is a self-consistent false positive.
    # `--save-only` checks the model only after Save, because MCP edits legitimately differ from disk;
    # its canaries and the PID/port/catalogue binding still refuse before Save.
    if not args.save_only and not _identity_gate(port, cache):
        return 2
    try:
        _ui_read_budget(deadline)
    except TimeoutError:
        print("REFRESH: TIMEOUT - UI refresh deadline exhausted during CLI preflight; no action invoked")
        return 3
    return _ui_refresh_and_save(pid, port, cache, args, state, deadline=deadline)


if __name__ == "__main__":
    sys.exit(main())
