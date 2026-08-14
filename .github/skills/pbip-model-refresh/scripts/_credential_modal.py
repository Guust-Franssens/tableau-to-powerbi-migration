"""Credential modal detection shared by the PBIP refresh/query scripts.

The regex in ``credential_modal_signature.regex`` is the same signature used by
``probe_desktop_credential.ps1``. Keep the detector's matching rule here and the signature in that
resource so the fast Python checks and the PowerShell arbiter cannot drift.
"""

from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import wintypes
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

SIGNATURE_PATH = Path(__file__).resolve().with_name("credential_modal_signature.regex")
POLL_SECONDS = 5.0
CONNECTOR_SOURCE_RE = re.compile(
    r"\b(?P<kind>Sql\.Database|Snowflake\.Databases|Databricks\.[A-Za-z]+|Odbc\.DataSource|Web\.Contents)\s*\("
    r"\s*(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)')?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DesktopWindow:
    """Visible top-level Power BI Desktop window plus descendant Win32 text."""

    title: str
    class_name: str
    width: int
    height: int
    texts: tuple[str, ...] = ()
    minimized: bool = False
    hwnd: int = 0


@dataclass(frozen=True)
class CredentialModal:
    """Evidence that Power BI Desktop is blocked on a data-source credential dialog."""

    matched_text: str
    window: DesktopWindow

    @property
    def excerpt(self) -> str:
        """Short UI text excerpt suitable for a verdict line."""
        return self.matched_text[:80]


@dataclass(frozen=True)
class BlockingDialog:
    """Evidence that Desktop is blocked by a visible dialog whose text may be unreadable."""

    window: DesktopWindow


@dataclass(frozen=True)
class CredentialDetection:
    """Credential-dialog inspection result for a Desktop PID."""

    modal: CredentialModal | None = None
    blocking_dialog: BlockingDialog | None = None
    unknown_reason: str | None = None
    windows: tuple[DesktopWindow, ...] = ()


class CredentialMissingError(RuntimeError):
    """Power BI Desktop is showing a data-source credential dialog."""

    def __init__(self, pid: int, modal: CredentialModal, source_hint: str | None = None) -> None:
        self.pid = pid
        self.modal = modal
        self.source_hint = source_hint
        super().__init__(describe_modal(modal, source_hint))


class DialogBlockedError(RuntimeError):
    """Power BI Desktop is showing an unreadable/non-credential blocking dialog."""

    def __init__(self, pid: int, dialog: BlockingDialog) -> None:
        self.pid = pid
        self.dialog = dialog
        super().__init__(describe_blocking_dialog(dialog))


WindowEnumerator = Callable[[int], Iterable[DesktopWindow]]

DESKTOP_MAIN_CLASS_PREFIX = "WindowsForms10.Window.8"
MIN_DIALOG_WIDTH = 100
MIN_DIALOG_HEIGHT = 100


class Win32EnumerationError(RuntimeError):
    """Win32 window enumeration failed before a reliable modal verdict could be formed."""


def _winenumproc_type():
    """Return the Win32 callback type lazily so the module imports on non-Windows CI."""
    if os.name != "nt":
        raise Win32EnumerationError("Win32 callback types are only available on Windows")
    return ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def credential_signature() -> re.Pattern[str]:
    """Compile the shared credential-dialog signature."""
    return re.compile(SIGNATURE_PATH.read_text(encoding="utf-8").strip(), re.IGNORECASE)


def match_credential_modal(windows: Iterable[DesktopWindow]) -> CredentialModal | None:
    """Return the first window whose descendant text matches the shared credential signature."""
    signature = credential_signature()
    for window in windows:
        for text in window.texts:
            if text and signature.search(text):
                return CredentialModal(matched_text=text, window=window)
    return None


def blocking_dialog_candidates(windows: Iterable[DesktopWindow]) -> list[BlockingDialog]:
    """Visible non-main, non-zero-size windows that can block Desktop operations."""
    candidates: list[BlockingDialog] = []
    for window in windows:
        if window.class_name.startswith(DESKTOP_MAIN_CLASS_PREFIX):
            continue
        if window.width < MIN_DIALOG_WIDTH or window.height < MIN_DIALOG_HEIGHT:
            continue
        candidates.append(BlockingDialog(window))
    return candidates


def _hwnd_value(hwnd) -> int:
    """Normalize ctypes callback HWND values (plain int on this Python, ctypes object on others)."""
    value = getattr(hwnd, "value", hwnd)
    return int(value or 0)


def _configure_user32(user32) -> None:
    """Set Win32 signatures explicitly so 64-bit handles are not truncated."""
    callback_type = _winenumproc_type()
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL


def _window_text(user32, hwnd: int) -> str:
    """Best-effort Win32 text for ``hwnd``."""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class_name(user32, hwnd: int) -> str:
    """Win32 class name for ``hwnd``."""
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _child_texts(user32, hwnd: int) -> tuple[str, ...]:
    """Text from child HWNDs of a candidate top-level window."""
    texts: list[str] = []
    errors: list[BaseException] = []

    def callback(child_hwnd, _lparam):
        try:
            text = _window_text(user32, _hwnd_value(child_hwnd))
            if text:
                texts.append(text)
        except BaseException as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            errors.append(exc)
        return True

    enum_child_proc = _winenumproc_type()(callback)
    user32.EnumChildWindows(hwnd, enum_child_proc, 0)
    if errors:
        raise Win32EnumerationError(f"child window enumeration callback failed: {errors[0]}") from errors[0]
    return tuple(texts)


def _enumerate_pid_windows_with_count(pid: int) -> tuple[list[DesktopWindow], int]:
    """Enumerate visible top-level/owned windows for ``pid`` using Win32 ``EnumWindows``.

    UI Automation root-child discovery misses Power BI connector dialogs because they are owned
    windows, not root children. Win32 ``EnumWindows`` sees that shape and still scopes by PID, so
    concurrent Desktop instances do not cross-contaminate.
    """
    if os.name != "nt":
        raise Win32EnumerationError("Win32 window enumeration is only available on Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    _configure_user32(user32)
    windows: list[DesktopWindow] = []
    errors: list[BaseException] = []
    visited = 0

    def callback(hwnd, _lparam):
        nonlocal visited
        visited += 1
        try:
            hwnd_int = _hwnd_value(hwnd)
            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd_int, ctypes.byref(owner_pid))
            if owner_pid.value != pid or not user32.IsWindowVisible(hwnd_int):
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd_int, ctypes.byref(rect))
            title = _window_text(user32, hwnd_int)
            texts = tuple(text for text in (title, *_child_texts(user32, hwnd_int)) if text)
            windows.append(
                DesktopWindow(
                    title=title,
                    class_name=_class_name(user32, hwnd_int),
                    width=max(0, rect.right - rect.left),
                    height=max(0, rect.bottom - rect.top),
                    texts=texts,
                    minimized=bool(user32.IsIconic(hwnd_int)),
                    hwnd=hwnd_int,
                )
            )
        except BaseException as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            errors.append(exc)
        return True

    enum_windows_proc = _winenumproc_type()(callback)
    if not user32.EnumWindows(enum_windows_proc, 0):
        error_code = ctypes.get_last_error()
        raise Win32EnumerationError(f"EnumWindows failed with Win32 error {error_code}")
    if errors:
        raise Win32EnumerationError(f"window enumeration callback failed: {errors[0]}") from errors[0]
    if visited == 0:
        raise Win32EnumerationError("EnumWindows returned no callbacks")
    return windows, visited


