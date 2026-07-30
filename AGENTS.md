# AGENTS.md — shared conventions for this repo's Copilot agents

This file is auto-loaded by GitHub Copilot CLI (and other agent runtimes) for every session in this
repository. It has two jobs: (1) tell you (or a fresh contributor) exactly which Copilot **plugins and
MCP servers** this toolkit needs so the repo is self-configuring, and (2) hold the **conventions every
agent inherits**, so the individual `.github/agents/*.agent.md` files stay lean and don't restate them.

> **VS Code users:** VS Code Copilot auto-loads `.github/copilot-instructions.md`, *not* this file.
> That pointer file duplicates only the session-start step below and defers everything else here, so
> the two cannot drift.

---

## Session start, do this first (before any other work)

```
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1 -Update
```

`-Update` repairs the npm bridge CLIs **only when they are below the correctness floor** — it is a
floor check, not a blind `@latest`, so at or above the floor it costs nothing.

**Why a floor:** `powerbi-report-author` **>= 0.1.4** is a *correctness* floor. Older builds returned
`errorCount: 0` for PBIR that Power BI Desktop cannot open (e.g. a `report.json` missing the
schema-required `reportVersionAtImport`) — a stale CLI silently green-lights a broken report.

**Above the known-good matrix is a WARN, not an error.** It means the version-specific Gotchas in
`.github/agents/` were verified against an older build and may be stale — re-verify the prose; never
"fix" it by downgrading.

**The timing rule is what makes this safe:**

| When | Run | Why |
|---|---|---|
| Session start (nothing in flight) | `preflight.ps1 -Update` | Safe; the CLI floor is a correctness floor |
| Migration start (orchestrator step 0) | `preflight.ps1` (plain) | Confirm READY without swapping tooling mid-flow |
| Mid-migration | **never** | Swapping the validator under a half-built report is worse than a slightly old one |

It cannot update the **skill bundles**: `copilot plugin update` hits a file lock while any Copilot
session is running, so plugin updates remain a manual, between-sessions step.

---

## Required Copilot setup (self-configuring dependencies)

This toolkit is not self-contained: its agents build on Microsoft's official **Fabric / Power BI
skills** (published as a Copilot *plugin*) and talk to Power BI through **MCP servers**. A clone needs
all three layers below. The agent files under `.github/agents/` are already committed and load
automatically; `.github/skills/` is an enabled skill location (see `.vscode/settings.json`) that is
currently empty — repo-local skills dropped there load automatically too.

### 1. Skill plugin — `powerbi-authoring@fabric-collection`

