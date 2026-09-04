# Start with one workbook

You have **one** Tableau dashboard and its screenshots. No Tableau Server connection, no estate
sweep. This is the whole route, and every command below was run end to end on 2026-09-04 with no
`TABLEAU_SERVER_URL` and no PAT set.

For a whole site instead, see [`AGENTS.md`](../AGENTS.md) → *Starting a migration*.

---

## 0. Before anything — the two prerequisites that cost real time

> ⚠️ **The data source must ALREADY be connected in Power BI Desktop, signed in, under the account
> you will run as.** An agent cannot see or set Desktop's saved data-source connections, and it
> cannot fill a sign-in dialog. If the connection is not there, the agent burns an hour failing to
> authenticate and then has to stop and ask you anyway. Open Desktop, connect to the database or
> warehouse the dashboard reads, and confirm it refreshes — *before* you start.

Then get the environment green:

```
git clone <this repo>
cd tableau-to-pbi-migration
uv sync --all-extras
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1 -Update
```

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
| The rest of the name is the **exact** Tableau dashboard name | It is matched case-insensitively, spaces and all — **not** slugified. `tableau-Sales Overview.png` matches the dashboard `Sales Overview`; `tableau-sales-overview.png` does **not**. If the name cannot be spelled in a filename, pass `--manual-object-type dashboard` in step 4 instead. |
| Full page, legible, one per page | The screenshot is the fidelity ground truth the whole migration is graded against. Anything below 64 px on either edge is rejected as illegible. |

Small files are fine — a simple dashboard PNG is often under 20 KB, and that is not a problem.

## 3. What you get, and what you do not

A dropped screenshot is recorded as **layout + text** evidence. That is enough to *start*. It is not a
signed-off fidelity claim: nothing verified its resolution, its filter state, or that it came from
this build of the workbook. If you captured it yourself at full resolution with filters pinned, say
so with `--manual-validation-grade` — your assertion, logged as yours.

## 4. The command sequence

Windows PowerShell, from the repo root, through the uv-managed environment:

```
uv run --frozen python scripts/parse_tableau.py migrations/workbooks/<slug>/source/<workbook>.twbx `
    -o migrations/workbooks/<slug>/migration-spec.json

uv run --frozen python scripts/capture_tableau_reference.py migrations/workbooks/<slug>

uv run --frozen python scripts/run_estate.py --input migrations/workbooks/<slug>/source --output _runs/001-<slug>/bundle

uv run --frozen python scripts/check_reference_readiness.py _runs/001-<slug>/bundle `
    --source migrations/workbooks/<slug>/source/<workbook>.twbx `
    --reference migrations/workbooks/<slug>/reference
```

All four exit **0** on a healthy run. Judge by exit code, never by printed text.

- **Step 1** writes the normalized `migration-spec.json`. Run it *before* step 2 — that is where the
  dashboard/worksheet names come from.
- **Step 2** adopts your screenshots and writes `reference/manifest.json`. Every file it did **not**
  adopt is named in the output with the reason. Exit 1 with `REJECTED, not missing` means your file is
  there and unusable, not absent. Re-run with `--force` once a manifest exists.
- **Step 3** runs the deterministic conversion engine over the single workbook (~1 s for a small one)
  and produces the PBIP bundle plus a per-workbook handover slice.
- **Step 4** is the **entry gate**: it proves there is a legible, attributable picture behind every
  page the engine emitted. Exit 1 or 3 is *not* a pass — fix it before building, because a blind page
  makes an equivalent fidelity bug unfalsifiable rather than merely unverified.

## 5. Hand it to the agent

In Copilot CLI or VS Code, pick **`tableau-migrator`** from the agent picker, or name it in chat:

```
@tableau-migrator migrate migrations/workbooks/<slug> — bundle is at _runs/001-<slug>/bundle,
reference screenshots are in migrations/workbooks/<slug>/reference (manual capture, layout+text grade).
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
