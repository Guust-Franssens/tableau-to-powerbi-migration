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
| `disable-model-invocation` | `true` = must be selected explicitly, never auto-delegated. **Not used here any more** — it was set on `tableau-migrator` and removed 2026-08-01, because it also blocks *deliberate* programmatic invocation: the agent vanished from the `task` tool's list, so no agent could orchestrate a migration |
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
`.agent.md` and assume it works.

Read the quote precisely, because it is easy to over-read: what a subagent does not inherit is the
**preload** — no skill *body* is ever injected into a subagent. The very next sentence, *"skill names
are resolved from the session-level `skillDirectories`"*, is the part that does hold: §6.1 measures a
subagent invoking both a plugin skill and a **repo-local** `.github/skills/` skill **by name**, and
getting the real body back. So "sub-agents do not inherit skills" means *not preloaded*, **not**
*unreachable*. Treat `skills:` itself as: *documented for SDK-defined agents, unverified for
`.agent.md`* — but treat invoke-by-name as **working and measured**.

VS Code-only (ignored elsewhere): `argument-hint`, `agents`, `handoffs`, `hooks`.

**Prompt cap: 30,000 characters** for the markdown body. Measured here: Copilot CLI does **not**
enforce it (a 48k persona quoted its own final section back verbatim). The failure mode on the
GitHub-hosted path — truncate or reject — is **untested**. As of 2026-08-01 all four personas fit
(~98–99%, §5) and `python scripts/sync_agent_conventions.py --check` **fails on an overage** rather
than warning about it. That gate was advisory while personas sat at 108–160%, where a hard failure
would have blocked every commit; once they all fit, the only thing left to catch is a regression, and
this repo's own rule is that a mandate without an exit code behind it is an anti-pattern (§3).
`--allow-over-cap` exists for a deliberate, temporary overage.

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
| `.github/skills/pbip-model-refresh/` (SKILL.md + `scripts/` + `tests/`) | Repo-local **skill bundle**, also published as a plugin | **Yes, two ways.** §6.1 measured a subagent invoking a repo-local skill **by name** and getting the body back, so `use the pbip-model-refresh skill` works; reading `.github/skills/pbip-model-refresh/SKILL.md` by path also works. Either is discretionary — the agent must choose to do it |
| `.github/skills/powerbi-ai-readiness/` (SKILL.md + `scripts/` + `tests/`) | Repo-local **skill bundle**, also published as a plugin | Same. It absorbed `docs/ai-instructions-authoring-guide.md` (now a stub) and that persona's ~7.3 KB "Prep the model for AI" section — two copies of one recipe that had already diverged (§8), now one |
| `.github/skills/powerbi-report-gotchas/` (SKILL.md only) | Repo-local **skill bundle**, also published | Same. Holds `pbi-report-builder`'s former inline Gotchas; the persona invokes it at step 0, gates on it in its DoD, and keeps a section index |
| `.github/skills/powerbi-semantic-model-gotchas/` (SKILL.md only) | Repo-local **skill bundle**, also published | Same, for `pbi-semantic-builder` |
| Per-agent Gotchas | `tableau-migrator` and `pbi-migration-validator` keep theirs inline; the two builders' moved into the bundles above (2026-07-31) | Inline = yes; bundled = only if invoked |

An **explicit instruction to read a file** is a normal tool call and does reach a subagent — this is
different from passive inheritance, and the repo already depends on it for ~44 KB. The documented
anti-pattern ("requests to refer to external resources") is about *unresolved ambient conformance*
("conform to styleguide.md"), not an imperative, verifiable step. But it is still **discretionary**:
an agent may skip it.

**Why package procedural knowledge as a skill.** Four reasons — and §6.1 has now resolved *in favour*
of the strongest one, that the name resolves inside a subagent:

1. **It is strictly no worse than the status quo, and now measurably better.** A skill file is still a
   readable path, so the floor is today's advisory "read this file" behaviour. Since §6.1, the ceiling
   is higher than that floor: a subagent can invoke the bundle **by name**, which is shorter to write
   in a persona and cheaper in persona budget than a path. There is no downside branch: a bundle is
   never worse than the paragraph it replaced.
2. **It reclaims persona budget.** This is no longer theoretical: extracting the two gotcha catalogues
   took all four personas under the 30,000-char cap (§5) for the first time. Procedure that is not
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

**How a persona must reference a bundle.** §6.1 reversed the earlier answer here. A subagent **does**
carry an `<available_skills>` registry and **can** invoke a repo-local skill by name (proven with
`sentinel-probe`, which exists in no plugin). So a persona may say **"use the `<name>` skill"** — with
two conditions:

1. **The persona must not declare a `tools:` allow-list that omits `skill`.** That is what actually
   broke `pbi-migration-validator`, and it fails silently (§6, experiment 3).
2. **Prefer the name; keep the path as the fallback.** `read .github/skills/<name>/SKILL.md` is an
   ordinary `view` call that works in every configuration measured, including agents with a restricted
   tool set. Naming *and* pathing the bundle in one sentence costs a few characters and removes the
   failure mode entirely — which is what the personas now do.

Publishing a bundle as a plugin remains worthwhile (it is how *other* repos get it), but it is no
longer a prerequisite for the name to resolve here. Note the trade-off it introduces: the plugin copy
**shadows** the repo copy, so a published bundle must be kept in sync — `scripts/preflight.ps1` hashes
each pair and fails on drift.

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

**That rule blocked the obvious next trim — until 2026-07-31, when the trim was done anyway. Here is
why that is not a violation.** The `Gotchas` sections were ~36% of `pbi-report-builder` (16,788 chars)
and ~35% of `pbi-semantic-builder`, making them the fattest targets, and the rule above said to leave
them alone: a gotcha the agent stops seeing is a defect it silently repeats. Two things changed:

1. **Inline was no longer reliable delivery.** At 153% of the cap, those sections *were already* the
   part a hosted run truncates first (§2). "Absence is silent and harmful" was the argument for
   keeping them inline, but at 153% inline delivery had itself become a silent-absence risk. The
   choice was not "reliable inline vs discretionary skill" — it was two discretionary options.
2. **A skill is not `.github/pbi.kb/`.** §6.1 measured that a subagent *can* invoke a repo-local skill
   by name and get the body back. That is a real mechanism with a real tool call, not a hope that the
   agent reads a reference folder.

So the content moved into `powerbi-report-gotchas` and `powerbi-semantic-model-gotchas`, with three
mitigations that are the actual price of the move:

- the persona invokes the bundle as **step 0** of its skill chain,
- it is a **Definition of Done item** ("the skill was read this session"),
- the persona keeps a **section index** — a table of what each `§` covers — so the agent can *see
  what it is missing* and know when it needs it. Without the index this would be a plain violation.

⚠️ **Residual risk, stated plainly:** an agent that skips the invocation still loses the knowledge, and
nothing mechanically fails. The index and the DoD item make that *visible*, not *impossible*. If a
future migration shows an agent repeating a gotcha it never read, the mitigation was insufficient —
record it here rather than quietly reverting.

**The rule itself is unchanged** for everything else: anything whose absence is silent and harmful
stays inline unless it moves somewhere the agent can *actively reach and be told to reach*. A hook
still does not qualify.

Budget after the 2026-07-31 extraction — **all four now fit** (`sync_agent_conventions.py --check`
exits 0):

| Persona | chars | % of 30k cap | was |
|---|---|---|---|
| `tableau-migrator` | 29,992 | 99% | 108% |
| `pbi-report-builder` | 29,953 | 99% | 153% |
| `pbi-semantic-builder` | 29,728 | 99% | 141% (peak 160%) |
| `pbi-migration-validator` | 17,677 | 58% | 58% |

There is now **no headroom**: at ~98–99% a single appended gotcha puts a persona back over, and
`sync_agent_conventions.py --check` now **fails** on that rather than warning (§2). That is
deliberate — new craft learnings belong in the bundles (orchestrator step 12 routes them there), not
back in a persona.

## 6. Experiments

