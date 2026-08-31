r"""Tests for scripts/harvest_engine_gaps.py - the reports/-vs-pbip/ engine-gap harvest.

Three design choices here are load-bearing.

**No test creates a path longer than 259 characters.** A stock Windows runner cannot create one, so
the fixture rather than the assertion would fail. The git blind-spot behaviour is exercised by
driving a LOW `GIT_READABLE_PATH_MAX` over a short tree, which walks the identical code path. Same
substitution `tests/test_check_path_ceiling.py` uses, and for the same reason.

**Unreadability is injected, not simulated with permissions.** `chmod` is not portable to Windows and
an unreadable directory is not reliably creatable on CI, so `sha256_file` is monkeypatched to raise
for one named file. That reproduces the exact branch that matters: a file the harvest cannot hash.

**The provenance tests mutate AFTER the baseline is recorded.** That is the only way to reproduce the
distinction the whole module exists for - a byte the engine wrote versus a byte someone changed
afterwards - because both look identical to a plain diff.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "harvest_engine_gaps.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_migration_progress as cmp_mod  # noqa: E402  # pylint: disable=wrong-import-position
import harvest_engine_gaps as heg  # noqa: E402  # pylint: disable=wrong-import-position
import harvest_gap_report as hgr  # noqa: E402  # pylint: disable=wrong-import-position
import harvest_gap_shapes as hgs  # noqa: E402  # pylint: disable=wrong-import-position

VISUAL = "definition/pages/page-1/visuals/v1/visual.json"


def _visual(entity: str, *, position: int = 0) -> dict:
    """A minimal but structurally realistic PBIR visual bound to `entity`."""
    return {
        "position": {"x": position, "y": 0, "width": 100, "height": 100, "tabOrder": position},
        "visual": {
            "visualType": "columnChart",
            "query": {
                "queryState": {
                    "Category": {
                        "projections": [
                            {
                                "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": "A"}},
                                "queryRef": f"{entity}.A",
                                "nativeQueryRef": "A",
                            }
                        ]
                    }
                }
            },
        },
    }


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) if not isinstance(payload, str) else payload
    path.write_text(text, encoding="utf-8")


def _model(root: Path, table: str) -> None:
    """A minimal `.SemanticModel` folder whose only table is `table`."""
    _write(root / "definition" / "tables" / f"{table}.tmdl", f"table '{table}'\n\n\tcolumn A\n")
    _write(root / "definition.pbism", {"version": "4.0"})


def _report(root: Path, *, entity: str, model_relative: str, position: int = 0) -> None:
    """A minimal `.Report` folder with one visual and a dataset reference."""
    _write(root / "definition.pbir", {"version": "4.0", "datasetReference": {"byPath": {"path": model_relative}}})
    _write(root / VISUAL, _visual(entity, position=position))


def _record_baseline(bundle: Path, *, generated_at: str = "2026-08-30T00:00:00+00:00") -> None:
    """Record every current file as the engine's own output, exactly as run_estate.py does."""
    report = {"generated_at": generated_at}
    (bundle / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    files = {
        path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(bundle.rglob("*"))
        if path.is_file() and path.name not in {"report.json", "input_manifest.json"}
    }
    manifest = {
        "root": str(bundle),
        "generated_artifacts": {
            "version": 1,
            "run_id": "test-run",
            "recorded_at": "2026-08-30T00:00:00+00:00",
            "report_sha256": hashlib.sha256((bundle / "report.json").read_bytes()).hexdigest(),
            "report_generated_at": generated_at,
            "files": files,
        },
    }
    (bundle / "input_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _bundle(tmp_path: Path, *, entity_baseline: str = "Orders", entity_working: str = "Orders") -> Path:
    """One-unit bundle: engine baseline + bound working copy, both recorded."""
    bundle = tmp_path / "bundle"
    _report(bundle / "reports" / "WB.Report", entity=entity_baseline, model_relative="../WB.SemanticModel")
    _report(bundle / "pbip" / "WB" / "WB.Report", entity=entity_working, model_relative="../Sales.SemanticModel")
    _model(bundle / "pbip" / "WB" / "Sales.SemanticModel", "Orders")
    _model(bundle / "semantic_models" / "Sales.SemanticModel", "Orders")
    _record_baseline(bundle)
    return bundle


def _pair(report: dict, layer: str, artifact: str) -> dict:
    return next(p for p in report["pairs"] if p["layer"] == layer and p["artifact"] == artifact)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recorded(bundle: Path, target: str) -> str | None:
    """The engine-time hash of one bundle-relative path, straight from the manifest."""
    manifest = json.loads((bundle / "input_manifest.json").read_text(encoding="utf-8"))
    return manifest["generated_artifacts"]["files"].get(target)


def _declare(
    bundle: Path,
    target: str,
    *,
    kind: str = "changed",
    identity: str = "scripts/fix_layout.py",
    run_id: str = "test-run",
    baseline_sha256: str | None = "recorded",
    expected_sha256: str | None = "current",
) -> None:
    """Write ONE edit declaration in the exact shape the tamper gate accepts as proof.

    The sentinel defaults resolve to the real hashes, so a test that wants a STALE or FORGED
    declaration overrides just the field it is falsifying and nothing else changes.
    """
    if baseline_sha256 == "recorded":
        baseline_sha256 = _recorded(bundle, target)
    if expected_sha256 == "current":
        path = bundle / target
        expected_sha256 = _sha(path) if path.is_file() else None
    _write(
        bundle / "_build" / "generated-edit-declarations.json",
        {
            "version": 1,
            "declarations": [
                {
                    "version": 1,
                    "run_id": run_id,
                    "kind": kind,
                    "target": target,
                    "baseline_sha256": baseline_sha256,
                    "expected_sha256": expected_sha256,
                    "script_identity": identity,
                    "script_sha256": "0" * 64,
                }
            ],
        },
    )


# ---------------------------------------------------------------------------------------------
# Pairing and coverage. Unassessable input never lands in the clean bucket.
# ---------------------------------------------------------------------------------------------


def test_byte_identical_pair_reports_identical_and_no_differences(tmp_path):
    """The case nothing else tracks: the engine got this one completely right."""
    bundle = _bundle(tmp_path)
    # Make the two report trees byte-identical, including the dataset reference.
    _report(bundle / "reports" / "WB.Report", entity="Orders", model_relative="../Sales.SemanticModel")
    _record_baseline(bundle)

    report = heg.harvest(bundle)

    assert _pair(report, "report", "WB")["status"] == heg.PAIR_IDENTICAL
    assert report["provenance"]["differing_files"] == 0


def test_content_difference_is_reported_as_differs(tmp_path):
    report = heg.harvest(_bundle(tmp_path, entity_working="Orders"))
    assert _pair(report, "report", "WB")["status"] == heg.PAIR_DIFFERS
    assert report["provenance"]["differing_files"] > 0


def test_artifact_with_no_engine_baseline_is_not_counted_as_agreeing(tmp_path):
    """Issue #179: a missing baseline is a finding, never a silent pass."""
    bundle = _bundle(tmp_path)
    _report(bundle / "pbip" / "Extra" / "Extra.Report", entity="Orders", model_relative="../Sales.SemanticModel")
    _record_baseline(bundle)

    report = heg.harvest(bundle)

    assert _pair(report, "report", "Extra")["status"] == heg.PAIR_NO_BASELINE
    assert report["layers"]["report"]["unpaired_no_baseline"] == 1
    assert report["layers"]["report"]["identical"] == 0
    assert report["status"] == heg.STATUS_INCOMPLETE


def test_baseline_with_no_working_copy_is_reported(tmp_path):
    bundle = _bundle(tmp_path)
    _report(bundle / "reports" / "Orphan.Report", entity="Orders", model_relative="../Orphan.SemanticModel")
    _record_baseline(bundle)

    report = heg.harvest(bundle)

    assert _pair(report, "report", "Orphan")["status"] == heg.PAIR_NO_WORKING
    assert report["layers"]["report"]["unpaired_no_working"] == 1


def test_model_layer_pairs_on_model_name_not_unit_name(tmp_path):
    """Regression: `pbip/WB/` holds `Sales.SemanticModel`, not `WB.SemanticModel`.

    Pairing the model layer by UNIT name reported 21 of the estate's artifacts as having no engine
    baseline when 20 of them do - a coverage figure wrong by a factor of three.
    """
    report = heg.harvest(_bundle(tmp_path))
    entry = _pair(report, "model", "Sales")
    assert entry["unit"] == "WB"
    assert entry["status"] == heg.PAIR_IDENTICAL


def test_shared_model_is_compared_once_per_working_copy(tmp_path):
    """A published datasource copied into several units is several independent emissions."""
    bundle = _bundle(tmp_path)
    _report(bundle / "pbip" / "WB2" / "WB2.Report", entity="Orders", model_relative="../Sales.SemanticModel")
    _model(bundle / "pbip" / "WB2" / "Sales.SemanticModel", "Orders")
    _record_baseline(bundle)

    report = heg.harvest(bundle)

    model_pairs = [p for p in report["pairs"] if p["layer"] == "model" and p["artifact"] == "Sales"]
    assert sorted(p["unit"] for p in model_pairs) == ["WB", "WB2"]


def test_unreadable_file_is_unassessable_and_never_identical(tmp_path, monkeypatch):
    """A file that cannot be hashed is not a file that is the same."""
    bundle = _bundle(tmp_path)
    _report(bundle / "reports" / "WB.Report", entity="Orders", model_relative="../Sales.SemanticModel")
    _record_baseline(bundle)
    real = heg.sha256_file

    def explode(path: Path) -> str:
        if path.name == "visual.json":
            raise OSError("injected: unreadable")
        return real(path)

    monkeypatch.setattr(heg, "sha256_file", explode)
    report = heg.harvest(bundle)

    entry = _pair(report, "report", "WB")
    assert entry["status"] == heg.PAIR_UNASSESSABLE
    assert entry["unassessable"] > 0
    assert report["status"] == heg.STATUS_INCOMPLETE


def test_unreadable_file_is_not_reported_as_added_or_removed(tmp_path, monkeypatch):
    """The withdrawal has to work on POSIX-relative keys, or Windows separators defeat it."""
    bundle = _bundle(tmp_path)
    _report(bundle / "reports" / "WB.Report", entity="Orders", model_relative="../Sales.SemanticModel")
    _record_baseline(bundle)
    real = heg.sha256_file

    def explode(path: Path) -> str:
        if path.name == "visual.json" and "reports" in path.parts:
            raise OSError("injected: unreadable")
        return real(path)

    monkeypatch.setattr(heg, "sha256_file", explode)
    report = heg.harvest(bundle)

    entry = _pair(report, "report", "WB")
    assert entry["files"]["added"] == 0
    assert entry["files"]["removed"] == 0


# ---------------------------------------------------------------------------------------------
# Provenance. The whole point: a difference is not evidence until you know who wrote it.
# ---------------------------------------------------------------------------------------------


def test_engine_written_difference_is_engine_internal_not_a_tier_edit(tmp_path):
    """Both sides still match the engine's own record, so nobody edited anything."""
    report = heg.harvest(_bundle(tmp_path, entity_baseline="Extract", entity_working="Orders"))

    assert report["provenance"][heg.PROV_ENGINE] > 0
    assert report["provenance"][heg.PROV_TIER] == 0
    assert report["tier_edits"] == []


def test_edit_after_the_engine_ran_is_attributed_as_a_tier_edit(tmp_path):
    bundle = _bundle(tmp_path)
    _write(bundle / "pbip" / "WB" / "WB.Report" / VISUAL, _visual("Orders", position=42))

    report = heg.harvest(bundle)

    assert report["provenance"][heg.PROV_TIER] == 1
    assert report["tier_edits"][0]["path"] == VISUAL
    assert hgs.SHAPE_LAYOUT in report["tier_edits"][0]["shapes"]


def test_tampered_baseline_is_refused_rather_than_reported(tmp_path):
    """A rewritten `reports/` copy already caused one retracted upstream bug report."""
    bundle = _bundle(tmp_path)
    _write(bundle / "reports" / "WB.Report" / VISUAL, _visual("Something Else"))

    report = heg.harvest(bundle)

    assert report["status"] == heg.STATUS_UNTRUSTWORTHY
    assert report["baseline_tampered"]
    assert report["provenance"][heg.PROV_TAMPERED] > 0


def test_bundle_without_a_baseline_reports_unattributed_not_engine_internal(tmp_path):
    """`NO_BASELINE` is honest ignorance; it must not be laundered into either bucket."""
    bundle = _bundle(tmp_path, entity_working="Orders")
    (bundle / "input_manifest.json").unlink()

    report = heg.harvest(bundle)

    assert report["attribution"]["usable"] is False
    assert report["provenance"][heg.PROV_UNATTRIBUTED] == report["provenance"]["differing_files"]
    assert report["provenance"][heg.PROV_ENGINE] == 0
    assert report["status"] == heg.STATUS_INCOMPLETE
    assert report["attribution"]["notes"]


def test_declared_tier_edit_names_the_declaring_script(tmp_path):
    """A declaration is credited only when it proves THIS run, THIS baseline and THIS result."""
    bundle = _bundle(tmp_path)
    target = "pbip/WB/WB.Report/" + VISUAL
    _write(bundle / target, _visual("Orders", position=7))
    _declare(bundle, target)

    report = heg.harvest(bundle)

    assert report["tier_edits"][0]["declared_by"] == "scripts/fix_layout.py"


# ---------------------------------------------------------------------------------------------
# Shape classification.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("/position/x", hgs.SHAPE_LAYOUT),
        ("/position/tabOrder", hgs.SHAPE_LAYOUT),
        ("/filterConfig/filters[]/name", hgs.SHAPE_FILTER),
        ("/visual/objects/general[]/properties/filter/filter/From[]/Entity", hgs.SHAPE_FILTER),
        ("/visual/visualType", hgs.SHAPE_VISUAL_TYPE),
        ("/datasetReference/byPath/path", hgs.SHAPE_REBIND),
        ("/pageOrder[]", hgs.SHAPE_PAGE_ORDER),
        ("/activePageName", hgs.SHAPE_PAGE_ORDER),
        ("/resourcePackages[]/items[]", hgs.SHAPE_RESOURCES),
        ("/visual/query/queryState/Y/projections[]/queryRef", hgs.SHAPE_MODEL_NAMES),
        ("/visual/query/queryState/Y/projections[]/field/Aggregation", hgs.SHAPE_QUERY),
        ("/visual/objects/dataPoint[]/properties/fill/solid/color", hgs.SHAPE_FORMATTING),
        ("/somethingEntirelyNew", hgs.SHAPE_UNCLASSIFIED),
    ],
)
def test_pointer_shape_classification(pointer, expected):
    assert hgs.pointer_shape(pointer) == expected


