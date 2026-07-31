---
name: powerbi-ai-readiness
description: Make a Power BI semantic model answer natural-language questions correctly - descriptions, enumerated domains, model-level AI instructions (CustomInstructions), and the qnaEnabled switch that silently voids all of it. Use when a migrated or newly built semantic model needs to be prepped for Copilot / Fabric data agents, when writing or reviewing an ai-instructions.md, or when a Copilot answer is wrong and you suspect the model's AI metadata. Source-tool agnostic (Tableau, Qlik, Cognos to Power BI).
---

# Power BI AI readiness

A model that renders correct visuals can still answer natural-language questions wrongly. Copilot and
Fabric data agents never see your report — they see **model metadata plus the Prep-for-AI config**, and
generate DAX from that. This skill covers the levers that are committable to Git, the exact storage
mechanism, the two scripts that stamp and audit them, and — the largest part — **what to write**.

## Available scripts

| Script | What it does |
|---|---|
| `scripts/set_ai_instructions.py` | Stamps a migration's `ai-instructions.md` into the model's culture TMDL, forces `qnaEnabled: true`, lints the text, and reports/gates coverage (`--check [--strict] [--model]`) |
| `scripts/check_ai_readiness.py` | Audits TMDL description coverage per table/column/measure and flags categorical columns whose description does not enumerate its domain |

Both are standalone (`python <path>`), import nothing outside this folder, and locate the host repo by
walking up for a migration tree — so they work from the bundle, from a `scripts/` forwarding shim, or
from a copy in another repo.

## 1. The five levers you can commit

| # | Lever | Where it lives | Status |
|---|---|---|---|
| 1 | **Descriptions** on every table, column, measure | `/// ` lines in `definition/tables/*.tmdl` | ✅ committable, audited by `check_ai_readiness.py` |
| 2 | **Enumerated domains** on categorical columns ("One of: A, B, C") | inside those descriptions | ✅ committable, audited |
| 3 | **Synonyms** (linguistic schema `Entities`) | same `linguisticMetadata` JSON as #4 | ⚠️ committable in principle, **not exercised here** — no MCP write surface, and hand-authoring the entity/binding graph is error-prone. Fold abbreviation meanings into descriptions instead |
| 4 | **AI instructions** (`CustomInstructions`) | `definition/cultures/<lcid>.tmdl` | ✅ committable, stamped by `set_ai_instructions.py` |
| 5 | **`qnaEnabled`** | `settings.qnaEnabled` in `definition.pbism` | ✅ committable — **and the one that silently voids 1–4 if false** |

Why #2 matters specifically: DAX Copilot reads roughly the **first 200 chars** of each description, so
"One of: Onshore, Offshore" is what lets it resolve *"how did offshore do?"* to a filter rather than a
guess ([Microsoft Learn](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-evaluate-data)).

Why #5 matters: migrated models default to `qnaEnabled: false`. With it false the natural-language
surface ignores the linguistic metadata **and** the `CustomInstructions` entirely — you get a stamped
model, a green report, and zero effect. `set_ai_instructions.py` sets it to `true` when stamping and
flags any stamped-but-disabled model in `--check`.

### ⏸️ Not committable today (do these post-deploy, and say so)

| Item | Why it can't be a file |
|---|---|
| **AI data schema** | Lives in service LSDL; no stable file-authoring contract |
| **Verified answers** | Not Git-supported and requires report visuals — author after the report ships |
| **"Approved for Copilot" + semantic indexing** | Tenant/workspace runtime settings, not model files |

Listing these explicitly is part of the job: a migration that claims "AI-ready" without naming what was
deferred is overstating its own coverage.

## 2. Storage mechanism (and why the TMDL is edited directly)

```
definition/cultures/<lcid>.tmdl
  cultureInfo <lcid>
      linguisticMetadata = {"Version": "2.0.0", "Language": "<lcid>", "CustomInstructions": "<markdown>"}
          contentType: json
```

- `CustomInstructions` is a **top-level key of the linguistic-metadata JSON**, a sibling of `Version`
  and `Language` — not nested under `Entities`.
- Scaffold `"Version"` is **`2.0.0`**, matching Power BI's own culture output. `4.2.0` is
  `definition.pbism`'s version — a different schema. (Measured 2026-07-30: a model published with
  `4.2.0` still round-tripped intact, so this is correctness hygiene, not a fix for observed data loss.)
- **The Modeling-MCP culture `Update` surface cannot reach this key** (it exposes name / annotations /
  extendedProperties). Editing the TMDL directly is therefore the mechanism, not a shortcut — and it
  has a second benefit: no XMLA refresh is required to apply it.
- The markdown source is the editable artifact (`<migration>/ai-instructions.md`, two levels above the
  `.SemanticModel` folder); the culture TMDL is **generated**. Edit the markdown, re-stamp.

## 3. Run it

