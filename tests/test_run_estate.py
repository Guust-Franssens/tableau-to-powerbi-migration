"""The estate coordinator turns an engine run into something safe to hand downstream.

Every test here corresponds to a measured gap in the deterministic tier's output contract, not to a
hypothetical. The engine is not at fault for any of them - it is a batch migrator and its choices are
defensible for that job. They are simply not safe for a CONSUMER, which is what this script is.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_estate  # noqa: E402  # pylint: disable=wrong-import-position
from check_path_ceiling import DIR_CEILING, FILE_CEILING, utf16_len  # noqa: E402


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


def _root_for_length(length: int) -> Path:
    """A synthetic absolute-like root with an exact UTF-16 length."""
    return Path("/" + "r" * (length - 1))


def _boundary_root(unit: str, ceiling: int) -> Path:
    probe = run_estate.project_estate_path_ceiling(Path("/r"), [unit])
    file_length = next(path["length"] for path in probe["paths"] if path["kind"] == "file")
    return _root_for_length(1 + ceiling - file_length + 1)


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


def test_projected_path_uses_utf16_and_accepts_the_measured_file_boundary() -> None:
    unit = "A" * 20
    root = _boundary_root(unit, FILE_CEILING)
    projection = run_estate.project_estate_path_ceiling(root, [unit])
    file_path = next(path for path in projection["paths"] if path["kind"] == "file")
    assert file_path["length"] == FILE_CEILING
    assert projection["status"] == "ok"


def test_projected_path_refuses_the_next_file_and_directory_boundaries() -> None:
    unit = "A" * 20
    root = _boundary_root(unit, FILE_CEILING + 1)
    projection = run_estate.project_estate_path_ceiling(root, [unit])
    assert projection["status"] == "over_ceiling"
    assert any(path["length"] == FILE_CEILING + 1 for path in projection["offenders"])
    assert any(path["length"] == DIR_CEILING + 1 for path in projection["offenders"])


def test_projected_path_counts_supplementary_characters_as_two_units() -> None:
    unit = "😀" * 20
    projection = run_estate.project_estate_path_ceiling(Path("/r"), [unit])
    file_path = next(path for path in projection["paths"] if path["kind"] == "file")
    assert file_path["length"] == utf16_len(file_path["path"])
    assert file_path["length"] > len(file_path["path"])


def test_estate_path_preflight_accepts_short_root_and_refuses_long_root(tmp_path: Path) -> None:
    source = tmp_path / ("A" * 20 + ".twb")
    source.write_text("<workbook />", encoding="utf-8")
    assert run_estate.preflight_estate_path_ceiling(source, Path("/short"))[0] is True
    ok, detail = run_estate.preflight_estate_path_ceiling(source, _boundary_root("A" * 20, FILE_CEILING + 1))
    assert ok is False
    assert "shorter run/output root" in detail


def test_estate_path_preflight_cannot_assess_missing_input(tmp_path: Path) -> None:
    ok, detail = run_estate.preflight_estate_path_ceiling(tmp_path / "missing", Path("/short"))
    assert ok is False
    assert "CANNOT ASSESS" in detail


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


def test_the_generated_manifest_is_written_before_the_engine_receipt(tmp_path: Path, monkeypatch) -> None:
    """Ordering is load-bearing, not cosmetic - the two mechanisms share a file.

    ``write_generated_artifact_manifest`` UPSERTS into ``input_manifest.json``; the engine receipt
    HASHES that same file. Receipt-first therefore leaves ``input_manifest_sha256`` stale on every
    legitimate run, and the credential gate rejects the bundle the engine just produced. Measured
    when merging the two changes, which were developed independently and neither of whose suites
    could observe the interaction.

    This drives ``main()`` rather than the helpers, so re-ordering the real pipeline fails it. A
    helper-level test would document the constraint without guarding it.
    """
    sys.path.insert(0, str(Path(run_estate.__file__).resolve().parent))
    from credential_gate import _receipt_matches_bundle  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    out = tmp_path / "bundle"

    def _fake_engine(_engine: Path, _src: Path, dest: Path, _dax: Path | None) -> tuple[int, str]:
        _write(dest / "report.json", json.dumps(_report()))
        _write(dest / "input_manifest.json", '{"inputs": []}')
        _write(dest / "fabric" / "Orders.SemanticModel" / "definition" / "t.tmdl", "table Orders")
        return 0, ""

    monkeypatch.setattr(run_estate, "run_engine", _fake_engine)
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    argv = [
        "--engine",
        str(tmp_path / "engine"),
        "--allow-noncanonical-engine",
        "--input",
        str(src),
        "--output",
        str(out),
    ]
    assert run_estate.main(argv) == run_estate.EXIT_OK

    receipt = json.loads((out / "engine-output-receipt.json").read_text(encoding="utf-8"))
    assert _receipt_matches_bundle(out, receipt), (
        "the receipt does not describe the bundle main() just produced - "
        "the generated-artifact manifest must be written BEFORE the receipt"
    )


# ---------------------------------------------------------------------------
# One engine, and the bundle says which one (issue #107)
# ---------------------------------------------------------------------------


def test_a_noncanonical_engine_stops_the_run_instead_of_running_it(tmp_path: Path, monkeypatch) -> None:
    """The estate coordinator must not run whatever tree it is pointed at without being told to.

    Measured 2026-08-12: this script's `--engine` DEFAULT was a sibling clone at 2.126.0 while other
    steps resolved the plugin at 2.113.0, and the two emit materially different map visuals. Refusing
    here is what makes "the plugin is the single source" true at the point of execution rather than
    only in a document.
    """
    ran: list[Path] = []
    monkeypatch.setattr(run_estate, "run_engine", lambda engine, *_: (ran.append(engine), (0, ""))[1])

    src = tmp_path / "src"
    src.mkdir()
    argv = ["--engine", str(tmp_path / "elsewhere"), "--input", str(src), "--output", str(tmp_path / "bundle")]
    assert run_estate.main(argv) == run_estate.EXIT_ENGINE_SOURCE
    assert not ran, "the engine ran despite being non-canonical and unacknowledged"


def test_the_bundle_records_which_engine_built_it(tmp_path: Path, monkeypatch) -> None:
    """#107's acceptance criterion: the artifact answers "what built me?" without the machine."""
    engine = tmp_path / "engine"
    (engine / "skills" / "tableau-migration").mkdir(parents=True)
    (engine / "skills" / "tableau-migration" / "VERSION").write_text("2.126.0\n", encoding="utf-8")

    out = tmp_path / "bundle"

    def _fake_engine(_engine: Path, _src: Path, dest: Path, _dax: Path | None) -> tuple[int, str]:
        _write(dest / "report.json", json.dumps(_report()))
        _write(dest / "input_manifest.json", '{"inputs": []}')
        return 0, ""

    monkeypatch.setattr(run_estate, "run_engine", _fake_engine)
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    argv = ["--engine", str(engine), "--allow-noncanonical-engine", "--input", str(src), "--output", str(out)]
    assert run_estate.main(argv) == run_estate.EXIT_OK

    receipt = json.loads((out / "engine-output-receipt.json").read_text(encoding="utf-8"))
    assert receipt["engine"]["version"] == "2.126.0"
    assert receipt["engine"]["root"] == str(engine)
    assert receipt["engine"]["canonical"] is False, "an override must be recorded AS an override"


