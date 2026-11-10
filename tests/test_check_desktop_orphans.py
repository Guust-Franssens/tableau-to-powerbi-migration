"""check_desktop_orphans: only a run-owned, still-live Desktop process can fail the gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_desktop_orphans as gate  # noqa: E402  # pylint: disable=wrong-import-position

T0 = "20260822120000.000000-300"
T1 = "20260822130000.000000-300"


def _process(pid: int, creation_time: str = T0) -> dict[str, str | int]:
    return {"pid": pid, "creation_time": creation_time, "command_line": f"PBIDesktop.exe pid={pid}"}


def _write_event(unit: Path, action: str, pid: int, creation_time: str = T0) -> None:
    payload = {"ts": "2026-08-22T12:00:00+00:00", "action": action, **_process(pid, creation_time)}
    payload["pbip"] = str(unit / "Probe.pbip")
    with (unit / gate.AUDIT).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def test_no_audit_is_clean_without_censusing_desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A unit that never opened Desktop must not even look at someone else's Desktop."""

    def explode() -> list[dict[str, str | int]]:
        raise AssertionError("process census should not run with no tracked instances")

    monkeypatch.setattr(gate, "desktop_processes", explode)
    result = gate.audit_target(tmp_path)
    assert result["status"] == gate.STATUS_OK and result["tracked"] == 0


@pytest.mark.parametrize(
    ("events", "live", "status", "last_action"),
    [
        pytest.param([], [_process(111)], gate.STATUS_OK, None, id="untracked-parallel-work"),
        pytest.param([("desktop-open", T0)], [_process(111)], gate.STATUS_ORPHANS, "desktop-open", id="open"),
        pytest.param([("desktop-open", T0), ("desktop-kept", T0)], [_process(111)], gate.STATUS_OK, None, id="kept"),
        pytest.param(
            [("desktop-open", T0), ("desktop-closed", T0)],
            [_process(111)],
            gate.STATUS_ORPHANS,
            "desktop-closed",
            id="closed-but-live",
        ),
        pytest.param([("desktop-open", T0)], [_process(111, T1)], gate.STATUS_OK, None, id="reused-pid"),
    ],
)
def test_orphan_classification(tmp_path, monkeypatch, events, live, status, last_action) -> None:
    """PID plus start time identify our process; `--keep` is a handoff, a failed close is a leak."""
    for action, created in events:
        _write_event(tmp_path, action, 111, created)
    monkeypatch.setattr(gate, "desktop_processes", lambda: live)
    result = gate.audit_target(tmp_path)
    assert result["status"] == status
    assert [o["last_action"] for o in result["orphans"]] == ([last_action] if last_action else [])
    if last_action == "desktop-open":
        assert result["orphans"] == [
            {**_process(111), "pbip": str(tmp_path / "Probe.pbip"), "last_action": last_action}
        ]


def test_cli_orphan_exit_and_usage_error_are_distinct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ORPHANS exits 1 with a JSON payload; a broken invocation exits 64 with no verdict."""
    _write_event(tmp_path, "desktop-open", 111)
    monkeypatch.setattr(gate, "desktop_processes", lambda: [_process(111)])
    output = tmp_path / "desktop.json"
    assert gate.main([str(tmp_path), "--json", str(output), "--quiet"]) == gate.EXIT_ORPHANS
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == gate.STATUS_ORPHANS and payload["orphans"][0]["pid"] == 111
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_desktop_orphans.py"), str(tmp_path / "missing")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64 and "not a directory" in result.stderr
    assert "DESKTOP ORPHANS" not in result.stdout
