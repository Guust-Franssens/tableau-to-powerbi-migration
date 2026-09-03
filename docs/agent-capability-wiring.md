# Agent capability wiring registry

This registry is the durable prompt to wire shipped capabilities into agent-reachable guidance. Skills remain the authoritative documentation home: a capability belongs in a skill when it is **how** to do the work. A persona carries **when** to do it, what verdict is required, and when to route to the skill. This registry is for cases where an agent-facing instruction would otherwise omit or contradict a capability the agent needs at that moment. When that happens, add a row here with the load-bearing token, the owning agent, and suggested wording for the human fixing it, then run `python scripts/check_agent_capabilities.py`.

The gate does **not** count this registry as wiring. It scans only agent-reachable files, strips fenced code blocks and HTML comments, and requires the token below in visible prose in the named file. The suggested wording is guidance for failures, not a verbatim string to match.

It also derives the runnable script capability inventory from tracked `scripts/*.py` files whose module
docstring declares a `usage:` line. Every such script must either be named in agent-reachable script
guidance (`AGENTS.md`, `docs/INDEX.md`, `scripts/README.md`, or an agent persona) or explicitly mark
itself as internal with both `internal: true` and an `internal-reason:` line in that docstring. This
keeps three states distinct: if the script scan section below is absent the scan was **not evaluated**;
if it is present and no script is missing wiring, the discovered missing set is present-but-empty; if
it is present and scripts are missing, the gate names them.

## Capability registry

| Token | Why it exists | Agent that needs it | Reachable in | Suggested agent-facing wording |
|---|---|---|---|---|
| `check_reference_readiness.py` | Issue #421: every other gate here answers whether the work is **done**, so nobody ever asked whether there was enough visual evidence to **start**. An agent that begins with no picture of the Tableau source builds confidently against nothing, and the gap is self-concealing — wherever a capture gap exists, an equivalent fidelity bug is *structurally unfalsifiable*, not merely unverified. | `tableau-migrator` | `docs/INDEX.md` | Run `python scripts/check_reference_readiness.py <bundle>` BEFORE dispatching any builder. Exit 1 (findings) and exit 3 (`CANNOT_ESTABLISH`) are both non-starts: capture the missing references, or record the blind pages in `limitations_encountered` and say plainly that fidelity there cannot be judged. Its per-page grade is the ceiling on any later visual claim — `layout/text only` never supports a fidelity sign-off. |
| `pbip-model-refresh skill` | The refresh mechanics, flags such as `--calculate-only`, and pid-binding rule live in the skill; the semantic-builder persona must route refresh handoff there instead of carrying a stale command copy. | `pbi-semantic-builder` | `.github/agents/pbi-semantic-builder.agent.md` | Use the pbip-model-refresh skill for the command, flags, pid-binding rule and save mechanics; then require `REFRESH: DATA_OK + PERSISTED`. |
| `NOT_CHECKED` | `check_unit` now has a third outcome; missing inputs previously produced false green runs, including PASS on an empty model directory. | `pbi-migration-validator` | `.github/agents/pbi-migration-validator.agent.md` | `NOT_CHECKED` is not a pass: in `SUMMARY`, `not_checked_structural` means no artifact can exist for that scoped check, while `not_checked_missing_input` means this run lacked an expected input and you may be pointed at the wrong target. |
| `BROWNFIELD DISCOVERY` | Customer migrations may already exist in non-canonical folders; the orchestrator must route discovered artifacts instead of restarting work. | `tableau-migrator` | `.github/agents/tableau-migrator.agent.md` | When `check_unit` prints `BROWNFIELD DISCOVERY`, treat it as read-only artifact discovery: it found engine output by content, not path, and the expected/found-instead block is the path forward before redoing work. |
| `--scope` | Layer owners need scoped `check_unit` gates without mistaking a scoped pass for full sign-off. | all personas | `docs/INDEX.md` | first status and final gate; direct `check_*.py` gates only isolate one finding. |
| `read_handover` | Builders need the compact residual-work queue instead of re-reading large engine handover JSON by hand. | model and report builders | `docs/INDEX.md` | `python scripts/read_handover.py <bundle> --workbook <name>` — residual work queue and engine-block reason. |
| `desktop-orphans` | Run-owned Power BI Desktop processes must be cleaned up without killing sibling work. | all personas | `AGENTS.md` | leaks are enforced by `check_unit.py`'s `desktop-orphans` gate. |
| `reprobe_blocked.py` | After a machine-wide sign-in an agent has no cheap way to resume: it cannot `authorize` (agent-hostile by design), and re-probing dozens of gated units by hand is the friction that invites a bypass. This is the agent-SAFE resume path — probe-only, never authorizes, so it converts unearned→earned by measurement without any human-only step. | `tableau-migrator` | `docs/INDEX.md` | After a machine-wide sign-in, resume with `python scripts/reprobe_blocked.py` (dry-run; `--apply` to run) — re-probes every BLOCKED unit and earns each gate's own clear; it never authorizes. |
| `credential_gate.py list` | Every other gate subcommand takes exactly one migration, so "which units are still gated?" had no answer across an estate. Measured 2026-08-26 on a ~44-unit estate: a human hand-typed `authorize` per unit and so took the lossy exit for all of them, permanently marking builds UNVALIDATED that a re-probe *might* have earned (whether it would depends on post-sign-in reachability, which was never measured). An agent had no resume signal at all after a human signed in. | all personas | `docs/INDEX.md` | `python scripts/credential_gate.py list <estate-root>` names every gated unit (`--json`); exit 1 means work remains. After a sign-in re-probe the blocked units to earn `probe-cleared` — a credential caches machine-wide, so one sign-in can clear several. Never mass-`authorize`. |

