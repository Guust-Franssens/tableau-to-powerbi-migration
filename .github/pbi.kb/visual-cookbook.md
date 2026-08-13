# PBIR Visual Cookbook

Ground-truth PBIR (`visual.json`) encodings for every Power BI visual type + formatting idiom we
migrate Tableau vizzes into. The goal: the `pbi-report-builder` agent **copies a proven encoding**
instead of guessing, and we only put a human in the loop for the genuinely render-uncertain cases.

## Why this exists

The azureMap-choropleth episode showed the failure mode: `powerbi-report-author validate` passes
0-errors while the rendered visual is broken, and there were **zero public PBIR examples** to copy.
The fix was to have a human build it once in Desktop and capture the resulting `visual.json` as
ground truth. This cookbook generalizes that: capture/verify each encoding **once**, reuse forever.

## Two tiers of confidence

1. **Structural template** (🟡) — built deterministically from the `powerbi-report-author` CLI
   (`catalog describe <type>` for roles, `formatting effective-properties <type>` for the formatting
   surface) and `validate`d. Structurally correct, but the *render* is not guaranteed. Treat a 🟡
   entry as **cached CLI output**: no more authoritative than calling the CLI live, and it can go
   stale — on any conflict, the **live CLI wins**.
2. **Render-verified ground truth** (🟢) — proven by an actual rendered visual in one of our
   migrations, OR hand-built by a human in Power BI Desktop and captured here. This is the **one thing
   more trustworthy than the CLI for *composition*** (the CLI describes properties in isolation and
   `validate` green-lights structurally-valid-but-wrong JSON; a 🟢 entry actually rendered). Use it for
   anything where structure alone is insufficient (dynamic field parameters + slicer defaults, azureMap
   reference layers, custom polygon/geometry marks, dual-axis secondary binding, analytics-pane lines).

**Neither tier is a claim about the *data*.** Both tiers describe the **shape** of a `visual.json` —
see the two sections below for exactly how narrow that guarantee is, and for the one line in every
entry you must not copy blindly.

## What a green `validate` does NOT prove — it never checks model bindings

Every entry here is `validate`d at 0 errors. That is a claim about **shape only**. Measured
(2026-08-13, CLI 0.1.4), rewriting a *passing* entry's every `Entity` → `NoSuchTable_ZZZ` and every
`Property` → `NoSuchColumn_ZZZ` — 6 substitutions in `visuals/treemap.visual.json` — still returns:

```json
{"result":"succeeded","errorCount":0,"warningCount":0}
```

So `validate` will happily green-light a visual bound to tables and columns **that do not exist in the
semantic model**. Concretely, for a copied cookbook entry:

- ✅ it proves — the JSON parses, the `visualType` exists, the roles/properties are structurally legal.
- ❌ it does not prove — that `Entity`/`Property` resolve against *your* model, that `queryRef` /
  `nativeQueryRef` agree with the field they name, that the measure is the right one, or that anything
  renders.

**After rebinding a copied entry, the binding is verified by a Desktop open + render (and a value
compared against the Tableau source), never by `validate`.** This is the repo-wide rule — structural
validation is necessary, not sufficient — stated where it is easiest to forget: a 🟢 tier is green for
*composition*, and says nothing about the fields you just swapped in.

## The `$schema` line — copy an encoding, but pin the version yourself

A `visualContainer` `$schema` URL that 404s makes `powerbi-report-author validate` **skip JSON-schema
checking entirely** and still print `0 errors`, with a single `PBIR_SCHEMA_UNREACHABLE` warning as the
only trace. A dead `$schema` therefore does not fail loudly — it *silently downgrades* every copy made
from that entry.

Measured 2026-08-13 by direct fetch of
`https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/<v>/schema.json`:

| version | HTTP |
|---|---|
| `2.16.0`, `2.15.0`, `2.14.0`, `2.13.0`, `2.12.0`, `2.11.0`, `2.10.0` | **404** |
| `2.9.0` ← newest that resolves | 200 |
| `2.8.0` … `2.1.0` | 200 |

