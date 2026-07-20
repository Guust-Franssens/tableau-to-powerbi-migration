# error formatting idiom

## Roles (catalog describe: columnChart)
| Role | Kind | Required | Max | Notes |
|---|---|---:|---:|---|
| Category | Grouping | Yes | — | X-axis categories. |
| Y | Measure | Yes | — | Column values. |
| Series | Grouping | No | 1 | Legend/color split. |
| Rows | Grouping | No | — | Small multiples. |
| Tooltips | Measure | No | — | Hover fields. |

## Key formatting objects
`error`: `enabled`, `errorRange` (CLI type: unknown), `barShow`, `barMatchSeriesColor`, `barColor`, `barWidth`, `barBorderColor`, `barBorderSize`, `markerShow`, `markerShape` (`circle`, `square`, `diamond`, `triangle`, `x`, `shortDash`, `longDash`, `plus`, `none`), `markerSize`, label formatting, `labelFormat` (`absolute`, `relativeNumeric`, `relativePercentage`, `range`), tooltip formatting.

## Tableau idiom mapping
Maps Tableau confidence interval / error bar overlays on a column chart. This template enables and styles the `error` object but intentionally avoids inventing the `errorRange` encoding because the CLI reports it as `unknown`.

## Tier verdict
🔴 needs-human-Desktop-capture — render depends on Desktop's `errorRange` payload for fixed, percentage, percentile, or measure-based ranges.

## Desktop click instructions
1. Add a Clustered column chart (or Column chart). Bind X-axis = `Date[Month Start]`, Y-axis = `Sample Superstore[CP Sales]`.
2. Open Format > Error bars. Enable error bars, choose the intended range mode (fixed value, percentage, percentile, or upper/lower bound fields), set bar/marker/label options as needed.
3. Save and capture the emitted `error.errorRange` structure from `visual.json` as render-verified ground truth.
