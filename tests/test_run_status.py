"""Tests for the read-only run_status CLI."""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_status.py"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run(tmp_path: Path, name: str = "001-estate") -> Path:
    run = tmp_path / "one" / "_runs" / name
    for subdir in ("assessment", "assets", "bundle", "oracle", "packages", "scratch"):
        (run / subdir).mkdir(parents=True, exist_ok=True)
    _write_json(
        run / "run.json",
        {
            "run": 1,
            "unit_key": "estate",
            "status": "active",
            "allocated_dir_name": name,
            "allocated_abs_path": str(run),
        },
    )
    return run


def _package(root: Path, unit: str, kind: str, **updates: object) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("synthetic package\n", encoding="utf-8")
    digest = hashlib.sha256((root / "README.md").read_bytes()).hexdigest()
    manifest: dict[str, object] = {
        "unit": unit,
        "kind": kind,
        "construction_status": "ASSEMBLED",
        "self_contained": False,
        "has_engine_working_copy": False,
        "model_binding": {"state": "unbound"},
        "oracle": {"reference_required": True, "partial": True},
        "dispatch_readiness": {"status": "COMPLETE", "recorded_at": "2026-01-01T00:00:00Z"},
        "contents": {"files": {"README.md": digest}},
    }
    manifest.update(updates)
    _write_json(root / "package-manifest.json", manifest)


def _invoke(run: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--run", str(run), *extra],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_real_cli_reports_mixed_run_without_certifying_readiness(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write_json(
        run / "bundle" / "report.json",
        {"workbooks": [{"name": "Book"}], "datasources": [{"name": "SharedData"}]},
    )
    (run / "bundle" / "pbip" / "Book").mkdir(parents=True)
    (run / "bundle" / "pbip" / "PbipOnly").mkdir(parents=True)
    (run / "bundle" / "handover").mkdir()
    _write_json(run / "bundle" / "handover" / "Book.json", {"workbook": {"name": "Book"}})
    _write_json(
        run / "bundle" / "source-provenance.json",
        {"phase": {"status": "failed", "errors": [{"code": "collect-inputs-failed", "operation": "collect"}]}},
    )
    _package(run / "packages" / "Book", "Book", "workbook", has_engine_working_copy=True)
    _package(run / "packages" / "Recovered", "Recovered", "workbook")
    (run / "packages" / "Bad").mkdir()
    (run / "packages" / "Bad" / "package-manifest.json").write_text("{", encoding="utf-8")
    before = _hash_tree(run)

    json_result = _invoke(run, "--json")
    human_result = _invoke(run)

    assert json_result.returncode == 0, json_result.stderr + json_result.stdout
    assert human_result.returncode == 0, human_result.stderr + human_result.stdout
    assert before == _hash_tree(run), "run_status wrote to the synthetic run"
    payload = json.loads(json_result.stdout)
    assert payload["selected_run"] == str(run)
    assert payload["run_recorded_status_semantics"].startswith("allocation metadata")
    assert payload["current_certification"] == "NOT_CHECKED"
    assert payload["inventory_scope"] == "established"
    assert {(unit["kind"], unit["unit"]) for unit in payload["units"]} == {
        ("workbook", "Book"),
        ("datasource", "SharedData"),
        ("unknown", "PbipOnly"),
    }
    book = next(unit for unit in payload["units"] if unit["unit"] == "Book")
    assert book["observations"]["generated_working_copy"] == "present"
    assert book["observations"]["handover"] == "present"
    assert book["package"]["stored_readiness"]["current_certification"] == "NOT_CHECKED"
    assert {package["relative_path"] for package in payload["unscoped_packages"]} == {
        "packages/Bad",
        "packages/Recovered",
    }
    assert payload["recorded_failures"] == [
        {"source": "bundle/source-provenance.json", "phase": "collect", "code": "collect-inputs-failed"}
    ]
    assert payload["next_action"]["headline"].startswith("Inspect the recorded failed phase")
    assert f"units: {len(payload['units'])}" in human_result.stdout
    assert f"unscoped_packages: {len(payload['unscoped_packages'])}" in human_result.stdout
    assert "UNSCOPED_PACKAGE packages/Recovered" in human_result.stdout
    assert "current_certification: NOT_CHECKED" in human_result.stdout