`visualContainer` is the only kind with a dead version in use here. Re-measured the same day across
every distinct `$schema` under `examples/`: `page/2.1.0` + `page/1.4.0`, `pagesMetadata/1.0.0` +
`1.1.0`, `versionMetadata/1.0.0`, `report/3.0.0` + `3.3.0` and the theme schema all return 200 — so
they were left alone. Don't generalize a version across kinds; each is published independently
(`page` stops at `2.1.0`, `report` at `3.3.0`).

**`0 errors` is also what a *skipped* validation prints, so the fix is proved by injection, not by a
green run.** Measured 2026-08-13, same defect (`"x": "NOT_A_NUMBER"`) in the same visual:

| `$schema` | result |
|---|---|
| `2.11.0` (dead) | `0 error(s), 1 warning(s); result=succeededWithWarnings` — defect invisible |
| `2.9.0` (resolves) | `1 error(s); result=failed` — `Schema validation: /position/x must be number` |
| `2.1.0` (resolves) | `1 error(s); result=failed` — same |

Rules:

- **Cookbook entries declare `2.9.0`** — the newest version that actually resolves. Two 🟢 entries
  (`actionButton`, `shape`) keep the `2.1.0` their Desktop capture wrote: it resolves, validates at
  0 errors, and editing a ground-truth capture would make it no longer ground truth.
- **The `examples/` deliverables are swept too, so an *in-situ* copy is safe as well.** 776
  `visual.json` files under `examples/**` declared the dead `2.11.0`; all now declare `2.9.0`
  (`$schema` line only — no encoding changed). The 64 files at `2.1.0` (interactive-resume's
  Desktop-built report + one `image`) were **deliberately left**: `2.1.0` resolves and really
  validates (table above), so they were never schema-skipped, and they are ground-truth captures for
  the same reason `actionButton`/`shape` are. They do also pass at `2.9.0` (measured), so pin them if
  you ever have a reason to — it is a preference, not a fix.
- **Re-check the table before trusting it.** Versions get published; the number above is a
  *measurement with a date*, not a constant. `curl -I <url>` is the whole check.
- **`PBIR_SCHEMA_UNREACHABLE` means "schema validation did NOT run"** — never "warning, but fine". If
  it appears after you copy an entry, the `$schema` you pasted is dead; fix it and re-validate before
  reading the error count. (The deterministic engine currently emits `2.10.0`, which 404s, so its
  output is schema-skipped too — upstream's to fix, but it means a clean engine `validate` is weaker
  than it looks.)
- Same finding, stated for authoring rather than copying, in
  `.github/skills/powerbi-report-gotchas/SKILL.md` (§1 "Validation-invisible rendering bugs", and its
  header note that `2.9.0` is the newest published).

## Precedence — CLI for current truth, cookbook for proven shapes, MS Learn for the mapping

The CLI and the cookbook answer *different* questions. The CLI is the **live vocabulary** (roles,
properties, enums — always reflects the installed version). The cookbook is a **cache of worked
compositions**. Use them in this order:

1. **Which visual to use** → research **Microsoft Learn** for current best practice (esp. maps),
   cross-checked against `catalog list`/`catalog describe`. Product capabilities move; don't assume.
2. **Encoding vocabulary** (roles/props/enums) → the **CLI, first and always** — it catches cookbook
   staleness.
3. **Encoding composition** → **🟢 render-verified cookbook entry** if one exists (reconcile its
   property names against the CLI) **> compose from the CLI** (🟡 templates are stale cache, defer to
   live CLI) **> research + human capture** (which then becomes a new 🟢 entry).

Each `visuals/<type>.md` carries a `## MS Learn best practice (as of <date>)` section with a dated
citation, refreshed by the report-builder's per-idiom research subtasks — so the mapping guidance stays
current instead of freezing. See `.github/agents/pbi-report-builder.agent.md` ("Research subtasks").

## The CLI is the research tool (deterministic, no guessing)

```
powerbi-report-author catalog list                      # all built-in types + deprecations (57 on CLI 0.1.4)
powerbi-report-author catalog describe <type>           # field-well roles, required/optional, formatting objects
powerbi-report-author formatting effective-properties <type>   # every formatting surface for the type
powerbi-report-author formatting describe-object <type> <object>
powerbi-report-author validate <path-to-.Report-dir>    # structural validation after every edit
```

