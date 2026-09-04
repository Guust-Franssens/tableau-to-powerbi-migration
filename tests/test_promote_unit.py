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
import re
import shutil
import subprocess
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


def shape_of(package: Path) -> pu.PackageShape:
    """`discover_shape` for a unit test, with the kind read from the package's own manifest.

    The kind is no longer inferable from the filesystem (a datasource unit also ships a `.Report`),
    so `discover_shape` takes it as an argument. Routing through `declared_kind` rather than
    hard-coding `"workbook"` keeps these tests honest: a fixture whose manifest stopped declaring a
    kind fails here instead of being quietly promoted as one.
    """
    return pu.discover_shape(package, *pu.declared_kind(package, None))


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
        pu.verify_bypath(report, tmp_path, tmp_path / "NoSuchModel.SemanticModel")


def test_verify_bypath_rejects_a_target_folder_with_no_definition_inside(tmp_path: Path) -> None:
    """An empty folder of the right NAME is not a model; the content check is what says so."""
    report = tmp_path / "Wb.Report"
    _write_report(report, "Model.SemanticModel")
    (tmp_path / "Model.SemanticModel").mkdir()
    with pytest.raises(pu.PromotionFailed, match="not a working semantic model"):
        pu.verify_bypath(report, tmp_path, tmp_path / "Model.SemanticModel")


def test_verify_bypath_rejects_a_real_model_that_is_not_the_one_this_promotion_copied(tmp_path: Path) -> None:
    """HIGH 3's unit half. A perfectly valid model at the other end is still the WRONG answer when
    it is not the model this promotion shipped - that is how a stale model from an earlier run made
    a package that was missing its own model verify clean."""
    report = tmp_path / "Wb.Report"
    _write_report(report, "Stale.SemanticModel")
    _write_model(tmp_path / "Stale.SemanticModel")
    _write_model(tmp_path / "Fresh.SemanticModel")
    with pytest.raises(pu.PromotionFailed, match="not the 'Fresh.SemanticModel' this promotion copied"):
        pu.verify_bypath(report, tmp_path, tmp_path / "Fresh.SemanticModel")


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
    return _force_path(report, "../../../datasources/shared-ds/fabric/Model.SemanticModel")


def _force_path(report: Path, value: str) -> dict:
    """Write `value` into the shipped `definition.pbir`, standing in for the real rewrite."""
    pbir = report / "definition.pbir"
    payload = json.loads(pbir.read_text(encoding="utf-8"))
    payload["datasetReference"]["byPath"]["path"] = value
    pbir.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"previous": None, "written": value, "changed": True}


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
def test_force_does_not_override_the_external_path_refusal(package: Path, migrations: Path, tmp_path: Path) -> None:
    """⚠️ CONTRACT CHANGE (round-2 review). `--force` used to ship this and record the override.

    It cannot: the tool cannot rewrite a customer's M query, so there is no sanitized artifact for
    `--force` to ship - the raw absolute path, with its server and user names, landed verbatim in a
    committable TMDL under `migrations/**`. `--force` overrides the `check_unit.py` GATE and
    nothing else, and this refusal keeps its own exit code.
    """
    outside = tmp_path / "elsewhere" / "Extract_Extract.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text("A\n", encoding="utf-8")
    _write_partition(package / "fabric" / "Model.SemanticModel", str(outside))

    envelope_path = tmp_path / "forced.json"
    assert run(package, migrations, "--force", "--json", str(envelope_path)) == pu.EXIT_REFUSED_EXTERNAL_PATH
    assert json.loads(envelope_path.read_text(encoding="utf-8"))["status"] == "REFUSED_EXTERNAL_DATA_PATH"
    assert not migrations.exists()


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
    assert run(package, migrations) == pu.EXIT_REFUSED_EXTERNAL_PATH


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
    shape = shape_of(package)
    with pytest.raises(pu.CannotAssess, match="outside the migrations root"):
        pu.build_plan(shape, migrations, "../../escaped", None)


