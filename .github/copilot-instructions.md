# Copilot instructions, tableau-to-pbi-migration

> This file exists so agent runtimes that auto-load **`.github/copilot-instructions.md`** (VS Code
> Copilot) pick up the same conventions as runtimes that auto-load **`/AGENTS.md`** (Copilot CLI).
> **`/AGENTS.md` at the repo root is the source of truth — read it.** Only the session-start step is
> duplicated here, because it has to fire before anything else.

## Session start, do this first (before any other work)

```
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1 -Update -CheckUpstream
```

Repairs the npm bridge CLIs **if they are below the correctness floor**, then prints the environment
readiness matrix. Session start is the **only safe moment** to change them.

- `powerbi-report-author` **>= 0.1.4 is a correctness floor**, not a nicety. Older builds returned
  `errorCount: 0` for PBIR that Power BI Desktop cannot open (e.g. a `report.json` whose
  `themeCollection` entries are missing `reportVersionAtImport` — it is **required inside each
  `themeCollection` entry** and **forbidden at the top level**, where 0.1.4 rejects it as an
  additional property). A stale CLI silently green-lights a broken report.
- `-Update` is a **floor check, not a blind `@latest`**. At or above the floor it does nothing (so it
  costs no time); it only upgrades an install that is genuinely too old.
- Being **above** the recorded known-good matrix is not an error — it raises a WARN, because that is
  exactly when the version-specific Gotchas in `.github/agents/` need re-verification. Don't "fix" it
  by downgrading; re-verify the prose.
- **Mid-migration, don't upgrade the installed tooling.** The risk is not the calendar but *work
  already validated by the current CLI* — swap the validator underneath it and earlier results are no
  longer covered by the same check. Between migrations is fine. A version-*comparison* run into a
  fresh output dir is not an upgrade and is always safe — see the engine timing rule in
  [`/AGENTS.md`](../AGENTS.md).
- It cannot update the **skill bundles** — `copilot plugin update` hits a file lock while any Copilot
  session is running. That lock only blocks renaming the plugin directory, not writing inside it, so a
  *content* refresh needs no restart: `python scripts/sync_installed_skills.py`. It publishes what is
  **merged** (`origin/master`), not your worktree, so an unmerged skill edit on your branch does not
  fail preflight — see [`/AGENTS.md`](../AGENTS.md) and issue #410.
- It **blocks** if the deterministic conversion engine is installed more than once. The installed
  `tableau-fabric-skills@tableau-collection` plugin is its **single canonical source**; a sibling
  clone or any other checkout is a `MISS`, not a warning, because two engine versions silently built
  one pipeline (issue #107). Delete the extra copy — after confirming it has no uncommitted or
  unpushed work. `-CheckUpstream` additionally reports when the plugin is behind upstream `main`.

### When to run which

| When | Run | Why |
|---|---|---|
| Session start (nothing in flight) | `preflight.ps1 -Update -CheckUpstream` | Safe; the CLI floor is a correctness floor, and this is the one moment upgrading is allowed |
| Migration start (orchestrator step 0) | `preflight.ps1` (plain) | Confirm READY without changing tooling mid-flow |
| Mid-migration | don't re-arm `-Update` | Swapping the validator mid-build leaves earlier-validated work uncovered by the same check; a comparison run into a fresh dir is a separate, always-safe operation (see [`/AGENTS.md`](../AGENTS.md)) |

## Everything else

See [`/AGENTS.md`](../AGENTS.md) for the required plugin/MCP servers, the `migration-spec.json`
contract, and the conventions every agent inherits (cite your source, confidence markers, own your
layer, structural validation is necessary but not sufficient, keep `limitations_encountered` alive).
Per-agent instructions live in [`.github/agents/`](agents/), and PBIR knowledge in
[`.github/pbi.kb/`](pbi.kb/).
