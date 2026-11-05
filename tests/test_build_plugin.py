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
    checking for the folder name catches the README table and nothing else. The distinctive-identifier
    check below is what actually covers the marketing copy: `ImageSave` and `cache.abf` appear in the
    withheld bundle's own frontmatter and in no shipped one, so their presence in the marketplace
    copy is exactly the lie. (Verified failing against the v0.3.0 text.)

    ⚠️ **Its reach is identifiers, not plain English.** The v0.3.0 README opened with "persisting a
    refreshed local PBIP", which names the withheld capability in ordinary words carrying no
    mixed-case or dotted token - measured, this check returns `[]` for that sentence. Detecting that
    class reliably needs a human reading the summary paragraph, so treat this as a floor, not a
    guarantee: it catches the copy-paste of technical terms, which is the common failure, and it
    will not catch a fluent English claim.
    """
    readme = (built / "README.md").read_text(encoding="utf-8")
    manifest = json.loads((built / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    description = manifest["plugins"][0]["description"]

    for name in build_plugin.SHIPPED_SKILLS:
        assert name in readme, f"{name} ships but the README does not mention it"

    def identifiers(text: str) -> set[str]:
        """Technical tokens a marketing sentence would only carry if it meant that specific feature."""
        return set(re.findall(r"\b[A-Za-z]+\.[a-z]{2,}\b|\b[A-Za-z]*[a-z][A-Z][A-Za-z]+\b", text))

    def frontmatter(skill: str) -> str:
        """Read the repository's two-field descriptor, not arbitrary YAML or guide-body links.

        Accept a single-line prose description, plain or simply quoted (no escapes). Blocks,
        comments, extra fields and other YAML constructs require explicit review, never an empty
        token set. This intentionally narrow contract matches every current canonical descriptor.
        """
        text = (build_plugin.SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"---\n(.*?)^---(?:\n|\Z)", text, re.DOTALL | re.MULTILINE)
        assert match, f"{skill} has missing or unclosed frontmatter"
        front = match.group(1)
        assert re.search(rf"^name:[ \t]*{re.escape(skill)}[ \t]*$", front, re.MULTILINE), (
            f"{skill} frontmatter has a missing or mismatched name"
        )
        description_field = re.fullmatch(rf"name: {re.escape(skill)}\ndescription:[ \t]*([^\n]*)\n", front)
        assert description_field, f"{skill} frontmatter has a missing or empty description or unsupported structure"
        value = description_field.group(1).strip()
        if value.startswith(("'", '"')):
            quoted = re.fullmatch(r"""(?:'([^'\\]*)'|"([^"\\]*)")""", value)
            assert quoted, f"{skill} frontmatter has a malformed or unsupported quoted description"
            value = next(group for group in quoted.groups() if group is not None).strip()
        else:
            assert not re.search(r"(^|[ \t])#", value), f"{skill} frontmatter has an unsupported description comment"
        assert value, f"{skill} frontmatter has a missing or empty description"
        assert re.match(r"[A-Za-z]", value) and value.lower() not in {
            "null",
            "true",
            "false",
            "yes",
            "no",
            "on",
            "off",
        }, f"{skill} frontmatter has an unsupported description scalar; expected single-line prose"
        return value

    shipped_terms = {term for name in build_plugin.SHIPPED_SKILLS for term in identifiers(frontmatter(name))}
    withheld = {p.name for p in build_plugin.SKILLS_DIR.iterdir() if p.is_dir()} - set(build_plugin.SHIPPED_SKILLS)

    for name in withheld:
        exclusive = identifiers(frontmatter(name)) - shipped_terms
        for surface, text in (("plugin description", description), ("README", readme)):
            assert name not in text, f"{name} is NOT shipped but the {surface} still advertises it"
            leaked = sorted(term for term in exclusive if term in text)
            assert not leaked, f"{name} is NOT shipped, but the {surface} still sells it via {leaked}"


def _replace_skill_read(monkeypatch: pytest.MonkeyPatch, skill: Path, text: str) -> None:
    original_read = Path.read_text

    def read_text(path: Path, *args, **kwargs) -> str:
        return text if path == skill else original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


