"""The estate coordinator turns an engine run into something safe to hand downstream.

Every test here corresponds to a measured gap in the deterministic tier's output contract, not to a
hypothetical. The engine is not at fault for any of them - it is a batch migrator and its choices are
defensible for that job. They are simply not safe for a CONSUMER, which is what this script is.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_estate  # noqa: E402  # pylint: disable=wrong-import-position


def _report(workbooks=None, dod_status="pass", gates=None) -> dict:
    """A minimal report.json in the engine's real shape."""
    return {
        "tool": "tableau-fabric-skills",
        "generated_at": "2026-08-06T00:00:00Z",
        "source": {"kind": "folder", "root": "in"},
        "pending_gates": gates or [],
        "definition_of_done": {
            "applicable": True,
            "status": dod_status,
            "reports_bound": 1,
            "reports_failed": 0,
            "reports_warned": 0,
            "workbooks_total": 1,
        },
        "summary": {"workbook_calcs_stubbed": 0, "visuals_warned": 0},
        "workbooks": workbooks if workbooks is not None else [],
    }


def _workbook(name: str, model: str, requests: list[dict] | None = None) -> dict:
    return {
        "name": name,
        "bound_model": model,
        "model_translation_handoff": {"requests": requests or []},
        "viz_fidelity": [],
    }


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The reason this script exists at all
# ---------------------------------------------------------------------------


def test_a_failed_definition_of_done_is_not_a_pass() -> None:
    """The engine prints [FAIL] and then returns 0 anyway.

    `migrate_estate.py` ends with `# Soft-but-loud: exit stays 0` and an unconditional `return 0`.
    That is deliberate on its side - one bad workbook should not fail a batch - but a consumer that
    gates on the exit code silently accepts a failed migration. This is the single check that most
    justifies the coordinator being code rather than an instruction an agent must remember.
    """
    ok, detail = run_estate.check_definition_of_done(_report(dod_status="failed"))
    assert ok is False
    assert "failed" in detail


def test_warn_is_allowed_through() -> None:
    """`warn` is the NORMAL state of a real migration - deferred visuals, stubbed calcs.

    Blocking on it would make the coordinator useless on every workbook that has any gap, which is
    all of them. Only `failed` blocks.
    """
    ok, _ = run_estate.check_definition_of_done(_report(dod_status="warn"))
    assert ok is True


def test_a_run_without_a_definition_of_done_is_not_failed() -> None:
    """A datasource-only run has no report to bind, so DoD is not applicable. That is not a failure."""
    report = _report()
    report["definition_of_done"] = {"applicable": False}
    ok, detail = run_estate.check_definition_of_done(report)
    assert ok is True
    assert "not applicable" in detail


# ---------------------------------------------------------------------------
# The latent hazard: --approved-dax is estate-global and name-keyed
# ---------------------------------------------------------------------------


def test_same_calc_name_in_two_models_is_a_collision() -> None:
    """`_load_approved_dax` returns a flat {name: DAX} map with no model scoping.

    Measured on six real workbooks: 10 stubbed calcs, 0 collisions - but the names were
    `Calculation2` (Tableau's auto-generated default), `Rank`, `Size`, `Running Sum`. Latent, not
    observed, which is exactly when a cheap check earns its place.
    """
    report = _report(
        workbooks=[
            _workbook("Sales WB", "SalesModel", [{"name": "Running Sum", "formula": "RUNNING_SUM(SUM([A]))"}]),
            _workbook("HR WB", "HrModel", [{"name": "Running Sum", "formula": "RUNNING_SUM(SUM([B]))"}]),
        ]
    )
    collisions = run_estate.find_approval_collisions(report)
    assert "running sum" in collisions
    assert len(collisions["running sum"]) == 2


def test_the_same_name_twice_in_one_model_is_not_a_collision() -> None:
    """A collision is (same name, DIFFERENT model). One model cannot land the wrong DAX in itself."""
    report = _report(
        workbooks=[
            _workbook(
                "One WB",
                "OneModel",
                [{"name": "Rank", "formula": "RANK()"}, {"name": "Rank", "formula": "RANK()"}],
            )
        ]
    )
    assert not run_estate.find_approval_collisions(report)


def test_collision_detection_is_case_insensitive() -> None:
    """The upstream loader is a plain dict keyed by the author's spelling; ours must not be fooled."""
    report = _report(
        workbooks=[
            _workbook("A", "ModelA", [{"name": "Calculation2", "formula": "X"}]),
            _workbook("B", "ModelB", [{"name": "calculation2", "formula": "Y"}]),
        ]
    )
    assert "calculation2" in run_estate.find_approval_collisions(report)


def test_collision_carries_the_formulas_so_a_human_can_judge() -> None:
    """Identical formulas under one name are harmless; differing formulas land the WRONG DAX.

    The check must not force a caller to go and look this up - the distinction is the whole decision.
    """
    report = _report(
        workbooks=[
            _workbook("A", "ModelA", [{"name": "Size", "formula": "SUM([X])"}]),
            _workbook("B", "ModelB", [{"name": "Size", "formula": "SUM([X])"}]),
        ]
    )
    claims = run_estate.find_approval_collisions(report)["size"]
    assert len({c["formula"] for c in claims}) == 1


# ---------------------------------------------------------------------------
# Generated artifact fingerprints: downstream edits must be visible
# ---------------------------------------------------------------------------


