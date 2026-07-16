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
    assert len(spec["dashboards"]) == 2
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
    assert labels == {"Min", "Max", "Total"}
    # Tableau reference-line aggregation formulas beyond constant/computed (e.g. 'total') must be
    # captured verbatim and accepted by the schema (surfaced from the Dis-OrderPodcast workbook).
    assert "total" in {rl["formula"] for rl in worksheet["reference_lines"]}


def test_detail_and_tooltip_shelves_resolved():
    """Tableau's Detail shelf serializes as <lod> elements and the Tooltip shelf as <tooltip>
    elements (both multi-field); the parser must resolve them to field ids rather than stubbing []."""
    spec = parse_workbook(FIXTURE)
    enc = spec["worksheets"][0]["encodings"]
    detail_ids = {f["field_id"] for f in enc["detail"]}
    tooltip_ids = {f["field_id"] for f in enc["tooltip"]}
    fields = {f["caption"]: f["id"] for f in spec["data_sources"][0]["fields"]}
    assert fields["Name"] in detail_ids
    assert fields["Sales Scaled"] in tooltip_ids


def test_join_relation_graph_extracted():
    """<relation type='join'> operands, join type, and on-clause conditions must be captured into the
    data source's joins[] so the semantic builder can rebuild Power BI relationships (here: an inner
    join Cities <-> Regions on [Region ID])."""
    spec = parse_workbook(FIXTURE)
    joins = spec["data_sources"][0]["joins"]
    assert len(joins) == 1
    join = joins[0]
    assert join["type"] == "inner"
    assert {join["left"], join["right"]} == {"Cities", "Regions"}
    assert join["conditions"] == [{"left_field": "[Cities$].[Region ID]", "right_field": "[Regions$].[Region ID]"}]


def test_collection_relation_descends_to_leaf_tables():
    """A <relation type='collection'> (or join/union) is a container wrapping child relations (a
    multi-file union). The parser must descend it to the underlying leaf tables rather than emitting
    one opaque 'collection' table, so each physical table is captured."""
    spec = parse_workbook(FIXTURE)
    tables = spec["data_sources"][0]["tables"]
    names = {t["name"] for t in tables}
    assert {"Cities", "Regions"} <= names
    # the wrapper itself must NOT surface as a table
    assert all(t["source_relation"] != "collection" for t in tables)
    assert all(t["source_relation"] == "table" for t in tables if t["name"] in {"Cities", "Regions"})


def test_metadata_only_physical_column_recovered():
    """Physical/extract columns that appear only in <metadata-records> (no <column> element) must be
    recovered into fields[] with from_metadata_record=True, deduped against existing <column> fields,
    and role-inferred from local-type (integer/real -> measure)."""
    spec = parse_workbook(FIXTURE)
    fields = {f["caption"]: f for f in spec["data_sources"][0]["fields"]}
    # 'Region Code' exists only as a metadata-record -> recovered.
    assert "Region Code" in fields
    region = fields["Region Code"]
    assert region["from_metadata_record"] is True
    assert region["kind"] == "column"
    assert region["data_type"] == "integer"
    assert region["role"] == "measure"
    # 'Name' has a real <column> element AND a metadata-record with the same [NAME] internal name ->
    # must NOT be duplicated, and the surfaced <column> entry (not the metadata one) is kept.
    name_entries = [f for f in spec["data_sources"][0]["fields"] if f["internal_name"] == "[NAME]"]
    assert len(name_entries) == 1
    assert name_entries[0].get("from_metadata_record", False) is False


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


def test_relative_date_filter_class_captured():
    """Tableau filter classes use hyphens (relative-date, parameter-binding), not underscores. The
    parser emits the raw class verbatim, so the schema enum must accept the hyphenated forms (a real
    'relative-date' filter surfaced from Tableau Public's CaseOverview-ServiceDesk workbook)."""
    spec = parse_workbook(FIXTURE)
    filter_types = {f["type"] for ws in spec["worksheets"] for f in ws["filters"]}
    assert "relative-date" in filter_types


