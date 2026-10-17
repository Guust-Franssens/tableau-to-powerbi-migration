# Dry-run operator persona diet — Refs #733

## Scope and evidence

This is an analysis/report only; no persona, shared block, skill, test, or product file is changed,
and no customer data is included.

Evidence snapshot: master `511bb2098d179f2190943e45c650739c8c794fea` (#748). The future stack was
still open/draft: #745 `1823251d45169cf3dbcec201428b3df2d06bbf1b`, #747
`57cd4310c8fd72e4d1aadd803034761ed119c5fc`, #752 `86190257e40b3a59a1f8972329e732012a249bb0`.
#746's AI policy was merged separately (head `d7a3ebfc298eee075fa3c9abba3c237b8e893868`); #752
says its stack still needs refreshing onto that policy.

**Evidence limit:** issue #733 records 16,392 characters after #720, but no line count. This report
uses that size and existing sync/pin assertions; it is not an exhaustive paragraph census or verified
redline. Re-measure with `sync_agent_conventions.prompt_size` and inspect the exact diff before
implementing the diet.

## Size and reduction target

| Surface | Before / available measure | Proposed target |
|---|---:|---:|
| Whole dry-run persona | 16,392 chars, issue #733's post-#720 measure; lines not recorded | 10–12k chars total, including generated block |
| Generated shared block | 6,661 LF chars / 81 lines, measured from current `AGENTS.md` | unchanged; authored remainder gets about 3.3–5.3k chars |
| Authored remainder | ~9,731 chars by subtraction only, not independently measured | about 45–65% shorter; preserve safety contract over the numeric target |
| Whole-persona lines | Not available from cited baseline | No line cap or estimate: use the character gate and exact source after access |

The target implies a 4.4–6.4k-char (about 27–39%) total reduction. #733's target is a hypothesis,
not a hard limit; keep more text if needed for safety and tests. The shared block is generated into
each persona and pinned separately; do not trim it here (`AGENTS.md:77-157`;
`tests/test_openability_claim_citations.py:208-212,697-741`).

## Future persona contract: proposed outline

1. **Role/layer.** Run a customer-shaped pipeline dry run for the exact scope delegated; observe and
   report failures. Read-only against toolkit/product/persona files. Route a defect to its owning
   layer; do not fix it or impersonate a builder, validator, or engine maintainer.
2. **Inputs.** Require one explicit scope (live Tableau site/project or local folder), source and
   working paths, run identity, requested stage/expected outcome, and available credentials through
   the approved environment. Never accept secrets in argv, silently widen scope, reuse/move a run,
   or compose authoritative run paths when a tool returns them.
3. **Outputs.** Return the observed run path, commands/stages and native exits, artifact/evidence
   references, pass/refuse/cannot-establish verdicts, limitations, and a concise next-owner handoff.
   Keep source rows, captures, credentials, identifiable diagnostics, and private run evidence out
   of public output.
4. **Verdicts.** **PASS** means only that the named dry-run stages/gates completed on observed
   evidence; it is not migration `COMPLETE`, fidelity sign-off, or deployment approval.
   **REFUSE/STOP** for a credential prompt, permission boundary, unsupported scope, destructive
   overwrite, or failing owner gate. **CANNOT_ESTABLISH** for missing, stale, contradictory, or
   inaccessible evidence; never infer success from a green structural check or summary.
5. **Skills and modes.** Use the runbook/`docs/INDEX.md` for the direct pipeline. Invoke
   `live-source-reachability` when its live-source/credential decision applies, and
   `migration-feedback` for a confirmed engineering defect that needs private evidence routing.
   Do not reproduce their procedures. `powerbi-report-cli` modes belong to the report owner, not this
   operator; no `management`, publish, rebind, or DIY report/model edits.
6. **Repo-unique gates and handoff.** Preserve the short-root/authoritative-path and cleanup/privacy
   contracts below. Pass only evidence and exact paths to the next owner; route TMDL/DAX to
   `pbi-semantic-builder`, PBIR to `pbi-report-builder`, and fidelity findings to the read-only
   `pbi-migration-validator`. Escalation prepares a private payload; it does not publish customer
   evidence.

Subagents receive their own persona and task text, not root `AGENTS.md`; skills are opt-in and not
inherited. A pointer alone is therefore not enough: make each required skill an explicit invocation
in the persona and pass exact task inputs (`docs/agent-architecture.md:25-51,71-92`).

## Replacement table

Paragraph names below are functional groups, not claims that every raw paragraph was inspected.

| Existing content class | Disposition | Keep / replace with |
|---|---|---|
| Role, tool permissions, full-pipeline narrative | **Replace** | One role/layer statement and bounded inputs/outputs; no inline toolkit edits. |
| Repeated stage-by-stage script recipes and CLI help | **Replace with pointers** | `docs/INDEX.md`, `scripts/README.md`, `docs/operator-runbook.md`; retain only tested short-root commands and required native-result reporting. |
| Source-scope branches / universal command claims | **Delete or narrow** | Explicit live-site/project or local-folder scope. Pilot `t2p` is project-only if it exists and passes its gates; folders stay on existing direct routes. |
| Power BI authoring, gotchas, model/report repair detail | **Delete; route to owner/skill** | Do not teach TMDL/PBIR or copy #747's report modes into this persona. Preserve layer ownership and validator read-only status. |
| Repeated version checks, installs, and stale skill names | **Replace with pointer** | Preflight is the version/capability authority. No `@latest`, plugin update, or CLI installation during a migration. |
| Credentials, retry loops, and external-system handling | **Retain the stop; replace mechanics with pointers** | Human credential stop outranks autonomy; no bypass or self-authorization. Use the live-source skill/runbook and shared external-system cap. |
| Fidelity/validation claims | **Retain the evidence ceiling** | Dry-run success or structural validation is not fidelity or `COMPLETE`; defer to independent Tableau comparison and the validator. |
| Generic privacy, confidence, cleanup, and no-patch repetition | **Keep only non-shared role-specific clauses** | The generated convention block remains unchanged. Keep the exact run-path cleanup distinction and private escalation boundary. |
| Long recovery/path-limit explanation | **Replace with pointer, retain local exception** | `docs/windows-path-limits.md`; preserve tested same-user `subst` limits and short-root allocation contract. |

## Current dependencies and transitional wording

- **#745 / preflight:** the proposed floor is a fail-closed capability boundary; its PR reports no
  plugin/CLI maintenance acceptance. Do not tell this persona to update tools. The migration phase
  remains blocked by a missing capability until approved between-session maintenance and fresh
  preflight establish it (#745 head above; `docs/operator-runbook.md:165-178`).
- **#747 / report skill:** its prospective `powerbi-report-cli` route is `authoring` for an existing
  report, `design` then `authoring` for a missing page, and greenfield `planning` only with a locked
  spec and `approval_pending`. It forbids management/publish/rebind and says absent capability is
  BLOCKED. The PR's upstream 0.3.19 disk oracle is not a fresh runtime probe; keep the wording
  conditional until maintenance, publication, installed-content sync, and a fresh-session probe
  pass ([#747 exact head](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/pull/747),
  `57cd4310c8fd72e4d1aadd803034761ed119c5fc`).
- **#746 / #752 AI policy:** #746 is merged; #752 is still a draft semantic-persona wiring stack and
  explicitly says its base does not yet contain #746. It assigns the local AI skill sole policy/
  mechanics authority and preserves the unowned-text refusal. This operator should route AI/model
  work, not repeat or expand that exception (PR #752 head; `.github/skills/powerbi-ai-readiness/SKILL.md:13-37,76-90`).
- **#748 / t2p pilot:** master records a proposal, not a delivered command. `t2p start/status` is
  live-project-only; local folders, builders, validation, promotion, deploy, and dispatch are
  excluded. Safe pre-network path projection and exact first-failure/native-exit behavior are
  blockers. No `scripts/t2p*` exists in this checkout. Do not add a command, status, `next`, or local
  file route to the persona until implemented, tested, and explicitly supported
  (`docs/cli-consensus/consensus.md:45-71,121-138,153-159`; master #748 head above).
- **Initial pilot transition:** until the same-scope blind comparison and pilot acceptance are
  complete, keep `scripts/README.md` and the runbook as the executable path. If later accepted, add
  only the verified live-project `start/status` pointer; local-folder dry runs remain direct. A
  successful `start` cannot be described as readiness, fidelity, or completion.

## Retained invariants and mutation evidence

| Invariant that must survive | Existing evidence / control | Required future reversal control |
|---|---|---|
| Allocator JSON, not agent-composed paths, governs stages; use the actual short root | `tests/test_sync_agent_conventions.py:527-534,606-616` | Delete the authoritative-JSON anchor or substitute a guessed path; the named direct assertion must fail. |
| Do not keep an unconditional in-repo allocator beside short-root allocation | `tests/test_sync_agent_conventions.py:552-557,596-603` | Restore that call; the absence test must fail. |
| `subst` is temporary, same-user, machine-local, and waives no gate | `tests/test_sync_agent_conventions.py:505-515` | Mutate each qualifier or remove the persona anchor; the named contract mutation must fail. |
| Keep repo-local vs external short-root cleanup/privacy distinct; leave evidence and siblings alone | `tests/test_sync_agent_conventions.py:517-543` | Remove each privacy/cleanup anchor; its named deletion mutation must fail. |
| Credentials, human authorization, privacy and no-patch rules remain hard stops | `AGENTS.md:55-60,121-145`; `tests/test_sync_agent_conventions.py:431-447,726-729` | Mutate STOP to retry/authorize, expose a secret, or patch a gate; direct assertions and a sanitized-output fixture must fail. |
| No false PASS: unknown evidence, capture-only success, or clean structure is not fidelity/COMPLETE | `AGENTS.md:35-39,101-116`; `docs/cli-consensus/consensus.md:64-71` | Mutate unknown/missing evidence or start exit 0 into PASS/COMPLETE; intended verdict assertion must fail. |
| Correct layer handoff and skills are actually reachable | `docs/agent-capability-wiring.md:3-5,19-27`; `docs/agent-architecture.md:71-92` | Remove a required visible token and separately suppress its skill invocation; capability gate and a controlled skill-canary must fail. |
| Every persona instruction and generated block stays reviewed, lossless, synchronized | `tests/test_openability_claim_citations.py:697-741,840-869`; `tests/test_sync_agent_conventions.py:619-637` | Delete/reword one pinned block without regenerating; pin test fails. Then mutate each role anchor; its direct contract fails. |

The capability registry proves visible wiring, not that a subagent actually loaded or followed a
skill. Pin/sync tests prove reviewed bytes and shared-block identity, not fidelity or behavioral
obedience. Keep a fresh-session skill canary and blind migration replay as separate acceptance
evidence; never count a static test as runtime proof.

## Conflicts and shared-convention ownership

- **#732:** it owns upstream-vs-local gotcha deduplication in skills/docs, including unique cache,
  credential-prompt, screenshot-stability, Tableau, and cross-tree binding knowledge. #733 owns the
  persona contract. Do not remove skill material or move another persona's gotchas while reducing
  this one (issue #732).
- **#745/#747/#752:** their changes are scoped to preflight/CLI, the three builder/validator/migrator
  personas and semantic AI policy. Keep the dry-run operator read-only and do not collide with those
  owners. Refresh this draft stack onto merged #746 before using it as the implementation base.
- **#748:** its one-file CLI consensus explicitly flags unindexed new Markdown as a navigation-gate
  risk. This task's one-file constraint prevents adding an `INDEX.md` entry; the new report will
  therefore need that scope conflict resolved before it can pass the navigation gate
  (`docs/INDEX.md:45-47`; `scripts/check_navigation_index.py:50-66`).

No shared-conventions bullet is removable because one persona is shorter. The potentially
skill/tool-backed subjects—citation/evidence, confidence labels, layer ownership, stage separation,
structural-vs-fidelity, limitations, edit declarations, credential/time stops, no-patch, and
PID-scoped cleanup—can be reconsidered **only as a global change** after every persona has equivalent
reachable guidance and direct controls. Keep the generated block verbatim for this report. Shared
block removal requires coordinated `AGENTS.md` change, regeneration, pin update, all-persona mutation
coverage, sync check, and independent blind replay—not an exception in the dry-run persona.

## Validation and next dependency

This 156-line file is an analysis artifact, not a persona edit or passed pilot. Exact paragraph
deletions and source line counts remain unverified. Before implementation, resolve the stack refresh
and pilot blockers, inspect the persona, freeze a source-to-target diff, and add/extend reversible
tests for each retained boundary. Navigation fails until an index update is separately authorized;
this report does not silently widen the one-file scope.
