# Narrow `t2p` pilot plan — Refs #749

## Decision and scope

**❌ BLOCKED for live implementation: no safe pre-network projection is established.**
This is a plan, not approval to bypass a guard or to ship an always-refusing pilot as useful.
Base: `511bb2098d179f2190943e45c650739c8c794fea`; issue: <https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/749>.
⚠️ Proposed contracts below need independent plan approval before behavioral coding.
Only these commands are contemplated:
```text
python -B scripts/t2p.py start --project <exact-name|LUID> [--env <path>] [--runs-parent <path>] [--json]
python -B scripts/t2p.py status --run <absolute-run> [--json]
```
Require one nonempty project selector; disable argparse abbreviation; reject other selectors/verbs.
No local file/folder/datasource/site/server routes, `next`, credential, package, build, validation,
promotion, deployment or dispatch commands; no installed entrypoint, framework, registry, DB or router.
✅ This matches the approved narrow scope; assessment is still site-wide, not project-only:
`docs/cli-consensus/consensus.md:45-71,81,130-138` (all evidence paths are repository-relative).

## 1. Path invariant and blocking proof

**PASS:** before any preflight child, survey, authentication or download, the path owner must establish
a conservative bound for the selected scope at the allocator-owned physical run location, including
every variable name transformation; file/directory bounds must fit **259/247 UTF-16 code units**.
PASS permits delegation only; it certifies neither emitted paths nor readiness. Retain downstream gates.
**REFUSE:** a known required path exceeds either ceiling, or the existing output guard refuses.
**CANNOT_ESTABLISH:** names/bounds/physical identity/visibility are missing, unreadable or contradictory.
Both non-pass outcomes stop before network; never turn unknown into PASS, relocate, truncate names,
invent a second allocation formula, or silently choose an external parent.
Fail-open risks an unusable artifact/privacy leak and blocks merge; fail-closed blocks this pilot,
not existing direct scripts. A root-only length check is not a downstream projection.
✅ The existing projection needs engine-derived input names, repeats each unit in PBIP/report paths,
and returns cannot-establish without names; it is a minimum skeleton, not an emitted-tree guarantee:
`scripts/run_estate.py:76-82,492-544,561-585,3661-3670`.
✅ The front door allocates before survey and obtains harvested assets only before conversion:
`scripts/start_migration.py:463-503,520-538,585-649`.
❌ A project name/LUID does not establish workbook/unit names, collision expansion, or generated tails.
Neither the requested selector nor a cached prior survey proves a current upper bound. No supplied
owner contract proves that choosing `C:\t2p` fixes this missing information.
**Decision:** with current evidence, both default and explicit-short-root starts are CANNOT_ESTABLISH.
Require explicit `--runs-parent` for a short-root retry, but never treat the flag itself as proof.
Unblock only with an independently reviewed owner-level bound/identity contract; if that needs more
files or a new naming policy, split/replan, not an opportunistic guard in this facade.

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
For status, do not launch git; visibility is `unknown` unless supported without additional operations.
Path report always has ceilings 259/247, current longest file/directory locators and lengths,
remaining budgets (ceiling minus measured UTF-16 length), projection verdict and reason.
Unknown measurements/margins are null, never zero; a current scan never proves projected PASS.
Status v1 may report these measurements unknown: its authority does not currently supply a path scan.
Do not call `check_path_ceiling.scan` from status: it launches git and would widen the read boundary:
`scripts/check_path_ceiling.py:292-315,374-409,415-455`.
Private human output includes exact shell-correct rerun argv/display, retaining project and optional env:
`python -B <absolute-toolkit>/scripts/t2p.py start --project <original> --runs-parent C:\t2p`.
This is a new invocation, old run retained; label it “short-root candidate; projection still required”.
JSON argv AND display replace toolkit, selector, env and short-root tokens with safe placeholders;
mark `rerun_complete:false`. Exact JSON rerun/root disclosure needs approval; do not silently leak it.
An active `subst` spelling, if known, is separately labelled temporary/non-authoritative; otherwise
alias state is unknown. Never create/discover aliases by launching processes in status or store one
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
Stage records: fixed stage/owner, native code (null for not launched/launch failure), explicit state,
safe relative expected artifact locator and evidence-check outcome; distinguish skipped from failed.
No persisted ledger/result file. Record native results where obtained, not from printed prose.
JSON mode explicitly routes child stdout AND stderr plus owner progress to stderr, live/unbuffered;
Python-only stdout redirection is insufficient for inherited child descriptors. No end-only capture.
One JSON object on stdout, including syntax/refusal/interrupt errors; human uses those records plus private paths.
Pin this shape in tests (schema is an inline contract, no schema framework/new dependency):
```json
{"schema_version":"t2p/v1","command":"start","status":"cannot_establish","exit_code":3,"authority":"t2p.path-policy","native_exit_code":null,"stages":[],"evidence":[],"artifacts":[],"run_location":null,"path_report":{},"limitations":[],"proof_boundary":{"established":[],"not_established":[]}}
```
Statuses: `ok|findings|refused|cannot_establish|error|interrupted`; authority names the actual owner.
After delegation, outer exit equals exact owner exit; native_exit_code is that wrapper/diagnostic code,
not the first leaf code. Before delegation only: syntax=2, refusal=1, unknown=3, launch/error=1,
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
No replacement of script guidance/personas before pilot benefit; no direct-script projection retrofit.

