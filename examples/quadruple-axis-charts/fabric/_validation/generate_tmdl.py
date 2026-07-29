"""
TMDL generator for the '10 Ways to Make Quadruple-Axis Charts' Fabric semantic model.

Hand-authors the .SemanticModel/definition TMDL (no live Desktop/MCP available),
mirroring the sibling Superstore / Tale-of-100 models and the pbi-semantic-builder
gotchas (single-line DAX, bare `database`, explicit M cultures, name-vs-sourceColumn
discipline, measure/column namespace safety, bracketed field-param sourceColumn).

Design
------
* 1 import fact table `Orders` (Sample Superstore extract, 9994 rows, 0 Tableau joins).
* NO Date table: this workbook has no time-intelligence / prior-period logic; a marked
  Date table would impose cross-filtering the source never had AND risk the DATESBETWEEN
  axis-wiping trap. Date bucketing is done with plain calc columns on Orders instead
  (Ship Month / Order Quarter) — which is exactly what the window table-calcs address over.
* 4 disconnected parameter tables (Select Highlight Function / Profit Ratio Goal /
  Select Hashing / Variable Metric) with `<name> Value` SELECTEDVALUE measures.
* Every one of the 71 Tableau calculated fields is dispositioned (see DISPOSITION):
  2 become calc columns (row-level), the rest measures; a handful of pure visual-layout
  "fake extra axis" tricks (constant anchors, bar-in-bar offset) are modeled as trivial
  constants and flagged as a report-layer / Power-BI-dual-axis-capability boundary.

The multi-axis techniques
-------------------------
Tableau fakes 3rd/4th/5th/6th axes by stacking measures on the Rows/Columns shelf
(dual-axis) plus constant "anchor" placeholders (MAX(1.0)/MAX(0.0)/0) on extra axes,
then layering unicode SHAPE MARKS (⬤ ⚪ △ ▯ ★ ◔◑◕◉ hatching) driven by table
calculations. Power BI combo charts support at most a DUAL axis, so the *data* behind
every mark is modeled here (as measures with grounded window addressing), while the
literal N-axis stacking is left to the report layer and documented as a capability bound.
"""
import os, uuid, sys
sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))            # ...\fabric
MODEL = os.path.join(BASE, "QuadrupleAxisCharts.SemanticModel")
DEFN = os.path.join(MODEL, "definition")
TABLES = os.path.join(DEFN, "tables")
DATA_FOLDER = os.path.abspath(os.path.join(BASE, "..", "data")) + "\\"           # ...\data\
NS = uuid.UUID("7c2b4d10-9a00-4e00-8b00-000000000000")

def lt(*parts): return str(uuid.uuid5(NS, "|".join(parts)))
def esc_m(s):   return s.replace('"', '""')
def one_line(s):
    return " ".join(str(s).split())

MTYPE = {"string": "type text", "int64": "Int64.Type", "double": "type number",
         "dateTime": "type date"}

O = "'Orders'"   # shorthand

# ============================================================================
#  Physical columns:  (display, source_csv, dataType, hidden, summarizeBy, desc)
# ============================================================================
PHYS_COLS = [
    ("Row ID", "Row ID", "int64", True, "none",
     "Surrogate row identifier from the Tableau extract (one per order line, 9994 rows). Hidden; not a business attribute."),
    ("Order ID", "Order ID", "string", False, "none",
     "Order identifier; 5009 distinct orders. One order spans multiple line-item rows in this fact table."),
    ("Order Date", "Order Date", "dateTime", False, "none",
     "Date the order was placed (day grain). Basis for the Order Quarter helper and the quarter-addressed East/West window table-calcs."),
    ("Ship Date", "Ship Date", "dateTime", False, "none",
     "Date the order shipped (day grain). Basis for Days to Ship, On Time Ship?, and the Ship Month helper the Sales window table-calcs address over."),
    ("Ship Mode", "Ship Mode", "string", False, "none",
     "Shipping service level (a.k.a. shipping method). One of: First Class, Same Day, Second Class, Standard Class."),
    ("Customer ID", "Customer ID", "string", False, "none",
     "Customer identifier; 793 distinct customers. Pairs with Customer Name."),
    ("Customer Name", "Customer Name", "string", False, "none",
     "Customer full name; 793 distinct. Basis of the Customer Count variable metric (distinct count)."),
    ("Segment", "Segment", "string", False, "none",
     "Customer market segment. One of: Consumer, Corporate, Home Office."),
    ("Country", "Country", "string", False, "none",
     "Country of the order. Single value in this extract: United States."),
    ("City", "City", "string", False, "none",
     "Customer city; 531 distinct."),
    ("State", "State", "string", False, "none",
     "US state of the order (a.k.a. province); 49 distinct. Addressed by the INDEX() map tile-grid table-calcs (Map Rows / Map Columns)."),
    # Postal Code kept as STRING to preserve leading-zero ZIPs (no calc uses it arithmetically).
    ("Postal Code", "Postal Code", "string", False, "none",
     "US ZIP code, kept as text to preserve leading zeros; 631 distinct. No calculation uses it numerically."),
    ("Region", "Region", "string", False, "none",
     "US sales region. One of: Central, East, South, West. The East/West window table-calcs filter on this."),
    ("Product ID", "Product ID", "string", False, "none",
     "Product identifier; 1862 distinct."),
    ("Category", "Category", "string", False, "none",
     "Product category. One of: Furniture, Office Supplies, Technology."),
    ("Sub-Category", "Sub-Category", "string", False, "none",
     "Product sub-category within Category. One of: Accessories, Appliances, Art, Binders, Bookcases, Chairs, Copiers, Envelopes, Fasteners, Furnishings, Labels, Machines, Paper, Phones, Storage, Supplies, Tables."),
    ("Product Name", "Product Name", "string", False, "none",
     "Product name; 1850 distinct. Basis of the Product Count variable metric (distinct count)."),
    ("Sales", "Sales", "double", False, "sum",
     "Line-item sales amount in USD (a.k.a. revenue; additive, range 0.44 to 22638.48). Base of [Total Sales] and the Sales window table-calcs."),
    ("Quantity", "Quantity", "int64", False, "sum",
     "Units sold on the line (additive; 1 to 14)."),
    ("Discount", "Discount", "double", False, "sum",
     "Discount rate applied to the line (0 to 0.80, i.e. 0% to 80%); percent-formatted."),
    ("Profit", "Profit", "double", False, "sum",
     "Line-item profit in USD (a.k.a. margin; additive, range -6599.98 to 8399.98). Base of [Total Profit] and the profit window / profit-ratio table-calcs."),
    # F22 = Ship Date + ~7 years; opaque Excel artifact, referenced by NO calc/worksheet.
    ("F22", "F22", "dateTime", True, "none",
     "Opaque source column (= Ship Date + ~2556 days); an Excel export artifact referenced by no calculation or worksheet. Hidden, retained for lineage."),
]

