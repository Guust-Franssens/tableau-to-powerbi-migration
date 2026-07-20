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
   surface) and `validate`d. Structurally correct, but the *render* is not guaranteed. Treat a 🟡
   entry as **cached CLI output**: no more authoritative than calling the CLI live, and it can go
   stale — on any conflict, the **live CLI wins**.
2. **Render-verified ground truth** (🟢) — proven by an actual rendered visual in one of our
   migrations, OR hand-built by a human in Power BI Desktop and captured here. This is the **one thing
   more trustworthy than the CLI for *composition*** (the CLI describes properties in isolation and
   `validate` green-lights structurally-valid-but-wrong JSON; a 🟢 entry actually rendered). Use it for
   anything where structure alone is insufficient (dynamic field parameters + slicer defaults, azureMap
   reference layers, custom polygon/geometry marks, dual-axis secondary binding, analytics-pane lines).

## Precedence — CLI for current truth, cookbook for proven shapes, MS Learn for the mapping

The CLI and the cookbook answer *different* questions. The CLI is the **live vocabulary** (roles,
properties, enums — always reflects the installed version). The cookbook is a **cache of worked
compositions**. Use them in this order:

1. **Which visual to use** → research **Microsoft Learn** for current best practice (esp. maps),
   cross-checked against `catalog list`/`catalog describe`. Product capabilities move; don't assume.
2. **Encoding vocabulary** (roles/props/enums) → the **CLI, first and always** — it catches cookbook
   staleness.
3. **Encoding composition** → **🟢 render-verified cookbook entry** if one exists (reconcile its
   property names against the CLI) **> compose from the CLI** (🟡 templates are stale cache, defer to
   live CLI) **> research + human capture** (which then becomes a new 🟢 entry).

Each `visuals/<type>.md` carries a `## MS Learn best practice (as of <date>)` section with a dated
citation, refreshed by the report-builder's per-idiom research subtasks — so the mapping guidance stays
current instead of freezing. See `.github/agents/pbi-report-builder.agent.md` ("Research subtasks").

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
| `pageNavigator`, `textbox` | all migrations |
| `shape` (fill + tileShape + **`visualLink` WebUrl**) | interactive-resume `bg-dtc` (🟢 render-verified: hexagon/oval badges + clickable Web-URL buttons; **embedded `text` object does NOT render → use textbox overlay**; see `visuals/shape.md`) |
| `actionButton` (static Web URL / link button) | interactive-resume — 🔴 **render-broken**: Desktop ignores `visual.objects`, draws a blank rectangle (validate still 0-errors). **Use `shape` instead** (same `visualLink`); see `visuals/actionButton.md` |

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
