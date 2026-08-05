# Review remediation plan

**Status:** Phases A + B ✅ COMPLETE · next: Phase C · **Branch:** `feat/deterministic-tier-integration` · **Created:** 2026-08-04

Tracks the work arising from four review rounds (plan · knowledge-architecture · code · direction).
Both direction reviewers returned **CHANGE SHAPE — do not merge, do not continue plan v3 as written**,
and both put fixing our own safety boundary ahead of any further integration work.

**Restate this plan and its status at the end of every phase.**

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

## Phase C — deal with the committed branch  ☐ NOT STARTED

Both code reviewers: not safe to merge. Per artefact:

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
