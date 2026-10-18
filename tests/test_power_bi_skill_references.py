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
    """Stage the approved compact cores once; publication/installation is separately authorized."""
    assert build_plugin.VERSION == "0.3.2"


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
    """Selected mode/topic pointers are finite; disk checks do not prove fresh-role resolution."""
    gotchas = _text(".github/skills/powerbi-report-gotchas/SKILL.md")
    assert "Portable upstream procedures: installed `powerbi-report-cli`" in gotchas, "report upstream owner"
    assert "existing report → `authoring`; missing page → `design` then `authoring`" in gotchas, "report intent mode"
    assert "Modes select `references/<mode>.md`, not CLI flags" in gotchas, "selected-mode entry"
    assert set(re.findall(r"`(references/[^`]+\.md)`", gotchas)) == {
        "references/<mode>.md",
        *(
            f"references/authoring/{topic}.md"
            for topic in (
                "expressions",
                "conditional-formatting",
                "field-parameters",
                "slicers-part-02",
                "map",
                "model-binding",
                "screenshot-review",
            )
        ),
    }, "report authoring topics"
    semantic = _text(".github/skills/powerbi-semantic-model-gotchas/SKILL.md")
    assert "installed `semantic-model-authoring` existing-model workflow" in semantic, "semantic upstream owner"
    assert set(re.findall(r"`(references/[^`]+\.md)`", semantic)) == {
        "references/tmdl-guidelines.md",
        "references/authoring/field-parameters.md",
    }, "semantic parameter topics"
    assert "Until fresh S resolves both parameter topics, keep this constructor guard" in semantic
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


def test_compact_core_citations_keep_their_meaning() -> None:
    """Surviving numbers are not evidence that a citation still points to the same rule."""
    report = _persona("pbi-report-builder")
    semantic = _persona("pbi-semantic-builder")
    mapping = report.split("*idiom* through", 1)[1].split("\n6.", 1)[0]
    assert "`powerbi-report-gotchas` §8" in mapping, "mapping source-intent citation"
    assert "`powerbi-report-cli` authoring topic" in mapping, "mapping procedure owner"
    assert "§9" not in mapping, "removed report section"
    scale = report.split("7. **Every percentage/scaled", 1)[1].split("\n8.", 1)[0]
    assert "core §8" in scale and r"scripts\probe_desktop_query.py" in scale, "scale source/query citation"
    uniqueness = semantic.split("7. **Model-wide measure-name uniqueness", 1)[1].split("\n8.", 1)[0]
    assert r"scripts\check_datamodel.py" in uniqueness and "skill §2" in uniqueness, (
        "uniqueness executable-owner citation"
    )
    rows = semantic.split("`PERSISTED` alone", 1)[1].split("\n11.", 1)[0]
    assert "skill §4; host live-source owner" in rows, "row-proof owner citation"
    assert "Held out of the playbook; resolve" in semantic, "held-out refresh owner"


@pytest.mark.parametrize(
    ("path", "before", "after", "diagnostic"),
    [
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "installed `powerbi-report-cli`",
            "installed `missing-skill`",
            "report upstream owner",
        ),
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "installed `powerbi-report-cli`",
            "installed `powerbi-report-authoring`",
            "report upstream owner",
        ),
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "existing report → `authoring`",
            "existing report → `planning`",
            "report intent mode",
        ),
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "references/authoring/expressions.md",
            "references/expressions.md",
            "report authoring topics",
        ),
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "references/authoring/slicers-part-02.md",
            "references/authoring/slicers-part-20.md",
            "report authoring topics",
        ),
        (
            ".github/skills/powerbi-report-gotchas/SKILL.md",
            "`references/authoring/map.md`, ",
            "",
            "report authoring topics",
        ),
        (
            ".github/skills/powerbi-semantic-model-gotchas/SKILL.md",
            "installed `semantic-model-authoring`",
            "installed `missing-skill`",
            "semantic upstream owner",
        ),
        (
            ".github/skills/powerbi-semantic-model-gotchas/SKILL.md",
            "references/authoring/field-parameters.md",
            "references/design/field-parameters.md",
            "semantic parameter topics",
        ),
    ],
)
def test_missing_retired_or_wrong_mode_pointers_fail_their_owner_assertion(
    monkeypatch: pytest.MonkeyPatch, path: str, before: str, after: str, diagnostic: str
) -> None:
    """Direct pointer negatives, not a fabricated installed-skill registry or runtime canary."""
    original_read = Path.read_text
    target = ROOT / path
    source = original_read(target, encoding="utf-8")
    assert source.count(before) == 1
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda current, *args, **kwargs: (
            source.replace(before, after) if current == target else original_read(current, *args, **kwargs)
        ),
    )
    with pytest.raises(AssertionError, match=diagnostic):
        test_current_references_and_query_surfaces()


@pytest.mark.parametrize(
    ("name", "before", "after", "diagnostic"),
    [
        (
            "pbi-report-builder",
            "core §8; `scripts\\probe_desktop_query.py`",
            "core §3; `scripts\\probe_desktop_query.py`",
            "scale source/query citation",
        ),
        (
            "pbi-semantic-builder",
            "`scripts\\check_datamodel.py` (skill §2)",
            "`scripts\\check_datamodel.py` (skill §4)",
            "uniqueness executable-owner citation",
        ),
        (
            "pbi-report-builder",
            "*idiom* through `powerbi-report-gotchas` §8",
            "*idiom* through `powerbi-report-gotchas` §9",
            "mapping source-intent citation",
        ),
    ],
)
def test_same_number_or_removed_section_reversals(
    monkeypatch: pytest.MonkeyPatch, name: str, before: str, after: str, diagnostic: str
) -> None:
    """Reintroducing the old meaning must fail even if the old section number still exists."""
    original_read = Path.read_text
    target = ROOT / ".github" / "agents" / f"{name}.agent.md"
    source = original_read(target, encoding="utf-8")
    assert source.count(before) == 1
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda current, *args, **kwargs: (
            source.replace(before, after) if current == target else original_read(current, *args, **kwargs)
        ),
    )
    with pytest.raises(AssertionError, match=diagnostic):
        test_compact_core_citations_keep_their_meaning()


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
