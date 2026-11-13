"""Offline controls for the frozen doctor/project-start/read-only-status pilot."""

from __future__ import annotations

import gettext
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import t2p  # noqa: E402
import tableau_env  # noqa: E402
import work_dirs  # noqa: E402
from test_start_migration import ENV, _Pipeline, _captured  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_tableau_environment(monkeypatch):
    """Never inherit workstation credentials in any offline control."""
    for key in tableau_env.ACCEPTED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("COPILOT_AGENT_SESSION_ID", raising=False)


def _config(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)


def _snapshot(root):
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def _json(capsys):
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
    return result, captured


def _preflight_bytes(*blockers, warnings=0):
    return json.dumps(
        {"critical_missing": len(blockers), "recommended_warnings": warnings, "blockers": list(blockers)}
    ).encode()


def _status_observed():
    return {
        "run_location": {"state": "intact"},
        "run_recorded_status": "active",
        "inventory_scope": "established",
        "canonical_subdirs": {
            "assessment": "present",
            "assets": "present",
            "bundle": "present",
            "oracle": "missing",
            "packages": "present",
            "deliverables": "missing",
            "scratch": "present",
        },
        "child_evidence": {
            "bundle/pbip": "present",
            "bundle/handover": "present",
            "bundle/source-provenance.json": "recorded",
            "bundle/phase-timings.json": "recorded",
        },
        "findings": [
            {
                "code": "UNSCOPED_PACKAGE",
                "reason": "preserve_and_inspect_association",
                "unassessable": False,
                "unit": "PRIVATE_FINDING_CANARY_84",
                "source": "packages/PRIVATE_FINDING_CANARY_84",
            },
            {
                "code": "UNSCOPED_HANDOVER",
                "reason": "preserve_and_inspect_identity",
                "unassessable": False,
                "unit": "PRIVATE_HANDOVER_CANARY",
                "source": "bundle/handover/PRIVATE_HANDOVER_CANARY",
            },
        ],
        "units": [
            {
                "unit": "PRIVATE_UNIT_CANARY_73",
                "kind": "workbook",
                "scope": "scoped",
                "generated_working_copy": "missing",
                "handover": "present",
                "occurrences": [{"kind": "workbook", "status": "observed", "source": "PRIVATE_OCCURRENCE_CANARY"}],
                "package": {
                    "unit": "PRIVATE_PACKAGE_CANARY_95",
                    "relative_path": "packages/PRIVATE_PACKAGE_CANARY_95",
                    "construction_status": "ASSEMBLED",
                    "binding": {"status": "BOUND"},
                    "stored_readiness": [{"status": "START_READY"}, {"status": "COMPLETE"}],
                },
                "current_certification": "NOT_CHECKED",
                "id": "u0001",
                "name": "Sales Overview",
                "stage": "packaged",
                "working_package": "packages/sales_overview/fabric",
            },
            {
                "unit": "PRIVATE_DATASOURCE_CANARY",
                "kind": "datasource",
                "scope": "working_copy_only",
                "generated_working_copy": "present",
                "handover": "not_applicable",
                "occurrences": [
                    {"kind": "datasource", "status": "observed"},
                    {"kind": "datasource", "status": "observed"},
                    {"kind": "datasource", "status": "present"},
                ],
                "package": None,
                "current_certification": "NOT_CHECKED",
                "id": None,
                "name": "Orders",
                "stage": "converted",
                "working_package": "not-started",
            },
        ],
        "scope_denominator": {"status": "established", "counts": {"workbooks": 1, "required_datasources": 1}},
        "unscoped_packages": [{"unit": "PRIVATE_UNSCOPED_CANARY", "relative_path": "packages/PRIVATE_UNSCOPED_CANARY"}],
        "recorded_phases": [
            {
                "source": "bundle/source-provenance.json",
                "phase": "provenance",
                "last_observed": {
                    "status": "failed",
                    "exit_code": 1,
                    "elapsed_sec": 0.25,
                    "started_at": "2026-10-19T08:00:00+00:00",
                    "errors": [
                        {"code": "collect-inputs-failed", "operation": "collect-inputs"},
                        {"code": "unknown", "operation": "unknown"},
                    ],
                },
                "failed": True,
            },
            {
                "source": "bundle/phase-timings.json#phases[0]",
                "phase": "path_ceiling",
                "last_observed": {
                    "status": "ok",
                    "exit_code": 0,
                    "elapsed_sec": 0.5,
                    "started_at": "2026-10-19T08:00:01+00:00",
                },
                "failed": False,
            },
            {"source": "bundle/phase-timings.json#phases[1]", "last_observed": {}, "failed": False},
        ],
        "locations": [{"path": r"C:\PRIVATE_HOST_CANARY\PRIVATE_TOOLKIT_CANARY"}],
        "next_action": {
            "kind": "command",
            "command": 'python scripts/set_data_folder.py --package "C:\\synthetic\\_runs\\001-demo\\packages\\sales"',
            "headline": "Bind the package data folder.",
            "owner": "set_data_folder.py",
            "gate": "data_binding",
            "affected_units": ["sales_overview"],
            "details": ["PRIVATE_PROSE_CANARY"],
        },
        "path_budget": {
            "runs_parent": "C:\\synthetic\\_runs",
            "root_kind": "external",
            "unknown": 0,
            "file": {"ceiling": 259, "longest": 120, "path": "packages/sales/fabric/x.tmdl", "remaining": 139},
            "directory": {"ceiling": 247, "longest": 100, "path": "packages/sales/fabric", "remaining": 147},
            "rerun": None,
        },
        "path_report": {"projection_verdict": "PASS", "remaining_file_utf16": 999},
    }


