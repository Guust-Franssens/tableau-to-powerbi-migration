---
name: pbip-model-refresh
description: Refresh a local PBIP/TMDL semantic model open in Power BI Desktop, and persist the result to .pbi/cache.abf (headlessly via AMO Server.ImageSave, UI-Automation fallback). Persisting is the DEFAULT - opt OUT with `--no-save` for read-only or validate-then-deploy work; saving aligns database.tmdl's declared compatibilityLevel so the cache stays loadable, and the cache write is staged and swapped atomically. Use after finishing TMDL edits, before handing a model to report authoring, or whenever Desktop reopens a migrated model empty. Source-tool agnostic - the model is already Power BI, so this applies equally to Tableau, Qlik and Cognos migrations.
---

# Refresh a local PBIP model, and make the data survive a close

**Windows only.** Talks to the local Analysis Services instance that Power BI Desktop hosts, over
ADOMD.NET/AMO (pythonnet), plus the Desktop Bridge CLI for process to file mapping.

Requirements, in full: Windows with Power BI Desktop, Python >= 3.11 with `pythonnet`, the ADOMD.NET
and AMO client libraries under `~/.nuget/packages`, and `npx` for
`@microsoft/powerbi-desktop-bridge-cli`. No other dependency, and nothing repo-specific.

## Available scripts

- [**`scripts/refresh_pbip_model.py`**](scripts/refresh_pbip_model.py) - refresh the open model,
  refuse a wrong-instance bind, and persist `cache.abf` (AMO `ImageSave`, UI-Automation fallback).
- [**`scripts/probe_desktop_query.py`**](scripts/probe_desktop_query.py) - read-only preflight: port
  discovery, ADOMD loading, table listing, and the one-row DAX probe. `refresh_pbip_model.py`
  imports from this file and from nothing else.
- [**`scripts/probe_desktop_credential.ps1`**](scripts/probe_desktop_credential.ps1) - the arbiter a
  refresh TIMEOUT names: a UI-Automation check for a data-source sign-in modal, so "slow" and
  "blocked" are told apart by evidence, not guessed. Ships **inside** the bundle, so the instruction
  the script prints at runtime resolves to a file that is actually here.
- [**`tests/`**](tests) - the regression suite for both, runnable from this folder
  (`pytest tests`). It is what makes the portability claim below checkable rather than aspirational.

## The problem this solves

A migrated model hands over as *TMDL plus a promise*. Two things go wrong:

1. **Refreshing is hand-rolled every time.** The only working path against a local PBIP is TMSL over
   the child `msmdsrv` port: discover the port, load ADOMD.NET, resolve the catalog GUID, send
   `refresh`. Re-deriving that per migration is slow and easy to get subtly wrong.
2. **An XMLA refresh is not persisted.** It populates the *in-memory* model only. Desktop keeps a
   PBIP's data in `<Name>.SemanticModel/.pbi/cache.abf`, and if nothing writes that file the next
   person opens an empty model and hits the credential prompt all over again.

## Run it

```
python scripts/refresh_pbip_model.py [--pid <pbidesktop-pid>] [--tables "A" "B"]
                                     [--no-save] [--verify-only] [--ui-save]
```

**Persisting is the DEFAULT** — that is this script's stated purpose, *"so the next agent (and the
next Desktop open) sees real data instead of an empty model"*. Pass **`--no-save`** for read-only
work (the validator is read-only **by contract**) or for validate-then-deploy, where the project must
stay byte-identical.

