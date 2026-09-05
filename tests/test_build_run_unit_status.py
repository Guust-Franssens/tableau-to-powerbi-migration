"""Tests for scripts/build_run_unit_status.py.

Verifies the derive-only per-unit status roll-up over a migration run.
Tests:
- Strict survey validation ({}, missing/non-list fields, nameless entries -> exit 3/CannotAssess).
- Support for project_name, projectName, project fields.
- Preservation of topological/survey order (including reversed order).
- Preserving unresolved dependencies as distinct units without lossy name merging.
- Resolved vs unresolved name collision handling.
- Strict promotion record validation ({}, foreign unit, string forced="false", missing fabric/ deliverables).
- Missing promotion record vs missing deliverable directory.
- Slug collisions across duplicate display names or different projects/kinds.
- Multi-stage evidence collection and cross-stage contradiction detection.
- CLI exit code contracts: 0 (all assessed clean), 2 (usage/bad args), 3 (cannot-assess row or survey).
- Parsed record-for-record Markdown and JSON parity across all emitted fields.
"""

from __future__ import annotations

import json
import re
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


def _create_dummy_fabric(deliv_dir: Path) -> Path:
    fabric_dir = deliv_dir / "fabric"
    fabric_dir.mkdir(parents=True, exist_ok=True)
    (fabric_dir / "definition.pbir").write_text("{}", encoding="utf-8")
    return fabric_dir


def _parse_markdown_table(md_content: str) -> list[dict[str, str]]:
    """Parses the Markdown units table into a list of row dicts."""
    lines = md_content.splitlines()
    table_lines: list[str] = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("| # | Kind | Name | Slug | LUID | Project | Status | Recorded At | Deliverable | Evidence |"):
            in_table = True
            continue
        if in_table:
            if stripped.startswith("|---"):
                continue
            if stripped.startswith("|") and stripped.endswith("|"):
                table_lines.append(stripped)
            else:
                break

    rows: list[dict[str, str]] = []
    for t_line in table_lines:
        # Split by unescaped pipe
        raw_parts = [p.strip() for p in re.split(r"(?<!\\)\|", t_line)]
        # Filter empty ends from leading/trailing pipe
        parts = [p.replace(r"\|", "|") for p in raw_parts[1:-1]]
        if len(parts) >= 10:
            rows.append({
                "order": parts[0],
                "kind": parts[1],
                "name": parts[2],
                "slug": parts[3],
                "luid": parts[4],
                "project": parts[5],
                "status_label": parts[6],
                "recorded_at": parts[7],
                "deliverable": parts[8],
                "evidence": parts[9],
            })
    return rows


# ==============================================================================
# 1. Survey Scope Extraction & Strict Validation (Item 1 & Item 3)
# ==============================================================================


def test_sanitize_slug() -> None:
    assert rus.sanitize_slug("Sales & Marketing (Q3)") == "sales-marketing-q3"
    assert rus.sanitize_slug("Very Long Name That Exceeds Thirty Characters In Total") == "very-long-name-that-exceeds-th"
    assert rus.sanitize_slug("") == "unit"
    assert rus.sanitize_slug("---") == "unit"


def test_survey_empty_dict_is_cannot_assess(tmp_path: Path) -> None:
    survey_path = _write_json(tmp_path / "empty_survey.json", {})
    with pytest.raises(rus.CannotAssess, match="contains no recognized scope"):
        rus.load_survey_scope(survey_path)


def test_survey_non_dict_is_cannot_assess(tmp_path: Path) -> None:
    survey_path = _write_json(tmp_path / "array_survey.json", [])
    with pytest.raises(rus.CannotAssess, match="not a JSON object"):
        rus.load_survey_scope(survey_path)


