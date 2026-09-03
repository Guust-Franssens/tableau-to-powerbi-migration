"""Regression tests for `scripts/promote_unit.py` - the phase 2 -> phase 3 ship step (issue #458).

Everything here runs against a synthetic package under `tmp_path`, so no test touches the real
`migrations/` tree, the reference estate, or any package on disk. `--migrations-root` exists for
exactly this reason.

Two things are deliberately NOT mocked away:

* the **positive controls** promote a real, complete package for BOTH documented shapes and assert
  the deliverable is openable-shaped afterwards. A promoter that refuses everything passes every
  negative test, so a refusal-only suite is worthless.
* one test lets the REAL `check_unit.py` subprocess run, so the wiring in criterion 2 is proven
  rather than assumed. The rest stub `run_check_unit` because a synthetic package cannot pass a
  gate that reads oracle evidence, and stubbing it is the only way to reach the code AFTER it.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
# pylint: disable=wrong-import-position
import promote_unit as pu

PASSING_GATE = {"ran": True, "exit_code": 0, "status": "AUTOMATED_CHECKS_PASS", "passed": True}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_report(report: Path, model_name: str, *, pages: int = 2, visuals_per_page: int = 2) -> None:
    """A structurally complete PBIR report: a pbir reference, pages, and real visuals."""
    _write_json(
        report / "definition.pbir",
        {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
            "version": "4.0",
            "datasetReference": {"byPath": {"path": f"../{model_name}"}},
        },
    )
    page_ids = [f"page-{index}" for index in range(pages)]
    _write_json(report / "definition" / "pages" / "pages.json", {"pageOrder": page_ids})
    for page_id in page_ids:
        page_dir = report / "definition" / "pages" / page_id
        _write_json(page_dir / "page.json", {"name": page_id, "displayName": page_id})
        for visual_index in range(visuals_per_page):
            _write_json(
                page_dir / "visuals" / f"v{visual_index}" / "visual.json",
                {"name": f"v{visual_index}", "visual": {"visualType": "clusteredColumnChart"}},
            )


def _write_model(model: Path, *, tables: int = 2) -> None:
    """A structurally complete TMDL semantic model: a model.tmdl and real table declarations."""
    definition = model / "definition"
    definition.mkdir(parents=True, exist_ok=True)
    (definition / "model.tmdl").write_text("model Model\n\tculture: en-US\n", encoding="utf-8")
    (definition / "tables").mkdir(parents=True, exist_ok=True)
    for index in range(tables):
        (definition / "tables" / f"T{index}.tmdl").write_text(
            f"table T{index}\n\tlineageTag: t{index}\n\n\tcolumn A\n\t\tdataType: string\n",
            encoding="utf-8",
        )


def make_package(root: Path, *, unit: str = "Wb", model_name: str = "Model.SemanticModel") -> Path:
    """A complete, promotable phase-2 package - the shape `package_unit.py` emits."""
    package = root / unit
    fabric = package / "fabric"
    _write_report(fabric / f"{unit}.Report", model_name)
    _write_model(fabric / model_name)
    _write_json(fabric / f"{unit}.pbip", {"version": "1.0", "artifacts": [{"report": {"path": f"{unit}.Report"}}]})
    _write_json(package / "engine-output-receipt.json", {"engine": {"version": "2.353.0", "canonical": True}})
    _write_json(package / "package-manifest.json", {"unit": unit, "kind": "workbook"})
    return package


@pytest.fixture(name="package")
def package_fixture(tmp_path: Path) -> Path:
    """One complete package per test, under the test's own tmp_path."""
    return make_package(tmp_path / "packages")


@pytest.fixture(name="migrations")
def migrations_fixture(tmp_path: Path) -> Path:
    """A `migrations/` root that is NOT the repo's."""
    return tmp_path / "migrations"


