# Tableau → Power BI Migration: Capabilities & Limitations

**Answers the question**: "Are there AI tools that can help migrate dashboards from Tableau to
Power BI, and what are their limitations?"

This is a grounded, evidence-based answer, not a generic claim. Most examples below come from actual
end-to-end runs of this toolkit against **16 real, publicly available Tableau Public workbooks** (every
folder under `examples/`), ranging from a 7-worksheet KPI dashboard to a 91-worksheet enterprise
navigation app, plus IronViz infographics and custom-geometry charts. The known-limits section also
cites later field evidence from a 44-unit customer estate where the hardest report-layer migrations
exposed gaps the public examples did not. Where a specific behavior was observed on a specific
workbook or unit, that source is cited so the claim is checkable.

## What the pipeline does automatically

1. **Structural extraction (deterministic, reliable).** `scripts/parse_tableau.py` captures every data
   source, field, calculated-field formula, worksheet encoding, dashboard layout element, and reference
   line from the raw `.twb` XML into a normalized `migration-spec.json`. This runs with zero manual
   effort on all 16 workbooks and is covered by a 48-test `pytest` suite, so it is the reproducible
   foundation the fuzzy AI steps build on.
2. **Real data extraction from `.hyper` extracts.** `scripts/extract_hyper_data.py` pulls actual row
   data out of packaged extracts via `tableauhyperapi`, so a migrated model shows real numbers rather
   than just a correct-looking empty shell (used, for example, on `eea-urban-adaptation`).
3. **Calculated-field translation to DAX, including the hard cases.** The `pbi-semantic-builder` agent
   translates Tableau formulas to DAX measures, calculated columns, or Power Query reshapes, guided by
   `docs/tableau-dax-translation-guide.md`. This is no longer just string logic and conditionals:
   - **FIXED LOD expressions** are exercised and translated (for example the per-shipment profit-ratio
     FIXED LOD in `shipping-kpis`, and LOD logic in `health-tracker`).
   - **Table calculations** are exercised and translated (LOOKUP first/last and running INDEX in
     `tale-of-100-entrepreneurs`; WINDOW/RANK-style quad-axis calculations in `quadruple-axis-charts`).
   - **What-If parameters** become native Power BI what-if parameters (three of them driving a live
     Sales-vs-Compensation calculator in `sales-commission-model`).
4. **Recognizing and upgrading Tableau workarounds, not just transliterating them.** The AI simplifies
   Tableau-specific tricks into more idiomatic, more capable Power BI equivalents:
   - Tableau's classic "scatter point + reference lines" fake-gauge trick was upgraded to Power BI's
     native Gauge visual in `eea-urban-adaptation` (a fidelity improvement, not a workaround).
   - A recurring "select one value" parameter workaround collapsed into a single native slicer, and
     cross-tab pivot re-derivation was moved into Power Query reshaping instead of brittle DAX
     string-parsing (both in `eea-urban-adaptation`).
   - Bucketed conditional coloring (GOOD/OK/BAD in `shipping-kpis`, 4-bucket quota attainment in
     `sales-commission-model`) is reproduced with Power BI conditional formatting.
5. **Reusing verified PBIR JSON instead of guessing.** The `pbi-report-builder` agent draws on the
   `.github/pbi.kb/` visual cookbook (27 known-good `visual.json` templates harvested from real
   migrations) so structurally unusual visuals are built from proven encodings rather than
   invented ones.
6. **Preparing the model for AI/Copilot.** The
   [`powerbi-ai-readiness`](../.github/skills/powerbi-ai-readiness/SKILL.md) skill.
   `check_ai_readiness.py` reports description coverage across tables, columns, and measures and flags
   categorical columns that do not enumerate their domain values; `set_ai_instructions.py` stamps the
   model's AI instructions into its culture TMDL and forces `qnaEnabled: true`. Getting to near-100%
   coverage is a required final phase of `pbi-semantic-builder`, so the generated model can answer
   Power BI Copilot / natural-language questions. What that does **not** prove is covered in the
   skill's "Evidence and limits": the instructions provably survive publish, but "Copilot obeys them"
   is unverified, and consumption additionally needs a post-deploy refresh.
