// ============================================================================
// Airline Alliance Activity — PBIR report generator (re-runnable).
// Builds AirlineAllianceActivity.pbip + AirlineAllianceActivity.Report from the
// FROZEN semantic model. Run: node build.js
// ============================================================================
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = path.resolve(__dirname, '..');            // migrations/airline-alliance-activity
const FAB = path.join(ROOT, 'fabric');
const REPORT = path.join(FAB, 'AirlineAllianceActivity.Report');
const DEF = path.join(REPORT, 'definition');
const PAGES_DIR = path.join(DEF, 'pages');
const spec = require(path.join(ROOT, 'migration-spec.json'));

const PAGE_W = 1400, PAGE_H = 950;
const VC = 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.11.0/schema.json';

// ---- entities ----
const FA = 'Flight Activity', DATE = 'Date';
const P_YEAR = 'Year Parameter', P_MONTH = 'Month Parameter', P_REGION = 'Region Parameter';
const P_AIRLINE = 'Airline Parameter', P_AIRCRAFT = 'Aircraft Type Parameter';

// ---- id helpers ----
const hexName = () => crypto.randomBytes(10).toString('hex');

// ---- zone lookup (coords already flattened to 0..100000 abs) ----
function dash(name){ return spec.dashboards.find(d => d.name === name); }
function findZone(zone, id){
  if (String(zone.id) === String(id)) return zone;
  for (const c of (zone.children||[])){ const r = findZone(c, id); if (r) return r; }
  return null;
}
function zone(dashName, id){
  const d = dash(dashName); const z = findZone(d.zones, id);
  if (!z) throw new Error('zone not found '+dashName+' '+id);
  return z;
}
// zone -> px position box
function px(z, zoverride){
  return {
    x: +(z.x/100000*PAGE_W).toFixed(1),
    y: +(z.y/100000*PAGE_H).toFixed(1),
    w: +(z.w/100000*PAGE_W).toFixed(1),
    h: +(z.h/100000*PAGE_H).toFixed(1),
  };
}
function zpx(dashName, id){ return px(zone(dashName, id)); }

