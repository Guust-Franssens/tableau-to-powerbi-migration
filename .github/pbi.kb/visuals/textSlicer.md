# textSlicer (Input slicer)

## Role

| Role | Kind | Display name | Required | Max per role | Notes |
|---|---|---|---|---|---|
| Values | GroupingOrMeasure | Field | Yes | 1 | Text column being searched. |

## Key formatting / filter payload

- `objects.dropdown[0].properties.filterMode = 'Filter_Contains_Any'`
- Initial search term is persisted in `objects.general[0].properties.filter.filter` as a `Contains`
  condition.

## Tableau idiom mapping

Maps Tableau typed-parameter text search patterns (for example
`CONTAINS(LOWER([Customer Name]), LOWER([Parameter]))`) to a native Power BI input slicer.

## Tier verdict

🟢 render-verified ground truth (from the measured migration run described in issue #176).

## Evidence and confidence

- ⚠️ inferred, needs local re-check in this environment: no Desktop/engine plugin is available here,
  so this session could not replay the rendering experiment.
- ✅ verified from committed repo scope: this entry is intentionally tracked as a native capability,
  and the cookbook index now includes it as covered (not out-of-scope).

## Tooling gotcha

`powerbi-report-author formatting describe-object textSlicer ...` does not currently expose the full
property slot used for the persisted `Contains` filter payload. Treat this as a known CLI visibility
gap, not as evidence that input slicers are unsupported.