def test_slice_only_needs_no_engine_at_all(tmp_path: Path, monkeypatch) -> None:
    """Re-deriving handovers from an existing bundle must not require the plugin to be installed."""
    import engine_source  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", tmp_path / "no-plugin-here")
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report(workbooks=[_workbook("Alpha", "AlphaModel")])))
    assert run_estate.main(["--slice-only", "--output", str(out)]) == run_estate.EXIT_OK
    assert (out / "handover" / "Alpha.json").is_file()


# ---------------------------------------------------------------------------
# issue #230: --slice-only skipped the generated-artifact baseline entirely
# ---------------------------------------------------------------------------


def test_slice_only_backfills_a_missing_baseline(tmp_path: Path) -> None:
    """The defect: a bundle built with --slice-only never carried a generated_artifacts baseline.

    Reproduces the exact shape ``migrate_estate.py`` itself writes - an ``input_manifest.json`` with
    no ``generated_artifacts`` key at all - and asserts ``run_estate.py --slice-only`` now backfills
    one instead of leaving ``check_migration_progress.py --tamper`` permanently unable to check it.
    """
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report()))
    _write(out / "input_manifest.json", json.dumps({"assets": [], "root": str(out)}))
    _write(out / "fabric" / "M.SemanticModel" / "definition" / "tables" / "Orders.tmdl", "table Orders")

    assert run_estate.main(["--output", str(out), "--slice-only"]) == run_estate.EXIT_OK

    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    generated = manifest["generated_artifacts"]
    assert generated["coverage"] == "slice_only_backfill"
    assert "fabric/M.SemanticModel/definition/tables/Orders.tmdl" in generated["files"]


def test_slice_only_backfill_never_overwrites_an_existing_baseline(tmp_path: Path) -> None:
    """A prior full engine run through run_estate.py already recorded real evidence - never clobber it."""
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report()))
    original = {
        "version": 1,
        "run_id": "original-run",
        "recorded_at": "2026-08-01T00:00:00+00:00",
        "report_generated_at": _report().get("generated_at"),
        "report_sha256": run_estate.sha256_file(out / "report.json"),
        "files": {"fabric/Stale.SemanticModel/definition/t.tmdl": "deadbeef"},
    }
    _write(out / "input_manifest.json", json.dumps({"generated_artifacts": original}))

    assert run_estate.main(["--output", str(out), "--slice-only"]) == run_estate.EXIT_OK

    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated_artifacts"] == original


def test_slice_only_backfill_does_not_clobber_an_invalid_baseline_either(tmp_path: Path) -> None:
    """Even a broken/mismatched generated_artifacts entry might be tamper evidence - leave it in place."""
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report()))
    invalid = {"version": 999, "files": {}}
    _write(out / "input_manifest.json", json.dumps({"generated_artifacts": invalid}))

    run_estate.backfill_slice_only_baseline(out, _report(), [])

    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated_artifacts"] == invalid


# ---------------------------------------------------------------------------
# The empty-model gate: a bundle that passes everything above and holds no data
# ---------------------------------------------------------------------------


def _unlanded_model(out: Path, workbook: str = "global_superstores_db") -> None:
    """An Import partition over a flat file that was never landed - the measured silent success.

    The path is absolute and belongs to the machine the Tableau workbook was authored on. On the
    Windows host that produced the measured estate the detector calls that `foreign_path`; on a Linux
    CI runner the same path is simply `missing_file`. Both block, which is the point: these
    assertions are about the coordinator's verdict, not about which runner executed them.
    """
    tables = out / "pbip" / workbook / "Orders.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    _write(
        tables / "Orders.tmdl",
        "table Orders\n\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t\tSource = Excel.Workbook(File.Contents("/Users/<author>/Datasets/Orders.xlsx"), null, true)\n'
        "\t\t\tin\n"
        "\t\t\t\tSource\n",
    )


def _slice_only_argv(out: Path) -> list[str]:
    return ["--output", str(out), "--slice-only"]


def test_an_empty_model_blocks_a_bundle_that_the_definition_of_done_let_through(tmp_path: Path, capsys) -> None:
    """The exact measured case: `definition_of_done: warn`, report bound, model contains nothing.

    ``warn`` is deliberately allowed through (see the DoD tests above), so before this gate existed
    this bundle reached the deployer and then a customer. The verdict has to live in the exit code -
    a printed warning is what the engine already produced, and it was not enough.
    """
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report(dod_status="warn")))
    _unlanded_model(out)

    code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_EMPTY_MODEL
    printed = capsys.readouterr().out
    assert "global_superstores_db" in printed
    assert "Orders.xlsx" in printed


