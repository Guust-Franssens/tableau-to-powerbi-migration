"""
TMDL generator for the 'Tale of 100 Entrepreneurs' Fabric semantic model.

Hand-authors the .SemanticModel/definition TMDL (no live Desktop/MCP available),
following the conventions proven by the sibling EEA/Superstore models and the
pbi-semantic-builder gotchas (single-line DAX, bare `database`, explicit M
cultures, name-vs-sourceColumn discipline, measure/column namespace safety).

Design:
  * 4 independent import fact tables (Tableau had 0 joins/blends) + 3 disconnected
    parameter tables. No relationships.
  * Columns are authored from the *physical* extract CSV headers (source of truth),
    display names taken from the Tableau caption where the spec declares the field.
  * The 9 Tableau table calculations become measures/calculated columns using
    verifiable CALCULATE/ALLEXCEPT/EARLIER patterns (numbers checked in Python).
"""
import os, uuid, csv

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))            # ...\fabric
MODEL = os.path.join(BASE, "TaleOf100Entrepreneurs.SemanticModel")
DEFN = os.path.join(MODEL, "definition")
TABLES = os.path.join(DEFN, "tables")
DATA_FOLDER = os.path.abspath(os.path.join(BASE, "..", "data")) + "\\"           # ...\data\
NS = uuid.UUID("6f1a9c00-1000-4000-9000-000000000000")

def lt(*parts):
    return str(uuid.uuid5(NS, "|".join(parts)))

def esc_m(s):      # escape a string literal inside an M expression
    return s.replace('"', '""')

# ---- M type per TMDL dataType ----
MTYPE = {"string": "type text", "int64": "Int64.Type", "double": "type number",
         "dateTime": "type datetime"}

# ============================================================================
#  Physical columns per table:  (display_name, source_csv_col, dataType, hidden, summarizeBy)
# ============================================================================
IPOS_COLS = [
    ("Company Name (Full)", "Company Name (Full)", "string", False, "none"),
    ("Company Name", "Company Name", "string", False, "none"),
    ("Net Income", "Net Income", "double", True, "sum"),
    ("Revenue", "Revenue", "double", True, "sum"),
    ("Revenue (Inflation Adjusted)", "Revenue_Inf_Adjusted", "double", False, "sum"),
    ("Segment", "Segment", "string", False, "none"),
    ("Ticker", "Ticker", "string", False, "none"),
    ("Year Founded", "Year Founded", "int64", False, "none"),
    ("Year Number", "Year Number", "int64", False, "none"),
    ("Years to $50m", "Years to $50m", "int64", False, "none"),
    ("Z_Complete_Sales", "Z_Complete_Sales", "string", False, "none"),
    ("Z_Typical", "Z_Typical", "string", False, "none"),
]
SEC_COLS = [
    ("Close", "Close", "double", False, "sum"),
    ("Company Name (Full)", "Company Name (Full)", "string", False, "none"),
    ("Date", "Date", "dateTime", False, "none"),
    ("Segment", "Segment", "string", False, "none"),
    ("Ticker", "Ticker", "string", False, "none"),
    ("Volume", "Volume", "int64", False, "sum"),
    ("Z_Not Exchange", "Z_Not Exchange", "string", True, "none"),
]
SP_COLS = [
    ("Date", "Date", "dateTime", False, "none"),
    ("Decade", "Decade", "int64", False, "none"),
    ("Metric", "Metric", "string", False, "none"),
    ("Value", "Value", "double", False, "sum"),
]
STOCK_COLS = [
    ("Adj Close", "Adj Close", "double", False, "sum"),
    ("Close", "Close", "double", True, "sum"),
    ("Company", "Company", "string", False, "none"),
    ("Date", "Date", "dateTime", False, "none"),
    ("High", "High", "double", True, "sum"),
    ("Low", "Low", "double", True, "sum"),
    ("Open", "Open", "double", True, "sum"),
    ("Primary Group", "Primary Group", "string", False, "none"),   # un-hidden (spec hid it): useful sector dim
    ("Symbol", "Symbol", "string", False, "none"),                 # un-hidden: the partition key for every table calc
    ("Volume", "Volume", "int64", True, "sum"),
]

# ============================================================================
#  Metric alias remaps (Tableau value-aliases on the 'Latest Tall SP Data' copies)
# ============================================================================
def switch_from_aliases(col_ref, aliases):
    pairs = ", ".join(f'"{esc_m(k)}", "{esc_m(v)}"' for k, v in aliases.items())
    return f"SWITCH({col_ref}, {pairs}, {col_ref})"