# Complete stdout bytes captured from approved base 5af09020 with these synthetic owner records;
# status cases regenerated for #843 (additive t2p/v1 fields: unit columns, next, path budget).
STATUS_FROZEN_JSON = {
    "status0": (
        b'{"artifacts": [], "authority": "run_status.py", "checks": [], "command": "status", "dele'
        b'gated_exit_code": 0, "evidence": [], "exit_code": 0, "limitations": ["last_observed_not_'
        b'liveness", "customer_labels_local_only"], "next": {"affected_units": ["sales_overview"],'
        b' "command": "python scripts/set_data_folder.py --package \\"C:\\\\synthetic\\\\_runs\\\\001-dem'
        b'o\\\\packages\\\\sales\\"", "gate": "data_binding", "headline": "Bind the package data folder'
        b'.", "kind": "command", "owner": "set_data_folder.py"}, "observations": {"canonical_subdi'
        b'rs": {"assessment": "present", "assets": "present", "bundle": "present", "deliverables":'
        b' "missing", "oracle": "missing", "packages": "present", "scratch": "present"}, "child_ev'
        b'idence": {"bundle/handover": "present", "bundle/pbip": "present", "bundle/phase-timings.'
        b'json": "recorded", "bundle/source-provenance.json": "recorded"}, "findings": [], "invent'
        b'ory_scope": "established", "recorded_phases": [{"failed": true, "last_observed": {"elaps'
        b'ed_sec": 0.25, "errors": [{"code": "collect-inputs-failed", "operation": "collect-inputs'
        b'"}, {"code": "unknown", "operation": "unknown"}], "exit_code": 1, "started_at": "2026-10'
        b'-19T08:00:00+00:00", "status": "failed"}, "phase": "provenance", "source": "bundle/sourc'
        b'e-provenance.json"}, {"failed": false, "last_observed": {"elapsed_sec": 0.5, "exit_code"'
        b': 0, "started_at": "2026-10-19T08:00:01+00:00", "status": "ok"}, "phase": "path_ceiling"'
        b', "source": "bundle/phase-timings.json#phases[0]"}, {"failed": false, "last_observed": {'
        b'}, "phase": null, "source": "bundle/phase-timings.json#phases[1]"}], "run_recorded_statu'
        b's": "active", "scope_denominator": {"counts": {"required_datasources": 1, "workbooks": 1'
        b'}, "status": "established"}, "units": [{"current_certification": "NOT_CHECKED", "generat'
        b'ed_working_copy": "missing", "handover": "present", "id": "u0001", "kind": "workbook", "'
        b'name": "Sales Overview", "occurrences": [{"kind": "workbook", "status": "observed"}], "o'
        b'rdinal": 0, "package_count": 1, "scope": "scoped", "stage": "packaged", "working_package'
        b'": "packages/sales_overview/fabric"}, {"current_certification": "NOT_CHECKED", "generate'
        b'd_working_copy": "present", "handover": "not_applicable", "id": null, "kind": "datasourc'
        b'e", "name": "Orders", "occurrences": [{"kind": "datasource", "status": "observed"}, {"ki'
        b'nd": "datasource", "status": "observed"}, {"kind": "datasource", "status": "present"}], '
        b'"ordinal": 1, "package_count": 0, "scope": "working_copy_only", "stage": "converted", "w'
        b'orking_package": "not-started"}], "unscoped_package_count": 1}, "path_report": {"directo'
        b'ry_ceiling_utf16": 247, "file_ceiling_utf16": 259, "initial_decision": "cannot_establish'
        b'", "initial_reason": "unknown_until_harvest", "longest_directory_locator": {"reason": nu'
        b'll, "value": "packages/sales/fabric"}, "longest_directory_utf16": {"reason": null, "valu'
        b'e": 100}, "longest_file_locator": {"reason": null, "value": "packages/sales/fabric/x.tmd'
        b'l"}, "longest_file_utf16": {"reason": null, "value": 120}, "owner": "run_estate.py", "pr'
        b'ojection_reason": "not_recorded", "projection_verdict": null, "remaining_directory_utf16'
        b'": {"reason": null, "value": 147}, "remaining_file_utf16": {"reason": null, "value": 139'
        b'}, "rerun": {"argv": null, "authoritative": false, "command": null, "reason": "within_ce'
        b'ilings"}, "root_kind": "external", "runs_parent": "C:\\\\synthetic\\\\_runs", "subst": {"rea'
        b'son": "not_derivable_read_only", "value": null}, "unknown_paths": 0}, "proof_boundary": '
        b'{"established": ["read_only_inventory"], "not_established": ["credential_validity", "sou'
        b'rce_reachability", "path_safety", "process_liveness", "START_READY", "fidelity", "COMPLE'
        b'TE", "deployability"]}, "run_location": {"commit_safety": null, "commit_safety_reason": '
        b'"not_derivable_read_only", "git_visibility": null, "git_visibility_reason": "not_derivab'
        b'le_read_only", "physical_run": "C:\\\\synthetic\\\\_runs\\\\001-demo", "recorded_run": "C:\\\\sy'
        b'nthetic\\\\_runs\\\\001-demo", "relationship": null, "relationship_reason": "not_derivable_r'
        b'ead_only", "selected_run": "C:\\\\synthetic\\\\_runs\\\\001-demo"}, "run_location_reason": nul'
        b'l, "runs_root": null, "schema_version": "t2p/v1", "stages": [], "status": "ok"}\n'
    ),
    "findings0": (
        b'{"artifacts": [], "authority": "run_status.py", "checks": [], "command": "status", "dele'
        b'gated_exit_code": 0, "evidence": [], "exit_code": 0, "limitations": ["last_observed_not_'
        b'liveness", "customer_labels_local_only"], "next": {"affected_units": ["sales_overview"],'
        b' "command": "python scripts/set_data_folder.py --package \\"C:\\\\synthetic\\\\_runs\\\\001-dem'
        b'o\\\\packages\\\\sales\\"", "gate": "data_binding", "headline": "Bind the package data folder'
        b'.", "kind": "command", "owner": "set_data_folder.py"}, "observations": {"canonical_subdi'
        b'rs": {"assessment": "present", "assets": "present", "bundle": "present", "deliverables":'
        b' "missing", "oracle": "missing", "packages": "present", "scratch": "present"}, "child_ev'
        b'idence": {"bundle/handover": "present", "bundle/pbip": "present", "bundle/phase-timings.'
        b'json": "recorded", "bundle/source-provenance.json": "recorded"}, "findings": [{"code": "'
        b'UNSCOPED_PACKAGE", "reason": "preserve_and_inspect_association", "unassessable": false},'
        b' {"code": "UNSCOPED_HANDOVER", "reason": "preserve_and_inspect_identity", "unassessable"'
        b': false}], "inventory_scope": "established", "recorded_phases": [{"failed": true, "last_'
        b'observed": {"elapsed_sec": 0.25, "errors": [{"code": "collect-inputs-failed", "operation'
        b'": "collect-inputs"}, {"code": "unknown", "operation": "unknown"}], "exit_code": 1, "sta'
        b'rted_at": "2026-10-19T08:00:00+00:00", "status": "failed"}, "phase": "provenance", "sour'
        b'ce": "bundle/source-provenance.json"}, {"failed": false, "last_observed": {"elapsed_sec"'
        b': 0.5, "exit_code": 0, "started_at": "2026-10-19T08:00:01+00:00", "status": "ok"}, "phas'
        b'e": "path_ceiling", "source": "bundle/phase-timings.json#phases[0]"}, {"failed": false, '
        b'"last_observed": {}, "phase": null, "source": "bundle/phase-timings.json#phases[1]"}], "'
        b'run_recorded_status": "active", "scope_denominator": {"counts": {"required_datasources":'
        b' 1, "workbooks": 1}, "status": "established"}, "units": [{"current_certification": "NOT_'
        b'CHECKED", "generated_working_copy": "missing", "handover": "present", "id": "u0001", "ki'
        b'nd": "workbook", "name": "Sales Overview", "occurrences": [{"kind": "workbook", "status"'
        b': "observed"}], "ordinal": 0, "package_count": 1, "scope": "scoped", "stage": "packaged"'
        b', "working_package": "packages/sales_overview/fabric"}, {"current_certification": "NOT_C'
        b'HECKED", "generated_working_copy": "present", "handover": "not_applicable", "id": null, '
        b'"kind": "datasource", "name": "Orders", "occurrences": [{"kind": "datasource", "status":'
        b' "observed"}, {"kind": "datasource", "status": "observed"}, {"kind": "datasource", "stat'
        b'us": "present"}], "ordinal": 1, "package_count": 0, "scope": "working_copy_only", "stage'
        b'": "converted", "working_package": "not-started"}], "unscoped_package_count": 1}, "path_'
        b'report": {"directory_ceiling_utf16": 247, "file_ceiling_utf16": 259, "initial_decision":'
        b' "cannot_establish", "initial_reason": "unknown_until_harvest", "longest_directory_locat'
        b'or": {"reason": null, "value": "packages/sales/fabric"}, "longest_directory_utf16": {"re'
        b'ason": null, "value": 100}, "longest_file_locator": {"reason": null, "value": "packages/'
        b'sales/fabric/x.tmdl"}, "longest_file_utf16": {"reason": null, "value": 120}, "owner": "r'
        b'un_estate.py", "projection_reason": "not_recorded", "projection_verdict": null, "remaini'
        b'ng_directory_utf16": {"reason": null, "value": 147}, "remaining_file_utf16": {"reason": '
        b'null, "value": 139}, "rerun": {"argv": null, "authoritative": false, "command": null, "r'
        b'eason": "within_ceilings"}, "root_kind": "external", "runs_parent": "C:\\\\synthetic\\\\_run'
        b's", "subst": {"reason": "not_derivable_read_only", "value": null}, "unknown_paths": 0}, '
        b'"proof_boundary": {"established": ["read_only_inventory"], "not_established": ["credenti'
        b'al_validity", "source_reachability", "path_safety", "process_liveness", "START_READY", "'
        b'fidelity", "COMPLETE", "deployability"]}, "run_location": {"commit_safety": null, "commi'
        b't_safety_reason": "not_derivable_read_only", "git_visibility": null, "git_visibility_rea'
        b'son": "not_derivable_read_only", "physical_run": "C:\\\\synthetic\\\\_runs\\\\001-demo", "reco'
        b'rded_run": "C:\\\\synthetic\\\\_runs\\\\001-demo", "relationship": null, "relationship_reason"'
        b': "not_derivable_read_only", "selected_run": "C:\\\\synthetic\\\\_runs\\\\001-demo"}, "run_loc'
        b'ation_reason": null, "runs_root": null, "schema_version": "t2p/v1", "stages": [], "statu'
        b's": "findings"}\n'
    ),
    "native1": (
        b'{"artifacts": [], "authority": "run_status.py", "checks": [], "command": "status", "dele'
        b'gated_exit_code": 1, "evidence": [], "exit_code": 1, "limitations": ["last_observed_not_'
        b'liveness", "customer_labels_local_only"], "next": null, "observations": {"canonical_subd'
        b'irs": null, "child_evidence": null, "findings": [{"code": "UNASSESSABLE_EVIDENCE", "reas'
        b'on": "run_location_unestablished", "unassessable": true}], "inventory_scope": null, "rec'
        b'orded_phases": [], "run_recorded_status": null, "scope_denominator": null, "units": [], '
        b'"unscoped_package_count": 0}, "path_report": {"directory_ceiling_utf16": 247, "file_ceil'
        b'ing_utf16": 259, "initial_decision": "cannot_establish", "initial_reason": "unknown_unti'
        b'l_harvest", "longest_directory_locator": {"reason": "not_recorded", "value": null}, "lon'
        b'gest_directory_utf16": {"reason": "not_recorded", "value": null}, "longest_file_locator"'
        b': {"reason": "not_recorded", "value": null}, "longest_file_utf16": {"reason": "not_recor'
        b'ded", "value": null}, "owner": "run_estate.py", "projection_reason": "not_recorded", "pr'
        b'ojection_verdict": null, "remaining_directory_utf16": {"reason": "not_recorded", "value"'
        b': null}, "remaining_file_utf16": {"reason": "not_recorded", "value": null}, "rerun": {"a'
        b'rgv": null, "authoritative": false, "reason": "arguments_not_recorded"}, "subst": {"reas'
        b'on": "not_derivable_read_only", "value": null}}, "proof_boundary": {"established": ["rea'
        b'd_only_inventory"], "not_established": ["credential_validity", "source_reachability", "p'
        b'ath_safety", "process_liveness", "START_READY", "fidelity", "COMPLETE", "deployability"]'
        b'}, "run_location": null, "run_location_reason": "identity_unestablished", "runs_root": n'
        b'ull, "schema_version": "t2p/v1", "stages": [], "status": "cannot_establish"}\n'
    ),
    "native2": (
        b'{"artifacts": [], "authority": "run_status.py", "checks": [], "command": "status", "dele'
        b'gated_exit_code": 2, "evidence": [], "exit_code": 2, "limitations": ["last_observed_not_'
        b'liveness", "customer_labels_local_only"], "next": null, "observations": {"canonical_subd'
        b'irs": null, "child_evidence": null, "findings": [{"code": "UNASSESSABLE_EVIDENCE", "reas'
        b'on": "run_location_unestablished", "unassessable": true}], "inventory_scope": null, "rec'
        b'orded_phases": [], "run_recorded_status": null, "scope_denominator": null, "units": [], '
        b'"unscoped_package_count": 0}, "path_report": {"directory_ceiling_utf16": 247, "file_ceil'
        b'ing_utf16": 259, "initial_decision": "cannot_establish", "initial_reason": "unknown_unti'
        b'l_harvest", "longest_directory_locator": {"reason": "not_recorded", "value": null}, "lon'
        b'gest_directory_utf16": {"reason": "not_recorded", "value": null}, "longest_file_locator"'
        b': {"reason": "not_recorded", "value": null}, "longest_file_utf16": {"reason": "not_recor'
        b'ded", "value": null}, "owner": "run_estate.py", "projection_reason": "not_recorded", "pr'
        b'ojection_verdict": null, "remaining_directory_utf16": {"reason": "not_recorded", "value"'
        b': null}, "remaining_file_utf16": {"reason": "not_recorded", "value": null}, "rerun": {"a'
        b'rgv": null, "authoritative": false, "reason": "arguments_not_recorded"}, "subst": {"reas'
        b'on": "not_derivable_read_only", "value": null}}, "proof_boundary": {"established": ["rea'
        b'd_only_inventory"], "not_established": ["credential_validity", "source_reachability", "p'
        b'ath_safety", "process_liveness", "START_READY", "fidelity", "COMPLETE", "deployability"]'
        b'}, "run_location": null, "run_location_reason": "identity_unestablished", "runs_root": n'
        b'ull, "schema_version": "t2p/v1", "stages": [], "status": "cannot_establish"}\n'
    ),
    "syntax": (
        b'{"artifacts": [], "authority": null, "checks": [], "command": "status", "delegated_exit_'
        b'code": null, "evidence": [], "exit_code": 2, "limitations": ["invalid_arguments_use_help'
        b'"], "path_report": {"directory_ceiling_utf16": 247, "file_ceiling_utf16": 259, "initial_'
        b'decision": "cannot_establish", "initial_reason": "unknown_until_harvest", "longest_direc'
        b'tory_locator": {"reason": "not_recorded", "value": null}, "longest_directory_utf16": {"r'
        b'eason": "not_recorded", "value": null}, "longest_file_locator": {"reason": "not_recorded'
        b'", "value": null}, "longest_file_utf16": {"reason": "not_recorded", "value": null}, "own'
        b'er": "run_estate.py", "projection_reason": "not_recorded", "projection_verdict": null, "'
        b'remaining_directory_utf16": {"reason": "not_recorded", "value": null}, "remaining_file_u'
        b'tf16": {"reason": "not_recorded", "value": null}, "rerun": {"argv": null, "authoritative'
        b'": false, "reason": "arguments_not_recorded"}, "subst": {"reason": "not_derivable_read_o'
        b'nly", "value": null}}, "proof_boundary": {"established": [], "not_established": ["creden'
        b'tial_validity", "source_reachability", "path_safety", "process_liveness", "START_READY",'
        b' "fidelity", "COMPLETE", "deployability"]}, "run_location": null, "run_location_reason":'
        b' "not_allocated_path_withheld", "runs_root": null, "schema_version": "t2p/v1", "stages":'
        b' [], "status": "error"}\n'
    ),
    "exception": (
        b'{"artifacts": [], "authority": "run_status.py", "checks": [], "command": "status", "dele'
        b'gated_exit_code": null, "evidence": [], "exit_code": 1, "limitations": ["owner_result_un'
        b'available"], "path_report": {"directory_ceiling_utf16": 247, "file_ceiling_utf16": 259, '
        b'"initial_decision": "cannot_establish", "initial_reason": "unknown_until_harvest", "long'
        b'est_directory_locator": {"reason": "not_recorded", "value": null}, "longest_directory_ut'
        b'f16": {"reason": "not_recorded", "value": null}, "longest_file_locator": {"reason": "not'
        b'_recorded", "value": null}, "longest_file_utf16": {"reason": "not_recorded", "value": nu'
        b'll}, "owner": "run_estate.py", "projection_reason": "not_recorded", "projection_verdict"'
        b': null, "remaining_directory_utf16": {"reason": "not_recorded", "value": null}, "remaini'
        b'ng_file_utf16": {"reason": "not_recorded", "value": null}, "rerun": {"argv": null, "auth'
        b'oritative": false, "reason": "arguments_not_recorded"}, "subst": {"reason": "not_derivab'
        b'le_read_only", "value": null}}, "proof_boundary": {"established": [], "not_established":'
        b' ["credential_validity", "source_reachability", "path_safety", "process_liveness", "STAR'
        b'T_READY", "fidelity", "COMPLETE", "deployability"]}, "run_location": null, "run_location'
        b'_reason": "not_allocated_path_withheld", "runs_root": null, "schema_version": "t2p/v1", '
        b'"stages": [], "status": "error"}\n'
    ),
    "interrupt": (
        b'{"artifacts": [], "authority": "run_status.py", "checks": [], "command": "status", "dele'
        b'gated_exit_code": null, "evidence": [], "exit_code": 130, "limitations": [], "path_repor'
        b't": {"directory_ceiling_utf16": 247, "file_ceiling_utf16": 259, "initial_decision": "can'
        b'not_establish", "initial_reason": "unknown_until_harvest", "longest_directory_locator": '
        b'{"reason": "not_recorded", "value": null}, "longest_directory_utf16": {"reason": "not_re'
        b'corded", "value": null}, "longest_file_locator": {"reason": "not_recorded", "value": nul'
        b'l}, "longest_file_utf16": {"reason": "not_recorded", "value": null}, "owner": "run_estat'
        b'e.py", "projection_reason": "not_recorded", "projection_verdict": null, "remaining_direc'
        b'tory_utf16": {"reason": "not_recorded", "value": null}, "remaining_file_utf16": {"reason'
        b'": "not_recorded", "value": null}, "rerun": {"argv": null, "authoritative": false, "reas'
        b'on": "arguments_not_recorded"}, "subst": {"reason": "not_derivable_read_only", "value": '
        b'null}}, "proof_boundary": {"established": [], "not_established": ["credential_validity",'
        b' "source_reachability", "path_safety", "process_liveness", "START_READY", "fidelity", "C'
        b'OMPLETE", "deployability"]}, "run_location": null, "run_location_reason": "not_allocated'
        b'_path_withheld", "runs_root": null, "schema_version": "t2p/v1", "stages": [], "status": '
        b'"interrupted"}\n'
    ),
}


