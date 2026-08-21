---
name: powerbi-semantic-model-gotchas
description: Hard-won Power BI semantic-model gotchas - TMDL hand-authoring pitfalls that crash Desktop on open, the field-parameter sourceColumn bracket trap, the PLACEHOLDER measure-filter error, MCP/Desktop operational rules, offline model-integrity checks, and table-calculation patterns. Use before hand-authoring TMDL or DAX, and whenever a model validates clean but fails at open, refresh or render. Source-tool agnostic (Tableau, Qlik, Cognos to Power BI).
---

# Power BI semantic-model gotchas

Every entry below cost a real debugging cycle on a real migration.

**The rule that generates most of this file:** structural validation is **necessary, not sufficient** —
a green gate proves shape, never rows. `TmdlSerializer.DeserializeDatabaseFromFolder` is the same parser
Power BI Desktop uses, and it still passes models that crash on open, silently fail to bind, or throw
only at query time. Every section below is a defect class that survived a clean parse.

> **Scope note:** the mechanisms here are pure Power BI (TMDL, DAX, Tabular, the modeling MCP), so this
> folder ports to a Qlik or Cognos migration unchanged. Entries that name a source-tool idiom
> (Tableau `ATTR()`, `MAKELINE`, "Number of Records") are examples of what produced the defect, not
> prerequisites. Cross-references to `docs/` and `.github/agents/` are paths in the host repo.

## 1. Translating source fields

- **`ATTR()`** in a calculated field used at row-granularity (post city-filter, exactly one row) is
  just the column value — don't over-engineer a `HASONEVALUE`/`VALUES` pattern unless the field is
  genuinely used in an aggregated, multi-row context.
- **Duplicate/unreachable `CASE WHEN` branches** in the source formula (seen in the EEA sample - a
  duplicate `WHEN 'f'` with two different results) should translate faithfully (`SWITCH` matches first
  hit, same as Tableau `CASE`) — flag it back to the customer as a possible source-workbook bug rather
  than silently "fixing" it.
- **Reference lines on gauge-style worksheets** (`worksheets[].reference_lines` with Min/Max/Average
  labels) need their own DAX measures (e.g. `[X Min]`, `[X Max]`, `[X Target]`) since
  `pbi-report-builder` will bind them to a Power BI Gauge visual's Minimum/Maximum/Target fields —
  coordinate naming so the report builder can find them predictably (suffix pattern:
  `<base measure> Min` / `Max` / `Target`).
- **Never pattern-match a Tableau parameter/field's internal name (`internal_name`) to infer its
  meaning — always use the parser-resolved `caption`.** Tableau's internal names become permanently
  stale after a Ctrl-drag duplication: e.g. a parameter internally named `[Y-Axis (copy 2)]` can have
  the real caption "Map KPI", entirely unrelated to any Y-axis control (seen in the Superstore sample
  workbook, which has several parameters duplicated this way). Reasoning from the internal name text
  (including the `(copy)`/`(copy N)` suffix itself, which is *not* a reliable "this is a duplicate of
  X" signal either) will misattribute the field's purpose. This applies to worksheet/dashboard zone
  `param` references too — always resolve through the spec's `field_id`, never the raw XML name.
- **Two non-tabular `data_type` values need special handling, never a plain column/measure** (seen in
  the Airline Alliance workbook — see `docs/tableau-dax-translation-guide.md` §8 for full detail):
  - `data_type: "table"` — Tableau's internal relationship-model table-anchor pseudo-column
    (`internal_name` prefixed `[__tableau_internal_object_id__]`). Not real data — exclude it from the
    semantic model entirely, same treatment as a vestigial field.
  - `data_type: "spatial"` (`MAKEPOINT`/`MAKELINE`-derived map geometry) — no native DAX/Power Query
    equivalent exists. Don't attempt to force it into a column; instead surface the underlying
    lat/long fields it references (still ordinary `real` columns) and flag the geometry field itself
    as a capability gap in `limitations_encountered` for `pbi-report-builder` to handle via a
    custom/AppSource visual or a reduced-fidelity two-point fallback.

## 2. TMDL hand-authoring pitfalls

If `powerbi-modeling-mcp` isn't connected and you're authoring TMDL files directly (per your skill's
own Tool Selection Priority fallback), the following mistakes compile-check fine but **crash Power BI
Desktop on open** — they only surface when the PBIP is actually opened, not from reading the files:

- **`database.tmdl` must be exactly**: `database` (no name after it) on its own line, then a
  tab-indented `compatibilityLevel: <n>` on the next line. A name after `database` or an unindented
  `compatibilityLevel` causes a TMDL indentation parse error.
- **Prefer single-line DAX over multi-line expressions for `column`/`measure`.** Multi-line
  expression continuation has a subtle, easy-to-get-wrong indentation contract; single-line
  `column X = <full DAX expression>` (DAX has no newline requirement) followed by properties at
  declaration+1 tab is the proven-safe pattern.
- **A measure's suffix-qualified name must never collide with any column name in the same table**
  (e.g. `measure 'X'` next to `column 'X'`, even if one is hidden). Tabular's naming rule shares one
  namespace between columns and measures per table — a bare-named "value" measure over a same-named
  base column is a common trap when a Tableau field and its derived measure share a caption. Suffix
  the measure (e.g. `'X Value'`) instead.
- **The `.pbip` file's `$schema` must end in a literal numeric version** (e.g.
  `.../pbipProperties/1.0.0/schema.json`) — never the placeholder text `1.x.x`.
- ⚠️ **GUID-shaped relationship names must be real GUIDs.** Friendly relationship names are valid
  (`relationship rel_Orders_Date`, or a quoted descriptive name), but a UUID-looking `8-4-4-4-12`
  token is parsed as a GUID by TOM. A cold-run build emitted `w1b1e5d0-...`; `w` is not hex, so the
  relationship failed even though the shape looked deliberate. If you want UUID names, generate real
  all-hex GUIDs; if you want readable names, do not fake a GUID with a prefix.
- **Inside a TMDL object block, every property must precede every annotation.** Violating this can
  raise `Invalid indentation was detected!` on a correctly indented neighbouring line, so don't chase
  whitespace first. Reorder the block as properties, then annotations/extended properties.
