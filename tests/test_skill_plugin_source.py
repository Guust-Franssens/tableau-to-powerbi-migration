"""Tests for content-based discovery of the installed reusable Power BI skill plugin."""
# pylint: disable=wrong-import-position

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_plugin  # noqa: E402
from build_plugin import SHIPPED_SKILLS  # noqa: E402
from skill_plugin_source import PLUGIN_ROOT_ENV, discover_skill_plugin  # noqa: E402


def _make_plugin(root: Path, marketplace: str, plugin: str, skills: tuple[str, ...] = SHIPPED_SKILLS) -> Path:
    """Create a fake installed plugin tree with minimal skill markers."""
    plugin_root = root / marketplace / plugin
    for skill in skills:
        skill_dir = plugin_root / "skills" / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
    return plugin_root


def test_single_install_found_by_bundle_content(tmp_path: Path) -> None:
    """The installed plugin is discovered from shipped skill folders, not a hard-coded name."""
    plugin_root = _make_plugin(tmp_path, "renamed-collection", "renamed-plugin")

    result = discover_skill_plugin(installed_plugins_root=tmp_path, env={})

    assert result.ok
    assert result.plugin_root == plugin_root
    assert result.skills_dir == plugin_root / "skills"
    assert result.identity == "renamed-plugin@renamed-collection"


def test_multiple_installs_fail_loudly_and_name_both_paths(tmp_path: Path) -> None:
    """Two installed copies are a shadowing hazard; discovery must not pick one silently."""
    first = _make_plugin(tmp_path, "collection-a", "plugin-a", skills=(SHIPPED_SKILLS[0],))
    second = _make_plugin(tmp_path, "collection-b", "plugin-b", skills=(SHIPPED_SKILLS[1],))

    result = discover_skill_plugin(installed_plugins_root=tmp_path, env={})

    assert result.status == "multiple"
    assert not result.ok
    assert result.candidates == (first, second)
    assert str(first) in result.detail
    assert str(second) in result.detail


def test_no_install_returns_actionable_install_hint(tmp_path: Path) -> None:
    """No installed copy is reported clearly without mutating the plugin directory.

    The identity is read from `build_plugin` rather than spelled out here. This test previously
    hard-coded `powerbi-migration-skills@powerbi-migration-collection`, which is the name the plugin
    was published under before v0.3.0 - so it pinned a dead install command in a hint whose whole job
    is to be copy-pasteable.
    """
    result = discover_skill_plugin(installed_plugins_root=tmp_path, env={})

    assert result.status == "missing"
    assert not result.ok
    assert "copilot plugin install" in result.install_hint
    assert f"{build_plugin.PLUGIN_NAME}@{build_plugin.MARKETPLACE_NAME}" in result.install_hint


def test_explicit_override_is_honoured_even_when_scan_would_be_ambiguous(tmp_path: Path) -> None:
    """A future rename can be handled immediately with an override instead of a code change."""
    _make_plugin(tmp_path, "collection-a", "plugin-a")
    override = _make_plugin(tmp_path, "collection-b", "plugin-b")

    result = discover_skill_plugin(installed_plugins_root=tmp_path, plugin_root_override=override, env={})

    assert result.ok
    assert result.override
    assert result.plugin_root == override
    assert result.identity == "plugin-b@collection-b"


def test_env_override_accepts_a_skills_directory(tmp_path: Path) -> None:
    """The environment override is convenient for operators who copy the path from a drift message."""
    plugin_root = _make_plugin(tmp_path, "collection", "plugin")

    result = discover_skill_plugin(
        installed_plugins_root=tmp_path / "empty", env={PLUGIN_ROOT_ENV: str(plugin_root / "skills")}
    )

    assert result.ok
    assert result.override
    assert result.plugin_root == plugin_root
