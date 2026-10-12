# Migration CLI consensus — lifecycle, state, and gate authority

**Scope:** this is a design proposal, not a claim that a unified `t2p` command exists.
✅ Verified behavior is cited inline; ⚠️ inferred choices are marked **PROPOSAL**; ❌ gaps are
explicit. A gate's owner decides; a facade only invokes it and reports its evidence.

## Lifecycle map

| Stage (required order) | Current script(s); artifacts read → written | Pass / refuse / cannot-establish authority and evidence |
|---|---|---|
| Doctor | `preflight.ps1`; environment → readiness matrix, no run artifact | **PASS** critical checks exit 0; **REFUSE** critical miss exit 1; **CANNOT-ESTABLISH** process did not start/output unreadable. Warning-tier misses do not fail it. [docs/operator-runbook.md:119-129] |
| Scope | No separate pre-run command; human plan/selectors read → none yet. `start_migration.py` records selectors in `<run>/run.json`; post-survey `assess_estate.py` writes `<run>/assessment/{assessment.json,estate.db,report.md}` | **PROPOSAL:** human-approved scope before allocation; mismatch/ambiguity refuses; identity is CANNOT-ESTABLISH until survey joins it. Current assessor runs after allocation and survey; datasource-only project scopes are unsupported by the front door. [scripts/README.md:39-49; scripts/start_migration.py:71-79,481-503; docs/operator-runbook.md:593-611] |
| Run (allocate) | `work_dirs.py <unit> [--runs-parent PATH]`; none → `<run>/run.json`, canonical dirs | **PASS** allocation records run number/name/absolute path; **REFUSE** allocation error; **CANNOT-ESTABLISH** malformed or contradictory identity (`--verify` exit 3). Never rename/reuse. [scripts/work_dirs.py:6-27,661-778; docs/migration-phases.md:58-99] |
| Survey | `run_engine_survey.py`; Tableau site → `<run>/assessment/estate_survey.json` | **PASS** complete scoped JSON and joins; **REFUSE** child nonzero; **CANNOT-ESTABLISH** missing/inconsistent scope/counts (front door exit 3). [scripts/start_migration.py:520-550] |
| Harvest | `harvest_estate_assets.py`; survey/`assessment/estate.db` → `<run>/assets/assets/*`, `<run>/parse-sweep.{json,md}` | **PASS** harvested set and parse sweep; **REFUSE** harvest failure/guard; **CANNOT-ESTABLISH** missing or irreconcilable asset evidence. [docs/migration-phases.md:100-118; scripts/start_migration.py:585-611] |
| Reference capture | `capture_tableau_oracle.py --out <run>/oracle`; alternatively `capture_tableau_reference.py <slug-dir>` → `<run>/oracle/oracle-manifest.json`, images/data; or `reference/manifest.json` | **PASS** only requested capture legs recorded; **REFUSE** failed requested capture (oracle 1–5); **CANNOT-ESTABLISH** missing/unknown record or attribution. This is input evidence, not fidelity. `capture_tableau_reference.py` exists, but `--server-rest` is a stub; live REST route is the oracle. [scripts/README.md:113-115; scripts/capture_tableau_reference.py:497-568,579-583] |
| Bundle (engine conversion) | `run_estate.py --input ... --output <run>/bundle`; assets/survey → `bundle/{report.json,input_manifest.json,pbip/,reports/,semantic_models/,handover/,engine-output-receipt.json,phase-timings.json,path-ceiling.json}` | **PASS** wrapper DoD exit 0; **REFUSE** DoD/gates (exit 3, etc.); **CANNOT-ESTABLISH/REFUSE** path ceiling exit 10 also covers unmeasurable projection/tree. Engine receipt identifies source/version; no fallback. [docs/operator-runbook.md:793-810; scripts/engine_source.py:23-31,100-113] |
| Credential gate | `credential_gate.py status|block|clear|verify|authorize|list`; marker/audit/ACL plus source probe → gate state | **PASS** only verified earned probe-clear; **REFUSE** still blocked/forged override; **CANNOT-ESTABLISH** ACL/audit unavailable. `authorize` is human-approved UNVALIDATED fallback, never a PASS; gate is armed at parse. [scripts/credential_gate.py:51-60,273-304,2491-2534; scripts/README.md:86-87] |
| Package / START_READY | `package_unit.py --bundle ... --out ...`; bundle/source/oracle/brief → `<run>/packages/<Unit>/{package-manifest.json,migration-spec.json,data-access.json,fabric/,handover/,oracle/}`; then `check_reference_readiness.py` | **PASS** construction `ASSEMBLED` is not readiness; only current package/cohort check yields `START_READY / 0`; **REFUSE** findings exit 1; **CANNOT-ESTABLISH** exit 3. [docs/migration-phases.md:160-183,245-251,356-377; docs/reference-readiness.md:77-85] |
| Builders (semantic + report) | Copilot builders; package `fabric/` (or pre-package `bundle/pbip/`) → TMDL/PBIR, replay scripts/declarations | **PASS** only at the canonical working tree; bundle generated edits use declarations, package `fabric/` is canonical once packaged; never edit `reports/`. **REFUSE** credential ACL/invalid structure; **CANNOT-ESTABLISH** unsupported mapping/gate. [AGENTS.md:522-523; docs/migration-phases.md:122-135,262-269; scripts/README.md:97] |
| Refresh | `refresh_pbip_model.py` forwarding shim → skill script; model/TMDL → `.pbi/cache.abf` | **PROPOSAL:** invoke only after TMDL/model validation; **PASS** refresh evidence from refresh owner; **REFUSE** failed refresh/credential modal; **CANNOT-ESTABLISH** timeout/unclassified result. Refresh shim itself only forwards. [scripts/refresh_pbip_model.py:1-10,19-31; scripts/README.md:110] |
| Fidelity (validator) | `pbi-migration-validator`; shipped package `fabric/` + attributable Tableau references/oracle → comparison findings | **PASS** only the validator's evidence-supported comparison; **REFUSE** mismatch; **CANNOT-ESTABLISH** grade/coverage missing. Oracle default-state imagery is layout/text-grade, never sufficient for fidelity PASS. [docs/reference-readiness.md:31-37; docs/reference-capture.md:64-80] |
| Promotion | `promote_unit.py --package ... --slug ... --receipt-sha256 H`; package + same caller-held H → `migrations/{workbooks,datasources}/<slug>/fabric/` | **PASS** checked promotion; **REFUSE** checker/content/host-path refusal; **CANNOT-ESTABLISH** package ambiguous/unreadable (exit 2). Force is explicitly unchecked, not COMPLETE. [docs/migration-phases.md:284-287,394-400; scripts/promote_unit.py:64-83] |
| Deploy | `deploy_estate.py --bundle ... --dry-run`, then explicit deploy; bundle/workspace → Fabric items and `<bundle>/deploy-journal.jsonl` | **PASS** dry-run item count is reviewed, then deploy succeeds; **REFUSE** destination/preflight/partial deploy; **CANNOT-ESTABLISH** unresolved operation/result. Current script does not prove a prior dry-run occurred. [docs/operator-runbook.md:497-510; scripts/README.md:93] |
| Verify / feedback | `verify_bindings.py --workspace ...` (read-only); `build_migration_feedback.py` → report results / private evidence + public-safe `issue-payload.json` | **PASS** all bindings resolve / evidence bundle validates; **REFUSE** findings or unsafe payload; **CANNOT-ESTABLISH** check/evidence unavailable. Binding is not render fidelity; feedback builder does not publish. [scripts/README.md:94-96,65] |

