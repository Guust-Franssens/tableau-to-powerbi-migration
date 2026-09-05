"""Tests for scripts/build_run_unit_status.py.

Verifies the derive-only per-unit status roll-up over a migration run.
Tests scope loading from estate_survey.json (fetch_order, required_datasources,
workbooks, unresolved_dependencies), artifact detection (deliverables with
promotion-record.json, packages, conversion bundle, harvested assets), fail-closed
behavior on missing/corrupt records or directory presence, duplicate name disambiguation,
exit codes, and JSON/Markdown parity.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import scripts.build_run_unit_status as rus

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "build_run_unit_status.py"


def _write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def test_sanitize_slug() -> None:
    assert rus.sanitize_slug("Sales & Marketing (Q3)") == "sales-marketing-q3"
    assert rus.sanitize_slug("Very Long Name That Exceeds Thirty Characters In Total") == "very-long-name-that-exceeds-th"
    assert rus.sanitize_slug("") == "unit"
    assert rus.sanitize_slug("---") == "unit"


def test_load_survey_scope_fetch_order_and_unresolved(tmp_path: Path) -> None:
    survey_data = {
        "fetch_order": [
            {"kind": "datasource", "name": "Global Orders", "luid": "ds-1", "project": "Sales"},
            {"kind": "workbook", "name": "Regional Overview", "luid": "wb-1", "project": "Sales"},
            "Simple Workbook Name",
        ],
        "required_datasources": [
            {"name": "Global Orders", "luid": "ds-1", "project": "Sales"},
            {"datasource_name": "Financial Data", "id": "ds-2", "projectName": "Finance"},
        ],
        "workbooks": [
            {"name": "Regional Overview", "luid": "wb-1"},
            {"workbook_name": "Executive Summary", "id": "wb-2", "project": "Exec"},
        ],
        "unresolved_dependencies": [
            {
                "datasource_name": "Financial Data",
                "status": "ambiguous",
                "candidates": [{"id": "ds-2a"}, {"id": "ds-2b"}],
                "workbook": "Executive Summary",
            },
            {
                "datasource_name": "Legacy DB",
                "status": "not_found",
                "candidates": [],
                "workbook": "Executive Summary",
            },
        ],
    }
    survey_path = _write_json(tmp_path / "estate_survey.json", survey_data)
    units, payload = rus.load_survey_scope(survey_path)

    assert payload == survey_data
    assert len(units) == 6

    # Unit 1: Global Orders (datasource)
    assert units[0].order == 1
    assert units[0].kind == "datasource"
    assert units[0].name == "Global Orders"
    assert units[0].luid == "ds-1"
    assert units[0].project == "Sales"
    assert units[0].slug == "global-orders"

    # Unit 2: Regional Overview (workbook)
    assert units[1].order == 2
    assert units[1].kind == "workbook"
    assert units[1].name == "Regional Overview"
    assert units[1].luid == "wb-1"

    # Unit 3: Simple Workbook Name (workbook from string in fetch_order)
    assert units[2].order == 3
    assert units[2].kind == "workbook"
    assert units[2].name == "Simple Workbook Name"

    # Unit 4: Financial Data (datasource from required_datasources, updated with unresolved info)
    assert units[3].order == 4
    assert units[3].kind == "datasource"
    assert units[3].name == "Financial Data"
    assert units[3].luid == "ds-2"
    assert units[3].unresolved_status == "ambiguous"
    assert len(units[3].unresolved_candidates) == 2
    assert units[3].unresolved_workbook == "Executive Summary"

    # Unit 5: Executive Summary (workbook from workbooks list)
    assert units[4].order == 5
    assert units[4].kind == "workbook"
    assert units[4].name == "Executive Summary"
    assert units[4].luid == "wb-2"

    # Unit 6: Legacy DB (datasource standalone from unresolved_dependencies)
    assert units[5].order == 6
    assert units[5].kind == "datasource"
    assert units[5].name == "Legacy DB"
    assert units[5].unresolved_status == "not_found"


def test_load_survey_scope_malformed(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad_survey.json"
    bad_json.write_text("{not-valid-json", encoding="utf-8")
    with pytest.raises(rus.CannotAssess, match="could not read JSON"):
        rus.load_survey_scope(bad_json)

    non_dict = tmp_path / "array_survey.json"
    non_dict.write_text("[]", encoding="utf-8")
    with pytest.raises(rus.CannotAssess, match="not a JSON object"):
        rus.load_survey_scope(non_dict)


def test_status_shipped_verified_and_forced_and_unverified(tmp_path: Path) -> None:
    survey_data = {
        "fetch_order": [
            {"kind": "workbook", "name": "Report Verified"},
            {"kind": "workbook", "name": "Report Forced"},
            {"kind": "workbook", "name": "Report Unverified"},
            {"kind": "datasource", "name": "DS Verified"},
        ]
    }
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    # 1. Report Verified: exit_code=0, forced=false
    wb1_dir = migrations_dir / "workbooks" / "report-verified"
    _write_json(
        wb1_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "Report Verified",
            "promoted_at": "2026-09-01T10:00:00Z",
            "forced": False,
            "check_unit": {"exit_code": 0, "status": "all gates passed"},
        },
    )

    # 2. Report Forced: forced=true
    wb2_dir = migrations_dir / "workbooks" / "report-forced"
    _write_json(
        wb2_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "Report Forced",
            "promoted_at": "2026-09-02T11:00:00Z",
            "forced": True,
            "check_unit": {"exit_code": 3, "status": "verification failed"},
        },
    )

    # 3. Report Unverified: exit_code=3, forced=false
    wb3_dir = migrations_dir / "workbooks" / "report-unverified"
    _write_json(
        wb3_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "Report Unverified",
            "promoted_at": "2026-09-03T12:00:00Z",
            "forced": False,
            "check_unit": {"exit_code": 3, "status": "gates failed"},
        },
    )

    # 4. DS Verified: check_unit.passed=True
    ds1_dir = migrations_dir / "datasources" / "ds-verified"
    _write_json(
        ds1_dir / "promotion-record.json",
        {
            "kind": "datasource",
            "unit": "DS Verified",
            "promoted_at": "2026-09-04T13:00:00Z",
            "forced": False,
            "check_unit": {"passed": True, "status": "passed"},
        },
    )

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["summary"]["total_units"] == 4
    assert rollup["summary"]["workbooks"] == 3
    assert rollup["summary"]["datasources"] == 1
    assert rollup["summary"]["by_status"][rus.STATUS_SHIPPED_VERIFIED] == 2
    assert rollup["summary"]["by_status"][rus.STATUS_SHIPPED_FORCED] == 1
    assert rollup["summary"]["by_status"][rus.STATUS_SHIPPED_UNVERIFIED] == 1

    units = rollup["units"]
    assert units[0]["status"] == rus.STATUS_SHIPPED_VERIFIED
    assert units[0]["recorded_at"] == "2026-09-01T10:00:00Z"
    assert units[0]["deliverable_path"] == "migrations/workbooks/report-verified"

    assert units[1]["status"] == rus.STATUS_SHIPPED_FORCED
    assert units[1]["recorded_at"] == "2026-09-02T11:00:00Z"

    assert units[2]["status"] == rus.STATUS_SHIPPED_UNVERIFIED
    assert units[2]["recorded_at"] == "2026-09-03T12:00:00Z"

    assert units[3]["status"] == rus.STATUS_SHIPPED_VERIFIED
    assert units[3]["deliverable_path"] == "migrations/datasources/ds-verified"


def test_directory_presence_alone_is_cannot_assess(tmp_path: Path) -> None:
    survey_data = {"fetch_order": [{"kind": "workbook", "name": "Unverified Presence"}]}
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    # Directory exists but promotion-record.json is missing
    deliv_dir = migrations_dir / "workbooks" / "unverified-presence"
    deliv_dir.mkdir(parents=True, exist_ok=True)
    (deliv_dir / "definition.pbir").write_text("{}", encoding="utf-8")

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["summary"]["by_status"][rus.STATUS_CANNOT_ASSESS] == 1
    unit = rollup["units"][0]
    assert unit["status"] == rus.STATUS_CANNOT_ASSESS
    assert "directory presence alone is unverified" in unit["evidence"]


def test_corrupt_promotion_record_is_cannot_assess(tmp_path: Path) -> None:
    survey_data = {"fetch_order": [{"kind": "workbook", "name": "Corrupt Promo"}]}
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    deliv_dir = migrations_dir / "workbooks" / "corrupt-promo"
    deliv_dir.mkdir(parents=True, exist_ok=True)
    (deliv_dir / "promotion-record.json").write_text("{corrupt", encoding="utf-8")

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["summary"]["by_status"][rus.STATUS_CANNOT_ASSESS] == 1
    assert "unreadable or malformed JSON" in rollup["units"][0]["evidence"]


def test_contradicting_kind_in_promotion_record_is_cannot_assess(tmp_path: Path) -> None:
    survey_data = {"fetch_order": [{"kind": "workbook", "name": "Kind Mismatch"}]}
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    deliv_dir = migrations_dir / "workbooks" / "kind-mismatch"
    _write_json(
        deliv_dir / "promotion-record.json",
        {
            "kind": "datasource",  # contradicts survey kind 'workbook'
            "unit": "Kind Mismatch",
            "check_unit": {"exit_code": 0},
        },
    )

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["summary"]["by_status"][rus.STATUS_CANNOT_ASSESS] == 1
    assert "contradicts survey kind" in rollup["units"][0]["evidence"]


def test_package_conversion_and_harvest_states(tmp_path: Path) -> None:
    run_dir = tmp_path / "_runs" / "001-test-run"
    survey_path = _write_json(
        run_dir / "assessment" / "estate_survey.json",
        {
            "fetch_order": [
                {"kind": "workbook", "name": "Packaged Unit", "luid": "luid-pkg"},
                {"kind": "workbook", "name": "Converted Unit"},
                {"kind": "datasource", "name": "Harvested Unit", "luid": "luid-harv"},
                {"kind": "workbook", "name": "Not Started Unit"},
            ]
        },
    )

    # 1. Packaged Unit
    pkg_dir = run_dir / "packages" / "batch-a" / "Packaged Unit"
    _write_json(
        pkg_dir / "package-manifest.json",
        {
            "unit": "Packaged Unit",
            "kind": "workbook",
            "packaged_at": "2026-09-05T08:00:00Z",
        },
    )
    _write_json(pkg_dir / "source-provenance.json", {"workbook_luid": "luid-pkg"})

    # 2. Converted Unit in bundle/pbip/
    conv_dir = run_dir / "bundle" / "pbip" / "converted-unit"
    conv_dir.mkdir(parents=True, exist_ok=True)
    (conv_dir / "report.json").write_text("{}", encoding="utf-8")

    # 3. Harvested Unit in assets/
    assets_dir = run_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "luid-harv_Harvested Unit.tdsx").write_bytes(b"PK\x03\x04")

    ctx = rus.StatusContext(
        repo_root=tmp_path,
        run_root=run_dir,
        packages_root=run_dir / "packages",
        bundle_root=run_dir / "bundle",
        assets_root=assets_dir,
        migrations_root=tmp_path / "migrations",
    )

    rollup = rus.build_status_rollup(survey_path, ctx)
    by_st = rollup["summary"]["by_status"]

    assert by_st[rus.STATUS_PACKAGED] == 1
    assert by_st[rus.STATUS_CONVERTED] == 1
    assert by_st[rus.STATUS_HARVESTED] == 1
    assert by_st[rus.STATUS_NOT_STARTED] == 1

    u_pkg = rollup["units"][0]
    assert u_pkg["status"] == rus.STATUS_PACKAGED
    assert u_pkg["recorded_at"] == "2026-09-05T08:00:00Z"
    assert "package assembled" in u_pkg["evidence"]

    u_conv = rollup["units"][1]
    assert u_conv["status"] == rus.STATUS_CONVERTED
    assert "bundle/pbip/converted-unit" in u_conv["evidence"]

    u_harv = rollup["units"][2]
    assert u_harv["status"] == rus.STATUS_HARVESTED
    assert "assets/luid-harv_Harvested Unit.tdsx" in u_harv["evidence"]

    u_ns = rollup["units"][3]
    assert u_ns["status"] == rus.STATUS_NOT_STARTED


def test_duplicate_display_names_ambiguity(tmp_path: Path) -> None:
    survey_data = {
        "fetch_order": [
            {"kind": "workbook", "name": "Sales Report", "luid": "wb-1", "project": "Region 1"},
            {"kind": "workbook", "name": "Sales Report", "luid": "wb-2", "project": "Region 2"},
        ]
    }
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    # Only one 'sales-report' exists on disk, so it ambiguously matches both
    deliv_dir = migrations_dir / "workbooks" / "sales-report"
    _write_json(
        deliv_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "Sales Report",
            "forced": False,
            "check_unit": {"exit_code": 0},
        },
    )

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["summary"]["by_status"][rus.STATUS_CANNOT_ASSESS] == 2
    for u in rollup["units"]:
        assert u["status"] == rus.STATUS_CANNOT_ASSESS
        assert "ambiguous match" in u["evidence"]


def test_markdown_and_json_parity(tmp_path: Path) -> None:
    survey_data = {
        "fetch_order": [
            {"kind": "datasource", "name": "Master | Data", "project": "Core | Dept"},
            {"kind": "workbook", "name": "Executive Dashboard", "project": "Exec"},
        ]
    }
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=tmp_path / "migrations")
    rollup = rus.build_status_rollup(survey_path, ctx)

    md = rus.render_markdown_report(rollup)

    # Verify table headers and contents in markdown
    assert "# Run Unit Status: standalone" in md
    assert rus.CEILING_STATEMENT in md
    assert "Master \\| Data" in md  # pipe is escaped
    assert "Core \\| Dept" in md
    assert "| 1 | datasource | Master \\| Data | Core \\| Dept | Not Started | — | not-started |" in md
    assert "| 2 | workbook | Executive Dashboard | Exec | Not Started | — | not-started |" in md

    # Summary table counts
    assert f"| {rus.STATUS_LABELS[rus.STATUS_NOT_STARTED]} | 2 |" in md
    assert f"| {rus.STATUS_LABELS[rus.STATUS_SHIPPED_VERIFIED]} | 0 |" in md


def test_cli_execution_with_run_and_output_files(tmp_path: Path) -> None:
    run_dir = tmp_path / "_runs" / "042-sample-estate"
    _write_json(
        run_dir / "assessment" / "estate_survey.json",
        {"fetch_order": [{"kind": "workbook", "name": "Finance Summary"}]},
    )

    # Create deliverable
    deliv_dir = tmp_path / "migrations" / "workbooks" / "finance-summary"
    _write_json(
        deliv_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "Finance Summary",
            "promoted_at": "2026-09-05T09:00:00Z",
            "forced": False,
            "check_unit": {"exit_code": 0},
        },
    )

    out_md = run_dir / "unit-status.md"
    out_json = run_dir / "unit-status.json"

    res = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--run",
            str(run_dir),
            "--migrations-root",
            str(tmp_path / "migrations"),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert res.returncode == 0
    assert out_md.is_file()
    assert out_json.is_file()

    # Verify JSON stdout
    json_out = json.loads(res.stdout)
    assert json_out["run"] == "042-sample-estate"
    assert json_out["summary"]["total_units"] == 1
    assert json_out["units"][0]["status"] == rus.STATUS_SHIPPED_VERIFIED

    # Verify JSON file on disk
    file_json = json.loads(out_json.read_text(encoding="utf-8"))
    assert file_json == json_out


def test_cli_exit_codes(tmp_path: Path) -> None:
    # Exit code 2: missing --run and --survey
    res_usage = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False)
    assert res_usage.returncode == 2

    # Exit code 2: run dir does not exist
    res_no_run = subprocess.run(
        [sys.executable, str(SCRIPT), "--run", str(tmp_path / "nonexistent-run")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_no_run.returncode == 2

    # Exit code 3: survey file does not exist
    res_no_survey = subprocess.run(
        [sys.executable, str(SCRIPT), "--survey", str(tmp_path / "missing_survey.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_no_survey.returncode == 3
