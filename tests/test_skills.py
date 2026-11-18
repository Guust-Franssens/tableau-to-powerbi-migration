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
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / ".github" / "skills"
SKILL_FILES = sorted(SKILLS_DIR.glob("*/SKILL.md"))
BUNDLED_SKILLS = sorted(skill.parent for skill in SKILL_FILES if (skill.parent / "tests").is_dir())

# A markdown link whose target is a relative path, i.e. not http(s):, mailto: or a #fragment.
MD_LINK_RE = re.compile(r"\[[^\]]*\]\((?!https?:|mailto:|#)([^)\s]+)\)")

# Shims left at `scripts/<name>.py` after the real script moved into a skill, mapped to the skill that
# now owns the file and a flag that only the bundled script's own argument parser knows about.
FORWARDING_SHIMS = {
    "probe_desktop_query": ("pbip-model-refresh", "--table"),
    "refresh_pbip_model": ("pbip-model-refresh", "--ui-refresh"),
    "open_desktop": ("pbip-model-refresh", "--check"),
    "set_ai_instructions": ("powerbi-ai-readiness", "--strict"),
    "check_ai_readiness": ("powerbi-ai-readiness", "--all"),
}

# The AI-instruction section template. Stated in the `powerbi-ai-readiness` skill and nowhere else -
# the heading below is the canary, because it is the one no other document would coin by accident.
SECTION_TEMPLATE_CANARY = "## Business terminology and defaults"
SECTION_TEMPLATE_HOME = SKILLS_DIR / "powerbi-ai-readiness" / "SKILL.md"
AZURE_MAP_COOKBOOK = REPO_ROOT / ".github" / "pbi.kb" / "visuals" / "azureMap.md"

# --- credential/dialog guidance contract (issue #146) ---------------------------------------------
# The arbiter `.github/skills/pbip-model-refresh/scripts/probe_desktop_credential.ps1` is the wording
# oracle; these are the operator-facing paragraphs that restate it. Only CURRENT-guidance blocks are
# read: the same files also carry historical defect narratives that legitimately quote the retired
# claims, and scanning those would forbid the repo from recording its own history.
PBIP_REFRESH_SKILL = SKILLS_DIR / "pbip-model-refresh" / "SKILL.md"
LIVE_SOURCE_SKILL = SKILLS_DIR / "live-source-reachability" / "SKILL.md"
CREDENTIAL_DOC = REPO_ROOT / "docs" / "data-source-credentials.md"
SCRIPTS_README = REPO_ROOT / "scripts" / "README.md"
AGENTS_DOC = REPO_ROOT / "AGENTS.md"
RUNBOOK = REPO_ROOT / "docs" / "operator-runbook.md"
CAPABILITY_WIRING = REPO_ROOT / "docs" / "agent-capability-wiring.md"
# Refresh machinery the UIA-default change removed; teaching any of it hands the operator an exit 2.
REMOVED_REFRESH_MACHINERY = ("--calculate-only", "--measures-only", "ImageSave", "NuGet")
# Issue #831: any blocking dialog is a human stop, so a verdict table carries only these live tokens.
LIVE_VERDICT_TOKENS = (
    "READY",
    "HUMAN_ACTION_REQUIRED",
    "UNKNOWN",
    "NO_WINDOW",
    "DIALOG_NEEDS_HUMAN",
    "CREDENTIAL_UNKNOWN",
    "DESKTOP_UNREADY",
)
# Text-classification tokens #831 retired. Receipts still accept them; a current table must not list them.
RETIRED_VERDICT_TOKENS = (
    "CREDENTIAL_MISSING",
    "CREDENTIAL_PRESENT",
    "REFRESH_IN_PROGRESS",
    "DIALOG_UNREADABLE",
    "DIALOG_UNRECOGNIZED",
    "CREDENTIAL_REQUIRED",
    "UNKNOWN_DIALOG",
    "UNREADABLE_DIALOG",
)
DESKTOP_VERDICT_TOKENS = LIVE_VERDICT_TOKENS + RETIRED_VERDICT_TOKENS
AMNESTY_BLOCK_LEAD = "### One read-only Desktop observer"
PREFLIGHT_STEP_LEAD = "3. **Local:**"
# The canonical clean-run paragraph: `DATA_OK` is earned, and an active dialog or any authoritative
# non-success token outranks a stale or late one. Bounded to its own paragraph so the next one cannot
# stand in for it.
CLEAN_RUN_LEAD = "⚠️ **`DATA_OK` is earned by a clean run"
CLEAN_RUN_ENDS = "**Pass `--pid`"
# Exact claims the arbiter's own evidence does not support. Retired, not merely discouraged.
RETIRED_CREDENTIAL_CLAIMS = (
    "$MinPromptWords",
    "still has the length-amnesty",
    "already cached",
    "ran to the deadline",
    "approval, not a sign-in",
    "not a sign-in prompt",
    "no sign-in implied",
)


