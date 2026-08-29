"""Tests for scripts/migration_cost_report.py."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import migration_cost_report as report  # noqa: E402  # pylint: disable=wrong-import-position

SCRATCH = REPO_ROOT / ".test-scratch" / "migration_cost_report"


@pytest.fixture(name="case_dir")
def fixture_case_dir() -> Path:
    """Create a repo-local scratch directory; do not rely on system temp paths."""
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    try:
        yield SCRATCH
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


def _write_run(case_dir: Path, name: str, payload: dict) -> Path:
    run_dir = case_dir / "_runs" / name
    run_dir.mkdir(parents=True)
    path = run_dir / "run.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _create_store(case_dir: Path) -> Path:
    store = case_dir / "session-store.db"
    connection = sqlite3.connect(store)
    try:
        connection.execute(
            """
            CREATE TABLE assistant_usage_events (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                turn_index INTEGER,
                agent_id TEXT,
                parent_tool_call_id TEXT,
                model TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                reasoning_tokens INTEGER,
                total_nano_aiu INTEGER,
                request_multiplier REAL,
                duration_ms INTEGER,
                time_to_first_token_ms INTEGER,
                inter_token_latency_ms INTEGER,
                initiator TEXT,
                api_endpoint TEXT,
                reasoning_effort TEXT,
                finish_reason TEXT,
                content_filter_triggered INTEGER,
                token_details_json TEXT,
                created_at TEXT,
                output_ttft_ms REAL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    return store


def _insert_event(
    store: Path,
    row_id: int,
    session_id: str,
    agent_id: str | None,
    parent_tool_call_id: str | None,
    total_nano_aiu: int,
    created_at: str,
) -> None:
    connection = sqlite3.connect(store)
    try:
        connection.execute(
            """
            INSERT INTO assistant_usage_events (
                id, session_id, turn_index, agent_id, parent_tool_call_id, model,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
                total_nano_aiu, duration_ms, created_at
            )
            VALUES (?, ?, 1, ?, ?, 'claude-opus-5', 10, 20, 30, 40, 50, ?, 1000, ?)
            """,
            (row_id, session_id, agent_id, parent_tool_call_id, total_nano_aiu, created_at),
        )
        connection.commit()
    finally:
        connection.close()


def _build_reports(case_dir: Path, store: Path) -> list[report.UnitReport]:
    runs = report.discover_runs(case_dir / "_runs")
    events = report.load_usage_events(store)
    return report.flag_shared_anchors([report.build_unit_report(run, events) for run in runs])


def test_rollup_arithmetic_against_fixture_database(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(
        case_dir,
        "001-sales",
        {"unit": "sales", "unit_type": "report", "session_id": "session-one", "fix_rounds": 2},
    )
    _insert_event(store, 1, "session-one", None, None, 100, "2026-08-27T08:00:00Z")
    _insert_event(store, 2, "session-one", "agent-a", "root-tool", 200, "2026-08-27T08:00:10Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.incurred.total_nano_aiu == 300
    assert unit.incurred.input_tokens == 20
    assert unit.incurred.output_tokens == 40
    assert unit.incurred.cache_read_tokens == 60
    assert unit.incurred.cache_write_tokens == 80
    assert unit.incurred.reasoning_tokens == 100
    assert unit.incurred.duration_ms == 2000
    assert unit.incurred.wall_span_ms == 10_000
    assert unit.run.fix_rounds == 2
    assert set(unit.by_agent) == {report.ROOT_LABEL, "agent-a"}
    assert unit.by_agent[report.ROOT_LABEL].total_nano_aiu == 100


def test_agent_only_anchor_is_partial_when_store_edges_are_self_references(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(
        case_dir,
        "002-agent-only",
        {
            "unit": "agent-only",
            "unit_type": "report",
            "attribution": {"roots": [{"agent_id": "root-agent", "outcome": "completed"}]},
        },
    )
    _insert_event(store, 1, "dev-session", None, None, 999, "2026-08-27T08:00:00Z")
    _insert_event(store, 2, "mixed-session", "root-agent", "root-agent", 100, "2026-08-27T08:00:01Z")
    _insert_event(store, 3, "mixed-session", "child-agent", "child-agent", 200, "2026-08-27T08:00:02Z")
    _insert_event(store, 4, "mixed-session", "validator-agent", "validator-agent", 300, "2026-08-27T08:00:03Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.attribution_status == report.PARTIAL
    assert unit.incurred.total_nano_aiu == 100
    assert unit.successful_path is not None
    assert unit.successful_path.total_nano_aiu == 100
    assert set(unit.by_agent) == {"root-agent"}
    assert not unit.aggregate_eligible


def test_run_json_with_agent_and_session_prefers_session_anchor(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(
        case_dir,
        "002-both",
        {
            "unit": "both",
            "unit_type": "report",
            "attribution": {"roots": [{"agent_id": "root-agent", "session_id": "session-one", "outcome": "completed"}]},
        },
    )
    _insert_event(store, 1, "session-one", None, None, 100, "2026-08-27T08:00:00Z")
    _insert_event(store, 2, "session-one", "root-agent", "root-agent", 200, "2026-08-27T08:00:01Z")
    _insert_event(store, 3, "session-one", "child-agent", "child-agent", 300, "2026-08-27T08:00:02Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.run.roots == (report.AttributionRoot("session", "session-one", outcome="completed"),)
    assert unit.attribution_status == report.MEASURED
    assert unit.incurred.total_nano_aiu == 600
    assert unit.aggregate_eligible


def test_run_json_without_session_or_agent_is_reported_unattributed(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(case_dir, "003-unattributed", {"unit": "no-anchor", "unit_type": "datasource"})
    _insert_event(store, 1, "session-one", None, None, 100, "2026-08-27T08:00:00Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.attribution_status == report.UNATTRIBUTED
    assert unit.incurred.total_nano_aiu == 0
    assert unit.by_agent == {}


def test_development_session_without_run_json_never_appears(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(case_dir, "004-migration", {"unit": "migration", "unit_type": "report", "session_id": "migration"})
    _insert_event(store, 1, "migration", None, None, 100, "2026-08-27T08:00:00Z")
    _insert_event(store, 2, "development", None, None, 900, "2026-08-27T08:00:01Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.run.name == "migration"
    assert unit.incurred.total_nano_aiu == 100
    assert "development" not in {root.value for root in unit.run.roots}


def test_crash_resume_roots_sum_incurred_and_report_completed_path(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(
        case_dir,
        "005-resume",
        {
            "unit": "resume",
            "unit_type": "report",
            "attribution": {
                "roots": [
                    {"session_id": "crashed-session", "outcome": "unknown"},
                    {"session_id": "completed-session", "outcome": "completed"},
                ]
            },
        },
    )
    _insert_event(store, 1, "crashed-session", None, None, 100, "2026-08-27T08:00:00Z")
    _insert_event(store, 2, "completed-session", None, None, 300, "2026-08-27T08:01:00Z")

    [unit] = _build_reports(case_dir, store)

    assert len(unit.run.roots) == 2
    assert unit.incurred.total_nano_aiu == 400
    assert unit.successful_path is not None
    assert unit.successful_path.total_nano_aiu == 300


def test_crash_resume_keeps_mixed_session_and_agent_attempts(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(
        case_dir,
        "005-mixed-resume",
        {
            "unit": "mixed-resume",
            "unit_type": "report",
            "attribution": {
                "roots": [
                    {"session_id": "crashed-session", "outcome": "crashed"},
                    {"agent_id": "completed-agent", "outcome": "completed"},
                ]
            },
        },
    )
    _insert_event(store, 1, "crashed-session", None, None, 60, "2026-08-27T08:00:00Z")
    _insert_event(store, 2, "resume-session", "completed-agent", "completed-agent", 10, "2026-08-27T08:01:00Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.run.roots == (
        report.AttributionRoot("session", "crashed-session", outcome="crashed"),
        report.AttributionRoot("agent", "completed-agent", outcome="completed"),
    )
    assert unit.attribution_status == report.PARTIAL
    assert len(unit.run.roots) == 2
    assert unit.incurred.total_nano_aiu == 70
    assert unit.successful_path is not None
    assert unit.successful_path.total_nano_aiu == 10
    assert not unit.aggregate_eligible


def test_polluted_units_are_flagged_and_excluded_from_estate_averages(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(case_dir, "006-clean", {"unit": "clean", "unit_type": "report", "session_id": "clean"})
    _write_run(
        case_dir,
        "007-polluted",
        {"unit": "polluted", "unit_type": "report", "session_id": "polluted", "unrelated_work": True},
    )
    _insert_event(store, 1, "clean", None, None, 100, "2026-08-27T08:00:00Z")
    _insert_event(store, 2, "polluted", None, None, 900, "2026-08-27T08:00:01Z")

    units = _build_reports(case_dir, store)
    aggregation = report.aggregate_by_type(units)

    assert {unit.run.name: unit.run.polluted for unit in units} == {"clean": False, "polluted": True}
    assert aggregation["report"]["count"] == 1
    assert aggregation["report"]["mean_total_nano_aiu"] == 100


def test_pollution_note_marks_unit_polluted_and_excludes_from_average(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(case_dir, "008-clean", {"unit": "clean", "unit_type": "report", "session_id": "clean"})
    _write_run(
        case_dir,
        "009-noted",
        {
            "unit": "noted",
            "unit_type": "report",
            "session_id": "noted",
            "pollution_note": "operator also answered two unrelated questions in this session",
        },
    )
    _insert_event(store, 1, "clean", None, None, 100, "2026-08-27T08:00:00Z")
    _insert_event(store, 2, "noted", None, None, 900, "2026-08-27T08:00:01Z")

    units = _build_reports(case_dir, store)
    by_name = {unit.run.name: unit for unit in units}
    aggregation = report.aggregate_by_type(units)

    assert by_name["noted"].run.polluted
    assert by_name["noted"].run.pollution_note.startswith("operator also")
    assert not by_name["noted"].aggregate_eligible
    assert aggregation["report"]["count"] == 1
    assert aggregation["report"]["mean_total_nano_aiu"] == 100


def test_two_units_claiming_one_session_are_shared_and_excluded(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(case_dir, "008-alpha", {"unit": "alpha", "unit_type": "report", "session_id": "shared"})
    _write_run(case_dir, "009-beta", {"unit": "beta", "unit_type": "report", "session_id": "shared"})
    _write_run(case_dir, "010-gamma", {"unit": "gamma", "unit_type": "report", "session_id": "gamma"})
    _insert_event(store, 1, "shared", None, None, 60, "2026-08-27T08:00:00Z")
    _insert_event(store, 2, "gamma", None, None, 20, "2026-08-27T08:00:01Z")

    units = _build_reports(case_dir, store)
    by_name = {unit.run.name: unit for unit in units}
    aggregation = report.aggregate_by_type(units)

    assert by_name["alpha"].attribution_status == report.UNATTRIBUTED_SHARED
    assert by_name["beta"].attribution_status == report.UNATTRIBUTED_SHARED
    assert by_name["alpha"].shared_anchors == ("session:shared",)
    assert by_name["beta"].shared_anchors == ("session:shared",)
    assert by_name["gamma"].attribution_status == report.MEASURED
    assert aggregation["report"]["count"] == 1
    assert aggregation["report"]["mean_total_nano_aiu"] == 20


def test_unit_type_can_come_from_canonical_unit_key(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(case_dir, "011-key", {"unit_key": "workbooks-sales", "session_id": "session-one"})
    _insert_event(store, 1, "session-one", None, None, 100, "2026-08-27T08:00:00Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.run.unit_type == "report"
    assert unit.run.unit_type_inferred
    assert report._unit_type_label(unit.run) == "report (inferred from name)"  # pylint: disable=protected-access
    assert report.aggregate_by_type([unit])["report"]["count"] == 1


def test_unit_type_can_come_from_sanitised_datasource_unit_key(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(case_dir, "011-datasource-key", {"unit_key": "orders-datasource", "session_id": "session-one"})
    _insert_event(store, 1, "session-one", None, None, 100, "2026-08-27T08:00:00Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.run.unit_type == "datasource"
    assert unit.run.unit_type_inferred
    assert report.aggregate_by_type([unit])["datasource"]["count"] == 1


def test_unknown_unit_type_is_visible_but_excluded(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(case_dir, "012-unknown", {"unit_key": "sales", "session_id": "session-one"})
    _insert_event(store, 1, "session-one", None, None, 100, "2026-08-27T08:00:00Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.run.unit_type == report.UNKNOWN
    assert unit.incurred.total_nano_aiu == 100
    assert report.aggregate_by_type([unit])["report"]["count"] == 0


def test_malformed_run_json_is_reported_without_aborting_other_units(case_dir: Path) -> None:
    store = _create_store(case_dir)
    bad_dir = case_dir / "_runs" / "013-bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "run.json").write_text("{", encoding="utf-8")
    _write_run(case_dir, "014-good", {"unit": "good", "unit_type": "datasource", "session_id": "good"})
    _insert_event(store, 1, "good", None, None, 100, "2026-08-27T08:00:00Z")

    units = _build_reports(case_dir, store)
    by_name = {unit.run.name: unit for unit in units}

    assert by_name["013-bad"].attribution_status == report.UNREADABLE
    assert "JSONDecodeError" in by_name["013-bad"].run.read_error
    assert by_name["good"].incurred.total_nano_aiu == 100


def test_non_object_run_json_is_reported_unreadable(case_dir: Path) -> None:
    store = _create_store(case_dir)
    run_dir = case_dir / "_runs" / "015-list"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("[]", encoding="utf-8")

    [unit] = _build_reports(case_dir, store)

    assert unit.attribution_status == report.UNREADABLE
    assert "must contain an object" in unit.run.read_error


def test_non_utf8_run_json_is_unreadable_without_aborting_other_units(case_dir: Path) -> None:
    store = _create_store(case_dir)
    utf16_dir = case_dir / "_runs" / "016-utf16"
    utf16_dir.mkdir(parents=True)
    (utf16_dir / "run.json").write_text('{"unit": "utf16"}', encoding="utf-16")
    latin1_dir = case_dir / "_runs" / "017-latin1"
    latin1_dir.mkdir(parents=True)
    (latin1_dir / "run.json").write_bytes('{"unit": "café"}'.encode("latin-1"))
    _write_run(case_dir, "018-good", {"unit": "good", "unit_type": "report", "session_id": "good"})
    _insert_event(store, 1, "good", None, None, 100, "2026-08-27T08:00:00Z")

    units = _build_reports(case_dir, store)
    by_name = {unit.run.name: unit for unit in units}

    assert by_name["016-utf16"].attribution_status == report.UNREADABLE
    assert "UnicodeDecodeError" in by_name["016-utf16"].run.read_error
    assert by_name["017-latin1"].attribution_status == report.UNREADABLE
    assert "UnicodeDecodeError" in by_name["017-latin1"].run.read_error
    assert by_name["good"].incurred.total_nano_aiu == 100


def test_utf8_bom_run_json_is_read_as_valid_json(case_dir: Path) -> None:
    store = _create_store(case_dir)
    bom_dir = case_dir / "_runs" / "019-bom"
    bom_dir.mkdir(parents=True)
    (bom_dir / "run.json").write_text(
        json.dumps({"unit": "bom", "unit_type": "report", "session_id": "bom"}),
        encoding="utf-8-sig",
    )
    _insert_event(store, 1, "bom", None, None, 100, "2026-08-27T08:00:00Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.attribution_status == report.MEASURED
    assert unit.incurred.total_nano_aiu == 100


def test_percentiles_use_nearest_rank(case_dir: Path) -> None:
    units: list[report.UnitReport] = []
    for index, value in enumerate([10, 20, 30, 40], start=1):
        run = report.MigrationRun(
            path=case_dir / f"{index}.json",
            name=f"unit-{index}",
            unit_type="report",
            roots=(report.AttributionRoot("session", f"session-{index}"),),
            fix_rounds=None,
            polluted=False,
            pollution_note="",
        )
        rollup = report.Rollup()
        rollup.total_nano_aiu = value
        rollup.event_ids.add(index)
        units.append(report.UnitReport(run, rollup, None, {}, report.MEASURED))

    aggregation = report.aggregate_by_type(units)["report"]

    assert aggregation["p50_total_nano_aiu"] == 20
    assert aggregation["p90_total_nano_aiu"] == 40


def test_inferred_and_zero_usage_units_are_excluded_from_averages(case_dir: Path) -> None:
    measured_run = report.MigrationRun(
        path=case_dir / "measured.json",
        name="measured",
        unit_type="report",
        roots=(report.AttributionRoot("session", "measured"),),
        fix_rounds=None,
        polluted=False,
        pollution_note="",
    )
    measured = report.Rollup()
    measured.total_nano_aiu = 50
    measured.event_ids.add(1)
    inferred = report.Rollup()
    inferred.total_nano_aiu = 500
    inferred.event_ids.add(2)
    zero = report.Rollup()
    units = [
        report.UnitReport(measured_run, measured, None, {}, report.MEASURED),
        report.UnitReport(measured_run, inferred, None, {}, report.INFERRED),
        report.UnitReport(measured_run, zero, None, {}, report.MEASURED),
    ]

    aggregation = report.aggregate_by_type(units)["report"]

    assert aggregation["count"] == 1
    assert aggregation["mean_total_nano_aiu"] == 50


def test_rollup_deduplicates_event_rows(case_dir: Path) -> None:
    event = report.UsageEvent(
        row_id=1,
        session_id="session-one",
        agent_id=None,
        parent_tool_call_id=None,
        model="claude-opus-5",
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_tokens=40,
        reasoning_tokens=50,
        total_nano_aiu=100,
        duration_ms=1000,
        created_at=report.parse_time("2026-08-27T08:00:00Z"),
    )

    rollup = report._rollup([event, event])  # pylint: disable=protected-access

    assert rollup.total_nano_aiu == 100
    assert rollup.input_tokens == 10


def test_pollution_note_marks_polluted_even_without_boolean_flag(case_dir: Path) -> None:
    store = _create_store(case_dir)
    _write_run(
        case_dir,
        "016-note",
        {
            "unit": "note",
            "unit_type": "report",
            "session_id": "session-one",
            "pollution_note": "reviewed: possible unrelated work occurred",
        },
    )
    _insert_event(store, 1, "session-one", None, None, 100, "2026-08-27T08:00:00Z")

    [unit] = _build_reports(case_dir, store)

    assert unit.run.polluted
    assert unit.run.pollution_note == "reviewed: possible unrelated work occurred"
    assert not unit.aggregate_eligible
