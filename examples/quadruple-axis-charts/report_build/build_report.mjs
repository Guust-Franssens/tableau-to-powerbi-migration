// QuadrupleAxisCharts — deterministic PBIR report generator.
// Owns ONLY the report layer. Emits fabric/QuadrupleAxisCharts.Report/**.
// Binds byPath to ../QuadrupleAxisCharts.SemanticModel. Never touches the model/TMDL.
import { mkdirSync, writeFileSync, rmSync, existsSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { join, dirname } from "node:path";

const REPO = "C:/Users/gfranssens/vscode-projects/tableau-to-pbi-migration";
const REPORT = join(REPO, "migrations/quadruple-axis-charts/fabric/QuadrupleAxisCharts.Report");
const ORD = "Orders";

// ---------- schema URLs ----------
const S = {
  vc:  "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.11.0/schema.json",
  page:"https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json",
  pages:"https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.1.0/schema.json",
  report:"https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.3.0/schema.json",
  ver: "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
  defp:"https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
  plat:"https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
  theme:"https://raw.githubusercontent.com/microsoft/powerbi-desktop-samples/main/Report%20Theme%20JSON%20Schema/reportThemeSchema-2.153.json",
};

// ---------- literal / field helpers ----------
const lit = v => ({ expr: { Literal: { Value: v } } });
const bL  = b => lit(b ? "true" : "false");
const nL  = n => lit(`${n}D`);
const sL  = s => lit(`'${s}'`);
const solid = hex => ({ solid: { color: lit(`'${hex}'`) } });

const M = prop => ({ field:{ Measure:{ Expression:{ SourceRef:{ Entity:ORD }}, Property:prop }}, queryRef:`${ORD}.${prop}`, nativeQueryRef:prop });
const MT = (t, prop) => ({ field:{ Measure:{ Expression:{ SourceRef:{ Entity:t }}, Property:prop }}, queryRef:`${t}.${prop}`, nativeQueryRef:prop });
const Col = (t,c) => ({ field:{ Column:{ Expression:{ SourceRef:{ Entity:t }}, Property:c }}, queryRef:`${t}.${c}`, nativeQueryRef:c });
const OCol = c => Col(ORD, c);
const role = projs => ({ projections: projs.map(p => ({ ...p, active:true })) });

// ---------- canvas / layout ----------
const CW = 1600, CH = 900;
const MX = 24, MW = CW - 48;                 // 1552
const BAND_Y = 86, BAND_H = 56;              // slicer band
const MAIN_Y = 150, MAIN_H = 694;            // 150..844
const FOOT_Y = 848, FOOT_H = 36;
const P = (x,y,w,h) => ({ x, y, z:0, width:w, height:h, tabOrder:0 });
const slot = i => P(MX + i*260, BAND_Y, 248, BAND_H);
const halfL = P(MX, MAIN_Y, (MW-16)/2, MAIN_H);          // 24 , w768
const halfR = P(MX+(MW-16)/2+16, MAIN_Y, (MW-16)/2, MAIN_H); // 808, w768
const full  = P(MX, MAIN_Y, MW, MAIN_H);
const topH  = P(MX, MAIN_Y, MW, 336);
const botH  = P(MX, MAIN_Y+352, MW, MAIN_H-352);

// ---------- container ----------
function container(name, visualType, pos, { queryState, objects, vco, filters, title } = {}) {
  const visual = { visualType, drillFilterOtherVisuals:true };
  if (queryState) visual.query = { queryState };
  if (objects) visual.objects = objects;
  const v = vco ? { ...vco } : {};
  if (title) v.title = [{ properties:{ show:bL(true), text:sL(title), fontSize:nL(11), bold:bL(true), fontColor:solid("#1F2E35") } }];
  if (Object.keys(v).length) visual.visualContainerObjects = v;
  const o = { $schema:S.vc, name, position:pos, visual };
  if (filters) o.filterConfig = { filters };
  return o;
}

// ---------- textbox ----------
function textbox(name, pos, paragraphs) {
  const paras = paragraphs.map(p => ({
    textRuns: p.map(r => ({
      value: r.t,
      textStyle: {
        fontFamily: r.family || "Segoe UI",
        fontSize: (r.size || 11) + "pt",
        color: r.color || "#1F2E35",
        ...(r.weight ? { fontWeight:r.weight } : {}),
        ...(r.italic ? { fontStyle:"italic" } : {}),
      },
    })),
  }));
  return container(name, "textbox", pos, {
    objects: { general:[{ properties:{ paragraphs:paras } }] },
    vco: { padding:[{ properties:{ top:nL(0), bottom:nL(0), left:nL(0), right:nL(0) } }] },
  });
}

// ---------- slicer (disconnected param, single-select dropdown + default) ----------
function slicer(name, pos, table, col, caption, defLit) {
  return container(name, "slicer", pos, {
    queryState: { Values: role([Col(table, col)]) },
    objects: {
      general:[{ properties:{ filter:{ filter:{
        Version:2, From:[{ Name:"s", Entity:table, Type:0 }],
        Where:[{ Condition:{ In:{ Expressions:[{ Column:{ Expression:{ SourceRef:{ Source:"s" }}, Property:col }}], Values:[[{ Literal:{ Value:defLit } }]] } } }],
      } } } }],
      selection:[{ properties:{ singleSelect:bL(true), selectAllCheckboxEnabled:bL(false) } }],
      data:[{ properties:{ mode:sL("Dropdown") } }],
      header:[{ properties:{ show:bL(false) } }],
      items:[{ properties:{ textSize:nL(9), padding:nL(2) } }],
    },
    title: caption,
  });
}

// ---------- region visual-level filter ----------
function regionFilter(fname, values) {
  return {
    name:fname,
    field:{ Column:{ Expression:{ SourceRef:{ Entity:ORD }}, Property:"Region" }},
    type:"Categorical",
    filter:{ Version:2, From:[{ Name:"o", Entity:ORD, Type:0 }],
      Where:[{ Condition:{ In:{ Expressions:[{ Column:{ Expression:{ SourceRef:{ Source:"o" }}, Property:"Region" }}], Values: values.map(v => [{ Literal:{ Value:`'${v}'` } }]) } } }] },
    howCreated:"User",
  };
}

// ---------- charts ----------
function lineChartV(name, pos, { cat, y, markers, title, filters }) {
  const objects = { legend:[{ properties:{ show:bL(true) } }] };
  if (markers) objects.lineStyles = [{ properties:{ showMarker:bL(true), markerSize:nL(5) } }];
  return container(name, "lineChart", pos, { queryState:{ Category:role([cat]), Y:role(y) }, objects, title, filters });
}

function combo(name, pos, { cat, y, y2, series, secondary, title, filters }) {
  const qs = { Category:role([cat]), Y:role(y) };
  if (y2 && y2.length) qs.Y2 = role(y2);
  if (series) qs.Series = role([series]);
  const objects = {
    valueAxis:[{ properties:{ secShow:bL(!!secondary), sharedAxis:bL(!secondary) } }],
    legend:[{ properties:{ show:bL(true) } }],
  };
  return container(name, "lineClusteredColumnComboChart", pos, { queryState:qs, objects, title, filters });
}

function clustered(name, pos, { cat, y, series, title, filters }) {
  const qs = { Category:role([cat]), Y:role(y) };
  if (series) qs.Series = role([series]);
  return container(name, "clusteredColumnChart", pos, { queryState:qs, objects:{ legend:[{ properties:{ show:bL(true) } }] }, title, filters });
}

function stacked100(name, pos, { cat, series, y, rows, title, filters }) {
  const qs = { Category:role([cat]), Y:role([y]) };
  if (series) qs.Series = role([series]);
  if (rows) qs.Rows = role([rows]);
  return container(name, "hundredPercentStackedColumnChart", pos, { queryState:qs, objects:{ legend:[{ properties:{ show:bL(true) } }] }, title, filters });
}

function scatter(name, pos, { x, y, cat, size, tooltips, title, filters, invertY, hideAxes, catLabels, fillMeasure }) {
  const qs = { X:role([x]), Y:role([y]) };
  if (cat) qs.Category = role([cat]);
  if (size) qs.Size = role([size]);
  if (tooltips) qs.Tooltips = role(tooltips);
  const objects = { legend:[{ properties:{ show:bL(false) } }] };
  const vaProps = {};
  if (invertY) vaProps.invertAxis = bL(true);
  if (hideAxes) vaProps.show = bL(false);
  if (Object.keys(vaProps).length) objects.valueAxis = [{ properties:vaProps }];
  if (hideAxes) objects.categoryAxis = [{ properties:{ show:bL(false) } }];
  if (catLabels) objects.categoryLabels = [{ properties:{ show:bL(true), fontSize:nL(8) } }];
  if (fillMeasure) objects.dataPoint = [{
    properties:{
      fill:{ solid:{ color:{ expr:{ FillRule:{
        Input:{ SelectRef:{ ExpressionName:`${ORD}.${fillMeasure}` } },
        FillRule:{ linearGradient3:{
          min:{ color:{ Literal:{ Value:"'#DFF6DD'" } }, value:{ Literal:{ Value:"0D" } } },
          mid:{ color:{ Literal:{ Value:"'#FFF4CE'" } } },
          max:{ color:{ Literal:{ Value:"'#F06E4C'" } } },
          nullColoringStrategy:{ strategy:{ Literal:{ Value:"'noColor'" } } },
        } },
      } } } } },
    },
    selector:{ data:[{ dataViewWildcard:{ matchingOption:1 } }], metadata:`${ORD}.${fillMeasure}` },
  }];
  return container(name, "scatterChart", pos, { queryState:qs, objects, title, filters });
}

function pivot(name, pos, { rows, cols, values, bgMeasure, title, filters }) {
  const qs = { Rows:role(rows), Values:role(values) };
  if (cols) qs.Columns = role(cols);
  const objects = {
    columnHeaders:[{ properties:{ fontSize:nL(9), bold:bL(true) }, selector:{ id:"default" } }],
    grid:[{ properties:{ textSize:nL(9), rowPadding:nL(3) }, selector:{ id:"default" } }],
    values:[{ properties:{ fontSize:nL(9) }, selector:{ id:"default" } }],
  };
  if (bgMeasure) objects.values.push({
    properties:{ backColor:{ solid:{ color:{ expr:{ FillRule:{
      Input:{ SelectRef:{ ExpressionName:`${ORD}.${bgMeasure}` } },
      FillRule:{ linearGradient3:{
        min:{ color:{ Literal:{ Value:"'#F4B7B0'" } }, value:{ Literal:{ Value:"0D" } } },
        mid:{ color:{ Literal:{ Value:"'#FFFFFF'" } } },
        max:{ color:{ Literal:{ Value:"'#9FD5E3'" } } },
        nullColoringStrategy:{ strategy:{ Literal:{ Value:"'noColor'" } } },
      } },
    } } } } } },
    selector:{ data:[{ dataViewWildcard:{ matchingOption:1 } }], metadata:`${ORD}.${bgMeasure}` } });
  return container(name, "pivotTable", pos, { queryState:qs, objects, title, filters });
}

function pageNav(name, pos) {
  return container(name, "pageNavigator", pos, {
    objects:{ text:[{ properties:{ fontSize:nL(10) }, selector:{ id:"default" } }] },
    vco:{ background:[{ properties:{ show:bL(false) } }] },
  });
}

// ---------- header / footer ----------
const hdr = (id, title, sub) => [
  textbox(`${id}_title`, P(MX,16,MW,38), [[{ t:title, size:20, color:"#0A0A0A", weight:"bold", family:"Segoe UI Semibold" }]]),
  textbox(`${id}_sub`,   P(MX,56,MW,26), [[{ t:sub, size:11, color:"#5B6B72" }]]),
];
const foot = (id, txt) => textbox(`${id}_note`, P(MX,FOOT_Y,MW,FOOT_H), [[{ t:txt, size:9, color:"#8A5A00", italic:true }]]);

// =========================================================================
// PAGES
// =========================================================================
const pages = [];

// ---- 0. Overview ----
pages.push({ id:"overview", displayName:"Overview", visuals:[
  ...hdr("ov", "10 Ways to Make Quadruple-Axis Charts",
    "Tableau multi-axis showcase, migrated to Power BI. 12 examples of fake extra-axis tricks; each rebuilt as the closest faithful native visual."),
  textbox("ov_intro", P(MX,110,752,352), [
    [{ t:"What this report is", size:14, color:"#067DB7", weight:"bold" }],
    [{ t:"", size:6 }],
    [{ t:"The source workbook is a catalog of ~10 techniques Tableau users employ to fake a 3rd, 4th, 5th or 6th axis: stacking identical measures on Rows/Columns, pinning constant axis-anchor placeholders (MAX(1.0)/MAX(0.0)/0), and layering unicode shape/glyph marks driven by table calcs.", size:10.5 }],
    [{ t:"", size:6 }],
    [{ t:"Every underlying metric window (East/West splits, profit-ratio windows, rank/quartile bins, INDEX map coordinates) is modeled as a real DAX measure and is bound faithfully here. Use the navigator to browse each example.", size:10.5 }],
  ]),
  textbox("ov_callout", P(MX,476,752,360), [
    [{ t:"The capability boundary (why this is honest, not a fake stack)", size:14, color:"#BC0016", weight:"bold" }],
    [{ t:"", size:6 }],
    [{ t:"Power BI genuinely caps at a dual-axis combo chart (columns + line on two value axes) plus small multiples. It cannot stack N>2 synthetic axes, render hatch/hash fills, or place arbitrary unicode shape-marks as data geometry.", size:10.5 }],
    [{ t:"", size:6 }],
    [{ t:"For each such trick this report builds the best native approximation that preserves the DATA, adds an on-page note, and logs a report_build limitation. We do NOT fake N-axis geometry — validation passes structurally-valid-but-wrong encodings, so faking would look right and be wrong. See the Summary page for the per-trick verdict.", size:10.5 }],
  ]),
  pageNav("ov_nav", P(808,110,768,726)),
]});

// ---- 1. Line/Area++ ----
pages.push({ id:"p01_line_area", displayName:"1 - Line/Area++", visuals:[
  ...hdr("p01", "Example 1 - Line / Area++  (Area + Dot + Line + Circle)",
    "South region sales history. Tableau layers an area + dot + line + circle mark on one synthetic sales axis."),
  slicer("p01_hl", slot(0), "Select Highlight Function", "Select Highlight Function", "HIGHLIGHT FUNCTION", "'Max'"),
  lineChartV("p01_line", full, {
    cat: OCol("Ship Month"),
    y: [ M("Total Sales"), M("Dot Sales Window"), M("Circle Sales Window") ],
    markers:true, title:"South Sales over Ship Month  (Total Sales + windowed Dot/Circle marks)",
    filters:[ regionFilter("f_p01_line_region", ["South"]) ],
  }),
  foot("p01", "HIGH boundary: Tableau stacks Area+Dot+Line+Circle marks on one synthetic axis. Power BI = markers on a shared axis; windowed marks bound as real measures, layered area+4-mark geometry not reproduced. See Summary."),
]});

// ---- 2. Line+++ ----
pages.push({ id:"p02_line_plus", displayName:"2 - Line+++", visuals:[
  ...hdr("p02", "Example 2 - Line+++  (Circle + Dot + Line + Circle)",
    "South region sales history with a deep-discount dot overlay - four mark types on one axis in Tableau."),
  slicer("p02_hl", slot(0), "Select Highlight Function", "Select Highlight Function", "HIGHLIGHT FUNCTION", "'Max'"),
  lineChartV("p02_line", full, {
    cat: OCol("Ship Month"),
    y: [ M("Total Sales"), M("Dot Sales Window"), M("Circle Sales Window"), M("Deep Discount Dot") ],
    markers:true, title:"South Sales over Ship Month  (Total Sales + Dot/Circle/Deep-Discount marks)",
    filters:[ regionFilter("f_p02_line_region", ["South"]) ],
  }),
  foot("p02", "HIGH boundary: four synthetic mark layers on one axis -> one line chart with markers. Marks are real measures; N-layer geometry not reproduced. See Summary."),
]});

// ---- 3. Triangles x4 ----
pages.push({ id:"p03_triangles", displayName:"3 - Triangles x4", visuals:[
  ...hdr("p03", "Example 3 - Triangles x 4",
    "South region Sales vs Profit by Sub-Category. Tableau draws four triangle shape-marks per point across profit-ratio windows."),
  scatter("p03_sc", full, {
    x: M("Total Sales"), y: M("Total Profit"), cat: OCol("Sub-Category"),
    size: M("Total Quantity"), tooltips:[ M("Profit Ratio") ],
    title:"Sales vs Profit by Sub-Category (South)  - bubble size = Quantity",
    filters:[ regionFilter("f_p03_sc_region", ["South"]) ],
  }),
  foot("p03", "HIGH boundary: 4 triangle shape-marks x profit-ratio windows per Sub-Category -> one Sales-vs-Profit scatter. Unicode shape-marks & N windows are a capability boundary. See Summary."),
]});

// ---- 4. Varying Shape (pies) ----
pages.push({ id:"p04_pies", displayName:"4 - Varying Shape", visuals:[
  ...hdr("p04", "Example 4 - Varying Shape  (On-Time Ship Pies)",
    "A pie per Region x Category encoding on-time vs late ship proportion."),
  stacked100("p04_sm", full, {
    cat: OCol("Region"), series: OCol("On Time Ship?"), y: M("Order Count"), rows: OCol("Category"),
    title:"On-Time vs Late Ship % by Region, small multiples by Category",
  }),
  foot("p04", "MEDIUM boundary: pie-per-cell grid -> 100% stacked columns with small multiples (faithful on-time proportion). Pie-mark geometry approximated. See Summary."),
]});

// ---- 5. Color & Size ----
pages.push({ id:"p05_color_size", displayName:"5 - Color & Size", visuals:[
  ...hdr("p05", "Example 5 - Color & Size  (Discount Scatter)",
    "South region Sales vs Profit by Sub-Category; discount depth drives both bubble size and color."),
  scatter("p05_sc", full, {
    x: M("Total Sales"), y: M("Total Profit"), cat: OCol("Sub-Category"),
    size: M("Deep Discount? (Highlight)"), tooltips:[ M("Profit Ratio") ],
    fillMeasure:"Deep Discount? (Highlight)",
    title:"Sales vs Profit (South)  - size & color = Deep Discount depth",
    filters:[ regionFilter("f_p05_sc_region", ["South"]) ],
  }),
  foot("p05", "LOW boundary: Color+Size double-encoding preserved via bubble size + color saturation on Deep Discount depth. Faithful."),
]});

// ---- 6. Border Highlight ----
pages.push({ id:"p06_highlight", displayName:"6 - Border Highlight", visuals:[
  ...hdr("p06", "Example 6 - Border Highlight  (Highlight Table)",
    "Profit by Sub-Category x Region. Tableau highlights cells with colored borders + profit icons."),
  pivot("p06_ht", full, {
    rows:[ OCol("Sub-Category") ], cols:[ OCol("Region") ], values:[ M("Total Profit") ],
    bgMeasure:"Total Profit", title:"Profit by Sub-Category x Region  (background gradient)",
  }),
  foot("p06", "MEDIUM boundary: cell border-highlight + profit icons -> background color gradient on a matrix. Border-as-third-encoding not reproduced. See Summary."),
]});

// ---- 7. Bar Hashing ----
pages.push({ id:"p07_hashing", displayName:"7 - Bar Hashing", visuals:[
  ...hdr("p07", "Example 7 - Bar Hashing  (Profit Ratio vs Goal)",
    "Two hashed bar variants comparing Profit Ratio to a param-driven goal. Hash fills + windowed quad-axis in Tableau."),
  slicer("p07_hl",   slot(0), "Select Highlight Function", "Select Highlight Function", "HIGHLIGHT FUNCTION", "'Max'"),
  slicer("p07_goal", slot(1), "Profit Ratio Goal", "Profit Ratio Goal", "PROFIT RATIO GOAL", "0.12D"),
  slicer("p07_hash", slot(2), "Select Hashing", "Select Hashing", "HASHING STYLE", "'\u2571\u2571\u2571'"),
  combo("p07_c1", halfL, {
    cat: OCol("Sub-Category"), y:[ M("Profit Ratio") ], y2:[ MT("Profit Ratio Goal", "Profit Ratio Goal Value") ], secondary:false,
    title:"SubCat Bar w/ Hash Indicator - Profit Ratio vs dynamic Goal",
  }),
  combo("p07_c2", halfR, {
    cat: OCol("Sub-Category"), y:[ M("Profit Ratio") ], y2:[ MT("Profit Ratio Goal", "Profit Ratio Goal Value") ], secondary:false,
    title:"SubCat Bar Hash Quad - Profit Ratio vs dynamic Goal",
  }),
  foot("p07", "HIGH boundary: hatch/hash bar fills + windowed quad-axis -> dual-axis combo (bars vs dynamic goal line on a shared axis). Hatch fills & N synthetic axes are a capability boundary. See Summary."),
]});

// ---- 8. L-Bar/Dot ----
pages.push({ id:"p08_lbar", displayName:"8 - L-Bar/Dot", visuals:[
  ...hdr("p08", "Example 8 - L-Bar / Dot",
    "Sales & Order Count per Sub-Category. Tableau fakes an L-shaped bar with check/dot marks."),
  combo("p08_c", full, {
    cat: OCol("Sub-Category"), y:[ M("Total Sales") ], y2:[ M("Order Count") ], secondary:true,
    title:"Sales (columns) vs Order Count (line) by Sub-Category",
  }),
  foot("p08", "HIGH boundary: L-shaped bar geometry + check/dot marks -> dual-axis combo (Sales columns + Order Count line). L-geometry & glyph marks not reproduced. See Summary."),
]});

// ---- 9. Bar/Line/Dot ----
pages.push({ id:"p09_barline", displayName:"9 - Bar/Line/Dot", visuals:[
  ...hdr("p09", "Example 9 - Bar / Line / Dot  (Bar-in-Bar)",
    "East/West Sales vs a dynamic Variable Metric by Order Quarter. Tableau overlays different-width bars (bar-in-bar)."),
  slicer("p09_vm", slot(0), "Variable Metric", "Variable Metric", "VARIABLE METRIC", "'- Order Count'"),
  combo("p09_top", topH, {
    cat: OCol("Order Quarter"), y:[ M("Total Sales") ], y2:[ M("Variable Metric COUNTD") ], secondary:true,
    title:"Measure vs Dynamic - Total Sales (columns) vs Variable Metric (line) by Order Quarter",
    filters:[ regionFilter("f_p09_top_region", ["East", "West"]) ],
  }),
  clustered("p09_bot", botH, {
    cat: OCol("Order Quarter"), y:[ M("Sales - West"), M("Sales - East") ],
    title:"Measure Values - Sales West vs East by Order Quarter",
  }),
  foot("p09", "HIGH boundary: bar-in-bar overlay (different-width bars) + unicode reference marks -> dual-axis combo + clustered columns. Overlapping-width bar geometry is a capability boundary. See Summary."),
]});

// ---- 10. Map Trellis ----
pages.push({ id:"p10_maptrellis", displayName:"10 - Map Trellis", visuals:[
  ...hdr("p10", "Example 10 - Map Trellis  (State Tile-Grid)",
    "49 states laid out in an INDEX-derived tile grid, each a mini choropleth in Tableau."),
  scatter("p10_grid", full, {
    x: M("Map Columns"), y: M("Map Rows"), cat: OCol("State"), size: M("Total Sales"),
    invertY:true, hideAxes:true, catLabels:true,
    title:"State tile-grid (Map Columns x Map Rows, inverted)  - bubble size = Sales",
  }),
  foot("p10", "HIGH boundary: 49 Multipolygon mini-maps in an INDEX tile-grid -> scatter tile-grid (same 49-cell layout, sized by Sales). Real geography -> azureMap choropleth is the alternative. See Summary."),
]});

// ---- 11. Summary & Fidelity ----
pages.push({ id:"p11_summary", displayName:"Summary & Fidelity", visuals:[
  ...hdr("p11", "Summary - Fidelity & Capability Boundary",
    "Per-trick verdict, plus the Ship Mode Test Sheet (Profit by Region/Sub-Category/Ship Mode)."),
  textbox("p11_txt", P(MX,110,752,726), [
    [{ t:"Multi-axis fidelity verdict", size:14, color:"#067DB7", weight:"bold" }],
    [{ t:"", size:5 }],
    [{ t:"Dual-axis combos (faithful):", size:11, weight:"bold" }],
    [{ t:"  Ex.8 L-Bar -> Sales columns + Order Count line;  Ex.9 Bar-in-Bar -> Sales columns + Variable Metric line + West/East clustered.", size:10 }],
    [{ t:"", size:4 }],
    [{ t:"Small multiples (faithful data, approximated marks):", size:11, weight:"bold" }],
    [{ t:"  Ex.4 On-Time Ship Pies -> 100% stacked columns, small-multipled by Category.", size:10 }],
    [{ t:"", size:4 }],
    [{ t:"Scatter / tile-grid (faithful data, marks simplified):", size:11, weight:"bold" }],
    [{ t:"  Ex.3 Triangles & Ex.5 Color+Size -> Sales-vs-Profit scatter (Ex.5 keeps size+color discount encoding);  Ex.10 Map Trellis -> INDEX tile-grid scatter (49-state layout preserved).", size:10 }],
    [{ t:"", size:4 }],
    [{ t:"Honest approximations (geometry is a hard boundary):", size:11, weight:"bold", color:"#BC0016" }],
    [{ t:"  Ex.1/2 Line/Area++ -> markers on one axis (no layered area + 4 marks).", size:10 }],
    [{ t:"  Ex.6 Border Highlight -> matrix background gradient (no border-as-encoding + icons).", size:10 }],
    [{ t:"  Ex.7 Bar Hashing -> bars vs dynamic goal line (no hatch fills, no windowed quad-axis).", size:10 }],
    [{ t:"", size:6 }],
    [{ t:"Why not fake it: Power BI caps at TWO value axes. Stacking N synthetic axes, hatch fills and arbitrary unicode shape-marks as data geometry cannot be encoded natively - and 'validate' passes structurally-valid-but-wrong encodings, so a fake stack would look right and be wrong. Every metric behind every mark is bound as a real measure; only the non-reproducible geometry is dropped, and each is logged as a report_build limitation.", size:10, italic:true }],
    [{ t:"", size:6 }],
    [{ t:"Param slicers: 4 disconnected param tables drive their SELECTEDVALUE measures (Highlight Function, Profit Ratio Goal, Select Hashing, Variable Metric). Being disconnected, they cannot zero a fact aggregate; defaults match the Tableau current values.", size:10 }],
  ]),
  pivot("p11_pt", P(808,110,768,726), {
    rows:[ OCol("Region"), OCol("Sub-Category"), OCol("Ship Mode") ],
    values:[ M("Total Profit"), M("Profit SUM by SubCat, Region, Ship Mode"), M("Profit MAX (SUM by SubCat, Region, Ship Mode)") ],
    title:"Ship Mode Test Sheet - Profit by Region / Sub-Category / Ship Mode",
  }),
]});

// =========================================================================
// WRITE FILES
// =========================================================================
function writeJSON(path, obj) {
  mkdirSync(dirname(path), { recursive:true });
  writeFileSync(path, JSON.stringify(obj, null, 2), "utf8");
}

// clean report tree (report layer only)
if (existsSync(REPORT)) rmSync(REPORT, { recursive:true, force:true });
mkdirSync(REPORT, { recursive:true });

// .platform
writeJSON(join(REPORT, ".platform"), {
  $schema:S.plat,
  metadata:{ type:"Report", displayName:"QuadrupleAxisCharts" },
  config:{ version:"2.0", logicalId:randomUUID() },
});
// definition.pbir (byPath)
writeJSON(join(REPORT, "definition.pbir"), {
  $schema:S.defp, version:"4.0",
  datasetReference:{ byPath:{ path:"../QuadrupleAxisCharts.SemanticModel" } },
});
// version.json
writeJSON(join(REPORT, "definition/version.json"), { $schema:S.ver, version:"2.0.0" });
// report.json
writeJSON(join(REPORT, "definition/report.json"), {
  $schema:S.report,
  themeCollection:{
    baseTheme:{ name:"CY24SU10", reportVersionAtImport:{ visual:"1.8.97", report:"2.0.97", page:"1.3.97" }, type:"SharedResources" },
    customTheme:{ name:"theme.json", reportVersionAtImport:{ visual:"1.8.100", report:"2.0.100", page:"1.3.100" }, type:"RegisteredResources" },
  },
  objects:{ outspacePane:[{ properties:{ expanded:bL(false), visible:bL(true) } }] },
  resourcePackages:[
    { name:"SharedResources", type:"SharedResources", items:[{ name:"CY24SU10", path:"BaseThemes/CY24SU10.json", type:"BaseTheme" }] },
    { name:"RegisteredResources", type:"RegisteredResources", items:[{ name:"theme.json", path:"theme.json", type:"CustomTheme" }] },
  ],
  settings:{ useStylableVisualContainerHeader:true, defaultFilterActionIsDataFilter:true, defaultDrillFilterOtherVisuals:true, allowChangeFilterTypes:true, allowInlineExploration:true, useEnhancedTooltips:true },
  slowDataSourceSettings:{ isCrossHighlightingDisabled:false, isSlicerSelectionsButtonEnabled:false, isFilterSelectionsButtonEnabled:false, isFieldWellButtonEnabled:false, isApplyAllButtonEnabled:false },
});
// theme.json (Tableau palette; Segoe UI for legibility)
writeJSON(join(REPORT, "StaticResources/RegisteredResources/theme.json"), {
  $schema:S.theme, name:"theme.json",
  dataColors:["#067DB7","#F06E4C","#BC0016","#5B6B72","#00A6A6","#8FA9B3","#F2A49C","#00313C"],
  good:"#067DB7", neutral:"#E6E6E6", bad:"#BC0016",
  maximum:"#067DB7", center:"#FFFFFF", minimum:"#BC0016", null:"#E6E6E6",
  foreground:"#1F2E35", foregroundNeutralSecondary:"#5B6B72",
  background:"#FFFFFF", backgroundLight:"#F7F8F9", tableAccent:"#067DB7",
  textClasses:{
    callout:{ fontSize:28, fontFace:"Segoe UI Semibold, Segoe UI, sans-serif", color:"#1F2E35" },
    title:{ fontSize:14, fontFace:"Segoe UI Semibold, Segoe UI, sans-serif", color:"#1F2E35" },
    header:{ fontSize:11, fontFace:"Segoe UI Semibold, Segoe UI, sans-serif", color:"#1F2E35" },
    label:{ fontSize:9, fontFace:"Segoe UI, sans-serif", color:"#1F2E35" },
  },
  visualStyles:{
    "*":{ "*":{
      border:[{ show:true, color:{ solid:{ color:"#D9D9D9" } }, radius:4 }],
      dropShadow:[{ show:false }],
      padding:[{ top:4, bottom:4, left:4, right:4 }],
      background:[{ transparency:0, color:{ solid:{ color:"#FFFFFF" } } }],
    } },
    textbox:{ "*":{ background:[{ show:false }], border:[{ show:false }] } },
    lineChart:{ "*":{
      lineStyles:[{ strokeWidth:2.5, lineChartType:"linear", areaShow:false }],
      categoryAxis:[{ show:true, fontSize:9, labelColor:{ solid:{ color:"#5B6B72" } }, showAxisTitle:false, gridlineShow:false }],
      valueAxis:[{ show:true, fontSize:9, labelColor:{ solid:{ color:"#5B6B72" } }, showAxisTitle:false }],
    } },
    scatterChart:{ "*":{
      categoryAxis:[{ show:true, fontSize:9, labelColor:{ solid:{ color:"#5B6B72" } }, showAxisTitle:true }],
      valueAxis:[{ show:true, fontSize:9, labelColor:{ solid:{ color:"#5B6B72" } }, showAxisTitle:true }],
    } },
    slicer:{ "*":{ items:[{ fontFamily:"Segoe UI, sans-serif", textSize:9, fontColor:{ solid:{ color:"#1F2E35" } }, outlineStyle:0, padding:2 }] } },
  },
});

// pages
const pageOrder = pages.map(p => p.id);
for (const pg of pages) {
  // assign z / tabOrder deterministically
  pg.visuals.forEach((v, i) => { v.position.z = 1000 + i*10; v.position.tabOrder = i*100; });
  const dir = join(REPORT, "definition/pages", pg.id);
  writeJSON(join(dir, "page.json"), {
    $schema:S.page, name:pg.id, displayName:pg.displayName, displayOption:"FitToPage",
    height:CH, width:CW,
    objects:{ background:[{ properties:{ color:solid("#FFFFFF"), transparency:nL(0) } }] },
  });
  for (const v of pg.visuals) writeJSON(join(dir, "visuals", v.name, "visual.json"), v);
}
writeJSON(join(REPORT, "definition/pages/pages.json"), { $schema:S.pages, pageOrder, activePageName:"overview" });

const nVis = pages.reduce((a,p)=>a+p.visuals.length,0);
console.log(`OK: ${pages.length} pages, ${nVis} visuals written to`);
console.log(REPORT);
