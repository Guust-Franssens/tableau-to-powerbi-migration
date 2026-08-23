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
"0 errors"). It checks reference **shape**, not reference **target**; a well-formed `(entity, property)`
pair, filter payload, or hierarchy can still point at the wrong thing or render at the wrong grain.
Only a live Desktop render catches the class of bug in §1.

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
- **Projection-level `format` overrides ARE honoured by Desktop.** 🟢 render-verified (Desktop
  2.157.627.0, table-calcs migration): `proj.format = "MMMM yyyy"` on a `pivotTable` Rows projection
  rendered `January 2015` (from a `Short Date` model format), and `proj.format = "#,0"` on a Values
  projection rendered `6,928` instead of `6,928.00`. This is the **report-layer** way to restyle a
  column you do not own — prefer it over asking the model owner to change `formatString`.
  *(This corrects an earlier version of this file which listed it as "unconfirmed offline".)*
  **`expansionStates`** remains unconfirmed and is a **no-op on initial render** (matrix still shows
  collapsed); don't burn cycles chasing it, document a collapsed default or use a flat `tableEx` when
  the grain is one row per leaf.
- **`labels.labelPosition = 'InsideCenter'` is silently IGNORED on a `lineChart`.** 🟢 render-verified
  (Desktop 2.157.627.0). `formatting describe-object lineChart labels` lists it in the enum and
  `validate` returns 0 errors, but the render is byte-identical to `Auto` **and** to `Above` — Desktop
  always draws a line chart's data label *above* its marker. Deceptive rather than obviously broken: on
  a bump chart (ranks 1..N in every column) an offset label still lands neatly inside *a* marker, just
  the **wrong one**, so every number reads one row off. Diagnose by rendering the labels **black** — a
  stray label with no marker under it appears at the edge of the plot. Fix with the lineChart-only
  numerics **`labels.maximumOffset` / `labels.minimumOffset`**; measured sweep at `markerSize = 14`:
  `0` ≈ half a row low, `-10` slightly high, **`-20` dead-centre**, `-30` slightly low, `-40` a full row
  below. The offset is a pixel quantity cancelling a pixel quantity, so it is independent of plot height
  (survives resize) but **coupled to `markerSize`** — re-sweep if the marker size changes.
  Vocabulary is **type-dependent**: `labelOverflow` is columnChart-only, `maximumOffset`/`minimumOffset`
  are lineChart-only, `labelDensity`/`show`/`color`/`labelPosition` exist on both — so a script that
  flips a visual's type must also swap the label property set (see §3).
- **A `lineChart` with a multi-level `Category` silently renders only the TOP level.** 🟢 render-verified
  (cold run S18): binding ordered `Date[Year]` + `Date[Month]` categories turned **48 monthly marks
  into 4 yearly points** on an axis still scaled for 48, while `validate` returned 0 errors / 0 warnings
  and the same two-level binding was correct on a `columnChart`. Fix: bind **one continuous column** at
  the desired grain (for example month start) with `objects.categoryAxis[0].properties.axisType = 'Scalar'`.
  Cross-layer trap: a semantic-model wave that "improves" a report from one date column to a Year→Month
  hierarchy is harmless on bars and silently destructive on line charts, so the report owner must
  re-render every line chart after that model-side rebinding.
- **A measure used as a visual-level filter at a FINER grain than it evaluates silently zeroes the
  visual.** A scatter carrying a `Region Filter` measure at Sub-Category grain has
  `SELECTEDVALUE('…'[Region])` blank, so the filter is false for every point → empty visual. When the
  underlying measures already bake in the restriction, drop the redundant visual filter.
- **Slicers/maps showing "Column … cannot be found or may not be used"** almost always mean a
  field-parameter table's columns didn't materialize — a **semantic-model** bug (`sourceColumn` needs
  brackets, `[Value1]`). Suspect this first for FP-bound visuals; it is not a report-layer fix.
- **`slicer` + `data.mode = 'Single'` on a NUMERIC column silently ignores its
  `objects.general[0].properties.filter` default.** 🟢 render-verified. Desktop draws a bare text input
  showing the column's **minimum**, not the pre-selected value — so two what-if slicers intended to load
  at `10` and `25.0` loaded at `1` and `0.0` while `validate` reported 0 errors. This breaks the
  "every slicer has a default" rule (§8) *without any diagnostic*. Fix: use `mode = 'Dropdown'`; the
  **identical** `general.filter` payload then renders the right value, proving the filter encoding was
  never the problem. Treat `'Single'` as unsafe for what-if/numeric parameter controls.
