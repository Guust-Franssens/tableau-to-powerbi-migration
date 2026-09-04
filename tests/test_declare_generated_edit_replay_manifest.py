"""Tests for the NAVIGATIONAL replay-manifest row `declare_generated_edit.py` writes (issue #259).

Scope, deliberately bounded per review: this manifest exists so a human can FIND the replay script
behind a declared edit. It is not a verification gate - there is no checker script, no schema
enforcement, no hashing, and no cross-check against the drift-declaration directory. These tests
prove the writer contract: declaring an edit writes/updates one navigational row, repeated
declaration is deterministic (no accumulating duplicates, and no collisions between distinct
targets that sanitize to the same stem), the authoritative generated-edit declaration always takes
precedence over the best-effort manifest write, and the existing generated-edit declaration
workflow (tamper-detection hashes) is unchanged.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DECLARE_SCRIPT = REPO / "scripts" / "declare_generated_edit.py"

sys.path.insert(0, str(REPO / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
from generated_edit_declarations import REPLAY_MANIFEST_DIR, load_replay_registrations, write_replay_registration


def _write_bundle(bundle: Path, target: str, target_contents: str) -> Path:
    target_path = bundle / target
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(target_contents, encoding="utf-8")
    (bundle / "input_manifest.json").write_text(
        json.dumps(
            {
                "generated_artifacts": {
                    "version": 1,
                    "run_id": "run-1",
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                    "files": {target: "irrelevant"},
                }
            }
        ),
        encoding="utf-8",
    )
    return target_path


def _write_fix_script(bundle: Path, target: str, new_contents: str) -> Path:
    fix = bundle / "_build" / "fix_target.py"
    fix.parent.mkdir(parents=True, exist_ok=True)
    fix.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                f"p = Path.cwd() / {target!r}",
                f"p.write_text({new_contents!r}, encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )
    return fix


def _declare(bundle: Path, target: str, script: Path, purpose: str | None = None) -> subprocess.CompletedProcess:
    args = [
        sys.executable,
        str(DECLARE_SCRIPT),
        "--bundle",
        str(bundle),
        "--target",
        target,
        "--script",
        str(script),
    ]
    if purpose is not None:
        args.extend(["--purpose", purpose])
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _manifest_rows(bundle: Path) -> list[dict]:
    manifest_dir = bundle / "_build" / "replay-manifest"
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(manifest_dir.glob("*.json"))]


def test_declaring_an_edit_writes_one_navigational_row_with_human_readable_fields(tmp_path):
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    _write_bundle(tmp_path, target, "original")
    fix = _write_fix_script(tmp_path, target, "fixed")

    proc = _declare(tmp_path, target, fix, purpose="fix Orders.tmdl date formatting")
    assert proc.returncode == 0, proc.stderr

    rows = _manifest_rows(tmp_path)
    assert len(rows) == 1
    (row,) = rows
    assert row["target"] == target
    assert row["script_identity"] == "_build/fix_target.py"
    assert row["purpose"] == "fix Orders.tmdl date formatting"
    assert row["run_id"] == "run-1"
    assert "recorded_at" in row


def test_purpose_is_optional_for_backward_compatible_callers(tmp_path):
    """`.github/agents/pbi-semantic-builder.agent.md` (outside this agent's sandbox) documents an
    invocation with no `--purpose`; the wrapper must keep accepting it."""
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    _write_bundle(tmp_path, target, "original")
    fix = _write_fix_script(tmp_path, target, "fixed")

    proc = _declare(tmp_path, target, fix, purpose=None)
    assert proc.returncode == 0, proc.stderr

    (row,) = _manifest_rows(tmp_path)
    assert row["purpose"] == ""


def test_repeated_declaration_of_the_same_target_is_idempotent(tmp_path):
    """A re-run (e.g. the idempotent `DECLARE: NO_CHANGE` path) must update its own row, never
    accumulate a second, contradictory one for the same declared edit."""
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    _write_bundle(tmp_path, target, "already fixed")
    fix = _write_fix_script(tmp_path, target, "already fixed")  # idempotent - no change

    first = _declare(tmp_path, target, fix, purpose="fix orders tmdl")
    assert first.returncode == 0, first.stderr
    assert "NO_CHANGE" in first.stdout

    second = _declare(tmp_path, target, fix, purpose="fix orders tmdl, re-run")
    assert second.returncode == 0, second.stderr

    rows = _manifest_rows(tmp_path)
    assert len(rows) == 1, "a re-declaration of the same target must not accumulate a second row"
    assert rows[0]["purpose"] == "fix orders tmdl, re-run", "the row reflects the latest declaration"


def test_existing_generated_edit_declaration_workflow_is_unchanged(tmp_path):
    """The drift-declaration directory (tamper detection) is untouched by the manifest change: a
    real edit still produces exactly one declaration file with correct before/after hashes."""
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    _write_bundle(tmp_path, target, "original")
    fix = _write_fix_script(tmp_path, target, "fixed")

    proc = _declare(tmp_path, target, fix, purpose="fix orders tmdl")
    assert proc.returncode == 0, proc.stderr
    assert "DECLARE: RECORDED" in proc.stdout

    declarations_dir = tmp_path / "_build" / "generated-edit-declarations"
    declaration_files = list(declarations_dir.glob("*.json"))
    assert len(declaration_files) == 1
    declaration = json.loads(declaration_files[0].read_text(encoding="utf-8"))
    assert declaration["target"] == target
    assert declaration["kind"] == "changed"
    assert declaration["baseline_sha256"] != declaration["expected_sha256"]


def test_distinct_targets_that_sanitize_to_the_same_stem_do_not_collide(tmp_path):
    """`A/B.tmdl` and `A_B.tmdl` both sanitize their separator to `_`, so a stem-only key would
    collide them onto one row. The digest suffix must keep them as two distinct rows, while a
    genuine repeat of either target still lands back on its own single row."""
    write_replay_registration(
        tmp_path, {"target": "A/B.tmdl", "script_identity": "_build/fix_a.py", "purpose": "fix a", "run_id": "run-1"}
    )
    write_replay_registration(
        tmp_path, {"target": "A_B.tmdl", "script_identity": "_build/fix_b.py", "purpose": "fix b", "run_id": "run-1"}
    )

    rows = load_replay_registrations(tmp_path)
    assert len(rows) == 2, "two distinct targets sanitizing to the same stem must not overwrite each other"
    by_target = {row["target"]: row for row in rows}
    assert by_target["A/B.tmdl"]["script_identity"] == "_build/fix_a.py"
    assert by_target["A_B.tmdl"]["script_identity"] == "_build/fix_b.py"

    manifest_dir = tmp_path / REPLAY_MANIFEST_DIR
    assert len(list(manifest_dir.glob("*.json"))) == 2

    # Repeating the SAME target remains idempotent: it still updates its own row, not a new one.
    write_replay_registration(
        tmp_path,
        {"target": "A/B.tmdl", "script_identity": "_build/fix_a.py", "purpose": "fix a, re-run", "run_id": "run-1"},
    )
    rows = load_replay_registrations(tmp_path)
    assert len(rows) == 2, "re-declaring one of the two targets must not add a third row"
    by_target = {row["target"]: row for row in rows}
    assert by_target["A/B.tmdl"]["purpose"] == "fix a, re-run"


def test_navigational_write_failure_never_costs_the_authoritative_declaration(tmp_path):
    """The generated-edit declaration is authoritative; the replay-manifest row is not. If the
    manifest write fails (simulated here by pre-occupying its directory path with a plain file, so
    `mkdir` raises), the authoritative declaration must already be persisted - never zero
    declarations for a real, hash-changing edit."""
    target = "M.SemanticModel/definition/tables/Orders.tmdl"
    _write_bundle(tmp_path, target, "original")
    fix = _write_fix_script(tmp_path, target, "fixed")

    manifest_path = tmp_path / REPLAY_MANIFEST_DIR
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("not a directory", encoding="utf-8")  # blocks REPLAY_MANIFEST_DIR's mkdir

    proc = _declare(tmp_path, target, fix, purpose="fix orders tmdl")
    assert proc.returncode == 0, proc.stderr
    assert "DECLARE: RECORDED" in proc.stdout
    assert "navigational replay-manifest write failed" in proc.stderr

    declarations_dir = tmp_path / "_build" / "generated-edit-declarations"
    declaration_files = list(declarations_dir.glob("*.json"))
    assert len(declaration_files) == 1, "the authoritative declaration must exist despite the manifest write failure"
    declaration = json.loads(declaration_files[0].read_text(encoding="utf-8"))
    assert declaration["kind"] == "changed"
