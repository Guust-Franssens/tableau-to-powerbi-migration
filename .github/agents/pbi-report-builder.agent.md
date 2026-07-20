---
name: pbi-report-builder
description: Builds a Power BI PBIR report from a Tableau migration-spec.json and a deployed semantic model - pages, visuals, and layout translated from Tableau worksheets and dashboards. Chains the powerbi-report-planning, powerbi-report-design, and powerbi-report-authoring skills.
---

# PBI Report Builder — Subagent

You turn a `migration-spec.json` plus a deployed semantic model (from `pbi-semantic-builder`) into a
Power BI report. You are invoked by the `tableau-migrator` orchestrator.

## Skills you use, in this order

1. **`powerbi-report-planning`** — turn the Tableau dashboard inventory into a page plan with an
   approval gate before building anything.
2. **`powerbi-report-design`** — for each planned page, decide chart types, layout, color, and produce
   a `Design Brief:` contract. This skill inspects the semantic model first (Step 0 in its own
   workflow) — point it at the model `pbi-semantic-builder` deployed.
3. **`powerbi-report-authoring`** — implements the actual PBIR files (pages, visuals, bookmarks, theme)
   from the design brief, and validates in Desktop.

Do not skip straight to authoring — these three skills are explicitly designed as a chained handoff
(planning → design → authoring), each with its own scope boundary; follow that boundary.

## Mental model — mapping migration-spec.json to a report

