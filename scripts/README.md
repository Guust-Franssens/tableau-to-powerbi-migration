# `scripts/` — what each one is for

23 files is a lot to scan, so this is the index. **Every tracked file in this folder appears below**,
and `tests/test_repo_layout.py` fails if one is missing — an undocumented script is one nobody can
find, which is how five of these ended up unreferenced before this file existed.

Conventions: Python first (`.ps1` only where Windows-specific APIs make it unavoidable), a
`purpose:` / `usage:` header docstring in every file, `ruff` + `pylint` clean. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

## Run every session / every migration

| Script | What it does | When |
|---|---|---|
| `preflight.ps1` | Verifies the whole toolchain: Python + parser deps, **both skill plugins** and whether the published bundles still match `.github/skills/`, MCP servers, Power BI Desktop + Bridge CLI, the npm CLI version matrix. PowerShell on purpose — it must run before Python exists, since checking *for* Python is one of its jobs. | `-Update` at **session start**; plain at **migration start**; never mid-migration |
| `parse_tableau.py` | The deterministic parser: `.twb`/`.twbx`/`.tds`/`.tdsx` → `migration-spec.json`, validated against `docs/migration-spec.schema.json`. The contract every downstream agent reads. | Once per workbook, before any agent work |

## Migration pipeline

| Script | What it does | Called by |
|---|---|---|
| `preflight_source_credentials.py` | Flags live-database sources that need a credential **a human must supply**, before a build starts and stalls on it. | `tableau-migrator` step 5 |
| `published_datasource_registry.py` | Matches a workbook's `published_datasource.key` against already-migrated data sources, so one shared Tableau datasource becomes **one** semantic model with many reports bound to it — not N near-identical models. | `tableau-migrator` step 4 |
| `tableau_lineage.py` | Queries the Tableau Metadata API for `publishedDatasources { downstreamWorkbooks }` and prints a model-first, two-phase migration plan for a whole estate. `--download` pulls each `.tdsx`. | `tableau-migrator` step 1 (estate migrations) |
| `extract_hyper_data.py` | Materializes `.hyper` extract data to CSV so a migrated model has real rows. Extracts have no live connection — this is the only honest alternative to fabricating data. | `pbi-semantic-builder` |
| `connection_target.py` | Resolves what a Power Query partition should actually connect to (extract folder vs live source). | `pbi-semantic-builder`, CI gate |
| `set_data_folder.py` | Points a model's `DataFolder` at the local extract path; `--sanitize` rewrites it to a portable form before committing. **Invalidates `cache.abf`** (it rewrites TMDL), so run it *before* the final refresh. | `pbi-semantic-builder` |
| `capture_tableau_reference.py` | Acquires a **provenance-stamped** reference image of the source dashboard, so the builder can mimic it and the validator can grade against immutable ground truth. Providers resolved by fitness, not availability. Design: [`docs/reference-capture.md`](../docs/reference-capture.md). | `tableau-migrator`, `pbi-migration-validator` |

## Forwarding shims into skill bundles

These four are **four-line `runpy` shims**. The real scripts live in the skill bundle that owns them,
so the bundle stays copy-one-folder portable; the shims keep the short `python scripts/…` paths the
personas already use. Deleting them would make every persona *longer* — see
[`CONTRIBUTING.md`](../CONTRIBUTING.md). `tests/test_skills.py` proves each shim still reaches its target.

| Shim | Forwards to |
|---|---|
| `refresh_pbip_model.py` | `.github/skills/pbip-model-refresh/` — refresh a PBIP and persist it to `.pbi/cache.abf` |
| `probe_desktop_query.py` | `.github/skills/pbip-model-refresh/` — one-row DAX probe against Desktop's local AS |
| `set_ai_instructions.py` | `.github/skills/powerbi-ai-readiness/` — stamp `CustomInstructions`, force `qnaEnabled` |
| `check_ai_readiness.py` | `.github/skills/powerbi-ai-readiness/` — audit description coverage + enumerated domains |

## Gates (CI and agent Definition of Done)

| Script | What it enforces |
|---|---|
| `check_m_syntax.py` | Power Query M syntax across generated models — catches breakage that TMDL deserialization does not. |
| `sync_agent_conventions.py` | Regenerates the shared-conventions block into all four personas; `--check` fails on drift **and prints each persona's size against the 30,000-char cap**. All four currently sit at ~99%, so this is the budget alarm. |
| `probe_desktop_credential.ps1` | Whether a Desktop credential is already cached (`CREDENTIAL_PRESENT`/`_MISSING`), so agents prompt only when needed. The 1-row data probe outranks it as the gate of record. |

## Toolkit maintenance

| Script | What it does |
|---|---|
| `build_plugin.py` | Generates the `powerbi-migration-skills` marketplace plugin from `.github/skills/`. `--check` fails on drift. **Re-run and re-publish after editing any shipped bundle** — the plugin copy shadows the repo copy for a subagent, so an unpublished edit is served stale and silently. |
| `hooks/probe_subagent_start.ps1` | `subagentStart` hook probe — logs the payload and injects a sentinel. Measured working; see [`docs/agent-architecture.md`](../docs/agent-architecture.md) §6. |

## Corpus harvesting (occasional, not part of a migration)

How the `examples/` corpus was built: find real Tableau Public workbooks, then pick the ones that
stress idioms the parser has *not* yet seen. Run as a pair.

| Script | What it does |
|---|---|
| `harvest_tableau_public.py` | `discover` collects candidate workbook ids from the Tableau Public feed; `triage` downloads a diverse subset and runs the parser over each, writing a triage report. This is how the airline `spatial`/`table` data-type gaps were found. |
| `classify_harvest_hardness.py` | Scores harvested specs by **idiom hardness**, weighting unexercised idioms (LOD, live connections, heavy table calcs) highest, so selection avoids easy extract-based smoke tests. |

## Documentation artifacts (regeneration paths)

Each of these regenerates something **committed**. Keep them: without the script, nobody knows how to
rebuild the artifact after editing its source.

| Script | Regenerates |
|---|---|
| `make_showcase.py` | `docs/showcase/` — the before/after migration gallery |
| `make_carousel.py` | `docs/showcase/carousel/linkedin-carousel.pdf` + `slides/slide-0*.png` |
| `render_excalidraw.py` | `docs/architecture.png` from `docs/architecture.excalidraw` (the editable source of truth), avoiding the SSO-gated hosted export |
