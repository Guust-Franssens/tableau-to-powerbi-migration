# ribbonChart

## Roles

| Role | Kind | Display name | Required | Max per role |
|---|---|---|---|---|
| Category | Grouping | X-axis | Yes | many |
| Series | Grouping | Legend | No | 1 |
| Y | Measure | Y-axis | Yes | many |
| Rows | Grouping | Small multiples | No | many |
| Tooltips | Measure | Tooltips | No | many |

## Key formatting objects

- `categoryAxis / valueAxis`: show, axisType, labelDisplayUnits, labelPrecision, showAxisTitle
- `legend`: show, position, showTitle
- `layout`: seriesOrderReversed, seriesOrderSorted, stackedGapSize, stackedGapExplodes, ribbonGapSize
- `ribbonBands`: show, fillMatchColor, fillColor, fillTransparency, borderShow
- `labels`: show plus dynamic label/detail options

## Tableau idiom

Tableau rank-flow / bump-chart idiom: category axis over time, series by rank/color, ribbons showing rank changes between periods.

## Tier

🟡 template-ready — structurally validated PBIR template generated from the `powerbi-report-author` catalog/formatting surface and bound to the Superstore model.

## Human Desktop capture instructions

None. This entry is not flagged 🔴; promote to 🟢 only after a Desktop render capture is saved and copied back as ground truth.

## Binding notes

Template binds Date[Month Start] to Category, Region to Series, and [CP Sales] to Y.
