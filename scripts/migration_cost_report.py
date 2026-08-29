"""
purpose: report AI spend and model-call elapsed time for migrated Tableau units from Copilot telemetry.
usage:   python scripts/migration_cost_report.py --runs-root _runs
         python scripts/migration_cost_report.py --runs-root migrations
         python scripts/migration_cost_report.py --store %USERPROFILE%\\.copilot\\session-store.db
         python scripts/migration_cost_report.py --runs-root _runs --json

This report is attribution-only. It joins explicit migration run metadata (`run.json`) to the local
Copilot usage store and never tries to backfill old development sessions. A session or agent subtree
with no `run.json` is development work for this purpose and is excluded by construction.

`pollution_note` and `unrelated_work_note` are load-bearing: any non-empty value means the run may
include unrelated work, so the unit is shown but excluded from estate averages.
"""

from __future__ import annotations

# Usage telemetry rows are naturally wide records; keeping them as dataclasses makes the SQLite
# boundary explicit without spreading positional tuples through the rollup code.
# pylint: disable=too-many-instance-attributes

import argparse
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    # pylint: disable-next=no-member  # astroid mis-infers TextIOWrapper.encoding as a class here
    if _stream is not None and _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
NUMERIC_FIELDS = (*TOKEN_FIELDS, "total_nano_aiu", "duration_ms")
DEFAULT_STORE = Path.home() / ".copilot" / "session-store.db"
ROOT_LABEL = "orchestrator/root"
UNKNOWN = "unknown"
MEASURED = "measured"
INFERRED = "inferred"
UNATTRIBUTED = "unattributed"
PARTIAL = "partial"
UNATTRIBUTED_SHARED = "unattributed-shared"
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class UsageEvent:
    """One row from assistant_usage_events."""

    row_id: int
    session_id: str
    agent_id: str | None
    parent_tool_call_id: str | None
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    total_nano_aiu: int
    duration_ms: int
    created_at: datetime


@dataclass(frozen=True)
class AttributionRoot:
    """A run.json attribution anchor."""

    kind: str
    value: str
    outcome: str = UNKNOWN
    evidence: str = MEASURED


@dataclass(frozen=True)
class MigrationRun:
    """The minimal run.json contract this report consumes."""

    path: Path
    name: str
    unit_type: str
    roots: tuple[AttributionRoot, ...]
    fix_rounds: int | None
    polluted: bool
    pollution_note: str
    unit_type_inferred: bool = False
    read_error: str = ""


