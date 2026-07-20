"""
TMDL generator for the 'Electricity generation per capita (2022)' Fabric semantic model.

Hand-authors the .SemanticModel/definition TMDL (no live Desktop/MCP available),
mirroring the proven tale-of-100-entrepreneurs / airline-alliance-activity models and
honouring every pbi-semantic-builder gotcha:
  * bare `database` + tab-indented compatibilityLevel
  * single-line DAX (incl. VAR..RETURN on one physical line)
  * DAX references a column's TMDL *name*, never its sourceColumn
  * no `'Table'[Col] = [Measure]` compact filters -> measures hoisted to VARs
  * measure names model-wide unique + never colliding with a same-table column
  * explicit "en-US" culture on every M type conversion
  * .pbip $schema ends in a literal numeric version

DESIGN (see DISPOSITIONS.md for the per-field rationale):
  5 source fact tables (one per Tableau data source) + 1 conformed Year dimension
  (deliberate composite-model enhancement) + 3 disconnected parameter tables.

  The Tableau workbook is NOT actually data-blended: every one of its 17 worksheets is
  single-source and there are no dashboard actions. The only genuine cross-table link is
  the `tree` source's internal relationship tree.csv[child] = "Elec generation"[Entity]
  (authoritative, from the .twb XML). That becomes the one many-to-many relationship.
  The shared Year key across the 4 year-bearing facts is materialised as a conformed
  Year dimension (the faithful translation of a "blend on Year"), single-direction.

  Dimension-scoped FIXED LODs  ->  CALCULATE(agg, ALLEXCEPT('T','T'[dim]) [, filter])
  WINDOW_max table calc        ->  CALCULATE(MAX, ALLEXCEPT(entity)) compared to MAX() in ctx
  Trellis/index/window-norm    ->  DROPPED (small-multiple layout = a report feature)
  Spatial MAKEPOINT (Sankey)   ->  CAPABILITY GAP, not modelled (underlying X/Y kept)
"""
import os, uuid, csv

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))            # ...\fabric
MODEL = os.path.join(BASE, "ElectricityPerCapita.SemanticModel")
DEFN = os.path.join(MODEL, "definition")
TABLES = os.path.join(DEFN, "tables")
DATA_FOLDER = os.path.abspath(os.path.join(BASE, "..", "data")) + "\\"           # ...\data\
NS = uuid.UUID("e1ec2c17-0000-4000-9000-000000000000")

def lt(*parts):
    return str(uuid.uuid5(NS, "|".join(parts)))

def esc_m(s):
    return s.replace('"', '""')

def one_line(s):
    return " ".join(s.split())

MTYPE = {"string": "type text", "int64": "Int64.Type", "double": "type number",
         "dateTime": "type datetime"}

# ---- resolve the three long "(adapted for visualization ...)" physical headers at runtime ----
def csv_header(csv_name):
    with open(os.path.join(DATA_FOLDER, csv_name), "r", encoding="utf-8-sig", newline="") as f:
        return next(csv.reader(f))

FNR_HDR = csv_header("ds.per_capita_electricity_fossil_nuclear_renewables.csv")
def find_col(hdr, prefix):
    for h in hdr:
        if h.startswith(prefix):
            return h
    raise KeyError(prefix + " not found in " + repr(hdr))
FF_SRC  = find_col(FNR_HDR, "Fossil fuel electricity per capita - kWh")
NUC_SRC = find_col(FNR_HDR, "Nuclear electricity per capita - kWh")
REN_SRC = find_col(FNR_HDR, "Renewable electricity per capita - kWh")

# ============================================================================
#  Fact table specs
#    col      : (display, source_csv_col, dataType, hidden, summarizeBy)
#    calc_col : (name, dax, dataType, hidden, summarizeBy, desc)
#    measure  : (name, dax, formatString|None, displayFolder|None, desc)
# ============================================================================
TABLE_SPECS = []

