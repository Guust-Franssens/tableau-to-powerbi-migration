"""Tests for scripts/check_relationship_health.py - the model-level relationship gate from #277.

The positive fixture mirrors the customer shape: a Date table, exactly one active relationship to a
secondary fact, and a date-bearing main fact with no join path. `check_field_bindings.py` already
catches the per-visual no-join symptom; this gate catches the model-owner relationship-health risk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_relationship_health as crh  # noqa: E402  # pylint: disable=wrong-import-position

CUSTOMER_SHAPED_TMDL = """table Date

	column Date
		dataType: dateTime

table 'Custom SQL Query'

	column INC_OPENED_AT
		dataType: dateTime

	column TicketId
		dataType: int64

	column Status
		dataType: string

table 'Custom SQL Query (Open Tickets)'

	column INC_OPENED_AT
		dataType: dateTime

relationship 'Open Tickets Date'
	fromColumn: 'Custom SQL Query (Open Tickets)'.INC_OPENED_AT
	toColumn: Date.Date
"""

WELL_CONNECTED_TMDL = """table Date

	column Date
		dataType: dateTime

table Tickets

	column INC_OPENED_AT
		dataType: dateTime

relationship Tickets_Date
	fromColumn: Tickets.INC_OPENED_AT
	toColumn: Date.Date
"""

FIELD_PARAMETER_TMDL = """table Date

	column Date
		dataType: dateTime

table Sales

	column OrderDate
		dataType: dateTime

table 'X-Axis'

	column 'X-Axis'
		dataType: string

	partition 'X-Axis' = calculated
		source = { ("Order Date", NAMEOF('Sales'[OrderDate]), 0) }

relationship Sales_Date
	fromColumn: Sales.OrderDate
	toColumn: Date.Date
"""


def _write_model(root: Path, tmdl: str, *, under_pbip: bool = False) -> Path:
    """Write one minimal `.SemanticModel` fixture."""
    base = root / "pbip" / "Book" if under_pbip else root
    model = base / "Book.SemanticModel"
    (model / "definition" / "tables").mkdir(parents=True)
    (model / "definition" / "tables" / "model.tmdl").write_text(tmdl, encoding="utf-8")
    return model


def test_customer_shape_fails_with_relationship_count_and_date_columns(tmp_path, capsys) -> None:
    """Kills: only checking visuals, or only counting relationships without naming the stranded table."""
    model = _write_model(tmp_path, CUSTOMER_SHAPED_TMDL)
    result = crh.scan(model)
    assert result["status"] == crh.STATUS_MISSING
    assert result["active_relationships"] == 1
    finding = result["models"][0]["findings"][0]
    assert finding["table"] == "Custom SQL Query"
    assert finding["date_columns"] == ["INC_OPENED_AT"]

    assert crh.main([str(model)]) == crh.EXIT_MISSING
    out = capsys.readouterr().out
    assert "MISSING_RELATIONSHIP" in out
    assert "Custom SQL Query" in out and "1 active relationship" in out


def test_well_connected_date_table_exits_zero(tmp_path, capsys) -> None:
    """Kills: a sparse-graph detector that flags every small model with a Date table."""
    model = _write_model(tmp_path, WELL_CONNECTED_TMDL)
    assert crh.scan(model)["status"] == crh.STATUS_OK
    assert crh.main([str(model)]) == crh.EXIT_OK
    assert "OK" in capsys.readouterr().out


def test_field_parameter_table_is_detached_ok_not_a_relationship_gap(tmp_path) -> None:
    """Kills: reimplementing detached-table handling instead of reusing check_field_bindings logic."""
    model = _write_model(tmp_path, FIELD_PARAMETER_TMDL)
    result = crh.scan(model)
    assert result["status"] == crh.STATUS_OK
    assert result["models"][0]["detached_ok"] == {"X-Axis": "field parameter"}


def test_bundle_scope_is_pbip_only_not_engine_baseline(tmp_path) -> None:
    """Kills: scanning `<bundle>/semantic_models`, which is not the shipping artifact."""
    _write_model(tmp_path, WELL_CONNECTED_TMDL, under_pbip=True)
    baseline = tmp_path / "semantic_models" / "Book.SemanticModel" / "definition" / "tables"
    baseline.mkdir(parents=True)
    (baseline / "model.tmdl").write_text(CUSTOMER_SHAPED_TMDL, encoding="utf-8")
    result = crh.scan(tmp_path)
    assert result["status"] == crh.STATUS_OK
    assert result["models_scanned"] == 1


def test_json_and_warn_only_keep_machine_readable_finding(tmp_path) -> None:
    """The suppressing mode still writes the actionable queue."""
    model = _write_model(tmp_path, CUSTOMER_SHAPED_TMDL)
    out = tmp_path / "relationships.json"
    assert crh.main([str(model), "--json", str(out), "--quiet", "--warn-only"]) == crh.EXIT_OK
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == crh.STATUS_MISSING
    assert payload["models"][0]["findings"][0]["table"] == "Custom SQL Query"


def test_missing_path_is_usage_error_not_a_verdict(tmp_path, capsys) -> None:
    """Kills: `rglob` over a typo producing OK or SKIPPED."""
    with pytest.raises(SystemExit) as exc:
        crh.main([str(tmp_path / "missing")])
    assert exc.value.code == crh.EXIT_USAGE
    assert "OK" not in capsys.readouterr().out


def test_no_models_is_skipped_not_ok(tmp_path, capsys) -> None:
    """An affirmative verdict requires at least one model to be measured."""
    assert crh.main([str(tmp_path)]) == crh.EXIT_SKIPPED
    assert "SKIPPED" in capsys.readouterr().out


def test_existing_model_with_no_tmdl_is_skipped_not_ok(tmp_path, capsys) -> None:
    """A cache-only `.SemanticModel` is a missing input state, not a clean relationship scan."""
    model = tmp_path / "CacheOnly.SemanticModel"
    model.mkdir()

    assert crh.main([str(model)]) == crh.EXIT_SKIPPED
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "OK" not in out
