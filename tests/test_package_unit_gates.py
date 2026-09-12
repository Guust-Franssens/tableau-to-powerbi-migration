"""The acceptance test for issue #446: BOTH gates, on the packaged folder, with NO flags.

This is the whole point of the packaging, so it is tested as a before/after rather than as an
assertion in isolation. The same gate, on the same engine output, is run twice:

* **before** - pointed at the engine working copy (`bundle/pbip/<Unit>`), which is what an operator
  actually has. `check_reference_readiness.py` exits 3 `CANNOT_ESTABLISH` because neither `--source`
  nor `--oracle` can be derived from that path, and `check_unit.py` cannot derive an expected page
  set at all (#443). Exit 3 reads like "this unit is broken" rather than "you did not tell me where
  the workbook is", which is the defect;
* **after** - pointed at the package, no flags, and both produce a real per-page verdict.

The negative control shares a fixture with the positive one on purpose: one workbook, renders for
some of its objects and not others, so a single run has to report `ready` for the covered pages and
`blind` for the rest. Packaging that manufactured coverage would fail here, and packaging that lost
it would fail here too.
"""

from __future__ import annotations

import hashlib
import builtins
import json
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_path_ceiling as cpc  # noqa: E402  # pylint: disable=wrong-import-position
import check_reference_readiness as crr  # noqa: E402  # pylint: disable=wrong-import-position
import check_unit  # noqa: E402  # pylint: disable=wrong-import-position
import package_role_identity as pri  # noqa: E402  # pylint: disable=wrong-import-position
import package_unit as pkg  # noqa: E402  # pylint: disable=wrong-import-position
import set_data_folder as sdf  # noqa: E402  # pylint: disable=wrong-import-position
from test_check_reference_readiness import (  # noqa: E402  # pylint: disable=wrong-import-position
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


def _write_pbir(bundle: Path, unit: str, objects: list) -> None:
    """A PBIR report with one visual-bearing page per Tableau object, named as the engine names them.

    `displayName` carries the Tableau object name because `check_unit.actual_pages` matches on it,
    and each page gets a `visual.json` because `_page_visual_count` is what distinguishes a rebuilt
    page from the engine's crash-guard placeholder.

    ⚠️ The `.SemanticModel`, the `definition.pbir` binding and the `.pbip` entry point are here
    because a real 2.339.0 `pbip/<Unit>/` carries all three - measured on all 62 units of the
    reference run - and #562 S2 checks that cardinality. A fixture with a report and no model was
    modelling engine output that does not exist.
    """
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
    """The engine receipt, listing every output it wrote for these units.

    `package_unit` re-roots the rows under `fabric/` and drops every row belonging to another unit,
    which is what lets #562 S2 ask whether a package's receipt accounts for the report/model/PBIP
    roles it claims - and only for files that are actually in it.
    """
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


def _brief(tmp_path: Path, unit: str, scope: str = "model_and_report", fallback_authorization: str = "stop") -> Path:
    """The dispatcher's brief - the file `--brief` copies into every package it writes."""
    path = tmp_path / "briefs" / f"{unit}-migration-brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'+++\nschema = "phase1-start-ready/v1"\nunit = "{unit}"\nscope = "{scope}"\n'
        f'fallback_authorization = "{fallback_authorization}"\n+++\n\nFaithful re-creation.\n',
        encoding="utf-8",
    )
    return path


def _bundle(tmp_path: Path, *, covered: set[str] | None, datasource_only: bool = False) -> tuple[Path, Path, list]:
    """`(bundle, oracle, source objects)` for one real workbook, covered by the named objects only."""
    bundle = tmp_path / "bundle"
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    asset = assets / f"{WB_LUID}_{UNIT}.twb"
    shutil.copy2(FIXTURE, asset)
    objects = crr.source_objects(asset) or []
    assert objects, "the fixture workbook must declare dashboards/worksheets or nothing is measured"

    write_engine_report(bundle, workbooks=[UNIT], datasources=[DS_UNIT] if datasource_only else [])
    write_handover(bundle, UNIT, source_id=str(Path("_runs") / "999-x" / "assets" / asset.name))
    _write_pbir(bundle, UNIT, objects)
    staged = [asset]
    if datasource_only:
        _write_model(bundle, DS_UNIT)
        # The harvester's own `<luid>_<name>` filename, which is where a datasource's server
        # identity comes from - the engine strips that prefix to derive the unit name.
        datasource = assets / f"{DS_LUID}_{DS_UNIT}.tds"
        shutil.copy2(DS_FIXTURE, datasource)
        staged.append(datasource)
    _write_receipt(bundle, [UNIT, DS_UNIT] if datasource_only else [UNIT])
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
    # packaged subset and every page would read `unverifiable`. `conflicting_evidence_dirs` refuses
    # that layout; this fixture models the layout a real run actually has.
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
                # became authoritative that record is unassessable: packaging withholds its `path`
                # and both gates below correctly report NOT_CHECKED. This is the positive end-to-end
                # control, so it has to be a capture something actually measured; the negative half
                # is `test_a_legacy_uncertified_capture_earns_no_numeric_evidence_end_to_end`.
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
        brief=_brief(tmp_path, unit, scope),
    )
    return tmp_path / "out" / unit


def _readiness(target: Path, tmp_path: Path) -> tuple[int, dict]:
    """Run the ENTRY gate exactly as documented - the target, and nothing else."""
    out = tmp_path / f"readiness-{target.name}-{abs(hash(str(target))) % 9999}.json"
    code = crr.main([str(target), "--json", str(out), "--quiet"])
    return code, json.loads(out.read_text(encoding="utf-8"))