# ---- 1. Per Capita FNR  (source: per-capita-electricity-fossil-nuclear-renewables) ----
FNR = "'Per Capita FNR'"
TABLE_SPECS.append(dict(
    name="Per Capita FNR", csv="ds.per_capita_electricity_fossil_nuclear_renewables.csv",
    desc="OWID per-capita electricity by Entity x Year with fossil/nuclear/renewable kWh columns (5,956 rows; 223 entities incl. 'World'; 1985-2023). Tableau source 'per-capita-electricity-fossil-nuclear-renewables'. Backs the 3 'World ... only' worksheets.",
    cols=[
        ("Entity", "Entity", "string", False, "none"),
        ("Code", "Code", "string", False, "none"),
        ("Year", "Year", "int64", False, "none"),
        ("Fossil fuel electricity per capita - kWh", FF_SRC, "double", False, "sum"),
        ("Nuclear electricity per capita - kWh", NUC_SRC, "double", False, "sum"),
        ("Renewable electricity per capita - kWh", REN_SRC, "double", False, "sum"),
    ],
    calc_cols=[
        ("FF%", f"DIVIDE({FNR}[Fossil fuel electricity per capita - kWh], {FNR}[Fossil fuel electricity per capita - kWh] + {FNR}[Nuclear electricity per capita - kWh] + {FNR}[Renewable electricity per capita - kWh])",
         "double", False, "sum",
         "Tableau [FF%] = FF/(FF+Nuc+Ren), a row-level ratio. Row-level calc column (no top-level aggregation). Worksheet 'World FF only' shows SUM at one-row-per-year World grain -> summarizeBy=sum reproduces it. GROUND TRUTH World 2022 = 0.61432."),
        ("Nuclear%", f"DIVIDE({FNR}[Nuclear electricity per capita - kWh], {FNR}[Fossil fuel electricity per capita - kWh] + {FNR}[Nuclear electricity per capita - kWh] + {FNR}[Renewable electricity per capita - kWh])",
         "double", False, "sum",
         "Tableau [Nuclear%] = Nuc/(FF+Nuc+Ren). GROUND TRUTH World 2022 = 0.09152."),
        ("Renewables %", f"DIVIDE({FNR}[Renewable electricity per capita - kWh], {FNR}[Fossil fuel electricity per capita - kWh] + {FNR}[Nuclear electricity per capita - kWh] + {FNR}[Renewable electricity per capita - kWh])",
         "double", False, "sum",
         "Tableau [Renewables %] (internal 'Nuclear% (copy)') = Ren/(FF+Nuc+Ren). GROUND TRUTH World 2022 = 0.29416."),
    ],
    measures=[],
))

# ---- 2. Tree  (source: tree ; geometry rows only) ----
T = "'Tree'"
TABLE_SPECS.append(dict(
    name="Tree", csv="ds.tree.csv",
    desc="Sankey/treemap GEOMETRY rows exported from the 'tree' source (3,320 rows: 3,100 link + 216 node + 4 border). Pure layout coordinates; the consumption columns the tree source also exposes live in 'Elec Generation' (joined child=Entity). The Sankey itself is a HIGH capability gap - see limitations.",
    cols=[
        ("Index", "index", "int64", False, "none"),
        ("Path", "path", "int64", False, "none"),
        ("X", "x", "double", False, "none"),
        ("Y", "y", "double", False, "none"),
        ("Usage ration", "Usage ration", "double", False, "none"),
        ("Id", "id", "string", False, "none"),
        ("Parent", "parent", "string", False, "none"),
        ("Child", "child", "string", False, "none"),
        ("Usage", "Usage", "double", False, "none"),
        ("Type", "type", "string", False, "none"),
        ("Size", "size", "int64", False, "none"),
    ],
    calc_cols=[
        ("X Normalized", f"DIVIDE({T}[X], MAXX(ALL({T}), {T}[X]))", "double", False, "none",
         "Tableau [X Normalized] = [x]/{MAX([x])}. The {MAX([x])} is a partition-less LOD = grand-total max -> MAXX(ALL(Tree),[X])."),
        ("Y Normalized", f"DIVIDE({T}[Y], MAXX(ALL({T}), {T}[Y]))", "double", False, "none",
         "Tableau [Y Normalized] = [y]/{MAX([y])}."),
        ("X revised (for nodes)", f"IF({T}[Type] = \"node\" && NOT ISBLANK({T}[Usage]) && {T}[Path] = 0, DIVIDE({T}[Usage], MAXX(ALL({T}), {T}[Usage])) * {T}[X Normalized])",
         "double", False, "none",
         "Tableau [X revised (for nodes)] = IF type='node' AND NOT ISNULL(Usage) AND path=0 THEN (Usage/{MAX(Usage)})*[X Normalized] END."),
        ("Y revised (for nodes)", f"IF({T}[Type] = \"node\" && NOT ISBLANK({T}[Usage]) && {T}[Path] = 0, DIVIDE({T}[Usage], MAXX(ALL({T}), {T}[Usage])) * {T}[Y Normalized])",
         "double", False, "none",
         "Tableau [Y revised (for nodes)] (internal 'X revised (copy)') = same as X revised but scaling [Y Normalized]."),
        ("X fixed for last path", f"VAR mlp = CALCULATE(MAX({T}[Path]), ALLEXCEPT({T}, {T}[Id]), {T}[Type] = \"link\") RETURN IF({T}[Path] = mlp, ({T}[Usage ration] * {T}[X Normalized]) + {T}[X Normalized], {T}[X Normalized])",
         "double", False, "none",
         "DIMENSION-FIXED LOD [X fixed for last path]: IF [path] = {FIXED [id]: MAX(IF type='link' THEN path END)} THEN (Usage ration*[X Normalized])+[X Normalized] ELSE [X Normalized] END. {FIXED [id]: MAX(...)} -> CALCULATE(MAX(Path), ALLEXCEPT(Tree,Id), Type='link')."),
        ("Y fixed for last path", f"VAR mlp = CALCULATE(MAX({T}[Path]), ALLEXCEPT({T}, {T}[Id]), {T}[Type] = \"link\") RETURN IF({T}[Path] = mlp, ({T}[Usage ration] * {T}[Y Normalized]) + {T}[Y Normalized], {T}[Y Normalized])",
         "double", False, "none",
         "DIMENSION-FIXED LOD [Y fixed for last path] (internal 'X doubled for last path (copy)'): as X fixed for last path but on [Y Normalized]."),
        ("Calculation1", f"VAR a = CALCULATE(AVERAGE({T}[Usage]), ALLEXCEPT({T}, {T}[Id])) RETURN IF({T}[Type] = \"node\" && ISBLANK({T}[X revised (for nodes)]), a)",
         "double", False, "none",
         "DIMENSION-FIXED LOD [Calculation1]: IF type='node' AND ISNULL([X revised (for nodes)]) THEN {FIXED [id]: AVG(Usage)} END. {FIXED [id]: AVG(Usage)} -> CALCULATE(AVERAGE(Usage), ALLEXCEPT(Tree,Id))."),
        ("Consumption axis", f"{T}[Child]", "string", False, "none",
         "Tableau [Consumption axis] = [child] (passthrough alias)."),
        ("Child (copy)", f"{T}[Child]", "string", False, "none",
         "Tableau [Child (copy)] = [child] (passthrough duplicate)."),
        ("Source label", f"IF({T}[Type] = \"link\", {T}[Parent])", "string", False, "none",
         "Tableau [Source label] = IF type='link' THEN [parent] END."),
        ("Filter", f"{T}[Type] = \"link\" && ISBLANK({T}[Parent])", "string", False, "none",
         "Tableau [Filter] (boolean) = [type]='link' AND ISNULL([parent]); the top-of-tree link. Kept as a filterable flag column."),
    ],
    measures=[
        ("X Axis Bar chart", "0", "0.0", "Axis anchors",
         "Tableau [X Axis - Bar chart] = MIN(0.0): a constant 0 axis anchor for the small-multiple bar layout. Kept as a 0 measure."),
        ("X Axis Rank", "0", "0.0", "Axis anchors",
         "Tableau [X Axis - Rank] = MIN(0.0): constant 0 axis anchor."),
        ("X Axis Text", "0", "0.0", "Axis anchors",
         "Tableau [X Axis - Text] = MIN(0.0): constant 0 axis anchor."),
    ],
))

