"""Golden-output tests for the committed dirty gate fixture.

These snapshots only prove that gate stdout did not change. They pass happily on output that is
verbose, confusing, or otherwise unhelpful. The harness makes drift visible in code review; judging
whether the output is good remains a review job.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "check-gates-dirty"
GOLDEN = REPO_ROOT / "tests" / "golden" / "check-gates-dirty"


def _normalize_stdout(text: str) -> str:
    """Normalize process-output line endings, but not gate wording."""
    return text.replace("\r\n", "\n")


GATE_SNAPSHOTS = [
    ("stub-measures", "check_stub_measures.py", ["--strict"], 1),
    ("sqlproxy-connections", "check_sqlproxy_connections.py", [], 1),
    ("relationship-health", "check_relationship_health.py", [], 1),
    ("field-bindings", "check_field_bindings.py", [], 1),
    ("blank-placeholders", "check_blank_placeholders.py", [], 2),
]


@pytest.mark.parametrize(("snapshot_name", "script_name", "extra_args", "expected_exit"), GATE_SNAPSHOTS)
def test_dirty_gate_stdout_matches_golden(
    snapshot_name: str,
    script_name: str,
    extra_args: list[str],
    expected_exit: int,
) -> None:
    """The dirty fixture locks each gate's stdout until a deliberate prose change updates the golden."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script_name), str(FIXTURE), *extra_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert result.stderr == ""
    assert _normalize_stdout(result.stdout) == (GOLDEN / f"{snapshot_name}.stdout").read_text(encoding="utf-8")
