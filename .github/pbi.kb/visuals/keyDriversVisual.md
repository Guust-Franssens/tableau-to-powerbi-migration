# keyDriversVisual

## Roles

| Role | Kind | Display name | Required | Max per role | Notes |
|---|---|---|---|---|---|
| Target | GroupingOrMeasure | Analyze | No | many | Field/measure to analyze. |
| ExplainBy | GroupingOrMeasure | Explain by | No | many | Candidate influencers. |
| Details | Grouping | Expand by | No | many | Evaluation grain for summarized targets/measures. |

## Key formatting objects

- `keyDrivers`: analysis mode, key-influencer/profile toggles, numeric analysis type, count type, sort.
- `keyInfluencersVisual`: primary/secondary/canvas colors and font colors.
- `keyDriversDrillVisual`: drill visual default color and reference-line color.
- Visual container: title, background, border, padding.

## Tableau idiom

Maps Tableau explanatory-analysis sheets to Power BI Key influencers: identify dimensions/values that drive a target metric.

## Tier verdict

🔴 needs-human-Desktop-capture — the PBIR validates structurally, but AI visual initialization, target-analysis mode, and influencer output are render/runtime behaviors.

## Desktop click instructions

1. Open the cookbook capture PBIP in Power BI Desktop and add a Key influencers visual.
2. Bind Analyze = `Sample Superstore[CP Profit]`.
3. Bind Explain by = `Sample Superstore[Sub-Category]` and `Sample Superstore[Segment]`.
4. Bind Expand by = `Sample Superstore[Region]`.
5. In Format/Analysis, select Key influencers, continuous numeric analysis, impact sort, and enable profiles if available.
6. Wait for the influencers list to compute; click one influencer so the drill/profile pane materializes.
7. Save the PBIP and copy the resulting `visual.json` back over this template before marking 🟢.

## Binding notes

Template binds `[CP Profit]` to Target, Sub-Category/Segment to ExplainBy, and Region to Details.