METRIC = "'Latest Tall SP Data'[Metric]"
ALIAS_METRIC3 = {"Inflation Rate Past 12 Months": "Inflation Rate", "Interest Rate": "Real interest rate",
    "Real Forward 10 Years Annualized Return": "10 Years", "Real Forward 12 Months Return": "1 Year",
    "Real Forward 3 Years Annualized Return": "3 Years",
    "Real Last 12 Months Return": "Real S&P Comp returns with dividends", "Yield": "Real S&P Comp yield"}
ALIAS_FINMETRIC = {"Inflation Rate Past 12 Months": "Inflation rate past 12 months", "Interest Rate": "Real interest rate",
    "Real Forward 10 Years Annualized Return": "Real forward 10 years annualized S&P Comp annualized return with dividend",
    "Real Forward 12 Months Return": "Real forward 12 months S&P Comp return with dividend",
    "Real Forward 3 Years Annualized Return": "Real forward 3 years annualized S&P Comp returns with dividend",
    "Real Last 12 Months Return": "Real past 12 months return", "Yield": "Real S&P Comp yield"}

S = "'Stocks (DJIA)'"   # shorthand for the stocks table ref used throughout the DAX

# ============================================================================
#  Table specs.  Each: name, description, csv, physical cols, calc cols, measures, partition kind
#  calc col: (name, dax, dataType, hidden, summarizeBy, desc)
#  measure : (name, dax, formatString|None, displayFolder|None, desc)
# ============================================================================
def stock_last_date(agg):   # per-symbol max/min date (LAST()/FIRST() over the Date partition)
    return f"CALCULATE({agg}({S}[Date]), ALLEXCEPT({S}, {S}[Symbol]))"

TABLE_SPECS = []

TABLE_SPECS.append(dict(
    name="Top 100 IPOs", csv="ds.top_100_ipos.csv", cols=IPOS_COLS,
    desc="IPO/company master facts (930 rows, one row per company x year). Source: Tableau extract 'Top 100 IPOs' ('Facts (Named Range)+ (Company Master.xlsx)'). Independent fact table (no Tableau joins/blends).",
    calc_cols=[
        ("Growth Group",
         'SWITCH(TRUE(), \'Top 100 IPOs\'[Years to $50m] <= 6, "Rocket Ship", \'Top 100 IPOs\'[Years to $50m] <= 12, "Hot Company", "Slow Burner")',
         "string", False, "none",
         "Tableau calc [Growth Group]: IF [Years to $50m]<=6 'Rocket Ship' ELSEIF <=12 'Hot Company' ELSE 'Slow Burner'. Used as color/row shelf in Top IPOs 1/2."),
        ("Years to Hit CoHort",
         'SWITCH(TRUE(), \'Top 100 IPOs\'[Years to $50m] < 5, "<5 Years", \'Top 100 IPOs\'[Years to $50m] < 10, "<10 Years", \'Top 100 IPOs\'[Years to $50m] < 20, ">10 Years", ">20 Years")',
         "string", False, "none",
         "Tableau calc [Years to Hit CoHort]: banded [Years to $50m]. Faithful SWITCH(TRUE()) translation."),
    ],
    measures=[
        ("Number of Records", "COUNTROWS('Top 100 IPOs')", "#,##0", None,
         "Tableau [Number of Records] (=1 per row) -> DAX row count."),
        ("Calculation2",
         "IF(SUM('Top 100 IPOs'[Revenue (Inflation Adjusted)]) <= [Dollar Amount Value] * 100000, TRUE, FALSE)",
         None, "Tableau Helpers",
         "Tableau measure [Calculation2]: sum([Revenue_Inf_Adjusted]) <= [Parameters].[Parameter 1]*100000 (boolean gate)."),
        ("Calculation1",
         'IF([Dollar Amount Value] * 100000 >= SUM(\'Top 100 IPOs\'[Revenue (Inflation Adjusted)]), "keep", "hide")',
         None, "Tableau Helpers",
         "Tableau measure [Calculation1]: IF last()=0 AND [Parameter 1]*100000>=SUM([Revenue_Inf_Adjusted]) 'keep' ELSE 'hide'. The last()=0 table-calc gate (evaluate only on the partition's last row) is a Tableau filter-helper idiom superseded by native PBI filtering and is dropped; the threshold logic is preserved. See limitations."),
        ("Calculation3",
         'IF(CALCULATE(SUM(\'Top 100 IPOs\'[Revenue (Inflation Adjusted)]), ALL(\'Top 100 IPOs\')) >= [Dollar Amount Value] * 100000, MAX(\'Top 100 IPOs\'[Company Name (Full)]), "No")',
         None, "Tableau Helpers",
         "TABLE CALC [Calculation3]: IF total(SUM([Revenue_Inf_Adjusted]))>=[Parameter 1]*100000 THEN max([Company Name (Full)]) ELSE 'No'. TOTAL() -> ALL() grand total. Partition INFERRED (field is orphaned). Ground truth: grand total=31,790.76 < 10,000,000 -> 'No' at default param=100."),
    ],
))

