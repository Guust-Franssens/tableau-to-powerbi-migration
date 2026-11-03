"""Fit a launcher's own Desktop frame to its nearest work area, identity-gated and non-activating (#805)."""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import _desktop_frame as frame_fit
from _desktop_observer import DesktopWindow, Observation

PID = 4242
HWND = 555
HANDLE = 77
START = "638963424000000010"
UNAWARE = 0x11
PER_MONITOR = -3
# NOACTIVATE | NOZORDER | NOOWNERZORDER | ASYNCWINDOWPOS, spelled as the literal Win32 bit values.
EXPECTED_FLAGS = 0x0010 | 0x0004 | 0x0200 | 0x4000
MONITORS = {
    "primary": (101, (0, 0, 2560, 1440), (48, 40, 2560, 1440)),
    "secondary": (202, (-1920, 0, 0, 1080), (-1920, 0, 0, 1040)),
}
SMALL = (100, 100, 736, 500)


class FakeApi:  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """Records every call; rectangles are physical only while per-monitor DPI awareness is set."""

    def __init__(self, *, rect=SMALL, monitor="primary", **facts) -> None:
        self.log: list[tuple] = []
        self.context = UNAWARE
        self.rect = rect
        self.monitor = monitor
        self.facts = {
            "dpi_ok": True,
            "handle": HANDLE,
            "start": START,
            "is_window": True,
            "thread_pid": PID,
            "visible": True,
            "enabled": True,
            "owner": 0,
            "iconic": False,
            "zoomed": False,
            "swp_ok": True,
        }
        self.facts.update(facts)

    def _scaled(self, rect):
        return rect if self.context == PER_MONITOR else tuple(value // 2 for value in rect)

    def names(self) -> list[str]:
        return [entry[0] for entry in self.log]

    def set_thread_dpi_awareness_context(self, context):
        self.log.append(("dpi", context))
        if not self.facts["dpi_ok"]:
            return 0
        previous, self.context = self.context, context
        return previous

    def open_process(self, pid):
        self.log.append(("open", pid))
        return self.facts["handle"]

    def process_start_ticks(self, handle, pid):
        self.log.append(("start", handle, pid))
        return self.facts["start"]

    def close_handle(self, handle):
        self.log.append(("close", handle))

    def is_window(self, hwnd):
        self.log.append(("is_window", hwnd))
        return self.facts["is_window"]

    def get_window_thread_process_id(self, hwnd):
        self.log.append(("thread_pid", hwnd))
        return self.facts["thread_pid"]

    def is_window_visible(self, hwnd):
        self.log.append(("visible", hwnd))
        return self.facts["visible"]

    def is_window_enabled(self, hwnd):
        self.log.append(("enabled", hwnd))
        return self.facts["enabled"]

    def get_owner(self, hwnd):
        self.log.append(("owner", hwnd))
        return self.facts["owner"]

    def is_iconic(self, hwnd):
        self.log.append(("iconic", hwnd))
        return self.facts["iconic"]

    def is_zoomed(self, hwnd):
        self.log.append(("zoomed", hwnd))
        return self.facts["zoomed"]

    def get_window_rect(self, hwnd):
        self.log.append(("rect", hwnd))
        return self._scaled(self.rect)

    def monitor_from_window(self, hwnd, flags):
        self.log.append(("monitor", hwnd, flags))
        # MONITOR_DEFAULTTONULL=0, DEFAULTTOPRIMARY=1, DEFAULTTONEAREST=2.
        return {0: 0, 1: MONITORS["primary"][0], 2: MONITORS[self.monitor][0]}[flags]

    def get_monitor_info(self, monitor):
        self.log.append(("monitor_info", monitor))
        for handle, full, work in MONITORS.values():
            if handle == monitor:
                return self._scaled(full), self._scaled(work)
        return None

    def set_window_pos(self, *args):  # pylint: disable=too-many-arguments
        self.log.append(("swp", *args))
        return self.facts["swp_ok"]

    def show_window(self, *_args):
        raise AssertionError("ShowWindow activates the window and is never called")


def _fit(api: FakeApi, start: str = START) -> frame_fit.FrameFit:
    return frame_fit.fit_owned_frame(HWND, PID, start, api=api)


@pytest.mark.parametrize(
    "monitor,rect,target",
    [
        ("primary", SMALL, (48, 40, 2512, 1400)),
        ("secondary", (-1800, 100, -1164, 500), (-1920, 0, 1920, 1040)),
    ],
)
def test_one_non_activating_call_fills_the_nearest_monitor_work_area(monitor: str, rect, target) -> None:
    api = FakeApi(rect=rect, monitor=monitor)

    result = _fit(api)

    swps = [entry for entry in api.log if entry[0] == "swp"]
    assert swps == [("swp", HWND, 0, *target, EXPECTED_FLAGS)]
    assert ("monitor", HWND, 2) in api.log
    assert result == frame_fit.FrameFit("requested", "work_area", rect, rect, MONITORS[monitor][2])


REFUSALS = {
    "dpi_unavailable": ({"dpi_ok": False}, "refused", "physical_geometry_unavailable"),
    "open_denied": ({"handle": 0}, "refused", "process_identity_changed"),
    "start_differs": ({"start": "638963424000000020"}, "refused", "process_identity_changed"),
    "exited": ({"start": None}, "refused", "process_identity_changed"),
    "not_window": ({"is_window": False}, "refused", "root_window_not_actionable"),
    "other_pid": ({"thread_pid": PID + 4}, "refused", "root_window_not_actionable"),
    "hidden": ({"visible": False}, "refused", "root_window_not_actionable"),
    "disabled": ({"enabled": False}, "refused", "root_window_not_actionable"),
    "owned": ({"owner": 999}, "refused", "root_window_not_actionable"),
    "minimized": ({"iconic": True}, "skipped", "minimized"),
    "maximized": ({"zoomed": True}, "skipped", "maximized"),
    "already_fits": ({"rect": MONITORS["primary"][2]}, "skipped", "already_fits"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_every_refusal_and_skip_makes_no_call_and_restores_dpi(case: str) -> None:
    facts, outcome, reason = REFUSALS[case]
    facts = dict(facts)
    api = FakeApi(rect=facts.pop("rect", SMALL), **facts)

    result = _fit(api)

    assert (result.outcome, result.reason) == (outcome, reason)
    assert "swp" not in api.names()
    if case != "dpi_unavailable":
        assert api.log[0] == ("dpi", PER_MONITOR)
        assert api.log[-1] == ("dpi", UNAWARE), "the previous DPI context was not restored"
        assert api.context == UNAWARE
    if case not in {"dpi_unavailable", "open_denied"}:
        assert api.names().count("close") == 1 and ("close", HANDLE) in api.log, "process handle leaked"


def test_dpi_awareness_precedes_every_geometry_read_and_is_restored_last() -> None:
    api = FakeApi()

    _fit(api)

    names = api.names()
    first_geometry = min(names.index("rect"), names.index("monitor_info"))
    assert names.index("dpi") == 0 < first_geometry
    assert api.log[-1] == ("dpi", UNAWARE)


def test_identity_is_proven_before_the_call_and_the_handle_outlives_it() -> None:
    api = FakeApi()

    _fit(api)

    names = api.names()
    assert names.index("open") < names.index("start") < names.index("swp") < names.index("close")
    assert api.log[names.index("start")] == ("start", HANDLE, PID)
    assert names.count("close") == 1


def test_an_unchanged_rect_after_the_call_is_still_only_requested_once() -> None:
    api = FakeApi()

    result = _fit(api)

    assert result.outcome == "requested" and result.after == result.before == SMALL
    assert api.names().count("swp") == 1


def test_a_failed_call_is_refused_without_retry() -> None:
    api = FakeApi(swp_ok=False)

    result = _fit(api)

    assert (result.outcome, result.reason) == ("refused", "set_window_pos_failed")
    assert api.names().count("swp") == 1


# Launch-record arithmetic: 1793331708137 ms after the epoch, plus 0.6 ms that the floor must drop.
LAUNCH_MS = 1793331708137
OBSERVED_TICKS = str(frame_fit.TICKS_AT_UNIX_EPOCH + LAUNCH_MS * 10000 + 6000)


class Launch:
    """A fake observer plus a fake fit, recording calls."""

    def __init__(self, windows=None, *, start: str | None = OBSERVED_TICKS) -> None:
        if windows is None:
            windows = (DesktopWindow("Power BI", "frame", 636, 400, hwnd=HWND, pid=str(PID), process_started_at=start),)
        self.state = Observation(pid=str(PID), process_started_at=start, windows=tuple(windows))
        self.observed: list[tuple] = []
        self.fitted: list[tuple] = []

    def observe(self, pid, **kwargs):
        self.observed.append((pid, kwargs))
        return self.state

    def fit(self, *args):
        self.fitted.append(args)
        return frame_fit.FrameFit("requested", "work_area")

    def run(self, record: str) -> frame_fit.FrameFit:
        return frame_fit.fit_launched_frame(PID, record, observe=self.observe, fit=self.fit)


def test_launcher_fits_when_the_record_is_the_floored_observer_start() -> None:
    launch = Launch()

    result = launch.run(f"/Date({LAUNCH_MS})/")

    assert result.outcome == "requested"
    assert launch.observed == [(PID, {"timeout_s": 8})]
    assert launch.fitted == [(HWND, PID, OBSERVED_TICKS)]


@pytest.mark.parametrize("record", [f"/Date({LAUNCH_MS + 1})/", f"/Date({LAUNCH_MS - 1})/"])
def test_launcher_start_mismatch_never_fits(record: str) -> None:
    launch = Launch()

    assert launch.run(record).reason == "start_mismatch"
    assert not launch.fitted


@pytest.mark.parametrize(
    "record",
    [
        "",
        "2026-11-03T21:51:28.392+01:00",
        "/Date(-1)/",
        "\\/Date(1793331708137)\\/",
        "/Date(1793331708137)/x",
        " /Date(1793331708137)/",
        "/Date(1793331708137+0100)/",
        "/Date(\u0661\u0662)/",
    ],
)
def test_unproven_launch_record_is_refused_before_any_observation(record: str) -> None:
    launch = Launch()

    assert launch.run(record).reason == "start_unproven"
    assert not launch.observed and not launch.fitted


@pytest.mark.parametrize("case", ["other_pid", "no_frame", "no_start", "frame_start_differs", "no_hwnd"])
def test_launcher_refuses_a_root_it_cannot_tie_to_its_pid(case: str) -> None:
    launch = {
        "other_pid": lambda: Launch([DesktopWindow("x", "frame", 636, 400, hwnd=HWND, pid="999")]),
        "no_frame": lambda: Launch([]),
        "no_start": lambda: Launch(start=None),
        "frame_start_differs": lambda: Launch(
            [DesktopWindow("x", "frame", 636, 400, hwnd=HWND, pid=str(PID), process_started_at="1")]
        ),
        "no_hwnd": lambda: Launch([DesktopWindow("x", "frame", 636, 400, hwnd=0, pid=str(PID))]),
    }[case]()

    assert launch.run(f"/Date({LAUNCH_MS})/").reason == "root_unresolved"
    assert not launch.fitted


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 process identity is Windows-only")
def test_real_bindings_match_the_cim_launch_record_and_refuse_a_non_window() -> None:
    """No window is created or moved: the real calls run against this test process and HWND 0."""
    pid = os.getpid()
    api = frame_fit.Win32Api()
    handle = api.open_process(pid)
    try:
        start = api.process_start_ticks(handle, pid)
        aliased = api.process_start_ticks(handle, pid + 1)
    finally:
        api.close_handle(handle)
    # The same CIM + Windows PowerShell 5.1 ConvertTo-Json path that check_desktop_orphans records.
    script = (
        f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | "
        "Select-Object CreationDate | ConvertTo-Json -Compress"
    )
    record = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, check=True, timeout=60
    )
    creation = json.loads(record.stdout)["CreationDate"]

    assert start is not None and int(start) % 10 == 0
    assert aliased is None
    assert frame_fit.launch_record_ms(creation) == frame_fit.ticks_to_epoch_ms(start)
    assert frame_fit.fit_owned_frame(0, pid, start).reason == "root_window_not_actionable"
    assert frame_fit.fit_owned_frame(0, pid, str(int(start) + 10)).reason == "process_identity_changed"
