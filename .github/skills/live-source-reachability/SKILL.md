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
   table's same-scope Power BI `DATA_OK` from this invocation; otherwise it performs connector
   navigation in the one-table PBIP. SQL Server uses the normalized server including port/instance
   and exact database; Databricks uses host, HTTP path and exact catalog/database; Snowflake uses
   account, warehouse, role and exact database. Scalar navigation metadata is eagerly buffered on
   the output dependency path; nested object tables are not read and no local success row is made.
   The conservative reuse key retains **all** declared connection fields, including credential/
   authentication/session hints. Different scopes cannot borrow proof; shell/ODBC success and
   prior-run observations never participate.

   The SQL stays untouched in the spec: no normalization, comment stripping, native SQL fallback,
   `SELECT 1`, TOP/LIMIT/WHERE wrapper, or automatic exact execution. #692 owns the separate explicit,
   cost-disclosed exact-query mode; it is not implemented here.

## Verdict routing

| Verdict | Meaning | Action |
|---|---|---|
| `DATA_OK` | Power BI returned a real row; the probe earns the clear itself. | Continue. |
| `CONNECTION_OK_QUERY_UNVALIDATED` | Only Power BI connector credential/session scope is evidenced. The custom SQL was **not executed**; no object/query permission, schema, rows, semantics, cost or refreshability claim. | **Exit 1; gate armed.** Keyed `probe-error` audit envelope with detail starting `CONNECTION_OK_QUERY_UNVALIDATED:` (the audit action vocabulary is closed); no `proved_names`, `probe-cleared` or gate lift. Stop unless an already valid brief/audit-backed degradation authorization permits model-only continuation; this probe never grants it. |
| `OPERATOR_REQUIRED` | The shipped-artifact probe requires a human Desktop action for native-query/cost risk. | Hard stop; do not accept SQL-client proof or silently run custom SQL. |
| `NO_CREDENTIAL` | Missing/rejected credential or sign-in evidence; not proof Power BI never authenticated before or that the source is reachable. | Hard stop after one attempt; ask for Desktop sign-in/credential repair or human build-only authorization. |
| `ACCESS_DENIED` | The classifier matched access-denial-shaped text (`403`/forbidden/permission denied/not authorized) ahead of the credential markers. It does **not** establish that authentication succeeded, that the failure is permission-only, or that a fresh sign-in cannot help — `403 Unauthorized: authentication failed` and `403 Forbidden: access token revoked` both land here. | Hard stop; the gate stays armed. **Unchanged retry is not useful** — read the redacted detail and change what the source named: the credential/token when it speaks of authentication or an expired or revoked token, the permission or object grant when it names a principal or object. Do not route it as a timeout or a transient error. |
| `UNREACHABLE` | Address/network/spec failure, not a credential wall. | Report the bad address/path; do not send the user to sign in. |
| `ERROR` | Local tooling/artifact evidence failure. | Stop; fix/reroute the artifact evidence before retrying. |
| `SKIPPED` | No live source exists. | Record the skip and continue. |

## Rules that prevent false greens

- A credential/sign-in/permission refusal is final after **one** attempt. Retrying does not create a
  credential.
- On connection-only completion say exactly: **Power BI connected using Desktop credentials. Your custom SQL was not executed and remains unvalidated; the gate is still armed.**
- ⚠️ Metadata navigation can itself take time or use compute; only customer-SQL execution is
  excluded. Timeout/no popup is not success. Existing dialog/error routing stays unchanged;
  #146/#687 own actual-Desktop prompt qualification. Empty navigation and empty ordinary tables
  remain non-success; zero-row `DATA_EMPTY` behavior is outside this slice.
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
