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
| `DATEDIFF('hour', start, end)` | `DATEDIFF(start, end, HOUR)` | **[seen, LogisticsLive]** **Same argument order** — only the interval moves from first to last. Guard the operands: `IF(ISBLANK([start]) \|\| ISBLANK([end]), BLANK(), DATEDIFF(...))`, because DAX reads a blank date as 1899-12-30 and returns a huge number instead of a blank. See the sign note below. |

### DATEDIFF sign — verify what the source workbook actually meant [seen, LogisticsLive]

Tableau's `DATEDIFF('hour', [actual_ship_date], [expected_ship_date])` for a field *named* **Delay
Hours** is `expected − actual`, so a **late** shipment comes out **negative**. DAX preserves this
exactly, because the operand order is identical — which is the right default (translate bug-for-bug,
then flag it), but it is invisible unless you look.

Two things worth knowing, both **verified live** (2026-08-01, Desktop `EVALUATE`):

- **DAX `DATEDIFF` does not error when `start > end`** — it returns a negative number:
  `DATEDIFF(DATE(2024,1,10), DATE(2024,1,1), HOUR)` = `-216`. Older guidance that it throws is wrong
  for current engines, so no `ABS`/swap workaround is needed and none should be added silently.
- Therefore a literal translation is safe; **report the sign convention to the customer** rather than
  "fixing" it. If they confirm the field should be positive-when-late, swap the two operands — a
  one-token change.


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

**Gotcha — never emit the compact filter `'Table'[Col] = [Measure]` (measure on the RHS) [high
severity, found and fixed in 58 Airline measures].** When you restrict a `CALCULATE` by a
parameter-selection or prior-period measure (`'Flight Activity'[Year] = [Year Parameter Value]`,
`'Flight Activity'[Month] = [PM Month Value]`), the compact boolean-filter form with a **measure** on
the right-hand side is illegal DAX. Every such measure fails **at query/render time** with `A function
'PLACEHOLDER' has been used in a True/False expression that is used as a table filter expression` — and
it is invisible to `powerbi-report-author validate` and TMDL structural validation (the whole report
renders "Something's wrong with one or more fields" only in Desktop). Hoist each measure into a `VAR`
and compare the column to the VAR (a constant scalar):

```dax
CM Costs =
VAR _year  = [Year Parameter Value]
VAR _month = [Month Parameter Value]
RETURN CALCULATE(SUM('Flight Activity'[Total Costs Usd]),
    'Flight Activity'[Year] = _year, 'Flight Activity'[Month] = _month, 'Flight Activity'[Completed Flights] = 1)
```

A **literal** RHS (`'…'[Completed Flights] = 1`) and an **explicit** `FILTER(ALL('…'[Col]), '…'[Col] =
[Measure])` both already work — only the compact `column = [measure]` form is rejected.

## 5. LOD expressions [seen, Shipping — a FIXED per-shipment ratio]

| Tableau LOD | DAX equivalent |
|---|---|
| `{FIXED [Dim] : SUM([Measure])}` | `CALCULATE(SUM('T'[Measure]), ALLEXCEPT('T', 'T'[Dim]))` |
| `{EXCLUDE [Dim] : SUM([Measure])}` | `CALCULATE(SUM('T'[Measure]), ALL('T'[Dim]))` (combine with `ALLEXCEPT`/`VALUES` of other dims actually in view) |
| `{INCLUDE [Dim] : SUM([Measure])}` | Needs a finer grain first: `<OUTER>X(VALUES('T'[Dim]), CALCULATE(SUM('T'[Measure])))` — **`<OUTER>` is read off the SHELF, not the formula**; see below |

LOD expressions are the highest-risk translation category — always validate the DAX result against
a known Tableau value (via `semantic-model-consumption` EVALUATE, or a Python replica against the
extract CSV when no live engine) before trusting it. **With no Tableau site to query, try
`scripts/extract_twbx_result_cache.py <wb>.twbx` first**: a packaged `.twbx` usually carries
`TwbxExternalCache/TwbxResultsCacheV3/` — Tableau Desktop's *own* cached result tuples for the
queries it last ran, LOD queries included (the `.key` sidecar even flags `has-lod-calcs='true'`).
That is real ground truth, not a re-derivation, so it can falsify your DAX rather than merely agree
with it. Caveat to state whenever you use it: the cache holds only what Desktop happened to run
before the last save, so **absence is not evidence**.