def _cli_args(bundle: Path, out: Path, oracle: Path, tmp_path: Path) -> list[str]:
    """The documented `package_unit.py` command line, WITH the brief the dispatcher writes.

    `--brief` is part of the ordinary invocation rather than an extra: a package with no brief has
    no role for the one document saying what the migration is for, and the entry gate blocks it
    (`test_a_package_written_with_no_brief_is_blocked_at_the_entry_gate` is that negative control).
    """
    return [
        "--bundle",
        str(bundle),
        "--out",
        str(out),
        "--oracle",
        str(oracle),
        "--brief",
        str(_brief(tmp_path, UNIT)),
        "--quiet",
    ]


# --------------------------------------------------------------------------------------------
# before - what an operator actually has
# --------------------------------------------------------------------------------------------


def test_the_engine_working_copy_alone_cannot_be_assessed(tmp_path: Path) -> None:
    """The defect, reproduced: exit 3 on a unit that is fine, because the join is not on disk."""
    bundle, _, _ = _bundle(tmp_path, covered=None)
    code, payload = _readiness(bundle / "pbip" / UNIT, tmp_path)
    assert code == 3
    assert payload["status"] == "CANNOT_ESTABLISH"


def test_the_engine_working_copy_alone_has_no_expected_page_set(tmp_path: Path) -> None:
    """`check_unit`'s half of the same defect: no `migration-spec.json` on the estate route (#443)."""
    bundle, _, _ = _bundle(tmp_path, covered=None)
    parity = check_unit.check_page_parity(bundle / "pbip" / UNIT, check_unit.load_exemptions(bundle / "pbip" / UNIT))
    assert parity["status"] == check_unit.STATUS_NOT_CHECKED
    assert "no migration-spec.json" in parity["detail"]


# --------------------------------------------------------------------------------------------
# after - the positive control
# --------------------------------------------------------------------------------------------


def test_readiness_needs_no_flags_on_a_package_and_reports_every_page_ready(tmp_path: Path) -> None:
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    code, payload = _readiness(_package(tmp_path, bundle, oracle), tmp_path)
    assert (code, payload["status"]) == (0, "READY")
    assert payload["pages_ready"] == payload["pages_expected"] == len(objects)
    assert payload["pages_blind"] == 0


def test_check_unit_finds_the_spec_and_the_oracle_with_no_overrides(tmp_path: Path) -> None:
    """`reference_dir`/`oracle_dir` are None - exactly the CLI's no-flag call."""
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)

    parity = check_unit.check_page_parity(unit, check_unit.load_exemptions(unit))
    coverage = check_unit.check_oracle_coverage(unit, None, None)
    assert parity["status"] == check_unit.STATUS_PASS
    assert parity["expected_count"] == parity["actual_count"] == len(objects)
    assert coverage["status"] == check_unit.STATUS_PASS
    assert coverage["pages"] == coverage["visual_present"] == coverage["numeric_present"] > 0


# --------------------------------------------------------------------------------------------
# after - the negative control, in the SAME run
# --------------------------------------------------------------------------------------------


def test_a_page_with_no_render_is_still_blind_after_packaging(tmp_path: Path) -> None:
    """Packaging must never manufacture coverage: an uncaptured page stays BLIND, and blocks exit 0."""
    bundle, oracle, objects = _bundle(tmp_path, covered={_first_object_name()})
    unit = _package(tmp_path, bundle, oracle)
    code, payload = _readiness(unit, tmp_path)

    assert (code, payload["status"]) == (1, "FINDINGS")
    assert payload["pages_ready"] == 1
    assert payload["pages_blind"] == len(objects) - 1
    readiness = {row["readiness"] for unit_row in payload["units"] for row in unit_row["pages"]}
    assert sorted(readiness) == ["blind", "ready"]


def test_oracle_coverage_reports_the_uncaptured_pages_as_missing(tmp_path: Path) -> None:
    bundle, oracle, _ = _bundle(tmp_path, covered={_first_object_name()})
    coverage = check_unit.check_oracle_coverage(_package(tmp_path, bundle, oracle), None, None)
    assert coverage["status"] == check_unit.STATUS_NOT_CHECKED
    assert coverage["visual_missing"]
    assert coverage["visual_present"] >= 1


def _first_object_name() -> str:
    """The name of the first Tableau object in the fixture - the one the negative control covers."""
    return (crr.source_objects(FIXTURE) or [])[0].name


# --------------------------------------------------------------------------------------------
# after - the LEGACY negative control, end to end (#480 round 3)
#
# The positive control above was, until round 3, `{"status": "ok", "path": ...}` and nothing else -
# which is a PRE-CERTIFICATION record, not a current one. It passed because a bare `row_count` (and
# before that, a bare `path`) was accepted as evidence. Now that certification is authoritative, the
# same fixture must be split in two: a genuinely certified capture that stays consumable, and this -
# the shape a customer's existing `_oracle/` actually holds - which must not reach a numeric gate.
# --------------------------------------------------------------------------------------------


