# Calculated-field dispositions — FastFashionImpact

Every one of the **35** Tableau calculated fields recovered from the source `.twb`
(`<calculation class='tableau' formula='…'>`; the parser leaves `formula` null across the repo)
is dispositioned below, grouped by its owning data source.

**Totals:** 6 calc columns, 5 measures, 24 dropped, 0 spatial capability gaps.

- **calc column** / **measure** — translated into the model (target table shown).
- **dropped** — empty-string annotation label (report text), or a visual-layout **table calc**
  (`RUNNING_SUM`/`FIRST`/`LOOKUP`/`PREVIOUS_VALUE`) that belongs to the report/custom-visual layer,
  or a calc on the **vestigial** `Radial (Radar Environmental)` source that no worksheet binds.

There is **no spatial (MAKEPOINT/MAKELINE) capability gap here**: all 5 polygon/“silhouette”
sources store their geometry as plain integer `pointX`/`pointY` columns baked into the extract, not
as Tableau `spatial` datatype — so the geometry imports as ordinary columns (see `RELATIONSHIPS.md`).

Model-added helper **not** derived from a Tableau calc field: `Jeans Sheet Swap`[Jeans Sheet Swap Value]
(`SELECTEDVALUE(…, "1")`) — the disconnected-slicer read that the three toggle measures build on.

---

## 1. Polygon Materials (Fast Fashion Data FINAL) → table **Polygon Materials**

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| Point X Flipped | calc column | Polygon Materials | `[pointX] * -1` row-level horizontal mirror. DAX references friendly name `[Point X]` (sourceColumn `pointX`). Ground truth row0 pointX 287 → −287. |
| Materials flag | measure | Jeans Sheet Swap | `[Parameters].[Parameter 3] = '2'` → SELECTEDVALUE toggle **Show Materials**. Parameter-value boolean driving silhouette visibility, relocated to the disconnected param table. |
| REFERENCES | dropped | — | Constant string `'References'`; a caption/label → report text, not data. |

## 2. JEANS (Fast Fashion Data FINAL) → table **JEANS**

_No calculated fields (all base columns: ID, Category, Value (%))._

## 3. Jeans Polygon (Fast Fashion Data FINAL) → table **Jeans Polygon**

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| Point X FLipped | calc column | Jeans Polygon | `[pointX] * -1`. **Source name typo** `FLipped` normalized to `Point X Flipped` for cross-table consistency with the other two mirror columns (flagged to customer; data untouched). |
| Co2 flag | measure | Jeans Sheet Swap | `[Parameters].[Parameter 3] = '1'` → SELECTEDVALUE toggle **Show CO2 Jeans**. |

## 4. Marimekko (Marikekko - Segments) → table **Marimekko**

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| Segment Pulse LOD | measure | Marimekko | `{FIXED [Segment]: SUM([Pulse Score])}` → `CALCULATE(SUM('Marimekko'[Pulse Score]), ALLEXCEPT('Marimekko','Marimekko'[Segment]))`. **Ground truth (dual-method):** Middle Segment=327, Premium=106, Lower Middle / Entry Price Points=196, n/a=80. |
| Calculation1 | dropped | — | `RUNNING_SUM(SUM([Market Share]))` cumulative marimekko **column-width** table calc → report/custom-visual layer (marimekko x-position). |
| Running Total X Axis | dropped | — | `IF FIRST()=0 … LOOKUP(…,-1) … PREVIOUS_VALUE(0) …` cumulative x-position **table calc** → report layer. |

