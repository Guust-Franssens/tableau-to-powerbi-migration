# Issue #734 — CLI design synthesis

## Executive verdict

**Narrow pilot, not a lifecycle-wide CLI.** Four design reports converge on a thin, artifact-reading facade; the red-team report correctly finds no measured proof that a broad CLI improves migration outcomes, and identifies real compatibility and validation costs. Pilot only a local single-workbook route plus read-only `status`/`next`; decide whether to expand after blind run 2. Do not present the pilot as the “only” supported interface or retire script guidance yet.

Method: read issue #734, its path-contract comment, and all five reports in full. Counted proposals by report, not by repeated mentions; “support” means support for the capability, not agreement on its CLI name. Checked source where reports differ. ✅ = source-verified; ⚠️ = design inference/proposal; ❌ = unsupported or known gap. The red-team report is a challenge to the premise, not an ordinary command vote.

## Consensus scorecard

`CJ` customer-journeys; `LS` lifecycle-state; `UX` agent-ux; `IA` implementation-architecture; `RT` red-team. `+` proposes/supports; `~` discusses capability but not the same command; `×` rejects; `—` absent. Three or more compatible proposals = consensus; three or more capability votes with different grouping/names = qualified consensus.

| Candidate | CJ | LS | UX | IA | RT | Existing owner (current) | Classification and adjudication |
|---|---:|---:|---:|---:|---:|---|---|
| `start` | + | + | + | + | × | `start_migration.py`; local `parse_tableau.py` / `run_estate.py` | **Qualified consensus:** common front door, but local/site coverage differs. Pilot only the single local workbook route. |
| `status` | + | + | + | + | × | `run_status.py`; `work_dirs.py --verify` | **Consensus:** read-only artifact observations; never equate diagnostic success with readiness. |
| `next` | + | + | + | + | × | Existing artifacts and owning gates; no dedicated script | **Consensus:** propose one evidence-justified legal action; do not execute it or dispatch an agent. |
| `why` / explain finding | — | ~ | + | ~ | — | Owner gate output/docs; no standalone `why` | **Minority:** optional evidence-owner/doc explanation; no new verdict and not needed for the pilot. |
| `paths` | + | + | ~ | ~ | × | `work_dirs.py`; `check_path_ceiling.py` | **Qualified consensus:** path fields are mandatory; standalone verb versus always-visible `status`/`next` is unsettled. Embed in pilot output. |
| `doctor` / preflight | + | + | + | + | × | `preflight.ps1` | **Consensus capability:** report the existing preflight result. It cannot replace session-start preflight or upgrade tools mid-run. |
| `allocate` / `init` | ~ | + | + | + | — | `work_dirs.py` | **Qualified consensus:** allocation is needed, but normally belongs inside `start`; standalone allocation is advanced/operator-only. |
| `survey`, `plan` / assess-lineage, `harvest`, `convert` / build | ~ | + | + | + | — | `run_engine_survey.py`; `assess_estate.py`; `tableau_lineage.py`; `harvest_estate_assets.py`; `run_estate.py` | **Qualified consensus:** retain existing stages as advanced escapes; reports differ on whether the facade exposes or composes each. |
| reference capture | + | + | + | + | — | `capture_tableau_reference.py`; `capture_tableau_oracle.py` | **Qualified consensus:** manual/local and Tableau oracle capture are distinct routes/evidence; do not flatten their grades. |
| `unit prepare` / package | + | + | ~ | + | × | `package_unit.py`; readiness owned by `check_reference_readiness.py` | **Qualified consensus:** package assembly is not `START_READY`; naming and whether core differ. Defer from pilot. |
| `validate` / check | + | + | + | + | × | `check_reference_readiness.py`; `check_unit.py` | **Qualified consensus:** reference readiness and unit `COMPLETE` are separate gates/scopes, not one CLI verdict. |
| `credentials list` / `reprobe` | + | + | + | + | × | `credential_gate.py`; `reprobe_blocked.py` / `probe_live_source.py` | **Qualified consensus:** read-only listing and an explicit probe are different actions; no credential authority in the facade. |
| `refresh` | + | + | ~ | + | — | `refresh_pbip_model.py` / refresh skill | **Qualified consensus:** delegate only; refresh modes/progress and model validation are not a new CLI policy. |
| `promote` | + | + | — | + | × | `promote_unit.py`, checked against `check_unit.py` | **Minority/qualified capability:** preserve caller-held receipt and checked promotion; not pilot scope. |
| `deploy` dry-run / apply | + | + | + | + | × | `deploy_estate.py` | **Qualified consensus:** dry-run count and explicit deployment are separate; never auto-deploy or imply prior human approval. |
| `verify` bindings | + | + | + | + | — | `verify_bindings.py` | **Consensus capability:** binding check only, not data parity or fidelity. |
| `resume` | ~ | + | + | ~ | × | `work_dirs.py --verify`; `run_status.py`; no replay owner | **Minority:** prefer `status` + `next` on an explicitly selected, verified run; no timestamp-driven replay. |
| `brief` / dispatch card | ~ | ~ | + | + | — | Existing migration brief; Copilot task tool dispatches | **Minority:** may print task context; Copilot task tool alone dispatches. |
| `help` registry / shell completion | — | ~ | + | + | — | Existing argparse help, `scripts/README.md`; no unified registry | **Minority:** useful discovery option, not evidence for a runtime facade or v1 requirement. |
| costs / `migration_cost_report` | — | — | — | + | — | `migration_cost_report.py` | **Minority:** existing specialized report only. |
| feedback bundle | — | + | — | + | — | `build_migration_feedback.py` | **Minority:** existing private evidence/payload builder; never publish from a CLI. |
| full-server / multi-site fan-out | + | ~ | — | — | — | No supported front door | **Minority:** no supported front door or ownership contract established; exclude. |
| `run-all` / autonomous migrate | — | × | — | — | × | No single owner; would cross existing gates | **Rejected:** crosses human credential, fidelity and deployment boundaries; duplicates authorities. |
| new stage DB/ledger, gate-owning `status`, generic `exec`, CLI credential authorization | — | × | × | × | × | Explicitly none; existing artifact/gate owners retain authority | **Rejected:** creates competing authority, hides evidence, or fails red-team criteria. |

