"""
purpose: Unit tests for scripts/parse_tableau.py, using a small synthetic .twb fixture that exercises
         the core mechanics (calculated fields, column-instance resolution, the parameter-equality
         filter idiom, and reference-line/gauge parsing) without depending on a real customer workbook.
usage:   pytest -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from parse_tableau import parse_workbook  # noqa: E402  (path insert must precede this import)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal.twb"


def test_parses_top_level_shape():
    spec = parse_workbook(FIXTURE)
    assert spec["migration_spec_version"] == "1.0"
    assert len(spec["data_sources"]) == 1
    assert len(spec["worksheets"]) == 2
    assert len(spec["dashboards"]) == 1
    assert len(spec["parameters"]) == 1


def test_parameter_parsed():
    spec = parse_workbook(FIXTURE)
    param = spec["parameters"][0]
    assert param["caption"] == "City_param"
    assert param["current_value"] == "Springfield"
    assert "Shelbyville" in param["allowed_values"]


def test_calculated_field_and_dependencies():
    spec = parse_workbook(FIXTURE)
    fields = {f["caption"]: f for f in spec["data_sources"][0]["fields"]}
    assert fields["City filter"]["kind"] == "calculated"
    assert fields["City filter"]["tableau_formula"].startswith("if [Parameters]")
    # "Sales Scaled" = SUM([SALES])*100 should depend on the base "Sales" field.
    assert fields["Sales"]["id"] in fields["Sales Scaled"]["referenced_fields"]


def test_extract_connection_mode_detected():
    spec = parse_workbook(FIXTURE)
    connection = spec["data_sources"][0]["connection"]
    assert connection["mode"] == "extract"
    assert connection["hyper_file"] == "Data/test.twb Files/federated.hyper"


def test_worksheet_shelf_and_reference_lines_resolved():
    spec = parse_workbook(FIXTURE)
    worksheet = spec["worksheets"][0]
    assert worksheet["mark_type"] == "Circle"
    assert worksheet["encodings"]["rows"][0]["field_id"].endswith("__sales")
    assert worksheet["encodings"]["rows"][0]["aggregation"] == "SUM"
    labels = {rl["label"] for rl in worksheet["reference_lines"]}
    assert labels == {"Min", "Max"}


def test_measure_names_values_pivot_detected_and_resolved():
    """Tableau's 'Measure Names/Measure Values' virtual pivot has no direct Power BI equivalent - it
    should be detected structurally (not left as an opaque UNRESOLVED:... shelf/filter reference) and
    resolved to the real underlying field ids via the accompanying 'Measure Names' filter."""
    spec = parse_workbook(FIXTURE)
    sales_gauge = spec["worksheets"][0]
    assert sales_gauge["measure_names_values_pivot"] is None

    pivot_ws = next(w for w in spec["worksheets"] if w["name"] == "Measure Values Chart")
    pivot = pivot_ws["measure_names_values_pivot"]
    assert pivot is not None
    assert pivot["axis"] == "columns"
    fields_by_id = {f["id"]: f for f in spec["data_sources"][0]["fields"]}
    resolved_captions = {fields_by_id[fid]["caption"] for fid in pivot["pivoted_field_ids"]}
    assert resolved_captions == {"Sales", "Sales Scaled"}


def test_parameter_equality_filter_idiom_flagged():
    """The IF [Param]=[Dim] THEN [Dim] END + exclude-null pattern should be recognized and annotated
    so pbi-semantic-builder simplifies it to a plain slicer instead of recreating it as DAX."""
    spec = parse_workbook(FIXTURE)
    filt = spec["worksheets"][0]["filters"][0]
    assert filt["exclude_nulls"] is True
    assert filt["note"] is not None
    assert "plain PBI slicer" in filt["note"]


def test_dashboard_zone_tree_resolves_worksheet_reference():
    spec = parse_workbook(FIXTURE)
    top_zone = spec["dashboards"][0]["zones"]
    worksheet_zone = next(z for z in top_zone["children"] if z["type"] == "worksheet")
    assert worksheet_zone["worksheet_id"] == spec["worksheets"][0]["id"]
    text_zone = next(z for z in top_zone["children"] if z["type"] == "text")
    assert text_zone["text_html"] == "Footer note"


def test_limitations_are_collected_not_silently_dropped():
    spec = parse_workbook(FIXTURE)
    assert any(item["item"] == spec["data_sources"][0]["id"] for item in spec["limitations_encountered"])
