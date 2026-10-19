# Desktop observer simplification — Refs #741

**Implementation note:** sections 1–11 preserve the pre-implementation investigation and estimates.
The approved amendments and implemented slice-1 contract in §12 supersede the proposed identity types,
timestamps and API examples below. The historical census is not rewritten to look like the new tree.

## 1. Executive recommendation
⚠️ **Recommend a Python observer in the portable refresh skill, with one bounded, harvest-only PowerShell backend.** Retire the standalone refresh-clicking credential arbiter; consolidate classification, observation scheduling, diagnostics and private-image lifecycle, not source proof.
Land observation/caller contracts first. Modern CLI-first reload is a subsequent implementation choice, conditional on #729/#730; the `t2p` command is a thin presentation adapter conditional on #734.
✅ Static investigation of tree `524ecc1` (the current branch's product baseline), issue #741 and related discussions; no Desktop, database, screenshots or migration execution. ✅ means inspected evidence; ⚠️ means proposed design/estimate, not a demonstrated implementation.
Citation shorthand: `S/` = `.github/skills/pbip-model-refresh/scripts/`; `T/` = `.github/skills/pbip-model-refresh/tests/`. Other citations are repository-relative `path:line` ranges.

## 2. Before census
✅ Physical lines include comments/blanks; Python function counts include methods/nested functions via AST; PowerShell counts named `function` declarations, not embedded C# methods. Test counts are `def test_*` definitions, **not parametrized executions**, Windows passes or deletion candidates.

| Product responsibility | File:lines | Lines / functions |
|---|---|---:|
| Window identity, classification, diagnosis, polling, private images | `S/_credential_modal.py:1-2066` | 2,066 / 84 |
| Parallel classifier, UIA harvesting, Refresh invocation, arbiter | `S/probe_desktop_credential.ps1:1-952` | 952 / 16 |
| AS identity/discovery, DAX canaries, observation workers, prompt adaptation | `S/probe_desktop_query.py:1-924` | 924 / 47 |
| XMLA/AMO refresh, prompt adaptation, persistence, schema identity | `S/refresh_pbip_model.py:1-2229` | 2,229 / 74 |
| Shared pattern data, read by both classifiers | `S/{credential_modal_signature,blocking_prompt_signature,benign_dialog_signature,benign_chrome_signature}.regex:1` | 4 / 0 |
| Probe construction/orchestration, source errors, lifecycle, earned proof | `scripts/probe_live_source.py:1-1260` | 1,260 / 35 |
| Child text-verdict recognition, precedence and table scoping | `scripts/_verdict_lines.py:1-300` | 300 / 9 |
| Stable page capture, optional A1 preparation, receipts | `scripts/capture_powerbi_pages.py:1-672` | 672 / 27 |
| Persistent read-only DAX protocol, numeric conversion, Desktop refusals | `scripts/dax_oracle_server.py:1-344` | 344 / 13 |
| Physical ACL gate, source-key audit, package data-access assessment | `scripts/credential_gate.py:1-2538` | 2,538 / 110 |
| **Requested surface total** | File-level inventory, not all observation code | **11,289 / 415** |

✅ Adjacent retained machinery: `S/_verdict.py:1-126` (126/3), `S/_abf.py:1-342` (342/11), `S/_lock.py:1-228` (228/15); `scripts/_probe_pbip.py:1-185` (185/3), `scripts/_gate_lift.py:1-96` (96/2), forwarding shims `scripts/{probe_desktop_query,refresh_pbip_model}.py:1-35` (70/2), `scripts/iteration_receipt.py:1-1349` (1,349/61). These are not disposable observer duplication.
✅ Additional slice-1 consumers, outside the requested-surface total: root `scripts/probe_desktop_credential.ps1:1-32` forwards the retiring command; `T/conftest.py:113-161` imports `CredentialDetection` and patches both old inspection entry points; `T/test_canary_data_gate.py:351-406,594-620` directly exercises those seams. Receipt refusal vocabulary/schema is owned by `scripts/iteration_receipt.py:67-78,264-279`, with version/reader controls in `tests/test_iteration_receipt.py:1285-1486`. These are compatibility/harness obligations, **not deletion candidates**.

| Direct tests by responsibility | Files (line counts) | Definitions |
|---|---|---:|
| Classifiers, native/test-owned windows, diagnostics, images, portable copy | `T/test_credential_modal_detection.py:1-6387` | 176 |
| Desktop/AS/catalogue binding and observation workers | `T/test_desktop_instance_binding.py:1-2079` | 76 |
| Refresh budgets, progress tracing and polling | `T/test_refresh_wall_clock.py:1-1264` | 44 |
| Canary scope; persistence/locking; assembly discovery | `T/test_canary_data_gate.py:1-620`; `T/test_persist_atomicity.py:1-2124`; `T/test_nuget_cache_resolution.py:1-50` | 30 + 60 + 3 |
| Probe connectors; verdicts; custom SQL; quoting; earned clears | `tests/test_probe_live_source.py:1-264`; `tests/test_probe_live_source_verdict.py:1-1163`; `tests/test_probe_live_custom_sql.py:1-454`; `tests/test_probe_live_tmdl_quoting.py:1-182`; `tests/test_probe_earned_clear.py:1-1275` | 11 + 33 + 25 + 11 + 33 |
| Page capture/A1; oracle execution; retained iteration authority | `tests/test_capture_powerbi_pages.py:1-943`; `tests/test_dax_oracle_server.py:1-429`; `tests/test_iteration_receipt.py:1-1486` | 32 + 26 + 79 |
| Gate proof/ACLs/scope/shield/target verification | `tests/test_credential_gate.py:1-3989`; `tests/test_credential_gate_acl_layouts.py:1-540`; `tests/test_credential_gate_scope.py:1-89`; `tests/test_credential_gate_shield.py:1-159`; `tests/test_credential_gate_verify_target.py:1-66` | 162 + 19 + 8 + 7 + 3 |
| **Direct-suite total** | **23,563 physical lines** | **838** |

✅ Integration halo is additional, not counted/deletable here: `tests/test_skills.py:1-573`, `tests/test_parallel_test_loop.py:1-576`, package/data-access/preflight/orphan suites. Counts above do not imply 838 tests classify dialogs.

### Every observation / waiting / translation owner in the requested surface
| Responsibility | Current owners and evidence |
|---|---|
| Window candidates, ownership, roots | Python `main_frame`, `dialog_candidates`, enumeration: `S/_credential_modal.py:505-638,700-894`; PowerShell candidate selection, Win32 enumerator and PID enrichment: `S/probe_desktop_credential.ps1:253-268,674-852`. Neither first/largest window nor `MainWindowHandle` is identity (`S/_credential_modal.py:524-554`). |
| Window text / UIA | Python Win32 child text: `S/_credential_modal.py:733-830`; exact-HWND diagnostic subprocess/payload validation: `:965-1038`; PS `Get-AutomationHarvest`, role/password filtering and target checks: `S/probe_desktop_credential.ps1:538-672`; bounded harvesting/enrichment: `:768-838`. |
| Prompt/progress classification | Python normalization/signatures/completeness/classifier/fold: `S/_credential_modal.py:315-698`; PS equivalents and guidance: `S/probe_desktop_credential.ps1:187-536`; four regex files above; Python whole-dialog diagnostic templates: `S/_credential_modal.py:915-962`. |
| Waits, latches, timeouts | Shared Python worker join/latches: `S/_credential_modal.py:1098-1114,1239-1381`; image workers: `:1387-2066`; PS harvest child caps 2–8s and 75s probe/2s polling: `S/probe_desktop_credential.ps1:146-149,765-803,921-952`. |
| Query waits/adapters | Identity subprocess 30s, bounded observation calls ≤120s, port discovery 6 × 2s: `S/probe_desktop_query.py:82-87,208-438,504-572`; legacy query 5s poll **without overall deadline**: `:758-880`; its emitters: `:771-837`. |
| Refresh waits/adapters | `S/refresh_pbip_model.py:204-250,256-383,399-521`: 300s XMLA +30s grace, 5s dialog polls; progress-mode 120s liveness warning/3,600s backstop and separate wait branch; trace handling `:911-1180`; save/dirty wait 120s `:1576-1629`; persistence lock 120s `:1336-1454`; final translation `:1922-2040`. |
| Residual mutating UIA / lock waits | Save fallback selects the first PID UIA root child and invokes Save, then polls bridge dirty state (`S/refresh_pbip_model.py:1576-1629`); its invocation subprocess and `_bridge_status` (`:799-812`) have no explicit timeout. This is a persistence mutator, **not part of the read-only observer**; retain separately and never reuse its first-window selection for diagnosis. Lock acquisition/reclaim polling remains `S/_lock.py:127-161,176-215`. |
| Probe lifecycle/budgets | `scripts/probe_live_source.py:480-516,637-698`: bridge status 60s/open 240s, 20 × 2s identity retry; PID listing/close subprocesses have no explicit timeout. Catalogue 240s loop/3s sleep invokes an unbounded query child (`:701-747`); refresh parent 390s derives from child +60s, disables progress (`:118-147,765-844`). |
| Probe verdict translation | `scripts/_verdict_lines.py:20-300` plus connector/error marker ordering `scripts/probe_live_source.py:149-201,381-477`; final directives/audit/source aggregation `:847-905,975-1121`. These classify source/operation outcomes as well as dialog tokens. |
| Images / capture waits | Exact-window GDI/DPI/PNG/private-file checks, 8s acquisition/60s lifetime and cleanup: `S/_credential_modal.py:1387-2066`; page bridge capture 90s wait/120s child, frame dwell/poll/max-wait: `scripts/capture_powerbi_pages.py:65-67,123-145,188-227`; terminal diagnosis `:87-99,280-282`. |
| Other consumers | Oracle duplicates state→refusal mapping at connection-open and each ExecuteReader: `scripts/dax_oracle_server.py:169-210` (no command deadline set there). A1 holds identity across refresh/canaries/persist: `scripts/capture_powerbi_pages.py:330-412`; status/reload child cap 60s: `scripts/iteration_receipt.py:567-605`. |
| Receipt / harness / operator consumers | Receipt reason schema and direct bundled fixtures above must migrate with caller contracts. Root PS shim forwards arguments/exit (`scripts/probe_desktop_credential.ps1:24-32`). Operational arbiter/verdict guidance: `docs/data-source-credentials.md:84-123,250-257`, `docs/credential-gate-testing.md:90-106`; independent UIA/modal recipes: `.github/skills/powerbi-semantic-model-gotchas/SKILL.md:1088-1133`. Historical test naming/measurements need review, not erasure (`docs/parallel-test-loop.md:64-98`). |

## 3. Authority boundary
✅ The real source path is `build_m_query` → writable one-table PBIP → Desktop refresh → scoped row query (`scripts/probe_live_source.py:271-370,1182-1214`; `S/probe_desktop_query.py:683-717`). A cached arbitrary-table canary, successful reload, UIA result or image is **not fresh live-source coverage**.
✅ Preserve zero exit + correct-table success + absence of conflicting non-success (`scripts/_verdict_lines.py:154-179,263-300`), exact live-leg coverage/audit (`scripts/probe_live_source.py:975-1025`), and earned-clear verification (`scripts/_gate_lift.py:48-96`; `scripts/credential_gate.py:1016-1061`).
✅ `_probe` is deliberately writable and excluded from artifact auditing; denied model/report anchors stay locked (`scripts/credential_gate.py:440-480,720-725`). No proposed alternative removes the sandbox, relaxes ACLs or lets observer/operator testimony emit probe-earned `DATA_OK`.
⚠️ Ownership remains explicit: **portable Python observer → observations; bounded PowerShell child → harvested facts; caller → operation outcome; one-row query → DATA_OK; credential gate → physical clear**. `_probe` stays writable; images remain private/non-authoritative; #729/#730 gate reload deletion; #734 only presents observer results.

## 4. Alternatives comparison
⚠️ All estimates are **net**, including replacement code/tests, relative to the census; moving/renaming lines earns zero deletion. Each option preserves §3 and the private evidence contract in §5 unless explicitly describing today's behavior.

| Alternative | Exact product-file delta and callers | Contract / portability / process model | Expected deletion; risk and failure direction |
|---|---|---|---|
| **A. Python authority — recommended** | Rename `S/_credential_modal.py` → `S/_desktop_observer.py`, PS arbiter → `S/_desktop_uia.ps1`; adapt both bundled callers, `scripts/{probe_live_source,_verdict_lines,capture_powerbi_pages,dax_oracle_server,iteration_receipt}.py` and root `scripts/probe_desktop_credential.ps1`; mandatory tests/docs in slice 1. Keep four regexes, Python shims, gate and proof modules. | §5 JSON/vocabulary; PS returns only private harvest facts. Entire implementation/tests travel with skill; Windows backend required, non-Windows returns explicit unsupported/unreadable. One observer supervisor bounds/owns harvest/image children; operation deadline supplied by caller. | **Unverified hypothesis only: ~650–1,100 product lines, 1,000–2,200 test lines / 15–35 definitions net**; an implementation diff is needed, and new contracts may erase savings. Demonstrated gross policy/arbiter candidates are only ~449 PS lines (§7). Risk: changed precedence/latches; invalid/incomplete harvest fails closed. |
| **B. PowerShell authority** | Rename arbiter → `S/_desktop_observer.ps1`, replace `S/_credential_modal.py` with thin `_desktop_observer.py` JSON adapter; same eight caller/compatibility/schema files and mandatory test/docs surface as A. Remove Python classifier/template interpretation; retain four regexes, move complete identity/role/template policy into PS. | Same §5 states/JSON; Python never classifies UI text. PS owns collection + classification + helper-child caps, Python watchdog bounds PS. Portable bundle still Windows-only backend; offline policy tests now need PowerShell or recorded-contract fixtures. Images still §5, using the Python native image helper as subordinate, not another classifier. | **Unverified hypothesis: ~400–900 product / 700–1,800 test lines, 10–30 definitions net**. Less Python policy, more cross-process/schema plumbing; new PS image orchestration may erase savings. Hung provider/invalid JSON → unreadable, never READY; two language supervisors remain. |
| **C. Modern CLI-first refresh** | **A plus**, after measurement: reduce `scripts/probe_live_source.py` open/status/workaround flow; change `scripts/capture_powerbi_pages.py`, `scripts/iteration_receipt.py`, `S/refresh_pbip_model.py`; revise three skill guides listed in slice 3. Keep observer/query/proof/ABF/lock/gate. | Same observer JSON, supplemental to upstream structured preview/processing output. Supported public CLI resolves folder, **no public `--pid` or direct host bypass**; observer still verifies exact internally resolved PID/HWND. Requires #729 floor and #730 measured binding/reload/processing. Bound owned CLI children; no fallback into old automatic retry loops. | A savings plus **0–300 product / 0–600 test lines, 0–10 definitions**, contingent, not additive replacement of the 2,229-line refresh module. Risk: wrong instance, unsaved-change loss, reload mistaken for processing; ambiguous identity/failed reload stops. |
| **D. No change** | Change/delete no product or test files; every caller retains census behavior. | Existing `CredentialDetection`, `DIALOG_*`, `CREDENTIAL_PRESENT`, REFRESH/PREFLIGHT/PROBE text families; portable skill with Windows PS/CLR, nested budgets and inconsistent waits remain. Existing optional 60s image lease remains; no new unified JSON. | **0** deletion. No migration regression from refactoring, but unreadable/progress/misrouting and unbounded-wait debt remains; failure direction is mixed local diagnostic refusal/misclassification, while row-proof gate must remain closed. |

✅ Upstream guidance, checked 2026-10-16: [`preview-part-04.md:11-48`](https://github.com/microsoft/skills-for-fabric/blob/main/skills/powerbi-report-cli/references/authoring/preview-part-04.md#L11-L48) separates reload from Calculate/targeted Import Full/storage-mode processing, requires explicit `hasUnsavedChanges: false`, serialized operations and DAX verification; [`skills/powerbi-report-cli/references/authoring-part-03.md:170-182`](https://github.com/microsoft/skills-for-fabric/blob/main/skills/powerbi-report-cli/references/authoring-part-03.md#L170-L182) forbids public PID/host bypass (no extra `authoring/` directory in this URL). These are guidance, **not this repo's measured success**.
✅ Older repo claims remain at `.github/skills/pbip-model-refresh/SKILL.md:761-830,896-910` and `.github/skills/powerbi-report-gotchas/SKILL.md:641-650`; #730 owns reconciling them, not observer extraction.

## 5. Recommended observer API / JSON / states
⚠️ `observe(pid, *, hwnd=None, timeout_s=8, evidence=None)` returns one immutable snapshot; `watch(pid, *, deadline, operation_id, evidence=None)` yields snapshots with one shared latch/scheduling implementation. Observer is read-only against Desktop: no invoke/focus/restore/refresh/query/save/close/gate write.
⚠️ Schema `desktop.observation.v1`, closed fields: `schema`, `observation_id`, `operation_id` (nullable), `pid`, `process_started_at`, `hwnd` (nullable), `owner_hwnd` (nullable), `observed_at`, `elapsed_ms`, `complete`, `state`, `reason`, `findings`, `image`, `cannot_prove:["source_reachability","credential_validity","data_correctness"]`.
⚠️ Types: PID positive integer (not bool); HWND/owner canonical hex strings or null; UTC timestamps; finite nonnegative elapsed milliseconds; strict boolean completeness; bounded strings/arrays; closed reason/message/image-status enums. Reject extra/duplicate keys, unsupported schema, identity mismatch and oversized payload before interpreting evidence. Snapshot exit 0 means valid observation, **not READY or DATA_OK**; invalid invocation/backend transport exits nonzero with a closed unreadable reason, never caller text scanning.
⚠️ Example payload (synthetic, not captured evidence):
```json
{"schema":"desktop.observation.v1","observation_id":"example","operation_id":null,"pid":1234,"process_started_at":"2026-10-16T00:00:00Z","hwnd":"0x123","owner_hwnd":null,"observed_at":"2026-10-16T00:00:01Z","elapsed_ms":8,"complete":false,"state":"UNREADABLE_DIALOG","reason":"truncated","findings":[],"image":null,"cannot_prove":["source_reachability","credential_validity","data_correctness"]}
```
⚠️ State enum: `READY`, `REFRESH_IN_PROGRESS`, `CREDENTIAL_REQUIRED`, `NATIVE_QUERY_APPROVAL`, `HUMAN_ACTION_REQUIRED`, `DESKTOP_SESSION_ERROR`, `UNKNOWN_DIALOG`, `UNREADABLE_DIALOG`, `NO_WINDOW`, `PROCESS_GONE`. `HUMAN_ACTION_REQUIRED` preserves today's positively known but action-ambiguous prompt; never guess native-query versus authentication.
⚠️ `READY` means **complete observation found no unexplained candidate**, not data/model ready. `findings` retains all exact identities and closed message IDs; `complete=false` carries a fixed reason (timeout/truncated/target_changed/minimized/ambiguous/unsupported). Unknown schema, malformed JSON and unreadable providers cannot become READY.
⚠️ Preserve PID creation identity + HWND/owner pre/post checks, unique-root logic, editable/password exclusion and whole-dialog role/template evidence. Positive human prompts outrank progress; incomplete/mixed evidence outranks apparent cleanliness. Nonterminal uncertainty is latched in watch summaries; later quiet polls cannot erase it. No size/title/class/WebView/OCR-only credential inference.
⚠️ Observation state and operation outcome are separate: progress never means refresh succeeded; deadline never means bad credentials/network. Do not stack on an existing refresh. Preserve successful-operation versus latched-uncertainty distinction without relabelling the observer clean; conflicting outcomes cannot earn a gate clear.
⚠️ One absolute monotonic operation deadline; each harvest/image timeout is `min(8s, remaining)`, including startup/IPC. Kill and reap **owned helper process trees only**, never Desktop; native calls live in killable helpers, not supposedly cancellable threads. Outer watchdog outlasts inner deadline/cleanup margin, never resets it per window; timeout returns unreadable plus operation timeout, not credential absence.
⚠️ Images: opt-in ignored private run scratch only, exact HWND not desktop/foreground; reuse DACL/reparse/exclusive-file/delete-on-close/DPI/extent checks. Retain current ≤60s lease, cleanup on exit/cancellation, fixed cleanup-failure notice; no raw text, full paths, pixels/base64 or arbitrary child stderr in public JSON/logs. `image` contains only opaque basename/id, status, dimensions/hash, expiry/cleanup state. Acquisition/failure never changes the observation or row verdict.
⚠️ Preserve page screenshots as a **different artifact purpose**: page dwell/iteration receipts remain in capture, not transient prompt images. No OCR classifier. `t2p desktop observe` only serializes/formats this API; it cannot contain a second classifier.

## 6. Caller-by-caller changes
| Caller | Proposed change; retained authority |
|---|---|
| `S/probe_desktop_query.py` | Replace `_credential_state`, emitters/verdict branches, observation monitor and legacy unbounded polling with observer watch + shared refusal adapter. Keep PID/port/catalogue binding, ADOMD, TOPN(1), canary scope and invocation-owned observations. |
| `S/refresh_pbip_model.py` | Replace prompt adapters and two dialog-wait branches with the same watch; AMO trace remains progress telemetry, not UI classification. Keep XMLA processing, identity/schema checks, atomic ImageSave/rollback/locks, explicit persistence and canaries. |
| `S/probe_desktop_credential.ps1` | Retire command/Refresh invocation/75s arbiter/`CREDENTIAL_PRESENT` and duplicate classifier; harvest-only replacement accepts exact PID/HWND and returns private bounded facts. No “clean after waiting” certificate. |
| `scripts/probe_live_source.py` | Stream structured observer events instead of buffering away prompt status; replace “no catalog substring absent” readiness with explicit successful catalogue binding. Keep sandbox connector M, scope keys, source-error semantics, row proof, audit and owned lifecycle. Operator states stop/ask, local unknown states remain ERROR; timeout alone is neither NO_CREDENTIAL nor UNREACHABLE. |
| `scripts/_verdict_lines.py` | Reduce dialog prose/token families to one shared observer-state mapping. Keep anchored success/table/exit/conflict checks until a separately reviewed structured **operation-proof** protocol replaces text; observer JSON is not that proof. |
| `scripts/capture_powerbi_pages.py` | Replace `_report_dialog` with observer result, preserving capture exit/refusal and held PID. Retain stable-frame dwell, staging/partial evidence and A1 binding/receipts; observer cannot bless a report. |
| `scripts/dax_oracle_server.py` | Replace hand-written state folding with shared observer refusal before opening and each query. Keep read-only DAX/numeric/error protocol; do not infer a PID for port-only/offline calls. |
| `scripts/iteration_receipt.py` / receipt tests | Mandatory reconciliation of `MEASUREMENT_REFUSALS`/`_fact_schema` and contract tests, not deletion. Use the shared compatibility mapping for persisted refusal reasons; retain literal v2/v3 reader contracts and reject unknown reasons. Changing persisted vocabulary requires explicit version compatibility, not treating observer states as processing or persistence observations. |
| Root `scripts/probe_desktop_credential.ps1` | Mandatory retarget/documentation/argument-contract edit: retain a bounded, read-only compatibility forwarder to the observer, or explicit unsupported-invocation refusal for obsolete refresh-click flags. Never leave a broken target or claim the old Refresh behavior still happens. |
| Gate / `_probe` / Python shims | **No behavioral change.** Gate consumes earned source proof, never observer cleanliness; Python forwarding purpose remains (`CONTRIBUTING.md:163-184`). |

## 7. What is deleted/reduced, not merely moved
⚠️ A retires old `_credential_modal.py`/`probe_desktop_credential.ps1` names via replacement, **not wholesale removal of their safeguards**; removes PS classification/Refresh workflow, Python caller-specific adapters/schedulers and duplicate guidance. Four one-line regex files remain canonical Python policy data; combining them saves no meaningful logic.
✅ **Gross PS candidates, not net savings:** classifier/policy `S/probe_desktop_credential.ps1:187-536` (~350 lines) plus refresh-click/poll arbiter `:855-952` (~99). Exact-PID candidates, Win32 enumeration, target checks and bounded UIA harvesting around `:538-852` **remain**, including `:674-852`; classification-dependent selection is rewired to Python authority, not deleted as harmless plumbing.
⚠️ **Caller reductions, separately:** prompt adapters `S/probe_desktop_query.py:758-880`, `S/refresh_pbip_model.py:256-383,399-521`; dialog translation `scripts/_verdict_lines.py:182-260`; capture/oracle state folding `scripts/capture_powerbi_pages.py:87-99`, `scripts/dax_oracle_server.py:169-185`. Shared supervision/JSON/refusal adapters replace these responsibilities; none is a wholesale deletion allowance. Receipt schema, compatibility shim and harness consumers add migration work, not savings.
⚠️ Rename `T/test_credential_modal_detection.py` → `T/test_desktop_observer.py`; remove mirrored Python↔PS classification parity and arbiter-click/poll harnesses, move retained native HWND/UIA/DPI/privacy controls into that suite. Reduce bundled binding/wall-clock suites and `tests/test_probe_live_source_verdict.py`; replace transcript/prose-coupled mutation fixtures with state-contract tests, not fresh mutation infrastructure.
⚠️ Keep row/negative/conflict/table-scope, all gate/ACL/source coverage, ABF/lock/canary, capture dwell/A1 and oracle numeric tests. Caller suites become small JSON/refusal contracts, not copied dialog matrices. Preserve portable-copy tests in `tests/test_skills.py` and native-test scheduling recognition in `tests/test_parallel_test_loop.py`.
✅ **Retained controls, not deletable harness blocks:** native HWND/sibling ownership `T/test_credential_modal_detection.py:1623-1740`; exact-target/UIA/completeness `:94-206,398-550,5370-5406`; wedged-provider timeout `:4880-4918`; DPI/private-file/lease/cleanup `:794-1184`; caller token/exit/poll controls `:219-267`, `tests/test_capture_powerbi_pages.py:53-141`, `tests/test_dax_oracle_server.py:341-429`. Adapt assertions to the observer contract while preserving independent safety oracles; retain receipt readers and fixture/GUI-exclusion controls, not just their test counts.
⚠️ **No defensible after-census yet.** Withdraw the prior numeric after-budget: net product/test ranges in §4 are unverified hypotheses until an actual diff separates removed duplication, moved safeguards and new contracts. `T/test_credential_modal_detection.py:3965-6387` is mixed native/timeout/completeness/policy coverage, **not a deletable range**. Most refresh/probe/test machinery remains.

## 8. Implementation slices and exact file sets
⚠️ **1 — observer consolidation, after plan approval; independent of #729/#730. Mandatory implementation edits:** rename `S/_credential_modal.py` → `S/_desktop_observer.py`, `S/probe_desktop_credential.ps1` → `S/_desktop_uia.ps1`; edit `S/{probe_desktop_query,refresh_pbip_model}.py`, `scripts/{probe_live_source,_verdict_lines,capture_powerbi_pages,dax_oracle_server,iteration_receipt}.py` and root `scripts/probe_desktop_credential.ps1`. Receipt/schema compatibility and shim routing land here, not deferred to reload.
⚠️ **Mandatory test/harness adaptations:** rename `T/test_credential_modal_detection.py` → `T/test_desktop_observer.py`; edit `T/conftest.py`, `T/{test_canary_data_gate,test_desktop_instance_binding,test_refresh_wall_clock}.py`, `tests/{test_probe_live_source_verdict,test_capture_powerbi_pages,test_dax_oracle_server,test_iteration_receipt,test_skills,test_parallel_test_loop}.py`. Fixture seams, reason schemas and GUI scheduling remain protected; these consumers are not savings.
⚠️ **Mandatory operational docs:** `.github/skills/{pbip-model-refresh,powerbi-semantic-model-gotchas}/SKILL.md`, `scripts/README.md`, `docs/{data-source-credentials,credential-gate-testing,INDEX}.md` must point at the one observation authority and preserve source proof; remove independent prompt recipes/retired command claims. **Review obligation:** `docs/parallel-test-loop.md` keeps measured historical names/figures, adds a renamed-test pointer only if needed; historical evidence is not rewritten as a new run.
⚠️ **Unchanged regression controls:** four regexes; Python shims; `scripts/{credential_gate,_gate_lift,_probe_pbip}.py`, `S/{_abf,_lock,_verdict}.py`; `T/{test_persist_atomicity,test_nuget_cache_resolution}.py`, `tests/{test_probe_live_source,test_probe_live_custom_sql,test_probe_live_tmdl_quoting,test_probe_earned_clear,test_credential_gate,test_credential_gate_acl_layouts,test_credential_gate_scope,test_credential_gate_shield,test_credential_gate_verify_target}.py`. Their gate/row/persistence coverage is retained, not counted as observer deletion.
⚠️ **2 — reload/refresh deletion, only after #729 floor + #730 positive measurement.** Edit `scripts/{probe_live_source,capture_powerbi_pages,iteration_receipt}.py`, `S/refresh_pbip_model.py`; tests `tests/{test_probe_live_source,test_probe_live_source_verdict,test_capture_powerbi_pages,test_iteration_receipt}.py`, `T/test_refresh_wall_clock.py`; guides `.github/skills/{pbip-model-refresh,powerbi-report-gotchas,powerbi-semantic-model-gotchas}/SKILL.md`. Delete only measured-obsolete close/reopen/direct-host/PID workarounds, never internal identity verification, safe persistence or one-row proof. No whole-file deletion preauthorized.
⚠️ CLI exposure is a #734-owned follow-up consuming slice 1's API, not a third implementation slice with invented CLI file paths. #704/#709 handoff/publication repair also remains separate; neither is a prerequisite to extracting observation authority.

## 9. Risks and discriminating controls / mutations
⚠️ Ordinary positive/negative controls first: test-owned owned/unowned/sibling windows, nested ownership, role-correct prompt/progress/mixed/unreadable trees, explicit unsupported backend. Mutate wrong PID/reused HWND, dropped truncation, swallowed operator prompt, progress-over-unknown precedence, deadline reset, leaked child and public raw text: each must fail its intended identity/completeness/privacy assertion.
⚠️ Independently feed READY/nonblank PNG/refresh-only/zero rows/wrong table/nonzero exit/conflicting prompt with a success token: **none earns clear**; fresh correct-source row + all source keys is positive control. Mutate `_probe` under a denied anchor: sandbox usability must fail; mutate gate removal on observer READY: earned-clear test must fail. Preserve DPI/cropped-image, path replacement/reparse and lease-cleanup negatives.
⚠️ Consolidation can fail closed more often on localization, minimized/ambiguous windows and provider stalls; report that uncertainty rather than invent source failures. It must not fix noisy diagnostics by dropping identity/completeness. No new digest/census/mutation runner is justified just to prove this refactor.

## 10. Issues affected
✅ [#723](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/pull/723) is merged diagnostic enrichment; qualification used test-owned WPF windows, with real-Desktop/localization/MSHTML residuals ([final review](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/pull/723#issuecomment-6041326875)). Reuse its safeguards, not its claims as fresh Desktop evidence.
⚠️ #741 owns consolidation and closes only after caller convergence and real acceptance evidence, not this report. Reduce [#146](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/146) to remaining handoff/gate policy after transferring observation acceptance; its [latest operator-confirmation correction](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/146#issuecomment-5725992310) must remain distinct from probe-earned DATA_OK.
⚠️ Keep [#498](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/498) open/reduce to original MSHTML-only error reader and safe private detail disclosure; UIA templates do not establish that original body/file/line acceptance. A future bounded MSHTML backend belongs behind observer, not another classifier.
✅ [#697](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/697#issuecomment-5723943214) closed without the proposed OCR classification change; do not resurrect it. [#704](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/704)/[#709](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/pull/709#issuecomment-5728249399) are **open issue / blocked draft, not merged**; retain deadline/publication/changed-directory counterexamples before considering supersession or merging. Observer does not repair that handoff transaction.
⚠️ #729/#730 remain dependency/measurement work; #734 owns command naming/routing. Merge only duplicated observation scope into #741; close predecessors as superseded only with explicit transfer of residual criteria. This PR says **Refs #741**, not Fixes.

## 11. What remains unverified
⚠️ All net savings, live Desktop/UIA latency, localization, connector prompts, DPI completeness, crash cleanup, MSHTML extraction, modern CLI/bridge behavior and blind-run-2 fidelity are unmeasured here. No tests or tool availability can substitute for that future qualification; blind run 2 must show actual observer state, not guessed source status.
✅ Session preflight could not start (`powershell` unavailable); static source/GitHub/upstream-document inspection only. No source data, screenshots or retained private evidence was created. This revision changes **only this report and its `docs/INDEX.md` navigation entry** (`scripts/check_navigation_index.py:50-66`); all implementation edits above are proposals, not product changes in this PR.

## 12. Implemented slice 1 — Refs #741

**Status: draft, blocked at the approved surface boundary.** The complete repository run exposed
`tests/test_gui_marker_gate.py`'s hard-coded minimum of ten GUI/spawn sites; retiring the old PS parity
fixtures leaves six retained native/UIA cases. That gate is **outside N=35**. It has not been edited,
and duplicate GUI cases have not been added merely to satisfy its historical census. The owner must
review the surface/deletion decision before this slice can claim a green repository gate or merge.

This section describes the implementation against **511bb209**, not a retroactive qualification of
the investigation above. The binding approval is
[#741's slice-1 contract](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/741#issuecomment-6239850790).
The independent review's amendments take precedence over the earlier proposal.

### Authority and identity

- `S/_desktop_observer.py` owns the classifier, whole-template interpretation, same-snapshot fold,
  shared bounded latch, scheduling defaults, closed operation diagnostics and existing private image
  lease. `S/_desktop_uia.ps1` only harvests Win32/UIA facts. It contains no signatures, verdict
  classifier, Refresh invocation, click, save or polling arbiter.
- `observe(pid, *, hwnd=None, expected_process_start=None, timeout_s=8, evidence=None)` returns a
  frozen `Observation`; `watch(..., deadline, operation_id, expected_process_start=...)` owns an
  invocation deadline and helper settlement. Bound callers supply their **held** start token, not a
  later PID rediscovery. Watch admission, cancellation and record publication share a lock.
- In `desktop.harvest.v1` and `desktop.observation.v1`, PID and creation identity are canonical
  decimal **strings**; HWND/owner are canonical hexadecimal strings or null. Creation identity is
  the existing lossless UTC-tick token, **not** an RFC3339 value round-tripped through Python
  `datetime`. `observed_at` remains a UTC display timestamp. Existing receipt binding schemas keep
  their literal bounded-integer PID fields; they are not silently versioned or coerced.
- Duplicate/extra keys, wrong identities, lossy numeric handles/ticks, invalid flags, incomplete
  providers, truncation and late transport cannot establish a clean observation. Owner-enabled
  facts remain three-valued; changed owner/enabled facts do not earn an exoneration.
- The root/candidate exclusions remain the approved native-window policy: an identified application
  frame, an attested enabled owner, or an unowned zero-area window. Completeness concerns enumeration,
  target attestations and required candidate harvests; it is **not** a claim to read an application's
  entire document/DOM. Positive prompt evidence survives modality exclusions and outranks progress.
  Editable/password values and non-read-only document patterns are not read.

### Outcomes, receipts and private evidence

`READY` means only complete observation with no unexplained candidate. It cannot mean refresh,
query, persistence or source success. Callers retain operation exit bands and phase decisions;
progress is factual and is never rewritten to READY. Query publication waits for started inspection
helpers to settle. Refresh may finish with retained nonterminal uncertainty, without erasing it.
First established terminal wins; a same-snapshot stronger state follows the shared precedence.

Only the existing correct-table, conflict-free one-row Power BI path plus required live-leg coverage
can earn `DATA_OK`; only the credential gate changes physical denial. `_probe` stays writable.
Observer events are validated separately and excluded from connector free-text scanning. Neither
READY, an image nor processing completion is row proof.

Receipt versions 2/3 and their literal readers/enums remain unchanged. A trusted explicit adapter
maps runtime `DESKTOP_GONE` and `REFRESH_IN_PROGRESS` to `TOOL_UNAVAILABLE`; unknown codes reject.
Observer JSON is never stamped as successful A1 processing/persistence measurement.

The exact-window, owner-only DACL, reparse/exclusive-file, delete-on-close, DPI/extent and <=60-second
private-image lease are retained. There is no OCR, retained-evidence repair or image-based gate clear.
The root `scripts/probe_desktop_credential.ps1` accepts every old parameter set and **always** exits
3 with fixed `VERDICT: UNKNOWN`: no observation or Refresh is performed and no arguments are echoed.
The portable bundle contains the new Python/backend names and all four unchanged regex resources,
not a dead arbiter forwarder.

### Measured deletion census

Same file sets and counting rules as §2, with the reviewed receipt/root-shim additions. Renames and
moved safeguards earn **zero** deletion credit. Counts are physical lines and AST/named-PS functions;
test counts are definitions, **not** parametrized executions or Windows passes.

| Fixed census | Before | After | Net |
|---|---:|---:|---:|
| Product, 15 files: physical lines | 12,670 | 12,407 | -263 |
| Product functions | 476 | 492 | +16 |
| Direct + halo tests, 22 files: physical lines | 24,916 | 22,927 | -1,989 |
| Test definitions | 873 | 821 | -52 |

The demonstrated gross PS classifier/click/poll block is **449 lines**: old `:187-536` plus
`:854-952`. The new contracts, lossless transport, helper settlement and caller adapters consume much
of the saving. **The earlier 650–1,100 product-line estimate was not achieved**; it was an explicitly
unverified hypothesis, not a measured result. The architectural reduction is two classifiers to one,
one mutating PS arbiter to zero, and no caller-owned UI-text classifier. The operation-source parser,
physical gate, source coverage, persistence, locks and private-image safeguards are not deleted.

### Closed surface and limits

The atomic surface remains **35 paths / 32 logical files / three renames**: the runtime/caller,
receipt, root shim, bundled/root harness, two skill guides, scripts README, credential/gate/design/
parallel-loop/index docs and workflow comment listed in the approved plan. Four regexes, root Python
shims, credential gate/lift/probe construction and ABF/lock/verdict modules remain unchanged.

Windows qualification uses **test-owned windows only**, including native ownership, same-PID/start/
HWND controls, role/read-only provider guards and private GDI/ACL/PNG controls. Pure fixtures cover
mixed/incomplete precedence, latches, late settlement, receipt compatibility and source/gate negatives.
Copy-out runs remove host imports and require the backend/resources. Exact commands, execution
counts, mutation outcomes and any baseline failures are recorded with the draft PR.

Final targeted/caller/receipt/copy-out partition: **1,298 passed, three expected real-cache skips**.
Quiet qualification: **eight timing/serial and six test-owned GUI controls passed**. All seven
requested mutations passed their positive baseline and failed an executed assertion when broken.
The full non-GUI partition ran to completion: **13,632 passed, 52 failed, 63 skipped**. It is not
reported green. Four nested-pytest/conftest failures independently reproduce on 511bb209; other
full-run residuals and the out-of-surface GUI census require owner reconciliation.

Product ruff/pylint and navigation/capability/privacy/diff gates pass. The broad test-harness pylint
command is not green: it already reports substantial import-resolution/style debt on approved base
(7.92/10, exit 30); this slice does not waive or repair that unrelated debt to manufacture a clean run.

No live Desktop, customer source, localization/MSHTML completeness, large real-report UIA latency,
modern CLI behavior or blind-run-2 fidelity is certified by this slice. #146 policy, #498 MSHTML,
#704/#709 retained-artifact transactions, #730 reload/processing policy and public CLI exposure remain
separate. This implementation is **Refs #741**, not closure of its remaining qualification.
