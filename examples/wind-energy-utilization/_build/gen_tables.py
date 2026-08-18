"""Re-runnable generator for the data tables' measures + the two fact/scaffold tables.
- Writes 'Daily Performance.tmdl' (14 source cols + 4 date calc cols + energy/rate measures + M partition)
- Writes 'NL Densification.tmdl' (Year/Num cols + spiral-geometry measures + M partition)
- Injects measure blocks into the already-authored 'Turbine.tmdl' and 'CO2 Savings.tmdl'
  (right after the `table '...'` line, before the first column) - idempotent (skips if a
  measure is already present).
All DAX is single-line (avoids the TMDL multi-line indentation trap). Measure names never
collide with a column name in the same table; model-wide names are asserted unique by the
validator script. Column refs use the TMDL friendly `name`, never `sourceColumn`.
"""
import os
T = "\t"
BASE = os.path.join(os.path.dirname(__file__), "..", "fabric",
                    "WindEnergyUtilization.SemanticModel", "definition", "tables")
BASE = os.path.abspath(BASE)


# ---------- helpers ----------
def emit_measure(name, dax, fmt=None, folder=None, doc=None):
    L = []
    if doc:
        L.append(T + "/// " + doc)
    L.append(T + "measure '" + name + "' = " + dax)
    if fmt is not None:
        L.append(T + T + "formatString: " + fmt)
    if folder:
        L.append(T + T + "displayFolder: " + folder)
    L.append("")
    return "\n".join(L)


def emit_source_col(name, dtype, src, summ, fmt=None, datacat=None, sortby=None, hidden=False, doc=None):
    L = []
    if doc:
        L.append(T + "/// " + doc)
    L.append(T + "column '" + name + "'")
    L.append(T + T + "dataType: " + dtype)
    if fmt:
        L.append(T + T + "formatString: " + fmt)
    L.append(T + T + "summarizeBy: " + summ)
    L.append(T + T + "sourceColumn: " + src)
    if datacat:
        L.append(T + T + "dataCategory: " + datacat)
    if sortby:
        L.append(T + T + "sortByColumn: '" + sortby + "'")
    if hidden:
        L.append(T + T + "isHidden")
    L.append("")
    L.append(T + T + "annotation SummarizationSetBy = Automatic")
    L.append("")
    return "\n".join(L)


def emit_calc_col(name, dax, dtype, summ, fmt=None, sortby=None, doc=None):
    L = []
    if doc:
        L.append(T + "/// " + doc)
    L.append(T + "column '" + name + "' = " + dax)
    L.append(T + T + "dataType: " + dtype)
    if fmt:
        L.append(T + T + "formatString: " + fmt)
    L.append(T + T + "summarizeBy: " + summ)
    if sortby:
        L.append(T + T + "sortByColumn: '" + sortby + "'")
    L.append("")
    L.append(T + T + "annotation SummarizationSetBy = Automatic")
    L.append("")
    return "\n".join(L)


def emit_partition_csv(table, csv, ncols, transforms):
    L = []
    L.append(T + "partition '" + table + "' = m")
    L.append(T + T + "mode: import")
    L.append(T + T + "source =")
    L.append(T + T + T + "let")
    L.append(T + T + T + T + 'Source = Csv.Document(File.Contents(DataFolder & "' + csv +
             '"), [Delimiter = ",", Columns = ' + str(ncols) +
             ', Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),')
    L.append(T + T + T + T + '#"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),')
    tr = ", ".join("{\"" + c + "\", " + t + "}" for c, t in transforms)
    L.append(T + T + T + T + '#"Changed Type" = Table.TransformColumnTypes(#"Promoted Headers", {' + tr + '}, "en-US")')
    L.append(T + T + T + "in")
    L.append(T + T + T + T + '#"Changed Type"')
    L.append("")
    L.append(T + "annotation PBI_ResultType = Table")
    L.append("")
    return "\n".join(L)


