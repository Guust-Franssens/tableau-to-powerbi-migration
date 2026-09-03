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
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_reference_readiness as crr  # noqa: E402  # pylint: disable=wrong-import-position
import check_unit  # noqa: E402  # pylint: disable=wrong-import-position
import package_unit as pkg  # noqa: E402  # pylint: disable=wrong-import-position
from test_check_reference_readiness import (  # noqa: E402  # pylint: disable=wrong-import-position
    write_engine_report,
    write_handover,
    write_oracle,
)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal.twb"
UNIT = "Minimal"
DS_UNIT = "Shared_Extract"
WB_LUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _write_pbir(bundle: Path, unit: str, objects: list) -> None:
    """A PBIR report with one visual-bearing page per Tableau object, named as the engine names them.

    `displayName` carries the Tableau object name because `check_unit.actual_pages` matches on it,
    and each page gets a `visual.json` because `_page_visual_count` is what distinguishes a rebuilt
    page from the engine's crash-guard placeholder.
    """
    pages = bundle / "pbip" / unit / f"{unit}.Report" / "definition" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "pages.json").write_text(json.dumps({"pageOrder": [obj.page_id for obj in objects]}), encoding="utf-8")
    for obj in objects:
        page = pages / obj.page_id
        (page / "visuals" / "v-1").mkdir(parents=True, exist_ok=True)
        (page / "page.json").write_text(json.dumps({"name": obj.page_id, "displayName": obj.name}), encoding="utf-8")
        (page / "visuals" / "v-1" / "visual.json").write_text(json.dumps({"name": "v-1"}), encoding="utf-8")


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
    if datasource_only:
        model = bundle / "pbip" / DS_UNIT / f"{DS_UNIT}.SemanticModel" / "definition"
        model.mkdir(parents=True, exist_ok=True)
        (model / "model.tmdl").write_text("model Model\n", encoding="utf-8")

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
                "data": {"status": "ok", "path": f"data/{index}.csv"},
            }
            for index, obj in enumerate(chosen)
        ],
    )
    (oracle / "data").mkdir(exist_ok=True)
    for index, _ in enumerate(chosen):
        (oracle / "data" / f"{index}.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return bundle, oracle, objects


def _package(tmp_path: Path, bundle: Path, oracle: Path, unit: str = UNIT) -> Path:
    pkg.package_unit(bundle, unit, tmp_path / "out", oracle_dir=oracle, assets_dir=bundle.parent / "assets")
    return tmp_path / "out" / unit


def _readiness(target: Path, tmp_path: Path) -> tuple[int, dict]:
    """Run the ENTRY gate exactly as documented - the target, and nothing else."""
    out = tmp_path / f"readiness-{target.name}-{abs(hash(str(target))) % 9999}.json"
    code = crr.main([str(target), "--json", str(out), "--quiet"])
    return code, json.loads(out.read_text(encoding="utf-8"))


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
# after - the datasource-only control
# --------------------------------------------------------------------------------------------


def test_a_datasource_only_unit_packages_and_neither_gate_crashes(tmp_path: Path) -> None:
    """18 of 67 units in the reference run are datasource-only; a model, no report, no oracle."""
    bundle, oracle, _ = _bundle(tmp_path, covered=None, datasource_only=True)
    unit = _package(tmp_path, bundle, oracle, unit=DS_UNIT)

    assert (unit / "fabric" / f"{DS_UNIT}.SemanticModel").is_dir()
    assert not (unit / "oracle").exists()
    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (0, "NOT_APPLICABLE")
    parity = check_unit.check_page_parity(unit, check_unit.load_exemptions(unit))
    assert parity["status"] in {check_unit.STATUS_NOT_CHECKED, check_unit.STATUS_PASS}


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


def test_an_out_dir_inside_the_capture_tree_is_refused(tmp_path: Path) -> None:
    """The silent downgrade this fixture layout produced: BOTH oracles visible, every page unverifiable.

    Refused up front rather than documented, because the symptom - `unverifiable` instead of `ready` -
    reads as a capture problem and points nowhere near the `--out` that caused it.
    """
    bundle, oracle, _ = _bundle(tmp_path, covered=None)
    nested = oracle.parent / "units"
    with pytest.raises(SystemExit) as excinfo:
        pkg.main(["--bundle", str(bundle), "--out", str(nested), "--oracle", str(oracle), "--quiet"])
    assert excinfo.value.code == 2
    assert not any(nested.iterdir())
    assert pkg.conflicting_evidence_dirs(nested) == [oracle]


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
    unit = _package(tmp_path, bundle, oracle, unit=DS_UNIT)

    scoped = json.loads((unit / "report.json").read_text(encoding="utf-8"))
    assert [entry["name"] for entry in scoped["datasources"]] == [DS_UNIT]
    assert scoped["workbooks"] == []
    assert crr._engine_report(unit) is not None  # pylint: disable=protected-access
    assert check_unit._is_engine_report(unit / "report.json")  # pylint: disable=protected-access

    code, payload = _readiness(unit, tmp_path)
    assert (code, payload["status"]) == (0, "NOT_APPLICABLE")
