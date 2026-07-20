# shapeMap

## Roles

| Role | Kind | Display name | Required | Max per role | Notes |
|---|---|---|---|---|---|
| Category | Grouping | Location | Yes | many | Shape key / territory name to match the TopoJSON features. |
| Series | Grouping | Legend | No | many | Optional categorical color split. |
| Value | Measure | Color saturation | No | many | Measure used for choropleth saturation. |
| Tooltips | Measure | Tooltips | No | many | Extra hover metrics. |

## Key formatting objects

- `shape`: datasource type, TopoJSON map URL/reference, projection.
- `legend`: visibility, position, title, font size, label color.
- `dataPoint`: default fill and value-driven `FillRule` gradient.
- `zoom`: auto/selection/manual zoom behavior.
- Visual container: title, background, border, padding.

## Tableau idiom

Maps custom-territory Tableau maps/polygons to Power BI Shape map with a custom TopoJSON shape reference and a measure-driven choropleth.

## Tier verdict

🔴 needs-human-Desktop-capture — the PBIR validates structurally, but Shape map custom TopoJSON loading, feature-key matching, and projection render cannot be guaranteed without Desktop.

## Desktop click instructions

1. Open the cookbook capture PBIP in Power BI Desktop and ensure Shape map visuals are enabled if Desktop prompts for preview features.
2. Add a Shape map visual.
3. Bind Location = `Sample Superstore[State]`.
4. Bind Color saturation = `Sample Superstore[CP Profit Ratio]`.
5. Bind Tooltips = `Sample Superstore[CP Sales]`.
6. In Format > Shape, choose Custom map. If Desktop requires a local file, download `https://raw.githubusercontent.com/deldersveld/topojson/master/countries/us-states/US-States.json` and import it; otherwise set the map URL/reference to that TopoJSON.
7. Set projection to Albers USA if available, apply a two-color gradient, verify state names match, save, and copy the resulting `visual.json` back over this template before marking 🟢.

## Binding notes

Template binds State to Category, `[CP Profit Ratio]` to Value, and `[CP Sales]` to Tooltips.