def _legacy_oracle(oracle: Path) -> None:
    """Rewrite a captured manifest into the shape `origin/master`'s producer wrote for every 200.

    A `row_count` and `columns` derived from the body, and NO `certification` - because nothing
    certified anything. The files stay exactly where they are: this is a manifest-shape change, which
    is the only kind a pre-#480 capture on disk can have.
    """
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    for view in manifest["views"]:
        data = view.get("data") or {}
        if data.get("status") == "ok":
            data.pop("certification", None)
            data["row_count"] = 1
            data["columns"] = ["a", "b"]
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_a_legacy_uncertified_capture_earns_no_numeric_evidence_end_to_end(tmp_path: Path) -> None:
    """The whole point of #471/#480: a sign-off must not be built on numbers nobody measured.

    ⚠️ This is the SAME workbook, the SAME pages and the SAME CSV bytes as the positive control
    above; only the manifest's certification differs. So a fix that merely made packaging stricter
    for everything would fail `test_check_unit_finds_the_spec_and_the_oracle_with_no_overrides`, and
    a fix that kept trusting `row_count` would fail here. Both together are the discrimination.
    """
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    _legacy_oracle(oracle)
    unit = _package(tmp_path, bundle, oracle)

    shipped = json.loads((unit / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert shipped["views"], "the views must still ship - the bytes are retained, not deleted"
    for view in shipped["views"]:
        data = view["data"]
        assert data["status"] == "ok", "the transport DID succeed and that distinction survives"
        assert "path" not in data, "a legacy row count must not license an evidence path end to end"
        assert data["row_count"] == 1, "the recorded number is kept for forensics"
        assert data["evidence_withheld"], "the package must SAY why the number is not evidence"

    coverage = check_unit.check_oracle_coverage(unit, None, None)
    assert coverage["status"] == check_unit.STATUS_NOT_CHECKED
    assert coverage["numeric_present"] == 0, "not one page may count as numerically evidenced"
    assert coverage["visual_present"] == coverage["pages"], "the RENDER evidence is untouched by this"


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
    bundle, oracle, _ = _bundle(tmp_path, covered=None, datasource_only=True)
    unit = _package(tmp_path, bundle, oracle, unit=DS_UNIT, scope="model_only")

    assert (unit / "fabric" / f"{DS_UNIT}.SemanticModel").is_dir()
    assert not (unit / "oracle").exists()
    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (0, "NOT_APPLICABLE")
    parity = check_unit.check_page_parity(unit, check_unit.load_exemptions(unit))
    assert parity["status"] in {check_unit.STATUS_NOT_CHECKED, check_unit.STATUS_PASS}


def test_a_complete_datasource_package_is_role_and_identity_resolved(tmp_path: Path) -> None:
    """The producer half of #562 S2: the datasource package the packager could NOT emit before.

    Measured on the audited master, this same fixture packaged with `artifacts.asset: null`, no
    `migration-spec.json` and an empty provenance `inputs` list - `NOT_APPLICABLE` at exit 0, which
    is a correct REFERENCE verdict about a package a semantic build cannot start from. Reference is
    still N/A; the source, the spec, the one SHA-matching provenance row and the datasource LUID the
    harvester wrote into the filename are not, and they are what the verdict now depends on.

    ⚠️ Each assertion names the role and its state. A bare `START_READY` would also pass if the
    packager had shipped nothing at all and the verifier had stopped checking.
    """
    bundle, oracle, _ = _bundle(tmp_path, covered=None, datasource_only=True)
    package = _package(tmp_path, bundle, oracle, unit=DS_UNIT, scope="model_only")
    manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    provenance = json.loads((package / "source-provenance.json").read_text(encoding="utf-8"))

    asset = f"assets/{DS_LUID}_{DS_UNIT}.tds"
    assert manifest["artifacts"]["asset"] == asset
    assert manifest["artifacts"]["migration_spec"] == "migration-spec.json"
    assert manifest["artifacts"]["migration_brief"] == "migration-brief.md"
    assert len(provenance["inputs"]) == 1
    assert provenance["inputs"][0]["input"]["sha256"] == manifest["contents"]["files"][asset]
    assert provenance["inputs"][0]["origin"]["datasource_luid"] == DS_LUID
    assert "workbook_luid" not in provenance["inputs"][0]["origin"], "the two LUID namespaces are typed"

    result = pri.verify_phase1_role_identity([package])[0]
    states = {row.role: row.state for row in result.roles}
    assert result.verdict == pri.VERDICT_START_READY, result.blockers
    assert result.topology == pri.TOPOLOGY_STANDALONE_DATASOURCE
    assert states[pri.ROLE_SOURCE_ASSET] == pri.STATE_RESOLVED
    assert states[pri.ROLE_MIGRATION_SPEC] == pri.STATE_RESOLVED
    assert states[pri.ROLE_SOURCE_PROVENANCE] == pri.STATE_RESOLVED
    assert states[pri.ROLE_FABRIC_MODEL] == pri.STATE_RESOLVED
    assert states[pri.ROLE_SERVER_IDENTITY] == pri.STATE_RESOLVED
    assert result.source_identity is not None and result.source_identity.tableau_luid == DS_LUID
    assert states[pri.ROLE_HANDOVER] == pri.STATE_NOT_APPLICABLE
    assert states[pri.ROLE_TABLEAU_ORACLE] == pri.STATE_NOT_APPLICABLE


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


def test_a_package_written_with_no_brief_is_blocked_at_the_entry_gate(tmp_path: Path) -> None:
    """The producer's own negative: `--brief` omitted, so the package carries no brief role."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    pkg.package_unit(bundle, UNIT, tmp_path / "out", oracle_dir=oracle, assets_dir=bundle.parent / "assets")
    package = tmp_path / "out" / UNIT

    assert not (package / "migration-brief.md").exists()
    code, payload = _readiness(package, tmp_path)
    block = payload["role_identity"][0]

    assert (code, payload["status"]) == (1, "FINDINGS")
    assert block["verdict"] == "BLOCKED"
    assert [row["state"] for row in block["roles"] if row["role"] == pri.ROLE_MIGRATION_BRIEF] == [pri.STATE_MISSING]


def test_the_packaged_brief_carries_the_bytes_and_not_the_dispatchers_path(tmp_path: Path) -> None:
    """An absolute path to the dispatcher's copy is a host disclosure AND proves no availability."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    source = _brief(tmp_path, UNIT)
    package = _package(tmp_path, bundle, oracle)

    assert (package / "migration-brief.md").read_bytes() == source.read_bytes()
    manifest = (package / "package-manifest.json").read_text(encoding="utf-8")
    assert str(source) not in manifest
    assert str(source.parent) not in manifest
    assert json.loads(manifest)["artifacts"]["migration_brief"] == "migration-brief.md"


# --------------------------------------------------------------------------------------------
# the documented command line, end to end
# --------------------------------------------------------------------------------------------


@pytest.mark.slow
def test_the_documented_check_unit_command_runs_on_a_package(tmp_path: Path) -> None:
    """`python scripts/check_unit.py <packaged-unit>` - no flags but the ones that capture output."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)
    out = tmp_path / "unit.json"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS / "check_unit.py"), str(unit), "--quiet", "--json", str(out)],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert proc.returncode != check_unit.EXIT_USAGE, proc.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["page-parity"]["status"] == check_unit.STATUS_PASS
    assert checks["oracle-coverage"]["status"] == check_unit.STATUS_PASS


#: The usage exit each gate returns for an argument it cannot use, keyed by the command the README
#: prints. Deliberately PER SCRIPT rather than one shared tuple.
#:
#: ⚠️ A shared `(2, check_unit.EXIT_USAGE)` was wrong, and it took ubuntu CI to show it: exit 2 is
#: argparse's usage code in `check_reference_readiness.py` (`EXIT_USAGE = 2`, verdicts 0/1/3) but it
#: is `check_unit.EXIT_NOT_CHECKED` - a genuine VERDICT - in `check_unit.py`, whose usage code is 64.
#: So a package on which check_unit legitimately reported NOT_CHECKED was read as a usage error and
#: failed this test, with an empty stderr as the only clue. Windows local runs happened to land on a
#: different verdict, so nothing but CI saw it.
#:
#: ⚠️ **`check_unit.py` emits BOTH.** 64 is its own refusal for a directory it cannot use; 2 is what
#: argparse emits for malformed SYNTAX - measured, `check_unit.py --bogus` exits 2 with a message on
#: stderr - and dropping 2 from its map classified a real usage error as a verdict (round-2 finding
#: 5). Both are mapped, and :func:`_rejected_the_argument` requires stderr, which is what separates
#: argparse's 2 from `EXIT_NOT_CHECKED`'s silent 2. That distinction is asserted directly by
#: `test_the_usage_map_separates_argparses_2_from_check_units_NOT_CHECKED`, because a mapping nobody
#: exercises is how the wrong one survived a round.
USAGE_EXITS = {
    "scripts/check_reference_readiness.py": (crr.EXIT_USAGE,),
    "scripts/check_unit.py": (2, check_unit.EXIT_USAGE),
}

#: The one placeholder the package README puts where a caller must substitute the package's path.
#: The command is executed with ONLY this token replaced, so any other malformed argument the README
#: might grow is executed AS PRINTED and fails.
PATH_PLACEHOLDER = "<path-to-this-folder>"

#: The command the package README leads with. Not a gate - it has no verdict and no usage-exit map -
#: so it is checked by RUNNING it and reading its effect, not by its exit classification.
BIND_SCRIPT = "scripts/set_data_folder.py"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--package", "package", "--check"],
        ["--package", "package", "--inspect", "--sanitize"],
        ["--package", "package", "--sanitize", "--provider-package", "provider"],
        ["--inspect"],
        ["--provider-package", "provider"],
    ],
)
def test_binding_cli_conflicts_are_usage_before_any_package_read(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_package(*_args, **_kwargs):
        pytest.fail("usage reached package implementation")

    monkeypatch.setattr(sdf, "_package", no_package)
    with pytest.raises(SystemExit) as caught:
        sdf.main(arguments)
    assert caught.value.code == 2 and capsys.readouterr().err


def test_binding_cli_preserves_provider_order_and_duplicates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = []

    def bind(root, *, provider_packages, planner):
        captured.append((root, provider_packages, planner))
        return pkg.PackageBindingResult(1, "unchanged", "binding_roles_refused")

    monkeypatch.setattr(pkg, "bind_package", bind)
    with pytest.raises(SystemExit) as caught:
        sdf.main(
            [
                "--package",
                "root",
                "--provider-package",
                "second",
                "--provider-package",
                "first",
                "--provider-package",
                "second",
            ]
        )
    assert caught.value.code == 1
    assert captured == [("root", ("second", "first", "second"), sdf._rewritten)]
    assert json.loads(capsys.readouterr().out)["code"] == "binding_roles_refused"


def test_binding_cli_does_not_double_import_its_main_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_package_data_access_snapshot import _binding_package  # pylint: disable=import-outside-toplevel

    root = _binding_package(tmp_path)
    original_import = builtins.__import__

    def import_once(name, *args, **kwargs):
        assert name != "set_data_folder", "running __main__ must supply its planner, not re-import itself"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_once)
    monkeypatch.setattr(sys, "argv", [str(SCRIPTS / "set_data_folder.py"), "--package", str(root)])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(SCRIPTS / "set_data_folder.py"), run_name="__main__")
    assert caught.value.code == 0
    assert json.loads(capsys.readouterr().out)["code"] == "binding_bound"
    assert pri.verify_s1(root).integrity.is_clean


def test_checkout_binder_modes_retain_their_original_bytes_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "examples" / "fixture" / "fabric" / "Model.SemanticModel" / "definition" / "expressions.tmdl"
    path.parent.mkdir(parents=True)
    original = 'expression DataFolder = "<REPO_ROOT>\\examples\\fixture\\data\\"\n'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(sdf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sdf, "_model_expression_files", lambda: [path])
    assert sdf.main([]) is None
    assert path.read_text(encoding="utf-8") == (
        f'expression DataFolder = "{tmp_path / "examples" / "fixture" / "data"}{sdf.flavour_join("", trailing=True)}"\n'
    )
    output = capsys.readouterr().out
    assert output.startswith("localize (this checkout): 1 model(s)\n") and output.endswith("done - 1 file(s) updated\n")
    assert sdf.main(["--sanitize"]) is None
    assert path.read_text(encoding="utf-8") == original
    assert capsys.readouterr().out.startswith("sanitize (placeholder): 1 model(s)\n")
    monkeypatch.setattr(sdf, "_tracked_files", lambda: [])
    with pytest.raises(SystemExit) as caught:
        sdf.main(["--check"])
    assert caught.value.code == 0
    assert capsys.readouterr().out == "OK - no absolute user paths found in tracked files.\n"


@pytest.mark.parametrize("kind", ["relative", "foreign", "unc"])
def test_binding_rejects_nonlocal_roots_before_filesystem_reads(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import os  # pylint: disable=import-outside-toplevel

    values = {
        "relative": "relative-package",
        "foreign": "/foreign/Package" if os.name == "nt" else r"Q:\foreign\Package",
        "unc": r"\\unreachable-fixture\share\Package",
    }

    def no_read(_path):
        pytest.fail("nonlocal root reached filesystem admission")

    monkeypatch.setattr(Path, "lstat", no_read)
    result = pkg.bind_package(values[kind])
    assert (result.exit_code, result.code, result.outcome) == (1, "binding_root_not_native_local", "unchanged")
    assert values[kind] not in json.dumps(result.as_dict())


@pytest.mark.parametrize("kind", ["profile", "home", "temp"])
def test_binding_accepts_current_local_working_roots_without_a_neutral_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from test_package_data_access_snapshot import _binding_package  # pylint: disable=import-outside-toplevel

    local = tmp_path / kind / "fixture-person"
    local.mkdir(parents=True)
    for key in ("HOME", "USERPROFILE", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(local))
    root = _binding_package(local)
    result = pkg.bind_package(root)
    assert (result.exit_code, result.code) == (0, "binding_bound")
    output = json.dumps(result.as_dict()) + repr(result)
    assert str(local) not in output and "fixture-person" not in output
    assert pkg.sanitize_package(root).exit_code == 0


def test_binding_transfer_and_rebind_read_recipient_rows_not_old_rows(tmp_path: Path) -> None:
    from test_package_data_access_snapshot import _binding_package, _files  # pylint: disable=import-outside-toplevel

    old = _binding_package(tmp_path / "sender", b"value\n101\n")
    assert pkg.bind_package(old).exit_code == 0
    assert pkg.sanitize_package(old).exit_code == 0
    recipient = tmp_path / "recipient" / "Package"
    shutil.copytree(old, recipient)
    old_csv = next((old / "data").rglob("*.csv"))
    old_csv.write_bytes(b"value\n-999\n")
    assert pkg.bind_package(recipient).exit_code == 0
    before_move = _files(recipient)
    rebound = tmp_path / "rebound" / "Package"
    shutil.copytree(recipient, rebound)
    assert pri.verify_s1(rebound).integrity.is_clean
    mismatch = pkg.inspect_package(rebound)
    assert (mismatch.exit_code, mismatch.code) == (1, "binding_mismatch")
    assert _files(recipient) == before_move
    result = pkg.bind_package(rebound)
    assert (result.exit_code, result.code) == (0, "binding_bound")
    expression = next(rebound.glob("fabric/*.SemanticModel/definition/expressions.tmdl")).read_text(encoding="utf-8")
    literal = re.search(r'expression \w+ = "([^"]+)"', expression).group(1)
    assert Path(literal) == rebound / "data"
    assert next(Path(literal).rglob("*.csv")).read_bytes() == b"value\n101\n"
    assert old_csv.read_bytes() == b"value\n-999\n"


def test_binding_root_path_budget_is_refused_before_staging(tmp_path: Path) -> None:
    from test_package_data_access_snapshot import _binding_package, _files  # pylint: disable=import-outside-toplevel

    source = _binding_package(tmp_path / "source")
    longest_tail = max(len(key) for key in _files(source))
    depth = max(1, cpc.WINDOWS_LIMITS.file_ceiling - len(str(tmp_path)) - longest_tail + 20)
    root = tmp_path / ("x" * depth) / "Package"
    shutil.copytree(source, root)
    before = _files(root)
    result = pkg.bind_package(root)
    assert (result.exit_code, result.code) == (1, "binding_path_budget")
    assert _files(root) == before and not pkg.staging_dir(root.parent, root.name).exists()


def test_binding_reparse_root_is_refused_without_touching_target(tmp_path: Path) -> None:
    import os  # pylint: disable=import-outside-toplevel
    from test_package_data_access_snapshot import _binding_package, _files  # pylint: disable=import-outside-toplevel

    source = _binding_package(tmp_path / "source")
    alias = tmp_path / "alias"
    before = _files(source)
    if os.name == "nt":
        created = subprocess.run(
            [os.environ["COMSPEC"], "/c", "mklink", "/J", str(alias), str(source)], capture_output=True, check=False
        )
        assert created.returncode == 0, "the real junction fixture must be creatable"
    else:
        alias.symlink_to(source, target_is_directory=True)
    try:
        result = pkg.bind_package(alias)
        assert (result.exit_code, result.code) == (1, "binding_root_unsafe")
        assert _files(source) == before
    finally:
        os.rmdir(alias) if os.name == "nt" else alias.unlink()


def test_binding_missing_and_case_aliased_roots_are_not_normalized_into_acceptance(tmp_path: Path) -> None:
    import os  # pylint: disable=import-outside-toplevel
    from test_package_data_access_snapshot import _binding_package, _files  # pylint: disable=import-outside-toplevel

    root = _binding_package(tmp_path)
    before = _files(root)
    missing = root.with_name("missing")
    result = pkg.bind_package(missing)
    assert (result.exit_code, result.code) == (1, "binding_root_missing")
    if os.name == "nt":
        alias = root.with_name(root.name.swapcase())
        assert alias.name != root.name and alias.is_dir()
        result = pkg.bind_package(alias)
        assert (result.exit_code, result.code) == (1, "binding_root_alias")
    assert _files(root) == before


def test_binding_volume_identity_mismatch_refuses_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace  # pylint: disable=import-outside-toplevel
    from test_package_data_access_snapshot import _binding_package  # pylint: disable=import-outside-toplevel

    root = _binding_package(tmp_path)
    lstat = Path.lstat
    hits = []

    def foreign_device(path):
        info = lstat(path)
        if path == root:
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            fields["st_dev"] = info.st_dev + 1
            hits.append(True)
            return SimpleNamespace(**fields)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_device)
    result = pkg.bind_package(root)
    assert hits
    assert (result.exit_code, result.code) == (1, "binding_volume_mismatch")
    assert not pkg.staging_dir(root.parent, root.name).exists()


def _rejected_the_argument(script: str, proc: subprocess.CompletedProcess[str]) -> bool:
    """Whether the gate refused the ARGUMENT, as opposed to returning a verdict about a package.

    Both halves are required. The exit code alone conflates a verdict with a refusal on any gate
    whose codes overlap - `check_unit.py` returns 2 for both argparse and `NOT_CHECKED` - and stderr
    alone would accept a gate that grumbles and still reports.
    """
    return proc.returncode in USAGE_EXITS[script] and bool(proc.stderr.strip())


@pytest.mark.slow
def test_every_command_the_readme_prints_produces_a_verdict_not_a_usage_error(tmp_path: Path) -> None:
    """The README showed the unit NAME where both gates require a PATH (2026-09-03 cold run).

    Measured against the shipped `HR_Dashboard` package before this fix::

        $ python scripts/check_reference_readiness.py HR_Dashboard
        error: HR_Dashboard is not a directory                       # exit 2

    An argparse usage error is not a verdict, so an agent following the package's own map learned
    nothing about its package.

    ⚠️ **The DOCUMENTED argument is what runs.** Round-2 finding 5: this used to parse the argument
    out of the README, throw it away, and run the gate on a path it had constructed itself - so any
    malformed argument other than the exact bare unit name passed, and the test could not fail for
    the defect it was written for (`mutation_survived=True`). Only the recognized
    `<path-to-this-folder>` placeholder is substituted; everything else is executed as printed.

    ⚠️ A *verdict* is any exit the gate reaches after reading the package, INCLUDING
    `check_unit.EXIT_NOT_CHECKED` (2). "I looked and could not check it" is an opinion about the
    package; "I cannot use this argument" is not. See :data:`USAGE_EXITS`.
    """
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)
    readme = (unit / "README.md").read_text(encoding="utf-8")
    commands = [line.split() for line in readme.splitlines() if line.startswith("    python scripts/")]
    unmapped = [command[1] for command in commands if command[1] not in {*USAGE_EXITS, BIND_SCRIPT}]
    assert not unmapped, f"the README prints a command whose usage exit is unknown here: {unmapped}"
    gates = [command for command in commands if command[1] in USAGE_EXITS]
    assert len(gates) == 2, f"expected both gate commands in the package README, got {commands}"

    for _python, script, *arguments in gates:
        as_printed = [argument.replace(PATH_PLACEHOLDER, str(unit)) for argument in arguments]
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
    """Round-2 finding 5: `check_unit.py`'s map omitted argparse's own 2, so a usage error read as a verdict.

    Three measured cases, and each one is a different cell of the table:

    * `check_unit.py --bogus`   -> 2, stderr  -> a USAGE error (argparse), not a verdict
    * `check_unit.py <not-a-dir>` -> 64, stderr -> the gate's own refusal
    * `check_unit.py <package>` -> a verdict, whatever it is, and NEVER classified as usage

    The third is what stops the fix for the first from swallowing `EXIT_NOT_CHECKED`, which is also
    2 and is a genuine opinion about the package.
    """
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


@pytest.mark.slow
def test_the_readme_command_that_BINDS_the_package_actually_binds_it(tmp_path: Path) -> None:
    """The package is not runnable until it is bound, so the README's first command has to work.

    Round-2 finding 4 measured the documented relocation command writing `/tmp/package\\data\\...`
    on POSIX, reporting the folder missing, exiting 1 - and leaving the file rewritten to that
    invalid value. Round-2 finding 1 makes binding load-bearing rather than a repair, so it is run
    exactly as printed and its effect is read off disk.

    ⚠️ This fixture's unit is report-only, so what it proves is that the README's own first command
    RUNS and succeeds on a package this packager actually produced - including the model-less shape,
    which is a whole class of units and used to be answered with "is this a package folder?". The
    row-level proof (placeholder in, real directory out, partition reads the file) needs a model with
    imported data and lives in `test_package_unit.py`:
    `test_a_moved_package_still_reaches_its_rows_once_it_is_BOUND`.
    """
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    unit = _package(tmp_path, bundle, oracle)
    readme = (unit / "README.md").read_text(encoding="utf-8")
    printed = [line.split() for line in readme.splitlines() if line.startswith(f"    python {BIND_SCRIPT}")]
    assert len(printed) == 1, f"the package README no longer prints the binding command: {readme[:400]}"

    _python, script, *arguments = printed[0]
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS.parent / script), *[a.replace(PATH_PLACEHOLDER, str(unit)) for a in arguments]],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
        timeout=900,
    )
    assert proc.returncode == 0, f"the documented binding command failed: {proc.stdout}\n{proc.stderr}"
    expressions = list(unit.glob("fabric/*.SemanticModel/definition/expressions.tmdl"))
    for path in expressions:
        text = path.read_text(encoding="utf-8")
        assert pkg.PACKAGE_ROOT_TOKEN not in text, "binding left the placeholder in place"
        for value in re.findall(r'expression\s+(?:#"[^"]+"|[^\s=]+)\s*=\s*"([^"]*)"', text):
            if value.startswith(str(unit)):
                assert Path(value.rstrip("\\/")).is_dir(), f"binding wrote a directory that is not there: {value}"


def _run_gate(script: str, arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one README command line, from a directory where the bare unit name resolves to nothing."""
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS.parent / script), *arguments, "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
        timeout=900,
    )