@pytest.fixture(name="pass_gate")
def pass_gate_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `check_unit.py` to a PASS so tests can reach the code that runs after it."""
    monkeypatch.setattr(pu, "run_check_unit", lambda package, repo_root: dict(PASSING_GATE))


def run(package: Path, migrations: Path, *extra: str) -> int:
    """Invoke the CLI in-process and return its exit code."""
    argv = ["--package", str(package), "--slug", "wb", "--migrations-root", str(migrations), *extra]
    return pu.main(argv)


# --------------------------------------------------------------------------------------
# Positive controls. A promoter that refuses everything passes every negative test below.
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_model_per_workbook_promotes_contents_as_siblings(package: Path, migrations: Path) -> None:
    """Criteria 1 + 3a: the CONTENTS of `fabric/` land as siblings, never nested one level deeper.

    The source folder is named for the WORKBOOK while the model inside is named for the DATASOURCE,
    so copying the folder itself nests them wrongly and the sibling `byPath` stops resolving.
    """
    assert run(package, migrations) == pu.EXIT_OK

    fabric = migrations / "workbooks" / "wb" / "fabric"
    assert (fabric / "Wb.Report" / "definition.pbir").is_file(), "report must land directly under fabric/"
    assert (fabric / "Model.SemanticModel" / "definition" / "model.tmdl").is_file(), "model must be its SIBLING"
    assert (fabric / "Wb.pbip").is_file(), "the loose .pbip travels with the report"
    assert not (fabric / "fabric").exists(), "the package's fabric/ folder itself must not be copied"
    assert not (fabric / "Wb").exists(), "the unit folder must not be copied either"


@pytest.mark.usefixtures("pass_gate")
def test_shared_datasource_splits_the_halves_and_rewrites_bypath(package: Path, migrations: Path) -> None:
    """Criteria 1 + 3b: the model lands ONCE under datasources/, the report under workbooks/, and
    `definition.pbir` is rewritten to the four-levels-up path that reaches it."""
    assert run(package, migrations, "--datasource-slug", "shared-ds") == pu.EXIT_OK

    report = migrations / "workbooks" / "wb" / "fabric" / "Wb.Report"
    model = migrations / "datasources" / "shared-ds" / "fabric" / "Model.SemanticModel"
    assert report.is_dir() and model.is_dir()
    assert not (migrations / "workbooks" / "wb" / "fabric" / "Model.SemanticModel").exists(), (
        "the shared model must NOT also be left beside the report"
    )
    written = json.loads((report / "definition.pbir").read_text(encoding="utf-8"))
    assert (
        written["datasetReference"]["byPath"]["path"] == "../../../../datasources/shared-ds/fabric/Model.SemanticModel"
    )


@pytest.mark.usefixtures("pass_gate")
def test_shared_datasource_bypath_resolves_on_disk_from_inside_the_report(package: Path, migrations: Path) -> None:
    """Criterion 4, positive half: four levels up is not an arithmetic claim - resolve it."""
    assert run(package, migrations, "--datasource-slug", "shared-ds") == pu.EXIT_OK
    report = migrations / "workbooks" / "wb" / "fabric" / "Wb.Report"
    declared = json.loads((report / "definition.pbir").read_text(encoding="utf-8"))["datasetReference"]["byPath"][
        "path"
    ]
    target = (report / declared).resolve()
    assert target.is_dir(), f"{declared} does not resolve from inside the .Report folder"
    assert (target / "definition" / "model.tmdl").is_file(), "it resolves to something that is not a model"


@pytest.mark.usefixtures("pass_gate")
def test_a_datasource_only_package_promotes_under_datasources(tmp_path: Path, migrations: Path) -> None:
    """A package with a model and no report is a datasource unit, and lands as one."""
    package = tmp_path / "packages" / "DS"
    _write_model(package / "fabric" / "Shared.SemanticModel")
    _write_json(package / "package-manifest.json", {"unit": "DS", "kind": "datasource"})
    assert run(package, migrations) == pu.EXIT_OK
    assert (
        migrations / "datasources" / "wb" / "fabric" / "Shared.SemanticModel" / "definition" / "model.tmdl"
    ).is_file()
    assert not (migrations / "workbooks").exists(), "a datasource-only unit must not create a workbook deliverable"


# --------------------------------------------------------------------------------------
# Criterion 2 - the gate is re-run here, and refusal is by EXIT CODE
# --------------------------------------------------------------------------------------


def test_a_failing_check_unit_refuses_the_promotion_and_ships_nothing(
    package: Path, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 2: a non-zero `check_unit.py` exit REFUSES, and nothing is written."""
    monkeypatch.setattr(
        pu,
        "run_check_unit",
        lambda p, r: {"ran": True, "exit_code": 1, "status": "FINDINGS", "passed": False},
    )
    assert run(package, migrations) == pu.EXIT_REFUSED_BY_GATE
    assert not migrations.exists(), "a refused promotion must not create the deliverable"


