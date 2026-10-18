"""Tests for audit-backed credential-gate shielding in the hook."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import credential_gate as gate  # noqa: E402  # pylint: disable=wrong-import-position
import preflight_source_credentials as preflight  # noqa: E402  # pylint: disable=wrong-import-position

GATE = REPO / "scripts" / "credential_gate.py"
HOOK = REPO / "scripts" / "hooks" / "credential_gate.py"
MARKER = ".credential-gate-BLOCKED.json"
OVERRIDE = ".credential-gate-AUTHORIZED"
AUDIT = ".credential-gate-audit.log"


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


def migration_fixture(root: Path, names: tuple[str, ...] = ("A",)) -> Path:
    """Create a minimal migration-shaped directory."""
    migration = root / "mig"
    (migration / "fabric").mkdir(parents=True)
    sources = [
        {
            "name": name,
            "connection": {
                "class": "sqlserver",
                "server": f"{name}.example.invalid",
                "database": "fixture",
                "powerbi_target": "live_source",
            },
        }
        for name in names
    ]
    (migration / "migration-spec.json").write_text(json.dumps({"data_sources": sources}), encoding="utf-8")
    return migration


def source_keys(migration: Path) -> list[str]:
    """Use the actual classifier identities, not display/count aliases."""
    sources = json.loads((migration / "migration-spec.json").read_text(encoding="utf-8"))["data_sources"]
    return [preflight._classify_legs(source, index)[0][0] for index, source in enumerate(sources)]


def earn(migration: Path, sources: list[str]) -> None:
    """Supported writers establish both current-epoch observations and their paired clears."""
    gate.record_probe_attempt(migration, "DATA_OK", "fixture row returned", sources)
    assert gate.clear_block(migration, "fixture DATA_OK", earned=True, sources=sources) == 0


@pytest.fixture(autouse=True)
def marker_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests qualify hook decisions, not native ACL composition."""
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")


def write_marker(path: Path, sources: list[str]) -> None:
    """Write a BLOCKED marker without going through apply_block's scope guard."""
    path.write_text(json.dumps({"writes_blocked": True, "sources": sources}), encoding="utf-8")


def append_audit(migration: Path, action: str, detail: str) -> None:
    """Append one audit entry for states that are awkward to create through the CLI."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "detail": detail,
        "user": "test",
        "scope": str(migration.resolve()),
    }
    with (migration / AUDIT).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def hook_for_model_write(root: Path, migration: Path) -> dict:
    """Ask the hook whether a model write under migration/fabric is allowed."""
    target = migration / "fabric" / "Model.tmdl"
    return run_hook({"toolName": "create", "toolArgs": json.dumps({"path": str(target)}), "cwd": str(root)})


def test_probe_cleared_directory_shields_against_later_ancestor_marker(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    keys = source_keys(migration)
    assert gate.apply_block(migration, keys) == 0
    earn(migration, keys)
    write_marker(tmp_path / MARKER, keys)

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") != "deny", "earned local clearance must stop an unrelated ancestor block"


def test_forged_bare_override_does_not_shield_from_an_ancestor_marker(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    (migration / OVERRIDE).write_text("forged by agent", encoding="utf-8")
    write_marker(tmp_path / MARKER, source_keys(migration))

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "a bare file with no audit backing must not become a shield"


def test_manual_clear_does_not_shield_from_an_ancestor_marker(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    assert gate.apply_block(migration, source_keys(migration)) == 0
    assert gate.clear_block(migration, "manual teardown") != 0
    write_marker(tmp_path / MARKER, source_keys(migration))

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "manual-clear earns no exemption from ancestor blocks"


def test_authorize_audit_entry_without_override_does_not_shield_from_ancestor_marker(tmp_path: Path) -> None:
    """Pin the stricter shield: only a probe-cleared source, not build-only authorization, counts.

    The normal `authorize` command creates the override file, which `_blocking_marker` handles before
    the audit-shield path. Removing the local marker and writing the audit entry directly isolates
    the reachable state where `_redundant_rearm` would return `authorize`.
    """
    migration = migration_fixture(tmp_path)
    assert gate.apply_block(migration, source_keys(migration)) == 0
    (migration / MARKER).unlink()
    append_audit(migration, "authorize", "by=test; chain=['fixture-terminal.exe']")
    write_marker(tmp_path / MARKER, source_keys(migration))

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "authorize is build-only permission, not source proof"


def test_probe_clear_for_one_source_does_not_cover_a_later_two_source_block(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path, ("A", "B"))
    keys = source_keys(migration)
    assert gate.apply_block(migration, keys[:1]) == 0
    earn(migration, keys[:1])
    write_marker(tmp_path / MARKER, keys)

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "a new live source must still be blocked"


def test_probe_clear_before_later_rearm_does_not_count_as_current_shield(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path, ("A", "B"))
    keys = source_keys(migration)
    assert gate.apply_block(migration, keys) == 0
    earn(migration, keys)
    gate.record_probe_attempt(migration, "ERROR", "fixture later blocker", keys[:1])
    assert gate.clear_block(migration, "teardown after rearm") != 0
    write_marker(tmp_path / MARKER, keys)

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
    append_audit(migration, "block-marker-only", "sources=[]")
    append_audit(migration, "probe-cleared", "sources=[]; reason=historical unkeyed clear")
    (tmp_path / MARKER).write_text(marker_text, encoding="utf-8")

    out = hook_for_model_write(tmp_path, migration)

    assert out.get("permissionDecision") == "deny", "unparseable marker sources must fail closed"


def test_historical_display_clear_never_shields_a_current_key(tmp_path: Path) -> None:
    migration = migration_fixture(tmp_path)
    append_audit(migration, "block-marker-only", "sources=['A']")
    append_audit(migration, "probe-cleared", "probe-cleared: DATA_OK from A")
    write_marker(tmp_path / MARKER, source_keys(migration))
    assert hook_for_model_write(tmp_path, migration).get("permissionDecision") == "deny"
