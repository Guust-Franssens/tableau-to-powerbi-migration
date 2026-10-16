# Issue #734 — CLI design synthesis

## Executive verdict

**Narrow pilot, not a lifecycle-wide CLI.** Four design reports converge on a thin facade; the red-team report finds no measured proof that a broad CLI improves migration outcomes and identifies real compatibility costs. Pilot the already-landed live-site/project front door for the same Tableau Cloud project and oracle as blind run 1 (`20 - Supply Chain`), with read-only `status`; do not include `next` in v1. This pilot is conditional on safe pre-network path projection and explicit first-failure/native-exit behavior; the current front door does not fully establish the latter. Compare like-for-like before expansion. Do not call a local-file trial “blind run 2” against the live project baseline or retire script guidance yet.

Method: read issue #734, its path-contract comment, and all five reports in full. Counted proposals by report, not by repeated mentions; “support” means support for the capability, not agreement on its CLI name. Checked source where reports differ. ✅ = source-verified; ⚠️ = design inference/proposal; ❌ = unsupported or known gap. The red-team report is a challenge to the premise, not an ordinary command vote.

## Consensus scorecard

`CJ` customer-journeys; `LS` lifecycle-state; `UX` agent-ux; `IA` implementation-architecture; `RT` red-team. `+` proposes/supports; `~` discusses capability but not the same command; `×` rejects; `—` absent. Three or more compatible proposals = consensus; three or more capability votes with different grouping/names = qualified consensus.

