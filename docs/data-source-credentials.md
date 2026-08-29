# Data-source credentials: the migration preflight gate

Every migration in this repo so far uses **extract-based** Tableau sources (`.hyper`), which the toolkit
materialises to **CSV under a `DataFolder` parameter**. Flat files need **no credentials**: the model
refreshes from disk. The moment a workbook instead uses a **live database** (Databricks, SQL Server,
Snowflake, Synapse, ...), that stops being true: Power BI needs a **credential** to connect, and the
credential is **never in the committed model files**. It lives outside them, and a human normally
enters it once. This doc explains the two places that credential lives, how to detect when it's
missing, and what to do. It is grounded in a first-hand Databricks test (2026-07).

## TL;DR for the migrator

1. After parsing, **classify the data sources**:
   `python scripts/preflight_source_credentials.py --spec migrations/workbooks/<slug>/migration-spec.json`.
2. If every source is extract/flat -> no credential gate, proceed normally.
3. If any source is a **live database**, the migration cannot refresh/validate against data until a
   credential is configured. **You (the agent) cannot supply it.** Prompt the user, then verify:
   - **Local (Desktop, the iterative loop):** ask the user to open the `.pbip` in Power BI Desktop once
     and authenticate the source (Sign in / paste PAT) so Desktop caches it on the machine. Then prove
     data actually flows with `python .github/skills/pbip-model-refresh/scripts/probe_desktop_query.py
     --pid <pid>` (queries one row from the loaded model via the Desktop local AS) ->
     `PREFLIGHT: DATA_OK` before building the report. That probe ships inside the
     [`pbip-model-refresh`](../.github/skills/pbip-model-refresh/SKILL.md) skill; `scripts/probe_desktop_query.py`
     is a forwarding shim kept for existing callers.
   - **Cloud (service):** after publish, probe a refresh
     (`python scripts/preflight_source_credentials.py --model "<Workspace>" "<Model>"`); if it reports
     `ModelRefreshFailed_CredentialsNotSpecified`, have the user bind a credential (UI or a Fabric
     connection). Do not proceed to data validation until the gate is green.

## Why the agent cannot just supply the credential

Credentials are secrets stored **outside** the TMDL/PBIR files, encrypted, in a place with **no
agent-writable API**:

- **Power BI Desktop (local):** credentials are cached **per Windows user, DPAPI-encrypted**, in the
  Desktop / Mashup credential store on that machine. The connector shows a **modal** auth dialog the
  first time. Microsoft's own doc: *"Once you enter your credentials for a particular Databricks SQL
  Warehouse, Power BI Desktop caches and reuses those same credentials in subsequent connection
  attempts."* The **Desktop Bridge has no method to fill the credential modal** (its surface is
  `application.state.get` / `report.snapshot.capture` / `file.reload`; CLI 0.1.2 verbs are `status`,
  `manifest`, `open`, `reload`, `screenshot`, `screenshot-all` — none of them authenticate). So the
  agent's build/render/screenshot loop is blocked on live data until the user authenticates once.
- **Power BI service (cloud):** credentials live server-side in a **gateway datasource / Fabric
  connection**, set via the UI (*Semantic model > Settings > Data source credentials*) or the
  Connections API. Publishing via `fab import` binds **no** credential, so a refresh fails immediately.

## The two gates, proven first-hand (Databricks, `samples.nyctaxi.trips`, 1-row M probe)

The probe model uses a **pure-M** navigation (NOT a native SQL query), returning one row:

```powerquery
let
    Source = Databricks.Catalogs("adb-<id>.<n>.azuredatabricks.net", "/sql/1.0/warehouses/<id>",
        [Catalog=null, Database=null, EnableAutomaticProxyDiscovery=null]),
    samples_Database = Source{[Name="samples", Kind="Database"]}[Data],   // catalog level Kind = "Database"
    nyctaxi_Schema = samples_Database{[Name="nyctaxi", Kind="Schema"]}[Data],
    trips_Table = nyctaxi_Schema{[Name="trips", Kind="Table"]}[Data],
    OneRow = Table.FirstN(trips_Table, 1)
in
    OneRow
```