- **Field Parameter / dimension-parameter calc tables: `sourceColumn` must be the BRACKETED
  calc-column reference `[Value1]`/`[Value2]`/`[Value3]` — never bare `Value1`, never the friendly
  display name.** A DAX table-constructor row like `{("Label", NAMEOF(...), Order), ...}` with 3
  columns always produces physical columns named `Value1`/`Value2`/`Value3`, and in a *calculated*
  table a column binds to them as a **bracketed column reference**. The correct form is
  `column 'Map KPI'` … `sourceColumn: [Value1]` (friendly Name on top, bracketed source below).
  Writing `sourceColumn: Value1` **without brackets** (or `sourceColumn: <FriendlyName>`) passes
  `TmdlSerializer` structural validation cleanly AND `powerbi-report-author validate` (0 errors) but
  does NOT bind: Power BI Desktop silently **infers** `Value1`/`Value2`/`Value3` (`isNameInferred`)
  columns instead, the friendly `'Map KPI'` column never materializes, and every `'Map KPI'[Map KPI]`
  reference in a measure or slicer fails ("Column 'Map KPI' in table 'Map KPI' cannot be found or may
  not be used in this expression"). Worse: on open/refresh Desktop **rewrites the `.tmdl` on disk to
  the inferred `Value1`/`Value2`/`Value3` form**, discarding your friendly columns — so this must be
  correct *before* the first Desktop open. Found in all 5 Field Parameter tables of the Superstore
  build (only surfaced in Desktop, never in validation). See
  `docs/tableau-dax-translation-guide.md` §3 for the full pattern.
- **Never emit the compact filter `'Table'[Col] = [Measure]` (measure on the RHS).** When a measure
  filters a `CALCULATE` by a parameter-selection or prior-period **measure**
  (`'Flight Activity'[Year] = [Year Parameter Value]`, `'…'[Month] = [PM Month Value]`), the compact
  boolean-filter form is illegal DAX and fails **only at query/render time** with `A function
  'PLACEHOLDER' has been used in a True/False expression that is used as a table filter expression`
  (invisible to `validate` and TMDL structural checks; the report shows "Something's wrong with one or
  more fields" in Desktop). Hoist the measure into a `VAR` and compare the column to the VAR. Found in
  58 CM/CY/PM measures of the Airline build. See `docs/tableau-dax-translation-guide.md` §4.
- **Validate before reporting success.** After writing TMDL files, load
  `Microsoft.AnalysisServices.Tabular.dll` (ships with Tabular Editor, bundled in this skill's
  `scripts/_tools/TabularEditor/`) and call
  `[Microsoft.AnalysisServices.Tabular.TmdlSerializer]::DeserializeDatabaseFromFolder(<path>)` — this
  is the same parser Power BI Desktop uses, and it catches syntax errors (though not the
  naming-collision one above, which only surfaces on actual model commit) without needing to launch
  the full Desktop UI.

## 3. MCP / Desktop operational gotchas

- **DAX must reference a column's TMDL `name`, never its `sourceColumn`.** These can legitimately
  differ (e.g. after a rename to Title Case, or to dodge the measure/column naming collision above) —
  writing `SUM('UA Cities'[CDD_0_1])` when the column's actual `name` is `'Cdd 0 1'`
  (`sourceColumn: "CDD_0_1"`) looks fine and even validates fine, but fails **only at refresh/commit
  time** with `Column 'CDD_0_1' cannot be found`. Whenever you rename a column for any reason, grep
  every measure/calculated-column expression that references it and update to the new `name`.
- **When a verification harness reads dates through pythonnet/ADOMD, never compare `str(value)`.**
  `str()` formats a .NET `DateTime` in the host culture; a cold run saw `03/01/2015` instead of the
  invariant `2015-01-03`, creating a false failure on the exact date columns being checked for real
  `.xls` corruption. Format explicitly (`yyyy-MM-dd`, or round-trip `o` for date-times) before any
  oracle comparison.
- **Always pass an explicit culture to M type-conversion calls** (`Table.TransformColumnTypes`,
  `Number.FromText`, `Date.FromText`) — **and make its value match the source's actual textual
  representation, never a fixed constant and never the model's `culture`** (a hard-coded constant is
  the exact thing that silently corrupts a legacy `.xls`; evidence and decision tree below). Passing
  *a* culture is cheap insurance against a real failure mode: on a machine with a non-standard
  Windows regional format (e.g. language=English, region=Belgium — a "custom locale", LCID
  4096/`LOCALE_CUSTOM_UNSPECIFIED`), an XMLA-triggered refresh (`partition_operations
  RefreshWithXMLA`, or any MCP-driven refresh/commit) can fail with `'4096' locale is not supported`
  — even for a trivial metadata-only change, and even after adding the explicit culture argument
  (the failure can live below the M/model layer, in the AS engine process itself, inherited from the
  OS at process launch). **If you hit this: don't jump straight to an OS-level `Set-Culture`
  change** — that's an account-wide change outside the repo's scope; ask the user first. Instead,
  **try Power BI Desktop's own UI "Refresh" button** — empirically, a UI-triggered refresh can
  succeed where an externally-issued XMLA commit fails identically, so it's worth trying before
  escalating.
- ⚠️ **A hard-coded culture SILENTLY CORRUPTS a legacy `.xls` source** [measured,
  book_5-1-Table-Calcs, en-BE host] — this is the concrete proof of the "match the source, not a
  constant" half of the rule above, and it fails *silently and green*. `"en-US"` is the exact
  constant that breaks this case.
  - **Mechanism, proven step by step.** `Excel.Workbook(File.Contents(…), null, …)` reading a genuine
    BIFF `.xls` (magic `D0 CF 11 E0`) returns numeric and date cells as **text**
    (`Value.Is(sv, type text)` = `true`), and it formats that text in the **OS user locale** — *not*
    `Culture.Current` (measured `en-US` inside the mashup engine), *not* the model's `culture` /
    `sourceQueryCulture` (both `en-US` on disk). So `261.96` arrives as `"261,96"` and `2017-11-08` as
    `"08/11/2017"`. A downstream `Table.TransformColumnTypes(…, "en-US")` then reads `,` as a *group*
    separator → **26196** (a 100× inflation), and `d/m` as `m/d` → 11 Aug. Days > 12 raise a hard
    error and are nulled by `returnErrorValuesAsNull`.
  - **It refreshes GREEN and looks populated.** `SUM(Sales)` was **493× too large** (1,131,591,720 vs
    2,297,200.86) and **5,952 / 9,994 dates were blank**, while integer columns with no separator
    (`Quantity`) were untouched. `openability_selfcheck` reported `ok: true` on all six checks.
  - **`delayTypes` is NOT the cause — hypothesis tested and refuted.** `Excel.Workbook(…, null, false)`
    returns byte-identical text. Don't spend a cycle there.
  - **The discriminator that proves it is ONE cause, not two bugs:** sweep candidate cultures in a probe
    table. `en-GB` fixes the dates and still corrupts the numbers; `en-BE`/`nl-BE`/`fr-BE`/`de-DE` all
    reproduce source truth exactly. Two symptoms, one text round-trip.
  - **Decision tree — choose the culture by source, and prefer removing the `.xls` reader entirely:**
    1. **Durable fix — land the sheet as `.csv`/`.xlsx` upstream** (invariant `yyyy-MM-dd` dates, `.`
       decimals), where the values never become text and no culture can corrupt them. This is the
       real answer; fuller treatment and the deterministic reproduction are in §6.
    2. **Stopgap, only if you must parse the `.xls` in place — detect the OS locale at build time**
       (`GetUserDefaultLocaleName`) and emit *that* as the parse culture (it is the locale the reader
       used to format the text). Do **not** hard-code either `"en-US"` or `"en-BE"`. **No
       locale-independent M fix exists**: the reader's output locale is not observable from M, and
       `"1,234"` is genuinely ambiguous. ⚠️ This makes the emitted `.tmdl` **host-dependent** (it
       corrupts on the next machine), so it is a stopgap, not the fix.
- ⚠️ **A legacy `.xls` also needs a different NAVIGATION KEY, or the model cannot refresh at all**
  [same workbook]. The `.xlsx`-shaped `Source{[Item="Orders", Kind="Sheet"]}[Data]` does not resolve
  against a BIFF `.xls`, whose nav table has only `Name | Data` columns — there is no `Item` or `Kind`
  column to match. Use `Source{[Name="Orders"]}[Data]`. This one at least fails loudly (refresh
  errors outright) rather than silently, but note it *still* passed every structural check.
- **Rediscover the Desktop AS connection after every Desktop restart.** The child
  `AnalysisServicesWorkspace` process gets a new port every time Desktop (re)starts (observed
  57025 → 59524 across one session) — never reuse a cached connection string; always re-run the
  MCP's local-instance discovery first.
- **A blank/empty response from an MCP write operation (e.g. `RefreshWithXMLA`) means success**, not
  failure or a silent no-op — don't retry or assume something went wrong just because there's no
  descriptive payload back.
- **After any structural change that's loaded into an already-open Desktop session** (new
  column/measure/relationship, or a fresh `ExportToTmdlFolder`), Desktop shows a "columns need
  refresh"/pending-changes banner. Clear it with a `partition_operations RefreshWithXMLA` **Calculate**
  (not a full data reload) before treating the model as done, and confirm the banner is gone with a
  follow-up screenshot — don't just assume the Calculate silently worked.
