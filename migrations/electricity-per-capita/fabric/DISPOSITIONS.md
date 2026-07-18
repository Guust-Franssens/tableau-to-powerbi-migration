# Calculated-field dispositions - ElectricityPerCapita

Every one of the **61** Tableau calculated fields across the 5 data sources is dispositioned below.

**Totals:** 29 calc columns, 15 measures, 12 dropped, 5 capability gaps.

- **calc column** / **measure** - translated into the model (table shown).
- **dropped** - visual-layout table calc (trellis/index/window-normalization) or redundant alias; belongs to the report, not the model.
- **capability gap** - spatial MAKEPOINT geometry for the Sankey; no native Power BI equivalent (see limitations_encountered).


## 1. per-capita-electricity-fossil-nuclear-renewables -> table **Per Capita FNR**

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| FF% | calc column | Per Capita FNR | Row-level ratio FF/(FF+Nuc+Ren). Ground truth World 2022=0.61432. |
| Nuclear% | calc column | Per Capita FNR | Row-level ratio. World 2022=0.09152. |
| Renewables % | calc column | Per Capita FNR | Row-level ratio. World 2022=0.29416. |

## 2. tree -> table **Tree** (+ consumption calcs relocated to **Elec Generation**)

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| X revised (for nodes) | calc column | Tree | Node-only scaling by Usage/{MAX(Usage)}. |
| X fixed for last path | calc column | Tree | DIMENSION-FIXED LOD {FIXED [id]: MAX(IF type='link' THEN path)} -> CALCULATE(MAX(Path), ALLEXCEPT(Tree,Id), Type='link'). Ground truth mlp verified. |
| Calculation1 | calc column | Tree | FIXED[id] LOD {FIXED [id]: AVG(Usage)} -> CALCULATE(AVERAGE(Usage), ALLEXCEPT(Tree,Id)). |
| Elec gen at 2022 | calc column | Elec Generation | IF [Year]=2022 THEN percap. Relocated row-level column. |
| Max year | measure | Elec Generation | WINDOW table calc WINDOW_MAX(MAX([Year]))=MAX([Year]) -> CALCULATE(MAX(Year), ALLEXCEPT(Entity)) vs MAX(Year) in context. Norway@2023=28056.230, @2022=BLANK. |
| X-trellis | dropped | - | Small-multiple grid X = (index()-1)%int(SQRT(size())). Native PBI small-multiples feature. |
| Y-trllis | dropped | - | Small-multiple grid Y. Native PBI small-multiples. |
| Consumption axis | calc column | Tree | = [child] passthrough. |
| Year Axis Consumption | dropped | - | = [Year] redundant alias; report binds Elec Generation[Year]. |
| Year Axis CY | dropped | - | = [CY] redundant alias; use measure [CY]. |
| Consumption Axis Test | dropped | - | WINDOW_MIN(...) table calc for mark positioning; visual-only. |
| X Axis -  Bar chart | measure | Tree | MIN(0.0) -> constant 0 axis anchor measure. |
| X Axis - Rank | measure | Tree | MIN(0.0) -> 0 axis anchor. |
| X Axis - Text | measure | Tree | MIN(0.0) -> 0 axis anchor. |
| Y Axis Position | dropped | - | Depends on Consumption Axis Test + Normalised consumption; visual layout. |
| Normalised consumption | dropped | - | Window min/max normalization; use visual axis normalization. |
| CY Consumption | measure | Elec Generation | DIMENSION-FIXED LOD {FIXED [child]: sum(IF Year=CY THEN percap)}; CY hoisted to VAR. Ground truth Norway@2023=28056.230. |
| CY | measure | Elec Generation | {MAX([Year])} partition-less LOD -> CALCULATE(MAX(Year), ALL()). =2023. |
| DZV: show tree map | measure | Show chart as | [Parameter 1]='Tree' -> SELECTEDVALUE toggle measure. |
| Calculation2 | measure | Elec Generation | Boolean [CY Consumption] > {FIXED [Region]: AVG(percap)}; measure hoisted to VAR (no compact filter). |
| Source label | calc column | Tree | IF type='link' THEN [parent]. |
| Region and country count | calc column | Elec Generation | FIXED[Region] COUNTD(Entity) -> ALLEXCEPT(Region) label string. |
| X Normalized | calc column | Tree | [x]/{MAX([x])} -> DIVIDE(X, MAXX(ALL(Tree),X)). |
| Filter | calc column | Tree | Boolean [type]='link' AND ISNULL([parent]) (top-of-tree). |
| Child (copy) | calc column | Tree | = [child] duplicate. |
| DZV: table with region | measure | Show chart as | [Parameter 1]='Table'. |
| DZV: table without region | measure | Show chart as | [Parameter 1]='Table no region'. |
| Links with usage | capability gap | - | MAKEPOINT link geometry scaled by usage; Sankey gap. |
| Nodes | capability gap | - | MAKEPOINT node geometry; no native visual. Underlying X/Y Normalized retained. |
| Max year (copy) | calc column | Elec Generation | IF [Year]=[CY] THEN percap -> calc column 'Consumption at CY'. |
| Nodes with usage | capability gap | - | MAKEPOINT node geometry scaled by usage; Sankey gap. |
| Links | capability gap | - | MAKEPOINT link geometry; Sankey gap. |
| Border | capability gap | - | MAKEPOINT border geometry; Sankey gap. |
| Y Normalized | calc column | Tree | [y]/{MAX([y])}. |
| Y fixed for last path | calc column | Tree | FIXED[id] LOD on Y (internal 'X doubled for last path (copy)'). |
| Y revised (for nodes) | calc column | Tree | Node-only scaling on Y. |

