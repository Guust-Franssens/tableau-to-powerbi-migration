# Writing good AI instructions for a migrated semantic model

Model-level **AI instructions** are the free-form guidance that Power BI Copilot and Fabric data
agents read *before* they query a model. In this toolkit they are file-committable: they live in the
culture object (`cultureInfo <lcid>` → `linguisticMetadata` JSON → top-level **`CustomInstructions`**)
and are stamped from a per-migration `migrations/<slug>/ai-instructions.md` by
`scripts/set_ai_instructions.py`. See the storage mechanism in
[`.github/agents/pbi-semantic-builder.agent.md`](../.github/agents/pbi-semantic-builder.agent.md)
("Prep the model for AI"). This file is about **what to write**, not where it goes.

Verified end-to-end (2026-07): after `fab import`, the remote Power BI MCP server
(`GetSemanticModelSchema`) returns the model schema **with** the `CustomInstructions` field, byte-for-byte
what we stamped, so Copilot / data agents genuinely consume it.

## Principles (from Microsoft Learn + [tabulareditor.com](https://tabulareditor.com/blog/how-to-write-good-ai-instructions-for-a-semantic-model), grounded in Anthropic's [context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents))

1. **It is a writing task, not an engineering one.** Do not mass-generate it. Ground every line in the
   real model (read the TMDL, the extracted CSV, the ground-truth totals).
2. **Highest-signal tokens only; beware context rot.** The model's recall of *any* single line drops as
   the text grows. Informative yet tight. Prefer ~1–3 KB of dense guidance over a 10 KB manual (the hard
   cap is 10,000 chars, but that is a ceiling, not a target).
3. **Say nothing the metadata already shows.** Table/column/measure names, data types, and format
   strings are already in the schema the agent receives. Only write what the schema *cannot* convey:
   conventions, defaults, business logic, and intent.
4. **Resolve the ambiguities a user's phrasing leaves open.** This is the core value. Map fuzzy business
   terms to specific measures, and state the default table/filter/period to use when a question is vague.
5. **Add a "For Copilot" section for output + visualization style.** Concise answers (lead with the
   number), preferred chart types, part-to-whole = bar not pie, etc. (Data agents use only the DAX-gen
   guidance; Copilot uses the style/viz guidance too.)
6. **Iterate from real questions.** Treat it as living: test with the kinds of questions the dashboard
   answers, watch where the agent guesses wrong, tighten. Record durable learnings here or in the file.

## What Copilot and data agents actually consume

Per [Microsoft Learn](https://learn.microsoft.com/en-us/fabric/data-science/semantic-model-best-practices),
when Copilot or a Fabric data agent answers a question, the DAX-generation tool grounds itself in: the
model **schema** (table/column/measure names + types), object **descriptions**, **synonyms**, numeric
column **min/max**, **report-visual metadata**, and the **Prep-for-AI** config (AI instructions, AI data
schema, verified answers). Two consequences shape how we author:

- **AI instructions are the only free-text lever that reaches the DAX tool.** Data-agent-level notes are
  **ignored** for semantic-model queries, so all model-specific guidance MUST live in `CustomInstructions`
  (Prep for AI), never on the data agent.
- **Instructions complement, do not replace, base metadata.** Keep doing the fundamentals (star schema,
  business-friendly names, a description on every object, hidden helper columns, explicit measures +
  synonyms). Instructions add the judgement the schema cannot express.
- **`qnaEnabled` must be `true` (crucial).** The model's Q&A / natural-language surface (which is what
  consumes the linguistic metadata *and* the `CustomInstructions`) is gated by `settings.qnaEnabled` in
  `definition.pbism`. Migrated models default to `false`, which silently makes Copilot/Q&A ignore the
  instructions you stamped. `scripts/set_ai_instructions.py` sets it to `true` automatically when
  stamping and flags any stamped model still on `false` in `--check`.

