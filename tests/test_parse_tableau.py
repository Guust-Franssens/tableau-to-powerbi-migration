"""
purpose: Unit tests for scripts/parse_tableau.py, using a small synthetic .twb fixture that exercises
         the core mechanics (calculated fields, column-instance resolution, the parameter-equality
         filter idiom, and reference-line/gauge parsing) without depending on a real customer workbook.
usage:   pytest -q
"""

import sys
import sqlite3
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import parse_tableau  # noqa: E402  (path insert must precede this import)
from parse_tableau import (  # noqa: E402
    _conn_attr,
    _custom_sql_limitations,
    _published_ds_name,
    _parse_published_datasource,
    parse_workbook,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "minimal.twb"
PUBLISHED_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "published_datasource.twb"
TDS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "standalone_datasource.tds"
FEDERATED_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "federated_multi_connection.twb"
CAPTIONLESS_TDS_FIXTURES = {
    "CustomSQL_Parameter_And_Doubled_Operators.tds": "ds.customsqlparametertest",
    "Meridian_Custom_SQL_Databricks.tds": "ds.meridiantripslivedatabricks",
    "Meridian_Custom_SQL_Snowflake.tds": "ds.meridiancalcgauntletlivesnowflake",
}


def test_parses_top_level_shape():
    spec = parse_workbook(FIXTURE)
    assert spec["migration_spec_version"] == "1.0"
    assert len(spec["data_sources"]) == 1
    assert len(spec["worksheets"]) == 2
    assert len(spec["dashboards"]) == 3
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


def test_bin_field_preserves_bin_class_and_size():
    """Tableau histogram bins serialize as calculation class='bin' with a bin-size, not as a normal
    formula-authored calculation. The parser must preserve the bin kind and width."""
    fields = {f["caption"]: f for f in parse_workbook(FIXTURE)["data_sources"][0]["fields"]}
    sales_bin = fields["Sales (bin)"]
    assert sales_bin["kind"] == "bin"
    assert sales_bin["bin_size"] == "200"
    assert sales_bin["bin_source_column"] == "[Sales]"


def test_modern_bin_field_preserves_size_formula_and_parameter():
    """Modern Tableau bin XML can use size/formula or size-parameter/formula instead of
    bin-size/column; those real variants must retain their bin semantics too."""
    fields = {f["caption"]: f for f in parse_workbook(FIXTURE)["data_sources"][0]["fields"]}

    pivot_bin = fields["Pivot Values (bin)"]
    assert pivot_bin["kind"] == "bin"
    assert pivot_bin["bin_size"] == "0.1"
    assert pivot_bin["bin_source_column"] == "[Pivot Field Values]"
    assert pivot_bin["bin_size_parameter"] is None

    parameter_bin = fields["Profit (parameter bin)"]
    assert parameter_bin["kind"] == "bin"
    assert parameter_bin["bin_size"] is None
    assert parameter_bin["bin_source_column"] == "[Profit]"
    assert parameter_bin["bin_size_parameter"] == "[Parameters].[Parameter 2]"


def test_keywordless_aggregate_lod_is_flagged_high_severity():
    """A Tableau LOD may be table-scoped with no FIXED/INCLUDE/EXCLUDE keyword, e.g. {SUM([Sales])}.
    It still needs the high-severity grain/filter-context warning."""
    spec = parse_workbook(FIXTURE)
    fields = {f["caption"]: f for f in spec["data_sources"][0]["fields"]}
    grand_total = fields["Grand Total"]
    assert grand_total["is_lod"] is True
    assert any(
        item["item"] == grand_total["id"] and item["severity"] == "high" and "LOD expression" in item["issue"]
        for item in spec["limitations_encountered"]
    )


def test_keywordless_corr_lod_is_flagged_high_severity():
    """Tableau documents {CORR([Sales], [Profit])} as a valid table-scoped LOD; detection must not
    drift behind Tableau's aggregate-function list."""
    spec = parse_workbook(FIXTURE)
    fields = {f["caption"]: f for f in spec["data_sources"][0]["fields"]}
    corr = fields["Sales Profit Correlation"]
    assert corr["is_lod"] is True
    assert any(
        item["item"] == corr["id"] and item["severity"] == "high" and "LOD expression" in item["issue"]
        for item in spec["limitations_encountered"]
    )


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


def test_formatted_text_soft_line_break_sentinels_do_not_leak():
    """Tableau writes standalone U+00C6/U+00A0 runs as invisible soft line-break sentinels inside
    formatted text. They must not leak into worksheet titles or customized tooltips."""
    worksheet = parse_workbook(FIXTURE)["worksheets"][0]
    assert worksheet["title_text"] == "Revenue by Region\nFY1998"
    assert worksheet["customized_tooltip_text"] == "Sales:\n<[federated.testds1].[sum:SALES:qk]>"
    assert "\u00c6" not in worksheet["title_text"]
    assert "\u00a0" not in worksheet["customized_tooltip_text"]


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


def test_nested_join_table_recovered_from_condition_ref():
    """A join operand that is itself a nested join carries no name/table attribute; the parser must
    recover the participating table from the on-clause's [Table].[Field] reference instead of emitting
    left=null (regression: UNICEF SOWC 2016's 13-way star join emitted 13 null lefts and failed schema
    validation with 'None is not of type string')."""
    from parse_tableau import _table_from_ref

    assert _table_from_ref("[Table 1].[Countries and areas]") == "Table 1"
    assert _table_from_ref("[Regions$].[Region ID]") == "Regions$"
    assert _table_from_ref(None) is None
    assert _table_from_ref("unqualified") is None


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


def test_parser_emits_unknown_table_row_count_without_guessing_zero():
    """Parse is offline and does not open .hyper files, so the normal generated hint is unknown."""
    table = parse_workbook(FIXTURE)["data_sources"][0]["tables"][0]
    assert table["row_count"] == {"source": "unknown"}
    assert "value" not in table["row_count"]


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


def test_topn_filter_carries_limit_and_validates_against_schema():
    """Tableau top-N filters serialize as class='topn' with direction/max attributes. They must not
    crash schema validation, and the limit must survive for the report builder to recreate the filter."""
    import json

    import jsonschema

    spec = parse_workbook(FIXTURE)
    topn_filter = next(f for ws in spec["worksheets"] for f in ws["filters"] if f["type"] == "topn")
    assert topn_filter["field_id"].endswith("__sales")
    assert topn_filter["direction"] == "top"
    assert topn_filter["max"] == "10"

    schema = json.loads((Path(__file__).resolve().parent.parent / "docs" / "migration-spec.schema.json").read_text())
    jsonschema.validate(spec, schema)


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
    assert text_zone["text_html"] == "Footer\nnote"
    assert "\u00c6" not in text_zone["text_html"]


def test_limitations_are_collected_not_silently_dropped():
    spec = parse_workbook(FIXTURE)
    assert any(item["item"] == spec["data_sources"][0]["id"] for item in spec["limitations_encountered"])


ZERO_DATASOURCES_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "zero_datasources.twb"
SHAPE_MISMATCH_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "datasource_shape_mismatch.twb"


def test_zero_data_sources_raises_a_limitation_instead_of_a_silent_clean_spec():
    """A workbook that genuinely has no data sources must say so in limitations_encountered, not
    return a schema-valid empty spec indistinguishable from a parser that failed silently (issue
    #518)."""
    spec = parse_workbook(ZERO_DATASOURCES_FIXTURE)
    assert spec["data_sources"] == []
    assert any(
        item["item"] == "workbook" and "datasources/datasource" in item["issue"]
        for item in spec["limitations_encountered"]
    )


def test_unreached_datasource_elements_raise_a_high_severity_limitation():
    """When <datasource> elements exist in the workbook but not under the standard
    'datasources/datasource' path, the parser must flag the root-shape mismatch (high severity)
    rather than reporting a genuinely-empty workbook (issue #518)."""
    spec = parse_workbook(SHAPE_MISMATCH_FIXTURE)
    assert spec["data_sources"] == []
    matches = [
        item
        for item in spec["limitations_encountered"]
        if item["item"] == "workbook" and "did not reach" in item["issue"]
    ]
    assert len(matches) == 1
    assert matches[0]["severity"] == "high"


def test_a_normal_workbook_with_real_data_sources_raises_no_zero_data_source_limitation():
    """Negative control: a workbook that parses real data sources must NOT raise the zero-data-source
    limitation - a limitation that fires on every normal workbook is worse than none (issue #518)."""
    spec = parse_workbook(FIXTURE)
    assert spec["data_sources"]
    assert not any(
        item["item"] == "workbook" and "0 data source" in item["issue"] for item in spec["limitations_encountered"]
    )
    assert not any(
        item["item"] == "workbook" and "0 usable data source" in item["issue"]
        for item in spec["limitations_encountered"]
    )


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
    assert len(root_zone["children"]) == 7


def test_standalone_legend_web_and_button_zones_are_not_collapsed_to_containers():
    """Regression (book_8-1-Dashboards): Tableau types standalone legends 'color'/'size'/'shape',
    Web Page objects 'web' and nav buttons 'dashboard-object'. None matched the parser's allow-list,
    so all of them silently became generic 'layout-basic' CONTAINERS - invisible to downstream
    consumers while still occupying real canvas (a web object filling 62%x46% of the page and a
    10%-wide legend rail vanished). They must survive with their real type."""
    spec = parse_workbook(FIXTURE)
    floating = next(d for d in spec["dashboards"] if d["name"] == "floating")
    by_id = {z["id"]: z for z in floating["zones"]["children"]}

    color_legend, size_legend = by_id["30"], by_id["31"]
    assert (color_legend["type"], color_legend["legend_kind"]) == ("legend", "color")
    assert (size_legend["type"], size_legend["legend_kind"]) == ("legend", "size")
    # a legend zone's name= is its OWNING WORKSHEET, which must still resolve
    assert color_legend["worksheet_id"] == spec["worksheets"][0]["id"]

    assert by_id["32"]["type"] == "web"
    assert by_id["32"]["url"] == "http://example.com/embedded"
    assert by_id["33"]["type"] == "button"


def test_dashboard_object_limitations_flag_web_legend_and_button():
    """Each recovered non-container object carries a Power BI capability consequence, so each must
    reach limitations_encountered rather than being silently rendered (or silently dropped)."""
    spec = parse_workbook(FIXTURE)
    dash_id = next(d["id"] for d in spec["dashboards"] if d["name"] == "floating")
    issues = [item["issue"] for item in spec["limitations_encountered"] if item["item"] == dash_id]
    assert any("WEB PAGE object" in i and "http://example.com/embedded" in i for i in issues)
    assert any("STANDALONE color legend" in i for i in issues)
    assert any("navigation button" in i for i in issues)


def test_floating_dashboard_paramctrl_and_bitmap_zone_types_resolved():
    spec = parse_workbook(FIXTURE)
    floating = next(d for d in spec["dashboards"] if d["name"] == "floating")
    children_by_type = {z["type"]: z for z in floating["zones"]["children"]}
    assert {"parameter", "worksheet", "image"} <= set(children_by_type)
    assert children_by_type["parameter"]["field_id"] == spec["parameters"][0]["id"]
    assert children_by_type["worksheet"]["worksheet_id"] == spec["worksheets"][0]["id"]


def test_empty_dashboard_survives_parse_and_validates_against_schema():
    """A dashboard the author created and never populated serializes as a self-closing <zones/>.
    Regression (book_8-1-Dashboards, an empty 'Commissions' dashboard): the parser emitted `{}` for
    its zone tree, which violates the spec's own zone schema (type/x/y/w/h required) and failed the
    WHOLE workbook parse over one empty dashboard. It must survive as a valid empty root zone and be
    reported as an empty dashboard in limitations_encountered."""
    import json

    import jsonschema

    spec = parse_workbook(FIXTURE)
    empty = next(d for d in spec["dashboards"] if d["name"] == "empty")
    assert empty["zones"]["type"] == "layout-basic"
    assert empty["zones"]["children"] == []
    assert any(
        item["item"] == empty["id"] and "EMPTY in the Tableau source" in item["issue"]
        for item in spec["limitations_encountered"]
    )
    schema = json.loads((Path(__file__).resolve().parent.parent / "docs" / "migration-spec.schema.json").read_text())
    jsonschema.validate(spec, schema)  # the crash was a schema failure, so assert the whole spec validates


def test_embedded_datasource_is_not_flagged_as_published():
    """A workbook-level <repository-location> (Tableau Public workbooks always carry one) must NOT be
    mistaken for a published *datasource*; only a datasource-scoped one counts."""
    spec = parse_workbook(FIXTURE)
    assert all("published_datasource" not in ds for ds in spec["data_sources"])


def test_published_datasource_detected_with_stable_dedup_key():
    spec = parse_workbook(PUBLISHED_FIXTURE)
    ds = spec["data_sources"][0]
    assert ds["connection"]["class"] == "sqlproxy"
    published = ds["published_datasource"]
    assert published["id"] == "SalesMaster"
    assert published["site"] == "Finance"
    assert published["derived_from"].endswith("/datasources/SalesMaster?rev=1.0")
    # The dedup key deliberately excludes revision + server host so the SAME published datasource
    # resolves identically across workbooks (and across republishes) -> one shared semantic model.
    assert published["key"] == "finance/salesmaster"
    assert published["revision"] not in published["key"]


def test_published_datasource_name_survives_a_stale_id_attribute():
    """Regression, found against a REAL Tableau Cloud workbook (vimosh0812/ai-bi-assistant new-ds.twb):
    the datasource had been renamed, leaving repository-location@id='new' while derived-from, dbname
    and caption all said 'dandan003'. Keying on @id would give two workbooks that share ONE published
    datasource two different keys, defeating de-duplication."""
    spec = parse_workbook(PUBLISHED_FIXTURE)
    published = spec["data_sources"][0]["published_datasource"]
    assert published["id_attribute"] == "SalesMaster_oldname"  # the stale value, kept for audit
    assert published["name_source"] == "derived-from"  # authoritative source won
    assert published["key"] == "finance/salesmaster"  # stale @id did NOT leak into the key


def test_standalone_tds_parses_into_data_sources_and_calculations():
    """A .tds has `<datasource>` as its ROOT (no <workbook><datasources> wrapper). Before this was
    handled the parser returned ZERO data sources *without erroring* -- a silent failure for exactly
    the artifact we tell users to export when a workbook points at a published data source."""
    spec = parse_workbook(TDS_FIXTURE)
    assert len(spec["data_sources"]) == 1
    ds = spec["data_sources"][0]
    assert ds["connection"]["class"] == "postgres"
    formulas = {f["caption"]: f["tableau_formula"] for f in ds["fields"] if f.get("tableau_formula")}
    assert formulas["Amount Scaled"] == "[amount] * 100"
    # A .tds legitimately has no worksheets/dashboards.
    assert spec["worksheets"] == []
    assert spec["dashboards"] == []


def test_empty_repository_location_in_tds_is_not_a_published_datasource():
    """Standalone .tds files carry an EMPTY `<repository-location />` placeholder even when they are
    not published (verified against tableau/document-api-python's datasource_test.tds). The element
    alone must not trigger the published-datasource flag, or every .tds would false-positive."""
    spec = parse_workbook(TDS_FIXTURE)
    assert "published_datasource" not in spec["data_sources"][0]
    assert not [limit for limit in spec["limitations_encountered"] if "PUBLISHED Tableau data source" in limit["issue"]]


def test_captionless_datasources_use_stable_published_names_for_ids():
    """Regression for #333, grounded in three real caption-less .tds exports.

    They used to all emit id='ds.' because standalone .tds roots often carry neither caption nor
    internal name. Published datasource identity is already parsed from repository-location, so it is
    the stable readable fallback before opaque formatted-name/hash fallbacks.
    """
    seen_ids = set()
    for fixture_name, expected_id in CAPTIONLESS_TDS_FIXTURES.items():
        spec = parse_workbook(Path(__file__).resolve().parent / "fixtures" / fixture_name)
        ds = spec["data_sources"][0]

        assert ds["id"] == expected_id
        assert ds["id"] != "ds."
        assert ds["caption"] == ds["published_datasource"]["id"]
        assert all(field["id"].startswith(f"fld.{expected_id.replace('.', '_')}__") for field in ds["fields"])
        assert not any(field["id"].startswith("fld.ds__") for field in ds["fields"])
        seen_ids.add(ds["id"])

    assert len(seen_ids) == len(CAPTIONLESS_TDS_FIXTURES)


def test_unsluggable_datasource_name_falls_through_to_stable_ascii_name():
    """Non-ASCII-only names must not short-circuit to slugify's constant 'field' fallback."""
    from lxml import etree  # noqa: PLC0415

    def ids_by_internal_name(datasource_names: list[str]) -> dict[str, str]:
        datasources = "".join(
            f"<datasource name='{name}'><repository-location id='{published_name}' site='s' /></datasource>"
            for name, published_name in zip(datasource_names, ["売上マスタ", "顧客分析"], strict=True)
        )
        root = etree.fromstring(f"<workbook><datasources>{datasources}</datasources></workbook>")
        parsed, _, _ = parse_tableau.parse_data_sources(root, {}, parse_tableau.IdRegistry())
        return {ds["internal_name"]: ds["id"] for ds in parsed}

    forward = ids_by_internal_name(["federated.aaa111", "federated.bbb222"])
    reversed_order = ids_by_internal_name(["federated.bbb222", "federated.aaa111"])

    assert forward == {
        "federated.aaa111": "ds.federated_aaa111",
        "federated.bbb222": "ds.federated_bbb222",
    }
    assert forward == reversed_order
    assert "ds.field" not in forward.values()


def test_duplicate_datasource_id_guard_fails_loudly(monkeypatch):
    """If id generation regresses, duplicate datasource ids must fail instead of corrupting the spec."""
    from lxml import etree  # noqa: PLC0415

    original_make = parse_tableau.IdRegistry.make

    def duplicate_datasource_id(self, prefix: str, *parts: str) -> str:
        if prefix == "ds":
            return "ds.duplicate"
        return original_make(self, prefix, *parts)

    root = etree.fromstring(
        "<workbook><datasources>"
        "<datasource caption='Orders'><connection class='federated' /></datasource>"
        "<datasource caption='Customers'><connection class='federated' /></datasource>"
        "</datasources></workbook>"
    )
    monkeypatch.setattr(parse_tableau.IdRegistry, "make", duplicate_datasource_id)

    with pytest.raises(ValueError, match="Duplicate datasource id 'ds\\.duplicate'"):
        parse_tableau.parse_data_sources(root, {}, parse_tableau.IdRegistry())


def test_field_id_suffixes_cannot_collide_with_legitimate_slugged_names():
    """A generated _1 suffix must not collide with a real field whose caption slug already ends _1."""
    from lxml import etree  # noqa: PLC0415

    root = etree.fromstring(
        "<workbook><datasources>"
        "<datasource caption='Orders'>"
        "<column caption='Sales' name='[sales_a]' datatype='real' role='measure' type='quantitative' />"
        "<column caption='Sales' name='[sales_b]' datatype='real' role='measure' type='quantitative' />"
        "<column caption='Sales 1' name='[sales_1]' datatype='real' role='measure' type='quantitative' />"
        "</datasource>"
        "</datasources></workbook>"
    )

    parsed, _, _ = parse_tableau.parse_data_sources(root, {}, parse_tableau.IdRegistry())
    field_ids = [field["id"] for field in parsed[0]["fields"]]

    assert field_ids == ["fld.ds_orders__sales", "fld.ds_orders__sales_1", "fld.ds_orders__sales_1_1"]
    assert len(field_ids) == len(set(field_ids))


def test_published_datasource_raises_high_severity_limitation():
    spec = parse_workbook(PUBLISHED_FIXTURE)
    entries = [
        limit
        for limit in spec["limitations_encountered"]
        if limit["severity"] == "high" and "PUBLISHED Tableau data source" in limit["issue"]
    ]
    assert len(entries) == 1
    issue = entries[0]["issue"]
    assert ".tds" in issue  # tells the user which artifact to export
    assert "finance/salesmaster" in issue  # carries the dedup key
    assert "bind every downstream report" in issue  # states the shared-model rule


def test_parser_and_server_lineage_agree_on_the_dedup_key():
    """THE LINCHPIN: the key the parser derives from a workbook must equal the key
    scripts/tableau_lineage.py derives from the Tableau Metadata API for the same data source.

    If these two ever diverge, server-side lineage cannot be matched to locally parsed workbooks and
    the shared-model de-duplication silently fails. The nasty case is a name containing a space: the
    publish URL percent-encodes it ('Sales%20Master') while the API returns it plain."""
    from lxml import etree

    from tableau_lineage import dedup_key

    ds_el = etree.fromstring(
        "<datasource>"
        "<repository-location derived-from='https://x/t/Finance/datasources/Sales%20Master?rev=1.0'"
        " id='stale' path='/t/Finance/datasources' revision='1.0' site='Finance' />"
        "<connection class='sqlproxy' dbname='Sales Master' />"
        "</datasource>"
    )
    parsed = _parse_published_datasource(ds_el, {"database": "Sales Master"})
    assert parsed["id"] == "Sales Master"  # percent-decoded, not 'Sales%20Master'
    assert parsed["key"] == dedup_key("Finance", "Sales Master")
    assert parsed["key"] == "finance/sales master"


def test_a_federated_datasource_reports_every_named_connection():
    """A `class='federated'` datasource can wrap several live systems; report all of them.

    Measured 2026-08-04 on a real tri-source workbook (Azure SQL + Snowflake + Databricks joined in
    one Tableau datasource): the parser used `.find()` on the named-connection path, kept only the
    first, and `preflight_source_credentials` therefore armed the credential gate for 1 of 3 live
    systems while reporting the other two nowhere. Under-reporting live sources is the one direction
    the gate must never fail in.
    """
    spec = parse_workbook(FEDERATED_FIXTURE)
    conn = spec["data_sources"][0]["connection"]

    classes = [c["class"] for c in conn["connections"]]
    assert classes == ["azure_sqldb", "snowflake", "databricks"], "all three legs, in document order"
    assert all(c["powerbi_target"] == "live_source" for c in conn["connections"])

    servers = {c["server"] for c in conn["connections"]}
    assert "esookcu-vg56333.snowflakecomputing.com" in servers
    assert "adb-7405612403187675.15.azuredatabricks.net" in servers


def test_the_primary_connection_stays_backwards_compatible():
    """`connection` (singular) must keep describing the first leg.

    The schema, the 16 committed example specs and every existing consumer predate `connections[]`,
    so adding it must not move the primary.
    """
    conn = parse_workbook(FEDERATED_FIXTURE)["data_sources"][0]["connection"]
    assert conn["class"] == "azure_sqldb"
    assert conn["server"] == "tableaumigration.database.windows.net"
    assert conn["powerbi_target"] == "live_source"


def test_connector_specific_details_survive_per_leg():
    """Each leg keeps the details its connector needs, not just class and server.

    A Databricks M query needs the SQL-warehouse HTTP path and a Snowflake one needs the warehouse;
    losing them per-leg would make the connection unusable even once the credential is supplied.
    """
    legs = {c["class"]: c for c in parse_workbook(FEDERATED_FIXTURE)["data_sources"][0]["connection"]["connections"]}
    assert legs["databricks"].get("http_path") == "/sql/1.0/warehouses/abc123"
    assert legs["snowflake"].get("warehouse") == "COMPUTE_WH"


def test_shelf_encodings_carry_the_table_calc_addressing_inputs():
    """The shelves are MODEL-layer input, not just report-layer decoration. Do not trim them.

    Measured 2026-08-06 on the first real two-tier round trip. The deterministic tier stubs a
    formula-authored table calc as ``category: missing_addressing_intent`` and says outright:

        "This is a table calculation whose partition/order/scope (Tableau 'Compute Using') is not
         carried by the .tds. Recover the addressing from worksheet context (the .twb
         'ordering-type' + <order>/<sort> and the rows/cols shelves)."

    That was the LARGEST stub category in that workbook (3 of 4). The three fields asserted below are
    exactly what a caller needs to reconstruct the addressing:

      * ``rows`` / ``columns``  - which pill is being computed and what it is computed ALONG;
      * ``derivation``          - the grain of that axis (e.g. "tmn" = truncate-to-month), which
                                  decides the ORDER BY, not merely the display format;
      * ``manual_sort``         - an explicit ordering that overrides the natural one.

    The hazard this guards against is specific and was nearly realised: while planning the persona
    cuts, the parser's shelf extraction looked like duplicate work next to an engine that already
    rebuilds visuals, and was a candidate for removal. It is not duplicate - it is the only surviving
    source for the model-layer fallback path. A cut that removes it would not fail loudly; the calcs
    would simply stay stubs, or worse, be authored with a guessed order.
    """
    spec = parse_workbook(FIXTURE)
    worksheet = spec["worksheets"][0]
    enc = worksheet["encodings"]

    assert enc["rows"] and enc["columns"], "both shelves are needed to tell WHAT from ALONG-WHAT"
    for shelf in ("rows", "columns"):
        for pill in enc[shelf]:
            assert "field_id" in pill
            assert "derivation" in pill, f"{shelf} pill lost its derivation - the axis grain is what sets the ORDER BY"
    assert "manual_sort" in worksheet, "an explicit sort overrides the natural order and must survive"


FCP_WORKBOOK = """<workbook version='18.1'>
  <document-format-change-manifest>
    <_.fcp.DatabricksCatalog.true...DatabricksCatalog />
  </document-format-change-manifest>
  <datasources><datasource caption='D' name='d.0'><connection class='federated'>
    <named-connections><named-connection name='dbx.0'>
      <connection class='databricks' server='adb.azuredatabricks.net'
        _.fcp.DatabricksCatalog.false...dbname='/sql/1.0/warehouses/LEGACY'
        _.fcp.DatabricksCatalog.true...dbname='unity_catalog'
        _.fcp.DatabricksCatalog.true...v-http-path='/sql/1.0/warehouses/abc123' />
    </named-connection></named-connections>
  </connection></datasource></datasources></workbook>"""


def _fcp_connection_element():
    """The real Tableau shape: a manifest entry making `.true` live, and BOTH dbname variants."""
    from lxml import etree  # noqa: PLC0415

    return etree.fromstring(FCP_WORKBOOK.encode()).find(".//named-connection/connection")


def test_feature_flagged_attribute_is_resolved_when_only_the_FCP_spelling_exists():
    """`v-http-path` exists ONLY behind the flag, so a bare-name read returns None and the M dies.

    The existing federated fixture carries the BARE `v-http-path`, so it exercises the early-return
    branch and proves nothing about this path - which is why this test builds the flagged shape
    explicitly. Without it, a real Databricks .twbx yields `http_path: None` (measured 2026-08-05).
    """
    assert _conn_attr(_fcp_connection_element(), "v-http-path") == "/sql/1.0/warehouses/abc123"


def test_the_LIVE_variant_wins_and_a_blind_prefix_strip_would_corrupt_the_catalog():
    """`.true` and `.false` mean DIFFERENT things, so stripping the prefix is actively harmful.

    For DatabricksCatalog the `.false` variant of `dbname` holds the LEGACY meaning (the HTTP path)
    while `.true` holds the Unity catalog. A parser that strips the prefix and takes whichever it
    meets last writes `/sql/1.0/warehouses/...` into `database` - a wrong value that still looks
    like a value, so nothing downstream errors.
    """
    resolved = _conn_attr(_fcp_connection_element(), "dbname")
    assert resolved == "unity_catalog"
    assert not resolved.startswith("/sql/"), "the legacy .false variant overwrote the catalog"


def test_a_plain_attribute_still_wins_over_any_flagged_variant():
    """Control: the direct spelling is authoritative when present, so this cannot regress."""
    from lxml import etree  # noqa: PLC0415

    el = etree.fromstring(b"<connection dbname='plain' _.fcp.X.true...dbname='flagged' />")
    assert _conn_attr(el, "dbname") == "plain"


def _repo_and_conn(repo_attrs: str, conn_class: str, dbname: str | None):
    """One `<repository-location>` plus the connection dict the parser would hand alongside it."""
    from lxml import etree  # noqa: PLC0415

    repo = etree.fromstring(f"<repository-location {repo_attrs} />".encode())
    return repo, {"class": conn_class, "database": dbname}


def test_dbname_is_the_datasource_name_ONLY_for_sqlproxy():
    """For sqlproxy, `dbname` IS the published datasource. For anything else it is a DATABASE.

    Measured 2026-08-07 on a downloaded `.tds`: the connection is `snowflake` with
    `dbname='MERIDIAN'`, so an unguarded rule 2 keyed the datasource `.../meridian` while the two
    workbooks binding it keyed `.../meridiansaleslivesnowflake` from `derived-from`. The keys could
    never join, so one shared datasource silently failed to de-duplicate into one semantic model -
    and the failure is invisible, because both keys look reasonable on their own.
    """
    repo, conn = _repo_and_conn("id='MeridianSalesLiveSnowflake'", "snowflake", "MERIDIAN")
    name, source = _published_ds_name(repo, conn)
    assert name == "MeridianSalesLiveSnowflake"
    assert source == "repository-location.id", "a physical database name must never become the key"


def test_sqlproxy_dbname_still_wins_over_a_stale_repository_id():
    """The guard must not break rule 2 where it is sound - `id` can be a stale leftover after a rename."""
    repo, conn = _repo_and_conn("id='new'", "sqlproxy", "dandan003")
    assert _published_ds_name(repo, conn) == ("dandan003", "connection.dbname")


def test_derived_from_outranks_everything():
    """Control: the publish URL is authoritative, whatever the connection class says."""
    repo, conn = _repo_and_conn(
        "id='stale' derived-from='https://x/datasources/Sales%20Master?rev=1.0'", "sqlproxy", "OTHER"
    )
    assert _published_ds_name(repo, conn) == ("Sales Master", "derived-from")


def test_a_shared_datasource_and_its_workbooks_produce_ONE_key():
    """The whole point: a `.tds` and the `.twb`s that bind it must land on the SAME key.

    Without it, `published_datasource_registry.py` cannot match them, so one shared Tableau
    datasource becomes N near-identical semantic models instead of one with N reports bound to it.
    """
    tds_repo, tds_conn = _repo_and_conn("id='Shared' site='s'", "snowflake", "PHYSICAL_DB")
    twb_repo, twb_conn = _repo_and_conn("id='Shared' site='s'", "sqlproxy", "Shared")
    assert _published_ds_name(tds_repo, tds_conn)[0] == _published_ds_name(twb_repo, twb_conn)[0] == "Shared"


# --- Issue #80: inherited custom SQL is copied verbatim; lint (REPORT, never repair) suspicious ---
# --- shapes -- doubled comparison operators and literal \n/\t escapes in inherited SQL ---


def _custom_sql_table(sql: str) -> dict:
    return {"id": "tbl.custom_sql_query", "name": "Custom SQL Query", "custom_sql": sql}


def test_doubled_less_than_operator_is_flagged_but_sql_is_not_rewritten():
    """#80 shape 1: a corrupted '<' that became '<<'. REPORT, never REPAIR - the original SQL text
    must survive untouched in the table dict; only a limitation is added."""
    table = _custom_sql_table("SELECT * FROM orders WHERE amount << 10")
    limitations = _custom_sql_limitations(table)

    assert len(limitations) == 1
    entry = limitations[0]
    assert entry["item"] == "tbl.custom_sql_query"
    assert entry["severity"] == "medium"
    assert entry["stage"] == "parse"
    assert "doubled comparison operator" in entry["issue"]
    assert "NOT auto-repaired" in entry["issue"]
    assert table["custom_sql"] == "SELECT * FROM orders WHERE amount << 10"  # untouched


def test_doubled_greater_than_operator_is_flagged():
    """#80 shape 1, the mirror case: '>>' where '>' was meant."""
    table = _custom_sql_table("SELECT * FROM orders WHERE amount >> 10")
    limitations = _custom_sql_limitations(table)

    assert len(limitations) == 1
    assert "doubled comparison operator" in limitations[0]["issue"]
    assert ">>" in limitations[0]["issue"]


def test_literal_backslash_n_after_line_comment_is_flagged():
    """#80 shape 2, straight from the issue's own reproduction: a literal two-character '\\n' (not a
    real newline) after a '--' comment does not terminate it, so 'where active = 1' is silently
    swallowed into the comment by any SQL engine that reads '--' to the next REAL line break."""
    table = _custom_sql_table("select * from t -- keep only active\\nwhere active = 1")
    limitations = _custom_sql_limitations(table)

    assert len(limitations) == 1
    entry = limitations[0]
    assert entry["severity"] == "high"
    assert "does NOT terminate" in entry["issue"]
    assert "\\n" in entry["issue"]


def test_literal_backslash_t_after_line_comment_is_flagged():
    """#80 shape 2, the '\\t' variant named alongside '\\n' in the issue."""
    table = _custom_sql_table("select * from t -- tab example\\tSELECT * FROM secrets")
    limitations = _custom_sql_limitations(table)

    assert len(limitations) == 1
    assert "\\t" in limitations[0]["issue"]


def test_literal_escape_before_a_sql_clause_in_a_line_comment_is_high():
    """A real newline would make the following clause executable SQL."""
    limitations = _custom_sql_limitations(_custom_sql_table("SELECT 1 -- note\\nORDER BY 1"))

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "high"


def test_literal_escape_before_a_ddl_statement_in_a_line_comment_is_high():
    """A real newline would make the following DDL statement executable SQL."""
    limitations = _custom_sql_limitations(_custom_sql_table("SELECT 1; -- note\\nCREATE TABLE x (id INTEGER)"))

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "high"


def test_literal_escape_before_an_expression_continuation_in_a_line_comment_is_high():
    """A real newline would make the following expression continuation executable SQL."""
    limitations = _custom_sql_limitations(_custom_sql_table("SELECT 1 -- note\\n+ 1"))

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "high"


@pytest.mark.parametrize("continuation", ("-- continued comment", "/* continued comment */"))
def test_literal_escape_before_another_comment_is_medium(continuation):
    """A real newline before another comment would not expose executable SQL."""
    limitations = _custom_sql_limitations(_custom_sql_table(f"SELECT 1 -- note\\n{continuation}"))

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "medium"


def test_literal_escape_in_an_eof_line_comment_is_medium():
    """A line comment at EOF has no following SQL to swallow."""
    limitations = _custom_sql_limitations(_custom_sql_table("SELECT 1 -- source C:\\temp\\new.csv"))

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "medium"


def test_literal_escapes_outside_an_unterminated_line_comment_are_medium():
    """Literal escapes corrupt custom SQL wherever they occur, but only an unterminated line comment
    swallows trailing SQL."""
    table = _custom_sql_table("SELECT 1\\n+ 1 /* docs \\t harmless */")
    limitations = _custom_sql_limitations(table)

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "medium"
    assert "\\n" in limitations[0]["issue"]
    assert "\\t" in limitations[0]["issue"]


def test_literal_escape_in_a_line_comment_terminated_by_a_real_newline_is_medium():
    """A real newline terminates a '--' comment even when its body contains literal '\\n' text."""
    limitations = _custom_sql_limitations(_custom_sql_table("SELECT 1 -- source C:\\temp\\new.csv\n+ 1"))

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "medium"


def test_literal_escape_outside_an_eof_comment_keeps_its_medium_finding():
    """An EOF comment cannot swallow following SQL, so both escape occurrences remain medium."""
    limitations = _custom_sql_limitations(_custom_sql_table("SELECT '\\n' -- \\n"))

    assert {limitation["severity"] for limitation in limitations} == {"medium"}


def test_ordinary_comparison_operators_are_not_flagged():
    """Negative control: legitimate SQL using '<', '>', '<=', '>=' and '<>' (none doubled) must NOT
    be flagged. A lint with false positives on ordinary SQL trains reviewers to ignore it."""
    table = _custom_sql_table("SELECT * FROM t WHERE a < 10 AND b > 5 AND c <= 3 AND d >= 4 AND e <> 6")
    assert _custom_sql_limitations(table) == []


def test_legitimate_backslash_in_custom_sql_is_not_flagged():
    """Negative control: a realistic Windows file path (a real customer shape for a `.tds` pointing at
    a Databricks/CSV volume) carries genuine backslashes that are never followed by 'n' or 't', so no
    escape corruption exists. Must not be flagged."""
    table = _custom_sql_table(r"SELECT * FROM t WHERE file_path = 'C:\Data\Files\export.csv'")
    assert _custom_sql_limitations(table) == []


def test_doubled_operator_inside_a_string_literal_is_not_flagged():
    """Negative control: '<<'/'>>' appearing only as DATA inside a quoted string literal (e.g. a
    customer's own sentinel value) is not a corrupted SQL operator and must not be flagged - the
    issue's own suggested fix scopes the check to occurrences 'outside strings/comments'."""
    table = _custom_sql_table("SELECT * FROM t WHERE code = '<<active>>'")
    assert _custom_sql_limitations(table) == []


def test_doubled_operator_inside_a_line_comment_is_not_flagged():
    """Operators in comments are not SQL code and must remain masked."""
    assert _custom_sql_limitations(_custom_sql_table("SELECT 1 -- << obsolete syntax")) == []


def test_doubled_operator_inside_a_bracket_quoted_identifier_is_not_flagged():
    """SQL Server/SQLite bracket-quoted identifier content is not an operator."""
    assert _custom_sql_limitations(_custom_sql_table("SELECT [a<<b] FROM (SELECT 1 AS [a<<b])")) == []


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT DISTINCT [a<<b]",
        "SELECT COUNT([a<<b])",
        "SELECT COUNT(*) [total << count] FROM orders",
        "SELECT 1 [a<<b]",
        "SELECT 1 [1<<b]",
        "UPDATE [a<<b] SET x = 1",
    ),
)
def test_doubled_operator_in_bracket_identifier_contexts_is_not_flagged(sql):
    """Bracket identifiers can follow SQL modifiers, functions, and DML keywords."""
    assert _custom_sql_limitations(_custom_sql_table(sql)) == []


