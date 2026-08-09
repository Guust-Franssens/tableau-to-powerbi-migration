"""
purpose: tell an orchestrator whether a delegated migration is PROGRESSING or SPINNING, from the
         artifacts on disk rather than from the subagent's own narrative.
usage:   python scripts/check_migration_progress.py --bundle <dir> [--since-minutes 20] [--json]
         python scripts/check_migration_progress.py --bundle <dir> --baseline 2026-08-09T20:00:00+02:00

Why this exists
---------------
Measured 2026-08-07, four migrations running in parallel. Two of them passed 100 minutes on their
first turn. Elapsed time could not tell them apart:

* one had written **27 model files and 148 report files** and was still going - it had 30 stubbed
  calculations to author, which is genuinely the hardest work in the corpus;
* the other had written **zero report files in 105 minutes**, and its whole report tree still carried
  a single emission timestamp, while it accumulated screenshots and DAX probes in a scratch folder.

Both looked identical to a stopwatch and to a "still running" status. What separated them is
**deliverable output over time**: edits to the semantic model and the report, as opposed to activity
in scratch. That file signal is still incomplete during read-heavy triage, so callers can add the
runtime signal with `--liveness active` when the tool-call count is climbing.

This is deliberately NOT a kill switch. Its output is a prompt to ASK - the orchestrator's job when
this reports STALLED is to make the subagent report what it is blocked on, which is the thing that
did not happen for 105 minutes.

⚠️ **A stall is not a failure, and a fast run is not a success.** A migration can legitimately go
quiet while Power BI Desktop loads a model (~60-90s) or an XMLA refresh runs (measured 93s), and a
correct early STOP - an unreachable source, a missing credential - produces almost no artifacts at
all and is the RIGHT outcome. Read the verdict together with what the run was asked to do.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

LOG = logging.getLogger("check_migration_progress")

# Buckets, in the order a migration produces them. `deliverable` is what the user gets; `scratch` is
# how the agent got there. The distinction IS the measurement - see the module docstring.
# Matched against path COMPONENTS relative to the bundle, never against the absolute path.
DELIVERABLE_SUFFIXES = (".semanticmodel", ".report", ".pbip")
SCRATCH_DIRS = frozenset({"scratch", "_work", "_build", "_probe", "tmp", "temp", "_shots"})
SCRATCH_INTENTS = frozenset(part.lstrip("._") for part in SCRATCH_DIRS)

# A Desktop model load is 60-90s and a refresh + ImageSave was measured at 93s, so a window shorter
# than this cannot distinguish "loading" from "stuck" and must not try.
SHORT_WINDOW_MINUTES = 10
LIVENESS_ACTIVE = "active"
LIVENESS_UNKNOWN = "unknown"


def parse_baseline(value: str) -> datetime:
    """Parse the delegation baseline supplied by the orchestrator."""
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as ex:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 baseline: {value}") from ex
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def classify(path: Path) -> str:
    """Bucket one path: deliverable, scratch, or other.

    ⚠️ **Pass a path RELATIVE to the bundle, and match on path COMPONENTS.** The first version
    lower-cased the absolute path and substring-matched it, which meant any bundle living under
    `...\\AppData\\Local\\Temp\\` had every file classified as scratch - and so would a customer
    working in `C:\\work\\migrations\\`, which would report STALLED forever regardless of output.
    Component matching also stops `temp` matching `template` and `tmp` matching `tmpl`.
    """
    parts = [part.lower() for part in path.parts]
    if any(part.lstrip("._") in SCRATCH_INTENTS for part in parts):
        return "scratch"
    if any(part.endswith(DELIVERABLE_SUFFIXES) for part in parts):
        return "deliverable"
    return "other"


def scan(bundle: Path, since: datetime, baseline: datetime | None = None) -> dict[str, Any]:
    """Count files by bucket within the window, and record the newest write in each.

    `newest_overall` is tracked separately and ignores the window, because "nothing in the last 30
    minutes" has two very different meanings: a run that never started (the delegation failed) and a
    run that went quiet an hour ago (finished, blocked on a human, or dead). Without it the tool
    reports "has this run started?" for a bundle that has been working all evening.
    """
    buckets: dict[str, dict[str, Any]] = {
        b: {"count": 0, "newest": None, "example": None} for b in ("deliverable", "scratch", "other")
    }
    overall_buckets: dict[str, dict[str, Any]] = {
        b: {"count": 0, "newest": None, "example": None} for b in ("deliverable", "scratch", "other")
    }
    total = 0
    newest_overall: datetime | None = None
    cutoff = max(since, baseline) if baseline else since
    for path in bundle.rglob("*"):
        if not path.is_file():
            continue
        try:
            written = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        total += 1
        if baseline and written < baseline:
            continue
        relative = path.relative_to(bundle)
        bucket_name = classify(relative)
        if newest_overall is None or written > newest_overall:
            newest_overall = written
        overall_bucket = overall_buckets[bucket_name]
        overall_bucket["count"] += 1
        if overall_bucket["newest"] is None or written > overall_bucket["newest"]:
            overall_bucket["newest"] = written
            overall_bucket["example"] = str(relative)
        if written < cutoff:
            continue
        bucket = buckets[bucket_name]
        bucket["count"] += 1
        if bucket["newest"] is None or written > bucket["newest"]:
            bucket["newest"] = written
            bucket["example"] = str(relative)
    return {
        "buckets": buckets,
        "overall_buckets": overall_buckets,
        "files_total": total,
        "newest_overall": newest_overall,
        "baseline": baseline,
    }


def _age_minutes(written: datetime) -> float:
    """Minutes since a filesystem write."""
    return (datetime.now() - written).total_seconds() / 60


def verdict(scanned: dict[str, Any], window_minutes: int, liveness: str = LIVENESS_UNKNOWN) -> tuple[str, str]:
    """Turn the counts into a verdict and the sentence an orchestrator should act on.

    Four outcomes, and the two middle ones are the point:

    * PROGRESSING  - deliverables are being written; leave it alone.
    * THINKING     - only scratch activity, but the window is too SHORT to judge on yet.
    * STALLED      - scratch activity across a full window and NO deliverable. This is the shape that
                     burned 105 minutes: busy, and producing nothing the user asked for.
    * SILENT       - nothing at all. Either finished, blocked on a human, or dead.

    ⚠️ **Recency must NOT rescue a window with zero deliverables.** The first version tested "last
    write < 180s ago -> THINKING", which meant an agent touching a scratch file every 30 seconds for
    105 minutes read THINKING forever - the exact run this tool was written for. Recency only
    separates STALLED from SILENT; the WINDOW decides whether the absence of output is meaningful.
    """
    deliverable = scanned["buckets"]["deliverable"]
    scratch = scanned["buckets"]["scratch"]
    other = scanned["buckets"]["other"]
    prior_deliverable = scanned.get("overall_buckets", {}).get("deliverable", {})

    if liveness == LIVENESS_ACTIVE and not deliverable["count"]:
        newest_overall = scanned.get("newest_overall")
        file_signal = "no file writes" if not newest_overall else f"last write {_age_minutes(newest_overall):.0f}m ago"
        state, detail = (
            "THINKING",
            (
                f"{file_signal}, but the external runtime liveness signal is active (tool-call count is climbing). "
                "Treat this as a read-heavy phase and re-check; file mtimes alone cannot prove a stall."
            ),
        )
    elif deliverable["count"]:
        state, detail = "PROGRESSING", f"{deliverable['count']} deliverable file(s) in the last {window_minutes}m."
    elif not scratch["count"] and not other["count"]:
        if prior_deliverable.get("newest"):
            age = _age_minutes(prior_deliverable["newest"])
            state, detail = (
                "THINKING",
                (
                    f"no deliverables in the last {window_minutes}m, but this bundle already has deliverable output "
                    f"(last {age:.0f}m ago: {prior_deliverable.get('example')}). Agents write in bursts; "
                    "use this age and runtime liveness before interrupting it."
                ),
            )
        else:
            # `newest_overall` deliberately ignores the window - see scan().
            newest_overall = scanned.get("newest_overall")
            if newest_overall is None:
                baseline_note = " after baseline" if scanned.get("baseline") else " at all"
                state, detail = "SILENT", f"no files written{baseline_note} - has this run started?"
            else:
                quiet = (datetime.now() - newest_overall).total_seconds()
                state, detail = (
                    "SILENT",
                    (
                        f"nothing written for {quiet / 60:.0f}m. Finished, blocked on a human, or dead - "
                        "check whether it is waiting on a credential before assuming it is stuck."
                    ),
                )
    elif window_minutes < SHORT_WINDOW_MINUTES:
        state, detail = (
            "THINKING",
            (
                f"{scratch['count']} scratch file(s), nothing deliverable yet, but a {window_minutes}m "
                f"window is too short to judge (a Desktop load is ~90s and a refresh ~93s). Re-check "
                f"over >= {SHORT_WINDOW_MINUTES}m."
            ),
        )
    else:
        state, detail = (
            "STALLED",
            (
                f"{scratch['count']} scratch file(s) but ZERO deliverables in {window_minutes}m. "
                "Busy and producing nothing the user asked for - ASK IT WHAT IT IS BLOCKED ON."
            ),
        )
    return state, detail


def render(bundle: Path, scanned: dict[str, Any], state: str, detail: str, window: int) -> str:
    """The human-readable check-in."""
    lines = [f"PROGRESS [{state}] {bundle.name} - {detail}", ""]
    for name in ("deliverable", "scratch", "other"):
        bucket = scanned["buckets"][name]
        when = bucket["newest"].strftime("%H:%M:%S") if bucket["newest"] else "-"
        lines.append(
            f"  {name:12} {bucket['count']:>4} file(s) in last {window}m   newest {when}  {bucket['example'] or ''}"
        )
    if state == "STALLED":
        lines += [
            "",
            "  The file signal says this run is busy but not producing deliverables.",
            "  Do NOT kill it - make it report what it is blocked on,",
            "  which is the thing that does not happen on its own.",
        ]
    return "\n".join(lines)


def newest_write(root: Path, suffixes: tuple[str, ...]) -> tuple[datetime | None, Path | None]:
    """The most recent write among files with these suffixes, and which file it was."""
    newest, which = None, None
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes:
            try:
                written = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                continue
            if newest is None or written > newest:
                newest, which = written, path
    return newest, which


def handoff_ready(bundle: Path) -> tuple[str, list[str]]:
    """Is every semantic model in this bundle SAFE to hand to the report builder?

    Two failures, both measured on a real parallel run, and both invisible to the report builder:

    1. **No cache at all.** Desktop opens an empty model, and the agent's natural next move is to
       trigger its own refresh - minutes of cost, and on a live source a modal credential prompt that
       stops the run outright.
    2. **A cache OLDER than the TMDL.** The model was edited after it was persisted, so the warm data
       no longer matches the schema on disk. This is the more dangerous one: something loads, so
       nothing looks wrong.

    There is a third, measured 2026-08-07 at 22:19 against a cache written at 22:22: the report side
    opened Desktop **two minutes before the cache existed**. The cache was correct and the handoff
    gate had run; the ORDER was wrong. That is precisely what this check exists to make impossible -
    the orchestrator runs it before assigning the report phase, not after.
    """
    problems: list[str] = []
    models = sorted(p for p in bundle.rglob("*.SemanticModel") if p.is_dir())
    if not models:
        return "NO_MODEL", ["no *.SemanticModel directory in this bundle - nothing to hand over"]

    for model in models:
        rel = model.relative_to(bundle)
        cache = model / ".pbi" / "cache.abf"
        tmdl_at, tmdl_file = newest_write(model, (".tmdl",))
        if not cache.is_file():
            problems.append(
                f"{rel}: NO cache.abf - the report builder will open an EMPTY model and is likely "
                "to trigger its own refresh (minutes, and a credential prompt on a live source)"
            )
            continue
        cache_at = datetime.fromtimestamp(cache.stat().st_mtime)
        if tmdl_at and cache_at < tmdl_at:
            problems.append(
                f"{rel}: cache.abf is STALE - persisted {cache_at:%H:%M:%S} but "
                f"{tmdl_file.name if tmdl_file else 'TMDL'} was edited {tmdl_at:%H:%M:%S}. "
                "Something will load, so nothing will look wrong. Re-refresh and re-save."
            )

    if problems:
        return "NOT_READY", problems
    return "READY", [f"{len(models)} model(s) carry a cache that post-dates their TMDL"]


def main(argv: list[str] | None = None) -> int:
    """Exit 0 PROGRESSING/THINKING/READY, 1 STALLED/NOT_READY, 2 SILENT/NO_MODEL."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, type=Path, help="the migration's output directory")
    parser.add_argument("--since-minutes", type=int, default=20, help="observation window (default 20)")
    parser.add_argument(
        "--baseline",
        type=parse_baseline,
        help="delegation timestamp (ISO-8601); ignore files older than this so setup artifacts are not agent progress",
    )
    parser.add_argument(
        "--liveness",
        choices=(LIVENESS_UNKNOWN, LIVENESS_ACTIVE, "idle"),
        default=LIVENESS_UNKNOWN,
        help="external runtime signal; use active when tool-call count rose since the last poll",
    )
    parser.add_argument(
        "--handoff",
        action="store_true",
        help="instead of progress: is this bundle safe to hand to the report builder?",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.bundle.is_dir():
        LOG.error("PROGRESS: ERROR no such bundle: %s", args.bundle)
        return 2

    if args.handoff:
        state, notes = handoff_ready(args.bundle)
        if args.json:
            sys.stdout.write(json.dumps({"bundle": str(args.bundle), "state": state, "notes": notes}, indent=2) + "\n")
        else:
            LOG.info("HANDOFF [%s] %s", state, args.bundle.name)
            for note in notes:
                LOG.info("  %s", note)
        return {"READY": 0, "NOT_READY": 1, "NO_MODEL": 2}[state]

    since = datetime.now() - timedelta(minutes=args.since_minutes)
    scanned = scan(args.bundle, since, args.baseline)
    state, detail = verdict(scanned, args.since_minutes, args.liveness)

    if args.json:
        payload = {
            "bundle": str(args.bundle),
            "state": state,
            "detail": detail,
            "window_minutes": args.since_minutes,
            "baseline": args.baseline.isoformat(timespec="seconds") if args.baseline else None,
            "liveness": args.liveness,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "buckets": {
                name: {
                    "count": b["count"],
                    "newest": b["newest"].isoformat(timespec="seconds") if b["newest"] else None,
                    "example": b["example"],
                }
                for name, b in scanned["buckets"].items()
            },
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        LOG.info("%s", render(args.bundle, scanned, state, detail, args.since_minutes))

    return {"PROGRESSING": 0, "THINKING": 0, "STALLED": 1, "SILENT": 2}[state]


if __name__ == "__main__":
    sys.exit(main())