# ---------- DAX fragments ----------
CM = "FILTER(ALL('Daily Performance'[Month Number]), _m = 2024 || 'Daily Performance'[Month Number] = _m)"
PM = "FILTER(ALL('Daily Performance'[Month Number]), _m = 2024 || 'Daily Performance'[Month Number] = _m - 1)"
TF = "FILTER(ALL('Turbine'[Upd Turbine Name]), 'Turbine'[Upd Turbine Name] = _t)"


def mom(cm, pm, op):
    return ("VAR _cm = [" + cm + "] VAR _pm = [" + pm + "] RETURN IF(_cm " + op +
            " _pm, DIVIDE(_cm, _pm) - 1)")


# ======================================================================
# DAILY PERFORMANCE
# ======================================================================
dp_cols = [
    emit_source_col("Turbine Id", "string", "turbine_id", "none"),
    emit_source_col("Date", "dateTime", "date", "none", fmt="yyyy-mm-dd",
                    doc="Daily observation date (2024). Source of the Month/Weekday date-part calc columns and the P1 month gate in every CM/PM measure."),
    emit_source_col("Timestamp", "dateTime", "timestamp", "none", fmt="yyyy-mm-dd", hidden=True,
                    doc="Redundant copy of Date in the source extract; hidden."),
    emit_source_col("Energy Forecast Mwh", "double", "energy_forecast_mwh", "sum"),
    emit_source_col("Energy Actual Mwh", "double", "energy_actual_mwh", "sum"),
    emit_source_col("Capacity Factor Forecast", "double", "capacity_factor_forecast", "none"),
    emit_source_col("Capacity Factor Actual", "double", "capacity_factor_actual", "none"),
    emit_source_col("Avg Wind Speed Ms", "double", "avg_wind_speed_ms", "none"),
    emit_source_col("Max Wind Speed Ms", "double", "max_wind_speed_ms", "none"),
    emit_source_col("Availability Percent", "double", "availability_percent", "none"),
    emit_source_col("Downtime Hours", "double", "downtime_hours", "sum"),
    emit_source_col("Performance Ratio", "double", "performance_ratio", "none"),
    emit_source_col("Grid Export Mwh", "double", "grid_export_mwh", "sum"),
    emit_source_col("Ambient Temperature C", "double", "ambient_temperature_c", "none"),
    emit_calc_col("Month Number", "MONTH('Daily Performance'[Date])", "int64", "none", fmt="#,##0",
                  doc="MONTH([Date]); the join key for the Tableau P1 month-parameter gate."),
    emit_calc_col("Month", "FORMAT('Daily Performance'[Date], \"MMMM\")", "string", "none",
                  sortby="Month Number",
                  doc="DATENAME('month',[date]) equivalent; sorted by Month Number."),
    emit_calc_col("Weekday", "FORMAT('Daily Performance'[Date], \"dddd\")", "string", "none",
                  doc="DATENAME('weekday',[date]) equivalent."),
    emit_calc_col("Month Label", "LEFT('Daily Performance'[Month], 1)", "string", "none",
                  doc="First initial of the month name. Tableau's [Month Label] IF/ELSE branches were identical (dead conditional) - simplified to the unconditional LEFT(...,1)."),
]
dp_measures = [
    # --- Fleet KPIs ---
    emit_measure("CM Total Output", "VAR _m = [Month Parameter Value] RETURN CALCULATE(SUM('Daily Performance'[Energy Actual Mwh]), " + CM + ")", "#,##0.00", "01 Fleet KPIs",
                 "Current-month (P1) total actual output (MWh). P1=2024 means annual. Default June=28,240.79 MWh."),
    emit_measure("PM Total Output", "VAR _m = [Month Parameter Value] RETURN CALCULATE(SUM('Daily Performance'[Energy Actual Mwh]), " + PM + ")", "#,##0.00", "01 Fleet KPIs",
                 "Prior-month (P1-1) total actual output (MWh). Default (June selected) = May = 36,292.69 MWh."),
    emit_measure("CM Capacity Factor", "VAR _m = [Month Parameter Value] RETURN CALCULATE(AVERAGE('Daily Performance'[Capacity Factor Actual]), " + CM + ") / 100", "0.0%", "01 Fleet KPIs",
                 "Current-month average capacity factor (fraction). Default June = 0.3007."),
    emit_measure("CM Performance Ratio", "VAR _m = [Month Parameter Value] RETURN CALCULATE(AVERAGE('Daily Performance'[Performance Ratio]), " + CM + ") / 100", "0.0%", "01 Fleet KPIs",
                 "Current-month average performance ratio (fraction). Default June = 0.9919."),
    emit_measure("CM Availability", "VAR _m = [Month Parameter Value] RETURN CALCULATE(AVERAGE('Daily Performance'[Availability Percent]), " + CM + ") / 100", "0.0%", "01 Fleet KPIs",
                 "Current-month average availability (fraction). Default June = 0.9407."),
    emit_measure("Total Actual Output (2024)", "SUM('Daily Performance'[Energy Actual Mwh])", "#,##0.00", "01 Fleet KPIs",
                 "Full-year actual output (MWh) = 453,167.28."),
    emit_measure("Total Forecast Output (2024)", "SUM('Daily Performance'[Energy Forecast Mwh])", "#,##0.00", "01 Fleet KPIs",
                 "Full-year forecast output (MWh) = 455,969.88."),
    # --- Turbine KPIs ---
    emit_measure("T CM Total Output", "VAR _m = [Month Parameter Value] VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(SUM('Daily Performance'[Energy Actual Mwh]), " + CM + ", " + TF + ")", "#,##0.00", "02 Turbine KPIs",
                 "Selected-turbine current-month actual output. Default (GRWF Turbine 18, June) = 1,117.32 MWh."),
    emit_measure("T PM Total Output", "VAR _m = [Month Parameter Value] VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(SUM('Daily Performance'[Energy Actual Mwh]), " + PM + ", " + TF + ")", "#,##0.00", "02 Turbine KPIs",
                 "Selected-turbine prior-month actual output."),
    emit_measure("T CM Capacity Factor", "VAR _m = [Month Parameter Value] VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(AVERAGE('Daily Performance'[Capacity Factor Actual]), " + CM + ", " + TF + ") / 100", "0.0%", "02 Turbine KPIs",
                 "Selected-turbine current-month capacity factor (fraction, /100)."),
    emit_measure("T PM Capacity Factor", "VAR _m = [Month Parameter Value] VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(AVERAGE('Daily Performance'[Capacity Factor Actual]), " + PM + ", " + TF + ")", "#,##0.00", "02 Turbine KPIs",
                 "SOURCE BUG (faithful): Tableau's [T PM Capacity Factor] omits the /100 that [T CM Capacity Factor] applies, so T MoM Capacity Factor compares a fraction (~0.30) to a percent (~30) - a 100x scale mismatch. Translated as-authored; flagged."),
    emit_measure("T CM Performance Ratio", "VAR _m = [Month Parameter Value] VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(AVERAGE('Daily Performance'[Performance Ratio]), " + CM + ", " + TF + ") / 100", "0.0%", "02 Turbine KPIs",
                 "Selected-turbine current-month performance ratio (fraction, /100)."),
    emit_measure("T PM Performance Ratio", "VAR _m = [Month Parameter Value] VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(AVERAGE('Daily Performance'[Performance Ratio]), " + PM + ", " + TF + ") / 100", "0.0%", "02 Turbine KPIs",
                 "Selected-turbine prior-month performance ratio (fraction, /100)."),
    emit_measure("T CM Performance Ratio (Abs)", "VAR _m = [Month Parameter Value] VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(AVERAGE('Daily Performance'[Performance Ratio]), " + CM + ", " + TF + ")", "#,##0.00", "02 Turbine KPIs",
                 "Selected-turbine current-month performance ratio in ABSOLUTE percent (no /100). Drives the spiral length (# of Num points shown). Default (GRWF Turbine 18, June) = 104.68."),
    emit_measure("T CM Availability", "VAR _m = [Month Parameter Value] VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(AVERAGE('Daily Performance'[Availability Percent]), " + CM + ", " + TF + ") / 100", "0.0%", "02 Turbine KPIs",
                 "Selected-turbine current-month availability (fraction, /100)."),
    emit_measure("T PM Availability", "VAR _m = [Month Parameter Value] VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(AVERAGE('Daily Performance'[Availability Percent]), " + PM + ", " + TF + ") / 100", "0.0%", "02 Turbine KPIs",
                 "Selected-turbine prior-month availability (fraction, /100)."),
    # --- MoM (fleet) ---
    emit_measure("Pos MoM Total Output", mom("CM Total Output", "PM Total Output", ">"), "0.0%", "03 MoM Total Output",
                 "Positive month-over-month change in fleet output (fires only when CM > PM)."),
    emit_measure("Neg MoM Total Output", mom("CM Total Output", "PM Total Output", "<"), "0.0%", "03 MoM Total Output",
                 "Negative month-over-month change (fires only when CM < PM)."),
    emit_measure("Neut MoM Total Output", mom("CM Total Output", "PM Total Output", "="), "0.0%", "03 MoM Total Output",
                 "Neutral month-over-month (fires only when CM = PM)."),
    # --- MoM (turbine) ---
    emit_measure("T Pos MoM Total Output", mom("T CM Total Output", "T PM Total Output", ">"), "0.0%", "04 MoM Turbine",
                 "Selected-turbine positive MoM output."),
    emit_measure("T Neg MoM Total Output", mom("T CM Total Output", "T PM Total Output", "<"), "0.0%", "04 MoM Turbine",
                 "Selected-turbine negative MoM output."),
    emit_measure("T Neut MoM Total Output", mom("T CM Total Output", "T PM Total Output", "="), "0.0%", "04 MoM Turbine",
                 "Selected-turbine neutral MoM output."),
    emit_measure("T Pos MoM Performance Ratio", mom("T CM Performance Ratio", "T PM Performance Ratio", ">"), "0.0%", "04 MoM Turbine",
                 "Selected-turbine positive MoM performance ratio."),
    emit_measure("T Neg MoM Performance Ratio", mom("T CM Performance Ratio", "T PM Performance Ratio", "<"), "0.0%", "04 MoM Turbine",
                 "Selected-turbine negative MoM performance ratio."),
    emit_measure("T Neut MoM Performance Ratio", mom("T CM Performance Ratio", "T PM Performance Ratio", "="), "0.0%", "04 MoM Turbine",
                 "Selected-turbine neutral MoM performance ratio."),
    emit_measure("T Pos MoM Availability", mom("T CM Availability", "T PM Availability", ">"), "0.0%", "04 MoM Turbine",
                 "Selected-turbine positive MoM availability."),
    emit_measure("T Neg MoM Availability", mom("T CM Availability", "T PM Availability", "<"), "0.0%", "04 MoM Turbine",
                 "Selected-turbine negative MoM availability."),
    emit_measure("T Neut MoM Availability", mom("T CM Availability", "T PM Availability", "="), "0.0%", "04 MoM Turbine",
                 "Selected-turbine neutral MoM availability."),
    emit_measure("T Pos MoM Capacity Factor", mom("T CM Capacity Factor", "T PM Capacity Factor", ">"), "0.0%", "04 MoM Turbine",
                 "SOURCE BUG (faithful): compares [T CM Capacity Factor] (/100 fraction) to [T PM Capacity Factor] (raw percent) - 100x mismatch; near-always blank."),
    emit_measure("T Neg MoM Capacity Factor", mom("T CM Capacity Factor", "T PM Capacity Factor", "<"), "0.0%", "04 MoM Turbine",
                 "SOURCE BUG (faithful): CM(fraction) < PM(percent) almost always true -> this fires ~-99%. See T PM Capacity Factor note."),
    emit_measure("T Neut MoM Capacity Factor", mom("T CM Capacity Factor", "T PM Capacity Factor", "="), "0.0%", "04 MoM Turbine",
                 "SOURCE BUG (faithful): CM/PM 100x scale mismatch; near-always blank."),
    # --- Table-calc labels ---
    emit_measure("Max Monthly Output", "VAR _cur = [Total Actual Output (2024)] VAR _mx = MAXX(ALLSELECTED('Daily Performance'[Month]), [Total Actual Output (2024)]) RETURN IF(_cur = _mx, \"Max\")", None, "05 Labels",
                 "Tableau WINDOW_MAX label: 'Max' on the peak month's bar (evaluate with Month on the axis). MAXX over ALLSELECTED months replaces the table calc."),
    emit_measure("Min Monthly Output", "VAR _cur = [Total Actual Output (2024)] VAR _mn = MINX(ALLSELECTED('Daily Performance'[Month]), [Total Actual Output (2024)]) RETURN IF(_cur = _mn, \"Min\")", None, "05 Labels",
                 "Tableau WINDOW_MIN label: 'Min' on the trough month's bar."),
    # --- Dynamic param labels ---
    emit_measure("Month in View", "VAR _p = [Month Parameter Value] RETURN IF(_p = 2024, \"2024\", FORMAT(DATE(2024, _p, 1), \"MMMM\") & \" 2024\")", None, "05 Labels",
                 "Card label: selected month name + year, or '2024' when annual (P1=2024)."),
    emit_measure("Selected Month (Bars)", "VAR _p = [Month Parameter Value] VAR _mn = SELECTEDVALUE('Daily Performance'[Month Number]) RETURN IF(_p = _mn, \"Selected Month\", IF(_p = 2024, \"Annual\", \"Others\"))", None, "05 Labels",
                 "Bar-colour bucket: 'Selected Month' / 'Annual' / 'Others' (evaluate per month)."),
    emit_measure("vs. PM Label", "IF([Month Parameter Value] <> 2024, \"vs. PM\")", None, "05 Labels",
                 "Shows 'vs. PM' only when a specific month (not annual) is selected."),
    emit_measure("in 2024 Label", "IF([Month Parameter Value] = 2024, \"in 2024\")", None, "05 Labels",
                 "Shows 'in 2024' only when annual (P1=2024) is selected."),
]