def test_survey_malformed_json_is_cannot_assess(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{broken", encoding="utf-8")
    with pytest.raises(rus.CannotAssess, match="could not read JSON"):
        rus.load_survey_scope(bad_json)


def test_survey_non_list_scope_field_is_cannot_assess(tmp_path: Path) -> None:
    survey_path = _write_json(tmp_path / "bad_field.json", {"fetch_order": "not-a-list"})
    with pytest.raises(rus.CannotAssess, match="must be a JSON array"):
        rus.load_survey_scope(survey_path)


def test_survey_nameless_or_empty_entry_is_cannot_assess(tmp_path: Path) -> None:
    # Nameless dict in fetch_order
    survey_path1 = _write_json(tmp_path / "nameless.json", {"fetch_order": [{"kind": "workbook"}]})
    with pytest.raises(rus.CannotAssess, match="missing a valid name"):
        rus.load_survey_scope(survey_path1)

    # Empty string in fetch_order
    survey_path2 = _write_json(tmp_path / "empty_str.json", {"fetch_order": ["   "]})
    with pytest.raises(rus.CannotAssess, match="empty string entry"):
        rus.load_survey_scope(survey_path2)


def test_survey_supports_project_name_aliases(tmp_path: Path) -> None:
    survey_data = {
        "fetch_order": [
            {"kind": "datasource", "name": "DS 1", "project_name": "Project Underscore"},
            {"kind": "datasource", "name": "DS 2", "projectName": "Project Camel"},
            {"kind": "workbook", "name": "WB 1", "project": "Project Plain"},
        ]
    }
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    units, _ = rus.load_survey_scope(survey_path)

    assert len(units) == 3
    assert units[0].project == "Project Underscore"
    assert units[1].project == "Project Camel"
    assert units[2].project == "Project Plain"


def test_survey_order_preservation_and_reversed_order(tmp_path: Path) -> None:
    survey_data = {
        "fetch_order": [
            {"kind": "datasource", "name": "Zebra DS"},
            {"kind": "workbook", "name": "Alpha WB"},
            {"kind": "datasource", "name": "Beta DS"},
        ]
    }
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    units, _ = rus.load_survey_scope(survey_path)

    assert [u.name for u in units] == ["Zebra DS", "Alpha WB", "Beta DS"]
    assert [u.order for u in units] == [1, 2, 3]


def test_unresolved_dependencies_preserved_distinctly_and_name_collision(tmp_path: Path) -> None:
    # A resolved datasource in fetch_order with name 'Finance DS'
    # and an unresolved dependency in unresolved_dependencies with same name 'Finance DS'.
    # They must NOT be merged into a single unit.
    survey_data = {
        "fetch_order": [
            {"kind": "datasource", "name": "Finance DS", "luid": "ds-resolved-1", "project": "Finance"},
            {"kind": "workbook", "name": "Quarterly Report", "luid": "wb-1"},
        ],
        "unresolved_dependencies": [
            {
                "datasource_name": "Finance DS",
                "status": "ambiguous",
                "candidates": [{"id": "c1"}, {"id": "c2"}],
                "workbook": "Quarterly Report",
            },
            {
                "datasource_name": "Legacy Oracle",
                "status": "not_found",
                "workbook": "Quarterly Report",
            },
        ],
    }
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    units, _ = rus.load_survey_scope(survey_path)

    # 4 distinct units: 1 resolved DS, 1 WB, 2 distinct unresolved DS units
    assert len(units) == 4
    assert units[0].name == "Finance DS"
    assert units[0].luid == "ds-resolved-1"
    assert units[0].unresolved_status is None

    assert units[1].name == "Quarterly Report"

    assert units[2].name == "Finance DS"
    assert units[2].unresolved_status == "ambiguous"
    assert units[2].unresolved_workbook == "Quarterly Report"

    assert units[3].name == "Legacy Oracle"
    assert units[3].unresolved_status == "not_found"

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=tmp_path / "migrations")
    rollup = rus.build_status_rollup(survey_path, ctx)
    assert rollup["units"][2]["status"] == rus.STATUS_UNRESOLVED_DEPENDENCY
    assert rollup["units"][3]["status"] == rus.STATUS_UNRESOLVED_DEPENDENCY


# ==============================================================================
# 2. Promotion Records & Deliverables Validation (Item 2)
# ==============================================================================


def test_status_shipped_verified_forced_and_unverified(tmp_path: Path) -> None:
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
    _create_dummy_fabric(wb1_dir)
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
    _create_dummy_fabric(wb2_dir)
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
    _create_dummy_fabric(wb3_dir)
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
    _create_dummy_fabric(ds1_dir)
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


def test_promotion_record_empty_dict_is_cannot_assess(tmp_path: Path) -> None:
    survey_data = {"fetch_order": [{"kind": "workbook", "name": "Empty Promo"}]}
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    deliv_dir = migrations_dir / "workbooks" / "empty-promo"
    _create_dummy_fabric(deliv_dir)
    _write_json(deliv_dir / "promotion-record.json", {})

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["units"][0]["status"] == rus.STATUS_CANNOT_ASSESS
    assert "empty or not a JSON object" in rollup["units"][0]["evidence"]


def test_promotion_record_foreign_unit_is_cannot_assess(tmp_path: Path) -> None:
    survey_data = {"fetch_order": [{"kind": "workbook", "name": "Local Unit"}]}
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    deliv_dir = migrations_dir / "workbooks" / "local-unit"
    _create_dummy_fabric(deliv_dir)
    _write_json(
        deliv_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "Foreign Unit",
            "forced": False,
            "check_unit": {"exit_code": 0},
        },
    )

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["units"][0]["status"] == rus.STATUS_CANNOT_ASSESS
    assert "does not match surveyed unit" in rollup["units"][0]["evidence"]