The `pbi-report-builder` and `pbi-semantic-builder` agents chain the official
`powerbi-report-planning` / `powerbi-report-design` / `powerbi-report-authoring` /
`semantic-model-authoring` skills, which ship in the **`powerbi-authoring`** plugin from the
**`fabric-collection`** marketplace (GitHub repo [`microsoft/skills-for-fabric`](https://github.com/microsoft/skills-for-fabric)).

Install once, in Copilot CLI:

```
/plugin
```

then add the marketplace `microsoft/skills-for-fabric` and install/enable `powerbi-authoring`
(the broader `fabric-skills` bundle from the same marketplace is optional — it adds cross-workload
Fabric agents like FabricIQ). Equivalent settings, if you edit `~/.copilot/settings.json` by hand:

```jsonc
{
  "extraKnownMarketplaces": {
    "fabric-collection": { "source": { "source": "github", "repo": "microsoft/skills-for-fabric" } }
  },
  "enabledPlugins": {
    "powerbi-authoring@fabric-collection": true
    // optional: "fabric-skills@fabric-collection": true
  }
}
```

> Do **not** use the deprecated `RuiRomano/powerbi-agentic-plugins` marketplace — it is superseded by
> `powerbi-authoring@fabric-collection`.

### 2. MCP servers — see [`.vscode/mcp.json`](.vscode/mcp.json)

`.vscode/mcp.json` (auto-read by VS Code Copilot) declares the two servers the pipeline uses:
- **`powerbi-modeling-mcp`** (stdio, `@microsoft/powerbi-modeling-mcp`) — semantic-model writes.
- **`powerbi-remote`** (http) — read-only schema inspection + DAX `EVALUATE`.

Copilot CLI users register the same servers with `/mcp`, or copy them into
`~/.copilot/mcp-config.json` under an `mcpServers` key.

### 3. Repo-local skills & agents (already committed, zero setup)

- Agents: [`.github/agents/*.agent.md`](.github/agents/) — `tableau-migrator` (orchestrator),
  `pbi-semantic-builder`, `pbi-report-builder`, `pbi-migration-validator`.
- Any repo-specific skills live under `.github/skills/` (already an enabled skill location via
  `.vscode/settings.json`).
- **Visual cookbook: [`.github/pbi.kb/visual-cookbook.md`](.github/pbi.kb/visual-cookbook.md) +
  [`.github/pbi.kb/visuals/`](.github/pbi.kb/visuals/)** — a committed library of worked,
  `validate`-passing PBIR `visual.json` encodings, each with roles, the Tableau idiom it maps to, and a
  confidence tier. **Coverage note:** the `visuals/` folder holds the *harder/rarer* encodings (combo,
  waterfall, ribbon, treemap, small multiples, azureMap, shape…); the common core types
  (`columnChart`/`barChart`/`lineChart`/`cardVisual`/`tableEx`/`slicer`/`textbox`) are proven **in situ**
  and are copied from the migration paths cited in the cookbook's 🟢 table, not from a local file.
  `pbi-report-builder` copies these instead of guessing visual JSON; new ground-truth encodings are
  added back here so every migration compounds.

### 4. Node.js + the Power BI CLIs

Two npm CLIs do real work in the loop and are **not** bundled with the plugin — install them globally
(needs **Node >= 20**):

```
npm install -g @microsoft/powerbi-report-authoring-cli @microsoft/powerbi-desktop-bridge-cli
```

- `powerbi-report-author` — `validate`, `catalog`, `formatting`, `expr`, `theme`, plus
  `preview-visuals` / `preview-pages` / `preview-filters` / `preview-themes` and `doctor`.
- `powerbi-desktop` — the Desktop Bridge: `status`, `manifest`, `open`, `reload`, `screenshot`,
  `screenshot-all`.

**Known-good version matrix** (verified 2026-07-27). These are *unpinned global* installs, so they can
change under you with no repo diff — and several agent Gotchas encode version-specific tool behaviour.
`scripts/preflight.ps1` prints the installed versions and WARNs on drift:

| Component | Known-good |
|---|---|
| `@microsoft/powerbi-report-authoring-cli` | 0.1.4 |
| `@microsoft/powerbi-desktop-bridge-cli` | 0.1.2 |
| `powerbi-authoring@fabric-collection` | 0.3.9 |
| Node.js | >= 20 (26.2.0 tested) |
| Python | >= 3.11 (3.11.9 tested) |

If a version differs, re-verify the version-specific Gotchas in `.github/agents/` before trusting them.

### 5. Python tooling

`uv venv && uv sync --all-extras` — the deterministic parser (`scripts/parse_tableau.py`), harvester,
showcase, and validation scripts (`--all-extras` pulls `tableauhyperapi`/`playwright`/`pillow`, which
several documented scripts import). Lint/format with `ruff`; the parser has a `pytest` regression suite
in `tests/`. **`uv.lock` is deliberately gitignored** — see the note in `.gitignore`.

### 6. Preflight — verify everything above in one command

```
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1
```

The `tableau-migrator` agent runs this first on every invocation. It is a PowerShell bootstrap (not
Python) so it works even before Python is installed — checking FOR Python is one of its jobs. It checks
the parser deps, the `powerbi-authoring` plugin, the MCP servers, Power BI Desktop + Bridge CLI, `npx`,
the .NET SDK, and the CLI version matrix (plus `powerbi-report-author doctor`), printing an install hint
for anything missing (exit 0 = ready). Run it yourself after
cloning to confirm the machine is configured.

---

## Shared agent conventions (all agents inherit these)

- **Cite your source.** Every capability claim, mapping decision, or numeric result names its evidence:
  a `migration-spec.json` field, a TMDL/PBIR path + line, a live `EVALUATE` result, or a doc URL.
  "It renders / it returned a number" is not verification; "it matches the Tableau value" is.
- **Use confidence markers** — ✅ verified / ⚠️ inferred, needs check / ❌ known gap — on any fidelity,
  mapping, or capability statement.
- **Own your layer; don't cross it.** `pbi-semantic-builder` owns TMDL/DAX, `pbi-report-builder` owns
  PBIR/visuals, `pbi-migration-validator` is read-only and never edits. A subagent never "just fixes"
  a finding another agent owns — it reports; the orchestrator routes.
- **Research first, then a human in the loop for uncertain PBIR.** For any visual/encoding whose PBIR
  JSON is undocumented, verify feasibility against Microsoft Learn + the `powerbi-report-author` CLI
  first; if the exact JSON is still unknown, ask the human to build it once in Desktop and reuse the
  resulting `visual.json` as ground truth (see `pbi-report-builder.agent.md`). Do not guess-and-iterate
  blindly — `validate` passes structurally-valid-but-wrong encodings.
- **Structural validation is necessary, not sufficient.** `powerbi-report-author validate` and TMDL
  deserialization pass many defects that only surface in Desktop (field-parameter `sourceColumn`
  brackets, the `'Table'[Col]=[Measure]` PLACEHOLDER error, flat-lined trend measures). Verify in
  Desktop with data before declaring a page done. **Worse: `validate` SILENTLY SKIPS all JSON-schema
  checks when it can't fetch the visualContainer schema** — it prints `PBIR_SCHEMA_UNREACHABLE` and
  still reports "0 errors" even for structurally broken PBIR (the declared `2.11.0` schema 404s; `2.9.0`
  is the newest published). Treat that warning as "schema validation did NOT run" and confirm with a
  Desktop open-test (a schema violation shows an error dialog on open) or an offline `ajv` harness
  against the real 2.9.0-family schemas.
- **Keep `limitations_encountered` alive** through the whole build **and** fix phase; every bug found
  and fixed later is itself worth recording. Regenerate it from the final artifacts before sign-off so
  stale entries don't mislead the validator.
- **Surface complexity mismatches proactively.** If the parsed workbook implies more effort than the
  user assumes (many LOD/table-calc fields, extract-only data with no upstream, >20 floating-layout
  worksheets), say so before building rather than discovering it mid-migration.
- **NEVER block silently on an external system — time-box it, then ASK.** This is a hard rule, from a
  real user report: an agent sat on "Testing live Snowflake connectivity" for **129 minutes / 298 tool
  calls**, retrying without ever surfacing the problem, until the user intervened and suggested taking
  the credential from Power BI Desktop. Waiting is not progress, and a credential is something only a
  human can supply — no number of retries will conjure one.
  - **Cap it: ~2 minutes or 3 attempts, whichever comes first**, for any connect / refresh / sign-in /
    connectivity test against a database, warehouse, gateway or tenant.
  - On hitting the cap, **STOP and ask the user a specific, actionable question** — name the system,
    the server, what you tried, and the concrete options (e.g. "sign in interactively in Desktop", or
    "give me a PAT/key"). Never re-run the same call hoping for a different result.
  - **Report elapsed time in your progress updates** whenever an operation exceeds ~60s, so a stall is
    visible rather than looking like work.
  - If a credential is already cached in **Power BI Desktop**, prefer that path — it is usually the
    fastest unblock, and `scripts/probe_desktop_query.py` tells you definitively whether it worked.
  - The same cap applies to any tool call that has hung once: the second identical retry needs a
    reason, and the third needs the user.
- **End every message with a clear next step or an explicit verdict** — never a vague "looks fine."
- **Durable learnings go in committed files** (the agent `Gotchas` sections and
  `docs/tableau-dax-translation-guide.md`), never in a git-ignored scratch folder — that is how each
  real migration permanently improves the toolkit.
- **Clean up after yourself when you finish.** (a) **Close any Power BI Desktop instance you opened.**
  In a parallel batch, orphaned Desktop instances (+ their child `msmdsrv`) cause Desktop-bridge
  contention that blocks later agents from opening/rendering — a real bottleneck. Close the instance
  you pinned your screenshots to: `Stop-Process -Id <your literal pid> -Force` (map instance→migration
  by `MainWindowTitle`; note the shell guard rejects looped/variable `-Id`, and `$pid` is a read-only
  automatic variable, so use literal PIDs). **Never** close a sibling's instance, and don't close one
  mid-handoff that a peer still needs (e.g. a validator awaiting a semantic-builder's fix). (b) **Remove
  scratch/temp files you created** (ajv harnesses in `%TEMP%`, `.pbip` cache/backups, one-off probe
  scripts) — keep only committed deliverables plus the re-runnable `_build/` scripts; confirm nothing
  scratch leaked into git before reporting done.
