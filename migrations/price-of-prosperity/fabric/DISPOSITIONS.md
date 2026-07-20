# Calculated-field dispositions - PriceOfProsperity

The Tableau workbook "The Price of Prosperity" has **1** data source, **5** calculated
fields, **1** parameter, 13 worksheets, and 1 dashboard. Every calculated field, the
parameter, and the two non-data fields are dispositioned below.

**Totals:** 0 calc columns, 0 measures, 3 simplified away, 2 excluded (+1 pseudo-column
excluded, +1 Tableau Group reconstructed). All **4** model measures are NEW simple
aggregations, not translations of the Tableau calc fields.

- **simplified away** - the Tableau construct is superseded by a native Power BI feature
  (a single-select Year slicer); it belongs to the report interaction layer, not the model.
- **excluded** - vestigial constant/helper or Tableau-internal pseudo-column; not real data.
- **reconstructed** - a Tableau Group (not a physical column) rebuilt as a model column at
  the Power Query load layer (see limitations_encountered / guide section 6).


## 1. Renewable Energy vs GDP -> table **Country Indicators**

### Calculated fields (all 5)

| Tableau field | Fate | Reason |
|---|---|---|
| Filter Year | simplified away | Parameter-equality idiom (guide section 2): `[Select Y] = DATETRUNC('year',[Year])`, used only to filter visuals to the selected year. Superseded by a native single-select **Year slicer** cross-filtering the scatters/map. pbi-report-builder owns this. |
| Decrease Year | simplified away | Prior-year helper (`[Select Y] - 1`) for the year "-" navigation button. Superseded by native slicer interaction. |
| Increase Year | simplified away | Next-year helper (`[Select Y] + 1`) for the year "+" navigation button. Superseded by native slicer interaction. |
| True | excluded | Vestigial boolean-constant helper. Not referenced by any retained visual. |
| False | excluded | Vestigial boolean-constant helper. Not referenced by any retained visual. |

### Parameter

| Tableau parameter | Fate | Reason |
|---|---|---|
| Select Y (1990-2023) | simplified away | Its only real use is the `Filter Year` calc above. NOT materialized as a Power BI what-if parameter; a native Year slicer supersedes it. |

### Non-data fields

| Field | Fate | Reason |
|---|---|---|
| Renewable Energy vs GDP.csv (`data_type: table`) | excluded | Tableau internal relationship-model table-anchor pseudo-column (spec limitation #2). Not real data. |
| [Regions] (Tableau Group) | reconstructed | Categorical-bin GROUP on `[Country Name]` (6 continents), NOT a physical extract column; parser left it `UNRESOLVED:[Regions]`. Rebuilt as the **Continent** column from `data/dim_region_group.csv` and merged onto the fact at the Power Query load layer. |

### Model measures (NEW aggregations - serve both the country-grain scatters and the year-grain trend charts)

| Measure | DAX | Ground-truth check (2018, filtered domain) |
|---|---|---|
| Avg CO2 per Capita | `AVERAGE([CO2 per Capita])` | Global = 4.1304; Africa = 1.1662 over 54 countries |
| Avg GDP per Capita | `AVERAGE([GDP per Capita])` | Europe = 41,562.07 |
| Total Population | `SUM([Population])` | Global = 7,636,740,830 |
| Avg Renewable Energy % | `AVERAGE([Renewable Energy %])` | (validated across trend spot-checks) |


## 2. Analysis-domain filters (Power Query load layer)

Three workbook **data-source-level** filters (they affect ALL worksheets) are replicated at
the Power Query load layer - the faithful equivalent:

1. `[Regions] except "NULL"` -> drop 48 World-Bank regional-aggregate pseudo-countries
   (World, Arab World, European Union, income groups, ...) that map to `Continent = "NULL"`
   (1,632 rows).
2. `[Country Name] except "Greenland"` -> drop outlier.
3. `[Year] except {2021, 2022, 2023}` -> keep 1990-2020.

Net analysis domain = **216 countries x 1990-2020 = 6,696 rows**. Rationale: NOT applying
filter (1) inflates Total Population ~10x (82.7B vs the correct 7.64B for 2018) because the
WB aggregates double-count member countries. Applying all three reproduces
`data/ground_truth.json` exactly.


## 3. Validation

- **Structure / engine load:** `TmdlSerializer.DeserializeDatabaseFromFolder` + AS-engine
  `ConnectFolder` (offline) both load cleanly: 1 table, 10 columns, 4 measures, en-US culture
  with 10 synonym entities. Integrity asserts pass (measure-name uniqueness model-wide; no
  measure == column name in a table; every DAX `[bracket]` token resolves).
- **AI readiness:** `scripts/check_ai_readiness.py` -> 100% description coverage (15/15), no
  categorical column missing its domain values.
- **Numeric ground truth:** every `data/ground_truth.json` target reproduced via an identical
  pandas filter pipeline (2018 Total Population = 7,636,740,830; 2018 global Avg CO2/cap =
  4.1304; Africa 2018 avg CO2/cap = 1.1662 / 54 countries; Europe 2018 avg GDP/cap =
  41,562.07; plus 1990/2000/2010/2018/2020 trend spot-checks and all 6 continents - all PASS).
  A live DAX `EVALUATE` re-check is deferred to the Desktop/report phase (the offline engine
  connection is read-only and rejects DAX query operations).
