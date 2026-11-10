"""
purpose: validate assessment evidence and publish one deterministic offline HTML inventory.
usage:   python -B scripts/assessment_report.py --run <assessment-run>
internal: true
internal-reason: invoked only as the fourth child of the assessment-only front door.
"""

from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import logging
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tableau_lineage import load_survey  # noqa: E402  # pylint: disable=wrong-import-position

INPUTS = ("estate_survey.json", "assessment.json", "estate.db", "admin_insights.json")
REQUIRED_TABLES = {
    "workbook",
    "view",
    "datasource",
    "dependency",
    "project",
    "assessment_run",
    "listing_error",
    "upstream_table",
    "grp",
    "permission",
    "flow",
}
COLLECTOR_REASONS = {
    "datasource_not_found",
    "permission_denied",
    "vds_unavailable",
    "required_fields_missing",
    "ambiguous_datasource",
    "transport_failure",
    "malformed_response",
    "sign_in_failed",
}
COLLECTOR_OUTCOMES = {
    "datasource_not_found": "unavailable",
    "permission_denied": "unavailable",
    "vds_unavailable": "unavailable",
    "required_fields_missing": "unavailable",
    "ambiguous_datasource": "cannot_establish",
    "transport_failure": "cannot_establish",
    "malformed_response": "cannot_establish",
    "sign_in_failed": "cannot_establish",
}
WEIGHTS = {"sheets": 1.0, "dashboards": 2.0, "calcs": 1.0, "lods": 5.0, "table_calcs": 5.0}


class EvidenceError(ValueError):
    """Fixed-vocabulary refusal for absent, malformed or inconsistent evidence."""


class _DigestParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.digest: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        values = dict(attrs)
        if values.get("name") == "evidence-sha256":
            self.digest = values.get("content")


def _read_bytes(path: Path, *, optional: bool = False) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        if optional:
            return None
        raise EvidenceError("evidence_missing") from None
    except (OSError, ValueError):
        raise EvidenceError("evidence_unrecognized") from None


def _json_bytes(raw: bytes, reason: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise EvidenceError(reason) from None


def _db_read(path: Path) -> dict[str, Any]:
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not REQUIRED_TABLES.issubset(tables):
                raise EvidenceError("assessment_unrecognized")
            run_rows = list(
                connection.execute(
                    "SELECT assessed_at, degraded, degraded_primary, workbooks_total, listing_errors "
                    "FROM assessment_run"
                )
            )
            if len(run_rows) != 1:
                raise EvidenceError("assessment_unrecognized")
            permission_count_row = connection.execute("SELECT COUNT(*) FROM permission").fetchone()
            if permission_count_row is None:
                raise EvidenceError("assessment_unrecognized")
            permission_count = permission_count_row[0]
            if not isinstance(permission_count, int) or isinstance(permission_count, bool) or permission_count < 0:
                raise EvidenceError("assessment_unrecognized")
            permission_denied_listings = None
            if "permission_collection" in tables:
                denied_rows = list(connection.execute("SELECT denied_listings FROM permission_collection"))
                if len(denied_rows) != 1 or (
                    denied_rows[0][0] is not None
                    and (
                        not isinstance(denied_rows[0][0], int)
                        or isinstance(denied_rows[0][0], bool)
                        or denied_rows[0][0] < 0
                    )
                ):
                    raise EvidenceError("assessment_unrecognized")
                permission_denied_listings = denied_rows[0][0]
            return {
                "run": dict(run_rows[0]),
                "permission_count": permission_count,
                "permission_denied_listings": permission_denied_listings,
                "workbooks": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT luid, name, project, project_luid, sheets, dashboards, calcs, lods, "
                        "table_calcs, has_user_reference, complexity, complexity_understated, "
                        "views_lifetime, view_count, subscriptions, alerts, custom_views, rank, "
                        "cumulative_share, tier, tier_reason FROM workbook ORDER BY rank, luid"
                    )
                ],
                "views": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT luid, workbook_luid, name, content_url, views_lifetime, updated_at "
                        "FROM view ORDER BY luid"
                    )
                ],
                "datasources": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT luid, name, project, project_luid, is_certified, has_extracts, "
                        "extract_last_refresh FROM datasource ORDER BY luid"
                    )
                ],
                "dependencies": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT workbook_name, workbook_luid, datasource_name, datasource_luid, source "
                        "FROM dependency ORDER BY workbook_luid, datasource_luid, datasource_name"
                    )
                ],
                "projects": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT luid, name, parent_luid, content_permissions FROM project ORDER BY luid"
                    )
                ],
                "listing_errors": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT listing, severity, status FROM listing_error ORDER BY listing, severity, status"
                    )
                ],
            }
        finally:
            connection.close()
    except EvidenceError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        raise EvidenceError("assessment_unrecognized") from None


