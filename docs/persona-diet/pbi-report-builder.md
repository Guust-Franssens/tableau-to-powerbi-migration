# #733 — `pbi-report-builder` persona diet

**Decision: substantial diet is safe, but not by deleting the report-specific contract.** Move
procedures to the current report skill; keep the small set of Tableau migration, ownership and gate
rules that a stateless subagent must act on. This is an analysis, not an agent rewrite or runtime claim.

## Evidence and state

- Current master persona: `.github/agents/pbi-report-builder.agent.md` blob
  `4b49a6107d374ca97040974e214122b8fccf7594`. Current master has the #720 shared block.
- Future stack inputs (not merged): #745 head `1823251d45169cf3dbcec201428b3df2d06bbf1b`;
  #747 head `57cd4310c8fd72e4d1aadd803034761ed119c5fc`; #752 head
  `86190257e40b3a59a1f8972329e732012a249bb0`. #745 does not edit this persona; #747 replaces
  retired report-skill names/modes and tightens its boundaries; #752 edits only the semantic persona,
  its sync test and pins. #752's recorded base is #747, so it must be rebased to include merged #746
  before treating the complete stack as integrated.
- Merged AI policy: #746 head `d7a3ebfc298eee075fa3c9abba3c237b8e893868` (semantic model owns
  AI-instruction authoring/stamping). Merged CLI consensus: #748 head
  `0c765bac493b15bd09c48e1e03214e089d81ff30`, `docs/cli-consensus/consensus.md`.
- CLI implementation is **not** implied by that consensus. #750 head
  `8e15fa7e622dae3527700da0a78a95e504b47268` is an open, docs-only plan (implementation N=0);
  it proposes a bounded `t2p doctor/start/status` surface but does not make those commands executable.
  Until implementation and command/capability tests land, use only existing repo authorities.