The reports agree more strongly on *invariants* than on a command tree: delegate to existing owners, show evidence and limits, preserve native results, and keep direct scripts available. ✅ `run_status.py` is explicitly diagnostic/non-certifying; `start_migration.py` exit 0 is capture-only, not completion (`scripts/run_status.py:1-8,972-987`; `scripts/start_migration.py:7-9`; `scripts/README.md:38-49,64,77`).

## Recommended v1 tree (pilot)

```text
t2p start --input <one.twb|one.twbx> [--reference <dir>] [--runs-parent <short-root>] [--json]
t2p status --run <absolute-run-path> [--json]
t2p next   --run <absolute-run-path> [--json]
```

`paths` is required information in `start`, `status`, and `next`, not a separate v1 verb. No `doctor`: the required session-start PowerShell preflight remains; the plain preflight is available directly. No folder, datasource, remote site, package, builder, refresh, validation, promotion, deploy, or dispatch command in this pilot. Existing scripts remain the supported advanced/recovery surface. ⚠️ A local-workbook pilot is a proposed comparator, not a currently shipped `t2p` route.

### Exact v1 command semantics

Common JSON envelope (proposed, `schema_version: "t2p/v1"`):

```json
{"schema_version":"t2p/v1","command":"start|status|next","status":"ok|findings|cannot_establish|error|interrupted","exit_code":0,"authority":"script-or-gate|null","native_exit_code":null,"evidence":[],"artifacts":[],"run_location":null,"path_report":null,"next_command":null,"proof_boundary":{"established":[],"not_established":[]}}
```

`evidence` entries identify the observed artifact and its owning script/gate; `artifacts` are references, not copied state. Additive fields are compatible; incompatible shape changes require a new schema. The facade must not parse child prose as evidence. In JSON mode, stdout is one envelope; child progress and diagnostics go to stderr. Human mode streams child output and prints absolute physical run path at start and finish.

