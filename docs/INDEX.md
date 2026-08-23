# Agent navigation index

Tier-2 navigation contract for agents: choose the task row, then load that file on demand.

Use this for searchable knowledge. Always-needed failure rules stay inline in `AGENTS.md` and generated personas.

## Section index

| Section | What it covers |
|---|---|
| Author PBIR visuals | Proven `.github/pbi.kb/` visual notes and `visual.json` fixtures. |
| Validate an artifact | `scripts/check_*.py` gates and when to run each one. |
| Invoke a custom subagent | The four persona files exactly as subagents receive them. |
| Load a repo skill | Repo-local skills to invoke before specialized Power BI work. |
| Find migration guidance | Operator, credential, migration-spec, reference-capture, and DAX/map guidance. |
| Navigate repo artifacts | Top-level README, examples, migrations, scripts, and contribution/security docs. |
| Explicit exclusions | Eligible files intentionally not used as navigation targets, with reasons. |

## Inclusion rule

Eligible files: tracked Markdown knowledge/navigation files, `.github/pbi.kb/**/*.json` visual fixtures, and `scripts/check_*.py` gates. Each must appear exactly once below or in explicit exclusions with a reason. Run `python scripts/check_navigation_index.py` after eligible file changes.

## Indexed paths

| Task | Path | Read when... |
|---|---|---|
| Author PBIR visuals | [`.github/pbi.kb/visual-cookbook.md`](../.github/pbi.kb/visual-cookbook.md) | Read before choosing any visual encoding; it routes to the visual/idiom files with confidence tiers and Tableau mappings. |
| Validate an artifact | [`scripts/README.md`](../scripts/README.md) | Read when choosing validation/check gates; it is the curated scripts index with purpose, caller, and exit-code context. |
| Check third-party notices | [`THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md) | Read for license/notice questions. |
| Contribute safely | [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Read before code/docs changes. |
| Find migration guidance | [`docs/agent-architecture.md`](../docs/agent-architecture.md) | Read when reasoning about root sessions, subagents, skill visibility, and inheritance boundaries. |
| Find migration guidance | [`docs/ai-instructions-authoring-guide.md`](../docs/ai-instructions-authoring-guide.md) | Read when authoring AI instructions; it redirects to the canonical skill guidance. |
| Find migration guidance | [`docs/capabilities-and-limitations.md`](../docs/capabilities-and-limitations.md) | Read when setting expectations about what the migration toolkit can and cannot automate. |
| Find migration guidance | [`docs/credential-gate-testing.md`](../docs/credential-gate-testing.md) | Read when testing credential gates, modal sign-in handling, or authorization/audit behavior. |
| Find migration guidance | [`docs/credential-gate.md`](../docs/credential-gate.md) | Read when a live source, Desktop, or external credential dependency blocks automation. |
| Find migration guidance | [`docs/customer-text-exposure.md`](../docs/customer-text-exposure.md) | Read before handling customer text, extracts, screenshots, or artifacts with privacy exposure. |
| Find migration guidance | [`docs/data-source-credentials.md`](../docs/data-source-credentials.md) | Read when configuring Tableau, database, or warehouse credentials for migration probes. |
| Find migration guidance | [`docs/deterministic-tier-integration.md`](../docs/deterministic-tier-integration.md) | Read when connecting this repo to the deterministic conversion engine or its handoff artifacts. |
| Find migration guidance | [`docs/dry-run-findings-2026-08-11.md`](../docs/dry-run-findings-2026-08-11.md) | Read for durable F00x dry-run findings that are still cited by regression tests. |
| Find migration guidance | [`docs/migration-programme.md`](../docs/migration-programme.md) | Read when planning the broader migration programme, phases, or operating model. |
| Find migration guidance | [`docs/migration-spec.md`](../docs/migration-spec.md) | Read when creating, validating, or interpreting migration-spec.json contracts. |
| Find migration guidance | [`docs/operator-runbook.md`](../docs/operator-runbook.md) | Read when running the migration pipeline by hand or diagnosing expected operator failure modes. |
| Find migration guidance | [`docs/reference-capture.md`](../docs/reference-capture.md) | Read when capturing Tableau reference images/data or grading validation evidence. |
| Find migration guidance | [`docs/review-remediation-plan.md`](../docs/review-remediation-plan.md) | Read when turning validation/review findings into routed fix work. |
| Find migration guidance | [`docs/showcase/README-afterbefore.md`](../docs/showcase/README-afterbefore.md) | Read when building or reviewing before/after showcase evidence. |
| Find migration guidance | [`docs/showcase/README.md`](../docs/showcase/README.md) | Read when generating or browsing the showcase outputs. |
| Find migration guidance | [`docs/tableau-dax-translation-guide.md`](../docs/tableau-dax-translation-guide.md) | Read when translating Tableau calculations, LODs, table calcs, and Measure Names patterns to DAX. |
| Find migration guidance | [`docs/tableau-map-to-azuremaps.md`](../docs/tableau-map-to-azuremaps.md) | Read when translating Tableau map marks, layers, or geospatial fields to Power BI Azure Maps. |
| Find migration guidance | [`docs/upstream-issue-gate.md`](../docs/upstream-issue-gate.md) | Read before filing or triaging deterministic-engine issues upstream versus local repo issues. |
| Handle conduct or disclosure | [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md) | Read for conduct/community questions. |
| Handle security reports | [`SECURITY.md`](../SECURITY.md) | Read for security reports. |
| Inspect example evidence | [`examples/electricity-per-capita/fabric/DISPOSITIONS.md`](../examples/electricity-per-capita/fabric/DISPOSITIONS.md) | Use for example dispositions/caveats. |
| Inspect example evidence | [`examples/fast-fashion-impact/fabric/DISPOSITIONS.md`](../examples/fast-fashion-impact/fabric/DISPOSITIONS.md) | Use for example dispositions/caveats. |
| Inspect example evidence | [`examples/fast-fashion-impact/fabric/RELATIONSHIPS.md`](../examples/fast-fashion-impact/fabric/RELATIONSHIPS.md) | Read when checking per-example relationship modeling decisions. |
| Inspect example evidence | [`examples/health-tracker/ai-instructions.md`](../examples/health-tracker/ai-instructions.md) | Use for AI-instruction examples. |
| Inspect example evidence | [`examples/interactive-resume/fabric/DISPOSITIONS.md`](../examples/interactive-resume/fabric/DISPOSITIONS.md) | Use for example dispositions/caveats. |
| Inspect example evidence | [`examples/price-of-prosperity/fabric/DISPOSITIONS.md`](../examples/price-of-prosperity/fabric/DISPOSITIONS.md) | Use for example dispositions/caveats. |
| Inspect example evidence | [`examples/quadruple-axis-charts/report_build/PAGE-PLAN.md`](../examples/quadruple-axis-charts/report_build/PAGE-PLAN.md) | Use for report-planning examples. |
| Inspect example evidence | [`examples/superstore-sales-performance/_brief/report-spec.md`](../examples/superstore-sales-performance/_brief/report-spec.md) | Use for report-planning examples. |
| Inspect example evidence | [`examples/wind-energy-utilization/ai-instructions.md`](../examples/wind-energy-utilization/ai-instructions.md) | Use for AI-instruction examples. |
| Invoke a custom subagent | [`.github/agents/pbi-migration-validator.agent.md`](../.github/agents/pbi-migration-validator.agent.md) | Read when doing read-only fidelity validation against Tableau references and Power BI output. |
| Invoke a custom subagent | [`.github/agents/pbi-report-builder.agent.md`](../.github/agents/pbi-report-builder.agent.md) | Read when fixing PBIR visuals, pages, filters, layout, screenshots, or Desktop rendering. |
| Invoke a custom subagent | [`.github/agents/pbi-semantic-builder.agent.md`](../.github/agents/pbi-semantic-builder.agent.md) | Read when fixing TMDL, DAX, relationships, model load, refresh, or AI-readiness work. |
| Invoke a custom subagent | [`.github/agents/tableau-migrator.agent.md`](../.github/agents/tableau-migrator.agent.md) | Read when dispatching a workbook/data-source migration subagent or checking orchestration duties. |
| Load a repo skill | [`.github/skills/pbip-model-refresh/SKILL.md`](../.github/skills/pbip-model-refresh/SKILL.md) | Invoke after TMDL edits or when Desktop/cache refresh and .pbi/cache.abf persistence are needed. |
| Load a repo skill | [`.github/skills/powerbi-ai-readiness/SKILL.md`](../.github/skills/powerbi-ai-readiness/SKILL.md) | Invoke when a semantic model must answer Copilot/Fabric data-agent natural-language questions correctly. |
| Load a repo skill | [`.github/skills/powerbi-report-gotchas/SKILL.md`](../.github/skills/powerbi-report-gotchas/SKILL.md) | Invoke before PBIR authoring/debugging, especially when validation passes but rendering is wrong. |
| Load a repo skill | [`.github/skills/powerbi-semantic-model-gotchas/SKILL.md`](../.github/skills/powerbi-semantic-model-gotchas/SKILL.md) | Invoke before hand-authoring TMDL/DAX or diagnosing model open/refresh/render failures. |
| Navigate repo artifacts | [`examples/README.md`](../examples/README.md) | Use to select example workbooks. |
| Navigate repo artifacts | [`migrations/README.md`](../migrations/README.md) | Use to navigate migration outputs. |
| Navigate repo artifacts | [`migrations/datasources/README.md`](../migrations/datasources/README.md) | Use for migrated data sources. |
| Navigate repo artifacts | [`migrations/workbooks/README.md`](../migrations/workbooks/README.md) | Use for migrated workbooks. |
| Start from VS Code Copilot | [`.github/copilot-instructions.md`](../.github/copilot-instructions.md) | Use for VS Code runtime instructions. |
| Start or route a repo session | [`AGENTS.md`](../AGENTS.md) | Read before repo work. |
| Understand the toolkit | [`README.md`](../README.md) | Use for project tour/quick start. |
| Use test fixtures | [`fixtures/large-refresh/README.md`](../fixtures/large-refresh/README.md) | Use for large refresh fixture. |
| Use test fixtures | [`fixtures/upstream-repros/README.md`](../fixtures/upstream-repros/README.md) | Use for upstream repro fixtures. |
| Use test fixtures | [`fixtures/upstream-repros/issue-166-custom-sql-disambiguation/README.md`](../fixtures/upstream-repros/issue-166-custom-sql-disambiguation/README.md) | Use for upstream repro fixtures. |
| Use test fixtures | [`fixtures/upstream-repros/issue-168-case-one-bad-branch/README.md`](../fixtures/upstream-repros/issue-168-case-one-bad-branch/README.md) | Use for upstream repro fixtures. |
| Use test fixtures | [`fixtures/upstream-repros/issue-171-measure-names-parameter/README.md`](../fixtures/upstream-repros/issue-171-measure-names-parameter/README.md) | Use for upstream repro fixtures. |
| Validate an artifact | [`tests/fixtures/check-gates-dirty/README.md`](../tests/fixtures/check-gates-dirty/README.md) | Use for dirty gate fixtures. |

## Explicit exclusions

| Task | Path | Exclusion reason |
|---|---|---|
| Exclude from navigation targets | [`docs/INDEX.md`](../docs/INDEX.md) | This file is the registry being checked; indexing itself would not help an agent choose a next file. |
| Exclude from production migration guidance | [`.github/skills/sentinel-probe/SKILL.md`](../.github/skills/sentinel-probe/SKILL.md) | Test-only sentinel skill for proving subagent skill visibility; not part of the migration pipeline. |

### Exclusion set: PBIR visual cookbook entries

Reason: indexed by `.github/pbi.kb/visual-cookbook.md`, which carries confidence tiers and Tableau idiom mapping this flat list cannot.
Base: `.github/pbi.kb/visuals/`

```text
actionButton.md
actionButton.visual.json
areaChart.md
areaChart.visual.json
azureMap.md
card.md
card.visual.json
decompositionTreeVisual.md
decompositionTreeVisual.visual.json
donutChart.md
donutChart.visual.json
error-bars.md
error-bars.visual.json
forecast.md
funnel.md
funnel.visual.json
heatMap.md
heatMap.visual.json
hundredPercentStackedAreaChart.md
hundredPercentStackedAreaChart.visual.json
hundredPercentStackedColumnChart.md
hundredPercentStackedColumnChart.visual.json
keyDriversVisual.md
keyDriversVisual.visual.json
kpi.md
kpi.visual.json
lineClusteredColumnComboChart.md
lineClusteredColumnComboChart.visual.json
lineStackedColumnComboChart.md
lineStackedColumnComboChart.visual.json
multiRowCard.md
multiRowCard.visual.json
pieChart.md
pieChart.visual.json
reference-lines.md
reference-lines.visual.json
ribbonChart.md
ribbonChart.visual.json
shape.md
shape.visual.json
shapeMap.md
shapeMap.visual.json
smallmultiples.md
smallmultiples.visual.json
stackedAreaChart.md
stackedAreaChart.visual.json
step-line.md
table-cond-format.md
table-cond-format.visual.json
table-databars.md
table-databars.visual.json
textSlicer.md
treemap.md
treemap.visual.json
waterfallChart.md
waterfallChart.visual.json
zoom-slider.md
zoom-slider.visual.json
```

### Exclusion set: validation gate implementations

Reason: indexed by `scripts/README.md`, which carries purpose, caller, and exit-code guidance this flat list cannot.
Base: `scripts/`

```text
check_ai_readiness.py
check_blank_placeholders.py
check_datamodel.py
check_empty_model.py
check_engine_receipts.py
check_field_bindings.py
check_m_syntax.py
check_migration_progress.py
check_navigation_index.py
check_pbir_layout.py
check_pbir_valid.py
check_relationship_health.py
check_sqlproxy_connections.py
check_stub_measures.py
check_unit.py
```