# ============================================================================
#  Calculated columns (row-level, no aggregation): (name, dax, dataType, hidden, summ, fmt, desc)
# ============================================================================
CALC_COLS = [
    ("Days to Ship",
     f"DATEDIFF({O}[Order Date], {O}[Ship Date], DAY)",
     "int64", False, "none", "#,##0",
     "Calendar days between order and shipment (Ship Date minus Order Date); row-level integer. From Tableau [Days to Ship]."),
    ("On Time Ship?",
     (f"({O}[Ship Mode] = \"Same Day\" && {O}[Days to Ship] = 0) || "
      f"({O}[Ship Mode] = \"First Class\" && {O}[Days to Ship] <= 2) || "
      f"({O}[Ship Mode] = \"Second Class\" && {O}[Days to Ship] <= 4) || "
      f"({O}[Ship Mode] = \"Standard Class\" && {O}[Days to Ship] <= 5)"),
     "boolean", False, "none", None,
     "Whether the line shipped within its ship-mode SLA (Same Day 0d, First Class 2d, Second Class 4d, Standard Class 5d); row-level TRUE/FALSE. From Tableau [On Time Ship?]."),
    # --- window-addressing helper buckets (grounded by worksheet encodings) ---
    ("Ship Month",
     f"DATE(YEAR({O}[Ship Date]), MONTH({O}[Ship Date]), 1)",
     "dateTime", True, "none", None,
     "Ship Date truncated to the first of its month (month grain); hidden helper axis the Sales window table-calcs address over. Tableau tmn:Ship Date axis."),
    ("Order Quarter",
     f"DATE(YEAR({O}[Order Date]), (QUARTER({O}[Order Date]) - 1) * 3 + 1, 1)",
     "dateTime", True, "none", None,
     "Order Date truncated to the first of its quarter (quarter grain); hidden helper axis the East/West and 5th/6th-axis window table-calcs address over. Tableau tqr:Order Date axis."),
]

# ============================================================================
#  Window-mark (Select Highlight Function) DAX builder — single-line, VAR-based,
#  measure references (clean context transition). Faithfully preserves the copy-paste
#  WINDOW_STDEV(SUM([Sales])) quirk on the '+' control band of the Profit / Profit-Ratio
#  windows (sp_meas differs from sm_meas) — see limitations.
# ============================================================================
def window_mark(axis, e_meas, glyph_dax, sp_meas, sm_meas):
    A = f"{O}[{axis}]"
    return (
        f"VAR P = [Select Highlight Function Value] "
        f"VAR Cur = {e_meas} "
        f"VAR WMax = MAXX(ALLSELECTED({A}), {e_meas}) "
        f"VAR WMin = MINX(ALLSELECTED({A}), {e_meas}) "
        f"VAR WAvg = AVERAGEX(ALLSELECTED({A}), {e_meas}) "
        f"VAR SPlus = STDEVX.S(ALLSELECTED({A}), {sp_meas}) "
        f"VAR SMinus = STDEVX.S(ALLSELECTED({A}), {sm_meas}) "
        f"VAR IsLast = MAX({A}) = CALCULATE(MAX({A}), ALLSELECTED({A})) "
        f"RETURN IF(P = \"All\" || (P = \"Max\" && Cur = WMax) || (P = \"Min\" && Cur = WMin) || "
        f"(P = \"Last\" && IsLast) || (P = \"Max & Min\" && (Cur = WMax || Cur = WMin)) || "
        f"(P = \"Control\" && (Cur > WAvg + SPlus * 2 || Cur < WAvg - SMinus * 2)), {glyph_dax})"
    )

HASH_DEFAULT = "\u2571\u2571\u2571"  # ╱╱╱

# ============================================================================
#  Measures.  (name, dax, fmt|None, folder, hidden, desc)
# ============================================================================
DOLLAR = "\"$\"#,##0.00"
PCT = "0.0%"
NUM = "#,##0"

M = []   # measures on Orders

# ---- base helpers (hidden; reused across window DAX so context transition is clean) ----
M += [
 ("Total Sales", f"SUM({O}[Sales])", DOLLAR, "Base (hidden helpers)", True,
  "Hidden helper = SUM(Sales); reused by window table-calc measures for clean context transition."),
 ("Total Profit", f"SUM({O}[Profit])", DOLLAR, "Base (hidden helpers)", True,
  "Hidden helper = SUM(Profit)."),
 ("Total Quantity", f"SUM({O}[Quantity])", NUM, "Base (hidden helpers)", True,
  "Hidden helper = SUM(Quantity)."),
 ("Order Row Count", f"COUNTROWS({O})", NUM, "Base (hidden helpers)", True,
  "Hidden helper = COUNT of order-line rows (Tableau COUNT([Orders]) / Number of Records)."),
]

# ---- #12 Profit Ratio, #24 Order Count ----
M += [
 ("Profit Ratio", f"DIVIDE([Total Profit], [Total Sales])", PCT, "Base", False,
  "Tableau [Profit Ratio] = SUM(Profit)/SUM(Sales)."),
 ("Order Count", f"DISTINCTCOUNT({O}[Order ID])", NUM, "Base", False,
  "Tableau [Order Count] = COUNTD([Order ID])."),
]

# ---- Region split (#25 #66 #58 #67) — KEEPFILTERS = row-level IF, does not override outer ----
M += [
 ("Sales - East", f"CALCULATE([Total Sales], KEEPFILTERS({O}[Region] = \"East\"))", DOLLAR, "Region Split (East/West)", False,
  "Tableau [Sales - East] = SUM(IF [Region]='East' THEN [Sales] END). KEEPFILTERS matches the row-level IF (restrict, not override)."),
 ("Sales - West", f"CALCULATE([Total Sales], KEEPFILTERS({O}[Region] = \"West\"))", DOLLAR, "Region Split (East/West)", False,
  "Tableau [Sales - West] = SUM(IF [Region]='West' THEN [Sales] END)."),
 ("Profit - East", f"CALCULATE([Total Profit], KEEPFILTERS({O}[Region] = \"East\"))", DOLLAR, "Region Split (East/West)", False,
  "Tableau [Profit - East] = SUM(IF [Region]='East' THEN [Profit] END)."),
 ("Profit - West", f"CALCULATE([Total Profit], KEEPFILTERS({O}[Region] = \"West\"))", DOLLAR, "Region Split (East/West)", False,
  "Tableau [Profit - West] = SUM(IF [Region]='West' THEN [Profit] END)."),
]

# ---- Variable Metric (param 5) (#71 #38 #5) ----
def vm(region_pred_id=None):
    if region_pred_id is None:
        oc = f"DISTINCTCOUNT({O}[Order ID])"
        cc = f"DISTINCTCOUNT({O}[Customer ID])"
        pc = f"DISTINCTCOUNT({O}[Product ID])"
    else:
        k = f"KEEPFILTERS({O}[Region] = \"{region_pred_id}\")"
        oc = f"CALCULATE(DISTINCTCOUNT({O}[Order ID]), {k})"
        cc = f"CALCULATE(DISTINCTCOUNT({O}[Customer ID]), {k})"
        pc = f"CALCULATE(DISTINCTCOUNT({O}[Product ID]), {k})"
    return (f"SWITCH([Variable Metric Value], \"- Order Count\", {oc}, "
            f"\"- Customer Count\", {cc}, \"- Product Count\", {pc})")
M += [
 ("Variable Metric COUNTD", vm(), NUM, "Variable Metric (Param)", False,
  "Tableau [Variable Metric COUNTD] = CASE [Parameter 5] WHEN '- Order Count' COUNTD([Order ID]) ... Param-driven COUNTD."),
 ("Variable Metric - East", vm("East"), NUM, "Variable Metric (Param)", False,
  "Tableau [Variable Metric - East] = CASE [Parameter 5] ... COUNTD(IF [Region]='East' THEN key END)."),
 ("Variable Metric - West", vm("West"), NUM, "Variable Metric (Param)", False,
  "Tableau [Variable Metric - West] = CASE [Parameter 5] ... COUNTD(IF [Region]='West' THEN key END). (Internal name Ctrl-drag-scrambled to '5th Axis Metric - East (copy)'; resolved via caption.)"),
]

