"""Tests for the live-source credential gate.

These lock in behaviour that was established empirically against real agents, so a future
refactor cannot quietly undo it. The important cases are the adversarial ones: a bare override
file must authorize NOTHING, because agents demonstrably create it themselves.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "credential_gate.py"
HOOK = REPO / "scripts" / "hooks" / "credential_gate.py"

sys.path.insert(0, str(REPO / "scripts"))

import credential_gate as cg  # noqa: E402  # pylint: disable=wrong-import-position
import preflight_source_credentials as pf  # noqa: E402  # pylint: disable=wrong-import-position


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_engine_receipt(migration: Path, artifacts: list[Path]) -> None:
    receipt = migration / "engine-output-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "version": 1,
                "created_at": "2026-08-10T00:00:00+00:00",
                "report_sha256": _sha256(migration / "report.json"),
                "input_manifest_sha256": _sha256(migration / "input_manifest.json"),
                "artifacts": [
                    {
                        "path": artifact.relative_to(migration).as_posix(),
                        "size": artifact.stat().st_size,
                        "sha256": _sha256(artifact),
                    }
                    for artifact in artifacts
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _append_audit(migration, "engine-receipt", f"sha256={_sha256(receipt)}")


def _append_audit(migration: Path, action: str, detail: str, sources: list[str] | None = None) -> None:
    audit = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "detail": detail,
        "user": "test",
        # Real `_audit()` always stamps `scope`; issue #354's review tightened `_audit_entries` to
        # drop any entry missing it (a stripped-scope copy is no longer "legacy", it is untrusted).
        # An unscoped synthetic fixture would now be silently dropped and never actually exercised.
        "scope": str(migration.resolve()),
    }
    if sources is not None:
        # `sources` is what makes an entry ATTRIBUTABLE to one live endpoint. Optional here only so
        # the fixtures can build the unkeyed shape deliberately, which is a control, not a default.
        audit["sources"] = list(sources)
    with (migration / ".credential-gate-audit.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(audit) + "\n")


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


# The rights `apply_block` denies (`DENY_RIGHTS = "(OI)(CI)(WD,AD,WA)"`), as icacls renders them
# back: WD = write data / create files, AD = append data / create dirs, WA = write attributes.
DENY_ACE_RIGHTS = frozenset({"WD", "AD", "WA"})


def _icacls_read(target: Path) -> tuple[int, str]:
    """Read-only `icacls <target>` inspection: (exit code, combined output)."""
    proc = subprocess.run(["icacls", str(target)], capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr)


def _current_account_tokens() -> set[str]:
    r"""Every rendering of THIS account icacls might print: bare name, DOMAIN\name, and the SID.

    Bound at run time, never hard-coded. The #543 audit measured a `DOMAIN\user` rendering on one host,
    but a machine-local user, a service account or a differently tokened session each render
    differently, and a literal would be true on exactly one machine. The SID comes from `whoami`
    when it is available and is simply absent when it is not - matching is a membership test over
    whatever renderings we could establish, so a missing SID weakens nothing.
    """
    tokens: set[str] = set()
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    if user:
        tokens.add(user.lower())
        if domain:
            tokens.add(f"{domain}\\{user}".lower())
    try:
        proc = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=False)
    except OSError:  # no `whoami` on this platform - the environment renderings still stand
        return tokens
    if proc.returncode == 0 and proc.stdout.strip():
        for field in proc.stdout.strip().splitlines()[-1].split(","):
            cleaned = field.strip().strip('"').lower()
            if cleaned:
                tokens.add(cleaned)
    return tokens


def _deny_rights_for_current_account(target: Path) -> tuple[int, str, list[frozenset[str]]]:
    """(icacls exit code, its output, the rights of every DENY ACE naming the CURRENT account).

    Parses the ACE rows rather than matching an English success sentence, because icacls is
    localized: a run on a non-English Windows prints different prose and identical ACE syntax.
    """
    code, out = _icacls_read(target)
    tokens = _current_account_tokens()
    rights: list[frozenset[str]] = []
    for line in out.splitlines():
        marker = line.rfind(":(")
        if marker == -1:
            continue
        head = line[:marker].split()
        if not head or head[-1].lower() not in tokens:
            continue
        groups = [group.upper() for group in re.findall(r"\(([^)]*)\)", line[marker + 1 :])]
        if "DENY" not in groups:
            continue
        deny_at = groups.index("DENY")
        granted = groups[deny_at + 1] if deny_at + 1 < len(groups) else ""
        rights.append(frozenset(token.strip() for token in granted.split(",") if token.strip()))
    return code, out, rights


def _assert_gate_fully_cleared(migration: Path) -> None:
    """Asserted teardown: `clear` must exit 0 AND the kernel deny must actually be gone.

    #543's state-2 node called no cleanup at all and leaked a real deny ACE into the temp tree; the
    module `migration` fixture does call `clear`, but discards its exit code, so a failed
    `/remove:d` would leave a denied directory behind and still look like clean teardown. A failure
    here fails/errors the test and carries the icacls output as evidence rather than swallowing it.
    """
    proc = run_gate("clear", str(migration), "--reason", "test-teardown")
    assert proc.returncode == 0, f"teardown `clear` must succeed or the deny ACE leaks:\n{proc.stdout}{proc.stderr}"
    if platform.system() != "Windows":
        return
    fabric = migration / "fabric"
    code, out, rights = _deny_rights_for_current_account(fabric)
    assert code == 0, f"icacls could not inspect {fabric} after clear (residue unverified):\n{out}"
    assert not rights, f"a current-account deny ACE survived `clear` on {fabric}:\n{out}"
    # The syscall is the real proof: owner/token semantics can differ from what icacls prints.
    landed = fabric / "post-clear-teardown-write.tmdl"
    landed.write_text("table PostClear", encoding="utf-8")
    assert landed.is_file(), f"a write must succeed again once the gate is cleared: {landed}"
    landed.unlink()


@pytest.fixture
def migration(tmp_path: Path) -> Path:
    (tmp_path / "fabric").mkdir()
    # A real migration dir always carries its spec, and `apply_block` now REQUIRES a scope marker
    # before it will arm (a marker governs its whole subtree; one written too high blocked ~13
    # unrelated agents in a real incident). Writing it here makes the fixture match reality.
    (tmp_path / "migration-spec.json").write_text("{}", encoding="utf-8")
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
    monkeypatch.setattr(gate, "_ancestry", lambda: ["python.exe", "pwsh.exe"])
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


def test_verify_flags_materialized_source_data(migration: Path) -> None:
    """Extracted customer ROWS are a violation, not just a `.tmdl`.

    `verify` used to scan only `{.tmdl,.pbism,.pbir,.pbip}`. Measured 2026-08-04: a
    deterministic-tier run wrote **two 110 MB CSVs** of source rows next to the model and `verify`
    reported *"OK - gate applied, no model/report artifacts exist"*. A materialized CSV is a
    strictly LARGER harm than a definition file - a `.tmdl` describes a model, a `.csv` IS the
    customer's data on a workstation, extracted from a source whose reachability was never proven.
    """
    run_gate("block", str(migration), "--sources", "warehouse")
    run_gate("clear", str(migration), "--reason", "simulate-evasion")
    (migration / ".credential-gate-BLOCKED.json").write_text('{"blocked": true, "sources": []}')
    data = migration / "data" / "Orders"
    data.mkdir(parents=True)
    (data / "Extract_Extract.csv").write_text("order_id,amount\n1,42\n")

    result = run_gate("verify", str(migration))
    assert result.returncode == 1, "materialized rows must fail verification"
    assert "Extract_Extract.csv" in (result.stdout + result.stderr)


def test_verify_scans_outside_fabric(migration: Path) -> None:
    """A build that lands anywhere in the migration counts, not only under `fabric/`.

    The deterministic tier writes to `pbip/`, `reports/`, `semantic_models/` and `data/`. A
    `fabric/`-only scan reported "no artifacts exist" beside a complete, unvalidated PBIP.
    """
    run_gate("block", str(migration), "--sources", "warehouse")
    run_gate("clear", str(migration), "--reason", "simulate-evasion")
    (migration / ".credential-gate-BLOCKED.json").write_text('{"blocked": true, "sources": []}')
    emitted = migration / "deterministic" / "pbip" / "Wb.SemanticModel" / "definition" / "tables"
    emitted.mkdir(parents=True)
    (emitted / "Orders.tmdl").write_text("table Orders")

    assert run_gate("verify", str(migration)).returncode == 1


def test_verify_allows_provenance_backed_engine_artifacts_that_predate_the_gate(migration: Path) -> None:
    """Engine output exists before the gate can arm; verify must classify, not mislabel it.

    This is the #56 engine-path shape: the deterministic tier has already written `pbip/` and
    `semantic_models/`, then the agent tier arms a gate after reading the handover. The files are
    still unvalidated, but they are not evidence that an agent built while blocked.
    """
    (migration / "report.json").write_text('{"workbooks": []}', encoding="utf-8")
    (migration / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    emitted = migration / "semantic_models" / "Orders.SemanticModel" / "definition" / "tables"
    emitted.mkdir(parents=True)
    artifact = emitted / "Orders.tmdl"
    artifact.write_text("table Orders", encoding="utf-8")
    _write_engine_receipt(migration, [artifact])

    run_gate("block", str(migration), "--sources", "warehouse")
    result = run_gate("verify", str(migration))

    assert result.returncode == 0
    assert "PRE-GATE TIER OUTPUT" in (result.stdout + result.stderr)


def test_verify_allows_receipted_reports_engine_artifacts(migration: Path) -> None:
    """`reports/` is pristine engine output too; a matching receipt must exempt it (#172)."""
    (migration / "report.json").write_text('{"workbooks": []}', encoding="utf-8")
    (migration / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    report = migration / "reports" / "Orders.Report"
    visual = report / "definition" / "pages" / "Page1" / "visuals" / "Visual1" / "visual.json"
    visual.parent.mkdir(parents=True)
    pbir = report / "definition.pbir"
    pbir.write_text('{"version":"4.0"}', encoding="utf-8")
    visual.write_text('{"visualType":"barChart"}', encoding="utf-8")
    _write_engine_receipt(migration, [pbir, visual])

    run_gate("block", str(migration), "--sources", "warehouse")
    result = run_gate("verify", str(migration))

    assert result.returncode == 0
    assert "PRE-GATE TIER OUTPUT" in (result.stdout + result.stderr)


def test_verify_rejects_changed_reports_engine_artifacts(migration: Path) -> None:
    """The reports exemption is receipt-backed, not a blanket allow-list."""
    (migration / "report.json").write_text('{"workbooks": []}', encoding="utf-8")
    (migration / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    report = migration / "reports" / "Orders.Report"
    visual = report / "definition" / "pages" / "Page1" / "visuals" / "Visual1" / "visual.json"
    visual.parent.mkdir(parents=True)
    visual.write_text('{"visualType":"barChart"}', encoding="utf-8")
    _write_engine_receipt(migration, [visual])
    visual.write_text('{"visualType":"lineChart"}', encoding="utf-8")

    run_gate("block", str(migration), "--sources", "warehouse")
    result = run_gate("verify", str(migration))

    assert result.returncode == 1
    assert "visual.json" in (result.stdout + result.stderr)


def test_verify_still_flags_agent_artifacts_in_engine_roots_when_not_receipted(migration: Path) -> None:
    """The provenance exception must not become a fail-open blanket for the engine path."""
    (migration / "report.json").write_text('{"workbooks": []}', encoding="utf-8")
    (migration / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    emitted = migration / "semantic_models" / "Orders.SemanticModel" / "definition" / "tables"
    emitted.mkdir(parents=True)
    engine_artifact = emitted / "Orders.tmdl"
    engine_artifact.write_text("table Orders", encoding="utf-8")
    _write_engine_receipt(migration, [engine_artifact])

    run_gate("block", str(migration), "--sources", "warehouse")

    agent_output = migration / "semantic_models" / "Agent.SemanticModel" / "definition" / "tables" / "AgentModel.tmdl"
    agent_output.parent.mkdir(parents=True)
    agent_output.write_text("table AgentModel", encoding="utf-8")
    os.utime(agent_output, (946684800, 946684800))

    result = run_gate("verify", str(migration))
    assert result.returncode == 1
    assert "AgentModel.tmdl" in (result.stdout + result.stderr)


def test_verify_rejects_a_mismatched_engine_receipt(migration: Path) -> None:
    """A receipt whose artifact hashes no longer match must not launder current artifacts."""
    (migration / "report.json").write_text('{"workbooks": []}', encoding="utf-8")
    (migration / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    emitted = migration / "semantic_models" / "Orders.SemanticModel" / "definition" / "tables"
    emitted.mkdir(parents=True)
    artifact = emitted / "Orders.tmdl"
    artifact.write_text("table Orders", encoding="utf-8")
    _write_engine_receipt(migration, [artifact])
    artifact.write_text("table Orders\n// agent changed it", encoding="utf-8")

    run_gate("block", str(migration), "--sources", "warehouse")

    result = run_gate("verify", str(migration))
    assert result.returncode == 1
    assert "Orders.tmdl" in (result.stdout + result.stderr)


def test_verify_rejects_a_foreign_run_receipt_with_matching_artifacts(migration: Path) -> None:
    """Only _receipt_matches_bundle can reject this: artifacts match, run markers do not."""
    (migration / "report.json").write_text('{"workbooks": []}', encoding="utf-8")
    (migration / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    emitted = migration / "semantic_models" / "Orders.SemanticModel" / "definition" / "tables"
    emitted.mkdir(parents=True)
    artifact = emitted / "Orders.tmdl"
    artifact.write_text("table Orders", encoding="utf-8")
    _write_engine_receipt(migration, [artifact])

    (migration / "report.json").write_text('{"workbooks": ["other-run"]}', encoding="utf-8")

    run_gate("block", str(migration), "--sources", "warehouse")
    result = run_gate("verify", str(migration))

    assert result.returncode == 1


def test_verify_rejects_receipt_written_after_the_gate_arm(migration: Path) -> None:
    """A helper-minted receipt after block is traceable drift, not engine output."""
    (migration / "report.json").write_text('{"workbooks": []}', encoding="utf-8")
    (migration / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    run_gate("block", str(migration), "--sources", "warehouse")

    emitted = migration / "semantic_models" / "Agent.SemanticModel" / "definition" / "tables"
    emitted.mkdir(parents=True)
    artifact = emitted / "Agent.tmdl"
    artifact.write_text("table Agent", encoding="utf-8")
    _write_engine_receipt(migration, [artifact])

    result = run_gate("verify", str(migration))
    assert result.returncode == 1
    assert "Agent.tmdl" in (result.stdout + result.stderr)


def test_verify_ignores_the_probe_sandbox_and_the_source_workbook(migration: Path) -> None:
    """The sanctioned exceptions must not self-report a violation.

    `_probe/` is built WHILE the gate is up - that is how a clear is earned, so flagging it would
    make the gate impossible to satisfy. `source/` is the input we were handed, and `reference/`
    holds Tableau screenshots; neither is something we built.
    """
    run_gate("block", str(migration), "--sources", "warehouse")
    for relative, name in (
        ("_probe", "Probe.tmdl"),
        ("source", "workbook.twbx"),
        ("source", "bundled.hyper"),
        ("reference", "tableau-page.csv"),
    ):
        folder = migration / relative
        folder.mkdir(exist_ok=True)
        (folder / name).write_text("x")

    result = run_gate("verify", str(migration))
    assert result.returncode == 0, f"sanctioned paths must not trip the gate: {result.stdout}{result.stderr}"


def test_verify_does_not_create_directories(tmp_path: Path) -> None:
    """`verify` is a post-hoc check and must not mutate the tree it judges.

    `denied_dirs` deliberately creates `fabric/` (the ACL needs a directory to apply to), so the
    audit surface has to be a separate, read-only function - otherwise a read-only verification
    conjures a phantom directory into every migration it inspects.
    """
    run_gate("verify", str(tmp_path))
    assert not (tmp_path / "fabric").exists(), "verify must not create fabric/"


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

    #543 additionally hardened the enforcement half. `block`'s exit code used to be discarded here,
    so a run where `icacls /deny` FAILED still reached the deliverable assertion; the failure was
    then absorbed by whichever fail-closed guard fired next instead of failing at setup. The ACL is
    now proven three independent ways - the arm's exit code, the ACE icacls reports for THIS
    account, and the syscall itself - because none of the three implies the others.
    """
    try:
        armed = run_gate("block", str(migration), "--sources", "shipment")
        assert armed.returncode == 0, (
            f"the gate must ARM before enforcement can be asserted:\n{armed.stdout}{armed.stderr}"
        )
        actions = _audit_actions(migration)
        assert actions and actions[-1] in BLOCK_ACTIONS, f"arming must be recorded as a block: {actions}"

        # SIBLING of fabric/, not a child - that placement is the fix. A sandbox inside the denied
        # tree inherits the deny, which is what caused the deadlock in the first place.
        probe = migration / "_probe" / "Probe.SemanticModel" / "definition" / "tables"
        probe.mkdir(parents=True, exist_ok=True)
        (probe / "shipment.tmdl").write_text("table shipment", encoding="utf-8")
        assert (probe / "shipment.tmdl").exists(), "the probe must be able to build, or the gate deadlocks"

        # The other half - that the DELIVERABLE stays blocked - is the only assertion here that
        # needs the kernel ACL, so it is the only thing guarded by platform. Everything above holds
        # anywhere.
        if platform.system() != "Windows":
            pytest.skip("write-deny enforcement is an icacls ACL; the marker-only path cannot block a write")

        assert actions[-1] == "block", f"the enforced path must record `block`, not marker-only: {actions}"
        code, out, rights = _deny_rights_for_current_account(migration / "fabric")
        assert code == 0, f"icacls could not inspect the denied directory, so the ACE is unproven:\n{out}"
        assert any(DENY_ACE_RIGHTS <= granted for granted in rights), (
            f"icacls must report a (DENY) ACE carrying {sorted(DENY_ACE_RIGHTS)} for this account; "
            f"found {[sorted(granted) for granted in rights]}:\n{out}"
        )
        with pytest.raises(PermissionError):
            (migration / "fabric" / "Deliverable.tmdl").write_text("table x", encoding="utf-8")
        assert not (migration / "fabric" / "Deliverable.tmdl").exists(), "the denied write must not have landed"
    finally:
        _assert_gate_fully_cleared(migration)


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

    The forged entry carries the migration's own `scope` - a real same-user forger can trivially
    copy that field too, so omitting it here would test something else entirely: issue #354's
    mixed-scope-poisoning requirement now makes an entry with no/mismatched `scope` untrust the
    WHOLE trail (exit 3, "cannot assess"), not just this one entry. Stamping the real scope isolates
    the ordering assertion this test is actually about from that unrelated, newer check.
    """
    import json as _json

    run_gate("block", str(migration), "--sources", "x")
    run_gate("clear", str(migration), "--reason", "sneaky")
    audit = migration / ".credential-gate-audit.log"
    forged = _json.dumps(
        {
            "ts": "2020-01-01T00:00:00+00:00",
            "action": "probe-cleared",
            "detail": "forged",
            "user": "test",
            "scope": str(migration.resolve()),
        }
    )
    audit.write_text(audit.read_text(encoding="utf-8") + forged + "\n", encoding="utf-8")
    (migration / "fabric" / "M.tmdl").write_text("table x", encoding="utf-8")
    assert run_gate("verify", str(migration)).returncode == 1, "backdated evidence must not count"


def test_every_block_action_invalidates_earlier_evidence_on_EVERY_platform() -> None:
    """The gate's ordering guarantee must not be Windows-only, silently.

    Measured 2026-08-03: `apply_block` records `block` on Windows but `block-marker-only` where
    there is no icacls, while `_clear_was_earned` and `_last_block_sources` recognised only `block`.
    So on Linux/macOS a `probe-cleared` recorded BEFORE a re-arm still counted as earned AFTER it -
    backdated evidence survived exactly the event that exists to invalidate it, and `verify` said OK.

    CI had been reporting this for four consecutive runs and it was read as "those tests are
    Windows-specific". Asserting the invariant directly, with no subprocess and no platform branch,
    is what makes the next such failure unambiguous.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import credential_gate as cg  # noqa: PLC0415

    assert cg.BLOCK_ACTIONS == {"block", "block-marker-only"}, (
        "a new arming action was added without deciding whether it invalidates prior evidence; "
        "every action that ARMS the gate must be in BLOCK_ACTIONS or the ordering guarantee leaks"
    )
    source = (REPO / "scripts" / "credential_gate.py").read_text(encoding="utf-8")
    readers = source.split("def _icacls", 1)[0]
    assert '== "block"' not in readers and '!= "block"' not in readers, (
        "a reader is comparing the audit action against the bare string 'block'. That silently "
        "excludes 'block-marker-only', which is how the non-Windows ordering hole was introduced. "
        "Compare against BLOCK_ACTIONS."
    )


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
    """Conflating verdicts is the defect class this whole script exists to remove.

    BAD_TABLE is checked before NO_CREDENTIAL on purpose: a "not found" message proves the server
    answered us, so it cannot be a credential problem - but the text often also mentions the
    connection and would otherwise trip a credential marker and send a user hunting for a sign-in
    they do not need.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import _classify_failure  # noqa: PLC0415

    cases = [
        ("Table or view not found: shipment", "BAD_TABLE"),
        ("[TABLE_OR_VIEW_NOT_FOUND] the table cannot be found", "BAD_TABLE"),
        ("Invalid object name 'dbo.orders'", "BAD_TABLE"),
        ("The credential was not provided; please sign in", "NO_CREDENTIAL"),
        ("Exception: no catalog found on the instance", "ERROR"),
        ("something else entirely went wrong", "ERROR"),
    ]
    for text, expected in cases:
        assert _classify_failure(text, network_fault_observed=False)[0] == expected, (
            f"{text!r} should classify as {expected}"
        )


def test_unknown_refresh_failure_is_not_reported_as_unreachable_and_keeps_the_message_head() -> None:
    """A fallback verdict must not invent a network fault or hide the useful exception message."""
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import _classify_failure  # noqa: PLC0415

    head = "DataSource.Error: The connector returned an application-specific refusal before refresh."
    tail = "\n".join(f"   at Microsoft.PowerBI.Some.Stack.Frame{i}()" for i in range(80))
    verdict, detail = _classify_failure(f"{head}\n{tail}", network_fault_observed=False)

    assert verdict == "ERROR"
    assert head in detail
    assert "unclassified" in detail.lower()


def test_lineage_check_fails_closed_on_an_unknown_chain() -> None:
    """The bug that let a forged authorization through, locked down.

    The first version returned False for an empty chain, so a lineage query that failed - which
    happened for real under four concurrent agents - silently AUTHORIZED. "I could not tell" must
    never be read as permission.
    """
    if platform.system() != "Windows":
        pytest.skip("lineage check is Windows-only")
    sys.path.insert(0, str(REPO / "scripts"))
    from credential_gate import _has_copilot_ancestor  # noqa: PLC0415

    assert _has_copilot_ancestor([]) is True
    assert _has_copilot_ancestor(["<lineage-unavailable>"]) is True
    assert _has_copilot_ancestor(["python.exe", "pwsh.exe", "copilot.exe"]) is True
    assert _has_copilot_ancestor(["python.exe", "pwsh.exe", "explorer.exe"]) is False


def _classifier_output(tmp_path: Path) -> str:
    """Run the static classifier over a one-live-source spec and return everything it printed."""
    spec = tmp_path / "migration-spec.json"
    spec.write_text(
        json.dumps(
            {
                "data_sources": [
                    {
                        "id": "ds.x",
                        "connection": {
                            "class": "databricks",
                            "mode": "live",
                            "server": "adb-1.1.azuredatabricks.net",
                            "database": "db",
                            "http_path": "/sql/1.0/warehouses/abc",
                            "powerbi_target": "live_source",
                        },
                        "tables": [{"name": "t"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "fabric").mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "preflight_source_credentials.py"), "--spec", str(spec)],
        capture_output=True,
        text=True,
        check=False,
    )
    run_gate("clear", str(tmp_path), "--reason", "test-teardown")
    return (proc.stdout + proc.stderr).lower()


def test_the_classifier_sends_the_agent_to_the_probe_instead_of_terminating(tmp_path: Path) -> None:
    """The defect that shipped to this branch, pinned.

    `preflight_source_credentials.py` opens no socket - it cannot know whether a credential exists.
    It nonetheless printed an unconditional "STOP - A HUMAN MUST ACT / TERMINATE THE RUN NOW" for
    every live source. Measured 2026-08-02: 10 of 15 models obeyed it literally and never reached
    `probe_live_source.py`, and claude-opus-5 refused a FULLY CREDENTIALED, reachable warehouse on
    the happy path - a migration that would have succeeded in seconds.

    The classifier must withhold judgement and hand off to the measurement.
    """
    out = _classifier_output(tmp_path)

    assert "probe_live_source.py" in out, "the classifier must name the probe as the next action"

    forbidden = ["terminate the run", "a human must act", "you cannot fix this yourself"]
    present = [p for p in forbidden if p in out]
    assert not present, f"a socket-less classifier must not issue a terminal stop; found {present}"


def test_the_classifier_ACTUALLY_ARMS_the_gate_not_just_talks_about_it(tmp_path: Path) -> None:
    """End-to-end: the classifier must ARM the ACL, not merely print a warning about it.

    Measured 2026-08-03, and this one shipped to master: merging the credential-gate branch back
    into master auto-merged `preflight_source_credentials.py` with NO conflict, and in doing so
    silently took master's reverted version of one contiguous block - deleting `_write_gate_marker`,
    `_clear_gate_marker` and both call sites. Every other file in that merge conflicted visibly and
    was reviewed; this one did not, because the branch's later edits never textually overlapped the
    reverted hunk.

    The result was the worst possible failure shape: the classifier still printed the whole STOP
    directive, still exited 1, and still *looked* correct in every log - while arming nothing at all.
    The gate was completely inert on master and four freshly created fixtures came up unarmed.

    Every existing classifier test asserted only on its printed TEXT, so the entire suite stayed
    green. This test asserts the SIDE EFFECT instead - the marker on disk and the `block` entry in
    the audit log - which is the only thing that actually protects anything.
    """
    _classifier_output(tmp_path)  # runs the classifier, then tears the gate down

    audit = tmp_path / ".credential-gate-audit.log"
    assert audit.is_file(), (
        "the classifier produced NO audit log - it never invoked credential_gate.py at all, "
        "so nothing was armed no matter what it printed"
    )
    actions = [json.loads(line)["action"] for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert BLOCK_ACTIONS & set(actions), f"classifier must record a block action; got {actions}"


def test_the_classifier_still_forbids_building(tmp_path: Path) -> None:
    """Paired control: softening the directive must not soften the actual prohibition."""
    out = _classifier_output(tmp_path)
    assert "may not build" in out
    assert "proof required" in out


def test_the_terminal_stop_lives_in_the_probe_where_the_verdict_is_known() -> None:
    """The strong wording is not deleted - it moves to the component that can tell the difference.

    NO_CREDENTIAL and UNREACHABLE need OPPOSITE advice: one needs a human at a sign-in modal, the
    other needs a spec edit and no sign-in at all. Only a real connection attempt separates them.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import probe_live_source  # noqa: PLC0415

    src = Path(probe_live_source.__file__).read_text(encoding="utf-8").lower()
    assert "a human must act" in src
    assert "nobody needs to sign in" in src, "UNREACHABLE must not send the user to authenticate"


SNOWFLAKE_CONN = {
    "class": "snowflake",
    "server": "MYORG-ACCT001.snowflakecomputing.com",
    "database": "TABLEAU_MIGRATION",
    "schema": "PROBE",
    "warehouse": "PROBE_WH",
}


def _m(**overrides) -> str:
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import build_m_query  # noqa: PLC0415

    return build_m_query({**SNOWFLAKE_CONN, **overrides}, "SHIPMENT", "CUSTOMER")[0]


def test_a_snowflake_account_written_as_a_url_still_resolves(tmp_path: Path) -> None:
    """A URL-shaped account must not be misdiagnosed as UNREACHABLE.

    Snowflake accounts are routinely written as `https://ORG-ACCOUNT.snowflakecomputing.com/` - that
    is the form Snowsight shows and the form a .env carries. Un-normalized it breaks BOTH consumers
    at once: the DNS pre-check cannot resolve `https://host/`, so a perfectly good account is
    reported UNREACHABLE, and Snowflake.Databases would reject it too.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import _host_resolves, normalize_host  # noqa: PLC0415

    bare = "MYORG-ACCT001.snowflakecomputing.com"
    for written in (f"https://{bare}", f"https://{bare}/", bare, f"  {bare}.  ", f"https://{bare}/some/path"):
        assert normalize_host(written) == bare, f"failed to normalize {written!r}"
    assert _host_resolves(normalize_host(f"https://{bare}/")) is True
    assert f'Snowflake.Databases("{bare}"' in _m(server=f"https://{bare}/")


def test_snowflake_without_a_warehouse_fails_loudly_not_silently() -> None:
    """Snowflake cannot execute a query with no compute warehouse.

    Passing "" produced a refresh failure that the taxonomy would read as a reachability or
    credential problem - a wrong verdict for a spec bug. Databricks already raised for a missing
    http_path; Snowflake must match.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import build_m_query  # noqa: PLC0415

    conn = {k: v for k, v in SNOWFLAKE_CONN.items() if k != "warehouse"}
    with pytest.raises(ValueError, match="warehouse"):
        build_m_query(conn, "SHIPMENT", "CUSTOMER")


def test_snowflake_navigation_is_kind_qualified_like_power_bis_own_m() -> None:
    """Power BI's generated Snowflake M qualifies every navigation step with Kind.

    Without it a database and schema sharing a name navigate ambiguously. Databricks was already
    Kind-qualified; Snowflake silently was not.
    """
    m = _m()
    for kind in ("Database", "Schema", "Table"):
        assert f'Kind="{kind}"' in m, f"missing Kind={kind} in Snowflake navigation"


def test_snowflake_role_is_passed_through_when_the_spec_has_one() -> None:
    """Corporate accounts often need an explicit role - the user's default may have no grants."""
    assert 'Snowflake.Databases("MYORG-ACCT001.snowflakecomputing.com", "PROBE_WH", null)' in _m()
    assert '"PROBE_WH", [Role="ANALYST"]' in _m(role="ANALYST")


MUTATION_CASES = [
    ("icacls C:\\repo\\fabric", False),
    ("icacls C:\\repo\\fabric /deny gfranssens:(W)", True),
    ("icacls C:\\repo\\fabric /remove:d gfranssens", True),
    ("Get-Content .credential-gate-audit.log", False),
    ("cat .credential-gate-BLOCKED.json", False),
    ("Remove-Item .credential-gate-BLOCKED.json", True),
    ("Set-Content .credential-gate-AUTHORIZED -Value x", True),
    ("python scripts/credential_gate.py clear _probe-lab/v1", True),
    ("python scripts/credential_gate.py verify _probe-lab/v1", False),
    ("python scripts/credential_gate.py status _probe-lab/v1", False),
    ("takeown /f C:\\repo\\fabric", True),
    ("pytest tests/test_credential_gate.py", False),
]


def load_hook_module():
    """Import the HOOK by file path, under a name that cannot collide.

    `scripts/credential_gate.py` and `scripts/hooks/credential_gate.py` share a module name, and
    other tests here put `scripts/` on sys.path first - so a plain `import credential_gate` inside a
    test silently resolves to the WRONG module and every assertion fails on a missing attribute.
    """
    import importlib.util  # noqa: PLC0415

    path = REPO / "scripts" / "hooks" / "credential_gate.py"
    spec = importlib.util.spec_from_file_location("_gate_hook_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("command", "should_deny"), MUTATION_CASES)
def test_the_hook_matches_mutations_not_mentions(command: str, should_deny: bool) -> None:
    """The guard must distinguish CHANGING its control surface from merely NAMING it.

    The first version matched the bare word `icacls` and the control files' names anywhere in the
    payload. Measured 2026-08-03, that denied: a read-only `icacls <path>`, any command that READ
    the audit log, tearing down finished fixtures, and writing a test whose source quotes a control
    file's name. It ended in a real deadlock - a leftover marker armed the guard repo-wide, and the
    guard then blocked the edit that fixes the guard, so a human had to intervene by hand.

    Every False case below is work that was wrongly denied; every True case is the behaviour that
    must stay denied. Both halves matter: widening the matcher until the False cases pass is only
    safe while the True cases still fail.
    """
    sys.path.insert(0, str(REPO / "scripts" / "hooks"))
    assert load_hook_module()._mutates_control_surface(command) is should_deny


def test_a_write_tool_is_judged_on_its_path_not_its_content(migration: Path) -> None:
    """Writing a test ABOUT the gate must not be confused with forging the override.

    This is the case that produced the deadlock: `tests/test_credential_gate.py` necessarily quotes
    the override's filename, so a content match denied the very suite that defends the gate - and,
    worse, denied the edit that repairs the guard.

    Driven through the real hook rather than its internals, so the payload shapes are the ones the
    runtime actually sends.
    """
    run_gate("block", str(migration), "--sources", "shipment")

    a_test_file = migration / "tests" / "test_credential_gate.py"
    body = "assert '.credential-gate-AUTHORIZED' and '.credential-gate-BLOCKED.json'"
    allowed = run_hook(
        {
            "toolName": "create",
            "toolArgs": json.dumps({"path": str(a_test_file), "file_text": body}),
            "cwd": str(migration),
        }
    )
    assert allowed.get("permissionDecision") != "deny", "writing a test that quotes the names must be allowed"

    forged = run_hook(
        {
            "toolName": "create",
            "toolArgs": json.dumps({"path": str(migration / ".credential-gate-AUTHORIZED")}),
            "cwd": str(migration),
        }
    )
    assert forged.get("permissionDecision") == "deny", "forging the override must still be denied"

    deleted = run_hook(
        {
            "toolName": "powershell",
            "toolArgs": json.dumps({"command": f"Remove-Item {migration / '.credential-gate-BLOCKED.json'}"}),
            "cwd": str(migration),
        }
    )
    assert deleted.get("permissionDecision") == "deny", "deleting the marker must still be denied"


def test_a_write_tool_is_judged_on_its_path_not_a_mention_of_a_guarded_suffix(migration: Path) -> None:
    """Issue #228, class 2: content that merely NAMES a guarded suffix must not be denied.

    `_candidate_paths` used to run `_extract_args_text` (the WHOLE payload, including the file body
    being written) through a regex that matches any token ending in `.tmdl`/`.pbism`/etc. So writing
    `docs/notes.md` whose CONTENT documented "the Model.tmdl layout" was denied for a path
    ("Model.tmdl") the write never touched - reproduced against the hook before this fix landed.

    Every assertion here has its negative twin: a write that genuinely targets a guarded suffix, and
    a shell command that genuinely writes one, must both still be denied.
    """
    run_gate("block", str(migration), "--sources", "shipment")

    mentions_only = run_hook(
        {
            "toolName": "create",
            "toolArgs": json.dumps(
                {
                    "path": str(migration / "docs" / "notes.md"),
                    "file_text": "This note documents the Model.tmdl layout for future readers.",
                }
            ),
            "cwd": str(migration),
        }
    )
    assert mentions_only.get("permissionDecision") != "deny", (
        "a write whose CONTENT merely mentions a guarded suffix must be allowed"
    )

    real_write = run_hook(
        {
            "toolName": "create",
            "toolArgs": json.dumps({"path": str(migration / "fabric" / "Model.tmdl"), "file_text": "table Foo"}),
            "cwd": str(migration),
        }
    )
    assert real_write.get("permissionDecision") == "deny", "a genuine write to a guarded suffix must still be denied"

    shell_write = run_hook(
        {
            "toolName": "powershell",
            "toolArgs": json.dumps(
                {"command": f"Set-Content -Path {migration / 'fabric' / 'Model.tmdl'} -Value 'table Foo'"}
            ),
            "cwd": str(migration),
        }
    )
    assert shell_write.get("permissionDecision") == "deny", "a shell command that genuinely writes one must be denied"

    read_only = run_hook(
        {
            "toolName": "view",
            "toolArgs": json.dumps({"path": str(migration / "fabric" / "Model.tmdl")}),
            "cwd": str(migration),
        }
    )
    assert read_only.get("permissionDecision") != "deny", "a read-only tool must remain unaffected"


def test_apply_patch_target_is_read_from_the_header_not_the_diff_body(migration: Path) -> None:
    """apply_patch has no structured path key - its target lives in the patch HEADER line only.

    Its `toolArgs` is raw patch text, not JSON (see `_extract_args_text`'s docstring), so
    `_path_arguments` finds nothing for it; `_apply_patch_paths` reads the `*** Add/Update/Delete
    File:` header instead. Scanning the diff BODY as before would deny a patch that only ADDS a
    line mentioning a guarded suffix to an unrelated file - the same false positive, one tool over.
    """
    run_gate("block", str(migration), "--sources", "shipment")

    unrelated_target = migration / "docs" / "notes.md"
    mentions_only = run_hook(
        {
            "toolName": "apply_patch",
            "toolArgs": (
                f"*** Begin Patch\n*** Add File: {unrelated_target}\n"
                "+This note documents the Model.tmdl layout for future readers.\n*** End Patch\n"
            ),
            "cwd": str(migration),
        }
    )
    assert mentions_only.get("permissionDecision") != "deny", (
        "a patch whose ADDED LINE merely mentions a guarded suffix must be allowed"
    )

    real_target = migration / "fabric" / "Model.tmdl"
    real_write = run_hook(
        {
            "toolName": "apply_patch",
            "toolArgs": f"*** Begin Patch\n*** Add File: {real_target}\n+table Foo\n*** End Patch\n",
            "cwd": str(migration),
        }
    )
    assert real_write.get("permissionDecision") == "deny", "a patch that genuinely adds a guarded suffix file must deny"


def test_the_hook_lets_the_agent_inspect_the_gate_it_is_under(migration: Path) -> None:
    """Reading the audit log and the ACL is how an agent reports honestly - never deny it.

    Measured 2026-08-03: a read-only `icacls <path>` and a plain `Get-Content` of the audit log were
    both denied, which blocked legitimate inspection AND `credential_gate.py verify`, whose whole
    job is to read that log.
    """
    run_gate("block", str(migration), "--sources", "shipment")
    for command in (
        f"icacls {migration / 'fabric'}",
        f"Get-Content {migration / '.credential-gate-audit.log'}",
        f"python scripts/credential_gate.py verify {migration}",
    ):
        out = run_hook({"toolName": "powershell", "toolArgs": json.dumps({"command": command}), "cwd": str(migration)})
        assert out.get("permissionDecision") != "deny", f"inspection wrongly denied: {command}"


def test_the_hook_config_fails_open_when_the_script_is_broken() -> None:
    """A CRASHED guard must not brick the CLI - existence is not validity.

    Measured 2026-08-03: the script existed but raised NameError mid-edit. `Test-Path` was true, so
    the hook ran, crashed, and preToolUse failed CLOSED - denying every write and shell call,
    including the edit that would repair it. A restart could not fix it because the file was still
    there. A human had to edit it by hand.

    Failing open on a crash is the right trade: the ACL is the enforcement and this hook is only the
    explanation layer, so a crashed hook protects nothing while blocking everything.
    """
    cfg = json.loads((REPO / ".github" / "hooks" / "credential-gate.json").read_text(encoding="utf-8"))
    for event in ("preToolUse", "permissionRequest"):
        for entry in cfg["hooks"][event]:
            for shell in ("powershell", "bash"):
                cmd = entry[shell]
                assert "LASTEXITCODE" in cmd or "$?" in cmd, f"{event}/{shell} does not check the exit code"
                assert "'{}'" in cmd, f"{event}/{shell} has no allow-fallback"


def test_the_hook_still_denies_a_subagent_clearing_its_own_armed_gate(migration: Path) -> None:
    """The regression this whole fix exists to close, pinned.

    `clear_block()` has NO internal defense against being run unearned - it lifts the ACL
    unconditionally and only labels the audit entry `manual-clear` (vs `probe-cleared`) for
    `verify` to flag AFTERWARDS. `authorize` self-defends via `_has_copilot_ancestor`; `clear` does
    not. So this hook is the ONLY thing stopping a subagent from lifting its own armed gate by
    invoking the documented teardown verb directly.

    Measured 2026-08-03: an edit removed the match for `clear`/`authorize` entirely, reasoning
    (incorrectly) that credential_gate.py enforces both itself - true only for `authorize`. That
    reopened the simplest possible bypass: no ctypes, no ACL API, just the ordinary command.
    """
    run_gate("block", str(migration), "--sources", "shipment")
    cmd = f'python scripts/credential_gate.py clear {migration} --reason "self-clear attempt"'
    out = run_hook({"toolName": "powershell", "toolArgs": json.dumps({"command": cmd}), "cwd": str(migration)})
    assert out.get("permissionDecision") == "deny", "a subagent must not be able to clear its own armed gate"


def test_the_hook_does_not_block_teardown_of_an_unrelated_cleared_fixture(tmp_path: Path) -> None:
    """The false positive that caused today's real deadlock, pinned as a paired control.

    Without this control, tightening the test above could be satisfied by reverting to the
    tree-wide `_any_marker_under(cwd)` check - which is precisely what produced the deadlock: ANY
    leftover marker anywhere under cwd armed the guard for the WHOLE tree, so tearing down one
    finished, already-cleared fixture was denied merely because a DIFFERENT, unrelated fixture
    nearby was still gated. A human had to clear fixtures by hand before the hook's own fix could
    even be written.
    """
    armed = tmp_path / "variant-still-armed"
    cleared = tmp_path / "variant-already-cleared"
    (armed / "fabric").mkdir(parents=True)
    (armed / "migration-spec.json").write_text("{}", encoding="utf-8")
    (cleared / "fabric").mkdir(parents=True)
    (cleared / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(armed), "--sources", "shipment")
    run_gate("block", str(cleared), "--sources", "shipment")
    run_gate("clear", str(cleared), "--reason", "already earned")

    cmd = f"python scripts/credential_gate.py clear {cleared} --reason teardown"
    out = run_hook({"toolName": "powershell", "toolArgs": json.dumps({"command": cmd}), "cwd": str(tmp_path)})
    assert out.get("permissionDecision") != "deny", "teardown of an unrelated, already-cleared fixture must be allowed"

    run_gate("clear", str(armed), "--reason", "test-teardown")


def test_the_hook_denies_the_cd_plus_relative_clear_bypass(tmp_path: Path) -> None:
    """A real, live bypass this session, pinned verbatim.

    Measured 2026-08-03, mid-run, `claude-haiku-4.5` (variant-m5c, happy path): after its model was
    already built against a legitimately-probed source, it re-ran the credential classifier (which
    re-arms unconditionally, with no check for prior earned state), then - faced with its OWN
    re-armed gate - ran:

        cd _probe-lab/variant-m5c; python ../../scripts/credential_gate.py clear . 2>&1

    instead of re-probing. The hook's target resolution joined the bare `.` against the tool's
    STARTING cwd (the repo root - never gated), so `_targets_an_armed_gate` concluded "not armed"
    and ALLOWED the bypass. `credential_gate.py verify` caught the result afterward
    (`UNEARNED CLEAR ... UNVALIDATED`) - detection held - but enforcement should not have needed
    rescuing here. This is the exact command, replayed against a real armed marker.
    """
    run_gate("block", str(migration_fixture(tmp_path)), "--sources", "shipment")
    mig = tmp_path / "mig"
    cmd = f"cd {mig}; python ../../scripts/credential_gate.py clear . 2>&1"
    out = run_hook({"toolName": "powershell", "toolArgs": json.dumps({"command": cmd}), "cwd": str(tmp_path)})
    assert out.get("permissionDecision") == "deny", "the real observed bypass command must now be denied"
    run_gate("clear", str(mig), "--reason", "test-teardown")


def test_the_cd_fix_does_not_block_legitimate_teardown_via_the_same_pattern(tmp_path: Path) -> None:
    """Paired control: the SAME `cd <dir>; clear .` shape must still be allowed once earned.

    Without this, tightening the test above could be satisfied by denying every `cd`-then-`clear`
    command outright, regardless of whether the named directory is actually armed - which would
    just trade one over-broad failure mode for another.
    """
    mig = tmp_path / "mig2"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "x")
    run_gate("clear", str(mig), "--reason", "already earned")

    cmd = f"cd {mig}; python ../../scripts/credential_gate.py clear . 2>&1"
    out = run_hook({"toolName": "powershell", "toolArgs": json.dumps({"command": cmd}), "cwd": str(tmp_path)})
    assert out.get("permissionDecision") != "deny", "teardown of an already-cleared dir must stay allowed"


def migration_fixture(tmp_path: Path) -> Path:
    """Helper: an armed migration directory at tmp_path/mig, for tests that need the path fixed."""
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    return mig


def test_identity_unverified_is_classified_as_error_not_unreachable() -> None:
    """A LOCAL tooling failure must never come out as a claim about the customer's source.

    Measured 2026-08-03, live (gpt-5.6-sol, happy-path run against the CREDENTIALED warehouse):
    the refresh returned "model  : identity unverified (no model folder resolved for this pid)" -
    note the double space and colon between "model" and "identity", from the caller's own print
    formatting. The classifier's substring check required the exact phrase "model identity
    unverified" with no punctuation in between, so it never matched, and this fell through to the
    "no catalog" branch as a confident UNREACHABLE - telling the user to fix a server address that
    was reachable seconds earlier for a sibling run against the identical warehouse.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import _classify_failure  # noqa: PLC0415

    real_text = (
        "PROBE: UNREACHABLE the probe model failed to load even after waiting - the data source "
        "did not resolve. Check server and http_path in the spec before treating this as a "
        "credential problem. Raw: model  : identity unverified (no model folder resolved for this pid)\n"
        "REFRESH: ERROR RuntimeError: no catalog found on the Desktop Analysis Services instance"
    )
    verdict, detail = _classify_failure(real_text, network_fault_observed=False)
    assert verdict == "ERROR", f"a local pid-binding failure must classify as ERROR, got {verdict}"
    assert "local tooling failure" in detail.lower()
    assert "not a fact about the data source" in detail.lower()


def test_desktop_pid_binding_uses_exact_current_file_path_not_a_sibling_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sibling Desktop instance in the same migration tree must never be selected."""
    sys.path.insert(0, str(REPO / "scripts"))
    import probe_live_source  # noqa: PLC0415

    pbip = tmp_path / "mig" / "_probe" / "Probe.pbip"
    pbip.parent.mkdir(parents=True)
    pbip.write_text("{}", encoding="utf-8")
    sibling = tmp_path / "mig" / "fabric" / "Sibling.pbip"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("{}", encoding="utf-8")
    status = {
        "instances": [
            {"pid": 111, "currentFilePath": str(sibling.resolve())},
            {"pid": 222, "currentFilePath": str(pbip.resolve())},
        ]
    }

    monkeypatch.setattr(probe_live_source, "_npx", lambda _args, timeout: (0, json.dumps(status)))

    assert probe_live_source._pid_for_file(pbip) == 222


def test_duplicate_exact_current_file_path_refuses_to_bind_and_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Duplicate exact-path matches are ambiguous, so the probe must kill nothing."""
    sys.path.insert(0, str(REPO / "scripts"))
    import probe_live_source  # noqa: PLC0415

    pbip = tmp_path / "mig" / "_probe" / "run-a" / "Probe.pbip"
    pbip.parent.mkdir(parents=True)
    pbip.write_text("{}", encoding="utf-8")
    status = {
        "instances": [
            {"pid": 333, "currentFilePath": str(pbip.resolve())},
            {"pid": 444, "currentFilePath": str(pbip.resolve())},
        ]
    }
    stop_calls = []

    def fake_run(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        stop_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(probe_live_source, "_npx", lambda _args, timeout: (0, json.dumps(status)))
    monkeypatch.setattr(probe_live_source.subprocess, "run", fake_run)

    assert probe_live_source._pid_for_file(pbip) is None
    assert probe_live_source._close(333, pbip) is False
    assert stop_calls == []


def test_a_genuine_no_catalog_failure_still_classifies_as_unreachable() -> None:
    """Paired control: the fix must not swallow REAL load failures into ERROR."""
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import _classify_failure  # noqa: PLC0415

    verdict, _ = _classify_failure(
        "no catalog found on the Desktop Analysis Services instance", network_fault_observed=True
    )
    assert verdict == "UNREACHABLE"


def test_no_catalog_with_reachable_network_is_not_reported_as_unreachable() -> None:
    """UNREACHABLE must be earned by an observed network fault, not guessed from no-catalog text."""
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import _classify_failure  # noqa: PLC0415

    verdict, detail = _classify_failure(
        "no catalog found on the Desktop Analysis Services instance", network_fault_observed=False
    )
    assert verdict == "ERROR"
    assert "did not observe a network fault" in detail


def test_refresh_timeout_is_not_final_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeouts can be transient source stalls, so they must not become final credential stops."""
    sys.path.insert(0, str(REPO / "scripts"))
    import probe_live_source  # noqa: PLC0415

    def raise_timeout(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise subprocess.TimeoutExpired(cmd="refresh", timeout=1)

    monkeypatch.setattr(probe_live_source.subprocess, "run", raise_timeout)

    assert probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False) == (1, "ERROR")
    assert probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=True) == (
        1,
        "UNREACHABLE",
    )


def test_permission_failures_are_access_denied_not_credentials_or_retryable_errors() -> None:
    """Permission refusals are final, but the remedy is grant access rather than sign in."""
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import _classify_failure  # noqa: PLC0415

    cases = [
        "DataSource.Error: 403 Forbidden",
        "permission denied for table FACT_ORDERS",
        "SQL compilation error: insufficient privileges to operate on schema SALES",
    ]
    for text in cases:
        assert _classify_failure(text, network_fault_observed=False)[0] == "ACCESS_DENIED"


def test_the_probe_template_never_downgrades_the_tabular_compatibility_level() -> None:
    """A real Power BI Desktop crash, pinned.

    Measured 2026-08-03 ("Frown" feedback, a genuine Desktop crash mid-batch): "Tabular databases
    do not support CompatibilityLevel downgrade. Current CompatibilityLevel: '1606'. Requested
    CompatibilityLevel: '1567'." The probe's throwaway PBIP template requested 1567 - a value that
    appears NOWHERE else in this repo's real migrations, and lower even than the 1606 TOM already
    had cached for the AS instance the probe was opened into.

    This repo's own documented convention (superstore-sales-performance/migration-spec.json:
    "below this skill's own documented guidance of 1702+ for newly created models") is 1702+.
    Pinning the floor rather than the exact value, so a future bump to an even newer level does
    not fail this test for the wrong reason.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    from probe_live_source import _pbip_files  # noqa: PLC0415

    files = _pbip_files("Probe", "let\n    one = 1\nin\n    one", "t", "c")
    db_tmdl = files["Probe.SemanticModel/definition/database.tmdl"]
    match = re.search(r"compatibilityLevel:\s*(\d+)", db_tmdl)
    assert match, "database.tmdl must declare a compatibilityLevel"
    assert int(match.group(1)) >= 1702, f"probe template compat level {match.group(1)} is below this repo's 1702+ floor"


def _audit_actions(migration: Path) -> list[str]:
    """The ordered `action` sequence from a migration's audit log."""
    text = (migration / ".credential-gate-audit.log").read_text(encoding="utf-8")
    return [json.loads(line)["action"] for line in text.splitlines() if line.strip()]


# `block` (kernel ACL) and `block-marker-only` (non-Windows) both mean "the gate was armed"; they
# differ only in enforcement strength. Tests assert the SEMANTIC, so they pin the invariant on every
# platform rather than only where icacls exists - and so a CI failure means the gate is wrong, not
# that CI runs Linux. A suite that is red for a platform reason trains everyone to ignore it, which
# is how a real ordering defect here survived four consecutive red runs.
BLOCK_ACTIONS = frozenset({"block", "block-marker-only"})


def test_the_probe_tells_the_agent_not_to_kill_it_for_the_2_minute_cap() -> None:
    """A measured conflict between two pieces of this repo's own guidance, pinned.

    `AGENTS.md` says to cap an unresponsive external system at ~2 minutes. `probe_live_source.py`
    defaults to a 180s refresh timeout (plus up to 240s waiting for the catalog), so an agent
    applying that cap literally kills the probe BEFORE it can reach a verdict.

    Measured 2026-08-03 running the same fixture on two models:
      * gpt-5.6-sol  - killed the probe at ~120s citing the 2-minute rule. Gate held and nothing was
        built (safe), but the audit log recorded NO probe verdict at all, so afterwards there was no
        evidence a probe had ever run.
      * claude-opus-5 - let it finish, got `PROBE: NO_CREDENTIAL`, and the audit log recorded
        `probe-no_credential`.

    Both outcomes were safe; only one was accountable. The cap is a good rule aimed at an agent's own
    unbounded waiting, and it misfires here because this script IS the bounded timer.

    The fix has to reach the agent at the moment it would otherwise start its own clock, so it lives
    in the probe's OUTPUT, not only in persona prose - the same reasoning that put the classifier's
    STOP directive in tool output. This pins that the message is actually emitted before the wait.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    source = (REPO / "scripts" / "probe_live_source.py").read_text(encoding="utf-8")

    body = source.split("def _refresh_and_classify", 1)[1].split("\n    started = time.monotonic()", 1)[0]
    assert "DO NOT kill this process" in body, (
        "the probe must warn against being killed BEFORE it starts the long refresh wait - "
        "an agent applying the 2-minute cap has no way to know this script is itself the timer"
    )
    assert "self-terminates" in body and "ALWAYS prints a verdict" in body


def test_agents_md_deliberately_does_NOT_carry_the_probe_exemption() -> None:
    """The exemption lives in the probe's OUTPUT, not in the personas. That is a deliberate choice.

    Two reasons, both concrete:

    * **Budget.** The cap rule sits inside the `<!-- BEGIN:shared-conventions -->` block, which
      `sync_agent_conventions.py` copies verbatim into all four personas - three of which sit at ~99%
      of the 30,000-char cap. Measured: adding the exemption there pushed `pbi-semantic-builder` to
      30,045 chars, OVER cap, and `--check` failed. Persona text is the scarcest resource here.
    * **Reach.** Tool output is what actually lands. This repo already measured that agents follow
      tool output far more literally than their own persona prose - it is why the classifier's STOP
      directive lives in its printed output. The probe prints its warning the instant the long wait
      begins, which is exactly when an agent would otherwise start its own 2-minute clock.

    This test exists so nobody "helpfully" adds it back to AGENTS.md without re-checking the budget.
    If it ever SHOULD live in the personas, delete this test deliberately and offset the characters.
    """
    agents_md = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert (
        "probe_live_source.py"
        not in agents_md.split("BEGIN:shared-conventions", 1)[1].split("END:shared-conventions", 1)[0]
    ), (
        "the probe exemption was added to the shared-conventions block - that syncs into all four "
        "personas, which are at ~99% of the 30,000-char cap. Keep it in the probe's output instead, "
        "or offset the characters and delete this test on purpose."
    )


def test_rearming_an_already_probe_cleared_gate_is_a_no_op(tmp_path: Path) -> None:
    """The idempotency hole that INVITED a real bypass, pinned.

    Measured 2026-08-03 (`claude-haiku-4.5`, variant-m5c): a legitimate probe had cleared the gate
    and the model was built. The agent then re-ran the classifier, which re-armed unconditionally.
    Faced with its own freshly re-armed gate on a source it had already proven reachable minutes
    earlier, it bypassed rather than re-probed (`cd variant-m5c; clear .`). Re-arming a gate that a
    probe has already satisfied does not add safety - it manufactures the dead end.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "shipment")
    run_gate("clear", str(mig), "--reason", "probe ok", "--earned")

    run_gate("block", str(mig), "--sources", "shipment")

    assert not (mig / ".credential-gate-BLOCKED.json").exists(), "gate must NOT re-arm for already-proven sources"
    assert _audit_actions(mig)[-1] == "block-skipped"
    assert "GATE VERIFY: OK" in (run_gate("verify", str(mig)).stdout + run_gate("verify", str(mig)).stderr)


def test_rearming_after_a_BARE_clear_still_arms(tmp_path: Path) -> None:
    """Control: an UNEARNED clear must not buy permanent immunity from the gate.

    Without this, the idempotency fix would launder `clear` (which earns nothing - it only labels
    the audit entry `manual-clear`) into "this migration can never be gated again", handing the
    bypass a far better tool than the one it replaced.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "shipment")
    run_gate("clear", str(mig), "--reason", "I decided it is fine")  # NOT --earned

    run_gate("block", str(mig), "--sources", "shipment")
    try:
        assert (mig / ".credential-gate-BLOCKED.json").exists(), "a bare manual-clear must NOT prevent re-arming"
        assert _audit_actions(mig)[-1] in BLOCK_ACTIONS
    finally:
        run_gate("clear", str(mig), "--reason", "test-teardown")


def test_source_specific_clear_keeps_gate_for_still_pending_sources(tmp_path: Path) -> None:
    """Clearing one source from a multi-source marker must not open writes for the rest."""
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "orders", "customers")

    run_gate("clear", str(mig), "--reason", "orders probe ok", "--earned", "--sources", "orders")

    marker = json.loads((mig / ".credential-gate-BLOCKED.json").read_text(encoding="utf-8"))
    assert marker["sources"] == ["customers"]
    assert run_gate("status", str(mig)).returncode == 1

    run_gate("clear", str(mig), "--reason", "customers probe ok", "--earned", "--sources", "customers")
    assert not (mig / ".credential-gate-BLOCKED.json").exists()


def test_a_sibling_block_does_not_discard_another_source_clearance(tmp_path: Path) -> None:
    """A bundle shared by sibling agents must remember each source's earned clearance independently."""
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "orders")
    run_gate("clear", str(mig), "--reason", "probe ok", "--earned", "--sources", "orders")

    run_gate("block", str(mig), "--sources", "customers")
    run_gate("clear", str(mig), "--reason", "probe ok", "--earned", "--sources", "customers")
    run_gate("block", str(mig), "--sources", "orders")

    assert not (mig / ".credential-gate-BLOCKED.json").exists(), "orders clearance must survive a sibling block"
    assert _audit_actions(mig)[-1] == "block-skipped"


def test_a_sibling_manual_clear_does_not_launder_an_unproven_source(tmp_path: Path) -> None:
    """Source-aware state must not turn one earned source into a pass for another source."""
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "orders")
    run_gate("clear", str(mig), "--reason", "probe ok", "--earned", "--sources", "orders")
    run_gate("block", str(mig), "--sources", "customers")
    run_gate("clear", str(mig), "--reason", "manual teardown")
    (mig / "fabric" / "model.tmdl").write_text("table Customers")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 1, out
    assert "UNEARNED CLEAR" in out


def test_rearming_with_a_NEW_source_still_arms(tmp_path: Path) -> None:
    """Control: a source that was never probed must still be gated.

    The skip is keyed on the source list, not merely on "was previously cleared". If the spec gains
    a live source, that source has no reachability evidence at all, so the gate must re-arm even
    though a DIFFERENT source was legitimately proven earlier.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "shipment")
    run_gate("clear", str(mig), "--reason", "probe ok", "--earned")

    run_gate("block", str(mig), "--sources", "shipment", "orders")
    try:
        assert (mig / ".credential-gate-BLOCKED.json").exists(), "a newly-added live source must re-arm the gate"
        assert _audit_actions(mig)[-1] in BLOCK_ACTIONS
    finally:
        run_gate("clear", str(mig), "--reason", "test-teardown")


def test_the_rearm_skip_is_order_insensitive_on_sources(tmp_path: Path) -> None:
    """Source ORDER is a classifier implementation detail, not a change in what was proven."""
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "shipment", "orders")
    run_gate("clear", str(mig), "--reason", "probe ok", "--earned")

    run_gate("block", str(mig), "--sources", "orders", "shipment")

    assert not (mig / ".credential-gate-BLOCKED.json").exists(), "reordered but identical sources must not re-arm"
    assert _audit_actions(mig)[-1] == "block-skipped"


def test_an_extract_only_migration_that_was_never_gated_verifies_CLEAN(tmp_path: Path) -> None:
    """The false BLOCK on the final gate, pinned.

    Measured 2026-08-08 (`book_5-2-LOD`, one embedded `excel-direct` datasource, zero live sources):
    workflow step 15 runs `verify` on EVERY migration, but step 6 correctly raises no gate when
    nothing is live. `verify` then found artifacts, no deny-ACE and no `probe-cleared`/`authorize`
    entry, and concluded the gate had been lifted unearned - reporting `UNEARNED CLEAR ... this model
    is UNVALIDATED. Do not ship it.` and exiting 1 for a migration that was never gated and had
    nothing to probe.

    Two reasons this mattered more than a cosmetic wrong message: it fires on exactly the shape most
    likely to be run fully offline, and it fires at the LAST step, after all the work - the point
    where a spurious "do not ship" is most expensive and most likely to be believed.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    (mig / "fabric" / "model.tmdl").write_text("table Orders")  # a real, audited artifact

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"a never-gated extract-only migration must verify clean:\n{out}"
    assert "UNEARNED" not in out
    # The verdict must say WHY it passed, so this is never mistaken for a gate that WAS lifted.
    assert "no gate was ever applied" in out


def test_a_genuinely_unearned_clear_is_STILL_reported(tmp_path: Path) -> None:
    """Control - the whole point of the check above must survive the fix.

    Identical to the test above except a gate really was applied and then lifted by a bare `clear`
    (which earns nothing). Artifacts built after that are unvalidated, and `verify` must still say so.
    If this ever passes with exit 0, the fix has laundered the bypass it was supposed to leave alone.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "shipment")
    run_gate("clear", str(mig), "--reason", "I decided it is fine")  # NOT --earned
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 1, f"artifacts after an unearned clear must still BLOCK:\n{out}"
    assert "UNEARNED CLEAR" in out


def test_an_earned_clear_still_verifies_clean_for_a_live_source(tmp_path: Path) -> None:
    """Control - the legitimate live-source path is unchanged by the never-gated branch.

    Asserts the reason as well as the exit code: this must pass because the probe EARNED the lift,
    not because it fell through the `_gate_was_ever_applied` escape hatch.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "shipment")
    run_gate("clear", str(mig), "--reason", "probe returned a row", "--earned")
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert "no gate was ever applied" not in out, "this migration WAS gated - it passed by earning the lift"


# --- issue #354: `verify` must not pass vacuously at a ship destination with no audit history ---
#
# The mandated final gate reads `.credential-gate-audit.log` at EXACTLY the path it is given, and
# nowhere else. The documented ship step copies built artifacts from an engine bundle to
# `migrations/{workbooks,datasources}/<slug>/fabric/` - a location that never had a gate armed at
# it, so it never got an audit log either. Before this fix, `verify` there reported "OK - no gate
# was ever applied" (exit 0), indistinguishable from a migration that genuinely never needed one.
#
# Three states, three exit codes:
#   0 - a gate was applied and passed, or genuinely never needed one, at a legitimate gate root
#   1 - a gate was applied and failed (unchanged, pre-existing)
#   3 - no audit history exists here AT ALL, and this is not a place a gate could have been armed


def test_a_ship_destination_copy_with_no_audit_history_CANNOT_be_verified_ok(tmp_path: Path) -> None:
    """State 3: the exact vacuous-pass reproduction from issue #354, pinned.

    A directory that carries built model artifacts (as a plain copy of engine-bundle output would),
    but none of the scope markers (`migration-spec.json`, `engine-output-receipt.json`,
    `input_manifest.json`) that would make it a legitimate place for a gate to have been armed, and
    no `.credential-gate-audit.log` either. This is the shape of
    `migrations/workbooks/<name>/fabric/` after the documented sign-off copy: real artifacts, zero
    audit history. `verify` must refuse to call this clean.
    """
    ship_destination = tmp_path / "migrations" / "workbooks" / "some-dash" / "fabric"
    ship_destination.mkdir(parents=True)
    (ship_destination / "model.tmdl").write_text("table Shipment")  # copied engine output, no receipt beside it

    proc = run_gate("verify", str(ship_destination))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"a no-audit-history ship copy must not verify OK or as a violation:\n{out}"
    assert "CANNOT ASSESS" in out
    assert "GATE VERIFY: OK" not in out, "must never print the same verdict as a genuine never-gated pass"


def test_extract_only_at_its_own_spec_root_still_verifies_ok_state_1(tmp_path: Path) -> None:
    """Negative control: a genuine pass must still exit 0 (state 1 is not swallowed by state 3).

    Identical shape to the state-3 fixture above - artifacts, no audit log - except this directory
    DOES carry `migration-spec.json`, exactly like a real parser-path migration that correctly never
    had a gate armed (extract-only). An over-correction that also fails this would be its own
    defect: `test_an_extract_only_migration_that_was_never_gated_verifies_CLEAN` already locks this
    in structurally; this test additionally locks in the exit code contract from issue #354's fix.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    (mig / "fabric" / "model.tmdl").write_text("table Orders")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"a legitimate never-gated migration must still verify clean:\n{out}"
    assert "CANNOT ASSESS" not in out


def test_a_gate_applied_and_failed_at_its_own_bundle_root_is_still_state_2(tmp_path: Path) -> None:
    """State 2 (gate applied and failed) must still exit 1, unaffected by the new state-3 check.

    This is the pre-existing VIOLATION path: verified AT the bundle/spec root that actually carries
    the audit log, so `_no_audit_trail_reason` must stand aside and let the existing violation
    logic run.

    The artifact is landed BEFORE the gate is armed, deliberately (#543). It used to be written
    afterwards, so on Windows - where `block` arms a REAL write-deny ACL on `fabric/`, see
    `denied_dirs` - the write raised `PermissionError` and the node skipped, meaning the state-2
    assertion below never ran on any machine where enforcement actually WORKS. Pre-landing it
    reproduces the state this check exists for (artifacts sitting under an armed gate) identically
    on the ACL and the marker-only branch, with no skip and no dependence on enforcement being weak.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text("{}", encoding="utf-8")
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")  # built first, then gated over

    try:
        armed = run_gate("block", str(mig), "--sources", "shipment")
        assert armed.returncode == 0, f"the gate must ARM before state 2 can be asserted:\n{armed.stdout}{armed.stderr}"
        expected_action = "block" if platform.system() == "Windows" else "block-marker-only"
        actions = _audit_actions(mig)
        assert actions and actions[-1] == expected_action, f"arming must be recorded as {expected_action}: {actions}"

        proc = run_gate("verify", str(mig))
        out = proc.stdout + proc.stderr

        assert proc.returncode == 1, f"artifacts built while the gate is applied must still VIOLATE:\n{out}"
        assert "VIOLATION" in out
        assert "CANNOT ASSESS" not in out
    finally:
        _assert_gate_fully_cleared(mig)


def test_an_empty_ship_destination_with_no_artifacts_is_not_flagged(tmp_path: Path) -> None:
    """No artifacts, nothing to assess: state 3 must not fire on an empty/not-yet-built directory."""
    ship_destination = tmp_path / "migrations" / "workbooks" / "not-started-yet" / "fabric"
    ship_destination.mkdir(parents=True)

    proc = run_gate("verify", str(ship_destination))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, out
    assert "CANNOT ASSESS" not in out


# --- issue #354, review round 3 (scoped DOWN): three bounded fail-closed fixes only -------------
#
# A third review round explicitly REJECTED the content-sniffing "does the spec merely NAME a live
# source" classifier that used to live here and required three narrower, precisely-bounded fixes
# instead - each one reusing an EXISTING classifier/helper, never a new one:
#   1. never-gated classification now defers entirely to `preflight_source_credentials._classify_legs`
#      (the SAME canonical classifier - `connection_target.powerbi_target` - that arms the gate in
#      the first place) and requires an EXPLICIT, VALID `flat_file` verdict on EVERY declared
#      source; anything else (unknown, missing, malformed, unsupported, or a live target) is
#      CANNOT ASSESS, never assumed extract-only by default.
#   2. `_audit_entries` no longer skips a single malformed line and keeps trusting whatever else
#      parsed - ANY nonblank unparseable line now poisons the WHOLE trail. An entry with NO `scope`
#      key at all is no longer trusted as "legacy" evidence either - only an entry naming THIS
#      directory counts.
#   3. a trusted `block`/earned-`clear` history is now also checked against what the CURRENT spec
#      declares: if the cleared source's identity key was swapped for a different one in the same
#      directory, that stale clearance no longer certifies what is here today.
#
# Residual, deliberately left open (tracked in #391, NOT solved here): a copied `flat_file` spec
# placed beside artifacts from an unrelated, live-source build is not provably attributable to
# those specific artifacts without a receipt/run-identity correlation mechanism. Issue #354 stays
# open after this partial fix.


def test_an_unknown_stamped_target_CANNOT_be_verified_ok(tmp_path: Path) -> None:
    """Requirement 1: an EXPLICIT `powerbi_target: unknown` stamp is not extract-only by default.

    `classify_source` returns "review" (neither `no-creds` nor `needs-credential`) for anything
    that is not a validated `flat_file` - an unknown stamp must fail closed, not fall through to a
    vacuous pass. `class`/`server` are included so `_leg_key` can compute a stable identity and the
    leg is genuinely classified `review`, rather than falling back to `needs-credential` for having
    no identifying fields at all (that fallback is covered separately, below).
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(
        json.dumps(
            {
                "data_sources": [
                    {"connection": {"class": "some-mystery-system", "server": "host1", "powerbi_target": "unknown"}}
                ]
            }
        ),
        encoding="utf-8",
    )
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"an unknown-stamped target must not verify OK:\n{out}"
    assert "CANNOT ASSESS" in out


def test_a_missing_stamp_with_no_classifiable_class_CANNOT_be_verified_ok(tmp_path: Path) -> None:
    """Requirement 1: a connection with no `powerbi_target` AND no `class` is not extract-only.

    Legacy/unstamped specs must be classified through the same canonical
    `connection_target.powerbi_target` computation the parser itself uses - here there is nothing
    to compute FROM, so it resolves to `unknown` and must fail closed exactly like the explicit
    unknown stamp above.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"connection": {}}]}),
        encoding="utf-8",
    )
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"an unstamped, unclassifiable connection must not verify OK:\n{out}"
    assert "CANNOT ASSESS" in out


def test_a_genuine_all_flat_file_spec_still_verifies_ok(tmp_path: Path) -> None:
    """Negative control for requirement 1: every source EXPLICITLY and VALIDLY `flat_file` passes.

    Replaces the rejected `test_b1_negative_control...` fixture, which asserted a pass for
    `powerbi_target: "extract"` - not a real classification value, and which CANNOT pass under the
    tightened allow-list. A genuine flat-file stamp (what the parser actually writes for a packaged
    Excel/CSV extract) must still verify clean.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"connection": {"class": "excel-direct", "powerbi_target": "flat_file"}}]}),
        encoding="utf-8",
    )
    (mig / "fabric" / "model.tmdl").write_text("table Orders")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"a genuinely all-flat-file spec must still verify clean:\n{out}"
    assert "CANNOT ASSESS" not in out


def test_a_live_stamped_target_with_no_audit_CANNOT_be_verified_ok(tmp_path: Path) -> None:
    """A copied spec explicitly naming a live source, with no audit trail, must not pass.

    Same shape `package_unit.py` would produce copying a real live-source spec into a handover
    package that was never itself gated - a gate SHOULD have existed here; its total absence is
    suspicious, not reassuring.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"connection": {"powerbi_target": "live_source"}}]}),
        encoding="utf-8",
    )
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"a copied spec naming a live source with no audit trail must not pass:\n{out}"
    assert "CANNOT ASSESS" in out


