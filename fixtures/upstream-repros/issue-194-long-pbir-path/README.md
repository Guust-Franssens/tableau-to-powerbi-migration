# `issue-194-long-pbir-path` — a downloadable repro: one over-long nested path fails the whole PBIP

**What this shows.** The engine caps the *folder* bases it emits (`_MAX_FS_BASE = 64` in
`migrate_estate._fs_safe`), but it does **not** cap
`<model>.SemanticModel/definition/tables/<table>.tmdl`, whose name comes from the source table /
flat-file name. A workbook with an ordinary-looking long export filename therefore still emits a
**required** child past Power BI Desktop's MAX_PATH boundary — **at the skill's own short
`C:\tfmig\runs\NNNN\out` run root**, with `LongPathsEnabled = 1`. The `.pbip` pointer is short and
legal; the project still cannot open.

Measured on canonical engine **2.368.0**, Power BI Desktop **2.157.828.0**, Windows with
`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1` and `git core.longpaths`
unset. Nothing in this fixture contains customer, company, host, credential or private-path data.

## The pair

| | long case | short control |
|---|---|---|
| archive | `Regional Sales Performance and Inventory Turnover Review FY2026 Q3 Final.twbx` | `Regional Sales FY26Q3.twbx` |
| workbook stem | 72 units | 21 units |
| flat file inside | `Regional Sales Performance and Inventory Turnover FY2026 Q3 Detail Extract.csv` (78) | `regional_sales.csv` (18) |
| data | identical 4 rows | identical 4 rows |
| worksheet / dashboard / marks / encodings | identical structure | identical structure |

Both archives are generated from **one** template, `src/workbook-template.twb`, by substituting only
the four identity placeholders `@@DATASOURCE@@`, `@@DASHBOARD@@`, `@@WORKSHEET@@`, `@@CSVFILE@@`.
The name set is the only variable.

## Reproduce

```
python fixtures/upstream-repros/issue-194-long-pbir-path/build_repro.py --check    # archives are reproducible
python <engine>/skills/tableau-migration/scripts/migrate_estate.py ^
    -i C:\tfmig\runs\9194\in -o C:\tfmig\runs\9194\out
```

or run both cases and census every emitted path in one step:

```
python fixtures/upstream-repros/issue-194-long-pbir-path/measure_repro.py ^
    --engine <engine-plugin-root> --runs-parent C:\tfmig\runs
```

Engine exit is **0** in both cases: nothing warns, and every file is written correctly (the writer
is long-path aware).

## Expected measurement

| | long | short |
|---|---:|---:|
| output root | `C:\tfmig\runs\9194\out` (22) | `C:\tfmig\runs\9195\out` (22) |
| entries emitted | 51 (27 files, 24 dirs) | 51 (27 files, 24 dirs) |
| unreadable / undecodable | 0 | 0 |
| **longest FILE** | **273** (ceiling 259) ❌ | **165** ✅ |
| longest DIRECTORY | 239 (ceiling 247) ✅ | 153 ✅ |
| `.pbip` pointer | 162 ✅ | 76 ✅ |

The single offender is a **required** child of the semantic model:

```
C:\tfmig\runs\9194\out\pbip\
  Regional Sales Performance and Inventory Turnover Revie-b52b9462\        <- 64, capped
  Regional Sales Performance and Inventory Turnover Conso-980f2851.SemanticModel\   <- 64, capped
  definition\tables\
  Regional Sales Performance and Inventory Turnover FY2026 Q3 Detail Extract.csv.tmdl   <- 83, NOT capped
```

Its relative tail is **250** units, so this project fits only under a root of **8 characters or
fewer** — i.e. effectively nowhere. Under the repository's canonical
`<repo>\_runs\<NNN>-<slug>\bundle` shape (71) the same archive measures **file 322 / dir 288**.
**The upstream repro target is the `C:\tfmig\runs\NNNN\out` shape**, because it is the tool's own
default and it already fails.

## Power BI Desktop, A/B

Long case — double-click the `.pbip`. Desktop shows a modal **"Issues were found"** naming the exact
path, the window title stays `Untitled - Power BI Desktop`, and the Desktop Bridge reports
`bridgeStatus: error` / *"Host is not ready to accept operations"*:

> Cannot read `C:\tfmig\runs\9194\out\pbip\Regional Sales Performance and Inventory Turnover
> Revie-b52b9462\Regional Sales Performance and Inventory Turnover
> Conso-980f2851.SemanticModel\definition\tables\Regional Sales Performance and Inventory Turnover
> FY2026 Q3 Detail Extract.csv.tmdl'. The specified path, file name, or both are too long. The fully
> qualified file name must be less than 260 characters, and the directory name must be less than 248
> characters.

Short control — same double-click, same machine: Desktop opens, the bridge reports
`bridgeStatus: connected`, page `Regional Sales Review` enumerates, and after a refresh the model
returns real rows (`North, 2026-Q1, 120, 48250.0`) and the bar chart renders
East > North > South > West.

## Why the folder cap does not save it

`_MAX_FS_BASE = 64` is applied by `_fs_safe` to the report and model **folder** bases, and its own
comment explains why ("a 77-char title pushed `visual.json` to 278 chars … Power BI Desktop could
not READ them"). With the cap in force the deepest **report** path here is 251 at this root — inside
the ceiling with 8 units to spare. The model's table **file** name is not passed through the same
cap, so it spends 83 units in a single component and overruns on its own.

## Files

| file | what |
|---|---|
| `src/workbook-template.twb` | the one source of both workbooks; four identity placeholders |
| `src/regional_sales.csv` | 4 rows, 4 columns, synthetic |
| `build_repro.py` | deterministic packager; `--check` verifies the committed archives |
| `measure_repro.py` | runs the canonical engine on both cases and censuses every emitted path |
| `*.twbx` | the two downloadable archives |
