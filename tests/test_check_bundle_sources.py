"""check_bundle_sources: M_PARAM_UNDEFINED, M_PARAM_EMPTY and SOURCE_COLLAPSED (#91)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_bundle_sources as cbs  # noqa: E402  # pylint: disable=wrong-import-position


def _param(name: str, value: str) -> str:
    return f'expression {name} = "{value}" meta [IsParameterQuery=true]\n'


def _model(tmp_path: Path, expressions: str, partitions: dict[str, str]) -> Path:
    """Write a minimal bundle with the given parameters and one M partition per table."""
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


def _spec(tmp_path: Path, connection: dict) -> Path:
    path = tmp_path / "migration-spec.json"
    path.write_text(json.dumps({"data_sources": [{"name": "ds", "connection": connection}]}), encoding="utf-8")
    return path


def _legs(*servers: tuple[str, str]) -> dict:
    legs = [{"server": s, "database": d, "powerbi_target": "live_source"} for s, d in servers]
    return {"powerbi_target": "live_source", "connections": legs}


SQL = 'Sql.Database(#"Server", #"Database"),'
TWO_LEGS = (("sales-sql.example.net", "salesdb"), ("hr-sql.example.net", "hrdb"))


@pytest.mark.parametrize(
    ("expressions", "undefined", "empty", "token"),
    [
        pytest.param(_param("'Server'", "s") + _param("'Database'", "d"), [], [], None, id="populated"),
        pytest.param(_param("'Server'", "x"), ["Database"], [], "M_PARAM_UNDEFINED: Database", id="undefined"),
        # Defined-but-blank is as fatal as missing (measured on a Snowflake `.tdsx` with warehouse='').
        pytest.param(
            _param("'Server'", "s") + _param("'Database'", ""), [], ["Database"], "M_PARAM_EMPTY: Database", id="empty"
        ),
    ],
)
def test_m_parameters_and_cli_tokens(tmp_path, capsys, expressions, undefined, empty, token) -> None:
    """Every referenced parameter must be defined AND non-blank; the CLI exits 1 with the token."""
    bundle = _model(tmp_path, expressions, {"Sales": SQL})
    params = cbs.check_m_parameters(cbs.find_model_dir(bundle))
    assert (params["undefined"], params["empty"]) == (undefined, empty)
    assert cbs.main([str(bundle)]) == (1 if token else 0)
    out = capsys.readouterr().out
    if token:
        assert token in out
    else:  # No spec is not a finding, but coverage must print UNKNOWN, never OK.
        assert "SOURCE_COVERAGE: UNKNOWN - no --spec given" in out
        assert "declared endpoint(s) are reachable" not in out


def test_two_declared_servers_collapsed_to_one_is_caught(tmp_path: Path, capsys) -> None:
    """Parameters are clean and the model refreshes; only endpoint counting sees the wrong server."""
    model = _model(
        tmp_path, _param("Server", TWO_LEGS[0][0]) + _param("Database", "salesdb"), {"Orders": SQL, "Employees": SQL}
    )
    spec = _spec(tmp_path, _legs(*TWO_LEGS))
    params = cbs.check_m_parameters(cbs.find_model_dir(model))
    assert params["undefined"] == params["empty"] == []
    result = cbs.check_source_coverage(cbs.find_model_dir(model), spec)
    assert (result["status"], result["missing"]) == ("SOURCE_COLLAPSED", ["hr-sql.example.net"])
    assert cbs.main([str(model), "--spec", str(spec)]) == 1
    out = capsys.readouterr().out
    assert "SOURCE_COLLAPSED: declared 2 endpoint(s), model reaches 1" in out
    assert "MISSING : hr-sql.example.net" in out


def test_source_coverage_verdicts(tmp_path: Path) -> None:
    """Per-connector and scalar-only specs pass; no, missing or extract-only spec is UNKNOWN, never OK."""
    per_connector = _model(
        tmp_path / "a",
        "".join(
            _param(n, v)
            for n, v in [
                ("SalesServer", TWO_LEGS[0][0]),
                ("SalesDb", "salesdb"),
                ("HrServer", TWO_LEGS[1][0]),
                ("HrDb", "hrdb"),
            ]
        ),
        {"Orders": 'Sql.Database(#"SalesServer", #"SalesDb"),', "Employees": 'Sql.Database(#"HrServer", #"HrDb"),'},
    )
    spec = _spec(tmp_path / "a", _legs(*TWO_LEGS))
    assert cbs.check_source_coverage(cbs.find_model_dir(per_connector), spec)["status"] == "OK"

    single = _model(tmp_path / "s", _param("Server", "only.example.net"), {"T": 'Sql.Database(#"Server", "db"),'})
    scalar = _spec(tmp_path / "s", {"powerbi_target": "live_source", "server": "only.example.net", "database": "db"})
    assert cbs.check_source_coverage(cbs.find_model_dir(single), scalar)["status"] == "OK"
    for unknown in (None, tmp_path / "does-not-exist.json"):
        assert cbs.check_source_coverage(cbs.find_model_dir(single), unknown)["status"] == "UNKNOWN"

    extract = _model(tmp_path / "e", "", {"T": 'Csv.Document(File.Contents("x.csv")),'})
    flat = _spec(tmp_path / "e", {"powerbi_target": "flat_file", "server": None})
    assert cbs.check_source_coverage(cbs.find_model_dir(extract), flat)["status"] == "UNKNOWN"


def test_cli_missing_bundle_exits_2_and_no_model_exits_1(tmp_path: Path, capsys) -> None:
    """A bad path is a usage error (2); a bundle without a model is a finding (1)."""
    assert cbs.main([str(tmp_path / "absent")]) == 2
    assert "does not exist" in capsys.readouterr().err
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match=r"no \*\.SemanticModel/definition"):  # exits 1 via stderr
        cbs.main([str(tmp_path / "empty")])


def test_script_entry_point_smoke(tmp_path: Path) -> None:
    """One real `python check_bundle_sources.py` run keeps the `__main__` exit path honest."""
    script = Path(cbs.__file__)
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "absent")], capture_output=True, text=True, check=False, timeout=60
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
