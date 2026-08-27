"""
purpose: after ONE machine-wide Power BI sign-in, re-run the reachability probe across MANY blocked
         credential-gate units and let each gate EARN its own clear where the probe now passes.
         This tool NEVER clears, authorizes, forces or mass-authorizes anything itself.
usage:   python scripts/reprobe_blocked.py --unit <dir> [--unit <dir> ...]          # explicit
         python scripts/reprobe_blocked.py --units-from <file>                      # one path per line
         python scripts/credential_gate.py list <root> --json | \
             python scripts/reprobe_blocked.py --stdin --apply                      # compose with `list`
         (DRY RUN by default - prints what WOULD be probed; pass --apply to actually run the probe)

The problem this solves (issue #344)
------------------------------------
Power BI caches a credential MACHINE-WIDE (DPAPI) once a human signs in interactively - verified in
`docs/data-source-credentials.md`. So after ONE sign-in, every gated unit sharing that data source
may now be reachable. But every `credential_gate.py` subcommand takes exactly ONE migration, so a
44-unit estate offers no way to say "credentials changed; re-check everything blocked and let through
whatever now passes." The only visible route was N hand-typed `authorize` calls - which PERMANENTLY
stamps every unit UNVALIDATED, including the ones whose credentials now work, destroying the very
quality signal the gate exists to produce.

The one hard rule: this tool never lifts a gate
------------------------------------------------
Clearing a gate must remain a CONSEQUENCE of a real probe success, recorded as `probe-cleared` by the
existing machinery in `probe_live_source.py` (`run_probe` -> `_lift_gate` -> `credential_gate.py clear
--earned`). This tool therefore does exactly one thing to each blocked unit: it runs the real probe
(`probe_live_source.py --bundle <unit>`) and then READS the ground-truth gate state. It writes no
marker, no override, no audit line, and edits no ACL. The whole value of the feature is converting
`unearned -> earned by measurement`; a shortcut that cleared a gate itself would destroy the only
signal it exists to produce. It also never authorizes, and never mass-authorizes - `authorize` stays
a one-human, one-deliberate-decision, agent-hostile command, untouched.

Why DRY RUN is the default
--------------------------
A probe opens Power BI Desktop and can take minutes per unit (a suspended Snowflake warehouse
cold-starting on a 1-row probe was measured at 167s). Across a 44-unit estate an accidental full
sweep is an expensive, surprising, hours-long operation with a real (if earned) side effect on gate
state. Dry run shows exactly which units are BLOCKED and would be probed, and which are skipped and
why, so a human sees the blast radius before committing. `--apply` is the explicit, obvious action.

Bounded: this tool IS the supervising timer - do not wrap it in a 2-minute cap
------------------------------------------------------------------------------
The probe (`probe_live_source.py`) is the PRIMARY, self-bounding timer: it caps its own refresh phase
(default 390s) and internally bounds Desktop open + catalog load, so ONE open->catalog->refresh
attempt is ~870s worst case (its own docstring). This sweep is a SUPERVISOR of N such probes, run
SEQUENTIALLY (never in parallel - concurrent Desktop instances exhaust the machine). Each unit is
probed EXACTLY ONCE per sweep; a missing credential is a final answer, never retried. The per-unit
`--per-unit-timeout-sec` is a wall-clock BACKSTOP for a wedged Desktop, sized to comfortably outlast
one probe attempt and the 167s cold-start floor, so it never fires before the probe's own
better-classified deadline. It is NOT a 2-minute cap - applying one here would kill a legitimately
cold-starting warehouse. If a killed probe leaked a Desktop instance, `check_desktop_orphans.py` finds
it; this tool never closes a Desktop it did not personally open.

A discovered, pre-existing defect this sweep SURFACES (does not mask)
--------------------------------------------------------------------
Measured 2026-08-27: when the gate was armed with NAMED sources (which `preflight_source_credentials.py
classify` does), a successful probe does NOT actually lift the gate. `run_probe` calls
`_lift_gate(migration, "N live source(s)")`, whose summary string is passed as `clear --earned
--sources "N live source(s)"`; that never matches the marker's real source names, so `clear_block`
takes its PARTIAL-clear branch and leaves the marker and ACL in place while recording a phantom
`probe-cleared`. The single-unit path clears correctly ONLY when the gate was armed with EMPTY
`--sources`. This is a defect in `probe_live_source._lift_gate` + `credential_gate.clear_block`, both
outside this file's ownership, so it is reported (not patched here). This sweep is honest about it: a
unit whose probe returns DATA_OK yet whose gate is STILL armed afterwards is classified `anomaly`
(not misreported as `still-blocked: NO_CREDENTIAL`), because ground truth - the gate transition -
wins over the probe's self-reported verdict.

Per-unit outcomes and exit codes
--------------------------------
Each unit lands in exactly one category:

    newly-earned  the gate went DOWN (blocked -> not blocked): a real, measured, earned clear.
    still-blocked the probe ran and the gate stayed up for a SOURCE reason - reason distinguishes
                  NO_CREDENTIAL (a human must sign in) from UNREACHABLE (a spec/DNS fix, no sign-in)
                  from ACCESS_DENIED / BAD_TABLE / OPERATOR_REQUIRED, which have different fixes.
    anomaly       DATA_OK but the gate stayed up (the defect above), or SKIPPED-no-live on an armed
                  unit: a tooling/spec problem, not a source verdict. Do NOT authorize past it.
    errored       the probe could not run, timed out at the backstop, or produced no verdict line -
                  the measurement did not happen, so nothing was learned about the source.
    skipped       not blocked (nothing to do), or a non-BLOCKED `list` state, or no longer blocked
                  at probe time.
    would-probe   DRY-RUN only: this unit is blocked and WOULD be probed under --apply.

Exit code (precedence: forged > anomaly/errored > blocked > clean; magnitude is an id, not a rank):

    0  clean          nothing left blocked (or nothing was blocked to begin with)
    1  blocked         >=1 unit still BLOCKED for a source reason (or, in dry run, >=1 would-probe)
    2  usage           bad/absent arguments (argparse convention)
    3  forged-override a FORGED-OVERRIDE state was present in the input (from `list --json`); security
    5  anomaly         >=1 unit errored or returned DATA_OK-but-gate-stuck (measurement/tooling problem)
    (4 is intentionally unused: it is the probe's OPERATOR_REQUIRED exit; this sweep folds
     operator-required into exit 1 and never emits 4.)

What is NOT unit-tested here
----------------------------
The real Desktop-touching seam - `_default_probe_runner` actually launching `probe_live_source.py`
against Power BI Desktop - is NOT exercised by the unit tests (it needs Desktop, a live warehouse and
a cached credential). Tests inject a fake runner and drive every classification/exit branch from
canned outcomes plus real marker-file transitions. The probe's verdict/exit contract that this parser
depends on was verified manually (SKIPPED exit 0, UNREACHABLE exit 1, `--bundle <dir>` accepts a
directory); see the module docstring of `probe_live_source.py` for the full outcome vocabulary.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from credential_gate import MARKER as GATE_MARKER

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("reprobe_blocked")

_PROBE_SCRIPT = Path(__file__).resolve().parent / "probe_live_source.py"

# The probe prints its structural verdict as `PROBE: <VERDICT> <detail>`. It ALSO prints trailing
# summary lines that begin with `PROBE:` but are NOT verdicts (e.g. `PROBE: source index 0 failed`),
# so a naive "last PROBE: line" read picks the wrong token. Matching only this known vocabulary - and
# taking the LAST match - selects the real verdict, and the final source's verdict in a multi-source
# probe. Kept in sync with `probe_live_source.py`'s documented outcomes.
KNOWN_VERDICTS = frozenset(
    {"DATA_OK", "SKIPPED", "NO_CREDENTIAL", "ACCESS_DENIED", "UNREACHABLE", "BAD_TABLE", "OPERATOR_REQUIRED", "ERROR"}
)

# Source-side reasons the gate legitimately stays armed. Each needs a DIFFERENT fix, so they are
# reported distinctly rather than collapsed into "still blocked".
STILL_BLOCKED_REASON = {
    "NO_CREDENTIAL": (
        "reachable but Power BI has no credential - a HUMAN must sign in interactively, then re-run "
        "this sweep. Not retryable by automation."
    ),
    "UNREACHABLE": (
        "address/network fault (e.g. DNS) - fix `server`/`http_path` in the spec; NO sign-in will "
        "help. This is not a credential problem."
    ),
    "ACCESS_DENIED": (
        "authenticated but permission was refused - permissions must change; signing in again is not enough."
    ),
    "BAD_TABLE": "the probed table name was not found at the source - a spec/name error, not a reachability one.",
    "OPERATOR_REQUIRED": (
        "custom-SQL source - a human must open the probe PBIP in Power BI Desktop and refresh; the "
        "sweep will not run an arbitrary customer query."
    ),
}

# Verdicts that mean the gate stayed up despite a probe that thought it succeeded, or found nothing to
# do: a tooling/spec problem, not a source verdict. See the module docstring's "_lift_gate" note.
ANOMALY_REASON = {
    "DATA_OK": (
        "probe returned DATA_OK but the gate is STILL armed - the probe's own clear did not take (a "
        "known _lift_gate/clear_block source-mismatch defect, filed separately). This is NOT a "
        "credential problem; do NOT authorize past it. It needs a tooling fix."
    ),
    "SKIPPED": (
        "the gate is armed but the probe found no live source to probe - investigate why it was armed "
        "(a spec/target mismatch); the sweep cannot resolve this."
    ),
}

CAT_NEWLY_EARNED = "newly-earned"
CAT_STILL_BLOCKED = "still-blocked"
CAT_ANOMALY = "anomaly"
CAT_ERRORED = "errored"
CAT_SKIPPED = "skipped"
CAT_WOULD_PROBE = "would-probe"

# Display order for the per-unit lines and the summary counts.
CATEGORY_ORDER = (
    CAT_NEWLY_EARNED,
    CAT_STILL_BLOCKED,
    CAT_ANOMALY,
    CAT_ERRORED,
    CAT_WOULD_PROBE,
    CAT_SKIPPED,
)

EXIT_CLEAN = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2
EXIT_FORGED = 3
EXIT_ANOMALY = 5

FORGED_STATE = "FORGED-OVERRIDE"
BLOCKED_STATE = "BLOCKED"

# Wall-clock BACKSTOP per unit. The probe self-bounds one open->catalog->refresh attempt at ~870s
# worst case and a suspended-warehouse cold start was measured at 167s, so 1200s comfortably outlasts
# a healthy probe without firing first. It is a backstop for a WEDGED Desktop, not a primary timer and
# emphatically not a 2-minute cap - a naive short cap here would kill a legitimately cold-starting
# source. A unit that genuinely needs several table attempts can exceed it; raise it for that unit
# rather than lowering it globally. `--per-unit-timeout-sec 0` disables the backstop entirely.
DEFAULT_PER_UNIT_TIMEOUT_SEC = 1200


@dataclass(frozen=True)
class UnitInput:
    """One requested unit: an absolute-or-relative directory, plus optional `list --json` metadata."""

    path: Path
    state: str | None = None
    relative: str | None = None


@dataclass(frozen=True)
class SweepOptions:
    """Runtime knobs threaded to the probe runner and the sweep."""

    apply: bool
    refresh_timeout_sec: int | None
    per_unit_timeout_sec: int


@dataclass(frozen=True)
class ProbeOutcome:
    """What running the probe against one unit produced. `returncode` is None on a supervisory kill."""

    returncode: int | None
    verdict: str | None
    timed_out: bool
    detail: str


@dataclass(frozen=True)
class UnitResult:
    """The classified outcome for one unit."""

    unit: Path
    category: str
    verdict: str | None
    reason: str
    elapsed_sec: float


@dataclass(frozen=True)
class SweepReport:
    """The whole sweep's results plus the two facts the exit code needs."""

    results: list[UnitResult]
    forged: bool
    applied: bool


