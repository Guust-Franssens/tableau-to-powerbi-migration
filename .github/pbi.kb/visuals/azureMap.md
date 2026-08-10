# Azure Maps visual migration guidance

Azure Maps is the default Power BI target for migrated Tableau map idioms; use reference layers for territory/choropleth fidelity, marker/bubble layers for points, path layers for routes, and heat map layers for density.

## Roles

Installed-product source: `powerbi-report-author catalog describe azureMap` on 2026-07-19. Catalog cross-check: `powerbi-report-author catalog list` shows `azureMap`, `map`, `filledMap`, `shapeMap`, and `heatMap`; `map` and `filledMap` are deprecated with `alternative: azureMap`.

| Role | Power BI field well | Required? | Kind | Max | Migration use |
| --- | --- | --- | --- | --- | --- |
| `Category` | Location | Yes | Grouping | not reported | Geocoded location, data-bound reference-layer key, or place hierarchy. |
| `Y` | Latitude | No | GroupingOrMeasure | 1 | Prefer for point/route/heat maps when coordinates exist; avoids ambiguous geocoding. |
| `X` | Longitude | No | GroupingOrMeasure | 1 | Prefer for point/route/heat maps when coordinates exist; avoids ambiguous geocoding. |
| `Series` | Legend | No | Grouping | 1 | Categorical color for markers/bubbles and paths. |
| `Size` | Size | No | Measure | 1 | Bubble/marker sizing or heat-map weighting. |
| `Tooltips` | Tooltips | No | Measure | not reported | Hover measures. |
| `PathID` | Path ID | No | Grouping | 1 | Groups points into a route/path. |
| `PointOrder` | Point Order | No | Grouping | 1 | Sorts points within each path. |

Installed formatting objects include `bubbleLayer`, `filledMap`, `heatMapLayer`, `pathLayer`, `referenceLayer`, `tileLayer`, `traffic`, `mapControls`, `legend`, `dataPoint`, `categoryLabels`, and labels/general objects. Layer object highlights from `powerbi-report-author formatting describe-object azureMap <object>`:

| Object | Key installed properties |
| --- | --- |
| `referenceLayer` | `datasourceType` = `url`/`file_upload`, `referenceLayerUrl`, `polygonFillColor`, `polygonStrokeColor`, `polygonStrokeWidth`, `polygonStrokeTransparency`, point/line style properties, unmapped-object visibility/colors. |
| `bubbleLayer` | `show`, radius/min/max sizing, fill/stroke, `clusteringEnabled`, cluster color/size/text properties, marker image/icon settings, min/max zoom, `layerPosition`. |
| `heatMapLayer` | `show`, `heatMapRadius`, `heatMapRadiusUnit`, transparency, intensity, low/center/high gradient colors, `heatMapUseSize`, min/max zoom, `layerPosition`. |
| `pathLayer` | `show`, path color, width, transparency, min/max zoom. |
| `filledMap` | `show`, fill color/transparency, outline color/width/transparency, min/max zoom, `layerPosition`. |

## Tableau map idiom -> Power BI

