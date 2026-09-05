# Agent navigation index

Map of maps for agent-facing knowledge. Start here, then open the matching index or direct file.

## Start here for a migration unit

1. `python scripts/check_reference_readiness.py <bundle>` — **run this FIRST**, before any building. It is the only ENTRY gate here: every other gate answers whether the work is *done*, so the "do I have a legible picture of the Tableau source?" question was only ever asked afterwards. Per page it reports completeness, evidence and **grade**. Exit 0 ready / 1 findings / 3 `CANNOT_ESTABLISH`, and neither 1 nor 3 is a pass — a blind page means an equivalent fidelity bug there is **structurally unfalsifiable**, not merely unverified. `NOT_APPLICABLE` (a datasource-only unit) is earned from the engine's own `report.json`, so it never blocks legitimately reference-free work.
2. `python scripts/check_unit.py <unit-or-bundle> --scope <model|report|integration|all>` — first status and final gate; direct `check_*.py` gates only isolate one finding.
3. `python scripts/read_handover.py <bundle> --workbook <name>` — residual work queue and engine-block reason.
4. `python scripts/credential_gate.py list <estate-root>` — which units are gated, across a whole estate (`--json` for an agent). Exit 1 = still blocked, so it is the resume signal after a human signs in. A credential caches machine-wide, so re-probe the blocked units to earn a `probe-cleared`; never mass-`authorize`, which stamps a build UNVALIDATED permanently. Detail: [`docs/credential-gate.md`](credential-gate.md).
5. Load the right skill before acting: `live-source-reachability` for live-source proof and
   credential-gate routing, `powerbi-semantic-model-gotchas` for TMDL/DAX,
   `powerbi-report-gotchas` for PBIR/visuals, `pbip-model-refresh` for cache/refresh, and
   `powerbi-ai-readiness` for Copilot/Q&A metadata.