def test_entity_rename_into_the_bound_model_is_binding_resolution(tmp_path):
    """The reference copy names entities that exist nowhere; resolving them is by design."""
    report = heg.harvest(_bundle(tmp_path, entity_baseline="Extract", entity_working="Orders"))
    shapes = {row["shape"] for row in report["shapes"]}
    assert hgs.SHAPE_BINDING in shapes
    assert hgs.SHAPE_MODEL_NAMES not in shapes


def test_rename_between_two_valid_tables_is_not_excused_as_binding_resolution(tmp_path):
    """Both names exist in the bound model, so nothing about binding explains the swap."""
    bundle = _bundle(tmp_path, entity_baseline="Orders", entity_working="Returns")
    _model(bundle / "pbip" / "WB" / "Sales.SemanticModel", "Orders")
    _write(
        bundle / "pbip" / "WB" / "Sales.SemanticModel" / "definition" / "tables" / "Returns.tmdl",
        "table 'Returns'\n\n\tcolumn A\n",
    )
    _record_baseline(bundle)

    report = heg.harvest(bundle)

    shapes = {row["shape"] for row in report["shapes"]}
    assert hgs.SHAPE_MODEL_NAMES in shapes
    assert hgs.SHAPE_BINDING not in shapes


def test_tmdl_measure_addition_is_classified_not_dismissed_as_binary(tmp_path):
    """The only model-layer signal on the real estate; `BINARY_CHANGED` would have hidden it."""
    bundle = _bundle(tmp_path)
    _write(
        bundle / "pbip" / "WB" / "Sales.SemanticModel" / "definition" / "tables" / "Orders.tmdl",
        "table 'Orders'\n\n\tcolumn A\n\n\tmeasure 'Revenue doubled' = SUM('Orders'[A]) * 2\n",
    )

    report = heg.harvest(bundle)

    shapes = {row["shape"] for row in report["shapes"]}
    assert "TMDL_MEASURE" in shapes
    assert hgs.SHAPE_BINARY not in shapes


def test_added_and_removed_files_carry_their_own_shapes(tmp_path):
    bundle = _bundle(tmp_path)
    _write(bundle / "pbip" / "WB" / "WB.Report" / "definition/pages/page-2/page.json", {"name": "page-2"})
    _write(bundle / "reports" / "WB.Report" / "definition/pages/page-9/visuals/v9/visual.json", _visual("Orders"))
    _record_baseline(bundle)

    report = heg.harvest(bundle)

    shapes = {row["shape"] for row in report["shapes"]}
    assert "PAGE_ADDED" in shapes
    assert "VISUAL_REMOVED" in shapes


