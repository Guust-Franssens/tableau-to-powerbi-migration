"""Regression tests for the marketplace build (`scripts/build_plugin.py`).

The published plugin is what makes a skill's *name* resolve inside a custom-agent subagent - a
repo-local `.github/skills/` bundle does not (see `docs/agent-architecture.md` section 6.1). So a
silently broken build is a silently broken persona instruction, which is why these run in CI even
though the artifact itself is gitignored.
"""

from __future__ import annotations

import json
import re
import stat
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
    """The copy-one-folder promise: a consumer installs the whole bundle, not just its markdown.

    Conditional on the SOURCE bundle actually shipping code, because not every bundle does: the two
    gotcha catalogues are pure knowledge (no scripts, so nothing to gate). Asserting unconditionally
    would forbid a documentation-only bundle, and asserting nothing would let a build silently drop
    the `scripts/` of a bundle that has them - which is the failure this test exists to catch.
    """
    skills = built / "plugins" / build_plugin.PLUGIN_NAME / "skills"
    carried = 0
    for name in build_plugin.SHIPPED_SKILLS:
        source = build_plugin.SKILLS_DIR / name
        assert (skills / name / "SKILL.md").is_file(), f"{name} published without its SKILL.md"
        for folder in ("scripts", "tests"):
            if (source / folder).is_dir():
                assert (skills / name / folder).is_dir(), f"{name} published without its {folder}/"
                carried += 1
    assert carried, "no shipped bundle carries scripts/ or tests/ - this test now proves nothing"


def test_no_build_artifacts_are_published(built: Path) -> None:
    """`__pycache__` in a published plugin is noise at best and stale bytecode at worst."""
    junk = [p for p in built.rglob("*") if p.name in {"__pycache__", ".pytest_cache"} or p.suffix == ".pyc"]
    assert not junk, f"build published artifacts: {[str(p.relative_to(built)) for p in junk]}"


def test_the_diagnostic_probe_skill_is_never_published() -> None:
    """`sentinel-probe` is a context-visibility experiment, not something a consumer should install."""
    assert "sentinel-probe" not in build_plugin.SHIPPED_SKILLS


