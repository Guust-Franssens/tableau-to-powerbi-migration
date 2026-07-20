# QuadrupleAxisCharts — Report Page Plan & Design Brief

Source: `10 Ways to Make Quadruple-Axis Charts` (Tableau showcase). 12 dashboards, 13 worksheets.
Model: `QuadrupleAxisCharts.SemanticModel` (Orders fact + 4 disconnected param tables; 79 measures; 0 relationships).
Canvas: **1600 × 900** per page (Tableau dashboards are 1400×800 / 1.75 ratio; 1600×900=1.778, ~1.6% h-stretch, negligible). Zone coords are Tableau 0–100000 %-space → scaled linearly to px.

## The capability boundary (why this is honest, not a fake stack)
This workbook is a *catalog of fake-extra-axis tricks*: Tableau fakes the 3rd..6th axis by stacking
identical measures on Rows/Columns + constant anchor placeholders (MAX(1.0)/MAX(0.0)/0), then layers
unicode shape/glyph marks driven by table calcs. **Power BI caps at a dual-axis combo chart + small
multiples.** The *data* behind every mark is modeled as real measures (bind those); the literal
N-axis geometry, unicode shape marks, and hatch fills are a hard capability boundary. For each such
trick: build the best native approximation that preserves the data, add an on-page honest note, and
log a `report_build` HIGH limitation. **We do NOT fake N-axis geometry** (validate passes
structurally-valid-but-wrong encodings).

## Param slicers (4 disconnected → single-select dropdowns, superstore 🟢 pattern)
Bound to the param *column*; value read by the `<name> Value` SELECTEDVALUE measure (defaults to the
Tableau current value even with no selection — disconnected, so they can never zero a fact aggregate).
Default selection set explicitly for fidelity (matches Tableau current value).

| Slicer (table[column]) | Value measure | Default | Placed on pages |
|---|---|---|---|
| `Select Highlight Function`[Select Highlight Function] | Select Highlight Function Value | `Max` | 1, 2, 7 |
| `Profit Ratio Goal`[Profit Ratio Goal] | Profit Ratio Goal Value | `0.12` | 7 |
| `Select Hashing`[Select Hashing] | Select Hashing Value | `╱╱╱` | 7 |
| `Variable Metric`[Variable Metric] | Variable Metric Value | `- Order Count` | 9 |

## Page plan (12 pages = 12 dashboards)

| # | Page (id) | Tableau dashboard | Worksheet(s) | Native visual decision |
|---|---|---|---|---|
| 0 | Overview (`overview`) | Quad Charts (nav hub) | — | Title + intro textbox + **pageNavigator** |
| 1 | 1. Line/Area++ (`p01_line_area`) | 1. Line/Area++ | Area+Dot+Line+Circle | **lineChart** Ship Month x [Total Sales] + [Dot Sales Window] + [Circle Sales Window] (markers). Area+4-marks-at-a-point -> HIGH note. + Highlight slicer |
| 2 | 2. Line+++ (`p02_line_plus`) | 2. Line+++ | Circle+Dot+Line+Circle | **lineChart** Ship Month x [Total Sales] + [Dot Sales Window] + [Circle Sales Window] + [Deep Discount Dot]. HIGH note. + Highlight slicer |
| 3 | 3. Triangles x4 (`p03_triangles`) | 3. Triangles x 4 | Scatter: Triangles | **scatterChart** X=[Total Sales] Y=[Total Profit] Category=Sub-Category. Triangle shape marks -> circles: HIGH note |
| 4 | 4. Varying Shape (`p04_pies`) | 4. Varying Shape | On Time Ship Pies | **hundredPercentStackedColumnChart** X=Region, Series=[On Time Ship?], Y=[Order Count], small-multiples Rows=Category. Pie grid -> 100% stacked SM: MEDIUM note |
| 5 | 5. Color & Size (`p05_color_size`) | 5. Color & Size | Scatter: Color Discount | **scatterChart** X=[Total Sales] Y=[Total Profit] Category=Sub-Category, Size=[Deep Discount? (Highlight)], color FillRule on discount. LOW note |
| 6 | 6. Border Highlight (`p06_highlight`) | 6. Border Highlight | Highlight Table | **pivotTable** Rows=Sub-Category, Columns=Region, Values=[Total Profit] + conditional bg gradient. Border->bg + icon marks: MEDIUM note |
| 7 | 7. Bar Hashing (`p07_hashing`) | 7. Bar Hashing | SubCat Bar w/ Hash Indicator + SubCat Bar Hash Quad | 2x **lineClusteredColumnComboChart** Category=Sub-Category, Y=[Profit Ratio], Y2=[Profit Ratio Goal Value] (dynamic goal line). Hash fills + quad-axis: HIGH note. + Highlight + Hashing + Goal slicers |
| 8 | 8. L-Bar/Dot (`p08_lbar`) | 8. L-Bar/Dot | L-Bar | **lineClusteredColumnComboChart** Category=Sub-Category, Y=[Order Count], Y2=[Total Sales]. L-shape geometry + check/dot marks: HIGH note |
| 9 | 9. Bar/Line/Dot (`p09_barline`) | 9. Bar/Line/Dot | Bar-in-Bar Measure vs. Dynamic + Bar-in-Bar Measure Values | Top: **combo** Order Quarter, Y=[Sales-East]+[Sales-West], Y2=[Profit-East]+[Profit-West]. Bottom: **clusteredColumnChart** Order Quarter x [Sales-West]+[Sales-East]. Bar-in-bar geometry + unicode ref-marks: HIGH note. + Variable Metric slicer |
| 10 | 10. Map Trellis (`p10_maptrellis`) | 10. Map Trellis | States | **scatterChart tile-grid** X=[Map Columns], Y=[Map Rows] (inverted), Category=State, Size=[Total Sales]. INDEX tile-grid preserved; Multipolygon mini-maps -> dots: HIGH note (azureMap choropleth noted as alt) |
| 11 | Summary (`p11_summary`) | Summary | Ship Mode Test Sheet | Credits + **Fidelity & Capability Boundary** note + **pivotTable** (Region/Sub-Category/Ship Mode x [Profit], [Profit SUM by...], [Profit MAX...]) |

## Layout contract (per page, px on 1600x900)
- **Header band** y=0-124 (title strip + example subtitle), reserved before visuals.
- **Slicer/legend band** y=124-178 (param slicers top-right where the dashboard shows the control label).
- **Main visual region** y=180-860 (single visual full-width, or 2 side-by-side for pages 7 & 9).
- **Footer note** y=862-892 (honest capability-boundary note where applicable).
- space_audit: single-column full-width main region => zero overlaps by construction; 2-up pages split
  the main region at x=792 (gutter 16).

## Honest-note strategy
Every HIGH-boundary page carries a small footer textbox ("N-axis / shape-mark trick approximated -
Power BI caps at dual-axis; see Summary"). The Summary page enumerates, per trick, what became a
dual-axis combo vs small multiples vs an honest approximation.
