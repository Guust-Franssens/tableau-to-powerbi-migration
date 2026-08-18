"""The PBIR validity gate delegates to Microsoft's validator - so it must not lie about the result.

Every test here corresponds to a measured failure, not a hypothetical. The most important one is
`test_nonzero_exit_with_no_output_is_error_not_invalid`: during development this module was written
without the `validate` subcommand, so the CLI exited 1 with empty output on EVERY report and the gate
confidently reported "2 of 2 reports FAIL". A gate that always says no is strictly worse than no gate
- it trains its reader to ignore it, and it would have blocked every migration in the repo.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_pbir_valid as cpv  # noqa: E402  # pylint: disable=wrong-import-position

FAIL_TEXT = """Validating report...
ERROR [PBIR_ROLE_REQUIRED_MISSING] Required role "Y" missing for "clusteredColumnChart"
1 error(s), 0 warning(s); result=failed
"""
PASS_TEXT = "No diagnostics.\n0 error(s), 0 warning(s); result=succeeded\n"


class _Proc:
    """Stand-in for `subprocess.CompletedProcess`."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(monkeypatch, proc: _Proc) -> list[list[str]]:
    """Capture the argv the module builds, and return a canned result."""
    seen: list[list[str]] = []

    def _run(argv, **_kwargs):
        seen.append(list(argv))
        return proc

    monkeypatch.setattr(subprocess, "run", _run)
    return seen


def test_invokes_the_validate_subcommand_with_a_positional_path(monkeypatch, tmp_path) -> None:
    """Two measured usage traps: `validate` is required, and the path is POSITIONAL.

    `powerbi-report-author <path>` (no subcommand) exits 1 with no output, and
    `validate --path <p>` fails with INVALID_USAGE. Both look like a failing report.
    """
    seen = _fake_run(monkeypatch, _Proc(0, PASS_TEXT))
    cpv.validate_one(tmp_path / "X.Report", "powerbi-report-author")
    argv = seen[0]
    assert argv[1] == "validate", f"the validate subcommand must be present: {argv}"
    assert str(tmp_path / "X.Report") in argv, "the report path must be passed"
    assert "--path" not in argv, "the path is positional; --path is INVALID_USAGE"


def test_nonzero_exit_with_no_output_is_error_not_invalid(monkeypatch, tmp_path) -> None:
    """The regression that motivated this file: a broken checker must not read as a broken report."""
    _fake_run(monkeypatch, _Proc(1, ""))
    entry = cpv.validate_one(tmp_path / "X.Report", "powerbi-report-author")
    assert entry["status"] == "error", "a diagnostic-free non-zero exit is a TOOLING fault"
    assert entry["status"] != "invalid"


def test_a_real_validation_failure_is_invalid_and_names_its_code(monkeypatch, tmp_path) -> None:
    """The genuine defect: exit 1 WITH diagnostics is a finding about the report."""
    _fake_run(monkeypatch, _Proc(1, FAIL_TEXT))
    entry = cpv.validate_one(tmp_path / "X.Report", "powerbi-report-author")
    assert entry["status"] == "invalid"
    assert entry["codes"] == ["PBIR_ROLE_REQUIRED_MISSING"]
    assert entry["errors"] == 1


def test_a_clean_report_is_valid(monkeypatch, tmp_path) -> None:
    """Exit 0 is a pass - the gate must not manufacture findings."""
    _fake_run(monkeypatch, _Proc(0, PASS_TEXT))
    entry = cpv.validate_one(tmp_path / "X.Report", "powerbi-report-author")
    assert entry["status"] == "valid"
    assert entry["errors"] == 0


def test_missing_cli_skips_and_never_blocks(monkeypatch, tmp_path) -> None:
    """A machine without Node must still complete an estate run."""
    monkeypatch.setattr(cpv, "find_cli", lambda _explicit=None: None)
    report = cpv.scan(tmp_path)
    assert report["status"] == "SKIPPED"
    assert "npm i -g" in report["reason"], "the skip must say how to fix itself"


def test_tooling_error_is_reported_but_does_not_refuse_the_bundle(monkeypatch, tmp_path) -> None:
    """ERROR is loud and non-blocking; only INVALID refuses. Halting on a tooling fault costs more."""
    (tmp_path / "pbip" / "W.Report").mkdir(parents=True)
    monkeypatch.setattr(cpv, "find_cli", lambda _explicit=None: "powerbi-report-author")
    _fake_run(monkeypatch, _Proc(1, ""))
    report = cpv.scan(tmp_path)
    assert report["status"] == "ERROR"
    assert report["reports_invalid"] == 0, "a tooling fault must not be counted as a failing report"
    assert "TOOLING fault" in cpv.render(report)


def test_scan_reads_the_shipping_copy_not_the_pristine_baseline(monkeypatch, tmp_path) -> None:
    """`pbip/` ships; `reports/` is reference-only (no model beside it) and would report noise."""
    (tmp_path / "pbip" / "W.Report").mkdir(parents=True)
    (tmp_path / "reports" / "W.Report").mkdir(parents=True)
    monkeypatch.setattr(cpv, "find_cli", lambda _explicit=None: "powerbi-report-author")
    _fake_run(monkeypatch, _Proc(0, PASS_TEXT))
    found = cpv.find_reports(tmp_path)
    assert [p.parent.name for p in found] == ["pbip"], f"only pbip/ should be scanned: {found}"


def test_pointing_at_a_report_folder_overrides_the_pbip_rule(tmp_path) -> None:
    """A caller can validate one report directly - that is how `reports/` gets inspected on purpose."""
    target = tmp_path / "W.Report"
    target.mkdir()
    assert cpv.find_reports(target) == [target.resolve()]


def test_render_tells_the_reader_to_bind_the_stub_not_delete_the_visual(monkeypatch, tmp_path) -> None:
    """The repair is counter-intuitive: deleting the visual also clears the error, and is wrong."""
    (tmp_path / "pbip" / "W.Report").mkdir(parents=True)
    monkeypatch.setattr(cpv, "find_cli", lambda _explicit=None: "powerbi-report-author")
    _fake_run(monkeypatch, _Proc(1, FAIL_TEXT))
    text = cpv.render(cpv.scan(tmp_path))
    assert "PBIR_ROLE_REQUIRED_MISSING" in text
    assert "Bind the stub" in text
    assert "do NOT delete the visual" in text
