r"""
Deterministic TMDL/PBIP generator for the FastFashionImpact semantic model.

Migrates the Tableau workbook "Fast Fashion's Environmental Wake Up Call" (11 extract
data sources, 34 worksheets, 1 infographic dashboard) into a Fabric PBIP semantic model,
mirroring the conventions of migrations/electricity-per-capita (DataFolder M-parameter,
per-source import table, disconnected parameter table, /// lineage comments).

KEY MODELING FACTS (see DISPOSITIONS.md / RELATIONSHIPS.md for the full write-up):
  * No genuine Tableau data blends exist - every worksheet binds exactly ONE real data
    source (the parser's "multi-source" flags are Parameters-pseudo-source references).
  * The dashboard's only actions are 3 edit-parameter-actions (sheet-swap nav), NOT
    cross-worksheet filter/highlight actions -> no relationship-equivalents there.
  * Two clean value<->geometry key pairs are materialised as deliberate single-direction
    relationships (documented as composite-model enhancements, not Tableau joins).
  * __tableau_internal_object_id__ table-anchor pseudo-columns are excluded (guide s8).

Run:  .venv\Scripts\python.exe migrations\fast-fashion-impact\fabric\_validation\generate_tmdl.py
"""
import os, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
FABRIC = os.path.abspath(os.path.join(HERE, ".."))
MIG = os.path.abspath(os.path.join(FABRIC, ".."))
DEFN = os.path.join(FABRIC, "FastFashionImpact.SemanticModel", "definition")
TABLES_DIR = os.path.join(DEFN, "tables")
DATA_FOLDER = os.path.join(MIG, "data") + os.sep   # absolute, trailing sep (matches analog)

NS = uuid.UUID("6b1f0e2a-fa57-4c11-9d3e-fa57fa57fa57")
def lt(*parts):
    return str(uuid.uuid5(NS, "|".join(parts)))

def q(name):
    """Quote a TMDL identifier, doubling embedded single quotes."""
    return "'" + name.replace("'", "''") + "'"

TAB = "\t"

# ---------------------------------------------------------------------------
# M type + TMDL dataType helpers
# ---------------------------------------------------------------------------
# kind: "int" -> Int64.Type/int64 ; "num" -> type number/double ; "text" -> type text/string
MTYPE = {"int": "Int64.Type", "num": "type number", "text": "type text"}
DTYPE = {"int": "int64", "num": "double", "text": "string"}

def col_block(c):
    """Emit a physical (source) column."""
    lines = []
    if c.get("comment"):
        lines.append(f"{TAB}/// {c['comment']}")
    lines.append(f"{TAB}column {q(c['name'])}")
    lines.append(f"{TAB}{TAB}dataType: {DTYPE[c['kind']]}")
    lines.append(f"{TAB}{TAB}lineageTag: {lt('col', c['table'], c['name'])}")
    lines.append(f"{TAB}{TAB}summarizeBy: {c['summarize']}")
    lines.append(f"{TAB}{TAB}sourceColumn: {c['source']}")
    lines.append("")
    lines.append(f"{TAB}{TAB}annotation SummarizationSetBy = Automatic")
    lines.append("")
    return "\n".join(lines)

def calc_col_block(c):
    """Emit a calculated column (single-line DAX)."""
    lines = []
    if c.get("comment"):
        lines.append(f"{TAB}/// {c['comment']}")
    lines.append(f"{TAB}column {q(c['name'])} = {c['expr']}")
    lines.append(f"{TAB}{TAB}dataType: {DTYPE[c['kind']]}")
    lines.append(f"{TAB}{TAB}lineageTag: {lt('calc', c['table'], c['name'])}")
    lines.append(f"{TAB}{TAB}summarizeBy: {c['summarize']}")
    if c.get("format"):
        lines.append(f"{TAB}{TAB}formatString: {c['format']}")
    lines.append("")
    lines.append(f"{TAB}{TAB}annotation SummarizationSetBy = Automatic")
    lines.append("")
    return "\n".join(lines)

def measure_block(m, table):
    lines = []
    if m.get("comment"):
        lines.append(f"{TAB}/// {m['comment']}")
    lines.append(f"{TAB}measure {q(m['name'])} = {m['expr']}")
    if m.get("format"):
        lines.append(f"{TAB}{TAB}formatString: {m['format']}")
    lines.append(f"{TAB}{TAB}lineageTag: {lt('meas', table, m['name'])}")
    if m.get("folder"):
        lines.append(f"{TAB}{TAB}displayFolder: {m['folder']}")
    lines.append("")
    return "\n".join(lines)