def _collector(raw: bytes | None, *, degraded_primary: bool) -> dict[str, Any]:  # pylint: disable=too-many-branches
    if raw is None:
        reason = "degraded_primary" if degraded_primary else "collector_no_result"
        return {
            "outcome": "not_collected",
            "reason": reason,
            "window": {"first_day": None, "last_day": None},
            "rows": [],
            "unmapped_events": None,
        }
    value = _json_bytes(raw, "admin_insights_unrecognized")
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "outcome",
        "reason",
        "datasource_luid",
        "window",
        "rows",
        "unmapped_events",
    }:
        raise EvidenceError("admin_insights_unrecognized")
    outcome, reason = value.get("outcome"), value.get("reason")
    if outcome not in {"available", "unavailable", "cannot_establish"}:
        raise EvidenceError("admin_insights_unrecognized")
    if outcome == "available":
        if reason is not None:
            raise EvidenceError("admin_insights_unrecognized")
    elif reason not in COLLECTOR_REASONS or COLLECTOR_OUTCOMES.get(reason) != outcome:
        raise EvidenceError("admin_insights_unrecognized")
    if value.get("schema") != "admin-insights-aggregate/1" or not isinstance(value.get("rows"), list):
        raise EvidenceError("admin_insights_unrecognized")
    window = value.get("window")
    if (
        not isinstance(value.get("datasource_luid"), (str, type(None)))
        or not isinstance(window, dict)
        or set(window) != {"first_day", "last_day"}
        or any(window.get(key) is not None and not isinstance(window.get(key), str) for key in window)
        or not isinstance(value.get("unmapped_events"), int)
    ):
        raise EvidenceError("admin_insights_unrecognized")
    rows = []
    for row in value["rows"]:
        if not isinstance(row, dict) or set(row) != {
            "event_type",
            "event_name",
            "item_type",
            "item_luid",
            "workbook_luid",
            "day",
            "events",
        }:
            raise EvidenceError("admin_insights_unrecognized")
        if not all(
            isinstance(row.get(key), str) for key in ("event_type", "event_name", "item_type", "item_luid", "day")
        ):
            raise EvidenceError("admin_insights_unrecognized")
        if row.get("workbook_luid") is not None and not isinstance(row["workbook_luid"], str):
            raise EvidenceError("admin_insights_unrecognized")
        if not isinstance(row.get("events"), int) or isinstance(row.get("events"), bool) or row["events"] < 0:
            raise EvidenceError("admin_insights_unrecognized")
        rows.append(row)
    if isinstance(value["unmapped_events"], bool) or value["unmapped_events"] < 0:
        raise EvidenceError("admin_insights_unrecognized")
    if outcome == "available":
        days = [row["day"] for row in rows]
        if (
            not value["datasource_luid"]
            or window["first_day"] != (min(days) if days else None)
            or window["last_day"] != (max(days) if days else None)
        ):
            raise EvidenceError("admin_insights_unrecognized")
    elif rows or value["unmapped_events"] or window != {"first_day": None, "last_day": None}:
        raise EvidenceError("admin_insights_unrecognized")
    return {**value, "rows": rows}


