"""PBIR validity gate: a broken checker must never read as a broken report."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_pbir_valid as cpv  # noqa: E402  # pylint: disable=wrong-import-position

CLI = "powerbi-report-author"
FAIL_TEXT = """Validating report...
ERROR [PBIR_ROLE_REQUIRED_MISSING] Required role "Y" missing for "clusteredColumnChart"
1 error(s), 0 warning(s); result=failed
"""
PASS_TEXT = "No diagnostics.\n0 error(s), 0 warning(s); result=succeeded\n"


def _fake_run(monkeypatch, returncode: int, stdout: str) -> list[list[str]]:
    """Capture the argv the module builds, and return a canned result."""
    seen: list[list[str]] = []

    def _run(argv, **_kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(subprocess, "run", _run)
    return seen


@pytest.mark.parametrize(
    ("returncode", "stdout", "status", "codes", "errors"),
    [
        (0, PASS_TEXT, "valid", [], 0),
        (1, FAIL_TEXT, "invalid", ["PBIR_ROLE_REQUIRED_MISSING"], 1),
        (1, "", "error", None, None),  # diagnostic-free non-zero exit is a TOOLING fault
    ],
)
def test_validate_one_classifies_the_cli_result(monkeypatch, tmp_path, returncode, stdout, status, codes, errors):
    """`validate <path>` (positional) is the invocation; exit+diagnostics decide the status."""
    seen = _fake_run(monkeypatch, returncode, stdout)
    entry = cpv.validate_one(tmp_path / "X.Report", CLI)
    assert seen[0][1] == "validate" and str(tmp_path / "X.Report") in seen[0] and "--path" not in seen[0]
    assert entry["status"] == status
    if codes is not None:
        assert entry["codes"] == codes and entry["errors"] == errors


def test_missing_cli_skips_and_never_blocks(monkeypatch, tmp_path) -> None:
    """A machine without Node must still complete an estate run, and the skip says how to fix it."""
    monkeypatch.setattr(cpv, "find_cli", lambda _explicit=None: None)
    report = cpv.scan(tmp_path)
    assert report["status"] == "SKIPPED" and "npm i -g" in report["reason"]


@pytest.mark.parametrize(
    ("returncode", "stdout", "status", "needles"),
    [
        (1, "", "ERROR", ["TOOLING fault"]),
        (1, FAIL_TEXT, "INVALID", ["PBIR_ROLE_REQUIRED_MISSING", "Bind the stub", "do NOT delete the visual"]),
    ],
)
def test_scan_status_and_render(monkeypatch, tmp_path, returncode, stdout, status, needles) -> None:
    """ERROR is loud and non-blocking; only INVALID refuses, and the repair is bind, not delete."""
    (tmp_path / "pbip" / "W.Report").mkdir(parents=True)
    monkeypatch.setattr(cpv, "find_cli", lambda _explicit=None: CLI)
    _fake_run(monkeypatch, returncode, stdout)
    report = cpv.scan(tmp_path)
    assert report["status"] == status
    assert report["reports_invalid"] == (1 if status == "INVALID" else 0)
    text = cpv.render(report)
    assert all(needle in text for needle in needles)


def test_find_reports_scans_pbip_only_unless_pointed_at_a_report(tmp_path) -> None:
    """`pbip/` ships; `reports/` is reference-only unless a caller targets it directly."""
    (tmp_path / "pbip" / "W.Report").mkdir(parents=True)
    (tmp_path / "reports" / "W.Report").mkdir(parents=True)
    assert [p.parent.name for p in cpv.find_reports(tmp_path)] == ["pbip"]
    target = tmp_path / "reports" / "W.Report"
    assert cpv.find_reports(target) == [target.resolve()]
