# AGENTS.md — shared conventions for this repo's Copilot agents

This file is auto-loaded by GitHub Copilot CLI (and other agent runtimes) for every session in this
repository. It has two jobs: (1) tell you (or a fresh contributor) exactly which Copilot **plugins and
MCP servers** this toolkit needs so the repo is self-configuring, and (2) hold the **conventions every
agent inherits**, so the individual `.github/agents/*.agent.md` files stay lean and don't restate them.

---

## Required Copilot setup (self-configuring dependencies)

This toolkit is not self-contained: its agents build on Microsoft's official **Fabric / Power BI
skills** (published as a Copilot *plugin*) and talk to Power BI through **MCP servers**. A clone needs
all three layers below. The agent files under `.github/agents/` and the repo-local skills under
`.github/skills/` are already committed and load automatically.

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

### 4. Python tooling

`uv venv && uv sync` — the deterministic parser (`scripts/parse_tableau.py`), harvester, showcase, and
validation scripts. Lint/format with `ruff`; the parser has a `pytest` regression suite in `tests/`.

### 5. Preflight — verify everything above in one command

```
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1
```

The `tableau-migrator` agent runs this first on every invocation. It is a PowerShell bootstrap (not
Python) so it works even before Python is installed — checking FOR Python is one of its jobs. It checks
the parser deps, the `powerbi-authoring` plugin, the MCP servers, Power BI Desktop + Bridge CLI, `npx`,
and the TOM DLL, printing an install hint for anything missing (exit 0 = ready). Run it yourself after
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
  Desktop with data before declaring a page done.
- **Keep `limitations_encountered` alive** through the whole build **and** fix phase; every bug found
  and fixed later is itself worth recording. Regenerate it from the final artifacts before sign-off so
  stale entries don't mislead the validator.
- **Surface complexity mismatches proactively.** If the parsed workbook implies more effort than the
  user assumes (many LOD/table-calc fields, extract-only data with no upstream, >20 floating-layout
  worksheets), say so before building rather than discovering it mid-migration.
- **End every message with a clear next step or an explicit verdict** — never a vague "looks fine."
- **Durable learnings go in committed files** (the agent `Gotchas` sections and
  `docs/tableau-dax-translation-guide.md`), never in a git-ignored scratch folder — that is how each
  real migration permanently improves the toolkit.
