"""purpose: fit a launcher's own Power BI Desktop frame to its nearest monitor's work area (#805).
usage:   from _desktop_frame import fit_launched_frame; a launcher calls it once after resolving its PID.
internal: true
internal-reason: Launcher library with no CLI; `open_desktop.py` calls it once Desktop is ready.

A freshly launched Desktop can open at about 636x400, where the Home ribbon collapses and no exact-name
Refresh control exists. Only the launcher knows it owns the instance, so only the launcher may fit it:
one SetWindowPos, non-activating, no z-order change, identity-gated, never ShowWindow, never restored.
The outcome ``requested`` names what was asked; SWP_ASYNCWINDOWPOS means Desktop applies it later.
"""

from __future__ import annotations

import ctypes
import re
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

from _desktop_observer import (
    PROCESS_QUERY_LIMITED_INFORMATION_SYNCHRONIZE,
    configure_kernel32,
    held_process_start,
    main_frame,
    observe,
)

# .NET ticks (100 ns) at 1970-01-01 UTC.
TICKS_AT_UNIX_EPOCH = 621355968000000000
TICKS_PER_MS = 10000
# Windows PowerShell 5.1 ConvertTo-Json of Win32_Process.CreationDate, as check_desktop_orphans stores it.
_LAUNCH_RECORD = re.compile(r"/Date\(([0-9]{1,19})\)/")

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
GW_OWNER = 4
MONITOR_DEFAULTTONEAREST = 2
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
SWP_ASYNCWINDOWPOS = 0x4000
FIT_FLAGS = SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER | SWP_ASYNCWINDOWPOS

Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class FrameFit:
    """One fit attempt: ``requested`` (asked, not verified), ``skipped`` or ``refused``, never fatal."""

    outcome: str
    reason: str
    before: Rect | None = None
    after: Rect | None = None
    work_area: Rect | None = None

    def describe(self) -> str:
        """The launcher's single log line."""
        return f"frame: {self.outcome} {self.reason} {self.before} -> {self.after} (work area {self.work_area})"


def launch_record_ms(creation_time: str) -> int | None:
    """Epoch milliseconds from a ``/Date(<ms>)/`` launch record; anything else is unproven."""
    match = _LAUNCH_RECORD.fullmatch(creation_time or "")
    return int(match.group(1)) if match else None


def ticks_to_epoch_ms(ticks: str) -> int:
    """Floor, not round: CIM's /Date(ms)/ truncates, and so must this side of the comparison."""
    return (int(ticks) - TICKS_AT_UNIX_EPOCH) // TICKS_PER_MS


def fit_owned_frame(hwnd: int, pid: int, expected_start: str, *, api=None) -> FrameFit:
    """One bounded fit of an owned, visible, restored root frame to its nearest work area."""
    if api is None:
        if sys.platform != "win32":
            return FrameFit("refused", "physical_geometry_unavailable")
        api = Win32Api()
    previous = api.set_thread_dpi_awareness_context(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE)
    if not previous:
        return FrameFit("refused", "physical_geometry_unavailable")
    try:
        handle = api.open_process(pid)
        if not handle:
            return FrameFit("refused", "process_identity_changed")
        try:
            # The held handle pins the PID until after SetWindowPos returns.
            return _fit_held(api, handle, hwnd, pid, expected_start)
        finally:
            api.close_handle(handle)
    finally:
        api.set_thread_dpi_awareness_context(previous)


def _fit_held(api, handle, hwnd: int, pid: int, expected_start: str) -> FrameFit:
    # pylint: disable=too-many-return-statements  # one guard per fail-closed fact, in order
    if api.process_start_ticks(handle, pid) != expected_start:
        return FrameFit("refused", "process_identity_changed")
    if not (
        api.is_window(hwnd)
        and api.get_window_thread_process_id(hwnd) == pid
        and api.is_window_visible(hwnd)
        and api.is_window_enabled(hwnd)
        and api.get_owner(hwnd) == 0
    ):
        return FrameFit("refused", "root_window_not_actionable")
    if api.is_iconic(hwnd):
        return FrameFit("skipped", "minimized")
    if api.is_zoomed(hwnd):
        return FrameFit("skipped", "maximized")
    before = api.get_window_rect(hwnd)
    info = api.get_monitor_info(api.monitor_from_window(hwnd, MONITOR_DEFAULTTONEAREST))
    if before is None or info is None:
        return FrameFit("refused", "physical_geometry_unavailable", before)
    work = info[1]
    if before == work:
        return FrameFit("skipped", "already_fits", before, before, work)
    left, top, right, bottom = work
    if not api.set_window_pos(hwnd, 0, left, top, right - left, bottom - top, FIT_FLAGS):
        return FrameFit("refused", "set_window_pos_failed", before, None, work)
    return FrameFit("requested", "work_area", before, api.get_window_rect(hwnd), work)