- **A slicer's pre-selection lives in `objects.general[0].properties.filter`, not top-level
  `filterConfig`.** 🟢 render-verified (cold run S19): a `filterConfig`-only Categorical `In [10L]`
  rendered **All**; the byte-identical payload moved to `general.filter` rendered **10**. On a slicer,
  `filterConfig` restricts which items are offered and pre-selects nothing, so engine-emitted defaults
  there are inert. The filter `name` must be unique report-wide — a duplicate is a `validate`
  **warning** (`PBIR_FILTER_NAME_DUPLICATE_GLOBAL`, verified in CLI 0.1.4 `dist/index.js`), so an
  `errorCount`-only gate misses it — and an `Int64` literal needs the `L` suffix (`10L`).
- **A textbox that mixes a large title run and a small descriptor run in ONE paragraph wraps and
  clips.** 🟢 render-verified. Desktop wraps the second run onto a new line, cuts it off at the box
  bottom, and draws a stray vertical overflow mark at the right edge — `validate` says 0 errors. Fix:
  emit **two paragraphs** (heading run, then descriptor run) and size the box for both lines; a
  single-run wrap is not deterministic against the box width.
- **`scatterChart` projecting the SAME column into both `Category` and `Series` with an identical
  `nativeQueryRef` renders "Error fetching data for this visual"** (LogisticsLive migration,
  `powerbi-report-author` CLI 0.1.4). 🟢 render-verified — a 4-category-point scatter (`Category` =
  `Discount Band`, `Series` = the same `Discount Band` column for the legend) passed `validate` with 0
  errors/warnings but Desktop showed the error glyph with no further detail. Root cause: both
  projections shared `nativeQueryRef: "Discount Band"` even though their `queryRef`s were correctly
  disambiguated (`Superstore Orders.Discount Band` vs `...Discount Band1`) — per
  `references/expressions.md` "if duplicated across roles, append a number", this applies to
  **`nativeQueryRef` too, not just `queryRef`**. Fix: give the second projection a distinct
  `nativeQueryRef` (e.g. `"Discount Band1"`); re-validate and reload — the error disappears and the
  scatter renders normally. This is a second, independent instance of a validation-invisible bug class
  (§1's core warning) — always reload+screenshot a scatter/chart that legends by the same field it
  categorizes by, even after a clean `validate`.
- **Adding a `Rows` (small-multiples) field SILENTLY DROPS a measure-driven `sortDefinition`; the
  category axis reverts to alphabetical.** 🟢 render-verified by controlled before/after
  (`book_5-2-LOD`, `powerbi-report-author` CLI 0.1.4). A `clusteredBarChart` sorted Sub-Category DESC
  by `'_Measures'[Sales across Regions]` rendered correctly (Phones, Chairs, Storage…). Moving
  `Region` from a second `Category` level into `Rows` — **changing nothing else** — re-rendered it as
  Accessories, Appliances, Art… `validate` still returned 0 errors, and the `sortDefinition` node is
  still physically present and still correct in `visual.json`. So neither a schema check nor a
  "did my sort survive?" JSON assertion can detect this; only a screenshot can.
  **It also holds when the sort targets an ON-AXIS measure — retargeting is not a fix.** 🟢
  render-verified, same report, three runs (Desktop 2.157.627.0, 2026-08-08): (1) sort → off-axis
  `_Measures[Sales across Regions]` + `Rows` → alphabetical; (2) sort → the on-axis
  `Sum(Orders[Sales])` that the bars are drawn from + `Rows` → **still alphabetical**; (3) that same
  sort node with `Rows` REMOVED → perfect DESC (Phones, Chairs, Storage…). Runs 2 vs 3 are a clean
  isolation — byte-identical `sortDefinition`, identical Category/Y/Tooltips, only the `Rows` well
  differs — so the panes are the cause, not the sort target. Worth knowing because "the sort field
  isn't on an axis" is the obvious first hypothesis and it is *wrong*: don't spend a cycle on it.
  ⚠️ **Do NOT cite Learn for this — the docs do not say it.** An earlier revision of this entry
  claimed corroboration from Learn's small-multiples *Considerations and limitations*; that list
  (re-fetched 2026-08-08) covers no-data items, scroll-to-load, Analyze/Summarize, rectangular
  select, axis zoom, concatenate labels, total labels, zoom sliders, trend lines and forecasting —
  **sorting is not on it**. The companion page *Interact with small multiples in Power BI* reads the
  other way: "you can sort multiple aspects of a visual at once. Sort by the category, and also by
  the axis in each multiple." Desktop does not do that. So this limitation is **real but
  undocumented**, which is exactly why it can only be found by rendering — cite the measurement.
  **Consequence for migrations:** when a source tool expresses *both* "this discrete pill forms
  panes" *and* "sort this axis by a measure" (a Tableau `<cols>` pane pill + `<computed-sort>` is
  exactly this pair), the **report layer** cannot carry the sort — a `sortDefinition` is ignored once
  the pane field is in the small-multiples well, whether it names an off-axis or an on-axis field
  (measured above). **This is NOT an accepted limitation — it has a proven model-side fix.** A
  model-level **Sort by Column** (`sortByColumn`) on the category, keyed to a numeric rank column,
  DOES survive small multiples: measured in `powerbi-semantic-model-gotchas` §4 ("a model-level sort
  SURVIVES small multiples where a visual-level sort does not"), it reordered all 17 categories
  correctly across all 4 panes on the byte-identical `clusteredBarChart` where the report-layer sort
  was ignored. So **route the sort to the model owner as the real fix, not a fallback** — a rank
  column plus `sortByColumn` is the mechanism that works, and it is the model's job, not the
  report's. Keeping the report-layer `sortDefinition` targeted at a projected field is still a
  harmless, render-neutral adjunct (belt-and-braces), but do not log an accepted limitation and stop:
  the sort is recoverable, and we have proven it.

- **A measure's `SourceRef.Entity` must be the measure's HOME table, not the table it aggregates.**
  🟢 verified at query level. `Sales (w/o Category)` = `CALCULATE(SUM('Orders'[Sales]),
  REMOVEFILTERS('Orders'[Category]))` reads `Orders`, but is *defined* on a `_Measures` table — so
  `{"Measure":{"Expression":{"SourceRef":{"Entity":"Orders"}},"Property":"Sales (w/o Category)"}}` is
  wrong, and `"_Measures"` is right (`queryRef` follows: `_Measures.Sales (w/o Category)`). `validate`
  returns **0 errors / 0 warnings** on the broken form (shape, not target — the header rule), and the
  failure is `'Orders'[Sales (w/o Category)]` → *"Column … in table 'Orders' cannot be found or may not
  be used"* only at query time. **A measure reference has TWO sites that must agree**: the projection
  *and* the `filterConfig` `From` clause (`{"Name":"f","Entity":…}`); the previous pass fixed neither
  and shipped. Two traps worth naming: (a) a **dedicated measures table is the common convention**, so
  on any model with one, the fact-table entity is wrong for *every* measure — if your codegen has a
  single `ENTITY` constant, columns and measures must not share it; (b) **verifying the measure's
  semantics in DAX is not verifying the reference** — the previous pass ran `'_Measures'[…]` (correct)
  while the PBIR encoded `'Orders'[…]` (wrong), and the green DAX result was read as proof. Prove the
  **exact entity/property path the PBIR encodes**, and keep a negative control: the wrong path must
  *error*.
- **On a visual that cannot render (an unlicensed azureMap, a tooltip-only page), this class of bug is
  undetectable by screenshot** — a wrong measure reference and an environmental blank look identical.
  Fall back to executing the encoded path against the live model.

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

- **Handing a bundle over: keep `definition.pbir`'s `byPath` resolving.** 🟢 Verified 2026-08-11. The
  deliverable is a **copy** of `<bundle>/pbip/` (the engine's `<bundle>/reports/` stays pristine as the
  attribution baseline; there is no `out/` level — a bundle is
  `<bundle>/{pbip,reports,semantic_models,handover,data}`). Two cases, and only one is a plain copy:
  - **Model per workbook — plain copy, no rewrite.** `<bundle>/pbip/<wb>/` already holds `<Name>.Report/`
    and `<Name>.SemanticModel/` as **siblings**, with `"path": "../<Name>.SemanticModel"`, and the
    delivery folder has the identical shape — so copy the **contents** of `<bundle>/pbip/<wb>/`, not the
    folder. (That folder is named for the *workbook*; the model inside is named for the *datasource*,
    so copying the folder itself nests them wrongly.)
  - **Shared/published datasource — the reference MUST be rewritten.** The model lands once in a
    shared `datasources/<ds-slug>/fabric/` while each report goes to `workbooks/<slug>/fabric/`. They
    are no longer siblings, so `definition.pbir` becomes
    `"../../../../datasources/<ds-slug>/fabric/<Name>.SemanticModel"` — four levels up from inside
    `<Name>.Report/`. **Verify it resolves on disk after writing it:** a broken `byPath` opens as a
    report with *no model*, which reads like a binding defect and sends you debugging the wrong layer.
  - **Never ship `<bundle>/reports/` itself** — it is a reference-only baseline, not portable. Its
    `definition.pbir` does not resolve where it sits: `reports/` holds only `*.Report` folders, so the
    `byPath` next to it has no model to point at (2026-08-11 it read `"../../pbip/<wb>/<Name>.SemanticModel"`;
    on a 2026-08-13 bundle it reads `"../<Name>.SemanticModel"` — ⚠️ engine-version dependent, and
    unresolvable either way).
  - **Compare engine truth to working copy with git, not PowerShell `diff`.** The two sides differ in
    shape, so compare the matching pair with:

    ```bash
    git diff --no-index --stat <bundle>/reports/<WB>.Report <bundle>/pbip/<WB>/<WB>.Report
    ```

    On Windows, bare `diff` is a PowerShell alias for `Compare-Object` and can compare only the two
    path strings. `git diff` exits 1 both when trees differ and when a path is wrong, so require a real
    stat line, not just the exit code.

- ⚠️ **A "shipped" deliverable can be structurally present and functionally EMPTY — check content, not
  existence.** Reported 2026-08-19 from a 46-asset estate, found by direct verification rather than by
  trusting a prior "done" status. A report folder that a sign-off had already passed contained only
  Desktop-local settings: no real pages, no visuals, no model — the copy step was silently skipped or
  partially interrupted, and every folder that was *supposed* to exist did exist. This is a different
  failure from the `byPath` case above: there the reference is broken and Desktop tells you; here the
  reference is fine and there is simply nothing behind it.
  - Assert the shipped `<Name>.Report/definition/pages/` enumerates real pages **with visuals**, and
    the shipped `<Name>.SemanticModel/definition/tables/` holds real tables — not merely that the
    folders exist. A folder count is not a content check.
  - **Trace the specific files you verified through to the shipped path.** Same estate, separate case:
    a semantic model shipped with an unresolved stub still in it because the *fixed* working copy was
    never the copy that got promoted. Verifying a fix in `<bundle>/pbip/` proves nothing about
    `migrations/**/fabric/` — one direction, three locations, and the last hop is the one nobody
    re-checks. Never assume a fix in one location propagated to another; re-run the check against the
    deliverable itself.
  - Corollary: a `✅ shipped` from an earlier session is a **claim**, not evidence. Re-verify it against
    the artifact before building on it.

- **Visual-level filter = a top-level `filterConfig` key in `visual.json` (sibling to `visual`, NOT
  nested under it)**, `type:"Categorical"`, `Version:2` `In`-condition. Nesting it under `visual` is
  silently ignored. **Do not use this as a slicer default** — slicer pre-selection is
  `objects.general[0].properties.filter` (see §1).
- **Stacked bar = `barChart` visualType (not `clusteredBarChart`); the first Y projection stacks from
  0.** Per-series colours via `dataPoint[]` with `selector.metadata = <queryRef>` (queryRef, not
  nativeQueryRef).
- **`displayName` on a projection is the header-rename mechanism** — Desktop auto-labels non-default
  aggregations "Average of X"; `nativeQueryRef` does not control the header.
- **When two re-runnable fix scripts must commute across a VISUAL-TYPE change, the type-specific
  property set must live in exactly one module.** A script that flips `columnChart` → `lineChart` and a
  separate script that writes label properties cannot be order-independent if each hard-codes its own
  vocabulary — run type-flip-last and you keep the old type's properties (`labelOverflow` on a
  lineChart), run it first and you lose the new type's. Fix: expose `apply_<layer>(doc, visual_type)`
  **and** `strip_foreign_<layer>_props(doc, visual_type)` from one module; whichever script runs last
  re-applies the correct set for the final type and strips the other's leftovers, so both orders land
  byte-identical. Prove it by hashing the whole `definition/` tree across several shuffled orders, not
  by reasoning about it.
