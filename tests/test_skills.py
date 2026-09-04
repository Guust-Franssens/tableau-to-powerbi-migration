"""Regression tests for repo-local skills under `.github/skills/`.

Why this file exists: a skill is *documentation that an agent acts on*, so the two ways it rots are
both invisible at review time. `docs/agent-architecture.md` §8 names one of them outright -
"Referencing tools, scripts or paths that do not exist. Cheap to catch mechanically; worth a test."

The other is subtler and is the whole point of packaging this knowledge as a skill: the
`pbip-model-refresh` skill promises the procedure is portable to another BI migration (Qlik, Cognos)
by copying **the skill folder**. An import of a repo-local module, a test that reaches back into this
repo's `tests/`, or a fixture path that only exists here would all break that silently - the copy
still looks fine in review and only fails at runtime, in the other repo. So the promise is executed,
not asserted: the folder is copied to a temp directory and made to pass its own tests with this repo
unimportable.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / ".github" / "skills"
SKILL_FILES = sorted(SKILLS_DIR.glob("*/SKILL.md"))
BUNDLED_SKILLS = sorted(skill.parent for skill in SKILL_FILES if (skill.parent / "tests").is_dir())
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import build_plugin  # noqa: E402

# A markdown link whose target is a relative path, i.e. not http(s):, mailto: or a #fragment.
MD_LINK_RE = re.compile(r"\[[^\]]*\]\((?!https?:|mailto:|#)([^)\s]+)\)")

# Shims left at `scripts/<name>.py` after the real script moved into a skill, mapped to the skill that
# now owns the file and a flag that only the bundled script's own argument parser knows about.
FORWARDING_SHIMS = {
    "probe_desktop_query": ("pbip-model-refresh", "--table"),
    "refresh_pbip_model": ("pbip-model-refresh", "--ui-save"),
    "set_ai_instructions": ("powerbi-ai-readiness", "--strict"),
    "check_ai_readiness": ("powerbi-ai-readiness", "--all"),
}

# The AI-instruction section template. Stated in the `powerbi-ai-readiness` skill and nowhere else -
# the heading below is the canary, because it is the one no other document would coin by accident.
SECTION_TEMPLATE_CANARY = "## Business terminology and defaults"
SECTION_TEMPLATE_HOME = SKILLS_DIR / "powerbi-ai-readiness" / "SKILL.md"
REPORT_GOTCHAS = SKILLS_DIR / "powerbi-report-gotchas" / "SKILL.md"
AZURE_MAP_COOKBOOK = REPO_ROOT / ".github" / "pbi.kb" / "visuals" / "azureMap.md"
SEMANTIC_GOTCHAS = SKILLS_DIR / "powerbi-semantic-model-gotchas" / "SKILL.md"
BUNDLED_PATH_CLAIM_RE = re.compile(r"\bbundled(?:\s+in\s+this skill's)?\s+[`']([^`']+)[`']")


def test_the_skills_directory_is_not_empty() -> None:
    """Guards the collection itself: `SKILL_FILES` drives parametrization, and an empty glob would
    turn every test below into a silent no-op that still reports green."""
    assert SKILL_FILES, f"no SKILL.md found under {SKILLS_DIR}"


def test_semantic_model_gotchas_uses_live_validation_and_no_dead_bundled_claims(tmp_path: Path) -> None:
    """Keep issue #411's retired artifact claims out of both published copies of the skill."""
    source = SEMANTIC_GOTCHAS.read_text(encoding="utf-8")
    assert "scripts/_tools/TabularEditor/" not in source
    assert "bpa.ps1" not in source

    command = "python scripts/check_datamodel.py <SemanticModel>"
    assert command in source
    assert (REPO_ROOT / "scripts" / "check_datamodel.py").is_file()

    output = tmp_path / "marketplace"
    build_plugin.build(output)
    published = (
        output
        / "plugins"
        / build_plugin.PLUGIN_NAME
        / "skills"
        / "powerbi-semantic-model-gotchas"
        / "SKILL.md"
    )
    published_text = published.read_text(encoding="utf-8")
    assert command in published_text
    assert "scripts/_tools/TabularEditor/" not in published_text
    assert "bpa.ps1" not in published_text

    # Only prose that explicitly calls a relative path bundled by this skill is in scope here.
    for relative in BUNDLED_PATH_CLAIM_RE.findall(source):
        assert (SEMANTIC_GOTCHAS.parent / relative).exists(), f"missing source bundled path: {relative}"
        assert (published.parent / relative).exists(), f"missing published bundled path: {relative}"


