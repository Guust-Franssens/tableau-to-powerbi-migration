# Agent capability wiring registry

This registry is the durable prompt to wire shipped capabilities into agent-reachable guidance. Skills remain the authoritative documentation home: a capability belongs in a skill when it is **how** to do the work. A persona carries **when** to do it, what verdict is required, and when to route to the skill. This registry is for cases where an agent-facing instruction would otherwise omit or contradict a capability the agent needs at that moment. When that happens, add a row here with the load-bearing token, the owning agent, and suggested wording for the human fixing it, then run `python scripts/check_agent_capabilities.py`.

The gate does **not** count this registry as wiring. It scans only agent-reachable files, strips fenced code blocks and HTML comments, and requires the token below in visible prose in the named file. The suggested wording is guidance for failures, not a verbatim string to match.

It also gives every tracked `scripts/*.py` and `scripts/*.ps1` file exactly one tier below. **Core**
scripts are the ordinary migration route and must be named in `AGENTS.md` or `docs/INDEX.md`.
**Advanced** scripts must not be named in `AGENTS.md`, `.github/copilot-instructions.md` or
`docs/INDEX.md`; personas and skills may name them. **Internal** scripts are also kept out of personas.
These script rules ignore HTML comments but read fenced code, because agents read fences.

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

## Script capability inventory

This heading is the "script scan was evaluated" marker: delete it and the gate fails closed. Reasons
appear only where the tier is not obvious. `scripts/README.md` remains the advanced reference.

