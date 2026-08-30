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
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "harvest_engine_gaps.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import harvest_engine_gaps as heg  # noqa: E402  # pylint: disable=wrong-import-position

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
    assert heg.SHAPE_LAYOUT in report["tier_edits"][0]["shapes"]


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
    bundle = _bundle(tmp_path)
    _write(bundle / "pbip" / "WB" / "WB.Report" / VISUAL, _visual("Orders", position=7))
    _write(
        bundle / "_build" / "generated-edit-declarations.json",
        {
            "version": 1,
            "declarations": [
                {
                    "version": 1,
                    "target": "pbip/WB/WB.Report/" + VISUAL,
                    "script_identity": "scripts/fix_layout.py",
                }
            ],
        },
    )

    report = heg.harvest(bundle)

    assert report["tier_edits"][0]["declared_by"] == "scripts/fix_layout.py"


# ---------------------------------------------------------------------------------------------
# Shape classification.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("/position/x", heg.SHAPE_LAYOUT),
        ("/position/tabOrder", heg.SHAPE_LAYOUT),
        ("/filterConfig/filters[]/name", heg.SHAPE_FILTER),
        ("/visual/objects/general[]/properties/filter/filter/From[]/Entity", heg.SHAPE_FILTER),
        ("/visual/visualType", heg.SHAPE_VISUAL_TYPE),
        ("/datasetReference/byPath/path", heg.SHAPE_REBIND),
        ("/pageOrder[]", heg.SHAPE_PAGE_ORDER),
        ("/activePageName", heg.SHAPE_PAGE_ORDER),
        ("/resourcePackages[]/items[]", heg.SHAPE_RESOURCES),
        ("/visual/query/queryState/Y/projections[]/queryRef", heg.SHAPE_MODEL_NAMES),
        ("/visual/query/queryState/Y/projections[]/field/Aggregation", heg.SHAPE_QUERY),
        ("/visual/objects/dataPoint[]/properties/fill/solid/color", heg.SHAPE_FORMATTING),
        ("/somethingEntirelyNew", heg.SHAPE_UNCLASSIFIED),
    ],
)
def test_pointer_shape_classification(pointer, expected):
    assert heg._pointer_shape(pointer) == expected  # pylint: disable=protected-access


def test_entity_rename_into_the_bound_model_is_binding_resolution(tmp_path):
    """The reference copy names entities that exist nowhere; resolving them is by design."""
    report = heg.harvest(_bundle(tmp_path, entity_baseline="Extract", entity_working="Orders"))
    shapes = {row["shape"] for row in report["shapes"]}
    assert heg.SHAPE_BINDING in shapes
    assert heg.SHAPE_MODEL_NAMES not in shapes


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
    assert heg.SHAPE_MODEL_NAMES in shapes
    assert heg.SHAPE_BINDING not in shapes


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
    assert heg.SHAPE_BINARY not in shapes


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
    markdown = heg.render_markdown(report)
    assert "**None.**" in markdown
    assert "engine_internal" in markdown


def test_markdown_lists_tier_edits_when_they_exist(tmp_path):
    bundle = _bundle(tmp_path)
    _write(bundle / "pbip" / "WB" / "WB.Report" / VISUAL, _visual("Orders", position=99))
    markdown = heg.render_markdown(heg.harvest(bundle))
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
