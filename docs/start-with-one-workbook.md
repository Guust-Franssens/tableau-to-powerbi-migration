# Start with one workbook

You have **one** Tableau dashboard and its screenshots. No Tableau Server connection, no estate
sweep. The repository-manual workflow was exercised end to end on 2026-09-04 with no
`TABLEAU_SERVER_URL` and no PAT set. Choose the route before using its layout.

For a whole site instead, see [`AGENTS.md`](../AGENTS.md) → *Starting a migration*.

## Choose the route first

1. **Respect the requested write/output boundary.** If the request says local output and no toolkit
   repository changes, use an external working/output root and leave supplied inputs unchanged.
   The `migrations/` layout below is not permission to write into the repository.
2. **If the installed `tableau-migration` skill is the selected orchestrator, follow that route.**
   Use its `scripts\new_run.py` allocator for a fresh short external run and its optional D7
   `reference_images.py --mode export` acquisition. Do not silently switch to the repository layout
   or copy another engine.
3. **This page's commands are the explicitly selected repository-manual alternative**, for a
   repository-managed unit and later promotion. Use `scripts\work_dirs.py` to allocate the run, as
   shown below; its existing `--runs-parent <short-parent>` option provides a short external run
   root when needed. Never hand-invent or reuse a run number.
4. **The acquisition manifests are not interchangeable.** The installed helper's
   `out\reference_images\manifest.json` records coverage/confidence, not the repository reader's
   `dashboards[].states[]` capabilities contract. Its exit 0, filename or `named` confidence is
   not repository reference readiness. At a repository handoff, supply evidence the existing
   reader accepts or report the gap; there is no implied manifest adapter.

