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
    with pytest.raises(pu.PromotionFailed, match="does not resolve to a directory"):
        pu.verify_bypath(report, tmp_path)


def test_verify_bypath_rejects_a_target_folder_with_no_definition_inside(tmp_path: Path) -> None:
    """An empty folder of the right NAME is not a model; the content check is what says so."""
    report = tmp_path / "Wb.Report"
    _write_report(report, "Model.SemanticModel")
    (tmp_path / "Model.SemanticModel").mkdir()
    with pytest.raises(pu.PromotionFailed, match="not a working semantic model"):
        pu.verify_bypath(report, tmp_path)


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

    def _lossy(plan: pu.PromotionPlan) -> pu.AppliedCopies:
        applied = real_execute(plan)
        for visuals in plan.report_destination.rglob("visuals"):
            shutil.rmtree(visuals)
        return applied

    monkeypatch.setattr(pu, "execute_plan", _lossy)
    assert run(package, migrations) == pu.EXIT_PROMOTION_FAILED


def _write_partition_expression(model: Path, source_expression: str) -> None:
    """Give the model a partition whose M `source =` is `source_expression`, verbatim."""
    table = model / "definition" / "tables" / "Data.tmdl"
    table.write_text(
        "table Data\n"
        "\tlineageTag: data\n\n"
        "\tcolumn A\n\t\tdataType: string\n\n"
        "\tpartition 'Data' = m\n"
        "\t\tmode: import\n"
        f"\t\tsource = {source_expression}\n",
        encoding="utf-8",
    )


