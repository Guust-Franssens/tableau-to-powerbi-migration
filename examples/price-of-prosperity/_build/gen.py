#!/usr/bin/env python
"""Re-runnable PBIR generator for the 'Price of Prosperity' report.

Single source of truth for the page layout (positions in px on a 1366x900 page,
derived from the Tableau dashboard zone tree) AND for every visual.json.

Modes:
  python gen.py audit       -> overlap/space audit + layout table (skeleton gate)
  python gen.py wireframe   -> render _build/wireframe.png from positions
  python gen.py emit        -> (re)write all visual.json files under the .Report
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.abspath(os.path.join(HERE, "..", "fabric", "PriceOfProsperity.Report"))
VIS_DIR = os.path.join(REPORT, "definition", "pages", "dashboard", "visuals")
# 2.9.0 is the newest visualContainer schema that actually RESOLVES. A newer URL 404s and
# `powerbi-report-author validate` then silently SKIPS schema checking for every visual it
# emits while still printing "0 error(s)", so a broken encoding ships green.
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.9.0/schema.json"
E = "Country Indicators"

PAGE_W, PAGE_H = 1366, 900

# ---- Continent palette (alpha order == theme dataColors) -------------------
CONT = {
    "Africa":        {"color": "#45B7A8", "bg": "#F3FAF9", "xend": 10},
    "Asia":          {"color": "#C77FC1", "bg": "#FBF7FB", "xend": 50},
    "Europe":        {"color": "#9B93C9", "bg": "#F0F3FA", "xend": 30},
    "North America": {"color": "#7EA9D6", "bg": "#F0F7FA", "xend": 20},
    "Oceania":       {"color": "#E9A25D", "bg": "#F9F3EF", "xend": 20},
    "South America": {"color": "#E4C550", "bg": "#FEFAF1", "xend": 6},
}

PARA1 = ("The pursuit of economic growth has historically correlated with the exploitation of "
         "natural resources. Countries with great wealth today often gained their fortune at an "
         "environmental cost, creating a world where for other countries to follow in their "
         "footsteps would result in environmental collapse. Wealthy nations have a moral "
         "obligation to aid developing countries in achieving economic progress without "
         "worsening environmental degradation.")
PARA2 = ("This visualization explores the relationship between CO2 emissions, GDP, population, "
         "and per capita metrics, highlighting the pressing need for affluent nations to balance "
         "prosperity with environmental stewardship and support global efforts to address "
         "climate change.")

# ---------------------------------------------------------------------------
# low-level PBIR helpers
# ---------------------------------------------------------------------------
def L(v):            return {"expr": {"Literal": {"Value": v}}}
def solid(c):        return {"solid": {"color": {"expr": {"Literal": {"Value": "'%s'" % c}}}}}
def col(p):          return {"Column":  {"Expression": {"SourceRef": {"Entity": E}}, "Property": p}}
def meas(p):         return {"Measure": {"Expression": {"SourceRef": {"Entity": E}}, "Property": p}}

def proj(field, native, active=True):
    d = {"field": field, "queryRef": "%s.%s" % (E, native), "nativeQueryRef": native}
    if active:
        d["active"] = True
    return d

def pos(x, y, w, h, z):
    return {"x": x, "y": y, "z": z, "height": h, "width": w, "tabOrder": z}

def container(name, position, visual, filterConfig=None):
    d = {"$schema": SCHEMA, "name": name, "position": position, "visual": visual}
    if filterConfig:
        d["filterConfig"] = filterConfig
    return d

def obj(props, selector=None):
    d = {"properties": props}
    if selector:
        d["selector"] = selector
    return d

def hide(*names):
    return {n: [obj({"show": L("false")})] for n in names}

def run(text, size, color, bold=False):
    ts = {"fontSize": size, "color": color}
    if bold:
        ts["fontWeight"] = "bold"
    return {"value": text, "textStyle": ts}

def para(runs, align=None):
    p = {"textRuns": runs}
    if align:
        p["horizontalTextAlignment"] = align
    return p

# ---------------------------------------------------------------------------
# visual builders
# ---------------------------------------------------------------------------
def textbox(name, p, paragraphs):
    v = {"visualType": "textbox", "drillFilterOtherVisuals": True,
         "objects": {"general": [obj({"paragraphs": paragraphs})]},
         "visualContainerObjects": {"background": [obj({"show": L("false")})],
                                     "border": [obj({"show": L("false")})]}}
    return container(name, p, v)

def shape(name, p, tile, fill):
    v = {"visualType": "shape", "drillFilterOtherVisuals": True,
         "objects": {
             "shape":   [obj({"tileShape": L("'%s'" % tile)})],
             "fill":    [obj({"fillColor": solid(fill), "transparency": L("0D"), "show": L("true")},
                             {"id": "default"})],
             "outline": [obj({"show": L("false")}, {"id": "default"})],
         },
         "visualContainerObjects": {"background": [obj({"show": L("false")})],
                                    "border": [obj({"show": L("false")})]}}
    return container(name, p, v)

def area(name, p, measure, title, units):
    v = {"visualType": "areaChart", "drillFilterOtherVisuals": True,
         "query": {"queryState": {
             "Category": {"projections": [proj(col("Year"), "Year")]},
             "Y":        {"projections": [proj(meas(measure), measure)]},
         }},
         "objects": {
             "categoryAxis": [obj({"show": L("true"), "showAxisTitle": L("false"),
                                   "fontSize": L("8D"), "labelColor": solid("#C8C8D2"),
                                   "gridlineShow": L("false")})],
             "valueAxis":    [obj({"show": L("true"), "showAxisTitle": L("false"),
                                   "fontSize": L("8D"), "labelColor": solid("#C8C8D2"),
                                   "labelDisplayUnits": L("%dD" % units),
                                   "gridlineShow": L("false")})],
             "legend":    [obj({"show": L("false")})],
             "dataPoint": [obj({"fill": solid("#9A9AAE")})],
         },
         "visualContainerObjects": {
             "title": [obj({"show": L("true"), "text": L("'%s'" % title),
                            "fontSize": L("10D"), "bold": L("true"),
                            "fontColor": solid("#ECECF2"), "heading": L("'Heading3'")})],
             "background": [obj({"show": L("false")})],
             "border": [obj({"show": L("false")})],
             "padding": [obj({"top": L("2D"), "bottom": L("2D"), "left": L("2D"), "right": L("2D")})],
         }}
    return container(name, p, v)

def _map_controls():
    return [obj({"defaultStyle": L("'grayscale_light'"),
                 "showStylePicker": L("false"),
                 "showNavigationControls": L("false"),
                 "showSelectionControl": L("false"),
                 "autoZoom": L("true")})]

NE50M_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
             "geojson/ne_50m_admin_0_countries.geojson")

def azuremap(name, p):
    # Data-bound REFERENCE LAYER choropleth (geocoding-independent): match Country Code
    # (ISO A3) to the GeoJSON's ISO_A3_EH property; color polygons categorically by
    # Continent (Legend). See MS Learn power-bi-visual-add-reference-layer (2026-07-19):
    # geocoded layers (bubble/marker) won't render without a Data Category on the
    # location field, but the data-bound reference layer works via property matching.
    v = {"visualType": "azureMap", "drillFilterOtherVisuals": True,
         "query": {"queryState": {
             "Category": {"projections": [proj(col("Country Code"), "Country Code")]},
             "Series":   {"projections": [proj(col("Continent"), "Continent")]},
             "Tooltips": {"projections": [
                 proj(meas("Avg GDP per Capita"), "Avg GDP per Capita", active=False),
                 proj(meas("Avg CO2 per Capita"), "Avg CO2 per Capita", active=False),
                 proj(meas("Total Population"),   "Total Population",   active=False),
             ]},
         }},
         "objects": {
             "mapControls": [obj({"defaultStyle": L("'grayscale_light'"),
                                  "showStylePicker": L("false"),
                                  "showNavigationControls": L("false"),
                                  "showSelectionControl": L("false"),
                                  "autoZoom": L("true")})],
             "bubbleLayer": [obj({"show": L("false")})],
             "referenceLayer": [obj({
                 "show": L("true"),
                 "datasourceType": L("'url'"),
                 "referenceLayerUrl": L("'%s'" % NE50M_URL),
                 "unmappedObjectVisibility": L("false"),
                 "polygonStrokeWidth": L("1L"),
             })],
         },
         "visualContainerObjects": {
             "title": [obj({"show": L("false")})],
             "background": [obj({"show": L("false")})],
             "border": [obj({"show": L("false")})],
             "padding": [obj({"top": L("0D"), "bottom": L("0D"), "left": L("0D"), "right": L("0D")})],
         }}
    return container(name, p, v)

def _axis(role_props):
    return [obj(role_props)]

def region_scatter(name, p):
    v = {"visualType": "scatterChart", "drillFilterOtherVisuals": True,
         "query": {"queryState": {
             "Category": {"projections": [proj(col("Continent"), "Continent")]},
             "Series":   {"projections": [proj(col("Continent"), "Continent")]},
             "X":        {"projections": [proj(meas("Avg CO2 per Capita"), "Avg CO2 per Capita")]},
             "Y":        {"projections": [proj(meas("Avg GDP per Capita"), "Avg GDP per Capita")]},
             "Size":     {"projections": [proj(meas("Total Population"), "Total Population")]},
         }},
         "objects": {
             "categoryAxis": [obj({"show": L("true"), "start": L("0D"), "showAxisTitle": L("true"),
                                   "titleText": L("'CO2'"), "fontSize": L("9D")})],
             "valueAxis":    [obj({"show": L("true"), "start": L("0D"), "showAxisTitle": L("true"),
                                   "titleText": L("'GDP'"), "labelDisplayUnits": L("1000D"),
                                   "fontSize": L("9D")})],
             "categoryLabels": [obj({"show": L("true"), "fontSize": L("9D"), "color": solid("#555555")})],
             "legend": [obj({"show": L("false")})],
         },
         "visualContainerObjects": {
             "title": [obj({"show": L("false")})],
             "background": [obj({"show": L("false")})],
             "border": [obj({"show": L("false")})],
             "padding": [obj({"top": L("0D"), "bottom": L("0D"), "left": L("0D"), "right": L("0D")})],
         }}
    return container(name, p, v)

def _continent_filter(continent):
    return {"filters": [{
        "name": "Filter_%s" % continent.replace(" ", ""),
        "field": col("Continent"),
        "type": "Categorical",
        "filter": {"Version": 2,
                   "From": [{"Name": "c", "Entity": E, "Type": 0}],
                   "Where": [{"Condition": {"In": {
                       "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "c"}},
                                                   "Property": "Continent"}}],
                       "Values": [[{"Literal": {"Value": "'%s'" % continent}}]]}}}]},
        "howCreated": "User"}]}

def panel_scatter(name, p, continent):
    c = CONT[continent]
    v = {"visualType": "scatterChart", "drillFilterOtherVisuals": True,
         "query": {"queryState": {
             "Category": {"projections": [proj(col("Country Name"), "Country Name")]},
             "X":        {"projections": [proj(meas("Avg CO2 per Capita"), "Avg CO2 per Capita")]},
             "Y":        {"projections": [proj(meas("Avg GDP per Capita"), "Avg GDP per Capita")]},
             "Size":     {"projections": [proj(meas("Total Population"), "Total Population")]},
         }},
         "objects": {
             "dataPoint": [obj({"fill": solid(c["color"])},
                               {"data": [{"dataViewWildcard": {"matchingOption": 0}}]})],
             "categoryAxis": [obj({"show": L("true"), "start": L("0D"), "end": L("%dD" % c["xend"]),
                                   "showAxisTitle": L("true"), "titleText": L("'CO2'"),
                                   "fontSize": L("8D"), "labelColor": solid("#666666")})],
             "valueAxis":    [obj({"show": L("true"), "start": L("0D"), "showAxisTitle": L("true"),
                                   "titleText": L("'GDP'"), "labelDisplayUnits": L("1000D"),
                                   "fontSize": L("8D"), "labelColor": solid("#666666")})],
             "categoryLabels": [obj({"show": L("true"), "fontSize": L("7D"), "color": solid("#555555")})],
             "legend": [obj({"show": L("false")})],
         },
         "visualContainerObjects": {
             "title": [obj({"show": L("true"), "text": L("'%s 2020'" % continent),
                            "fontSize": L("10D"), "bold": L("true"), "fontColor": solid("#333333")})],
             "background": [obj({"show": L("true"), "color": solid(c["bg"]), "transparency": L("0D")})],
             "border": [obj({"show": L("false")})],
             "padding": [obj({"top": L("2D"), "bottom": L("2D"), "left": L("2D"), "right": L("2D")})],
         }}
    return container(name, p, v, filterConfig=_continent_filter(continent))

def year_slicer(name, p):
    v = {"visualType": "slicer", "drillFilterOtherVisuals": True,
         "query": {"queryState": {"Values": {"projections": [proj(col("Year"), "Year")]}}},
         "objects": {
             "selection": [obj({"singleSelect": L("true"), "selectAllCheckboxEnabled": L("false")})],
             "header":    [obj({"show": L("false")})],
             "data":      [obj({"mode": L("'Dropdown'")})],
             "general":   [obj({"filter": {"filter": {
                 "Version": 2,
                 "From": [{"Name": "c", "Entity": E, "Type": 0}],
                 "Where": [{"Condition": {"In": {
                     "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "c"}},
                                                 "Property": "Year"}}],
                     "Values": [[{"Literal": {"Value": "2020L"}}]]}}}]}}})],
         },
         "visualContainerObjects": {
             "background": [obj({"show": L("false")})],
             "border": [obj({"show": L("false")})],
         }}
    return container(name, p, v)

# ---------------------------------------------------------------------------
# ELEMENTS: id, layer, x, y, w, h, z, builder
#   layer 'bg' -> excluded from content-overlap audit (intentional backdrop)
# ---------------------------------------------------------------------------
def build_all():
    els = []
    def add(id, layer, x, y, w, h, z, vis):
        els.append({"id": id, "layer": layer, "x": x, "y": y, "w": w, "h": h, "z": z, "vis": vis})

    # backdrops
    add("sidebarBg", "bg", 0, 0, 341, 900, 100, shape("sidebarBg", pos(0, 0, 341, 900, 100), "rectangle", "#333333"))
    add("divider", "bg", 20, 182, 300, 3, 150, shape("divider", pos(20, 182, 300, 3, 150), "rectangle", "#8E8EA5"))
    add("yearBoxBg", "bg", 1090, 270, 266, 152, 140, shape("yearBoxBg", pos(1090, 270, 266, 152, 140), "rectangle", "#FFFFFF"))
    add("sizeBubble", "bg", 905, 344, 78, 66, 141, shape("sizeBubble", pos(905, 344, 78, 66, 141), "oval", "#DADADD"))

    # sidebar content
    add("infoIcon", "c", 286, 14, 42, 42, 210,
        textbox("infoIcon", pos(286, 14, 42, 42, 210),
                [para([run("\u24d8", "16pt", "#C8C8D2")], align="center")]))
    add("titleBox", "c", 20, 18, 256, 158, 211,
        textbox("titleBox", pos(20, 18, 256, 158, 211), [
            para([run("The Price of Prosperity", "24pt", "#F2F2F7", bold=True)]),
            para([run("CO2 Emissions, GDP and Population Trends Across Time", "11pt", "#C8C8D2")]),
        ]))
    add("narrative", "c", 20, 192, 300, 286, 211,
        textbox("narrative", pos(20, 192, 300, 286, 211), [
            para([run(PARA1, "8pt", "#C2C2CE")]),
            para([run("", "5pt", "#C2C2CE")]),
            para([run(PARA2, "8pt", "#C2C2CE")]),
        ]))
    add("areaGdp", "c", 16, 486, 312, 126, 220, area("areaGdp", pos(16, 486, 312, 126, 220), "Avg GDP per Capita", "Avg GDP/Capita ($)", 1000))
    add("areaCo2", "c", 16, 616, 312, 126, 221, area("areaCo2", pos(16, 616, 312, 126, 221), "Avg CO2 per Capita", "Avg CO2 Emissions/Capita (metric tons)", 0))
    add("areaPop", "c", 16, 748, 312, 126, 222, area("areaPop", pos(16, 748, 312, 126, 222), "Total Population", "Total Population", 1000000000))

    # main top
    add("mapTitle", "c", 348, 8, 526, 44, 300,
        textbox("mapTitle", pos(348, 8, 526, 44, 300), [
            para([run("World Map and Regions", "14pt", "#333333", bold=True)]),
            para([run("Select a Country to filter / highlight the dashboard", "10pt", "#666666")]),
        ]))
    add("mapWorld", "c", 345, 54, 528, 370, 310, azuremap("mapWorld", pos(345, 54, 528, 370, 310)))
    add("regionTitle", "c", 886, 8, 470, 44, 320,
        textbox("regionTitle", pos(886, 8, 470, 44, 320), [
            para([run("Average GDP vs CO2 per Capita 2020, by Region", "13pt", "#333333", bold=True)]),
            para([run("Select a Region to filter / highlight the dashboard", "10pt", "#666666")]),
        ]))
    add("regionScatter", "c", 886, 54, 470, 212, 330, region_scatter("regionScatter", pos(886, 54, 470, 212, 330)))
    add("sizeLegend", "c", 886, 278, 190, 64, 345,
        textbox("sizeLegend", pos(886, 278, 190, 64, 345), [
            para([run("Charts are sized by:", "9pt", "#666666")]),
            para([run("Total Population", "10pt", "#333333", bold=True)]),
        ]))
    add("yearLabel", "c", 1100, 274, 248, 34, 350,
        textbox("yearLabel", pos(1100, 274, 248, 34, 350),
                [para([run("Select Year", "10pt", "#333333", bold=True)])]))
    add("yearSlicer", "c", 1100, 312, 248, 50, 351, year_slicer("yearSlicer", pos(1100, 312, 248, 50, 351)))

    # bottom 3x2 grid
    grid = [
        ("panelAfrica",   "Africa",        345, 429, 332, 226),
        ("panelEurope",   "Europe",        683, 429, 332, 226),
        ("panelOceania",  "Oceania",      1021, 429, 335, 226),
        ("panelNAmerica", "North America", 345, 661, 332, 226),
        ("panelAsia",     "Asia",          683, 661, 332, 226),
        ("panelSAmerica", "South America",1021, 661, 335, 226),
    ]
    z = 400
    for pid, cont, x, y, w, h in grid:
        add(pid, "c", x, y, w, h, z, panel_scatter(pid, pos(x, y, w, h, z), cont))
        z += 1
    return els

# ---------------------------------------------------------------------------
def audit(els):
    content = [e for e in els if e["layer"] == "c"]
    problems = []
    for i in range(len(content)):
        for j in range(i + 1, len(content)):
            a, b = content[i], content[j]
            if (a["x"] < b["x"] + b["w"] and a["x"] + a["w"] > b["x"] and
                    a["y"] < b["y"] + b["h"] and a["y"] + a["h"] > b["y"]):
                problems.append((a["id"], b["id"]))
    for e in els:
        if e["x"] < 0 or e["y"] < 0 or e["x"] + e["w"] > PAGE_W or e["y"] + e["h"] > PAGE_H:
            problems.append(("OUT_OF_BOUNDS", e["id"]))
    print("elements: %d  (content: %d, backdrop: %d)" % (len(els), len(content), len(els) - len(content)))
    print("%-16s %5s %5s %5s %5s %6s" % ("id", "x", "y", "w", "h", "z"))
    for e in sorted(els, key=lambda e: (e["y"], e["x"])):
        print("%-16s %5d %5d %5d %5d %6d" % (e["id"], e["x"], e["y"], e["w"], e["h"], e["z"]))
    if problems:
        print("\nSPACE_AUDIT: %d problem(s):" % len(problems))
        for p in problems:
            print("  overlap/oob:", p)
    else:
        print("\nSPACE_AUDIT: CLEAN (no content overlaps, all in-bounds)")
    return not problems

def wireframe(els):
    from PIL import Image, ImageDraw
    scale = 1.0
    img = Image.new("RGB", (PAGE_W, PAGE_H), "#F5F5FA")
    d = ImageDraw.Draw(img)
    fills = {
        "sidebarBg": "#333333", "divider": "#8E8EA5", "yearBoxBg": "#FFFFFF", "sizeBubble": "#DADADD",
    }
    for e in sorted(els, key=lambda e: e["z"]):
        x, y, w, h = e["x"], e["y"], e["w"], e["h"]
        fid = e["id"]
        if fid in CONT_BG:
            fill = CONT_BG[fid]
        else:
            fill = fills.get(fid, None)
        outline = "#B0B0C0"
        if fid == "sizeBubble":
            d.ellipse([x, y, x + w, y + h], fill=fill, outline=outline)
        else:
            d.rectangle([x, y, x + w, y + h], fill=fill, outline=outline)
        label = e["id"]
        d.text((x + 3, y + 2), label, fill="#F5F5FA" if fid == "sidebarBg" else "#202020")
    img.save(os.path.join(HERE, "wireframe.png"))
    print("wrote", os.path.join(HERE, "wireframe.png"))

CONT_BG = {
    "panelAfrica": "#F3FAF9", "panelEurope": "#F0F3FA", "panelOceania": "#F9F3EF",
    "panelNAmerica": "#F0F7FA", "panelAsia": "#FBF7FB", "panelSAmerica": "#FEFAF1",
    "mapWorld": "#E8EEF2", "regionScatter": "#FFFFFF",
}

def emit(els):
    n = 0
    for e in els:
        folder = os.path.join(VIS_DIR, e["id"])
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "visual.json"), "w", encoding="utf-8") as f:
            json.dump(e["vis"], f, indent=2)
        n += 1
    print("emitted %d visual.json files to %s" % (n, VIS_DIR))

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "audit"
    els = build_all()
    if mode == "audit":
        ok = audit(els)
        sys.exit(0 if ok else 1)
    elif mode == "wireframe":
        wireframe(els)
    elif mode == "emit":
        ok = audit(els)
        if not ok:
            print("REFUSING to emit: space audit failed")
            sys.exit(1)
        emit(els)
    else:
        print("unknown mode", mode)
        sys.exit(2)