def partition_csv(table, csv_name, cols):
    """import partition reading DATA_FOLDER & csv via Csv.Document with explicit culture."""
    typ = ", ".join("{" + f'"{c["source"]}", {MTYPE[c["kind"]]}' + "}" for c in cols)
    src = (
        f"{TAB}partition {q(table)} = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f'{TAB}{TAB}{TAB}{TAB}let\n'
        f'{TAB}{TAB}{TAB}{TAB}    Source = Csv.Document(File.Contents(DataFolder & "{csv_name}"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n'
        f'{TAB}{TAB}{TAB}{TAB}    #"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n'
        f'{TAB}{TAB}{TAB}{TAB}    #"Changed Type" = Table.TransformColumnTypes(#"Promoted Headers", {{{typ}}}, "en-US")\n'
        f'{TAB}{TAB}{TAB}{TAB}in\n'
        f'{TAB}{TAB}{TAB}{TAB}    #"Changed Type"\n'
    )
    return src

def partition_list(table, values, colname):
    vals = ", ".join(f'"{v}"' for v in values)
    src = (
        f"{TAB}partition {q(table)} = m\n"
        f"{TAB}{TAB}mode: import\n"
        f"{TAB}{TAB}source =\n"
        f'{TAB}{TAB}{TAB}{TAB}let\n'
        f'{TAB}{TAB}{TAB}{TAB}    Source = {{{vals}}},\n'
        f'{TAB}{TAB}{TAB}{TAB}    #"Converted to Table" = Table.FromList(Source, Splitter.SplitByNothing(), {{"{colname}"}}),\n'
        f'{TAB}{TAB}{TAB}{TAB}    #"Changed Type" = Table.TransformColumnTypes(#"Converted to Table", {{{{"{colname}", type text}}}}, "en-US")\n'
        f'{TAB}{TAB}{TAB}{TAB}in\n'
        f'{TAB}{TAB}{TAB}{TAB}    #"Changed Type"\n'
    )
    return src

def emit_table(t):
    out = []
    if t.get("comment"):
        out.append(f"/// {t['comment']}")
    out.append(f"table {q(t['name'])}")
    out.append(f"{TAB}lineageTag: {lt('table', t['name'])}")
    out.append("")
    for m in t.get("measures", []):
        out.append(measure_block(m, t["name"]))
    for c in t.get("columns", []):
        c["table"] = t["name"]
        out.append(col_block(c))
    for c in t.get("calc_columns", []):
        c["table"] = t["name"]
        out.append(calc_col_block(c))
    if t.get("csv"):
        out.append(partition_csv(t["name"], t["csv"], t["columns"]).rstrip("\n"))
    else:
        out.append(partition_list(t["name"], t["list_values"], t["columns"][0]["name"]).rstrip("\n"))
    out.append("")
    out.append(f"{TAB}annotation PBI_ResultType = Table")
    out.append("")
    return "\n".join(out)

# ---------------------------------------------------------------------------
# Column shorthand builders
# ---------------------------------------------------------------------------
def C(name, source, kind, summarize="none", comment=None):
    return dict(name=name, source=source, kind=kind, summarize=summarize, comment=comment)

# Common polygon geometry columns (shapeType included per source that has it)
def poly_geo(has_shape_label=False):
    cols = [
        C("Shape Id", "shapeId", "int"),
        C("Shape Type", "shapeType", "text"),
    ]
    if has_shape_label:
        cols.append(C("Shape Label", "shapeLabel", "text"))
    cols += [
        C("Point Id", "pointId", "int"),
        C("Point X", "pointX", "int"),
        C("Point Y", "pointY", "int"),
    ]
    return cols

FLIP = "* -1"

# ===========================================================================
#  MODEL DEFINITION
# ===========================================================================
tables = []

