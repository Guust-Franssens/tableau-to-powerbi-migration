"""Keep the operator runbook's exit-code tables tied to the scripts they describe."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "operator-runbook.md"


def _exit_constants(script: str) -> set[int]:
    source = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
    return {int(code) for code in re.findall(r"^EXIT_\w+\s*=\s*(\d+)\s*$", source, re.MULTILINE)}


def _preflight_exits() -> set[int]:
    source = (REPO_ROOT / "scripts" / "preflight.ps1").read_text(encoding="utf-8")
    return {int(code) for code in re.findall(r"^\s*exit\s+(\d+)\s*$", source, re.IGNORECASE | re.MULTILINE)}


def _runbook_table_after(anchor: str) -> list[str]:
    text = RUNBOOK.read_text(encoding="utf-8")
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