@pytest.mark.parametrize(
    "make_audit",
    [
        pytest.param(lambda p: p.write_bytes(b""), id="zero-byte"),
        pytest.param(lambda p: p.write_text('{"action": "block", "detail":', encoding="utf-8"), id="truncated-json"),
        pytest.param(lambda p: p.write_text("not json at all\n", encoding="utf-8"), id="malformed-json"),
        pytest.param(lambda p: p.mkdir(), id="directory-not-a-file"),
    ],
)
def test_an_unreadable_or_corrupt_audit_log_CANNOT_be_verified_ok(tmp_path: Path, make_audit) -> None:
    """Requirement 2: the audit PATH existing is not evidence - only successfully PARSED content is.

    Each variant here would satisfy a bare `.exists()` check and fall through to a false pass, even
    though nothing about it could actually be trusted.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"connection": {"powerbi_target": "live_source"}}]}),
        encoding="utf-8",
    )
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")
    make_audit(mig / ".credential-gate-audit.log")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"a corrupt/unreadable audit log must not be trusted as a pass:\n{out}"
    assert "CANNOT ASSESS" in out


def test_a_malformed_trailing_line_poisons_an_otherwise_genuine_history(tmp_path: Path) -> None:
    """Requirement 2, the reversal specifically: a REAL earned clearance is not immune to corruption.

    A previous implementation silently SKIPPED a single unparseable line and kept trusting whatever
    else parsed, so a genuinely earned clearance survived a corrupted trailing record. That is
    exactly backwards: any nonblank line that fails to parse must poison the WHOLE trail, because a
    log that can be partially forged/truncated and still "mostly" trusted is not trustworthy at all.

    Uses an `engine-output-receipt.json` bundle root (not `migration-spec.json`) so the state-3
    check cannot ALSO fire for spec-classification reasons - this isolates the audit-corruption
    path specifically.
    """
    mig = tmp_path / "bundle"
    (mig / "fabric").mkdir(parents=True)
    (mig / "engine-output-receipt.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(mig), "--sources", "shipment")
    run_gate("clear", str(mig), "--reason", "probe returned a row", "--earned")
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")
    audit = mig / ".credential-gate-audit.log"
    audit.write_text(audit.read_text(encoding="utf-8") + "not json at all\n", encoding="utf-8")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"a genuinely earned clearance must not survive a corrupted trailing line:\n{out}"
    assert "CANNOT ASSESS" in out


def test_an_unscoped_legacy_audit_copy_CANNOT_certify_this_directory(tmp_path: Path) -> None:
    """Requirement 2: an entry with NO `scope` key at all is legacy/unbound evidence, not trusted.

    A previous implementation trusted an entry carrying no `scope` field at all (pre-fix legacy
    shape) as-is - which meant a foreign log with its `scope` field stripped, rather than merely
    pointing at a different directory, would ALSO certify wherever it was copied. Missing scope
    must now fail exactly like mismatched scope.

    The recorded `sources` key deliberately MATCHES what the spec classifies today (computed via
    the real `_leg_key`), so requirement 3's source-set-mismatch check cannot ALSO produce a state-3
    verdict here - this isolates the missing-scope trust question specifically. No `block`/`clear`
    marker files exist either (the log is hand-written, not produced by the CLI), so a false pass
    would show up as exit 0, not a marker-enforcement violation.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import preflight_source_credentials as pf  # noqa: PLC0415

    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"connection": {"class": "sqlserver", "server": "host1", "database": "db"}}]}),
        encoding="utf-8",
    )
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")
    key = pf._leg_key({}, 0, {"class": "sqlserver", "server": "host1", "database": "db"})
    (mig / ".credential-gate-audit.log").write_text(
        json.dumps({"action": "block", "sources": [key]})
        + "\n"
        + json.dumps({"action": "probe-cleared", "sources": [key]})
        + "\n",
        encoding="utf-8",
    )

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"an unscoped legacy audit copy must not certify this directory:\n{out}"
    assert "CANNOT ASSESS" in out