# ---- LOD (#22 #62 #33 #23 #34 #68) ----
SUBCAT, REGION, SHIPM = f"{O}[Sub-Category]", f"{O}[Region]", f"{O}[Ship Mode]"
M += [
 ("Profit SUM by SubCat, Region, Ship Mode",
  f"CALCULATE([Total Profit], ALLEXCEPT({O}, {SUBCAT}, {REGION}, {SHIPM}))",
  DOLLAR, "LOD", False,
  "Tableau LOD {FIXED [Sub-Category],[Region],[Ship Mode]:SUM([Profit])} -> CALCULATE + ALLEXCEPT."),
 ("Profit MAX (SUM by SubCat, Region, Ship Mode)",
  f"CALCULATE(MAXX(VALUES({SHIPM}), CALCULATE([Total Profit])), ALLEXCEPT({O}, {SUBCAT}, {REGION}))",
  DOLLAR, "LOD", False,
  "Tableau nested LOD {FIXED [Sub-Category],[Region]:MAX({FIXED ...,[Ship Mode]:SUM([Profit])})} -> MAXX over ship modes of each mode's fixed profit sum, scoped to Sub-Category+Region."),
 ("Ship Mode - Most Profitable LOD",
  (f"CALCULATE(MAXX(FILTER(VALUES({SHIPM}), "
   f"[Profit SUM by SubCat, Region, Ship Mode] = [Profit MAX (SUM by SubCat, Region, Ship Mode)]), "
   f"{SHIPM}), ALLEXCEPT({O}, {REGION}, {SUBCAT}))"),
  None, "LOD", False,
  "Tableau LOD {FIXED [Region],[Sub-Category]: MAX(IF fixedSum=fixedMax THEN [Ship Mode] END)} -> the ship-mode name whose FIXED profit sum equals the group max (MAX(text)=last alphabetically)."),
 ("Ship Mode - Most Profitable",
  (f"IF([Profit SUM by SubCat, Region, Ship Mode] = [Profit MAX (SUM by SubCat, Region, Ship Mode)], "
   f"SELECTEDVALUE({SHIPM}))"),
  None, "LOD", False,
  "Tableau [Ship Mode - Most Profitable] = IF fixedSum=fixedMax THEN [Ship Mode]. Viz-LOD version (Ship Mode on the row axis) -> SELECTEDVALUE."),
 ("Ship Mode - Most Profitable Icon",
  ("SWITCH([Ship Mode - Most Profitable LOD], \"Same Day\", \"+\uFE0F\", \"First Class\", \"\u25A2\", "
   "\"Second Class\", \"\u25A3\", \"Standard Class\", \"\u25A4\uFE0F\")"),
  None, "Ship Mode (LOD)", False,
  "Tableau [Ship Mode - Most Profitable Icon] = CASE [Ship Mode - Most Profitable LOD] -> unicode icon."),
 ("Ship Mode - Most Profitable Short",
  ("SWITCH([Ship Mode - Most Profitable LOD], \"Same Day\", \"Same\uFE0F\", \"First Class\", \"1st\uFE0F\", "
   "\"Second Class\", \"2nd\", \"Standard Class\", \"Std\uFE0F\")"),
  None, "Ship Mode (LOD)", False,
  "Tableau [Ship Mode - Most Profitable Short] = CASE [Ship Mode - Most Profitable LOD] -> short label."),
]

# ---- On Time / Late / Exclude-Ship (#57 #7 #55 #56) ----
ONTIME_COND = (f"({O}[Ship Mode] = \"Same Day\" && {O}[Days to Ship] = 0) || "
               f"({O}[Ship Mode] = \"First Class\" && {O}[Days to Ship] <= 2) || "
               f"({O}[Ship Mode] = \"Second Class\" && {O}[Days to Ship] <= 4) || "
               f"({O}[Ship Mode] = \"Standard Class\" && {O}[Days to Ship] <= 5)")
M += [
 ("On Time Ship %",
  f"AVERAGEX({O}, IF({ONTIME_COND}, 1))",
  PCT, "On Time / Late", False,
  "Tableau [On Time Ship %] = AVG(IF <on-time> THEN 1 END). Faithful: AVERAGEX ignores blanks exactly like Tableau AVG (source has no ELSE 0, so on a pure on-time context this trends to 1 — a source quirk, preserved). See limitations."),
 ("Late %",
  (f"DIVIDE(CALCULATE([Order Row Count], KEEPFILTERS({O}[On Time Ship?] = FALSE())), "
   f"CALCULATE([Order Row Count], REMOVEFILTERS({O}[On Time Ship?])))"),
  PCT, "On Time / Late", False,
  "Tableau [Late %] = (late COUNT)/TOTAL(COUNT) gated by IF NOT ATTR([On Time Ship?]). Late-record share of the pane. GROUND-TRUTH target."),
 ("Not Profitable Circle - Exclude Ship",
  (f"IF(SELECTEDVALUE({O}[On Time Ship?]) = TRUE(), "
   f"CALCULATE(IF(NOT([Profitable?]), \"\u25EF\"), REMOVEFILTERS({O}[On Time Ship?])))"),
  None, "On Time / Late", False,
  "Tableau LOD IF [On Time Ship?] THEN {EXCLUDE [On Time Ship?]: IF NOT [Profitable?] THEN '\u25EF'}. EXCLUDE -> REMOVEFILTERS. Pie label-centering trick; report-layer cosmetic. See limitations."),
 ("Profitable - Exclude Ship",
  f"CALCULATE([Total Profit], REMOVEFILTERS({O}[On Time Ship?])) > 0",
  None, "On Time / Late", False,
  "Tableau LOD {EXCLUDE [On Time Ship?]: [Profitable?]} -> profit>0 ignoring the on-time split."),
]

# ---- Booleans + simple shape marks (#10 #11 #13 #14 #63 #36 #61 #35) ----
M += [
 ("Profitable?", f"[Total Profit] > 0", None, "Shape / Symbol Marks", False,
  "Tableau [Profitable?] = SUM(Profit)>0."),
 ("Not Profitable Circle", f"IF(NOT([Profitable?]), \"\u25EF\")", None, "Shape / Symbol Marks", False,
  "Tableau [Not Profitable Circle] = IF NOT [Profitable?] THEN '\u25EF'."),
 ("Low Volume", f"[Order Row Count] < 50", None, "Shape / Symbol Marks", False,
  "Tableau [Low Volume] = COUNT([Orders])<50."),
 ("Low Volume Triangle", f"IF([Low Volume], \"\u25B3\")", None, "Shape / Symbol Marks", False,
  "Tableau [Low Volume Triangle] = IF [Low Volume] THEN '\u25B3'."),
 ("Profitable Circle Color", f"SIGN([Total Profit])", NUM, "Shape / Symbol Marks", False,
  "Tableau [Profitable Circle Color] = SIGN(SUM(Profit))."),
 ("Profit Ratio + Symbol", f"IF([Profit Ratio] >= 0, \"+\")", None, "Shape / Symbol Marks", False,
  "Tableau [Profit Ratio + Symbol] = IF [Profit Ratio]>=0 THEN '+'."),
 ("Profit Ratio - Symbol", f"IF([Profit Ratio] < 0, \"\u2212\")", None, "Shape / Symbol Marks", False,
  "Tableau [Profit Ratio - Symbol] = IF [Profit Ratio]<0 THEN '\u2212'."),
 ("Profit Ratio Mark Highlight Color", f"NOT(ISBLANK([Dot Profit Ratio Window]))", None, "Shape / Symbol Marks", False,
  "Tableau [Profit Ratio Mark Highlight Color] = NOT ISNULL([Dot Profit Ratio Window]) -> transparent where no dot mark."),
 ("Highlight Dummy", "\"x\"", None, "Tableau Helpers", False,
  "Tableau [Highlight Dummy] = 'x'. Constant placeholder used on Detail/Text to force a single mark."),
]