Without steering, agents tend to invent **implicit measures** (raw `SUM`/`AVERAGE` of columns) and bypass
the carefully built DAX a migration produces. An explicit "prefer the model's measures" line is high-value
([data-marc](https://data-marc.com/2025/06/04/automatically-populate-data-agents-with-semantic-model-synonyms/)).

## Recommended sections for a migrated dashboard

Keep the headings; drop any section that would only restate metadata.

```markdown
# <Model name>
<1–3 sentences: what the model is, its grain, and what questions it answers.>

## Grain and tables
- <fact table>: grain + role. <dimension/disconnected tables>: role (esp. parameter-proxy /
  disconnected tables a migration produces, which the agent must NOT treat as dimensions).

## Business terminology and defaults        # resolve ambiguity — the highest-value part
- "<fuzzy term>" means [<specific measure>], not [<other>].
- Default to <table/filter/period> when a question is ambiguous.

## Measure-naming conventions               # explain PATTERNS, do not enumerate every measure
- Prefix/suffix conventions the migration introduced (e.g. CM = current month, T = turbine-filtered).

## Verified headline numbers                 # optional: anchor the agent to known-correct totals
- [<measure>] = <ground-truth value> (so a wrong answer is self-evident).

## For Copilot (style + visuals)
- Answer length/tone; preferred/avoided chart types.

## Things to avoid
- Disconnected/parameter tables that must not be grouped by; measures that must not be summed/averaged;
  "latest" = max date in data, not today; etc.
```

### Instruction patterns that work (pick the ones that apply)

From Microsoft Learn and [rossmcneely](https://rossmcneely.com/2025/11/17/maximizing-power-bi-copilot-a-data-analyst-guide-to-ai-ready-semantic-models/):

- **Business terminology:** "'churn' = no purchase in 90 days"; "'sales' means `[Net Sales]`, never `[Gross Sales]`".
- **Metric preferences:** when several measures look similar, name the one to use ("for profitability use `[Contribution Margin]`, not `[Gross Profit]`").
- **Data-source routing:** which table to prefer for a kind of question ("for inventory, prefer `'Warehouse Inventory'` over `'Sales Orders'`").
- **Default groupings / time:** fiscal vs calendar, default period, default filter (e.g. completed orders only).
- **Clarification triggers:** when to ask the user to disambiguate (which region? which period?).
- **Prefer explicit measures:** tell the agent to use the model's measures, not build implicit `SUM`/`AVERAGE`
  of raw columns — this is where the migrated DAX logic lives.

## Migration-specific gotchas worth encoding

Tableau→Power BI migrations reliably produce a few things an agent will otherwise mishandle. Call them
out explicitly:

- **Parameter-proxy / disconnected tables** (from Tableau parameters): single-value control tables that
  feed a `[... Value]` measure. Tell the agent they are not dimensions and not calendars.
- **"Latest" semantics**: extract-based models freeze at a max date. State that "latest/current" = the
  max date present, never the system date.
- **Snapshot measures**: `Latest*` / `Prior*` / `CM*` / `PM*` measures are already period-scoped; tell the
  agent not to re-aggregate them across dates.
- **Geometry/helper measures** (spiral X/Y, angles, thickness for IronViz-style visuals): mark them as
  visual helpers, not business metrics, so they never surface in an answer.

## Anti-patterns

- A wall of prose, or a line-per-measure catalog (context rot; restates metadata).
- Restating data types / format strings / obvious column meanings.
- Generic BI advice with no reference to this model's real fields.
- Anything you could not verify against the model or its ground truth.

## Sources

- Microsoft Learn — [Semantic model best practices for data agent](https://learn.microsoft.com/en-us/fabric/data-science/semantic-model-best-practices) (query-processing flow; "the DAX-generation tool relies solely on model metadata + Prep-for-AI, and ignores data-agent-level instructions"; effective instruction patterns).
- Microsoft Learn — [Prepare your data for AI](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-prepare-data-ai) (AI instructions, AI data schema, verified answers).
- tabulareditor.com — [How to write good AI instructions for a semantic model](https://tabulareditor.com/blog/how-to-write-good-ai-instructions-for-a-semantic-model) (writing task, sectioned high-signal lines, storage in `CustomInstructions`).
- Anthropic — [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) (right altitude, context rot).
- rossmcneely.com — [Maximizing Power BI Copilot: a data analyst guide to AI-ready semantic models](https://rossmcneely.com/2025/11/17/maximizing-power-bi-copilot-a-data-analyst-guide-to-ai-ready-semantic-models/) (fundamentals + instruction content categories).
- data-marc.com — [Automatically populate data agents with semantic model synonyms](https://data-marc.com/2025/06/04/automatically-populate-data-agents-with-semantic-model-synonyms/) (steer to explicit measures; synonyms respected since Oct 2025).