def test_every_skill_that_ships_code_also_ships_the_tests_that_travel_with_it() -> None:
    """Same guard, one level down - and the rule that keeps bundled code gated.

    `BUNDLED_SKILLS` is discovered by looking for a `tests/` folder beside each `SKILL.md`, so
    renaming or relocating that folder would empty the parameter set of the portability gate below,
    and pytest turns an empty `parametrize` into a *skip*: green CI, promise never executed. This
    also states the convention for the next bundle - if a skill ships `scripts/`, the tests that
    prove those scripts survive the copy ship with them.
    """
    assert BUNDLED_SKILLS, f"no skill under {SKILLS_DIR} has a tests/ folder - the portability gate now skips"
    for skill in SKILL_FILES:
        if (skill.parent / "scripts").is_dir():
            assert skill.parent in BUNDLED_SKILLS, f"{skill.parent.name} ships scripts/ but no tests/ beside them"


@pytest.mark.parametrize("skill", SKILL_FILES, ids=lambda p: p.parent.name)
def test_every_skill_declares_the_frontmatter_that_makes_it_loadable(skill: Path) -> None:
    """`description` is what the model matches on to auto-select a skill, so a skill without one is
    dead weight that only ever runs when named explicitly."""
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill} has no YAML frontmatter"
    front = text.split("---\n", 2)[1]
    assert re.search(r"^name:\s*\S", front, re.MULTILINE), f"{skill} frontmatter has no name"
    assert re.search(r"^description:\s*\S", front, re.MULTILINE), f"{skill} frontmatter has no description"
    declared = re.search(r"^name:\s*(\S+)", front, re.MULTILINE).group(1)
    assert declared == skill.parent.name, f"{skill}: name '{declared}' != folder '{skill.parent.name}'"


@pytest.mark.parametrize("skill", SKILL_FILES, ids=lambda p: p.parent.name)
def test_every_path_a_skill_points_at_exists(skill: Path) -> None:
    """The named anti-pattern. A skill that cites a moved script sends the agent to read nothing and
    then improvise, which is worse than having no skill at all."""
    for target in MD_LINK_RE.findall(skill.read_text(encoding="utf-8")):
        resolved = (skill.parent / target.split("#", 1)[0]).resolve()
        assert resolved.exists(), f"{skill} links to {target}, which does not exist"