def test_spaced_bracket_alias_is_executable_and_not_flagged():
    """SQLite accepts a spaced bracket alias as an identifier, not executable operator syntax."""
    sql = "SELECT 1 [a<<b]"

    assert sqlite3.connect(":memory:").execute(sql).fetchone() == (1,)
    assert _custom_sql_limitations(_custom_sql_table(sql)) == []


def test_spaced_bracket_alias_after_an_array_value_suffix_function_is_not_flagged():
    """Only DuckDB's `array_value` constructor identifies a spaced postfix subscript."""
    sql = "SELECT custom_array_value(1) [a<<b]"
    connection = sqlite3.connect(":memory:")
    connection.create_function("custom_array_value", 1, lambda value: value)

    assert connection.execute(sql).fetchone() == (1,)
    assert _custom_sql_limitations(_custom_sql_table(sql)) == []


def test_doubled_operator_inside_an_array_or_subscript_expression_is_flagged():
    """An opening bracket immediately following an identifier is SQL expression syntax, not quoting."""
    limitations = _custom_sql_limitations(_custom_sql_table("SELECT values[1 << 2] FROM t"))

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "medium"


@pytest.mark.parametrize(
    "sql",
    ("SELECT (ARRAY[1, 2])[1 << 1]", "SELECT values [1 << 2] FROM t"),
)
def test_doubled_operator_inside_a_postfix_subscript_is_flagged(sql):
    """Postfix array subscripts remain expressions even after a parenthesis or whitespace."""
    assert len(_custom_sql_limitations(_custom_sql_table(sql))) == 1


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT (ARRAY[10,20,30,40]) [1 << 1]",
        "SELECT array_value(10,20,30,40) [1 << 1]",
        "SELECT (ARRAY[']']) [1 << 1]",
    ),
)
def test_doubled_operator_inside_a_spaced_array_subscript_is_flagged(sql):
    """Spaced postfix subscripts remain executable array expressions, not bracket aliases."""
    limitations = _custom_sql_limitations(_custom_sql_table(sql))

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "medium"


