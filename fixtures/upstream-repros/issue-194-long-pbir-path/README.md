# `issue-194-long-pbir-path` — a downloadable repro: one over-long nested path fails the whole PBIP

**What this shows.** The engine caps the *folder* bases it emits (`_MAX_FS_BASE = 64` in
`migrate_estate._fs_safe`), but it does **not** cap
`<model>.SemanticModel/definition/tables/<table>.tmdl`, whose name comes from the source table /
flat-file name. A workbook with an ordinary-looking long export filename therefore still emits a
**required** child past Power BI Desktop's MAX_PATH boundary — **at the skill's own short 22-unit
run root**, with `LongPathsEnabled = 1`. The `.pbip` pointer is short and legal; the project still
cannot open.

Nothing in this fixture contains customer, company, host, credential or private-path data, and a
test enforces that as an **allowlist** (exactly one root `.twb` + one `Data/<dir>/<file>.csv`, and an
XML tree carrying no location, identity or secret element or attribute).

## Engine-version provenance — read before quoting a number

| what | version | status |
|---|---|---|
| the measurements and the Desktop A/B below | canonical **2.368.0**, on **Windows** | ✅ measured locally |
| the repository's **required** engine-integration job | pinned **2.356.0**, on **ubuntu-latest** | ⚠️ **whether this fixture reproduces there is UNVERIFIED** |
| scheduled / manual drift runs | current upstream `main`, on **ubuntu-latest** | not exercised here |

This fixture PR deliberately does **not** roll the repository's pinned engine. The engine-dependent
test is written to survive either: it asserts the **boundary** unconditionally, and asserts the
**specific uncapped table-file offender** only at or above `2.368.0`, recording the observed version
in both directions. `measure_repro.py` writes the observed `engine_version` into its JSON.

⚠️ **HOST provenance matters too, and one claim is host-scoped.** The emitted relative paths — and
therefore every length, offender and ceiling verdict below — are host-independent, so those claims
are asserted on any runner. The engine's **MAX_PATH warning is not**: canonical 2.368.0 guards it
with `if os.name == "nt" and len(projected) >= MAX_PATH:`
(`skills/tableau-migration/scripts/migrate_estate.py`), so a **Linux** run emits nothing however long
the projected path is. The warning quoted below is a **measured Windows 2.368.0 observation**; the
test asserts it only there and merely records it elsewhere, claiming neither presence nor absence off
Windows.

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

⚠️ **The archives are built line-ending-normalised and ZIP-`STORED`, and the first of those is the
one that was actually broken.** `build_repro.py --check` must hold on *any* machine. It did not:
`src/regional_sales.csv` is **156 bytes with CRLF** in a Windows working tree (`core.autocrlf=true`)
and **151 bytes with LF** in the git blob a Linux runner checks out, and the builder read it with
`read_bytes()` — so the archive contained a literally different member on each platform. The builder
now reads every source through one normalising helper (`payload_bytes`), which is what the `.twb`
template already got for free from `read_text()`. Storing rather than deflating removes a *second*
class of host dependence (a DEFLATE stream depends on the linked zlib build); its contribution was
never isolated, and it is kept as hardening rather than as the fix.

## Reproduce

```
python fixtures/upstream-repros/issue-194-long-pbir-path/build_repro.py --check     # archives are reproducible
python fixtures/upstream-repros/issue-194-long-pbir-path/measure_repro.py ^
    --engine <engine-plugin-root> --runs-parent C:\tfmig\i194 --json census.json
```

⚠️ **`measure_repro.py` never deletes anything.** It allocates a fresh four-digit run directory under
`--runs-parent` with an atomic exclusive `mkdir`, steps over any id already on disk, and prints what
it allocated. The default parent is `C:\tfmig\i194` — this fixture's own — deliberately **not** the
shared `C:\tfmig\runs`.

Exit codes are the verdict: **0** measured and the documented A/B held · **2** INVALID/UNMEASURED ·
**3** measured cleanly but the A/B did not hold · **64** usage. A non-zero exit never prints a clean
verdict; a missing engine, an unreadable path, an empty tree, or a missing/ambiguous `.pbip` are all
INVALID rather than "within ceilings".

Or run the engine by hand:

```
python <engine>\skills\tableau-migration\scripts\migrate_estate.py -i <run>\in -o <run>\out
```

## Expected measurement

Engine **exit 0** in both cases.

⚠️ **The engine is not silent on Windows — and its two remedies are not equivalent.** Measured on
**Windows** with canonical **2.368.0**, the long case prints:

```
[WARN] manual attention required: workbook .pbip output path is 273 chars, at/over the Windows
MAX_PATH (260) limit -- the build proceeds via long-path (\\?\) writes, but to OPEN this .pbip
locally in Power BI Desktop re-run with a shorter output root (e.g. -o C:\tfmig) or enable Windows
long paths
```

Five things about it are worth an upstream maintainer's attention:

1. it is **Windows-only** — the emitter is guarded by `os.name == "nt"`, so the same run on Linux
   emits nothing at all while producing the identical over-long path. The path defect is
   cross-platform; the warning is not;
2. it does **not** change the exit code — the run reports success;
3. it says *"workbook **.pbip** output path is 273 chars"*, but the `.pbip` here is **162**. The 273
   belongs to the deepest emitted child, not to the pointer the sentence names;
4. *"or enable Windows long paths"* — **does not work.** Every measurement here was taken with
   `LongPathsEnabled = 1` and Desktop refused anyway (Desktop enforces its own limit in managed
   code);
5. *"a shorter output root (e.g. `-o C:\tfmig`)"* — ⚠️ **this one is arithmetically true, and an
   earlier revision of this README wrongly said it was not.** The relative tail is fixed at **250**
   units, so the verdict is a pure function of the root length:

   | output root | length | deepest file | verdict |
   |---|---:|---:|---|
   | `C:\tfmig\runs\NNNN\out` — the ordinary skill run output root | **22** | **273** | ❌ over 259 |
   | `C:\tfmig` — the container root itself | **8** | **259** | ✅ exactly legal |

   So `-o C:\tfmig` does avoid *this fixture's* boundary. It is an **extreme placement workaround,
   not long-path support**: it abandons the canonical `runs/<id>/{in,out}` layout, writes engine
   output directly into the container root beside every other run, leaves **zero** headroom (one
   more character anywhere and it fails again), and may simply be unavailable on a managed VDI where
   the writable location is fixed and deeper. The defect is that a required child is 250 units long,
   not that operators picked the wrong folder.

| | long | short |
|---|---:|---:|
| output root | 22 units | 22 units |
| entries emitted | 51 (27 files, 24 dirs) | 51 (27 files, 24 dirs) |
| unreadable / undecodable | 0 | 0 |
| **longest FILE** | **273** (ceiling 259) ❌ | **165** ✅ |
| longest DIRECTORY | 239 (ceiling 247) ✅ | 153 ✅ |
| `.pbip` pointer | 162 ✅ | 76 ✅ |
| offenders | **1 file, 0 directories** | 0 |
| engine MAX_PATH warning (**Windows only**) | yes | no |

The single offender is a **required** child of the semantic model:

```
<root>\pbip\
  Regional Sales Performance and Inventory Turnover Revie-b52b9462\                    64  (capped)
  Regional Sales Performance and Inventory Turnover Conso-980f2851.SemanticModel\      64+14 (capped)
  definition\tables\
  Regional Sales Performance and Inventory Turnover FY2026 Q3 Detail Extract.csv.tmdl  83  (NOT capped)
```

Its relative tail is **250** units, so this project fits only under a root of **8 characters or
fewer** — `C:\tfmig` exactly, and nothing deeper (see the root-length table above). Under the
repository's canonical `<repo>\_runs\<NNN>-<slug>\bundle` shape (71) the same archive measures
**file 322 / dir 288**. **The upstream repro target is the 22-unit `C:\tfmig\runs\NNNN\out` shape**,
because it is the tool's own default and it already fails.

## Power BI Desktop, A/B

Long case — double-click the `.pbip`. Desktop shows a modal **"Issues were found"** naming the exact
path, the window title stays `Untitled - Power BI Desktop`, and the Desktop Bridge reports
`bridgeStatus: error` / *"Host is not ready to accept operations"*:

> Cannot read `…\pbip\Regional Sales Performance and Inventory Turnover Revie-b52b9462\Regional Sales
> Performance and Inventory Turnover Conso-980f2851.SemanticModel\definition\tables\Regional Sales
> Performance and Inventory Turnover FY2026 Q3 Detail Extract.csv.tmdl'. The specified path, file
> name, or both are too long. The fully qualified file name must be less than 260 characters, and the
> directory name must be less than 248 characters.

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
| `measure_repro.py` | allocates a fresh run, invokes the canonical engine, censuses every emitted path, and refuses to report an unmeasurable run as clean |
| `*.twbx` | the two downloadable archives |