ADOMD_BLOCKER = {
    "id": "adomd_client",
    "source": None,
    "reason": "pythonnet_missing",
    "remedy": "restore_adomd",
}


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("preflight_code,expected", [(0, 0), (1, 1), (7, 3), (None, 3)])
def test_doctor_private_presence_only(monkeypatch, tmp_path, capsys, json_mode, preflight_code, expected):
    env_file = tmp_path / "selected.env"
    values = {key: f"SYNTHETIC_PRIVATE_{key}_a9" for key in tableau_env.ACCEPTED_ENV_KEYS}
    values["TABLEAU_SERVER"] = values["TABLEAU_SERVER_URL"]
    values["TABLEAU_PAT_VALUE"] = values["TABLEAU_PAT_SECRET"]
    env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
    raw = "RAW_PREFLIGHT_PRIVATE_CANARY"
    calls = []

    def child(command, **options):
        calls.append((command, options))
        assert options["env"]["PYTHONDONTWRITEBYTECODE"] == "1", "doctor child bytecode must be disabled"
        if preflight_code is None:
            raise OSError(raw)
        return subprocess.CompletedProcess(command, preflight_code, raw.encode(), raw.encode())

    monkeypatch.setattr(t2p.subprocess, "run", child)
    before = _snapshot(tmp_path)
    code = t2p.main(["doctor", "--env", str(env_file), *(["--json"] if json_mode else [])])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code == expected
    for value in [*values.values(), raw, str(env_file), str(t2p.ROOT)]:
        assert value not in output, "doctor must not disclose values or host paths"
    for value in values.values():
        assert hashlib.sha256(value.encode()).hexdigest() not in output, "doctor must not disclose value digests"
    assert _snapshot(tmp_path) == before, "doctor must not mutate its configuration/tree"
    assert len(calls) == 1
    command, options = calls[0]
    assert command == [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(t2p.ROOT / "scripts" / "preflight.ps1"),
        "-DoctorDiagnostics",
    ], "doctor must delegate plain preflight with only the internal presentation switch"
    assert options["stdout"] == options["stderr"] == subprocess.PIPE
    assert captured.err == "Checking existing preflight; no upgrades or Tableau login.\n"
    if json_mode:
        result = json.loads(captured.out)
        assert result["limitations"] == ["presence_only", "presence_only_not_authentication"], (
            "doctor value/length/digest leak assertion"
        )
        assert all(
            set(row) == {"key", "state", "source", "required", "secret", "reason", "remedy"} for row in result["checks"]
        ), "doctor value/length/digest leak assertion"
        jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
        details = result["doctor_details"]
        assert set(details) == {"configuration_aliases", "preflight"}
        assert set(details["preflight"]) == {
            "availability",
            "reason",
            "critical_missing",
            "recommended_warnings",
            "blockers",
        }
        assert all(set(group) == {"key", "winning_key", "sources"} for group in details["configuration_aliases"])
        assert all(
            set(origin) == {"source", "keys", "conflict"}
            for group in details["configuration_aliases"]
            for origin in group["sources"]
        ), "doctor detail has closed value-free fields, never derivatives"
        assert result["delegated_exit_code"] is None
        assert result["run_location"] is result["path_report"] is None
        assert result["stages"][0]["native_exit_code"] == preflight_code
        assert len(result["checks"]) == len(tableau_env.CANONICAL_ENV_KEYS) + len(
            tableau_env.DATASOURCE_CREDENTIAL_KEYS
        )
        assert len(captured.out.splitlines()) == 1, "JSON stdout must be one object and newline"
    else:
        assert captured.out.startswith("DOCTOR "), "human doctor must render a verdict, not the JSON envelope"
        assert "(exit " + str(expected) + ")" in captured.out
        assert "Recommended-warning count: unavailable." in captured.out
        assert "Rerun doctor: python -B scripts\\t2p.py doctor --env <same-selected-env>" in captured.out
        assert "No run, login or repair command was started." in captured.out


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("good", 0),
        ("missing", 1),
        ("blank", 1),
        ("conflict", 1),
        ("malformed", 3),
        ("missing_file", 3),
    ],
)
def test_doctor_configuration_results(monkeypatch, tmp_path, capsys, mode, expected):
    _config(monkeypatch)
    path = tmp_path / "selected.env"
    path.write_text("# empty\n", encoding="utf-8")
    if mode == "missing":
        monkeypatch.delenv("TABLEAU_PAT_NAME")
    if mode == "blank":
        monkeypatch.setenv("TABLEAU_PAT_SECRET", " \t ")
    if mode == "conflict":
        monkeypatch.setenv("TABLEAU_PAT_VALUE", "different-secret")
    if mode == "malformed":
        path.write_text("PRIVATE_INVALID_LINE", encoding="utf-8")
    if mode == "missing_file":
        path.unlink()
    monkeypatch.setattr(t2p.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    assert t2p.main(["doctor", "--env", str(path), "--json"]) == expected
    result, _ = _json(capsys)
    assert result["status"] == {0: "READY_TO_ATTEMPT", 1: "BLOCKED_CONFIG", 3: "CANNOT_ESTABLISH"}[expected]


def test_doctor_child_cannot_create_pycache(monkeypatch, tmp_path, capsys):
    """Exercise a real Python grandchild through the plain-preflight invocation boundary."""
    _config(monkeypatch)
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    root = tmp_path / "clean"
    root.mkdir()
    (root / "witness.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_run = subprocess.run
    calls = []

    def preflight(command, **options):
        calls.append(command)
        return real_run([sys.executable, "-c", "import witness"], **{**options, "cwd": root})

    monkeypatch.setattr(t2p.subprocess, "run", preflight)
    monkeypatch.setattr(t2p, "ROOT", root)
    before = _snapshot(root)
    assert t2p.main(["doctor", "--json"]) == 0
    _json(capsys)
    assert calls[0][0] == "powershell"
    assert _snapshot(root) == before, "doctor clean-tree/no-pyc assertion"
    assert not list(root.rglob("*.pyc")), "doctor clean-tree/no-pyc assertion"


@pytest.mark.parametrize(
    "argv",
    [
        ["next"],
        ["start"],
        ["start", "--project", " "],
        ["start", "--proj", "name"],
        ["start", "--project", "name", "--workbook", "PRIVATE_ARGV_CANARY"],
        ["assess", "--project", "name"],
        ["assess", "--storage-decision", "policy.json"],
        ["doctor", "--show-values"],
        ["status"],
        ["status", "--run", "x", "--repair"],
    ],
)
def test_closed_surface_and_safe_syntax(argv, capsys):
    assert t2p.main([*argv, "--json"]) == 2
    result, captured = _json(capsys)
    assert result["status"] == "error"
    assert result["delegated_exit_code"] is None
    assert "PRIVATE_ARGV_CANARY" not in captured.out + captured.err


@pytest.mark.parametrize("name", [None, "doctor", "start", "assess", "status"])
def test_help_cp1252(name):
    result = subprocess.run(
        [sys.executable, "-B", str(REPO_ROOT / "scripts" / "t2p.py"), *([name] if name else []), "--help"],
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert b"--json" in result.stdout or name is None
    assert b"--next" not in result.stdout


@pytest.mark.parametrize(
    "native,wrapper,options",
    [
        ({}, 0, {}),
        ({"run_engine_survey.py": 7}, 1, {}),
        ({"capture_tableau_oracle.py": 4}, 1, {}),
        ({}, 3, {"missing_oracle": True}),
        ({"run_estate.py": 10}, 1, {}),
        ({}, 130, {"interrupt": True}),
    ],
)
def test_start_owner_parity_and_native_stages(monkeypatch, tmp_path, capsys, native, wrapper, options):
    import start_migration

    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, exit_codes=native, **options)
    monkeypatch.setattr(
        start_migration,
        "diagnose_env",
        lambda *a, **k: (
            dict(ENV),
            {"status": "READY_TO_ATTEMPT", "checks": [], "reason": "presence_only"},
        ),
    )
    assert t2p.main(["start", "--project", "Finance", "--json"]) == wrapper
    out, err = _captured(capsys, pipeline)
    result = json.loads(out)
    jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
    assert result["delegated_exit_code"] == wrapper, "preserve wrapper exit, not first native failure"
    stages = {row["stage"]: row for row in result["stages"]}
    expected_names = [
        "run_engine_survey.py",
        "assess_estate.py",
        "harvest_estate_assets.py",
        "capture_tableau_oracle.py",
        "run_estate.py",
    ]
    if "run_engine_survey.py" in native:
        expected_names = expected_names[:1]
    if options.get("interrupt"):
        expected_names = expected_names[:2]
    actual = [next(Path(part).name for part in command if part.endswith(".py")) for command in pipeline.commands]
    assert actual == expected_names, "reference failure must still attempt bundle; earlier failure stops"
    for name, code in native.items():
        stage_name = next(stage for stage, owner in t2p.STAGE_OWNERS.items() if owner == name)
        assert stages[stage_name]["native_exit_code"] == code, "retain per-stage native results"
    assert result["path_report"]["initial_reason"] == "unknown_until_harvest"
    assert result["path_report"]["projection_verdict"] is None, "never invent projection PASS from exit 0"
    assert result["run_location"]["physical_run"] in err
    assert err.splitlines()[0].startswith("RUN NOT_ALLOCATED; runs_root=")
    assert err.splitlines()[-1] == "RUN " + result["run_location"]["physical_run"]
    assert all(
        "--reference-best" in command
        for command in pipeline.commands
        if "capture_tableau_oracle.py" in " ".join(command)
    )
    assert all(options["stdout"] is None and options["stderr"] is None for options in pipeline.options), (
        "stage output must stream without buffering"
    )


def test_assess_facade_is_additive_and_projects_assessment_status(monkeypatch, tmp_path, capsys):
    import start_migration

    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, collector_outcome="available")
    monkeypatch.setattr(
        start_migration,
        "diagnose_env",
        lambda *a, **k: (
            dict(ENV),
            {"status": "READY_TO_ATTEMPT", "checks": [], "reason": "presence_only"},
        ),
    )

    assert t2p.STAGE_OWNERS == {
        "preflight": "preflight.ps1",
        "survey": "run_engine_survey.py",
        "assessment": "assess_estate.py",
        "harvest": "harvest_estate_assets.py",
        "reference": "capture_tableau_oracle.py",
        "bundle": "run_estate.py",
    }
    assert t2p.ASSESS_ONLY_OWNERS == {
        "admin_insights": "admin_insights_collector.py",
        "report": "assessment_report.py",
    }
    assert t2p.main(["assess", "--json"]) == 0
    result, captured = _json(capsys)
    assert result["command"] == "assess"
    assert result["path_report"] is None
    assert "assessment_only_not_migration_readiness" in result["limitations"]
    assert [row["stage"] for row in result["stages"]] == [
        "preflight",
        "survey",
        "assessment",
        "admin_insights",
        "report",
    ]
    assert {row["stage"] for row in result["stages"]}.isdisjoint({"harvest", "reference", "bundle"})
    assert captured.out.count("\n") == 1
    progress = "".join(piece.err for piece in pipeline.before_exit) + captured.err
    assert progress.splitlines()[0].startswith("RUN NOT_ALLOCATED; runs_root=")
    assert progress.splitlines()[-1].startswith("RUN ")
    assert [next(Path(part).name for part in command if part.endswith(".py")) for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
        "admin_insights_collector.py",
        "assessment_report.py",
    ]

    run = result["run_location"]["physical_run"]
    assert t2p.main(["status", "--run", run, "--json"]) == 0
    status, _captured_status = _json(capsys)
    assert status["command"] == "status"
    assert status["observations"]["intent"] == "assessment"
    assert status["observations"]["assessment"]["report"] == "current"
    assert status["observations"]["not_requested"]["bundle"] == "not_requested"
    assert "assessment_only_not_migration_readiness" in status["limitations"]


def test_start_preallocation_block_has_no_path(monkeypatch, capsys):
    import start_migration

    monkeypatch.setattr(start_migration.harvest, "refuse_unignored_output", lambda *a, **k: True)
    monkeypatch.setattr(work_dirs, "allocate_run", lambda *a, **k: pytest.fail("must not allocate"))
    assert t2p.main(["start", "--project", "Finance", "--json"]) == 1
    result, captured = _json(capsys)
    assert result["run_location"] is result["runs_root"] is None
    assert result["run_location_reason"] == "not_allocated_path_withheld"
    assert captured.err.splitlines()[0] == captured.err.splitlines()[-1]


def test_native_descriptor_output_is_live_and_json_stdout_single(tmp_path):
    """A real subprocess emits on fd1/fd2 while still alive, without source/network access."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(REPO_ROOT / "scripts" / "t2p.py", scripts / "t2p.py")
    (scripts / "work_dirs.py").write_text(
        "from pathlib import Path\ndef runs_root(parent=None): return Path(__file__).parent / '_runs'\n",
        encoding="utf-8",
    )
    (scripts / "start_migration.py").write_text(
        "import os,time,subprocess,sys\n"
        "def main(argv, *, observer=None, diagnostic_env=False):\n"
        "    observer({'event':'allocated','run':os.path.dirname(__file__),'output_guard':'passed'})\n"
        "    os.write(1,b'LIVE_FD1\\n'); os.write(2,b'LIVE_FD2\\n')\n"
        "    subprocess.run([sys.executable,'-B','-c',\"import os; os.write(1,b'LIVE_CHILD_FD1\\\\n'); os.write(2,b'LIVE_CHILD_FD2\\\\n')\"],check=True)\n"
        "    time.sleep(2)\n"
        "    observer({'event':'stage','stage':'bundle','native_exit_code':10,'state':'failed','artifact':'bundle/report.json'})\n"
        "    return 1\n",
        encoding="utf-8",
    )
    with subprocess.Popen(
        [sys.executable, "-B", str(scripts / "t2p.py"), "start", "--project", "Fixture", "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stderr], daemon=True)
        reader.start()
        progress = []
        while not any("LIVE_CHILD_FD2" in line for line in progress):
            progress.append(lines.get(timeout=10))
        assert process.poll() is None, "progress must stream before delegated completion"
        assert any("LIVE_FD1" in line for line in progress), "OS stdout must be routed to stderr"
        assert any("LIVE_CHILD_FD1" in line for line in progress), "inherited child stdout must be routed to stderr"
        output = process.stdout.read()
        assert process.wait(timeout=15) == 1
        reader.join(timeout=5)
        assert not reader.is_alive()
        while not lines.empty():
            progress.append(lines.get_nowait())
    result = json.loads(output)
    assert result["delegated_exit_code"] == 1
    assert len(output.splitlines()) == 1
    assert progress[0].startswith("RUN NOT_ALLOCATED")
    assert progress[-1].strip() == "RUN " + str(scripts)


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("damage", [None, "missing", "malformed", "moved"])
def test_status_readonly_and_owner_parity(monkeypatch, tmp_path, capsys, damage, json_mode):
    import run_status

    run = work_dirs.allocate_run("synthetic", repo_root=tmp_path)
    path = run.root
    if damage == "missing":
        (path / "run.json").unlink()
    elif damage == "malformed":
        (path / "run.json").write_text("not-json", encoding="utf-8")
    elif damage == "moved":
        moved = path.with_name("009-moved")
        path.rename(moved)
        path = moved
    direct, expected = run_status.build_status(str(path))
    before = _snapshot(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("status must not launch subprocess/git"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("status must not launch subprocess"))
    for _ in range(2):
        assert t2p.main(["status", "--run", str(path), *(["--json"] if json_mode else [])]) == expected
        if json_mode:
            result, _captured_output = _json(capsys)
            assert result["delegated_exit_code"] == expected
            assert len(result["observations"]["findings"]) == len(direct["findings"])
            assert "next_action" not in json.dumps(result), "status exposes the owner's next only as `next`"
            assert result["next"] == {
                key: direct["next_action"][key]
                for key in ("kind", "command", "headline", "owner", "gate", "affected_units")
            }, "status must carry the owner's structured next step"
            assert result["path_report"]["projection_verdict"] is None
            if damage:
                assert result["status"] == "cannot_establish"
                assert result["run_location"] is None
        else:
            _captured_output = capsys.readouterr()
            assert _captured_output.out.startswith("STATUS ")
            assert "next_action" not in _captured_output.out, "status exposes the owner's next only as `next`"
            assert "\nBlocked by: " + direct["next_action"]["gate"] in _captured_output.out
            if damage:
                assert "\nRun: " not in _captured_output.out
        assert _captured_output.err == ""
        assert _snapshot(tmp_path) == before, "status byte/mtime/tree read-only assertion"


def test_status_rejects_relative_without_scan(capsys):
    assert t2p.main(["status", "--run", "relative", "--json"]) == 2
    result, _ = _json(capsys)
    assert result["status"] == "cannot_establish"
    assert result["run_location"] is None


@pytest.mark.parametrize("command", ["doctor", "start", "assess", "status", "refresh"])
def test_explicit_branch_reaches_owner(monkeypatch, capsys, command):
    calls = []

    def owner(_args, result):
        calls.append(command)
        result["status"] = "error"
        return 1

    monkeypatch.setattr(t2p, "start" if command == "assess" else command, owner)
    arguments = {
        "doctor": [],
        "start": ["--project", "Fixture"],
        "assess": [],
        "status": ["--run", "relative"],
        "refresh": ["model.pbip"],
    }
    t2p.main([command, *arguments[command], "--json"])
    _json(capsys)
    assert calls == [command], "explicit command branch must reach its owning operation"


def _fake_refresh_owner(root: Path, code: int) -> None:
    """A stand-in refresh CLI under `root` that echoes its argv and exits `code`."""
    script = root / t2p.REFRESH_OWNER
    script.parent.mkdir(parents=True)
    script.write_text(
        f"import json, sys\nprint('CHILD ' + json.dumps(sys.argv[1:]), flush=True)\nsys.exit({code})\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(("code", "status"), [(0, "ok"), (1, "error"), (2, "error"), (3, "cannot_establish")])
def test_refresh_delegates_the_path_and_flags_and_returns_the_child_verdict(monkeypatch, tmp_path, capfd, code, status):
    _fake_refresh_owner(tmp_path, code)
    monkeypatch.setattr(t2p, "ROOT", tmp_path)
    pbip = tmp_path / "pkg"

    assert t2p.main(["refresh", str(pbip), "--canaries", "Orders", "Sales", "--no-save", "--json"]) == code
    captured = capfd.readouterr()
    result = json.loads(captured.out)
    jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
    assert len(captured.out.splitlines()) == 1, "child output must not reach the JSON stdout"
    forwarded = [str(pbip.resolve()), "--refresh", "--canaries", "Orders", "Sales", "--no-save"]
    assert f"CHILD {json.dumps(forwarded)}" in captured.err
    assert (result["command"], result["status"], result["exit_code"]) == ("refresh", status, code)
    assert (result["authority"], result["delegated_exit_code"]) == ("open_desktop.py --refresh", code)
    assert result["run_location"] is None and result["path_report"] is None


def test_refresh_human_mode_streams_the_child_and_adds_nothing(monkeypatch, tmp_path, capfd):
    _fake_refresh_owner(tmp_path, 0)
    monkeypatch.setattr(t2p, "ROOT", tmp_path)

    assert t2p.main(["refresh", str(tmp_path / "model.pbip")]) == 0
    captured = capfd.readouterr()
    assert captured.out.splitlines() == [f"CHILD {json.dumps([str((tmp_path / 'model.pbip').resolve()), '--refresh'])}"]
    assert captured.err == ""


def test_refresh_without_a_path_is_a_syntax_error_that_starts_nothing(monkeypatch, capsys):
    monkeypatch.setattr(t2p.subprocess, "run", lambda *_a, **_k: pytest.fail("refresh child started"))

    assert t2p.main(["refresh", "--json"]) == 2
    result, _ = _json(capsys)
    assert result["limitations"] == ["invalid_arguments_use_help"]


@pytest.mark.parametrize("root_kind", ["repo", "external"])
@pytest.mark.parametrize("gate", ["ok", "over_ceiling", "cannot_establish"])
@pytest.mark.parametrize("json_mode", [False, True])
def test_start_reaches_existing_postharvest_gate(monkeypatch, tmp_path, capsys, root_kind, gate, json_mode):
    """Join the facade to the real conversion entry point; intercept only external work."""
    import run_estate
    import start_migration

    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    monkeypatch.setattr(
        start_migration,
        "diagnose_env",
        lambda *a, **k: (
            dict(ENV),
            {"status": "READY_TO_ATTEMPT", "checks": [], "reason": "presence_only"},
        ),
    )
    original = start_migration._run_child
    gate_calls = []
    monkeypatch.setattr(run_estate, "resolve_engine", lambda *a, **k: pipeline.root)
    monkeypatch.setattr(
        run_estate,
        "engine_provenance",
        lambda *_a: {
            "root": str(pipeline.root),
            "version": "synthetic",
            "canonical": True,
        },
    )

    original_projection = run_estate.project_estate_path_ceiling

    def projection(*args, **kwargs):
        gate_calls.append("post_harvest")
        assert any("harvest_estate_assets.py" in " ".join(command) for command in pipeline.commands)
        return original_projection(*args, **kwargs)

    monkeypatch.setattr(run_estate, "project_estate_path_ceiling", projection)
    monkeypatch.setattr(run_estate, "_input_candidates", lambda *_a: [Path("synthetic.twb")])
    monkeypatch.setattr(
        run_estate,
        "_engine_unit_names",
        lambda *_a: {
            "ok": ["W"],
            "over_ceiling": ["W" * 300],
            "cannot_establish": None,
        }[gate],
    )
    conversion_calls = []

    def engine(*_args, **_kwargs):
        assert gate == "ok", "non-pass path gate must prevent conversion"
        conversion_calls.append("permitted")
        return 7, "synthetic later engine failure"

    monkeypatch.setattr(run_estate, "run_engine", engine)

    def child(name, index, command, env, output, *, observer=None):
        if name != "bundle":
            return original(name, index, command, env, output)
        with start_migration._observe_path_gate(run_estate, observer):
            code = run_estate.main(command[command.index("--input") :])
        return start_migration.StageResult(name, code, output)

    monkeypatch.setattr(start_migration, "_run_child", child)
    arguments = ["start", "--project", "Finance", *(["--json"] if json_mode else [])]
    if root_kind == "external":
        arguments.extend(["--runs-parent", str(tmp_path / "external")])
    assert t2p.main(arguments) == 1
    out, _err = _captured(capsys, pipeline)
    result = json.loads(out) if json_mode else json.JSONDecoder().raw_decode(out, out.index('{\n  "artifacts"'))[0]
    assert gate_calls == ["post_harvest"], "existing path gate must be reached after harvest"
    expected_native = run_estate.EXIT_ENGINE_FAILED if gate == "ok" else 10
    assert result["stages"][-1]["native_exit_code"] == expected_native, "non-pass path gate must retain native 10"
    assert result["path_report"]["initial_decision"] == "cannot_establish", "unknown must not become PASS"
    report = result["path_report"]
    assert (
        report["projection_verdict"]
        == {
            "ok": "PASS",
            "over_ceiling": "OVER_CEILING",
            "cannot_establish": "CANNOT_ESTABLISH",
        }[gate]
    ), "authoritative owner path verdict must be exposed"
    assert any(row["locator"] == "bundle#pre_conversion_path_projection" for row in result["evidence"])
    assert conversion_calls == (["permitted"] if gate == "ok" else [])
    if gate != "cannot_establish":
        assert isinstance(report["longest_file_utf16"]["value"], int)
        assert report["remaining_file_utf16"]["value"] == 259 - report["longest_file_utf16"]["value"]
    else:
        assert report["longest_file_utf16"] == {"value": None, "reason": "not_recorded"}


@pytest.mark.parametrize("source", ["environment", "selected_env"])
@pytest.mark.parametrize("site", [None, "", " \t "])
def test_doctor_valid_default_and_optional_absence(monkeypatch, tmp_path, capsys, source, site):
    values = {key: value for key, value in ENV.items() if key != "TABLEAU_SITE"}
    if site is not None:
        values["TABLEAU_SITE"] = site
    selected = tmp_path / "selected.env"
    selected.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) if source == "selected_env" else "# empty\n",
        encoding="utf-8",
    )
    if source == "environment":
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(t2p.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    assert t2p.main(["doctor", "--env", str(selected), "--json"]) == 0
    result, _ = _json(capsys)
    row = next(row for row in result["checks"] if row["key"] == "TABLEAU_SITE")
    assert (row["state"], row["reason"], row["remedy"]) == ("set", "default_site", "none"), (
        "valid Default site must not prescribe repair"
    )
    assert row["source"] == ("default" if site is None else source)
    for row in result["checks"]:
        if not row["required"] and row["state"] == "missing":
            assert row["remedy"] == "none", "optional missing keys must not prescribe repair"


def _label_locations(value, path=""):
    """Every (json-path, string) leaf, so a label can be pinned to the fields allowed to carry it."""
    if isinstance(value, dict):
        return [hit for key, item in value.items() for hit in _label_locations(item, f"{path}/{key}")]
    if isinstance(value, list):
        return [hit for index, item in enumerate(value) for hit in _label_locations(item, f"{path}/{index}")]
    return [(path, value)] if isinstance(value, str) else []


@pytest.mark.parametrize("json_mode", [False, True])
def test_status_customer_labels_stay_in_their_fields(monkeypatch, tmp_path, capsys, json_mode):
    """Local names, package locators and the owner's command are shown; nothing else leaks."""
    import run_status

    unit, engine, package = "PRIVATE_UNIT_CANARY_73", "PRIVATE_ENGINE_CANARY_61", "PRIVATE_PACKAGE_CANARY_95"
    env, project, finding = "PRIVATE_ENV_CANARY", "PRIVATE_PROJECT_CANARY", "PRIVATE_FINDING_CANARY_84"
    absent = [
        finding,
        "PRIVATE_TOOLKIT_CANARY",
        "PRIVATE_PROFILE_CANARY",
        "PRIVATE_HOST_CANARY",
        "PRIVATE_SERVER_CANARY",
        "PRIVATE_SITE_CANARY",
        "PRIVATE_PROSE_CANARY",
    ]
    rerun = f'python -B scripts\\start_migration.py --env "C:\\{env}.env" --runs-parent C:\\t2p --project {project}'
    command = f'python scripts/set_data_folder.py --package "C:\\runs\\001\\packages\\{package}"'
    observed = {
        "run_location": {"state": "intact"},
        "findings": [
            {
                "code": "UNSCOPED_PACKAGE",
                "reason": "preserve_and_inspect_association",
                "unassessable": False,
                "unit": finding,
                "source": f"packages/{finding}",
            }
        ],
        "units": [
            {
                "unit": engine,
                "id": "u0001",
                "name": unit + "\x1b[31m\nFORGED_LINE",
                "stage": "packaged",
                "working_package": f"packages/{package}/fabric",
                "kind": "workbook",
                "scope": "scoped",
                "generated_working_copy": "present",
                "handover": "present",
                "occurrences": [
                    {"unit": engine, "source": f"bundle/{engine}", "kind": "workbook", "status": "observed"}
                ],
                "package": {"unit": engine, "relative_path": f"packages/{package}"},
                "current_certification": "NOT_CHECKED",
            }
        ],
        "unscoped_packages": [{"unit": package, "relative_path": f"packages/{package}"}],
        "locations": [{"path": f"C:\\{label}\x1b[31m\nFORGED_LINE"} for label in absent[1:]],
        "next_action": {
            "kind": "command",
            "command": command,
            "headline": "Bind the data folder.",
            "owner": "set_data_folder.py",
            "gate": "data_binding",
            "affected_units": [engine],
            "details": ["PRIVATE_PROSE_CANARY"],
        },
        "path_budget": {
            "runs_parent": "C:\\runs",
            "root_kind": "external",
            "unknown": 0,
            "file": {"ceiling": 259, "longest": None, "path": None, "remaining": None},
            "directory": {"ceiling": 247, "longest": None, "path": None, "remaining": None},
            "rerun": {"command": rerun, "runs_parent": "C:\\t2p", "authoritative": True},
        },
        "environment": env,
        "profile": "PRIVATE_PROFILE_CANARY",
    }
    monkeypatch.setattr(run_status, "build_status", lambda _run: (observed, 0))
    assert t2p.main(["status", "--run", str(tmp_path), *(["--json"] if json_mode else [])]) == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    for label in absent:
        assert label not in output, f"status leaked customer label: {label}"
    assert "\x1b" not in output and not any(line.startswith("FORGED_LINE") for line in output.splitlines()), (
        "a local name must not control the terminal or forge a line"
    )
    assert captured.err == ""
    if json_mode:
        result = json.loads(captured.out)
        jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
        hits = _label_locations(result)
        allowed = {
            unit: {"/observations/units/0/name"},
            package: {"/observations/units/0/working_package", "/next/command"},
            engine: {"/next/affected_units/0"},
            env: {"/path_report/rerun/command"},
            project: {"/path_report/rerun/command"},
        }
        for label, paths in allowed.items():
            assert {path for path, text in hits if label in text} == paths, f"{label} escaped its field"
        assert result["next"]["command"] == command
        assert result["observations"]["unscoped_package_count"] == 1
    else:
        assert engine not in captured.out, f"status leaked customer label: {engine}"
        rows = captured.out.splitlines()
        allowed = {
            unit: ("  u0001 workbook ",),
            package: ("  u0001 workbook ", "Next: "),
            env: ("  Rerun: ",),
            project: ("  Rerun: ",),
        }
        for label, prefixes in allowed.items():
            assert all(row.startswith(prefixes) for row in rows if label in row), f"{label} escaped its field"
            assert any(label in row for row in rows), f"{label} must be shown locally"
        assert str(tmp_path) in captured.out
        assert "Unscoped packages: 1" in captured.out


@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
@pytest.mark.parametrize("case", list(STATUS_FROZEN_JSON))
def test_status_native_envelopes(monkeypatch, capsys, case, json_mode):
    """Pin complete base JSON bytes and every human/native/error/authority distinction."""
    observed, calls = _status_observed(), []
    if case == "status0":
        observed["findings"] = []
    if case in {"native1", "native2"}:
        observed = {
            "findings": [
                {"code": "UNASSESSABLE_EVIDENCE", "reason": "run_location_unestablished", "unassessable": True}
            ]
        }

    def owner(run):
        calls.append(run)
        if case == "exception":
            raise OSError("PRIVATE_EXCEPTION_CANARY\x1b[31m")
        if case == "interrupt":
            raise KeyboardInterrupt("PRIVATE_INTERRUPT_CANARY\x1b[31m")
        return observed, {"native1": 1, "native2": 2}.get(case, 0)

    monkeypatch.setattr("run_status.build_status", owner)
    argv = ["status", "--run", r"C:\synthetic\_runs\001-demo", *(["--json"] if json_mode else [])]
    if case == "syntax":
        argv.append("--PRIVATE_ARGV_\x1b[31m")
    expected = json.loads(STATUS_FROZEN_JSON[case])
    assert t2p.main(argv) == expected["exit_code"], "status must preserve exact native and outer exits"
    captured = capsys.readouterr()
    assert len(calls) == (0 if case == "syntax" else 1), "status reaches exactly one owner; syntax reaches none"
    assert captured.err == "", "status adds no stderr on any existing envelope path"
    assert "PRIVATE_" not in captured.out + captured.err and "\x1b" not in captured.out + captured.err
    if json_mode:
        assert captured.out.encode("ascii") == STATUS_FROZEN_JSON[case], "complete frozen v1 bytes must not change"
        jsonschema.validate(json.loads(captured.out), t2p.ENVELOPE_SCHEMA)
    else:
        assert captured.out.startswith(f"STATUS {expected['status']} (exit {expected['exit_code']})")
        assert captured.out.splitlines()[1:6] == [
            "Goal: inspect the explicitly selected run.",
            next(line for line in captured.out.splitlines() if line.startswith("Now: ")),
            "Observed handoff: NOT CHECKED - openability, START_READY and COMPLETE were not evaluated.",
            next(line for line in captured.out.splitlines() if line.startswith("Need: ")),
            f"Existing owner: {expected['authority'] or 'not reached'}; delegated diagnostic exit="
            f"{expected['delegated_exit_code'] if expected['delegated_exit_code'] is not None else 'unavailable'}; "
            f"facade exit={expected['exit_code']}.",
        ], "human status must lead with five consecutive inspection headings"
        assert ("\nRun: " in captured.out) == (expected["run_location"] is not None)
        if "observations" not in expected:
            assert "Now: diagnostic observations unavailable (not assessed)." in captured.out
            assert "Unscoped packages:" not in captured.out and "\nUnits " not in captured.out
        if case in {"native1", "native2"}:
            assert "run_location_unestablished unassessable=true (run-level)" in captured.out
            assert r"C:\synthetic" not in captured.out
            assert "Unscoped packages: 0 (projected count; inventory unavailable)" in captured.out
            assert not any(
                label in captured.out
                for label in (
                    "Recorded allocation status:",
                    "Inventory scope:",
                    "\nUnits ",
                    "Observed run folders:",
                    "Observed child evidence:",
                )
            ), "absent owner structures must not become an assessed empty inventory or null-field forest"


@pytest.mark.parametrize("order", ["original", "units", "findings", "phases"])
def test_status_human_preserves_every_observation(monkeypatch, capsys, order):
    """Ordered anonymous units, duplicate occurrences and run-level findings remain independent."""
    observed = _status_observed()
    observed["units"].append(
        {
            "unit": "PRIVATE_PLANNED_CANARY",
            "kind": "workbook",
            "scope": "scoped",
            "generated_working_copy": "missing",
            "handover": "missing",
            "occurrences": [],
            "package": None,
            "current_certification": "NOT_CHECKED",
            "id": None,
            "name": "Planned",
            "stage": "not_started",
            "working_package": "not-started",
        }
    )
    if order == "units":
        observed["units"].reverse()
        for unit in observed["units"]:
            unit["occurrences"].reverse()
    if order == "findings":
        observed["findings"].reverse()
    if order == "phases":
        observed["recorded_phases"].reverse()
    monkeypatch.setattr("run_status.build_status", lambda _run: (observed, 0))
    assert t2p.main(["status", "--run", r"C:\synthetic\_runs\001-demo"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("STATUS findings (exit 0) - observations only\n"), (
        "human status must render inspection headings, not pretty JSON"
    )
    assert "Now: inventory scope=established; 2 observed units (not the commissioned denominator)." in captured.out
    assert "Units (ID or -, kind, name, stage, working package):" in captured.out
    assert "Recorded allocation status: active (metadata only; not process liveness)" in captured.out
    assert "Inventory scope: established\nScope denominator: established; workbooks=1; required_datasources=1\n" in (
        captured.out
    ), "the sealed scope denominator is shown in text, not only in --json"
    assert "Unscoped packages: 1" in captured.out
    assert captured.err == ""
    expected = []
    for unit in observed["units"]:
        expected.append(
            f"  {unit['id'] or '-'} {unit['kind']} {json.dumps(unit['name'])}; stage={unit['stage']}; "
            f"working-package={unit['working_package']}; scope={unit['scope']}; "
            f"working-copy={unit['generated_working_copy']}; "
            f"handover={unit['handover']}; packages={int(unit['package'] is not None)}; certification=NOT_CHECKED"
        )
        expected.extend(
            f"    occurrence[{number}] kind={item['kind']} status={item['status']}"
            for number, item in enumerate(unit["occurrences"])
        )
    assert captured.out.split("Units (ID or -, kind, name, stage, working package):\n")[1].split("Findings")[0] == (
        "\n".join(expected) + "\n"
    ), "every ordered unit and occurrence, including duplicates, must survive"
    assert captured.out.split("Findings (run-level; not associated with units):\n")[1].split("Unscoped packages:")[
        0
    ] == "".join(
        f"  [{index}] code={row['code']} reason={row['reason']} unassessable=false (run-level)\n"
        for index, row in enumerate(observed["findings"])
    ), "findings stay ordered at run level, never positionally joined to units"
    for title, key in (("Observed run folders", "canonical_subdirs"), ("Observed child evidence", "child_evidence")):
        assert title + ": " + "; ".join(f"{key}={value}" for key, value in observed[key].items()) in captured.out
    for index, row in enumerate(observed["recorded_phases"]):
        assert (
            f"Last observed phase[{index}]: source={row['source']}; phase={row.get('phase', 'unavailable')}; "
            f"failed={str(row['failed']).lower()}; last_observed: "
            + (
                "; ".join(
                    f"{key}={json.dumps(value) if not isinstance(value, str) else value}"
                    for key, value in row["last_observed"].items()
                )
                or "{}"
            )
            + " - historical only, not a current check."
        ) in captured.out, "every safe phase field and ordered nested error record is historical only"
    assert captured.out.split("Paths: ")[1].splitlines() == [
        r"runs-parent=C:\synthetic\_runs root=external; unmeasured=0",
        "  longest file=120 of 259; remaining=139; path=packages/sales/fabric/x.tmdl",
        "  longest directory=100 of 247; remaining=147; path=packages/sales/fabric",
        r'Next: python scripts/set_data_folder.py --package "C:\synthetic\_runs\001-demo\packages\sales"',
        "Blocked by: data_binding (owner set_data_folder.py; kind command)",
        "Unavailable here: original intent/revision; openability; recorded parent/root/repo/git/commit state; subst.",
    ], "the owner's path budget and next command are shown, and nothing beyond them is claimed"


@pytest.mark.parametrize(
    "state", ["missing", "present", "unassessable:directory_unreadable", "finding", "other_missing"]
)
def test_status_inspection_never_certifies_handoff(monkeypatch, capsys, state):
    """Presence reversals affect inspection only, never package/history-derived readiness."""
    observed = _status_observed()
    observed["findings"] = observed["findings"][:1] if state == "finding" else []
    observed["canonical_subdirs"] = {key: "present" for key in observed["canonical_subdirs"]}
    observed["units"][0]["generated_working_copy"] = (
        state if state.startswith(("missing", "unassessable")) else "present"
    )
    if state == "other_missing":
        observed["canonical_subdirs"]["oracle"] = "missing"
    monkeypatch.setattr("run_status.build_status", lambda _run: (observed, 0))
    assert t2p.main(["status", "--run", r"C:\synthetic\_runs\001-demo"]) == 0
    captured = capsys.readouterr()
    assert (
        "Observed handoff: NOT CHECKED - openability, START_READY and COMPLETE were not evaluated." in captured.out
        and captured.out.count("certification=NOT_CHECKED") == 2
    ), "package presence and historical success must never promote current handoff"
    assert (
        "Need: this diagnostic establishes no required operator/lifecycle decision." in captured.out
        if state == "present"
        else "Need: inspect missing/unassessable/finding observations; cause and required "
        "operator/lifecycle decision unknown." in captured.out
    ), "Need is only the fixed inspection rule, not a lifecycle or credential classifier"
    assert "Paths: runs-parent=" in captured.out and "Next: python scripts/set_data_folder.py" in captured.out
    assert "ASSEMBLED" not in captured.out and "BOUND" not in captured.out and "PASS" not in captured.out
    assert "remaining_file_utf16" not in captured.out and "999" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("packages", [0, 2])
def test_status_zero_units_keeps_unscoped_packages(monkeypatch, capsys, packages):
    """A package-only inventory is not silently turned into scoped units."""
    observed = _status_observed()
    observed["units"] = []
    observed["unscoped_packages"] *= packages
    monkeypatch.setattr("run_status.build_status", lambda _run: (observed, 0))
    assert t2p.main(["status", "--run", r"C:\synthetic\_runs\001-demo"]) == 0
    captured = capsys.readouterr()
    assert "0 observed units (not the commissioned denominator)" in captured.out
    assert f"Unscoped packages: {packages}\n" in captured.out
    assert "  none projected\nFindings" in captured.out
    assert "working-copy=" not in captured.out and "certification=" not in captured.out


@pytest.mark.parametrize("json_mode", [False, True])
def test_status_scalar_controls_are_escaped(monkeypatch, capsys, json_mode):
    """Safe-field observations remain visible without controlling the terminal."""
    observed = _status_observed()
    text = "OBSERVED\n\r\t\x00\x1b[31m\x7f\u0085\u202e\U0001f642"
    observed["run_recorded_status"] = text
    observed["canonical_subdirs"] = {text: text}
    observed["child_evidence"] = {text: text}
    observed["units"][0].update(
        kind=text, scope=text, generated_working_copy=text, handover=text, current_certification=text
    )
    observed["units"][0]["occurrences"] = [{"kind": text, "status": text}]
    observed["findings"][0].update(code=text, reason=text)
    observed["recorded_phases"][0].update(source=text, phase=text)
    observed["recorded_phases"][0]["last_observed"] = {"status": text, "errors": [{"code": text, "operation": text}]}
    monkeypatch.setattr("run_status.build_status", lambda _run: (observed, 0))
    assert t2p.main(["status", "--run", "C:\\synthetic\\" + text, *(["--json"] if json_mode else [])]) == 0
    captured = capsys.readouterr()
    assert json.dumps(text, ensure_ascii=True)[1:-1] in captured.out, (
        "escape controls without dropping real observations"
    )
    assert captured.out.isascii() and not any(ord(char) < 32 and char != "\n" for char in captured.out + captured.err)
    assert "\x7f" not in captured.out + captured.err
    assert captured.err == ""
    if json_mode:
        assert json.loads(captured.out)["observations"]["units"][0]["kind"] == text


@pytest.mark.parametrize("json_mode", [False, True])
def test_status_presentation_has_no_extra_io(monkeypatch, tmp_path, capsys, json_mode):
    """Allow cold parser metadata reads, then trap all presentation-side I/O."""
    observed, calls = _status_observed(), []
    locale_lookups, find_locale = [], gettext.find

    def cold_parser_locale(*_args, **_kwargs):
        location = find_locale("t2p-parser-control", localedir=str(tmp_path), languages=["zz"])
        locale_lookups.append(location)
        return location

    with monkeypatch.context() as parser_setup:
        parser_setup.setattr(gettext, "find", cold_parser_locale)
        parser = t2p.build_parser()
    assert locale_lookups and all(value is None for value in locale_lookups), (
        "cold parser locale metadata lookup must precede presentation traps"
    )
    monkeypatch.setattr(t2p, "build_parser", lambda: parser)

    def owner(run):
        assert not calls, "status presentation reaches one owner and does not reread it"
        calls.append(run)
        return observed, 0

    def forbidden(*_args, **_kwargs):
        pytest.fail("status presentation must not perform extra I/O or invoke another owner")

    monkeypatch.setattr("run_status.build_status", owner)
    monkeypatch.setattr("check_path_ceiling.scan", forbidden)
    monkeypatch.setattr("run_status.render_human", forbidden)
    monkeypatch.setattr("run_status._next_action", forbidden)
    monkeypatch.setattr(work_dirs, "allocate_run", forbidden)
    with monkeypatch.context() as boundary:
        for name in (
            "open",
            "read_text",
            "read_bytes",
            "write_text",
            "write_bytes",
            "stat",
            "lstat",
            "resolve",
            "absolute",
            "exists",
            "is_dir",
            "is_file",
            "iterdir",
            "glob",
            "rglob",
            "mkdir",
            "touch",
            "unlink",
            "rename",
            "replace",
            "__truediv__",
        ):
            boundary.setattr(Path, name, forbidden)
        for name in (
            "open",
            "stat",
            "lstat",
            "scandir",
            "listdir",
            "mkdir",
            "makedirs",
            "unlink",
            "remove",
            "system",
            "kill",
        ):
            boundary.setattr(os, name, forbidden)
        for name in ("exists", "lexists", "isdir", "isfile", "islink", "realpath"):
            boundary.setattr(os.path, name, forbidden)
        for name in ("run", "Popen", "check_output", "check_call"):
            boundary.setattr(subprocess, name, forbidden)
        for name in ("socket", "create_connection", "getaddrinfo"):
            boundary.setattr(socket, name, forbidden)
        boundary.setattr("builtins.open", forbidden)
        boundary.setattr(io, "open", forbidden)
        assert t2p.main(["status", "--run", r"C:\synthetic\_runs\001-demo", *(["--json"] if json_mode else [])]) == 0
    assert calls == [r"C:\synthetic\_runs\001-demo"], "status presentation reaches one owner and does not reread it"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "file,before,after,selector,assertion",
    [
        (
            "t2p.py",
            "code = doctor(args, result)",
            "code = 0",
            "test_explicit_branch_reaches_owner[doctor]",
            "explicit command branch must reach",
        ),
        (
            "t2p.py",
            "code = owner(args, result)",
            "code = 0",
            "test_explicit_branch_reaches_owner[start]",
            "explicit command branch must reach",
        ),
        (
            "t2p.py",
            'owner = refresh if args.command == "refresh" else start',
            'owner = (lambda _args, _result: 0) if args.command == "refresh" else start',
            "test_explicit_branch_reaches_owner[refresh]",
            "explicit command branch must reach",
        ),
        (
            "t2p.py",
            "code = status(args, result)",
            "code = 0",
            "test_explicit_branch_reaches_owner[status]",
            "explicit command branch must reach",
        ),
        (
            "t2p.py",
            '"PYTHONDONTWRITEBYTECODE": "1"',
            '"MUTANT_BYTECODE": "1"',
            "test_doctor_child_cannot_create_pycache",
            "doctor clean-tree/no-pyc assertion",
        ),
        (
            "t2p.py",
            '"Bypass",',
            '"Bypass", "-Update",',
            "test_doctor_private_presence_only",
            "doctor must delegate plain preflight",
        ),
        (
            "t2p.py",
            'result["checks"] = configuration["checks"]',
            'result["checks"] = configuration["checks"]\n    result["checks"][0]["reason"] = _values.get("TABLEAU_PAT_SECRET")',
            "test_doctor_private_presence_only",
            "doctor must not disclose values",
        ),
        (
            "t2p.py",
            '"presence_only_not_authentication"]',
            '"presence_only_not_authentication", len(_values.get("TABLEAU_PAT_SECRET", ""))]',
            "test_doctor_private_presence_only",
            "doctor value/length/digest leak assertion",
        ),
        (
            "t2p.py",
            '"presence_only_not_authentication"]',
            '"presence_only_not_authentication", __import__("hashlib").sha256(_values["TABLEAU_PAT_SECRET"].encode()).hexdigest()]',
            "test_doctor_private_presence_only",
            "doctor must not disclose value digests",
        ),
        (
            "tableau_env.py",
            "source, values = label, candidate",
            'source, values = "absent", candidate',
            "test_doctor_provenance_control",
            "winning source must be environment",
        ),
        (
            "t2p.py",
            '"initial_decision": "cannot_establish"',
            '"initial_decision": "PASS"',
            "test_start_reaches_existing_postharvest_gate",
            "unknown must not become PASS",
        ),
        (
            "run_estate.py",
            "if args.input:\n        path_ok, path_detail = preflight_estate_path_ceiling",
            "if False:\n        path_ok, path_detail = preflight_estate_path_ceiling",
            "test_start_reaches_existing_postharvest_gate",
            "non-pass path gate must prevent conversion",
        ),
        (
            "t2p.py",
            'record["native_exit_code"],',
            "None,",
            "test_start_owner_parity_and_native_stages",
            "retain per-stage native results",
        ),
        (
            "start_migration.py",
            "        bundle_command = [",
            "        if failed:\n            return _finalize(root, bundle, oracle_root, failed, invalid, fidelity)\n        bundle_command = [",
            "test_start_owner_parity_and_native_stages",
            "reference failure must still attempt bundle",
        ),
        (
            "t2p.py",
            'result["delegated_exit_code"] = code',
            'result["delegated_exit_code"] = 0',
            "test_start_owner_parity_and_native_stages",
            "preserve wrapper exit",
        ),
        (
            "t2p.py",
            "os.dup2(2, 1)",
            "os.dup2(saved, 1)",
            "test_native_descriptor_output_is_live_and_json_stdout_single",
            "OS stdout must be routed to stderr",
        ),
        (
            "start_migration.py",
            '"stdout": None, "stderr": None',
            '"stdout": subprocess.PIPE, "stderr": subprocess.PIPE',
            "test_start_owner_parity_and_native_stages",
            "stage output must stream without buffering",
        ),
        (
            "t2p.py",
            "observed, code = run_status.build_status(args.run)",
            'observed, code = run_status.build_status(args.run)\n    (Path(args.run) / "mutant-write").write_text("changed")',
            "test_status_readonly_and_owner_parity",
            "status byte/mtime/tree read-only assertion",
        ),
        (
            "t2p.py",
            'result["authority"] = "run_status.py"',
            'result["authority"] = "run_status.py"\n    result["next_action"] = "mutant"',
            "test_status_readonly_and_owner_parity",
            "status exposes the owner's next only as `next`",
        ),
        (
            "start_migration.py",
            "observer(_path_gate_record(projection, allowed))",
            "pass",
            "test_start_reaches_existing_postharvest_gate",
            "authoritative owner path verdict must be exposed",
        ),
        (
            "tableau_env.py",
            "if default_site:",
            "if default_site and not present:",
            "test_doctor_valid_default_and_optional_absence",
            "valid Default site must not prescribe repair",
        ),
        (
            "tableau_env.py",
            "key in required and state in",
            "state in",
            "test_doctor_valid_default_and_optional_absence",
            "optional missing keys must not prescribe repair",
        ),
        (
            "t2p.py",
            '"ordinal": index,',
            '"ordinal": index, "unit": row["unit"],',
            "test_status_customer_labels_stay_in_their_fields",
            "PRIVATE_ENGINE_CANARY_61 escaped its field",
        ),
        pytest.param(
            "t2p.py",
            "_status_human(result)",
            "_print_safe(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))",
            "test_status_human_preserves_every_observation",
            "human status must render inspection headings, not pretty JSON",
            id="status-human-headings",
        ),
        pytest.param(
            "t2p.py",
            '"kind": row["kind"],\n                "scope": row["scope"],',
            '"kind": row["unit"],\n                "scope": row["scope"],',
            "test_status_customer_labels_stay_in_their_fields[False]",
            "status leaked customer label: PRIVATE_ENGINE_CANARY_61",
            id="status-private-unit",
        ),
        pytest.param(
            "t2p.py",
            "Observed handoff: NOT CHECKED - openability, START_READY and COMPLETE were not evaluated.",
            "Observed handoff: START_READY - package/history accepted.",
            "test_status_inspection_never_certifies_handoff[present]",
            "package presence and historical success must never promote current handoff",
            id="status-false-green",
        ),
        pytest.param(
            "t2p.py",
            "code = status(args, result)",
            "code = min(status(args, result), 1)",
            "test_status_native_envelopes[native2-human]",
            "status must preserve exact native and outer exits",
            id="status-native-normalization",
        ),
        (
            "tableau_env.py",
            '"source": label,',
            '"source": "environment" if label == "selected_env" else "selected_env",',
            "test_doctor_all_blockers_sources_and_safe_remedies",
            "losing-file conflict must be located in the selected file",
        ),
        (
            "tableau_env.py",
            "for candidate in (file_values, process)\n        )",
            "for candidate in (process,)\n        )",
            "test_doctor_all_blockers_sources_and_safe_remedies",
            "losing-source conflict must retain the original refusal",
        ),
        (
            "t2p.py",
            "preserve it under {key} BEFORE removing {legacy}",
            "preserve it under {legacy} BEFORE removing {key}",
            "test_doctor_all_blockers_sources_and_safe_remedies",
            "canonical PAT preservation must precede legacy removal",
        ),
        pytest.param(
            "preflight.ps1",
            "blockers = @($critical | ForEach-Object",
            "blockers = @($critical | Select-Object -Skip 1 | ForEach-Object",
            "test_preflight_contract.py::test_doctor_actual_windows_child[blocked-ids1]",
            "all actual failed-critical blockers must be retained",
            marks=pytest.mark.skipif(
                os.name != "nt", reason="requires Windows PowerShell/PowerShell 7 on Windows and NTFS"
            ),
        ),
        pytest.param(
            "preflight.ps1",
            "$adomdReason = 'pythonnet_missing'",
            "$adomdReason = 'assembly_missing'",
            "test_preflight_contract.py::test_doctor_actual_windows_child[blocked-ids1]",
            "each ADOMD failure needs the exact owner ID/reason/remedy tuple",
            marks=pytest.mark.skipif(
                os.name != "nt", reason="requires Windows PowerShell/PowerShell 7 on Windows and NTFS"
            ),
        ),
        (
            "t2p.py",
            '"adomd_client": ({"assembly_missing", "pythonnet_missing"}, "restore_adomd")',
            '"adomd_client": ({"assembly_missing"}, "restore_adomd")',
            "test_doctor_all_blockers_sources_and_safe_remedies",
            "complete owner blockers must not be lost",
        ),
        (
            "t2p.py",
            '"-DoctorDiagnostics",',
            '"-DoctorDiagnostics", "-CheckUpstream",',
            "test_doctor_private_presence_only",
            "doctor must delegate plain preflight",
        ),
        pytest.param(
            "preflight.ps1",
            "reason = $DoctorReason; remedy = $DoctorRemedy",
            "reason = $Detail; remedy = $DoctorRemedy",
            "test_preflight_contract.py::test_doctor_actual_windows_child[blocked-ids1]",
            "owner safe records must never copy dynamic detail/hint/path/helper output",
            marks=pytest.mark.skipif(
                os.name != "nt", reason="requires Windows PowerShell/PowerShell 7 on Windows and NTFS"
            ),
        ),
        (
            "t2p.py",
            "except (ValueError, TypeError, UnicodeError, RecursionError):\n        return unavailable",
            'except (ValueError, TypeError, UnicodeError, RecursionError):\n        return {"availability":"available",'
            '"reason":"available","critical_missing":1,"recommended_warnings":0,"blockers":'
            '[{"id":"adomd_client","source":None,"reason":"pythonnet_missing","remedy":"restore_adomd"}]}',
            "test_doctor_unavailable_detail_preserves_native_policy",
            "malformed/noisy/unknown detail must be unavailable, never a guessed cause",
        ),
        (
            "t2p.py",
            "code = child.returncode",
            "code = 1 if child.returncode == 7 else child.returncode",
            "test_doctor_unavailable_detail_preserves_native_policy",
            "native 1 stays blocked; unexpected native 7 stays unknown",
        ),
        (
            "t2p.py",
            'unknown = configuration["status"] == "CANNOT_ESTABLISH" or code not in {0, 1}',
            'unknown = configuration["status"] == "CANNOT_ESTABLISH" or code not in {0, 1} or '
            '(code == 1 and result["doctor_details"]["preflight"]["availability"] == "unavailable")',
            "test_doctor_unavailable_detail_preserves_native_policy",
            "native 1 stays blocked; unexpected native 7 stays unknown",
        ),
        (
            "t2p.py",
            "if json_mode:\n        print(json.dumps(result",
            'if json_mode:\n        print("prose")\n        print(json.dumps(result',
            "test_doctor_channels_remain_distinct",
            "JSON stdout must be one object and newline",
        ),
        (
            "t2p.py",
            "_doctor_human(result, args)\n    else:",
            "_print_safe(json.dumps(result, sort_keys=True))\n    else:",
            "test_doctor_channels_remain_distinct",
            "human doctor must render a verdict, not the JSON envelope",
        ),
        (
            "t2p.py",
            '        for blocker in detail["blockers"]:\n            lines.extend',
            '        for blocker in detail["blockers"][:1]:\n            lines.extend',
            "test_doctor_all_blockers_sources_and_safe_remedies",
            "every safe preflight blocker must be rendered",
        ),
        pytest.param(
            "preflight.ps1",
            "if ($DoctorDiagnostics) {\n    Write-Output",
            "if ($DoctorDiagnostics) {\n"
            "    [IO.File]::WriteAllText((Join-Path $env:USERPROFILE 'mutant-write'), 'changed')\n"
            "    Write-Output",
            "test_preflight_contract.py::test_doctor_actual_windows_child[ready-ids0]",
            "actual-child monitored synthetic tree must remain byte/mtime/tree unchanged",
            marks=pytest.mark.skipif(
                os.name != "nt", reason="requires Windows PowerShell/PowerShell 7 on Windows and NTFS"
            ),
        ),
        pytest.param(
            "preflight.ps1",
            "if ($DoctorDiagnostics) {\n    Write-Output",
            "if ($DoctorDiagnostics) {\n"
            "    Invoke-WebRequest -Uri 'https://synthetic.invalid/' | Out-Null\n"
            "    Write-Output",
            "test_preflight_contract.py::test_doctor_actual_windows_child[ready-ids0]",
            "actual-child forbidden network/updater/installer/login command must not be invoked",
            marks=pytest.mark.skipif(
                os.name != "nt", reason="requires Windows PowerShell/PowerShell 7 on Windows and NTFS"
            ),
        ),
    ],
)
def test_isolated_mutations_fail_intended_assertion(tmp_path, file, before, after, selector, assertion):
    """Approved mutation controls fail on the intended assertion, never an import/exit alone."""
    isolated = tmp_path / "copy"
    scripts = isolated / "scripts"
    scripts.mkdir(parents=True)
    for source in (REPO_ROOT / "scripts").glob("*.py"):
        shutil.copyfile(source, scripts / source.name)
    shutil.copyfile(REPO_ROOT / "scripts" / "preflight.ps1", scripts / "preflight.ps1")
    tests = isolated / "tests"
    tests.mkdir()
    for name in ("test_t2p.py", "test_start_migration.py", "test_preflight_contract.py"):
        shutil.copyfile(REPO_ROOT / "tests" / name, tests / name)
    target = scripts / file
    source = target.read_text(encoding="utf-8")
    assert before in source, "mutation anchor must exist"
    target.write_text(source.replace(before, after, 1), encoding="utf-8")
    module, node = selector.split("::", 1) if "::" in selector else ("test_t2p.py", selector)
    outcome = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            str(tests / module) + "::" + node,
            "--tb=short",
            "--basetemp",
            str(isolated / "_test"),
        ],
        cwd=isolated,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
    )
    assert outcome.returncode == 1, outcome.stdout + outcome.stderr
    assert assertion in outcome.stdout, outcome.stdout + outcome.stderr
    assert "ERROR collecting" not in outcome.stdout + outcome.stderr


