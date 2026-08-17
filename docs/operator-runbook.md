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

A ✅ is a promise that costs more than it looks: a *wrong* ✅ is worse than no marker, because it
stops the reader checking. One was wrong in the first edition (§1.3a claimed a key was in
`.env.example` that has never been there) and it broke the very first step for a cold operator. If
you edit this file, re-read the code — do not carry a marker forward.

**Where the numbers come from — read this before quoting one to a customer.** Every reference
number below comes from **one** 38-workbook Tableau site:

| what | when | engine | note |
|---|---|---|---|
| survey/assess/harvest/convert timings, `bound=23/38`, the union trap, `pbip/`=39 | 2026-08-12 | 2.126.0 | the *reference bundle* referred to throughout |
| deploy rate, 76 planned / 74 created, 36/36 report bindings, `PBI_DESKTOP_PATH`, the wrong-tenant 404 | 2026-08-13 | 2.126.0 | a cold-start operator run against the **same** site |
| re-checks marked ✅ in this edition | 2026-08-13 | 2.126.0 | code at `master` @ `181324a` |

One site is one shape. It is also a **structural blind spot**: this document's numbers were measured
on the site it was written against, so re-running that site can never catch a number that has gone
stale — which is exactly how a 2.4×-optimistic deploy estimate survived until an operator with a
stopwatch ran it. **The next cold run should use a different estate.** Treat every count and duration
here as an order of magnitude, and re-measure on the customer's estate (`--dry-run` for counts, the
deploy journal for rate) before quoting one.

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

Exit codes ✅ verified (the render block at the end of `preflight.ps1` — `$criticalMissing` decides,
nothing else does; cited by symbol because the line numbers drift on every edit):

| exit | meaning |
|---|---|
| `0` | all **critical** dependencies present — "Ready to migrate" (recommended warnings may still print) |
| `1` | one or more **critical** items missing — resolve before migrating |

A `[WARN]` in the RECOMMENDED tier **never** changes the exit code. Being *above* the known-good
version matrix is a WARN, not an error — it means the version-specific prose in `.github/agents/`
was written against an older build. Re-verify the prose; never "fix" it by downgrading.

**"Critical" does not mean "blocks the estate pipeline."** Preflight is tiered for the *whole*
toolkit, agents included, and steps 1–6 of §2 never open Power BI Desktop, never call an MCP server
and never invoke a skill (`run_estate.py`'s own docstring: *"never opens Power BI Desktop"* ✅
verified). So a `[MISS]` can be genuinely fatal or entirely irrelevant to the run in front of you.
Which is which ✅ verified by reading every `Add-Check`/`Add-Cli` tier in `preflight.ps1`:

| critical check | blocks §2 steps 1–6? | blocks the agent / Desktop phase? |
|---|---|---|
| `Python >= 3.11`, `python: lxml`, `python: jsonschema` | **yes** | yes |
| `engine: plugin installed`, `engine: single source` | **yes** — step 5 resolves the plugin (§1.2) | yes |
| `cli: powerbi-report-author` + its version floor | no | yes — it is the PBIR validator |
| `plugin: powerbi-authoring@…`, `skill bundles installed`, `skill bundles match published plugin` | no | yes |
| `cli: npx`, `cli: powerbi-desktop`, `Power BI Desktop`, **`PBI_DESKTOP_PATH`** | no | yes |
| `cli: dotnet` | no | yes — offline TMDL validator |

And the reverse trap, which is the more dangerous direction: **`az` is tiered `optional`, but step 6
cannot run without it** ✅ verified — `deploy_estate.py` mints its token by shelling out to
`az account get-access-token`. A green preflight is not a green light for the deploy; §1.3b is.

⚠️ **`PBI_DESKTOP_PATH` is the one that stops a cold start** (⚠️ reported: ~5 minutes lost,
2026-08-13). A machine with everything else in place reports NOT READY with:

```
[MISS] PBI_DESKTOP_PATH (bridge exe pin) - not set - the bridge is using its own version-pinned discovery
```

It is critical for a real reason — it removes a mismatch rather than detecting one, so a Desktop
auto-update stops silently breaking the Bridge — but it gates **nothing in steps 1–6**. See §1.4 for
how to set it, including why the hint preflight prints does not fix your current shell.

`-CheckUpstream` costs ~3s of network and is **advisory only**. It is the only check that asks *"has
the world moved"* rather than *"is what I have good enough"* — every other check compares against a
hard-coded number.

### 1.2 Know which engine you are running — the bundle records it for you

**This has caused three retracted defect reports.** The engine resolves at runtime and is not pinned
by this repo, and it has been installed **twice at different versions on one machine**, with
different pipeline steps resolving different trees. The two are not equivalent: 2.113.0 emitted
deprecated Bing `shapeMap`/`filledMap` visuals and dropped a density-map worksheet entirely where
2.126.0 emits `azureMap` with a heat layer. Nothing in the run output said which one ran.
⚠️ reported (issue #107).

**That is fixed, and the manual workaround this section used to mandate is gone.** PR #109 merged
2026-08-13; ✅ verified against `master` @ `181324a`:

- **The installed plugin is the single canonical engine.** `run_estate.py --help` now reads
  *"DELIBERATE OVERRIDE ONLY. Defaults to the installed tableau-fabric-skills plugin"* — the default
  no longer points at a sibling clone.
- **A non-canonical `--engine` fails closed.** ✅ verified by running it: pointing `--engine` at
  another directory exits **5** with `ESTATE: ENGINE_SOURCE - … is not the canonical engine plugin
  … Pass --allow-noncanonical-engine to override deliberately; the bundle receipt will record the
  run as non-canonical.`
- **`preflight.ps1` blocks on a second tree** — `engine: plugin installed` and `engine: single
  source`, both **critical** (§1.1).
- **The bundle answers *"what built me?"* by itself.** `migration_bundle.write_engine_receipt` writes
  an `engine` key into `engine-output-receipt.json` from `engine_source.engine_provenance` — the
  resolved root, its `VERSION`, whether it was canonical, and where it came from:

```powershell
python -c "import json;print(json.load(open(r'<bundle>\engine-output-receipt.json',encoding='utf-8')).get('engine'))"
# {'root': '...\tableau-collection\tableau-fabric-skills', 'version': '2.126.0', 'canonical': True, 'source': 'plugin'}
```

The run also prints it before it starts — `ENGINE SOURCE: <path> VERSION=2.126.0 (canonical plugin)`
✅ verified from a `--dry-run`.

⚠️ **An OLD bundle has no `engine` key, and that is not a defect — it is an age check.** ✅ measured
on the 2026-08-12 reference bundle: its receipt carries only `version`, `created_at`,
`report_sha256`, `input_manifest_sha256`, `artifacts`. Any bundle produced before #109 (merged
2026-08-13 11:32 local) predates the field. If `.engine` is absent, the bundle cannot tell you what
built it — do not guess, and do not file a defect against a version you inferred.

