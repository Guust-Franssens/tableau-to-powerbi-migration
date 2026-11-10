"""
purpose: Unit tests for scripts/parse_tableau.py against small synthetic .twb/.tds fixtures.
usage:   pytest -q
"""

import copy
import json
import sys
from pathlib import Path

import jsonschema
import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import parse_tableau  # noqa: E402  (path insert must precede this import)
from parse_tableau import (  # noqa: E402
    _conn_attr,
    _custom_sql_limitations,
    _parse_published_datasource,
    _published_ds_name,
    _table_from_ref,
    parse_workbook,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE = FIXTURES / "minimal.twb"
PUBLISHED_FIXTURE = FIXTURES / "published_datasource.twb"
TDS_FIXTURE = FIXTURES / "standalone_datasource.tds"
FEDERATED_FIXTURE = FIXTURES / "federated_multi_connection.twb"
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "docs" / "migration-spec.schema.json").read_text())
CAPTIONLESS_TDS_FIXTURES = {
    "CustomSQL_Parameter_And_Doubled_Operators.tds": "ds.customsqlparametertest",
    "Meridian_Custom_SQL_Databricks.tds": "ds.meridiantripslivedatabricks",
    "Meridian_Custom_SQL_Snowflake.tds": "ds.meridiancalcgauntletlivesnowflake",
}


@pytest.fixture(name="spec", scope="module")
def _spec():
    return parse_workbook(FIXTURE)


def _fields(spec):
    return {f["caption"]: f for f in spec["data_sources"][0]["fields"]}


def _has_limitation(spec, item, text, severity=None):
    return any(
        entry["item"] == item and text in entry["issue"] and severity in (None, entry["severity"])
        for entry in spec["limitations_encountered"]
    )


def test_717_parser_retains_physical_identifiers_and_exact_leg_references(tmp_path):
    twb = tmp_path / "identity.twb"
    twb.write_text(
        '<workbook version="2024.1"><datasources><datasource name="ds">'
        '<connection class="federated"><named-connections>'
        '<named-connection name="dbx"><connection class="databricks" server="adb.example" '
        'dbname="samples" schema="default" /></named-connection>'
        '<named-connection name="sql"><connection class="sqlserver" server="sql.example" '
        'dbname="DB" /></named-connection></named-connections>'
        '<relation type="collection">'
        '<relation type="table" name="Display Trips" table="[nyctaxi].[trips]" connection="dbx" />'
        '<relation type="table" name="Display Trips" table="[dbo].[trips]" connection="sql" />'
        '<relation type="text" name="Query" connection="dbx">SELECT 1</relation>'
        "</relation></connection></datasource></datasources></workbook>",
        encoding="utf-8",
    )
    spec = parse_workbook(twb)
    source = spec["data_sources"][0]
    legs = source["connection"]["connections"]
    assert [leg["name"] for leg in legs] == ["dbx", "sql"]
    assert legs[0]["schema"] == "default", "the parser must not backfill the relation's schema"
    assert not legs[1].get("schema")
    assert [(table["name"], table.get("table"), table["connection"]) for table in source["tables"]] == [
        ("Display Trips", "[nyctaxi].[trips]", "dbx"),
        ("Display Trips", "[dbo].[trips]", "sql"),
        ("Query", None, "dbx"),
    ]
    assert source["tables"][2]["custom_sql"] == "SELECT 1"
    assert all("schema" not in table for table in source["tables"])

    jsonschema.validate(spec, SCHEMA)
    legacy = copy.deepcopy(spec)
    for leg in legacy["data_sources"][0]["connection"]["connections"]:
        leg.pop("name")
    for table in legacy["data_sources"][0]["tables"]:
        table.pop("table", None)
        table.pop("connection", None)
    jsonschema.validate(legacy, SCHEMA)


def test_parses_top_level_shape_and_parameter(spec):
    assert spec["migration_spec_version"] == "1.0"
    assert [len(spec[k]) for k in ("data_sources", "worksheets", "dashboards", "parameters")] == [1, 2, 3, 1]
    param = spec["parameters"][0]
    assert (param["caption"], param["current_value"]) == ("City_param", "Springfield")
    assert "Shelbyville" in param["allowed_values"]