**⚠️ The outer aggregation is a SECOND decision, and it is not in the LOD formula
[seen, `book_5-2-LOD`].** A Tableau LOD is an *expression*, not a measure: the aggregation that
collapses it to one number per mark lives on the **shelf**, as the pill's `derivation` attribute in
the `.twb` (`<column-instance … derivation='Avg' …>`), and the same LOD field is routinely dropped on
two shelves with two different derivations. So translating `{INCLUDE [Customer Name] : SUM([Sales])}`
requires reading the worksheet, not just the calculation:

```dax
-- shelf says derivation='Avg'  ->  AVERAGEX, the average customer's spend
Avg Sales per Customer = AVERAGEX(VALUES('Orders'[Customer_Name]), CALCULATE(SUM('Orders'[Sales])))
-- shelf says derivation='Sum'  ->  SUMX, which for INCLUDE is just SUM(Sales) again
```

Copying the table's old hard-coded `SUMX` here returned **2,297,200 instead of 3,349** — a ~686×
error (686 = the distinct customers in view). Nothing structural catches it: the DAX is valid, the
grain is right, the measure renders, and the number is merely *wrong*. Grep every worksheet for the
LOD's `field_id` and translate one measure **per distinct derivation**.

**Three arithmetic grain traps worth asserting on any LOD, in DAX or Python** — each is cheap and
each fails loudly when the grain is wrong, unlike eyeballing a total:

1. **`SUM(INCLUDE-LOD)` must equal `SUM([Measure])` exactly.** INCLUDE only *adds* a grouping level,
   so summing back over it is lossless. Any drift means the added grain double-counts or drops rows.
2. **`SUM(LOD) / AVG(LOD)` must be a whole number**, and that integer is the distinct count of the
   LOD's dimension in the current view (686 / 674 / 629 / 512 across four panes, exactly). A
   non-integer means the outer aggregation and the grain disagree.
3. **An EXCLUDE measure must be CONSTANT across the excluded dimension** while the plain aggregate
   varies. Put both on one visual and check the spread is `0.000000`, not "looks flat".

Note trap 2's corollary: an INCLUDE LOD summed across four regions can legitimately exceed the
workbook's distinct customer count (2,501 marks vs 793 customers) because a customer buying in two
regions is counted in both. "Fixing" that back to 793 silently destroys the grain — the inflated
number is the correct one.

**A keyword-less `{SUM([Sales])}` is still an LOD** — it is a FIXED at the *empty* grain, i.e. a
grand total (`CALCULATE(SUM('T'[Sales]), ALL('T'))`), and it is easy to misread as an ordinary
aggregate. Parser-side coverage: `Yarbrdab000/tableau-fabric-skills` issue #49.

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
| `RUNNING_SUM(SUM([Sales]))` | `VAR _asOf = MAX('T'[Date]) RETURN CALCULATE(SUM('T'[Sales]), FILTER(ALL('T'[Date], 'T'[Month]), 'T'[Date] <= _asOf))` — list **every** date-ish column of `'T'` in the `ALL()`; see the axis-grain warning below |
| `INDEX()` (1-based running position in a partition) | calc column `CALCULATE(COUNTROWS('T'), FILTER(ALLEXCEPT('T',[part]), 'T'[order] <= EARLIER('T'[order])))` |
| `LOOKUP(agg, FIRST())` / `LOOKUP(agg, LAST())` | hidden helper calc column `CALCULATE(agg, ALLEXCEPT('T',[part]), 'T'[Date] = CALCULATE(MIN/MAX('T'[Date]), ALLEXCEPT('T',[part])))`, then "growth of $X" measures divide by it |
| `IF MIN([Date]) = LOOKUP(MIN([Date]), LAST()) THEN <expr> END` (is-last-row guard) | `IF(MAX('T'[Date]) = CALCULATE(MAX('T'[Date]), ALLEXCEPT('T',[part])), <expr>)`; OR-FIRST variant adds `\|\| MAX(...) = CALCULATE(MIN(...), ...)`; Tableau `END`-without-`ELSE` → omit the DAX else (BLANK on non-endpoint rows) |
| `% of Total` / `pcto:` table calc | `DIVIDE([m], CALCULATE([m], ALLSELECTED('T')))` (verify addressing/partitioning against Tableau when a live engine exists) |