> **On "delete this when X merges" notes.** The previous edition of this section carried exactly such
> a note, and rotted **within six hours** of the merge it predicted — the manual step it mandated
> then made §5.1 check 10 *fail on a correct bundle*. A promise to a future reader is not a
> mechanism. If you must write one, make it **checkable by the reader in one command, at the moment
> they read it**, and phrase the surrounding prose so both states are safe:
> *"run `<command>`; if it prints X the section below is live, if it prints Y it is stale — and here
> is what to do in each case."* The `.engine` check above is written that way on purpose: absent key
> → old bundle, present key → read it. Neither answer needs this paragraph to be up to date.

### 1.3 Credentials — test them the day before

Two independent credentials, and both have a failure mode that looks like a hang.

**a. Tableau PAT.** Copy `.env.example` to `.env` (git-ignored ✅ verified) and fill in the four keys
it ships ✅ verified by reading the file:

```
TABLEAU_SERVER_URL=https://<pod>.online.tableau.com
TABLEAU_SITE=<site-content-url>          # empty string for a Tableau Server Default site
TABLEAU_PAT_NAME=<pat-name>
TABLEAU_PAT_SECRET=<pat-secret>
```

`TABLEAU_PAT_SECRET` is the one documented secret name. The engine's legacy
`TABLEAU_PAT_VALUE` spelling remains accepted for existing `.env` files, but
`scripts/tableau_env.py` mirrors both names in process and child environments. **Always use
`scripts/run_engine_survey.py` for the estate survey**: it extends that bridge across the only former
process boundary and always passes `--no-prompt`, so a missing credential fails clearly instead of
opening a hidden-input prompt.

We cannot mint a PAT for the customer: Tableau's API answers **HTTP 405** to create-PAT, so a
Tableau user with access must issue it, and it inherits that user's permissions (a restricted
account simply sees less of the estate). ✅ verified in `.env.example`.

Smoke-test it **the day before**, in this order — each is cheap and each proves a different layer:

```powershell
# 1. our tier reads .env and reaches the Metadata API (read-only, downloads nothing)
python scripts\tableau_lineage.py --plan --env .env --survey _assessment\estate_survey.json --save-json _assessment\lineage.json

# 2. the ENGINE's own auth path via the credential wrapper — see §4.1
python scripts\run_engine_survey.py `
    --server <host> --site <site> --pat-name <pat-name> --env-file .env `
    --json _assessment\estate_survey.json
```

**b. Fabric.** `deploy_estate.py` mints its token via the Azure CLI ✅ verified
(`az account get-access-token --resource https://api.fabric.microsoft.com`), so:

```powershell
az login --tenant <tenant-id>
az account get-access-token --resource https://api.fabric.microsoft.com | Out-Null   # must succeed
```

⚠️ **A token that mints successfully is not necessarily for the right tenant.** This is the single
most expensive stumble recorded against this document (⚠️ reported: ~15 minutes, and it hit **four
times across two independent operators on 2026-08-13**). On a multi-account machine the token
minted fine, for the *corp* tenant, while the landing zone lived in another — and `GET /workspaces/
{id}` then answered **`WorkspaceNotFound`** for a workspace that had just been filled with 74 items.
It reads like *"your deploy went somewhere else"*, and it lands in the phase where you are
reassuring the customer.

**Decode the token before you trust it.** ✅ verified end to end on 2026-08-13 — this prints the
claims and never the token:

```powershell
$tok = az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv
$p = $tok.Split('.')[1].Replace('-','+').Replace('_','/'); $p += '=' * ((4 - $p.Length % 4) % 4)
$claims = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p)) | ConvertFrom-Json
"tid=$($claims.tid)  aud=$($claims.aud)  upn=$($claims.upn)"   # tid must be the LANDING ZONE's tenant
```

`tid` matched `az account show --query tenantId` exactly ✅ verified, and `aud` proves the token was
scoped to `https://api.fabric.microsoft.com` rather than ARM.

**If `tid` is wrong**, in order of preference:

| do | why |
|---|---|
| `az account get-access-token --resource … --subscription <sub-id-in-that-tenant>` | ✅ verified as a real `az` flag (*"if the subscription argument isn't specified, the current account is used"*). Selects the account without mutating anything |
| `az login --tenant <id>` for the account that actually exists there | ⚠️ adding `--tenant` alone made it **worse** on the cold run: az picked the corp account and failed `AADSTS90072 … does not exist in tenant` |
| `az account set --subscription <id>` | works, but it changes the Azure CLI's **default** — not scoped to your shell, so it affects other work on the machine. ⚠️ reported; deliberately not re-tested here, because running it would mutate a shared machine |

⚠️ **`deploy_estate.py --tenant` inherits the same ambiguity** ✅ verified: it is appended verbatim to
the `az account get-access-token` command line, and there is **no `--subscription` passthrough**. So
fix the identity *before* the deploy — either by proving `tid` with the snippet above, or by taking
the `--subscription` route in your own `az` call and leaving `--tenant` off.

The identity needs **Contributor** on the landing-zone workspace. `deploy_estate.py:preflight`
distinguishes `404 does not exist (or this identity cannot see it)` from `403 no access … needs the
Contributor role` on purpose — conflating them costs an afternoon. ✅ verified. Note that a
wrong-tenant token lands in the **404** bucket, not the 403 one: see §4.4.

The landing zone must **already exist**: set its GUID once as `FABRIC_WORKSPACE_ID` in the
git-ignored `.env`, or pass `--workspace` to override that value for one deploy. ✅ verified.
Creating one means choosing a capacity, which is not our decision. It does **not** need an F capacity
— both item types we create (`SemanticModel`, `Report`) are Power BI items, so an appropriately
licensed identity suffices ✅ verified from the module's own comment.

### 1.4 Power BI Desktop

**Steps 1–6 never open Desktop** ✅ verified (`run_estate.py`: *"never opens Power BI Desktop"*).
Everything in this section is for the per-workbook agent phase and for visual verification — but two
of its prerequisites are checked *early*, so do them the day before.

**Two prerequisites, and preflight can only see one of them.**

**a. `PBI_DESKTOP_PATH` — critical in preflight, and the hint it prints will not fix your shell.**
✅ verified from `preflight.ps1`: it is tiered `critical`, and unset it reports
`not set - the bridge is using its own version-pinned discovery`. It exists to *remove* a mismatch
rather than detect one — the Bridge honours it and it wins over the Bridge's built-in, version-pinned
exe discovery, so a Desktop auto-update stops silently breaking every downstream call.