def test_cli_reads_only_the_explicit_absolute_run_when_numbers_repeat(tmp_path: Path) -> None:
    selected = _run(tmp_path / "selected")
    other = _run(tmp_path / "other")
    _write_json(selected / "bundle" / "report.json", {"workbooks": [{"name": "Chosen"}], "datasources": []})
    _write_json(other / "bundle" / "report.json", {"workbooks": [{"name": "Foreign"}], "datasources": []})

    result = _invoke(selected, "--json")

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert [unit["unit"] for unit in payload["units"]] == ["Chosen"]
    assert "Foreign" not in result.stdout


def test_missing_or_malformed_report_unestablishes_inventory_but_preserves_package_observations(tmp_path: Path) -> None:
    run = _run(tmp_path)
    (run / "bundle" / "report.json").write_text("{", encoding="utf-8")
    _package(run / "packages" / "Recovered", "Recovered", "workbook")

    result = _invoke(run, "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["inventory_scope"] == "unestablished"
    assert payload["units"] == []
    assert payload["unscoped_packages"][0]["relative_path"] == "packages/Recovered"
    assert any(finding["code"] == "ENGINE_REPORT_UNESTABLISHED" for finding in payload["findings"])


def test_duplicate_and_kind_mismatch_do_not_safely_associate_a_package(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write_json(
        run / "bundle" / "report.json",
        {"workbooks": [{"name": "Dup"}, {"name": "Dup"}], "datasources": [{"name": "Conflict"}]},
    )
    _package(run / "packages" / "Dup", "Dup", "workbook")
    _package(run / "packages" / "Conflict", "Conflict", "workbook")

    result = _invoke(run, "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert any(unit["unit"] == "Dup" and unit["scope"] == "ambiguous" for unit in payload["units"])
    assert {package["unit"] for package in payload["unscoped_packages"]} == {"Dup", "Conflict"}
    assert any(finding["code"] == "AMBIGUOUS_UNIT_IDENTITY" for finding in payload["findings"])


def test_duplicate_packages_for_one_unit_are_not_silently_dropped(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write_json(run / "bundle" / "report.json", {"workbooks": [{"name": "Book"}], "datasources": []})
    _package(run / "packages" / "Book", "Book", "workbook", construction_status="ASSEMBLED")
    _package(run / "packages" / "batch" / "BookCopy", "Book", "workbook", construction_status="ASSEMBLED")

    result = _invoke(run, "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    scoped = next(unit for unit in payload["units"] if unit["unit"] == "Book")["package"]
    assert scoped["relative_path"] == "packages/Book"
    assert [package["relative_path"] for package in payload["unscoped_packages"]] == ["packages/batch/BookCopy"]
    assert any(finding["code"] == "DUPLICATE_PACKAGE_ASSOCIATION" for finding in payload["findings"])


def test_moved_or_malformed_run_record_is_nonzero_before_child_inventory(tmp_path: Path) -> None:
    run = _run(tmp_path, "001-original")
    moved = run.rename(run.parent / "001-moved")
    _write_json(moved / "bundle" / "report.json", {"workbooks": [{"name": "ShouldNotMatter"}], "datasources": []})

    result = _invoke(moved, "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["run_location"]["state"] == "moved"
    assert "units" not in payload
    assert payload["next_action"]["headline"].startswith("Confirm the explicitly selected run")


def test_absolute_run_path_is_required(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--run", "_runs/001-estate", "--json"],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "--run must be an absolute path"


def test_stored_complete_remains_last_observed_after_file_edit(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write_json(run / "bundle" / "report.json", {"workbooks": [{"name": "Book"}], "datasources": []})
    _package(run / "packages" / "Book", "Book", "workbook")
    first = json.loads(_invoke(run, "--json").stdout)
    (run / "packages" / "Book" / "README.md").write_text("edited package\n", encoding="utf-8")

    second_result = _invoke(run, "--json")

    assert second_result.returncode == 0
    second = json.loads(second_result.stdout)
    for payload in (first, second):
        package = payload["units"][0]["package"]
        assert package["stored_readiness"]["last_observed"]["status"] == "COMPLETE"
        assert package["stored_readiness"]["current_certification"] == "NOT_CHECKED"
    assert second["units"][0]["package"]["integrity_status"] == "findings"


def test_run_status_source_has_no_process_network_or_write_calls() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    forbidden = ["subprocess", "socket", "requests", "urllib", ".write_text(", ".write_bytes(", "open("]
    assert all(token not in source for token in forbidden)
    assert "allocate_run" not in source