// ---- literal / expr helpers ----
const litRaw = v => ({ expr: { Literal: { Value: v } } });
const lstr = s => litRaw("'" + String(s).replace(/'/g, "''") + "'");
const lbool = b => litRaw(b ? 'true' : 'false');
const lnum = (n, suf='D') => litRaw(n + suf);
const solid = colorStr => ({ solid: { color: lstr(colorStr) } });

const colExpr = (e, p) => ({ Column: { Expression: { SourceRef: { Entity: e } }, Property: p } });
const measExpr = (e, p) => ({ Measure: { Expression: { SourceRef: { Entity: e } }, Property: p } });
const projC = (e, p, extra={}) => Object.assign({ field: colExpr(e,p), queryRef: e+'.'+p, nativeQueryRef: p }, extra);
const projM = (e, p, extra={}) => Object.assign({ field: measExpr(e,p), queryRef: e+'.'+p, nativeQueryRef: p }, extra);

// aggregated-column projection (implicit measure). f: 0=Sum, 1=Avg
const aggExpr = (e, p, f) => ({ Aggregation: { Expression: { Column: { Expression: { SourceRef: { Entity: e } }, Property: p } }, Function: f } });
const projA = (e, p, f, extra={}) => Object.assign({ field: aggExpr(e,p,f), queryRef: (f===1?'Avg(':'Sum(')+e+'.'+p+')', nativeQueryRef: p }, extra);
// visual-level single-value categorical filter (top-level filterConfig on a visual)
function vFilter(entity, col, val){
  const lit = "'" + String(val).replace(/'/g,"''") + "'";
  return { filters: [{
    name: 'Filter'+hexName(),
    field: colExpr(entity, col),
    type: 'Categorical',
    filter: { Version: 2, From: [{ Name: 's', Entity: entity, Type: 0 }],
      Where: [{ Condition: { In: {
        Expressions: [{ Column: { Expression: { SourceRef: { Source: 's' } }, Property: col } }],
        Values: [[ { Literal: { Value: lit } } ]],
      } } }] },
    howCreated: 'User',
  }] };
}

function qstate(obj){ const qs={}; for (const [k,v] of Object.entries(obj)) qs[k]={projections:v}; return { queryState: qs }; }

// objects entry helpers
const props = (o) => [{ properties: o }];
const propsSel = (o, id='default') => [{ properties: o, selector: { id } }];

// visualContainerObjects: title / background / border
function vcoTitle(text, { size=11, bold=true, color, align } = {}){
  const p = { show: lbool(true), text: lstr(text), fontSize: lnum(size) };
  if (bold) p.bold = lbool(true);
  if (color) p.fontColor = solid(color);
  if (align) p.alignment = lstr(align);
  return { title: props(p) };
}
function vcoBg(color, { transparency=0 } = {}){
  return { background: props({ show: lbool(true), color: solid(color), transparency: lnum(transparency) }) };
}
function vcoBorder(color='#E6E6EA', { radius } = {}){
  const p = { show: lbool(true), color: solid(color) };
  if (radius != null) p.radius = lnum(radius);
  return { border: props(p) };
}

// ---- base visual ----
function V(type, pos, body){
  const top = {};
  if (body && body.filterConfig){ body = Object.assign({}, body); top.filterConfig = body.filterConfig; delete body.filterConfig; }
  return Object.assign({
    $schema: VC,
    name: hexName(),
    position: { x: pos.x, y: pos.y, z: pos.z||5000, height: pos.h, width: pos.w, tabOrder: pos.tab||0 },
    visual: Object.assign({ visualType: type }, body, { drillFilterOtherVisuals: true }),
  }, top);
}

// ---- factories ----
// Empty background card (proven: textbox with bg+border renders as a card).
function cardBg(pos, { fill='#FFFFFF', border='#E6E6EA', radius=6 } = {}){
  return V('textbox', pos, {
    objects: { general: [{ properties: { paragraphs: [{ textRuns: [{ value: '' }] }] } }] },
    visualContainerObjects: Object.assign({}, vcoBg(fill), vcoBorder(border, { radius })),
  });
}

// Rich textbox from an array of runs [{t,size,color,bold,family}]
function textbox(pos, runs, { bg, border, align } = {}){
  const textRuns = runs.map(r => ({
    value: r.t,
    textStyle: Object.assign(
      { fontFamily: r.family || (r.bold ? 'Segoe UI Semibold' : 'Segoe UI'), fontSize: (r.size||11)+'pt' },
      r.color ? { color: r.color } : {},
      r.bold && !r.family ? { fontWeight: 'bold' } : {},
    ),
  }));
  const para = { textRuns };
  if (align) para.horizontalTextAlignment = align;
  const vco = {};
  if (bg) Object.assign(vco, vcoBg(bg));
  if (border) Object.assign(vco, vcoBorder(border));
  const body = { objects: { general: [{ properties: { paragraphs: [para] } }] } };
  if (Object.keys(vco).length) body.visualContainerObjects = vco;
  return V('textbox', pos, body);
}

// KPI big-number card
function kpiCard(pos, entity, measure, label, { unit, color='#1A1A2E', size=22, showLabel=false } = {}){
  const valProps = { fontSize: lnum(size), bold: lbool(true), fontColor: solid(color) };
  const objects = {
    value: propsSel(valProps),
    label: propsSel(showLabel
      ? { show: lbool(true), position: lstr('aboveValue'), fontSize: lnum(8), fontColor: solid('#5A5A6E') }
      : { show: lbool(false) }),
    divider: propsSel({ show: lbool(false) }),
  };
  if (unit != null) objects.value[0].properties.labelDisplayUnits = lstr(String(unit));
  return V('cardVisual', pos, {
    query: qstate({ Data: [ projM(entity, measure, { displayName: label }) ] }),
    objects,
    visualContainerObjects: { title: props({ show: lbool(false) }) },
  });
}

// KPI card bound to a base COLUMN with an implicit aggregation (Fleet/Flight pages).
// agg: 0=Sum, 1=Avg. Optional per-visual filter (filterCol=filterVal) and format string (fmt).
function kpiCardCol(pos, entity, col, label, { agg=0, unit, precision, fmt, color='#1A1A2E', size=18, showLabel=true, labelPos='belowValue', filterCol, filterVal } = {}){
  const valProps = { fontSize: lnum(size), bold: lbool(true), fontColor: solid(color) };
  if (unit != null) valProps.labelDisplayUnits = lstr(String(unit));
  if (precision != null) valProps.labelPrecision = lnum(precision);
  const objects = {
    value: propsSel(valProps),
    label: propsSel(showLabel
      ? { show: lbool(true), position: lstr(labelPos), fontSize: lnum(8), fontColor: solid('#6A6A7E') }
      : { show: lbool(false) }),
    divider: propsSel({ show: lbool(false) }),
  };
  const proj = projA(entity, col, agg, { displayName: label });
  if (fmt) proj.format = fmt;
  const body = {
    query: qstate({ Data: [ proj ] }),
    objects,
    visualContainerObjects: { title: props({ show: lbool(false) }) },
  };
  if (filterCol) body.filterConfig = vFilter(entity, filterCol, filterVal);
  return V('cardVisual', pos, body);
}
function deltaCard(pos, entity, measure, label, color){
  return V('cardVisual', pos, {
    query: qstate({ Data: [ projM(entity, measure, { displayName: label }) ] }),
    objects: {
      value: propsSel({ fontSize: lnum(11), bold: lbool(true), fontColor: solid(color) }),
      label: propsSel({ show: lbool(true), position: lstr('belowValue'), fontSize: lnum(8), fontColor: solid('#7A7A8E') }),
      divider: propsSel({ show: lbool(false) }),
    },
    visualContainerObjects: { title: props({ show: lbool(false) }) },
  });
}

// column chart (monthly). cat = {e,p,col:true}, y measure
function columnChart(pos, catE, catP, yE, yP, { title, catIsCol=true, sort } = {}){
  const cat = catIsCol ? projC(catE, catP, { active: true }) : projM(catE, catP);
  const body = {
    query: Object.assign(qstate({ Category: [cat], Y: [ projM(yE, yP) ] }),
      sort ? { sortDefinition: { sort: [{ field: sort==='cat'?colExpr(catE,catP):measExpr(yE,yP), direction: sort==='cat'?'Ascending':'Descending' }], isDefaultSort: false } } : {}),
    objects: {
      categoryAxis: propsSel({ show: lbool(true), fontSize: lnum(7), showAxisTitle: lbool(false) }),
      valueAxis: propsSel({ show: lbool(false) }),
      dataPoint: propsSel({ fill: { solid: { color: lstr('#B9BFD6') } } }),
      legend: propsSel({ show: lbool(false) }),
    },
    visualContainerObjects: {},
  };
  if (title) body.visualContainerObjects = vcoTitle(title, { size: 10 });
  else body.visualContainerObjects = { title: props({ show: lbool(false) }) };
  return V('columnChart', pos, body);
}

// horizontal bar breakdown: cat column, y measure, sorted desc
function barChart(pos, catE, catP, yE, yP, { title, color='#2E4C9A', topN, sortMeasure=true } = {}){
  const q = qstate({ Category: [ projC(catE, catP, { active: true }) ], Y: [ projM(yE, yP) ] });
  q.sortDefinition = { sort: [{ field: measExpr(yE,yP), direction: 'Descending' }], isDefaultSort: false };
  const body = {
    query: q,
    objects: {
      categoryAxis: propsSel({ show: lbool(true), fontSize: lnum(8), showAxisTitle: lbool(false) }),
      valueAxis: propsSel({ show: lbool(false) }),
      dataPoint: propsSel({ fill: { solid: { color: lstr(color) } } }),
      labels: propsSel({ show: lbool(true), fontSize: lnum(8) }),
      legend: propsSel({ show: lbool(false) }),
    },
    visualContainerObjects: title ? vcoTitle(title, { size: 10 }) : { title: props({ show: lbool(false) }) },
  };
  return V('barChart', pos, body);
}

// classic slicer (dropdown or list) bound to a parameter table column; single-select w/ default
function slicer(pos, entity, propName, { mode, defaultValue, header, numeric=false } = {}){
  const objects = {
    selection: props({ singleSelect: lbool(true), selectAllCheckboxEnabled: lbool(false) }),
    header: props({ show: header?lbool(true):lbool(false), text: header?lstr(header):lstr(''), textSize: lnum(9) }),
  };
  if (mode) objects.data = props({ mode: lstr(mode) });
  if (defaultValue != null){
    const litVal = numeric ? (defaultValue + 'L') : ("'" + String(defaultValue).replace(/'/g,"''") + "'");
    objects.general = props({
      filter: { filter: { Version: 2,
        From: [{ Name: 's', Entity: entity, Type: 0 }],
        Where: [{ Condition: { In: {
          Expressions: [{ Column: { Expression: { SourceRef: { Source: 's' } }, Property: propName } }],
          Values: [[ { Literal: { Value: litVal } } ]],
        } } }],
      } },
    });
  }
  return V('slicer', pos, {
    query: qstate({ Values: [ projC(entity, propName, { active: true }) ] }),
    objects,
  });
}

// pivotTable (heatmap) rows x cols x values
function pivotTable(pos, rows, cols, valE, valP, { title } = {}){
  return V('pivotTable', pos, {
    query: qstate({
      Rows: rows.map(r => projC(r.e, r.p, { active: true })),
      Columns: cols.map(c => projC(c.e, c.p, { active: true })),
      Values: [ projM(valE, valP) ],
    }),
    objects: {
      values: propsSel({ fontSize: lnum(8) }),
      columnHeaders: propsSel({ fontSize: lnum(8) }),
      rowHeaders: propsSel({ fontSize: lnum(8) }),
    },
    visualContainerObjects: title ? vcoTitle(title, { size: 10 }) : { title: props({ show: lbool(false) }) },
  });
}

// donut approximated as a card (single ratio; ring is decorative in source)
function ratioCard(pos, entity, measure, label){
  return V('cardVisual', pos, {
    query: qstate({ Data: [ projM(entity, measure, { displayName: label }) ] }),
    objects: {
      value: propsSel({ fontSize: lnum(28), bold: lbool(true), fontColor: solid('#2E4C9A') }),
      label: propsSel({ show: lbool(true), position: lstr('belowValue'), fontSize: lnum(8), fontColor: solid('#7A7A8E') }),
      divider: propsSel({ show: lbool(false) }),
    },
    visualContainerObjects: { title: props({ show: lbool(false) }) },
  });
}

// tableEx (flat list of columns). Optional per-visual filter + sort.
function tableEx(pos, columns, { title, filterCol, filterVal, sortCol, sortDir, sortEntity } = {}){
  const q = qstate({ Values: columns.map(c => c.measure ? projM(c.e, c.p, { active: true }) : projC(c.e, c.p, { active: true })) });
  if (sortCol){ q.sortDefinition = { sort: [{ field: colExpr(sortEntity||FA, sortCol), direction: sortDir||'Ascending' }], isDefaultSort: false }; }
  const body = {
    query: q,
    objects: {
      values: propsSel({ fontSize: lnum(8) }),
      columnHeaders: propsSel({ fontSize: lnum(8), bold: lbool(true) }),
    },
    visualContainerObjects: title ? vcoTitle(title, { size: 11 }) : { title: props({ show: lbool(false) }) },
  };
  if (filterCol) body.filterConfig = vFilter(sortEntity||FA, filterCol, filterVal);
  return V('tableEx', pos, body);
}

// azureMap bubble layer. Y=latitude, X=longitude (Avg-aggregated per Category), on a
// satellite basemap. NOTE: a great-circle route ARC would require Azure Map's PathID +
// PointOrder wells, which need one row per endpoint (path-shaped data). The frozen model
// stores origin+destination lat/long as 4 columns on a single flight-leg row, so the arc
// is NOT achievable without reshaping the model (forbidden). We plot destination points.
function azureMapPoints(pos, latE, latP, lonE, lonP, catE, catP, { title, style='satellite_road_labels', sizeE, sizeP, seriesE, seriesP } = {}){
  const roles = {
    Category: [ projC(catE, catP, { active: true }) ],
    Y: [ projA(latE, latP, 1) ],   // Avg latitude
    X: [ projA(lonE, lonP, 1) ],   // Avg longitude
  };
  if (sizeP) roles.Size = [ projM(sizeE, sizeP) ];
  if (seriesP) roles.Series = [ projC(seriesE, seriesP, { active: true }) ];
  return V('azureMap', pos, {
    query: qstate(roles),
    objects: {
      mapControls: propsSel({ defaultStyle: lstr(style), autoZoom: lbool(true), showLabels: lbool(true), worldWrap: lbool(false) }),
      bubbleLayer: propsSel({ show: lbool(true), bubbleRadius: lnum(6, 'L') }),
      pathLayer: propsSel({ show: lbool(false) }),
    },
    visualContainerObjects: title ? vcoTitle(title, { size: 11 }) : { title: props({ show: lbool(false) }) },
  });
}

// pageNavigator
function pageNav(pos){
  return V('pageNavigator', pos, {
    objects: {
      text: propsSel({ fontSize: lnum(10) }),
    },
    visualContainerObjects: { background: props({ show: lbool(false) }) },
  });
}

// ============================================================================
// SHARED CHROME
// ============================================================================
const HEADER_H = 118, SIDE_W = 250;
const NAVY = '#141B34', NAVY2 = '#1E2749', GREEN = '#2E9E5B', RED = '#C0392B', BLUE = '#2E4C9A';

function chrome(pageTitle, greeting='Hello, Guest!'){
  const out = [];
  // logo box (white) top-left
  out.push(textbox({ x: 0, y: 0, w: SIDE_W, h: HEADER_H, z: 1000 },
    [{ t: '  AeroLink', size: 20, bold: true, color: '#141B34', family: 'Segoe UI Semibold' }],
    { bg: '#FFFFFF' }));
  // navy header band
  out.push(textbox({ x: SIDE_W, y: 0, w: PAGE_W - SIDE_W, h: HEADER_H, z: 1000 },
    [
      { t: 'Airline Alliance Activity Dashboard ', size: 20, bold: true, color: '#FFFFFF', family: 'Segoe UI Semibold' },
      { t: '| ' + pageTitle, size: 13, color: '#AEB6D6' },
      { t: '\nWelcome! Follow all of AeroLink Alliance\u2019s activities from 2022 to 2025.', size: 9, color: '#AEB6D6' },
    ],
    { bg: NAVY }));
  // greeting (right)
  out.push(textbox({ x: PAGE_W - 260, y: 34, w: 240, h: 48, z: 1500 },
    [
      { t: greeting + '\n', size: 10, bold: true, color: '#FFFFFF', family: 'Segoe UI Semibold' },
      { t: 'Last Login: 16 Jul 2026', size: 8, color: '#8CE0A6' },
    ], {}));
  // sidebar (white)
  out.push(textbox({ x: 0, y: HEADER_H, w: SIDE_W, h: PAGE_H - HEADER_H, z: 900 }, [{ t: '' }], { bg: '#FFFFFF', border: '#ECECEC' }));
  // content bg (light)
  out.push(textbox({ x: SIDE_W, y: HEADER_H, w: PAGE_W - SIDE_W, h: PAGE_H - HEADER_H, z: 800 }, [{ t: '' }], { bg: '#F3F5F9' }));
  return out;
}

// VIEW SELECTIONS block for the sidebar. `which` picks which slicers.
// realCols=true binds Region/Airline/Aircraft to the REAL fact columns (Fleet/Flight
// pages, where base-column aggregations must respond to the slicer) instead of the
// disconnected parameter tables. date=false drops the Date (Year/Month) block.
function sidebarControls({ date=true, region=false, airline=false, aircraft=false, realCols=false } = {}){
  const out = [];
  const LX = 18, LW = 214;
  // NAVIGATION header + navigator
  out.push(textbox({ x: LX, y: HEADER_H + 12, w: LW, h: 34, z: 2000 },
    [{ t: 'NAVIGATION', size: 10, bold: true, color: '#8A8A9A', family: 'Segoe UI Semibold' }]));
  out.push(pageNav({ x: LX - 2, y: HEADER_H + 46, w: LW + 4, h: 140, z: 2000 }));
  // VIEW SELECTIONS header
  let y = HEADER_H + 200;
  out.push(textbox({ x: LX, y, w: LW, h: 34, z: 2000 },
    [{ t: 'VIEW SELECTIONS', size: 10, bold: true, color: '#8A8A9A', family: 'Segoe UI Semibold' }]));
  y += 36;
  const label = (txt) => { out.push(textbox({ x: LX, y, w: LW, h: 34, z: 2000 }, [{ t: txt, size: 9, bold: true, color: '#4A4A5A' }])); y += 34; };
  const sl = (entity, prop, def, numeric) => { out.push(slicer({ x: LX, y, w: LW, h: 56, z: 2000 }, entity, prop, { defaultValue: def, numeric, mode: 'Dropdown' })); y += 62; };
  // Date (Year + Month)
  if (date){ label('\uD83D\uDCC5  Date'); sl(P_YEAR, 'Year', 2023, true); sl(P_MONTH, 'Month', 7, true); }
  if (region){ label('\uD83C\uDF10  Region'); if (realCols) sl(FA, 'Origin Region', 'North America', false); else sl(P_REGION, 'Region', 'North America', false); }
  if (airline){ label('\u2708\uFE0F  Airline'); if (realCols) sl(FA, 'Airline Name', 'SkyConnect Airways', false); else sl(P_AIRLINE, 'Airline', 'SkyConnect Airways', false); }
  if (aircraft){ label('\uD83D\uDEE9\uFE0F  Aircraft Type'); if (realCols) sl(FA, 'Aircraft Type', 'A330', false); else sl(P_AIRCRAFT, 'Aircraft Type', 'A330', false); }
  return out;
}

// ============================================================================
// PAGE: ALLIANCE
// ============================================================================
function pageAlliance(){
  const D = 'Alliance Page';
  const v = [];
  v.push(...chrome('Alliance'));
  v.push(...sidebarControls({ region: true }));

  // comparison legend caption row (below header)
  v.push(textbox({ x: SIDE_W + 12, y: HEADER_H + 4, w: PAGE_W - SIDE_W - 24, h: 34, z: 2500 }, [
    { t: '\u25CF ', size: 8, color: '#2E7D5B' }, { t: 'Month of CY > PY,   ', size: 8, color: '#5A5A6E' },
    { t: '\u25CF ', size: 8, color: '#B3305C' }, { t: 'Month of CY < PY,   ', size: 8, color: '#5A5A6E' },
    { t: '\u25CF ', size: 8, color: '#8A8A9A' }, { t: 'Month of CY = PY        ', size: 8, color: '#5A5A6E' },
    { t: '\u25A0 ', size: 8, color: BLUE }, { t: 'Current Month | ', size: 8, color: '#5A5A6E' },
    { t: '\u25A0 ', size: 8, color: '#B9BFD6' }, { t: 'Prev Month', size: 8, color: '#5A5A6E' },
  ]));
  const tiles = [
    ['Completed Flights',        'CM No of Comp Flights', 'Pos MoM Comp Flights', 'CY No of Comp Flights', 'CM No of Comp Flights', 9,   { }],
    ['On-Time Performance',      'CM On-Time Perf%',      'Pos MoM On-Time Perf', 'CY On-Time Perf%',      'CM On-Time Perf%',      10,  { }],
    ['Revenue Passenger Km (RPK)','CM RPK',               'Pos MoM RPK',          'CY RPK',                'CM RPK',                104, { unit: '1000000000' }],
    ['Avg. Satisfaction Score',  'CM Avg. CSAT',          'Pos MoM Avg. CSAT',    'CY Avg. CSAT',          'CM Avg. CSAT',          113, { }],
  ];
  for (const [name, big, delta, bars, brk, rowId, opt] of tiles){
    const row = zpx(D, rowId);
    // tile card background (inset 4px)
    v.push(cardBg({ x: row.x + 3, y: row.y + 3, w: row.w - 6, h: row.h - 8, z: 3000 }));
    // ban block (left ~26%)
    const banX = row.x + 12, banW = 150;
    v.push(textbox({ x: banX, y: row.y + 14, w: banW, h: 34, z: 5000 }, [{ t: name, size: 11, bold: true, color: '#2B2B3A' }]));
    v.push(kpiCard({ x: banX - 4, y: row.y + 50, w: banW, h: 46, z: 5000 }, FA, big, name, opt));
    v.push(deltaCard({ x: banX, y: row.y + 98, w: banW, h: 30, z: 5000 }, FA, delta, 'vs. Prev. Month', GREEN));
    // monthly bars (middle)
    const barsX = row.x + 175, barsW = 195;
    v.push(columnChart({ x: barsX, y: row.y + 16, w: barsW, h: row.h - 34, z: 5000 }, DATE, 'Month', FA, bars, { catIsCol: true, sort: 'cat' }));
    // per-airline breakdown (right)
    const brkX = row.x + 380, brkW = row.w - 392;
    v.push(barChart({ x: brkX, y: row.y + 16, w: brkW, h: row.h - 34, z: 5000 }, FA, 'Airline Name', FA, brk, { color: BLUE }));
  }

  // RIGHT COLUMN
  // Top Flight Destinations (id=43 title 128 + ws 125)
  let z = zpx(D, 43);
  v.push(cardBg({ x: z.x + 3, y: z.y + 3, w: z.w - 6, h: z.h - 8, z: 3000 }));
  v.push(textbox({ x: z.x + 14, y: z.y + 12, w: z.w - 28, h: 34, z: 5000 }, [{ t: 'Top Flight Destinations \u2013 ', size: 11, bold: true, color: '#2B2B3A' }, { t: 'North America', size: 11, bold: true, color: BLUE }]));
  v.push(barChart({ x: z.x + 12, y: z.y + 48, w: z.w - 24, h: z.h - 62, z: 5000 }, FA, 'Destination City', FA, 'CM Comp Flights to Destination', { color: BLUE, topN: 5 }));

  // Major Flight Times heatmap (id=44)
  z = zpx(D, 44);
  v.push(cardBg({ x: z.x + 3, y: z.y + 3, w: z.w - 6, h: z.h - 8, z: 3000 }));
  v.push(textbox({ x: z.x + 14, y: z.y + 8, w: z.w - 28, h: 34, z: 5000 }, [{ t: 'Major Flight Times', size: 11, bold: true, color: '#2B2B3A' }]));
  v.push(pivotTable({ x: z.x + 12, y: z.y + 42, w: z.w - 24, h: z.h - 54, z: 5000 },
    [{ e: FA, p: 'Day Of Week' }], [{ e: DATE, p: 'Month' }], FA, 'CY All Flights'));

  // Aircraft Utilization (id=45) — two bar charts
  z = zpx(D, 45);
  v.push(cardBg({ x: z.x + 3, y: z.y + 3, w: z.w - 6, h: z.h - 8, z: 3000 }));
  v.push(textbox({ x: z.x + 14, y: z.y + 8, w: z.w - 28, h: 34, z: 5000 }, [{ t: 'Aircraft Utilization\n', size: 11, bold: true, color: '#2B2B3A' }, { t: 'By Completed Flights (vs PM)   |   By Avg. Load Factor (vs PM)', size: 8, color: '#7A7A8E' }]));
  v.push(barChart({ x: z.x + 12, y: z.y + 46, w: (z.w-24)/2 - 4, h: z.h - 58, z: 5000 }, FA, 'Aircraft Type', FA, 'CM No of Comp Flights', { color: BLUE }));
  v.push(barChart({ x: z.x + 12 + (z.w-24)/2 + 4, y: z.y + 46, w: (z.w-24)/2 - 4, h: z.h - 58, z: 5000 }, FA, 'Aircraft Type', FA, 'CM Load Factor', { color: '#7A2E4C' }));

  // Baggage Mishandling (id=169): donut/card (148) + airlines bar (173)
  z = zpx(D, 169);
  v.push(cardBg({ x: z.x + 3, y: z.y + 3, w: z.w - 6, h: z.h - 8, z: 3000 }));
  const zc = zpx(D, 176); // title zone
  v.push(textbox({ x: z.x + 14, y: z.y + 10, w: 150, h: 40, z: 5000 }, [{ t: 'Flights with Baggage Mishandling', size: 10, bold: true, color: '#2B2B3A' }]));
  const zdon = zpx(D, 148);
  v.push(ratioCard({ x: zdon.x, y: zdon.y, w: zdon.w, h: zdon.h, z: 5000 }, FA, 'CM % of Mishandled Baggage', 'Mishandled'));
  const zbag = zpx(D, 173);
  v.push(barChart({ x: zbag.x + 4, y: zbag.y + 6, w: zbag.w - 8, h: zbag.h - 12, z: 5000 }, FA, 'Airline Name', FA, 'CM % of Mishandled Baggage', { color: BLUE }));

  return { display: 'Alliance', visuals: v };
}

// ============================================================================
// AIRLINES PAGE (dash idx0) — selected-airline detail
// ============================================================================
function pageAirlines(){
  const D = 'Airlines Page';
  const v = [];
  v.push(...chrome('Airlines'));
  v.push(...sidebarControls({ region: true, airline: true }));

  // ---- Airline Summary panel ----
  const sp = zpx(D, 13);
  v.push(cardBg({ x: sp.x, y: sp.y - 4, w: sp.w, h: sp.h + 6, z: 3000 }));
  const st = zpx(D, 19);
  v.push(textbox({ x: st.x, y: sp.y + 4, w: st.w, h: 37, z: 5000 }, [{ t: 'Airline Summary', size: 13, bold: true, color: '#2B2B3A' }]));

  // summary KPI cell: name (card label) + big value + delta (or placeholder '\u2014')
  function summaryKPI(cell, name, big, delta, opt = {}){
    if (big){
      v.push(kpiCard({ x: cell.x, y: cell.y + 4, w: cell.w * 0.60, h: cell.h - 8, z: 5000 }, FA, big, name, Object.assign({ size: 15, showLabel: true }, opt)));
      if (delta) v.push(deltaCard({ x: cell.x + cell.w * 0.63, y: cell.y + 12, w: cell.w * 0.37 - 10, h: 30, z: 5000 }, FA, delta, 'vs. PM', GREEN));
    } else {
      v.push(textbox({ x: cell.x, y: cell.y + 4, w: cell.w - 8, h: cell.h - 8, z: 5000 }, [
        { t: name + '\n', size: 9, bold: true, color: '#5A5A6E' },
        { t: '\u2014  ', size: 14, bold: true, color: '#9A9AA8' },
        { t: '(not in model)', size: 7, color: '#B0B0BE' },
      ]));
    }
  }
  const r1 = zpx(D, 185), r2 = zpx(D, 192);
  const cw1 = r1.w / 3, cw2 = r2.w / 3;
  const cell = (r, cw, i) => ({ x: r.x + cw * i + 8, y: r.y, w: cw - 8, h: r.h });
  // row 1
  summaryKPI(cell(r1, cw1, 0), 'Completed Flights', 'CM No of Comp Flights', 'Pos MoM Comp Flights');
  summaryKPI(cell(r1, cw1, 1), 'Avg. Distance Covered', null, null);
  summaryKPI(cell(r1, cw1, 2), 'On-Time Performance', 'CM On-Time Perf%', 'Pos MoM On-Time Perf');
  // row 2
  summaryKPI(cell(r2, cw2, 0), 'Airline Hub', null, null);
  summaryKPI(cell(r2, cw2, 1), 'Major Flight Route', null, null);
  summaryKPI(cell(r2, cw2, 2), 'Avg. CSAT Score', 'CM Avg. CSAT', 'Pos MoM Avg. CSAT');

  // comparison legend caption (below summary)
  v.push(textbox({ x: sp.x + 4, y: sp.y + sp.h + 6, w: sp.w - 8, h: 34, z: 2500 }, [
    { t: '\u25CF ', size: 8, color: '#2E7D5B' }, { t: 'Month of CY > PY (\u25CF for Costs),   ', size: 8, color: '#5A5A6E' },
    { t: '\u25CF ', size: 8, color: '#B3305C' }, { t: 'Month of CY < PY (\u25CF for Costs),   ', size: 8, color: '#5A5A6E' },
    { t: '\u25CF ', size: 8, color: '#8A8A9A' }, { t: 'Month of CY = PY', size: 8, color: '#5A5A6E' },
  ]));

  // ---- 3 metric-category columns ----
  const RPK_B = '1000000000', MILL = '1000000';
  const cols = [
    { titleZ: 31, colZ: 21, title: 'Passenger Traffic Metrics', groups: [
      { name: 'All Passengers',                banZ: 42,  barsZ: 48,  big: 'CM Passenger Count',     delta: 'Pos MoM Passenger Count',    bars: 'CY Passenger Count' },
      { name: 'Revenue Passenger\nKilometres (RPK)', banZ: 56, barsZ: 61, big: 'CM RPK (A)',         delta: 'Pos MoM RPK (A)',            bars: 'CY RPK (A)',        unit: RPK_B },
      { name: 'Passenger Load\nFactor (PLF)',  banZ: 70,  barsZ: 75,  big: 'CM Load Factor',         delta: 'Pos MoM Load Factor',        bars: 'CY Load Factor' },
    ]},
    { titleZ: 38, colZ: 23, title: 'Cargo Traffic Metrics', groups: [
      { name: 'Freight Tonnes',                banZ: 84,  barsZ: 89,  big: 'CM Freight Tonnes',      delta: 'Pos MoM Freight Tonnes',     bars: 'CY Freight Tonnes' },
      { name: 'Cargo Tonnes\nKilometres (CTK)',banZ: 96,  barsZ: 101, big: 'CM CTK',                delta: 'Pos MoM CTK',                bars: 'CY CTK',            unit: MILL },
      { name: 'Cargo Load Factor\n(CLF)',      banZ: 110, barsZ: 115, big: 'CM Cargo Load Factor',   delta: 'Pos MoM Cargo Load Factor',  bars: 'CY Cargo Load Factor' },
    ]},
    { titleZ: 144, colZ: 22, title: 'Cost & Revenue Metrics', groups: [
      { name: 'Total Revenue',                 banZ: 152, barsZ: 159, big: 'CM Total Revenue',       delta: 'Pos MoM Total Revenue',      bars: 'CY Total Revenue',  unit: MILL },
      { name: 'Total Operating\nProfit',       banZ: 168, barsZ: 173, big: 'CM Op Profit',           delta: 'Pos MoM Op Profit',          bars: 'CY Op Profit',      unit: MILL },
      { name: 'Total Operating\nCost',         banZ: 199, barsZ: 204, big: 'CM Costs',               delta: 'Pos MoM Costs',              bars: 'CY Costs',          unit: MILL },
    ]},
  ];
  for (const col of cols){
    const cz = zpx(D, col.colZ);
    v.push(cardBg({ x: cz.x, y: cz.y - 2, w: cz.w, h: cz.h + 4, z: 3000 }));
    const tz = zpx(D, col.titleZ);
    v.push(textbox({ x: tz.x, y: cz.y + 2, w: tz.w, h: 36, z: 5000 }, [{ t: col.title, size: 12, bold: true, color: '#2B2B3A' }]));
    for (const g of col.groups){
      const bz = zpx(D, g.banZ), rz = zpx(D, g.barsZ);
      // ban block: name + big value + delta (kept within ban-zone width so it never overlaps the bars)
      v.push(textbox({ x: bz.x, y: bz.y + 8, w: bz.w, h: 40, z: 5000 }, [{ t: g.name, size: 9, bold: true, color: '#4A4A5A' }]));
      v.push(kpiCard({ x: bz.x, y: bz.y + 52, w: bz.w, h: 40, z: 5000 }, FA, g.big, g.name, { size: 18, unit: g.unit }));
      v.push(deltaCard({ x: bz.x, y: bz.y + 96, w: bz.w, h: 26, z: 5000 }, FA, g.delta, 'vs. Prev. Month', GREEN));
      // monthly bars
      v.push(columnChart({ x: rz.x, y: rz.y, w: rz.w, h: rz.h + 20, z: 5000 }, DATE, 'Month', FA, g.bars, { catIsCol: true, sort: 'cat' }));
    }
  }

  return { display: 'Airlines', visuals: v };
}

// ============================================================================
// FLEET PAGE (dash idx2) — aircraft gallery + selected-aircraft detail.
// Base-column aggregations + REAL-column slicers (Airline Name / Origin Region);
// aircraft "selected" via a per-visual Aircraft Type filter (static A330, matching
// the reference). No date slicer on this page. Aircraft photos unavailable -> styled
// placeholders. Interactive per-aircraft "Details" drill not reproduced (documented).
// ============================================================================
function pageFleet(){
  const D = 'Fleet Page';
  const SEL = 'A330';                 // selected aircraft (reference default)
  const v = [];
  v.push(...chrome('Fleet'));
  v.push(...sidebarControls({ date: false, airline: true, region: true, realCols: true }));

  // ---- aircraft gallery (2 cols x 3 rows) ----
  const GAL = [
    { t: 32, type: 'A320', name: 'Airbus A320' },
    { t: 33, type: 'A330', name: 'Airbus A330' },
    { t: 38, type: 'A350', name: 'Airbus A350' },
    { t: 39, type: 'B737', name: 'Boeing B737' },
    { t: 43, type: 'B777', name: 'Boeing B777' },
    { t: 44, type: 'B787', name: 'Boeing B787' },
  ];
  for (const g of GAL){
    const b = zpx(D, g.t);
    v.push(cardBg({ x: b.x + 3, y: b.y + 3, w: b.w - 6, h: b.h - 8, z: 3000 }));
    const phH = 100;
    // photo placeholder (assets unavailable)
    v.push(textbox({ x: b.x + 10, y: b.y + 8, w: b.w - 20, h: phH, z: 4200 },
      [{ t: '\u2708\uFE0F  ' + g.type, size: 20, bold: true, color: '#AEB6CE' }], { bg: '#EEF1F6', border: '#E1E5EE', align: 'center' }));
    // name
    v.push(textbox({ x: b.x + 10, y: b.y + phH + 14, w: b.w - 20, h: 35, z: 5000 },
      [{ t: g.name, size: 12, bold: true, color: '#2B2B3A' }], { align: 'center' }));
    // stats (two cards, full numbers) — filtered to this aircraft type
    const sy = b.y + phH + 52;
    const cw = (b.w - 24) / 2;
    v.push(kpiCardCol({ x: b.x + 10, y: sy, w: cw, h: 40, z: 5000 }, FA, 'Completed Flights', 'completed flights', { agg: 0, size: 12, filterCol: 'Aircraft Type', filterVal: g.type }));
    v.push(kpiCardCol({ x: b.x + 10 + cw + 4, y: sy, w: cw, h: 40, z: 5000 }, FA, 'Passengers Carried', 'passengers', { agg: 0, size: 12, filterCol: 'Aircraft Type', filterVal: g.type }));
    // Details button (decorative — interactive drill not reproduced)
    v.push(textbox({ x: b.x + (b.w - 100) / 2, y: b.y + b.h - 44, w: 100, h: 34, z: 5000 },
      [{ t: 'Details', size: 9, bold: true, color: '#2E4C9A' }], { bg: '#FFFFFF', border: '#2E4C9A', align: 'center' }));
  }

  // ---- right panel: selected aircraft (static A330) ----
  const rp = zpx(D, 86);
  v.push(cardBg({ x: rp.x, y: rp.y - 2, w: rp.w, h: rp.h + 6, z: 2900 }, { border: '#D8D8E0' }));
  const tz = zpx(D, 83);
  v.push(textbox({ x: tz.x, y: tz.y + 6, w: tz.w, h: 40, z: 5000 }, [{ t: 'Airbus A330', size: 15, bold: true, color: '#2B2B3A' }]));
  const pz = zpx(D, 82);
  v.push(textbox({ x: pz.x + 8, y: pz.y, w: pz.w - 16, h: pz.h, z: 4200 },
    [{ t: '\u2708\uFE0F  Airbus A330', size: 22, bold: true, color: '#AEB6CE' }], { bg: '#F1F3F8', border: '#E1E5EE', align: 'center' }));

  // ---- 6 KPI grid (panel 92), all filtered to the selected aircraft ----
  const kp = zpx(D, 92);
  v.push(cardBg({ x: kp.x, y: kp.y, w: kp.w, h: kp.h, z: 3000 }, { fill: '#F8F8FA' }));
  const kpad = 12, kcw = (kp.w - kpad*2 - 10) / 2, kch = (kp.h - kpad*2) / 3;
  const kcell = (r, c) => ({ x: kp.x + kpad + c*(kcw+10), y: kp.y + kpad + r*kch, w: kcw, h: kch - 6, z: 5000 });
  const KPI = [
    [0,0,'Completed Flights',        'Completed Flights',  0, {}],
    [0,1,'All Passengers',           'Passengers Carried', 0, { unit: '1000000',    precision: 2 }],
    [1,0,'Avg. Distance Covered (km)','Distance Km',        1, {}],
    [1,1,'Avg. Load Factor',         'Load Factor',        1, { fmt: '0.00%' }],
    [2,0,'Avg. CO\u2082 Emissions (kg)','Co2 Emissions Kg', 1, { unit: '1000',       precision: 2 }],
    [2,1,'Total Fuel Cost ($)',      'Fuel Cost Usd',      0, { unit: '1000000000', precision: 2 }],
  ];
  for (const [r,c,label,col,agg,opt] of KPI){
    v.push(kpiCardCol(kcell(r,c), FA, col, label, Object.assign({ agg, size: 17, labelPos: 'aboveValue', filterCol: 'Aircraft Type', filterVal: SEL }, opt)));
  }

  // ---- recent completed flights (panel 93) ----
  const fp = zpx(D, 93);
  v.push(cardBg({ x: fp.x, y: fp.y, w: fp.w, h: fp.h, z: 3000 }, { fill: '#F8F8FA' }));
  const ftz = zpx(D, 97);
  v.push(textbox({ x: ftz.x, y: ftz.y, w: ftz.w, h: 35, z: 5000 }, [{ t: 'Recent Completed Flights', size: 12, bold: true, color: '#2B2B3A' }]));
  const flz = zpx(D, 98);
  v.push(tableEx({ x: flz.x, y: flz.y + 8, w: flz.w, h: flz.h - 8, z: 5000 }, [
    { e: FA, p: 'Date' }, { e: FA, p: 'Flight Id' }, { e: FA, p: 'Route' },
    { e: FA, p: 'Average Fare Usd' }, { e: FA, p: 'Passengers Carried' }, { e: FA, p: 'Flight Duration Hours' },
  ], { filterCol: 'Aircraft Type', filterVal: SEL, sortCol: 'Date', sortDir: 'Descending' }));

  return { display: 'Fleet', visuals: v };
}

// ============================================================================
// FLIGHT PAGE (dash idx3) — recent-flights list + route MAP (capability centerpiece).
// List: base-column table (real Airline/Region slicers, most-recent first).
// Map: azureMap destination bubbles on a satellite basemap. The Tableau great-circle
// route ARC (MAKELINE geometry, one selected flight) has NO native Power BI equivalent
// and the frozen model stores origin+dest lat/long as 4 columns on one row (cannot feed
// Azure Map's PathID/PointOrder path layer without reshaping) -> documented fidelity gap.
// ============================================================================
function pageFlight(){
  const D = 'Flight Page';
  const v = [];
  v.push(...chrome('Flights'));
  v.push(...sidebarControls({ date: false, airline: true, region: true, realCols: true }));

  // white content panel
  const cp = zpx(D, 3);
  v.push(cardBg({ x: cp.x, y: cp.y, w: cp.w, h: cp.h, z: 2900 }, { border: '#D8D8E0' }));

  // ---- recent flights list (id5/7) ----
  const lz = zpx(D, 5);
  v.push(tableEx({ x: lz.x, y: lz.y, w: lz.w, h: lz.h, z: 5000 }, [
    { e: FA, p: 'Date' }, { e: FA, p: 'Flight Id' },
    { e: FA, p: 'Origin City' }, { e: FA, p: 'Destination City' },
    { e: FA, p: 'Aircraft Type' }, { e: FA, p: 'Average Fare Usd' },
    { e: FA, p: 'Passengers Carried' }, { e: FA, p: 'Flight Duration Hours' },
  ], { sortCol: 'Date', sortDir: 'Descending' }));

  // ---- flight route map (id6/20) — destination bubbles on satellite ----
  const mz = zpx(D, 20);
  v.push(azureMapPoints({ x: mz.x, y: mz.y, w: mz.w, h: mz.h - 44, z: 5000 },
    FA, 'Destination Latitude', FA, 'Destination Longitude', FA, 'Destination City', { style: 'satellite_road_labels' }));
  // on-canvas fidelity note
  v.push(textbox({ x: mz.x, y: mz.y + mz.h - 40, w: mz.w, h: 34, z: 5000 },
    [{ t: 'Map plots recent-flight destination points. Tableau\u2019s great-circle route arc (MAKELINE geometry) has no native Power BI equivalent \u2014 see report notes.', size: 7, color: '#8A8A9A' }], { bg: '#FFFFFF' }));

  return { display: 'Flights', visuals: v };
}
function rimraf(p){ if (fs.existsSync(p)) fs.rmSync(p, { recursive: true, force: true }); }
function writeJson(p, obj){ fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, JSON.stringify(obj, null, 2)); }

function build(){
  rimraf(REPORT);
  fs.mkdirSync(PAGES_DIR, { recursive: true });

  // .pbip
  writeJson(path.join(FAB, 'AirlineAllianceActivity.pbip'), {
    $schema: 'https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json',
    version: '1.0',
    artifacts: [{ report: { path: 'AirlineAllianceActivity.Report' } }],
    settings: { enableAutoRecovery: true },
  });
  // definition.pbir
  writeJson(path.join(REPORT, 'definition.pbir'), {
    $schema: 'https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json',
    version: '4.0',
    datasetReference: { byPath: { path: '../AirlineAllianceActivity.SemanticModel' } },
  });
  // version.json
  writeJson(path.join(DEF, 'version.json'), {
    $schema: 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json',
    version: '2.0.0',
  });
  // report.json (proven sibling shape — report/3.3.0 requires themeCollection)
  writeJson(path.join(DEF, 'report.json'), {
    $schema: 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.3.0/schema.json',
    themeCollection: {
      baseTheme: { name: 'CY24SU10', reportVersionAtImport: { visual: '1.8.97', report: '2.0.97', page: '1.3.97' }, type: 'SharedResources' },
      customTheme: { name: 'theme.json', reportVersionAtImport: { visual: '1.8.100', report: '2.0.100', page: '1.3.100' }, type: 'RegisteredResources' },
    },
    objects: { outspacePane: [{ properties: { expanded: { expr: { Literal: { Value: 'false' } } }, visible: { expr: { Literal: { Value: 'true' } } } } }] },
    resourcePackages: [
      { name: 'SharedResources', type: 'SharedResources', items: [{ name: 'CY24SU10', path: 'BaseThemes/CY24SU10.json', type: 'BaseTheme' }] },
      { name: 'RegisteredResources', type: 'RegisteredResources', items: [{ name: 'theme.json', path: 'theme.json', type: 'CustomTheme' }] },
    ],
    settings: { useStylableVisualContainerHeader: true, defaultFilterActionIsDataFilter: true, defaultDrillFilterOtherVisuals: true, allowChangeFilterTypes: true, allowInlineExploration: true, useEnhancedTooltips: true },
    slowDataSourceSettings: { isCrossHighlightingDisabled: false, isSlicerSelectionsButtonEnabled: false, isFilterSelectionsButtonEnabled: false, isFieldWellButtonEnabled: false, isApplyAllButtonEnabled: false },
  });
  // custom theme
  writeJson(path.join(REPORT, 'StaticResources', 'RegisteredResources', 'theme.json'), {
    name: 'theme.json',
    dataColors: ['#2E4C9A', '#7A2E4C', '#2E9E5B', '#E1A730', '#5B7FD1', '#9A6B2E', '#4AAE8C', '#B0486B'],
    background: '#FFFFFF', foreground: '#2B2B3A', tableAccent: '#2E4C9A',
    maximum: '#2E4C9A', center: '#C6CEE4', minimum: '#F1F3F9',
  });

  const PAGES = [ pageAlliance(), pageAirlines(), pageFleet(), pageFlight() ];

  const pageNames = [];
  for (const pg of PAGES){
    const pname = hexName();
    pageNames.push(pname);
    const pdir = path.join(PAGES_DIR, pname);
    writeJson(path.join(pdir, 'page.json'), {
      $schema: 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/1.4.0/schema.json',
      name: pname,
      displayName: pg.display,
      displayOption: 'FitToPage',
      height: PAGE_H,
      width: PAGE_W,
    });
    for (const vis of pg.visuals){
      writeJson(path.join(pdir, 'visuals', vis.name, 'visual.json'), vis);
    }
  }
  // pages.json
  writeJson(path.join(PAGES_DIR, 'pages.json'), {
    $schema: 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json',
    pageOrder: pageNames,
    activePageName: pageNames[0],
  });

  console.log('Built report with', PAGES.length, 'page(s).');
  for (const pg of PAGES) console.log('  -', pg.display, ':', pg.visuals.length, 'visuals');
}

build();
