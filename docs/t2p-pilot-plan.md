# Narrow `t2p doctor/start/status` pilot plan — Refs #749
## Decision and scope
**⚠️ Narrow pilot: accept the existing post-harvest/pre-conversion path gate; no #751 dependency.**
Original pilot plan; this amendment changes doctor presentation only. Initial path decision stays CANNOT_ESTABLISH / `unknown_until_harvest`, never PASS.
Base: `511bb2098d179f2190943e45c650739c8c794fea`; issue: <https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/749>.
Owner decisions: [path timing](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/749#issuecomment-6233864458), [doctor addition](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/749#issuecomment-6239785454); retain other independent-review amendments.
```text
python -B scripts/t2p.py doctor [--env <path>] [--json]
python -B scripts/t2p.py start --project <exact-name|LUID> [--env <path>] [--runs-parent <path>] [--json]
python -B scripts/t2p.py status --run <absolute-run> [--json]
```
Start requires one nonempty project selector; disable argparse abbreviation; reject other selectors/verbs.
No local file/folder/datasource/site/server routes, `next`, credential, package, build, validation,
promotion, deployment or dispatch commands; no installed entrypoint, framework, registry, DB or router.
✅ Assessment stays site-wide; owner comments extend/supersede `docs/cli-consensus/consensus.md:45-71,81,130-138`.

## 1. Path invariant and decision timing
**Initial:** CANNOT_ESTABLISH / `unknown_until_harvest` at either root; no pre-network PASS required.
Delegation may allocate, authenticate, survey and harvest under existing guards; unknown is not safety.
**PASS:** after harvest the owner's projection fits **259/247 UTF-16 units**; canonical skeleton, not every generated tail.
PASS permits conversion only; it certifies neither emitted paths nor readiness. Retain downstream gates.
**REFUSE / OVER_CEILING:** owner projection exceeds a ceiling; existing output-guard refusal still applies.
**CANNOT_ESTABLISH at conversion gate:** unusable names/projection evidence refuses conversion as today.
Neither later non-pass proceeds to conversion; preserve the owning stage and exact delegated exit.
No unknown→PASS, relocation, truncation, second bound/allocation formula or silent external parent; bypass/false PASS blocks merge.
✅ Projection needs harvested names; minimum skeleton only, not an emitted-tree guarantee:
`scripts/run_estate.py:76-82,492-544,561-585,3661-3670`.
✅ Front door allocates before survey, harvests before conversion: `scripts/start_migration.py:463-503,520-538,585-649`.
❌ Selector/short root alone proves no bound; owner accepts repeated harvest on the self-populated trial site.
On over-ceiling, retain the old run and exact new-run `--runs-parent` command; do not auto-retry.
✅ `run_estate` returns 10 at non-pass projection; front door retains inputs, returns wrapper 1 (3 if invalid), no facade normalization:
`scripts/run_estate.py:3617-3622`; `scripts/start_migration.py:649-705`; `tests/test_start_migration.py:752-773`.
Consume existing owner results only; any additional facade policy/scanner/state file requires replan.

## 2. Location and output contract
✅ Sole allocator `work_dirs`: repo-local `_runs` default, explicit external parent only; no preallocation/number prediction:
`scripts/work_dirs.py:17-27,779-856`.
Before allocation, first/last human lines are `RUN NOT_ALLOCATED; runs_root=<resolved-root>`;
JSON has `run_location:null`, `runs_root:null` with reason `not_allocated_path_withheld`; invent no path.
After allocation, first/last human lines are `RUN <absolute-physical-run>`, including failure/interrupt;
print before children; preserve location in JSON, never discover “latest”. Doctor has no run/path claim.
Human: recorded/selected/physical run/root, repo-local/external, git visibility `ignored|visible|unknown`,
commit safety `not_for_git|unsafe|unknown`; JSON withholds other roots.
Only the existing output guard establishes ignore safety; external is not synonymous with ignored.
For status, do not launch git; underivable visibility is null with reason `not_derivable_read_only`.
Path fields always present: ceilings 259/247, longest file/directory locators/lengths, remaining UTF-16 budgets, projection verdict/reason.
Underivable status values are null with stable per-field reasons
(`unknown_until_harvest|not_recorded|not_derivable_read_only|identity_unestablished`), never fabricated PASS.
No status scanner: `check_path_ceiling.scan` launches git (`scripts/check_path_ceiling.py:292-315,415-455`).
Exact private rerun argv/display requires every argument recorded (project, env/default, toolkit/root):
Shape: `python -B <recorded-toolkit>/scripts/t2p.py start --project <recorded> [--env <recorded-env>] --runs-parent <recorded-parent>`.
Otherwise emit a partial/candidate command marked non-authoritative, or unavailable with a reason.
New invocation only, old run retained; shorter-root candidate goes through the same existing owner gate.
JSON redacts toolkit/selector/env/root tokens; mark partial/non-authoritative, never exact or safe.
`subst` temporary/non-authoritative or null/`not_derivable_read_only`; no status discovery/run-identity alias/ceiling waiver:
`docs/windows-path-limits.md:100-112,265-271,339-371,408-417`.

## 3. Delegation, native exits and stable envelope
**Choose one delegated owner:** `start_migration`, not a facade-owned five-child chain.
Preserve its exact wrapper exit: 0 capture-only, 1 failed, 3 invalid evidence (takes precedence), 130
interrupted. Preserve all stage/native outcomes separately; never assign global meaning to numbers.
Reference failure/invalid evidence still attempts bundle; earlier failures stop, not first-native-failure semantics:
`scripts/start_migration.py:613-688,691-705`; `tests/test_start_migration.py:697-736`.
Optional in-memory observer/stream seam preserves defaults; reports allocation, preflight, stages and wrapper result.
Stage records: fixed stage/owner, `native_exit_code` (null if not launched/launch failure), state, safe relative artifact/evidence check.
No persisted ledger/result file. Record native results where obtained, not from printed prose.
Start JSON routes child stdout AND stderr plus progress to stderr live/unbuffered (including OS descriptors).
One JSON object on stdout, including syntax/refusal/interrupt errors; human uses those records plus private paths.
Pin inline schema (no framework/dependency):
```json
{"schema_version":"t2p/v1","command":"start","status":"error","exit_code":1,"authority":"start_migration.py","delegated_exit_code":1,"stages":[],"evidence":[],"artifacts":[],"run_location":null,"path_report":{"initial_decision":"cannot_establish","initial_reason":"unknown_until_harvest"},"limitations":[],"proof_boundary":{"established":[],"not_established":[]}}
```
Start/status statuses: `ok|findings|cannot_establish|error|interrupted`; refusal uses owner details, not a new status.
After delegation, outer exit equals exact owner exit; `delegated_exit_code` is the wrapper/diagnostic code.
`native_exit_code` occurs only in stage records. Before delegation: syntax=2, refusal=1, unknown=3, error=1,
interrupt=130; pin ownership as well as integers. `findings` may have native diagnostic exit 0.
Evidence/artifact entries contain owner plus safe relative locator (digest only if already supplied).
Stable keys/nullability and deterministic ordering; additive fields compatible, incompatible shape=new schema.
Never copy customer text, PAT, server/site, arbitrary exception/manifest prose or unrelated absolute paths
into envelopes. Start child streams remain private native diagnostics, not sanitized/public attachments;
doctor never forwards raw output; status omits toolkit/other absolute locations. Absolute run paths are local-only.

## 4. Status authority and compatibility
Call `run_status.build_status` under `-B`; preserve code, multiplicity/findings and identity boundaries.
No status subprocess, network, cache, write, repair, allocation or readiness gate.
Omit its `next_action` from the facade projection (direct script remains unchanged); add no routing.
Missing/moved/contradictory identity => cannot-establish, no descendant scan or derived run path.
`active` is allocation metadata, stored results last-observed; no current liveness/readiness/fidelity/credential claim:
`scripts/run_status.py:851-939,972-987`.
Direct start/status/work_dirs/path scripts retain arguments, output, exit and artifact behavior.
No replacement of guidance/personas before benefit; no direct-script path policy changes in v1.

## 5. Read-only doctor contract
Run the same plain preflight checks once, adding only internal `-DoctorDiagnostics` (no -Update/-CheckUpstream), child env `PYTHONDONTWRITEBYTECODE=1`; invoke doctor with `python -B`.
Use the existing `tableau_env.diagnose_env` owner with selected env (default repo .env); no install/plugin mutation, allocation,
login, source probe or Tableau authentication. `--env` selects Tableau input, not all host/Fabric configuration; session-start preflight remains separate.
✅ Resolver normalizes each source then overlays process on file, absent site defaults empty:
`scripts/tableau_env.py:194-208,243-289,314-330`; canonical/optional keys: `scripts/tableau_env.py:47-75`.
✅ The existing opt-in diagnostic owner handles malformed/explicit-missing files, whitespace and conflicts; legacy direct defaults stay unchanged.
Ordinary doctor renders a human verdict, all configuration/failed-critical blockers, owner/native evidence, safe manual actions and warning count.
Explicit JSON preserves every existing field/check/stage/proof/limitation/null; optional doctor-only `doctor_details` adds present alias groups and preflight detail.
Report canonical key, states `set|missing|blank|conflict|deprecated-alias`, source `environment|selected_env|default|absent`,
required/optional, secret flag (PAT_SECRET/SF_PASSWORD/DBX_TOKEN; aliases inherit) and fixed safe reason/remedy.
All names TABLEAU_-prefixed: require SERVER_URL/PAT_NAME/PAT_SECRET; SERVER/PAT_VALUE warn; SITE empty/default valid.
List canonical optional version keys and DATASOURCE_CREDENTIAL_KEYS as optional presence, never authentication.
Only SERVER_URL/SERVER and PAT_SECRET/PAT_VALUE gain alias metadata: winning spelling plus present source/key/conflict rows; a losing-file conflict remains located there.
Preserve the privately intended PAT under PAT_SECRET BEFORE removing PAT_VALUE; move a legacy intended value first. Default site/optional absences retain existing checks only.
Doctor JSON uses `schema_version:t2p/v1`, `command:doctor`, `status:READY_TO_ATTEMPT|BLOCKED_CONFIG|CANNOT_ESTABLISH`;
`checks` carries safe records, preflight stage retains native code, delegated_exit_code is null (two owners).
Outer exits: 0 only if preflight=0 and required config consistent; 1 missing/blank/conflict or preflight=1; 3 unreadable/malformed/unknown source, launch or unexpected preflight code;
unknown overrides blocked; syntax 2 and interrupt 130 retain error/interrupted envelopes. No global child-code meaning.
run_location/path_report null (`not_applicable_no_run`); never values, secret length/digest/prefix/suffix,
server/site/PAT name, host paths, exception prose or --show-values, on stdout OR stderr in either mode.
Capture both child streams privately; closed owner codes contain ALL failed-critical blockers plus recommended-warning COUNT, no passing/optional/manual census.
Missing/noisy/malformed/unknown detail is unavailable with null counts/empty blockers; native 0/1 policy stays unchanged. Run plain preflight privately without -Update for unavailable causes, never guess PAT/Desktop.
Flush `Checking existing preflight; no upgrades or Tableau login.` to stderr; human stdout is not JSON. Rerun retains `--env <same-selected-env>` when selected; paths stay private.
Dead Desktop guidance refreshes the inherited process pin and retries in the SAME invocation after verifying saved intent; never persistence-only/unpin-to-pass. Doctor intentionally writes nothing; existing Azure/Fabric traffic and external host caches remain possible, not an offline/zero-write promise.
READY_TO_ATTEMPT proves no PAT validity, reachability, datasource credentials, path safety, START_READY/fidelity/COMPLETE.

## 6. Closed surface, slices and independent controls
**Original plan-only PR: `docs/t2p-pilot-plan.md` and `docs/INDEX.md`; doctor-only presentation amendment: N=8 existing files, no new files or lifecycle scope.**
Original pilot proposal (9 files; historical): new `scripts/t2p.py`, `tests/test_t2p.py`; edit shared resolver
`scripts/tableau_env.py`, `tests/test_tableau_env.py` for diagnostic metadata only (no second policy);
edit `scripts/start_migration.py`, `tests/test_start_migration.py`, `scripts/README.md`,
`docs/agent-capability-wiring.md`, `docs/INDEX.md`. This historical inventory does not expand the doctor-only amendment.
Shared: front door, resolver and three documentation inventories; all other files unchanged.
Reads: argv/process/env file, preflight, allocation/run.json, stages/artifacts/status; owner joins selector→catalog→harvest→oracle→bundle,
recorded→physical, canonical↔alias/file↔process. Residuals: remote bounds, non-atomic reads, unsafe paths, unknown measures, host-cache effects.
Slice 0: independent exact-head plan review against the latest owner timing decision; no #751 dependency.
Slice 1: shared diagnostics/optional observer; slice 2: three explicit argparse branches/envelopes;
slice 3: tests/help/docs, Windows read-only review then blind run; new files/class => replan.
Direct controls: same owner/argv/environment, independently fixed artifacts (not facade output); positive intact run/project/259/247 paths.
Negative: missing/moved/malformed/reparse run; 260/248 and astral/surrogate paths. At BOTH roots initial
unknown permits harvest but never claims PASS; post-harvest unknown/over-ceiling must prevent engine launch.
Pin wrapper/native pairs: reference 4→bundle attempted→wrapper 1; invalid reference→bundle→3;
survey 7→no later stages→1; bundle 10 retained; preflight failure; launch failure; interruption/reaping.
Status controls: direct/facade codes/observations, repeated byte/tree hashes, absent fresh bytecode prefix; trap network/process/write.
Doctor controls: fixed fixtures pin unchanged JSON/native policy, both alias groups, losing-file conflicts, all blockers, safe PAT/current-process Desktop remedies, default SITE/optional absence,
unavailable detail/native 0/1/7/launch/syntax/interrupt, human/JSON channels, flushed progress, exact one-child argv and private canaries/derivatives.
Mandatory full Windows child uses a synthetic toolkit/profile, allowlisted environment and PATH stubs; snapshots bytes/mtimes/tree and command logs catch writes/network/updater/login calls.
This proves only the monitored fixture/stub path, not real host-cache behavior. Existing mutation helper exercises all twelve doctor reversals on intended assertions; no new runner/proof framework. Preserve existing no-pyc/start/status/path/stream/label controls.
Mutations in isolated copies: remove doctor/start/status branch; disconnect path gate; force unknown→PASS;
drop native stage record; suppress post-reference bundle; normalize exit; buffer progress; enable
status write/next. Each must fail its named scope/guard/stage/exit/stream/read-only assertion.

## 7. Validation, blind run 2 and stop conditions
Exact commands below run from the toolkit root; future commands are NOT authorization to implement.
Docs now: `wc -l docs/t2p-pilot-plan.md` (≤160); `git diff --check`;
`python -B scripts/check_navigation_index.py`; `python -B scripts/set_data_folder.py --check`.
Future, stage new scripts first: `pytest -q tests/test_t2p.py tests/test_start_migration.py tests/test_run_status.py tests/test_work_dirs.py tests/test_check_path_ceiling.py tests/test_tableau_env.py tests/test_preflight_contract.py`;
`pytest -q tests/test_scripts_help.py tests/test_repo_layout.py tests/test_agent_capabilities.py`.
`ruff format --check scripts/t2p.py scripts/start_migration.py scripts/tableau_env.py tests/test_t2p.py tests/test_start_migration.py tests/test_tableau_env.py`;
`ruff check scripts/t2p.py scripts/start_migration.py scripts/tableau_env.py tests/test_t2p.py tests/test_start_migration.py tests/test_tableau_env.py`;
`pylint scripts/t2p.py scripts/start_migration.py scripts/tableau_env.py`; `python -B scripts/check_agent_capabilities.py`;
`python -B scripts/sync_agent_conventions.py --check`; repeat docs gates; secret scan and blind diff review.
Full gate: `T2P_ENGINE_TESTS_NOT_CHECKED_REASON=covered-by-pinned-engine-integration-job uv run pytest -q`.
Blind run 2 after approved plan/implementation/Windows review: cold session gets only help/output, no source/full README;
same `20 - Supply Chain`, environment and oracle coverage as run 1. Check doctor boundaries, run first/last paths, live output/exits/read-only status.
Compare time/calls/scope/evidence/false claims and independent Tableau fidelity; benefit needs fewer calls/time with no regression;
different coverage/environment => inconclusive. No measurable benefit => stop, not broader commands.