**Validate every table calc two independent ways in Python** (Tableau semantics via sorted-partition
`.iloc`/`cumcount`, and a literal DAX-mechanics replica via boolean masks over the raw table); two
independent codings agreeing is far stronger than restating one formula.

**Running totals break silently when the visual's axis is coarser than the accumulation column
[seen, LogisticsLive]:** the familiar `CALCULATE(SUM('T'[v]), FILTER(ALL('T'[Date]), 'T'[Date] <=
MAX('T'[Date])))` clears only `'T'[Date]`. Put a coarser column of the **same table** on the axis —
typically the month-start column you added so the report could reproduce a Tableau `mn`/`tmn` bin — and
that column's filter survives `ALL('T'[Date])` and still restricts the fact rows, so each point shows
**that bucket's own total** instead of the cumulative one. The line looks plausible (it rises and falls
like a normal series) and every structural check passes. Name every date-ish column in the `ALL()` and
pin the as-of value in a `VAR`. Ground-truthed on a 730-row/24-month table: the old form returned exactly
the month total for all 24 months; the new form accumulated to the CSV grand total to the cent; and at the
original **daily** grain the two forms differed on **0 of 730 days**, i.e. the fix is a strict superset.

**Date-part shelf derivations (`mn`, `tmn`, `qr`, `yr`) are a MODEL-layer job, not a report one
[seen, LogisticsLive]:** Power BI cannot bin a date to month in PBIR when the model has no date table
and `__PBI_TimeIntelligenceEnabled = 0`. Emit a date-typed month-start calculated column per table —
`IF(ISBLANK('T'[D]), BLANK(), DATE(YEAR('T'[D]), MONTH('T'[D]), 1))`, `dataType: dateTime`,
`formatString: mmm yyyy` — never a text label (alphabetical sort scrambles the axis) unless you also set
`sortByColumn`. Keep it **per table** when the sources are independent (`joins: []`): a shared calendar
would invent relationships Tableau never had. And record which reading you implemented: `mn` is strictly
the MONTH *date part* (cycles Jan–Dec across years), `tmn` the truncated month; a multi-year trend line
means month-start.


**b − a**. A source workbook with swapped arguments silently makes "late" durations negative and
inverts any threshold KPI built on it. Translate exactly as authored and **flag it to the customer**
as a probable source bug — don't silently "fix" it.

**Color-encoding fidelity loss [seen, Airline]:** a Tableau "color helper" field that returns an
indicator *string* (e.g. `… Circle Col` → "Up"/"Down") cannot drive a Power BI data-color rule (those
need a numeric/categorical driver on the visual). Such series render single-color; note the fidelity
loss rather than forcing it.

### 6a. ⚠️ An inline metric inside an X-iterator needs ONE EXTRA `CALCULATE` [seen, Sales & Customer Dashboards]

This is the highest-cost defect class in this guide: it **compiles, returns plausible numbers, and is
silently wrong**. Only a per-axis-point ground-truth comparison catches it.

`CALCULATE` evaluates its **filter arguments in the OUTER filter context**, before applying its own
context transition. So when you iterate an axis and inline the metric:

```dax
-- WRONG: returns the ANNUAL total for every month
MAXX(VALUES('Date'[Month No]),
     CALCULATE(DISTINCTCOUNT('Orders.csv'[Customer_ID]),
               FILTER('Orders.csv', YEAR('Orders.csv'[Order_Date]) = _y)))
```

the `FILTER('Orders.csv', …)` never sees the iterated month — and, being a table filter on the fact
table, it then *overrides* the month filter arriving through the date relationship. Measured: every
month returned 693, the full-year distinct customer count.