def test_doctor_provenance_control(monkeypatch, tmp_path, capsys):
    _config(monkeypatch)
    path = tmp_path / "selected.env"
    path.write_text("TABLEAU_PAT_SECRET=losing-file-secret\n", encoding="utf-8")
    monkeypatch.setattr(t2p.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    assert t2p.main(["doctor", "--env", str(path), "--json"]) == 0
    result, _ = _json(capsys)
    row = next(row for row in result["checks"] if row["key"] == "TABLEAU_PAT_SECRET")
    assert row["source"] == "environment", "winning source must be environment"


@pytest.mark.parametrize("json_mode", [False, True])
def test_doctor_interrupt_is_safe_in_both_modes(monkeypatch, capsys, json_mode):
    _config(monkeypatch)

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt("PRIVATE_INTERRUPT_CANARY")

    monkeypatch.setattr(t2p.subprocess, "run", interrupt)
    assert t2p.main(["doctor", *(["--json"] if json_mode else [])]) == 130
    captured = capsys.readouterr()
    if json_mode:
        result = json.loads(captured.out)
        jsonschema.validate(result, t2p.ENVELOPE_SCHEMA)
        assert result["status"] == "interrupted"
        assert result["delegated_exit_code"] is None
        assert result["stages"] == [], "interruption must not invent a completed child"
        assert len(captured.out.splitlines()) == 1
    else:
        assert captured.out.startswith("DOCTOR interrupted (exit 130)\n")
    assert "PRIVATE_INTERRUPT_CANARY" not in captured.out + captured.err


def test_offline_help_output_journey(monkeypatch, tmp_path, capsys):
    """Help/output-only mechanics rehearsal, explicitly not live blind-run acceptance."""
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    monkeypatch.setattr(t2p, "ROOT", pipeline.root)
    _config(monkeypatch)
    with pytest.raises(SystemExit) as help_result:
        t2p.main(["--help"])
    assert help_result.value.code == 0
    help_text = capsys.readouterr().out
    assert "{doctor,start,assess,status,units,refresh}" in help_text
    assert t2p.main(["doctor", "--json"]) == 0
    doctor_result, _ = _json(capsys)
    assert doctor_result["status"] == "READY_TO_ATTEMPT"
    assert t2p.main(["start", "--project", "Finance", "--json"]) == 0
    output, progress = _captured(capsys, pipeline)
    started = json.loads(output)
    selected = started["run_location"]["physical_run"]
    assert progress.splitlines()[-1] == "RUN " + selected
    assert t2p.main(["status", "--run", selected, "--json"]) in {0, 1}
    observed, _ = _json(capsys)
    assert observed["run_location"]["physical_run"] == selected
    for result in (doctor_result, started, observed):
        assert {"START_READY", "COMPLETE", "fidelity", "process_liveness"} <= set(
            result["proof_boundary"]["not_established"]
        )
    assert len(pipeline.commands) == 5, "offline harness must exercise the existing five-stage owner"


@pytest.mark.parametrize("json_mode", [False, True])
def test_doctor_all_blockers_sources_and_safe_remedies(monkeypatch, tmp_path, capsys, json_mode):
    """One result explains all blockers, including a losing-file conflict, without acting on them."""
    _config(monkeypatch)
    monkeypatch.delenv("TABLEAU_PAT_NAME")
    path = tmp_path / "PRIVATE_SELECTED_PATH_CANARY.env"
    path.write_text("TABLEAU_PAT_SECRET=PRIVATE_CANONICAL_CANARY\nTABLEAU_PAT_VALUE=PRIVATE_LEGACY_CANARY\n")
    import_blocker = {
        "id": "python_lxml",
        "source": None,
        "reason": "import_failed",
        "remedy": "sync_python_dependencies",
    }
    monkeypatch.setattr(
        t2p.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 1, _preflight_bytes(ADOMD_BLOCKER, import_blocker, warnings=2), b""
        ),
    )
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("doctor must not open a socket"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("doctor must not launch another process"))
    monkeypatch.setattr(work_dirs, "allocate_run", lambda *a, **k: pytest.fail("doctor must not allocate"))
    before = _snapshot(tmp_path)
    with monkeypatch.context() as writes:
        writes.setattr(Path, "write_text", lambda *a, **k: pytest.fail("doctor must not write"))
        writes.setattr(Path, "write_bytes", lambda *a, **k: pytest.fail("doctor must not write"))
        assert t2p.main(["doctor", "--env", str(path), *(["--json"] if json_mode else [])]) == 1
    captured = capsys.readouterr()
    assert _snapshot(tmp_path) == before
    assert all(
        canary not in captured.out + captured.err
        for canary in ("PRIVATE_CANONICAL_CANARY", "PRIVATE_LEGACY_CANARY", path.name, str(tmp_path), *ENV.values())
    ), "doctor must not disclose values or host paths"
    if json_mode:
        result = json.loads(captured.out)
        detail = result["doctor_details"]
        pat = next(row for row in result["checks"] if row["key"] == "TABLEAU_PAT_SECRET")
        assert pat["source"] == "environment", "process remains the effective source"
        assert pat["state"] == "conflict", "losing-source conflict must retain the original refusal"
        group = next(row for row in detail["configuration_aliases"] if row["key"] == "TABLEAU_PAT_SECRET")
        assert group["sources"] == [
            {"source": "selected_env", "keys": ["TABLEAU_PAT_SECRET", "TABLEAU_PAT_VALUE"], "conflict": True},
            {"source": "environment", "keys": ["TABLEAU_PAT_SECRET"], "conflict": False},
        ], "losing-file conflict must be located in the selected file"
        assert detail["preflight"]["blockers"] == [ADOMD_BLOCKER, import_blocker], (
            "complete owner blockers must not be lost"
        )
        assert detail["preflight"]["recommended_warnings"] == 2
    else:
        assert "TABLEAU_PAT_NAME: missing; effective source: absent." in captured.out
        assert (
            "Effective source: process environment; conflict to resolve: selected environment file." in captured.out
        ), "losing-file conflict must be located in the selected file"
        assert "preserve it under TABLEAU_PAT_SECRET BEFORE removing TABLEAU_PAT_VALUE" in captured.out, (
            "canonical PAT preservation must precede legacy removal"
        )
        assert "If TABLEAU_PAT_VALUE holds the intended value, move it to TABLEAU_PAT_SECRET first." in captured.out
        assert (
            "Preflight blocker: Power BI Desktop's bundled ADOMD client could not be loaded;"
            " reason: pythonnet_missing.\nAction: install pythonnet into the Python that runs the toolkit;"
            " otherwise install or repair Power BI Desktop; then rerun." in captured.out
        )
        assert "Preflight blocker: Python cannot import lxml; reason: import_failed." in captured.out, (
            "every safe preflight blocker must be rendered"
        )
        assert "Recommended warnings: 2 (non-blocking)." in captured.out
        assert "Preflight owner: preflight.ps1; native exit: 1." in captured.out


