---
name: powerbi-report-gotchas
description: Hard-won Power BI PBIR authoring and Desktop-verification gotchas - validation-invisible rendering bugs, conditional-formatting and data-colour encodings, azureMap/scatter/matrix recipes, field-parameter failures, and the Desktop refresh/bridge mechanics. Use before authoring or debugging any PBIR visual, and whenever a report validates clean but renders wrong. Source-tool agnostic (Tableau, Qlik, Cognos to Power BI).
---

# Power BI report gotchas

Every entry below cost a real debugging cycle on a real migration. They are grouped by *when they bite
you*, not by visual type.

**The one rule that generates most of this file:** `powerbi-report-author validate` is **necessary, not
sufficient**. It passes structurally-valid-but-wrong encodings, and it **silently skips all JSON-schema
checks** when it cannot fetch the visualContainer schema (`PBIR_SCHEMA_UNREACHABLE` — it still prints
"0 errors"). Only a live Desktop render catches the class of bug in §1.

**Treat `PBIR_SCHEMA_UNREACHABLE` as "schema validation did NOT run."** The declared `2.11.0` schema
404s; `2.9.0` is the newest published. Confirm instead with a Desktop open-test (a schema violation
raises an error dialog on open) or an offline `ajv` harness against the real 2.9.0-family schemas.

> **Scope note:** the mechanisms here are pure Power BI, so this folder ports to a Qlik or Cognos
> migration unchanged. A handful of entries name the source-tool idiom that led to the discovery
> (Tableau shelves, `MAKELINE`); read those as examples, not as prerequisites. Cross-references to
> `.github/pbi.kb/` and `.github/agents/` are paths in the host migration repo.

## 1. Validation-invisible rendering bugs

These pass `validate` but render wrong. Only a live Desktop screenshot catches them.

- **Conditional/Cases `Else` is IGNORED by Desktop for table `fontColor`** — the top/else band renders
  **black**. Fix: append an explicit always-true final `Case` (e.g. `driver < 1e12` → the else colour)
  instead of relying on `Else`.
- **azureMap `Location` role + explicit Lat/Long = a Desktop error** ("Remove Location… or set aggregate
  to Average"). Fix: keep `Location`, set Lat/Long to **Average** aggregation (lossless when the grain
  is one coordinate per point).
- **`field.Aggregation.Function` enum: Sum=0, Avg=1, Count=2, Min=3, Max=4.** 🟢 render-verified — a
  5-projection `cardVisual` over a 730-row column (values 75.75–92.78) returned `0 → 61.82K` (sum),
  `1 → 84.69` (avg), `2 → 533`, `3 → 75.75` (min), `4 → 92.78` (max). Note `2` is a **DISTINCT** count
  (533 distinct weights out of 730 rows), not the row count — use `CountNonNull` if you want 730.
  A wrong value is not a field reference, so it passes validation but **silently aggregates wrong**.
  *(This corrects an earlier version of this file which claimed Max=2, Min=3, Count=4; the
  `powerbi-report-authoring` references had it right.)*
- **Projection-level `format` overrides** (`proj.format = "0.00%"`) and **`expansionStates`** both pass
  validation but their Desktop honouring is unconfirmed offline — `expansionStates` in particular is a
  **no-op on initial render** (matrix still shows collapsed); don't burn cycles chasing it, document a
  collapsed default or use a flat `tableEx` when the grain is one row per leaf.
- **A measure used as a visual-level filter at a FINER grain than it evaluates silently zeroes the
  visual.** A scatter carrying a `Region Filter` measure at Sub-Category grain has
  `SELECTEDVALUE('…'[Region])` blank, so the filter is false for every point → empty visual. When the
  underlying measures already bake in the restriction, drop the redundant visual filter.
- **Slicers/maps showing "Column … cannot be found or may not be used"** almost always mean a
  field-parameter table's columns didn't materialize — a **semantic-model** bug (`sourceColumn` needs
  brackets, `[Value1]`). Suspect this first for FP-bound visuals; it is not a report-layer fix.
