"""Tests for scripts/check_replay_manifest.py (issue #259).

`declare_generated_edit.py` writes a drift declaration only when a fix script's target actually
changed hash; the replay-manifest registration it ALSO writes is unconditional, so it stays
discoverable even on a `DECLARE: NO_CHANGE` re-run. This gate is the enforcement half: a
`_build/*.py` replay script with no registration, or a registration with no script, must fail -
and an entirely absent manifest directory must fail LOUDLY rather than read as "nothing to check",
which is the exact trap issue #259 names.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_replay_manifest.py"
DECLARE_SCRIPT = REPO / "scripts" / "declare_generated_edit.py"

spec = importlib.util.spec_from_file_location("check_replay_manifest", SCRIPT)
crm_mod = importlib.util.module_from_spec(spec)
sys.modules["check_replay_manifest"] = crm_mod
spec.loader.exec_module(crm_mod)


def _write_registration(bundle: Path, script_identity: str, **overrides) -> Path:
    record = {
        "version": 1,
        "run_id": "run-1",
        "script_identity": script_identity,
        "script_sha256": "deadbeef",
        "purpose": "a registered replay script",
        "order_independent": True,
        "depends_on": [],
        "recorded_at": "2026-08-20T00:00:00+00:00",
    }
    record.update(overrides)
    manifest_dir = bundle / "_build" / "replay-manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / f"{script_identity.replace('/', '_')}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def _write_script(bundle: Path, relative: str) -> Path:
    path = bundle / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# a replay script\n", encoding="utf-8")
    return path


def test_registered_and_consistent_is_clean(tmp_path):
    _write_script(tmp_path, "_build/fix_orders.py")
    _write_registration(tmp_path, "_build/fix_orders.py")

    state, notes = crm_mod.check(tmp_path)

    assert state == "CLEAN"
    assert "1 replay script" in notes[0]


def test_no_scripts_and_no_manifest_is_clean(tmp_path):
    """A bundle with nothing to register has nothing to fail on - this is NOT the absent-manifest trap."""
    state, notes = crm_mod.check(tmp_path)

    assert state == "CLEAN"
    assert notes == ["0 replay script(s) registered and consistent"]


def test_script_with_no_registration_is_a_violation(tmp_path):
    _write_script(tmp_path, "_build/fix_undeclared.py")
    _write_script(tmp_path, "_build/fix_declared.py")
    _write_registration(tmp_path, "_build/fix_declared.py")

    state, notes = crm_mod.check(tmp_path)

    assert state == "VIOLATIONS"
    assert any("UNREGISTERED: _build/fix_undeclared.py" in note for note in notes)


def test_registration_with_no_script_is_a_violation(tmp_path):
    _write_registration(tmp_path, "_build/fix_ghost.py")

    state, notes = crm_mod.check(tmp_path)

    assert state == "VIOLATIONS"
    assert any("DANGLING: _build/fix_ghost.py" in note for note in notes)


def test_manifest_absent_entirely_is_distinct_from_clean(tmp_path):
    """The trap: scripts exist, the manifest directory was never created. Must NOT read as CLEAN."""
    _write_script(tmp_path, "_build/fix_orders.py")
    _write_script(tmp_path, "_build/subdir/fix_customers.py")
    assert not (tmp_path / "_build" / "replay-manifest").exists()

    state, notes = crm_mod.check(tmp_path)

    assert state == "MANIFEST_ABSENT"
    assert any("does not exist" in note for note in notes)
    assert any("_build/fix_orders.py" in note for note in notes)
    assert any("_build/subdir/fix_customers.py" in note for note in notes)


def test_unknown_dependency_is_a_violation(tmp_path):
    _write_script(tmp_path, "_build/fix_a.py")
    _write_registration(tmp_path, "_build/fix_a.py", order_independent=False, depends_on=["_build/fix_missing.py"])

    state, notes = crm_mod.check(tmp_path)

    assert state == "VIOLATIONS"
    assert any("UNKNOWN DEPENDENCY" in note and "_build/fix_missing.py" in note for note in notes)


def test_dependency_cycle_is_a_violation(tmp_path):
    _write_script(tmp_path, "_build/fix_a.py")
    _write_script(tmp_path, "_build/fix_b.py")
    _write_registration(tmp_path, "_build/fix_a.py", order_independent=False, depends_on=["_build/fix_b.py"])
    _write_registration(tmp_path, "_build/fix_b.py", order_independent=False, depends_on=["_build/fix_a.py"])

    state, notes = crm_mod.check(tmp_path)

    assert state == "VIOLATIONS"
    assert any("DEPENDENCY CYCLE" in note for note in notes)


def test_a_satisfied_dependency_chain_is_clean(tmp_path):
    _write_script(tmp_path, "_build/fix_a.py")
    _write_script(tmp_path, "_build/fix_b.py")
    _write_registration(tmp_path, "_build/fix_a.py", order_independent=False, depends_on=[])
    _write_registration(tmp_path, "_build/fix_b.py", order_independent=False, depends_on=["_build/fix_a.py"])

    state, _notes = crm_mod.check(tmp_path)

    assert state == "CLEAN"


def test_cli_exit_codes(tmp_path):
    _write_script(tmp_path, "_build/fix_orders.py")
    _write_registration(tmp_path, "_build/fix_orders.py")
    clean = subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle", str(tmp_path)], capture_output=True, text=True, check=False
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr

    (tmp_path / "_build" / "fix_undeclared.py").write_text("# orphan\n", encoding="utf-8")
    violations = subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle", str(tmp_path)], capture_output=True, text=True, check=False
    )
    assert violations.returncode == 1

    no_bundle = subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle", str(tmp_path / "does-not-exist")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert no_bundle.returncode == 2


def test_cli_manifest_absent_exits_three(tmp_path):
    _write_script(tmp_path, "_build/fix_orders.py")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert payload["state"] == "MANIFEST_ABSENT"


def test_declare_wrapper_registers_even_on_no_change(tmp_path):
    """The gap issue #259 measured directly: a NO_CHANGE re-run must still register the script."""
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    (tmp_path / target).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / target).write_text("already fixed", encoding="utf-8")
    (tmp_path / "input_manifest.json").write_text(
        json.dumps({"generated_artifacts": {"version": 1, "run_id": "run-1", "files": {target: "irrelevant"}}}),
        encoding="utf-8",
    )
    fix = tmp_path / "_build" / "fix_orders.py"
    fix.parent.mkdir(parents=True, exist_ok=True)
    fix.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                f"p = Path.cwd() / {target!r}",
                "p.write_text('already fixed', encoding='utf-8')",  # idempotent - no change
            ]
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(DECLARE_SCRIPT),
            "--bundle",
            str(tmp_path),
            "--target",
            target,
            "--script",
            str(fix),
            "--purpose",
            "fix orders tmdl",
            "--order-independent",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "NO_CHANGE" in proc.stdout

    state, notes = crm_mod.check(tmp_path)
    assert state == "CLEAN", notes


def test_declare_wrapper_requires_an_order_claim(tmp_path):
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    (tmp_path / target).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / target).write_text("x", encoding="utf-8")
    fix = tmp_path / "_build" / "fix_orders.py"
    fix.parent.mkdir(parents=True, exist_ok=True)
    fix.write_text("pass\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(DECLARE_SCRIPT),
            "--bundle",
            str(tmp_path),
            "--target",
            target,
            "--script",
            str(fix),
            "--purpose",
            "fix orders tmdl",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "--order-independent" in proc.stderr or "required" in proc.stderr.lower()