def test_the_real_check_unit_subprocess_is_what_refuses(package: Path, migrations: Path) -> None:
    """The gate is genuinely invoked as a subprocess, not merely modelled.

    Nothing is stubbed here: a synthetic package carries no oracle evidence, so the real
    `check_unit.py` cannot return 0 - and the promotion must refuse on ITS answer.
    """
    exit_code = run(package, migrations, "--json", str(migrations.parent / "envelope.json"))
    assert exit_code == pu.EXIT_REFUSED_BY_GATE
    envelope = json.loads((migrations.parent / "envelope.json").read_text(encoding="utf-8"))
    assert envelope["check_unit"]["ran"] is True
    assert envelope["check_unit"]["exit_code"] not in (0, None), "the real gate's exit code must be recorded"
    assert not migrations.exists()


def test_force_overrides_the_gate_and_the_override_is_recorded(package: Path, migrations: Path) -> None:
    """Criterion 2 + 6: `--force` may promote past a failing gate, but the record must say so and
    must carry the exit code that was observed - an unchecked promotion can never look checked."""
    assert run(package, migrations, "--force") == pu.EXIT_OK
    record = json.loads((migrations / "workbooks" / "wb" / "promotion-record.json").read_text(encoding="utf-8"))
    assert record["forced"] is True
    assert record["check_unit"]["passed"] is False
    assert isinstance(record["check_unit"]["exit_code"], int) and record["check_unit"]["exit_code"] != 0


def test_a_gate_timeout_is_a_refusal_not_a_pass(
    package: Path, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that never answers must never be treated as a gate that passed."""

    def _timeout(command, **kwargs):
        raise pu.subprocess.TimeoutExpired(cmd=command, timeout=1)

    monkeypatch.setattr(pu.subprocess, "run", _timeout)
    assert run(package, migrations) == pu.EXIT_REFUSED_BY_GATE
    assert not migrations.exists()


# --------------------------------------------------------------------------------------
# Criterion 4 - byPath is verified against the FILESYSTEM, which no validator does
# --------------------------------------------------------------------------------------


def test_verify_bypath_rejects_a_reference_whose_target_exists_nowhere(tmp_path: Path) -> None:
    """`powerbi-report-author validate` returns errorCount 0 for exactly this file - measured
    against 0.1.4 on `examples/shipping-kpis`: result `succeeded`, exit 0, with the byPath pointing
    at a `.SemanticModel` that exists nowhere. It checks shape, not target."""
    report = tmp_path / "Wb.Report"
    _write_report(report, "NoSuchModel.SemanticModel")
    with pytest.raises(pu.PromotionFailed, match="does not resolve"):
        pu.verify_bypath(report)


def test_verify_bypath_rejects_a_target_folder_with_no_definition_inside(tmp_path: Path) -> None:
    """An empty folder of the right NAME is not a model; the check requires a `definition/`."""
    report = tmp_path / "Wb.Report"
    _write_report(report, "Model.SemanticModel")
    (tmp_path / "Model.SemanticModel").mkdir()
    with pytest.raises(pu.PromotionFailed, match="does not resolve"):
        pu.verify_bypath(report)


@pytest.mark.usefixtures("pass_gate")
def test_a_dangling_bypath_fails_the_promotion_rather_than_shipping_it(
    package: Path, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: if the rewrite produces a reference that does not resolve, the run FAILS.

    The rewrite is replaced with a wrong-but-plausible one - an off-by-one in the number of `../`
    levels, which is the realistic defect and the one a schema validator cannot see.
    """
    monkeypatch.setattr(pu, "rewrite_bypath", lambda report, bypath: _force_bad_path(report))
    assert run(package, migrations, "--datasource-slug", "shared-ds") == pu.EXIT_PROMOTION_FAILED


def _force_bad_path(report: Path) -> dict:
    """Write a byPath that is one level short - the realistic off-by-one."""
    pbir = report / "definition.pbir"
    payload = json.loads(pbir.read_text(encoding="utf-8"))
    payload["datasetReference"]["byPath"]["path"] = "../../../datasources/shared-ds/fabric/Model.SemanticModel"
    pbir.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"previous": None, "written": payload["datasetReference"]["byPath"]["path"], "changed": True}