def test_every_shape_row_carries_its_denominator(tmp_path):
    report = heg.harvest(_bundle(tmp_path, entity_working="Renamed"))
    total = report["provenance"]["differing_files"]
    assert total > 0
    for row in report["shapes"]:
        assert row["files"] <= total
        assert 0 < row["share_of_differing_files"] <= 1


def test_baseline_dataset_reference_resolution_is_measured(tmp_path):
    """`reports/` is a reference-only emission: 0 of 45 resolved on the real estate."""
    report = heg.harvest(_bundle(tmp_path))
    summary = report["layers"]["report"]
    assert summary["baseline_reference_checked"] == 1
    assert summary["baseline_reference_resolves"] == 0


# ---------------------------------------------------------------------------------------------
# The git blind spot, driven by a LOW ceiling rather than a long path.
# ---------------------------------------------------------------------------------------------


def test_git_readable_path_max_is_the_measured_constant():
    """A silent edit to this number is the most damaging change anyone could make to the module.

    Measured 2026-08-30 on `_runs/estate-2.339.0-20260829/`: of 44 report pairs, the 41 that
    `git diff --no-index --stat` could read had a worst full path of exactly 259 characters, and the
    3 it could not (261 / 285 / 287) each returned exit 1 with NO stat line. `worst_path > 259`
    therefore predicted the failures 3/3 with no false positives on that corpus.

    The behavioural tests around it drive a LOW ceiling over a short tree, because a stock Windows
    runner cannot create a 260-character path - so without this pin the shipped value is unexercised.
    Mutation-tested: raising it to 10,000,000 survived the entire suite until this test existed.
    """
    assert heg.GIT_READABLE_PATH_MAX == 259


def test_pair_over_the_git_ceiling_is_still_assessed_and_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(heg, "GIT_READABLE_PATH_MAX", 1)
    report = heg.harvest(_bundle(tmp_path, entity_working="Renamed"))

    assert report["git_blind_spot"]["count"] > 0
    assert _pair(report, "report", "WB")["status"] == heg.PAIR_DIFFERS


def test_pair_within_the_git_ceiling_is_not_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(heg, "GIT_READABLE_PATH_MAX", 10_000)
    report = heg.harvest(_bundle(tmp_path, entity_working="Renamed"))
    assert report["git_blind_spot"]["count"] == 0


# ---------------------------------------------------------------------------------------------
# CLI contract: exit codes, and the JSON-before-console ordering.
# ---------------------------------------------------------------------------------------------


def test_exit_code_is_incomplete_when_coverage_is_partial(tmp_path):
    bundle = _bundle(tmp_path)
    _report(bundle / "reports" / "Orphan.Report", entity="Orders", model_relative="../Orphan.SemanticModel")
    _record_baseline(bundle)
    assert heg.main([str(bundle)]) == heg.EXIT_INCOMPLETE


def test_exit_code_is_untrustworthy_when_the_baseline_drifted(tmp_path):
    bundle = _bundle(tmp_path)
    _write(bundle / "reports" / "WB.Report" / VISUAL, _visual("Tampered"))
    assert heg.main([str(bundle)]) == heg.EXIT_UNTRUSTWORTHY


def test_exit_code_is_usage_for_a_missing_bundle(tmp_path):
    assert heg.main([str(tmp_path / "nope")]) == heg.EXIT_USAGE


def test_exit_code_is_usage_for_a_nonsense_top(tmp_path):
    assert heg.main([str(_bundle(tmp_path)), "--top", "0"]) == heg.EXIT_USAGE


def test_warn_only_always_exits_zero(tmp_path):
    bundle = _bundle(tmp_path)
    _write(bundle / "reports" / "WB.Report" / VISUAL, _visual("Tampered"))
    assert heg.main([str(bundle), "--warn-only"]) == heg.EXIT_OK


def test_exit_code_is_ok_when_everything_is_paired_and_attributed(tmp_path):
    """A complete run must be reachable, or `incomplete` means nothing."""
    bundle = tmp_path / "bundle"
    _report(bundle / "reports" / "WB.Report", entity="Orders", model_relative="../Sales.SemanticModel")
    _report(bundle / "pbip" / "WB" / "WB.Report", entity="Orders", model_relative="../Sales.SemanticModel")
    _model(bundle / "pbip" / "WB" / "Sales.SemanticModel", "Orders")
    _model(bundle / "semantic_models" / "Sales.SemanticModel", "Orders")
    _record_baseline(bundle)

    assert heg.main([str(bundle), "--quiet"]) == heg.EXIT_OK


def test_json_is_written_before_the_console_render(tmp_path, monkeypatch):
    """`--json` is a contract; it must not depend on the terminal's codec."""
    bundle = _bundle(tmp_path, entity_working="Renamed")
    destination = tmp_path / "out" / "harvest.json"

    class Hostile(io.StringIO):
        def write(self, s):  # noqa: D102
            raise RuntimeError("console is hostile")

    monkeypatch.setattr(sys, "stdout", Hostile())
    with pytest.raises(RuntimeError):
        heg.main([str(bundle), "--json", str(destination)])

    assert destination.is_file()
    assert json.loads(destination.read_text(encoding="utf-8"))["version"] == heg.REPORT_VERSION


def test_markdown_states_plainly_when_there_are_no_tier_edits(tmp_path):
    """A bundle nobody edited cannot answer issue #274, and the report must say so."""
    report = heg.harvest(_bundle(tmp_path, entity_working="Renamed"))
    markdown = hgr.render_markdown(report)
    assert "**None.**" in markdown
    assert "engine_internal" in markdown


def test_markdown_lists_tier_edits_when_they_exist(tmp_path):
    bundle = _bundle(tmp_path)
    _write(bundle / "pbip" / "WB" / "WB.Report" / VISUAL, _visual("Orders", position=99))
    markdown = hgr.render_markdown(heg.harvest(bundle))
    assert "Tier edits (the engine-gap evidence)" in markdown
    assert "**undeclared**" in markdown


def test_cli_runs_end_to_end_and_writes_both_artifacts(tmp_path):
    """A fully paired, fully attributed bundle exits 0 - judged by exit code, not printed text."""
    bundle = _bundle(tmp_path, entity_working="Renamed")
    json_path = tmp_path / "harvest.json"
    md_path = tmp_path / "harvest.md"

    proc = _run(str(bundle), "--json", str(json_path), "--markdown", str(md_path))

    assert proc.returncode == heg.EXIT_OK, proc.stderr
    assert json_path.is_file() and md_path.is_file()
    assert "differing files" in proc.stdout


# ---------------------------------------------------------------------------------------------
# The delegation contract. These are the tests the first version could not have passed, and they
# are written as a PROPERTY rather than as seven examples: whatever the mutation, the harvest must
# never look cleaner than `check_migration_progress.tamper_check()` does on the same bundle. That
# gate found every one of the four provenance defects a blind review of PR #399 reported, so it is
# the oracle, not a second opinion.
# ---------------------------------------------------------------------------------------------


def _identical_bundle(tmp_path: Path) -> Path:
    """A bundle whose two trees are byte-identical, so the delta alone reports NOTHING."""
    bundle = tmp_path / "bundle"
    _report(bundle / "reports" / "WB.Report", entity="Orders", model_relative="../Sales.SemanticModel")
    _report(bundle / "pbip" / "WB" / "WB.Report", entity="Orders", model_relative="../Sales.SemanticModel")
    _model(bundle / "pbip" / "WB" / "Sales.SemanticModel", "Orders")
    _model(bundle / "semantic_models" / "Sales.SemanticModel", "Orders")
    _record_baseline(bundle)
    return bundle


def _differing_bundle(tmp_path: Path) -> Path:
    """The normal shape: the reference emission and the bound working copy legitimately differ."""
    return _bundle(tmp_path, entity_baseline="Extract", entity_working="Orders")


WORKING_VISUAL = "pbip/WB/WB.Report/" + VISUAL
BASELINE_VISUAL = "reports/WB.Report/" + VISUAL


def _mutate_baseline_rewritten(bundle: Path) -> None:
    """Replace the pristine baseline with a copy of the working tree - the delta then agrees."""
    shutil.rmtree(bundle / "reports" / "WB.Report")
    shutil.copytree(bundle / "pbip" / "WB" / "WB.Report", bundle / "reports" / "WB.Report")


def _mutate_baseline_file_added(bundle: Path) -> None:
    _write(bundle / "reports" / "WB.Report" / "definition/pages/p9/page.json", {"name": "p9"})


