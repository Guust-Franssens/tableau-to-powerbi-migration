"""Tests for the AI-readiness layer bundled in this skill.

Two factual errors shipped in the first two hand-written `ai-instructions.md` files, and both were
found by human/model review rather than by any check:

  * wind-energy described `'NL Densification'` - a disconnected spiral-geometry scaffold - as
    "geography for the map";
  * it told Copilot to compare by "ANSP", a term belonging to an entirely different migration.

Instructions that name the wrong object are worse than no instructions: they actively steer Copilot
to the wrong table or to a concept that does not exist. These tests lock in the checks added in
response, and the gate's ability to FAIL - it previously exited 0 unconditionally, so it could never
have blocked anything.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import check_ai_readiness
import pytest
import set_ai_instructions
from set_ai_instructions import (
    cmd_check,
    host_root,
    lint_instructions,
    model_object_names,
    read_qna_enabled,
    unresolved_references,
)

SKILL_ROOT = Path(__file__).resolve().parents[1]


def _example_models() -> list[Path]:
    """The host repo's committed `examples/` corpus, if it has one.

    Ground truth for a fabricated-reference check has to come from real models. They are a HOST-repo
    fixture, though, not part of the skill: a Qlik or Cognos repo that copies this folder has no
    `examples/` tree, so the corpus tests must skip with a reason there rather than fail (or, worse,
    silently parametrize to nothing). `tests/test_skills.py` in this repo asserts the corpus is
    non-empty HERE, so the skip cannot quietly turn into a no-op where it is meant to run.
    """
    for parent in Path(__file__).resolve().parents:
        models = sorted((parent / "examples").glob("*/fabric/*.SemanticModel"))
        if models:
            return models
    return []


EXAMPLE_MODELS = _example_models()
WIND_MODEL = next((m for m in EXAMPLE_MODELS if m.name == "WindEnergyUtilization.SemanticModel"), None)
needs_wind = pytest.mark.skipif(WIND_MODEL is None, reason="no wind-energy example model in this repo")
needs_corpus = pytest.mark.skipif(not EXAMPLE_MODELS, reason="no examples/*/fabric/*.SemanticModel corpus")


def test_the_suite_exercises_the_scripts_bundled_beside_it() -> None:
    """A copied skill must test ITS OWN scripts, not a same-named module from the host repo.

    This repo keeps forwarding shims at `scripts/set_ai_instructions.py` (so existing agent
    invocations do not break), which is exactly the shape that could shadow the real modules if
    anything put the host repo's `scripts/` on `sys.path` first. Then every test below would pass
    while proving nothing about the files that actually ship.
    """
    for module in (set_ai_instructions, check_ai_readiness):
        assert Path(module.__file__).resolve().parent == SKILL_ROOT / "scripts", (
            f"{module.__name__} was imported from {module.__file__}, not from this skill's scripts/"
        )


@needs_wind
def test_model_object_names_reads_the_tmdl() -> None:
    names = model_object_names(WIND_MODEL)
    assert len(names) > 50, "should find the model's tables/columns/measures"
    assert "NL Densification" in names


@needs_wind
def test_fabricated_references_are_detected() -> None:
    """The class of error that shipped: naming an object the model does not declare."""
    known = model_object_names(WIND_MODEL)
    text = "Use `'Nonexistent Table'` for the map and report [Fake Measure]."
    assert unresolved_references(text, known) == ["Fake Measure", "Nonexistent Table"]


@needs_wind
def test_placeholders_are_not_flagged() -> None:
    """Templates and globs are deliberate, not claims about the schema - flagging them cries wolf."""
    known = model_object_names(WIND_MODEL)
    text = "`'* Parameter'` tables are proxies; `[<metric> Delta]` is the pattern; `[... Cars Offset]`."
    assert unresolved_references(text, known) == []


@needs_corpus
def test_shipped_instructions_reference_only_real_objects() -> None:
    """Every hand-written ai-instructions.md must resolve against its own model.

    This is the regression guard for the two errors above.
    """
    offenders: dict[str, list[str]] = {}
    for model in EXAMPLE_MODELS:
        md = model.parents[1] / "ai-instructions.md"
        if not md.exists():
            continue
        missing = unresolved_references(md.read_text(encoding="utf-8"), model_object_names(model))
        if missing:
            offenders[md.parent.name] = missing
    assert not offenders, f"ai-instructions reference objects that do not exist: {offenders}"


@needs_wind
def test_lint_flags_unresolved_references() -> None:
    known = model_object_names(WIND_MODEL)
    warnings = lint_instructions("# X\n\n## Things to avoid\n- Use `'Ghost Table'`.\n", known)
    assert any("does NOT declare" in w for w in warnings)


@needs_corpus
def test_stamped_models_have_qna_enabled() -> None:
    """A stamped model with qnaEnabled false is a SILENT no-op - Copilot ignores the instructions."""
    for model in EXAMPLE_MODELS:
        culture = model / "definition" / "cultures" / "en-US.tmdl"
        if not culture.exists() or "CustomInstructions" not in culture.read_text(encoding="utf-8"):
            continue
        assert read_qna_enabled(model) is True, f"{model.name} is stamped but Q&A is disabled"


def test_scaffold_uses_a_real_linguistic_schema_version() -> None:
    """ "4.2.0" was copy-pasted from definition.pbism, a different schema.

    Power BI-generated culture files use 2.0.0 / 1.0.0. Measured 2026-07-30: a model published to
    Fabric with "4.2.0" round-tripped through `getDefinition` with CustomInstructions intact, so this
    is a correctness tidy-up rather than a fix for an observed data loss.
    """
    source = (SKILL_ROOT / "scripts" / "set_ai_instructions.py").read_text(encoding="utf-8")
    assert '"Version": "2.0.0"' in source
    assert '"Version": "4.2.0"' not in source
    for model in EXAMPLE_MODELS:
        for culture in model.glob("definition/cultures/*.tmdl"):
            assert '"Version": "4.2.0"' not in culture.read_text(encoding="utf-8"), culture


def test_host_root_finds_the_repo_that_owns_the_migrations(tmp_path: Path) -> None:
    """The scripts ship inside a skill folder, so their depth below the repo root is not fixed.

    A hard-coded `parents[N]` resolves to the skill folder itself, where every glob matches nothing -
    `--check` then reports "no models found" and exits 0: a clean pass over a repo it never read.
    """
    root = tmp_path / "host"
    (root / "migrations" / "workbooks").mkdir(parents=True)
    deep = root / ".github" / "skills" / "powerbi-ai-readiness" / "scripts"
    deep.mkdir(parents=True)
    assert host_root(deep / "set_ai_instructions.py") == root
    assert check_ai_readiness.host_root(deep / "check_ai_readiness.py") == root


def test_host_root_does_not_invent_a_root_when_there_is_no_migration_tree(tmp_path: Path) -> None:
    """Copied into a repo with no migration tree, it must fall back rather than climb to the drive."""
    lonely = tmp_path / "somewhere" / "scripts"
    lonely.mkdir(parents=True)
    assert host_root(lonely / "set_ai_instructions.py") == Path.cwd()


def _fake_model(root: Path, slug: str, name: str, *, stamped: bool) -> Path:
    model = root / "examples" / slug / "fabric" / f"{name}.SemanticModel"
    (model / "definition" / "cultures").mkdir(parents=True)
    if stamped:
        payload = json.dumps({"Version": "2.0.0", "CustomInstructions": "Latest = the max date in the data."})
        body = "\n".join("\t\t\t" + line for line in payload.splitlines())
        (model / "definition" / "cultures" / "en-US.tmdl").write_text(
            f"cultureInfo en-US\n\n\tlinguisticMetadata =\n{body}\n\n\t\tcontentType: json\n", encoding="utf-8"
        )
        (model / "definition.pbism").write_text(
            json.dumps({"version": "4.2.0", "settings": {"qnaEnabled": True}}), encoding="utf-8"
        )
    return model


def test_check_can_be_scoped_to_one_model(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """`--strict` is only usable by an agent if it can gate the model it just built.

    Repo-wide `--strict` fails on every model that predates this layer, so an unscoped gate is one
    nobody can ever turn on - which is why the gate added alongside `--strict` was never wired
    anywhere. Scoping is what turns it from a slogan into a hand-off check.
    """
    root = tmp_path / "host"
    stamped = _fake_model(root, "good", "Good", stamped=True)
    bare = _fake_model(root, "legacy", "Legacy", stamped=False)

    with caplog.at_level(logging.INFO):
        assert cmd_check(root, strict=True) == 1, "repo-wide strict must still fail on the legacy model"
        caplog.clear()
        assert cmd_check(root, strict=True, model_dir=stamped) == 0, "scoped strict must pass on the stamped model"
        scoped_report = caplog.text
        caplog.clear()
        assert cmd_check(root, strict=True, model_dir=bare) == 1, "scoped strict must fail on the bare model"
    assert "Legacy.SemanticModel" not in scoped_report, "a scoped check must not report models outside its scope"
