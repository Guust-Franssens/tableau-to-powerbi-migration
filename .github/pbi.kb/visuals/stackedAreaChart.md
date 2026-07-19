# stackedAreaChart

## Roles (catalog describe)
| Role | Kind | Required | Max | Notes |
|---|---|---:|---:|---|
| Category | Grouping | Yes | — | X-axis fields. |
| Y | Measure | Yes | — | Stacked y-axis values. |
| Series | Grouping | No | 1 | Legend/color stack segments. |
| Rows | Grouping | No | — | Small multiples. |
| Tooltips | Measure | No | — | Hover fields. |

## Key formatting objects
categoryAxis, valueAxis, legend, dataPoint, labels, lineStyles, markers, totals, trend, y1AxisReferenceLine, zoom, smallMultiplesLayout.

## Tableau idiom mapping
Maps Tableau `Stacked area mark / cumulative composition over time` views onto a native Power BI `stackedAreaChart` visual. This worked template binds `Date[Month Start]` plus Superstore measures from `Sample Superstore` using the same projection shape as the committed Superstore PBIR visuals.

## Tier verdict
🟡 structural-template-ready — validates as PBIR structure, but is not Desktop render-captured ground truth.
