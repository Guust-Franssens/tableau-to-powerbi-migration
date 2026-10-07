"""Tests for audit-backed credential-gate shielding in the hook."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_credential_gate import KEY_A, KEY_B, LIVE_A, LIVE_B, REARM_BLOCKERS, _da_spec, cg, load_hook_module
from test_credential_gate import gate_acl as gate_acl

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "credential_gate.py"
HOOK = REPO / "scripts" / "hooks" / "credential_gate.py"
MARKER = ".credential-gate-BLOCKED.json"
OVERRIDE = ".credential-gate-AUTHORIZED"
AUDIT = ".credential-gate-audit.log"
pytestmark = pytest.mark.usefixtures("gate_acl")


def run_gate(*args: str) -> subprocess.CompletedProcess:
    """Run the real CLI parser with the fixture's fully stubbed Windows ACL operations."""
    return subprocess.CompletedProcess([str(GATE), *args], cg.main(list(args)), "", "")


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


def migration_fixture(root: Path, *, both: bool = False) -> Path:
    """Create an exact current source scope, not a display-name-only proof fixture."""
    migration = root / "mig"
    (migration / "fabric").mkdir(parents=True)
    spec = _da_spec(LIVE_A, LIVE_B) if both else _da_spec(LIVE_A)
    (migration / "migration-spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return migration


def write_marker(path: Path, sources: list[str]) -> None:
    """Write a BLOCKED marker without going through apply_block's scope guard."""
    path.write_text(json.dumps({"writes_blocked": True, "sources": sources}), encoding="utf-8")


def append_audit(migration: Path, action: str, detail: str, sources: list[str] | None = None) -> None:
    """Append one audit entry for states that are awkward to create through the CLI."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "detail": detail,
        "user": "test",
        "scope": str(migration.resolve()),
    }
    if sources is not None:
        entry["sources"] = sources
    with (migration / AUDIT).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def hook_for_model_write(root: Path, migration: Path) -> dict:
    """Ask the hook whether a model write under migration/fabric is allowed."""
    target = migration / "fabric" / "Model.tmdl"
    return run_hook({"toolName": "create", "toolArgs": json.dumps({"path": str(target)}), "cwd": str(root)})


def earn(migration: Path) -> None:
    """The shield requires a keyed measurement followed by a clear in the same arm epoch."""
    assert run_gate("block", str(migration), "--sources", KEY_A).returncode == 0
    append_audit(migration, "probe-data_ok", "fixture measured a row", [KEY_A])
    assert run_gate("clear", str(migration), "--reason", "DATA_OK", "--earned", "--sources", KEY_A).returncode == 0
    assert cg._clear_was_earned(migration, [KEY_A]) == "probe-cleared"


def test_probe_cleared_directory_shields_against_later_ancestor_marker(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    earn(migration)
    write_marker(tmp_path / MARKER, [KEY_A])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") != "deny", "earned local clearance must stop an unrelated ancestor block"


def test_forged_bare_override_does_not_shield_from_an_ancestor_marker(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    (migration / OVERRIDE).write_text("forged by agent", encoding="utf-8")
    write_marker(tmp_path / MARKER, [KEY_A])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "a bare file with no audit backing must not become a shield"


def test_manual_clear_does_not_shield_from_an_ancestor_marker(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    assert run_gate("block", str(migration), "--sources", KEY_A).returncode == 0
    assert run_gate("clear", str(migration), "--reason", "manual teardown").returncode == 0
    write_marker(tmp_path / MARKER, [KEY_A])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "manual-clear earns no exemption from ancestor blocks"


def test_authorize_audit_entry_without_override_does_not_shield_from_ancestor_marker(tmp_path: Path) -> None:
    """An authorize action without its file is neither permission nor probe proof."""
    migration = migration_fixture(tmp_path)
    assert run_gate("block", str(migration), "--sources", KEY_A).returncode == 0
    (migration / MARKER).unlink()
    append_audit(migration, "authorize", "by=test; chain=['python.exe', 'pwsh.exe']")
    write_marker(tmp_path / MARKER, [KEY_A])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "authorize is build-only permission, not source proof"


def test_probe_clear_for_one_source_does_not_cover_a_later_two_source_block(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    earn(migration)
    write_marker(tmp_path / MARKER, [KEY_A, KEY_B])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "a new live source must still be blocked"


def test_probe_clear_before_later_rearm_does_not_count_as_current_shield(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path, both=True)
    earn(migration)
    assert run_gate("block", str(migration), "--sources", KEY_A, KEY_B).returncode == 0
    assert run_gate("clear", str(migration), "--reason", "teardown after rearm").returncode == 0
    write_marker(tmp_path / MARKER, [KEY_A, KEY_B])

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "a later block must invalidate earlier probe evidence"


@pytest.mark.parametrize("marker_text", ['{"writes_blocked": true}', "not-json"])
def test_marker_without_parseable_sources_fails_closed_even_after_empty_source_clear(
    tmp_path: Path, marker_text: str
) -> None:
    """Malformed markers must not be converted into an empty-source shield.

    The edge case is a prior empty-source block: if `_marker_sources(marker)` is treated as `[]` on
    parse failure, `_redundant_rearm` sees matching empty source lists and incorrectly shields.
    """
    migration = migration_fixture(tmp_path)
    assert run_gate("block", str(migration)).returncode == 0
    assert run_gate("clear", str(migration), "--reason", "DATA_OK", "--earned").returncode == 0
    (tmp_path / MARKER).write_text(marker_text, encoding="utf-8")

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "unparseable marker sources must fail closed"


@pytest.mark.parametrize("action", (*REARM_BLOCKERS, "probe-data_ok"))
def test_newer_attempt_revokes_the_exact_local_ancestor_shield(tmp_path: Path, action: str) -> None:
    migration = migration_fixture(tmp_path)
    earn(migration)
    append_audit(migration, action, "fixture newer attempt", [KEY_A])
    write_marker(tmp_path / MARKER, [KEY_A])
    assert hook_for_model_write(tmp_path, migration).get("permissionDecision") == "deny"


def test_foreign_key_failure_does_not_revoke_the_exact_local_shield(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path, both=True)
    earn(migration)
    append_audit(migration, "probe-error", "fixture unrelated endpoint", [KEY_B])
    write_marker(tmp_path / MARKER, [KEY_A])
    assert hook_for_model_write(tmp_path, migration).get("permissionDecision") != "deny"


def test_authentic_model_only_permission_is_not_a_probe_earned_shield(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    assert run_gate("block", str(migration), "--sources", KEY_A).returncode == 0
    assert cg.authorize(migration, "Fixture Human") == 0
    assert cg._override_is_authentic(migration)
    write_marker(tmp_path / MARKER, [KEY_A])
    assert not load_hook_module()._earned_clear_shields(migration, tmp_path / MARKER)