- **Normalise JSON key order when you write PBIR back.** Two scripts that *insert* different formatting
  cards into `visual.objects` produce byte-different-but-semantically-identical files depending on run
  order, which makes a genuine order-independence proof impossible to distinguish from a real defect.
  Sort keys on write once, in the shared helper.
- **Reference-line `value` needs a type-suffixed numeric literal** (`{Literal:{Value:"100D"}}`); a bare
  `"100"` parses to 0 and pins the line to the axis baseline with **no validation error**.
- **Theme: custom `visualStyles` are strictly validated per-visual-object and `fillPoint` is not valid
  for scatterChart/filledMap** — keep custom themes minimal (set visual-specific formatting in each
  `visual.json`); the theme file's internal `name` must exactly equal the `report.json` `customTheme`
  reference **including `.json`**. Single-line caption/legend textboxes need ≥3.4 grid rows or they trip
  `PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR`.
- **`reportVersionAtImport` belongs inside every `themeCollection` entry, never at the top level of
  `report.json`.** 🟢 CLI-verified against `powerbi-report-author` 0.1.4. Removing it from
  `themeCollection.baseTheme` raises a schema error (`/themeCollection/baseTheme must have required
  property 'reportVersionAtImport'`); removing it from `customTheme` raises both
  `PBIR_THEME_VERSION_AT_IMPORT_MISSING` and the schema error; adding it at report root raises
  `PBIR_SCHEMA_VALIDATION_ERROR` for an additional property. Ground-truth committed shape:
  `examples/shipping-kpis/fabric/ShippingKPIs.Report/definition/report.json` has top-level keys
  `$schema`, `themeCollection`, `resourcePackages`, `settings`, and both theme entries carry
  `name`, `type`, `reportVersionAtImport`.