def test_a_foreign_audit_log_copied_in_from_another_scope_CANNOT_be_verified_ok(tmp_path: Path) -> None:
    """Requirement 2: a real, valid audit log is not proof for a directory it was never written for.

    Arms and earns a clearance in scope A, then copies that exact, byte-for-byte valid log into
    unrelated scope B. Before scope-binding, B's `verify` read A's history as its own - the gate
    became forgeable by `cp`.
    """
    scope_a = tmp_path / "scope-a"
    (scope_a / "fabric").mkdir(parents=True)
    (scope_a / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(scope_a), "--sources", "shipment")
    run_gate("clear", str(scope_a), "--reason", "probe returned a row", "--earned")

    scope_b = tmp_path / "scope-b"
    (scope_b / "fabric").mkdir(parents=True)
    (scope_b / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"connection": {"powerbi_target": "live_source"}}]}),
        encoding="utf-8",
    )
    (scope_b / "fabric" / "model.tmdl").write_text("table Shipment")
    (scope_b / ".credential-gate-audit.log").write_text(
        (scope_a / ".credential-gate-audit.log").read_text(encoding="utf-8"), encoding="utf-8"
    )

    proc = run_gate("verify", str(scope_b))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"a copied audit log from a different scope must not certify this one:\n{out}"
    assert "CANNOT ASSESS" in out