## 5. Closed surface, slices and independent controls

**This PR: exactly one added file, this plan; no product/test/index edits or issue comments.**
Conditional later implementation surface **N=7 files**: new `scripts/t2p.py`, `tests/test_t2p.py`;
edit `scripts/start_migration.py`, `tests/test_start_migration.py`, `scripts/README.md`,
`docs/agent-capability-wiring.md`, `docs/INDEX.md`. No other implementation files are authorized.
Shared files are the front door and those three documentation inventories. All other files unchanged.
Read surface: argv/environment, owner allocation/run.json, native stage results and existing owner
artifacts, run-status observations; joins are selector→catalog→harvest→oracle→bundle and recorded→physical
identity, all retained in owners. Residuals: unknown remote bounds, non-atomic reads, unsafe/unavailable
locations and unknown visibility/measurements. No new scanner or mutable-read authority.
Slice 0: independent path/exception review; BLOCKED now. Stop here unless approved evidence unblocks it.
Slice 1: owner observer seam with unchanged defaults; slice 2: two explicit argparse branches/envelopes;
slice 3: tests/inventory/help, Windows review, then blind run. New surface/class => split/replan.
Direct controls use the same owner, argv, environment and fixed external fixture artifacts, not facade
output as the oracle. Positive: intact run, valid project artifacts and known 259/247 paths.
Negative: missing/moved/malformed/reparse run; 260/248 and astral/surrogate paths; unknown unit bound
at BOTH roots must launch no child and allocate nothing. No claimed live positive while blocked.
Pin wrapper/native pairs: reference 4→bundle attempted→wrapper 1; invalid reference→bundle→3;
survey 7→no later stages→1; bundle 10 retained; preflight failure; launch failure; interruption/reaping.
Compare direct/in-facade status codes/observations in both modes; hash bytes and list entries before/
after repeated calls, fresh bytecode prefix stays absent; trap network/process/write calls.
Mutations in isolated copies: remove start/status branch; disconnect path guard; force unknown→PASS;
drop native stage record; suppress post-reference bundle; normalize exit; buffer progress; enable
status write/next. Each must fail its named scope/guard/stage/exit/stream/read-only assertion.

## 6. Validation, blind run 2 and stop conditions

For implementation: targeted pytest for t2p/start_migration/run_status/work_dirs/check_path_ceiling,
then scripts-help (including cp1252), repo-layout and agent-capability tests; stage new scripts first.
Run existing ruff check/format-check and pylint on edited Python, navigation/capability/convention gates,
privacy check, full pytest under the documented engine policy, secret scan and blind diff review.
✅ Inventory/help gates: `tests/test_scripts_help.py:51-71,102-173`;
`tests/test_repo_layout.py:290-319`;
`scripts/check_agent_capabilities.py:173-237`.
Plan-only checks: line count ≤160, citation/manual scope review, diff/secret checks, navigation gate.
❌ One-file scope leaves this page unindexed: report navigation failure, do not exempt it or edit INDEX;
owner must approve a separate index change before green merge (`scripts/check_navigation_index.py:39-66`).
Blind run 2 is NOT authorized while blocked. After approval, cold independent Windows session gets
only `t2p --help`/output, no source/full README; same `20 - Supply Chain`, environment and comparable
oracle/reference coverage as run 1. Verify first/last physical path, live output, exits and read-only status.
Compare elapsed time, tool calls, exact scope, evidence retention, false claims and independent Tableau
fidelity. Benefit requires fewer calls or elapsed time without worsened claims/evidence/gates/fidelity;
different coverage/environment => inconclusive. No measurable benefit => stop, not broader commands.
