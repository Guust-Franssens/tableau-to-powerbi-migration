"""Tests for the stable Power BI page capture helper."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
import capture_powerbi_pages as capture


class ManualClock:
    """Tiny controllable clock for convergence tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _workspace(name: str) -> Path:
    root = REPO_ROOT / ".test-work" / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root


def test_capture_stable_ignores_a_partial_plateau_until_final_frame_dwells() -> None:
    """One unchanged poll can be a partial-render plateau, not convergence."""
    out_dir = _workspace("capture-converges")
    dest = out_dir / "Map.png"
    frames = [
        b"partial-west",
        b"partial-west",
        b"complete-nationwide",
        b"complete-nationwide",
        b"complete-nationwide",
    ]
    clock = ManualClock()

    def fake_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        frame.write_bytes(frames.pop(0))
        return True

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=1.0, stable_seconds=2.0, max_wait=10.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert result.captured
        assert result.converged
        assert result.frames == 5
        assert dest.read_bytes() == b"complete-nationwide"
        assert not (out_dir / ".Map.frames").exists()
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_stable_does_not_count_screenshot_duration_as_dwell() -> None:
    """A slow screenshot call must not turn one unchanged poll into convergence."""
    out_dir = _workspace("capture-slow-screenshot")
    dest = out_dir / "Map.png"
    frames = [
        b"partial-west",
        b"partial-west",
        b"complete-nationwide",
        b"complete-nationwide",
        b"complete-nationwide",
    ]
    clock = ManualClock()

    def fake_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        if len(frames) == 5:
            clock.sleep(5.0)
        frame.write_bytes(frames.pop(0))
        return True

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=4.0, stable_seconds=8.0, max_wait=30.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert result.captured
        assert result.converged
        assert result.frames == 5
        assert dest.read_bytes() == b"complete-nationwide"
        assert not (out_dir / ".Map.frames").exists()
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_stable_flags_newest_frame_when_page_never_converges() -> None:
    """A page still changing at max-wait is kept for inspection but fails the gate."""
    out_dir = _workspace("capture-unstable")
    dest = out_dir / "Map.png"
    frame_number = 0
    clock = ManualClock()

    def fake_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        nonlocal frame_number
        frame.write_bytes(f"frame-{frame_number}".encode("utf-8"))
        frame_number += 1
        return True

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=1.0, stable_seconds=2.0, max_wait=2.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert result.captured
        assert not result.converged
        assert result.frames == 2
        assert dest.read_bytes() == b"frame-1"
        assert not (out_dir / ".Map.frames").exists()
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_stable_discards_partial_frames_after_a_later_failure() -> None:
    """A failed page never promotes a plausible partial frame to the evidence path."""
    out_dir = _workspace("capture-later-failure")
    dest = out_dir / "Map.png"
    calls = 0

    def flaky_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            frame.write_bytes(f"PARTIAL-FRAME-{calls}".encode("utf-8"))
            return True
        return False

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=0.0, stable_seconds=20.0, max_wait=75.0),
            capture.CaptureRuntime(flaky_screenshot, lambda _seconds: None, lambda: 0.0),
        )

        assert result == capture.CaptureResult(captured=False, converged=False, seconds=0.0, frames=3)
        assert not dest.exists()
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_stable_discards_a_partial_file_from_the_first_failed_screenshot() -> None:
    """A bridge error after writing a truncated image cannot leave output evidence."""
    out_dir = _workspace("capture-first-failure")
    dest = out_dir / "Map.png"

    def partial_then_fail(_page_id: str, _pid: str, frame: Path) -> bool:
        frame.write_bytes(b"TRUNCATED-FRAME")
        return False

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=0.0, stable_seconds=20.0, max_wait=75.0),
            capture.CaptureRuntime(partial_then_fail, lambda _seconds: None, lambda: 0.0),
        )

        assert result == capture.CaptureResult(captured=False, converged=False, seconds=0.0, frames=1)
        assert not dest.exists()
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_stable_preserves_prior_evidence_when_a_new_capture_fails() -> None:
    """A failed recapture cannot delete a previously settled output PNG."""
    out_dir = _workspace("capture-preserves-prior-evidence")
    dest = out_dir / "Map.png"
    dest.write_bytes(b"PREVIOUSLY-SETTLED")

    def partial_then_fail(_page_id: str, _pid: str, frame: Path) -> bool:
        frame.write_bytes(b"TRUNCATED-FRAME")
        return False

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=0.0, stable_seconds=20.0, max_wait=75.0),
            capture.CaptureRuntime(partial_then_fail, lambda _seconds: None, lambda: 0.0),
        )

        assert not result.captured
        assert dest.read_bytes() == b"PREVIOUSLY-SETTLED"
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_report_exits_nonzero_when_any_page_is_unstable() -> None:
    """The process-level gate must fail when a page capture never converges."""
    root = _workspace("capture-report-unstable")
    report = root / "Book.Report"
    page = report / "definition" / "pages" / "ReportSection1"
    page.mkdir(parents=True)
    (page / "page.json").write_text('{"displayName": "Map"}', encoding="utf-8")
    out_dir = root / "out"
    clock = ManualClock()
    frame_number = 0

    def fake_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        nonlocal frame_number
        frame.write_bytes(f"frame-{frame_number}".encode("utf-8"))
        frame_number += 1
        return True

    try:
        code = capture.capture_report(
            report,
            out_dir,
            "1234",
            capture.CaptureOptions(poll=1.0, stable_seconds=2.0, max_wait=2.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert code == 1
        assert (out_dir / "Map.png").read_bytes() == b"frame-1"
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_capture_report_limits_capture_to_requested_page_ids() -> None:
    """The --pages selection captures only the exact PBIR page folder IDs."""
    root = _workspace("capture-report-selected-page")
    report = root / "Book.Report"
    for page_id, display_name in (
        ("ReportSectionOverview", "Overview"),
        ("ReportSectionMap", "Map"),
    ):
        page = report / "definition" / "pages" / page_id
        page.mkdir(parents=True)
        (page / "page.json").write_text(f'{{"displayName": "{display_name}"}}', encoding="utf-8")
    captured_page_ids: list[str] = []
    clock = ManualClock()

    def fake_screenshot(page_id: str, _pid: str, frame: Path) -> bool:
        captured_page_ids.append(page_id)
        frame.write_bytes(b"settled")
        return True

    try:
        code = capture.capture_report(
            report,
            root / "out",
            "1234",
            capture.CaptureOptions(
                poll=1.0,
                stable_seconds=0.0,
                max_wait=2.0,
                page_ids=frozenset({"ReportSectionMap"}),
            ),
            capture.CaptureRuntime(
                screenshotter=fake_screenshot,
                sleep=clock.sleep,
                clock=clock,
            ),
        )

        assert code == 0
        assert captured_page_ids == ["ReportSectionMap"]
        assert (root / "out" / "Map.png").read_bytes() == b"settled"
        assert not (root / "out" / "Overview.png").exists()
        assert not list((root / "out").glob(".*.frames"))
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_parse_args_accepts_comma_separated_page_ids() -> None:
    """The CLI exposes the exact page-ID filter to callers."""
    args = capture.parse_args(
        [
            "Book.Report",
            "out",
            "--pid",
            "1234",
            "--pages",
            "ReportSectionOverview, ReportSectionMap",
        ]
    )

    assert args.pages == frozenset({"ReportSectionOverview", "ReportSectionMap"})


def test_capture_report_rejects_unknown_page_id_before_capturing(capsys: pytest.CaptureFixture[str]) -> None:
    """A misspelled PBIR page ID is an argument error, never a silent empty success."""
    root = _workspace("capture-report-unknown-page")
    report = root / "Book.Report"
    page = report / "definition" / "pages" / "ReportSectionMap"
    page.mkdir(parents=True)
    (page / "page.json").write_text('{"displayName": "Map"}', encoding="utf-8")
    screenshot_called = False

    def fake_screenshot(_page_id: str, _pid: str, _frame: Path) -> bool:
        nonlocal screenshot_called
        screenshot_called = True
        return True

    try:
        code = capture.capture_report(
            report,
            root / "out",
            "1234",
            capture.CaptureOptions(
                poll=1.0,
                stable_seconds=0.0,
                max_wait=2.0,
                page_ids=frozenset({"Map"}),
            ),
            capture.CaptureRuntime(
                screenshotter=fake_screenshot,
                sleep=ManualClock().sleep,
                clock=ManualClock(),
            ),
        )

        assert code == 2
        assert not screenshot_called
        assert "requested page id(s) not found: Map" in capsys.readouterr().out
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_capture_report_full_sweep_captures_every_page() -> None:
    """Omitting --pages retains the ordinary full-sweep page traversal."""
    root = _workspace("capture-report-full-sweep")
    report = root / "Book.Report"
    for page_id, display_name in (
        ("ReportSectionOverview", "Overview"),
        ("ReportSectionMap", "Map"),
    ):
        page = report / "definition" / "pages" / page_id
        page.mkdir(parents=True)
        (page / "page.json").write_text(f'{{"displayName": "{display_name}"}}', encoding="utf-8")
    captured_page_ids: list[str] = []
    clock = ManualClock()

    def fake_screenshot(page_id: str, _pid: str, frame: Path) -> bool:
        captured_page_ids.append(page_id)
        frame.write_bytes(page_id.encode("utf-8"))
        return True

    try:
        code = capture.capture_report(
            report,
            root / "out",
            "1234",
            capture.CaptureOptions(poll=1.0, stable_seconds=0.0, max_wait=2.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert code == 0
        assert captured_page_ids == ["ReportSectionMap", "ReportSectionOverview"]
        assert (root / "out" / "Map.png").read_bytes() == b"ReportSectionMap"
        assert (root / "out" / "Overview.png").read_bytes() == b"ReportSectionOverview"
        assert not list((root / "out").glob(".*.frames"))
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_screenshot_timeout_treats_a_hung_bridge_call_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Python timeout caps a bridge subprocess that does not return itself."""
    observed_timeout: int | None = None

    def fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal observed_timeout
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, int)
        observed_timeout = timeout
        raise subprocess.TimeoutExpired("powerbi-desktop", timeout)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)

    assert not capture.screenshot("ReportSectionMap", "1234", Path("unused.png"))
    assert observed_timeout == capture.SCREENSHOT_TIMEOUT_SECONDS
    assert observed_timeout > capture.BRIDGE_WAIT_SECONDS


def test_capture_report_exits_nonzero_when_report_has_no_pages() -> None:
    """A typo or invalid .Report path must fail closed, not report 0/0 success."""
    root = _workspace("capture-report-empty")
    report = root / "Missing.Report"

    try:
        code = capture.capture_report(
            report,
            root / "out",
            "1234",
            capture.CaptureOptions(poll=1.0, stable_seconds=2.0, max_wait=2.0),
        )

        assert code == 1
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)
