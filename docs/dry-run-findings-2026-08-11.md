# Findings log

**This file is the deliverable.** The PBIP is a by-product.

Classification, decided **at the moment of observation** (retrofitting is how a UX problem gets
excused as a known bug):

| tag | meaning |
|---|---|
| `new-UX` | the thing we are hunting: unintuitive, unclear, surprising, or slow |
| `known-engine` | listed in the contemporaneous dry-run known-defects file. Record it, do **not** re-file upstream |
| `docs-gap` | the answer exists in the repo but was not findable |
| `regression` | something previously verified working, now broken. Highest priority |

**Interventions are findings.** Every time the observing session has to step in, that is a UX failure
by definition — log it with what the agent was stuck on and what unstuck it.

---

## F001 — No `.env.example`; required variables are discoverable only from source

- **When:** 2026-08-11, before the run (banked in advance)
- **Tag:** `docs-gap`
- **Phase:** 0 (setup)
- **Observed:** A fresh clone gives no indication of what credentials the estate path needs.
  `TABLEAU_SERVER_URL`, `TABLEAU_SITE`, `TABLEAU_PAT_NAME` and `TABLEAU_PAT_SECRET` appear only
  inside `scripts/assess_estate.py` (~line 84). `.gitignore` covers `.env`, so nothing is committed
  to copy from.
- **Impact:** A newcomer must read Python source to learn how to authenticate. `AGENTS.md` documents
  the *estate* entry point but not its prerequisites.
- **Suggested fix:** commit a `.env.example` with the variable names, empty values, and a comment
  pointing at Tableau's *My Account Settings → Personal Access Tokens*.
- **Status:** open

---

## F002 — The site→folder step exists but is absent from the entry-point triage

- **When:** 2026-08-11, before the run
- **Tag:** `docs-gap`
- **Phase:** 0 (planning)
- **⚠️ Correction:** an earlier version of this entry claimed no path existed. **That was wrong** —
  `scripts/harvest_estate_assets.py` downloads every workbook and published datasource to
  `<out>/assets/` as `.twbx`/`.tdsx`, which is exactly what `run_estate.py --input` consumes. The
  defect is documentation, not capability.
- **Observed:** `AGENTS.md`'s input-triage table maps *a site* → `assess_estate.py` +
  `tableau_lineage.py --plan`, and *a folder of `.twb`/`.twbx`* → `run_estate.py --input <folder>`.
  The step that gets you from the first row to the second is never named. An operator following the
  table reaches an assessment and stops.