The authoritative [conversion, dispatch, and fidelity boundaries](reference-readiness.md#conversion-dispatch-and-fidelity-boundaries)
apply at the handoff. The following is the repository-manual sequence, not an override of the
selected installed-skill route.

---

## 0. Before anything — the two prerequisites that cost real time

> ⚠️ **The data source must ALREADY be connected in Power BI Desktop, signed in, under the account
> you will run as.** An agent cannot see or set Desktop's saved data-source connections, and it
> cannot fill a sign-in dialog. If the connection is not there, the agent burns an hour failing to
> authenticate and then has to stop and ask you anyway. Open Desktop, connect to the database or
> warehouse the dashboard reads, and confirm it refreshes — *before* you start.

Get the environment green with the direct preflight command, without a signing-policy pre-check:

```
git clone <this repo>
cd tableau-to-pbi-migration
uv sync --all-extras
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1 -Update
```

**Only after an actual unsigned/ExecutionPolicy startup refusal**, follow
[preflight cannot start](operator-runbook.md#preflight-cannot-start).
If recovery is allowed, retry the **exact originating command and arguments**; this setup call
retains `-Update`, not the runbook's session-start arguments.

Preflight is the contract — it prints an install hint beside anything missing and exits non-zero.
Beyond the Fabric skills, the Power BI Desktop bridge and the `powerbi-report-author` CLI, the one
extra dependency people miss is **.NET** plus the tabular client assemblies (ADOMD/AMO). Without them
a refresh still runs, but you lose live per-table row counts and fall back to the MCP path.

`uv sync --all-extras` matters more than it looks: without the extras, `parse_tableau.py` cannot
import `lxml` and several scripts fail on a fresh clone.

---

## 1. Drop the workbook in

```
migrations/workbooks/<your-slug>/
    source/<your-workbook>.twbx      <- .twb or .twbx, either is fine
    reference/                       <- screenshots go here (step 2)
```

`source/` and `reference/` are **git-ignored**, so a workbook holding customer data never lands in a
commit.

## 2. Drop the screenshots in

One PNG per dashboard page, in `migrations/workbooks/<your-slug>/reference/`:

```
reference/tableau-<exact dashboard name>.png
```

Three rules, all of them load-bearing:

| Rule | Why |
|---|---|
| The name must start with **`tableau-`** and end in **`.png`** | Anything else is not read. A `.jpg` or a plain `overview.png` is now reported by name with the reason, but it is still not used. |
| The rest of the name is the **exact** Tableau dashboard name | It is matched case-insensitively, spaces and all — **not** slugified. `tableau-Sales Overview.png` matches the dashboard `Sales Overview`; `tableau-sales-overview.png` does **not**. An unrepresentable, different or ambiguous name remains an identity gap; `--manual-object-type dashboard` declares kind only, not identity. |
| Full page, legible, one per page | It is candidate evidence, subject to the manifest's attribution, integrity and grade checks, not automatic fidelity ground truth. Anything below 64 px on either edge is rejected as illegible. |

Small files are fine — a simple dashboard PNG is often under 20 KB, and that is not a problem.

## 3. What you get, and what you do not

An adopted screenshot is recorded as **layout + text** evidence by default. Only an accepted,
current-source, per-object record can meet the default reference bar; adoption alone is not
readiness or fidelity sign-off. If you personally captured full-resolution evidence with filters
pinned, `--manual-validation-grade` records **your assertion**, not independent verification of its
depicted revision/state. Do not add it merely to make the gate green. Read the accepted capabilities
and ceiling in the manifest/readiness result, not the directory name.

## 4. The command sequence

Windows PowerShell, from the repo root, through the uv-managed environment. Replace the placeholders
and allocate once; reuse the returned paths for this run:

```powershell
$run = uv run --frozen python scripts\work_dirs.py "<slug>" --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Run allocation failed" }
```

For a short external run, add `--runs-parent <short-parent>` to that allocation command. Use the
printed paths; do not rename or move the allocated run.

Run these four commands separately and inspect each exit code. Acquisition may also happen earlier
while source access is available; it is not a dependency of step 2.

```powershell
uv run --frozen python scripts\parse_tableau.py "migrations\workbooks\<slug>\source\<workbook>.twbx" `
    -o "migrations\workbooks\<slug>\migration-spec.json"

uv run --frozen python scripts\run_estate.py --input "migrations\workbooks\<slug>\source" --output $run.bundle

uv run --frozen python scripts\capture_tableau_reference.py "migrations\workbooks\<slug>"

uv run --frozen python scripts\check_reference_readiness.py $run.bundle `
    --source "migrations\workbooks\<slug>\source\<workbook>.twbx" `
    --reference "migrations\workbooks\<slug>\reference"
```

All four can exit **0** with usable evidence and successful conversion. Judge each operation by its
own exit code, never by printed success text or another operation's result.

- **Step 1** writes the normalized `migration-spec.json`. Run it *before* step 3 — that is where the
  dashboard/worksheet names come from.
- **Step 2** runs the deterministic conversion engine over the single workbook and produces the
  PBIP bundle plus a per-workbook handover slice. Missing images do not prevent this step; unrelated
  engine failures still need attention.
- **Step 3** optionally captures/adopts available images and writes `reference/manifest.json`. Every file it did **not**
  adopt is named in the output with the reason. Exit 1 with `REJECTED, not missing` means your file is
  there and unusable, not absent. An existing manifest can short-circuit to exit 0: use `--force`
  after changing the source or recapturing, then rerun readiness. Neither that exit nor an empty
  `--structural-only` capture manifest clears readiness or authorizes model-only data access.
- **Step 4** checks the **reference prerequisite for agentic fidelity work**, using the exact
  source and reference paths above. Exit 1 or 3 is not a pass: resolve the evidence finding before
  that dispatch, not before the already-completed engine emission. Default reference `READY` is
  not final package `START_READY` or a fidelity verdict.

## 5. Prepare the agent handoff

After the requested reference bar and the applicable dispatch prerequisites are met, pick
**`tableau-migrator`** in Copilot CLI or VS Code, or name it in chat. Substitute the actual allocated
bundle path and the accepted evidence grade; package dispatch remains subject to the pending-consumer
boundary linked above.

```
@tableau-migrator migrate migrations/workbooks/<slug> — bundle is at <allocated bundle path>,
reference screenshots are in migrations/workbooks/<slug>/reference (accepted manifest grade: <grade>).
```

Write the four answers it needs into `migrations/workbooks/<slug>/migration-brief.md` first — scope,
autonomy, fidelity bar (faithful re-creation vs. modernise), and what to do at a wall. It is
stateless and cannot infer them, and the file survives a closed terminal.

---

## What to expect

- **~1.5 hours minimum** for a simple report, most of it not model time — Desktop opens, refreshes and
  validation passes dominate.
- **Roughly $50–100 of model spend** for a medium-complexity report; more for a complex one.
- This is an **accelerator, not a one-shot conversion.** It gets you a loading model and a bound
  report to review and correct; it does not hand you a finished dashboard.

## Two limits worth knowing up front

1. **A Power BI Desktop error dialog still needs you.** The agent cannot read it. When Desktop stops
   on an error, copy the text and paste it into the chat — that is the intended route, and UI
   automation is not a recommended substitute.
2. **Power Query M has no validation path equivalent to TMDL.** Generated M that is subtly wrong is
   not self-detectable yet, so a data-shaping step deserves a human read even when every gate is
   green.

Feedback: open an issue on this repo.
