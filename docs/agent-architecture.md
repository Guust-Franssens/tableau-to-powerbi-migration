# Agent architecture — what reaches an agent, and how to change it

Why this file exists: this repo's four agents grew organically, and several conventions were written
on assumptions that turned out to be wrong. This is the researched, cited baseline — what GitHub
actually documents, what was measured here, and what is still unknown. Read it before changing
anything under `.github/agents/`.

Last verified **2026-07-31**, by fetching the primary sources directly (not via a research summary)
and by running the §6.1 subagent experiment. This area moves fast; re-check before relying on a claim.

> **Corrections landed 2026-07-31.** Three claims in the previous revision were wrong and had been
> written from an unverified research summary: (a) "there is no `skills` property" — there is one, on
> the SDK surface (§2); (b) the 10 KB `additionalContext` cap was attributed to hooks generally — it
> is documented for `postToolUse` only (§4); (c) `subagentStart`'s `additionalContext` field name was
> labelled "inferred" — it is documented (§4). Cite the primary source, not this file, when it matters.

---

## 1. The core constraint: a subagent sees only its own persona

A custom agent invoked **as a subagent** (via the Task/agent tool) receives:

- its own `.agent.md` prompt body, and
- the task text the orchestrator passes it.

It does **not** receive `AGENTS.md`, `.github/copilot-instructions.md`, user-global instructions, or
the parent's conversation. Skills are not inherited from the parent either — that one is now
documented, not just measured:

> "Skills are **opt-in**: agents receive no skills by default, and sub-agents do not inherit skills
> from the parent. Skill names are resolved from the session-level `skillDirectories`."
> — <https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/custom-agents> (Per-agent skills)

This is **intended design**, not a bug — GitHub's SDK states it plainly:

> "Provide each sub-agent with complete context; sub-agents are stateless across calls."
> — <https://github.com/github/copilot-sdk/blob/main/docs/features/fleet-mode.md>

Same agent invoked as a **root session** (`copilot --agent=<name>`) *does* get the instruction files,
because then it *is* the main session. Same file, different bootstrap path. Verified here with a
sentinel experiment (2026-07-30): root saw all sentinels, subagent saw none.

Consequence, and the rule to remember: **text that is not physically in a persona does not apply to
that agent.** `@file` includes work in `AGENTS.md`/`copilot-instructions.md` but **not** in
`.agent.md`.

## 2. Frontmatter schema

Canonical reference: <https://docs.github.com/en/copilot/reference/custom-agents-configuration>

| Property | Notes |
|---|---|
| `name` | Optional display name |
| `description` | **Required.** Used to auto-select the agent — be specific |
| `tools` | Allow-list. Omit = all tools. **See the enforcement caveat below** |
| `model` | Optional; unset inherits the session model |
| `target` | `vscode` or `github-copilot`; unset = both |
| `disable-model-invocation` | `true` = must be selected explicitly, never auto-delegated |
| `user-invocable` | `false` = programmatic only |
| `mcp-servers` | Per-agent MCP servers |
| `metadata` | Annotation only |
| `infer` | **Retired** — use the two booleans above |
| `deferred-tool-loading` | Appears in the tool-search docs, absent from the canonical reference — treat as unverified |

**`skills` — surface-dependent, and the earlier "no such property" claim was wrong.** A `skills`
property **does** exist, on the **SDK** agent-definition surface, and it does exactly what per-agent
progressive disclosure needs:

> `| skills | string[] | Skill names to preload into the agent's context at startup |`
> "When specified, the **full content** of each listed skill is eagerly injected into the agent's
> context at startup — the agent doesn't need to invoke a skill tool; the instructions are already
> present. Skills are **opt-in**: agents receive no skills by default, and sub-agents do not inherit
> skills from the parent. Skill names are resolved from the session-level `skillDirectories`."
> — <https://github.com/github/copilot-sdk/blob/main/docs/features/custom-agents.md>

It is **absent from the `.agent.md` YAML frontmatter table** at the canonical reference above, so it
is not known to apply to the markdown persona files this repo uses. Do not write `skills:` into an
`.agent.md` and assume it works — §6.1 measured that repo-local skills do not even reach a subagent's
skill registry. Treat it as: *documented for SDK-defined agents, unverified for `.agent.md`.*

VS Code-only (ignored elsewhere): `argument-hint`, `agents`, `handoffs`, `hooks`.

