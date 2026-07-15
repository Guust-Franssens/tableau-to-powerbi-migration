# migration-spec.json — schema guide

`scripts/parse_tableau.py` parses a Tableau `.twb`/`.twbx` workbook into `migration-spec.json`, a
normalized intermediate representation. This is the contract between the deterministic parser and
the two LLM subagents (`pbi-semantic-builder`, `pbi-report-builder`). Formal schema:
[`migration-spec.schema.json`](./migration-spec.schema.json) (JSON Schema draft-07, validated by the
parser itself via `jsonschema` before it writes the file).

## Why a separate intermediate representation

Tableau's `.twb` XML is deeply nested, uses internal names that are meaningless out of context
(`[Calculation_5871029]`), and separates "what a shelf references" (a `column-instance`, e.g.
`[sum:CDD_0_1:qk]`) from "what that instance actually is" (base column `[CDD_0_1]` + `Sum`
derivation). Parsing this correctly once, in code, and handing subagents a clean normalized JSON is
far more reliable than asking an LLM to re-derive this structure from raw XML on every run.

## Top-level shape

| Key | Purpose |
|---|---|
| `parameters` | Tableau's `Parameters` pseudo-datasource — user-facing controls (e.g. the city selector) |
| `data_sources` | Tables, joins, fields (columns + calculated fields) per Tableau datasource |
| `worksheets` | One Tableau worksheet = mark type + shelf encodings + filters + reference lines |
| `dashboards` | Recursive zone tree (Tableau's actual `<zones>` layout) + cross-worksheet actions |
| `theme` | Best-effort aggregated palette — a starting point, not an authoritative theme |
| `limitations_encountered` | Appended to by every stage; becomes the capabilities/limitations writeup |

## Field-level notes (grounded in the EEA "Urban Adaptation" sample workbook)

- **Stable synthetic `id`s everywhere.** Tableau internal names collide or are opaque
  (`[Calculation_-6493909170053079029]`); every object gets a parser-assigned id so subagents
  cross-reference unambiguously.
- **`referenced_fields` + `is_lod` / `is_table_calc`.** Detected via formula pattern
  (`{FIXED...}`/`{INCLUDE...}`/`{EXCLUDE...}` for LOD; `WINDOW_*`/`RUNNING_*`/`INDEX()`/`RANK()` for
  table calcs). The EEA workbook has neither — its 94 calculated fields are all row-context
  conditional/string logic or simple `SUM(x)*100`-style aggregations — but the flags exist because
  real customer workbooks will likely have both.
- **`reshape_hint: "pivot_derived"`.** The EEA workbook's source data was cross-tab shaped, so
  Tableau auto-pivoted it into generic `[Pivot Field Names]`/`[Pivot Field Values]` columns, and ~15
  calculated fields re-derive real category/date labels by string-matching the pivoted field name
  (`IF CONTAINS([Pivot Field Names],'aseline') THEN 'Baseline'...`, `LEFT([Pivot Field Names],3)`,
  `DATE(DATEPARSE(...))`). These are flagged so `pbi-semantic-builder` handles the reshape in Power
  Query (`Table.Unpivot` + conditional columns) instead of replicating brittle string-parsing as DAX
  calculated columns — cleaner and matches where the transform actually belongs.
- **Parameter-equality filter idiom.** Recurring pattern: a calculated field
  `IF [Parameters].[Parameter 1] = [NAME] THEN [NAME] END`, then filtered to exclude nulls. This is
  Tableau's workaround for "show only the selected city." In Power BI this collapses entirely — a
  native single-select slicer on the `Name` dimension does the same job with no calculated column.
  Flagged via the filter's `note` field so the semantic builder doesn't waste a DAX measure recreating
  a workaround PBI doesn't need.
- **`reference_lines`.** Several worksheets (e.g. `CDD_0_1`) are Tableau's classic
  "scatter point + Min/Max/Average reference line" trick used to fake a gauge (Tableau has no native
  gauge mark). This maps cleanly and *better* to Power BI's native **Gauge visual**
  (Value / Minimum / Maximum / Target) — a fidelity improvement, not just a workaround.
- **`worksheets[].measure_names_values_pivot`.** Tableau's built-in "Measure Names/Measure Values"
  virtual pivot (dragging the `Measure Names` pseudo-dimension onto rows/columns/label so
  `Measure Values` can carry N real measures side by side) has no backing datasource field and no
  direct Power BI equivalent. The EEA workbook uses it on 5+ worksheets (e.g. the "Forest Fires"
  chart, which pivoted 5 raw `Fire Perc *` measures this way). Rather than leaving it as an opaque
  `UNRESOLVED:[:Measure Names]` shelf/filter reference for a subagent to reverse-engineer, the parser
  detects the idiom and resolves the real field ids straight from the accompanying "Measure Names"
  filter's member list (`{"axis": "columns", "pivoted_field_ids": ["fld....", ...], "note": "..."}`).
  `pbi-report-builder` should bind each resolved field directly onto the target visual (e.g. one
  Y-axis field per measure on a clustered column chart) instead of trying to recreate a literal pivot
  column. `pivoted_field_ids` is empty when no resolvable filter was found (e.g. the idiom is used
  purely for text-table labeling) — that case needs a manual look at the worksheet's shelves/tooltip.
- **`connection.mode`.** The EEA workbook's 7 datasources are all `.hyper` extracts (`mode: extract`,
  no live DB) — real rows must be pulled from the embedded `.hyper` file via `tableauhyperapi` (see
  `scripts/extract_hyper_data.py`). Real-world workbooks may have `mode: live` connections instead;
  the schema supports both without changing shape.

## Validation

The parser validates its own output against `migration-spec.schema.json` before writing the file —
fail fast on a malformed spec rather than handing broken input to an LLM subagent.

```bash
python scripts/parse_tableau.py <workbook>.twbx -o migration-spec.json
python -c "import json, jsonschema; s=json.load(open('docs/migration-spec.schema.json')); jsonschema.validate(json.load(open('migration-spec.json')), s)"
```