# 1. Polygon Materials --------------------------------------------------------
tables.append(dict(
    name="Polygon Materials",
    comment="Custom polygon GEOMETRY for the fibre-materials silhouette (201 rows; 5 shapes x pointId). Tableau source 'Polygon Materials (Fast Fashion Data FINAL)'. Category (Cotton / Polymer synthetics / Wood-based synthetics) + Value (%) are baked into the extract. Backs worksheets 'POLYGON - materials' and 'REFS'. Independent table - no genuine blend (see RELATIONSHIPS.md).",
    csv="ds.polygon_materials_fast_fashion_data_final.csv",
    columns=poly_geo() + [
        C("Category", "Category", "text"),
        C("Value (%)", "Value (%)", "num", "sum"),
    ],
    calc_columns=[
        dict(name="Point X Flipped", expr="'Polygon Materials'[Point X] * -1", kind="int",
             summarize="none", comment="Tableau [Point X Flipped] = [pointX] * -1. Horizontal mirror of the polygon X so the silhouette faces the intended direction."),
    ],
))

# 2. JEANS (value table) ------------------------------------------------------
tables.append(dict(
    name="JEANS",
    comment="Jeans lifecycle CO2 value table (8 rows; ID 0-7). Tableau source 'JEANS (Fast Fashion Data FINAL)'. One row per silhouette shape - the same (Category, Value %) that is baked into 'Jeans Polygon'. Backs the 'Bar ' worksheet. Related 1->many to 'Jeans Polygon' on ID<->Shape Id (deliberate enhancement).",
    csv="ds.jeans_fast_fashion_data_final.csv",
    columns=[
        C("ID", "ID", "int", comment="Shape id 0-7; the clean key to 'Jeans Polygon'[Shape Id]."),
        C("Category", "Category", "text"),
        C("Value (%)", "Value (%)", "num", "sum"),
    ],
))

# 3. Jeans Polygon ------------------------------------------------------------
tables.append(dict(
    name="Jeans Polygon",
    comment="Custom polygon GEOMETRY for the jeans/CO2 silhouette (223 rows; 8 shapes x pointId). Tableau source 'Jeans Polygon (Fast Fashion Data FINAL)'. Category+Value(%) baked into the extract (mirror of 'JEANS'). Backs worksheet 'JEANS POLYGON - co2'.",
    csv="ds.jeans_polygon_fast_fashion_data_final.csv",
    columns=poly_geo(has_shape_label=True) + [
        C("Category", "Category", "text"),
        C("Value (%)", "Value (%)", "num", "sum"),
    ],
    calc_columns=[
        dict(name="Point X Flipped", expr="'Jeans Polygon'[Point X] * -1", kind="int",
             summarize="none", comment="Tableau [Point X FLipped] = [pointX] * -1. Horizontal mirror of the polygon X."),
    ],
))

# 4. Marimekko ----------------------------------------------------------------
tables.append(dict(
    name="Marimekko",
    comment="Market-share vs Fashion-Pulse-score marimekko source (13 rows). Tableau source 'Marimekko (Marikekko - Segments)'. Segment groups Price Positions; Market Share = column width, Pulse Score = height/color. Backs worksheets 'Marimekko' and 'MarketShare'. Independent.",
    csv="ds.marimekko_marikekko_segments.csv",
    columns=[
        C("Segment", "Segment", "text"),
        C("Price Position", "Price Position", "text"),
        C("Market Share", "Market Share", "int", "sum"),
        C("Pulse Score", "Pulse Score", "int", "sum"),
        C("Actual Pulse Score", "Actual Pulse Score", "int", "none",
          comment="Duplicate of Pulse Score (Tableau declared it string, values are the same clean integers). Kept as a label; summarizeBy none."),
    ],
    measures=[
        dict(name="Segment Pulse LOD",
             expr="CALCULATE(SUM('Marimekko'[Pulse Score]), ALLEXCEPT('Marimekko', 'Marimekko'[Segment]))",
             format="0", folder="LOD",
             comment="Tableau [Segment Pulse LOD] = {FIXED [Segment]: SUM([Pulse Score])}. DIMENSION-FIXED LOD -> ALLEXCEPT(Segment). GROUND TRUTH: Middle Segment=327, Premium=106, Lower Middle=196, n/a=80."),
    ],
))

