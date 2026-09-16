"""Boundary -> producer-shaped reader -> normalized rendering -> exit controls for run_status."""

from __future__ import annotations

# pylint: disable=missing-function-docstring,protected-access

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_status.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run_status as rs  # noqa: E402  # pylint: disable=wrong-import-position
import stamp_tableau_provenance as prov  # noqa: E402  # pylint: disable=wrong-import-position
from bundle_corpus import is_reparse_entry  # noqa: E402  # pylint: disable=wrong-import-position


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
    run = tmp_path / "_runs" / name
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


def _report(run: Path, workbooks: tuple[str, ...] = ("Book",), datasources: tuple[str, ...] = ()) -> dict:
    report = {
        "workbooks": [{"name": name} for name in workbooks],
        "datasources": [{"name": name} for name in datasources],
    }
    _write_json(run / "bundle" / "report.json", report)
    return report


def _package(root: Path, unit: str = "Book", kind: str = "workbook", **updates: object) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("synthetic package\n", encoding="utf-8")
    manifest = {
        "unit": unit,
        "kind": kind,
        "construction_status": "ASSEMBLED",
        "self_contained": False,
        "has_engine_working_copy": False,
        "model_binding": {"kind": "no_report", "path": None, "resolves_in_package": True},
        "data_sources": {"binding": {"state": "unbound"}},
        "oracle": {"objects": [], "omissions": [], "route": None, "reason": "no oracle capture supplied"},
        "dispatch_readiness": {"availability": "UNAVAILABLE", "status": "NOT_EVALUATED"},
        "contents": {"files": {"README.md": hashlib.sha256((root / "README.md").read_bytes()).hexdigest()}},
    }
    manifest.update(updates)
    _write_json(root / "package-manifest.json", manifest)
    return manifest


