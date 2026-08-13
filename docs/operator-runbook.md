# Operator runbook — migrating a Tableau estate into Fabric

**Audience:** the engineer sitting at the keyboard during a live migration, who may never have seen
this repo before. Read it top to bottom once the day before; use it as a lookup table on the day.

**Scope:** an *estate* migration — a whole Tableau site, or a folder of workbooks — from survey
through to items landing in a Fabric **landing-zone** workspace. A single-workbook migration is the
same pipeline with steps 1–4 skipped.

> **This repository is PUBLIC.** Nothing here names a customer, tenant, site, workspace, host or
> token. Every real value is a placeholder — `<site>`, `<workspace-id>`, `<tenant-id>`, `<bundle>` —
> and comes from either the git-ignored `.env` (Tableau credentials) or the migration brief
> (destination, scope, autonomy). If you paste a real value into a file that git can see, you have
> made a mistake; see [§7 Public-repo hygiene](#7-public-repo-hygiene).

**Evidence convention used throughout:**

| marker | means |
|---|---|
| ✅ **verified** | read out of the code, or reproduced against a local artifact, while writing this |
| ⚠️ **reported** | measured in a previous run and recorded elsewhere; not re-checked here |
| ❌ **not proven** | we do *not* have evidence for this — do not claim it to a customer |

Measurements below come from a real 38-workbook estate run on 2026-08-12 with engine **2.126.0**.

---

## 0. The 60-second mental model

```
Tableau site ──1─► estate_survey.py ──► REST dependency ground truth (JSON)
                     │
             ──2─► assess_estate.py ──► WHAT to migrate + IAM decisions + estate.db
                     │
             ──3─► tableau_lineage.py --plan ──► model-first ORDER
                     │
             ──4─► harvest_estate_assets.py ──► <out>/assets/*.twbx|*.tdsx  (+ parse sweep)
                     │
             ──5─► run_estate.py ──► <bundle>/   (engine converts; OUR wrapper adjudicates)
                     │
             ──6─► deploy_estate.py ──► Fabric landing-zone workspace
```

Two tiers, and knowing which one you are talking to decides who owns a bug:

- **the engine** — `tableau-fabric-skills`, a separate upstream project this repo does **not** pin.
  It does the conversion. It is deliberately failure-isolated: one bad workbook never fails a batch,
  so **it exits 0 even when its own definition of done says `failed`.**
- **our tier** — everything in `scripts/`. It surveys, assesses, harvests, *adjudicates* the engine's
  output into a real exit code, and deploys. ✅ verified: `run_estate.py:check_definition_of_done`.

Three locations, one direction (from `AGENTS.md`, and it is enforced):

| stage | path | rule |
|---|---|---|
| engine truth | `<bundle>/reports/`, `<bundle>/semantic_models/` | **never edited, by anyone** |
| working copy | `<bundle>/pbip/` | agents edit here; this is what `deploy_estate.py` reads |
| deliverable | `migrations/{workbooks,datasources}/<slug>/fabric/` | copied at sign-off |

---

## 1. Pre-flight — the day before, not on the day

Everything in this section fails silently or slowly if you leave it to the morning.

### 1.1 Tooling

```powershell
powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1 -Update -CheckUpstream
```

✅ verified — the timing rule is real and it matters:

| when | run | why |
|---|---|---|
| **session start, nothing in flight** | `preflight.ps1 -Update -CheckUpstream` | the only safe moment to change tooling |
| **migration start** | `preflight.ps1` (plain) | confirm READY without swapping tools mid-flow |
| **mid-migration** | **never** | swapping the validator under a half-built report is worse than a slightly old one |

Exit codes ✅ verified (`preflight.ps1:388-395`):

| exit | meaning |
|---|---|
| `0` | all **critical** dependencies present — "Ready to migrate" (recommended warnings may still print) |
| `1` | one or more **critical** items missing — resolve before migrating |

A `[WARN]` in the RECOMMENDED tier **never** changes the exit code. Being *above* the known-good
version matrix is a WARN, not an error — it means the version-specific prose in `.github/agents/`
was written against an older build. Re-verify the prose; never "fix" it by downgrading.

`-CheckUpstream` costs ~3s of network and is **advisory only**. It is the only check that asks *"has
the world moved"* rather than *"is what I have good enough"* — every other check compares against a
hard-coded number.

### 1.2 Know which engine you are running — and write it down

**This has caused three retracted defect reports.** The engine resolves at runtime and is not pinned
by this repo, and it has been installed **twice at different versions on one machine**, with
different pipeline steps resolving different trees. The two are not equivalent: 2.113.0 emitted
deprecated Bing `shapeMap`/`filledMap` visuals and dropped a density-map worksheet entirely where
2.126.0 emits `azureMap` with a heat layer. Nothing in the run output said which one ran.
⚠️ reported (issue #107).

Check both known locations before you start:

```powershell
$roots = @(
  "$env:USERPROFILE\.copilot\installed-plugins\tableau-collection\tableau-fabric-skills",
  "$env:USERPROFILE\vscode-projects\tableau-fabric-skills"
)
foreach ($e in $roots) {
  $v = Join-Path $e "skills\tableau-migration\VERSION"
  if (Test-Path $v) { "$e => $((Get-Content $v -Raw).Trim())" } else { "$e => ABSENT" }
}
```

**Today (master), on this machine, both trees exist and both report `2.126.0`** ✅ verified. Equal
versions are luck, not a control.

**Where `--engine` resolves today** ✅ verified against master:

| caller | resolves to |
|---|---|
| `run_estate.py` | argparse default `~/vscode-projects/tableau-fabric-skills` (**the sibling clone**) |
| `harvest_estate_assets.py`, `dax_oracle_server.py` | installed plugin first, then the sibling clone — first hit wins, silently |
| `transpile_tableau_calc.py` | installed plugin only |

So a single pipeline can legitimately survey with one tree and convert with another.

**Landing in parallel (branch `feat/single-engine-source`, issue #107 → **PR #109**, open, not merged
at the time of writing):** `scripts/engine_source.py` makes the **installed plugin the single
canonical engine** and *raises* rather than falling back; `run_estate.py --engine` becomes a
deliberate override that requires `--allow-noncanonical-engine` and adds `EXIT_ENGINE_SOURCE = 5`;
`preflight.ps1` gains two **critical** checks (`engine: plugin installed`, `engine: single source`)
that block on a second tree; and the resolved path + `VERSION` + a canonical/override flag land in
`engine-output-receipt.json` under a new `engine` key (`migration_bundle.write_engine_receipt(bundle,
engine)` → `engine_source.engine_provenance`). ✅ verified against `origin/feat/single-engine-source`.

**When #109 merges, delete the manual step below and read the receipt instead** — and re-point
step 5's `--engine` guidance, because the default flips from the sibling clone to the plugin.

**Until it merges, the receipt does NOT record the engine** ✅ verified on `master` —
`migration_bundle.write_engine_receipt` writes only `version`, `created_at`, `report_sha256`,
`input_manifest_sha256`, `artifacts`. **So record it by hand:**

```powershell
# run this INTO the bundle, immediately after step 5, before anything else touches it
"engine=$((Get-Content "$env:USERPROFILE\vscode-projects\tableau-fabric-skills\skills\tableau-migration\VERSION" -Raw).Trim())" |
  Out-File <bundle>\ENGINE-VERSION.txt
```

A bundle that cannot answer *"what built me?"* cannot support a defect report. Do not skip this.

### 1.3 Credentials — test them the day before

Two independent credentials, and both have a failure mode that looks like a hang.

**a. Tableau PAT.** Copy `.env.example` to `.env` (git-ignored ✅ verified) and fill in:

```
TABLEAU_SERVER_URL=https://<pod>.online.tableau.com
TABLEAU_SITE=<site-content-url>          # empty string for a Tableau Server Default site
TABLEAU_PAT_NAME=<pat-name>
TABLEAU_PAT_SECRET=<pat-secret>
TABLEAU_PAT_VALUE=<pat-secret>           # SAME VALUE, second name — see §4.1. Not optional.
```

We cannot mint a PAT for the customer: Tableau's API answers **HTTP 405** to create-PAT, so a
Tableau user with access must issue it, and it inherits that user's permissions (a restricted
account simply sees less of the estate). ✅ verified in `.env.example`.

Smoke-test it **the day before**, in this order — each is cheap and each proves a different layer:

```powershell
# 1. our tier reads .env and reaches the Metadata API (read-only, downloads nothing)
python scripts\tableau_lineage.py --plan --env .env --save-json _assessment\lineage.json

# 2. the ENGINE's own auth path, which is a different code path — see §4.1
python <engine>\skills\tableau-migration\scripts\estate_survey.py `
    --server <host> --site <site> --pat-name <pat-name> --env-file .env --no-prompt `
    --json _assessment\estate_survey.json
```

**b. Fabric.** `deploy_estate.py` mints its token via the Azure CLI ✅ verified
(`az account get-access-token --resource https://api.fabric.microsoft.com`), so:

```powershell
az login --tenant <tenant-id>
az account get-access-token --resource https://api.fabric.microsoft.com | Out-Null   # must succeed
```

The identity needs **Contributor** on the landing-zone workspace. `deploy_estate.py:preflight`
distinguishes `404 does not exist (or this identity cannot see it)` from `403 no access … needs the
Contributor role` on purpose — conflating them costs an afternoon. ✅ verified.

The landing zone must **already exist**: `--workspace` is documented as *"EXISTING landing-zone
workspace id (never created here)"* ✅ verified. Creating one means choosing a capacity, which is not
our decision. It does **not** need an F capacity — both item types we create (`SemanticModel`,
`Report`) are Power BI items, so an appropriately licensed identity suffices ✅ verified from the
module's own comment.

### 1.4 Power BI Desktop

Concurrent Desktop instances are **fine** — the Bridge addresses one by `--pid` natively. What is
*not* fine is an unnamed lookup with several running: that is a coin flip, and it is a deliberate
error, not bad luck. Note your PID when you open one, and close only what you opened
(`Stop-Process -Id <literal-pid> -Force`; map instance → migration by `MainWindowTitle`).

One-time Desktop setting, and preflight **cannot** check it (Desktop is MSIX, the setting is not in
the registry): **Options → Global → Privacy → "Always ignore Privacy Level settings"**. Without it,
any multi-source model raises a modal *before the model loads*, which no automation can dismiss and
which looks exactly like a hang.

### 1.5 Write the brief

Per `AGENTS.md`, the brief is a **file**, not a conversation: a closed terminal takes the session's
entire working memory with it. Put it at `migrations/workbooks/<slug>/migration-brief.md` and record
the four answers that cannot be inferred:

1. the plan from §2 — this ordering, these workbooks, this destination workspace;
2. **autonomy** — `guided` / `standard` (default) / `autopilot`;
3. **fidelity bar** — faithful re-creation, or modernise where Power BI is better;
4. **if we hit a wall — stop, or degrade?** (pre-authorising the fallback is what lets an unattended
   run survive one).

No autonomy level clears a credential wall — that is a modal sign-in dialog no automation can fill.

> ⚠️ **Check the brief is actually ignored before you write anything into it:**
> `git check-ignore -v migrations/workbooks/<slug>/migration-brief.md`. See §7.

---

## 2. The happy path

Run every step from the repo root with the venv active. Output directories are git-ignored by
convention — see §7 for which ones actually are.

| # | command | ~time (38 wb / 55 assets) | produces |
|---|---|---|---|
| 1 | `python <engine>/…/estate_survey.py --server <host> --site <site> --pat-name <name> --env-file .env --no-prompt --json _assessment/estate_survey.json` | ⚠️ ~32 s | REST dependency ground truth |
| 2 | `python scripts/assess_estate.py --out _assessment --survey _assessment/estate_survey.json` | ⚠️ ~34 s | `report.md`, `assessment.json`, `estate.db` |
| 3 | `python scripts/tableau_lineage.py --plan` | seconds | model-first order |
| 4 | `python scripts/harvest_estate_assets.py --out _harvest` | ✅ **120 s / 55 assets** | `_harvest/assets/*`, `parse-sweep.md` |
| 5 | `python scripts/run_estate.py --input _harvest/assets --output _bundle` | ✅ **81.7 s total** (engine 41.3 s, provenance 38.7 s) | `_bundle/` |
| 6 | `python scripts/deploy_estate.py --bundle _bundle --workspace <workspace-id> --tenant <tenant-id> --estate-db _assessment/estate.db --journal _bundle/deploy-journal.jsonl` | ⚠️ ~13 min / 64 items | items in the landing zone |

> **Timing discrepancy, stated plainly.** Issue #106 records step 5 as *"137 s for 38 workbooks"*,
> but the bundle's own `phase-timings.json` records `total_elapsed_sec: 81.7` ✅ verified by reading
> the artifact. 137 s is plausible as wall-clock including interpreter start and shell overhead;
> 81.7 s is what the script measured. **Trust `phase-timings.json`** — it is written by the run
> itself. If a step feels slow, read that file rather than guessing.

### Step 1 — survey the site

⚠️ **Three flag traps, all ✅ verified against `estate_survey.py --help`:**

- `--server` is **required** and has no default. It takes a host *or* a URL.
- `--json` takes a **PATH**, not a bare flag.
- `--no-prompt` is not in the brief's original command and **you should always pass it** — see §4.1.
  It converts the worst failure in this pipeline from a silent block into an instant, explanatory
  error.

`--pat-name` on the command line is the safe default. It *can* come from the process environment
variable `TABLEAU_PAT_NAME` ✅ verified (`fetch_tds._resolve_auth`:
`args.pat_name or os.environ.get("TABLEAU_PAT_NAME")`) — but **not** from the `.env` file, because
the `--env-file` layer is only consulted for *secrets*. Omitting the name is harmless: it raises an
immediate `SystemExit` naming both options ✅ verified by direct call.

Expect on success: `[SURVEY] N workbook(s); M depend on a published datasource; K datasource(s) must
be fetched first.` then one `[DEPENDS]` line per dependent workbook, then `[OK] survey written to …`.

### Step 2 — assess

Emits a **decision, not an inventory**: a coverage curve, a per-workbook complexity score, a
migrate/consolidate/archive/retire tier, and the IAM hard cases.

Read `_assessment/report.md` and check three things before moving on:

- **Is usage data present?** If the site is new or usage stats are unavailable, the report says so:
  *"usage data is too sparse to tier on … Every tier below is therefore **unproven** — scope by hand,
  and do not present this curve to a customer as evidence."* ✅ verified — that exact warning fired on
  the reference run (0 lifetime view events across 38 workbooks). **Do not put an unproven curve on a
  slide.**
- **Understated complexity.** Workbooks backed by a published datasource have their calculated
  fields counted on the *server*, not in the workbook, so their score is low and wrong. The report
  names the count.
- **Without `--survey`, ordering is reported as `unknown`, never as "none"** ✅ verified from the
  module docstring. The Metadata API reported `upstreamDatasources` for **0 of 13** workbooks where
  REST `connections` showed `sqlproxy` on **9**. A plan built on that concludes "migrate in any
  order" and produces **empty reports**. Always pass `--survey`.

IAM is **exported, not mapped**. Mapping needs the Power BI workspace topology, and that is a human
decision. Two hard cases show up on nearly every estate: Power BI's Build permission is
all-or-nothing (*"see the chart, not the numbers"* is not expressible), and local Tableau groups have
no Entra counterpart — that one needs an identity owner and is usually the long pole.

### Step 3 — lineage plan

`--plan` prints the migration order by leverage: most-consumed published datasource first. The dedup
key it prints is the same key `parse_tableau.py` stamps on a parsed workbook, so server-side lineage
and locally parsed workbooks line up ✅ verified.

Migrating workbook-by-workbook rebuilds a near-identical semantic model every time, and those copies
then drift. That is the whole reason this step exists.

### Step 4 — harvest

Downloads every workbook and published datasource to `<out>/assets/`, then runs **both** parsers over
all of them and writes `parse-sweep.md` / `parse-sweep.json`.

The sweep is worth more than the download: a workbook that one parser reads and the other refuses is
a finding *by construction*, and which way round it fails says which tier owns it. It also turns an
upstream feature request from an anecdote into an estate-wide failure distribution.

⚠️ **Downloads are the session-fragile part.** Tableau Cloud drops sessions intermittently and the
failure is a `401002` mid-loop, so each asset is fetched with its **own** sign-in. Measured: a shared
token truncated a 58-asset run repeatedly; fresh-per-asset completed. Slower, and the only thing that
finishes. Assets are fetched **by LUID, never by name** — Tableau permits duplicate names across
projects, and name-keyed identity has already produced four separate defects here.

Reference run ✅ verified from `_harvest.log`: `55 asset(s) — ours failed 0, his failed 0, both
parsed 55` in 120 s.

Useful flags: `--limit N` (quick pass), `--skip-download` (reuse `<out>/assets`), `--workbooks-only`,
`--db _assessment/estate.db` (take LUIDs from the assessment).

### Step 5 — convert

**This is the gate.** The engine exits 0 regardless; our wrapper is what turns its report into an
answer.

`run_estate.py` exit codes ✅ verified (`run_estate.py:75-79`, and the `main()` returns):

| exit | constant | meaning | what to do |
|---|---|---|---|
| `0` | `EXIT_OK` | `ESTATE: READY` — DoD not failed, no approval collisions | proceed |
| `1` | `EXIT_ENGINE_FAILED` | the engine itself exited non-zero | read the last 2000 chars it printed (the wrapper prints them to stderr) |
| `2` | *(argparse/usage)* | `--input` missing and `--slice-only` not given | fix the command |
| `3` | `EXIT_DOD_FAILED` | engine's definition of done is `failed` | **§3 decision point** — do not deploy |
| `4` | `EXIT_COLLISION` | two models claim the same calc name with different formulas | resolve before approving DAX |
| `5` | `EXIT_ENGINE_SOURCE` | *(pending branch only)* non-canonical engine | see §1.2 |

Note `warn` is **deliberately allowed through** — it is the normal state of a real migration
(deferred visuals, stubbed calcs), and blocking on it would make the wrapper useless ✅ verified
(`DOD_BLOCKING = {"failed"}`).

Reference run ✅ verified from `_convert.log`: `bound=23/38 failed=15 warned=20`, the wrapper returned
**3** and refused to hand the bundle on. **That is the wrapper working, not the wrapper breaking.**

> ⚠️ **`--approved-dax` re-runs are DELETE-AND-RECREATE, not merge.** The engine `rmtree`s the
> `.SemanticModel` folder, the whole `.pbip` project dir and `<name>.Report` before rewriting them,
> and the stale-output guard *exempts* that path — so the most destructive re-run is the one that
> needs no `--force`. **All DAX approvals land in ONE run; per-workbook agent work starts only
> afterwards.** ✅ verified from `run_estate.py`'s own docstring.

What a bundle contains ✅ verified against the reference bundle:

| path | what | may I edit it? |
|---|---|---|
| `report.json` | the engine's full estate report (~1.8 MB for 38 workbooks) | no |
| `handover/` | one slice per workbook, so a per-workbook agent never loads the whole report | no |
| `pbip/` | **the working copy** — one folder per deployable unit; what `deploy_estate.py` reads | **yes** |
| `reports/`, `semantic_models/` | the engine's pristine output — the free baseline for `diff` | **never** |
| `phase-timings.json` | per-phase elapsed seconds | — |
| `engine-output-receipt.json` | hashes of the engine's output (see §1.2 for what it does *not* record) | — |
| `input_manifest.json`, `source-provenance.json` | what went in, and where upstream it came from | — |
| `deploy-estate-id.txt` | **minted at first deploy — keep it with the bundle** (§3.3) | — |

**`pbip/` is not one folder per workbook.** On the reference bundle it held **39** folders = **23**
converted workbooks + **16** datasource-only semantic models ✅ verified by reconciling `pbip/`
against `report.json`. That is model-first migration working as intended: a shared published
datasource becomes one model, not one per consumer. **The item count is not the workbook count** —
get the real number from `deploy_estate.py --dry-run`.

### Step 6 — deploy

```powershell
python scripts\deploy_estate.py --bundle _bundle --workspace <workspace-id> --tenant <tenant-id> `
    --estate-db _assessment\estate.db --journal _bundle\deploy-journal.jsonl --dry-run
```

**Always `--dry-run` first.** It prints, per workbook, where it would land and whether it is
`model + report` or `model only`, how many folders it would create, how many workbooks have no known
Tableau project (they land at the root), and finally the **item count** — *"the number to agree
BEFORE deploying, since each item carries a cost in the customer's capacity and licensing terms"* ✅
verified. On the reference bundle a dry run plans **76 items** (39 pairs, 2 reports skipped as empty)
✅ verified by calling `discover()` + `report_is_empty()` offline against the bundle.

Then drop `--dry-run`.

`deploy_estate.py` exit codes ✅ verified (`deploy_estate.py:129-131`):

| exit | constant | meaning |
|---|---|---|
| `0` | `EXIT_OK` | everything planned was deployed (skipped-as-empty reports are reported honestly, not counted as deployed) |
| `1` | `EXIT_FAILED` | at least one item failed, **or one or more workbooks were refused** — the run names them |
| `2` | `EXIT_PREFLIGHT` | never started: workspace missing, no access, or the item budget does not fit |

What it does, in order, and why the order is not negotiable:

1. **preflight** — workspace readable, item budget fits (`1000` per workspace minus what is already
   there, minus 10 % headroom ✅ verified);
2. **folders** — mirror the Tableau project tree (`--estate-db`), or `--no-folders` for a flat root;
3. **models first** — a report cannot be rebound to a model that does not exist yet;
4. **rebind** — `definition.pbir` arrives as `byPath` (a Git-integration mechanism the service cannot
   resolve) and is rewritten to `byConnection` with the model guid *inside* the connection string as
   `semanticModelId=<guid>`;
5. **reports**.

Three service behaviours encoded here, each of which silently produces a broken deployment if you
hand-roll it ✅ verified from the module docstring:

- the five-field `byConnection` form everyone quotes is PBIR schema **1.0.0**; schema **2.0.0**
  declares `additionalProperties: false` and allows exactly `connectionString`. Sending the old form
  gets `Workload_FailedToParseFile`; omitting the guid gets `InvalidConnectionInformation`.
- **`202 Accepted` tells you nothing.** Create returns an empty body; a FAILED operation is
  indistinguishable from success until `/operations/{id}` is polled.
- **Fabric does not reject duplicate item names** for `Report`/`SemanticModel`. Two identical pairs
  sat side by side in a real workspace. Nothing downstream catches this — which is exactly why the
  ownership stamp, the journal and the run lock exist.

---

## 3. Decision points — stop and ask

`AGENTS.md` calls this **Gate B**. Present these as **one block**, not four serial stops: serial
stops are the same questions with strictly more waiting, and each is another chance to catch the
customer out of the room. Where the brief (§1.5) already answered one, **apply it and say so** — do
not re-ask.

### 3.1 After assess/lineage — published datasources

A workbook pointing at a published datasource has its calcs on the *server*. Migrating the workbook
first rebuilds an incomplete model.

**Ask:** fetch the `.tds`/`.tdsx` and migrate the datasource first, or proceed knowingly incomplete?

### 3.2 After convert — the DoD gate (`exit 3`)

`bound=<X>/<N> failed=<F> warned=<W>` is the whole decision. Read the *per-workbook* detail, never the
summary line: `report.json`, and the `handover/` slices.

**Ask:** resolve the failing workbooks, explicitly accept them as out of scope, or narrow the estate?

**Do not deploy an `exit 3` bundle** because "most of it is fine". If the customer accepts the gap,
record that acceptance in the brief — an accepted gap is a decision, an ignored one is a defect.

### 3.3 Before deploy — the ownership decision

`--adopt-existing` is **not a default**. It means *"take ownership of same-named items already in the
workspace"*, and it **overwrites them in place** ✅ verified.

| situation | do |
|---|---|
| empty or first-time landing zone | nothing — the normal path |
| the landing zone **is ours**, but the journal was lost | `--adopt-existing`, having confirmed the workspace really is ours |
| an item of that name exists and is **not** ours | **do not adopt** — rename the workbook, or use a separate workspace |

Why the guard exists: Fabric item identity is only `(displayName, type)`. An unrelated customer
report called e.g. `Sales` was indistinguishable from ours and **was overwritten in place**, reported
as *"already existed — definition updated"*. And a second estate deployed into the same landing zone
silently overwrote a same-named item from the first — one item where there should have been two, the
first project's folder left empty, and **both runs exited 0** ✅ verified from the module's comments.

**`deploy-estate-id.txt` — keep this file with the bundle.** It is minted into the bundle on first
deploy (`<bundle-name>-<12 hex>`) and is what lets a re-run recognise its own work ✅ verified
(`estate_identity()`). Naming the estate after the bundle *directory* was measured wrong in both
directions: too strict (a copy, a rename, an unzip as `bundle (1)`, or merely tab-completing a
different **case** on Windows made a legitimate re-run refuse the entire estate) and too loose (two
customers whose bundle folders were both called `bundle` silently overwrote each other, exit 0 both
times). Copy it, rename it, re-zip it — the identity travels. Lose it and a re-run will refuse.

A refused workbook is **skipped, not fatal**: the rest of the estate deploys and the run exits 1
naming the refusals ✅ verified (`_refusals` / `_announce_refusals` / `_run_all`). The refusal check
is **per workbook** on purpose — per item created a model and then discovered the report was foreign,
leaving an orphan; per estate let one colliding name block six already-deployed workbooks.

### 3.4 Any time — the credential wall

A refusal naming authentication, permissions or a sign-in prompt is a **final answer**. Try once.
Only a plainly transient timeout (a serverless warehouse cold-starting) earns a retry.

**Cap any unresponsive external system at ~2 minutes or 3 attempts, whichever comes first** — unless
the tool tells you it *is* the timer (some of our scripts self-bound and announce their own
deadline; killing one of those at 120 s records *no verdict at all*, which is strictly worse than
waiting). Then **stop and ask a specific question**: name the system, what you tried, the concrete
options. Never re-run the same call hoping for a different result.

**Autopilot does not override this.** It governs *choices*; a credential is a physical dependency on
a human.

---

## 4. Failure playbook

Ordered by what actually cost time.

### 4.1 `estate_survey.py` sits there doing nothing

| | |
|---|---|
| **symptom** | the command produces no further output and never returns. ⚠️ reported: 13 minutes lost; the tell was **0.11 CPU-seconds and zero network connections** |
| **cause** | it is **blocked on a hidden `getpass` prompt** for the PAT secret |
| **check** | `Get-Process -Id <pid> \| Select-Object CPU` — near-zero CPU with no sockets is a prompt, not work. Then: does `.env` contain **`TABLEAU_PAT_VALUE`**? |
| **fix** | add `TABLEAU_PAT_VALUE=<secret>` to `.env` (alongside `TABLEAU_PAT_SECRET`), **and always pass `--no-prompt`** |

**Why our `.env` is not enough on its own** ✅ verified: our scripts document the secret as
`TABLEAU_PAT_SECRET`; the engine reads it as `TABLEAU_PAT_VALUE`. `scripts/tableau_env.py` bridges
the two — **but only for an engine script that OUR Python spawns**. Running one yourself from a
shell crosses a process boundary no bridge reaches. The engine's `--env-file` layer looks the secret
up under its *own* key (`TABLEAU_PAT_VALUE`), finds nothing, and falls through to the prompt.

**Correction to the folklore — the current engine is NOT silent.** ✅ verified by calling
`fetch_tds._resolve_auth` directly with injected seams: before prompting, 2.126.0 writes a loud
banner to **stderr**:

> `[auth] Tableau PAT secret: type it into THIS terminal now (input is hidden). Do NOT paste secrets
> into chat. For a non-interactive / agent-driven run, provide it instead via --pat-secret …`

The "no output at all" experience is consistent with the **older 2.113.0** tree, or with stderr being
redirected/swallowed. Either way the *hang* is real; the *silence* is version- and
plumbing-dependent. Do not rely on seeing the banner.

**`--no-prompt` turns it into an instant, explanatory failure** ✅ verified — it raises
`SystemExit: No Tableau PAT secret was provided. Supply --pat-secret …, set TABLEAU_PAT_VALUE, add it
to a git-ignored --env-file entry, store it in an OS keyring …`. **Always pass it.** The only reason
not to is if you genuinely intend to type the secret at a console.

Related, and *not* a hang ✅ verified: a missing PAT **name** raises immediately —
`Tableau PAT sign-in needs a token NAME (it is not a secret): pass --pat-name or set
TABLEAU_PAT_NAME.`

### 4.2 `run_estate.py` exits 3 with `bound=<X>/<N>`

Not a crash. Our wrapper refusing to hand a failed bundle downstream. See §3.2, and §4.3 for the
single most common cause.

### 4.3 A large slice of the estate is "skipped" — the union trap

| | |
|---|---|
| **symptom** | `pbip_status: "skipped"` on many workbooks, `bound` far below `workbooks_total`, but **`viz_status: "built"`** and a `.Report` folder exists |
| **cause** | ⚠️ upstream `Yarbrdab000/tableau-fabric-skills#124`: a **Tableau UNION** — a datasource with several relations — reports *every* member as having "no resolvable columns", and one unresolvable relation discards the **whole workbook** |
| **impact** | **14 of 38** on the reference estate (a further 1 failed for an unrelated, honest reason) |

**How to spot it, precisely** ✅ verified by querying the reference `report.json`:

```powershell
python -c "import json;r=json.load(open(r'<bundle>/report.json',encoding='utf-8'));[print(w['name'],'|',(w.get('pbip_warnings') or [''])[0][:120]) for w in r['workbooks'] if w.get('pbip_status')=='skipped']"
```

The warning reads:

> `manual attention required: embedded datasource '<name>' needs a storage decision (Direct-upstream
> rebuild not safe (relation '<a>.csv' has no resolvable columns; relation '<b>.csv' has no
> resolvable columns); the column schema IS readable -- what is missing is a storage-mode choice …)
> -- workbook .pbip skipped (model lands separately)`

⚠️ **The word "union" never appears** — ✅ verified: zero occurrences in the whole 1.8 MB
`report.json`. Search for `needs a storage decision` + `no resolvable columns`, not for `union`.

⚠️ **The warning names the WRONG datasource.** Upstream #124 established that the failing relations
belong to a *different* embedded datasource than the one named (11 of the 14 blamed a datasource that
was entirely healthy). **Do not spend time investigating the datasource in the message.**

**What it is not:** not a parse failure. Both parsers read 55/55 assets on the same run. The extracts
are present and typed — in the documented case, 11,807 real rows sat in the packaged `.hyper` that
was reported as unresolvable.

**What to tell the customer:** these workbooks were converted (the visuals built) but no semantic
model was produced, so there is nothing to deploy for them yet. It is a known upstream defect with a
filed issue — not a property of their data, and not a failed migration.

### 4.4 `deploy_estate.py` refuses a workbook

Read the refusal text — it is written to be actionable. Three distinct shapes ✅ verified:

| message contains | means | do |
|---|---|---|
| *"carries no marker from a previous deploy, and this run's journal has no record"* | an item of that name exists and is **not** ours | rename the workbook, use an empty workspace, or `--adopt-existing` **only if the zone is genuinely ours** |
| *"N items share this name, so which one to update is ambiguous"* | duplicates already in the workspace | remove the extras in the workspace, re-run |
| *"came from '<other-estate>', not '<this-estate>'"* | a **second estate** in the same landing zone | separate landing zone per estate — or `--adopt-existing` if you truly mean to overwrite |

### 4.5 Deploy stops partway naming connectivity

✅ verified: after **3 consecutive** `HTTP 0` failures (our client failing to resolve/reach the host,
not a service verdict) the run stops rather than marking the rest of the estate failed. A laptop
moving between networks mid-deploy previously burned through an entire estate emitting
`getaddrinfo failed`.

**Fix connectivity, then re-run the identical command.** Everything already deployed is skipped by
**content hash** — see §6.

### 4.6 `401 TokenExpired` mid-deploy

Handled ✅ verified: the token re-mints itself once on `401 TokenExpired` and continues. A 66-item
deploy previously outran its token and failed every remaining call. If you still see it, `az login`
has expired entirely — re-authenticate and re-run.

### 4.7 A report fails with `invalid package content stream`

The report has **no pages** (`pageOrder: []`), because the source workbook had no convertible
worksheets. ✅ verified: `report_is_empty()` detects this *before* the call and skips it with
`report has NO PAGES - skipping (the model is still deployed)`, so it should not reach you as a
service error. If it does, you are deploying by hand rather than through this script.

### 4.8 The engine's pristine output fails `powerbi-report-author validate`

⚠️ reported (issue #108), engine 2.126.0. Straight out of `run_estate.py`, no agent edits:
`PBIR_FORMATTING_PROP_UNKNOWN` on `azureMap` `dataPoint.defaultColor`, and `PBIR_ROLE_MAX_EXCEEDED`
on a `scatterChart` with two `Category` projections. Exit 1.

**We do not yet know whether the gate or the engine is wrong.** Do not present a red `validate` from
the engine's own output as a migration defect, and do not "fix" it by editing `reports/` (that is the
pristine baseline — §0). Also stale in the same area: `viz_fidelity` still labels these visuals
`shape_map`/`filled_map` and advises enabling a **preview feature that is no longer needed**. Do not
send a customer to that toggle.

### 4.9 Something feels stuck

**Report elapsed time whenever an operation exceeds ~60 s.** An anomaly in elapsed time or tool-call
count is a signal, not noise. Ground truth is readable *mid-run* — `phase-timings.json`, the deploy
journal, the artifact folder — and reading it early is what has caught real failures before a run
self-reported success.

---

## 5. Verification checklist

Run in order. Each line says what it proves — and §5.2 says what none of them prove.

### 5.1 What we can check

| # | check | how | pass |
|---|---|---|---|
| 1 | conversion was adjudicated | `echo $LASTEXITCODE` after step 5 | `0` |
| 2 | DoD not failed | `phase-timings.json` + the `definition_of_done` line in the run summary | `status` ≠ `failed` |
| 3 | inputs are accounted for | `_harvest/parse-sweep.md` | `ours failed 0, his failed 0` |
| 4 | the item count was agreed | `deploy_estate.py --dry-run` | number matches what the customer signed off |
| 5 | deploy completed | `echo $LASTEXITCODE` after step 6 | `0` (`1` = at least one failure or refusal — read the named list) |
| 6 | nothing was silently skipped | the final line: `all N item(s) deployed` **or** `N deployed; M skipped as empty` | M is a number you can explain |
| 7 | the journal has no unfinished intent | `Select-String -Path <bundle>\deploy-journal.jsonl -Pattern '"status":"failed"'` | no hits, or hits you have triaged |
| 8 | every report resolves its model | the portal: open each report; it loads its model rather than erroring | ⚠️ reported 31/31 on a previous estate; **not re-verified here** |
| 9 | the estate identity survived | `Get-Content <bundle>\deploy-estate-id.txt` | non-empty, and stored with the bundle |
| 10 | the engine version is recorded | `<bundle>\ENGINE-VERSION.txt` (§1.2) | present |
| 11 | connections the customer must make | `python scripts\connections_manifest.py --bundle <bundle> --out <dir>` | `connections.md` delivered |

**Check 11 is a deliverable, not a diagnostic.** Credentials do not travel with a migrated item.
Without this list the customer discovers which sources need connecting **one failed refresh at a
time**. It never emits a secret (host/database/account are configuration; passwords and keys are
not — a test proves no credential-shaped value reaches it), and it never calls an extract
"connected": a model built from a materialised `.hyper` is a **snapshot frozen at extract time that
will never refresh**, listed separately and labelled. It is ordered by **blast radius** — a published
datasource feeding twelve workbooks is a different task from one feeding a single archived report.
✅ verified from the module docstring.

### 5.2 What this does NOT prove — say this out loud

❌ **We have never verified that a migrated report RENDERS.** The strongest claim we can make is
⚠️ reported: on a previous estate, **31/31 reports bound `byConnection` to a real semantic model in
the service**. **Binding resolves ≠ the report works.** We have not confirmed the visuals draw, with
data, matching the Tableau original. Treat the deployed estate as *ready for review*, never as
*validated*.

❌ **A green `validate` is necessary, not sufficient.** TMDL deserialization and
`powerbi-report-author validate` both pass defects that only surface in Desktop **with data**. And
`validate` reports 0 errors even when it could not fetch the visual schema at all
(`PBIR_SCHEMA_UNREACHABLE`) — `preflight.ps1` checks mechanically whether a green result this session
means "schema-checked" or only "structure-checked".

❌ **The engine's own output currently fails `validate`** on this estate (§4.8, issue #108), so a red
result there is not evidence about *your* migration.

❌ **No numbers have been reconciled against Tableau.** "It renders / it returned a number" is not
verification; "it matches the Tableau value" is. That comparison is the `pbi-migration-validator`
agent's job and is a separate phase.

❌ **Refresh has not been proven.** Live sources need a connection + credential established in the
target workspace (check 11) before any refresh can succeed.

---

## 6. Rollback, re-runs and cleanup

### 6.1 Re-running the deploy is safe — and here is why to believe that

Re-run the **identical command**. ✅ verified from the code:

- A **run journal** records **intent before the mutation and outcome after**, keyed to a **hash of
  the exact definition deployed**. Intent-first closes the window between "we called create" and "we
  recorded that we called create" — an outcome-only log cannot tell a crash there from never having
  called.
- The hash closes the more dangerous case: an item that **exists but uploaded partially**. A
  name-only *"does it exist?"* check answers *present*, and a resume would silently ship it broken.
  A resume skips an item **only when the journal says done AND the hash matches** what is about to be
  deployed.
- Rebinding happens **before** hashing, so the digest describes the bytes actually sent.
- A **run lock** prevents overlapping deploys. Not hypothetical: a truncated shell pipeline let a
  first run keep going invisibly, a second was started, and the workspace ended with **duplicate
  models and reports** — which also disproved the assumption that Fabric rejects duplicate names.

There is **no idempotency key** on the Fabric item APIs, so all of this is the client's job. That is
why the journal and the lock are load-bearing rather than conveniences.

`--force-unlock` takes the lock even if another deploy appears to hold it. **Only use it when you
have positively confirmed no other deploy is running** — that is the exact scenario that produced the
duplicates above.

### 6.2 Purging a landing zone

There is **no automated teardown** in this repo ❌ not proven — do not promise one. To reset a
landing zone, delete the items in the portal (or via the Fabric REST API), and then **also delete the
journal** — a journal that claims items exist which no longer do will cause a resume to skip work it
should do. Deleting the journal alone is safe: the next run re-derives ownership from the **service**
(the provenance stamp on each item description), which is stronger evidence than a local file.

Keep `deploy-estate-id.txt` unless you intend the next run to be a *different* estate.

### 6.3 Closing Power BI Desktop

Concurrent instances are fine; **orphans are not** — each holds an `msmdsrv` with the model in RAM,
so leaked instances exhaust the machine.

```powershell
Get-Process PBIDesktop | Select-Object Id, MainWindowTitle    # map instance -> migration by title
Stop-Process -Id <your-literal-pid> -Force
```

**Never** close a sibling's instance, and never close one mid-handoff that a peer still needs.

### 6.4 Scratch

Remove one-off probe scripts, `%TEMP%` harnesses, and `.pbip` cache/backups. Keep the committed
deliverables and the re-runnable `_build/` scripts. Confirm nothing scratch leaked into git before
reporting done.

---

## 7. Public-repo hygiene

This repo is public, and customer-identifying files **have been committed here before** — a workshop
plan and a prerequisites email were pushed before anyone noticed, and required a history rewrite.

**Ignored ✅ verified with `git check-ignore -v`:**

| path | rule |
|---|---|
| `.env`, `.env.local` | `.gitignore:87-88` |
| `_assessment*/` | `/_assessment*/` — real estate: workbook/project names, owner LUIDs, group membership, permissions |
| `_harvest/`, `_sweep/` | downloaded `.twbx`/`.tdsx` from a real site |
| `migrations/workshop-*/`, `engagement-*/`, `customer-*/` | engagement notes |
| `**/data/`, `**/source/*.twb` | extracted customer data and source workbooks |

**Before this runbook landed, two holes were open and are now closed** (same PR):

| path | why it was a hole |
|---|---|
| `migrations/workbooks/<slug>/migration-brief.md` | the rule was `/migrations/*/migration-brief.md` — **one level deep** — but `AGENTS.md` instructs writing the brief **two** levels deep, at `migrations/workbooks/<slug>/`. `git check-ignore` returned **NOT IGNORED** for the exact documented path. The brief holds the customer's name, scope and destination workspace. |
| `_bundle/` (and `_bundle*/`) | the conventional convert output. It contains `report.json` with every workbook name, every calculated-field formula, and the generated TMDL/PBIR — i.e. the customer's content — while its siblings `_assessment*/` and `_harvest/` were already ignored. |

**Habit, regardless:** before staging anything during an engagement,

```powershell
git status --short          # anything unexpected?
git check-ignore -v <path>  # prove a customer file is ignored, do not assume
```

Placeholders used in this document — and where the real value lives:

| placeholder | source |
|---|---|
| `<host>`, `<site>`, `<pat-name>`, `<pat-secret>` | git-ignored `.env` (see `.env.example`) |
| `<workspace-id>`, `<tenant-id>` | the migration brief (git-ignored) |
| `<bundle>` | your local output dir, e.g. `_bundle` |
| `<engine>` | the resolved `tableau-fabric-skills` tree — §1.2 |

---

## 8. Quick reference

**Exit codes**

| script | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|
| `preflight.ps1` | ready | critical missing | — | — | — | — |
| `run_estate.py` | READY | engine failed | usage | **DoD failed** | approval collision | *(pending)* engine source |
| `deploy_estate.py` | all deployed | item failed / refused | preflight | — | — | — |

**Where the truth lives**

| question | file |
|---|---|
| how long did each phase take? | `<bundle>/phase-timings.json` |
| what did the engine actually produce? | `<bundle>/report.json`, `<bundle>/handover/<workbook>.json` |
| which engine built this bundle? | `<bundle>/ENGINE-VERSION.txt` (manual today — §1.2) |
| what did the deploy do, and when? | `<bundle>/deploy-journal.jsonl` |
| which estate do these items belong to? | `<bundle>/deploy-estate-id.txt`, and each item's description in the service |
| what must the customer connect? | `connections.md` from `scripts/connections_manifest.py` |
| what is in scope, and who decided? | the migration brief (git-ignored) |

**Related reading:** [`/AGENTS.md`](../AGENTS.md) (dispatcher flow, Gate B, shared conventions) ·
[`docs/capabilities-and-limitations.md`](capabilities-and-limitations.md) ·
[`docs/data-source-credentials.md`](data-source-credentials.md) ·
[`docs/credential-gate.md`](credential-gate.md) ·
[`docs/migration-programme.md`](migration-programme.md)
