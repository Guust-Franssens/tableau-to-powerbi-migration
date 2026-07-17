# Tableau calculation → DAX translation guide

Reference playbook for the `pbi-semantic-builder` subagent. Patterns marked **[seen]** are drawn
directly from real migrated workbooks — the 94 calculated fields in the EEA "Urban Adaptation" sample
workbook (iteration 1), the 63 calculated fields + 15 parameters in the "Superstore Sales
Performance" sample workbook (iteration 2, source-tagged **[seen, Superstore]** below), and parser-
level structural idioms found while triaging the "Airline Alliance Activity" workbook (iteration 3,
source-tagged **[seen, Airline Alliance]**). Patterns marked **[general]** are common in the wild but
weren't present in any workbook yet — keep them here because real-world workbooks likely will have them.

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

**Click-to-highlight / cross-filter helper fields [seen, Superstore, 4 occurrences]**
```
Tableau calc: IF [Region] = [Region Parameter] THEN "CLICK TO HIGHLIGHT" ELSE NULL END
              (bound to a Detail/Tooltip shelf; a dashboard click-action keys off whether it's non-null)
```
Tableau has no native click-to-cross-highlight action — workbooks fake it with a calculated field
bound to Detail/Tooltip, gated by an `IF`, that a dashboard action then reads. **Do not build these as
TMDL columns/measures.** Power BI has native cross-visual highlighting and cross-filter-on-click with
no calculated-field workaround required — the equivalent interactivity (if the customer wants it) is
wired up by `pbi-report-builder` as a plain visual interaction or drillthrough action, not modeled as
data. Superseded, not vestigial: these fields have a real Tableau purpose, it's just one Power BI's
native capability makes unnecessary to reproduce. Flag as a positive simplification in
`limitations_encountered`, same as the parameter-equality-filter idiom above.

## 3. Field Parameters — parameter-driven measure/dimension switching [seen, Superstore, 5 occurrences]

A Tableau parameter that lets the end user pick *which measure or dimension* a worksheet plots (e.g.
a "Y-Axis" parameter listing `Sales`/`Profit`/`Quantity`/...) maps directly to Power BI's native
**Field Parameters** feature: a small calculated table where each row is
`(Label, NAMEOF(<measure or column>), Order)`, bound to a visual's field well and switched with a
slicer. This is a first-class primitive, not a workaround — prefer it over a hand-rolled
disconnected-table-plus-`SWITCH`-measure pattern whenever a parameter genuinely swaps a shelf binding.

**Not every Tableau parameter is a Field Parameter candidate — verify consumption before choosing:**
- **Field Parameter**: confirmed by checking every worksheet's shelves for the parameter feeding a
  computed field that's actually *bound to* Rows/Columns/Values, not just referenced in a filter.
- **Plain disconnected slicer table + `SELECTEDVALUE`**: the parameter is only ever read inside one or
  two measures/`IF`s (e.g. baked into a filter argument) — it never swaps a shelf binding. Building
  this as a Field Parameter is unnecessary ceremony. **[seen]** Superstore's `Region Parameter` (read
  via `SELECTEDVALUE` inside CP/PP measures' region-restriction `FILTER`, never a join key or a shelf
  swap) and `Date Comparison` (feeds one `IF` converting a text choice into a day-count offset) were
  both plain slicers, alongside 5 genuine Field Parameters (`Y-Axis`, `X-Axis`, `Map KPI`,
  `Scatter Plot Detail`, `Date Granularity`) in the same workbook.