def _verdict_rows(path: Path) -> str:
    """The Desktop-arbiter verdict-table rows in `path`, blockquoted or not."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip(">").strip()
        if not stripped.startswith("| `"):
            continue
        if stripped.split("`")[1] in DESKTOP_VERDICT_TOKENS:
            rows.append(stripped)
    return "\n".join(rows)


def _block_from(path: Path, lead: str, *, ends: str | None = None) -> str:
    """The contiguous block that starts at the line beginning with `lead`.

    It stops before the first line starting with `ends`, and - when `lead` is a blockquote - at the
    first unquoted line, so a current-guidance paragraph cannot silently absorb the historical
    narrative that follows it and pass on that text instead.
    """
    quoted = lead.startswith(">")
    block: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not block:
            if line.startswith(lead):
                block.append(line)
            continue
        if (ends is not None and line.startswith(ends)) or (quoted and not line.startswith(">")):
            break
        block.append(line)
    return "\n".join(block)


def _table_row(path: Path, key: str) -> str:
    """The single markdown table row whose first cell names `key`."""
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(f"| `{key}`")]
    return "\n".join(rows)


def test_the_skills_directory_is_not_empty() -> None:
    """Guards the collection itself: `SKILL_FILES` drives parametrization, and an empty glob would
    turn every test below into a silent no-op that still reports green."""
    assert SKILL_FILES, f"no SKILL.md found under {SKILLS_DIR}"


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
    return env


def _nested_marker_filter() -> list[str]:
    """Deselect wall-clock-budget tests from the nested run, but only under a parallel outer run.

    This nested pytest is serial, but its PROCESS competes with every other xdist worker, and the
    bundle contains assertions on a ~0.03s operation finishing inside 0.5s. Measured (issue #387):
    that test failed at 0.519s inside this nested run during a parallel campaign, while the outer
    suite's own copy of it - deselected by `-m "not timing"` - never ran. The outer
    exclusion structurally cannot reach here: the bundle is copied to a temp directory, so the repo
    root `conftest.py` and `pyproject.toml` are both absent by construction.

    Conditional on `PYTEST_XDIST_WORKER` rather than unconditional, so the serial pre-PR gate still
    executes every test the bundle ships. Tier 1 trades those six for a loop that does not flake;
    tier 2 gives them back.
    """
    return ["-m", "not timing"] if os.environ.get("PYTEST_XDIST_WORKER") else []


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
    if skill_dir.name == "pbip-model-refresh":
        for name in (
            "_desktop_actions.py",
            "_desktop_observer.py",
            "_desktop_actions.ps1",
        ):
            assert (copied / "scripts" / name).is_file(), f"portable observer dependency missing: {name}"
        assert not (copied / "scripts" / "_desktop_uia.ps1").exists()
        assert not (copied / "scripts" / "_credential_modal.py").exists()
        assert not (copied / "scripts" / "probe_desktop_credential.ps1").exists()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--confcutdir",
            str(copied),
            "--basetemp",
            str(tmp_path / "pytest-work"),
            str(copied / "tests"),
            *_nested_marker_filter(),
        ],
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

    Bundled scripts import their Windows dependencies lazily, inside functions (`_load_adomd_from`),
    and the bundled suite fakes pythonnet/clr rather than loading the CLR - so a repo-local import
    added there would run in nobody's test, pass the copy-and-run gate, and break only in the repo
    that copied the folder. `ast.walk` sees it whatever code path it is on.
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


def test_ai_stamping_deviation_has_one_policy_home_and_sealed_handoff() -> None:
    """Policy travels with the portable writer; other documents point to it, not a fork."""
    canary = "Owner decision #731: direct stamping is a bounded exception"
    text = SECTION_TEMPLATE_HOME.read_text(encoding="utf-8")
    assert canary in text
    others = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _markdown_outside_the_agents_dir()
        if path != SECTION_TEMPLATE_HOME and canary in path.read_text(encoding="utf-8")
    ]
    assert not others
    for required in (
        "microsoft/skills-for-fabric/issues/72",
        "microsoft/skills-for-fabric/issues/89",
        "--adopt-existing <sha256>",
        "T2P_CustomInstructionsSha256",
        "annotations: []",
        "--check --strict --model <model> --md <md>",
        "after",
        "Unmeasured",
        "AI visibility",
        "verified answers",
        "semantic indexing",
        "tenant/workspace",
        "--targets",
        "power loss",
    ):
        assert required.lower() in text.lower(), required
    capabilities = (REPO_ROOT / "docs" / "capabilities-and-limitations.md").read_text(encoding="utf-8")
    assert "SKILL.md#0-deliberate-deviation-owned-instruction-stamping" in capabilities


def test_azure_map_cookbook_does_not_recommend_dead_end_filter_or_blank_basemap_defaults() -> None:
    """The cookbook is copied by agents, so keep measured-negative remedies from creeping back in."""
    text = AZURE_MAP_COOKBOOK.read_text(encoding="utf-8")

    assert "**Top-N filter**" not in text
    assert "only after confirming the source genuinely has no basemap" in text
    assert "same 3 of 10" in text


def test_credential_guidance_states_only_what_the_desktop_arbiter_establishes() -> None:
    """Issue #146: keep the operator-facing credential guidance inside the arbiter's own evidence.

    The oracle is `probe_desktop_credential.ps1`'s emitted guidance, which may claim a sign-in only
    for `CREDENTIAL_MISSING`, must send a `DIALOG_NEEDS_HUMAN` reader to the prompt on screen rather
    than prescribing an action, and must leave the credential state undetermined in BOTH directions
    for the two ambiguous dialog tokens. Docs that outran that evidence are what this pins.

    Deliberately scoped to the current-guidance blocks. The same files also carry the historical
    defect narratives (the #406 length amnesty, the retired `BLOCKED_BY_DIALOG` band) that quote the
    retired wording on purpose, and a whole-file scan would forbid the repo from recording its own
    history.
    """
    guidance = {
        "pbip-model-refresh verdict table": _verdict_rows(PBIP_REFRESH_SKILL),
        "pbip-model-refresh length-amnesty status": _block_from(
            PBIP_REFRESH_SKILL,
            AMNESTY_BLOCK_LEAD,
            ends="> ⚠️ **Do not wait blindly",
        ),
        "data-source-credentials verdict table": _verdict_rows(CREDENTIAL_DOC),
        "data-source-credentials preflight step": _block_from(CREDENTIAL_DOC, PREFLIGHT_STEP_LEAD, ends="4. "),
        "scripts/README arbiter row": _table_row(SCRIPTS_README, "probe_desktop_credential.ps1"),
    }
    for name, block in guidance.items():
        assert block, f"the {name} block was not found - this contract now proves nothing"
        for claim in RETIRED_CREDENTIAL_CLAIMS:
            assert claim not in block, f"{name} still carries the retired claim {claim!r}"

    for name, needle in [
        ("pbip-model-refresh verdict table", "Act on the prompt visible in Desktop"),
        ("pbip-model-refresh verdict table", "the remedy is **not** established either"),
        ("pbip-model-refresh verdict table", "A bounded observation"),
        ("pbip-model-refresh length-amnesty status", "#406 remains closed"),
        ("data-source-credentials verdict table", "does **not** establish which action"),
        ("data-source-credentials verdict table", "A bounded observation only"),
        ("data-source-credentials preflight step", "bounded observation"),
        ("scripts/README arbiter row", "Retired compatibility shim"),
        ("scripts/README arbiter row", "no observation or Refresh occurs"),
    ]:
        assert needle in guidance[name], f"{name} no longer states {needle!r}"

    # Issue #831: any blocking dialog is a human stop named by its title, and neither table may
    # bring back a retired text-classification token as a row of current guidance.
    for name in ("pbip-model-refresh verdict table", "data-source-credentials verdict table"):
        table = guidance[name]
        assert "any blocking dialog is a human stop" in table.lower(), f"{name} lost the structural human stop"
        assert 'dialog is open: "<title>"' in table, f"{name} no longer names the dialog title"
        row_tokens = {row.split("`")[1] for row in table.splitlines()}
        retired = sorted(row_tokens & set(RETIRED_VERDICT_TOKENS))
        assert not retired, f"{name} still lists retired verdict token(s) {retired}"

    # A clean run is what earns DATA_OK: an active dialog, any authoritative non-success token, or a
    # non-zero exit outranks a stale or late DATA_OK-looking line. The refresh's own terminal
    # outcome and named canaries matter, not text collected from a different or failed attempt.
    clean_run = _block_from(CREDENTIAL_DOC, CLEAN_RUN_LEAD, ends=CLEAN_RUN_ENDS)
    assert clean_run, (
        f"the clean-run DATA_OK paragraph was not found in {CREDENTIAL_DOC.name} - this contract now proves nothing"
    )
    assert "an active dialog vetoes it" in clean_run, (
        f"{CREDENTIAL_DOC.name} no longer states that an active dialog vetoes DATA_OK"
    )
    assert "outranks a stale or late" in clean_run, (
        f"{CREDENTIAL_DOC.name} no longer states that an authoritative non-success verdict outranks a "
        "stale or late DATA_OK line"
    )
    for permissive in ("may be accepted", "can still be trusted", "even while an active dialog"):
        assert permissive not in clean_run.lower(), (
            f"{CREDENTIAL_DOC.name} now permits a DATA_OK that was never earned ({permissive!r}): an "
            "active dialog or any authoritative non-success token must veto it"
        )


def test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop() -> None:
    """The live-source route after the credential gate was retired: instruction and tool behaviour only.

    With no file-system block left, these sentences are the whole control: check the emitted model,
    refresh the shipped copy with canaries, stop for a human on any dialog, do it before builders,
    and count only check_unit's data-evidence row as proof.
    """
    text = LIVE_SOURCE_SKILL.read_text(encoding="utf-8")
    for needle, diagnostic in [
        (r"python scripts\check_bundle_sources.py <bundle> --spec <spec>", "live-source skill lost static check"),
        ("--pid <pid> --canaries", "live-source skill lost canary refresh"),
        ("**before dispatching builders**", "live-source skill lost builder order"),
        ("**A dialog is a human stop.**", "live-source skill lost human stop"),
        ("Never type, paste or store", "live-source skill lost no-secrets rule"),
        ("PASS `data-evidence` row", "live-source skill lost data evidence"),
        ("same-run PASS `data-evidence` row", "live-source skill lost same-run evidence"),
        ("trusted engine-emission snapshot, never the live model", "live-source skill lost trusted source identity"),
        (
            "Structural refusals do **not** prove missing credentials",
            "live-source skill lost structural stop distinction",
        ),
        ("Never drop the flag or switch refresh routes", "live-source skill lost fail-closed qualification"),
        (
            "A refresh without `--automatic-source-scope` remains operator-only",
            "live-source skill lost operator-only consent",
        ),
        (
            "[<table> ...] --automatic-source-scope <expected-sources.json>",
            "live-source skill lost mandatory automatic source scope",
        ),
        ("`fallback_authorization: model_only_unvalidated`", "live-source skill lost model-only source"),
    ]:
        assert needle in text, diagnostic
    for retired in ("probe_live_source", "probe_bundle", r"scripts\credential_gate.py", "authorize", "earned clear"):
        assert retired not in text, f"live-source skill names retired gate machinery {retired!r}"
    static_row = _table_row(SCRIPTS_README, "check_bundle_sources.py")
    assert "Offline, credential-free source checks" in static_row, "script index lost static source check"
    assert "M_PARAM_UNDEFINED" in static_row and "SOURCE_COLLAPSED" in static_row
    assert not _table_row(SCRIPTS_README, "probe_live_source.py"), "script index routes to retired live probe"


def test_host_refresh_contract_is_uia_default_with_operator_handoff() -> None:
    """One automated refresh (Desktop's own Refresh, then its guarded Save) plus a human handoff.

    The removed flags fail at argument parsing, so guidance that still teaches them is a guaranteed
    exit 2; the reload rule is the measured one (a dirty reload discards unsaved edits).
    """
    agents = " ".join(AGENTS_DOC.read_text(encoding="utf-8").split())
    assert "refresh strategy (`uia` default or `operator` human handoff only)" in agents, "AGENTS lost uia default"
    runbook = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
    for needle, diagnostic in (
        ("`uia` (default automated route)", "runbook lost uia default"),
        ("`operator` (human-driven Desktop handoff only)", "runbook lost operator handoff"),
        ("REFRESH_STATUS", "runbook lost progress lines"),
        ("fail at argument parsing with exit 2", "runbook lost removed-flag exit"),
    ):
        assert needle in runbook, diagnostic
    skill = " ".join(PBIP_REFRESH_SKILL.read_text(encoding="utf-8").split())
    for needle, diagnostic in (
        ("`process_jobs=0` is not completion", "refresh skill lost typed completion"),
        ("--save-only --pid <pid> --canaries", "refresh skill lost save-only"),
        ("bridge reports `hasUnsavedChanges: false`; otherwise stop for a human", "refresh skill lost clean reload"),
        ("A refresh without `--automatic-source-scope` is operator-only.", "refresh skill lost operator-only"),
    ):
        assert needle in skill, diagnostic
    for retired in REMOVED_REFRESH_MACHINERY:
        assert retired not in skill, f"refresh skill teaches removed machinery {retired}"
        assert retired not in runbook, f"runbook teaches removed machinery {retired}"
    assert _table_row(CAPABILITY_WIRING, "scripts/open_desktop.py"), "wiring lost open_desktop row"


@pytest.mark.parametrize(
    "case",
    [
        (
            LIVE_SOURCE_SKILL,
            "**A dialog is a human stop.**",
            "**A dialog can be dismissed.**",
            test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop,
            "live-source skill lost human stop",
        ),
        (
            LIVE_SOURCE_SKILL,
            "PASS `data-evidence` row",
            "PASS structural row",
            test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop,
            "live-source skill lost data evidence",
        ),
        (
            LIVE_SOURCE_SKILL,
            "same-run PASS `data-evidence` row",
            "prior-run PASS `data-evidence` row",
            test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop,
            "live-source skill lost same-run evidence",
        ),
        (
            LIVE_SOURCE_SKILL,
            "trusted engine-emission snapshot, never the live model",
            "live model copied into a matching snapshot",
            test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop,
            "live-source skill lost trusted source identity",
        ),
        (
            LIVE_SOURCE_SKILL,
            "Structural refusals do **not** prove missing credentials",
            "Structural refusals prove missing credentials",
            test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop,
            "live-source skill lost structural stop distinction",
        ),
        (
            LIVE_SOURCE_SKILL,
            "Never drop the flag or switch refresh routes",
            "Drop the flag or switch refresh routes",
            test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop,
            "live-source skill lost fail-closed qualification",
        ),
        (
            LIVE_SOURCE_SKILL,
            "[<table> ...] --automatic-source-scope <expected-sources.json>",
            "[<table> ...] [--automatic-source-scope <expected-sources.json>]",
            test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop,
            "live-source skill lost mandatory automatic source scope",
        ),
        (
            SCRIPTS_README,
            "| `check_bundle_sources.py` |",
            "| `probe_live_source.py` |",
            test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop,
            "script index lost static source check",
        ),
        (
            LIVE_SOURCE_SKILL,
            "--pid <pid> --canaries",
            "--pid <pid> --ui-refresh --canaries",
            test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop,
            "live-source skill lost canary refresh",
        ),
        (
            LIVE_SOURCE_SKILL,
            "A refresh without `--automatic-source-scope` remains operator-only",
            "A refresh without `--automatic-source-scope` is automatic",
            test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop,
            "live-source skill lost operator-only consent",
        ),
        (
            AGENTS_DOC,
            "refresh strategy (`uia` default",
            "refresh strategy (`scripted` default",
            test_host_refresh_contract_is_uia_default_with_operator_handoff,
            "AGENTS lost uia default",
        ),
        (
            RUNBOOK,
            "`uia` (default automated route)",
            "`scripted` (default automated route)",
            test_host_refresh_contract_is_uia_default_with_operator_handoff,
            "runbook lost uia default",
        ),
        (
            PBIP_REFRESH_SKILL,
            "reports `hasUnsavedChanges: false`; otherwise stop for a human",
            "reports any state; reload anyway",
            test_host_refresh_contract_is_uia_default_with_operator_handoff,
            "refresh skill lost clean reload",
        ),
        (
            PBIP_REFRESH_SKILL,
            "A refresh without `--automatic-source-scope` is",
            "A refresh without `--automatic-source-scope` is not",
            test_host_refresh_contract_is_uia_default_with_operator_handoff,
            "refresh skill lost operator-only",
        ),
        (
            PBIP_REFRESH_SKILL,
            "`--verify-only` is read-only.",
            "`--verify-only` is read-only. Use `--calculate-only` after DAX edits.",
            test_host_refresh_contract_is_uia_default_with_operator_handoff,
            "refresh skill teaches removed machinery --calculate-only",
        ),
        (
            CAPABILITY_WIRING,
            "| `scripts/open_desktop.py` | `advanced` | Skill shim; `--refresh` is reached through `t2p refresh`. |",
            "",
            test_host_refresh_contract_is_uia_default_with_operator_handoff,
            "wiring lost open_desktop row",
        ),
    ],
    ids=lambda case: case[-1],
)
def test_compact_contract_reversals_fail_the_intended_assertion(
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[Path, str, str, Callable[[], None], str],
) -> None:
    """Mutate only in-memory guidance; an import error or a different guard is not a kill."""
    path, before, after, check, diagnostic = case
    original_read = Path.read_text
    original = original_read(path, encoding="utf-8")
    assert original.count(before) == 1, f"reversal must select exactly one rule: {before}"
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda source, *args, **kwargs: (
            original.replace(before, after) if source == path else original_read(source, *args, **kwargs)
        ),
    )
    with pytest.raises(AssertionError, match=re.escape(diagnostic)):
        check()


def test_compact_contract_noop_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reversal seam must accept the unchanged source through the same read path."""
    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", original_read)
    test_credential_guidance_states_only_what_the_desktop_arbiter_establishes()
    test_live_source_skill_routes_through_the_shipped_model_and_a_human_stop()
    test_host_refresh_contract_is_uia_default_with_operator_handoff()
