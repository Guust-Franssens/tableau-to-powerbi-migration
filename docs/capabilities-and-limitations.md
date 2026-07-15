# Tableau → Power BI Migration: Capabilities & Limitations

**Answers the question**: "Are there AI tools that can help migrate dashboards from Tableau to
Power BI, and what are their limitations?"

This is a grounded, evidence-based answer — not a generic claim. Everything below comes from an
actual end-to-end run of this toolkit against a real, publicly available Tableau workbook: the EEA
(European Environment Agency) "Urban Audit city factsheets — Urban Adaptation Map Viewer"
(16 worksheets, 7 data sources, 152 fields, 46,225 rows of real data). See `migrations/eea-urban-adaptation/`
for the full worked example.

## What was built

1. **Deterministic parser** (`scripts/parse_tableau.py`) — extracts every data source, field,
   calculated-field formula, worksheet encoding, dashboard layout, and reference line from the raw
   `.twb` XML into a normalized `migration-spec.json`.
2. **Data extraction** (`scripts/extract_hyper_data.py`) — pulls real row data out of the workbook's
   packaged `.hyper` extracts via `tableauhyperapi`, so the migrated model isn't just structurally
   correct but actually shows real numbers.
3. **AI semantic-model builder** (`pbi-semantic-builder` agent) — translates all 34 calculated fields
   to DAX (measures, calculated columns, or Power Query reshapes as appropriate), builds the star
   schema, and produces a full Fabric TMDL semantic model.
4. **AI report builder** (`pbi-report-builder` agent) — maps all 16 Tableau worksheets to Power BI
   visuals (including upgrading Tableau's fake-gauge trick to native Gauge visuals) and reproduces
   the original dashboard layout as a PBIR report.
5. **Verified end-to-end**, in two distinct validation passes (see below) — the generated PBIP opens
   in Power BI Desktop with all 7 tables, 143 columns, 32 measures, and real data loaded, and the
   rendered visuals were checked figure-by-figure against the source Tableau dashboard.

## What this demonstrates well

- **Structural extraction is fully automated and reliable.** Every data source, field, formula,
  worksheet, and layout element was captured from the raw XML with zero manual effort.
- **The majority of calculated-field logic translates automatically.** Of 34 calculated fields, all
  34 were translated automatically — string building, conditionals, aggregations, date parsing — with
  no LOD expressions or table calculations in this particular workbook (though the pipeline has
  documented translation patterns ready for those, see `docs/tableau-dax-translation-guide.md`).
- **The AI doesn't just transliterate — it recognizes and simplifies Tableau-specific workarounds**
  into more idiomatic, more capable Power BI equivalents:
  - A recurring "select one value" parameter workaround (5 occurrences) collapsed into a single
    native Power BI slicer.
  - Cross-tab pivot re-derivation logic (11 fields) was moved into Power Query reshaping instead of
    being replicated as brittle DAX string-parsing.
  - Tableau's classic "scatter point + reference lines" fake-gauge trick (5 worksheets) was upgraded
    to Power BI's native Gauge visual — a fidelity *improvement*, not a workaround.
  - A duplicated/unreachable branch bug in one of the source workbook's own formulas was preserved
    faithfully and flagged back, rather than silently "fixed" — the right call, since whoever owns
    the source workbook should decide whether that's intentional.
- **Full traceability.** Every parser decision, AI translation choice, and simplification is logged
  in a structured `limitations_encountered` list (dozens of entries across parsing, model-building,
  report-building, and validation) — nothing is silently guessed.

## What needs human validation, every time

Two structurally different validation passes were needed, and each caught bugs the other could not.
This is the most important finding in this whole exercise.

### Pass 1 — "Does it open?" (file-format mechanics)

Building a Fabric semantic model by hand-authoring TMDL (rather than through the Power BI Modeling
MCP server or Desktop's native save path) surfaced issues that only appear when the file is actually
opened in Power BI Desktop — not from reading the file:

- A placeholder value left in the `.pbip` schema version.
- A TMDL indentation formatting error in the database definition.
- Multi-line DAX expression formatting that Desktop's parser rejected.
- Measures whose names collided with their underlying columns (a Tabular naming-uniqueness rule
  that only surfaces on model commit, not on static review).

All of these are *file-format mechanics* problems, not *migration-logic* problems — the underlying
DAX translations and visual mappings were structurally correct. A "does the PBIP open without
crashing" check catches this class of bug quickly.

### Pass 2 — "Is it right?" (figure-by-figure fidelity)

Passing Pass 1 is necessary but **nowhere near sufficient**. A systematic, figure-by-figure
comparison against the source Tableau dashboard — checking both the visual and the underlying
numbers via live DAX queries — found real bugs that a clean Desktop open did not surface:

- A table visual was projecting a redundant/wrong field, crowding out the actual value column it
  needed to show.
- A percentage-scale measure had a `0.00%` display format applied to values that were already
  percentage-scale (e.g. `12.83` meaning "12.83%"), inflating the displayed number by ~100x. This is
  an easy mistake to make when translating Tableau's calculated-field formatting (which often bakes
  the `* 100` or `/ 100` into the formula itself) to Power BI's separate format-string mechanism, and
  it will not throw any error — the report just silently shows the wrong number.
- Several smaller measure/text/field-reference issues (blank-check logic, cleanup of unused
  intermediate columns) that only surfaced by cross-referencing specific rendered values against
  what the source data and formulas actually said.

**The practical implication**: "the model loaded and the report renders" is not a fidelity check.
The only reliable way to catch scaling/format/field-projection bugs is to pick a concrete filter
value (e.g. one city, one row), open both the original and the migrated dashboard side by side, and
compare every visible number. Doing this with more than one independent reviewer (or model) surfaces
more issues than a single pass — cross-checking with multiple LLMs during this exercise caught
discrepancies a single reviewer missed.

### Other limitations (not exercised by this specific workbook, but documented)

- **LOD expressions and table calculations** (not present in this workbook, but common in real
  dashboards) have documented translation patterns, but grain and filter-context assumptions must be
  verified per field against known Tableau output before trusting them.
- **Extract-based data sources with no live upstream** — structure migrates automatically, but actual
  row data requires a separate extraction step (as done here via `tableauhyperapi`), or repointing to
  a true upstream system if one exists behind the extract.
- **Visual polish** — chart-type and layout mapping is automated and directionally correct, but a
  design pass (spacing, exact colors, fonts) is still worth doing before a customer-facing rollout.

## Bottom line

AI-assisted migration turns a multi-week, worksheet-by-worksheet manual rebuild into an automated
first draft plus a **structured, evidence-based validation pass** — not a rubber stamp. In this
exercise: zero manual DAX was hand-written and zero manual layout was hand-built; the entire
16-worksheet, 152-field dashboard was generated automatically. But getting from "generated" to
"trustworthy" took two distinct rounds of validation (file-mechanics, then figure-by-figure fidelity)
across multiple iterations, not a single quick check. That's the honest ratio: hours of automated
build + a real, structured validation effort vs. weeks of fully manual rebuild — the value is in
shifting human effort from *rebuilding* to *reviewing*, and the reviewing step has to be taken
seriously for the output to be trustworthy.
