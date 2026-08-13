"""Keep the operator runbook's exit-code tables tied to the scripts they describe."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "operator-runbook.md"


def _exit_constants(script: str) -> set[int]:
    source = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
    return {int(code) for code in re.findall(r"^EXIT_\w+\s*=\s*(\d+)\s*$", source, re.MULTILINE)}


def _preflight_exits() -> set[int]:
    """Return literal preflight exit codes.

    Assumption: preflight.ps1 uses literal `exit N` statements. A future computed exit such as
    `$rc = 2; exit $rc` would require a PowerShell-aware parser or an executable test matrix.
    Preflight's exits are intentionally stable today (0/1), so that extra machinery is not worth it
    until the script grows a third outcome.
    """
    source = (REPO_ROOT / "scripts" / "preflight.ps1").read_text(encoding="utf-8")
    return {int(code) for code in re.findall(r"^\s*exit\s+(\d+)\s*$", source, re.IGNORECASE | re.MULTILINE)}


def _runbook_table_after(anchor: str) -> list[str]:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert anchor in text, f"runbook anchor {anchor!r} not found in {RUNBOOK} - update the test with the heading"
    start = text.index(anchor)
    lines = text[start:].splitlines()
    table_start = next(index for index, line in enumerate(lines) if line.startswith("|"))
    table: list[str] = []
    for line in lines[table_start:]:
        if not line.startswith("|"):
            break
        table.append(line)
    return table


def _codes_in_detailed_table(anchor: str) -> set[int]:
    rows = _runbook_table_after(anchor)[2:]
    codes: set[int] = set()
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        match = re.fullmatch(r"`(\d+)`", cells[0])
        assert match, f"exit-code row has no numeric first cell: {row}"
        codes.add(int(match.group(1)))
    return codes


def _quick_reference_codes(script: str) -> set[int]:
    table = _runbook_table_after("**Exit codes**")
    headers = [cell.strip() for cell in table[0].strip("|").split("|")]
    codes = [int(header) for header in headers[1:]]
    for row in table[2:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if cells[0] == f"`{script}`":
            return {code for code, meaning in zip(codes, cells[1:], strict=True) if meaning != "—"}
    raise AssertionError(f"{script} row missing from quick-reference exit-code table")


def _help_flags(script: str) -> set[str]:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return set(re.findall(r"(?<![\w-])--[a-z0-9][a-z0-9-]*", proc.stdout))


def test_run_estate_detailed_exit_table_matches_source_constants() -> None:
    """A new `EXIT_*` in run_estate.py must not leave the runbook stale."""
    assert _codes_in_detailed_table("`run_estate.py` exit codes") == _exit_constants("run_estate.py")


def test_deploy_estate_detailed_exit_table_matches_source_constants() -> None:
    """A new `EXIT_*` in deploy_estate.py must not leave the runbook stale."""
    assert _codes_in_detailed_table("`deploy_estate.py` exit codes") == _exit_constants("deploy_estate.py")


def test_quick_reference_exit_matrix_matches_sources() -> None:
    """In the quick matrix, `—` must mean 'this script cannot return that code'."""
    assert _quick_reference_codes("preflight.ps1") == _preflight_exits()
    assert _quick_reference_codes("run_estate.py") == _exit_constants("run_estate.py")
    assert _quick_reference_codes("deploy_estate.py") == _exit_constants("deploy_estate.py")


def test_runbook_does_not_deny_deploy_flags_that_help_lists() -> None:
    """A stale "no flag exists" workaround must fail once `--help` exposes the supported flag."""
    runbook = RUNBOOK.read_text(encoding="utf-8")
    flags = _help_flags("deploy_estate.py")
    assert {"--skip", "--skip-empty-models"} <= flags
    denied = {flag for flag in flags if re.search(rf"\b[Nn]o `{re.escape(flag)}`\b", runbook)}
    assert not denied, f"runbook denies deploy_estate.py flag(s) that --help lists: {sorted(denied)}"
    assert "There is no supported flag for it" not in runbook
    assert "Until it exists, move the folder" not in runbook
