"""Tests for scripts/check_desktop_orphans.py.

The useful property is not "find any PBIDesktop.exe"; that would punish legitimate parallel work.
These tests pin the narrower promise: only a process this unit recorded as run-owned can fail the
gate, and an explicitly kept process is in-use rather than orphaned.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_desktop_orphans as gate  # noqa: E402  # pylint: disable=wrong-import-position


def _process(pid: int, creation_time: str = "20260822120000.000000-300") -> dict[str, str | int]:
    return {"pid": pid, "creation_time": creation_time, "command_line": f"PBIDesktop.exe pid={pid}"}


def _write_event(unit: Path, action: str, pid: int, creation_time: str = "20260822120000.000000-300") -> None:
    payload = {
        "ts": "2026-08-22T12:00:00+00:00",
        "action": action,
        "pid": pid,
        "creation_time": creation_time,
        "command_line": f"PBIDesktop.exe pid={pid}",
        "pbip": str(unit / "Probe.pbip"),
    }
    with (unit / gate.AUDIT).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def test_no_audit_is_clean_without_censusing_desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A unit that never opened Desktop must not fail because someone else has Desktop open."""

    def explode() -> list[dict[str, str | int]]:
        raise AssertionError("process census should not run with no tracked instances")

    monkeypatch.setattr(gate, "desktop_processes", explode)

    result = gate.audit_target(tmp_path)

    assert result["status"] == gate.STATUS_OK
    assert result["tracked"] == 0


def test_untracked_live_desktop_is_legitimate_parallel_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate ignores colleagues' or sibling agents' Desktop processes."""
    monkeypatch.setattr(gate, "desktop_processes", lambda: [_process(111)])

    result = gate.audit_target(tmp_path)

    assert result["status"] == gate.STATUS_OK
    assert result["orphans"] == []


def test_run_owned_open_process_is_an_orphan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded open with the same live PID/start-time is the enforceable leak."""
    _write_event(tmp_path, "desktop-open", 111)
    monkeypatch.setattr(gate, "desktop_processes", lambda: [_process(111)])

    result = gate.audit_target(tmp_path)

    assert result["status"] == gate.STATUS_ORPHANS
    assert result["orphans"] == [
        {
            "pid": 111,
            "creation_time": "20260822120000.000000-300",
            "pbip": str(tmp_path / "Probe.pbip"),
            "last_action": "desktop-open",
            "command_line": "PBIDesktop.exe pid=111",
        }
    ]


def test_explicitly_kept_process_is_in_use_not_orphaned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`probe_live_source.py --keep` is a deliberate handoff, not a leak."""
    _write_event(tmp_path, "desktop-open", 111)
    _write_event(tmp_path, "desktop-kept", 111)
    monkeypatch.setattr(gate, "desktop_processes", lambda: [_process(111)])

    result = gate.audit_target(tmp_path)

    assert result["status"] == gate.STATUS_OK
    assert result["orphans"] == []


def test_closed_but_still_live_process_is_still_an_orphan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A close log means it should be gone; if it is still alive, the cleanup failed."""
    _write_event(tmp_path, "desktop-open", 111)
    _write_event(tmp_path, "desktop-closed", 111)
    monkeypatch.setattr(gate, "desktop_processes", lambda: [_process(111)])

    result = gate.audit_target(tmp_path)

    assert result["status"] == gate.STATUS_ORPHANS
    assert result["orphans"][0]["last_action"] == "desktop-closed"


def test_reused_pid_with_different_start_time_is_not_our_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PID reuse must not make this unit accuse a later, unrelated Desktop process."""
    _write_event(tmp_path, "desktop-open", 111, "20260822120000.000000-300")
    monkeypatch.setattr(gate, "desktop_processes", lambda: [_process(111, "20260822130000.000000-300")])

    result = gate.audit_target(tmp_path)

    assert result["status"] == gate.STATUS_OK
    assert result["orphans"] == []


def test_cli_orphan_exit_is_not_just_any_command_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation guard: the expected finding has status ORPHANS and exit 1, not arbitrary nonzero."""
    _write_event(tmp_path, "desktop-open", 111)
    monkeypatch.setattr(gate, "desktop_processes", lambda: [_process(111)])

    output = tmp_path / "desktop.json"
    code = gate.main([str(tmp_path), "--json", str(output), "--quiet"])
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert code == gate.EXIT_ORPHANS
    assert payload["status"] == gate.STATUS_ORPHANS
    assert payload["orphans"][0]["pid"] == 111


def test_cli_usage_error_is_distinct_from_orphan_finding(tmp_path: Path) -> None:
    """A broken invocation must not be counted as a caught orphan mutation."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_desktop_orphans.py"), str(tmp_path / "missing")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 64
    assert "not a directory" in result.stderr
    assert "DESKTOP ORPHANS" not in result.stdout
