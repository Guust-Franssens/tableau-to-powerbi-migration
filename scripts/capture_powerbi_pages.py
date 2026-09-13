#!/usr/bin/env python
"""
purpose: Capture stable Power BI pages, or retain a neutral package-local Phase-2 evidence iteration.
usage:   python scripts/capture_powerbi_pages.py <report.Report> <outdir> --pid PID
         python scripts/capture_powerbi_pages.py iterate --package <package> --pid PID
         python scripts/capture_powerbi_pages.py finalize --package <package> --capture-sha256 SHA
                                                --judgement <review.json>

Retain the checksum PRINTED by iterate, not a checksum recomputed from an edited receipt.
Review a separate copy of iteration.json's judgement object OUTSIDE the package; iteration.json
is producer-owned. For the next iterate, pass --previous-sha256 using finalize's returned checksum.
Package capture verifies the coherent report/model/PBIP binding against trusted PID-scoped bridge
status, reloads that PID, and requires finite positive polling/dwell and repeated equal frames.

Stability remains a heuristic, not a render-readiness signal. A progressive azureMap can pause
longer than a dwell before drawing more marks. Blocking screenshot time earns no stable dwell.
Standalone capture still writes bare PNGs. A zero-dwell standalone request now needs two frames,
not one, but its existing positional grammar and failure/partial-frame behavior remain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import current_artifact_revision as rev
import iteration_receipt as receipt

# As in probe_live_source, import the canonical bundled APIs, not the CLI forwarding shims.
# Neither import loads CLR or starts native work; there is no alternate implementation.
SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "skills" / "pbip-model-refresh" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
from probe_desktop_query import (  # noqa: E402  # pylint: disable=wrong-import-position,no-name-in-module
    BoundDesktop,
    CanaryObservation,
    ObservationUnavailable,
    bind_desktop,
    bound_call,
    probe_observations,
)
from refresh_pbip_model import (  # noqa: E402  # pylint: disable=wrong-import-position,no-name-in-module
    CompatRollbackError,
    ModelLockTimeout,
    PersistenceObservation,
    RefreshObservation,
    image_save,
    refresh,
)

Screenshotter = Callable[[str, str, Path], bool]
BRIDGE_WAIT_SECONDS = 90
SCREENSHOT_TIMEOUT_SECONDS = BRIDGE_WAIT_SECONDS + 30
TOOL_VERSION = receipt.TOOL_VERSION
SUBCOMMANDS = ("iterate", "finalize")
EXIT_OK, EXIT_CAPTURE_FAILED, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3


def _emit(text: str, *, stream=None, flush: bool = False) -> None:
    """The repository console-safe pattern (check_unit._safe_print), before the first write."""
    stream = stream if stream is not None else sys.stdout
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        try:
            text = text.encode(encoding, "backslashreplace").decode(encoding, "replace")
        except LookupError:
            text = text.encode("ascii", "backslashreplace").decode("ascii")
    print(text, file=stream, flush=flush)


@dataclass(frozen=True)
class CaptureResult:
    """Measured capture outcome, including idle-only stable dwell."""

    captured: bool
    converged: bool
    seconds: float
    frames: int
    stable_elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class CaptureOptions:
    """Per-page capture policy."""

    poll: float
    stable_seconds: float
    max_wait: float
    page_ids: frozenset[str] | None = None


def screenshot(page_id: str, pid: str, dest: Path) -> bool:
    """Capture through the PID-scoped bridge without persisting its diagnostic output."""
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
            shell=True,
            check=False,
            timeout=SCREENSHOT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and dest.exists()


@dataclass(frozen=True)
class CaptureRuntime:
    """Trusted runtime seam; CLI callers cannot supply a current-file claim."""

    screenshotter: Screenshotter
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    state_reader: receipt.StatusReader = receipt.bridge_status
    reload: Callable[[int], bool] = receipt.bridge_reload


DEFAULT_RUNTIME = CaptureRuntime(screenshot, time.sleep, time.monotonic)


def pages(report: Path) -> list[tuple[str, str]]:
    """Standalone legacy page discovery; package mode uses the strict current inventory."""
    output = []
    for path in sorted((report / "definition" / "pages").glob("*/page.json")):
        doc = receipt.read_strict_json(path)
        output.append((path.parent.name, doc.get("displayName", path.parent.name)))
    return output


def frame_digest(path: Path) -> str:
    """Hash all frame bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _staged_destination(dest: Path) -> Path:
    return dest.with_name(f".{dest.stem}.capturing{dest.suffix}")