def load_evidence(  # pylint: disable=too-many-branches,too-many-locals,too-many-boolean-expressions
    directory: Path | str,
) -> dict[str, Any]:
    """Read only fixed evidence paths and return allowlisted, reconciled values."""
    root = Path(directory)
    survey_raw = _read_bytes(root / INPUTS[0])
    assessment_raw = _read_bytes(root / INPUTS[1])
    database_raw = _read_bytes(root / INPUTS[2])
    collector_raw = _read_bytes(root / INPUTS[3], optional=True)
    try:
        survey = load_survey(root / INPUTS[0])
        survey_data = _json_bytes(survey_raw or b"", "survey_unrecognized")
    except Exception as exc:  # noqa: BLE001 - any producer/schema change is one fixed refusal.
        del exc
        raise EvidenceError("survey_unrecognized") from None
    survey_workbooks = survey_data.get("workbooks") if isinstance(survey_data, dict) else None
    if not isinstance(survey_workbooks, list):
        raise EvidenceError("survey_unrecognized")
    survey_luids = []
    for row in survey_workbooks:
        if not isinstance(row, dict) or not isinstance(row.get("luid"), str) or not row["luid"]:
            raise EvidenceError("survey_unrecognized")
        survey_luids.append(row["luid"])

    assessment = _json_bytes(assessment_raw or b"", "assessment_unrecognized")
    summary = assessment.get("summary") if isinstance(assessment, dict) else None
    workbooks = assessment.get("workbooks") if isinstance(assessment, dict) else None
    if (
        not isinstance(summary, dict)
        or not isinstance(workbooks, list)
        or not isinstance(assessment.get("degraded_primary"), bool)
        or not isinstance(summary.get("degraded_primary"), bool)
        or assessment["degraded_primary"] != summary["degraded_primary"]
        or not isinstance(summary.get("workbooks_total"), int)
        or isinstance(summary.get("workbooks_total"), bool)
        or summary["workbooks_total"] < 1
        or not isinstance(assessment.get("degraded"), bool)
        or not isinstance(summary.get("degraded"), bool)
        or assessment["degraded"] != summary["degraded"]
        or not isinstance(assessment.get("listing_errors"), list)
        or not isinstance(assessment.get("iam_hard_cases"), list)
        or not isinstance(assessment.get("datasource_hazards"), dict)
    ):
        raise EvidenceError("assessment_unrecognized")
    required_keys = {"luid", "name", "rank", "cumulative_share", "tier", "tier_reason", "complexity", "views_lifetime"}
    if any(not isinstance(row, dict) or not required_keys.issubset(row) for row in workbooks):
        raise EvidenceError("assessment_unrecognized")
    if any(
        not isinstance(row["luid"], str)
        or not isinstance(row["name"], str)
        or not isinstance(row["rank"], int)
        or isinstance(row["rank"], bool)
        or not isinstance(row["cumulative_share"], (int, float))
        or isinstance(row["cumulative_share"], bool)
        or not isinstance(row["tier"], str)
        or not isinstance(row["tier_reason"], str)
        or not isinstance(row["complexity"], (int, float))
        or isinstance(row["complexity"], bool)
        or not isinstance(row["views_lifetime"], int)
        or isinstance(row["views_lifetime"], bool)
        for row in workbooks
    ):
        raise EvidenceError("assessment_unrecognized")
    db = _db_read(root / INPUTS[2])
    degraded = bool(assessment["degraded_primary"])
    run = db["run"]
    assessment_by_luid = {row["luid"]: row for row in workbooks}
    database_by_luid = {row["luid"]: row for row in db["workbooks"]}
    consumed_fields = ("rank", "cumulative_share", "tier", "tier_reason", "complexity", "views_lifetime")
    if run.get("degraded_primary") not in (0, 1) or run.get("degraded") not in (0, 1):
        raise EvidenceError("assessment_unrecognized")
    if (
        summary["workbooks_total"] != len(workbooks)
        or summary["workbooks_total"] != len(db["workbooks"])
        or len(assessment_by_luid) != len(workbooks)
        or set(assessment_by_luid) != set(database_by_luid)
        or any(
            assessment_by_luid[luid][field] != database_by_luid[luid].get(field)
            for luid in assessment_by_luid
            for field in consumed_fields
        )
        or bool(run.get("degraded_primary")) != degraded
        or bool(run.get("degraded")) != assessment["degraded"]
        or (not degraded and not set(survey_luids).issubset({row["luid"] for row in db["workbooks"]}))
    ):
        raise EvidenceError("assessment_unrecognized")
    collector = _collector(collector_raw, degraded_primary=degraded)
    digests = []
    for name, raw in zip(INPUTS, (survey_raw, assessment_raw, database_raw, collector_raw), strict=True):
        digests.append(name.encode() + b"\0" + (b"<absent>" if raw is None else raw))
    digest = hashlib.sha256(b"\0".join(digests)).hexdigest()
    return {
        "survey": survey,
        "survey_data": survey_data,
        "survey_luids": survey_luids,
        "assessment": assessment,
        "database": db,
        "collector": collector,
        "degraded_primary": degraded,
        "evidence_sha256": digest,
    }


