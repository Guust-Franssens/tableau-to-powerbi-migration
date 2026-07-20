# smallMultiplesLayout idiom

## Roles (catalog describe: lineChart)
| Role | Kind | Required | Max | Notes |
|---|---|---:|---:|---|
| Category | Grouping | Yes | — | X-axis fields. |
| Y | Measure | Yes | — | Primary y-axis values. |
| Series | Grouping | No | 1 | Legend/color split. |
| Y2 | Measure | No | — | Secondary y-axis values. |
| Rows | Grouping | No | — | Small multiples. |
| Tooltips | Measure | No | — | Hover fields. |

## Key formatting objects
`smallMultiplesLayout`: `layoutType` (`auto`, `custom`), `rowCount`, `columnCount`, `backgroundColor`, `backgroundTransparency`, `gridLineType`, `gridLineShow`, `gridLineWidth`, `gridLineColor`, `gridLineTransparency`, `gridLineStyle`, `gridPadding`, `advancedPaddingOptions`, `columnPaddingInner`, `columnPaddingOuter`, `rowPaddingInner`, `rowPaddingOuter`.

## Tableau idiom mapping
Maps Tableau trellis / small multiples to the Power BI `Rows` role plus the `smallMultiplesLayout` formatting object. The template uses a `lineChart`, `Date[Month Start]`, `[CP Sales]`, and `Sample Superstore[Region]` as the multiple.

## Tier verdict
🟡 structural-template-ready — grid layout validates; Desktop capture can still refine visual density and header styling.