def test_negative_control_verifying_at_the_original_scope_still_passes(tmp_path: Path) -> None:
    """Negative control: the log's OWN scope must still verify clean, unaffected."""
    scope_a = tmp_path / "scope-a"
    (scope_a / "fabric").mkdir(parents=True)
    (scope_a / "migration-spec.json").write_text("{}", encoding="utf-8")
    run_gate("block", str(scope_a), "--sources", "shipment")
    run_gate("clear", str(scope_a), "--reason", "probe returned a row", "--earned")
    (scope_a / "fabric" / "model.tmdl").write_text("table Shipment")

    proc = run_gate("verify", str(scope_a))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"the scope the log was actually written for must still verify clean:\n{out}"


def test_a_swapped_live_source_key_makes_a_stale_clearance_unable_to_certify_it(tmp_path: Path) -> None:
    """Requirement 3: correctly-scoped evidence for a DIFFERENT source cannot cover this one.

    Arms and earns a clearance for one live source's real `_leg_key` identity, then the spec in the
    SAME directory is re-pointed at a different upstream without ever being re-gated. The audit
    trail is genuine and correctly scoped - it just names a source that no longer matches what the
    spec declares today.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import preflight_source_credentials as pf  # noqa: PLC0415

    def _spec(server: str) -> str:
        return json.dumps(
            {"data_sources": [{"connection": {"class": "sqlserver", "server": server, "database": "db"}}]}
        )

    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    mig_spec = mig / "migration-spec.json"
    mig_spec.write_text(_spec("e1.example"), encoding="utf-8")

    key_e1 = pf._leg_key({}, 0, {"class": "sqlserver", "server": "e1.example", "database": "db"})
    run_gate("block", str(mig), "--sources", key_e1)
    run_gate("clear", str(mig), "--reason", "probe returned a row", "--earned")
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")

    # Re-point the SAME directory's spec at a different upstream, without ever re-gating it.
    mig_spec.write_text(_spec("e2.example"), encoding="utf-8")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"a stale clearance for a swapped source must not verify OK:\n{out}"
    assert "CANNOT ASSESS" in out


def test_negative_control_an_unswapped_source_still_verifies_clean(tmp_path: Path) -> None:
    """Negative control for requirement 3: an UNCHANGED source key must still verify clean."""
    sys.path.insert(0, str(REPO / "scripts"))
    import preflight_source_credentials as pf  # noqa: PLC0415

    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(
        json.dumps(
            {"data_sources": [{"connection": {"class": "sqlserver", "server": "e1.example", "database": "db"}}]}
        ),
        encoding="utf-8",
    )

    key_e1 = pf._leg_key({}, 0, {"class": "sqlserver", "server": "e1.example", "database": "db"})
    run_gate("block", str(mig), "--sources", key_e1)
    run_gate("clear", str(mig), "--reason", "probe returned a row", "--earned")
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"an unchanged, correctly-cleared source must still verify clean:\n{out}"


# --- Requirement 1 (2nd bounded round, comment 5546182629): mixed-scope audit poisoning --------
#
# The single-entry cases above (a wholly unscoped copy, a wholly foreign-scope copy) already
# return None from `_audit_entries` because EVERY entry fails the scope check, so the trail is
# empty either way. The gap this closes is different: an otherwise-genuine, fully-scoped history
# with just ONE extra unscoped/foreign-scope line mixed in used to keep trusting the rest of it
# (the earlier "drop just this entry" implementation). That must now poison the WHOLE trail too.


def test_a_mixed_trail_with_one_unscoped_entry_CANNOT_certify_this_directory(tmp_path: Path) -> None:
    """Requirement 1: one entry with NO `scope` key mixed into an otherwise-genuine trail poisons it all.

    A real, earned clearance is written first (fully scoped, by the real CLI), then a single
    hand-appended line with no `scope` field is appended after it. Every OTHER line is genuine and
    correctly scoped - only ONE line is bad. Dropping just that line and trusting the rest would
    still report OK; the whole trail must be untrustworthy instead.
    """
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"connection": {"class": "sqlserver", "server": "host1", "database": "db"}}]}),
        encoding="utf-8",
    )
    sys.path.insert(0, str(REPO / "scripts"))
    import preflight_source_credentials as pf  # noqa: PLC0415

    key = pf._leg_key({}, 0, {"class": "sqlserver", "server": "host1", "database": "db"})
    run_gate("block", str(mig), "--sources", key)
    run_gate("clear", str(mig), "--reason", "probe returned a row", "--earned")
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")
    audit = mig / ".credential-gate-audit.log"
    unscoped = json.dumps({"ts": "2020-01-01T00:00:00+00:00", "action": "probe-cleared", "detail": "no scope here"})
    audit.write_text(audit.read_text(encoding="utf-8") + unscoped + "\n", encoding="utf-8")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"a mixed genuine+unscoped trail must not certify this directory:\n{out}"
    assert "CANNOT ASSESS" in out


def test_a_mixed_trail_with_one_foreign_scope_entry_CANNOT_certify_this_directory(tmp_path: Path) -> None:
    """Requirement 1: one entry naming a DIFFERENT scope mixed into a real trail poisons it all."""
    mig = tmp_path / "mig"
    other = tmp_path / "unrelated-other-scope"
    other.mkdir(parents=True)
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"connection": {"class": "sqlserver", "server": "host1", "database": "db"}}]}),
        encoding="utf-8",
    )
    sys.path.insert(0, str(REPO / "scripts"))
    import preflight_source_credentials as pf  # noqa: PLC0415

    key = pf._leg_key({}, 0, {"class": "sqlserver", "server": "host1", "database": "db"})
    run_gate("block", str(mig), "--sources", key)
    run_gate("clear", str(mig), "--reason", "probe returned a row", "--earned")
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")
    audit = mig / ".credential-gate-audit.log"
    foreign = json.dumps(
        {"ts": "2020-01-01T00:00:00+00:00", "action": "probe-cleared", "detail": "wrong scope", "scope": str(other)}
    )
    audit.write_text(audit.read_text(encoding="utf-8") + foreign + "\n", encoding="utf-8")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"a mixed genuine+foreign-scope trail must not certify this directory:\n{out}"
    assert "CANNOT ASSESS" in out


def test_negative_control_a_fully_scoped_mixed_action_trail_still_verifies_ok(tmp_path: Path) -> None:
    """Negative control for requirement 1: a trail where EVERY entry matches scope still passes."""
    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"connection": {"class": "sqlserver", "server": "host1", "database": "db"}}]}),
        encoding="utf-8",
    )
    sys.path.insert(0, str(REPO / "scripts"))
    import preflight_source_credentials as pf  # noqa: PLC0415

    key = pf._leg_key({}, 0, {"class": "sqlserver", "server": "host1", "database": "db"})
    run_gate("block", str(mig), "--sources", key)
    run_gate("clear", str(mig), "--reason", "probe returned a row", "--earned")
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"a fully, consistently scoped trail must still verify clean:\n{out}"


# --- Requirement 2 (2nd bounded round, comment 5546182629): multi-source earned state -----------
#
# `_source_set_mismatch_reason` used to compare against only `_last_block_sources` - the MOST
# RECENT `block`'s source list. A migration with two independently-blocked-and-cleared live
# sources (E1 earned first, E2 blocked and earned later) would have its earlier source E1 silently
# drop out of that "most recent" list, producing a FALSE state-3 against a fully, correctly earned
# migration. It must instead compare against the complete accumulated earned set.


def test_two_independently_earned_sources_both_verify_clean_together(tmp_path: Path) -> None:
    """Requirement 2: E1 earned, then E2 independently earned later, both still verify 0.

    Regression guard: comparing only the MOST RECENT block's source list (instead of the full
    accumulated earned set) would report E1 as newly "uncovered" the moment E2 alone gets
    re-blocked and cleared, even though E1 was never touched, dropped, or unproven.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import preflight_source_credentials as pf  # noqa: PLC0415

    def _spec() -> str:
        return json.dumps(
            {
                "data_sources": [
                    {"connection": {"class": "sqlserver", "server": "e1.example", "database": "db"}},
                    {"connection": {"class": "sqlserver", "server": "e2.example", "database": "db"}},
                ]
            }
        )

    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    (mig / "migration-spec.json").write_text(_spec(), encoding="utf-8")

    key_e1 = pf._leg_key({}, 0, {"class": "sqlserver", "server": "e1.example", "database": "db"})
    key_e2 = pf._leg_key({}, 1, {"class": "sqlserver", "server": "e2.example", "database": "db"})

    run_gate("block", str(mig), "--sources", key_e1)
    run_gate("clear", str(mig), "--reason", "E1 probe returned a row", "--earned")
    # E2 is blocked and earned INDEPENDENTLY, later, and its own `block` names only E2 - so a
    # "most recent block" comparison would see only E2 here, not E1+E2.
    run_gate("block", str(mig), "--sources", key_e2)
    run_gate("clear", str(mig), "--reason", "E2 probe returned a row", "--earned")
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"two independently earned sources must both verify clean:\n{out}"


