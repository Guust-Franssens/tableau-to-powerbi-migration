---
name: powerbi-semantic-model-gotchas
description: Tableau-migration semantic core for a host repository: source calculations, request roles, visual grain and model/report handoff. Use for translated-model work, not generic TMDL/DAX or greenfield authoring. Host tooling is not bundled; general procedures belong to installed semantic-model-authoring.
---

# Tableau migration semantic core

## 1. Translating source fields

Host-repo only: `docs\tableau-dax-translation-guide.md` owns source translation patterns; load the relevant section. Relative docs/scripts/local owners require the host checkout, not just this plugin. An unavailable owner blocks only the affected migration operation; never invent a replacement or treat absence as success.

Preserve source semantics, including first-match branches and ATTR's actual grain; flag suspected source defects rather than silently correcting them. Use resolved IDs/captions, not stale internal names.

Keep the source's column/measure role, target table, parameter meaning, units and independent-source relationships. Parameter equality is not automatically a numeric what-if control. Relationship anchors are not real data columns; spatial expressions require an explicit supported representation/gap, not a forced scalar column (§8 of the translation guide).

## 2. TMDL hand-authoring pitfalls

Portable upstream procedures: installed `semantic-model-authoring` existing-model workflow and `references/tmdl-guidelines.md` own generic TMDL/DAX, relationship, parameter and integrity mechanics. For report-side parameters use `powerbi-report-cli`'s `references/authoring/field-parameters.md` when needed. Missing topic blocks that operation, not permission to DIY.

Until fresh S resolves both parameter topics, keep this constructor guard: bracketed `sourceColumn` `[Value1]`/`[Value2]`/`[Value3]`, grouping/`ParameterMetadata` and first-open bindings. A clean parse cannot prove those bindings survived Desktop's first open.

Host `python scripts\check_datamodel.py <SemanticModel>` owns repository model checks, including name uniqueness; a clean result does not establish source values, live rows or runtime identity.

## 3. MCP / Desktop operational gotchas

Use the explicit returned `connectionName` on each supported operation; independently check model contents/current definitions against the intended working model. Implicit “last connection,” echoed connection metadata or a plausible sibling model is not target identity. Never save stale memory over newer working files or discard unexported work to clear a refresh problem.

Refresh/persist/export/reopen mechanisms belong to host `.github\skills\pbip-model-refresh\SKILL.md` (“Order matters” and “Bind to the right instance”), held out of the playbook. Resolve that owner path in the fresh role or block the dependent handoff; do not assume a registered plugin skill.

Until #730 establishes a measured replacement, local custody/persist/export rules outrank generic upstream claims of desync-free live MCP changes or no need for explicit persistence. Preserve explicit export/persistence/current-definition checks and unresolved reload/cold-reopen safeguards; no unproved workaround retires.

Only for wrong-model reads, stale-save conflicts or empty reopen: load the relevant owner section or host-only `docs\gotcha-dedup-plan.md` symptom index. If the index is absent, current attribution is unestablished. Optional pinned history is not a portable owner or a universal ban on current preview/reload; newer versions/mtime cannot settle #730.

## 4. Model integrity, table calcs, cross-agent hand-offs, and scale

Validate translated LOD/table calculations at the report's actual axis, partition, ordering, filters and manual-sort grain (translation guide §§5–6). Prefer the engine's visual-calculation route for layout-sensitive addressing; a model measure needs explicit grain agreement. A correct result at another date grain is insufficient. Compare real model queries to authorized Tableau results; Python replicas/metadata are not independent source or live-row proof.

Coordinate source-required sort keys, bins, geographic grain, projected summary fields and helper measures with the report owner before handoff. Do not invent shared relationships between independent sources, silently change fraction/percent scales, or regenerate a scaffold over authored report/AI work. Land approved calculation changes through the orchestrator's existing route.

For live connectors resolve/invoke host `.github\skills\live-source-reachability\SKILL.md`; it alone owns credential routing/shipped-artifact live proof and is not shipped by this playbook. Missing owner blocks that operation. For AI invoke locally owned, shipped `powerbi-ai-readiness` §0; its owned stamp/batch/strict final seal is unchanged, not replaced by generic upstream AI guidance.

The host handoff gate/refresh owner require current-model row canaries for every commissioned import/live table and commissioned persistence/cold-reopen checks. One arbitrary row, `PERSISTED`, cache/mtime, a screenshot or a successful connection cannot prove table coverage or durability. Route PBIR findings to the report owner; never hand an unproved model to report repair.

Missing, stale, inaccessible or ambiguous evidence → `CANNOT_ESTABLISH`/BLOCKED; observed mismatch → REFUSE/finding. Neither becomes PASS. Credential stops retain their owner's vocabulary.

## 8. Reading the handover queue

Use `python scripts\read_handover.py <bundle> --workbook <name>`. Repair from `requests[]` with formula/role/target table, not the incomplete `needs_review[]` summary. Keep requests, measure stubs and calculated-column stubs as distinct denominators; never silently drop a remaining object.

`openability_selfcheck.ok` is the engine's claim, not an observed Desktop open or source match. Preserve unresolved/approved-deferral provenance, report actual object fates and limitations, and hand the exact shipped model/evidence to the independent validator.
