---
name: pbip-model-refresh
description: Refresh a local PBIP/TMDL semantic model open in Power BI Desktop, and persist the result to .pbi/cache.abf (headlessly via AMO Server.ImageSave, UI-Automation fallback). Persisting is the DEFAULT - opt OUT with `--no-save` for read-only or validate-then-deploy work; saving aligns database.tmdl's declared compatibilityLevel so the cache stays loadable, and the cache write is staged and swapped atomically. Use after finishing TMDL edits, before handing a model to report authoring, or whenever Desktop reopens a migrated model empty. For DAX-only edits, pass `--calculate-only` / `--measures-only` to recalculate formulas without re-reading source rows. `--ui-refresh` is a separate explicit route for supported whole-model Import M models and requires typed engine completion evidence. Source-tool agnostic - the model is already Power BI, so this applies equally to Tableau, Qlik and Cognos migrations.
---

# Refresh a local PBIP model, and make the data survive a close

**Windows only.** Talks to the local Analysis Services instance that Power BI Desktop hosts, over
ADOMD.NET/AMO (pythonnet), plus the Desktop Bridge CLI for process to file mapping.

Requirements, in full: Windows with Power BI Desktop, Python >= 3.11 with `pythonnet`, the ADOMD.NET
and AMO client libraries under the NuGet global-packages cache (`$env:NUGET_PACKAGES` when set,
otherwise `~/.nuget/packages`), and the installed `powerbi-desktop` CLI from
`@microsoft/powerbi-desktop-bridge-cli` on PATH for `--ui-refresh` (legacy XMLA discovery still uses
`npx`). No other dependency, and nothing repo-specific.

The ADOMD.NET client is a *separate* nuget package from TOM/AMO, so a machine can have one and not the
other. If `probe_desktop_query.py` prints `AdomdClient.dll not found in the nuget cache` (and
`preflight.ps1` WARNs `ADOMD.NET client (Desktop probe/refresh)`), restore it — forcing a supported TFM
so `dotnet add` cannot silently no-op on a net10 default:
`dotnet new console -o $env:TEMP\adomd --framework net8.0; dotnet add $env:TEMP\adomd package Microsoft.AnalysisServices.AdomdClient.NetCore.retail.amd64 --version 19.84.1`

## Available scripts

- [**`scripts/refresh_pbip_model.py`**](scripts/refresh_pbip_model.py) - refresh the open model,
  refuse a wrong-instance bind, and persist `cache.abf` (AMO `ImageSave`, UI-Automation fallback).
- [**`scripts/probe_desktop_query.py`**](scripts/probe_desktop_query.py) - read-only preflight: port
  discovery, ADOMD loading, table listing, and the one-row DAX probe. `refresh_pbip_model.py`
  shares the observation authority below with refresh.
- [**`scripts/_desktop_observer.py`**](scripts/_desktop_observer.py) - the sole read-only classifier,
  exact-identity observation supervisor, closed diagnostics and private short-lived image leases.
- [**`scripts/_desktop_uia.ps1`**](scripts/_desktop_uia.ps1) - private bounded Win32/UIA fact harvest;
  no policy, clicks, Refresh invocation or polling arbiter. It is not a user-facing command.
- `scripts/_desktop_actions.py` and [`scripts/_desktop_actions.ps1`](scripts/_desktop_actions.ps1) -
  private bounded child supervision and one named UIA action for the explicit `--ui-refresh` route;
  neither decides engine completion or classifies prompts.
- [**`tests/`**](tests) - the regression suite for both, runnable from this folder
  (`pytest tests`). It is what makes the portability claim below checkable rather than aspirational.

## The problem this solves

A migrated model hands over as *TMDL plus a promise*. Two things go wrong:

1. **Refreshing is hand-rolled every time.** The only working path against a local PBIP is TMSL over
   the child `msmdsrv` port: discover the port, load ADOMD.NET, resolve the catalog GUID, send
   `refresh`. Re-deriving that per migration is slow and easy to get subtly wrong.
2. **An XMLA refresh is not persisted.** It populates the *in-memory* model only. Desktop keeps a
   PBIP's data in `<Name>.SemanticModel/.pbi/cache.abf`, and if nothing writes that file the next
   person opens an empty model and hits the credential prompt all over again.

## Run it

```
python scripts/refresh_pbip_model.py [--pid <pbidesktop-pid>] [--canaries "A" "B"] [--tables "A" "B"]
                                     [--calculate-only|--measures-only] [--no-save] [--verify-only]
                                     [--ui-save] [--ui-refresh]
```

### Explicit foreground UI refresh

