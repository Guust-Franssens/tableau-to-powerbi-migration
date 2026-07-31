"""Regression tests for repo-local skills under `.github/skills/`.

Why this file exists: a skill is *documentation that an agent acts on*, so the two ways it rots are
both invisible at review time. `docs/agent-architecture.md` §8 names one of them outright -
"Referencing tools, scripts or paths that do not exist. Cheap to catch mechanically; worth a test."

The other is subtler and is the whole point of packaging this knowledge as a skill: the
`pbip-model-refresh` skill promises the procedure is portable to another BI migration (Qlik, Cognos)
by copying **two files**. That promise is only true while `refresh_pbip_model.py` imports nothing
from `scripts/` except `probe_desktop_query`. One innocent `from tableau_lineage import ...` would
silently make the skill wrong everywhere it was copied, so the claim is checked rather than trusted.
"""

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / ".github" / "skills"
SKILL_FILES = sorted(SKILLS_DIR.glob("*/SKILL.md"))

# A markdown link whose target is a relative path, i.e. not http(s):, mailto: or a #fragment.
MD_LINK_RE = re.compile(r"\[[^\]]*\]\((?!https?:|mailto:|#)([^)\s]+)\)")


def test_the_skills_directory_is_not_empty() -> None:
    """Guards the collection itself: `SKILL_FILES` drives parametrization, and an empty glob would
    turn every test below into a silent no-op that still reports green."""
    assert SKILL_FILES, f"no SKILL.md found under {SKILLS_DIR}"


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


def _repo_module_imports(script: Path, siblings: set[str]) -> set[str]:
    """Names `script` imports that resolve to another module in the same folder."""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in siblings:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name in siblings)
    return found


def test_the_refresh_procedure_stays_portable_to_other_migration_repos() -> None:
    """`pbip-model-refresh` tells the reader to copy exactly two files. Keep that true.

    Nothing about refreshing and persisting a PBIP is Tableau-specific - the input is already a Power
    BI model - so this pair is meant to move to a Qlik or Cognos migration repo (or a global skill
    location) unchanged. A new import from `scripts/` would break that silently: the copy would still
    look fine in review and only fail at runtime, in the other repo.
    """
    scripts = REPO_ROOT / "scripts"
    siblings = {path.stem for path in scripts.glob("*.py")}
    portable = {"probe_desktop_query", "refresh_pbip_model"}

    for name in sorted(portable):
        imported = _repo_module_imports(scripts / f"{name}.py", siblings)
        assert imported <= portable, (
            f"scripts/{name}.py imports {sorted(imported - portable)} from scripts/, "
            "which breaks the 'copy these two files' claim in .github/skills/pbip-model-refresh/SKILL.md"
        )