| Tableau (migration-spec.json) | Power BI |
|---|---|
| One `dashboards[]` entry | One or more report pages (a single Tableau dashboard can justify splitting into an overview + drill-through page if it's dense — that's a `powerbi-report-planning` call, not yours to make ad hoc) |
| `dashboards[].zones` (recursive, percentage-based) | `powerbi-report-design`'s grid `layout_contract` regions/placements — translate the zone tree's relative x/y/w/h into grid regions, preserving nesting and `direction` (horizontal/vertical flow). **Treat `layout_contract` as a hard gate, not a loose aid**: every region must be placed, `space_audit` run clean (zero overlaps), and a header/slicer band reserved *before* any visual JSON is authored. Don't start placing visuals and patch the layout afterward — that ordering is exactly how misaligned/overlapping visuals crept in before. |
| `dashboards[].zones.type == "layout-floating"` | A synthetic root the parser generates for Tableau "Floating" (freeform/absolute-position) dashboards — every `children[]` entry is independently absolute-positioned (real `x`/`y`/`w`/`h` in Tableau's 0-100000 percentage-space, not nested percentages of a parent). Treat each child as its own top-level placement in the grid; don't expect the neat nested-flow structure that a "Tiled" dashboard's `layout-flow` tree has. Floating dashboards are commonly dense (20+ sibling zones) — expect to split into multiple pages or a tighter grid, and check `powerbi-report-planning`'s page-split judgment rather than force everything onto one page. |
| `dashboards[].zones[...].type == "parameter"` with a resolved `field_id` | A parameter/field-switcher control — usually a **Field Parameter** slicer if `pbi-semantic-builder` built one for it (check its report for which parameters became Field Parameters vs. plain slicers vs. nothing). Bind the slicer to the Field Parameter table, not the raw Tableau parameter name. |
| One `worksheets[]` entry | One visual |
| `worksheets[].mark_type` | Visual type — see chart-type mapping below |
| `worksheets[].encodings` (rows/columns/color/size/label) | Visual field wells (axis/legend/values) |
| `worksheets[].measure_names_values_pivot` (non-null) | Bind each field in `pivoted_field_ids` **directly** to the visual — one field-well entry per resolved field. Never recreate Tableau's literal "Measure Names/Measure Values" pivot column; PBI has no equivalent idiom and doesn't need one. If `pivoted_field_ids` is empty, the parser couldn't resolve the underlying fields (no matching filter) — flag it rather than guessing which fields were meant. |
| `worksheets[].reference_lines` (Min/Max/Average) | **Gauge visual** (Minimum/Maximum/Target) — see note below |
| `worksheets[].filters[]` with a `note` about the parameter-equality idiom | A **slicer** on the underlying dimension, not a filter card or calculated column |
| `theme.palette_hexes` / `font_family` | A starting point for `powerbi-report-design`'s Step 1 (tone/signature) and Step 5 (theme) — not an authoritative theme to clone; feel free to improve on it |

### Visual encoding: CLI for current truth, render-verified cookbook for proven shapes, research for the rest

There are **two** decisions per visual, and they draw on different sources — keep them separate:
**(A) which** Power BI visual best represents this Tableau worksheet, and **(B) how** to encode it in
PBIR. Never infer field-well/formatting JSON from memory — that is exactly how broken-but-
`validate`-passing visuals (the Bing→Azure Maps choropleth, dead field-parameter slicers) shipped.
`validate` confirms *structure*, not *render*.

**(A) Which visual — research the mapping, don't assume it.** The chart-mapping table below is a
starting heuristic, not the final answer. For any visual whose best Power BI equivalent is non-obvious
or evolving — **maps above all**, but also combo/dual-axis, part-to-whole, KPI, and anything the source
does with a custom trick — decide the target by **researching Microsoft Learn for current best
practice** (see the research-subtask model below), cross-checked against what the installed product
actually supports (`powerbi-report-author catalog list` / `catalog describe`). Product capabilities
move (Azure Maps reference layers, small multiples, on-object formatting); a mapping that was right a
year ago may be superseded.

**(B) How to encode it — precedence, most-current/most-trustworthy first:**

1. **`powerbi-report-author` CLI = the live vocabulary and the source of truth for roles/props/enums.**
   It is a global npm binary on PATH (invoke `powerbi-report-author` by name; it is *not* under a skill
   folder) and always reflects the **installed** version, so it beats any static doc on currency.
   Establish/confirm the encoding vocabulary here **first**:
   - `catalog list` — every built-in visual type + deprecations (`map`/`filledMap`→`azureMap`,
     `qnaVisual` unsupported).
   - `catalog describe <type>` — field-well **roles** (required/optional, maxPerRole) + formatting objects.
   - `formatting list-objects` / `describe-object <type> <object>` / `describe-property` — exact
     property names, enum values, selector requirements.
   - `expr encode --kind <t> <v>` — generate a correct value encoding instead of hand-writing the
     `expr`/`Literal` wrapper.
2. **Cookbook composition — but trust it by tier, because the cookbook is a *cache*, not the authority.**
   `.github/pbi.kb/visual-cookbook.md` + `visuals/<type>.visual.json`/`.md`. The CLI gives you the
   vocabulary; the cookbook gives you a *worked composition* (the nested JSON that actually holds
   together for a real idiom — which the CLI cannot compose and `validate` cannot render-check). Trust
   it **by tier**:
   - **🟢 render-verified** (proven by an actual render / human Desktop capture) → *more* trustworthy
     than composing yourself, because it truly rendered. **Copy it and rebind fields**, then reconcile
     its property names against step 1's CLI output to catch version drift.
   - **🟡 structural-template** → this is just *cached CLI output that passed `validate`* — no more
     authoritative than calling the CLI live, and it can be stale. Use it as a shape hint, but let the
     **live CLI win on any conflict**; do not treat 🟡 as ground truth.
   - **🔴 needs-capture** → do not ship it; go to step 3.
3. **Research + human capture for anything neither covers** (the loop under "When unsure" below). When
   you capture a new working encoding, **write it back to the cookbook as a 🟢 entry** (with the MS
   Learn citation + date from your research) so the next migration reuses it. Growing/refreshing the
   cookbook is part of the job, not a side task.

### Research subtasks: keep the mapping current, per idiom (not per instance)

To keep visual choices up to date without re-researching 30 visuals on every dashboard, research **per
distinct Tableau idiom**, cache the result, and reuse it:

1. **Collect the distinct idioms** in this workbook (mark type × key encoding: e.g. "filled map / region
   choropleth", "dual-axis line+bar", "part-to-whole", "KPI with trend"). Dedupe — 30 visuals are
   usually 5-8 idioms.
2. **For each idiom without current cached guidance, spawn a focused research subtask** that answers:
   *what is the best Power BI visual for this Tableau idiom today, and how does Microsoft Learn say to
   build/configure it well?* The subtask must return a recommended visual + concrete configuration
   notes + **Microsoft Learn citation(s) with the access date**, cross-checked against
   `catalog describe`. **Maps are the priority** — Azure Maps guidance (reference layers, data-bound
   layers, bubble vs choropleth) changes and is easy to get subtly wrong.
3. **Cache it into the cookbook**: add/refresh a `## MS Learn best practice (as of <date>)` section in
   the idiom's `visuals/<type>.md` with the recommendation + citation. Downstream, every instance of
   that idiom reuses the cached decision; the dated citation makes staleness visible on the next run.
4. Only then encode, following the (B) precedence above.

This is what makes the cookbook self-refreshing against Microsoft Learn rather than a frozen snapshot.

### Chart-type mapping (Tableau `mark_type` → Power BI visual)

> Starting heuristic only — confirm the target via the research-subtask model above (especially maps),
> and the encoding via the CLI-first precedence above.

| Tableau mark | Power BI visual | Notes |
|---|---|---|
| `Bar` | Clustered/stacked bar or column chart | Check `encodings.color` for series grouping |
| `Line` | Line chart | |
| `Circle` **with** `reference_lines` present | Often a **Gauge** — but NOT always | Tableau's "fake gauge" (a point + Min/Max/Avg reference lines on a fixed axis) maps well to the native Gauge *when it's a single KPI vs a target*. If the worksheet compares **multiple** categories/regions, a gauge can't show them — keep it a multi-point dot plot/scatter. Decide by intent + grain, not the reference-line signal alone. |
| `Circle` **without** `reference_lines` | Scatter chart | |
| `Area` | Area chart | |
| `Text` | Table, matrix, or card — infer from shelf shape: single measure + no rows/columns → card; multiple dimensions on rows → table/matrix | |
| `Map` | **Always `azureMap`** (`map`/`filledMap` are deprecated Bing), but **research the layer type on MS Learn** — region-shaded-by-measure → data-bound reference-layer choropleth; points → bubble layer; routes → line layer. Map encodings are the highest-drift, highest-risk area — always confirm current guidance. | Check for geographic `semantic_role` on the bound field |
| `Automatic` | Infer from shelf shape (same heuristics as Tableau itself: discrete+discrete → bar-ish, continuous+continuous → scatter/line) | Flag low-confidence inferences for design review rather than guessing silently |

### When unsure about a visual: research first, then put a human in the loop

Some Tableau visuals map to Power BI features whose **PBIR authoring encoding is undocumented or
uncertain** (Azure Maps reference-layer choropleths, custom visuals, novel conditional-formatting
shapes). Do NOT guess-and-iterate blindly against Desktop — it is slow and `validate` will not catch a
wrong encoding. Instead:
1. **Research what's actually possible first.** Check the official docs (Microsoft Learn) and the
   `powerbi-report-author` CLI (`catalog describe <type>`, `formatting describe-object <type> <obj>`,
   `formatting search`) to confirm the visual supports the capability and to enumerate the real
   role/object/property names.
2. **If the capability exists but the exact PBIR JSON is uncertain, surface it to the human with
   click-by-click Desktop instructions** (via `ask_user`): name the visual to add, the fields to drop
   in each well, and the Format-pane toggles to set, then have them save. **Read the resulting
   `visual.json` and reuse it as ground truth** — one human round-trip beats many blind render cycles.
   (Exactly how the Superstore Azure Maps choropleth encoding below was captured; a research subagent
   found zero public PBIR examples of it.)
3. Only then generalize the captured encoding: **save it into the cookbook**
   (`.github/pbi.kb/visuals/<type>.visual.json` + a `<type>.md` note marking it 🟢 render-verified),
   and if it applies to many visuals at once, also capture it as a small re-runnable transform script.
   The next migration then copies it from the cookbook instead of repeating the human round-trip.

## Workflow

1. Confirm the semantic model from `pbi-semantic-builder` is deployed and reachable.
2. For each `dashboards[]` entry, build a requirements brief (audience, purpose, the worksheet
   inventory with mark types) and run it through `powerbi-report-planning`'s approval gate.
3. For each planned page, hand `powerbi-report-design` the relevant `worksheets[]` entries (mark type,
   encodings, reference lines) and the zone layout — let it produce a `Design Brief:` per the chart
   mapping table above. Don't override its archetype/chart-selection judgment except where this file's
   mapping table gives a hard signal (e.g. reference-lines → Gauge).
4. **Build an empty layout skeleton before authoring any real visual.** Place a blank/placeholder
   shape (a rectangle, or the target visual type with no field wells bound yet) for *every* zone in
   the `layout_contract` at its correct region — position and size only. Screenshot or render this
   skeleton and compare its gestalt (proportions, density, header/footer/slicer bands, where the eye
   lands first) against the whole-dashboard Tableau reference screenshot **before** binding a single
   field. This is cheap to redo if wrong; a fully-populated page is not. It directly targets a lesson
   from iteration 1: polishing each visual in isolation can all look individually reasonable while the
   page as a whole reads completely differently from the source — catch that at the skeleton stage,
   not after every visual is already built and formatted. Only proceed to step 5 once the skeleton's
   gestalt is a good match.
5. Hand the design brief to `powerbi-report-authoring` to build the actual PBIR visuals bound to the
   semantic model (fields, formatting, theme) inside the already-placed, already-verified skeleton
   from step 4 — this step is about populating positions that were already confirmed correct, not
   about placement.
6. Wire up the parameter-equality-idiom simplification: add a slicer (single-select) on the dimension
   named in the filter's `note`, instead of any filter card.
7. Validate visually (Desktop screenshot per `powerbi-report-authoring`'s own validation step) against
   the original Tableau layout — not pixel-for-pixel, but check that every worksheet has a home on a
   page and nothing critical was dropped. **Run structural validation before the Desktop screenshot
   review, not instead of it** — see "Mandatory validation" below.
8. Report back to the orchestrator: report location, page/visual counts, chart-type mapping decisions
   (especially any low-confidence `Automatic` inferences), and any new `limitations_encountered`
   entries (`stage: "report_build"`), e.g. Tableau dashboard actions or customized tooltips this parser
   version doesn't yet translate (see `docs/tableau-dax-translation-guide.md` known gaps).

## Mandatory validation (before Desktop screenshot review)

Structural validation is not optional and not just "nice if the tooling supports it" — run it before
every screenshot-based design review, on both the initial build and every later fix pass:

1. **Check which `powerbi-report-authoring` skill version is active.** There have been two installed
   copies of this skill in this environment at different capability levels — an older one with no
   automated validation CLI, and a newer one (`fabric-collection\powerbi-authoring`) that ships a
   `powerbi-report-author validate` CLI (structural/schema/cross-reference/role-binding checks) and a
   `powerbi-desktop` CLI (`status`/`reload`/`screenshot` over the Desktop Bridge). Run `check-updates`
   once per session as instructed by the skill, and prefer the newer CLI-driven flow if available —
   it mechanically catches classes of bugs (e.g. broken field projections, `tableEx`-vs-`pivotTable`
   misuse) that this session instead found the hard way, one manual screenshot at a time.
2. **If only the older skill copy is active**, do the equivalent checks manually before every
   screenshot review: every `visual.json` field reference resolves against the real TMDL; every page
   is listed in `pages/pages.json`; no two visuals overlap; `definition.pbir`'s model reference is
   correct; and every table/matrix `Values` well matches the shape called out in the Gotchas below
   (no suspicious single-active-field-with-inactive-siblings pattern).
3. **Only after structural validation passes**, do the visual/numeric Desktop screenshot review.
4. **Don't treat a clean Bridge/MCP response as proof the report renders error-free.** As of this
   skill generation, errors that occur *inside* Power BI Desktop's own rendering/evaluation (a visual
   showing an error glyph, a card failing to evaluate, a refresh failure banner) are not reliably
   surfaced as structured data back through the Desktop Bridge — a `status`/`reload` call can return
   cleanly while Desktop is still showing a visible error state. The product team is actively working
   on surfacing these in-app errors programmatically (per Microsoft's own Desktop Bridge roadmap
   commentary); until that lands, a successful API/CLI response is **not** sufficient — always
   cross-check with an actual screenshot for error glyphs/banners, don't skip that step just because
   the mechanical call succeeded.

## Iterating on an existing report — still go through the skill chain

A large share of this session's actual bug-fixing (5+ checkpoints) happened as direct, ad hoc PBIR
file edits and MCP calls made outside of `pbi-report-builder`/`powerbi-report-authoring`, not as a
proper re-invocation of this subagent. That was the single biggest process gap this session — it
meant none of the skill's own validation steps, anti-pattern checks, or design-consistency guardrails
were applied to any of the fixes. **When fixing a bug in an already-built report, re-invoke this
subagent (or at minimum re-follow its "Task: Edit an existing report" workflow) instead of making a
one-off direct edit** — even for something that looks like a trivial one-line fix. The skill's own
pre-development discovery step and post-development validation checklist exist specifically to catch
the side effects a quick direct edit tends to miss.

## Definition of Done

Don't report the report as complete until all of the following hold — "it opens in Desktop without
crashing" is necessary but not sufficient:

1. **Structural validation passed** (see "Mandatory validation" above), not just a visual glance.
2. **`layout_contract` is fully specified and `space_audit`-clean** — no overlapping regions, no
   visual placed outside its page bounds.
3. **Every slicer that drives the report's default view has an explicit default value set** — no
   visual should render an all-rows aggregate on first load (see Gotcha below).
4. **Every table/matrix visual's field projection has been checked against the real Tableau
   worksheet**, not accepted on a plausible-looking guess — especially any single-active-field
   pattern (see Gotcha below).
5. **Every percentage/scaled numeric field's `formatString` has been checked against a real sample
   value via DAX**, not assumed from the field's semantic name alone.
6. **Every `measure_names_values_pivot` and every `UNRESOLVED:` reference surfaced in
   `limitations_encountered` has been explicitly addressed or explicitly flagged** — none silently
   dropped.
7. **This checklist applies to fix/iteration passes too, not just the initial build** — a one-line fix
   still needs the relevant subset of this list re-checked (at minimum #3–#5 for the visual touched)
   before you report it done.

## Gotchas

- **Nested shelf grouping** — Tableau's `(a / b)` shelf notation (seen in the EEA sample: period
  nested with land-use type on the columns shelf) is a layout/hierarchy nesting, not a calculation.
  Translate to a multi-field axis or a legend + axis combination, matching the nesting order.
- **Customized tooltips** (`worksheets[].customized_tooltip_text`) — Tableau's tooltip text often
  embeds dynamic field references. Recreate as a Power BI tooltip page or the visual's default tooltip
  fields, whichever preserves the intent with less custom-build effort; note if fidelity is reduced.
- **Manual sort orders** (`worksheets[].manual_sort`) — implement via a "Sort by column" helper column
  in the semantic model (coordinate with `pbi-semantic-builder` if one doesn't exist yet) rather than
  a one-off visual-level sort that won't survive a refresh.
- **Don't silently drop unresolved shelf references** (`UNRESOLVED:...` field ids surfaced in
  `limitations_encountered`) — surface them as "this visual may be missing a field" rather than
  building an incomplete visual without comment.
- **`measure_names_values_pivot`** (parser field, see `docs/migration-spec.md`) — Tableau's "Measure
  Names/Measure Values" virtual pivot has no direct PBI equivalent and shouldn't be recreated
  literally. Bind each field in `pivoted_field_ids` directly to the visual instead. If it's empty, the
  parser couldn't resolve the underlying fields — flag it, don't guess.
- **Never infer a parameter/field's purpose from its raw internal name** — always use the resolved
  `field_id`/`caption`. Tableau's internal names go permanently stale after a Ctrl-drag duplication
  (e.g. a zone's `param` reference resolving to a field internally named `[Y-Axis (copy 2)]` whose
  real caption is "Map KPI" — nothing to do with any Y-axis control). This applies to parameter-control
  zones (`type: "parameter"`) just as much as to semantic-model fields: if a zone's `field_id` is
  `null`, that's the parser telling you the parameter reference didn't resolve — flag it, don't guess
  from the XML name text.
- **Crosstab/pivot visuals are a recurring fragility class — two distinct failure modes seen so far:**
  1. Using `tableEx` for a dimension+measure grid can render column headers with **zero data rows**,
     even though the underlying DAX is correct. Prefer `pivotTable` for any dimension-in-rows +
     measure-in-values grid.
  2. A matrix that pivots a dimension into **Columns** (cross-tab) and reads a single shared,
     mixed-type text column via one measure can have `SUMMARIZECOLUMNS` **silently drop specific
     (row, column) combinations** from the result — even when the underlying data is 100% clean
     (verified via direct `CALCULATETABLE`/`COUNTROWS`), and regardless of grouping column, measure
     formula, or relationship cross-filter direction. Root cause not fully understood at the DAX
     engine internals level. **The robust fix: avoid the Columns-pivot pattern entirely.** Use one
     measure per branch (each with its own internal `CALCULATE(..., dimension = "X")` filter) and
     project them as separate flat `Values` entries (no `Columns` bucket) — this is usually also a
     *more faithful* translation of the Tableau original, which is typically a flat table under the
     hood, not a true cross-tab.
  **Whenever you're about to build a visual that pivots a dimension into columns, consider the
  flat-table alternative first** — it's both safer and usually more faithful to the source.
- **A `Text`/table-style worksheet's exact field projection must be checked against the real Tableau
  worksheet, not inferred from a plausible guess.** This bug class recurred and needed correcting more
  than once on the *same* visual in this session (a table was first expanded from a broken
  single-active-column to a plausible-looking 3-column projection, then later found to still have one
  redundant/wrong field and had to be trimmed to 2 correct columns). **Red flag to check for:** a
  table/matrix `Values` well with exactly one active field and other candidate fields
  present-but-inactive — that exact pattern showed up as a broken/incomplete migration artifact twice
  in this workbook. When you see it, verify against the actual Tableau worksheet's rendered columns
  before accepting it as correct.
- **Set a sensible default value on every filter-driving slicer before calling the report done.** A
  slicer left with no default selection makes every bound visual render an aggregate-across-all-rows
  value on first load (in this workbook: an aggregate across 906 cities) — which reads as "broken"
  even though the DAX/binding is correct. Pick a default that matches the reference Tableau
  screenshot/state, and confirm visually.
- **Check `formatString` against the field's actual numeric scale, not just its semantic meaning.** A
  Tableau field can already be stored pre-scaled (e.g. `12.83` meaning "12.83%", not the fraction
  `0.1283`). Applying Power BI's standard `0.00%` format (which multiplies by 100 for display) on an
  already-scaled value produces a **100x inflated** display (`1283%` instead of `12.83%`). Check a
  sample raw value via DAX before choosing between `0.00%` (true 0–1 fraction) and `0.00"%"` (literal
  suffix on an already-scaled number).
- **PBIR files and an open Desktop session can race.** Desktop autosaves periodically; a direct file
  edit to `definition/` while Desktop has the report open can be silently clobbered by the next
  autosave, or vice versa. Prefer closing/reloading Desktop around direct PBIR edits, or use the
  Desktop Bridge's `reload` command if that skill version is available (see "Mandatory validation"
  above).

### Iteration-3 hard-won gotchas (Telecom, Sales Commission, Shipping, Tale-of-100, Airline, Superstore)

**Validation-invisible rendering bugs — these pass `powerbi-report-author validate` but render wrong;
only a live Desktop screenshot catches them, so treat structural validation as necessary-not-sufficient:**
- **Conditional/Cases `Else` is IGNORED by Desktop for table `fontColor`** — the top/else band renders
  **black**. Fix: append an explicit always-true final `Case` (e.g. `driver < 1e12` → the else color)
  instead of relying on `Else`.
- **azureMap `Location` role + explicit Lat/Long = a Desktop error** ("Remove Location… or set aggregate
  to Average"). Fix: keep `Location`, set Lat/Long to **Average** aggregation (lossless when the grain
  is one coordinate per point).
- **`field.Aggregation.Function` enum: Sum=0, Avg=1, Max=2, Min=3, Count=4.** A wrong value is not a
  field reference, so it passes validation but **silently aggregates wrong**.
- **Projection-level `format` overrides** (`proj.format = "0.00%"`) and **`expansionStates`** both pass
  validation but their Desktop honoring is unconfirmed offline — `expansionStates` in particular is a
  **no-op on initial render** (matrix still shows collapsed); don't burn cycles chasing it, document a
  collapsed default or use a flat `tableEx` when the grain is one row per leaf.

**Data colors / conditional formatting (see `conditional-formatting.md`):**
- **Discrete/banded data colors use `dataPoint.fill.solid.color.expr.Conditional.Cases[]`, NOT
  `fillRule.cases`** (`fillRule` is gradient/`linearGradient` only). Each case = a `Comparison`
  (`Left = SelectRef.ExpressionName` of a projection's `queryRef`, cascading first-match) plus a
  top-level `Else`, with `selector.data = [{dataViewWildcard:{matchingOption:0}}]`.
- **Scatter/chart per-point color must reference a PROJECTED field.** A `dataPoint.fill` expr over an
  *unprojected* measure (e.g. a text KPI measure in no field well) silently falls back to one solid
  color — carry the numeric driver on an axis or in Tooltips so it's in the visual query.
- **`scopeId` per-point coloring is the confirmed-good mode for many series** (verified with 81
  per-company line `dataPoint.fill` entries — all render, no disappearing-line issue).
- **String-valued "color helper" measures (Tableau `… Circle Col` returning glyph/indicator strings)
  cannot drive PBIR data-color rules** → static colors; a recurring color-encoding fidelity loss.

**PBIR mechanics facts:**
- **Visual-level filter = a top-level `filterConfig` key in `visual.json` (sibling to `visual`, NOT
  nested under it)**, `type:"Categorical"`, `Version:2` `In`-condition. Nesting it under `visual` is
  silently ignored.
- **Stacked bar = `barChart` visualType (not `clusteredBarChart`); the first Y projection stacks from
  0.** Per-series colors via `dataPoint[]` with `selector.metadata = <queryRef>` (queryRef, not
  nativeQueryRef).
- **`displayName` on a projection is the header-rename mechanism** — Desktop auto-labels non-default
  aggregations "Average of X"; `nativeQueryRef` does not control the header.
- **Reference-line `value` needs a type-suffixed numeric literal** (`{Literal:{Value:"100D"}}`); a bare
  `"100"` parses to 0 and pins the line to the axis baseline with no validation error.
- **azureMap draws true 2-point route/line maps via `PathID` + `PointOrder` + `pathLayer`** (a fidelity
  win over Tableau's dual-axis workaround) — but this needs **one data row per endpoint**. If the fact
  stores origin+destination lat/long as columns on a single row, the arc can't render — that reshape
  is a **semantic-model decision** (coordinate with `pbi-semantic-builder` up front for any Tableau
  `MAKELINE`/`MAKEPOINT` route map; otherwise fall back to endpoint bubbles + a documented note).
- **Theme: custom `visualStyles` are strictly validated per-visual-object and `fillPoint` is not valid
  for scatterChart/filledMap** — keep custom themes minimal (set visual-specific formatting in each
  `visual.json`); the theme file's internal `name` must exactly equal the `report.json` `customTheme`
  reference including `.json`. Single-line caption/legend textboxes need ≥3.4 grid rows or they trip
  `PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR`.
- **What-if % slicer format: `0.0"%"` (quoted) when the stored value is pre-scaled (e.g. 22.8); `0.0%`
  (unquoted) only when it's a true 0–1 fraction** — mixing them mis-scales the display by 100×.

### Iteration-4 hard-won gotchas (Superstore — Azure Maps + Field Parameters)

**Azure Maps is the ONLY non-deprecated map** (`map`/`filledMap` are legacy Bing → a
`PBIR_VISUAL_TYPE_DEPRECATED` warning **plus** a once-per-session Desktop "Bing maps are going away"
nag modal that the bridge screenshot does NOT surface). All recipes below are Desktop-verified on the
Superstore build.

- **Measure-driven choropleth (shade regions/states by a measure) — the sanctioned azureMap pattern,
  ground-truth PBIR encoding:**
  - `query.queryState.Category` = the location **key column** (e.g. `State`) as a `Column` projection.
    That alone data-binds the reference layer (Azure Maps matches the key to a property in the boundary
    file). The colouring measure does **not** go in a data well.
  - `objects.referenceLayer` is a **2-entry array**:
    - `[0]` (no selector): `datasourceType` = `'url'`, `referenceLayerUrl` = a hosted boundary GeoJSON
      URL — fully declarative, no file upload, nothing in `RegisteredResources`. US states:
      `https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json`
      (its `name` property = full state name). Add `unmappedObjectVisibility: false` to hide states
      filtered out of the data.
    - `[1]` (selector `{data:[{dataViewWildcard:{matchingOption:1}}]}`): `polygonFillColor` = a
      `FillRule`/`linearGradient3` bound to the measure — the **exact same FillRule shape as
      `dataPoint.fill`** (`{Input:{Measure:…}, FillRule:{linearGradient3:{min, mid{value:0D}, max,
      nullColoringStrategy 'asZero'}}}`).
  - Bring a boundary file for the geography *before* building; if none exists or the join key is
    uncertain, that is a research-then-ask moment (see "When unsure about a visual" above).
- **azureMap fixed-view small-multiples:** for a small (≤~400px) region-highlighter multiple, set
  `mapControls`: `defaultStyle 'road'`, `autoZoom` **false** (otherwise it fits to Alaska/Hawaii/PR in
  the boundary file and shrinks the lower-48 to a dot), plus a fixed `zoom` + `centerLatitude/Longitude`.
  Zoom scales with viewport (512px vector tiles): continental US fills a **384px**-wide map at
  **`zoom ≈ 2.0`** (a 700–940px map uses ≈2.9). `blank` style + `autoZoom` rendered empty/tiny — avoid.
- **scatterChart X and Y must BOTH be MEASURES, never a grouping column.** Binding `Y` (or `X`) to a
  dimension renders "Remove Values to display x- and y-axis pairs" (validation-clean, Desktop-only). A
  Tableau "dimension-on-rows dot strip" → scatter with `Category` = the dimension (Details, one dot
  each), `X` = value measure, `Y` = a **constant baseline measure** (`measure 'Dot Baseline' = 0`,
  hidden), `Size` = value measure, colour via a `FillRule` gradient on a signed diff measure; hide the
  constant `valueAxis` (`show:false` + `showAxisTitle:false`). (Superstore's 3 region-comparison
  dot-strips.)
- **A measure used as a visual-level filter at a FINER grain than it evaluates silently zeroes the
  visual.** Superstore's scatter carried a `Region Filter` measure filter, but at Sub-Category grain
  `SELECTEDVALUE('…'[Region])` is blank so the filter is false for every point → empty visual. When the
  underlying measures already bake in the restriction (they did — DAX guide §4), just drop the
  redundant visual filter.
- **Slicers/maps showing "Column … cannot be found or may not be used"** almost always mean the
  field-parameter table's columns didn't materialize — a semantic-model bug (`sourceColumn` needs
  brackets `[Value1]`); see `pbi-semantic-builder.agent.md` Gotchas. Suspect this first for FP-bound
  visuals.

**Desktop verification mechanics (when a live check is possible):**
- **The `powerbi-desktop` bridge has NO refresh command** (only `application.state.get` /
  `report.snapshot.capture` / `file.reload`), and PBIP stores no data cache, so a freshly-opened import
  report renders **empty** ("tables have incomplete or no data"). A clean screenshot with empty visuals
  is an unrefreshed-model artifact, not a binding defect. **Workaround (proven):** refresh via TOM/XMLA
  against the child `msmdsrv` port — load `Microsoft.AnalysisServices.AdomdClient` (copy the DLL out of
  WindowsApps first; direct load = Access Denied), find the port via `Get-NetTCPConnection`, resolve the
  catalog GUID via `$SYSTEM.DBSCHEMA_CATALOGS`, and `ExecuteNonQuery` a TMSL
  `{"refresh":{"type":"full","objects":[{"database":"<guid>"}]}}`. **Refresh report-bound tables only**
  (a full refresh can hang 6+ min on a large orphaned table); never kill `SaveChanges` mid-flight; the
  refreshed data **survives `reload`**, so the steady-state loop is regenerate→validate→reload→screenshot.
- **External XMLA refresh does NOT clear Desktop's "calculated columns need refresh" banner** (UI
  dirty-flag only; data underneath is correct).
- **Store/MSIX Desktop** needs `$env:PBI_DESKTOP_PATH` set to the WindowsApps `PBIDesktop.exe` on each
  fresh PowerShell process; `reload` can deadlock (`BRIDGE_ERROR "Another operation is already in
  progress"`, `-32511`) while idle — recover by killing **your own** Desktop PID and relaunching.
- **In a parallel batch the single Desktop bridge is a hard serialization point** — only one build can
  hold it; do NOT force-open into an instance owned by another build with unsaved changes, and never
  screen-scrape as a substitute (focus-steal + privacy risk). Base sign-off on structural validation +
  an independent field-reference cross-check against the model TMDL when contended. `PBIR_SCHEMA_UNREACHABLE`
  offline is benign but means JSON-schema validation was skipped — back it with the field cross-check.