def test_doubled_operator_inside_a_postgres_dollar_quoted_string_is_not_flagged():
    """Dollar-quoted PostgreSQL literal content is not executable operator syntax."""
    assert _custom_sql_limitations(_custom_sql_table("SELECT $tag$a << b$tag$")) == []


def test_doubled_operator_outside_a_string_is_still_flagged_alongside_one_inside_a_string():
    """The masking must be surgical: suppress the match INSIDE the string literal while still
    catching a real doubled operator elsewhere in the same statement."""
    table = _custom_sql_table("SELECT * FROM t WHERE code = '<<active>>' AND amount << 10")
    limitations = _custom_sql_limitations(table)

    assert len(limitations) == 1  # NOT two - the string-literal '<<' must not add a second finding
    assert "amount << 10" in limitations[0]["issue"]


def test_table_with_no_custom_sql_is_not_scanned():
    """A regular (non custom-SQL) table has custom_sql=None; the lint must be a no-op, not an error."""
    table = {"id": "tbl.orders", "name": "Orders", "source_relation": "table", "custom_sql": None}
    assert _custom_sql_limitations(table) == []


def test_custom_sql_relation_with_missing_sql_is_reported():
    """A text relation without text is unassessable, not an ordinary non-custom-SQL table."""
    datasource = parse_tableau.etree.fromstring(
        "<datasource><connection><relation name='Empty Custom SQL' type='text' /></connection></datasource>"
    )
    table = parse_tableau._parse_tables(datasource, parse_tableau.IdRegistry())[0]

    assert table["source_relation"] == "custom-sql"
    assert table["custom_sql"] is None
    limitations = _custom_sql_limitations(table)

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "high"
    assert "has no SQL text" in limitations[0]["issue"]