| Tableau idiom | Recommended approach | Tier | Note/citation |
| --- | --- | --- | --- |
| Filled/region map shaded by a measure (choropleth) | Prefer `azureMap` with a data-bound `referenceLayer` (GeoJSON/KML/WKT/SHP/CSV) and conditional `polygonFillColor`; use the built-in `filledMap` layer only for standard geographies where approximate Microsoft boundaries are acceptable. | ✅ green render-verified | Proven in Superstore at `examples\superstore-sales-performance\fabric\SuperstoreSalesPerformance.Report\definition\pages\prescriptive\visuals\9d3297e633e4cdaa9e20\visual.json`; Learn says data-bound reference layers match shape properties to the Location field and support conditional formatting. Source: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-reference-layer, ms.date 2025-01-17. |
| Symbol/point map (bubbles sized/colored by measure) | Use `azureMap` marker/bubble layer with Location or Latitude/Longitude, Legend for color, Size for measure scaling, and cluster bubbles when dense. | 🟨 yellow structural | Installed object is `bubbleLayer` with size, fill, stroke, and `clusteringEnabled`; Learn marker guidance says add Location or Latitude/Longitude, optionally Legend and Size. Source: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-marker-layer, ms.date 2026-01-07. Needs a render-verified PBIR exemplar. |
| Path/route map (origin-destination lines) | Use `azureMap` path layer: create one row per route point, set `PathID`, numeric/timestamp `PointOrder`, plus Location or Latitude/Longitude. Transform OD rows into origin/destination point rows before visual binding. | 🟨 yellow structural | Learn explicitly states path layer visualizes connections and requires Path ID plus Point Order; OD data must be transformed because Azure Maps doesn't directly support origin-destination rows. Source: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-path-layer, ms.date 2024-11-27. Needs a render-verified PBIR exemplar. |
| Density/heatmap | Use `azureMap` heat map layer with Latitude/Longitude or valid locations; tune radius, units, transparency, intensity, gradient, min/max zoom, and optionally `Size` as weight. | 🟨 yellow structural | Learn says heat maps are for density/hot spots and perform better than many overlapping symbols for large point datasets. Source: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-heat-map-layer, ms.date 2025-01-17. Needs a render-verified PBIR exemplar. |
| Custom-territory map (non-standard regions) | Use `azureMap` data-bound `referenceLayer` with simplified GeoJSON/KML/WKT/SHP/CSV boundaries and a stable territory key in Location; style polygons with conditional formatting. Avoid legacy `shapeMap` unless a human has captured an exact unsupported requirement. | ✅ green for reference-layer choropleth; 🟥 red for Shape Map parity | Learn supports data-bound reference layers and custom styling; installed catalog shows `shapeMap` exists but `map`/`filledMap` are deprecated to `azureMap`. Source: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-reference-layer, ms.date 2025-01-17; installed catalog 2026-07-19. |

## Basemap: read the SOURCE style from the `.twb`; the target mapping is a separate judgement

Azure Maps `mapControls.defaultStyle` is the basemap. Getting it from a 192 px reference thumbnail
is guesswork (and impossible when no thumbnail exists), but the source workbook states its own style
outright — so **extracting the Tableau side is exact and mechanical.** Choosing the Azure Maps
equivalent is a separate step, and is *not* verified by the extraction.

Per worksheet, the `.twb` carries a `<style-rule element='map'>` and a `<mapsource>`:

```python
for ws in ET.parse(twb).getroot().iter("worksheet"):
    styles = [(f.get("attr"), f.get("value"))
              for sr in ws.iter("style-rule") if sr.get("element") == "map"
              for f in sr.iter("format")]
    sources = [e.get("name") for e in ws.iter("mapsource")]
```

**Extracting the Tableau side is exact and mechanical. Choosing the Azure Maps equivalent is a
separate judgement, and none of the three target mappings has been render-compared yet:**

| Tableau source style | Azure Maps `defaultStyle` | confidence | evidence |
| --- | --- | --- | --- |
| `mapsource='Tableau'`, no `map-style` override | `grayscale_light` | ⚠️ inferred | **Source side ✅:** `book_6-1-Maps`, 6 worksheets share this config, 3 have reference thumbnails, all light grey. **Target side ⚠️:** a Power BI render at `grayscale_light` exists (the 🟢 POSITIVE entry below, 2026-08-09, Desktop MSIX 2.157.627.0) but was never placed beside a Tableau thumbnail and judged a match |
| `map-style='tableau-z-black'` | `night` | ⚠️ inferred | source side read from the `.twb` (Dark Map); the Azure target is a name match, **no side-by-side render captured** |
| `mapsource='Satellite'` | `satellite` | ⚠️ inferred | source side read from the `.twb` (Mapbox); Azure target not render-compared. The `powerbi-report-gotchas` skill (§5, Maps) likewise records `satellite`/`night` behaviour as structural only |

To promote a row to ✅, capture the Tableau reference render and the Power BI render of the **same
worksheet**, compare them, and record the worksheet, date and Desktop/CLI version here. Note what
that bar excludes, because it is the trap this table already fell into once: confirming the *source*
is light grey says nothing about whether Azure Maps' `grayscale_light` resembles it.

**The method matters as much as the table** — it is how you convert an ⚠️ into a ✅ *on the source
side*. Measured on `book_6-1-Maps`: **six** worksheets shared one identical config
(`mapsource='Tableau'`, no override), and **three** of those six had reference thumbnails, all light
grey. Same configuration + a rendered exemplar of that configuration = evidence for the other three,
including two that had no thumbnail at all and had been flagged "confirm before changing".