- **What-if % slicer format: `0.0"%"` (quoted) when the stored value is pre-scaled (e.g. 22.8); `0.0%`
  (unquoted) only when it is a true 0–1 fraction** — mixing them mis-scales the display by 100×.
- **Check `formatString` against the field's actual numeric scale, not its semantic meaning.** A source
  field can already be stored pre-scaled (`12.83` meaning "12.83%", not `0.1283`). Power BI's `0.00%`
  multiplies by 100 for display, so an already-scaled value renders **100× inflated** (`1283%`). Sample
  a raw value via DAX before choosing `0.00%` (true fraction) vs `0.00"%"` (literal suffix).
- **`validate` does NOT check that `definition.pbir`'s model reference resolves.** 🟢 verified: a
  `.Report` whose `datasetReference.byPath.path` named a `.SemanticModel` folder that **exists nowhere
  in the bundle** returned `errorCount: 0` — shape, not target again, so a report that **cannot
  possibly open** validates clean. Check the path yourself (resolve it relative to the `.Report` folder
  and confirm a `definition/` inside). Two migration-specific traps make this likely rather than exotic:
  (a) an engine that emits the report **twice** (a `reports/` deliverable plus a packaged `pbip/`) gives
  each copy a *different* relative path, and only the one beside the model is right — the copies being
  byte-identical everywhere else hides it; (b) the model folder is often named after the **data
  source**, not the workbook, so a workbook-named guess dangles. Deleting a redundant copy is usually
  the wrong fix if the engine's own manifests declare it as `output_folder` — repoint it instead, and
  prefer a relative cross-tree `byPath` (`../../pbip/<name>/<Model>.SemanticModel`), which does resolve.