def _validate_options(options: CaptureOptions, *, package: bool = False) -> None:
    for value in (options.poll, options.stable_seconds, options.max_wait):
        if type(value) not in (int, float) or not 0 <= value <= receipt.MAX_SECONDS or not math.isfinite(value):
            raise receipt.ReceiptError("CAPTURE_POLICY", "capture timing must be finite and in range")
    if options.max_wait <= 0 or (package and (options.poll <= 0 or options.stable_seconds <= 0)):
        raise receipt.ReceiptError("CAPTURE_POLICY", "package polling, dwell and deadline must be positive")


def capture_stable(
    page_id: str,
    pid: str,
    dest: Path,
    options: CaptureOptions,
    runtime: CaptureRuntime = DEFAULT_RUNTIME,
) -> CaptureResult:
    """Require repeated byte-identical frames; blocking screenshot duration earns no dwell."""
    _validate_options(options)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged_dest = _staged_destination(dest)
    staged_dest.unlink(missing_ok=True)
    started = runtime.clock()
    stable_digest: str | None = None
    stable_idle_seconds = 0.0
    frames = 0
    previous_frame_finished = started
    while runtime.clock() - started < options.max_wait and frames < receipt.MAX_FRAMES:
        capture_started = runtime.clock()
        frames += 1
        if not runtime.screenshotter(page_id, pid, staged_dest):
            staged_dest.unlink(missing_ok=True)
            return CaptureResult(False, False, runtime.clock() - started, frames)
        digest = frame_digest(staged_dest)
        unchanged = digest == stable_digest
        if digest != stable_digest:
            stable_digest = digest
            stable_idle_seconds = 0.0
        else:
            stable_idle_seconds += max(0.0, capture_started - previous_frame_finished)
        previous_frame_finished = runtime.clock()
        if unchanged and frames >= 2 and stable_idle_seconds >= options.stable_seconds:
            staged_dest.replace(dest)
            return CaptureResult(True, True, runtime.clock() - started, frames, stable_idle_seconds)
        runtime.sleep(options.poll)
    if frames:
        staged_dest.replace(dest)
        return CaptureResult(True, False, runtime.clock() - started, frames, stable_idle_seconds)
    staged_dest.unlink(missing_ok=True)
    return CaptureResult(False, False, runtime.clock() - started, frames)


def _safe_filename(name: str) -> str:
    return "".join(char if char not in '<>:"/\\|?*' else "_" for char in name).strip() or "page"


def _selected_pages(report_pages: list[tuple[str, str]], requested: frozenset[str] | None) -> list[tuple[str, str]]:
    if requested is None:
        return report_pages
    missing = requested - {page_id for page_id, _ in report_pages}
    if missing:
        raise ValueError(", ".join(sorted(missing)))
    return [(page_id, name) for page_id, name in report_pages if page_id in requested]


def capture_report(
    report: Path,
    out_dir: Path,
    pid: str,
    options: CaptureOptions,
    runtime: CaptureRuntime = DEFAULT_RUNTIME,
) -> int:
    """Standalone bare-PNG capture, with safe console output."""
    _validate_options(options)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    unstable: list[str] = []
    failed: list[str] = []
    report_pages = pages(report)
    if not report_pages:
        _emit(f"FAILED: no pages found under {report / 'definition' / 'pages'}")
        return EXIT_CAPTURE_FAILED
    try:
        report_pages = _selected_pages(report_pages, options.page_ids)
    except ValueError as error:
        _emit(f"FAILED: requested page id(s) not found: {error}")
        return EXIT_USAGE
    for page_id, name in report_pages:
        result = capture_stable(page_id, pid, out_dir / f"{_safe_filename(name)}.png", options, runtime)
        tag = "OK" if result.captured and result.converged else ("UNSTABLE" if result.captured else "FAIL")
        _emit(
            f"  {tag:<9}{name:<26} settled in {result.seconds:5.1f}s over {result.frames} frames "
            f"({time.monotonic() - started:6.1f}s total)",
            flush=True,
        )
        if not result.captured:
            failed.append(name)
        elif not result.converged:
            unstable.append(name)
    _emit(f"\n{len(report_pages) - len(failed)}/{len(report_pages)} captured in {time.monotonic() - started:.1f}s")
    if unstable:
        _emit("NEVER CONVERGED (still changing at max-wait, treat as PARTIAL): " + ", ".join(unstable))
    if failed:
        _emit("FAILED: " + ", ".join(failed))
    return EXIT_CAPTURE_FAILED if failed or unstable else EXIT_OK