@dataclass
class Rollup:
    """Aggregated usage for a migration unit or one breakdown bucket."""

    event_ids: set[int] = field(default_factory=set)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_nano_aiu: int = 0
    duration_ms: int = 0
    models: set[str] = field(default_factory=set)
    first_call: datetime | None = None
    last_call: datetime | None = None

    def add(self, event: UsageEvent) -> None:
        """Add one event once."""
        if event.row_id in self.event_ids:
            return
        self.event_ids.add(event.row_id)
        self.input_tokens += event.input_tokens
        self.output_tokens += event.output_tokens
        self.cache_read_tokens += event.cache_read_tokens
        self.cache_write_tokens += event.cache_write_tokens
        self.reasoning_tokens += event.reasoning_tokens
        self.total_nano_aiu += event.total_nano_aiu
        self.duration_ms += event.duration_ms
        self.models.add(event.model)
        self.first_call = event.created_at if self.first_call is None else min(self.first_call, event.created_at)
        self.last_call = event.created_at if self.last_call is None else max(self.last_call, event.created_at)

    @property
    def wall_span_ms(self) -> int:
        """Elapsed wall-clock span between first and last model call."""
        if self.first_call is None or self.last_call is None:
            return 0
        return max(0, int((self.last_call - self.first_call).total_seconds() * 1000))

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable representation."""
        return {
            "event_count": len(self.event_ids),
            "total_nano_aiu": self.total_nano_aiu,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "duration_ms": self.duration_ms,
            "wall_span_ms": self.wall_span_ms,
            "models": sorted(self.models),
            "first_call": self.first_call.isoformat() if self.first_call else None,
            "last_call": self.last_call.isoformat() if self.last_call else None,
        }


@dataclass(frozen=True)
class UnitReport:
    """A cost report for one run.json."""

    run: MigrationRun
    incurred: Rollup
    successful_path: Rollup | None
    by_agent: dict[str, Rollup]
    attribution_status: str
    shared_anchors: tuple[str, ...] = ()

    @property
    def aggregate_eligible(self) -> bool:
        """Whether this unit should contribute to estate averages."""
        return (
            self.attribution_status == MEASURED
            and not self.run.polluted
            and bool(self.incurred.event_ids)
            and self.run.unit_type in {"report", "datasource"}
            and not self.run.read_error
            and not self.shared_anchors
        )


def parse_time(value: str | None) -> datetime:
    """Parse SQLite's ISO-ish timestamps as timezone-aware datetimes."""
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    normalised = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        parsed = datetime.fromisoformat(normalised.split(".")[0]).replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _first_text(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty string-ish value for any key."""
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


# The type classifier deliberately accepts several existing and proposed manifest spellings.
def _normalise_unit_type(run_json: dict[str, Any], path: Path) -> tuple[str, bool]:  # pylint: disable=too-many-return-statements
    """Read report/datasource from run.json, falling back to auditable name/path hints."""
    value = _first_text(
        run_json,
        (
            "unit_type",
            "unit_kind",
            "unitType",
            "artifact_type",
            "item_type",
            "migration_type",
            "kind",
            "type",
            "unitKind",
        ),
    )
    if value:
        lowered = value.lower().replace("_", "-")
        if "datasource" in lowered or "data-source" in lowered:
            return "datasource", False
        if "report" in lowered or "workbook" in lowered:
            return "report", False
    unit_key = _first_text(run_json, ("unit_key", "unit", "slug", "name", "workbook", "datasource"))
    if unit_key:
        lowered = unit_key.lower().replace("\\", "/").replace("_", "-")
        parts = [part for chunk in lowered.split("/") for part in chunk.split(":")]
        if any(part in {"datasource", "datasources", "data-source", "data-sources"} for part in parts):
            return "datasource", True
        if any(part in {"report", "reports", "workbook", "workbooks"} for part in parts):
            return "report", True
        if lowered.startswith(("datasource-", "datasources-")) or lowered.endswith(("-datasource", "-datasources")):
            return "datasource", True
        if lowered.startswith(("report-", "reports-", "workbook-", "workbooks-")):
            return "report", True
    parts = {part.lower() for part in path.parts}
    if "datasources" in parts or "datasource" in parts:
        return "datasource", True
    if "workbooks" in parts or "reports" in parts or "report" in parts:
        return "report", True
    return UNKNOWN, False


def _read_fix_rounds(run_json: dict[str, Any]) -> int | None:
    """Read the optional complexity signal without designing #363's full layout."""
    for key in ("fix_rounds", "fixRounds", "visual_iterations", "visualIterations", "iteration_count"):
        value = run_json.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
    return None


def _pollution_note(run_json: dict[str, Any]) -> tuple[bool, str]:
    """Return whether run.json explicitly says this run mixed unrelated work."""
    for key in ("unrelated_work", "polluted", "mixed_session"):
        if run_json.get(key):
            return True, f"run.json set {key}=true"
    exclusions = run_json.get("exclusions")
    if isinstance(exclusions, dict) and exclusions.get("unrelated_work"):
        return True, "run.json exclusions.unrelated_work=true"
    note = _first_text(run_json, ("pollution_note", "unrelated_work_note"))
    return (bool(note), note or "")


def _root_from_mapping(value: dict[str, Any]) -> AttributionRoot | None:
    """Parse one attribution root entry."""
    outcome = str(value.get("outcome") or UNKNOWN)
    evidence = str(value.get("evidence") or value.get("attribution") or MEASURED)
    session_id = value.get("session_id") or value.get("root_session_id")
    if session_id:
        return AttributionRoot("session", str(session_id), outcome=outcome, evidence=evidence)
    agent_id = value.get("agent_id") or value.get("root_agent_id")
    if agent_id:
        return AttributionRoot("agent", str(agent_id), outcome=outcome, evidence=evidence)
    return None


# Backward-compatible root parsing is branchy because run.json has had several draft shapes.
def _read_roots(run_json: dict[str, Any]) -> tuple[AttributionRoot, ...]:  # pylint: disable=too-many-branches
    """Read measured roots from the current and legacy run.json shapes."""
    roots: list[AttributionRoot] = []
    attribution = run_json.get("attribution")
    if isinstance(attribution, dict):
        raw_roots = attribution.get("roots")
        if isinstance(raw_roots, list):
            for raw_root in raw_roots:
                if isinstance(raw_root, dict) and (root := _root_from_mapping(raw_root)):
                    roots.append(root)
    for key in ("session_id", "copilot_session_id"):
        if run_json.get(key):
            roots.append(AttributionRoot("session", str(run_json[key])))
    if not any(root.kind == "session" for root in roots):
        for key in ("agent_id", "root_agent_id", "copilot_agent_id"):
            if run_json.get(key):
                roots.append(AttributionRoot("agent", str(run_json[key])))
    if not any(root.kind == "session" for root in roots) and isinstance(attribution, dict):
        for key in ("session_id", "root_session_id"):
            if attribution.get(key):
                roots.append(
                    AttributionRoot(
                        "session",
                        str(attribution[key]),
                        outcome=str(attribution.get("outcome") or UNKNOWN),
                        evidence=str(attribution.get("evidence") or MEASURED),
                    )
                )
    if not any(root.kind == "session" for root in roots) and isinstance(attribution, dict):
        for key in ("agent_id", "root_agent_id"):
            if attribution.get(key):
                roots.append(
                    AttributionRoot(
                        "agent",
                        str(attribution[key]),
                        outcome=str(attribution.get("outcome") or UNKNOWN),
                        evidence=str(attribution.get("evidence") or MEASURED),
                    )
                )

    return tuple(dict.fromkeys(roots))


def read_run_json(path: Path) -> MigrationRun:
    """Read one run.json."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        unit_type, unit_type_inferred = _normalise_unit_type({}, path)
        return MigrationRun(
            path=path,
            name=path.parent.name,
            unit_type=unit_type,
            roots=(),
            fix_rounds=None,
            polluted=False,
            pollution_note="",
            unit_type_inferred=unit_type_inferred,
            read_error=f"{type(exc).__name__}: {exc}",
        )
    if not isinstance(data, dict):
        unit_type, unit_type_inferred = _normalise_unit_type({}, path)
        return MigrationRun(
            path=path,
            name=path.parent.name,
            unit_type=unit_type,
            roots=(),
            fix_rounds=None,
            polluted=False,
            pollution_note="",
            unit_type_inferred=unit_type_inferred,
            read_error=f"run.json must contain an object, not {type(data).__name__}",
        )
    unit_name = _first_text(data, ("unit", "slug", "name", "workbook", "datasource")) or path.parent.name
    polluted, note = _pollution_note(data)
    unit_type, unit_type_inferred = _normalise_unit_type(data, path)
    return MigrationRun(
        path=path,
        name=unit_name,
        unit_type=unit_type,
        roots=_read_roots(data),
        fix_rounds=_read_fix_rounds(data),
        polluted=polluted,
        pollution_note=note,
        unit_type_inferred=unit_type_inferred,
    )


def discover_runs(runs_root: Path) -> list[MigrationRun]:
    """Find all run.json files below a root."""
    return [read_run_json(path) for path in sorted(runs_root.rglob("run.json"))]


def load_usage_events(store: Path) -> list[UsageEvent]:
    """Load usage rows from the Copilot session store in read-only mode."""
    uri = f"file:{store}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, session_id, agent_id, parent_tool_call_id, model,
                   input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                   reasoning_tokens, total_nano_aiu, duration_ms, created_at
            FROM assistant_usage_events
            """
        ).fetchall()
    finally:
        connection.close()
    return [
        UsageEvent(
            row_id=int(row["id"]),
            session_id=str(row["session_id"]),
            agent_id=row["agent_id"],
            parent_tool_call_id=row["parent_tool_call_id"],
            model=str(row["model"]),
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cache_read_tokens=int(row["cache_read_tokens"] or 0),
            cache_write_tokens=int(row["cache_write_tokens"] or 0),
            reasoning_tokens=int(row["reasoning_tokens"] or 0),
            total_nano_aiu=int(row["total_nano_aiu"] or 0),
            duration_ms=int(row["duration_ms"] or 0),
            created_at=parse_time(row["created_at"]),
        )
        for row in rows
    ]


