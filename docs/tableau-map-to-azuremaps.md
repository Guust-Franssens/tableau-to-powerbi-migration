# Tableau map worksheets → Azure Maps: a per-mark-type equivalence table

Reference workbook: **`book_6-1-Maps`** (Tableau's own *6-1 Maps* sample, sha256
`1885eea8a6d7189960299f2d45ac227b634b1adc0c16d16319ff121c7a48171b`). It is a deliberately awkward
set — nine worksheets covering symbol, filled, density, dual-layer, satellite and viz-in-tooltip
maps — which makes it a good single fixture for map conversion.

Everything below is read from the `.twb` XML or from emitted PBIR, not inferred from a screenshot.

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
separate judgement, and only the first row is render-verified:**

| Tableau source style | Azure `defaultStyle` | confidence | evidence |
|---|---|---|---|
| `mapsource='Tableau'`, no `map-style` | `grayscale_light` | ✅ verified | 6 of 9 worksheets share this config; 3 have reference thumbnails, all light grey |
| `map-style='tableau-z-black'` | `night` | ⚠️ inferred | source read from the `.twb` (Dark Map); target is a name match, no side-by-side render captured |
| `mapsource='Satellite'` | `satellite` | ⚠️ inferred | source read from the `.twb` (Mapbox); target not render-compared |

`washout` (0.0 in most sheets) has no direct Azure Maps control; it is a basemap opacity treatment
and is safe to drop, but worth recording as a fidelity note rather than silently ignoring.

**A worksheet with no `<mapsource>` is not a map.** `Over Time` has none and is correctly a
`columnChart`. Do not let a workbook named "Maps" force a map visual.

## 3. The two cases that need a judgement call

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

## 4. The finding that matters most: conversion depends on batch composition

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

## 5. Suggested detection

Whatever the root cause, deprecated map types are cheap to assert against:

```python
BING = {"map", "filledMap", "shapeMap"}
# fail the run if any emitted visual.json carries one
```

A build-time check would have caught all 7 instances in this estate without any render step.