def _page_ids(value: str) -> frozenset[str]:
    ids = [page_id.strip() for page_id in value.split(",")]
    if not all(ids) or len(set(ids)) != len(ids):
        raise argparse.ArgumentTypeError("page ids must be unique, nonempty and comma-separated")
    return frozenset(ids)


@dataclass(frozen=True)
class IterationRequest:  # pylint: disable=too-many-instance-attributes
    """Package capture inputs; no caller-supplied Desktop path or data-success record."""

    package: Path
    pid: str
    mode: str | None = None
    reviewer: str = "pbi-migration-validator"
    session_id: str | None = None
    previous_sha256: str | None = None
    refresh: bool = False
    persist: bool = False
    canaries: tuple[str, ...] = ()


def _preparation_request(request: IterationRequest) -> dict[str, Any]:
    if type(request.canaries) is not tuple:  # pylint: disable=unidiomatic-typecheck
        raise receipt.ReceiptError("REQUEST_INVALID", "canaries must be an explicit tuple of table names")
    requested = {"refresh": request.refresh, "persist": request.persist, "canaries": list(request.canaries)}
    receipt._validate(receipt.PREPARATION_REQUEST_SCHEMA, requested)  # pylint: disable=protected-access
    if any(not name.strip() for name in request.canaries) or len({name.casefold() for name in request.canaries}) != len(
        request.canaries
    ):
        raise receipt.ReceiptError("CANARIES_REQUIRED", "canaries must be nonempty, unique explicit table names")
    return requested


def _recheck_a1(bound: BoundDesktop | None) -> None:
    if bound is not None:
        try:
            bound_call(bound, lambda _connection: None)
        except ObservationUnavailable as error:
            raise receipt.ReceiptError(
                "A1_BINDING_UNESTABLISHED", "the held A1 binding could not be rechecked"
            ) from error


def _observe_a1(operation: Callable[[], Any], result_type: type, bound: BoundDesktop) -> dict[str, Any]:
    """Consume this invocation's return object only; never a sink, legacy tuple or supplied result."""
    try:
        result = operation()
    except ObservationUnavailable as error:
        if len(error.args) != 1 or error.args[0] not in receipt.MEASUREMENT_REFUSALS:
            raise receipt.ReceiptError("A1_BINDING_UNESTABLISHED", "A1 refused observation authority") from error
        return {"status": receipt.REFUSED, "reason": error.args[0], "observation": None}
    except ModelLockTimeout:
        return {"status": receipt.REFUSED, "reason": "MODEL_LOCK_TIMEOUT", "observation": None}
    except CompatRollbackError as error:
        raise receipt.ReceiptError(
            "A1_PERSISTENCE_UNESTABLISHED", "A1 could not restore its publication state"
        ) from error
    if result is None:
        return receipt.unestablished_fact("observation_unavailable")
    multiple = result_type is CanaryObservation
    if multiple and type(result) is not tuple:  # pylint: disable=unidiomatic-typecheck
        raise receipt.ReceiptError("A1_OBSERVATION_INVALID", "canary observations must be invocation-owned returns")
    documents = []
    for observed in result if multiple else (result,):
        if type(observed) is not result_type:  # pylint: disable=unidiomatic-typecheck
            raise receipt.ReceiptError("A1_OBSERVATION_INVALID", "a structured A1 observation is required")
        if observed.identity != bound.identity or observed.catalogue != bound.catalogue:
            raise receipt.ReceiptError("A1_BINDING_MISMATCH", "the returned observation belongs to another binding")
        document = asdict(observed)
        if multiple:
            document["query_sha256"] = hashlib.sha256(observed.query.encode("utf-8")).hexdigest()
        documents.append(document)
    observation = documents if multiple else documents[0]
    return {"status": receipt.OBSERVED, "reason": None, "observation": json.loads(json.dumps(observation))}


