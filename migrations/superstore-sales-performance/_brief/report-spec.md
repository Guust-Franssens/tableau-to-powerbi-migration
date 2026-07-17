# Report Spec — Superstore Sales Performance

**Status:** Self-approved by `pbi-report-builder` (autonomous one-shot subagent invocation — no
interactive user available for the literal Approval Gate; every judgment call below is flagged
explicitly in the final report back to the orchestrator instead). Per explicit task instructions:
"work through it fully... don't stop at a partial page plan."

## Round 0 — Dependencies

- Semantic model: `SuperstoreSalesPerformance.SemanticModel` (TMDL), already built and deployed
  (folder exists; no live Desktop/Fabric session assumed). Verified directly by reading all 13 TMDL
  table files plus `model.tmdl`/`relationships.tmdl`.
- No live `powerbi-modeling-mcp` connection available in this environment — worked entirely from
  TMDL files, which is the documented fallback.
- No existing `.pbip`/`.Report` — both created fresh this task, mirroring
  `migrations/eea-urban-adaptation/fabric/UrbanAdaptation.pbip` + `.Report` folder shape exactly
  (confirmed via direct inspection: `.platform`, `.pbi/localSettings.json`, `definition.pbir`,
  `definition/{report.json,version.json,pages/{pages.json,mainPage/{page.json,visuals/*}}}`,
  `StaticResources/RegisteredResources/theme.json`).

## Round 1 — Audience & Job

- **Audience**: internal retail-analytics stakeholders (regional sales leadership, ops/logistics,
  merchandising) — inferred from the source workbook's content (region/date/KPI comparisons,
  prescriptive scatter diagnostics, narrative annotations) and Ryan Sleeper's own public framing of
  this workbook as a "descriptive → prescriptive → annotated narrative" 3-act analytics story.
- **Job to be done**: (1) *Descriptive* — "how did Sales/Profit Ratio/Days-to-Ship perform this
  period vs. the comparison period, by region, over time?" (2) *Prescriptive* — "which
  state/sub-category/segment combinations explain the KPI movement, and where should I look next?"
  (3) *Annotations* — "what are the 3 headline take-aways an analyst has already flagged?"
- **Decision the report drives**: where to focus regional sales/ops attention next period; not a
  finance-of-record report (no audit trail requirement).

## Round 2 — Model Inventory & Scope

Full model read (13 tables). Key facts respected as instructed (not re-derived):

- 5 Field Parameter tables (`Y-Axis`, `X-Axis`, `Map KPI`, `Scatter Plot Detail`, `Date Granularity`)
  — each has a display column (bind to slicers) and a hidden `<Name> Fields` NAMEOF column (bind to
  the visual's axis/values/details well).
- `Region Parameter` (plain 5-value disconnected list: US/Central/East/South/West, default **East**)
  and `Date Comparison` (plain 2-value list: Prior Period/Prior Year, default **Prior Period**) —
  ordinary single-select slicers on `[Region]`/`[Date Comparison]`, backed by
  `SELECTEDVALUE(...)` measures.
- `Minimum Date` / `Maximum Date` (disconnected date-list slicer tables, 2014-01-01 to 2018-12-31,
  defaults **2016-09-01** / **2017-03-31** exactly matching `migration-spec.json`'s
  `param.minimum_date` / `param.maximum_date` current values).
- `Insights` — static 3-row seed table (`Insight Number`, `Insight Text`, `Indicator` ∈
  Positive/Neutral/Negative), verbatim from `param.insight_1/2/3` + `_indicator` current values.
- `Region Average` benchmark measures confirmed by exact name: `Sales Region Average`,
  `Profit Ratio Region Average`, `Days to Ship Region Average` — each `AVERAGEX(ALL(Region), CP <X>
  (All Regions))`, documented in TMDL as the Bullet/Region-Comp reference-line target.
- CP/PP/Difference/Trend measure families confirmed for Sales, Profit, Quantity, Discount, Returns,
  Days to Ship, Profit Ratio — `(Trend)` PP variants exist for Sales/Profit/Profit Ratio/Days To Ship
  specifically for axis-aligned trend charts.
- **Scale traps checked via TMDL formatString (not guessed):** `CP Profit Ratio`/`PP Profit
  Ratio (Trend)`/`Profit Ratio Region Average` are all **native 0–1 fraction**, `formatString: 0.0%`
  (safe, standard). `CP Profit Ratio Pct`/`CP Discount Pct` are **pre-scaled ×100**,
  `formatString: #,##0.0` (no "%" baked in) — these two are used **only** inside the Field-Parameter
  measures (`Y-Axis`/`X-Axis`/`Map KPI`), never on the fixed KPI-column visuals. `Profit Ratio
  Difference` is also pre-scaled ×100 (percentage-point difference), `formatString: #,##0.0`.
  **Decision:** the 3 fixed KPI columns (Descriptive page) bind the **native** `CP Profit Ratio` /
  `PP Profit Ratio (Trend)` / `Profit Ratio Region Average` (all already correctly `0.0%`) — no
  format override needed there. `Profit Ratio Difference` gets a report-level display format
  `+#,##0.0"pp";-#,##0.0"pp";0.0"pp"` (percentage-**point** suffix) at author time, to disambiguate
  a pre-scaled point-difference from a plain count — flagged as a `report_build` decision, not a
  model change.
- **Region Comp resolved via TMDL doc comments, not guessed:** despite the worksheet's raw
  `field_id` pointing at the plain `CP <X>` measure, the TMDL is explicit that the 3 Region-Comp
  dot-plots must bind `CP <X> (All Regions)` sliced by `'Sample Superstore'[Region]` (the
  region-*filtered* `CP <X>` would evaluate identically for all 4 region rows, defeating the "compare
  across regions" purpose — confirmed by reading the measure's own `FILTER(ALL(Region), ...)` logic).
  Color = `'Region = Region Parameter'` (boolean highlight), Label = `'Region Abbreviation'`.
- **Map colors are binary highlights, not KPI gradients** (Descriptive page only): `US Map Color` =
  `IF([Region Parameter Value]="US","US","NULL")`, `Region = Region Parameter` = boolean — both
  2-state highlight fields, matching the sampled screenshot palette (teal=on/gray=off), *not* a
  continuous choropleth. The Prescriptive page's map is the one genuine continuous/diverging
  choropleth (`Map KPI Difference`, signed, teal=improved/red=decreased/gray=no data).
- **Scatter color resolved to the correct sibling field:** `map_kpi_difference_0` ≠
  `Map KPI Difference` — its caption resolves to `Map KPI Difference >= 0` (a **boolean**), which is
  what the Prescriptive Scatter Plot's color well actually needs (2-state teal/red, no gray — a
  scatter never plots a "no data" point by construction). The Prescriptive **Map** keeps the
  continuous signed `Map KPI Difference` (3-state incl. gray-for-blank).
- No explicit Min/Max scale measures exist for the 3 Gauge (Bullet-derived) visuals — Power BI's
  native Gauge needs a Minimum/Maximum. **Decision:** Minimum = static 0; Maximum = a static
  per-KPI ceiling set at authoring time from a reasonable headroom over the observed CP value range
  (no live DAX query engine available to sample exact percentiles in this environment) — exact
  chosen constants documented as a `report_build` limitation for later human tuning if the static
  ceiling turns out too tight/loose once real numbers render.
- Out of scope / no equivalent by design (per task brief, not re-litigated): `CLICK TO HIGHLIGHT`
  family (superseded by native cross-filtering + an optional drillthrough), live Insight text-entry
  parameters (superseded by the static `Insights` table).

## Round 3 — Narrative & Page Plan

**Page-split decision: 1 Tableau dashboard = 1 Power BI page (3 pages total).** Rationale (unchanged
from pre-brief analysis, restated here for the record):
1. All 3 dashboards are Tableau "Floating" layouts whose zones are already non-overlapping and
   sequentially organized (header → controls → dividers → content → footer) — there is no natural
   fold requiring a split.
2. The KPI-column / map-row / insight-row groupings are explicitly designed for **side-by-side**
   comparison (3 KPI columns compared at a glance; 4 regional maps compared at a glance) — splitting
   across pages would break the comparison the source dashboard is built around.
3. At **visual-group** granularity (not raw visual count), each page has ~6-8 groups (a header/filter
   band, a map row, 3 KPI column-groups, a footer — or the Prescriptive equivalent), satisfying the
   "7±2 groups" composition guidance even though raw leaf-visual counts are high (32 / 26 / ~10 after
   consolidation — see below).
4. My own agent instructions explicitly anticipate dense floating dashboards fitting as single pages
   "when zones are well-organized" — true here.

**Consolidations applied before counting "distinct visual units" (documented, not silent):**
- 3 nav-button worksheets per dashboard (Active + 2×Inactive, ×3 dashboards = 9 worksheets total) →
  **1 native Page Navigator visual** per page (Power BI's built-in active/inactive styling supersedes
  the manual Tableau button-swap trick).
- Prescriptive `X-Axis Label` worksheet + the Scatter's own nested `Y-Axis Label` row-field →
  **dropped**, relying on Power BI's native behavior of showing the currently-selected Field
  Parameter member's display name as the axis title automatically (a cleaner native equivalent — see
  `report_build` limitations for the fallback plan if Desktop rendering doesn't confirm this).
- Annotations dashboard's "entry form" section (3× live `parameter`-type zones,
  `param.insight_N`/`param.insight_N_indicator` — a **write** surface with no Power BI equiv400
  equivalent) + its separate read-only "PREVIEW" section (3× `ws.insight_N`/`ws.insight_N_indicator`
  worksheets) → **consolidated into one read-only section** styled with the entry-form's original
  visual prominence (bordered per-insight card), since both sections would show byte-for-byte
  identical content once the entry form can only ever be read-only in Power BI — true duplication add
  no information. **Flagged explicitly as a judgment call** in the final report.

