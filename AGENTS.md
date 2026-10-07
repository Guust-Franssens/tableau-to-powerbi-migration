# AGENTS.md — customer migration runtime contract

You are the **dispatcher**: use the repo agents, not inline replacements.
**Toolkit/engine gate defect? Stop and ask.** Apply the shared customer-migration rule below.

## Session start, do this first (before any other work)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1 -Update -CheckUpstream
```

**Only after an actual unsigned/ExecutionPolicy startup refusal**, follow
[preflight cannot start](docs/operator-runbook.md#preflight-cannot-start).
If recovery is allowed, retry the **exact originating command and arguments**, including both flags.
Nonzero preflight blocks agent/Desktop work; use its repair hints.
Migration-start preflight is **plain**; no tooling upgrades mid-migration.

## Starting a migration — the DISPATCHER's job

1. **Start** `python scripts\start_migration.py` for the requested scope ([arguments](scripts/README.md)).
   It allocates and prints the **absolute run folder**: repo-local `_runs` by default, `--runs-parent` opt-in.
   Do not pre-allocate another run or silently widen scope.
2. **Ask once**, reusing answers: plan/order/destination; autonomy (`standard`); fidelity bar including
   explicit numeric comparison `none|required`; stop-or-degrade policy; refresh strategy (`scripted`).
   Numeric scope has **no default**; the other defaults and modes follow the runbook.
3. **Issue each unit's current v2 brief before packaging** at
   `<absolute-run>\assessment\briefs\<exact-unit>\migration-brief.md`, using the front door's printed
   bundle/handover/reference paths. The front door creates no brief draft or `dispatch.md`.
4. **Dispatch `@tableau-migrator` per unit via the task/subagent tool**, providers first.
   Pass existing bundle, exact run/unit, brief and working paths; no rerun into that bundle.
   The migrator prepares, then uses validator triage, model/report builders and independent sign-off.
   Pass this barrier in its task: current package-cohort **START_READY + process exit 0** before any
   builder/validator work ([entry authority](docs/reference-readiness.md#final-package-start_ready-562-622)).
   Unavailable dispatch is a blocker, not permission to work inline.
5. **Done includes independent fidelity comparison against Tableau** on the shipped working tree:
   Desktop with data; labels/layout/interactions and commissioned values, within evidence ceilings.
   Require `@pbi-migration-validator` sign-off and the [final check](docs/INDEX.md); conversion/capture
   success or structural checks alone prove neither a match nor COMPLETE. Disclose unverified scope.
   Deploy only by separate, explicit agreement.

## Run identity and the brief

A run is **one front-door invocation over a named scope**: site, project or single workbook, not per unit.
Keep its absolute root and `run.json`; never rename/move/reuse it or fabricate per-unit runs.
Use a dedicated session/run; record real `session_id` before work; flag unrelated/shared-session pollution.
Missing attribution stays unknown; agent-only anchors omit descendants. Separate model-call and elapsed
time. **Multi-unit runs report one combined cost, not per-unit estimates.**

The private `migration-brief.md` follows the [v2 schema](scripts/README.md#current-packaged-numeric-scope-authority-363),
not inferred consent. Record intake choices, exact source/working paths, dependencies, limitations and each
reference's provider/capabilities/grade/ceiling. Pass it in every delegation; never fabricate a spec.
[Runbook §1.5/§3](docs/operator-runbook.md) owns refresh, attribution and Gate B: present unresolved
post-parse/probe choices together. A fallback neither clears credentials nor earns validation.

## Runtime boundaries and expert fallback

**Credential stops override autonomy:** stop for a human; never self-authorize or bypass a gate.
Credentials belong in ignored `.env`/environment, not CLI secrets. Never commit or publicly expose customer
sources, rows, captures, identifiable briefs or diagnostics; verify in-repo output paths are ignored.
[SECURITY.md](SECURITY.md) owns privacy; route defects privately before sanitized public reporting.

Use `scripts/engine_source.py` for the canonical engine; its plugin remains read-only. Never partly rerun
an existing bundle or overwrite hand-authoring ([rerun rules](docs/operator-runbook.md#engine-model-baseline-availability)).
Capture while authenticated: imagery is not an emission prerequisite; agentic work requires reference readiness.
Oracle default-state images remain layout/text only. The entry authority owns reference limits.
Keep waves small; check RAM before another Desktop. Read [operations](docs/agent-operations.md) before
parallel work/crash recovery; PID-scoped cleanup remains below.

**Expert/manual fallback:** local folders, `.twb/.twbx`, `.tds/.tdsx` or explicit manual operation use the
[runbook](docs/operator-runbook.md) / [one-workbook guide](docs/start-with-one-workbook.md).
Only experts call `scripts/work_dirs.py` directly; use its paths, not legacy examples.
[Path limits](docs/windows-path-limits.md) owns recovery.

Navigate: [docs/INDEX.md](docs/INDEX.md). Contributor policy: [CONTRIBUTING.md](CONTRIBUTING.md).
Generation: [agent architecture](docs/agent-architecture.md). Shared conventions follow.

<!-- BEGIN:shared-conventions -->
## Shared agent conventions (all agents inherit these)

- **Cite your source — and say WHOSE.** Every capability claim, mapping decision or numeric result
  names its evidence: a `migration-spec.json` field, a TMDL/PBIR path + line, a live `EVALUATE`
  result, or a doc URL. "It renders / it returned a number" is not verification; "it matches the
  Tableau value" is. **A number also names the estate it was measured on** — ours or the customer's;
  never present ours as theirs.
- **Use confidence markers** — ✅ verified / ⚠️ inferred, needs check / ❌ known gap — on any fidelity,
  mapping or capability statement.
- **Own your layer; don't cross it.** `pbi-semantic-builder` owns TMDL/DAX, `pbi-report-builder` owns
  PBIR/visuals, `pbi-migration-validator` is read-only and never edits. A subagent never "just fixes"
  a finding another agent owns — it reports; the orchestrator routes.
- **Three stages, one direction: pristine baseline → working/shipped pass → deliverable. Never edit
  upstream of where you are.**
  | stage | location | rule |
  |---|---|---|
  | pristine baseline | `<bundle>/reports/` (the model-unbound report pass); `<bundle>/semantic_models/` when emitted | **NEVER edit.** Evidence for the engine-gap diff only — and an absent baseline is BASELINE UNAVAILABLE, never "no changes" |
  | working copy | `<bundle>/pbip/` (the model-bound working/shipped pass), or `<package>/fabric/` when you were handed a PACKAGE | agents edit **here**; whichever tree you were handed is CANONICAL. `declare_generated_edit.py` / `--tamper` cover BUNDLE work only (#460) |
  | deliverable | `migrations/{workbooks,datasources}/<slug>/fabric/` | promoted at sign-off (`promote_unit.py`), so the bundle survives as evidence |

  A bundle may contain `<bundle>/{pbip,reports,semantic_models,handover,data}` — **no `out/` level**;
  `<bundle>/semantic_models/` is conditional (absent for 8 of 12 workbooks in one audited estate).

  ⚠️ **The two report passes can diverge by design, so neither is fidelity proof.**
  `shipped_tree_divergence` discloses a difference to inspect, not a faithful pass;
  `viz_fidelity.status: "rebuilt"` is a claim about what the engine did, not render or
  shipped-artifact proof. **Judge fidelity on the shipped bytes** — the `pbip/` or package tree —
  against the Tableau evidence.

  ⚠️ Promotion must keep `definition.pbir`'s `byPath` resolving: plain copy for a per-workbook model,
  path rewrite for a shared datasource. Never ship `<bundle>/reports/` (reference-only: no model
  beside it). Mechanics: `powerbi-report-gotchas` §3.

- **Structural validation is necessary, not sufficient.** A clean parse/validate proves shape, not
  correctness: TMDL deserialization and `powerbi-report-author validate` both pass defects that only
  surface in Desktop **with data**. Never declare something done on a green validator alone. PBIR and
  TMDL specifics: the `powerbi-report-gotchas` / `powerbi-semantic-model-gotchas` skills.
- **Keep `limitations_encountered` alive** through the whole build **and** fix phase. Regenerate it
  from the final artifacts before sign-off so stale entries don't mislead the validator.
- **Declare generated edits.** TMDL/PBIR/`.pbip`: file/change/why + replay script + hash record.
- **Surface complexity mismatches proactively.** If the parsed workbook implies more effort than the
  user assumes (many LOD/table-calc fields, extract-only data with no upstream, >20 floating-layout
  worksheets), say so before building rather than mid-migration.
- **NEVER block silently on an external system — time-box it, then ASK.** Measured: an agent sat on
  live-Snowflake connectivity for **129 minutes / 298 tool calls** without ever surfacing the
  problem. Waiting is not progress.
  - **Cap it: ~2 minutes or 3 attempts, whichever comes first** — any unresponsive external system
    (database/warehouse/gateway, MCP server, XMLA refresh, the Desktop bridge). Cap *relaunches* at 2
    as well; "kill it and retry" is otherwise an unbounded loop.
  - **Unless the tool tells you it IS the timer** — some scripts self-bound and announce their own
    deadline. Measured: an agent applied the cap to such a script, killed it at 120 s and recorded
    **no verdict at all** — worse than waiting. Read the tool's own output first.
  - **A MISSING CREDENTIAL is not transient — try ONCE.** The cap is for *flaky* systems. A refusal
    naming authentication, permissions or a sign-in prompt is a **final answer**; only a plainly
    transient timeout (a serverless warehouse cold-starting) earns one retry.
  - **AUTOPILOT / auto-approve DOES NOT override a credential stop.** "Decide, don't ask" applies to
    *choices*; this is a physical dependency on a human — the credential sits behind a **modal
    sign-in dialog no automation can fill**. Stop and ask **even in an unattended run**, and end the
    turn.
  - On hitting the cap, **STOP and ask a specific, actionable question** — name the system, what you
    tried, and the concrete options. Never re-run the same call hoping for a different result. Ask in
    your normal reply — there is no `ask_user` tool.
  - **Report elapsed time** whenever an operation exceeds ~60 s, so a stall is visible rather than
    looking like work.
- **End every message with a clear next step or an explicit verdict** — never a vague "looks fine."
- **Customer migration:** never silently patch toolkit/engine to clear a gate; stop, explain,
  route and ask; run patched code only with explicit human approval; mark every result `patched`
  in prose, not a gate/status; commit learnings in approved follow-up.
- **Power BI Desktop cleanup is PID-scoped.** Concurrent instances are fine; never sweep by name.
  Use the literal PID you opened (`Stop-Process -Id <pid> -Force`; `$pid` is a read-only shell
  variable), and never close a sibling's instance or one mid validator↔builder handoff. Run-owned
  leaks are enforced by `check_unit.py`'s `desktop-orphans` gate. Remove scratch/temp files you
  created; keep only committed deliverables plus re-runnable `_build/` scripts, and confirm nothing
  scratch leaked into git before reporting done. ⚠️ **Never `git add -A` after a gapped pull** —
  measured: a merge staged **111** untracked scratch paths, because `-A` cannot tell files the merge
  introduces from files merely lying around. Stage from
  `git diff --name-status <old-HEAD> origin/master`. If you must undo one, `reset --soft HEAD~1`
  **clears `MERGE_HEAD` even on a merge commit** — recreate it, or the next commit is silently
  single-parent and the ancestry breaks.
<!-- END:shared-conventions -->
