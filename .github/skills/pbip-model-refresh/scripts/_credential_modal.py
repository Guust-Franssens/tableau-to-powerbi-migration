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

# Sibling module, resolved once the caller puts this scripts/ dir on sys.path (probe, refresh, and the
# test conftest all do). Reused rather than reimplemented for issue #158's zero-window liveness split:
# its bias errs toward "alive" on any ambiguity, so an uncertain Desktop routes to UNKNOWN-latched and
# never to a false crash - and it is already load-bearing and tested for stale-lock reclaim.
from _lock import _process_alive

SIGNATURE_PATH = Path(__file__).resolve().with_name("credential_modal_signature.regex")
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
    desktop_unready: str | None = None
    process_gone: str | None = None
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


class CredentialUnknownError(RuntimeError):
    """Power BI Desktop's blocking-dialog state stayed indeterminate right up to the deadline.

    Raised by :func:`join_with_credential_poll` when it LATCHED an indeterminate observation - the
    owner window went iconic, which hides its owned modal dialogs from enumeration - that a later
    ``none`` could not erase. It is a mandatory THIRD outcome, distinct from a detected block
    (:class:`CredentialMissingError` / :class:`DialogBlockedError`) and from a healthy deadline:
    minimizing then restoring the owner destroys the dialog evidence for good (measured 2026-08-14,
    issue #154), so ``none`` after the fact is indistinguishable from a genuinely healthy Desktop and
    is NOT proof of health. Reporting a bare timeout here would blame a slow source for what is really
    an unobservable dialog no automation can fill.

    ``reason`` is the detector's own marker-free string, carried verbatim so the caller's verdict line
    cannot smuggle a ``CREDENTIAL_MARKER`` into the parent classifier's free-text scan (issue #153).
    """

    def __init__(self, pid: int, reason: str) -> None:
        self.pid = pid
        self.reason = reason
        super().__init__(f"desktop dialog state indeterminate for pid {pid}: {reason}")


class DesktopGoneError(RuntimeError):
    """Power BI Desktop enumerated ZERO windows AND the process is no longer running (issue #158).

    A live, working Desktop always owns at least its main window, so an empty enumeration is never
    evidence of health - it means the process has exited, crashed, or (while still starting) has not
    yet created a window. This class is the DEFINITIVE half of that split: the liveness check in
    :func:`inspect_credential_modal` has confirmed the process is gone, so there is nothing left to
    wait for. It is distinct from :class:`CredentialUnknownError` (process still ALIVE but momentarily
    window-less - indeterminate, latched) and must never be reported as a slow source: the data source
    is not implicated at all when Desktop itself has died.

    ``reason`` is the detector's own marker-free string, carried verbatim so the caller's verdict line
    cannot smuggle a ``CREDENTIAL_MARKER`` into the parent classifier's free-text scan (issue #153).
    """

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


