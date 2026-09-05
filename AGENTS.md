# AGENTS.md — shared conventions for this repo's Copilot agents

Auto-loaded by GitHub Copilot CLI (and other agent runtimes) for every session in this repository. It
has two jobs: (1) name the **environment contract** so the repo is self-configuring, and (2) hold the
**canonical copy of the shared agent conventions**, which `scripts/sync_agent_conventions.py`
generates into every `.github/agents/*.agent.md`.

> **Why generated, not inherited:** a custom-agent **subagent** receives ONLY its own persona file —
> this file, `.github/copilot-instructions.md` and user-global instructions do **not** reach it
> (verified 2026-07-30 with a sentinel experiment; all four agents confirmed independently). There is
> no `include`/`extends` mechanism, so the block at the end is *duplicated* into each persona on
> purpose and CI fails when a copy drifts. **Edit it here, then run
> `python scripts/sync_agent_conventions.py`** — never edit the copy inside an agent file.
>
> `--check` reports three failures in one run, the path first: a documented `<bundle>/…` path that is
> **not a real bundle directory**, **drift**, and a persona over the **30,000-char cap** (measured on
> the whole file). It scans the block *and each persona in full*, and **write mode exits non-zero
> too** — that run has propagated the error, not merely proposed it. `--bundle <dir>` also resolves
> location-shaped paths (`<bundle>/reports/`) on disk. Rationale and the defects behind each rule:
> [`docs/agent-architecture.md`](docs/agent-architecture.md).

> **VS Code users:** VS Code Copilot auto-loads `.github/copilot-instructions.md`, *not* this file.
> That pointer duplicates only the session-start step below and defers everything else here.

**Navigation:** use [`docs/INDEX.md`](docs/INDEX.md) as the tier-2 map before searching blindly.
Subagents read it as step 0; `scripts/check_navigation_index.py` checks it bidirectionally.

---

## Session start, do this first (before any other work)

```
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1 -Update -CheckUpstream
```

`-Update` repairs the npm bridge CLIs **only when they are below the correctness floor** — a floor
check, not a blind `@latest`, so at or above the floor it costs nothing.

**Why a floor:** `powerbi-report-author` **>= 0.1.4** is a *correctness* floor. Older builds returned
`errorCount: 0` for PBIR that Power BI Desktop cannot open — e.g. a `report.json` whose
`themeCollection` entries are missing `reportVersionAtImport`, which is **required inside each
`themeCollection` entry and forbidden at the top level** (mutation-by-mutation evidence and the
committed ground-truth shape live in the `powerbi-report-gotchas` skill). A stale CLI silently
green-lights a broken report.

**Above the known-good matrix is a WARN, not an error.** It means the version-specific gotchas in
`.github/agents/` and the skills were verified against an older build and may be stale — re-verify
the prose; never "fix" it by downgrading.

**`-CheckUpstream` is opt-in (~3 s of network) and advisory — it never upgrades and never fails the
run.** Every other check compares an installed version against a hard-coded number: that answers "is
what I have good enough", never "has the world moved". It asks npm for `powerbi-report-author` /
`powerbi-desktop` and GitHub for the engine's upstream `VERSION`. Measured 2026-08-06: the engine
moved **2.60.0 → 2.72.0** unnoticed and **Power BI Desktop auto-updated** and broke the bridge's exe
discovery, both while preflight still reported "Ready to migrate".

**The timing rule is what makes upgrading safe:**

| When | Run | Why |
|---|---|---|
| Session start (nothing in flight) | `preflight.ps1 -Update -CheckUpstream` | Safe; the floor is a correctness floor, and this is the one moment upgrading is allowed |
| Migration start (orchestrator step 0) | `preflight.ps1` (plain) | Confirm READY without swapping tooling mid-flow, and without a network round trip on every migration |
| Mid-migration | **don't upgrade the installed tooling** | The variable is not the calendar but *work already validated by the current CLI*: swap the validator mid-build and earlier results are no longer covered by the same check. A version-*comparison* run into a fresh output dir is not an upgrade (see the engine rules in step 1) and is not forbidden |

It cannot update the **skill bundles**: `copilot plugin update` hits a file lock while any Copilot
session is running. That lock blocks renaming the plugin directory, not writing inside it, so a
*content* refresh needs no restart: `python scripts/sync_installed_skills.py`.

---

## Required Copilot setup (self-configuring dependencies)

The environment contract lives in **`scripts/preflight.ps1`**. It is the gate, not this prose: it
checks the required tools, plugins, MCP servers, Python dependencies, Desktop bridge assumptions and
version floors, prints the install or repair hint beside the failing item, and **exits non-zero for
any critical miss**.

```
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1
```

**If setup guidance is missing or too thin, improve the preflight hint rather than adding another
install recipe here.** An agent that never reads this section should still be stopped by the exit
code before it can migrate with a broken environment.

The one judgement the gate cannot carry: **a skill bundle can be stale even when repo-local
`.github/skills/` is correct**, because an installed plugin copy with the same name shadows it and
preflight blocks on `STALE in plugin`. Fix content drift immediately with
`python scripts/sync_installed_skills.py`; new sessions and newly spawned subagents then see the
refreshed files, while a running session keeps its old in-memory copy.

