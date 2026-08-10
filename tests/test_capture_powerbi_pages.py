"""Tests for the stable Power BI page capture helper."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

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


def test_capture_stable_stops_on_two_identical_frames() -> None:
    """Two equal consecutive frame bytes are convergence evidence."""
    out_dir = _workspace("capture-converges")
    dest = out_dir / "Map.png"
    frames = [b"partial-west", b"complete-nationwide", b"complete-nationwide"]
    clock = ManualClock()

    def fake_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        frame.write_bytes(frames.pop(0))
        return True

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=1.0, max_wait=10.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert result.captured
        assert result.converged
        assert result.frames == 3
        assert dest.read_bytes() == b"complete-nationwide"
        assert not (out_dir / ".Map.frames").exists()
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
            capture.CaptureOptions(poll=1.0, max_wait=2.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert result.captured
        assert not result.converged
        assert result.frames == 2
        assert dest.read_bytes() == b"frame-1"
        assert not (out_dir / ".Map.frames").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)