def test_parameter_equality_filter_idiom_flagged():
    """The IF [Param]=[Dim] THEN [Dim] END + exclude-null pattern should be recognized and annotated
    so pbi-semantic-builder simplifies it to a plain slicer instead of recreating it as DAX."""
    spec = parse_workbook(FIXTURE)
    filt = spec["worksheets"][0]["filters"][0]
    assert filt["exclude_nulls"] is True
    assert filt["note"] is not None
    assert "plain PBI slicer" in filt["note"]


def test_dashboard_actions_parsed_and_attached_to_source_dashboard():
    """Workbook <actions> (cross-sheet filter/highlight/URL wiring) must be parsed and attached to their
    source dashboard, with type + run_on + source worksheet resolved (here: an on-select filter action
    on the 'main' dashboard sourced from the 'Sales Gauge' worksheet)."""
    spec = parse_workbook(FIXTURE)
    main_dash = next(d for d in spec["dashboards"] if d["name"] == "main")
    assert len(main_dash["actions"]) == 1
    action = main_dash["actions"][0]
    assert action["type"] == "filter"
    assert action["run_on"] == "select"
    assert action["source_worksheet_id"] == spec["worksheets"][0]["id"]


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


def test_internal_object_id_pseudo_column_flagged_not_silently_dropped():
    """Tableau's relationship-model data sources carry a '[__tableau_internal_object_id__]'-prefixed
    pseudo-column per physical table (datatype='table') that isn't real data. It must still be parsed
    (never silently dropped) but flagged so pbi-semantic-builder knows to exclude it."""
    spec = parse_workbook(FIXTURE)
    fields = {f["caption"]: f for f in spec["data_sources"][0]["fields"]}
    pseudo_col = fields["cities.csv"]
    assert pseudo_col["data_type"] == "table"
    assert any(
        item["item"] == pseudo_col["id"] and "internal" in item["issue"] for item in spec["limitations_encountered"]
    )


def test_spatial_field_flagged_as_high_severity_capability_gap():
    """MAKEPOINT/MAKELINE-derived 'spatial' fields (map geometry) have no native DAX/Power Query
    equivalent - flagged high severity so it's triaged as a design decision, not silently dropped."""
    spec = parse_workbook(FIXTURE)
    fields = {f["caption"]: f for f in spec["data_sources"][0]["fields"]}
    spatial_field = fields["City Point"]
    assert spatial_field["data_type"] == "spatial"
    assert any(
        item["item"] == spatial_field["id"] and item["severity"] == "high" and "spatial" in item["issue"]
        for item in spec["limitations_encountered"]
    )


def test_floating_dashboard_captures_all_sibling_zones():
    """A Tableau 'Floating' layout dashboard serializes <zones> as flat sibling <zone> elements with
    no wrapping root container. All siblings must be captured (as a synthesized 'layout-floating'
    root's children), not just the first one a naive .find() would grab."""
    spec = parse_workbook(FIXTURE)
    floating = next(d for d in spec["dashboards"] if d["name"] == "floating")
    assert floating["size"]["sizing_mode"] == "automatic"
    root_zone = floating["zones"]
    assert root_zone["type"] == "layout-floating"
    assert len(root_zone["children"]) == 3


def test_floating_dashboard_paramctrl_and_bitmap_zone_types_resolved():
    spec = parse_workbook(FIXTURE)
    floating = next(d for d in spec["dashboards"] if d["name"] == "floating")
    children_by_type = {z["type"]: z for z in floating["zones"]["children"]}
    assert set(children_by_type) == {"parameter", "worksheet", "image"}
    assert children_by_type["parameter"]["field_id"] == spec["parameters"][0]["id"]
    assert children_by_type["worksheet"]["worksheet_id"] == spec["worksheets"][0]["id"]
