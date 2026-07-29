# -*- coding: utf-8 -*-
"""Deterministic TMDL generator for the Airline Alliance Activity semantic model.
Emits the full AirlineAllianceActivity.SemanticModel definition from migration-spec.json + CSV header."""

import json, csv, os, io
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
SPEC = os.path.join(ROOT, "migration-spec.json")
CSV = os.path.join(ROOT, "data", "ds.airline_alliance_performance_2022_2025_1.csv")
DATADIR = os.path.join(ROOT, "data") + "\\"
BASE = os.path.join(ROOT, "fabric", "AirlineAllianceActivity.SemanticModel")
DEF = os.path.join(BASE, "definition")
TBL = os.path.join(DEF, "tables")
FACT = "Flight Activity"
CSVFILE = "ds.airline_alliance_performance_2022_2025_1.csv"

T = "\t"


def w(path, text):
    # normalise to CRLF to match the Superstore template files, tabs preserved
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print("wrote", os.path.relpath(path, ROOT))


# ---------------------------------------------------------------- load spec + csv header
spec = json.load(io.open(SPEC, encoding="utf-8"))
by_source = {}
for d in spec["data_sources"]:
    for fld in d["fields"]:
        hdr = (fld.get("internal_name") or "").strip("[]")
        if fld["kind"] == "column" and fld["data_type"] != "table":
            by_source.setdefault(hdr, fld)
with io.open(CSV, encoding="utf-8-sig", newline="") as f:
    header = next(csv.reader(f))
assert len(header) == 70, len(header)

DTYPE = {
    "integer": ("int64", "Int64.Type"),
    "real": ("double", "type number"),
    "string": ("string", "type text"),
    "date": ("dateTime", "type date"),
}

cols = []  # (caption, source, tmdl_dtype, m_type, data_type)
for h in header:
    fld = by_source[h]
    td, mt = DTYPE[fld["data_type"]]
    cols.append((fld["caption"], h, td, mt, fld["data_type"]))

# ================================================================ MEASURE DEFINITIONS
REGION = (
    "FILTER(ALL('{F}'[Origin Region]),[Region Parameter Value]=\"Global\"||'{F}'[Origin Region]=[Region Parameter Value])"
).format(F=FACT)
REGION_DEST = (
    "FILTER(ALL('{F}'[Destination Region]),[Region Parameter Value]=\"Global\"||'{F}'[Destination Region]=[Region Parameter Value])"
).format(F=FACT)


def col(c):
    return "'%s'[%s]" % (FACT, c)


PERIOD = {
    "CY": [col("Year") + "=[Year Parameter Value]"],
    "PY": [col("Year") + "=[Year Parameter Value]-1"],
    "CM": [col("Year") + "=[Year Parameter Value]", col("Month") + "=[Month Parameter Value]"],
    "PM": [col("Year") + "=[PM Year Value]", col("Month") + "=[PM Month Value]"],
}
EXTRA = {
    "completed": col("Completed Flights") + "=1",
    "ontime": col("On Time Performance") + "=1",
    "mishandled": col("Mishandled Baggage") + ">=1",
    "airline": col("Airline Name") + "=[Airline Parameter Value]",
}


# base metric registry: name -> (agg_core, extra_list, airline?, formatString)
def AGG(kind, c=None):
    if kind == "distinct":
        return "DISTINCTCOUNT('%s'[Flight Id])" % FACT
    if kind == "sum":
        return "SUM(%s)" % col(c)
    if kind == "avg":
        return "AVERAGE(%s)" % col(c)
    if kind == "sumx_pd":
        return "SUMX('%s',%s*%s)" % (FACT, col("Passengers Carried"), col("Distance Km"))
    if kind == "sumx_cd":
        return "SUMX('%s',%s*%s)/1000" % (FACT, col("Cargo Carried Kg"), col("Distance Km"))
    if kind == "sum1000":
        return "SUM(%s)/1000" % col(c)
    raise ValueError(kind)


