# Health Tracker

Personal daily health-metrics dashboard: one subject, one row per calendar day (body, activity, vitals,
wellbeing). Answers questions about daily values and how the most recent day compares to an earlier one.

## Grain and tables

- `'Health Data'`: one row per day (`[Date]`); holds the raw readings and every measure.
- `'Date Period'`: a disconnected slicer ("Last Week"/"Last Month"/"Last Year") that only feeds
  `[Date Period Value]`. Not related to `'Health Data'`, and not a calendar.

## Measure-naming conventions

- `Latest <metric>` = the value on `[Latest Date]` (the max `[Date]` in the data).
- `Prior <metric>` = the same metric on the day chosen by the `'Date Period'` slicer (Last Week =
  latest - 7 days; Last Month / Last Year = same day one month / one year earlier).
- `<metric> Delta` = Latest minus Prior. `'Health Data'[Latest Week]` = trailing 7 days incl. the max date.

## Business terminology and defaults

- "Latest" / "current" / "now" = the max `[Date]` in the data, never the system date (fixed extract).
- "How does it compare" / "vs last week/month/year" = the `[<metric> Delta]` measure; state the current
  `'Date Period'` selection.
- Blood pressure is systolic/diastolic: `[Latest BP Combined]` for display, `[BP text]` for the clinical
  category. `[BMI text]` and `[HR text]` are the equivalent text classifiers.

## For Copilot (style + visuals)

- Lead with the number, then one line of context; two or three sentences.
- Trends over time: line or column chart on `[Date]`. Avoid pie charts.

## Things to avoid

- Do not sum or average `Latest*` / `Prior*` measures across dates; they are single-day snapshots.
- Do not group by or use `'Date Period'` as a date axis; it is a parameter proxy, not a calendar.
