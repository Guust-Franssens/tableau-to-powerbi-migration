"""Tests for scripts/check_sqlproxy_connections.py - the sqlproxy published-datasource gate.

Every fixture uses the engine-shaped `expression Server_sqlproxy*` / `Database_sqlproxy*` lines from
issue #282. The positive case deliberately uses a Tableau Cloud host, not localhost, because the bug
is the Tableau `sqlproxy` protocol itself; a localhost-only rule would miss the committed corpus.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_sqlproxy_connections as csc  # noqa: E402  # pylint: disable=wrong-import-position

REAL_ENGINE_SQLPROXY_EXPRESSIONS = """expression Server_sqlproxy = "10ax.online.tableau.com" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
expression Database_sqlproxy = "TSEvents" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
expression Server_sqlproxy2 = "10ax.online.tableau.com" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
expression Database_sqlproxy2 = "VizLoadTimes" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
"""


def _write_model(root: Path, expressions: str, *, under_pbip: bool = False) -> Path:
    """Write a minimal `.SemanticModel` with an `expressions.tmdl`."""
    base = root / "pbip" / "Admin_Insights_Starter" if under_pbip else root
    model = base / "Admin_Insights_Starter.SemanticModel"
    (model / "definition").mkdir(parents=True)
    (model / "definition" / "expressions.tmdl").write_text(expressions, encoding="utf-8")
    return model


def _write_bundle(root: Path, expressions: str) -> Path:
    """Write a shipping pbip model plus a dirty engine baseline that must not be scanned."""
    _write_model(root, expressions, under_pbip=True)
    baseline = root / "semantic_models" / "Admin_Insights_Starter.SemanticModel" / "definition"
    baseline.mkdir(parents=True)
    (baseline / "expressions.tmdl").write_text(REAL_ENGINE_SQLPROXY_EXPRESSIONS, encoding="utf-8")
    return root


def test_real_tableau_cloud_sqlproxy_pairs_are_blocking_and_actionable(tmp_path, capsys) -> None:
    """Kills: a localhost-only detector, or a detector that reports only a count."""
    model = _write_model(tmp_path, REAL_ENGINE_SQLPROXY_EXPRESSIONS)
    result = csc.scan(model)
    assert result["status"] == csc.STATUS_SQLPROXY
    assert result["connections"] == 2
    findings = result["models"][0]["findings"]
    assert [(item["server"], item["database"]) for item in findings] == [
        ("10ax.online.tableau.com", "TSEvents"),
        ("10ax.online.tableau.com", "VizLoadTimes"),
    ]
    assert all(item["database_parameter"].startswith("Database_sqlproxy") for item in findings)

    assert csc.main([str(model)]) == csc.EXIT_SQLPROXY
    out = capsys.readouterr().out
    assert "SQLPROXY CONNECTION CHECK: SQLPROXY" in out
    assert "TSEvents" in out and "VizLoadTimes" in out


def test_clean_model_is_ok_and_exits_zero(tmp_path, capsys) -> None:
    """Kills: a gate that fires on any parameter expression."""
    model = _write_model(
        tmp_path,
        'expression Server_snowflake = "acct.snowflakecomputing.com" meta [IsParameterQuery=true]\n'
        'expression Database_snowflake = "ANALYTICS" meta [IsParameterQuery=true]\n',
    )
    assert csc.scan(model)["status"] == csc.STATUS_OK
    assert csc.main([str(model)]) == csc.EXIT_OK
    assert "OK" in capsys.readouterr().out


def test_bundle_scope_is_pbip_only_not_engine_baseline(tmp_path) -> None:
    """Kills: scanning `<bundle>/semantic_models`, which would flag the non-shipping baseline."""
    bundle = _write_bundle(
        tmp_path,
        'expression Server_snowflake = "acct.snowflakecomputing.com" meta [IsParameterQuery=true]\n',
    )
    result = csc.scan(bundle)
    assert result["status"] == csc.STATUS_OK
    assert result["connections"] == 0
    assert result["models_scanned"] == 1


def test_suffix_pairing_keeps_incomplete_pairs_visible(tmp_path) -> None:
    """Kills: zipping servers/databases by position and hiding a missing half."""
    model = _write_model(
        tmp_path,
        'expression Server_sqlproxy = "10ax.online.tableau.com" meta [IsParameterQuery=true]\n'
        'expression Database_sqlproxy2 = "VizLoadTimes" meta [IsParameterQuery=true]\n',
    )
    result = csc.scan(model)
    assert result["status"] == csc.STATUS_SQLPROXY
    assert result["connections"] == 2
    assert result["incomplete_connections"] == 2
    findings = result["models"][0]["findings"]
    assert findings[0]["server_parameter"] == "Server_sqlproxy"
    assert findings[0]["database_parameter"] is None
    assert findings[1]["server_parameter"] is None
    assert findings[1]["database_parameter"] == "Database_sqlproxy2"


def test_quoted_tmdl_expression_names_and_values_are_unescaped(tmp_path) -> None:
    """Kills: a bare-name-only expression parser."""
    model = _write_model(
        tmp_path,
        "expression 'Server_sqlproxy' = \"10ax.online.tableau.com\" meta [IsParameterQuery=true]\n"
        'expression \'Database_sqlproxy\' = "Published ""Datasource" meta [IsParameterQuery=true]\n',
    )
    finding = csc.scan(model)["models"][0]["findings"][0]
    assert finding["server_parameter"] == "Server_sqlproxy"
    assert finding["database"] == 'Published "Datasource'


def test_missing_path_is_usage_error_not_a_verdict(tmp_path, capsys) -> None:
    """Kills: `rglob` over a nonexistent folder quietly reporting OK or SKIPPED."""
    with pytest.raises(SystemExit) as exc:
        csc.main([str(tmp_path / "does-not-exist")])
    assert exc.value.code == csc.EXIT_USAGE
    assert "OK" not in capsys.readouterr().out


def test_no_models_is_skipped_not_ok(tmp_path, capsys) -> None:
    """Kills: an affirmative verdict when no semantic model was measured."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert csc.main([str(empty)]) == csc.EXIT_SKIPPED
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "OK" not in out


