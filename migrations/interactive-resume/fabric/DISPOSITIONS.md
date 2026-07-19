# Semantic-model dispositions — InteractiveResume

Migration of `MariaBrock-InteractiveResume.twbx` → `InteractiveResume.SemanticModel` (PBIP/TMDL).

This is a **spatially-composed "interactive resume" infographic**, not an analytical dashboard. It is
built from **6 tiny, independent Tableau data sources** (extract-based `.hyper`, materialized to CSV),
**25 worksheets** (each a single filtered mark or a small per-row viz), **1 dashboard**, and — the
headline — **14 dashboard actions that are ALL URL/hyperlink actions**. There are **0 joins, 0 data
blends, 0 filter/highlight/navigation/parameter actions, and 0 Tableau parameters.**

**Model shape:** 6 import tables · 40 columns · 6 measures · **1 (inactive) relationship**. Faithful
to the source: the 6 sources are data-independent, so the model is essentially **6 islands** + one
documented, dormant cross-table key.

---

## 1. Table map (Tableau data source → TMDL table)

| Tableau data source (caption) | TMDL table | Rows | Bound worksheets |
|---|---|---:|---|
| `Sheet1 (fun facts)` | **Fun Facts** | 7 | box, pianobar, voicebar, theaterbar, whbar, frisbeebar, myersbar |
| `Sheet1 (Coursework)` | **Coursework** | 14 | Favorites |
| `Sheet1 (Coursework) (2)` | **Coursework Detail** | 14 | 11, 22, 33, 44, Coursework |
| `Sheet1 (Fun Facts_Resume)` | **Fun Facts Resume** | 5 | Background, gmail, linked, tab, twitter |
| `Sheet1 (Maj_Min_Resume)` | **Major Minor Resume** | 3 | Bubbles |
| `Sheet1 (Project Badges_Resume)` | **Project Badges Resume** | 6 | AD, DRP, DTC, DVP, SD, TA |

Table names drop the meaningless `Sheet1` prefix (the underlying Excel tab) and use the parenthetical
concept (mirrors the electricity-per-capita precedent of clean display names). `Coursework` and
`Coursework Detail` are **near-duplicates** (same 14 Courses); both are kept because distinct
worksheets bind each — `Coursework Detail` is the superset (adds Course Descrip, InternTitle values,
Index, N). Per the near-duplicate gotcha, reconciled by **worksheet binding**, not row content.

---

## 2. Calculated-field dispositions (every calc field)

The parser left `formula: null` (a known parser gap); all formulas were recovered from the `.twb` XML.
There are **18 calc-bearing `<column>` nodes → 10 distinct calc fields**. None are LOD or table calcs.

### 2a. Data-source calc fields → **measures** (6)

| Tableau field | Formula | Fate | Target table | Measure name |
|---|---|---|---|---|
| `Number of Records` ×6 | `1` | **measure** | each of the 6 tables | `<Table> Row Count` = `COUNTROWS('<Table>')` |

Tableau auto-generates one `Number of Records` (=1) measure **per data source**. Six identically-named
measures **crash Power BI Desktop on load** ("a Measure with the same name already exists"), and this
is invisible to offline `TmdlSerializer` deserialization. Each is therefore **renamed** to a unique
`'<Table> Row Count'` (e.g. `Fun Facts Row Count`), hidden, `displayFolder: Row Counts`. Ground-truthed
below.

### 2b. Worksheet-level calc fields → **dropped** (4 distinct, report-layer)

| Tableau field (internal) | Formula | Fate | Reason |
|---|---|---|---|
| `Calculation_879046380311916547` | `"."` | **dropped** | Constant string literal; single-mark placeholder on the *Background* text worksheet. Report-layer decoration; no data-model meaning. |
| `Calculation_2596888163891249153` | `" "` | **dropped** | Constant space; label placeholder on *Favorites*. Report-layer. |
| `Calculation_879046380873080839` | `" "` | **dropped** | Constant space; detail placeholder on the *Fun Facts* bar worksheets (pianobar/theaterbar/…). Report-layer. |
| `Calculation_879046380664172549` | `" "` | **dropped** | Constant space; detail placeholder on the *Fun Facts Resume* icon worksheets (gmail/tab/twitter/linked). Report-layer. |

