"""Pinning tests for tiny upstream-engine repro fixtures (``fixtures/upstream-repros``).

Each test's docstring states its direction: a defect-direction pin (#171) fails when upstream fixes
the defect; a post-fix regression guard (#166, #168) fails if the defect returns. Either way a failure
is a signal: verify against the current canonical engine, then update the expectation and
``PINNED_ENGINE_VERSION``. Version drift is a non-fatal warning (#486) so a bump never hides the pins.
Engine-backed tests skip cleanly when the optional engine plugin is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import engine_source  # noqa: E402  # pylint: disable=wrong-import-position

FIXTURES = REPO / "fixtures" / "upstream-repros"
RUN_ROOT = REPO / ".pytest_cache" / "upstream-repro-pins"
PINNED_ENGINE_VERSION = "2.368.0"
SIMULATE_ENGINE_ABSENT = "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS"

_ENGINE_RUN: dict[str, Any] | None = None


def _contract() -> Path | None:
    """The canonical engine root, or None when the deterministic tier is not installed."""
    if os.environ.get(SIMULATE_ENGINE_ABSENT):
        return None
    try:
        return engine_source.engine_root()
    except engine_source.EngineNotFoundError:
        return None


ENGINE_SKIP_REASON = "deterministic tier not installed"


def requires_engine(test):
    """Mark an engine-dependent test and skip it when the canonical engine is absent."""
    test = pytest.mark.engine_dependency(expected_skip_reason=ENGINE_SKIP_REASON)(test)
    return pytest.mark.skipif(_contract() is None, reason=ENGINE_SKIP_REASON)(test)


def _observed_version() -> str:
    run = _ENGINE_RUN
    return str(run["version"]) if run else "unknown"


def _pin_message(issue: str, expectation: str, direction: str) -> str:
    return (
        f"Upstream repro pin {issue} changed from the expectation pinned at engine "
        f"{PINNED_ENGINE_VERSION}: {expectation}. Observed on canonical engine "
        f"{_observed_version()}. This is a signal, not necessarily a local bug: "
        f"{direction}. Verify against the current canonical engine, then update this test's expectation "
        "and pinned engine version."
    )


def _run_engine_once() -> dict[str, Any]:
    global _ENGINE_RUN  # pylint: disable=global-statement
    if _ENGINE_RUN is not None:
        return _ENGINE_RUN

    engine = _contract()
    if engine is None:  # pragma: no cover - requires_engine handles normal collection-time absence.
        pytest.skip("deterministic tier not installed")

    version = engine_source.engine_version(engine)
    if version != PINNED_ENGINE_VERSION:
        # Deliberately NOT an assert: see the module docstring. Failing here short-circuits every
        # behaviour pin in this file, which is how #166/#168/#171 went unevaluated for weeks.
        warnings.warn(
            f"Canonical engine is {version}, but these pins were last verified at "
            f"{PINNED_ENGINE_VERSION}. The behaviour assertions below still run and are the real "
            "signal; re-verify them and update PINNED_ENGINE_VERSION once you have.",
            UserWarning,
            stacklevel=2,
        )

    if RUN_ROOT.exists():
        shutil.rmtree(RUN_ROOT)
    RUN_ROOT.mkdir(parents=True)

    cmd = [
        sys.executable,
        str(engine_source.engine_scripts_dir(engine) / "migrate_estate.py"),
        "-i",
        str(FIXTURES),
        "-o",
        str(RUN_ROOT),
    ]
    completed = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True, timeout=180, check=False)
    assert completed.returncode == 0, (
        "The upstream repro harness could not run the canonical engine. This is a harness failure, "
        "not a pinned behaviour change.\n"
        f"Command: {cmd}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )

    report = json.loads((RUN_ROOT / "report.json").read_text(encoding="utf-8"))
    workbooks = {workbook["name"]: workbook for workbook in report["workbooks"]}
    _ENGINE_RUN = {"root": RUN_ROOT, "version": version, "report": report, "workbooks": workbooks}
    return _ENGINE_RUN


def _workbook(slug: str) -> dict[str, Any]:
    return _run_engine_once()["workbooks"][slug]


def _model_root(slug: str) -> Path:
    run_root = _run_engine_once()["root"]
    return next((run_root / "pbip" / slug).glob("*.SemanticModel"))


def _model_table_text(slug: str, table_name: str) -> str:
    return (_model_root(slug) / "definition" / "tables" / f"{table_name}.tmdl").read_text(encoding="utf-8")


def _semantic_text(slug: str) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((_model_root(slug) / "definition").rglob("*.tmdl"))
    )


def _report_visual_json(slug: str) -> list[dict[str, Any]]:
    run_root = _run_engine_once()["root"]
    report_root = run_root / "pbip" / slug / f"{slug}.Report" / "definition" / "pages"
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(report_root.rglob("visual.json"))]


def _source_ref_entities(value: Any) -> list[str]:
    if isinstance(value, dict):
        entities = []
        if set(value) >= {"SourceRef"} and isinstance(value["SourceRef"], dict):
            entity = value["SourceRef"].get("Entity")
            if isinstance(entity, str):
                entities.append(entity)
        for child in value.values():
            entities.extend(_source_ref_entities(child))
        return entities
    if isinstance(value, list):
        return [entity for child in value for entity in _source_ref_entities(child)]
    return []


@requires_engine
def test_issue_168_pins_partial_dispatcher_with_disclosure() -> None:
    """Post-fix regression guard (#486): #168 shipped, so a return to the all-or-nothing stub is a REGRESSION.

    The engine emits a partial dispatcher that keeps the three live branches, drops the unresolvable
    one, and discloses the drop in a TranslatedBy annotation.
    """
    slug = "issue-168-case-one-bad-branch"
    measures = _model_table_text(slug, "_Measures")
    handoff_requests = _workbook(slug)["model_translation_handoff"]["requests"]

    assert "measure 'Selected KPI' = BLANK()" not in measures, _pin_message(
        "#168",
        "the dispatcher regressed to the all-or-nothing BLANK() stub that #168 fixed",
        "this is a REGRESSION direction: 14 working branches were previously discarded with one bad one",
    )
    for branch in ("'Orders'[SALES]", "'Orders'[PROFIT]", "'Orders'[QUANTITY]"):
        assert branch in measures, _pin_message(
            "#168",
            f"the live dispatcher branch {branch} is no longer preserved",
            "this failing means partial emission stopped keeping the branches that DO translate",
        )
    assert "MISSING_METRIC" not in measures.split("annotation TableauFormula")[0], _pin_message(
        "#168",
        "the unresolvable branch leaked into the emitted DAX instead of being dropped",
        "the source formula is still recorded in the TableauFormula annotation; only the DAX drops it",
    )
    assert "3 of 4 branches live" in measures and 'dropped WHEN "Bad Branch"' in measures, _pin_message(
        "#168",
        "the partial dispatcher no longer DISCLOSES which branch it dropped",
        "silent partial emission is worse than the original stub: it reads as missing data, not a defect",
    )
    assert not handoff_requests, _pin_message(
        "#168",
        "a handoff request reappeared for a dispatcher that now translates",
        "this failing may mean the dispatcher stubbed again; read the TMDL before editing this test",
    )


@requires_engine
def test_issue_166_pins_negative_custom_sql_binding_shape() -> None:
    """Ordinary-direction pin: a future wrong base-table PBIR binding is a regression."""
    slug = "issue-166-custom-sql-disambiguation"
    upgraded = _model_table_text(slug, "Custom SQL Query (Upgrade Aircraft Installs)")
    workbook = _workbook(slug)
    entities = [entity for visual in _report_visual_json(slug) for entity in _source_ref_entities(visual)]

    assert "column TAIL" in upgraded and "column NEW_TECHNOLOGY" in upgraded, _pin_message(
        "#166/#164",
        "the disambiguated custom-SQL model table still carries TAIL and NEW_TECHNOLOGY",
        "this failing may mean the synthetic negative fixture stopped bounding the upstream issue",
    )
    assert "Custom SQL Query" not in entities, _pin_message(
        "#166/#164",
        "no PBIR visual binds the disambiguated worksheet to the base Custom SQL Query table",
        "this failing may mean a future engine introduced the wrong-binding regression this fixture guards",
    )
    first_warning = workbook["viz_fidelity"][0]
    assert "TAIL (Custom SQL Query (Upgrade Aircraft Installs))" in first_warning["reason"], _pin_message(
        "#166/#164",
        "the report layer still fails closed/skips the disambiguated TAIL field instead of binding it wrongly",
        "this failing may mean the engine now emits a visual; verify whether it is correct or a regression",
    )


@requires_engine
def test_issue_171_pins_partial_measure_names_parameter_gap() -> None:
    """Partial-gap pin: generated field-parameter support should make this fail for review."""
    slug = "issue-171-measure-names-parameter"
    measures = _model_table_text(slug, "_Measures")
    semantic = _semantic_text(slug)
    reasons = "\n".join(item["reason"] for item in _workbook(slug)["viz_fidelity"])

    assert "measure 'Selected Measure Value' = IF(EXACT([Select Measure Value]" in measures, _pin_message(
        "#171",
        "the parameter-driven calculated measure still translates successfully",
        "this failing may mean the partial #171 baseline changed; inspect the generated TMDL",
    )
    assert "NAMEOF(" not in semantic and "ParameterMetadata" not in semantic, _pin_message(
        "#171",
        "no Power BI field-parameter table is emitted for virtual Measure Names",
        "this failing may mean upstream added field-parameter support; verify, then update the pin",
    )
    assert "Measure Values shelf could not be enumerated to member measures" in reasons, _pin_message(
        "#171",
        "the Measure Names/Values worksheet remains skipped/deferred",
        "this failing may mean upstream can now enumerate/rebuild the Measure Values shelf",
    )