**Prompt cap: 30,000 characters** for the markdown body. Measured here: Copilot CLI does **not**
enforce it (a 48k persona quoted its own final section back verbatim). The failure mode on the
GitHub-hosted path — truncate or reject — is **untested**. Since the tail of our personas is the
accumulated Gotchas, treat over-cap as a portability risk and keep it visible:
`python scripts/sync_agent_conventions.py --check` prints each persona's size.

## 3. Enforcement vs. advice

Only two things actually constrain an agent. Everything else is advisory prose, and "MANDATORY"
prose with nothing behind it is a named anti-pattern — one this repo has been bitten by repeatedly.

| Mechanism | Enforces? |
|---|---|
| `tools` allow-list | Documented as enforcement. **Measured 2026-07-30: Copilot CLI did NOT apply it** — a validator declared `read/search/execute/web` still held `edit`, `create` and `task`. Treat as a declaration honoured where the platform implements it, not a sandbox |
| Hooks (`.github/hooks/*.json`) | Yes — documented as deterministic, with guaranteed execution at lifecycle events |
| Prose ("MANDATORY", "never", "always") | **No.** Advisory only |
| A gate the workflow must pass (script exit code) | Yes, in practice — this is why the repo prefers scripts to bullets |

## 4. Hooks — the documented answer to per-agent context

Reference: <https://docs.github.com/en/copilot/reference/hooks-reference>

`subagentStart` fires **before a named custom subagent runs**, takes a `matcher` regex on the agent
name, and can inject `additionalContext` that is prepended to the subagent's prompt. Its input
carries `sessionId`, `timestamp`, `cwd`, `transcriptPath`, `agentName`, `agentDisplayName`,
`agentDescription`.

That makes it the documented mechanism for **per-agent progressive disclosure** — the thing a
frontmatter `skills` property would have provided on the `.agent.md` surface. Crucially, injected
context counts against the **context window**, not the 30,000-char persona cap.

The `additionalContext` field name for this event is **documented, not inferred**: the events table
states "Optional — cannot block creation, but `additionalContext` is prepended to the subagent's
prompt." Note there is no dedicated TypeScript *Output* block for `subagentStart` (unlike
`postToolUse` and `notification`, which have explicit ones) — only that sentence.

Notes worth knowing before building on it:
- The built-in `general-purpose` agent does **not** emit `subagentStart`/`subagentStop`. Custom
  agents do.
- It can inject but **cannot block** subagent creation.
- The **~10 KB cap is documented for `postToolUse` only** — "When multiple hooks return
  `additionalContext`, the results are joined with a double newline and capped at 10 KB." No cap is
  stated anywhere for `subagentStart`. Do not assume the two behave alike.
- `subagentStart` **also fires under Copilot cloud agent** (events table, Cloud agent column:
  "Fires."), so a hook-based design is not automatically broken on the hosted path.
- `preToolUse` payload has **no `agentName`** — so a global `preToolUse` hook cannot cheaply say
  "block edits *only* for the validator".
- **A repo hook is not a guarantee you control.** Hooks load from six places — policy-level
  (machine-wide, and *cannot* be switched off by `disableAllHooks`), `.github/hooks/*.json`,
  user-level `~/.copilot/hooks/`, inline `hooks` in `.github/copilot/settings.json`, inline `hooks`
  in `~/.copilot/settings.json`, and plugin-contributed hooks. Any contributor can set
  `disableAllHooks: true` in their repository `settings.json` and silently disable **every** repo
  hook for their sessions. That is the decisive argument against moving anything load-bearing out of
  the personas and into a hook — see §5.

⚠️ **Measured 2026-07-31: the probe hook never executed.** `.github/hooks/subagent-context.json` +
`scripts/hooks/probe_subagent_start.ps1` wrote **no** `_hook_probe.log` line when a custom subagent
was invoked, and the subagent reported nothing prepended to its prompt (it begins directly with its
own `#` heading). The session predated the hook file, which is consistent with hooks being snapshotted
at session start — so this is "not loaded", **not** "fires but wrong output field". Re-run in a fresh
session to separate the two; §6.2 has the outcome table.

## 5. What currently lives where

