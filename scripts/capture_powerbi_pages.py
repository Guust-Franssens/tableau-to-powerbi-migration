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
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import capture_receipt

Screenshotter = Callable[[str, str, Path], bool]
StatusQuerier = Callable[[str], tuple[int, str]]
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


def _staged_destination(dest: Path) -> Path:
    """Return the hidden sibling path used before a capture becomes report evidence."""
    return dest.with_name(f".{dest.stem}.capturing{dest.suffix}")


def capture_stable(
    page_id: str,
    pid: str,
    dest: Path,
    options: CaptureOptions,
    runtime: CaptureRuntime = DEFAULT_RUNTIME,
) -> CaptureResult:
    """Screenshot until one frame digest remains stable for the configured dwell."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged_dest = _staged_destination(dest)
    staged_dest.unlink(missing_ok=True)

    started = runtime.clock()
    stable_digest: str | None = None
    stable_idle_seconds = 0.0
    frames = 0
    captured_frame = False
    previous_frame_finished = started
    while runtime.clock() - started < options.max_wait:
        capture_started = runtime.clock()
        frames += 1
        if not runtime.screenshotter(page_id, pid, staged_dest):
            staged_dest.unlink(missing_ok=True)
            return CaptureResult(False, False, runtime.clock() - started, frames)
        captured_frame = True
        digest = frame_digest(staged_dest)
        if digest != stable_digest:
            stable_digest = digest
            stable_idle_seconds = 0.0
        else:
            stable_idle_seconds += max(0.0, capture_started - previous_frame_finished)
        previous_frame_finished = runtime.clock()
        if stable_idle_seconds >= options.stable_seconds:
            staged_dest.replace(dest)
            return CaptureResult(True, True, runtime.clock() - started, frames)
        runtime.sleep(options.poll)

    if captured_frame:
        staged_dest.replace(dest)
        return CaptureResult(True, False, runtime.clock() - started, frames)
    staged_dest.unlink(missing_ok=True)
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


# ---------------------------------------------------------------------------
# Bridge status default querier
# ---------------------------------------------------------------------------


def query_bridge_status(_pid: str) -> tuple[int, str]:
    """Query the Desktop bridge ``status`` command.  Returns ``(exit_code, stdout)``."""
    try:
        proc = subprocess.run(
            ["powerbi-desktop", "status"],
            capture_output=True,
            text=True,
            shell=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return (1, "")
    return (proc.returncode, proc.stdout)


# ---------------------------------------------------------------------------
# Package capture — the --package mode
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageCaptureRuntime:
    """Injectable runtime hooks for package capture tests."""

    screenshotter: Screenshotter
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    status_querier: StatusQuerier
    now_utc: Callable[[], datetime]


DEFAULT_PACKAGE_RUNTIME = PackageCaptureRuntime(
    screenshot,
    time.sleep,
    time.time,
    query_bridge_status,
    lambda: datetime.now(timezone.utc),
)


def _resolve_and_validate(
    package_root: Path,
    pid: str,
    runtime: PackageCaptureRuntime,
) -> tuple[capture_receipt.PackageIdentity, list[capture_receipt.PageInfo], str] | str:
    """Resolve package, validate bridge, read inventory.  Returns error string on failure."""
    try:
        identity = capture_receipt.resolve_package(package_root)
    except ValueError as exc:
        return str(exc)
    code, raw = runtime.status_querier(pid)
    if code != 0:
        return f"bridge status query returned exit code {code}"
    try:
        instances = capture_receipt.parse_bridge_status(raw)
        capture_receipt.validate_bridge_open(pid, identity.pbip_path, instances)
    except ValueError as exc:
        return str(exc)
    current_file_path = ""
    for inst in instances:
        if inst.pid == int(pid):
            current_file_path = inst.current_file_path
            break
    try:
        inventory = capture_receipt.read_pbir_inventory(identity.report_folder)
    except ValueError as exc:
        return str(exc)
    return (identity, inventory, current_file_path)


def _capture_pages(
    selected_pages: list[capture_receipt.PageInfo],
    pages_dir: Path,
    pid: str,
    options: CaptureOptions,
    runtime: PackageCaptureRuntime,
) -> list[capture_receipt.PageCapture] | None:
    """Capture each page.  Returns None on any failure (unconverged / zero-byte / failed)."""
    page_captures: list[capture_receipt.PageCapture] = []
    for page_info in selected_pages:
        dest = pages_dir / f"{page_info.page_id}.png"
        result = capture_stable(
            page_info.page_id,
            pid,
            dest,
            options,
            CaptureRuntime(
                screenshotter=runtime.screenshotter,
                sleep=runtime.sleep,
                clock=runtime.clock,
            ),
        )
        tag = "OK" if result.captured and result.converged else ("UNSTABLE" if result.captured else "FAIL")
        print(
            f"  {tag:<9}{page_info.display_name:<26} settled in {result.seconds:5.1f}s over {result.frames} frames",
            flush=True,
        )
        if not result.captured or not result.converged:
            reason = "capture failed" if not result.captured else "never converged"
            print(f"FAILED: page {page_info.display_name!r} {reason}")
            return None
        if not dest.exists() or dest.stat().st_size == 0:
            print(f"FAILED: zero-byte screenshot for page {page_info.display_name!r}")
            return None
        screenshot_bytes = dest.read_bytes()
        page_captures.append(
            capture_receipt.PageCapture(
                page_id=page_info.page_id,
                display_name=page_info.display_name,
                visual_ids=[v.name for v in page_info.visuals],
                screenshot_relative_path=f"pages/{page_info.page_id}.png",
                screenshot_sha256=hashlib.sha256(screenshot_bytes).hexdigest(),
                screenshot_bytes=len(screenshot_bytes),
                converged=result.converged,
                frames=result.frames,
                elapsed_seconds=round(result.seconds, 3),
            )
        )
    return page_captures


def _write_receipt(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    iteration_dir: Path,
    identity: capture_receipt.PackageIdentity,
    iteration_id: str,
    mode: str,
    scope: str,
    current_file_path: str,
    options: CaptureOptions,
    page_captures: list[capture_receipt.PageCapture],
    runtime: PackageCaptureRuntime,
) -> list[str]:
    """Build, validate and write the capture receipt.  Returns validation errors (empty = ok)."""
    receipt = capture_receipt.CaptureReceipt(
        iteration_id=iteration_id,
        mode=mode,
        scope=scope,
        package_root=str(identity.package_root),
        pbip_path=str(identity.pbip_path),
        pbip_sha256=identity.pbip_sha256,
        report_folder=str(identity.report_folder),
        definition_pbir_sha256=identity.definition_pbir_sha256,
        current_file_path=current_file_path,
        timestamp=runtime.now_utc().isoformat(),
        stable_seconds=options.stable_seconds,
        poll_seconds=options.poll,
        max_wait_seconds=options.max_wait,
        pages=page_captures,
    )
    receipt_dict = capture_receipt.receipt_to_dict(receipt)
    errors = capture_receipt.validate_receipt(receipt_dict)
    if not errors:
        (iteration_dir / "capture.json").write_text(json.dumps(receipt_dict, indent=2) + "\n", encoding="utf-8")
    return errors


def capture_package(  # pylint: disable=too-many-locals,too-many-return-statements
    package_root: Path,
    pid: str,
    options: CaptureOptions,
    runtime: PackageCaptureRuntime = DEFAULT_PACKAGE_RUNTIME,
) -> int:
    """Capture every page in a package, writing an immutable numbered capture receipt.

    Returns a process exit code: 0 on success, non-zero on any failure.
    """
    resolved = _resolve_and_validate(package_root, pid, runtime)
    if isinstance(resolved, str):
        print(f"FAILED: {resolved}")
        return 1
    identity, inventory, current_file_path = resolved

    # Determine mode/scope
    if options.page_ids is not None:
        mode, scope = "triage", "subset"
        missing = sorted(options.page_ids - {p.page_id for p in inventory})
        if missing:
            print(f"FAILED: requested page id(s) not found: {', '.join(missing)}")
            return 2
    else:
        mode, scope = "sign-off", "all-pages"

    # Allocate iteration
    try:
        iteration_dir, iteration_id = capture_receipt.allocate_iteration(package_root)
    except (ValueError, OSError) as exc:
        print(f"FAILED: {exc}")
        return 1

    pages_dir = iteration_dir / "pages"
    pages_dir.mkdir()

    selected = [p for p in inventory if options.page_ids is None or p.page_id in options.page_ids]
    started = runtime.clock()
    page_captures = _capture_pages(selected, pages_dir, pid, options, runtime)
    if page_captures is None:
        shutil.rmtree(iteration_dir, ignore_errors=True)
        print(f"iteration {iteration_id} removed")
        return 1

    # Verify sign-off completeness
    if mode == "sign-off" and {pc.page_id for pc in page_captures} != {p.page_id for p in inventory}:
        shutil.rmtree(iteration_dir, ignore_errors=True)
        print("FAILED: sign-off requires all pages")
        return 1

    # Build and write receipt
    errors = _write_receipt(
        iteration_dir,
        identity,
        iteration_id,
        mode,
        scope,
        current_file_path,
        options,
        page_captures,
        runtime,
    )
    if errors:
        shutil.rmtree(iteration_dir, ignore_errors=True)
        print(f"FAILED: receipt validation errors: {errors}")
        return 1
    elapsed = runtime.clock() - started
    print(f"\n{len(page_captures)}/{len(selected)} captured in {elapsed:.1f}s → iteration {iteration_id}")
    return 0


def _page_ids(value: str) -> frozenset[str]:
    """Parse a non-empty, comma-separated list of PBIR page folder names."""
    page_ids = [page_id.strip() for page_id in value.split(",")]
    if not all(page_ids):
        raise argparse.ArgumentTypeError("page ids must be non-empty and comma-separated")
    return frozenset(page_ids)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path, nargs="?", help="Path to a .Report folder (legacy mode)")
    parser.add_argument("outdir", type=Path, nargs="?", help="Folder where page PNGs should be written (legacy mode)")
    parser.add_argument("--package", type=Path, help="Package root for immutable capture receipt mode")
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
    if args.package:
        return capture_package(args.package, args.pid, options)
    if not args.report or not args.outdir:
        print("FAILED: legacy mode requires both report and outdir positional arguments")
        return 64
    return capture_report(args.report, args.outdir, args.pid, options)


if __name__ == "__main__":
    sys.exit(main())