def test_calculated_field_and_dependencies(spec):
    fields = _fields(spec)
    assert fields["City filter"]["kind"] == "calculated"
    assert fields["City filter"]["tableau_formula"].startswith("if [Parameters]")
    assert fields["Sales"]["id"] in fields["Sales Scaled"]["referenced_fields"]


def test_bin_fields_preserve_classic_modern_and_parameter_sizes(spec):
    fields = _fields(spec)
    got = {
        caption: (fields[caption]["kind"], fields[caption]["bin_size"], fields[caption]["bin_source_column"])
        for caption in ("Sales (bin)", "Pivot Values (bin)", "Profit (parameter bin)")
    }
    assert got == {
        "Sales (bin)": ("bin", "200", "[Sales]"),
        "Pivot Values (bin)": ("bin", "0.1", "[Pivot Field Values]"),
        "Profit (parameter bin)": ("bin", None, "[Profit]"),
    }
    assert fields["Pivot Values (bin)"]["bin_size_parameter"] is None
    assert fields["Profit (parameter bin)"]["bin_size_parameter"] == "[Parameters].[Parameter 2]"


@pytest.mark.parametrize("caption", ["Grand Total", "Sales Profit Correlation"])
def test_keywordless_lod_is_flagged_high_severity(spec, caption):
    """{SUM(...)} and {CORR(...)} are table-scoped LODs without FIXED/INCLUDE/EXCLUDE."""
    field = _fields(spec)[caption]
    assert field["is_lod"] is True
    assert _has_limitation(spec, field["id"], "LOD expression", "high")


def test_extract_connection_mode_detected(spec):
    connection = spec["data_sources"][0]["connection"]
    assert connection["mode"] == "extract"
    assert connection["hyper_file"] == "Data/test.twb Files/federated.hyper"


def test_worksheet_shelves_reference_lines_and_text_resolved(spec):
    worksheet = spec["worksheets"][0]
    assert worksheet["mark_type"] == "Circle"
    assert worksheet["encodings"]["rows"][0]["field_id"].endswith("__sales")
    assert worksheet["encodings"]["rows"][0]["aggregation"] == "SUM"
    assert {rl["label"] for rl in worksheet["reference_lines"]} == {"Min", "Max", "Total"}
    assert "total" in {rl["formula"] for rl in worksheet["reference_lines"]}
    enc = worksheet["encodings"]
    fields = {caption: f["id"] for caption, f in _fields(spec).items()}
    assert fields["Name"] in {f["field_id"] for f in enc["detail"]}
    assert fields["Sales Scaled"] in {f["field_id"] for f in enc["tooltip"]}
    # U+00C6/U+00A0 soft line-break sentinels must not leak into titles or tooltips.
    assert worksheet["title_text"] == "Revenue by Region\nFY1998"
    assert worksheet["customized_tooltip_text"] == "Sales:\n<[federated.testds1].[sum:SALES:qk]>"


def test_shelf_encodings_carry_the_table_calc_addressing_inputs(spec):
    """Rows/columns, derivation and manual_sort are the model-layer source for table-calc addressing."""
    worksheet = spec["worksheets"][0]
    enc = worksheet["encodings"]
    assert enc["rows"] and enc["columns"]
    for shelf in ("rows", "columns"):
        for pill in enc[shelf]:
            assert "field_id" in pill
            assert "derivation" in pill
    assert "manual_sort" in worksheet


def test_join_graph_and_collection_leaf_tables(spec):
    source = spec["data_sources"][0]
    assert len(source["joins"]) == 1
    join = source["joins"][0]
    assert join["type"] == "inner"
    assert {join["left"], join["right"]} == {"Cities", "Regions"}
    assert join["conditions"] == [{"left_field": "[Cities$].[Region ID]", "right_field": "[Regions$].[Region ID]"}]
    tables = source["tables"]
    assert {"Cities", "Regions"} <= {t["name"] for t in tables}
    assert all(t["source_relation"] != "collection" for t in tables)
    assert all(t["source_relation"] == "table" for t in tables if t["name"] in {"Cities", "Regions"})
    assert tables[0]["row_count"] == {"source": "unknown"}