---

## Delegating and monitoring work (orchestrator discipline, not persona content)

Deliberately **outside** the synced block: this is what *whoever delegates* — the top-level session,
or `tableau-migrator` orchestrating the builders — owes the work once it hands out a task. Incident
evidence behind every rule below: [`docs/agent-operations.md`](docs/agent-operations.md).

- **A subagent's own summary is a claim, not evidence — verify before repeating it.** Run an
  authoritative, non-narrative check: an audit log's `action` field (`probe-cleared` vs
  `manual-clear`), `credential_gate.py verify`'s exit code, an artifact count, a checksum. Measured
  2026-08-02: a summary declared "Sign-off ready: YES" while the gate's log showed it had cleared its
  credential gate unearned.
- **An anomaly in elapsed time or tool-call count is a signal, not noise.** Ground truth is readable
  *mid-run*; reading it early caught that bypass, a misclassified `UNREACHABLE` and a real Desktop
  crash — none of which appeared in the eventual "done" message.
- **When a summary and the ground truth disagree, ground truth wins, unconditionally.** Restate the
  verdict from the evidence; do not soften it to be polite about the subagent's framing.
- **A green CI is the start of review, not the end.** Measured 2026-08-09: five agents fixed eleven
  issues, all five PRs went green, and a blind review — given only the diff and the issue, never the
  author's rationale — requested changes on all five, because each fix had *moved* its failure
  boundary rather than removing it.
- **Review the diff without the author's explanation**; say plainly that a clean bill of health is a
  legitimate outcome (or reviewers invent findings); tell authors to push back with evidence; send a
  re-review to the reviewer who found the defect; and require **`Fixes #N`** in the commit message,
  not merely "reference the issue" — four issues stayed open after being fixed and merged because
  the commits said `(#46)`.
- **After a host crash, in-flight subagent work is UNKNOWN — never assume lost, never assume
  complete.** Do file-level forensics BEFORE re-dispatching: `git status` / `git diff --stat` in the
  target worktree, plus file mtimes against the crash time. Measured 2026-08-19: three agents with
  identical "in progress" status had finished, half-finished and done nothing; blind re-dispatch
  would have overwritten verified-good work.
- **Prefer briefs that land work incrementally** (commit and `git push` as you go) over ones that
  buffer everything until a final write — the first turns a crash into truncation, the second into
  total loss.

### The review contract — state this in the brief BEFORE coding

Measured over eight merged PRs (mean **7.75** pre-merge review rounds): the cause was **an unbounded
claim fixed one site at a time** — 66 % of round-2+ findings shared a defect class with round N−1 of
a *different* PR. Size, file contention, operator routing and proof machinery were each ruled out by
a discriminating case, and the reviews were mostly right, so none of this means "review less". Data,
method and limits: [`docs/review-throughput-postmortem.md`](docs/review-throughput-postmortem.md).
⚠️ **Two rules an earlier edition proposed are contradicted — do not reintroduce them:** a hard
artifact cap (new test files vs rounds is Spearman **−0.695**: *more* test files went with *fewer*
rounds) and an absolute two-round cap (44 false-clean / wrong-object / security findings arrived
after round 2, and those normally block a merge).

1. **Invariant and direction.** State the exact pass / refuse / cannot-establish contract. Name the
   fail-open consequence, the fail-closed consequence, and which one blocks merge.
2. **Closed surface.** Enumerate every consumer, phase, transformation, identity-loss join and
   mutable read that can affect the invariant (`N = ___`); name residuals explicitly. **If review
   finds a new class or an unlisted surface after round 1, do not add another local guard —
   simplify, delete, split, or descope.** This is the stop rule the 66 % recurrence argues for.
3. **Independent oracle.** For each verdict name evidence *not produced by the code under test*, plus
   one positive and one negative control. A proof must fail on its intended assertion; a non-zero
   exit alone is not a kill.
4. **Proof escalation.** Direct tests are the default. A new mutation runner, digest, census, anchor
   map or pin requires **all four**: a real need (customer/repo reproduction, accepted requirement,
   or a mandatory security/data-loss boundary); a severe consequence if the ordinary test is vacuous;
   a **demonstrated** mutation that direct positive/negative tests miss; and evidence the mechanism
   has power over *this* claim. ⚠️ The deciding factor is the **consequence of vacuity**, not file
   type and not the guard's nominal direction. Machinery larger than the product change is a
   **split trigger**.
5. **Round route.** R1 reviews the invariant and the enumerated surface; R2 checks regressions and
   whether the class is closed. **After R2 freeze scope**: a further defect *in the same class* may
   be fixed; a **new class or new proof mechanism** forces simplify/delete/split/descope. Fail-open,
   security and data-loss findings block; fail-closed, diagnostic and proof residuals become issues.
6. **Integration.** Name shared/contended files and the base SHA. Bring the branch current once
   before final review and **prove the reviewed tree's SHA** — a stale head is how a review round
   gets spent on code that no longer exists.

