# pieChart

## Roles

| Role | Kind | Display name | Required | Max per role |
|---|---|---|---|---|
| Category | Grouping | Legend | Yes | 1 |
| Series | Grouping | Details | No | 1 |
| Y | Measure | Values | Yes | many |
| Tooltips | Measure | Tooltips | No | many |

## Key formatting objects

- `legend`: show, position, showTitle, titleText, fontFamily, fontSize, labelColor
- `labels`: show, labelStyle, position, labelPrecision, percentageLabelPrecision, background
- `slices`: startAngle

## Tableau idiom

Tableau pie marks: one categorical color/detail field sized by a measure. Use sparingly for low-cardinality part-to-whole shares.

## Tier

🟡 template-ready — structurally validated PBIR template generated from the `powerbi-report-author` catalog/formatting surface and bound to the Superstore model.

## Human Desktop capture instructions

None. This entry is not flagged 🔴; promote to 🟢 only after a Desktop render capture is saved and copied back as ground truth.

## Binding notes

Template binds Sub-Category to Category and [CP Sales] to Y.
