"""Pinning tests for tiny upstream-engine repro fixtures.

These tests intentionally assert the canonical deterministic engine's CURRENT behaviour for
``fixtures/upstream-repros``. They are not normal pass-after-fix regressions. For an upstream defect
such as #168, the expected value is the defect observed at the pinned engine version: while upstream
is broken the test passes; when upstream fixes it the test FAILS. Treat that failure as a signal to
verify the new engine output, then update the expectation and the pinned engine version below.

The engine is an optional installed Copilot plugin, not a repo dependency, so engine-backed tests use
the existing ``requires_engine`` skip pattern and skip cleanly when the deterministic tier is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
PINNED_ENGINE_VERSION = "2.260.0"
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


requires_engine = pytest.mark.skipif(_contract() is None, reason="deterministic tier not installed")


def _pin_message(issue: str, expectation: str, direction: str) -> str:
    return (
        f"Upstream repro pin {issue} changed from the expectation pinned at engine "
        f"{PINNED_ENGINE_VERSION}: {expectation}. This is a signal, not necessarily a local bug: "
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
    assert version == PINNED_ENGINE_VERSION, _pin_message(
        "fixture harness",
        f"engine version {PINNED_ENGINE_VERSION}",
        "the engine version moved; re-run and decide whether the pinned behaviours still describe reality",
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


def test_engine_absence_contract_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same contract shape used by ``requires_engine`` becomes None when the plugin is absent."""

    def missing_engine() -> Path:
        raise engine_source.EngineNotFoundError("simulated missing engine")

    monkeypatch.setattr(engine_source, "engine_root", missing_engine)
    assert _contract() is None


@requires_engine
def test_issue_168_pins_current_dispatcher_stub_defect() -> None:
    """Defect-direction pin: upstream fixing #168 should make this test fail loudly."""
    slug = "issue-168-case-one-bad-branch"
    measures = _model_table_text(slug, "_Measures")
    handoff_requests = _workbook(slug)["model_translation_handoff"]["requests"]

    assert "measure 'Selected KPI' = BLANK()" in measures, _pin_message(
        "#168",
        "the dispatcher measure is still stubbed to BLANK() when one CASE branch is unresolved",
        "this failing may mean upstream #168 is FIXED; check whether valid branches are now preserved",
    )
    assert len(handoff_requests) == 1, _pin_message(
        "#168",
        "exactly one handoff request is emitted for Selected KPI",
        "this failing may mean upstream changed the #168 remediation surface; verify before editing",
    )
    assert handoff_requests[0]["fallback_reason"] == "unresolved/ambiguous field [MISSING_METRIC]", _pin_message(
        "#168",
        "the handoff is still attributed to the unresolved [MISSING_METRIC] branch",
        "this failing may mean upstream #168 is FIXED or classified differently; verify the generated report.json",
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
