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
| Filled/region map shaded by a measure (choropleth) | Prefer `azureMap` with a data-bound `referenceLayer` (GeoJSON/KML/WKT/SHP/CSV) and conditional `polygonFillColor`; use the built-in `filledMap` layer only for standard geographies where approximate Microsoft boundaries are acceptable. | ✅ green render-verified | Proven in Superstore at `migrations\superstore-sales-performance\fabric\SuperstoreSalesPerformance.Report\definition\pages\prescriptive\visuals\9d3297e633e4cdaa9e20\visual.json`; Learn says data-bound reference layers match shape properties to the Location field and support conditional formatting. Source: https://learn.microsoft.com/en-us/azure/azure-maps/power-bi-visual-add-reference-layer, ms.date 2025-01-17. |
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

Green/render-verified source: `migrations\superstore-sales-performance\fabric\SuperstoreSalesPerformance.Report\definition\pages\prescriptive\visuals\9d3297e633e4cdaa9e20\visual.json`.

That visual is an `azureMap` choropleth using a two-entry `objects.referenceLayer` array:

1. Datasource entry: `datasourceType` is the literal `'url'`, and `referenceLayerUrl` points to `https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json`.
2. Conditional polygon style entry: has a wildcard selector (`dataViewWildcard.matchingOption = 1`) and sets `polygonFillColor.solid.color.expr.FillRule` where the input is measure `'Sample Superstore'[Map KPI Difference]`; the `linearGradient3` fill rule uses red min `#FC4237`, gray midpoint `#E6E6E6` at `0D`, blue max `#34657F`, and `nullColoringStrategy = 'asZero'`.

The same visual keeps the Power BI data binding minimal: `query.queryState.Category` contains only `'Sample Superstore'[State]`, allowing Azure Maps to data-bind the state names to the GeoJSON properties. `objects.mapControls` fixes the default style to grayscale light, hides style/navigation/selection controls, and pins the continental-US viewport; `objects.bubbleLayer` is still present with `show = true`, but the choropleth effect comes from `referenceLayer.polygonFillColor`.

## Open questions / needs-human-capture

- 🟥 Need one render-verified PBIR exemplar each for `pathLayer`, `heatMapLayer`, clustered `bubbleLayer`, and built-in `filledMap` layer. The installed catalog exposes the objects and Learn describes the UX, but structural validity is not enough for cookbook-grade PBIR generation.
- 🟥 Exact PBIR encoding for marker layer image/icon conditional formatting should be human-captured from Desktop if a Tableau workbook uses custom mark shapes/icons.
- 🟥 `shapeMap` exists in the installed catalog, but current guidance and installed deprecation metadata point map migrations to `azureMap`; use Shape Map only after a human confirms Azure Maps cannot meet the requirement.
- 🟨 Path-layer limitation to remember: Learn says data-bound reference layer is unavailable when path layer is enabled, so route maps that also need custom territories may require separate visuals or a tile/reference workaround.
- 🟨 Reference-layer data prep is critical: simplify and filter high-detail files, because only the first 30,000 reference-layer features render.