def _mutate_baseline_file_deleted(bundle: Path) -> None:
    (bundle / BASELINE_VISUAL).unlink()


def _mutate_working_file_created(bundle: Path) -> None:
    _write(bundle / "pbip" / "WB" / "WB.Report" / "definition/pages/p2/page.json", {"name": "p2"})


def _mutate_working_file_deleted(bundle: Path) -> None:
    (bundle / WORKING_VISUAL).unlink()


def _mutate_working_file_changed(bundle: Path) -> None:
    _write(bundle / WORKING_VISUAL, _visual("Orders", position=42))


def _mutate_stale_declaration(bundle: Path) -> None:
    """Declare an edit, then edit again: the declaration no longer describes the current file."""
    _write(bundle / WORKING_VISUAL, _visual("Orders", position=7))
    _declare(bundle, WORKING_VISUAL, identity="scripts/unrelated_old.py")
    _write(bundle / WORKING_VISUAL, _visual("Orders", position=99))


def _mutate_pbip_project_file(bundle: Path) -> None:
    _write(bundle / "pbip" / "WB" / "WB.pbip", {"version": "2.0", "artifacts": []})


def _mutate_whole_working_report_deleted(bundle: Path) -> None:
    shutil.rmtree(bundle / "pbip" / "WB" / "WB.Report")


def _mutate_unpaired_working_report_added(bundle: Path) -> None:
    _write(bundle / "pbip" / "Extra" / "Extra.Report" / "definition/pages/p1/page.json", {"name": "p1"})


def _bundle_with_project_file(tmp_path: Path) -> Path:
    """A bundle carrying a `pbip/<unit>/<unit>.pbip` - the estate has 51 of them, none paired."""
    bundle = _identical_bundle(tmp_path)
    _write(bundle / "pbip" / "WB" / "WB.pbip", {"version": "1.0", "artifacts": []})
    _record_baseline(bundle)
    return bundle


MUTATIONS = {
    "baseline_rewritten_from_working": (_differing_bundle, _mutate_baseline_rewritten),
    "baseline_file_added": (_identical_bundle, _mutate_baseline_file_added),
    "baseline_file_deleted": (_identical_bundle, _mutate_baseline_file_deleted),
    "working_file_created": (_identical_bundle, _mutate_working_file_created),
    "working_file_deleted": (_identical_bundle, _mutate_working_file_deleted),
    "working_file_changed": (_identical_bundle, _mutate_working_file_changed),
    "stale_declaration": (_identical_bundle, _mutate_stale_declaration),
    "pbip_project_file_changed": (_bundle_with_project_file, _mutate_pbip_project_file),
    "whole_working_report_deleted": (_identical_bundle, _mutate_whole_working_report_deleted),
    "unpaired_working_report_added": (_identical_bundle, _mutate_unpaired_working_report_added),
}


def _moved_paths(report: dict) -> set[str]:
    """Every bundle-relative path the harvest says moved after the engine ran."""
    moved = {entry["target"] for entry in report["baseline_drift"]}
    for record in report["tier_edits"] + report["baseline_tampered"]:
        moved |= {p for p in (record["working_path"], record["baseline_path"]) if p}
    return moved


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_drift_the_tamper_gate_finds_is_accounted_for(tmp_path, name):
    """The property behind the whole redesign, over every mutation shape the review found.

    Not "the harvest must also look unhappy" - a fully attributed tier edit is exactly the evidence
    this module exists to produce, and `complete` is the right word for it. The contract is stricter
    and more useful: **every path the gate says moved must appear in the harvest's own output.**
    Measured on the first version (2026-08-30), five of these seven shapes returned `complete` while
    `tamper_check()` returned `DRIFT` on the identical bundle, and four of them named nothing at all.
    """
    build, mutate = MUTATIONS[name]
    bundle = build(tmp_path)
    mutate(bundle)

    state, _notes = cmp_mod.tamper_check(bundle)
    assert state in {"DRIFT", "DECLARED_DRIFT"}, f"fixture no longer produces drift for {name}"
    generated = cmp_mod.load_generated_artifact_baseline(bundle)
    drifted = {item["target"] for item in cmp_mod.adjudicate_generated_drift(bundle, generated)}
    assert drifted, f"fixture no longer produces drift for {name}"

    report = heg.harvest(bundle)

    assert drifted <= _moved_paths(report), f"{name}: {sorted(drifted - _moved_paths(report))} went unreported"


# ---------------------------------------------------------------------------------------------
# Finding 1: a baseline rewrite must not be able to erase its own evidence.
# ---------------------------------------------------------------------------------------------


def test_baseline_rewritten_from_the_working_copy_is_untrustworthy_though_the_delta_is_empty(tmp_path):
    """The delta agrees perfectly afterwards - that is exactly what makes this the dangerous shape."""
    bundle = _differing_bundle(tmp_path)
    _mutate_baseline_rewritten(bundle)

    report = heg.harvest(bundle)

    assert report["provenance"]["differing_files"] == 0
    assert report["baseline_drift"], "baseline integrity must be validated independently of the delta"
    assert report["status"] == heg.STATUS_UNTRUSTWORTHY
    assert heg.main([str(bundle), "--quiet"]) == heg.EXIT_UNTRUSTWORTHY


@pytest.mark.parametrize(
    ("mutate", "expected_kind"),
    [(_mutate_baseline_file_added, "added"), (_mutate_baseline_file_deleted, "missing")],
)
def test_baseline_tree_additions_and_deletions_are_refused(tmp_path, mutate, expected_kind):
    """`reports/` is validated as a complete file SET, not only where it still pairs up."""
    bundle = _identical_bundle(tmp_path)
    mutate(bundle)

    report = heg.harvest(bundle)

    assert [e["kind"] for e in report["baseline_drift"]] == [expected_kind]
    assert report["status"] == heg.STATUS_UNTRUSTWORTHY


def test_a_declared_edit_to_the_pristine_baseline_is_still_refused(tmp_path):
    """A declaration makes an edit visible, not legitimate: `reports/` is never edited by anyone."""
    bundle = _identical_bundle(tmp_path)
    _write(bundle / BASELINE_VISUAL, _visual("Orders", position=3))
    _declare(bundle, BASELINE_VISUAL)

    report = heg.harvest(bundle)

    assert cmp_mod.tamper_check(bundle)[0] == "DECLARED_DRIFT"
    assert report["baseline_drift"][0]["declared_by"] == "scripts/fix_layout.py"
    assert report["status"] == heg.STATUS_UNTRUSTWORTHY


# ---------------------------------------------------------------------------------------------
# Finding 2: post-engine additions and deletions ARE tier edits.
# ---------------------------------------------------------------------------------------------


def test_file_created_after_the_engine_is_a_tier_edit_not_unattributed(tmp_path):
    bundle = _identical_bundle(tmp_path)
    _mutate_working_file_created(bundle)

    report = heg.harvest(bundle)

    assert report["provenance"][heg.PROV_TIER] == 1
    assert report["provenance"][heg.PROV_UNATTRIBUTED] == 0
    assert report["tier_edits"][0]["post_engine"] == "created"


def test_file_deleted_after_the_engine_is_a_tier_edit_not_engine_internal(tmp_path):
    """The record used to inspect only the BASELINE path, so a deletion looked like engine output."""
    bundle = _identical_bundle(tmp_path)
    _mutate_working_file_deleted(bundle)

    report = heg.harvest(bundle)

    assert report["provenance"][heg.PROV_TIER] == 1
    assert report["provenance"][heg.PROV_ENGINE] == 0
    assert report["tier_edits"][0]["post_engine"] == "deleted"


def test_deleted_engine_emitted_working_only_file_does_not_vanish(tmp_path):
    """Absent from BOTH trees, so only the engine-time inventory still remembers it existed."""
    bundle = _identical_bundle(tmp_path)
    working_only = "pbip/WB/WB.Report/definition/pages/p9/page.json"
    _write(bundle / working_only, {"name": "p9"})
    _record_baseline(bundle)
    (bundle / working_only).unlink()

    report = heg.harvest(bundle)

    assert [r["path"] for r in report["tier_edits"]] == ["definition/pages/p9/page.json"]
    assert report["tier_edits"][0]["kind"] == "removed_after_engine"
    assert _pair(report, "report", "WB")["files"]["post_engine_only"] == 1
    assert _pair(report, "report", "WB")["status"] == heg.PAIR_DIFFERS


def test_working_copy_reverted_onto_the_baseline_is_still_a_tier_edit(tmp_path):
    """Content-identical to the reference again, so the delta sees nothing. The inventory does."""
    bundle = _bundle(tmp_path, entity_baseline="Extract", entity_working="Orders")
    _write(bundle / WORKING_VISUAL, _visual("Extract"))

    report = heg.harvest(bundle)

    reverted = [r for r in report["tier_edits"] if r["path"] == VISUAL]
    assert reverted and reverted[0]["kind"] == "changed_after_engine"
    assert hgs.SHAPE_REVERTED in reverted[0]["shapes"]