def _agent_subtree_ids(root_agent_id: str, events: list[UsageEvent]) -> set[str]:
    """Return only the root agent id.

    The local store records `parent_tool_call_id`, but live data shows it is self-referential for
    agent rows rather than a child-to-parent edge. Agent-only anchors are therefore partial evidence;
    callers must prefer `session_id` when available.
    """
    _ = events
    return {root_agent_id}


def _events_for_root(root: AttributionRoot, events: list[UsageEvent]) -> list[UsageEvent]:
    """Return all events attributable to one session or agent root."""
    if root.kind == "session":
        return [event for event in events if event.session_id == root.value]
    if root.kind == "agent":
        return [event for event in events if event.agent_id in _agent_subtree_ids(root.value, events)]
    return []


def _rollup(events: list[UsageEvent]) -> Rollup:
    """Aggregate events once by row id."""
    result = Rollup()
    for event in events:
        result.add(event)
    return result


def _by_agent(events: list[UsageEvent]) -> dict[str, Rollup]:
    """Aggregate events by agent_id, presenting NULL as orchestrator/root."""
    breakdown: dict[str, Rollup] = {}
    for event in events:
        label = event.agent_id or ROOT_LABEL
        breakdown.setdefault(label, Rollup()).add(event)
    return dict(sorted(breakdown.items()))