# 5. Radial with Borders ------------------------------------------------------
tables.append(dict(
    name="Radial with Borders",
    comment="Environmental radar (radial) source WITH concentric grid rings (50 rows = 5 Impact Areas x 2 Years x 5 ring names C1-C5). Tableau source 'Radial with Borders (Radar Environmental)'. Radial Value is constant per (Impact Area, Year). Backs 'RADAR','PLANET LIMIT','R Chem','R ENergy','R Land','R Waste','R Water'. NOTE source typo 'Waste creation (Mn rons)' (should be 'tons') preserved faithfully.",
    csv="ds.radial_with_borders_radar_environmental.csv",
    columns=[
        C("Impact Area", "Impact Area", "text"),
        C("Actual Value", "Actual Value", "text", comment="Mixed blanks/numbers in source; typed text to avoid refresh nulling."),
        C("Radial Value", "Radial Value", "num", "sum"),
        C("Year", "Year", "int"),
        C("Name", "Name", "text", comment="Grid-ring name C1-C5."),
        C("Value", "Value", "int", comment="Grid-ring radius 1-5 (concentric pentagon guides)."),
    ],
    measures=[
        dict(name="Distance (r)", expr="AVERAGE('Radial with Borders'[Radial Value])",
             format="0.00", folder="Radar",
             comment="Tableau [Distance (r)] = AVG([Radial Value]); the polar radius. Constant per (Impact Area, Year). GROUND TRUTH: Energy 2015=2.97, Water 2030=1.80, Chemicals=4.80."),
    ],
    calc_columns=[
        dict(name="Angle",
             expr="(2 * PI() / CALCULATE(DISTINCTCOUNT('Radial with Borders'[Impact Area]), ALL('Radial with Borders'))) * RANKX(ALL('Radial with Borders'[Impact Area]), 'Radial with Borders'[Impact Area], , ASC, Dense)",
             kind="num", summarize="none", format="0.0000",
             comment="Tableau [Angle] = RUNNING_SUM((2*PI())/MIN({COUNTD([Impact Area])})). Cumulative polar angle, step = 2*PI/5 per Impact Area. ORDERING ASSUMPTION: alphabetical Impact-Area rank (only rotates the radar; polygon shape/values unchanged). Angles sum to 2*PI."),
        dict(name="X-axis", expr="'Radial with Borders'[Radial Value] * COS('Radial with Borders'[Angle])",
             kind="num", summarize="none", format="0.0000",
             comment="Tableau [X-axis] = [Distance (r)] * COS([Angle]). Row-level polar->cartesian X (Radial Value is constant per mark). Report may plot as a scatter/line radar."),
        dict(name="Y-axis", expr="'Radial with Borders'[Radial Value] * SIN('Radial with Borders'[Angle])",
             kind="num", summarize="none", format="0.0000",
             comment="Tableau [Y-axis] (internal 'X-axis (copy)') = [Distance (r)] * SIN([Angle]). Row-level polar->cartesian Y."),
    ],
))

# 6. Radial (Radar Environmental) - VESTIGIAL --------------------------------
tables.append(dict(
    name="Radial (Radar Environmental)",
    comment="VESTIGIAL source (5 rows, wide 2015/2030 format). Tableau source 'Radial (Radar Environmental)' - NO worksheet binds it (superseded by 'Radial with Borders'). Kept for completeness per repo 'never silently drop' discipline; its 10 calculated fields (Angle/Distance/X/Y + 6 empty-string labels) are dropped as vestigial.",
    csv="ds.radial_radar_environmental.csv",
    columns=[
        C("Impact Area", "Impact Area", "text"),
        C("2015 Actual", "2015 Actual", "int"),
        C("2030 Actual", "2030 Actual", "text", comment="Contains 'n/a'; typed text."),
        C("2015 Value", "2015 Value", "num", "sum"),
        C("2030 Value", "2030 Value", "num", "sum"),
        C("C1", "C1", "int"), C("C2", "C2", "int"), C("C3", "C3", "int"),
        C("C4", "C4", "int"), C("C5", "C5", "int"),
    ],
))

# 7. Performance Quartile -----------------------------------------------------
tables.append(dict(
    name="Performance Quartile",
    comment="Circularity performance by supply-chain stage x quartile (36 rows). Tableau source 'Performance Quartile (Quartile Performance)'. Backs the 8 stage panels ('1. design'...'8. end of use'), '10. avg', 'Sheet 22','Sheet 22 (2)'. Independent.",
    csv="ds.performance_quartile_quartile_performance.csv",
    columns=[
        C("Quartile", "Quartile", "text"),
        C("Chain Stage", "Chain Stage", "text"),
        C("Value", "Value", "int", "sum"),
    ],
))