Deprecated (do not emit): `filledMap` -> `azureMap`, `map` -> `azureMap`, `qnaVisual` (unsupported in PBIR).

## What's actually in here (measured against CLI 0.1.4, 2026-07-28)

An empirical sweep of all 29 `visuals/*.md` entries against `catalog describe` gives the honest
breakdown — **use it to decide whether opening an entry is even worth a lookup** (19 + 7 + 3 = 29;
27 of the 29 also ship a sibling `visuals/<type>.visual.json` — `azureMap` and `forecast` do not):

| Category | Count | Do you need the cookbook? |
|---|---|---|
| **Typed entries whose role tables exactly match the CLI** (zero drift found) | **19** | ❌ **No.** Their Roles / formatting-object sections are literally transcribed `catalog describe` output. Call the CLI instead — it's live and can't go stale. Their only residual value is the Tableau-idiom mapping + tier verdict. |
| **Idiom entries** — `error-bars`, `reference-lines`, `smallmultiples`, `zoom-slider`, `table-cond-format`, `table-databars`, `forecast` | **7** | ✅ **Yes.** These are *not visual types*: `catalog describe error-bars` → `VISUAL_TYPE_UNKNOWN`. They document a technique applied to a host visual, which the CLI has no concept of. `forecast` is the strongest case: the CLI *does* describe a `forecast` object, but only its cosmetics — the entry exists to tell you the model parameters are **not authorable at all**. |
| **Render-truth entries** — `actionButton`, `shape`, `azureMap` | **3** | ✅ **Yes, critically.** These carry behaviour the CLI cannot know and in one case gets actively wrong. |

**The canonical proof that CLI vocabulary ≠ render truth:** `catalog describe actionButton` reports
`"deprecated": false` and a `text` formatting object — i.e. perfectly usable. In reality Desktop
**ignores `visual.objects` and draws a blank rectangle**, while `validate` still returns 0 errors. Only
`visuals/actionButton.md` tells you to use `shape` instead. Rule of thumb: **the CLI is authoritative
for what you may *declare*; only a render-verified entry is authoritative for what actually *draws*.**

**When adding a new entry:** if all you would write is the roles/formatting tables, **don't** — that's a
cache of a command that already exists. Add an entry only for an idiom, a render-verified composition,
or a behavioural trap.

## Confidence map (Tableau-relevant types + idioms)

Legend: 🟢 render-proven · 🟡 structural template (CLI) · 🔴 needs human Desktop capture · ⛔ no native visual (marketplace `.pbiviz` / capability gap)

### 🟢 Proven in our migrations (copy from the cited migration)

> **These rows have NO local `visuals/<type>.visual.json` file** — unlike the 🟡/🔴 entries below, the
> 🟢 core types are proven *in situ*. To copy one, resolve it from the cited migration's PBIR, e.g.:
> `Select-String -Path examples\*\fabric\*.Report\definition\pages\*\visuals\*\visual.json -Pattern '"visualType": "columnChart"'`
> then open that `visual.json` and rebind fields. If a row's location is vague ("all migrations"), any
> hit from that glob is a valid, render-proven starting point. Do **not** treat a missing
> `visuals/<type>.md` as "unproven" for these types.
>
> **At copy time, check the `$schema` line you just pasted — before rebinding, not after validating.**
> Every `examples/` source declares a version that resolves today (`2.9.0`, or `2.1.0` in the
> Desktop-built reports), so a straight copy is fine; keep whichever one you copied. But the line is
> *inherited*, so if your destination report or the engine's output declares a **404** version
> (the engine emits `2.10.0`), the pasted visual is schema-skipped in its new home even though the
> source was clean. Rewrite it to `2.9.0` **then** validate — `PBIR_SCHEMA_UNREACHABLE` afterwards is
> the last line of defence, not the check.

