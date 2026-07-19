# treemap

## Roles

| Role | Kind | Display name | Required | Max per role |
|---|---|---|---|---|
| Group | Grouping | Category | No | 1 |
| Details | Grouping | Details | No | 1 |
| Values | Measure | Values | Yes | many |
| Tooltips | Measure | Tooltips | No | many |

## Key formatting objects

- `layout`: tilingMethod (stableSquarified/binary/alternating), innerPadding, outerPadding
- `labels`: show, fontSize, labelPrecision
- `categoryLabels`: show, fontSize
- `dataPoint`: fill, fillRule

## Tableau idiom

Tableau treemap / packed part-to-whole rectangles: category group plus detail leaves sized by a measure.

## Tier

🟡 template-ready — structurally validated PBIR template generated from the `powerbi-report-author` catalog/formatting surface and bound to the Superstore model.

## Human Desktop capture instructions

None. This entry is not flagged 🔴; promote to 🟢 only after a Desktop render capture is saved and copied back as ground truth.

## Binding notes

Template binds Category to Group, Sub-Category to Details, and [CP Sales] to Values.
