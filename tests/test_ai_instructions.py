"""Tests for the AI-readiness layer (scripts/set_ai_instructions.py).

Two factual errors shipped in this repo's only two hand-written `ai-instructions.md` files, and
both were found by human/model review rather than by any check:

  * wind-energy described `'NL Densification'` - a disconnected spiral-geometry scaffold - as
    "geography for the map";
  * it told Copilot to compare by "ANSP", a term belonging to an entirely different migration.

Instructions that name the wrong object are worse than no instructions: they actively steer Copilot
to the wrong table or to a concept that does not exist. These tests lock in the checks added in
response, and the gate's ability to FAIL - it previously exited 0 unconditionally, so it could never
have blocked anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
from set_ai_instructions import (
    lint_instructions,
    model_object_names,
    read_qna_enabled,
    unresolved_references,
)

WIND_MODEL = REPO_ROOT / "examples/wind-energy-utilization/fabric/WindEnergyUtilization.SemanticModel"


def test_model_object_names_reads_the_tmdl() -> None:
    names = model_object_names(WIND_MODEL)
    assert len(names) > 50, "should find the model's tables/columns/measures"
    assert "NL Densification" in names


def test_fabricated_references_are_detected() -> None:
    """The class of error that shipped: naming an object the model does not declare."""
    known = model_object_names(WIND_MODEL)
    text = "Use `'Nonexistent Table'` for the map and report [Fake Measure]."
    assert unresolved_references(text, known) == ["Fake Measure", "Nonexistent Table"]


def test_placeholders_are_not_flagged() -> None:
    """Templates and globs are deliberate, not claims about the schema - flagging them cries wolf."""
    known = model_object_names(WIND_MODEL)
    text = "`'* Parameter'` tables are proxies; `[<metric> Delta]` is the pattern; `[... Cars Offset]`."
    assert unresolved_references(text, known) == []


def test_shipped_instructions_reference_only_real_objects() -> None:
    """Every hand-written ai-instructions.md must resolve against its own model.

    This is the regression guard for the two errors above.
    """
    offenders: dict[str, list[str]] = {}
    for md in sorted(REPO_ROOT.glob("examples/*/ai-instructions.md")):
        models = sorted((md.parent / "fabric").glob("*.SemanticModel"))
        if not models:
            continue
        missing = unresolved_references(md.read_text(encoding="utf-8"), model_object_names(models[0]))
        if missing:
            offenders[md.parent.name] = missing
    assert not offenders, f"ai-instructions reference objects that do not exist: {offenders}"


def test_lint_flags_unresolved_references() -> None:
    known = model_object_names(WIND_MODEL)
    warnings = lint_instructions("# X\n\n## Things to avoid\n- Use `'Ghost Table'`.\n", known)
    assert any("does NOT declare" in w for w in warnings)


def test_stamped_models_have_qna_enabled() -> None:
    """A stamped model with qnaEnabled false is a SILENT no-op - Copilot ignores the instructions."""
    for model in sorted(REPO_ROOT.glob("examples/*/fabric/*.SemanticModel")):
        culture = model / "definition" / "cultures" / "en-US.tmdl"
        if not culture.exists() or "CustomInstructions" not in culture.read_text(encoding="utf-8"):
            continue
        assert read_qna_enabled(model) is True, f"{model.name} is stamped but Q&A is disabled"


def test_scaffold_uses_a_real_linguistic_schema_version() -> None:
    """ "4.2.0" was copy-pasted from definition.pbism, a different schema.

    Power BI-generated culture files in this repo use 2.0.0 / 1.0.0; an unrecognised version risks
    the service silently dropping the payload.
    """
    source = (REPO_ROOT / "scripts" / "set_ai_instructions.py").read_text(encoding="utf-8")
    assert '"Version": "2.0.0"' in source
    assert '"Version": "4.2.0"' not in source
    for culture in REPO_ROOT.glob("examples/*/fabric/*.SemanticModel/definition/cultures/*.tmdl"):
        assert '"Version": "4.2.0"' not in culture.read_text(encoding="utf-8"), culture
