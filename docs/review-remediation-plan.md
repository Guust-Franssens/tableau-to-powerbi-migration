# Review remediation plan

**Status:** Phases A · B · B′ ✅ COMPLETE · Phase 0 (connectivity) ✅ PROVEN LIVE · E1 ✅ DONE ·
next: **D1 spike** → C → D → E · **Branch:** `feat/deterministic-tier-integration` ·
**Created:** 2026-08-04 · **Last updated:** 2026-08-05

Tracks the work arising from four review rounds (plan · knowledge-architecture · code · direction).
Both direction reviewers returned **CHANGE SHAPE — do not merge, do not continue plan v3 as written**,
and both put fixing our own safety boundary ahead of any further integration work.

**Restate this plan and its status at the end of every phase.**

---

## Where we are today (read this first)

### What we are building

A **two-tier** Tableau → Power BI migration. A colleague's *deterministic* engine
(`Yarbrdab000/tableau-fabric-skills`, currently 2.59.0) converts a workbook into a PBIP — TMDL
semantic model + PBIR report — with **no LLM in the loop**, getting roughly 60-70% of the way. Our
Copilot **agents** are tier 2: **critic · enricher · fixer** on an artifact that already exists. We
do not author the model from scratch any more.

That split is why our layer's job is *acceptance*, not re-implementation. The boundary we hold:
**we only check things observable at the data layer** — "will it load, and will it load the *right*
data" — never TMDL syntax his own `tmdl_lint` already covers. Anything else is a parallel validator
and pure waste.

### Branch state

| | |
|---|---|
| branch | `feat/deterministic-tier-integration` (19 commits ahead of `master`) |
| size | 34 files, **+4,079 / −263** |
| tests | **336 passing** (was 276 at branch start) |
| lint | ruff clean, pylint **10.00/10** |
| upstream issues filed | **#91**, **#92** (both with credential-free reproducers) |

### The one thing that had to be true first

**Nothing downstream is observable without loaded data.** Screenshots, `EVALUATE` gates, AI-readiness
checks and fidelity comparison all require a model that actually refreshed. So proving the
connectivity loop — in **both** directions, against **real** systems — became Phase 0 and consumed
most of the effort. It is now done:

| connector | happy path | sad path |
|---|---|---|
| Databricks | `DATA_OK` **21 s** | revoked PAT → 403 → `CREDENTIAL_REQUIRED` |
| Snowflake | `DATA_OK` **167 s** (cold warehouse) | never-authed account → modal → `CREDENTIAL_REQUIRED` |
| Azure SQL | `DATA_OK` | never-authed server → modal → `CREDENTIAL_REQUIRED` |
| all three at once | `DATA_OK` **220 s** cold / **19 s** warm | one bad leg → `CREDENTIAL_REQUIRED` |

⚠️ **Keep three confidence layers separate — do not conflate them:**

| layer | coverage | meaning |
|---|---|---|
| M emission logic | **10/10** | we emit correct M for ten connectors |
| input-shape fidelity vs a **real** export | **4/10** | the other six are inferred — and inference already failed once (the FCP bug) |
| **live** connectivity | **3/10** | actually reached a running system |

### What that work turned up

Four **false greens in our own probe**, each of which passed every static validator (ours *and* his)
and was caught only by a real Desktop open: a receipt claiming a credential it never tested;
orphaned annotations that crash TMDL merge; gutted calculated tables; dangling column references.
Plus `M_PARAM_EMPTY` — we checked a parameter *name* existed but never its *value*.

And two defects in the deterministic tier, both now filed with reproducers that need no credentials:

- **#91 — federated multi-connection datasource collapses to one parameter set.** The *heterogeneous*
  case fails loudly; the **same-class case (two SQL servers) fails silently and returns the wrong
  data**, which is the common shape and strictly worse. **Our probe missed the silent one too** —
  now fixed (`SOURCE_COLLAPSED`, `2f6f026`).
- **#92 — FCP-prefixed connection attributes not read**, so Databricks `HttpPath` is empty and the
  table degrades to an empty placeholder.

---

## Why this exists — the three findings that reordered everything

1. **Two defects are shipping to other repos right now** via the published `pbip-model-refresh`
   bundle: a false "credential modal" stop, and `ImageSave` guidance that makes a PBIP unopenable.
2. **The credential gate — our stated #1 do-not-break control — fails open** on live sources, and
   220 MB of materialized customer data escapes `verify()` entirely.