```bash
# audit description coverage + categorical domain enumeration
python scripts/check_ai_readiness.py <tree>/<slug>            # one migration
python scripts/check_ai_readiness.py <tree>/<slug> --strict   # exit 1 below 100% coverage
python scripts/check_ai_readiness.py --all                    # every migration

# stamp the instructions (also forces qnaEnabled: true) and lint them
python scripts/set_ai_instructions.py --model <path to *.SemanticModel>
python scripts/set_ai_instructions.py --all                   # every migration that has an ai-instructions.md

# report / gate
python scripts/set_ai_instructions.py --check                                     # advisory, always exits 0
python scripts/set_ai_instructions.py --check --strict --model <*.SemanticModel>  # GATE one model, exit 1
```

**Use the scoped form as your hand-off gate.** `--check --strict` without `--model` scans every
migration tree, so it fails on any model that predates this layer — a repo-wide gate nobody can turn on
is a gate that never runs. `--model` narrows it to the model you just built, which is the one you are
actually accountable for. Advisory repo-wide `--check` still belongs in CI as a visible backlog counter.

`--check` prints, per model: char count, lint flags, and a loud warning for **stamped but
`qnaEnabled != true`** — the silent-no-op state.

## 4. How to write good AI instructions

### Principles

1. **It is a writing task, not an engineering one.** Do not mass-generate it. Ground every line in the
   real model — read the TMDL, the extracted data, the ground-truth totals.
2. **Highest-signal tokens only; beware context rot.** Recall of *any* single line drops as the text
   grows. Target **~1–3 KB of dense guidance**; `set_ai_instructions.py` warns above **4,000 chars**
   and hard-fails the Power BI cap at **10,000**. The cap is a ceiling, not a target.
3. **Say nothing the metadata already shows.** Names, data types, and format strings are already in the
   schema the agent receives. Write only what the schema *cannot* convey: conventions, defaults,
   business logic, intent.
4. **Resolve the ambiguities a user's phrasing leaves open.** This is the core value — map fuzzy
   business terms to specific measures, and state the default table/filter/period for a vague question.
5. **Add a "For Copilot" section for output and visualization style.** Data agents use only the DAX-gen
   guidance; Copilot uses the style/visual guidance too.
6. **Iterate from real questions.** Test with the questions the dashboard answers, watch where the agent
   guesses wrong, tighten. Record durable learnings back into this skill.

### What the agent actually consumes