CUR = "\\$#,##0"
INT = "#,##0"
PCT = "0.0%"
DEC2 = "0.00"
REG = {
    "All Flights": (AGG("distinct"), [], False, INT),
    "Count On-Time Perf": (AGG("distinct"), ["ontime"], False, INT),
    "No of Comp Flights": (AGG("distinct"), ["completed"], False, INT),
    "Avg. CSAT": (AGG("avg", "Customer Satisfaction Score"), [], False, DEC2),
    "RPK": (AGG("sumx_pd"), ["completed"], False, INT),
    "No of Mishandled Baggage": (AGG("distinct"), ["completed", "mishandled"], False, INT),
    "No of Passengers": (AGG("sum", "Passengers Carried"), ["completed"], False, INT),
    "Passenger Count": (AGG("sum", "Passengers Carried"), ["completed"], True, INT),
    "Total Revenue": (AGG("sum", "Total Revenue Usd"), ["completed"], True, CUR),
    "Op Profit": (AGG("sum", "Operating Profit Usd"), ["completed"], True, CUR),
    "Costs": (AGG("sum", "Total Costs Usd"), ["completed"], True, CUR),
    "Load Factor": (AGG("avg", "Load Factor"), ["completed"], True, PCT),
    "Cargo Load Factor": (AGG("avg", "Cargo Load Factor"), ["completed"], True, PCT),
    "CTK": (AGG("sumx_cd"), ["completed"], True, INT),
    "Freight Tonnes": (AGG("sum1000", "Cargo Carried Kg"), ["completed"], True, INT),
    "RPK (A)": (AGG("sumx_pd"), ["completed"], True, INT),
}
FOLDER = {"CY": "Current Year (CY)", "PY": "Prior Year (PY)", "CM": "Current Month (CM)", "PM": "Prior Month (PM)"}

# which (period, metric) base measures are actually referenced (exact Tableau caption set)
BASE_SET = {
    "CY": [
        "All Flights",
        "Avg. CSAT",
        "CTK",
        "Cargo Load Factor",
        "Costs",
        "Count On-Time Perf",
        "Freight Tonnes",
        "Load Factor",
        "No of Comp Flights",
        "Op Profit",
        "Passenger Count",
        "RPK",
        "RPK (A)",
        "Total Revenue",
    ],
    "PY": [
        "Avg. CSAT",
        "CTK",
        "Cargo Load Factor",
        "Count On-Time Perf",
        "Freight Tonnes",
        "Load Factor",
        "No of Comp Flights",
        "Op Profit",
        "Passenger Count",
        "RPK",
        "RPK (A)",
        "Total Revenue",
    ],
    "CM": [
        "All Flights",
        "Avg. CSAT",
        "CTK",
        "Cargo Load Factor",
        "Costs",
        "Count On-Time Perf",
        "Freight Tonnes",
        "Load Factor",
        "No of Comp Flights",
        "No of Mishandled Baggage",
        "Op Profit",
        "Passenger Count",
        "RPK",
        "RPK (A)",
        "Total Revenue",
    ],
    "PM": [
        "All Flights",
        "Avg. CSAT",
        "CTK",
        "Cargo Load Factor",
        "Costs",
        "Count On-Time Perf",
        "Freight Tonnes",
        "Load Factor",
        "No of Comp Flights",
        "No of Mishandled Baggage",
        "No of Passengers",
        "Op Profit",
        "Passenger Count",
        "RPK",
        "RPK (A)",
        "Total Revenue",
    ],
}

measures = []  # (name, dax, formatString|None, displayFolder|None, isHidden, desc)


def add_m(name, dax, fmt, folder, hidden, desc):
    measures.append((name, dax, fmt, folder, hidden, desc))


def base_dax(period, metric):
    core, extra, airline, fmt = REG[metric]
    preds = list(PERIOD[period])
    for e in extra:
        preds.append(EXTRA[e])
    if airline:
        preds.append(EXTRA["airline"])
    preds.append(REGION)
    return "CALCULATE(%s,%s)" % (core, ",".join(preds)), fmt


for period in ["CY", "PY", "CM", "PM"]:
    for metric in BASE_SET[period]:
        nm = "%s %s" % (period, metric)
        dax, fmt = base_dax(period, metric)
        core, extra, airline, _ = REG[metric]
        scope = "airline = [Airline Parameter Value]" if airline else "all airlines (region-wide)"
        add_m(
            nm,
            dax,
            fmt,
            FOLDER[period],
            False,
            "Tableau '%s' measure. %s over %s period, %s, region-restricted via the Origin Region parameter (Global = show all)."
            % (nm, metric, period, scope),
        )

# ---- ratio measures
add_m(
    "CY On-Time Perf%",
    "DIVIDE([CY Count On-Time Perf],[CY All Flights])",
    PCT,
    "Ratios",
    False,
    "Tableau 'CY On-Time Perf%': CY on-time flights / CY all flights.",
)
add_m(
    "CM On-Time Perf%",
    "DIVIDE([CM Count On-Time Perf],[CM All Flights])",
    PCT,
    "Ratios",
    False,
    "Tableau 'CM On-Time Perf%': CM on-time flights / CM all flights.",
)
add_m(
    "PM On-Time Perf %",
    "DIVIDE([PM Count On-Time Perf],[PM All Flights])",
    PCT,
    "Ratios",
    False,
    "Tableau 'PM On-Time Perf %': PM on-time flights / PM all flights.",
)
add_m(
    "CM % of Mishandled Baggage",
    "DIVIDE([CM No of Mishandled Baggage],[CM No of Comp Flights])",
    PCT,
    "Ratios",
    False,
    "Tableau 'CM % of Mishandled Baggage': CM mishandled-baggage flights / CM completed flights.",
)
add_m(
    "PM % of Mishandled Baggage",
    "DIVIDE([PM No of Mishandled Baggage],[CM No of Comp Flights])",
    PCT,
    "Ratios",
    False,
    "Tableau 'PM % of Mishandled Baggage'. NOTE: denominator is CM (not PM) No of Comp Flights - faithful reproduction of a source-workbook quirk (see limitations_encountered).",
)