| Type | Example location |
|---|---|
| `columnChart` / `clusteredColumnChart` | airline `9f2607ea` pages |
| `barChart` / `clusteredBarChart` | airline / superstore |
| `hundredPercentStackedBarChart` | (used) |
| `lineChart` | superstore descriptive trends |
| `scatterChart` | superstore prescriptive `377a8368` |
| `cardVisual` | superstore descriptive cards |
| `gauge` | superstore descriptive gauges |
| `tableEx` / `pivotTable` (matrix) | airline `ba1e195d` / tale-of-100 |
| `azureMap` (choropleth reference-layer) | superstore prescriptive `9d3297e6` (render-verified, Desktop-built) — ⚠️ **blank-render gate**: azureMap needs the *"Users can use the Azure Maps visual"* tenant setting + a signed-in Desktop, else it renders **empty with 0 validate errors and no error glyph**. 60-second minimal bisect + controls in `visuals/azureMap.md` (🟢 render-verified negative, `book_6-1-Maps` 2026-07-19) |
| `slicer` (list/dropdown, single/multi) | all migrations |
| `pageNavigator`, `textbox` | all migrations |
| `shape` (fill + tileShape + **`visualLink` WebUrl**) | interactive-resume `bg-dtc` (🟢 render-verified: hexagon/oval badges + clickable Web-URL buttons; **embedded `text` object does NOT render → use textbox overlay**; see `visuals/shape.md`) |
| `actionButton` (static Web URL / link button) | interactive-resume — 🔴 **render-broken**: Desktop ignores `visual.objects`, draws a blank rectangle (validate still 0-errors). **Use `shape` instead** (same `visualLink`); see `visuals/actionButton.md` |

### 🎯 Research targets (Tableau-common, not yet proven) — being templated by cluster agents
- **Cartesian family**: `areaChart`, `stackedAreaChart`, `hundredPercentStackedAreaChart`,
  `hundredPercentStackedColumnChart`, `lineClusteredColumnComboChart` + `lineStackedColumnComboChart`
  (dual-axis: `Y`=columns, `Y2`=line) — Tableau dual-axis / combined-axis / area marks.
- **Cartesian idioms**: `smallMultiplesLayout` (Tableau trellis/small multiples), analytics/reference
  lines (`referenceLine`, `trend`, `y1AxisReferenceLine`, constant/min/max/average/percentile),
  `error` bars, `zoom` slider.
- **Part-to-whole / flow**: `pieChart`, `donutChart`, `treemap`, `funnel`, `ribbonChart`,
  `waterfallChart` (Category/Breakdown/Y + `sentimentColors`), `heatMap`.
- **KPI / card family**: `kpi` (classic goal/trend), `multiRowCard`, `card` (legacy single).
- **Maps (non-azure)**: `shapeMap` (custom TopoJSON), `map` bubble (deprecated -> prefer azureMap bubble layer).
- **AI visuals**: `decompositionTreeVisual`, `keyDriversVisual` (key influencers).
- **Table idioms**: data-bar / icon / background conditional formatting (`conditional-formatting.md`
  Type 4 data bars), report-page tooltips, drill-through.

### ⛔ No native visual — Tableau idioms that need a marketplace `.pbiviz` or are a capability gap
Sankey (Tableau flow), radar/spider, bullet graph, box-and-whisker, chord, network/node-link,
custom polygon marks (IronViz triangle/hex geometry via `INDEX()`). Log these as
`limitations_encountered` HIGH-severity gaps; note the closest native fallback + the marketplace
visual name if one exists.

### ➖ Out of scope (not Tableau-migration relevant)
`rdlVisual`, `dataQueryVisual`, `realTimeLineChart`, `scriptVisual` / `pythonVisual`,
`accessibleTable`, `animatedNumber`, `scorecard`, `bookmarkNavigator`, `aiNarratives`,
niche slicers (`listSlicer`/`textSlicer`/`advancedSlicerVisual`/`filterSlicer` beyond core `slicer`).

## Layout

- `visual-cookbook.md` — this index.
- `visuals/<type>.visual.json` — one minimal worked example per type (tier noted in its sibling `.md`).
- `visuals/<type>.md` — roles, the idiom notes, tier (🟡/🟢), and any human-capture instructions.

## Human capture workflow (for 🔴 render-uncertain items)

1. Open the cookbook capture report (a PBIP bound to a simple generic model) in Power BI Desktop.
2. On the page for the flagged visual, follow the textbox click instructions to build it.
3. Save. The saved `visual.json` under that page's `visuals/` folder is the ground truth — copy it to
   `visuals/<type>.visual.json` and mark the entry 🟢.
