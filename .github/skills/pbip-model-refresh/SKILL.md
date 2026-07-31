---
name: pbip-model-refresh
description: Refresh a local PBIP/TMDL semantic model open in Power BI Desktop and PERSIST the result to .pbi/cache.abf, headlessly via AMO Server.ImageSave with a UI-Automation fallback. Use after finishing TMDL edits, before handing a model to report authoring, or whenever Desktop reopens a migrated model empty. Source-tool agnostic - the model is already Power BI, so this applies equally to Tableau, Qlik and Cognos migrations.
---

# Refresh a local PBIP model, and make the data survive a close

**Windows only.** Talks to the local Analysis Services instance that Power BI Desktop hosts, over
ADOMD.NET/AMO (pythonnet), plus the Desktop Bridge CLI for process to file mapping.

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

Anything that rewrites TMDL afterwards invalidates it, including this repo's own
`scripts/set_data_folder.py --sanitize`, which must run before committing. If you sanitize last, you
have thrown the cache away; re-run this script after.

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

## Reusing this in another migration repo

Nothing here is Tableau-specific, the input is already a Power BI model. The portable unit is **two
files with no other repo imports**:

- [`scripts/probe_desktop_query.py`](../../../scripts/probe_desktop_query.py) - port discovery, ADOMD
  loading, table listing, the read-only DAX probe.
- [`scripts/refresh_pbip_model.py`](../../../scripts/refresh_pbip_model.py) - refresh, identity gate,
  `ImageSave`/UIA persistence. Imports only from the file above.

Requirements: Windows + Power BI Desktop, Python >= 3.11 with `pythonnet`, the ADOMD.NET and AMO
client libraries under `~/.nuget/packages`, and `npx` for `@microsoft/powerbi-desktop-bridge-cli`.

Copy both files plus this skill folder into the target repo (or promote the folder to a global skill
location such as `~/.copilot/skills/`) and the procedure moves with it. If the scripts land somewhere
other than `scripts/`, update the two links above: `tests/test_skills.py` fails on a skill that points
at a path which does not exist.