Generalise it: when a reference render is missing, look for a **sibling that shares the identical
source configuration and does have one**, rather than guessing or leaving the item unresolved.

## MS Learn best practice (as of 2026-07-19)

Web access was available through `web_fetch`; several old Power BI `/power-bi/visuals/...azure-maps...` URLs now 404, while the current Microsoft Learn Azure Maps URLs below resolved.

- Azure Maps overview / when to use: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-get-started, ms.date 2025-02-25. Quote: "The Azure Maps Power BI visual provides a rich set of data visualizations for spatial data on top of a map." It also says the visual supports up to 30,000 data points and can use Location or Latitude/Longitude.
- Layers available: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-understanding-layers, ms.date 2023-07-19. Quote: "There are two types of layers available in an Azure Maps Power BI visual": data rendering layers (Marker, 3D column, Filled map, Heat map) and external/context layers (Reference, Tile, Traffic). It lists `Path Layer` in the layer order table.
- Reference layer: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-reference-layer, ms.date 2025-01-17. Quote: "Reference layers enable the enhancement of spatial visualizations by overlaying a secondary spatial dataset on the map to provide more context." Supported files include GeoJSON, WKT, KML, SHP, and CSV with WKT; hosted URLs and file uploads are supported.
- Data-bound reference layer: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-reference-layer, ms.date 2025-01-17. Quote: "The data-bound reference layer enables the association of data with specific shapes in the reference layer based on common attributes." It matches the Location field to properties in the spatial file, choosing the property with the highest number of matches when multiple properties exist.
- Reference-layer feature limit: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-reference-layer, ms.date 2025-01-17. Quote: "The Azure Maps Power BI visual renders only the first 30,000 features from a reference layer." Simplify national ZIP/postal or parcel files before using them.
- Marker/bubble layer: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-marker-layer, ms.date 2026-01-07. Quote: "The Marker layer in the Azure Maps visual allows you to plot individual locations as points on the map, using either simple circle markers or custom icon imagery." It supports Legend categorization and Size scaling.
- Heat map layer: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-heat-map-layer, ms.date 2025-01-17. Quote: "Heat maps, also known as density maps, are a type of overlay on a map used to represent the density of data using different colors." Learn recommends heat maps for large numbers of points because overlapping symbols degrade performance and usability.
- Path layer: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-path-layer, ms.date 2024-11-27. Quote: "The path layer feature in the Azure Maps Power BI Visual enables the visualization of connections between multiple geographic points." It requires Path ID and Point Order, and says origin-destination rows must be unpivoted/transformed.
- Filled-map/Bing Maps deprecation: https://learn.microsoft.com/en-us/power-bi/visuals/power-bi-visualization-filled-maps-choropleths, ms.date 2025-10-01. Quote: "The Bing Maps visual is scheduled for deprecation" and "upgrade to Azure Maps" unless users are in China, Korea, or government clouds. The map tips page also says Power BI plans to deprecate older map visuals and migrate existing reports to Azure Maps: https://learn.microsoft.com/en-us/power-bi/visuals/power-bi-map-tips-and-tricks, ms.date 2025-09-17.

## Known-good encoding

Green/render-verified source: `examples\superstore-sales-performance\fabric\SuperstoreSalesPerformance.Report\definition\pages\prescriptive\visuals\9d3297e633e4cdaa9e20\visual.json`.

That visual is an `azureMap` choropleth using a two-entry `objects.referenceLayer` array:

1. Datasource entry: `datasourceType` is the literal `'url'`, and `referenceLayerUrl` points to `https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json`.
2. Conditional polygon style entry: has a wildcard selector (`dataViewWildcard.matchingOption = 1`) and sets `polygonFillColor.solid.color.expr.FillRule` where the input is measure `'Sample Superstore'[Map KPI Difference]`; the `linearGradient3` fill rule uses red min `#FC4237`, gray midpoint `#E6E6E6` at `0D`, blue max `#34657F`, and `nullColoringStrategy = 'asZero'`.

The same visual keeps the Power BI data binding minimal: `query.queryState.Category` contains only `'Sample Superstore'[State]`, allowing Azure Maps to data-bind the state names to the GeoJSON properties. `objects.mapControls` fixes the default style to grayscale light, hides style/navigation/selection controls, and pins the continental-US viewport; `objects.bubbleLayer` is still present with `show = true`, but the choropleth effect comes from `referenceLayer.polygonFillColor`.