| Content | Where it is | Reaches a subagent? |
|---|---|---|
| Shared conventions | Generated into all four personas by `scripts/sync_agent_conventions.py`, CI-gated for drift | Yes — because it is physically in each persona |
| `docs/tableau-dax-translation-guide.md` (24k) | External; persona says "Read … before starting" | Only if the agent actually reads it — advisory |
| `docs/migration-spec.md` (9k) | External; same instruction | Same |
| `.github/pbi.kb/visual-cookbook.md` (10k) | External; referenced at point of use | Same |
| `.github/skills/pbip-model-refresh/` (SKILL.md + `scripts/` + `tests/`) | Repo-local **skill bundle** | **Only if the persona points at the path.** §6.1 measured that a repo-local skill is not in a subagent's registry at all, so the *name* does not resolve there — the persona must say **"read `.github/skills/pbip-model-refresh/SKILL.md`"**, which is an ordinary `view` call and does reach a subagent |
| `.github/skills/powerbi-ai-readiness/` (SKILL.md + `scripts/` + `tests/`) | Repo-local **skill bundle** | Same — `pbi-semantic-builder` reads it **by path** (done, #33). It absorbed `docs/ai-instructions-authoring-guide.md` (now a stub) and that persona's ~7.3 KB "Prep the model for AI" section — two copies of one recipe that had already diverged (§8), now one |
| Per-agent Gotchas | Inline in each persona | Yes |

An **explicit instruction to read a file** is a normal tool call and does reach a subagent — this is
different from passive inheritance, and the repo already depends on it for ~44 KB. The documented
anti-pattern ("requests to refer to external resources") is about *unresolved ambient conformance*
("conform to styleguide.md"), not an imperative, verifiable step. But it is still **discretionary**:
an agent may skip it.

**Why package procedural knowledge as a skill anyway**, now that §6.1 has resolved *against* a skill
being auto-delivered to a subagent. Four reasons, none of which depended on that outcome:

1. **It is strictly no worse than the status quo.** A skill file is still a readable path, so the
   floor is today's advisory "read this file" behaviour — and per §6.1 that floor is also the
   ceiling inside a subagent, until a bundle is promoted to a plugin. There is no downside branch: a
   bundle is never worse than the paragraph it replaced.
2. **It reclaims persona budget.** `tableau-migrator` is already 108% of the 30,000-char cap (§2), and
   the tail of a persona is exactly where the accumulated knowledge lives. Procedure that is not
   Tableau-specific does not belong in a Tableau persona at all.
3. **It is the only shape that ports.** Nothing about refreshing and persisting a PBIP — or about
   `CustomInstructions` and `qnaEnabled` — is source-tool-specific; the input is already a Power BI
   model, so the same procedure is needed by a Qlik or Cognos migration. **One self-contained folder**
   moves — `SKILL.md`, the scripts it runs and the tests that gate them; a paragraph inside
   `pbi-semantic-builder.agent.md` does not. That is why each bundle owns its `scripts/` and `tests/`
   instead of borrowing the repo's, and why `tests/test_skills.py` proves it by copying the folder to
   a temp dir and running its tests there with this repo unimportable.
4. **It collapses duplicate copies into one.** `powerbi-ai-readiness` replaced a persona section and a
   `docs/` guide that stated the same recipe twice; they had already diverged (one listed the
   "Verified headline numbers" section, the other did not), which is §8's "conflicting instructions
   across files" in the wild. A skill gives the knowledge exactly one home, and
   `tests/test_skills.py` now fails if the section template reappears elsewhere.

**Relocation does not enforce anything, though.** Prose is advisory wherever it lives (§8), so moving
words only pays off if the mandate rides on an exit code. `powerbi-ai-readiness` therefore ships the
scoped gate the prose always implied: `set_ai_instructions.py --check --strict --model <model>` fails
closed on the one model an agent is accountable for, while the repo-wide `--check` stays advisory in
CI as a visible backlog. A repo-wide `--strict` would fail on every model that predates the layer,
which is a gate nobody can ever switch on.

**How a persona must reference a bundle.** §6.1 settled the discriminator: a subagent listed 91
skills, every one from a plugin or user-global directory and **none** project-local — so
**registration scope**, not the subagent boundary, is what excludes a repo-local skill. A persona
must therefore say **"read `.github/skills/<name>/SKILL.md`"** (an ordinary `view` call, which
demonstrably reaches a subagent) and **not** "use the `<name>` skill", whose name does not resolve
there. Promoting a proven bundle into a plugin/global collection is the fix that would make the name
work; authoring the procedure as a bundle now keeps that path open without a rewrite.

### Why the shared block stays generated into the personas

It is tempting to move the ~6 KB shared-conventions block out of all four personas and into a
`subagentStart` hook, reclaiming ~24 KB of budget. **Don't** — at least not for the conventions that
must always hold. Ranked by how they fail:

| Mechanism | Fails when | Failure mode |
|---|---|---|
| Generated into the persona | never — it is the prompt | — |
| `subagentStart` hook | contributor sets `disableAllHooks`; hook file added mid-session; IDE/other runtime that does not run repo hooks; fresh clone before trust is granted | **silent** — the agent simply behaves as if the rules never existed |
| Repo-local skill | always, inside a subagent (§6.1) | silent |

The hook's failure mode is the disqualifier: nothing errors, the conventions just quietly stop
applying, and the first symptom is an agent doing the exact thing the block forbids (retrying a dead
credential for two hours, editing a layer it does not own). Duplication that CI keeps in sync is the
cheaper problem. **Rule: anything whose absence is silent and harmful stays inline; a hook may only
*supplement*.** If the persona budget must come down, cut per-agent Gotchas that have gone stale —
that is what orchestrator step 12 is for.

**The same rule blocks the obvious next trim, so state it before someone tries it.** `Gotchas`
sections are ~36% of `pbi-report-builder` (16,788 chars across three), which makes them the fattest
remaining target — but relocating them into `.github/pbi.kb/` puts them in the *discretionary* row of
the §5 table, and a gotcha the agent stops seeing is a defect it silently repeats. Gotchas are the
"absence is silent and harmful" category almost by definition. The only safe reduction is **deleting
ones with evidence they are obsolete** (fixed upstream, superseded by a script gate), which needs a
real migration's retrospective — not a guess.

Budget after #33 pointed `pbi-semantic-builder` at both bundles:

| Persona | chars | % of 30k cap |
|---|---|---|
| `pbi-report-builder` | 46,051 | 153% |
| `pbi-semantic-builder` | 42,796 | 142% (was 160% / 48,049) |
| `tableau-migrator` | 32,574 | 108% |
| `pbi-migration-validator` | 17,677 | 58% |

Three are still over. That is a **portability risk, not a live bug** — Copilot CLI was measured not to
enforce the cap (§2), and the hosted failure mode is untested. Track it; don't panic-trim into the
silent-failure category above.

## 6. Experiments — one resolved, one still open

1. **Do repository skills reach a subagent? — ✅ RESOLVED 2026-07-31: NO.**
   `.github/skills/sentinel-probe/SKILL.md` contains `SKILL_SENTINEL_ZEPHYR_74193`. A
   `pbi-migration-validator` subagent was asked to quote it **without being told the value**, and
   **under a no-tools rule** so it could not read the file and fake a hit. Result:

   | Probe | Result |
   |---|---|
   | `sentinel-probe` skill (repo-local, `.github/skills/`) | **ABSENT** — not in the subagent's skill registry at all, not even by name |
   | Its `SKILL_SENTINEL_*` token | **ABSENT** |
   | `HOOK_SENTINEL_*` (see §6.2) | **ABSENT** — nothing prepended; prompt begins at its own `#` heading |
   | *Control:* own persona | **PRESENT** |
   | *Control:* generated shared-conventions block | **PRESENT**, quoted verbatim |
   | *Control:* `AGENTS.md` | **ABSENT** — as predicted |

   Two findings, both load-bearing:
   - **Repo-local skills are root-only.** The parent session lists `sentinel-probe` with
     `<location>project</location>`; the subagent listed **91** skills — every one of them from a
     plugin or from user-global `~/.copilot/skills/` — and **no** project-local skill. The
     discriminator is *registration scope*, not merely the subagent boundary.
   - **No skill's body is ever injected**, for any skill, in a subagent. 26 arrived as
     name + description, 65 as a bare name in an "Additional skills available (invoke by name)"
     list. So a skill can at best be *invoked* by name — it can never silently carry content the way
     the SDK's `skills:` preload does (§2).

   **Consequence for persona authoring:** an instruction of the form "use the `<x>` skill" **does not
   work** for a repo-local skill inside a subagent. A second probe (2026-07-31) settled the narrower
   question of whether an *explicit* invocation resolves even though the name is unlisted: a
   subagent was told to call the `skill` tool with `sentinel-probe` and nothing else. It has the
   tool, it made the call, and it got a hard failure:

   > `Skill "sentinel-probe" not found. Available skills: account-explorer, acr, … xlsx`

   — 91 names, every one from a plugin or user-global directory, no project-local entry. So naming
   the skill in the persona is **not** a workaround for it being unlisted; both paths fail. Use an
   explicit **file path** — "read `.github/skills/<x>/SKILL.md`" — which is an ordinary `view` call
   and demonstrably works.

   **The fix is to publish, and it is proven end to end (2026-07-31).** Packaging the two
   source-tool-agnostic bundles as a marketplace plugin makes the name resolve:

   ```
   copilot plugin marketplace add Guust-Franssens/powerbi-migration-skills
   copilot plugin install powerbi-migration-skills@powerbi-migration-collection
   → Plugin "powerbi-migration-skills" installed successfully. Installed 2 skills.
   ```

   A **fresh** session then lists both, and invoking one by name returns its body:
   `skill(powerbi-ai-readiness)` → `# Power BI AI readiness`. Built by
   `scripts/build_plugin.py`; see §5 for why it publishes to a separate repo.

   ⚠️ **Two traps when testing this.** (a) Skills are snapshotted at **session start** — a plugin
   installed mid-session is invisible to that session *and* its subagents, which looks exactly like a
   broken plugin. Restart before concluding anything. (b) In non-interactive `-p` mode, subagents get
   **no `skill` tool at all** (`Tool 'skill' is not available.`), so `-p` cannot test the subagent hop.
   Neither is a defect in the plugin; both produce convincing false negatives.

2. **Does `subagentStart` fire and inject? — ⬜ STILL OPEN (needs a fresh session).**
   `.github/hooks/subagent-context.json` + `scripts/hooks/probe_subagent_start.ps1` log the payload
   to `_hook_probe.log` and inject `HOOK_SENTINEL_ORBIT_58231`.
   Measured 2026-07-31: **no log, no sentinel** — but that session predated the hook file, so it only
   shows "not loaded", not "does not work". **Restart the CLI**, then invoke `pbi-migration-validator`
   and check both:
   - Log written **and** sentinel visible → the mechanism works; a hook *could* carry supplementary
     context. Even then, see §5 before moving anything load-bearing into it.
   - Log written, sentinel absent → hook fires but the output shape is wrong. The field name itself
     is documented (§4), so suspect the JSON envelope or the exit code.
   - No log again, in a session that started *after* the file existed → the hook is genuinely not
     loading; check location and precedence before designing around it.

3. **Is the `tools` allow-list enforced on the current CLI?** Re-run the validator probe: ask whether
   it holds `edit`/`create`/`task`. Last measured 2026-07-30: not enforced.

## 7. Things the docs genuinely do not say

Recorded so nobody re-derives them or fills the gap with a plausible guess:

1. ~~Whether a repo-local skill, once *named* in a persona, can still be **invoked** from inside a
   subagent.~~ **Answered 2026-07-31 — no.** See §6.1; the `skill` tool rejects the name outright.
2. Whether `subagentStart`'s `additionalContext` has any size cap. The **field name is documented**
   (§4) — that entry previously said it was inferred, which was wrong. What is genuinely missing is a
   dedicated Output block and any stated cap; the 10 KB figure belongs to `postToolUse`.
3. Whether `deferred-tool-loading` is a supported frontmatter property.
4. Whether `tools` is enforced on CLI specifically (docs don't split CLI vs cloud).
5. What happens when a persona exceeds 30,000 chars on each surface.
6. How to scope a `preToolUse` hook to one agent (no `agentName` in its payload).
7. Whether skills declared in a plugin's `plugin.json` are scoped to that plugin's agents.

## 8. Anti-patterns to avoid in this repo

Each is documented by GitHub, and each has bitten us:

- **Conflicting instructions across files.** The top anti-pattern. Example found here: a persona
  described `tools:` as "deferred" while its own frontmatter set it; another told itself to use
  `ask_user` 170 lines after stating that tool does not exist.
- **"MANDATORY" prose with no gate.** Prefer a script, a check, or an exit code.
- **Assuming a subagent inherits anything.** It does not.
- **Over-long prompts.** GitHub's own sample agents are 150–300 words; community guidance is
  500–2,000 chars per agent. Ours are 18k–47k. Length is not free — it competes for attention even
  where it is not truncated.
- **Referencing tools, scripts or paths that do not exist.** Cheap to catch mechanically; worth a
  test.
