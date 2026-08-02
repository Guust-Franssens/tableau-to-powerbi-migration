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


def test_the_probe_can_build_while_the_deliverable_stays_blocked(migration: Path) -> None:
    """The regression that got v1 reverted off master: the gate deadlocked the SUCCESS path.

    The way an agent EARNS the right to build is the one-row reachability probe - but the probe is
    itself a PBIP, and v1 denied all of `fabric/`, so the probe was blocked by the gate it exists to
    satisfy. Every live-source migration dead-ended at "a human must authorize an unvalidated build",
    working credentials or not. Only the negative case had been tested, where "nothing was built" is
    the pass condition, so a gate that blocked everything passed perfectly.
    """
    run_gate("block", str(migration), "--sources", "shipment")

    # SIBLING of fabric/, not a child - that placement is the fix. A sandbox inside the denied tree
    # inherits the deny, which is what caused the deadlock in the first place.
    probe = migration / "_probe" / "Probe.SemanticModel" / "definition" / "tables"
    probe.mkdir(parents=True, exist_ok=True)
    (probe / "shipment.tmdl").write_text("table shipment", encoding="utf-8")
    assert (probe / "shipment.tmdl").exists(), "the probe must be able to build, or the gate deadlocks"

    with pytest.raises(PermissionError):
        (migration / "fabric" / "Deliverable.tmdl").write_text("table x", encoding="utf-8")


def test_clearing_after_a_successful_probe_lets_the_build_proceed(migration: Path) -> None:
    """The other half of the positive path: DATA_OK -> clear -> build."""
    run_gate("block", str(migration), "--sources", "shipment")
    run_gate("clear", str(migration), "--reason", "probe-data-ok")
    (migration / "fabric" / "Model.tmdl").write_text("table x", encoding="utf-8")
    assert (migration / "fabric" / "Model.tmdl").exists()


def test_the_hook_never_denies_when_its_own_config_is_broken() -> None:
    """A hook that errors takes the whole CLI down, because preToolUse fails CLOSED on error.

    Measured 2026-08-02: deleting the hook script while a session still had the config loaded denied
    EVERY tool call in that session - no shell, no edit, no read - and the only recovery was a
    restart. So the hook must degrade to allow-everything rather than raise, whatever it is handed.
    """
    for payload in ({}, {"toolName": None}, {"toolName": "create", "toolArgs": None}):
        assert run_hook(payload) == {}, f"hook must return {{}} for {payload}, never raise"


def test_the_probe_sandbox_is_a_sibling_of_the_denied_folder(migration: Path) -> None:
    """Placement is the fix, so assert the placement.

    A sandbox INSIDE `fabric/` inherits the deny, which is what deadlocked v1 and then needed a
    grant, a create-before-deny ordering rule, and a heal path - three fragile things measured
    failing. Outside the denied tree, none of them exist. If someone moves it back under `fabric/`,
    this fails before the deadlock can reach a user.
    """
    run_gate("block", str(migration), "--sources", "x")
    assert (migration / "_probe").is_dir(), "the sandbox must be a sibling of fabric/"
    assert not (migration / "fabric" / "_probe").exists(), "the sandbox must NOT be inside the denied tree"

    # And it must be writable with the gate up - the whole point.
    (migration / "_probe" / "canary.txt").write_text("ok", encoding="utf-8")
    assert (migration / "_probe" / "canary.txt").exists()


def test_a_bare_clear_earns_nothing_and_verify_says_so(migration: Path) -> None:
    """The front-door bypass. Measured: `clear --reason "I decided it is fine"` lifted the ACL and
    the build proceeded with no probe run - the whole guarantee gone without touching the ACL.
    """
    run_gate("block", str(migration), "--sources", "x")
    run_gate("clear", str(migration), "--reason", "I decided it is fine")
    (migration / "fabric" / "M.tmdl").write_text("table x", encoding="utf-8")
    assert run_gate("verify", str(migration)).returncode == 1, "an unearned clear must be reported"


def test_an_earned_clear_passes_verify(migration: Path) -> None:
    """Paired control for the test above.

    Without it, `verify` could satisfy that test by always returning 1, and both would look green
    while the gate was useless. Every "must be caught" needs a "must NOT be caught" beside it.
    """
    run_gate("block", str(migration), "--sources", "x")
    run_gate("clear", str(migration), "--reason", "probe-cleared: DATA_OK from t", "--earned")
    (migration / "fabric" / "M.tmdl").write_text("table x", encoding="utf-8")
    assert run_gate("verify", str(migration)).returncode == 0


def test_evidence_predating_the_most_recent_block_does_not_count(migration: Path) -> None:
    """Naive forgery: append a `probe-cleared` line with an old timestamp.

    The audit log is a text file and cannot be made unforgeable at same-user privilege - that is
    documented, not hidden. Ordering at least means stale or backdated evidence earns nothing.
    """
    import json as _json

    run_gate("block", str(migration), "--sources", "x")
    run_gate("clear", str(migration), "--reason", "sneaky")
    audit = migration / ".credential-gate-audit.log"
    forged = _json.dumps({"ts": "2020-01-01T00:00:00+00:00", "action": "probe-cleared", "detail": "forged"})
    audit.write_text(audit.read_text(encoding="utf-8") + forged + "\n", encoding="utf-8")
    (migration / "fabric" / "M.tmdl").write_text("table x", encoding="utf-8")
    assert run_gate("verify", str(migration)).returncode == 1, "backdated evidence must not count"


def test_dns_precheck_separates_a_bad_address_from_a_missing_credential() -> None:
    """`a hang means a sign-in modal` is only true once the host resolves.

    Measured: an unresolvable host loaded into Desktop fine (the M query is not evaluated at load)
    and then hung for the full timeout, landing on NO_CREDENTIAL - a 200s wrong answer. Also
    measured, and the reason the original fixture was invalid: *.azuredatabricks.net wildcards to a
    real Azure IP, so a made-up workspace RESOLVES and genuinely has no credential.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import _host_resolves  # noqa: PLC0415

    assert _host_resolves("no-such-host-98765.invalid") is False
    assert _host_resolves("adb-0000000000000000.00.azuredatabricks.net") is True


def test_failure_classification_distinguishes_the_causes() -> None:
    """Conflating verdicts is the defect class this whole script exists to remove."""
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import _classify_failure  # noqa: PLC0415

    assert _classify_failure("Exception: no catalog found on the instance")[0] == "UNREACHABLE"
    assert _classify_failure("The credential was not provided; please sign in")[0] == "NO_CREDENTIAL"
    assert _classify_failure("something else entirely went wrong")[0] == "UNREACHABLE"


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