def test_nested_join_table_recovered_from_condition_ref():
    assert _table_from_ref("[Table 1].[Countries and areas]") == "Table 1"
    assert _table_from_ref("[Regions$].[Region ID]") == "Regions$"
    assert _table_from_ref(None) is None
    assert _table_from_ref("unqualified") is None


def test_metadata_only_physical_column_recovered(spec):
    region = _fields(spec)["Region Code"]
    assert region["from_metadata_record"] is True
    assert (region["kind"], region["data_type"], region["role"]) == ("column", "integer", "measure")
    name_entries = [f for f in spec["data_sources"][0]["fields"] if f["internal_name"] == "[NAME]"]
    assert len(name_entries) == 1
    assert name_entries[0].get("from_metadata_record", False) is False


def test_measure_names_values_pivot_detected_and_resolved(spec):
    assert spec["worksheets"][0]["measure_names_values_pivot"] is None
    pivot = next(w for w in spec["worksheets"] if w["name"] == "Measure Values Chart")["measure_names_values_pivot"]
    assert pivot["axis"] == "columns"
    fields_by_id = {f["id"]: f for f in spec["data_sources"][0]["fields"]}
    assert {fields_by_id[fid]["caption"] for fid in pivot["pivoted_field_ids"]} == {"Sales", "Sales Scaled"}


def test_filter_classes_topn_and_parameter_equality_idiom(spec):
    filters = [f for ws in spec["worksheets"] for f in ws["filters"]]
    assert "relative-date" in {f["type"] for f in filters}
    topn = next(f for f in filters if f["type"] == "topn")
    assert topn["field_id"].endswith("__sales")
    assert (topn["direction"], topn["max"]) == ("top", "10")
    idiom = spec["worksheets"][0]["filters"][0]
    assert idiom["exclude_nulls"] is True
    assert "plain PBI slicer" in idiom["note"]


def test_dashboard_actions_and_zone_tree(spec):
    main_dash = next(d for d in spec["dashboards"] if d["name"] == "main")
    assert len(main_dash["actions"]) == 1
    action = main_dash["actions"][0]
    assert (action["type"], action["run_on"]) == ("filter", "select")
    assert action["source_worksheet_id"] == spec["worksheets"][0]["id"]
    top_zone = spec["dashboards"][0]["zones"]
    assert (
        next(z for z in top_zone["children"] if z["type"] == "worksheet")["worksheet_id"]
        == (spec["worksheets"][0]["id"])
    )
    assert next(z for z in top_zone["children"] if z["type"] == "text")["text_html"] == "Footer\nnote"


def test_limitations_cover_datasource_internal_id_and_spatial_fields(spec):
    assert any(item["item"] == spec["data_sources"][0]["id"] for item in spec["limitations_encountered"])
    fields = _fields(spec)
    assert fields["cities.csv"]["data_type"] == "table"
    assert _has_limitation(spec, fields["cities.csv"]["id"], "internal")
    assert fields["City Point"]["data_type"] == "spatial"
    assert _has_limitation(spec, fields["City Point"]["id"], "spatial", "high")


def test_zero_and_unreached_data_sources_raise_limitations(spec):
    """Issue #518: an empty spec must never look like a clean parse."""
    zero = parse_workbook(FIXTURES / "zero_datasources.twb")
    assert zero["data_sources"] == []
    assert _has_limitation(zero, "workbook", "datasources/datasource")
    mismatch = parse_workbook(FIXTURES / "datasource_shape_mismatch.twb")
    assert mismatch["data_sources"] == []
    hits = [i for i in mismatch["limitations_encountered"] if i["item"] == "workbook" and "did not reach" in i["issue"]]
    assert [hit["severity"] for hit in hits] == ["high"]
    assert not _has_limitation(spec, "workbook", "0 data source")
    assert not _has_limitation(spec, "workbook", "0 usable data source")