## 🟢 Render-verified POSITIVE: the measure-driven choropleth works, exactly as encoded

**Verified 2026-08-09, Desktop MSIX 2.157.627.0, `book_6-1-Maps`, signed-in session on an entitled
tenant.** Supersedes the "structurally verified only" caveat this file previously carried.

The `referenceLayer` recipe above renders a genuine choropleth: US state polygons filled from the
bound measure, California and New York darkest, Texas mid-tone, the remaining states graded, and
unmapped geography left grey. `mapControls.defaultStyle` is honoured literally — `'night'` produced a
a true dark basemap, `'road'` the standard one. So the encoding in "Known-good encoding" above is now
**render-verified, not inferred**.

**The entitlement caveat below is still true** — it is about an *unauthenticated* session, not about
the encoding. Both states look identical to `validate`.

## 🔴 Render-verified DEFECT: a `Series`/Legend well VETOES a data-bound `referenceLayer.polygonFillColor`

**Verified 2026-08-09, Desktop MSIX 2.157.627.0, `book_6-1-Maps` (`Combined Map`), entitled
signed-in session.** This is the caveat to the 🟢 POSITIVE entry directly above: the choropleth
recipe works **only while the visual has no legend**. Add a `Series` projection and the ramp dies
silently — `validate` reports 0 errors, the `linearGradient3` stays in `visual.json`, and the map
still draws.

**Symptom.** `referenceLayer[1].polygonFillColor` declares a `linearGradient3` over
`Sum(Orders.Profit)`, yet **the measure ramp is not applied** — polygons take categorical `Series`
colours instead, so large same-category areas read as a flat wash. Texas (`SUM(Profit)` =
**−25,729**, the most negative value in the model) sampled **byte-identical RGB (156,177,200)** to
mildly-positive states, and a single colour covered 33% of the landmass. The map is not
single-coloured overall — the point is that colour no longer encodes the measure.

**Why it is easy to misdiagnose.** The visual also had a multi-field Location well (the defect
below), so "the choropleth is broken" looks like it should resolve once the Location is fixed. It
does not — the two defects are independent.

**The experiment that isolates the mechanism** (re-runnable; this is the part worth copying):

1. Find a category that is *unique to one region*. In Superstore, **North Dakota sells ONLY Office
   Supplies** (`SUM(Profit)` = +230.15) and **Wyoming ONLY Furniture** — verify with
   `EVALUATE SUMMARIZECOLUMNS('Orders'[State], 'Orders'[Category], "p", SUM('Orders'[Profit]))`.
2. Note the polygon colour of that region. Baseline: North Dakota rendered **reddish (226,100,103)**,
   matching *neither* gradient stop — the anomaly that looks inexplicable until you know the cause.
3. Change **only the category palette** (here: the `dataPoint` fix swapping Office Supplies from red
   to orange). Change nothing about `referenceLayer`.
4. North Dakota's polygon flips **red → orange**, and Montana follows its own dominant category.

The polygons are being painted by the **Legend**, not by the FillRule. A region whose rows are
100% one category takes that category's colour outright, which is exactly why single-category states
read as "wrong colour" rather than "wrong shade".

**Control.** `Filled Map`, in the same report, same `referenceLayer` encoding shape, **no `Series`
well** → renders a true ramp. So the encoding is correct and the Legend is the differentiator.

**Consequence for migrations.** Tableau's dual-axis map (`rows = Latitude + Latitude`) is genuinely
two-or-more mark layers — `Combined Map`'s `.twb` has `pane[0] mark=Pie`, `pane[1] mark=Pie`,
`pane[2] mark=Multipolygon`. An azureMap has **one Location well and one Legend well**, so it cannot
carry two LODs with two colour encodings. Do not promise both. Choose:

| keep | how | give up |
|---|---|---|
| the category pies (usually the point of the worksheet) | `Series` + city-grain Location; make `referenceLayer` a **static** boundary overlay (`unmappedObjectVisibility: true`, neutral fill, drop the inert FillRule) | the measure choropleth |
| the measure choropleth | drop `Series`, Location = the polygon key | per-category marks |
| both | two stacked visuals — **unproven**, azureMap has no transparent canvas, and it breaks the no-overlap space audit | — |