- **A missing REQUIRED role in engine output means the measure was STUBBED — bind the stub, do NOT
  delete the visual.** 🟢 Measured 2026-08-18 on a cold run of engine 2.151.0 (issue #220).
  `validate` failed with `PBIR_ROLE_REQUIRED_MISSING` — *Required role "Y" missing or has no
  projections for visualType "clusteredColumnChart"* — on the engine's own **pristine** output. Cause:
  that visual's only measure was a Tableau `FIXED` LOD which fell back to an inert stub
  (`unresolved_reference: cross-table terms`), and the engine dropped the projection rather than
  binding it; the two sibling visuals whose measure *translated* bound `Y` normally. ⚠️ The repair is
  **not** to delete the visual — the stub already exists in the model as
  `measure 'Regional Revenue (FIXED)' = BLANK()`, so re-adding it to `Y` makes the report valid and
  openable while keeping the gap visible (a blank column) and the preserved `TableauFormula`
  annotation pointing at what to restore. Deleting instead throws away the only in-report evidence
  that the source had a chart there. So before concluding a field is missing, grep the model for a
  `= BLANK()` measure matching the worksheet's shelf.

### Declaring a generated PBIR edit, or sign-off blocks

Every file under a `*.Report` folder is hash-baselined by the engine run, and the orchestrator's
pre-sign-off `check_migration_progress.py --bundle <b> --tamper` exits **1** on any that changed
without a matching declaration. `scripts/declare_generated_edit.py` is the **only** thing that writes
one: it runs your `_build/` script for you and records the before/after hashes as one append-only
`_build/generated-edit-declarations/*.json` file.

```bash
python scripts/declare_generated_edit.py --bundle <b> \
  --target pbip/<WB>/<WB>.Report/definition/pages/<p>/visuals/<id>/visual.json \
  --script <b>/_build/fix_axis_title.py \
  -- --only pbip/<WB>/<WB>.Report/definition/pages/<p>/visuals/<id>/visual.json
# DECLARE: RECORDED pbip/.../visual.json -> <b>/_build/generated-edit-declarations/<timestamp...>.json
```

Measured — each of these leaves the gate RED while looking like it worked:

- **One `--target` per run.** A second run of an idempotent script prints `DECLARE: NO_CHANGE` and
  records nothing, so a whole-tree emitter leaves every file but one UNDECLARED. Give the script an
  `--only <bundle-relative path>` scope argument, pass it after `--`, and run the wrapper per target.
- **Never hand-edit first.** The wrapper hashes the target *before* running your script and the gate
  only accepts a declaration whose baseline is the engine's hash, so a retro-declaration is never
  accepted. Restore the target by re-running the engine — `reports/` is a **different file**, not a
  copy of `pbip/` (see the `byPath` entry above).
- **Declare last.** Touching the target again afterwards invalidates its declaration.

Self-check before reporting done: `--tamper` must exit 0 (`DECLARED_DRIFT` passes, `DRIFT` does not).

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

**Bing `map` colour saturation is INERT — a §1-class validation-invisible bug.** 🔴 render-verified
NEGATIVE (2026-08-08, Desktop MSIX 2.157.627.0). `formatting describe-object map dataPoint` *does*
list `fillRule` with `displayName: "Color saturation"` (`type: unknown`), so a
`linearGradient3` over a projected measure looks correct and `validate` returns **0 errors** — but
Desktop renders **one flat hue**. Proof used: of 604 bubbles, 138 had negative values and the minimum
(−13,838) belonged to the *second-largest* bubble on the map, which an orange minimum stop must render
unmistakably orange; a pixel scan found **zero** orange pixels, and the max-value and min-value bubbles
rendered *identically pale*. **Do not read bubble lightness as a colour encoding** — on a bubble map
apparent lightness tracks **radius** (stroke-to-fill ratio), so small bubbles look dark and large ones
look pale whether or not any gradient is bound. Binding the measure to a `Gradient`
("Color saturation") **data role** instead does not rescue it: `catalog describe map` exposes only
`Category`/`Series`/`Y`/`X`/`Size`/`Tooltips`, `validate` raises `PBIR_ROLE_UNKNOWN`, and Desktop then
fails to produce canvas-capture metadata at all (`clip` height `0`) until it is removed. **Consequence:
a source `color = <measure>` encoding is not reproducible on Bing `map`** — it needs `azureMap` (whose
`bubbleLayer.fillColor` *does* honour the same FillRule), which in turn needs the Azure Maps tenant
entitlement. Treat that as a tenant/human action and log the gap rather than shipping a map that
silently drops one whole encoding channel.

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

**Match the source worksheet's basemap before choosing `defaultStyle`.** 🔴 render-verified (cold run
S20): applying the old "prefer `blank_accessible`" advice to 9 map visuals produced **9 blank-basemap
maps** — marks floating on white — because those source worksheets did draw real basemaps. For Tableau
specifically, read the worksheet basemap first; use `grayscale_light`, `night`, `satellite` or another
real style when the source has one, and reserve `blank_accessible` (with `showStylePicker: false`,
`showNavigationControls: false`, light `polygonStrokeColor`) for a source that genuinely draws none.
⚠️ The original `blank_accessible` advice is the deterministic engine maintainer's
(`Yarbrdab000/tableau-fabric-skills#106`, 2026-08-10, Desktop 2.157.627.0) and **not independently
reproduced here**; note it is a different value from plain `'blank'`, which rendered empty/tiny above.

