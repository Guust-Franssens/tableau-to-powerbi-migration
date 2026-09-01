"""Tests for PROVENANCE-based discovery of the installed reusable Power BI skill plugin.

Discovery used to select "any installed plugin carrying any bundle from the inventory", and
`sync_installed_skills.py` then WROTE into whatever it selected. Issue #410 round-2 review measured
what that costs: a foreign plugin that merely shared one bundle name was overwritten and a file
inside it deleted, and under `--from-worktree` a bundle invented on a branch could point the publish
at an unrelated plugin entirely. Content is exactly the wrong evidence, because content is what a
feature branch - or anyone who can drop a directory into `~/.copilot/installed-plugins` - controls.

**Content-based SELECTION is therefore gone on purpose, not by accident**, and two tests here used to
pin it: `test_single_install_found_by_bundle_content` asserted that an arbitrarily named plugin was
discovered from its bundles alone, and `test_multiple_installs_fail_loudly_and_name_both_paths`
asserted that two such content-only plugins were reported as `multiple`. Both are rewritten below
rather than dropped, and each keeps the coverage it was there for:

* discovery can still FIND an install - now via a PROOF (`explicit` / `marker` / `identity`);
* an ambiguous multi-install is still reported loudly and names every path - now when more than one
  destination is PROVEN, which is the only case in which a wrong pick could destroy anything;
* and the removed behaviour is pinned in its own right: content now yields `unproven`, which names
  the look-alike and writes nothing.
"""
# pylint: disable=wrong-import-position

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_plugin  # noqa: E402
from build_plugin import SHIPPED_SKILLS  # noqa: E402
from skill_plugin_source import PLUGIN_ROOT_ENV, discover_skill_plugin, write_owner_marker  # noqa: E402

OURS = (build_plugin.MARKETPLACE_NAME, build_plugin.PLUGIN_NAME)


def _make_plugin(root: Path, marketplace: str, plugin: str, skills: tuple[str, ...] = SHIPPED_SKILLS) -> Path:
    """Create a fake installed plugin tree with minimal skill markers."""
    plugin_root = root / marketplace / plugin
    for skill in skills:
        skill_dir = plugin_root / "skills" / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
    return plugin_root