dp_transforms = [
    ("turbine_id", "type text"), ("date", "type date"), ("timestamp", "type date"),
    ("energy_forecast_mwh", "type number"), ("energy_actual_mwh", "type number"),
    ("capacity_factor_forecast", "type number"), ("capacity_factor_actual", "type number"),
    ("avg_wind_speed_ms", "type number"), ("max_wind_speed_ms", "type number"),
    ("availability_percent", "type number"), ("downtime_hours", "type number"),
    ("performance_ratio", "type number"), ("grid_export_mwh", "type number"),
    ("ambient_temperature_c", "type number"),
]

dp_doc = "Daily turbine performance fact (10,980 rows = 30 turbines x 366 days, 2024). Source: Tableau relationship-model table 'daily_performance_2024'. Many-to-one to Turbine on Turbine Id. Hosts the energy/capacity-factor/performance/availability CM (current-month), PM (prior-month), T (selected-turbine) and MoM measures gated by the Month Parameter (P1)."

with open(os.path.join(BASE, "Daily Performance.tmdl"), "w", encoding="utf-8") as f:
    f.write("/// " + dp_doc + "\n")
    f.write("table 'Daily Performance'\n\n")
    f.write("\n".join(dp_measures))
    f.write("\n")
    f.write("\n".join(dp_cols))
    f.write("\n")
    f.write(emit_partition_csv("Daily Performance", "daily_performance_2024.csv", 14, dp_transforms))