A model-side lat/long column does **not** rescue this: the Legend veto applies regardless of how the
Location is expressed.

## 🔴 Render-verified DEFECT: a multi-field Location well collapses every mark to ONE

**Verified 2026-08-09, same session. This is the highest-value entry in this file** — it is
validation-invisible, renders "successfully", and silently destroys the entire point of the map.

**Symptom.** The map draws, the basemap is correct, the legend is correct, and there is **exactly one
mark in the middle of the country** instead of one per state/city. No error, no warning,
`validate` clean.

**Measured, same report, same model — the discriminator is the number of fields in `Category`:**

| page | `Category` projections | rendered | Tableau |
|---|---|---|---|
| Filled Map | `[State]` | ✅ one polygon per state | 49 |
| Combined Map | `[State, City]` | ❌ renders at **State** | pane-1 LOD is **City** |
| Symbol Map | `[Country, State, City]` | ❌ **one bubble** at the US centroid | 604 |
| Pie Chart Map | `[Country, State, City]` | ❌ **one pie** at the US centroid | 10 |
| Dark Map | `[Country, State, City]` | ❌ **one bubble** at the US centroid | 604 |
| Density / Mapbox / Viz-in-Tooltip | `[Country, State, City]` | ❌ same | 604 |

**`Combined Map` is the entry that generalises the rule.** It has no `Country`, yet still fails —
rendering at `State` where Tableau plots at `City`. So the rule is not "`Country` poisons the well";
it is that **any** multi-field Location well renders at its **top** level.

**Objective confirmation** (better than eyeballing): isolating the bubble gradient colour and running
connected-components over the map canvas gives **exactly one** blob per broken page, at canvas px
(1139, 744) — which is `mapControls.centerLatitude 39.2795` / `centerLongitude −97.4361`, the centroid
of the contiguous US. The mark *is* the "United States" geocode.

**Root cause.** Stacking several geographic columns into `Category` builds a **drill hierarchy**, and
azureMap renders at the **top** level until the user drills down. `Country` has one member
("United States"), so every row aggregates into a single mark at that country's centroid. Tableau
does the opposite: geographic fields on **Detail** plot at the **finest** level present, so the same
shelf configuration means "one mark per city" there and "one mark per country" here.

**This is a translation trap, not a Power BI bug.** A Tableau map worksheet with `Country`, `State`
and `City` on Detail must not be transliterated field-for-field.

**Fix — and the obvious repair is ALSO wrong.** Put **only the leaf geography** in `Category`. But
"the leaf" is not `City`: measured on Superstore (9,994 rows), there are **604 distinct
`(City, State)` pairs** versus only **531 distinct `City` names**, because **57 city names recur
across states — 130 of the 604 pairs, 21.5%** (`Springfield` in 4 states, `Columbia` in 4,
`Columbus`/`Roseville`/`Burlington`/`Florence`/`Concord`/`Lancaster` in 3 each). Binding `City` alone
merges or mis-geocodes a fifth of the marks and looks plausible while doing it.

So the Location well needs a **composite `City, State` key** (or real Lat/Long at Average
aggregation). That column usually does not exist in a migrated model, which makes the blocking half of
this defect a **semantic-model** change, not a report edit — coordinate before "fixing" it in PBIR.

**Verify by COUNTING MARKS against the source grain, never by looking for an error.** The target here
is 604, and both 1 and 531 render without complaint.

**Detection rule worth automating:** any `azureMap` whose `Category` well holds **more than one
`Column` projection** is suspect. It is legal, it validates, and it is almost never what the Tableau
source meant.

## 🔴 Render-verified DEFECT: a visual-level MEASURE filter silently drops azureMap marks

**Verified 2026-08-09, Desktop MSIX 2.157.627.0, `book_6-1-Maps` (`Pie Chart Map`), entitled
signed-in session** — same session and build as the two defects above.

**Symptom.** An azureMap whose `filterConfig` carries an `Advanced` comparison against a **measure**
plots far fewer marks than the identical DAX predicate returns. The filter
`[Sales (w/o Category)] >= 24711.0D` should keep **10** cities; the map drew **5-6**. The missing
ones were not the marginal ones — **New York City, the single largest value in the workbook
(256,368)**, plus San Francisco, Philadelphia, San Diego and Jacksonville, were absent.

