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

**Attribution matters here and I got it wrong once** — see the correction note at the end of this
section. Two different things happened, in sequence:

**(a) What the ENGINE emits** (pristine, no downstream tooling):

| Tableau worksheet | mark class | engine emitted | location well |
|---|---|---|---|
| Combined Map | `Multipolygon` + `Pie` | `filledMap` ⚠️ Bing | `Orders.State` |
| Dark Map | `Automatic` | `shapeMap` ⚠️ Bing | `Orders.City` |
| Filled Map | `Automatic` | `shapeMap` ⚠️ Bing | `Orders.State` |
| Mapbox | `Shape` | `filledMap` ⚠️ Bing | `Orders.City` |
| Map with Viz in Tooltip | `Automatic` | `shapeMap` ⚠️ Bing | `Orders.City` |
| Symbol Map | `Automatic` | `shapeMap` ⚠️ Bing | `Orders.City` |
| **Pie Chart Map** | `Pie` | **`pieChart`** — geography dropped | `Orders.Category` |
| **Density Map** | `Heatmap` | **no page emitted** | — |
| Over Time | `Automatic`, no `<mapsource>` | `columnChart` ✅ | `Orders.Order_Date` |

The engine binds **one** location column throughout — which is correct.

**(b) What OUR tier then did**: converted every Bing visual to `azureMap`, added the missing Density
Map page, and — the defect — bound `Category` to `[Country, State, City]`, which collapsed 6 of the
maps to a single mark at the US centroid. That was later fixed by rebinding to a composite
`'City, State'` key (§4). Both the defect and the fix are ours.

**The durable lesson: a multi-field Location well collapses every mark to one.**

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

## 7. RETRACTED: "conversion depends on batch composition"

**This section previously claimed the engine's map conversion was non-deterministic** — Bing in a
19-workbook batch, `azureMap` when the same workbook ran alone. **That was wrong.** It is kept here
rather than deleted because the way it failed is the useful part.

**What was actually compared:** a pristine engine bundle (`_screen`) against one **our own agents had
already rewritten in place** (`3-maps`). The `azureMap` output was ours, not the engine's. Batch size
was never the variable.

**What refutes it in one command:** `4-dashboards` is a **single-workbook** run and its engine output
is Bing `shapeMap`. One workbook, still Bing. That bundle sat on disk throughout.

**The evidence that was already in hand:** the engine's own handover for `book_6-1-Maps` carries *two*
visual-type lists — `filledMap`/`shapeMap`/`pieChart` (the engine's) and a later all-`azureMap`
rewrite (ours). The validator sign-off even noted "a later pass rewrote the visuals and did not
regenerate the handover". Both were read without connecting them.

**Why it survived a self-check:** the stated caveat was *"I cannot exclude that the two runs used
different engine working trees"* — a plausible-sounding doubt aimed at the wrong thing. Naming a
sophisticated confound is not the same as ruling out the simple one, and it can substitute for
checking. The cheap check (open the third bundle) was never run.

**Generalisable rule: `<bundle>/reports/` is not pristine engine output once a fix pass has run.** Any
claim about engine behaviour must come from a bundle no agent has touched — or from the handover's
original list, which records what the engine actually said before anything rewrote it.

## 8. Suggested detection

Whatever the root cause, deprecated map types are cheap to assert against:

```python
BING = {"map", "filledMap", "shapeMap"}
# fail the run if any emitted visual.json carries one
```

A build-time check would have caught all 7 instances in this estate without any render step.