def test_a_healthy_bundle_still_exits_zero(tmp_path: Path) -> None:
    """The false-positive control at coordinator level: a landed CSV must not block the estate."""
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report(dod_status="warn")))
    landed = _write(out / "data" / "Orders" / "Extract.csv", "a,b\n1,2\n")
    tables = out / "pbip" / "wb" / "Orders.SemanticModel" / "definition" / "tables"
    _write(
        tables / "Orders.tmdl",
        "table Orders\n\n"
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        f'\t\t\t\tSource = Csv.Document(File.Contents("{landed.as_posix()}"))\n'
        "\t\t\tin\n"
        "\t\t\t\tSource\n",
    )

    assert run_estate.main(_slice_only_argv(out)) == run_estate.EXIT_OK


def test_the_empty_model_verdict_is_printed_even_when_the_definition_of_done_already_failed(
    tmp_path: Path, capsys
) -> None:
    """Precedence is DoD-first, but the READER must still be told about both.

    A failed DoD returns before the empty-model branch, so if the render were emitted there the
    quieter defect would be invisible on exactly the runs that have more than one problem. Measured
    on the 38-workbook estate, that was the actual situation.
    """
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report(dod_status="failed")))
    _unlanded_model(out)

    code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_DOD_FAILED
    assert "EMPTY_MODEL" in capsys.readouterr().out


def test_the_empty_model_verdict_is_persisted_for_later_steps(tmp_path: Path) -> None:
    """The deployer runs in a different process and must not have to re-derive this."""
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_report(dod_status="warn")))
    _unlanded_model(out)

    run_estate.main(_slice_only_argv(out))

    verdict = json.loads((out / "empty-model-check.json").read_text(encoding="utf-8"))
    assert verdict["status"] == "EMPTY_MODELS"
    assert verdict["models"][0]["owner"] == "global_superstores_db"


# ---------------------------------------------------------------------------
# The blank-placeholder gate: a bundle whose report consumes a calc the engine refused
# ---------------------------------------------------------------------------

_REFUSED_CALC = {
    "category": "type_or_shape_mismatch",
    "fallback_reason": "IFNULL arguments return inconsistent types",
    "has_suggestion": False,
    "name": "Last Usage Filter",
    "role": "dimension",
    "target_table": "UDP_SF",
}


def _placeholder_report(dod_status: str = "warn", workbook: str = "Alpha") -> dict:
    """A report.json in the engine's real shape whose one workbook carries a refused calc.

    `pbip_folder` is the engine's own name for the folder the workbook built, and is what the
    checker keys the correlation on; it is carried here because a fixture that omits it would test
    a shape the engine does not emit.
    """
    wb = _workbook(workbook, workbook, requests=[dict(_REFUSED_CALC)])
    wb["pbip_folder"] = f"pbip/{workbook}/{workbook}.pbip"
    return _report(workbooks=[wb], dod_status=dod_status)


def _placeholder_model(out: Path, workbook: str = "Alpha") -> None:
    """The other half of the correlation: the BLANK()-only column the engine emitted instead."""
    tables = out / "pbip" / workbook / f"{workbook}.SemanticModel" / "definition" / "tables"
    _write(tables / "UDP_SF.tmdl", "table UDP_SF\n\n\tcolumn 'Last Usage Filter' = BLANK()\n\t\tsummarizeBy: none\n")


def _report_consuming_placeholder(out: Path, workbook: str = "Alpha") -> None:
    """A shipping PBIR page whose filter depends on the placeholder - the blocking case."""
    page = out / "pbip" / workbook / f"{workbook}.Report" / "definition" / "pages" / "p1"
    _write(page / "page.json", json.dumps({"name": "p1", "displayName": "Overview"}))
    _write(
        page / "visuals" / "v1" / "visual.json",
        json.dumps(
            {
                "name": "v1",
                "visual": {"visualType": "tableEx"},
                "filterConfig": {
                    "filters": [
                        {
                            "name": "f1",
                            "field": {
                                "Column": {
                                    "Expression": {"SourceRef": {"Entity": "UDP_SF"}},
                                    "Property": "Last Usage Filter",
                                }
                            },
                            "type": "Categorical",
                        }
                    ]
                },
            }
        ),
    )


def _without_pbir_validator(monkeypatch) -> None:
    """Run as if Node/the first-party validator were absent, which `check_pbir_valid` supports.

    Not a convenience: PBIR validity OUTRANKS this gate, and these fixtures are hand-written PBIR
    fragments rather than whole reports, so on a machine that has the CLI the run would stop at
    EXIT_INVALID_PBIR and never reach the branch under test. Patching `find_cli` exercises the real
    `scan` down its real SKIPPED path instead of substituting a fake verdict.
    """
    import check_pbir_valid  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(check_pbir_valid, "find_cli", lambda *_args, **_kwargs: None)


def test_a_report_referenced_blank_placeholder_blocks_a_bundle_on_its_first_run(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The FRESH-RUN case, and the one that matters: no `handover/` folder exists yet.

    `<bundle>/handover/` is not engine output - `slice_handovers` writes it in phase 3, while this
    gate runs in phase 2. A gate that reads the slices therefore sees nothing on a first run and
    passes the bundle, then blocks on a SECOND run over the same `--output` folder. Measured on
    identical bytes: exit 0 / "OK - 0 placeholder(s)" first, exit 8 second.

    So the assertion that the folder is absent BEFORE the run and present after is the test, not
    scenery: it pins the phase ordering that made the evidence unreadable, and it is what forces
    the correlation to come from `report.json`.
    """
    _without_pbir_validator(monkeypatch)
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_placeholder_report()))
    _placeholder_model(out)
    _report_consuming_placeholder(out)
    assert not (out / "handover").exists(), "fixture is not a fresh run if the slices already exist"

    code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_BLANK_PLACEHOLDER
    assert (out / "handover").is_dir(), "phase 3 writes the slices AFTER the gate that needed them"
    printed = capsys.readouterr().out
    assert "BLANK-PLACEHOLDER CHECK: REFERENCED" in printed
    assert "Last Usage Filter" in printed


def test_the_blank_placeholder_verdict_is_printed_even_when_the_definition_of_done_already_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Precedence is DoD-first, but the reader must still be told about both."""
    _without_pbir_validator(monkeypatch)
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_placeholder_report(dod_status="failed")))
    _placeholder_model(out)
    _report_consuming_placeholder(out)

    code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_DOD_FAILED
    assert "BLANK-PLACEHOLDER CHECK: REFERENCED" in capsys.readouterr().out