print("WROTE Daily Performance.tmdl:", len(dp_measures), "measures,", len(dp_cols), "columns")

# ======================================================================
# NL DENSIFICATION (disconnected spiral scaffold)
# ======================================================================
nl_cols = [
    emit_source_col("Year", "int64", "Year", "none", fmt="0"),
    emit_source_col("Num", "int64", "Num", "none", fmt="#,##0",
                    doc="Spiral point index 0..125; the path/detail dimension of the spiral line chart."),
]
nl_measures = [
    emit_measure("Spiral Radius", "[Radius Parameter Value] + MIN('NL Densification'[Num]) + [Thickness Value]", "0.000000", "Spiral",
                 "Tableau [Radius] = Radius Param + Num + Thickness. Renamed from 'Radius' to avoid confusion with 'Radius Parameter'[Radius]."),
    emit_measure("Spiral Angle", "RADIANS(MIN('NL Densification'[Num]) * (360.0 / 100.0)) + RADIANS([Spiral Start Point Value])", "0.000000", "Spiral",
                 "Tableau [Angle] = RADIANS(Num*3.6) + RADIANS(Spiral Start Point). Renamed from 'Angle'."),
    emit_measure("Spiral X", "VAR _r = [Spiral Radius] VAR _a = [Spiral Angle] RETURN ((_r * COS(_a)) / 360) * -1", "0.000000", "Spiral",
                 "Tableau [X] cartesian coordinate of the spiral point. Plot on a scatter/line X axis (NOT a map - MAKEPOINT here builds a cartesian spiral, not geography)."),
    emit_measure("Spiral Y", "VAR _r = [Spiral Radius] VAR _a = [Spiral Angle] RETURN ((_r * SIN(_a)) / 360) * -1", "0.000000", "Spiral",
                 "Tableau [Y] cartesian coordinate of the spiral point. Plot on the scatter/line Y axis."),
    emit_measure("Spiral Check", "VAR _abs = [T CM Performance Ratio (Abs)] RETURN IF(MIN('NL Densification'[Num]) <= _abs, MIN('NL Densification'[Num]))", "#,##0", "Spiral",
                 "Tableau [Spiral Check]: Num if Num <= selected-turbine perf-ratio (abs), else blank."),
    emit_measure("Spiral Filter", "VAR _abs = [T CM Performance Ratio (Abs)] RETURN IF(MIN('NL Densification'[Num]) <= _abs, 1, 0)", "#,##0", "Spiral",
                 "Tableau [Spiral Filter] boolean as 1/0: keep Num points up to the selected-turbine perf ratio. Report filters Num where = 1 (default GRWF Turbine 18 -> Num 0..104)."),
    emit_measure("Spiral Colour", "VAR _c = [Spiral Check] RETURN IF(NOT ISBLANK(_c) && _c <= 100, \"A\", \"B\")", None, "Spiral",
                 "Tableau [Spiral Colour]: 'A' for Num<=100 (within one revolution), else 'B'. Explicit ISBLANK guard because DAX BLANK<=100 would wrongly be TRUE."),
    emit_measure("Spiral Length", "VAR _abs = [T CM Performance Ratio (Abs)] RETURN INT(_abs)", "#,##0", "Spiral",
                 "Simplification of Tableau's nested FIXED [Max Path] LOD (spiral point count - 1): with integer Num the count of Num in [0, abs] minus 1 = INT(abs). Default GRWF Turbine 18 = 104."),
    emit_measure("Spiral Zero X", "0", "0", "Spiral",
                 "Tableau [Spiral Zero] = MAKEPOINT(0,0) centre marker, X. Plot as a single reference point at the spiral origin."),
    emit_measure("Spiral Zero Y", "0", "0", "Spiral",
                 "Tableau [Spiral Zero] = MAKEPOINT(0,0) centre marker, Y."),
]
nl_doc = "Disconnected spiral-geometry scaffold (126 rows: Year, Num=0..125). Source: Tableau relationship-model table 'NL Densification' (the pre-extracted ds.turbine_master_data_nl_wind_energy_2024.csv is this Year/Num scaffold). NOT related to any table - it only supplies the Num index that the spiral X/Y measures sweep to draw the hero spiral performance chart on a native scatter/line visual (NOT a map)."
with open(os.path.join(BASE, "NL Densification.tmdl"), "w", encoding="utf-8") as f:
    f.write("/// " + nl_doc + "\n")
    f.write("table 'NL Densification'\n\n")
    f.write("\n".join(nl_measures))
    f.write("\n")
    f.write("\n".join(nl_cols))
    f.write("\n")
    f.write(emit_partition_csv("NL Densification", "nl_densification.csv", 2,
                               [("Year", "Int64.Type"), ("Num", "Int64.Type")]))
