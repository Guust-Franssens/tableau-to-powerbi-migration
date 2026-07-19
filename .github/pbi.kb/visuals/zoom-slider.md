# zoom formatting idiom

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
`zoom`: `show`, `showOnCategoryAxis`, `categoryMin`, `categoryMax`, `categorySize`, `showOnValueAxis`, `valueMin`, `valueMax`, `valueSize`, `showLabels`, `showTooltip`, `showOnValueSecAxis`, `valueSecMin`, `valueSecMax`, `valueSecSize`.

## Tableau idiom mapping
Maps Tableau range/axis navigation patterns to Power BI's native zoom slider. The template shows the category-axis slider on a monthly line chart.

## Tier verdict
🟡 structural-template-ready — validates and uses CLI-described properties; Desktop capture can refine initial min/max/slider size if needed.