# ---- Circle Col (CY vs PY) with the two source quirks
CIRCLE = [  # (measure name, CY base, PY base)
    ("Passenger Count Circle Col", "CY Passenger Count", "PY Passenger Count"),
    ("Total Revenue Circle Col", "CY Total Revenue", "PY Total Revenue"),
    ("Op Profit Circle Col", "CY Op Profit", "PY Op Profit"),
    ("Costs Circle Col", "CY Op Profit", "PY Op Profit"),  # QUIRK: compares Op Profit, not Costs
    ("Load Factor Circle Col", "CY Load Factor", "PY Load Factor"),
    ("Cargo Load Factor Circle Col", "CY Cargo Load Factor", "PY Cargo Load Factor"),
    ("CTK Circle Col", "CY CTK", "PY CTK"),
    ("Freight Tonnes Circle Col", "CY Freight Tonnes", "PY Freight Tonnes"),
    ("RPK Perf Circle Col", "CY RPK", "PY RPK"),
    ("RPK Perf Circle Col (A)", "CY RPK (A)", "PY RPK (A)"),
    ("Comp Flights Circle Col", "CY No of Comp Flights", "PY No of Comp Flights"),
    ("On-Time Perf Circle Col", "CY Count On-Time Perf", "PY Count On-Time Perf"),  # raw count, not %
    ("CSAT Perf Circle Col", "CY Avg. CSAT", "PY Avg. CSAT"),
]
for nm, cy, py in CIRCLE:
    dax = 'IF([%s]>[%s],"Up",IF([%s]<[%s],"Down","No"))' % (cy, py, cy, py)
    note = ""
    if nm == "Costs Circle Col":
        note = " NOTE: source formula compares CY/PY Op Profit (not Costs) - faithful reproduction of a source quirk."
    if nm == "On-Time Perf Circle Col":
        note = " Compares raw CY/PY on-time flight COUNT (not the %)."
    add_m(
        nm,
        dax,
        None,
        "Trend Indicators (YoY)",
        False,
        "Tableau '%s': YoY direction (Up/Down/No) of %s vs %s.%s" % (nm, cy, py, note),
    )

# ---- Pos MoM (CM vs PM), blank when not growing
MOM = [
    ("Pos MoM Passenger Count", "CM Passenger Count", "PM Passenger Count"),
    ("Pos MoM Total Revenue", "CM Total Revenue", "PM Total Revenue"),
    ("Pos MoM Op Profit", "CM Op Profit", "PM Op Profit"),
    ("Pos MoM Costs", "CM Costs", "PM Costs"),
    ("Pos MoM Load Factor", "CM Load Factor", "PM Load Factor"),
    ("Pos MoM Cargo Load Factor", "CM Cargo Load Factor", "PM Cargo Load Factor"),
    ("Pos MoM CTK", "CM CTK", "PM CTK"),
    ("Pos MoM Freight Tonnes", "CM Freight Tonnes", "PM Freight Tonnes"),
    ("Pos MoM RPK", "CM RPK", "PM RPK"),
    ("Pos MoM RPK (A)", "CM RPK (A)", "PM RPK (A)"),
    ("Pos MoM Comp Flights", "CM No of Comp Flights", "PM No of Comp Flights"),
    ("Pos MoM On-Time Perf", "CM Count On-Time Perf", "PM Count On-Time Perf"),
    ("Pos MoM Avg. CSAT", "CM Avg. CSAT", "PM Avg. CSAT"),
]
for nm, cm, pm in MOM:
    dax = "IF([%s]>[%s],DIVIDE([%s],[%s])-1)" % (cm, pm, cm, pm)
    add_m(
        nm,
        dax,
        PCT,
        "MoM Growth (Positive)",
        False,
        "Tableau '%s': month-over-month growth (%s/%s - 1); BLANK when not growing." % (nm, cm, pm),
    )