| Candidate | CJ | LS | UX | IA | RT | Existing owner (current) | Classification and adjudication |
|---|---:|---:|---:|---:|---:|---|---|
| `start` | + | + | + | + | × | `start_migration.py` | **Qualified consensus:** common front door, but scope coverage differs. Pilot only the already-landed live project route. |
| `status` | + | + | + | + | × | `run_status.py`; `work_dirs.py --verify` | **Consensus:** read-only artifact observations; never equate diagnostic success with readiness. |
| `next` | + | + | + | + | × | Existing artifacts and owning gates; no dedicated script | **Consensus capability, deferred from v1:** only a future suggestion-only router; no execution, PASS, authorization, or agent dispatch. |
| `why` / explain finding | — | ~ | + | ~ | — | Owner gate output/docs; no standalone `why` | **Minority:** optional evidence-owner/doc explanation; no new verdict and not needed for the pilot. |
| `paths` | + | + | + | + | × | `work_dirs.py`; `check_path_ceiling.py` | **Qualified consensus:** path fields are mandatory; standalone verb versus always-visible `status` is unsettled. Embed in pilot output. |
| `doctor` / preflight | + | + | + | + | × | `preflight.ps1` | **Consensus capability:** report the existing preflight result. It cannot replace session-start preflight or upgrade tools mid-run. |
| `allocate` / `init` | ~ | + | + | + | — | `work_dirs.py` | **Qualified consensus:** allocation is needed, but normally belongs inside `start`; standalone allocation is advanced/operator-only. |
| `scope` | ~ | ~ | — | + | — | `start_migration.py` selectors; `assess_estate.py` (post-survey) | **Minority command:** only IA names a standalone verb; source identities/assessment remain owner-derived, and current assessment is site-wide. |
| `runs list` / `runs verify` | ~ | ~ | + | ~ | — | `work_dirs.py` listing/`--verify`; `run_status.py` for artifact observations | **Minority command:** explicit UX proposal; other reports support related path/run checks but not a common runs command or scope. Verify run identity, not path length/readiness. |
| `survey`, `plan` / assess-lineage, `harvest`, `convert` / build | ~ | + | + | + | — | `run_engine_survey.py`; `assess_estate.py`; `tableau_lineage.py`; `harvest_estate_assets.py`; `run_estate.py` | **Qualified consensus:** retain existing stages as advanced escapes; reports differ on whether the facade exposes or composes each. |
| reference capture | + | + | + | + | — | `capture_tableau_reference.py`; `capture_tableau_oracle.py` | **Qualified consensus:** manual/local and Tableau oracle capture are distinct routes/evidence; do not flatten their grades. |
| `unit prepare` / package | + | + | + | + | × | `package_unit.py`; readiness owned by `check_reference_readiness.py` | **Qualified consensus:** package assembly is not `START_READY`; naming and whether core differ. Defer from pilot. |
| `validate` / check | + | + | + | + | × | `check_reference_readiness.py`; `check_unit.py` | **Qualified consensus:** reference readiness and unit `COMPLETE` are separate gates/scopes, not one CLI verdict. |
| `credentials list` / `reprobe` | + | + | + | + | × | `credential_gate.py`; `reprobe_blocked.py` / `probe_live_source.py` | **Qualified consensus:** read-only listing and an explicit probe are different actions; no credential authority in the facade. |
| `refresh` | + | + | ~ | + | — | `refresh_pbip_model.py` / refresh skill | **Qualified consensus:** delegate only; refresh modes/progress and model validation are not a new CLI policy. |
| `promote` | + | + | — | + | × | `promote_unit.py`, checked against `check_unit.py` | **Qualified consensus:** three reports support a promotion capability, but its scope/placement differs and red-team rejects promotion that hides checked review; retain receipt-pinned owner gate. Not pilot scope. |
| `deploy` dry-run / apply | + | + | + | + | × | `deploy_estate.py` | **Qualified consensus:** dry-run count and explicit deployment are separate; never auto-deploy or imply prior human approval. |
| `verify` bindings | + | + | + | + | — | `verify_bindings.py` | **Consensus capability:** binding check only, not data parity or fidelity. |
| `resume` | × | + | + | ~ | × | `work_dirs.py --verify`; `run_status.py`; no replay owner | **Minority:** customer journeys rejects a separate resume; prefer explicit verified status followed by the owning command, never timestamp-driven replay. |
| `brief` / dispatch card | ~ | ~ | + | + | — | Existing migration brief; Copilot task tool dispatches | **Minority:** may print task context; Copilot task tool alone dispatches. |
| `help` registry / shell completion | — | + | + | + | — | Existing argparse help, `scripts/README.md`; no unified registry | **Consensus capability:** at least three reports support discoverable help/registry, but not a common invocation or completion requirement; no v1 registry requirement. |
| costs / `migration_cost_report` | — | — | — | + | — | `migration_cost_report.py` | **Minority:** existing specialized report only. |
| feedback bundle | — | + | — | + | — | `build_migration_feedback.py` | **Minority:** existing private evidence/payload builder; never publish from a CLI. |
| full-server / multi-site fan-out | + | ~ | — | — | — | No supported front door | **Minority:** no supported front door or ownership contract established; exclude. |
| `run-all` / autonomous migrate | — | × | — | — | × | No single owner; would cross existing gates | **Rejected:** crosses human credential, fidelity and deployment boundaries; duplicates authorities. |
| new stage DB/ledger, gate-owning `status`, generic `exec`, CLI credential authorization | × | × | × | × | × | Explicitly none; existing artifact/gate owners retain authority | **Rejected:** duplicates authority, hides evidence, or fails red-team criteria. |

The reports agree more strongly on *invariants* than on a command tree: delegate to existing owners, show evidence and limits, preserve native results, and keep direct scripts available. ✅ `run_status.py` is explicitly diagnostic/non-certifying; `start_migration.py` exit 0 is capture-only, not completion (`scripts/run_status.py:1-8,972-987`; `scripts/start_migration.py:7-9`; `scripts/README.md:38-49,64,77`).

## Recommended v1 tree (pilot)

```text
t2p start --project <exact-name|LUID> [--env <path>] [--runs-parent <short-root>] [--json]
t2p status --run <absolute-run-path> [--json]
```

V1 `start` delegates to the existing live-site/project `start_migration.py` route; configured server/site and PAT secret come from the existing environment/`.env`, never argv. Its exact comparison target is the same project/oracle used in blind run 1. `status` reports read-only observations. No local file, folder, datasource, whole-site, full-server, `next`, package, builder, refresh, validation, promotion, deploy, or dispatch command in this pilot. Keep existing scripts as the advanced/recovery surface. Path fields are part of both commands; `next` remains deferred.

