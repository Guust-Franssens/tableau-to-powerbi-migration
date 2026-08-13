"""Regression tests for the three-migration-tree layout.

The repo has three trees that do NOT share a depth:

    examples/<slug>/                  <- this repo's worked examples (read-only reference)
    migrations/workbooks/<slug>/      <- the user's own workbook migrations
    migrations/datasources/<slug>/    <- the user's own published-data-source migrations

Every test here exists because the 16 committed examples all live in the ONE-level tree, so a bug
that only affects the two-level user trees is invisible to every other test in the suite.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
# pylint: disable=wrong-import-position
from published_datasource_registry import _by_path_from_report, _near_misses, _normalize_key
from set_data_folder import ABSOLUTE_USER_PATH_RE, _tree_and_slug_for
from tableau_lineage import dedup_key

TREES = ("examples", "migrations/workbooks", "migrations/datasources")
# Every script that walks the trees, by PATH rather than by import: two of them now ship inside the
# `powerbi-ai-readiness` skill, and `scripts/` keeps same-named forwarding shims. A bare `import
# set_ai_instructions` here would bind the shim (which scans nothing) AND poison `sys.modules` for
# the bundled suite that has to import the real one.
TREE_SCANNERS = (
    REPO_ROOT / "scripts" / "set_data_folder.py",
    REPO_ROOT / ".github/skills/powerbi-ai-readiness/scripts/set_ai_instructions.py",
    REPO_ROOT / ".github/skills/powerbi-ai-readiness/scripts/check_ai_readiness.py",
)


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
    for script in TREE_SCANNERS:
        assert script.exists(), f"{script} moved - repoint this test at its new home"
        source = script.read_text(encoding="utf-8")
        for tree in TREES:
            assert f'"{tree}"' in source, f"{script.name} does not scan {tree}"


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


def test_every_script_is_documented_in_the_scripts_readme() -> None:
    """`scripts/` is 20+ files; an undocumented one is a file nobody can find.

    This is not pedantry - it is a regression test. Five scripts had drifted to zero references
    anywhere in the repo (two of them a coherent corpus-harvesting workflow, two the only way to
    regenerate committed docs artifacts, one genuinely superseded and now deleted). Nothing failed;
    they were simply invisible. The README is the index that fixes that, and this keeps it honest -
    otherwise the next script added is undocumented from birth and the index rots into a lie.
    """
    readme = REPO_ROOT / "scripts" / "README.md"
    assert readme.is_file(), "scripts/README.md is missing - it is the index for this folder"
    listed = readme.read_text(encoding="utf-8")

    tracked = subprocess.run(
        ["git", "ls-files", "scripts"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    scripts = [Path(p) for p in tracked if Path(p).name != "README.md"]
    assert scripts, "no tracked scripts found - this guard now proves nothing"

    undocumented = sorted(p.name for p in scripts if p.name not in listed)
    assert not undocumented, (
        f"{undocumented} are tracked in scripts/ but absent from scripts/README.md - "
        "add a row describing what each does and when it runs. "
        "NOTE: this reads `git ls-files`, so a brand-new script is invisible to it until you "
        "`git add` it - run pytest AFTER staging, or CI will catch what your local run did not."
    )


def test_tracked_hook_configs_do_not_point_at_missing_scripts() -> None:
    """A tracked hook that names a gitignored probe script breaks the next clean clone.

    The subagentStart probe was intentionally kept out of the public repo, but its tracked hook config
    was left behind. The failure is invisible until the hook fires, so the layout test now checks every
    script path embedded in a tracked hook command.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z", ".github/hooks/*.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    configs = [REPO_ROOT / rel for rel in sorted(filter(None, tracked.split("\0")))]
    assert configs, "no tracked hook configs found - this guard now proves nothing"

    missing = []
    for config in configs:
        payload = json.loads(config.read_text(encoding="utf-8"))
        commands = [
            entry.get(shell, "")
            for event_entries in payload.get("hooks", {}).values()
            for entry in event_entries
            for shell in ("powershell", "bash")
        ]
        references = [
            match for command in commands for match in re.findall(r"scripts[/\\][\w./\\-]+\.(?:py|ps1|sh)", command)
        ]
        for reference in references:
            script = REPO_ROOT / Path(reference.replace("\\", "/"))
            if not script.is_file():
                rel_config = config.relative_to(REPO_ROOT).as_posix()
                missing.append(f"{rel_config}: {reference}")

    assert not missing, "tracked hook config(s) reference missing script(s):\n  " + "\n  ".join(missing)


# The newest `visualContainer` schema version that actually RESOLVES, measured 2026-08-13 by direct
# fetch: 2.10.0 through 2.16.0 all 404, 2.9.0 and below return 200. Raising this constant is a
# DELIBERATE act that requires re-measuring first:
#     curl -I https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/<v>/schema.json
# Deliberately a pinned number rather than a live fetch - the suite is ~1100 offline tests and a
# network call in CI is its own outage.
NEWEST_RESOLVING_VISUAL_CONTAINER_SCHEMA = (2, 9, 0)

# Where a schema URL can be *stored* (PBIR artifacts) or *produced* (generators). Markdown is
# excluded on purpose: the docs exist to WARN about the dead versions, so they must be able to name
# one (`.github/pbi.kb/visual-cookbook.md` tabulates every 404 version).
SCHEMA_BEARING_GLOBS = (
    "*.json",
    "*.pbir",
    "*.pbip",
    "*.pbism",
    "*.platform",
    "*.js",
    "*.mjs",
    "*.cjs",
    "*.ts",
    "*.py",
    "*.ps1",
    "*.sh",
    "*.ipynb",
)

_VISUAL_CONTAINER_SCHEMA_RE = re.compile(
    r"json-schemas/fabric/item/report/definition/visualContainer/(\d+)\.(\d+)\.(\d+)/schema\.json"
)


def test_no_committed_file_declares_an_unresolvable_visual_container_schema() -> None:
    """A dead `$schema` URL does not fail loudly - it silently DISABLES validation.

    `powerbi-report-author validate` cannot fetch a 404 schema, so it skips JSON-schema checking for
    that visual entirely, still prints `0 error(s)`, and leaves one `PBIR_SCHEMA_UNREACHABLE` warning
    as the only trace. Measured 2026-08-13 with the identical defect (`"x": "NOT_A_NUMBER"`) in one
    visual: at the dead `2.11.0` it reports `0 errors, succeededWithWarnings`; at `2.9.0` it reports
    `1 error, result=failed` (`/position/x must be number`). So a broken encoding ships green.

    Why this is a repo-wide gate and not a one-off cleanup: the same defect has now been fixed three
    times in the same issue (#131). First the 25 `.github/pbi.kb/visuals/*.visual.json` entries; then
    776 `visual.json` files under `examples/**`, because the cookbook's own green table routes
    copiers to the examples instead; then the two committed GENERATORS
    (`examples/airline-alliance-activity/_work/build.js`,
    `examples/quadruple-axis-charts/report_build/build_report.mjs`), which re-emitted 198 and 57
    dead-schema visuals on the next `node build.js` - re-introducing the defect after the artifacts
    were clean. Fixing an artifact without fixing its generator leaves a live source of regression,
    so this checks both.
    """
    patterns = ["git", "ls-files", "-z", *SCHEMA_BEARING_GLOBS]
    tracked = subprocess.run(patterns, cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    files = [REPO_ROOT / rel for rel in sorted(filter(None, tracked.split("\0")))]
    assert files, "no tracked schema-bearing files found - this guard now proves nothing"

    seen = 0
    dead = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in _VISUAL_CONTAINER_SCHEMA_RE.finditer(content):
            seen += 1
            version = tuple(int(part) for part in match.groups())
            if version > NEWEST_RESOLVING_VISUAL_CONTAINER_SCHEMA:
                rel = path.relative_to(REPO_ROOT).as_posix()
                dead.append(f"{rel}: {'.'.join(str(part) for part in version)}")

    assert seen, "no visualContainer schema URL found anywhere - the glob list has drifted, so this guard is a no-op"
    newest = ".".join(str(part) for part in NEWEST_RESOLVING_VISUAL_CONTAINER_SCHEMA)
    assert not dead, (
        f"committed file(s) declare a visualContainer schema newer than {newest}, which does not resolve; "
        "validation is silently SKIPPED for every visual they produce. Fix the value (and the generator "
        "that emits it, not just its output). If Microsoft has since published a newer schema, re-measure "
        f"with `curl -I` and raise NEWEST_RESOLVING_VISUAL_CONTAINER_SCHEMA:\n  " + "\n  ".join(sorted(set(dead)))
    )


def test_no_committed_file_leaks_an_absolute_user_path() -> None:
    """`ABSOLUTE_USER_PATH_RE` was only ever unit-tested against a synthetic string - never applied
    to the repo it is meant to protect. This applies it.

    Why it matters, measured 2026-08-01: an agent building ONE migration ran the localize step
    repo-wide and rewrote the `DataFolder` of all 16 committed example models from the portable
    `<REPO_ROOT>\\...` placeholder to `C:\\Users\\<name>\\...`. The full suite still passed. Committing
    that would have broken every example for every other contributor and published a local username
    path. A migration-scoped operation escaping to repo scope is invisible without this gate.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.tmdl", "*.json", "*.pbism", "*.pbir", "*.pbip"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = [REPO_ROOT / rel for rel in sorted(filter(None, tracked.split("\0")))]
    assert files, "no tracked model files found - this guard now proves nothing"

    leaks = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hit = ABSOLUTE_USER_PATH_RE.search(content)
        if hit:
            leaks.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {hit.group(0)!r}")

    assert not leaks, (
        "committed file(s) contain an absolute user path; run `python scripts/set_data_folder.py "
        "--sanitize` (scoped to the migration you touched) before committing:\n  " + "\n  ".join(leaks)
    )
