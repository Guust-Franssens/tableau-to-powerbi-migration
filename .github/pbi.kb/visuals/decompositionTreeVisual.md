# decompositionTreeVisual

## Roles

| Role | Kind | Display name | Required | Max per role | Notes |
|---|---|---|---|---|---|
| Analyze | Measure | Analyze | Yes | 1 | Metric to break down. |
| ExplainBy | Grouping | Explain by | No | many | Dimensions available as split levels. |
| Tooltips | Measure | Tooltips | No | many | Extra hover metrics. |

## Key formatting objects

- `analysis`: AI mode / AI enablement knobs.
- `tree`: density, accent color, connector color/type, responsive layout, bars per level, click behavior.
- `dataBars`: positive/negative/background colors, width %, scaling type.
- `categoryLabels`: `categoryLabelFontSize`, `categoryLabelFontColor`, font family/style.
- `dataLabels`: `dataLabelFontSize`, `dataLabelFontColor`, units/precision.
- Visual container: title, background, border, padding.

## Tableau idiom

Maps interactive Tableau drill/decomposition sheets where users break a metric down by successive dimensions, with optional AI/high-value splits.

## Tier verdict

🔴 needs-human-Desktop-capture — the PBIR validates structurally, but decomposition tree render/state and AI split configuration cannot be proven from structure alone.

## Desktop click instructions

1. Open the cookbook capture PBIP in Power BI Desktop and add a Decomposition tree visual.
2. Bind Analyze = `Sample Superstore[CP Sales]`.
3. Bind Explain by = `Sample Superstore[Category]`, `Sample Superstore[Region]`, `Sample Superstore[Segment]`.
4. Bind Tooltips = `Sample Superstore[CP Profit]`.
5. In Format, enable AI splits / analysis behavior if available, set bars per level to 5, curved connectors, and responsive layout.
6. On the canvas, click the root `CP Sales` node `+`, choose `Category`, then add one AI split such as High value if Desktop offers it.
7. Save the PBIP and copy the resulting `visual.json` back over this template before marking 🟢.

## Binding notes

Template binds `[CP Sales]` to Analyze, Category/Region/Segment to ExplainBy, and `[CP Profit]` to Tooltips.