def test_a_post_engine_change_is_never_counted_twice(tmp_path):
    """The record set is a UNION; a file in both the delta and the inventory gets ONE record."""
    bundle = _identical_bundle(tmp_path)
    _mutate_working_file_changed(bundle)

    report = heg.harvest(bundle)

    assert report["provenance"]["differing_files"] == 1
    assert [r["path"] for r in report["tier_edits"]] == [VISUAL]


# ---------------------------------------------------------------------------------------------
# Finding 3: `complete` may not contain unattributed or backfilled evidence.
# ---------------------------------------------------------------------------------------------


def test_empty_recorded_inventory_is_unavailable_not_complete(tmp_path):
    """`files: {}` covers nothing, so it can neither attribute nor be read as 'all added later'."""
    bundle = _bundle(tmp_path, entity_baseline="Extract", entity_working="Orders")
    manifest = json.loads((bundle / "input_manifest.json").read_text(encoding="utf-8"))
    manifest["generated_artifacts"]["files"] = {}
    (bundle / "input_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    report = heg.harvest(bundle)

    assert report["attribution"]["usable"] is False
    assert report["attribution"]["unavailable_reason"] == "empty_inventory"
    assert report["status"] == heg.STATUS_INCOMPLETE
    assert heg.main([str(bundle), "--quiet"]) == heg.EXIT_INCOMPLETE


def test_slice_only_backfill_is_unavailable_for_engine_attribution(tmp_path):
    """It proves nothing changed since the BACKFILL - and this module asks about the ENGINE."""
    bundle = _bundle(tmp_path, entity_baseline="Extract", entity_working="Orders")
    manifest = json.loads((bundle / "input_manifest.json").read_text(encoding="utf-8"))
    manifest["generated_artifacts"]["coverage"] = "slice_only_backfill"
    (bundle / "input_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    report = heg.harvest(bundle)

    assert report["attribution"]["unavailable_reason"] == "slice_only_backfill"
    assert report["provenance"][heg.PROV_ENGINE] == 0
    assert report["provenance"][heg.PROV_UNATTRIBUTED] == report["provenance"]["differing_files"]
    assert report["status"] == heg.STATUS_INCOMPLETE


def test_any_unattributed_difference_forces_incomplete(tmp_path):
    """Existence of an attribution object is not coverage of the paths actually compared.

    A `.pbi` sidecar is the honest example: the tamper gate deliberately excludes it (a refresh
    rewrites it and must not read as tampering), so the engine inventory can say nothing about it -
    yet the tree comparison still sees it. That difference is real and its authorship is unknown,
    which is `unattributed`, which may not be reported as a `complete` harvest.
    """
    bundle = _identical_bundle(tmp_path)
    _write(bundle / "pbip" / "WB" / "WB.Report" / ".pbi" / "localSettings.json", {"version": "1.0"})

    report = heg.harvest(bundle)
    coverage = report["attribution"]["coverage"]

    assert report["provenance"][heg.PROV_UNATTRIBUTED] == 1
    assert coverage["paths_unattributed"] == 1
    assert coverage["complete"] is False
    assert report["status"] == heg.STATUS_INCOMPLETE
    assert heg.main([str(bundle), "--quiet"]) == heg.EXIT_INCOMPLETE


def test_attribution_coverage_is_reported_with_its_denominator(tmp_path):
    """A fully covered bundle says so in numbers, so the claim is inspectable rather than implied."""
    report = heg.harvest(_bundle(tmp_path, entity_baseline="Extract", entity_working="Orders"))
    coverage = report["attribution"]["coverage"]

    assert coverage["paths_compared"] == report["provenance"]["differing_files"]
    assert coverage["paths_attributed"] == coverage["paths_compared"]
    assert coverage["complete"] is True


@pytest.mark.parametrize(
    ("recorded", "drift", "baseline_rel", "working_rel", "expected"),
    [
        (["reports/a", "pbip/a"], {}, "reports/a", "pbip/a", heg.PROV_ENGINE),
        (["reports/a"], {}, "reports/a", None, heg.PROV_ENGINE),
        (["reports/a", "pbip/a"], {"pbip/a": "changed"}, "reports/a", "pbip/a", heg.PROV_TIER),
        (["reports/a", "pbip/a"], {"pbip/a": "missing"}, "reports/a", "pbip/a", heg.PROV_TIER),
        (["reports/a", "pbip/a"], {"reports/a": "changed"}, "reports/a", "pbip/a", heg.PROV_TAMPERED),
        # Baseline drift outranks working drift: the reference the comparison rests on is gone, so
        # nothing can be said about what the tier did to the other side.
        (
            ["reports/a", "pbip/a"],
            {"reports/a": "changed", "pbip/a": "changed"},
            "reports/a",
            "pbip/a",
            heg.PROV_TAMPERED,
        ),
        # ⚠️ ONE side matching is not attribution. Only when EVERY present side is accounted for can
        # a difference be called the engine's own. Unobserved on the estate corpus - `artifact_drift`
        # reports an unrecorded but existing generated artifact as `added` drift, so this mixed state
        # needs a path the inventory excludes by design (a `.pbi` sidecar) on exactly one side - but
        # it is the method's contract, and relaxing it to `"match" in states` survived every
        # end-to-end test in this file.
        (["reports/a"], {}, "reports/a", "pbip/a", heg.PROV_UNATTRIBUTED),
        ([], {}, "reports/a", "pbip/a", heg.PROV_UNATTRIBUTED),
    ],
)
def test_evidence_verdict_requires_every_present_side_to_be_accounted_for(
    recorded, drift, baseline_rel, working_rel, expected
):
    evidence = heg.Evidence(
        Path("bundle"),
        {path: "hash" for path in recorded},
        {path: {"kind": kind, "declared_by": None} for path, kind in drift.items()},
        [],
    )
    assert evidence.verdict(baseline_rel, working_rel) == expected


def test_an_unusable_evidence_object_attributes_nothing():
    evidence = heg.Evidence(Path("bundle"), {"reports/a": "hash"}, {}, [], unavailable_reason="empty_inventory")
    assert evidence.usable is False
    assert evidence.verdict("reports/a", "pbip/a") == heg.PROV_UNATTRIBUTED


# ---------------------------------------------------------------------------------------------
# Finding 4: an inaccessible directory must not fabricate additions beneath it.
# ---------------------------------------------------------------------------------------------


def _block_directory(monkeypatch, blocked_name: str, side: str, *, locatable: bool = True) -> None:
    """Make `os.walk` fail on one directory of one side, exactly as a PermissionError would."""
    real_walk = heg.os.walk

    def walk(top, onerror=None, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, onerror=onerror, **kwargs):
            if Path(dirpath).name == blocked_name and side in Path(dirpath).parts:
                if onerror is not None:
                    exc = PermissionError(13, "injected: unreadable directory")
                    exc.filename = dirpath if locatable else "<unlocatable>"
                    onerror(exc)
                continue
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(heg.os, "walk", walk)


def _bundle_with_blocked_dir(tmp_path: Path) -> Path:
    bundle = _identical_bundle(tmp_path)
    for side in ("reports/WB.Report", "pbip/WB/WB.Report"):
        _write(bundle / side / "definition/pages/blocked/visual.json", _visual("Orders"))
    _record_baseline(bundle)
    return bundle


def test_unreadable_directory_does_not_fabricate_additions_beneath_it(tmp_path, monkeypatch):
    """`os.walk` reports only the directory, so exact-path withdrawal left every descendant behind."""
    bundle = _bundle_with_blocked_dir(tmp_path)
    _block_directory(monkeypatch, "blocked", "reports")

    report = heg.harvest(bundle)
    entry = _pair(report, "report", "WB")

    assert entry["files"]["added"] == 0, "a file under an unreadable directory was counted as added"
    assert entry["files"]["removed"] == 0
    assert entry["status"] == heg.PAIR_UNASSESSABLE
    assert report["status"] == heg.STATUS_INCOMPLETE


def test_a_traversal_failure_that_cannot_be_located_suppresses_the_whole_pair(tmp_path, monkeypatch):
    """Nothing about the pair can be scoped, so a partial answer would look complete and be wrong."""
    bundle = _bundle_with_blocked_dir(tmp_path)
    _write(bundle / "pbip/WB/WB.Report/definition/pages/p2/page.json", {"name": "p2"})
    _block_directory(monkeypatch, "blocked", "reports", locatable=False)

    report = heg.harvest(bundle)
    entry = _pair(report, "report", "WB")

    assert entry["provenance"] == {}
    assert entry["status"] == heg.PAIR_UNASSESSABLE
    assert report["status"] == heg.STATUS_INCOMPLETE


# ---------------------------------------------------------------------------------------------
# Finding 5: `BINDING_RESOLUTION` is decided per changed leaf, never per file.
# ---------------------------------------------------------------------------------------------


def _projection(entity: str, prop: str) -> dict:
    return {
        "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}",
        "nativeQueryRef": prop,
    }


def _multi_visual(*pairs: tuple[str, str]) -> dict:
    return {
        "position": {"x": 0, "y": 0, "width": 100, "height": 100, "tabOrder": 0},
        "visual": {
            "visualType": "columnChart",
            "query": {
                "queryState": {
                    f"role{index}": {"projections": [_projection(entity, prop)]}
                    for index, (entity, prop) in enumerate(pairs)
                }
            },
        },
    }


def _mixed_name_change_bundle(tmp_path: Path, before: tuple, after: tuple) -> Path:
    bundle = tmp_path / "bundle"
    _write(
        bundle / "reports" / "WB.Report" / "definition.pbir",
        {"version": "4.0", "datasetReference": {"byPath": {"path": "../Sales.SemanticModel"}}},
    )
    _write(bundle / "reports" / "WB.Report" / VISUAL, _multi_visual(*before))
    _write(
        bundle / "pbip" / "WB" / "WB.Report" / "definition.pbir",
        {"version": "4.0", "datasetReference": {"byPath": {"path": "../Sales.SemanticModel"}}},
    )
    _write(bundle / "pbip" / "WB" / "WB.Report" / VISUAL, _multi_visual(*after))
    for root in (bundle / "pbip" / "WB" / "Sales.SemanticModel", bundle / "semantic_models" / "Sales.SemanticModel"):
        _model(root, "Orders")
        _write(root / "definition" / "tables" / "Returns.tmdl", "table 'Returns'\n\n\tcolumn A\n")
    _record_baseline(bundle)
    return bundle


def test_one_valid_rebind_does_not_excuse_an_unrelated_table_substitution(tmp_path):
    """Measured on the first version: all three of these collapsed into BINDING_RESOLUTION alone."""
    bundle = _mixed_name_change_bundle(
        tmp_path,
        before=(("Extract", "A"), ("Orders", "A"), ("Orders", "A")),
        after=(("Orders", "A"), ("Returns", "A"), ("Orders", "B")),
    )

    shapes = {row["shape"] for row in heg.harvest(bundle)["shapes"]}

    assert hgs.SHAPE_BINDING in shapes, "the genuine invalid->valid rebind must still be recognised"
    assert hgs.SHAPE_MODEL_NAMES in shapes, "a valid->valid substitution must not be excused"


def test_a_property_rename_is_never_excused_as_binding_resolution(tmp_path):
    """Only TABLE names are read from the bound model, so a column rename cannot be demonstrated."""
    bundle = _mixed_name_change_bundle(
        tmp_path,
        before=(("Extract", "A"),),
        after=(("Orders", "B"),),
    )

    shapes = {row["shape"] for row in heg.harvest(bundle)["shapes"]}

    assert hgs.SHAPE_MODEL_NAMES in shapes


@pytest.mark.parametrize(
    ("leaf", "before", "after", "expected"),
    [
        ("/visual/query/x/Entity", "Extract", "Orders", hgs.SHAPE_BINDING),
        ("/visual/query/x/Entity", "Orders", "Returns", hgs.SHAPE_MODEL_NAMES),
        ("/visual/query/x/Entity", "Orders", "Nowhere", hgs.SHAPE_MODEL_NAMES),
        ("/visual/query/x/queryRef", "Extract.A", "Orders.A", hgs.SHAPE_BINDING),
        ("/visual/query/x/queryRef", "Extract.A", "Orders.B", hgs.SHAPE_MODEL_NAMES),
        ("/visual/query/x/Property", "A", "B", hgs.SHAPE_MODEL_NAMES),
        ("/visual/query/x/nativeQueryRef", "A", "B", hgs.SHAPE_MODEL_NAMES),
        # ⚠️ A COLUMN whose name collides with a TABLE name. `bound_model_tables` returns table names
        # only, so testing a property against it is a category error: `Property: Extract -> Orders`
        # would look exactly like an invalid->valid rebind while being a column rename. Without these
        # two rows the guard is unobserved - mutation-tested: widening the `Entity` branch to cover
        # `Property`/`nativeQueryRef` survived the whole suite until they existed.
        ("/visual/query/x/Property", "Extract", "Orders", hgs.SHAPE_MODEL_NAMES),
        ("/visual/query/x/nativeQueryRef", "Extract", "Orders", hgs.SHAPE_MODEL_NAMES),
    ],
)
def test_name_change_shape_is_decided_leaf_by_leaf(leaf, before, after, expected):
    difference = hgs.Difference(leaf, "value", before, after)
    assert hgs.name_change_shape(difference, {"Orders", "Returns"}) == expected


def test_an_unknown_bound_model_never_excuses_a_name_change():
    difference = hgs.Difference("/visual/query/x/Entity", "value", "Extract", "Orders")
    assert hgs.name_change_shape(difference, None) == hgs.SHAPE_MODEL_NAMES


# ---------------------------------------------------------------------------------------------
# Finding 6: unreadable metadata is `incomplete` (exit 3), never the tamper exit code.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b'{"generated_artifacts": "\xff\xfe not utf-8"}', id="invalid-utf8"),
        pytest.param(b"{not json at all", id="invalid-json"),
    ],
)
def test_undecodable_manifest_is_incomplete_not_untrustworthy(tmp_path, payload):
    """Two very different situations must not share the exit code that means 'tampered baseline'."""
    bundle = _bundle(tmp_path, entity_baseline="Extract", entity_working="Orders")
    (bundle / "input_manifest.json").write_bytes(payload)

    report = heg.harvest(bundle)

    assert report["attribution"]["usable"] is False
    assert report["status"] == heg.STATUS_INCOMPLETE
    assert heg.main([str(bundle), "--quiet"]) == heg.EXIT_INCOMPLETE