⚠️ **Operator-only source-execution consent:** supplying `--ui-refresh` expressly commissions
execution of **all current M**, including native/custom SQL and unknown/indirect query forms,
with potential source-side costs or effects. There is no custom-query admission screen.
**`--no-save` prevents Save, not source execution.** No additional consent flag is required.
This route does **not** authorize automatic credential-gate use or earn credential clearance.
Defaults and existing callers remain unchanged. Automatic custom/unknown-query integration in
PR B depends on accepted follow-up [#780](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/780)
(linked [#692](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/692)).

`--ui-refresh` is an opt-in route; the existing XMLA default and migration gates do not change. After
the clean-start sequence below, it invokes one named Desktop **Refresh** control, then waits for engine
metadata—not the Invoke return, quiet UI, or positive cached rows—to prove that the exact source-partition
set stayed bound and every Import M source partition became `READY` with a typed UTC `RefreshedTime` strictly newer than its
baseline and at or after the action start. It never retries Refresh or falls back to AMO/XMLA
processing. After completion it runs the existing canary checks on that same held binding. An
optional named UI **Save** is invoked only after completion, positive required row checks, a fresh
READY observer state and the existing model-identity check; `--no-save` suppresses every Save.

**UI readiness requires explicit `--pid`.** Before cache/engine discovery this route waits for
top-level bridge `status: "ready"` AND that exact PID's `bridgeStatus: "connected"` with a
nonempty absolute current-file binding. One monotonic **60-second maximum** startup budget,
clipped to the existing operation deadline, includes initial readiness, both READY observations,
reload, post-reload status and helper cleanup.
It proceeds immediately when ready; it never sleeps a blind minute or proceeds after expiry.
The installed CLI's `--wait-seconds` waits for connection discovery only. A small same-PID loop
also awaits ordinary `not_connected` and the recognized `BRIDGE_ERROR` "Another operation is
already in progress" response with Boolean `retryable: true` and finite nonnegative numeric
`retryAfterMs` (delay clipped to 50–1000 ms, then remaining time). Unknown errors, malformed
retry metadata and process disappearance refuse immediately; no terminal/credential error retry.

**Clean-start reload is defensive sequencing, not repair proof.** The **already-fetched** ready
instance must report `hasUnsavedChanges: false` as an actual Boolean before any reload. Dirty,
missing or non-Boolean values refuse; the route never silently discards existing edits. One complete
READY observer snapshot pins the held PID's exact `.NET CreationDate.ToUniversalTime().Ticks`
string and the bridge's exact absolute `currentFilePath`. Prompts, unknown/incomplete evidence or
unestablished process identity refuse before reload.

It then issues **one** exact-PID bridge `reload`, requires `status: "ok"`, an exact integer PID,
`result.success: true` and no reported errors, and performs **one** subsequent bounded status call
(not another readiness retry loop). That status must be ready/connected for the same PID and exact
absolute file path, again with strict Boolean `hasUnsavedChanges: false`. A fresh complete READY
observation must match the pinned process-start ticks. Expired budgets and late output, including
late cleanup, refuse before Refresh. Cache/port discovery, engine binding, model identity and all
partition baselines are acquired **after** these checks; the pre-reload observer is not handed
downstream. There is no reload between Refresh and optional Save.

✅ The bundled synthetic controls exercise this ordering and fail-closed clean-state checks.
⚠️ This does **not** establish external-warning repair, persistence, synchronization of disk TMDL
with Desktop, or actual-Desktop performance. Reload is not a substitute for reopening after model
edits; the model synchronization and stale-Save warnings below still apply.

The UI route resolves the already-installed `powerbi-desktop` launcher on PATH for `status --pid ...`
and the same contained transport for its one `reload --pid ...`,
never `--yes`, a custom package resolver or automatic tooling updates. Missing installations
refuse. The child wait uses the remaining original budget; timeout terminates only the owned
launcher PID and its descendants, never Desktop or a sibling process. Native startup/teardown
may take additional OS time, but late responses cannot authorize any action.
Readiness failure prints a non-success refusal/timeout immediately, then permits only one
existing bounded read-only exact-PID observer diagnostic. This extra diagnostic allowance is
not a new readiness budget; it cannot clear refusal, foreground Desktop or manipulate prompts.
Known exact-window templates may provide predefined readable messages; arbitrary error text
export, clipboard capture and foreground-only inspection are **not implemented**. Withheld or
unreadable text requires local human inspection and is not a credential diagnosis. These UI-only
rules do not change legacy XMLA callers or the historical 90-second CLI examples below.

Completion reads may encounter the existing bound reader's `REFRESH_IN_PROGRESS` preflight
refusal. Only that closed state is awaited within the original operation deadline; prompt,
unknown/incomplete, identity and catalogue refusals are not retried as progress.
An unreadable or unrecognized finding in the UI-route completion observer is reported with
its existing `DIALOG_UNREADABLE` or `DIALOG_UNRECOGNIZED` refusal and exact-window diagnostic,
not replaced by a generic claim that uncertainty persisted to the deadline. This preserves
the refusal; it does not establish completion or identify the dialog as progress or credentials.

The UI route holds one monotonic operation deadline set in the CLI before initial observation,
port discovery and identity preflight, through completion and
Save; no phase resets it. Each UI-only bound metadata/no-op/data read receives at most
**30 seconds for the whole bound call, including observation and identity/catalogue rechecks**,
not 30 seconds per internal step, clipped to the remaining operation time and recalculated
before each read. Exhausted budgets and late results cannot authorize Refresh or Save.
The native action remains capped at 20 seconds or the remaining time, with unchanged cleanup.
Shared binding/observer budgets and the XMLA route are unchanged; failed reads are not retried.

The initial supported surface is whole-model Import M source partitions. DirectQuery, Dual/mixed,
unsupported source kinds, modes that cannot be resolved from model/partition metadata,
changed or incomplete partition metadata, a non-READY initial observer, and
`--verify-only`, any `--tables` argument
(including an empty one), `--calculate-only` or `--measures-only` refuse before Refresh. Supply
`--canaries` to verify commissioned source tables. Positive rows alone do not prove processing;
no-action timestamp rereads, partial advancement, stale timestamps, prompt/unknown observer evidence
and deadline expiry cannot authorize success or Save. Deadline expiry is unknown, not a credential
diagnosis. Focus acquisition is limited to three verified attempts with a 150 ms wait each; a failed
focus/action is surfaced, and a human foreground switch is never overridden during handback.

The action child focuses the held root before enumerating controls. Only enabled, visible,
exactly named Invoke controls without an ExpandCollapse pattern qualify. A child provider must
have a verified process/start ancestry chain to the held Desktop, and its nearest native renderer
must belong to that provider and descend from the exact root. Under per-monitor DPI awareness,
a native hit-test at the control's physical interior must land in that renderer's verified
subtree. This is ownership corroboration only: there is no coordinate click or activation.
Exactly one candidate must survive; zero, multiple, missing geometry or changed ownership refuse.
The child reacquires and revalidates the selected control, ancestry, renderer/hit ownership and
foreground before Invoke. Refusals after focus acquisition also attempt truthful best-effort
handback only while Desktop still owns foreground. The same rules apply to Refresh and Save.
Each individual enumeration caches the verified ancestry/start chain once per distinct provider PID;
renderer, root, geometry and native-hit checks remain live for every candidate. Every surviving
provider is reread freshly before accepting the scan; changed or unavailable identity refuses the
whole scan, not a reduced set. The cache is discarded after that enumeration. The second full
uniqueness scan and independent selected/final ownership checks remain fresh, within the unchanged
20-second child cap and overall deadline.
Focus uses bounded thread-input attachment between the current thread and distinct required
foreground/target threads, with exact root and permitted-foreground checks before attachment and
before BringWindowToTop/SetForegroundWindow. Only successful attachments are tracked and detached
in finally on every path. There is no input injection, ShowWindow or minimized-window recovery.
Handback additionally requires the captured prior HWND's PID/start identity to remain unchanged;
a detected human foreground switch or identity change prevents restoration.
Handback verification is a point-in-time observation after 150 ms, not a guarantee of stable
foreground ownership: Desktop can activate a later window. There is no delayed restoration
retry, since a later Desktop activation cannot establish that the human did not switch back.

Mode decoding uses the engine's `ModeType` for `Model.DefaultMode` and `Partition.Mode`:
Import=0, DirectQuery=1, Default=2, Push=3, Dual=4, DirectLake=5. Only partition Default
inherits a positively established Import model default; missing metadata does not inherit.
`TMSCHEMA_TABLES` supplies identity/name, not an assumed `StorageMode` column or encoding.
See [ModeType](https://learn.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.tabular.modetype)
and [the table rowset](https://learn.microsoft.com/en-us/openspecs/sql_server_protocols/ms-ssas-t/c32ff15a-77b1-4d7b-bd33-84f5921d7e18).

This is not exclusive attribution against a human refreshing the same Desktop instance. Partition
timestamps do not prove upstream freshness, source reachability beyond the commissioned canaries,
query equivalence, or cold-reopen durability. UI Save Invoke and the legacy cache timestamp display
are not persistence proof.

### Read an already-detected dialog at the failing tool (#498)

✅ Refresh and query CLI refusals now attempt one read-only UIA harvest of the **exact HWND already
selected by the Win32 detector**, after the refusal or terminal latch is chosen. Standalone capture
failures and package capture/reload/observation failures inspect only their held PID. The explicit-PID
DAX oracle checks before opening its connection and before each reader, including persistent requests;
a refusal returns its existing JSON error, with the diagnostic on stderr and unchanged exit behavior.
Port-only and offline oracle calls do not infer a PID.

The harvest-only `-DiagnosticPid` option verifies the HWND's process before reading text and again
afterwards. It omits editable/password values, keeps control roles, and marks oversized or incomplete
reads non-complete. It exits before the normal PowerShell probe can invoke Refresh. Python bounds the
child wait at eight seconds and discards child stderr; native startup/teardown latency is additional.
There are no clicks, focus changes, clipboard reads, approvals, saves or restarts.

Only two complete English title/instruction/**Button.Name** structures produce fixed, value-free
messages: `privacy_warning` (the operator's file-trust decision) and `package_session` (a Desktop
session error; check unsaved-state safety before reopening). Everything else is `WITHHELD` or
`CANNOT_READ`, never arbitrary UI text. `DIALOG_DIAGNOSTIC` is a separate JSON line, not a verdict;
decode its escaped numeric strings before reading PID/HWND metadata. The detector's token, exit,
suppression, deadlines, success behavior and gate state are unchanged.

⚠️ This is diagnostic-only, not a second classifier. An incomplete read never establishes clean
absence. Real-Desktop latency, localization, MSHTML-only dialogs, dialogs absent from existing
discovery, and modals appearing after a DAX precheck remain unverified/outside this slice. The buffered
`probe_live_source.py` wrapper and its catalog-readiness behavior are a separate follow-up.

### Inspect an unreadable in-flight dialog without stealing focus (#146)

**Phase-1 seam: acquisition plus a metadata record, not pixel classification.** An in-flight
`DIALOG_UNREADABLE` owned finding is offered to a background worker for one bounded exact-HWND
**`PrintWindow(PW_RENDERFULLCONTENT)`** attempt. PID, visibility, direct owner, owner PID and disabled
owner are checked before rendering, after rendering and after writing. Failure, blank/unpainted
pixels, changed identity or incomplete metadata cannot become a semantic verdict. The existing
unreadable latch, refresh deadline and successful-worker behavior remain unchanged.

✅ **DPI-cropped evidence is not acquired (#686).** Measured September 17, 2026 on actual
Desktop **2.158.758.0 at 125% scaling**: the old child reported `ACQUIRED` for a **502×264**
top-left crop of a **627×330** physical popup, omitting lower/right Close controls. Valid PNG,
matching digest and nonblank pixels all passed; none established completeness.

The killable capture child now sets its thread to **per-monitor DPI aware** before reading bounds
or rendering, and restores its previous context afterwards. Before and after rendering, its full
window rectangle must contain the independently physical **DWM extended-frame bounds**. The full
rectangle retains invisible resize borders; allocating only the smaller visible-frame dimensions
would introduce another crop. Unavailable DPI support returns `UNSUPPORTED`; failed setup or
unestablished physical bounds returns `BLANK_OR_INCOMPLETE`, never a virtualized fallback.
The v1 thread context requires Windows 10 1607 or later; v2 is not required. See Microsoft's
[DPI context API](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setthreaddpiawarenesscontext)
and [physical versus virtualized bounds](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowrect).

**Invariant:** `ACQUIRED` requires the complete physical frame of the pinned PID/HWND/owner.
Independent physical bounds and bottom/right content markers gate the synthetic controls; merely
matching the child's own dimensions or the old top-left pixels does not. This closes the measured
geometric crop, not renderer staleness, text legibility or semantic classification.
⚠️ **Post-fix actual-Desktop requalification is still required after #687 releases Desktop.**
The retained actual-Desktop evidence above proves the old defect, not qualification of this fix.
Observer eligibility remains `DIALOG_UNREADABLE` only; no OCR or new prompt classification is added.

**The wait only queues an immutable exact-target snapshot.** Its single pending slot never waits
for storage validation, Git, file/process creation or acquisition. A busy slot can drop an
observation; later polls can offer it again. At most one attempt per PID/HWND is started.
The original monotonic refresh deadline is pinned before evidence-worker construction and checked
before accepting completion in both wait branches, including after a delayed inspection. A worker
finishing after that deadline cannot overwrite a timeout or latched dialog result during cleanup.
Timely success remains success even while optional evidence is delayed.

Queueing, startup and capture share an **8-second** attempt budget, capped by the remaining refresh
deadline. A late storage check cannot start acquisition. The acquisition watcher starts **before**
`Popen`; timeout/exit closes the reserved delete-on-close lease, and a process handle returned late
is killed without publishing an image. Native process creation itself cannot always be interrupted:
it runs on a daemon observer, never on the refresh wait thread, and teardown does not join that
observer. This is not a guarantee that a wedged OS call returns; its eventual result is discarded.

**Configure caller-owned private scratch explicitly.** Supply
`--evidence-dir <existing-local-run-scratch>` or export **`PBIP_EVIDENCE_DIR`** before invoking a
probe that launches this refresh. The Python API also accepts `refresh(..., evidence_dir=Path(...))`;
the explicit argument overrides the environment. A suitable location is the allocated run's
`scratch` directory, never a package, deliverable, model or report directory. When inside a checkout
the destination must be positively Git-ignored. Remote/reparse locations, actual artifact ancestry
and artifact/package markers (including `package-manifest.json`) are refused, even at a Git root.
An ancestor's unrelated `fabric` or `pbip` sibling alone does **not** classify the scratch directory.
The directory must already exist. **There is no image fallback to the working directory.**
An unconfigured/unusable directory emits a non-verdict diagnostic and preserves the wait.

Run the refresh **asynchronously, attached to the session**, with its explicit PID and the usual
`--no-save` for read-only work. Read live stdout early; do not wait for the final exit. The stable
wire interface is a line beginning **`LOCAL_IMAGE `**, followed by one JSON object:

| Field | v1 meaning |
|---|---|
| `schema`, `event` | `pbip.window-image.v1`, `owned_modal_image` |
| `capture_id`, `status` | Opaque per-attempt ID; one `ACQUIRED` notice per stable HWND, followed by cleanup updates using the same marker/ID |
| `timestamp_utc`, `captured_at_utc` | UTC notice time; publication time after verified acquisition (`null` when not acquired) |
| `desktop_pid`, `main_hwnd`, `dialog_hwnd`, `owner_hwnd` | Decimal **strings**. `main_hwnd` is the existing enumerator's root snapshot, or `null` when ambiguous; ownership checks attest the dialog/direct-owner relationship, not an invented main-window identity |
| `ownership_checks` | `before`, `after_render`, `after_write`: real Booleans or `null` for a check not established |
| `dimensions`, `capture_success`, `capture_method` | Captured width/height as decimal strings, success Boolean, `PrintWindow_PW_RENDERFULLCONTENT` |
| `image_basename`, `path`, `sha256` | Same random basename/relative locator, and SHA-256 of the PNG, checked against the observer's pinned file handle; never a host path |
| `expires_at_utc`, `cleanup_state` | Planned expiry; `not_created`, `pending`, `removed_externally`, `expired`, `removed_on_exit`, `removed`, or `cleanup_failed` |
| `classification_provenance` | `null`: deterministic acquisition has not reviewed the pixels. A visual reviewer records its own provenance separately |

**Parse JSON before interpreting fields.** Wire strings escape zero as `\u0030`: that preserves the
decoded PID/HWND/hash/timestamp while preventing numeric substrings such as `10054` or `403` from
tripping legacy free-text failure classifiers. Numeric values are intentionally strings, not JSON
numbers. Do not feed re-serialized metadata into an authentication-text scanner.

**Exact local reviewer workflow:**

1. Match `schema`, `capture_id` and the expected Desktop PID. Require `status: ACQUIRED`,
   `capture_success: true`, all three ownership checks `true`, `cleanup_state: pending` and an
   unexpired record. Resolve **only the basename against the configured evidence directory**,
   never against the process working directory. Read only that file and verify its SHA-256 before
   reviewing pixels. Neither hash nor geometry identifies a prompt.
2. Use a reader that permits **`FILE_SHARE_DELETE`**. The image is held under a protected owner-only
   DACL and a kernel **delete-on-close** lease. Node/libuv readers are verified; ordinary Python
   `open(path)` does not share deletion on Windows. A Python image decoder can use
   `_open_private_image(path, existing=True)` as a shared-delete stream and decode it in memory.
   Do not copy pixels to another file to work around an incompatible viewer, and never print
   bytes/base64 or put images in a log, receipt, Git tree, package or deliverable.
3. A **positively identified username/password form** may be routed by the visual reviewer to the
   existing credential stop; a **positively identified native-query approval prompt** to
   operator-required. Record only category, reviewer/method, review time, `capture_id` and image
   hash as classification provenance—not field values, source names or dialog text. Do not enter
   credentials, approve a prompt, forge the refresh's exit code, or clear a gate from metadata alone.
   Absent positive review, preserve `DIALOG_UNREADABLE` and the existing wait/success semantics.
4. **Delete the exact file immediately after handling and close the viewer.** For example, remove
   `Join-Path $env:PBIP_EVIDENCE_DIR $record.image_basename` with `Remove-Item -LiteralPath`.
   The observer notices external deletion without inferring who deleted it or what was classified.
   It also deletes at **60 seconds**, normal/error wait exit and process exit. Kernel handle cleanup
   covers forced termination too; the native control observed a 5–6 ms delay after process signalling.
   Readers holding their own open handles may delay final deallocation, so close them promptly.
5. `CLEANED`/`CLEANUP_FAILED` updates retain metadata/hash only. Other statuses are closed,
   non-semantic diagnostics. A cleanup refusal must not replace the original refresh result;
   close a holding viewer and remove the same private file locally. Never publish pixels to explain
   a cleanup failure. Durable logs may retain the safe record and external review provenance only.

**Follow-up seam, deliberately not wired here:** `probe_live_source.py` currently buffers the
refresh's output. Its Phase-1 consumer must forward/stream `LOCAL_IMAGE` lines, supply the run-owned
`PBIP_EVIDENCE_DIR`, perform the schema/PID/hash/lifetime checks above, and route a separate positive
visual review. That orchestration requires a file outside this change's closed surface. The
image seam itself adds no UIA enrichment, OCR or external-verdict ingestion. The separate #498
exact-HWND text diagnostic above runs only at terminal reporting; detector verdicts and active-wait
behavior remain unchanged.

✅ **Precedent:** the two independent September 15, 2026 controls on #146 acquired readable
Snowflake forms with their owners minimized and without foreground/restore. This implementation
never foregrounds, restores, activates or screen-captures. Foreground is neither the default nor a
proven universal fallback.

⚠️ **Limits:** identity checks are snapshots; identical PID/HWND/owner destruction/reuse entirely
between checks cannot be distinguished. Painted/nonuniform pixels do not prove current, complete or
legible connector content. Native synthetic controls exercise the implementation; real
Desktop/connector qualification remains separate. The capture child's eight-second budget and the
image's lifetime never change the refresh's XMLA or wall-clock deadline.

**`--calculate-only` / `--measures-only` is an opt-in DAX-only shortcut, not the default.** It sends
TMSL refresh type `calculate`, which recalculates formulas, relationships and hierarchies without
re-reading source rows. Use it only when the caller knows the pending edit was measure/DAX-only; after
an M, partition, relationship or data-shape change, `calculate` can leave stale data wearing a fresh
verdict, so the default remains the safe full refresh. This recipe is credited to SES field use
(Sandeep Munagala, 2026-08-21), where the proven sequence was live measure edit -> Calculate ->
ExportToTmdlFolder -> cache persist across ~6 workbooks in one day.

**Persisting is the DEFAULT** — that is this script's stated purpose, *"so the next agent (and the
next Desktop open) sees real data instead of an empty model"*. Pass **`--no-save`** for read-only
work (the validator is read-only **by contract**) or for validate-then-deploy, where the project must
stay byte-identical.

> ⚠️ **Saving ALIGNS `database.tmdl`, and that alignment is what makes it work.**
> **Root-caused 2026-08-07.** The earlier finding (3-vs-3 on 2026-08-04: a persisted `cache.abf`
> made the PBIP open as **"Untitled - Power BI Desktop"** with the bridge reporting
> `Host is not ready to accept operations`) was **real but mis-attributed**. Persisting is not
> inherently destructive; persisting a cache whose compatibility level **disagrees with the
> project's** is.
>
> **The mechanism.** Desktop silently runs the database at a HIGHER level than the project declares —
> measured live `Database.CompatibilityLevel: 1606` against a `database.tmdl` of `1604`, on a server
> whose default is `1700`. `ImageSave` serialises the **live** database, so without alignment the
> cache is a 1606 image in a 1604 project. The reopen then hits Desktop's own **"Tabular databases do
> not support CompatibilityLevel downgrade"**, which surfaces with **no visible message** — only the
> `Untitled` window and the bridge error.
>
> **`--save` therefore aligns, always, with no flag.** That is exactly what Desktop's own Save does: a
> UI Save was measured updating `database.tmdl` `1604 -> 1606` and writing `cache.abf` **at the same
> timestamp**. An earlier version put the alignment behind `--align-compat` and refused otherwise;
> that was removed as **ceremony rather than safety** — the only possible response to the refusal is
> to re-run with the flag, so an agent simply learns to always pass it. Verified end to end: after
> aligning, a cold reopen gives `PREFLIGHT: DATA_OK` with **no refresh**, and the write touches
> **2 files** where a UI Save touches **79**.
>
> ⚠️ **Saving EDITS THE DEPLOYABLE ARTIFACT**, which is what `--no-save` is for. `database.tmdl` ships
> with the model and its declared level goes up. A **read-only** consumer — auditing, validating,
> grading fidelity — must pass `--no-save`, or it mutates the very artifact it is judging.
>
> ⚠️ **A refusal must never fall through to the UI-save fallback.** Measured while fixing this: an
> early version *returned* a refusal, `main()` read it as "ImageSave unavailable" and drove Desktop's
> UI instead — which wrote `cache.abf` anyway and rewrote **74 of 89 files**, performing precisely the
> write that had just been refused. Anything that declines to write must stop the pipeline, not hand
> off to a fallback that writes.
>
> ⚠️ **`MainWindowTitle` is a LOADING STATE before it is a verdict — do not read it early.** Without a
> cache the title still reads `Untitled - Power BI Desktop` at t+25 s and only becomes the report name
> by ~t+55 s. Reading at 25 s produced a false "it is broken" on a bundle that was merely still
> opening. **Wait >= 90 s**, and prefer the bridge error over the title.
>
> ⚠️ **Never write TMDL with a BOM.** Desktop's project reader hard-rejects one —
> `UTF8EncodingThrowOnBOM.CheckBom` -> *"Only text with UTF8 encoding without BOM is supported"* — and
> the file does not open. This bit us during the investigation itself: a probe wrote `database.tmdl`
> with `[System.Text.UTF8Encoding]::new($true)` and contaminated a whole test arm. In Python use
> `encoding="utf-8"`; in PowerShell use `-Encoding utf8NoBOM` and never the `Out-File`/`Set-Content`
> defaults. **This is a separate failure from the compatibility mismatch** — same symptom, different
> cause, and only the Desktop crash report ("Frown") distinguishes them. Ask for one rather than
> inferring from the window title.

> ⚠️ **A refresh TIMEOUT is not evidence of a credential modal.** This script used to assert that,
> and it was wrong: measured 2026-08-04 it fired on 2 of 5 Desktop instances opened on the *same*
> bundle that refreshed cleanly on the other 3 (38.8 s on a good run, >87 s on a slow one, against a
> 90 s ceiling — a ~3 s margin). It now reports `REFRESH: TIMEOUT` with the cause **UNKNOWN** and
> names the bundled row-query path (`scripts/probe_desktop_query.py`), not a mutating modal arbiter.
>
> **Timeouts are mode-specific.** The legacy `--no-progress` and
> `--calculate-only` / `--measures-only` paths use a **300 s XMLA ceiling plus 30 s wall-clock grace**.
> Traced full refresh uses a default **3600 s fatal backstop**, retained even if trace setup fails.
> **120 s without progress is a non-fatal liveness warning**, not a termination or source diagnosis.
> Traced settings are exposed by `--refresh-absolute-timeout-seconds` and
> `--progress-liveness-seconds`; the constants and argparse in `scripts/refresh_pbip_model.py` are
> authoritative. A timeout alone leaves the cause unestablished. Inspect the held Desktop locally
> before retrying rather than asserting a dialog or warehouse state from elapsed time.
>
> ⚠️ **Bridge errors that look technical can be a blocking Desktop dialog.** The pair
> `powerbi-desktop status` -> **"Host is not ready to accept operations"** and
> `powerbi-desktop screenshot` -> **"Print metadata is not available"** was measured on an otherwise
> healthy Desktop/bridge when a data-source dialog was already open. Some connector dialogs expose no
> readable text, so the fast check reports either `CREDENTIAL_MISSING` (signature text matched) or one
> of the `DIALOG_*` / `REFRESH_IN_PROGRESS` verdicts (a dialog is up whose content did not positively
> read as harmless). In both cases a human must look at Desktop before the run proceeds. Check this
> first before suspecting the bridge or filing an upstream defect.

### One read-only Desktop observer (#741)

`_desktop_observer.observe(pid, expected_process_start=<held exact UTC tick string>)` returns an
immutable `desktop.observation.v1` snapshot; `watch` owns one invocation deadline, latches and helper
settlement. PID/start/HWND/owner identities are lossless strings in JSON. `_desktop_uia.ps1` supplies
bounded Win32 enumeration and role-bearing UIA facts only. Python owns all root/candidate selection,
signatures, complete-template matching and precedence. No caller classifies UI text.

| Observation | Existing operation adapter | Meaning |
|---|---|---|
| `READY` | no refusal | Complete observation, not operation success or source proof. |
| `CREDENTIAL_REQUIRED` | `CREDENTIAL_MISSING` | Positive credential-specific evidence; a human acts locally. |
| `HUMAN_ACTION_REQUIRED` | `DIALOG_NEEDS_HUMAN` | Act on the prompt visible in Desktop; the remedy is **not** established either. |
| `REFRESH_IN_PROGRESS` | `REFRESH_IN_PROGRESS` | A bounded observation of progress; at preflight **do not stack** a refresh. |
| `UNKNOWN_DIALOG` | `DIALOG_UNRECOGNIZED` | Read but unaccounted-for content; credential state **undetermined in both directions**. |
| `UNREADABLE_DIALOG` | `DIALOG_UNREADABLE` | Content could not be established; credential state **undetermined in both directions**. |
| `DESKTOP_SESSION_ERROR` / `NO_WINDOW` / `PROCESS_GONE` | `DESKTOP_UNREADY` / `DESKTOP_UNREADY` / `DESKTOP_GONE` | Local Desktop state, not a warehouse or credential diagnosis. |

`NATIVE_QUERY_APPROVAL` is reserved; the broad existing prompt signature cannot establish that
specific remedy. Positive prompts outrank progress; incomplete/unknown evidence is latched and cannot
be erased by later quiet polls. Whole-template diagnostics use the original harvest, not a second
read. Name/readonly TextPattern are read; editable/password values are excluded. Short unknown prose
has no amnesty (#406 remains closed). Ambiguous roots exclude nothing; enabled-owner and unowned
zero-area exclusions retain their one-way evidence rules.

Queries refuse immediately and publish results only after all started observation helpers have
settled. Refresh retains nonterminal uncertainty while its own operation may finish. The single
absolute deadline includes native startup/IPC; only owned helpers are killed/reaped. Non-Windows
observation is explicitly unreadable/unsupported, never an empty healthy state. Optional exact-window
images keep the private <=60-second lease and cannot change observation, row or gate outcomes.

The old standalone `probe_desktop_credential.ps1` arbiter is retired. The source repo's root shim
accepts every legacy parameter set but always returns fixed `VERDICT: UNKNOWN`, exit 3, with no
observation or Refresh. No `CREDENTIAL_PRESENT` remains in runtime. Use the existing Python
refresh/query entry points, then inspect the held Desktop locally when they refuse.

> ⚠️ **Do not wait blindly for `NO_BRIDGE` / `not_connected` — bound the bridge wait, then prove the
> executable by PID.** Field report, 2026-08-18, two machines: a box with two Desktop versions
> installed silently launched the **older** one by default, and the Desktop Bridge stayed at
> `NO_BRIDGE` for 10+ minutes with no error. `PBI_DESKTOP_PATH` does **not** fix an already-running
> wrong instance; it is read at launch, not polled. Kill that Desktop by PID, set `PBI_DESKTOP_PATH`
> first, then relaunch.
>
> Use the CLI's bounded wait instead of a human-length cold-start guess:
>
> ```
> powerbi-desktop status --pid <pid> --wait-seconds 90
> ```
>
> Measured in this doc pass with
> `npx --yes @microsoft/powerbi-desktop-bridge-cli status --help`: `--wait-seconds <seconds>` is
> documented by the CLI itself as **"Wait for bridge readiness before returning not_connected"**
> (default `0`). Ninety seconds matches this repo's ~2-minute cap for an unresponsive external
> system. If the bounded wait still returns `not_connected`, treat the likely cause as "wrong Desktop
> build for this machine's bridge", not "keep waiting".
>
> The bridge CLI does **not** expose the Desktop exe path or version. Verified output for a connected
> instance has exactly these fields: `pid`, `bridgeStatus`, `currentFilePath`, `hasUnsavedChanges`,
> `reportDir`, `pages`. So the discovery primitive is OS-level: take the `pid` from `status`, then
> run `Get-CimInstance Win32_Process -Filter "ProcessId=<pid>" | Select-Object CommandLine`.
>
> ⚠️ **MSIX/Store Desktop remains unresolved, not supported or unsupported.** A separate field report
> found that the documented known-good `2.157.627.0` was not installed on a third machine; only a
> classic per-machine build and a Store/MSIX build were present. Do not generalize from that. Open
> [#178](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/178) Defect 2
> reports `refresh_pbip_model.py` refusing an MSIX-launched Desktop with `WRONG_MODEL` because
> pid→model-folder resolution could not follow the Store launch path, while
> `probe_desktop_query._child_port` resolved the same pid fine. That measures a narrow
> identity-resolution gap, not Store bridge compatibility.

> ⚠️ **A `Value.NativeQuery` partition can block an unattended refresh on its own approval modal —
> and this is now a live concern, because migrated custom-SQL sources emit exactly that shape.**
> Power BI Desktop gates native database queries behind *"Permission is required to run this native
> database query"*, controlled by **Options → Global → Security → "Require user approval for new
> native database queries"**
> ([Microsoft Learn](https://learn.microsoft.com/en-us/power-query/native-database-query)).
> Researched 2026-08-19; the parts that decide how you handle it:
>
> | fact | consequence |
> |---|---|
> | The prompt is a **Desktop / mashup-engine** construct. The Service, a gateway refresh and XMLA-triggered refresh never show it. | If the deliverable refreshes in the Service, there is no problem to solve. It is *our* Desktop-bridge pipeline that is exposed. |
> | Approval is keyed to the **exact query text**, not the data source or the file. | A migration that regenerates M re-arms the prompt every time the SQL changes by so much as whitespace. |
> | Approval state is **user- and machine-scoped** and is **not** stored in the PBIP/PBIX. | It does not travel with the artifact. A fresh build agent, or the customer's analyst opening the deliverable, starts unapproved. |
> | `[EnableFolding=true]` does **not** suppress it — the Learn folding page shows the prompt appearing *with* that option set. | Do not expect the emitted options record to help. |
> | `Sql.Database(server, db, [Query="..."])` triggers the **same** prompt. | Switching connector shape is not an escape. |
> | No option in `Value.NativeQuery`'s options record suppresses it. | The only supported off switch is the global setting. |
>
> ⚠️ **The registry recipe you will find online (`DisableNativeDbQueryPrompt = 1` under
> `HKCU\SOFTWARE\Microsoft\Microsoft Power BI Desktop`) is community folklore, not Microsoft-documented
> — and it does NOT apply to an MSIX/Store Desktop.** Measured here 2026-08-19 on Desktop
> 2.157.828.0 installed from the Store: all three candidate keys (`HKCU\SOFTWARE\Microsoft\...`,
> `HKLM\SOFTWARE\Microsoft\...`, `HKCU\SOFTWARE\Policies\Microsoft\...`) are **absent**, because MSIX
> virtualises registry writes into
> `%LOCALAPPDATA%\Packages\Microsoft.MicrosoftPowerBIDesktop_8wekyb3d8bbwe\Settings\settings.dat`.
> So on a Store install, pushing that key does nothing at all and gives you a false sense of having
> fixed it. Verify by observation on **your** install: `reg export` the key, toggle the option in the
> Options UI, export again, diff. On MSIX, set it through the UI once per agent profile.
>
> **If the probe or a refresh stalls on a custom-SQL source, check for this modal before concluding
> anything about credentials.** ✅ **The shared observer reports the broad positive prompt.**
> `scripts/blocking_prompt_signature.regex` recognises `native database quer(y|ies)` /
> `requires your approval`, and the sole Python
> `_desktop_observer.classify_dialog` authority reports **`DIALOG_NEEDS_HUMAN`, exit 3** — checked
> *before* the progress signature, so a refresh dialog in the same window cannot suppress it. Round 3
> of #367's review found exactly that suppression and it exited 0; see the verdict table above.
> Until #376 the Python path reported this as `BLOCKED_BY_DIALOG` at exit 1, i.e. as a sign-in wall,
> which is the wrong remedy: this needs an **approval**, not an account.

Read-only preflight (proves credentials + source reachability without changing anything):

```
python scripts/probe_desktop_query.py [--pid <pbidesktop-pid>] [--canaries "A" "B"]
```

Use a model with a persisted `.pbi/cache.abf` for Desktop/bridge smoke tests. A PBIP whose live SQL
Server source does not exist in the current tenant will always open behind a credential dialog and is
a bad unattended smoke-test fixture, no matter how well the bridge itself is working.

**Name one canary table per distinct live source** with `--canaries`. A model-level `DATA_OK` is only
emitted when *every* named canary returns rows; with **no** table named, only the first queryable
table is probed and the verdict is **downgraded** to `TABLE_OK '<table>'` — a static parameter/CSV
table can return rows while a live source never loaded, so one arbitrary table can never certify the
whole model. This mirrors the `powerbi-semantic-model-gotchas` rule: *prove a REAL read per live
source*.

⚠️ **`--canaries` verifies; `--tables` narrows the refresh. They are different knobs, on purpose.**
They used to be one, and that built a trap: the only documented way to earn `DATA_OK` was to name
tables in `--tables`, which simultaneously refreshed **only** those tables — certifying a *model*-level
verdict over a *partially* refreshed model, i.e. the "next agent gets an empty model" failure this
bundle exists to prevent, wearing a green badge. So `refresh_pbip_model.py` now emits
`TABLES_OK '<a>', '<b>'` — never `DATA_OK` — whenever `--tables` narrowed the refresh, and
`--canaries` verifies without narrowing anything. `--tables` alone still supplies the canary set (old
callers keep a probe), it just can no longer certify the whole model. `probe_desktop_query.py` never
refreshes, so there `--tables`/`--table` remain plain aliases for `--canaries`.

Both print one authoritative machine-readable verdict line: `REFRESH: DATA_OK + PERSISTED` / `TABLES_OK '<a>', '<b>'` /
`TABLE_OK '<table>'` / `NO_DATA` / `NOT_PERSISTED` / `WRONG_MODEL` / `ERROR <msg>`, and
`PREFLIGHT: DATA_OK` / `TABLE_OK '<table>'` / `NO_DATA` / `ERROR`. Exit 0 is the good outcome — but it
covers a model verdict (`DATA_OK`), a scoped verdict (`TABLES_OK`) **and** a single-table verdict
(`TABLE_OK`), so a gate that needs model-level certainty must require the literal `DATA_OK` — which
means running a **whole-model** refresh with explicit `--canaries`, not merely exit 0.

### Read persistence output as an observation, not a durability guarantee (#641)

✅ **Reporting controls:** `tests/test_desktop_instance_binding.py` exercises the real legacy AMO
tuple producer, UIA outcomes, and unchanged versus externally advanced cache timestamps. Each
completed invocation emits exactly one authoritative `REFRESH:` verdict; `save` and `cache` lines
explain it rather than issuing competing verdicts.

| Condition | Human-readable report | Terminal compatibility behavior |
|---|---|---|
| AMO helper returns true | `save   : AMO ImageSave attempt completed; legacy cache-update verification pending` | Final verdict decided later, not by the helper Boolean |
| UIA helper returns true | Existing `UI Automation save attempted (...; verification pending)` variant | Final verdict decided later |
| `--verify-only` | `cache  : persistence not requested (--verify-only; no refresh or save was run)` | Existing data/scope token, no persistence suffix |
| `--no-save` | `cache  : persistence not requested (--no-save; refreshed data remains in memory only)` | Existing data/scope token, no persistence suffix |
| Requested save; timestamp advanced | `cache  : legacy timestamp check observed a newer cache -> <path> (cold reopen not tested)` | Existing `+ PERSISTED` suffix |
| Requested save; timestamp unchanged | `cache  : legacy timestamp check did not observe a newer cache (write outcome unconfirmed)` | `REFRESH: NOT_PERSISTED (legacy timestamp check did not observe a newer cache.abf; write outcome unconfirmed)` when the data check passes |
| Save helper fails | Factual helper failure; no prediction about the next open or old cache | One `REFRESH: NOT_PERSISTED (save helper failed; write outcome unconfirmed)` |

**The legacy `after_stamp > before_stamp` predicate, data/scope tokens, `NO_DATA` precedence and
exit codes are unchanged.** `+ PERSISTED` is a compatibility label for that timestamp observation,
not durable-write or cold-reopen proof. Typed image observations still carry
**`commitment: UNESTABLISHED`**; this display does not upgrade them. Cold-reopen qualification
remains separate (#149).

**A manual save is not verified by `--verify-only`.** That flag queries in-memory data and runs no
refresh or save. Inspect Desktop's save state after a UIA failure; a subsequent verify-only data
verdict says nothing about whether that save landed. With either no-persistence flag, external
changes to cache bytes or metadata are never attributed to this invocation. If both flags are
present, `--verify-only` describes the run: no refresh and no save.

## Order matters, do not refresh before you finish editing

Desktop **discards `cache.abf` when the model definition is newer than the cache**. Verified: a model
whose `definition/*.tmdl` were touched after the cache was written opened `NO_DATA` despite a 113 KB
cache sitting right there. So:

> make **all** TMDL edits, **reopen Desktop so it loads them**, then refresh, then save.

That middle step is not optional, and it is the easiest one to drop: `powerbi-desktop reload` does
**not** re-read edited TMDL (measured below). Refresh without reopening and you refresh the *old*
in-memory definition, then persist a cache that does not match what is on disk — which opens
`NO_DATA` and looks like the cache-invalidation problem rather than the operator error it is.

Anything that rewrites TMDL afterwards invalidates it, including the host repo's own sanitize step
(here, `set_data_folder.py --sanitize`, which must run before committing). If you sanitize last, you
have thrown the cache away; re-run this script after.

**A cache newer than the definition can still be invalid — you cannot win this by ordering.** Measured
2026-08-01 on `logistics-live-dbx`: sanitize rewrote `expressions.tmdl` at 12:24:08, `ImageSave` wrote
`cache.abf` at 12:24:23 (15 s *newer*, 57.5 KB) — and a cold reopen still came back `NO_DATA`.
Re-localizing, reopening, refreshing and saving again produced a cache that **did** survive a
`Stop-Process -Force` + reopen (`PREFLIGHT: DATA_OK`, no refresh). So "refresh last, after sanitize"
does not rescue the cache: Desktop keys the cache to the definition it was built from.

⚠️ **Scope this measurement honestly.** It changed `expressions.tmdl` — a *data-affecting* file.
Whether a culture-only edit (AI instructions), a description-only `ExportToTmdlFolder` or a
byte-identical rewrite also invalidates the cache is **UNMEASURED**; do not cite this paragraph for
those. What it does establish — and all the warning below needs — is that a favourable mtime proves
nothing.

> ⚠️ **Do NOT gate on `cache.abf` mtime ≥ newest definition-file mtime.** Proposed independently on
> 2026-08-19 by a field team hitting the post-ship-edit case above, and it is a **false-green
> generator**: the measurement in the paragraph above is precisely a cache that was 15 s *newer* than
> the definition and still opened `NO_DATA`. A timestamp comparison passes exactly the build it needs
> to catch. The concern behind it is real — a post-ship polish edit (a caption, a reference line, a
> textbox resize) silently invalidates an already-verified refresh — but the only sound gate is
> behavioural: **after any TMDL rewrite, re-run refresh + persist, then prove it with a cold reopen**
> (`Stop-Process -Id <literal pid> -Force`, reopen, `PREFLIGHT: DATA_OK` with no refresh). Compare
> behaviour, never timestamps.

> **Hand-off rule:** leave the model **localized + refreshed**. Persisting is the default and safe —
> the compatibility alignment above keeps the cache loadable — but a persisted cache only *survives*
> a hand-off when nothing rewrites the TMDL afterwards, and the committer still has to run the
> sanitize step, which invalidates it. So either persist and tell whoever commits to re-run this
> script after sanitizing, or hand off refreshed-but-not-persisted with `--no-save` and have them
> refresh once more post-sanitize. ⚠️ **Revised 2026-08-04, corrected 2026-08-07:** an earlier note
> here blamed *persisting* for making the PBIP unopenable and told you to persist only with a flag;
> root-caused, that was a compatibility-level mismatch `image_save` now aligns away, not persistence
> itself, so persisting is the safe default again.

**`powerbi-desktop reload` does NOT re-read edited TMDL.** Measured 2026-08-01: after editing two
measures on disk, `reload` returned `{"success": true}` and `INFO.MEASURES()` still showed the **old**
expressions — the reload refreshes the report, not the model definition. Only closing Desktop
(`Stop-Process -Id <literal pid> -Force`) and reopening the `.pbip` picks up a model change. This is
easy to miss precisely because the reload reports success, so **verify a model edit landed** with
`EVALUATE SELECTCOLUMNS(INFO.MEASURES(), "Name", [Name], "Expr", [Expression])` before trusting any
number you read back.

⚠️ **A stale Desktop session can also SAVE the old model back over same-day file edits.** Customer
report: Hemang Patel (SES), 2026-08-21, found a verified fix silently reverted overnight, with every
file in the affected workbook's `.Report` and `.SemanticModel` folders sharing one timestamp burst.
Measured by us the same day: while Desktop held the pre-edit in-memory model, a disk-only sentinel
measure was added to TMDL. `powerbi-desktop reload` returned success and **did not overwrite** the
file; `refresh_pbip_model.py --pid` **refused safely** with `REFRESH: WRONG_MODEL`, exit 2; Desktop's
own UI **Save** overwrote the file silently, removed the sentinel, and reverted the TMDL hash exactly
to the pre-edit value.

**Forensic signature:** many files in one workbook folder sharing a single modified time to the
second. Hemang's customer evidence showed ~30+ files rewritten together; our fixture-scale
reproduction rewrote all 18 files under `.Report` + `.SemanticModel` in one burst (15 at
`2026-08-21 12:10:52`, 3 at `12:10:53`). That signature is how to diagnose this after the fact when
no Desktop process remains to inspect.

**Boundary of the measurement:** we reproduced an immediate, explicit Desktop UI Save. We did **not**
reproduce an overnight/idle autosave, and we did **not** evaluate the "Apply external changes" prompt.
Do not cite this as proof of either. The operational rule is still binding: **if you edited files on
disk, do not Save from a Desktop instance that was already open before the edit; close and reopen it
first.** The guarded script path is safer than the human Save path precisely because it fails closed
on `WRONG_MODEL` instead of writing stale state.

## How persistence actually works

| Path | What it is | Status |
|---|---|---|
| **AMO `Server.ImageSave(databaseId, Stream)`** | Writes `cache.abf` directly from the engine. No UI, works headless; staged to a per-run private file and swapped in atomically, so a failed/partial write (or a concurrent second run) can't destroy an existing good cache. | **Default (opt out with `--no-save`)** |
| **UI Automation (`InvokePattern` on the named "Save" element)** | Drives Desktop's own Save through the Windows *accessibility* tree. | Legacy fallback / `--ui-save`; also the optional save after explicit `--ui-refresh` |

⚠️ **UI Save can rewrite model definitions.** Desktop Save may update serialized definitions,
inferred data types, and auto-date scaffolding in addition to writing `cache.abf`; it is not
byte-neutral. The `--ui-refresh` route therefore saves only after its engine and canary checks, and
its Invoke is never described as committed persistence or cold-reopen proof.

⚠️ **Read the `--no-save` warning above before using either.** Both write the same `cache.abf`. A
present cache was once believed to make the PBIP unopenable; root-caused 2026-08-07, that was a
compatibility-level **mismatch**, which `image_save` now prevents by aligning `database.tmdl` (and
rolling that alignment back if the write fails). With the hazard gone, persisting is the DEFAULT and
matches this script's stated purpose; `--no-save` opts out for read-only or validate-then-deploy work.

**Why ImageSave exists at all**, because the obvious answer says it should not: the guidance is that
writing model state back to `.pbip`/`cache.abf` "is a separate Save action that only the Desktop UI
performs". That is true of the MCP/Bridge surface but **not of the engine**. A TMSL `backup` is
refused only because Desktop runs Analysis Services in **Diskless mode** ("Backup/Restore ... not
supported"), and that same configuration sets `EnableDisklessTMImageSave=1`. AMO exposes the matching
call, and it works. Proven end to end with the control that rules out a silent re-refresh: delete
`cache.abf`, refresh in memory, `ImageSave`, **kill Desktop with `-Force`** (no save prompt),
**rename the source data folder away**, reopen, `DATA_OK` with real rows. With the source absent, that
data can only have come from the cache.

Two traps:

- The AMO client raises **"The server sent an unrecognizable response"** *while writing the file
  correctly*. Judge success by the FILE, never by the absence of an exception — but "the file" means a
  **complete** ABF: only that specific benign response is tolerated, and the staged file is swapped in
  only after its **block chain** walks cleanly to EOF. A `cache.abf` is an Analysis Services backup,
  **not** a Compound File / OLE2 container — an earlier round asserted CFBF and its check therefore
  rejected 100% of real caches, silently disabling persist-by-default on every run. Measured across 13
  real caches (17 KB → 60 MB, written by Desktop *and* by `ImageSave`): a 100-byte UTF-16LE
  `"This backup was created using XPress9 compression."` preamble, a 2-byte pad, then blocks of
  `uint32 uncompressed, uint32 lengthFromTheMagic, 4-byte magic 2A D7 86 4E, payload`, where the next
  header sits at `offset + 8 + length` and the chain ends **exactly** at EOF. A truncated write leaves a
  block claiming bytes past EOF; a write that stopped on a chunk boundary leaves a full-2 MiB *final*
  block (every measured final block is smaller), so both are rejected instead of replacing a good cache.
  The check **fails closed** on any uncertainty and **prints the reason** it refused — a predicate that
  can only say "no" is how the CFBF mistake survived a whole review round. Any other exception propagates and
  the provisional compatibility-level alignment is rolled back; if that rollback cannot itself be
  completed the run **stops fatally** rather than falling through to the UI Save, because
  `database.tmdl` would otherwise ship declaring a level no cache was ever written at.
- `database_operations ExportToTmdlFolder` persists model *definition* changes but carries no rows.
  It cannot substitute for this.

UIA is a **workaround, not a contract**: an accessibility surface is not an automation API. It depends
on an element literally named "Save", so it breaks on ribbon changes and non-English installs, cannot
run headless, and a modal dialog swallows the invoke silently. It is not SendKeys, because
`SetForegroundWindow` is refused in this context, so a Ctrl+S keystroke lands on whatever window has
focus while the model stays dirty. Its Boolean reports an attempt after observing the bridge's
`hasUnsavedChanges` flag as false, not persisted success; the later legacy timestamp check controls
the compatibility verdict described above. If a real `save` verb ever ships, delete it.

## Bind to the right instance, or you will corrupt a sibling

The destination is resolved from the **pid**; the data comes from whatever instance answers on the
**port**. If those disagree (one bad `--port`, or a port lookup that widens to "any `msmdsrv` on the
machine") this writes *another migration's model into your own correct-looking `cache.abf`*, and every
downstream signal still agrees: the file exists, is non-empty, is newly written, and the row count
queries that same wrong port.

So, before refreshing, querying or saving anything, two guards run. First, **if you pass `--port` it
must EQUAL the port derived from `--pid`** — a mismatch aborts, so `--port` can never bypass PID-based
discovery to point the read (and the `ImageSave` write) at another instance. Second, `same_model()`
compares the connected model against the TMDL that owns the destination cache and requires an **exact
match of tables, columns AND measures** — table names alone are too coarse (a bound sibling with the
same table names but different columns or measures would pass), so the fingerprint descends into each
table's columns and the model's measures, filtering only the auto-generated noise that never appears in
TMDL (RowNumber system columns, `LocalDateTable_*` / `DateTableTemplate_*` auto-date tables). A
mismatch — or an identity it cannot establish at all (no model folder resolved, or no TMDL to
fingerprint) — aborts with `WRONG_MODEL`, **failing closed** rather than assuming it is fine. A superset
schema (all your tables plus more) is a mismatch, not a "confirmed". File metadata fundamentally cannot
tell you whose rows are in a blob, only the model's own contents can. When a project folder holds
several `.SemanticModel` directories the destination is resolved from the `.pbip` → report →
`definition.pbir` `byPath` **binding first**; only if no binding resolves does a single same-named
sibling act as a last-resort fallback — the binding is authoritative and the name heuristic can never
short-circuit it. In a parallel batch **always pass `--pid`** (`powerbi-desktop status` maps pid to open
file); with several instances open the scripts refuse to guess.

**Sweep for orphans before you build, not just siblings while you build.** Measured 2026-08-01: a
Desktop instance was already running on the *exact `.pbip` path* about to be generated, left over from
an earlier, since-deleted attempt. The tables-only fingerprint then in force would not have caught it —
its TMDL tables matched — though the columns+measures fingerprint now would, because `INFO.MEASURES()`
showed measures the new build never defines. But do not lean on that: an orphan whose schema is
byte-for-byte your model (the common re-open case) is **identical** to `same_model()`, one Save away
from overwriting the files you are generating. So at the start of a build, list `Get-Process
PBIDesktop`, read each one's command line
(`Get-CimInstance Win32_Process -Filter "ProcessId=<pid>"`), and force-close any instance already bound
to your own `.pbip` before writing to it. Identify by `MainWindowTitle` + command line, never by "the
one instance that is running".

## Reusing this in another migration repo

Nothing here is Tableau-specific, the input is already a Power BI model. **Copy this folder.**
`SKILL.md`, the query/refresh scripts, `_desktop_observer.py`, `_desktop_uia.ps1`, four regex resources
and bundled tests are one portable unit. The Python siblings import no host-repo modules;
`tests/conftest.py` resolves them at `../scripts`, so the
suite — and every runtime instruction the scripts print — runs from wherever the folder lands. Drop
it in the target repo's skill location (`.github/skills/`) or promote it to a global one such as
`~/.copilot/skills/`, then prove the copy on the spot:

```
pytest tests
```

The one repo-bound fixture degrades honestly: the TMDL-fingerprint test that reads this repo's
`examples/*/fabric/*.SemanticModel` corpus **skips with a reason** when there is no such tree, rather
than failing or silently collecting nothing.

In the source repo that claim is a CI gate, not a sentence:
`tests/test_skills.py::test_a_bundled_skill_passes_its_own_tests_after_being_copied_out_of_this_repo`
copies this folder to a temp directory and runs `pytest tests` there with the repo root out of
`sys.path`. A new `import <something-repo-local>` fails there, before it can fail in your repo.

Two things do **not** travel, by design:

- `scripts/probe_desktop_query.py` and `scripts/refresh_pbip_model.py` at the *source repo's* root
  are forwarding shims for callers that predate the move. Do not copy them.
- `refresh_pbip_model.py` warns about this repo's `set_data_folder.py --sanitize`. Read that as
  "any post-processing that rewrites TMDL", and substitute your own.

No `allowed-tools` in the frontmatter, deliberately. Pre-approving `shell` would save one
confirmation per run, and GitHub's warning is the reason it is not worth it: pre-approving `shell` or
`bash` *"removes the confirmation step for running terminal commands and can allow attacker-controlled
skills or prompt injections to execute arbitrary commands in your environment"*
([docs](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/add-skills)).
These scripts spawn PowerShell, load .NET assemblies and write a binary cache file, so they are
exactly the shape that warning is about. The field is also marked experimental in the spec, with
support varying by runtime. Revisit only with a reason.