# ---- Discount marks (#21 #51 #43 #44 #45 #46) ----
AVGDISC = f"AVERAGE({O}[Discount])"
M += [
 ("Low Discount Circle", f"IF(COALESCE({AVGDISC}, 0) <= 0.10, \"O\")", None, "Discount Marks", False,
  "Tableau [Low Discount Circle] = IF ZN(AVG([Discount]))<=0.10 THEN 'O'. ZN -> COALESCE(...,0)."),
 ("Mid Discount Circle", f"IF({AVGDISC} <= 0.25 && {AVGDISC} > 0.10, \"O\")", None, "Discount Marks", False,
  "Tableau [Mid Discount Circle] = IF AVG(Discount)<=0.25 AND >0.10 THEN 'O'."),
 ("Deep Discount?", f"{AVGDISC} > 0.25", None, "Discount Marks", False,
  "Tableau [Deep Discount?] = AVG(Discount)>0.25."),
 ("Deep Discount Circle", f"IF({AVGDISC} > 0.25, \"O\")", None, "Discount Marks", False,
  "Tableau [Deep Discount Circle] = IF AVG(Discount)>0.25 THEN 'O'."),
 ("Deep Discount Dot", f"IF({AVGDISC} > 0.25, \"\u2B24\")", None, "Discount Marks", False,
  "Tableau [Deep Discount Dot] = IF AVG(Discount)>0.25 THEN '\u2B24'."),
 ("Deep Discount? (Highlight)", f"IF({AVGDISC} > 0.25, 0.97)", "0.00", "Discount Marks", False,
  "Tableau [Deep Discount? (Highlight)] = IF AVG(Discount)>0.25 THEN 0.97 -> conditional-border anchor (report-layer trick)."),
]

# ---- Profit Ratio Goal (param 3) (#17 #59 #60 #8) ----
M += [
 ("Profit Ratio Over Goal?", f"[Profit Ratio] > [Profit Ratio Goal Value]", None, "Profit Ratio Goal", False,
  "Tableau [Profit Ratio Over Goal?] = [Profit Ratio] > [Parameter 3]."),
 ("Profit Ratio Over Goal Display", f"IF([Profit Ratio Over Goal?], [Profit Ratio])", PCT, "Profit Ratio Goal", False,
  "Tableau [Profit Ratio Over Goal Display] = IF [Over Goal?] THEN [Profit Ratio]."),
 ("Profit Ratio Under Goal Display", f"IF(NOT([Profit Ratio Over Goal?]), [Profit Ratio])", PCT, "Profit Ratio Goal", False,
  "Tableau [Profit Ratio Under Goal Display] = IF NOT [Over Goal?] THEN [Profit Ratio]."),
 ("Profit Ratio Spacer",
  ("LEFT(\"    \", 4 - (INT(LOG(ROUND(ABS([Profit Ratio]) * 100, 0), 10)) + IF([Profit Ratio] < 0, 1, 0)))"),
  None, "Profit Ratio Goal", False,
  "Tableau [Profit Ratio Spacer] = LEFT('    ',4-(INT(LOG(ROUND(ABS([Profit Ratio])*100),10))+IIF(...<0,1,0))). Cosmetic label-alignment spacer; report-layer. See limitations."),
]

# ---- Met Sales Goal (#52 #53 #54 #69) ----
M += [
 ("Met Sales Goal?", f"DIVIDE([Total Sales], DISTINCTCOUNT({O}[Order ID])) > 460", None, "Met Sales Goal", False,
  "Tableau [Met Sales Goal?] = SUM(Sales)/COUNTD([Order ID]) > 460."),
 ("Met Sales Goal Dot", f"IF([Met Sales Goal?], \"\u2B24 \")", None, "Met Sales Goal", False,
  "Tableau [Met Sales Goal Dot] = IF [Met Sales Goal?] THEN '\u2B24 '."),
 ("Met Sales Goal *", f"IF([Met Sales Goal?], \"*\", \" \")", None, "Met Sales Goal", False,
  "Tableau [Met Sales Goal *] = IF [Met Sales Goal?] THEN '*' ELSE ' '."),
 ("Met Sales Goal Check", f"IF([Met Sales Goal?], \"\u2713\")", None, "Met Sales Goal", False,
  "Tableau [Met Sales Goal Check] = IF [Met Sales Goal?] THEN '\u2713'."),
]

# ---- Ranking (#28 #29 #65 #64) — table calc RANK; addressing INFERRED (orphaned copies) ----
RANKX_VOL = f"RANKX(ALLSELECTED({SUBCAT}), [Order Row Count], , DESC, Dense)"
M += [
 ("Order Volume - Rank of Count", RANKX_VOL, NUM, "Ranking", False,
  "Tableau [Order Volume - Rank of Count] = RANK(COUNT([Orders])). Table calc -> RANKX. Addressing INFERRED over Sub-Category (the volume-bar axis). See limitations."),
 ("Rank Volume + - 1", f"IF([Order Volume - Rank of Count] = 1, \"+\")", None, "Ranking", False,
  "Tableau [Rank Volume + - 1] = IF rank=1 THEN '+'."),
 ("Rank Volume + - 2", f"IF([Order Volume - Rank of Count] = 2, \"+\")", None, "Ranking", False,
  "Tableau [Rank Volume + - 2] = IF rank=2 THEN '+'."),
 ("Rank Volume + - 3", f"IF([Order Volume - Rank of Count] = 3, \"+\")", None, "Ranking", False,
  "Tableau [Rank Volume + - 3] = IF rank=3 THEN '+'."),
 ("Star Rating",
  (f"LEFT(\"\u2605\u2605\u2605\u2605\u2605\", INT((MOD([Order Row Count], 10) + 1) / 2)) & "
   f"IF(MOD([Order Row Count], 2) = 1, \"\u2606\", \"\")"),
  None, "Ranking", False,
  "Tableau [Star Rating] = LEFT('\u2605\u2605\u2605\u2605\u2605',(COUNT%10+1)/2)+IF COUNT%2=1 THEN '\u2606'. Pseudo-random rating from row count."),
]

# ---- Indicator grid (random) (#1 #3 #4 #18 #2) ----
NL = "UNICHAR(10)"
BLK, WHT = "\u2B1B\uFE0F", "\U0001F532"   # ⬛️ , 🔲
def grid_row(mref):
    return (f"IF({mref} < 33, \"{BLK}\", \"{WHT}\") & "
            f"IF({mref} >= 33 && {mref} < 66, \"{BLK}\", \"{WHT}\") & "
            f"IF({mref} >= 66 && {mref} < 100, \"{BLK}\", \"{WHT}\")")