def test_undecodable_manifest_still_writes_the_structured_report(tmp_path):
    """`--json` is a contract; a crash out of main() destroyed it and named no reason."""
    bundle = _bundle(tmp_path, entity_baseline="Extract", entity_working="Orders")
    (bundle / "input_manifest.json").write_bytes(b'{"generated_artifacts": "\xff\xfe"}')
    destination = tmp_path / "out" / "harvest.json"

    assert heg.main([str(bundle), "--quiet", "--json", str(destination)]) == heg.EXIT_INCOMPLETE

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["attribution"]["unavailable_reason"].startswith("UnicodeDecodeError")


def test_an_unreadable_declaration_ledger_does_not_crash_the_harvest(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path)
    _write(bundle / WORKING_VISUAL, _visual("Orders", position=42))

    def explode(_bundle_path):
        raise OSError("injected: declaration ledger is unreadable")

    monkeypatch.setattr(cmp_mod, "load_generated_edit_declarations", explode)
    report = heg.harvest(bundle)

    assert report["attribution"]["usable"] is False
    assert report["status"] == heg.STATUS_INCOMPLETE


# ---------------------------------------------------------------------------------------------
# Finding 7: a declaration is credited only when it proves the CURRENT edit.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"run_id": "some-other-run"}, "a declaration from another engine run"),
        ({"kind": "added"}, "a declaration describing a different operation"),
        ({"baseline_sha256": "0" * 64}, "a declaration whose baseline is not the engine's"),
        ({"expected_sha256": "0" * 64}, "a declaration whose result is not the current file"),
        ({"identity": ""}, "a declaration naming no script"),
    ],
)
def test_a_declaration_that_does_not_prove_the_current_edit_is_not_credited(tmp_path, override, reason):
    bundle = _bundle(tmp_path)
    _write(bundle / WORKING_VISUAL, _visual("Orders", position=7))
    _declare(bundle, WORKING_VISUAL, **override)

    report = heg.harvest(bundle)

    assert report["tier_edits"][0]["declared_by"] is None, reason


def test_a_declaration_invalidated_by_a_later_edit_is_not_credited(tmp_path):
    """The file moved again after being declared, so the declaration describes a state that is gone."""
    bundle = _bundle(tmp_path)
    _mutate_stale_declaration(bundle)

    report = heg.harvest(bundle)

    assert cmp_mod.tamper_check(bundle)[0] == "DRIFT"
    assert report["tier_edits"][0]["declared_by"] is None


