# Tableau → Power BI / Fabric Migration Toolkit

AI-assisted migration of Tableau workbooks (`.twb` / `.twbx`) to Microsoft Fabric Power BI
(semantic model + report), built as [GitHub Copilot CLI](https://github.com/github/copilot-cli)
custom agents on top of the official
[`powerbi-authoring`](https://github.com/microsoft/skills-for-fabric) Fabric skills.

This is not a generic "AI can do anything" claim. It's a working pipeline, run end-to-end against a
real, publicly available 16-worksheet Tableau dashboard, with every bug found along the way
documented honestly. See [`docs/capabilities-and-limitations.md`](docs/capabilities-and-limitations.md)
for the full, evidence-based writeup of what worked automatically and what needed human validation.

![Architecture: a deterministic parser extracts a schema-validated migration-spec.json contract, then LLM agents translate it to a Fabric Power BI semantic model + report](docs/architecture.png)

**[See the migration showcase](docs/showcase/README.md)** — original Tableau dashboards side-by-side
with the Power BI reports the pipeline generated from them.

## Why a separate parser instead of an LLM doing everything

Tableau's `.twb` XML (datasources, shelves, zones) is exact and structural — a deterministic parser
is more reliable and reproducible than LLM reasoning for extraction. LLM reasoning is reserved for
the genuinely fuzzy part: translating Tableau calculation formulas (including LOD expressions and
table calculations) to DAX, and mapping chart intent to the right Power BI visual.

## How it works

```
.twb / .twbx
     │
     ▼
scripts/parse_tableau.py  ──────►  migration-spec.json
(deterministic XML parser)          (data sources, fields, calculated-field
                                     formulas, worksheet encodings, dashboard
                                     layout, theme — see docs/migration-spec.md)
     │
     ├──► pbi-semantic-builder (agent)  ──►  Fabric TMDL semantic model
     │    Translates Tableau formulas to DAX using
     │    docs/tableau-dax-translation-guide.md as a playbook.
     │
     └──► pbi-report-builder (agent)  ──►  PBIR report (pages/visuals/bookmarks)
          Chains the official powerbi-report-planning → powerbi-report-design →
          powerbi-report-authoring skills.
```

Both agents are orchestrated by `tableau-migrator`, a custom Copilot CLI agent
(`.github/agents/tableau-migrator.agent.md`).

## Setup: Copilot plugins & MCP (self-configuring)

This toolkit's agents build on Microsoft's official Fabric/Power BI **skill plugin** and talk to Power
BI through **MCP servers**. Those dependencies are declared in the repo so a clone is self-configuring:

- [`AGENTS.md`](AGENTS.md) — auto-loaded by Copilot CLI; declares the required plugin
  (`powerbi-authoring@fabric-collection` from `microsoft/skills-for-fabric`), the MCP servers, and the
  conventions every agent inherits. **Read this first.**
- [`.vscode/mcp.json`](.vscode/mcp.json) — MCP server definitions (auto-read by VS Code Copilot; CLI
  users add the same with `/mcp`).
- Repo-local agents (`.github/agents/`) and skills (`.github/skills/`) are committed and load
  automatically.

In Copilot CLI, install the plugin once with `/plugin` (add marketplace `microsoft/skills-for-fabric`,
enable `powerbi-authoring`) and register the MCP servers with `/mcp` — then the agents below just work.

## Quickstart

```powershell
uv venv
.venv\Scripts\Activate.ps1
uv sync

# Parse a workbook into the intermediate spec
python scripts\parse_tableau.py migrations\<name>\source\<workbook>.twbx `
    -o migrations\<name>\migration-spec.json

# If the workbook uses .hyper extracts (no live DB), pull the real row data too:
python scripts\extract_hyper_data.py migrations\<name>\source\<workbook>.twbx `
    -o migrations\<name>\data
```

Then, in [GitHub Copilot CLI](https://github.com/github/copilot-cli), run the orchestrator agent:

```
/agent tableau-migrator
```

and point it at `migrations\<name>\migration-spec.json`.

## Try the worked example

`migrations/eea-urban-adaptation/` contains a complete, real run against the European Environment
Agency's public
["Urban Audit city factsheets — Urban Adaptation Map Viewer"](https://public.tableau.com/app/profile/european.environment.agency/viz/test_20190116Urban_vulnerability_ideasFR_0/mainpage)
Tableau Public workbook (16 worksheets, 7 data sources, 152 fields) — including the generated
`fabric/UrbanAdaptation.SemanticModel` and `fabric/UrbanAdaptation.Report` PBIP project, ready to
open in Power BI Desktop.

**After cloning**, before you can refresh the model, you must:

1. Download the workbook yourself from the Tableau Public link above (source `.twbx`/extracted data
   are gitignored — not redistributed in this repo) and re-run the two scripts above, **or** just open
   the report to inspect the already-built semantic model/report structure without live data.
2. Update the `DataFolder` Power Query parameter (Transform data → Manage Parameters, in
   `UrbanAdaptation.SemanticModel`) to point at your local `migrations/eea-urban-adaptation/data/`
   path — it ships with a placeholder path since M parameters can't be relative to the project file.

## Repo layout

```
.github/agents/          Custom Copilot CLI agents (orchestrator + subagents)
scripts/                 Python automation (Tableau parser, .hyper extractor)
docs/                    migration-spec schema, Tableau->DAX translation guide, capabilities & limitations
migrations/<name>/       Per-workbook working folder: source file (gitignored), spec, Fabric output
tests/                   pytest suite + XML fixtures for the parser
```

## Development

```powershell
uv sync --extra dev
ruff format . ; ruff check . --fix
pylint scripts
pytest -q
```

## Status

Working end-to-end on one real workbook (see the worked example above). LOD expressions and table
calculations aren't exercised by that workbook, but translation patterns for both are documented in
`docs/tableau-dax-translation-guide.md`, ready to be validated against a second, more complex
workbook. A `pbi-deployer` agent (publish to a Fabric workspace, refresh, screenshot-based fidelity
check) is a natural next phase but isn't built yet.

Contributions — especially a second and third worked example against different Tableau workbooks —
are very welcome.

## License

See [LICENSE](LICENSE).