**Resulting page/visual-unit counts:**

| Page | Source dashboard | Top-level zones | Distinct visual units (post-consolidation) |
|---|---|---|---|
| 1 | Super: Descriptive | 34 | 32 |
| 2 | Super: Prescriptive | 31 | 26 |
| 3 | Super: Annotations | 24 | ~15 (entry+preview merge removes 6 duplicate zones) |

## Round 4 — Design Identity, Accessibility, Delivery

- **Tone**: *Industrial Dense (Analyst Workbench)*, remixed with the source's own sampled palette
  (not the catalog's generic steel-blue/burnt-orange) — its "sales-pipeline workbenches" example
  domain is a near-literal match for this workbook. Deliberately overrides the tone catalog's generic
  "Retail/consumer → Playful Energetic" domain default: the actual audience (internal sales/ops
  analysts) and screenshots (dense corporate-analytical, no consumer-facing polish) dictate the
  denser, more technical tone.
- **Signature**: *Composite KPI Column* (remix of catalog S9, repeated ×3 per Descriptive page as the
  3 KPI columns) + *S1 tabular numerals* for all KPI values (callouts, differences, bullet/gauge
  labels) so digits align across the repeated column pattern.
- **Archetype**: *Analytical Canvas* for all 3 pages (multi-visual, filter-rich, comparison-heavy —
  matches every page). Two documented, justified deviations from this archetype's generic guidance:
  1. **Gauge is used** for the 3 Bullet-derived KPI visuals despite the archetype table's generic
     "Avoid: Gauge" guidance — overridden by this repo's own `pbi-report-builder` agent file, which
     explicitly mandates Gauge for Circle/Bullet-graph-with-reference-line worksheets on migration-
     fidelity grounds. (The 3 Region-Comp worksheets are Circle+refline too, but are **not** Gauge —
     see chart-mapping note below; a Gauge physically cannot show 4 simultaneous per-region marks.)
  2. **5 slicers stay inline in a single horizontal band** (Descriptive/Prescriptive) despite the
     generic "2-3 slicers max, 4+ → vertical rail" guidance — overridden because the source has 5
     slicers arranged in exactly this horizontal band and migration fidelity requires preserving it,
     not converting to an unfamiliar rail layout.
- **Grid resolution**: **100×100** (not the skill's typical 12×12 example) — a deliberate deviation,
  justified by the extreme zone density (26-32 leaf placements per page) requiring near-exact 1:1
  fidelity to Tableau's native 0-100000 zone coordinate system (simple ÷1000 conversion). Regions are
  therefore many small, precisely-named units (e.g. `col1_callout`, `map_east`, `div_v1`) rather than
  the usual `header`/`kpis`/`hero`/`detail` broad-region convention.
- **Canvas**: **1920×1080 (FHD)**, not the source's nominal 1000×800 — justified because the source
  dashboards use `sizing_mode: "automatic"` (not fixed), meaning the design already tolerates
  aspect-ratio changes by design; FHD is the design-brief skill's own greenfield default and gives
  more absolute pixel real estate for 26-32 leaf visuals per page. (Note: this diverges from
  `eea-urban-adaptation`'s 1366×890 canvas — an independent, lower-density report; canvas size is not
  a cross-migration consistency requirement.)
- **Color map** — sampled via PIL pixel-sampling directly from the 3 reference screenshots (exact
  RGB, high confidence, verified against the Prescriptive legend caption text "IMPROVED
  PERFORMANCE / DECREASED PERFORMANCE / NO COMP AVAILABLE"):
  - `favorable` (positive/selected/improved) = **`#34657F`** (teal)
  - `unfavorable` (negative/decreased) = **`#FC4237`** (red)
  - `neutral` (unselected/no-comparison/context) = **`#E6E6E6`** (light gray)
  - `tab_active_bg` = `#D4DEE4`, `refline_tick` = `#00313C`, `background` = `#FFFFFF`
  - No green anywhere in the source — deliberately preserved (teal=good/red=bad/gray=neutral) rather
    than "corrected" to a conventional green/red/amber scheme.
- **Accessibility**: teal `#34657F` vs. white passes WCAG AA for text ≥14pt bold / UI components
  (contrast ≈ 4.9:1); red `#FC4237` vs. white is borderline (≈ 3.9:1) — used only for fills/badges
  with a text label alongside (never red text on white body copy) to avoid a contrast failure on
  small text. Teal/red pair alone is not colorblind-safe for protanopia/deuteranopia; every
  teal/red/gray semantic encoding in this report is **always paired with a redundant text/shape**
  cue (region abbreviation label, +/- sign, "IMPROVED/DECREASED" legend text, Indicator badge text)
  so color is never the sole channel — mitigates without changing the source's established palette.
- **Delivery**: local PBIP only (no Fabric workspace publish requested in the task brief) — skip the
  optional `powerbi-report-management` publish step.

---

## `Design Brief:`

```yaml
Design Brief:
  generated_by: pbi-report-builder (autonomous synthesis of powerbi-report-planning +
    powerbi-report-design, skill-tool invocation unavailable in this environment — SKILL.md and all
    references/*.md read directly and followed manually in full)
  contract_version: "1.0"
  source: migrations/superstore-sales-performance/migration-spec.json
  semantic_model: SuperstoreSalesPerformance.SemanticModel

  design_identity:
    tone: "Industrial Dense (Analyst Workbench) — remixed with source-sampled palette, not catalog default"
    signature: "Composite KPI Column (S9 remix) x3 per Descriptive page + S1 tabular numerals"
    deviations_from_generic_guidance:
      - "Gauge used for 3 Bullet-derived KPI visuals (archetype table says avoid Gauge for Analytical Canvas) -- overridden by this repo's own agent-file migration-fidelity mandate for Circle/Bullet+refline worksheets."
      - "5 slicers kept inline in one horizontal band on Descriptive/Prescriptive (generic guidance: 2-3 max, 4+ -> rail) -- overridden by migration fidelity to the source's own horizontal filter band."
      - "100x100 grid resolution (not the skill's typical 12x12) -- justified by extreme zone density (26-32 leaf placements/page) needing near-1:1 fidelity to Tableau's 0-100000 zone coordinates."
      - "1920x1080 FHD canvas, not source's nominal 1000x800 -- source uses sizing_mode=automatic (aspect-ratio tolerant by design); FHD is the skill's own greenfield default and fits high visual density better."

  archetype: Analytical Canvas

  color_map:
    favorable: "#34657F"     # teal - positive/selected/improved/CP-line
    unfavorable: "#FC4237"   # red - negative/decreased
    neutral: "#E6E6E6"       # light gray - unselected/no-comparison/PP-line/context
    tab_active_bg: "#D4DEE4"
    refline_tick: "#00313C"
    background: "#FFFFFF"
    text_primary: "#1F2E35"
    text_secondary: "#5B6B72"
    divider: "#D9D9D9"

  interaction_pattern: >
    Native Power BI cross-filter-on-click is the primary interaction model, superseding Tableau's
    CLICK TO HIGHLIGHT / Filtered State calculated fields (no semantic-model equivalent by design,
    per task brief). Clicking a state on the Prescriptive Map cross-filters the Prescriptive Scatter
    Plot -- this literally matches the source's own on-canvas caption "CLICK STATE TO FILTER SCATTER
    PLOT", kept verbatim since native PBI behavior fulfills it exactly as worded, no rewrite needed.
    Page Navigator (all 3 pages) replaces the 9 manual nav-button worksheets.

  accessibility: >
    Teal #34657F vs white passes WCAG AA for bold/large text and UI components (~4.9:1). Red
    #FC4237 vs white is borderline (~3.9:1) -- used only for fills/badges always paired with a text
    label, never as body text color. Every teal/red/gray semantic encoding is paired with a redundant
    non-color cue (region-abbreviation label, +/- sign, legend text, indicator badge text) since the
    teal/red pair is not colorblind-safe alone for protanopia/deuteranopia -- source palette preserved
    as-is (no green anywhere, deliberately not "corrected").

  theme:
    file: theme.json
    base: "fabric-collection/powerbi-authoring/skills/powerbi-report-design/assets/base.json (adapted)"
    font_family: "Segoe UI"
    good: "#34657F"
    bad: "#FC4237"
    neutral: "#E6E6E6"

  pages:

    # =================================================================
    - role: "Super: Descriptive"
      archetype: Analytical Canvas
      layout_variant: "Dense floating-to-grid: header+5-slicer-band, 5-map row, 3 KPI columns x 4 sub-visuals"
      variant_rationale: >
        Direct 1:1 translation of the Tableau Floating dashboard's own sequential zone
        organization (header -> controls -> divider -> maps -> divider -> 3 KPI columns -> footer).
        No archetype-standard hero/detail split applies since the source itself has no single
        "hero" visual -- the 5 regional maps are co-equal, and the 3 KPI columns are co-equal.
      page_background: "#FFFFFF"
      layout_summary: >
        1920x1080, 100x100 grid. Header band (rows 1-8): title + Page Navigator. Filter band
        (rows 9-17): 5 inline slicers (Start Date, End Date, Region, Date Comparison, Date
        Granularity). Divider. Map row (rows 24-43): 5 filled maps (All US + West/Central/East/
        South). Divider + 2 vertical dividers splitting 3 KPI columns (rows 47-94): each column =
        Callout -> Gauge (Bullet) -> Trend line -> Region-Comp dot-plot, for Sales / Profit Ratio /
        Days to Ship respectively. Footer band (rows 94-101): attribution text + logo image.
      layout_contract:
        canvas: { width: 1920, height: 1080 }
        grid: { columns: 100, rows: 100 }
        regions:
          header_bg: [1, 1, 101, 8]
          title: [2, 2, 46, 6]
          nav: [57, 1, 101, 8]
          slicer_start: [3, 11, 21, 17]
          slicer_end: [23, 11, 40, 17]
          slicer_region: [43, 11, 60, 17]
          slicer_comparison: [62, 11, 79, 17]
          slicer_granularity: [82, 11, 99, 17]
          div_h1: [1, 21, 101, 22]
          map_us: [1, 24, 21, 43]
          map_west: [21, 24, 41, 43]
          map_central: [41, 24, 61, 43]
          map_east: [61, 24, 81, 43]
          map_south: [81, 24, 101, 43]
          div_h2: [1, 47, 101, 48]
          div_v1: [34, 48, 35, 94]
          div_v2: [67, 48, 68, 94]
          col1_callout: [3, 49, 33, 54]
          col1_bullet: [3, 54, 33, 57]
          col1_trend: [3, 59, 33, 79]
          col1_regioncomp: [3, 82, 33, 92]
          col2_callout: [36, 49, 66, 54]
          col2_bullet: [36, 54, 66, 57]
          col2_trend: [36, 59, 66, 79]
          col2_regioncomp: [36, 82, 66, 92]
          col3_callout: [69, 49, 99, 54]
          col3_bullet: [69, 54, 99, 57]
          col3_trend: [69, 59, 99, 79]
          col3_regioncomp: [69, 82, 99, 92]
          footer_bg: [1, 94, 101, 101]
          footer_text: [2, 95, 78, 100]
          footer_logo: [95, 95, 101, 100]
        placements:
          - { id: title, region: title, kind: textbox, purpose: "Page title", field_bindings: { text: "'Super Sample Superstore Dashboard' (static, from source dashboard title zone)" } }
          - { id: nav, region: nav, kind: pageNavigator, purpose: "Replaces 9 manual nav-button worksheets across all 3 dashboards", field_bindings: {} }
          - { id: slicer_start, region: slicer_start, kind: slicer, purpose: "START DATE", field_bindings: { field: "'Minimum Date'[Minimum Date]" }, slicer_type: single-select (date list), default_value: "2016-09-01" }
          - { id: slicer_end, region: slicer_end, kind: slicer, purpose: "END DATE", field_bindings: { field: "'Maximum Date'[Maximum Date]" }, slicer_type: single-select (date list), default_value: "2017-03-31" }
          - { id: slicer_region, region: slicer_region, kind: slicer, purpose: "SELECT REGION", field_bindings: { field: "'Region Parameter'[Region]" }, slicer_type: single-select, default_value: East }
          - { id: slicer_comparison, region: slicer_comparison, kind: slicer, purpose: "DATE COMPARISON", field_bindings: { field: "'Date Comparison'[Date Comparison]" }, slicer_type: single-select, default_value: "Prior Period" }
          - { id: slicer_granularity, region: slicer_granularity, kind: slicer, purpose: "DATE GRANULARITY", field_bindings: { field: "'Date Granularity'[Date Granularity]" }, slicer_type: single-select, default_value: Month }
          - { id: div_h1, region: div_h1, kind: rectangle, purpose: "hairline divider (render ~2px, not full grid-cell height)", field_bindings: {} }
          - { id: map_us, region: map_us, kind: filledMap (shapeMap fallback), purpose: "All-US context map", field_bindings: { location: "'Sample Superstore'[State]", color: "'Sample Superstore'[US Map Color]" }, color_strategy: "categorical 2-value: 'US'->favorable, 'NULL'->neutral", comparison_basis: "binary highlight, not a KPI gradient (verified via TMDL: US Map Color = IF(Region Parameter='US','US','NULL'))" }
          - { id: map_west, region: map_west, kind: filledMap, purpose: "West region highlight map", field_bindings: { location: "'Sample Superstore'[State]", color: "'Sample Superstore'[Region = Region Parameter]" }, visual_filter: "Region = West", color_strategy: "boolean: TRUE->favorable, FALSE->neutral" }
          - { id: map_central, region: map_central, kind: filledMap, purpose: "Central region highlight map", field_bindings: { location: "'Sample Superstore'[State]", color: "'Sample Superstore'[Region = Region Parameter]" }, visual_filter: "Region = Central", color_strategy: "boolean: TRUE->favorable, FALSE->neutral" }
          - { id: map_east, region: map_east, kind: filledMap, purpose: "East region highlight map", field_bindings: { location: "'Sample Superstore'[State]", color: "'Sample Superstore'[Region = Region Parameter]" }, visual_filter: "Region = East", color_strategy: "boolean: TRUE->favorable, FALSE->neutral" }
          - { id: map_south, region: map_south, kind: filledMap, purpose: "South region highlight map", field_bindings: { location: "'Sample Superstore'[State]", color: "'Sample Superstore'[Region = Region Parameter]" }, visual_filter: "Region = South", color_strategy: "boolean: TRUE->favorable, FALSE->neutral" }
          - { id: div_h2, region: div_h2, kind: rectangle, purpose: "hairline divider", field_bindings: {} }
          - { id: div_v1, region: div_v1, kind: rectangle, purpose: "hairline vertical divider between KPI col 1/2", field_bindings: {} }
          - { id: div_v2, region: div_v2, kind: rectangle, purpose: "hairline vertical divider between KPI col 2/3", field_bindings: {} }
          - { id: col1_callout, region: col1_callout, kind: card, purpose: "Sales CP callout + difference", field_bindings: { primary: "'Sample Superstore'[CP Sales]", secondary: "'Sample Superstore'[Sales Difference]" }, callout_value_basis: "CP Sales big number; Sales Difference as secondary small label (source tooltip: 'CP Sales / Sales Difference')" }
          - { id: col1_bullet, region: col1_bullet, kind: gauge, purpose: "Sales bullet-graph vs region average", field_bindings: { value: "'Sample Superstore'[CP Sales]", target: "'Sample Superstore'[Sales Region Average]", minimum: "0 (static)", maximum: "static ceiling, set at authoring time from observed CP Sales range + ~25% headroom" }, color_strategy: "conditional fill by 'Sample Superstore'[Sales Difference] sign: >=0 favorable, <0 unfavorable", comparison_basis: "CP vs cross-region average (task-brief-confirmed reference-line hypothesis)" }
          - { id: col1_trend, region: col1_trend, kind: lineChart, purpose: "Sales trend, CP vs PP", field_bindings: { axis: "'Date Granularity'[Date Granularity Fields]", cp_line: "'Sample Superstore'[CP Sales]", pp_line: "'Sample Superstore'[PP Sales (Trend)]" }, color_strategy: "CP line = favorable (teal), PP line = neutral (gray) -- matches sampled screenshot", sort_policy: "chronological by underlying date, granularity switched via Date Granularity Field Parameter" }
          - { id: col1_regioncomp, region: col1_regioncomp, kind: scatterChart (dot-plot substitute for Circle+refline), purpose: "Sales region comparison", field_bindings: { x: "'Sample Superstore'[CP Sales (All Regions)]", legend: "'Sample Superstore'[Region]", label: "'Sample Superstore'[Region Abbreviation]", color: "'Sample Superstore'[Region = Region Parameter]" }, comparison_basis: "reference line = Sales Region Average", color_strategy: "boolean highlight: selected region = favorable, others = neutral", insight_basis: "Gauge cannot show 4 simultaneous per-region marks -- Scatter/dot-plot is the necessary override of the generic Circle+refline->Gauge mapping rule for this specific 4-mark-per-pane case" }
          - { id: col2_callout, region: col2_callout, kind: card, purpose: "Profit Ratio CP callout + difference", field_bindings: { primary: "'Sample Superstore'[CP Profit Ratio]", secondary: "'Sample Superstore'[Profit Ratio Difference]" }, callout_value_basis: "CP Profit Ratio (native 0.0% format); Difference formatted +#,##0.0\"pp\" (percentage-point, pre-scaled x100 in the model)" }
          - { id: col2_bullet, region: col2_bullet, kind: gauge, purpose: "Profit Ratio bullet-graph vs region average", field_bindings: { value: "'Sample Superstore'[CP Profit Ratio]", target: "'Sample Superstore'[Profit Ratio Region Average]", minimum: "0 (static)", maximum: "static ceiling ~2x observed average, set at authoring time" }, color_strategy: "conditional fill by 'Sample Superstore'[Profit Ratio Difference] sign" }
          - { id: col2_trend, region: col2_trend, kind: lineChart, purpose: "Profit Ratio trend, CP vs PP", field_bindings: { axis: "'Date Granularity'[Date Granularity Fields]", cp_line: "'Sample Superstore'[CP Profit Ratio]", pp_line: "'Sample Superstore'[PP Profit Ratio (Trend)]" }, color_strategy: "CP=favorable, PP=neutral" }
          - { id: col2_regioncomp, region: col2_regioncomp, kind: scatterChart, purpose: "Profit Ratio region comparison", field_bindings: { x: "'Sample Superstore'[CP Profit Ratio (All Regions)]", legend: "'Sample Superstore'[Region]", label: "'Sample Superstore'[Region Abbreviation]", color: "'Sample Superstore'[Region = Region Parameter]" }, comparison_basis: "reference line = Profit Ratio Region Average" }
          - { id: col3_callout, region: col3_callout, kind: card, purpose: "Days to Ship CP callout + difference", field_bindings: { primary: "'Sample Superstore'[CP Days To Ship]", secondary: "'Sample Superstore'[Days to Ship Difference]" }, callout_value_basis: "both plain day-count, #,##0.0 -- no scale trap here" }
          - { id: col3_bullet, region: col3_bullet, kind: gauge, purpose: "Days to Ship bullet-graph vs region average", field_bindings: { value: "'Sample Superstore'[CP Days To Ship]", target: "'Sample Superstore'[Days to Ship Region Average]", minimum: "0 (static)", maximum: "static ceiling ~2x observed average, set at authoring time" }, color_strategy: "conditional fill by 'Sample Superstore'[Days to Ship Difference] sign -- NOTE: lower is better for this KPI, so sign convention is INVERTED vs Sales/Profit Ratio (verified via Map KPI Difference measure's own -[...] negation for Days to Ship/Discount/Returns)" }
          - { id: col3_trend, region: col3_trend, kind: lineChart, purpose: "Days to Ship trend, CP vs PP", field_bindings: { axis: "'Date Granularity'[Date Granularity Fields]", cp_line: "'Sample Superstore'[CP Days To Ship]", pp_line: "'Sample Superstore'[PP Days To Ship (Trend)]" }, color_strategy: "CP=favorable, PP=neutral" }
          - { id: col3_regioncomp, region: col3_regioncomp, kind: scatterChart, purpose: "Days to Ship region comparison", field_bindings: { x: "'Sample Superstore'[CP Days To Ship (All Regions)]", legend: "'Sample Superstore'[Region]", label: "'Sample Superstore'[Region Abbreviation]", color: "'Sample Superstore'[Region = Region Parameter]" }, comparison_basis: "reference line = Days to Ship Region Average" }
          - { id: footer_text, region: footer_text, kind: textbox, purpose: "attribution", field_bindings: { text: "static: 'This workbook was created by Ryan Sleeper using the Sample - Superstore.xlsx data source.'" } }
          - { id: footer_logo, region: footer_logo, kind: image, purpose: "logo", field_bindings: { source: "extracted from source .twbx if available, else omit with a report_build note" } }
        space_audit:
          content_cell_count: 8300
          placed_cell_count: 6022
          empty_cell_pct: 27.4
          largest_region_pct: 7.2
          rule_check: "27.4% empty exceeds the generic <=15% analytical-page guidance -- DELIBERATE, documented deviation: the gaps mirror genuine breathing-room spacing present in the source Tableau dashboard itself (divider gaps between map row/KPI columns/footer), not laziness. No single region exceeds 45% of content (largest = col*_trend at 7.2%). No overlaps (verified programmatically)."

    # =================================================================
    - role: "Super: Prescriptive"
      archetype: Analytical Canvas
      layout_variant: "Dense floating-to-grid: header+5-slicer-band, legend caption, map+scatter split (left/right halves), 3 insight callouts"
      variant_rationale: >
        Direct 1:1 translation of the source's own left/right split (state map driving a
        cross-filtered scatter plot) plus its numbered insight callouts underneath. This is the
        report's true "detail/diagnostic" page in the 3-page narrative arc.
      page_background: "#FFFFFF"
      layout_summary: >
        Header + 5-slicer band (4 shared with Descriptive, 5th = Select KPI). Legend caption band
        explaining the teal/red/gray semantic + "circle size = CP value". Left half: state choropleth
        (Map KPI Difference) with its own "CLICK STATE TO FILTER SCATTER PLOT" caption (kept verbatim
        -- native PBI cross-filter fulfills this literally). Right half: 3 small Field-Parameter
        slicers (Y-Axis/X-Axis/Breakdown) atop a scatter plot. 3 insight rows below (shared with
        Annotations page's preview content, same Insights table rows). Footer.
      layout_contract:
        canvas: { width: 1920, height: 1080 }
        grid: { columns: 100, rows: 100 }
        regions:
          header_bg: [1, 1, 101, 8]
          title: [2, 2, 46, 6]
          nav: [57, 1, 101, 8]
          slicer_start: [3, 11, 21, 17]
          slicer_end: [23, 11, 40, 17]
          slicer_region: [43, 11, 60, 17]
          slicer_comparison: [62, 11, 79, 17]
          slicer_mapkpi: [82, 11, 99, 17]
          div_h1: [1, 21, 101, 22]
          legend_caption: [1, 22, 101, 24]
          div_h2: [1, 25, 101, 26]
          map_caption: [1, 27, 50, 30]
          prescriptive_map: [1, 30, 50, 79]
          div_v: [50, 26, 51, 79]
          scatter_slicer_yaxis: [53, 28, 67, 34]
          scatter_slicer_xaxis: [69, 28, 83, 34]
          scatter_slicer_breakdown: [85, 28, 98, 34]
          prescriptive_scatter: [52, 36, 99, 74]
          div_h3: [1, 79, 101, 80]
          insight1_indicator: [3, 80, 6, 84]
          insight1_text: [7, 81, 96, 84]
          insight2_indicator: [3, 85, 6, 89]
          insight2_text: [7, 86, 96, 88]
          insight3_indicator: [3, 89, 6, 93]
          insight3_text: [7, 90, 96, 93]
          footer_bg: [1, 94, 101, 101]
          footer_text: [2, 95, 78, 100]
          footer_logo: [95, 95, 101, 100]
        placements:
          - { id: title, region: title, kind: textbox, purpose: "Page title", field_bindings: { text: "static, same as page 1" } }
          - { id: nav, region: nav, kind: pageNavigator, purpose: "shared nav", field_bindings: {} }
          - { id: slicer_start, region: slicer_start, kind: slicer, purpose: "START DATE", field_bindings: { field: "'Minimum Date'[Minimum Date]" }, default_value: "2016-09-01" }
          - { id: slicer_end, region: slicer_end, kind: slicer, purpose: "END DATE", field_bindings: { field: "'Maximum Date'[Maximum Date]" }, default_value: "2017-03-31" }
          - { id: slicer_region, region: slicer_region, kind: slicer, purpose: "SELECT REGION", field_bindings: { field: "'Region Parameter'[Region]" }, default_value: East }
          - { id: slicer_comparison, region: slicer_comparison, kind: slicer, purpose: "DATE COMPARISON", field_bindings: { field: "'Date Comparison'[Date Comparison]" }, default_value: "Prior Period" }
          - { id: slicer_mapkpi, region: slicer_mapkpi, kind: slicer, purpose: "SELECT KPI", field_bindings: { field: "'Map KPI'[Map KPI]" }, default_value: Sales }
          - { id: div_h1, region: div_h1, kind: rectangle, purpose: "hairline divider", field_bindings: {} }
          - { id: legend_caption, region: legend_caption, kind: textbox, purpose: "semantic color legend", field_bindings: { text: "static: 'IMPROVED PERFORMANCE (teal) / DECREASED PERFORMANCE (red) / NO COMP AVAILABLE (gray) -- circle size represents CP value', with 3 small color swatch shapes inline" } }
          - { id: div_h2, region: div_h2, kind: rectangle, purpose: "hairline divider", field_bindings: {} }
          - { id: map_caption, region: map_caption, kind: textbox, purpose: "instruction caption", field_bindings: { text: "static: 'CLICK STATE TO FILTER SCATTER PLOT' -- kept VERBATIM, native PBI cross-filter fulfills this literally, no rewrite needed" } }
          - { id: prescriptive_map, region: prescriptive_map, kind: filledMap, purpose: "state-level KPI difference choropleth", field_bindings: { location: "'Sample Superstore'[State]", color: "'Sample Superstore'[Map KPI Difference]" }, visual_filter: "'Sample Superstore'[Region Filter] = TRUE (dynamic, parameter-driven -- NOT the 4 Descriptive maps' hardcoded literal-region filter)", color_strategy: "continuous diverging: positive->favorable, negative->unfavorable, blank/no-data->neutral (genuine gradient, NOT a binary highlight like the Descriptive page's maps)", insight_basis: "the one visual on this page acting as the cross-filter source for the scatter", verified_correction: "CORRECTED after re-verifying against the screenshot during skeleton review: the reference screenshot shows this map auto-zoomed/cropped to ONLY the selected region's states (e.g. only Northeast states visible when Region=East), not all 50 US states with some greyed out. Confirmed via migration-spec.json worksheet 'Prescriptive Map' filters[] -> field_id fld.ds_sample_superstore__region_filter -> Tableau formula '[Parameter 3]=\"US\" OR [Parameter 3]=[Region]'. The model's own 'Region Filter' measure (Sample Superstore.tmdl:22-24) doc comment confirms it exists exactly for this: 'report-level filter parity with the source worksheets that apply it as a worksheet filter (Callout/Bullet/Trend/Prescriptive Map/Prescriptive Scatter Plot)'. For Callout/Bullet/Trend, the equivalent restriction is already baked into the CP/PP measures themselves (no Region dimension present to filter rows on, so no visual-level filter is needed or applicable there) -- but Map needs an explicit row-removing visual filter (not just a blank/neutral color) to get the auto-zoom/crop effect, since a baked-in scalar restriction alone would leave all 50 state-shapes present just blank-colored, not cropped." }
          - { id: div_v, region: div_v, kind: rectangle, purpose: "hairline vertical divider, left/right halves", field_bindings: {} }
          - { id: scatter_slicer_yaxis, region: scatter_slicer_yaxis, kind: slicer, purpose: "Y-AXIS", field_bindings: { field: "'Y-Axis'[Y-Axis]" }, default_value: Sales }
          - { id: scatter_slicer_xaxis, region: scatter_slicer_xaxis, kind: slicer, purpose: "X-AXIS", field_bindings: { field: "'X-Axis'[X-Axis]" }, default_value: Discount }
          - { id: scatter_slicer_breakdown, region: scatter_slicer_breakdown, kind: slicer, purpose: "BREAKDOWN", field_bindings: { field: "'Scatter Plot Detail'[Scatter Plot Detail]" }, default_value: Sub-Category }
          - id: prescriptive_scatter
            region: prescriptive_scatter
            kind: scatterChart
            purpose: "diagnostic scatter, dynamic X/Y/size/detail"
            field_bindings:
              x: "'X-Axis'[X-Axis Fields] (via 'Sample Superstore'[X-Axis] measure)"
              y: "'Y-Axis'[Y-Axis Fields] (via 'Sample Superstore'[Y-Axis] measure)"
              details: "'Scatter Plot Detail'[Scatter Plot Detail Fields]"
              size: "'Sample Superstore'[Map KPI]"
              color: "'Sample Superstore'[Map KPI Difference >= 0] (boolean -- distinct sibling field from the Map's continuous Map KPI Difference)"
            visual_filter: "'Sample Superstore'[Region Filter] = TRUE -- added for exact worksheet-filter parity per Sample Superstore.tmdl:22-24 doc comment (this worksheet is explicitly listed alongside Prescriptive Map as an original recipient of the Tableau 'Region Filter' worksheet filter). Redundant-but-harmless for VALUE correctness (X-Axis/Y-Axis measures already bake in the same region restriction internally, verified same FILTER(ALL(Region),...) pattern as CP Sales/CP Discount), since Scatter Plot Detail (e.g. Sub-Category) has no per-row region dependency to crop -- applied anyway for exact source-filter parity, not because it changes the rendered result."
            color_strategy: "boolean 2-state: TRUE->favorable, FALSE->unfavorable (no gray/no-data state possible on a scatter by construction)"
            insight_basis: >
              X-Axis-Label/Y-Axis-Label worksheets DROPPED here -- relying on Power BI's native
              behavior of showing the selected Field Parameter member's display name as the axis
              title automatically. TENTATIVE pending empirical confirmation during authoring/Desktop
              screenshot review; fallback = small Card bound to 'Sample Superstore'[Y-Axis Label]/
              [X-Axis Label] measures (which DO exist in the model) if native titling doesn't render
              as expected.
          - { id: div_h3, region: div_h3, kind: rectangle, purpose: "hairline divider", field_bindings: {} }
          - { id: insight1_indicator, region: insight1_indicator, kind: shape (badge), purpose: "Insight 1 indicator", field_bindings: { text: "Insights[Insight Number]=1 (literal '1')", fill_color: "mapped from Insights[Indicator] where Insights[Insight Number]=1: Positive->favorable/teal, Neutral->neutral/gray, Negative->unfavorable/red" }, style_basis: "replicates the screenshot's PREVIEW-section numbered circle badge (1/2/3 colored by sentiment), NOT the entry-form's Positive/Neutral/Negative radio-button trio -- radio buttons are a write-parameter UI idiom with no live-editable purpose in a read-only report and are correctly not replicated." }
          - { id: insight1_text, region: insight1_text, kind: textbox (card), purpose: "Insight 1 text", field_bindings: { text: "Insights[Insight Text] where Insights[Insight Number]=1" } }
          - { id: insight2_indicator, region: insight2_indicator, kind: shape (badge), purpose: "Insight 2 indicator", field_bindings: { text: "Insights[Insight Number]=2 (literal '2')", fill_color: "mapped from Insights[Indicator] where Insights[Insight Number]=2" } }
          - { id: insight2_text, region: insight2_text, kind: textbox (card), purpose: "Insight 2 text", field_bindings: { text: "Insights[Insight Text] where Insights[Insight Number]=2" } }
          - { id: insight3_indicator, region: insight3_indicator, kind: shape (badge), purpose: "Insight 3 indicator", field_bindings: { text: "Insights[Insight Number]=3 (literal '3')", fill_color: "mapped from Insights[Indicator] where Insights[Insight Number]=3" } }
          - { id: insight3_text, region: insight3_text, kind: textbox (card), purpose: "Insight 3 text", field_bindings: { text: "Insights[Insight Text] where Insights[Insight Number]=3" } }
          - { id: footer_text, region: footer_text, kind: textbox, purpose: "attribution", field_bindings: { text: "static, same as page 1" } }
          - { id: footer_logo, region: footer_logo, kind: image, purpose: "logo", field_bindings: { source: "same as page 1" } }
        space_audit:
          content_cell_count: 8300
          placed_cell_count: 6408
          empty_cell_pct: 22.8
          largest_region_pct: 28.9
          rule_check: "22.8% empty, within reasonable range for a split-layout diagnostic page. Largest region (prescriptive_map, 28.9%) is under the 45% single-region cap. No overlaps (fixed one trivial 1-grid-unit hairline-divider overlap with map_caption during space_audit.py verification by trimming map_caption to end at col 50)."

    # =================================================================
    - role: "Super: Annotations"
      archetype: Analytical Canvas
      layout_variant: "Sparse single-column narrative list (3 consolidated insight cards)"
      variant_rationale: >
        The source dashboard itself is a simple annotation/data-entry-style page (not a dense
        analytical canvas) -- classified as Analytical Canvas only for cross-page consistency of
        the design contract, but authored intentionally sparse to match the source's own much
        lower content density (confirmed via screenshot: this is the simplest of the 3 dashboards).
      page_background: "#FFFFFF"
      layout_summary: >
        Header + Page Navigator (no slicer band -- this dashboard has none in the source).
        Instructional banner (reworded, see judgment call below). 3 consolidated insight cards
        (indicator badge + text), each larger/more prominent than a bare list row, filling the
        vertical space previously split across 2 redundant sections in the source. Footer.
      judgment_calls:
        - >
          CONSOLIDATION: source has both a live "entry form" section (3x labeled text-entry boxes,
          each followed by a literal Positive/Neutral/Negative RADIO-BUTTON trio -- a write-parameter
          UI surface, confirmed via screenshot review of the actual dashboard) and a separate
          read-only "PREVIEW" section (3x compact rows: a colored circular badge numbered 1/2/3,
          colored by sentiment, plus the insight text). Both sections show the SAME 3 insights and
          would render BYTE-FOR-BYTE IDENTICAL content in Power BI (both are necessarily read-only
          here, and Power BI has no live radio-button-as-data-entry control to bind to a static
          value meaningfully) -- consolidated into ONE section. CORRECTED during skeleton review:
          the adopted visual style is the PREVIEW section's compact colored-numbered-badge + text
          row (enlarged and given a bordered-card treatment for visual prominence to fill the space),
          NOT the entry-form's radio-button trio -- replicating literal Positive/Neutral/Negative
          radio buttons with no live binding would be non-functional UI clutter implying an
          interactivity that doesn't exist, exactly the same category of problem as the "click to
          highlight" fields. Full text/indicator fidelity is preserved (badge text = Insight Number,
          badge color = Indicator mapped to the report's favorable/neutral/unfavorable palette).
          This also directly addresses this page's otherwise-high (55.9% pre-consolidation)
          empty-space measurement by giving the 3 real content cards more room instead of splitting
          it across 2 redundant copies.
        - >
          BANNER TEXT: source instructional text is "Enter your insights here to have them appear on
          the prescriptive dashboard" -- actively misleading in a read-only report (there is no
          "entering" possible). Reworded to "Key insights for this reporting period (read-only in
          this report -- originally a live-editable entry form in Tableau; see build notes)" --
          a necessary content correction, not a style preference, since the literal original text
          describes a capability that no longer exists.
      layout_contract:
        canvas: { width: 1920, height: 1080 }
        grid: { columns: 100, rows: 100 }
        regions:
          header_bg: [1, 1, 101, 8]
          title: [2, 2, 46, 6]
          nav: [57, 1, 101, 8]
          instr_banner: [3, 11, 97, 17]
          div_h1: [1, 21, 101, 22]
          card1_indicator: [4, 26, 9, 42]
          card1_text: [10, 26, 96, 42]
          card2_indicator: [4, 46, 9, 62]
          card2_text: [10, 46, 96, 62]
          card3_indicator: [4, 66, 9, 82]
          card3_text: [10, 66, 96, 82]
          footer_bg: [1, 94, 101, 101]
          footer_text: [2, 95, 78, 100]
          footer_logo: [95, 95, 101, 100]
        placements:
          - { id: title, region: title, kind: textbox, purpose: "Page title", field_bindings: { text: "static, same as page 1" } }
          - { id: nav, region: nav, kind: pageNavigator, purpose: "shared nav", field_bindings: {} }
          - { id: instr_banner, region: instr_banner, kind: textbox, purpose: "reworded instructional banner", field_bindings: { text: "'Key insights for this reporting period (read-only in this report -- originally a live-editable entry form in Tableau; see build notes)'" } }
          - { id: div_h1, region: div_h1, kind: rectangle, purpose: "hairline divider", field_bindings: {} }
          - { id: card1_indicator, region: card1_indicator, kind: shape (badge, bordered card style), purpose: "Insight 1 indicator (consolidated entry+preview, Preview-section badge style)", field_bindings: { text: "Insights[Insight Number]=1 (literal '1')", fill_color: "mapped from Insights[Indicator] where Insights[Insight Number]=1" } }
          - { id: card1_text, region: card1_text, kind: textbox (bordered card style), purpose: "Insight 1 text (consolidated)", field_bindings: { text: "Insights[Insight Text] where Insights[Insight Number]=1" } }
          - { id: card2_indicator, region: card2_indicator, kind: shape (badge, bordered card style), purpose: "Insight 2 indicator", field_bindings: { text: "Insights[Insight Number]=2 (literal '2')", fill_color: "mapped from Insights[Indicator] where Insights[Insight Number]=2" } }
          - { id: card2_text, region: card2_text, kind: textbox (bordered card style), purpose: "Insight 2 text", field_bindings: { text: "Insights[Insight Text] where Insights[Insight Number]=2" } }
          - { id: card3_indicator, region: card3_indicator, kind: shape (badge, bordered card style), purpose: "Insight 3 indicator", field_bindings: { text: "Insights[Insight Number]=3 (literal '3')", fill_color: "mapped from Insights[Indicator] where Insights[Insight Number]=3" } }
          - { id: card3_text, region: card3_text, kind: textbox (bordered card style), purpose: "Insight 3 text", field_bindings: { text: "Insights[Insight Text] where Insights[Insight Number]=3" } }
          - { id: footer_text, region: footer_text, kind: textbox, purpose: "attribution", field_bindings: { text: "static, same as page 1" } }
          - { id: footer_logo, region: footer_logo, kind: image, purpose: "logo", field_bindings: { source: "same as page 1" } }
        space_audit:
          content_cell_count: 8300
          placed_cell_count: "~3900 post-consolidation (up from 3664 pre-consolidation once card regions were enlarged to fill the space freed by removing the duplicate section)"
          empty_cell_pct: "~53 (still well above the generic <=15% guidance)"
          rule_check: >
            DELIBERATE, clearly-flagged deviation: the source Annotations dashboard is genuinely
            sparse by original design (a 3-row annotation list, not a dense analytical page) --
            forcing artificial density here (decorative filler, oversized fonts, invented content)
            would reduce fidelity, not improve it. Documented rather than "fixed."
```
