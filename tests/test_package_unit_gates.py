"""The acceptance test for issue #446: BOTH gates, on the packaged folder, with NO flags. See #443."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from dataclasses import replace
import subprocess
import sys
import zipfile
from pathlib import Path
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_path_ceiling as cpc  # noqa: E402  # pylint: disable=wrong-import-position
import check_reference_readiness as crr  # noqa: E402  # pylint: disable=wrong-import-position
import check_unit  # noqa: E402  # pylint: disable=wrong-import-position
import package_role_identity as pri  # noqa: E402  # pylint: disable=wrong-import-position
import package_unit as pkg  # noqa: E402  # pylint: disable=wrong-import-position
import set_data_folder as sdf  # noqa: E402  # pylint: disable=wrong-import-position
from test_check_reference_readiness import (  # noqa: E402  # pylint: disable=wrong-import-position
    seal,
    write_engine_report,
    write_handover,
    write_oracle,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal.twb"
DS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "standalone_datasource.tds"
UNIT = "Minimal"
DS_UNIT = "Shared_Extract"
WB_LUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DS_LUID = "11111111-2222-3333-4444-555555555555"
PUBLISHED_KEY = "sales-site/shared_sales"


def _write_pbir(bundle: Path, unit: str, objects: list) -> None:
    """A PBIR report with one visual-bearing page per Tableau object, named as the engine names them. See #562."""
    working = bundle / "pbip" / unit
    pages = working / f"{unit}.Report" / "definition" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "pages.json").write_text(json.dumps({"pageOrder": [obj.page_id for obj in objects]}), encoding="utf-8")
    for obj in objects:
        page = pages / obj.page_id
        (page / "visuals" / "v-1").mkdir(parents=True, exist_ok=True)
        (page / "page.json").write_text(json.dumps({"name": obj.page_id, "displayName": obj.name}), encoding="utf-8")
        (page / "visuals" / "v-1" / "visual.json").write_text(json.dumps({"name": "v-1"}), encoding="utf-8")
    (working / f"{unit}.Report" / "definition.pbir").write_text(
        json.dumps({"version": "4.0", "datasetReference": {"byPath": {"path": f"../{unit}.SemanticModel"}}}),
        encoding="utf-8",
    )
    _write_model(bundle, unit)
    (working / f"{unit}.pbip").write_text(json.dumps({"version": "1.0"}), encoding="utf-8")


def _write_model(bundle: Path, unit: str) -> None:
    """The `.SemanticModel` every engine working copy carries, datasource-only units included."""
    definition = bundle / "pbip" / unit / f"{unit}.SemanticModel" / "definition"
    definition.mkdir(parents=True, exist_ok=True)
    (definition / "model.tmdl").write_text("model Model\n", encoding="utf-8")


