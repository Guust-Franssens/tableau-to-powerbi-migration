# Tableau map worksheets → Azure Maps: a per-mark-type equivalence table

Reference workbook: **`book_6-1-Maps`** (Tableau's own *6-1 Maps* sample, sha256
`1885eea8a6d7189960299f2d45ac227b634b1adc0c16d16319ff121c7a48171b`). It is a deliberately awkward
set — nine worksheets covering symbol, filled, density, dual-layer, satellite and viz-in-tooltip
maps — which makes it a good single fixture for map conversion.

Everything below is read from the `.twb` XML or from emitted PBIR, not inferred from a screenshot.

Sent upstream to the deterministic engine as
[`Yarbrdab000/tableau-fabric-skills#112`](https://github.com/Yarbrdab000/tableau-fabric-skills/issues/112),
which owns Bing→Azure Maps conversion (tracked here by
[`#60`](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/60)).

---

## 1. The mark type is stated in the source — no inference needed

Each worksheet carries a `<mark class='…'>`. That is the highest-signal input for choosing the
Power BI visual, and it is mechanical:

| Tableau `mark class` | worksheet in the fixture | Azure Maps equivalent | how it is encoded |
|---|---|---|---|
| `Automatic` (geo role, no measure on Size) | Filled Map | `azureMap` + **`referenceLayer`** | boundary GeoJSON by URL; `Category` = the location key |
| `Automatic` (geo role + measure) | Symbol Map, Map with Viz in Tooltip | `azureMap` + **`bubbleLayer`** | `Category` = location, `Size` = measure |
| `Heatmap` | Density Map | `azureMap` + **`heatMapLayer`** | `Category` = location; no `Size` needed |
| `Shape` | Mapbox | `azureMap` + `bubbleLayer` | shape vocabulary does not port; bubble is the honest fallback |
| `Pie` | Pie Chart Map | `azureMap` + `bubbleLayer` + `Series` | ⚠️ **no true equivalent** — see §3 |
| `Multipolygon` + `Pie` (two classes, one sheet) | Combined Map | `azureMap` with **`referenceLayer` *and* `bubbleLayer`** | see §3 — this is the valuable one |

## 2. The basemap source style is also stated in the source

`mapControls.defaultStyle` does not need to be guessed from a thumbnail. Per worksheet the `.twb`
carries a `<style-rule element='map'>` and a `<mapsource>`:

```python
for ws in ET.parse(twb).getroot().iter("worksheet"):
    styles  = [(f.get("attr"), f.get("value"))
               for sr in ws.iter("style-rule") if sr.get("element") == "map"
               for f in sr.iter("format")]
    sources = [e.get("name") for e in ws.iter("mapsource")]
```

**Extracting the Tableau side is exact and mechanical. Choosing the Azure Maps equivalent is a
separate judgement, and **none of the three target mappings has been render-compared yet:**

| Tableau source style | Azure `defaultStyle` | confidence | evidence |
|---|---|---|---|
| `mapsource='Tableau'`, no `map-style` | `grayscale_light` | ⚠️ inferred | **source ✅** 6 of 9 worksheets share this config, 3 have reference thumbnails, all light grey; **target ⚠️** a Power BI render at `grayscale_light` exists but was never compared against a Tableau thumbnail |
| `map-style='tableau-z-black'` | `night` | ⚠️ inferred | source read from the `.twb` (Dark Map); target is a name match, no side-by-side render captured |
| `mapsource='Satellite'` | `satellite` | ⚠️ inferred | source read from the `.twb` (Mapbox); target not render-compared |

`washout` (0.0 in most sheets) has no direct Azure Maps control; it is a basemap opacity treatment
and is safe to drop, but worth recording as a fidelity note rather than silently ignoring.

**A worksheet with no `<mapsource>` is not a map.** `Over Time` has none and is correctly a
`columnChart`. Do not let a workbook named "Maps" force a map visual.

## 3. What actually happened to all nine worksheets

Per-worksheet outcome from the single-workbook run (the one that emitted `azureMap` throughout).
This is the table that matters, because **one engine choice broke eight of the nine**:

| Tableau worksheet | mark class | emitted | `Category` (Location) well | outcome vs Tableau |
|---|---|---|---|---|
| Filled Map | `Automatic` | `azureMap` + `referenceLayer` | `[State]` | ✅ 49 polygons vs 49 |
| Combined Map | `Multipolygon` + `Pie` | `azureMap` + `referenceLayer` + `bubbleLayer` | `[State, City]` | ❌ renders at **State** — 49 vs 531 |
| Symbol Map | `Automatic` | `azureMap` + `bubbleLayer` | `[Country, State, City]` | ❌ **1 mark** vs 531 |
| Dark Map | `Automatic` | `azureMap` + `bubbleLayer` | `[Country, State, City]` | ❌ **1 mark** vs 531 |
| Density Map | `Heatmap` | `azureMap` + `heatMapLayer` | `[Country, State, City]` | ❌ **1 mark** vs 531 |
| Mapbox | `Shape` | `azureMap` + `bubbleLayer` | `[Country, State, City]` | ❌ **1 mark** vs 531 |
| Map with Viz in Tooltip | `Automatic` | `azureMap` + `bubbleLayer` | `[Country, State, City]` | ❌ **1 mark** vs 531 |
| Pie Chart Map | `Pie` | `azureMap` + `bubbleLayer` + `Series` | `[Country, State, City]` | ❌ **1 mark** vs 10 |
| Over Time | `Automatic`, **no `<mapsource>`** | `columnChart` | — | ✅ correctly not a map |

**The single highest-value finding: a multi-field Location well collapses every mark to one.**

Stacking geographic columns into `Category` builds a **drill hierarchy**, and `azureMap` renders at
its **top** level until a user drills. `Country` has exactly one distinct value in this extract
(`"United States"`), so any well starting with `Country` is *mathematically guaranteed* to draw one
bubble at the US centroid — confirmed objectively by connected-components over the canvas: exactly
one blob at px (1139, 744), which is `centerLatitude 39.2795` / `centerLongitude −97.4361`.

`Combined Map` generalises the rule: it has **no** `Country` and still fails, rendering at `State`
where Tableau plots at `City`. So it is not "`Country` poisons the well" — **any** multi-field
Location well renders at its top level.

It is validation-invisible: the map draws, the basemap is right, the legend is right, `validate` is
clean. Only counting marks against the source grain finds it.

## 4. The model change that fixes it — and why the obvious fix is also wrong

This is a **semantic-model** change, not a report-layer one. Put only the *leaf* geography in
`Category` — but the leaf is **not** `City`:

- `DISTINCTCOUNT([City])` = **531**
- distinct `(City, State)` pairs = **604**
- **57 city names recur across states** (4 Springfields, 4 Columbias, 3 each of Columbus, Roseville,
  Burlington, Florence, Concord, Lancaster) — **130 of 604 pairs, 21.5% of marks**

So binding `City` alone silently merges or mis-geocodes a fifth of the map. The shipped fix is a
calculated column carrying the qualified key:

```dax
column 'City, State' = IF(OR('Orders'[City] = "", 'Orders'[State] = ""), BLANK(),
                          'Orders'[City] & ", " & 'Orders'[State])
    dataCategory: Address
```

`dataCategory` matters — it tells Azure Maps how to geocode. Expect the mark count to **rise**
(531 → 604): that equals Tableau's own grain when its Detail shelf carried City + State + Country,
so it is a fidelity gain, but say so or it reads as a regression.

**Verify by counting marks against the source grain, never by looking for an error** — the target is
604, and both 1 and 531 render without complaint.

**Detection rule worth automating:** any `azureMap` whose `Category` well holds more than one field
is suspect by construction.

## 5. Two further render-verified defects on these maps

- **A visual-level MEASURE filter silently drops marks.** On `Pie Chart Map`, the filter
  `[Sales (w/o Category)] >= 24711.0D` should keep 10 cities; the map drew 5–6, and the missing ones
  included **New York City, the single largest value in the workbook**. Deleting `filterConfig` took
  the count 5 → 82. `validate` clean throughout.
- **A `Series`/Legend well vetoes a data-bound `referenceLayer.polygonFillColor`.** The measure ramp
  is replaced by categorical legend colours: Texas (`SUM(Profit)` = −25,729) sampled byte-identical
  RGB (156,177,200) to mildly-positive states.

## 6. The two cases that need a judgement call

**Dual-layer (`Combined Map`).** Tableau puts *two* mark classes on one worksheet — `Multipolygon`
(a choropleth) with `Pie` marks on top. `azureMap` supports this natively: a `referenceLayer` for
the polygons plus a `bubbleLayer` for the points, in one visual. This is the highest-value pattern
in the fixture because a naive one-mark-class-per-visual reading loses a whole layer.

⚠️ **Known defect when doing it:** adding a `Series`/legend projection **vetoes** a data-bound
`referenceLayer.polygonFillColor`. The gradient stays in `visual.json`, `validate` reports 0 errors,
and the polygons render as one flat wash — verified by sampling Texas (`SUM(Profit)` = −25,729, the
most negative value in the model) at byte-identical RGB (156,177,200) to mildly-positive states. So
choropleth colouring and a legend are mutually exclusive on the same `azureMap` today.

**Pie-on-map (`Pie Chart Map`).** Power BI has no pie marker for Azure Maps. `bubbleLayer` +
`Series` gives per-category colouring but not per-point pie slices. This is a genuine fidelity loss
and should be reported as one rather than quietly downgraded.

## 7. The finding that matters most: conversion depends on batch composition

Two runs of the **same engine build**, 59 seconds apart, on the **byte-identical** workbook:

| run | batch size | `book_6-1-Maps` output |
|---|---|---|
| `_screen` | 19 workbooks | `filledMap` ×2, `shapeMap` ×4 — **deprecated Bing** |
| `3-maps` | 1 workbook | all `azureMap` ✅ |

Same `tool: migrate_estate`, same `LocalFilesSource`, same input hash. The only recorded difference
is how many workbooks were in the batch.

This matters more than the Bing→Azure conversion itself: a single-workbook pilot renders clean
while the customer's estate run silently emits deprecated visuals, and the difference is invisible
without diffing `visualType` across runs. `book_8-1-Dashboards` in the same estate run also still
carries a Bing `map` (`Category` = `Orders.Map Location`, `Size` = `Sum(Orders.Sales)`).

**Caveat, stated honestly:** I have not reproduced this on demand — it is an observation over two
existing bundles. The confound I cannot fully exclude from the artifacts alone is that the two runs
used different engine working trees despite the timestamps. Worth reproducing upstream before
treating the batch-composition hypothesis as established.

## 8. Suggested detection

Whatever the root cause, deprecated map types are cheap to assert against:

```python
BING = {"map", "filledMap", "shapeMap"}
# fail the run if any emitted visual.json carries one
```

A build-time check would have caught all 7 instances in this estate without any render step.
