# hundredPercentStackedAreaChart

## Roles (catalog describe)
| Role | Kind | Required | Max | Notes |
|---|---|---:|---:|---|
| Category | Grouping | Yes | — | X-axis fields. |
| Y | Measure | Yes | — | Values normalized to 100%. |
| Series | Grouping | No | 1 | Legend/color stack segments. |
| Rows | Grouping | No | — | Small multiples. |
| Tooltips | Measure | No | — | Hover fields. |

## Key formatting objects
categoryAxis, valueAxis, legend, dataPoint, labels, lineStyles, markers, totals, trend, y1AxisReferenceLine, zoom, smallMultiplesLayout.

## Tableau idiom mapping
Maps Tableau `100% stacked area / share-of-total over time` views onto a native Power BI `hundredPercentStackedAreaChart` visual. This worked template binds `Date[Month Start]` plus Superstore measures from `Sample Superstore` using the same projection shape as the committed Superstore PBIR visuals.

## Tier verdict
🟡 structural-template-ready — validates as PBIR structure, but is not Desktop render-captured ground truth.