# ---- 3. Pivoted  (source: Pivoted (Per capita electricity generation by source)) ----
P = "'Pivoted'"
def pivoted_fixed(fuel):
    return f"CALCULATE(SUM({P}[Electricity generation per capita]), FILTER(ALLEXCEPT({P}, {P}[Year], {P}[Code]), {P}[Fuel Source] = \"{fuel}\"))"
TABLE_SPECS.append(dict(
    name="Pivoted", csv="ds.pivoted_per_capita_electricity_generation_by_source.csv",
    desc="Per-capita electricity generation unpivoted to one row per Country x Year x Fuel Source (16,269 rows; Fossil Fuel/Nuclear/Renewables; 1985-2022). Tableau source 'Pivoted'. Backs 8 worksheets (per-country bars, per-region lines, All regions trellis).",
    cols=[
        ("Country", "Entity", "string", False, "none"),
        ("Code", "Code", "string", False, "none"),
        ("Year", "Year", "int64", False, "none"),
        ("Region", "Region", "string", False, "none"),
        ("Fuel Source", "Fuel Source", "string", False, "none"),
        ("Electricity generation per capita", "Electricity generation per capita", "double", False, "sum"),
    ],
    calc_cols=[
        ("FF Electricity", pivoted_fixed("Fossil Fuel"), "double", False, "average",
         "DIMENSION-FIXED LOD [FF Electricity] = {FIXED [Year],[Code]: SUM(IF [Fuel Source]='Fossil Fuel' THEN [Electricity generation per capita] END)}. -> CALCULATE(SUM, FILTER(ALLEXCEPT(Year,Code), Fuel='Fossil Fuel')). GROUND TRUTH USA 2022 = 7559.257. summarizeBy=average because 'Region FF only' shows AVG([FF Electricity])."),
        ("Nuclear Electricity", pivoted_fixed("Nuclear"), "double", False, "average",
         "DIMENSION-FIXED LOD [Nuclear Electricity] = FIXED [Year],[Code] SUM of Nuclear per capita. GROUND TRUTH FRA 2022 = 4560.504."),
        ("Renewable Electricity", pivoted_fixed("Renewables"), "double", False, "average",
         "DIMENSION-FIXED LOD [Renewable Electricity] = FIXED [Year],[Code] SUM of Renewables per capita. GROUND TRUTH USA 2022 = 2832.334."),
        ("FF Electricity%", f"DIVIDE({P}[FF Electricity], {P}[FF Electricity] + {P}[Nuclear Electricity] + {P}[Renewable Electricity])",
         "double", False, "none",
         "Tableau [FF Electricity%] = FF/(FF+Nuc+Ren) over the FIXED LODs. GROUND TRUTH USA 2022 = 0.59652."),
        ("Nuclear Electricity%", f"DIVIDE({P}[Nuclear Electricity], {P}[FF Electricity] + {P}[Nuclear Electricity] + {P}[Renewable Electricity])",
         "double", False, "none",
         "Tableau [Nuclear Electricity%] = Nuc/(FF+Nuc+Ren). GROUND TRUTH FRA 2022 = 0.62795."),
        ("Renewable Electricity%", f"DIVIDE({P}[Renewable Electricity], {P}[FF Electricity] + {P}[Nuclear Electricity] + {P}[Renewable Electricity])",
         "double", False, "none",
         "Tableau [Renewable Electricity%] = Ren/(FF+Nuc+Ren). GROUND TRUTH USA 2022 = 0.22351."),
        ("Year for label", f"IF({P}[Year] = 2020, {P}[Year])", "int64", False, "none",
         "Tableau [Year for label] = IF [Year]=2020 THEN [Year] END (a single-year annotation anchor)."),
        ("Region and country count", f"{P}[Region] & UNICHAR(10) & UNICHAR(10) & CALCULATE(DISTINCTCOUNT({P}[Country]), ALLEXCEPT({P}, {P}[Region])) & \" countries\"",
         "string", False, "none",
         "DIMENSION-FIXED LOD [Region and country count] = [Region] + newlines + STR({FIXED [Region]: COUNTD([Entity])}) + ' countries'. {FIXED [Region]: COUNTD} -> CALCULATE(DISTINCTCOUNT(Country), ALLEXCEPT(Region))."),
    ],
    measures=[],
))

