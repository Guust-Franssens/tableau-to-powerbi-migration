"""
purpose: Regression tests for the contract-integrity gates added after a battle-test run found that
         `migration-spec.json` - described as "the contract every stage reads and writes" - was only
         ever validated by its FIRST writer. Three subsequent agents append to it and nothing checked
         the result, so a subagent silently left the contract schema-invalid.
usage:   pytest -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from validate_spec import validate_spec  # noqa: E402  (path insert must precede this import)

FIXTURE_SPEC = REPO / "examples" / "shipping-kpis" / "migration-spec.json"


def _spec() -> dict:
    return json.loads(FIXTURE_SPEC.read_text(encoding="utf-8"))


def test_every_committed_spec_validates_against_the_schema():
    """The corpus is the contract's regression suite.

    These 16 specs are valid today by luck rather than by a gate - the agents that append to them
    happened to use allowed values. This test makes that a property instead of a coincidence.
    """
    specs = sorted((REPO / "examples").glob("*/migration-spec.json"))
    assert specs, "no example specs found - the corpus is the regression suite"
    invalid = {p.parent.name: validate_spec(p) for p in specs}
    assert not {k: v for k, v in invalid.items() if v}


def test_an_out_of_enum_severity_is_rejected(tmp_path: Path):
    """The exact defect observed: a subagent appended `severity: "critical"`, which is not one of
    info/low/medium/high. Nothing anywhere noticed."""
    spec = _spec()
    spec["limitations_encountered"].append(
        {"item": "x", "issue": "seeded", "severity": "critical", "stage": "semantic_build"}
    )
    path = tmp_path / "migration-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    problems = validate_spec(path)
    assert any("severity" in p and "critical" in p for p in problems)


def test_validator_names_the_offending_path(tmp_path: Path):
    """A gate that says only "invalid" is not actionable - it has to say where."""
    spec = _spec()
    spec["limitations_encountered"].append({"item": "x", "issue": "seeded", "severity": "nope", "stage": "validate"})
    path = tmp_path / "migration-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert any(p.startswith("limitations_encountered/") for p in validate_spec(path))


def _parse(workbook: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "parse_tableau.py"), str(workbook), "-o", str(out), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.fixture(name="spec_with_downstream")
def _spec_with_downstream(tmp_path: Path) -> Path:
    """A spec that already carries entries appended by a later stage."""
    out = tmp_path / "migration-spec.json"
    spec = _spec()
    spec["limitations_encountered"].append(
        {"item": "x", "issue": "appended by a build agent", "severity": "high", "stage": "semantic_build"}
    )
    out.write_text(json.dumps(spec), encoding="utf-8")
    return out


def test_reparse_refuses_to_destroy_downstream_limitations(spec_with_downstream: Path):
    """Re-parsing rewrites the spec in place, destroying the 20-50 limitations the build and
    validate stages appended - the raw material for the migration summary.

    The rule existed only as prose in one agent persona, which the repo itself calls an anti-pattern
    ("MANDATORY prose without enforcement"). This is the enforcement.
    """
    before = spec_with_downstream.read_text(encoding="utf-8")
    proc = _parse(REPO / "tests" / "fixtures" / "databricks_live.twb", spec_with_downstream)
    assert proc.returncode != 0
    assert "REFUSING to overwrite" in proc.stderr + proc.stdout
    assert spec_with_downstream.read_text(encoding="utf-8") == before, "the spec must be untouched"


def test_reparse_is_allowed_with_force(spec_with_downstream: Path):
    """--force is the documented escape hatch for a genuinely changed source workbook."""
    proc = _parse(REPO / "tests" / "fixtures" / "databricks_live.twb", spec_with_downstream, "--force")
    assert proc.returncode == 0
    assert json.loads(spec_with_downstream.read_text(encoding="utf-8"))["data_sources"][0]["connection"]["http_path"]


def test_reparse_of_a_parse_only_spec_is_frictionless(tmp_path: Path):
    """The guard must not nag on the normal case: a spec only the parser has ever written."""
    out = tmp_path / "migration-spec.json"
    assert _parse(REPO / "tests" / "fixtures" / "databricks_live.twb", out).returncode == 0
    assert _parse(REPO / "tests" / "fixtures" / "databricks_live.twb", out).returncode == 0