def _prepare_a1(
    target: receipt.PackageTarget, pid: int, requested: dict[str, Any]
) -> tuple[dict[str, Any], BoundDesktop | None]:
    """Optional, explicit preparation through one held Desktop/AS/catalogue binding."""
    facts = {key: receipt.unestablished_fact() for key in ("binding", "refresh", "canaries", "persistence")}
    if not (requested["refresh"] or requested["persist"] or requested["canaries"]):
        return facts, None
    try:
        bound = bind_desktop(pid)
    except ObservationUnavailable as error:
        raise receipt.ReceiptError("A1_BINDING_UNESTABLISHED", "no held A1 binding was established") from error
    if type(bound) is not BoundDesktop:  # pylint: disable=unidiomatic-typecheck
        raise receipt.ReceiptError("A1_BINDING_UNESTABLISHED", "the A1 binding return is unavailable")
    receipt._validate(receipt.BINDING_SCHEMA, asdict(bound))  # pylint: disable=protected-access
    if bound.identity.pid != pid:
        raise receipt.ReceiptError("A1_BINDING_MISMATCH", "A1 bound a different Desktop process")
    facts["binding"] = {"status": receipt.OBSERVED, "reason": None, "observation": asdict(bound)}
    operations = (
        (
            "refresh",
            requested["refresh"],
            lambda: refresh(
                bound.identity.port, None, refresh_type="full", desktop_pid=pid, bound=bound, return_observation=True
            ),
            RefreshObservation,
        ),
        (
            "canaries",
            bool(requested["canaries"]),
            lambda: probe_observations(bound, requested["canaries"]),
            CanaryObservation,
        ),
        (
            "persistence",
            requested["persist"],
            lambda: image_save(
                bound.identity.port,
                target.model_dir / ".pbi" / "cache.abf",
                target.model_dir,
                bound=bound,
                return_observation=True,
            ),
            PersistenceObservation,
        ),
    )
    for key, enabled, operation, result_type in operations:
        if enabled:
            facts[key] = _observe_a1(operation, result_type, bound)
            _recheck_a1(bound)  # Measurement refusal is retainable only while authority still holds.
    return facts, bound


def _capture_pages(
    selected: list[rev.PageInventory],
    pid: str,
    directory: Path,
    options: CaptureOptions,
    runtime: CaptureRuntime,
) -> dict[str, dict[str, Any]]:
    captured = {}
    for page in selected:
        dest = directory / receipt.PAGES_DIRNAME / receipt.page_image_name(page.page_id)
        result = capture_stable(page.page_id, pid, dest, options, runtime)
        if not result.captured:
            raise receipt.ReceiptError("CAPTURE_FAILED", "a page capture failed; no iteration was retained")
        captured[page.page_id] = {
            "converged": result.converged,
            "frames": result.frames,
            "poll_seconds": options.poll,
            "stable_seconds": options.stable_seconds,
            "max_wait_seconds": options.max_wait,
            "settled_seconds": result.seconds,
            "stable_elapsed_seconds": result.stable_elapsed_seconds,
        }
    return captured


def _request_review(request: IterationRequest) -> dict[str, Any]:
    if not isinstance(request.pid, str) or not request.pid.isascii() or not request.pid.isdigit():
        raise receipt.ReceiptError("DESKTOP_UNVERIFIED", "a decimal Desktop PID is required")
    review = {
        "reviewer": request.reviewer,
        "session_id": request.session_id,
        "tool": receipt.TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "desktop_pid": int(request.pid),
        "desktop_binding_matches": True,
        "reload_confirmed": True,
    }
    receipt._validate(receipt.V3_GENERATED_SCHEMA["properties"]["review"], review)  # pylint: disable=protected-access
    return review