def test_completed_flat_out_dir_layout_is_accepted_and_gates_pass(tmp_path: Path) -> None:
    """The flat `--out <run>/packages` layout is accepted and gate verdicts are clean.

    Because the package carries `package-manifest.json` (`is_self_contained`), it searches only its
    own evidence and does not shadow or double-match against the run-root oracle.
    """
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    run_oracle = tmp_path / "oracle"
    if not run_oracle.exists():
        shutil.copytree(oracle, run_oracle)
    flat_packages = tmp_path / "packages"
    exit_code = pkg.main(_cli_args(bundle, flat_packages, run_oracle, tmp_path))
    assert exit_code == 0
    unit = flat_packages / UNIT
    assert (unit / "package-manifest.json").is_file()

    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (0, "READY")
    assert payload["pages_ready"] == len(objects)

    parity = check_unit.check_page_parity(unit, check_unit.load_exemptions(unit))
    coverage = check_unit.check_oracle_coverage(unit, None, None)
    assert parity["status"] == check_unit.STATUS_PASS
    assert parity["expected_count"] == parity["actual_count"] == len(objects)
    assert coverage["status"] == check_unit.STATUS_PASS
    assert coverage["pages"] == coverage["visual_present"] == coverage["numeric_present"] > 0


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
    # instead of reporting the unit's pages as blind. Both are exit 3; the new one additionally
    # proves discovery never happened. The exit gate is asserted separately below.
    code, payload = _readiness(unit, tmp_path)
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
    # anything and there is no `oracle-coverage` row left to inspect. Strictly stronger than the
    # NOT_CHECKED row this used to assert - the coverage assertions above still hold that end.
    run_report = check_unit.run_all(unit, scope=check_unit.SCOPE_REPORT)
    checks = {c["id"]: c for c in run_report["checks"]}
    assert "oracle-coverage" not in checks
    assert run_report["exit_code"] == check_unit.EXIT_NOT_CHECKED
    assert checks[check_unit.PACKAGE_BOUNDARY_CHECK_ID]["code"] == "package_marker_missing"