⚠️ The printed hint is `setx PBI_DESKTOP_PATH "…" (then reopen the shell)`. **`setx` does not affect
an already-open shell**, and an agent's tool shells inherit the parent environment — so following it
verbatim leaves preflight failing in the session you are actually working in. Set both:

```powershell
$exe = (Join-Path (Get-AppxPackage Microsoft.MicrosoftPowerBIDesktop).InstallLocation 'bin\PBIDesktop.exe')
$env:PBI_DESKTOP_PATH = $exe     # this shell, right now
setx PBI_DESKTOP_PATH $exe       # every future shell
```

Re-set it after a Desktop auto-update — preflight's `version: Power BI Desktop` WARN is the reminder.

**b. Privacy Levels — preflight cannot check this at all**, and states so honestly rather than
asserting a check it cannot perform (Desktop is MSIX; the setting lives in the package's private
`settings.dat`, which needs `SeRestorePrivilege` and is locked while Desktop runs) ✅ verified from
`preflight.ps1`'s own comment. Set it by hand, once: **Options → Global → Privacy → "Always ignore
Privacy Level settings"**. Without it, any multi-source model raises a modal *before the model
loads*, which no automation can dismiss and which looks exactly like a hang.

Concurrent Desktop instances are **fine** — the Bridge addresses one by `--pid` natively. What is
*not* fine is an unnamed lookup with several running: that is a coin flip, and it is a deliberate
error, not bad luck. Note your PID when you open one, and close only what you opened
(`Stop-Process -Id <literal-pid> -Force`; map instance → migration by `MainWindowTitle`).

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
| 1 | `python scripts/run_engine_survey.py --server <host> --site <site> --pat-name <name> --env-file .env --json _assessment/estate_survey.json` | ⚠️ ~32 s | REST dependency ground truth |
| 2 | `python scripts/assess_estate.py --out _assessment --survey _assessment/estate_survey.json` | ⚠️ ~34 s | `report.md`, `assessment.json`, `estate.db` |
| 3 | `python scripts/tableau_lineage.py --plan --survey _assessment/estate_survey.json` | seconds | model-first order — **survey edges override the Metadata API** |
| 4 | `python scripts/harvest_estate_assets.py --out _sweep` | ✅ **120 s / 55 assets** | `_sweep/assets/*`, `parse-sweep.md` |
| 5 | `python scripts/run_estate.py --input _sweep/assets --output _bundle` | ✅ **81.7 s of recorded phases** (engine 41.3 s, provenance 38.7 s) | `_bundle/` |
| 6 | `python scripts/deploy_estate.py --bundle _bundle --workspace <workspace-id> --tenant <tenant-id> --estate-db _assessment/estate.db --journal _bundle/deploy-journal.jsonl` | ⚠️ **~25 s per item** — budget 30 min for 75 items | items in the landing zone |

> ⚠️ **`--out _sweep`, not `_harvest`** — the previous edition said `_harvest`, which `.gitignore`
> reserves for a **different** tool. See §7: choosing an unignored name here stages real customer
> `.twbx` files in a public repo. Prove your exact path with `git check-ignore -v` before you
> download anything.

> **What `phase-timings.json` does and does not measure.** `total_elapsed_sec` is the plain **sum of
> the recorded phases** ✅ verified (`write_phase_record`: `sum(p["elapsed_sec"] for p in phases)`),
> which is *"where did the time go inside the run"* — **not** how long your command took. Work that
> sits outside a phase timer is invisible to it: reading the ~1.8 MB `report.json`, and the
> generated-artifact hash manifest that runs just before the `engine_receipt` timer starts.
> Interpreter start + engine resolution is ✅ measured at **0.3 s**, so the gap is normally small —
> but it is a *floor*, not a total. **If you need wall clock, measure wall clock**
> (`Measure-Command { … }`); if you want to know which phase was slow, read the file.
>
> ✅ **Re-measured 2026-08-13, correcting a report that said otherwise:** the empty-model scan **is**
> inside a recorded phase — `check_empty_models()` runs within the `adjudicate` timer in `main()`.
> Scanning the reference bundle's **55 models took 8.7 s** on this machine (the reference bundle
> shows `adjudicate: 0.0` only because it was built on 2026-08-12, before that check existed). So
> expect `adjudicate` to be seconds, not zero, on any bundle built today — and no, it is not the
> unrecorded phase it was once thought to be. Issue #106's *"137 s"* against the file's `81.7` is the
> same wall-clock-vs-phases distinction.

### Step 1 — survey the site

⚠️ **Three flag traps, all ✅ verified against `estate_survey.py --help`:**

- `--server` is **required** and has no default. It takes a host *or* a URL.
- `--json` takes a **PATH**, not a bare flag.
- `scripts/run_engine_survey.py` supplies `--no-prompt` itself, converting a missing secret from a
  hidden-input block into an instant, explanatory error.

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

**Check a fourth thing: is the run DEGRADED?** Since #193 a listing that cannot be read no longer
kills the run — it is retried within a bound, then recorded. The contract:

| where | what to look for |
|---|---|
| exit code | `0` clean or secondary-degraded · **`3` a PRIMARY listing is incomplete** · `1` nothing assessed |
| `report.md` | a `> ⚠️ **DEGRADED**` blockquote (secondary) or a `# ⚠️ DEGRADED` **first heading** (primary) |
| `assessment.json` | `degraded`, `degraded_primary`, and `listing_errors[]` naming each endpoint, page, attempts and elapsed time |
| `estate.db` | the `assessment_run` row carries `degraded` / `degraded_primary` and the counts; `listing_error` rows name each failed listing (error text scrubbed). This is the ONLY degradation signal a programmatic consumer (`harvest_estate_assets.py --db`, `deploy_estate.py --estate-db`) sees — neither opens `assessment.json` |
| the log | one `[WARN]` per failed listing, then one `[ACTION]` line |

A **secondary** failure (subscriptions, alerts, custom views, group membership, flows) only ever
*under*-reports deliberate use, so the retire-candidate tier is the one to distrust — the rest of the
assessment stands. A **primary** failure (workbooks, views, datasources, projects, structure) means
the coverage curve is computed from data known to be partial: **do not scope from it, re-run.**

Four flags tune the network behaviour; the defaults are the old hard-coded ones:
`--rest-timeout 180` · `--graphql-timeout 300` · `--max-attempts 3` · `--retry-budget 300`
(seconds, except attempts). Raise the timeout for a slow connection, raise attempts for a flaky one.
Neither retries an auth or permission refusal — that is a final answer, and a human has to fix it.

### Step 3 — lineage plan

`--plan` prints the migration order by leverage: most-consumed published datasource first. The dedup
key it prints is the same key `parse_tableau.py` stamps on a parsed workbook, so server-side lineage
and locally parsed workbooks line up ✅ verified.