@pytest.mark.parametrize(
    "legacy,equal", [("TABLEAU_SERVER", False), ("TABLEAU_PAT_VALUE", False), ("TABLEAU_PAT_VALUE", True)]
)
def test_doctor_alias_warning_is_nonblocking(monkeypatch, tmp_path, capsys, legacy, equal):
    """Canonical/legacy alias warnings must preserve the existing ready result."""
    _config(monkeypatch)
    canonical = "TABLEAU_SERVER_URL" if legacy == "TABLEAU_SERVER" else "TABLEAU_PAT_SECRET"
    monkeypatch.setenv(legacy, ENV[canonical])
    if not equal:
        monkeypatch.delenv(canonical)
    monkeypatch.setattr(t2p, "ROOT", tmp_path)
    monkeypatch.setattr(
        t2p.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, _preflight_bytes(warnings=3), b"")
    )
    assert t2p.main(["doctor"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("DOCTOR READY_TO_ATTEMPT (exit 0)\n")
    assert f"Alias warning in process environment: {legacy} is deprecated; use {canonical}." in captured.out
    assert "Recommended warnings: 3 (non-blocking)." in captured.out
    assert "Rerun doctor: python -B scripts\\t2p.py doctor\n" in captured.out
    assert "--env <same-selected-env>" not in captured.out


@pytest.mark.parametrize("native,expected", [(0, 0), (1, 1), (7, 3)])
@pytest.mark.parametrize(
    "stdout,stderr",
    [
        (b"", b""),
        (b"PRIVATE_MALFORMED_DESKTOP_PAT_CAUSE", b""),
        (b"[" * 1100 + b"]" * 1100, b""),
        (b'noise\n{"critical_missing":0,"recommended_warnings":0,"blockers":[]}', b""),
        (_preflight_bytes(), b"PRIVATE_HOST_PLUGIN_PATH\x1b[31mCANARY"),
        (b'{"critical_missing":1,"recommended_warnings":0,"blockers":[]}', b""),
        (b'{"critical_missing":true,"recommended_warnings":0,"blockers":[]}', b""),
        (b'{"critical_missing":0,"recommended_warnings":false,"blockers":[]}', b""),
        (b'{"critical_missing":0,"recommended_warnings":-1,"blockers":[]}', b""),
        (b'{"critical_missing":0,"blockers":[]}', b""),
        (_preflight_bytes({**ADOMD_BLOCKER, "id": "PRIVATE_UNKNOWN_ID"}), b""),
        (_preflight_bytes({**ADOMD_BLOCKER, "reason": "PRIVATE_UNKNOWN_REASON"}), b""),
        (_preflight_bytes({**ADOMD_BLOCKER, "source": "PRIVATE_HOST_PATH"}), b""),
        (_preflight_bytes({**ADOMD_BLOCKER, "remedy": "PRIVATE_HELPER_OUTPUT"}), b""),
        (_preflight_bytes({**ADOMD_BLOCKER, "Detail": "PRIVATE_HINT_CANARY"}), b""),
    ],
)
def test_doctor_unavailable_detail_preserves_native_policy(
    monkeypatch, tmp_path, capsys, native, expected, stdout, stderr
):
    """Reject unsafe owner details without normalizing the native or outer exit."""
    _config(monkeypatch)
    monkeypatch.setattr(t2p, "ROOT", tmp_path)
    monkeypatch.setattr(t2p.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, native, stdout, stderr))
    assert t2p.main(["doctor", "--json"]) == expected, "native 1 stays blocked; unexpected native 7 stays unknown"
    result, captured = _json(capsys)
    detail = result["doctor_details"]["preflight"]
    assert (detail["availability"], detail["critical_missing"], detail["recommended_warnings"], detail["blockers"]) == (
        "unavailable",
        None,
        None,
        [],
    ), "malformed/noisy/unknown detail must be unavailable, never a guessed cause"
    assert result["stages"][0]["native_exit_code"] == native, "retain exact unexpected native evidence"
    assert "PRIVATE_" not in captured.out + captured.err and "\x1b" not in captured.out + captured.err
    assert t2p.main(["doctor"]) == expected
    human = capsys.readouterr()
    assert "Recommended-warning count: unavailable." in human.out
    assert "powershell -ExecutionPolicy Bypass -File scripts\\preflight.ps1" in human.out
    assert "PBI_DESKTOP_PATH" not in human.out, "do not guess Desktop/PAT from raw prose"
    assert "Do not change Tableau credentials on this evidence." in human.out


@pytest.mark.parametrize(
    "native,expected,status",
    [(0, 0, "READY_TO_ATTEMPT"), (1, 1, "BLOCKED_CONFIG"), (7, 3, "CANNOT_ESTABLISH"), (None, 3, "CANNOT_ESTABLISH")],
)
@pytest.mark.parametrize(
    "configuration", ["ready", "missing", "blank", "conflict", "default_site", "malformed", "absent", "unreadable"]
)
def test_doctor_preexisting_json_is_byte_equivalent(
    monkeypatch, tmp_path, capsys, native, expected, status, configuration
):
    """Pinned base fixture semantics: exclude only the one permitted additive property."""
    _config(monkeypatch)
    monkeypatch.setattr(t2p, "ROOT", tmp_path)
    selected, reason = tmp_path / "selected.env", "presence_only"
    arguments = ["doctor", "--json"]
    if configuration == "missing":
        monkeypatch.delenv("TABLEAU_PAT_NAME")
    elif configuration == "blank":
        monkeypatch.setenv("TABLEAU_PAT_SECRET", " \t ")
    elif configuration == "conflict":
        monkeypatch.setenv("TABLEAU_PAT_VALUE", "PRIVATE_ALIAS_CANARY")
    elif configuration == "default_site":
        monkeypatch.delenv("TABLEAU_SITE")
    elif configuration in {"malformed", "absent", "unreadable"}:
        arguments.extend(["--env", str(selected)])
        if configuration == "malformed":
            selected.write_text("PRIVATE_MALFORMED_LINE_CANARY", encoding="utf-8")
        elif configuration == "unreadable":
            selected.mkdir()
        reason = {
            "malformed": "selected_env_malformed",
            "absent": "selected_env_missing",
            "unreadable": "selected_env_unreadable",
        }[configuration]
        expected, status = 3, "CANNOT_ESTABLISH"
    if configuration in {"missing", "blank", "conflict"}:
        reason = "configuration_inconsistent"
        if native in {0, 1}:
            expected, status = 1, "BLOCKED_CONFIG"

    def child(*a, **_kwargs):
        if native is None:
            raise OSError("PRIVATE_LAUNCH_EXCEPTION")
        return subprocess.CompletedProcess(
            a, native, _preflight_bytes(ADOMD_BLOCKER) if native == 1 else _preflight_bytes(), b""
        )

    monkeypatch.setattr(t2p.subprocess, "run", child)
    assert t2p.main(arguments) == expected
    result, _ = _json(capsys)
    result.pop("doctor_details")
    checks = [
        {
            "key": key,
            "state": "set" if index < 4 else "missing",
            "source": "environment" if index < 4 else "absent",
            "required": key in {"TABLEAU_SERVER_URL", "TABLEAU_PAT_NAME", "TABLEAU_PAT_SECRET"},
            "secret": key in {"TABLEAU_PAT_SECRET", "TABLEAU_DBX_TOKEN", "TABLEAU_SF_PASSWORD"},
            "reason": "set" if index < 4 else "missing",
            "remedy": "none",
        }
        for index, key in enumerate(
            (
                "TABLEAU_SERVER_URL",
                "TABLEAU_SITE",
                "TABLEAU_PAT_NAME",
                "TABLEAU_PAT_SECRET",
                "TABLEAU_REST_API_VERSION",
                "TABLEAU_PRODUCT_VERSION",
                "TABLEAU_DBX_TOKEN",
                "TABLEAU_SF_PASSWORD",
                "TABLEAU_SF_USER",
            )
        )
    ]
    rows = {row["key"]: row for row in checks}
    if configuration == "missing":
        rows["TABLEAU_PAT_NAME"].update(
            state="missing", source="absent", reason="missing", remedy="resolve_configuration"
        )
    elif configuration in {"blank", "conflict"}:
        rows["TABLEAU_PAT_SECRET"].update(state=configuration, reason=configuration, remedy="resolve_configuration")
    elif configuration == "default_site":
        rows["TABLEAU_SITE"].update(source="default", reason="default_site")
    baseline = {
        "schema_version": "t2p/v1",
        "command": "doctor",
        "status": status,
        "exit_code": expected,
        "authority": "preflight.ps1+tableau_env.py",
        "delegated_exit_code": None,
        "stages": [
            {
                "stage": "preflight",
                "owner": "preflight.ps1",
                "native_exit_code": native,
                "state": "ok" if native == 0 else "cannot_establish" if native is None else "failed",
                "artifact": None,
            }
        ],
        "checks": checks,
        "evidence": [],
        "artifacts": [],
        "run_location": None,
        "runs_root": None,
        "run_location_reason": "not_applicable_no_run",
        "path_report": None,
        "limitations": [reason, "presence_only_not_authentication"],
        "proof_boundary": {
            "established": ["configuration_presence", "plain_preflight_result"],
            "not_established": [
                "credential_validity",
                "source_reachability",
                "path_safety",
                "process_liveness",
                "START_READY",
                "fidelity",
                "COMPLETE",
                "deployability",
            ],
        },
    }
    assert json.dumps(result, sort_keys=True).encode() == json.dumps(baseline, sort_keys=True).encode(), (
        "every pre-existing JSON field/record must remain byte-equivalent"
    )


def test_doctor_progress_is_fixed_and_flushed_before_child(monkeypatch, tmp_path, capsys):
    """The child must observe the fixed progress write and flush before it starts."""
    _config(monkeypatch)
    monkeypatch.setattr(t2p, "ROOT", tmp_path)
    writes, events = [], []

    def child(*a, **_kwargs):
        assert "".join(writes) == "Checking existing preflight; no upgrades or Tableau login.\n"
        assert events == ["flush"], "fixed progress must flush before waiting for the child"
        return subprocess.CompletedProcess(a, 0, _preflight_bytes(), b"")

    monkeypatch.setattr(t2p.subprocess, "run", child)
    with monkeypatch.context() as streams:
        streams.setattr(sys, "stderr", SimpleNamespace(write=writes.append, flush=lambda: events.append("flush")))
        assert t2p.main(["doctor", "--json"]) == 0
    _json(capsys)


@pytest.mark.parametrize("json_mode", [False, True])
def test_doctor_syntax_is_safe_and_noninteractive(capsys, json_mode):
    """Reject unknown arguments without reflecting private values or control text."""
    assert t2p.main(["doctor", "--PRIVATE_\x1b[31mARG", *(["--json"] if json_mode else [])]) == 2
    captured = capsys.readouterr()
    assert "PRIVATE_" not in captured.out + captured.err and "\x1b" not in captured.out + captured.err
    if json_mode:
        assert json.loads(captured.out)["status"] == "error"
        assert len(captured.out.splitlines()) == 1
    else:
        assert captured.out.startswith("DOCTOR error (exit 2)\n")


def test_doctor_channels_remain_distinct(monkeypatch, tmp_path, capsys):
    """Catch channel reversals on the intended assertion, before a JSON decoder can fail."""
    _config(monkeypatch)
    monkeypatch.setattr(t2p, "ROOT", tmp_path)
    monkeypatch.setattr(
        t2p.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, _preflight_bytes(), b"")
    )
    assert t2p.main(["doctor", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("{") and captured.out.endswith("}\n") and len(captured.out.splitlines()) == 1, (
        "JSON stdout must be one object and newline"
    )
    assert json.loads(captured.out)["status"] == "READY_TO_ATTEMPT"
    assert t2p.main(["doctor"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("DOCTOR READY_TO_ATTEMPT (exit 0)\n"), (
        "human doctor must render a verdict, not the JSON envelope"
    )


def test_doctor_details_never_extend_start_or_status(monkeypatch, tmp_path, capsys):
    """Reach both unchanged owners, not just the common envelope constructor."""
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    _config(monkeypatch)
    monkeypatch.setattr(t2p, "ROOT", pipeline.root)
    assert t2p.main(["start", "--project", "Finance", "--json"]) == 0
    output, _ = _captured(capsys, pipeline)
    result = json.loads(output)
    assert "doctor_details" not in result
    assert t2p.main(["status", "--run", result["run_location"]["physical_run"], "--json"]) in {0, 1}
    result, _ = _json(capsys)
    assert "doctor_details" not in result