def _write_receipt(bundle: Path, units: list[str]) -> None:
    """The engine receipt, listing every output it wrote for these units. See #562."""
    artifacts = [
        {"path": path.relative_to(bundle).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for unit in units
        for path in sorted((bundle / "pbip" / unit).rglob("*"))
        if path.is_file()
    ]
    (bundle / "engine-output-receipt.json").write_text(
        json.dumps(
            {
                "version": 1,
                "created_at": "2026-09-10T00:00:00+00:00",
                "engine": {"version": "2.339.0", "source": "plugin", "canonical": True},
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )


def _write_input_manifest(bundle: Path, assets: list[Path]) -> None:
    """`input_manifest.json` as the engine writes it: the harvested name plus its digest."""
    (bundle / "input_manifest.json").write_text(
        json.dumps(
            {
                "assets": [
                    {"name": asset.name, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()} for asset in assets
                ]
            }
        ),
        encoding="utf-8",
    )


def _brief(
    tmp_path: Path,
    unit: str,
    scope: str = "model_and_report",
    fallback_authorization: str = "stop",
    *,
    numeric_obligation: str | None = None,
) -> Path:
    """The dispatcher's brief - the file `--brief` copies into every package it writes."""
    path = tmp_path / "briefs" / f"{unit}-migration-brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = "v1" if numeric_obligation is None else "v2"
    numeric = "" if numeric_obligation is None else f'numeric_obligation = "{numeric_obligation}"\n'
    path.write_text(
        f'+++\nschema = "phase1-start-ready/{schema}"\nunit = "{unit}"\nscope = "{scope}"\n'
        f'fallback_authorization = "{fallback_authorization}"\n{numeric}+++\n\nFaithful re-creation.\n',
        encoding="utf-8",
    )
    return path


def _bundle(
    tmp_path: Path, *, covered: set[str] | None, datasource_only: bool = False, unit: str = UNIT
) -> tuple[Path, Path, list]:
    """`(bundle, oracle, source objects)` for one real workbook, covered by the named objects only."""
    bundle = tmp_path / "bundle"
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    asset = assets / f"{WB_LUID}_{unit}.twb"
    shutil.copy2(FIXTURE, asset)
    objects = crr.source_objects(asset) or []
    assert objects, "the fixture workbook must declare dashboards/worksheets or nothing is measured"

    write_engine_report(bundle, workbooks=[unit], datasources=[DS_UNIT] if datasource_only else [])
    write_handover(bundle, unit, source_id=str(Path("_runs") / "999-x" / "assets" / asset.name))
    _write_pbir(bundle, unit, objects)
    staged = [asset]
    if datasource_only:
        _write_model(bundle, DS_UNIT)
        # The harvester's own `<luid>_<name>` filename, which is where a datasource's server
        # identity comes from - the engine strips that prefix to derive the unit name.
        datasource = assets / f"{DS_LUID}_{DS_UNIT}.tds"
        shutil.copy2(DS_FIXTURE, datasource)
        staged.append(datasource)
    _write_receipt(bundle, [unit, DS_UNIT] if datasource_only else [unit])
    _write_input_manifest(bundle, staged)

    (bundle / "source-provenance.json").write_text(
        json.dumps(
            {
                "inputs": [
                    {
                        "input": {"file": asset.name, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()},
                        "origin": {"workbook_luid": WB_LUID, "workbook_name": UNIT, "match": "sha256"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    chosen = objects if covered is None else [obj for obj in objects if obj.name in covered]
    # The capture lives in its OWN subtree, never at `tmp_path`: `_collect_evidence` scans the
    # target's grandparent too, so an `_oracle/` beside `out/` would be matched alongside the
    oracle = write_oracle(
        tmp_path / "capture",
        [
            {
                "view_luid": f"{index:08d}-0000-0000-0000-000000000000",
                "view_name": obj.name,
                "workbook_luid": WB_LUID,
                "workbook_name": UNIT,
                "view_type": obj.kind,
                # ⚠️ The shape a CURRENT capture writes, all three fields together (#480 round 3).
                # `status`+`path` alone was the pre-certification shape, and since certification
                "data": {
                    "status": "ok",
                    "certification": "certified",
                    "path": f"data/{index}.csv",
                    "row_count": 1,
                    "columns": ["a", "b"],
                },
            }
            for index, obj in enumerate(chosen)
        ],
    )
    (oracle / "data").mkdir(exist_ok=True)
    for index, _ in enumerate(chosen):
        (oracle / "data" / f"{index}.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return bundle, oracle, objects


def _package(tmp_path: Path, bundle: Path, oracle: Path, unit: str = UNIT, scope: str = "model_and_report") -> Path:
    pkg.package_unit(
        bundle,
        unit,
        tmp_path / "out",
        oracle_dir=oracle,
        assets_dir=bundle.parent / "assets",
        brief=_brief(tmp_path, unit, scope, numeric_obligation="required"),
    )
    return tmp_path / "out" / unit


def _readiness(target: Path, *others: Path) -> tuple[int, dict]:
    """Exercise the public CLI with explicit stdout capture, never a file-valued JSON argument."""
    output = io.StringIO()
    with redirect_stdout(output):
        code = crr.main([*map(str, (target, *others)), "--json", "-", "--quiet"])
    return code, json.loads(output.getvalue())


def _cli_args(bundle: Path, out: Path, oracle: Path, tmp_path: Path) -> list[str]:
    """The documented `package_unit.py` command line, WITH the brief the dispatcher writes."""
    return [
        "--bundle",
        str(bundle),
        "--out",
        str(out),
        "--oracle",
        str(oracle),
        "--brief",
        str(_brief(tmp_path, UNIT, numeric_obligation="required")),
        "--quiet",
    ]


def _binding_package(
    parent: Path,
    *,
    datasource: bool = False,
    folder: bool = False,
    datasource_luid: str = DS_LUID,
    quoted: bool = False,
    lookalikes: bool = False,
    asset_extension: str = ".tds",
) -> Path:
    """Real producer output with independently inspectable CSV bytes and an accepted projection."""
    bundle, oracle, _objects = _bundle(parent, covered=None, datasource_only=datasource)
    unit = DS_UNIT if datasource else UNIT
    if datasource:
        asset = bundle.parent / "assets" / f"{DS_LUID}_{DS_UNIT}.tds"
        text = asset.read_text(encoding="utf-8")
        assert text.count("class='postgres'") == 1
        asset.write_text(text.replace("class='postgres'", "class='textscan'"), encoding="utf-8")
        if datasource_luid != DS_LUID:
            asset.rename(asset.with_name(f"{datasource_luid}_{DS_UNIT}.tds"))
            asset = asset.with_name(f"{datasource_luid}_{DS_UNIT}.tds")
        if asset_extension == ".tdsx":
            with zipfile.ZipFile(asset.with_suffix(".tdsx"), "w") as archive:
                archive.writestr("Data/source.tds", asset.read_bytes())
            asset.unlink()
        _write_input_manifest(bundle, sorted((bundle.parent / "assets").iterdir()))
    source = parent / "Extract.Data" / "rows.csv"
    source.parent.mkdir()
    source.write_bytes(b"value\n7\n")
    definition = bundle / "pbip" / unit / f"{unit}.SemanticModel" / "definition"
    (definition / "tables").mkdir()
    if quoted:
        expressions = (
            "/// Unicode Ω before the exact byte span\n"
            'expression Caption = "data"\n'
            'expression Server = "fixture.example"\n'
            'expression Database = "warehouse"\n'
            'expression HttpPath = "/sql/1.0/warehouses/fixture"\n'
            f"""expression 'Owner''s "Archive" Root' = "{source.parent}" meta [IsParameterQuery=true, Type="Text"]\n"""
            "\tlineageTag: 16d36c9b-49bd-45bf-969b-d27606b4e1c0\n"
        )
        (definition / "expressions.tmdl").write_bytes(b"\xef\xbb\xbf" + expressions.replace("\n", "\r\n").encode())
        expression = """File.Contents(#"Owner's ""Archive"" Root" & "\\rows.csv")"""
    elif folder:
        (definition / "expressions.tmdl").write_text(
            f'expression Caption = "ordinary"\nexpression #"Extract Folder" = "{source.parent}"\n',
            encoding="utf-8",
        )
        # Preserve the source's Windows suffix, not a CI host's "/" absolute-source literal.
        expression = 'File.Contents(#"Extract Folder" & "\\rows.csv")'
    else:
        expression = f'File.Contents("{source}")'
    source_m = f"Csv.Document({expression})"
    if quoted:
        source_m = (
            "let C = Text.Length(Caption), D = Sql.Database(Server, Database), "
            f"H = Databricks.Catalogs(Server, HttpPath), R = {source_m} in R"
        )
    if lookalikes:
        source_m = (
            """let Metadata = [#"Owner's ""Archive"" Root" = 1], Predicate = each [#"File.Contents"] <> null, """
            f"Bytes = ({source_m}) in Bytes"
        )
        (definition / "tables" / "Calculated.tmdl").write_text(
            "table Calculated\n\tpartition Calculated = calculated\n\t\tsource =\n"
            "\t\t\tVAR expression = { 1 }\n\t\t\tRETURN\n\t\t\t\texpression\n",
            encoding="utf-8",
        )
    (definition / "tables" / "Rows.tmdl").write_text(
        f"table Rows\n\tpartition Rows = m\n\t\tmode: import\n\t\tsource = {source_m}\n",
        encoding="utf-8",
    )
    _write_receipt(bundle, [UNIT, DS_UNIT] if datasource else [UNIT])
    root = _package(parent, bundle, oracle, unit, "model_only" if datasource else "model_and_report")
    assert pkg.pri.verify_s1(root).integrity.is_clean
    assessment = pkg.data_access.read_data_access(root / "data-access.json")
    assert assessment.state == "local_import_ready", assessment.codes
    return root


def _binding_cli(root: str | Path, *flags: str) -> dict:
    """The public argument parser and return code, with diagnostics held for privacy assertions."""
    output = io.StringIO()
    with redirect_stdout(output):
        code = sdf.main(["--package", str(root), *flags])
    result = json.loads(output.getvalue())
    assert result["exit_code"] == code
    assert str(root) not in output.getvalue()
    assert "START_READY" not in output.getvalue()
    return result


def _shared_ready_cohort(parent: Path) -> tuple[Path, Path]:
    """Real parsed published source, producer projections, source-named pages and genuine renders."""
    from test_package_unit_reproductions import _shared_bundle
    from test_check_reference_readiness import write_report

    bundle, _old_oracle = _shared_bundle(parent)
    source = bundle.parent / "assets" / f"{WB_LUID}_{UNIT}.twb"
    assert source.read_bytes() == (Path(__file__).parent / "fixtures" / "published_datasource.twb").read_bytes()
    objects = crr.source_objects(source)
    assert objects
    pages = bundle / "pbip" / UNIT / f"{UNIT}.Report" / "definition" / "pages"
    shutil.rmtree(pages)
    write_report(bundle, UNIT, [obj.page_id for obj in objects])
    _write_receipt(bundle, [UNIT, DS_UNIT])
    oracle = write_oracle(
        parent / "source-capture",
        [
            {
                "view_name": obj.name,
                "view_type": obj.kind,
                "workbook_luid": WB_LUID,
                "workbook_name": UNIT,
                "view_luid": f"{index:08d}-0000-0000-0000-000000000000",
            }
            for index, obj in enumerate(objects)
        ],
    )
    out = parent / "out"
    provider, consumer = out / DS_UNIT, out / UNIT
    for unit, scope in ((DS_UNIT, "model_only"), (UNIT, "report_only_shared_model")):
        pkg.package_unit(
            bundle,
            unit,
            out,
            assets_dir=bundle.parent / "assets",
            oracle_dir=oracle,
            brief=_brief(parent, unit, scope, numeric_obligation="required"),
            provider_packages=(provider,) if unit == UNIT else (),
        )
    assert _binding_cli(provider)["exit_code"] == 0
    assert _binding_cli(consumer, "--provider-package", str(provider))["exit_code"] == 0
    return provider, consumer


def _stamp_resolved_published_pair(provider: Path, consumer: Path) -> None:  # pylint: disable=too-many-locals
    """Opt in downstream binding fixtures only; refuse conflicting identity rather than repair it."""
    publication = {"id": DS_UNIT, "site": "sales-site", "key": PUBLISHED_KEY}
    snapshots = []
    for root, kind, luid in ((provider, "datasource", DS_LUID), (consumer, "workbook", WB_LUID)):
        manifest = json.loads((root / "package-manifest.json").read_bytes())
        assert manifest["kind"] == kind
        asset = root / manifest["artifacts"]["asset"]
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        provenance = json.loads((root / "source-provenance.json").read_bytes())
        (row,) = [item for item in provenance["inputs"] if item["input"]["file"] == asset.name]
        assert row["input"]["sha256"] == digest
        origin = row["origin"]
        assert origin[f"{kind}_luid"] == luid
        assert origin["match"] == ("packaged_bytes" if kind == "datasource" else "sha256")
        spec = json.loads((root / manifest["artifacts"]["migration_spec"]).read_bytes())
        assert spec["source"]["file_name"] == asset.name
        if kind == "datasource":
            assert manifest["unit"] == DS_UNIT
            assert "published_dependencies" not in origin
            (source,) = spec["data_sources"]
            assert source.get("published_datasource") in (None, publication)
            metadata = publication
        else:
            ((ordinal, source),) = [
                (index, item) for index, item in enumerate(spec["data_sources"]) if "published_datasource" in item
            ]
            metadata = {**publication, "luid": DS_LUID}
            assert source["published_datasource"] in ({"luid": DS_LUID}, metadata)
            authority = {
                "schema": "tableau-published-dependencies/v1",
                "source_sha256": digest,
                "workbook_luid": origin["workbook_luid"],
                "source_match": "sha256",
                "rows": [
                    {
                        "source_ordinal": ordinal,
                        "published_key": PUBLISHED_KEY,
                        "state": "resolved",
                        "candidate_count": 1,
                        "datasource_luid": DS_LUID,
                    }
                ],
            }
            # S2's fixture already carries P. Do not overwrite a contradictory or mistyped block.
            if "published_dependencies" in origin:
                assert json.dumps(origin["published_dependencies"], sort_keys=True) == json.dumps(
                    authority, sort_keys=True
                )
            origin["published_dependencies"] = authority
        source["published_datasource"] = metadata
        snapshots.append((root, manifest, spec, provenance))
    # Validate both identities before writing either package, then seal all fixture changes last.
    for root, manifest, spec, provenance in snapshots:
        (root / manifest["artifacts"]["migration_spec"]).write_text(json.dumps(spec), encoding="utf-8")
        (root / "source-provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
        seal(root, **manifest)
        assert pri.verify_s1(root).integrity.is_clean


def _bound_shared_cohort(parent: Path) -> tuple[Path, Path]:
    """A real BOUND local datasource and a report-only workbook with source-bound published authority."""
    import test_check_reference_readiness as source_fixtures

    provider = _binding_package(parent / "selected", datasource=True)
    consumer = parent / "consumer" / source_fixtures.WB_UNIT
    model = os.path.relpath(
        provider / "fabric" / f"{DS_UNIT}.SemanticModel",
        consumer / "fabric" / f"{source_fixtures.WB_UNIT}.Report",
    ).replace(os.sep, "/")
    source_fixtures.workbook_package(
        consumer,
        published={"luid": DS_LUID},
        binding=model,
        body="<workbook><worksheets><worksheet name='Overview'/></worksheets></workbook>",
    )
    shutil.rmtree(consumer / "oracle")
    source_fixtures.write_report(
        consumer, source_fixtures.WB_UNIT, [crr.engine_page_id("page-ws-Overview")], base="fabric"
    )
    write_oracle(
        consumer, [{"view_name": "Overview", "view_type": "worksheet", "workbook_luid": source_fixtures.WB_LUID}]
    )
    projection_path = consumer / "data-access.json"
    projection = json.loads(projection_path.read_bytes())
    projection["provider_unit"] = (
        "provider-ref:v1:sha256:"
        + hashlib.sha256(b"phase1-data-access/provider-unit/v1\0" + DS_UNIT.encode()).hexdigest()
    )
    projection_path.write_text(json.dumps(projection), encoding="utf-8")
    _stamp_resolved_published_pair(provider, consumer)
    assert _binding_cli(provider)["inspection"]["state"] == "BOUND"
    assert _binding_cli(consumer, "--provider-package", str(provider))["inspection"]["state"] == "NOT_APPLICABLE"
    return provider, consumer


@pytest.mark.parametrize("reverse", [True])
@pytest.mark.parametrize("extra_provider", [True], ids=["unselected-unbound-datasource"])
def test_start_ready_selected_bound_pair_ignores_unrelated_unbound_datasource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool, extra_provider: bool
) -> None:
    provider, consumer = _bound_shared_cohort(tmp_path / "shared")
    other_luid = "22222222-3333-4444-5555-666666666666"
    other = _binding_package(tmp_path / "other", datasource=True, datasource_luid=other_luid)
    assert other.name == provider.name and str(other) != str(provider)
    unbound = _binding_cli(other, "--inspect")
    assert unbound["exit_code"] == 1 and unbound["inspection"]["state"] == "UNBOUND"
    roots = [other, provider, consumer] if extra_provider else [provider, consumer]
    if reverse:
        roots.reverse()
    issued = pri.verify_phase1_role_identity(roots)
    assert all(role.is_start_ready for role in issued)
    selected = issued[roots.index(consumer)].dependencies
    assert len(selected) == 1
    assert type(selected[0]) is pri.DependencyResult and selected[0].state == pri.STATE_RESOLVED
    assert type(selected[0].provider_ordinal) is int and selected[0].provider_ordinal == roots.index(provider)
    assert issued[roots.index(provider)].source_identity.tableau_luid == DS_LUID
    if extra_provider:
        assert issued[roots.index(other)].source_identity.tableau_luid == other_luid
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for root in (provider, consumer, other)
        for path in root.rglob("*")
        if path.is_file()
    }
    calls = []
    bind = pkg.bind_package

    def inspect(root, **kwargs):
        assert kwargs["inspect"] is True and kwargs["sanitize"] is False
        calls.append((root, kwargs["provider_packages"]))
        return bind(root, **kwargs)

    monkeypatch.setattr(pkg, "bind_package", inspect)
    code, report = _readiness(*roots)
    assert calls == [(str(root), (str(provider),) if root == consumer else ()) for root in roots]
    assert (code, report["status"]) == ((1, "FINDINGS") if extra_provider else (0, "START_READY"))
    assert report["packages_scanned"] == len(report["package_readiness"]) == len(roots)
    assert report["packages_start_ready"] == 2 and report["packages_cannot_establish"] == 0
    assert [row["ordinal"] for row in report["package_readiness"]] == list(range(len(roots)))
    for root in (provider, consumer):
        row = report["package_readiness"][roots.index(root)]
        assert row["status"] == "START_READY" and row["failed_stage"] is None and row["codes"] == []
    if extra_provider:
        row = report["package_readiness"][roots.index(other)]
        assert (row["status"], row["failed_stage"], row["codes"]) == ("FINDINGS", "binding", ["binding_not_current"])
    assert before == {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for root in (provider, consumer, other)
        for path in root.rglob("*")
        if path.is_file()
    }
    code, report = _readiness(other)
    assert code == 1 and report["package_readiness"][0]["codes"] == ["binding_not_current"]
    assert report["packages_scanned"] == 1 and report["packages_start_ready"] == 0
    assert before == {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for root in (provider, consumer, other)
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("extension", [".tdsx"])
def test_start_ready_real_datasource_extensions_keep_earned_reference_not_applicable(
    tmp_path: Path, extension: str
) -> None:
    root = _binding_package(tmp_path, datasource=True, asset_extension=extension)
    assert _binding_cli(root)["exit_code"] == 0
    code, report = _readiness(root)
    assert (code, report["status"]) == (0, "START_READY")
    assert report["units"][0]["status"] == "NOT_APPLICABLE"
    assert report["pages_expected"] == 0
    assert report["package_source"][0]["kind"] == "datasource"
    assert json.loads((root / "package-manifest.json").read_bytes())["artifacts"]["asset"].endswith(extension)


@pytest.mark.parametrize("reverse", [True])
@pytest.mark.parametrize("extra_provider", [True], ids=["equal-name-unselected-provider"])
def test_start_ready_real_shared_report_only_cohort_uses_exact_roots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], reverse: bool, extra_provider: bool
) -> None:
    provider, consumer = _shared_ready_cohort(tmp_path / "shared")
    roots = [provider, consumer]
    if extra_provider:
        other = _binding_package(
            tmp_path / "other", datasource=True, datasource_luid="22222222-3333-4444-5555-666666666666"
        )
        assert other.name == provider.name and str(other) != str(provider)
        assert _binding_cli(other)["exit_code"] == 0
        roots.insert(0, other)
    if reverse:
        roots.reverse()
    spec = json.loads((consumer / "migration-spec.json").read_bytes())
    assert (
        spec["data_sources"][0]["connection"]["class"],
        spec["data_sources"][0]["connection"]["powerbi_target"],
    ) == ("sqlproxy", "live_source")
    before = [
        {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()} for root in roots
    ]
    capsys.readouterr()
    assert crr.main([*map(str, roots), "--json", "-", "--quiet"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "START_READY"
    assert report["packages_start_ready"] == report["packages_scanned"] == len(roots)
    assert [row["ordinal"] for row in report["package_readiness"]] == list(range(len(roots)))
    inherited = report["package_data_access"][roots.index(consumer)]["assessment"]
    assert (inherited["state"], inherited["provider_state"], inherited["effective_scope"]) == (
        "provider_inherited",
        "local_import_ready",
        "report_only_shared_model",
    )
    assert (
        inherited["provider_unit"]
        == "provider-ref:v1:sha256:"
        + hashlib.sha256(b"phase1-data-access/provider-unit/v1\0Shared_Extract").hexdigest()
    )
    assert report["pages_ready"] == report["pages_expected"] > 0
    assert before == [
        {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()} for root in roots
    ]
    assert crr.main([str(consumer), "--quiet"]) == 1, "a report-only consumer cannot discover its provider"


@pytest.mark.parametrize("authorized", [True], ids=["brief-model-only"])
def test_start_ready_live_source_without_probe_proof_keeps_its_own_ceiling(tmp_path: Path, authorized: bool) -> None:
    """A live source dispatches with no probe ledger; it is never stored or reported as validated."""
    import test_data_access_contract as authority  # pylint: disable=import-outside-toplevel

    original = authority._root_fixture.__wrapped__(tmp_path)
    assert sorted(path.name for path in original.iterdir()) == ["migration-spec.json"], "no gate evidence exists"
    bundle, oracle, _ = _bundle(tmp_path / "source", covered=None, datasource_only=True)
    asset = bundle.parent / "assets" / f"{DS_LUID}_{DS_UNIT}.tds"
    asset.write_text(
        "<datasource name='Shared'><connection class='federated'><named-connections>"
        "<named-connection name='orders-leg'><connection class='sqlserver' server='source.example' "
        "dbname='db' schema='dbo'/></named-connection></named-connections>"
        "<relation name='Orders' table='[dbo].[Orders]' connection='orders-leg' type='table'/>"
        "</connection></datasource>",
        encoding="utf-8",
    )
    _write_input_manifest(bundle, sorted((bundle.parent / "assets").iterdir()))
    out = tmp_path / "out"
    pkg.package_unit(
        bundle,
        DS_UNIT,
        out,
        oracle_dir=oracle,
        assets_dir=bundle.parent / "assets",
        gate_root=original,
        brief=_brief(
            tmp_path,
            DS_UNIT,
            "model_only",
            "model_only_unvalidated" if authorized else "stop",
            numeric_obligation="required",
        ),
    )
    package = out / DS_UNIT
    stored = json.loads((package / "data-access.json").read_bytes())
    assert stored["source_keys"] == [authority.KEY]
    assert stored["state"] == ("authorized_model_only" if authorized else "live_unproven")
    assert _binding_cli(package)["exit_code"] == 0
    before = {str(path.relative_to(original)): path.read_bytes() for path in original.rglob("*") if path.is_file()}
    code, report = _readiness(package)
    assert (code, report["status"]) == (0, "START_READY")
    assert report["package_data_access"][0]["stored"] == report["package_data_access"][0]["assessment"] == stored
    assert (stored["validation"], stored["max_phase2_claim"]) == (
        ("unvalidated", "structural_only") if authorized else ("unvalidated", "data_validated")
    )
    assert before == {
        str(path.relative_to(original)): path.read_bytes() for path in original.rglob("*") if path.is_file()
    }


def _binding_tail_package(parent: Path, tail: str, trailing: bool) -> Path:
    """A clean portable fixture with literal, independently placed later-data segments."""
    root = _binding_package(parent, folder=True)
    original, target = root / "data" / "Extract.Data", root / "data" / tail
    target.parent.mkdir(parents=True, exist_ok=True)
    original.rename(target)
    path = next((root / "fabric").glob("*.SemanticModel/definition/expressions.tmdl"))
    old = f"<PACKAGE_ROOT>{os.sep}data{os.sep}Extract.Data"
    new = f"<PACKAGE_ROOT>{os.sep}data{os.sep}{tail.replace('/', os.sep)}" + (os.sep if trailing else "")
    raw = path.read_bytes()
    assert raw.count(old.encode()) == 1
    path.write_bytes(raw.replace(old.encode(), new.encode()))
    manifest_path = root / "package-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    for row in manifest["data_sources"]["shipped"]:
        row["path"] = row["path"].replace("data/Extract.Data/", f"data/{tail}/")
    manifest["contents"]["files"] = pkg.package_contents(root)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert pkg.pri.verify_s1(root).integrity.is_clean
    assert (target / "rows.csv").read_bytes() == b"value\n7\n"
    return root


@pytest.mark.parametrize("tail", ["north/data/leaf"])
def test_binding_root_boundary_preserves_every_later_segment(tmp_path: Path, tail: str) -> None:
    root = _binding_tail_package(tmp_path, tail, True)
    held = pkg._binding_hold(root)
    portable = f"<PACKAGE_ROOT>{os.sep}data{os.sep}{tail.replace('/', os.sep)}{os.sep}"
    bound = str(root / "data" / tail) + os.sep
    text = f'expression Folder = "{portable}"\n'
    rewritten, count, untouched = sdf._rewritten(text, str(root))
    assert rewritten == text.replace(portable, bound), "portable rewriting truncated the declared data tail"
    assert (count, untouched) == (1, [])
    selected = pkg.interpret_folder_parameter(
        [(key.split(".SemanticModel/", 1)[1], raw) for key, raw in held.members.items() if key.endswith(".tmdl")]
    ).root
    assert selected is not None
    for value in (portable, bound):
        assert pkg._binding_relative(held, root, replace(selected, value=value)) == f"data/{tail}", (
            "held-root parsing truncated the declared data tail"
        )


@pytest.mark.parametrize("tail", ["data/data"])
@pytest.mark.parametrize("trailing", [True])
def test_binding_later_data_segments_survive_planning_inspection_and_public_lifecycle(
    tmp_path: Path, tail: str, trailing: bool
) -> None:
    root = _binding_tail_package(tmp_path / "data", tail, trailing)
    expression = f"fabric/{UNIT}.SemanticModel/definition/expressions.tmdl"
    portable = (root / expression).read_bytes()
    value = f"<PACKAGE_ROOT>{os.sep}data{os.sep}{tail.replace('/', os.sep)}" + (os.sep if trailing else "")
    held = pkg._binding_hold(root)
    planned = pkg._binding_plan(held, root, False)
    bound = str(root / "data" / tail) + (os.sep if trailing else "")
    assert planned[expression] == portable.replace(value.encode(), bound.encode())
    before = _binding_cli(root, "--inspect")
    assert before["inspection"]["state"] == "UNBOUND"
    assert before["inspection"]["parameters"][0]["data_tail"] == f"data/{tail}"
    first = _binding_cli(root)
    assert first["exit_code"] == 0 and first["inspection"]["state"] == "BOUND"
    assert (root / expression).read_bytes() == planned[expression]
    recipient = tmp_path / "data" / "recipient" / root.name
    shutil.copytree(root, recipient)
    for current, moved in ((root, False), (recipient, True)):
        inspection = _binding_cli(current, "--inspect")
        row = inspection["inspection"]["parameters"][0]
        assert row["data_tail"] == f"data/{tail}" and row["target_exists"]
        assert row["trailing_separator"] is trailing and row["current_root_match"] is not moved
        if moved:
            assert inspection["exit_code"] == 1
        rebound = _binding_cli(current)
        assert rebound["exit_code"] == 0 and rebound["inspection"]["validation"] == "UNVALIDATED"
        bound = str(current / "data" / tail) + (os.sep if trailing else "")
        assert (current / expression).read_bytes() == portable.replace(value.encode(), bound.encode())
        assert (Path(bound) / "rows.csv").read_bytes() == b"value\n7\n"
        sanitized = _binding_cli(current, "--sanitize")
        assert sanitized["exit_code"] == 0 and sanitized["inspection"]["state"] == "UNBOUND"
        assert sanitized["codes"] == ["binding_unbound"]
        assert (current / expression).read_bytes() == portable


@pytest.mark.parametrize("datasource", [True])
@pytest.mark.parametrize("lookalikes", [True])
def test_custom_quoted_folder_uses_one_identity_through_inspect_bind_move_rebind_sanitize(
    tmp_path: Path, datasource: bool, lookalikes: bool
) -> None:
    root = _binding_package(tmp_path, datasource=datasource, quoted=True, lookalikes=lookalikes)
    unit = DS_UNIT if datasource else UNIT
    expression = f"fabric/{unit}.SemanticModel/definition/expressions.tmdl"
    table = f"fabric/{unit}.SemanticModel/definition/tables/Rows.tmdl"
    portable, partition = (root / expression).read_bytes(), (root / table).read_bytes()
    value = ("<PACKAGE_ROOT>" + os.sep + "data" + os.sep + "Extract.Data").encode()
    identity = hashlib.sha256(b"""'Owner''s "Archive" Root'""").hexdigest()
    assert portable.startswith(b"\xef\xbb\xbf") and b"\r\n" in portable
    assert portable.count(value) == 1 and b'expression Caption = "data"' in portable
    source_access = (root / "data-access.json").read_bytes()
    references = (root / "oracle" / "oracle-manifest.json").read_bytes() if not datasource else None
    for move in (False, True):
        if move:
            target = tmp_path / "recipient" / "RenamedPackage"
            target.parent.mkdir()
            root.rename(target)
            root = target
        before = _binding_cli(root, "--inspect")
        assert before["exit_code"] == 1 and before["inspection"]["state"] == "UNBOUND"
        assert before["inspection"]["parameters"][0]["parameter_identity"] == identity
        result = _binding_cli(root)
        assert result["exit_code"] == 0 and result["inspection"]["state"] == "BOUND"
        row = result["inspection"]["parameters"][0]
        assert row["parameter_identity"] == identity and row["parameter_ordinal"] == 4
        assert row["current_root_match"] and row["target_exists"]
        assert (root / expression).read_bytes() == portable.replace(value, str(root / "data" / "Extract.Data").encode())
        assert (root / table).read_bytes() == partition
        assert (root / row["data_tail"] / "rows.csv").read_bytes() == b"value\n7\n"
        assert _binding_cli(root, "--inspect")["inspection"]["state"] == "BOUND"
        assert (root / "data-access.json").read_bytes() == source_access
        if references is not None:
            assert (root / "oracle" / "oracle-manifest.json").read_bytes() == references
        assert result["inspection"]["validation"] == "UNVALIDATED"
    sanitized = _binding_cli(root, "--sanitize")
    assert sanitized["exit_code"] == 0 and sanitized["inspection"]["state"] == "UNBOUND"
    assert sanitized["inspection"]["parameters"][0]["parameter_identity"] == identity
    assert (root / expression).read_bytes() == portable
    assert (root / table).read_bytes() == partition
    assert pkg.pri.verify_s1(root).integrity.is_clean
    assert _binding_cli(root)["exit_code"] == 0


@pytest.mark.parametrize("mode", ["bind"])
@pytest.mark.parametrize("fault", ["multiple", "comment"])
def test_package_folder_refusal_is_clean_authority_not_a_dirty_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, fault: str
) -> None:
    root = _binding_package(tmp_path, quoted=True)
    if mode == "sanitize":
        assert _binding_cli(root)["exit_code"] == 0
    definition = root / "fabric" / f"{UNIT}.SemanticModel" / "definition"
    if fault in ("multiple", "comment"):
        path = definition / "expressions.tmdl"
        path.write_bytes(path.read_bytes() + b'expression PrivateSymbolCanary = "Q:\\PrivateValueCanary\\"\r\n')
        use = 'File.Contents(PrivateSymbolCanary & "\\PrivateMemberCanary.csv")'
        if fault == "comment":
            use += " /* PrivateMalformedCanary"
        (definition / "tables" / "PrivateModelCanary.tmdl").write_bytes(
            f"table Other\n\tpartition Other = m\n\t\tsource = {use}\n".encode()
        )
    else:
        path = definition / "tables" / "Rows.tmdl"
        path.write_bytes(path.read_bytes().rstrip(b"\r\n") + f" {fault}\n".encode())
    manifest_path = root / pkg.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    manifest["contents"]["files"] = pkg.package_contents(root)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert pkg.pri.verify_s1(root).integrity.is_clean
    assert pkg.pri.verify_phase1_role_identity((root,))[0].is_start_ready
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def no_stage(*_args, **_kwargs):
        pytest.fail("the folder decision must refuse before binding target lookup or staging")

    monkeypatch.setattr(pkg, "_binding_relative", no_stage)
    monkeypatch.setattr(pkg, "_binding_stage", no_stage)
    result = _binding_cli(root, *(("--" + mode,) if mode != "bind" else ()))
    assert result["exit_code"] == (1 if fault == "multiple" else 3)
    assert result["codes"] == ["folder_parameter_multiple" if fault == "multiple" else "folder_parameter_malformed_m"]
    assert result["inspection"] == {} and result["outcome"] == "unchanged"
    for canary in ("PrivateSymbolCanary", "PrivateValueCanary", "PrivateMemberCanary", "PrivateModelCanary"):
        assert canary not in json.dumps(result)
    assert before == {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


# --------------------------------------------------------------------------------------------
# before - what an operator actually has
# --------------------------------------------------------------------------------------------


def test_the_engine_working_copy_alone_cannot_be_assessed(tmp_path: Path) -> None:
    """The defect, reproduced: exit 3 on a unit that is fine, because the join is not on disk."""
    bundle, _, _ = _bundle(tmp_path, covered=None)
    code, payload = _readiness(bundle / "pbip" / UNIT)
    assert code == 3
    assert payload["status"] == "CANNOT_ESTABLISH"


def test_the_engine_working_copy_alone_has_no_expected_page_set(tmp_path: Path) -> None:
    """`check_unit`'s half of the same defect: no `migration-spec.json` on the estate route (#443)."""
    bundle, _, _ = _bundle(tmp_path, covered=None)
    parity = check_unit.check_page_parity(bundle / "pbip" / UNIT, check_unit.load_exemptions(bundle / "pbip" / UNIT))
    assert parity["status"] == check_unit.STATUS_NOT_CHECKED
    assert "no migration-spec.json" in parity["detail"]


def _first_object_name() -> str:
    """The name of the first Tableau object in the fixture - the one the negative control covers."""
    return (crr.source_objects(FIXTURE) or [])[0].name


def test_oracle_coverage_reports_the_uncaptured_pages_as_missing(tmp_path: Path) -> None:
    bundle, oracle, _ = _bundle(tmp_path, covered={_first_object_name()})
    coverage = check_unit.check_oracle_coverage(_package(tmp_path, bundle, oracle), None, None)
    assert coverage["status"] == check_unit.STATUS_NOT_CHECKED
    assert coverage["visual_missing"]
    assert coverage["visual_present"] >= 1


# --------------------------------------------------------------------------------------------
# after - the positive control
# --------------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------------
# after - the negative control, in the SAME run
# --------------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------------
# after - the LEGACY negative control, end to end (#480 round 3)


def _legacy_oracle(oracle: Path) -> None:
    """Rewrite a captured manifest into the shape `origin/master`'s producer wrote for every 200. See #480."""
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    for view in manifest["views"]:
        data = view.get("data") or {}
        if data.get("status") == "ok":
            data.pop("certification", None)
            data["row_count"] = 1
            data["columns"] = ["a", "b"]
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_the_documented_check_unit_command_refuses_a_legacy_capture_as_numeric_evidence(tmp_path: Path) -> None:
    """The same claim through the CLI, because `check_unit`'s in-process API is not what an operator runs."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    _legacy_oracle(oracle)
    unit = _package(tmp_path, bundle, oracle)
    out = tmp_path / "legacy-unit.json"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS / "check_unit.py"), str(unit), "--quiet", "--json", str(out)],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert proc.returncode != check_unit.EXIT_USAGE, proc.stderr
    checks = {check["id"]: check for check in json.loads(out.read_text(encoding="utf-8"))["checks"]}
    assert checks["page-parity"]["status"] == check_unit.STATUS_PASS, "only the NUMERIC half is withheld"
    assert checks["oracle-coverage"]["status"] == check_unit.STATUS_NOT_CHECKED


# --------------------------------------------------------------------------------------------
# after - the datasource-only control
# --------------------------------------------------------------------------------------------


def test_a_datasource_only_unit_packages_and_neither_gate_crashes(tmp_path: Path) -> None:
    """18 of 67 units in the reference run are datasource-only; a model, no report, no oracle."""
    unit = _binding_package(tmp_path, datasource=True)
    assert _binding_cli(unit)["exit_code"] == 0

    assert (unit / "fabric" / f"{DS_UNIT}.SemanticModel").is_dir()
    assert not (unit / "oracle").exists()
    code, payload = _readiness(unit)
    assert (code, payload["status"]) == (0, "START_READY")
    assert payload["units"][0]["status"] == "NOT_APPLICABLE"
    parity = check_unit.check_page_parity(unit, check_unit.load_exemptions(unit))
    assert parity["status"] in {check_unit.STATUS_NOT_CHECKED, check_unit.STATUS_PASS}


def test_a_local_datasource_with_no_harvest_prefix_resolves_by_sha_alone(tmp_path: Path) -> None:
    """A `.tds` that never came from a server has no LUID to agree with - and that is not a failure."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None, datasource_only=True)
    harvested = bundle.parent / "assets" / f"{DS_LUID}_{DS_UNIT}.tds"
    harvested.rename(bundle.parent / "assets" / f"{DS_UNIT}.tds")
    _write_input_manifest(bundle, [bundle.parent / "assets" / f"{DS_UNIT}.tds"])

    package = _package(tmp_path, bundle, oracle, unit=DS_UNIT, scope="model_only")
    result = pri.verify_phase1_role_identity([package])[0]
    row = next(entry for entry in result.roles if entry.role == pri.ROLE_SERVER_IDENTITY)

    assert result.verdict == pri.VERDICT_START_READY, result.blockers
    assert row.state == pri.STATE_NOT_APPLICABLE
    assert pri.LIMITATION_LOCAL_SOURCE in result.authorized_limitations
    assert result.source_identity is not None and result.source_identity.sha256


@pytest.mark.parametrize("numeric_obligation", ["required"])
@pytest.mark.parametrize("newline", ["\r\n"], ids=["crlf"])
def test_v2_packager_passage_preserves_validated_bytes_role_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, numeric_obligation: str, newline: str
) -> None:
    """The real producer copies its held v2 bytes even if the caller's brief changes afterwards."""
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    source = _brief(tmp_path, UNIT)
    expected = (
        (
            f'+++\nschema = "phase1-start-ready/v2"\nunit = "{UNIT}"\nscope = "model_and_report"\n'
            f'fallback_authorization = "stop"\nnumeric_obligation = "{numeric_obligation}"\n+++\n\nFaithful re-creation.\n'
        )
        .replace("\n", newline)
        .encode("utf-8")
    )
    source.write_bytes(expected)
    assemble = pkg._assemble_unit  # pylint: disable=protected-access
    replacement = "required" if numeric_obligation == "none" else "none"

    def change_source_after_validation(*args: object, **kwargs: object) -> object:
        source.write_bytes(expected.replace(f'"{numeric_obligation}"'.encode(), f'"{replacement}"'.encode()))
        return assemble(*args, **kwargs)

    monkeypatch.setattr(pkg, "_assemble_unit", change_source_after_validation)
    pkg.package_unit(
        bundle, UNIT, tmp_path / "out", oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=source
    )
    package = tmp_path / "out" / UNIT
    assert source.read_bytes() != expected
    assert (package / "migration-brief.md").read_bytes() == expected
    manifest = json.loads((package / "package-manifest.json").read_bytes())
    assert manifest["artifacts"]["migration_brief"] == "migration-brief.md"
    assert manifest["contents"]["files"]["migration-brief.md"] == hashlib.sha256(expected).hexdigest()
    assert str(source) not in json.dumps(manifest)
    policy = pri.BriefPolicy("model_and_report", "stop", numeric_obligation)
    assert pri.read_current_brief_policy(package) == (None, policy)
    phase1 = pri.verify_phase1_role_identity([package])[0]
    assert phase1.is_start_ready and phase1.brief_policy == policy
    (package / "migration-brief.md").write_bytes(source.read_bytes())
    assert pri.read_current_brief_policy(package) == ("package_file_digest_mismatch", None)


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        ("", "brief_policy_invalid"),
        ('numeric_obligation = "optional"\n', "brief_policy_invalid"),
        ('numeric_obligation = "none"\nnumeric_obligation = "none"\n', "brief_frontmatter_unparseable"),
    ],
)
def test_invalid_v2_brief_refuses_before_real_packager_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str, code: str
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    source = _brief(tmp_path, UNIT)
    source.write_bytes(
        (
            f'+++\nschema = "phase1-start-ready/v2"\nunit = "{UNIT}"\nscope = "model_and_report"\n'
            f'fallback_authorization = "stop"\n{replacement}+++\n\nNo numeric work is needed.\n'
        ).encode("utf-8")
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an invalid explicit v2 brief reached package assembly")

    monkeypatch.setattr(pkg, "_assemble_unit", forbidden)
    with pytest.raises(pkg.PackagingError, match=f"^{code}$"):
        pkg.package_unit(
            bundle, UNIT, tmp_path / "out", oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=source
        )
    assert not (tmp_path / "out" / UNIT).exists()


# --------------------------------------------------------------------------------------------
# the documented command line, end to end
# --------------------------------------------------------------------------------------------


#: The usage exit each gate returns for an argument it cannot use, keyed by the command the README
#: prints. Deliberately PER SCRIPT rather than one shared tuple.
USAGE_EXITS = {
    "scripts/check_reference_readiness.py": (crr.EXIT_USAGE,),
    "scripts/check_unit.py": (2, check_unit.EXIT_USAGE),
    "scripts/promote_unit.py": (64,),
}

#: The one placeholder the package README puts where a caller must substitute the package's path.
#: The command is executed with ONLY this token replaced, so any other malformed argument the README
#: might grow is executed AS PRINTED and fails.
PATH_PLACEHOLDER = "<path-to-this-folder>"
TOKEN_PLACEHOLDER = "<caller-held-final-sha256>"
SLUG_PLACEHOLDER = "<delivery-slug>"

#: The command the package README leads with. Not a gate - it has no verdict and no usage-exit map -
#: so it is checked by RUNNING it and reading its effect, not by its exit classification.
BIND_SCRIPT = "scripts/set_data_folder.py"


def _rejected_the_argument(script: str, proc: subprocess.CompletedProcess[str]) -> bool:
    """Whether the gate refused the ARGUMENT, as opposed to returning a verdict about a package."""
    return proc.returncode in USAGE_EXITS[script] and bool(proc.stderr.strip())


@pytest.mark.slow
def test_every_command_the_readme_prints_produces_a_verdict_not_a_usage_error(tmp_path: Path) -> None:
    """The README showed the unit NAME where both gates require a PATH (2026-09-03 cold run)."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)
    readme = (unit / "README.md").read_text(encoding="utf-8")
    commands = [line.split() for line in readme.splitlines() if line.startswith("    python scripts/")]
    unmapped = [command[1] for command in commands if command[1] not in {*USAGE_EXITS, BIND_SCRIPT}]
    assert not unmapped, f"the README prints a command whose usage exit is unknown here: {unmapped}"
    gates = [command for command in commands if command[1] in USAGE_EXITS]
    assert len(gates) == 4, f"expected entry, diagnostic, pinned final check and promotion, got {commands}"
    assert [command[1] for command in commands] == [
        BIND_SCRIPT,
        "scripts/check_reference_readiness.py",
        "scripts/check_unit.py",
        "scripts/check_unit.py",
        "scripts/promote_unit.py",
    ]
    assert commands[1][-2:] == ["--json", "-"], "START_READY is stdout-only; never restore file-valued JSON"
    assert all("--force" not in command for command in commands)
    substitutions = {
        PATH_PLACEHOLDER: str(unit),
        TOKEN_PLACEHOLDER: "c" * 64,  # Explicit caller input, not a receipt discovered in the fixture.
        SLUG_PLACEHOLDER: "r2-command-rehearsal",
    }
    assert [command[-1] for command in commands if "--receipt-sha256" in command] == [TOKEN_PLACEHOLDER] * 2
    binding = _run_gate(BIND_SCRIPT, ["--package", str(unit)], tmp_path)
    assert binding.returncode == 0, binding.stdout + binding.stderr

    for _python, script, *arguments in gates:
        as_printed = [substitutions.get(argument, argument) for argument in arguments]
        by_doc = _run_gate(script, as_printed, tmp_path)
        assert not _rejected_the_argument(script, by_doc), (
            f"the README's own command `{script} {' '.join(arguments)}` returned no verdict: "
            f"exit {by_doc.returncode}, stderr {by_doc.stderr.strip()!r}"
        )
        by_name = _run_gate(script, [unit.name], tmp_path)
        assert _rejected_the_argument(script, by_name), (
            f"negative control failed: {script} accepted the bare unit name {unit.name!r} "
            f"(exit {by_name.returncode}), so this test could not have caught the defect it exists for"
        )


@pytest.mark.slow
def test_the_usage_map_separates_argparses_2_from_check_units_NOT_CHECKED(tmp_path: Path) -> None:
    """Round-2 finding 5: `check_unit.py`'s map omitted argparse's own 2, so a usage error read as a verdict."""
    script = "scripts/check_unit.py"
    bogus = _run_gate(script, ["--definitely-not-a-flag"], tmp_path)
    assert (bogus.returncode, bool(bogus.stderr.strip())) == (2, True)
    assert _rejected_the_argument(script, bogus), "an argparse usage error is being read as a verdict"

    missing = _run_gate(script, ["definitely-not-a-directory"], tmp_path)
    assert missing.returncode == check_unit.EXIT_USAGE
    assert _rejected_the_argument(script, missing)

    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)
    real = _run_gate(script, [str(unit)], tmp_path)
    assert not _rejected_the_argument(script, real), (
        f"a verdict on a real package was classified as a usage error: exit {real.returncode}, "
        f"stderr {real.stderr.strip()!r}"
    )


def _run_gate(script: str, arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one README command line, from a directory where the bare unit name resolves to nothing."""
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SCRIPTS.parent / script),
            *arguments,
            *(["--quiet"] if script in ("scripts/check_unit.py", "scripts/check_reference_readiness.py") else []),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
        timeout=900,
    )


def test_incomplete_flat_package_without_manifest_fails_closed_when_ancestor_evidence_present(
    tmp_path: Path,
) -> None:
    """An incomplete flat package without manifest and without local evidence refuses ancestor evidence."""
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    run_oracle = tmp_path / "oracle"
    if not run_oracle.exists():
        shutil.copytree(oracle, run_oracle)
    flat_packages = tmp_path / "packages"
    pkg.main(_cli_args(bundle, flat_packages, run_oracle, tmp_path))
    unit = flat_packages / UNIT

    # Simulate incomplete package by removing both package-manifest.json AND local oracle evidence
    (unit / "package-manifest.json").unlink()
    shutil.rmtree(unit / "oracle")

    # ENTRY GATE ONLY. ⚠️ The refusal SHAPE changed with #562: the damaged boundary is classified
    # before the root is resolved and before any discovery runs, so the gate forms no opinion at all
    code, payload = _readiness(unit)
    assert (code, payload["status"]) == (3, "CANNOT_ESTABLISH")
    assert payload["pages_ready"] == 0
    assert payload["pages_expected"] == 0, f"classification must precede the {len(objects)}-page expectation"
    assert payload["evidence_records"] == 0, "no ancestor evidence may be collected for a damaged package"
    assert "package_marker_missing" in payload["units"][0]["detail"]

    # Exit gate refuses ancestor evidence: oracle-coverage is NOT_CHECKED with 0 evidence.
    # ⚠️ Pre-existing behaviour on a REAL (un-aliased) path, unchanged and unclaimed by #562.
    coverage = check_unit.check_oracle_coverage(unit, None, None)
    assert coverage["status"] == check_unit.STATUS_NOT_CHECKED
    assert coverage["visual_present"] == 0
    assert coverage["numeric_present"] == 0

    # ⚠️ The exit gate's SHAPE changed the same way, one slice later (#562 follow-up): `run_all`
    # classifies the original target first, so a damaged boundary stops before `_unit_dir` resolves
    run_report = check_unit.run_all(unit, scope=check_unit.SCOPE_REPORT)
    checks = {c["id"]: c for c in run_report["checks"]}
    assert "oracle-coverage" not in checks
    assert run_report["exit_code"] == check_unit.EXIT_NOT_CHECKED
    assert checks[check_unit.PACKAGE_BOUNDARY_CHECK_ID]["code"] == "package_marker_missing"


def test_the_packager_budgets_against_those_same_two_literals() -> None:
    """A second copy of "260" is how a repo ends up with two length rules - so there is only one."""
    assert (pkg.WINDOWS_LIMITS.file_ceiling, pkg.WINDOWS_LIMITS.dir_ceiling) == (259, 247)
    assert pkg.platform_limits("nt") == pkg.WINDOWS_LIMITS
    assert pkg.platform_limits("posix").file_ceiling == cpc.POSIX_PATH_CEILING > cpc.FILE_CEILING


# --------------------------------------------------------------------------------------------
# compact-ID runs (#865): the engine ships `<luid>_<unit>`, provenance stamped the original name
# --------------------------------------------------------------------------------------------

COMPACT_UNIT = "u0001"
ORIGINAL_ASSET = f"{WB_LUID}_{UNIT}.twb"


def _compact_package(tmp_path: Path) -> Path:
    """A compact-ID package whose provenance `input.file` is the original, pre-staging filename."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None, unit=COMPACT_UNIT)
    path = bundle / "source-provenance.json"
    provenance = json.loads(path.read_text(encoding="utf-8"))
    provenance["inputs"][0]["input"]["file"] = ORIGINAL_ASSET
    path.write_text(json.dumps(provenance), encoding="utf-8")
    return _package(tmp_path, bundle, oracle, COMPACT_UNIT)


def test_a_compact_id_package_is_identified_by_sha_and_luid_not_its_filename_label(tmp_path: Path) -> None:
    package = _compact_package(tmp_path)
    result = pri.verify_phase1_role_identity([package])[0]

    assert result.verdict == pri.VERDICT_START_READY, result.blockers
    (row,) = json.loads((package / "source-provenance.json").read_text(encoding="utf-8"))["inputs"]
    assert row["input"]["file"] == ORIGINAL_ASSET
    assert [path.name for path in (package / "assets").iterdir()] == [f"{WB_LUID}_{COMPACT_UNIT}.twb"]


@pytest.mark.parametrize(
    ("part", "field", "value", "code"),
    [
        ("input", "sha256", "0" * 64, pri.CODE_PROVENANCE_SHA),
        ("origin", "workbook_luid", DS_LUID, pri.CODE_LUID_CONTRADICTION),
    ],
    ids=["sha-mismatch", "luid-contradiction"],
)
def test_a_compact_id_package_still_refuses_foreign_bytes_or_luid(
    tmp_path: Path, part: str, field: str, value: str, code: str
) -> None:
    package = _compact_package(tmp_path)
    path = package / "source-provenance.json"
    provenance = json.loads(path.read_text(encoding="utf-8"))
    provenance["inputs"][0][part][field] = value
    path.write_text(json.dumps(provenance), encoding="utf-8")
    seal(package, **json.loads((package / "package-manifest.json").read_bytes()))
    result = pri.verify_phase1_role_identity([package])[0]

    assert result.verdict != pri.VERDICT_START_READY
    assert code in {row.code for row in result.roles if row.state in pri.BLOCKING_STATES}, result.roles