# ---- period helper measures (on fact)
add_m(
    "PM Month Value",
    "IF([Month Parameter Value]=1,12,[Month Parameter Value]-1)",
    INT,
    "Parameters",
    True,
    "Prior-month month number derived from the Month parameter (wraps 1->12).",
)
add_m(
    "PM Year Value",
    "IF([Month Parameter Value]=1,[Year Parameter Value]-1,[Year Parameter Value])",
    INT,
    "Parameters",
    True,
    "Prior-month year derived from the Year/Month parameters (rolls back at January).",
)
add_m(
    "Date From Params Value",
    "DATE([Year Parameter Value],[Month Parameter Value],1)",
    "Short Date",
    "Parameters",
    True,
    "Tableau 'Date From Params': MAKEDATE(Year Parameter, Month Parameter, 1) - first day of the selected month.",
)

# ---- Selected echoes (param values surfaced under their Tableau captions)
add_m(
    "Selected Region",
    "[Region Parameter Value]",
    None,
    "Parameters",
    False,
    "Tableau 'Selected Region' = current Origin Region parameter value.",
)
add_m(
    "Selected Airline",
    "[Airline Parameter Value]",
    None,
    "Parameters",
    False,
    "Tableau 'Selected Airline' = current Airline Name parameter value.",
)
add_m(
    "Selected Aircraft Type",
    "[Aircraft Parameter Value]",
    None,
    "Parameters",
    False,
    "Tableau 'Selected Aircraft Type' = current Aircraft Type parameter value.",
)

# ---- colour / layout helper measures
add_m(
    "Colour",
    'IF([Month Parameter Value]=SELECTEDVALUE(\'Date\'[Month Number]),"Month","Others")',
    None,
    "Report Helpers",
    False,
    "Tableau 'Colour': highlights the month matching the Month parameter (assumes visual grouped by Date[Month Number]).",
)
add_m(
    "Year Colour",
    'IF([Year Parameter Value]=SELECTEDVALUE(\'Date\'[Year]),"Year","Others")',
    None,
    "Report Helpers",
    False,
    "Tableau 'Year Colour': highlights the year matching the Year parameter (assumes visual grouped by Date[Year]).",
)
add_m(
    "Bar Colours",
    'IF(SELECTEDVALUE(\'Date\'[Month Start])=[Date From Params Value],"Selected Month","Others")',
    None,
    "Report Helpers",
    False,
    "Tableau 'Bar Colours': highlights the selected month bar (assumes visual grouped by Date[Month Start]).",
)
add_m(
    "Airline Bar Colours",
    "IF(SELECTEDVALUE('Date'[Month Start])=[Date From Params Value]&&SELECTEDVALUE('%s'[Airline Name])=[Airline Parameter Value],\"Selected Month\",\"Others\")"
    % FACT,
    None,
    "Report Helpers",
    False,
    "Tableau 'Airline Bar Colours': highlights the selected month for the selected airline (assumes month+airline in context).",
)
add_m("Today", "TODAY()", "Short Date", "Report Helpers", False, "Tableau 'Today' = TODAY().")
add_m("Header size", "180", INT, "Report Helpers", False, "Tableau 'Header size' constant (180).")

# ---- convenience measure for the simplified 'CM Main Destinations' visual (destination-region scoped)
core = "DISTINCTCOUNT('%s'[Flight Id])" % FACT
dax = "CALCULATE(%s,%s,%s,%s,%s)" % (core, PERIOD["CM"][0], PERIOD["CM"][1], EXTRA["completed"], REGION_DEST)
add_m(
    "CM Comp Flights to Destination",
    dax,
    INT,
    "Current Month (CM)",
    False,
    "Convenience replacement for Tableau dimension 'CM Main Destinations' (a parameter-equality projection). Put Destination City on the visual + this measure to rank top CM destinations; region filter applies to Destination Region.",
)


# ================================================================ EMIT: fact table
def esc(s):
    return s.replace("\r", "").replace("\n", " ")


def measure_block(m):
    nm, dax, fmt, folder, hidden, desc = m
    out = []
    out.append("%s/// %s" % (T, esc(desc)))
    out.append("%smeasure '%s' = %s" % (T, nm, dax))
    if fmt:
        out.append("%s%sformatString: %s" % (T, T, fmt))
    if folder:
        out.append("%s%sdisplayFolder: %s" % (T, T, folder))
    if hidden:
        out.append("%s%sisHidden" % (T, T))
    out.append("")
    return "\n".join(out)


