"""Tests for `scripts/check_bundle_sources.py`, the static source checks lifted from the retired
probe builder's `--check-only` mode (`M_PARAM_UNDEFINED`, `M_PARAM_EMPTY`, `SOURCE_COLLAPSED`)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_bundle_sources.py"
sys.path.insert(0, str(REPO / "scripts"))

import check_bundle_sources  # noqa: E402  # pylint: disable=wrong-import-position

PARTITION_TMDL = """\
table 'Sales'
\tcolumn 'Amount'
\t\tdataType: double
\t\tsourceColumn: Amount

\tpartition 'Sales' = m
\t\tmode: directQuery
\t\tsource =
\t\t\tlet
\t\t\t\tSource = Sql.Database(#"Server", #"Database"),
\t\t\t\tData = Source{[Schema="dbo",Item="Sales"]}[Data]
\t\t\tin
\t\t\t\tData
"""

EXPRESSIONS_TMDL = """\
expression 'Server' = "myserver.database.windows.net" meta [IsParameterQuery=true]
expression 'Database' = "sales" meta [IsParameterQuery=true]
"""


@pytest.fixture(name="bundle")
def bundle_fixture(tmp_path: Path) -> Path:
    """A minimal emitted bundle: one DirectQuery table plus its two M parameters."""
    model = tmp_path / "bundle" / "Demo.SemanticModel" / "definition"
    (model / "tables").mkdir(parents=True)
    (model / "tables" / "Sales.tmdl").write_text(PARTITION_TMDL, encoding="utf-8")
    (model / "expressions.tmdl").write_text(EXPRESSIONS_TMDL, encoding="utf-8")
    return tmp_path / "bundle"


def test_empty_m_parameter_is_detected(bundle: Path) -> None:
    """A parameter that is DEFINED but blank is exactly as fatal as a missing one.

    Measured 2026-08-05 on a real Snowflake `.tdsx` whose `<connection warehouse=''>` was blank.
    The emitter honestly wrote `expression Warehouse = ""` with a TODO; our check looked only for
    the NAME and reported "all referenced parameters are defined", exit 0 - a false green on a
    bundle that provably cannot refresh.
    """
    expressions = bundle / "Demo.SemanticModel" / "definition" / "expressions.tmdl"
    expressions.write_text(
        "expression 'Server' = \"s\" meta [IsParameterQuery=true]\n"
        "expression 'Database' = \"\" meta [IsParameterQuery=true, IsParameterQueryRequired=true]\n",
        encoding="utf-8",
    )

    params = check_bundle_sources.check_m_parameters(check_bundle_sources.find_model_dir(bundle))

    assert params["undefined"] == [], "the name IS defined - that is the trap"
    assert params["empty"] == ["Database"]


def test_a_populated_parameter_is_not_flagged_empty(bundle: Path) -> None:
    """Guard the other direction, so a normal bundle is not blocked."""
    params = check_bundle_sources.check_m_parameters(check_bundle_sources.find_model_dir(bundle))

    assert params["empty"] == []
    assert params["undefined"] == []


def test_undefined_m_parameter_is_detected(bundle: Path) -> None:
    """The static check that neither `validate` nor the emitter's own self-check performs."""
    expressions = bundle / "Demo.SemanticModel" / "definition" / "expressions.tmdl"
    expressions.write_text("expression 'Server' = \"x\" meta [IsParameterQuery=true]\n", encoding="utf-8")

    params = check_bundle_sources.check_m_parameters(check_bundle_sources.find_model_dir(bundle))

    assert params["undefined"] == ["Database"]


# ---------------------------------------------------------------------------
# SOURCE_COLLAPSED - the silent sibling of M_PARAM_UNDEFINED (upstream issue #91)
# ---------------------------------------------------------------------------


def _coverage_model(tmp_path: Path, expressions: str, partitions: dict[str, str]) -> Path:
    """Write a minimal semantic model with the given parameters and table partitions."""
    model = tmp_path / "b" / "M.SemanticModel" / "definition"
    (model / "tables").mkdir(parents=True, exist_ok=True)
    (model / "expressions.tmdl").write_text(expressions, encoding="utf-8")
    for name, source in partitions.items():
        (model / "tables" / f"{name}.tmdl").write_text(
            f"table {name}\n\tpartition {name} = m\n\t\tmode: import\n\t\tsource =\n\t\t\tlet\n"
            f"\t\t\t\tSource = {source}\n\t\t\tin\n\t\t\t\tSource\n",
            encoding="utf-8",
        )
    return tmp_path / "b"


def _spec(tmp_path: Path, servers: list[tuple[str, str]]) -> Path:
    """Write a migration-spec.json declaring one live datasource with the given connection legs."""
    path = tmp_path / "migration-spec.json"
    path.write_text(
        json.dumps(
            {
                "data_sources": [
                    {
                        "name": "ds",
                        "connection": {
                            "powerbi_target": "live_source",
                            "connections": [
                                {"server": s, "database": d, "powerbi_target": "live_source"} for s, d in servers
                            ],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _collapsed_model(tmp_path: Path) -> tuple[Path, Path]:
    """The measured upstream defect: a cross-database join over two SQL servers, emitted as one."""
    model = _coverage_model(
        tmp_path,
        'expression Server = "sales-sql.example.net" meta [IsParameterQuery=true]\n'
        'expression Database = "salesdb" meta [IsParameterQuery=true]\n',
        {
            "Orders": 'Sql.Database(#"Server", #"Database"),',
            "Employees": 'Sql.Database(#"Server", #"Database"),',
        },
    )
    spec = _spec(tmp_path, [("sales-sql.example.net", "salesdb"), ("hr-sql.example.net", "hrdb")])
    return model, spec


def test_two_declared_servers_collapsed_to_one_is_caught(tmp_path: Path) -> None:
    """Both parameters ARE defined, so `check_m_parameters` is clean and the model refreshes without
    error - it just silently reads the wrong server. Only endpoint counting catches it.
    """
    model, spec = _collapsed_model(tmp_path)

    params = check_bundle_sources.check_m_parameters(check_bundle_sources.find_model_dir(model))
    assert not params["undefined"], "precondition: the parameter check must NOT catch this"
    assert not params["empty"]

    result = check_bundle_sources.check_source_coverage(check_bundle_sources.find_model_dir(model), spec)
    assert result["status"] == "SOURCE_COLLAPSED"
    assert result["missing"] == ["hr-sql.example.net"]


def test_per_connector_parameters_pass(tmp_path: Path) -> None:
    """The shape the fix should produce - one parameter set per connector - must pass."""
    model = _coverage_model(
        tmp_path,
        'expression SalesServer = "sales-sql.example.net" meta [IsParameterQuery=true]\n'
        'expression SalesDb = "salesdb" meta [IsParameterQuery=true]\n'
        'expression HrServer = "hr-sql.example.net" meta [IsParameterQuery=true]\n'
        'expression HrDb = "hrdb" meta [IsParameterQuery=true]\n',
        {
            "Orders": 'Sql.Database(#"SalesServer", #"SalesDb"),',
            "Employees": 'Sql.Database(#"HrServer", #"HrDb"),',
        },
    )
    spec = _spec(tmp_path, [("sales-sql.example.net", "salesdb"), ("hr-sql.example.net", "hrdb")])
    result = check_bundle_sources.check_source_coverage(check_bundle_sources.find_model_dir(model), spec)
    assert result["status"] == "OK"


def test_a_missing_spec_is_unknown_never_ok(tmp_path: Path) -> None:
    """Absence of evidence must not print as coverage: claiming a property was checked when the
    input needed to check it was never supplied is a false green.
    """
    model = _coverage_model(
        tmp_path,
        'expression Server = "a.example.net" meta [IsParameterQuery=true]\n',
        {"T": 'Sql.Database(#"Server", "db"),'},
    )
    for spec in (None, tmp_path / "does-not-exist.json"):
        result = check_bundle_sources.check_source_coverage(check_bundle_sources.find_model_dir(model), spec)
        assert result["status"] == "UNKNOWN"


def test_an_extract_only_spec_is_unknown_not_a_false_pass(tmp_path: Path) -> None:
    """Extracts reference no Server parameter, so there is nothing to compare - say so."""
    spec = tmp_path / "migration-spec.json"
    spec.write_text(
        json.dumps({"data_sources": [{"connection": {"powerbi_target": "flat_file", "server": None}}]}),
        encoding="utf-8",
    )
    model = _coverage_model(tmp_path, "", {"T": 'Csv.Document(File.Contents("x.csv")),'})
    result = check_bundle_sources.check_source_coverage(check_bundle_sources.find_model_dir(model), spec)
    assert result["status"] == "UNKNOWN"


def test_a_single_connection_spec_without_the_plural_array_still_works(tmp_path: Path) -> None:
    """Older specs carry only the scalar server/database - they must still get a real answer."""
    spec = tmp_path / "migration-spec.json"
    spec.write_text(
        json.dumps(
            {
                "data_sources": [
                    {
                        "connection": {
                            "powerbi_target": "live_source",
                            "server": "only.example.net",
                            "database": "db",
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    model = _coverage_model(
        tmp_path,
        'expression Server = "only.example.net" meta [IsParameterQuery=true]\n',
        {"T": 'Sql.Database(#"Server", "db"),'},
    )
    result = check_bundle_sources.check_source_coverage(check_bundle_sources.find_model_dir(model), spec)
    assert result["status"] == "OK"


# ---------------------------------------------------------------------------
# The CLI: exit codes and output tokens are the contract callers branch on
# ---------------------------------------------------------------------------


def _cli(*args: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_cli_reports_an_undefined_parameter_with_exit_1(bundle: Path) -> None:
    """An undefined parameter is a finding: exit 1 and the `M_PARAM_UNDEFINED` token."""
    expressions = bundle / "Demo.SemanticModel" / "definition" / "expressions.tmdl"
    expressions.write_text("expression 'Server' = \"x\" meta [IsParameterQuery=true]\n", encoding="utf-8")
    proc = _cli(bundle)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "M_PARAM_UNDEFINED: Database" in proc.stdout


def test_cli_reports_an_empty_parameter_with_exit_1(bundle: Path) -> None:
    """A defined-but-blank parameter is a finding: exit 1 and the `M_PARAM_EMPTY` token."""
    expressions = bundle / "Demo.SemanticModel" / "definition" / "expressions.tmdl"
    expressions.write_text(
        "expression 'Server' = \"s\" meta [IsParameterQuery=true]\nexpression 'Database' = \"\"\n",
        encoding="utf-8",
    )
    proc = _cli(bundle)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "M_PARAM_EMPTY: Database" in proc.stdout


def test_cli_reports_a_collapsed_source_with_exit_1(tmp_path: Path) -> None:
    """A collapsed endpoint set is a finding: exit 1, the counts and the missing host."""
    model, spec = _collapsed_model(tmp_path)
    proc = _cli(model, "--spec", spec)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "SOURCE_COLLAPSED: declared 2 endpoint(s), model reaches 1" in proc.stdout
    assert "MISSING : hr-sql.example.net" in proc.stdout


def test_cli_without_a_spec_passes_but_never_claims_coverage(bundle: Path) -> None:
    """No spec is not a finding (exit 0), but coverage must print UNKNOWN, never OK."""
    proc = _cli(bundle)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SOURCE_COVERAGE: UNKNOWN - no --spec given" in proc.stdout
    assert "declared endpoint(s) are reachable" not in proc.stdout


def test_cli_missing_bundle_exits_2_and_no_model_exits_1(tmp_path: Path) -> None:
    """A bad path is a usage error (2); a bundle without a model is a finding (1)."""
    missing = _cli(tmp_path / "absent")
    assert missing.returncode == 2
    assert "does not exist" in missing.stderr
    empty = tmp_path / "empty"
    empty.mkdir()
    no_model = _cli(empty)
    assert no_model.returncode == 1
    assert "no *.SemanticModel/definition" in no_model.stderr