M += [
 ("3x3 row 1 random", f"MOD(INT([Total Sales]), 100)", NUM, "Indicator Grid (random)", False,
  "Tableau [3x3 row 1 random] = INT(SUM([Sales]))%100."),
 ("3x3 row 2 random", f"MOD(INT([Total Profit]), 100)", NUM, "Indicator Grid (random)", False,
  "Tableau [3x3 row 2 random] = INT(SUM([Profit]))%100."),
 ("3x3 row 3 random", f"MOD(INT([Total Quantity]), 100)", NUM, "Indicator Grid (random)", False,
  "Tableau [3x3 row 3 random] = INT(SUM([Quantity]))%100."),
 ("3x3 Indicator Grid",
  f"{grid_row('[3x3 row 1 random]')} & {NL} & {grid_row('[3x3 row 2 random]')} & {NL} & {grid_row('[3x3 row 3 random]')}",
  None, "Indicator Grid (random)", False,
  "Tableau [3x3 Indicator Grid] = 3 rows of \u2B1B/\U0001F532 indicators from the row-1/2/3 random measures, newline-separated."),
 ("1x9 Indicator Line",
  f"{grid_row('[3x3 row 1 random]')} & {grid_row('[3x3 row 2 random]')} & {grid_row('[3x3 row 3 random]')}",
  None, "Indicator Grid (random)", False,
  "Tableau [1x9  Indicator Line] = the 3x3 grid flattened onto one line (no newlines)."),
]

# ---- Map tile grid (#19 #20) — INDEX() over State; RANKX alphabetical. GROUND-TRUTH. ----
STATE = f"{O}[State]"
RANK_STATE = f"RANKX(ALLSELECTED({STATE}), {STATE}, , ASC, Dense)"
M += [
 ("Map Columns", f"MOD(INT({RANK_STATE} - 1), 10)", NUM, "Map Tile Grid", False,
  "Tableau [Map Columns] = INT(INDEX()-1)%10. INDEX() over State (alphabetical) -> RANKX. GROUND-TRUTH target."),
 ("Map Rows", f"INT(({RANK_STATE} - 1) / 10) * 1.0", "0.0", "Map Tile Grid", False,
  "Tableau [Map Rows] = INT((INDEX()-1)/10)*1.0. INDEX() over State -> RANKX. GROUND-TRUTH target."),
]

# ---- Volume Quartile (#32) — WINDOW_PERCENTILE of COUNT over the addressing (INFERRED subcat) ----
M += [
 ("Volume Quartile",
  (f"VAR C = [Order Row Count] "
   f"VAR Q1 = PERCENTILEX.INC(ALLSELECTED({SUBCAT}), [Order Row Count], 0.25) "
   f"VAR Q2 = PERCENTILEX.INC(ALLSELECTED({SUBCAT}), [Order Row Count], 0.50) "
   f"VAR Q3 = PERCENTILEX.INC(ALLSELECTED({SUBCAT}), [Order Row Count], 0.75) "
   f"RETURN IF(C <= Q1, \"\u25D4\", IF(C <= Q2, \"\u25D1\", IF(C <= Q3, \"\u25D5\", \"\u25C9\")))"),
  None, "Shape / Symbol Marks", False,
  "Tableau [Volume Quartile] = quartile of COUNT via WINDOW_PERCENTILE -> \u25D4\u25D1\u25D5\u25C9 marks. Addressing INFERRED over Sub-Category. See limitations."),
]

# ---- 5th/6th axis normalization (#26 #40 #39 #6 #41 #42 #27) — window over Order Quarter ----
OQ = "Order Quarter"
M += [
 ("Sales East/West Max",
  f"MAXX(ALLSELECTED({O}[{OQ}]), MAX([Sales - East], [Sales - West]))",
  DOLLAR, "5th/6th Axis (Normalization)", False,
  "Tableau [Sales East/West Max] = WINDOW_MAX(MAX([Sales-East],[Sales-West])) over Order Quarter. GROUND-TRUTH target."),
 ("Sales East/West Window Max",
  f"[Sales East/West Max]",
  DOLLAR, "5th/6th Axis (Normalization)", False,
  "Tableau auto-named field 'WINDOW_MAX(MAX([Sales - East],[Sales - West]))' — identical to [Sales East/West Max]; kept as an alias (renamed; bracketed caption is not a legal measure name)."),
 ("Axis Metric MAX East/West",
  f"MAX([Variable Metric - East], [Variable Metric - West])",
  NUM, "5th/6th Axis (Normalization)", False,
  "Tableau [5th/6th Axis Metric - MAX East/West] = MAX([Variable Metric-East],[Variable Metric-West]) (two-arg scalar MAX)."),
 ("Axis Metric East Normalized",
  f"DIVIDE([Variable Metric - East], MAXX(ALLSELECTED({O}[{OQ}]), [Variable Metric - East]))",
  "0.00", "5th/6th Axis (Normalization)", False,
  "Tableau '[5th/6th Axis Metric - East]/WINDOW_MAX([5th/6th Axis Metric - East])' -> East metric normalized by its window max (renamed; bracketed caption is not a legal measure name)."),
 ("5th/6th Axis Normalized - East",
  (f"DIVIDE([Sales East/West Max] * [Variable Metric - East], "
   f"MAXX(ALLSELECTED({O}[{OQ}]), MAX([Variable Metric - East], [Variable Metric - West])))"),
  "0.00", "5th/6th Axis (Normalization)", False,
  "Tableau [5th/6th Axis Normalized - East] = WINDOW_MAX(MAX Sales E/W) * [VM East] / WINDOW_MAX(MAX(VM East,VM West)). Sizes the East metric onto the shared Sales axis."),
 ("5th/6th Axis Normalized - West",
  (f"DIVIDE([Sales East/West Max] * [Variable Metric - West], "
   f"MAXX(ALLSELECTED({O}[{OQ}]), MAX([Variable Metric - East], [Variable Metric - West])))"),
  "0.00", "5th/6th Axis (Normalization)", False,
  "Tableau [5th/6th Axis Normalized - West] = WINDOW_MAX(MAX Sales E/W) * [VM West] / WINDOW_MAX(MAX(VM East,VM West)). (Internal names Ctrl-drag-scrambled; resolved via caption.)"),
 ("bar-in-bar negative offset",
  f"-0.05 * [Sales East/West Max]",
  DOLLAR, "5th/6th Axis (Normalization)", False,
  "Tableau [bar-in-bar negative offset] = -0.05*[Sales East/West Max]. Negative reference-line anchor for the bar-in-bar layout (report-layer trick)."),
]

# ---- Window highlight marks (param 2) (#9 #48 #47 #49 #50 #70) — grounded addressing ----
M += [
 ("Dot Sales Window",
  window_mark("Ship Month", "[Total Sales]", "\"\u2B24\"", "[Total Sales]", "[Total Sales]"),
  None, "Table Calc - Window Highlight Marks", False,
  "Tableau [Dot Sales Window]: highlight '\u2B24' where SUM(Sales) hits WINDOW Max/Min/Last/Control per [Select Highlight Function]. Window over Ship Month (grounded: Area+Dot+Line, Region=South)."),
 ("Circle Sales Window",
  window_mark("Ship Month", "[Total Sales]", "\"\u26AA\"", "[Total Sales]", "[Total Sales]"),
  None, "Table Calc - Window Highlight Marks", False,
  "Tableau [Circle Sales Window]: highlight '\u26AA' on SUM(Sales) window extremes. Window over Ship Month (grounded: Circle+Dot+Line, Region=South)."),
 ("Bar Profit Window",
  window_mark("Sub-Category", "[Total Profit]", "\"\u25AF\"", "[Total Sales]", "[Total Profit]"),
  None, "Table Calc - Window Highlight Marks", False,
  "Tableau [Bar Profit Window]: '\u25AF' on SUM(Profit) window extremes. Window over Sub-Category. NOTE: '+' control band uses WINDOW_STDEV(SUM([Sales])) not Profit — a copy-paste quirk, preserved. See limitations."),
 ("Triangles Profit Ratio Window",
  window_mark("Sub-Category", "[Profit Ratio]", "\"\u25B3\"", "[Total Sales]", "[Profit Ratio]"),
  None, "Table Calc - Window Highlight Marks", False,
  "Tableau [Triangles Profit Ratio Window]: '\u25B3' on [Profit Ratio] window extremes over Sub-Category. '+' band uses WINDOW_STDEV(SUM([Sales])) quirk, preserved."),
 ("Dot Profit Ratio Window",
  window_mark("Sub-Category", "[Profit Ratio]", "\"\u2B24\"", "[Total Sales]", "[Profit Ratio]"),
  None, "Table Calc - Window Highlight Marks", False,
  "Tableau [Dot Profit Ratio Window]: '\u2B24' on [Profit Ratio] window extremes over Sub-Category. '+' band uses WINDOW_STDEV(SUM([Sales])) quirk, preserved."),
 ("Hash Profit Ratio Window",
  window_mark("Sub-Category", "[Profit Ratio]", "[Hashing Display]", "[Total Sales]", "[Profit Ratio]"),
  None, "Table Calc - Window Highlight Marks", False,
  "Tableau [Hash Profit Ratio Window]: emits [Hashing Display] hatching on [Profit Ratio] window extremes over Sub-Category. '+' band uses WINDOW_STDEV(SUM([Sales])) quirk, preserved."),
]