# ---------------------------------------------------------------------------------------------
# Round-3 review. Three of these four defects were reachable while all 98 tests stayed green, so
# each one below is written to fail against the code that shipped, not merely to pass against the
# code that fixed it. The mutation table in the PR description names which test catches which.
# ---------------------------------------------------------------------------------------------


def _dotted_model(root: Path, *tables: str) -> None:
    """A `.SemanticModel` whose table names may themselves contain dots (`HumanResources.csv`)."""
    for table in tables:
        _write(root / "definition" / "tables" / f"{table}.tmdl", f"table '{table}'\n\n\tcolumn A\n")
    _write(root / "definition.pbism", {"version": "4.0"})


DOTTED_TABLES = {"Date", "HumanResources.csv", "Orders", "Orders.csv", "Customers.csv", "Returns", "_Measures"}


@pytest.mark.parametrize(
    ("before", "after", "expected", "why"),
    [
        # ⚠️ The bound model really does hold a table called `HumanResources.csv`. Splitting a
        # queryRef at the FIRST dot read this as entity `HumanResources` + property `csv.Status`,
        # decided the property had changed, and retained a textbook rebind as unexplained.
        ("HumanResources.Status", "HumanResources.csv.Status", hgs.SHAPE_BINDING, "dotted table name"),
        ("Extract.State", "HumanResources.csv.State", hgs.SHAPE_BINDING, "invalid entity -> dotted table"),
        ("Customers.csv_110301F4.Country", "Customers.csv.Country", hgs.SHAPE_BINDING, "hashed extract name"),
        ("Sum(Orders.csv_AB12.Sales)", "Sum(Orders.csv.Sales)", hgs.SHAPE_BINDING, "aggregation wrapper"),
        # Still retained, and these are the true positives the whole refinement exists to keep.
        ("Orders.Order_Date", "Date.Year", hgs.SHAPE_MODEL_NAMES, "different table AND column"),
        ("Orders.Amount", "Returns.Amount", hgs.SHAPE_MODEL_NAMES, "valid -> valid substitution"),
        ("Sum(Orders.csv_AB.Sales)", "Avg(Orders.csv.Sales)", hgs.SHAPE_MODEL_NAMES, "aggregation changed"),
        ("Extract.A", "Nowhere.A", hgs.SHAPE_MODEL_NAMES, "after-entity is not a table"),
        ("Orders.csv.Sales", "Orders.csv.Margin", hgs.SHAPE_MODEL_NAMES, "property changed"),
        ("Orders.csv.Sales", "Orders.csv.Sales", hgs.SHAPE_MODEL_NAMES, "no entity change at all"),
        # `Orders` and `Orders.csv` are BOTH tables: the longest prefix is the right split, and the
        # shortest would resolve `Orders.csv.Sales` to entity `Orders` + property `csv.Sales`.
        ("Extract.Sales", "Orders.csv.Sales", hgs.SHAPE_BINDING, "longest matching table wins"),
        # ⚠️ Power BI appends a disambiguation suffix to a duplicated query reference. Requiring the
        # closing paren to END the string missed exactly three estate leaves - the whole 474-vs-477
        # gap - because `) 2` never unwrapped and the rebind inside it stayed invisible.
        (
            "Sum(Orders.csv_5AF5F66F.Sales) 2",
            "Sum(Orders.csv.Sales) 2",
            hgs.SHAPE_BINDING,
            "aggregation with a disambiguation suffix",
        ),
        # ...but the suffix is COMPARED, not stripped: changing it changes which duplicate is meant.
        ("Sum(Orders.csv_AB.Sales) 2", "Sum(Orders.csv.Sales) 3", hgs.SHAPE_MODEL_NAMES, "suffix changed"),
        ("Sum(Orders.csv_AB.Sales)", "Sum(Orders.csv.Sales) 2", hgs.SHAPE_MODEL_NAMES, "suffix gained"),
    ],
)
def test_query_ref_resolves_against_real_table_names_not_the_first_dot(before, after, expected, why):
    difference = hgs.Difference("/visual/query/x/projections[]/queryRef", "value", before, after)
    assert hgs.name_change_shape(difference, DOTTED_TABLES) == expected, why


def test_a_rebind_onto_a_dotted_table_is_not_reported_as_unexplained(tmp_path):
    """End-to-end shape of the estate's dominant false positive: 574 -> 100 retained queryRef leaves."""
    bundle = tmp_path / "bundle"
    _write(
        bundle / "reports" / "WB.Report" / "definition.pbir",
        {"version": "4.0", "datasetReference": {"byPath": {"path": "../HR.SemanticModel"}}},
    )
    _write(bundle / "reports" / "WB.Report" / VISUAL, _visual("Extract"))
    _write(
        bundle / "pbip" / "WB" / "WB.Report" / "definition.pbir",
        {"version": "4.0", "datasetReference": {"byPath": {"path": "../HR.SemanticModel"}}},
    )
    _write(bundle / "pbip" / "WB" / "WB.Report" / VISUAL, _visual("HumanResources.csv"))
    _dotted_model(bundle / "pbip" / "WB" / "HR.SemanticModel", "HumanResources.csv")
    _dotted_model(bundle / "semantic_models" / "HR.SemanticModel", "HumanResources.csv")
    _record_baseline(bundle)

    shapes = {row["shape"] for row in heg.harvest(bundle)["shapes"]}

    assert hgs.SHAPE_BINDING in shapes
    assert hgs.SHAPE_MODEL_NAMES not in shapes, "the whole rebind is explained; nothing should be retained"


# ---------------------------------------------------------------------------------------------
# Finding 3: no adjudicated path may disappear just because it sits outside a discovered pair.
# ---------------------------------------------------------------------------------------------


def test_a_changed_pbip_project_file_is_reported_as_a_tier_edit(tmp_path):
    """⚠️ The gate adjudicates the WHOLE inventory; the pair loop only sees `.Report`/`.SemanticModel`.

    Measured (blind review round 3): editing an engine-recorded `pbip/WB/WB.pbip` was reported
    `changed` by `adjudicate_generated_drift` while the harvest returned `complete` with **zero**
    differences and zero tier edits. The real corpus holds 51 such files.
    """
    bundle = _identical_bundle(tmp_path)
    _write(bundle / "pbip" / "WB" / "WB.pbip", {"version": "1.0", "artifacts": []})
    _record_baseline(bundle)
    _mutate_pbip_project_file(bundle)

    report = heg.harvest(bundle)

    assert report["provenance"][heg.PROV_TIER] == 1
    assert report["unpaired_drift_records"] == 1
    edit = report["tier_edits"][0]
    assert edit["working_path"] == "pbip/WB/WB.pbip"
    assert edit["layer"] == heg.LAYER_BUNDLE
    assert edit["unit"] == "WB"
    assert edit["unpaired"] is True


def test_deleting_a_whole_working_report_reports_every_lost_file(tmp_path):
    bundle = _identical_bundle(tmp_path)
    _mutate_whole_working_report_deleted(bundle)

    report = heg.harvest(bundle)

    assert report["provenance"][heg.PROV_TIER] == 2
    assert {r["working_path"] for r in report["tier_edits"]} == {
        "pbip/WB/WB.Report/definition.pbir",
        "pbip/WB/WB.Report/" + VISUAL,
    }
    assert all(r["layer"] == "report" and r["artifact"] == "WB" for r in report["tier_edits"])


def test_an_adjudicated_path_that_cannot_be_placed_is_listed_and_forces_incomplete(tmp_path):
    """A `.pbip` at the BUNDLE root is under neither a baseline root nor `pbip/`.

    It must not be silently absorbed: it is named in `unreconciled_drift`, and while anything is
    there the run cannot be `complete`. That guard is what makes "nothing disappears" structural
    rather than a property of the cases we happened to enumerate.
    """
    bundle = _identical_bundle(tmp_path)
    _write(bundle / "Estate.pbip", {"version": "1.0"})
    _record_baseline(bundle)
    _write(bundle / "Estate.pbip", {"version": "2.0"})

    report = heg.harvest(bundle)

    assert [e["target"] for e in report["unreconciled_drift"]] == ["Estate.pbip"]
    assert report["status"] == heg.STATUS_INCOMPLETE
    assert heg.main([str(bundle), "--quiet"]) == heg.EXIT_INCOMPLETE