def evidence_state(directory: Path | str) -> dict[str, Any]:
    """Shared fixed-vocabulary assessment/admin/report state used by front door and status."""
    root = Path(directory)
    try:
        evidence = load_evidence(root)
    except EvidenceError as exc:
        reason = str(exc)
        assessment_state = "missing" if reason == "evidence_missing" else "unrecognized"
        return {
            "assessment": assessment_state,
            "assessment_reason": reason,
            "admin_insights": "not_judged",
            "admin_insights_reason": "not_judged",
            "report": "not_judged",
            "report_reason": "not_judged",
        }
    collector = evidence["collector"]
    report_path = root / "assessment-report.html"
    try:
        report_bytes = report_path.read_bytes()
    except FileNotFoundError:
        report = "missing"
        report_reason = "report_missing"
    except OSError:
        report = "unrecognized"
        report_reason = "report_unrecognized"
    else:
        parser = _DigestParser()
        try:
            parser.feed(report_bytes.decode("utf-8"))
        except (UnicodeError, ValueError):
            parser.digest = None
        report = "current" if parser.digest == evidence["evidence_sha256"] else "stale"
        report_reason = None if report == "current" else "report_stale"
    if collector["outcome"] == "not_collected":
        admin_state = "not_collected"
    else:
        admin_state = collector["outcome"]
    return {
        "assessment": "degraded_primary" if evidence["degraded_primary"] else "established",
        "assessment_reason": "degraded_primary" if evidence["degraded_primary"] else None,
        "admin_insights": admin_state,
        "admin_insights_reason": collector["reason"],
        "report": report,
        "report_reason": report_reason,
        "evidence_sha256": evidence["evidence_sha256"],
    }


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


TOP_N = 20
LABEL_CHARS = 30


def _metric(name: str, value: Any, label: str | None = None) -> str:
    return (
        f'<div class="metric" data-metric="{_esc(name)}"><b>{_esc(value)}</b><span>{_esc(label or name)}</span></div>'
    )


def _percent(share: Any) -> str:
    return "" if share is None else f"{float(share) * 100:.1f}%"


def _label_units(char: str) -> int:
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _truncate(value: Any) -> str:
    """Shorten by display width (wide/fullwidth characters count double); the SVG clip still bounds glyph width."""
    text = "" if value is None else str(value)
    if sum(_label_units(char) for char in text) <= LABEL_CHARS:
        return text
    kept, used = [], 3
    for char in text:
        used += _label_units(char)
        if used > LABEL_CHARS:
            break
        kept.append(char)
    return "".join(kept) + "..."


def _top_note(shown: int, total: int) -> str:
    if shown >= total:
        return ""
    return (
        f'<p class="note">Top {shown} of {total} workbooks by rank; the full list is in section 8, '
        "Workbook inventory.</p>"
    )


def _pareto_svg(rows: list[dict[str, Any]]) -> str:
    """Draw one bar per workbook, sized by lifetime views, inside a viewBox fitted to the bars drawn."""
    row_height, label_width, bar_width = 26, 220, 300
    width, height = label_width + bar_width + 80, 10 + row_height * len(rows)
    peak = max((row.get("views_lifetime") or 0 for row in rows), default=0) or 1
    bars = []
    for index, row in enumerate(rows):
        views = row.get("views_lifetime") or 0
        y = 6 + index * row_height
        size = max(1, int(bar_width * views / peak))
        bars.append(
            f"<g><title>{_esc(row.get('name'))}: {_esc(views)} lifetime views</title>"
            f'<text x="8" y="{y + 15}" clip-path="url(#pareto-label-clip)">{_esc(_truncate(row.get("name")))}</text>'
            f'<rect x="{label_width}" y="{y}" width="{size}" height="20" fill="#4a78a8"></rect>'
            f'<text x="{label_width + size + 6}" y="{y + 15}">{_esc(views)}</text></g>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
        f'aria-label="Lifetime views by workbook"><defs><clipPath id="pareto-label-clip">'
        f'<path d="M0 0H{label_width - 6}V{height}H0Z"></path></clipPath></defs>'
        f"{''.join(bars)}</svg>"
    )


