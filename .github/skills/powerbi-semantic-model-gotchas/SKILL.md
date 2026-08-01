---
name: powerbi-semantic-model-gotchas
description: Hard-won Power BI semantic-model gotchas - TMDL hand-authoring pitfalls that crash Desktop on open, the field-parameter sourceColumn bracket trap, the PLACEHOLDER measure-filter error, MCP/Desktop operational rules, offline model-integrity checks, and table-calculation patterns. Use before hand-authoring TMDL or DAX, and whenever a model validates clean but fails at open, refresh or render. Source-tool agnostic (Tableau, Qlik, Cognos to Power BI).
---

# Power BI semantic-model gotchas

Every entry below cost a real debugging cycle on a real migration.

**The rule that generates most of this file:** structural validation is **necessary, not sufficient**.
`TmdlSerializer.DeserializeDatabaseFromFolder` is the same parser Power BI Desktop uses, and it still
passes models that crash on open, silently fail to bind, or throw only at query time. Every section
below is a defect class that survived a clean parse.

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
- **Always pass an explicit culture to M type-conversion calls** (`Table.TransformColumnTypes`,
  `Number.FromText`, `Date.FromText` — e.g. `Table.TransformColumnTypes(#"prior step", {...},
  "en-US")`). This is cheap insurance against a real failure mode: on a machine with a non-standard
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
  offline; the window-function alternatives (`OFFSET`/`INDEX`/`WINDOW`) need compat **1702+ and a live
  Desktop** to author/verify, so don't ship them when you can't ground-truth them. Verified patterns:
  `LOOKUP(agg,FIRST()/LAST())` → per-partition MIN/MAX-date helper calc column; `INDEX()` →
  `CALCULATE(COUNTROWS(t),FILTER(ALLEXCEPT(t,[part]),t[order]<=EARLIER(t[order])))`; `IF MIN(Date)=LOOKUP(MIN(Date),LAST())`
  → an is-last-row guard. See `docs/tableau-dax-translation-guide.md` §5–6.
- **Ground-truth EACH table calc two independent ways in Python** (Tableau semantics via sorted-partition
  `.iloc`/`cumcount`, and a literal DAX-mechanics replica via boolean masks over the raw table) and assert
  equality per probe row — two independent codings agreeing is far stronger than restating one formula.
- **A running total must clear EVERY date-ish column of its own table, not just the daily one** [seen,
  LogisticsLive]. The canonical `CALCULATE(SUM(t[v]), FILTER(ALL(t[Date]), t[Date] <= MAX(t[Date])))` is
  only correct while `t[Date]` is the axis. Put a coarser same-table column on the axis — a month-start
  column you added so the report can reproduce a Tableau `mn`/`tmn` bin — and the surviving `t[Month]`
  filter still restricts the fact rows, so the "running total" silently collapses to **that bucket's own
  total**: a plausible-looking, non-cumulative line that no structural validation flags. Fix:
  `VAR _asOf = MAX(t[Date]) RETURN CALCULATE(SUM(t[v]), FILTER(ALL(t[Date], t[Month]), t[Date] <= _asOf))`
  — `ALL()` takes several columns of one table, and the `VAR` pins the as-of date in the outer context.
  Measured on a 730-row table: old form returned exactly the month total for all 24 months, new form
  accumulated to the grand total to the cent, and the two agreed on **0 of 730 days** of difference at the
  daily grain (strict superset). Generalizes to any measure that removes a filter by naming one column.

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

**Cross-agent — the report builder needs these FROM you (decide at model-design time):**
- **Azure Map route/great-circle maps (Tableau `MAKELINE`/`MAKEPOINT`): build an endpoint-unpivoted PATH
  table** (one row per endpoint, with a shared path id + point order) so the report can feed azureMap's
  `PathID`+`PointOrder` wells. Origin+destination lat/long as four columns on a single fact row **cannot**
  draw an arc — the report is then stuck with endpoint bubbles. This is a model-shape decision, not a
  report one.
- **Provision EVERY dashboard-visible metric.** If a Tableau dashboard shows a KPI tile/value, the model
  must have a backing measure or column for it — the report builder works against a *frozen* model and can
  only render a static placeholder card for a metric that has no backing field (seen: 3 Airline tiles).
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
