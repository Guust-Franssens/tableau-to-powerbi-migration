# table-databars

## Roles

| Role | Kind | Display name | Required | Max per role | Notes |
|---|---|---|---|---|---|
| Values | GroupingOrMeasure | Columns | Yes | many | Table columns/measures. |

## Key formatting objects

- `values`: default cell font sizing.
- `columnFormatting`: per-column cell formatting; current CLI validates `dataBars` here.
- `columnHeaders`: header font size and bold.
- `grid`: vertical/horizontal grid, row padding, text size.
- Visual container: title, background, border, padding.

## Tableau idiom

Maps Tableau in-cell data bars / bar-in-table worksheets to Power BI `tableEx` data bars on a numeric column.

## Tier verdict

🟡 template-ready — validates structurally in a scratch Airline PBIR report against the real `tableEx` model. Render is not Desktop-captured yet.

## Human Desktop capture instructions

None. Promote to 🟢 only after a Desktop render capture confirms the in-cell data bars render on `Passengers Carried`.

## Binding notes

Template binds Airline `Flight Activity` columns Date, Route, Average Fare Usd, Passengers Carried, and Flight Duration Hours. Data bars target metadata `Flight Activity.Passengers Carried`.

## Capability note

The conditional-formatting reference documents table data bars as a `values` cell element, but the current `powerbi-report-author validate` surface rejects `values.dataBars` and accepts `columnFormatting.dataBars`. Treat this as a CLI/Desktop surface mismatch until Desktop capture resolves it.