def _permission_metric(db: dict[str, Any]) -> str:
    """Render only an aggregate and fixed completeness state, never grant identities."""
    permission_denied_listings = db.get("permission_denied_listings")
    permission_errors = any(
        row.get("severity") == "secondary"
        and isinstance(row.get("listing"), str)
        and row["listing"].startswith(("project grants:", "workbook grants:"))
        for row in db["listing_errors"]
    )
    if db["run"].get("degraded_primary") or permission_denied_listings is None:
        why = "primary listing degraded" if db["run"].get("degraded_primary") else "collection not recorded"
        value, label, state = "unknown", f"permission entries ({why})", "unknown"
    elif permission_errors or permission_denied_listings:
        value, label, state = f"{db['permission_count']} observed", "permission entries (partial)", "partial"
    else:
        value, label, state = db["permission_count"], "permission entries", "ok"
    return (
        f'<div class="metric" data-metric="permission entries" data-completeness="{state}">'
        f"<b>{_esc(value)}</b><span>{_esc(label)}</span></div>"
    )


def _permission_listing_label(value: Any) -> Any:
    """Keep object names from grant-error listing labels out of the report."""
    if isinstance(value, str) and value.startswith(("project grants:", "workbook grants:")):
        return "permission grants"
    return value


def _section(number: int, title: str, state: str, reason: str | None, source: str, body: str) -> str:  # pylint: disable=too-many-arguments,too-many-positional-arguments
    return (
        f'<section data-section="{number}" data-state="{_esc(state)}" data-reason="{_esc(reason or "")}">'
        f'<h2>{number}. {_esc(title)}</h2><p class="source">Source: {_esc(source)}</p>{body}</section>'
    )