def _repo_free_env() -> dict[str, str]:
    """A child environment with nothing of this repo importable."""
    env = dict(os.environ)
    # An inherited PYTHONPATH (or the parent pytest's own bookkeeping) would put this repo's
    # `scripts/` back on `sys.path` and make the copy pass for the wrong reason.
    env.pop("PYTHONPATH", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    # `T2P_RUN_GUI` is the PORTABLE opt-in the copied bundle's own conftest reads (issue #447), and
    # inheriting it here would make a PORTABILITY check spawn ten real top-level windows as a side
    # effect. Whoever exported it asked to run the gui tests, not to have a `test_skills.py` run
    # hijack their desktop; CI's `--run-gui -m gui` leg is where those tests are actually executed.
    # Dropping it also makes this nested run deterministic rather than ambient-environment-dependent.
    env.pop("T2P_RUN_GUI", None)
    return env


def _nested_marker_filter() -> list[str]:
    """Deselect wall-clock-budget tests from the nested run, but only under a parallel outer run.

    This nested pytest is serial, but its PROCESS competes with every other xdist worker, and the
    bundle contains assertions on a ~0.03s operation finishing inside 0.5s. Measured (issue #387):
    that test failed at 0.519s inside this nested run during a parallel campaign, while the outer
    suite's own copy of it - deselected by `-m "not (serial or timing)"` - never ran. The outer
    exclusion structurally cannot reach here: the bundle is copied to a temp directory, so the repo
    root `conftest.py` and `pyproject.toml` are both absent by construction.

    Conditional on `PYTEST_XDIST_WORKER` rather than unconditional, so the serial pre-PR gate still
    executes every test the bundle ships. Tier 1 trades those six for a loop that does not flake;
    tier 2 gives them back.
    """
    return ["-m", "not (serial or timing)"] if os.environ.get("PYTEST_XDIST_WORKER") else []


@pytest.mark.parametrize("skill_dir", BUNDLED_SKILLS, ids=lambda p: p.name)
def test_a_bundled_skill_passes_its_own_tests_after_being_copied_out_of_this_repo(
    skill_dir: Path, tmp_path: Path
) -> None:
    """The portability promise, executed instead of asserted.

    `pbip-model-refresh` tells the reader to copy ONE folder into a Qlik or Cognos migration repo -
    nothing about refreshing and persisting a PBIP is Tableau-specific, the input is already a Power
    BI model. This copies exactly what a reader would copy, then runs the bundled tests from that
    copy with the repo root out of `sys.path` (`cwd` is the temp dir, `PYTHONPATH` cleared, and the
    project installs no modules - see `py-modules = []`). A stray `import tableau_lineage`, a
    `parents[N]` walk that assumes this repo's depth, or a fixture that only exists here all fail
    HERE rather than in someone else's repo.
    """
    copied = shutil.copytree(
        skill_dir,
        tmp_path / skill_dir.name,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(copied / "tests"), *_nested_marker_filter()],
        cwd=tmp_path,
        env=_repo_free_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{skill_dir.name} does not pass its own tests when copied out:\n{result.stdout}"
    assert "no tests ran" not in result.stdout, f"{skill_dir.name} shipped a tests/ folder that collects nothing"


def _imported_top_level_names(script: Path) -> set[str]:
    """Every top-level module name `script` imports, on any code path."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(script.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".", 1)[0])
    return names


@pytest.mark.parametrize("skill_dir", BUNDLED_SKILLS, ids=lambda p: p.name)
def test_no_bundled_script_imports_a_module_that_only_exists_in_this_repo(skill_dir: Path) -> None:
    """Static counterpart to the copy-and-run gate above, which only sees imports that EXECUTE.

    Both bundled scripts import their Windows dependencies lazily, inside functions (`_load_adomd`,
    `_load_amo`), and the bundled suite monkeypatches ADOMD/AMO rather than reaching those bodies -
    so a repo-local import added there would run in nobody's test, pass the copy-and-run gate, and
    break only in the repo that copied the folder. `ast.walk` sees it whatever code path it is on.
    """
    bundled = {path.stem for path in (skill_dir / "scripts").glob("*.py")}
    repo_local = {path.stem for path in (REPO_ROOT / "scripts").glob("*.py")}
    repo_local |= {path.stem for path in (REPO_ROOT / "tests").glob("*.py")}
    forbidden = repo_local - bundled

    for script in sorted((skill_dir / "scripts").glob("*.py")):
        offenders = _imported_top_level_names(script) & forbidden
        assert not offenders, (
            f"{script.relative_to(REPO_ROOT)} imports {sorted(offenders)} from this repo, which "
            f"breaks the 'copy this folder' claim in {skill_dir.name}/SKILL.md"
        )


@pytest.mark.parametrize("shim", sorted(FORWARDING_SHIMS), ids=str)
def test_the_scripts_shim_still_reaches_the_script_that_moved_into_the_skill(shim: str) -> None:
    """`scripts/<name>.py` is a forwarding shim now; prove the forward actually arrives.

    The four personas under `.github/agents/` still invoke `python scripts/refresh_pbip_model.py` and
    `python scripts/set_ai_instructions.py`, and that directory is out of reach for the agent that
    moved these files - so the shims are the reason nothing broke on merge. An entry point nobody
    exercises is an entry point that rots, and the failure would surface mid-migration. `--help` is
    enough: only the BUNDLED parser knows the flag asserted below, so seeing it proves the real script
    ran with `sys.argv` intact.
    """
    skill, flag = FORWARDING_SHIMS[shim]
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / f"{shim}.py"), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert flag in result.stdout, f"scripts/{shim}.py did not reach the bundled script ({flag} missing)"
    assert str(Path(".github/skills") / skill / "scripts" / f"{shim}.py") in result.stdout.replace("/", os.sep)


def test_this_repo_really_has_the_examples_corpus_the_skill_tests_fall_back_from() -> None:
    """Keeps the skills' honest `skip` from becoming a silent no-op *here*.

    Both bundles skip their corpus tests when there is no `examples/` tree, which is right for a
    copied skill and wrong for this repo: those 16 committed models are the only ground truth the
    TMDL fingerprint and the fabricated-reference check have. Without this guard, deleting or
    renaming the corpus would turn a pile of assertions into green skips.
    """
    models = sorted((REPO_ROOT / "examples").glob("*/fabric/*.SemanticModel"))
    assert models, "examples/*/fabric/*.SemanticModel is empty - the skills' corpus tests now skip silently"


def test_the_bundled_scripts_are_the_ones_the_shims_and_docs_point_at() -> None:
    """One canonical copy. Two files with the same name and different contents is the worst outcome."""
    for name, (skill, _flag) in sorted(FORWARDING_SHIMS.items()):
        bundled = SKILLS_DIR / skill / "scripts" / f"{name}.py"
        assert bundled.exists(), f"{bundled} is missing"
        shim = (REPO_ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
        assert "runpy" in shim and skill in shim, (
            f"scripts/{name}.py is no longer a forwarding shim for {skill} - if the script moved back, "
            "delete it from the skill bundle and update SKILL.md's 'Available scripts' section"
        )


def _markdown_outside_the_agents_dir() -> list[Path]:
    """Every **tracked** markdown file except agent personas and generated instruction instances.

    `.github/agents/` is excluded because the persona carries a deliberately compressed pointer to
    the template rather than the template itself. `ai-instructions.md` files are excluded because
    they are *instances* of the template - filling it in is the point, not drift.

    Enumerated with `git ls-files`, not `rglob`. An earlier version walked the filesystem while
    claiming to list committed files, so it also swept up **gitignored build output**: once
    `scripts/build_plugin.py` copied the skills into `dist/marketplace/`, the template legitimately
    existed twice on disk and this guard failed on an artifact nobody committed. Tracking is the
    property the guard actually cares about.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        REPO_ROOT / rel
        for rel in sorted(filter(None, tracked.split("\0")))
        if ".github/agents" not in rel and not rel.endswith("ai-instructions.md")
    ]


