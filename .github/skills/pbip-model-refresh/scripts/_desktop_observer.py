"""purpose: read-only, exact-identity structural observations of one Power BI Desktop process.
usage:   import observe, watch from the portable skill; callers retain operation/proof authority.
internal: true
internal-reason: Portable import authority; existing query/refresh entry points remain public.

Python reads native window structure in-process through ctypes; no child process is started.
Any blocking window is a stop for a human (#831). Its title is read only to NAME it for that human:
it is untrusted, sanitized, never compared, matched or classified, and never persisted.
These snapshots are not an atomic UI transaction.
READY is observation completeness, never source reachability, row evidence or a gate clear.
"""

from __future__ import annotations

import ctypes
import functools
import json
import math
import os
import re
import threading
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

# Sibling module, resolved once the caller puts this scripts/ dir on sys.path (probe, refresh, and the
# test conftest all do). Reused for issue #158's zero-window liveness split: its bias errs toward
# "alive" on any ambiguity, so an uncertain Desktop never reads as a false crash.
from _lock import _process_alive

POLL_INTERVAL_SECONDS = 5.0
DESKTOP_MAIN_CLASS_PREFIX = "WindowsForms10.Window.8"
DIALOG_NEEDS_HUMAN = "DIALOG_NEEDS_HUMAN"

# `GetWindow(hwnd, GW_OWNER)`. The owner of a top-level window - the window a modal disables.
GW_OWNER = 4
# .NET ticks (100 ns) at 1601-01-01 UTC, the FILETIME epoch.
TICKS_AT_FILETIME_EPOCH = 504911232000000000
PROCESS_QUERY_LIMITED_INFORMATION_SYNCHRONIZE = 0x101000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
ERROR_INVALID_PARAMETER = 87
TITLE_BUFFER_CHARS = 256
TITLE_MAX_CHARS = 80
_TITLE_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


@dataclass(frozen=True)
class DesktopWindow:
    """Visible exact-PID window structure. ``title`` is only ever a display name for a human."""

    # pylint: disable=too-many-instance-attributes
    title: str
    class_name: str
    width: int
    height: int
    minimized: bool = False
    hwnd: int = 0
    owner_hwnd: int = 0
    owner_enabled: bool | None = None
    complete: bool = True
    reason: str = "complete"
    pid: str | None = None
    process_started_at: str | None = None
    enabled: bool = True
    owner_same_pid: bool | None = None


@dataclass(frozen=True)
class DialogFinding:
    """A window that blocks Desktop, plus its sanitized title for the human who must act."""

    window: DesktopWindow
    title: str = ""

    def __post_init__(self) -> None:
        # The one place a title is sanitized: every printer reads this already-safe field.
        object.__setattr__(self, "title", sanitize_title(self.title))


@dataclass(frozen=True)
class Observation:
    """Immutable observation; transport success and READY establish no operation outcome."""

    # These are one immutable observation plus its private facts, not independent state owners.
    # pylint: disable=too-many-instance-attributes

    dialog: DialogFinding | None = None
    unknown_reason: str | None = None
    desktop_unready: str | None = None
    process_gone: str | None = None
    windows: tuple[DesktopWindow, ...] = ()
    pid: str | None = None
    process_started_at: str | None = None
    complete: bool = True
    reason: str = "complete"
    operation_id: str | None = None
    observation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    elapsed_ms: float = 0.0
    findings: tuple[DialogFinding, ...] = ()
    target_window: DesktopWindow | None = None

    @property
    def state(self) -> str:
        """Closed observer vocabulary, folded in safety precedence order."""
        if self.process_gone is not None:
            return "PROCESS_GONE"
        if self.dialog is not None:
            return "HUMAN_ACTION_REQUIRED"
        if self.desktop_unready is not None:
            return "NO_WINDOW"
        if self.unknown_reason is not None or not self.complete:
            return "UNKNOWN"
        return "READY"

    def to_json(self) -> str:
        """Serialize only closed metadata; window titles never cross this structured boundary."""
        window = self.dialog.window if self.dialog else self.target_window
        raw = json.dumps(
            {
                "schema": "desktop.observation.v2",
                "observation_id": self.observation_id,
                "operation_id": self.operation_id,
                "pid": self.pid,
                "process_started_at": self.process_started_at,
                "hwnd": _handle_string(window.hwnd) if window else None,
                "owner_hwnd": _handle_string(window.owner_hwnd) if window else None,
                "observed_at": self.observed_at,
                "elapsed_ms": self.elapsed_ms,
                "complete": self.complete,
                "state": self.state,
                "reason": self.reason,
                "findings": [
                    {
                        "hwnd": _handle_string(finding.window.hwnd),
                        "owner_hwnd": _handle_string(finding.window.owner_hwnd),
                        "state": "HUMAN_ACTION_REQUIRED",
                        "reason": "dialog_open",
                        "complete": finding.window.complete,
                    }
                    for finding in self.findings
                ],
                "cannot_prove": ["source_reachability", "credential_validity", "data_correctness"],
            },
            allow_nan=False,
        )
        observation_token(raw)
        return raw


