"""
purpose: collect an allowlisted Admin Insights event aggregate for assessment-only runs.
usage:   python -B scripts/admin_insights_collector.py --env <env> --db <estate.db> --out <json>
internal: true
internal-reason: invoked only as the third child of the assessment-only front door.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_luid_census import _session  # noqa: E402  # pylint: disable=wrong-import-position,protected-access

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
SCHEMA = "admin-insights-aggregate/1"
# ASSUMED mock contract only: no live Tableau/Admin Insights endpoint or payload qualification is claimed.
METADATA_PATH = "/vizql-data-service/metadata"
QUERY_PATH = "/vizql-data-service/query"
# ASSUMED mock contract only; request fields, grouping and aggregate names require a separate live trial.
METADATA_BODY = {"fields": ["item_luid", "event_type", "event_name", "item_type", "day"]}
QUERY_BODY = {
    "fields": ["item_luid", "event_type", "event_name", "item_type", "day"],
    "group_by": ["item_luid", "event_type", "event_name", "item_type", "day"],
    "aggregates": [{"field": "event", "function": "COUNT"}],
}
REQUIRED_FIELDS = {"item_luid", "event_type", "event_name", "item_type", "day"}
_STATUS = {
    "datasource_not_found": ("unavailable", 0),
    "permission_denied": ("unavailable", 0),
    "vds_unavailable": ("unavailable", 0),
    "required_fields_missing": ("unavailable", 0),
    "ambiguous_datasource": ("cannot_establish", 3),
    "transport_failure": ("cannot_establish", 3),
    "malformed_response": ("cannot_establish", 3),
    "sign_in_failed": ("cannot_establish", 3),
}


def _identity(database: Path) -> tuple[str | None, str | None]:
    """Resolve exactly one top-level Admin Insights/TS Events pair from fixed DB fields."""
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = list(
            connection.execute(
                "SELECT d.luid FROM datasource AS d "
                "JOIN project AS p ON p.luid=d.project_luid "
                "WHERE d.name=? AND p.name=? AND (p.parent_luid IS NULL OR p.parent_luid='')",
                ("TS Events", "Admin Insights"),
            )
        )
    finally:
        connection.close()
    if not rows:
        return "datasource_not_found", None
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        return "ambiguous_datasource", None
    return None, rows[0][0]


def _payload(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("malformed_response") from None
    if not isinstance(value, dict):
        raise ValueError("malformed_response")
    return value


def _metadata_fields(response: dict[str, Any]) -> set[str]:
    fields = response.get("fields")
    if not isinstance(fields, list):
        raise ValueError("malformed_response")
    found = set()
    for field in fields:
        if isinstance(field, str):
            found.add(field)
        elif isinstance(field, dict) and isinstance(field.get("name"), str):
            found.add(field["name"])
        else:
            raise ValueError("malformed_response")
    return found


def _query_rows(response: dict[str, Any], view_to_workbook: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    records = response.get("rows")
    if not isinstance(records, list):
        raise ValueError("malformed_response")
    rows = []
    unmapped = 0
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("malformed_response")
        item_luid = record.get("item_luid")
        event_type = record.get("event_type")
        event_name = record.get("event_name")
        item_type = record.get("item_type")
        day = record.get("day")
        count = record.get("events")
        if (
            not all(isinstance(value, str) for value in (item_luid, event_type, event_name, item_type, day))
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            raise ValueError("malformed_response")
        workbook_luid = view_to_workbook.get(item_luid)
        if workbook_luid is None:
            unmapped += count
        rows.append(
            {
                "event_type": event_type,
                "event_name": event_name,
                "item_type": item_type,
                "item_luid": item_luid,
                "workbook_luid": workbook_luid,
                "day": day,
                "events": count,
            }
        )
    return (
        sorted(
            rows,
            key=lambda row: (
                row["day"],
                row["event_type"],
                row["item_luid"],
                row["event_name"],
                row["item_type"],
                row["workbook_luid"] or "",
                row["events"],
            ),
        ),
        unmapped,
    )


def _request_once(session: oracle.TableauSession, path: str, body: dict[str, Any]) -> tuple[int, bytes]:
    status, payload, _headers = session._request(  # pylint: disable=protected-access
        "POST", path, body=body, api="v1"
    )
    if session.reflected_credential(payload):
        raise ValueError("malformed_response")
    return status, payload


def _collect(env_path: Path, database: Path) -> tuple[dict[str, Any], int]:  # pylint: disable=too-many-locals,too-many-return-statements
    outcome: dict[str, Any] = {
        "schema": SCHEMA,
        "outcome": "cannot_establish",
        "reason": "transport_failure",
        "datasource_luid": None,
        "window": {"first_day": None, "last_day": None},
        "rows": [],
        "unmapped_events": 0,
    }
    session = None
    try:
        reason, datasource_luid = _identity(database)
        if reason:
            outcome["outcome"], code = _STATUS[reason]
            outcome["reason"] = reason
            return outcome, code
        session = _session(env_path)
        try:
            session.sign_in()
        except Exception:  # noqa: BLE001 - response text can contain credential material. # pylint: disable=broad-exception-caught
            outcome["reason"] = "sign_in_failed"
            return outcome, 3
        outcome["datasource_luid"] = datasource_luid
        try:
            status, raw = _request_once(session, METADATA_PATH, {**METADATA_BODY, "datasource_luid": datasource_luid})
            if status != 200:
                reason = (
                    "permission_denied"
                    if status == 403
                    else "vds_unavailable"
                    if status == 404
                    else "transport_failure"
                )
                outcome["outcome"], code = _STATUS[reason]
                outcome["reason"] = reason
                return outcome, code
            metadata = _payload(raw)
            fields = _metadata_fields(metadata)
            if not REQUIRED_FIELDS.issubset(fields):
                outcome["outcome"], code = _STATUS["required_fields_missing"]
                outcome["reason"] = "required_fields_missing"
                return outcome, code
            status, raw = _request_once(session, QUERY_PATH, {**QUERY_BODY, "datasource_luid": datasource_luid})
            if status != 200:
                reason = (
                    "permission_denied"
                    if status == 403
                    else "vds_unavailable"
                    if status == 404
                    else "transport_failure"
                )
                outcome["outcome"], code = _STATUS[reason]
                outcome["reason"] = reason
                return outcome, code
            result = _payload(raw)
            connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
            try:
                view_to_workbook = dict(
                    connection.execute(
                        "SELECT luid, workbook_luid FROM view WHERE luid IS NOT NULL AND workbook_luid IS NOT NULL"
                    )
                )
            finally:
                connection.close()
            rows, unmapped = _query_rows(result, view_to_workbook)
            days = [row["day"] for row in rows]
            outcome.update(
                {
                    "outcome": "available",
                    "reason": None,
                    "window": {"first_day": min(days) if days else None, "last_day": max(days) if days else None},
                    "rows": rows,
                    "unmapped_events": unmapped,
                }
            )
            return outcome, 0
        except (ValueError, sqlite3.Error):
            outcome["reason"] = "malformed_response"
            return outcome, 3
        except Exception:  # noqa: BLE001 - transport exception details are never persisted. # pylint: disable=broad-exception-caught
            outcome["reason"] = "transport_failure"
            return outcome, 3
    except (OSError, sqlite3.Error, ValueError):
        outcome["reason"] = "transport_failure"
        return outcome, 3
    finally:
        if session is not None and session.token:
            session.sign_out()


def _publish(path: Path, value: dict[str, Any], code: int) -> int:
    """Flush a closed, allowlisted artifact before atomic publication; return immediately afterward."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=".admin-insights-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
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
    return code


def main(argv: list[str] | None = None) -> int:
    """Collect one metadata/query pair and publish only aggregate allowlisted fields."""
    parser = argparse.ArgumentParser(description="Collect an Admin Insights aggregate for assessment.")
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    value, code = _collect(args.env, args.db)
    return _publish(args.out, value, code)


if __name__ == "__main__":
    raise SystemExit(main())