## 5. Radial with Borders (Radar Environmental) → table **Radial with Borders**

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| Distance (r) | measure | Radial with Borders | `AVG([Radial Value])` polar radius; constant per (Impact Area, Year) mark. **Ground truth (dual-method):** Energy emissions 2015=2.97, Water consumption 2030=1.80, Chemicals usage=4.80. |
| Angle | calc column | Radial with Borders | `RUNNING_SUM((2*PI())/MIN({COUNTD([Impact Area])}))` → `(2*PI()/DISTINCTCOUNT(Impact Area)) * RANKX(ALL(Impact Area), …, ASC, Dense)`. **Ordering assumption** (alphabetical rank; only rotates the radar). Ground truth: 5 spokes, step 2π/5, closes at 2π. |
| X-axis | calc column | Radial with Borders | `[Distance (r)] * COS([Angle])` → row-level `'…'[Radial Value] * COS([Angle])` (Radial Value substituted for the aggregate since it is constant per mark). |
| Y-axis | calc column | Radial with Borders | `[Distance (r)] * SIN([Angle])` (internal name `X-axis (copy)`) → row-level `'…'[Radial Value] * SIN([Angle])`. |
| Planetary Boundary | dropped | — | `''` empty-string annotation label → report text. |
| Energy | dropped | — | `''` empty-string label. |
| Land | dropped | — | `''` empty-string label. |
| Chemicals | dropped | — | `''` empty-string label. |
| Waste | dropped | — | `''` empty-string label. |

## 6. Radial (Radar Environmental) → **VESTIGIAL source — no worksheet binds it** (table kept as `Radial (Radar Environmental)` for completeness; all its calcs dropped)

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| Angle | dropped | — | Vestigial duplicate of the live `Radial with Borders` radar; unreferenced by any worksheet. |
| Distance (r) | dropped | — | Vestigial. |
| X-axis | dropped | — | Vestigial. |
| Y-axis | dropped | — | Vestigial. |
| Planetary Boundary | dropped | — | Vestigial `''` label. |
| Energy | dropped | — | Vestigial `''` label. |
| Water | dropped | — | Vestigial `''` label. |
| Land | dropped | — | Vestigial `''` label. |
| Chemicals | dropped | — | Vestigial `''` label. |
| Waste | dropped | — | Vestigial `''` label. |

## 7. Performance Quartile (Quartile Performance) → table **Performance Quartile**

_No calculated fields (base columns: Quartile, Chain Stage, Value)._

## 8. Fibre Production 1970-2018 (Fast Fashion Data FINAL) → table **Fibre Production 1970-2018**

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| Cotton | dropped | — | `''` empty-string annotation label → report text. |
| CO2 | dropped | — | `''` empty-string label. |
| ENERGY | dropped | — | `''` empty-string label. |
| ASTERISK | dropped | — | `''` empty-string label. |
| WATER | dropped | — | `''` empty-string label. |
| PROBLEM | dropped | — | `''` empty-string label. |

## 9. Polygon water (Fast Fashion Data FINAL) → table **Polygon water**

| Tableau field | Fate | Target | Reason |
|---|---|---|---|
| Point X Flipped | calc column | Polygon water | `[pointX] * -1` row-level mirror. |
| Water flag | measure | Jeans Sheet Swap | `[Parameters].[Parameter 3] = '3'` → SELECTEDVALUE toggle **Show Water**. |

## 10. Ripped Polygon (Consumer Importance) → table **Ripped Polygon**

_No calculated fields (base columns: Importance, Value, geometry)._

## 11. JEANS water (Fast Fashion Data FINAL) → table **JEANS water**

_No calculated fields (base columns: ID, Category, Value (%))._

---

## Parameters (from `parameters[]`, not `data_sources[].fields[]`)

| Tableau parameter | Fate | Reason |
|---|---|---|
| Jeans Sheet Swap (`[Parameter 3]`, string, members '1'/'2'/'3', default '1') | modeled | Disconnected 1-column table **Jeans Sheet Swap** + 3 SELECTEDVALUE toggle measures (Show CO2 Jeans / Show Materials / Show Water) + helper `Jeans Sheet Swap Value`. Driven by the 3 dashboard `edit-parameter-action`s. |
| Top Customers (int, 5) | dropped | Vestigial Superstore leftover; unreferenced by any worksheet, calc, or filter. |
| Profit Bin Size (int, 200) | dropped | Vestigial Superstore leftover; unreferenced. |