7. **Full traceability.** Every parser decision, translation choice, and simplification is recorded in a
   structured `limitations_encountered` list. Nothing is silently guessed.

## What needs human validation, every time

The most important finding across all 16 runs: **structural validation is necessary but nowhere near
sufficient.** A file that opens cleanly in Power BI Desktop can still show the wrong number. Two
structurally different validation passes are needed, and each catches bugs the other cannot. This is
why the pipeline has a dedicated, read-only `pbi-migration-validator` agent whose only job is to
critique the built report against the Tableau original.

### Pass 1: "Does it open?" (file-format mechanics)

Hand-authoring Fabric TMDL (rather than going through the modeling MCP or Desktop's native save path)
surfaces issues that only appear when the file is actually opened, not from reading it:

- Placeholder values left in the `.pbip` schema version.
- TMDL indentation/formatting errors that Desktop's parser rejects.
- Multi-line DAX expression formatting Desktop will not accept.
- Measures whose names collide with their underlying columns (a Tabular naming-uniqueness rule that
  only surfaces on model commit).

These are file-format mechanics, not migration-logic problems. A "does the PBIP open without crashing"
check catches this class quickly. The `pbi-migration-validator` and the official
`powerbi-report-author validate` step handle the structural layer, but see the warning below: they pass
many defects that only surface with data in Desktop.

### Pass 2: "Is it right?" (figure-by-figure fidelity)

A systematic, figure-by-figure comparison against the source Tableau dashboard, checking both the
visual and the underlying numbers via live DAX queries, finds real bugs a clean open never surfaces.
Concrete classes seen across migrations:

- **Format-scale bugs that silently multiply by ~100x.** A percentage-scale measure (for example a
  value of `12.83` meaning "12.83%") given a `0.00%` display format shows the wrong number and throws
  no error. Easy to introduce when Tableau bakes the `* 100` or `/ 100` into the formula while Power BI
  keeps formatting separate (seen in `eea-urban-adaptation`).
- **Wrong or redundant field projection.** A table visual projecting an extra/wrong field, crowding out
  the value column it actually needed (seen in `eea-urban-adaptation`).
- **Systematic DAX-pattern bugs at scale.** In `airline-alliance-activity` (91 worksheets, 4 pages,
  108 measures), 58 comparison measures used the illegal compact filter `'Table'[Col]=[Measure]`. It
  deserializes fine and only fails in Desktop; the fix was hoisting each to a VAR. One structural
  mistake repeated 58 times is exactly what a figure-by-figure pass with live queries catches.
- **Source-workbook quirks that must be preserved, not "fixed."** A duplicated/unreachable branch in
  one EEA source formula, and the Expected-minus-Actual delay convention in `shipping-kpis`, were
  reproduced faithfully and flagged back rather than silently corrected. Whoever owns the source
  workbook should decide whether those are intentional.

**Practical implication:** "the model loaded and the report renders" is not a fidelity check. The
reliable way to catch scaling/format/field-projection bugs is to pick a concrete filter value (one
city, one shipment, one company), open the original and the migrated report side by side, and compare
every visible number. Doing this with more than one independent reviewer (or model) surfaces more than
a single pass does.

> **Structural validation is necessary, not sufficient.** `powerbi-report-author validate` and TMDL
> deserialization pass many defects that only appear in Desktop with data (field-parameter
> `sourceColumn` bracketing, the `'Table'[Col]=[Measure]` error above, flat-lined trend measures). A
> page is not "done" until it is verified in Desktop against real data.

## Known limitations and honest gaps

- **Tableau Sets do not translate at all, in any form** (measured on canonical engine **2.339.0**,
  2026-09-01). Every set form we could find is logged as `could not resolve field '<name>' (skipped)`
  and then **the visual is emitted anyway, without the filter** — so the output renders confidently
  over an unfiltered superset. Confirmed on a real Tableau training sample (`Section 08 - Organizing
  Data`) for four distinct forms: a manual/lasso set, a **condition** set (`SUM([Score]) > 400`), a
  **top-N rank** set, and a **combined** set (`intersection` of two set references). A set used as a
  `<color>` encoding is dropped just as silently — the emitted `scatterChart` lost its In/Out series
  split entirely. Reproduction fixture: `tests/fixtures/issue-185-set-filter.twb`; filed upstream as
  [`Yarbrdab000/tableau-fabric-skills#185`](https://github.com/Yarbrdab000/tableau-fabric-skills/issues/185).
  ⚠️ **Sets are hard to find with a naive grep, and this project produced THREE wrong counts in a row
  before getting it right.** There is no `class='set'` attribute (count: 0). Naming the set literally
  (`[Set 1]`) finds only sets a user never renamed (count: 4 workbooks). The correct marker is the
  authoring attribute on the group: **`user:ui-builder='filter-group'`** (condition / top-N sets) or
  **`'lasso-group'`** (manual sets). `.twbx` are ZIP archives and must be extracted, or a `.twb`-only
  sweep misses them entirely. Measured correctly, with controls:

  ```
  assets scanned                              137
  files containing <group>                     51
  files with a SET marker                      23   -> 12 DISTINCT workbooks
  NEGATIVE CONTROL: <group> but no set marker  28
  POSITIVE CONTROL: a known set-bearing file   FOUND
  set kinds: top-n 32 | membership 27 | condition 13
  ```

  ⚠️ **Set kind matters when reporting a defect:** "sets are dropped" and "top-N sets are dropped" are
  different claims. All three kinds are dropped, verified on three unrelated workbooks — but a count of
  set *markers* is not a count of *defects*: `Airline Alliance` carries 10 markers and only 2 warn,
  because only sets actually referenced by a view are ever resolved.
- **A zero is not a measurement unless you state the positive control that proves the predicate can
  see what it is looking for.** Four false zeros were produced here in one day: the `class='set'` sweep
  above; a `[Set N]` sweep that undercounted 12 workbooks as 4; a shell function-scope bug that
  silently returned an empty array; and a `Conditional.Cases` search that returned 0 against the
  engine and was used to infer the engine cannot emit conditional fills — it emits **13 of them** into
  a public workbook, because `Conditional.Cases` is *path notation in prose* and the serialized form is
  nested keys `{"Conditional": {"Cases": [...]}}`. Every count in this repo should name its positive
  control beside it, and a corpus too simple to exhibit the effect is a control failure too: our first
  "0 visuals under 20px tall" was measured on single-zone workbooks, and a real multi-zone dashboard
  produced **14**, the smallest at **12.56px**.
- **The PBIR height floor covers `slicer` and `textbox` only, so sub-renderable CHART visuals pass
  validation silently.** Measured on engine 2.339.0 against the public `Airline Alliance Activity
  Dashboard _ #VOTD.twbx`: `powerbi-report-author` 0.1.4 raised 6 errors, all
  `PBIR_SLICER_HEIGHT_BELOW_FLOOR` / `PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR` — while a **12.56px**
  `clusteredColumnChart` (and 13 more chart visuals under 20px tall, 64 under the 76px slicer floor,
  22 under 150px wide) raised **nothing**. So "validate passed" does not mean "the visuals can be
  seen"; this is the concrete, named instance of the general rule that structural validation is
  necessary but not sufficient. Reported upstream on
  [#186](https://github.com/Yarbrdab000/tableau-fabric-skills/issues/186); related to
  [#180](https://github.com/Yarbrdab000/tableau-fabric-skills/issues/180) (slicers regressed to 57/62px
  against a 76px floor).
- **An explicit `mark class='Bar'` supports strictly fewer shelf layouts than `mark class='Automatic'`**
  (engine 2.339.0). `twb_to_pbir.py::_visual_type` accepts `bar` for exactly two layouts
  (dimension-on-cols + measure-on-rows, or dimension-on-rows + measure-on-cols); `automatic` reaches
  five further fallbacks (scatter, matrix, table, column, continuous-date line). So changing a mark
  from Automatic to Bar in Tableau — cosmetic there — can silently cost the whole visual here
  (`mark class 'Bar' / shelf layout not supported -> no visual emitted`, `zone left empty`). A raw
  count of `Bar` marks proves nothing: they are common and mostly work. Controlled A/B fixture:
  `tests/fixtures/issue-185-bar-shelf-layout.twb`.
- **Origin-destination and line maps are the hardest surface, and not all are verified yet.** Tableau's
  MAKELINE great-circle arc has no native Power BI equivalent, so `airline-alliance-activity` uses
  destination bubbles instead of arcs (an honest downgrade, documented). Two map-heavy renders,
  `telecommunications-analytics` (a two-point route/line map) and `superstore-sales-performance`
  (Azure Maps choropleths), are being re-rendered and are **not yet render-verified**; treat them as
  in progress rather than proven.
- **LOD and table-calc grain must be checked per field.** These now translate automatically (see
  above), but grain and filter-context assumptions still have to be verified against known Tableau
  output before the translated measure is trusted; the automation gets you a first draft, not a
  guarantee.
- **IronViz infographics and custom geometry parse and build, but render capture may lag.**
  `broadway-stage-to-screen` (IronViz), `spiraling-satellites` (custom spiral geometry) and
  `fast-fashion-impact` all produce a semantic model and report, but their Power BI Desktop render
  verification is still pending, so they are marked built-but-not-yet-render-verified.
  (`wind-energy-utilization` IS render-verified - its committed before/after pair is
  `docs/showcase/assets/wind-energy-utilization-1.png` - and the README lists it as such.)
- **Extract-based data sources with no live upstream** migrate structurally, but real row data requires
  the separate `.hyper` extraction step (or repointing to a true upstream if one exists behind the
  extract).
- **Report-layer fidelity is the biggest variable, not just "visual polish."** On the hardest field
  units, the customer did not elect to rebuild the semantic models, but that does not mean the model
  tier is risk-free: the same field record includes unresolved model defects such as stubbed
  `BLANK()` measures and missing reference columns. The report layer is where manual rework was chosen
  before customer rollout. In a later 44-unit SES estate, the two hardest units exposed this tail risk:
  [**IA CAPS Dashboard**](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/342)
  shipped provisional after retry validation found **3 FAIL / 8 PARTIAL of 11 pages**, including two
  hard-crash pages, and the customer chose to have their team manually repair the visual layer;
  [**IA IPTV Dashboard**](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/342)
  was baseline **FAIL / NOT READY with 22 findings**. Other measured failure modes include wrong chart
  types for Tableau idioms, field-parameter swaps that validate but do not engage, bindings that
  validate but render against the wrong field, and pages that fail outright in high-page-count
  workbooks. This is the tail of an estate rather than the median outcome, and the available evidence
  does not yet prove which part of "not great" on CAPS was chart choice, data binding, layout, or
  formatting, so treat the report layer as a first draft that needs explicit fidelity validation.

## Bottom line

AI-assisted migration turns a multi-week, worksheet-by-worksheet manual rebuild into an automated first
draft plus a structured, evidence-based validation pass, not a rubber stamp. Across 16 workbooks the
pipeline generated the semantic models and reports with no hand-written DAX and no hand-built layout,
including FIXED LOD expressions, table calculations, what-if parameters, and a 91-worksheet enterprise
workbook. But getting from "generated" to "trustworthy" still takes two distinct validation rounds
(file mechanics, then figure-by-figure fidelity), and some visuals (great-circle line maps, choropleths,
IronViz custom geometry) still need a human to confirm or finish. That is the honest ratio: hours of
automated build plus a real, structured review versus weeks of fully manual rebuild. The value is in
shifting human effort from rebuilding to reviewing, and the reviewing step has to be taken seriously for
the output to be trustworthy.