# ---- 4. Elec Generation  (source: Elec generation per capita + regions) + relocated tree consumption ----
E = "'Elec Generation'"
TABLE_SPECS.append(dict(
    name="Elec Generation", csv="ds.elec_generation_per_capita_regions.csv",
    desc="Per-capita electricity by Region/Entity/Year (6,061 rows; 209 countries + 7 World-Bank regions; no 'World' total; 1985-2023). Tableau source 'Elec generation per capita + regions' (its vestigial Entity=Entity self-join is dropped). Also HOSTS the tree source's consumption calcs, which physically resolve to this same 'Elec generation' table via child=Entity. Backs 'Average per region' + the tree consumption marks.",
    cols=[
        ("Region", "Region", "string", False, "none"),
        ("Income group", "Income group", "string", False, "none"),
        ("Entity", "Entity", "string", False, "none"),
        ("Code", "Code", "string", False, "none"),
        ("Year", "Year", "int64", False, "none"),
        ("Per capita electricity - kWh", "Per capita electricity - kWh", "double", False, "sum"),
    ],
    calc_cols=[
        ("Elec gen at 2022", f"IF({E}[Year] = 2022, {E}[Per capita electricity - kWh])", "double", False, "sum",
         "Tableau tree calc [Elec gen at 2022] = IF [Year]=2022 THEN [Per capita electricity - kWh] END. Relocated here (references Elec generation columns)."),
        ("Consumption at CY", f"VAR cy = CALCULATE(MAX({E}[Year]), ALL({E})) RETURN IF({E}[Year] = cy, {E}[Per capita electricity - kWh])",
         "double", False, "sum",
         "Tableau tree calc [Max year (copy)] = IF [Year]=[CY] THEN [Per capita electricity - kWh] END, where [CY]={MAX([Year])} (grand-total max year). Relocated row-level column."),
        ("Region and country count", f"{E}[Region] & UNICHAR(10) & UNICHAR(10) & CALCULATE(DISTINCTCOUNT({E}[Entity]), ALLEXCEPT({E}, {E}[Region])) & \" countries\"",
         "string", False, "none",
         "DIMENSION-FIXED LOD [Region and country count] = [Region] + newlines + STR({FIXED [Region]: COUNTD([Entity])}) + ' countries'."),
    ],
    measures=[
        ("CY", f"CALCULATE(MAX({E}[Year]), ALL({E}))", "0", "Consumption",
         "Tableau tree calc [CY] = {MAX([Year])} (partition-less LOD grand-total max year). ALL()-scoped MAX. = 2023 in the current extract."),
        ("CY Consumption", f"VAR cy = CALCULATE(MAX({E}[Year]), ALL({E})) RETURN CALCULATE(SUM({E}[Per capita electricity - kWh]), {E}[Year] = cy)",
         "#,##0.000", "Consumption",
         "DIMENSION-FIXED LOD [CY Consumption] = SUM({FIXED [child]: SUM(IF [Year]=[CY] THEN [Per capita electricity - kWh] END)}). CY hoisted to a VAR (never a '[Col]=[Measure]' compact filter). In a child/Entity context this is that entity's per-capita at the latest year. GROUND TRUTH child='Norway' @CY=2023 -> 28056.230."),
        ("Max year", f"VAR emax = CALCULATE(MAX({E}[Year]), ALLEXCEPT({E}, {E}[Entity])) RETURN IF(MAX({E}[Year]) = emax, SUM({E}[Per capita electricity - kWh]))",
         "#,##0.000", "Consumption",
         "WINDOW table calc [Max year] = IF WINDOW_MAX(MAX([Year]))=MAX([Year]) THEN SUM([Per capita electricity - kWh]) END = show consumption only on each country's latest year. WINDOW_MAX over the per-country partition -> CALCULATE(MAX(Year), ALLEXCEPT(Entity)); compared to MAX(Year) in the current (Year-axis) context."),
        ("Calculation2", f"VAR regavg = CALCULATE(AVERAGE({E}[Per capita electricity - kWh]), ALLEXCEPT({E}, {E}[Region])) RETURN [CY Consumption] > regavg",
         None, "Consumption",
         "Tableau tree calc [Calculation2] (boolean) = [CY Consumption] > SUM({FIXED [Region]: AVG([Per capita electricity - kWh])}). {FIXED [Region]: AVG} -> CALCULATE(AVERAGE, ALLEXCEPT(Region)); measure hoisted, not a compact filter."),
        ("Global avg in 2022", "3616.7", "#,##0.0", "Consumption",
         "Tableau [Global avg in 2022] = literal constant 3616.7 (equals the FNR 'World' entity's 2022 total per-capita 3616.692). A fixed reference-line value."),
        ("Ratio", f"DIVIDE(AVERAGE({E}[Per capita electricity - kWh]), [Global avg in 2022])", "0.00", "Consumption",
         "Tableau [Ratio] = AVG([Per capita electricity - kWh]) / AVG([Global avg in 2022]); the constant AVG is itself 3616.7. Each country/region's average per-capita vs the world 2022 benchmark."),
    ],
))