Migrating workbook-by-workbook rebuilds a near-identical semantic model every time, and those copies
then drift. That is the whole reason this step exists.

> ⛔ **Where step 3 and step 1 disagree, STEP 1 WINS. Read them side by side before you act.**
>
> This step reads the **Metadata API** (GraphQL), and the Metadata-API blindness that step 2 already
> warns about applies here identically — the warning was simply never attached to step 3. ⚠️ reported
> from the cold run, same estate, minutes apart:
>
> | | says |
> |---|---|
> | step 1 (`estate_survey.py`, REST) | *38 workbooks; **10** depend on a published datasource; **11** datasources must be fetched first* — with explicit edges |
> | step 3 without `--survey` (`tableau_lineage.py --plan`, GraphQL only) | *17 published data sources feed **1** workbook* and warns that sources with no consumers have **no downstream usage VISIBLE TO THE METADATA API** — **not** evidence they are unused |
>
> Every certified source the survey proves is a **hard dependency** is unknown on the no-survey path.
> Treating that as abandoned at §3.1 means telling the customer their live sources are dead and
> migrating the consumers first — which is exactly the ordering that produces **empty reports**.
>
> **Always pass the survey.** ✅ verified 2026-08-13 against `tableau_lineage.py --help`:
> `--survey SURVEY` is `estate_survey.py --json output. Its dependency edges OVERRIDE the Metadata
> API's, which is blind to 'sqlproxy' connections. Without this the plan is known-incomplete.`
> Issue #126 is closed by #138. Without `--survey`, read every "no downstream usage VISIBLE TO THE
> METADATA API" line as unknown, never as unused; with `--survey`, the REST-derived dependency edges
> from step 1 are fed into the plan directly.

### Step 4 — harvest

Downloads every workbook and published datasource to `<out>/assets/`, then runs **both** parsers over
all of them and writes `parse-sweep.md` / `parse-sweep.json`.