_LEGACY_TOKENS = {
    "READY": None,
    "HUMAN_ACTION_REQUIRED": DIALOG_NEEDS_HUMAN,
    "NO_WINDOW": "DESKTOP_UNREADY",
    "UNKNOWN": "CREDENTIAL_UNKNOWN",
    "PROCESS_GONE": "DESKTOP_GONE",
}
_TERMINAL_STATES = frozenset({"HUMAN_ACTION_REQUIRED", "PROCESS_GONE"})
_REASONS = frozenset(
    {
        "complete",
        "dialog_open",
        "ambiguous",
        "minimized",
        "timeout",
        "target_changed",
        "backend_failed",
        "no_window",
        "process_gone",
        "unsupported",
        "invalid_payload",
    }
)
_OBSERVATION_KEYS = frozenset(
    {
        "schema",
        "observation_id",
        "operation_id",
        "pid",
        "process_started_at",
        "hwnd",
        "owner_hwnd",
        "observed_at",
        "elapsed_ms",
        "complete",
        "state",
        "reason",
        "findings",
        "cannot_prove",
    }
)
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?(?:Z|\+00:00)")


def legacy_token(observation: Observation) -> str | None:
    """The sole trusted observer-to-operation vocabulary adapter."""
    return _LEGACY_TOKENS[observation.state]


def measurement_refusal(code: str) -> str:
    """Explicit adaptation to unchanged receipt v2/v3 refusal enums; unknown codes reject."""
    if code in {"DESKTOP_GONE", "REFRESH_IN_PROGRESS"}:
        return "TOOL_UNAVAILABLE"
    if code in {
        "CREDENTIAL_MISSING",
        "CREDENTIAL_UNKNOWN",
        "DESKTOP_UNREADY",
        "DIALOG_NEEDS_HUMAN",
        "DIALOG_UNREADABLE",
        "DIALOG_UNRECOGNIZED",
        "IDENTITY_UNESTABLISHED",
        "WRONG_PID_PORT",
        "PID_REUSED",
        "CATALOGUE_UNESTABLISHED",
        "CATALOGUE_CHANGED",
        "CANARIES_REQUIRED",
        "TIMEOUT",
        "TOOL_UNAVAILABLE",
        "MODEL_LOCK_TIMEOUT",
    }:
        return code
    raise ValueError("unknown observation refusal")


def observation_token(raw: str) -> str | None:
    """Validate a public event before adapting it; malformed/unknown evidence is never clean."""
    # Closed protocol fields are checked explicitly, rather than delegated to a second schema owner.
    # pylint: disable=too-many-branches,unidiomatic-typecheck
    if len(raw.encode("utf-8")) > 131072:
        raise ValueError("observation size")
    payload = json.loads(raw, object_pairs_hook=_unique_object)
    if (
        not isinstance(payload, dict)
        or set(payload) != _OBSERVATION_KEYS
        or payload["schema"] != "desktop.observation.v2"
    ):
        raise ValueError("observation shape")
    for key in ("observation_id", "operation_id"):
        if key == "operation_id" and payload[key] is None:
            continue
        if not isinstance(payload[key], str) or str(uuid.UUID(payload[key])) != payload[key]:
            raise ValueError("observation id")
    if not _decimal_identity(payload["pid"]) or not 0 < int(payload["pid"]) < 2**32:
        raise ValueError("observation pid")
    if type(payload["complete"]) is not bool or payload["state"] not in _LEGACY_TOKENS:
        raise ValueError("observation state")
    if payload["process_started_at"] is not None and not _decimal_identity(payload["process_started_at"]):
        raise ValueError("observation start")
    if payload["complete"] and payload["process_started_at"] is None:
        raise ValueError("missing process start")
    if payload["state"] == "READY" and not payload["complete"]:
        raise ValueError("incomplete ready")
    if payload["reason"] not in _REASONS:
        raise ValueError("observation reason")
    _handle_value(payload["hwnd"])
    _handle_value(payload["owner_hwnd"])
    elapsed = payload["elapsed_ms"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (float, int)) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("observation elapsed")
    if not isinstance(payload["observed_at"], str) or not _TIMESTAMP.fullmatch(payload["observed_at"]):
        raise ValueError("observation timestamp")
    datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
    findings = payload["findings"]
    if not isinstance(findings, list) or len(findings) > 4000:
        raise ValueError("observation findings")
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"hwnd", "owner_hwnd", "state", "reason", "complete"}:
            raise ValueError("observation finding")
        _handle_value(finding["hwnd"], nullable=False)
        _handle_value(finding["owner_hwnd"])
        if (
            finding["state"] not in _LEGACY_TOKENS
            or finding["reason"] not in _REASONS
            or type(finding["complete"]) is not bool
        ):
            raise ValueError("observation finding enum")
    if payload["complete"] and any(not finding["complete"] for finding in findings):
        raise ValueError("observation completeness")
    if payload["state"] == "READY" and (findings or payload["reason"] != "complete"):
        raise ValueError("unexplained ready")
    if payload["cannot_prove"] != ["source_reachability", "credential_validity", "data_correctness"]:
        raise ValueError("observation authority")
    return _LEGACY_TOKENS[payload["state"]]


def emit_observation(observation: Observation, *, stream=None) -> None:
    """Emit validated closed metadata on a diagnostic channel, never operation success."""
    raw = observation.to_json()
    observation_token(raw)
    print("DESKTOP_OBSERVATION " + raw, file=stream or sys.stderr, flush=True)