def fit_launched_frame(
    pid: int,
    launch_creation_time: str,
    *,
    observe: Callable = observe,  # pylint: disable=redefined-outer-name
    fit: Callable = fit_owned_frame,
) -> FrameFit:
    """Fit the frame of the Desktop this launcher started, only if it is still that process."""
    launch_ms = launch_record_ms(launch_creation_time)
    if launch_ms is None:
        return FrameFit("refused", "start_unproven")
    state = observe(pid, timeout_s=8)
    frame = state.target_window or main_frame(state.windows)
    start = state.process_started_at
    if frame is None or not isinstance(frame.hwnd, int) or frame.hwnd <= 0 or frame.pid != str(pid):
        return FrameFit("refused", "root_unresolved")
    if start is None or frame.process_started_at not in {None, start}:
        return FrameFit("refused", "root_unresolved")
    # Exact for the same process: the observer reads GetProcessTimes as UTC ticks (truncated to
    # microseconds) and the launch record is CIM CreationDate as truncated UTC epoch ms (#805).
    if launch_ms != ticks_to_epoch_ms(start):
        return FrameFit("refused", "start_mismatch")
    return fit(frame.hwnd, pid, start)


class _MonitorInfo(ctypes.Structure):  # pylint: disable=too-few-public-methods
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", ctypes.c_long * 4),
        ("rcWork", ctypes.c_long * 4),
        ("dwFlags", ctypes.c_uint32),
    ]


class Win32Api:
    """The ctypes calls ``fit_owned_frame`` makes; tests substitute a recording fake."""

    def __init__(self) -> None:
        self.user32 = user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hwnd, rect = wintypes.HWND, ctypes.POINTER(wintypes.RECT)
        signatures = (
            (user32.SetThreadDpiAwarenessContext, [ctypes.c_void_p], ctypes.c_void_p),
            (user32.IsWindow, [hwnd], wintypes.BOOL),
            (user32.GetWindowThreadProcessId, [hwnd, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (user32.IsWindowVisible, [hwnd], wintypes.BOOL),
            (user32.IsWindowEnabled, [hwnd], wintypes.BOOL),
            (user32.GetWindow, [hwnd, wintypes.UINT], hwnd),
            (user32.IsIconic, [hwnd], wintypes.BOOL),
            (user32.IsZoomed, [hwnd], wintypes.BOOL),
            (user32.GetWindowRect, [hwnd, rect], wintypes.BOOL),
            (user32.MonitorFromWindow, [hwnd, wintypes.DWORD], wintypes.HMONITOR),
            (user32.GetMonitorInfoW, [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)], wintypes.BOOL),
            (user32.SetWindowPos, [hwnd, hwnd] + [ctypes.c_int] * 4 + [wintypes.UINT], wintypes.BOOL),
        )
        for function, argtypes, restype in signatures:
            function.argtypes, function.restype = argtypes, restype
        configure_kernel32(kernel32)

    def set_thread_dpi_awareness_context(self, context: int) -> int:
        """Previous context, or 0 when the call failed."""
        return self.user32.SetThreadDpiAwarenessContext(context) or 0

    def open_process(self, pid: int) -> int:
        """A query-only handle, or 0."""
        return self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION_SYNCHRONIZE, False, pid) or 0

    def process_start_ticks(self, handle: int, pid: int) -> str | None:
        """The observer's start token for a held handle; None when exited, aliased or unreadable."""
        try:
            return held_process_start(self.kernel32, handle, pid)
        except OSError:
            return None

    def close_handle(self, handle: int) -> None:
        """Release the process handle."""
        self.kernel32.CloseHandle(handle)

    def is_window(self, hwnd: int) -> bool:
        """IsWindow."""
        return bool(self.user32.IsWindow(hwnd))

    def get_window_thread_process_id(self, hwnd: int) -> int:
        """Owning PID, or 0."""
        pid = ctypes.c_ulong()
        return pid.value if self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) else 0

    def is_window_visible(self, hwnd: int) -> bool:
        """IsWindowVisible."""
        return bool(self.user32.IsWindowVisible(hwnd))

    def is_window_enabled(self, hwnd: int) -> bool:
        """IsWindowEnabled."""
        return bool(self.user32.IsWindowEnabled(hwnd))

    def get_owner(self, hwnd: int) -> int:
        """GetWindow(GW_OWNER) as an int, 0 for none."""
        return self.user32.GetWindow(hwnd, GW_OWNER) or 0

    def is_iconic(self, hwnd: int) -> bool:
        """IsIconic."""
        return bool(self.user32.IsIconic(hwnd))

    def is_zoomed(self, hwnd: int) -> bool:
        """IsZoomed."""
        return bool(self.user32.IsZoomed(hwnd))

    def get_window_rect(self, hwnd: int) -> Rect | None:
        """Physical window rectangle while per-monitor aware, or None."""
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def monitor_from_window(self, hwnd: int, flags: int) -> int:
        """HMONITOR for ``flags``, or 0."""
        return self.user32.MonitorFromWindow(hwnd, flags) or 0

    def get_monitor_info(self, monitor: int) -> tuple[Rect, Rect] | None:
        """(rcMonitor, rcWork), or None."""
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        if not monitor or not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        return tuple(info.rcMonitor), tuple(info.rcWork)

    def set_window_pos(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, hwnd: int, insert_after: int, x: int, y: int, cx: int, cy: int, flags: int
    ) -> bool:
        """SetWindowPos."""
        return bool(self.user32.SetWindowPos(hwnd, insert_after, x, y, cx, cy, flags))