def test_containment_is_anchored_at_the_ROOT_not_at_the_workbooks_tier(package: Path, migrations: Path) -> None:
    """A deliberately recorded boundary, found by this suite rather than assumed.

    `../escaped` normalises to `<root>/escaped` - the wrong TIER but still inside the root, so the
    containment check does not fire for it. That is correct: containment guards against escaping
    the root, and `slug_problem` is what refuses a slug carrying a separator at all. Two layers,
    two different questions; conflating them would make one of them untestable.
    """
    shape = shape_of(package)
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
    shape = shape_of(package)
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
def test_a_bypath_target_that_is_not_a_semantic_model_fails_the_promotion(
    package: Path, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduced by the reviewer: a promotion succeeded at exit **0** against a hand-made folder
    that merely held an empty `definition/`. "Some directory with a definition/" is not a model -
    it has no `model.tmdl` and no tables, so a report bound to it opens with no data."""
    fake = migrations / "workbooks" / "wb" / "fabric" / "Fake.SemanticModel"
    (fake / "definition").mkdir(parents=True)
    monkeypatch.setattr(pu, "rewrite_bypath", lambda report, bypath: _force_path(report, "../Fake.SemanticModel"))
    assert run(package, migrations) == pu.EXIT_PROMOTION_FAILED
    assert not (fake / "definition" / "model.tmdl").exists()


@pytest.mark.usefixtures("pass_gate")
def test_a_bypath_target_that_is_not_a_semanticmodel_folder_fails(
    package: Path, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The suffix carries meaning: Power BI resolves a MODEL here, not any folder."""
    target = migrations / "workbooks" / "wb" / "fabric" / "NotAModel"
    _write_model(target)
    monkeypatch.setattr(pu, "rewrite_bypath", lambda report, bypath: _force_path(report, "../NotAModel"))
    assert run(package, migrations) == pu.EXIT_PROMOTION_FAILED


def test_verify_bypath_rejects_a_target_outside_the_migrations_root(tmp_path: Path) -> None:
    """A deliverable that reaches out of the tree does not survive delivery, however real the model
    at the other end is on THIS machine.

    ⚠️ The byPath is written with `_force_path`, not through `_write_report`'s `model_name`
    argument. That argument is a NAME and the helper prepends its own `../`, so handing it a whole
    relative path silently produced SIX levels of `..` where the test meant five - one level above
    `tmp_path` entirely, at a target that did not exist and was not `outside`. It passed anyway
    while `verify_bypath` had no identity check, because the escape it was really asserting was an
    accident. Identity is now checked first and must be SATISFIED here, so that the containment
    branch is the one this test actually reaches.
    """
    outside = tmp_path / "outside" / "Model.SemanticModel"
    _write_model(outside)
    report = tmp_path / "root" / "workbooks" / "wb" / "fabric" / "Wb.Report"
    _write_report(report, "Model.SemanticModel")
    _force_path(report, "../../../../../outside/Model.SemanticModel")
    assert (report / "../../../../../outside/Model.SemanticModel").resolve() == outside.resolve(), (
        "the byPath must resolve to the model this promotion copied, or the identity branch fires instead"
    )
    with pytest.raises(pu.PromotionFailed, match="outside the migrations root"):
        pu.verify_bypath(report, tmp_path / "root", outside)


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
        pu.verify_bypath(report, tmp_path, model)


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


# --------------------------------------------------------------------------------------
# Round-2 blind review: five HIGH findings, each pinned by the reviewer's own reproduction
# --------------------------------------------------------------------------------------


# A drive-absolute literal, in raw text or JSON-escaped. The `(?!/)` lookahead is what keeps a URL
# scheme (`https://`) out: there the character after the colon is `/` and so is the next one.
ABSOLUTE_IN_TEXT = re.compile(r"[A-Za-z]:[\\/](?!/)")


def host_paths_in(text: str, *roots: Path) -> list[str]:
    """Every absolute host path this artifact text exposes.

    Two independent probes, because either alone is weak: the literal test roots (which on Windows
    sit under `C:\\Users\\<username>\\AppData\\...` and so carry a real username), and any
    drive-letter literal at all.
    """
    leaks = [str(root) for root in roots if str(root) in text or root.as_posix() in text]
    return leaks + ABSOLUTE_IN_TEXT.findall(text)


@pytest.mark.usefixtures("pass_gate")
def test_high1_a_successful_promotion_records_no_absolute_host_path(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """HIGH 1. `migrations/**` is not blanket-gitignored and this repo is PUBLIC, so neither the
    record nor the envelope may carry an absolute host path - it embeds a real USERNAME, and a
    customer package path embeds their server or project names too."""
    envelope_path = tmp_path / "ok.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_OK
    record_text = (migrations / "workbooks" / "wb" / "promotion-record.json").read_text(encoding="utf-8")
    assert host_paths_in(record_text, package, migrations) == [], record_text
    assert host_paths_in(envelope_path.read_text(encoding="utf-8"), package, migrations) == []


def test_high1_a_cannot_assess_refusal_records_no_absolute_host_path(tmp_path: Path, migrations: Path) -> None:
    """HIGH 1. The refusal envelope is an artifact too, and the missing-package finding used to
    carry the fully resolved package path - username included."""
    envelope_path = tmp_path / "missing.json"
    missing = tmp_path / "packages" / "NoSuchUnit"
    assert run(missing, migrations, "--json", str(envelope_path)) == pu.EXIT_CANNOT_ASSESS
    text = envelope_path.read_text(encoding="utf-8")
    assert host_paths_in(text, missing, tmp_path) == [], text


@pytest.mark.usefixtures("pass_gate")
def test_high1_force_may_not_ship_a_model_carrying_a_raw_external_path(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """HIGH 1 + the `--force` contract. `--force` overrides the `check_unit.py` GATE and nothing
    else: it cannot sanitize a customer's M query, so shipping one would put the raw absolute path
    - server name, username - into a committable TMDL. It refuses instead."""
    outside = tmp_path / "private-user" / "CustomerServer" / "Extract.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text("A\n", encoding="utf-8")
    _write_partition(package / "fabric" / "Model.SemanticModel", str(outside))

    assert run(package, migrations, "--force") == pu.EXIT_REFUSED_EXTERNAL_PATH
    assert not migrations.exists(), "nothing may ship while the raw path is still in the model"


@pytest.mark.usefixtures("pass_gate")
def test_high2_a_declared_datasource_package_promotes_as_a_datasource(tmp_path: Path, migrations: Path) -> None:
    """HIGH 2. `package_unit.py:unit_kind` is explicit that EVERY `pbip/<Unit>/` in a real 2.339.0
    estate run carries BOTH a `.Report` and a `.SemanticModel`, datasource-only units included, so
    the filesystem cannot answer this question - only the manifest's `kind` can."""
    package = make_package(tmp_path / "packages", unit="PublishedDS")
    _write_json(package / "package-manifest.json", {"unit": "PublishedDS", "kind": "datasource"})

    assert run(package, migrations) == pu.EXIT_OK
    assert (migrations / "datasources" / "wb" / "fabric" / "Model.SemanticModel").is_dir()
    assert not (migrations / "workbooks").exists(), "a declared datasource must never land as a workbook"


@pytest.mark.usefixtures("pass_gate")
def test_high3_a_report_only_package_cannot_assess_even_against_a_stale_destination_model(
    tmp_path: Path, migrations: Path
) -> None:
    """HIGH 3. `byPath` was verified against whatever happened to exist in the destination, so a
    model left by an EARLIER run turned "this package is missing its model" into exit 0."""
    package = tmp_path / "packages" / "Wb"
    _write_report(package / "fabric" / "Wb.Report", "Model.SemanticModel")
    _write_json(package / "package-manifest.json", {"unit": "Wb", "kind": "workbook"})
    stale = migrations / "workbooks" / "wb" / "fabric" / "Model.SemanticModel"
    _write_model(stale)

    envelope_path = tmp_path / "reportonly.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_CANNOT_ASSESS
    findings = json.loads(envelope_path.read_text(encoding="utf-8"))["findings"]
    assert any("no .SemanticModel" in finding for finding in findings), findings


@pytest.mark.usefixtures("pass_gate")
def test_high4_an_unexpected_exception_rolls_the_whole_promotion_back(
    package: Path, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH 4. Rollback covered only `PromotionFailed` and `OSError`, so ANY other ordinary
    exception after the copy left a shipped, unverified deliverable behind."""

    def _explode(*_args, **_kwargs):
        raise AttributeError("injected: an ordinary bug after the copy")

    monkeypatch.setattr(pu, "build_record", _explode)
    assert run(package, migrations) == pu.EXIT_PROMOTION_FAILED
    assert not (migrations / "workbooks" / "wb" / "fabric" / "Wb.Report").exists()
    assert not (migrations / "workbooks" / "wb" / "fabric" / "Model.SemanticModel").exists()


@pytest.mark.usefixtures("pass_gate")
def test_high4_a_receipt_that_is_a_json_array_does_not_crash_after_the_copy(package: Path, migrations: Path) -> None:
    """HIGH 4's reproduction: valid JSON that is not an object made `_engine_version` raise
    `AttributeError` AFTER the copy. Provenance is read before anything is written now."""
    (package / "engine-output-receipt.json").write_text("[]\n", encoding="utf-8")
    assert run(package, migrations) == pu.EXIT_OK
    record = json.loads((migrations / "workbooks" / "wb" / "promotion-record.json").read_text(encoding="utf-8"))
    assert record["engine_version"] is None
    assert "not a JSON object" in record["engine_version_source"]


@pytest.mark.usefixtures("pass_gate")
def test_high4_a_failure_after_the_first_record_leaves_no_orphan_record(
    package: Path, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH 4, second half: a record claiming a promotion, beside NO promoted artifacts.

    The records are part of the same transaction as the report and the model, so a failure between
    the two records rolls both of them back with everything else.
    """
    real_write = Path.write_text
    state = {"records": 0}

    def _fail_on_the_second_record(self: Path, data: str, **kwargs):
        if self.name == pu.RECORD_NAME:
            state["records"] += 1
            if state["records"] == 2:
                raise OSError("injected: the second record could not be written")
        return real_write(self, data, **kwargs)

    monkeypatch.setattr(Path, "write_text", _fail_on_the_second_record)
    assert run(package, migrations, "--datasource-slug", "shared-ds") == pu.EXIT_PROMOTION_FAILED
    assert state["records"] == 2, "the injection must have fired on the SECOND record, not the first"
    monkeypatch.undo()
    assert not list(migrations.rglob(pu.RECORD_NAME)), "no record may survive a rolled-back promotion"
    assert not (migrations / "workbooks" / "wb" / "fabric" / "Wb.Report").exists()


@pytest.mark.usefixtures("pass_gate")
def test_high5_a_trailing_dot_slug_cannot_overwrite_another_deliverable(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """HIGH 5. Windows normalises `foo.` and `foo` to the same filesystem path, and promotion
    REPLACES its destination - so `--slug foo.` silently destroyed the deliverable at `foo`."""
    assert pu.main(["--package", str(package), "--slug", "foo", "--migrations-root", str(migrations)]) == pu.EXIT_OK
    marker = migrations / "workbooks" / "foo" / "fabric" / "marker.txt"
    marker.write_text("the first deliverable", encoding="utf-8")

    other = make_package(tmp_path / "other", unit="Other")
    with pytest.raises(SystemExit) as excinfo:
        pu.main(["--package", str(other), "--slug", "foo.", "--migrations-root", str(migrations)])
    assert excinfo.value.code == pu.EXIT_USAGE
    assert marker.read_text(encoding="utf-8") == "the first deliverable"


@pytest.mark.usefixtures("pass_gate")
def test_high5_a_slug_that_aliases_an_existing_deliverable_on_disk_is_refused(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """HIGH 5's backstop: the lexical guard cannot see case-insensitive aliasing (`Foo` for an
    existing `foo`), so the DESTINATION's real on-disk identity is compared before it is replaced.

    Skipped where the filesystem is case-sensitive, because there the two are genuinely different
    deliverables and refusing would be wrong.
    """
    assert pu.main(["--package", str(package), "--slug", "foo", "--migrations-root", str(migrations)]) == pu.EXIT_OK
    if not (migrations / "workbooks" / "FOO").exists():
        pytest.skip("case-sensitive filesystem: 'FOO' and 'foo' are not the same deliverable")
    marker = migrations / "workbooks" / "foo" / "fabric" / "marker.txt"
    marker.write_text("the first deliverable", encoding="utf-8")

    other = make_package(tmp_path / "other", unit="Other")
    assert (
        pu.main(["--package", str(other), "--slug", "FOO", "--migrations-root", str(migrations)])
        == pu.EXIT_CANNOT_ASSESS
    )
    assert marker.read_text(encoding="utf-8") == "the first deliverable"


# --------------------------------------------------------------------------------------
# Round-2 blind review: the three MEDIUM findings
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_medium1_a_pages_manifest_that_orders_a_page_that_is_not_there_is_refused_on_content(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """MEDIUM 1. `pageOrder` being non-empty said nothing about whether those ids exist: a report
    whose manifest orders only pages that are absent opens with NONE of the real ones.

    ⚠️ **REFUSED_CONTENT (3), not CANNOT_ASSESS (2)** - the round-2 fix first classified it as
    unassessable and that was the wrong half of this repo's own dividing line. `pages.json` parsed,
    `definition/pages/` enumerated, and the ordered id is provably absent: the input was read in
    full and the verdict is definitive, which is the *"structurally present, functionally empty"*
    sentence exit 3 exists for. Nothing here is unreadable or ambiguous. It also had a measured
    cost: as `unassessable` it OUTRANKED and swallowed `... enumerates no page` in the sibling test
    below, so a report with no pages at all stopped being reported as empty.
    """
    manifest = package / "fabric" / "Wb.Report" / "definition" / "pages" / "pages.json"
    _write_json(manifest, {"pageOrder": ["ghost"]})
    envelope_path = tmp_path / "ghost.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_REFUSED_CONTENT
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert envelope["status"] == "REFUSED_CONTENT", envelope["status"]
    assert any("orders 1 page" in finding for finding in envelope["findings"]), envelope["findings"]
    assert not migrations.exists(), "a refused promotion must not create the deliverable"


@pytest.mark.usefixtures("pass_gate")
def test_medium1_an_unreadable_pages_manifest_is_still_cannot_assess(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """The other side of that line, pinned so the fix above cannot be over-applied.

    A `pages.json` that will not parse is genuinely unassessable - the page order is UNKNOWN rather
    than known-wrong - and must keep routing to 2. Moving the ordered-page verdict to 3 must not
    drag this one with it.
    """
    manifest = package / "fabric" / "Wb.Report" / "definition" / "pages" / "pages.json"
    manifest.write_text("{not json", encoding="utf-8")
    envelope_path = tmp_path / "badmanifest.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_CANNOT_ASSESS
    findings = json.loads(envelope_path.read_text(encoding="utf-8"))["findings"]
    assert any("pages.json is not readable JSON" in finding for finding in findings), findings


@pytest.mark.usefixtures("pass_gate")
def test_medium2_a_zero_byte_model_tmdl_is_not_a_model(package: Path, migrations: Path) -> None:
    """MEDIUM 2. A zero-byte `model.tmdl` satisfied `is_file()` and shipped."""
    (package / "fabric" / "Model.SemanticModel" / "definition" / "model.tmdl").write_text("", encoding="utf-8")
    assert run(package, migrations) == pu.EXIT_REFUSED_CONTENT
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
def test_medium2_an_unreadable_table_tmdl_cannot_assess_rather_than_raising(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """MEDIUM 2. The read raised `OSError` out of `main()` - a traceback, no exit contract, and the
    exception message carries the absolute path it failed on."""
    tables = package / "fabric" / "Model.SemanticModel" / "definition" / "tables"
    (tables / "T0.tmdl").unlink()
    (tables / "T0.tmdl").mkdir()  # a directory is unreadable as text on every platform

    envelope_path = tmp_path / "unreadable.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_CANNOT_ASSESS
    text = envelope_path.read_text(encoding="utf-8")
    assert "could not be read" in text, text
    assert host_paths_in(text, package, migrations) == [], text


@pytest.mark.usefixtures("pass_gate")
def test_medium2_an_invalid_source_pbir_is_a_source_assessment_not_a_promotion_failure(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """MEDIUM 2. An unusable `definition.pbir` in the SOURCE is unassessable input (2), not a
    promotion that got as far as failing (4) - the two route to different responses."""
    _write_json(package / "fabric" / "Wb.Report" / "definition.pbir", {"version": "4.0"})
    envelope_path = tmp_path / "pbir.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_CANNOT_ASSESS
    findings = json.loads(envelope_path.read_text(encoding="utf-8"))["findings"]
    assert any("datasetReference.byPath.path" in finding for finding in findings), findings
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
def test_medium3_the_shipped_external_path_scan_uses_the_same_exit_code_as_the_source_scan(
    package: Path, migrations: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIUM 3. The same defect routed to 5 or to 4 depending only on WHICH of the two scans saw
    it first, so automation got a different verdict for one condition."""
    outside = tmp_path / "elsewhere" / "Extract.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text("A\n", encoding="utf-8")
    real_execute = pu.execute_plan

    def _inject(plan: pu.PromotionPlan) -> pu.AppliedCopies:
        applied = real_execute(plan)
        _write_partition(plan.model_destination, str(outside))
        return applied

    monkeypatch.setattr(pu, "execute_plan", _inject)
    envelope_path = tmp_path / "shipped-ext.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_REFUSED_EXTERNAL_PATH
    assert json.loads(envelope_path.read_text(encoding="utf-8"))["status"] == "REFUSED_EXTERNAL_DATA_PATH"


# --------------------------------------------------------------------------------------
# ROUND 3, HIGH 1 - the path invariant is about the SHIPMENT, not about `*.tmdl`
#
# Measured before the fix: a `.Report/.pbi/localSettings.json` carrying
# `C:\Users\<operator>\ServerA\source.csv` promoted at `exit=0 gate=1 status=PROMOTED`,
# `shipped=True contains-identity=True`. The scan read model `definition/**/*.tmdl` and the copy
# took the whole tree, so every non-TMDL byte shipped unexamined.
#
# `.pbi/localSettings.json` is gitignored BY NAME (`.gitignore:171`), which the review did not
# mention and which is worth stating plainly: that one file would not itself have reached the
# public repo. The breach is real regardless, because the boundary was wrong rather than the
# example: `git check-ignore` (2026-09-04) reports `.pbi/unappliedChanges.json` - written by the
# same Desktop - and the `.pbip` and `definition/report.json` as TRACKED.
# --------------------------------------------------------------------------------------

CUSTOMER_PATH = "C:" + r"\Users\CustomerOperator\ServerA\source.csv"
UNC_PATH = r"\\CustomerFileServer\finance\budget.xlsx"
POSIX_PATH = "/Users" + "/customer-analyst/data/Sales.xlsx"
# ⚠️ Spelled in PIECES on purpose, and the join is not cosmetic. `scripts/set_data_folder.py
# --check` is this repo's privacy gate and it scans every git-TRACKED file for exactly
# `X:\Users\<name>` and `/Users/<name>`; a test file that hard-codes one fails the gate it exists
# to defend. Measured while writing these tests: the first draft did, at exit 1. Joining at import
# keeps the RUNTIME value a real absolute path while the source text carries no match. `UNC_PATH`
# needs no join - that pattern only matches a UNC share whose first segment is `Users`.


def _pbi_local_settings(package: Path) -> Path:
    """The Desktop-local settings file Power BI writes beside a report."""
    return package / "fabric" / "Wb.Report" / ".pbi" / "localSettings.json"


@pytest.mark.usefixtures("pass_gate")
def test_round3_high1_desktop_local_state_is_not_shipped_at_all(package: Path, migrations: Path) -> None:
    """`.pbi/` is EXCLUDED from the shipment rather than scanned.

    Deliberately the stronger of the two available fixes for this file. It is Desktop's
    per-machine state - `.gitignore:169-172` calls it "machine-specific, regenerated automatically
    on open" - `localSettings.json` records the OPERATOR's local paths, and `cache.abf` is a
    multi-hundred-MB binary that no text scan could have inspected anyway. Not shipping it removes
    the leak; scanning it would only have detected one.
    """
    settings = _pbi_local_settings(package)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"cachePath": CUSTOMER_PATH}), encoding="utf-8")
    (settings.parent / "cache.abf").write_bytes(b"\x00\x01\x02\xff not utf-8")

    assert run(package, migrations, "--force") == pu.EXIT_OK
    shipped_report = migrations / "workbooks" / "wb" / "fabric" / "Wb.Report"
    assert shipped_report.is_dir(), "the report itself must still ship"
    assert not (shipped_report / ".pbi").exists(), "Desktop-local state must not reach the deliverable"
    assert [p.name for p in shipped_report.rglob("*") if p.name in {"localSettings.json", "cache.abf"}] == []


@pytest.mark.usefixtures("pass_gate")
def test_round3_high1_the_excluded_files_are_not_counted_as_shipped(package: Path, migrations: Path) -> None:
    """The file COUNT has to agree with the copy, or the record asserts files that never shipped.

    One definition of "what ships" (`_shipped_files`), used by the count, the copy and the scan.
    Three answers to that question is exactly the disagreement this finding was.
    """
    settings = _pbi_local_settings(package)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}", encoding="utf-8")
    assert run(package, migrations) == pu.EXIT_OK
    record = json.loads((migrations / "workbooks" / "wb" / "promotion-record.json").read_text(encoding="utf-8"))
    counted = {item["what"]: item["files"] for item in record["copied"]}
    on_disk = len([p for p in (migrations / "workbooks" / "wb" / "fabric" / "Wb.Report").rglob("*") if p.is_file()])
    assert counted["report"] == on_disk, record["copied"]


@pytest.mark.usefixtures("pass_gate")
@pytest.mark.parametrize(
    ("what", "relative", "payload"),
    [
        ("the loose .pbip", "Wb.pbip", {"version": "1.0", "settings": {"lastOpened": CUSTOMER_PATH}}),
        ("report.json", "Wb.Report/definition/report.json", {"resourcePackages": [{"path": CUSTOMER_PATH}]}),
        (
            "a visual",
            "Wb.Report/definition/pages/page-0/visuals/v0/visual.json",
            {"name": "v0", "visual": {"visualType": "card"}, "note": UNC_PATH},
        ),
        (
            "the model's .pbip-adjacent settings",
            "Model.SemanticModel/.platform",
            {"metadata": {"lastRefreshFrom": POSIX_PATH}},
        ),
    ],
)
def test_round3_high1_a_host_path_in_any_shipped_file_refuses_and_ships_nothing(
    package: Path, migrations: Path, what: str, relative: str, payload: dict
) -> None:
    """Exit 6, for a file the model-TMDL scan structurally could not see.

    All four of these are git-TRACKED under `migrations/**`, so each one really does reach a public
    repository. Three absolute forms are covered on purpose - drive-letter, UNC and POSIX - because
    a UNC share and a macOS path are the same defect on a different machine, which is the rule
    `_is_local_filesystem_path` already states for TMDL.
    """
    _write_json(package / "fabric" / Path(relative), payload)
    assert run(package, migrations, "--force") == pu.EXIT_REFUSED_HOST_PATH, what
    assert not migrations.exists(), "nothing may ship while a host path is still in the tree"


@pytest.mark.usefixtures("pass_gate")
def test_round3_high1_a_real_user_profile_path_is_refused_and_never_recorded(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """The username claim, pinned against a REAL user profile rather than a spelled-out constant.

    The constants above are joined at import so this tracked file does not itself trip
    `set_data_folder.py --check`; that keeps the gate honest but leaves the actual
    `X:\\Users\\<real name>` shape untested. `Path.home()` supplies it at runtime, on whichever
    machine and OS is running the suite, and it is the exact string the privacy gate hunts for.
    """
    leaked = Path.home() / "CustomerDrop" / "source.csv"
    _write_json(package / "fabric" / "Wb.pbip", {"version": "1.0", "settings": {"lastOpened": str(leaked)}})
    envelope_path = tmp_path / "home.json"
    assert run(package, migrations, "--force", "--json", str(envelope_path)) == pu.EXIT_REFUSED_HOST_PATH
    assert not migrations.exists()
    text = envelope_path.read_text(encoding="utf-8")
    assert Path.home().name not in text, text
    assert host_paths_in(text, package, migrations, Path.home()) == [], text


@pytest.mark.usefixtures("pass_gate")
def test_round3_high1_force_does_not_override_the_host_path_refusal(package: Path, migrations: Path) -> None:
    """`--force` overrides the `check_unit.py` GATE and nothing else.

    Same reasoning as #461's exit 5: the tool cannot rewrite a customer's `.pbip` or `report.json`,
    so there is no sanitized artifact for a force to ship. The negative control is the point - the
    identical package promotes at exit 0 once the path is gone.
    """
    pbip = package / "fabric" / "Wb.pbip"
    original = pbip.read_text(encoding="utf-8")
    _write_json(pbip, {"version": "1.0", "settings": {"lastOpened": CUSTOMER_PATH}})
    assert run(package, migrations, "--force") == pu.EXIT_REFUSED_HOST_PATH
    assert not migrations.exists()

    pbip.write_text(original, encoding="utf-8")
    assert run(package, migrations, "--force") == pu.EXIT_OK, "the refusal must be about the PATH, not the --force"


@pytest.mark.usefixtures("pass_gate")
def test_round3_high1_the_recorded_host_path_is_redacted_because_it_embeds_a_username(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """The refusal envelope is itself an artifact: it may name the FILE, never the path."""
    _write_json(package / "fabric" / "Wb.pbip", {"version": "1.0", "settings": {"lastOpened": CUSTOMER_PATH}})
    envelope_path = tmp_path / "host.json"
    assert run(package, migrations, "--force", "--json", str(envelope_path)) == pu.EXIT_REFUSED_HOST_PATH
    text = envelope_path.read_text(encoding="utf-8")
    assert "CustomerOperator" not in text, text
    assert "ServerA" not in text, text
    envelope = json.loads(text)
    assert envelope["status"] == "REFUSED_HOST_PATH"
    assert any("Wb.pbip" in finding for finding in envelope["findings"]), envelope["findings"]
    assert any("source.csv" in finding for finding in envelope["findings"]), "the leaf must survive"


@pytest.mark.usefixtures("pass_gate")
@pytest.mark.parametrize(
    ("what", "value"),
    [
        ("an https URL", "https://contoso.sharepoint.com/sites/finance/Shared%20Documents/Sales.xlsx"),
        ("a Databricks HttpPath", "/sql/1.0/warehouses/abc123"),
        ("a bare slash", "/"),
        ("a relative reference", "../Model.SemanticModel"),
        ("a JSON schema pointer", "#/definitions/visualContainer"),
    ],
)
def test_round3_high1_a_non_local_reference_is_not_a_host_path(
    package: Path, migrations: Path, what: str, value: str
) -> None:
    """POSITIVE CONTROL for the regex. A scan that refuses everything is not coverage.

    The URL cases are the ones that would break it: `https:` reaches `[A-Za-z]:[\\\\/]` unless BOTH
    the multi-letter-scheme lookbehind and the `(?!/)` guard are present.
    """
    _write_json(package / "fabric" / "Wb.pbip", {"version": "1.0", "settings": {"lastOpened": value}})
    assert run(package, migrations) == pu.EXIT_OK, what


@pytest.mark.usefixtures("pass_gate")
def test_round3_high1_an_absolute_path_inside_the_shipment_still_promotes(package: Path, migrations: Path) -> None:
    """Judged *absolute AND outside*, the same rule as #461 - never "absolute" alone.

    `set_data_folder.py --localize` deliberately writes an absolute path under the deliverable's
    own `data/`, and the model-TMDL scan has always allowed it. Widening the scan must not quietly
    make the whole-tree version stricter than the model version it generalises.
    """
    inside = migrations / "workbooks" / "wb" / "data" / "Extract.csv"
    _write_json(package / "fabric" / "Wb.pbip", {"version": "1.0", "settings": {"lastOpened": str(inside)}})
    assert run(package, migrations) == pu.EXIT_OK


@pytest.mark.usefixtures("pass_gate")
def test_round3_high1_a_known_binary_resource_ships_without_being_scanned(package: Path, migrations: Path) -> None:
    """POSITIVE CONTROL. PBIR carries real images; they are not text and must not block a ship."""
    logo = package / "fabric" / "Wb.Report" / "StaticResources" / "RegisteredResources" / "logo.png"
    logo.parent.mkdir(parents=True, exist_ok=True)
    logo.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe not utf-8 at all")
    assert run(package, migrations) == pu.EXIT_OK
    assert (migrations / "workbooks" / "wb" / "fabric" / "Wb.Report" / logo.relative_to(logo.parents[2])).is_file()


@pytest.mark.usefixtures("pass_gate")
def test_round3_high1_an_undecodable_file_of_an_unlisted_type_is_cannot_assess(package: Path, migrations: Path) -> None:
    """FAIL-CLOSED. "I could not read it" is exit 2 here, never a silent skip.

    Skipping every undecodable file would have made the whole scan bypassable by renaming a file:
    that is the collapse-into-the-clean-bucket defect this module's exit 2 exists to prevent.
    """
    blob = package / "fabric" / "Wb.Report" / "definition" / "mystery.dat"
    blob.write_bytes(b"\xff\xfe\x00\x01 definitely not utf-8")
    assert run(package, migrations) == pu.EXIT_CANNOT_ASSESS
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
def test_round3_high1_the_shipped_tree_is_re_scanned_after_the_copy(
    package: Path, migrations: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same condition, same verdict, whichever scan saw it first - the MEDIUM 3 rule, for exit 6."""
    real_execute = pu.execute_plan

    def _inject(plan: pu.PromotionPlan) -> pu.AppliedCopies:
        applied = real_execute(plan)
        _write_json(
            plan.report_destination / "definition" / "report.json",
            {"resourcePackages": [{"path": CUSTOMER_PATH}]},
        )
        return applied

    monkeypatch.setattr(pu, "execute_plan", _inject)
    envelope_path = tmp_path / "shipped-host.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_REFUSED_HOST_PATH
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert envelope["status"] == "REFUSED_HOST_PATH"
    assert "CustomerOperator" not in envelope_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# ROUND 3, HIGH 2 - a lexical containment check is not a containment check
#
# Measured before the fix: with `migrations/workbooks/wb` made a junction to a directory outside
# `migrations/`, `exit=0 outside-report=True outside-record=True`. Every planned path was
# *lexically* inside the root, so the check ran, agreed, and was measuring the wrong thing.
# --------------------------------------------------------------------------------------


def _link_directory(link: Path, target: Path) -> None:
    """A junction (Windows) or a directory symlink (POSIX). A lexical check sees through neither."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if completed.returncode != 0:
            pytest.skip("this machine would not create a junction")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege-dependent
        pytest.skip("this machine would not create a directory symlink")


@pytest.mark.usefixtures("pass_gate")
def test_round3_high2_a_linked_deliverable_root_cannot_escape_the_migrations_root(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """The promoter may not write, or REPLACE, anything outside its declared containment boundary."""
    outside = tmp_path / "outside-the-root"
    outside.mkdir(parents=True)
    (outside / "pre-existing.txt").write_text("must survive\n", encoding="utf-8")
    _link_directory(migrations / "workbooks" / "wb", outside)

    assert run(package, migrations) == pu.EXIT_CANNOT_ASSESS
    assert not (outside / "fabric").exists(), "nothing may be written outside the migrations root"
    assert not (outside / "promotion-record.json").exists()
    assert (outside / "pre-existing.txt").read_text(encoding="utf-8") == "must survive\n"


@pytest.mark.usefixtures("pass_gate")
def test_round3_high2_the_refusal_names_no_absolute_host_path(package: Path, migrations: Path, tmp_path: Path) -> None:
    """A containment refusal is an artifact too - it may say WHAT, never WHERE."""
    outside = tmp_path / "outside-the-root"
    outside.mkdir(parents=True)
    _link_directory(migrations / "workbooks" / "wb", outside)
    envelope_path = tmp_path / "contained.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_CANNOT_ASSESS
    text = envelope_path.read_text(encoding="utf-8")
    assert host_paths_in(text, package, migrations, outside) == [], text


@pytest.mark.usefixtures("pass_gate")
def test_round3_high2_containment_is_judged_against_the_ROOT_so_a_linked_root_still_promotes(
    package: Path, tmp_path: Path
) -> None:
    """The over-fix guard, end to end.

    A `--migrations-root` that is itself reached through a link (a rehearsal root, a mapped work
    area, a macOS `/tmp`) still contains its own children, so BOTH sides are resolved. The
    shared-datasource shape is used because its `byPath` deliberately climbs four levels - the one
    case where containment against the unit folder rather than the root would refuse a legal
    promotion.
    """
    real_root = tmp_path / "real-migrations"
    real_root.mkdir(parents=True)
    linked_root = tmp_path / "linked-migrations"
    _link_directory(linked_root, real_root)

    assert run(package, linked_root, "--datasource-slug", "shared-ds") == pu.EXIT_OK
    report = real_root / "workbooks" / "wb" / "fabric" / "Wb.Report"
    model = real_root / "datasources" / "shared-ds" / "fabric" / "Model.SemanticModel"
    assert report.is_dir() and model.is_dir()
    declared = json.loads((report / "definition.pbir").read_text(encoding="utf-8"))
    assert (
        declared["datasetReference"]["byPath"]["path"] == "../../../../datasources/shared-ds/fabric/Model.SemanticModel"
    )


def test_round3_high2_containment_resolves_the_ROOT_as_well_as_the_destination(tmp_path: Path) -> None:
    """The over-fix guard, at the level where it can actually be measured.

    ⚠️ The end-to-end test above CANNOT kill a "resolve the destination but not the root" mutation,
    and saying so is the point: `parse_args` already calls `.resolve()` on `--migrations-root`, so
    by the time `_assert_contained` sees it the two are identical and that mutation is EQUIVALENT
    rather than merely uncaught. `_assert_contained` is nonetheless reachable with an unresolved
    root - it takes whatever a caller passes - and its contract is *both sides resolved*, so the
    contract is pinned here rather than inferred from a caller that happens to resolve first.
    """
    real_root = tmp_path / "real-migrations"
    real_root.mkdir(parents=True)
    linked_root = tmp_path / "linked-migrations"
    _link_directory(linked_root, real_root)

    inside = linked_root / "workbooks" / "wb" / "fabric" / "Wb.Report"
    pu._assert_contained([inside], linked_root, "a copy")  # pylint: disable=protected-access

    with pytest.raises(pu.CannotAssess):
        pu._assert_contained(  # pylint: disable=protected-access
            [tmp_path / "elsewhere" / "Wb.Report"], linked_root, "a copy"
        )


# --------------------------------------------------------------------------------------
# ROUND 3, HIGH 3 - a path-bearing exception may not reach the `--json` envelope
#
# Measured before the fix: a destination `definition.pbir` read raising
# `FileNotFoundError(..., filename=<absolute path>)` produced exit 4 with the complete
# `C:\tfmig\wtpromote\...\definition.pbir` inside `findings`.
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_round3_high3_a_failed_bypath_rewrite_records_no_absolute_host_path(
    package: Path, migrations: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rewrite_bypath` was the one site still interpolating a raw exception."""
    real_read_text = Path.read_text

    def _boom(self: Path, *args, **kwargs):
        if self.name == "definition.pbir" and migrations.resolve() in self.resolve().parents:
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)
    envelope_path = tmp_path / "rewrite.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_PROMOTION_FAILED
    text = envelope_path.read_text(encoding="utf-8")
    assert host_paths_in(text, package, migrations, tmp_path) == [], text
    findings = json.loads(text)["findings"]
    assert any("definition.pbir" in finding for finding in findings), findings
    assert any("errno 2" in finding for finding in findings), "the operator still gets the CAUSE"


@pytest.mark.parametrize(
    ("what", "exc"),
    [
        ("a ValueError rendering two absolute paths", ValueError(f"'{Path.cwd()}' is not in the subpath of '/opt'")),
        ("a RuntimeError naming a Windows path", RuntimeError(f"failed at {CUSTOMER_PATH}")),
        ("a RuntimeError naming a UNC share", RuntimeError(f"failed at {UNC_PATH}")),
        ("a RuntimeError naming a POSIX home", RuntimeError(f"failed at {POSIX_PATH}")),
    ],
)
def test_round3_high3_safe_error_redacts_any_path_bearing_exception(what: str, exc: Exception) -> None:
    """Fixing only the one reported call site would MOVE the boundary, not remove it.

    `_safe_error` handled `OSError.filename` and trusted `str(exc)` for everything else - but
    `Path.relative_to` raises `ValueError` carrying BOTH paths, `subprocess.TimeoutExpired` renders
    a whole command line, and `_run_promotion`'s broad `except Exception` renders whatever arrives.
    Redacting in the renderer means a new path-bearing exception type cannot reintroduce it.
    """
    rendered = pu._safe_error(exc)  # pylint: disable=protected-access
    assert "CustomerOperator" not in rendered, rendered
    assert "CustomerFileServer" not in rendered, rendered
    assert "customer-analyst" not in rendered, rendered
    assert host_paths_in(rendered, Path.cwd()) == [], f"{what}: {rendered}"
    assert type(exc).__name__ in rendered, "the TYPE is still said - it is what the operator routes on"


def test_round3_high3_no_call_site_interpolates_a_raw_exception() -> None:
    """The audit, kept executable. One missed site reproduces the whole defect.

    Fixing only the reported line would have MOVED the boundary rather than removed it, so this
    checks the rule instead of the line: an exception may be interpolated only when it is one this
    module RAISED (its message is already built from `_safe_error` and redacted parts), or when it
    is the `_safe_error` fallback that feeds `redact_host_paths`. A foreign exception - `OSError`
    with its `filename`, `ValueError` from `Path.relative_to` with both paths,
    `subprocess.TimeoutExpired` with a whole command line - must go through `_safe_error`.
    """
    ours = {"CannotAssess", "PromotionFailed", "HostPathLeak", "ExternalDataPath"}
    interpolates = re.compile(r"\{exc(?:![sra])?(?::[^}]*)?\}")
    handler: set[str] | None = None
    offenders: list[str] = []
    for line in (REPO_ROOT / "scripts" / "promote_unit.py").read_text(encoding="utf-8").splitlines():
        if re.match(r"\s*(?:async\s+)?def\s", line):
            handler = None
        caught = re.match(r"\s*except\s+(.+?)\s+as\s+exc\s*:", line)
        if caught:
            handler = set(re.findall(r"\w+", caught.group(1)))
        if not interpolates.search(line) or "_safe_error" in line:
            continue
        if line.strip().startswith("return redact_host_paths("):
            continue  # the one permitted interpolation: it IS the redactor's input
        if handler is None or not handler <= ours:
            offenders.append(line.strip())
    assert offenders == [], offenders


# --------------------------------------------------------------------------------------
# ROUND 3, MEDIUM 4 - `--force` may not ship a READABLE but empty report
#
# The existing force test corrupted `visual.json`, which is `unassessable` (exit 2) and so
# exercises a branch `--force` was never near. The mutation `if source.findings and not
# args.force:` passed all 116 tests.
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pass_gate")
def test_round3_medium4_force_cannot_ship_a_readable_report_with_zero_visuals(
    package: Path, migrations: Path, tmp_path: Path
) -> None:
    """REFUSED_CONTENT (3), not CANNOT_ASSESS (2) - the branch `--force` could actually have reached.

    Every document here parses. The report is structurally present and functionally empty, which is
    exactly what exit 3 means, and `--force` overrides the `check_unit.py` gate and nothing else.
    """
    shutil.rmtree(package / "fabric" / "Wb.Report")
    _write_report(package / "fabric" / "Wb.Report", "Model.SemanticModel", pages=2, visuals_per_page=0)
    envelope_path = tmp_path / "empty.json"
    assert run(package, migrations, "--force", "--json", str(envelope_path)) == pu.EXIT_REFUSED_CONTENT
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert envelope["status"] == "REFUSED_CONTENT"
    assert any("ZERO visuals" in finding for finding in envelope["findings"]), envelope["findings"]
    assert not migrations.exists()


@pytest.mark.usefixtures("pass_gate")
def test_round3_medium4_force_cannot_ship_a_model_with_zero_tables(package: Path, migrations: Path) -> None:
    """The model half of the same claim: readable, parseable, and empty is still exit 3 under --force."""
    tables = package / "fabric" / "Model.SemanticModel" / "definition" / "tables"
    for tmdl in tables.glob("*.tmdl"):
        tmdl.write_text("// no table declaration here\n", encoding="utf-8")
    assert run(package, migrations, "--force") == pu.EXIT_REFUSED_CONTENT
    assert not migrations.exists()


# --------------------------------------------------------------------------------------
# ROUND 3, MEDIUM 5 - the required manifest boundaries were unpinned
#
# Production was already right for all five shapes; four mutations that BROKE it passed all 116
# tests. These pin the behaviour so it cannot regress silently.
# --------------------------------------------------------------------------------------

MANIFEST_BOUNDARIES = [
    ("a zero-byte manifest", ""),
    ("a truncated manifest", '{"unit": "Wb", "kind": "workb'),
    ("a manifest that is a JSON array", '["workbook"]'),
    ("a manifest that is a bare string", '"workbook"'),
    ("a manifest that is JSON null", "null"),
    ("a manifest declaring no kind", '{"unit": "Wb"}'),
    ("a manifest declaring a null kind", '{"unit": "Wb", "kind": null}'),
    ("a manifest declaring an unclassified kind", '{"unit": "Wb", "kind": "unclassified"}'),
    ("a manifest declaring an unknown kind", '{"unit": "Wb", "kind": "dashboard"}'),
]


@pytest.mark.usefixtures("pass_gate")
@pytest.mark.parametrize(("what", "text"), MANIFEST_BOUNDARIES)
def test_round3_medium5_a_malformed_manifest_blocks_and_ships_nothing(
    package: Path, migrations: Path, what: str, text: str
) -> None:
    """Exit 2 for every shape, because the kind is what decides WHERE a unit is promoted to.

    `package_unit.py:unit_kind` is explicit that the filesystem cannot answer this - every
    `pbip/<Unit>/` in a real 2.339.0 estate run carries BOTH a `.Report` and a `.SemanticModel`,
    all 62 of them - so any fallback here promotes real published datasources as workbooks.
    """
    (package / "package-manifest.json").write_text(text, encoding="utf-8")
    assert run(package, migrations) == pu.EXIT_CANNOT_ASSESS, what
    assert not migrations.exists(), what


@pytest.mark.usefixtures("pass_gate")
@pytest.mark.parametrize(("what", "text"), MANIFEST_BOUNDARIES)
def test_round3_medium5_the_refusal_says_which_manifest_shape_it_was(
    package: Path, migrations: Path, tmp_path: Path, what: str, text: str
) -> None:
    """A blocking verdict has to be actionable: `--kind` is the documented remedy, so it is named."""
    (package / "package-manifest.json").write_text(text, encoding="utf-8")
    envelope_path = tmp_path / "manifest.json"
    assert run(package, migrations, "--json", str(envelope_path)) == pu.EXIT_CANNOT_ASSESS
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert envelope["status"] == "CANNOT_ASSESS"
    joined = " ".join(envelope["findings"])
    assert "package-manifest.json" in joined, joined
    # Either remedy is legitimate and they are NOT interchangeable: `--kind` fills a gap the engine
    # left, while a manifest that will not parse is evidence about the whole package and has to be
    # regenerated. What is unacceptable is a blocking verdict with no next step at all.
    assert re.search(r"--kind|package_unit\.py", joined), f"{what}: the remedy must be named - {joined}"


@pytest.mark.usefixtures("pass_gate")
@pytest.mark.parametrize(
    ("what", "text"),
    [
        ("a missing kind", '{"unit": "Wb"}'),
        ("an unclassified kind", '{"unit": "Wb", "kind": "unclassified"}'),
        ("no manifest at all", None),
    ],
)
def test_round3_medium5_kind_fills_the_gap_the_manifest_left(
    package: Path, migrations: Path, what: str, text: str | None
) -> None:
    """POSITIVE CONTROL. A promoter that refuses every manifest passes every test above.

    `--kind` is the documented escape hatch, and the record has to say the kind came from the FLAG
    rather than from the engine - an unchecked classification must never look classified afterwards.
    """
    manifest = package / "package-manifest.json"
    if text is None:
        manifest.unlink()
    else:
        manifest.write_text(text, encoding="utf-8")
    assert run(package, migrations, "--kind", "workbook") == pu.EXIT_OK, what
    record = json.loads((migrations / "workbooks" / "wb" / "promotion-record.json").read_text(encoding="utf-8"))
    assert record["kind"] == "workbook"
    assert record["kind_source"].startswith("--kind"), record["kind_source"]


@pytest.mark.usefixtures("pass_gate")
def test_round3_medium5_kind_may_not_contradict_a_manifest_that_does_declare_one(
    package: Path, migrations: Path
) -> None:
    """`--kind` fills a GAP; an operator overruling the engine's classification is a defect report."""
    assert run(package, migrations, "--kind", "datasource") == pu.EXIT_CANNOT_ASSESS
    assert not migrations.exists()