**It is validation-invisible and render-plausible.** `validate` returns 0 errors, `preview-filters`
shows one clean Visual-scoped filter, and the map draws a perfectly convincing set of pies. Nothing
announces that half the marks are gone. **Only counting marks against a DAX ground truth finds it.**

**Isolating experiment (re-runnable).** Four hypotheses eliminated in order, each with its own control:

| hypothesis | test | result |
|---|---|---|
| render race / partial draw | recapture after a 25 s dwell | frame **byte-identical** → not timing |
| geocoding fails for those cities | `Combined Map` — same `Orders[City, State]` Location, **no filter** | 110 blobs, **10 east of x=72%**, 2nd-largest at x=78.8% = NYC → geocoding is fine |
| the measure is wrong at visual grain | DAX at the visual's true `(City,State) x Category` grain | returns the **city total on every category row** (Akron = 2729.986 x3) → correct |
| the filter payload is malformed | read `filterConfig` | single clean `ComparisonKind: 2` (`>=`) vs `24711.0D` → well-formed |
| **the filter itself** | **delete `filterConfig`, reload, recount** | **5 blobs -> 82 blobs.** Confirmed. |

That last row is the whole experiment: one field removed, one reload, a 16x change in mark count.
**Read it precisely, though** — on its own it proves the filter is what reduces the marks, not that
it reduces them *wrongly*. The wrongness comes from the third row: DAX at the visual's own grain
says the predicate keeps **10** cities, and the map drew **5-6**. Filter-is-the-cause (row 5) plus
correct-count-is-10 (row 3) is what makes this a defect rather than a filter doing its job.

**Mechanism (inferred).** Same family as the gotcha "*a measure used as a visual-level filter at a
finer grain than it evaluates silently zeroes the visual*" — but the **partial** form, which is far
more dangerous than the total one. A visual that renders *empty* gets investigated; a visual that
renders *most* of its marks gets signed off.

**Remedies, in order of preference.**

1. **Filter on a COLUMN, not a measure** — ask the semantic-model owner for a boolean/flag calculated
   column evaluated at the filter's own grain (e.g. `Orders[City Is Top Sales]`). A column filter is
   evaluated in the query's group-by rather than re-evaluated per mark, so it should not exhibit
   this — ⚠️ **inferred, not tested here:** no A/B control was run against a column filter on this
   visual. Run that control before relying on it. **This is a model change — route it, don't make it
   from the report layer.**
2. **Top-N filter** (`filterConfig` `type: "TopN"`) when the source intent really was "top N by
   measure" rather than a threshold.
3. **Leave the measure filter and document it** — only acceptable if you have counted the marks and
   the count is right.

**Standing rule this produces:** whenever an azureMap (or any high-cardinality visual) carries a
**measure** filter, the mark count is not optional — get the DAX cardinality and count the rendered
marks. `validate` cannot see this class of bug and neither can a glance at the render.
## 🟢 Render-verified NEGATIVE: azureMap draws NOTHING without the tenant entitlement

**Verified 2026-07-19, Desktop MSIX 2.157.627.0, `book_6-1-Maps` maps migration.** Cost most of a
render-verification pass. **Check this BEFORE debugging any azureMap encoding.**

**Symptom.** Every `azureMap` on every page renders as a completely **empty container** — the visual
title paints, and nothing else. **No basemap, no marks, no error glyph, no banner, and
`powerbi-report-author validate` reports 0 errors / 0 warnings.** It looks exactly like a broken
field binding, which is what makes it expensive: you will "fix" correct JSON for hours.

**Root cause.** The Azure Maps visual is a *cloud service* client. It requires the tenant setting
**"Users can use the Azure Maps visual"** (Admin portal → Tenant settings → Visual options) **plus an
authenticated Power BI session**. Without the entitlement the visual degrades **silently to blank**
rather than reporting an error. Learn:
<https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-manage-access> (accessed
2026-07-19); tenant-setting changes rolled out June 2025 and can take 24–48 h to propagate.

**The 60-second bisect that settles it — do this first.** Temporarily replace ONE azureMap's
`visual.json` with the *minimal* case: `visualType: "azureMap"` plus a single `Category` Column
projection and **no `objects` key at all**.

- Minimal case renders a basemap → the environment is fine, the bug is in **your encoding**; add
  `mapControls` → `referenceLayer` → `bubbleLayer` back one at a time.