def column_block(caption, source, dtype, fmt=None, hidden=False, sortby=None, calc=None, desc=None):
    out = []
    if desc:
        out.append("%s/// %s" % (T, esc(desc)))
    if calc:
        out.append("%scolumn '%s' = %s" % (T, caption, calc))
    else:
        out.append("%scolumn '%s'" % (T, caption))
    out.append("%s%sdataType: %s" % (T, T, dtype))
    if fmt:
        out.append("%s%sformatString: %s" % (T, T, fmt))
    out.append("%s%ssummarizeBy: none" % (T, T))
    if not calc:
        out.append("%s%ssourceColumn: %s" % (T, T, source))
    if sortby:
        out.append("%s%ssortByColumn: '%s'" % (T, T, sortby))
    if hidden:
        out.append("%s%sisHidden" % (T, T))
    out.append("")
    out.append("%s%sannotation SummarizationSetBy = Automatic" % (T, T))
    out.append("")
    return "\n".join(out)


# fact partition M
mtypes = ", ".join('{"%s", %s}' % (c[1], c[3]) for c in cols)
part = []
part.append("%spartition '%s' = m" % (T, FACT))
part.append("%s%smode: import" % (T, T))
part.append("%s%ssource =" % (T, T))
part.append("%s%s%slet" % (T, T, T))
part.append(
    '%s%s%s%sSource = Csv.Document(File.Contents(DataFolder & "%s"),[Delimiter=",", Columns=70, Encoding=65001, QuoteStyle=QuoteStyle.Csv]),'
    % (T, T, T, T, CSVFILE)
)
part.append('%s%s%s%s#"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),' % (T, T, T, T))
part.append(
    '%s%s%s%s#"Changed Type" = Table.TransformColumnTypes(#"Promoted Headers",{%s}, "en-US")' % (T, T, T, T, mtypes)
)
part.append("%s%s%sin" % (T, T, T))
part.append('%s%s%s%s#"Changed Type"' % (T, T, T, T))

fact_lines = []
fact_lines.append(
    "/// Flight-leg grain fact table (246,236 rows; one row per scheduled flight leg per date). Source: Tableau data source 'airline_alliance_performance_2022_2025' (the '_1' extract; the near-duplicate non-suffixed source is vestigial - 0 worksheet bindings - see limitations_encountered). Hosts all period-comparison KPI measures."
)
fact_lines.append("table '%s'" % FACT)
fact_lines.append("")
for m in measures:
    fact_lines.append(measure_block(m))
# raw columns
for caption, source, dtype, mtype, dt in cols:
    fmt = None
    if dtype == "int64":
        fmt = "#,##0"
    if dtype == "dateTime":
        fmt = "Short Date"
    fact_lines.append(column_block(caption, source, dtype, fmt=fmt))
# Aircraft Name calc column
fact_lines.append(
    column_block(
        "Aircraft Name",
        None,
        "string",
        calc='IF(LEFT(\'%s\'[Aircraft Type],1)="A","Airbus","Boeing")' % FACT,
        desc="Tableau 'Aircraft Name' calculated column: IF LEFT(Aircraft Type,1)='A' THEN 'Airbus' ELSE 'Boeing'.",
    )
)
fact_lines.append("\n".join(part))
fact_lines.append("")
fact_lines.append("%sannotation PBI_ResultType = Table" % T)
fact_lines.append("")
w(os.path.join(TBL, "%s.tmdl" % FACT), "\n".join(fact_lines))