def test_floating_dashboard_zones_keep_every_object_type(spec):
    floating = next(d for d in spec["dashboards"] if d["name"] == "floating")
    assert floating["size"]["sizing_mode"] == "automatic"
    root_zone = floating["zones"]
    assert root_zone["type"] == "layout-floating"
    assert len(root_zone["children"]) == 7
    by_id = {z["id"]: z for z in root_zone["children"]}
    assert (by_id["30"]["type"], by_id["30"]["legend_kind"]) == ("legend", "color")
    assert (by_id["31"]["type"], by_id["31"]["legend_kind"]) == ("legend", "size")
    assert by_id["30"]["worksheet_id"] == spec["worksheets"][0]["id"]
    assert (by_id["32"]["type"], by_id["32"]["url"]) == ("web", "http://example.com/embedded")
    assert by_id["33"]["type"] == "button"
    by_type = {z["type"]: z for z in root_zone["children"]}
    assert {"parameter", "worksheet", "image"} <= set(by_type)
    assert by_type["parameter"]["field_id"] == spec["parameters"][0]["id"]
    issues = [item["issue"] for item in spec["limitations_encountered"] if item["item"] == floating["id"]]
    assert any("WEB PAGE object" in i and "http://example.com/embedded" in i for i in issues)
    assert any("STANDALONE color legend" in i for i in issues)
    assert any("navigation button" in i for i in issues)


def test_empty_dashboard_survives_parse_and_validates_against_schema(spec):
    empty = next(d for d in spec["dashboards"] if d["name"] == "empty")
    assert empty["zones"]["type"] == "layout-basic"
    assert empty["zones"]["children"] == []
    assert _has_limitation(spec, empty["id"], "EMPTY in the Tableau source")
    jsonschema.validate(spec, SCHEMA)


def test_embedded_datasource_is_not_flagged_as_published(spec):
    assert all("published_datasource" not in ds for ds in spec["data_sources"])


def test_published_datasource_key_survives_a_stale_id_and_raises_limitation():
    spec = parse_workbook(PUBLISHED_FIXTURE)
    ds = spec["data_sources"][0]
    assert ds["connection"]["class"] == "sqlproxy"
    published = ds["published_datasource"]
    assert (published["id"], published["site"]) == ("SalesMaster", "Finance")
    assert published["derived_from"].endswith("/datasources/SalesMaster?rev=1.0")
    assert published["key"] == "finance/salesmaster"
    assert published["revision"] not in published["key"]
    assert published["id_attribute"] == "SalesMaster_oldname"
    assert published["name_source"] == "derived-from"
    entries = [
        limit
        for limit in spec["limitations_encountered"]
        if limit["severity"] == "high" and "PUBLISHED Tableau data source" in limit["issue"]
    ]
    assert len(entries) == 1
    for fragment in (".tds", "finance/salesmaster", "bind every downstream report"):
        assert fragment in entries[0]["issue"]


def test_standalone_tds_parses_and_empty_repository_location_is_not_published():
    spec = parse_workbook(TDS_FIXTURE)
    assert len(spec["data_sources"]) == 1
    ds = spec["data_sources"][0]
    assert ds["connection"]["class"] == "postgres"
    formulas = {f["caption"]: f["tableau_formula"] for f in ds["fields"] if f.get("tableau_formula")}
    assert formulas["Amount Scaled"] == "[amount] * 100"
    assert spec["worksheets"] == [] and spec["dashboards"] == []
    assert "published_datasource" not in ds
    assert not [limit for limit in spec["limitations_encountered"] if "PUBLISHED Tableau data source" in limit["issue"]]


def test_captionless_datasources_use_stable_published_names_for_ids():
    """Issue #333: caption-less .tds roots used to all emit id='ds.'."""
    for fixture_name, expected_id in CAPTIONLESS_TDS_FIXTURES.items():
        ds = parse_workbook(FIXTURES / fixture_name)["data_sources"][0]
        assert ds["id"] == expected_id
        assert ds["caption"] == ds["published_datasource"]["id"]
        assert all(field["id"].startswith(f"fld.{expected_id.replace('.', '_')}__") for field in ds["fields"])


