"""Guard current Power BI skill routes and their migration safety boundaries (#728)."""

from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_plugin  # noqa: E402  # pylint: disable=wrong-import-position

RETIRED = re.compile(
    r"(?<![\w-])(powerbi-report-(?:planning|design|authoring|management)|check-updates|"
    r"semantic-model-consumption|powerbi-authoring-cli|powerbi-consumption-cli|"
    r"powerbi-semantic-model-authoring)(?![\w-])"
)
# Exact frozen 2026-07-31 evidence, not a present-day instruction to load this name.
HISTORICAL = {
    (
        "docs/agent-architecture.md",
        '   | `skill("powerbi-report-authoring")` (plugin) | **SUCCESS**, base dir under '
        r"`.copilot\installed-plugins\fabric-collection\powerbi-authoring\` |",
    )
}
PERSONAS = ("pbi-report-builder", "pbi-migration-validator")


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _persona(name: str) -> str:
    return _text(f".github/agents/{name}.agent.md")


def test_retired_skill_census() -> None:
    """Check guidance paths and contents; exclude provenance examples/migrations and negative tests."""
    paths = [ROOT / name for name in ("AGENTS.md", "README.md", "CONTRIBUTING.md")]
    for pattern in (
        ".github/agents/*.agent.md",
        ".github/skills/**/SKILL.md",
        ".github/pbi.kb/**/*.md",
        "docs/**/*.md",
        "scripts/**/*.py",
        "scripts/**/*.ps1",
        "scripts/**/*.md",
    ):
        paths.extend(ROOT.glob(pattern))
    found = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        assert not RETIRED.search(relative), f"Retired skill path: {relative}"
        found.extend((relative, line) for line in path.read_text(encoding="utf-8").splitlines() if RETIRED.search(line))
    assert sorted(found) == sorted(HISTORICAL), f"Retired skill references: {found}"


def test_playbook_release_contract() -> None:
    """Changed published skill content requires the independently approved 0.3.1 release."""
    assert build_plugin.VERSION == "0.3.1"


def test_current_modes_and_approval_stop() -> None:
    """Existing repair is direct; greenfield cannot silently become a build."""
    builder = _persona(PERSONAS[0])
    validator = _persona(PERSONAS[1])
    for text in (builder, validator, _text("README.md")):
        assert "powerbi-report-cli" in text
    assert re.search(r"existing report.*directly.*`authoring`", builder, re.I)
    assert re.search(r"missing page.*`design`.*then `authoring`", builder, re.I)
    assert "references/<mode>.md" in builder
    assert "mode parameter" in builder
    assert re.search(r"greenfield.*`planning`", builder, re.I)
    assert "_brief/report-spec.md" in builder
    assert "approval_pending" in builder
    assert re.search(r"end the turn.*explicit.*later.*approval", builder, re.I | re.S)
    assert re.search(r"no.*(?:edit|scaffold).*publish", builder, re.I)
    assert "locked spec" in builder and "migration brief" in builder
    assert re.search(r"`authoring`.*read-only.*inventory/preview/screenshots", validator, re.I)
    assert re.search(r"never edit.*plan.*design", validator, re.I)
    assert "run-owned" in validator


@pytest.mark.parametrize("name", PERSONAS)
def test_no_management_or_mid_migration_install(name: str) -> None:
    """A prohibitive clause, not mere presence of 'latest' and 'preflight', is required."""
    text = _persona(name)
    assert re.search(r"never.*`management`.*publish.*rebind", text, re.I)
    assert re.search(r"skip.*@latest.*install", text, re.I)
    assert re.search(r"never.*install/update npm mid-migration", text, re.I)
    assert re.search(r"(?:absent|missing|unavailable).*BLOCKED.*preflight", text, re.I)
    assert "sole version authority" in text and "preflight" in text
    assert not re.search(r"\bnpm\s+(?:install|update|i|add)\b", text, re.I)


def test_current_references_and_query_surfaces() -> None:
    """Read-only EVALUATE routes to query tools, not a made-up consumption skill alias."""
    gotchas = _text(".github/skills/powerbi-report-gotchas/SKILL.md")
    for reference in ("expressions", "conditional-formatting"):
        assert f"references/authoring/{reference}.md" in gotchas
    assert not re.search(r"references/(?!authoring/|design/)[a-z-]+\.md", gotchas)
    guide = _text("docs/tableau-dax-translation-guide.md")
    query = guide.split("LOD expressions are the highest-risk", 1)[1].split("before trusting it.", 1)[0]
    for token in (
        "EVALUATE",
        "scripts/probe_desktop_query.py",
        "powerbi-modeling-mcp",
        "dax_query_operations",
        "powerbi-remote",
        "ExecuteQuery",
        "Python replica",
    ):
        assert token in query
    assert "semantic-model-authoring" not in query
    assert "references/design/chart-selection.md" in guide


def test_historical_probe_and_pending_current_probe() -> None:
    """Do not turn old evidence into a claimed successful new-name runtime probe."""
    doc = _text("docs/agent-architecture.md")
    assert "then-current retired name" in doc
    assert 'skill("powerbi-report-cli")' in doc
    assert "post-maintenance" in doc and "PENDING" in doc
    assert "date, CLI version, plugin version" in doc
    assert "not a successful runtime probe" in doc


@pytest.mark.parametrize(
    ("name", "baseline"),
    [("pbi-report-builder", 21902), ("pbi-migration-validator", 21799), ("tableau-migrator", 21951)],
)
def test_personas_are_net_negative(name: str, baseline: int) -> None:
    """Compare conservative CRLF size with reviewed #745 head 1823251d, also on Linux."""
    text = _persona(name)
    size = len(text.replace("\n", "\r\n"))
    assert size < baseline, f"{name}: {size} >= reviewed baseline {baseline}"
