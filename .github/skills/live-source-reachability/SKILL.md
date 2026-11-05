---
name: live-source-reachability
description: Prove a Tableau migration's live database sources are reachable through the Power BI artifact that will ship. Use before builders start on any workbook/datasource with live sources, when a Desktop refresh stops on a sign-in or other dialog, or when sign-off needs real-data evidence for a live source.
---

# Live-source reachability

Use this before any builder starts on a workbook/datasource with live database sources. Prove that
the **Power BI artifact you will ship** can reach its source. A Python, SQL client or Tableau-only
check does not use Power BI Desktop's credential store, so it proves nothing here.

Run it **before dispatching builders**. START_READY can admit `live_unproven`; it is not data proof.
There is no write-time enforcement. Use the existing
[`pbip-model-refresh` procedure](../pbip-model-refresh/SKILL.md#automatic-callers---automatic-source-scope-780),
with its Desktop, bridge and client-library prerequisites; do not create a second refresh route.

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

   Non-zero means a static finding or unavailable input, not a credential diagnosis. Route
   `M_PARAM_UNDEFINED` / `M_PARAM_EMPTY` (undefined/blank M parameters) and `SOURCE_COLLAPSED`
   (declared endpoints missing from the model) to the model owner first. Exit 0 is structural only.

3. **Open the shipped working copy in Desktop.** That is `<bundle>/pbip/`, or `<package>/fabric/`
   when you were handed a package. Never the pristine `<bundle>/reports/` baseline.

4. **Refresh it with named canaries** through the existing automatic-source procedure:

   ```powershell
   python .github\skills\pbip-model-refresh\scripts\refresh_pbip_model.py --pid <pid> --ui-refresh --canaries <table> [<table> ...] --automatic-source-scope <expected-sources.json>
   ```

   - Pick named canaries covering the live sources. Each must return rows on the held model binding.
   - Expected identities come from a **trusted engine-emission snapshot, never the live model**.
     Follow the existing skill's derivation procedure. There is no bundled expected-sources producer;
     unavailable trusted input is a stop, never permission to fabricate a matching file.
   - Every agent-directed refresh uses `--automatic-source-scope` or explicitly delegates to that
     existing procedure. It fails closed on custom SQL, unsupported M, mismatched identities or
     unreadable metadata. Never drop the flag or switch refresh routes to evade a refusal.
   - Plain `--ui-refresh` remains operator-only execution of all current M, including custom SQL.
     No new consent flag or ceremony is introduced.

5. **A dialog is a human stop.** Any sign-in, permission or other Desktop dialog ends the attempt.
   The refresh refuses to act while a dialog is open.
   - Ask the human **once**, naming the source and what Desktop showed. Never type, paste or store
     credentials, and never click through a prompt.
   - Autopilot does not override this. End the turn and wait.
   - Structural refusals do **not** prove missing credentials. Inspect the reported scope/model
     finding; retain wrong-model checks and do not enter credentials or automate approval.
   - Only after the human resolves a dialog or the owner corrects the finding, repeat step 4.
     An unchanged credential/permission refusal is final, not a transient retry.

## Record the outcome

| Outcome | Meaning | Next |
|---|---|---|
| Exit 0, `DATA_OK + PERSISTED` | Power BI reached the source through the shipped model and every canary returned rows. | Continue; keep the output for sign-off. |
| A dialog finding | Needs a human; the structural stop alone does not classify the dialog. | Ask once (step 5). |
| `UI_PREFLIGHT_REFUSED` | Source scope, readiness or model identity was not established; not proof of missing credentials. | Stop; route the reported cause without bypassing qualification. |
| `NO_DATA` (a canary returned zero rows) | The refresh ran but a canary is empty. | Check the canary choice and source filters. Not proof. |
| `DATA_CHECK_FAILED`, `ERROR`, `TIMEOUT`, `COMPLETION_UNKNOWN`, `NOT_PERSISTED`, `WRONG_MODEL` | No real-data proof. | Stop; correct the cause and binding before a new attempt. |

- Write the outcome into the unit's handover and `limitations_encountered`. An unproven live source
  is a disclosed limitation, never a silent pass.
- **COMPLETE needs real-data proof.** `check_unit.py` reports COMPLETE for a live source only with a
  same-run PASS `data-evidence` row: full refresh, persisted, and named canaries that returned rows.
  Independent `@pbi-migration-validator` shipped-artifact fidelity sign-off remains mandatory;
  data proof alone is not a Tableau match or COMPLETE.
- **Model-only work** is allowed only when the brief says `fallback_authorization: model_only_unvalidated`.
  The result stays unvalidated and cannot serve a report as validated data.

## Old run trees

Historical write-deny remnants are handled only by the
[disarm recovery instructions](../../../docs/operator-runbook.md#414-an-old-run-folder-refuses-writes-after-the-credential-gate-was-retired).
Old markers and audit logs confer no current authority.