def _parse_verdict(text: str) -> str | None:
    """Return the LAST recognised `PROBE: <VERDICT>` token in `text`, or None if there is none."""
    verdict: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        marker = "PROBE:"
        if marker not in stripped:
            continue
        token = stripped.split(marker, 1)[1].strip().split(None, 1)[0] if stripped else ""
        if token in KNOWN_VERDICTS:
            verdict = token
    return verdict


def _verdict_line(text: str) -> str:
    """The first recognised `PROBE: <VERDICT> ...` line, trimmed, for use as a human detail string."""
    for line in text.splitlines():
        stripped = line.strip()
        if "PROBE:" not in stripped:
            continue
        token = stripped.split("PROBE:", 1)[1].strip().split(None, 1)[0]
        if token in KNOWN_VERDICTS:
            return stripped[:300]
    return ""


def unit_is_blocked(unit: Path) -> bool:
    """Ground-truth 'is the gate still armed here?', derived from the marker artifact.

    Uses `credential_gate.MARKER` presence - the exact file the probe's own earned clear removes on
    success. Deliberately NOT a re-implementation of the gate's full state machine: it does not
    adjudicate override authenticity (that is `credential_gate.py verify`'s job). An `authorize`d unit
    has no marker (authorize removes it), so marker-presence already excludes that case; a forged
    override is surfaced from `list --json` input state instead.
    """
    return (unit / GATE_MARKER).exists()


