# #733 — `pbi-semantic-builder` persona diet (proposal)

**Scope:** design report only; no persona, skill, shared convention, or product changes. Evidence is
current `origin/master` (`511bb20`, #748), the #733 issue, and future heads #745
(`1823251d45169cf3dbcec201428b3df2d06bbf1b`), #747
(`57cd4310c8fd72e4d1aadd803034761ed119c5fc`), and #752
(`86190257e40b3a59a1f8972329e732012a249bb0`). #745/#747/#752 are draft stack evidence, not merged
behavior. #746's AI policy is merged to master, but #752 notes the exact #747 stack does not contain it.
Rebase and revalidate before using the #752 text as the final base.

## Measurement and confidence

| Measure | Before / evidence | Target after stack and diet |
|---|---|---|
| Whole persona | #733 records 21,657 chars on master; #747 reports 21,884 at its head; #752 reports CRLF `prompt_size` 21,990 → 21,037. These branch states and reported measures disagree; use none as a false exact cross-PR delta. | ~10.7–12.7k LF chars total (about 41–51% below the #733 baseline); not a hard cap. |
| Generated block | `AGENTS.md` canonical block: 6,661 LF chars / 81 lines, from `<!-- BEGIN/END:shared-conventions -->` (77–157). `sync_agent_conventions.py` copies it into every persona. | Keep unchanged: still ~6.7k chars / 81 lines. Reduce only persona-authored text. |
| Authored text | #733 whole-file count less the same block estimates ~14,996 chars. This is arithmetic, not a direct persona recount. Exact master line count and exact authored paragraph boundaries are not recorded by #733 or these pins. | ~4–6k chars / 55–75 lines, plus the generated 81 lines: ~136–156 total lines. Re-measure on rebased bytes with `prompt_size`; no exact line claim until then. |

The baseline is from issue #733's size table; the shared-block measurement is reproducible from
`AGENTS.md:77-157`. The persona's direct line-by-line inventory was not available in the evidence set,
so the table below maps the indexed themes, not fabricated paragraph numbers or a purported exhaustive
token diff. `tests/persona_pins.txt:324-434` and #752's N=3 report identify the current role, skills,
inputs, AI-readiness section, and handoff topics. An authorized source diff should replace this
limitation before implementing the diet.

## Proposed thin contract (exact outline)

1. **Role / boundary (2–3 sentences):** finish residual TMDL/DAX in the existing, engine-emitted and
   bound semantic model; never create the model from scratch. Own semantic-model files only. PBIR and
   visuals belong to `pbi-report-builder`; fidelity sign-off belongs to the read-only
   `pbi-migration-validator`; orchestration remains with `tableau-migrator`.
2. **Inputs / stop conditions (short checklist):** exact run/unit and canonical working tree; model
   path; handover/residual queue and applicable migration spec/reference evidence. Use
   `scripts/read_handover.py` and the handed tree, not guessed paths. If the model/baseline, scope,
   permission, capability, or required evidence is missing/ambiguous, refuse or report
   `cannot-establish`; do not bootstrap, rerun over edits, or infer consent.
3. **Required skill routing (one line each, conditional):**
   - `semantic-model-authoring` before model authoring: select its existing-model workflow and
     applicable TMDL/DAX/field-parameter references. Follow its current MCP-first / PBIP fallback
     and stop when neither supported path exists; do not invoke creation, deploy, or refresh routes
     outside the delegated task. Current upstream selector/Tier 1–2 rules are in Microsoft's
     [skill](https://github.com/microsoft/skills-for-fabric/blob/main/plugins/powerbi-authoring/skills/semantic-model-authoring/SKILL.md);
     it calls Prep-for-AI artifacts user-configured. The merged local #731 policy is a narrow
     exception, not permission to generalize upstream's draft-only route
     ([upstream guidance](https://github.com/microsoft/skills-for-fabric/blob/main/plugins/powerbi-authoring/skills/semantic-model-authoring/references/semantic-model-ai-readiness.md);
     local authority: `.github/skills/powerbi-ai-readiness/SKILL.md:13-74`).
   - `powerbi-semantic-model-gotchas` before hand-authoring TMDL or when a semantic defect maps there.
   - `pbip-model-refresh` only when refresh/persistence is required; it owns commands, PID binding,
     persistence mechanics, and evidence ceilings.
   - `powerbi-ai-readiness` when AI readiness is in scope; this local skill is the sole authority
     for the deliberate #731 stamping exception and handoff gate.
4. **Do / verify:** make only declared semantic-layer edits; use the applicable structural/model and
   live-query checks, then refresh/persist only when required. A parse or green structural validator
   is not proof of Desktop openability, data correctness, Tableau numeric fidelity, service round-trip,
   or Copilot consumption.
5. **Verdict / handoff:** report exact edited paths, authored vs engine output, checks and their
   evidence/estate, remaining limitations, and the `DATA_OK + PERSISTED` refresh result if required.
   State what cannot be established and route PBIR/fidelity findings to their owners; never claim
   `COMPLETE` or visual/numeric match on the model builder's own assertion.

## Replacement table

| Indexed paragraph/token family | Action | Future persona text / destination |
|---|---|---|
| Role and “finish the model that already exists” | **Retain, replace** | Compress to outline §1; preserve no-new-model and semantic-only ownership. Do not say the builder owns the whole PBIP. |
| Detailed model input expectations and steps | **Retain, replace** | Keep exact working-tree/model/handover requirements and stop states (§2); replace procedural enumeration with `read_handover.py` and current skill pointers. |
| TMDL, DAX, table/column/measure, field-parameter and modeling recipes | **Replace with skill pointer** | `semantic-model-authoring` owns upstream procedure/references; local semantic gotchas remain the repo-specific supplement. Do not copy upstream prose or claim unavailable MCP routes. |
| Long gotcha examples/checklists | **Replace with skill pointer** | Point to `powerbi-semantic-model-gotchas`; retain only a named trigger, not duplicated craft guidance. #732 is open, so do not assume any skill deletions/stale-claim fixes have landed. |
| Refresh commands, flags, Desktop process targeting, cache persistence | **Replace with skill pointer; retain gate** | `pbip-model-refresh` owns mechanics. Keep only “when delegated, require `REFRESH: DATA_OK + PERSISTED`”; never promote refresh to semantic-builder-owned procedure. |
| AI readiness authoring guidance | **Replace with local skill; retain exception** | `powerbi-ai-readiness` owns the mechanics. Persona must say no unowned replacement/adoption, no synonyms/AI-schema authoring, and no service/Copilot claim; scoped strict model+Markdown check after final reopen/refresh/save is required when stamping is in scope. |
| Output and handoff checklist | **Retain, compress** | Keep paths, what changed vs engine, validation evidence, unresolved limitations, refresh verdict, and explicit non-claims (§5). |
| Citations/confidence, baseline→working→deliverable, generated-edit declarations, limitation updates | **Do not locally duplicate** | Leave in generated shared block. They are cross-agent invariants, not model-builder-specific text. |
| Examples, repeated rationale, static command lists and duplicated universal boundaries | **Delete or pointer** | Delete only when the outline/skill/shared block/tool owns the invariant; do not remove a fail-closed decision merely to hit a size target. |

The #752 future diff itself reports a net 953-character persona decrease (21,990 → 21,037 on its
CRLF `prompt_size` basis), only about 4.3%. It is a policy-routing change, not the diet: its contract
and 16 reversal controls should be carried forward, then the next persona edit should be a substantial
procedure-to-skill replacement. Target authored text 4–6k chars; if the required boundaries do not fit,
keep them and report a smaller reduction rather than deleting safeguards.

## Retained outcomes, controls, and mutations

| Invariant | Required evidence/control |
|---|---|
| Semantic-only ownership; no created model; report-layer issues routed away | #752's direct semantic contract assertions; reversal controls for broad override and layer/ownership boundary; independent validator remains read-only (`AGENTS.md:91-107`). |
| Never replace/adopt unowned AI instructions; no synonym or service-state authoring | Local policy and script tests: `.github/skills/powerbi-ai-readiness/SKILL.md:13-74,85-90`; `tests/test_sync_agent_conventions.py` semantic mutations from #752: unowned replacement, equal-text adoption, per-target stamping, portal/service edits, and synonym authoring. |
| AI stamp is scoped and sealed, with Q&A enabled; no premature/weak pass | Explicit model **and** Markdown, strict check, and post-final-operation seal (`powerbi-ai-readiness/SKILL.md:62-74,167-180`); #752 reversals: absent Markdown, non-strict gate, pre-seal check, allowed failed handoff, and `qnaEnabled` false. |
| Refresh is not an unverified reload/cache claim | Current refresh skill description and repo capability row (`.github/skills/pbip-model-refresh/SKILL.md:3`; `docs/agent-capability-wiring.md:20`); #752 reversal for invented reload/cache result. |
| Structural checks do not become fidelity/openability claims | `AGENTS.md:111-121`; CLI consensus says builders author and validator compares shipped bytes (`docs/cli-consensus/consensus.md:94-97`); require a “cannot establish” negative control. |
| Shared block and pin/capability wiring survive shortening | Regenerate only through `scripts/sync_agent_conventions.py`; `--check`, bundle-path tests, and semantic pin tests must pass (`scripts/sync_agent_conventions.py:359-367,441-496`; `tests/test_sync_agent_conventions.py:253-255,325-334`; `tests/test_agent_capabilities.py:57-89`). |

#752 reports 16 reversals caught at head `86190257`; examples above map the stated controls, not
independent reruns here. Its own body says maintenance/rebase and blind run 2 remain outstanding.
Add direct semantic persona mutations for any retained invariant not covered after rebasing; a shorter
prompt and passing length/pin tests alone do not prove behavior.

## Dependencies, conflicts, and shared-block policy

- **#745 → #747 → #752 → #733.** #745 establishes current CLI/plugin floors; #747 routes retired
  report skills to `powerbi-report-cli` modes; #752 adds the merged #746 AI policy to its persona.
  #745/#747/#752 are draft at the cited heads. The base lineage is stale relative to merged #746/#748;
  refresh stack, plugin/profile, generated pins, and exact-head review before implementation/acceptance.
- **#748 / initial t2p pilot:** master contains a *consensus document*, not a builder command. It proposes
  only live-project `t2p start` and read-only `status`; no builder/refresh/validation/dispatch route is
  in pilot scope, and first-failure semantics remain a stated blocker
  (`docs/cli-consensus/consensus.md:48-59,71,94-97,133-155`). Do not name or invoke an unimplemented
  `t2p` command in this contract.
- **#732:** open gotcha dedup/staleness work overlaps only with pointer wording; give its owner the
  skill changes. This report edits neither skills nor any other persona. The semantic builder's
  handoff must still route PBIR to report-builder and independent comparison to validator.
- **Shared conventions:** keep the generated block byte-for-byte for this persona change. Later
  removal is only global at `AGENTS.md` followed by sync and all-agent verification—not an individual
  persona trim. No current safety/fidelity/privacy/credential/layer bullet is removable based on
  evidence here. Possible later global candidates are the conversational “end every message” reminder
  and coding-agent-only scratch/git staging advice, but only after deciding they do not govern any
  custom agent. Citations/confidence, own-layer boundaries, stage custody, structural-validation
  limits, live limitations, edit declarations, complexity warnings, credential/external-system stops,
  no silent engine patch, privacy, and PID-scoped cleanup still need universal enforcement or must
  remain shared.

**Next:** rebase the future stack, obtain the authorized persona diff and exact LF/CRLF line counts,
then implement this proposal as a separate, mutation-tested persona change. No product or persona
edit is authorized by this report.