**Gotcha — `sourceColumn` must be the BRACKETED `[Value1]`/`[Value2]`/`[Value3]`, never bare `Value1`
and never the display name [high severity, found and fixed in all 5 Superstore FP tables].** A DAX
table-constructor row like `{("Label", NAMEOF(...), Order), ...}` with 3 columns always produces
physical columns named `Value1`/`Value2`/`Value3`; in a *calculated* table each friendly column binds
to them as a **bracketed column reference**. The correct TMDL is `column 'Map KPI'` … `sourceColumn:
[Value1]`. Writing bare `sourceColumn: Value1` (no brackets) — or `sourceColumn: <FriendlyName>` —
passes both `TmdlSerializer` structural validation and `powerbi-report-author validate` (0 errors) but
does **not** bind. Power BI Desktop silently infers `Value1`/`Value2`/`Value3` (`isNameInferred`)
columns, the friendly `'Map KPI'` column never materializes, and every `'Map KPI'[Map KPI]` reference
fails ("Column 'Map KPI' … cannot be found or may not be used in this expression"); on open Desktop
also **rewrites the `.tmdl` to the inferred form**, silently deleting your friendly columns. Fix it
before the first Desktop open — it is invisible to every offline validator and only surfaces when the
model is loaded in Desktop. (The bare-name form is a natural but wrong reading of "sourceColumn must be
Value1, not the friendly name" — the missing piece is the brackets.)

## 4. Comparison-period (CP/PP) pattern → `CALCULATE` + `DATESBETWEEN` [seen, Superstore]

Workbooks with a "Current Period" (CP) vs "Prior Period" (PP) comparison are typically hand-rolled
from boolean-flag calculated fields (e.g. `Date Filter CP`/`Date Filter PP`) consumed as
`SUM(IF(...))` inside each CP*/PP* field, with the comparison window itself driven by live date
parameters and a `Date Comparison` (Prior Period vs. Prior Year) mode switch. **Translate to a real
`Date` dimension table plus `CALCULATE(<aggregation>, DATESBETWEEN('Date'[Date], <start>, <end>))`,
not a literal port of the boolean-flag mechanism** — it's simpler, composes better with visual filter
context, and DAX has no efficient equivalent of scanning a boolean helper column per row.

```dax
CP Sales = CALCULATE(SUM('Fact'[Sales]), DATESBETWEEN('Date'[Date], [Minimum Date Value], [Maximum Date Value]))
PP Sales = CALCULATE(SUM('Fact'[Sales]), DATESBETWEEN('Date'[Date], [PP Start], [PP End]))
```

For a **trend/sparkline chart** that needs CP and PP plotted on the *same relative axis positions*
(Tableau's `Date Equalizer with Granularity` shared-axis technique), a per-bucket shift works better
than a single fixed offset: `VAR _bucketStart = MIN('Date'[Date])` / `_bucketEnd = MAX('Date'[Date])`
inside `CALCULATE(..., ALL('Date'), <shifted date filter>)`, so every axis bucket (Week/Month/Quarter/
Year, itself often a `Date Granularity` Field Parameter — see §3) is independently shifted back by the
comparison offset rather than the whole visual being re-filtered once.

**Gotcha — bake region/dimension restriction into the measure itself when no shelf carries that
field [medium severity].** If a visual type has no natural place for a restricting dimension (KPI
cards, Bullet bars, Trend sparklines, Scatter plots, Maps with no Region field on any shelf), a
Tableau-style external worksheet-level filter has no report-level equivalent to bind to. Bake the
restriction directly into the default measure instead
(`FILTER(ALL('Dim'), 'Dim'[Key] = [Parameter Value] || [Parameter Value] = "<show-all sentinel>")`),
and add a parallel `(All <Dim>)` measure family for any visual (region-comparison dot-plots,
average/target reference lines) that needs the *unrestricted* per-group breakdown instead. This is
behaviorally equivalent to Tableau but architecturally different (filter baked into the measure vs.
applied externally) — flag it in `limitations_encountered` so future maintenance isn't surprised the
measure doesn't respond to a page-level Region filter the way a Tableau worksheet would.

## 5. LOD expressions [seen, Shipping — a FIXED per-shipment ratio]

| Tableau LOD | DAX equivalent |
|---|---|
| `{FIXED [Dim] : SUM([Measure])}` | `CALCULATE(SUM('T'[Measure]), ALLEXCEPT('T', 'T'[Dim]))` |
| `{EXCLUDE [Dim] : SUM([Measure])}` | `CALCULATE(SUM('T'[Measure]), ALL('T'[Dim]))` (combine with `ALLEXCEPT`/`VALUES` of other dims actually in view) |
| `{INCLUDE [Dim] : SUM([Measure])}` | Usually needs a finer grain first: `SUMX(VALUES('T'[Dim]), CALCULATE(SUM('T'[Measure])))` — treat case-by-case, verify grain matches the visual |

LOD expressions are the highest-risk translation category — always validate the DAX result against
a known Tableau value (via `semantic-model-consumption` EVALUATE, or a Python replica against the
extract CSV when no live engine) before trusting it.

**Gotcha [seen, Shipping]: use `DIVIDE`, never `/`, for a FIXED-LOD *ratio* calc column.** Real
extracts contain zero/blank denominators (Shipping had 67 shipment ids with `SUM(Pay)=0`). `DIVIDE`
returns BLANK (which `AVERAGE` then excludes, matching Tableau's overall value); a bare `/` yields
infinity/errors and corrupts the overall average. The guard is load-bearing, not cosmetic.

## 6. Table calculations [seen, Tale-of-100 — 9 real table calcs, all ground-truthed]

Prefer forms that validate at **compat 1606** (so they can be ground-truthed offline). The window
functions `OFFSET`/`INDEX`/`WINDOW` need compat **1702+ and a live Desktop** — don't ship them when
you can't verify them.

| Tableau | DAX equivalent (offline-verifiable) |
|---|---|
| `RANK(SUM([Sales]))` | `RANKX(ALL('T'[Category]), [Sales Measure])` |
| `RUNNING_SUM(SUM([Sales]))` | `CALCULATE(SUM('T'[Sales]), FILTER(ALL('T'[Date]), 'T'[Date] <= MAX('T'[Date])))` |
| `INDEX()` (1-based running position in a partition) | calc column `CALCULATE(COUNTROWS('T'), FILTER(ALLEXCEPT('T',[part]), 'T'[order] <= EARLIER('T'[order])))` |
| `LOOKUP(agg, FIRST())` / `LOOKUP(agg, LAST())` | hidden helper calc column `CALCULATE(agg, ALLEXCEPT('T',[part]), 'T'[Date] = CALCULATE(MIN/MAX('T'[Date]), ALLEXCEPT('T',[part])))`, then "growth of $X" measures divide by it |
| `IF MIN([Date]) = LOOKUP(MIN([Date]), LAST()) THEN <expr> END` (is-last-row guard) | `IF(MAX('T'[Date]) = CALCULATE(MAX('T'[Date]), ALLEXCEPT('T',[part])), <expr>)`; OR-FIRST variant adds `\|\| MAX(...) = CALCULATE(MIN(...), ...)`; Tableau `END`-without-`ELSE` → omit the DAX else (BLANK on non-endpoint rows) |
| `% of Total` / `pcto:` table calc | `DIVIDE([m], CALCULATE([m], ALLSELECTED('T')))` (verify addressing/partitioning against Tableau when a live engine exists) |

**Validate every table calc two independent ways in Python** (Tableau semantics via sorted-partition
`.iloc`/`cumcount`, and a literal DAX-mechanics replica via boolean masks over the raw table); two
independent codings agreeing is far stronger than restating one formula.

**Faithful-translation-of-source-quirks [seen, Shipping]:** `datediff('unit',[a],[b])` computes
**b − a**. A source workbook with swapped arguments silently makes "late" durations negative and
inverts any threshold KPI built on it. Translate exactly as authored and **flag it to the customer**
as a probable source bug — don't silently "fix" it.

**Color-encoding fidelity loss [seen, Airline]:** a Tableau "color helper" field that returns an
indicator *string* (e.g. `… Circle Col` → "Up"/"Down") cannot drive a Power BI data-color rule (those
need a numeric/categorical driver on the visual). Such series render single-color; note the fidelity
loss rather than forcing it.

## 7. Visual pattern note — reference lines → Gauge visual

Several worksheets (e.g. `CDD_0_1`) use a Tableau-specific trick: a single `Circle` mark plotted on a
fixed continuous axis, annotated with `Min`/`Max`/`Average` reference lines, to fake a gauge (classic
Tableau has no native gauge mark). **This maps directly, and better, to Power BI's native Gauge
visual** (Value / Minimum / Maximum / Target fields) — a fidelity *improvement* over the source, not
a workaround. This is a `pbi-report-builder` concern (see `powerbi-report-design`'s chart-selection
reference), noted here because it's discovered from the same `reference_lines` data the semantic
layer also touches (Min/Max/Average become the Gauge's Minimum/Maximum/Target measures).

## 8. Capabilities & limitations (what to tell the customer)

Directly answers the question that started this: *"Are there Microsoft-recommended AI tools that can
help migrate dashboards to Power BI, and what are their limitations?"*

**What this AI-assisted approach handles well:**
- Structural extraction: data sources, fields, calculated-field formulas, worksheet encodings,
  dashboard layout — all parsed deterministically and reliably from the workbook XML.
- Straightforward calculated fields: string building, conditionals, `SUM`/`AVG`-style aggregations,
  date parsing — translate to DAX with high confidence (§1).
- Recognizing and *simplifying* Tableau-specific workarounds (parameter-equality filters, pivot
  string-parsing, scatter-based gauges, click-to-highlight helper fields) into more idiomatic, often
  more capable Power BI equivalents, rather than blindly transliterating them (§2, §7).
- Parameter-driven measure/dimension switching (§3) and period-over-period comparison patterns (§4) —
  both translate to genuine native Power BI primitives (Field Parameters, `CALCULATE`+`DATESBETWEEN`)
  rather than needing a manual workaround port, provided each parameter's actual consumption is
  verified first (Field Parameter vs. plain slicer, §3).

**What needs human validation, every time:**
- **LOD expressions and table calculations** (§5, §6) — translation patterns exist, but grain and
  filter-context assumptions must be verified per field against real Tableau output.
- **Extract-based (`.hyper`) data sources with no live upstream** — structure migrates automatically;
  actual row data requires a separate extraction step (`tableauhyperapi` → Parquet, or repointing to
  the true upstream system if one exists behind the extract).
- **Visual fidelity** — chart-type and layout mapping is automated, but final polish (colors, spacing,
  exact fonts) benefits from a design pass, not a pixel-diff guarantee.
- **Field Parameter table constructors** (§3) — the **bracketed** `sourceColumn: [Value1]` gotcha
  passes every offline validator (structural TMDL check AND `powerbi-report-author validate`) but a
  bare `Value1` silently fails to bind in Desktop (columns inferred to `Value1`/`Value2`/`Value3`), so
  a live Desktop/Fabric round-trip is the only way to fully close this verification gap.
- **Any formula this guide doesn't yet cover** — flagged in `limitations_encountered` for manual
  follow-up rather than silently guessed.

**A durable capability-gap class, not a translation shortfall — live user-input parameters
[seen, Superstore, high severity].** Tableau supports two live end-user-input mechanisms Power BI has
no direct equivalent for:
- **Live free-text entry** bound to a visual (e.g. Superstore's 3 "Insight" text boxes feeding a
  live preview and downstream callouts). Power BI has no native writeback UI — true writeback needs
  Power Apps integration, out of scope for a like-for-like migration.
- **Live date-entry parameters** (as opposed to a date-range *slicer* over real data). Power BI
  What-if parameters are numeric-slider-only; there's no native live date-text-entry control.

Both were implemented as **static seed tables/measures** defaulted to the Tableau workbook's current
values — the downstream logic they feed (e.g. the CP/PP comparison window) remains fully dynamic and
recomputes correctly if the seed is changed via a slicer, but the specific "type a value into a live
text/date box" interaction style is lost. Call this out to the customer as its own named capability
gap, distinct from ordinary measure-translation limitations that a better prompt or more effort could
close — this one is a genuine Power BI product-surface gap, not an execution shortfall.

**A parser-level structural idiom, not a translation gap — internal relationship-model table-anchor
pseudo-columns [seen, Airline Alliance].** Tableau data sources built on the newer relationship model
(as opposed to the older join-based model) carry one synthetic `<column>` per physical table with
`datatype="table"` and an `internal_name` prefixed `[__tableau_internal_object_id__]` (its `caption`
is just the source file/table name, e.g. `airline_alliance_performance_2022_2025.csv`). This is
Tableau's internal anchor for the relationship graph, not a real, queryable field — `pbi-semantic-
builder` should exclude it from the semantic model entirely (no column/measure), the same treatment as
a vestigial field. The parser still surfaces it in `fields[]` and flags it via `limitations_encountered`
(`severity: low`) rather than silently dropping it, matching this repo's "never silently drop, always
route through limitations" discipline.

**A durable capability-gap class, not a translation shortfall — `spatial` (MAKEPOINT`/`MAKELINE`)
geometry fields [seen, Airline Alliance, high severity].** Tableau supports calculated fields with
`datatype="spatial"`, built from `MAKEPOINT(lat, lon)` (a map point) and `MAKELINE(point1, point2)` (a
line/arc between two points) — the standard idiom behind origin-destination "flight route"/network
maps (seen here driving an airline alliance route map: `Origin Point` → `Destination Point` →
`Flight Line`). Power BI has **no native DAX or Power Query equivalent** for a geometry-typed
column — there's nothing to translate a `MAKELINE` into. Options, in order of preference, are:
1. A custom/AppSource visual that natively supports origin-destination arcs (e.g. an arc/flow-map
   visual) fed by plain lat/long measure columns (the underlying `[LAT]`/`[LON]` fields the spatial
   calc references still translate normally — only the `MAKEPOINT`/`MAKELINE` wrapper itself has no
   home).
2. A reduced-fidelity fallback: plot origin and destination as two separate point layers on a native
   Map/Shape Map visual, accepting the loss of the connecting line.
3. An R/Python custom visual, if exact line rendering is a hard requirement.
Flag this to the customer explicitly as a genuine Power BI product-surface gap (like live user-input
parameters above), not something a better prompt would close.

**Bottom line for the demo:** AI-assisted migration turns a multi-week manual rebuild into an
automated first draft plus a focused validation pass — it does not eliminate human review, especially
for calculation-heavy dashboards, but it removes the large majority of repetitive rebuild effort.