def run_iteration(  # pylint: disable=too-many-locals
    request: IterationRequest,
    options: CaptureOptions,
    runtime: CaptureRuntime = DEFAULT_RUNTIME,
) -> dict[str, Any]:
    """Prepare a bound PID, capture, revalidate current facts and persist a producer-owned receipt."""
    with receipt._named_refusals():  # pylint: disable=protected-access
        _validate_options(options, package=True)
        review = _request_review(request)
        requested = _preparation_request(request)
        target = receipt.resolve_package(request.package)
        inventory = receipt.report_inventory(target.report_dir)
        if options.page_ids is not None and options.page_ids - {page.page_id for page in inventory}:
            raise receipt.ReceiptError("UNKNOWN_PAGE_ID", "a requested page is absent from the current PBIR")
        selected = [page for page in inventory if options.page_ids is None or page.page_id in options.page_ids]
        if not selected:
            raise receipt.ReceiptError("NO_PAGES", "no current page was selected")
        mode = request.mode or (receipt.MODE_SIGN_OFF if len(selected) == len(inventory) else receipt.MODE_TRIAGE)
        if mode not in receipt.MODES or (mode == receipt.MODE_SIGN_OFF and len(selected) != len(inventory)):
            raise receipt.ReceiptError("SUBSET_CANNOT_SIGN_OFF", "sign-off must cover every current page")
        receipt.assert_shareable([{"page": page.page_id, "name": page.display_name} for page in selected])
        history = receipt.checked_history(request.package, request.previous_sha256)
        receipt.assert_desktop_binding(target, review["desktop_pid"], runtime.state_reader)
        if runtime.reload(review["desktop_pid"]) is not True:
            raise receipt.ReceiptError("DESKTOP_UNVERIFIED", "the bound Desktop instance did not confirm reload")
        receipt.assert_desktop_binding(target, review["desktop_pid"], runtime.state_reader)
        preparation = {"requested": requested, "artifact_before": receipt.artifact_facts(target)}
        observations, bound = _prepare_a1(target, review["desktop_pid"], requested)
        if (
            receipt.resolve_package(request.package) != target
            or receipt.report_inventory(target.report_dir) != inventory
        ):
            raise receipt.ReceiptError("GENERATED_CHANGED", "preparation changed the package or page binding")
        receipt.assert_desktop_binding(target, review["desktop_pid"], runtime.state_reader)
        before = receipt.artifact_facts(target)
        receipt.assert_iteration_progress(before, history[-1] if history else None)
        directory, previous = receipt.allocate_iteration(request.package, request.previous_sha256)
        try:
            captured = _capture_pages(selected, request.pid, directory, options, runtime)
            _recheck_a1(bound)
            receipt.assert_desktop_binding(target, review["desktop_pid"], runtime.state_reader)
            generated = receipt.generated_facts(
                target,
                directory,
                captured,
                review,
                receipt.now_rfc3339(),
                previous,
                schema_version=receipt.SCHEMA_VERSION,
                preparation=preparation,
                observations=observations,
            )
            if generated["artifact"] != before:
                raise receipt.ReceiptError("GENERATED_CHANGED", "the package changed during capture")
            payload = {
                "schema_version": receipt.SCHEMA_VERSION,
                "iteration": directory.name,
                "mode": mode,
                "state": receipt.STATE_PENDING,
                "generated": generated,
                "judgement": receipt.pending_judgement(selected),
            }
            if previous:
                payload["judgement"]["findings"] = json.loads(json.dumps(previous.payload["judgement"]["findings"]))
            receipt.write_receipt(directory, payload)
            receipt._assert_snapshot(request.package, receipt.read_history(request.package))  # pylint: disable=protected-access
            return payload
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise


def _refused(error: receipt.ReceiptError | rev.RevisionError) -> int:
    _emit(f"REFUSED: {error.code}: {error.detail}")
    return EXIT_REFUSED