3. **"Silence = correctness" was never validated.** It was measured with a DuckDB oracle reading the
   *same CSV Tier 0 materialized*, written by the same agent that wrote the DAX. That is
   self-consistency. There is no Tableau-side numeric ground truth anywhere in the repo — so plan
   v3's Phase 0.5 STOP condition **cannot fire**. A gate that cannot fail is not a gate.

---

## Phase A — stop shipping broken guidance  ✅ COMPLETE (`0cbff41`)

Nothing outranks this: it is wrong guidance in a bundle other repos consume today.

**Design change (user, 2026-08-04) — separate the two refresh purposes rather than patch the
heuristic.** The false credential stop exists because one script serves two profiles with opposite
timing expectations:

| purpose | rows | expected | a timeout means |
|---|---|---|---|
| probe refresh | 1 | seconds | genuinely blocked → **stop and ask a human** |
| real refresh | full (246,236 measured) | 38-87 s and up | slow → report `SLOW_REFRESH`, claim nothing about credentials |

A one-row refresh that exceeds its budget really is blocked, so the credential verdict moves to the
probe. `refresh_pbip_model.py` then stops asserting a cause it cannot observe.

| # | task | site | acceptance | status |
|---|---|---|---|---|
| A1 | On timeout, report `REFRESH: TIMEOUT` + `CAUSE UNKNOWN`, name the arbiter, add `--timeout-sec` | `refresh_pbip_model.py:429-449` | ✅ regression test `test_a_timeout_does_not_assert_a_credential_modal` asserts the stop-word is gone and the arbiter is named | ✅ |
| A2 | `--save` opt-in; `--no-save` kept as a no-op; SKILL.md hand-off rule corrected | same file + `SKILL.md` | ✅ regression test `test_not_saving_is_the_default` asserts no `cache.abf` is written | ✅ |
| A3 | Sync all three copies | `scripts/`, `.github/skills/`, `dist/marketplace/` | ✅ dist rebuilt (31,215 B, matches bundle). ⚠️ Required fixing `build_plugin.py`, which used `shutil.rmtree(onexc=)` (py3.12+) on a py3.11 target and died before writing | ✅ |

⚠️ `refresh_pbip_model.py` exists in **three** locations. A fix that misses one leaves the published
plugin stale, which preflight blocks on.

---

## Phase B — repair the credential gate  ✅ COMPLETE (`96b5482`, `087aba5`, `c24ba73`)

All four fixes landed with regression tests. Suite 276 -> 301 passing.

- **B1+B2** dissolved into one deletion: the preflight held a second, contradictory policy (deny-list, fails open) against `connection_target`'s allow-list (fails safe). Removing it fixed the extract-over-live precedence AND the missing `azure_sqldb` at once — 51 lines deleted, not extended.
- **B3** split `denied_dirs()` (enforcement, `fabric/` only) from `audited_paths()` (verification, whole migration + materialized data). A new test also caught `verify` CREATING `fabric/` as a side effect.
- **B4** parser emits `connection.connections[]`; preflight gates every leg. Upstream half filed as their issue #90.

| # | task | site | status |
|---|---|---|---|
| B1 | Test `LIVE_DB_CLASSES` **before** `mode == "extract"`; delete the duplicate policy and consume `connection_target.powerbi_target()` | `preflight_source_credentials.py:92,96` | ☐ |
| B2 | Add `azure_sqldb` (+ unwrap `federated`) — currently **0 hits repo-wide** | `preflight_source_credentials.py:40` | ☐ |
| B3 | Split `denied_dirs()` from `audited_paths()`; audit materialized data by CONTENT, not suffix | `credential_gate.py:500,516` | ☐ |
| B4 | Emit `data_sources[].connections[]` plural + a `limitations_encountered` entry when >1 | `parse_tableau.py:164` | ☐ |

**Acceptance:** an 8-case table test asserting `classify_source` ≡ `connection_target.powerbi_target`
(6 currently contradict) · a migration whose only artifact is a 110 MB `.csv` makes `verify()` exit
non-zero · the tri-source workbook parses **3** connections, not 1.

---

## Phase B′ — bound the refresh itself  ✅ COMPLETE (live-testing finding, not a review finding)

Found while proving the multi-source probe against three live systems. A direct
`refresh_pbip_model.py --pid` call against a **never-authenticated** Azure SQL server
(`sql-demo-server-sociale-fraude`, the deliberate sad leg of the `2happy1sad` arm) sat blocked on a
Desktop sign-in modal for **956 s** while `REFRESH_TIMEOUT_SECONDS = 300` never fired.