> ⚠️ **Saving ALIGNS `database.tmdl`, and that alignment is what makes it work.**
> **Root-caused 2026-08-07.** The earlier finding (3-vs-3 on 2026-08-04: a persisted `cache.abf`
> made the PBIP open as **"Untitled - Power BI Desktop"** with the bridge reporting
> `Host is not ready to accept operations`) was **real but mis-attributed**. Persisting is not
> inherently destructive; persisting a cache whose compatibility level **disagrees with the
> project's** is.
>
> **The mechanism.** Desktop silently runs the database at a HIGHER level than the project declares —
> measured live `Database.CompatibilityLevel: 1606` against a `database.tmdl` of `1604`, on a server
> whose default is `1700`. `ImageSave` serialises the **live** database, so without alignment the
> cache is a 1606 image in a 1604 project. The reopen then hits Desktop's own **"Tabular databases do
> not support CompatibilityLevel downgrade"**, which surfaces with **no visible message** — only the
> `Untitled` window and the bridge error.
>
> **`--save` therefore aligns, always, with no flag.** That is exactly what Desktop's own Save does: a
> UI Save was measured updating `database.tmdl` `1604 -> 1606` and writing `cache.abf` **at the same
> timestamp**. An earlier version put the alignment behind `--align-compat` and refused otherwise;
> that was removed as **ceremony rather than safety** — the only possible response to the refusal is
> to re-run with the flag, so an agent simply learns to always pass it. Verified end to end: after
> aligning, a cold reopen gives `PREFLIGHT: DATA_OK` with **no refresh**, and the write touches
> **2 files** where a UI Save touches **79**.
>
> ⚠️ **Saving EDITS THE DEPLOYABLE ARTIFACT**, which is what `--no-save` is for. `database.tmdl` ships
> with the model and its declared level goes up. A **read-only** consumer — auditing, validating,
> grading fidelity — must pass `--no-save`, or it mutates the very artifact it is judging.
>
> ⚠️ **A refusal must never fall through to the UI-save fallback.** Measured while fixing this: an
> early version *returned* a refusal, `main()` read it as "ImageSave unavailable" and drove Desktop's
> UI instead — which wrote `cache.abf` anyway and rewrote **74 of 89 files**, performing precisely the
> write that had just been refused. Anything that declines to write must stop the pipeline, not hand
> off to a fallback that writes.
>
> ⚠️ **`MainWindowTitle` is a LOADING STATE before it is a verdict — do not read it early.** Without a
> cache the title still reads `Untitled - Power BI Desktop` at t+25 s and only becomes the report name
> by ~t+55 s. Reading at 25 s produced a false "it is broken" on a bundle that was merely still
> opening. **Wait >= 90 s**, and prefer the bridge error over the title.
>
> ⚠️ **Never write TMDL with a BOM.** Desktop's project reader hard-rejects one —
> `UTF8EncodingThrowOnBOM.CheckBom` -> *"Only text with UTF8 encoding without BOM is supported"* — and
> the file does not open. This bit us during the investigation itself: a probe wrote `database.tmdl`
> with `[System.Text.UTF8Encoding]::new($true)` and contaminated a whole test arm. In Python use
> `encoding="utf-8"`; in PowerShell use `-Encoding utf8NoBOM` and never the `Out-File`/`Set-Content`
> defaults. **This is a separate failure from the compatibility mismatch** — same symptom, different
> cause, and only the Desktop crash report ("Frown") distinguishes them. Ask for one rather than
> inferring from the window title.

> ⚠️ **A refresh TIMEOUT is not evidence of a credential modal.** This script used to assert that,
> and it was wrong: measured 2026-08-04 it fired on 2 of 5 Desktop instances opened on the *same*
> bundle that refreshed cleanly on the other 3 (38.8 s on a good run, >87 s on a slow one, against a
> 90 s ceiling — a ~3 s margin). It now reports `REFRESH: TIMEOUT` with the cause **UNKNOWN** and
> names the arbiter (`scripts/probe_desktop_credential.ps1`, which **ships in this bundle** — the
> script prints its absolute path at runtime so the instruction always points at a file that is here).
>
> **The ceiling is 300 s and is deliberately NOT agent-tunable — there is no flag.** A knob here is
> an attractive nuisance: an agent that hits a timeout reaches for a bigger number, and the one case
> where waiting longer never helps is the credential wait, which this ceiling cannot interrupt
> anyway. If a model legitimately needs longer, refresh less with `--tables`. Never emit a
> "this needs a human" stop from an unverified timeout - that phrasing names the one blocker an
> agent must not retry, so a false positive turns a slow refresh into a permanent dead end.

Read-only preflight (proves credentials + source reachability without changing anything):

```
python scripts/probe_desktop_query.py [--pid <pbidesktop-pid>] [--tables "A" "B"]
```

**Name one canary table per distinct live source** with `--tables`. A model-level `DATA_OK` is only
emitted when *every* named canary returns rows; with **no** table named, only the first queryable
table is probed and the verdict is **downgraded** to `TABLE_OK '<table>'` — a static parameter/CSV
table can return rows while a live source never loaded, so one arbitrary table can never certify the
whole model. This mirrors the `powerbi-semantic-model-gotchas` rule: *prove a REAL read per live
source*. `refresh_pbip_model.py` applies the same rule to its own data check (its `--tables` is both
the refresh scope and the canary set).

Both print a machine-readable last line: `REFRESH: DATA_OK + PERSISTED` / `TABLE_OK '<table>'` /
`NO_DATA` / `NOT_PERSISTED` / `WRONG_MODEL` / `ERROR <msg>`, and `PREFLIGHT: DATA_OK` /
`TABLE_OK '<table>'` / `NO_DATA` / `ERROR`. Exit 0 is the good outcome — but it covers **both** a
model verdict (`DATA_OK`) and a single-table verdict (`TABLE_OK`), so a gate that needs model-level
certainty must require the literal `DATA_OK` (i.e. pass explicit canaries), not merely exit 0.

## Order matters, do not refresh before you finish editing