# ---- Hashing Display (param 4) (#16) ----
def hashing_switch():
    pairs = {
        "   ": " ",
        "\u2571\u2571\u2571": "\u2571" * 25,
        "\u2572\u2572\u2572": "\u2572" * 25,
        "\u2551\u2551\u2551": "\u2551" * 23,
        "\u256C\u256C\u256C": "\u256C" * 25,
        "\u2593\u2593\u2593": "\u2593" * 30,
        "\u2592\u2592\u2592": "\u2592" * 30,
        "\u2591\u2591\u2591": "\u2591" * 30,
        "\u2590\u2590\u2590": "\u2590" * 30,
    }
    parts = ", ".join(f'"{esc_m(k)}", "{esc_m(v)}"' for k, v in pairs.items())
    return f"SWITCH([Select Hashing Value], {parts})"
M += [
 ("Hashing Display", hashing_switch(), None, "Shape / Symbol Marks", False,
  "Tableau [Hashing Display] = CASE [Parameter 4] mapping a 3-char hatch pick to a long repeated-glyph bar."),
]

# ---- Axis anchors (the literal fake-extra-axis trick) — constants, report-layer ----
M += [
 ("Axis Anchor One", "1", NUM, "Axis Anchors (layout)", False,
  "Fake-axis scaffolding: Tableau MAX(1.0) constant placed on a secondary axis (Highlight Table). Power BI supports at most a dual axis; provided as a constant for the report builder. Capability boundary — see limitations."),
 ("Axis Anchor Zero", "0", NUM, "Axis Anchors (layout)", False,
  "Fake-axis scaffolding: collapses Tableau's MAX(0.0)/0.0/0 constant anchors (L-Bar, On Time Pies, SubCat Bar) placed on secondary/tertiary axes to fabricate extra axes. Report-layer capability boundary — see limitations."),
]

# ============================================================================
#  Parameter tables
# ============================================================================
PARAM_SPECS = [
 dict(name="Select Highlight Function", col_type="string", col_fmt=None,
      values=["Max", "Min", "Max & Min", "Last", "Control", "All", "None"],
      default_dax="\"Max\"", vfmt=None,
      col_desc="Highlight-function selector (disconnected slicer) choosing which window marks are emphasized. One of: Max, Min, Max & Min, Last, Control, All, None.",
      desc="Disconnected slicer for Tableau parameter 'Select Highlight Function:' (internal [Parameter 2], list, default 'Max'). Drives every window highlight-mark measure."),
 dict(name="Profit Ratio Goal", col_type="double", col_fmt=PCT,
      values=[0, 0.05, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30], default_dax="0.12", vfmt=PCT,
      col_desc="Target profit-ratio goal (disconnected slicer, percent). Discrete approximation of the Tableau range parameter: 0%, 5%, 10%, 12% (default), 15%, 20%, 25%, 30%.",
      desc="Disconnected slicer for Tableau parameter 'Profit Ratio Goal' (internal [Parameter 3], real range, default 0.12). Range param approximated as a discrete list."),
 dict(name="Select Hashing", col_type="string", col_fmt=None,
      values=["   ", "\u2571\u2571\u2571", "\u2572\u2572\u2572", "\u2551\u2551\u2551",
              "\u256C\u256C\u256C", "\u2593\u2593\u2593", "\u2592\u2592\u2592",
              "\u2591\u2591\u2591", "\u2590\u2590\u2590"],
      default_dax="\"" + HASH_DEFAULT + "\"", vfmt=None,
      col_desc="Hatch/hash fill-pattern selector (disconnected slicer). Unicode glyph options (blank, diagonal, cross-hatch, shaded) feeding [Hashing Display]; default light diagonal.",
      desc="Disconnected slicer for Tableau parameter 'Select Hashing' (internal [Parameter 4], list of hatch patterns, default '\u2571\u2571\u2571'). Feeds [Hashing Display]."),
 dict(name="Variable Metric", col_type="string", col_fmt=None,
      values=["None", "- Order Count", "- Customer Count", "- Product Count"],
      default_dax="\"- Order Count\"", vfmt=None,
      col_desc="Variable count-metric selector (disconnected slicer) switching the distinct-count family. One of: None, - Order Count, - Customer Count, - Product Count.",
      desc="Disconnected slicer for Tableau parameter 'Variable Metric' (internal [Parameter 5], list, default '- Order Count'). Drives the Variable Metric COUNTD family."),
]