**Two corrections to how this was first written up, both from ground truth:**

1. **It was never a product hang on the probe path.** `probe_live_source.py` already wraps the
   script in `subprocess.run(..., timeout=timeout_sec)` and converts `TimeoutExpired` →
   `NO_CREDENTIAL`. The 956 s was an *ad-hoc harness* bypassing that wrapper. The original framing
   ("OPEN BUG: the cap must wrap the whole operation") over-claimed the blast radius.
2. **The residual defect was real but different**: `refresh()`'s own docstring said *"the caller must
   run its own clock."* That is a rule, not a bound — and the caller who forgot it was this repo's
   own agent. Every **direct** caller (which the `pbip-model-refresh` skill documents as the normal
   way to refresh a model) inherited nothing.

**Fix — scope, never duration.** `REFRESH_TIMEOUT_SECONDS` stays **300 s**: cold starts are real (a
1-row probe against a suspended Snowflake warehouse measured **167 s**, vs 21 s warm), and this repo
has already produced a false `TIMEOUT` once by shortening a ceiling to 90 s. The ADOMD call now runs
on a **daemon** thread joined at `timeout_sec + REFRESH_WALL_CLOCK_GRACE_SECONDS` (330 s total), so
XMLA still gets first crack at raising its far better error, and a parked mashup engine can no longer
keep the *process* alive. We cannot cancel work the server itself cannot preempt — we can always
return a verdict.

| # | task | site | acceptance | status |
|---|---|---|---|---|
| B′1 | `refresh()` bounds itself on a daemon thread; keep the 300 s ceiling | `refresh_pbip_model.py:118-` | ✅ `test_a_command_that_never_returns_still_yields_a_verdict` (a fake that never returns ends in ~3 s, message names the modal diagnosis) | ✅ |
| B′2 | A worker failure must reach the caller unchanged — `main` classifies on its text | same | ✅ `test_an_error_from_the_worker_reaches_the_caller_unchanged`; **caught a real bug**: `conn.Open()` sat outside the `try`, so a connect failure escaped the thread and surfaced as the generic "worker returned no result" | ✅ |
| B′3 | Sync all three copies | `scripts/`, `.github/skills/`, plugin | ✅ `sync_installed_skills.py` → 2 files copied | ✅ |

**What this does NOT fix, and why that is fine.** A multi-source model still gives an all-or-nothing
verdict: the modal blocks the whole refresh before any table loads, so `2happy1sad` records
model-level `CREDENTIAL_REQUIRED` with **no per-table rows** — the PARTIAL downgrade never fires
(it needs a refresh that *completes* with empty tables). That is not a gap, because **the product
does not probe that way**: `probe_live_source.py` iterates live sources and builds a **one-table
model per source**, so the per-source matrix comes from there, and the orchestrator's step 5b hard
-stops before `pbi-semantic-builder` is ever called. The fused tri-source bundle exists only to
stress the multi-source path.

---

## What happens next — ordered, with the dependency that sets the order

The order is not preference, it is **what actually advances the integration**.

### ⚠️ First — an honest answer to "how does Phase C get us closer to the integration?"

**It mostly does not.** Phase C is *merge hygiene* — debt from the review rounds — not integration
progress. Worse, **most of it just became obsolete** (measured 2026-08-05):

- Upstream **#89 is CLOSED**, fixed in 2.59.0 (109 lines in `migrate_estate.py`, 62 in
  `twb_to_pbir.py`, 163 lines of tests). The reviewer kept `detect_occlusion.py` explicitly
  *"while #89 is open"* — that condition has expired.
- **Verified empirically, not from the changelog:** ran his 2.60.0 on `interactive-resume` (the
  example with the most image content) and our detector reports `OCCLUSION: none detected`, exit 0.

So C2a/C2b would be **fixing a script that no longer has a job**. The open question is not "fix it"
but "does it have a *second* life as a check on **our own** report-builder output?" — a decision, and
not on the integration path either way.

### The integration is further along than this plan implies

> 📄 **The agent redesign lives in [`deterministic-tier-integration.md`](deterministic-tier-integration.md) §0 (v4)** —
> per-persona cut lists (105,482 → ~46 k), the validator's two new jobs, the report-builder
> constraints, and the phase-telemetry design. This document tracks *review remediation*; that one
> tracks *the integration*. Read both.

**The two-tier loop already ran end-to-end today, twice** — it just was not labelled as the milestone
it is:

1. **his engine** built a semantic model from a Tableau workbook
2. **our parser** produced `migration-spec.json` from the same workbook
3. **our probe** compared the two and found a real defect (`SOURCE_COLLAPSED`) that **both** his
   `tmdl_lint` and our own earlier checks passed
4. we reported it, he shipped 2.60.0, and **our check independently confirmed the fix** (exit 1 → 0)

That is the **critic** half of critic-enricher-fixer, working against a live upstream on real output.
Missing are the **enricher** and **fixer** halves — and the persona wiring that would let an agent do
this instead of a human driving it by hand.

### The real gap, and therefore the real next step

| capability | status |
|---|---|
| connectivity proven, both directions, 3 real systems | ✅ done |
| **critic** — find defects the structural validators miss | ✅ **proven today** (`SOURCE_COLLAPSED`, and the FCP finding behind #92) |
| **handoff contract** — how a tier-1 bundle formally enters our pipeline | ❌ undefined; done by hand each time |
| **personas rewired** as critic-enricher-fixer | ❌ `tableau-migrator` step 6 still delegates to `pbi-semantic-builder` to **BUILD** a model that, in the two-tier world, already exists |
| **enricher** — AI-readiness, descriptions, model critique on **his** output | ❌ not wired to his artifacts |
| **fixer** — apply a fix to his bundle without re-authoring it | ❌ undefined, and the layer-ownership rule makes this the delicate one |

| # | work | effort | advances integration? | note |
|---|---|---|---|---|
| **1** | **Handoff contract + persona rewiring** — define how a tier-1 bundle enters our pipeline, and rewrite `tableau-migrator` steps 5-8 so the builders **critique and enrich** an existing PBIP instead of authoring one | days | ✅ **this IS the integration** | The critic half already works; this makes it repeatable by an agent rather than by hand |
| **2** | **C1 decision** — accept the in-place `probe_bundle.py` fix, or honour the revert | a decision | ⬜ unblocks the merge only | Needs the repo owner |
| **3** | **Close the input-shape gap** (6 connectors × one real `.tds`) | ~1 h each, no credentials | ⬜ indirectly | 4/10 → 10/10; inference already failed here once |
| **4** | **F1 — source-side numeric verification** | days | ✅ yes — it is the fidelity claim | ~90% of calculated fields; needs a live-source migration |
| **5** | **Phase C code** (C2a/C2b/C3) | ~1 h | ❌ **no** | Mostly obsolete; C3 (import safety) is the only piece with lasting value |
| **6** | **Hook write-up** | ~1 h | ❌ no | Knowledge capture: `github/copilot-cli#2392`, undocumented subagent scope |

**Deprioritised: D1** (the `.twbx` results cache). Real and readable, but present in **1 of 16**
workbooks; F1 reaches ~90% of calculated fields instead. Kept as a tracked idea, not a next step.

### C1 — the decision only the repo owner can make

Both code reviewers said this branch is not safe to merge. One item needs **a human decision, not an
agent's**: reviewers said *revert* `probe_bundle.py` and re-derive it smaller; we instead **fixed it
in place** (`d8dec54`) and it has since grown a second check (`2f6f026`) and 26 tests. Whether that
counts as satisfying the review is a call for the repo owner.

### What is NOT next, and why

- **The remaining probe arms (`1happy2sad`, `3sad`).** They will re-prove the same modal block with
  less information. The sad path is already proven on three connectors and on the multi-source case.
- **Mid-migration tooling upgrades.** Never; swapping the validator under a half-built report is
  worse than a slightly old one.
- **A general TMDL linter on our side.** That is his layer. See the boundary in "What we are building".

---

## Estate orchestration — DECIDED 2026-08-06 (two independent models, same answer)

> 📄 Recorded here because it arose from the review work, but it is **integration** scope — the agent
> redesign it belongs to is [`deterministic-tier-integration.md`](deterministic-tier-integration.md) §0 (v4).

**Decision: neither (a) nor plain (b). A thin `scripts/run_estate.py` coordinator + the top-level
session as control plane.** Reached independently by `claude-opus-5` and `gpt-5.6-sol`, then
re-derived after a false constraint was removed — both said **the recommendation is unchanged, only
its sizing**, which is the strongest form of agreement available.

> **"The main agent drives the estate; a script remembers and enforces it."**
> *"A conversation should not be the only scheduler for 29 workbooks and ~400 KB of raw report state."*

### The false constraint that had to be removed first

Both first-round answers said **"Desktop concurrency = 1, acquire a lease"** — because `AGENTS.md`
told them so. It was wrong: the Bridge takes `--pid` natively, every port lookup is PID-scoped, and
**8 tests pin multi-instance binding**. Fixed at source in `48af6b3`. Practical N ≈ 4, bounded by
RAM, and **per model-closure** (the pool is sized by peak model size, not workbook count).

### Why we are downstream of the engine, never a peer of it

The `--approved-dax` re-run is **delete-and-recreate**, verified directly:

| site | what it does |
|---|---|
| `migrate_estate.py:880` | `shutil.rmtree(dest)` before `write_model_folder` — *"clear stale parts so a rerun never leaves renamed/dropped tables"* |
| `:3005` | `shutil.rmtree(dest)` before `write_local_pbip` — the whole `.pbip` project dir |
| `:3253` | `shutil.rmtree(dest)` for `<name>.Report` |

**Nothing we wrote into that bundle survives a landing re-run**, and this is wholly independent of
Desktop. Sharper still (`:5003-5005`): the stale-output guard **exempts** the landing re-run —
`landing_rerun = bool(args.approved_dax or second_compile)`, and the guard only fires
`if not args.force and not landing_rerun`. So the re-run that destroys the most is the one that needs
**no `--force`**. It is deliberate and documented on his side; the consequence for a *consumer* is
that the barrier is unguarded by design.

⚠️ **Two of the five citations originally offered for this were wrong** — `:4997-5005` is the guard
(and argues the opposite of how it was used) and `:3971-3981` is the estate loop threading the flat
`approved_calc_dax`, which is collision evidence, not deletion evidence. Caught by the second model
and verified here. The conclusion stands on the three `rmtree` sites alone.

### Concurrency unit: the **model-closure**, not the workbook

One semantic model **plus every report bound to it**. Several workbooks routinely share a model, so:

| phase | unit | concurrency |
|---|---|---|
| engine runs (1..n) | whole estate | **serial** — each one `rmtree`s |
| **BARRIER** — last engine run | — | freeze, then copy to writable trees |
| model enrichment | per closure | parallel across closures, **once per model** (not per workbook) |
| report finish | per report | parallel within a closure, once its model is frozen |
| validation | per report | parallel; a late model fix invalidates **that closure only** |

### The real binding constraint is **human credential attention**, not Desktop and not the barrier

Ranked: (1) **credential prompts** — the only true zero, since a modal sign-in cannot be automated and
a human answers one at a time, so K first-time credentials = K touchpoints queued on *a person*;
(2) shared-model freeze/invalidation; (3) RAM/CPU; (4) ~~screenshot capture~~ — **tested and cleared,
see below**; (5) top-level context volume, solved by slicing; (6) source-side refresh throttling.

**Actionable now:** batch-probe every workbook with `--check-only`, collect all `NO_CREDENTIAL` into
**one** message, and ask the human **once**. That was previously hidden behind "Desktop forces serial
anyway."

⚠️ Ranking the barrier first conflates *ordering* with *throughput* — it is crossed once and costs
seconds. It constrains sequence, not capacity.

#### Screenshot concurrency — MEASURED 2026-08-06, no serial phase needed ✅

The one open risk that could have reintroduced a serial phase (`PrintWindow` against a *background*
window capturing blank — the validator has recorded blank KPI cards under `PrintWindow` before). Two
Desktop instances opened concurrently, one deliberately raised to the foreground via
`SetForegroundWindow`, then `screenshot-all --pid` run against the **background** one:

| | result |
|---|---|
| bridge visibility | **both** instances `bridgeStatus: connected` simultaneously |
| background capture | `"failures": []`, 3034x1624 PNG |
| pixel check | **2,181 distinct colours, 74.2% dominant** — comparable to the foreground pages (2,770-4,933) |
| visual check | fully rendered: title, body text, colours, section headers. No blank cards |

**Per-instance footprint, measured on the same run:** Desktop 733-769 MB + its `msmdsrv` 532-584 MB
= **~1.28 GB per loaded instance**. So the repo owner's practical N ≈ 4 costs ~5 GB, which is the
number to size `desktop_slots` against — RAM, not the bridge.

⚠️ One incidental finding worth knowing: `powerbi-desktop open` returned **pid 55812 for both opens**
— the *first* instance's pid, not the newly launched one. So **do not trust `open`'s pid when an
instance is already running**; resolve the real pid from `status` (which correctly listed both) or by
window title. A fan-out that binds to `open`'s return value would silently point every worker at the
same instance.

### What `run_estate.py` owns (the only estate code worth funding now)

1. Invoke his CLI, and **convert `definition_of_done.status` into a real exit code** — `:5072`
   `# Soft-but-loud: exit stays 0` with an unconditional `return 0`, so `failed` exits 0 today. *An
   agent may read the JSON field; a script must.* This is the disqualifying evidence for letting a
   conversation be the only scheduler.
2. Stamp `VERSION` + `input_manifest` sha256s for provenance.
3. **Collision-check `approved_dax.json` before flattening** — the map is estate-global and
   name-keyed; observed names include `Calculation2` (Tableau's auto-generated default), `Rank`,
   `Size`. Key decisions internally by `(model identity, calc name, formula hash)`.
4. Slice `report.json` (83.4 KB / 6 workbooks ≈ 14 KB each) into per-workbook handovers so the raw
   estate report never enters a subagent's context.
5. Hold the DAG and survive context loss.

**No worker ever writes `approved_dax.json`.** DAX authoring and validation run in parallel into
isolated decision files; exactly one central merge crosses the barrier.

---

## Open decisions that need a human

1. **Phase C / `probe_bundle.py`** — accept the in-place fix, or honour the reviewers' revert?
2. **If D1 finds no values** — fund a Tableau licence for one live workbook, or formally re-scope the
   validator to structural fidelity only? (Carrying an unfundable numeric tier is the dishonest
   option, and it is what we are doing today.)
3. **Hook-based gating at the handoff** — worth adding a `preToolUse` deny on the `task` tool so an
   armed credential gate stops the *delegation* rather than the first write? Verified feasible
   (parent hooks do fire in subagents since CLI 1.0.49, `github/copilot-cli#2392`). It is an
   optimisation — the kernel ACL is already the enforcement — so it is a cost/benefit call.

---


## Phase C — deal with the committed branch  ☐ NOT STARTED

Both code reviewers: not safe to merge. Per artefact:

**Measured status 2026-08-05** — two of the four are already resolved, so the real remaining work is
smaller than this table implies:

| item | reviewer's ask | actual status now |
|---|---|---|
| C1 `probe_bundle.py` | revert, re-derive smaller | ⚠️ **needs a human decision** — we fixed it in place (`d8dec54`) and it has since grown `SOURCE_COLLAPSED` (`2f6f026`) and 26 tests |
| C2a `--fix` exit code | return 1 while occluders remain | ❌ **open, and confirmed a real bug**: `main()` returns `0` unconditionally after `--fix` (L173-180), printing `RE-CHECK: N occluder(s) remain` and then ignoring N. ~1 line |
| C2b geometry | replace `contains` with ≥90% area overlap | ❌ **open** — still `contains()` at L60, used at L94. 8 victims missed on the very report we cite. ~10 lines |
| C3 `transpile_tableau_calc.py` | import safety | ❌ **open** — module-level driver code at the file tail, no `if __name__ == "__main__":` guard |
| C4 CI | make green | ✅ **already passes** — `test_every_script_is_documented_in_the_scripts_readme` is green |

So Phase C is roughly **one decision (C1) plus ~a dozen lines (C2a, C2b, C3)**, not the open-ended
rebuild the original table suggests.

| artefact | action | rationale |
|---|---|---|
| `probe_bundle.py` | **revert**, re-derive smaller | The flaw is design, not lines: it writes `"credential is bound in Power BI"` having executed nothing. The 1-row M wrap itself is sound (33/33 partitions, byte-identical unwrap); the corruption comes from `strip_dax_objects` (duplicate annotations in 8/8 files) and the missing execution step. Rebuild as: external, non-mutating, `--keep-dax` behaviour, and **only claims what it executed** |
| `detect_occlusion.py` | **fix + keep** | Useful while #89 is open. Gate findings on `data_victims` (kills the quad false-red), make `--fix` return 1 when occluders remain, replace `contains` with ≥90% area overlap (8 victims currently missed on the very report we cite) |
| `transpile_tableau_calc.py` | **keep as evidence, offer upstream** | It is compiler coverage, not polish. Two import-safety fixes so it does not break collection: driver under `if __name__ == "__main__":`, plugin import inside a function |
| CI | **make green** | `test_every_script_is_documented_in_the_scripts_readme` currently FAILS — 3 scripts missing from `scripts/README.md` |

---

## Phase D — the honest re-scope  ☐ NOT STARTED

- **D1 — price the numeric ground truth.** Both reviewers' #1 untracked risk. Either fund a Tableau
  licence + one live workbook, or re-scope the validator to *structural fidelity only* and say so.
  Carrying an unfundable numeric tier as the differentiator is the dishonest option.

  > 🔻 **DEPRIORITISED 2026-08-05 — superseded in priority by F1 below, but kept as a live idea.**
  > The cache lead is real (readable, no Tableau needed) but present in **1 of 16** workbooks, so at
  > best it is an oracle for one migration. **F1 reaches ~90% of calculated fields.** Revisit the
  > cache only if F1 is blocked, or opportunistically when a workbook that has one comes up.

  > ⚠️ **Do not confuse the two caches — they are unrelated files with opposite roles.**
  >
  > | file | lives in | what it is | relevance |
  > |---|---|---|---|
  > | `.pbi/cache.abf` | a **Power BI** PBIP | the persisted model image `pbip-model-refresh --save` writes | **not D1** — the one that can make a PBIP unopenable |
  > | `TwbxExternalCache/TwbxResultsCacheV3/` | a **Tableau** `.twbx` | Tableau's own saved *query results* | **this is D1** — a candidate numeric oracle |
  >
  > D1 is not about anything breaking. It asks whether Tableau left real **numbers** in the workbook
  > that we can compare our Power BI output against.

- **F1 — verify against the DATA SOURCE, not against Tableau.** ☐ TRACKED, NOT STARTED.
  *Proposed by the repo owner 2026-08-05; it is the better-scoped idea and displaces D1 in priority.*

  **The idea:** for a **live** source we do not need Tableau to tell us the right number — we can ask
  the upstream system. Query Databricks/Snowflake/SQL directly, compute the expected value, and
  compare it with what the Power BI model returns.

  **Why it beats the cache lead** (measured 2026-08-05 across the 16 examples):

  | | count | share |
  |---|---|---|
  | calculated fields | **1,009** | |
  | LOD expressions | 46 | 5% |
  | table calculations | 59 | 6% |
  | **everything else** | **904** | **90%** |

  So ~90% of calculated fields are ordinary expressions a source-side check could adjudicate, versus
  a results cache that exists in 1 of 16 workbooks. An order of magnitude more reach.

  ⚠️ **The trap this must not fall into — we have already fallen into it once.** The retracted
  "silence = correctness" result was measured with a DuckDB oracle reading *the same CSV*, written by
  *the same agent* that wrote the DAX. That is self-consistency, not validation. A source-side check
  inherits the same hazard the moment the validating SQL is written from the same reading of the
  Tableau formula that produced the DAX. **Two implementations by one author agreeing proves only
  that the author is consistent.**

  So scope the claim precisely — these are different strengths:

  | claim | can F1 prove it? |
  |---|---|
  | the model reads the **right server / table / rows** | ✅ yes, strongly — and it would have caught the cross-wired-server bug in upstream #91 at the data layer |
  | a **simple aggregate** matches the source | ✅ yes, if the SQL is derived independently |
  | an **LOD / table calc** was translated correctly | ❌ no — "what should this number be?" *is* the question, and answering it in SQL means reimplementing Tableau's semantics, which is the thing under test |

  **Prerequisite that limits it today:** all 16 examples are **extracts** (`textscan`), so there is no
  live source to query for any of them. F1 needs a live-source migration — which the connectivity
  work has now proven we can stand up (Databricks / Snowflake / Azure SQL, all `DATA_OK`).

  **Net effect on the honest re-scope:** we can credibly claim **structural fidelity + data-pipeline
  fidelity**. We still cannot claim **semantic fidelity of the hard 10%** without a Tableau-side
  oracle. That is a much better position than today's, and it is honest.
  ⚠️ Lead worth a spike first: `.twbx` files carry `TwbxExternalCache/TwbxResultsCacheV3/*.bin`,
  i.e. Tableau's own cached query results. If readable, every workbook ships its own ground truth.

  **Spike run 2026-08-05 — the lead is real but thin. Three measured corrections:**
  1. **The path in this doc was wrong.** It is `TwbxResultsCacheV3`, not `TwbxLQResultsCacheV3`
     (plus a sibling `TwbxTimestampsCacheV3`). Searching for the old name returns **0 of 16**, which
     would have retired a live lead as a dead end.
  2. **Coverage is 1 of 16, not "every workbook."** Only `urban-adaptation.twbx` ships a cache
     (6 result entries + 6 timestamp entries, 92 KB). So this can **never be *the* oracle** — but it
     is a free one for at least one workbook, against a baseline of "no numeric ground truth exists."
  3. ✅ **It is readable without Tableau.** `.key` is plain XML naming the query context
     (`<pack class='hyper' dbname='…federated 6.hyper'>`); `.bin` is UTF-16LE XML behind a short
     binary header, opening with `<metadata-record class='column'><remote-name>Calculation_…`.
     ⚠️ **Unconfirmed:** whether the `.bin` carries result *values* or only column metadata — only
     the first 220 bytes were inspected. That question decides the whole spike and is ~1 hour of work.
- **D2 — update issue #89** with `twb_to_pbir.py:8418` and `:8881` (*"images z=1100 ... are never
  moved"*). The constant is deliberate and documented, so the report should be framed as a layering
  scheme that fails for full-canvas backgrounds, not as an oversight.
- **D3 — plan v4:** shape becomes *upstream-first optional provider*; **vendor at a commit SHA**
  rather than "version-pinned" (the plugin path is unversioned and `report.json` carries no version
  at all); drop the 5th-bundle and conventions-slimming proposals.

---

## Phase E — settle the persona question by measurement  ☐ NOT STARTED

Run airline **both ways** — the four personas vs a generated run-brief — and compare
**oracle-verified correct measures per agent-visible instruction byte**. The dual-built corpus
(`examples/airline-alliance-activity/fabric/`, 108 measures ours vs 88 + 188 his) and a working seam
already exist. This converts a two-round disagreement (-47,450 chars vs +600) into one number.

⚠️ Depends on D1: without an oracle the numerator is unmeasurable.

**Budget measured 2026-08-05 — the denominator is already at the ceiling:**

| persona | chars | % of 30,000 cap |
|---|---|---|
| `pbi-semantic-builder` | 29,979 | **99%** |
| `pbi-report-builder` | 29,932 | **99%** |
| `tableau-migrator` | 29,731 | **99%** |
| `pbi-migration-validator` | 17,656 | 58% |

The shared block is **5,551 chars, duplicated ×4 = 22,204**. It is not evenly distributed — one
bullet is **44% of it**:

| bullet | chars | % of block |
|---|---|---|
| NEVER block silently on an external system | **2,460** | **44%** |
| Clean up after yourself | 963 | 17% |
| Structural validation is necessary, not sufficient | 564 | 10% |
| (7 others) | 1,564 | 28% |

**E1 — a slimming candidate that does not need D1's oracle.** The 2,460-char bullet is the one whose
subject matter has been progressively moved *into code* — `probe_live_source.py` self-bounds and
prints its own DO-NOT-KILL directive, and `refresh()` now self-bounds (Phase B′). The repo's own
retrospective rule is *"delete what a newer tool now catches automatically."*

The precedent is already in `probe_live_source.py`'s comments: an agent applied the persona's
"~2 minute cap" **to the bounded script itself**, killed it at 120 s and recorded no verdict — and
the fix was to put the directive in **tool output**, not in persona prose ("Saying so in the output
is what actually reaches the agent"). So prose and enforcement have already collided once, and tool
output won.

⚠️ **What must NOT be cut**, because no script can enforce it: the rule generalises past our own
scripts (MCP servers, XMLA, the Desktop bridge), and "AUTOPILOT does not override a credential stop"
governs the agent's *own* reasoning. Cut the situation-specific halves, keep the general rule.
Estimated recovery ~1,200 chars ×4 ≈ 5 KB, i.e. 99% → ~95% of cap on three personas.

---

## Conclusions retracted under review — do not re-adopt without new evidence

| retracted | replaced by |
|---|---|
| "his pipeline never lands data" | extract-only artifact; 13/16 land under 2.40.0 |
| "2.40.0 fixes data but not the report layer" | it was z-order occlusion |
| "one keystone unblocks ~60 calcs" | unblocked exactly 1; the real effect is a breadth-cascade (~2x) |
| "he ships no reconciliation oracle" | he ships `fidelity_oracle.py` (227 KB) |
| "we own the Tableau-side ground truth" | screenshots only; no numeric ground truth exists |
| "silence = correctness ✅ holds" | self-consistency, not validation |
| "all four personas survive with narrower jobs" | a compromise between two contradictory reviews; the effective unit is a run-scoped generated brief |
| "he builds, we finish" | he should own deterministic finishing too; ours is acceptance, safety, source ground truth, and the irreducible tail |