## Script capability inventory

The gate owns this inventory mechanically rather than as another hand-maintained table. This heading is
the "script scan was evaluated" marker. Delete it and the gate fails closed instead of treating an
absent scan as an empty result. Every tracked `scripts/*.py` file with a module-docstring `usage:` line
must appear below unless the script docstring marks it `internal: true` with an `internal-reason:`.

| Script | Status | Reason |
|---|---|---|
| `scripts/assess_estate.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/build_plugin.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/build_reconcile_items.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/build_synthetic_reference.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/bundle_corpus.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/capture_powerbi_pages.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/capture_tableau_oracle.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/capture_tableau_reference.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_agent_capabilities.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_ai_readiness.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_blank_placeholders.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_connection_fidelity.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_datamodel.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_desktop_orphans.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_empty_model.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_engine_receipts.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_field_bindings.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_identity_normalization.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_m_syntax.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_migration_progress.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_navigation_index.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_path_ceiling.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_pbir_layout.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_pbir_valid.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_reference_readiness.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_relationship_health.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_sqlproxy_connections.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_stub_measures.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/check_unit.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/classify_harvest_hardness.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/connection_target.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/connections_manifest.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/credential_gate.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/dax_oracle_server.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/declare_generated_edit.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/deploy_estate.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/derive_connection_templates.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/detect_occlusion.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/engine_source.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/extract_hyper_data.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/extract_twb_thumbnails.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/extract_twbx_result_cache.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/generated_edit_declarations.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/group_oracle_by_workbook.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/harvest_engine_gaps.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/harvest_gap_report.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/harvest_gap_shapes.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/harvest_gap_trees.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/harvest_estate_assets.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/harvest_tableau_public.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/hooks/credential_gate.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/make_carousel.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/make_live_source_fixture.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/make_refresh_fixture.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/make_seed_workbook.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/make_showcase.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/migration_bundle.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/migration_cost_report.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/object_identity.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/parse_tableau.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/preflight_source_credentials.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/probe_bundle.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/probe_desktop_query.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/probe_live_source.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/provision_tableau_estate.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/published_datasource_registry.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/read_handover.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/reference_evidence.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/refresh_pbip_model.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/render_excalidraw.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/reprobe_blocked.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/run_engine_survey.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/run_estate.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/set_ai_instructions.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/set_data_folder.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/skill_plugin_source.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/stamp_tableau_provenance.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/sync_agent_conventions.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/sync_engine_plugin.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/sync_installed_skills.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/tableau_capture_policy.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/tableau_env.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/tableau_http.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/tableau_lineage.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/tableau_oracle_manifest.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/tableau_payload_facts.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/tableau_render_capability.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/tableau_luid_census.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/tableau_view_types.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/trace_customer_text.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/transpile_tableau_calc.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/validate_spec.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/verify_bindings.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |
| `scripts/work_dirs.py` | `agent-facing` | Named in `scripts/README.md`, which `docs/INDEX.md` routes agents to for script selection. |

## Follow-up: generated flag inventory

This curated list is intentionally small and will lag the full script surface. A future PR should enumerate `argparse` flags from `scripts/`, then require each shipped flag to be either agent-reachable or explicitly marked internal with a reason, using the same reasoned-exclusion shape as `docs/INDEX.md`.