Per [Microsoft Learn](https://learn.microsoft.com/en-us/fabric/data-science/semantic-model-best-practices),
the DAX-generation tool grounds itself in: model **schema**, object **descriptions**, **synonyms**,
numeric column **min/max**, **report-visual metadata**, and the **Prep-for-AI** config. Two consequences:

- **AI instructions are the only free-text lever that reaches the DAX tool.** Data-agent-level notes are
  **ignored** for semantic-model queries, so all model-specific guidance MUST live in
  `CustomInstructions` — never on the data agent.
- **Instructions complement, never replace, base metadata.** Keep doing the fundamentals: star schema,
  business-friendly names, a description on every object, hidden helper columns, explicit measures.

Without steering, agents invent **implicit measures** (raw `SUM`/`AVERAGE` over columns) and bypass the
carefully built DAX a migration produces. An explicit "prefer the model's measures" line is high-value
([data-marc](https://data-marc.com/2025/06/04/automatically-populate-data-agents-with-semantic-model-synonyms/)).

### Section template

This is the **single source of truth** for the recommended sections — a second copy elsewhere is how the
"Verified headline numbers" section went missing from one of them. Keep the headings; drop any section
that would only restate metadata.

```markdown
# <Model name>
<1–3 sentences: what the model is, its grain, and what questions it answers.>

## Grain and tables
- <fact table>: grain + role. <dimension/disconnected tables>: role (especially the proxy tables a
  migration produces, which the agent must NOT treat as dimensions).

## Business terminology and defaults        # resolve ambiguity — the highest-value part
- "<fuzzy term>" means [<specific measure>], not [<other>].
- Default to <table/filter/period> when a question is ambiguous.

## Measure-naming conventions               # explain PATTERNS, do not enumerate every measure
- Prefix/suffix conventions the migration introduced (e.g. CM = current month, T = turbine-filtered).

## Verified headline numbers                # optional: anchor the agent to known-correct totals
- [<measure>] = <ground-truth value> (so a wrong answer is self-evident).

## For Copilot (style + visuals)
- Answer length/tone; preferred/avoided chart types.

## Things to avoid
- Disconnected/proxy tables that must not be grouped by; measures that must not be summed/averaged;
  "latest" = max date in data, not today; etc.
```

### Patterns that work

- **Business terminology:** "'churn' = no purchase in 90 days"; "'sales' means `[Net Sales]`, never `[Gross Sales]`".
- **Metric preferences:** when several measures look similar, name the one to use ("for profitability use `[Contribution Margin]`, not `[Gross Profit]`").
- **Data-source routing:** which table to prefer for a kind of question ("for inventory, prefer `'Warehouse Inventory'` over `'Sales Orders'`").
- **Default groupings / time:** fiscal vs calendar, default period, default filter (e.g. completed orders only).
- **Clarification triggers:** when to ask the user to disambiguate (which region? which period?).
- **Prefer explicit measures:** tell the agent to use the model's measures rather than build implicit
  `SUM`/`AVERAGE` over raw columns — that is where the migrated logic lives.

### Anti-patterns

- A wall of prose, or a line-per-measure catalog (context rot; restates metadata).
- Restating data types / format strings / obvious column meanings.
- Generic BI advice with no reference to this model's real fields.
- **Anything you could not verify against the model.** Two real errors shipped this way: a disconnected
  spiral-geometry scaffold described as "geography for the map", and a comparison term borrowed from an
  entirely different migration. Instructions that name the wrong object are **worse than none** — they
  actively steer Copilot wrong. `unresolved_references()` now fails the build on this class of error;
  it only checks `` `'quoted'` `` and `[bracketed]` names, because those are the explicit claims.

## 5. Migration-produced idioms an agent will mishandle

Every BI-to-Power-BI migration leaves behind artifacts that look like dimensions but are not. Name them
explicitly in the instructions — this is the highest-yield section for a migrated model.

| Idiom | Tableau source | Qlik source | Cognos source | What to tell the agent |
|---|---|---|---|---|
| **Control / proxy tables** | Parameters → single-row `'* Parameter'` tables feeding a `[... Value]` measure | Variables and island (unlinked) tables | Prompt/parameter tables | "Not a dimension, not a calendar; never group or filter by it" |
| **"Latest" semantics** | `.hyper` extract frozen at a max date | QVD reload snapshot | Cube build date | "'latest'/'current' = the max date present in the data, never the system date" |
| **Pre-scoped snapshot measures** | `Latest*` / `Prior*` / `CM*` / `PM*` from table calcs | Set-analysis measures | Rolling-period query items | "Already period-scoped — do not re-aggregate across dates" |
| **Visual-geometry helpers** | Spiral X/Y, angle, thickness for IronViz-style charts | Chart-position expressions | Layout-only query items | "Visual helpers, not business metrics — never surface in an answer" |
| **Domain-narrowing filters** | Data-source-level `<filter>` elements | Section-access / reduction | Package-level filters | State the surviving domain, so totals are interpreted against the right universe |

Add a row when a new source tool teaches you one. The mechanism sections above are entirely Power BI —
this table is the only place source-tool specifics belong.

## 6. Evidence and limits

- ✅ **Survives publish, byte-for-byte.** Measured 2026-07-30: a model published via the Fabric items API
  round-tripped through `getDefinition` with `definition/cultures/<lcid>.tmdl` intact and the
  `CustomInstructions` payload exactly as stamped.
- ⚠️ **"Copilot obeys it" is NOT proven by that.** A round-trip shows the text reached the service, not
  that it changed an answer. Verify on your own model with real questions before claiming it works.
- ❌ **Two hard gates on consumption:** `qnaEnabled: true`, and — per Microsoft's guidance — a
  semantic-model **refresh after a Git/deployment change** so the linguistic layer re-syncs.
- **Coverage checks are necessary, not sufficient.** 100% description coverage with a fabricated
  reference in the instructions is worse than 80% with none.

## 7. Reusing this in another migration repo

Copy **this folder**. That is the whole contract — the two scripts import nothing outside it, and the
bundled tests import via `tests/conftest.py` relative to their own location, so they run wherever the
folder lands. There is no repo-root walk in the import path and no dependency on `scripts/`.

Adjust one thing: `MIGRATION_TREES` at the top of each script lists the folders to scan
(`examples`, `migrations/workbooks`, `migrations/datasources`). A repo with a different layout edits
that tuple. `host_root()` finds the owning repo by walking up for the first of those directories, so
nothing else is path-dependent.

Host-repo fixtures (an `examples/` corpus of real models) are optional: the tests that need them skip
with a reason rather than fail.

## Sources

- Microsoft Learn — [Semantic model best practices for data agent](https://learn.microsoft.com/en-us/fabric/data-science/semantic-model-best-practices) (query-processing flow; the DAX tool ignores data-agent-level instructions).
- Microsoft Learn — [Prepare your data for AI](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-prepare-data-ai) (AI instructions, AI data schema, verified answers).
- Microsoft Learn — [Evaluate and improve Copilot answers](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-evaluate-data) (descriptions, first ~200 chars).
- tabulareditor.com — [How to write good AI instructions for a semantic model](https://tabulareditor.com/blog/how-to-write-good-ai-instructions-for-a-semantic-model) (writing task; storage in `CustomInstructions`).
- Anthropic — [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) (right altitude, context rot).
- rossmcneely.com — [Maximizing Power BI Copilot](https://rossmcneely.com/2025/11/17/maximizing-power-bi-copilot-a-data-analyst-guide-to-ai-ready-semantic-models/) (fundamentals + instruction content categories).
- data-marc.com — [Automatically populate data agents with semantic model synonyms](https://data-marc.com/2025/06/04/automatically-populate-data-agents-with-semantic-model-synonyms/) (steer to explicit measures).

## No `allowed-tools` here, deliberately

Both scripts run through `shell`. Pre-approving it in frontmatter would remove the confirmation step
for every command this skill runs, which GitHub explicitly warns against; the one-time approval prompt
is cheap and is the only thing standing between a skill and unattended shell access.
