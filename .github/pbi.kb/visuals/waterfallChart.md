# waterfallChart

## Roles

| Role | Kind | Display name | Required | Max per role |
|---|---|---|---|---|
| Category | Grouping | Category | Yes | 1 |
| Breakdown | Grouping | Breakdown | No | 1 |
| Y | Measure | Y-axis | Yes | 1 |
| Tooltips | Measure | Tooltips | No | many |

## Key formatting objects

- `sentimentColors`: increaseFill, decreaseFill, totalFill, otherFill
- `breakdown`: maxBreakdowns
- `y1AxisReferenceLine`: show, displayName, value, position, width, lineColor, style, dataLabelShow, shadeShow
- `categoryAxis / valueAxis`: show, labelDisplayUnits, labelPrecision, totalsEnabled
- `labels`: show, labelPosition, labelDisplayUnits

## Tableau idiom

Tableau waterfall / contribution bridge: stage/category deltas by measure, optionally broken down by region; includes sentiment colors and a break-even reference line.

## Tier

🟡 template-ready — structurally validated PBIR template generated from the `powerbi-report-author` catalog/formatting surface and bound to the Superstore model.

## Human Desktop capture instructions

None. This entry is not flagged 🔴; promote to 🟢 only after a Desktop render capture is saved and copied back as ground truth.

## Binding notes

Template binds Sub-Category to Category, Region to Breakdown, and [CP Profit] to Y; sentiment colors, breakdown limit, and y1AxisReferenceLine are included.