def enumerate_pid_windows(pid: int) -> list[DesktopWindow]:
    """Enumerate visible top-level/owned windows for ``pid`` using Win32 ``EnumWindows``."""
    windows, _visited = _enumerate_pid_windows_with_count(pid)
    return windows


def inspect_credential_modal(
    pid: int, enumerate_windows: WindowEnumerator = enumerate_pid_windows
) -> CredentialDetection:
    """Inspect ``pid`` for a credential modal, preserving indeterminate states."""
    try:
        windows = tuple(enumerate_windows(pid))
    except Win32EnumerationError as exc:
        return CredentialDetection(unknown_reason=f"window enumeration failed: {exc}")
    candidates = blocking_dialog_candidates(windows)
    candidate_windows = [candidate.window for candidate in candidates]
    modal = match_credential_modal(candidate_windows)
    if modal is not None:
        return CredentialDetection(modal=modal, windows=windows)
    if candidates:
        return CredentialDetection(blocking_dialog=candidates[0], windows=windows)
    minimized = [
        window for window in windows if window.minimized and window.class_name.startswith(DESKTOP_MAIN_CLASS_PREFIX)
    ]
    if minimized:
        return CredentialDetection(
            unknown_reason="Power BI Desktop owner window is minimized; owned credential dialogs are hidden",
            windows=windows,
        )
    return CredentialDetection(windows=windows)


def detect_credential_modal(
    pid: int, enumerate_windows: WindowEnumerator = enumerate_pid_windows
) -> CredentialModal | None:
    """Detect a currently visible credential modal for ``pid``."""
    return inspect_credential_modal(pid, enumerate_windows).modal