- **Worth knowing:** `harvest_estate_assets.py` does more than download — it runs **both** parsers
  (ours for fidelity, the engine's for conversion) and writes `parse-sweep.md` / `parse-sweep.json`,
  a failure distribution across the estate. That is precisely the phase-2a evidence this run wants,
  and it is buried in a script nobody is pointed at.
- **Separately true:** the engine's `LiveTableauSource` is an explicit stub — *"Documented SEAM …
  network calls NOT built yet"* — so there is no **one-button** live-site→PBIP flow. The two-step
  harvest-then-run path is the supported route and should be documented as such.
- **Suggested fix:** add the harvest step to the triage table; state plainly that live-site ingestion
  is a two-step flow, and why.
- **Status:** open

---

## F004 — `harvest_estate_assets.py` documents a `--workbooks-only` flag it does not implement

- **When:** 2026-08-11, before the run
- **Tag:** `new-UX`
- **Phase:** 0 (setup)
- **Observed:** the module docstring's usage line advertises it:

  ```
  usage:   python scripts/harvest_estate_assets.py --out <dir> [--env .env] [--limit N]
                                                   [--skip-download] [--workbooks-only]
  ```

  It appears **only** there — never in `argparse`. Running the documented line verbatim:

  ```
  $ python scripts/harvest_estate_assets.py --out C:\tfmig\_probe_out --workbooks-only
  harvest_estate_assets.py: error: unrecognized arguments: --workbooks-only
  EXIT=2
  ```

- **Impact:** copying the script's own documented invocation fails. Compounded by F003 — on Windows
  `--help` crashes before printing, so the real flag list is not easily discoverable either.
  Together they make this script hostile on first contact, and it is the script that bridges F002.
- **Suggested fix:** implement the flag or delete it from the docstring. A test asserting that every
  flag in a script's usage line actually parses would catch this class repo-wide, alongside the
  `--help` smoke test proposed in F003.
- **Status:** open

---

## F003 — `--help` crashes on Windows for 6 of 47 scripts (UnicodeEncodeError)

- **When:** 2026-08-11, before the run
- **Tag:** `new-UX`
- **Phase:** 0 (setup)
- **Observed:** Without `PYTHONUTF8=1`, `python scripts/<name>.py --help` dies with
  `UnicodeEncodeError: 'charmap' codec can't encode characters` — Windows defaults to cp1252 and the
  docstrings contain non-ASCII characters. Measured across `scripts/*.py`: **41 work, 6 fail.**

  ```
  build_reconcile_items.py       harvest_estate_assets.py     setup_snowflake_keypair.py
  check_migration_progress.py    provision_snowflake_fixture.py   transpile_tableau_calc.py
  ```

- **Impact:** `--help` is the first thing anyone runs on an unfamiliar script, and it returns a
  traceback rather than help. Two of the six are load-bearing: `harvest_estate_assets.py` is the
  site→folder bridge from F002, and `check_migration_progress.py` is the supervision tool the
  orchestrator persona instructs agents to run.
- **Impact beyond `--help`:** any `print()` of the same characters fails identically, so this is a
  latent crash in normal output, not only in help text.
- **Suggested fix:** either force UTF-8 at entry (`sys.stdout.reconfigure(encoding="utf-8")`) in the
  affected scripts, or restrict docstrings to ASCII. A repo-wide `--help` smoke test would keep it
  fixed — 47 scripts, one loop, and it just caught six real breakages.
- **Status:** open

---

## Template

```
## F0NN — <one-line summary>

- **When:** <timestamp>
- **Tag:** new-UX | known-engine | docs-gap | regression
- **Phase:** 1 assess | 2 migrate | 3 debrief
- **Observed:** <what happened, verbatim where possible>
- **Impact:** <what it cost: time, a wrong turn, a false belief>
- **Intervention:** <none | what the observer had to say>
- **Suggested fix:** <if clear>
- **Status:** open | filed as #NN | fixed
```

---

## Phase timings

| Phase | Started | Ended | Elapsed | Notes |
|---|---|---|---|---|
| 1 — survey + assess 38 | | | | |
| 1b — harvest 38 | | | | |
| 2a — deterministic, all 38 | | | | |
| 2b — agentic, 4–5 | | | | |
| 3 — debrief | | | | |

## Baseline to measure against — AVA Promocode, 2026-07-31

The only pre-deterministic-tier measurement we have, from a **real customer migration** of **one
workbook** (1 table, 2 pages, 21 visuals) on the agentic-only toolkit:

| Sub-agent | Elapsed | Tool calls | Turns |
|---|---:|---:|---:|
| `pbi-semantic-builder` | **3h 39m 28s** | ~347 | 2 |
| `pbi-report-builder` | **2h 36m 56s** | ~400 | 3 |
| `pbi-migration-validator` | 38m 25s | ~106 | 1 |
| **Total** | **6h 54m 49s** | **~853** | **6** |

**90.7% of that time was the two builders** — and both of those jobs are now done by the
deterministic tier. So the honest question for this run is **not** "is it faster" (it must be), but
**where did the time move to?**

Expected new bottlenecks: credential probes and Desktop waits · residual DAX / table-calc work ·
AI enrichment · visual repair · cache persistence · validator rounds.

**Regression signals** — record time separately for each, because a bigger total is not automatically
bad (triage and validation are deliberately stricter now):

- semantic time spent **recreating** objects the engine already emitted
- report time spent **rebuilding** rather than repairing
- external retries beyond the documented cap
- validator loops beyond 2–3 rounds
- hours on the **trivial control workbook** with an empty residual queue
- inability to attribute agentic time at all

Also record **human-wait time separately from active time**, and **time spent diagnosing
`known-engine` defects separately from genuine agentic work** — otherwise a known bug inflates the
UX measurement.

## Questions the agent asked

Recorded verbatim. A good question at the right moment is a **success**, not a finding — the
dispatcher intake exists precisely so questions are asked up front rather than mid-flight.

| # | When | Question | Was it answerable from the repo? |
|---|---|---|---|
| | | | |

## Interventions

| # | When | Agent was stuck on | What unstuck it | Could docs have prevented it? |
|---|---|---|---|---|
| | | | | |