def observation_refusal_events(events: Iterable[str], *, terminal_only: bool = False) -> str | None:
    """Fold validated events with the same first-terminal/retained-uncertainty lattice as watch."""
    latched = None
    for raw in events:
        try:
            token = observation_token(raw)
            terminal = json.loads(raw)["state"] in _TERMINAL_STATES
        except (ValueError, TypeError, KeyError):
            token, terminal = "CREDENTIAL_UNKNOWN", False
        if terminal:
            return token
        if token is not None and latched is None:
            latched = token
    return None if terminal_only else latched


class DesktopRefusal(RuntimeError):
    """Closed operation adapter refusal, never reflected provider text."""

    def __init__(self, observation: Observation) -> None:
        self.observation = observation
        self.code = legacy_token(observation)
        if self.code is None:
            raise ValueError("READY is not a refusal or an operation success")
        super().__init__(self.code)


@dataclass
class _ObservationLatch:
    """One bounded lattice for watch and worker waits; raw history is not accumulated."""

    terminal: Observation | None = None
    snapshots: tuple[Observation, ...] = ()
    desktop_unready: str | None = None
    unknown: str | None = None

    def add(self, snapshot: Observation) -> Observation:
        """Keep first terminal, first refusal per band, and one snapshot per closed state."""
        state = snapshot.state
        if state != "READY" and not any(old.state == state for old in self.snapshots):
            self.snapshots += (snapshot,)
        if self.terminal is None and state in _TERMINAL_STATES:
            self.terminal = snapshot
        if self.desktop_unready is None and snapshot.desktop_unready:
            self.desktop_unready = snapshot.desktop_unready
        if self.unknown is None and snapshot.unknown_reason:
            self.unknown = snapshot.unknown_reason
        return self.terminal or snapshot


class DialogFoundError(RuntimeError):
    """Power BI Desktop is showing a blocking window; a human must act in Desktop (#831).

    The sanitized title travels as the ``title`` ATTRIBUTE only. ``args`` stays a single closed
    string so receipt adapters that read ``args[0]`` can never persist window text.
    """

    def __init__(self, pid: int, finding: DialogFinding) -> None:
        self.pid = pid
        self.finding = finding
        self.title = finding.title
        super().__init__(f"{DIALOG_NEEDS_HUMAN} pid={pid}; title withheld from the exception text")


class CredentialUnknownError(RuntimeError):
    """Power BI Desktop's blocking-dialog state stayed indeterminate right up to the deadline."""

    def __init__(self, pid: int, reason: str) -> None:
        self.pid = pid
        self.reason = reason
        super().__init__(f"desktop dialog state indeterminate for pid {pid}: {reason}")


class DesktopGoneError(RuntimeError):
    """Power BI Desktop enumerated ZERO windows AND the process is no longer running (issue #158)."""

    def __init__(self, pid: int, reason: str) -> None:
        self.pid = pid
        self.reason = reason
        super().__init__(f"power bi desktop process {pid} is gone: {reason}")


class DesktopUnreadyError(RuntimeError):
    """Power BI Desktop is alive but has no window, so its local state cannot be inspected."""

    def __init__(self, pid: int, reason: str) -> None:
        self.pid = pid
        self.reason = reason
        super().__init__(f"Power BI Desktop process {pid} is not ready: {reason}")


class Win32EnumerationError(RuntimeError):
    """Win32 window enumeration failed before a reliable verdict could be formed."""


WindowEnumerator = Callable[[int], Iterable[DesktopWindow]]
ProcessLivenessCheck = Callable[[int], bool]
TitleReader = Callable[[int], str]


def sanitize_title(raw: object) -> str:
    """Make an untrusted window title safe to quote once on one line for a human.

    Drops control, format (bidi overrides, zero-width) and line/paragraph separators, turns ``"``
    into ``'`` so the quoted field cannot be closed early, collapses whitespace and caps the length.
    Characters stdout cannot encode (a Polish, Chinese or emoji title on a cp1252 pipe, a lone surrogate)
    are backslash-escaped, so printing the stop can never raise and lose it.
    """
    if not isinstance(raw, str):
        return ""
    kept = "".join(char for char in raw if unicodedata.category(char) not in _TITLE_DROPPED_CATEGORIES)
    text = " ".join(kept.replace('"', "'").split())
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    text = text.encode(encoding, "backslashreplace").decode(encoding)
    return text[:TITLE_MAX_CHARS] + "..." if len(text) > TITLE_MAX_CHARS else text


def describe_open_dialog(title: str) -> str:
    """The D1 sentence naming an open dialog by its (already sanitized) title, without a full stop."""
    return f'a Power BI Desktop dialog is open: "{title}"' if title else "a Power BI Desktop dialog is open (no title)"


def _ownership_root(window: DesktopWindow, by_hwnd: dict[int, DesktopWindow]) -> DesktopWindow | None:
    """Walk ``window``'s owner links to the UNOWNED root, or ``None`` if the chain cannot be resolved."""
    seen = {id(window)}
    current = window
    while current.owner_hwnd:
        owner = by_hwnd.get(current.owner_hwnd)
        if owner is None or id(owner) in seen:
            return None
        seen.add(id(owner))
        current = owner
    return current