## Proposed command surface (thin wrappers only)

**PROPOSAL:** one command name, `t2p`; stage verbs map one-to-one to owners; navigation verbs compose read-only owner outputs, never gate logic. Core (default help): `doctor`, `start`, `status`, `next`, `resume`, `unit prepare`, `validate`, `credentials list`, `deploy --dry-run`.
Advanced/support verbs are in advanced help. Underlying scripts are internal implementation names, remain callable for compatibility, but stay out of agent prompts.
Every command has strong `--help`, human output, and versioned `--json`; relay child output live to stderr (keeping JSON stdout clean), redact secrets, preserve `native_exit`/owner evidence, and encode errors. The command registry/help is canonical; README/INDEX become generated summaries or pointers. [Issue #734: Desired agent experience, Tiering]

| Tier / verb | Wraps (thin) | Reads → writes | Mutating? | Native exits / notes |
|---|---|---|---|---|
| Core | `doctor` → `preflight.ps1` | environment → none | No | 0 / 1 [docs/operator-runbook.md:119-129] |
| Advanced | `scope` → `assess_estate.py` | survey → assessment dir | Yes | 0 / 1 / 2 / 3; only after survey [scripts/assess_estate.py:70-75,1813] |
| Core | `start` → `start_migration.py` | selectors/env → allocated run and five-stage outputs | Yes | 0 / 1 / 2 / 3 / 130; 0 is capture-only [scripts/start_migration.py:363,454] |
| Advanced | `allocate` → `work_dirs.py` | unit, root → `_runs/.../run.json` | Yes | 0 / 1 / 2; `--verify`: 0/1/2/3 [scripts/work_dirs.py:74-89,1346] |
| Advanced | `survey` → `run_engine_survey.py` | Tableau → `assessment/estate_survey.json` | Yes/network | child-native 0 / 1 / 2 [scripts/run_engine_survey.py:15-18,52-61] |
| Advanced | `harvest` → `harvest_estate_assets.py` | assessment/site → assets + parse sweep | Yes/network | native 0/1/2/3 [scripts/harvest_estate_assets.py:12-20,149-152] |
| Advanced | `reference` → `capture_tableau_oracle.py` | Tableau → oracle manifest/renders/data | Yes/network | native 0–5 [scripts/capture_tableau_oracle.py:2003-2007] |
| Advanced | `build` → `run_estate.py` | assets/survey → bundle/receipts | Yes | native 0–11; 3 DoD, 9 rewrite, 10 path [scripts/run_estate.py:204-214] |
| Core + advanced | Core `credentials list`; advanced `credentials status|block|clear|verify|authorize` → `credential_gate.py` | marker/ACL/audit → verdict | Except block/clear/authorize | preserve subcommand exits; list 1 blocked, 3 forged, 4 bad root [scripts/credential_gate.py:1196-1198,2491-2534] |
| Core | `unit prepare` → `package_unit.py` | bundle/brief/evidence → package | Yes | 0 / 2 / 3 / 5 / 6 (1, 4 legacy) [scripts/package_unit.py:123-137,397-403] |
| Advanced | `readiness` → `check_reference_readiness.py` | package/cohort → START_READY result | No | native 0 / 1 / 2 / 3 [scripts/check_reference_readiness.py:31-41,164-167] |
| Core | `validate` → `check_unit.py` | unit/package → layer or all-scope result | No | 0 / 1 / 2 / 4 / 64; only caller-pinned all-scope can COMPLETE [scripts/check_unit.py:6-13,93-97] |
| Advanced | `refresh` → `refresh_pbip_model.py` | PBIP/TMDL → cache | Yes | child-native; shim 2 if target missing [scripts/refresh_pbip_model.py:23-31] |
| Advanced | `promote` → `promote_unit.py` | package/H → `migrations/.../fabric/` | Yes | native 0–6/64 [scripts/promote_unit.py:64-73] |
| Core + advanced | Core `deploy --dry-run`; advanced `deploy` → `deploy_estate.py` | bundle/workspace → plan or Fabric/journal | Dry-run no; deploy yes | 0 / 1 / 2 / 3; dry-run first, explicit deploy only [scripts/deploy_estate.py:169-182] |
| Advanced | `verify` → `verify_bindings.py` | workspace → optional JSON | No/network | 0 / 1 / 2 [scripts/verify_bindings.py:8-10,118-120] |
| Advanced | `feedback` → `build_migration_feedback.py` | run evidence → private bundle/payload | Yes/local | 0 / 1 / 2 / 3; never publish [scripts/build_migration_feedback.py:1580-1605] |
| Core | `status` / `next` / `resume` → `run_status.py` plus read-only gate artifacts (no stage execution) | selected run → observations / one legal next command; resume re-derives, then returns control to caller | No | native 0 / 1 / 2; no current resume script [scripts/run_status.py:972-987] |

## `status`, `next`, and resume semantics

- **PROPOSAL — read-only `status`:** derive afresh from `run.json`, engine/path receipts, manifests,
  credential audit/ACL, stored gate outputs, and existing `check_unit.py` results. The existing
  `run_status.py --run ABS [--json]` is read-only/non-certifying: exit 0 says inputs were assessable,
  not START_READY/COMPLETE; it does not establish current readiness. Its JSON schema v2 is not the
  proposed facade schema v1. Use `python -B`. [scripts/README.md:77; scripts/run_status.py:1-8,972-987]
- **PROPOSAL — `next`:** explain present evidence, blocking authority, proof limits, then emit exactly
  one legal command and its reason, or one human action at the first blocker. Never auto-advance or
  call a later-stage gate to override an earlier refusal.
- **PROPOSAL — contradictions/staleness:** current bytes and owning gate verdict outrank narrative,
  timestamps, directory presence, stale JSON, and a previous PASS. Re-run only the owning read-only
  gate where safe. Missing/changed evidence is `CANNOT-ESTABLISH`; absent baseline is
  `UNAVAILABLE`, never “no change.” [docs/agent-operations.md:14-29; docs/migration-phases.md:137-142]
- **PROPOSAL — resume:** select one explicit absolute run, `work_dirs.py --verify`, re-read artifacts,
  and derive the first legal step; rerun no completed stage by exit code alone. Refuse moved,
  unverifiable, contradictory, foreign-scope, or incomplete package evidence; never allocate a
  replacement or relocate the old run. `--select-run` changes the checkout navigation note, so it is
  not the read-only status operation. [scripts/work_dirs.py:74-89,1336-1366]
- JSON **PROPOSAL**, shared fields: `schema_version:1`, `command`, `outcome=OK|REFUSE|CANNOT_ESTABLISH|NEEDS_HUMAN|CHILD_FAILURE`, `native_exit`, `error:{code,message}`, `evidence[]`, `proof:{can[],cannot[]}`, `result`; stable per command.
  For `status`, derived fields only: `run:{recorded_path,physical_path,runs_parent,root_kind,git_visibility,commit_safety,integrity}`;
  `paths:{longest_file,longest_directory,file_remaining,dir_remaining,projection,rerun_command}`;
  `stages:[{name,state,authority,evidence[],native_exit}]`;
  `blocker:{gate,reason,human_action}`; `next:{command,why}`.
  Enums: `state=NOT_STARTED|PASS|REFUSE|CANNOT_ESTABLISH|STALE|UNAVAILABLE`;
  `integrity=intact|moved|unverifiable`; `root_kind=repo_local|short_external|unknown`;
  `projection=PASS|OVER_CEILING|UNKNOWN`. No writable cache, timestamp authority, or stored verdict.
- **Finding — current vocabularies differ:** oracle uses 0–5, `run_estate` uses 0–11 (including
  DoD=3), readiness uses 0/1/2/3, promotion uses 0–6/64, and credential `list` uses 0/1/3/4.
  [scripts/capture_tableau_oracle.py:2000-2007; docs/operator-runbook.md:793-810;
  docs/reference-readiness.md:77-85; scripts/promote_unit.py:64-73; scripts/README.md:86]

## State transitions

| From → to | Trigger; required evidence | Authority |
|---|---|---|
| Unallocated → allocated | selected scope + path projection; absolute `run.json` identity | **PROPOSAL:** facade, then `work_dirs.py` |
| Allocated → surveyed → scoped | complete survey JSON; assessment JSON + DB reconcile | `run_engine_survey.py`, `assess_estate.py` |
| Scoped → harvested → referenced | assets and parse-sweep; manifest records requested captures | `harvest_estate_assets.py`, capture owner |
| Referenced → converted | valid inputs + canonical engine receipt + DoD/path reports | `run_estate.py`, `engine_source.py` |
| Converted → credential-cleared | per-source probe/audit + verified ACL state | `credential_gate.py`; human for sign-in/authorize |
| Credential decision + converted → assembled → START_READY | verified probe-clear, or explicit human `authorized_model_only` fallback, plus package manifest/roles, issued brief, canonical data, fresh binding and ready reference. Fallback is model-only and remains UNVALIDATED, never evidence of live reachability. [scripts/credential_gate.py:1548,2264-2310; scripts/check_reference_readiness.py:1429-1458] | `credential_gate.py`; `package_unit.py`; `check_reference_readiness.py` |
| START_READY → built → refreshed | declared edits; structural TMDL/PBIR checks before refresh; persisted cache | builders; validation/refresh owners |
| Refreshed → fidelity PASS → COMPLETE | reference supports requested claim; current all-scope caller H | validator; `check_unit.py --scope all --receipt-sha256 H` |
| COMPLETE → promoted → deployed → verified | same H promotion; reviewed dry-run count; deployment journal; workspace binding check | `promote_unit.py`, `deploy_estate.py`, `verify_bindings.py` |

## Illegal transitions and current enforcement

| Illegal transition | Detection today; refusal/exit (or explicit gap) |
|---|---|
| Builder before credential decision | ACL blocks writes; `credential_gate.py verify` owns ACL/audit. No unified CLI transition guard exists; proposed wrapper requires verified clear or explicit human-authorized model-only build path. Missing/unreadable state is CANNOT-ESTABLISH. [scripts/README.md:86-87; scripts/credential_gate.py:2264-2310] |
| Promote without checked COMPLETE | `promote_unit.py` calls `check_unit.py`; nonzero refuses unless explicit `--force`, which records “PROMOTED unchecked.” Force is not proposed as a normal CLI path. [scripts/promote_unit.py:64-83,135] |
| Deploy without dry-run item-count agreement | **Gap:** `deploy_estate.py --dry-run` reports count but does not establish prior human agreement. Proposed CLI reruns dry-run and requires explicit matching count before separate deploy; no saved approval ledger. [scripts/README.md:93; docs/operator-runbook.md:497-510] |
| Partial existing-bundle rerun with changed engine version | `run_estate.py` rewrite barrier exit 9; `--accept-engine-version-change` is explicit acknowledgment, never auto-added. [docs/operator-runbook.md:807-809,855-870] |
| Refresh before model/TMDL validates | **Gap:** forwarding shim has no validation refusal/code. Proposed CLI invokes existing validator first; refuses on non-PASS, without reimplementing TMDL rules. [scripts/refresh_pbip_model.py:23-31; scripts/README.md:110] |
| Fidelity PASS from oracle imagery alone | Reference policy caps oracle images at layout/text; readiness is not comparison. `check_unit.py` retains non-pass when required validation grade/comparison is absent. [docs/reference-readiness.md:31-37,77-85] |
| Skip capture and still dispatch | Conversion may proceed; package entry gate returns 1 for findings or 3 CANNOT_ESTABLISH, never ready on missing evidence. [docs/reference-readiness.md:15-30,36-41] |
| Edit `<bundle>/reports/` baseline | Shared convention says never edit; generated-drift check may detect change, but there is no edit-prevention refusal code. Refuse/rebuild from baseline, don't bless drift. [AGENTS.md:522-523; scripts/README.md:97] |
| Move/rename a run or treat absent baseline as clean | `work_dirs.py --verify`: moved=1, unverifiable=3; missing baseline is UNAVAILABLE. Do not reallocate over the old evidence. [scripts/work_dirs.py:74-89; docs/migration-phases.md:91-99,137-142] |

## Shared exits and JSON vocabulary

**PROPOSAL:** wrapper exits: `0 OK`, `2 USAGE`, `10 REFUSE`, `11 CANNOT_ESTABLISH`, `12 NEEDS_HUMAN`,
`20 CHILD_FAILURE`, `130 INTERRUPTED`. JSON retains `native_exit`, `native_status`, owner evidence;
never rewrite native meaning. Oracle 2 means credential, argparse commonly uses 2; `run_estate` 3
means DoD failure, readiness 3 means CANNOT-ESTABLISH. Decode by subcommand, never globally.
[scripts/capture_tableau_oracle.py:2000-2007; docs/operator-runbook.md:793-810; docs/reference-readiness.md:77-85]

## Restart, brownfield, and path/location contract

- **PROPOSAL:** after dropped session, derive from artifacts, not agent summary. After crash, in-flight
  work is UNKNOWN: inspect target worktree `git status`, `git diff --stat`, and file mtimes against
  crash time before dispatch/replay. [docs/agent-operations.md:60-80]
- Existing bundle: require receipt and baselines; missing baseline is `UNAVAILABLE`; changed engine
  version or uncertain destructive scope refuses in-place run unless the owner script's explicit
  acknowledgment is supplied. Never change installed engine mid-migration; compare in a fresh output.
  [docs/operator-runbook.md:855-870; AGENTS.md:58-64]
- Existing `migrations/.../fabric/`: inspect as brownfield, do not overwrite or assume it equals
  package/bundle. Half-run estate resumes per unit and per authority; datasource providers precede
  consumers for shared models. [docs/migration-phases.md:34-39,230-243; docs/reference-readiness.md:120-135]
- Windows Desktop measured ceilings are **259 UTF-16 code units/file, 247/directory**; code measures
  UTF-16, not Python code points. `work_dirs.py --runs-parent <short-parent>` creates the same
  `_runs/<NNN>-<slug>` layout; `--verify` is run-identity gate, not path-length gate. [docs/windows-path-limits.md:76-78,100-112; scripts/work_dirs.py:17-27,1328-1334]
- **Our measured example, not a customer estimate:** Windows-path audit root was 90 characters; its
  longest bundle tail was 197 (deep PBIR `.../pbip/<name>/<name>.Report/.../visuals/<id>/visual.json`).
  `90 + 197 = 287`; file budget is 259, so this shape exceeds by 28 and tolerates root length only
  `259 - 197 = 62`. The directory ceiling is independently 247; real binding root budget is the
  minimum across file and directory tails (the same audit measured 62). [docs/windows-path-limits.md:116-150,437-450]
- **Gap / PROPOSAL:** current `run_estate.py` projects before conversion and measures emitted files,
  but `start_migration.py` allocates first. A facade computes conservative UTF-16 bounds before
  `work_dirs.allocate_run`; projection stays `UNKNOWN` until harvest supplies required names. Refuse
  default-root allocation if it cannot establish a safe bound; over ceiling prints the exact
  `t2p start ... --runs-parent <short-parent>` rerun. Never move old evidence.
  [scripts/run_estate.py:76-89,561-566; scripts/start_migration.py:465-503]
- **Owner requirement / PROPOSAL:** `start` prints absolute physical run path first and last.
  `status`/`next` always show recorded and physical paths, runs-parent, repo-local vs external,
  git visibility/commit safety, longest file+directory and remaining budget, projection state, and
  exact short-root rerun command. If same-user `subst` is active, display its alias as temporary and
  non-authoritative; never write it to `run.json`. Existing `subst` success is one fixture/host
  (#566), not a portability claim. [scripts/start_migration.py:503,430-446; docs/windows-path-limits.md:319-345,404-412]

## Gate authority and ambiguous surfaces

| Gate | Sole current owner; CLI behavior |
|---|---|
| Environment | `preflight.ps1`; report its critical result only. [docs/operator-runbook.md:119-129] |
| Run identity | `work_dirs.py --verify`; distinct from path ceilings. [scripts/work_dirs.py:80-89] |
| Engine identity / DoD / path | `engine_source.py`; `run_estate.py`; `check_path_ceiling.py` measurement. [scripts/engine_source.py:23-31; docs/operator-runbook.md:793-810; scripts/run_estate.py:76-89] |
| Source credential | `credential_gate.py`; probe earns clear, human owns authorization. [scripts/README.md:82,86-87] |
| Package entry / binding / reference | `check_reference_readiness.py`; consumes existing authorities. [docs/reference-readiness.md:90-105,137-142] |
| Unit completion / promotion | `check_unit.py` is sole COMPLETE authority; `promote_unit.py` forwards same caller H. [docs/migration-phases.md:363-377,394-400] |
| TMDL/PBIR structural and refresh | existing validators and refresh script/skill; wrapper must not substitute a pass. [scripts/README.md:110; scripts/refresh_pbip_model.py:1-10] |
| Fidelity / deployed binding | `pbi-migration-validator` for comparison; `verify_bindings.py` only for binding. [docs/reference-readiness.md:31-37; scripts/README.md:94] |
| Deployment | `deploy_estate.py`; deploy only on explicit verb. [scripts/README.md:93] |

**Findings:** `run_status.py` exit 0 means assessable diagnostics, while readiness and completion
have their own gates; treating those as one “status” authority would overclaim. [scripts/run_status.py:1-8;
docs/reference-readiness.md:77-85; docs/migration-phases.md:363-377] `start_migration.py` exit 0 means bundle +
references captured only, while `check_reference_readiness.py` decides package dispatch; both are
accurate but easily conflated. [scripts/start_migration.py:7-9; scripts/README.md:43-49] `run_estate.py` returns exit 10
for both over-ceiling and unassessable path states; shared CLI must inspect its report and not flatten
them. [docs/operator-runbook.md:807-810] `capture_tableau_reference.py --server-rest` differs from the supported
oracle path despite its name; present the actual route, not a guessed alias. [scripts/capture_tableau_reference.py:579-583; scripts/README.md:113-115]

## Anti-features and rejected alternatives

- No new state DB, stage ledger, lock, or second source of truth; derive from `run.json`, receipts,
  gate outputs, and manifests.
- No duplicated readiness/credential/fidelity/deploy policy; invoke the owning script and preserve
  evidence/native exit. No engine fallback. [scripts/engine_source.py:23-31,100-113]
- No mid-migration tooling upgrades, autopilot credential clearance, implicit deployment/publish,
  run deletion/relocation, or `git add -A`. [AGENTS.md:58-64; docs/migration-phases.md:45-48;
  docs/windows-path-limits.md:3-9]
- Reject a new state database (drift); replacing scripts with a second implementation (split
  authority); resuming by timestamps alone (not proof); and auto-running every “next” stage (can
  cross credential, fidelity, or deployment boundaries).

## Open questions / not verified

- ❌ No `t2p` facade or `resume` command was found; command names and shared exit mapping are proposals.
- ⚠️ V1 invocation remains open: `python scripts/t2p.py` versus installed `t2p`; Windows, cloud-agent and copied-toolkit behavior, persona cleanup, mutation tests, and blind-run comparison are not established by this design doc. [Issue #734: Questions to investigate, Acceptance]
- ❌ No live Tableau, Fabric, credentials, Windows Desktop, or refresh was exercised; findings here
  are static source/document inspection only. Startup preflight could not run because this Linux
  sandbox has no `powershell` executable (exit 127), not because of an execution-policy refusal.
- ⚠️ How a stable existing dry-run item-count agreement is represented without a new ledger remains
  a UX decision; proposal is fresh dry-run plus explicit count in the deploy invocation.
