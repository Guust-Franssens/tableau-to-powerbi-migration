#!/usr/bin/env python
"""
purpose: Capture every Power BI report page, waiting until each page's render has actually stabilised.
usage:   python scripts/capture_powerbi_pages.py <report.Report> <output-dir> [--pid PID]
                                                [--poll 4] [--max-wait 75]

Why this exists - and why the obvious version is wrong
------------------------------------------------------
An azureMap draws progressively and asynchronously: model query -> basemap tiles -> remote reference
layer GeoJSON -> marks, with the marks themselves filling in over time. A capture taken too early is
not blank-or-correct, it is PARTIALLY DRAWN - which is far more dangerous, because it looks like a
finished map and silently under-reports the mark count.

Measured on ``Combined Map`` (604 city pies), same report, same warm Desktop:
    captured immediately after navigating       411 distinct colours   pies only in the W/central US
    captured after the render settled        41,185 distinct colours   pies nationwide, incl. NE

Both look like plausible maps. Only the second is real. This is the "it rendered" failure mode all
over again, so the capture step itself needs evidence, not a guess.

Three things that do not work
-----------------------------
1. ``screenshot-all --settle <ms>`` - the flag exists but delays only before the FIRST capture, not
   between pages. Measured: ``--settle 5000`` over 10 pages cost 38 s, not the ~76 s a per-page delay
   would cost. It covers the post-``reload`` cold start (worth using) and nothing else.
2. ``sleep(n)`` then screenshot - the trap the first version of this file fell into. The sleep happens
   while sitting on the PREVIOUS page; the screenshot verb then navigates and captures almost
   immediately, so the page being captured gets no settle at all. It produced confident, plausible,
   PARTIAL maps.
3. A single long fixed sleep - unreliable in both directions: wasteful on a cached page, still too
   short on a cold GeoJSON fetch.

What works
----------
Capture repeatedly and compare consecutive frames. When two successive captures of the same page are
byte-identical, the render has converged. That is a measurement of stability rather than an assumption
about timing, so it self-tunes to a cold or warm Desktop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Screenshotter = Callable[[str, str, Path], bool]


@dataclass(frozen=True)
class CaptureResult:
    """Outcome for one page capture."""

    captured: bool
    converged: bool
    seconds: float
    frames: int


@dataclass(frozen=True)
class CaptureOptions:
    """Timing options for one page capture."""

    poll: float
    max_wait: float


@dataclass(frozen=True)
class CaptureRuntime:
    """Injectable runtime hooks for tests."""

    screenshotter: Screenshotter
    sleep: Callable[[float], None]
    clock: Callable[[], float]


def pages(report: Path) -> list[tuple[str, str]]:
    """Return (page-id, displayName) for every page, resolved semantically - never by folder order."""
    page_root = report / "definition" / "pages"
    output = []
    for page_json in sorted(page_root.glob("*/page.json")):
        doc = json.loads(page_json.read_text(encoding="utf-8"))
        output.append((page_json.parent.name, doc.get("displayName", page_json.parent.name)))
    return output


def screenshot(page_id: str, pid: str, dest: Path) -> bool:
    """Capture one report page through the Desktop bridge."""
    proc = subprocess.run(
        [
            "powerbi-desktop",
            "screenshot",
            page_id,
            "--pid",
            pid,
            "--output",
            str(dest),
            "--wait-seconds",
            "90",
        ],
        capture_output=True,
        text=True,
        shell=True,
        check=False,
    )
    return proc.returncode == 0 and dest.exists()


DEFAULT_RUNTIME = CaptureRuntime(screenshot, time.sleep, time.time)


def frame_digest(path: Path) -> str:
    """Return a content digest for a captured frame."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_stable(
    page_id: str,
    pid: str,
    dest: Path,
    options: CaptureOptions,
    runtime: CaptureRuntime = DEFAULT_RUNTIME,
) -> CaptureResult:
    """Screenshot until two consecutive frames match, copying the converged/newest frame to `dest`."""
    scratch = dest.parent / f".{dest.stem}.frames"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    started = runtime.clock()
    previous_digest: str | None = None
    frames = 0
    newest_frame: Path | None = None
    try:
        while runtime.clock() - started < options.max_wait:
            frame = scratch / f"{frames}.png"
            frames += 1
            if not runtime.screenshotter(page_id, pid, frame):
                return CaptureResult(False, False, runtime.clock() - started, frames)
            newest_frame = frame
            digest = frame_digest(frame)
            if digest == previous_digest:
                shutil.copyfile(frame, dest)
                return CaptureResult(True, True, runtime.clock() - started, frames)
            previous_digest = digest
            runtime.sleep(options.poll)

        if newest_frame is not None:
            shutil.copyfile(newest_frame, dest)
            return CaptureResult(True, False, runtime.clock() - started, frames)
        return CaptureResult(False, False, runtime.clock() - started, frames)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _safe_filename(name: str) -> str:
    """Return a readable filename stem for a report page display name."""
    return "".join(char if char not in '<>:"/\\|?*' else "_" for char in name).strip() or "page"


def capture_report(report: Path, out_dir: Path, pid: str, poll: float, max_wait: float) -> int:
    """Capture every page in `report`; return a process exit code."""
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    unstable: list[str] = []
    failed: list[str] = []
    report_pages = pages(report)
    options = CaptureOptions(poll=poll, max_wait=max_wait)

    for page_id, name in report_pages:
        result = capture_stable(page_id, pid, out_dir / f"{_safe_filename(name)}.png", options)
        tag = "OK" if result.captured and result.converged else ("UNSTABLE" if result.captured else "FAIL")
        print(
            f"  {tag:<9}{name:<26} settled in {result.seconds:5.1f}s over {result.frames} frames "
            f"({time.time() - started:6.1f}s total)",
            flush=True,
        )
        if not result.captured:
            failed.append(name)
        elif not result.converged:
            unstable.append(name)

    print(f"\n{len(report_pages) - len(failed)}/{len(report_pages)} captured in {time.time() - started:.1f}s")
    if unstable:
        print("NEVER CONVERGED (still changing at max-wait, treat as PARTIAL): " + ", ".join(unstable))
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed or unstable else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path, help="Path to a .Report folder")
    parser.add_argument("outdir", type=Path, help="Folder where page PNGs should be written")
    parser.add_argument("--pid", required=True, help="Power BI Desktop PID to capture from")
    parser.add_argument("--poll", type=float, default=4.0, help="Seconds between frames for one page")
    parser.add_argument("--max-wait", type=float, default=75.0, help="Max seconds to wait for one page")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return capture_report(args.report, args.outdir, args.pid, args.poll, args.max_wait)


if __name__ == "__main__":
    sys.exit(main())