Desktop **discards `cache.abf` when the model definition is newer than the cache**. Verified: a model
whose `definition/*.tmdl` were touched after the cache was written opened `NO_DATA` despite a 113 KB
cache sitting right there. So:

> make **all** TMDL edits, then refresh, then save.

Anything that rewrites TMDL afterwards invalidates it, including the host repo's own sanitize step
(here, `set_data_folder.py --sanitize`, which must run before committing). If you sanitize last, you
have thrown the cache away; re-run this script after.

**It is the CONTENT that invalidates it, not the mtime — you cannot win this by ordering.** Measured
2026-08-01 on `logistics-live-dbx`: sanitize rewrote `expressions.tmdl` at 12:24:08, `ImageSave` wrote
`cache.abf` at 12:24:23 (15 s *newer*, 57.5 KB) — and a cold reopen still came back `NO_DATA`.
Re-localizing, reopening, refreshing and saving again produced a cache that **did** survive a
`Stop-Process -Force` + reopen (`PREFLIGHT: DATA_OK`, no refresh). So "refresh last, after sanitize"
does not rescue the cache: Desktop keys the cache to the definition it was built from.

> **Hand-off rule:** leave the model **localized + refreshed**. Persisting is the default and safe —
> the compatibility alignment above keeps the cache loadable — but a persisted cache only *survives*
> a hand-off when nothing rewrites the TMDL afterwards, and the committer still has to run the
> sanitize step, which invalidates it. So either persist and tell whoever commits to re-run this
> script after sanitizing, or hand off refreshed-but-not-persisted with `--no-save` and have them
> refresh once more post-sanitize. ⚠️ **Revised 2026-08-04, corrected 2026-08-07:** an earlier note
> here blamed *persisting* for making the PBIP unopenable and told you to persist only with a flag;
> root-caused, that was a compatibility-level mismatch `image_save` now aligns away, not persistence
> itself, so persisting is the safe default again.

**`powerbi-desktop reload` does NOT re-read edited TMDL.** Measured 2026-08-01: after editing two
measures on disk, `reload` returned `{"success": true}` and `INFO.MEASURES()` still showed the **old**
expressions — the reload refreshes the report, not the model definition. Only closing Desktop
(`Stop-Process -Id <literal pid> -Force`) and reopening the `.pbip` picks up a model change. This is
easy to miss precisely because the reload reports success, so **verify a model edit landed** with
`EVALUATE SELECTCOLUMNS(INFO.MEASURES(), "Name", [Name], "Expr", [Expression])` before trusting any
number you read back.

## How persistence actually works

| Path | What it is | Status |
|---|---|---|
| **AMO `Server.ImageSave(databaseId, Stream)`** | Writes `cache.abf` directly from the engine. No UI, works headless; staged to `cache.abf.tmp` and swapped in atomically, so a failed/partial write can't destroy an existing good cache. | **Default (opt out with `--no-save`)** |
| **UI Automation (`InvokePattern` on the "Save" element)** | Drives Desktop's own Save through the Windows *accessibility* tree. | Fallback / `--ui-save` |

⚠️ **Read the `--no-save` warning above before using either.** Both write the same `cache.abf`. A
present cache was once believed to make the PBIP unopenable; root-caused 2026-08-07, that was a
compatibility-level **mismatch**, which `image_save` now prevents by aligning `database.tmdl` (and
rolling that alignment back if the write fails). With the hazard gone, persisting is the DEFAULT and
matches this script's stated purpose; `--no-save` opts out for read-only or validate-then-deploy work.