# ================================================================ EMIT: Date table
date_tbl = r"""/// Genuine Date dimension (Power Query calendar, 2021-01-01 to 2026-12-31 - a buffer around the real 2022-01-01..2025-01-31 fact-date domain so Prior Year (Year-1) windows never truncate). Related to 'Flight Activity'[Date]. Hosts the Tableau month/year layout-helper columns (Year Label, Month List, Label, Row Pos, Col Pos) which were row-level date calcs in the source workbook.
table Date
	dataCategory: Time

	/// Calendar date (relationship key; 'many' side is 'Flight Activity'[Date]).
	column Date
		dataType: dateTime
		formatString: Short Date
		summarizeBy: none
		isKey
		sourceColumn: Date

		annotation SummarizationSetBy = Automatic

	/// Calendar year, e.g. 2024.
	column Year
		dataType: int64
		formatString: #,##0
		summarizeBy: none
		sourceColumn: Year

		annotation SummarizationSetBy = Automatic

	/// Month number 1-12, hidden sort helper for 'Month'.
	column 'Month Number'
		dataType: int64
		formatString: #,##0
		summarizeBy: none
		sourceColumn: Month Number
		isHidden

		annotation SummarizationSetBy = Automatic

	/// Month display label, e.g. "January". Sorted chronologically by 'Month Number'.
	column Month
		dataType: string
		summarizeBy: none
		sourceColumn: Month
		sortByColumn: 'Month Number'

		annotation SummarizationSetBy = Automatic

	/// Quarter number 1-4, hidden sort helper for 'Quarter'.
	column 'Quarter Number'
		dataType: int64
		formatString: #,##0
		summarizeBy: none
		sourceColumn: Quarter Number
		isHidden

		annotation SummarizationSetBy = Automatic

	/// Quarter display label, e.g. "Q1". Sorted chronologically by 'Quarter Number'.
	column Quarter
		dataType: string
		summarizeBy: none
		sourceColumn: Quarter
		sortByColumn: 'Quarter Number'

		annotation SummarizationSetBy = Automatic

	/// First day of the calendar month containing Date (Tableau DATETRUNC('month',...)). Bound by the 'Bar Colours'/'Airline Bar Colours' measures.
	column 'Month Start'
		dataType: dateTime
		formatString: Short Date
		summarizeBy: none
		sourceColumn: Month Start

		annotation SummarizationSetBy = Automatic

	/// Tableau 'Year Label' calc: "'" + last 2 digits of the year, e.g. "'24" (the source IF is a no-op; both branches identical).
	column 'Year Label'
		dataType: string
		summarizeBy: none
		sourceColumn: Year Label

		annotation SummarizationSetBy = Automatic

	/// Tableau 'Month List' calc: first letter of the month name, e.g. "J".
	column 'Month List'
		dataType: string
		summarizeBy: none
		sourceColumn: Month List

		annotation SummarizationSetBy = Automatic

	/// Tableau 'Label' calc: first letter of the month name (duplicate of 'Month List'; source IF is a no-op).
	column Label
		dataType: string
		summarizeBy: none
		sourceColumn: Label

		annotation SummarizationSetBy = Automatic

	/// Tableau 'Row Pos' calc: month-grid row (1 for Jan-Apr, 2 for May-Aug, 3 for Sep-Dec).
	column 'Row Pos'
		dataType: int64
		formatString: #,##0
		summarizeBy: none
		sourceColumn: Row Pos

		annotation SummarizationSetBy = Automatic

	/// Tableau 'Col Pos' calc: month-grid column (1 for months 1/5/9, 2 for 2/6/10, 3 for 3/7/11, else 4).
	column 'Col Pos'
		dataType: int64
		formatString: #,##0
		summarizeBy: none
		sourceColumn: Col Pos

		annotation SummarizationSetBy = Automatic

	partition Date = m
		mode: import
		source =
				let
					StartDate = #date(2021, 1, 1),
					EndDate = #date(2026, 12, 31),
					NumberOfDays = Duration.Days(EndDate - StartDate) + 1,
					DateList = List.Dates(StartDate, NumberOfDays, #duration(1, 0, 0, 0)),
					#"Converted to Table" = Table.FromList(DateList, Splitter.SplitByNothing(), {"Date"}),
					#"Changed Type" = Table.TransformColumnTypes(#"Converted to Table", {{"Date", type date}}, "en-US"),
					#"Added Year" = Table.AddColumn(#"Changed Type", "Year", each Date.Year([Date]), Int64.Type),
					#"Added Month Number" = Table.AddColumn(#"Added Year", "Month Number", each Date.Month([Date]), Int64.Type),
					#"Added Month" = Table.AddColumn(#"Added Month Number", "Month", each Date.MonthName([Date]), type text),
					#"Added Quarter Number" = Table.AddColumn(#"Added Month", "Quarter Number", each Date.QuarterOfYear([Date]), Int64.Type),
					#"Added Quarter" = Table.AddColumn(#"Added Quarter Number", "Quarter", each "Q" & Text.From([Quarter Number]), type text),
					#"Added Month Start" = Table.AddColumn(#"Added Quarter", "Month Start", each Date.StartOfMonth([Date]), type date),
					#"Added Year Label" = Table.AddColumn(#"Added Month Start", "Year Label", each "'" & Text.End(Text.From([Year]), 2), type text),
					#"Added Month List" = Table.AddColumn(#"Added Year Label", "Month List", each Text.Start([Month], 1), type text),
					#"Added Label" = Table.AddColumn(#"Added Month List", "Label", each Text.Start([Month], 1), type text),
					#"Added Row Pos" = Table.AddColumn(#"Added Label", "Row Pos", each if [Month Number] <= 4 then 1 else if [Month Number] <= 8 then 2 else 3, Int64.Type),
					#"Added Col Pos" = Table.AddColumn(#"Added Row Pos", "Col Pos", each Number.Mod([Month Number] - 1, 4) + 1, Int64.Type)
				in
					#"Added Col Pos"

	annotation PBI_ResultType = Table
"""
w(os.path.join(TBL, "Date.tmdl"), date_tbl)