# ============================================================================
#  Coverage assertion: every one of the 71 spec calc captions must be dispositioned.
#  Maps spec caption -> modeled object name (measure/column) or a report-layer flag.
# ============================================================================
DISPOSITION = {
 # calc columns
 "Days to Ship": "col:Days to Ship", "On Time Ship?": "col:On Time Ship?",
 # measures (spec caption -> measure name)
 "Profit Ratio": "Profit Ratio", "Order Count": "Order Count",
 "Sales - West": "Sales - West", "Sales - East": "Sales - East",
 "Profit - West": "Profit - West", "Profit - East": "Profit - East",
 "Variable Metric COUNTD": "Variable Metric COUNTD", "Variable Metric - East": "Variable Metric - East",
 "Variable Metric - West": "Variable Metric - West",
 "Profit SUM by SubCat, Region, Ship Mode": "Profit SUM by SubCat, Region, Ship Mode",
 "Profit MAX (SUM by SubCat, Region, Ship Mode )": "Profit MAX (SUM by SubCat, Region, Ship Mode)",
 "Ship Mode - Most Profitable LOD": "Ship Mode - Most Profitable LOD",
 "Ship Mode - Most Profitable": "Ship Mode - Most Profitable",
 "Ship Mode - Most Profitable Icon": "Ship Mode - Most Profitable Icon",
 "Ship Mode - Most Profitable Short": "Ship Mode - Most Profitable Short",
 "On Time Ship %": "On Time Ship %", "Late %": "Late %",
 "Not Profitable Circle - Exclude Ship": "Not Profitable Circle - Exclude Ship",
 "Profitable - Exclude Ship": "Profitable - Exclude Ship",
 "Profitable?": "Profitable?", "Not Profitable Circle": "Not Profitable Circle",
 "Low Volume": "Low Volume", "Low Volume Triangle": "Low Volume Triangle",
 "Profitable Circle Color": "Profitable Circle Color",
 "Profit Ratio + Symbol": "Profit Ratio + Symbol", "Profit Ratio - Symbol": "Profit Ratio - Symbol",
 "Profit Ratio Mark Highlight Color": "Profit Ratio Mark Highlight Color",
 "Highlight Dummy": "Highlight Dummy",
 "Low Discount Circle": "Low Discount Circle", "Mid Discount Circle": "Mid Discount Circle",
 "Deep Discount?": "Deep Discount?", "Deep Discount Circle": "Deep Discount Circle",
 "Deep Discount Dot": "Deep Discount Dot", "Deep Discount? (Highlight)": "Deep Discount? (Highlight)",
 "Profit Ratio Over Goal?": "Profit Ratio Over Goal?",
 "Profit Ratio Over Goal Display": "Profit Ratio Over Goal Display",
 "Profit Ratio Under Goal Display": "Profit Ratio Under Goal Display",
 "Profit Ratio Spacer": "Profit Ratio Spacer",
 "Met Sales Goal?": "Met Sales Goal?", "Met Sales Goal Dot": "Met Sales Goal Dot",
 "Met Sales Goal *": "Met Sales Goal *", "Met Sales Goal Check": "Met Sales Goal Check",
 "Order Volume - Rank of Count": "Order Volume - Rank of Count",
 "Rank Volume + - 1": "Rank Volume + - 1", "Rank Volume + - 2": "Rank Volume + - 2",
 "Rank Volume + - 3": "Rank Volume + - 3", "Star Rating": "Star Rating",
 "3x3 row 1 random": "3x3 row 1 random", "3x3 row 2 random": "3x3 row 2 random",
 "3x3 row 3 random": "3x3 row 3 random", "3x3 Indicator Grid": "3x3 Indicator Grid",
 "1x9  Indicator Line": "1x9 Indicator Line",
 "Map Columns": "Map Columns", "Map Rows": "Map Rows", "Volume Quartile": "Volume Quartile",
 "Sales East/West Max": "Sales East/West Max",
 "WINDOW_MAX(MAX([Sales - East],[Sales - West]))": "Sales East/West Window Max",
 "[5th/6th Axis Metric - East]/WINDOW_MAX([5th/6th Axis Metric - East])": "Axis Metric East Normalized",
 "5th/6th Axis Metric - MAX East/West": "Axis Metric MAX East/West",
 "5th/6th Axis Normalized - East": "5th/6th Axis Normalized - East",
 "5th/6th Axis Normalized - West": "5th/6th Axis Normalized - West",
 "bar-in-bar negative offset": "bar-in-bar negative offset",
 "Dot Sales Window": "Dot Sales Window", "Circle Sales Window": "Circle Sales Window",
 "Bar Profit Window": "Bar Profit Window",
 "Triangles Profit Ratio Window": "Triangles Profit Ratio Window",
 "Dot Profit Ratio Window": "Dot Profit Ratio Window",
 "Hash Profit Ratio Window": "Hash Profit Ratio Window",
 "Hashing Display": "Hashing Display",
}

# ============================================================================
#  Business-meaning leads (AI/Copilot readiness). Prepended to the measure
#  description so Copilot reads plain meaning FIRST (first ~200 chars), with the
#  faithful Tableau-formula provenance retained after. Only the business-queryable
#  measures are listed; visual-encoding / glyph / window-highlight helpers keep their
#  own purpose descriptions (already meaning-bearing, not a formula dump).
# ============================================================================
MEANING_LEAD = {
 "Total Sales": "Total sales amount in USD.",
 "Total Profit": "Total profit in USD.",
 "Total Quantity": "Total units sold.",
 "Order Row Count": "Number of order-line rows.",
 "Profit Ratio": "Profit as a share of sales (margin percentage).",
 "Order Count": "Number of distinct orders.",
 "Sales - East": "Total sales for the East region.",
 "Sales - West": "Total sales for the West region.",
 "Profit - East": "Total profit for the East region.",
 "Profit - West": "Total profit for the West region.",
 "Variable Metric COUNTD": "Distinct count of orders, customers, or products, chosen by the Variable Metric slicer.",
 "Variable Metric - East": "East-region distinct count of orders, customers, or products, chosen by the Variable Metric slicer.",
 "Variable Metric - West": "West-region distinct count of orders, customers, or products, chosen by the Variable Metric slicer.",
 "Profit SUM by SubCat, Region, Ship Mode": "Profit totaled at the Sub-Category x Region x Ship Mode grain.",
 "Profit MAX (SUM by SubCat, Region, Ship Mode)": "Highest ship-mode profit within each Sub-Category x Region.",
 "Ship Mode - Most Profitable LOD": "The most profitable ship mode within each Region x Sub-Category.",
 "Ship Mode - Most Profitable": "The most profitable ship mode (Ship Mode on the visual axis).",
 "On Time Ship %": "Share of order lines shipped within their ship-mode SLA.",
 "Late %": "Share of order lines shipped late (missed their ship-mode SLA).",
 "Profitable?": "Whether the current context is profitable (profit greater than 0).",
 "Low Volume": "Whether the current context has fewer than 50 order lines.",
 "Deep Discount?": "Whether the average discount exceeds 25%.",
 "Met Sales Goal?": "Whether average sales per order exceed the 460 USD goal.",
 "Profit Ratio Over Goal?": "Whether Profit Ratio exceeds the Profit Ratio Goal slicer value.",
 "Order Volume - Rank of Count": "Dense rank of Sub-Categories by order-line count (1 = highest volume).",
 "Map Columns": "Column index (0-9) of the state's tile in the map tile-grid layout.",
 "Map Rows": "Row index of the state's tile in the map tile-grid layout.",
 "Volume Quartile": "Quartile bucket of Sub-Category order volume, shown as a filled-arc glyph.",
 "Sales East/West Max": "Peak quarterly value of the larger of East/West sales across the visible quarters.",
 "Sales East/West Window Max": "Peak quarterly value of the larger of East/West sales across the visible quarters.",
}

# ============================================================================
#  Emitters
# ============================================================================
def emit_column(tname, disp, src, dtype, hidden, summ, calc_dax=None, fmt=None, desc=None):
    L = []
    if desc: L.append(f"\t/// {one_line(desc)}")
    L.append(f"\tcolumn '{disp}' = {calc_dax}" if calc_dax is not None else f"\tcolumn '{disp}'")
    L.append(f"\t\tdataType: {dtype}")
    if fmt: L.append(f"\t\tformatString: {fmt}")
    elif dtype == "dateTime": L.append("\t\tformatString: Long Date")
    L.append(f"\t\tlineageTag: {lt(tname, 'col', disp)}")
    L.append(f"\t\tsummarizeBy: {summ}")
    if calc_dax is None: L.append(f"\t\tsourceColumn: {src}")
    if hidden: L.append("\t\tisHidden")
    L.append("")
    L.append("\t\tannotation SummarizationSetBy = Automatic")
    return "\n".join(L)

def emit_measure(tname, name, dax, fmt, folder, hidden, desc):
    L = []
    lead = MEANING_LEAD.get(name)
    full = f"{lead} {desc}".strip() if lead else desc
    if full: L.append(f"\t/// {one_line(full)}")
    L.append(f"\tmeasure '{name}' = {dax}")
    if fmt: L.append(f"\t\tformatString: {fmt}")
    if hidden: L.append("\t\tisHidden")
    L.append(f"\t\tlineageTag: {lt(tname, 'measure', name)}")
    if folder: L.append(f"\t\tdisplayFolder: {folder}")
    return "\n".join(L)