def test_the_blank_placeholder_verdict_is_persisted_for_later_steps(tmp_path: Path, monkeypatch) -> None:
    """The triage step runs in a different process and must not have to re-derive this."""
    _without_pbir_validator(monkeypatch)
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps(_placeholder_report()))
    _placeholder_model(out)
    _report_consuming_placeholder(out)

    run_estate.main(_slice_only_argv(out))

    verdict = json.loads((out / "blank-placeholder-check.json").read_text(encoding="utf-8"))
    assert verdict["status"] == "REFERENCED"
    assert verdict["placeholders_referenced"] == 1
    assert verdict["findings"][0]["owner"] == "Alpha"
    assert verdict["findings"][0]["name"] == "Last Usage Filter"


def test_an_unreadable_handover_input_cannot_silence_the_other_gates(tmp_path: Path, monkeypatch, capsys) -> None:
    """One truncated JSON file used to take the whole coordinator down, with the wrong exit code.

    `GateResults(...)` evaluates this gate before the empty-model one and prints all three verdicts
    afterwards, so an exception here meant NO verdict was printed at all and Python exited 1 - which
    in this script's vocabulary is EXIT_ENGINE_FAILED, "the engine itself exited non-zero".

    The report.json here deliberately does not carry the engine's `workbooks` list, which is what
    sends the checker to its `handover/` fallback and so makes the corrupt slice reachable at all.
    """
    _without_pbir_validator(monkeypatch)
    out = tmp_path / "bundle"
    _write(out / "report.json", json.dumps({"tool": "tableau-fabric-skills"}))
    _write(
        out / "handover" / "Alpha.json",
        json.dumps({"workbook": {"model_translation_handoff": {"requests": [dict(_REFUSED_CALC)]}}}),
    )
    _write(out / "handover" / "Truncated.json", '{"workbook": {')
    _placeholder_model(out)
    _report_consuming_placeholder(out)

    code = run_estate.main(_slice_only_argv(out))

    assert code == run_estate.EXIT_BLANK_PLACEHOLDER, "the readable slice's evidence was lost with the corrupt one"
    printed = capsys.readouterr().out
    assert "EMPTY-MODEL CHECK" in printed, "a corrupt input silenced a sibling gate"
    assert "handover/Truncated.json" in printed
    verdict = json.loads((out / "blank-placeholder-check.json").read_text(encoding="utf-8"))
    assert verdict["handover_unreadable"] == 1
    assert verdict["handover_unreadable_paths"] == ["handover/Truncated.json"]


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


# ---------------------------------------------------------------------------
# issue #250: the destructive-re-run barrier is CHECKED now, not merely documented
#
# The docstring claimed "this script owns that ordering so no agent has to remember it" and owned
# nothing: every gate ran in phase 2, reading output the engine had already written. A landing
# re-run into a bundle holding ~20 items of hand-authored fix work destroyed all of it with no
# --force, no prompt and no pre-check. These tests drive `main()` so that a guard moved back after
# the engine, or quietly turned into an opt-in, fails them.
# ---------------------------------------------------------------------------


def _versioned_engine(root: Path, version: str) -> Path:
    """An engine tree whose VERSION is what `engine_provenance` reads back off disk."""
    skill = root / "skills" / "tableau-migration"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


def _bundle_engine(calls: list[Path] | None = None):
    """A stand-in engine emitting a stable, realistic bundle: a model, a PBIR report and a .pbip.

    DESTRUCTIVE on purpose. `migrate_estate.py` rmtree()s the `.SemanticModel` folder, the whole
    `.pbip` project dir and `<name>.Report` before rewriting them, so a stand-in that merely
    overwrites the files it happens to know about would let a test claim "nothing was destroyed"
    while a real engine had eaten the sentinel beside them. Wiping `pbip/` first is what lets a test
    assert on the DISK rather than only on a call log.

    `calls` is how a test proves the guard is PRE-engine: a refusal must leave it empty.
    """

    def _fake(_engine: Path, _src: Path, dest: Path, _dax: Path | None) -> tuple[int, str]:
        if calls is not None:
            calls.append(dest)
        shutil.rmtree(dest / "pbip", ignore_errors=True)
        shutil.rmtree(dest / "data", ignore_errors=True)
        _write(dest / "report.json", json.dumps(_report()))
        _write(dest / "input_manifest.json", '{"inputs": []}')
        model = dest / "pbip" / "WB" / "WB.SemanticModel"
        _write(model / "definition.pbism", "{}")
        _write(model / "definition" / "tables" / "Orders.tmdl", "table Orders")
        report = dest / "pbip" / "WB" / "WB.Report"
        _write(report / "definition.pbir", "{}")
        _write(report / "definition" / "report.json", '{"pages": []}')
        _write(dest / "pbip" / "WB" / "WB.Data" / "orders.txt", "id,amount\n1,10\n")
        _write(dest / "data" / "orders.csv", "id,amount\n1,10\n")
        _write(dest / "pbip" / "WB" / "WB.pbip", "{}")
        return 0, ""

    return _fake