| Script | Tier | Reason |
|---|---|---|
| `scripts/preflight.ps1` | `core` | |
| `scripts/t2p.py` | `core` | Opt-in pilot (`docs/t2p-pilot-plan.md`); direct calls stay authoritative. |
| `scripts/start_migration.py` | `core` | |
| `scripts/package_unit.py` | `core` | |
| `scripts/set_data_folder.py` | `core` | |
| `scripts/check_reference_readiness.py` | `core` | |
| `scripts/read_handover.py` | `core` | |
| `scripts/declare_generated_edit.py` | `core` | |
| `scripts/check_unit.py` | `core` | |
| `scripts/promote_unit.py` | `core` | |
| `scripts/assess_estate.py` | `advanced` | |
| `scripts/build_migration_feedback.py` | `advanced` | Run by the migration-feedback skill. |
| `scripts/build_plugin.py` | `advanced` | |
| `scripts/build_reconcile_items.py` | `advanced` | |
| `scripts/build_synthetic_reference.py` | `advanced` | |
| `scripts/capture_powerbi_pages.py` | `advanced` | |
| `scripts/capture_tableau_oracle.py` | `advanced` | |
| `scripts/capture_tableau_reference.py` | `advanced` | |
| `scripts/check_agent_capabilities.py` | `advanced` | CI gate for this registry. |
| `scripts/check_ai_readiness.py` | `advanced` | |
| `scripts/check_blank_placeholders.py` | `advanced` | |
| `scripts/check_bundle_sources.py` | `advanced` | |
| `scripts/check_connection_fidelity.py` | `advanced` | |
| `scripts/check_datamodel.py` | `advanced` | |
| `scripts/check_desktop_orphans.py` | `advanced` | |
| `scripts/check_empty_model.py` | `advanced` | |
| `scripts/check_engine_receipts.py` | `advanced` | |
| `scripts/check_field_bindings.py` | `advanced` | |
| `scripts/check_identity_normalization.py` | `advanced` | |
| `scripts/check_m_syntax.py` | `advanced` | Compatibility alias. |
| `scripts/check_migration_progress.py` | `advanced` | Orchestrator diagnostic that personas name. |
| `scripts/check_navigation_index.py` | `advanced` | CI gate for `docs/INDEX.md`. |
| `scripts/check_path_ceiling.py` | `advanced` | |
| `scripts/check_pbir_layout.py` | `advanced` | |
| `scripts/check_pbir_valid.py` | `advanced` | |
| `scripts/check_relationship_health.py` | `advanced` | |
| `scripts/check_sqlproxy_connections.py` | `advanced` | |
| `scripts/check_stub_measures.py` | `advanced` | |
| `scripts/classify_harvest_hardness.py` | `advanced` | |
| `scripts/connections_manifest.py` | `advanced` | |
| `scripts/dax_oracle_server.py` | `advanced` | |
| `scripts/deploy_estate.py` | `advanced` | Deploy only by separate, explicit agreement. |
| `scripts/derive_connection_templates.py` | `advanced` | |
| `scripts/detect_occlusion.py` | `advanced` | |
| `scripts/disarm_credential_gate.py` | `advanced` | |
| `scripts/engine_source.py` | `advanced` | |
| `scripts/extract_hyper_data.py` | `advanced` | |
| `scripts/extract_twb_thumbnails.py` | `advanced` | |
| `scripts/extract_twbx_result_cache.py` | `advanced` | |
| `scripts/group_oracle_by_workbook.py` | `advanced` | |
| `scripts/harvest_engine_gaps.py` | `advanced` | |
| `scripts/harvest_estate_assets.py` | `advanced` | |
| `scripts/harvest_tableau_public.py` | `advanced` | |
| `scripts/make_carousel.py` | `advanced` | |
| `scripts/make_live_source_fixture.py` | `advanced` | |
| `scripts/make_refresh_fixture.py` | `advanced` | |
| `scripts/make_seed_workbook.py` | `advanced` | |
| `scripts/make_showcase.py` | `advanced` | |
| `scripts/migration_cost_report.py` | `advanced` | |
| `scripts/parse_tableau.py` | `advanced` | |
| `scripts/preflight_source_credentials.py` | `advanced` | |
| `scripts/probe_desktop_query.py` | `advanced` | Skill shim. |
| `scripts/provision_tableau_estate.py` | `advanced` | |
| `scripts/published_datasource_registry.py` | `advanced` | |
| `scripts/refresh_pbip_model.py` | `advanced` | |
| `scripts/render_excalidraw.py` | `advanced` | |
| `scripts/resolve_datasource_target.py` | `advanced` | |
| `scripts/run_engine_survey.py` | `advanced` | |
| `scripts/run_estate.py` | `advanced` | |
| `scripts/run_status.py` | `advanced` | Reached through `t2p status`. |
| `scripts/set_ai_instructions.py` | `advanced` | |
| `scripts/skill_plugin_source.py` | `advanced` | |
| `scripts/stamp_tableau_provenance.py` | `advanced` | |
| `scripts/sync_agent_conventions.py` | `advanced` | |
| `scripts/sync_engine_plugin.py` | `advanced` | |
| `scripts/sync_installed_skills.py` | `advanced` | |
| `scripts/tableau_lineage.py` | `advanced` | |
| `scripts/tableau_luid_census.py` | `advanced` | |
| `scripts/tableau_render_capability.py` | `advanced` | |
| `scripts/trace_customer_text.py` | `advanced` | |
| `scripts/transpile_tableau_calc.py` | `advanced` | |
| `scripts/update_playbook_plugin.ps1` | `advanced` | |
| `scripts/validate_spec.py` | `advanced` | |
| `scripts/verify_bindings.py` | `advanced` | |
| `scripts/work_dirs.py` | `advanced` | |
| `scripts/admin_insights_collector.py` | `internal` | Invoked only as the third child of the assessment-only front door. |
| `scripts/assessment_report.py` | `internal` | Invoked only as the fourth child of the assessment-only front door. |
| `scripts/bundle_corpus.py` | `internal` | |
| `scripts/connection_target.py` | `internal` | |
| `scripts/copilot_home.py` | `internal` | Shared path-only library for toolkit consumers. |
| `scripts/current_artifact_revision.py` | `internal` | |
| `scripts/engine_input_staging.py` | `internal` | Imported by `run_estate.py`; deleted at #836 S4. |
| `scripts/generated_edit_declarations.py` | `internal` | |
| `scripts/harvest_gap_report.py` | `internal` | |
| `scripts/harvest_gap_shapes.py` | `internal` | |
| `scripts/harvest_gap_trees.py` | `internal` | |
| `scripts/host_paths.py` | `internal` | |
| `scripts/issue_intake_guard.py` | `internal` | Actions-only helper for `.github/workflows/issue-intake.md`, not a migration command. |
| `scripts/iteration_receipt.py` | `internal` | |
| `scripts/manifest_scope.py` | `internal` | |
| `scripts/migration_bundle.py` | `internal` | |
| `scripts/object_identity.py` | `internal` | |
| `scripts/package_data_access.py` | `internal` | |
| `scripts/package_filesystem.py` | `internal` | |
| `scripts/package_role_identity.py` | `internal` | |
| `scripts/package_source.py` | `internal` | Library; only the readiness check consumes this projection. |
| `scripts/path_flavour.py` | `internal` | |
| `scripts/probe_desktop_credential.ps1` | `internal` | Retired; always refuses. |
| `scripts/prompt_injection.py` | `internal` | |
| `scripts/reference_evidence.py` | `internal` | |
| `scripts/tableau_capture_policy.py` | `internal` | |
| `scripts/tableau_env.py` | `internal` | |
| `scripts/tableau_http.py` | `internal` | |
| `scripts/tableau_oracle_manifest.py` | `internal` | |
| `scripts/tableau_payload_facts.py` | `internal` | |
| `scripts/tableau_view_types.py` | `internal` | |
| `scripts/tmdl_checks.py` | `internal` | Library; agents run `check_datamodel.py`, which re-exports it. |
| `scripts/tmdl_oracle.py` | `internal` | Library; agents run `check_datamodel.py`, which carries this gate. |
| `scripts/unit_ids.py` | `internal` | Library behind `t2p units`. |

## Follow-up: generated flag inventory

This curated list is intentionally small and will lag the full script surface. A future PR should enumerate `argparse` flags from `scripts/`, then require each shipped flag to be either agent-reachable or explicitly marked internal with a reason, using the same reasoned-exclusion shape as `docs/INDEX.md`.
