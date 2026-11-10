"""check_relationship_health: the model-level relationship gate (#277)."""

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


def _write_model(root: Path, tmdl: str) -> Path:
    """Write one minimal `.SemanticModel` fixture."""
    model = root / "Book.SemanticModel"
    (model / "definition" / "tables").mkdir(parents=True)
    (model / "definition" / "tables" / "model.tmdl").write_text(tmdl, encoding="utf-8")
    return model


def test_customer_shape_fails_with_relationship_count_and_date_columns(tmp_path, capsys) -> None:
    """Kills: only checking visuals, or counting relationships without naming the stranded table."""
    model = _write_model(tmp_path, CUSTOMER_SHAPED_TMDL)
    result = crh.scan(model)
    assert result["status"] == crh.STATUS_MISSING and result["active_relationships"] == 1
    finding = result["models"][0]["findings"][0]
    assert (finding["table"], finding["date_columns"]) == ("Custom SQL Query", ["INC_OPENED_AT"])
    assert crh.main([str(model)]) == crh.EXIT_MISSING
    out = capsys.readouterr().out
    assert "MISSING_RELATIONSHIP" in out and "Custom SQL Query" in out and "1 active relationship" in out
    payload_path = tmp_path / "relationships.json"
    assert crh.main([str(model), "--json", str(payload_path), "--quiet", "--warn-only"]) == crh.EXIT_OK
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["status"] == crh.STATUS_MISSING
    assert payload["models"][0]["findings"][0]["table"] == "Custom SQL Query"


def test_well_connected_and_field_parameter_models_are_ok(tmp_path, capsys) -> None:
    """Kills: flagging every small model with a Date table, or a field parameter as a gap."""
    model = _write_model(tmp_path / "a", WELL_CONNECTED_TMDL)
    assert crh.main([str(model)]) == crh.EXIT_OK
    assert "OK" in capsys.readouterr().out
    result = crh.scan(_write_model(tmp_path / "b", FIELD_PARAMETER_TMDL))
    assert result["status"] == crh.STATUS_OK
    assert result["models"][0]["detached_ok"] == {"X-Axis": "field parameter"}


def test_bundle_scope_is_pbip_only_not_engine_baseline(tmp_path) -> None:
    """Kills: scanning `<bundle>/semantic_models`, which is not the shipping artifact."""
    _write_model(tmp_path / "pbip" / "Book", WELL_CONNECTED_TMDL)
    _write_model(tmp_path / "semantic_models", CUSTOMER_SHAPED_TMDL)
    result = crh.scan(tmp_path)
    assert result["status"] == crh.STATUS_OK and result["models_scanned"] == 1


def test_missing_path_is_usage_and_nothing_measured_is_skipped(tmp_path, capsys) -> None:
    """A typo, an empty folder, or a cache-only model must never read as OK."""
    with pytest.raises(SystemExit) as exc:
        crh.main([str(tmp_path / "missing")])
    assert exc.value.code == crh.EXIT_USAGE
    assert "OK" not in capsys.readouterr().out
    assert crh.main([str(tmp_path)]) == crh.EXIT_SKIPPED
    assert "SKIPPED" in capsys.readouterr().out
    (tmp_path / "CacheOnly.SemanticModel").mkdir()
    assert crh.main([str(tmp_path / "CacheOnly.SemanticModel")]) == crh.EXIT_SKIPPED
    out = capsys.readouterr().out
    assert "SKIPPED" in out and "OK" not in out