**Why ImageSave exists at all**, because the obvious answer says it should not: the guidance is that
writing model state back to `.pbip`/`cache.abf` "is a separate Save action that only the Desktop UI
performs". That is true of the MCP/Bridge surface but **not of the engine**. A TMSL `backup` is
refused only because Desktop runs Analysis Services in **Diskless mode** ("Backup/Restore ... not
supported"), and that same configuration sets `EnableDisklessTMImageSave=1`. AMO exposes the matching
call, and it works. Proven end to end with the control that rules out a silent re-refresh: delete
`cache.abf`, refresh in memory, `ImageSave`, **kill Desktop with `-Force`** (no save prompt),
**rename the source data folder away**, reopen, `DATA_OK` with real rows. With the source absent, that
data can only have come from the cache.

Two traps:

- The AMO client raises **"The server sent an unrecognizable response"** *while writing the file
  correctly*. Judge success by the FILE (exists, non-empty, mtime advanced), never by the absence of
  an exception.
- `database_operations ExportToTmdlFolder` persists model *definition* changes but carries no rows.
  It cannot substitute for this.

UIA is a **workaround, not a contract**: an accessibility surface is not an automation API. It depends
on an element literally named "Save", so it breaks on ribbon changes and non-English installs, cannot
run headless, and a modal dialog swallows the invoke silently. It is not SendKeys, because
`SetForegroundWindow` is refused in this context, so a Ctrl+S keystroke lands on whatever window has
focus while the model stays dirty. Its result is confirmed against the bridge's own
`hasUnsavedChanges` flag. If a real `save` verb ever ships, delete it.

## Bind to the right instance, or you will corrupt a sibling

The destination is resolved from the **pid**; the data comes from whatever instance answers on the
**port**. If those disagree (one bad `--port`, or a port lookup that widens to "any `msmdsrv` on the
machine") this writes *another migration's model into your own correct-looking `cache.abf`*, and every
downstream signal still agrees: the file exists, is non-empty, is newly written, and the row count
queries that same wrong port.

So, before refreshing, querying or saving anything, two guards run. First, **if you pass `--port` it
must EQUAL the port derived from `--pid`** — a mismatch aborts, so `--port` can never bypass PID-based
discovery to point the read (and the `ImageSave` write) at another instance. Second, `same_model()`
compares the connected model's tables against the TMDL that owns the destination cache and requires an
**EXACT table-for-table match**; a mismatch — or an identity it cannot establish at all (no model
folder resolved, or no TMDL tables to fingerprint) — aborts with `WRONG_MODEL`, **failing closed**
rather than assuming it is fine. A superset schema (all your tables plus more) is a mismatch, not a
"confirmed". File metadata fundamentally cannot tell you whose rows are in a blob, only the model's
own contents can. When a project folder holds several `.SemanticModel` directories the destination is
resolved from the `.pbip` → report → `definition.pbir` `byPath` **binding**, never an arbitrary first
match. In a parallel batch **always pass `--pid`** (`powerbi-desktop status` maps pid to open file);
with several instances open the scripts refuse to guess.

**Sweep for orphans before you build, not just siblings while you build.** Measured 2026-08-01: a
Desktop instance was already running on the *exact `.pbip` path* about to be generated, left over from
an earlier, since-deleted attempt. `same_model()` would not have caught it — its TMDL tables matched —
but `INFO.MEASURES()` showed measures the new build never defines, i.e. a **different model held in
memory on your path**, one Save away from overwriting the files you are generating. So at the start of
a build, list `Get-Process PBIDesktop`, read each one's command line
(`Get-CimInstance Win32_Process -Filter "ProcessId=<pid>"`), and force-close any instance already bound
to your own `.pbip` before writing to it. Identify by `MainWindowTitle` + command line, never by "the
one instance that is running".

## Reusing this in another migration repo

Nothing here is Tableau-specific, the input is already a Power BI model. **Copy this folder.**
`SKILL.md`, the two Python scripts it runs, the `probe_desktop_credential.ps1` arbiter they name at
runtime, and the tests that gate them are one unit: the scripts import only each other, name the
arbiter by their own bundled path, and `tests/conftest.py` resolves them at `../scripts`, so the
suite — and every runtime instruction the scripts print — runs from wherever the folder lands. Drop
it in the target repo's skill location (`.github/skills/`) or promote it to a global one such as
`~/.copilot/skills/`, then prove the copy on the spot:

```
pytest tests
```

The one repo-bound fixture degrades honestly: the TMDL-fingerprint test that reads this repo's
`examples/*/fabric/*.SemanticModel` corpus **skips with a reason** when there is no such tree, rather
than failing or silently collecting nothing.

In the source repo that claim is a CI gate, not a sentence:
`tests/test_skills.py::test_a_bundled_skill_passes_its_own_tests_after_being_copied_out_of_this_repo`
copies this folder to a temp directory and runs `pytest tests` there with the repo root out of
`sys.path`. A new `import <something-repo-local>` fails there, before it can fail in your repo.

Two things do **not** travel, by design:

- `scripts/probe_desktop_query.py` and `scripts/refresh_pbip_model.py` at the *source repo's* root
  are forwarding shims for callers that predate the move. Do not copy them.
- `refresh_pbip_model.py` warns about this repo's `set_data_folder.py --sanitize`. Read that as
  "any post-processing that rewrites TMDL", and substitute your own.

No `allowed-tools` in the frontmatter, deliberately. Pre-approving `shell` would save one
confirmation per run, and GitHub's warning is the reason it is not worth it: pre-approving `shell` or
`bash` *"removes the confirmation step for running terminal commands and can allow attacker-controlled
skills or prompt injections to execute arbitrary commands in your environment"*
([docs](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/add-skills)).
These scripts spawn PowerShell, load .NET assemblies and write a binary cache file, so they are
exactly the shape that warning is about. The field is also marked experimental in the spec, with
support varying by runtime. Revisit only with a reason.