| Command | Inputs and selector | Artifact / authority | Human output and JSON | Result/exit vocabulary | Cannot prove |
|---|---|---|---|---|---|
| `start` | Exactly one local `.twb` or `.twbx`; optional reference directory and short root. No secrets in argv. | Allocate canonical run through `work_dirs.py`; preserve source/reference paths; delegate parsing/conversion to existing parser/engine route. Refuse before work if a safe path projection cannot be established. | Show chosen input, adopted reference records/provenance (if any), absolute physical path first/last, path report, child evidence and one next action. JSON uses common envelope plus artifacts. | Return native child code unchanged; wrapper syntax error 64, launch error 70, interrupt 130. `status` records child result, never upgrades it. | Supplied screenshots prove neither depicted object/state nor fidelity; successful parse/conversion is not `START_READY`, `COMPLETE`, refresh, fidelity, or deployability. |
| `status` | One explicit absolute run path; no implicit “latest”. | `work_dirs --verify` plus read-only `run_status.py`; artifacts/receipts/manifests and path scan remain their owners. No mutation. | State what was observed, missing/contradictory evidence, gate owner/native result, recorded and physical location, and full path report. JSON `status` may be `findings` or `cannot_establish`; diagnostic success is not readiness. | Preserve native diagnostic result; moved/unverifiable identity is refusal/cannot-establish according to owner output. Never invent a global interpretation of child codes. | Does not freshly certify credential status, `START_READY`, `COMPLETE`, current fidelity, or successful deployment. |
| `next` | Same explicit run path; no run creation or stage execution. | Re-read `status` evidence and identify the existing owner’s gate/action. Copilot task tool remains dispatch authority. | Give at most one legal next command or human action, its reason, owner, and proof boundary; if evidence/authority is unclear, say cannot establish and provide no “legal” command. Include same path report. JSON `next_command` is null unless justified. | Same outer outcomes; never convert an owner’s refusal, human credential stop, unknown, or child failure to OK. | Does not execute, authorize, certify, infer from timestamps, or guarantee that a human/agent will complete the action. |

Stable outer statuses are descriptive only; preserve child/gate `native_exit_code` and owner. Do not globally map child code numbers: oracle, readiness, bundle, package, and promotion reuse numbers with different meanings (`scripts/capture_tableau_oracle.py:2000-2007`; `scripts/run_estate.py:204-215`; `docs/reference-readiness.md:77-85`; `scripts/promote_unit.py:64-73`). No arbitrary normalized “success” exit may hide a nonzero child.

## Input-shape matrix

| Input/scope | Decision | Existing route/evidence |
|---|---|---|
| One local `.twb`/`.twbx`, optional supplied screenshots | **Pilot.** Screenshot directory is optional; keep its manifest/provenance, do not infer grade from filename. | Parser accepts both formats; local reference capture can adopt images (`scripts/parse_tableau.py:1-6`; `docs/start-with-one-workbook.md:66-102,136-149`; `scripts/capture_tableau_reference.py:585-607`). |
| Standalone `.tds`/`.tdsx` | **Defer.** Parser accepts it, but do not claim model-only end-to-end package/readiness/refresh/completion. | Model-first datasource has no report; workbook-centric `start_migration.py` does not support datasource-only scope (`scripts/parse_tableau.py:1-6`; `docs/migration-phases.md:186-190`; `scripts/README.md:38-49`). |
| Local folder | **Defer from pilot; supported direct route remains.** One `run_estate` run emits per-workbook handovers; it is not a server survey. | `AGENTS.md:213-218`; `scripts/README.md:76`. |
| One site workbook | **Later/advanced:** exact workbook name/LUID, configured server/site, existing `start_migration.py`; identity joins must be verified. | `scripts/start_migration.py:71-80,451-550,585-649`. |
| One project | **Later/advanced:** project selector plus its dependent published datasources; assessment is site-wide, so do not describe it as a project-only assessment. | `scripts/start_migration.py:74-80,480-499`. |
| Whole site | **Later/advanced:** one configured site, not an entire server; current front door is site scoped. | `scripts/start_migration.py:451-538`; `scripts/README.md:38-49`. |
| Full server / multiple sites | **Exclude:** no supported enumeration/fan-out or cross-site run-ownership contract. If later added, separate site child runs and require explicit scope/confirmation. | `scripts/start_migration.py:480-489`; `scripts/README.md:41-49`. |
| Existing/brownfield run, dropped session, hand-edited package | **Status/next only after explicit path and positive identity verification.** Do not replay from timestamps or overwrite hand edits. | `scripts/work_dirs.py:74-89,1300-1397`; `docs/migration-phases.md:120-142,408-420`; `AGENTS.md:278-289`. |

## Lifecycle coverage and legal transitions

The pilot covers allocation → parse/convert attempt → evidence-derived observation. Later stages remain explicit owner routes, not hidden `next` automation:

| Legal transition | Required evidence and authority | Facade boundary |
|---|---|---|
| Unallocated → allocated → surveyed/scoped → harvested/referenced → converted | `work_dirs.py`; survey/assessment/harvest/capture artifacts; `run_estate.py` receipt and DoD/path result. | Pilot only local workbook; remote route waits for safe pre-survey path projection. |
| Converted → credential decision → package assembled → `START_READY` | Source probe plus `credential_gate.py`; `package_unit.py`; `check_reference_readiness.py`. `authorize` means human-authorized model-only/unvalidated, never reachable. | Later advanced commands may delegate; never create/clear credential state in facade. |
| `START_READY` → semantic/report build → refresh → fidelity validation → `COMPLETE` | Builders own TMDL/PBIR; refresh owner persists cache; validator compares shipped bytes against attributable Tableau evidence; only `check_unit.py` all-scope with caller-held receipt establishes `COMPLETE`. | Copilot builders/task tool remain human-directed; wrapper cannot author, validate fidelity, or assert completion. |
| `COMPLETE` → promotion → deployment → binding verification | `promote_unit.py` with same receipt; reviewed deploy dry-run count then explicit `deploy_estate.py`; read-only `verify_bindings.py`. | No implicit promotion/deploy. Binding is not fidelity or data parity. |

Invalid/unknown evidence means refuse or `cannot_establish`, not an invented transition. Examples: moved run; missing baseline; credential modal; package edits that would be overwritten; engine-version mismatch; oracle imagery alone offered as fidelity; deployment without an explicit human action. Owners and details: `docs/migration-phases.md:356-420`; `scripts/credential_gate.py:2491-2534`; `docs/reference-readiness.md:31-37,77-85`; `scripts/deploy_estate.py:169-182`.

## Path/location contract

✅ Owner requirement: Windows Desktop ceilings are **259 UTF-16 code units per file path and 247 per directory**; do not round to generic 260 (`docs/windows-path-limits.md:76-78,100-112,265-271`). `work_dirs.py` records canonical run identity and verifies moved/unverifiable locations (`scripts/work_dirs.py:17-27,74-89`).

Every `start`, `status`, and `next` human/JSON result must expose:

- recorded canonical absolute run path and runs-parent, actual physical path, and repo-local vs short external root;
- git visibility/commit safety; longest current file and directory paths and remaining file/directory budgets;
- pre-engine projection `PASS`, `OVER_CEILING`, or `UNKNOWN` until sufficient evidence; no root-only measurement;
- when over, the exact command for a **new** run using `--runs-parent <short-root>` and original selectors; old run retained, never moved;
- if active, a same-user `subst` alias shown separately as temporary/non-authoritative and never written to `run.json`.

`run_estate.py` has a projection and emitted-tree check, but the live front door reaches conversion’s path gate after survey/harvest (`scripts/run_estate.py:492-585,3615-3711`; `scripts/start_migration.py:585-649`). ⚠️ Therefore a pre-survey/harvest projection is a prerequisite for any remote `start`, not a claim that current code already provides it. If projection is `UNKNOWN`, do not start network/download work. A short-root retry is a new allocation; `subst` waives no ceiling or gate (`docs/windows-path-limits.md:319-371,408-417`).

## Advanced commands and internal scripts

If a later pilot expands, keep survey → assessment/lineage → harvest → capture → convert as explicit advanced routes or compose existing front doors; preserve all native outcomes and per-stage evidence. Package/readiness (`package_unit.py`, `check_reference_readiness.py`), credentials/probes (`credential_gate.py`, `probe_live_source.py`, `reprobe_blocked.py`), refresh, promotion, deploy, and verify stay delegated to their current owners. `check_unit.py` all-scope and caller-held receipt alone owns `COMPLETE` (`docs/migration-phases.md:356-420`).

Semantic/report construction and fidelity are agent work, not CLI mutations. `next` may print a dispatch card containing role, run/brief, evidence owner, exact input, and limitations; **the Copilot task tool remains the dispatch authority**. No command launches agents. Cost and feedback remain specialized existing tools; feedback builds a private bundle/payload and does not publish (`scripts/README.md:65,94-96`; `scripts/build_migration_feedback.py:1580-1605`).

Internal helpers stay out of agent help, not deleted or made unreachable. Preserve direct script compatibility for CI/recovery. In particular, current layout tests require every tracked `scripts/` file (except README) to remain inventoried (`tests/test_repo_layout.py:290-319`).

## Rejected commands / anti-features