# --------------------------------------------------------------------------------------
# Criterion 5 - content, not existence
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_a_report_with_pages_but_no_visuals_is_refused(package: Path, migrations: Path) -> None:
    """The measured precedent: every folder that was supposed to exist did exist, and there was
    nothing behind them. A folder count is not a content check."""
    for visual in (package / "fabric" / "Wb.Report").rglob("visuals"):
        shutil.rmtree(visual)
    assert run(package, migrations) == pu.EXIT_REFUSED_CONTENT
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
def test_a_report_with_no_pages_at_all_is_refused(package: Path, migrations: Path, tmp_path: Path) -> None:
    """Desktop-local settings and an empty `pages/` is the exact shape that passed a sign-off.

    The finding must name the MISSING PAGES, not the (also absent) visuals: refusing with the wrong
    reason sends the next person to look at the wrong thing.
    """
    pages = package / "fabric" / "Wb.Report" / "definition" / "pages"
    for page in pages.iterdir():
        if page.is_dir():
            shutil.rmtree(page)
    envelope_path = tmp_path / "nopages.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_REFUSED_CONTENT
    findings = json.loads(envelope_path.read_text(encoding="utf-8"))["findings"]
    assert any("enumerates no page" in finding for finding in findings), findings
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
def test_a_model_whose_tmdl_declares_no_table_is_refused(package: Path, migrations: Path) -> None:
    """Present `.tmdl` FILES are not present TABLES - the files are emptied of declarations here."""
    for tmdl in (package / "fabric" / "Model.SemanticModel" / "definition" / "tables").glob("*.tmdl"):
        tmdl.write_text("/// a comment and nothing else\n", encoding="utf-8")
    assert run(package, migrations) == pu.EXIT_REFUSED_CONTENT
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
def test_content_is_re_checked_at_the_DESTINATION_not_only_at_the_source(
    package: Path, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifying a fix in the working copy proves nothing about `migrations/**/fabric/`.

    A copy that silently drops the visuals - the measured failure - must be caught after the write,
    even though the SOURCE passed its content check.
    """
    real_execute = pu.execute_plan

    def _lossy(plan: pu.PromotionPlan) -> None:
        real_execute(plan)
        for visuals in plan.report_destination.rglob("visuals"):
            shutil.rmtree(visuals)

    monkeypatch.setattr(pu, "execute_plan", _lossy)
    assert run(package, migrations) == pu.EXIT_PROMOTION_FAILED


# --------------------------------------------------------------------------------------
# Criteria 6-8 - the record, the dry run, and re-runnability
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_the_promotion_record_answers_what_promoted_me_from_what_and_was_it_checked(
    package: Path, migrations: Path
) -> None:
    """Criterion 6."""
    assert run(package, migrations) == pu.EXIT_OK
    record = json.loads((migrations / "workbooks" / "wb" / "promotion-record.json").read_text(encoding="utf-8"))
    assert record["source_package"].endswith("Wb")
    assert record["engine_version"] == "2.353.0", "the engine version comes from the package's own receipt"
    assert record["check_unit"]["exit_code"] == 0
    assert record["forced"] is False
    assert record["promoted_at"].endswith("+00:00")
    assert {c["what"] for c in record["copied"]} == {"report", "model", "loose"}
    assert record["bypath_verified"]["resolves"] is True
    assert record["shipped_content"]["Wb.Report"]["visuals"] == 4


@pytest.mark.usefixtures("pass_gate")
def test_both_halves_of_a_split_promotion_get_their_own_record(package: Path, migrations: Path) -> None:
    """Either half can be found alone months later, so each must answer the question by itself."""
    assert run(package, migrations, "--datasource-slug", "shared-ds") == pu.EXIT_OK
    assert (migrations / "workbooks" / "wb" / "promotion-record.json").is_file()
    assert (migrations / "datasources" / "shared-ds" / "promotion-record.json").is_file()


@pytest.mark.usefixtures("pass_gate")
def test_dry_run_changes_nothing_and_reports_the_file_count(
    package: Path, migrations: Path, capsys: pytest.CaptureFixture
) -> None:
    """Criterion 7."""
    envelope_path = migrations.parent / "dry.json"
    assert run(package, migrations, "--dry-run", "--json", str(envelope_path)) == pu.EXIT_OK
    assert not migrations.exists(), "--dry-run must not create the deliverable"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert envelope["status"] == "DRY_RUN"
    assert envelope["planned_files"] == 12, "2 pages x 2 visuals + 2 page.json + pages.json + pbir, 3 model, 1 pbip"
    assert "12 files" in capsys.readouterr().out


@pytest.mark.usefixtures("pass_gate")
def test_promotion_is_idempotent(package: Path, migrations: Path) -> None:
    """Criterion 8: a second run produces the same tree, and does not nest or duplicate anything."""
    assert run(package, migrations) == pu.EXIT_OK
    first = sorted(p.relative_to(migrations).as_posix() for p in migrations.rglob("*") if p.is_file())
    assert run(package, migrations) == pu.EXIT_OK
    second = sorted(p.relative_to(migrations).as_posix() for p in migrations.rglob("*") if p.is_file())
    assert first == second


@pytest.mark.usefixtures("pass_gate")
def test_re_promoting_as_a_shared_datasource_clears_the_model_left_by_the_first_shape(
    package: Path, migrations: Path
) -> None:
    """Re-runnable ACROSS shapes: the workbook copy of the model must not survive as an orphan that
    the report no longer points at."""
    assert run(package, migrations) == pu.EXIT_OK
    assert (migrations / "workbooks" / "wb" / "fabric" / "Model.SemanticModel").is_dir()
    assert run(package, migrations, "--datasource-slug", "shared-ds") == pu.EXIT_OK
    assert not (migrations / "workbooks" / "wb" / "fabric" / "Model.SemanticModel").exists()
    assert (migrations / "datasources" / "shared-ds" / "fabric" / "Model.SemanticModel").is_dir()


# --------------------------------------------------------------------------------------
# Criterion 9 - "cannot assess" is its own blocking state, never 0
# --------------------------------------------------------------------------------------


def _remove_both_artifacts(package: Path) -> None:
    """Leave a `fabric/` that holds neither a report nor a model."""
    shutil.rmtree(package / "fabric" / "Wb.Report")
    shutil.rmtree(package / "fabric" / "Model.SemanticModel")


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda pkg: shutil.rmtree(pkg / "fabric"), "no fabric/ working copy"),
        (_remove_both_artifacts, "neither artifact"),
        (lambda pkg: shutil.copytree(pkg / "fabric" / "Wb.Report", pkg / "fabric" / "Other.Report"), "two reports"),
        (
            lambda pkg: shutil.copytree(pkg / "fabric" / "Model.SemanticModel", pkg / "fabric" / "Other.SemanticModel"),
            "two models",
        ),
        (lambda pkg: (pkg / "fabric" / "surprise").mkdir(), "an unrecognised directory"),
    ],
)
def test_an_unassessable_package_blocks_and_never_exits_zero(package: Path, migrations: Path, mutate, why: str) -> None:
    """Criterion 9's blocking state. Unassessable input collapsing into the clean bucket is this
    repo's most common gate defect; here it is an explicit code of its own."""
    mutate(package)
    assert run(package, migrations) == pu.EXIT_CANNOT_ASSESS, why
    assert not migrations.exists()


def test_a_missing_package_directory_cannot_assess(tmp_path: Path, migrations: Path) -> None:
    """A path that does not exist is an unassessable input, not an empty success."""
    assert run(tmp_path / "nope", migrations) == pu.EXIT_CANNOT_ASSESS


@pytest.mark.usefixtures("pass_gate")
def test_datasource_slug_on_a_package_with_no_model_cannot_assess(tmp_path: Path, migrations: Path) -> None:
    """Asking for a shared-model split when there is no model to share is unassessable, not a pass."""
    package = tmp_path / "packages" / "Wb"
    _write_report(package / "fabric" / "Wb.Report", "Model.SemanticModel")
    assert run(package, migrations, "--datasource-slug", "shared-ds") == pu.EXIT_CANNOT_ASSESS


def test_a_usage_error_exits_64_not_2(migrations: Path) -> None:
    """64, because 2 already means CANNOT_ASSESS here - argparse's default would make "you typed
    the flag wrong" indistinguishable from "this package could not be assessed"."""
    with pytest.raises(SystemExit) as excinfo:
        pu.main(["--slug", "wb", "--migrations-root", str(migrations)])
    assert excinfo.value.code == pu.EXIT_USAGE


def test_an_empty_slug_is_a_usage_error(package: Path, migrations: Path) -> None:
    """An empty slug would promote into `migrations/workbooks//fabric`."""
    with pytest.raises(SystemExit) as excinfo:
        pu.main(["--package", str(package), "--slug", "  ", "--migrations-root", str(migrations)])
    assert excinfo.value.code == pu.EXIT_USAGE


# --------------------------------------------------------------------------------------
# The drift REPORT - divergence only, never provenance, never fatal
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_drift_reports_divergence_from_the_originating_bundle_without_failing(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """#460's silent-loss case: someone edited the OTHER tree. It is reported, not fatal, and the
    report never claims which side is authoritative."""
    bundle_unit = tmp_path / "bundle" / "pbip" / "Wb"
    shutil.copytree(package / "fabric", bundle_unit)
    shutil.rmtree(bundle_unit / "Wb.Report" / "definition" / "pages" / "page-1")

    envelope_path = tmp_path / "drift.json"
    exit_code = run(package, migrations, "--bundle", str(tmp_path / "bundle"), "--json", str(envelope_path))
    assert exit_code == pu.EXIT_OK, "drift must never fail a promotion"
    drift = json.loads(envelope_path.read_text(encoding="utf-8"))["drift"]
    assert drift["status"] == "diverged"
    assert any("page-1" in name for name in drift["only_in_package"])
    assert "does NOT establish which side is authoritative" in drift["reason"]


@pytest.mark.usefixtures("pass_gate")
def test_drift_is_not_checked_rather_than_clean_when_no_bundle_is_given(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """ "I did not look" must never be recorded as "there is no drift"."""
    envelope_path = tmp_path / "nodrift.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_OK
    assert json.loads(envelope_path.read_text(encoding="utf-8"))["drift"]["status"] == "not_checked"


@pytest.mark.usefixtures("pass_gate")
def test_drift_keys_the_bundle_lookup_on_the_manifest_unit_not_the_folder_name(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """A package can legitimately be copied to another folder name (the reference estate has an
    `e2e-<Unit>` working copy); keying on the folder name silently reports `not_checked`."""
    renamed = package.parent / "e2e-Wb"
    shutil.copytree(package, renamed)
    shutil.copytree(package / "fabric", tmp_path / "bundle" / "pbip" / "Wb")

    envelope_path = tmp_path / "renamed.json"
    assert run(renamed, migrations, "--bundle", str(tmp_path / "bundle"), "--json", str(envelope_path)) == pu.EXIT_OK
    assert json.loads(envelope_path.read_text(encoding="utf-8"))["drift"]["status"] == "identical"