# ---- 5. By Source  (source: By source ; VESTIGIAL - no worksheet binds it) ----
B = "'By Source'"
TABLE_SPECS.append(dict(
    name="By Source", csv="ds.by_source.csv",
    desc="VESTIGIAL source 'By source' (caption 'Country & Regions', 5,496 rows) - NO worksheet in the workbook binds it (verified). Modelled for source-fidelity per the explicit 5-table requirement; safe to omit if trimming. Same FNR kWh columns plus Region/Income group.",
    cols=[
        ("Entity", "Entity", "string", False, "none"),
        ("Code", "Code", "string", False, "none"),
        ("Year", "Year", "int64", False, "none"),
        ("Fossil fuel electricity per capita - kWh", FF_SRC, "double", False, "sum"),
        ("Nuclear electricity per capita - kWh", NUC_SRC, "double", False, "sum"),
        ("Renewable electricity per capita - kWh", REN_SRC, "double", False, "sum"),
        ("Region", "Region", "string", False, "none"),
        ("Income group", "Income group", "string", False, "none"),
    ],
    calc_cols=[
        ("% of Fossil Fuel Electricity", f"DIVIDE({B}[Fossil fuel electricity per capita - kWh], {B}[Fossil fuel electricity per capita - kWh] + {B}[Nuclear electricity per capita - kWh] + {B}[Renewable electricity per capita - kWh])",
         "double", False, "sum",
         "Tableau [% of Fossil Fuel Electricity] = FF/(FF+Nuc+Ren) row-level ratio (vestigial source)."),
        ("% of Nuclear Electricity", f"DIVIDE({B}[Nuclear electricity per capita - kWh], {B}[Fossil fuel electricity per capita - kWh] + {B}[Nuclear electricity per capita - kWh] + {B}[Renewable electricity per capita - kWh])",
         "double", False, "sum",
         "Tableau [% of Nuclear Electricity] = Nuc/(FF+Nuc+Ren) (vestigial)."),
        ("% of Renewable Electricity", f"DIVIDE({B}[Renewable electricity per capita - kWh], {B}[Fossil fuel electricity per capita - kWh] + {B}[Nuclear electricity per capita - kWh] + {B}[Renewable electricity per capita - kWh])",
         "double", False, "sum",
         "Tableau [% of Renewable Electricity] = Ren/(FF+Nuc+Ren) (vestigial)."),
        ("Year for label", f"IF({B}[Year] = 2020, {B}[Year])", "int64", False, "none",
         "Tableau [Year for label] = IF [Year]=2020 THEN [Year] END (vestigial)."),
    ],
    measures=[],
))

# ============================================================================
#  Conformed Year dimension (deliberate composite-model enhancement)
# ============================================================================
YEAR_MIN, YEAR_MAX = 1985, 2023

