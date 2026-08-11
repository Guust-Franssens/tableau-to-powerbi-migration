# Findings log

**This file is the deliverable.** The PBIP is a by-product.

Classification, decided **at the moment of observation** (retrofitting is how a UX problem gets
excused as a known bug):

| tag | meaning |
|---|---|
| `new-UX` | the thing we are hunting: unintuitive, unclear, surprising, or slow |
| `known-engine` | listed in `known-defects.md`. Record it, do **not** re-file upstream |
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

## F002 — No documented path from "a Tableau site" to "a folder of workbooks"

- **When:** 2026-08-11, before the run
- **Tag:** `docs-gap`
- **Phase:** 0 (planning)
- **Observed:** `AGENTS.md`'s input-triage table maps *a site* → `assess_estate.py` + `tableau_lineage.py --plan`, and *a folder of `.twb`/`.twbx`* → `run_estate.py --input <folder>`. But `run_estate.py` only accepts a **local folder**, and nothing in the table says how to get from the first row to the second. The bridge (`harvest_estate_assets.py`, which wraps the engine's `fetch_tds.py`) exists but is never named in the triage.
- **Root cause, upstream:** the engine's `LiveTableauSource` is an explicit stub — *"Documented SEAM for a live Tableau Server / Cloud connection — network calls NOT built yet."* Only `LocalFilesSource` and `InMemoryTableauSource` run end-to-end. So the one-button site→PBIP flow genuinely does not exist yet; the gap is real, not a doc oversight alone.
- **Impact:** an operator following `AGENTS.md` for a site reaches an assessment and then has no documented next step. We only found this by reading `run_estate.py --help` during planning.
- **Suggested fix:** add the download step to the triage table, and state plainly that live-site ingestion is not implemented upstream.
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
| 1 — assess 38 | | | | |
| 2 — migrate 4–5 | | | | |
| 3 — debrief | | | | |

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