TABLE_SPECS.append(dict(
    name="Securities", csv="ds.securities.csv", cols=SEC_COLS,
    desc="Daily securities prices (17,314 rows). Source: Tableau extract 'Securities'. Independent fact table (no Tableau joins/blends).",
    calc_cols=[],
    measures=[
        ("Number of Records", "COUNTROWS('Securities')", "#,##0", None,
         "Tableau [Number of Records] (=1 per row) -> DAX row count."),
    ],
))

TABLE_SPECS.append(dict(
    name="Latest Tall SP Data", csv="ds.latest_tall_sp_data.csv", cols=SP_COLS,
    desc="Tall S&P macro time-series (1,296 rows: Date x Metric x Value). Source: Tableau extract 'Latest Tall SP Data'. Independent fact table.",
    calc_cols=[
        ("Financial metric", switch_from_aliases(METRIC, ALIAS_FINMETRIC), "string", False, "none",
         "Tableau calc [Financial metric] = [Metric], with the field's Tableau display-aliases materialized as a SWITCH value remap."),
        ("Financial metric 2", METRIC, "string", False, "none",
         "Tableau calc [Financial metric 2] = [Metric] (no aliases -> passthrough copy)."),
        ("Metric3", switch_from_aliases(METRIC, ALIAS_METRIC3), "string", False, "none",
         "Tableau calc [Metric3] = [Metric], with its (short-label) Tableau display-aliases materialized as a SWITCH value remap."),
        ("YrStr", 'FORMAT(YEAR(\'Latest Tall SP Data\'[Date]), "0")', "string", False, "none",
         "Tableau calc [YrStr] = STR(YEAR([Date]))."),
    ],
    measures=[
        ("Number of Records", "COUNTROWS('Latest Tall SP Data')", "#,##0", None,
         "Tableau [Number of Records] (=1 per row) -> DAX row count."),
    ],
))

