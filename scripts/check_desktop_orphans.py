"""
purpose: fail when a migration unit still owns a Power BI Desktop process it should have closed.
usage:   python scripts/check_desktop_orphans.py <unit-or-fabric> [--json <path>] [--quiet]

The gate is deliberately evidence-scoped. It does not census every PBIDesktop.exe on the machine,
because concurrent Desktop instances are legitimate and may belong to sibling agents or humans. It
only checks PIDs this repo recorded in the unit's `.desktop-instance-audit.log`; an unrelated process
is out of scope rather than an orphan.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUDIT = ".desktop-instance-audit.log"
STATUS_OK = "OK"
STATUS_ORPHANS = "ORPHANS"
STATUS_ERROR = "ERROR"
EXIT_OK = 0
EXIT_ORPHANS = 1
EXIT_ERROR = 2

RUN_OWNED_ACTIONS = frozenset({"desktop-open", "desktop-closed"})
KEEP_ACTION = "desktop-kept"


def _unit_dir(target: Path) -> Path:
    """Return the migration unit directory when ``target`` is its fabric folder or bundle."""
    target = target.resolve()
    return target.parent if target.name == "fabric" else target


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_process(raw: dict[str, Any]) -> dict[str, str | int]:
    pid = raw.get("ProcessId") or raw.get("processId") or raw.get("pid")
    return {
        "pid": int(pid),
        "creation_time": str(raw.get("CreationDate") or raw.get("creation_time") or ""),
        "command_line": str(raw.get("CommandLine") or raw.get("command_line") or ""),
    }


def desktop_processes() -> list[dict[str, str | int]]:
    """Return live PBIDesktop.exe processes via the Windows process table."""
    if os.name != "nt":
        raise RuntimeError("PBIDesktop.exe process inspection is only available on Windows")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$p = Get-CimInstance Win32_Process -Filter \"Name='PBIDesktop.exe'\" | "
        "Select-Object ProcessId,CreationDate,CommandLine; "
        "if ($null -eq $p) { '[]' } else { $p | ConvertTo-Json -Compress }"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "Get-CimInstance failed")
    text = proc.stdout.strip() or "[]"
    payload = json.loads(text)
    rows = payload if isinstance(payload, list) else [payload]
    return [_normalize_process(row) for row in rows if isinstance(row, dict) and row.get("ProcessId")]


def _process_by_pid(pid: int) -> dict[str, str | int] | None:
    """Return the live Desktop process for ``pid`` when it still exists."""
    return next((process for process in desktop_processes() if process["pid"] == pid), None)


def record_desktop_event(
    unit: Path,
    action: str,
    pid: int,
    pbip: Path,
    *,
    process: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a run-owned Desktop lifecycle event and return the exact event written."""
    if action not in RUN_OWNED_ACTIONS | {KEEP_ACTION}:
        raise ValueError(f"unknown desktop audit action: {action}")
    if process is None:
        process = _process_by_pid(pid)
    event = {
        "ts": _now(),
        "action": action,
        "pid": int(pid),
        "creation_time": str(process.get("creation_time") or "") if process else "",
        "command_line": str(process.get("command_line") or "") if process else "",
        "pbip": str(pbip.resolve()),
    }
    audit = _unit_dir(unit) / AUDIT
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event


def _read_events(audit: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not audit.is_file():
        return [], []
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_number, line in enumerate(audit.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: invalid JSON: {exc}")
            continue
        if not isinstance(event, dict):
            errors.append(f"line {line_number}: expected object")
            continue
        action = str(event.get("action") or "")
        pid = event.get("pid")
        if action not in RUN_OWNED_ACTIONS | {KEEP_ACTION} or not isinstance(pid, int):
            continue
        events.append(event)
    return events, errors


def _event_key(event: dict[str, Any]) -> tuple[int, str]:
    return int(event["pid"]), str(event.get("creation_time") or "")


def _last_events(events: list[dict[str, Any]]) -> dict[tuple[int, str], dict[str, Any]]:
    latest: dict[tuple[int, str], dict[str, Any]] = {}
    for event in events:
        latest[_event_key(event)] = event
    return latest


def _matching_live_process(event: dict[str, Any], live: dict[int, dict[str, str | int]]) -> dict[str, str | int] | None:
    process = live.get(int(event["pid"]))
    if process is None:
        return None
    recorded_creation = str(event.get("creation_time") or "")
    if recorded_creation and recorded_creation != str(process.get("creation_time") or ""):
        return None
    return process


def audit_target(target: Path) -> dict[str, Any]:
    """Return OK/ORPHANS/ERROR for run-owned Desktop instances associated with ``target``."""
    unit = _unit_dir(target)
    audit = unit / AUDIT
    events, errors = _read_events(audit)
    latest = _last_events(events)
    tracked = [event for event in latest.values() if event.get("action") in RUN_OWNED_ACTIONS]
    if errors:
        return {
            "status": STATUS_ERROR,
            "audit": str(audit),
            "errors": errors,
            "detail": "desktop audit is unreadable; refusing to guess clean",
        }
    if not tracked:
        return {
            "status": STATUS_OK,
            "audit": str(audit),
            "tracked": 0,
            "orphans": [],
            "detail": "no run-owned Desktop instances recorded",
        }
    try:
        live_rows = desktop_processes()
    except (RuntimeError, json.JSONDecodeError) as exc:
        return {
            "status": STATUS_ERROR,
            "audit": str(audit),
            "tracked": len(tracked),
            "errors": [str(exc)],
            "detail": "could not inspect live PBIDesktop.exe processes",
        }
    live = {int(process["pid"]): process for process in live_rows}
    orphans = []
    for event in tracked:
        process = _matching_live_process(event, live)
        if process is None:
            continue
        orphans.append(
            {
                "pid": event["pid"],
                "creation_time": event.get("creation_time") or process.get("creation_time"),
                "pbip": event.get("pbip"),
                "last_action": event.get("action"),
                "command_line": process.get("command_line") or event.get("command_line") or "",
            }
        )
    return {
        "status": STATUS_ORPHANS if orphans else STATUS_OK,
        "audit": str(audit),
        "tracked": len(tracked),
        "live_pbidesktop": len(live_rows),
        "orphans": orphans,
        "detail": (
            "run-owned PBIDesktop.exe process(es) still live after completion"
            if orphans
            else "no run-owned Desktop process remains live"
        ),
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path, help="migration unit, fabric folder, or engine bundle")
    parser.add_argument("--json", type=Path, help="write machine-readable result")
    parser.add_argument("--quiet", action="store_true", help="suppress human summary")
    args = parser.parse_args(argv)
    if not args.target.is_dir():
        print(f"ERROR: not a directory: {args.target}", file=sys.stderr)
        return 64
    result = audit_target(args.target)
    if args.json:
        _write_json(args.json, result)
    if not args.quiet:
        print(f"DESKTOP ORPHANS: {result['status']} - {result.get('detail', '')}")
        for orphan in result.get("orphans", []):
            print(f"  pid={orphan['pid']} pbip={orphan.get('pbip')} last_action={orphan.get('last_action')}")
    return {STATUS_OK: EXIT_OK, STATUS_ORPHANS: EXIT_ORPHANS}.get(str(result.get("status")), EXIT_ERROR)


if __name__ == "__main__":
    sys.exit(main())