def _write_partition(model: Path, source_path: str) -> None:
    """Give the model a real import partition reading from `source_path` - the #461 shape."""
    table = model / "definition" / "tables" / "Data.tmdl"
    table.write_text(
        "table Data\n"
        "\tlineageTag: data\n\n"
        "\tcolumn A\n\t\tdataType: string\n\n"
        "\tpartition 'Data' = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        f'\t\t\t\tSource = Csv.Document(File.Contents("{source_path}"), [Delimiter=","])\n'
        "\t\t\tin\n"
        "\t\t\t\tSource\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------------------
# Issue #461 - a model that reads data from OUTSIDE the tree it is promoted into
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_an_absolute_data_path_outside_the_deliverable_refuses_and_ships_nothing(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """The measured #461 defect: 22 absolute machine-local `File.Contents` references across 17 of
    62 packaged units, all pointing into the bundle's gitignored, prunable `data/`.

    It gets its own exit code and its own named reason - an operator has to know that THIS is why,
    because the remedy (carry the extract into the package) is nothing like the remedy for a page
    -parity finding.
    """
    outside = tmp_path / "elsewhere" / "bundle" / "data" / "Extract_Extract.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text("A\n1\n", encoding="utf-8")
    _write_partition(package / "fabric" / "Model.SemanticModel", str(outside))

    envelope_path = tmp_path / "ext.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_REFUSED_EXTERNAL_PATH
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert envelope["status"] == "REFUSED_EXTERNAL_DATA_PATH"
    assert any("EXTERNAL_DATA_PATH" in finding for finding in envelope["findings"])
    assert not migrations.exists(), "a refused promotion must ship nothing"


@pytest.mark.usefixtures("pass_gate")
def test_the_recorded_external_path_is_redacted_because_it_embeds_a_username(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """`migrations/**` is not blanket-gitignored and this repo is PUBLIC. The envelope and the
    record carry the leaf only; the operator gets the full path on stderr, where it is a terminal
    line rather than an artifact `scripts/set_data_folder.py --check` would have to catch later."""
    outside = tmp_path / "elsewhere" / "Extract_Extract.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text("A\n", encoding="utf-8")
    _write_partition(package / "fabric" / "Model.SemanticModel", str(outside))

    envelope_path = tmp_path / "ext.json"
    run(package, migrations, "--json", str(envelope_path))
    text = envelope_path.read_text(encoding="utf-8")
    assert "Extract_Extract.csv" in text, "the leaf is kept so the finding is actionable"
    assert str(tmp_path / "elsewhere") not in text, "the directory part must never reach an artifact"
    assert "<absolute-path-redacted>" in text


@pytest.mark.usefixtures("pass_gate")
def test_force_overrides_the_external_path_refusal_and_records_it(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """Like the `check_unit` override: allowed, but never invisible afterwards."""
    outside = tmp_path / "elsewhere" / "Extract_Extract.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text("A\n", encoding="utf-8")
    _write_partition(package / "fabric" / "Model.SemanticModel", str(outside))

    assert run(package, migrations, "--force") == pu.EXIT_OK
    record = json.loads((migrations / "workbooks" / "wb" / "promotion-record.json").read_text(encoding="utf-8"))
    assert record["external_data_paths"]["forced"] is True
    assert record["external_data_paths"]["shipped"], (
        "the shipped model's findings must be recorded, not just the source's"
    )


@pytest.mark.usefixtures("pass_gate")
def test_a_relative_data_path_promotes_successfully(package: Path, migrations: Path) -> None:
    """POSITIVE CONTROL. A checker that refuses every model passes every negative test above."""
    _write_partition(package / "fabric" / "Model.SemanticModel", "data/HumanResources.csv")
    assert run(package, migrations) == pu.EXIT_OK
    assert (migrations / "workbooks" / "wb" / "fabric" / "Model.SemanticModel").is_dir()


@pytest.mark.usefixtures("pass_gate")
def test_an_absolute_path_inside_the_deliverables_own_data_folder_promotes(package: Path, migrations: Path) -> None:
    """POSITIVE CONTROL, and the one that stops this from breaking an existing convention.

    `scripts/set_data_folder.py --sanitize` rewrites every model's `DataFolder` to an ABSOLUTE
    `<REPO_ROOT>\\<tree>\\<slug>\\data\\`. The test is *absolute AND outside*, never *absolute*, so
    that path must keep promoting.
    """
    own_data = migrations / "workbooks" / "wb" / "data"
    own_data.mkdir(parents=True)
    (own_data / "HumanResources.csv").write_text("A\n", encoding="utf-8")
    _write_partition(package / "fabric" / "Model.SemanticModel", str(own_data / "HumanResources.csv"))
    assert run(package, migrations) == pu.EXIT_OK


@pytest.mark.usefixtures("pass_gate")
def test_a_url_is_not_treated_as_a_local_path(package: Path, migrations: Path) -> None:
    """POSITIVE CONTROL: `https://…` is a connection, not a file this promotion could carry.

    There is no explicit URL guard in the production code - a URL is absolute in neither path
    flavour, so one killed no mutation and was deleted as dead code. This test is what keeps that
    true: it fails the moment the detector becomes broad enough to need one.
    """
    _write_partition(package / "fabric" / "Model.SemanticModel", "https://example.invalid/share/data.csv")
    assert run(package, migrations) == pu.EXIT_OK


@pytest.mark.usefixtures("pass_gate")
def test_a_unc_share_is_treated_as_an_external_path(package: Path, migrations: Path, tmp_path: Path) -> None:
    """Not keyed on a drive letter: a UNC share is the same defect on a different machine."""
    _write_partition(package / "fabric" / "Model.SemanticModel", r"\\fileserver\share\Extract.csv")
    assert run(package, migrations, "--json", str(tmp_path / "unc.json")) == pu.EXIT_REFUSED_EXTERNAL_PATH


@pytest.mark.usefixtures("pass_gate")
def test_a_posix_absolute_data_file_is_treated_as_an_external_path(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """Measured on the reference estate: one model carries `/Users/<someone>/…/Global Superstore.xlsx`.
    A Windows-only rule would have shipped it."""
    _write_partition(package / "fabric" / "Model.SemanticModel", "/Users/<someone>/Datasets/Global Superstore.xlsx")
    assert run(package, migrations, "--json", str(tmp_path / "posix.json")) == pu.EXIT_REFUSED_EXTERNAL_PATH


@pytest.mark.usefixtures("pass_gate")
def test_a_databricks_http_path_is_not_treated_as_a_local_path(package: Path, migrations: Path) -> None:
    """POSITIVE CONTROL, and a measured false positive this rule had to lose.

    `expression HttpPath = "/sql/1.0/warehouses/<id>"` is a LIVE connection parameter and appears in
    three units of the reference estate. Refusing it would block every live-Databricks promotion.
    """
    _write_partition(package / "fabric" / "Model.SemanticModel", "/sql/1.0/warehouses/764e5801f0e0fac8")
    assert run(package, migrations) == pu.EXIT_OK


@pytest.mark.usefixtures("pass_gate")
def test_a_bare_slash_in_a_formula_is_not_treated_as_a_local_path(package: Path, migrations: Path) -> None:
    """POSITIVE CONTROL: a `"/"` literal inside a `TableauFormula` annotation is not a data path -
    also measured on the reference estate, where it accounted for 3 of 9 POSIX-only hits."""
    model = package / "fabric" / "Model.SemanticModel"
    (model / "definition" / "tables" / "T0.tmdl").write_text(
        'table T0\n\tmeasure Ratio = DIVIDE(1, 2)\n\t\tannotation TableauFormula = [a] "/" [b]\n',
        encoding="utf-8",
    )
    assert run(package, migrations) == pu.EXIT_OK


@pytest.mark.usefixtures("pass_gate")
def test_the_shipped_model_is_re_scanned_after_the_copy(
    package: Path, migrations: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifying the working copy proves nothing about `migrations/**/fabric/`.

    The source passes; the copy is then made lossy in exactly the way that matters - an absolute
    path appears only in the SHIPPED model - and the promotion must still fail.
    """
    outside = tmp_path / "elsewhere" / "Extract.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text("A\n", encoding="utf-8")
    real_execute = pu.execute_plan

    def _inject(plan: pu.PromotionPlan) -> pu.AppliedCopies:
        applied = real_execute(plan)
        _write_partition(plan.model_destination, str(outside))
        return applied

    monkeypatch.setattr(pu, "execute_plan", _inject)
    assert run(package, migrations) == pu.EXIT_PROMOTION_FAILED


def external_findings(model: Path, root: Path) -> list:
    """`external_data_paths` with one allowed root - a thin readability wrapper for the tests."""
    return pu.external_data_paths(model, (root,))


def test_an_absolute_path_that_does_not_exist_is_still_reported(tmp_path: Path) -> None:
    """Fail closed. The check is about WHERE the model points, not about what is on this disk
    today: a bundle's `data/` that has already been pruned is the worst case, not an exempt one."""
    model = tmp_path / "M.SemanticModel"
    _write_model(model)
    _write_partition(model, r"C:\does-not-exist\nowhere\Extract.csv")
    assert external_findings(model, tmp_path), "an absolute path outside the tree must be reported"


# --------------------------------------------------------------------------------------
# BLOCKING 1 - a slug is a single path component, and must never escape the migrations root
# --------------------------------------------------------------------------------------


TRAVERSAL_SLUGS = [
    ("..", "the bare parent"),
    (r"..\..\escaped", "a Windows traversal"),
    ("../../escaped", "a POSIX traversal"),
    (r"C:\Windows\Temp\escaped", "an absolute Windows path"),
    (r"\\fileserver\share\escaped", "a UNC path"),
    ("/etc/escaped", "an absolute POSIX path"),
    ("sub/dir", "a nested component"),
    ("NUL", "a reserved device name"),
    ("nul.fabric", "a reserved device name with an extension"),
    ("bad:name", "a drive-separator character"),
    (" leading", "leading whitespace"),
]


@pytest.mark.parametrize(("slug", "why"), TRAVERSAL_SLUGS)
def test_an_unsafe_slug_is_a_usage_error_and_writes_nothing(
    package: Path, migrations: Path, slug: str, why: str
) -> None:
    """The reviewer's highest-severity finding: `--slug ..\\..\\escaped` exited **0**, promoted
    OUTSIDE the migrations root, and reported success - and because `execute_plan` replaces its
    destination, a crafted slug could DELETE a directory outside the root.

    Malformed input exits 64. It never reaches the filesystem at all.
    """
    with pytest.raises(SystemExit) as excinfo:
        pu.main(["--package", str(package), "--slug", slug, "--migrations-root", str(migrations)])
    assert excinfo.value.code == pu.EXIT_USAGE, why
    assert not migrations.exists()


@pytest.mark.parametrize(("slug", "why"), TRAVERSAL_SLUGS)
def test_an_unsafe_datasource_slug_is_a_usage_error(package: Path, migrations: Path, slug: str, why: str) -> None:
    """`--datasource-slug` lands a directory too, so it is validated identically. Validating only
    `--slug` would have left the same traversal open one flag over."""
    with pytest.raises(SystemExit) as excinfo:
        pu.main(
            [
                "--package",
                str(package),
                "--slug",
                "wb",
                "--datasource-slug",
                slug,
                "--migrations-root",
                str(migrations),
            ]
        )
    assert excinfo.value.code == pu.EXIT_USAGE, why


@pytest.mark.usefixtures("pass_gate")
def test_nothing_outside_the_migrations_root_is_ever_deleted(package: Path, migrations: Path, tmp_path: Path) -> None:
    """The consequence, stated as a property rather than as an exit code.

    A directory that a traversal slug would have pointed at must still be there, with its contents,
    after the run - `execute_plan` replaces its destination, so this is a DATA-LOSS guard.
    """
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "keep.txt").write_text("do not delete me", encoding="utf-8")
    with pytest.raises(SystemExit):
        pu.main(
            [
                "--package",
                str(package),
                "--slug",
                str(victim),
                "--migrations-root",
                str(migrations / "workbooks"),
            ]
        )
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "do not delete me"


def test_a_plan_that_would_escape_the_root_cannot_assess(package: Path, migrations: Path) -> None:
    """Defence in depth behind the argument check: `build_plan` itself refuses an escaping
    destination, so a future caller that skips `parse_args` still cannot promote outside the root.
    """
    shape = pu.discover_shape(package)
    with pytest.raises(pu.CannotAssess, match="outside the migrations root"):
        pu.build_plan(shape, migrations, "../../escaped", None)


def test_containment_is_anchored_at_the_ROOT_not_at_the_workbooks_tier(package: Path, migrations: Path) -> None:
    """A deliberately recorded boundary, found by this suite rather than assumed.

    `../escaped` normalises to `<root>/escaped` - the wrong TIER but still inside the root, so the
    containment check does not fire for it. That is correct: containment guards against escaping
    the root, and `slug_problem` is what refuses a slug carrying a separator at all. Two layers,
    two different questions; conflating them would make one of them untestable.
    """
    shape = pu.discover_shape(package)
    plan = pu.build_plan(shape, migrations, "../escaped", None)
    assert plan.report_destination.is_relative_to(migrations)
    assert pu.slug_problem("../escaped") is not None, "the argument layer is what rejects this one"


def test_execute_plan_refuses_an_escaping_destination_even_if_a_plan_reaches_it(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """The last guard before the filesystem. A plan built by hand - bypassing both checks above -
    is still refused, and the victim directory is untouched."""
    victim = tmp_path / "outside"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    shape = pu.discover_shape(package)
    plan = pu.PromotionPlan(
        shape=shape,
        steps=[pu.CopyStep(shape.report, victim, "report")],
        report_destination=victim,
        model_destination=None,
        bypath=None,
        migrations_root=migrations,
    )
    with pytest.raises(pu.CannotAssess, match="outside the migrations root"):
        pu.execute_plan(plan)
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("slug", ["wb", "customer-acme", "hr-dashboard", "a.b.c", "Superstore_2024"])
def test_ordinary_slugs_are_accepted(slug: str) -> None:
    """POSITIVE CONTROL. A validator that rejects everything passes every test above."""
    assert pu.slug_problem(slug) is None


# --------------------------------------------------------------------------------------
# BLOCKING 2 - byPath must resolve to a REAL semantic model, not merely to a directory
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_a_bypath_target_that_is_not_a_semantic_model_fails_the_promotion(tmp_path: Path, migrations: Path) -> None:
    """Reproduced by the reviewer: a report-only package promoted at exit **0** against a hand-made
    folder that merely held an empty `definition/`. "Some directory with a definition/" is not a
    model - it has no `model.tmdl` and no tables, so a report bound to it opens with no data."""
    package = tmp_path / "packages" / "Wb"
    _write_report(package / "fabric" / "Wb.Report", "Fake.SemanticModel")
    fake = migrations / "workbooks" / "wb" / "fabric" / "Fake.SemanticModel"
    (fake / "definition").mkdir(parents=True)
    assert run(package, migrations) == pu.EXIT_PROMOTION_FAILED
    assert not (fake / "definition" / "model.tmdl").exists()


@pytest.mark.usefixtures("pass_gate")
def test_a_bypath_target_that_is_not_a_semanticmodel_folder_fails(tmp_path: Path, migrations: Path) -> None:
    """The suffix carries meaning: Power BI resolves a MODEL here, not any folder."""
    package = tmp_path / "packages" / "Wb"
    _write_report(package / "fabric" / "Wb.Report", "NotAModel")
    target = migrations / "workbooks" / "wb" / "fabric" / "NotAModel"
    _write_model(target)
    assert run(package, migrations) == pu.EXIT_PROMOTION_FAILED


def test_verify_bypath_rejects_a_target_outside_the_migrations_root(tmp_path: Path) -> None:
    """A deliverable that reaches out of the tree does not survive delivery, however real the model
    at the other end is on THIS machine."""
    outside = tmp_path / "outside" / "Model.SemanticModel"
    _write_model(outside)
    report = tmp_path / "root" / "workbooks" / "wb" / "fabric" / "Wb.Report"
    _write_report(report, "../../../../../outside/Model.SemanticModel")
    with pytest.raises(pu.PromotionFailed, match="outside the migrations root"):
        pu.verify_bypath(report, tmp_path / "root")


def test_verify_bypath_rejects_a_model_folder_whose_tables_are_empty(tmp_path: Path) -> None:
    """The SAME content check criterion 5 applies to a shipped model, reused rather than
    re-implemented: a `.SemanticModel` with a `model.tmdl` but no table declarations is not one."""
    report = tmp_path / "Wb.Report"
    _write_report(report, "Model.SemanticModel")
    model = tmp_path / "Model.SemanticModel"
    _write_model(model)
    for tmdl in (model / "definition" / "tables").glob("*.tmdl"):
        tmdl.write_text("/// nothing declared here\n", encoding="utf-8")
    with pytest.raises(pu.PromotionFailed, match="not a working semantic model"):
        pu.verify_bypath(report, tmp_path)


# --------------------------------------------------------------------------------------
# BLOCKING 3 - the content guard PARSES; a file that exists is not a document
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_a_missing_pages_manifest_cannot_assess(package: Path, migrations: Path, tmp_path: Path) -> None:
    """`pages.json` is the manifest Power BI reads for page order; without it the report opens with
    no pages. It used to be never read at all, and the promotion still exited 0."""
    (package / "fabric" / "Wb.Report" / "definition" / "pages" / "pages.json").unlink()
    envelope_path = tmp_path / "nomanifest.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_CANNOT_ASSESS
    findings = json.loads(envelope_path.read_text(encoding="utf-8"))["findings"]
    assert any("pages.json is missing" in finding for finding in findings), findings
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ("", "an empty file"),
        ("{", "truncated JSON"),
        ("[]", "valid JSON that is not an object"),
        ("{}", "an object declaring no visual"),
        ('{"name": "v0", "visual": {}}', "a visual object with no visualType"),
    ],
)
def test_a_visual_that_is_not_a_document_cannot_assess(
    package: Path, migrations: Path, tmp_path: Path, payload: str, why: str
) -> None:
    """The reviewer's reproduction: an empty `visual.json` exited 0 AND the record asserted
    `"visuals": 4`. A record claiming content that was never verified is worse than no record."""
    for visual in (package / "fabric" / "Wb.Report").rglob("visual.json"):
        visual.write_text(payload, encoding="utf-8")
    envelope_path = tmp_path / "badvisual.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_CANNOT_ASSESS, why
    assert not migrations.exists(), "nothing may ship when the content could not be assessed"


@pytest.mark.usefixtures("pass_gate")
def test_a_page_that_is_not_a_document_cannot_assess(package: Path, migrations: Path) -> None:
    """Same rule one level up: an unparseable `page.json` is not a page."""
    for page in (package / "fabric" / "Wb.Report").rglob("page.json"):
        page.write_text("{not json", encoding="utf-8")
    assert run(package, migrations) == pu.EXIT_CANNOT_ASSESS


@pytest.mark.usefixtures("pass_gate")
def test_a_page_that_declares_no_name_cannot_assess(package: Path, migrations: Path) -> None:
    """Parseable is not the same as well-formed: a page that never names itself is not addressable
    by `pages.json`'s `pageOrder`, so the report opens without it."""
    for page in (package / "fabric" / "Wb.Report").rglob("page.json"):
        page.write_text(json.dumps({"displayName": "Nameless", "height": 720}), encoding="utf-8")
    assert run(package, migrations) == pu.EXIT_CANNOT_ASSESS
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
def test_a_pages_manifest_with_an_empty_page_order_cannot_assess(package: Path, migrations: Path) -> None:
    """An enumerated-but-empty manifest is the same defect as a missing one."""
    manifest = package / "fabric" / "Wb.Report" / "definition" / "pages" / "pages.json"
    manifest.write_text(json.dumps({"pageOrder": []}), encoding="utf-8")
    assert run(package, migrations) == pu.EXIT_CANNOT_ASSESS


def test_force_cannot_bypass_the_mandatory_content_checks(package: Path, migrations: Path) -> None:
    """`--force` is documented to override the GATE, not the content checks - confirmed in code.

    Nothing is stubbed here: the real gate is not even reached, because the content check runs
    first and refuses whatever `--force` says.
    """
    for visual in (package / "fabric" / "Wb.Report").rglob("visual.json"):
        visual.write_text("", encoding="utf-8")
    assert run(package, migrations, "--force") == pu.EXIT_CANNOT_ASSESS
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
def test_the_recorded_counts_are_the_parsed_counts(package: Path, migrations: Path) -> None:
    """POSITIVE CONTROL for the parser: a good package still counts exactly what is there, so the
    record's assertion is earned rather than assumed."""
    assert run(package, migrations) == pu.EXIT_OK
    record = json.loads((migrations / "workbooks" / "wb" / "promotion-record.json").read_text(encoding="utf-8"))
    assert record["shipped_content"]["Wb.Report"] == {"pages": 2, "pages_with_visuals": 2, "visuals": 4}


# --------------------------------------------------------------------------------------
# BLOCKING 4 - a path assembled from concatenated literals is still a path
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_a_concatenated_absolute_path_is_detected(package: Path, migrations: Path, tmp_path: Path) -> None:
    """`File.Contents("C:" & "\\secret\\data.csv")` - neither fragment is absolute on its own
    (`"C:"` is a drive with no root, `"\\secret\\..."` a root with no drive), so judged separately
    both pass and the reference shipped at exit 0 without `--force`."""
    drive, rest = str(tmp_path)[:2], str(tmp_path)[2:]
    _write_partition_expression(
        package / "fabric" / "Model.SemanticModel",
        f'File.Contents("{drive}" & "{rest}\\elsewhere\\Extract.csv")',
    )
    assert run(package, migrations, "--json", str(tmp_path / "cat.json")) == pu.EXIT_REFUSED_EXTERNAL_PATH
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
def test_a_three_way_concatenation_is_detected(package: Path, migrations: Path, tmp_path: Path) -> None:
    """The joiner takes the whole run, not just the first pair."""
    drive, rest = str(tmp_path)[:2], str(tmp_path)[2:]
    _write_partition_expression(
        package / "fabric" / "Model.SemanticModel",
        f'File.Contents("{drive}" & "{rest}" & "\\elsewhere\\Extract.csv")',
    )
    assert run(package, migrations) == pu.EXIT_REFUSED_EXTERNAL_PATH


@pytest.mark.usefixtures("pass_gate")
def test_a_concatenation_that_stays_inside_the_deliverable_promotes(package: Path, migrations: Path) -> None:
    """POSITIVE CONTROL: joining must not turn a legitimate in-tree reference into a refusal."""
    own_data = migrations / "workbooks" / "wb" / "data"
    own_data.mkdir(parents=True)
    inside = str(own_data)
    _write_partition_expression(
        package / "fabric" / "Model.SemanticModel",
        f'File.Contents("{inside[:2]}" & "{inside[2:]}\\HumanResources.csv")',
    )
    assert run(package, migrations) == pu.EXIT_OK


@pytest.mark.usefixtures("pass_gate")
def test_unrelated_adjacent_literals_do_not_become_a_false_path(package: Path, migrations: Path) -> None:
    """POSITIVE CONTROL with real power: two literals that WOULD form an absolute path if joined
    indiscriminately, but that are not concatenated at all.

    Joining is keyed on M's `&` operator; a promoter that simply glued every literal in a file
    together would refuse this legitimate model.
    """
    _write_partition_expression(
        package / "fabric" / "Model.SemanticModel",
        r'Table.FromRecords({[Drive = "C:", Leaf = "\Windows\Temp\evil.csv", Data = "data/local.csv"]})',
    )
    assert run(package, migrations) == pu.EXIT_OK


def test_an_absolute_parameter_is_caught_at_its_definition_site(tmp_path: Path) -> None:
    """Why identifier concatenation needs no static evaluation.

    `File.Contents(SourceFolder & "\\x.csv")` is unresolvable on its own - but `SourceFolder` is
    itself `expression SourceFolder = "<absolute>"` in `expressions.tmdl`, which this scan reads
    like any other file. Measured on the reference estate, that is how 9 of the 32 findings
    surfaced.
    """
    model = tmp_path / "M.SemanticModel"
    _write_model(model)
    (model / "definition" / "expressions.tmdl").write_text(
        'expression SourceFolder = "C:\\somewhere\\bundle\\data\\" meta [IsParameterQuery=true]\n',
        encoding="utf-8",
    )
    _write_partition_expression(model, 'File.Contents(SourceFolder & "\\HumanResources.csv")')
    found = external_findings(model, tmp_path)
    assert found, "the parameter's own definition must be reported"
    assert any("expressions.tmdl" in item["file"] for item in found)


# --------------------------------------------------------------------------------------
# Non-blocking follow-ups: rollback on failure, and explicit engine provenance
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_a_failed_copy_leaves_no_half_shipped_deliverable(
    package: Path, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected `copy2` failure used to exit 4 with the report already written and no record -
    an artifact that looks promoted and was never verified."""
    real_copy2 = pu.shutil.copy2

    def _explode(source, destination, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pu.shutil, "copy2", _explode)
    assert run(package, migrations) == pu.EXIT_PROMOTION_FAILED
    assert not (migrations / "workbooks" / "wb" / "fabric" / "Wb.Report").exists()
    monkeypatch.setattr(pu.shutil, "copy2", real_copy2)


@pytest.mark.usefixtures("pass_gate")
def test_a_failed_verification_restores_the_previous_deliverable(
    package: Path, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback RESTORES, it does not merely delete: a re-promotion that fails verification must
    leave the deliverable that was already there, not an empty hole where it used to be."""
    assert run(package, migrations) == pu.EXIT_OK
    marker = migrations / "workbooks" / "wb" / "fabric" / "Wb.Report" / "definition" / "pages" / "pages.json"
    original = marker.read_text(encoding="utf-8")

    def _fail(report, migrations_root):
        raise pu.PromotionFailed("injected verification failure")

    monkeypatch.setattr(pu, "verify_bypath", _fail)
    assert run(package, migrations) == pu.EXIT_PROMOTION_FAILED
    assert marker.is_file(), "the previous deliverable must be RESTORED, not merely removed"
    assert marker.read_text(encoding="utf-8") == original, "and restored intact"


@pytest.mark.usefixtures("pass_gate")
def test_a_package_with_no_receipt_records_why_the_engine_version_is_unknown(package: Path, migrations: Path) -> None:
    """A missing receipt is a provenance gap, not a correctness one, so it does not block - but a
    bare `null` cannot be told apart from a receipt that declared no version. Both are explicit."""
    (package / "engine-output-receipt.json").unlink()
    assert run(package, migrations) == pu.EXIT_OK
    record = json.loads((migrations / "workbooks" / "wb" / "promotion-record.json").read_text(encoding="utf-8"))
    assert record["engine_version"] is None
    assert record["engine_version_source"] == "UNAVAILABLE: no engine-output-receipt.json"


@pytest.mark.usefixtures("pass_gate")
def test_a_receipt_without_a_version_is_distinguishable_from_a_missing_receipt(package: Path, migrations: Path) -> None:
    """The two `null`s mean different things and the record says which."""
    _write_json(package / "engine-output-receipt.json", {"version": 1})
    assert run(package, migrations) == pu.EXIT_OK
    record = json.loads((migrations / "workbooks" / "wb" / "promotion-record.json").read_text(encoding="utf-8"))
    assert record["engine_version_source"] == "UNAVAILABLE: receipt declares no engine.version"


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
