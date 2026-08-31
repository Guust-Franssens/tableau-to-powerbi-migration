# WIP-STATE — issue #406 (PR #412) — **DELETE THIS FILE BEFORE MERGE**

Branch `fix/ps-arbiter-benign-prose`. Written 2026-08-31 under an imminent-disconnect warning.
**Status: work is COMPLETE and pushed.** This file exists only because the next session may get
nothing but the repo. Full narrative lives in PR #412's body and its two comments.

## DONE and verified (gate + exit code)

| # | Change | Proved by |
|---|---|---|
| 1 | `$MinPromptWords = 5` **deleted** from `probe_desktop_credential.ps1`; replaced by the shared enumerated `benign_chrome_signature.regex` | `Password:` / `Please enter your password` went `benign`→`$null` (exit 0) under `-RefreshInFlight` **→** `mixed-content` / `DIALOG_UNRECOGNIZED` (exit 3) |
| 2 | Harvest **reason** surfaced (`truncated` / `patterns-incomplete` / `no-payload` / `bad-schema` / `not-attempted`) + item count in `Format-DialogEvidence`. **Diagnostic only** | `test_the_harvest_reason_is_diagnostic_and_cannot_change_a_verdict`, 8 reasons × flag pinned both ways |
| 3 | Three shipped serial tests moved onto `_assert_convicts_once_the_window_is_readable` (precondition → exact assertion → skip only when unreachable) | serial suite **8 passed / 0 skipped**, exit 0 |
| 4 | Pre-existing `-TimeoutSec 1` race in `test_a_wedged_uia_provider_still_produces_a_verdict` — skips when no dialog was observed; deadline deliberately NOT raised | serial suite green |

Gates, all exit 0: `ruff format --check` + `ruff check` on `conftest.py scripts tests .github/skills`;
`pylint` **10.00/10** on all four CI roots; offline bundle suite **319 passed / 3 skipped**; serial
**8 passed**. Full repo suite earlier: 3144 passed (3 known `test_upstream_repro_pins` + 1 unrelated
`test_check_unit` parallel mtime race that passes in isolation twice).

## IN PROGRESS / NOT STARTED

Nothing. No half-applied edit. Working tree was clean at `98cd917`.

## Mutation results — including SURVIVORS

Amnesty work (all caught, named): M1 restore amnesty · M2 allowlist→`.*` (also broke the **Python**
test, proving the shared file) · M3 remove allowlist · M4 neuter veto · M5 inline a private copy.

Harvest-reason work: N1 let the reason authorise suppression (10 named) · N2 collapse to one token ·
N3 always report `complete` · N4 drop the item count.

⚠️ **N5 (`not-attempted` vs `no-payload` conflation) SURVIVED the entire offline suite first time.**
`ConvertTo-ProbeWindow` sits BELOW the `-LoadDetectorsOnly` seam, so no synthesised-window test can
reach it. Closed by the live `test_a_partial_harvest_is_never_reported_as_no_modal_appeared`. **If you
add anything below that seam, an offline test cannot cover it — write a `serial` one.**

## Findings that CONTRADICT the briefs (highest value; re-deriving these is expensive)

1. **"Skip whenever the harvest is INCOMPLETE" is wrong for the element-cap test.** A `truncated`
   harvest at the shipped cap of 2000 (451-element window) *is* the regression, so that rule skips on
   the defect's own signature. Measured by defaulting `$HarvestMaxElements` to 400:
   band-only → never detects · skip-on-INCOMPLETE → never detects · reason-aware+retry → **3/3**.
2. **A skip inside a retry loop ABORTS the test.** `_run_probe_against_wpf_modal`'s own `pytest.skip`
   on fixture-startup lag gave the helper ONE attempt, not three → detection was **1/3**. Fixed with
   `require_refresh_invoked=False`; also took the serial suite 5-passed/3-skipped → **8/0**.
3. **My own first mechanism claim was WRONG.** I reported a forced case as a cap truncation; with the
   reason surfaced it read `no-payload` (child killed). `truncated` only appears at
   `-HarvestTimeoutSec 40`. The derived budget (clamped 2..8 s) sits close to what a 451-element WPF
   window costs, independently of other load.
4. **Option 2 from #406 (a control-type amnesty) is actively dangerous, not merely unmeasured.**
   Databricks labels its auth-kind chooser `Personal Access Token` / `Databricks Client Credentials` —
   both in `credential_modal_signature.regex` *because* they identify a credential dialog.
5. **Port-vs-share:** completeness exists only in the arbiter. Python's `classify_dialog` takes no
   completeness input; a partial read fails the whole enumeration → `unknown_reason` (also exit 3).
   Sharing would mean adding a concept only PowerShell can produce. Vocabulary is shared (the four
   `.regex` files), control flow ported, gated by
   `test_the_arbiter_and_the_python_detector_share_one_vocabulary`.

## Environment / cleanup

- **No Power BI Desktop instance was opened by this session.** Nothing to clean up, and do not close
  anyone else's.
- WPF fixture apps are killed by `_run_probe_against_wpf_modal`'s `finally: app.kill()`.
- `scripts/sync_installed_skills.py` was **NOT** run in any mode (per #410). The installed plugin copy
  of `pbip-model-refresh` is stale vs this branch; `preflight.ps1` will report `STALE in plugin` until
  this merges and is published from master. That is expected, not a defect.
- Scratch (`_scratch/`) was deleted. `git status --porcelain` was empty at push time.

## Not done, deliberately

The harvest budget (derived from `-TimeoutSec`, clamped 2..8 s) is **not tuned**. Its margin is thin
for element-dense windows, so `DIALOG_UNREADABLE` will be commoner than the happy path suggests.
Tuning is a real trade (a longer budget is a longer poll) and belongs in its own issue.
