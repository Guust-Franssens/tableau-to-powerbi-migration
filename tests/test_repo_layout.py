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
from published_datasource_registry import _by_path_from_report, _normalize_key
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
        r"C:\Users\gfranssens\vscode-projects\x",
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


def test_by_path_refuses_to_guess_for_a_model_outside_the_default_tree() -> None:
    """`--datasources-dir` can point anywhere; the four-hop shape is only valid in-tree.

    Emitting a plausible-but-wrong relative path would fail at bind time with no explanation.
    """
    assert _by_path_from_report("migrations/datasources/x/fabric/X.SemanticModel").startswith("../../../../")
    assert _by_path_from_report("some/other/place/x/fabric/X.SemanticModel") == ""
    assert _by_path_from_report("C:/elsewhere/x/fabric/X.SemanticModel") == ""


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