def main_frame(windows: Iterable[DesktopWindow]) -> DesktopWindow | None:
    """The application FRAME - the window dialogs block - or ``None`` when its identity is AMBIGUOUS."""
    windows = list(windows)
    by_hwnd = {window.hwnd: window for window in windows if window.hwnd}
    roots: list[DesktopWindow] = []
    for window in windows:
        if window.owner_hwnd:
            root = _ownership_root(window, by_hwnd)
            if root is None:
                # An unknown root must not be guessed at: the failure mode is excluding the wrong window.
                return None
        elif renders_nothing(window):
            continue
        else:
            root = window
        if not any(root is known for known in roots):
            roots.append(root)
    return roots[0] if len(roots) == 1 else None


def is_proven_non_blocking(window: DesktopWindow) -> bool:
    """An ENABLED owner proves this window blocks nothing; a modal disables its owner."""
    return window.owner_enabled is True


def renders_nothing(window: DesktopWindow) -> bool:
    """Is ``window`` incapable of showing a human anything, on TWO independent grounds?"""
    return not window.owner_hwnd and (window.width <= 0 or window.height <= 0)


def dialog_candidates(windows: Iterable[DesktopWindow], *, frame: DesktopWindow | None = None) -> list[DesktopWindow]:
    """Every visible window not POSITIVELY proven harmless, in enumeration (z-) order."""
    windows = list(windows)
    frame = frame if frame is not None else main_frame(windows)
    return [
        window
        for window in windows
        if window is not frame and not is_proven_non_blocking(window) and not renders_nothing(window)
    ]


def blocking_window(windows: Iterable[DesktopWindow]) -> DesktopWindow | None:
    """The window to NAME for a human, or ``None`` when the frame is provably unblocked.

    Order: the topmost owned window whose owner is disabled; else, with several rendering roots and no
    identified frame, the first root in z-order; else the first window whose owner chain cannot be
    resolved. A frame that is disabled with nothing else to name is itself the blocker.
    """
    windows = list(windows)
    frame = main_frame(windows)
    if frame is not None:
        candidates = dialog_candidates(windows, frame=frame)
        if candidates:
            return next((window for window in candidates if window.owner_enabled is False), candidates[0])
        if not frame.enabled and not frame.minimized:
            return frame
        return None
    owned = next(
        (window for window in windows if window.owner_hwnd and window.owner_enabled is False),
        None,
    )
    if owned is not None:
        return owned
    roots = [window for window in windows if not window.owner_hwnd and not renders_nothing(window)]
    if len(roots) > 1:
        return roots[0]
    by_hwnd = {window.hwnd: window for window in windows}
    unresolved = [window for window in windows if window.owner_hwnd and _ownership_root(window, by_hwnd) is None]
    return unresolved[0] if unresolved else windows[0]


def inspect_desktop_observer(
    pid: int,
    enumerate_windows: WindowEnumerator | None = None,
    process_is_alive: ProcessLivenessCheck = _process_alive,
    *,
    read_title: TitleReader | None = None,
) -> Observation:
    """Fold exact-window facts into one observation; the pure-policy injection seam."""
    try:
        windows = tuple((enumerate_windows or enumerate_pid_windows)(pid))
    except Win32EnumerationError as exc:
        return Observation(unknown_reason=f"window enumeration failed: {exc}", complete=False, reason="backend_failed")
    if not windows:
        # Zero windows is never proof of health (issue #158): alive means starting up or wedged
        # (DESKTOP_UNREADY), gone means exited or crashed (DESKTOP_GONE). Both are local failures.
        if process_is_alive(pid):
            return Observation(
                desktop_unready=(
                    "Power BI Desktop enumerated no windows while its process is still running; a "
                    "window-less process is starting up or wedged and its dialog state cannot be read"
                ),
                windows=windows,
                complete=False,
                reason="no_window",
            )
        return Observation(
            process_gone=(
                "Power BI Desktop enumerated no windows and its process is no longer running; it "
                "exited or crashed before any dialog state could be observed"
            ),
            windows=windows,
            complete=False,
            reason="process_gone",
        )
    frame = main_frame(windows)
    blocker = blocking_window(windows)
    if blocker is not None:
        title = "" if blocker is frame else _safe_title(blocker, read_title)
        finding = DialogFinding(blocker, title)
        return Observation(
            dialog=finding, findings=(finding,), windows=windows, target_window=frame, reason="dialog_open"
        )
    if frame is not None and frame.minimized:
        return Observation(
            unknown_reason=(
                "Power BI Desktop owner window is minimized; owned modal dialogs are hidden from enumeration"
            ),
            windows=windows,
            target_window=frame,
            complete=False,
            reason="minimized",
        )
    return Observation(windows=windows, target_window=frame)


def _safe_title(window: DesktopWindow, read_title: TitleReader | None) -> str:
    """A title read can never fail or widen an observation; it only names the window."""
    try:
        return read_title(window.hwnd) if read_title is not None else window.title
    except Exception:  # pylint: disable=broad-exception-caught
        return ""


def _hwnd_value(hwnd) -> int:
    """Normalize ctypes callback HWND values (plain int on this Python, ctypes object on others)."""
    value = getattr(hwnd, "value", hwnd)
    return int(value or 0)


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if os.name == "nt" else None


def _configure_user32(user32) -> None:
    """Set Win32 signatures explicitly so 64-bit handles are not truncated."""
    user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int


