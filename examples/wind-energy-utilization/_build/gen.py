#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate all PBIR visual.json files for the wind-energy-utilization overview page.
Copies render-verified shapes (superstore/airline/eea/cookbook) — no hand-guessed JSON.
Output strictly inside migrations\\wind-energy-utilization\\fabric\\WindEnergyUtilization.Report."""
import json, os, hashlib, shutil
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

REP = str(_REPO / "migrations" / "wind-energy-utilization" / "fabric" / "WindEnergyUtilization.Report")
PAGE = "overview"
VIS = os.path.join(REP, "definition", "pages", PAGE, "visuals")
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.11.0/schema.json"

# ---- palette ----
NAVY="'#29263D'"; BLUE="'#5E79BC'"; TEAL="'#76B7B2'"; LIGHT="'#C1CAE0'"
GRID="'#E3E3E8'"; WHITE="'#FFFFFF'"; SUB="'#6B7280'"; INK="'#2B2B3A'"; ACCENT="'#118DFF'"

# ---- literal helpers ----
def lit(v): return {"expr":{"Literal":{"Value":v}}}
def solid(color_literal): return {"solid":{"color":{"expr":{"Literal":{"Value":color_literal}}}}}
def b(x): return lit("true" if x else "false")
def num(n): return lit(f"{n}D")

def colref(e,p): return {"Column":{"Expression":{"SourceRef":{"Entity":e}},"Property":p}}
def measref(e,p): return {"Measure":{"Expression":{"SourceRef":{"Entity":e}},"Property":p}}
def aggref(e,p,f): return {"Aggregation":{"Expression":{"Column":{"Expression":{"SourceRef":{"Entity":e}},"Property":p}},"Function":f}}
AGGNAME={0:"Sum",1:"Avg",2:"Max",3:"Min",4:"Count"}

def pc(e,p,active=True):
    d={"field":colref(e,p),"queryRef":f"{e}.{p}","nativeQueryRef":p}
    if active: d["active"]=True
    return d
def pm(e,p,active=False):
    d={"field":measref(e,p),"queryRef":f"{e}.{p}","nativeQueryRef":p}
    if active: d["active"]=True
    return d
def pa(e,p,f,active=False):
    d={"field":aggref(e,p,f),"queryRef":f"{AGGNAME[f]}({e}.{p})","nativeQueryRef":p}
    if active: d["active"]=True
    return d

def name(seed): return hashlib.md5(seed.encode()).hexdigest()[:20]
def fname(seed): return hashlib.md5(("flt::"+seed).encode()).hexdigest()[:20]

def base(key,x,y,w,h,z,vtype):
    return {"$schema":SCHEMA,"name":name(key),
            "position":{"x":x,"y":y,"z":z,"height":h,"width":w,"tabOrder":z},
            "visual":{"visualType":vtype,"drillFilterOtherVisuals":True}}

def vco(title=None,panel=False,pad=0,title_size=11,title_color=NAVY,title_bold=True):
    o={}
    if title is not None:
        o["title"]=[{"properties":{"show":b(True),"text":lit("'"+title.replace("'","")+"'"),
                     "fontSize":num(title_size),"bold":b(title_bold),
                     "fontColor":solid(title_color)}}]
    else:
        o["title"]=[{"properties":{"show":b(False)}}]
    if panel:
        o["background"]=[{"properties":{"show":b(True),"color":solid(WHITE),"transparency":num(0)}}]
        o["border"]=[{"properties":{"show":b(True),"color":solid(GRID),"radius":num(6)}}]
    else:
        o["background"]=[{"properties":{"show":b(False)}}]
        o["border"]=[{"properties":{"show":b(False)}}]
    o["padding"]=[{"properties":{"top":num(pad),"bottom":num(pad),"left":num(pad),"right":num(pad)}}]
    return o

# ============================================================ emitters
def textbox(key,x,y,w,h,paras,z=500):
    v=base(key,x,y,w,h,z,"textbox")
    P=[]
    for para in paras:
        runs=[]
        align=para.get("align","left")
        for r in para["runs"]:
            ts={"fontFamily":r.get("font","Segoe UI"),"fontSize":f"{r.get('size',11)}pt","color":r.get("color","#29263D")}
            if r.get("bold"): ts["fontWeight"]="bold"
            runs.append({"value":r["text"],"textStyle":ts})
        P.append({"horizontalTextAlignment":align,"textRuns":runs})
    v["visual"]["objects"]={"general":[{"properties":{"paragraphs":P}}]}
    v["visual"]["visualContainerObjects"]={"padding":[{"properties":{"top":num(0),"bottom":num(0),"left":num(0),"right":num(0)}}]}
    return v

def card(key,x,y,w,h,e,m,label,vsize=24,vcolor=NAVY,units=0,precision=None,panel=True,z=1000):
    v=base(key,x,y,w,h,z,"cardVisual")
    v["visual"]["query"]={"queryState":{"Data":{"projections":[pm(e,m)]}}}
    valprops={"fontSize":num(vsize),"bold":b(True),"fontColor":solid(vcolor),
              "labelDisplayUnits":num(units)}
    if precision is not None: valprops["labelPrecision"]=num(precision)
    v["visual"]["objects"]={
        "value":[{"properties":valprops,"selector":{"id":"default"}}],
        "label":[{"properties":{"show":b(True),"text":lit("'"+label+"'"),"fontSize":num(9),
                  "fontColor":solid(SUB)},"selector":{"id":"default"}}],
        "outline":[{"properties":{"show":b(False)},"selector":{"id":"default"}}]}
    v["visual"]["visualContainerObjects"]=vco(None,panel=panel,pad=4)
    return v

def slicer(key,x,y,w,h,e,col,default,title,alias,z=4000):
    v=base(key,x,y,w,h,z,"slicer")
    v["visual"]["query"]={"queryState":{"Values":{"projections":[pc(e,col)]}}}
    v["visual"]["objects"]={
        "general":[{"properties":{"filter":{"filter":{"Version":2,
            "From":[{"Name":alias,"Entity":e,"Type":0}],
            "Where":[{"Condition":{"In":{"Expressions":[{"Column":{"Expression":{"SourceRef":{"Source":alias}},"Property":col}}],
                      "Values":[[{"Literal":{"Value":"'"+default+"'"}}]]}}}]}}}}],
        "selection":[{"properties":{"singleSelect":b(True),"selectAllCheckboxEnabled":b(False)}}],
        "data":[{"properties":{"mode":lit("'Dropdown'")}}],
        "header":[{"properties":{"show":b(False)}}],
        "items":[{"properties":{"textSize":num(9),"padding":num(2)}}]}
    v["visual"]["visualContainerObjects"]={
        "title":[{"properties":{"show":b(True),"text":lit("'"+title+"'"),"fontSize":num(9),
                  "bold":b(True),"fontColor":solid(NAVY)}}],
        "background":[{"properties":{"show":b(True),"color":solid(WHITE),"transparency":num(0)}}],
        "border":[{"properties":{"show":b(True),"color":solid(GRID),"radius":num(6)}}],
        "padding":[{"properties":{"top":num(2),"bottom":num(2),"left":num(4),"right":num(4)}}]}
    return v

def spiral(key,x,y,w,h,z=16000):
    v=base(key,x,y,w,h,z,"scatterChart")
    v["visual"]["query"]={"queryState":{
        "Category":{"projections":[pc("NL Densification","Num")]},
        "X":{"projections":[pm("NL Densification","Spiral X",active=True)]},
        "Y":{"projections":[pm("NL Densification","Spiral Y")]}}}
    v["visual"]["objects"]={
        "dataPoint":[{"properties":{"defaultColor":solid(TEAL),
            "fill":{"solid":{"color":{"expr":{"Literal":{"Value":TEAL}}}}}},
            "selector":{"data":[{"dataViewWildcard":{"matchingOption":0}}]}}],
        "categoryAxis":[{"properties":{"show":b(False),"showAxisTitle":b(False)}}],
        "valueAxis":[{"properties":{"show":b(False),"showAxisTitle":b(False)}}],
        "categoryLabels":[{"properties":{"show":b(False)}}],
        "legend":[{"properties":{"show":b(False)}}]}
    v["visual"]["visualContainerObjects"]=vco(None,panel=True,pad=6)
    # visual-level measure filter: Spiral Filter > 0  (clip to selected-turbine perf ratio)
    fn=fname(key+"::spiralfilter")
    v["filterConfig"]={"filters":[{
        "name":fn,"field":measref("NL Densification","Spiral Filter"),"type":"Advanced",
        "filter":{"Version":2,"From":[{"Name":"n","Entity":"NL Densification","Type":0}],
            "Where":[{"Condition":{"Comparison":{"ComparisonKind":1,
                "Left":{"Measure":{"Expression":{"SourceRef":{"Source":"n"}},"Property":"Spiral Filter"}},
                "Right":{"Literal":{"Value":"0D"}}}}}]},
        "howCreated":"User"}]}
    return v

def wind_scatter(key,x,y,w,h,z=17000):
    v=base(key,x,y,w,h,z,"scatterChart")
    v["visual"]["query"]={"queryState":{
        "Category":{"projections":[pc("Daily Performance","Date")]},
        "X":{"projections":[pa("Daily Performance","Max Wind Speed Ms",1,active=True)]},
        "Y":{"projections":[pa("Daily Performance","Energy Actual Mwh",0)]}}}
    v["visual"]["objects"]={
        "dataPoint":[{"properties":{"defaultColor":solid(BLUE)},
            "selector":{"data":[{"dataViewWildcard":{"matchingOption":0}}]}}],
        "categoryAxis":[{"properties":{"show":b(True),"showAxisTitle":b(True),"fontSize":num(8),"titleText":lit("'Max Wind Speed (m/s)'")}}],
        "valueAxis":[{"properties":{"show":b(True),"showAxisTitle":b(True),"fontSize":num(8),"titleText":lit("'Daily Output (MWh)'")}}],
        "categoryLabels":[{"properties":{"show":b(False)}}],
        "legend":[{"properties":{"show":b(False)}}],
        "xAxisReferenceLine":[{"properties":{"show":b(True),"displayName":lit("'Avg wind'"),
            "value":num(15.322),"lineColor":solid(NAVY),"style":lit("'dashed'"),
            "width":num(1),"dataLabelShow":b(False)}}]}
    v["visual"]["visualContainerObjects"]=vco(None,panel=True,pad=6)
    return v

def column_bars(key,x,y,w,h,z=6000):
    v=base(key,x,y,w,h,z,"clusteredColumnChart")
    v["visual"]["query"]={"queryState":{
        "Category":{"projections":[pc("Daily Performance","Month")]},
        "Y":{"projections":[pm("Daily Performance","Total Actual Output (2024)",active=True)]},
        "Tooltips":{"projections":[pm("Daily Performance","Selected Month (Bars)")]}}}
    smb="Daily Performance.Selected Month (Bars)"
    cond={"Conditional":{"Cases":[
        {"Condition":{"Comparison":{"ComparisonKind":0,
            "Left":{"SelectRef":{"ExpressionName":smb}},
            "Right":{"Literal":{"Value":"'Selected Month'"}}}},
         "Value":{"Literal":{"Value":TEAL}}},
        {"Condition":{"Comparison":{"ComparisonKind":0,
            "Left":{"SelectRef":{"ExpressionName":smb}},
            "Right":{"Literal":{"Value":"'Annual'"}}}},
         "Value":{"Literal":{"Value":BLUE}}}],
        "Else":{"Value":{"Literal":{"Value":LIGHT}}}}}
    v["visual"]["objects"]={
        "dataPoint":[{"properties":{"fill":{"solid":{"color":{"expr":cond}}}},
            "selector":{"data":[{"dataViewWildcard":{"matchingOption":0}}]}}],
        "categoryAxis":[{"properties":{"show":b(True),"fontSize":num(8),"showAxisTitle":b(False)}}],
        "valueAxis":[{"properties":{"show":b(False),"showAxisTitle":b(False)}}],
        "legend":[{"properties":{"show":b(False)}}],
        "labels":[{"properties":{"show":b(False)}}]}
    v["visual"]["visualContainerObjects"]=vco(None,panel=True,pad=4)
    return v

def azuremap(key,x,y,w,h,z=5000):
    v=base(key,x,y,w,h,z,"azureMap")
    v["visual"].pop("drillFilterOtherVisuals",None)
    v["visual"]["query"]={"queryState":{
        "Category":{"projections":[pc("Turbine","Upd Turbine Name")]},
        "Y":{"projections":[pa("Turbine","Latitude",1)]},
        "X":{"projections":[pa("Turbine","Longitude",1)]},
        "Size":{"projections":[pm("Turbine","CM Total Capacity")]}}}
    v["visual"]["objects"]={
        "mapControls":[{"properties":{"defaultStyle":lit("'road'"),"autoZoom":b(True),
            "showLabels":b(True),"worldWrap":b(False)},"selector":{"id":"default"}}],
        "bubbleLayer":[{"properties":{"show":b(True),"bubbleRadius":lit("6L"),
            "fillColor":solid(BLUE)},"selector":{"id":"default"}}],
        "pathLayer":[{"properties":{"show":b(False)},"selector":{"id":"default"}}]}
    v["visual"]["visualContainerObjects"]=vco("Fleet Turbine Map",panel=True,pad=0,title_size=10)
    v["visual"]["drillFilterOtherVisuals"]=True
    return v

def table_list(key,x,y,w,h,z=2000):
    v=base(key,x,y,w,h,z,"tableEx")
    v["visual"]["query"]={"queryState":{"Values":{"projections":[
        pc("Turbine","Upd Turbine Name"),
        pm("Daily Performance","CM Total Output",active=True)]}}}
    v["visual"]["sortDefinition"]={"sort":[{"field":measref("Daily Performance","CM Total Output"),
        "direction":"Descending"}],"isDefaultSort":True}
    v["visual"]["objects"]={
        "values":[{"properties":{"fontSize":num(8)},"selector":{"id":"default"}}],
        "columnHeaders":[{"properties":{"fontSize":num(8),"bold":b(True)},"selector":{"id":"default"}}],
        "grid":[{"properties":{"gridVertical":b(False),"gridHorizontal":b(True),
            "rowPadding":num(3),"textSize":num(8)},"selector":{"id":"default"}}],
        "columnFormatting":[{"properties":{"dataBars":{
            "positiveColor":solid(BLUE),"negativeColor":solid("'#D13438'"),
            "axisColor":solid(GRID),"reverseDirection":{"expr":{"Literal":{"Value":"false"}}},
            "hideText":{"expr":{"Literal":{"Value":"false"}}}}},
            "selector":{"metadata":"Daily Performance.CM Total Output"}}]}
    v["visual"]["visualContainerObjects"]=vco(None,panel=True,pad=4)
    return v

def table_perf(key,x,y,w,h,topn_dir,title,z=2100):
    # topn_dir: 2=Descending(highest), 1=Ascending(lowest)
    v=base(key,x,y,w,h,z,"tableEx")
    v["visual"]["query"]={"queryState":{"Values":{"projections":[
        pc("Turbine","Upd Turbine Name"),
        pm("Daily Performance","CM Total Output",active=True)]}}}
    v["visual"]["sortDefinition"]={"sort":[{"field":measref("Daily Performance","CM Total Output"),
        "direction":"Descending" if topn_dir==2 else "Ascending"}],"isDefaultSort":True}
    v["visual"]["objects"]={
        "values":[{"properties":{"fontSize":num(8)},"selector":{"id":"default"}}],
        "columnHeaders":[{"properties":{"fontSize":num(8),"bold":b(True)},"selector":{"id":"default"}}],
        "grid":[{"properties":{"gridVertical":b(False),"gridHorizontal":b(True),
            "rowPadding":num(3),"textSize":num(8)},"selector":{"id":"default"}}]}
    v["visual"]["visualContainerObjects"]=vco(title,panel=True,pad=4,title_size=9)
    fn=fname(key+"::topn")
    v["filterConfig"]={"filters":[{
        "name":fn,"field":colref("Turbine","Upd Turbine Name"),"type":"TopN",
        "filter":{"Version":2,"From":[
            {"Name":"t","Entity":"Turbine","Type":0},
            {"Name":"d","Entity":"Daily Performance","Type":0}],
            "Where":[{"Condition":{"TopN":{
                "Expressions":[{"Column":{"Expression":{"SourceRef":{"Source":"t"}},"Property":"Upd Turbine Name"}}],
                "OrderBy":[{"Direction":topn_dir,"Expression":{"Measure":{"Expression":{"SourceRef":{"Source":"d"}},"Property":"CM Total Output"}}}]}}}]},
        "howCreated":"User","itemCount":3}]}
    return v

def matrix_heatmap(key,x,y,w,h,z=3000):
    v=base(key,x,y,w,h,z,"pivotTable")
    v["visual"]["query"]={"queryState":{
        "Rows":{"projections":[pc("Daily Performance","Weekday")]},
        "Columns":{"projections":[pc("Daily Performance","Month")]},
        "Values":{"projections":[pm("Daily Performance","Total Actual Output (2024)",active=True)]}}}
    qr="Daily Performance.Total Actual Output (2024)"
    fillrule={"solid":{"color":{"expr":{"FillRule":{
        "Input":{"SelectRef":{"ExpressionName":qr}},
        "FillRule":{"linearGradient3":{
            "min":{"color":{"Literal":{"Value":"'#EAF0F9'"}},"value":{"Literal":{"Value":"0D"}}},
            "mid":{"color":{"Literal":{"Value":TEAL}}},
            "max":{"color":{"Literal":{"Value":NAVY}}},
            "nullColoringStrategy":{"strategy":{"Literal":{"Value":"'asZero'"}}}}}}}}}}
    v["visual"]["objects"]={
        "values":[
            {"properties":{"fontSize":num(8)},"selector":{"id":"default"}},
            {"properties":{"backColor":fillrule},
             "selector":{"data":[{"dataViewWildcard":{"matchingOption":1}}],"metadata":qr}}],
        "columnHeaders":[{"properties":{"fontSize":num(8),"bold":b(True)},"selector":{"id":"default"}}],
        "rowHeaders":[{"properties":{"fontSize":num(8)},"selector":{"id":"default"}}],
        "grid":[{"properties":{"textSize":num(8),"rowPadding":num(1)},"selector":{"id":"default"}}],
        "subTotals":[{"properties":{"rowSubtotals":b(False),"columnSubtotals":b(False)}}]}
    v["visual"]["visualContainerObjects"]=vco(None,panel=True,pad=4)
    return v

# ============================================================ LAYOUT
V=[]
def add(v,tag): V.append((tag,v))

# ---------- HEADER ----------
add(textbox("h_title",36,16,560,50,[
    {"runs":[{"text":"NL Wind Energy Utilization","size":20,"color":"#29263D","bold":True}]},
    {"runs":[{"text":"GRWF Renewable Fleet — Daily Performance 2024","size":10,"color":"#6B7280"}]}]),"header")
add(card("h_month_view",36,72,320,44,"Daily Performance","Month in View","VIEWING",vsize=16,vcolor=BLUE,panel=False),"header")
add(card("h_today",1196,18,168,46,"Turbine","Today","DATA AS OF",vsize=13,vcolor=NAVY,panel=True),"header")
add(slicer("s_month",380,116,245,50,"Month Parameter","Month","June","MONTH","mp"),"header")
add(slicer("s_turbine",635,116,270,50,"Upd Turbine Name Parameter","Upd Turbine Name","GRWF Turbine 18","TURBINE","up"),"header")
add(slicer("s_region",915,116,210,50,"Region Parameter","Region","Flevoland","REGION","rp"),"header")
add(slicer("s_turbineid",1135,116,225,50,"Turbine Id Parameter","Turbine Id","NL-WT-2024-001","TURBINE ID","tp"),"header")

# ---------- KPI BAND ----------
add(card("k_no_turbines",36,176,156,90,"Turbine","No of Turbines","TURBINES",vsize=30),"kpi")
add(card("k_active",200,176,160,90,"Turbine","Active Turbines","ACTIVE",vsize=30,vcolor=TEAL),"kpi")
add(card("k_onshore",36,274,156,90,"Turbine","Onshore Turbines","ONSHORE",vsize=30),"kpi")
add(card("k_co2",200,274,160,90,"CO2 Savings","CM CO2 Saved (Tn)","CO2 SAVED (t)",vsize=24,vcolor=TEAL),"kpi")
add(card("k_cm_output",372,176,300,86,"Daily Performance","CM Total Output","TOTAL OUTPUT (MWh)",vsize=30,vcolor=NAVY),"kpi")
add(column_bars("total_output_bars",372,270,300,94),"kpi")
add(card("k_max_output",684,176,330,58,"Daily Performance","Max Monthly Output","BEST MONTH (MWh)",vsize=18,vcolor=TEAL),"kpi")
add(table_perf("highest_perf",684,242,330,122,2,"Highest Performers"),"kpi")
add(card("k_min_output",1026,176,338,58,"Daily Performance","Min Monthly Output","LOWEST MONTH (MWh)",vsize=18,vcolor=BLUE),"kpi")
add(table_perf("lowest_perf",1026,242,338,122,1,"Lowest Performers"),"kpi")

# ---------- MAIN BODY ----------
# left column: turbine list
add(textbox("m_list_title",36,378,324,40,[
    {"runs":[{"text":"All Turbines — by Total Output","size":11,"color":"#29263D","bold":True}]},
    {"runs":[{"text":"Ranked high → low (MWh)","size":8,"color":"#6B7280"}]}]),"body")
add(table_list("turbine_list",36,424,324,480),"body")
# middle column: selected-turbine detail + map
add(card("m_sel_turbine",372,378,300,48,"Upd Turbine Name Parameter","Upd Turbine Name Parameter Value","SELECTED TURBINE",vsize=15,vcolor=NAVY),"body")
add(card("m_t_output",372,432,148,62,"Daily Performance","T CM Total Output","OUTPUT (MWh)",vsize=15,vcolor=BLUE),"body")
add(card("m_t_capfactor",524,432,148,62,"Daily Performance","T CM Capacity Factor","CAP. FACTOR",vsize=15,vcolor=TEAL),"body")
add(azuremap("map",372,500,300,404),"body")
# right column: heatmap + spiral + wind scatter
add(textbox("m_heat_title",684,378,680,34,[
    {"runs":[{"text":"Daily Output Heatmap","size":11,"color":"#29263D","bold":True},
             {"text":"   Weekday × Month — fleet total actual output (2024)","size":8,"color":"#6B7280"}]}]),"body")
add(matrix_heatmap("turbine_heatmap",684,416,680,176),"body")
add(card("m_t_perfratio",684,596,330,48,"Daily Performance","T CM Performance Ratio","PERFORMANCE RATIO",vsize=15,vcolor=TEAL),"body")
add(textbox("m_spiral_legend",684,648,330,22,[
    {"runs":[{"text":"● Performance Spiral — length ∝ selected turbine ratio","size":8,"color":"#6B7280"}]}]),"body")
add(spiral("spiral",684,672,264,232),"body")
add(textbox("m_wind_title",1026,596,338,34,[
    {"runs":[{"text":"Wind Speed vs. Power Output","size":11,"color":"#29263D","bold":True}]}]),"body")
add(wind_scatter("wind_vs_output",1026,634,338,270),"body")

# ============================================================ SPACE AUDIT
def audit(items):
    rects=[]
    for tag,v in items:
        p=v["position"]; rects.append((v["name"],tag,p["x"],p["y"],p["width"],p["height"]))
    overl=[]
    EPS=0.5
    for i in range(len(rects)):
        for j in range(i+1,len(rects)):
            a=rects[i]; c=rects[j]
            ax,ay,aw,ah=a[2],a[3],a[4],a[5]; cx,cy,cw,ch=c[2],c[3],c[4],c[5]
            ox=max(0,min(ax+aw,cx+cw)-max(ax,cx))
            oy=max(0,min(ay+ah,cy+ch)-max(ay,cy))
            if ox>EPS and oy>EPS:
                overl.append((a[0],c[0],round(ox,1),round(oy,1)))
    oob=[r for r in rects if r[2]<0 or r[3]<0 or r[2]+r[4]>1400.5 or r[3]+r[5]>960.5]
    return overl,oob,rects

overl,oob,rects=audit(V)
print(f"VISUALS: {len(V)}")
print(f"OVERLAPS: {len(overl)}")
for o in overl: print("  !! overlap",o)
print(f"OUT-OF-BOUNDS: {len(oob)}")
for r in oob: print("  !! oob",r)

if not overl and not oob:
    # clean visuals dir then write
    if os.path.isdir(VIS): shutil.rmtree(VIS)
    os.makedirs(VIS,exist_ok=True)
    seen=set()
    for tag,v in V:
        nm=v["name"]
        assert nm not in seen, f"DUP visual name {nm}"
        seen.add(nm)
        d=os.path.join(VIS,nm); os.makedirs(d,exist_ok=True)
        json.dump(v,open(os.path.join(d,"visual.json"),"w",encoding="utf-8"),indent=2)
    print(f"WROTE {len(V)} visuals to {VIS}")
else:
    print("NOT WRITING — fix overlaps/oob first")
