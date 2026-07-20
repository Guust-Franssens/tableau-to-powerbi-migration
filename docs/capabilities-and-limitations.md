# Tableau → Power BI Migration: Capabilities & Limitations

**Answers the question**: "Are there AI tools that can help migrate dashboards from Tableau to
Power BI, and what are their limitations?"

This is a grounded, evidence-based answer, not a generic claim. Everything below comes from actual
end-to-end runs of this toolkit against **16 real, publicly available Tableau Public workbooks** (every
folder under `migrations/`), ranging from a 7-worksheet KPI dashboard to a 91-worksheet enterprise
navigation app, plus IronViz infographics and custom-geometry charts. Where a specific behavior was
observed on a specific workbook, that workbook's slug is cited so the claim is checkable.

## What the pipeline does automatically

1. **Structural extraction (deterministic, reliable).** `scripts/parse_tableau.py` captures every data
   source, field, calculated-field formula, worksheet encoding, dashboard layout element, and reference
   line from the raw `.twb` XML into a normalized `migration-spec.json`. This runs with zero manual
   effort on all 16 workbooks and is covered by a 20-test `pytest` suite, so it is the reproducible
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
   `.github/pbi.kb/` visual cookbook (26 known-good `visual.json` templates harvested from real
   migrations) so structurally unusual visuals are built from proven encodings rather than
   invented ones.
6. **Preparing the model for AI/Copilot.** `scripts/check_ai_readiness.py` reports description coverage
   across tables, columns, and measures and flags categorical columns that do not enumerate their
   domain values. Getting to near-100% coverage is a required final phase of `pbi-semantic-builder`, so
   the generated model can answer Power BI Copilot / natural-language questions.
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
  `broadway-stage-to-screen` (IronViz), `spiraling-satellites` (custom spiral geometry),
  `fast-fashion-impact`, and `wind-energy-utilization` all produce a semantic model and report, but
  their Power BI Desktop render verification is still pending, so they are marked built-but-not-yet-
  render-verified.
- **Extract-based data sources with no live upstream** migrate structurally, but real row data requires
  the separate `.hyper` extraction step (or repointing to a true upstream if one exists behind the
  extract).
- **Visual polish is a separate pass.** Chart-type and layout mapping is automated and directionally
  correct, but exact spacing, colors, and fonts still deserve a design pass before a customer-facing
  rollout.

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
