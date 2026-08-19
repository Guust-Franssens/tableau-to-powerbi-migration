"""Regression tests for post-parse migration-spec.json validation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
from validate_spec import collect_spec_validation_errors

FIXTURE_SPEC = REPO_ROOT / "examples" / "shipping-kpis" / "migration-spec.json"


def _fixture_spec() -> dict:
    return json.loads(FIXTURE_SPEC.read_text(encoding="utf-8"))


def _spec_with_first_table_row_count(row_count: object) -> dict:
    spec = _fixture_spec()
    if not spec["data_sources"][0].get("tables"):
        spec["data_sources"][0]["tables"] = [{"id": "tbl.sales", "name": "Sales", "source_relation": "table"}]
    spec["data_sources"][0]["tables"][0]["row_count"] = row_count
    return spec


def _validation_errors_for_spec(tmp_path: Path, spec: dict) -> list[str]:
    path = tmp_path / "migration-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return collect_spec_validation_errors(path)


def _run_cli_with_schema(spec_path: Path, schema_path: Path) -> subprocess.CompletedProcess:
    script = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
import validate_spec
validate_spec.SCHEMA_PATH = Path(sys.argv[2])
raise SystemExit(validate_spec.main([sys.argv[1]]))
"""
    return subprocess.run(
        [sys.executable, "-c", script, str(spec_path), str(schema_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_schema_rejects_bare_integer_row_count(tmp_path: Path) -> None:
    """row_count must carry provenance; a bare number is unsafe because it can be stale or absent."""
    errors = _validation_errors_for_spec(tmp_path, _spec_with_first_table_row_count(42))

    assert any("row_count" in error and "42" in error for error in errors)


def test_schema_rejects_unknown_row_count_with_value(tmp_path: Path) -> None:
    """Unknown is a normal state, but it must not carry a fake zero/small value."""
    errors = _validation_errors_for_spec(tmp_path, _spec_with_first_table_row_count({"source": "unknown", "value": 0}))

    assert any("row_count" in error and "not valid under any" in error for error in errors)


def test_schema_rejects_unknown_row_count_source(tmp_path: Path) -> None:
    """Consumers branch on source, so unrecognised provenance must fail validation."""
    errors = _validation_errors_for_spec(
        tmp_path,
        _spec_with_first_table_row_count(
            {"value": 42, "source": "spreadsheet-guess", "observed_at": "2026-08-19T10:04:00Z"}
        ),
    )

    assert any("row_count" in error and "spreadsheet-guess" in error for error in errors)


def test_schema_accepts_unknown_row_count_without_value(tmp_path: Path) -> None:
    """Unknown round-trips explicitly and cannot be confused with zero."""
    errors = _validation_errors_for_spec(tmp_path, _spec_with_first_table_row_count({"source": "unknown"}))

    assert errors == []


def test_existing_spec_without_row_count_still_validates(tmp_path: Path) -> None:
    """Back-compat: committed specs produced before this field remains valid."""
    spec = _fixture_spec()
    for data_source in spec["data_sources"]:
        for table in data_source.get("tables", []):
            table.pop("row_count", None)

    errors = _validation_errors_for_spec(tmp_path, spec)

    assert errors == []


@pytest.mark.parametrize(
    "bad",
    [
        "yesterday",
        "",
        "2026-13-45T99:99:99Z",
        "2026-02-30T00:00:00Z",
        "2026-08-19",
        "2026-08-19T10:04:00",
    ],
)
def test_schema_rejects_an_observed_at_that_is_not_a_real_utc_timestamp(tmp_path: Path, bad: str) -> None:
    """jsonschema's format:date-time is annotation-only here, so a stdlib check does the work.

    Measured in this venv: Draft7Validator.FORMAT_CHECKER enforces only
    date/email/idn-email/idn-hostname/ipv4/ipv6/regex - `date-time` is absent because
    `rfc3339-validator` is not installed (pyproject declares `jsonschema>=4.22` with no
    `format-nongpl` extra). Passing `format_checker=` would accept every value below while looking
    like a fix. The last two cases are why a shape regex is not enough either: a date-only value and
    a naive local time are both well-formed and both ambiguous.
    """
    errors = _validation_errors_for_spec(
        tmp_path, _spec_with_first_table_row_count({"value": 42, "source": "hyper", "observed_at": bad})
    )

    assert any("observed_at" in error for error in errors), f"{bad!r} was accepted"


def test_schema_accepts_a_real_rfc3339_observed_at(tmp_path: Path) -> None:
    """Both a Z suffix and an explicit offset are valid; the stdlib check must not over-reject."""
    for good in ("2026-08-19T10:04:00Z", "2026-08-19T10:04:00+02:00", "2026-08-19T10:04:00.123456Z"):
        errors = _validation_errors_for_spec(
            tmp_path, _spec_with_first_table_row_count({"value": 42, "source": "hyper", "observed_at": good})
        )

        assert errors == [], f"{good!r} was rejected: {errors}"


def test_malformed_appended_limitation_names_the_bad_field(tmp_path: Path) -> None:
    """The observed failure was an appended severity outside the schema enum."""
    spec = _fixture_spec()
    spec["limitations_encountered"].append(
        {"item": "worksheet:profit", "issue": "seeded bad append", "severity": "critical", "stage": "semantic_build"}
    )
    path = tmp_path / "migration-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    errors = collect_spec_validation_errors(path)

    assert any(
        "limitations_encountered[" in error
        and ".severity" in error
        and "worksheet:profit" in error
        and "critical" in error
        and "expected one of: info, low, medium, high" in error
        for error in errors
    )


def test_missing_appended_limitation_stage_is_rejected(tmp_path: Path) -> None:
    """Every post-parse limitation must say which stage appended it."""
    spec = _fixture_spec()
    spec["limitations_encountered"].append(
        {"item": "worksheet:profit", "issue": "seeded bad append", "severity": "high"}
    )
    path = tmp_path / "migration-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    errors = collect_spec_validation_errors(path)

    assert any(
        "limitations_encountered[" in error
        and "worksheet:profit" in error
        and "missing required field(s): stage" in error
        for error in errors
    )


def test_typoed_appended_limitation_key_is_rejected(tmp_path: Path) -> None:
    """A misspelled key must not masquerade as an agent-readable limitation."""
    spec = _fixture_spec()
    spec["limitations_encountered"].append(
        {
            "item": "worksheet:profit",
            "issue": "seeded typo append",
            "severity": "high",
            "stage": "semantic_build",
            "severtiy": "critical",
        }
    )
    path = tmp_path / "migration-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    errors = collect_spec_validation_errors(path)

    assert any("severtiy" in error and "expected only these fields" in error for error in errors)


def test_cli_rejects_malformed_append_with_actionable_output(tmp_path: Path) -> None:
    """The command agents run must fail, not just the imported helper."""
    spec = _fixture_spec()
    spec["limitations_encountered"].append(
        {"item": "worksheet:profit", "issue": "seeded bad append", "severity": "critical", "stage": "semantic_build"}
    )
    path = tmp_path / "migration-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "validate_spec.py"), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    output = proc.stderr + proc.stdout
    assert proc.returncode == 1
    assert "limitations_encountered[" in output
    assert ".severity" in output
    assert "critical" in output
    assert "expected one of: info, low, medium, high" in output


def test_cli_fails_closed_when_jsonschema_is_unavailable(tmp_path: Path) -> None:
    """A contract gate that cannot run must not report a malformed spec as valid."""
    spec = _fixture_spec()
    spec["limitations_encountered"].append(
        {"item": "worksheet:profit", "issue": "seeded bad append", "severity": "critical", "stage": "semantic_build"}
    )
    path = tmp_path / "migration-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    script = """
import builtins
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
import validate_spec
original_import = builtins.__import__
def blocked_import(name, *args, **kwargs):
    if name == "jsonschema":
        raise ImportError("blocked by regression test")
    return original_import(name, *args, **kwargs)
builtins.__import__ = blocked_import
raise SystemExit(validate_spec.main([sys.argv[1]]))
"""

    proc = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    output = proc.stderr + proc.stdout
    assert proc.returncode == 1
    assert "validation unavailable" in output
    assert "jsonschema not installed" in output


@pytest.mark.parametrize(
    "schema_mutation",
    [
        "invalid_enum_shape",
        "empty_schema",
        "irrelevant_schema",
        "ref_accept_anything",
    ],
)
def test_cli_fails_closed_when_schema_cannot_validate_the_contract(tmp_path: Path, schema_mutation: str) -> None:
    """A corrupt or vacuous schema must not make malformed specs look valid."""
    spec = _fixture_spec()
    spec["limitations_encountered"].append(
        {"item": "worksheet:profit", "issue": "seeded bad append", "severity": "critical", "stage": "semantic_build"}
    )
    spec_path = tmp_path / "migration-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    schema = json.loads((REPO_ROOT / "docs" / "migration-spec.schema.json").read_text(encoding="utf-8"))
    if schema_mutation == "invalid_enum_shape":
        schema["properties"]["limitations_encountered"]["items"]["properties"]["severity"]["enum"] = {
            "critical": "wrong shape"
        }
    elif schema_mutation == "empty_schema":
        schema = {}
    elif schema_mutation == "irrelevant_schema":
        schema = {"description": "valid JSON Schema, but not the migration-spec contract"}
    elif schema_mutation == "ref_accept_anything":
        schema["definitions"]["accept_anything"] = {}
        schema["$ref"] = "#/definitions/accept_anything"
    schema_path = tmp_path / f"{schema_mutation}.schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    proc = _run_cli_with_schema(spec_path, schema_path)

    output = proc.stderr + proc.stdout
    assert proc.returncode == 1
    assert "validation unavailable" in output
    assert "cannot validate" in output or "not a valid Draft 7 JSON schema" in output


def test_legitimate_appended_limitation_passes_and_is_not_rewritten(tmp_path: Path) -> None:
    """A valid semantic-build append must not be blocked or normalized away."""
    spec = _fixture_spec()
    spec["limitations_encountered"].append(
        {"item": "worksheet:profit", "issue": "valid downstream note", "severity": "high", "stage": "semantic_build"}
    )
    path = tmp_path / "migration-spec.json"
    original = json.dumps(spec, indent=2)
    path.write_text(original, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "validate_spec.py"), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert path.read_text(encoding="utf-8") == original


def test_every_committed_spec_validates_against_the_schema() -> None:
    """The example/migration corpus carries real appended limitations and must stay valid."""
    specs = sorted((REPO_ROOT / "examples").glob("*/migration-spec.json"))
    specs.extend(sorted((REPO_ROOT / "migrations").glob("*/*/migration-spec.json")))
    assert specs
    invalid = {path: collect_spec_validation_errors(path) for path in specs}
    assert not {path: errors for path, errors in invalid.items() if errors}
