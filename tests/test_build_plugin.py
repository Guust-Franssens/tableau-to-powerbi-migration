"""Regression tests for the marketplace build (`scripts/build_plugin.py`).

The published plugin is what makes a skill's *name* resolve inside a custom-agent subagent - a
repo-local `.github/skills/` bundle does not (see `docs/agent-architecture.md` section 6.1). So a
silently broken build is a silently broken persona instruction, which is why these run in CI even
though the artifact itself is gitignored.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_plugin  # noqa: E402  (needs the sys.path insert above)


@pytest.fixture(name="built")
def _built(tmp_path: Path) -> Path:
    """A full marketplace tree, built into a temp dir."""
    out = tmp_path / "marketplace"
    build_plugin.build(out)
    return out


def test_every_shipped_skill_actually_exists_in_this_repo() -> None:
    """Renaming or deleting a bundle must fail the build loudly, not publish a broken plugin."""
    for name in build_plugin.SHIPPED_SKILLS:
        skill = build_plugin.SKILLS_DIR / name / "SKILL.md"
        assert skill.is_file(), f"{name} is in SHIPPED_SKILLS but {skill} does not exist"


def test_the_manifest_is_the_shape_copilot_reads(built: Path) -> None:
    """`.claude-plugin/marketplace.json` must match the contract, mirroring microsoft/skills-for-fabric."""
    manifest = json.loads((built / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))

    assert manifest["name"] == build_plugin.MARKETPLACE_NAME
    assert len(manifest["plugins"]) == 1

    plugin = manifest["plugins"][0]
    assert plugin["name"] == build_plugin.PLUGIN_NAME
    assert plugin["source"] == f"./plugins/{build_plugin.PLUGIN_NAME}"
    for key in ("description", "version", "skills", "repository", "license"):
        assert plugin[key], f"plugin.{key} must not be empty"


def test_every_skill_the_manifest_declares_is_present_at_that_path(built: Path) -> None:
    """A declared-but-missing skill path is the exact failure that yields a plugin that installs empty."""
    manifest = json.loads((built / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugin = manifest["plugins"][0]
    source = built / Path(plugin["source"])

    for declared in plugin["skills"]:
        resolved = (source / Path(declared)).resolve()
        assert resolved.is_dir(), f"manifest declares {declared} but {resolved} is not a directory"
        assert (resolved / "SKILL.md").is_file(), f"{declared} has no SKILL.md"


def test_each_published_skill_travels_with_its_scripts_and_tests(built: Path) -> None:
    """The copy-one-folder promise: a consumer installs the whole bundle, not just its markdown."""
    skills = built / "plugins" / build_plugin.PLUGIN_NAME / "skills"
    for name in build_plugin.SHIPPED_SKILLS:
        assert (skills / name / "scripts").is_dir(), f"{name} published without its scripts/"
        assert (skills / name / "tests").is_dir(), f"{name} published without its tests/"


def test_no_build_artifacts_are_published(built: Path) -> None:
    """`__pycache__` in a published plugin is noise at best and stale bytecode at worst."""
    junk = [p for p in built.rglob("*") if p.name in {"__pycache__", ".pytest_cache"} or p.suffix == ".pyc"]
    assert not junk, f"build published artifacts: {[str(p.relative_to(built)) for p in junk]}"


def test_the_diagnostic_probe_skill_is_never_published() -> None:
    """`sentinel-probe` is a context-visibility experiment, not something a consumer should install."""
    assert "sentinel-probe" not in build_plugin.SHIPPED_SKILLS


def test_check_detects_drift(tmp_path: Path) -> None:
    """`--check` must fail when the published tree no longer matches the bundles, or it gates nothing."""
    out = tmp_path / "marketplace"
    build_plugin.build(out)
    assert build_plugin.main(["--out", str(out), "--check"]) == 0

    skill = build_plugin.SHIPPED_SKILLS[0]
    (out / "plugins" / build_plugin.PLUGIN_NAME / "skills" / skill / "SKILL.md").write_text("drifted", encoding="utf-8")
    assert build_plugin.main(["--out", str(out), "--check"]) == 1
