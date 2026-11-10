"""Tests for the local large-refresh fixture generator."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "make_refresh_fixture.py"
spec = importlib.util.spec_from_file_location("make_refresh_fixture", SCRIPT)
make_refresh_fixture = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(make_refresh_fixture)


def test_csv_is_deterministic_per_rows_and_seed(tmp_path: Path) -> None:
    paths = [tmp_path / name / make_refresh_fixture.CSV_NAME for name in ("a", "b", "c")]
    first = make_refresh_fixture.write_csv(paths[0], rows=1_000, seed=262)
    assert make_refresh_fixture.write_csv(paths[1], rows=1_000, seed=262) == first
    assert paths[0].read_bytes() == paths[1].read_bytes()
    assert make_refresh_fixture.write_csv(paths[2], rows=1_000, seed=263)[1] != first[1]


def test_binding_points_source_folder_at_generated_data(tmp_path: Path) -> None:
    """The generated expressions.tmdl carries the machine-local data folder and no committed payload."""
    definition = tmp_path / "LargeRefresh.SemanticModel" / "definition"
    data = tmp_path / "data"

    binding = make_refresh_fixture.write_binding(definition, data)

    text = binding.read_text(encoding="utf-8")
    assert "expression SourceFolder" in text
    assert str(data.resolve()) + "\\" in text
    assert "IsParameterQuery=true" in text


def test_cli_can_generate_and_bind_a_small_fixture(tmp_path: Path) -> None:
    """Subprocess smoke test for the documented CLI surface."""
    out = tmp_path / "data"
    definition = tmp_path / "model" / "definition"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--rows",
            "25",
            "--seed",
            "262",
            "--out",
            str(out),
            "--model-definition-dir",
            str(definition),
            "--print-hash",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert (out / make_refresh_fixture.CSV_NAME).is_file()
    assert (definition / "expressions.tmdl").is_file()
    assert "rows=25" in result.stdout
    assert "sha256=" in result.stdout
