#!/usr/bin/env python
"""
purpose: Capture every Power BI report page, waiting until each page's render has actually stabilised.
usage:   python scripts/capture_powerbi_pages.py <report.Report> <output-dir> [--pid PID]
                                                [--pages <id>[,<id>...]] [--poll 4]
                                                [--stable-seconds 20] [--max-wait 75]

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
Capture repeatedly and compare frames across a minimum stable dwell. This is the best available
heuristic (bridge CLI 0.1.2 exposes no render-readiness signal), not a proof: a partial plateau longer
than ``--stable-seconds`` can still pass. The dwell clock excludes the blocking screenshot call itself,
so a slow capture cannot collapse the check back to one unchanged polling interval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Screenshotter = Callable[[str, str, Path], bool]
BRIDGE_WAIT_SECONDS = 90
SCREENSHOT_TIMEOUT_SECONDS = BRIDGE_WAIT_SECONDS + 30


@dataclass(frozen=True)
class CaptureResult:
    """Outcome for one page capture."""

    captured: bool
    converged: bool
    seconds: float
    frames: int


@dataclass(frozen=True)
class CaptureOptions:
    """Capture options for each selected report page."""

    poll: float
    stable_seconds: float
    max_wait: float
    page_ids: frozenset[str] | None = None


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
    try:
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
                str(BRIDGE_WAIT_SECONDS),
            ],
            capture_output=True,
            text=True,
            shell=True,
            check=False,
            timeout=SCREENSHOT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
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
    """Screenshot until one frame digest remains stable for the configured dwell."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    started = runtime.clock()
    stable_digest: str | None = None
    stable_idle_seconds = 0.0
    frames = 0
    captured_frame = False
    previous_frame_finished = started
    while runtime.clock() - started < options.max_wait:
        capture_started = runtime.clock()
        frames += 1
        if not runtime.screenshotter(page_id, pid, dest):
            return CaptureResult(False, False, runtime.clock() - started, frames)
        captured_frame = True
        digest = frame_digest(dest)
        if digest != stable_digest:
            stable_digest = digest
            stable_idle_seconds = 0.0
        else:
            stable_idle_seconds += max(0.0, capture_started - previous_frame_finished)
        previous_frame_finished = runtime.clock()
        if stable_idle_seconds >= options.stable_seconds:
            return CaptureResult(True, True, runtime.clock() - started, frames)
        runtime.sleep(options.poll)

    if captured_frame:
        return CaptureResult(True, False, runtime.clock() - started, frames)
    return CaptureResult(False, False, runtime.clock() - started, frames)


def _safe_filename(name: str) -> str:
    """Return a readable filename stem for a report page display name."""
    return "".join(char if char not in '<>:"/\\|?*' else "_" for char in name).strip() or "page"


def _selected_pages(
    report_pages: list[tuple[str, str]], requested_page_ids: frozenset[str] | None
) -> list[tuple[str, str]]:
    """Return requested page IDs or raise when an exact ID is absent."""
    if requested_page_ids is None:
        return report_pages
    available_page_ids = {page_id for page_id, _ in report_pages}
    missing_page_ids = sorted(requested_page_ids - available_page_ids)
    if missing_page_ids:
        raise ValueError(", ".join(missing_page_ids))
    return [(page_id, name) for page_id, name in report_pages if page_id in requested_page_ids]


def capture_report(
    report: Path,
    out_dir: Path,
    pid: str,
    options: CaptureOptions,
    runtime: CaptureRuntime = DEFAULT_RUNTIME,
) -> int:
    """Capture every page in `report`; return a process exit code."""
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    unstable: list[str] = []
    failed: list[str] = []
    report_pages = pages(report)
    if not report_pages:
        print(f"FAILED: no pages found under {report / 'definition' / 'pages'}")
        return 1

    try:
        report_pages = _selected_pages(report_pages, options.page_ids)
    except ValueError as error:
        print(f"FAILED: requested page id(s) not found: {error}")
        return 2

    for page_id, name in report_pages:
        result = capture_stable(page_id, pid, out_dir / f"{_safe_filename(name)}.png", options, runtime)
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


def _page_ids(value: str) -> frozenset[str]:
    """Parse a non-empty, comma-separated list of PBIR page folder names."""
    page_ids = [page_id.strip() for page_id in value.split(",")]
    if not all(page_ids):
        raise argparse.ArgumentTypeError("page ids must be non-empty and comma-separated")
    return frozenset(page_ids)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path, help="Path to a .Report folder")
    parser.add_argument("outdir", type=Path, help="Folder where page PNGs should be written")
    parser.add_argument("--pid", required=True, help="Power BI Desktop PID to capture from")
    parser.add_argument(
        "--pages",
        type=_page_ids,
        help="Comma-separated PBIR page IDs (folder names) to capture; display names are not matched",
    )
    parser.add_argument("--poll", type=float, default=4.0, help="Seconds between frames for one page")
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=20.0,
        help="Minimum byte-identical dwell before treating a page as converged",
    )
    parser.add_argument("--max-wait", type=float, default=75.0, help="Max seconds to wait for one page")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    options = CaptureOptions(
        poll=args.poll,
        stable_seconds=args.stable_seconds,
        max_wait=args.max_wait,
        page_ids=args.pages,
    )
    return capture_report(args.report, args.outdir, args.pid, options)


if __name__ == "__main__":
    sys.exit(main())