def _units_from_json(payload: object) -> list[UnitInput]:
    """Parse the `credential_gate.py list --json` shape, a bare array, or a list of path strings."""
    if isinstance(payload, dict):
        items = payload.get("units", [])
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    units: list[UnitInput] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str) and item.strip():
            units.append(UnitInput(path=Path(item.strip())))
        elif isinstance(item, dict):
            raw = item.get("unit") or item.get("path")
            if raw:
                state = item.get("state")
                relative = item.get("relative")
                units.append(
                    UnitInput(
                        path=Path(str(raw)),
                        state=str(state) if state is not None else None,
                        relative=str(relative) if relative is not None else None,
                    )
                )
    return units


def read_units_source(text: str) -> list[UnitInput]:
    """Turn a text blob (JSON in the `list` shape, a JSON array, or newline paths) into UnitInputs."""
    stripped = text.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return [
            UnitInput(path=Path(line.strip()))
            for line in stripped.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    return _units_from_json(payload)


def _read_stdin() -> str:
    """Read stdin explicitly (the user asked for it via --stdin or --units-from -)."""
    return sys.stdin.read()


def _maybe_read_stdin() -> str | None:
    """Read piped stdin when no other source was given; None on a TTY or when stdin is unavailable."""
    stream = sys.stdin
    try:
        if stream is None or stream.isatty():
            return None
        return stream.read()
    except (OSError, ValueError):  # pytest replaces stdin with a stream that raises on read
        return None


def gather_units(args: argparse.Namespace) -> list[UnitInput]:
    """Collect units from --unit, --units-from (file or -), --stdin, or an auto-detected pipe."""
    units = [UnitInput(path=Path(raw)) for raw in (args.unit or [])]
    text: str | None = None
    if args.stdin or args.units_from == "-":
        text = _read_stdin()
    elif args.units_from:
        text = Path(args.units_from).read_text(encoding="utf-8")
    elif not units:
        text = _maybe_read_stdin()
    if text:
        units.extend(read_units_source(text))
    return units


def _skip(unit: Path, reason: str) -> UnitResult:
    """A unit the sweep will not probe, with the reason it was skipped."""
    return UnitResult(unit=unit, category=CAT_SKIPPED, verdict=None, reason=reason, elapsed_sec=0.0)


def _classify_selection(unit: UnitInput, resolved: Path) -> tuple[bool, UnitResult | None, bool]:
    """Decide selection for one resolved unit: (probe_it, skip_result, forged).

    `list --json` state is authoritative for classification, so FORGED-OVERRIDE flags the sweep
    (highest-severity exit) even when this process cannot see the directory. A BLOCKED state (or a
    bare path) additionally re-verifies the marker is present RIGHT NOW - a unit cleared by a
    concurrent probe since it was listed must not be re-probed (verify before repeating).
    """
    state = (unit.state or "").strip()
    if state == FORGED_STATE:
        return (
            False,
            _skip(resolved, "state=FORGED-OVERRIDE (from list) - run `credential_gate.py verify`; not re-probing"),
            True,
        )
    if state and state != BLOCKED_STATE:
        return False, _skip(resolved, f"state={state} (from list); not re-probing"), False
    if not resolved.is_dir():
        return False, _skip(resolved, f"not a directory: {resolved}"), False
    if not unit_is_blocked(resolved):
        reason = (
            "listed BLOCKED but the marker is gone now (cleared since listing)"
            if state
            else "not blocked (no marker present)"
        )
        return False, _skip(resolved, reason), False
    return True, None, False


def select(units: list[UnitInput]) -> tuple[list[UnitInput], list[UnitResult], bool]:
    """Split inputs into (to_probe, pre_skipped, forged_seen), de-duplicating by resolved path.

    A unit carrying a `list --json` state is probed iff that state is BLOCKED and its marker is still
    present; FORGED-OVERRIDE is skipped AND flags the sweep; every other state is skipped with its
    state as the reason. A bare path (no state) is probed iff its BLOCKED marker is present right now.
    """
    to_probe: list[UnitInput] = []
    skipped: list[UnitResult] = []
    forged = False
    seen: set[Path] = set()
    for unit in units:
        resolved = unit.path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        probe_it, skip_result, unit_forged = _classify_selection(unit, resolved)
        forged = forged or unit_forged
        if probe_it:
            to_probe.append(UnitInput(path=resolved, state=BLOCKED_STATE, relative=unit.relative))
        elif skip_result is not None:
            skipped.append(skip_result)
    return to_probe, skipped, forged


def _default_probe_runner(unit: Path, options: SweepOptions) -> ProbeOutcome:
    """Run the REAL probe as a subprocess and capture its verdict. The only Desktop-touching seam.

    Delegates entirely to `probe_live_source.py --bundle <unit>`, whose own machinery records
    `probe-cleared` and lifts the gate on DATA_OK. This function NEVER clears, authorizes, or touches
    a gate file itself - it constructs a `probe_live_source.py` command and nothing else.
    `options.per_unit_timeout_sec` is a supervisory wall-clock backstop over the probe's own refresh
    timer; on expiry the child is killed and the unit is recorded ERRORED. NOT unit-tested (needs
    Power BI Desktop); tests inject a fake runner instead.
    """
    cmd = [sys.executable, str(_PROBE_SCRIPT), "--bundle", str(unit)]
    if options.refresh_timeout_sec is not None:
        cmd += ["--refresh-timeout-sec", str(options.refresh_timeout_sec)]
    timeout = (
        options.per_unit_timeout_sec if options.per_unit_timeout_sec and options.per_unit_timeout_sec > 0 else None
    )
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        return ProbeOutcome(
            returncode=None,
            verdict=_parse_verdict(partial if isinstance(partial, str) else ""),
            timed_out=True,
            detail=(
                f"probe exceeded the {timeout}s supervisory backstop and was killed; Power BI Desktop "
                "may have leaked - check `check_desktop_orphans.py`"
            ),
        )
    combined = (proc.stdout or "") + (proc.stderr or "")
    verdict = _parse_verdict(combined)
    detail = _verdict_line(combined) or f"probe exited {proc.returncode} with no recognised PROBE verdict line"
    return ProbeOutcome(returncode=proc.returncode, verdict=verdict, timed_out=False, detail=detail)


def classify(outcome: ProbeOutcome, blocked_after: bool) -> tuple[str, str | None, str]:
    """Map a probe outcome + ground-truth gate state to (category, verdict, reason). Pure."""
    verdict = outcome.verdict
    if not blocked_after:
        # Ground truth: the gate is DOWN. A DATA_OK probe is the only thing that EARNS that on this
        # path, so DATA_OK -> newly-earned. If the gate is down but the probe did NOT report DATA_OK,
        # it was lowered out-of-band (e.g. a concurrent `authorize`/`clear`) - report that honestly
        # rather than crediting this probe with an earned clear it did not make.
        if verdict == "DATA_OK":
            return (
                CAT_NEWLY_EARNED,
                verdict,
                "gate went down: probe reached the source and earned its own probe-cleared",
            )
        return (
            CAT_ANOMALY,
            verdict,
            "gate is down but the probe did not report DATA_OK - it was cleared out-of-band (e.g. a "
            "concurrent authorize/clear), not earned by this probe. Run `credential_gate.py verify`.",
        )
    if outcome.timed_out:
        return CAT_ERRORED, verdict, outcome.detail
    if verdict in ANOMALY_REASON:
        return CAT_ANOMALY, verdict, ANOMALY_REASON[verdict]
    if verdict in STILL_BLOCKED_REASON:
        return CAT_STILL_BLOCKED, verdict, STILL_BLOCKED_REASON[verdict]
    return CAT_ERRORED, verdict, outcome.detail or "probe produced no recognised PROBE verdict line"


def probe_unit(unit: UnitInput, runner, options: SweepOptions) -> UnitResult:
    """Probe one blocked unit once and classify it from the ground-truth gate transition."""
    path = unit.path
    if not unit_is_blocked(path):
        # Re-checked at probe time: the gate may have cleared since it was listed (verify before
        # repeating). Probing a unit that is no longer blocked would waste a Desktop launch.
        return _skip(path, "no longer blocked at probe time (cleared since it was listed)")
    start = time.monotonic()
    try:
        outcome = runner(path, options)
    except OSError as exc:
        return UnitResult(
            unit=path,
            category=CAT_ERRORED,
            verdict=None,
            reason=f"probe could not be launched: {exc}",
            elapsed_sec=time.monotonic() - start,
        )
    elapsed = time.monotonic() - start
    category, verdict, reason = classify(outcome, unit_is_blocked(path))
    if elapsed > 60:
        log.info("  %s took %.0fs", path, elapsed)
    return UnitResult(unit=path, category=category, verdict=verdict, reason=reason, elapsed_sec=elapsed)


def sweep(units: list[UnitInput], options: SweepOptions, runner=_default_probe_runner) -> SweepReport:
    """Select the blocked units and, when `options.apply`, probe each ONCE, sequentially."""
    to_probe, skipped, forged = select(units)
    if not options.apply:
        would = [
            UnitResult(
                unit=unit.path,
                category=CAT_WOULD_PROBE,
                verdict=unit.state,
                reason="blocked; would be re-probed (pass --apply to run)",
                elapsed_sec=0.0,
            )
            for unit in to_probe
        ]
        return SweepReport(results=would + skipped, forged=forged, applied=False)
    results = [probe_unit(unit, runner, options) for unit in to_probe]
    return SweepReport(results=results + skipped, forged=forged, applied=True)


def exit_code(report: SweepReport) -> int:
    """Scriptable exit code. Precedence: forged > anomaly/errored > blocked/would-probe > clean."""
    if report.forged:
        return EXIT_FORGED
    categories = {result.category for result in report.results}
    if CAT_ERRORED in categories or CAT_ANOMALY in categories:
        return EXIT_ANOMALY
    if CAT_STILL_BLOCKED in categories or CAT_WOULD_PROBE in categories:
        return EXIT_BLOCKED
    return EXIT_CLEAN


def _summary_counts(results: list[UnitResult]) -> dict[str, int]:
    """Count results per category, in display order."""
    counts = {category: 0 for category in CATEGORY_ORDER}
    for result in results:
        counts[result.category] = counts.get(result.category, 0) + 1
    return {category: count for category, count in counts.items() if count}


def _sorted_results(results: list[UnitResult]) -> list[UnitResult]:
    """Order results by category (display order) then path, for a stable readable report."""
    rank = {category: index for index, category in enumerate(CATEGORY_ORDER)}
    return sorted(results, key=lambda result: (rank.get(result.category, len(CATEGORY_ORDER)), str(result.unit)))


def render_text(report: SweepReport) -> str:
    """Human-readable report to stdout."""
    header = "APPLIED (probes were run)" if report.applied else "DRY RUN - no probes executed; pass --apply to run"
    lines = [f"credential-gate re-probe sweep - {header}", "=" * 78]
    if report.forged:
        lines.append("!! FORGED-OVERRIDE state present in input - run `credential_gate.py verify` on it. !!")
    counts = _summary_counts(report.results)
    lines.append("  ".join(f"{category}={count}" for category, count in counts.items()) or "(no units)")
    lines.append("-" * 78)
    for result in _sorted_results(report.results):
        verdict = f" {result.verdict}" if result.verdict else ""
        elapsed = f" ({result.elapsed_sec:.0f}s)" if result.elapsed_sec else ""
        lines.append(f"  [{result.category}]{verdict}{elapsed} {result.unit}")
        lines.append(f"      {result.reason}")
    return "\n".join(lines)


def render_json(report: SweepReport) -> str:
    """Machine-readable report to stdout."""
    payload = {
        "applied": report.applied,
        "forged_override_present": report.forged,
        "summary": _summary_counts(report.results),
        "exit_code": exit_code(report),
        "units": [
            {
                "unit": str(result.unit),
                "category": result.category,
                "verdict": result.verdict,
                "reason": result.reason,
                "elapsed_sec": round(result.elapsed_sec, 2),
            }
            for result in _sorted_results(report.results)
        ],
    }
    return json.dumps(payload, indent=2)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Define and parse the CLI."""
    parser = argparse.ArgumentParser(
        description="Re-probe blocked credential-gate units after a sign-in; the probe earns its own clear.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--unit", action="append", metavar="DIR", help="a gated unit directory (repeatable)")
    parser.add_argument(
        "--units-from",
        metavar="FILE",
        help="read unit paths, a JSON array, or `list --json` from FILE (use - for stdin)",
    )
    parser.add_argument("--stdin", action="store_true", help="read unit paths or `list --json` from stdin")
    parser.add_argument("--apply", action="store_true", help="actually run the probe (default: dry-run preview)")
    parser.add_argument(
        "--refresh-timeout-sec",
        type=int,
        default=None,
        help="passed through to the probe's refresh timer (default: the probe's own default)",
    )
    parser.add_argument(
        "--per-unit-timeout-sec",
        type=int,
        default=DEFAULT_PER_UNIT_TIMEOUT_SEC,
        help=f"supervisory wall-clock backstop per unit (default {DEFAULT_PER_UNIT_TIMEOUT_SEC}; 0 disables)",
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable JSON report on stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, runner=None) -> int:
    """CLI entry point. `runner` is an injection seam for tests; production uses the real probe."""
    args = _parse_args(argv)
    units = gather_units(args)
    if not units:
        log.error(
            "no units given. Pass --unit <dir> (repeatable), --units-from <file|->, or pipe "
            "`credential_gate.py list <root> --json` and add --stdin."
        )
        return EXIT_USAGE
    options = SweepOptions(
        apply=args.apply, refresh_timeout_sec=args.refresh_timeout_sec, per_unit_timeout_sec=args.per_unit_timeout_sec
    )
    if options.apply:
        log.info("APPLYING: opening Power BI Desktop and probing each blocked unit sequentially. This tool is the")
        log.info("supervising timer - do not wrap it in an external 2-minute cap; the probe self-bounds each refresh.")
    report = sweep(units, options, runner=runner or _default_probe_runner)
    print(render_json(report) if args.json else render_text(report))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