⚠️ No PR has yet used this contract prospectively, so its benefit is a testable hypothesis, not a
measured result. Record what happens on the first ones that do, and correct it from that evidence.

## Concurrency budgets: Desktop RAM, and the agent host

Two different resources, both measured; neither is about addressability, because concurrent Power BI
Desktop instances *are* addressable (the bridge and the AS-port lookup are PID-scoped). Dumps,
numbers and what remains unexplained: [`docs/agent-operations.md`](docs/agent-operations.md).

- **Power BI Desktop spends machine RAM.** ⚠️ Inferred from a 2026-08-19 field incident, not a
  controlled reproduction: Desktop crashed with
  `Microsoft.Mashup.Host.Document.PlatformDependentOptions` at 4–5 open instances with ~3.1 GB free
  of 31.7 GB, and deleting the model's 313 MB `.pbi/cache.abf` did not fix it. Keep large-model
  concurrency low, check free RAM before opening another instance, and close each instance as soon
  as its handoff is complete.
- **The agent host spends the V8 heap of `copilot.exe`**, and reaches its limit with no Desktop
  running at all. ✅ Three retained crash dumps all read *"Allocation failed - JavaScript heap out of
  memory"* / `OOMError` at ~3.44–3.48 GB used. **Cap the wave**: six concurrent subagents failed —
  and so did four, so any specific safe number is unproven. Advice to "dispatch the whole wave at
  once" (common in user-level delegation guidance) is silent about host memory; this document does
  not endorse it.
- **A crash takes every subagent's UNPUSHED work.** Committing is not enough. Brief agents to
  `git push` incrementally, and read the crash dump first after a restart — it timestamps the crash,
  which is the reference point the file-mtime forensics depend on.

---

## Starting a migration — the DISPATCHER's job

**Who this is for:** the regular Copilot CLI session that auto-loads this file — the one the human
actually talks to. It is **not** `tableau-migrator`, which is a *per-unit worker* migrating one
workbook against a plan someone else made. Deciding **what** to migrate, **in what order** and **to
where** happens up here: **the dispatcher decides and writes the brief; `tableau-migrator` reads the
brief and executes.**

### Step 1 — work out what you are actually pointing at

Do not ask "which workbook?" until you know what kind of thing you were handed. The four input shapes
take genuinely different first moves, and picking the wrong one is expensive rather than merely wrong
— a site migrated workbook-by-workbook rebuilds a near-identical semantic model N times, and those
copies then drift.

| You were given | First move | Because |
|---|---|---|
| **A Tableau Server/Cloud site** (URL + PAT) | **`python scripts/run_engine_survey.py --server <host> --site <slug> --pat-name <name> --env-file .env --json _assessment/estate_survey.json`** → `python scripts/assess_estate.py --out _assessment --survey _assessment/estate_survey.json` → `python scripts/tableau_lineage.py --plan` → **`python scripts/harvest_estate_assets.py --out <dir>`** → `python scripts/run_estate.py --input <dir>/assets --output <bundle>` | Assess emits *a decision, not an inventory* — but without `--survey` it reports migration **order as unknown**, and a workbook whose published datasource has not landed first rebuilds to an **empty report**. Harvest is the seam: it downloads every workbook and published datasource to `<out>/assets/` as `.twbx`/`.tdsx`, exactly what `run_estate.py --input` consumes. The two-step shape is deliberate — the engine's `LiveTableauSource` is an explicit stub, so there is no one-button live-site→PBIP path. Command-by-command: [`docs/operator-runbook.md`](docs/operator-runbook.md) |
| **A folder of `.twb`/`.twbx`** | `python scripts/run_estate.py --input <folder> --output <bundle>` | Sweeps the whole folder through the deterministic tier and emits per-workbook handover slices. No server, so ordering comes from the parsed specs rather than Tableau's metadata API. |
| **One `.twb`/`.twbx`** | `python scripts/parse_tableau.py <file> -o <spec>` → dispatch `@tableau-migrator`. First-timer route, verified end to end with no server: [`docs/start-with-one-workbook.md`](docs/start-with-one-workbook.md) | The simple path. Still write a brief. |
| **A `.tds`/`.tdsx`** (data source, no workbook) | `parse_tableau.py` accepts it directly | **Phase 1** of a model-first estate: a semantic model with **no report**. Also the fix for a `sqlproxy` published source, whose calcs live on the server and are therefore *under-reported* by any workbook that merely points at it. |

`estate_survey.py` is the deterministic **engine's** script, not ours, so it is not on `PATH`; ask
**`python scripts/engine_source.py`** for its location rather than typing a path, and never point a
step at a second copy. Three things about that invocation bite, all measured: `--server` is required,
`--json` takes a **PATH** rather than being a bare flag, and the PAT **name** rides through from
`.env` only via `run_engine_survey.py` — a *direct* call must supply `--pat-name` or an exported
`TABLEAU_PAT_NAME`, because the engine's `credential_resolver.py` reads only the *secret* from
`--env-file`.

`harvest_estate_assets.py` also runs **both** parsers (ours for fidelity, the engine's for
conversion) over every asset and writes `<out>/parse-sweep.md` / `parse-sweep.json` — an estate-wide
failure distribution, and exactly the evidence an upstream feature request needs instead of an
anecdote.