## 3. Pivoted -> table **Pivoted**

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| X-trellis | dropped | - | Small-multiple grid X; native PBI small-multiples. |
| Y-trllis | dropped | - | Small-multiple grid Y; native PBI small-multiples. |
| Year for label | calc column | Pivoted | IF [Year]=2020 THEN [Year]. |
| FF Electricity% | calc column | Pivoted | FF/(FF+Nuc+Ren) over the FIXED LODs. USA 2022=0.59652. |
| Region and country count | calc column | Pivoted | FIXED[Region] COUNTD(Country) -> ALLEXCEPT(Region) label. |
| FF Electricity | calc column | Pivoted | DIMENSION-FIXED LOD {FIXED [Year],[Code]: SUM(FF)} -> CALCULATE(SUM, FILTER(ALLEXCEPT(Year,Code), Fuel='Fossil Fuel')). USA 2022=7559.257. |
| Rank | dropped | - | index() rank; use visual Top-N / rank. |
| DZV: World | measure | View data for | [Parameter 2]='World' -> SELECTEDVALUE toggle. |
| DZV: Region | measure | View data for | [Parameter 2]='Region'. |
| DZV: Country | measure | View data for | [Parameter 2]='Country'. |
| Nuclear Electricity | calc column | Pivoted | FIXED[Year,Code] Nuclear. FRA 2022=4560.504. |
| Nuclear Electricity% | calc column | Pivoted | Nuc/(...). FRA 2022=0.62795. |
| Renewable Electricity | calc column | Pivoted | FIXED[Year,Code] Renewables. |
| Renewable Electricity% | calc column | Pivoted | Ren/(...). USA 2022=0.22351. |

## 4. Elec generation per capita + regions -> table **Elec Generation**

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| Global avg in 2022 | measure | Elec Generation | Literal 3616.7 (=FNR World 2022 total). Constant reference-line measure. |
| Ratio | measure | Elec Generation | AVG(percap)/3616.7 benchmark ratio. |

## 5. By source -> table **By Source** (VESTIGIAL - no worksheet binds it)

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| % of Nuclear Electricity | calc column | By Source | Row-level Nuclear ratio (vestigial). |
| % of Renewable Electricity | calc column | By Source | Row-level Renewable ratio (vestigial). |
| X-trellis | dropped | - | Small-multiple grid X (vestigial). |
| Y-trllis | dropped | - | Small-multiple grid Y (vestigial). |
| % of Fossil Fuel Electricity | calc column | By Source | Row-level FF ratio (vestigial source). |
| Year for label | calc column | By Source | IF [Year]=2020 THEN [Year] (vestigial). |