def test_incomplete_nested_package_without_manifest_fails_closed_when_ancestor_evidence_present(
    tmp_path: Path,
) -> None:
    """An incomplete nested package without manifest and without local evidence refuses ancestor evidence."""
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    run_oracle = tmp_path / "oracle"
    if not run_oracle.exists():
        shutil.copytree(oracle, run_oracle)
    nested = tmp_path / "packages" / "coldrun2"
    pkg.main(_cli_args(bundle, nested, run_oracle, tmp_path))
    unit = nested / UNIT

    # Simulate incomplete package by removing both package-manifest.json AND local oracle evidence
    (unit / "package-manifest.json").unlink()
    shutil.rmtree(unit / "oracle")

    # ENTRY GATE ONLY - the exit-gate assertions below are pre-existing and unclaimed by #562.
    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (3, "CANNOT_ESTABLISH")
    assert payload["pages_ready"] == 0
    assert payload["pages_expected"] == 0, f"classification must precede the {len(objects)}-page expectation"
    assert payload["evidence_records"] == 0, "no ancestor evidence may be collected for a damaged package"
    assert "package_marker_missing" in payload["units"][0]["detail"]

    coverage = check_unit.check_oracle_coverage(unit, None, None)
    assert coverage["status"] == check_unit.STATUS_NOT_CHECKED
    assert coverage["visual_present"] == 0
    assert coverage["numeric_present"] == 0

    # ⚠️ Same shape change as the flat case above (#562 follow-up): the boundary is classified before
    # resolution, so the exit gate forms no opinion rather than reporting an unmeasurable coverage row.
    run_report = check_unit.run_all(unit, scope=check_unit.SCOPE_REPORT)
    checks = {c["id"]: c for c in run_report["checks"]}
    assert "oracle-coverage" not in checks
    assert run_report["exit_code"] == check_unit.EXIT_NOT_CHECKED
    assert checks[check_unit.PACKAGE_BOUNDARY_CHECK_ID]["code"] == "package_marker_missing"