**Capture the Tableau reference imagery in the SAME trip — the dispatcher's job, not a subagent's.**
A fidelity review later needs a picture of the *source*, and nothing downstream produces it: the
Desktop Bridge `screenshot`/`screenshot-all` commands shoot the Power BI *output*, not Tableau.
Capturing it while you are already authenticated is the difference between one command and a
re-authentication against a server that may since have gone dark (#198). **Pick the tool by the
source:**

| Source | Capture command | Notes |
|---|---|---|
| **Tableau Public URL, or a local `.twb`/`.twbx`** | `python scripts/capture_tableau_reference.py migrations/workbooks/<slug> [--public-url <url> --view <view>]` | Writes a provenance-stamped `reference/manifest.json` — a `capabilities`-carrying `validation_grade` source; its `manual` provider also adopts user-dropped `tableau-*.png` in `reference/`. An existing manifest **short-circuits to exit 0** — re-run with `--force`. |
| **Tableau Server/Cloud** (`TABLEAU_SERVER_URL` configured) | `python scripts/capture_tableau_oracle.py --out _oracle --images [--reference-best] [--workbook "<published name>"]` | The command on the left **exits 3** on an empty target when the URL is set (URL unset → 1): that is *"wrong tool for this source"*, **NOT** "capture is impossible" — misreading it is the whole of #198. Use the oracle, which does the live REST image export. |

Three oracle traps, all verified in `scripts/capture_tableau_oracle.py`:

- **`--out` is required** — omit it and argparse exits 2, capturing nothing.
- **`--workbook` is an exact, case-insensitive *published-workbook-name* filter** (repeatable), so
  substituting a migration slug for the published name silently matches **zero** views.
- **Read the manifest, not just the exit code.** `_oracle/oracle-manifest.json` carries `view_count`
  and each view's per-render status; the exit code compresses that to `0` all captured / `1` partial
  non-credential failure / `2` a view needs a human to re-authorize in Tableau / `3` total
  non-credential failure / `4` no views selected / `5` a required reference render (`--reference-best`)
  never arrived. Renders land at `_oracle/images/<full-view-luid>.png` — the stem is the **validated
  LUID and nothing else**, because a view NAME is response data (a reflected session token once
  arrived as one). Grading and the render ladder: [`docs/reference-capture.md`](docs/reference-capture.md).

Credentials for either tool come from `.env` **or exported environment variables** (the latter win),
never CLI arguments.

**The conversion engine has exactly ONE source: the installed plugin**
(`tableau-fabric-skills@tableau-collection`) — not a sibling clone, not another checkout, not
"whichever one a script finds first". Every step resolves it through **`scripts/engine_source.py`**,
which **raises rather than falling back** — a silent fallback is exactly what issue #107 was.
Measured 2026-08-12, this machine had the engine installed twice at different versions (2.113.0 and
2.126.0), with different pipeline steps resolving different trees and nothing in the output saying
which one ran; they are not equivalent — on one workbook 2.113.0 emitted deprecated Bing `shapeMap`
visuals and **no visual at all** for a density map, where 2.126.0 emitted `azureMap` with a heat
layer. Three defect reports were retracted or nearly retracted before this was enforced.

| mechanism | what it does |
|---|---|
| `scripts/engine_source.py` | the single resolver. `engine_root()` returns the plugin or **raises**; `resolve_engine()` refuses a non-plugin `--engine` unless `--allow-noncanonical-engine` is passed |
| `engine-output-receipt.json` | every bundle records `engine.root`, `engine.version` and `engine.canonical`, so an artifact answers *"what built me?"* on its own ([`docs/operator-runbook.md` §1.2](docs/operator-runbook.md)) |
| `preflight.ps1` | **critical** checks: the plugin is installed (printing its `VERSION`), and **no alternative engine tree exists** anywhere it could be resolved from. A second copy is a MISS, not a warning |
| `preflight.ps1 -CheckUpstream` | advisory: installed engine `VERSION` vs upstream `main`. Being behind is not an error |

Keeping it current: `copilot plugin update tableau-fabric-skills@tableau-collection`, **between
sessions** (a running session file-locks the plugin directory). Mid-session only a *content* refresh
is possible — `python scripts/sync_engine_plugin.py --source <checkout>` — and it refuses a
downgrade, because walking the canonical engine backwards turns a cleanup into the regression above.

**Whether a version change is safe keys on downstream investment, not the calendar** — whether it
would mix two engine versions inside one deliverable, or discard hand-authoring layered on the
engine's output:

| Doing this | Verdict | Check, right now |
|---|---|---|
| **Comparing** two versions — run the second with `--allow-noncanonical-engine` into a **fresh** `--output` dir, keep the old bundle, diff | ✅ always safe — not an upgrade; the installed engine is untouched | is the installed plugin left as-is and `--output` a new dir? |
| Re-running a unit with **~no hand-authoring since it ran** | ✅ re-run into a fresh dir | **both** baseline diffs are ~empty |
| Re-running a unit with **substantial hand-authored TMDL/PBIR** on top | ⛔ finish or checkpoint that unit first | **either** diff is large |
| **Partial** re-run into an **existing** bundle (some workbooks, not all) | ⛔ **never** | `write_engine_receipt` stamps ONE `engine.version` over the whole bundle, so it would claim a single version for artifacts two builds produced — the #107 shape, invisible to `check_engine_receipts.py` |

⚠️ **The `reports/`-vs-`pbip/` diff is structurally blind to hand-authored TMDL** (`reports/` holds
**0** `.tmdl` files), and a workbook bundle may have **no paired model baseline** at all — measured on
a 12-workbook estate, 4 of 12; on run 408, 18 model baselines for 62 `pbip/` units. `git diff
--no-index` exits **1** identically for *"could not access"* and *"they differ"*, so a missing
baseline reads as "large diff, do not re-run". Record an absent counterpart as **BASELINE
UNAVAILABLE**, never as a clean/no-change diff, and never generalise model churn from the paired
subset to an estate. Exact diff commands, both directions and the `--stat` caveats:
[`docs/migration-phases.md`](docs/migration-phases.md); the operational procedure:
[`docs/operator-runbook.md`](docs/operator-runbook.md#engine-model-baseline-availability) (#274, #359).
For *"what have I hand-edited"*, `_build/generated-edit-declarations.json` is the better source than
any diff.

**An engine defect is filed UPSTREAM — `gh issue create --repo Yarbrdab000/tableau-fabric-skills`.**
The plugin tree is read-only, so an issue is the only way a finding reaches the person who can fix
it. **The test: who has to change code?** If the fix edits `migrate_estate.py`, `pbir_lint.py` or
anything under the plugin, it is upstream — even when we also ship a mitigation here; our tracker is
for *our* tier (`scripts/`, personas, skills, docs). When both apply, file upstream and keep a local
issue only for the mitigation, cross-linked. ⚠️ **The two issue-number ranges do not overlap and that
is the trap**: ours passed 200 while upstream was near 141, so a bare `#220` reads as plausible in
either repo. Three engine issues sat in our tracker for days before being re-filed upstream. Routing
detail: [`docs/upstream-issue-gate.md`](docs/upstream-issue-gate.md).

**Where does the output go?** A migration produces a **local PBIP bundle**; publishing it is a
separate, deliberate step. `scripts/deploy_estate.py` lands a bundle in an **existing** Fabric
landing-zone workspace: models first, each report rebound from `byPath` to `byConnection` once its
model exists, crash-safe by a run journal. Run `--dry-run` first — it reports the plan and the **item
count**, the number a customer agrees before you deploy (mechanics: `scripts/README.md`; post-deploy
check: `verify_bindings.py`). It deliberately does **not** decide **topology**: it takes ONE
`--workspace` and never creates one, so cross-workspace shared-model binding, permission mapping and
promotion out of the landing zone remain human decisions (#57). So the question is not "local or
Fabric?" but *"name the destination workspace in the brief, and say whether this run stops at the
bundle or goes on to deploy."*

### Step 2 — five questions, asked ONCE, in one message

**The problem was never that we ask too little; it is that every question arrived too late.** Before
this section existed, all four ask-moments were mid-flight (published datasource, credential stop,
re-parse confirmation, retry cap) — and if the user has stepped away, the run dies there (measured
2026-08-07). Meanwhile fidelity bar and autonomy were inferred silently, and from the outside an hour
of confident work on a wrong assumption looks exactly like an hour of correct work.

Step 1 answers *scope* by investigation, so only these are genuinely questions:

| # | Question | Why it cannot be inferred |
|---|---|---|
| 1 | **Confirm the plan from step 1** — this ordering, these workbooks, this destination? | Assessment says what is *used*; only the human knows what is *wanted*. |
| 2 | **Autonomy** — see below. Default `standard`. | The failure modes are symmetric: too autonomous and a run spends 105 minutes saying nothing; too interactive and an overnight run stops on question 1. |
| 3 | **Fidelity bar** — faithful re-creation, or modernise where Power BI is better? | It decides real translations (a Tableau dual-axis trick → a native combo chart; a `MAKELINE` route map → endpoint bubbles). Both builders need it. |
| 4 | **If we hit a wall — stop, or degrade?** | Pre-authorising the fallback is what lets an unattended run *survive* one instead of dying at 3 am. |
| 5 | **Who drives the data refreshes?** — see below. Default `scripted`. | Only you know how large the source is and whether you will be at the keyboard. |

**Autonomy levels, defined by behaviour at a decision point — not by vibe:**

| level | reversible choice | costly or irreversible | credential wall |
|---|---|---|---|
| `guided` | ask | ask | ask |
| **`standard`** (default) | decide, log it | **ask** | ask |
| `autopilot` | decide, log it | decide, flag in the summary | **ask — always** |

**Autonomy governs choices; it cannot govern physics.** No level clears the credential stop: that is
a modal sign-in dialog no automation can fill. Question 4 pre-authorises the *fallback* (build
model-only under `credential_gate.py authorize`, artifacts marked unvalidated) — never pretending a
source was reachable.

**Refresh strategies:**

| strategy | who drives it | ceiling | progress evidence |
|---|---|---|---|
| **`scripted`** (default) | `refresh_pbip_model.py` | **3600 s** on a default full refresh (configurable, and retained even when trace setup fails); the legacy **300 s** / 330 s applies only to `--no-progress` and `--calculate-only`/`--measures-only` | ⚠️ conditional — row counts only once `ProgressReportCurrent` emits them, else an elapsed heartbeat; a non-fatal silence warning at 120 s |
| `operator` | the agent prepares everything, stops, and asks **you** to hit Refresh in Desktop | none | ✅ per-table row counts, live in the UI |
| `xmla` | manual XMLA/TOM against the live instance | none | ⚠️ partial |

⚠️ **Re-read `refresh_pbip_model.py`'s constants before quoting a number from this table** — it was
stale for two weeks and stated the opposite. Three ways to lose a refresh remain: the legacy 300 s
path still kills the measured 700–750 s Snowflake case; 3600 s is a fatal absolute backstop; and
row-count evidence is not guaranteed even with the trace up (small tables can emit only Begin/End
events). The **AMO/TOM assembly** changes *observability*, not the timeout — without it you keep the
ceiling but lose row counts, liveness warnings and the `ImageSave` persist path. ⚠️ A refresh is also
where the shared "time-box an unresponsive system" rule and its "don't kill a tool that IS the timer"
carve-out point in opposite directions: resolving it wrongly either records **no verdict at all** or
waits 129 minutes, both measured. A **detected** credential modal aborts with a specific error; an
unclassifiable timeout says so in its own output.

⚠️ `xmla` must be **whole-database** scope if a calculated table depends on a refreshed table (e.g. a
`Date` table built with `CALENDAR(MINX(...), MAXX(...))` over the fact). Refresh the fact alone and
the calculated object does not recompute in the same transaction.

### Step 3 — write the brief, then dispatch

Write the answers to `migrations/workbooks/<name>/migration-brief.md`. **The file is the point**, for
two reasons no persona can solve alone: it survives a **dropped session** (a closed terminal takes
this session's entire working memory with it — measured 2026-08-08), and it is what a **stateless**
subagent receives instead of re-deriving intent nobody wrote down. Then invoke `@tableau-migrator`
per unit of work, handing it the brief.

**Record the Step-1 capture in the brief — grade included.** For each unit write **where the
reference landed**, **which tool produced it**, and **what grade of evidence it is** — the
load-bearing part. A `reference/` capture carries a `capabilities` manifest and is a
`validation_grade` source. An **oracle** capture is **not**: its images land outside `reference/`,
carry no `capabilities` manifest, and are taken in the view's **default state only** (no `?vf_`
filter pinning), so they are **layout- and text-grade only**. Say so, and tell the consumer to log
that ceiling in `limitations_encountered` — a visual PASS signed off on oracle imagery alone is
overstated (#194). Do not quietly drop this, and do not inflate it.

**Add `--reference-best` to any oracle run that covers dashboards.** `?resolution=high` is measured
to be exactly 2× the dashboard's declared size with no parameter that raises it, so a label-dense
page can be structurally legible and content-illegible at once. `--reference-best` **probes** the
ladder (`svg` → `pdf` → `png_high`), takes the best rung the site actually answers on, and records
the tier, per-rung verdicts and version numbers in the manifest's `render_capability`. ⚠️ Which rung
a customer gets depends on their Tableau version and **Cloud is not representative**; never infer
capability from a version string. The API→release map and the failure modes:
[`docs/reference-capture.md`](docs/reference-capture.md) (#403, #194). None of this upgrades the
grade — it raises the ceiling *within* text-grade.

> **Running the pipeline by hand, or standing behind someone who is?**
> [`docs/operator-runbook.md`](docs/operator-runbook.md) is the command-by-command version of this
> section: the day-before checklist, expected timings, the failure playbook (the `estate_survey.py`
> site-wide silent sweep, the `exit 3` DoD gate, the storage-decision/union skip), the verification
> checklist and — importantly — what that checklist does **not** prove.

### Migration cost attribution

Every migrated workbook or datasource **must have its own** `_runs/<NNN>-<slug>/run.json` before
agentic work starts. Record attribution at dispatch/allocation time, not at completion: a crash after
spend but before stamping the root makes that spend permanently unattributable.

A dedicated Copilot **session** per migration unit is the reliable anchor; record its `session_id` in
`run.json` and do **not** mix unrelated questions or other units into that session. If unrelated work
happens anyway, flag the run as polluted — it stays visible but must not be silently averaged into
customer budget estimates. A dispatched `@tableau-migrator` root `agent_id` may be recorded as an
extra label under `attribution.roots[]`, but it captures only that agent's own calls, never its
descendants: no single store maps `parent_tool_call_id` back to the issuing agent
(`assistant_usage_events` is local-only, `tool_requests` cloud-only), so a subtree walk is not
reconstructible and the session is the only bucket holding a dispatched agent and all its children
(#364).

A session with no `run.json` is development work for cost reporting and is excluded; retroactive
attribution is impossible. Report **both** model time and elapsed time — they differ by a large
factor because tool execution runs outside model calls, so quoting only one misleads.

### Gate B — after parse + probe, before building

Some decisions genuinely cannot be front-loaded: you do not know a workbook points at a published
datasource until you parse it, or that a warehouse refuses Power BI until you probe it. Fine — but
`tableau-migrator` must present them as **ONE block, not four serial stops**. Serial stops are the
same questions with strictly more waiting, and each is another chance to catch the user absent.

1. Published datasources → fetch the `.tds` and migrate it first, or proceed knowingly incomplete.
2. Live sources that failed the probe → credential, or the authorised build-only path.
3. Extract-only sources → materialize real rows, or model-only.
4. The high-severity `limitations_encountered` digest → proceed, or narrow scope.

Where step 2 question 4 already answered one, **apply it and say so** — do not re-ask. The brief
exists so each question is answered once per migration, not once per session.

---

## Canonical work layout (pre-bundle stages, scratch, deliverables)

The stages **before** `run_estate.py` had no shared convention and per-run scratch had no home or
lifecycle — 31 ad-hoc `_*` roots accumulated with nothing recording what any was for (#291, #234).
The convention is **`_runs/<NNN>-<slug>/`**, never `_work/` (`.gitignore`'s existing `**/_work/` rule
means the OPPOSITE thing). The **number is the identity** — never renamed or reused, because bundle
output embeds absolute self-paths; the slug is decoration, because a display name is never unique.

```
_runs/<NNN>-<slug>/
    run.json          <- the one authoritative description of this run
    assessment/        assess_estate.py-shaped output
    assets/             harvest_estate_assets.py-shaped downloads
    bundle/              run_estate.py-shaped conversion output
    oracle/               capture_tableau_oracle.py-shaped reference capture
    packages/              package_unit.py's per-unit handover packages — ⚠️ `--out` names a
                            subdirectory INSIDE this one (`packages/<batch>/<Unit>/`), never
                            `packages/` itself: `conflicting_evidence_dirs` refuses an `--out` whose
                            parent holds `oracle/`, and a run root always does (measured: bare exits
                            2, one level deeper exits 0). See `docs/migration-phases.md`
    deliverables/          operator-facing outputs meant for the CUSTOMER, never for git — the
                            `ses-prep/` near-miss (#322): a `connections.json` naming 17 real
                            customer servers landed unprefixed at the repo root, one `git add -A`
                            away from being committed
    scratch/                disposable, run-owned — the only subdir a future `--prune` may delete
```

`/_*` in `.gitignore` covers the whole tree by construction — verified with
`git check-ignore -v -- _runs/<NNN>-<slug>/deliverables/connections.json`, **without** a trailing
slash (a trailing slash makes `git check-ignore` report every path as ignored, which proves nothing).

**`scripts/work_dirs.py` is the single source of truth for these paths** — `sanitize_unit_key`,
`allocate_run` (atomic `mkdir`-exclusive, retry on collision, never a read-then-write race),
`RunPaths` and `list_runs`. It resolves the repo root from its **own file location**, never from
`Path.cwd()`: a stray empty `fabric/` was once written at the repo root by a script that resolved a
relative path against whatever CWD an agent invoked it from.

**Scope landed so far:** the convention plus the helper only. `assess_estate.py`,
`harvest_estate_assets.py`, `capture_tableau_oracle.py` and `run_estate.py` keep their documented
`_assessment*/` / `_sweep*/` / `_oracle*/` / `_bundle*/` defaults **unchanged** (#234). `_estate/`,
`_build/` and `migrations/` are explicitly **exempt** — persistent test infra, a bundle-internal
replay convention, and committed deliverables.

---

<!-- BEGIN:shared-conventions -->
## Shared agent conventions (all agents inherit these)

- **Cite your source — and say WHOSE.** Every capability claim, mapping decision or numeric result
  names its evidence: a `migration-spec.json` field, a TMDL/PBIR path + line, a live `EVALUATE`
  result, or a doc URL. "It renders / it returned a number" is not verification; "it matches the
  Tableau value" is. **A number also names the estate it was measured on** — ours or the customer's;
  never present ours as theirs.
- **Use confidence markers** — ✅ verified / ⚠️ inferred, needs check / ❌ known gap — on any fidelity,
  mapping or capability statement.
- **Own your layer; don't cross it.** `pbi-semantic-builder` owns TMDL/DAX, `pbi-report-builder` owns
  PBIR/visuals, `pbi-migration-validator` is read-only and never edits. A subagent never "just fixes"
  a finding another agent owns — it reports; the orchestrator routes.
- **Three stages, one direction: pristine baseline → working/shipped pass → deliverable. Never edit
  upstream of where you are.**
  | stage | location | rule |
  |---|---|---|
  | pristine baseline | `<bundle>/reports/` (the model-unbound report pass); `<bundle>/semantic_models/` when emitted | **NEVER edit.** Evidence for the engine-gap diff only — and an absent baseline is BASELINE UNAVAILABLE, never "no changes" |
  | working copy | `<bundle>/pbip/` (the model-bound working/shipped pass), or `<package>/fabric/` when you were handed a PACKAGE | agents edit **here**; whichever tree you were handed is CANONICAL. `declare_generated_edit.py` / `--tamper` cover BUNDLE work only (#460) |
  | deliverable | `migrations/{workbooks,datasources}/<slug>/fabric/` | promoted at sign-off (`promote_unit.py`), so the bundle survives as evidence |

  A bundle may contain `<bundle>/{pbip,reports,semantic_models,handover,data}` — **no `out/` level**;
  `<bundle>/semantic_models/` is conditional (absent for 8 of 12 workbooks in one audited estate).

  ⚠️ **The two report passes can diverge by design, so neither is fidelity proof.**
  `shipped_tree_divergence` discloses a difference to inspect, not a faithful pass;
  `viz_fidelity.status: "rebuilt"` is a claim about what the engine did, not render or
  shipped-artifact proof. **Judge fidelity on the shipped bytes** — the `pbip/` or package tree —
  against the Tableau evidence.

  ⚠️ Promotion must keep `definition.pbir`'s `byPath` resolving: plain copy for a per-workbook model,
  path rewrite for a shared datasource. Never ship `<bundle>/reports/` (reference-only: no model
  beside it). Mechanics: `powerbi-report-gotchas` §3.

- **Structural validation is necessary, not sufficient.** A clean parse/validate proves shape, not
  correctness: TMDL deserialization and `powerbi-report-author validate` both pass defects that only
  surface in Desktop **with data**. Never declare something done on a green validator alone. PBIR and
  TMDL specifics: the `powerbi-report-gotchas` / `powerbi-semantic-model-gotchas` skills.
- **Keep `limitations_encountered` alive** through the whole build **and** fix phase. Regenerate it
  from the final artifacts before sign-off so stale entries don't mislead the validator.
- **Declare generated edits.** TMDL/PBIR/`.pbip`: file/change/why + replay script + hash record.
- **Surface complexity mismatches proactively.** If the parsed workbook implies more effort than the
  user assumes (many LOD/table-calc fields, extract-only data with no upstream, >20 floating-layout
  worksheets), say so before building rather than mid-migration.
- **NEVER block silently on an external system — time-box it, then ASK.** Measured: an agent sat on
  live-Snowflake connectivity for **129 minutes / 298 tool calls** without ever surfacing the
  problem. Waiting is not progress.
  - **Cap it: ~2 minutes or 3 attempts, whichever comes first** — any unresponsive external system
    (database/warehouse/gateway, MCP server, XMLA refresh, the Desktop bridge). Cap *relaunches* at 2
    as well; "kill it and retry" is otherwise an unbounded loop.
  - **Unless the tool tells you it IS the timer** — some scripts self-bound and announce their own
    deadline. Measured: an agent applied the cap to such a script, killed it at 120 s and recorded
    **no verdict at all** — worse than waiting. Read the tool's own output first.
  - **A MISSING CREDENTIAL is not transient — try ONCE.** The cap is for *flaky* systems. A refusal
    naming authentication, permissions or a sign-in prompt is a **final answer**; only a plainly
    transient timeout (a serverless warehouse cold-starting) earns one retry.
  - **AUTOPILOT / auto-approve DOES NOT override a credential stop.** "Decide, don't ask" applies to
    *choices*; this is a physical dependency on a human — the credential sits behind a **modal
    sign-in dialog no automation can fill**. Stop and ask **even in an unattended run**, and end the
    turn.
  - On hitting the cap, **STOP and ask a specific, actionable question** — name the system, what you
    tried, and the concrete options. Never re-run the same call hoping for a different result. Ask in
    your normal reply — there is no `ask_user` tool.
  - **Report elapsed time** whenever an operation exceeds ~60 s, so a stall is visible rather than
    looking like work.
- **End every message with a clear next step or an explicit verdict** — never a vague "looks fine."
- **Durable learnings go in committed files** (agent `Gotchas`, the skills,
  `docs/tableau-dax-translation-guide.md`), never in a git-ignored scratch folder — that is how each
  real migration permanently improves the toolkit.
- **Power BI Desktop cleanup is PID-scoped.** Concurrent instances are fine; never sweep by name.
  Use the literal PID you opened (`Stop-Process -Id <pid> -Force`; `$pid` is a read-only shell
  variable), and never close a sibling's instance or one mid validator↔builder handoff. Run-owned
  leaks are enforced by `check_unit.py`'s `desktop-orphans` gate. Remove scratch/temp files you
  created; keep only committed deliverables plus re-runnable `_build/` scripts, and confirm nothing
  scratch leaked into git before reporting done. ⚠️ **Never `git add -A` after a gapped pull** —
  measured: a merge staged **111** untracked scratch paths, because `-A` cannot tell files the merge
  introduces from files merely lying around. Stage from
  `git diff --name-status <old-HEAD> origin/master`. If you must undo one, `reset --soft HEAD~1`
  **clears `MERGE_HEAD` even on a merge commit** — recreate it, or the next commit is silently
  single-parent and the ancestry breaks.
<!-- END:shared-conventions -->