ORDERS_TMDL = "pbip/WB/WB.SemanticModel/definition/tables/Orders.tmdl"
REPORT_JSON = "pbip/WB/WB.Report/definition/report.json"
TEXTSCAN_DATA = "pbip/WB/WB.Data/orders.txt"


def _landing_argv(engine: Path, src: Path, out: Path, *extra: str) -> list[str]:
    return [
        "--engine",
        str(engine),
        "--allow-noncanonical-engine",
        "--input",
        str(src),
        "--output",
        str(out),
        *extra,
    ]


def _first_run(tmp_path: Path, monkeypatch, version: str = "2.339.0") -> tuple[Path, Path, Path]:
    """Build the bundle the way a real run builds it, so both baselines are the real ones."""
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", version)
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine())
    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_OK
    return engine, src, out


def _relanding(monkeypatch) -> list[Path]:
    """Re-arm the stand-in engine for a SECOND run and return the call log."""
    calls: list[Path] = []
    monkeypatch.setattr(run_estate, "run_engine", _bundle_engine(calls))
    return calls


def test_a_landing_rerun_into_a_pristine_bundle_still_proceeds(tmp_path: Path, monkeypatch) -> None:
    """The documented one-run landing flow must survive the guard (issue #250, DoD 2).

    A guard that blocked every re-run would be trivially "safe" and would break the exact workflow
    the barrier exists to protect. Both baselines are re-hashed here against a bundle nothing has
    touched, so a false positive fails this test rather than an operator's estate.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    calls = _relanding(monkeypatch)
    dax = _write(tmp_path / "approved.json", json.dumps({"Rank": "RANKX(...)"}))

    code = run_estate.main(_landing_argv(engine, src, out, "--approved-dax", str(dax)))

    assert code == run_estate.EXIT_OK
    assert calls == [out], "a pristine bundle must still be re-runnable"


def test_hand_authored_work_in_the_bundle_refuses_the_landing_rerun(tmp_path: Path, monkeypatch, capsys) -> None:
    """The reported case: bulk approved DAX landed into a bundle holding manual fix work.

    The engine's own stale-output guard exempts `--approved-dax`, so nothing upstream refuses this.
    `calls` is the load-bearing assertion: the refusal has to happen BEFORE the delete-and-recreate,
    not after it.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = SUM(Orders[Amount])\n", encoding="utf-8")
    calls = _relanding(monkeypatch)
    dax = _write(tmp_path / "approved.json", json.dumps({"Rank": "RANKX(...)"}))

    code = run_estate.main(_landing_argv(engine, src, out, "--approved-dax", str(dax)))

    assert code == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == [], "the engine ran anyway - the barrier must be PRE-engine"
    assert ORDERS_TMDL in capsys.readouterr().out, "a refusal must name what would be destroyed"


def test_a_hand_edited_report_file_is_work_the_receipt_catches(tmp_path: Path, monkeypatch) -> None:
    """PBIR JSON is now receipt-backed engine output; changing it is downstream work."""
    engine, src, out = _first_run(tmp_path, monkeypatch)
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    assert REPORT_JSON in {record["path"] for record in receipt["artifacts"]}

    (out / REPORT_JSON).write_text('{"pages": [{"name": "p1"}]}', encoding="utf-8")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_a_new_artifact_under_pbip_counts_as_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """An agent-authored model file the engine never wrote is work too, not just an edit."""
    engine, src, out = _first_run(tmp_path, monkeypatch)
    _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Custom.tmdl", "table Custom")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_a_newly_authored_pbir_file_is_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """HIGH 1: PBIR is `.json`, which the engine receipt's suffix allowlist does not record.

    Addition-detection used to run off that allowlist, so a hand-authored page or visual was
    invisible - while the engine deletes the whole `.Report` directory around it. The barrier now
    allowlists LOCATIONS, so anything inside a folder the engine rmtree()s is accounted for.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    authored = out / "pbip" / "WB" / "WB.Report" / "definition" / "pages" / "p1" / "visuals" / "v1"
    sentinel = _write(authored / "visual.json", json.dumps({"name": "v1"}))
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    assert not any(record["path"].endswith("visual.json") for record in receipt["artifacts"])
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file(), "the authored PBIR file was destroyed by a run the barrier let through"