def test_swapping_either_of_two_earned_sources_for_an_unearned_one_fails(tmp_path: Path) -> None:
    """Requirement 2: replacing EITHER earned source with an unearned E3 must verify 3."""
    sys.path.insert(0, str(REPO / "scripts"))
    import preflight_source_credentials as pf  # noqa: PLC0415

    def _spec(second_server: str) -> str:
        return json.dumps(
            {
                "data_sources": [
                    {"connection": {"class": "sqlserver", "server": "e1.example", "database": "db"}},
                    {"connection": {"class": "sqlserver", "server": second_server, "database": "db"}},
                ]
            }
        )

    mig = tmp_path / "mig"
    (mig / "fabric").mkdir(parents=True)
    mig_spec = mig / "migration-spec.json"
    mig_spec.write_text(_spec("e2.example"), encoding="utf-8")

    key_e1 = pf._leg_key({}, 0, {"class": "sqlserver", "server": "e1.example", "database": "db"})
    key_e2 = pf._leg_key({}, 1, {"class": "sqlserver", "server": "e2.example", "database": "db"})

    run_gate("block", str(mig), "--sources", key_e1)
    run_gate("clear", str(mig), "--reason", "E1 probe returned a row", "--earned")
    run_gate("block", str(mig), "--sources", key_e2)
    run_gate("clear", str(mig), "--reason", "E2 probe returned a row", "--earned")
    (mig / "fabric" / "model.tmdl").write_text("table Shipment")

    # Swap E2 for an unearned E3, without ever re-gating - E1 is untouched and still earned.
    mig_spec.write_text(_spec("e3.example"), encoding="utf-8")

    proc = run_gate("verify", str(mig))
    out = proc.stdout + proc.stderr

    assert proc.returncode == 3, f"swapping an earned source for an unearned one must not verify OK:\n{out}"
    assert "CANNOT ASSESS" in out


# --- `list`: the multi-unit query (#344) ------------------------------------------------------
#
# Added after a field report from a ~44-unit estate: "I am always asked to run these for all the
# dashboards manually". Every other subcommand takes exactly one migration, so answering "what is
# still gated?" cost one invocation per unit -- and an agent had no way at all to discover what
# became retryable after a human signed in.


def _unit(root: Path, name: str, *, marker: bool = False, override: bool = False, audit: tuple[str, ...] = ()) -> Path:
    """Build one unit on disk in a given gate state. Artifacts only -- never prose."""
    d = root / name
    d.mkdir(parents=True)
    if marker:
        (d / ".credential-gate-BLOCKED.json").write_text("{}", encoding="utf-8")
    if override:
        (d / ".credential-gate-AUTHORIZED").write_text("x", encoding="utf-8")
    for action in audit:
        _trail(d, (action, [] if action in cg.BLOCK_ACTIONS else None))
    return d


def _list(root: Path) -> tuple[int, dict[str, str]]:
    """Run `list --json` and return (exit code, {relative unit -> state})."""
    r = subprocess.run(
        [sys.executable, str(GATE), "list", str(root), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    units = json.loads(r.stdout)["units"]
    return r.returncode, {u["relative"]: u["state"] for u in units}


def test_list_classifies_every_gate_state(tmp_path: Path) -> None:
    """Each state is derived from artifacts on disk, not from what anything claims."""
    _unit(tmp_path, "blocked", marker=True, audit=("block",))
    _unit(tmp_path, "earned", audit=("block", "probe-cleared"))
    _unit(tmp_path, "unearned", override=True, audit=("block", "authorize"))
    _unit(tmp_path, "clean", audit=("block",))

    _, states = _list(tmp_path)
    assert states["blocked"] == "BLOCKED"
    assert states["earned"] == "cleared-earned"
    assert states["unearned"] == "authorized-unearned"
    assert states["clean"] == "clean"


def test_list_reports_an_override_with_no_authorize_entry_as_forged(tmp_path: Path) -> None:
    """The file alone authorizes nothing -- agents demonstrably create it themselves.

    Without the audit entry this must NOT read as `authorized-unearned`, which is the benign state
    it would otherwise be indistinguishable from.
    """
    _unit(tmp_path, "forged", override=True, audit=("block",))
    code, states = _list(tmp_path)
    assert states["forged"] == "FORGED-OVERRIDE"
    assert code == 3


def test_list_ranks_the_security_signal_above_the_workflow_signal(tmp_path: Path) -> None:
    """A forged override anywhere outranks "something is still blocked".

    Both conditions are true here. Reporting 1 would let a bypass attempt hide behind ordinary
    workflow state, which is exactly the case a sweep exists to surface.
    """
    _unit(tmp_path, "blocked", marker=True, audit=("block",))
    _unit(tmp_path, "forged", override=True, audit=("block",))
    assert _list(tmp_path)[0] == 3


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"marker": True, "audit": ("block",)}, 1),
        ({"audit": ("block", "probe-cleared")}, 0),
        ({"override": True, "audit": ("block", "authorize")}, 0),
    ],
)
def test_list_exit_code_is_scriptable(tmp_path: Path, kwargs: dict, expected: int) -> None:
    """1 only while something is genuinely blocked -- this is an agent's retry signal."""
    _unit(tmp_path, "u", **kwargs)
    assert _list(tmp_path)[0] == expected


def test_list_on_an_estate_with_nothing_gated_is_clean_and_zero(tmp_path: Path) -> None:
    """An extract-only estate was never gated; that must not look like a problem."""
    (tmp_path / "u").mkdir()
    code, states = _list(tmp_path)
    assert code == 0
    assert states == {}


