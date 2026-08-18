# step-line idiom (`lineChart`)

Tableau `mark = line` + stepped interpolation translates to a Power BI `lineChart` with an explicit
step line style.

## Encoding that actually works

- `objects.lineStyles[0].properties.lineChartType = 'step'`
- `objects.lineStyles[0].properties.interpolationStep = 'after'`

## Encoding to avoid

- `objects.lineStyles[0].properties.stepped = true`

That property name is not part of the line-chart formatting surface and can validate as inert JSON,
so it is a known "looks plausible, renders wrong" trap.

## Evidence and confidence

- ⚠️ inferred, needs local re-check: this repository currently has no committed `lineChart` PBIR
  capture that proves a rendered step line end-to-end.
- ✅ verified in committed cookbook context: `interpolationStep` is already part of the documented
  interpolation surface (`.github/pbi.kb/visuals/forecast.md`), so this idiom aligns with existing
  vocabulary and closes the missing recipe gap.

## Tableau idiom mapping

Use this for Tableau stepped lines where each value should hold until the next category bucket.
