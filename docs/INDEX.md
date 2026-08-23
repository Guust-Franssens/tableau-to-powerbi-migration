# Agent navigation index

This is the tier-2 navigation contract for agents. It is intentionally task-oriented: choose the row for what you are about to do, then load that file on demand.

Use this for content an agent goes looking for. Rules that must already be in an agent's head when things go wrong — retry caps, layer ownership, credential stops — stay inline in the personas through `AGENTS.md` and are not outsourced to this index.

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

The reverse check treats these as eligible: every tracked Markdown knowledge/navigation file, every `.github/pbi.kb/**/*.json` visual fixture, and every `scripts/check_*.py` gate. Each eligible file must appear exactly once below or exactly once in the explicit-exclusion table with a reason. Run `python scripts/check_navigation_index.py` after adding, moving, or deleting any eligible file.

## Indexed paths

| Task | Path | Read when... |
|---|---|---|
| Author PBIR visuals | [`.github/pbi.kb/visual-cookbook.md`](../.github/pbi.kb/visual-cookbook.md) | Read before choosing a visual encoding or copying a proven PBIR pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/actionButton.md`](../.github/pbi.kb/visuals/actionButton.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/actionButton.visual.json`](../.github/pbi.kb/visuals/actionButton.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/areaChart.md`](../.github/pbi.kb/visuals/areaChart.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/areaChart.visual.json`](../.github/pbi.kb/visuals/areaChart.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/azureMap.md`](../.github/pbi.kb/visuals/azureMap.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/card.md`](../.github/pbi.kb/visuals/card.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/card.visual.json`](../.github/pbi.kb/visuals/card.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/decompositionTreeVisual.md`](../.github/pbi.kb/visuals/decompositionTreeVisual.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/decompositionTreeVisual.visual.json`](../.github/pbi.kb/visuals/decompositionTreeVisual.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/donutChart.md`](../.github/pbi.kb/visuals/donutChart.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/donutChart.visual.json`](../.github/pbi.kb/visuals/donutChart.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/error-bars.md`](../.github/pbi.kb/visuals/error-bars.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/error-bars.visual.json`](../.github/pbi.kb/visuals/error-bars.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/forecast.md`](../.github/pbi.kb/visuals/forecast.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/funnel.md`](../.github/pbi.kb/visuals/funnel.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/funnel.visual.json`](../.github/pbi.kb/visuals/funnel.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/heatMap.md`](../.github/pbi.kb/visuals/heatMap.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/heatMap.visual.json`](../.github/pbi.kb/visuals/heatMap.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/hundredPercentStackedAreaChart.md`](../.github/pbi.kb/visuals/hundredPercentStackedAreaChart.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/hundredPercentStackedAreaChart.visual.json`](../.github/pbi.kb/visuals/hundredPercentStackedAreaChart.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/hundredPercentStackedColumnChart.md`](../.github/pbi.kb/visuals/hundredPercentStackedColumnChart.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/hundredPercentStackedColumnChart.visual.json`](../.github/pbi.kb/visuals/hundredPercentStackedColumnChart.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/keyDriversVisual.md`](../.github/pbi.kb/visuals/keyDriversVisual.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/keyDriversVisual.visual.json`](../.github/pbi.kb/visuals/keyDriversVisual.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/kpi.md`](../.github/pbi.kb/visuals/kpi.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/kpi.visual.json`](../.github/pbi.kb/visuals/kpi.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/lineClusteredColumnComboChart.md`](../.github/pbi.kb/visuals/lineClusteredColumnComboChart.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/lineClusteredColumnComboChart.visual.json`](../.github/pbi.kb/visuals/lineClusteredColumnComboChart.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/lineStackedColumnComboChart.md`](../.github/pbi.kb/visuals/lineStackedColumnComboChart.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/lineStackedColumnComboChart.visual.json`](../.github/pbi.kb/visuals/lineStackedColumnComboChart.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/multiRowCard.md`](../.github/pbi.kb/visuals/multiRowCard.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/multiRowCard.visual.json`](../.github/pbi.kb/visuals/multiRowCard.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/pieChart.md`](../.github/pbi.kb/visuals/pieChart.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/pieChart.visual.json`](../.github/pbi.kb/visuals/pieChart.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/reference-lines.md`](../.github/pbi.kb/visuals/reference-lines.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/reference-lines.visual.json`](../.github/pbi.kb/visuals/reference-lines.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/ribbonChart.md`](../.github/pbi.kb/visuals/ribbonChart.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/ribbonChart.visual.json`](../.github/pbi.kb/visuals/ribbonChart.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/shape.md`](../.github/pbi.kb/visuals/shape.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/shape.visual.json`](../.github/pbi.kb/visuals/shape.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/shapeMap.md`](../.github/pbi.kb/visuals/shapeMap.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/shapeMap.visual.json`](../.github/pbi.kb/visuals/shapeMap.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/smallmultiples.md`](../.github/pbi.kb/visuals/smallmultiples.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/smallmultiples.visual.json`](../.github/pbi.kb/visuals/smallmultiples.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/stackedAreaChart.md`](../.github/pbi.kb/visuals/stackedAreaChart.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/stackedAreaChart.visual.json`](../.github/pbi.kb/visuals/stackedAreaChart.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/step-line.md`](../.github/pbi.kb/visuals/step-line.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/table-cond-format.md`](../.github/pbi.kb/visuals/table-cond-format.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/table-cond-format.visual.json`](../.github/pbi.kb/visuals/table-cond-format.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/table-databars.md`](../.github/pbi.kb/visuals/table-databars.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/table-databars.visual.json`](../.github/pbi.kb/visuals/table-databars.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/textSlicer.md`](../.github/pbi.kb/visuals/textSlicer.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/treemap.md`](../.github/pbi.kb/visuals/treemap.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/treemap.visual.json`](../.github/pbi.kb/visuals/treemap.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/waterfallChart.md`](../.github/pbi.kb/visuals/waterfallChart.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/waterfallChart.visual.json`](../.github/pbi.kb/visuals/waterfallChart.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/zoom-slider.md`](../.github/pbi.kb/visuals/zoom-slider.md) | Read when you need the task-specific notes for this PBIR visual or formatting pattern. |
| Author PBIR visuals | [`.github/pbi.kb/visuals/zoom-slider.visual.json`](../.github/pbi.kb/visuals/zoom-slider.visual.json) | Copy or compare when you need a validate-passing visual.json fixture for this visual pattern. |
| Check third-party notices | [`THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md) | Read before answering license or notice questions. |
| Contribute safely | [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Read before changing code or docs to follow local validation and PR expectations. |
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
| Handle conduct or disclosure | [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md) | Read when a collaboration or community-standard question arises. |
| Handle security reports | [`SECURITY.md`](../SECURITY.md) | Read before reporting or routing a security vulnerability. |
| Inspect example evidence | [`examples/electricity-per-capita/fabric/DISPOSITIONS.md`](../examples/electricity-per-capita/fabric/DISPOSITIONS.md) | Read when checking per-example migration disposition decisions and known caveats. |
| Inspect example evidence | [`examples/fast-fashion-impact/fabric/DISPOSITIONS.md`](../examples/fast-fashion-impact/fabric/DISPOSITIONS.md) | Read when checking per-example migration disposition decisions and known caveats. |
| Inspect example evidence | [`examples/fast-fashion-impact/fabric/RELATIONSHIPS.md`](../examples/fast-fashion-impact/fabric/RELATIONSHIPS.md) | Read when checking per-example relationship modeling decisions. |
| Inspect example evidence | [`examples/health-tracker/ai-instructions.md`](../examples/health-tracker/ai-instructions.md) | Read when comparing committed AI-instruction examples for generated semantic models. |
| Inspect example evidence | [`examples/interactive-resume/fabric/DISPOSITIONS.md`](../examples/interactive-resume/fabric/DISPOSITIONS.md) | Read when checking per-example migration disposition decisions and known caveats. |
| Inspect example evidence | [`examples/price-of-prosperity/fabric/DISPOSITIONS.md`](../examples/price-of-prosperity/fabric/DISPOSITIONS.md) | Read when checking per-example migration disposition decisions and known caveats. |
| Inspect example evidence | [`examples/quadruple-axis-charts/report_build/PAGE-PLAN.md`](../examples/quadruple-axis-charts/report_build/PAGE-PLAN.md) | Read when comparing report planning artifacts against generated example reports. |
| Inspect example evidence | [`examples/superstore-sales-performance/_brief/report-spec.md`](../examples/superstore-sales-performance/_brief/report-spec.md) | Read when comparing report planning artifacts against generated example reports. |
| Inspect example evidence | [`examples/wind-energy-utilization/ai-instructions.md`](../examples/wind-energy-utilization/ai-instructions.md) | Read when comparing committed AI-instruction examples for generated semantic models. |
| Invoke a custom subagent | [`.github/agents/pbi-migration-validator.agent.md`](../.github/agents/pbi-migration-validator.agent.md) | Read when doing read-only fidelity validation against Tableau references and Power BI output. |
| Invoke a custom subagent | [`.github/agents/pbi-report-builder.agent.md`](../.github/agents/pbi-report-builder.agent.md) | Read when fixing PBIR visuals, pages, filters, layout, screenshots, or Desktop rendering. |
| Invoke a custom subagent | [`.github/agents/pbi-semantic-builder.agent.md`](../.github/agents/pbi-semantic-builder.agent.md) | Read when fixing TMDL, DAX, relationships, model load, refresh, or AI-readiness work. |
| Invoke a custom subagent | [`.github/agents/tableau-migrator.agent.md`](../.github/agents/tableau-migrator.agent.md) | Read when dispatching a workbook/data-source migration subagent or checking orchestration duties. |
| Load a repo skill | [`.github/skills/pbip-model-refresh/SKILL.md`](../.github/skills/pbip-model-refresh/SKILL.md) | Invoke after TMDL edits or when Desktop/cache refresh and .pbi/cache.abf persistence are needed. |
| Load a repo skill | [`.github/skills/powerbi-ai-readiness/SKILL.md`](../.github/skills/powerbi-ai-readiness/SKILL.md) | Invoke when a semantic model must answer Copilot/Fabric data-agent natural-language questions correctly. |
| Load a repo skill | [`.github/skills/powerbi-report-gotchas/SKILL.md`](../.github/skills/powerbi-report-gotchas/SKILL.md) | Invoke before PBIR authoring/debugging, especially when validation passes but rendering is wrong. |
| Load a repo skill | [`.github/skills/powerbi-semantic-model-gotchas/SKILL.md`](../.github/skills/powerbi-semantic-model-gotchas/SKILL.md) | Invoke before hand-authoring TMDL/DAX or diagnosing model open/refresh/render failures. |
| Navigate repo artifacts | [`examples/README.md`](../examples/README.md) | Read when selecting example workbooks as fixtures, demonstrations, or regression evidence. |
| Navigate repo artifacts | [`migrations/README.md`](../migrations/README.md) | Read when navigating committed migration outputs and their intended structure. |
| Navigate repo artifacts | [`migrations/datasources/README.md`](../migrations/datasources/README.md) | Read when working with migrated standalone Tableau data sources. |
| Navigate repo artifacts | [`migrations/workbooks/README.md`](../migrations/workbooks/README.md) | Read when working with migrated workbook deliverables. |
| Navigate repo artifacts | [`scripts/README.md`](../scripts/README.md) | Read when choosing or adding CLI scripts and understanding script categories. |
| Start from VS Code Copilot | [`.github/copilot-instructions.md`](../.github/copilot-instructions.md) | Read when VS Code is the runtime; it points at AGENTS.md and duplicates only the session-start rule. |
| Start or route a repo session | [`AGENTS.md`](../AGENTS.md) | Read before any work to get the canonical repo workflow, migration dispatcher, and shared conventions. |
| Understand the toolkit | [`README.md`](../README.md) | Read when you need the project purpose, quick-start shape, and repository tour. |
| Use test fixtures | [`fixtures/large-refresh/README.md`](../fixtures/large-refresh/README.md) | Read when using the large local refresh fixture for Desktop/cache refresh behavior. |
| Use test fixtures | [`fixtures/upstream-repros/README.md`](../fixtures/upstream-repros/README.md) | Read when using upstream repro fixtures or explaining why a minimized Tableau fixture exists. |
| Use test fixtures | [`fixtures/upstream-repros/issue-166-custom-sql-disambiguation/README.md`](../fixtures/upstream-repros/issue-166-custom-sql-disambiguation/README.md) | Read when using upstream repro fixtures or explaining why a minimized Tableau fixture exists. |
| Use test fixtures | [`fixtures/upstream-repros/issue-168-case-one-bad-branch/README.md`](../fixtures/upstream-repros/issue-168-case-one-bad-branch/README.md) | Read when using upstream repro fixtures or explaining why a minimized Tableau fixture exists. |
| Use test fixtures | [`fixtures/upstream-repros/issue-171-measure-names-parameter/README.md`](../fixtures/upstream-repros/issue-171-measure-names-parameter/README.md) | Read when using upstream repro fixtures or explaining why a minimized Tableau fixture exists. |
| Validate an artifact | [`scripts/check_ai_readiness.py`](../scripts/check_ai_readiness.py) | Prefer `check_unit --scope model`; run directly only to isolate a single finding around model descriptions, CustomInstructions, domains, or qnaEnabled. |
| Validate an artifact | [`scripts/check_blank_placeholders.py`](../scripts/check_blank_placeholders.py) | Prefer `check_unit --scope integration`; run directly only to isolate a single finding around PBIR text boxes, titles, labels, or generated placeholders. |
| Validate an artifact | [`scripts/check_datamodel.py`](../scripts/check_datamodel.py) | Prefer `check_unit --scope model`; run directly only to isolate a single finding around M/TMDL structure or model deserialization. |
| Validate an artifact | [`scripts/check_desktop_orphans.py`](../scripts/check_desktop_orphans.py) | Prefer `check_unit --scope all`; run directly only to isolate a single finding around run-owned Power BI Desktop processes left open after completion. |
| Validate an artifact | [`scripts/check_empty_model.py`](../scripts/check_empty_model.py) | Prefer `check_unit --scope model`; run directly only to isolate a single finding when Desktop opens a model/report empty or a cache/model handoff looks suspect. |
| Validate an artifact | [`scripts/check_engine_receipts.py`](../scripts/check_engine_receipts.py) | Prefer `check_unit --scope all`; run directly only to isolate a single finding around deterministic-engine provenance and canonical engine receipts. |
| Validate an artifact | [`scripts/check_field_bindings.py`](../scripts/check_field_bindings.py) | Prefer `check_unit --scope integration`; run directly only to isolate a single finding around visuals binding missing columns/measures or incorrect projections. |
| Validate an artifact | [`scripts/check_m_syntax.py`](../scripts/check_m_syntax.py) | Run or inspect when Power Query M was generated or edited and must parse before Desktop. |
| Validate an artifact | [`scripts/check_migration_progress.py`](../scripts/check_migration_progress.py) | Run or inspect when supervising whether migrated workbooks have required artifacts and status. |
| Validate an artifact | [`scripts/check_navigation_index.py`](../scripts/check_navigation_index.py) | Run or inspect when adding/removing agent-facing docs, gates, personas, skills, or KB files. |
| Validate an artifact | [`scripts/check_pbir_layout.py`](../scripts/check_pbir_layout.py) | Prefer `check_unit --scope report`; run directly only to isolate a single finding around PBIR page geometry, visual bounds, or off-canvas layout. |
| Validate an artifact | [`scripts/check_pbir_valid.py`](../scripts/check_pbir_valid.py) | Prefer `check_unit --scope report`; run directly only to isolate a single finding around PBIR schema validation and report-author CLI results. |
| Validate an artifact | [`scripts/check_relationship_health.py`](../scripts/check_relationship_health.py) | Prefer `check_unit --scope model`; run directly only to isolate a single finding around model relationships, cardinality, active paths, or ambiguity. |
| Validate an artifact | [`scripts/check_sqlproxy_connections.py`](../scripts/check_sqlproxy_connections.py) | Prefer `check_unit --scope model`; run directly only to isolate a single finding around Tableau sqlproxy/published-source connections hiding server-side logic. |
| Validate an artifact | [`scripts/check_stub_measures.py`](../scripts/check_stub_measures.py) | Prefer `check_unit --scope model`; run directly only to isolate a single finding around deterministic conversion placeholder/stub DAX measures. |
| Validate an artifact | [`scripts/check_unit.py`](../scripts/check_unit.py) | Preferred façade for layer-scoped validation: run with `--scope model`, `--scope report`, `--scope integration`, or `--scope all` for a workbook, model, or report. |
| Validate an artifact | [`tests/fixtures/check-gates-dirty/README.md`](../tests/fixtures/check-gates-dirty/README.md) | Read when updating dirty gate fixtures or interpreting their golden stdout snapshots. |

## Explicit exclusions

| Task | Path | Exclusion reason |
|---|---|---|
| Exclude from navigation targets | [`docs/INDEX.md`](../docs/INDEX.md) | This file is the registry being checked; indexing itself would not help an agent choose a next file. |
| Exclude from production migration guidance | [`.github/skills/sentinel-probe/SKILL.md`](../.github/skills/sentinel-probe/SKILL.md) | Test-only sentinel skill for proving subagent skill visibility; not part of the migration pipeline. |
