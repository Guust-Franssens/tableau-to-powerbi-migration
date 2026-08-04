# Review remediation plan

**Status:** Phase A in progress · **Branch:** `feat/deterministic-tier-integration` · **Created:** 2026-08-04

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

## Phase A — stop shipping broken guidance  🔴 IN PROGRESS

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
| A1 | On timeout, report `SLOW_REFRESH` and stop claiming a credential modal. Parameterise the ceiling | `.github/skills/pbip-model-refresh/scripts/refresh_pbip_model.py:89,109` | a slow full refresh no longer emits credential language; a genuine modal is still reported by the probe path | ☐ |
| A2 | Make `--no-save` the DEFAULT; `--save` opt-in, documented with the 3-vs-3 evidence | same file + its `SKILL.md:23-37` | after a default refresh the PBIP opens (window title is the model name, not "Untitled") | ☐ |
| A3 | Sync all three copies | `scripts/`, `.github/skills/`, `dist/marketplace/` | `preflight.ps1` reports no STALE bundle | ☐ |

⚠️ `refresh_pbip_model.py` exists in **three** locations. A fix that misses one leaves the published
plugin stale, which preflight blocks on.

---

## Phase B — repair the credential gate  ☐ NOT STARTED

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
  ⚠️ Lead worth a spike first: `.twbx` files carry `TwbxExternalCache/TwbxLQResultsCacheV3/*.bin`,
  i.e. Tableau's own cached query results. If readable, every workbook ships its own ground truth.
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