6. `python scripts/promote_unit.py --package <package> --slug <slug> [--datasource-slug <ds>]` — **the last hop**: ship the finished unit from its phase-2 package to `migrations/{workbooks,datasources}/<slug>/fabric/`. It re-runs `check_unit.py` and refuses on a non-zero exit (`--force` overrides, and the override plus the observed exit code land in the promotion record), rewrites `definition.pbir` for a shared datasource, and then **resolves that `byPath` on disk** — `powerbi-report-author validate` returns `errorCount: 0` for a reference whose target exists nowhere. It also refuses a model that **reads data from outside the deliverable** (#461: 32 references across 26 of run 408's 62 packaged units point into the bundle's gitignored `data/`, and no other gate sees it), and refuses to ship **any** file carrying an absolute host path — the scan is keyed on the shipment, not on `*.tmdl`, and its shipped half allows only the deliverable roots, never the phase-2 package. Containment of every planned destination is judged on **resolved** paths, both sides (a lexical check passed a junctioned deliverable root and shipped outside the migrations tree at exit 0). Exit 0 promoted / 1 refused by the gate / 2 CANNOT_ASSESS / 3 refused on content / 4 promotion failed / 5 refused on an external data path / 6 refused on a host path in a shipped file / 64 usage. Shape rules: [`docs/migration-phases.md`](migration-phases.md) phase 3.

After a machine-wide Power BI sign-in, resume every still-blocked unit at once with `python scripts/reprobe_blocked.py` (dry-run by default; `--apply` to run). It re-probes each BLOCKED credential-gate unit and lets the gate earn its own `probe-cleared` where the probe now passes — it **never authorizes**, so it is the agent-safe counterpart to the human-only `credential_gate.py authorize`.

Toolkit-maintenance scripts live in `scripts/README.md`, outside the per-unit route.

## Retrospective targets

At the end of a migration, route durable learnings to the smallest permanent home that will be read
next time. Craft belongs in skills/docs/tests, not back in a persona unless the learning is
orchestration-specific.

| Learning about | Put it here |
|---|---|
| Every agent | `AGENTS.md` conventions block, then run `scripts/sync_agent_conventions.py` |
| Live-source reachability / credential gates | `.github/skills/live-source-reachability/SKILL.md` or `docs/credential-gate.md` |
| PBIR / visual / Desktop craft | `.github/skills/powerbi-report-gotchas/SKILL.md` |
| TMDL / DAX / modeling craft | `.github/skills/powerbi-semantic-model-gotchas/SKILL.md` |
| Refresh / cache or AI readiness | `.github/skills/pbip-model-refresh/SKILL.md` / `.github/skills/powerbi-ai-readiness/SKILL.md` |
| Orchestration or cross-agent process | `.github/agents/tableau-migrator.agent.md` `## Gotchas` |
| Tableau formula → DAX | `docs/tableau-dax-translation-guide.md` |
| A visual encoding that renders | `.github/pbi.kb/visual-cookbook.md` + `.github/pbi.kb/visuals/` |
| Parser/tooling behaviour | the script itself plus a regression test |
| Upstream engine behaviour | fresh empty-output run first; then upstream issue + credential-free reproducer |

If you edit a skill bundle that is also published, re-run `scripts/build_plugin.py` or preflight flags
the drift. If a learning is one-off or already caught by a gate, say "nothing worth recording" rather
than inventing durable knowledge.

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
| Understand run directory layout | [`_runs/README.md`](../_runs/README.md) | Map of canonical run directory stages, lifecycle, and self-contained package isolation. |
| Check third-party notices | [`THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md) | License/notice questions. |
| Handle conduct | [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md) | Conduct/community questions. |
| Handle security reports | [`SECURITY.md`](../SECURITY.md) | Security disclosure routing. |
| Find migration guidance | [`docs/agent-architecture.md`](../docs/agent-architecture.md) | Root sessions, subagents, skill visibility. |
| Operate delegated agents | [`docs/agent-operations.md`](../docs/agent-operations.md) | Monitoring a subagent's claims, host V8-heap and Desktop RAM concurrency budgets, and post-crash file forensics — the measured evidence behind `AGENTS.md`'s delegation rules. |
| Find migration guidance | [`docs/agent-capability-wiring.md`](../docs/agent-capability-wiring.md) | Registry of shipped capabilities that must be visible to agents. |
| Find migration guidance | [`docs/ai-instructions-authoring-guide.md`](../docs/ai-instructions-authoring-guide.md) | AI-instruction authoring redirect. |
| Find migration guidance | [`docs/capabilities-and-limitations.md`](../docs/capabilities-and-limitations.md) | Automation capability boundaries. |
| Find migration guidance | [`docs/credential-gate-testing.md`](../docs/credential-gate-testing.md) | Credential-gate tests and audit behavior. |
| Find migration guidance | [`docs/credential-gate.md`](../docs/credential-gate.md) | Live-source/Desktop credential wall. |
| Find migration guidance | [`docs/customer-text-exposure.md`](../docs/customer-text-exposure.md) | Customer text/privacy exposure. |
| Find migration guidance | [`docs/data-source-credentials.md`](../docs/data-source-credentials.md) | Tableau/database/warehouse credentials. |
| Find migration guidance | [`docs/deterministic-tier-integration.md`](../docs/deterministic-tier-integration.md) | Engine and handoff integration. |
| Find migration guidance | [`docs/dry-run-findings-2026-08-11.md`](../docs/dry-run-findings-2026-08-11.md) | Durable F00x dry-run findings. |
| Find migration guidance | [`docs/engine-gap-harvest.md`](../docs/engine-gap-harvest.md) | Attribute and classify the `reports/` vs `pbip/` delta into engine-gap evidence — provenance delegated to the `--tamper` gate's own adjudicator. |
| Find migration guidance | [`docs/migration-phases.md`](../docs/migration-phases.md) | The three-phase pipeline shape: what each phase produces, where it lands, which gate guards each hop, the `_runs/<NNN>-<slug>` identity rule (never renamed/renumbered; one run per pipeline run, **not** per workbook), and the known gaps. |
| Find migration guidance | [`docs/migration-programme.md`](../docs/migration-programme.md) | Programme phases and operating model. |
| Find migration guidance | [`docs/migration-spec.md`](../docs/migration-spec.md) | `migration-spec.json` contract. |
| Find migration guidance | [`docs/operator-runbook.md`](../docs/operator-runbook.md) | Manual pipeline operation, the failure playbook, and §4.12 **Windows/PowerShell misreads** — `NativeCommandError` on a command that exited 0, long-path `Remove-Item`, `Start-Process -ArgumentList` mangling a multi-word `--workbook`, and `requests`+certifi failing TLS where the toolkit's `urllib` + Windows certificate store succeeds. |
| Test the offline deployment rehearsal | [`docs/offline-mock-harness.md`](offline-mock-harness.md) | Offline Tableau-to-Fabric mock harness and fidelity boundary. |
| Run the test suite | [`docs/parallel-test-loop.md`](parallel-test-loop.md) | Fast parallel loop vs the serial pre-PR gate, and the `serial` marker. |
| Find migration guidance | [`docs/reference-capture.md`](../docs/reference-capture.md) | Tableau reference capture and evidence grading. |
| Find migration guidance | [`docs/reference-readiness.md`](../docs/reference-readiness.md) | The ENTRY gate: is there enough visual evidence to START, per page, with its grade. |
| Migrate ONE workbook by hand | [`docs/start-with-one-workbook.md`](../docs/start-with-one-workbook.md) | Bring-your-own `.twb` + screenshots, no Tableau Server: the verified command sequence, the `tableau-<exact name>.png` naming contract, and the Desktop-connection prerequisite. |
| Find migration guidance | [`docs/review-remediation-plan.md`](../docs/review-remediation-plan.md) | Route validation/review findings. |
| Improve review throughput | [`docs/review-throughput-postmortem.md`](../docs/review-throughput-postmortem.md) | Measured 2026-09-01/02 review-round costs, causes, counterexamples, and the replacement brief contract. |
| Find migration guidance | [`docs/tableau-dax-translation-guide.md`](../docs/tableau-dax-translation-guide.md) | Translate Tableau calcs/LODs/table calcs. |
| Find migration guidance | [`docs/tableau-map-to-azuremaps.md`](../docs/tableau-map-to-azuremaps.md) | Translate Tableau maps to Azure Maps. |
| Find migration guidance | [`docs/windows-path-limits.md`](../docs/windows-path-limits.md) | Windows MAX_PATH ceilings a shipped bundle must respect — and how to DELETE a tree that is already over one (`\\?\` prefix plus a chmod handler, because git objects are read-only). |
| Find migration guidance | [`docs/upstream-issue-gate.md`](../docs/upstream-issue-gate.md) | Route engine issues upstream vs local. |
| Invoke subagent | [`.github/agents/dry-run-operator.agent.md`](../.github/agents/dry-run-operator.agent.md) | Full customer-shaped pipeline dry-run persona; read-only against the toolkit. |
| Invoke subagent | [`.github/agents/pbi-migration-validator.agent.md`](../.github/agents/pbi-migration-validator.agent.md) | Read-only fidelity validator persona. |
| Invoke subagent | [`.github/agents/pbi-report-builder.agent.md`](../.github/agents/pbi-report-builder.agent.md) | PBIR/report repair persona. |
| Invoke subagent | [`.github/agents/pbi-semantic-builder.agent.md`](../.github/agents/pbi-semantic-builder.agent.md) | TMDL/DAX/model repair persona. |
| Invoke subagent | [`.github/agents/tableau-migrator.agent.md`](../.github/agents/tableau-migrator.agent.md) | Workbook/datasource migration worker persona. |
| Use fixture | [`fixtures/large-refresh/README.md`](../fixtures/large-refresh/README.md) | Large refresh/cache behavior fixture. |
| Use fixture | [`tests/fixtures/check-gates-dirty/README.md`](../tests/fixtures/check-gates-dirty/README.md) | Dirty gate golden-output fixture. |
| Use fixture | [`tests/fixtures/connection-fidelity/README.md`](../tests/fixtures/connection-fidelity/README.md) | Silent live-source-to-flat-file downgrade fixture (issue #328). |

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
check_connection_fidelity.py
check_datamodel.py
check_desktop_orphans.py
check_empty_model.py
check_engine_receipts.py
check_field_bindings.py
check_identity_normalization.py
check_m_syntax.py
check_migration_progress.py
check_navigation_index.py
check_path_ceiling.py
check_pbir_layout.py
check_pbir_valid.py
check_reference_readiness.py
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
fixtures/upstream-repros/issue-424-automatic-mark-discrete-date/README.md
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
.github/skills/live-source-reachability/SKILL.md
.github/skills/pbip-model-refresh/SKILL.md
.github/skills/powerbi-ai-readiness/SKILL.md
.github/skills/powerbi-report-gotchas/SKILL.md
.github/skills/powerbi-semantic-model-gotchas/SKILL.md
```