### Local gate (Power BI Desktop)

Opening this model on a machine with **no cached credential** and triggering Refresh produces a modal:

> **Databricks** — `{"host":"adb-...azuredatabricks.net"...}` — **"You aren't signed in."** with tabs
> *Databricks Client Credentials / Personal Access Token / Azure Active Directory* and **Connect /
> Cancel**. Status bar: *"Some of the tables have incomplete or no data."*

The Bridge cannot dismiss or fill this. **Remediation:** the user opens the `.pbip` in Desktop, picks an
auth method (paste a PAT, or Sign in with AAD), clicks Connect. Desktop caches it; subsequent
agent-driven opens/reloads then refresh without prompting.

**Detecting present vs missing (so the agent only prompts when needed).** The credential is cached
per-Windows-user (DPAPI), so it persists across Desktop restarts and even across different `.pbip`
files — **but it is keyed by the full data-source path (host *and* `httpPath`/warehouse), not the host
alone.** Verified 2026-07 first-hand: re-pointing the same model from one Databricks warehouse to a
freshly-created warehouse on the *same host* re-triggered the sign-in modal, because the new
`…/warehouses/<id>` path had no cached credential. So a prior sign-in only counts as "present" when the
host **and** warehouse/`httpPath` match; changing the warehouse opens a new local credential gate even
on an already-authenticated host.

**The one-row data probe is the AUTHORITATIVE local gate; the modal probe is a secondary signal.**
Verified 2026-07 (agent-run): against a *serverless* warehouse that cold-starts, the modal-watch
`scripts/probe_desktop_credential.ps1` returned a **false `CREDENTIAL_PRESENT` three times** because the
sign-in modal appeared only *after* the probe's 90s window. The one-row data probe
(`probe_desktop_query.py --pid <pid>`) correctly reported `NO_DATA` (0 rows) throughout, then a
UIA re-dump confirmed the modal was open. So: **treat `probe_desktop_query.py` (`DATA_OK` vs
`NO_DATA`/`ERROR`) as the gate of record**, and use `probe_desktop_credential.ps1` only to *explain* a
`NO_DATA` (i.e. "is a credential modal the reason?"). A `CREDENTIAL_PRESENT` from the modal probe must
NOT be trusted on its own for a serverless source — always confirm with `DATA_OK`.

`scripts/probe_desktop_credential.ps1 -DesktopPid <pid>` triggers a refresh via UI Automation and
watches for the connector modal:
- Modal appears (or is already open) -> `VERDICT: CREDENTIAL_MISSING` (exit 1) -> prompt the user to
  sign in once. **This is the only verdict that is a hard stop.**
- No modal within the timeout -> `VERDICT: CREDENTIAL_PRESENT` (exit 0), **but** re-confirm with the
  data probe before trusting it (see the serverless false-positive above).
- A dialog is up that is *not* a credential prompt -> one of `REFRESH_IN_PROGRESS` (another refresh
  already owns this instance — wait for it or cancel the stale one), `DIALOG_NEEDS_HUMAN` (a **known**
  human-blocking prompt that is not a credential prompt — the native-database-query approval modal;
  approve it, no sign-in implied), `DIALOG_UNRECOGNIZED` (no signature matched, or progress text
  alongside prose that is not progress status) or `DIALOG_UNREADABLE` (its **content** could not be
  shown to be harmless), each **exit 3**. All four mean *"could not probe"*, never *"a human must sign
  in"*.

⚠️ **Do not read a non-`CREDENTIAL_MISSING` verdict as a credential wall.** Until issue #367 the probe
returned `BLOCKED_BY_DIALOG` at **exit 1** for *any* visible non-main window >= 100x100 — a Power BI
Refresh progress dialog trips that trivially, and a field report on 2026-08-28 caught it doing so under
three concurrent refreshes against a cold Snowflake warehouse. The probe now classifies a dialog by its
text and reports what it actually saw; the size test only decides which windows are worth reading. The
**Python** fast check (`probe_desktop_query.py` / `refresh_pbip_model.py`) still emits
`BLOCKED_BY_DIALOG` from a size-only rule, so treat that token — wherever it comes from — as
"something is on screen", not as "sign-in required".