def _registry(path: Path, entries: dict[Path, tuple[str, str]]) -> Path:
    """Write a Copilot-CLI-shaped `config.json` naming each plugin root's installed identity."""
    path.write_text(
        json.dumps(
            {
                "installedPlugins": [
                    {"name": plugin, "marketplace": marketplace, "cache_path": str(root)}
                    for root, (marketplace, plugin) in entries.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_bundle_content_alone_never_selects_a_destination(tmp_path: Path) -> None:
    """The removed behaviour, pinned: carrying EVERY shipped bundle is still not ownership.

    This is the exact tree `test_single_install_found_by_bundle_content` used to accept - a plugin
    under a name this repo has never published - and accepting it is what let a stranger be picked
    and written to. The verdict must name it (so an operator can adopt it with `--plugin-root`) and
    must refuse to hand a caller anywhere to write.
    """
    lookalike = _make_plugin(tmp_path, "renamed-collection", "renamed-plugin")

    result = discover_skill_plugin(installed_plugins_root=tmp_path, env={}, registry=tmp_path / "no-config.json")

    assert result.status == "unproven"
    assert not result.ok
    assert result.plugin_root is None, "an unproven verdict must not name a destination at all"
    assert result.skills_dir is None
    assert result.proof is None
    assert result.candidates == (lookalike,)
    assert str(lookalike) in result.detail
    assert "--plugin-root" in result.install_hint


def test_provenance_finds_the_install_that_content_may_not(tmp_path: Path) -> None:
    """Discovery still resolves a plugin root, skills dir and identity - once ownership is PROVED.

    Both non-operator proofs are exercised on the SAME tree the test above refuses, because the
    replacement has to carry the "it can find an install" coverage that content-matching provided:

    * ``identity`` - the CLI's own install record names it (this survives the rename that
      `build_plugin.KNOWN_PLUGIN_IDENTITIES` exists to remember);
    * ``marker``   - a previous publish left this tool's own provenance record inside it.
    """
    plugin_root = _make_plugin(tmp_path, "renamed-collection", "renamed-plugin")
    registry = _registry(tmp_path / "config.json", {plugin_root: ("renamed-collection", "renamed-plugin")})

    by_identity = discover_skill_plugin(
        installed_plugins_root=tmp_path,
        env={},
        identities=["renamed-plugin@renamed-collection"],
        registry=registry,
    )

    assert by_identity.ok
    assert by_identity.proof == "identity"
    assert by_identity.plugin_root == plugin_root
    assert by_identity.skills_dir == plugin_root / "skills"
    assert by_identity.identity == "renamed-plugin@renamed-collection"

    write_owner_marker(plugin_root, publish_repo="https://example.invalid/repo", bundles=list(SHIPPED_SKILLS))
    by_marker = discover_skill_plugin(
        installed_plugins_root=tmp_path,
        env={},
        publish_repo="https://example.invalid/repo",
        registry=tmp_path / "no-config.json",
    )

    assert by_marker.ok
    assert by_marker.proof == "marker"
    assert by_marker.plugin_root == plugin_root
    assert by_marker.skills_dir == plugin_root / "skills"


def test_the_cli_registry_is_JSONC_and_must_still_prove_ownership(tmp_path: Path) -> None:
    """Measured on a real machine: `~/.copilot/config.json` OPENS with a `//` comment line.

    Its first line is `// User settings belong in settings.json`, so a strict `json.loads` raises on
    character 0 and `registry_identities` - which degrades an unreadable registry to "no entries" -
    returned `{}` every time. The `identity`-by-REGISTRY proof therefore never fired on any real
    install, silently, and only failed CLOSED (fewer proofs, never more writes), which is why
    nothing noticed. The bug is not academic here: this machine's plugin lives at
    `installed-plugins/powerbi-migration-collection/powerbi-migration-skills` and the registry names
    it correctly.

    The install below is deliberately at a directory whose LAYOUT proves nothing - `abc123@cache` is
    in no allowlist - so the registry is the only possible proof and a fixture writing strict JSON
    could not tell the two apart.
    """
    plugin_root = _make_plugin(tmp_path, "cache", "abc123")
    registry = tmp_path / "config.json"
    registry.write_text(
        "// User settings belong in settings.json\n"
        + json.dumps(
            {
                "installedPlugins": [
                    {
                        "name": build_plugin.PLUGIN_NAME,
                        "marketplace": build_plugin.MARKETPLACE_NAME,
                        "cache_path": str(plugin_root),
                        "version": "0.3.0",
                    }
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result = discover_skill_plugin(installed_plugins_root=tmp_path, env={}, registry=registry)

    assert result.ok, "a JSONC registry is the real format, so it must be readable"
    assert result.proof == "identity"
    assert result.plugin_root == plugin_root


def test_a_registry_naming_someone_else_still_refuses(tmp_path: Path) -> None:
    """Reading the registry must not become a way to be MORE permissive.

    The registry is the CLI's own record and outranks the directory name: when it says this
    directory is a plugin nobody declared, a layout that happens to look like ours is not ownership.
    """
    plugin_root = _make_plugin(tmp_path, *OURS)
    registry = tmp_path / "config.json"
    registry.write_text(
        "// jsonc\n"
        + json.dumps(
            {
                "installedPlugins": [
                    {"name": "their-plugin", "marketplace": "someone-else", "cache_path": str(plugin_root)}
                ]
            }
        ),
        encoding="utf-8",
    )

    result = discover_skill_plugin(installed_plugins_root=tmp_path, env={}, registry=registry)

    assert result.status == "unproven"
    assert not result.ok


def test_multiple_proven_installs_fail_loudly_and_name_both_paths(tmp_path: Path) -> None:
    """Two PROVEN copies are a shadowing hazard; discovery must not pick one silently.

    The successor to `test_multiple_installs_fail_loudly_and_name_both_paths`. What changed is only
    which installs count: a shadowing hazard is now two destinations this repo can prove it owns -
    one by its published identity, one by the marker a previous publish stamped - because those are
    the only two a publish could have written to.
    """
    by_identity = _make_plugin(tmp_path, *OURS, skills=(SHIPPED_SKILLS[0],))
    by_marker = _make_plugin(tmp_path, "collection-b", "plugin-b", skills=(SHIPPED_SKILLS[1],))
    write_owner_marker(by_marker, publish_repo=build_plugin.PUBLISH_REPO, bundles=[SHIPPED_SKILLS[1]])

    result = discover_skill_plugin(installed_plugins_root=tmp_path, env={}, registry=tmp_path / "no-config.json")

    assert result.status == "multiple"
    assert not result.ok
    assert result.plugin_root is None, "an ambiguous verdict must not name a destination either"
    assert sorted(result.candidates) == sorted([by_identity, by_marker])
    assert str(by_identity) in result.detail
    assert str(by_marker) in result.detail


def test_two_unproven_lookalikes_are_unproven_rather_than_multiple(tmp_path: Path) -> None:
    """The behaviour change itself: sharing bundle names is not even enough to be AMBIGUOUS.

    `multiple` says "pick one of these" and its hint tells the operator to delete a copy; saying
    that about two strangers would invite deleting someone else's plugin. Both are still named, so
    nothing is hidden - they are reported as look-alikes rather than as candidates to write to.
    """
    first = _make_plugin(tmp_path, "collection-a", "plugin-a", skills=(SHIPPED_SKILLS[0],))
    second = _make_plugin(tmp_path, "collection-b", "plugin-b", skills=(SHIPPED_SKILLS[1],))

    result = discover_skill_plugin(installed_plugins_root=tmp_path, env={}, registry=tmp_path / "no-config.json")

    assert result.status == "unproven"
    assert not result.ok
    assert result.plugin_root is None
    assert sorted(result.candidates) == sorted([first, second])
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