- **`slicer` + `data.mode = 'Single'` on a NUMERIC column silently ignores its
  `objects.general.filter` default.** 🟢 render-verified. Desktop draws a bare text input showing the
  column's **minimum**, not the pre-selected value — so two what-if slicers intended to load at `10`
  and `25.0` loaded at `1` and `0.0` while `validate` reported 0 errors. This breaks the
  "every slicer has a default" rule (§8) *without any diagnostic*. Fix: use `mode = 'Dropdown'`; the
  **identical** `general.filter` payload then renders the right value, proving the filter encoding was
  never the problem. Treat `'Single'` as unsafe for what-if/numeric parameter controls.
- **A textbox that mixes a large title run and a small descriptor run in ONE paragraph wraps and
  clips.** 🟢 render-verified. Desktop wraps the second run onto a new line, cuts it off at the box
  bottom, and draws a stray vertical overflow mark at the right edge — `validate` says 0 errors. Fix:
  emit **two paragraphs** (heading run, then descriptor run) and size the box for both lines; a
  single-run wrap is not deterministic against the box width.

## 2. Data colours and conditional formatting

See the `powerbi-report-authoring` skill → `references/conditional-formatting.md`; for table-specific
idioms see `.github/pbi.kb/visuals/table-cond-format.md`.

