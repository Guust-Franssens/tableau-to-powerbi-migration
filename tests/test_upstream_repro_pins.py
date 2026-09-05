"""Pinning tests for tiny upstream-engine repro fixtures.

These tests assert the canonical deterministic engine's CURRENT behaviour for
``fixtures/upstream-repros``. **Two opposite directions live in this file, and #168 has changed
sides** — read each test's own docstring rather than assuming one lifetime for all of them:

* **Defect-direction pins** hold the defect observed at the pinned engine version: while upstream is
  broken the test passes, and when upstream fixes it the test FAILS. ``#171`` is one — generated
  field-parameter support should break it, for review.
* **Post-fix regression guards** hold the FIXED behaviour: they pass today and fail if the defect
  returns. ``#168`` was reversed into one on 2026-09-03 against measured output at engine 2.356.0,
  then re-verified unchanged at 2.368.0 on 2026-09-05 — its defect-direction pin fired exactly as
  designed, upstream had shipped the fix, and the pin was turned around rather than deleted. ``#166``
  was always this direction.

Either way a failure is a signal, not a verdict: verify against the current canonical engine, then
update the expectation and the pinned engine version below.

The engine is an optional installed Copilot plugin, not a repo dependency, so engine-backed tests use
the existing ``requires_engine`` skip pattern and skip cleanly when the deterministic tier is absent.

⚠️ **The engine version is a SIGNAL, not a gate — and it used to be a gate, which broke this file.**
Until 2026-09-03 ``_run_engine_once`` ``assert``\\ ed ``version == PINNED_ENGINE_VERSION``. Because
every behaviour pin reaches the engine through that one function, a version bump did not merely
report drift: it made **every** pin in this file fail *before its own assertion ran*, so #166, #168
and #171 went **entirely unevaluated** from the moment the canonical plugin passed 2.260.0 — while
still costing three red tests that read as "expected". The failure text compounded it by printing the
pinned version on both sides of the comparison (*"changed from the expectation pinned at engine
2.260.0: engine version 2.260.0"*), so it never even disclosed what was actually installed.

That is the mechanism behind the standing "expect exactly six pre-existing engine-pin failures"
baseline: a version guard that fires on every release converts a designed alarm into background
noise, and a permanently-red test trains readers to skip the one message that mattered. Version drift
is now a non-fatal ``UserWarning`` and the observed version is reported in every pin message, so a
bump is disclosed **and** the behaviour pins still get to speak. Issue #486.
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


def test_engine_absence_contract_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same contract shape used by ``requires_engine`` becomes None when the plugin is absent."""

    def missing_engine() -> Path:
        raise engine_source.EngineNotFoundError("simulated missing engine")

    monkeypatch.setattr(engine_source, "engine_root", missing_engine)
    assert _contract() is None


@requires_engine
def test_issue_168_pins_partial_dispatcher_with_disclosure() -> None:
    """Post-fix regression guard: #168 shipped, so a return to the all-or-nothing stub is a REGRESSION.

    ⚠️ **Direction reversed on 2026-09-03 (issue #486), against measured output at engine 2.356.0,
    and re-verified unchanged at 2.368.0 on 2026-09-05.** This was a defect-direction pin asserting
    ``measure 'Selected KPI' = BLANK()``. It fired exactly as designed — and was then invisible for
    weeks behind the harness version gate (module docstring). Upstream
    ``Yarbrdab000/tableau-fabric-skills#168`` is CLOSED COMPLETED, and the current engine emits a
    **partial** dispatcher plus the disclosure the maintainer said was blocking the fix::

        measure 'Selected KPI' = IF(EXACT([Select KPI Value], "Sales"), SUM('Orders'[SALES]), ...)
        annotation TranslatedBy = deterministic (parameter dispatcher; 3 of 4 branches live;
                                  dropped WHEN "Bad Branch")

    So the three live branches are preserved, the unresolvable one is dropped rather than discarding
    the whole measure, and the drop is stated in the model where a debugger looks. Guarding all three
    directions: a silent revert to ``BLANK()``, a silent revert to a *complete* translation that
    invents the bad branch, and a partial that stops disclosing itself.
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
