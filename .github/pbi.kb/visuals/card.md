# card

## Roles

| Role | Kind | Display name | Required | Max per role | Notes |
|---|---|---|---|---|---|
| Values | Measure | Fields | Yes | many | Measure displayed as the legacy single-card number. |

## Key formatting objects

- `labels`: value font size, weight, color, display units, and precision.
- `categoryLabels`: measure label visibility, font size, and color.
- `wordWrap`: category/value wrapping behavior.
- Visual container: title, background, border, padding.

## Tableau idiom

Maps a legacy Tableau KPI/BAN card where one measure is shown as a large headline value.

## Tier verdict

🟡 template-ready — validates structurally in a scratch Superstore PBIR report. Render is not Desktop-captured yet.

## Human Desktop capture instructions

None. Promote to 🟢 only after a Desktop render capture is saved and copied back as ground truth.

## Binding notes

Template binds `Sample Superstore[CP Sales]` to Values.