def test_list_steers_the_human_toward_the_EARNED_route(tmp_path: Path) -> None:
    """The field report's root cause: `authorize` was reached for units a probe could have earned.

    Both routes must be named, and the earned one must come first -- a credential caches
    machine-wide, so one sign-in can legitimately clear several units.
    """
    _unit(tmp_path, "blocked", marker=True, audit=("block",))
    r = subprocess.run(
        [sys.executable, str(GATE), "list", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    out = r.stderr
    assert "EARNED" in out and "UNEARNED" in out
    assert out.index("EARNED   -") < out.index("UNEARNED -")
    assert "UNVALIDATED" in out


def test_list_does_not_raise_the_forgery_code_for_a_bad_root(tmp_path: Path) -> None:
    """A mistyped estate root must NOT exit 2 -- that code is the forged-override alarm.

    Blind review 2026-08-27 measured all four of these returning 2, identical to a real bypass
    attempt, while the docs sold 2 as meaning forgery and only forgery. No `list` test had ever
    passed an invalid root, which is exactly why it survived.
    """
    missing = tmp_path / "nope"
    not_a_dir = tmp_path / "f.txt"
    not_a_dir.write_text("x", encoding="utf-8")

    for bad in (missing, not_a_dir):
        r = subprocess.run(
            [sys.executable, str(GATE), "list", str(bad)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert r.returncode == 4, f"{bad} returned {r.returncode}, not the bad-root code 4"


def test_bad_target_still_exits_2_for_every_OTHER_subcommand(tmp_path: Path) -> None:
    """Only `list` was renumbered. Renumbering the rest would be an unrelated breaking change."""
    missing = str(tmp_path / "nope")
    for cmd in (["status", missing], ["verify", missing]):
        r = subprocess.run([sys.executable, str(GATE), *cmd], capture_output=True, text=True, check=False)
        assert r.returncode == 2, f"{cmd[0]} returned {r.returncode}, expected unchanged 2"


def test_a_forged_override_is_confirmable_from_json_not_the_exit_code(tmp_path: Path) -> None:
    """argparse usage errors also exit 2 and are not ours to renumber.

    So the exit code cannot self-certify a forgery; the `--json` state field is what a scripted
    consumer must key on. This pins that the machine-readable channel says it unambiguously.

    ⚠️ Half of this test guards an EXTERNAL invariant. The forgery half is source-falsifiable
    (mutating `_unit_state`'s `FORGED-OVERRIDE` breaks it), but the usage-error half pins
    **argparse's** contract, which no mutation inside this repo can break. It is a valid regression
    guard for the documented "exit 2 with no JSON on stdout = usage error" discriminator -- just do
    not read its passing as evidence that our own code is covered.
    """
    _unit(tmp_path, "forged", override=True, audit=("block",))
    usage = subprocess.run(
        [sys.executable, str(GATE), "list", str(tmp_path), "--bogus"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert usage.returncode == 2, "argparse owns 2; the security signal moved to 3 so they cannot collide"
    assert not usage.stdout.strip(), "a usage error must emit no JSON, so consumers can tell them apart"

    real = subprocess.run(
        [sys.executable, str(GATE), "list", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert real.returncode == 3
    states = {u["state"] for u in json.loads(real.stdout)["units"]}
    assert "FORGED-OVERRIDE" in states


# ---------------------------------------------------------------------------------------------
# Phase-1 package-local data-access authority (#562)
#
# `assess_data_access` is the first thing in this module that must be PURE: every other entry point
# here is allowed to write - `apply_block` writes a marker, `clear_block` writes an audit line, and
# `verify` appends `violation` lines as it judges. A projection built by calling `verify` would
# therefore change the evidence it is derived from, so the first test below is the one that makes
# the rest meaningful, and it carries its own positive control proving the check can fail.
# ---------------------------------------------------------------------------------------------

LIVE_A = {"class": "sqlserver", "server": "a.example", "database": "db", "powerbi_target": "live_source"}
LIVE_B = {
    "class": "snowflake",
    "server": "b.example",
    "database": "db",
    "warehouse": "wh",
    "powerbi_target": "live_source",
}
FLAT = {"class": "excel-direct", "powerbi_target": "flat_file"}
REVIEW = {"class": "unknown", "server": "u.example", "powerbi_target": "unknown"}
# Same unclassifiable leg, minus any hashable endpoint identity. `_classify_legs` converts a leg it
# cannot key into `needs-credential` with an `unstable-source[...]` placeholder - fail-closed, and
# deliberately NOT the same answer as `REVIEW` above: a key that cannot be derived can never be
# matched against audit evidence, so it is an authority failure rather than a data finding.
REVIEW_UNSTABLE = {"class": "unknown", "powerbi_target": "unknown"}

KEY_A = pf._leg_key({}, 0, LIVE_A)
KEY_B = pf._leg_key({}, 0, LIVE_B)

COMPLETE_LOCAL = {
    "self_contained": True,
    "omissions": [],
    "neutralized": [],
    "retained_network": [],
    "binding": {"state": "unbound"},
}


def _da_spec(*connections: dict) -> dict:
    """A package-local `migration-spec.json` payload declaring exactly these connection legs."""
    return {
        "data_sources": [
            {"name": f"ds{index}", "connection": dict(conn), "tables": [], "fields": []}
            for index, conn in enumerate(connections)
        ]
    }


def _da_root(tmp_path: Path, name: str, *connections: dict) -> Path:
    """A gate root that is a legitimate scope target and declares these live/flat legs itself."""
    root = tmp_path / name
    (root / "fabric").mkdir(parents=True, exist_ok=True)
    (root / "migration-spec.json").write_text(json.dumps(_da_spec(*connections)), encoding="utf-8")
    return root


def _trail(root: Path, *entries: tuple) -> None:
    """Append `(action, sources_or_None)` with the production action's canonical detail shape."""
    for action, sources in entries:
        detail = (
            "by=test; chain=['python.exe', 'pwsh.exe', 'WindowsTerminal.exe']"
            if action == "authorize"
            else f"{action} fixture"
        )
        if action in cg.BLOCK_ACTIONS and sources is not None:
            detail = "sources_json=" + json.dumps(sources)
        _append_audit(root, action, detail, sources)


def _assess(root: Path, spec: dict, local: dict | None = None, **kwargs) -> object:
    return cg.assess_data_access(
        root,
        package_spec=spec,
        package_data_sources=COMPLETE_LOCAL if local is None else local,
        fallback_authorization=kwargs.pop("policy", "stop"),
        requested_scope=kwargs.pop("scope", "model_and_report"),
        provider=kwargs.pop("provider", None),
    )


def _tree_snapshot(root: Path) -> list[tuple[str, int, bytes]]:
    """Every file beneath `root` with its size and bytes - enough to catch any write at all."""
    return sorted(
        (str(path.relative_to(root)), path.stat().st_size, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    )


def test_the_assessor_never_writes_a_byte_while_verify_demonstrably_does(tmp_path: Path) -> None:
    """The purity invariant, with the positive control that stops it being vacuous.

    A test that only asserts "nothing changed" passes just as happily against a function that was
    never called. So the same tree is put through `verify()` afterwards: `verify` MUST append a
    `violation` line for an unearned clear, and if it does not, the snapshot comparison above
    proves nothing about the assessor either.
    """
    root = _da_root(tmp_path, "pure", LIVE_A)
    _trail(root, ("block", [KEY_A]), ("probe-no_credential", [KEY_A]), ("manual-clear", None))
    (root / "fabric" / "Model.tmdl").write_text("table T\n", encoding="utf-8")
    before = _tree_snapshot(root)

    for scope in ("model_and_report", "model_only"):
        for policy in ("stop", "model_only_unvalidated"):
            _assess(root, _da_spec(LIVE_A), scope=scope, policy=policy)
    _assess(root, _da_spec(FLAT))
    _assess(root, _da_spec(REVIEW))

    assert _tree_snapshot(root) == before, "assess_data_access must not write, rename or truncate anything"

    assert cg.verify(root) == 1, "control: this tree IS an unearned clear, so verify must fail it"
    assert _tree_snapshot(root) != before, (
        "control failed: verify() did not append its violation line, so the snapshot comparison "
        "above cannot distinguish a pure assessor from an uncalled one"
    )


def test_an_all_flat_package_with_complete_bytes_is_local_import_ready(tmp_path: Path) -> None:
    """No live leg, complete packaged bytes, and deliberately NO audit log at all.

    An extract-only unit is legitimately never gated (`_gate_was_ever_applied`), so demanding audit
    evidence here would block every flat-file package. `binding.state=unbound` is Phase-2 work and
    must not read as a dependency failure.
    """
    root = _da_root(tmp_path, "flat", FLAT, FLAT)
    assert not (root / cg.AUDIT).exists(), "fixture must have no audit trail, or it proves nothing"

    result = _assess(root, _da_spec(FLAT, FLAT))

    assert (result.state, result.codes) == ("local_import_ready", ("all-flat-file", "package-self-contained"))
    assert result.source_keys == ()
    assert (result.validation, result.max_phase2_claim) == ("validated", "data_validated")
    assert result.effective_scope == "model_and_report"


def test_one_live_key_earned_by_a_keyed_probe_then_clear_is_live_data_ok(tmp_path: Path) -> None:
    """The only shape that earns `live_data_ok`: keyed measurement, then keyed clear, same epoch."""
    root = _da_root(tmp_path, "one-live", LIVE_A)
    _trail(root, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.codes) == ("live_data_ok", ("probe-cleared", "probe-data-ok"))
    assert result.source_keys == (KEY_A,)
    assert (result.validation, result.max_phase2_claim) == ("validated", "data_validated")


def test_two_live_keys_earned_in_separate_epochs_are_both_retained(tmp_path: Path) -> None:
    """Re-arming B must not erase A: independent endpoints are independent reachability facts."""
    root = _da_root(tmp_path, "two-live", LIVE_A, LIVE_B)
    _trail(
        root,
        ("block", [KEY_A]),
        ("probe-data_ok", [KEY_A]),
        ("probe-cleared", [KEY_A]),
        ("block", [KEY_B]),
        ("probe-data_ok", [KEY_B]),
        ("probe-cleared", [KEY_B]),
    )

    result = _assess(root, _da_spec(LIVE_A, LIVE_B))

    assert result.state == "live_data_ok"
    assert result.source_keys == tuple(sorted((KEY_A, KEY_B)))


def test_the_latest_arm_invalidates_only_its_own_keys_proof(tmp_path: Path) -> None:
    """Per-key epochs, proved in both directions from ONE trail.

    The same audit log must say `blocked/marker-only` for the re-armed key and `live_data_ok` for
    the untouched one. A global "cleared" bit cannot produce both answers, so this is the test that
    distinguishes a per-key ledger from one.
    """
    root = _da_root(tmp_path, "rearm", LIVE_A, LIVE_B)
    _trail(
        root,
        ("block", [KEY_A, KEY_B]),
        ("probe-data_ok", [KEY_A]),
        ("probe-cleared", [KEY_A]),
        ("probe-data_ok", [KEY_B]),
        ("probe-cleared", [KEY_B]),
        ("block", [KEY_B]),
    )

    both = _assess(root, _da_spec(LIVE_A, LIVE_B))
    only_a = _assess(root, _da_spec(LIVE_A))

    assert (both.state, both.codes) == ("blocked", ("marker-only",))
    assert (only_a.state, only_a.source_keys) == ("live_data_ok", (KEY_A,))


def test_a_mixed_flat_and_live_package_needs_every_live_key_and_complete_bytes(tmp_path: Path) -> None:
    """A live leg does not excuse the flat half, and the flat half does not excuse the live one."""
    root = _da_root(tmp_path, "mixed", FLAT, LIVE_A, LIVE_B)
    _trail(
        root,
        ("block", [KEY_A, KEY_B]),
        ("probe-data_ok", [KEY_A]),
        ("probe-data_ok", [KEY_B]),
        ("probe-cleared", [KEY_A, KEY_B]),
    )
    spec = _da_spec(FLAT, LIVE_A, LIVE_B)

    earned = _assess(root, spec)
    leaky = _assess(root, spec, {**COMPLETE_LOCAL, "retained_network": ["\\\\share\\rows.csv"]})

    assert (earned.state, earned.source_keys) == ("live_data_ok", tuple(sorted((KEY_A, KEY_B))))
    assert (leaky.state, leaky.codes) == ("blocked", ("local-import-incomplete",))


def test_an_unkeyed_probe_success_cannot_earn_a_clear(tmp_path: Path) -> None:
    """The producer gap #562 closes, pinned from the READER side.

    Before `_record_attempt` stamped source keys, a two-key bundle could hold a `probe-data_ok` and
    a `probe-cleared` with nothing tying either to an endpoint. The positive control is the same
    trail with the key present, so this cannot pass by refusing everything.
    """
    root = _da_root(tmp_path, "unkeyed", LIVE_A)
    _trail(root, ("block", [KEY_A]), ("probe-data_ok", None), ("probe-cleared", [KEY_A]))
    unkeyed = _assess(root, _da_spec(LIVE_A))

    keyed_root = _da_root(tmp_path, "keyed", LIVE_A)
    _trail(keyed_root, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))
    keyed = _assess(keyed_root, _da_spec(LIVE_A))

    assert (unkeyed.state, unkeyed.codes) == ("blocked", ("stale-clear",))
    assert keyed.state == "live_data_ok"


def test_a_real_bare_earned_clear_through_the_gate_cli_stays_blocked(tmp_path: Path) -> None:
    """End to end through the PRODUCTION arm/clear path, not synthetic audit lines.

    `clear_block(..., earned=True)` writes a `probe-cleared` naming the last block's sources even
    though nothing was ever measured. That entry is real, well-formed, keyed and trusted - and it
    still must not produce `live_data_ok`.
    """
    root = _da_root(tmp_path, "cli-clear", LIVE_A)
    cg.apply_block(root, [KEY_A])
    assert (root / cg.MARKER).exists(), "fixture must start armed"
    assert cg.clear_block(root, "operator said it is fine", earned=True) == 0
    assert "probe-cleared" in _direct_actions(root), "fixture must produce the real earned-clear record"

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.codes) == ("blocked", ("stale-clear",))


def test_a_later_non_success_probe_invalidates_an_already_earned_key(tmp_path: Path) -> None:
    """Proof is not permanent: the LATEST measurement decides, including when it goes backwards."""
    root = _da_root(tmp_path, "regressed", LIVE_A)
    _trail(
        root,
        ("block", [KEY_A]),
        ("probe-data_ok", [KEY_A]),
        ("probe-cleared", [KEY_A]),
        ("probe-no_credential", [KEY_A]),
    )

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.codes) == ("blocked", ("probe-no-credential",))


@pytest.mark.parametrize(
    ("entries", "expected_code"),
    [
        pytest.param((("block", [KEY_A]),), "marker-only", id="marker-only"),
        pytest.param((("block", [KEY_A]), ("manual-clear", None)), "manual-clear", id="manual-clear"),
        pytest.param(
            (("block", [KEY_A]), ("probe-credential_present", [KEY_A])),
            "credential-present-only",
            id="credential-present",
        ),
        pytest.param((("block", [KEY_A]), ("probe-skipped", [KEY_A])), "live-probe-skipped", id="skipped"),
        pytest.param(
            (("block", [KEY_A]), ("probe-operator_required", [KEY_A])),
            "probe-operator-required",
            id="operator-required",
        ),
        pytest.param((("block", [KEY_A]), ("probe-no_credential", [KEY_A])), "probe-no-credential", id="no-credential"),
        pytest.param((("block", [KEY_A]), ("probe-access_denied", [KEY_A])), "probe-access-denied", id="access-denied"),
        pytest.param((("block", [KEY_A]), ("probe-unreachable", [KEY_A])), "probe-unreachable", id="unreachable"),
        pytest.param((("block", [KEY_A]), ("probe-bad_table", [KEY_A])), "probe-bad-table", id="bad-table"),
        pytest.param((("block", [KEY_A]), ("probe-error", [KEY_A])), "probe-error", id="error"),
    ],
)
def test_each_unearned_shape_names_its_own_blocking_code(tmp_path: Path, entries: tuple, expected_code: str) -> None:
    """Every refusal must be distinguishable. A single fail-closed code would pass all of these."""
    root = _da_root(tmp_path, f"blocked-{expected_code}", LIVE_A)
    _trail(root, *entries)

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.codes) == ("blocked", (expected_code,))


@pytest.mark.parametrize(
    ("poison", "expected_code"),
    [
        pytest.param("missing", "audit-missing", id="missing"),
        pytest.param("empty", "audit-malformed", id="empty"),
        pytest.param("malformed", "audit-malformed", id="malformed-line"),
        pytest.param("foreign", "audit-foreign-scope", id="foreign-scope"),
        pytest.param("unscoped", "audit-foreign-scope", id="unscoped"),
    ],
)
def test_an_untrusted_audit_cannot_establish_live_access(tmp_path: Path, poison: str, expected_code: str) -> None:
    """An untrusted trail is `cannot_establish`, never `blocked` - and never a silent pass.

    Each poison is mixed into an OTHERWISE COMPLETE, genuinely earned history, so the refusal comes
    from the poison rather than from missing evidence.
    """
    root = _da_root(tmp_path, f"poison-{poison}", LIVE_A)
    if poison != "missing":
        _trail(root, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))
    audit = root / cg.AUDIT
    if poison == "empty":
        audit.write_text("", encoding="utf-8")
    elif poison == "malformed":
        audit.write_text(audit.read_text(encoding="utf-8") + "{ this is not json\n", encoding="utf-8")
    elif poison == "foreign":
        foreign = json.dumps({"ts": "x", "action": "block", "detail": "d", "scope": str(tmp_path / "elsewhere")})
        audit.write_text(audit.read_text(encoding="utf-8") + foreign + "\n", encoding="utf-8")
    elif poison == "unscoped":
        audit.write_text(audit.read_text(encoding="utf-8") + json.dumps({"action": "block"}) + "\n", encoding="utf-8")

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.codes) == ("cannot_establish", (expected_code,))
    assert result.source_keys == (), "an untrusted authority must not still publish its keys"


def test_an_untrusted_audit_does_not_block_a_package_with_no_live_leg(tmp_path: Path) -> None:
    """Negative control for the poison suite: the audit is only load-bearing when something needs it."""
    root = _da_root(tmp_path, "poison-irrelevant", FLAT)
    (root / cg.AUDIT).write_text("{ not json at all\n", encoding="utf-8")

    assert _assess(root, _da_spec(FLAT)).state == "local_import_ready"


def test_a_force_scoped_arm_is_never_portable_one_unit_authority(tmp_path: Path) -> None:
    """`--force-scope` gates a whole subtree, so its evidence cannot certify one package."""
    root = _da_root(tmp_path, "forced", LIVE_A)
    _trail(
        root,
        ("block-forced-scope", None),
        ("block", [KEY_A]),
        ("probe-data_ok", [KEY_A]),
        ("probe-cleared", [KEY_A]),
    )

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.codes) == ("cannot_establish", ("forced-scope",))


def _authorized_root(tmp_path: Path, name: str, *, override: bool = True, authorize_first: bool = False) -> Path:
    root = _da_root(tmp_path, name, LIVE_A)
    if authorize_first:
        _trail(root, ("authorize", None), ("block", [KEY_A]))
    else:
        _trail(root, ("block", [KEY_A]), ("authorize", None), ("probe-cleared", [KEY_A]))
    if override:
        (root / cg.OVERRIDE).write_text("authorized by a human\n", encoding="utf-8")
    return root


def test_an_authentic_authorization_at_model_only_scope_is_the_limited_accepted_state(tmp_path: Path) -> None:
    """Authorized is ACCEPTED but never validated, and its ceiling drops to structural-only."""
    root = _authorized_root(tmp_path, "authorized")

    result = _assess(root, _da_spec(LIVE_A), policy="model_only_unvalidated", scope="model_only")

    assert (result.state, result.codes) == ("authorized_model_only", ("brief-model-only", "human-authorize"))
    assert (result.validation, result.max_phase2_claim) == ("unvalidated", "structural_only")
    assert (result.effective_scope, result.source_keys) == ("model_only", (KEY_A,))


@pytest.mark.parametrize(
    ("kwargs", "root_kwargs", "why"),
    [
        pytest.param({"policy": "stop", "scope": "model_only"}, {}, "brief says stop", id="brief-says-stop"),
        pytest.param(
            {"policy": "model_only_unvalidated", "scope": "model_and_report"},
            {},
            "requested scope is not model-only",
            id="scope-not-model-only",
        ),
        pytest.param(
            {"policy": "model_only_unvalidated", "scope": "model_only"},
            {"override": False},
            "no override file, so nothing authentic to honour",
            id="no-override-file",
        ),
        pytest.param(
            {"policy": "model_only_unvalidated", "scope": "model_only"},
            {"authorize_first": True},
            "the authorization predates the current arm",
            id="authorize-before-arm",
        ),
    ],
)
def test_a_partial_authorization_is_a_named_mismatch_not_a_quiet_pass(
    tmp_path: Path, kwargs: dict, root_kwargs: dict, why: str
) -> None:
    """All three legs must agree. Any partial combination blocks and SAYS it was a mismatch."""
    root = _authorized_root(tmp_path, f"mismatch-{len(why)}-{kwargs['scope']}-{kwargs['policy']}", **root_kwargs)

    result = _assess(root, _da_spec(LIVE_A), **kwargs)

    assert result.state == "blocked", why
    assert "authorization-mismatch" in result.codes, why


def test_a_brief_that_asks_for_the_fallback_without_any_authorization_is_a_mismatch(tmp_path: Path) -> None:
    """The other direction: policy wants the fallback, but no human ever signed anything."""
    root = _da_root(tmp_path, "wants-fallback", LIVE_A)
    _trail(root, ("block", [KEY_A]))

    result = _assess(root, _da_spec(LIVE_A), policy="model_only_unvalidated", scope="model_only")

    assert result.state == "blocked"
    assert set(result.codes) == {"marker-only", "authorization-mismatch"}


def test_a_forged_override_with_no_authorize_entry_authorizes_nothing(tmp_path: Path) -> None:
    """The file alone has never authorized anything, and this authority does not change that."""
    root = _da_root(tmp_path, "forged-override", LIVE_A)
    _trail(root, ("block", [KEY_A]))
    (root / cg.OVERRIDE).write_text("I wrote this myself\n", encoding="utf-8")

    result = _assess(root, _da_spec(LIVE_A), policy="model_only_unvalidated", scope="model_only")

    assert result.state == "blocked"
    assert "authorization-mismatch" in result.codes


@pytest.mark.parametrize(
    ("root_legs", "package_legs", "expected"),
    [
        pytest.param((LIVE_A,), (LIVE_A, LIVE_B), "source-key-set-changed", id="key-added"),
        pytest.param((LIVE_A,), (LIVE_B,), "source-key-set-changed", id="key-swapped"),
        pytest.param((LIVE_A, LIVE_A), (LIVE_A, LIVE_A), "source-key-invalid", id="key-duplicated"),
    ],
)
def test_a_package_key_the_gate_root_never_covered_cannot_be_established(
    tmp_path: Path, root_legs: tuple, package_legs: tuple, expected: str
) -> None:
    """Root evidence is bound to package evidence by KEY, in both directions."""
    root = _da_root(tmp_path, f"keys-{expected}-{len(package_legs)}", *root_legs)
    _trail(root, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))

    result = _assess(root, _da_spec(*package_legs))

    assert (result.state, result.codes) == ("cannot_establish", (expected,))


def test_a_package_carrying_fewer_keys_than_the_gate_root_still_earns_its_own(tmp_path: Path) -> None:
    """Negative control for the key-set rules: an estate root legitimately gates more than one unit."""
    root = _da_root(tmp_path, "root-superset", LIVE_A, LIVE_B)
    _trail(
        root,
        ("block", [KEY_A, KEY_B]),
        ("probe-data_ok", [KEY_A]),
        ("probe-data_ok", [KEY_B]),
        ("probe-cleared", [KEY_A, KEY_B]),
    )

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.source_keys) == ("live_data_ok", (KEY_A,)), "only this package's own key ships"


def test_an_unknown_or_review_leg_blocks_and_is_never_dropped_from_the_denominator(tmp_path: Path) -> None:
    """A leg nobody can classify is not a flat file, and an earned sibling does not cover it."""
    root = _da_root(tmp_path, "review", LIVE_A, REVIEW)
    _trail(root, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))

    with_review = _assess(root, _da_spec(LIVE_A, REVIEW))
    without_review = _assess(root, _da_spec(LIVE_A))

    assert (with_review.state, with_review.codes) == ("blocked", ("unknown-target",))
    assert without_review.state == "live_data_ok", "control: the review leg is what blocked, nothing else"