def test_promotion_record_string_forced_is_cannot_assess(tmp_path: Path) -> None:
    survey_data = {"fetch_order": [{"kind": "workbook", "name": "String Forced"}]}
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    deliv_dir = migrations_dir / "workbooks" / "string-forced"
    _create_dummy_fabric(deliv_dir)
    _write_json(
        deliv_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "String Forced",
            "forced": "false",  # string boolean is invalid
            "check_unit": {"exit_code": 0},
        },
    )

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["units"][0]["status"] == rus.STATUS_CANNOT_ASSESS
    assert "forced' field must be a boolean" in rollup["units"][0]["evidence"]


def test_promotion_record_missing_fabric_is_cannot_assess(tmp_path: Path) -> None:
    survey_data = {"fetch_order": [{"kind": "workbook", "name": "Missing Fabric"}]}
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    deliv_dir = migrations_dir / "workbooks" / "missing-fabric"
    # No fabric dir created!
    _write_json(
        deliv_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "Missing Fabric",
            "forced": False,
            "check_unit": {"exit_code": 0},
        },
    )

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["units"][0]["status"] == rus.STATUS_CANNOT_ASSESS
    assert "missing durable artifacts under fabric/" in rollup["units"][0]["evidence"]


def test_promotion_record_missing_declared_copied_file_is_cannot_assess(tmp_path: Path) -> None:
    survey_data = {"fetch_order": [{"kind": "workbook", "name": "Missing Copied"}]}
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    deliv_dir = migrations_dir / "workbooks" / "missing-copied"
    _create_dummy_fabric(deliv_dir)
    _write_json(
        deliv_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "Missing Copied",
            "forced": False,
            "check_unit": {"exit_code": 0},
            "copied": [{"destination": "fabric/missing_file.pbip"}],
        },
    )

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["units"][0]["status"] == rus.STATUS_CANNOT_ASSESS
    assert "promoted artifact destination declared in promotion-record.json missing on disk" in rollup["units"][0]["evidence"]


def test_directory_presence_alone_is_cannot_assess(tmp_path: Path) -> None:
    survey_data = {"fetch_order": [{"kind": "workbook", "name": "Unverified Presence"}]}
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    # Directory exists with fabric artifacts but promotion-record.json is missing
    deliv_dir = migrations_dir / "workbooks" / "unverified-presence"
    _create_dummy_fabric(deliv_dir)

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["summary"]["by_status"][rus.STATUS_CANNOT_ASSESS] == 1
    unit = rollup["units"][0]
    assert unit["status"] == rus.STATUS_CANNOT_ASSESS
    assert "directory presence alone is unverified" in unit["evidence"]