Two gotchas learned building it: (a) use a **generous timeout (>=60s)** because a serverless warehouse
(Databricks) can **cold-start** before the modal appears, so a short wait yields a false PRESENT; and
(b) also treat an **already-open** modal as MISSING (check before triggering refresh).

⚠️ **A third, found in blind review 2026-08-29: Win32 child-HWND text cannot see inside a WPF dialog,
and no proxy for "we read the content" survives contact.** WPF renders its whole visual tree into one
HWND, so only the window *caption* survives child enumeration. A real owned WPF modal captioned
`Refresh`, whose content read `Enter your credentials`, was classified as a progress dialog and the
probe exited **0 with `CREDENTIAL_PRESENT`** — a silent false negative on a hard stop. A second attempt
that also required "we harvested some text" fell to a `Cancel` button. The probe now merges **UI
Automation** text (`Name`, `ValuePattern`, `TextPattern`) before matching any signature, runs that
harvest in a **killable child process** (a hung provider held a 1 s probe for 15 s+ producing no verdict
at all), and — the load-bearing part — **suppresses a dialog only when it positively reads benign
CONTENT**, never because a caption looked reassuring. If you extend this probe, or write another dialog
detector: **a caption is not content, and "we read something" is not "we read the thing that matters".**
A third round found the same shape twice more: the **first** benign element used to classify the whole
window (so `Evaluating` beside *"Permission is required to run this native database query"* cleared it),
and a **missing JSON property** in the harvest child's payload computed to "complete" because
`-not $null` is `$true`. The probe now scans all content, recognises known human-blocking prompts by
name (`DIALOG_NEEDS_HUMAN`, exit 3), and validates the child's schema and exit code. The one-line
lesson across all three rounds: **missing evidence must never read as good evidence.**

**The robust positive check: query one row from the Desktop model (verified 2026-07).** Modal-absence is
a *negative* signal; the *positive* proof that data actually flows is to query the loaded model. Power BI
Desktop runs a local Analysis Services (`msmdsrv`); the skill's `scripts/probe_desktop_query.py --pid <pid>`
discovers its port and runs `EVALUATE TOPN(1, '<table>')` via ADOMD.NET. A returned row proves, in one
shot, **credentials present + source reachable + M/partition valid** -> `PREFLIGHT: DATA_OK`. Zero rows
or a connect error -> the gate is red. This is the real "can Power BI actually get data?" test, and it
runs entirely **locally, before any publish**. (It needs the model open in Desktop and refreshed; run it
after `probe_desktop_credential.ps1` returns `CREDENTIAL_PRESENT`, or after the user signs in.)

**Pass `--pid` whenever more than one Desktop instance is open** (a parallel batch): the probe binds
strictly to the `msmdsrv` owned by that pid - it retries briefly for startup lag, then fails, rather than
falling back to another instance. Without a pid it refuses to choose between instances, because a
`DATA_OK` read from a *sibling's* model looks exactly like a `DATA_OK` read from yours.
`powerbi-desktop status` maps pid -> open file.

### Cloud gate (Power BI service)

Publish the model (`fab import`) and trigger a refresh: it **fails in ~2 s** with

```json
{ "status": "Failed", "serviceExceptionJson": "{\"errorCode\":\"ModelRefreshFailed_CredentialsNotSpecified\"}" }
```

**Remediation, verified end-to-end** (refresh then `Completed`, 1 row loaded, confirmed via the remote
Power BI MCP `ExecuteQuery`):

