---
name: pbip-model-refresh
description: Refresh a local PBIP/TMDL semantic model open in Power BI Desktop and PERSIST the result to .pbi/cache.abf, headlessly via AMO Server.ImageSave with a UI-Automation fallback. Use after finishing TMDL edits, before handing a model to report authoring, or whenever Desktop reopens a migrated model empty. Source-tool agnostic - the model is already Power BI, so this applies equally to Tableau, Qlik and Cognos migrations.
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

Read-only preflight (proves credentials + source reachability without changing anything):

```
python scripts/probe_desktop_query.py [--pid <pbidesktop-pid>] [--table "<table>"]
```

Both print a machine-readable last line: `REFRESH: DATA_OK + PERSISTED` / `NO_DATA` /
`NOT_PERSISTED` / `WRONG_MODEL` / `ERROR <msg>`, and `PREFLIGHT: DATA_OK` / `NO_DATA` / `ERROR`.
Exit 0 only on the good outcome, so these are usable as gates.

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

> **Hand-off rule:** leave the model **localized + refreshed + persisted** so the next agent gets data,
> and tell whoever commits to run the sanitize step (which knowingly discards the cache).

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
| **AMO `Server.ImageSave(databaseId, Stream)`** | Writes `cache.abf` directly from the engine. No UI, works headless. | **Default** |
| **UI Automation (`InvokePattern` on the "Save" element)** | Drives Desktop's own Save through the Windows *accessibility* tree. | Fallback / `--ui-save` |

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

So, before refreshing, querying or saving anything, `same_model()` compares the connected model's
tables against the TMDL that owns the destination cache; a mismatch aborts with `WRONG_MODEL`. File
metadata fundamentally cannot tell you whose rows are in a blob, only the model's own contents can. In
a parallel batch **always pass `--pid`** (`powerbi-desktop status` maps pid to open file); with
several instances open the scripts refuse to guess.

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
`SKILL.md`, the two scripts it runs and the tests that gate them are one unit: the scripts import
only each other, and `tests/conftest.py` resolves them at `../scripts`, so the suite runs from
wherever the folder lands. Drop it in the target repo's skill location (`.github/skills/`) or promote
it to a global one such as `~/.copilot/skills/`, then prove the copy on the spot:

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
