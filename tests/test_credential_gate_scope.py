"""`credential_gate.py block` must refuse a target whose subtree is broader than one unit of work.

From a real incident, 2026-08-18: the command was run from the wrong working directory and wrote its
marker at the REPO ROOT. `scripts/hooks/credential_gate.py:_blocking_marker()` walks UPWARD from any
write target and returns the first marker it meets, so that single file governed every migration in
the checkout at once - blocking ~13 unrelated in-flight agents, including bundles that had already
independently earned their clearance, and stranding a live unsaved DAX measure in a Desktop session
with nowhere to write.

Nothing refused it, because `apply_block` accepted any directory at all. These tests pin the guard.

The escape hatch is tested too: an unusual-but-legitimate layout must stay workable via
`--force-scope`, or the guard just gets removed by the next person it inconveniences.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import credential_gate  # noqa: E402  # pylint: disable=wrong-import-position


def _migration(root: Path) -> Path:
    """A directory that legitimately IS one unit of work."""
    d = root / "mig"
    d.mkdir()
    (d / "migration-spec.json").write_text(json.dumps({"schema": "x"}), encoding="utf-8")
    return d


def test_a_git_checkout_root_is_refused(tmp_path):
    (tmp_path / ".git").mkdir()
    assert credential_gate.apply_block(tmp_path, ["snowflake"]) == 2
    assert not (tmp_path / credential_gate.MARKER).exists(), "nothing may be written on refusal"


@pytest.mark.parametrize("sign", ["AGENTS.md", "pyproject.toml"])
def test_a_checkout_exported_without_git_is_still_refused(tmp_path, sign):
    (tmp_path / sign).write_text("x", encoding="utf-8")
    assert credential_gate.apply_block(tmp_path, ["snowflake"]) == 2
    assert not (tmp_path / credential_gate.MARKER).exists()


def test_a_bare_directory_with_no_scope_marker_is_refused(tmp_path):
    # The shape that actually caused the incident: an ancestor holding migrations but owning none.
    _migration(tmp_path)
    assert credential_gate.apply_block(tmp_path, ["snowflake"]) == 2
    assert not (tmp_path / credential_gate.MARKER).exists()


def test_the_child_migration_is_untouched_when_its_parent_is_refused(tmp_path):
    migration = _migration(tmp_path)
    credential_gate.apply_block(tmp_path, ["snowflake"])
    assert not (migration / credential_gate.MARKER).exists()


def test_a_real_migration_directory_is_still_armed(tmp_path):
    migration = _migration(tmp_path)
    assert credential_gate.apply_block(migration, ["snowflake"]) == 0
    assert (migration / credential_gate.MARKER).is_file(), "the guard must not break the normal flow"


def test_an_engine_bundle_is_a_valid_target(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / credential_gate.ENGINE_RECEIPT).write_text("{}", encoding="utf-8")
    assert credential_gate.apply_block(bundle, ["snowflake"]) == 0
    assert (bundle / credential_gate.MARKER).is_file()


def test_force_scope_still_allows_a_deliberate_broad_block(tmp_path):
    _migration(tmp_path)
    assert credential_gate.apply_block(tmp_path, ["snowflake"], force_scope=True) == 0
    assert (tmp_path / credential_gate.MARKER).is_file()


def test_the_refusal_is_reported_before_any_side_effect(tmp_path):
    (tmp_path / ".git").mkdir()
    credential_gate.apply_block(tmp_path, ["snowflake"])
    audit = tmp_path / credential_gate.AUDIT
    if audit.exists():
        assert "block" not in audit.read_text(encoding="utf-8"), "a refused block must not log as armed"
