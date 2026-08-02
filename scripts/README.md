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
| `preflight_source_credentials.py` | Classifies which data sources are **live** (and so need a reachability probe) and arms the credential gate. It is a *classifier*, not a connectivity test — it opens no socket, and deliberately no longer decides GO/STOP on its own. | `parse_tableau.py`, at parse time |
| `probe_live_source.py` | **The measurement.** Builds a one-table PBIP from the spec, opens it in Power BI Desktop, refreshes, and requires a real row back — emitting `DATA_OK` / `SKIPPED` / `NO_CREDENTIAL` / `UNREACHABLE` / `ERROR`. This is the `SELECT 1`, and it must go *through* Power BI: a shell query authenticates as the agent, while Power BI uses Desktop's per-user credential store, so a shell probe can pass against a source Power BI cannot open. | `tableau-migrator`, before any build |
| `credential_gate.py` | Enforces that gate at the **filesystem** level: denies write access to the migration's `fabric/` folder while a live source is unproven. `block` / `clear` / `authorize` / `verify` / `status`. Prose and tool-call hooks both lost to agents that rationalized or pattern-evaded; a kernel ACL does not care how a write is attempted. **`verify` is the authoritative pre-ship check** — it reads the ACL and audit log, not files an agent can forge. Full rationale + threat model: [`docs/credential-gate.md`](../docs/credential-gate.md) | `parse_tableau.py` (armed at parse time) and `tableau-migrator` step 13 |
| `hooks/credential_gate.py` | `preToolUse`/`permissionRequest` hook wired by [`.github/hooks/credential-gate.json`](../.github/hooks/credential-gate.json). Turns the ACL's opaque `PermissionError` into an explanation plus `interrupt: true`, and defends the gate's own narrow control surface. **Explanation layer, not the enforcement** — never rely on it alone. | every tool call |
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
| `sync_installed_skills.py` | Brings the **installed** plugin's bundles up to date **in place, mid-session** — the fix when `copilot plugin update` returns `Access is denied. (os error 5)`. That lock only blocks renaming the top two plugin directories; files inside stay writable, and `plugin update` fails solely because it swaps the directory. Content only: a manifest/version/MCP change still needs a real `plugin update` between sessions. `--check` reports drift and exits 1. |
| `update_migration_skills.ps1` | The between-sessions path: kills every Copilot CLI process (and its children) so the plugin directory unlocks, then runs `marketplace update` + `plugin update` and verifies with `preflight.ps1`. **Run from a plain PowerShell window, not from inside Copilot** — it kills the session you would be typing into. Prefer `sync_installed_skills.py` when only bundle content changed. |
| `probe_lab.py` | Agent-behaviour test harness. `make` generates **minimal** Tableau fixtures (one live source, two columns, no calculations) so the "probe the source before building" decision is reached in ~2 min instead of ~20; `watch` polls a running migration and returns PASS/FAIL/TIMEOUT so a deviation can be killed on sight. Writes to the gitignored `_probe-lab/`. Use when changing any instruction whose effect is only visible in agent behaviour. |
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
