# lineStackedColumnComboChart

## Roles (catalog describe)
| Role | Kind | Required | Max | Notes |
|---|---|---:|---:|---|
| Category | Grouping | Yes | — | Shared X-axis. |
| Y | Measure | No | — | Stacked column y-axis values. |
| Y2 | Measure | No | — | Line y-axis values. |
| Series | Grouping | No | 1 | Column stack/legend. |
| Rows | Grouping | No | — | Small multiples. |
| Tooltips | Measure | No | — | Hover fields. |

## Key formatting objects
categoryAxis, valueAxis (including sec* secondary-axis properties), legend, dataPoint, lineStyles, markers, totals, y1AxisReferenceLine, zoom.

## Tableau idiom mapping
Maps Tableau `Dual-axis combo: stacked columns plus line` views onto a native Power BI `lineStackedColumnComboChart` visual. This worked template binds `Date[Month Start]` plus Superstore measures from `Sample Superstore` using the same projection shape as the committed Superstore PBIR visuals.

## Tier verdict
🟡 structural-template-ready — validates as PBIR structure, but is not Desktop render-captured ground truth.