### Exact v1 command semantics

Common JSON envelope (proposed, `schema_version: "t2p/v1"`):

```json
{"schema_version":"t2p/v1","command":"start|status","status":"ok|findings|cannot_establish|error|interrupted","exit_code":0,"authority":"script-or-gate|null","native_exit_code":null,"evidence":[],"artifacts":[],"run_location":null,"path_report":null,"proof_boundary":{"established":[],"not_established":[]}}
```

`evidence` entries identify the observed artifact and its owning script/gate; `artifacts` are references, not copied state. Additive fields are compatible; incompatible shape changes require a new schema. The facade must not parse child prose as evidence. In JSON mode, stdout is one envelope; child progress and diagnostics go to stderr. Human mode streams child output and prints absolute physical run path at start and finish.

| Command | Inputs and selector | Artifact / authority | Human output and JSON | Result/exit vocabulary | Cannot prove |
|---|---|---|---|---|---|
| `start` | One exact Tableau project name/LUID, optional environment file and runs-parent; server/site resolved from existing environment. PAT secret only in environment/`.env`. | Delegate to `start_migration.py`; its existing owners allocate the run, survey/assess/harvest/capture references and build the bundle. Preserve each artifact and stage evidence. | Print actual physical run path first/last, project selector, owner/stage result, artifacts and full path report. JSON uses common envelope plus artifacts; do not echo secrets or server values. | A future facade-owned multi-child chain must stop at the first failing child, return its exact code plus owner/stage, retain earlier artifacts, and report failure—not success. No global numeric interpretation. | Exit 0 is bundle + references captured only, not fidelity, `START_READY`, `COMPLETE`, or deployability (`scripts/start_migration.py:7-8`). |
| `status` | One explicit absolute run path; no implicit “latest”. | `work_dirs --verify` plus read-only `run_status.py`; artifacts/receipts/manifests and path scan remain their owners. No mutation. | Report observations, missing/contradictory evidence, gate owner/native result, recorded and physical location, and full path report. JSON `status` may be `findings` or `cannot_establish`; diagnostic success is not readiness. | Preserve native diagnostic result; moved/unverifiable identity is refusal/cannot-establish according to owner output. Never invent a global interpretation of child codes. | Does not freshly certify credential status, `START_READY`, `COMPLETE`, current fidelity, or successful deployment. |

Stable outer statuses are descriptive only; preserve child/gate `native_exit_code` and owner/stage. Any facade-owned multi-child command stops at the first nonzero child, returns that child’s exact code plus owner/stage, retains earlier artifacts, and never reports success. Do not globally map numeric codes: oracle, readiness, bundle, package, and promotion reuse numbers with different meanings (`scripts/capture_tableau_oracle.py:2000-2007`; `scripts/run_estate.py:204-215`; `docs/reference-readiness.md:77-85`; `scripts/promote_unit.py:64-73`).

⚠️ **Front-door compatibility gap:** `start_migration.py` is currently one delegated process, but internally it continues to bundle after a reference-capture failure and `_finalize` synthesizes 1/3 rather than returning that stage’s exact exit (`scripts/start_migration.py:613-674,691-705`). Do not claim first-failure semantics for that existing route. Enabling the proposed `t2p start` under the strict multi-child contract depends on the front-door owner exposing first-failure stop and native stage code (or an explicitly approved exception); until then, the mismatch is a pilot blocker, not something the facade should mask.

## Input-shape matrix