# ============================================================================
#  Parameter tables (disconnected slicers). extra_measures: (name, dax, fmt, folder, desc)
# ============================================================================
PARAM_SPECS = [
    dict(name="Show chart as", col_type="string", col_fmt=None,
         values=["Tree", "Table", "Table no region"], default_dax='"Tree"', vfmt=None,
         desc="Disconnected slicer for Tableau parameter 'Show chart as:' ([Parameter 1], list, default 'Tree'). Drives which view (tree / table / table-no-region) the dashboard shows.",
         extra_measures=[
            ("DZV show tree map", "SELECTEDVALUE('Show chart as'[Show chart as], \"Tree\") = \"Tree\"", None, "View toggles",
             "Tableau [DZV: show tree map] = [Parameters].[Parameter 1]='Tree'. A view-mode boolean; use for visual show/hide (or replace with a bookmark)."),
            ("DZV table with region", "SELECTEDVALUE('Show chart as'[Show chart as], \"Tree\") = \"Table\"", None, "View toggles",
             "Tableau [DZV: table with region] = [Parameters].[Parameter 1]='Table'."),
            ("DZV table without region", "SELECTEDVALUE('Show chart as'[Show chart as], \"Tree\") = \"Table no region\"", None, "View toggles",
             "Tableau [DZV: table without region] = [Parameters].[Parameter 1]='Table no region'."),
         ]),
    dict(name="View data for", col_type="string", col_fmt=None,
         values=["World", "Region", "Country"], default_dax='"World"', vfmt=None,
         desc="Disconnected slicer for Tableau parameter 'View data for:' ([Parameter 2], list, default 'World'). Drives World/Region/Country granularity of the Pivoted views.",
         extra_measures=[
            ("DZV World", "SELECTEDVALUE('View data for'[View data for], \"World\") = \"World\"", None, "View toggles",
             "Tableau [DZV: World] = [Parameters].[Parameter 2]='World'."),
            ("DZV Region", "SELECTEDVALUE('View data for'[View data for], \"World\") = \"Region\"", None, "View toggles",
             "Tableau [DZV: Region] = [Parameters].[Parameter 2]='Region'."),
            ("DZV Country", "SELECTEDVALUE('View data for'[View data for], \"World\") = \"Country\"", None, "View toggles",
             "Tableau [DZV: Country] = [Parameters].[Parameter 2]='Country'."),
         ]),
    dict(name="Year Slider", col_type="int64", col_fmt="0",
         values=list(range(YEAR_MIN, YEAR_MAX + 1)), default_dax="2021", vfmt="0",
         desc="Disconnected slicer for Tableau parameter 'Slider' ([Parameter 3], real range, current 2021) - a year selector. Not referenced by any Tableau calc; provided as a report-facing year slider.",
         extra_measures=[]),
]

# ============================================================================
#  Relationships:  (name, fromTable, fromCol, toTable, toCol, fromCard, toCard, xfilter)
#  Default (None,None,None) = many:one single-direction (the 'to' side filters the 'from' side).
# ============================================================================
RELATIONSHIPS = [
    ("Year_PerCapitaFNR", "Per Capita FNR", "Year", "Year", "Year", None, None, None),
    ("Year_Pivoted", "Pivoted", "Year", "Year", "Year", None, None, None),
    ("Year_ElecGeneration", "Elec Generation", "Year", "Year", "Year", None, None, None),
    ("Year_BySource", "By Source", "Year", "Year", "Year", None, None, None),
    # The one genuine Tableau relationship (tree noodle): tree.csv[child] = 'Elec generation'[Entity].
    # child is non-unique (3320 rows) and Entity is non-unique (per year) => many:many.
    # oneDirection with to='Tree' => Tree[Child] filters Elec Generation[Entity]
    # (so 'Country trend'/'Advanced table', built on the tree source, filter consumption by child).
    ("Tree_ElecGeneration", "Elec Generation", "Entity", "Tree", "Child", "many", "many", "oneDirection"),
]

# ============================================================================
#  Emitters
# ============================================================================
def emit_column(tname, disp, src, dtype, hidden, summ, calc_dax=None, desc=None):
    L = []
    if desc:
        L.append(f"\t/// {one_line(desc)}")
    if calc_dax is not None:
        L.append(f"\tcolumn '{disp}' = {one_line(calc_dax)}")
    else:
        L.append(f"\tcolumn '{disp}'")
    L.append(f"\t\tdataType: {dtype}")
    if dtype == "dateTime":
        L.append("\t\tformatString: General Date")
    L.append(f"\t\tlineageTag: {lt(tname, 'col', disp)}")
    L.append(f"\t\tsummarizeBy: {summ}")
    if calc_dax is None:
        L.append(f"\t\tsourceColumn: {src}")
    if hidden:
        L.append("\t\tisHidden")
    L.append("")
    L.append("\t\tannotation SummarizationSetBy = Automatic")
    return "\n".join(L)

def emit_measure(tname, name, dax, fmt, folder, desc):
    L = []
    if desc:
        L.append(f"\t/// {one_line(desc)}")
    L.append(f"\tmeasure '{name}' = {one_line(dax)}")
    if fmt:
        L.append(f"\t\tformatString: {fmt}")
    L.append(f"\t\tlineageTag: {lt(tname, 'measure', name)}")
    if folder:
        L.append(f"\t\tdisplayFolder: {folder}")
    return "\n".join(L)

def emit_m_partition(tname, csv_name, cols):
    types = ", ".join("{\"%s\", %s}" % (esc_m(src), MTYPE[dtype]) for (_d, src, dtype, _h, _s) in cols)
    src = (
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        f"\t\t\t\t    Source = Csv.Document(File.Contents(DataFolder & \"{esc_m(csv_name)}\"), [Delimiter=\",\", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n"
        "\t\t\t\t    #\"Promoted Headers\" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n"
        f"\t\t\t\t    #\"Changed Type\" = Table.TransformColumnTypes(#\"Promoted Headers\", {{{types}}}, \"en-US\")\n"
        "\t\t\t\tin\n"
        "\t\t\t\t    #\"Changed Type\""
    )
    return f"\tpartition '{tname}' = m\n\t\tmode: import\n{src}"