def test_missing_record_vs_missing_deliverable(tmp_path: Path) -> None:
    # 1 unit with deliverable dir but no promotion-record -> cannot_assess
    # 1 unit with no deliverable dir and no other artifacts -> not_started
    survey_data = {
        "fetch_order": [
            {"kind": "workbook", "name": "Dir Exists No Record"},
            {"kind": "workbook", "name": "Dir Does Not Exist"},
        ]
    }
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    deliv1 = migrations_dir / "workbooks" / "dir-exists-no-record"
    deliv1.mkdir(parents=True, exist_ok=True)

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["units"][0]["status"] == rus.STATUS_CANNOT_ASSESS
    assert rollup["units"][1]["status"] == rus.STATUS_NOT_STARTED


def test_slug_collision_across_projects_and_kinds(tmp_path: Path) -> None:
    # Two workbooks with same display name in different projects map to same slug
    survey_data = {
        "fetch_order": [
            {"kind": "workbook", "name": "Executive KPI", "luid": "wb-1", "project": "Sales"},
            {"kind": "workbook", "name": "Executive KPI", "luid": "wb-2", "project": "Marketing"},
        ]
    }
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    migrations_dir = tmp_path / "migrations"

    # Single deliverable on disk
    deliv_dir = migrations_dir / "workbooks" / "executive-kpi"
    _create_dummy_fabric(deliv_dir)
    _write_json(
        deliv_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "Executive KPI",
            "forced": False,
            "check_unit": {"exit_code": 0},
        },
    )

    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=migrations_dir)
    rollup = rus.build_status_rollup(survey_path, ctx)

    assert rollup["summary"]["by_status"][rus.STATUS_CANNOT_ASSESS] == 2
    for u in rollup["units"]:
        assert u["status"] == rus.STATUS_CANNOT_ASSESS
        assert "slug collision: multiple survey units map to deliverable slug 'executive-kpi'" in u["evidence"]


# ==============================================================================
# 3. Multi-Stage Evidence & Cross-Stage Contradictions (Item 4)
# ==============================================================================


def test_package_conversion_and_harvest_stages(tmp_path: Path) -> None:
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


def test_contradictory_deliverable_vs_package_kind_is_cannot_assess(tmp_path: Path) -> None:
    # Unit surveyed as workbook, has workbook promotion record, but datasource package!
    run_dir = tmp_path / "_runs" / "002-contradiction"
    survey_path = _write_json(
        run_dir / "assessment" / "estate_survey.json",
        {"fetch_order": [{"kind": "workbook", "name": "Conflict Unit"}]},
    )

    migrations_dir = tmp_path / "migrations"
    deliv_dir = migrations_dir / "workbooks" / "conflict-unit"
    _create_dummy_fabric(deliv_dir)
    _write_json(
        deliv_dir / "promotion-record.json",
        {
            "kind": "workbook",
            "unit": "Conflict Unit",
            "forced": False,
            "check_unit": {"exit_code": 0},
        },
    )

    # Package manifest claims kind: datasource
    pkg_dir = run_dir / "packages" / "batch-a" / "Conflict Unit"
    _write_json(
        pkg_dir / "package-manifest.json",
        {
            "unit": "Conflict Unit",
            "kind": "datasource",
            "packaged_at": "2026-09-05T08:00:00Z",
        },
    )

    ctx = rus.StatusContext(
        repo_root=tmp_path,
        run_root=run_dir,
        packages_root=run_dir / "packages",
        bundle_root=run_dir / "bundle",
        assets_root=run_dir / "assets",
        migrations_root=migrations_dir,
    )

    rollup = rus.build_status_rollup(survey_path, ctx)
    assert rollup["units"][0]["status"] == rus.STATUS_CANNOT_ASSESS
    assert "package-manifest.json claims kind='datasource' which contradicts survey kind 'workbook'" in rollup["units"][0]["evidence"]


# ==============================================================================
# 4. Markdown & JSON Record-for-Record Parity (Item 6)
# ==============================================================================