⛔ **Choose `--out` before you download, and prove it is ignored.** `--out` is required and has no
default ✅ verified (`--help`: *"output directory (must be git-ignored, see below)"*), and this directory will
hold the customer's actual `.twbx`/`.tdsx`. `.gitignore`'s own comment assigns
`harvest_estate_assets.py` output to **`/_sweep*/`** (`/_harvest*/` belongs to
`harvest_tableau_public.py`'s public-corpus tool) ✅ verified. And ⚠️ measured 2026-08-13, the trap
that makes this urgent is now gated by code: `harvest_estate_assets.py` refuses to start unless git
already ignores probe files under `--out` (or `--allow-unignored-out` is passed deliberately) —

```powershell
git check-ignore -v _sweep/x.twbx        # ignored
git check-ignore -v _sweep2/x.twbx       # ignored too: /_sweep*/ is a prefix glob
```

The natural move when `_sweep/` is already occupied by a previous run — appending a date or a suffix
— is covered now, but the operational rule did not change: **`git check-ignore -v` before the
download, every time**. It is one second, the script enforces it, and this repo has already paid for
a history rewrite.

The sweep is worth more than the download: a workbook that one parser reads and the other refuses is
a finding *by construction*, and which way round it fails says which tier owns it. It also turns an
upstream feature request from an anecdote into an estate-wide failure distribution.

⚠️ **Downloads are the session-fragile part.** Tableau Cloud drops sessions intermittently and the
failure is a `401002` mid-loop, so each asset is fetched with its **own** sign-in. Measured: a shared
token truncated a 58-asset run repeatedly; fresh-per-asset completed. Slower, and the only thing that
finishes. Assets are fetched **by LUID, never by name** — Tableau permits duplicate names across
projects, and name-keyed identity has already produced four separate defects here.

Reference run ✅ verified from the harvest log: `55 asset(s) — ours failed 0, his failed 0, both
parsed 55` in 120 s.

Useful flags ✅ verified against `--help`: `--limit N` (quick pass), `--skip-download` (reuse
`<out>/assets`), `--workbooks-only`, `--db _assessment/estate.db` (take LUIDs from the assessment).

### Step 5 — convert

**This is the gate.** The engine exits 0 regardless; our wrapper is what turns its report into an
answer.

`run_estate.py` exit codes ✅ verified — read out of the `EXIT_*` constants and `final_verdict()`,
and 2/5 reproduced by running the command:

| exit | constant | meaning | what to do |
|---|---|---|---|
| `0` | `EXIT_OK` | `ESTATE: READY` — DoD not failed, no approval collisions, no empty models | proceed |
| `1` | `EXIT_ENGINE_FAILED` | the engine itself exited non-zero | read the last 2000 chars it printed (the wrapper prints them to stderr) |
| `2` | `EXIT_USAGE` | ✅ reproduced both shapes: `--input` missing without `--slice-only` prints `ERROR: --input is required…`; a missing `--output` is argparse's own exit 2 | fix the command |
| `3` | `EXIT_DOD_FAILED` | engine's definition of done is `failed` | **§3 decision point** — do not deploy |
| `4` | `EXIT_COLLISION` | two models claim the same calc name with different formulas | resolve before approving DAX |
| `5` | `EXIT_ENGINE_SOURCE` | **live** ✅ reproduced: a non-canonical `--engine` without `--allow-noncanonical-engine` | see §1.2 |
| `6` | `EXIT_EMPTY_MODEL` | **live** — a model would open and load **zero rows** (an Import partition over a flat file that never landed) | read `<bundle>/empty-model-check.json`; see the block below |

❌ **Correction: exits 5 and 6 are NOT "pending branch only"** — the previous edition said so, and
§5.1 check 10 was written against the same stale assumption. Both shipped 2026-08-13 (#109, #111).

⚠️ **One run, one exit code — but an estate can trip more than one gate.** `final_verdict()` returns
the **first** blocking verdict in a fixed order (collision → DoD → empty model), so a bundle that is
both `failed` **and** carries an empty model exits **3** and never mentions 6 in its exit status.
The empty-model block *is* printed — deliberately before the verdict, and on a pass as well as a
fail — so **read the log body and `empty-model-check.json`, not just `$LASTEXITCODE`**. ⚠️ reported:
this is exactly what happened on the cold run, which saw the `EMPTY-MODEL CHECK` block and an exit 3.

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

What a bundle contains ✅ verified against the reference bundle by listing it, 2026-08-13:

| path | what | may I edit it? |
|---|---|---|
| **`summary.md`** | **read this first** — the engine's human-readable estate report (~30 KB). Opens with the DEFINITION OF DONE verdict and a one-line reason **per failing workbook**, then the pending review gates, then a copy-pasteable list of every openable `.pbip`. It answers "what happened and why" faster than any JSON here | no |
| `report.json` | the engine's full estate report (~1.8 MB for 38 workbooks) — the machine-readable form of the above | no |
| `handover/` | one slice per workbook, so a per-workbook agent never loads the whole report | no |
| `pbip/` | **the working copy** — one folder per deployable unit; what `deploy_estate.py` reads | **yes** |
| `reports/`, `semantic_models/` | the engine's pristine output — the free baseline for `diff` | **never** |
| `data/` | flat-file data landed next to the models that import it (4 folders on the reference bundle) | no |
| `empty-model-check.json` | the exit-6 verdict: every model, its partitions, and why any of them would load zero rows. **Written on every run, pass or fail** | — |
| `phase-timings.json` | per-phase elapsed seconds — see the note under §2's table for what it does *not* cover | — |
| `engine-output-receipt.json` | hashes of the engine's output **and `engine` — what built this bundle** (§1.2) | — |
| `.credential-gate-audit.log` | append-only audit trail; `credential_gate.py verify` reads it. **This is the non-narrative record** — trust it over any summary, including your own | — |
| `input_manifest.json`, `source-provenance.json` | what went in, and where upstream it came from | — |
| `deploy-journal.jsonl` | written by step 6, not step 5: intent-then-outcome per item (§6.1) | — |
| `deploy-estate-id.txt` | **minted at first deploy — keep it with the bundle** (§3.3) | — |

**`pbip/` is not one folder per workbook.** On the reference bundle it held **39** folders = **23**
converted workbooks + **16** datasource-only semantic models ✅ verified by reconciling `pbip/`
against `report.json`. That is model-first migration working as intended: a shared published
datasource becomes one model, not one per consumer. **The item count is not the workbook count** —
get the real number from `deploy_estate.py --dry-run`.

#### If one model is empty and the rest of the estate is fine

The verdict says *"never deploy it: it looks finished and shows nothing"*, and the two fixes it
offers — land the file and repoint the partition, or make the table live — are both *repairs*. On a
workshop clock the third option is usually the real one: **deploy the other N and leave that one
behind.**

✅ **Use the supported skip path — do not hand-move folders.** Verified against
`deploy_estate.py --help` and the module docstring: `--skip <unit>` withholds one named unit, and
`--skip-empty-models` withholds every unit that `<bundle>\empty-model-check.json` reports EMPTY.
Issue #127 is closed by #140.

Run a dry run first and confirm the item count drops by the withheld unit's model/report count:

```powershell
python scripts\deploy_estate.py --bundle <bundle> --workspace <workspace-id> --skip-empty-models --dry-run
# or, for one known unit:
python scripts\deploy_estate.py --bundle <bundle> --workspace <workspace-id> --skip <unit-folder> --dry-run
```

The count dropping by exactly **2** (the model and its report) is the confirmation signal — a drop of
1, or no drop, means you withheld a model-only unit or named the wrong folder. A `--skip` name that
matches no directory under `<bundle>\pbip` refuses to start rather than silently deploying it.
Record the withheld unit in the brief: an accepted gap is a decision, an unmentioned one is a
defect. The real run exits **3 / `EXIT_INCOMPLETE`** when the attempted deploy succeeds but a unit
was withheld (§2 step 6).

### Step 6 — deploy

```powershell
python scripts\deploy_estate.py --bundle _bundle --workspace <workspace-id> --tenant <tenant-id> `
    --estate-db _assessment\estate.db --journal _bundle\deploy-journal.jsonl --dry-run
```

**Always `--dry-run` first.** It prints, per workbook, where it would land and whether it is
`model + report` or `model only`, how many folders it would create, how many workbooks have no known
Tableau project (they land at the root), and finally the **item count** — *"the number to agree
BEFORE deploying, since each item carries a cost in the customer's capacity and licensing terms"* ✅
verified. On the **2026-08-12 reference bundle** a dry run plans **76 items** — 39 units × 2, minus
2 reports skipped as empty ✅ re-verified 2026-08-13 by calling `discover()` + `report_is_empty()`
offline against that bundle. (The cold run's newer bundle landed on 76 too, but only *after* the
empty model was withheld — do not read one estate's number as the other's.)

Then drop `--dry-run`.

⚠️ **Budget ~25 seconds per item, and say "about half an hour", not "about a quarter of an hour".**
The previous edition said *"~13 min / 64 items"* (~12 s/item) and was **2.4× optimistic**: measured
2026-08-13, **76 planned / 74 created took 30.4 minutes** — ~24 s per item, with nothing failing. It
is simply slow: every item is a create plus a `202` polled to a terminal state, and the poll's
own first sleep is 3 s ✅ verified (`await_operation`). This is the **only planning number an
operator has**, so an under-promise here is half an hour of silence in front of a customer.

**Confirm the rate from the journal after the first ten items** rather than trusting either number —
one line is appended per intent and per outcome, so it is a live progress meter:

```powershell
# in a second shell, while the deploy runs
(Get-Content <bundle>\deploy-journal.jsonl | Measure-Object -Line).Lines   # re-run; watch it climb
Get-Content <bundle>\deploy-journal.jsonl -Tail 3                          # what it is doing right now
```

Elapsed ÷ items-done, extrapolated to the dry-run count, is a better estimate than anything in this
document — and it is the number to give the customer once you have it.

`deploy_estate.py` exit codes ✅ verified (the `EXIT_OK` / `EXIT_FAILED` / `EXIT_PREFLIGHT` /
`EXIT_INCOMPLETE` constants):

| exit | constant | meaning | what to do |
|---|---|---|---|
| `0` | `EXIT_OK` | everything planned was deployed (skipped-as-empty reports are reported honestly, not counted as deployed) | proceed |
| `1` | `EXIT_FAILED` | at least one item failed, **or one or more workbooks were refused** — the run names them | fix the named failures/refusals, then re-run the same command to resume |
| `2` | `EXIT_PREFLIGHT` | never started: workspace missing, no access, item budget does not fit, an unreadable empty-model report was requested, or a `--skip` name matched no unit | fix the preflight/refusal message; for a bad `--skip`, use a directory name under `<bundle>\pbip` (or `--dry-run` to list them) |
| `3` | `EXIT_INCOMPLETE` | everything attempted succeeded, but one or more whole units were deliberately withheld by `--skip` / `--skip-empty-models`; nothing was created for those units, so the estate is knowingly incomplete | repair the withheld units, then re-run without the skip to finish the estate; a caller that intentionally withheld them may accept exactly this code |

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
  indistinguishable from success until `/operations/{id}` is polled. **This is not only a create
  problem** — it applies to every long-running item call, including the `getDefinition` a verifier
  reaches for. See §5.1 check 8, where the naive version fabricates a plausible false defect.
- **Duplicate item names.** Cite the deployer's settled source-of-truth; do not restate an older
  contradiction here:

  > **Measured against a real Fabric tenant, via this script's own path** (`POST
  > https://api.fabric.microsoft.com/v1/workspaces/{id}/items`, Create Item, item types `Report` and
  > `SemanticModel`): **Fabric did NOT reject a second item with the same `displayName` and type** —
  > two identical pairs sat side by side in the workspace afterwards.

  ✅ verified 2026-08-13 from `deploy_estate.py`'s module docstring. The code still handles
  `ItemDisplayNameNotAvailableYet`/`ItemDisplayNameAlreadyInUse` defensively as *"already there, go
  verify"*, but the same source states those errors have **never been observed** for this create path
  and are **not** evidence that the service rejects duplicates. Operational rule: assume a duplicate
  name will be *accepted*, and let the journal (§6.1), run lock and ownership guard (§3.3) do the
  protecting. Nothing downstream catches it if you are wrong.

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

### 3.2 After convert — the DoD gate (`exit 3`) and the empty-model gate (`exit 6`)

`bound=<X>/<N> failed=<F> warned=<W>` is the whole decision. Read the *per-workbook* detail, never the
summary line: start with `summary.md` (one line per failing workbook, in English), then `report.json`
and the `handover/` slices.

**Ask:** resolve the failing workbooks, explicitly accept them as out of scope, or narrow the estate?

**Do not deploy an `exit 3` bundle** because "most of it is fine". If the customer accepts the gap,
record that acceptance in the brief — an accepted gap is a decision, an ignored one is a defect.

**Ask about any empty model in the same breath, because you will only get one exit code.** A bundle
that fails the DoD *and* contains a model that would load zero rows exits **3** and never says 6
(§2 step 5). Read `empty-model-check.json` on every run, and put the question in this same block:
repair it (land the data, or make the table live), or quarantine the unit and deploy the rest — the
folder-move recipe and its dry-run confirmation are in §2 step 5.

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

> **Two rules that pre-empt half of this section.** ① A **`202 Accepted` is not an answer** — from
> any Fabric item call, create *or* `getDefinition`. The body is empty; you must poll the operation
> and then read `/result`. Parsing the `202` itself yields a clean-looking, entirely fictional
> result — see §5.1 check 8 for the recipe and the false defect it prevents. ② When a Fabric call
> disagrees with what you believe about your access, **check the token before you check the object**
> (§4.4).

### 4.1 `estate_survey.py` sits there doing nothing

| | |
|---|---|
| **symptom** | the command produces no further output and never returns. ⚠️ reported: 13 minutes lost; the tell was **0.11 CPU-seconds and zero network connections** |
| **cause** | it is **blocked on a hidden `getpass` prompt** for the PAT secret |
| **check** | `Get-Process -Id <pid> \| Select-Object CPU` — near-zero CPU with no sockets is a prompt, not work. Then: was the engine run through `scripts/run_engine_survey.py`? |
| **fix** | use `python scripts/run_engine_survey.py … --env-file .env`; it supplies the engine's legacy spelling and `--no-prompt` automatically |

**Why the wrapper is required** ✅ verified: our scripts document the secret as
`TABLEAU_PAT_SECRET`; the engine reads its legacy `TABLEAU_PAT_VALUE` spelling. The wrapper reads
either spelling from `.env`, exports both to the engine, and invokes it with `--no-prompt`.

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

### 4.4 `WorkspaceNotFound` on a workspace you know exists

| | |
|---|---|
| **symptom** | `WorkspaceNotFound` / `EntityNotFound` (an HTTP **404**) for a workspace you can see in the portal — worst case, one you have *just* filled with items |
| **cause** | the token minted successfully **for the wrong tenant**. Fabric answers "not found" for a workspace your identity cannot see, so an identity problem arrives dressed as a missing object |
| **check** | **decode the token's `tid` before you check anything about the workspace** — the snippet in §1.3b prints `tid`/`aud` without printing the token |
| **fix** | re-mint scoped: `az account get-access-token --resource https://api.fabric.microsoft.com --subscription <sub-id-in-that-tenant>`. See §1.3b for why `--tenant` alone can make it worse, and why `deploy_estate.py --tenant` inherits the ambiguity |

⚠️ reported: ~15 minutes lost, and it recurred **four times across two independent operators on
2026-08-13** — every time presenting as a 404 rather than as an identity problem. If a `404` and a
`403` disagree with what you believe about your access, believe the token.

A genuine 404 (wrong workspace id, or an identity that truly lacks access) looks identical, so this
check is cheap *because* it is unambiguous: a `tid` that matches the landing zone's tenant rules the
whole class out in one command.

### 4.5 `deploy_estate.py` refuses a workbook

Read the refusal text — it is written to be actionable. Three distinct shapes ✅ verified:

| message contains | means | do |
|---|---|---|
| *"carries no marker from a previous deploy, and this run's journal has no record"* | an item of that name exists and is **not** ours | rename the workbook, use an empty workspace, or `--adopt-existing` **only if the zone is genuinely ours** |
| *"N items share this name, so which one to update is ambiguous"* | duplicates already in the workspace | remove the extras in the workspace, re-run |
| *"came from '<other-estate>', not '<this-estate>'"* | a **second estate** in the same landing zone | separate landing zone per estate — or `--adopt-existing` if you truly mean to overwrite |

### 4.6 Deploy stops partway naming connectivity

✅ verified: after **3 consecutive** `HTTP 0` failures (our client failing to resolve/reach the host,
not a service verdict) the run stops rather than marking the rest of the estate failed. A laptop
moving between networks mid-deploy previously burned through an entire estate emitting
`getaddrinfo failed`.

**Fix connectivity, then re-run the identical command.** Everything already deployed is skipped by
**content hash** — see §6.

### 4.7 `401 TokenExpired` mid-deploy

Handled ✅ verified: the token re-mints itself once on `401 TokenExpired` and continues. A 66-item
deploy previously outran its token and failed every remaining call. If you still see it, `az login`
has expired entirely — re-authenticate and re-run. ⚠️ If re-authenticating "fixes" it but the
workspace then 404s, you have changed identity as well as refreshed it — §4.4.

### 4.8 A report fails with `invalid package content stream`

The report has **no pages** (`pageOrder: []`), because the source workbook had no convertible
worksheets. ✅ verified: `report_is_empty()` detects this *before* the call and skips it with
`report has NO PAGES - skipping (the model is still deployed)`, so it should not reach you as a
service error. If it does, you are deploying by hand rather than through this script.

### 4.9 The engine's pristine output fails `powerbi-report-author validate`

⚠️ reported (issue #108), engine 2.126.0. Straight out of `run_estate.py`, no agent edits:
`PBIR_FORMATTING_PROP_UNKNOWN` on `azureMap` `dataPoint.defaultColor`, and `PBIR_ROLE_MAX_EXCEEDED`
on a `scatterChart` with two `Category` projections. Exit 1.

**We do not yet know whether the gate or the engine is wrong.** Do not present a red `validate` from
the engine's own output as a migration defect, and do not "fix" it by editing `reports/` (that is the
pristine baseline — §0). Also stale in the same area: `viz_fidelity` still labels these visuals
`shape_map`/`filled_map` and advises enabling a **preview feature that is no longer needed**. Do not
send a customer to that toggle.

### 4.10 Something feels stuck

**Report elapsed time whenever an operation exceeds ~60 s.** An anomaly in elapsed time or tool-call
count is a signal, not noise. Ground truth is readable *mid-run* — `phase-timings.json`, the deploy
journal, the artifact folder — and reading it early is what has caught real failures before a run
self-reported success.

### 4.11 `assess_estate.py` says DEGRADED, or exits 3

| | |
|---|---|
| **symptom** | the run finishes and `report.md` opens with a `DEGRADED` banner; the process may exit `3` |
| **cause** | one or more listings could not be read — a timeout, a dropped connection, or a refusal. Before #193 this was a **traceback** that discarded the whole run (three consecutive failures on one customer estate, on `customviews`, `groups/{id}/users`, `customviews`) |
| **check** | `listing_errors[]` in `assessment.json` (or the `listing_error` table in `estate.db`) names the endpoint, page, attempt count, elapsed seconds, and `transport: true` when no status code ever came back |
| **fix** | `transport: true` and slow (elapsed ≈ the timeout) → raise `--rest-timeout`; `transport: true` and fast → raise `--max-attempts` / `--retry-budget`; status `401`/`403` → a credential or permission problem, which **no retry can fix** |

**Exit `3` is not a crash — it is a refusal to let a partial inventory pass as an estate.** Exit `0`
with a blockquote banner means every primary listing was read in full and only a deliberate-use
signal is missing; that assessment is usable, with the retire tier flagged as unproven.

The pass-1 inventory is written to `_assessment/raw/` **before** the flakier passes run, so even a
later failure leaves the expensive part on disk.

---

## 5. Verification checklist

Run in order. Each line says what it proves — and §5.2 says what none of them prove.

### 5.1 What we can check

| # | check | how | pass |
|---|---|---|---|
| 1 | conversion was adjudicated | `echo $LASTEXITCODE` after step 5 | `0` |
| 2 | DoD not failed | `summary.md`'s first heading, and the `definition_of_done` line in the run summary | `status` ≠ `failed` |
| 3 | inputs are accounted for | `_sweep/parse-sweep.md` (§2 step 4 — **not** `_harvest/`) | `ours failed 0, his failed 0` |
| 4 | the item count was agreed | `deploy_estate.py --dry-run` | number matches what the customer signed off |
| 5 | deploy completed | `echo $LASTEXITCODE` after step 6 | `0`, or `3` when you deliberately withheld a unit (`1` = at least one failure or refusal — read the named list) |
| 6 | nothing was silently skipped | the final line: `all N item(s) deployed` **or** `N deployed; M skipped as empty` | M is a number you can explain |
| 7 | the journal has no unfinished intent | `Select-String -Path <bundle>\deploy-journal.jsonl -Pattern '"status":"failed"'` | no hits, or hits you have triaged |
| 8 | every report resolves its model | `python scripts\verify_bindings.py --workspace <workspace-id> --tenant <tenant-id>` (the API, polled) | exit `0`: every report resolves to a `SemanticModel` in this workspace; exit `1` = findings; exit `2` = the check could not be performed |
| 9 | the estate identity survived | `Get-Content <bundle>\deploy-estate-id.txt` | non-empty, and stored with the bundle |
| 10 | the engine version is recorded | `.engine` in `<bundle>\engine-output-receipt.json` (§1.2) | present, with `canonical: true` |
| 11 | no model would load zero rows | `<bundle>\empty-model-check.json` | `"status": "OK"`, or a unit you deliberately withheld with `--skip` / `--skip-empty-models` (§2 step 5) |
| 12 | connections the customer must make | `python scripts\connections_manifest.py --bundle <bundle> --out <dir>` | `connections.md` delivered |

❌ **Correction: check 10 no longer looks for `ENGINE-VERSION.txt`.** That file was a manual
workaround, deleted with §1.2 — the previous edition's check therefore **failed on a correct
bundle**. Read the receipt:

```powershell
python -c "import json;print(json.load(open(r'<bundle>\engine-output-receipt.json',encoding='utf-8')).get('engine'))"
```

An absent `.engine` key means the bundle predates #109 (2026-08-13), not that something is wrong with
today's run — see §1.2.

#### Check 8 — the recipe, and the false defect it prevents

Check 8 used to say *"open each report in the portal"*, which nobody does at 36 reports, so an
operator reaches for the API — and **the obvious call returns a clean-looking wrong answer**:

> `POST /v1/workspaces/{ws}/items/{id}/getDefinition` returns **`202 Accepted` with an empty body**.
> Parsing that body yields `byPath=False  semanticModelId=NONE`, which reads exactly like a report
> bound to nothing — a real and serious defect. It is not. It is the *"`202` tells you nothing"*
> trap that `deploy_estate.py`'s docstring documents for **create**, applying identically to
> **getDefinition**.

⚠️ reported: 12 minutes lost on 2026-08-13 and a critical bug report nearly filed — and it caught a
second, independent operator the same day. That is a property of the documentation, not of the
operators, which is why the recipe now lives here.

**Poll it.** The shape ✅ verified against `deploy_estate.py`'s own `await_operation` + `/result`
handling, which is the same contract:

1. `POST {API}/workspaces/{ws}/items/{id}/getDefinition` → `202`, and a **`Location`** header (fall
   back to `{API}/operations/{x-ms-operation-id}` if absent);
2. `GET {Location}` every few seconds until `status` is `Succeeded` / `Failed` / `Undetermined` —
   ~15 lines, and the only part the naive version skips;
3. `GET {Location}/result` → the real body: `definition.parts[]`, base64 in `payload`;
4. decode the part whose `path` is `definition.pbir` and read `datasetReference`:
   `byConnection.connectionString` should contain `semanticModelId=<guid>`; a surviving
   `byPath` means the rebind did not happen.

For orientation, the *pre-deploy* form on disk is the failing one — ✅ verified by reading a
`definition.pbir` straight out of the reference bundle:
`{"datasetReference": {"byPath": {"path": "../<name>.SemanticModel"}}}`. Step 6 rewrites that to
`byConnection` at deploy time (§2 step 6), so `byPath` in the *service* is the defect signal;
`byPath` in the *bundle* is normal.

⚠️ reported, 2026-08-13: **36/36 reports carried a `semanticModelId` guid resolving to a semantic
model in the same workspace, none `byPath`.** Which proves they bind — and nothing more; see §5.2.

**Check 12 is a deliverable, not a diagnostic.** Credentials do not travel with a migrated item.
Without this list the customer discovers which sources need connecting **one failed refresh at a
time**. It never emits a secret (host/database/account are configuration; passwords and keys are
not — a test proves no credential-shaped value reaches it), and it never calls an extract
"connected": a model built from a materialised `.hyper` is a **snapshot frozen at extract time that
will never refresh**, listed separately and labelled. It is ordered by **blast radius** — a published
datasource feeding twelve workbooks is a different task from one feeding a single archived report.
✅ verified from the module docstring.

### 5.2 What this does NOT prove — say this out loud

❌ **We have never verified that a migrated report RENDERS.** The strongest claim we can make is
⚠️ reported: **36/36 reports on 2026-08-13, and 31/31 on an earlier estate, bound `byConnection` to a
real semantic model in the service** (check 8). **Binding resolves ≠ the report works.** We have not
confirmed the visuals draw, with data, matching the Tableau original. A better check-8 recipe raises
the *confidence* of the binding claim; it does not change *what is claimed*. Treat the deployed
estate as *ready for review*, never as *validated*.

❌ **A green `validate` is necessary, not sufficient.** TMDL deserialization and
`powerbi-report-author validate` both pass defects that only surface in Desktop **with data**. And
`validate` reports 0 errors even when it could not fetch the visual schema at all
(`PBIR_SCHEMA_UNREACHABLE`) — `preflight.ps1` checks mechanically whether a green result this session
means "schema-checked" or only "structure-checked".

❌ **The engine's own output currently fails `validate`** on this estate (§4.9, issue #108), so a red
result there is not evidence about *your* migration.

❌ **No numbers have been reconciled against Tableau.** "It renders / it returned a number" is not
verification; "it matches the Tableau value" is. That comparison is the `pbi-migration-validator`
agent's job and is a separate phase.

❌ **Refresh has not been proven.** Live sources need a connection + credential established in the
target workspace (check 12) before any refresh can succeed.

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

**Ignored ✅ verified by running `git check-ignore -v` on every row, 2026-08-13:**

| path | rule |
|---|---|
| `.env`, `.env.local` | git-ignored — prove it, do not trust a line number here |
| `_assessment*/` | `/_assessment*/` — real estate: workbook/project names, owner LUIDs, group membership, permissions |
| `_sweep*/` | `harvest_estate_assets.py` output — downloaded `.twbx`/`.tdsx` from a real site |
| `_harvest*/` | ⚠️ **a different tool's** output (`harvest_tableau_public.py`'s public corpus). Ignored, but not where §2 step 4 should write |
| `_bundle*/`, `_estate*/` | convert output — `report.json` carries every workbook name and calc formula |
| `migrations/workshop-*/`, `engagement-*/`, `customer-*/` | engagement notes |
| `**/data/`, `**/source/*.twb` | extracted customer data and source workbooks |