| Input/scope | Decision | Existing route/evidence |
|---|---|---|
| One local `.twb`/`.twbx`, optional supplied screenshots | **Defer; not v1.** `run_estate.py` takes a folder; local capture expects a slug directory with `migration-spec.json` and `reference/`. Exact-head review evidence reports single-file `run_estate --dry-run` exit 10 while an isolated folder succeeded. Single-file isolation, screenshot staging/adoption, recorded provenance, and failure retention are unproven; a later route must define them and delegate parsing/reference/engine checks to their owners. | Parser accepts both formats, but that is not a complete local front door (`scripts/parse_tableau.py:1-6`; `scripts/run_estate.py:5,3493-3548`; `scripts/capture_tableau_reference.py:9,174,571-607`; review comment #6222509572). |
| Standalone `.tds`/`.tdsx` | **Defer.** Parser accepts it, but do not claim model-only end-to-end package/readiness/refresh/completion. | Model-first datasource has no report; workbook-centric `start_migration.py` does not support datasource-only scope (`scripts/parse_tableau.py:1-6`; `docs/migration-phases.md:186-190`; `scripts/README.md:38-49`). |
| Local folder | **Defer from pilot; supported direct route remains.** One `run_estate` run emits per-workbook handovers; it is not a server survey. | `AGENTS.md:213-218`; `scripts/README.md:76`. |
| One site workbook | **Defer:** exact workbook name/LUID using existing project/site environment; identity joins must be verified. | `scripts/start_migration.py:71-80,451-550,585-649`. |
| One project | **Pilot:** exact name/LUID for the same project and Tableau oracle used in blind run 1 (`20 - Supply Chain`); assessment is site-wide, so this is not a project-only assessment. | `scripts/start_migration.py:74-80,480-499`; run-1 comparator supplied in PR review comment #6222509572. |
| Whole site | **Defer:** front door supports one configured site; not in the project-pilot v1 scope. | `scripts/start_migration.py:451-538`; `scripts/README.md:38-49`. |
| Full server / multiple sites | **Exclude:** no supported enumeration/fan-out or cross-site run-ownership contract. If later added, separate site child runs and require explicit scope/confirmation. | `scripts/start_migration.py:480-489`; `scripts/README.md:41-49`. |
| Existing/brownfield run, dropped session, hand-edited package | **Status only after explicit path and positive identity verification.** Do not replay from timestamps or overwrite hand edits. | `scripts/work_dirs.py:74-89,1300-1397`; `docs/migration-phases.md:120-142,408-420`; `AGENTS.md:278-289`. |

## Lifecycle coverage and legal transitions

The pilot covers allocation → survey/assessment/harvest/reference capture/bundle attempt → evidence-derived `status`. Later stages remain explicit owner routes, not hidden automation:

| Legal transition | Required evidence and authority | Facade boundary |
|---|---|---|
| Unallocated → allocated → surveyed/scoped → harvested/referenced → converted | `work_dirs.py`; survey/assessment/harvest/capture artifacts; `run_estate.py` receipt and DoD/path result. | Pilot uses existing live project route; requires safe pre-survey path projection. |
| Converted → credential decision → package assembled → `START_READY` | Source probe plus `credential_gate.py`; `package_unit.py`; `check_reference_readiness.py`. `authorize` means human-authorized model-only/unvalidated, never reachable. | Later advanced commands may delegate; never create/clear credential state in facade. |
| `START_READY` → semantic/report build → refresh → fidelity validation → `COMPLETE` | Builders own TMDL/PBIR; refresh owner persists cache; validator compares shipped bytes against attributable Tableau evidence; only `check_unit.py` all-scope with caller-held receipt establishes `COMPLETE`. | Copilot builders/task tool remain human-directed; wrapper cannot author, validate fidelity, or assert completion. |
| `COMPLETE` → promotion → deployment → binding verification | `promote_unit.py` with same receipt; reviewed deploy dry-run count then explicit `deploy_estate.py`; read-only `verify_bindings.py`. | No implicit promotion/deploy. Binding is not fidelity or data parity. |

Invalid/unknown evidence means refuse or `cannot_establish`, not an invented transition. Examples: moved run; missing baseline; credential modal; package edits that would be overwritten; engine-version mismatch; oracle imagery alone offered as fidelity; deployment without an explicit human action. Owners and details: `docs/migration-phases.md:356-420`; `scripts/credential_gate.py:2491-2534`; `docs/reference-readiness.md:31-37,77-85`; `scripts/deploy_estate.py:169-182`.

## Path/location contract

✅ Owner requirement: Windows Desktop ceilings are **259 UTF-16 code units per file path and 247 per directory**; do not round to generic 260 (`docs/windows-path-limits.md:76-78,100-112,265-271`). `work_dirs.py` records canonical run identity and verifies moved/unverifiable locations (`scripts/work_dirs.py:17-27,74-89`).

Every v1 `start` and `status` human/JSON result must expose (a later `next`, if approved, must expose the same):

- recorded canonical absolute run path and runs-parent, actual physical path, and repo-local vs short external root;
- git visibility/commit safety; longest current file and directory paths and remaining file/directory budgets;
- pre-engine projection `PASS`, `OVER_CEILING`, or `UNKNOWN` until sufficient evidence; no root-only measurement;
- when over, the exact command for a **new** run using `--runs-parent <short-root>` and original selectors; old run retained, never moved;
- if active, a same-user `subst` alias shown separately as temporary/non-authoritative and never written to `run.json`.

`run_estate.py` has a projection and emitted-tree check, but the live front door reaches conversion’s path gate after survey/harvest (`scripts/run_estate.py:492-585,3615-3711`; `scripts/start_migration.py:585-649`). ⚠️ Therefore the project pilot is conditional on a pre-survey/harvest projection of `PASS`; this is a required pilot guard, not a claim that the current front door already provides it. If projection is `UNKNOWN`, do not start network/download work. A short-root retry is a new allocation; `subst` waives no ceiling or gate (`docs/windows-path-limits.md:319-371,408-417`).

## Advanced commands and internal scripts

If a later pilot expands, keep survey → assessment/lineage → harvest → capture → convert as explicit advanced routes or compose existing front doors; preserve all native outcomes and per-stage evidence. Package/readiness (`package_unit.py`, `check_reference_readiness.py`), credentials/probes (`credential_gate.py`, `probe_live_source.py`, `reprobe_blocked.py`), refresh, promotion, deploy, and verify stay delegated to their current owners. `check_unit.py` all-scope and caller-held receipt alone owns `COMPLETE` (`docs/migration-phases.md:356-420`).

Semantic/report construction and fidelity are agent work, not CLI mutations. A future `next` is deferred from v1 because no existing owner emits a legal next command. If later approved, a small non-certifying routing registry may emit suggestions only: require a positively verified run identity plus current, readable `run.json` and the specific stage artifact/gate output needed by the selected route; include evidence and owner. Missing, stale, contradictory, or unknown evidence yields `CANNOT_ESTABLISH` and no suggestion. A suggestion never means PASS, authorization, or execution; Copilot task tool remains the dispatch authority. Cost and feedback remain specialized tools; feedback builds a private bundle/payload and does not publish (`scripts/README.md:65,94-96`; `scripts/build_migration_feedback.py:1580-1605`).

Internal helpers stay out of agent help, not deleted or made unreachable. Preserve direct script compatibility for CI/recovery. In particular, current layout tests require every tracked `scripts/` file (except README) to remain inventoried (`tests/test_repo_layout.py:290-319`).

## Rejected commands / anti-features

- `migrate`, `run-all`, auto-`next`, or full-server fan-out: hides human decisions and combines distinct scopes/gates.
- `resume` that replays stages, chooses “latest”, or trusts timestamps; use verified `status` and then consult the owning gate/script.
- `status` that owns/caches readiness, credential, fidelity, or completion verdicts; any second DB, stage ledger, receipt, or readiness definition.
- Credential entry/storage/`authorize`, PAT arguments, automatic retry past a human sign-in stop, or tool upgrades during a migration.
- Build commands that edit TMDL/PBIR; deploy/promote that hides dry-run, receipt, review, or explicit approval; generic `exec`.
- A façade that swallows native output/code, treats `start` success as completion, or treats binding/reference presence as fidelity.

## Incremental slices and dependencies

1. **Proof before broad build:** resolve the draft-doc/index-check conflict; agree pilot scope and path contract. Do not change `docs/INDEX.md` in this one-file synthesis.
2. **Local-workbook vertical pilot:** one source shape, optional reference, canonical `run.json`, conservative pre-work path check, and direct-script positive/negative oracle. Do not add a console entrypoint until install/root behavior is proven; reports disagree on `python scripts/t2p.py` versus installed `t2p`.
3. **Read-only `status` first; consider `next` separately:** verify path identity and report current owner outputs. Add a suggestion-only routing registry only after its artifact prerequisites and unknown behavior are directly tested; it never maintains state or issues PASS/authorization.
4. **Only after blind run 2:** consider additional input shapes and one owner-routed lifecycle slice at a time (package/validate, then deploy/verify); preserve direct script behavior and independent gate tests. Do not start with every verb or a registry that claims untested capability.

Any implementation tests must compare the same script, argv, environment and artifacts directly versus through the facade, including refusal/nonzero and long-running output. Blind run 2 must use the same live project scope and Tableau oracle as run 1 in an independent session and compare tool calls, elapsed time, false claims, and fidelity. Remove/disable a required route and sever its gate link in isolated test copies; each mutation must fail the intended assertion, not merely exit nonzero.

## Documentation reduction

Only after the pilot wins blind run 2: make the supported journey and short command tree discoverable from `AGENTS.md`, personas, and `scripts/README.md`; replace repeated selection recipes with links to generated/verified help, retaining a full direct-script inventory and advanced recovery commands. Personas should retain role ownership, hard boundaries, evidence, and task-tool dispatch—not just a command list. Do not remove `docs/INDEX.md` entries or alter scripts inventory until their completeness checks and callers are migrated. ✅ A new unindexed Markdown page may fail `scripts/check_navigation_index.py` / CI; this one-file constraint prevents fixing that here (`docs/INDEX.md:45-47`; `scripts/check_navigation_index.py:50-66`; `.github/workflows/checks.yml:83-84`).

## Owner decisions still required

1. Does measured discovery pain justify any implementation before the same-project two-run comparison, given the red-team report found no evidence that script selection caused migration failures?
2. Approve the live-site/project pilot against run 1’s same Tableau Cloud project (`20 - Supply Chain`) and oracle; keep local files, `.tds[x]`, folders, whole site, and full server deferred?
3. What evidence can establish a safe *pre-network* projection for each remote scope? Is `UNKNOWN` a hard stop?
4. Should v1 remain `python scripts/t2p.py` (no packaging) or become an installed entrypoint after a separate environment contract?
5. Should outer exits pass through native child codes (with only facade syntax/launch/interrupt codes), or normalize them? How are dry-run count agreement and a human-authorized action represented without a ledger?
6. When may docs/personas stop listing scripts, and who resolves the unindexed-file/one-file-scope conflict?

## Blind run 2 acceptance

Blind run 2 must repeat the **same live Tableau Cloud project scope** as run 1 (`20 - Supply Chain`), using the same oracle/reference coverage and comparable environment, through the proposed `t2p start --project <exact-name|LUID>` and `status` route. The independent cold session gets only CLI help/output, not source or the full README. Compare elapsed time, tool calls, scope correctness, claims/limitations, retained evidence, and fidelity against run 1. Pass only if tool calls or elapsed time improve with **no** increase in false claims, missing evidence, gate bypass, or fidelity gaps; report inconclusive if scope/environment/oracle differ. A separate local-file experiment needs its own direct-script baseline first and cannot be labeled blind run 2 against the live-project run.

## Dissent retained

The red-team recommendation is **STOP**, not “build a CLI”: current evidence establishes a large script inventory and real stale-help/navigation defects, but not that a new runtime reduces agent effort, false claims, or fidelity failures. It also warns that the new file is unindexed, direct script callers remain widespread, and the facade risks duplicating authority. Those are blocking conditions for broad rollout, not objections to be voted away. A narrow pilot is justified only as a falsifiable experiment; if no measurable net reduction or safe path projection is demonstrated, stop rather than expand. (Red-team evidence: `pyproject.toml:45-50`; `scripts/README.md:1-5,38-52,62-77`; `tests/test_scripts_help.py:7-18,102-123`; `.github/workflows/checks.yml:73-97,124-153`.)