```dax
-- RIGHT: one extra CALCULATE lands the context transition first
MAXX(VALUES('Date'[Month No]),
     CALCULATE(CALCULATE(DISTINCTCOUNT('Orders.csv'[Customer_ID]),
                         FILTER('Orders.csv', YEAR('Orders.csv'[Order_Date]) = _y))))
```

Measured after the fix: 216 max / 53 min per month, matching the CSV exactly.

**Why a measure reference doesn't need this.** `[My Measure]` inside an iterator expands to
`CALCULATE([My Measure])` — you already get two nested `CALCULATE`s for free. That is why the same
logic works when factored into a measure and breaks when inlined, which makes the bug look like magic
during debugging. Rule of thumb: **if you inline a `CALCULATE(…, FILTER(fact, …))` inside `SUMX`/
`MAXX`/`MINX`/`AVERAGEX`, wrap it once more.**

**The `FILTER` is the trigger — don't cargo-cult the extra wrap** [confirmed, book_5-1-Table-Calcs].
A bare `CALCULATE(SUM('Orders'[Sales]))` inside `RANKX`/`AVERAGEX` is *pure context transition* with no
filter argument, so there is nothing to mis-evaluate in the outer context and **one `CALCULATE` is
correct**. Both table calcs in that workbook verified exact against source ground truth with a single
wrap. Apply this section when you see a `FILTER(<fact>, …)` (or any table-filter argument) inline
inside the iterator — not to every `CALCULATE` you meet.

### 6b. Window (WINDOW_MAX/MIN/AVG) calcs with no addressing spec

> ⚠️ **Scope — check BOTH preconditions before using this recipe** [added after book_5-1-Table-Calcs,
> where it was wrong on both measures in the workbook]. The whole-partition `ALLSELECTED()` form below
> is faithful **only** when:
>
> 1. **The Tableau call has NO offset arguments.** `WINDOW_AVG(SUM([Sales]))` is whole-partition;
>    `WINDOW_AVG(SUM([Sales]), -N, 0)` is a *trailing N+1 window* and needs §6b-bis instead. Measured
>    cost of getting this wrong: the `ALLSELECTED()` form returned **1 distinct value across all 48
>    months — a flat line — max error 38,479.96** against the same workbook's real trailing average.
> 2. **The calc is genuinely order-invariant.** `WINDOW_MAX`/`MIN`/`AVG` are; `RANK`, `RUNNING_*`,
>    `INDEX`, `LOOKUP` and `PREVIOUS_VALUE` are **not** — for those, addressing *is* the semantics and
>    must be read off the workbook, never defaulted.

Tableau records addressing only when it is non-default: if the `.twb` has **zero** `compute-using`,
`addressing`, `partitioning`, `scope-isolation` **and `ordering-field`** elements, every `<table-calc>`
is at the default **Table (across)** — one partition over the whole pane.

> ⚠️ **`ordering-field` belongs in that list and is easy to miss — it is stored as an ATTRIBUTE of
> `<table-calc>`, not as a child element**, so an element-only scan reports "no addressing spec" on a
> file that plainly has one. Measured, `book_5-1-Table-Calcs`: the four element markers counted
> **0 / 0 / 0 / 0**, yet the view-level `<table-calc ordering-field='[federated…].[Sub-Category]'
> ordering-type='Field' />` (`.twb:430`, `:473`) sets "Compute Using → Sub-Category". Taking the
> element count at face value would have ranked **across months instead of across sub-categories** —
> a wrong answer that still returns one rank-1 per row and looks entirely plausible.
> Note also that a **datasource-level** `<table-calc ordering-type='Rows'/>` (`.twb:375`) can be
> **overridden per view** — read the worksheet's own element, not just the datasource default.

The `ordering-type` attribute records the layout direction (it flips `Rows`→`Columns` when the axis
pill moves shelves), so it is irrelevant to order-invariant `WINDOW_MAX`/`MIN`/`AVG` — **but that is a
statement about order-invariant calcs only**, not a general claim that `ordering-*` never carries
evaluation order (see the box above).

The faithful DAX is a no-arg `ALLSELECTED()` window over the axis column, which keeps the window
responsive to slicers while ignoring the axis point:

```dax
Min/Max Sales =
VAR _mx = CALCULATE(MAXX(VALUES('Date'[Month No]), CALCULATE([CY Sales])), ALLSELECTED())
VAR _mn = CALCULATE(MINX(VALUES('Date'[Month No]), CALCULATE([CY Sales])), ALLSELECTED())
VAR _v  = [CY Sales]
RETURN IF(_v = _mx, "Max", IF(_v = _mn, "Min"))
```

**Prove it, don't assert it:** evaluate over the axis and check that **exactly one** point is `Max`
and one is `Min`, and that those points match the max/min computed independently from the source data
— then repeat with a different slicer selection to prove the window is not frozen.

### 6b-bis. Offset windows: `WINDOW_AVG(expr, -N, 0)` → `WINDOW(-N, REL, 0, REL, …, ORDERBY(…))`

[seen, book_5-1-Table-Calcs] A Tableau offset window is **axis-position relative** — "the previous N
rows *present in the view*", not a calendar interval — which is exactly what DAX `WINDOW(…, REL, …)`
expresses. Requires compatibility level **1606+** and a live Desktop to verify (measured working at
declared `compatibilityLevel: 1606`, so the older "1702+" folklore is too conservative).

```dax
Moving Average =
VAR _p = 'Time Period'[Time Period Value]      -- the migrated Tableau parameter
RETURN AVERAGEX(
    WINDOW(-_p, REL, 0, REL,
        SUMMARIZE(ALLSELECTED('Orders'), 'Date'[Month Start]),   -- see the axis-membership trap
        ORDERBY('Date'[Month Start], ASC)),
    CALCULATE(SUM('Orders'[Sales])))
```

Three things that are easy to get wrong, all measured:

- **`(-N, 0)` is a trailing `N + 1` window, not `N`** — it spans N rows back *through the current row
  inclusive*. With Tableau's default parameter value 10 that is an **11-month** average. Reproduce the
  off-by-one faithfully and say so in the measure description; do not "correct" it to N.
- **Truncate at the partition start, don't blank it.** Tableau averages row 1 against itself alone, and
  bare `WINDOW` agrees. Adding a "full window only" guard is a fidelity regression, not a safety check.
- ⚠️ **Axis-membership trap: `ALLSELECTED('Date'[Month Start])` is NOT the same axis Tableau draws.**
  A generated date table usually spans further than the fact table (here `CALENDAR(...)` ran to
  `YEAR(MAX(Ship_Date))` = 2019 giving **60 months**, while Orders covers **48**). The `ALLSELECTED` on
  the *date column* therefore renders **58 points — a 12-month phantom tail** of months Tableau never
  shows, each averaging a shrinking set of real months. `SUMMARIZE(ALLSELECTED(<fact>), <axis col>)`
  restricts the window to months that actually have marks and rendered **exactly 48 = Tableau's mark
  count**, agreeing to `0.000000000` on every real month. Both forms are identical on the real months,
  so **only a point-COUNT check catches this — a value comparison will not.**

**Prove it with a parameter sweep.** A frozen or off-by-one window looks perfectly plausible at one
setting. Evaluate every axis point at **≥2 different parameter values** and assert the results differ
(`P=2 / 10 / 30` → 3 distinct values at the same month), then check the leading edge explicitly, where
fewer than N predecessors exist.


### 6c. Two mechanical traps when landing approved DAX

- **`ADDCOLUMNS`/`SELECTCOLUMNS` extension-column references (`[@v]`) are rejected by the openability
  gate.** `[@v]` is resolved lexically by the DAX engine, not a model object, but the gate reports
  *"references [@v], which is neither a measure nor a column in the model"* and fails the run
  (`definition_of_done: FAILED`). Rewrite the window without `ADDCOLUMNS` — `CALCULATE(MAXX(VALUES(…),
  …), ALLSELECTED())` needs no extension column at all.
- **A measure name containing `]` must have it DOUBLED in every DAX reference.** A Tableau LOD caption
  kept verbatim, e.g. `{SUM([CY Sales])}`, is a legal measure *name* but referencing it naively is a
  syntax error. Correct: `'_Measures'[{SUM([CY Sales]])}]`. Worth flagging to the report builder and
  in the model's AI instructions, because nothing about the failure points at the bracket.

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