def _successful_path(run: MigrationRun, events: list[UsageEvent]) -> Rollup | None:
    """Return the last completed root's rollup when run.json carries outcome metadata."""
    completed_roots = [root for root in run.roots if root.outcome.lower() == "completed"]
    if not completed_roots:
        return None
    return _rollup(_events_for_root(completed_roots[-1], events))


def build_unit_report(run: MigrationRun, events: list[UsageEvent]) -> UnitReport:
    """Build one unit report."""
    if run.read_error:
        return UnitReport(run, Rollup(), None, {}, UNREADABLE)
    if not run.roots:
        return UnitReport(run, Rollup(), None, {}, UNATTRIBUTED)

    matching_events: list[UsageEvent] = []
    inferred = False
    partial = False
    for root in run.roots:
        inferred = inferred or root.evidence.lower() == INFERRED
        partial = partial or root.kind == "agent"
        matching_events.extend(_events_for_root(root, events))

    incurred = _rollup(matching_events)
    status = PARTIAL if partial else INFERRED if inferred else MEASURED
    return UnitReport(
        run=run,
        incurred=incurred,
        successful_path=_successful_path(run, events),
        by_agent=_by_agent(matching_events),
        attribution_status=status,
    )


def flag_shared_anchors(unit_reports: list[UnitReport]) -> list[UnitReport]:
    """Mark units that claim the same attribution root as unattributable shared work."""
    owners: dict[tuple[str, str], list[UnitReport]] = {}
    for unit in unit_reports:
        for root in unit.run.roots:
            owners.setdefault((root.kind, root.value), []).append(unit)

    shared = {root: units for root, units in owners.items() if len({unit.run.path for unit in units}) > 1}
    if not shared:
        return unit_reports

    flagged: list[UnitReport] = []
    for unit in unit_reports:
        collisions = tuple(
            f"{kind}:{value}"
            for kind, value in sorted(shared)
            if any(root.kind == kind and root.value == value for root in unit.run.roots)
        )
        if collisions:
            flagged.append(replace(unit, attribution_status=UNATTRIBUTED_SHARED, shared_anchors=collisions))
        else:
            flagged.append(unit)
    return flagged


