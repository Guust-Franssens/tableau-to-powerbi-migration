---
name: live-source-reachability
description: Prove a Tableau migration's live database sources are reachable through the Power BI artifact that will ship, and route credential-gate verdicts. Use before building any workbook/datasource that has live sources, when probe_bundle/probe_live_source returns DATA_OK, CONNECTION_OK_QUERY_UNVALIDATED, OPERATOR_REQUIRED, NO_CREDENTIAL, ACCESS_DENIED, UNREACHABLE, ERROR, or SKIPPED, or when a credential-gate audit/verify decision is needed.
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

   ✅ **Custom SQL is never executed by this default probe (#690).** It reuses only an ordinary
   table's same-scope Power BI `DATA_OK` from this invocation; otherwise it returns
   `OPERATOR_REQUIRED` **without generating custom M/PBIP, checking the network, opening Desktop or
   refreshing**. There is no connector navigation or native/generated SQL fallback, including for
   unsupported custom-only connectors.

   The conservative key retains **all** declared connection fields: SQL Server server/port-or-instance/
   database; Databricks host/HTTP path/catalog; Snowflake account/warehouse/role/database; and every
   credential/authentication/session hint. Only existing class/URL-host normalization is used.
   Differences prevent reuse, not trigger another operation. Shell/ODBC success and prior-run
   observations never participate. Ordinary one-row probing remains unchanged.

   The SQL stays untouched in the spec: no normalization, comment stripping, native SQL fallback,
   `SELECT 1`, TOP/LIMIT/WHERE wrapper, or automatic exact execution. #692 owns the separate explicit,
   cost-disclosed exact-query mode; it is not implemented here.

## Verdict routing

| Verdict | Meaning | Action |
|---|---|---|
| `DATA_OK` | Power BI returned a real row; the probe earns the clear itself. | Continue. |
| `CONNECTION_OK_QUERY_UNVALIDATED` | Only existing same-invocation, exact-scope ordinary `DATA_OK` is reused. The custom SQL was **not executed**; no custom-query/object permission, schema, rows, semantics, cost or refreshability claim. | **Exit 1; gate armed.** Keyed `probe-error` envelope with detail starting `CONNECTION_OK_QUERY_UNVALIDATED:`; no custom `proved_names`, `probe-cleared` or gate lift. Only an already valid brief/audit-backed degradation authorization may permit model-only continuation; this probe never grants it. |
| `OPERATOR_REQUIRED` | Custom SQL has no same-invocation, same-scope ordinary proof. No custom connection operation was attempted and no connection claim was earned. `probe_bundle.py` retains its separate operator handoff. | **Exit 1; hard stop; gate armed.** This custom no-operation path uses keyed `probe-error` with detail starting `OPERATOR_REQUIRED:`; other operator producers are unchanged. No PBIP/Desktop/network/native-query fallback, authorization or model-only permission. Do not accept SQL-client proof. |
| `NO_CREDENTIAL` | Missing/rejected credential or sign-in evidence; not proof Power BI never authenticated before or that the source is reachable. | Hard stop after one attempt; ask for Desktop sign-in/credential repair or human build-only authorization. |
| `ACCESS_DENIED` | The classifier matched access-denial-shaped text (`403`/forbidden/permission denied/not authorized) ahead of the credential markers. It does **not** establish that authentication succeeded, that the failure is permission-only, or that a fresh sign-in cannot help — `403 Unauthorized: authentication failed` and `403 Forbidden: access token revoked` both land here. | Hard stop; the gate stays armed. **Unchanged retry is not useful** — read the redacted detail and change what the source named: the credential/token when it speaks of authentication or an expired or revoked token, the permission or object grant when it names a principal or object. Do not route it as a timeout or a transient error. |
| `UNREACHABLE` | Address/network/spec failure, not a credential wall. | Report the bad address/path; do not send the user to sign in. |
| `ERROR` | Local tooling/artifact evidence failure. | Stop; fix/reroute the artifact evidence before retrying. |
| `SKIPPED` | No live source exists. | Record the skip and continue. |

## Rules that prevent false greens

- A credential/sign-in/permission refusal is final after **one** attempt. Retrying does not create a
  credential.
- On connection-only completion say exactly: **Power BI reached this same connection scope through an ordinary table in this probe. Your custom SQL was not executed and remains unvalidated; the gate is still armed.**
- Without same-scope ordinary proof say exactly: **Your custom SQL was not executed. No safe automated connection-only operation is currently available without catalog enumeration or a native-query approval prompt. No connection claim was earned; the gate remains armed.**
- ⚠️ #694 is a nonblocking enhancement for future connector strategies. Any future generated-query
  default needs explicit dialect support and #146/#687's qualified native-approval routing. No
  popup/timeout is success; existing ordinary dialog/error and zero-row behavior stays unchanged.
  No health-query template, popup watcher, exact-query mode or new model-only authority is added.
- `probe_bundle.py` checks the artifact you ship; `probe_live_source.py` checks a reconstructed
  one-table probe model. If they disagree, believe the shipped-artifact check.
- Never clear the gate by hand. `credential_gate.py clear` earns nothing; only a probe-earned
  `probe-cleared` line or a human `authorize` produces an auditable state.
- Finish by verifying the same bundle/dir whose audit log was armed:

  ```powershell
  python scripts\credential_gate.py verify <bundle-or-migration-dir>
  ```

  `verify` rejects unvalidated artifacts behind the armed gate; with no artifacts it can report
  compliance while the gate remains blocked. That is not earned clearance or authority to build.