**⚠️ `shapeMap` renders NOTHING — a blank rectangle** (same source, same session, a US-state
choropleth shaded by `Sum(Profit)`; `powerbi-report-author validate` returns 0 errors for it). That
makes the *modern* branch worse than the deprecated one: `filledMap` at least draws, and merely warns.
**Not independently reproduced here** — but it explains why a pristine engine run emits `shapeMap` for
several worksheets that nobody ever saw rendered, because our tier converts them to `azureMap` first.
Treat `shapeMap` as unusable rather than merely superseded.

**Route/line maps:** azureMap draws true 2-point routes via `PathID` + `PointOrder` + `pathLayer` (a
fidelity win over the dual-axis workaround) — but it needs **one data row per endpoint**. If the fact
stores origin+destination lat/long as columns on a single row, the arc cannot render, and that reshape
is a **semantic-model decision**: coordinate with the model owner up front for any `MAKELINE`/
`MAKEPOINT`-style route map, or fall back to endpoint bubbles with a documented note.

**Undocumented azureMap properties** — none appear in the CLI `catalog describe` output, so they are
recorded here rather than rediscovered. ⚠️ **Confidence: structural only.** They are accepted by
`validate` (0/0) and survive a Desktop reload, but they were authored in an environment where azureMap
cannot draw (see the licensing note above), so *no one has seen them take visual effect*. Treat the
property **names** as verified and their **rendered behaviour** as unconfirmed; upgrade to 🟢 on the
first signed-in render.

