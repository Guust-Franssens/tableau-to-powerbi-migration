// Append report_build limitations to migration-spec.json. Idempotent: skips any
// entry whose (stage, item) already exists. Uses JSON.parse/stringify (never regex).
import { readFileSync, writeFileSync } from "node:fs";

const SPEC = "C:/Users/gfranssens/vscode-projects/tableau-to-pbi-migration/migrations/quadruple-axis-charts/migration-spec.json";
const spec = JSON.parse(readFileSync(SPEC, "utf8"));
spec.limitations_encountered ||= [];

const S = "report_build";
const add = [
  // ---- HIGH: N-axis stacking geometry ----
  { item: "report.multiaxis.line_area_stack", severity: "high", stage: S,
    issue: "Ex.1 Line/Area++ & Ex.2 Line+++ stack an Area + Dot + Line + Circle (and a deep-discount Dot) as 4-5 synthetic mark layers pinned on ONE sales axis via duplicated Rows measures. Power BI caps at dual-axis; rebuilt as a single lineChart over Ship Month with markers, binding the real windowed measures (Total Sales, Dot Sales Window, Circle Sales Window, Deep Discount Dot). The layered area-fill + N stacked mark geometry is NOT reproduced (capability boundary)." },
  { item: "report.multiaxis.bar_hashing_quad_axis", severity: "high", stage: S,
    issue: "Ex.7 Bar Hashing fakes a quad-axis by stacking Profit Ratio windows (Triangles/Dot/Hash Profit Ratio Window measures) plus hatch/hash fills driven by the Select Hashing param. Rebuilt as two dual-axis combos: Profit Ratio columns vs a dynamic Profit Ratio Goal Value line on a shared axis. Hatch/hash fills and the N>2 synthetic axis stack are NOT reproducible in native Power BI (capability boundary)." },
  { item: "report.multiaxis.bar_in_bar_overlay", severity: "high", stage: S,
    issue: "Ex.9 Bar/Line/Dot (Bar-in-Bar Measure vs Dynamic + Measure Values) overlays different-WIDTH bars on a shared axis (a wide Sales bar behind a narrow Variable Metric bar) plus unicode reference marks - a geometry Power BI cannot encode. Rebuilt as a dual-axis combo (Total Sales columns + Variable Metric COUNTD line) over Order Quarter, plus a clustered column of Sales West/East (the resolved Measure Names/Values fields). Overlapping-width bar-in-bar geometry is a capability boundary." },
  // ---- HIGH: constant axis-anchor placeholders ----
  { item: "report.axis_anchor_placeholders", severity: "high", stage: S,
    issue: "The constant axis-anchor measures that make the fake axes line up (Axis Anchor One = MAX(1.0), Axis Anchor Zero = MAX(0.0)/0, and the normalized 5th/6th-axis pins) exist in the model but are intentionally NOT bound to any visual: Power BI has no synthetic secondary/tertiary axis to pin a constant to, so an anchor placeholder has no native target. They are the mechanism of the N-axis trick and are dropped in the report layer (capability boundary)." },
  // ---- HIGH: unicode shape / glyph mark encodings ----
  { item: "report.shape_marks.triangles", severity: "high", stage: S,
    issue: "Ex.3 Triangles x4 uses Tableau 'Shape' marks (four triangle glyphs per Sub-Category across profit-ratio windows). Rebuilt as a Sales-vs-Profit scatter by Sub-Category (South), bubble size = Quantity, preserving the underlying data. Arbitrary unicode/shape mark geometry and the 4 stacked windows are NOT reproducible as native data marks (capability boundary)." },
  { item: "report.glyph_marks.lbar_and_indicators", severity: "high", stage: S,
    issue: "Ex.8 L-Bar/Dot draws an L-shaped bar with check/dot glyph marks; several sheets also encode unicode indicator strings (Ship Mode Most Profitable Icon, Profit Ratio +/- Symbol, Star Rating, 3x3/1x9 indicator grids, Select Hashing glyphs). Rebuilt Ex.8 as a dual-axis combo (Total Sales columns + Order Count line). String/glyph indicator measures cannot drive PBIR mark geometry or data-color rules and render as plain text/labels only (capability boundary)." },
  // ---- HIGH: map tile-grid / real geography ----
  { item: "report.map_multipolygon_trellis", severity: "high", stage: S,
    issue: "Ex.10 Map Trellis renders 49 state Multipolygon mini-maps arranged in an INDEX-derived tile grid (Map Rows/Map Columns). Rebuilt as a scatterChart tile-grid (X=Map Columns, Y=Map Rows inverted, Category=State, size=Sales) preserving the exact 49-cell layout. The per-cell real geography (a small choropleth per state) is dropped; a single azureMap choropleth over State is the alternative but is not a trellis-of-maps (capability boundary)." },
  // ---- MEDIUM ----
  { item: "report.pie_marks.on_time_ship", severity: "medium", stage: S,
    issue: "Ex.4 Varying Shape draws a pie per Region x Category (On Time Ship Pies). pieChart has no Small-Multiples role, so rebuilt as a hundredPercentStackedColumnChart (X=Region, legend=On Time Ship?, Y=Order Count) small-multipled by Category - faithful on-time-vs-late proportion. The pie-mark (angular) geometry is approximated by stacked columns." },
  { item: "report.border_highlight_encoding", severity: "medium", stage: S,
    issue: "Ex.6 Border Highlight uses colored cell BORDERS (a 3rd visual encoding) plus most-profitable ship-mode icons on a Sub-Category x Region highlight table. Rebuilt as a pivotTable with a linearGradient3 background FillRule on Total Profit. Border-as-encoding and the per-cell icon set are not reproduced (Power BI cell formatting has no border-value channel)." },
  { item: "report.mnv_unresolved_fields", severity: "medium", stage: S,
    issue: "Several worksheets carry UNRESOLVED calc references inside their Measure Names/Measure Values pivots that the parser could not resolve to a model field: Highlight Table (a column calc), L-Bar (a column calc), On Time Ship Pies (angle/color calcs), SubCat Bar w/Hash Indicator & Hash Quad (a Quantity/Sales hash calc), Ship Mode Test Sheet (a label calc). Resolved pivot fields were bound directly; unresolved ones were NOT guessed, so those visuals may be missing a secondary field. Surfaced for review rather than fabricated." },
  // ---- LOW ----
  { item: "report.color_size_scatter_gradient", severity: "low", stage: S,
    issue: "Ex.5 Color & Size: Tableau's continuous color-by-Discount + size ramp is rebuilt as a scatter with bubble size = Deep Discount (Highlight) and a linearGradient3 dataPoint FillRule on the same measure (projected so it can drive color). Faithful double-encoding; the exact Tableau color ramp is approximated by a green->amber->orange saturation gradient." },
  { item: "report.disconnected_param_slicers", severity: "low", stage: S,
    issue: "The 4 disconnected parameter tables (Select Highlight Function, Profit Ratio Goal, Select Hashing, Variable Metric; 0 relationships by design) are rendered as single-select dropdown slicers with default selections matching the Tableau current values (Max / 0.12 / hatch glyph / - Order Count). Being disconnected they only feed their SELECTEDVALUE measures and cannot cross-filter the Orders fact - expected for a parameter, noted for traceability." },
];

const key = e => `${e.stage}::${e.item}`;
const have = new Set(spec.limitations_encountered.map(key));
let added = 0;
for (const e of add) if (!have.has(key(e))) { spec.limitations_encountered.push(e); added++; }

writeFileSync(SPEC, JSON.stringify(spec, null, 2) + "\n", "utf8");

const bySev = spec.limitations_encountered.filter(l => l.stage === S)
  .reduce((a, l) => (a[l.severity] = (a[l.severity] || 0) + 1, a), {});
console.log(`Appended ${added} new report_build limitations (skipped ${add.length - added} already present).`);
console.log(`Total limitations now: ${spec.limitations_encountered.length}`);
console.log(`report_build by severity:`, JSON.stringify(bySev));