def cmd_iterate(args: argparse.Namespace, runtime: CaptureRuntime = DEFAULT_RUNTIME) -> int:
    """CLI package capture; print the immutable capture token separately from the receipt."""
    try:
        payload = run_iteration(
            IterationRequest(
                args.package,
                args.pid,
                args.mode,
                args.reviewer,
                args.session_id,
                args.previous_sha256,
                args.refresh,
                args.persist,
                tuple(args.canary_table),
            ),
            CaptureOptions(args.poll, args.stable_seconds, args.max_wait, args.pages),
            runtime,
        )
    except (receipt.ReceiptError, rev.RevisionError) as error:
        return _refused(error)
    generated = payload["generated"]
    _emit(
        f"ITERATION {payload['iteration']} ({payload['mode']}, {generated['scope']}): {len(generated['pages'])} pages"
    )
    _emit(f"CAPTURE_SHA256={receipt.receipt_sha256(payload)}")
    _emit("Keep this checksum. Review a separate judgement object outside the package; do not edit iteration.json.")
    _emit("Evidence retained, not a measurement-success verdict. Numeric evidence: unestablished.")
    return EXIT_OK


def cmd_finalize(args: argparse.Namespace, runtime: CaptureRuntime = DEFAULT_RUNTIME) -> int:
    """CLI finalization consumes only separate, strict reviewer input."""
    try:
        payload = receipt.finalize(
            args.package,
            args.capture_sha256,
            receipt.read_strict_json(args.judgement),
            args.iteration,
            state_reader=runtime.state_reader,
        )
    except (receipt.ReceiptError, rev.RevisionError) as error:
        return _refused(error)
    detail = (
        f"outcome {payload['outcome']}" if payload["schema_version"] == 2 else "evidence sealed; no success verdict"
    )
    _emit(f"FINALIZED {payload['iteration']} ({payload['mode']}): {detail}")
    _emit(f"FINAL_SHA256={receipt.receipt_sha256(payload)}")
    _emit("Use this returned checksum as --previous-sha256 for the next iteration.")
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    """Argument errors must not reflect hostile strings or crash legacy consoles."""

    def error(self, message: str) -> None:
        _emit("REFUSED: USAGE: invalid command arguments", stream=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _timing_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--poll", type=float, default=4.0)
    parser.add_argument("--stable-seconds", type=float, default=20.0)
    parser.add_argument("--max-wait", type=float, default=75.0)
    parser.add_argument("--pages", type=_page_ids, help="Exact, comma-separated page IDs")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Keep the standalone grammar; add explicit producer-checksum/reviewer boundaries."""
    parser = _Parser(prog="capture_powerbi_pages.py")
    if argv and argv[0] in SUBCOMMANDS:
        commands = parser.add_subparsers(dest="command", required=True)
        iterate = commands.add_parser("iterate", help="capture a package-local iteration")
        iterate.add_argument("--package", type=Path, required=True)
        iterate.add_argument("--pid", required=True)
        iterate.add_argument("--mode", choices=receipt.MODES)
        iterate.add_argument("--reviewer", default="pbi-migration-validator")
        iterate.add_argument("--session-id")
        iterate.add_argument("--previous-sha256", help="The previous finalize command's returned checksum")
        iterate.add_argument("--refresh", action="store_true", help="Observe a full database-scoped A1 refresh")
        iterate.add_argument("--persist", action="store_true", help="Observe direct A1 ImageSave/readback")
        iterate.add_argument("--canary-table", action="append", default=[], help="Explicit A1 canary table; repeatable")
        _timing_args(iterate)
        finalize = commands.add_parser("finalize", help="seal separate reviewer input against an immutable capture")
        finalize.add_argument("--package", type=Path, required=True)
        finalize.add_argument("--iteration")
        finalize.add_argument("--capture-sha256", required=True, help="The checksum returned by iterate")
        finalize.add_argument(
            "--judgement", type=Path, required=True, help="Separate judgement JSON outside the package"
        )
    else:
        parser.add_argument("report", type=Path)
        parser.add_argument("outdir", type=Path)
        parser.add_argument("--pid", required=True)
        _timing_args(parser)
        parser.set_defaults(command=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; unreadable JSON and console errors never escape as host-path tracebacks."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "iterate":
        return cmd_iterate(args)
    if args.command == "finalize":
        return cmd_finalize(args)
    try:
        return capture_report(
            args.report,
            args.outdir,
            args.pid,
            CaptureOptions(args.poll, args.stable_seconds, args.max_wait, args.pages),
        )
    except (receipt.ReceiptError, rev.RevisionError) as error:
        return _refused(error)


if __name__ == "__main__":
    sys.exit(main())