TABLE_SPECS.append(dict(
    name="Stocks (DJIA)", csv="ds.stocks_in_dow_jones_industrial_average_csv_stocks_in_dow_jones_industrial_average_csv.csv",
    cols=STOCK_COLS,
    desc="Daily OHLC + Adj Close for 31 Dow Jones stocks (292,280 rows; exactly one row per Symbol x Date; Adj Close has no nulls). Source: Tableau extract 'stocks in dow jones industrial average.csv'. Independent fact table. Home of 8 of the 9 Tableau table calculations (per-Symbol time-series 'growth of $X' idioms); their PARTITION BY Symbol / ORDER BY Date grain is INFERRED because no worksheet binds them.",
    calc_cols=[
        ("Number of Records", "1", "int64", True, "none",
         "Tableau [Number of Records] (=1). Kept as a hidden column so [Original Investment Amt] can faithfully do DISTINCTCOUNT of it (=1)."),
        ("Adj Close First", f"CALCULATE(AVERAGE({S}[Adj Close]), ALLEXCEPT({S}, {S}[Symbol]), {S}[Date] = CALCULATE(MIN({S}[Date]), ALLEXCEPT({S}, {S}[Symbol])))",
         "double", True, "none",
         "Helper: LOOKUP(ZN(AVG([Adj Close])), FIRST()) = the Adj Close at each symbol's earliest date (per-symbol constant). UTX -> 0.33."),
        ("Adj Close Last", f"CALCULATE(AVERAGE({S}[Adj Close]), ALLEXCEPT({S}, {S}[Symbol]), {S}[Date] = CALCULATE(MAX({S}[Date]), ALLEXCEPT({S}, {S}[Symbol])))",
         "double", True, "none",
         "Helper: LOOKUP(ZN(AVG([Adj Close])), LAST()) = the Adj Close at each symbol's latest date (per-symbol constant). UTX -> 70.51."),
        ("Index", f"CALCULATE(COUNTROWS({S}), FILTER(ALLEXCEPT({S}, {S}[Symbol]), {S}[Date] <= EARLIER({S}[Date])))",
         "int64", False, "none",
         "TABLE CALC [Index] = INDEX(): 1-based position ordered by Date within Symbol. UTX first date -> 1, last -> 10254."),
        ("Controllable Dates", f"DATE(YEAR({S}[Date]), 1, 1)", "dateTime", False, "none",
         "Tableau calc [Controllable Dates] = DATETRUNC('year',[Date])."),
        ("Week", f"{S}[Date] - (WEEKDAY({S}[Date], 1) - 1)", "dateTime", False, "none",
         "Tableau calc [Week] = DATE(DATETRUNC('week',[Date])) (Sunday-start week)."),
    ],
    measures=[
        ("Original Investment Amt", "DIVIDE([Investment Amount Value], DISTINCTCOUNT('Stocks (DJIA)'[Number of Records]))",
         "\\$#,##0.00;-\\$#,##0.00;\\$#,##0.00", "Investment",
         "Tableau measure [Original Investment Amt] = [Investment Amount]/CountD([Number of Records]). CountD([NoR]) is degenerate (=1), so this = the Investment Amount parameter (500 default)."),
        ("Original Share Amount", "DIVIDE([Original Investment Amt], AVERAGE('Stocks (DJIA)'[Adj Close First]))",
         "#,##0.000000", "Investment",
         "TABLE CALC [Original Share Amount] = ([Investment Amount]/CountD([NoR]))/LOOKUP(avg([Adj Close]),First()) = shares bought at the first price. UTX -> 1515.151515."),
        ("Avg Adj Close Value", "AVERAGE('Stocks (DJIA)'[Adj Close]) * DIVIDE([Investment Amount Value], AVERAGE('Stocks (DJIA)'[Adj Close First]))",
         "\\$#,##0.00;-\\$#,##0.00;\\$#,##0.00", "Investment",
         "TABLE CALC [Avg. Adj Close] (internal '[Difference in Avg. Adj Close]') = ZN(AVG([Adj Close]))*([Investment Amount]/LOOKUP(ZN(AVG([Adj Close])),FIRST())) = value over time of the initial investment. UTX last date -> 106,833.33; first date -> 500."),
        ("Very Last Value", "[Original Investment Amt] * DIVIDE(AVERAGE('Stocks (DJIA)'[Adj Close Last]), AVERAGE('Stocks (DJIA)'[Adj Close First]))",
         "\\$#,##0.00;-\\$#,##0.00;\\$#,##0.00", "Investment",
         "TABLE CALC [Very Last Value] = [Orig Inv Amt]*LOOKUP(...,LAST())/LOOKUP(...,FIRST()) = final value of the investment (per-symbol constant). UTX -> 106,833.33; AA -> 7,934.78."),
        ("Gains Loss", "[Avg Adj Close Value] - [Original Investment Amt]",
         "\\$#,##0.00;-\\$#,##0.00;\\$#,##0.00", "Investment",
         "Tableau measure [Gains/(Loss)] = [Avg. Adj Close] - [Original Investment Amt]. UTX last date -> 106,333.33."),
        ("Pct", "DIVIDE([Gains Loss], [Original Investment Amt])", "0.00%", "Investment",
         "Tableau measure [%] = [Gains/(Loss)]/[Original Investment Amt]. UTX last date -> 21266.67% (i.e. 212.6667x)."),
        ("Last Value", f"IF(MAX({S}[Date]) = {stock_last_date('MAX')}, [Avg Adj Close Value])",
         "\\$#,##0.00;-\\$#,##0.00;\\$#,##0.00", "Investment",
         "TABLE CALC [Last Value] = IF Min([Date])=Lookup(Min([Date]),LAST()) THEN [Avg. Adj Close] END = show the growth value only on the symbol's last date. UTX -> 106,833.33 on 2010-08-17, blank elsewhere."),
        ("First Last Value", f"IF(MAX({S}[Date]) = {stock_last_date('MAX')} || MAX({S}[Date]) = {stock_last_date('MIN')}, [Avg Adj Close Value])",
         "\\$#,##0.00;-\\$#,##0.00;\\$#,##0.00", "Investment",
         "TABLE CALC [First/Last Value] = IF MAX([Date])=Lookup(MAX,LAST) OR MAX([Date])=Lookup(MAX,FIRST) THEN [Avg. Adj Close] END = show the value on the first AND last date. UTX -> 500 on first date, 106,833.33 on last."),
        ("Lookup Min Date Last", stock_last_date('MAX'), "General Date", "Dates",
         "TABLE CALC [LOOKUP(ZN(AVG([Adj Close])), FIRST())] = LOOKUP(MIN([Date]), Last()) = the last date of the symbol's partition. UTX -> 2010-08-17."),
        ("Window Min Last", stock_last_date('MAX'), "General Date", "Dates",
         "TABLE CALC [LOOKUP(...FIRST()) (copy)] = WINDOW_MIN(MIN([Date]),Last(),Last()) = min date over the single last mark = the last date. UTX -> 2010-08-17."),
        ("Bars", 'SWITCH([Date Detail Value], "year", 365, "quarter", 91, "month", 30, "week", 7, "day", 1)',
         "#,##0", "Granularity",
         "Tableau calc [Bars] = param [Date Detail] -> 365/91/30/7/1. Param-driven scalar."),
        ("Calendar", f'SWITCH([Date Detail Value], "year", DATE(YEAR(MAX({S}[Date])), 1, 1), "quarter", DATE(YEAR(MAX({S}[Date])), (QUARTER(MAX({S}[Date])) - 1) * 3 + 1, 1), "month", DATE(YEAR(MAX({S}[Date])), MONTH(MAX({S}[Date])), 1), "week", MAX({S}[Date]) - (WEEKDAY(MAX({S}[Date]), 1) - 1), "day", MAX({S}[Date]))',
         "General Date", "Granularity",
         "Tableau calc [Calendar] = param [Date Detail]-driven DATETRUNC of [Date]. Implemented as a measure; a dynamic date-granularity *axis* is better done as a Date Granularity Field Parameter in the report - see limitations."),
    ],
))

