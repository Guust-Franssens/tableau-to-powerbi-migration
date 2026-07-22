# NL Wind Energy Utilization (2024)

Daily performance of a fleet of Netherlands wind turbines for calendar year 2024, plus CO2 savings and
an IronViz-style spiral visual. Use this model to answer questions about energy output, capacity factor,
performance ratio, availability, CO2 saved, and how a month compares to the prior month, at either fleet
level or for a single selected turbine.

## Grain and tables

- `'Daily Performance'`: fact table, one row per turbine per day. Core columns include
  `[Energy Actual Mwh]`, `[Capacity Factor Actual]`, `[Month Number]`.
- `'Turbine'`: turbine dimension (`[Upd Turbine Name]`, onshore/offshore, active flag).
- `'CO2 Savings'`: `[Co2 Saved Tonnes]` per the fleet's output.
- `'NL Densification'`: geography for the map.
- `'* Parameter'` / `'Spiral Start Point'` / `'Thickness'` tables are disconnected slicer proxies (what-if
  and view controls), NOT related dimensions. They feed `[... Parameter Value]` measures only.

## Measure naming conventions

- `CM ...` = current month, scoped by `[Month Parameter Value]`. The sentinel value **2024 means the whole
  year** (no month filter); any 1-12 value filters to that month.
- `PM ...` = prior month (current month minus 1).
- `... MoM ...` = month-over-month change. `Pos` / `Neg` / `Neut` variants split the change by sign and
  exist to drive conditional colouring; do not add them together.
- A leading `T ...` = the same metric filtered to the single turbine selected via
  `[Upd Turbine Name Parameter Value]` (turbine-detail page). Non-prefixed measures are fleet-level.

## Headline measures and verified totals

- `[Total Actual Output (2024)]` = full-year actual output = **453,167.284 MWh**.
- `[Total Co2 Saved]` = **169,031.37 tonnes** CO2 avoided.
- `[CM Capacity Factor]`, `[CM Performance Ratio]`, `[CM Availability]` are percentages (already /100).
- `[CM Homes Powered]`, `[CM Trees]`, `[... Cars Offset]` are illustrative CO2-equivalent conversions.

## Answering guidance

- For fleet output use `[CM Total Output]` (respecting the month slicer) or `[Total Actual Output (2024)]`
  for the year. For one turbine, use the `T ...` variant.
- Mention whether a figure is fleet-level or a single turbine, and which month the `[Month Parameter Value]`
  slicer is on.

## For Copilot (style + visuals)

- Lead with the number and its unit (MWh, tonnes CO2, %); keep to two or three sentences.
- Monthly trends: line or column by month; fleet-vs-turbine or ANSP comparisons: bar. Avoid pie charts.

## Things to avoid

- Do not sum or average `CM`/`PM` measures across months; they are already month-scoped snapshots.
- Do not surface the `Spiral ...` measures (`[Spiral X]`, `[Spiral Y]`, `[Spiral Angle]`, etc.) as answers;
  they are geometry helpers for the spiral visual, not business metrics.
- Do not treat `[Month Parameter Value]` = 2024 as a month; it is the "whole year" sentinel.
- Do not use the `'* Parameter'` tables as dimensions to group by; they are single-value control proxies.
