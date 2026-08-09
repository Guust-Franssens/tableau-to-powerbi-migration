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

## 🔴 Render-verified DEFECT: a multi-field Location well collapses every mark to ONE

**Verified 2026-08-09, same session. This is the highest-value entry in this file** — it is
validation-invisible, renders "successfully", and silently destroys the entire point of the map.

**Symptom.** The map draws, the basemap is correct, the legend is correct, and there is **exactly one
mark in the middle of the country** instead of one per state/city. No error, no warning,
`validate` clean.

**Measured, same report, same model — the discriminator is the number of fields in `Category`:**

| page | `Category` projections | rendered |
|---|---|---|
| Filled Map | `[State]` | ✅ one polygon per state, correctly graded |
| Symbol Map | `[Country, State, City]` | ❌ **one bubble** at the US centroid |
| Pie Chart Map | `[Country, State, City]` | ❌ **one pie** at the US centroid |
| Dark Map | `[Country, State, City]` | ❌ **one bubble** at the US centroid |

**Root cause.** Stacking several geographic columns into `Category` builds a **drill hierarchy**, and
azureMap renders at the **top** level until the user drills down. `Country` has one member
("United States"), so every row aggregates into a single mark at that country's centroid. Tableau
does the opposite: geographic fields on **Detail** plot at the **finest** level present, so the same
shelf configuration means "one mark per city" there and "one mark per country" here.

**This is a translation trap, not a Power BI bug.** A Tableau map worksheet with `Country`, `State`
and `City` on Detail must not be transliterated field-for-field.

**Fix.** Put **only the leaf geography** in `Category` and move the coarser levels to `Tooltips` (they
are still needed for disambiguation — "Springfield" exists in many states, so either use a composite
`City, State` column from the model or keep explicit Lat/Long). Confirm by counting marks, not by
looking for an error.

**Detection rule worth automating:** any `azureMap` whose `Category` well holds **more than one
`Column` projection** is suspect. It is legal, it validates, and it is almost never what the Tableau
source meant.

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