def test_a_textscan_extract_beside_the_project_is_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """HIGH 2: `.txt` is in neither the receipt's suffix list nor the generated-artifact baseline.

    A packaged Tableau `textscan` datasource lands as flat files under `<project>.Data` and `data/`,
    both of which the engine deletes. Format allowlists cannot cover this; location coverage can.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    assert TEXTSCAN_DATA not in {record["path"] for record in receipt["artifacts"]}
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    assert TEXTSCAN_DATA not in manifest[run_estate.GENERATED_ARTIFACTS_KEY]["files"]
    assert TEXTSCAN_DATA in manifest[run_estate.ENGINE_TREE_KEY]["files"]

    (out / TEXTSCAN_DATA).write_text("id,amount\n1,10\n2,99\n", encoding="utf-8")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_a_new_flat_file_under_data_is_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """The addition shape of the same gap: a landed extract the engine never wrote."""
    engine, src, out = _first_run(tmp_path, monkeypatch)
    sentinel = _write(out / "data" / "hand-landed.txt", "id,amount\n7,70\n")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file()


def test_a_slice_only_backfill_cannot_bless_downstream_work_as_engine_output(tmp_path: Path, monkeypatch) -> None:
    """HIGH 5: `--slice-only` hashes the WORKING COPY, downstream edits included.

    It writes that as `generated_artifacts` with `coverage: "slice_only_backfill"`. Trusting it
    would launder an agent's edits into "engine output" and hand the next destructive run a clean
    bill of health - one step removed from where the barrier was looking. The marker is now read,
    the baseline is not trusted, and the bundle stays indeterminate until acknowledged.
    """
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    _write(out / "report.json", json.dumps(_report()))
    sentinel = _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Hand.tmdl", "table Hand")

    assert run_estate.main(["--output", str(out), "--slice-only"]) == run_estate.EXIT_OK
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    generated = manifest[run_estate.GENERATED_ARTIFACTS_KEY]
    assert generated["coverage"] == run_estate.SLICE_ONLY_COVERAGE
    assert "pbip/WB/WB.SemanticModel/definition/tables/Hand.tmdl" in generated["files"], (
        "fixture no longer reproduces the laundering shape - the backfill must have hashed the edit"
    )
    assert run_estate.ENGINE_TREE_KEY not in manifest, "--slice-only must not write an engine-output tree"

    calls = _relanding(monkeypatch)
    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file()


def test_a_deleted_engine_artifact_counts_as_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """A bundle missing something the receipt attests to is no longer the bundle that was measured."""
    engine, src, out = _first_run(tmp_path, monkeypatch)
    (out / ORDERS_TMDL).unlink()
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_accepting_the_rewrite_proceeds_and_the_bundle_records_what_was_destroyed(tmp_path: Path, monkeypatch) -> None:
    """DoD 3: the opt-out works, and the ARTIFACT says the loss was deliberate.

    The record is written before the engine runs and lives at the bundle root, which the engine's
    rmtree sites do not touch - so it survives the rewrite it describes.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = 1\n", encoding="utf-8")
    calls = _relanding(monkeypatch)

    code = run_estate.main(_landing_argv(engine, src, out, "--accept-bundle-rewrite"))

    assert code == run_estate.EXIT_OK
    assert calls == [out]
    record = json.loads((out / run_estate.BUNDLE_REWRITE_RECORD).read_text(encoding="utf-8"))
    assert len(record["records"]) == 1
    assert record["records"][0]["accepted_bundle_rewrite"] is True
    assert ORDERS_TMDL in record["records"][0]["destroyed"]["modified"]


def test_a_bundle_with_no_baseline_blocks_rather_than_reporting_clean(tmp_path: Path, monkeypatch, capsys) -> None:
    """HIGH 3: a pre-receipt or third-party bundle cannot be assessed, so it must not be waved through.

    The first cut treated "no baseline" as "no drift" and destroyed a sentinel at exit 0, told apart
    from a real pass only by warning TEXT. Unassessable is now its own blocking state; the flag is
    how a legacy bundle stays usable, which is exactly what the flag is for.
    """
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    _write(out / "report.json", json.dumps(_report()))
    sentinel = _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Hand.tmdl", "table Hand")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file(), "the sentinel was destroyed by a run the barrier let through"
    assert "CANNOT ASSESS" in capsys.readouterr().out


def test_an_unassessable_bundle_is_recoverable_with_both_acknowledgements(tmp_path: Path, monkeypatch) -> None:
    """The escape hatch has to work, or the barrier bricks every bundle built before it existed."""
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    _write(out / "report.json", json.dumps(_report()))
    _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Hand.tmdl", "table Hand")
    calls = _relanding(monkeypatch)

    code = run_estate.main(_landing_argv(engine, src, out, "--accept-bundle-rewrite", "--accept-engine-version-change"))

    assert code == run_estate.EXIT_OK
    assert calls == [out]
    record = json.loads((out / run_estate.BUNDLE_REWRITE_RECORD).read_text(encoding="utf-8"))
    assert record["records"][0]["coverage_complete"] is False
    assert record["records"][0]["coverage_gaps"], "an acknowledgement must record what could not be assessed"


