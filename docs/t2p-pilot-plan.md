# Narrow `t2p` pilot plan — Refs #749

## Decision and scope

**⚠️ Narrow pilot: accept the existing post-harvest/pre-conversion path gate; no #751 dependency.**
This PR is documentation only; initial path decision is CANNOT_ESTABLISH / `unknown_until_harvest`, never PASS.
Base: `511bb2098d179f2190943e45c650739c8c794fea`; issue: <https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/749>.
Owner decision: [#749 latest comment](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/749#issuecomment-6233864458); earlier independent-review amendments remain except the pre-network admission/dependency requirement.
Only these commands are contemplated:
```text
python -B scripts/t2p.py start --project <exact-name|LUID> [--env <path>] [--runs-parent <path>] [--json]
python -B scripts/t2p.py status --run <absolute-run> [--json]
```
Require one nonempty project selector; disable argparse abbreviation; reject other selectors/verbs.
No local file/folder/datasource/site/server routes, `next`, credential, package, build, validation,
promotion, deployment or dispatch commands; no installed entrypoint, framework, registry, DB or router.
✅ Command scope stays narrow and assessment site-wide; latest owner timing decision supersedes pre-network admission:
`docs/cli-consensus/consensus.md:45-71,81,130-138` (all evidence paths are repository-relative).

## 1. Path invariant and decision timing

**Initial:** CANNOT_ESTABLISH / `unknown_until_harvest` at either root; no pre-network PASS required.
Delegation may allocate, authenticate, survey and harvest under existing guards; unknown is not safety.
**PASS:** after harvested unit names exist, the owner's pre-conversion projection fits
**259/247 UTF-16 code units**. This is the existing canonical skeleton decision, not every generated tail.
PASS permits conversion only; it certifies neither emitted paths nor readiness. Retain downstream gates.
**REFUSE / OVER_CEILING:** owner projection exceeds a ceiling; existing output-guard refusal still applies.
**CANNOT_ESTABLISH at conversion gate:** unusable names/projection evidence refuses conversion as today.
Neither later non-pass proceeds to conversion; preserve the owning stage and exact delegated exit.
Never turn initial unknown into PASS, relocate, truncate names, invent a second bound/allocation
formula or silently choose an external parent. Bypassing a gate/false PASS blocks merge.
✅ The existing projection needs engine-derived input names, repeats each unit in PBIP/report paths,
and returns cannot-establish without names; it is a minimum skeleton, not an emitted-tree guarantee:
`scripts/run_estate.py:76-82,492-544,561-585,3661-3670`.
✅ The front door allocates before survey and obtains harvested assets only before conversion:
`scripts/start_migration.py:463-503,520-538,585-649`.
❌ A project selector/short root alone proves no bound. The owner accepts possible repeated harvest
on this self-populated trial site instead of introducing a new metadata admission protocol.
On over-ceiling, retain the old run and exact new-run `--runs-parent` command; do not auto-retry.
✅ `run_estate` returns 10 at its non-pass projection gate; the front door retains inputs and returns
its wrapper result (normally 1, or 3 for invalid evidence), not a facade-normalized path code:
`scripts/run_estate.py:3617-3622`; `scripts/start_migration.py:649-705`; `tests/test_start_migration.py:752-773`.
Consume existing owner results only; any additional facade policy/scanner/state file requires replan.

## 2. Location and output contract

✅ Allocation remains solely `work_dirs`: repo-local `_runs` default, explicit external parent only;
one invocation/one run. Do not preallocate outside the front door or race a predicted next number:
`scripts/work_dirs.py:17-27,779-856`.
Before allocation, first/last human lines are `RUN NOT_ALLOCATED; runs_root=<resolved-root>`;
JSON has `run_location:null`, `runs_root:null` with reason `not_allocated_path_withheld`; invent no path.
After allocation, first line before any child and last human line are `RUN <absolute-physical-run>`,
including failure/interrupt; preserve that same location in JSON. Never discover the run by “latest”.
Human reports recorded run/root, selected/physical location, repo-local/external relationship, git
visibility (`ignored|visible|unknown`), commit safety (`not_for_git|unsafe|unknown`); JSON withholds other roots.
Only the existing output guard establishes ignore safety; external is not synonymous with ignored.
For status, do not launch git; underivable visibility is null with reason `not_derivable_read_only`.
Path report always has ceilings 259/247, current longest file/directory locators and lengths,
remaining budgets (ceiling minus measured UTF-16 length), projection verdict and reason.
Status always emits every path field; underivable values are null with stable per-field reasons
(`unknown_until_harvest|not_recorded|not_derivable_read_only|identity_unestablished`), never fabricated PASS.
Do not call `check_path_ceiling.scan` from status: it launches git and would widen the read boundary:
`scripts/check_path_ceiling.py:292-315,374-409,415-455`.
Exact private rerun argv/display requires every argument recorded (project, env/default, toolkit/root):
Shape: `python -B <recorded-toolkit>/scripts/t2p.py start --project <recorded> [--env <recorded-env>] --runs-parent <recorded-parent>`.
Otherwise emit a partial/candidate command marked non-authoritative, or unavailable with a reason.
New invocation only, old run retained; shorter-root candidate goes through the same existing owner gate.
JSON redacts toolkit/selector/env/root tokens; mark partial/non-authoritative, never exact or safe.
An active `subst` spelling, if known, is separately labelled temporary/non-authoritative; otherwise
alias is null with reason `not_derivable_read_only`. Never launch alias discovery in status or store one
in run identity; aliases waive no physical ceiling:
`docs/windows-path-limits.md:100-112,265-271,339-371,408-417`.

## 3. Delegation, native exits and stable envelope

**Choose one delegated owner:** `start_migration`, not a facade-owned five-child chain.
Preserve its exact wrapper exit: 0 capture-only, 1 failed, 3 invalid evidence (takes precedence), 130
interrupted. Preserve all stage/native outcomes separately; never assign global meaning to numbers.
Reference failure or invalid reference evidence still attempts the bundle; earlier failures stop
as today. Do not rewrite this into first-native-failure exit/stop semantics:
`scripts/start_migration.py:613-688,691-705`;
`tests/test_start_migration.py:697-736`.
Conditional implementation: add an optional in-memory observer/progress-stream seam to that owner,
defaulting to today's behavior; report allocation, preflight, stage completion and final wrapper result.
Stage records: fixed stage/owner, `native_exit_code` (null for not launched/launch failure), explicit state,
safe relative expected artifact locator and evidence-check outcome; distinguish skipped from failed.
No persisted ledger/result file. Record native results where obtained, not from printed prose.
JSON mode explicitly routes child stdout AND stderr plus owner progress to stderr, live/unbuffered;
Python-only stdout redirection is insufficient for inherited child descriptors. No end-only capture.
One JSON object on stdout, including syntax/refusal/interrupt errors; human uses those records plus private paths.
Pin this shape in tests (schema is an inline contract, no schema framework/new dependency):
```json
{"schema_version":"t2p/v1","command":"start","status":"error","exit_code":1,"authority":"start_migration.py","delegated_exit_code":1,"stages":[],"evidence":[],"artifacts":[],"run_location":null,"path_report":{"initial_decision":"cannot_establish","initial_reason":"unknown_until_harvest"},"limitations":[],"proof_boundary":{"established":[],"not_established":[]}}
```
Statuses: `ok|findings|cannot_establish|error|interrupted`; refusal uses owner details, not a new status.
After delegation, outer exit equals exact owner exit; `delegated_exit_code` is the wrapper/diagnostic code.
`native_exit_code` occurs only in stage records. Before delegation: syntax=2, refusal=1, unknown=3, error=1,
interrupt=130; pin ownership as well as integers. `findings` may have native diagnostic exit 0.
Evidence/artifact entries contain owner plus safe relative locator (digest only if already supplied).
Stable keys/nullability and deterministic ordering; additive fields compatible, incompatible shape=new schema.
Never copy customer text, PAT, server/site, arbitrary exception/manifest prose or unrelated absolute paths
into the envelope. Absolute run paths are local-only; child streams remain private native diagnostics,
not sanitized JSON/public attachments. Safe status projection must omit toolkit/other absolute locations.

## 4. Status authority and compatibility

Call `run_status.build_status` in-process under `-B`; preserve its exact code, multiplicity, findings
and identity boundaries. No subprocess, network, cache, write, repair, allocation or readiness gate.
Omit its `next_action` from the facade projection (direct script remains unchanged); add no routing.
Missing/moved/contradictory identity => cannot-establish, no descendant scan or derived run path.
`active` is allocation metadata, stored results are last-observed; never claim current liveness,
START_READY, COMPLETE, fidelity, credential reachability or deployment:
`scripts/run_status.py:851-939,972-987`.
Direct start/status/work_dirs/path scripts retain arguments, output, exit and artifact behavior.
No replacement of guidance/personas before pilot benefit; no direct-script path policy changes in v1.

## 5. Closed surface, slices and independent controls

**This PR: docs-only, exactly `docs/t2p-pilot-plan.md` and `docs/INDEX.md`; implementation N=0.**
Proposed later implementation surface (7 files): new `scripts/t2p.py`, `tests/test_t2p.py`;
edit `scripts/start_migration.py`, `tests/test_start_migration.py`, `scripts/README.md`,
`docs/agent-capability-wiring.md`, `docs/INDEX.md`. This is conditional inventory, not authorization.
Shared files are the front door and those three documentation inventories. All other files unchanged.
Read surface: argv/environment, owner allocation/run.json, native stage results and existing owner
artifacts, run-status observations; joins are selector→catalog→harvest→oracle→bundle and recorded→physical
identity, all retained in owners. Residuals: unknown remote bounds, non-atomic reads, unsafe/unavailable
locations and unknown visibility/measurements. No new scanner or mutable-read authority.
Slice 0: independent exact-head plan review against the latest owner timing decision; no #751 dependency.
Slice 1: owner observer seam with unchanged defaults; slice 2: two explicit argparse branches/envelopes;
slice 3: tests/inventory/help, Windows review, then blind run. New surface/class => split/replan.
Direct controls use the same owner, argv, environment and fixed external fixture artifacts, not facade
output as the oracle. Positive: intact run, valid project artifacts and known 259/247 paths.
Negative: missing/moved/malformed/reparse run; 260/248 and astral/surrogate paths. At BOTH roots initial
unknown permits harvest but never claims PASS; post-harvest unknown/over-ceiling must prevent engine launch.
Pin wrapper/native pairs: reference 4→bundle attempted→wrapper 1; invalid reference→bundle→3;
survey 7→no later stages→1; bundle 10 retained; preflight failure; launch failure; interruption/reaping.
Compare direct/in-facade status codes/observations in both modes; hash bytes and list entries before/
after repeated calls, fresh bytecode prefix stays absent; trap network/process/write calls.
Mutations in isolated copies: remove start/status branch; disconnect pre-conversion/emitted gate; force unknown→PASS;
drop native stage record; suppress post-reference bundle; normalize exit; buffer progress; enable
status write/next. Each must fail its named scope/guard/stage/exit/stream/read-only assertion.

## 6. Validation, blind run 2 and stop conditions

Exact commands below run from the toolkit root; future commands are NOT authorization to implement.
Docs now: `wc -l docs/t2p-pilot-plan.md` (≤160); `git diff --check`;
`python -B scripts/check_navigation_index.py`; `python -B scripts/set_data_folder.py --check`.
Future, stage new scripts first: `pytest -q tests/test_t2p.py tests/test_start_migration.py tests/test_run_status.py tests/test_work_dirs.py tests/test_check_path_ceiling.py`;
`pytest -q tests/test_scripts_help.py tests/test_repo_layout.py tests/test_agent_capabilities.py`.
`ruff format --check scripts/t2p.py scripts/start_migration.py tests/test_t2p.py tests/test_start_migration.py`;
`ruff check scripts/t2p.py scripts/start_migration.py tests/test_t2p.py tests/test_start_migration.py`;
`pylint scripts/t2p.py scripts/start_migration.py`; `python -B scripts/check_agent_capabilities.py`;
`python -B scripts/sync_agent_conventions.py --check`; repeat docs gates; secret scan and blind diff review.
Full gate: `T2P_ENGINE_TESTS_NOT_CHECKED_REASON=covered-by-pinned-engine-integration-job uv run pytest -q`.
Blind run 2 follows approved plan, implementation and Windows review; cold independent session gets
only `t2p --help`/output, no source/full README; same `20 - Supply Chain`, environment and comparable
oracle/reference coverage as run 1. Verify first/last physical path, live output, exits and read-only status.
Compare elapsed time, tool calls, exact scope, evidence retention, false claims and independent Tableau
fidelity. Benefit requires fewer calls or elapsed time without worsened claims/evidence/gates/fidelity;
different coverage/environment => inconclusive. No measurable benefit => stop, not broader commands.