def emit_m_partition(tname, csv_name, cols):
    types = ", ".join("{\"%s\", %s}" % (src, MTYPE[dtype]) for (_d, src, dtype, *_r) in cols)
    src = (
        "\t\tsource =\n\t\t\t\tlet\n"
        f"\t\t\t\t    Source = Csv.Document(File.Contents(DataFolder & \"{csv_name}\"), [Delimiter=\",\", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n"
        "\t\t\t\t    #\"Promoted Headers\" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n"
        f"\t\t\t\t    #\"Changed Type\" = Table.TransformColumnTypes(#\"Promoted Headers\", {{{types}}}, \"en-US\")\n"
        "\t\t\t\tin\n\t\t\t\t    #\"Changed Type\""
    )
    return f"\tpartition '{tname}' = m\n\t\tmode: import\n{src}"

def emit_orders():
    t = "Orders"
    desc = ("Sample Superstore order lines (9994 rows, Tableau extract 'Orders'). Single import fact table; "
            "Tableau had 0 joins/blends. NO Date table by design (no time-intelligence in the workbook).")
    out = [f"/// {desc}", f"table '{t}'", f"\tlineageTag: {lt(t, 'table')}", ""]
    for (name, dax, fmt, folder, hidden, d) in M:
        out.append(emit_measure(t, name, dax, fmt, folder, hidden, d)); out.append("")
    for (disp, src, dtype, hidden, summ, d) in PHYS_COLS:
        out.append(emit_column(t, disp, src, dtype, hidden, summ, desc=d)); out.append("")
    for (nm, dax, dtype, hidden, summ, fmt, d) in CALC_COLS:
        out.append(emit_column(t, nm, None, dtype, hidden, summ, calc_dax=dax, fmt=fmt, desc=d)); out.append("")
    out.append(emit_m_partition(t, "ds.orders_sample_superstore.csv", PHYS_COLS))
    out.append("")
    out.append("\tannotation PBI_ResultType = Table")
    out.append("")
    return "\n".join(out)

def emit_param_table(p):
    t = p["name"]
    out = [f"/// {one_line(p['desc'])}", f"table '{t}'", f"\tlineageTag: {lt(t, 'table')}", ""]
    out.append(f"\t/// Reads the current slicer selection on '{t}'[{t}], defaulting to Tableau's current value when nothing is selected.")
    out.append(f"\tmeasure '{t} Value' = SELECTEDVALUE('{t}'[{t}], {p['default_dax']})")
    if p["vfmt"]: out.append(f"\t\tformatString: {p['vfmt']}")
    out.append(f"\t\tlineageTag: {lt(t, 'measure', t + ' Value')}")
    out.append(f"\t\tdisplayFolder: Parameter")
    out.append("")
    if p.get("col_desc"): out.append(f"\t/// {one_line(p['col_desc'])}")
    out.append(f"\tcolumn '{t}'")
    out.append(f"\t\tdataType: {p['col_type']}")
    if p["col_fmt"]: out.append(f"\t\tformatString: {p['col_fmt']}")
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
        mtype = "type number"
    src = (
        "\t\tsource =\n\t\t\t\tlet\n"
        f"\t\t\t\t    Source = {listexpr},\n"
        f"\t\t\t\t    #\"Converted to Table\" = Table.FromList(Source, Splitter.SplitByNothing(), {{\"{t}\"}}),\n"
        f"\t\t\t\t    #\"Changed Type\" = Table.TransformColumnTypes(#\"Converted to Table\", {{{{\"{t}\", {mtype}}}}}, \"en-US\")\n"
        "\t\t\t\tin\n\t\t\t\t    #\"Changed Type\""
    )
    out.append(f"\tpartition '{t}' = m\n\t\tmode: import\n{src}")
    out.append("")
    out.append("\tannotation PBI_ResultType = Table")
    out.append("")
    return "\n".join(out)

# ============================================================================
#  Coverage check
# ============================================================================
def check_coverage():
    import json
    spec = json.load(open(os.path.join(BASE, "..", "migration-spec.json"), encoding="utf-8"))
    calc_caps = [f.get("caption") or f.get("name")
                 for f in spec["data_sources"][0]["fields"] if f.get("kind") == "calculated"]
    measure_names = {m[0] for m in M}
    col_names = {c[0] for c in PHYS_COLS} | {c[0] for c in CALC_COLS}
    missing = []
    for cap in calc_caps:
        tgt = DISPOSITION.get(cap)
        if tgt is None:
            missing.append(f"NO DISPOSITION for calc caption: {cap!r}"); continue
        if tgt.startswith("col:"):
            if tgt[4:] not in col_names: missing.append(f"{cap!r} -> missing column {tgt[4:]!r}")
        elif tgt not in measure_names:
            missing.append(f"{cap!r} -> missing measure {tgt!r}")
    print(f"COVERAGE: {len(calc_caps)} spec calc fields; {len(missing)} unresolved.")
    for x in missing: print("   !!", x)
    return len(missing) == 0

# ============================================================================
#  Write files
# ============================================================================
os.makedirs(TABLES, exist_ok=True)

_platform_json = (
    '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
    '  "metadata": {\n    "type": "SemanticModel",\n    "displayName": "QuadrupleAxisCharts"\n  },\n'
    '  "config": {\n    "version": "2.0",\n'
    '    "logicalId": "' + lt("platform", "logicalId") + '"\n  }\n}\n')
with open(os.path.join(MODEL, ".platform"), "w", encoding="utf-8") as f:
    f.write(_platform_json)

with open(os.path.join(MODEL, "definition.pbism"), "w", encoding="utf-8") as f:
    f.write('{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
            '  "version": "4.2",\n  "settings": {\n    "qnaEnabled": false\n  }\n}\n')

with open(os.path.join(DEFN, "database.tmdl"), "w", encoding="utf-8") as f:
    f.write("database\n\tcompatibilityLevel: 1606\n")

with open(os.path.join(DEFN, "expressions.tmdl"), "w", encoding="utf-8") as f:
    f.write(f'expression DataFolder = "{esc_m(DATA_FOLDER)}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')
    f.write(f"\tlineageTag: {lt('expr', 'DataFolder')}\n\n\tannotation PBI_ResultType = Text\n")

all_tables = ["Orders"] + [p["name"] for p in PARAM_SPECS]
qorder = '["' + '","'.join(all_tables + ["DataFolder"]) + '"]'
with open(os.path.join(DEFN, "model.tmdl"), "w", encoding="utf-8") as f:
    f.write("model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n")
    f.write("\tsourceQueryCulture: en-US\n\tdataAccessOptions\n\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n")
    f.write(f"annotation PBI_QueryOrder = {qorder}\n\n")
    f.write("annotation __PBI_TimeIntelligenceEnabled = 0\n\n")
    for t in all_tables:
        f.write(f"ref table '{t}'\n")
    f.write("\n")

with open(os.path.join(TABLES, "Orders.tmdl"), "w", encoding="utf-8") as f:
    f.write(emit_orders())
for p in PARAM_SPECS:
    with open(os.path.join(TABLES, p["name"] + ".tmdl"), "w", encoding="utf-8") as f:
        f.write(emit_param_table(p))

ok = check_coverage()
print("Wrote model to:", MODEL)
print(f"Measures: {len(M)}  |  Phys cols: {len(PHYS_COLS)}  |  Calc cols: {len(CALC_COLS)}  |  Param tables: {len(PARAM_SPECS)}")
print("Files:")
for root, _d, files in os.walk(DEFN):
    for fn in sorted(files):
        print("   ", os.path.relpath(os.path.join(root, fn), MODEL))
if not ok:
    sys.exit("COVERAGE FAILED — some calc fields undispositioned.")