def test_emptied_baselines_block_exactly_like_missing_ones(tmp_path: Path, monkeypatch) -> None:
    """HIGH 3, second shape: `artifacts: []` and `files: {}` attest to nothing.

    They previously behaved identically to a real pass and were distinguished only by warning text,
    never by exit code - which is the same defect wearing a different hat.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    receipt["artifacts"] = []
    (out / run_estate.ENGINE_RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    manifest[run_estate.GENERATED_ARTIFACTS_KEY]["files"] = {}
    manifest[run_estate.ENGINE_TREE_KEY]["files"] = {}
    (out / "input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    sentinel = _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Hand.tmdl", "table Hand")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file()


def test_an_emptied_baseline_does_not_invent_a_file_list(tmp_path: Path, monkeypatch) -> None:
    """Blocking is right; naming phantom victims is not.

    With no trustworthy baseline every file in the bundle is ambiguous, so listing them as "added"
    would imply the rest had been cleared. The block comes from the coverage gap alone.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    receipt["artifacts"] = []
    (out / run_estate.ENGINE_RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
    (out / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    _relanding(monkeypatch)

    code = run_estate.main(_landing_argv(engine, src, out, "--accept-bundle-rewrite", "--accept-engine-version-change"))

    assert code == run_estate.EXIT_OK
    record = json.loads((out / run_estate.BUNDLE_REWRITE_RECORD).read_text(encoding="utf-8"))["records"][0]
    assert record["destroyed"] == {"modified": [], "added": [], "missing": []}
    assert record["coverage_gaps"]


def test_a_bundle_built_by_a_different_engine_version_is_not_rewritten_by_default(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """2.113.0 and 2.126.0 are not interchangeable (#107) - rewriting in place mixes both."""
    _, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    newer = _versioned_engine(tmp_path / "engine2", "2.260.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(newer, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    printed = capsys.readouterr().out
    assert "2.141.0" in printed and "2.260.0" in printed

    assert run_estate.main(_landing_argv(newer, src, out, "--accept-engine-version-change")) == run_estate.EXIT_OK
    assert calls == [out]


def test_the_same_engine_version_is_not_a_finding(tmp_path: Path, monkeypatch) -> None:
    """The version guard must not fire on the ordinary case, or it is noise that gets flagged away."""
    engine, src, out = _first_run(tmp_path, monkeypatch, version="2.339.0")
    same = _versioned_engine(tmp_path / "engine-copy", "2.339.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(same, src, out)) == run_estate.EXIT_OK
    assert calls == [out]
    assert engine != same


def test_accepting_an_engine_version_change_does_not_waive_the_destruction_guard(tmp_path: Path, monkeypatch) -> None:
    """The reason these are two flags and not one.

    A single acknowledgement would mean an operator who knows the engine moved silently also waives
    the guard on downstream work they did not know was there - which moves the failure boundary
    instead of removing it.
    """
    _, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = 1\n", encoding="utf-8")
    newer = _versioned_engine(tmp_path / "engine2", "2.260.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(newer, src, out, "--accept-engine-version-change")) == (
        run_estate.EXIT_BUNDLE_REWRITE
    )
    assert calls == []


def test_accepting_the_rewrite_does_not_waive_the_engine_version_guard(tmp_path: Path, monkeypatch) -> None:
    """The mirror image: accepting the loss of work says nothing about mixing engine versions."""
    _, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = 1\n", encoding="utf-8")
    newer = _versioned_engine(tmp_path / "engine2", "2.260.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(newer, src, out, "--accept-bundle-rewrite")) == (
        run_estate.EXIT_BUNDLE_REWRITE
    )
    assert calls == []


def test_slice_only_still_works_against_a_bundle_full_of_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """`--slice-only` legitimately points at an EXISTING bundle on every invocation.

    It never invokes the engine (see `resolve_run_engine`), so there is no delete-and-recreate to
    guard against. The call log is the load-bearing assertion and the reason this test was rewritten:
    asserting only `EXIT_OK` passed even when `--slice-only` was mutated into running the engine,
    because the exit code cannot tell "skipped the engine" from "ran it and it worked".
    """
    _, _, out = _first_run(tmp_path, monkeypatch)
    sentinel = _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Custom.tmdl", "table C")
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = 1\n", encoding="utf-8")
    calls = _relanding(monkeypatch)

    assert run_estate.main(["--output", str(out), "--slice-only"]) == run_estate.EXIT_OK
    assert calls == [], "--slice-only ran the engine, which is the destructive path it exists to avoid"
    assert sentinel.is_file(), "downstream work was destroyed by a --slice-only run"
    assert not (out / run_estate.BUNDLE_REWRITE_RECORD).exists()


def test_a_desktop_refresh_sidecar_is_not_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """A normal refresh writes `.pbi/cache.abf`; blocking on that would train operators to flag past it.

    The `.pbi` model file is the sharp case: it carries an artifact suffix the receipt DOES record
    elsewhere, so only the explicit volatile-folder exclusion keeps it out of the accounting.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    _write(out / "pbip" / "WB" / "WB.SemanticModel" / ".pbi" / "cache.abf", "refresh cache")
    _write(out / "pbip" / "WB" / "WB.SemanticModel" / ".pbi" / "unapplied" / "Orders.tmdl", "table Orders")
    _write(out / "pbip" / "WB" / "WB.Report" / ".pbi" / "localSettings.json", "{}")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_OK
    assert calls == [out]


def test_a_replay_script_nested_under_a_destructive_root_is_downstream_work(tmp_path: Path, monkeypatch) -> None:
    """`_build/` is this repo's durable replay-script convention, not scratch.

    AGENTS.md requires "every edit re-runnable from `_build/`", so `pbip/<project>/_build/replay.py`
    is exactly where an agent's re-runnable work lives - and it sits inside a directory the engine
    rmtree()s. The barrier used to borrow the generated-artifact manifest's SCRATCH predicate, which
    answers a different question, and so walked straight past it: a re-run destroyed the replay
    script for the very edits it reproduces, at exit 0.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    sentinel = _write(out / "pbip" / "WB" / "_build" / "replay.py", "# re-runnable edit for this unit\n")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file(), "the replay script was destroyed by a run the barrier let through"


@pytest.mark.parametrize("scratch_dir", sorted(run_estate.SCRATCH_DIRS))
def test_no_scratch_component_survives_inside_a_destructive_root(tmp_path: Path, monkeypatch, scratch_dir: str) -> None:
    """`_build` was the reported case; the predicate had excluded the whole set.

    Parametrised over the live constant rather than a copied list, so growing `SCRATCH_DIRS` cannot
    silently re-open the hole for a name nobody thought to re-test.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    sentinel = _write(out / "pbip" / "WB" / scratch_dir / "work.py", "# agent work\n")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file()


def test_a_bundle_root_build_folder_is_not_guarded(tmp_path: Path, monkeypatch) -> None:
    """The other side of the boundary: the fix must not over-reach into what the engine never deletes.

    `<bundle>/_build/` sits outside `ENGINE_TREE_ROOTS`, survives a re-run untouched, and is where
    replay scripts for the estate as a whole live. Guarding it would refuse every second run of a
    bundle whose declared edits were recorded correctly - the scope is the destructive roots, not the
    folder name.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    sentinel = _write(out / "_build" / "replay.py", "# estate-level re-runnable edit\n")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_OK
    assert calls == [out]
    assert sentinel.is_file()


def test_a_receipt_that_attests_to_nothing_is_not_read_as_a_clean_bundle(tmp_path: Path, monkeypatch, capsys) -> None:
    """An empty `artifacts` list is an absence of evidence, not evidence of absence."""
    engine, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    receipt = json.loads((out / run_estate.ENGINE_RECEIPT).read_text(encoding="utf-8"))
    receipt["artifacts"] = []
    (out / run_estate.ENGINE_RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
    (out / "input_manifest.json").write_text('{"inputs": []}', encoding="utf-8")
    newer = _versioned_engine(tmp_path / "engine2", "2.260.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(newer, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert "lists no usable artifacts" in capsys.readouterr().out
    assert engine != newer


def test_a_truncated_receipt_blocks_the_version_guard_rather_than_passing_it(tmp_path: Path, monkeypatch) -> None:
    """HIGH 4: one broken byte used to disable the engine-version guard entirely.

    A malformed receipt parses to None, `recorded_version` becomes None, and "is it different?"
    silently answered "no". Unknown is indeterminate now and blocks - and the rewrite flag must not
    answer it, because "I accept losing my work" says nothing about which engine rebuilds it.
    """
    _, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    receipt_path = out / run_estate.ENGINE_RECEIPT
    receipt_path.write_text(receipt_path.read_text(encoding="utf-8")[:40], encoding="utf-8")
    newer = _versioned_engine(tmp_path / "engine2", "2.260.0")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(newer, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert run_estate.main(_landing_argv(newer, src, out, "--accept-bundle-rewrite")) == (
        run_estate.EXIT_BUNDLE_REWRITE
    )
    assert calls == []


def test_an_engine_tree_with_no_version_is_indeterminate_not_unchanged(tmp_path: Path, monkeypatch) -> None:
    """The mirror case: if THIS run's engine has no VERSION, nothing can be compared either."""
    _, src, out = _first_run(tmp_path, monkeypatch, version="2.141.0")
    nameless = tmp_path / "engine-no-version"
    (nameless / "skills" / "tableau-migration" / "scripts").mkdir(parents=True)
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(nameless, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_a_slice_only_backfill_is_distrusted_even_when_the_other_baselines_look_fine(
    tmp_path: Path, monkeypatch
) -> None:
    """Defence in depth for HIGH 5, isolated from the coverage gap that usually fires first.

    In today's flows a `slice_only_backfill` block only ever coexists with a MISSING tree, so the
    tree gap blocks first and the distrust never gets to speak - which is precisely how a redundant
    check rots. This constructs the shape directly: a bundle whose tree and receipt are intact, and
    whose `generated_artifacts` came from a backfill. The backfill's hashes are not evidence of
    engine origin no matter what sits beside them, so the bundle stays indeterminate.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    hand = _write(out / "pbip" / "WB" / "WB.SemanticModel" / "definition" / "tables" / "Hand.tmdl", "table Hand")
    relative = "pbip/WB/WB.SemanticModel/definition/tables/Hand.tmdl"
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    manifest[run_estate.ENGINE_TREE_KEY]["files"][relative] = run_estate.sha256_file(hand)
    manifest[run_estate.GENERATED_ARTIFACTS_KEY] = {
        "version": 1,
        "coverage": run_estate.SLICE_ONLY_COVERAGE,
        "files": {relative: run_estate.sha256_file(hand)},
    }
    (out / "input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_a_legacy_bundle_with_a_receipt_but_no_tree_cannot_clear_an_added_file(tmp_path: Path, monkeypatch) -> None:
    """The realistic HIGH 1 shape: every bundle built before this barrier existed.

    Its receipt is valid, its generated-artifact baseline is valid, and the engine has not moved -
    so nothing else raises a finding. Only the missing tree makes an ADDED file undecidable, and
    only saying so blocks. Suppressing that one gap turns this bundle back into a silent exit 0.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    manifest = json.loads((out / "input_manifest.json").read_text(encoding="utf-8"))
    del manifest[run_estate.ENGINE_TREE_KEY]
    (out / "input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    authored = out / "pbip" / "WB" / "WB.Report" / "definition" / "pages" / "p1" / "visuals" / "v1"
    sentinel = _write(authored / "visual.json", json.dumps({"name": "v1"}))
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []
    assert sentinel.is_file()


def test_a_corrupt_receipt_still_blocks_when_only_the_version_change_was_accepted(tmp_path: Path, monkeypatch) -> None:
    """ "I accept a possible engine change" is not "I accept not knowing what is in the bundle".

    With the version half acknowledged, an unreadable receipt is the ONLY remaining finding - so
    this is what proves the receipt gap carries its own weight rather than riding on the version
    guard that usually fires alongside it.
    """
    engine, src, out = _first_run(tmp_path, monkeypatch)
    (out / run_estate.ENGINE_RECEIPT).write_text("{ truncated", encoding="utf-8")
    calls = _relanding(monkeypatch)

    code = run_estate.main(_landing_argv(engine, src, out, "--accept-engine-version-change"))

    assert code == run_estate.EXIT_BUNDLE_REWRITE
    assert calls == []


def test_an_existing_but_non_bundle_output_folder_is_not_treated_as_a_bundle(tmp_path: Path, monkeypatch) -> None:
    """The over-reach guard: `--output` may legitimately be a folder that simply already exists.

    An operator's scratch directory holds nothing the engine wrote, so there is nothing to protect
    and blocking would be noise on a first run. "Cannot assess" must block only where the engine has
    actually been.
    """
    _without_pbir_validator(monkeypatch)
    engine = _versioned_engine(tmp_path / "engine", "2.339.0")
    out = tmp_path / "bundle"
    src = tmp_path / "src"
    src.mkdir()
    (src / "unit.twb").write_text("<workbook />", encoding="utf-8")
    _write(out / "notes.md", "operator scratch, nothing the engine wrote")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out)) == run_estate.EXIT_OK
    assert calls == [out]


def test_a_dry_run_reports_the_refusal_and_never_writes_an_acknowledgement(tmp_path: Path, monkeypatch) -> None:
    """`--dry-run` says what WOULD happen, so it must say "this would be refused" - and change nothing."""
    engine, src, out = _first_run(tmp_path, monkeypatch)
    (out / ORDERS_TMDL).write_text("table Orders\n\n\tmeasure Sales = 1\n", encoding="utf-8")
    calls = _relanding(monkeypatch)

    assert run_estate.main(_landing_argv(engine, src, out, "--dry-run")) == run_estate.EXIT_BUNDLE_REWRITE
    assert run_estate.main(_landing_argv(engine, src, out, "--dry-run", "--accept-bundle-rewrite")) == (
        run_estate.EXIT_OK
    )
    assert calls == []
    assert not (out / run_estate.BUNDLE_REWRITE_RECORD).exists(), "a dry run must not write into the bundle"