# 8. Fibre Production 1970-2018 ----------------------------------------------
tables.append(dict(
    name="Fibre Production 1970-2018",
    comment="Global fibre production vs population time series, 1970-2018 in 5-year steps (44 rows). Tableau source 'Fibre Production 1970-2018 (Fast Fashion Data FINAL)'. Backs 'Cotton','CO2','POP Line','Asterisk','WATER','PROB'. Its 6 calculated fields are empty-string annotation-label placeholders (dropped -> report text). Independent.",
    csv="ds.fibre_production_1970_2018_fast_fashion_data_final.csv",
    columns=[
        C("Fibre", "Fibre", "text"),
        C("Year", "Year", "int"),
        C("Production (m tons per year)", "Production (m tons per year)", "num", "sum"),
        C("Population (billions)", "Population (billions)", "num", "sum"),
    ],
))

# 9. Polygon water ------------------------------------------------------------
tables.append(dict(
    name="Polygon water",
    comment="Custom polygon GEOMETRY for the water-droplet silhouette (179 rows; 7 shapes x pointId). Tableau source 'Polygon water (Fast Fashion Data FINAL)'. Category (Consumer wash / Fabric Mill / Industrial Laundry / Raw Materials) + Value(%) baked into the extract. Backs 'POLYGON - water'. Related many->1 to 'JEANS water' on Category (deliberate enhancement).",
    csv="ds.polygon_water_fast_fashion_data_final.csv",
    columns=poly_geo() + [
        C("Category", "Category", "text"),
        C("Value (%)", "Value (%)", "num", "sum"),
    ],
    calc_columns=[
        dict(name="Point X Flipped", expr="'Polygon water'[Point X] * -1", kind="int",
             summarize="none", comment="Tableau [Point X Flipped] = [pointX] * -1. Horizontal mirror of the droplet X."),
    ],
))

# 10. Ripped Polygon ----------------------------------------------------------
tables.append(dict(
    name="Ripped Polygon",
    comment="Custom polygon GEOMETRY for the ripped-fabric 'consumer importance' silhouette (115 rows; 5 shapes). Tableau source 'Ripped Polygon (Consumer Importance)'. Importance (5-level Likert) + Value baked in. Backs 'Rip Polygon','Rip Polygon (2)'. Independent.",
    csv="ds.ripped_polygon_consumer_importance.csv",
    columns=poly_geo() + [
        C("Importance", "Importance", "text"),
        C("Value", "Value", "int", "sum"),
    ],
))

# 11. JEANS water (value table) ----------------------------------------------
tables.append(dict(
    name="JEANS water",
    comment="Water-stage value table (4 rows; ID 0-3, one row per water Category). Tableau source 'JEANS water (Fast Fashion Data FINAL)'. Backs the 'Water' worksheet. Related 1->many to 'Polygon water' on Category (deliberate enhancement).",
    csv="ds.jeans_water_fast_fashion_data_final.csv",
    columns=[
        C("ID", "ID", "int"),
        C("Category", "Category", "text", comment="Clean key to 'Polygon water'[Category]."),
        C("Value (%)", "Value (%)", "num", "sum"),
    ],
))

# 12. Jeans Sheet Swap (disconnected parameter table) ------------------------
tables.append(dict(
    name="Jeans Sheet Swap",
    comment="Disconnected slicer for Tableau string parameter 'Jeans Sheet Swap' ([Parameter 3], members '1'/'2'/'3', default '1'). Drives which silhouette the infographic shows (1=CO2/jeans, 2=materials, 3=water) via the 3 dashboard edit-parameter-actions. Replaces the Tableau *flag calculated fields with SELECTEDVALUE toggle measures.",
    list_values=["1", "2", "3"],
    columns=[C("Jeans Sheet Swap", "Jeans Sheet Swap", "text",
               comment="Parameter code: 1=CO2/jeans silhouette, 2=materials, 3=water.")],
    measures=[
        dict(name="Jeans Sheet Swap Value",
             expr='SELECTEDVALUE(\'Jeans Sheet Swap\'[Jeans Sheet Swap], "1")',
             folder="Parameter",
             comment="Current slicer selection, defaulting to Tableau's current value '1' when nothing is selected."),
        dict(name="Show CO2 Jeans", expr='[Jeans Sheet Swap Value] = "1"', folder="View toggles",
             comment="Tableau [Co2 flag] = [Parameters].[Parameter 3] = '1'. Show the jeans/CO2 silhouette."),
        dict(name="Show Materials", expr='[Jeans Sheet Swap Value] = "2"', folder="View toggles",
             comment="Tableau [Materials flag] = [Parameters].[Parameter 3] = '2'. Show the materials silhouette."),
        dict(name="Show Water", expr='[Jeans Sheet Swap Value] = "3"', folder="View toggles",
             comment="Tableau [Water flag] = [Parameters].[Parameter 3] = '3'. Show the water-droplet silhouette."),
    ],
))