1. **Do repository skills reach a subagent? — ✅ RESOLVED 2026-07-31: YES, and they are invocable.**

   ⚠️ **This reverses an earlier conclusion in this same file.** On 2026-07-30 a probe concluded
   "repo-local skills are root-only; `use the <x> skill` fails outright inside a subagent", and that
   claim was shipped as fact (#36) and written into `pbi-semantic-builder`. It is **wrong** on the
   current CLI (1.0.77). Re-measured with a persona that declares no `tools:` allow-list:

   | Probe (from `pbi-report-builder`, no `tools:` allow-list) | Result |
   |---|---|
   | Does a `skill` tool exist? | **YES** — 22 loaded tools incl. `skill`, plus 204 deferred MCP tools |
   | `<available_skills>` registry | **PRESENT** — 92 entries (26 with description, 66 bare name) |
   | `skill("powerbi-report-authoring")` (plugin) | **SUCCESS**, base dir under `.copilot\installed-plugins\fabric-collection\powerbi-authoring\` |
   | `skill("powerbi-ai-readiness")` (plugin ∪ repo) | **SUCCESS**, base dir under `.copilot\installed-plugins\powerbi-migration-collection\` |
   | `skill("sentinel-probe")` (**repo-local only**) | **SUCCESS**, base dir `…\tableau-to-pbi-migration\.github\skills\sentinel-probe` |
   | Its `SKILL_SENTINEL_ZEPHYR_74193` token | **QUOTED**, arriving only via the `skill` tool |

   `sentinel-probe` is the load-bearing case: it exists in **no** plugin, so a correct quote of its
   token can only have come from `.github/skills/`. The probing agent attested it never opened the
   file by `view`/`grep`/shell, and its one prior filesystem touch was a `Get-ChildItem -Directory`
   that returned names only. Confirmed on disk afterwards: `sentinel-probe` appears **nowhere** under
   `~/.copilot` (no plugin copy, no user-global copy), and the installed `powerbi-migration-skills`
   plugin does not ship it — deliberately, and a test enforces that
   (`test_the_diagnostic_probe_skill_is_never_published`).

   **How the repo-local directory gets registered:** `.vscode/settings.json` (committed) sets
   `chat.agentSkillsLocations` with `".github/skills": true`. So the behaviour travels with a clone —
   but a repo that drops that file, or a runtime that ignores it, is the one configuration where
   repo-local skills might not resolve. That is the reason personas name the bundle **and** give its
   path: the path is an ordinary `view` call and never depends on registration.

   Two things remain true from the old finding, and one is now explained:
   - **No skill body is ever *preloaded*** into a subagent — 26 names arrived with a description, 66
     as bare names. Invocation is what fetches the body. This matches the SDK wording exactly (§2):
     not inherited ≠ not reachable.
   - **The earlier "no `skill` tool" observation was real but misattributed.** It was measured on
     `pbi-migration-validator`, the one persona that **declares a `tools:` allow-list** — see
     experiment 3. The allow-list, not the subagent boundary, is what removed the tool. The old note
     claimed "no persona declares `tools:`, so this is the runtime"; that statement was simply false
     about this repo.

   **Consequence for persona authoring.** `use the <x> skill` **does** work in a subagent, for plugin
   and repo-local skills alike — provided the persona does not declare a `tools:` allow-list that
   omits `skill`. Reading `.github/skills/<x>/SKILL.md` by path also still works and is the more
   conservative option; prefer invoke-by-name for brevity, path-read when the exact file matters.

   ⚠️ **Publishing introduces shadowing — but NOT duplicate registry entries.** Where a name exists
   both in `.github/skills/` and in an installed plugin, the registry lists it **once** and the
   **plugin copy wins**. Measured twice: `powerbi-ai-readiness` resolved to
   `.copilot\installed-plugins\` with the cwd inside this repo and the repo copy present; and after
   v0.2.0 made all four bundles exist in both places, a subagent counted **exactly one** entry per name
   (`powerbi-ai-readiness = 1`, `pbip-model-refresh = 1`, `powerbi-report-gotchas = 1`,
   `powerbi-semantic-model-gotchas = 1`). The registry total moved **92 → 94**, i.e. **+2 for the two
   new bundles, not +4** — independent corroboration that de-duplication is by name, not a coincidence
   of counting. Entries carry a `<location>plugin</location>`-style marker, so the winning source is
   visible in the registry itself.

   So there is no "installed twice" hazard and no ambiguity about which copy runs. The real hazard is
   the opposite one: because the plugin is a downstream *snapshot*, a repo-side edit that is never
   re-published is served stale and **silently** — the single registry entry points at the plugin,
   which is exactly the copy you did not edit. All four pairs hash identical today
   (`preflight.ps1`: `4 bundle(s) in sync`); that script hashes every shipped bundle and fails on
   **either** shape: `STALE in plugin` (edited but not published) or `NOT INSTALLED` (published but
   not re-installed).

   **Publishing works — proven, with the plugin copy isolated (2026-07-31):**

   ```
   copilot plugin marketplace add Guust-Franssens/powerbi-migration-skills
   copilot plugin install powerbi-migration-skills@powerbi-migration-collection
   → Plugin "powerbi-migration-skills" installed successfully. Installed 2 skills.
   ```

   (That output is from v0.1.0. v0.2.0 ships **four** — the two gotcha catalogues were added when they
   were extracted from the personas. Note `copilot plugin install` fails with `Access is denied`
   while a Copilot session is running, so installs are a **between-sessions** step.)

   **The full loop is proven repeatable, not a one-off (2026-08-01).** After v0.2.0 was published and
   installed between sessions, a `pbi-report-builder` subagent in a fresh process invoked **both** new
   bundles by name and got `SUCCESS`, each resolving to the plugin:

   ```
   Base directory for this skill: C:\Users\<user>\.copilot\installed-plugins\
       powerbi-migration-collection\powerbi-migration-skills\skills\powerbi-report-gotchas
   ```

   So `build_plugin.py` → marketplace repo → `plugin install` → invoke-by-name in a subagent works
   end to end, for a bundle that ships **no scripts at all**. Note the probe ran with the cwd *inside*
   this repo and still resolved to the plugin — shadowing again, now with all four pairs hash-identical
   (`preflight.ps1`: `4 bundle(s) in sync`), so nothing is lost either way.

   Run from a directory **outside this repo**, so no `.github/skills/` copy can shadow it, a fresh
   session lists both and `skill(powerbi-ai-readiness)` succeeds, loading from:

   ```
   C:\Users\<user>\.copilot\installed-plugins\powerbi-migration-collection\
       powerbi-migration-skills\skills\powerbi-ai-readiness
   ```

   Built by `scripts/build_plugin.py`; see [`AGENTS.md`](../AGENTS.md) §1b for why it publishes to a
   separate repo *and* why it is published at all, given repo-local skills already resolve here.

   ⚠️ **Four traps when testing this.** Each produced a convincing false result here:

   1. **Skills — and agent definitions — are snapshotted at session start.** A plugin installed
      mid-session is invisible to that session *and* its subagents — indistinguishable from a broken
      plugin. A subagent probed this way even concluded "the defect is specific to this plugin"; it was
      not. The same applies to `.agent.md` frontmatter (see experiment 3: an edited `tools:` list had
      no effect until a fresh process). **Restart before judging.**
   2. **Testing from inside this repo proves nothing about the *plugin*.** `.github/skills/` supplies
      the same two names. It does not shadow the plugin (the plugin wins), but a green
      `skill(powerbi-ai-readiness)` here cannot distinguish the two copies while they are identical —
      only the printed base directory can. Always read the base directory, or isolate from an
      unrelated directory.
   3. **A `tools:` allow-list silently strips the `skill` tool** — and with it the entire
      `<available_skills>` registry (the validator reported `NO SKILL REGISTRY BLOCK`). If a probe
      says "no skill tool", check the persona's frontmatter *before* blaming the runtime. That is the
      exact error this document made.
   4. **Built-in lightweight agents are not representative.** `explore` has a curated read-only set
      (`powershell`, `view`, `rg`, `glob`, 4 github-mcp tools) with **no `skill` tool**. Measuring
      `explore` and generalising to custom personas is invalid — they differ by 22 vs 11 tools.

2. **Does `subagentStart` fire and inject? — ✅ RESOLVED 2026-07-31: YES, both halves.**
   `.github/hooks/subagent-context.json` + `scripts/hooks/probe_subagent_start.ps1` log the payload to
   `_hook_probe.log` and inject `HOOK_SENTINEL_ORBIT_58231`. In a session started *after* the hook file
   existed, invoking `pbi-migration-validator` produced **both**: the log line, and the subagent
   quoting its first user-turn line verbatim as

   > `HOOK_SENTINEL_ORBIT_58231 - injected by subagentStart at 2026-07-31T16:13:45.5445600+02:00.`

   — matching the log timestamp `16:13:45.5372852` to the same second. So `additionalContext` from a
   `subagentStart` hook **does** reach a subagent's context, and the `matcher` field correctly scoped
   it to one agent name. The earlier "no log, no sentinel" result was a session that predated the hook
   file, i.e. "not loaded", exactly as suspected.

   Payload shape, measured (no `agentType`, no prompt text):

   ```json
   {"sessionId":"…","timestamp":1785507224746,"cwd":"…","transcriptPath":"…\\events.jsonl",
    "agentName":"pbi-migration-validator","agentDisplayName":"pbi-migration-validator",
    "agentDescription":"Read-only reviewer that critiques…"}
   ```

   **This does not change the §5 rule.** A hook can carry supplementary context, but it can still be
   disabled wholesale via `disableAllHooks`, so nothing whose absence is silent and harmful may move
   into it. Useful for *advisory* context; never for load-bearing rules.

3. **Is the `tools` allow-list enforced on the current CLI? — ✅ RESOLVED 2026-07-31: YES. This changed.**
   Measured 2026-07-30: **not** enforced (the validator came back still holding `edit`, `create`,
   `task`). Measured 2026-07-31 on CLI **1.0.77**: **enforced**. `pbi-migration-validator` declared

   ```yaml
   tools: ["read", "search", "execute", "web", "tool_search_tool", "powerbi-modeling-mcp/*", "powerbi-remote/*"]
   ```

   and received exactly 14 tools: `view`, the four `powershell` tools, and the nine `powerbi-remote-*`
   tools. Compare `pbi-report-builder`, which declares **no** `tools:` and received 22.

   ⚠️ **Enforcement is partial, and the misses are silent.** Of the declared entries, only `read`
   (→ `view`), `execute` (→ the `powershell` family) and `powerbi-remote/*` produced tools.
   **`search`, `web` and `tool_search_tool` produced nothing** — no search tool, no `web_fetch`, no
   `web_search`, and no `skill` (never declared). An unrecognised entry is dropped without warning, so
   an allow-list can quietly cost an agent capability its own persona tells it to use.

   Rewriting the list with **literal tool names** fixes it. Verified inventory afterwards: `skill`,
   `glob`, `web_fetch`, `web_search` **PRESENT**; `edit`, `create`, `task` **ABSENT** — so least
   privilege is real *and* the agent can reach its skills. Two entries still never resolve:
   `tool_search_tool` (in any form), and `grep` — the search tool is exposed under the name **`rg`**,
   so declare both. A dropped entry is harmless, so listing an alias costs nothing.

   ⚠️ **Trap: agent definitions are snapshotted at session start, exactly like skills.** Editing a
   persona's frontmatter mid-session changes nothing — a re-probe returned the *identical* 14-tool
   inventory and looked like "literal names don't work either". The corrected list only took effect in
   a **fresh CLI process** (`copilot --allow-all -p "…"` delegating to the subagent). Always verify a
   frontmatter change in a new process, or you will measure the old definition and draw the wrong
   conclusion. **Declare a `tools:` list only if you verify the resulting inventory that way**; when in
   doubt, omit it and enforce read-only behaviour in prose.

4. **Does a persona's "STOP and ask" rule actually hold? — ⚠️ NOT IN ROOT `-p` MODE (2026-08-01).**
   The most important behavioural finding here, because the rule it breaks is the one born from a real
   129-minute stall (§ shared conventions, "NEVER block silently — time-box, then ASK").

   Controlled test: a mocked Tableau workbook with two **live Azure Databricks** sources plus one flat
   file, a real warehouse + real tables, and **no** Power BI credential cached anywhere. Identical
   clean tree, identical prompt (*"Please migrate this Tableau workbook…"*), same persona:

   | Invocation | Result |
   |---|---|
   | **Subagent** (via the `task` tool) | ✅ **Paused in 83s.** Named both credential gates, asked three specific questions (target workspace / configure creds *or* authorize build-only / reference URL), built **nothing** |
   | **Root agent** (`copilot --agent=… -p`) | ❌ Ran the same preflight, acknowledged both gates, then said *"proceed with a structural migration"* and *"handing the semantic layer to its owning builder"* — no question asked |

   An earlier root run (before the tree was cleaned) went all the way to a complete semantic model
   **and** a 12-visual report, surfacing the blocker only in its closing summary.

   **The confound was ruled out.** The first root run also had a fixture-setup script sitting in the
   migration folder, which it read — it leaked the warehouse id, catalog, schema and ground-truth
   totals. Re-running root mode on a tree with that file removed produced the *same* push-through, so
   **invocation mode is the variable, not the leak.**

   Likely mechanism: under `-p` there is no one to answer, and `continueOnAutoMode` biases toward
   finishing; "build it structurally and report the blocker" reads as the helpful move. It is not — it
   spends capacity on a model whose grain and types cannot be verified against the real source, and it
   takes a decision that belongs to the user.

   **Consequences, both applied:**
   - Step 5 of `tableau-migrator` is now an explicit **HARD STOP** that says the stop is
     *unconditional in a non-interactive run*, that having no one to answer is **not** authorization,
     and that build-only is the user's call. A rule that only says "STOP and ask" is not enough — it
     must pre-empt the rationalization the model actually reaches for.
   - **Test personas as a subagent AND as a root agent.** They are different behavioural regimes, and
     a convention verified in one can silently fail in the other. Every stop/ask rule in this repo was
     previously only ever exercised as a subagent.

## 7. Things the docs genuinely do not say

Recorded so nobody re-derives them or fills the gap with a plausible guess:

1. ~~Whether a repo-local skill, once *named* in a persona, can still be **invoked** from inside a
   subagent.~~ **Answered 2026-07-31 — YES.** See §6.1: a subagent invoked `sentinel-probe` (which
   exists only in `.github/skills/`) and quoted its sentinel token. An earlier entry here said "no";
   that was measured on the one persona carrying a `tools:` allow-list that omits `skill`.
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