1. Create a **Fabric cloud connection** with the credential (no manual RSA encryption needed):
   `POST https://api.fabric.microsoft.com/v1/connections`
   ```jsonc
   {
     "connectivityType": "ShareableCloud",
     "displayName": "<name>",
     "connectionDetails": { "type": "Databricks", "creationMethod": "Databricks.Catalogs",
       "parameters": [ {"dataType":"Text","name":"host","value":"adb-....azuredatabricks.net"},
                       {"dataType":"Text","name":"httpPath","value":"/sql/1.0/warehouses/<id>"} ] },
     "privacyLevel": "Organizational",
     "credentialDetails": { "singleSignOnType":"None", "connectionEncryption":"NotEncrypted",
       "skipTestConnection": false,
       "credentials": { "credentialType":"Key", "key":"<PAT>" } }   // Databricks PAT
   }
   ```
   With `skipTestConnection:false` the API tests the credential on create (201 = it works).
2. **Bind** the model's datasource to it:
   `POST /v1.0/myorg/groups/<ws>/datasets/<model>/Default.BindToGateway`
   `{ "gatewayObjectId": "<connection.gatewayId>", "datasourceObjectIds": ["<connection.id>"] }`
   (the connection's `connectionDetails.path` must match the model's datasource path — it does when the
   host/httpPath are identical).
3. Refresh again -> `Completed`.

> The classic alternative (PATCH the gateway datasource with an RSA-OAEP-encrypted credential using the
> gateway public key) also works but is fiddly; the **cloud gateway is not GET-able** for a
> just-published cloud datasource (404), so prefer the Connections API + bind above.

## Detecting live vs flat sources (what the classifier keys on)

The parser records `data_sources[].connection.{class,mode,server}` in `migration-spec.json`.
`scripts/preflight_source_credentials.py` classifies:

- `mode == "extract"`  -> **no creds** (packaged `.hyper` -> CSV + DataFolder).
- `class` in a flat-file set (`textscan`, `excel-direct`, `json`, ...) -> **no creds** (path-based).
- `class` in a live-DB set (`databricks`, `sqlserver`, `snowflake`, `azure-sql-dw`, `postgres`,
  `oracle`, `bigquery`, `saphana`, ...) -> **needs-credential**.
- anything else -> **review** manually.

## Preflight procedure for a live-source migration

1. **Classify** (`--spec`). If no live sources: skip the rest.
2. **Warn the user up front** that this workbook hits a live source, so a credential is required in Power
   BI before the report can show data. Give them the host/database from the spec.
3. **Local:** build the model, then ask the user to open the `.pbip` in Desktop and authenticate the
   source once (the modal above). Use `scripts/probe_desktop_credential.ps1 -DesktopPid <pid>` to check
   whether a credential is already cached (`CREDENTIAL_PRESENT`) before prompting — the cache is
   machine-wide but keyed by host **+ warehouse/`httpPath`**, so a prior sign-in to the *same host and
   warehouse* counts (a different warehouse on the same host does not). Only prompt on
   `CREDENTIAL_MISSING`. Then
   confirm data actually flows with the skill's `scripts/probe_desktop_query.py --pid <pid>` (one-row DAX probe
   against the Desktop local AS -> `PREFLIGHT: DATA_OK`). This one-row query is the definitive local
   gate: it proves creds + reachability + valid M together, without publishing anything.
4. **Cloud (if publishing):** publish, then run the gate (`--model`). If
   `ModelRefreshFailed_CredentialsNotSpecified`, have the user configure the credential (UI) or provide
   the secret so a Fabric connection can be created + bound. Re-run the gate until `Completed`.
5. Only after the gate is green does data-level validation (numbers matching the source) make sense.

## Notes / gotchas

- The 1-row probe is deliberately **M navigation, not `Value.NativeQuery`/`Databricks.Query`**, so it
  exercises the same connector + credential path the real model will use (and `Databricks.Query` isn't
  supported with DirectQuery anyway).
- Since Feb 2026 new Power BI Databricks connections default to the **ADBC** driver; add
  `Implementation="2.0"` in the options record to force it. Either driver hits the same credential gate.
- A PAT is a **secret**: never commit it, never put it in TMDL. Store it only in the service-side
  connection (Option B) or the user's Desktop cache (local). This repo's throwaway test kept the PAT in
  a temp file that was deleted once the connection held it.