# ================================================================ EMIT: parameter tables
def param_table(
    table_name, col_name, dtype, rows, default, value_measure, folder="Parameters", ordered=True, extra_desc=""
):
    """rows: list of (value, order). value is python str/int."""
    q = '"%s"' if dtype == "string" else "%s"
    is_str = dtype == "string"
    mtype = "type text" if is_str else "Int64.Type"
    tdtype = "string" if is_str else "int64"
    lines = []
    lines.append(
        "/// Disconnected single-select slicer table backing the Tableau '%s' parameter (default %s).%s Native-slicer + SELECTEDVALUE equivalent of a Tableau live parameter (parameter-equality idiom)."
        % (table_name, (q % default), (" " + extra_desc if extra_desc else ""))
    )
    lines.append("table '%s'" % table_name)
    lines.append("")
    dv = ('"%s"' % default) if is_str else str(default)
    lines.append(
        "%s/// Reads the current '%s' slicer selection, defaulting to the Tableau default %s." % (T, table_name, dv)
    )
    lines.append("%smeasure '%s' = SELECTEDVALUE('%s'[%s], %s)" % (T, value_measure, table_name, col_name, dv))
    lines.append("%s%sdisplayFolder: %s" % (T, T, folder))
    lines.append("")
    # value column
    lines.append("%scolumn '%s'" % (T, col_name))
    lines.append("%s%sdataType: %s" % (T, T, tdtype))
    if not is_str:
        lines.append("%s%sformatString: #,##0" % (T, T))
    lines.append("%s%ssummarizeBy: none" % (T, T))
    lines.append("%s%ssourceColumn: %s" % (T, T, col_name))
    if ordered:
        lines.append("%s%ssortByColumn: '%s Order'" % (T, T, col_name))
    lines.append("")
    lines.append("%s%sannotation SummarizationSetBy = Automatic" % (T, T))
    lines.append("")
    if ordered:
        lines.append("%s/// Hidden sort-order helper preserving the Tableau parameter's list order." % T)
        lines.append("%scolumn '%s Order'" % (T, col_name))
        lines.append("%s%sdataType: int64" % (T, T))
        lines.append("%s%sformatString: #,##0" % (T, T))
        lines.append("%s%ssummarizeBy: none" % (T, T))
        lines.append("%s%ssourceColumn: %s Order" % (T, T, col_name))
        lines.append("%s%sisHidden" % (T, T))
        lines.append("")
        lines.append("%s%sannotation SummarizationSetBy = Automatic" % (T, T))
        lines.append("")
    # partition
    if ordered:
        colspec = '{"%s", "%s Order"}' % (col_name, col_name)
        rowspecs = ",\n".join("%s%s%s%s%s{%s, %d}" % (T, T, T, T, T, (q % v), o) for v, o in rows)
        tct = '{{"%s", %s}, {"%s Order", Int64.Type}}' % (col_name, mtype, col_name)
    else:
        colspec = '{"%s"}' % col_name
        rowspecs = ",\n".join("%s%s%s%s%s{%s}" % (T, T, T, T, T, (q % v)) for v, o in rows)
        tct = '{{"%s", %s}}' % (col_name, mtype)
    lines.append("%spartition '%s' = m" % (T, table_name))
    lines.append("%s%smode: import" % (T, T))
    lines.append("%s%ssource =" % (T, T))
    lines.append("%s%s%slet" % (T, T, T))
    lines.append("%s%s%s%sSource = #table(" % (T, T, T, T))
    lines.append("%s%s%s%s%s%s," % (T, T, T, T, T, colspec))
    lines.append("%s%s%s%s%s{" % (T, T, T, T, T))
    lines.append(rowspecs)
    lines.append("%s%s%s%s%s}" % (T, T, T, T, T))
    lines.append("%s%s%s%s)," % (T, T, T, T))
    lines.append('%s%s%s%s#"Changed Type" = Table.TransformColumnTypes(Source, %s, "en-US")' % (T, T, T, T, tct))
    lines.append("%s%s%sin" % (T, T, T))
    lines.append('%s%s%s%s#"Changed Type"' % (T, T, T, T))
    lines.append("")
    lines.append("%sannotation PBI_ResultType = Table" % T)
    lines.append("")
    return "\n".join(lines)