- `migrate`, `run-all`, auto-`next`, or full-server fan-out: hides human decisions and combines distinct scopes/gates.
- `resume` that replays stages, chooses “latest”, or trusts timestamps; use verified `status` + `next`.
- `status` that owns/caches readiness, credential, fidelity, or completion verdicts; any second DB, stage ledger, receipt, or readiness definition.
- Credential entry/storage/`authorize`, PAT arguments, automatic retry past a human sign-in stop, or tool upgrades during a migration.
- Build commands that edit TMDL/PBIR; deploy/promote that hides dry-run, receipt, review, or explicit approval; generic `exec`.
- A façade that swallows native output/code, treats `start` success as completion, or treats binding/reference presence as fidelity.

## Incremental slices and dependencies

1. **Proof before broad build:** resolve the draft-doc/index-check conflict; agree pilot scope and path contract. Do not change `docs/INDEX.md` in this one-file synthesis.
2. **Local-workbook vertical pilot:** one source shape, optional reference, canonical `run.json`, conservative pre-work path check, and direct-script positive/negative oracle. Do not add a console entrypoint until install/root behavior is proven; reports disagree on `python scripts/t2p.py` versus installed `t2p`.
3. **Read-only `status` + `next`:** verify path identity; derive from current owner outputs, never maintain state. Test missing, stale, moved, contradictory, credential-blocked, and successful evidence.
4. **Only after blind run 2:** consider additional input shapes and one owner-routed lifecycle slice at a time (package/validate, then deploy/verify); preserve direct script behavior and independent gate tests. Do not start with every verb or a registry that claims untested capability.

Any implementation tests must compare the same script, argv, environment and artifacts directly versus through the facade, including refusal/nonzero and long-running output. Blind run 2 must use the same source scope and reference oracle in an independent session and compare tool calls, elapsed time, false claims, and fidelity with run 1. Remove/disable a required route and sever its gate link in isolated test copies; each mutation must fail the intended assertion, not merely exit nonzero.

## Documentation reduction

Only after the pilot wins blind run 2: make the supported journey and short command tree discoverable from `AGENTS.md`, personas, and `scripts/README.md`; replace repeated selection recipes with links to generated/verified help, retaining a full direct-script inventory and advanced recovery commands. Personas should retain role ownership, hard boundaries, evidence, and task-tool dispatch—not just a command list. Do not remove `docs/INDEX.md` entries or alter scripts inventory until their completeness checks and callers are migrated. ✅ A new unindexed Markdown page may fail `scripts/check_navigation_index.py` / CI; this one-file constraint prevents fixing that here (`docs/INDEX.md:45-47`; `scripts/check_navigation_index.py:50-66`; `.github/workflows/checks.yml:83-84`).

## Owner decisions still required

1. Does measured discovery pain justify any implementation before the two-run comparison, given the red-team report found no evidence that script selection caused migration failures?
2. Approve the local-workbook-only pilot, and keep `.tds[x]`, folders, site scopes, and full server deferred?
3. What evidence can establish a safe *pre-network* projection for each remote scope? Is `UNKNOWN` a hard stop?
4. Should v1 remain `python scripts/t2p.py` (no packaging) or become an installed entrypoint after a separate environment contract?
5. Should outer exits pass through native child codes (with only facade syntax/launch/interrupt codes), or normalize them? How are dry-run count agreement and a human-authorized action represented without a ledger?
6. When may docs/personas stop listing scripts, and who resolves the unindexed-file/one-file-scope conflict?

## Blind run 2 acceptance

One independent cold session gets only `t2p --help` and command output; it completes the same local-workbook scope and supplied-reference scenario as run 1 without opening script source or the full README. An independent observer compares elapsed time, tool calls, scope correctness, claims/limitations, retained evidence and fidelity against the same Tableau oracle. Pass only if tool calls or elapsed time improve, with **no** increase in false claims, missing evidence, gate bypass, or fidelity gaps; report inconclusive if workload or evidence differs. The one-workbook pilot does not establish the broader issue’s full lifecycle acceptance.

## Dissent retained

The red-team recommendation is **STOP**, not “build a CLI”: current evidence establishes a large script inventory and real stale-help/navigation defects, but not that a new runtime reduces agent effort, false claims, or fidelity failures. It also warns that the new file is unindexed, direct script callers remain widespread, and the facade risks duplicating authority. Those are blocking conditions for broad rollout, not objections to be voted away. A narrow pilot is justified only as a falsifiable experiment; if no measurable net reduction or safe path projection is demonstrated, stop rather than expand. (Red-team evidence: `pyproject.toml:45-50`; `scripts/README.md:1-5,38-52,62-77`; `tests/test_scripts_help.py:7-18,102-123`; `.github/workflows/checks.yml:73-97,124-153`.)