def test_publication_ignores_skill_body_links(built: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Body cross-references are not exclusive capability descriptors, at any character offset."""
    skill = build_plugin.SKILLS_DIR / "live-source-reachability" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    front = text[: text.index("---\n", 4) + 4]
    link = "[Refresh procedure](../pbip-model-refresh/SKILL.md)\n"

    for body in ("", link, "Ordinary guide text.\n" * 100 + link):
        test_the_advertised_skills_are_the_shipped_skills(built)
        with monkeypatch.context() as patch:
            _replace_skill_read(patch, skill, front + "\n" + body)
            test_the_advertised_skills_are_the_shipped_skills(built)
        test_the_advertised_skills_are_the_shipped_skills(built)


@pytest.mark.parametrize(
    ("descriptor", "message"),
    [
        ("No metadata\n", "missing or unclosed frontmatter"),
        ("---\nname: live-source-reachability\n", "missing or unclosed frontmatter"),
        ("---\ndescription: A live source guide.\n---\n", "missing or mismatched name"),
        ("---\nname: wrong-skill\ndescription: A guide.\n---\n", "missing or mismatched name"),
        ("---\nname: live-source-reachability\n---\n", "missing or empty description"),
        ("---\nname: live-source-reachability\ndescription: \n---\n", "missing or empty description"),
        ('---\nname: live-source-reachability\ndescription: ""\n---\n', "missing or empty description"),
    ],
)
def test_publication_rejects_missing_descriptor(
    built: Path, monkeypatch: pytest.MonkeyPatch, descriptor: str, message: str
) -> None:
    """An unavailable descriptor cannot silently turn the publication check into a no-op."""
    skill = build_plugin.SKILLS_DIR / "live-source-reachability" / "SKILL.md"
    test_the_advertised_skills_are_the_shipped_skills(built)
    with monkeypatch.context() as patch:
        _replace_skill_read(patch, skill, descriptor)
        with pytest.raises(AssertionError, match=f"live-source-reachability.*{message}"):
            test_the_advertised_skills_are_the_shipped_skills(built)
    test_the_advertised_skills_are_the_shipped_skills(built)


@pytest.mark.parametrize(
    "description",
    [
        "# no value",
        "|",
        '"unterminated',
        "'unterminated",
        "'   '",
        "null",
        "[]",
        "|\n  Persist cache.abf through ImageSave.",
        "A guide.\ndescription: # duplicate",
    ],
)
@pytest.mark.parametrize("surface", ["README", "plugin description"])
def test_publication_rejects_unusable_descriptor_before_claims(
    built: Path, monkeypatch: pytest.MonkeyPatch, description: str, surface: str
) -> None:
    """Malformed/unsupported metadata must refuse even when a withheld claim would otherwise leak."""
    skill = build_plugin.SKILLS_DIR / "pbip-model-refresh" / "SKILL.md"
    path = built / ("README.md" if surface == "README" else ".claude-plugin/marketplace.json")
    text = path.read_text(encoding="utf-8")
    claim = "\nThis plugin persists cache.abf through ImageSave."
    if surface == "README":
        advertised = text + claim
    else:
        manifest = json.loads(text)
        manifest["plugins"][0]["description"] += claim
        advertised = json.dumps(manifest)

    test_the_advertised_skills_are_the_shipped_skills(built)
    with monkeypatch.context() as patch:
        _replace_skill_read(patch, path, advertised)
        with pytest.raises(AssertionError, match=f"pbip-model-refresh is NOT shipped, but the {surface} still sells"):
            test_the_advertised_skills_are_the_shipped_skills(built)
    test_the_advertised_skills_are_the_shipped_skills(built)
    with monkeypatch.context() as patch:
        _replace_skill_read(patch, path, advertised)
        _replace_skill_read(patch, skill, f"---\nname: pbip-model-refresh\ndescription: {description}\n---\n")
        with pytest.raises(AssertionError, match="pbip-model-refresh frontmatter has"):
            test_the_advertised_skills_are_the_shipped_skills(built)
    test_the_advertised_skills_are_the_shipped_skills(built)


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_publication_preserves_supported_description_values(
    built: Path, monkeypatch: pytest.MonkeyPatch, quote: str
) -> None:
    """Plain and simple quoted prose retain the capability terms, not their scalar syntax."""
    skill = build_plugin.SKILLS_DIR / "pbip-model-refresh" / "SKILL.md"
    value = f"{quote}Persist cache.abf through ImageSave.{quote}"
    test_the_advertised_skills_are_the_shipped_skills(built)
    with monkeypatch.context() as patch:
        _replace_skill_read(patch, skill, f"---\nname: pbip-model-refresh\ndescription: {value}\n---\n")
        test_the_advertised_skills_are_the_shipped_skills(built)
        for surface in ("README", "plugin description"):
            for claim in ("ImageSave", "cache.abf"):
                test_publication_rejects_withheld_claims(built, surface, claim)
    test_the_advertised_skills_are_the_shipped_skills(built)


@pytest.mark.parametrize("surface", ["README", "plugin description"])
@pytest.mark.parametrize("claim", ["ImageSave", "cache.abf", "pbip-model-refresh"])
def test_publication_rejects_withheld_claims(built: Path, surface: str, claim: str) -> None:
    """Mutate actual output, require the intended publication failure, then restore healthy bytes."""
    test_the_advertised_skills_are_the_shipped_skills(built)
    path = built / ("README.md" if surface == "README" else ".claude-plugin/marketplace.json")
    original = path.read_bytes()
    text = original.decode("utf-8")
    advertisement = f"\nThis plugin includes {claim} for local model persistence.\n"
    if surface == "README":
        mutated = text + advertisement
    else:
        manifest = json.loads(text)
        manifest["plugins"][0]["description"] += advertisement
        mutated = json.dumps(manifest)
    message = (
        f"pbip-model-refresh is NOT shipped but the {surface} still advertises it"
        if claim == "pbip-model-refresh"
        else f"pbip-model-refresh is NOT shipped, but the {surface} still sells it via ['{claim}']"
    )
    try:
        path.write_text(mutated, encoding="utf-8")
        with pytest.raises(AssertionError, match=re.escape(message)):
            test_the_advertised_skills_are_the_shipped_skills(built)
    finally:
        path.write_bytes(original)
    test_the_advertised_skills_are_the_shipped_skills(built)


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