- **Discrete/banded data colours use `dataPoint.fill.solid.color.expr.Conditional.Cases[]`, NOT
  `fillRule.cases`** (`fillRule` is gradient/`linearGradient` only). Each case = a `Comparison`
  (`Left = SelectRef.ExpressionName` of a projection's `queryRef`, cascading first-match) plus a
  top-level `Else`, with `selector.data = [{dataViewWildcard:{matchingOption:0}}]`.
- **Scatter/chart per-point colour must reference a PROJECTED field.** A `dataPoint.fill` expr over an
  *unprojected* measure (e.g. a text KPI measure in no field well) silently falls back to one solid
  colour — carry the numeric driver on an axis or in Tooltips so it is in the visual query.
- **`scopeId` per-point colouring is the confirmed-good mode for many series** (verified with 81
  per-company line `dataPoint.fill` entries — all render, no disappearing-line issue).
- **String-valued "colour helper" measures** (a source field returning a glyph/indicator string)
  **cannot drive PBIR data-colour rules** → static colours; a recurring colour-encoding fidelity loss.
  Record it rather than faking it.

## 3. PBIR mechanics

- **Visual-level filter = a top-level `filterConfig` key in `visual.json` (sibling to `visual`, NOT
  nested under it)**, `type:"Categorical"`, `Version:2` `In`-condition. Nesting it under `visual` is
  silently ignored.
- **Stacked bar = `barChart` visualType (not `clusteredBarChart`); the first Y projection stacks from
  0.** Per-series colours via `dataPoint[]` with `selector.metadata = <queryRef>` (queryRef, not
  nativeQueryRef).
- **`displayName` on a projection is the header-rename mechanism** — Desktop auto-labels non-default
  aggregations "Average of X"; `nativeQueryRef` does not control the header.
- **Reference-line `value` needs a type-suffixed numeric literal** (`{Literal:{Value:"100D"}}`); a bare
  `"100"` parses to 0 and pins the line to the axis baseline with **no validation error**.
- **Theme: custom `visualStyles` are strictly validated per-visual-object and `fillPoint` is not valid
  for scatterChart/filledMap** — keep custom themes minimal (set visual-specific formatting in each
  `visual.json`); the theme file's internal `name` must exactly equal the `report.json` `customTheme`
  reference **including `.json`**. Single-line caption/legend textboxes need ≥3.4 grid rows or they trip
  `PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR`.
- **What-if % slicer format: `0.0"%"` (quoted) when the stored value is pre-scaled (e.g. 22.8); `0.0%`
  (unquoted) only when it is a true 0–1 fraction** — mixing them mis-scales the display by 100×.
- **Check `formatString` against the field's actual numeric scale, not its semantic meaning.** A source
  field can already be stored pre-scaled (`12.83` meaning "12.83%", not `0.1283`). Power BI's `0.00%`
  multiplies by 100 for display, so an already-scaled value renders **100× inflated** (`1283%`). Sample
  a raw value via DAX before choosing `0.00%` (true fraction) vs `0.00"%"` (literal suffix).

## 4. Crosstabs and tables — a recurring fragility class

Two distinct failure modes, both seen in production:

1. **`tableEx` for a dimension+measure grid can render column headers with zero data rows**, even
   though the underlying DAX is correct. Prefer `pivotTable` for any dimension-in-rows +
   measure-in-values grid.
2. **A matrix that pivots a dimension into `Columns`** and reads a single shared, mixed-type text
   column via one measure can have `SUMMARIZECOLUMNS` **silently drop specific (row, column)
   combinations** — even with 100% clean data (verified via direct `CALCULATETABLE`/`COUNTROWS`), and
   regardless of grouping column, measure formula, or relationship cross-filter direction. Root cause
   not understood at the DAX-engine level. **Robust fix: avoid the Columns-pivot pattern entirely.**
   Use one measure per branch (each with its own internal `CALCULATE(…, dimension = "X")`) projected as
   separate flat `Values` entries (no `Columns` bucket).

**Whenever you are about to pivot a dimension into columns, consider the flat-table alternative
first** — it is safer, and usually a *more faithful* translation, because the source worksheet is
typically a flat table under the hood rather than a true cross-tab.

- **A table-style worksheet's exact field projection must be checked against the real source
  worksheet, never inferred from a plausible guess.** This bug class recurred on the *same* visual
  twice in one session. **Red flag:** a table/matrix `Values` well with exactly one active field and
  other candidate fields present-but-inactive — that exact pattern showed up as a broken migration
  artifact twice. Verify against the source worksheet's rendered columns before accepting it.

## 5. Maps — Azure Maps is the only non-deprecated option

`map`/`filledMap` are legacy Bing → a `PBIR_VISUAL_TYPE_DEPRECATED` warning **plus** a once-per-session
Desktop "Bing maps are going away" nag modal that the bridge screenshot does **not** surface.

**Measure-driven choropleth — the sanctioned azureMap pattern, ground-truth encoding:**

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
  uncertain, that is a research-then-ask moment, not a guess.

**Fixed-view small multiples:** for a small (≤~400px) region-highlighter multiple, set `mapControls`:
`defaultStyle 'road'`, `autoZoom` **false** (otherwise it fits to Alaska/Hawaii/PR in the boundary file
and shrinks the lower-48 to a dot), plus a fixed `zoom` + `centerLatitude/Longitude`. Zoom scales with
viewport (512px vector tiles): continental US fills a **384px**-wide map at **`zoom ≈ 2.0`** (a
700–940px map uses ≈2.9). `blank` style + `autoZoom` rendered empty/tiny — avoid.

**Route/line maps:** azureMap draws true 2-point routes via `PathID` + `PointOrder` + `pathLayer` (a
fidelity win over the dual-axis workaround) — but it needs **one data row per endpoint**. If the fact
stores origin+destination lat/long as columns on a single row, the arc cannot render, and that reshape
is a **semantic-model decision**: coordinate with the model owner up front for any `MAKELINE`/
`MAKEPOINT`-style route map, or fall back to endpoint bubbles with a documented note.

## 6. Scatter

**`scatterChart` X and Y must BOTH be MEASURES, never a grouping column.** Binding `Y` (or `X`) to a
dimension renders "Remove Values to display x- and y-axis pairs" — validation-clean, Desktop-only.

A "dimension-on-rows dot strip" → scatter with `Category` = the dimension (Details, one dot each),
`X` = value measure, `Y` = a **constant baseline measure** (`measure 'Dot Baseline' = 0`, hidden),
`Size` = value measure, colour via a `FillRule` gradient on a signed diff measure; hide the constant
`valueAxis` (`show:false` + `showAxisTitle:false`).

## 7. Desktop verification mechanics

- **The `powerbi-desktop` bridge has NO refresh verb.** Verbs as of bridge CLI 0.1.2: `status`,
  `manifest`, `open`, `reload`, `screenshot`, `screenshot-all` (underlying methods
  `application.state.get` / `report.snapshot.capture` / `file.reload`). PBIP stores no data cache, so a
  freshly-opened import report renders **empty** ("tables have incomplete or no data"). **A clean
  screenshot with empty visuals is an unrefreshed-model artifact, not a binding defect.**
- **First check whether you even need to refresh.** A model handed over already refreshed AND saved
  persists to `<Name>.SemanticModel/.pbi/cache.abf` and survives reopening. Run
  `python scripts/refresh_pbip_model.py --pid <pid> --verify-only` — `DATA_OK` means you are done. On
  `NO_DATA`, run the same script **without** `--verify-only` rather than hand-rolling the XMLA dance;
  it also saves, so the next open keeps the data. The cache is discarded whenever `definition/*.tmdl`
  becomes newer than it, so **any model edit means re-running it**. Full mechanism and the pid-binding
  rule: the `pbip-model-refresh` skill.
- **Manual XMLA fallback (proven).** Refresh via TOM/XMLA against the child `msmdsrv` port: load
  `Microsoft.AnalysisServices.AdomdClient` (copy the DLL out of WindowsApps first; direct load = Access
  Denied), find the port via `Get-NetTCPConnection`, resolve the catalog GUID via
  `$SYSTEM.DBSCHEMA_CATALOGS`, and `ExecuteNonQuery` a TMSL
  `{"refresh":{"type":"full","objects":[{"database":"<guid>"}]}}`. **Refresh report-bound tables only**
  — a full refresh can hang 6+ min on a large orphaned table. Never kill `SaveChanges` mid-flight. The
  refreshed data **survives `reload`**, so the steady-state loop is
  regenerate → validate → reload → screenshot.
- **External XMLA refresh does NOT clear Desktop's "calculated columns need refresh" banner** — a UI
  dirty-flag only; the data underneath is correct.
- **Store/MSIX Desktop** needs `$env:PBI_DESKTOP_PATH` set to the WindowsApps `PBIDesktop.exe` on each
  fresh PowerShell process. `reload` can deadlock (`BRIDGE_ERROR "Another operation is already in
  progress"`, `-32511`) while idle — recover by killing **your own** Desktop PID and relaunching.
- **PBIR files and an open Desktop session can race.** Desktop autosaves periodically, so a direct file
  edit to `definition/` while Desktop has the report open can be silently clobbered by the next
  autosave, or vice versa. Close/reload Desktop around direct PBIR edits, or use the bridge's `reload`.
- **In a parallel batch the single Desktop bridge is a hard serialization point** — only one build can
  hold it. Do **not** force-open into an instance owned by another build with unsaved changes, and
  never screen-scrape as a substitute (focus-steal + privacy risk). When contended, base sign-off on
  structural validation **plus** an independent field-reference cross-check against the model TMDL.
  `PBIR_SCHEMA_UNREACHABLE` offline is benign but means JSON-schema validation was skipped — back it
  with the field cross-check.

## 8. Reading the source spec

These are about translating a migration spec faithfully; they generalise to any source tool that has
shelves, tooltips and manual sorts.

- **Nested shelf grouping** — a `(a / b)` shelf notation is a layout/hierarchy nesting, **not** a
  calculation. Translate to a multi-field axis or a legend + axis combination, matching the nesting
  order.
- **Customized tooltips** often embed dynamic field references. Recreate as a Power BI tooltip page or
  the visual's default tooltip fields, whichever preserves intent with less build effort; note any
  reduced fidelity.
- **Manual sort orders** — implement via a "Sort by column" helper column in the semantic model
  (coordinate with the model owner if one does not exist) rather than a one-off visual-level sort that
  will not survive a refresh.
- **Never infer a field's purpose from its raw internal name** — always use the resolved
  `field_id`/`caption`. Internal names go permanently stale after a Ctrl-drag duplication (a zone's
  `param` resolving to a field internally named `[Y-Axis (copy 2)]` whose real caption is "Map KPI" —
  nothing to do with any Y-axis control). If a zone's `field_id` is `null`, the parser is telling you
  the reference did not resolve: **flag it, do not guess from the XML name text.**
- **A "Measure Names / Measure Values" virtual pivot has no direct Power BI equivalent** and must not
  be recreated literally. Bind each field in `pivoted_field_ids` directly to the visual. If it is
  empty, the parser could not resolve the underlying fields — flag it, don't guess.
- **Don't silently drop unresolved shelf references** (`UNRESOLVED:…` ids in
  `limitations_encountered`) — surface them as "this visual may be missing a field" rather than
  building an incomplete visual without comment.
- **Set a sensible default on every filter-driving slicer before calling the report done.** A slicer
  with no default selection makes every bound visual render an aggregate-across-all-rows value on first
  load (in one workbook: an aggregate across 906 cities) — which reads as "broken" even though the DAX
  and binding are correct. Pick a default matching the reference screenshot, and confirm visually.

## 9. Keeping the visual mapping current (research per idiom, not per instance)
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
