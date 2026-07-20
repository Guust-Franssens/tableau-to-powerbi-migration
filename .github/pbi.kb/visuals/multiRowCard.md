# multiRowCard

## Roles

| Role | Kind | Display name | Required | Max per role | Notes |
|---|---|---|---|---|---|
| Values | GroupingOrMeasure | Fields | Yes | many | Fields/measures displayed as repeated card rows. |

## Key formatting objects

- `dataLabels`: KPI value font size, weight, and color.
- `categoryLabels`: label visibility, size, and color.
- `cardTitle`: row/card title size, weight, and color.
- `card`: accent bar visibility/color/weight, padding, and card background.
- Visual container: title, background, border, padding.

## Tableau idiom

Maps a Tableau multi-KPI strip: several related measures/dimensions shown as compact card rows in a single visual.

## Tier verdict

🟡 template-ready — validates structurally in a scratch Superstore PBIR report. Render is not Desktop-captured yet.

## Human Desktop capture instructions

None. Promote to 🟢 only after a Desktop render capture is saved and copied back as ground truth.

## Binding notes

Template binds `Sample Superstore[Region]`, `[CP Sales]`, and `[CP Profit]` to Values.