# ============================================================================
#  Parameter tables (disconnected, M-sourced so column names are explicit -> no Value gotcha)
# ============================================================================
PARAM_SPECS = [
    dict(name="Investment Amount", col_type="double", col_fmt="\\$#,##0.00;-\\$#,##0.00;\\$#,##0.00",
         values=[0, 100, 250, 500, 1000, 2500, 5000, 10000], mval="500",
         default_dax="500", vfmt="\\$#,##0.00;-\\$#,##0.00;\\$#,##0.00",
         desc="Disconnected slicer table for the Tableau 'Investment Amount' parameter (real, range domain, default 500). Feeds the Stocks 'growth of $X' measures. Tableau range params have no fixed list; approximated as a discrete choice list. The value measure defaults to Tableau's current 500 when nothing is selected."),
    dict(name="Dollar Amount", col_type="double", col_fmt="\\$#,##0.00;-\\$#,##0.00;\\$#,##0.00",
         values=[0, 25, 50, 100, 250, 500, 1000], mval="100",
         default_dax="100", vfmt="\\$#,##0.00;-\\$#,##0.00;\\$#,##0.00",
         desc="Disconnected slicer table for the Tableau 'Dollar amount' parameter (internal '[Parameter 1]', real, range, default 100). This is the parameter actually placed on the dashboard; feeds Top 100 IPOs Calculation1/2/3. Value measure defaults to 100."),
    dict(name="Date Detail", col_type="string", col_fmt=None,
         values=["year", "quarter", "month", "week", "day"], mval='"month"',
         default_dax='"month"', vfmt=None,
         desc="Disconnected slicer table for the Tableau 'Date Detail' parameter (string, list domain year/quarter/month/week/day, default 'month'). Feeds the Stocks [Bars]/[Calendar] granularity helpers. Value measure defaults to 'month'."),
]

# ============================================================================
#  Emitters
# ============================================================================
def emit_column(tname, disp, src, dtype, hidden, summ, calc_dax=None, desc=None):
    L = []
    if desc:
        L.append(f"\t/// {desc}")
    if calc_dax is not None:
        L.append(f"\tcolumn '{disp}' = {calc_dax}")
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
        L.append(f"\t/// {desc}")
    L.append(f"\tmeasure '{name}' = {dax}")
    if fmt:
        L.append(f"\t\tformatString: {fmt}")
    L.append(f"\t\tlineageTag: {lt(tname, 'measure', name)}")
    if folder:
        L.append(f"\t\tdisplayFolder: {folder}")
    return "\n".join(L)

