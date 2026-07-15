# Tableau calculation → DAX translation guide

Reference playbook for the `pbi-semantic-builder` subagent. Patterns marked **[seen]** are drawn
directly from the 94 calculated fields in the EEA "Urban Adaptation" sample workbook; patterns marked
**[general]** are common in the wild but weren't present in that specific workbook — keep them here
because real-world workbooks likely will have them.

## 1. Direct expression translations

| Tableau | DAX | Notes |
|---|---|---|
| `a + b` (string concat) | `a & b` | **[seen]** `'(' + [letter] + ') ' + [Word]` → `"(" & [letter] & ") " & [Word]` |
| `IF ... THEN ... ELSEIF ... END` | `SWITCH(TRUE(), cond1, val1, cond2, val2, ..., default)` | **[seen]** see §2 |
| `CASE x WHEN a THEN 1 WHEN b THEN 2 END` | `SWITCH([x], "a", 1, "b", 2, BLANK())` | **[seen]** direct 1:1 |
| `CONTAINS(str, sub)` | `CONTAINSSTRING(str, sub)` (or `ISNUMBER(SEARCH(sub, str))`) | **[seen]** |
| `LEFT(str, n)` / `RIGHT(str, n)` | `LEFT(str, n)` / `RIGHT(str, n)` | **[seen]** identical signature |
| `REPLACE(str, old, new)` | `SUBSTITUTE(str, old, new)` | **[seen]** `REPLACE([Pivot Field Values],",",".")` → `SUBSTITUTE('T'[Pivot Field Values], ",", ".")` |
| `ISNULL(x)` | `ISBLANK(x)` | **[seen]** semantics differ subtly — DAX blank ≠ SQL NULL in all cases; verify on fields that can be `0` or `""` |
| `TRIM(SPLIT(str, delim, n))` | No 1:1 DAX. Prefer Power Query `Text.Split`/`Splitter.SplitTextByDelimiter`, or nested `MID`/`FIND` in DAX as a last resort | **[seen]** `TRIM( SPLIT( [Pivot Field Names], " ", 2 ) )` — do this in M, not DAX |
| `DATE(DATEPARSE("yyyy", str))` | `DATE(VALUE(str), 1, 1)` for year-only strings; `DATEVALUE(str)` for full dates | **[seen]** |
| `str(x)` (implicit in concatenation) | Not needed — DAX `&` auto-converts; use `FORMAT(x, "0")` for explicit control | **[seen]** |
| `ATTR(x)` | In a calculated **column** (row context) it's just `[x]`. In a **measure** (aggregated context), emulate with `IF(HASONEVALUE('T'[x]), VALUES('T'[x]), "*")` | **[seen]** used inside a row-level text-building calc, so becomes a plain column ref |
| `SUM(x) * k` | `SUM('T'[x]) * k` as a **measure** | **[seen]** `SUM([CDD_0_1])*100` → `[CDD Scaled] := SUM('T'[CDD_0_1]) * 100` |

### Worked example — CASE/WHEN [seen]

```
Tableau: case [letter]
    WHEN 'a' THEN 1  WHEN 'b' THEN 1  WHEN 'c' THEN 1
    WHEN 'd' THEN 1  WHEN 'e' THEN 1  WHEN 'f' THEN 1
    WHEN 'g' THEN 2  ...  WHEN 'f' THEN 2  ...
END
```
```dax
Letter Group =
SWITCH(
    'T'[letter],
    "a", 1, "b", 1, "c", 1, "d", 1, "e", 1, "f", 1,
    "g", 2, "h", 2, "i", 2, "j", 2, "k", 2,   -- NB: source has a duplicate WHEN 'f' (also THEN 2)
    "h", 3, "i", 3, "j", 3, "k", 3, "l", 3, "m", 3, "n", 3,
    BLANK()
)
```
`SWITCH` (like Tableau `CASE`) matches the **first** hit, so a bug-for-bug faithful translation
naturally falls out. **Flag duplicate/unreachable branches like this back to the customer** rather
than silently "fixing" them — it may be intentional, or it may be a real bug worth surfacing during
migration (this one looks like a copy-paste typo in the original workbook).

## 2. Recognized idioms — simplify, don't transliterate

**Parameter-equality single-value filter [seen, ~10+ occurrences]**
```
Tableau calc: IF [Parameters].[Parameter 1] = [NAME] THEN [NAME] END
              ... then filtered to exclude nulls, used as a worksheet slice
```
This is Tableau's workaround for "show only the row matching the selected parameter" — there's no
native "select one value and filter everything to it" primitive in classic Tableau parameters.
**Do not create the equivalent calculated column in DAX.** Power BI's native slicer (single-select
mode) on the `Name`/City dimension does this natively, with automatic cross-highlighting to every
visual on the page. Simpler *and* more capable than the original. Flag this simplification in
`limitations_encountered` (as a positive note, not a limitation) so it's visible in the migration diff.