These are Tableau's idiom for placing **a single decorative mark without a real dimension** (a constant
"detail"/"label" so exactly one mark renders). In Power BI a decorative shape/text/image is placed
directly by the report builder — no calc field required. **Semantic model: nothing to create.**

---

## 3. Relationships — candidate-key analysis

Every cross-table key overlap was measured from the data (not eyeballed). **No key is used by any
Tableau join, blend, or action** (see §4), so every relationship below would be a *net-new* Power BI
construct, not a migration of existing behavior.

| Candidate key | Overlap (data) | Cardinality | Shipped? | Reason |
|---|---|---|---|---|
| `Fun Facts Resume`[Fact] ↔ `Fun Facts`[Title] | 5 shared (Piano, Singing, Theater, White House, Ultimate Frisbee); Title has +Myers Briggs +1 blank orphan | **1:1** (both unique) | **YES — inactive** | The only genuine conceptual link (both = "fun facts/hobbies"). Shipped `isActive: false`, `crossFilteringBehavior: bothDirections`: captured as the model's one real cross-table key, but **dormant** so it imposes no fabricated interactivity by default (see below). |
| `Coursework`[Course] ↔ `Coursework Detail`[Course] | 14 shared (all) | 1:1 | no | Near-**duplicate** tables, not dimension→fact. A 1:1 between two copies of the same 14 courses adds no analytical value and risks double-count confusion. Documented as near-duplicate instead. |
| `Major Minor Resume`[Title] ↔ `Coursework`[Category] | 1 shared (`Economics` only) | — | no | Conceptually related (majors/minors vs course categories) but keys don't align: `Data Analysis-Statistics` ≠ `Data Analysis` + `Statistics`. Wiring it would orphan 2 of 3 majors — a bad relationship. |
| `Project Badges Resume`, `Major Minor Resume` vs others | 0 shared string keys | — | no | Genuine islands. |

**Why the one relationship is inactive.** All 14 dashboard actions are URL actions (§4) — there is **no
Tableau filter/highlight action to reproduce**, so no relationship is *required*. Additionally, the
Fun Facts Resume worksheets (gmail/tab/twitter/linked/Background) are **constant-driven single marks
that don't visually bind `Fact` at all**, so an active cross-highlight would have little visible effect.
Shipping it **active** would fabricate interactivity the source never had. Shipping it **inactive**
records the one real shared key (model is "well-related" where the data supports it) while keeping the
static infographic faithful. `pbi-report-builder` may **activate it** if an optional Piano↔Piano-style
cross-highlight enhancement is desired.

> ⚠️ **New idiom for the repo — "dashboard actions are a report-layer concern."** In this workbook the
> *entire* interactivity budget is URL actions, which have **no semantic-model footprint**. The
> model's job is only to (a) provide clean, correctly-typed tables and (b) record candidate cross-table
> keys; **all** action behavior is delegated to the report layer. This is the opposite of the "wire
> relationships + cross-filter direction to reproduce filter actions" pattern — there were simply no
> filter actions to reproduce.

---

## 4. Dashboard-action → native-Power-BI interactivity map

**15 raw `<action>` elements** in the `.twb` (the parser reports **14** after de-duplicating one
orphaned copy — `[Action6]`, a duplicate of the Theaterbar link with no `dashboard=` attribute).
**All are `url` (hyperlink) actions** — clicking a mark opens an external website. **None** is a filter,
highlight, go-to-sheet/dashboard, or parameter action.

Native mapping: a URL action = a **report-layer Web URL** in Power BI (a column with
`dataCategory: WebUrl` rendered as a clickable link, or a Button/Image with a **Web URL** action).
`dataCategory: WebUrl` is set on `Fun Facts`[Links] and `Project Badges Resume`[Links] — the natural
home for the 9 action URLs that live in the data.