def test_an_unclassifiable_leg_with_no_stable_identity_is_an_authority_failure(tmp_path: Path) -> None:
    """Contradicts the tidy "review blocks" reading, and the canonical classifier is right.

    `_classify_legs` promotes a leg it cannot KEY to `needs-credential` with an
    `unstable-source[...]` placeholder rather than leaving it as `review`. That placeholder can
    never be matched against audit evidence, so the honest answer is `cannot_establish`, not a
    `blocked` finding about data. Pinned here because it is a real behaviour of the shared
    classifier, not of this assessor, and a future "simplification" would silently downgrade it.
    """
    root = _da_root(tmp_path, "unstable-review", LIVE_A)

    result = _assess(root, _da_spec(REVIEW_UNSTABLE))

    assert (result.state, result.codes) == ("cannot_establish", ("source-key-invalid",))


@pytest.mark.parametrize(
    "local",
    [
        pytest.param({**COMPLETE_LOCAL, "self_contained": False}, id="not-self-contained"),
        pytest.param({**COMPLETE_LOCAL, "omissions": [{"reason": "unshippable"}]}, id="omission"),
        pytest.param({**COMPLETE_LOCAL, "neutralized": ["Sales.xlsx"]}, id="neutralized"),
        pytest.param({**COMPLETE_LOCAL, "retained_network": ["rows.csv"]}, id="retained-network"),
        pytest.param({"self_contained": True}, id="fields-absent"),
    ],
)
def test_incomplete_packaged_bytes_block_a_flat_package(tmp_path: Path, local: dict) -> None:
    """Local access is a claim about BYTES THAT SHIPPED; a missing field is incomplete, not silent."""
    root = _da_root(tmp_path, f"incomplete-{len(local)}-{local.get('self_contained')}", FLAT)

    result = _assess(root, _da_spec(FLAT), local)

    assert (result.state, result.codes) == ("blocked", ("local-import-incomplete",))


def test_an_unreadable_localization_record_is_cannot_establish_not_blocked(tmp_path: Path) -> None:
    """A record that is not even a mapping is an authority failure, not a data finding."""
    root = _da_root(tmp_path, "bad-local-record", FLAT)

    result = _assess(root, _da_spec(FLAT), ["not", "a", "mapping"])

    assert (result.state, result.codes) == ("cannot_establish", ("spec-unreadable",))


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param("not a mapping", id="spec-not-a-mapping"),
        pytest.param({"data_sources": "nope"}, id="data-sources-not-a-list"),
        pytest.param({"data_sources": ["nope"]}, id="source-not-a-mapping"),
    ],
)
def test_an_unreadable_package_spec_is_cannot_establish(tmp_path: Path, spec: object) -> None:
    root = _da_root(tmp_path, "bad-spec", FLAT)

    result = _assess(root, spec)

    assert (result.state, result.codes) == ("cannot_establish", ("spec-unreadable",))


def test_a_live_leg_with_no_hashable_identity_is_source_key_invalid(tmp_path: Path) -> None:
    """`_classify_legs` returns an `unstable-source[...]` placeholder; it is not a source key."""
    root = _da_root(tmp_path, "unstable", LIVE_A)
    unstable = {"class": "snowflake", "server": "s.example", "powerbi_target": "live_source"}

    result = _assess(root, _da_spec(unstable))

    assert (result.state, result.codes) == ("cannot_establish", ("source-key-invalid",))


def _direct_actions(root: Path) -> list[str]:
    return [json.loads(line)["action"] for line in (root / cg.AUDIT).read_text(encoding="utf-8").splitlines()]


UPSTREAM_REF = cg.provider_reference("Upstream")
OTHER_REF = cg.provider_reference("Other")
SUPERSTORE_REF = cg.provider_reference("Superstore")
UP_REF = cg.provider_reference("Up")
S2_UNIT_REF = cg.provider_reference("Exact_S2_unit")

PROVIDER_LIVE = cg.DataAccessAssessment(
    state="live_data_ok",
    source_keys=(KEY_A,),
    provider_unit=None,
    provider_state=None,
    validation="validated",
    effective_scope="model_and_report",
    max_phase2_claim="data_validated",
    codes=("probe-cleared", "probe-data-ok"),
)
PROVIDER_LOCAL = cg.DataAccessAssessment(
    state="local_import_ready",
    source_keys=(),
    provider_unit=None,
    provider_state=None,
    validation="validated",
    effective_scope="model_and_report",
    max_phase2_claim="data_validated",
    codes=("all-flat-file", "package-self-contained"),
)
PROVIDER_AUTHORIZED = cg.DataAccessAssessment(
    state="authorized_model_only",
    source_keys=(KEY_A,),
    provider_unit=None,
    provider_state=None,
    validation="unvalidated",
    effective_scope="model_only",
    max_phase2_claim="structural_only",
    codes=("brief-model-only", "human-authorize"),
)
PROVIDER_BLOCKED = cg.DataAccessAssessment(
    state="blocked",
    source_keys=(),
    provider_unit=None,
    provider_state=None,
    validation="not_established",
    effective_scope=None,
    max_phase2_claim="none",
    codes=("marker-only",),
)
PROVIDER_RECURSIVE = cg.DataAccessAssessment(
    state="provider_inherited",
    source_keys=(KEY_A,),
    provider_unit=UPSTREAM_REF,
    provider_state="live_data_ok",
    validation="validated",
    effective_scope="model_and_report",
    max_phase2_claim="data_validated",
    codes=("provider-exact",),
)


@pytest.mark.parametrize(
    ("provider_assessment", "expected_keys"),
    [
        pytest.param(PROVIDER_LIVE, (KEY_A,), id="live-provider"),
        pytest.param(PROVIDER_LOCAL, (), id="local-provider"),
    ],
)
def test_exactly_one_direct_provider_is_inherited_field_for_field(
    tmp_path: Path, provider_assessment: object, expected_keys: tuple
) -> None:
    """The consumer copies the provider's semantic fields verbatim and names it exactly."""
    root = _da_root(tmp_path, f"consumer-{len(expected_keys)}", FLAT)

    result = _assess(
        root, _da_spec(FLAT), scope="report_only_shared_model", provider=(SUPERSTORE_REF, provider_assessment)
    )

    assert (result.state, result.codes) == ("provider_inherited", ("provider-exact",))
    assert (result.provider_unit, result.provider_state) == (SUPERSTORE_REF, provider_assessment.state)
    assert result.source_keys == expected_keys
    assert result.validation == provider_assessment.validation
    assert result.max_phase2_claim == provider_assessment.max_phase2_claim
    assert result.effective_scope == "report_only_shared_model"


def test_a_model_only_provider_cannot_authorize_a_report_only_consumer(tmp_path: Path) -> None:
    """Intersecting `model_only` with `report_only_shared_model` is empty - but only for the REPORT.

    The same provider still inherits fine at model-only scope, which is the control that proves the
    refusal is about the topology intersection rather than about the provider being unusable.
    """
    root = _da_root(tmp_path, "model-only-provider", FLAT)
    provider = (cg.provider_reference("SharedModel"), PROVIDER_AUTHORIZED)

    report_consumer = _assess(root, _da_spec(FLAT), scope="report_only_shared_model", provider=provider)
    model_consumer = _assess(root, _da_spec(FLAT), scope="model_only", provider=provider)

    assert (report_consumer.state, report_consumer.codes) == ("blocked", ("provider-model-only",))
    assert (model_consumer.state, model_consumer.provider_state) == ("provider_inherited", "authorized_model_only")
    assert (model_consumer.validation, model_consumer.max_phase2_claim) == ("unvalidated", "structural_only")


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        pytest.param(None, "provider-missing", id="absent"),
        pytest.param([], "provider-missing", id="resolved-nothing"),
        pytest.param(
            [(UPSTREAM_REF, PROVIDER_LIVE), (OTHER_REF, PROVIDER_LOCAL)], "provider-ambiguous", id="two-candidates"
        ),
        pytest.param(
            [(UPSTREAM_REF, PROVIDER_LIVE), (UPSTREAM_REF, PROVIDER_LIVE)], "provider-ambiguous", id="two-equal"
        ),
        pytest.param((UPSTREAM_REF, PROVIDER_RECURSIVE), "provider-ambiguous", id="recursive"),
        pytest.param((UPSTREAM_REF, PROVIDER_BLOCKED), "provider-missing", id="not-an-authority"),
        pytest.param(("", PROVIDER_LIVE), "provider-foreign", id="empty-unit"),
        pytest.param((UPSTREAM_REF, {"state": "live_data_ok"}), "provider-foreign", id="foreign-shape"),
        pytest.param((UPSTREAM_REF, PROVIDER_LIVE, "extra"), "provider-foreign", id="wrong-arity"),
        pytest.param([(UPSTREAM_REF, "not an assessment")], "provider-foreign", id="one-malformed-candidate"),
        pytest.param("Upstream", "provider-foreign", id="bare-string"),
    ],
)
def test_an_unusable_provider_is_named_rather_than_searched_for(
    tmp_path: Path, provider: object, expected: str
) -> None:
    """No registry lookup, no name match, no ancestor walk - each failure gets its own code.

    The LIST cases are how a caller reports what its cohort resolution actually found. Without
    them `provider-ambiguous` is unreachable through the API and a caller holding two LUID matches
    has to collapse them into `None`, which reads as "no provider exists" and understates it.
    """
    root = _da_root(tmp_path, f"provider-{expected}-{type(provider).__name__}-{len(str(provider))}", FLAT)

    result = cg.assess_data_access(
        root,
        package_spec=_da_spec(FLAT),
        package_data_sources=COMPLETE_LOCAL,
        fallback_authorization="stop",
        requested_scope="report_only_shared_model",
        provider=provider,
    )

    assert (result.state, result.codes) == ("cannot_establish", (expected,))


def test_a_single_candidate_list_inherits_exactly_like_a_bare_pair(tmp_path: Path) -> None:
    """Control for the ambiguity rule: one candidate is not ambiguous, however it was passed."""
    root = _da_root(tmp_path, "one-candidate", FLAT)

    as_list = _assess(
        root, _da_spec(FLAT), scope="report_only_shared_model", provider=[(SUPERSTORE_REF, PROVIDER_LIVE)]
    )
    as_pair = _assess(root, _da_spec(FLAT), scope="report_only_shared_model", provider=(SUPERSTORE_REF, PROVIDER_LIVE))

    assert as_list == as_pair
    assert (as_list.state, as_list.provider_unit) == ("provider_inherited", SUPERSTORE_REF)


@pytest.mark.parametrize("policy", ["stop", "model_only_unvalidated"])
@pytest.mark.parametrize("scope", ["model_and_report", "model_only", "report_only_shared_model"])
def test_the_typed_inputs_are_a_closed_vocabulary(tmp_path: Path, policy: str, scope: str) -> None:
    """Accepted members must work; anything else is a CALLER bug and raises rather than degrading."""
    root = _da_root(tmp_path, f"vocab-{policy}-{scope}", FLAT)
    _assess(root, _da_spec(FLAT), policy=policy, scope=scope, provider=(cg.provider_reference("U"), PROVIDER_LOCAL))

    with pytest.raises(ValueError):
        _assess(root, _da_spec(FLAT), policy="whatever")
    with pytest.raises(ValueError):
        _assess(root, _da_spec(FLAT), scope="everything")


def _projection(**overrides) -> dict:
    payload = {
        "schema": "phase1-data-access/v1",
        "state": "live_data_ok",
        "source_keys": [KEY_A],
        "provider_unit": None,
        "provider_state": None,
        "validation": "validated",
        "effective_scope": "model_and_report",
        "max_phase2_claim": "data_validated",
        "codes": ["probe-cleared", "probe-data-ok"],
    }
    payload.update(overrides)
    return payload


ROUND_TRIP_CASES = (PROVIDER_LIVE, PROVIDER_LOCAL, PROVIDER_AUTHORIZED, PROVIDER_BLOCKED, PROVIDER_RECURSIVE)


@pytest.mark.parametrize("assessment", ROUND_TRIP_CASES, ids=[a.state for a in ROUND_TRIP_CASES])
def test_every_producible_assessment_survives_its_own_strict_parser(tmp_path: Path, assessment: object) -> None:
    """Producer and parser must agree by construction, and the bytes must be stable."""
    text = assessment.dumps()

    assert cg.parse_data_access(text) == assessment
    assert assessment.dumps() == text, "serialization must be deterministic, or S1 hashes churn"

    path = tmp_path / "data-access.json"
    path.write_text(text, encoding="utf-8")
    assert cg.read_data_access(path) == assessment


def test_a_cannot_establish_projection_round_trips_too(tmp_path: Path) -> None:
    """The refusal states ship as artifacts as well; they must survive the same parser."""
    root = _da_root(tmp_path, "refusal-round-trip", LIVE_A)
    refusal = _assess(root, _da_spec(LIVE_A))

    assert refusal.state == "cannot_establish"
    assert cg.parse_data_access(refusal.dumps()) == refusal


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        pytest.param("{ nope", "malformed-json", id="malformed-json"),
        pytest.param("[]", "not-an-object", id="not-an-object"),
        pytest.param('{"schema": 1, "schema": 2}', "duplicate-key", id="duplicate-key-shallow"),
        pytest.param(
            json.dumps(_projection())[:-1] + ', "codes": ["probe-cleared"]}', "duplicate-key", id="duplicate-key-real"
        ),
    ],
)
def test_the_projection_parser_refuses_malformed_text_by_name(text: str, reason: str) -> None:
    """Textual refusals, each identified. `.code` is always the shipped `projection-invalid`."""
    with pytest.raises(cg.DataAccessProjectionError) as excinfo:
        cg.parse_data_access(text)

    assert excinfo.value.reason == reason
    assert excinfo.value.code == "projection-invalid"


def test_a_nonfinite_number_is_refused_as_such(tmp_path: Path) -> None:
    """`NaN`/`Infinity` are not JSON; Python's decoder accepts them unless told not to."""
    payload = json.dumps(_projection())[:-1] + ', "extra": Infinity}'

    with pytest.raises(cg.DataAccessProjectionError) as excinfo:
        cg.parse_data_access(payload)

    assert excinfo.value.reason == "nonfinite"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        pytest.param({**_projection(), "surprise": 1}, "unknown-field", id="unknown-field"),
        pytest.param({k: v for k, v in _projection().items() if k != "codes"}, "missing-field", id="missing-field"),
        pytest.param(_projection(schema="phase1-data-access/v2"), "unknown-value", id="wrong-schema"),
        pytest.param(_projection(state=7), "bad-type", id="state-not-a-string"),
        pytest.param(_projection(state="excellent"), "unknown-value", id="unknown-state"),
        pytest.param(_projection(validation=None), "bad-type", id="validation-not-nullable"),
        pytest.param(_projection(effective_scope="everything"), "unknown-value", id="unknown-scope"),
        pytest.param(_projection(max_phase2_claim="lots"), "unknown-value", id="unknown-ceiling"),
        pytest.param(_projection(provider_unit=""), "bad-type", id="empty-provider-unit"),
        pytest.param(_projection(provider_state="blocked"), "unknown-value", id="provider-state-not-direct"),
        pytest.param(_projection(source_keys="nope"), "bad-type", id="source-keys-not-a-list"),
        pytest.param(_projection(source_keys=[1]), "bad-type", id="source-key-not-a-string"),
        pytest.param(_projection(source_keys=[KEY_B, KEY_A]), "source-keys-unsorted", id="unsorted-keys"),
        pytest.param(_projection(source_keys=[KEY_A, KEY_A]), "source-keys-unsorted", id="duplicate-keys"),
        pytest.param(_projection(source_keys=["source-key:zzzz"]), "source-key-invalid", id="bad-key-syntax"),
        pytest.param(_projection(source_keys=["a.example"]), "source-key-invalid", id="display-name-as-key"),
        pytest.param(_projection(codes=["probe-data-ok", "probe-cleared"]), "codes-unsorted", id="unsorted-codes"),
        pytest.param(_projection(codes=["probe-cleared", "totally-fine"]), "unknown-value", id="unknown-code"),
    ],
)
def test_the_projection_parser_refuses_bad_fields_by_name(payload: dict, reason: str) -> None:
    """Field-level strictness. Each case must name ITS refusal, not a single catch-all."""
    with pytest.raises(cg.DataAccessProjectionError) as excinfo:
        cg.parse_data_access(json.dumps(payload))

    assert excinfo.value.reason == reason


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_projection(source_keys=[]), id="live-without-keys"),
        pytest.param(_projection(codes=["probe-cleared"]), id="live-missing-a-code"),
        pytest.param(_projection(validation="unvalidated"), id="live-not-validated"),
        pytest.param(_projection(effective_scope="report_only_shared_model"), id="direct-with-consumer-scope"),
        pytest.param(
            _projection(
                state="local_import_ready",
                codes=["all-flat-file", "package-self-contained"],
            ),
            id="local-carrying-live-keys",
        ),
        pytest.param(
            _projection(
                state="authorized_model_only",
                source_keys=[KEY_A],
                codes=["brief-model-only", "human-authorize"],
                validation="unvalidated",
                effective_scope="model_and_report",
                max_phase2_claim="structural_only",
            ),
            id="authorized-outside-model-only",
        ),
        pytest.param(
            _projection(
                state="blocked",
                source_keys=[],
                codes=["all-flat-file"],
                validation="not_established",
                effective_scope=None,
                max_phase2_claim="none",
            ),
            id="blocked-with-an-accepted-code",
        ),
        pytest.param(
            _projection(
                state="cannot_establish",
                source_keys=[KEY_A],
                codes=["audit-missing"],
                validation="not_established",
                effective_scope=None,
                max_phase2_claim="none",
            ),
            id="cannot-establish-still-publishing-keys",
        ),
        pytest.param(
            _projection(
                state="provider_inherited",
                codes=["provider-exact"],
                provider_unit=UPSTREAM_REF,
                provider_state=None,
            ),
            id="inherited-without-a-provider-state",
        ),
        pytest.param(
            _projection(
                state="provider_inherited",
                source_keys=[],
                codes=["provider-exact"],
                provider_unit=UPSTREAM_REF,
                provider_state="live_data_ok",
            ),
            id="inherited-live-provider-with-no-keys",
        ),
        pytest.param(
            _projection(
                state="provider_inherited",
                codes=["provider-exact"],
                provider_unit=None,
                provider_state="live_data_ok",
            ),
            id="inherited-without-a-provider-unit",
        ),
        pytest.param(_projection(provider_unit=UPSTREAM_REF), id="direct-state-naming-a-provider"),
    ],
)
def test_the_projection_parser_refuses_impossible_state_combinations(payload: dict) -> None:
    """Types alone cannot catch a semantically impossible projection; these are the tampered shapes."""
    with pytest.raises(cg.DataAccessProjectionError) as excinfo:
        cg.parse_data_access(json.dumps(payload))

    assert excinfo.value.reason == "illegal-combination"


def test_reading_an_absent_projection_is_a_refusal_not_an_empty_state(tmp_path: Path) -> None:
    """`read_data_access` must never degrade a missing artifact into a permissive default."""
    with pytest.raises(cg.DataAccessProjectionError) as excinfo:
        cg.read_data_access(tmp_path / "nope" / "data-access.json")

    assert excinfo.value.reason == "unreadable"
    assert excinfo.value.code == "projection-invalid"