def configure_kernel32(kernel32) -> None:
    """Process-identity signatures shared with the launcher's frame fit."""
    handle, u64 = wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulonglong)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = handle
    kernel32.GetProcessId.argtypes = [handle]
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [handle, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetProcessTimes.argtypes = [handle, u64, u64, u64, u64]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.CloseHandle.restype = wintypes.BOOL


@functools.cache
def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    _configure_user32(user32)
    return user32


@functools.cache
def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    configure_kernel32(kernel32)
    return kernel32


def held_process_start(kernel32, handle: int, pid: int) -> str | None:
    """UTC .NET ticks truncated to 10 for a held handle; ``None`` when exited or aliased.

    An unreadable start raises ``OSError``: it is never evidence that the process is gone.
    """
    # OpenProcess ignores a PID's low two bits, so pid+1..+3 would open pid.
    if kernel32.GetProcessId(handle) != pid:
        return None
    for check in range(2):
        state = kernel32.WaitForSingleObject(handle, 0)
        if state == WAIT_OBJECT_0:
            return None
        if state != WAIT_TIMEOUT:
            raise OSError("process wait failed")
        if check:
            break
        times = [ctypes.c_ulonglong() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            raise OSError("process times unavailable")
    ticks = times[0].value + TICKS_AT_FILETIME_EPOCH
    return str(ticks - ticks % 10)


def process_start(pid: int) -> str | None:
    """The exact process-start token for ``pid``; ``None`` only when no such process exists."""
    if os.name != "nt":
        raise OSError("unsupported")
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION_SYNCHRONIZE, False, pid)
    if not handle:
        if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
            return None
        raise OSError("process unreadable")
    try:
        return held_process_start(kernel32, handle, pid)
    finally:
        kernel32.CloseHandle(handle)


def _owner_pid(user32, hwnd: int) -> int:
    value = wintypes.DWORD()
    return value.value if user32.GetWindowThreadProcessId(hwnd, ctypes.byref(value)) else 0


def _window_fact(user32, hwnd: int, pid: int) -> DesktopWindow:
    """One visible same-PID window's structure, re-verified so a racing change is never clean."""
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("backend_failed")
    owner = _hwnd_value(user32.GetWindow(hwnd, GW_OWNER))
    enabled = bool(user32.IsWindowEnabled(hwnd))
    owner_enabled = owner_same_pid = None
    if owner:
        owner_enabled = bool(user32.IsWindowEnabled(owner))
        owner_process = _owner_pid(user32, owner)
        owner_same_pid = owner_process == pid if owner_process else None
    if (
        _owner_pid(user32, hwnd) != pid
        or _hwnd_value(user32.GetWindow(hwnd, GW_OWNER)) != owner
        or bool(user32.IsWindowEnabled(hwnd)) != enabled
    ):
        raise ValueError("ambiguous")
    return DesktopWindow(
        title="",
        class_name="",
        width=max(0, rect.right - rect.left),
        height=max(0, rect.bottom - rect.top),
        minimized=bool(user32.IsIconic(hwnd)),
        hwnd=hwnd,
        owner_hwnd=owner,
        owner_enabled=owner_enabled,
        enabled=enabled,
        owner_same_pid=owner_same_pid,
    )


def _window_rows(pid: int) -> list[DesktopWindow]:
    """Visible top-level windows of ``pid`` in z-order, top first, via ``EnumWindows``."""
    user32 = _user32()
    rows: list[DesktopWindow] = []
    failures: list[BaseException] = []

    def visit(hwnd, _lparam) -> bool:
        try:
            handle = _hwnd_value(hwnd)
            if _owner_pid(user32, handle) == pid and user32.IsWindowVisible(handle):
                rows.append(_window_fact(user32, handle, pid))
            return True
        except Exception as error:  # pylint: disable=broad-exception-caught
            failures.append(error)
            return False

    callback = _WNDENUMPROC(visit)
    succeeded = user32.EnumWindows(callback, 0)
    if failures:
        raise failures[0]
    if not succeeded:
        raise ValueError("backend_failed")
    return rows


def _enumerate(pid: int, expected_process_start: str | None) -> tuple[str | None, tuple[DesktopWindow, ...]]:
    """Identity-bracketed in-process read: start before, windows, start after."""
    before = process_start(pid)
    if before is None:
        return None, ()
    if expected_process_start is not None and before != expected_process_start:
        raise ValueError("target_changed")
    rows = _window_rows(pid)
    if process_start(pid) != before:
        raise ValueError("target_changed")
    windows = tuple(replace(window, pid=str(pid), process_started_at=before) for window in rows)
    if len({window.hwnd for window in windows}) != len(windows):
        raise ValueError("invalid_payload")
    return before, windows


def _enumerate_pid_windows_with_count(pid: int) -> tuple[list[DesktopWindow], int]:
    """Read window facts for native qualification; production callers use observe with held identity."""
    try:
        windows = list(_enumerate(pid, None)[1])
    except (OSError, ValueError) as error:
        raise Win32EnumerationError("backend unavailable") from error
    return windows, len(windows)


def enumerate_pid_windows(pid: int) -> list[DesktopWindow]:
    """Enumerate visible top-level/owned windows for ``pid``; a failed read raises, never reads empty."""
    return _enumerate_pid_windows_with_count(pid)[0]


def _window_title(hwnd: int) -> str:
    """The caption of ``hwnd``, read only to name a blocking window for a human.

    For another process's window ``GetWindowTextW`` copies the stored caption without sending a message,
    so a busy Desktop UI thread cannot hang this read. A fixed buffer makes a length query unnecessary.
    """
    if os.name != "nt" or not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(TITLE_BUFFER_CHARS)
    copied = _user32().GetWindowTextW(hwnd, buffer, TITLE_BUFFER_CHARS)
    return buffer.value[: max(0, copied)]


_OPERATION_MESSAGES = {
    "CREDENTIAL_UNKNOWN": "Dialog evidence stayed indeterminate to the deadline; inspect Desktop before retrying.",
    "DESKTOP_GONE": (
        "Power BI Desktop enumerated no windows and its process is no longer running; it exited or crashed "
        "before any dialog state could be observed"
    ),
    "DESKTOP_UNREADY": (
        "Power BI Desktop enumerated no windows while its process is still running; a window-less process "
        "is starting up or wedged and its dialog state cannot be read"
    ),
}


def dialog_stop_detail(finding: DialogFinding) -> str:
    """The D1 detail text shared by every operation prefix."""
    return f"{describe_open_dialog(finding.title)}. A human must act in Desktop."


def print_operation_refusal(prefix: str, pid: int, code: str, *, finding: DialogFinding | None = None) -> None:
    """Format existing operation tokens only; exit bands and operation results stay with callers."""
    if prefix not in {"REFRESH", "PREFLIGHT"} or type(pid) is not int or not 0 < pid < 2**32:  # pylint: disable=unidiomatic-typecheck
        raise ValueError("operation diagnostic identity")
    if finding is not None:
        if code != DIALOG_NEEDS_HUMAN:
            raise ValueError("operation diagnostic vocabulary")
        detail = dialog_stop_detail(finding)
    elif code in _OPERATION_MESSAGES:
        detail = _OPERATION_MESSAGES[code]
    else:
        raise ValueError("operation diagnostic vocabulary")
    print(f"{prefix}: {code} pid={pid}; {detail}", flush=True)


def print_refresh_banner(
    pid: int,
    timeout_sec: int,
    grace_sec: int | float,
    operation: str = "refresh",
) -> None:
    """Print the no-dialog, self-bounded refresh warning before the wait starts."""
    total = timeout_sec + grace_sec
    if operation == "calculate":
        prefix = f"No blocking dialog on PID {pid}. Calculate in progress, bounded at "
        wait_note = "this mode recalculates formulas without reading source rows, so long waits are unusual. "
    else:
        prefix = f"No blocking dialog on PID {pid}. Refreshing, bounded at "
        wait_note = "a long wait here is expected for a serverless cold start. "
    print(
        f"{prefix}{timeout_sec}s XMLA + {grace_sec}s grace ({total}s total); {wait_note}"
        "DO NOT kill this process - at the deadline it re-checks and prints one final machine-readable "
        "verdict line. Killing it early yields NO verdict.",
        flush=True,
    )


def print_refresh_unknown_banner(pid: int, timeout_sec: int, grace_sec: int | float, reason: str) -> None:
    """Print the bounded-refresh warning when the t=0 dialog check is indeterminate (issue #153)."""
    total = timeout_sec + grace_sec
    print(
        f"Blocking-dialog check on PID {pid} is UNKNOWN ({reason}). Refreshing, bounded at "
        f"{timeout_sec}s XMLA + {grace_sec}s grace ({total}s total); a minimized owner can hide owned "
        "dialogs from enumeration. DO NOT kill this process - at the deadline it re-checks and prints "
        "one final machine-readable verdict line. Killing it early yields NO verdict.",
        flush=True,
    )


def print_refresh_heartbeat(elapsed: float, total: float) -> None:
    """Print an elapsed/total countdown without claiming progress."""
    print(f"still refreshing, {int(elapsed)}s / {int(total)}s", flush=True)


def print_indeterminate_state_notice(pid: int, reason: str) -> None:
    """Loudly note the first time the wait latches an indeterminate window state (issues #154, #158)."""
    print(
        f"PID {pid} entered an indeterminate window state mid-wait ({reason}); latching this "
        "observation - a later clear cannot un-see it, because the evidence a blocking dialog would "
        "leave is no longer readable. This run ends at the deadline with a distinct indeterminate "
        "verdict, never a slow-source timeout.",
        flush=True,
    )


def raise_terminal_detection(pid: int, state: Observation) -> None:
    """Raise for the observations that end a bounded wait IMMEDIATELY: any dialog, or a gone process."""
    if state.state == "HUMAN_ACTION_REQUIRED" and state.dialog is not None:
        raise DialogFoundError(pid, state.dialog)
    if state.process_gone is not None:
        raise DesktopGoneError(pid, state.process_gone)


def raise_latched_verdict(pid: int, *, desktop_unready: str | None = None, unknown: str | None = None) -> None:
    """Raise whichever latched observation a bounded wait ended with, in precedence order."""
    if desktop_unready is not None:
        raise DesktopUnreadyError(pid, desktop_unready)
    if unknown is not None:
        raise CredentialUnknownError(pid, unknown)


# pylint: disable=too-many-arguments
def join_with_credential_poll(
    worker,
    *,
    pid: int | None,
    total_timeout: float,
    heartbeat_seconds: float,
    poll_seconds: float,
    detector: Callable[[int], Observation] | None = None,
    initial_state: Observation | None = None,
    deadline: float | None = None,
    before_wait: Callable[[], None] | None = None,
    heartbeat: Callable[[float, float], None] | None = print_refresh_heartbeat,
    wait_limit: Callable[[], float] | None = None,
    notices: bool = True,
    completed_at: Callable[[], float | None] | None = None,
) -> bool:
    """Wait for ``worker`` while polling Desktop; any blocking dialog stops the wait at once."""
    # The locals are injected parameters plus the latch this wait must not lose.
    # pylint: disable=too-many-locals,too-many-branches
    deadline = time.monotonic() + total_timeout if deadline is None else deadline
    if detector is None and pid is not None:

        def inspect(target: int) -> Observation:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("observation deadline exhausted before start")
            return observe(
                target,
                timeout_s=min(8.0, remaining),
                expected_process_start=initial_state.process_started_at if initial_state else None,
            )

        detector = inspect
    latch = _ObservationLatch()
    if initial_state is not None and pid is not None:
        raise_terminal_detection(pid, latch.add(initial_state))
    started = deadline - total_timeout
    next_heartbeat = heartbeat_seconds
    while worker.is_alive():
        elapsed = time.monotonic() - started
        remaining = max(0.0, total_timeout - elapsed)
        if remaining <= 0:
            break
        if before_wait is not None:
            before_wait()
        worker.join(
            min(
                remaining,
                poll_seconds,
                max(0.01, next_heartbeat - elapsed),
                max(0.01, wait_limit()) if wait_limit is not None else remaining,
            )
        )
        elapsed = time.monotonic() - started
        if not worker.is_alive() or time.monotonic() >= deadline:
            break
        if pid is not None:
            try:
                state = detector(pid)
            except TimeoutError:
                break
            if time.monotonic() >= deadline and state.state in _TERMINAL_STATES:
                state = Observation(pid=str(pid), complete=False, unknown_reason="timeout", reason="timeout")
            prior_unknown = latch.unknown
            raise_terminal_detection(pid, latch.add(state))
            if notices and prior_unknown is None and latch.unknown is not None:
                print_indeterminate_state_notice(pid, latch.unknown)
        if elapsed >= next_heartbeat and worker.is_alive():
            if heartbeat is not None:
                heartbeat(elapsed, total_timeout)
            next_heartbeat += heartbeat_seconds
    settled_at = completed_at() if completed_at is not None else None
    if not worker.is_alive() and settled_at is not None and settled_at < deadline:
        return True
    if worker.is_alive() or time.monotonic() >= deadline:
        if pid is not None:
            if latch.terminal is not None:
                raise_terminal_detection(pid, latch.terminal)
            raise_latched_verdict(pid, desktop_unready=latch.desktop_unready, unknown=latch.unknown)
        return False
    return True


# pylint: enable=too-many-arguments


def _handle_string(value: int) -> str | None:
    return hex(value) if value else None


def _decimal_identity(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _handle_value(value: object, *, nullable: bool = True) -> int:
    if value is None and nullable:
        return 0
    if not isinstance(value, str) or not re.fullmatch(r"0x[1-9a-f][0-9a-f]{0,15}", value):
        raise ValueError("handle")
    return int(value, 16)


def observe(
    pid: int | str,
    *,
    hwnd: int | str | None = None,
    expected_process_start: str | None = None,
    timeout_s: float = 8.0,
) -> Observation:
    """Read one immutable snapshot; callers holding an identity must supply its exact start token."""
    # pylint: disable=too-many-branches
    started = time.monotonic()
    if isinstance(pid, int) and not isinstance(pid, bool):
        pid = str(pid)
    if not _decimal_identity(pid) or not 0 < int(pid) < 2**32:
        raise ValueError("pid")
    if expected_process_start is not None and not _decimal_identity(expected_process_start):
        raise ValueError("process start")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or not 0 < timeout_s <= 8
    ):
        raise ValueError("timeout")
    target = _handle_value(hwnd) if isinstance(hwnd, str) else hwnd
    if target is not None and (type(target) is not int or target <= 0):  # pylint: disable=unidiomatic-typecheck
        raise ValueError("hwnd")
    state = Observation(pid=pid, complete=False, reason="unsupported", unknown_reason="unsupported")
    try:
        started_at, windows = _enumerate(int(pid), expected_process_start)
        if started_at is None:
            state = Observation(pid=pid, complete=False, reason="process_gone", process_gone="process_gone")
        elif target is not None and not any(window.hwnd == target for window in windows):
            state = Observation(
                pid=pid,
                process_started_at=started_at,
                complete=False,
                reason="target_changed",
                unknown_reason="target_changed",
            )
        else:
            state = inspect_desktop_observer(int(pid), lambda _: windows, read_title=_window_title)
            state = replace(
                state,
                pid=pid,
                process_started_at=started_at,
                target_window=next((window for window in windows if window.hwnd == target), state.target_window),
            )
            if target is not None and state.dialog is not None and state.dialog.window.hwnd != target:
                state = Observation(
                    pid=pid,
                    process_started_at=started_at,
                    complete=False,
                    unknown_reason="ambiguous",
                    reason="ambiguous",
                    findings=state.findings,
                    target_window=state.target_window,
                )
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        reason = str(error) if str(error) in _REASONS else "invalid_payload"
        if reason == "target_changed":
            state = Observation(pid=pid, complete=False, reason="process_gone", process_gone="identity_changed")
        else:
            state = replace(state, reason=reason, unknown_reason=reason)
    elapsed_ms = max(0.0, (time.monotonic() - started) * 1000)
    if elapsed_ms > timeout_s * 1000:
        state = Observation(pid=pid, complete=False, reason="timeout", unknown_reason="timeout")
    return replace(state, elapsed_ms=elapsed_ms)


@dataclass
class Watch:
    """Invocation-local latch lattice; no background callback may mutate a settled observation."""

    # One invocation owns these locks, identity, deadline and retained evidence together.
    # pylint: disable=too-many-instance-attributes

    pid: int | str
    deadline: float
    operation_id: str
    expected_process_start: str | None = None
    _latch: _ObservationLatch = field(default_factory=_ObservationLatch, repr=False)
    closed: bool = False
    _state_lock: object = field(default_factory=threading.Lock, repr=False)
    _observe_lock: object = field(default_factory=threading.Lock, repr=False)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def latched(self) -> tuple[Observation, ...]:
        """Representative immutable uncertainty records, bounded by the closed state vocabulary."""
        return self._latch.snapshots

    @property
    def terminal(self) -> Observation | None:
        """The first established terminal observation."""
        return self._latch.terminal

    def close(self) -> None:
        """Settle this invocation; Desktop is never a termination target."""
        with self._state_lock:
            self.closed = True

    def observe(self, *, timeout_s: float = 8.0) -> Observation:
        """Serialize snapshots against one caller deadline and held process identity."""
        remaining = self.deadline - time.monotonic()
        # A context manager cannot enforce the remaining acquisition deadline.
        acquired = self._observe_lock.acquire(timeout=max(0.0, min(timeout_s, remaining)))  # pylint: disable=consider-using-with
        try:
            remaining = self.deadline - time.monotonic()
            if not acquired or self.closed or remaining <= 0:
                return Observation(pid=str(self.pid), complete=False, unknown_reason="timeout", reason="timeout")
            snapshot = observe(
                self.pid,
                expected_process_start=self.expected_process_start,
                timeout_s=min(timeout_s, remaining),
            )
            with self._state_lock:
                if self.closed:
                    return Observation(
                        pid=str(self.pid),
                        complete=False,
                        reason="timeout",
                        unknown_reason="timeout",
                        operation_id=self.operation_id,
                    )
                if self.expected_process_start is None and snapshot.process_started_at is not None:
                    self.expected_process_start = snapshot.process_started_at
                return replace(snapshot, operation_id=self.operation_id)
        finally:
            if acquired:
                self._observe_lock.release()

    def record(self, observation: Observation) -> Observation:
        """First terminal wins; uncertainty never disappears behind later quiet evidence."""
        with self._state_lock:
            if self.closed:
                raise RuntimeError("watch settled")
            if type(observation) is not Observation:  # pylint: disable=unidiomatic-typecheck
                raise ValueError("observation")
            if observation.pid != str(self.pid) or (
                observation.state not in {"UNKNOWN", "PROCESS_GONE"}
                and not _decimal_identity(observation.process_started_at)
            ):
                observation = Observation(
                    pid=str(self.pid), complete=False, reason="target_changed", unknown_reason="target_changed"
                )
            if time.monotonic() >= self.deadline:
                observation = Observation(pid=str(self.pid), complete=False, unknown_reason="timeout", reason="timeout")
            observation = replace(observation, operation_id=self.operation_id)
            if (
                self.expected_process_start is not None
                and observation.process_started_at != self.expected_process_start
                and (observation.process_started_at is not None or observation.complete)
                and observation.state != "PROCESS_GONE"
            ):
                observation = Observation(
                    pid=str(self.pid),
                    complete=False,
                    unknown_reason="target_changed",
                    reason="target_changed",
                    operation_id=self.operation_id,
                )
            return self._latch.add(observation)

    def __iter__(self):
        if not self.closed and time.monotonic() >= self.deadline:
            yield self.record(
                Observation(pid=str(self.pid), complete=False, reason="timeout", unknown_reason="timeout")
            )
            return
        while not self.closed and time.monotonic() < self.deadline:
            yield self.record(self.observe(timeout_s=min(8.0, max(0.001, self.deadline - time.monotonic()))))
            if self.terminal is not None:
                return
            time.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, self.deadline - time.monotonic())))


def watch(pid: int | str, *, deadline: float, operation_id: str, expected_process_start: str | None = None) -> Watch:
    """Create one observation supervisor without starting native work at import/construction time."""
    canonical_pid = str(pid) if type(pid) is int else pid  # pylint: disable=unidiomatic-typecheck
    if not _decimal_identity(canonical_pid) or not 0 < int(canonical_pid) < 2**32:
        raise ValueError("pid")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ValueError("deadline")
    if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
        raise ValueError("operation id")
    if expected_process_start is not None and not _decimal_identity(expected_process_start):
        raise ValueError("process start")
    return Watch(pid, deadline, operation_id, expected_process_start)