print("WROTE NL Densification.tmdl:", len(nl_measures), "measures,", len(nl_cols), "columns")

# ======================================================================
# TURBINE measures (inject) + CO2 SAVINGS measures (inject)
# ======================================================================
turbine_measures = [
    emit_measure("No of Turbines", "DISTINCTCOUNT('Turbine'[Turbine Id])", "#,##0", "Fleet Summary",
                 "Fleet count = 30."),
    emit_measure("Active Turbines", "CALCULATE(DISTINCTCOUNT('Turbine'[Turbine Id]), 'Turbine'[Operational Status] = \"Operational\")", "#,##0", "Fleet Summary",
                 "Operational turbine count = 28 (2 in Maintenance)."),
    emit_measure("Onshore Turbines", "CALCULATE(DISTINCTCOUNT('Turbine'[Turbine Id]), 'Turbine'[Onshore Offshore] = \"Onshore\")", "#,##0", "Fleet Summary",
                 "Onshore turbine count = 24 (6 offshore)."),
    emit_measure("CM Total Capacity", "SUM('Turbine'[Capacity Mw])", "#,##0.0", "Fleet Summary",
                 "Fleet nameplate capacity (MW) = 130.2. NOTE: Tableau's [CM Total Capacity] SUMs capacity_mw over month-filtered DAILY rows, fan-out-inflating by the day count (e.g. 30x130.2 for June); corrected here to static nameplate. Flagged."),
    emit_measure("T CM Total Capacity", "VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(SUM('Turbine'[Capacity Mw]), " + TF + ")", "#,##0.0", "Fleet Summary",
                 "Selected-turbine nameplate capacity (MW). NOTE: Tableau's [T CM Total Capacity] has BOTH a day-count fan-out AND an AND/OR precedence bug (missing parens: MONTH=P1 OR (P1=2024 AND name=param)); corrected to static per-turbine nameplate. Flagged."),
    emit_measure("Rank", "RANKX(ALL('Turbine'[Upd Turbine Name]), [CM Total Output], , DESC, Dense)", "#,##0", "Fleet Summary",
                 "Tableau RANK([CM Total Output],'desc') table calc -> RANKX over all turbines by current-month output (evaluate per turbine)."),
    emit_measure("Selected Turbine ID", "VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(SELECTEDVALUE('Turbine'[Turbine Id]), " + TF + ")", None, "Fleet Summary",
                 "turbine_id of the parameter-selected turbine (Tableau [Selected Turbine ID])."),
    emit_measure("Country", "\"Netherlands\"", None, "Fleet Summary",
                 "Tableau constant [Country] = 'Netherlands'."),
    emit_measure("Today", "TODAY()", "yyyy-mm-dd", "Fleet Summary",
                 "Tableau [Today] = TODAY()."),
]
co2_measures = [
    emit_measure("Total Co2 Saved", "SUM('CO2 Savings'[Co2 Saved Tonnes])", "#,##0.00", "CO2",
                 "Fleet annual CO2 saved (tonnes) = 169,031.37."),
    emit_measure("CM CO2 Saved (Tn)", "AVERAGE('CO2 Savings'[Co2 Saved Tonnes])", "#,##0.00", "CO2",
                 "Tableau [CM CO2 Saved (Tn)] = AVG of month-gated co2. CO2 is ANNUAL per turbine, so the value is MONTH-INVARIANT (proven): equals the simple mean over 30 turbines = 5,634.38. The Month gate is a structural no-op here."),
    emit_measure("CM Homes Powered", "AVERAGE('CO2 Savings'[Homes Powered Annually])", "#,##0", "CO2",
                 "Month-invariant fleet mean of annual homes powered (see CM CO2 Saved note)."),
    emit_measure("CM Trees", "AVERAGE('CO2 Savings'[Trees Equivalent])", "#,##0", "CO2",
                 "Month-invariant fleet mean of tree-equivalents."),
    emit_measure("T CM CO2 Saved (Tn)", "VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(AVERAGE('CO2 Savings'[Co2 Saved Tonnes]), " + TF + ")", "#,##0.00", "CO2",
                 "Selected-turbine annual CO2 saved (month-invariant)."),
    emit_measure("T CM Homes Powered", "VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(AVERAGE('CO2 Savings'[Homes Powered Annually]), " + TF + ")", "#,##0", "CO2",
                 "Selected-turbine annual homes powered (month-invariant)."),
    emit_measure("T PM CO2 Saved (Tn)", "VAR _t = [Upd Turbine Name Parameter Value] RETURN CALCULATE(AVERAGE('CO2 Savings'[Co2 Saved Tonnes]), " + TF + ")", "#,##0.00", "CO2",
                 "Selected-turbine 'prior-month' CO2 - annual/month-invariant so identical to T CM CO2 Saved (Tn). Makes T MoM CO2 definitionally 0/blank."),
    emit_measure("T CM Cars Offset", "VAR _m = [Month Parameter Value] VAR _t = [Upd Turbine Name Parameter Value] RETURN IF(_m = 2024, CALCULATE(AVERAGE('CO2 Savings'[Cars Offset Annually]), " + TF + "), AVERAGE('CO2 Savings'[Cars Offset Annually]))", "#,##0", "CO2",
                 "SOURCE BUG (faithful): Tableau's [T CM Cars Offset] is missing parens (MONTH=P1 OR (P1=2024 AND name=param)), so it is NOT turbine-filtered for a specific month (returns the fleet mean) and only turbine-specific when annual. Translated exactly; flagged."),
    emit_measure("T Pos MoM CO2 Saved (Tn)", mom("T CM CO2 Saved (Tn)", "T PM CO2 Saved (Tn)", ">"), "0.0%", "CO2",
                 "Always blank: CO2 is annual so T CM = T PM (no positive MoM possible). Faithful; flagged."),
    emit_measure("T Neg MoM CO2 Saved", mom("T CM CO2 Saved (Tn)", "T PM CO2 Saved (Tn)", "<"), "0.0%", "CO2",
                 "Always blank: CO2 annual so T CM = T PM. Faithful; flagged."),
    emit_measure("T Neut MoM CO2 Saved (Tn)", mom("T CM CO2 Saved (Tn)", "T PM CO2 Saved (Tn)", "="), "0.0%", "CO2",
                 "Always 0: CO2 annual so T CM = T PM -> (co2/co2)-1 = 0. Faithful; flagged."),
]


def inject(fname, measures_block):
    path = os.path.join(BASE, fname)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if "\n\tmeasure " in content:
        print("SKIP inject (measures already present):", fname)
        return
    lines = content.split("\n")
    # find the `table '...'` line, insert after it (+ a blank line)
    for i, ln in enumerate(lines):
        if ln.startswith("table '"):
            block = "\n" + "\n".join(measures_block)
            lines.insert(i + 1, block)
            break
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("INJECTED", len(measures_block), "measures into", fname)


inject("Turbine.tmdl", turbine_measures)
inject("CO2 Savings.tmdl", co2_measures)
print("DONE gen_tables")
