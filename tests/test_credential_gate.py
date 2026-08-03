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
        ("Exception: no catalog found on the instance", "UNREACHABLE"),
        ("something else entirely went wrong", "UNREACHABLE"),
    ]
    for text, expected in cases:
        assert _classify_failure(text)[0] == expected, f"{text!r} should classify as {expected}"


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
    (cleared / "fabric").mkdir(parents=True)
    run_gate("block", str(armed), "--sources", "shipment")
    run_gate("block", str(cleared), "--sources", "shipment")
    run_gate("clear", str(cleared), "--reason", "already earned")

    cmd = f"python scripts/credential_gate.py clear {cleared} --reason teardown"
    out = run_hook({"toolName": "powershell", "toolArgs": json.dumps({"command": cmd}), "cwd": str(tmp_path)})
    assert out.get("permissionDecision") != "deny", "teardown of an unrelated, already-cleared fixture must be allowed"

    run_gate("clear", str(armed), "--reason", "test-teardown")
