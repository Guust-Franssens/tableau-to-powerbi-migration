# kpi

## Roles

| Role | Kind | Display name | Required | Max per role | Notes |
|---|---|---|---|---|---|
| Indicator | Measure | Value | Yes | many | Performance measure shown as the headline number. |
| TrendLine | Grouping | Trend axis | No | many | Time/period field used for the KPI spark trend. |
| Goal | Measure | Target | No | many | Target measure used for status and distance-to-goal. |

## Key formatting objects

- `indicator`: display units, precision, headline font size/weight/color, status icon.
- `trendline`: show/hide and trend transparency.
- `goals`: showGoal, goalText, showDistance, distanceLabel, direction, goal/distance label colors.
- `status`: good/neutral/bad colors and positive/negative direction semantics.
- Visual container: title, background, border, padding.

## Tableau idiom

Maps Tableau KPI/BAN with an optional target and a small trend line. Use when a Tableau sheet shows one primary KPI number plus a target/goal indicator over time.

## Tier verdict

🟡 template-ready — validates structurally in a scratch Superstore PBIR report. Render is not Desktop-captured yet.

## Human Desktop capture instructions

None. Promote to 🟢 only after a Desktop render capture is saved and copied back as ground truth.

## Binding notes

Template binds `Sample Superstore[CP Sales]` to Indicator, `Date[Month Start]` to TrendLine, and `Sample Superstore[CP Profit]` to Goal.
