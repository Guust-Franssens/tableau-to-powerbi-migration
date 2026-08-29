---
name: live-source-reachability
description: Prove a Tableau migration's live database sources are reachable through the Power BI artifact that will ship, and route credential-gate verdicts. Use before building any workbook/datasource that has live sources, when probe_bundle/probe_live_source returns DATA_OK, OPERATOR_REQUIRED, NO_CREDENTIAL, UNREACHABLE, ERROR, or SKIPPED, or when a credential-gate audit/verify decision is needed.
---

# Live-source reachability and credential-gate routing

Use this before any builder starts on a workbook/datasource with live database sources. The invariant
is simple: prove the **Power BI artifact you will ship** can reach the source; a Python, SQL client or
Tableau-only proof does not exercise Power BI Desktop's credential store.

The full lifecycle and audit semantics live in
[`../../../docs/credential-gate.md`](../../../docs/credential-gate.md). This skill is the compact
execution route a migrator should invoke instead of carrying the mechanics inline.

## Run order

1. **Classify and arm without opening sockets.**

   ```powershell
   python scripts\preflight_source_credentials.py --spec <spec>
   python scripts\preflight_source_credentials.py --bundle <engine-bundle>
   ```

   Use the form that matches the active contract. Any live database source requires the proof below.

2. **Check the emitted artifact first.**

   ```powershell
   python scripts\probe_bundle.py <bundle> --check-only --spec <spec>
   ```

   Non-zero here outranks a later live probe: the model you plan to ship cannot refresh as emitted.
   Route `M_PARAM_UNDEFINED`, `SOURCE_COLLAPSED`, missing parameter, or missing endpoint evidence to
   the owner before probing live. On a parser-path migration with no bundle yet, continue to step 3.

3. **Probe through Power BI Desktop.**

   ```powershell
   python scripts\probe_live_source.py --spec <spec>
   python scripts\probe_live_source.py --bundle <engine-bundle>
   ```

   The probe builds a one-table PBIP sandbox, refreshes in Desktop, requires a row back for ordinary
   tables, records any earned `probe-cleared` audit entry, and refuses to fabricate missing
   table/column evidence.

## Verdict routing

| Verdict | Meaning | Action |
|---|---|---|
| `DATA_OK` | Power BI returned a real row; the probe earns the clear itself. | Continue. |
| `OPERATOR_REQUIRED` | Custom SQL/cost/modal risk needs a human Desktop refresh. | Hard stop; do not accept SQL-client proof. |
| `NO_CREDENTIAL` | Power BI lacks or rejects a credential. | Hard stop after one attempt; ask for Desktop sign-in or human build-only authorization. |
| `UNREACHABLE` | Address/network/spec failure, not a credential wall. | Report the bad address/path; do not send the user to sign in. |
| `ERROR` | Local tooling/artifact evidence failure. | Stop; fix/reroute the artifact evidence before retrying. |
| `SKIPPED` | No live source exists. | Record the skip and continue. |

## Rules that prevent false greens

- A credential/sign-in/permission refusal is final after **one** attempt. Retrying does not create a
  credential.
- `probe_bundle.py` checks the artifact you ship; `probe_live_source.py` checks a reconstructed
  one-table probe model. If they disagree, believe the shipped-artifact check.
- Never clear the gate by hand. `credential_gate.py clear` earns nothing; only a probe-earned
  `probe-cleared` line or a human `authorize` produces an auditable state.
- Finish by verifying the same bundle/dir whose audit log was armed:

  ```powershell
  python scripts\credential_gate.py verify <bundle-or-migration-dir>
  ```