- **Clean up junk/placeholder artifacts before reporting done.** Watch for oddly-named leftover
  measures or columns (seen in this workbook: `0,0`, `'Title Forklift'`, `'1.0'` — junk from an
  earlier authoring pass, likely a mis-parsed or duplicated calculated-field creation) that aren't
  referenced by any report visual or other measure. Confirm they're unreferenced, then delete them —
  don't ship a model with unexplained dead weight.

## 4. Model integrity, table calcs, cross-agent hand-offs, and scale

**Model-integrity checks the offline `TmdlSerializer` does NOT catch (add these to your validation):**
- **Model-wide DUPLICATE MEASURE NAMES break Desktop load.** Tableau auto-generates a `Number of
  Records = 1` measure *per data source*, so a multi-source workbook yields several measures all named
  `Number of Records`. `TmdlSerializer.DeserializeDatabaseFromFolder` deserializes this cleanly, but
  Power BI Desktop **refuses to open the `.pbip`** ("Could not add Measure with the name X because a
  Measure with the same name already exists"). **Rename duplicates to distinct names** (e.g. keep one
  `Number of Records`, rename the others `Securities Row Count` / `SP Data Row Count`). This shipped
  and broke a Desktop open — do not repeat it.
- **Offline validation recipe (do this when no live engine):** after `DeserializeDatabaseFromFolder`,
  programmatically assert (a) **model-wide measure-name uniqueness**, (b) **no measure name equal to a
  column name in the same table** (the commit-time trap), and (c) **every DAX `[bracket]` token resolves**
  to a real column/measure. These three catch the highest-frequency hand-authoring failures the
  structural parse misses. Also: an offline measure `DataType=Unknown` is **normal** (TOM infers it at
  refresh) — don't chase it.

**Table calculations & compat level:**
- **Prefer the `ALLEXCEPT`/`FILTER`/`EARLIER` form for table calcs at compat 1606** so the DAX validates
  offline; `OFFSET`/`INDEX`/`WINDOW` also evaluate at compat **1606** (measured on a model declaring
  1604, Desktop running 1606: `WINDOW(1, ABS, 0, REL, ORDERBY(...))` returned correct values). Use them
  only when you can ground-truth them. Verified patterns:
  `LOOKUP(agg,FIRST()/LAST())` → per-partition MIN/MAX-date helper calc column; `INDEX()` →
  `CALCULATE(COUNTROWS(t),FILTER(ALLEXCEPT(t,[part]),t[order]<=EARLIER(t[order])))`; `IF MIN(Date)=LOOKUP(MIN(Date),LAST())`
  → an is-last-row guard. See `docs/tableau-dax-translation-guide.md` §5–6.
- **AMO `ImageSave` raises `compatibilityLevel` from 1604 to Desktop's 1606.** This is an unavoidable
  persist side effect: align `database.tmdl` to the live level before shipping its cache.
- **Ground-truth EACH table calc two independent ways in Python** (Tableau semantics via sorted-partition
  `.iloc`/`cumcount`, and a literal DAX-mechanics replica via boolean masks over the raw table) and assert
  equality per probe row — two independent codings agreeing is far stronger than restating one formula.
- **A running total is correct only at the grain it was addressed for — validate it against the
  visual axis, not just the DAX shape.** Two different mechanisms shipped the same silent symptom:
  - `CALCULATE(SUM(t[v]), FILTER(ALL(t[Date]), t[Date] <= MAX(t[Date])))` only clears `t[Date]`. Put a
    coarser same-table column on the axis (for example a month-start bin) and the surviving `t[Month]`
    filter restricts the rows, so the line becomes that bucket's own total. Fix by pinning the as-of
    date in a `VAR` and clearing every same-table date-ish column the visual can filter, e.g.
    `CALCULATE(SUM(t[v]), FILTER(ALL(t[Date], t[Month]), t[Date] <= _asOf))`. Measured on a 730-row
    table: old form returned exactly the month total for all 24 months; the fixed form accumulated to
    the grand total to the cent.
  - `WINDOW(... ORDERBY(c))` has the matching trap by address, not by filter clearing: it is cumulative
    only when `c` is literally the visual's category grain. Measured cold run S14:

    | visual axis | emitted measure returned | verdict |
    |---|---|---|
    | `'Orders'[Order_Date]` | 16.45 → … → 2,297,200.86 | ✅ cumulative |
    | `'Date'[Date]` | 16.45, 288.06, 19.54 … | ❌ each day's own total |
    | `'Date'[Month Start]` | 14,236.89, 4,519.89 … | ❌ each month's own total |

    The emitted report used `'Date'[Month Start]` on the axis beside a measure ordered by
    `'Orders'[Order_Date]`, so the measure disagreed with its own visual.
  - **Gate reality:** structural validators and `check_datamodel.py` do not see PBIR category bindings;
    a cheap model-only gate cannot prove this. Until a cross-artifact report-axis check exists, run
    `EVALUATE` probes at every axis grain the emitted visuals bind to and compare to the source/oracle.

**Month/quarter binning is a MODEL job, and it lands on you mid-migration:**
- Power BI cannot bin a date to month in the **report** layer when the model has no date table, no
  `variation` blocks and `__PBI_TimeIntelligenceEnabled = 0` — `HierarchyLevel`/`GroupRef` are unavailable
  and `DateSpan` is for filter `Where` conditions, not projections. So a source `mn`/`tmn` date-part shelf
  becomes a **date-typed month-start calculated column** per table:
  `IF(ISBLANK(t[D]), BLANK(), DATE(YEAR(t[D]), MONTH(t[D]), 1))`, `dataType: dateTime`,
  `formatString: mmm yyyy`. Never a text label ("Jan 2022" sorts alphabetically and scrambles the axis);
  if you do ship a label column, give it an explicit `sortByColumn`.
- **Per-table, not a shared calendar**, when the sources were independent in the workbook (`joins: []`,
  one source per worksheet) — a shared date dimension invents relationships the source never had and
  changes cross-filter semantics. Two disconnected month columns are the faithful shape.
- Note the source-tool nuance in `limitations_encountered`: Tableau's `mn` is the MONTH **date part**
  (cycles Jan–Dec), `tmn` is the truncated month (month start). A multi-year trend line means month-start;
  say which reading you implemented rather than letting it pass silently.

**`sortByColumn` IS A DEPENDENCY EDGE — so its target can never be a calculated column derived from
the column it sorts** [measured 2026-08-08, `book_5-2-LOD`, Desktop 2.157.627.0]. Tableau sorts a
dimension axis **DESC by a measure**; the natural translation is to rank the dimension and set
`sortByColumn`. Written as a DAX calculated column, it **cannot load**:

```
A circular dependency was detected:
Orders[Sub-Category], Orders[Sub-Category Sales Rank], Orders[Sub-Category].
```

The sorted column depends on its sort key, and a key that is **single-valued per category** (which a
sort key must be) is by definition a function of that category — so it depends back. The cycle is
**unavoidable for any DAX calculated column ranking its own column**, and rewriting the DAX does not
escape it: dropping the direct `'Orders'[Sub-Category]` reference only routes the cycle through the
`ALLEXCEPT` argument instead. Contrast the shape that *does* work — the emitted `Date.tmdl`, where
`Month` sorts by `'Month No' = MONTH('Date'[Date])`: the key derives from a **different** column.

**Fix: materialize the rank in Power Query so the key carries no DAX dependencies at all** — an
ordinary source column. Strictly additive, after the verified type step:

```
SubCatTotals = Table.Group(Typed, {"Sub-Category"}, {{"SubCatSales", each List.Sum([Sales]), type number}}),
SubCatRanked = Table.AddIndexColumn(Table.Sort(SubCatTotals, {{"SubCatSales", Order.Descending}}), "Sub-Category Sales Rank", 1, 1, Int64.Type),
SubCatKey    = Table.RenameColumns(Table.SelectColumns(SubCatRanked, {"Sub-Category", "Sub-Category Sales Rank"}), {{"Sub-Category", "SubCatKey"}}),
Ranked       = Table.ExpandTableColumn(Table.NestedJoin(Typed, {"Sub-Category"}, SubCatKey, {"SubCatKey"}, "SubCatJoin", JoinKind.LeftOuter), "SubCatJoin", {"Sub-Category Sales Rank"})
```

`Table.Group` guarantees one row per category, so the LEFT OUTER join is 1:1 — verify row count is
unchanged and `COUNTROWS(SUMMARIZE(t, t[Cat], t[Rank]))` equals the distinct category count, or the
model refuses to commit ("There can't be more than one value in the 'sort by' column…").

⚠️ **This defect class is invisible to every offline check.** The calculated-column version passed
`TmdlSerializer.DeserializeDatabaseFromFolder` (compat 1606, `SortByColumn` resolving to a real
column object), `check_datamodel`, including M syntax and low-noise TMDL integrity assertions — and Desktop
still refused the project. The symptom is the **`Untitled - Power BI Desktop`** window from §2, and
the message lives in an **in-app dialog** that only a UI-Automation *descendants* scan surfaces (§5's
recipe, filtering on `circular|depend|Issues`). Read the modal; do not infer from the title.

✅ **And the payoff, also measured: a model-level sort SURVIVES small multiples where a visual-level
sort does not.** On the same `clusteredBarChart` with `Region` in the small-multiples well, a PBIR
`sortDefinition` was ignored (alphabetical) whether it named an off-axis measure or the on-axis
aggregation, while the byte-identical sort with the well emptied worked. `sortByColumn` reordered all
17 categories correctly across all 4 panes. So when a source tool sorts a small-multiples axis by a
measure, **`sortByColumn` is the mechanism that works** — and it is the model's job, not the report's.

**Cross-agent — the report builder needs these FROM you (decide at model-design time):**
- **Azure Map route/great-circle maps (Tableau `MAKELINE`/`MAKEPOINT`): build an endpoint-unpivoted PATH
  table** (one row per endpoint, with a shared path id + point order) so the report can feed azureMap's
  `PathID`+`PointOrder` wells. Origin+destination lat/long as four columns on a single fact row **cannot**
  draw an arc — the report is then stuck with endpoint bubbles. This is a model-shape decision, not a
  report one.
