# Relationship map & blend assumptions — FastFashionImpact

The source workbook is an **11-data-source infographic constellation**: 34 worksheets, 1 dashboard,
each worksheet an independent illustrated panel (polygon silhouettes, a radar, a marimekko, a fibre
line chart, quartile bars…). The parser flagged many worksheets as "multi-source", but that is a
**false positive**: the second "source" on every such worksheet is the Tableau `Parameters`
pseudo-source (the `[Parameter 3]` sheet-swap), **not** a second real data source.

**There are ZERO genuine Tableau data blends in this workbook.** Every worksheet binds exactly one
real `federated.*` data source; the dashboard has only 3 `edit-parameter-action`s (sheet-swap
navigation) and **no cross-worksheet filter/highlight actions**. So there is no blend to translate
into a physical join for 9 of the 11 sources — they are modeled as **independent tables**.

Two source **pairs**, however, exhibit the classic Tableau "geometry table + value table" idiom where
a silhouette polygon is segmented by the *same* dimension that a small value table is keyed on. In
Tableau these would be **blended on that shared dimension**; in Power BI we make the blend explicit as
a physical relationship (with a documented cardinality/direction), which is cleaner and lets the report
cross-highlight silhouette segments from the value table.

> **Blend ≠ physical join (semantics caveat).** A Tableau blend *aggregates the secondary source to
> the linking dimension, then links* — it never fans out the primary. A Power BI relationship links at
> row grain and lets filters propagate. For both pairs below the "one" side is a **unique-key value
> table** (verified), so a single-direction many→one relationship reproduces the blend's
> aggregate-then-link behaviour without fan-out risk: filtering the value table filters the geometry,
> and aggregating a value-table measure is unaffected by the geometry row count. Where that guarantee
> would **not** hold (no unique shared key), the sources are left independent (see "Rejected", below).

---

## Built relationships (2)

### 1. `Jeans Polygon`[Shape Id] → `JEANS`[ID]  (many → one, single-direction)

| Property | Value |
|---|---|
| From (many) | `Jeans Polygon`[Shape Id]  (223 geometry rows, 8 shapes) |
| To (one) | `JEANS`[ID]  (8-row lifecycle value table) |
| fromCardinality / toCardinality | many / one |
| crossFilteringBehavior | oneDirection (JEANS filters Jeans Polygon) |
| Key integrity (verified) | `JEANS.ID` = {0..7}, **8 distinct/unique** (valid one-side); `Jeans Polygon.shapeId` = {0..7}, **0 values unmatched** to JEANS.ID. |
| Rationale | The jeans silhouette is drawn as 8 stacked shapes, one **per garment-lifecycle stage**; `JEANS` holds the `Value (%)` and `Category` (Cotton Production, Yarn Production, Garment Production, Distribution & retailing, User phase, End of life) for each of those 8 stages. `shapeId` **is** the lifecycle-stage key. This is the Tableau blend-on-ID made explicit. |
| Direction rationale | `JEANS` is the dimension (unique key, descriptive attributes + %); `Jeans Polygon` is the many-row detail (geometry). Single direction dim→detail is correct and avoids ambiguous bidirectional paths. |

### 2. `Polygon water`[Category] → `JEANS water`[Category]  (many → one, single-direction)

| Property | Value |
|---|---|
| From (many) | `Polygon water`[Category]  (179 geometry rows) |
| To (one) | `JEANS water`[Category]  (4-row water-use value table) |
| fromCardinality / toCardinality | many / one |
| crossFilteringBehavior | oneDirection (JEANS water filters Polygon water) |
| Key integrity (verified) | `JEANS water.Category` = {Raw Materials, Fabric Mill, Industrial Laundry, Consumer wash}, **4 distinct/unique**; `Polygon water.Category` = same 4, **0 unmatched**. |
| Rationale | The water-droplet silhouette is segmented into the 4 water-use stages; `JEANS water` holds `Value (%)` per stage. Same geometry-table + value-table idiom, blended on `Category`. |
| Direction rationale | `JEANS water` is the unique-key dimension; `Polygon water` the many-row geometry. dim→detail single direction. |

Both are authored in `relationships.tmdl` with explicit `fromCardinality`/`toCardinality`/
`crossFilteringBehavior` (no `///` comments on relationship objects — TMDL forbids doc-comments there;
the rationale lives here instead).

---

## Rejected relationship candidates (kept independent — documented)

| Candidate | Why rejected |
|---|---|
| `Radial with Borders` ↔ `Radial (Radar Environmental)` on `Impact Area` | `Radial (Radar Environmental)` is **vestigial** — no worksheet binds it (it is a leftover duplicate of the live `Radial with Borders`). Relating to dead data adds noise. Kept as an isolated table. |
| `Polygon Materials`[Category] ↔ `JEANS`/`JEANS water` on Category | **Disjoint domains.** Polygon Materials categories are fibre types (Cotton, Polymer synthetics, Wood-based synthetics); JEANS categories are lifecycle stages; JEANS water categories are water-use stages. No shared key → no relationship. Polygon Materials already carries its own `Category` + `Value (%)` baked into the extract, so it is self-contained. |
| `Fibre Production 1970-2018`[Year] ↔ `Radial with Borders`[Year] (a "Year" conformed dim) | **Disjoint / incompatible grain.** Fibre Year is a continuous 1970–2018 annual series; Radial Year is two scenario snapshots (2015, 2030). A shared Year dimension would be semantically wrong (annual production vs scenario years) and there is no worksheet that crosses them. Left independent. |
| `Marimekko`, `Performance Quartile`, `Ripped Polygon`, `Polygon Materials`, `Fibre Production` inter-relations | Each is a standalone infographic panel over its own metric with **no shared key** to any other source and **no dashboard action** linking them. Independent tables. |
| `Polygon water` / `Jeans Polygon` ↔ their geometry siblings on `Point Id`/`Shape Id` | `Point Id`/`Shape Id` are **within-table geometry indices**, not cross-table keys. No relationship. |

---

## Disconnected table (1)

`Jeans Sheet Swap` — a 1-column, 3-row (`"1"`,`"2"`,`"3"`) disconnected slicer table backing the
Tableau `[Parameter 3]` sheet-swap. **Intentionally unrelated** to every other table; it drives the
silhouette-visibility toggle measures (`Show CO2 Jeans` / `Show Materials` / `Show Water`) via
`SELECTEDVALUE`, exactly as a disconnected what-if/parameter table should. See `DISPOSITIONS.md`.

---

## Summary

- **12 tables**: 11 data-source tables (incl. 1 vestigial `Radial (Radar Environmental)`) + 1
  disconnected `Jeans Sheet Swap` param table.
- **2 relationships**, both explicit **many → one, single-direction**, both with **verified clean
  key integrity**, both reproducing a Tableau geometry⊕value **blend-on-shared-dimension** as a
  physical join.
- **9 independent tables** — no genuine blend or shared key exists to justify a relationship; forcing
  one would invent structure the workbook does not have.
