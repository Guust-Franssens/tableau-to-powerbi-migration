# heatMap

## Roles

| Role | Kind | Display name | Required | Max per role |
|---|---|---|---|---|
| Category | Grouping | Location | No | many |
| Series | Grouping | Legend | No | 1 |
| Y | GroupingOrMeasure | Latitude | No | 1 |
| X | GroupingOrMeasure | Longitude | No | 1 |
| Size | Measure | Bubble size | No | 1 |
| Tooltips | Measure | Tooltips | No | many |

## Key formatting objects

- `heatMap`: show, filterRadius, transparency, unit (pixels/meters), color0, color50, color100
- `mapControls`: autoZoom, zoomLevel, centerLatitude, centerLongitude, showZoomButtons, showLassoButton, geocodingCulture
- `mapStyles`: mapTheme (aerial/canvasDark/canvasLight/grayscale/road), showLabels
- `bubbles`: bubbleSize, markerRangeType (magnitude/dataRange/auto)
- `dataPoint`: defaultColor, transparency, fill/fillRule

## Tableau idiom

Tableau density heatmap / density map: geographic locations weighted by a measure. For matrix-style highlight tables, use matrix/table conditional background formatting instead.

## Tier

🟡 template-ready — structurally validated PBIR template generated from the `powerbi-report-author` catalog/formatting surface and bound to the Superstore model.

## Human Desktop capture instructions

None. This entry is not flagged 🔴; promote to 🟢 only after a Desktop render capture is saved and copied back as ground truth.

## Binding notes

Template binds State to Location, Region to Legend, and [CP Sales] to Bubble size with heat map layer enabled.