⛔ **The harvest entries are prefix globs now, not exact directory names.** ✅ verified from
`.gitignore` (`/_harvest*/`, `/_sweep*/`) and `git check-ignore -v`, after #137/#125:

| path | ignored? |
|---|---|
| `_harvest/`, `_sweep/` | **yes** |
| `_harvest-op/`, `_sweep2/`, `_harvest-2026-08-13/` | **yes** — `/_harvest*/` and `/_sweep*/` are globs |
| `_assessment2/`, `_bundle2/`, `_bundleX/`, `_estate9/` | yes — `/_assessment*/`, `/_bundle*/`, `/_estate*/` are globs |

The old failure was entirely natural: `_sweep/` already holds yesterday's run, so you date the new
one, and the dated name was not covered. That is fixed, but **prove your exact path with
`git check-ignore -v` before the download** anyway. The cold operator tested three candidate names
before fetching a single file — that is the correct amount of paranoia here.

**Two holes closed when this runbook landed** (PR #110):

| path | why it was a hole |
|---|---|
| `migrations/workbooks/<slug>/migration-brief.md` | the rule was `/migrations/*/migration-brief.md` — **one level deep** — but `AGENTS.md` instructs writing the brief **two** levels deep, at `migrations/workbooks/<slug>/`. `git check-ignore` returned **NOT IGNORED** for the exact documented path. The brief holds the customer's name, scope and destination workspace. |
| `_bundle/` (and `_bundle*/`) | the conventional convert output. It contains `report.json` with every workbook name, every calculated-field formula, and the generated TMDL/PBIR — i.e. the customer's content — while its sibling `_assessment*/` was already ignored. |

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

**Exit codes** ✅ verified 2026-08-13

`—` means that script cannot return that exit code.

| script | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| `preflight.ps1` | ready | critical missing | — | — | — | — | — |
| `assess_estate.py` | assessed (may be secondary-degraded) | nothing assessed · sign-in refused (raises) | usage | **a PRIMARY listing is incomplete** | — | — | — |
| `run_estate.py` | READY | engine failed | usage | **DoD failed** | approval collision | non-canonical engine | **empty model** |
| `deploy_estate.py` | all deployed | item failed / refused | preflight | **incomplete by skip** | — | — | — |

One run returns **one** code, in the order collision → DoD → empty model — so a bundle can trip a
gate the exit code never mentions. Read the log body and `empty-model-check.json` too (§2 step 5).

**Where the truth lives**

| question | file |
|---|---|
| what happened, in English? | `<bundle>/summary.md` — **start here** |
| how long did each phase take? | `<bundle>/phase-timings.json` (sum of *recorded* phases, not wall clock) |
| what did the engine actually produce? | `<bundle>/report.json`, `<bundle>/handover/<workbook>.json` |
| which engine built this bundle? | `.engine` in `<bundle>/engine-output-receipt.json` (§1.2) |
| would any model load zero rows? | `<bundle>/empty-model-check.json` |
| what did the deploy do, and when? | `<bundle>/deploy-journal.jsonl` — also the live progress meter |
| which estate do these items belong to? | `<bundle>/deploy-estate-id.txt`, and each item's description in the service |
| what must the customer connect? | `connections.md` from `scripts/connections_manifest.py` |
| what is in scope, and who decided? | the migration brief (git-ignored) |
| am I holding the right token? | decode `tid`/`aud` — §1.3b |

**Related reading:** [`/AGENTS.md`](../AGENTS.md) (dispatcher flow, Gate B, shared conventions) ·
[`docs/capabilities-and-limitations.md`](capabilities-and-limitations.md) ·
[`docs/data-source-credentials.md`](data-source-credentials.md) ·
[`docs/credential-gate.md`](credential-gate.md) ·
[`docs/migration-programme.md`](migration-programme.md)
