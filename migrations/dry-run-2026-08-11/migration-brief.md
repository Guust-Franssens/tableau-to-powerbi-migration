# Dry run — Tableau Cloud site → local PBIP

**Date:** 2026-08-11 · **Status:** planned, not started

## What this is

A **UX study**, not a migration. The deliverable is `findings.md`. If we end with a beautiful PBIP
and no observations, the run failed.

The question: *what is intuitive, what is not, and where does a competent operator get stuck?*

## Design: the observer must not be the driver

The run is executed by a **fresh agent in a fresh clone**, with only the repo's own documentation.
Not by the session that built the toolkit — that session knows every workaround, which version to
pin, and when to ignore a validator. It would measure nothing.

This also tests, for the first time, `AGENTS.md`'s claim that the repo is *self-configuring*.

**Intervention rule:** the observing session intervenes only on (a) a direct question from the agent,
or (b) a time-box breach. **Every intervention is logged as a finding** — an intervention is a UX
failure by definition.

## Target: our own Tableau Cloud site

`https://10ax.online.tableau.com` — **38 workbooks, 340 views**.

Better than customer data for this purpose: it is a deliberately adversarial estate
(`Meridian Calc Gauntlet`, `Hostile Identifiers`, `Collision Alpha`/`Beta`, `Multi-Source (3 systems)`,
`RLS Demo`) plus `Superstore` and `World Indicators`. Built to break a migration tool, and free of
PII — so findings can quote real figures and column names, as we already do upstream.

Prior assessment on disk at `_assessment/` (2026-08-07), but it was run **without** `--survey`, so
migration order is recorded as **unknown**. Phase 1 fixes that.

## Phases

| # | Scope | Rationale |
|---|---|---|
| **1** | `estate_survey.py --json` → `assess_estate.py --survey` over all 38 | The estate path has **never** run on real data. Cheap, no Desktop, no data-source credentials. |
| **1b** | **Harvest**: download all 38 workbooks + published datasources to a local folder | The seam between "site" and "folder" — see F002. Untested, and not in `AGENTS.md`'s triage table. |
| **2a** | **Deterministic tier over ALL 38** (`run_estate.py --input <folder>`) | No LLM, no Desktop per workbook — fast enough to do the whole estate. **This is the highest-value phase.** |
| **2b** | Agentic flow on **4–5** workbooks | The agentic layer is what is slow; a sample is the only affordable option. |
| **3** | Debrief → issues | Findings classified and filed. |

**Why 2a matters more than it looks.** The deterministic tier is the foundation — if it fails at
estate scale, everything above it fails. Running all 38 turns anecdote into **statistics**: a
corpus-wide `definition_of_done` / `limitations_encountered` picture rather than a 5-workbook sample.

It also produces **evidence for the upstream merge argument** (`Yarbrdab000/tableau-fabric-skills#113`).
If we can say *"38 workbooks on `main` 2.113.0: N failed the DoD, M of those are already fixed on
`integrate-all-lanes`"*, that is a far stronger case for landing the branch than a list of issue
numbers. Run `main` first for exactly this reason; optionally re-run against the branch afterwards
for a clean before/after.

**Phase 1 gates phase 2.** A workbook whose published datasource has not landed rebuilds to an
*empty report*, so the survey's dependency ordering must be trusted before scoping.

Candidate workbooks for phase **2b** (final pick is the human's, informed by 1 and 2a):

| workbook | complexity | why |
|---|---|---|
| Superstore | 64 | the hardest thing on the site |
| Meridian Calc Gauntlet | 11 | LOD / table-calc stress |
| Meridian Hostile Identifiers | 3 | name-collision stress — exercises `check_datamodel`'s `NAME_COLLISION` |
| Meridian Multi-Source (3 systems) | 1 | federated connections; low complexity score, high structural risk |
| one trivial (e.g. `datasource_test`) | 3 | control — how fast is the easy case? |

## Credentials

**The PAT never enters the conversation.** The tooling reads `.env` via `load_env()`, so the secret
is only ever a file on disk.

Deliberate sequence, which tests the docs *and* keeps the credential clean:

1. Fresh clone starts with **no `.env`**.
2. The agent must work out what it needs and ask. *(This is where finding #1 bites — there is no
   `.env.example`; the four required variable names exist only inside `assess_estate.py`.)*
3. The observing session places `.env` with **only the `TABLEAU_*` keys** — not Snowflake/Databricks,
   which are not needed until phase 2.

**No short-lived PAT.** Verified 2026-08-11: the Tableau REST API exposes *list* and *revoke* for
PATs but **not create** (`POST …/personal-access-tokens` → **HTTP 405**). It would not help anyway —
a PAT inherits its owner's full permissions and cannot be scoped down, and this owner is
`SiteAdministratorCreator`. A short lifetime limits the *window*, not the *blast radius*.

Teardown, if wanted: `DELETE …/personal-access-tokens/{guid}`.

## Data-source credentials — a natural experiment

Per the site owner: **some Snowflake/Databricks sources are already authenticated in this machine's
Power BI Desktop credential store, and some are not.** So a single run exercises both paths:

- **Happy path** — live probe succeeds, refresh works, full validation available.
- **Sad path** — the credential wall is hit. Expected, and acceptable.

**Pre-authorised fallback:** on a sad-path source the agent builds **model-only** under
`credential_gate.py authorize`, marks the artifacts **unvalidated**, and continues. It must not die
there, and it must not silently pretend the source was reachable.

⚠️ **Realism caveat to carry into the findings.** Desktop's credential store is per-user and
per-machine. A refresh that succeeds here would **not** succeed on a colleague's laptop or in CI. So
"it worked" for the happy-path sources is a machine-specific result, not a general capability claim.
Record it as such, or it will mislead us later.

**Observation to make explicitly:** does the agent *distinguish* the two paths in its own reporting?
If happy-path and sad-path artifacts are both described as "validated", that is a finding — and a
serious one, because it is the false-success class we keep filing upstream.

## Instrumentation

Every entry in `findings.md` is timestamped and classified:

- `new-UX` — the thing we are actually hunting
- `known-engine` — a defect already registered in `known-defects.md`; **not** a UX finding
- `docs-gap` — the answer exists but was not findable

Also recorded: elapsed time per phase, every question the agent asks, every hesitation, every wrong
turn, every intervention.

## Explicit non-goals

- **Not** publishing to Fabric. Local PBIP only; `pbi-deployer` does not exist (issue #57).
- **Not** running the **agentic** flow over all 38 — too slow. The **deterministic** tier does run
  over all 38 (phase 2a); that is the distinction.
- **Not** producing a customer-ready artifact.
- **Not** fixing engine defects found along the way — register them and move on.