WindowEnumerator = Callable[[int], Iterable[DesktopWindow]]
ProcessLivenessCheck = Callable[[int], bool]

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
    pid: int,
    enumerate_windows: WindowEnumerator = enumerate_pid_windows,
    process_is_alive: ProcessLivenessCheck = _process_alive,
) -> CredentialDetection:
    """Inspect ``pid`` for a credential modal, preserving indeterminate states."""
    # pylint: disable=too-many-return-statements
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
            unknown_reason=(
                "Power BI Desktop owner window is minimized; owned modal dialogs are hidden from enumeration"
            ),
            windows=windows,
        )
    if not windows:
        # A live, working Desktop always owns at least its main window, so ZERO windows is never proof
        # of health (issue #158). Split it by liveness: an alive-but-window-less process is starting up
        # or wedged (indeterminate -> latch, like the minimized case), while a gone process exited or
        # crashed (definitive -> a distinct terminal state that must never be blamed on a slow source).
        if process_is_alive(pid):
            return CredentialDetection(
                desktop_unready=(
                    "Power BI Desktop enumerated no windows while its process is still running; a "
                    "window-less process is starting up or wedged and its dialog state cannot be read"
                ),
                windows=windows,
            )
        return CredentialDetection(
            process_gone=(
                "Power BI Desktop enumerated no windows and its process is no longer running; it "
                "exited or crashed before any dialog state could be observed"
            ),
            windows=windows,
        )
    return CredentialDetection(windows=windows)


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
    """Print the no-dialog, self-bounded refresh warning before the wait starts.

    Deliberately does NOT name the verdict tokens it may later print, and interpolates NO caller-supplied
    text - so unlike :func:`print_refresh_unknown_banner`, this banner's marker-free guarantee is total
    and self-contained. This banner is emitted on the HEALTHY no-dialog path and is captured verbatim by
    ``probe_live_source``'s classifier; naming ``CREDENTIAL_MISSING`` / ``BLOCKED_BY_DIALOG`` here (or
    even the bare word "credential") made the reassuring message classify a successful refresh as
    ``NO_CREDENTIAL`` (issue #153). The classifier now matches verdict LINES structurally, but keeping
    control tokens out of free prose is the belt-and-braces half of that fix - so a future helpful edit
    cannot re-arm the landmine.
    """
    total = timeout_sec + grace_sec
    print(
        f"No blocking dialog on PID {pid}. Refreshing, bounded at {timeout_sec}s XMLA + "
        f"{grace_sec}s grace ({total}s total); a long wait here is expected for a serverless cold start. "
        "DO NOT kill this process - at the deadline it re-checks and prints one final machine-readable "
        "verdict line. Killing it early yields NO verdict.",
        flush=True,
    )


def print_refresh_unknown_banner(pid: int, timeout_sec: int, grace_sec: int | float, reason: str) -> None:
    """Print the bounded-refresh warning when the t=0 modal check is indeterminate.

    This banner's OWN static prose names no control tokens and not the bare word "credential" - but it
    interpolates ``reason``, a caller-supplied string from ``inspect_credential_modal`` that the
    classifier scans along with everything else. So the marker-free guarantee for the UNKNOWN path is
    owned by the DETECTOR, not by this banner alone: the minimized-owner ``unknown_reason`` was reworded
    off the word "credential" (a ``CREDENTIAL_MARKER``) for exactly this reason (issue #153) - otherwise
    a slow/timeout refresh here was mislabelled ``NO_CREDENTIAL``. The prefix reads "Blocking-dialog
    check ... UNKNOWN" rather than the old "Credential dialog check" as the belt-and-braces static half.
    """
    total = timeout_sec + grace_sec
    print(
        f"Blocking-dialog check on PID {pid} is UNKNOWN ({reason}). Refreshing, bounded at "
        f"{timeout_sec}s XMLA + {grace_sec}s grace ({total}s total); a minimized owner can hide owned "
        "dialogs from enumeration. DO NOT kill this process - at the deadline it re-checks with the "
        "dialog arbiter and prints one final machine-readable verdict line. Killing it early "
        "yields NO verdict.",
        flush=True,
    )


def print_refresh_heartbeat(elapsed: float, total: float) -> None:
    """Print an elapsed/total countdown without claiming progress."""
    print(f"still refreshing, {int(elapsed)}s / {int(total)}s", flush=True)


