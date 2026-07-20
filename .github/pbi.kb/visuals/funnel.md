# funnel

## Roles

| Role | Kind | Display name | Required | Max per role |
|---|---|---|---|---|
| Category | Grouping | Category | Yes | 1 |
| Y | Measure | Values | Yes | many |
| Tooltips | Measure | Tooltips | No | many |

## Key formatting objects

- `categoryAxis`: show, fontSize, color
- `labels`: show, labelPosition, funnelLabelStyle, labelPrecision, percentageLabelPrecision, enableBackground
- `percentBarLabel`: show, fontSize, color
- `dataPoint`: defaultColor, showAllDataPoints, fill, fillRule

## Tableau idiom

Tableau funnel / staged conversion bar: ordered stage/category by value, with percent-of-first or percent-of-previous labels.

## Tier

🟡 template-ready — structurally validated PBIR template generated from the `powerbi-report-author` catalog/formatting surface and bound to the Superstore model.

## Human Desktop capture instructions

None. This entry is not flagged 🔴; promote to 🟢 only after a Desktop render capture is saved and copied back as ground truth.

## Binding notes

Template binds Sub-Category stages to Category and [CP Sales] to Y; sort/order should be set by the report builder if a true stage order exists.
