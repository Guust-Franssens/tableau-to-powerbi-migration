---
name: live-source-reachability
description: Prove a Tableau migration's live database sources are reachable through the Power BI artifact that will ship. Use before builders start on any workbook/datasource with live sources, when a Desktop refresh stops on a sign-in or other dialog, or when sign-off needs real-data evidence for a live source.
---

# Live-source reachability

Use this before any builder starts on a workbook/datasource with live database sources. Prove that
the **Power BI artifact you will ship** can reach its source. A Python, SQL client or Tableau-only
check does not use Power BI Desktop's credential store, so it proves nothing here.

Run it **before dispatching builders**. Nothing stops a builder from writing a model before the
source is reached, so the order is the safeguard.

## Run order

1. **Classify the sources (read-only, no network).**

   ```powershell
   python scripts\preflight_source_credentials.py --spec <spec>
   ```

   Any live database source needs steps 2 to 5. No live source: record that and stop here.

2. **Check the emitted artifact before opening Desktop.**

   ```powershell
   python scripts\check_bundle_sources.py <bundle> --spec <spec>
   ```

   Non-zero means the model cannot refresh as emitted. Route `M_PARAM_UNDEFINED` (an undefined M
   parameter) and `SOURCE_COLLAPSED` (declared endpoints missing from the model) to the model owner
   first. Refreshing a broken model proves nothing.

3. **Open the shipped working copy in Desktop.** That is `<bundle>/pbip/`, or `<package>/fabric/`
   when you were handed a package. Never the pristine `<bundle>/reports/` baseline.

4. **Refresh it with named canaries** through the `pbip-model-refresh` skill:

   ```powershell
   python .github\skills\pbip-model-refresh\scripts\refresh_pbip_model.py --pid <pid> --ui-refresh --canaries <table> [<table> ...] [--automatic-source-scope <expected-sources.json>]
   ```

   - Pick canaries that read from the live source. Each must return rows.
   - Plain `--ui-refresh` is operator-only consent to run all current M, including custom SQL. A
     human runs it. Nothing in this repo writes `expected-sources.json` yet, so for now every live
     refresh is a human step.
   - An automatic caller must add `--automatic-source-scope`. It refuses custom SQL and any M it
     cannot match to the expected sources, with `UI_PREFLIGHT_REFUSED` and exit 2.

5. **A dialog is a human stop.** Any sign-in, permission or other Desktop dialog, and any
   `UI_PREFLIGHT_REFUSED`, ends the attempt. The refresh refuses to act while a dialog is open.
   - Ask the human **once**, naming the source and what Desktop showed. Never type, paste or store
     credentials, and never click through a prompt.
   - Autopilot does not override this. End the turn and wait.
   - After the human answers in Desktop, rerun step 4 once. A repeat of the same refusal is final.

## Record the outcome

| Outcome | Meaning | Next |
|---|---|---|
| Exit 0, `DATA_OK + PERSISTED` | Power BI reached the source through the shipped model and every canary returned rows. | Continue; keep the output for sign-off. |
| A dialog finding or `UI_PREFLIGHT_REFUSED` | Needs a human. | Ask once (step 5). |
| `NO_DATA` (a canary returned zero rows) | The refresh ran but a canary is empty. | Check the canary choice and source filters. Not proof. |
| `DATA_CHECK_FAILED`, `ERROR`, `TIMEOUT`, `COMPLETION_UNKNOWN`, `NOT_PERSISTED`, `WRONG_MODEL` | No proof either way. | Fix the cause, then rerun once. |

- Write the outcome into the unit's handover and `limitations_encountered`. An unproven live source
  is a disclosed limitation, never a silent pass.
- **COMPLETE needs real-data proof.** `check_unit.py` reports COMPLETE for a live source only with a
  PASS `data-evidence` row: full refresh, persisted, and named canaries that returned rows. The
  validator must also sign off.
- **Model-only work** is allowed only when the brief says `fallback_authorization: model_only_unvalidated`.
  The result stays unvalidated and cannot serve a report as validated data.

## Old run trees

Run trees created before the gate was retired may still deny writes to `fabric`, `pbip`,
`*.SemanticModel` or `*.Report` folders. If a write fails with **Access denied**, run:

```powershell
python scripts\disarm_credential_gate.py <run-or-migration-root>          # list
python scripts\disarm_credential_gate.py <run-or-migration-root> --apply  # remove
```

Fallback for one folder: `icacls "<dir>" /remove:d %USERNAME%`. Any `.credential-gate-BLOCKED.json`
or audit log left in an old tree is inert and can be ignored.