def test_unsluggable_datasource_name_falls_through_to_stable_ascii_name():
    def ids_by_internal_name(datasource_names: list[str]) -> dict[str, str]:
        datasources = "".join(
            f"<datasource name='{name}'><repository-location id='{published_name}' site='s' /></datasource>"
            for name, published_name in zip(datasource_names, ["売上マスタ", "顧客分析"], strict=True)
        )
        root = etree.fromstring(f"<workbook><datasources>{datasources}</datasources></workbook>")
        parsed, _, _ = parse_tableau.parse_data_sources(root, {}, parse_tableau.IdRegistry())
        return {ds["internal_name"]: ds["id"] for ds in parsed}

    forward = ids_by_internal_name(["federated.aaa111", "federated.bbb222"])
    assert forward == {"federated.aaa111": "ds.federated_aaa111", "federated.bbb222": "ds.federated_bbb222"}
    assert forward == ids_by_internal_name(["federated.bbb222", "federated.aaa111"])


def test_duplicate_datasource_id_guard_fails_loudly(monkeypatch):
    original_make = parse_tableau.IdRegistry.make

    def duplicate_datasource_id(self, prefix: str, *parts: str) -> str:
        return "ds.duplicate" if prefix == "ds" else original_make(self, prefix, *parts)

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
    root = etree.fromstring(
        "<workbook><datasources><datasource caption='Orders'>"
        "<column caption='Sales' name='[sales_a]' datatype='real' role='measure' type='quantitative' />"
        "<column caption='Sales' name='[sales_b]' datatype='real' role='measure' type='quantitative' />"
        "<column caption='Sales 1' name='[sales_1]' datatype='real' role='measure' type='quantitative' />"
        "</datasource></datasources></workbook>"
    )
    parsed, _, _ = parse_tableau.parse_data_sources(root, {}, parse_tableau.IdRegistry())
    assert [f["id"] for f in parsed[0]["fields"]] == [
        "fld.ds_orders__sales",
        "fld.ds_orders__sales_1",
        "fld.ds_orders__sales_1_1",
    ]


def test_parser_and_server_lineage_agree_on_the_dedup_key():
    """The parser's key must equal tableau_lineage.dedup_key, including a percent-encoded space."""
    from tableau_lineage import dedup_key  # noqa: PLC0415

    ds_el = etree.fromstring(
        "<datasource>"
        "<repository-location derived-from='https://x/t/Finance/datasources/Sales%20Master?rev=1.0'"
        " id='stale' path='/t/Finance/datasources' revision='1.0' site='Finance' />"
        "<connection class='sqlproxy' dbname='Sales Master' />"
        "</datasource>"
    )
    parsed = _parse_published_datasource(ds_el, {"database": "Sales Master"})
    assert parsed["id"] == "Sales Master"
    assert parsed["key"] == dedup_key("Finance", "Sales Master") == "finance/sales master"


@pytest.mark.parametrize(
    ("repo_attrs", "conn_class", "dbname", "expected"),
    [
        # dbname is the datasource name ONLY for sqlproxy; elsewhere it is a physical database.
        (
            "id='MeridianSalesLiveSnowflake'",
            "snowflake",
            "MERIDIAN",
            ("MeridianSalesLiveSnowflake", "repository-location.id"),
        ),
        ("id='new'", "sqlproxy", "dandan003", ("dandan003", "connection.dbname")),
        (
            "id='stale' derived-from='https://x/datasources/Sales%20Master?rev=1.0'",
            "sqlproxy",
            "OTHER",
            ("Sales Master", "derived-from"),
        ),
        ("id='Shared' site='s'", "snowflake", "PHYSICAL_DB", ("Shared", "repository-location.id")),
        ("id='Shared' site='s'", "sqlproxy", "Shared", ("Shared", "connection.dbname")),
    ],
)
def test_published_ds_name_precedence(repo_attrs, conn_class, dbname, expected):
    repo = etree.fromstring(f"<repository-location {repo_attrs} />".encode())
    assert _published_ds_name(repo, {"class": conn_class, "database": dbname}) == expected