def test_the_ai_instruction_section_template_is_stated_in_exactly_one_place() -> None:
    """The drift guard. A second copy of the template is how a section quietly goes missing.

    That already happened: one copy listed six sections and the other seven - "Verified headline
    numbers" was in the guide and absent from the persona, so which one you read decided whether a
    migration anchored its instructions to ground-truth totals. Nothing failed; the copies just
    disagreed. `docs/ai-instructions-authoring-guide.md` is now a stub pointing here for exactly
    this reason.
    """
    assert SECTION_TEMPLATE_CANARY in SECTION_TEMPLATE_HOME.read_text(encoding="utf-8"), (
        f"the section template is no longer in {SECTION_TEMPLATE_HOME.name} - this guard now proves nothing"
    )
    others = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _markdown_outside_the_agents_dir()
        if path != SECTION_TEMPLATE_HOME and SECTION_TEMPLATE_CANARY in path.read_text(encoding="utf-8")
    ]
    assert not others, (
        f"the AI-instruction section template is restated in {others} - link to "
        f"{SECTION_TEMPLATE_HOME.relative_to(REPO_ROOT).as_posix()} instead of copying it"
    )


def test_powerbi_report_gotchas_keep_field_proven_report_defects_visible() -> None:
    """Regression guard for field-proven doc defects that previously generated broken reports."""
    text = REPORT_GOTCHAS.read_text(encoding="utf-8")
    collapsed = " ".join(text.split())

    assert "prefer `defaultStyle 'blank_accessible'`" not in text
    for needle in [
        "checks reference **shape**, not reference **target**",
        "A `lineChart` with a multi-level `Category` silently renders only the TOP level.",
        "Match the source worksheet's basemap before choosing `defaultStyle`.",
    ]:
        assert needle in text
    assert "filterConfig` restricts which items are offered and pre-selects nothing" in collapsed

    # Defect 3's point is that section 1 and section 8 must AGREE on where a slicer default lives, so
    # a half-regression has to fail too: one assertion per section, keyed on that section's own
    # wording. A bare `in` check on the shared path cannot see a one-sided break - section 3 has it too.
    for section, needle in [
        ("section 1", "A slicer's pre-selection lives in `objects.general[0].properties.filter`"),
        ("section 8", "via `objects.general[0].properties.filter`, never a top-level `filterConfig`"),
    ]:
        assert needle in collapsed, f"{section} no longer states where a slicer default lives"


def test_azure_map_cookbook_does_not_recommend_dead_end_filter_or_blank_basemap_defaults() -> None:
    """The cookbook is copied by agents, so keep measured-negative remedies from creeping back in."""
    text = AZURE_MAP_COOKBOOK.read_text(encoding="utf-8")

    assert "**Top-N filter**" not in text
    assert "only after confirming the source genuinely has no basemap" in text
    assert "same 3 of 10" in text