w(
    os.path.join(TBL, "Year Parameter.tmdl"),
    param_table(
        "Year Parameter",
        "Year",
        "integer",
        [(2022, 0), (2023, 1), (2024, 2), (2025, 3)],
        2023,
        "Year Parameter Value",
        ordered=True,
    ),
)
w(
    os.path.join(TBL, "Month Parameter.tmdl"),
    param_table(
        "Month Parameter",
        "Month",
        "integer",
        [(m, m) for m in range(1, 13)],
        7,
        "Month Parameter Value",
        ordered=False,
        extra_desc="Month number 1-12.",
    ),
)
w(
    os.path.join(TBL, "Region Parameter.tmdl"),
    param_table(
        "Region Parameter",
        "Region",
        "string",
        [
            ("Global", 0),
            ("Africa", 1),
            ("Asia", 2),
            ("Europe", 3),
            ("Middle East", 4),
            ("North America", 5),
            ("Oceania", 6),
            ("South America", 7),
        ],
        "North America",
        "Region Parameter Value",
        ordered=True,
        extra_desc="'Global' is the show-all sentinel (Origin Region filter is bypassed).",
    ),
)
w(
    os.path.join(TBL, "Airline Parameter.tmdl"),
    param_table(
        "Airline Parameter",
        "Airline",
        "string",
        [("Atlantic Express", 0), ("Global Star", 1), ("Pacific Wings", 2), ("SkyConnect Airways", 3)],
        "SkyConnect Airways",
        "Airline Parameter Value",
        ordered=True,
    ),
)
w(
    os.path.join(TBL, "Aircraft Type Parameter.tmdl"),
    param_table(
        "Aircraft Type Parameter",
        "Aircraft Type",
        "string",
        [("A320", 0), ("A330", 1), ("A350", 2), ("B737", 3), ("B777", 4), ("B787", 5)],
        "A330",
        "Aircraft Parameter Value",
        ordered=True,
    ),
)
w(
    os.path.join(TBL, "Airline Code Parameter.tmdl"),
    param_table(
        "Airline Code Parameter",
        "Airline Code",
        "string",
        [("AE", 0), ("GS", 1), ("PW", 2), ("SC", 3)],
        "GS",
        "Airline Code Parameter Value",
        ordered=True,
        extra_desc="Report-level slicer; not consumed by any KPI formula (parity only).",
    ),
)
w(
    os.path.join(TBL, "Guidelines Parameter.tmdl"),
    param_table(
        "Guidelines Parameter",
        "Guideline",
        "string",
        [("Show Guidelines", 0), ("Hide Guidelines", 1)],
        "Hide Guidelines",
        "Guidelines Value",
        ordered=True,
        extra_desc="Report-level show/hide toggle for gridline guides.",
    ),
)

# ================================================================ EMIT: relationships / model / db / expressions / pbism / platform
rel = """relationship 'Flight Activity_Date_Date_Date'
	fromColumn: 'Flight Activity'.Date
	toColumn: Date.Date
"""
w(os.path.join(DEF, "relationships.tmdl"), rel)

tables_order = [
    "Flight Activity",
    "Date",
    "Year Parameter",
    "Month Parameter",
    "Region Parameter",
    "Airline Parameter",
    "Aircraft Type Parameter",
    "Airline Code Parameter",
    "Guidelines Parameter",
]
qorder = json.dumps(tables_order + ["DataFolder"])
model = []
model.append("model Model")
model.append("%sculture: en-US" % T)
model.append("%sdefaultPowerBIDataSourceVersion: powerBI_V3" % T)
model.append("%ssourceQueryCulture: en-US" % T)
model.append("%sdataAccessOptions" % T)
model.append("%s%slegacyRedirects" % (T, T))
model.append("%s%sreturnErrorValuesAsNull" % (T, T))
model.append("")
model.append("annotation PBI_QueryOrder = %s" % qorder)
model.append("")
model.append("annotation __PBI_TimeIntelligenceEnabled = 0")
model.append("")
for t in tables_order:
    model.append("ref table '%s'" % t)
model.append("")
w(os.path.join(DEF, "model.tmdl"), "\n".join(model))

w(os.path.join(DEF, "database.tmdl"), "database\n\tcompatibilityLevel: 1702\n")

expr = (
    'expression DataFolder = "%s" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n\n\tannotation PBI_ResultType = Text\n'
    % DATADIR
)
w(os.path.join(DEF, "expressions.tmdl"), expr)

pbism = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
    "version": "4.2",
    "settings": {"qnaEnabled": False},
}
w(os.path.join(BASE, "definition.pbism"), json.dumps(pbism, indent=2))

platform = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
    "metadata": {"type": "SemanticModel", "displayName": "AirlineAllianceActivity"},
    "config": {"version": "2.0", "logicalId": "a1f4c9e2-7b6d-48a3-9c2e-5d8b1f3a6c04"},
}
w(os.path.join(BASE, ".platform"), json.dumps(platform, indent=2))

print("\nMEASURES:", len(measures), "| FACT COLUMNS:", len(cols) + 1, "| TABLES:", len(tables_order))
