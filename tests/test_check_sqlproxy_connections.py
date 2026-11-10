"""check_sqlproxy_connections: the Tableau `sqlproxy` published-datasource gate (#282)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_sqlproxy_connections as csc  # noqa: E402  # pylint: disable=wrong-import-position

META = 'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'
# A Tableau Cloud host, not localhost: the defect is the sqlproxy protocol itself.
REAL_ENGINE_SQLPROXY_EXPRESSIONS = f"""expression Server_sqlproxy = "10ax.online.tableau.com" {META}
expression Database_sqlproxy = "TSEvents" {META}
expression Server_sqlproxy2 = "10ax.online.tableau.com" {META}
expression Database_sqlproxy2 = "VizLoadTimes" {META}
"""
CLEAN = (
    'expression Server_snowflake = "acct.snowflakecomputing.com" meta [IsParameterQuery=true]\n'
    'expression Database_snowflake = "ANALYTICS" meta [IsParameterQuery=true]\n'
)


def _write_model(root: Path, expressions: str, *, under_pbip: bool = False) -> Path:
    """Write a minimal `.SemanticModel` with an `expressions.tmdl`."""
    base = root / "pbip" / "Admin_Insights_Starter" if under_pbip else root
    model = base / "Admin_Insights_Starter.SemanticModel"
    (model / "definition").mkdir(parents=True)
    (model / "definition" / "expressions.tmdl").write_text(expressions, encoding="utf-8")
    return model


def _write_bundle(root: Path, expressions: str, workbook: str, secondaries: list[str]) -> Path:
    """A shipping pbip model, a dirty engine baseline that must not be scanned, and a handover report."""
    _write_model(root, expressions, under_pbip=True)
    _write_model(root / "semantic_models", REAL_ENGINE_SQLPROXY_EXPRESSIONS)
    entry = {"name": workbook, "pbip_status": "built", "binding_signal": {"secondary_datasources": secondaries}}
    (root / "report.json").write_text(json.dumps({"workbooks": [entry]}), encoding="utf-8")
    return root


def test_real_tableau_cloud_sqlproxy_pairs_are_blocking_and_actionable(tmp_path, capsys) -> None:
    """Kills: a localhost-only detector, or a detector that reports only a count."""
    model = _write_model(tmp_path, REAL_ENGINE_SQLPROXY_EXPRESSIONS)
    result = csc.scan(model)
    assert result["status"] == csc.STATUS_SQLPROXY and result["connections"] == 2
    findings = result["models"][0]["findings"]
    assert [(item["server"], item["database"]) for item in findings] == [
        ("10ax.online.tableau.com", "TSEvents"),
        ("10ax.online.tableau.com", "VizLoadTimes"),
    ]
    assert all(item["database_parameter"].startswith("Database_sqlproxy") for item in findings)
    assert csc.main([str(model)]) == csc.EXIT_SQLPROXY
    out = capsys.readouterr().out
    assert "SQLPROXY CONNECTION CHECK: SQLPROXY" in out and "TSEvents" in out and "VizLoadTimes" in out


def test_clean_model_is_ok_and_several_paths_merge_with_an_empty_one(tmp_path, capsys) -> None:
    """Kills: firing on any parameter expression, or KeyErroring when a target was skipped."""
    model = _write_model(tmp_path / "clean", CLEAN)
    assert csc.main([str(model)]) == csc.EXIT_OK
    assert "OK" in capsys.readouterr().out
    (tmp_path / "empty").mkdir()
    assert csc.main([str(model), str(tmp_path / "empty"), "--quiet"]) == csc.EXIT_OK


def test_suffix_pairing_keeps_incomplete_pairs_visible_and_unescapes_quoted_names(tmp_path) -> None:
    """Kills: zipping servers/databases by position, or a bare-name-only expression parser."""
    model = _write_model(
        tmp_path / "a",
        'expression Server_sqlproxy = "10ax.online.tableau.com" meta [IsParameterQuery=true]\n'
        'expression Database_sqlproxy2 = "VizLoadTimes" meta [IsParameterQuery=true]\n',
    )
    result = csc.scan(model)
    assert (result["status"], result["connections"], result["incomplete_connections"]) == (csc.STATUS_SQLPROXY, 2, 2)
    findings = result["models"][0]["findings"]
    assert (findings[0]["server_parameter"], findings[0]["database_parameter"]) == ("Server_sqlproxy", None)
    assert (findings[1]["server_parameter"], findings[1]["database_parameter"]) == (None, "Database_sqlproxy2")
    quoted = _write_model(
        tmp_path / "b",
        "expression 'Server_sqlproxy' = \"10ax.online.tableau.com\" meta [IsParameterQuery=true]\n"
        'expression \'Database_sqlproxy\' = "Published ""Datasource" meta [IsParameterQuery=true]\n',
    )
    finding = csc.scan(quoted)["models"][0]["findings"][0]
    assert (finding["server_parameter"], finding["database"]) == ("Server_sqlproxy", 'Published "Datasource')


def test_missing_path_is_usage_and_nothing_measured_is_skipped(tmp_path, capsys) -> None:
    """A typo, an empty folder, or a cache-only model must never read as OK."""
    with pytest.raises(SystemExit) as exc:
        csc.main([str(tmp_path / "does-not-exist")])
    assert exc.value.code == csc.EXIT_USAGE
    assert "OK" not in capsys.readouterr().out
    (tmp_path / "CacheOnly.SemanticModel").mkdir()
    for target in (tmp_path, tmp_path / "CacheOnly.SemanticModel"):
        assert csc.main([str(target)]) == csc.EXIT_SKIPPED
        out = capsys.readouterr().out
        assert "SKIPPED" in out and "OK" not in out


def test_bundle_scans_pbip_only_and_json_carries_pairs_and_at_risk_workbooks(tmp_path) -> None:
    """The baseline is never scanned; the JSON keeps the action queue and the warning telemetry."""
    bundle = _write_bundle(tmp_path, REAL_ENGINE_SQLPROXY_EXPRESSIONS, "Admin_Insights_Starter", ["TSEvents", "V"])
    out = tmp_path / "sqlproxy.json"
    assert csc.main([str(bundle), "--json", str(out), "--quiet", "--warn-only"]) == csc.EXIT_OK
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert (payload["status"], payload["connections"], payload["models_scanned"]) == (csc.STATUS_SQLPROXY, 2, 1)
    assert payload["at_risk_workbooks"] == [
        {
            "workbook": "Admin_Insights_Starter",
            "path": str(bundle / "report.json"),
            "pbip_status": "built",
            "secondary_datasources": ["TSEvents", "V"],
        }
    ]


def test_secondary_datasources_warning_does_not_gate_without_artifact_sqlproxy(tmp_path, capsys) -> None:
    """A clean shipping model beside a dirty baseline is OK; the handover signal stays a warning."""
    bundle = _write_bundle(tmp_path, CLEAN, "AtRiskOnly", ["PublishedDS"])
    result = csc.scan(bundle)
    assert (result["status"], result["connections"], result["models_scanned"]) == (csc.STATUS_OK, 0, 1)
    assert csc.main([str(bundle)]) == csc.EXIT_OK
    out = capsys.readouterr().out
    assert "OK" in out and "WARN" in out and "AtRiskOnly" in out
