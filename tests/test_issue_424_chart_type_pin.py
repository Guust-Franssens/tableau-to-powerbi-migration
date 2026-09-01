"""Defect-direction pin for issue #424 — an `Automatic` mark over a DISCRETE date part.

Tableau's own documentation (*Change the Type of Marks in the View*) says the Line mark is chosen
whenever **a date field** and a measure are the inner fields — *"If the dimension is a date
dimension, the Line mark is used instead"* — with no continuous/discrete qualification. The engine
instead gates on continuity (``twb_to_pbir.py:2366`` ``_has_continuous_date``, true only for a
``*-Trunc`` derivation), so a discrete date PART falls through to ``VT_COLUMN``. With a colour
dimension on the marks card that becomes Power BI's **stacked** ``columnChart``, which sums the
series — meaningless for the ratio measures this was found on.

This is a PIN ON THE DEFECT, like ``tests/test_upstream_repro_pins.py``: while upstream is broken it
passes; when upstream fixes it, ``test_variant_a_still_emits_the_stacked_column_defect`` FAILS. That
failure is the signal to verify the new output and retire this pin — not a local regression.

It deliberately does NOT assert an engine version. The shared harness in
``tests/test_upstream_repro_pins.py`` pins one, which makes every test in that file fail the moment
the canonical plugin moves; this file reports the observed version in its failure text instead, so
the behaviour it describes stays checkable on whatever engine is installed.
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

FIXTURE = REPO / "fixtures" / "upstream-repros" / "issue-424-automatic-mark-discrete-date"
RUN_ROOT = REPO / ".pytest_cache" / "issue-424-chart-type-pin"
SIMULATE_ENGINE_ABSENT = "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS"

VARIANT_A = "issue-424-a-discrete-date-part"
VARIANT_B = "issue-424-b-continuous-date-trunc"
VARIANT_C = "issue-424-c-explicit-line-mark"

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


def _signal(expectation: str, direction: str) -> str:
    run = _ENGINE_RUN or {}
    return (
        f"Upstream repro pin #424 changed: {expectation}. Observed on canonical engine "
        f"{run.get('version', 'unknown')}. This is a signal, not necessarily a local bug: {direction}. "
        "Verify against the current canonical engine before editing this test."
    )


def _run_engine_once() -> dict[str, Any]:
    global _ENGINE_RUN  # pylint: disable=global-statement
    if _ENGINE_RUN is not None:
        return _ENGINE_RUN

    engine = _contract()
    if engine is None:  # pragma: no cover - requires_engine handles normal collection-time absence.
        pytest.skip("deterministic tier not installed")

    if RUN_ROOT.exists():
        shutil.rmtree(RUN_ROOT)
    RUN_ROOT.mkdir(parents=True)

    cmd = [
        sys.executable,
        str(engine_source.engine_scripts_dir(engine) / "migrate_estate.py"),
        "-i",
        str(FIXTURE),
        "-o",
        str(RUN_ROOT),
    ]
    completed = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True, timeout=600, check=False)
    assert completed.returncode == 0, (
        "The #424 repro harness could not run the canonical engine. This is a harness failure, not a "
        f"pinned behaviour change.\nCommand: {cmd}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )

    report = json.loads((RUN_ROOT / "report.json").read_text(encoding="utf-8"))
    _ENGINE_RUN = {
        "version": engine_source.engine_version(engine),
        "workbooks": {workbook["name"]: workbook for workbook in report["workbooks"]},
    }
    return _ENGINE_RUN


def _dashboard_visuals(slug: str) -> list[dict[str, Any]]:
    """Every emitted ``visual.json`` for one fixture workbook, ordered by path."""
    _run_engine_once()
    pages = RUN_ROOT / "pbip" / slug / f"{slug}.Report" / "definition" / "pages"
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(pages.rglob("visual.json"))]


def _only_visual(slug: str) -> dict[str, Any]:
    visuals = _dashboard_visuals(slug)
    assert len(visuals) == 1, f"{slug} emitted {len(visuals)} visuals; the fixture declares exactly one zone"
    return visuals[0]


@requires_engine
def test_variant_a_still_emits_the_stacked_column_defect() -> None:
    """Automatic mark + DISCRETE date part -> ``columnChart``. Tableau draws a line."""
    visual = _only_visual(VARIANT_A)
    assert visual["visual"]["visualType"] == "columnChart", _signal(
        "an Automatic mark over a discrete date part no longer emits columnChart",
        "this failing may mean upstream FIXED #424; confirm the emitted type is lineChart, then retire this pin",
    )
    assert visual["visual"]["query"]["queryState"].get("Series", {}).get("projections"), _signal(
        "the colour dimension no longer lands in the Series well",
        "without a Series well the stacked-ratio consequence disappears and this fixture stops bounding the defect",
    )


@requires_engine
def test_the_two_controls_still_emit_a_line_chart() -> None:
    """The controls prove the fixture is otherwise sound: only variant A's one difference matters."""
    for slug, difference in ((VARIANT_B, "a continuous *-Trunc date"), (VARIANT_C, "an explicit Line mark")):
        assert _only_visual(slug)["visual"]["visualType"] == "lineChart", _signal(
            f"the control workbook with {difference} no longer emits lineChart",
            "a control changing means the fixture no longer isolates one variable; re-derive it before trusting "
            "the variant-A assertion",
        )


@requires_engine
def test_the_shared_visual_id_is_input_derived_not_an_engine_inconsistency() -> None:
    """#424 read a shared visual id across workbooks as engine inconsistency. It is not.

    ``_sanitize(f"v-{page_name}-{i}-{ws['name']}")`` hashes (dashboard name, zone index, worksheet
    name) and nothing else, so three workbooks that agree on those three things emit the identical
    id while disagreeing on ``visualType``.
    """
    names = {slug: _only_visual(slug)["name"] for slug in (VARIANT_A, VARIANT_B, VARIANT_C)}
    types = {slug: _only_visual(slug)["visual"]["visualType"] for slug in (VARIANT_A, VARIANT_B, VARIANT_C)}

    assert len(set(names.values())) == 1, _signal(
        f"the three workbooks no longer share one visual id (got {names})",
        "this failing may mean the engine now salts the visual name with something beyond "
        "(dashboard, zone index, worksheet); if so, a shared id becomes stronger evidence than it is today",
    )
    assert len(set(types.values())) == 2, _signal(
        f"the shared id no longer spans two different visualTypes (got {types})",
        "the falsification only holds while one id demonstrably carries both types",
    )


@requires_engine
def test_the_engine_reports_no_warning_for_the_defect() -> None:
    """The silence is the dangerous part: stacking is structurally valid, so nothing flags it."""
    workbook = _run_engine_once()["workbooks"][VARIANT_A]
    worksheet_rows = [row for row in workbook["viz_fidelity"] if row["visual_type"] == "column"]

    assert worksheet_rows, _signal(
        "no viz_fidelity row describes the emitted column visual",
        "this failing may mean the fidelity record changed shape; re-read report.json before editing",
    )
    assert all(row["status"] == "rebuilt" and not row["reason"] for row in worksheet_rows), _signal(
        f"the column visual is no longer reported clean (got {worksheet_rows})",
        "this failing may mean upstream now WARNS about the chart-type choice, which would be a partial fix",
    )
    assert not workbook["remediation_worklist"]["items"], _signal(
        "the remediation worklist is no longer empty for the defective workbook",
        "this failing may mean upstream now routes the chart-type choice to a human",
    )