def test_nested_batch_out_dir_compatibility_is_preserved(tmp_path: Path) -> None:
    """Existing nested batch layouts (--out <run>/packages/<batch>) remain fully compatible."""
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    nested = tmp_path / "packages" / "coldrun2"
    exit_code = pkg.main(_cli_args(bundle, nested, oracle, tmp_path))
    assert exit_code == 0
    unit = nested / UNIT
    assert (unit / "package-manifest.json").is_file()

    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (0, "READY")
    assert payload["pages_ready"] == len(objects)

    parity = check_unit.check_page_parity(unit, check_unit.load_exemptions(unit))
    coverage = check_unit.check_oracle_coverage(unit, None, None)
    assert parity["status"] == check_unit.STATUS_PASS
    assert coverage["status"] == check_unit.STATUS_PASS


# --------------------------------------------------------------------------------------------
# the scoped report, against BOTH gates - the positive control for round-1 finding 1
#
# `test_package_unit.py` proves the negative half (no foreign unit survives). Scoping can fail the
# other way too, and that failure is invisible in a leak test: `_engine_report` returns None unless
# `workbooks` is a LIST, so a scoped report that trimmed one field too many silently costs a
# datasource-only unit its earned `NOT_APPLICABLE`. Both halves are run here on a report in the real
# engine's 13-field shape rather than the minimal fixture, because the minimal one has nothing to
# over-trim.
# --------------------------------------------------------------------------------------------


