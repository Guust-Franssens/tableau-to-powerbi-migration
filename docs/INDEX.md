# Agent navigation index

Map of maps for agent-facing knowledge. Start here, then open the matching index or direct file.

## Start here for a migration unit

1. `python scripts/check_unit.py <unit-or-bundle> --scope <model|report|integration|all>` — first status and final gate; direct `check_*.py` gates only isolate one finding.
2. `python scripts/read_handover.py <bundle> --workbook <name>` — residual work queue and engine-block reason.
3. Load the layer gotchas skill before editing: `powerbi-semantic-model-gotchas` for TMDL/DAX, `powerbi-report-gotchas` for PBIR/visuals, `pbip-model-refresh` for cache/refresh, `powerbi-ai-readiness` for Copilot/Q&A metadata.

Toolkit-maintenance scripts live in `scripts/README.md`, outside the per-unit route.

## Inclusion rule

Eligible files: tracked Markdown knowledge/navigation files, `.github/pbi.kb/**/*.json` visual fixtures, and `scripts/check_*.py` gates. Each appears once below or in explicit exclusions. Run `python scripts/check_navigation_index.py` after eligible file changes.

## Indexed maps and direct entries

| Task | Path | Read when... |
|---|---|---|
| Start or route session | [`AGENTS.md`](../AGENTS.md) | Canonical workflow plus repo agent/skill map. |
| Understand toolkit | [`README.md`](../README.md) | Project purpose and quick start. |
| Contribute safely | [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Validation and PR expectations. |
| Use VS Code Copilot | [`.github/copilot-instructions.md`](../.github/copilot-instructions.md) | VS Code entry instructions. |
| Run or choose scripts | [`scripts/README.md`](../scripts/README.md) | Map of scripts; `test_repo_layout.py` owns script-list completeness. |
| Author PBIR visuals | [`.github/pbi.kb/visual-cookbook.md`](../.github/pbi.kb/visual-cookbook.md) | Map of visual files with confidence tiers and Tableau idioms. |
| Inspect examples | [`examples/README.md`](../examples/README.md) | Map of example migrations, provenance, screenshots, and evidence files. |
| Browse showcase | [`docs/showcase/README.md`](../docs/showcase/README.md) | Map of rendered showcase outputs and variants. |
| Use upstream repros | [`fixtures/upstream-repros/README.md`](../fixtures/upstream-repros/README.md) | Map of fixtures, upstream issues, and pinned engine behavior. |
| Navigate migrations | [`migrations/README.md`](../migrations/README.md) | Map of workbook vs datasource migration folders. |
| Check third-party notices | [`THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md) | License/notice questions. |
| Handle conduct | [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md) | Conduct/community questions. |
| Handle security reports | [`SECURITY.md`](../SECURITY.md) | Security disclosure routing. |
| Find migration guidance | [`docs/agent-architecture.md`](../docs/agent-architecture.md) | Root sessions, subagents, skill visibility. |
| Find migration guidance | [`docs/agent-capability-wiring.md`](../docs/agent-capability-wiring.md) | Registry of shipped capabilities that must be visible to agents. |
| Find migration guidance | [`docs/ai-instructions-authoring-guide.md`](../docs/ai-instructions-authoring-guide.md) | AI-instruction authoring redirect. |
| Find migration guidance | [`docs/capabilities-and-limitations.md`](../docs/capabilities-and-limitations.md) | Automation capability boundaries. |
| Find migration guidance | [`docs/credential-gate-testing.md`](../docs/credential-gate-testing.md) | Credential-gate tests and audit behavior. |
| Find migration guidance | [`docs/credential-gate.md`](../docs/credential-gate.md) | Live-source/Desktop credential wall. |
| Find migration guidance | [`docs/customer-text-exposure.md`](../docs/customer-text-exposure.md) | Customer text/privacy exposure. |
| Find migration guidance | [`docs/data-source-credentials.md`](../docs/data-source-credentials.md) | Tableau/database/warehouse credentials. |
| Find migration guidance | [`docs/deterministic-tier-integration.md`](../docs/deterministic-tier-integration.md) | Engine and handoff integration. |
| Find migration guidance | [`docs/dry-run-findings-2026-08-11.md`](../docs/dry-run-findings-2026-08-11.md) | Durable F00x dry-run findings. |
| Find migration guidance | [`docs/migration-programme.md`](../docs/migration-programme.md) | Programme phases and operating model. |
| Find migration guidance | [`docs/migration-spec.md`](../docs/migration-spec.md) | `migration-spec.json` contract. |
| Find migration guidance | [`docs/operator-runbook.md`](../docs/operator-runbook.md) | Manual pipeline operation. |
| Test the offline deployment rehearsal | [`docs/offline-mock-harness.md`](offline-mock-harness.md) | Offline Tableau-to-Fabric mock harness and fidelity boundary. |
| Find migration guidance | [`docs/reference-capture.md`](../docs/reference-capture.md) | Tableau reference capture and evidence grading. |
| Find migration guidance | [`docs/review-remediation-plan.md`](../docs/review-remediation-plan.md) | Route validation/review findings. |
| Find migration guidance | [`docs/tableau-dax-translation-guide.md`](../docs/tableau-dax-translation-guide.md) | Translate Tableau calcs/LODs/table calcs. |
| Find migration guidance | [`docs/tableau-map-to-azuremaps.md`](../docs/tableau-map-to-azuremaps.md) | Translate Tableau maps to Azure Maps. |
| Find migration guidance | [`docs/upstream-issue-gate.md`](../docs/upstream-issue-gate.md) | Route engine issues upstream vs local. |
| Invoke subagent | [`.github/agents/pbi-migration-validator.agent.md`](../.github/agents/pbi-migration-validator.agent.md) | Read-only fidelity validator persona. |
| Invoke subagent | [`.github/agents/pbi-report-builder.agent.md`](../.github/agents/pbi-report-builder.agent.md) | PBIR/report repair persona. |
| Invoke subagent | [`.github/agents/pbi-semantic-builder.agent.md`](../.github/agents/pbi-semantic-builder.agent.md) | TMDL/DAX/model repair persona. |
| Invoke subagent | [`.github/agents/tableau-migrator.agent.md`](../.github/agents/tableau-migrator.agent.md) | Workbook/datasource migration worker persona. |
| Use fixture | [`fixtures/large-refresh/README.md`](../fixtures/large-refresh/README.md) | Large refresh/cache behavior fixture. |
| Use fixture | [`tests/fixtures/check-gates-dirty/README.md`](../tests/fixtures/check-gates-dirty/README.md) | Dirty gate golden-output fixture. |

## Explicit exclusions
| Task | Path | Exclusion reason |
|---|---|---|
| Exclude self | [`docs/INDEX.md`](../docs/INDEX.md) | This registry would not help choose a next file. |
| Exclude test skill | [`.github/skills/sentinel-probe/SKILL.md`](../.github/skills/sentinel-probe/SKILL.md) | Test-only subagent skill-visibility sentinel. |

### PBIR visual files
Reason: indexed by `.github/pbi.kb/visual-cookbook.md`; it adds confidence tiers and Tableau idiom mapping.
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

### Validation gate implementations
Reason: prefer `check_unit --scope <layer>`; `scripts/README.md` indexes direct gates for isolation.
Base: `scripts/`
```text
check_agent_capabilities.py
check_ai_readiness.py
check_blank_placeholders.py
check_datamodel.py
check_desktop_orphans.py
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

### Example evidence files
Reason: `examples/README.md` carries provenance, artifact context, screenshots, and nested evidence links.
Base: ``
```text
examples/electricity-per-capita/fabric/DISPOSITIONS.md
examples/fast-fashion-impact/fabric/DISPOSITIONS.md
examples/fast-fashion-impact/fabric/RELATIONSHIPS.md
examples/health-tracker/ai-instructions.md
examples/interactive-resume/fabric/DISPOSITIONS.md
examples/price-of-prosperity/fabric/DISPOSITIONS.md
examples/quadruple-axis-charts/report_build/PAGE-PLAN.md
examples/superstore-sales-performance/_brief/report-spec.md
examples/wind-energy-utilization/ai-instructions.md
```

### Showcase variants
Reason: `docs/showcase/README.md` carries showcase narrative, rendered images, and variant routing.
Base: ``
```text
docs/showcase/README-afterbefore.md
```

### Upstream repro notes
Reason: `fixtures/upstream-repros/README.md` carries issue mapping and pinned engine behavior.
Base: ``
```text
fixtures/upstream-repros/issue-166-custom-sql-disambiguation/README.md
fixtures/upstream-repros/issue-168-case-one-bad-branch/README.md
fixtures/upstream-repros/issue-171-measure-names-parameter/README.md
```

### Migration folder READMEs
Reason: `migrations/README.md` routes workbook vs datasource migration folders.
Base: ``
```text
migrations/datasources/README.md
migrations/workbooks/README.md
```

### Repo-local skill docs
Reason: `AGENTS.md` indexes repo-local skills; the start-here block names when to invoke the per-layer skills.
Base: ``
```text
.github/skills/pbip-model-refresh/SKILL.md
.github/skills/powerbi-ai-readiness/SKILL.md
.github/skills/powerbi-report-gotchas/SKILL.md
.github/skills/powerbi-semantic-model-gotchas/SKILL.md
```
