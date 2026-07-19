# donutChart

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
- `slices`: innerRadiusRatio, startAngle
- `centerValue / value`: show, valueDisplayUnits, valuePrecision

## Tableau idiom

Tableau donut-style part-to-whole: pie share with a center KPI/label space. Native Power BI supports the ring and center value.

## Tier

🟡 template-ready — structurally validated PBIR template generated from the `powerbi-report-author` catalog/formatting surface and bound to the Superstore model.

## Human Desktop capture instructions

None. This entry is not flagged 🔴; promote to 🟢 only after a Desktop render capture is saved and copied back as ground truth.

## Binding notes

Template binds Segment to Category and [CP Sales] to Y, with center value enabled.