- Minimal case is **also blank** → **environmental**. Stop editing PBIR; no report-layer change can
  fix it. Escalate for sign-in / tenant enablement.

Confirm Desktop actually re-read your file: the visual **title auto-changes to the bound column name**
(e.g. `State`) once the title override is gone. If the title does not change, you are debugging a
stale render, not your JSON.

**Controls worth running to avoid a misdiagnosis** (all four pointed to "environmental" here):

| Control | Result that exonerates your encoding |
|---|---|
| A non-map visual on the same report | `columnChart` rendered fully with correct data → model, refresh and bindings are healthy |
| Network reachability | `atlas.microsoft.com` responds; boundary-GeoJSON host returns 200 → not a firewall/offline issue |
| Dwell then re-screenshot | Byte-**identical** PNG after 35 s → not async tile loading |
| MSIX `LocalCache` user profile | No `Microsoft\Power BI Desktop` settings folder → client was never signed in |

**Consequences for sign-off.** A report whose maps are all `azureMap` **cannot be render-verified** in
an unentitled/offline environment. That is a legitimate blocker to report, **not** a reason to fall
back to Bing `map`/`filledMap` — those are deprecated and reverting is a fidelity + standards
regression. Ship the azureMap encoding, mark the map visuals **structurally verified only**, and say
so explicitly.

**Recurrence 2026-08-08, `book_8-1-Dashboards` (dashboards/layout migration).** Identical symptom,
same machine, same Desktop MSIX 2.157.627.0 — an `azureMap` bubble map (Location `Orders[City]`,
Size `Sum(Sales)`, `bubbleLayer.fillColor` diverging FillRule) rendered as an empty container while
`validate` reported 0 errors. **The entitlement gap is therefore persistent on this machine, not a
one-off.** Two additions to the guidance above:

- **Exonerating control that is nearly free:** if any *non-map* visual on the **same page** renders
  with correct data, model/refresh/binding are healthy and you have already excluded the expensive
  hypothesis without touching the map's JSON. Do this before the minimal-case bisect.
- **The "never fall back to Bing" rule has one documented exception.** That rule optimises for
  fidelity + standards. When the deliverable being graded is **whole-page layout/gestalt**, a blank
  visual is not a neutral "unverified" state — it silently deletes its share of the canvas (here
  **28.86%**), which corrupts the very property under test. In that case the legacy `map` is the
  better temporary choice; it is 🟢 **render-verified 2026-08-08** to draw sized bubbles on a live
  basemap. Costs, both must be logged: `PBIR_VISUAL_TYPE_DEPRECATED`, and an **in-visual "This visual
  type is being retired soon" banner** with an *Upgrade map* button that eats ~12% of the visual's
  height (this is distinct from, and additional to, the once-per-session Bing nag modal). Keep the
  azureMap encoding reachable behind a flag and state the deviation in the handover so the
  orchestrator can overrule it cheaply.
- **Bing `map` geocoding caveat found the same day:** `Location` = a bare city column mis-geocodes
  ambiguous US city names onto other continents. Adding Country/State to the *same* well does **not**
  add geocoding context — the Bing visual turns a multi-field Location into a **drill hierarchy** and
  renders the **top** level, so a single-valued Country column collapses every bubble into one. The
  real fix is a model-layer concatenated `"City, State"` column with `dataCategory` `Place`.

## Open questions / needs-human-capture

- 🟥 Need one render-verified PBIR exemplar each for `pathLayer`, `heatMapLayer`, clustered `bubbleLayer`, and built-in `filledMap` layer. The installed catalog exposes the objects and Learn describes the UX, but structural validity is not enough for cookbook-grade PBIR generation.
- 🟥 Exact PBIR encoding for marker layer image/icon conditional formatting should be human-captured from Desktop if a Tableau workbook uses custom mark shapes/icons.
- 🟥 `shapeMap` exists in the installed catalog, but current guidance and installed deprecation metadata point map migrations to `azureMap`; use Shape Map only after a human confirms Azure Maps cannot meet the requirement.
- 🟨 Path-layer limitation to remember: Learn says data-bound reference layer is unavailable when path layer is enabled, so route maps that also need custom territories may require separate visuals or a tile/reference workaround.
- 🟨 Reference-layer data prep is critical: simplify and filter high-detail files, because only the first 30,000 reference-layer features render.