- **`bubbleLayer.sizeByValue`** (boolean) — the switch that makes the `Size` well actually drive bubble
  radius. Expected behaviour without it: `Size` is projected but every bubble draws at the fixed
  `bubbleRadius`, silently dropping the magnitude encoding. Pair with `minBubbleRadius` / `maxRadius`
  to bound the ramp.
- **`mapControls.defaultStyle`** accepts more than `'road'`: **`'satellite'`** and **`'night'`** are both
  accepted, and are the faithful targets for a Mapbox-satellite or dark-basemap source worksheet.
  (`'blank'` is also accepted but rendered empty/tiny with `autoZoom` — see above.) Literal form is a
  quoted string: `{"Literal":{"Value":"'satellite'"}}`.
- **`visualTooltip`** — binds a **canvas tooltip page** to a visual, and is how a Tableau "viz in
  tooltip" worksheet is reproduced. It is a **`visualContainerObjects`** entry, *not* a
  `visual.objects` one. Exact shape (three properties):
  `show` = `true`, `type` = `'Canvas'`, `section` = **the target page's `name`/folder id**
  (e.g. `'page-tooltip-over-time'`) — the page **id, not its `displayName`**, which is the easy
  mistake. The tooltip *page itself* is render-verified; the *hover binding* is not, because the map
  that would trigger it cannot draw here.

## 6. Scatter

**`scatterChart` X and Y must BOTH be MEASURES, never a grouping column.** Binding `Y` (or `X`) to a
dimension renders "Remove Values to display x- and y-axis pairs" — validation-clean, Desktop-only.

A "dimension-on-rows dot strip" → scatter with `Category` = the dimension (Details, one dot each),
`X` = value measure, `Y` = a **constant baseline measure** (`measure 'Dot Baseline' = 0`, hidden),
`Size` = value measure, colour via a `FillRule` gradient on a signed diff measure; hide the constant
`valueAxis` (`show:false` + `showAxisTitle:false`).

## 7. Desktop verification mechanics

- **A screenshot can be PARTIALLY drawn, and that is more dangerous than a blank one.** 🟢
  Render-verified 2026-08-09, bridge CLI 0.1.2. Visuals that fetch remote resources — above all
  `azureMap`, which chains model query → basemap tiles → remote reference-layer GeoJSON → marks —
  draw **progressively**. Capture too early and you get a plausible, finished-looking map that is
  silently missing marks. Measured on one page (604 city pies), same report, same warm Desktop:
  **411** distinct canvas colours captured immediately after navigating (pies only in the
  western/central US) vs **41,185** once settled (pies nationwide). Both look like real maps; only
  the second is. This defeats mark-counting and any "does it render" check.
  - **`screenshot-all --settle <ms>` does NOT fix it.** The flag exists but delays only before the
    **first** capture. Measured: `--settle 5000` over 10 pages cost **38 s**, not the ~76 s a
    per-page delay would cost. Use it to cover the post-`reload` cold start — nothing more.
  - **`sleep(n)` then `screenshot` does NOT fix it either**, and is the trap most likely to catch
    you: the sleep elapses while sitting on the *previous* page, then the screenshot verb navigates
    and captures almost immediately, so the page you actually want gets **no settle at all**.
  - **What works: recapture until ONE digest stays unchanged for a dwell — not merely until two
    frames match.** Two consecutive equal frames only prove the render paused; the rule the
    implementation actually enforces is that the digest is unchanged across the whole
    `--stable-seconds` window, and that **time spent inside a blocking screenshot call does not
    count toward the dwell** (an early version counted it, so slow captures banked "stable" time
    they had not earned). Even then it is a *heuristic*, not a readiness signal — bridge CLI 0.1.2
    exposes none — so a partial plateau longer than the dwell still passes: raise it for cold or
    high-risk maps. It beats a fixed timeout in both directions, because one long sleep is
    simultaneously too slow for warm pages and too short for cold ones (measured: 108 s vs 209 s
    for 10 pages). Repo-local implementation:
    `python scripts/capture_powerbi_pages.py <report.Report> <out-dir> --pid <pid>` — that script
    lives in the host repo, not in this bundle, so outside it implement the rule above directly.
  - Corollary: **after any `reload`, treat the first captures as suspect** — the earliest pages in a
    batch are the ones that race.
