"""Tests for audit-backed credential-gate shielding in the hook."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "credential_gate.py"
HOOK = REPO / "scripts" / "hooks" / "credential_gate.py"
MARKER = ".credential-gate-BLOCKED.json"
OVERRIDE = ".credential-gate-AUTHORIZED"


def run_gate(*args: str) -> subprocess.CompletedProcess:
    """Run the gate CLI in a subprocess."""
    return subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True, check=False)


def run_hook(payload: dict) -> dict:
    """Run the hook with a Copilot-style payload."""
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(proc.stdout or "{}")


def migration_fixture(root: Path) -> Path:
    """Create a minimal migration-shaped directory."""
    migration = root / "mig"
    (migration / "fabric").mkdir(parents=True)
    (migration / "migration-spec.json").write_text("{}", encoding="utf-8")
    return migration


def write_marker(path: Path, sources: list[str]) -> None:
    """Write a BLOCKED marker without going through apply_block's scope guard."""
    path.write_text(json.dumps({"writes_blocked": True, "sources": sources}), encoding="utf-8")


def hook_for_model_write(root: Path, migration: Path) -> dict:
    """Ask the hook whether a model write under migration/fabric is allowed."""
    target = migration / "fabric" / "Model.tmdl"
    return run_hook({"toolName": "create", "toolArgs": json.dumps({"path": str(target)}), "cwd": str(root)})


def test_probe_cleared_directory_shields_against_later_ancestor_marker(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    assert run_gate("block", str(migration), "--sources", "warehouse").returncode == 0
    assert run_gate("clear", str(migration), "--reason", "DATA_OK", "--earned").returncode == 0
    write_marker(tmp_path / MARKER, ["warehouse"])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") != "deny", "earned local clearance must stop an unrelated ancestor block"


def test_forged_bare_override_does_not_shield_from_an_ancestor_marker(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    (migration / OVERRIDE).write_text("forged by agent", encoding="utf-8")
    write_marker(tmp_path / MARKER, ["warehouse"])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "a bare file with no audit backing must not become a shield"


def test_manual_clear_does_not_shield_from_an_ancestor_marker(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    assert run_gate("block", str(migration), "--sources", "warehouse").returncode == 0
    assert run_gate("clear", str(migration), "--reason", "manual teardown").returncode == 0
    write_marker(tmp_path / MARKER, ["warehouse"])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "manual-clear earns no exemption from ancestor blocks"


def test_probe_clear_for_one_source_does_not_cover_a_later_two_source_block(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    assert run_gate("block", str(migration), "--sources", "warehouse_a").returncode == 0
    assert run_gate("clear", str(migration), "--reason", "DATA_OK", "--earned").returncode == 0
    write_marker(tmp_path / MARKER, ["warehouse_a", "warehouse_b"])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "a new live source must still be blocked"


def test_probe_clear_before_later_rearm_does_not_count_as_current_shield(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    assert run_gate("block", str(migration), "--sources", "warehouse_a").returncode == 0
    assert run_gate("clear", str(migration), "--reason", "DATA_OK", "--earned").returncode == 0
    assert run_gate("block", str(migration), "--sources", "warehouse_a", "warehouse_b").returncode == 0
    assert run_gate("clear", str(migration), "--reason", "teardown after rearm").returncode == 0
    write_marker(tmp_path / MARKER, ["warehouse_a", "warehouse_b"])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "a later block must invalidate earlier probe evidence"