def test_markdown_and_json_record_level_parity(tmp_path: Path) -> None:
    survey_data = {
        "fetch_order": [
            {"kind": "datasource", "name": "Master | Data", "luid": "ds-01", "project": "Core | Dept"},
            {"kind": "workbook", "name": "Executive Dashboard", "luid": "wb-01", "project": "Exec"},
        ]
    }
    survey_path = _write_json(tmp_path / "survey.json", survey_data)
    ctx = rus.StatusContext(repo_root=tmp_path, migrations_root=tmp_path / "migrations")
    rollup = rus.build_status_rollup(survey_path, ctx)

    md = rus.render_markdown_report(rollup)

    # Header and ceiling statement check
    assert "# Run Unit Status: standalone" in md
    assert rus.CEILING_STATEMENT in md

    # Summary table checks
    assert f"| {rus.STATUS_LABELS[rus.STATUS_NOT_STARTED]} | 2 |" in md
    assert f"| {rus.STATUS_LABELS[rus.STATUS_SHIPPED_VERIFIED]} | 0 |" in md

    # Parse markdown table back and verify strict 1:1 match against JSON rollup units
    parsed_rows = _parse_markdown_table(md)
    assert len(parsed_rows) == len(rollup["units"])

    for p_row, j_unit in zip(parsed_rows, rollup["units"], strict=True):
        assert int(p_row["order"]) == j_unit["order"]
        assert p_row["kind"] == j_unit["kind"]
        assert p_row["name"] == j_unit["name"]
        assert p_row["slug"] == j_unit["slug"]
        assert p_row["luid"] == (j_unit["luid"] or "—")
        assert p_row["project"] == (j_unit["project"] or "—")
        assert p_row["status_label"] == rus.STATUS_LABELS[j_unit["status"]]
        assert p_row["recorded_at"] == (j_unit["recorded_at"] or "—")
        assert p_row["deliverable"] == (j_unit["deliverable_path"] or "not-started")
        assert p_row["evidence"] == j_unit["evidence"]


# ==============================================================================
# 5. CLI Execution & Nonzero Exit Code Contracts (Item 5)
# ==============================================================================


def test_cli_exit_0_on_clean_assessed_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "_runs" / "042-sample-estate"
    _write_json(
        run_dir / "assessment" / "estate_survey.json",
        {"fetch_order": [{"kind": "workbook", "name": "Finance Summary"}]},
    )

    # Create deliverable with verified promotion record & fabric dir
    deliv_dir = tmp_path / "migrations" / "workbooks" / "finance-summary"
    _create_dummy_fabric(deliv_dir)
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

    json_out = json.loads(res.stdout)
    assert json_out["run"] == "042-sample-estate"
    assert json_out["summary"]["total_units"] == 1
    assert json_out["units"][0]["status"] == rus.STATUS_SHIPPED_VERIFIED

    # Check file on disk matches stdout
    file_json = json.loads(out_json.read_text(encoding="utf-8"))
    assert file_json == json_out


def test_cli_exit_3_when_cannot_assess_rows_present_and_writes_diagnostics(tmp_path: Path) -> None:
    run_dir = tmp_path / "_runs" / "043-unverified-estate"
    _write_json(
        run_dir / "assessment" / "estate_survey.json",
        {"fetch_order": [{"kind": "workbook", "name": "Unverified Presence WB"}]},
    )

    # Deliverable has directory but NO promotion-record.json -> cannot_assess
    deliv_dir = tmp_path / "migrations" / "workbooks" / "unverified-presence-wb"
    _create_dummy_fabric(deliv_dir)

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

    # Exit code MUST be 3
    assert res.returncode == 3
    # Outputs MUST still be written
    assert out_md.is_file()
    assert out_json.is_file()

    json_out = json.loads(res.stdout)
    assert json_out["summary"]["by_status"][rus.STATUS_CANNOT_ASSESS] == 1
    assert json_out["units"][0]["status"] == rus.STATUS_CANNOT_ASSESS


def test_cli_exit_codes_usage_and_bad_inputs(tmp_path: Path) -> None:
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

    # Exit code 3: survey file is empty dict {}
    empty_survey = _write_json(tmp_path / "empty_survey.json", {})
    res_empty_survey = subprocess.run(
        [sys.executable, str(SCRIPT), "--survey", str(empty_survey)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_empty_survey.returncode == 3
