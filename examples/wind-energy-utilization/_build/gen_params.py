"""Re-runnable generator for the 8 disconnected parameter slicer tables.
Mirrors the Airline 'Year Parameter.tmdl' pattern: a #table seed + a Value measure
read via SELECTEDVALUE(col, <Tableau default>) so a bare EVALUATE reproduces the
default dashboard state. Writes one .tmdl per parameter into definition/tables/.
"""
import os
T = "\t"
OUT = os.path.join(os.path.dirname(__file__), "..", "fabric",
                   "WindEnergyUtilization.SemanticModel", "definition", "tables")
OUT = os.path.abspath(OUT)


def m_rows(rows):
    """rows: list of tuples -> M list-of-lists literal, each row on its own 4-tab line."""
    out = []
    for r in rows:
        cells = []
        for c in r:
            if isinstance(c, str):
                cells.append('"' + c.replace('"', '""') + '"')
            else:
                cells.append(str(c))
        out.append("\t\t\t\t\t{" + ", ".join(cells) + "}")
    return ",\n".join(out)


def build(table, value_measure, default, value_col, cols, rows, coltypes, doc, measure_doc,
          number_default=False):
    """cols: list of (name, dataType, isHidden, sortBy or None). value_col: name the Value measure reads."""
    L = []
    L.append("/// " + doc)
    L.append("table '" + table + "'")
    L.append("")
    L.append(T + "/// " + measure_doc)
    if number_default:
        expr = "SELECTEDVALUE('" + table + "'[" + value_col + "], " + str(default) + ")"
    else:
        expr = "SELECTEDVALUE('" + table + "'[" + value_col + "], \"" + str(default) + "\")"
    L.append(T + "measure '" + value_measure + "' = " + expr)
    L.append(T + T + "displayFolder: Parameters")
    L.append("")
    for (name, dtype, hidden, sortby) in cols:
        L.append(T + "column '" + name + "'")
        L.append(T + T + "dataType: " + dtype)
        if dtype in ("int64",):
            L.append(T + T + "formatString: #,##0")
        L.append(T + T + "summarizeBy: none")
        L.append(T + T + "sourceColumn: " + name)
        if sortby:
            L.append(T + T + "sortByColumn: '" + sortby + "'")
        if hidden:
            L.append(T + T + "isHidden")
        L.append("")
        L.append(T + T + "annotation SummarizationSetBy = Automatic")
        L.append("")
    # partition
    colnames = ", ".join('"' + c[0] + '"' for c in cols)
    tc = ", ".join("{\"" + c[0] + "\", " + coltypes[c[0]] + "}" for c in cols)
    L.append(T + "partition '" + table + "' = m")
    L.append(T + T + "mode: import")
    L.append(T + T + "source =")
    L.append(T + T + T + "let")
    L.append(T + T + T + T + "Source = #table(")
    L.append(T + T + T + T + T + "{" + colnames + "},")
    L.append(T + T + T + T + T + "{")
    L.append(m_rows(rows))
    L.append(T + T + T + T + T + "}")
    L.append(T + T + T + T + "),")
    L.append(T + T + T + T + '#"Changed Type" = Table.TransformColumnTypes(Source, {' + tc + '}, "en-US")')
    L.append(T + T + T + "in")
    L.append(T + T + T + T + '#"Changed Type"')
    L.append("")
    L.append(T + "annotation PBI_ResultType = Table")
    L.append("")
    path = os.path.join(OUT, table + ".tmdl")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("WROTE", path, "(" + str(len(rows)) + " rows)")


# 1. Month Parameter (int value 1-12 + 2024 annual; default 6 = June)
months = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
rows = [(i + 1, months[i], i) for i in range(12)] + [(2024, "'24", 12)]
build("Month Parameter", "Month Parameter Value", 6, "Month Number",
      [("Month Number", "int64", True, None), ("Month", "string", False, "Month Order"),
       ("Month Order", "int64", True, None)],
      rows, {"Month Number": "Int64.Type", "Month": "type text", "Month Order": "Int64.Type"},
      "Disconnected single-select slicer table backing the Tableau 'Month Parameter' ([Parameter 1], default June=6; '24=2024 means annual/all-year). Native-slicer + SELECTEDVALUE equivalent of the Tableau live parameter that gates every CM/PM measure.",
      "Reads the current 'Month Parameter' slicer selection (Month Number), defaulting to the Tableau default 6 (June). 2024 = annual.",
      number_default=True)

# 2. Page Parameter (string 1-6; default 1)
build("Page Parameter", "Page Parameter Value", "1", "Page",
      [("Page", "string", False, "Page Order"), ("Page Order", "int64", True, None)],
      [(str(i), i - 1) for i in range(1, 7)],
      {"Page": "type text", "Page Order": "Int64.Type"},
      "Disconnected slicer table backing the Tableau 'Page Parameter' ([Parameter 2], default '1'). Drove Tableau's show/hide-sheet page-navigation idiom; in Power BI page navigation is native (buttons/bookmarks), so this is retained only as the selection domain + Value measure.",
      "Reads the current 'Page Parameter' selection, defaulting to the Tableau default \"1\".")