def test_a_federated_datasource_reports_every_named_connection():
    """Every live leg must be reported; under-reporting disarms the credential gate."""
    conn = parse_workbook(FEDERATED_FIXTURE)["data_sources"][0]["connection"]
    assert [c["class"] for c in conn["connections"]] == ["azure_sqldb", "snowflake", "databricks"]
    assert all(c["powerbi_target"] == "live_source" for c in conn["connections"])
    servers = {c["server"] for c in conn["connections"]}
    assert "esookcu-vg56333.snowflakecomputing.com" in servers
    assert "adb-7405612403187675.15.azuredatabricks.net" in servers
    # The singular primary connection still describes the first leg.
    assert (conn["class"], conn["server"]) == ("azure_sqldb", "tableaumigration.database.windows.net")
    legs = {c["class"]: c for c in conn["connections"]}
    assert legs["databricks"].get("http_path") == "/sql/1.0/warehouses/abc123"
    assert legs["snowflake"].get("warehouse") == "COMPUTE_WH"


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


def test_feature_flagged_attributes_resolve_to_the_live_variant():
    """The .true (live) FCP variant wins over .false; a plain attribute wins over both."""
    el = etree.fromstring(FCP_WORKBOOK.encode()).find(".//named-connection/connection")
    assert _conn_attr(el, "v-http-path") == "/sql/1.0/warehouses/abc123"
    assert _conn_attr(el, "dbname") == "unity_catalog"
    plain = etree.fromstring(b"<connection dbname='plain' _.fcp.X.true...dbname='flagged' />")
    assert _conn_attr(plain, "dbname") == "plain"


# --- Issue #80: inherited custom SQL is copied verbatim; lint (REPORT, never repair) suspicious shapes ---


def _custom_sql_table(sql: str, relation: str | None = None) -> dict:
    table = {"id": "tbl.custom_sql_query", "name": "Custom SQL Query", "custom_sql": sql}
    if relation:
        table["source_relation"] = relation
    return table


@pytest.mark.parametrize(
    ("sql", "severities", "fragments"),
    [
        ("SELECT * FROM orders WHERE amount >> 10", ["medium"], [">>"]),
        ("select * from t -- keep only active\\nwhere active = 1", ["high"], ["does NOT terminate", "\\n"]),
        ("select * from t -- tab example\\tSELECT * FROM secrets", ["high"], ["\\t"]),
        ("SELECT 1 -- note\\nORDER BY 1", ["high"], []),
        ("SELECT 1; -- note\\nCREATE TABLE x (id INTEGER)", ["high"], []),
        ("SELECT 1 -- note\\n+ 1", ["high"], []),
        ("SELECT 1 -- note\\n-- continued comment", ["medium"], []),
        ("SELECT 1 -- note\\n/* continued comment */", ["medium"], []),
        ("SELECT 1 -- source C:\\temp\\new.csv", ["medium"], []),
        ("SELECT 1\\n+ 1 /* docs \\t harmless */", ["medium"], ["\\n", "\\t"]),
        ("SELECT 1 -- source C:\\temp\\new.csv\n+ 1", ["medium"], []),
        ("SELECT '\\n' -- \\n", None, []),
        ("SELECT * FROM t WHERE a < 10 AND b > 5 AND c <= 3 AND d >= 4 AND e <> 6", [], []),
        (r"SELECT * FROM t WHERE file_path = 'C:\Data\Files\export.csv'", [], []),
        ("SELECT * FROM t WHERE code = '<<active>>'", [], []),
        ("SELECT 1 -- << obsolete syntax", [], []),
        ("SELECT [a<<b] FROM (SELECT 1 AS [a<<b])", [], []),
        ("SELECT DISTINCT [a<<b]", [], []),
        ("SELECT COUNT([a<<b])", [], []),
        ("SELECT COUNT(*) [total << count] FROM orders", [], []),
        ("SELECT 1 [a<<b]", [], []),
        ("SELECT 1 [1<<b]", [], []),
        ("UPDATE [a<<b] SET x = 1", [], []),
        ("SELECT custom_array_value(1) [a<<b]", [], []),
        ("SELECT $tag$a << b$tag$", [], []),
        ("SELECT values[1 << 2] FROM t", ["medium"], []),
        ("SELECT (ARRAY[1, 2])[1 << 1]", ["medium"], []),
        ("SELECT values [1 << 2] FROM t", ["medium"], []),
        ("SELECT (ARRAY[10,20,30,40]) [1 << 1]", ["medium"], []),
        ("SELECT array_value(10,20,30,40) [1 << 1]", ["medium"], []),
        ("SELECT (ARRAY[']']) [1 << 1]", ["medium"], []),
        ("SELECT * FROM t WHERE code = '<<active>>' AND amount << 10", ["medium"], ["amount << 10"]),
    ],
)
def test_custom_sql_lint_patterns(sql, severities, fragments):
    limitations = _custom_sql_limitations(_custom_sql_table(sql))
    if severities is None:  # an EOF comment cannot swallow SQL, so every escape stays medium
        assert {entry["severity"] for entry in limitations} == {"medium"}
    else:
        assert [entry["severity"] for entry in limitations] == severities
    for fragment in fragments:
        assert fragment in limitations[0]["issue"]