def test_existing_model_with_no_tmdl_is_skipped_not_ok(tmp_path, capsys) -> None:
    """A cache-only `.SemanticModel` is a missing input state, not a clean sqlproxy scan."""
    model = tmp_path / "CacheOnly.SemanticModel"
    model.mkdir()

    assert csc.main([str(model)]) == csc.EXIT_SKIPPED
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "OK" not in out


def test_json_output_carries_pairs_and_warning_signal(tmp_path) -> None:
    """The machine-readable output must contain the action queue and non-blocking risk telemetry."""
    bundle = _write_bundle(tmp_path, REAL_ENGINE_SQLPROXY_EXPRESSIONS)
    (bundle / "report.json").write_text(
        json.dumps(
            {
                "workbooks": [
                    {
                        "name": "Admin_Insights_Starter",
                        "pbip_status": "built",
                        "binding_signal": {"secondary_datasources": ["TSEvents", "VizLoadTimes"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "sqlproxy.json"
    assert csc.main([str(bundle), "--json", str(out), "--quiet", "--warn-only"]) == csc.EXIT_OK
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == csc.STATUS_SQLPROXY
    assert payload["connections"] == 2
    assert payload["at_risk_workbooks"] == [
        {
            "workbook": "Admin_Insights_Starter",
            "path": str(bundle / "report.json"),
            "pbip_status": "built",
            "secondary_datasources": ["TSEvents", "VizLoadTimes"],
        }
    ]


def test_secondary_datasources_warning_does_not_gate_without_artifact_sqlproxy(tmp_path, capsys) -> None:
    """The handover signal is weaker than a broken model on disk, so it stays a warning."""
    bundle = _write_bundle(
        tmp_path,
        'expression Server_snowflake = "acct.snowflakecomputing.com" meta [IsParameterQuery=true]\n',
    )
    (bundle / "report.json").write_text(
        json.dumps(
            {
                "workbooks": [
                    {
                        "name": "AtRiskOnly",
                        "pbip_status": "built",
                        "binding_signal": {"secondary_datasources": ["PublishedDS"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert csc.main([str(bundle)]) == csc.EXIT_OK
    out = capsys.readouterr().out
    assert "OK" in out and "WARN" in out and "AtRiskOnly" in out


def test_several_paths_merge_without_turning_empty_input_into_a_crash(tmp_path) -> None:
    """Kills: treating a skipped target as a model-shaped dict and KeyErroring during merge."""
    clean = _write_model(tmp_path / "clean", 'expression Server_snowflake = "server" meta [IsParameterQuery=true]\n')
    empty = tmp_path / "empty"
    empty.mkdir()
    assert csc.main([str(clean), str(empty), "--quiet"]) == csc.EXIT_OK