- **The `powerbi-desktop` bridge has NO refresh verb.** Verbs as of bridge CLI 0.1.2: `status`,
  `manifest`, `open`, `reload`, `screenshot`, `screenshot-all` (underlying methods
  `application.state.get` / `report.snapshot.capture` / `file.reload`). PBIP stores no data cache, so a
  freshly-opened import report renders **empty** ("tables have incomplete or no data"). **A clean
  screenshot with empty visuals is an unrefreshed-model artifact, not a binding defect.**
- **A blank or sparse screenshot is not evidence of a blank or sparse page** — see the
  partial-render entry above; re-capture with a stability dwell before trusting any image.
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

- **A `.twb`/`.twbx` DOES contain Tableau-rendered reference images — check before declaring "no
  reference exists".** Every worksheet embeds a base64 PNG in a `<thumbnail>` element (192×192, one per
  worksheet). Three separate agents on one migration asserted no reference was obtainable because there
  was no Tableau Server/Public URL; the ground truth was inside the file the whole time. They are small,
  so use them for **shape, mark type, layering, axis direction, label presence, header formatting and
  actual numbers** — not font/pixel claims. This is decisive evidence: on one migration the thumbnails
  overturned a confidently-reasoned static mapping (see next entry).
- **Tableau's `<mark class='Automatic'/>` resolves to LINE when a date field sits on Columns — even a
  discrete date part.** Reading `Automatic` + a discrete `:ok` date pill as "bars" is a natural but
  wrong inference, and it produced a `columnChart` with the rank measure on `Y` and 17 categories on
  `Series` — which **stacks**, summing 17 ranks into a meaningless ~153 bar, for a source that was a
  classic bump chart of 17 crossing lines. The engine reported it `tier: rebuilt, status: rebuilt` with
  no warning, because stacking is structurally valid. **Never resolve `Automatic` from the XML alone
  when a date is on Columns — check the thumbnail.**
- **`columnChart` + `Series` STACKS. Never use it for a ranking/index measure.** Stacking is only
  meaningful for additive quantities; ranks, indices, percentages and averages are not additive, so a
  stacked encoding of them is always wrong and never raises a validation error.
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
- **Set a sensible default on every filter-driving slicer, via `objects.general[0].properties.filter`,
  never a top-level `filterConfig`, which pre-selects nothing (§1).** Without an *effective* default
  every bound visual renders an aggregate-across-all-rows value on first load (in one workbook: an
  aggregate across 906 cities) — which reads as "broken" even though the DAX and binding are correct.
  Pick a default matching the reference screenshot, and confirm visually.

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

## 10. Reading the report-side handover queue

```bash
python scripts/read_handover.py <bundle> --workbook <name> --viz
python scripts/read_handover.py <bundle> --workbook <name> --viz --severity blocking
python scripts/read_handover.py <bundle> --list        # estate-wide triage, both queues
```

A handover slice is large (347 KB for a 60-stub workbook) and a file-read tool refuses it outright,
so reading it by hand means parsing it programmatically and then filtering. The reader does that
once and ranks the result.

### What `--viz` puts first, and why it matters

**Emptied visuals** (`pbip_ref_drops[].emptied`) - visuals whose *every* field binding was dropped.
They render blank on a report that validates clean, and nothing else ranks them: in the worked
example there were **15**, sitting unremarked beside a 170-item `remediation_worklist`. This is the
single highest-value thing the reader surfaces, and it is why `--severity` **never hides them** - a
blank visual outranks any severity band the worklist assigns.

Then `remediation_worklist` grouped by category, with each distinct `remediation` text printed
**once** rather than repeated per item, then `viz_fidelity` tier counts.

⚠️ **A previous version of this section claimed these payloads were "unreachable at any workbook
size" because they sit ~93% into the file.** The offsets are real (`remediation_worklist` at byte
156,764, `viz_fidelity` at 315,571 of a 347 KB slice); the conclusion drawn from them was **wrong and
has been retracted** - see `powerbi-semantic-model-gotchas` §8 for the falsifying experiment and the
lesson. A deep byte offset is evidence of a byte offset, not of unreachability.

### Still true: a `viz_fidelity` reason can be a deferral you must NOT reverse

Reading the queue is necessary, not sufficient. Measured entry, quoted in full because the
abbreviated form reads as a simple gap:

> *"table-calc filter on 'Last' (LAST) is not reproduced: it runs after aggregation and HIDES marks,
> which Power BI cannot express as a filter ... 6 other table calc(s) share this view and would be
> silently re-scoped if it were re-added as an ordinary filter."*

Re-adding that as an ordinary filter would silently change six other visuals' numbers. The validator
classifies each row as **fixable / accepted-limitation / false-claim**; repair only what it routes to
you, and route a false claim back rather than fixing it quietly.
