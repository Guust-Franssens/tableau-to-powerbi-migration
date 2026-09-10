#!/usr/bin/env python
"""
purpose: Capture every Power BI report page, waiting until each page's render has actually stabilised,
         and (in `iterate` mode) retain that capture as one canonical package-local review iteration.
usage:   python scripts/capture_powerbi_pages.py <report.Report> <output-dir> [--pid PID]
                                                [--pages <id>[,<id>...]] [--poll 4]
                                                [--stable-seconds 20] [--max-wait 75]
         python scripts/capture_powerbi_pages.py iterate --package <package> --pid PID
                                                [--mode sign_off|triage] [--pages <id>[,<id>...]]
                                                [--reviewer NAME] [--session-id ID]
                                                [--data-evidence <file.json>]
                                                [--desktop-file-path <path>]
         python scripts/capture_powerbi_pages.py finalize --package <package> [--iteration NNN]

The two modes, and why both exist
---------------------------------
The bare positional form is unchanged: point it at a `.Report` folder and an output directory and it
writes settled PNGs. It is the right tool when you are looking at something, and it deliberately
produces no evidence - the result is printed and discarded.

`iterate` is the same capture with a MEMORY. It resolves the caller-supplied package's own canonical
report/model (no ancestor search), derives the page and visual inventory from the CURRENT PBIR
definition rather than from whatever was captured, allocates the next `validation/iterations/<NNN>/`
exclusively, retains the settled PNGs there, and writes one strict `iteration.json` whose generated
half is re-derivable and whose judgement half starts PENDING. `finalize` re-derives every generated
identity immediately before sealing it, so any edit to the report, model, cache, a retained
screenshot or a prior receipt makes the iteration stale instead of quietly authoritative.

⚠️ An iteration is EVIDENCE, not a verdict. Nothing here decides whether a unit is finished.

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
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ruff: noqa: E402  (the sys.path insert above must precede these sibling-module imports)
import current_artifact_revision as rev  # pylint: disable=wrong-import-position
import iteration_receipt as receipt  # pylint: disable=wrong-import-position

Screenshotter = Callable[[str, str, Path], bool]
BRIDGE_WAIT_SECONDS = 90
SCREENSHOT_TIMEOUT_SECONDS = BRIDGE_WAIT_SECONDS + 30

#: Recorded into every receipt's `review.tool_version`. Bump it when the CAPTURE RULE changes, so a
#: receipt says which rule produced it rather than merely which day it was written.
TOOL_VERSION = "1.0.0"

SUBCOMMANDS = ("iterate", "finalize")

EXIT_OK = 0
EXIT_CAPTURE_FAILED = 1
EXIT_USAGE = 2
#: A named invariant refused the work. Distinct from 1 so "the bridge did not settle" and "this
#: iteration would not have been evidence" are never confused by a caller.
EXIT_REFUSED = 3


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


def _page_ids(value: str) -> frozenset[str]:
    """Parse a non-empty, comma-separated list of PBIR page folder names."""
    page_ids = [page_id.strip() for page_id in value.split(",")]
    if not all(page_ids):
        raise argparse.ArgumentTypeError("page ids must be non-empty and comma-separated")
    return frozenset(page_ids)


# --------------------------------------------------------------------------------------------------
# iteration mode
# --------------------------------------------------------------------------------------------------


def _selected_inventory(
    inventory: list[rev.PageInventory], requested: frozenset[str] | None
) -> list[rev.PageInventory]:
    """The current pages this run covers, refusing a page id the report does not have."""
    if requested is None:
        return inventory
    missing = sorted(requested - {page.page_id for page in inventory})
    if missing:
        raise receipt.ReceiptError("UNKNOWN_PAGE_ID", f"the report has no page(s) {missing}")
    return [page for page in inventory if page.page_id in requested]


def _resolved_mode(requested: str | None, covers_every_page: bool) -> str:
    """Sign-off scope is EVERY current page; a subset can only ever be triage.

    A partial sweep that calls itself a sign-off is the exact confusion this producer exists to
    prevent - it certifies the pages nobody looked at by saying nothing about them.
    """
    if not covers_every_page:
        if requested == receipt.MODE_SIGN_OFF:
            raise receipt.ReceiptError(
                "SUBSET_CANNOT_SIGN_OFF", "a subset of the report's pages can never be a sign-off"
            )
        return receipt.MODE_TRIAGE
    return requested or receipt.MODE_SIGN_OFF


def _desktop_binding(target: receipt.PackageTarget, declared: str | None) -> tuple[bool, bool | None]:
    """`(checked, matches)` for the open Desktop file - the RESULT only, never the path.

    Producer-time evidence: whether the instance being screenshotted is showing THIS package's PBIP.
    The path itself is an absolute host path and must not reach a shareable receipt, so only the
    boolean survives; the PID and session identity that make it meaningful are recorded separately.
    """
    if declared is None:
        return False, None
    pbip = next(iter(sorted(target.report_dir.parent.glob("*.pbip"))), None)
    if pbip is None:
        raise receipt.ReceiptError("DESKTOP_BINDING_MISMATCH", "the package declares no .pbip to bind against")
    try:
        matches = Path(declared).resolve() == pbip.resolve()
    except OSError as error:
        raise receipt.ReceiptError("DESKTOP_BINDING_MISMATCH", "the open Desktop file could not be resolved") from error
    if not matches:
        raise receipt.ReceiptError(
            "DESKTOP_BINDING_MISMATCH", "the open Desktop instance is showing a different file than this package"
        )
    return True, True


def _capture_pages(
    selected: list[rev.PageInventory],
    pid: str,
    directory: Path,
    options: CaptureOptions,
    runtime: CaptureRuntime,
) -> list[dict[str, Any]]:
    """Settle and retain one PNG per selected page, returning its immutable capture facts."""
    pages_dir = directory / receipt.PAGES_DIRNAME
    pages_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for page in selected:
        name = receipt.page_image_name(page.page_id)
        dest = pages_dir / name
        result = capture_stable(page.page_id, pid, dest, options, runtime)
        if not result.captured or not dest.is_file():
            raise receipt.ReceiptError("CAPTURE_FAILED", f"page {page.page_id!r} produced no settled screenshot")
        blob = dest.read_bytes()
        if not blob:
            raise receipt.ReceiptError("SCREENSHOT_EMPTY", f"page {page.page_id!r} captured zero bytes")
        rows.append(
            {
                "page_id": page.page_id,
                "display_name": page.display_name,
                "expected_visual_ids": list(page.visual_ids),
                "tableau": None,
                "tableau_reason": None,
                "powerbi": {
                    "path": f"{receipt.PAGES_DIRNAME}/{name}",
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "byte_count": len(blob),
                    "converged": result.converged,
                    "frames": result.frames,
                    "stable_seconds": options.stable_seconds,
                    "settled_seconds": round(result.seconds, 3),
                },
            }
        )
    return rows


@dataclass(frozen=True)
class IterationRequest:
    """Everything `iterate` needs that is not a capture tuning knob."""

    package: Path
    pid: str
    mode: str | None = None
    reviewer: str = "pbi-migration-validator"
    session_id: str | None = None
    data_evidence: Path | None = None
    desktop_file_path: str | None = None


def run_iteration(  # pylint: disable=too-many-locals
    request: IterationRequest,
    options: CaptureOptions,
    runtime: CaptureRuntime = DEFAULT_RUNTIME,
) -> dict[str, Any]:
    """Allocate, capture and write one PENDING iteration receipt. Returns the receipt payload.

    The allocated directory is removed if anything after allocation refuses, because a numbered
    directory with no receipt in it would make the whole chain unreadable from then on - a failed
    capture must not cost the unit its history.
    """
    target = receipt.resolve_package(request.package)
    inventory = receipt.report_inventory(target.report_dir)
    selected = _selected_inventory(inventory, options.page_ids)
    if not selected:
        raise receipt.ReceiptError("NO_PAGES", "no current page was selected for capture")
    mode = _resolved_mode(request.mode, len(selected) == len(inventory))
    checked, matches = _desktop_binding(target, request.desktop_file_path)

    artifact = receipt.artifact_facts(target)
    data = (
        receipt.ingest_data_evidence(request.data_evidence, artifact["model_revision"], artifact["cache_sha256"])
        if request.data_evidence is not None
        else receipt.pending_data_evidence()
    )

    directory, previous = receipt.allocate_iteration(request.package)
    try:
        rows = _capture_pages(selected, request.pid, directory, options, runtime)
        for page_id, match in receipt.tableau_matches(target, selected).items():
            row = next(row for row in rows if row["page_id"] == page_id)
            row["tableau"], row["tableau_reason"] = match.evidence, match.reason
        payload = {
            "schema_version": receipt.SCHEMA_VERSION,
            "iteration": directory.name,
            "mode": mode,
            "state": receipt.STATE_PENDING,
            "outcome": None,
            "generated": {
                "generated_at": receipt.now_rfc3339(),
                "scope": receipt.SCOPE_ALL_PAGES if len(selected) == len(inventory) else receipt.SCOPE_SUBSET,
                "artifact": artifact,
                "review": {
                    "reviewer": request.reviewer,
                    "session_id": request.session_id,
                    "tool": receipt.TOOL_NAME,
                    "tool_version": TOOL_VERSION,
                    "desktop_binding_checked": checked,
                    "desktop_binding_matches": matches,
                },
                "previous": (
                    None
                    if previous is None
                    else {
                        "iteration": previous.name,
                        "receipt_sha256": previous.receipt_sha256,
                        "report_revision": previous.payload["generated"]["artifact"]["report_revision"],
                        "model_revision": previous.payload["generated"]["artifact"]["model_revision"],
                    }
                ),
                "limitations": receipt.limitation_facts(target.root),
                "data_evidence": data,
                "pages": rows,
                "changes_from_previous": receipt.changes_from_previous(previous, rows),
            },
            "judgement": receipt.pending_judgement(selected),
        }
        receipt.write_receipt(directory, payload)
        return payload
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _refused(error: receipt.ReceiptError | rev.RevisionError) -> int:
    print(f"REFUSED: {error.code}: {error.detail}")
    return EXIT_REFUSED


def cmd_iterate(args: argparse.Namespace, runtime: CaptureRuntime = DEFAULT_RUNTIME) -> int:
    """`iterate` entry point: capture into a new numbered iteration and write its pending receipt."""
    options = CaptureOptions(
        poll=args.poll, stable_seconds=args.stable_seconds, max_wait=args.max_wait, page_ids=args.pages
    )
    request = IterationRequest(
        package=args.package,
        pid=args.pid,
        mode=args.mode,
        reviewer=args.reviewer,
        session_id=args.session_id,
        data_evidence=args.data_evidence,
        desktop_file_path=args.desktop_file_path,
    )
    try:
        payload = run_iteration(request, options, runtime)
    except (receipt.ReceiptError, rev.RevisionError) as error:
        return _refused(error)
    generated = payload["generated"]
    unstable = [row["page_id"] for row in generated["pages"] if not row["powerbi"]["converged"]]
    blind = [row["page_id"] for row in generated["pages"] if row["tableau"] is None]
    print(
        f"ITERATION {payload['iteration']} ({payload['mode']}, {generated['scope']}): "
        f"{len(generated['pages'])} page(s) captured, {len(blind)} with no Tableau render"
    )
    if unstable:
        print("NEVER CONVERGED (treat as PARTIAL): " + ", ".join(unstable))
    if generated["data_evidence"]["status"] == receipt.DATA_STATUS_PENDING:
        print(f"DATA EVIDENCE PENDING: {generated['data_evidence']['pending_reason']}")
    print("Fill only the judgement fields, then run: capture_powerbi_pages.py finalize --package <package>")
    return EXIT_OK


def cmd_finalize(args: argparse.Namespace) -> int:
    """`finalize` entry point: re-derive every generated identity, then seal the iteration."""
    try:
        payload = receipt.finalize(args.package, args.iteration)
    except (receipt.ReceiptError, rev.RevisionError) as error:
        return _refused(error)
    print(f"FINALIZED {payload['iteration']} ({payload['mode']}): outcome {payload['outcome']}")
    return EXIT_OK


def _iteration_parser() -> argparse.ArgumentParser:
    """The subcommand parser. Kept separate so the positional capture form stays untouched."""
    parser = argparse.ArgumentParser(prog="capture_powerbi_pages.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    iterate = subparsers.add_parser("iterate", help="capture into a new package-local iteration")
    iterate.add_argument("--package", type=Path, required=True, help="Path to the phase-2 package")
    iterate.add_argument("--pid", required=True, help="Power BI Desktop PID to capture from")
    iterate.add_argument("--mode", choices=receipt.MODES, help="Defaults to sign_off for a full sweep")
    iterate.add_argument("--pages", type=_page_ids, help="Comma-separated PBIR page IDs; forces triage mode")
    iterate.add_argument("--reviewer", default="pbi-migration-validator", help="Who is reviewing this iteration")
    iterate.add_argument("--session-id", help="Agent session id, recorded for cost/identity attribution")
    iterate.add_argument("--data-evidence", type=Path, help="Tool-produced DATA_OK record to bind into the receipt")
    iterate.add_argument("--desktop-file-path", help="currentFilePath of the open Desktop instance (never recorded)")
    iterate.add_argument("--poll", type=float, default=4.0, help="Seconds between frames for one page")
    iterate.add_argument("--stable-seconds", type=float, default=20.0, help="Minimum byte-identical dwell")
    iterate.add_argument("--max-wait", type=float, default=75.0, help="Max seconds to wait for one page")

    finalize = subparsers.add_parser("finalize", help="validate and seal a pending iteration")
    finalize.add_argument("--package", type=Path, required=True, help="Path to the phase-2 package")
    finalize.add_argument("--iteration", help="Iteration name (NNN); defaults to the latest")
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments.

    The first token decides the grammar. That keeps the original `<report> <outdir> --pid` positional
    form byte-for-byte compatible - a `.Report` path is never one of the subcommand words - instead
    of demoting it behind a `capture` subcommand nobody's existing invocation types.
    """
    if argv and argv[0] in SUBCOMMANDS:
        return _iteration_parser().parse_args(argv)
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
    parser.set_defaults(command=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if getattr(args, "command", None) == "iterate":
        return cmd_iterate(args)
    if getattr(args, "command", None) == "finalize":
        return cmd_finalize(args)
    options = CaptureOptions(
        poll=args.poll,
        stable_seconds=args.stable_seconds,
        max_wait=args.max_wait,
        page_ids=args.pages,
    )
    return capture_report(args.report, args.outdir, args.pid, options)


if __name__ == "__main__":
    sys.exit(main())