def _plant_estate_report(bundle: Path, unit: str, *, datasources: list[str]) -> None:
    """Overwrite the fixture's minimal report with one shaped like a real estate run."""
    from test_package_unit import _estate_report  # pylint: disable=import-outside-toplevel

    full = _estate_report(unit)
    full["datasources"] = [{"name": name} for name in datasources] + full["datasources"]
    (bundle / "report.json").write_text(json.dumps(full), encoding="utf-8")


def test_a_scoped_estate_report_still_earns_every_page_ready(tmp_path: Path) -> None:
    """Positive control: full engine shape in, no flags out, and the verdict is unchanged."""
    bundle, oracle, objects = _bundle(tmp_path, covered=None)
    _plant_estate_report(bundle, UNIT, datasources=[])
    code, payload = _readiness(_package(tmp_path, bundle, oracle), tmp_path)
    assert (code, payload["status"]) == (0, "READY")
    assert payload["pages_ready"] == payload["pages_expected"] == len(objects)


def test_a_scoped_estate_report_still_earns_a_datasource_unit_its_not_applicable(tmp_path: Path) -> None:
    """The over-trim control: `NOT_APPLICABLE` is EARNED from `datasources[]`, and can be trimmed away.

    Dropping `workbooks` or `datasources` from the allowlist makes `_engine_report` return None here,
    and this unit stops being a datasource and starts being a broken workbook - exit 3, not exit 0.
    """
    bundle, oracle, _ = _bundle(tmp_path, covered=None, datasource_only=True)
    _plant_estate_report(bundle, UNIT, datasources=[DS_UNIT])
    unit = _package(tmp_path, bundle, oracle, unit=DS_UNIT, scope="model_only")

    scoped = json.loads((unit / "report.json").read_text(encoding="utf-8"))
    assert [entry["name"] for entry in scoped["datasources"]] == [DS_UNIT]
    assert scoped["workbooks"] == []
    assert crr._engine_report(unit) is not None  # pylint: disable=protected-access
    assert check_unit._is_engine_report(unit / "report.json")  # pylint: disable=protected-access

    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (0, "NOT_APPLICABLE")