def _percentile(values: list[int], percentile: float) -> int:
    """Nearest-rank percentile for small estate samples."""
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def aggregate_by_type(reports: list[UnitReport]) -> dict[str, dict[str, Any]]:
    """Mean / p50 / p90 total_nano_aiu by unit type, excluding polluted and unattributed rows."""
    result: dict[str, dict[str, Any]] = {}
    for unit_type in ("report", "datasource"):
        values = [
            report.incurred.total_nano_aiu
            for report in reports
            if report.aggregate_eligible and report.run.unit_type == unit_type
        ]
        result[unit_type] = {
            "count": len(values),
            "mean_total_nano_aiu": round(sum(values) / len(values)) if values else 0,
            "p50_total_nano_aiu": _percentile(values, 0.50),
            "p90_total_nano_aiu": _percentile(values, 0.90),
        }
    return result


def _format_nano_aiu(value: int) -> str:
    """Format nano AIU with the raw integer first."""
    return f"{value} ({value / 1_000_000_000:.3f} AIU)"


def _format_ms(value: int) -> str:
    """Format milliseconds as a compact duration."""
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {remaining_seconds:.0f}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(remaining_minutes)}m"


def _unit_type_label(run: MigrationRun) -> str:
    """Human-readable unit type with evidence when it came from a name/path heuristic."""
    if run.unit_type_inferred and run.unit_type != UNKNOWN:
        return f"{run.unit_type} (inferred from name)"
    return run.unit_type


def report_as_json(unit_reports: list[UnitReport]) -> dict[str, Any]:
    """JSON-serialisable full report."""
    return {
        "units": [
            {
                "name": unit.run.name,
                "run_json": str(unit.run.path),
                "unit_type": unit.run.unit_type,
                "unit_type_inferred": unit.run.unit_type_inferred,
                "attribution_status": unit.attribution_status,
                "attempts": len(unit.run.roots),
                "fix_rounds": unit.run.fix_rounds,
                "polluted": unit.run.polluted,
                "pollution_note": unit.run.pollution_note,
                "read_error": unit.run.read_error,
                "shared_anchors": list(unit.shared_anchors),
                "incurred": unit.incurred.as_dict(),
                "successful_path": unit.successful_path.as_dict() if unit.successful_path else None,
                "by_agent": {agent: rollup.as_dict() for agent, rollup in unit.by_agent.items()},
            }
            for unit in unit_reports
        ],
        "estate_aggregation": aggregate_by_type(unit_reports),
        "exclusions": [
            "Human review time is not captured.",
            "Power BI Desktop load, refresh, screenshot, and publish time are not captured unless "
            "the model was called.",
            "Development sessions without run.json are excluded by construction.",
            (
                "Sessions or agent roots that also did unrelated work must be flagged in run.json; "
                "flagged units are excluded from estate averages."
            ),
            "Any attribution root claimed by more than one run.json is shared and excluded from estate averages.",
            "Agent-only anchors are partial because parent_tool_call_id is not a reliable child-to-parent edge.",
            "Units with unknown unit_type are shown but excluded from report-vs-datasource averages.",
            ("Retroactive attribution is impossible without a run.json anchor recorded at dispatch/allocation time."),
        ],
    }


