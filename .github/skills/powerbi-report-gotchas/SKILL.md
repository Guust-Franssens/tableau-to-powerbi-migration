---
name: powerbi-report-gotchas
description: Tableau-migration report core for a host repository: source grain/defaults, shipped-model binding and layer handoff. Use for migrated-report repair/fidelity, not generic PBIR or greenfield work. Host tooling is not bundled; general procedures belong to installed powerbi-report-cli.
---

# Tableau migration report core

## Owners and reachability

Portable upstream procedures: installed `powerbi-report-cli`, existing report → `authoring`; missing page → `design` then `authoring`. Greenfield leaves this core for the upstream planning/explicit-approval contract. Modes select `references/<mode>.md`, not CLI flags. Missing topic/owner blocks the affected operation to the orchestrator; do not install or improvise.

Choose only the needed authoring topics: `references/authoring/expressions.md`, `references/authoring/conditional-formatting.md`, `references/authoring/field-parameters.md`, `references/authoring/slicers-part-02.md`, `references/authoring/map.md`, `references/authoring/model-binding.md`, and `references/authoring/screenshot-review.md`. They own visual encodings, schemas, map selection, preview and capture procedures. Do not load a cookbook or incident archive by default.

Host-repo only: `AGENTS.md`, `scripts\...`, local runtime/declaration owners and `docs\...` below require the migration checkout; they are not portable plugin payload. If absent, block only the dependent migration operation. Optional `docs\gotcha-dedup-plan.md` is a host-only symptom index: if unavailable, historical/current attribution is unestablished; pinned history is not a current procedure.

## 3. PBIR mechanics

Repair the canonical working report (`pbip` or package `fabric`), never pristine `reports`; that pass can differ by design and is not shippable evidence. Host `AGENTS.md` owns custody.

Keep `definition.pbir` bound to the intended shipped semantic model: per-workbook promotion copies its model; shared-datasource promotion rewrites `byPath`. Check the resolved target after promotion, not just the JSON shape. Do not substitute a sibling model or ship the unbound baseline.

Resolve every encoded table/property, measure home table and filter source against that intended shipped model; use the host's existing field-binding/integration gate where available. Valid JSON/reference shape or a plausible sibling model is insufficient.

Bundle PBIR changes must be replay-scripted and declared through the host route (`scripts\declare_generated_edit.py`), not hand-edit-first; require `--tamper` exit 0. Package edits follow their package contract, not bundle-only declaration/tamper rules.

Edit PBIR only; route missing columns/measures, sort keys and parameter tables to the semantic owner. Do not rerun a scaffold over finished work. Report authoring grants no refresh, semantic repair, publishing or service-rebind authority.

## 8. Reading the source spec

Use resolved field IDs/captions and the commissioned reference manifest. Unresolved IDs or empty `pivoted_field_ids` are gaps to route, not names/fields to guess. For Measure Names/Values, bind the resolved real fields rather than inventing a literal virtual pivot.

Preserve source mark type, shelf nesting, partition/address grain, aggregation, units/percent scale, tooltip intent and manual sort. Do not sum ranks/averages merely because a stacked chart is available. A multi-field grouping must render at the source grain; a structurally valid top-level-only result is not equivalent. Request model helpers from their owner.

Verify sampled values/scale using the existing real-model query route (host `scripts\probe_desktop_query.py` or the available read-only query tool) against the intended shipped model and authorized source results; formatting alone is not numeric agreement.

Set and verify the source's effective first-open slicer/parameter selection, not merely the offered item set. Follow the current upstream selection recipe and observe the shipped result; do not invent a convenient default.

For ambiguous Automatic marks, inspect permitted source images/shelves. Thumbnails have only their declared evidence ceiling; they do not automatically supply pixel or numeric validation. Engine “rebuilt” and successful structural checks do not establish fidelity.

## 10. Reading the report-side handover queue

Read `python scripts\read_handover.py <bundle> --workbook <name> --viz`. Inspect emptied/dropped bindings, remediation and `viz_fidelity`, not just successful emissions. Apply the validator's routing; do not silently reverse an accepted semantic deferral.

In particular, a post-aggregation LAST filter hides marks: replacing it with a row filter can change other table calculations. Route that decision jointly with the semantic owner. Compare the final shipped report's whole layout, every commissioned visual and required values against the authorized source evidence; return gaps to the independent validator, not a self-issued COMPLETE.

Pending measured #730 results, the host's supported capture/reload/identity route outranks generic upstream host/`--pid` prohibitions or reload assumptions; this is not permission to invent a bypass. Refresh/persistence stays with host `.github\skills\pbip-model-refresh\SKILL.md`, not report repair. Missing supported route blocks the affected step. Load diagnostics only for a symptom; an unreachable/skipped schema check is unverified, not a pass.

Missing, stale, inaccessible or ambiguous evidence → `CANNOT_ESTABLISH`/BLOCKED; observed mismatch → REFUSE/finding. Neither becomes PASS. Credential stops retain the live-source owner's vocabulary. Return evidence/gaps to the independent validator; never self-issue COMPLETE.