# --------------------------------------------------------------------------------------------
# The ceilings themselves. Pinned HERE, in a suite `tests/mutate_package_unit.py` scores, because
# that was the gap: mutating `DIR_CEILING` 247 -> 260 and running the two suites the mutation
# harness uses reported `148 passed, exit 0`. The full documented gate command DOES catch it
# (`tests/test_check_path_ceiling.py` pins both, and the same mutation gives `4 failed, exit 1`),
# so the defect was never "nothing detects a regression" - it was that nothing the harness can
# score detected one, and therefore no anchor could prove the pin can fail.
# --------------------------------------------------------------------------------------------


def test_the_measured_desktop_ceilings_are_pinned_as_two_DISTINCT_literals() -> None:
    """259 and 247 are two separate end-to-end measurements, not one number and an offset.

    They come from different guards - a fully qualified FILE name and a DIRECTORY name - and were
    validated separately against Power BI Desktop 2.157.828.0, so each is pinned to its own literal.
    Deriving one from the other would let a single edit move both and still look internally
    consistent, which is exactly what a pin exists to prevent.
    """
    assert cpc.FILE_CEILING == 259, "Desktop: 'fully qualified file name must be less than 260 characters'"
    assert cpc.DIR_CEILING == 247, "Desktop: 'the directory name must be less than 248 characters'"
    assert cpc.FILE_CEILING - cpc.DIR_CEILING == 12, "the gap is CreateDirectory's 8.3 reservation, not a guess"


def test_the_packager_budgets_against_those_same_two_literals() -> None:
    """A second copy of "260" is how a repo ends up with two length rules - so there is only one.

    `package_unit` imports the pair rather than restating it, and every projected path carries the
    ceiling it was judged against, so this is the join between the pin above and the budget.
    """
    assert (pkg.WINDOWS_LIMITS.file_ceiling, pkg.WINDOWS_LIMITS.dir_ceiling) == (259, 247)
    assert pkg.platform_limits("nt") == pkg.WINDOWS_LIMITS
    assert pkg.platform_limits("posix").file_ceiling == cpc.POSIX_PATH_CEILING > cpc.FILE_CEILING
