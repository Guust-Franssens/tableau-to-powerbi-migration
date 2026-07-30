# Agent architecture — what reaches an agent, and how to change it

Why this file exists: this repo's four agents grew organically, and several conventions were written
on assumptions that turned out to be wrong. This is the researched, cited baseline — what GitHub
actually documents, what was measured here, and what is still unknown. Read it before changing
anything under `.github/agents/`.

Last verified **2026-07-30** against docs.github.com. This area moves fast; re-check before relying
on a claim.

---

## 1. The core constraint: a subagent sees only its own persona

A custom agent invoked **as a subagent** (via the Task/agent tool) receives:

- its own `.agent.md` prompt body, and
- the task text the orchestrator passes it.

It does **not** receive `AGENTS.md`, `.github/copilot-instructions.md`, user-global instructions, or
the parent's conversation. Skills are not inherited from the parent either.

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

**There is no `skills` property.** Checked against the GitHub reference, the VS Code reference, the
plugin manifest schema and the agentskills.io spec. If you see that claim, ask for a doc URL.

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

That makes it the documented mechanism for **per-agent progressive disclosure** — the thing the
missing `skills` property would have provided. Crucially, injected context counts against the
**context window**, not the 30,000-char persona cap.

Notes worth knowing before building on it:
- The built-in `general-purpose` agent does **not** emit `subagentStart`/`subagentStop`. Custom
  agents do.
- It can inject but **cannot block** subagent creation.
- ~10 KB cap documented for the *combined* `additionalContext` of multiple hooks; the single-hook cap
  is undocumented.
- `preToolUse` payload has **no `agentName`** — so a global `preToolUse` hook cannot cheaply say
  "block edits *only* for the validator".

⚠️ **Untested here.** A probe hook in `.github/hooks/subagent-context.json` did not fire when a
subagent was invoked in a session that started **before** the file existed — no log line was written.
That is consistent with hooks being loaded at session start, like instruction files and skills. See
§6 for the experiment that settles it.

## 5. What currently lives where

| Content | Where it is | Reaches a subagent? |
|---|---|---|
| Shared conventions | Generated into all four personas by `scripts/sync_agent_conventions.py`, CI-gated for drift | Yes — because it is physically in each persona |
| `docs/tableau-dax-translation-guide.md` (24k) | External; persona says "Read … before starting" | Only if the agent actually reads it — advisory |
| `docs/migration-spec.md` (9k) | External; same instruction | Same |
| `.github/pbi.kb/visual-cookbook.md` (10k) | External; referenced at point of use | Same |
| Per-agent Gotchas | Inline in each persona | Yes |

An **explicit instruction to read a file** is a normal tool call and does reach a subagent — this is
different from passive inheritance, and the repo already depends on it for ~44 KB. The documented
anti-pattern ("requests to refer to external resources") is about *unresolved ambient conformance*
("conform to styleguide.md"), not an imperative, verifiable step. But it is still **discretionary**:
an agent may skip it.

## 6. Open experiments — run these in a FRESH session

Both probes below were inconclusive only because this session predated the files. Hooks, skills and
instruction files all appear to be snapshotted at session start, so **restart the CLI first**.

1. **Do repository skills reach a subagent?**
   `.github/skills/sentinel-probe/SKILL.md` contains `SKILL_SENTINEL_ZEPHYR_74193`.
   Start a fresh session → confirm the skill is listed → invoke a custom subagent → ask it to quote
   the token *without* telling it the value.
   - Quotes it → repo skills reach subagents; skills become the clean home for reference material.
   - Cannot → skills are root-only; hook injection is required.
   - Only after being told "use the sentinel-probe skill" → available but not auto-triggered; record
     that as a third distinct outcome.
   Note: a subagent probed here *did* see plugin-provided skills while not seeing this repo-local
   one, so the discriminator may be *registration*, not the subagent boundary.

2. **Does `subagentStart` fire and inject?**
   `.github/hooks/subagent-context.json` + `scripts/hooks/probe_subagent_start.ps1` log the payload
   to `_hook_probe.log` and inject `HOOK_SENTINEL_ORBIT_58231`.
   Fresh session → invoke `pbi-migration-validator` → check both.
   - Log written **and** sentinel visible → the mechanism works; migrate the shared block to a hook
     and reclaim ~6 KB × 4 of persona budget.
   - Log written, sentinel absent → hook fires but the output field/shape is wrong; fix the script.
   - No log → hook is not being loaded at all; check location and precedence before designing around
     it.

3. **Is the `tools` allow-list enforced on the current CLI?** Re-run the validator probe: ask whether
   it holds `edit`/`create`/`task`. Last measured 2026-07-30: not enforced.

## 7. Things the docs genuinely do not say

Recorded so nobody re-derives them or fills the gap with a plausible guess:

1. Whether repository skills are visible inside a custom-agent subagent.
2. The explicit output schema for `subagentStart` (the `additionalContext` field name is inferred
   from sibling hooks).
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
