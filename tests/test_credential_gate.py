"""Tests for the live-source credential gate.

These lock in behaviour that was established empirically against real agents, so a future
refactor cannot quietly undo it. The important cases are the adversarial ones: a bare override
file must authorize NOTHING, because agents demonstrably create it themselves.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "credential_gate.py"
HOOK = REPO / "scripts" / "hooks" / "credential_gate.py"


def run_gate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True, check=False)


def run_hook(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(proc.stdout or "{}")


@pytest.fixture
def migration(tmp_path: Path) -> Path:
    (tmp_path / "fabric").mkdir()
    yield tmp_path
    run_gate("clear", str(tmp_path), "--reason", "test-teardown")


def test_block_then_clear_round_trips(migration: Path) -> None:
    assert run_gate("block", str(migration), "--sources", "x").returncode == 0
    assert (migration / ".credential-gate-BLOCKED.json").is_file()
    assert run_gate("clear", str(migration), "--reason", "test").returncode == 0
    assert not (migration / ".credential-gate-BLOCKED.json").exists()


def test_a_bare_override_file_authorizes_nothing(migration: Path) -> None:
    """The failure that made this design necessary.

    Measured: agents created `.credential-gate-AUTHORIZED` themselves - directly, and then by
    assembling the name from string fragments once a matcher looked for the literal. So the file
    must carry no authority on its own; only an audit-backed `authorize` counts.
    """
    (migration / ".credential-gate-AUTHORIZED").write_text("authorized by tool fallback")
    run_gate("block", str(migration), "--sources", "x")
    assert (migration / ".credential-gate-BLOCKED.json").is_file(), "forged override must not prevent the block"
    assert run_gate("verify", str(migration)).returncode == 1, "forged override must be reported"


def test_authorize_is_audit_backed_and_lifts_the_gate(migration: Path, monkeypatch) -> None:
    """The human path: an audit-backed authorize is what makes an override authentic.

    Runs the mechanics in-process with the lineage guard stubbed to "human", because the suite itself
    executes inside an agent session and would otherwise (correctly) be refused. The refusal path has
    its own test.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import credential_gate as gate  # noqa: PLC0415

    run_gate("block", str(migration), "--sources", "x")
    monkeypatch.setattr(gate, "_has_copilot_ancestor", lambda _chain: False)
    assert gate.authorize(migration, "tester") == 0
    assert gate.verify(migration) == 0
    audit = (migration / ".credential-gate-audit.log").read_text(encoding="utf-8")
    assert '"action": "authorize"' in audit


def test_verify_flags_artifacts_built_while_blocked(migration: Path) -> None:
    run_gate("block", str(migration), "--sources", "x")
    # Simulate a successful evasion (an agent that stripped the ACE and wrote anyway).
    run_gate("clear", str(migration), "--reason", "simulate-evasion")
    (migration / ".credential-gate-BLOCKED.json").write_text('{"blocked": true, "sources": []}')
    (migration / "fabric" / "Model.tmdl").write_text("table X")
    assert run_gate("verify", str(migration)).returncode == 1


def test_hook_denies_a_guarded_write_under_a_blocked_migration(migration: Path) -> None:
    run_gate("block", str(migration), "--sources", "shipment")
    target = migration / "fabric" / "Shipment.tmdl"
    out = run_hook({"toolName": "create", "toolArgs": json.dumps({"path": str(target)}), "cwd": str(migration)})
    assert out.get("permissionDecision") == "deny"
    assert out.get("interrupt") is True, "must interrupt, not merely deny - a pause gets re-prompted"


def test_hook_allows_reads_of_guarded_files(migration: Path) -> None:
    """A gate that blinds the agent produces worse reports, not safer ones."""
    run_gate("block", str(migration), "--sources", "shipment")
    target = migration / "fabric" / "Shipment.tmdl"
    out = run_hook({"toolName": "view", "toolArgs": json.dumps({"path": str(target)}), "cwd": str(migration)})
    assert out.get("permissionDecision") != "deny"


def test_hook_handles_both_measured_payload_shapes(migration: Path) -> None:
    """preToolUse sends `toolArgs` (string); permissionRequest sends `toolInput` (object).

    An earlier version read only `tool_input` (snake_case) and matched nothing, so it allowed every
    write while appearing to work. That is worse than no guardrail, hence this test.
    """
    run_gate("block", str(migration), "--sources", "shipment")
    target = migration / "fabric" / "Shipment.tmdl"

    pre = run_hook(
        {"toolName": "apply_patch", "toolArgs": f"*** Begin Patch\n*** Add File: {target}\n+x\n", "cwd": str(migration)}
    )
    assert pre.get("permissionDecision") == "deny", "preToolUse/apply_patch shape must be caught"

    perm = run_hook({"toolName": "edit", "toolInput": {"file_path": str(target)}, "cwd": str(migration)})
    assert perm.get("permissionDecision") == "deny", "permissionRequest/toolInput shape must be caught"


def test_hook_is_fast_enough_that_it_cannot_time_out(migration: Path) -> None:
    """preToolUse command hooks fail OPEN on timeout, so a slow hook is a disabled hook."""
    import time

    start = time.monotonic()
    run_hook({"toolName": "view", "toolArgs": "{}", "cwd": str(migration)})
    assert time.monotonic() - start < 5.0


def test_authorize_is_refused_from_inside_an_agent_session(migration: Path) -> None:
    """Agents ran `authorize --who <user>` themselves, forging the human decision.

    The tests run under pytest, which is itself launched from an agent session here, so this asserts
    the refusal path. It is the behaviour that matters: an agent must not be able to certify its own
    unvalidated build through the sanctioned command.
    """
    import platform

    if platform.system() != "Windows":
        pytest.skip("lineage check is Windows-only")
    run_gate("block", str(migration), "--sources", "x")
    proc = run_gate("authorize", str(migration), "--who", "someone")
    combined = proc.stdout + proc.stderr
    assert "REFUSED" in combined or proc.returncode == 2
    assert not (migration / ".credential-gate-AUTHORIZED").exists()


def test_lineage_check_fails_closed_on_an_unknown_chain() -> None:
    """The bug that let a forged authorization through, locked down.

    The first version returned False for an empty chain, so a lineage query that failed - which
    happened for real under four concurrent agents - silently AUTHORIZED. "I could not tell" must
    never be read as permission.
    """
    import platform

    if platform.system() != "Windows":
        pytest.skip("lineage check is Windows-only")
    sys.path.insert(0, str(REPO / "scripts"))
    from credential_gate import _has_copilot_ancestor  # noqa: PLC0415

    assert _has_copilot_ancestor([]) is True
    assert _has_copilot_ancestor(["<lineage-unavailable>"]) is True
    assert _has_copilot_ancestor(["python.exe", "pwsh.exe", "copilot.exe"]) is True
    assert _has_copilot_ancestor(["python.exe", "pwsh.exe", "explorer.exe"]) is False