# ===========================================================================
#  RELATIONSHIPS (2 - deliberate single-direction composite-model enhancements)
# ===========================================================================
relationships = [
    dict(name="Jeans_JeansPolygon",
         from_tbl="Jeans Polygon", from_col="Shape Id",
         to_tbl="JEANS", to_col="ID"),
    dict(name="JeansWater_PolygonWater",
         from_tbl="Polygon water", from_col="Category",
         to_tbl="JEANS water", to_col="Category"),
]

def emit_relationships():
    out = []
    for r in relationships:
        out.append(f"relationship {r['name']}")
        out.append(f"{TAB}fromColumn: {q(r['from_tbl'])}.{q(r['from_col'])}")
        out.append(f"{TAB}toColumn: {q(r['to_tbl'])}.{q(r['to_col'])}")
        out.append(f"{TAB}fromCardinality: many")
        out.append(f"{TAB}toCardinality: one")
        out.append(f"{TAB}crossFilteringBehavior: oneDirection")
        out.append("")
    return "\n".join(out)

# ===========================================================================
#  Top-level files
# ===========================================================================
def emit_model():
    order = [t["name"] for t in tables]
    refs = "\n".join(f"ref table {q(n)}" for n in order)
    qorder = ",".join(f'"{n}"' for n in order)
    return (
        "model Model\n"
        f"{TAB}culture: en-US\n"
        f"{TAB}defaultPowerBIDataSourceVersion: powerBI_V3\n"
        f"{TAB}sourceQueryCulture: en-US\n"
        f"{TAB}dataAccessOptions\n"
        f"{TAB}{TAB}legacyRedirects\n"
        f"{TAB}{TAB}returnErrorValuesAsNull\n"
        "\n"
        f"annotation PBI_QueryOrder = [{qorder}]\n"
        "\n"
        "annotation __PBI_TimeIntelligenceEnabled = 0\n"
        "\n"
        f"{refs}\n"
    )

def emit_expressions():
    return (
        f'expression DataFolder = "{DATA_FOLDER}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n'
        f"{TAB}lineageTag: {lt('expr', 'DataFolder')}\n"
        "\n"
        f"{TAB}annotation PBI_ResultType = Text\n"
    )

def emit_database():
    return "database\n" + f"{TAB}compatibilityLevel: 1606\n"

PLATFORM = '''{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
  "metadata": {
    "type": "SemanticModel",
    "displayName": "FastFashionImpact"
  },
  "config": {
    "version": "2.0",
    "logicalId": "%s"
  }
}
''' % lt("platform", "FastFashionImpact")

PBISM = '''{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
  "version": "4.2",
  "settings": {
    "qnaEnabled": false
  }
}
'''

# ===========================================================================
#  Write everything
# ===========================================================================
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # TMDL/PBIP files are UTF-8; end with a single trailing newline
    if not text.endswith("\n"):
        text += "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("  wrote", os.path.relpath(path, FABRIC))

def main():
    print("Generating FastFashionImpact semantic model ...")
    write(os.path.join(DEFN, "database.tmdl"), emit_database())
    write(os.path.join(DEFN, "model.tmdl"), emit_model())
    write(os.path.join(DEFN, "expressions.tmdl"), emit_expressions())
    write(os.path.join(DEFN, "relationships.tmdl"), emit_relationships())
    for t in tables:
        write(os.path.join(TABLES_DIR, t["name"] + ".tmdl"), emit_table(t))
    write(os.path.join(FABRIC, "FastFashionImpact.SemanticModel", ".platform"), PLATFORM)
    write(os.path.join(FABRIC, "FastFashionImpact.SemanticModel", "definition.pbism"), PBISM)
    print(f"Done. {len(tables)} tables, {len(relationships)} relationships.")
    tot_meas = sum(len(t.get("measures", [])) for t in tables)
    tot_calc = sum(len(t.get("calc_columns", [])) for t in tables)
    tot_col = sum(len(t.get("columns", [])) for t in tables)
    print(f"     columns={tot_col} calc_columns={tot_calc} measures={tot_meas}")

if __name__ == "__main__":
    main()