def describe_modal(modal: CredentialModal, source_hint: str | None = None) -> str:
    """Human-readable modal evidence for the verdict line."""
    window = modal.window
    size = f"{window.width}x{window.height}" if window.width and window.height else "unknown size"
    source = f" source={source_hint};" if source_hint else ""
    title = window.title or "(empty title)"
    return f"{source} window title={title!r}, class={window.class_name!r}, size={size}, text={modal.excerpt!r}"


def describe_blocking_dialog(dialog: BlockingDialog) -> str:
    """Human-readable evidence for an unreadable/non-credential blocking dialog."""
    window = dialog.window
    size = f"{window.width}x{window.height}" if window.width and window.height else "unknown size"
    title = window.title or "(empty title)"
    return f"window title={title!r}, class={window.class_name!r}, size={size}, text=<unreadable or non-credential>"


def source_hint_from_model(model_dir: Path | None) -> str | None:
    """Best-effort source hint from TMDL/M expressions, without requiring Desktop."""
    if model_dir is None:
        return None
    definition = model_dir / "definition"
    if not definition.is_dir():
        return None
    for path in sorted(definition.rglob("*.tmdl")):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        match = CONNECTOR_SOURCE_RE.search(text)
        if not match:
            continue
        source = match.group("double") or match.group("single")
        kind = match.group("kind")
        return f"{kind}({source})" if source else kind
    return None


def print_refresh_banner(pid: int, timeout_sec: int, grace_sec: int | float) -> None:
    """Print the no-dialog, self-bounded refresh warning before the wait starts."""
    total = timeout_sec + grace_sec
    print(
        f"No blocking dialog on PID {pid}. Refreshing, bounded at {timeout_sec}s XMLA + "
        f"{grace_sec}s grace ({total}s total); a long wait here is expected for a serverless cold start. "
        "DO NOT kill this process - at the deadline it re-checks and reports CREDENTIAL_MISSING, "
        "BLOCKED_BY_DIALOG, or SLOW_SOURCE. Killing it early yields NO verdict.",
        flush=True,
    )


def print_refresh_unknown_banner(pid: int, timeout_sec: int, grace_sec: int | float, reason: str) -> None:
    """Print the bounded-refresh warning when the t=0 modal check is indeterminate."""
    total = timeout_sec + grace_sec
    print(
        f"Credential dialog check on PID {pid} is UNKNOWN ({reason}). Refreshing, bounded at "
        f"{timeout_sec}s XMLA + {grace_sec}s grace ({total}s total); a minimized owner can hide owned "
        "dialogs from enumeration. DO NOT kill this process - at the deadline it re-checks with the "
        "dialog arbiter and reports CREDENTIAL_MISSING, BLOCKED_BY_DIALOG, or SLOW_SOURCE. Killing it early "
        "yields NO verdict.",
        flush=True,
    )


def print_refresh_heartbeat(elapsed: float, total: float) -> None:
    """Print an elapsed/total countdown without claiming progress."""
    print(f"still refreshing, {int(elapsed)}s / {int(total)}s", flush=True)


# pylint: disable=too-many-arguments
def join_with_credential_poll(
    worker,
    *,
    pid: int,
    total_timeout: float,
    heartbeat_seconds: float,
    poll_seconds: float,
    source_hint: str | None = None,
    detector: Callable[[int], CredentialDetection] = inspect_credential_modal,
) -> bool:
    """Wait for ``worker`` while polling for a late credential dialog.

    Returns True when the worker finished before the wall-clock deadline, False when the deadline
    elapsed. Raises as soon as the shared detector sees a blocking dialog.
    """
    started = time.monotonic()
    next_heartbeat = heartbeat_seconds
    while worker.is_alive():
        elapsed = time.monotonic() - started
        remaining = max(0.0, total_timeout - elapsed)
        if remaining <= 0:
            break
        worker.join(min(remaining, poll_seconds, max(0.0, next_heartbeat - elapsed)))
        elapsed = time.monotonic() - started
        state = detector(pid)
        if state.modal is not None:
            raise CredentialMissingError(pid, state.modal, source_hint)
        if state.blocking_dialog is not None:
            raise DialogBlockedError(pid, state.blocking_dialog)
        if elapsed >= next_heartbeat and worker.is_alive():
            print_refresh_heartbeat(elapsed, total_timeout)
            next_heartbeat += heartbeat_seconds
    if worker.is_alive():
        state = detector(pid)
        if state.modal is not None:
            raise CredentialMissingError(pid, state.modal, source_hint)
        if state.blocking_dialog is not None:
            raise DialogBlockedError(pid, state.blocking_dialog)
        return False
    return True


# pylint: enable=too-many-arguments
