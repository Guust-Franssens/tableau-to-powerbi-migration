# table-cond-format

## Roles

| Role | Kind | Display name | Required | Max per role | Notes |
|---|---|---|---|---|---|
| Values | GroupingOrMeasure | Columns | Yes | many | Table columns/measures. |

## Key formatting objects

- `values`: default font sizing plus per-column background `FillRule` and icon cell element.
- `columnHeaders`: header font size and bold.
- `grid`: vertical/horizontal grid, row padding, text size.
- Visual container: title, background, border, padding.

## Tableau idiom

Maps Tableau background/icon conditional formatting to Power BI `tableEx`: value-driven cell background gradients plus traffic-light icons on a numeric column.

## Tier verdict

🟡 template-ready — validates structurally in a scratch Airline PBIR report against the real `tableEx` model. Render is not Desktop-captured yet.

## Human Desktop capture instructions

None. Promote to 🟢 only after a Desktop render capture confirms both the background gradient and traffic-light icons render on `Passengers Carried`.

## Binding notes

Template binds Airline `Flight Activity` columns Date, Route, Average Fare Usd, Passengers Carried, and Flight Duration Hours. Conditional background and icons target metadata `Flight Activity.Passengers Carried`.

## Capability note

The conditional-formatting reference documents icon sets as `iconRule`; the current `powerbi-report-author validate` surface exposes the table cell property as `values.icon`, so this template uses `icon` to stay structurally valid.