def test_the_plugin_carries_its_own_manifest_for_claude_code(built: Path) -> None:
    """Claude Code needs `plugins/<name>/.claude-plugin/plugin.json`; Copilot CLI does not.

    That asymmetry is the whole hazard. The root marketplace.json is the *catalogue*, and both
    clients read it - so a marketplace missing the per-plugin manifest still `marketplace add`s
    cleanly in both, installs fine in Copilot CLI, and simply is not recognised by Claude Code.
    v0.3.0 shipped exactly that way. Nothing in the build output differed, because from Copilot's
    point of view nothing was wrong.

    `microsoft/skills-for-fabric` ships both files per plugin, which is the working reference.
    """
    manifest_path = built / "plugins" / build_plugin.PLUGIN_NAME / ".claude-plugin" / "plugin.json"
    assert manifest_path.is_file(), "Claude Code will not recognise a plugin without its own plugin.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == build_plugin.PLUGIN_NAME
    assert manifest["version"] == build_plugin.VERSION

    # The skill paths are relative to the plugin directory, so they must resolve from there.
    for entry in manifest["skills"]:
        resolved = (manifest_path.parent.parent / entry).resolve()
        assert (resolved / "SKILL.md").is_file(), f"{entry} does not resolve to a skill from the plugin root"

    catalogue = json.loads((built / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert manifest["skills"] == catalogue["plugins"][0]["skills"], (
        "the plugin manifest and the marketplace catalogue disagree about which skills ship"
    )


def test_the_publish_target_matches_the_plugin_name() -> None:
    """The three identity constants must agree, and must be the post-rename ones.

    v0.3.0 renamed the marketplace to `powerbi-playbook` and the GitHub repo with it. A branch cut
    BEFORE that rename then merged cleanly and silently reverted all three constants (#144 landing
    after #134), because a stale base carrying the old values is not a textual conflict. Master would
    then have republished under the dead name, over a repo that no longer answers to it.

    Consistency alone would not have caught it — the revert changed all three together — so this
    pins the identity itself. If the plugin is ever legitimately renamed again, update this test in
    the same commit; that is the point, the rename should not be silent.
    """
    assert build_plugin.PLUGIN_NAME == "powerbi-playbook"
    assert build_plugin.MARKETPLACE_NAME == "powerbi-playbook-collection"
    assert build_plugin.PUBLISH_REPO.endswith(f"/{build_plugin.PLUGIN_NAME}"), (
        f"publish target {build_plugin.PUBLISH_REPO} does not match plugin name {build_plugin.PLUGIN_NAME}"
    )


def test_the_advertised_skills_are_the_shipped_skills(built: Path) -> None:
    """The README table and plugin description must not promise a bundle that is not in the release.

    Both are hand-written prose in `build_plugin.py`, so they drift the moment `SHIPPED_SKILLS`
    changes. v0.3.0 published with a description still advertising "persist a refreshed local PBIP
    ... via AMO ImageSave" for `pbip-model-refresh`, which that release had deliberately held back -
    a marketplace listing selling a capability the plugin did not contain. Nothing caught it; the
    build was green because the *files* were correct and only the prose lied.

    Note the description never contains the skill's NAME - it sells the capability in prose - so
    checking for the folder name catches the README and nothing else. The distinctive-identifier
    check below is what actually covers the description: `ImageSave` and `cache.abf` appear in the
    withheld bundle's own frontmatter and in no shipped one, so their presence in the marketplace
    copy is exactly the lie. (Verified failing against the v0.3.0 text.)
    """
    readme = (built / "README.md").read_text(encoding="utf-8")
    manifest = json.loads((built / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    description = manifest["plugins"][0]["description"]

    for name in build_plugin.SHIPPED_SKILLS:
        assert name in readme, f"{name} ships but the README does not mention it"

    def identifiers(text: str) -> set[str]:
        """Technical tokens a marketing sentence would only carry if it meant that specific feature."""
        return set(re.findall(r"\b[A-Za-z]+\.[a-z]{2,}\b|\b[a-z]+[A-Z][A-Za-z]+\b", text))

    def frontmatter(skill: str) -> str:
        return (build_plugin.SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")[:1500]

    shipped_terms = {term for name in build_plugin.SHIPPED_SKILLS for term in identifiers(frontmatter(name))}
    withheld = {p.name for p in build_plugin.SKILLS_DIR.iterdir() if p.is_dir()} - set(build_plugin.SHIPPED_SKILLS)

    for name in withheld:
        assert name not in readme, f"{name} is NOT shipped but the README still advertises it"
        exclusive = identifiers(frontmatter(name)) - shipped_terms
        leaked = sorted(term for term in exclusive if term in description)
        assert not leaked, f"{name} is NOT shipped, but the plugin description still sells it via {leaked}"


def test_rebuilding_preserves_a_git_clone(tmp_path: Path) -> None:
    """The publish workflow is clone -> rebuild -> commit -> push, so a rebuild must not nuke `.git`.

    It used to: `build()` called `shutil.rmtree(out)` unconditionally, which (a) deleted the clone's
    history, turning every republish into a fresh `git init`, and (b) crashed outright on Windows with
    `PermissionError: [WinError 5]`, because git marks objects under `.git/objects` read-only and
    `os.unlink` refuses those.
    """
    out = tmp_path / "marketplace"
    build_plugin.build(out)

    # Stand in for a clone: a .git dir containing a read-only object, exactly what tripped rmtree.
    objects = out / ".git" / "objects" / "07"
    objects.mkdir(parents=True)
    marker = objects / "d19e41700f821b3fb26bbe25216b7e1bad6577"
    marker.write_text("object", encoding="utf-8")
    marker.chmod(stat.S_IREAD)
    (out / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    stale = out / "plugins" / build_plugin.PLUGIN_NAME / "skills" / "removed-skill"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("gone next build", encoding="utf-8")

    build_plugin.build(out)

    assert marker.is_file(), "rebuild destroyed the clone's git objects"
    assert (out / ".git" / "HEAD").is_file(), "rebuild destroyed .git/HEAD"
    assert not stale.exists(), "rebuild left generated content from a previous run"
    assert (out / ".claude-plugin" / "marketplace.json").is_file(), "rebuild did not regenerate the manifest"


def test_check_detects_drift(tmp_path: Path) -> None:
    """`--check` must fail when the published tree no longer matches the bundles, or it gates nothing."""
    out = tmp_path / "marketplace"
    build_plugin.build(out)
    assert build_plugin.main(["--out", str(out), "--check"]) == 0

    skill = build_plugin.SHIPPED_SKILLS[0]
    (out / "plugins" / build_plugin.PLUGIN_NAME / "skills" / skill / "SKILL.md").write_text("drifted", encoding="utf-8")
    assert build_plugin.main(["--out", str(out), "--check"]) == 1