def test_no_adjudicated_path_is_ever_left_unreported(tmp_path):
    """The invariant, asserted directly rather than inferred from the cases above."""
    bundle = _identical_bundle(tmp_path)
    _write(bundle / "pbip" / "WB" / "WB.pbip", {"version": "1.0"})
    _write(bundle / "Estate.pbip", {"version": "1.0"})
    _record_baseline(bundle)
    _mutate_pbip_project_file(bundle)
    _mutate_working_file_changed(bundle)
    _write(bundle / "Estate.pbip", {"version": "9.9"})

    generated = cmp_mod.load_generated_artifact_baseline(bundle)
    adjudicated = {item["target"] for item in cmp_mod.adjudicate_generated_drift(bundle, generated)}
    report = heg.harvest(bundle)
    reported = _moved_paths(report) | {e["target"] for e in report["unreconciled_drift"]}

    assert len(adjudicated) == 3
    assert adjudicated <= reported, f"unreported: {sorted(adjudicated - reported)}"


# ---------------------------------------------------------------------------------------------
# Finding 4: access failures outside descendant hashing.
# ---------------------------------------------------------------------------------------------


def test_a_directory_that_cannot_be_enumerated_is_a_finding_not_a_traceback(tmp_path, monkeypatch):
    """Discovery used a bare `iterdir()`; a `PermissionError` on `pbip/` escaped `harvest()` entirely."""
    bundle = _identical_bundle(tmp_path)
    real_iterdir = Path.iterdir

    def blocked(self):
        if self.name == "pbip":
            raise PermissionError(13, "injected: cannot enumerate")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", blocked)
    report = heg.harvest(bundle)

    assert report["status"] == heg.STATUS_INCOMPLETE
    assert any(record.get("scope") == "discovery" for record in report["unassessable"])


def test_a_blocked_tree_root_withdraws_the_whole_tree_from_every_count(tmp_path, monkeypatch):
    """⚠️ A failure at the tree ROOT relativises to `"."`, which no key is prefixed by.

    Measured (blind review round 3): the round-2 prefix fix still let this through - `incomplete`,
    but both working files were counted as additions AND attributed `engine_internal`, contradicting
    this module's documented promise that unreadable content is excluded from every count.
    """
    bundle = _identical_bundle(tmp_path)
    real_walk = heg.os.walk

    def walk(top, onerror=None, **kwargs):
        if Path(top).name == "WB.Report" and "reports" in Path(top).parts:
            if onerror is not None:
                failure = PermissionError(13, "injected: cannot enter tree root")
                failure.filename = str(top)
                onerror(failure)
            return
        yield from real_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr(heg.os, "walk", walk)
    report = heg.harvest(bundle)
    entry = _pair(report, "report", "WB")

    assert entry["files"]["added"] == 0
    assert entry["files"]["removed"] == 0
    assert entry["provenance"] == {}
    assert entry["status"] == heg.PAIR_UNASSESSABLE
    assert report["status"] == heg.STATUS_INCOMPLETE


def test_withdraw_treats_the_tree_root_as_covering_everything():
    assert heg._withdraw({"a", "a/b", "c"}, frozenset({heg.TREE_ROOT})) == set()  # pylint: disable=protected-access
    assert heg._withdraw({"a", "a/b", "c"}, frozenset({"a"})) == {"c"}  # pylint: disable=protected-access


def test_an_unreadable_declaration_ledger_makes_the_harvest_incomplete_not_untrustworthy(tmp_path):
    """Exit 3, never the exit 1 that means a tampered baseline was positively detected."""
    bundle = _identical_bundle(tmp_path)
    _mutate_working_file_changed(bundle)
    ledger = bundle / "_build" / "generated-edit-declarations.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b'{"version": 1, "\xff\xfe": 1}')

    report = heg.harvest(bundle)

    assert report["attribution"]["unavailable_reason"] == "unreadable_declarations"
    assert report["status"] == heg.STATUS_INCOMPLETE
    assert heg.main([str(bundle), "--quiet"]) == heg.EXIT_INCOMPLETE


# ---------------------------------------------------------------------------------------------
# Round-4 review: the human-facing report may not claim what the machine-readable one denies.
# ---------------------------------------------------------------------------------------------

CLEAN_CLAIM = "**None.** Every differing byte in this bundle is still hash-identical"


def test_markdown_does_not_claim_cleanliness_when_attribution_is_unavailable(tmp_path):
    """⚠️ An empty `tier_edits` list has two meanings; the prose used to print the reassuring one.

    Measured (blind review round 4): deleting `input_manifest.json` from a differing fixture gave
    `status=incomplete`, `attribution.usable=false` and TWO unattributed differences - and the
    markdown still asserted that every differing byte matched the engine's own record. The JSON said
    `incomplete`; the prose a human actually reads said the opposite.
    """
    bundle = _bundle(tmp_path, entity_baseline="Extract", entity_working="Orders")
    (bundle / "input_manifest.json").unlink()

    report = heg.harvest(bundle)
    markdown = hgr.render_markdown(report)

    assert report["attribution"]["usable"] is False
    assert report["provenance"][heg.PROV_UNATTRIBUTED] > 0
    assert CLEAN_CLAIM not in markdown
    assert "**Undetermined - this is NOT a clean result.**" in markdown
    assert "Why this run is not complete:" in markdown
    for reason in report["incomplete_reasons"]:
        assert reason in markdown


def test_the_clean_claim_asks_the_harvest_rather_than_re_deriving_trust(tmp_path):
    """⚠️ FOUR rounds of review found four ways to re-derive this wrongly. Blind review round 5:

    `unreconciled_drift` and a `pbip/` discovery failure both leave `status=incomplete` with ZERO
    compared paths - so `engine_internal == differing_files` is **vacuously true** and all three of
    the previous round's conditions passed. Each round added a clause that rebuilt "is this
    trustworthy?" from parts and missed a different one.

    The harvest already computes `status` and `incomplete_reasons`. A re-derivation can only ever
    enumerate the reasons known on the day it was written, so this asks the authoritative field and
    every future incompleteness reason flows through automatically.
    """
    complete = {
        "provenance": {"engine_internal": 0, "differing_files": 0},
        "attribution": {"usable": True},
        "baseline_drift": [],
        "baseline_tampered": [],
        "tier_edits": [],
        "status": "complete",
        "incomplete_reasons": [],
    }
    assert hgr._tier_edits_determined(complete) is True

    for reason in ("unreconciled drift: pbip/WB/WB.pbip", "access failure enumerating pbip/"):
        incomplete = {**complete, "status": "incomplete", "incomplete_reasons": [reason]}

        assert hgr._tier_edits_determined(incomplete) is False, reason


def test_an_empty_comparison_can_never_be_the_only_thing_permitting_the_claim(tmp_path):
    """The vacuity itself, stated as a requirement rather than left implicit.

    `engine_internal == differing_files` is TRUE over an empty set, and that is what produced two of
    the four instances. It survives as belt-and-braces beneath the status check; it must never again
    be the load-bearing clause.
    """
    vacuous = {
        "provenance": {"engine_internal": 0, "differing_files": 0},
        "attribution": {"usable": True},
        "baseline_drift": [],
        "baseline_tampered": [],
        "tier_edits": [],
        "status": "incomplete",
        "incomplete_reasons": ["anything at all"],
    }

    # The equality passes; the status does not. The status must win.
    assert vacuous["provenance"]["engine_internal"] == vacuous["provenance"]["differing_files"]
    assert hgr._tier_edits_determined(vacuous) is False


def test_markdown_is_undetermined_when_only_some_paths_are_unattributed(tmp_path):
    """Attribution can be USABLE and still not cover every compared path (a `.pbi` sidecar)."""
    bundle = _identical_bundle(tmp_path)
    _write(bundle / "pbip" / "WB" / "WB.Report" / ".pbi" / "localSettings.json", {"version": "1.0"})

    report = heg.harvest(bundle)
    markdown = hgr.render_markdown(report)

    assert report["attribution"]["usable"] is True
    assert report["provenance"][heg.PROV_UNATTRIBUTED] == 1
    assert CLEAN_CLAIM not in markdown
    assert "**Undetermined - this is NOT a clean result.**" in markdown


def test_markdown_still_states_none_when_the_run_is_fully_attributed(tmp_path):
    """The claim must remain reachable, or "undetermined" carries no information."""
    report = heg.harvest(_bundle(tmp_path, entity_baseline="Extract", entity_working="Orders"))
    markdown = hgr.render_markdown(report)

    assert report["provenance"][heg.PROV_ENGINE] == report["provenance"]["differing_files"]
    assert CLEAN_CLAIM in markdown
    assert "**Undetermined" not in markdown


def test_markdown_does_not_claim_cleanliness_when_the_baseline_drifted(tmp_path):
    bundle = _differing_bundle(tmp_path)
    _mutate_baseline_rewritten(bundle)

    report = heg.harvest(bundle)
    markdown = hgr.render_markdown(report)

    assert report["status"] == heg.STATUS_UNTRUSTWORTHY
    assert CLEAN_CLAIM not in markdown
