"""Regression tests for the three-migration-tree layout.

The repo has three trees that do NOT share a depth:

    examples/<slug>/                  <- this repo's worked examples (read-only reference)
    migrations/workbooks/<slug>/      <- the user's own workbook migrations
    migrations/datasources/<slug>/    <- the user's own published-data-source migrations

Every test here exists because the 16 committed examples all live in the ONE-level tree, so a bug
that only affects the two-level user trees is invisible to every other test in the suite.
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
import check_ai_readiness
import set_ai_instructions
import set_data_folder
from published_datasource_registry import _by_path_from_report, _near_misses, _normalize_key
from set_data_folder import ABSOLUTE_USER_PATH_RE, _tree_and_slug_for
from tableau_lineage import dedup_key

TREES = ("examples", "migrations/workbooks", "migrations/datasources")


def _expr(tree: str, slug: str) -> Path:
    return REPO_ROOT / tree / slug / "fabric" / "Model.SemanticModel" / "definition" / "expressions.tmdl"


@pytest.mark.parametrize(
    ("tree", "slug"),
    [
        ("examples", "health-tracker"),
        ("migrations/workbooks", "acme-sales"),
        ("migrations/datasources", "acme-shared-ds"),
    ],
)
def test_data_folder_keeps_the_slug_in_every_tree(tree: str, slug: str) -> None:
    """`set_data_folder` must not drop the slug in the deeper user trees.

    Indexing `parts[0], parts[1]` returned ("migrations", "workbooks") for every user migration,
    pointing every model at one non-existent data folder while looking perfectly fine for examples.
    """
    got_tree, got_slug = _tree_and_slug_for(_expr(tree, slug))
    assert got_slug == slug
    assert got_tree == tree.replace("/", "\\")


def test_all_three_trees_are_scanned_for_models() -> None:
    """A tree missing from a scanner is silently never localized / AI-stamped / audited."""
    for module in (set_data_folder, set_ai_instructions, check_ai_readiness):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for tree in TREES:
            assert f'"{tree}"' in source, f"{Path(module.__file__).name} does not scan {tree}"


def test_shared_model_by_path_hop_count() -> None:
    """A cross-tree `byPath` is resolved relative to the `.Report` FOLDER, so it takes four hops.

    From `migrations/workbooks/<slug>/fabric/<X>.Report/`: fabric -> <slug> -> workbooks -> migrations.
    Verified in Power BI Desktop (the shared model's table and columns loaded). A three-hop path
    escapes to the repo root and silently fails to resolve.
    """
    got = _by_path_from_report("migrations/datasources/sales-master/fabric/SalesMaster.SemanticModel")
    assert got == "../../../../datasources/sales-master/fabric/SalesMaster.SemanticModel"

    report_dir = Path("migrations/workbooks/ops-dash/fabric/Ops.Report")
    resolved = (report_dir / got).resolve()
    expected = (REPO_ROOT / "migrations/datasources/sales-master/fabric/SalesMaster.SemanticModel").resolve()
    assert resolved == (Path.cwd() / expected).resolve() or resolved.as_posix().endswith(
        "migrations/datasources/sales-master/fabric/SalesMaster.SemanticModel"
    )


@pytest.mark.parametrize(
    "leaked",
    [
        r"C:\Users\alice\vscode-projects\x",
        "C:/Users/Alice/repo",  # forward slashes, as Power Query M writes them
        r"C:\\Users\\Alice\\repo",  # JSON-escaped
        r"\\fileserver\Users\Alice\repo",  # UNC
        "/Users/alice/repo",  # macOS
        "/home/alice/repo",  # Linux
        r"C:\Users\user\repo",  # 'user' is a real, registrable account name
        r"C:\Users\username\repo",
        r"C:\Users\youssef\repo",
        r"D:\Users\jdoe\data",
    ],
)
def test_absolute_path_gate_still_catches_real_leaks(leaked: str) -> None:
    """The privacy gate on a public repo: a real machine path must never pass.

    The backslash-only original silently missed the forward-slash form - which is exactly how the
    committed `report_build/*.mjs` artifacts leaked a real username into the public repo.
    """
    assert ABSOLUTE_USER_PATH_RE.search(leaked), f"leak not detected: {leaked}"


@pytest.mark.parametrize(
    "placeholder",
    [
        r"C:\Users\...\path",
        r"C:\Users\<name>\path",
        r"C:\Users\<username>\path",
        "C:/Users/<you>/path",
        r"C:\Users\%USERPROFILE%\path",
    ],
)
def test_absolute_path_gate_ignores_documentation_placeholders(placeholder: str) -> None:
    """SECURITY.md and the READMEs must be able to SHOW the pattern they warn about.

    Only syntactically unambiguous placeholders are exempt; a bare `username` is a real account.
    """
    assert not ABSOLUTE_USER_PATH_RE.search(placeholder), f"placeholder wrongly flagged: {placeholder}"


def test_dedup_key_is_case_normalized_end_to_end() -> None:
    """A hand-typed `--register`/`--key` must match the parser's lowercased key.

    The parser emits '<site>/<name>' lowercased; registration used to store whatever casing the user
    typed, so 'Finance/Sales Master' silently failed to match 'finance/sales master' and a duplicate
    model got built - the exact outcome the registry exists to prevent.
    """
    assert _normalize_key("  Finance/Sales Master  ") == dedup_key("Finance", "Sales Master")
    assert _normalize_key("FINANCE/SALES MASTER") == "finance/sales master"


@pytest.mark.parametrize(
    ("workbook_key", "registered_key"),
    [
        ("finance/sales master", "finance/salesmaster"),  # space vs none
        ("finance/sales master", "finance/sales%20master"),  # percent-encoded survived
        ("finance/sales master", "Finance/Sales Master"),  # casing
        ("finance/sales-master", "finance/sales_master"),  # separator style
        ("finance/salesmaster", "finance/sales.master"),  # punctuation
    ],
)
def test_near_miss_keys_are_shouted_about_not_silently_ignored(workbook_key: str, registered_key: str) -> None:
    """The round trip cannot be tested without a live Tableau tenant - so it must fail LOUDLY.

    If the key derived from the workbook ever disagrees with the key registered from the .tds, a
    plain dict lookup reports "not yet migrated" and a duplicate model gets built with no error at
    all. Detecting the near-miss converts that silent, expensive failure into a visible one.
    """
    assert _near_misses(workbook_key, [registered_key]) == [registered_key]


@pytest.mark.parametrize(
    ("workbook_key", "registered_key"),
    [
        ("finance/sales master", "finance/orders"),  # genuinely different data source
        ("finance/sales master", "hr/sales master"),  # same name, DIFFERENT site
    ],
)
def test_near_miss_does_not_fire_for_genuinely_different_keys(workbook_key: str, registered_key: str) -> None:
    """It must not cry wolf: a different site or a different name is a real 'not yet migrated'."""
    assert _near_misses(workbook_key, [registered_key]) == []


def test_by_path_refuses_to_guess_for_a_model_outside_the_default_tree() -> None:
    """`--datasources-dir` can point anywhere; the four-hop shape is only valid in-tree.

    Emitting a plausible-but-wrong relative path would fail at bind time with no explanation.
    """
    assert _by_path_from_report("migrations/datasources/x/fabric/X.SemanticModel").startswith("../../../../")
    assert _by_path_from_report("some/other/place/x/fabric/X.SemanticModel") == ""
    assert _by_path_from_report("C:/elsewhere/x/fabric/X.SemanticModel") == ""


def test_privacy_gate_allowlist_stays_minimal() -> None:
    """Only files that *define or test* the pattern may be exempt from the leak scan.

    The exemption is a deliberate blind spot, so it must stay tiny: this test fails if someone
    silences the gate for a real source file instead of fixing the leak.
    """
    source = (REPO_ROOT / "scripts" / "set_data_folder.py").read_text(encoding="utf-8")
    exempt = re.search(r"if path\.name in \{([^}]*)\}", source)
    assert exempt, "the allowlist changed shape - re-check that the gate is still scoped"
    names = set(re.findall(r'"([^"]+)"', exempt.group(1)))
    assert names == {"set_data_folder.py", "test_repo_layout.py"}, f"unexpected exemptions: {names}"


def test_shared_conventions_come_after_the_agent_role() -> None:
    """GitHub's documented ordering for an agent profile is role first, then constraints.

    The block was originally inserted straight after the frontmatter, which pushed each agent's own
    `# <Name> — Subagent` identity down ~70 lines: it read 6 KB of generic cross-cutting rules before
    learning what it *is*. Identity should frame the rules, not the reverse.
    """
    for agent in sorted((REPO_ROOT / ".github" / "agents").glob("*.agent.md")):
        lines = agent.read_text(encoding="utf-8").splitlines()
        h1 = next(i for i, line in enumerate(lines) if line.startswith("# "))
        block = next(i for i, line in enumerate(lines) if "BEGIN:shared-conventions" in line)
        assert block > h1, f"{agent.name}: shared conventions precede the agent's own role statement"


def test_every_agent_carries_the_shared_conventions() -> None:
    """A subagent sees ONLY its persona, so a convention absent here simply does not apply to it."""
    agents = sorted((REPO_ROOT / ".github" / "agents").glob("*.agent.md"))
    assert agents
    for agent in agents:
        text = agent.read_text(encoding="utf-8")
        assert "BEGIN:shared-conventions" in text and "END:shared-conventions" in text, agent.name
        assert "NEVER block silently on an external system" in text, f"{agent.name} lacks the retry cap"


def test_orchestrator_has_a_retrospective_step() -> None:
    """Each migration must make the next one cheaper - that only happens if it is a gated step."""
    text = (REPO_ROOT / ".github" / "agents" / "tableau-migrator.agent.md").read_text(encoding="utf-8")
    assert "Retrospective — MANDATORY" in text
    # It must route learnings somewhere a subagent will actually read them, and stay within budget.
    for anchor in ("sync_agent_conventions.py", "visual-cookbook.md", "30,000-char", "net-zero growth"):
        assert anchor in text, f"retrospective step is missing its {anchor!r} guidance"


def test_customer_data_is_gitignored_in_every_tree() -> None:
    """Extracted data and downloaded sources must be ignored at BOTH tree depths.

    A fixed-depth rule (`migrations/*/data/`) stopped matching when the user trees gained a level,
    which would have let a `git add .` stage real customer data on a public repo.
    """
    ignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^\*\*/data/\s*$", ignore_text, re.MULTILINE), "**/data/ rule missing"
    assert re.search(r"^\*\*/_downloads/\s*$", ignore_text, re.MULTILINE), "**/_downloads/ rule missing"
    assert re.search(r"^\*\*/source/\*\.tdsx\s*$", ignore_text, re.MULTILINE), "**/source/*.tdsx rule missing"
    # A screenshot of a REAL customer dashboard is customer data; the example ones are public.
    for tree in ("workbooks", "datasources"):
        assert re.search(rf"^/migrations/{tree}/\*/reference/\s*$", ignore_text, re.MULTILINE), (
            f"customer reference screenshots not ignored for migrations/{tree}/"
        )