**Pivot-derived category/date fields [seen, ~15 occurrences]**
```
Tableau calc: IF CONTAINS([Pivot Field Names],'aseline') THEN 'Baseline'
              ELSEIF CONTAINS([Pivot Field Names],'uture') THEN 'Future' END
```
The source data was cross-tab shaped (e.g. columns `UMZ_Baseline`, `UMZ_Future`, `Transport_Baseline`...);
Tableau auto-pivoted it into generic `Pivot Field Names`/`Pivot Field Values` columns, then these
calculated fields re-derive the real category by string-matching the pivoted field name. **Handle the
reshape in Power Query, not DAX**: `Table.UnpivotOtherColumns` followed by conditional columns is far
more robust than replicating `CONTAINS`/`LEFT`/`RIGHT` string parsing as calculated columns, and
performs better (reshaping is a load-time concern, not a query-time one). Fields with
`reshape_hint: "pivot_derived"` in the migration spec should route here first; only fall back to a
DAX calculated column if Power Query reshaping isn't feasible in the timeline.

## 3. LOD expressions [general — not present in the EEA sample, expect these in real-world workbooks]

| Tableau LOD | DAX equivalent |
|---|---|
| `{FIXED [Dim] : SUM([Measure])}` | `CALCULATE(SUM('T'[Measure]), ALLEXCEPT('T', 'T'[Dim]))` |
| `{EXCLUDE [Dim] : SUM([Measure])}` | `CALCULATE(SUM('T'[Measure]), ALL('T'[Dim]))` (combine with `ALLEXCEPT`/`VALUES` of other dims actually in view) |
| `{INCLUDE [Dim] : SUM([Measure])}` | Usually needs a finer grain first: `SUMX(VALUES('T'[Dim]), CALCULATE(SUM('T'[Measure])))` — treat case-by-case, verify grain matches the visual |

LOD expressions are the highest-risk translation category — always validate the DAX result against
a known Tableau value (via `semantic-model-consumption` EVALUATE) before trusting it.

## 4. Table calculations [general — not present in the EEA sample]

| Tableau | DAX equivalent |
|---|---|
| `RANK(SUM([Sales]))` | `RANKX(ALL('T'[Category]), [Sales Measure])` |
| `RUNNING_SUM(SUM([Sales]))` | `CALCULATE(SUM('T'[Sales]), FILTER(ALL('T'[Date]), 'T'[Date] <= MAX('T'[Date])))` |
| `WINDOW_SUM(SUM([Sales]), -2, 0)` | `CALCULATE(SUM('T'[Sales]), DATESINPERIOD('T'[Date], MAX('T'[Date]), -3, DAY))` (adjust window) |
| `INDEX()` | `RANKX(ALL(...), ..., , ASC)` or a surrogate row-number column, context-dependent |

## 5. Visual pattern note — reference lines → Gauge visual

Several worksheets (e.g. `CDD_0_1`) use a Tableau-specific trick: a single `Circle` mark plotted on a
fixed continuous axis, annotated with `Min`/`Max`/`Average` reference lines, to fake a gauge (classic
Tableau has no native gauge mark). **This maps directly, and better, to Power BI's native Gauge
visual** (Value / Minimum / Maximum / Target fields) — a fidelity *improvement* over the source, not
a workaround. This is a `pbi-report-builder` concern (see `powerbi-report-design`'s chart-selection
reference), noted here because it's discovered from the same `reference_lines` data the semantic
layer also touches (Min/Max/Average become the Gauge's Minimum/Maximum/Target measures).

## 6. Capabilities & limitations (what to tell the customer)

Directly answers the question that started this: *"Are there Microsoft-recommended AI tools that can
help migrate dashboards to Power BI, and what are their limitations?"*

**What this AI-assisted approach handles well:**
- Structural extraction: data sources, fields, calculated-field formulas, worksheet encodings,
  dashboard layout — all parsed deterministically and reliably from the workbook XML.
- Straightforward calculated fields: string building, conditionals, `SUM`/`AVG`-style aggregations,
  date parsing — translate to DAX with high confidence (§1).
- Recognizing and *simplifying* Tableau-specific workarounds (parameter-equality filters, pivot
  string-parsing, scatter-based gauges) into more idiomatic, often more capable Power BI equivalents,
  rather than blindly transliterating them (§2, §5).

**What needs human validation, every time:**
- **LOD expressions and table calculations** (§3, §4) — translation patterns exist, but grain and
  filter-context assumptions must be verified per field against real Tableau output.
- **Extract-based (`.hyper`) data sources with no live upstream** — structure migrates automatically;
  actual row data requires a separate extraction step (`tableauhyperapi` → Parquet, or repointing to
  the true upstream system if one exists behind the extract).
- **Visual fidelity** — chart-type and layout mapping is automated, but final polish (colors, spacing,
  exact fonts) benefits from a design pass, not a pixel-diff guarantee.
- **Any formula this guide doesn't yet cover** — flagged in `limitations_encountered` for manual
  follow-up rather than silently guessed.

**Bottom line for the demo:** AI-assisted migration turns a multi-week manual rebuild into an
automated first draft plus a focused validation pass — it does not eliminate human review, especially
for calculation-heavy dashboards, but it removes the large majority of repetitive rebuild effort.
