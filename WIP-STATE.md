# WIP-STATE — branch `chore/tableau-dashboard-reference-probe` (#405 / refs #403)

**Written 2026-08-31 as a crash-handover. Nothing is in progress; the tree is clean and everything
below is pushed.** This file exists because the next session gets only what is on disk.

## State: DONE and verified

Review round 6 is complete. Last SHA `ef18a12`. CI green on both jobs (`checks`, `windows-bundle`) —
verified by `gh run watch --exit-status` (exit 0), not by reading the web page.

| gate | how it was judged |
|---|---|
| full suite | `pytest -q -n auto --dist loadfile` → 3397 passed / 3 failed = the documented `test_upstream_repro_pins.py` engine pins **only** |
| ruff | `ruff format --check` + `ruff check` over `conftest.py scripts tests .github/skills` → exit 0 |
| pylint | `scripts`, `conftest.py` → 10.00/10, exit 0 |
| doc/capability gates | `check_agent_capabilities.py`, `check_navigation_index.py`, `sync_agent_conventions.py --check`, `set_data_folder.py --check`, `validate_spec.py --all --check` → all exit 0 |
| live | `capture_tableau_oracle.py --out <scratch> --reference-best --limit 3` against `fabric-migration-lab` → exit 0, `best tier = svg`, 3/3, `credential_scrubbed_at_sink: []` |

Rounds 2–6 of the credential-leak review are closed. The three mechanisms, which cover **disjoint**
artifacts and are not defence-in-depth duplicates:

1. `capture_tableau_oracle.artifact_stem()` — filenames are built from a **verified-UUID LUID only**;
   `safe_slug()` is deleted. Protects the on-disk `.csv`/`.svg`.
2. `TableauSession.export()` — refuses a **successful** body echoing the PAT secret or session token
   (`credential_reflected`). The only mechanism that can protect a file.
3. `tableau_env.scrub_tree()` at `write_manifest()` — scrubs manifest **values and dict keys**,
   disambiguates redaction-induced key collisions, and reports hits in
   `credential_scrubbed_at_sink` using the **scrubbed** key.

## IN PROGRESS

**Nothing.** No half-finished edit, no uncommitted file, no open refactor.

## NOT started

- Nothing outstanding from round 6's brief. Awaiting the reviewer's round-7 pass.

## Mutation results — 25/25 matched expectation (harness exit 0)

22 CAUGHT + 3 discriminating controls (cosmetic reword → SURVIVED, absent anchor → INVALID, syntax
error → INVALID). **No mutation SURVIVED that was expected to be caught.** All seven historical
escapes were re-verified as mutations in the same run and every one is still CAUGHT.

⚠️ Two earlier rounds each had genuine SURVIVORS that are worth not re-deriving:
- round 5: *"the seam only checks the PAT secret"* survived — every battery site planted its value as
  the secret, so the session-token arm was untested. Site added.
- round 5: **four** sink mutations survived because nothing drove a value through the sink at all —
  a backstop shipped with no test. The reachable path is the **PAT name** (deliberately redacted,
  never refused), and four tests now drive it.

The harness itself is scratch and was deleted; its mutation table is reproduced in the commit messages
of `92dc6d2` and `ef18a12`. Rebuild it under a gitignored `_*` directory if needed.

## Discovered, contradicting the brief — highest-value items

- **Round 5's brief described the CSV leak as reaching the manifest. It also reached DISK** —
  `_capture_data` writes the payload verbatim to `data/<view>.csv` before any manifest exists. That is
  what falsified "redact at the manifest boundary" as a sufficient fix and forced the seam refusal.
- **My own round-5 claim that the PAT name was "scrubbed from the manifest and knowingly left in the
  `.csv`" was false** until round 6 — it was in `format_hints` **keys**. The residual is only now
  bounded where it is documented, pinned by
  `test_the_pat_name_is_KNOWN_to_survive_in_the_csv_on_disk`.
- **A third artifact was unnamed for six rounds: the CONSOLE.** `log_progress` sliced a view name to
  34 characters before redacting it, and `_log_blocked_and_stale` printed raw names from the
  *unscrubbed* records (`scrub_tree` returns a copy). CI retains logs, so "only the terminal" is not a
  mitigation. Both now route through `redacted_note`.
- **The sink's own error message was a confident wrong diagnostic** ("the AUTHENTICATING halves cannot
  reach here at all"), falsified by round 6 finding 1: a session token reaches the manifest via a view
  name from `get_json`, which the export seam never sees. Corrected.
- **`RedactedText` was re-decided against on new evidence, not the old reason.** Rounds 3–4 are
  *mis-orderings* (a type would help; so does the chokepoint, provably). Rounds 5–6 are *omissions* —
  the value never entered a redaction-aware path — and a type cannot fix an omission, because you must
  choose to construct one.

## Known residuals — do not re-discover, argue with them

- Taint analysis in `tests/test_diagnostic_redaction.py` is **intra-module** inter-procedural. A
  cross-module chain is covered only by the declared `TAINT_SEEDS` table.
- The **exit** set is closed by Python; whether the AST recogniser catches every *syntactic* form of
  reaching one is an approximation. `artifact_stem` mitigates the filename case structurally.
- `tableau_env.redact()` still misses percent-encoded, base64, NFD and case-changed forms. Measured
  and defended: each needs a **third party** to re-encode our credential, and case-insensitivity was
  rejected on measurement (a PAT named `DataSource` falsely redacts inside
  `FederatedDataSourceException`, 10 chars per hit in a 476-char body).
- The PAT **name** still survives in `data/<luid>.csv` on disk — deliberate, since refusing would kill
  a legitimate estate over a column heading.

## Power BI Desktop

**None opened this session.** No PID to clean up. The only external system touched was the Tableau
Cloud site `fabric-migration-lab`, via the capture CLI, sequentially, with no concurrent requests.

## Scratch

All scratch directories used this session (`_r5/`, `_r6/`, `_review405*/`) were deleted. `git status
--porcelain` is empty. `_runs/` was never written to.