| # | Source ws | Table | URL in a `[Links]` column? | Native PBI treatment |
|---:|---|---|---|---|
| 1 | DTC | Project Badges Resume | no — 2nd link, hardcoded | Button/Image Web URL action (report) |
| 2 | tab | Fun Facts Resume | no — table has no Links column | Button/Image Web URL action (report) |
| 3 | WHbar | Fun Facts | **yes** → `[Links]` (WebUrl) | Clickable link / bound button |
| 4 | frisbeebar | Fun Facts | **yes** | Clickable link / bound button |
| 5 | myersbar | Fun Facts | **yes** | Clickable link / bound button |
| 6 | pianobar | Fun Facts | no — data `[Links]="link"` placeholder | Button Web URL action (report) |
| 7 | Theaterbar | Fun Facts | **yes** | Clickable link / bound button |
| 8 | linked | Fun Facts Resume | no — no Links column | Button/Image Web URL action (report) |
| 9 | TA | Project Badges Resume | **yes** | Clickable link / bound button |
| 10 | DTC | Project Badges Resume | **yes** | Clickable link / bound button |
| 11 | SD | Project Badges Resume | **yes** | Clickable link / bound button |
| 12 | Theaterbar | Fun Facts | **yes** (duplicate of #7) | — (dedup) |
| 13 | voicebar | Fun Facts | **yes** | Clickable link / bound button |
| 14 | twitter | Fun Facts Resume | no — no Links column | Button/Image Web URL action (report) |
| 15 | pianobar | Fun Facts | no — data `[Links]="link"` placeholder (2nd YouTube link) | Button Web URL action (report) |

**Totals:** 9 URLs are carried by a `[Links]` column (WebUrl handles them natively); **6 are
hardcoded/placeholder** and must be supplied by `pbi-report-builder` as explicit Button/Image **Web URL**
actions — specifically the 3 Fun Facts Resume icons (tab, twitter, linked, whose table has **no** Links
column), both pianobar YouTube links (data `[Links]="link"`), and the DTC GMU-news second link.

❌ **Report-layer limitations to hand to `pbi-report-builder`** (also appended to
`migration-spec.json → limitations_encountered`):
- All 14/15 URL actions are a report concern; the model only provides `WebUrl` link columns.
- pianobar carries **two** distinct URL actions on one mark, and the Fun Facts Resume icons carry URLs
  **not present in the data** — the report builder must author explicit Web URL buttons for these 6.

---

## 5. Column `summarizeBy` decisions

Only the **3 genuinely additive metrics** actually SUM-aggregated by a worksheet are `summarizeBy: sum`;
everything else (identifiers, years, indexes, labels) is `summarizeBy: none`, even where the Tableau
spec role was `measure` (e.g. `Project Badges Resume`[Year], `Coursework Detail`[Index]/[N] — summing a
year or a row-index is meaningless).

| Table | `sum` columns | Evidence |
|---|---|---|
| Fun Facts | `Value` | `box` worksheet: rows = `SUM(Value)` by `Year` |
| Fun Facts Resume | `Fact Sum` | resume fun-fact weight |
| Major Minor Resume | `Size` | `Bubbles` worksheet: Size encoding |

---

## 6. Ground-truth (vs CSVs / extract manifest)

All checks **MATCH** (offline; measures are `COUNTROWS`/`SUM`, so CSV = engine output by definition):

- **Row-count measures:** Fun Facts=7 · Coursework=14 · Coursework Detail=14 · Fun Facts Resume=5 ·
  Major Minor Resume=3 · Project Badges Resume=6 (all = `extract_manifest.json`).
- **Base aggregations:** `SUM('Fun Facts'[Value])` = **14** · `SUM('Fun Facts Resume'[Fact Sum])` =
  **21** · `SUM('Major Minor Resume'[Size])` = **5**.
- **Relationship key overlap:** 5 matched keys; Title-side orphans = {Myers Briggs} + 1 blank; Fact-side
  orphans = {}; both columns unique → 1:1 valid.

---

## 7. Structural validation

`TmdlSerializer.DeserializeDatabaseFromFolder` (the parser Power BI Desktop uses) → **OK**
(compatibilityLevel 1606, 6 tables, 1 relationship). Integrity: **[PASS]** model-wide measure-name
uniqueness (6 measures) · **[PASS]** all DAX bracket tokens resolve · **[PASS]** no measure==column
collision · every `sourceColumn` matches its CSV header exactly (no refresh "column cannot be found"
risk). Power BI Desktop is not installed on this machine, so a live `EVALUATE` was not run; the model is
trivial enough (`COUNTROWS`/`SUM`) that the CSV-derived numbers are authoritative.

Regenerate: `python migrations/interactive-resume/build/generate_semantic_model.py`
Validate: `pwsh -File migrations/interactive-resume/build/validate_tmdl.ps1`

---

## 8. AI / Copilot readiness (final build phase)

Every table, column, and measure carries a business-meaning-first description, and every
categorical/dimension column enumerates its domain values — this is what DAX Copilot reads
(first ~200 chars of each description) to disambiguate fields and resolve natural-language
category filters.

**Coverage — 100% (47/47 objects):** 6/6 tables · 35/35 columns · 6/6 measures.
Verified by `build/check_ai_readiness.py`.

**14 categorical columns enumerate their domain ("One of: ..."):**

| Column | Domain (distinct non-blank values) |
|---|---|
| `Fun Facts[Title]` | Myers Briggs, Piano, Singing, Theater, Ultimate Frisbee, White House (6) |
| `Fun Facts[TYPE]` | HOBBY, FUN FACT (2) |
| `Coursework[Category]`, `Coursework Detail[Category]` | Economics, Data Analysis-Statistics (2) |
| `Coursework[Abrev Cat]`, `Coursework Detail[Abrev Cat]` | Economics, Data/Stats (2) |
| `Coursework[Course]`, `Coursework Detail[Course]` | 14 course names (Econometrics, Biostatistics, …) |
| `Coursework Detail[Intern Title]` | Client Delivery, Development, PostgreSQL, Tableau Server (4) |
| `Fun Facts Resume[Fact]` | Piano, Singing, Theater, Ultimate Frisbee, White House (5) |
| `Major Minor Resume[Title]` | Economics, Data Analysis, Statistics (3) |
| `Major Minor Resume[Type]` | Major, Minor (2) |
| `Project Badges Resume[Abbrev]` | CD, DRP, DTC, DVP, SD, TA (6, expanded to full names) |
| `Project Badges Resume[Title]` | Choir Director, Data Redesign Project, … (6) |

Free-text (`Description`, `Course Descrip`), URL (`Links`), key (`ID`), and numeric columns
are described by role + unit/grain (no enum) — as intended.

**Mechanism (offline, no Power BI Desktop):** Power BI Modeling MCP — `ConnectFolder` (loads
the model from TMDL offline) → batch `table/column/measure Update` with the `description`
field → `database ExportToTmdlFolder` back to `definition/`. The export applied a one-time
cosmetic reformat (identifier quoting normalized to unquoted where legal, property re-order);
all content, `lineageTag`s, DAX, `dataCategory: WebUrl`, `summarizeBy`, and M partitions were
preserved, and `TmdlSerializer` still deserializes cleanly (compat 1606, 6 tables, 1 rel; all
integrity checks pass). Descriptions were then **back-ported into `generate_semantic_model.py`**
(now description-aware) and a temp-dir regeneration was **parity-checked against the committed
model: 0 diffs across all 47 objects** — so the generator remains the single re-runnable source
of truth and will not silently drop descriptions on a future regenerate.

Verify readiness: `python migrations/interactive-resume/build/check_ai_readiness.py`

**Deferred to post-deploy / report layer (not reliably committable in TMDL today):**
- **Synonyms** (linguistic schema / `LinguisticMetadata`): the available Modeling-MCP tools
  expose `description` but no synonym/culture-schema write operation, and hand-authoring the
  linguistic JSON-in-TMDL is error-prone. Abbreviation meanings are instead folded into the
  descriptions (e.g. "Abbreviated subject area", "Internship work-stream", badge codes expanded
  to full names), which is what the DAX-generation path reads. A formal Q&A synonym set is a
  low-cost future enhancement once a stable authoring surface is available.
- **AI data schema** and **AI instructions**: live in service LSDL with no stable file-authoring
  contract — set post-deploy in the Fabric/Power BI service.
- **Verified answers**: not Git-supported; require report visuals — author after the report ships.
- **"Approved for Copilot" + semantic indexing**: tenant/workspace runtime settings, not model files.