- Current upstream `powerbi-report-cli` main reports skill version 1.0.5. It dispatches by intent;
  existing edits use `authoring`, a missing page can use `design` then `authoring`, and each selected
  mode reference is read end-to-end. Its authoring completion gate requires validate → load → capture
  affected pages → review screenshots; it does not authorize publishing. The #747 disk oracle was
  plugin 0.3.19 and its post-maintenance runtime probe remains PENDING, not verified.
  Sources: [skill](https://github.com/microsoft/skills-for-fabric/blob/main/skills/powerbi-report-cli/SKILL.md),
  [authoring](https://github.com/microsoft/skills-for-fabric/blob/main/skills/powerbi-report-cli/references/authoring.md),
  #747 head above.

## Size accounting

Counts are Unicode characters and LF lines in the complete persona; CRLF size is shown where #747's
test contract uses it. Generated shared block and generated gotchas index are separated from authored
text; the latter is not part of `AGENTS.md`'s shared block.

| State | Whole persona | Authored (excluding both generated regions) | Shared block | Gotchas index |
|---|---:|---:|---:|---:|
| Current master | 21,617 chars / 285 lines (21,902 CRLF chars) | 13,881 / 184 | 6,874 / 85 | 862 / 16 |
| After #747, projected | 21,597 / 287 (21,884 CRLF chars) | 13,861 / 186 | 6,874 / 85 | 862 / 16 |
| Diet target, including generated text | **about 12,000 / about 155** | **about 4,264 / 54** | 6,874 / 85 | 862 / 16 |

Target is an estimate, not a cap: roughly 44% fewer total chars and 71% fewer authored chars than
the #747 projection, while preserving both generated regions. Do not reduce the shared block locally
to meet it. Measurements use the `prompt_size` convention (`scripts/sync_agent_conventions.py:257`)
and the conservative CRLF comparison used by #747's future test.

## Minimal future contract — exact outline

1. **Role/layer:** repair the already emitted, model-bound PBIR; own report visuals/layout only.
   TMDL/DAX goes to `pbi-semantic-builder`; findings about shipped fidelity go to the read-only
   `pbi-migration-validator`; never self-sign migration completion.
2. **Inputs/canonical tree:** use the exact handed-off bundle/package, handover/spec and attributable
   Tableau reference. Edit only its canonical working/shipped tree (`pbip/` or handed package
   `fabric/`); `<bundle>/reports/` is a pristine, read-only baseline. Missing baseline is unavailable,
   never “no changes.” Require a warm persisted model; stale/missing cache or credential prompt is a
   human/semantic-builder stop, never a self-refresh.
3. **Current skills/modes:** load `powerbi-report-gotchas` and `powerbi-report-cli`; use `authoring`
   directly for an existing report, `design` then `authoring` only for a genuinely missing page.
   Read the selected mode reference end-to-end; the CLI has no mode argument. True greenfield work
   stays in `planning` at `approval_pending` until explicit approval in a later turn. Never enter
   `management`, publish or rebind.
4. **Version/capability:** preflight is the sole version authority. Skip upstream `@latest`; never
   install/update npm mid-migration. Missing skill, command or capability is **BLOCKED**, routed to
   preflight, not a reason to improvise or substitute hand-authored PBIR.
5. **Pass / refuse / cannot establish:** pass only the report-layer repair after the repo report and
   integration gates, declared-edit check, fresh authoring screenshot review, and Tableau comparison
   of the shipped artifact (whole-page gestalt plus commissioned numeric evidence within its ceiling).
   Refuse baseline edits, model edits, undeclared/non-rerunnable generated edits, publishing, rebinding,
   credential workarounds or a green structural check presented as fidelity. Missing source intent,
   field/model evidence, tool support, warm cache, attributable reference or render proof is
   **CANNOT ESTABLISH**: preserve the limitation and route/ask; do not guess.
6. **Outputs/handoff:** report exact changed visuals/files and replay/declaration, gates and screenshot
   evidence, accepted limitations/new `limitations_encountered`, and every unresolved/false handover
   claim. Hand off the shipped tree to the independent validator; no `COMPLETE` claim. Keep customer
   rows, captures, briefs and diagnostics in private run-owned locations; never publish them.

## Current text → action

Line ranges refer to current master `.github/agents/pbi-report-builder.agent.md`; #747 changes below
are future-head wording, not already-merged source.

| Current paragraph/token | Action in diet / reason |
|---|---|
| Front matter and role, 1–11 | **Replace** with role, layer, canonical input/output in outline 1–2; preserve “existing engine output” scope. |
| Generated shared block, 12–96 | **Retain verbatim/generated.** Never hand-edit; its cross-agent rules are not inherited from root `AGENTS.md` by subagents (`scripts/sync_agent_conventions.py:7-35`). |
| Skill chain, 98–116 | **Replace** planning/design/authoring chain and retired `powerbi-report-authoring` / `check-updates` with current skill/mode pointer and explicit refusal/capability boundary from #747. Keep read-only DAX routes only as needed for sampled-value evidence; no model edits. |
| Received-artifact table, 117–132 | **Condense, retain** `read_handover.py`/`remediation_worklist`, pending gates, `migration-spec.json` source intent and attributable `reference/`; keep exact handed-in path and canonical working tree. Move queue field explanations to gotchas §10. |
| Baseline and `viz_fidelity` warnings, 129–140 | **Retain** shipped `pbip/` is judged; `reports/` is immutable baseline; `rebuilt`/handover rows are not render proof; a deferral must not be re-created. Point to shared stage rule and gotchas §10. |
| `_build/`/generated-edit procedure, 137–162 | **Replace with invariant + pointer** to gotchas §3 and `declare_generated_edit.py`: semantic, idempotent replay; declare every generated edit; `--tamper` must pass. Delete examples and flag-sequence tutorial. |
| Workflow 0–2, 166–180 | Delete repeated skill call and Desktop recipe. **Retain** warm persisted cache + no self-refresh/credential stop, reference-only baseline and whole-page gestalt comparison; point screenshot mechanics to CLI authoring. |
| Workflow 3–4, 181–191 | **Retain** report-scope field-binding gate, case-only vs missing-field routing, validator classification and refusal to reverse intentional deferrals. Delete measurement anecdotes and raw-list tutorial. |
| Workflow 5–6, 192–202 | **Pointer-replace** CLI/schema mechanics. **Retain** no guessing when catalog/evidence cannot establish an encoding, human stop, Tableau parameter-equality slicer and direct pivot-field binding. |
| Workflow 7–9, 203–210 | **Condense** to structural + fresh render evidence, declared replay, final limitation/report handoff; retain parser `validate_spec.py` and no-spec report/integration gate. Remove duplicated procedure and long output checklist. |
| “If a page must be built from scratch,” 211–215 | **Replace** with missing-page `design` → `authoring`; remove obsolete three-skill chain and ad-hoc PBIR warning. Keep greenfield `planning`/approval stop only for an actual greenfield request. |
| Mandatory validation, 217–236 | **Pointer-replace** generic validate/screenshot procedure with the selected CLI authoring reference. **Retain** `check_unit.py --scope report` then `--scope integration`, parser gate, latest affected-page screenshots and Tableau evidence; structural success alone is never report fidelity. Keep cross-tree `byPath` handoff, not its tutorial. |
| Gotchas callout and semantic-owner line, 235–257 | **Delete duplicate callout**; retain gotchas as required skill and fold layer routing into role. The 16-line generated index is generator-owned; leave it unless a separate reviewed sync/test change removes it. |
| DoD items 1–11, 259–285 | **Replace** checklist with the compact pass/handoff contract. Delete items 1 and 5's repeated skill/validation instructions; merge 2 with declaration, 3 with validator routing, 4 with gestalt, 6 with layout bounds, 7 with sampled `formatString`, 8 with Tableau field projection, 9 with source-matching defaults, 10 with pivot/`UNRESOLVED:` limits, and 11 with the blocking map-grain rule. |

### Repo-unique rules that are not upstream replacements

- `check_unit.py --scope report` and `--scope integration` remain repo gates; parser path also reruns
  `validate_spec.py`. The CLI validates PBIR, not this migration's handover/readiness/fidelity.
- `viz_fidelity` and `remediation_worklist` are routing evidence, not proof: repair only validator-owned
  fixable findings; never re-create a deliberate Tableau table-calc deferral or treat `rebuilt` as
  fidelity. Judge shipped bytes against the attributable Tableau reference.
- Tableau filter notes, parameter-equality slicers and measure-name/value pivots preserve source intent
  the engine input cannot carry. Missing field belongs to semantic builder; case-only binding repair
  can stay report-side. Keep actual sample `EVALUATE` evidence for percentage formatting; no fabricated
  “consumption” skill alias.
- The prompt/credential wall, private customer artifacts, no refresh on behalf of a model owner, and
  no cross-layer edit must remain explicit or be enforced by the shared tool contract. Root-only
  `AGENTS.md` credential/privacy prose is not a substitute for subagent-visible text.

## Controls and mutations required

| Invariant | Positive/control evidence | Required reversal mutation and expected failure |
|---|---|---|
| Correct skill, intent modes, greenfield approval stop | #747 `tests/test_power_bi_skill_references.py` (`test_current_modes_and_approval_stop`); upstream mode references. | Retire current skill name; route existing report through design; omit missing-page design; pass `approval_pending`; build before later approval. Each must fail its named assertion. |
| No publish/rebind/install and no invented capability | #747 `test_no_management_or_mid_migration_install`; plugin floor/preflight and bundle checks. | Remove prohibition, add `npm install`/`@latest`, allow management, or turn unavailable skill into fallback; assert BLOCKED/preflight and reject each mutation. Static presence/build is not proof of live subagent resolution. |
| Exact generated/shared and authored text reviewed | `tests/test_openability_claim_citations.py:698-720,840-889`; `tests/test_sync_agent_conventions.py:253-255`; `scripts/sync_agent_conventions.py:183-195,350-365`. | Remove/reword one retained safety block, alter shared marker/region, or skip sanctioned pin regeneration: exact pin/sync test fails. Removal of `REQUIRED` pins needs same-change replacement controls, not silent deletion. |
| Capability remains reachable | `tests/test_agent_capabilities.py:57-88,114-159`; `tests/test_build_plugin.py:33-83`. | Hide required token in comment/code or remove agent wiring / shipped skill; registry or marketplace test fails. These checks prove wiring/artifact shape, not runtime obedience. |
| Baseline/canonical tree and edit declarations | `tests/test_check_field_bindings.py:502`; `tests/test_check_migration_progress.py:299-311,442-455,733-756,842-849`. | Treat missing baseline as clean, edit `reports/`, skip declaration, or tamper after declaration; expected baseline/tamper gate fails. |
| No false fidelity / correct layer | Current `tests/test_check_field_bindings.py:161-173,788-805`; #747 future current-mode/query tests; independent validator + blind replay required. | Claim pass from `validate`/`rebuilt`, compare baseline not shipped PBIP, self-fix TMDL, or allow validator edit: named contract fails; blind replay must catch the shipped visual/value discrepancy. |
| Warm model, credential/privacy stop | `.github/skills/pbip-model-refresh/SKILL.md:25-50,177-182`; repo privacy rules in `AGENTS.md:57-60`. Add a direct report-persona contract test if the rule is retained here. | Remove stale-cache/modal stop, retry missing credentials, expose a canary row/capture in public output; direct assertion and secret/privacy scan must fail. Never use customer data as a fixture. |
| No phantom `t2p` dependency | `docs/cli-consensus/consensus.md:45-52,86-97,113-117`; #750 pilot-plan head explicitly says docs-only / N=0. | Add unimplemented `t2p` command/verb as an executable prerequisite: help/capability inventory and plan review must reject it until code and direct tests exist. |

## Overlap, shared-block disposition, and sequence

- **#732 overlap:** it owns deduplicating/updating `.github/skills/powerbi-report-gotchas/SKILL.md`.
  #733 owns this persona. The existing “validate is necessary, screenshot every page” duplication
  should be removed here only as #732 leaves an authoritative current skill pointer; map, schema and
  `shapeMap` claims are not to be copied forward without #732's evidence. Do not edit the gotchas
  skill from this diet. Run skill-index generation and its tests after any #732 heading changes.
- **Other personas:** semantic-builder owns TMDL/DAX, refresh and the #746/#752 AI stamping policy;
  validator is read-only; migrator owns intake, dispatch and migration gates. Report builder may inspect
  model evidence read-only, but routes model changes out. No copied AI policy or orchestration lifecycle.
- Shared block bullets are generated into all personas. **None is removable from this one persona.**
  Later global candidates, only after every persona is covered by direct mutation tests and a tool gate:
  generated-edit declaration (only if all generated layers are covered); pristine→working→deliverable
  and `byPath` promotion (only if every source/datasource path and absent-baseline case is gated);
  PID cleanup (only if run-owned/sibling-instance safety is enforced, not just leak detection).
  Citation/confidence, structural≠fidelity, live limitations, complexity escalation, external-system
  timeouts/credential stops, no silent toolkit patch, message verdict, and safe Git staging are not
  globally removable on current evidence; no single builder skill/tool proves them for every agent.
- Recommended order: merge/rebase #745 → #747 with maintenance/runtime capability still explicitly
  pending; bring #746 into #752 before semantic-policy integration; let #732 settle gotchas; then #733
  make only the report-persona diet with pins/sync regenerated. Treat #750 as a proposal, not a CLI.
  Finish with a fresh, exact-head review and blind comparison on the shipped artifact; shorter text alone
  is not acceptance.