def emit_m_partition(tname, csv_name, cols):
    types = ", ".join("{\"%s\", %s}" % (src, MTYPE[dtype]) for (_d, src, dtype, _h, _s) in cols)
    src = (
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        f"\t\t\t\t    Source = Csv.Document(File.Contents(DataFolder & \"{csv_name}\"), [Delimiter=\",\", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n"
        "\t\t\t\t    #\"Promoted Headers\" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n"
        f"\t\t\t\t    #\"Changed Type\" = Table.TransformColumnTypes(#\"Promoted Headers\", {{{types}}}, \"en-US\")\n"
        "\t\t\t\tin\n"
        "\t\t\t\t    #\"Changed Type\""
    )
    return f"\tpartition '{tname}' = m\n\t\tmode: import\n{src}"

def emit_table(spec):
    t = spec["name"]
    out = [f"/// {spec['desc']}", f"table '{t}'", f"\tlineageTag: {lt(t, 'table')}", ""]
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

def emit_param_table(p):
    t = p["name"]
    out = [f"/// {p['desc']}", f"table '{t}'", f"\tlineageTag: {lt(t, 'table')}", ""]
    # value measure
    out.append(f"\t/// Reads the current slicer selection on '{t}'[{t}], defaulting to Tableau's current value when nothing is selected.")
    out.append(f"\tmeasure '{t} Value' = SELECTEDVALUE('{t}'[{t}], {p['default_dax']})")
    if p["vfmt"]:
        out.append(f"\t\tformatString: {p['vfmt']}")
    out.append(f"\t\tlineageTag: {lt(t, 'measure', t + ' Value')}")
    out.append(f"\t\tdisplayFolder: Parameter")
    out.append("")
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
    # M partition (explicit column name -> no Value gotcha)
    if p["col_type"] == "string":
        listexpr = "{" + ", ".join(f'"{esc_m(str(v))}"' for v in p["values"]) + "}"
        mtype = "type text"
    else:
        listexpr = "{" + ", ".join(str(v) for v in p["values"]) + "}"
        mtype = "type number"
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

# ============================================================================
#  Write files
# ============================================================================
os.makedirs(TABLES, exist_ok=True)

# .platform  (git-integration metadata; deterministic logicalId)
with open(os.path.join(MODEL, ".platform"), "w", encoding="utf-8") as f:
    f.write(
        '{\n'
        '  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        '  "metadata": {\n'
        '    "type": "SemanticModel",\n'
        '    "displayName": "TaleOf100Entrepreneurs"\n'
        '  },\n'
        '  "config": {\n'
        '    "version": "2.0",\n'
        f'    "logicalId": "{lt("platform", "logicalId")}"\n'
        '  }\n'
        '}\n'
    )

# definition.pbism
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

# database.tmdl  (bare 'database', tab-indented compatibilityLevel)
with open(os.path.join(DEFN, "database.tmdl"), "w", encoding="utf-8") as f:
    f.write("database\n\tcompatibilityLevel: 1606\n")

# expressions.tmdl  (DataFolder parameter -> real absolute path)
with open(os.path.join(DEFN, "expressions.tmdl"), "w", encoding="utf-8") as f:
    f.write(f'expression DataFolder = "{DATA_FOLDER}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')
    f.write(f"\tlineageTag: {lt('expr', 'DataFolder')}\n\n\tannotation PBI_ResultType = Text\n")

# model.tmdl
all_tables = [s["name"] for s in TABLE_SPECS] + [p["name"] for p in PARAM_SPECS]
qorder = '["' + '","'.join(all_tables + ["DataFolder"]) + '"]'
with open(os.path.join(DEFN, "model.tmdl"), "w", encoding="utf-8") as f:
    f.write("model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n")
    f.write("\tsourceQueryCulture: en-US\n\tdataAccessOptions\n\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n")
    f.write(f"annotation PBI_QueryOrder = {qorder}\n\n")
    f.write("annotation __PBI_TimeIntelligenceEnabled = 0\n\n")
    for t in all_tables:
        f.write(f"ref table '{t}'\n")
    f.write("\n")

# table files
for spec in TABLE_SPECS:
    with open(os.path.join(TABLES, spec["name"] + ".tmdl"), "w", encoding="utf-8") as f:
        f.write(emit_table(spec))
for p in PARAM_SPECS:
    with open(os.path.join(TABLES, p["name"] + ".tmdl"), "w", encoding="utf-8") as f:
        f.write(emit_param_table(p))

print("Wrote model to:", MODEL)
print("Data folder    :", DATA_FOLDER)
print("Tables         :", ", ".join(all_tables))
print("Files:")
for root, _dirs, files in os.walk(DEFN):
    for fn in sorted(files):
        print("   ", os.path.relpath(os.path.join(root, fn), MODEL))