# 3. Radius Parameter (real, any-domain; default 500) - spiral tuning knob, single-row seed
build("Radius Parameter", "Radius Parameter Value", 500, "Radius",
      [("Radius", "double", False, None)],
      [(500,)], {"Radius": "type number"},
      "Single-row seed table backing the Tableau 'Radius Parameter' ([Parameter 3], any-numeric, default 500) - a spiral-geometry tuning knob. Held at the Tableau default; can be upgraded to a what-if slider if interactive spiral tuning is wanted.",
      "Reads the 'Radius Parameter' value, defaulting to the Tableau default 500 (spiral base radius).",
      number_default=True)

# 4. Spiral Start Point (real, any-domain; default 180)
build("Spiral Start Point", "Spiral Start Point Value", 180, "Spiral Start Point",
      [("Spiral Start Point", "double", False, None)],
      [(180,)], {"Spiral Start Point": "type number"},
      "Single-row seed table backing the Tableau 'Spiral Start Point' ([Parameter 4], any-numeric, default 180) - spiral start-angle (degrees) tuning knob. Held at the Tableau default.",
      "Reads the 'Spiral Start Point' value, defaulting to the Tableau default 180 (spiral start angle, degrees).",
      number_default=True)

# 5. Thickness (real, any-domain; default 0.1)
build("Thickness", "Thickness Value", 0.1, "Thickness",
      [("Thickness", "double", False, None)],
      [(0.1,)], {"Thickness": "type number"},
      "Single-row seed table backing the Tableau 'Thickness' ([Parameter 5], any-numeric, default 0.1) - spiral radius-increment tuning knob. Held at the Tableau default.",
      "Reads the 'Thickness' value, defaulting to the Tableau default 0.1 (spiral radius increment per Num step).",
      number_default=True)

# 6. Region Parameter (string, 5 NL provinces; default Flevoland)
regions = ["Flevoland", "Friesland", "Groningen", "Noord-Holland", "Zeeland"]
build("Region Parameter", "Region Parameter Value", "Flevoland", "Region",
      [("Region", "string", False, "Region Order"), ("Region Order", "int64", True, None)],
      [(regions[i], i) for i in range(len(regions))],
      {"Region": "type text", "Region Order": "Int64.Type"},
      "Disconnected slicer table backing the Tableau 'Region Parameter' (default 'Flevoland', 5 NL provinces). Not referenced by any calculated field; exposed as a native region slicer domain.",
      "Reads the current 'Region Parameter' selection, defaulting to the Tableau default \"Flevoland\".")

# 7. Turbine Id Parameter (string, 30 ids; default NL-WT-2024-001)
ids = ["NL-WT-2024-%03d" % i for i in range(1, 31)]
build("Turbine Id Parameter", "Turbine Id Parameter Value", "NL-WT-2024-001", "Turbine Id",
      [("Turbine Id", "string", False, "Turbine Id Order"), ("Turbine Id Order", "int64", True, None)],
      [(ids[i], i) for i in range(len(ids))],
      {"Turbine Id": "type text", "Turbine Id Order": "Int64.Type"},
      "Disconnected slicer table backing the Tableau 'Turbine Id Parameter' (default 'NL-WT-2024-001', 30 turbine ids). Not referenced by any calculated field; exposed as a native turbine-id slicer domain.",
      "Reads the current 'Turbine Id Parameter' selection, defaulting to the Tableau default \"NL-WT-2024-001\".")

# 8. Upd Turbine Name Parameter (string, 30 names; default GRWF Turbine 18) - THE turbine selector
names = ["FLWF Turbine 1", "ZEWF Turbine 2", "FRWF Turbine 3", "NHWF Turbine 4", "FRWF Turbine 5",
         "NHWF Turbine 6", "GRWF Turbine 7", "NHWF Turbine 8", "GRWF Turbine 9", "GRWF Turbine 10",
         "ZEWF Turbine 11", "FRWF Turbine 12", "FLWF Turbine 13", "FRWF Turbine 14", "NHWF Turbine 15",
         "FLWF Turbine 16", "FRWF Turbine 17", "GRWF Turbine 18", "ZEWF Turbine 19", "NHWF Turbine 20",
         "GRWF Turbine 21", "FLWF Turbine 22", "ZEWF Turbine 23", "GRWF Turbine 24", "ZEWF Turbine 25",
         "ZEWF Turbine 26", "FRWF Turbine 27", "GRWF Turbine 28", "ZEWF Turbine 29", "NHWF Turbine 30"]
build("Upd Turbine Name Parameter", "Upd Turbine Name Parameter Value", "GRWF Turbine 18", "Upd Turbine Name",
      [("Upd Turbine Name", "string", False, "Upd Turbine Name Order"),
       ("Upd Turbine Name Order", "int64", True, None)],
      [(names[i], i) for i in range(len(names))],
      {"Upd Turbine Name": "type text", "Upd Turbine Name Order": "Int64.Type"},
      "Disconnected single-select slicer table backing the Tableau 'Upd Turbine Name Parameter' (default 'GRWF Turbine 18', 30 turbines) - THE turbine selector driving every 'T ...' turbine-specific measure and the spiral length. SELECTEDVALUE equivalent of the Tableau live parameter (parameter-equality idiom).",
      "Reads the current turbine selection, defaulting to the Tableau default \"GRWF Turbine 18\".")

print("DONE - 8 parameter tables")