def print_indeterminate_state_notice(pid: int, reason: str) -> None:
    """Loudly note the first time the wait latches an indeterminate window state (issues #154, #158).

    Two observations land here, and they mean the same thing - the dialog state can no longer be read
    reliably: the owner window went iconic (minimize -> restore destroys the owned-dialog evidence for
    good, measured 2026-08-14), or enumeration returned no windows while the process was still alive
    (starting up or wedged, issue #158). Either way the wait latches it so a later ``none`` cannot pass
    for health; this notice makes the transition visible in the transcript instead of silent.

    Like every string this module prints into the refresh transcript, it is deliberately MARKER-FREE
    (no ``CREDENTIAL_MARKER`` token, not even the bare word "credential") and is NOT a ``REFRESH:``
    verdict line, so the parent classifier cannot mistake it for a verdict (issue #153). ``reason`` is
    the detector's own marker-free string and carries the specific cause.
    """
    print(
        f"PID {pid} entered an indeterminate window state mid-wait ({reason}); latching this "
        "observation - a later clear cannot un-see it, because the evidence a blocking dialog would "
        "leave is no longer readable. This run ends at the deadline with a distinct indeterminate "
        "verdict, never a slow-source timeout.",
        flush=True,
    )


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
    initial_unknown: str | None = None,
    initial_desktop_unready: str | None = None,
) -> bool:
    """Wait for ``worker`` while polling for a late credential dialog.

    Returns True when the worker finished before the wall-clock deadline, False when the deadline
    elapsed with the dialog state observably HEALTHY throughout. Raises immediately when the shared
    detector sees a credential modal or a blocking dialog.

    Indeterminate (``unknown_reason``) observations are LATCHED, not ignored (issue #154). The owner
    window going iconic hides its owned modal dialogs from enumeration, and - measured 2026-08-14 -
    restoring it does NOT bring them back: minimize -> restore destroys the evidence permanently, after
    which the detector reports ``none``, the SAME state as a genuinely healthy Desktop. A run that ever
    went indeterminate therefore ends at the deadline with :class:`CredentialUnknownError`, so a later
    ``none`` can never launder it into a bare timeout that blames a slow source.

    Latch-and-keep-waiting (rather than ending the instant UNKNOWN is first seen) is deliberate and is
    the false-positive-free choice: a worker that FINISHES clears the wait via ``return True`` below
    regardless of any latch, so a refresh that completes within the deadline is never overridden - the
    only run that ever surfaces the latched verdict is one that was going to hit the deadline anyway.
    Ending early instead would let a single transient enumeration hiccup, or a healthy-but-minimized
    slow refresh, cut off a run that would have succeeded.

    A ``process_gone`` observation is different: it is TERMINAL, raising :class:`DesktopGoneError`
    immediately (issue #158). Zero enumerated windows plus a confirmed-dead PID is definitive - Desktop
    has exited or crashed, there is nothing left to wait for, and the data source is not implicated -
    so unlike the latched-and-waited indeterminate case there is no value in running out the clock. It
    is not latched-but-waited because the liveness check has already removed the only false-positive it
    could have: an alive-but-window-less startup reads as ``unknown_reason`` (latched), never gone.
    """
    started = time.monotonic()
    next_heartbeat = heartbeat_seconds
    latched_unknown = initial_unknown
    latched_desktop_unready = initial_desktop_unready
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
        if state.process_gone is not None:
            raise DesktopGoneError(pid, state.process_gone)
        if state.desktop_unready and latched_desktop_unready is None:
            latched_desktop_unready = state.desktop_unready
        if state.unknown_reason and latched_unknown is None:
            latched_unknown = state.unknown_reason
            print_indeterminate_state_notice(pid, state.unknown_reason)
        if elapsed >= next_heartbeat and worker.is_alive():
            print_refresh_heartbeat(elapsed, total_timeout)
            next_heartbeat += heartbeat_seconds
    if worker.is_alive():
        state = detector(pid)
        if state.modal is not None:
            raise CredentialMissingError(pid, state.modal, source_hint)
        if state.blocking_dialog is not None:
            raise DialogBlockedError(pid, state.blocking_dialog)
        if state.process_gone is not None:
            raise DesktopGoneError(pid, state.process_gone)
        latched_desktop_unready = latched_desktop_unready or state.desktop_unready
        if latched_desktop_unready is not None:
            raise DesktopUnreadyError(pid, latched_desktop_unready)
        latched_unknown = latched_unknown or state.unknown_reason
        if latched_unknown is not None:
            raise CredentialUnknownError(pid, latched_unknown)
        return False
    return True


# pylint: enable=too-many-arguments
