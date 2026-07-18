# PBIR Visual Cookbook

Ground-truth PBIR (`visual.json`) encodings for every Power BI visual type + formatting idiom we
migrate Tableau vizzes into. The goal: the `pbi-report-builder` agent **copies a proven encoding**
instead of guessing, and we only put a human in the loop for the genuinely render-uncertain cases.

## Why this exists

The azureMap-choropleth episode showed the failure mode: `powerbi-report-author validate` passes
0-errors while the rendered visual is broken, and there were **zero public PBIR examples** to copy.
The fix was to have a human build it once in Desktop and capture the resulting `visual.json` as
ground truth. This cookbook generalizes that: capture/verify each encoding **once**, reuse forever.

## Two tiers of confidence

1. **Structural template** (🟡) — built deterministically from the `powerbi-report-author` CLI
   (`catalog describe <type>` for roles, `formatting effective-properties <type>` for the formatting
   surface) and `validate`d. Structurally correct; field-well roles + projection shape are right.
   Good enough for the report-builder to adapt, but the *render* is not guaranteed.
2. **Render-verified ground truth** (🟢) — either proven by an actual rendered visual in one of our
   migrations, OR hand-built by a human in Power BI Desktop and saved, then captured here. Use this
   tier for anything where structure alone is insufficient (dynamic field parameters + slicer
   defaults, azureMap reference layers, custom polygon/geometry marks, dual-axis secondary binding,
   analytics-pane lines).

## The CLI is the research tool (deterministic, no guessing)

```
powerbi-report-author catalog list                      # all 58 built-in types + deprecations
powerbi-report-author catalog describe <type>           # field-well roles, required/optional, formatting objects
powerbi-report-author formatting effective-properties <type>   # every formatting surface for the type
powerbi-report-author formatting describe-object <type> <object>
powerbi-report-author validate <path-to-.Report-dir>    # structural validation after every edit
```

Deprecated (do not emit): `filledMap` -> `azureMap`, `map` -> `azureMap`, `qnaVisual` (unsupported in PBIR).

## Confidence map (Tableau-relevant types + idioms)

Legend: 🟢 render-proven · 🟡 structural template (CLI) · 🔴 needs human Desktop capture · ⛔ no native visual (marketplace `.pbiviz` / capability gap)

### 🟢 Proven in our migrations (copy from the cited migration)
| Type | Example location |
|---|---|
| `columnChart` / `clusteredColumnChart` | airline `9f2607ea` pages |
| `barChart` / `clusteredBarChart` | airline / superstore |
| `hundredPercentStackedBarChart` | (used) |
| `lineChart` | superstore descriptive trends |
| `scatterChart` | superstore prescriptive `377a8368` |
| `cardVisual` | superstore descriptive cards |
| `gauge` | superstore descriptive gauges |
| `tableEx` / `pivotTable` (matrix) | airline `ba1e195d` / tale-of-100 |
| `azureMap` (choropleth reference-layer) | superstore prescriptive `9d3297e6` (render-verified, Desktop-built) |
| `slicer` (list/dropdown, single/multi) | all migrations |
| `pageNavigator`, `textbox`, `shape` | all migrations |

### 🎯 Research targets (Tableau-common, not yet proven) — being templated by cluster agents
- **Cartesian family**: `areaChart`, `stackedAreaChart`, `hundredPercentStackedAreaChart`,
  `hundredPercentStackedColumnChart`, `lineClusteredColumnComboChart` + `lineStackedColumnComboChart`
  (dual-axis: `Y`=columns, `Y2`=line) — Tableau dual-axis / combined-axis / area marks.
- **Cartesian idioms**: `smallMultiplesLayout` (Tableau trellis/small multiples), analytics/reference
  lines (`referenceLine`, `trend`, `y1AxisReferenceLine`, constant/min/max/average/percentile),
  `error` bars, `zoom` slider.
- **Part-to-whole / flow**: `pieChart`, `donutChart`, `treemap`, `funnel`, `ribbonChart`,
  `waterfallChart` (Category/Breakdown/Y + `sentimentColors`), `heatMap`.
- **KPI / card family**: `kpi` (classic goal/trend), `multiRowCard`, `card` (legacy single).
- **Maps (non-azure)**: `shapeMap` (custom TopoJSON), `map` bubble (deprecated -> prefer azureMap bubble layer).
- **AI visuals**: `decompositionTreeVisual`, `keyDriversVisual` (key influencers).
- **Table idioms**: data-bar / icon / background conditional formatting (`conditional-formatting.md`
  Type 4 data bars), report-page tooltips, drill-through.

### ⛔ No native visual — Tableau idioms that need a marketplace `.pbiviz` or are a capability gap
Sankey (Tableau flow), radar/spider, bullet graph, box-and-whisker, chord, network/node-link,
custom polygon marks (IronViz triangle/hex geometry via `INDEX()`). Log these as
`limitations_encountered` HIGH-severity gaps; note the closest native fallback + the marketplace
visual name if one exists.

### ➖ Out of scope (not Tableau-migration relevant)
`rdlVisual`, `dataQueryVisual`, `realTimeLineChart`, `scriptVisual` / `pythonVisual`,
`accessibleTable`, `animatedNumber`, `scorecard`, `bookmarkNavigator`, `aiNarratives`,
niche slicers (`listSlicer`/`textSlicer`/`advancedSlicerVisual`/`filterSlicer` beyond core `slicer`).

## Layout

- `visual-cookbook.md` — this index.
- `visuals/<type>.visual.json` — one minimal worked example per type (tier noted in its sibling `.md`).
- `visuals/<type>.md` — roles, the idiom notes, tier (🟡/🟢), and any human-capture instructions.

## Human capture workflow (for 🔴 render-uncertain items)

1. Open the cookbook capture report (a PBIP bound to a simple generic model) in Power BI Desktop.
2. On the page for the flagged visual, follow the textbox click instructions to build it.
3. Save. The saved `visual.json` under that page's `visuals/` folder is the ground truth — copy it to
   `visuals/<type>.visual.json` and mark the entry 🟢.
