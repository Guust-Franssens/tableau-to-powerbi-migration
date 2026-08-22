"""Tests for the skill-owned AI readiness checker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "skills" / "powerbi-ai-readiness" / "scripts" / "check_ai_readiness.py"


def test_empty_semantic_model_is_skipped_not_clean(tmp_path: Path) -> None:
    """A `.SemanticModel` with no TMDL objects must not be reported as 100% described."""
    unit = tmp_path / "unit"
    (unit / "fabric" / "CacheOnly.SemanticModel").mkdir(parents=True)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(unit), "--strict"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "SKIPPED - nothing measured" in result.stdout
    assert "100.0%" not in result.stdout
