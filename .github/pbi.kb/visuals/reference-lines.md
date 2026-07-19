# referenceLine / trend / y1AxisReferenceLine idiom

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
`referenceLine` and `y1AxisReferenceLine`: `displayName`, `value`, `position` (`back`, `front`), `show`, `width`, `lineColor`, `transparency`, `style` (`solid`, `dashed`, `dotted`, `custom`), `autoScale`, `dashArray`, `dashCap` (`none`, `round`, `square`), label properties, and shade properties. `trend`: `show`, `width`, `lineColor`, `transparency`, `style`, `displayName`, `combineSeries`, `useHighlightValues`.

## Tableau idiom mapping
Maps Tableau constant reference lines and linear trend lines. The CLI-described PBIR surface supports explicit numeric constant lines; it did not expose average/min/max/percentile aggregation selectors in `formatting describe-object`.

## Tier verdict
🔴 needs-human-Desktop-capture — constant lines and trend validate structurally, but average / min / max / percentile analytics lines are not fully described by the CLI.

## Desktop click instructions
1. Add a Line chart. Bind X-axis = `Date[Month Start]`, Y-axis = `Sample Superstore[CP Sales]`.
2. In Analytics, add one Constant line at `50000`, one Average line, one Min line, one Max line, and one Percentile line (for example 90th percentile). Add a Trend line.
3. Turn data labels on for each line and save. Capture the resulting `visual.json` to replace this structural template if Desktop emits additional analytics objects/properties.