@pytest.mark.parametrize("sql", ("-- only a comment", "/* only a comment */"))
def test_comment_only_custom_sql_is_reported(sql):
    """Comment-only text relations are unassessable rather than clean custom SQL."""
    table = {"id": "tbl.comment_only", "name": "Comment Only", "source_relation": "custom-sql", "custom_sql": sql}

    limitations = _custom_sql_limitations(table)

    assert len(limitations) == 1
    assert limitations[0]["severity"] == "high"
    assert "no executable SQL statement" in limitations[0]["issue"]


def test_parse_workbook_preserves_literal_escapes_in_custom_sql(tmp_path):
    """The production parser preserves literal escapes instead of converting them to whitespace."""
    sql = r"SELECT 1\n+ 1\tAS value"
    fixture = tmp_path / "literal_escapes.tds"
    fixture.write_text(
        f"<datasource name='Literal Escapes'><connection class='sqlite'><relation name='Custom SQL' "
        f"type='text'><![CDATA[{sql}]]></relation></connection></datasource>",
        encoding="utf-8",
    )

    spec = parse_workbook(fixture)

    assert spec["data_sources"][0]["tables"][0]["custom_sql"] == sql


def test_custom_sql_lint_is_wired_into_collect_limitations_end_to_end():
    """End-to-end regression for #80's own re-verification (issue comment 2026-09-02): parsing the
    committed real-shaped fixture must now flag the doubled '<<'/'>>' operators it carries, where
    previously the three emitted limitations covered other concerns only."""
    fixture = Path(__file__).resolve().parent / "fixtures" / "CustomSQL_Parameter_And_Doubled_Operators.tds"
    spec = parse_workbook(fixture)

    entries = [
        limit
        for limit in spec["limitations_encountered"]
        if limit["item"] == "tbl.custom_sql_query" and "doubled comparison operator" in limit["issue"]
    ]
    assert len(entries) == 1
    assert entries[0]["severity"] == "medium"
    # The underlying custom SQL must remain verbatim - the lint reports, it never rewrites.
    table = spec["data_sources"][0]["tables"][0]
    assert "<<" in table["custom_sql"]
    assert ">>" in table["custom_sql"]