def test_generated_artifact_manifest_records_only_stable_generated_files(tmp_path: Path) -> None:
    """A normal refresh writes .pbi/cache.abf; that must not look like artifact tampering."""
    _write(tmp_path / "fabric" / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl", "table Orders")
    _write(tmp_path / "fabric" / "M.SemanticModel" / ".pbi" / "cache.abf", "refresh cache")
    _write(tmp_path / "fabric" / "R.Report" / "definition" / "report.json", "{}")
    _write(tmp_path / "fabric" / "R.Report" / ".pbi" / "localSettings.json", "{}")
    _write(tmp_path / "fabric" / "Book.pbip", "{}")
    _write(tmp_path / "_probe" / "Probe.pbip", "{}")

    run_estate.write_generated_artifact_manifest(tmp_path)

    manifest = json.loads((tmp_path / "input_manifest.json").read_text(encoding="utf-8"))
    recorded = set(manifest["generated_artifacts"]["files"])
    assert "fabric/M.SemanticModel/definition/tables/Orders.tmdl" in recorded
    assert "fabric/R.Report/definition/report.json" in recorded
    assert "fabric/Book.pbip" in recorded
    assert "fabric/M.SemanticModel/.pbi/cache.abf" not in recorded
    assert "fabric/R.Report/.pbi/localSettings.json" not in recorded
    assert "_probe/Probe.pbip" not in recorded


def test_generated_artifact_manifest_ignores_foreign_roots_from_before_this_run(tmp_path: Path) -> None:
    """A landing run must not bless stale roots the engine did not recreate."""
    stale = _write(tmp_path / "fabric" / "Stale.Report" / "definition" / "report.json", "{}")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    started = time.time() - 1
    fresh = _write(tmp_path / "fabric" / "Fresh.Report" / "definition" / "report.json", "{}")
    assert fresh.stat().st_mtime >= started

    run_estate.write_generated_artifact_manifest(tmp_path, _report(), earliest_mtime=started)

    manifest = json.loads((tmp_path / "input_manifest.json").read_text(encoding="utf-8"))
    recorded = set(manifest["generated_artifacts"]["files"])
    assert "fabric/Fresh.Report/definition/report.json" in recorded
    assert "fabric/Stale.Report/definition/report.json" not in recorded


# ---------------------------------------------------------------------------
# Slicing: the estate report must never enter a per-workbook agent's context
# ---------------------------------------------------------------------------


def test_each_workbook_gets_its_own_slice(tmp_path: Path) -> None:
    """~14 KB/workbook measured, so a 29-workbook estate is ~400 KB of mostly-irrelevant context."""
    report = _report(workbooks=[_workbook("Alpha", "AlphaModel"), _workbook("Beta", "BetaModel")])
    written = run_estate.slice_handovers(report, tmp_path)
    assert len(written) == 2
    names = {p.stem for p in written}
    assert names == {"Alpha", "Beta"}


def test_a_slice_carries_its_own_workbook_and_no_sibling(tmp_path: Path) -> None:
    """A slice that leaked a sibling would defeat the point of slicing."""
    report = _report(workbooks=[_workbook("Alpha", "AlphaModel"), _workbook("Beta", "BetaModel")])
    run_estate.slice_handovers(report, tmp_path)
    alpha = json.loads((tmp_path / "handover" / "Alpha.json").read_text(encoding="utf-8"))
    assert alpha["workbook"]["name"] == "Alpha"
    assert "Beta" not in json.dumps(alpha)


def test_a_slice_keeps_the_estate_facts_a_workbook_agent_needs(tmp_path: Path) -> None:
    """Slicing must not strip the gates - an agent that cannot see them cannot offer them."""
    gates = [{"gate": "dashboard_audit", "count": 3}]
    report = _report(workbooks=[_workbook("Alpha", "AlphaModel")], gates=gates)
    run_estate.slice_handovers(report, tmp_path)
    alpha = json.loads((tmp_path / "handover" / "Alpha.json").read_text(encoding="utf-8"))
    assert alpha["estate"]["pending_gates"] == gates
    assert alpha["estate"]["definition_of_done_status"] == "pass"


def test_a_workbook_name_with_path_characters_cannot_escape_the_folder(tmp_path: Path) -> None:
    """Workbook names come from a customer file name, so they are untrusted input."""
    report = _report(workbooks=[_workbook("../../evil", "M")])
    written = run_estate.slice_handovers(report, tmp_path)
    assert len(written) == 1
    assert written[0].parent == tmp_path / "handover"


# ---------------------------------------------------------------------------
# Phase timings
# ---------------------------------------------------------------------------


def test_phase_timings_are_persisted_with_a_total(tmp_path: Path) -> None:
    """Not a telemetry system - the session store already has tokens and duration per turn.

    What it cannot know is which migration PHASE a turn belonged to. This supplies only that, and it
    is what lets the retrospective say "where did the time go" instead of "what did we learn".
    """
    phases = [
        {"phase": "engine_run", "elapsed_sec": 120.0, "exit_code": 0},
        {"phase": "slice_handovers", "elapsed_sec": 0.4, "count": 6},
    ]
    path = run_estate.write_phase_record(tmp_path, phases)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["total_elapsed_sec"] == 120.4
    assert [p["phase"] for p in data["phases"]] == ["engine_run", "slice_handovers"]


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_a_missing_report_is_a_loud_failure(tmp_path: Path) -> None:
    """A silent empty result here would be indistinguishable from a clean estate."""
    with pytest.raises(FileNotFoundError, match="no report.json"):
        run_estate.read_report(tmp_path)


def test_the_coordinator_never_emits_model_content() -> None:
    """Architectural guard: this script runs the engine and reads its report. It is not a migrator.

    If it ever starts writing TMDL or PBIR the tier split has been violated, and that is far easier
    to catch here than in review.
    """
    source = Path(run_estate.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # skip the module docstring, which explains these very terms
    for forbidden in ("write_model_folder", "write_local_pbip", ".tmdl", "visual.json"):
        assert forbidden not in body, f"coordinator must not emit model content ({forbidden})"