def emit_table(spec):
    t = spec["name"]
    out = [f"/// {one_line(spec['desc'])}", f"table '{t}'", f"\tlineageTag: {lt(t, 'table')}", ""]
    for m in spec["measures"]:
        out.append(emit_measure(t, *m)); out.append("")
    for (disp, src, dtype, hidden, summ) in spec["cols"]:
        out.append(emit_column(t, disp, src, dtype, hidden, summ)); out.append("")
    for (nm, dax, dtype, hidden, summ, desc) in spec["calc_cols"]:
        out.append(emit_column(t, nm, None, dtype, hidden, summ, calc_dax=dax, desc=desc)); out.append("")
    out.append(emit_m_partition(t, spec["csv"], spec["cols"]))
    out.append("")
    out.append("\tannotation PBI_ResultType = Table")
    out.append("")
    return "\n".join(out)

def emit_year_table():
    t = "Year"
    out = [f"/// Conformed Year dimension ({YEAR_MIN}-{YEAR_MAX}), M-generated. Deliberate composite-model enhancement: the faithful translation of a Tableau 'blend on Year' - single-direction 1:many to each year-bearing fact so a report can slice all sources by one Year.",
           f"table '{t}'", f"\tlineageTag: {lt(t, 'table')}", ""]
    out.append(emit_column(t, "Year", "Year", "int64", False, "none"))
    out.append("")
    src = (
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        f"\t\t\t\t    Source = {{{YEAR_MIN}..{YEAR_MAX}}},\n"
        "\t\t\t\t    #\"Converted to Table\" = Table.FromList(Source, Splitter.SplitByNothing(), {\"Year\"}),\n"
        "\t\t\t\t    #\"Changed Type\" = Table.TransformColumnTypes(#\"Converted to Table\", {{\"Year\", Int64.Type}}, \"en-US\")\n"
        "\t\t\t\tin\n"
        "\t\t\t\t    #\"Changed Type\""
    )
    out.append(f"\tpartition '{t}' = m\n\t\tmode: import\n{src}")
    out.append("")
    out.append("\tannotation PBI_ResultType = Table")
    out.append("")
    return "\n".join(out)

def emit_param_table(p):
    t = p["name"]
    out = [f"/// {one_line(p['desc'])}", f"table '{t}'", f"\tlineageTag: {lt(t, 'table')}", ""]
    # value measure
    out.append(f"\t/// Current slicer selection on '{t}'[{t}], defaulting to Tableau's current value when nothing is selected.")
    out.append(f"\tmeasure '{t} Value' = SELECTEDVALUE('{t}'[{t}], {p['default_dax']})")
    if p["vfmt"]:
        out.append(f"\t\tformatString: {p['vfmt']}")
    out.append(f"\t\tlineageTag: {lt(t, 'measure', t + ' Value')}")
    out.append(f"\t\tdisplayFolder: Parameter")
    out.append("")
    for (nm, dax, fmt, folder, desc) in p.get("extra_measures", []):
        out.append(emit_measure(t, nm, dax, fmt, folder, desc)); out.append("")
    # column
    out.append(f"\tcolumn '{t}'")
    out.append(f"\t\tdataType: {p['col_type']}")
    if p["col_fmt"]:
        out.append(f"\t\tformatString: {p['col_fmt']}")
    out.append(f"\t\tlineageTag: {lt(t, 'col', t)}")
    out.append(f"\t\tsummarizeBy: none")
    out.append(f"\t\tsourceColumn: {t}")
    out.append("")
    out.append(f"\t\tannotation SummarizationSetBy = Automatic")
    out.append("")
    if p["col_type"] == "string":
        listexpr = "{" + ", ".join(f'"{esc_m(str(v))}"' for v in p["values"]) + "}"
        mtype = "type text"
    else:
        listexpr = "{" + ", ".join(str(v) for v in p["values"]) + "}"
        mtype = "Int64.Type" if p["col_type"] == "int64" else "type number"
    src = (
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        f"\t\t\t\t    Source = {listexpr},\n"
        f"\t\t\t\t    #\"Converted to Table\" = Table.FromList(Source, Splitter.SplitByNothing(), {{\"{t}\"}}),\n"
        f"\t\t\t\t    #\"Changed Type\" = Table.TransformColumnTypes(#\"Converted to Table\", {{{{\"{t}\", {mtype}}}}}, \"en-US\")\n"
        "\t\t\t\tin\n"
        "\t\t\t\t    #\"Changed Type\""
    )
    out.append(f"\tpartition '{t}' = m\n\t\tmode: import\n{src}")
    out.append("")
    out.append("\tannotation PBI_ResultType = Table")
    out.append("")
    return "\n".join(out)