def _invoke(
    run: Path | str, *extra: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """The documented command. With `cwd`, the documented absolute `<toolkit>\\scripts` spelling."""
    script = str(SCRIPT) if cwd is not None else str(Path("scripts") / "run_status.py")
    return subprocess.run(
        [sys.executable, "-B", script, "--run", str(run), *extra],
        cwd=cwd or REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )


def _human_payload(text: str) -> dict:
    """Decode every displayed record, not selected substrings; counts independently check multiplicity."""
    lines = text.splitlines()
    assert lines.pop(0) == "RUN STATUS: DIAGNOSTIC ONLY (NOT readiness)"
    payload: dict[str, Any] = {}
    counts: dict[str, int] = {}
    key = ""
    for line in lines:
        if line.startswith("    path: "):
            shown = line.removeprefix("    path: ")
            record = payload["locations"][-1]
            assert "path" not in record, "each displayed location carries exactly one native path line"
            record["path"] = None if shown == rs.PATH_WITHHELD else shown
            continue
        if line.startswith("  "):
            payload[key].append(json.loads(line))
            continue
        key, value = line.split(": ", 1)
        key = "next_action" if key == "NEXT ACTION" else key
        key = "locations" if key == "LOCATIONS" else key
        assert payload or key == "locations", "the Locations block is displayed first"
        parsed = json.loads(value)
        if key in {"locations", "units", "unscoped_packages", "recorded_phases", "recorded_failures", "findings"}:
            counts[key] = parsed
            payload[key] = []
        else:
            payload[key] = parsed
    assert all(len(payload[key]) == count for key, count in counts.items())
    return payload


def _both(run: Path | str, expected: int) -> dict:
    machine = _invoke(run, "--json")
    human = _invoke(run)
    assert machine.returncode == expected, machine.stderr + machine.stdout
    assert human.returncode == expected, human.stderr + human.stdout
    payload = json.loads(machine.stdout)
    assert _human_payload(human.stdout) == payload
    assert payload["current_certification"] == "NOT_CHECKED"
    return payload


def test_assessable_mixed_inventory_and_real_cli_are_read_only(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _report(run, datasources=("SharedData",))
    (run / "bundle" / "pbip" / "Book").mkdir(parents=True)
    (run / "bundle" / "pbip" / "PbipOnly").mkdir()
    _write_json(run / "bundle" / "handover" / "Book.json", {"workbook": {"name": "Book"}})
    _package(run / "packages" / "Book")
    _package(run / "packages" / "Recovered", "Recovered")
    _write_json(
        run / "bundle" / "source-provenance.json",
        {
            "phase": {"status": "failed", "errors": [{"code": "collect-inputs-failed", "operation": "collect-inputs"}]},
        },
    )
    before = _hash_tree(run)

    payload = _both(run, 0)

    assert before == _hash_tree(run)
    assert payload["inventory_scope"] == "established"
    assert {(unit["kind"], unit["unit"]) for unit in payload["units"]} == {
        ("workbook", "Book"),
        ("datasource", "SharedData"),
        ("unknown", "PbipOnly"),
    }
    book = next(unit for unit in payload["units"] if unit["unit"] == "Book")
    assert book["generated_working_copy"] == "present"
    assert book["handover"] == "present"
    assert len(book["occurrences"]) == 2
    assert [package["unit"] for package in payload["unscoped_packages"]] == ["Recovered"]
    assert payload["recorded_failures"][0]["last_observed"]["errors"] == [
        {"code": "collect-inputs-failed", "operation": "collect-inputs"},
    ]
    assert payload["next_action"]["headline"].startswith("Inspect the recorded failed phase")
    assert payload["next_action"]["affected_units"] == ["Book", "PbipOnly", "Recovered", "SharedData"]


def test_explicit_empty_collections_are_empty_diagnostics_not_readiness(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _report(run, ())
    payload = _both(run, 0)
    assert payload["units"] == []
    assert payload["inventory_scope"] == "established"
    assert payload["child_evidence"]["bundle/source-provenance.json"] == "missing"


def test_only_the_explicit_run_is_read_when_numbers_repeat(tmp_path: Path) -> None:
    selected, other = _run(tmp_path / "selected"), _run(tmp_path / "other")
    _report(selected, ("Chosen",))
    _report(other, ("Foreign",))
    payload = _both(selected, 0)
    assert [unit["unit"] for unit in payload["units"]] == ["Chosen"]
    assert "Foreign" not in json.dumps(payload)


@pytest.mark.parametrize(
    "document",
    [
        None,
        "{",
        {},
        {"workbooks": []},
        {"datasources": []},
        {"workbooks": [], "datasources": None},
        {"workbooks": "wrong", "datasources": []},
    ],
)
def test_unestablished_report_never_erases_package_only_work(tmp_path: Path, document: Any) -> None:
    run = _run(tmp_path)
    if document == "{":
        (run / "bundle" / "report.json").write_text("{", encoding="utf-8")
    elif document is not None:
        _write_json(run / "bundle" / "report.json", document)
    _package(run / "packages" / "Recovered", "Recovered")
    payload = _both(run, 1)
    assert payload["inventory_scope"] == "unestablished"
    assert payload["unscoped_packages"][0]["unit"] == "Recovered"
    assert payload["unscoped_packages"][0]["scope"] == "UNSCOPED_PACKAGE"
    assert any(finding["unassessable"] for finding in payload["findings"])


def test_partial_report_retains_readable_occurrences(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _write_json(run / "bundle" / "report.json", {"workbooks": [{"name": "Book"}]})
    payload = _both(run, 1)
    assert payload["units"][0]["occurrences"] == [
        {"source": "engine_report", "unit": "Book", "kind": "workbook", "status": "observed"},
    ]


@pytest.mark.parametrize("kind", ["PRIVATE_INVALID_KIND", "unknown", None, ["workbook"]])
def test_invalid_package_kind_cannot_associate_with_a_working_copy_only_unit(tmp_path: Path, kind: Any) -> None:
    run = _run(tmp_path)
    _report(run, ())
    (run / "bundle" / "pbip" / "Recovered").mkdir(parents=True)
    _package(run / "packages" / "Recovered", "Recovered", kind)

    payload = _both(run, 1)

    unit = payload["units"][0]
    assert unit["kind"] == "unknown" and unit["scope"] == "working_copy_only"
    assert unit["package"] is None, "an unestablished package kind must never establish an association"
    package = payload["unscoped_packages"][0]
    assert package["kind"] is None, "the display sentinel is not a package identity"
    assert package["unit"] == "Recovered" and package["scope"] == "UNSCOPED_PACKAGE"
    assert any(
        finding["code"] == "UNSCOPED_PACKAGE" and finding["reason"] == "preserve_and_inspect_association"
        for finding in payload["findings"]
    )
    assert "preserve retained work" in payload["next_action"]["headline"]
    assert "PRIVATE_INVALID_KIND" not in json.dumps(payload)


@pytest.mark.parametrize("kind", ["workbook", "datasource"])
def test_valid_package_kinds_remain_associated_or_retained_package_only(tmp_path: Path, kind: str) -> None:
    run = _run(tmp_path)
    _report(run, ("Book",) if kind == "workbook" else (), ("Book",) if kind == "datasource" else ())
    _package(run / "packages" / "Book", "Book", kind)
    _package(run / "packages" / "Recovered", "Recovered", kind)

    payload = _both(run, 0)

    assert payload["units"][0]["package"]["kind"] == kind
    assert payload["units"][0]["package"]["scope"] == "associated"
    package = payload["unscoped_packages"][0]
    assert (package["unit"], package["kind"], package["scope"]) == ("Recovered", kind, "UNSCOPED_PACKAGE")
    assert payload["next_action"]["headline"].startswith("Preserve package-only or ambiguous package work")


def test_package_join_requires_an_accepted_kind_not_a_display_sentinel() -> None:
    package = rs.PackageObservation("packages/Recovered", "Recovered", "unknown")
    findings: list[rs.Finding] = []

    units, unscoped = rs._assemble_units(
        [rs.Occurrence("working_copy", "Recovered", "unknown", "present")],
        [package],
        findings,
        pbip_state="present",
        handover_state="missing",
        handovers={},
    )

    assert units[0].package is None, "the package join must require a KNOWN_KINDS member"
    assert unscoped == [package] and package.scope == "UNSCOPED_PACKAGE"
    assert findings[0].code == "UNSCOPED_PACKAGE"


@pytest.mark.parametrize("cross_kind", [False, True])
def test_duplicate_and_cross_kind_identities_preserve_occurrences_without_association(
    tmp_path: Path, cross_kind: bool
) -> None:
    run = _run(tmp_path)
    _report(run, ("Dup",) if cross_kind else ("Dup", "Dup"), ("Dup",) if cross_kind else ())
    _package(run / "packages" / "Dup", "Dup")
    payload = _both(run, 0)
    assert sum(len(unit["occurrences"]) for unit in payload["units"]) == 2
    assert all(unit["scope"] == "ambiguous" and unit["package"] is None for unit in payload["units"])
    assert payload["unscoped_packages"][0]["unit"] == "Dup"


def test_duplicate_packages_never_silently_choose_the_first_one(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _report(run)
    _package(run / "packages" / "Book")
    _package(run / "packages" / "batch" / "Book")
    payload = _both(run, 0)
    assert payload["units"][0]["package"] is None
    assert {package["relative_path"] for package in payload["unscoped_packages"]} == {
        "packages/Book",
        "packages/batch/Book",
    }


@pytest.mark.parametrize(
    "spelling",
    [
        "_runs/001-estate",
        r"\\invalid.example\share\_runs\001-estate",
        "//invalid.example/share/_runs/001-estate",
        r"\/invalid.example\share\001-estate",
        r"\\?\UNC\invalid.example\share\001-estate",
        r"\\?\C:\_runs\001-estate",
        r"\\.\C:\_runs\001-estate",
        r"\??\C:\_runs\001-estate",
        r"\Device\Mup\share",
        "file://invalid.example/share",
        "C:relative",
        "C:\\x\\..\\001-estate",
    ],
)
def test_cli_rejects_network_device_and_nonlocal_spellings_before_any_syscall(
    spelling: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("a rejected spelling reached a filesystem syscall")

    with monkeypatch.context() as context:
        context.setattr(rs.os, "lstat", forbidden)
        context.setattr(rs.os, "scandir", forbidden)
        context.setattr(Path, "read_text", forbidden)
        assert rs.main(["--run", spelling, "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert "units" not in payload
    assert payload["findings"][0]["unassessable"]


def _link_directory(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False)
        if result.returncode:
            pytest.skip(f"could not create junction: {result.stderr.decode(errors='replace').strip()}")
    else:
        link.symlink_to(target, target_is_directory=True)
    assert is_reparse_entry(os.lstat(link)), "the negative control must be a real native reparse entry"


@pytest.mark.parametrize(
    "boundary",
    [
        "run",
        "ancestor",
        "bundle",
        "oracle",
        "packages",
        "bundle/pbip",
        "bundle/pbip/Book",
        "bundle/handover",
        "bundle/handover/Book.json",
        "packages/batch",
        "packages/batch/Book",
        "packages/batch/Book/package-manifest.json",
    ],
)
def test_native_reparse_boundaries_stop_all_later_readers(
    tmp_path: Path, boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(tmp_path)
    _report(run)
    (run / "bundle" / "pbip" / "Book").mkdir(parents=True)
    _write_json(run / "bundle" / "handover" / "Book.json", {"workbook": {"name": "Book"}})
    _package(run / "packages" / "batch" / "Book")
    target = tmp_path / "outside"
    link = run if boundary == "run" else run.parent if boundary == "ancestor" else run.joinpath(*boundary.split("/"))
    if link.is_file():
        target.mkdir()
        link.rename(target / "original.json")
    else:
        link.rename(target)
    _write_json(target / "report.json", {"workbooks": [{"name": "OUTSIDE_SENTINEL"}], "datasources": []})
    _link_directory(link, target)
    lstat = os.lstat

    def no_descendant(path: Path | str, *args: Any, **kwargs: Any) -> os.stat_result:
        assert link not in Path(path).parents, "reader crossed an already rejected native reparse boundary"
        return lstat(path, *args, **kwargs)

    try:
        # Real CLI output and a syscall interception have independent failure assertions.
        payload = _both(run, 1)
        assert "OUTSIDE_SENTINEL" not in json.dumps(payload)
        with monkeypatch.context() as context:
            context.setattr(rs.os, "lstat", no_descendant)
            observed, code = rs.build_status(run)
        assert code == 1 and observed == payload
    finally:
        if sys.platform == "win32":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.parametrize(
    "relative",
    [
        "bundle",
        "packages",
        "bundle/pbip",
        "bundle/pbip/Book",
        "bundle/handover",
    ],
)
def test_file_in_place_of_directory_is_not_absence(tmp_path: Path, relative: str) -> None:
    run = _run(tmp_path)
    _report(run)
    path = run.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        path.rename(tmp_path / "retained-directory")
    path.write_text("retained non-directory entry", encoding="utf-8")
    payload = _both(run, 1)
    assert any(finding["reason"] == "not_directory" for finding in payload["findings"])


@pytest.mark.parametrize(
    "relative,operation",
    [
        ("bundle/pbip", "scandir"),
        ("bundle/handover", "scandir"),
        ("packages", "scandir"),
        ("bundle/report.json", "read_text"),
        ("bundle/source-provenance.json", "read_text"),
        ("bundle/phase-timings.json", "read_text"),
        ("bundle/handover/Book.json", "read_text"),
        ("packages/Book/package-manifest.json", "read_text"),
    ],
)
def test_unreadable_consumed_inputs_are_nonzero_and_never_echo_exceptions(
    tmp_path: Path, relative: str, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(tmp_path)
    _report(run)
    (run / "bundle" / "pbip").mkdir()
    _write_json(run / "bundle" / "handover" / "Book.json", {"workbook": {"name": "Book"}})
    _write_json(run / "bundle" / "source-provenance.json", {"phase": {"status": "local_only", "errors": []}})
    _write_json(run / "bundle" / "phase-timings.json", {"phases": []})
    _package(run / "packages" / "Book")
    rejected = run.joinpath(*relative.split("/"))
    owner = rs.os if operation == "scandir" else Path
    original = getattr(owner, operation)

    def denied(path: Path | str, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == rejected:
            raise PermissionError("PRIVATE_EXCEPTION_SENTINEL")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(owner, operation, denied)
        payload, code = rs.build_status(run)
    assert code == 1
    assert any(finding["unassessable"] for finding in payload["findings"])
    assert "PRIVATE_EXCEPTION_SENTINEL" not in json.dumps(payload)
    assert _human_payload(rs.render_human(payload)) == payload
    if relative in {"bundle/pbip", "bundle/handover"}:
        assert payload["child_evidence"][relative] == "unassessable:directory_unreadable"


@pytest.mark.parametrize(
    "name,document",
    [
        ("source-provenance.json", "{"),
        ("source-provenance.json", {}),
        ("source-provenance.json", {"phase": {"status": [], "errors": []}}),
        ("source-provenance.json", {"phase": {"status": {}, "errors": []}}),
        ("source-provenance.json", {"phase": {"status": "failed", "errors": "wrong"}}),
        ("source-provenance.json", {"phase": {"status": "failed", "errors": [None]}}),
        ("phase-timings.json", "{"),
        ("phase-timings.json", {"phases": None}),
        ("phase-timings.json", {"phases": [None]}),
        ("phase-timings.json", {"phases": [{"phase": "engine_run", "exit_code": True}]}),
    ],
)
def test_malformed_phase_evidence_is_not_an_empty_failure_list(tmp_path: Path, name: str, document: Any) -> None:
    run = _run(tmp_path)
    _report(run)
    path = run / "bundle" / name
    if document == "{":
        path.write_text("{", encoding="utf-8")
    else:
        _write_json(path, document)
    payload = _both(run, 1)
    assert payload["next_action"]["headline"].startswith("Inspect unassessable evidence")


def test_valid_success_and_known_failed_phase_records_are_observations(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _report(run)
    _write_json(run / "bundle" / "source-provenance.json", {"phase": {"status": "local_only", "errors": []}})
    _write_json(
        run / "bundle" / "phase-timings.json",
        {
            "phases": [
                {"phase": "engine_run", "exit_code": 0, "elapsed_sec": 12.5, "started_wall": 0},
                {"phase": "provenance", "status": "failed", "elapsed_sec": 3},
            ]
        },
    )
    payload = _both(run, 0)
    assert len(payload["recorded_phases"]) == 3
    assert len(payload["recorded_failures"]) == 1
    assert payload["recorded_phases"][1]["last_observed"]["started_at"] == "1970-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    "code,operation",
    [
        ("build-failed", "build"),
        ("live-lookup-failed", "lookup-origin"),
        ("content-unavailable", "download-workbook"),
    ],
)
def test_producer_failure_codes_preserve_the_recorded_failed_phase_action(
    tmp_path: Path, code: str, operation: str
) -> None:
    run = _run(tmp_path)
    _report(run, ())
    produced = prov.failure_result(code, operation, RuntimeError("PRIVATE_EXCEPTION_SENTINEL"))
    _write_json(run / "bundle" / "source-provenance.json", produced)

    payload = _both(run, 0)

    assert payload["recorded_failures"] == payload["recorded_phases"]
    assert payload["recorded_failures"][0]["last_observed"] == {
        "status": "failed",
        "errors": [{"code": code, "operation": operation}],
    }
    assert payload["next_action"]["headline"].startswith("Inspect the recorded failed phase")
    assert payload["findings"] == []
    assert "PRIVATE_EXCEPTION_SENTINEL" not in json.dumps(payload)


@pytest.mark.parametrize(
    "overrides,expected_code",
    [
        (None, "result-not-a-mapping"),
        ({"inputs": None}, "inputs-not-a-list"),
        ({"input_count": None}, "input-count-not-an-integer"),
        ({"input_count": -1}, "input-count-negative"),
        ({"input_count": 1}, "input-count-mismatch"),
        ({"phase": None}, "phase-status-unassessable"),
        ({"phase": {"status": "success", "errors": []}}, "success-without-inputs"),
    ],
)
def test_producer_normalization_failures_remain_assessable_diagnostics(
    tmp_path: Path, overrides: dict | None, expected_code: str
) -> None:
    run = _run(tmp_path)
    _report(run, ())
    raw = None if overrides is None else {**prov.phase_result([], "failed"), **overrides}
    produced = prov.normalize_result(raw)
    assert [error["code"] for error in produced["phase"]["errors"]] == [expected_code]
    _write_json(run / "bundle" / "source-provenance.json", produced)

    payload = _both(run, 0)

    assert payload["recorded_failures"][0]["last_observed"] == {
        "status": "failed",
        "errors": [{"code": expected_code, "operation": "validate-result"}],
    }
    assert payload["next_action"]["headline"].startswith("Inspect the recorded failed phase")
    assert payload["findings"] == []


@pytest.mark.parametrize("field", ["code", "operation"])
def test_unknown_code_like_producer_error_values_are_withheld(tmp_path: Path, field: str) -> None:
    run = _run(tmp_path)
    _report(run, ())
    error = prov.phase_error("build-failed", "build")
    error[field] = "private-unknown-sentinel"
    _write_json(run / "bundle" / "source-provenance.json", prov.phase_result([], "failed", [error]))

    payload = _both(run, 1)

    assert payload["recorded_failures"][0]["last_observed"]["errors"] == [{**error, field: "unknown"}]
    assert payload["next_action"]["headline"].startswith("Inspect unassessable evidence")
    assert "private-unknown-sentinel" not in json.dumps(payload)


@pytest.mark.parametrize(
    "field_path",
    [
        ("run", "status"),
        ("run", "allocated_dir_name"),
        ("run", "allocated_abs_path"),
        ("package", "construction_status"),
        ("package", "self_contained"),
        ("package", "has_engine_working_copy"),
        ("package", "model_binding", "kind"),
        ("package", "model_binding", "state"),
        ("package", "model_binding", "resolves_in_package"),
        ("package", "data_sources", "binding", "state"),
        ("package", "dispatch_readiness", "status"),
        ("package", "dispatch_readiness", "availability"),
        ("package", "dispatch_readiness", "recorded_at"),
        ("package", "dispatch_readiness", "checked_at"),
        ("package", "start_ready"),
        ("package", "complete"),
        ("package", "oracle", "objects"),
        ("provenance", "phase", "status"),
        ("provenance", "phase", "errors", 0, "code"),
        ("provenance", "phase", "errors", 0, "operation"),
        ("timings", "phases", 0, "phase"),
        ("timings", "phases", 0, "status"),
        ("timings", "phases", 0, "exit_code"),
        ("timings", "phases", 0, "elapsed_sec"),
        ("timings", "phases", 0, "started_wall"),
    ],
)
def test_unknown_values_in_every_projected_family_are_closed_not_echoed(tmp_path: Path, field_path: tuple) -> None:
    run = _run(tmp_path)
    _report(run)
    manifest = _package(run / "packages" / "Book")
    paths = {
        "run": run / "run.json",
        "package": run / "packages" / "Book" / "package-manifest.json",
        "provenance": run / "bundle" / "source-provenance.json",
        "timings": run / "bundle" / "phase-timings.json",
    }
    documents = {
        "run": json.loads(paths["run"].read_text(encoding="utf-8")),
        "package": manifest,
        "provenance": {"phase": {"status": "failed", "errors": [{"code": "deadline-expired", "operation": "phase"}]}},
        "timings": {"phases": [{"phase": "engine_run", "exit_code": 0}]},
    }
    row = documents[field_path[0]]
    for key in field_path[1:-1]:
        row = row[key]
    row[field_path[-1]] = "PRIVATE_RAW_SENTINEL"
    for key, document in documents.items():
        _write_json(paths[key], document)
    payload = _both(run, 1)
    assert "PRIVATE_RAW_SENTINEL" not in json.dumps(payload)


def test_freeform_objects_are_not_copied_and_all_stored_statuses_survive(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _report(run)
    _package(
        run / "packages" / "Book",
        dispatch_readiness={
            "status": "START_READY",
            "recorded_at": "2026-01-01T01:00:00+01:00",
            "token": "PRIVATE_RAW_SENTINEL",
            "message": "PRIVATE_RAW_SENTINEL",
            "nested": {"path": "PRIVATE_RAW_SENTINEL"},
        },
        start_ready="START_READY",
        complete={"status": "COMPLETE", "command": "PRIVATE_RAW_SENTINEL"},
    )
    payload = _both(run, 0)
    observations = payload["units"][0]["package"]["stored_readiness"]
    assert [row["source"] for row in observations] == ["dispatch_readiness", "start_ready", "complete"]
    assert observations[0]["last_observed"] == {"status": "START_READY", "recorded_at": "2026-01-01T00:00:00+00:00"}
    assert all(row["current_certification"] == "NOT_CHECKED" for row in observations)
    assert "PRIVATE_RAW_SENTINEL" not in json.dumps(payload)


def test_later_stored_complete_cannot_hide_malformed_earlier_evidence(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _report(run)
    _package(run / "packages" / "Book", dispatch_readiness={"status": []}, complete="COMPLETE")
    payload = _both(run, 1)
    observations = payload["units"][0]["package"]["stored_readiness"]
    assert [row["last_observed"]["status"] for row in observations] == ["unknown", "COMPLETE"]


@pytest.mark.parametrize(
    "text", ["Book\nNEXT ACTION: DELETE EVERYTHING", "Book\rFORGED", "Book\x1b[31m", "Book\u202eFORGED"]
)
def test_control_bearing_names_do_not_forge_human_lines(tmp_path: Path, text: str) -> None:
    run = _run(tmp_path)
    _write_json(run / "bundle" / "report.json", {"workbooks": [{"name": text}], "datasources": []})
    payload = _both(run, 1)
    assert payload["units"] == []
    assert "DELETE EVERYTHING" not in json.dumps(payload)
    human = _invoke(str(run) + text).stdout
    assert sum(line.startswith("NEXT ACTION:") for line in human.splitlines()) == 1
    assert "\x1b" not in human and "\u202e" not in human
    assert _human_payload(human)["current_certification"] == "NOT_CHECKED"


@pytest.mark.parametrize("extra", [(), ("--json",)])
def test_argparse_errors_do_not_echo_control_bearing_arguments(tmp_path: Path, extra: tuple[str, ...]) -> None:
    result = _invoke(tmp_path, *extra, "--PRIVATE_RAW_SENTINEL\nNEXT ACTION: DELETE EVERYTHING")
    assert result.returncode == 2
    assert "PRIVATE_RAW_SENTINEL" not in result.stderr
    assert "DELETE EVERYTHING" not in result.stderr
    assert "invalid arguments; use --help" in result.stderr


def test_package_discovery_ignores_regular_files_and_stops_at_a_marker(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _report(run)
    _package(run / "packages" / "Book")
    (run / "packages" / "README.md").write_text("package grouping directory", encoding="utf-8")
    _write_json(run / "packages" / "packaging.json", {"note": "not a directory"})
    # A nested marker is a package CONTENT, not a second discovery boundary.
    _write_json(run / "packages" / "Book" / "nested" / "package-manifest.json", {})
    payload = _both(run, 0)
    assert payload["unscoped_packages"] == []
    assert not any(finding["code"] == "UNSAFE_PACKAGE_ENTRY" for finding in payload["findings"])
    assert payload["units"][0]["package"]["integrity_codes"] == ["package_file_undeclared"]


@pytest.mark.parametrize("marker", ["invalid_json", "directory"])
def test_malformed_package_markers_remain_visible_without_descending(tmp_path: Path, marker: str) -> None:
    run = _run(tmp_path)
    _report(run, ())
    package = run / "packages" / "Damaged"
    path = package / "package-manifest.json"
    package.mkdir()
    if marker == "directory":
        path.mkdir()
    else:
        path.write_text("{", encoding="utf-8")
    payload = _both(run, 1)
    assert payload["unscoped_packages"][0]["relative_path"] == "packages/Damaged"
    assert payload["unscoped_packages"][0]["integrity_status"] == "unassessable"


@pytest.mark.parametrize("capture", ["present", "omitted", "missing"])
def test_real_package_producer_oracle_contract_reaches_both_renderers(tmp_path: Path, capture: str) -> None:
    import package_unit as producer  # pylint: disable=import-outside-toplevel
    from test_package_unit import _bundle  # pylint: disable=import-outside-toplevel

    run = _run(tmp_path)
    bundle, oracle = _bundle(run)
    _producer_handovers(bundle)
    if capture == "omitted":
        for path in oracle.rglob("*.png"):
            path.unlink()
    produced = producer.package_unit(
        bundle, "Book", run / "packages", oracle_dir=None if capture == "missing" else oracle, assets_dir=run / "assets"
    )
    # The fixture's legacy provenance has no phase. Remove that OPTIONAL synthetic input, rather
    # than claim it is an assessable current producer phase. The produced package stays untouched.
    (bundle / "source-provenance.json").unlink()
    payload = _both(run, 0)
    package = payload["units"][0]["package"]
    reference = package["reference"]
    assert reference["objects"] == len(produced["oracle"]["objects"])
    assert reference["omissions"] == len(produced["oracle"]["omissions"])
    assert reference["status"] == ("recorded_present" if capture == "present" else "recorded_missing")
    assert package["integrity_status"] == "clean"
    assert package["stored_readiness"][0]["last_observed"]["status"] == "NOT_EVALUATED"
    assert "complete" not in reference["status"]


def test_real_datasource_package_has_legitimate_reference_not_applicable(tmp_path: Path) -> None:
    import package_unit as producer  # pylint: disable=import-outside-toplevel
    from test_package_unit_gates import DS_UNIT, _bundle  # pylint: disable=import-outside-toplevel

    run = _run(tmp_path)
    bundle, _oracle, _objects = _bundle(run, covered=None, datasource_only=True)
    _producer_handovers(bundle)
    produced = producer.package_unit(bundle, DS_UNIT, run / "packages", oracle_dir=None, assets_dir=run / "assets")
    provenance = bundle / "source-provenance.json"
    if provenance.exists():
        provenance.unlink()
    payload = _both(run, 0)
    observed = next(unit for unit in payload["units"] if unit["kind"] == "datasource")["package"]
    assert produced["kind"] == "datasource" and produced["artifacts"]["report"] is None
    assert observed["reference"]["status"] == "not_applicable"


def _producer_handovers(bundle: Path) -> None:
    from run_estate import slice_handovers  # pylint: disable=import-outside-toplevel

    report = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
    for workbook in report["workbooks"]:
        path = bundle / "handover" / f"{workbook['name']}.json"
        workbook.update(json.loads(path.read_text(encoding="utf-8"))["workbook"])
    slice_handovers(report, bundle)


def test_punctuated_handover_uses_real_producer_embedded_identity(tmp_path: Path) -> None:
    from run_estate import slice_handovers  # pylint: disable=import-outside-toplevel

    run = _run(tmp_path)
    report = _report(run, ("Book.One",))
    written = slice_handovers(report, run / "bundle")
    assert [path.name for path in written] == ["Book_One.json"]
    payload = _both(run, 0)
    assert payload["units"][0]["handover"] == "present"
    assert payload["units"][0]["handover_sources"] == ["bundle/handover/Book_One.json"]


def test_colliding_producer_filenames_never_associate_the_survivor_with_both_units(tmp_path: Path) -> None:
    from run_estate import slice_handovers  # pylint: disable=import-outside-toplevel

    run = _run(tmp_path)
    report = _report(run, ("Book.One", "Book_One"))
    written = slice_handovers(report, run / "bundle")
    assert len(written) == 2 and len(set(written)) == 1, "real producer collision control"
    payload = _both(run, 0)
    assert {unit["unit"]: unit["handover"] for unit in payload["units"]} == {
        "Book.One": "missing",
        "Book_One": "present",
    }


def test_duplicate_embedded_handover_identity_is_unassessable_not_first_match(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _report(run)
    for name in ("A.json", "B.json"):
        _write_json(run / "bundle" / "handover" / name, {"workbook": {"name": "Book"}})
    payload = _both(run, 1)
    assert payload["units"][0]["handover"] == "ambiguous"
    assert len(payload["units"][0]["handover_sources"]) == 2


def test_misnamed_handover_and_corrupt_handover_are_not_assumed_present(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _report(run)
    _write_json(run / "bundle" / "handover" / "Book.json", {"workbook": {"name": "Other"}})
    payload = _both(run, 0)
    assert payload["units"][0]["handover"] == "missing"
    assert any(finding["code"] == "UNSCOPED_HANDOVER" for finding in payload["findings"])
    (run / "bundle" / "handover" / "Book.json").write_text("{", encoding="utf-8")
    payload = _both(run, 1)
    assert payload["units"][0]["handover"].startswith("unassessable:")


@pytest.mark.parametrize("condition", ["unbound", "references", "edited"])
def test_known_pending_or_changed_work_remains_actionable_without_certification(tmp_path: Path, condition: str) -> None:
    run = _run(tmp_path)
    _report(run)
    _package(
        run / "packages" / "Book",
        data_sources={"binding": {"state": "unbound" if condition == "unbound" else "bound"}},
        complete={"status": "COMPLETE", "recorded_at": "2026-01-01T00:00:00Z"},
    )
    if condition == "edited":
        (run / "packages" / "Book" / "README.md").write_text("retained edits", encoding="utf-8")
    payload = _both(run, 0)
    package = payload["units"][0]["package"]
    assert package["stored_readiness"][-1]["last_observed"]["status"] == "COMPLETE"
    action = payload["next_action"]
    assert action["affected_units"] == ["Book"]
    assert not any(word in action["headline"].lower() for word in ("delete", "rebuild", "reharvest"))
    if condition == "unbound":
        assert "set_data_folder.py --package <absolute-package>" in action["headline"]
    elif condition == "references":
        assert "check_reference_readiness.py <provider-package> <consumer-package>" in action["headline"]
    else:
        assert package["integrity_codes"] == ["package_file_digest_mismatch"]


def test_moved_or_malformed_run_never_reads_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path)
    moved = run.rename(run.parent / "001-moved")
    _report(moved, ("ShouldNotBeRead",))
    payload = _both(moved, 1)
    assert payload["run_location"]["state"] == "moved"
    assert "units" not in payload
    original = os.lstat

    def no_children(path: Path | str, *args: Any, **kwargs: Any) -> os.stat_result:
        assert not (moved in Path(path).parents and Path(path) != moved / "run.json")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(rs.os, "lstat", no_children)
        assert rs.build_status(moved)[1] == 1
    (moved / "run.json").write_text("{", encoding="utf-8")
    assert _both(moved, 1)["findings"][0]["reason"] == "invalid_json"


def test_exact_documented_entrypoint_does_not_write_bytecode_even_outside_the_run(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _report(run)
    for documentation in (REPO_ROOT / "scripts" / "README.md", REPO_ROOT / "_runs" / "README.md"):
        assert "python -B scripts\\run_status.py --run" in documentation.read_text(encoding="utf-8")
    prefix = tmp_path / "fresh-bytecode-prefix"
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["PYTHONPYCACHEPREFIX"] = str(prefix)
    before = _hash_tree(run)
    for extra in ((), ("--json",)):
        result = _invoke(run, *extra, env=env)
        assert result.returncode == 0, result.stderr + result.stdout
    assert not prefix.exists(), "the documented Python startup flag must prevent even import caches"
    assert before == _hash_tree(run)


def test_source_does_not_invoke_network_processes_or_writers() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    forbidden = ["subprocess", "socket", "requests", "urllib", ".write_text(", ".write_bytes(", "open("]
    assert all(token not in source for token in forbidden)
    assert "allocate_run" not in source


def _locations(payload: dict) -> dict[tuple[str, str | None], dict]:
    rows = {(row["name"], row["relative_path"]): row for row in payload["locations"]}
    assert len(rows) == len(payload["locations"]), "each displayed location is addressed exactly once"
    return rows


@pytest.mark.parametrize("from_toolkit", [True, False])
def test_locations_follow_the_selected_run_and_stay_outside_the_toolkit(tmp_path: Path, from_toolkit: bool) -> None:
    # A lookalike parent carrying the toolkit's own directory name is still outside the toolkit.
    run = _run(tmp_path / REPO_ROOT.name / "external")
    other = _run(tmp_path / "other")
    _report(run)
    _report(other, ("Foreign",))
    (run / "oracle").rmdir()
    _package(run / "packages" / "Book")
    before = _hash_tree(run)

    machine = _invoke(run, "--json", cwd=None if from_toolkit else tmp_path)
    human = _invoke(run, cwd=None if from_toolkit else tmp_path)
    assert machine.returncode == 0 and human.returncode == 0, machine.stderr + human.stderr
    payload = json.loads(machine.stdout)
    assert _human_payload(human.stdout) == payload, "the human Locations block carries the same observations"

    rows = _locations(payload)
    assert rows[("toolkit", None)] == {
        "name": "toolkit",
        "path": str(REPO_ROOT),
        "expected": "standard",
        "observed": "present",
        "relationship": "is_toolkit",
        "relative_path": None,
    }
    assert {name: (row["path"], row["observed"], row["relationship"]) for (name, _rel), row in rows.items()} == {
        "toolkit": (str(REPO_ROOT), "present", "is_toolkit"),
        "selected_run": (str(run), "present", "outside_toolkit"),
        "bundle": (str(run / "bundle"), "present", "outside_toolkit"),
        "oracle": (str(run / "oracle"), "missing", "outside_toolkit"),
        "packages": (str(run / "packages"), "present", "outside_toolkit"),
        "package": (str(run / "packages" / "Book"), "present", "outside_toolkit"),
        "package_working_copy": (str(run / "packages" / "Book" / "fabric"), "missing", "outside_toolkit"),
    }
    assert rows[("package", "packages/Book")]["expected"] == "discovered"
    assert rows[("package_working_copy", "packages/Book")]["expected"] == "standard"
    assert not (run / "oracle").exists() and not (run / "packages" / "Book" / "fabric").exists()
    assert before == _hash_tree(run)
    assert str(other) not in machine.stdout and str(other) not in human.stdout


def test_containment_is_component_aware_not_a_string_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toolkit = tmp_path / "toolkit"
    inside = _run(toolkit)
    lookalike = _run(tmp_path / "toolkit-lookalike")
    for run in (inside, lookalike):
        _report(run, ())
    with monkeypatch.context() as context:
        context.setattr(rs, "TOOLKIT_ROOT", toolkit)
        observed = {run: _locations(rs.build_status(run)[0]) for run in (inside, lookalike)}
    assert observed[inside][("selected_run", None)]["relationship"] == "inside_toolkit"
    assert observed[inside][("bundle", None)]["relationship"] == "inside_toolkit"
    assert observed[inside][("toolkit", None)] == {
        "name": "toolkit",
        "path": str(toolkit),
        "expected": "standard",
        "observed": "present",
        "relationship": "is_toolkit",
        "relative_path": None,
    }
    assert str(lookalike).startswith(str(toolkit)), "the fixture is a genuine string prefix"
    assert observed[lookalike][("selected_run", None)]["relationship"] == "outside_toolkit"
    assert observed[lookalike][("packages", None)]["relationship"] == "outside_toolkit"


@pytest.mark.parametrize("condition", ["moved", "malformed", "unsafe_spelling"])
def test_unestablished_identity_offers_no_run_derived_location(tmp_path: Path, condition: str) -> None:
    run = _run(tmp_path)
    _report(run, ("ShouldNotBeRead",))
    if condition == "moved":
        selected: Path | str = run.rename(run.parent / "001-moved")
    elif condition == "malformed":
        (run / "run.json").write_text("{", encoding="utf-8")
        selected = run
    else:
        selected = str(run / "..") + "\\001-estate"

    payload = _both(selected, 2 if condition == "unsafe_spelling" else 1)

    rows = _locations(payload)
    assert set(rows) == {("toolkit", None), ("selected_run", None)}
    assert rows[("toolkit", None)]["path"] == str(REPO_ROOT)
    assert rows[("selected_run", None)] == {
        "name": "selected_run",
        "path": None,
        "expected": "standard",
        "observed": "cannot_establish",
        "relationship": "cannot_establish",
        "relative_path": None,
    }
    assert "bundle" not in {name for name, _relative in rows}
    assert "ShouldNotBeRead" not in json.dumps(payload)


@pytest.mark.parametrize("text", ["run\x1b[31m", "run\nNEXT ACTION: DELETE EVERYTHING", "run\u202eFORGED"])
def test_unprintable_location_paths_are_withheld_from_the_copyable_line(tmp_path: Path, text: str) -> None:
    assert rs._printable(tmp_path / "plain-run") == str(tmp_path / "plain-run")
    assert rs._printable(tmp_path / text) is None
    status = {
        "locations": [
            {
                "name": "package",
                "path": rs._printable(tmp_path / text),
                "expected": "discovered",
                "observed": "present",
                "relationship": "outside_toolkit",
                "relative_path": f"packages/{text}",
            }
        ],
        "current_certification": "NOT_CHECKED",
    }
    human = rs.render_human(status)
    assert f"    path: {rs.PATH_WITHHELD}" in human
    assert "\x1b" not in human and "\u202e" not in human
    assert sum(line.startswith("NEXT ACTION") for line in human.splitlines()) == 0
    assert _human_payload(human) == status