def test_the_module_still_loads_the_way_the_hook_loads_it(tmp_path: Path) -> None:
    """A latent trap this slice tripped, pinned so the next addition cannot re-arm it.

    `scripts/hooks/credential_gate.py` loads this module with
    `importlib.util.module_from_spec` + `exec_module` and deliberately does NOT register it in
    `sys.modules`. Combined with `from __future__ import annotations`, a `@dataclass` in this
    module then makes `dataclasses._is_type` dereference `sys.modules.get(cls.__module__)` - which
    is `None` - and raise. The hook catches everything and fails closed, so the visible symptom is
    that EVERY hook decision becomes a deny: measured here, and caught only by
    `test_credential_gate_shield.py`, never by importing this module normally.

    ⚠️ Residual, deliberately not fixed in this slice: the hook's own loader is still fragile, and
    fixing it means editing `scripts/hooks/credential_gate.py`, which is outside this change's
    closed surface. This test makes the trap loud instead of silent.
    """
    spec = importlib.util.spec_from_file_location("_credential_gate_core_for_hook", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert "_credential_gate_core_for_hook" not in sys.modules, "the hook does not register it, so neither may this"

    spec.loader.exec_module(module)

    assert module.assess_data_access is not None
    assert (
        module.DataAccessAssessment(
            state="blocked",
            source_keys=(),
            provider_unit=None,
            provider_state=None,
            validation="not_established",
            effective_scope=None,
            max_phase2_claim="none",
            codes=("marker-only",),
        ).state
        == "blocked"
    )
    assert not (tmp_path / "unused").exists(), "importing the module must not touch the filesystem"


def test_an_unkeyed_clear_earns_nothing_even_after_a_keyed_measurement(tmp_path: Path) -> None:
    """The OTHER half of the keyed-evidence invariant, and a fail-open measured on this branch.

    A `probe-cleared` carrying no source list used to earn `live_data_ok` for every key that held a
    keyed success, because "does this clear name my key?" was SKIPPED rather than FAILED when there
    was no name to test. It is reachable from production, not only from a forged log:
    `clear_block(..., earned=True)` passes `_last_block_sources`, which is None whenever the arm's
    source list cannot be parsed, and `_audit` then omits the field.

    Three shapes, all unattributable, plus the keyed positive control.
    """
    keyed = _da_root(tmp_path, "clear-keyed", LIVE_A)
    _trail(keyed, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))

    absent = _da_root(tmp_path, "clear-absent", LIVE_A)
    _trail(absent, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", None))

    empty = _da_root(tmp_path, "clear-empty", LIVE_A)
    _trail(empty, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", []))

    other = _da_root(tmp_path, "clear-other-key", LIVE_A, LIVE_B)
    _trail(other, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_B]))

    assert _assess(keyed, _da_spec(LIVE_A)).state == "live_data_ok", "control: the keyed clear must still earn"
    for root, why in ((absent, "no sources field"), (empty, "empty sources list")):
        result = _assess(root, _da_spec(LIVE_A))
        assert (result.state, result.codes) == ("blocked", ("stale-clear",)), why

    # A clear naming a DIFFERENT key does not touch this one at all, so the honest code is
    # `marker-only` - armed, measured, and nothing ever lifted it - rather than `stale-clear`,
    # which would claim a clear was applied here and found wanting.
    sibling = _assess(other, _da_spec(LIVE_A))
    assert (sibling.state, sibling.codes) == ("blocked", ("marker-only",))


def test_an_unattributable_clear_does_not_un_earn_an_already_proved_key(tmp_path: Path) -> None:
    """Fail-closed must not overshoot: proof is removed by a new arm or a measured failure only.

    Without this the previous test would also be satisfied by a rule that treats any unreadable
    record as invalidating, which would blank out correctly earned units on a log with one legacy
    line in it.
    """
    root = _da_root(tmp_path, "unattributable-after-earned", LIVE_A)
    _trail(
        root,
        ("block", [KEY_A]),
        ("probe-data_ok", [KEY_A]),
        ("probe-cleared", [KEY_A]),
        ("probe-cleared", None),
    )

    assert _assess(root, _da_spec(LIVE_A)).state == "live_data_ok"


def test_an_unattributable_failure_record_still_invalidates_every_key(tmp_path: Path) -> None:
    """The failure direction of the same normalisation: `[]` must not be read as "affects nobody"."""
    root = _da_root(tmp_path, "unattributable-failure", LIVE_A)
    _trail(
        root,
        ("block", [KEY_A]),
        ("probe-data_ok", [KEY_A]),
        ("probe-cleared", [KEY_A]),
        ("probe-no_credential", []),
    )

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.codes) == ("blocked", ("probe-no-credential",))


def test_a_skipped_probe_is_corroborating_history_when_nothing_live_remains(tmp_path: Path) -> None:
    """SKIPPED blocks a CURRENTLY live key, and only that.

    A package whose spec now declares no live source at all is an import-only package; a historic
    `probe-skipped` in its root's log describes a source it no longer has and must not block it.
    The paired live spec is the control proving the entry is still read.
    """
    root = _da_root(tmp_path, "skipped-history", LIVE_A)
    _trail(root, ("block", [KEY_A]), ("probe-skipped", [KEY_A]))

    flat_now = _assess(root, _da_spec(FLAT))
    still_live = _assess(root, _da_spec(LIVE_A))

    assert flat_now.state == "local_import_ready"
    assert (still_live.state, still_live.codes) == ("blocked", ("live-probe-skipped",))


def test_every_state_the_assessor_actually_produces_survives_the_strict_parser(tmp_path: Path) -> None:
    """Producer/parser agreement on ASSESSOR OUTPUT, not on hand-built constants.

    The round-trip cases above are literals a test author wrote, so they prove the parser accepts
    what a human believed the assessor emits. This builds one assessment of each producible state
    from real fixtures and re-parses its own bytes, which is the check that actually catches the
    producer and the parser drifting apart.
    """
    live = _da_root(tmp_path, "rt-live", LIVE_A)
    _trail(live, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))
    blocked = _da_root(tmp_path, "rt-blocked", LIVE_A)
    _trail(blocked, ("block", [KEY_A]))
    authorized = _authorized_root(tmp_path, "rt-authorized")
    flat = _da_root(tmp_path, "rt-flat", FLAT)
    consumer = _da_root(tmp_path, "rt-consumer", FLAT)

    produced = [
        _assess(live, _da_spec(LIVE_A)),
        _assess(blocked, _da_spec(LIVE_A)),
        _assess(authorized, _da_spec(LIVE_A), policy="model_only_unvalidated", scope="model_only"),
        _assess(flat, _da_spec(FLAT)),
        _assess(flat, _da_spec(LIVE_A)),
        _assess(consumer, _da_spec(FLAT), scope="report_only_shared_model", provider=(UP_REF, PROVIDER_LIVE)),
    ]

    assert {result.state for result in produced} == set(cg.DATA_ACCESS_STATES), (
        "this corpus must cover every producible state, or the round trip is partial"
    )
    for result in produced:
        assert cg.parse_data_access(result.dumps()) == result, result.state


def test_no_projection_field_can_carry_a_host_path_or_display_name(tmp_path: Path) -> None:
    """Privacy is structural: the exported surface has no field shaped to hold customer text."""
    root = _da_root(tmp_path, "privacy", LIVE_A, FLAT)
    _trail(root, ("block", [KEY_A]), ("probe-unreachable", [KEY_A]))

    payload = json.dumps(_assess(root, _da_spec(LIVE_A, FLAT)).to_json())

    for secret in ("a.example", "sqlserver", "excel-direct", str(root), "ds0", "ds1"):
        assert secret not in payload, f"{secret!r} reached the projection"
    assert set(json.loads(payload)) == set(cg.DATA_ACCESS_FIELDS)


@pytest.mark.parametrize("authorized", [False, True], ids=["live-pair", "human-fallback"])
def test_data_access_requires_an_arm_for_each_current_key(tmp_path: Path, authorized: bool) -> None:
    """Neither a measurement pair nor authorization may invent a missing per-key epoch."""
    root = _da_root(tmp_path, "unarmed-key", LIVE_A, LIVE_B)
    _trail(root, ("block", [KEY_B]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))
    if authorized:
        _trail(root, ("authorize", None))
        (root / cg.OVERRIDE).write_text("human authorization\n", encoding="utf-8")

    result = _assess(
        root, _da_spec(LIVE_A), policy="model_only_unvalidated" if authorized else "stop", scope="model_only"
    )

    assert (result.state, result.codes) == ("cannot_establish", ("source-key-set-changed",))


@pytest.mark.parametrize("backdated_action", ["probe-data_ok", "probe-cleared", "authorize"])
def test_data_access_rejects_backdated_epoch_evidence(tmp_path: Path, backdated_action: str) -> None:
    """File order cannot launder a successful event timestamped before the current arm."""
    root = _da_root(tmp_path, "backdated-epoch", LIVE_A)
    events = [("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A])]
    if backdated_action == "authorize":
        events = [("block", [KEY_A]), ("authorize", None)]
        (root / cg.OVERRIDE).write_text("human authorization\n", encoding="utf-8")
    _trail(root, *events)
    audit = root / cg.AUDIT
    entries = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    for entry in entries:
        entry["ts"] = "2026-09-11T08:00:00+00:00"
        if entry["action"] == backdated_action:
            entry["ts"] = "2020-01-01T00:00:00+00:00"
    audit.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")

    kwargs = {"policy": "model_only_unvalidated", "scope": "model_only"} if backdated_action == "authorize" else {}
    result = _assess(root, _da_spec(LIVE_A), **kwargs)

    assert result.state == "blocked"
    assert ("authorization-mismatch" if backdated_action == "authorize" else "stale-clear") in result.codes
    for entry in entries:
        entry["ts"] = "2026-09-11T08:00:00+00:00"
    audit.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    assert _assess(root, _da_spec(LIVE_A), **kwargs).state == (
        "authorized_model_only" if backdated_action == "authorize" else "live_data_ok"
    )


def test_data_access_latest_success_still_needs_its_own_clear(tmp_path: Path) -> None:
    """The latest attempt must precede its earning clear, including after another success."""
    root = _da_root(tmp_path, "repeated-success", LIVE_A)
    _trail(
        root,
        ("block", [KEY_A]),
        ("probe-data_ok", [KEY_A]),
        ("probe-cleared", [KEY_A]),
        ("probe-data_ok", [KEY_A]),
    )

    assert _assess(root, _da_spec(LIVE_A)).state == "blocked"
    _trail(root, ("probe-cleared", [KEY_A]))
    assert _assess(root, _da_spec(LIVE_A)).state == "live_data_ok"


@pytest.mark.parametrize(
    "sources",
    [None, [], ["legacy display"], [""], [KEY_A, "legacy display"]],
    ids=["missing", "empty", "legacy-name", "blank-name", "mixed-identity"],
)
def test_data_access_unattributable_failures_cannot_leave_sibling_keys_green(tmp_path: Path, sources: object) -> None:
    """Unknown attribution is not an empty target set; both previously earned keys must refuse."""
    root = _da_root(tmp_path, "unknown-failure", LIVE_A, LIVE_B)
    _trail(root, ("block", [KEY_A, KEY_B]), ("probe-data_ok", [KEY_A, KEY_B]), ("probe-cleared", [KEY_A, KEY_B]))
    assert _assess(root, _da_spec(LIVE_A, LIVE_B)).state == "live_data_ok"
    _trail(root, ("probe-error", sources))

    for connection in (LIVE_A, LIVE_B):
        assert _assess(root, _da_spec(connection)).state in {"blocked", "cannot_establish"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("sources", True),
        ("sources", KEY_A),
        ("sources", {}),
        ("sources", [True]),
        ("sources", [None]),
        ("sources", [[KEY_A]]),
        ("sources", [KEY_A, KEY_A]),
        ("action", ["probe-error"]),
        ("action", False),
        ("ts", True),
        ("ts", "not-a-time"),
        ("ts", "2026-09-11T08:00:00"),
    ],
)
def test_data_access_malformed_audit_fields_poison_the_whole_trail(tmp_path: Path, field: str, value: object) -> None:
    """Typed audit fields cannot be stringified or ignored to preserve a previous green."""
    root = _da_root(tmp_path, "bad-audit-field", LIVE_A)
    _trail(root, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))
    assert _assess(root, _da_spec(LIVE_A)).state == "live_data_ok"
    entry = {
        "ts": "2026-09-11T08:00:00+00:00",
        "action": "probe-error",
        "detail": "source probe -> ERROR",
        "user": "test",
        "sources": [KEY_A],
        "scope": str(root.resolve()),
        field: value,
    }
    with (root / cg.AUDIT).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.codes) == ("cannot_establish", ("audit-malformed",))
    assert cg._audit_entries(root) is None


@pytest.mark.parametrize("poison", ["duplicate-action", "duplicate-scope", "invalid-utf8"])
def test_data_access_strict_audit_rejects_lossy_decoding(tmp_path: Path, poison: str) -> None:
    """The sole audit reader must reject duplicate JSON keys and undecodable bytes."""
    root = _da_root(tmp_path, "lossy-audit", LIVE_A)
    _trail(root, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))
    entry = json.dumps(
        {
            "scope": str(root.resolve()),
            "ts": "2026-09-11T08:00:00+00:00",
            "action": "probe-error",
            "detail": "source probe -> ERROR",
            "user": "test",
            "sources": [KEY_A],
        }
    )
    if poison == "duplicate-action":
        tail = (entry[:-1] + ', "action": "block-skipped"}\n').encode("utf-8")
    elif poison == "duplicate-scope":
        tail = (entry[:-1] + ', "scope": ' + json.dumps(str(root.resolve())) + "}\n").encode("utf-8")
    else:
        tail = b"\xff\n"
    with (root / cg.AUDIT).open("ab") as handle:
        handle.write(tail)

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.codes) == ("cannot_establish", ("audit-malformed",))


def test_data_access_authorization_reads_the_audit_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Authorization uses the same immutable ledger snapshot, not another mutable audit read."""
    root = _authorized_root(tmp_path, "single-read")
    read = cg._read_audit_trail
    calls = []

    def counted_read(target: Path) -> tuple:
        calls.append(target)
        return read(target)

    monkeypatch.setattr(cg, "_read_audit_trail", counted_read)
    result = _assess(root, _da_spec(LIVE_A), policy="model_only_unvalidated", scope="model_only")

    assert result.state == "authorized_model_only"
    assert calls == [root]


def test_data_access_root_keys_are_not_silently_collapsed(tmp_path: Path) -> None:
    """The package subset must not hide ambiguous keys in the canonical root facts."""
    root = _da_root(tmp_path, "duplicate-root", LIVE_A, LIVE_A)
    _trail(root, ("block", [KEY_A]), ("probe-data_ok", [KEY_A]), ("probe-cleared", [KEY_A]))

    result = _assess(root, _da_spec(LIVE_A))

    assert (result.state, result.codes) == ("cannot_establish", ("source-key-invalid",))


@pytest.mark.parametrize(
    "provider",
    [
        False,
        True,
        {},
        (),
        (UP_REF, PROVIDER_LIVE, "extra"),
        [UP_REF, PROVIDER_LIVE],
        [(UP_REF, PROVIDER_LIVE), False],
        [(UP_REF, PROVIDER_LIVE), (OTHER_REF, {})],
        ((UP_REF, PROVIDER_LIVE),),
        ((UP_REF, PROVIDER_LIVE), (OTHER_REF, PROVIDER_LOCAL)),
    ],
)
def test_data_access_provider_candidate_shapes_are_not_coerced(tmp_path: Path, provider: object) -> None:
    """A candidate list is not a list-shaped pair, tuple collection, or boolean sentinel."""
    root = _da_root(tmp_path, "provider-shape", FLAT)

    result = _assess(root, _da_spec(FLAT), scope="report_only_shared_model", provider=provider)

    assert (result.state, result.codes) == ("cannot_establish", ("provider-foreign",))


@pytest.mark.parametrize(
    "provider",
    [
        PROVIDER_LIVE._replace(source_keys=(KEY_A, KEY_A)),
        PROVIDER_LIVE._replace(source_keys=[KEY_A]),
        PROVIDER_LIVE._replace(source_keys=(KEY_A + "\n",)),
        PROVIDER_LIVE._replace(validation=True),
        PROVIDER_LIVE._replace(codes=("probe-cleared",)),
        PROVIDER_LIVE._replace(codes=["probe-cleared", "probe-data-ok"]),
        PROVIDER_LOCAL._replace(source_keys=""),
        PROVIDER_LIVE._replace(provider_unit=cg.provider_reference("hidden-recursion")),
    ],
)
def test_data_access_provider_assessment_must_already_be_strict(tmp_path: Path, provider: object) -> None:
    """Inheritance cannot repair, deduplicate, or amplify an invalid provider projection."""
    root = _da_root(tmp_path, "provider-semantics", FLAT)

    result = _assess(root, _da_spec(FLAT), scope="report_only_shared_model", provider=(UP_REF, provider))

    assert (result.state, result.codes) == ("cannot_establish", ("provider-foreign",))


def test_data_access_provider_resolution_stays_with_the_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real published-source consumer inherits only the supplied S2 result; it searches nothing."""
    root = _da_root(tmp_path, "published-consumer", FLAT)
    published_spec = _da_spec({"class": "sqlproxy", "powerbi_target": "unknown"})

    def forbidden(*_args, **_kwargs):
        pytest.fail("provider inheritance attempted direct evidence or source classification")

    for name in ("_read_audit_trail", "load_bundle", "_classify_legs", "_override_is_authentic", "verify", "_audit"):
        monkeypatch.setattr(cg, name, forbidden)
    inherited = _assess(root, published_spec, scope="report_only_shared_model", provider=(S2_UNIT_REF, PROVIDER_LIVE))
    missing = _assess(root, published_spec, scope="report_only_shared_model", provider=[])

    assert (inherited.state, inherited.provider_unit, inherited.source_keys) == (
        "provider_inherited",
        S2_UNIT_REF,
        PROVIDER_LIVE.source_keys,
    )
    assert (missing.state, missing.codes) == ("cannot_establish", ("provider-missing",))


@pytest.mark.parametrize("scope", ["model_and_report", "report_only_shared_model"])
def test_data_access_inherited_authorization_cannot_expand_to_reports(tmp_path: Path, scope: str) -> None:
    """Structural-only provider authority stays model-only in both producer and strict reader."""
    root = _da_root(tmp_path, "scope-ceiling", FLAT)
    result = _assess(root, _da_spec(FLAT), scope=scope, provider=(UP_REF, PROVIDER_AUTHORIZED))
    assert (result.state, result.codes) == ("blocked", ("provider-model-only",))
    payload = {
        **PROVIDER_RECURSIVE.to_json(),
        "provider_state": "authorized_model_only",
        "validation": "unvalidated",
        "max_phase2_claim": "structural_only",
        "effective_scope": scope,
    }
    with pytest.raises(cg.DataAccessProjectionError, match="illegal-combination"):
        cg.parse_data_access(json.dumps(payload))


def test_data_access_source_key_syntax_requires_the_whole_string() -> None:
    """A final newline is outside the stable key, despite the special meaning of regex `$`."""
    with pytest.raises(cg.DataAccessProjectionError, match="source-key-invalid"):
        cg.parse_data_access(json.dumps(_projection(source_keys=[KEY_A + "\n"])))


def test_data_access_unreadable_projection_still_has_a_typed_refusal(tmp_path: Path) -> None:
    """Invalid UTF-8 is a read failure, not an uncaught decoder exception."""
    path = tmp_path / "projection.json"
    path.write_bytes(b"\xff")
    with pytest.raises(cg.DataAccessProjectionError, match="unreadable"):
        cg.read_data_access(path)
