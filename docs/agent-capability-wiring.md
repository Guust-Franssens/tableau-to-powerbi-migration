# Agent capability wiring registry

This registry is the durable prompt to wire shipped capabilities into agent-reachable guidance. Skills remain the authoritative documentation home: a capability belongs in a skill when it is **how** to do the work. A persona carries **when** to do it, what verdict is required, and when to route to the skill. This registry is for cases where an agent-facing instruction would otherwise omit or contradict a capability the agent needs at that moment. When that happens, add a row here with the load-bearing token, the owning agent, and suggested wording for the human fixing it, then run `python scripts/check_agent_capabilities.py`.

The gate does **not** count this registry as wiring. It scans only agent-reachable files, strips fenced code blocks and HTML comments, and requires the token below in visible prose in the named file. The suggested wording is guidance for failures, not a verbatim string to match.

## Capability registry

| Token | Why it exists | Agent that needs it | Reachable in | Suggested agent-facing wording |
|---|---|---|---|---|
| `pbip-model-refresh skill` | The refresh mechanics, flags such as `--calculate-only`, and pid-binding rule live in the skill; the semantic-builder persona must route refresh handoff there instead of carrying a stale command copy. | `pbi-semantic-builder` | `.github/agents/pbi-semantic-builder.agent.md` | Use the pbip-model-refresh skill for the command, flags, pid-binding rule and save mechanics; then require `REFRESH: DATA_OK + PERSISTED`. |
| `NOT_CHECKED` | `check_unit` now has a third outcome; missing inputs previously produced false green runs, including PASS on an empty model directory. | `pbi-migration-validator` | `.github/agents/pbi-migration-validator.agent.md` | `NOT_CHECKED` is not a pass: in `SUMMARY`, `not_checked_structural` means no artifact can exist for that scoped check, while `not_checked_missing_input` means this run lacked an expected input and you may be pointed at the wrong target. |
| `BROWNFIELD DISCOVERY` | Customer migrations may already exist in non-canonical folders; the orchestrator must route discovered artifacts instead of restarting work. | `tableau-migrator` | `.github/agents/tableau-migrator.agent.md` | When `check_unit` prints `BROWNFIELD DISCOVERY`, treat it as read-only artifact discovery: it found engine output by content, not path, and the expected/found-instead block is the path forward before redoing work. |
| `--scope` | Layer owners need scoped `check_unit` gates without mistaking a scoped pass for full sign-off. | all personas | `docs/INDEX.md` | first status and final gate; direct `check_*.py` gates only isolate one finding. |
| `read_handover` | Builders need the compact residual-work queue instead of re-reading large engine handover JSON by hand. | model and report builders | `docs/INDEX.md` | `python scripts/read_handover.py <bundle> --workbook <name>` — residual work queue and engine-block reason. |
| `desktop-orphans` | Run-owned Power BI Desktop processes must be cleaned up without killing sibling work. | all personas | `AGENTS.md` | leaks are enforced by `check_unit.py`'s `desktop-orphans` gate. |
| `reprobe_blocked.py` | After a machine-wide sign-in an agent has no cheap way to resume: it cannot `authorize` (agent-hostile by design), and re-probing dozens of gated units by hand is the friction that invites a bypass. This is the agent-SAFE resume path — probe-only, never authorizes, so it converts unearned→earned by measurement without any human-only step. | `tableau-migrator` | `docs/INDEX.md` | After a machine-wide sign-in, resume with `python scripts/reprobe_blocked.py` (dry-run; `--apply` to run) — re-probes every BLOCKED unit and earns each gate's own clear; it never authorizes. |

## Follow-up: generated flag inventory

This curated list is intentionally small and will lag the full script surface. A future PR should enumerate `argparse` flags from `scripts/`, then require each shipped flag to be either agent-reachable or explicitly marked internal with a reason, using the same reasoned-exclusion shape as `docs/INDEX.md`.