- **Azure Map POINT maps (`visual.json` `visualType: "azureMap"`): the model owes the report a
  single LEAF geography column, because the Location well is a DRILL HIERARCHY** [seen,
  `book_6-1-Maps` Superstore, 2026-08-09]. Read the emitted PBIR `visualType`, not the source mark
  type, before choosing this recipe. Tableau plots
  geographic fields on Detail at the **finest** level present, so `Country / State / City` draws one
  mark per city. Azure Maps does the opposite: *"When entering multiple values into the Location
  field, you create a geo-hierarchy"*
  ([learn](https://learn.microsoft.com/azure/azure-maps/power-bi-visual-geocode)) and renders the
  **current (top)** level. Measured: `DISTINCTCOUNT('Orders'[Country]) = 1`, so 6 of 7 azureMap
  visuals collapsed to **one mark at the US centroid** — and it renders, refreshes and validates
  cleanly, so only a screenshot catches it. ⚠️ **The leaf must be the qualified key, not the bare
  city**: on this 9,994-row extract `City` alone is 531 values but `(City, State)` is **604** — 57
  city names repeat across states (4 Springfields), so `City` alone silently merges/mis-geocodes
  **21.5%** of marks. Emit `column 'Map Location' = IF(OR(t[City]="", t[State]=""), BLANK(), t[City] &
  ", " & t[State])` with **`dataCategory: Address`** (the free-form category — a partial address
  geocodes to the city centroid; that doc explicitly warns against stuffing composite values into
  `City`/`StateOrProvince`) and `summarizeBy: none`. Concatenating location fields into one mapped
  column is first-party guidance
  ([learn](https://learn.microsoft.com/power-bi/create-reports/desktop-tips-and-tricks-for-creating-reports#improve-geocoding-with-more-specific-locations)).
  ⚠️ **Give the column a comma-free name** (e.g. `'Map Location'`, above). The engine *does* accept a
  comma — verified against a live engine: TOM accepted the create, `'Orders'[City, State]` parses
  (brackets delimit), and every table-qualified reference evaluates — so it does **not** fail. But
  `,` is documented-invalid for table/column/measure names
  ([DAX naming requirements](https://learn.microsoft.com/en-us/dax/dax-syntax-reference)) and works
  only because hand-authored TMDL bypasses the validation Power BI Desktop's UI enforces: unsupported
  territory that external tooling (Tabular Editor, BPA, downstream parsers) may reject, with no
  guarantee it keeps working. Prefer the comma-free name and **tell the report builder the exact
  name**. Do **not** reach for Lat/Long unless
  the source actually has them: Tableau's `Latitude (generated)` is its geocoder's *output*, not
  workbook data, so synthesising coordinates means importing a gazetteer the migration never had.
- **Provision EVERY dashboard-visible metric.** If a Tableau dashboard shows a KPI tile/value, the model
  must have a backing measure or column for it — the report builder works against a *frozen* model and can
  only render a static placeholder card for a metric that has no backing field (seen: 3 Airline tiles).
- **Legacy Bing bubble maps (`visualType: "map"`/Bing, not `azureMap`) need a concatenated
  `Place` column FROM YOU, and it needs a comma-free name.** Read the emitted PBIR `visualType`; a cold
  run inherited a stale Bing assumption for a bundle that emitted only `azureMap` visuals and looked for
  a deprecation modal that could not exist. Bing geocodes a Location field **by name**, so bare US city
  names put Springfield/Columbus/Franklin in Europe, Africa and Australia — verified 100 % US data (1
  country, 49 states, 531 cities) rendering across three continents. The report layer **cannot** fix this:
  dragging `Country`/`State` in beside `City` turns the Location well into a **geo-hierarchy** that renders
  only its top level, so a single-valued `Country` collapses every bubble into one
  ([map tips](https://learn.microsoft.com/power-bi/visuals/power-bi-map-tips-and-tricks), tip 2). The model
  fix is one calculated column `City & ", " & State` with **`dataCategory: Place`** (that doc's tip 4 —
  Place is the category *for* a single column carrying full location info; the "keep `City` = `Southampton`,
  not `Southampton, New York`" warning applies to **City**-categorized columns, so leave the original
  `City`/`State`/`Country` categories untouched). ⚠️ **Name it without a comma** — e.g.
  `'Map Location'` — and **tell the report builder the exact name**. (A comma *works* but is
  documented-invalid and survives only by bypassing Desktop's UI validation, so it is unsupported
  territory; full evidence in the azureMap point-map entry above. Don't ship `'City, State'`.) Expect
  the **bubble count to rise** (531 → 604 here): the new column's
  cardinality equals the distinct `(City, State)` pair count, which *matches* Tableau's own mark grain when
  its Detail shelf carried City + State + Country — a fidelity gain, not a regression, but say so or it
  reads as a bug.
- **Dimension-flavored Field Parameters need the `ParameterMetadata` marker**, or the report can't native-
  swap the dimension (measure-flavored FPs switch fine via `SELECTEDVALUE` wrapper measures).
- **A re-runnable `_gen/gen_model.py` will clobber the report builder's work on your NEXT fix pass**
  [seen, LogisticsLive]. Model generators typically also emit a one-page report *placeholder* so the
  `.pbip` can be opened in Desktop at all. Re-run that generator for a model-only fix after
  `pbi-report-builder` has replaced the folder, and it silently rewrites `pages.json` back to
  `pageOrder: ["p_placeholder"]`, orphaning every built page — a cross-layer clobber no validator catches.
  Guard the scaffold (`if a real report exists: skip`) *before* re-running, and confirm afterwards that
  Desktop still enumerates the real pages. The same applies to `cultures/en-US.tmdl`: a generator re-run
  resets it to a bare scaffold, so re-stamp `set_ai_instructions.py` or the model silently loses its AI
  instructions.

**Modeling at scale / fidelity:**
- **Reconcile near-duplicate data sources by WORKSHEET BINDING, not row content** — byte-identical CSVs in
  a different row order have different MD5s; check which source the worksheets actually bind to, model the
  one that's used, and exclude the vestigial one (don't duplicate hundreds of thousands of rows).
- **Deduplicate large measure sets with a base-registry + period cross-product generator** (recognize
  CY/PY/CM/PM × {region-wide, entity-specific} families) and emit them from a re-runnable script rather
  than hand-writing 100+ measures — far safer and trivially re-runnable for fix passes.
- **`referenced_fields` tracks identity but NOT operand order** — for operand fidelity in a heavily
  Ctrl-drag-duplicated workbook, do an in-place internal-name→current-caption substitution on the raw
  formula; internal names are systematically scrambled (and can carry source typos like `Orignial`).
- **Extract-baked custom-SQL UNION → model one flat table** (the UNION is already materialized in the
  `.hyper`/CSV; don't rebuild it in Power Query). **Mixed numeric/alphanumeric keys** (e.g. `117` vs
  `WA-SNO457`) must be forced to **String** in the M type step or refresh nulls the alphanumeric ids.
- **BPA "Hide fact table columns" is an EXPECTED deviation for faithful Tableau migrations** — keep base
  numerics visible with `summarizeBy=sum` (Tableau exposed them as draggable measures); don't "fix" it.
  The bundled `bpa.ps1` runs Tabular Editor with `-G` (silent stdout, exit 0 even on violations) — to see
  the human-readable list, run `TabularEditor.exe <def> -A <rules>` **without** `-G`.

## 5. Live sources: prove reachability first, and never self-supply a credential

A live-source model (Databricks, Snowflake, SQL Server, BigQuery…) is the one case where a model can
be **structurally perfect and completely unverified**. The TMDL of a model whose warehouse was never
contacted is byte-identical to one that refreshes fine — nothing on disk tells you which you have.

### The failure this section exists to prevent (measured 2026-08-01)

An agent was told to migrate a workbook with two live Databricks sources and *"treat data validation
as deferred if it turns out you cannot reach the source."* It ran **45 minutes / 178 tool calls** and
produced a complete, well-formed model — Databricks partitions, correct host and HTTP path, honest
column descriptions — for a warehouse it had **never once contacted**. Proof: the SQL warehouse was
still `STOPPED` with `num_active_sessions = 0` (a serverless warehouse auto-starts on the first real
query), and no `.pbi/cache.abf` existed. It never came back to ask.

Two distinct root causes, both worth naming:

1. **The only real connectivity test lived at the END.** `preflight_source_credentials.py` is a
   *static* read of `migration-spec.json` — it reports "this looks like a live source that will need a
   credential" and never opens a socket. The genuine test was the handoff refresh gate, *after* the
   whole model was built. So nothing could fail fast.
2. **"Deferred" was read as "skip".** The agent took permission-to-continue-if-unreachable as
   permission to never find out. Those are different: deferral is a decision made **after** a probe
   fails, and it needs the probe result to be an informed one.

### The rule

**Read one row from every live source before translating a single calculation.** A ~30-second probe
replaces a 45-minute build you would have to throw away. Order matters more than the mechanism:

```bash
python scripts/preflight_source_credentials.py --spec <spec>   # inventory only - opens NO connection
# ... then a REAL read, before any table/measure work:
python scripts/probe_desktop_query.py --pid <pid>              # -> PREFLIGHT: DATA_OK
```

Cheapest honest probe: author a **canary** — one table, one live partition, `Table.FirstN(…, 1)` —
open it in Desktop, refresh, require a row. If it returns data, build for real; if it does not, you
have your answer in seconds. Cap at **~2 minutes or 3 attempts**, then stop and ask.

⚠️ **Before blaming the Desktop Bridge, look for a blocking Desktop dialog.** Measured bridge symptoms
for an already-open data-source dialog are `status` reporting **"Host is not ready to accept
operations"** and `screenshot` reporting **"Print metadata is not available"**. That combination is
not enough evidence for a bridge regression; run the bundled refresh/query probes, which check for
visible non-main dialogs at t=0 and keep polling while the source wakes up. Text-readable credential
prompts report `CREDENTIAL_MISSING`; unreadable/non-credential dialogs report `BLOCKED_BY_DIALOG`.

**Independent confirmation is available and worth taking.** For a serverless warehouse, `STOPPED` with
`num_active_sessions = 0` proves no query ever arrived, regardless of what any log claims. Prefer
evidence from the *source system* over the absence of an error on your side.

### ⛔ Never obtain or supply the credential yourself

You do not have it, and every route to getting it is a defect:

| Tempting shortcut | Why it is wrong |
|---|---|
| Read `.databrickscfg`, `~/.aws`, env vars, a keyring | Exfiltrates a user secret into your context |
| Reuse your own `az` / `databricks` CLI token | Builds a model **only you** can refresh; breaks for every real user |
| Embed a PAT/key in TMDL or M | A committed secret — the worst outcome, and it survives in git history |
| Drive Desktop's sign-in modal | The Desktop bridge cannot fill it; automating a credential UI is not yours to do |

Correct M **defers to Power BI's own credential store** and names no secret at all:

```
Source = Databricks.Catalogs(DatabricksHost, DatabricksHttpPath, [Catalog=null, Database=null])
Source = Sql.Database(ServerName, DatabaseName)
```

Your only move is to **ask** — naming the system, the server, what you tried, and the two options
(sign in interactively in Desktop, or supply a PAT/key). A credential is the canonical thing only a
human can provide; no amount of retrying or cleverness conjures one.

### Why you cannot just run `SELECT 1` from the shell

The obvious idea — "run a one-row query against the warehouse and see if it works" — tests the **wrong
credential**, and that failure mode is worse than no test.

`databricks sql`, an ODBC call, or an `az` token all authenticate as **you**, the agent's shell
identity. Power BI does not use any of them. It uses a credential cached **per-Windows-user in
Desktop's DPAPI store, keyed by data source**, and there is no `powerbi test-connection` verb that
reaches it. So a shell probe can return a happy row from a warehouse Power BI still cannot open — a
green light that means nothing. (This was exactly the setup measured 2026-08-01: the `databricks` CLI
was fully authenticated and could query the warehouse, while Power BI had never authenticated to it at
all.)

The test must therefore go **through Power BI**, and the smallest thing Power BI can execute is a
model. Hence: one table, `Table.FirstN(…, 1)`, refresh, require `DATA_OK`. That is the `SELECT 1` — it
just has to be expressed as a partition rather than a shell command.

**Do not build a separate throwaway "canary" PBIP for this.** That was tried and abandoned: a
hand-authored PBIP needs five exactly-correct `$schema` URLs plus a `.platform` file, and getting any
of them wrong makes Desktop throw a modal crash dialog that looks *identical* to an unreachable source.
You are already building a model — build its **first table only**, refresh that, and continue only on
`DATA_OK`. No new file-format surface, no new failure mode.

### `PERSISTED` does not prove the live source loaded

The hand-off gate requires `REFRESH: DATA_OK + PERSISTED`. For a **live** source that is necessary but
not sufficient, because **a partial refresh still writes a cache**.

Measured 2026-08-01: a migration with two Databricks tables plus one CSV finished with a 62 KB
`.pbi/cache.abf` on disk — while the SQL warehouse had never left `STOPPED` with
`num_active_sessions = 0`. The cache was real; it just held the **CSV table only**. Both live tables
were empty. The model looked refreshed, persisted and validated, and was none of those things where it
mattered.

So for every live table, assert rows explicitly:

```
EVALUATE ROW("n", COUNTROWS('Shipment'))     -- must be non-zero, per live table
```

And prefer evidence from the **source system** when you can get it: for a serverless warehouse,
`STOPPED` + `num_active_sessions = 0` proves no query ever arrived, whatever your own logs suggest.

### A missing credential looks like a HANG, not an error — read the modal, don't wait it out

Measured 2026-08-01 (`logistics-live-dbx`, Databricks `.../warehouses/764e5801f0e0fac8`, one-table
probe): `refresh_pbip_model.py --pid <pid>` produced **no output for ~7 minutes**. There is no error to
wait for — the mashup engine is parked on Desktop's credential modal, which only a human can answer, so
the XMLA refresh never returns. Waiting longer cannot change the outcome; **diagnose in seconds
instead**, and note the modal is *in-app*, so a top-level-window scan finds nothing (`FindAll(Children)`
returns only the main `LogisticsLive` window). Scan **descendants** of the Desktop window:

```powershell
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, <pid>)
$win  = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
$win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition) |
  ForEach-Object { $_.Current.Name } | Where-Object { $_ -match "signed in|Personal Access|httpPath|incomplete or no data" }
```

It returns the verdict *and* the exact data source the credential is keyed to:
`You aren't signed in.` / `{"host":"adb-….azuredatabricks.net","httpPath":"/sql/1.0/warehouses/…"}` /
`Some of the tables have incomplete or no data.` — quotable evidence for the user, in ~10 seconds.

Then stop the refresh and confirm the negative three ways before reporting: `probe_desktop_query.py`
→ `PREFLIGHT: NO_DATA` (the model loads and the DAX runs — 13 columns bound — it just has 0 rows), **no
`.pbi/cache.abf` written**, and the warehouse still `STOPPED` / `num_active_sessions = 0`. That trio
distinguishes "no credential" from "broken M or TMDL", which is the only ambiguity that matters here.

### The live-source failure is a CRASH, not an error message (measured 2026-08-01)

When Power BI Desktop has no cached credential for a live source, the refresh does not return a clean
"authentication failed". Two independent agents, on separate models, produced the same sequence:

1. XMLA refresh blocks for minutes (the sign-in modal is waiting on a human).
2. `msmdsrv.exe` dies with an unhandled `System.IO.IOException` in
   `PipeStream.WriteCore` → `MessageSerializer.Serialize` → `GlobalExceptionHandler.HandleException`
   (Windows Application log, **event 1026** — verified independently at 16:21:25 and 16:26:24).
3. The client sees only `AdomdConnectionException` / `SocketException (10054) forcibly closed`.

So the mashup engine raises the credential exception and then **crashes while posting it back over the
named pipe** — the real error text is destroyed in transit. Consequences:

- **Do not read a socket error as "the source rejected us".** It is the local host dying; the source
  may never have been contacted at all. Say "reachability unproven", not "credential refused".
- **A ~5-minute block is diagnostic.** A genuine refusal returns in seconds; a multi-minute hang is the
  shape of a modal waiting on a human. **Do not expect the XMLA `CommandTimeout` to cut it short.**
  Measured 2026-08-01 both ways, timeout verified on readback each time: it is honoured precisely for a
  *query* (a slow `EVALUATE` aborted at 1.2 s under `CommandTimeout = 1`), yet a credential-blocked
  *refresh* under `CommandTimeout = 45` ran **past 150 s** and never returned. ⚠️ Inferred: the mashup
  engine sits in a synchronous wait on a UI dialog in another process, which the server cannot
  preempt. **Run your own clock; stop at ~2 min.**
- **Never take a timing measurement against a bundle preflight calls STALE.** The plugin copy shadows
  `.github/skills/`, so the code that runs is the published one, not the one you just edited. An
  earlier attempt at the measurement above ran a plugin copy with no timeout in it at all and produced
  a confident, wrong conclusion. `preflight.ps1` now blocks on STALE for exactly this reason.
- **The crash takes down SIBLING Desktop instances**, not just yours. In a parallel batch this shows up
  as an unrelated agent's mysterious failure at the same timestamp. Check event 1026 before blaming
  their model.

### ⚠️ Inferred RAM-pressure Desktop crash: `PlatformDependentOptions`

Crash signature to grep for:

```text
Something went wrong
The type initializer for 'Microsoft.Mashup.Host.Document.PlatformDependentOptions' threw an exception
```

Field evidence from 2026-08-19: the crash happened during a customer migration sweep with 4–5 Power BI
Desktop instances open and about 3.1 GB free RAM out of 31.7 GB total. Each open Desktop instance owns
an `msmdsrv` child with the loaded model resident, so large-model estates can exhaust the machine well
before `--pid` addressability becomes a problem.

✅ Confirmed negative result: deleting the model's 313 MB `.pbi/cache.abf` and letting Desktop rebuild
it did **not** fix the crash, which argues against treating this signature as simple cache-file
corruption.

⚠️ Leading hypothesis, not proven cause: RAM pressure from too many loaded Desktop models. No
controlled reproduction varied free RAM and instance count while holding the model and machine
constant. To confirm this mechanism, reproduce the crash across controlled free-RAM / instance-count
levels and show it disappears with otherwise identical conditions and more available RAM.

Practical rule until then: before opening another Desktop instance, check free RAM and count live
`PBIDesktop`/`msmdsrv` processes. On large models, keep concurrency low and close each Desktop
instance as soon as its verification handoff is complete.

### ⚠️ `powerbi-desktop open` can return the WRONG pid

Measured 2026-08-01, with two Desktop instances open: `open` reported a `pid` belonging to a **sibling
agent's** instance. Binding to it would have refreshed and `ImageSave`-d into *another model's*
`cache.abf` — silent cross-contamination, and the exact `WRONG_MODEL` hazard the pid-binding rule
exists to prevent.

**Trust `powerbi-desktop status` + `currentFilePath`, never the pid `open` hands back.** Match the
instance to your own `.pbip` path before you touch it.

## 6. File-based extracts: a legacy `.xls` + a custom OS locale silently corrupts DATA

Measured 2026-08-08 (`book_8-1-Dashboards`, Tableau Superstore extract landed as a legacy BIFF8
`.xls`, machine locale **en-BE / LCID 4096** — decimal separator `,`, short date `dd/MM/yyyy`).

This is the worst class of defect in this file: **the model opens, refreshes, persists, reports
`REFRESH: DATA_OK + PERSISTED`, and passes the hand-off gate — while every decimal and every date in
the fact table is wrong.** No structural validator can see it.

**The signature** (model vs. the source file, read independently with `xlrd`):

| source value | rendered by the reader (en-BE) | in the model | what happened |
|---|---|---|---|
| `Sales 261.96` | `"261,96"` | `26196` | `,` consumed as a **group separator** |
| `Sales 957.5775` | `"957,5775"` | `9575775` | same, ×10⁴ |
| `Discount 0.45` | `"0,45"` | `45` | same |
| `Order Date 2017-11-08` | `2017-08-11` | day↔month **transposed** (silently plausible) |
| `Order Date 2017-06-16` | *(null)* | day > 12 → invalid month → null |
| `SUM(Sales) 2,297,200.86` | `1,131,591,720` | ~493× |
| 0 blank dates | **5,952 / 9,994 blank** | ~60% of the fact table |

**The tell that saves you:** integer and text columns are **perfectly correct** while decimals and
dates are wrong. `Quantity` (37,873), `Postal Code`, `City` (531 distinct) all matched exactly. A
partial-correctness pattern like that is a *parsing* fault, never a *binding* fault.

**⚠️ Adding `Table.TransformColumnTypes(..., "en-US")` changes NOTHING — but NOT because culture is
out of reach.** [corrected 2026-08-09 by `book_6-1-Maps`; the original text here asserted M never sees
the source text, which is false and contradicted §3 of this same file.] The measurement is sound —
`"en-US"` was confirmed live via `INFO.PARTITIONS()` and produced byte-identical corrupt output, and
`delayTypes = false` likewise. **The interpretation was wrong.** `"en-US"` is already the *effective*
parse culture, so pinning it is a no-op; the experiment shows the value is wrong, not that the knob is
disconnected.

The original reasoning — *"an `en-US` parse of `"261.96"` can only ever yield `261.96`"* — substitutes
the wrong string. **M never receives `"261.96"`; it receives `"261,96"`** (§3: the reader emits text in
the **OS user locale**, not `Culture.Current`). An `en-US` parse of `"261,96"` reads the comma as a
*group* separator and yields exactly `26196`. M *does* see the original text, and a culture argument
*can* reach it.

**Reproduced deterministically** (`book_6-1-Maps`, from the same `.xls` via `xlrd`): render each value
at **4 decimal places** in a comma-decimal locale, strip the separator, and you get
`SUM(Sales) = 1,131,591,720` and `SUM(Profit) = 1,799,876,538` — **digit-for-digit the values measured
live in `4-dashboards`.** The 4-dp rendering is why the multiplier is not a constant ×100: it is
10^(decimals), so the aggregate ratio differs per column (Sales ≈ 493×, Profit ≈ 6,285×). An arbitrary,
per-column inflation ratio is the fingerprint of this bug.

**So `nl-BE` genuinely does fix it** (independently shipped by `book_5-2-LOD`, 75/75 oracle checks) —
but **do not adopt it**: the correct culture is whatever locale the *build host* renders in, so a
culture pin bakes the build machine into the artifact and corrupts on the next machine. That, not
unreachability, is the reason to prefer the CSV path below.

**Fix: take the legacy `.xls` reader out of the path.** Re-land each sheet as an invariant-format
CSV and read it with `Csv.Document`. Do it from a re-runnable `_build/` script, and keep every generated
CSV inside that migration bundle's `data/` folder (or its documented data folder) so parallel waves do
not share mutable files. The details matter:

- Write RFC-4180 CSV with Python's `csv` module (`newline=""`, UTF-8/UTF-8-SIG, `QuoteStyle.Csv` in
  M) so embedded commas and quotes round-trip — e.g. `It's Hot Message Books with Stickers, 2 3/4" x 5"`.
- Emit decimals with a round-tripping invariant representation (`.` decimal separator). Strip a trailing
  `.0` only for values destined for `Int64.Type`, and fail the build if a value would be written in
  scientific notation unless you have explicitly tested the M type that will parse it.
- Choose date text from the model's M type list: `yyyy-MM-dd` for `date`, `yyyy-MM-ddTHH:mm:ss` for
  `datetime`/`datetimezone` (plus an explicit zone only when the source has one).
- Write atomically in the same folder: create `*.partial`, flush/close it, then replace the target path.
  Do not write to a shared temp directory or to another worktree's bundle.
- The type-conversion culture must describe the **bytes being parsed**. For an invariant CSV,
  `Table.TransformColumnTypes(..., "en-US")` is correct because the file uses `.` decimals and ISO
  dates — not because the author, host, customer or model culture is `en-US`. For an in-place `.xls`
  stopgap, the culture has to match the host locale that rendered the text (§3), which is why that
  stopgap is not portable.

Post-fix the measured model matched the source **exactly** (9,994 rows, `SUM(Sales) = 2,297,200.86`,
`SUM(Profit) = 286,397.02`, dates `2015-01-03..2018-12-30`, zero blanks). Do **not** reach for an
OS-level `Set-Culture`: that is an account-wide change outside the repo's scope (§3), and the CSV path
is locale-proof by construction.

**After the first full refresh, assert every import table's row count against its source/oracle.** Run
`python scripts/refresh_pbip_model.py --pid <pid> --canaries Orders Customers` (name every import
table) and compare each emitted `data : N row(s)` to the source count (for example, 9,994 for the
Orders extract). A green
`EMPTY-MODEL CHECK`, `openability_selfcheck`, or `probe_bundle --check-only` proves neither that M can
read the partition nor that rows landed; `EVALUATE ROW("n", COUNTROWS('Orders'))` is the equivalent
direct assertion. Then compare totals and min/max dates against the file itself (`xlrd` for BIFF8).

**This is machine-wide, so check your siblings.** All four bundles on this machine read the same
`.xls` the same way, so all four carry the same corruption. Report it to the orchestrator rather than
editing another agent's model — but do report it, because nothing downstream will catch it.

### ⚠️ The modeling MCP can serve a DIFFERENT model than the port you asked for

Measured 2026-08-08, same session. `connection_operations Connect` with
`Data Source=localhost:53583` reported success and even named the connection
`PBIDesktop-book_8-1-Dashboards-53583`, with `GetConnection` echoing back the correct server *and*
the correct catalog GUID. It then answered queries from a **different model**: `INFO.TABLES()`
returned four tables including a `_ProbeSheets` table that exists in no bundle here, `Orders` had 24
columns (the TMDL declares 22) including calculated columns belonging to a *sibling* migration, and
`COUNTROWS('Orders')` returned **0 rows** while `'Date'` returned 366 — against a model that a
pid-scoped ADOMD probe showed had 9,994 rows on that same port at that same moment.

**So the MCP's own connection metadata cannot be used to prove identity.** Cross-check it against
something the model itself must satisfy — the table list and column count from your TMDL — before
trusting a single number it returns. When they disagree, prefer the **pid-scoped** path
(`probe_desktop_query.py`'s `_child_port(pid)`, which walks Desktop→child `msmdsrv` and never widens
the lookup). A wrong-model read is far more dangerous than a failed read: it returns confident,
plausible numbers about somebody else's data.

### ⚠️ Offline `ConnectFolder` has the same implicit-connection cross-talk hazard

Measured 2026-08-18 in a multi-agent build, with a sibling `pbi-semantic-builder` using the same MCP
server for a different model. A `table_operations List` call that omitted `connectionName` silently
returned the sibling workbook's tables (confirmed against that build's `generated-edit-declarations.json`);
a follow-up column call then failed with "table not found." `ListConnections` also showed a stale
two-connection snapshot for a moment right after `Disconnect`.

**Rule:** after any connect call, pass the returned `connectionName` explicitly on every model-object
operation in a multi-agent build. Never rely on the implicit "last connection." The readback pattern
that confirmed a disconnect had landed was re-running `Disconnect` on the same name and getting
`not found`.

Nuance, verified against the MCP tool schemas in this doc pass: `connectionName` is optional and
documented as "Uses the last connection if omitted" on `table_operations`, `column_operations`,
`measure_operations`, `relationship_operations`, and `partition_operations`. The same schema says
`connection_operations` **forbids** `connectionName` on `Connect`, `ConnectFabric`, `ConnectFolder`,
and `ConnectBimFile` because the name is auto-generated; use the returned name only after the connect
call. That is 5 checked operation types out of the broader MCP surface, not proof that all operation
types share the same parameter.

What was **not** measured: how long the stale `ListConnections` snapshot persists. The observation was
"a moment" / seconds apart, not a bounded duration.

## 7. Legacy `.xls` navigation keys, and Desktop sessions that turn errors into hangs

Measured 2026-08-08 (`book_6-1-Maps`, same Superstore BIFF8 `.xls` and same en-BE machine as §6).
Three defects that cost a full debugging cycle each, none of which §6 covers.

### 7.1 The emitted navigation key is XLSX-shaped and can never match a legacy `.xls`

The engine emits the navigation record Power Query uses for a **modern `.xlsx`**:

```
Navigation = Source{[Item="Orders", Kind="Sheet"]}[Data]      -- fails on a legacy .xls
```

`Excel.Workbook` returns a navigation table whose **columns differ by file format**. For `.xlsx` it
has `Name / Data / Item / Kind / Hidden`; for a legacy BIFF8 `.xls` it has **only `Name` and
`Data`**. A record key naming a column that does not exist can never match, so the refresh fails:

```
[Expression.Error] The key didn't match any rows in the table.
```

The correct legacy form is Name-keyed: `Source{[Name="Orders"]}[Data]`.

**Do not guess the item spelling — enumerate it.** A plausible-looking guess (`Item="Orders$"`, the
`$` borrowed from ACE OLEDB's *table* naming convention) fails **identically**, because `Orders$` is
a different surface and appears nowhere in the navigation table. Both spellings produce the same
error, so a failed guess looks exactly like a correct one. Settle it with a throwaway probe table
whose M cannot fail on schema:

```
let
    Source = Excel.Workbook(File.Contents("...xls"), null, true),
    Line   = "COLUMNS: " & Text.Combine(Table.ColumnNames(Source), " ,"),
    Names  = List.Transform(Table.Column(Source, "Name"), each "Name=" & Text.From(_)),
    T      = Table.FromList(List.Combine({{Line}, Names}), Splitter.SplitByNothing(), {"Info"})
in  T
```

It returns the column list *and* every sheet name in one refresh (`COLUMNS: Name ,Data` /
`Name=Orders` / …), which is a fact rather than a hypothesis. Delete the probe afterwards.

### 7.2 Editing TMDL while Desktop has the `.pbip` open corrupts the session — and every later refresh HANGS

**The live model keeps serving the PRE-EDIT M.** Desktop loads the model into its child `msmdsrv`
at open; editing `definition/*.tmdl` on disk afterwards does **not** reload it. So an on-disk fix
appears to do nothing, and gets misdiagnosed as "the fix didn't work". Verify what is actually
running, never what is on disk:

```
SELECT [Name],[State],[QueryDefinition] FROM $SYSTEM.TMSCHEMA_PARTITIONS
```

**Worse, the edit can invalidate Desktop's package session**, which raises a modal:

> **Something went wrong** — `Could not find a PackageSession for the given sessionID.`

Once that modal is up, **every XMLA refresh blocks indefinitely instead of returning an error**
(observed >330 s, mistaken for a slow refresh — the same "a hang is a modal waiting on a human"
shape as §5, with no credential anywhere in sight; this source was a local file). The error that
*would* have been returned is never delivered, so a genuinely broken M reads as a timeout.

**Rules.** Close Desktop **before** editing TMDL, then reopen. **Do not use a graceful close here:**
it prompts to save, and saving writes the **stale in-memory model back over the corrected TMDL**,
silently undoing your fix. For an instance *you opened yourself to refresh a model you are editing on
disk*, a **force-kill** (`Stop-Process -Id <literal pid> -Force`) is the right close **once you have
confirmed there is no unsaved in-memory state worth keeping** (the cleanliness gate below); it also
discards live-only MCP *probe* objects (disposable scratch tables) as a free cleanup.

⚠️ **Authorise a force-kill by a positive cleanliness check, not by provenance.** Knowing you opened
the instance, and that `currentFilePath` is your `.pbip`, identifies *which* instance you hold — it
does **not** prove it is clean; an instance you opened can still accumulate unsaved changes. Read the
per-instance **`hasUnsavedChanges`** flag that `powerbi-desktop status` returns next to
`currentFilePath`:
- **`hasUnsavedChanges: false`** — the in-memory model holds nothing the disk lacks; force-kill is
  safe and your corrected TMDL loads on reopen. (Normal case: editing TMDL *on disk* does not dirty
  the Desktop document, it only makes the in-memory copy stale.)
- **`hasUnsavedChanges: true`** — unsaved in-memory state exists; do **not** force-kill on provenance
  alone, and do **not** scope the question to work *you did not create* — what matters is whether
  unsaved state exists, not whose it is. Force-kill only if you can *positively confirm* every unsaved
  change is already reproducible on disk (re-running this cycle's `_build/` script, and/or an
  `ExportToTmdlFolder` you have already run, rebuilds it exactly). Otherwise **stop and ask** whether
  to save/export or discard.

**The realistic source of unsaved work you authored yourself is the modeling MCP:** it writes
tables/measures/relationships **directly into the live Desktop model**, where they exist only in
memory (and read as `hasUnsavedChanges: true`) until `database_operations ExportToTmdlFolder`
persists them to the on-disk TMDL — a force-kill silently destroys any you have not exported. Mind
the interaction with the graceful-close hazard above: here the on-disk TMDL is the *corrected* copy
and the live model is *stale*, so neither a save nor an export can rescue in-memory work without
overwriting your fix; if you genuinely hold both an on-disk correction and unexported in-memory
changes you need, that is a real conflict — **stop and ask.** Never force-kill a sibling build's
instance (the pid-binding rule).

### 7.3 Scan for ALL modal windows, not just credential phrases

§5's UIA recipe filters descendant names to sign-in wording (`signed in|Personal Access|…`). That
filter **misses any other modal**, and any modal blocks refresh just as effectively. Here a
`Bing map visuals are going away` deprecation dialog — shown on **every open** of a report
containing Bing map visuals, so it recurs on every automated cycle — silently blocked the refresh,
and a credential-filtered scan reported "no modal found".

So **enumerate every child window and REPORT whether it is modal** (detect on `IsModal`, not on
text) — but **do not blanket-close them.** A modal can be a save prompt, an error dialog, an upgrade
nag, a security warning or a **credential/sign-in prompt**, and dismissing the wrong one destroys
work or silently defeats the stop-and-ask rule this bundle enforces elsewhere (§5: a missing
credential goes to a human, it is never worked around). Close only modals on an **explicit allowlist
of known, side-effect-free nuisance dialogs** — the `Bing map visuals are going away` deprecation
dialog is the canonical entry. **A credential/sign-in modal is NEVER auto-dismissed** — closing it
does not supply the credential, it just turns a recoverable stop-and-ask into a silent empty refresh;
surface the exact data source and escalate, exactly as §5 requires. Anything else not on the
allowlist: report it and leave it open for the caller to decide.

```powershell
# Auto-close ONLY these known, side-effect-free nuisance dialogs (they recur every cycle):
$modalAllowlist  = 'Bing map visuals are going away'
# Wording that marks a credential/sign-in modal - NEVER auto-close; escalate to a human (see §5):
$credentialModal = 'signed in|Personal Access|Sign in|credential|account|password'

$wcond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Window)
foreach ($d in $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $wcond)) {
    $wp   = $d.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern)
    $name = $d.Current.Name
    if (-not $wp.Current.IsModal) { continue }
    "MODAL: $name"                                     # always REPORT every modal found
    if ($name -match $credentialModal) {
        "  -> credential/sign-in modal: DO NOT close - escalate to a human (see §5)"; continue
    }
    if ($name -match $modalAllowlist) {
        $wp.Close()                                    # WindowPattern.Close beats clicking a button
        "  -> closed (allowlisted nuisance dialog)"
    } else {
        "  -> not on allowlist: left open, reporting it for the caller to decide"
    }
}
```

Dismiss via `WindowPattern.Close()` on the `WindowsForms10.*` host — invoking the WebView's own
`Close Dialog` button is unreliable. **Never** click an action button you did not intend
(`Upgrade to Azure Maps` would rewrite the report layer, which belongs to `pbi-report-builder`).

**Corollary — an idle `msmdsrv` is diagnostic.** During the "hang", the child `msmdsrv` had
accumulated only ~16 s CPU over an hour. A blocked-on-modal refresh is *idle*, not busy; a genuinely
slow refresh burns CPU. Check that before assuming you need to wait longer.