def emit_relationships():
    out = []
    for (name, ft, fc, tt, tc, fcard, tcard, xf) in RELATIONSHIPS:
        out.append(f"relationship {name}")
        out.append(f"\tfromColumn: '{ft}'.'{fc}'")
        out.append(f"\ttoColumn: '{tt}'.'{tc}'")
        if fcard:
            out.append(f"\tfromCardinality: {fcard}")
        if tcard:
            out.append(f"\ttoCardinality: {tcard}")
        if xf:
            out.append(f"\tcrossFilteringBehavior: {xf}")
        out.append("")
    return "\n".join(out)

# ============================================================================
#  Write files
# ============================================================================
os.makedirs(TABLES, exist_ok=True)

with open(os.path.join(MODEL, ".platform"), "w", encoding="utf-8") as f:
    f.write(
        '{\n'
        '  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        '  "metadata": {\n'
        '    "type": "SemanticModel",\n'
        '    "displayName": "ElectricityPerCapita"\n'
        '  },\n'
        '  "config": {\n'
        '    "version": "2.0",\n'
        f'    "logicalId": "{lt("platform", "logicalId")}"\n'
        '  }\n'
        '}\n'
    )

with open(os.path.join(MODEL, "definition.pbism"), "w", encoding="utf-8") as f:
    f.write(
        '{\n'
        '  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n'
        '  "settings": {\n'
        '    "qnaEnabled": false\n'
        '  }\n'
        '}\n'
    )

with open(os.path.join(DEFN, "database.tmdl"), "w", encoding="utf-8") as f:
    f.write("database\n\tcompatibilityLevel: 1606\n")

with open(os.path.join(DEFN, "expressions.tmdl"), "w", encoding="utf-8") as f:
    f.write(f'expression DataFolder = "{esc_m(DATA_FOLDER)}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')
    f.write(f"\tlineageTag: {lt('expr', 'DataFolder')}\n\n\tannotation PBI_ResultType = Text\n")

all_tables = [s["name"] for s in TABLE_SPECS] + ["Year"] + [p["name"] for p in PARAM_SPECS]
qorder = '["' + '","'.join(all_tables + ["DataFolder"]) + '"]'
with open(os.path.join(DEFN, "model.tmdl"), "w", encoding="utf-8") as f:
    f.write("model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n")
    f.write("\tsourceQueryCulture: en-US\n\tdataAccessOptions\n\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n")
    f.write(f"annotation PBI_QueryOrder = {qorder}\n\n")
    f.write("annotation __PBI_TimeIntelligenceEnabled = 0\n\n")
    for t in all_tables:
        f.write(f"ref table '{t}'\n")
    f.write("\n")

with open(os.path.join(DEFN, "relationships.tmdl"), "w", encoding="utf-8") as f:
    f.write(emit_relationships())

for spec in TABLE_SPECS:
    with open(os.path.join(TABLES, spec["name"] + ".tmdl"), "w", encoding="utf-8") as f:
        f.write(emit_table(spec))
with open(os.path.join(TABLES, "Year.tmdl"), "w", encoding="utf-8") as f:
    f.write(emit_year_table())
for p in PARAM_SPECS:
    with open(os.path.join(TABLES, p["name"] + ".tmdl"), "w", encoding="utf-8") as f:
        f.write(emit_param_table(p))

# .pbip  (numeric $schema version). By repo convention the .pbip references the REPORT
# artifact (which in turn binds this SemanticModel). The report is pbi-report-builder's
# deliverable; we emit the conventional pairing pointing at the future
# ElectricityPerCapita.Report so the project opens as a pair once the report is authored.
with open(os.path.join(BASE, "ElectricityPerCapita.pbip"), "w", encoding="utf-8") as f:
    f.write(
        '{\n'
        '  "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",\n'
        '  "version": "1.0",\n'
        '  "artifacts": [\n'
        '    {\n'
        '      "report": {\n'
        '        "path": "ElectricityPerCapita.Report"\n'
        '      }\n'
        '    }\n'
        '  ],\n'
        '  "settings": {\n'
        '    "enableAutoRecovery": true\n'
        '  }\n'
        '}\n'
    )

# ---- console summary ----
n_cols = sum(len(s["cols"]) + len(s["calc_cols"]) for s in TABLE_SPECS) + 1 + len(PARAM_SPECS)
n_calc = sum(len(s["calc_cols"]) for s in TABLE_SPECS)
n_meas = sum(len(s["measures"]) for s in TABLE_SPECS) + sum(1 + len(p.get("extra_measures", [])) for p in PARAM_SPECS)
print("Wrote model to:", MODEL)
print("Data folder    :", DATA_FOLDER)
print(f"Tables         : {len(all_tables)}  ({', '.join(all_tables)})")
print(f"Columns        : {n_cols} (calc {n_calc})   Measures: {n_meas}   Relationships: {len(RELATIONSHIPS)}")
print("Files:")
for root, _dirs, files in os.walk(DEFN):
    for fn in sorted(files):
        print("   ", os.path.relpath(os.path.join(root, fn), MODEL))