def _table(headers: tuple[str, ...], rows: list[tuple[Any, ...]], *, table_id: str | None = None) -> str:
    if not rows and not table_id:
        return f'<p class="empty">{_esc(headers[0])}: none recorded.</p>'
    ident = f' id="{_esc(table_id)}"' if table_id else ""
    head = "".join(f"<th>{_esc(cell)}</th>" for cell in headers)
    body = "".join("<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table{ident}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _sections(evidence: dict[str, Any]) -> str:  # pylint: disable=too-many-locals,too-many-statements
    assessment = evidence["assessment"]
    db = evidence["database"]
    collector = evidence["collector"]
    degraded = evidence["degraded_primary"]
    status = "partial" if degraded else "ok"
    reason = "degraded_primary" if degraded else None
    survey = evidence["survey"]
    workbooks = sorted(db["workbooks"], key=lambda row: (row["rank"] is None, row["rank"] or 0, row["luid"] or ""))
    views = db["views"]
    sources = db["datasources"]
    projects = db["projects"]
    assessed_at = db["run"].get("assessed_at") or "unknown"

    section1 = (
        '<div class="metrics">'
        + _metric("workbooks", len(workbooks))
        + _metric("published views", len(views))
        + _metric("datasources", len(sources))
        + _metric("projects", len(projects))
        + _permission_metric(db)
        + _metric("assessed_at", assessed_at)
        + "</div>"
    )
    if degraded:
        section1 += "<p>Incomplete primary listing evidence; unknown values are not zero.</p>"
        section1 += _table(
            ("Failed listing", "Severity", "HTTP status"),
            [
                (_permission_listing_label(row.get("listing")), row.get("severity"), row.get("status"))
                for row in db["listing_errors"]
            ],
        )
    elif assessment["degraded"]:
        section1 += "<p>Secondary listing degradation was recorded; primary inventory is established.</p>"
    ranks = [(int(row["rank"]), float(row["cumulative_share"])) for row in workbooks]
    cut80 = next((rank for rank, share in ranks if share >= 0.8), None)
    cut99 = next((rank for rank, share in ranks if share >= 0.99), None)
    tiers: dict[str, int] = {}
    for row in workbooks:
        tiers[str(row.get("tier") or "unknown")] = tiers.get(str(row.get("tier") or "unknown"), 0) + 1
    top = workbooks[:TOP_N]
    pareto_rows = [
        (
            row.get("rank"),
            row.get("name"),
            row.get("views_lifetime"),
            _percent(row.get("cumulative_share")),
            row.get("tier"),
        )
        for row in top
    ]
    section2 = (
        '<div class="metrics">'
        + _metric("cut80", cut80, "workbooks reaching 80% of lifetime views")
        + _metric("cut99", cut99, "workbooks reaching 99% of lifetime views")
        + _metric("pareto-points", len(workbooks), "ranked workbooks")
        + "</div><p>Lifetime views, not a time-windowed measure.</p>"
        + _top_note(len(top), len(workbooks))
        + _pareto_svg(top)
        + _table(("Rank", "Workbook", "Lifetime views", "Cumulative share", "Tier"), pareto_rows)
        + "<ul>"
        + "".join(f'<li data-tier="{_esc(tier)}">{_esc(tier)}: {count}</li>' for tier, count in sorted(tiers.items()))
        + "</ul>"
    )
    admin_state = collector["outcome"]
    admin_reason = collector["reason"]
    event_total = sum(row["events"] for row in collector["rows"])
    section3_state = {
        "available": "ok",
        "unavailable": "unavailable",
        "cannot_establish": "cannot_establish",
        "not_collected": "not_collected",
    }[admin_state]
    available = admin_state == "available"
    section3 = (
        '<div class="metrics">'
        + _metric("event-total", event_total if available else "unknown", "Admin Insights event records")
        + _metric(
            "unmapped-events",
            collector["unmapped_events"] if available else "unknown",
            "events not mapped to a workbook",
        )
        + _metric("first-day", collector["window"]["first_day"] or "unknown", "first event day")
        + _metric("last-day", collector["window"]["last_day"] or "unknown", "last event day")
    )
    if available:
        grouped: dict[str, int] = {}
        for row in collector["rows"]:
            grouped[row["event_type"]] = grouped.get(row["event_type"], 0) + row["events"]
        daily: dict[str, int] = {}
        for row in collector["rows"]:
            daily[row["day"]] = daily.get(row["day"], 0) + row["events"]
        section3 += _metric("daily-days", len(daily), "days with events") + _metric(
            "daily-events-total", sum(daily.values()), "events across those days"
        )
        section3 += (
            "</div><p>Events are event records, not human activity; view/consumption and authoring events are "
            "separate kinds.</p>"
        )
        section3 += _table(("Event type", "Aggregate events"), sorted(grouped.items()))
        section3 += _table(("Day", "Aggregate events"), sorted(daily.items()))
        section3 += _table(
            ("Event", "Item type", "Day", "Events"),
            [(r["event_name"], r["item_type"], r["day"], r["events"]) for r in collector["rows"]],
        )
    else:
        section3 += f"</div><p>Admin Insights {_esc(admin_state)}: {_esc(admin_reason)}.</p>"

    wb_ids = {row["luid"] for row in workbooks}
    ds_ids = {row["luid"] for row in sources}
    edges = {
        (row["workbook_luid"], row["datasource_luid"])
        for row in db["dependencies"]
        if row.get("workbook_luid") and row.get("datasource_luid")
    }
    edges = {(wb, ds) for wb, ds in edges if wb in wb_ids and ds in ds_ids}
    consumers: dict[str, set[str]] = {ds: set() for ds in ds_ids}
    for workbook_luid, datasource_luid in edges:
        consumers[datasource_luid].add(workbook_luid)

    # Admin Insights, Samples and External Assets Default Project are platform content only when
    # they are in the top-level project tree. An ordinary "default" project is not filtered.
    def platform_project(luid: str | None) -> bool:
        seen: set[str] = set()
        while luid and luid not in seen:
            seen.add(luid)
            matches = [p for p in projects if p.get("luid") == luid]
            if not matches:
                return False
            project = matches[0]
            if not project.get("parent_luid") and project.get("name") in {
                "Admin Insights",
                "Samples",
                "External Assets Default Project",
            }:
                return True
            luid = project.get("parent_luid")
        return False

    platform = {row["luid"] for row in sources if platform_project(row.get("project_luid"))}
    lineage_rows = [
        (row.get("name"), "platform" if row["luid"] in platform else len(consumers[row["luid"]]), row["luid"])
        for row in sources
    ]
    lineage_known = not db["run"]["degraded"] and survey.complete
    no_consumers = [row for row in sources if not consumers[row["luid"]] and row["luid"] not in platform]
    lineage_note = (
        f"No-consumer candidates: {', '.join(row['name'] or row['luid'] for row in no_consumers) or 'none'}."
        if lineage_known
        else "No-consumer conclusion unknown because primary assessment or survey completeness is degraded."
    )
    section4 = (
        '<div class="metrics">'
        + _metric("dependency-edges", len(edges), "resolved workbook-datasource edges")
        + _metric("datasource-denominator", len(sources), "published datasources")
        + _metric(
            "unresolved-identities",
            sum(not row.get("workbook_luid") or not row.get("datasource_luid") for row in db["dependencies"]),
            "dependency rows with an unresolved identity",
        )
        + _metric(
            "embedded-dependencies",
            sum(row.get("source") == "embedded" for row in db["dependencies"]),
            "embedded dependency rows",
        )
        + "</div>"
        + _table(("Datasource", "Consumer workbooks / classification", "Datasource LUID"), lineage_rows)
        + f"<p>{_esc(lineage_note)} Empty identity fields are unresolved, never edges. "
        "Source system and connection type were not delivered.</p>"
    )
    scatter = [
        (
            row.get("name"),
            row.get("views_lifetime"),
            "unknown" if degraded and row.get("complexity") == 0 else row.get("complexity"),
            "yes" if row.get("complexity_understated") else "no",
        )
        for row in top
    ]
    plot_rows = [row for row in workbooks if not (degraded and row.get("complexity") == 0)]
    max_views = max((row.get("views_lifetime") or 0 for row in plot_rows), default=1) or 1
    max_complexity = max((float(row.get("complexity") or 0) for row in plot_rows), default=1) or 1
    scatter_svg = (
        '<svg viewBox="0 0 640 280" width="640" height="280" role="img" '
        'aria-label="Lifetime views versus complexity">'
        '<line x1="60" y1="220" x2="600" y2="220" stroke="#4b5b69"></line>'
        '<line x1="60" y1="30" x2="60" y2="220" stroke="#4b5b69"></line>'
        '<text x="60" y="238" text-anchor="middle">0</text>'
        f'<text x="600" y="238" text-anchor="middle">{_esc(max_views)}</text>'
        '<text x="54" y="224" text-anchor="end">0</text>'
        f'<text x="54" y="34" text-anchor="end">{max_complexity:g}</text>'
        '<text x="330" y="266" text-anchor="middle">Lifetime views</text>'
        '<text x="16" y="125" text-anchor="middle" transform="rotate(-90 16 125)">Complexity</text>'
        + "".join(
            f'<circle cx="{60 + int(540 * (row.get("views_lifetime") or 0) / max_views)}" '
            f'cy="{220 - int(190 * float(row.get("complexity") or 0) / max_complexity)}" r="5" fill="#4a78a8">'
            f"<title>{_esc(row.get('name'))}</title></circle>"
            for row in plot_rows
        )
        + "</svg>"
    )
    section5 = (
        '<div class="metrics">'
        + _metric("scatter-points", len(plot_rows), "workbooks plotted")
        + _metric(
            "understated-count",
            sum(bool(r.get("complexity_understated")) for r in workbooks),
            "workbooks with understated complexity",
        )
        + "</div>"
        f"<p>Complexity = sheets×{WEIGHTS['sheets']:g} + dashboards×{WEIGHTS['dashboards']:g} + "
        f"calcs×{WEIGHTS['calcs']:g} + LODs×{WEIGHTS['lods']:g} + "
        f"table calculations×{WEIGHTS['table_calcs']:g}.</p>"
        + scatter_svg
        + _top_note(len(top), len(workbooks))
        + _table(("Workbook", "Lifetime views", "Complexity", "Understated"), scatter)
    )
    section6 = "<p>Proposed migration waves are not delivered. No grouping or wave rules are inferred.</p>"
    locked = [p.get("name") for p in projects if p.get("content_permissions") == "Locked"]
    hard_cases = [
        str(row.get("case") or row.get("kind") or "recorded IAM decision")
        for row in assessment.get("iam_hard_cases", [])
    ]
    section7 = (
        "<p>Observed decision categories only; no permission mapping is provided.</p>"
        + _table(("IAM hard case",), [(value,) for value in sorted(hard_cases)])
        + _table(("Locked project",), [(value,) for value in sorted(locked)])
        + "<p>Naming hazards and grantee/owner identities are not disclosed as access decisions.</p>"
    )
    tiers_seen = sorted({str(row.get("tier") or "") for row in workbooks if row.get("tier")})
    inventory = [
        (
            row.get("name"),
            row.get("project"),
            row.get("views_lifetime"),
            row.get("complexity"),
            row.get("tier"),
            row.get("tier_reason"),
        )
        for row in workbooks
    ]
    section8 = (
        '<label>Tier <select id="tier-filter"><option value="">All</option>'
        + "".join(f'<option value="{_esc(tier)}">{_esc(tier)}</option>' for tier in tiers_seen)
        + '</select></label><button id="sort-views" type="button">Sort by lifetime views</button>'
        + _table(
            ("Workbook", "Project", "Lifetime views", "Complexity", "Tier", "Tier reason"),
            inventory,
            table_id="inventory",
        )
    )
    section9 = (
        "<ul><li>Sources: estate_survey.json, assessment.json, estate.db, admin_insights.json.</li>"
        "<li>Survey lineage is partial evidence; unknown is not no-consumer.</li>"
        "<li>Published views are not sheets; no recorded views does not mean never viewed; missing last "
        "access is unknown.</li>"
        "<li>Tiers are proposals, not automatic retirement advice.</li>"
        "<li>Admin Insights endpoint, payload shape, captions, and status behavior are assumed from a "
        "synthetic mock contract, not live-qualified.</li></ul>"
    )
    return "".join(
        (
            _section(1, "Estate KPIs", status, reason, "assessment.json, estate.db", section1),
            _section(2, "Usage Pareto", status, reason, "assessment.json, estate.db", section2),
            _section(3, "Activity vs consumption", section3_state, admin_reason, "admin_insights.json", section3),
            _section(4, "Partial lineage", "partial", "lineage_is_partial", "estate_survey.json, estate.db", section4),
            _section(5, "Complexity and usage", status, reason, "assessment.json, estate.db", section5),
            _section(6, "Proposed waves", "not_delivered", "wave_analysis_deferred", "not delivered", section6),
            _section(
                7,
                "Security and governance",
                "partial",
                "permission_mapping_not_delivered",
                "assessment.json, estate.db",
                section7,
            ),
            _section(8, "Workbook inventory", status, reason, "assessment.json, estate.db", section8),
            _section(9, "Method and caveats", status, reason, "available evidence and fixed limitations", section9),
        )
    )


def render_html(evidence: dict[str, Any]) -> bytes:
    """Render a stable, self-contained document; source text is escaped and never inserted into script."""
    incomplete = evidence["degraded_primary"]
    banner = (
        '<div class="banner">INCOMPLETE: primary listing evidence is degraded. Unknown values are not zero.</div>'
        if incomplete
        else ""
    )
    script = (
        "document.getElementById('sort-views').addEventListener('click',()=>{"
        "const t=document.querySelector('#inventory tbody');const rows=[...t.rows];"
        "rows.sort((a,b)=>Number(b.cells[2].textContent)-Number(a.cells[2].textContent));"
        "rows.forEach(r=>t.appendChild(r));});"
        "document.getElementById('tier-filter').addEventListener('change',e=>{"
        "for(const r of document.querySelectorAll('#inventory tbody tr'))"
        "r.hidden=!!e.target.value&&r.cells[4].textContent!==e.target.value;});"
    )
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="evidence-sha256" content="{evidence["evidence_sha256"]}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Tableau estate assessment</title><style>"
        "body{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17212b}"
        "h1,h2{color:#17365d}section{border-top:1px solid #ccd4dc;padding:1rem 0}"
        ".banner{background:#fff0c2;padding:1rem;font-weight:bold}.metrics{display:flex;gap:1rem;flex-wrap:wrap}"
        ".metric{padding:.8rem;background:#eef3f8;min-width:8rem}.metric b,.metric span{display:block}"
        ".source{font-size:.9rem;color:#4b5b69}table{border-collapse:collapse;width:100%;margin:1rem 0}"
        "td,th{border:1px solid #ccd4dc;padding:.4rem;text-align:left}th{background:#eef3f8}"
        "svg{display:block;max-width:100%;height:auto;margin:1rem 0}svg text{font-size:12px;fill:#17212b}"
        ".note,.empty{color:#4b5b69;font-style:italic}"
        "</style></head><body><h1>Tableau estate assessment (not migration readiness)</h1>"
        + banner
        + _sections(evidence)
        + f"<script>{script}</script></body></html>"
    )
    return page.encode("utf-8")


def _publish(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".assessment-report-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        sys.stdout.flush()
        sys.stderr.flush()
        for handler in logging.getLogger().handlers:
            handler.flush()
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    """Validate all assessment evidence and atomically publish the fixed report path."""
    parser = argparse.ArgumentParser(description="Render validated offline assessment evidence.")
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.run / "assessment-report.html"
    try:
        evidence = load_evidence(args.run)
        payload = render_html(evidence)
        _publish(output, payload)
    except (EvidenceError, OSError, sqlite3.Error, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