def test_doubled_operator_is_reported_not_rewritten():
    table = _custom_sql_table("SELECT * FROM orders WHERE amount << 10")
    [entry] = _custom_sql_limitations(table)
    assert (entry["item"], entry["severity"], entry["stage"]) == ("tbl.custom_sql_query", "medium", "parse")
    assert "doubled comparison operator" in entry["issue"]
    assert "NOT auto-repaired" in entry["issue"]
    assert table["custom_sql"] == "SELECT * FROM orders WHERE amount << 10"


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ({"id": "tbl.orders", "name": "Orders", "source_relation": "table", "custom_sql": None}, None),
        (_custom_sql_table("-- only a comment", "custom-sql"), "no executable SQL statement"),
        (_custom_sql_table("/* only a comment */", "custom-sql"), "no executable SQL statement"),
    ],
)
def test_absent_or_comment_only_custom_sql(table, expected):
    limitations = _custom_sql_limitations(table)
    if expected is None:
        assert limitations == []
    else:
        assert [entry["severity"] for entry in limitations] == ["high"]
        assert expected in limitations[0]["issue"]


def test_custom_sql_relation_with_missing_sql_is_reported():
    datasource = parse_tableau.etree.fromstring(
        "<datasource><connection><relation name='Empty Custom SQL' type='text' /></connection></datasource>"
    )
    table = parse_tableau._parse_tables(datasource, parse_tableau.IdRegistry())[0]
    assert (table["source_relation"], table["custom_sql"]) == ("custom-sql", None)
    [entry] = _custom_sql_limitations(table)
    assert entry["severity"] == "high"
    assert "has no SQL text" in entry["issue"]


def test_parse_workbook_preserves_literal_escapes_in_custom_sql(tmp_path):
    sql = r"SELECT 1\n+ 1\tAS value"
    fixture = tmp_path / "literal_escapes.tds"
    fixture.write_text(
        f"<datasource name='Literal Escapes'><connection class='sqlite'><relation name='Custom SQL' "
        f"type='text'><![CDATA[{sql}]]></relation></connection></datasource>",
        encoding="utf-8",
    )
    assert parse_workbook(fixture)["data_sources"][0]["tables"][0]["custom_sql"] == sql


def test_custom_sql_lint_is_wired_into_collect_limitations_end_to_end():
    spec = parse_workbook(FIXTURES / "CustomSQL_Parameter_And_Doubled_Operators.tds")
    entries = [
        limit
        for limit in spec["limitations_encountered"]
        if limit["item"] == "tbl.custom_sql_query" and "doubled comparison operator" in limit["issue"]
    ]
    assert [entry["severity"] for entry in entries] == ["medium"]
    table = spec["data_sources"][0]["tables"][0]
    assert "<<" in table["custom_sql"] and ">>" in table["custom_sql"]