# The CLI output keeps each exclusion reason adjacent to the affected unit instead of hiding it.
def _print_table(unit_reports: list[UnitReport]) -> None:  # pylint: disable=too-many-branches
    """Print the human-readable report."""
    print("# Migration AI cost report")
    print()
    print("## Estate aggregation (unpolluted, measured report/datasource units only)")
    aggregation = aggregate_by_type(unit_reports)
    for unit_type, values in aggregation.items():
        print(
            f"- {unit_type}: n={values['count']}, mean={_format_nano_aiu(values['mean_total_nano_aiu'])}, "
            f"p50={_format_nano_aiu(values['p50_total_nano_aiu'])}, "
            f"p90={_format_nano_aiu(values['p90_total_nano_aiu'])}"
        )
    print()
    print("## Units")
    if not unit_reports:
        print("- No run.json files found. No migration sessions are costed.")
    for unit in unit_reports:
        status_bits = [unit.attribution_status]
        if unit.run.polluted:
            status_bits.append("polluted/excluded-from-averages")
        if unit.shared_anchors:
            status_bits.append("shared-anchor/excluded-from-averages")
        if unit.run.unit_type not in {"report", "datasource"}:
            status_bits.append("unit-type-unknown/excluded-from-averages")
        if not unit.incurred.event_ids and unit.attribution_status != UNATTRIBUTED:
            status_bits.append("no-usage-rows")
        print(f"- {unit.run.name} [{_unit_type_label(unit.run)}] ({', '.join(status_bits)})")
        print(f"  - run.json: {unit.run.path}")
        if unit.run.read_error:
            print(f"  - unreadable run.json: {unit.run.read_error}")
        if unit.shared_anchors:
            print(f"  - shared attribution anchors: {', '.join(unit.shared_anchors)}")
        fix_rounds = unit.run.fix_rounds if unit.run.fix_rounds is not None else "unknown"
        print(f"  - attempts: {len(unit.run.roots)}; fix_rounds: {fix_rounds}")
        print(f"  - incurred total_nano_aiu: {_format_nano_aiu(unit.incurred.total_nano_aiu)}")
        if unit.successful_path:
            print(f"  - successful-path total_nano_aiu: {_format_nano_aiu(unit.successful_path.total_nano_aiu)}")
        else:
            print("  - successful-path total_nano_aiu: n/a (no completed root recorded)")
        print(
            f"  - tokens: input={unit.incurred.input_tokens}, output={unit.incurred.output_tokens}, "
            f"cache_read={unit.incurred.cache_read_tokens}, cache_write={unit.incurred.cache_write_tokens}, "
            f"reasoning={unit.incurred.reasoning_tokens}"
        )
        print(
            f"  - model_time={_format_ms(unit.incurred.duration_ms)}; "
            f"telemetry_wall_span={_format_ms(unit.incurred.wall_span_ms)}; "
            f"models={', '.join(sorted(unit.incurred.models)) or 'none'}"
        )
        if unit.run.pollution_note:
            print(f"  - unrelated-work flag: {unit.run.pollution_note}")
        if unit.by_agent:
            print("  - by agent:")
            for agent_id, rollup in unit.by_agent.items():
                print(
                    f"    - {agent_id}: {_format_nano_aiu(rollup.total_nano_aiu)}, "
                    f"duration={_format_ms(rollup.duration_ms)}, calls={len(rollup.event_ids)}"
                )
        else:
            print("  - by agent: none")
    print()
    print("## Exclusions and caveats")
    for exclusion in report_as_json(unit_reports)["exclusions"]:
        print(f"- {exclusion}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=Path("."),
        help="Root to scan recursively for _runs/**/run.json or any run.json (default: current directory).",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_STORE,
        help="Path to Copilot session-store.db (default: %%USERPROFILE%%\\.copilot\\session-store.db).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(list(argv or sys.argv[1:]))
    runs_root = args.runs_root.resolve()
    store = Path(os.path.expandvars(str(args.store))).expanduser().resolve()
    if not runs_root.exists():
        print(f"ERROR: runs root does not exist: {runs_root}", file=sys.stderr)
        return 2
    if not store.is_file():
        print(f"ERROR: Copilot session store does not exist: {store}", file=sys.stderr)
        return 2

    runs = discover_runs(runs_root)
    events = load_usage_events(store)
    unit_reports = flag_shared_anchors([build_unit_report(run, events) for run in runs])
    if args.json:
        print(json.dumps(report_as_json(unit_reports), indent=2, sort_keys=True))
    else:
        _print_table(unit_reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
