# areaChart

## Roles (catalog describe)
| Role | Kind | Required | Max | Notes |
|---|---|---:|---:|---|
| Category | Grouping | Yes | — | X-axis fields. |
| Y | Measure | Yes | — | Primary y-axis values. |
| Series | Grouping | No | 1 | Legend/color split. |
| Y2 | Measure | No | — | Secondary y-axis values. |
| Rows | Grouping | No | — | Small multiples. |
| Tooltips | Measure | No | — | Hover fields. |

## Key formatting objects
categoryAxis, valueAxis, legend, dataPoint, labels, lineStyles, markers, plotArea, referenceLine, trend, y1AxisReferenceLine, y2Axis, zoom, smallMultiplesLayout.

## Tableau idiom mapping
Maps Tableau `Area mark / filled time-series` views onto a native Power BI `areaChart` visual. This worked template binds `Date[Month Start]` plus Superstore measures from `Sample Superstore` using the same projection shape as the committed Superstore PBIR visuals.

## Tier verdict
🟡 structural-template-ready — validates as PBIR structure, but is not Desktop render-captured ground truth.
