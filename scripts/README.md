# `scripts/` — what each one is for

This folder is a lot to scan, so this is the index. **Every tracked file in this folder appears below**,
and `tests/test_repo_layout.py` fails if one is missing — an undocumented script is one nobody can
find, which is how five of these ended up unreferenced before this file existed.

Conventions: Python first (`.ps1` only where Windows-specific APIs make it unavoidable), a
`purpose:` / `usage:` header docstring in every file, `ruff` + `pylint` clean. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

## Run every session / every migration

At session start, run the direct command first:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1 -Update -CheckUpstream
```

**Only after an actual unsigned/ExecutionPolicy startup refusal**, follow
[preflight cannot start](../docs/operator-runbook.md#preflight-cannot-start).
If recovery is allowed, retry the **exact originating command and arguments**: retain
`-Update -CheckUpstream` for session start; a migration-start call stays plain. No policy pre-check.

| Script | What it does | When |
|---|---|---|
| `preflight.ps1` | Verifies the whole toolchain: Python + parser deps, **both skill plugins** and whether the published bundles still match `.github/skills/`, MCP servers, Power BI Desktop + Bridge CLI, the npm CLI version matrix. PowerShell on purpose — it must run before Python exists, since checking *for* Python is one of its jobs. | `-Update` at **session start**; plain at **migration start**; never mid-migration |
| `object_identity.py` | The identity abstraction every join runs through (#421 round 3). One defect recurred at **five** layers — routing, matching, normalization, manual-kind, unit-join — always as "one object's evidence or excuse covering another"; each round closed the layer found and left the shape available one level along. This is the missing abstraction rather than a sixth patch. Four properties, each enforced by construction: `ObjectIdentity` is `(kind, exact_name)` and buildable **only** from an engine artifact, so a normalized or provider-supplied string can never become a key; a producer's name yields a `Candidate`, not an identity. `IdentityIndex.resolve()` returns a `Resolution` whose only reader **raises** unless exactly one match exists — there is no `.first()`, no indexing and no truthiness, so "take the first candidate" is not expressible. `add()` appends and never overwrites, and no `set()` is taken where identity is derived (that is what silently deleted a workbook collision). And **normalization is a property of the index, not a call site**: `IdentityIndex(normalized=False)` has no lossy table at all, so an engine-to-engine join cannot slip into one. The test of success is that a *future* join cannot express the ambiguous case. |
| `parse_tableau.py` | The deterministic parser: `.twb`/`.twbx`/`.tds`/`.tdsx` → `migration-spec.json`, validated against `docs/migration-spec.schema.json`. The contract every downstream agent reads. It flags prompt-injection-shaped untrusted workbook text in `limitations_encountered`; content is disclosed, never silently rewritten. | Once per workbook, before any agent work |
| `prompt_injection.py` | Scans untrusted Tableau-derived contract text for high-precision prompt-injection shapes and returns high-severity disclosure limitations. Detection is deliberately not sanitisation, preserving customer source evidence. | Called by `parse_tableau.py` |

### Run setup

For each new site/folder/workbook/datasource, before stage writes run
`python -B scripts\work_dirs.py <slug> --json`; external roots add
`--runs-parent <parent>` (`--repo-root` alias). Allocation auto-attempts ignored
`_MIGRATION.md`; a warning does not undo success.

For an accepted existing/resumed run, setup runs
`python -B scripts\work_dirs.py --select-run <absolute-existing-run>`. It migrates
nothing. Never infer selection from the note, reselect at handoffs, or reallocate
on refusal. Preserve collisions; lost markers need explicit recovery.
Do not ask the human to generate the note.

## Migration pipeline

| Script | What it does | Called by |
|---|---|---|
| `build_migration_feedback.py` | Offline, hash-pinned private evidence builder for the repo-local `migration-feedback` skill. Validates recorded identity, canonical fresh-output receipts and controls; emits a strict public-safe `issue-payload.json`, never publishes or reruns anything. No default output outside a selected run. [Input/output contract](#migration-feedback-phase-1). | Internal implementation helper for the feedback skill, not a second diagnostics exporter |
| `preflight_source_credentials.py` | Classifies which data sources are **live** (and so need a reachability probe) and arms the credential gate. It is a *classifier*, not a connectivity test — it opens no socket, and deliberately no longer decides GO/STOP on its own. | `parse_tableau.py`, at parse time |
| `migration_bundle.py` | Small shared contract reader for the two migration tiers: a parser `migration-spec.json` or a deterministic-engine bundle (`report.json` + `handover/*.json`). It exposes only the fields gate tools need (migration dir, explicit data sources, published-datasource keys) and refuses to fabricate a spec when the engine lacks them. Defines the receipt-backed engine output roots (`pbip/`, `reports/`, `semantic_models/`, `data/`) that `credential_gate.py verify` may classify as pre-gate tier output, including PBIR `*.json` only under report-definition roots. | gate tools (`preflight_source_credentials.py`, `probe_live_source.py`, `published_datasource_registry.py`) |
| `tableau_capture_policy.py` | **Every bound the capture runs under, and the vocabulary of the three time words that are NOT synonyms.** Split out of `capture_tableau_oracle.py` (#423) the third time it hit its 1200-line pylint ceiling, on the seam two review findings landed in: a default budget that sat *below* its own admission floor for sub-second timeouts, and a salvage ceiling that bounded attempts while claiming to bound wall clock. Both were hard to see because the relationships were scattered through a 1200-line file. Holds `REST_TIMEOUT_SEC`, `DEFAULT_MAX_AGE_MINUTES`, `validate_max_age`, the retry arithmetic (`retry_admission_floor` / `default_retry_budget`, and their evaluations at the default), `TRANSIENT_STATUSES`, `backoff_delay`, `RetryPolicy`, `build_retry_policy`, and the salvage pair `SALVAGE_RETRY` / `SALVAGE_BUDGET_MULTIPLIER`. ⚠️ The vocabulary is the point: a **`timeout`** is a *socket-operation* timeout and does NOT bound a request (a trickling response never times out — measured, HTTP 200 at 4.8× a 0.1 s timeout); a **`budget_sec`** is a retry-*admission* deadline an in-flight attempt may legitimately overrun; and the **salvage budget** is an admission budget paired with an *end-to-end* deadline in `tableau_http`, which is what makes its wall-clock claim enforced rather than assumed. Nothing here talks to Tableau, holds a credential or touches a response, which is what keeps it out of the taint gate and the seam acyclic; `capture_tableau_oracle.py` re-exports the names so every caller and test is unchanged. | `capture_tableau_oracle.py` |
| `tableau_env.py` | Small shared `.env` reader for every script that signs in to Tableau. `resolve_env()` layers a `.env` over the process environment, accepting the legacy engine `TABLEAU_PAT_VALUE` spelling but normalising it to the one documented `TABLEAU_PAT_SECRET` key. `engine_child_env()` mirrors both spellings for every engine child process. `run_engine_survey.py` applies that bridge to the hand-run estate survey and always adds `--no-prompt`, so a missing credential fails instead of hanging. `require()` names every missing variable at once. **PAT only** by decision; there is no create-PAT API (HTTP 405). | `assess_estate.py`, `capture_tableau_oracle.py`, `harvest_estate_assets.py`, `run_engine_survey.py`, `stamp_tableau_provenance.py`, `tableau_lineage.py` |
| `tableau_payload_facts.py` | **What a Tableau REST payload actually IS** - row/column shape and format hints for a CSV, IHDR dimensions for a PNG, millimetre geometry and `<text>` counts for an SVG, MediaBox and embedded-font counts for a PDF. Pure functions over bytes: no session, no HTTP, no filesystem. Split out of `capture_tableau_oracle.py` because parsing a payload is a different concern from talking to Tableau - and because this is where #405 round 5's leak lived (`summarise_csv` copies a CSV's first row into `data.columns`, which is response text reaching the manifest by a route nobody had classified as a diagnostic). Keeping the parsers in one small module makes that surface enumerable; the provenance gate in `tests/test_diagnostic_redaction.py` covers this file for exactly that reason. | `capture_tableau_oracle.py` |
| `tableau_oracle_manifest.py` | **Records -> manifest -> exit code**: the verdict layer of the oracle capture. Split out of `capture_tableau_oracle.py` (#423) because that module sat at its 1200-line pylint ceiling and this is a different concern - nothing here talks to Tableau, it only decides what the evidence MEANS. Owns `_partition` (the ok/empty/complete/blocked/failed sets the exit code reads), `write_manifest` (and THE SINK: `scrub_tree` over the whole tree immediately before serialisation), `log_progress`, and the ⚠️ **UNESTABLISHED census** - `render_unestablished` counts AND NAMES every view for which a render was requested and none was obtained. That field exists because an absent `image` key used to read exactly like “no render was asked for”, so a view on which no visual-fidelity finding can be made landed in the clean bucket (#423). ⚠️ The **EMPTY census** is the numeric half of the same argument (#471): `empty_classification` is the ONE predicate for “this capture carries no rows”, `data_empty_views` counts AND NAMES them, and `flag_empty` puts `flags: ["data_empty", <class>]` on the record so every downstream slice carries the fact. The count already existed and the list did not - `empty` was built and reduced to its `len()` on the same line - so 12 blank views among 94 were countable and unfindable. `status` is deliberately NOT overloaded: the export succeeded, and an otherwise-clean run still exits 0. The class is `empty_query_no_rows` when a header came back and `empty_cannot_classify` when nothing did, because a glossary sheet with no query and a real query returning nothing are indistinguishable from the payload (both `row_count=0, columns=[]`; the byte count is not the discriminator). ⚠️ Also owns the **evidence-path rule** (#480 round 2): `data_leg_fields` decides in ONE place both the file an uncertified body is written to (`unassessable/<luid>.bin`, never `data/*.csv`) and the key that names it (`retained_path` plus an authored `evidence_withheld` sentence, never `path`), `withhold_uncertified_evidence` enforces it over any record reaching `write_manifest`, and `read_manifest` restores it over a manifest written before the rule - read a capture manifest through that, never `json.loads`. Round 1 recorded the same fact as a FLAG and left the bytes at `data/<luid>.csv`, and blind review then found a fourth consumer (`build_reconcile_items`) emitting `tableau_value: 10.0` from a record whose own flags said `data_unassessable`; a flag must be remembered by every consumer that ever reads a capture, a missing `path` cannot be forgotten by any of them. Takes the session **duck-typed** (`reauth_count`, `retry_count`, `redact_text`) so the pair stays acyclic; covered by `tests/test_diagnostic_redaction.py`, whose MODULES it joined because THE SINK moved here with it. | `capture_tableau_oracle.py` |
| `tableau_http.py` | **The ONE hardened HTTP round trip** for the two reference-capture callers (see *Used by*). ⚠️ **Not every Tableau REST caller in this folder** — `assess_estate.py`, `tableau_lineage.py`, `stamp_tableau_provenance.py` call `urlopen` directly and `provision_tableau_estate.py` uses `tableauserverclient`; all four are pinned as measured `KNOWN_GAPS` in `tests/test_diagnostic_redaction.py` and tracked in **#419** (raw response persistence, raw logging, an uncaught reflected credential, an unscrubbed manifest). `_request(req, timeout=, redactor=)` returns `(status, body, headers)` and **never raises** for anything the network or the server can do - `HTTPError` (with the error-body read itself guarded, because Python does not route an exception raised inside one `except` to a sibling `except`), `OSError`/`URLError`, and - the round-9 finding - `http.client.HTTPException`, which is **not** an `OSError`: `BadStatusLine` carries the server's raw status line and `InvalidURL` a redirect's host/port, both fully server-controlled, and both escaped every `except (OSError, urllib.error.URLError)` as an uncaught traceback carrying a reflected credential (measured: exit 1, PAT in the traceback; after, exit 0 and `[REDACTED]`). It exists because `capture_tableau_oracle.py` had a hardened path and `tableau_render_capability.py` had **three** hand-rolled ones, and rounds 7, 8 and 9 each found a different hole in the unhardened copies - the defect was never a missing exception type, it was the second HTTP client. Response bodies pass through **raw** (classification must see the unmodified text, or a short PAT name mangles Tableau's own `401002`); every string this module *authors* goes through `tableau_env.redacted_note`. `header_value()` restores the case-insensitive header lookup a plain `dict` loses. ⚠️ The name `_request` is load-bearing: `TAINTING_CALLS` in `tests/test_diagnostic_redaction.py` keys on it, so a rename or an alias silently un-taints every call site. | `capture_tableau_oracle.py`, `tableau_render_capability.py` |
| `detect_occlusion.py` | Statically finds visuals hidden behind a higher-z image (typically a carried-over Tableau background), reading `visual.json` only - no Desktop, no model, no data. Exits 1 on detection, so it works as a build gate. `powerbi-report-author validate` has no occlusion rule and returns `errorCount: 0` over a fully occluded report. Run after any deterministic-tier build. |
| `check_blank_placeholders.py` | Correlates deterministic-engine handover fallback entries (`report.json` → `workbooks[].model_translation_handoff`, with `handover/*.json` slices as a fallback for bundles that have no usable `report.json`) with TMDL columns/measures whose body is a bare `BLANK()`, then cross-references shipping PBIR filters and visual field bindings. Exit 0 clean / 1 placeholder only documented and unreferenced / 2 report depends on it / 3 incomplete scope (null or absent handoff, or BLANK() owners outside `workbooks[]`). An input it cannot read is counted (`handover_unreadable`) and reported, never raised. Wired into `run_estate.py` as a blocking gate only for report-referenced placeholders. |
| `probe_bundle.py` | Rewrites an emitted PBIP bundle into a ONE-ROW probe variant (wraps each M partition's final expression in `Table.FirstN(..., 1)`, flips DirectQuery to import) so a credential can be proven against the SAME M the real model uses. If emitted M contains `Value.NativeQuery`, it still writes the probe artifact but exits `PROBE: OPERATOR_REQUIRED` (4) before any refresh is attempted. `--check-only` runs the M-parameter check alone; `--unwrap` and `--assert-clean` reverse it and prove a shipping bundle is probe-free. |
| `run_estate.py` | **The tier-2 entry point for a whole folder of workbooks.** Runs the deterministic tier's `migrate_estate.py`, then supplies the three things its output contract does not: (1) a **real exit code** — the engine prints `[FAIL] Definition of done` and returns `0` anyway (`# Soft-but-loud: exit stays 0`), so a consumer gating on the exit code silently accepts a failed migration; (2) an **`--approved-dax` collision check** — that map is estate-GLOBAL and name-keyed, so one approval for a calc called `Calculation2` (Tableau's auto-generated default) lands in *every* model that reuses the name, and a differing formula is a wrong-DAX landing that never errors; (3) **per-workbook handover slices**, measured 83.4 KB → 5.7-24.5 KB, so the raw estate report never enters a subagent's context. `--storage-decision <file.json>` confirms only that the supplied file is readable, then forwards its original token to the engine; the engine alone validates its JSON and policy. Also writes `engine-output-receipt.json` (path/size/hash receipt for native engine artifacts, **plus the resolved engine root, `VERSION` and whether it was the canonical plugin** - so a bundle answers "what built me?" without the machine that built it) and `phase-timings.json`. The engine is not a parameter you normally pass: it resolves to the installed plugin via `engine_source.py`, and a `--engine` pointing anywhere else is refused unless `--allow-noncanonical-engine` acknowledges it. Source provenance runs in one supervised leaf worker under `--provenance-timeout-sec` (default 120, finite and > 0 - `0` is a usage error, not a disable switch); expiry, a worker crash or a failed publication publishes an honest partial/failed `source-provenance.json` and exits 11 without adjudicating. Emits no model content, by test. |
| `run_status.py` | Read-only, non-certifying inventory for one explicitly supplied absolute **local** run (Refs #469). Invoke with **`python -B`**, including for `--json`, to prevent import-bytecode writes. Prints absolute toolkit/run/bundle/oracle/package locations that distinguish the standard expected location from what was observed, preserves engine/working-copy occurrences and unscoped packages, reports integrity codes and safe last-observed fields, and gives one non-destructive next action. Both outputs contain the same normalized records, including multiplicity and action details. Exit 0 means assessable diagnostics, never readiness; malformed/unreadable inputs or rejected boundaries exit nonzero. No allocation, root discovery, process/network operation, repair, promotion or current START_READY/COMPLETE. See [read-only run status](#read-only-run-status). | operator diagnostics after a selected run exists |
| `derive_connection_templates.py` | Derives committable connection **templates** from REAL Tableau exports (`.tds`/`.tdsx`/`.twb`/`.twbx`), replacing only endpoint VALUES with placeholders so the attribute SHAPE stays verbatim. Shape fidelity is the point: a hand-written connection element encodes what we *think* Tableau writes, and that guess was wrong in a way that mattered - the Databricks HTTP path is spelled `_.fcp.DatabricksCatalog.true...v-http-path`, which two parsers read as `None`. Writes `tests/fixtures/connection-templates/<class>.xml`. |
| `make_live_source_fixture.py` | Builds a `.twbx` whose single federated datasource spans several LIVE systems (Databricks + Snowflake + Azure SQL), from the templates above plus `PROBE_*` environment variables. The generator carries no hostnames and is committable; the generated workbook does, so `tests/fixtures/live/` is gitignored and `tests/test_live_multisource.py` skips when it is absent. |
| `make_refresh_fixture.py` | Generates the deterministic, credential-free input for `fixtures/large-refresh`: a tunable local `orders.csv` plus the machine-local `expressions.tmdl` binding that points the model's `SourceFolder` parameter at it. The payload and binding are gitignored; same rows + seed produce byte-identical CSV bytes, so refresh timing changes mean the model changed rather than the data. | Issue #262 refresh-behaviour reproduction |
| `transpile_tableau_calc.py` | **Research artifact, refactor pending** - see its module docstring. Deterministic Tableau->DAX transpiler for the recurring idiom families in a deterministic-tier handoff; authored 126 of 188 stubbed calcs in one pass. Requires the `tableau-migration` plugin to transpile calculations. |
| `probe_live_source.py` | **The measurement.** Builds a one-table PBIP from the spec; ordinary-table and custom-SQL live sources open in Power BI Desktop, refresh, and require a real row back. Custom SQL is projected onto a constant `ProbeOK` column, so missing Tableau column enumeration is not mistaken for an unassessable source. Federated datasources are probed per `connection.connections[]` leg and clear the credential gate with the same stable marker keys `preflight_source_credentials.py` armed, so duplicate display names cannot collapse siblings; the restored #357 guard refuses any clear when the marker still names more keys than the probe proved. Other verdicts: `DATA_OK` / `SKIPPED` / `NO_CREDENTIAL` / `ACCESS_DENIED` / `UNREACHABLE` / `ERROR`. `ACCESS_DENIED` is what the classifier returns when access-denial-shaped text (`403`/forbidden/permission denied/not authorized) matched ahead of the credential markers; the markers are bare, so it does **not** establish that authentication succeeded, that the failure is permission-only, or that a fresh sign-in cannot help — `403 Unauthorized: authentication failed` and `403 Forbidden: access token revoked` both land here. Unchanged retry is not useful: read the redacted detail and change the credential/token or the permission the source names. It is never a timeout or a transient `ERROR`. This is the `SELECT 1`, and it must go *through* Power BI. | `tableau-migrator`, before any build |
| `_verdict_lines.py` | **Pure verdict-line matchers, extracted from `probe_live_source.py`.** The four `*_VERDICT_RE` regexes and the `_has_*_verdict` helpers that recognise a child probe's structural verdict lines (`DATA_OK`, the credential-stop family including `CREDENTIAL_UNKNOWN`, `DESKTOP_GONE`, and `DESKTOP_UNREADY`) before any free-text marker scan. Split out purely to keep `probe_live_source.py` under pylint's `max-module-lines` cap, which that module had hit three times in two days; `import re` only, no Desktop and no state, and re-exported through `probe_live_source` so `_classify_failure` and the seam tests are unchanged. | imported by `probe_live_source.py` |
| `_probe_pbip.py` | **The PBIP scaffold writers, extracted from `probe_live_source.py`** — `_tmdl_ident`, `_tmdl_filename_stem`, `_pbip_files`, plus the five `$schema` constants. Given a table, a column and an M query they return every relative path and file body Desktop needs to open the throwaway one-table probe model; pure strings and JSON, no network and no subprocess. The second and last extraction that module's own docstring named (SPLIT, not waive). ⚠️ **Re-exported through `probe_live_source` and the import carries `noqa: F401`** — the seam tests reach them as `probe_live_source._tmdl_*`, and `ruff check --fix` deletes the names as unused without it. | imported by `probe_live_source.py` |
| `_gate_lift.py` | **The earned-clear decision, extracted from `probe_live_source.py`** — `lift_gate` shells out to `credential_gate.py clear --earned --sources <marker keys>`. Reaching a source and deciding whether that earned a credential-gate clear are different jobs; the deciding half now accepts only the real stable source keys the probe contacted, refuses empty proof, and keeps the conservative #357 cardinality guard: if the marker names more keys than the probe proved, the gate stays armed. ⚠️ **Re-exported through `probe_live_source` with `noqa: F401`**: seam tests reach it as `probe_live_source._lift_gate`, and `ruff check --fix` deletes the name as unused without it. | imported by `probe_live_source.py` |
| `credential_gate.py` | Enforces that gate at the **filesystem** level: denies write access to the migration's `fabric/` folder while a live source is unproven. `block` / `clear` / `authorize` / `verify` / `status` / `list`. Prose and tool-call hooks both lost to agents that rationalized or pattern-evaded; a kernel ACL does not care how a write is attempted. **`verify` is the authoritative pre-ship check** — it reads the ACL and audit log, not files an agent can forge; on the engine path, verify the audit-bearing bundle directly (ship-destination provenance is tracked by issue #354). **`list <estate-root>` answers "which units are gated?" across a whole estate** (`--json`; exit 1 = still blocked, 3 = forged override, 4 = bad root, 2 = argparse usage) and is a resume signal, never a ship gate. Full rationale + threat model: [`docs/credential-gate.md`](../docs/credential-gate.md) | `parse_tableau.py` (armed at parse time) and `tableau-migrator` step 15 |
| `reprobe_blocked.py` | **Estate-scale re-probe after ONE machine-wide sign-in.** Given a set of gated units (explicit `--unit` or a `--units-from` file, both standalone; or `credential_gate.py list <root> --json` piped to `--stdin`, which ****), it re-runs `probe_live_source.py` on the ones still **BLOCKED** and lets each gate EARN its own `probe-cleared` where the probe now passes — so one sign-in does not mean N hand-typed `authorize` calls that permanently stamp every unit unvalidated. **It never clears, authorizes, forces or mass-authorizes anything itself** — it only runs the probe and reads ground truth. **Dry-run by default** (`--apply` to run); it is the supervising timer, so don't wrap it in a 2-minute cap. Reports per unit: `newly-earned` / `still-blocked` (NO_CREDENTIAL vs UNREACHABLE distinguished) / `anomaly` (incl. a probe that returned DATA_OK yet left the gate armed) / `errored` / `skipped`. Exit 0 clean / 1 still blocked / 2 usage (no source) / 3 forged-override / 5 anomaly-or-structural-input-error. | a human (or `tableau-migrator`) after a human signs in, to resume blocked units |
| `hooks/credential_gate.py` | `preToolUse`/`permissionRequest` hook wired by [`.github/hooks/credential-gate.json`](../.github/hooks/credential-gate.json). Turns the ACL's opaque `PermissionError` into an explanation plus `interrupt: true`, and defends the gate's own narrow control surface. **Explanation layer, not the enforcement** — never rely on it alone. | every tool call |
| `published_datasource_registry.py` | Matches a workbook's `published_datasource.key` against already-migrated data sources, so one shared Tableau datasource becomes **one** semantic model with many reports bound to it — not N near-identical models. | `tableau-migrator` step 4 |
| `tableau_lineage.py` | Queries the Tableau Metadata API for `publishedDatasources { downstreamWorkbooks }` and prints a model-first, two-phase migration plan for a whole estate. `--download` pulls each `.tdsx`. | `tableau-migrator` step 1 (estate migrations) |
| `assess_estate.py` | **The Phase 0 front door: what should be migrated at all, before anything is.** Reads a whole Tableau site over REST + one Metadata API call, and emits a scored, tiered backlog + the **coverage curve** the scope decision is actually made on (how few workbooks carry 99% of usage). Encodes four refusals, each from a way this question is normally answered wrongly: (1) **it never retires on a metric** — a subscription, alert or saved custom view outranks a view count, because that is the only available proxy for the quarterly board pack with near-zero views; (2) **it never guesses dependencies** — the survey key is `datasource_name`, read explicitly, and it *raises* if a survey declares edges but none parse, because a guess that yields nothing is indistinguishable from a genuine absence (measured: an earlier guess reported "order unknown" for an estate with nine real edges); (3) **it exports IAM but never maps it** — Power BI has no Deny and shares per report not per page, so mapping before the workspace topology is fixed produces confident nonsense; (4) **it never claims a usage window it does not have** — Cloud counts are lifetime only, and it warns when usage is too sparse to tier on at all (measured: 1 view event across 13 workbooks). Complexity is knowingly **understated** for workbooks backed by a published datasource, and says so. Also reports the site's **render ceiling** up front (#474), and reports it as a **verdict per rung** rather than as two numbers an operator has to do arithmetic on: what we **send** (`TABLEAU_REST_API_VERSION`, a client preference), what the server **advertises** (`/serverinfo`, unauthenticated), then one line each for `svg` / `pdf` / `png_high` with its floor and an explicit verdict, and the bottom line (*“`--reference-best` should resolve to `pdf` on this site”*, because SVG needs REST **3.29** / Server 2026.2 and a site below that cannot export SVG **at any client setting**). ⚠️ It states the **raster ceiling** beside the availability, because *available* is the half that gets over-trusted: `?resolution=high` is **exactly 2× a dashboard's declared size** (52/52), so a 650×800 dashboard tops out at 1300×1600 forever - which is *why* PDF matters on a sub-3.29 server, it is the only vector rung left. Verdicts are **graded**: `unavailable` is firm (the site's own ceiling is below that floor), `available` is a claim the endpoint has not been asked to honour - only `capture_tableau_oracle.py --reference-best` settles it. ⚠️⚠️ **The three-state contract holds here too**: if the ceiling is not *established* - `/serverinfo` does not answer, answers **unsuccessfully** (a proxy's 404, the server's own 500, whose body may still carry a `<restApiVersion>`), or answers 200 reporting something that is **not a version** (`garbage-999`, `3.x`) - the ceiling is reported as *not established* **naming which of those it was**, every rung's structured `verdict` is `unknown`, and **no rung table is printed at all** - a table rendered from a ceiling nobody established is indistinguishable from a measured one. ⚠️ All three were measured producing CONFIDENT verdicts before #475's review, and in opposite directions from the same input class: a 500 carrying `3.30` was read as REST 3.30 with SVG available, `garbage-999` became `(999,)` and therefore best rung **SVG**, while `not-a-version` became `(0,)` and therefore **no reference render reachable at all** - so "it fails safe" was never a defence. A version is trusted only from a **successful** response and only against a numeric API-version grammar; ⚠️ that grammar is deliberately **not** membership in the version→release table, which stops at 3.29 while a live Cloud site already advertises 3.30, so a numeric but unpublished `9.99` stays established and above the floor. It **fails soft** and never degrades the assessment, since nothing downstream is computed from it. Writes `estate.db` (SQLite), `assessment.json` and a customer-facing `report.md`. Also computes and renders `datasource_hazards` (issue #368) — every datasource name that is not a safe unique lookup (a name shared across the published/embedded class boundary, or duplicated within one class), surfaced ONCE, estate-wide, before anyone picks a named target. | `tableau-migrator` step 0 (estate migrations) |
| `resolve_datasource_target.py` | **The class guard for a named migration target (issue #368).** A published datasource and a workbook-embedded one are different CLASSES of object that can legitimately share a display name — a request for `DS_CAPS` (published) was silently applied to `IA_CAPS_DS` (embedded) instead, then built, validated and reported on. Given `assess_estate.py`'s raw evidence and a `--name`/`--class {published,embedded}`, resolves EXACTLY — never a normalized or near-name fallback — to one of three structurally distinguishable outcomes: `resolved` (exactly one candidate in the requested class, none in the other — declares its class/LUID/project and may proceed), `absent` (nothing in the requested class anywhere in the estate — refuses, naming an other-class hit if one exists, never silently promoting it), or `ambiguous` (more than one candidate in the requested class, or at least one in EACH class — refuses). Exit 0 resolved / 1 absent / 2 ambiguous / usage error otherwise. | `tableau-migrator` step 0/1, before building a named datasource target |
| `deploy_estate.py` | **The last mile: lands a whole estate in a Fabric LANDING ZONE workspace, models first, each report rebound to its deployed model.** Encodes three facts measured against a real tenant, each of which silently produces a broken or absent deployment: (1) a migrated PBIP binds its report to its model **by PATH**, which the service cannot resolve — the report must be rebound to `byConnection` *after* the model exists, which is what forces model-first ordering; (2) the widely-quoted five-field `byConnection` is PBIR schema **1.0.0** — schema **2.0.0** sets `additionalProperties: false` and allows only `connectionString`, so the model's guid travels *inside* it as `semanticModelId=<guid>` (omit it and the service answers `InvalidConnectionInformation`; send the 1.0.0 shape and it answers `Workload_FailedToParseFile`); (3) **`202 Accepted` tells you nothing** — create returns an empty body and a FAILED operation is indistinguishable from success until `/operations/{id}` is polled. Crash-safe by a run journal that records **intent before the mutation** and hashes the exact definition deployed, so a resume skips only what is byte-identical — "an item with this name exists" is the check that silently ships a half-uploaded item. A **run lock** prevents two overlapping deploys, which is not hypothetical: Fabric does **not** reject a repeated report/model name, and two concurrent runs produced duplicate items in a real workspace. `--dry-run` reports the plan and the **item count**, which is the number a customer agrees before deploying because each item carries a cost in their capacity/licensing terms. Targets an **existing** workspace only — never creates one, since that means choosing a capacity. | `tableau-migrator`, after `run_estate.py` |
| `verify_bindings.py` | **Runbook check 8 as one command: does every deployed report actually resolve its model?** Read-only. Lists every page of a workspace's items and, for each report, **polls** `getDefinition` to a terminal state before decoding `definition.pbir` — because the naive call fabricates a convincing false defect: the POST answers **`202 Accepted` with an EMPTY body**, and parsing that yields `byPath=False semanticModelId=NONE` for the whole estate, which reads exactly like reports bound to nothing. That is the same *"202 tells you nothing"* trap `deploy_estate.py` documents for **create**, and it caught two independent operators on the same day, one of whom briefly reported it as a critical finding. Keeps three things distinct that the naive call conflates: a genuine `byPath` (re-deploy — the rebind is the deployer's job), a guid that is **not a SemanticModel in this workspace** (well-formed and still unresolved), and a failed or timed-out **read** (re-run; not evidence of anything about the report). On a 404 it decodes the token's `tid` and says so, because `az account get-access-token` can succeed against the **wrong tenant** and the API then reports a workspace that plainly exists as missing — 15 minutes, on a real run. The first poll is deliberately quick: measured, the endpoint sends `Retry-After: 20` for an operation that completes in ~0.3s, and obeying it turned a 36-report check into a 12-minute one nobody would run. Exit 0 all resolved / 1 findings / 2 could-not-check — including **zero reports**, so a vacuous pass is impossible. Verified live against a deployed estate: **36/36 byConnection, 124s**. Proves the reports **bind**; never that a visual **renders**. | after `deploy_estate.py`; operator-runbook check 8 |
| `check_migration_progress.py` | **Orchestrator supervision: is a delegated migration PROGRESSING or SPINNING, is a model safe to hand on, and did generated output drift?** Three mutually-exclusive modes, all answering from artifacts on disk rather than from a subagent's narrative. **Progress** distinguishes work from motion. Measured 2026-08-07 with four migrations in parallel: two passed 100 minutes on their first turn and elapsed time could not tell them apart - one had written 27 model + 148 report files (30 stubbed calcs is genuinely slow), the other had written **zero** report files in 105 minutes while accumulating scratch. Progress mode now fails closed unless the caller passes `--baseline <iso8601>` from delegation time, so dispatcher setup files cannot be credited to the agent. Pass `--liveness active` only when the runtime's tool-call count rose since the last poll; it can turn otherwise file-silent read-heavy phases into THINKING, but it never rescues scratch-only activity across a full window. The signal is **deliverable output over a window**, never recency: the first version asked "was the last write < 180s ago?", which meant an agent touching scratch every 30s forever read THINKING forever - exactly the run it was written for. Recent prior deliverables just outside the fixed window are reported as bounded burst context; ancient deliverables still go SILENT. **Handoff** (`--handoff`) answers the other half: every `*.SemanticModel` must carry a `cache.abf` that **post-dates its newest TMDL edit**. Measured race - a cache was written at 22:22 and Desktop opened at 22:19, so that instance loaded an EMPTY model though the handoff gate had run correctly; the ORDER was wrong. A stale cache is worse than none, because something loads and nothing looks wrong. **Tamper** (`--tamper`) compares generated-file hashes recorded by `run_estate.py` in `input_manifest.json` and reports TMDL/PBIR/`.pbip` drift unless either the legacy `_build/generated-edit-declarations.json` ledger or an append-only `_build/generated-edit-declarations/*.json` record ties the target, engine run id, baseline hash and expected post-fix hash to the current result; `.pbi` cache/autosave sidecars are ignored, and `data/` remains out of scope because this gate is for generated model/report artifacts rather than source/extract payloads. The declaration is an audit record, not authorization: it makes a replay visible, but a reviewer still judges whether the script is legitimate. A bundle whose `input_manifest.json` genuinely never recorded a `generated_artifacts` baseline (e.g. one built with `run_estate.py --slice-only` before it started backfilling one) is `NO_BASELINE_BY_DESIGN` (exit 3) - EXPECTED ABSENCE, not tampering - and is kept distinct from `NO_BASELINE` (exit 2), which means a baseline that SHOULD be there is missing, corrupt, or invalid (issue #230). `run_estate.py --slice-only` now backfills a best-effort baseline itself when none exists, scoped to whatever is on disk at that moment; its `coverage: "slice_only_backfill"` marker makes every verdict drawn from it - pass or fail - say its coverage is partial rather than claiming full attestation. Exit codes gate (0 ok / 1 stalled-or-not-ready-or-undeclared-drift / 2 silent-or-no-model-or-no-baseline / 3 no-baseline-by-design / 4 `UNREADABLE_DECLARATIONS`, the last two tamper mode only). Exit 4 means drift exists AND the ledger that might exonerate it cannot be read - deliberately not 1, which asserts the stronger "moved and undeclared". Drift is computed BEFORE the ledger is loaded, so a pristine bundle never consults it: loading it first made a corrupt ledger crash an otherwise-`CLEAN` check with a traceback exiting 1 (PR #399 round 3). Deliberately **not** a kill switch: STALLED routes to *ask what it is blocked on*, since killing a slow-but-productive run is the worse error. | `tableau-migrator`, on a cadence while subagents run |
| `migration_cost_report.py` | Joins `_runs/**/run.json` attribution anchors to the local read-only Copilot `assistant_usage_events` store and reports incurred AI spend (`total_nano_aiu`), token classes, model-call time, wall-clock telemetry span, model mix, by-agent breakdown, fix rounds, and report-vs-datasource mean/p50/p90. Unattributed run files are shown rather than zeroed, development sessions with no `run.json` are excluded by construction, and run files flagged as polluted by unrelated work are excluded from estate averages. | after each migration batch, for customer budget/schedule estimates |
| `declare_generated_edit.py` | Runs a replayable generated-artifact fix script, records the target's before/after hashes as one append-only `_build/generated-edit-declarations/*.json` file, and exits with the fix script's code. Use it around `_build/fix_*.py` scripts that intentionally edit generated TMDL/PBIR/`.pbip` so `check_migration_progress.py --tamper` reports declared drift rather than silent mutation. It is an audit wrapper, not a security boundary. **Two measured traps, both of which leave `--tamper` red at exit 1 while this exits 0** (issue #166): (a) `--target` declares exactly ONE file per run, and because a correct fix script is idempotent a second run prints `DECLARE: NO_CHANGE` and records nothing - so a whole-tree emitter leaves every file but one UNDECLARED. Give the fix script an `--only <bundle-relative path>` scope argument, pass it after `--`, and run the wrapper once per target. (b) It hashes the target *before* running the script and the gate only accepts a declaration whose `baseline_sha256` is the engine's, so an edit you already applied by hand **cannot be retro-declared** - re-run the engine to restore the target first (`<bundle>/reports/` is a reference-only baseline, **not** a copy of `pbip/`; its `definition.pbir` binds to a different model). Touching a target again after declaring invalidates that declaration. **It also writes a NAVIGATIONAL replay-manifest row** (issue #259) to `_build/replay-manifest/`, keyed by target so a re-declaration updates its own row instead of accumulating a duplicate - unconditionally, even on `DECLARE: NO_CHANGE`, so an idempotent re-run stays findable. Optional `--purpose <one-line>` records why the script exists. This is an index for discoverability only - it is not a gate, not schema-enforced, and proves nothing about the script's existence, digest, or coverage. | `_build/fix_*.py` authors and `tableau-migrator` sign-off |
| `generated_edit_declarations.py` | Shared reader/writer for generated-edit declarations, and the navigational replay-manifest rows `declare_generated_edit.py` writes beside them (issue #259). Declarations stay append-only, one file per record, so parallel agents never read-modify-write the same audit path; the replay-manifest is keyed by target and intentionally overwrites on re-declaration instead of accumulating duplicates. Preserves backward compatibility with the old single-ledger JSON for declarations. Imported by `declare_generated_edit.py` and `check_migration_progress.py`; not a user-facing CLI. | declaration/manifest writers, `check_migration_progress.py --tamper` |
| `harvest_engine_gaps.py` | **Turns the `reports/` vs `pbip/` delta into engine-gap evidence, or refuses to.** Issue #274 proposed reading that delta as the engine's gap list; measured on the 52-asset estate run `_runs/estate-2.339.0-20260829`, that reading is wrong in the noisy direction - 37 of 44 report pairs differ across 500 files and **100% of those bytes were written by the engine itself** (all 2481 recorded artifacts hash-match; no `_build/`, no edit declaration, nobody touched it). So it **attributes before it counts**, arbitrated by `generated_artifacts.files`, which covers **both** sides: `engine_internal` (engine wrote both - by design, never defect evidence), `tier_edit` (**the** answer to #274), `baseline_tampered` (**refuses**, exit 1 - the shape behind one already-retracted upstream report) and `unattributed` (a `NO_BASELINE` bundle prints the delta and withholds the authorship claim rather than guessing). **Provenance is NOT arbitrated here**: it delegates to `check_migration_progress.adjudicate_generated_drift()`, the structured core of the `--tamper` gate, after a blind review found four defects in its own hand-rolled attribution and measured that gate answering all four correctly - a baseline rewritten from `pbip/` (harvest `complete` with **0** differing files vs gate `DRIFT`), a post-engine file creation, a post-engine deletion, and a declaration invalidated by a later edit. Two divergences, both stricter: a `slice_only_backfill` baseline is **unavailable** (it has no engine boundary to answer from), and drift under `reports/`+`semantic_models/` is refused **even when declared**. `attribution.coverage` reports `paths_attributed`/`paths_compared`, and one unattributed difference forces `incomplete`. Every adjudicated path is **reconciled**: drift outside a discovered pair (a `pbip/*.pbip` project file - the corpus has 51 - or a deleted working report) becomes an `unpaired` tier edit, and anything that still cannot be placed lands in `unreconciled_drift` and blocks `complete`. Shapes come from a JSON-pointer diff plus a TMDL line diff (`harvest_gap_shapes.py`); `BINDING_RESOLUTION` is decided **per changed leaf**, and `queryRef` is resolved against the model's real table names (longest prefix, `Sum(...)` unwrapped) rather than split at the first dot - the estate has a table literally called `HumanResources.csv`. Measured across three rules, files carrying an unexplained name change went **27 (whole-file) -> 270 (first-dot per-leaf) -> 87 (table-resolved)**; the middle figure was 183 files of false positive, and publishing it is how it got caught. The markdown's "no tier edits" claim is emitted only when attribution is usable, every differing path is `engine_internal` AND the baseline is intact - it used to print "every differing byte is still hash-identical" on an unattributable run and on a rewritten baseline, contradicting the JSON beside it. The baseline is a reference-only emission whose `byPath` resolves in **0 of 45**. Deliberately **does not use git**: of 44 pairs the AGENTS.md-mandated `git diff --no-index` produced no stat line for 3 (paths 261/285/287, vs 259 for the 41 it could read) while agreeing with this module 41/41 on those it could - so UNASSESSABLE falls 3 to 0, and the blind spot is still reported. Per-layer denominators stay separate because they differ enormously (report 44 of 52 assessed; model **16 of 51**, i.e. 35 working models have no engine baseline at all - issue #179 at estate scale), and the model layer pairs on **model** name, not unit name. Exit 0 complete / 1 untrustworthy / 2 usage / 3 incomplete. Detail: [`docs/engine-gap-harvest.md`](../docs/engine-gap-harvest.md). | after a fix pass, and before filing anything upstream |
| `harvest_gap_report.py` | Renders one engine-gap harvest report - the console view and the upstream-fileable markdown. Split from `harvest_engine_gaps.py` because it consumes only the finished report dict: no bundle, no hashes, no filesystem, so it can be exercised on a literal payload, and the seam keeps the analysis module under pylint's `max-module-lines`. It imports **nothing** from `harvest_engine_gaps` on purpose - the provenance vocabulary is read out of `report["provenance"]` and the baseline roots out of `report["baseline_roots"]`, so the dependency stays one-way. Every table carries its denominator and every non-claim is printed rather than implied, because the output is meant to be pasted into an upstream issue. Not a user-facing CLI. | imported by `harvest_engine_gaps.py` |
| `harvest_gap_trees.py` | Reads two directory trees and reports how they differ - the layer beneath both axes of the engine-gap harvest. Split out of `harvest_engine_gaps.py` for the same two reasons `harvest_gap_shapes.py` was: it answers an independent question, needing neither the engine's hash baseline nor any notion of provenance, and the seam buys headroom under pylint's `max-module-lines`. Unreadable entries are kept apart from identical ones and withdrawn from BOTH sides with every descendant, so a directory `os.walk` could not enter can never masquerade as an addition, and a failure at the tree ROOT (`.`) means the whole tree. ⚠️ It does NOT attempt to prove the trees held still while it read them - that guarantee was tried over four review rounds of PR #399 and withdrawn; see issue #418. Not a user-facing CLI. | imported by `harvest_engine_gaps.py` |
| `harvest_gap_shapes.py` | Axis 2 of the engine-gap harvest: WHAT a difference is, from a structural JSON-pointer diff (PBIR) or a line diff (TMDL). Split out of `harvest_engine_gaps.py` because the two axes are independent - shape classification never needs the engine's hash baseline, and provenance never needs to parse a visual - and the seam buys both modules headroom under pylint's `max-module-lines`. Holds the leaf-level `BINDING_RESOLUTION` rule: a name change is excused only when THAT leaf's before-value is absent from the bound model and its after-value present, so one genuine invalid->valid rebind can no longer launder an unrelated valid->valid table substitution sitting beside it. ⚠️ A `queryRef` is resolved against the model's REAL table names - longest matching prefix, `Sum(...)` wrappers unwrapped and required unchanged, property suffix required unchanged - never split at the first dot: the estate's model holds a table literally called `HumanResources.csv`, and first-dot splitting retained 477 textbook rebinds as unexplained. The unwrap is deliberately NOT anchored at the end of the value, because Power BI appends a disambiguation suffix to a duplicated reference (`Sum(Orders.csv.Sales) 2`); that suffix is captured and COMPARED, since changing it changes which duplicate is meant. `Property` and `nativeQueryRef` can never be demonstrated (only TABLE names are read from the model) and always stay `MODEL_OBJECT_NAMES`. Not a user-facing CLI. | imported by `harvest_engine_gaps.py` |
| `harvest_estate_assets.py` | **Estate-wide dual-parser sweep** - downloads every workbook and published datasource on a site (by LUID, never by name) and runs BOTH tiers' parsers over them. Offline after the fetch: no Power BI Desktop, no Fabric capacity, no data-source credential, because a LIVE connection is only contacted at refresh and never at parse. Exercising a tier on whatever workbook is in front of us selects for the shapes we already know; an estate-wide pass selects for nothing, so it finds the shapes nobody thought to try. It runs both parsers because they answer different questions and the DISAGREEMENTS are the point - ours is the fidelity spec (mark types, encodings, shelves), his is the conversion descriptor (relations, columns, routing), so which side refuses says which tier owns the gap. **First run, 55 assets in 123s: both parsers 55/55, zero crashes - but 17 of 38 workbooks (45%) carry relations with no resolvable columns**, in exactly two families: 9 `sqlproxy` (binds a published datasource -> upstream #105) and 8 `textscan` (multi-table extract -> upstream #104). Each asset is fetched with its OWN sign-in, because a shared token drops intermittently mid-loop on Tableau Cloud. **Project scoping** is `--project NAME` / `--project-id LUID` (both repeatable, both matched exactly), plus `--project-url URL` for a link pasted straight out of the browser: a URL carrying a **LUID** is normalised into the `--project-id` path — canonicalised to the lowercase unbraced form Tableau itself stores, so an uppercase or `{braced}` paste selects the real project instead of matching no row — while Tableau's **numeric** web-UI id (`https://<site>/#/projects/35`) is refused *before any sign-in or download*, with the number echoed and the two usable inputs named — it is a legacy identifier with no public REST or Metadata API mapping (issue #191, verified against a live site: REST returns only GUIDs, the Metadata API answers `FieldUndefined`). A malformed, unsupported or ambiguous URL, a segment carrying a percent-decoded control or zero-width character, and any mixture containing one unresolvable URL, refuse the WHOLE invocation as a **sanitized usage error (exit 2)** — never a raw `urlsplit` traceback, which would exit **1** and so claim the estate could not be assessed, and never echoing userinfo, query or fragment. An empty selection reads as "that project has no content" rather than "we could not resolve your input". | `tableau-migrator` step 0, and after each engine release |
| `dax_oracle_server.py` | **Fills the deterministic tier's `fabric_oracle(dax_query) -> result` socket** - the injection point his `translation_reconcile` has always had and that, in his words on issue #96, *"no real executor has ever been attached"* to. The empirical half of his second compiler was written, tested and **unreachable**; this is the missing half. Speaks his `persistent_oracle` protocol (newline-delimited JSON on stdio) against one Power BI Desktop instance's local Analysis Services engine, reusing `probe_desktop_query.discover_port`'s **pid-scoped** lookup - which refuses to widen to "any msmdsrv on the machine", the difference in a parallel batch between querying your model and a sibling's. Enforces his three obligations rather than intending them: never raises (failures become `{"error": ...}`), pure read (statement allow-list, so `DROP`/`ALTER`/`<Batch>` never reach the connection), and **an empty result set is an error, never `0`** - a fabricated zero is indistinguishable from a real one and would produce a false `verified`. Marshals `System.Decimal` (what Currency columns return, which `json.dumps` cannot serialise and which would otherwise kill the session, not just the query) to a **number**, never a string - a stringified number silently turns the upstream comparison into a string comparison. `--certify` self-certifies against **his** `conforms()`, not our reading of it; `--offline` runs the whole protocol with no Desktop, so CI exercises the wiring. **Verified end-to-end against a live model**: Superstore refreshed to 10,194 rows, `SUM('Orders'[Sales])` = 2,326,534.35, and `reconcile` reaching both `verified` and `mismatch`. | `pbi-migration-validator`, `tableau-migrator` |
| `extract_hyper_data.py` | Materializes every `.hyper` extract relation to CSV so a migrated model has real rows. Extracts have no live connection — this is the only honest alternative to fabricating data. | `pbi-semantic-builder` |
| `extract_twb_thumbnails.py` | Pulls each worksheet's **base64 thumbnail out of the `.twb` XML** — Tableau's *own* render, so it needs no Tableau Server, no credential and no browser. Fills the gap that made a whole maps migration sign off blind: without a reference image the validator can only check structure. At ~192px it is decisive for **mark count**, mark type, layering and spatial distribution, and useless for fonts or exact hue — it says so on every run, because a reference that overstates its precision is worse than none. | `pbi-migration-validator`, `tableau-migrator` |
| `extract_twbx_result_cache.py` | Recovers **Tableau's own computed tuples** from a `.twbx`'s embedded `TwbxExternalCache/`, giving an offline numeric oracle with no server and no credential. Unlike a re-derivation from source rows, these values were produced by Tableau itself, so they can **falsify** a DAX translation rather than merely agree with it. Written after a model passed TMDL deserialization, M-syntax, openability *and* a persisted cache while every decimal was inflated ~493× — only a value-level oracle caught it. Not every workbook carries a cache (it exits 1 saying so). | `pbi-semantic-builder`, `pbi-migration-validator` |
| `connections_manifest.py` | **The answer to "what will this migration ask of my platform team?"** Emits `connections.md` + `connections.json` from an estate bundle into the git-ignored `_connections_manifest/` default (or another git-ignored / out-of-checkout directory): every data source, what it connects to, and **which reports stay broken until it is connected** — ordered by that blast radius, because a source feeding twelve workbooks is not the same task as one feeding an archived report. Four refusals: it **refuses unignored in-repo output** because the files name real customer servers/databases; it **never emits a secret** (allow-listed connection fields plus a credential-shaped-key alarm, so a future field carrying a token cannot reach a document meant to be emailed); it **never calls an extract "connected"** — a materialized `.hyper` is a *snapshot* with no upstream, which customers otherwise chase a credential for; and it **never tells you to connect to `sqlproxy`**, naming a published data source as one and pointing at where its upstream actually lives. Assembles `migration_bundle` + `preflight_source_credentials.classify_source` rather than holding its own policy. | `tableau-migrator`, before any deploy; hand to the customer |
| `connection_target.py` | Resolves what a Power Query partition should actually connect to (extract folder vs live source), including Tableau spatial-file connector aliases `ogr` / `ogrdirect` that have no credentialable upstream endpoint. | `pbi-semantic-builder`, CI gate |
| [`set_data_folder.py`](#package-folder-identification-616) | Checkout localize / `--sanitize` / `--check` are unchanged. `--package ABS` transactionally binds/reseals a clean **local working package**; `--inspect` freshly checks that location, not shareability or readiness. Its typed inspection is **BOUND / UNBOUND / NOT_APPLICABLE**; BOUND always has validation **UNVALIDATED**, including after publication. The in-process `PackageBindingResult.inspection` retains its held current S1/S2 and ordered cohort; JSON omits those private authorities. Report-only success consistently returns `binding_not_applicable`, but even a sanitize no-op requires the normal S1/S2/provider inputs and final cohort recheck; missing, blocked or stale providers earn no typed inspection. Bind providers separately first, then supply their fixed ordered cohort with repeated `--provider-package ABS`. Profile/home/temp roots remain allowed subject to native, absolute, existing, non-UNC/non-reparse, path-budget and same-volume transaction safety; admission I/O/resolution uncertainty is path-free `binding_root_unassessable`, exit 3. **Sanitize before transfer** with `--package ABS --sanitize`, supplying the same explicit providers for a report-only consumer, then bind at the recipient. Direct models still sanitize without providers. Package tails are anchored at the held root/manifest boundary, preserving later `data` segments; an ambiguous moved boundary is refused rather than guessed. Only eligible folder values, binding metadata/exact owned prose and corresponding digests change. The publication callback's inspection remains provisional until cleanup and one common read-only discovery assessment. That same assessment gates **every** public inspection-producing return (inspect, idempotent bind, sanitize/no-op, N/A and publication), checking all held cohort roots against exact bytes/identity and requiring absent staging/retired discovery markers. It also governs scratch admission before `mkdir`; only `FileNotFoundError` means absent, never an I/O failure or interrupt. Duplicate, unassessable or nonmatching authority returns `binding_discovery_unassessable` (3, or 130 on interrupt), with no typed inspection. Read-only inspect refuses without changing either copy. Mutating paths invoke the existing closure/revocation even before scratch ownership; unowned scratch evidence is preserved rather than deleted, and providers remain read-only. Cleanup never upgrades that refusal into a typed success. Manual copying remains residual; existing promotion retains its host-path shipping boundary. Legacy bound S1-dirty packages are **refused**, never rebaselined. Exits: 0 success/idempotent/N/A, 1 refusal/mismatch/published-with-residue, 2 usage, 3 cannot-establish, 130 interrupt with outcome. No readiness, revision token or sharing receipt is issued. **Invalidates `cache.abf`** (rewrites TMDL); bind before the final refresh. | `pbi-semantic-builder` |
| `build_reconcile_items.py` | Maps a captured Tableau oracle (per-view CSVs) into the `{name, grain_filters, tableau_value}` items the deterministic tier's `translation_reconcile.reconcile_all` consumes — the plug between our ground truth and its comparison engine, whose Tableau-side socket nothing has ever filled. Dimension-vs-measure split comes from Tableau's own field `role` via the Metadata API, **never inferred from the data**; an unmatched header is recorded as `unmapped` rather than guessed, which is also how generated fields (`Latitude (generated)`) are dropped. Carries `filter_context_known: false` because a view-level filter leaves no trace in the exported rows — measured: Power BI returned a region the Tableau view omitted, with nothing in the CSV explaining it. | `pbi-migration-validator` |
| `stamp_tableau_provenance.py` | **Links a migration input back to its Tableau origin**, so a finding filed weeks later is still reproducible. The engine's `input_manifest.json` records the local half (name, size, sha256, staged path); this adds the upstream half — site, workbook LUID, project, owner, `updatedAt`, and the Tableau **product** version. Emits a `.twbx`'s inner **member CRCs** as well as the outer hash, because zip metadata differs between downloads of identical content — and because member CRCs let a third party check their copy without either side redistributing a vendor's sample workbook (which matters when the other repo is a clean room). Confirms identity by **re-downloading and comparing sha256**: a same-named workbook that is a different build is recorded as `match: "name_only"`, never silently claimed as the source. Wired into `run_estate.py` as a best-effort phase. | `tableau-migrator`, `pbi-migration-validator` |
| `capture_tableau_oracle.py` | **Tableau's own computed values, keyed by view LUID** - the numeric oracle. **Also captures reference RENDERS** on a probe-selected ladder: `--reference-best` (recommended), or explicitly `--images` (`?resolution=high`), `--svg` (`?format=svg`) and `--pdf` (`/pdf?type=Unspecified`). `capture_tableau_reference.py`'s `server_rest` provider is *not* wired (#194). **Which rung you get depends on the customer's Tableau version, so it is PROBED, never assumed** (#403): SVG needs REST **3.29** (Cloud June 2026 / **Server 2026.2**), so an on-prem site on 2023.x-2025.x has none - but PDF reaches back to **2.8** (Server 10.5) and `resolution=high` to **2.5** (Server 10.2). `resolution=high` is measured to be **exactly 2x a dashboard's declared size with no parameter that raises it** (52/52; `standard`/`veryhigh`/`HIGH` are HTTP 400, and `vizWidth`/`vizHeight` are ignored *for dashboards* - a worksheet does honour `vizHeight`), so a text-dense dashboard can be structurally legible and content-illegible at once. The manifest records `dimensions_px` / `width_px` / `text_elements` / `page_pt` / `fontfile_count` / `vector` plus a `render_capability` block, so a consumer reads which grade of evidence it got instead of inferring it. Neither survives `data sources not connected` - `image`, `svg`, `pdf` and `data` all fail identically at the VizQL layer. Fills two sockets nothing else does: the deterministic tier's `fidelity_oracle.py` value tier reads *Power BI* values from a local Analysis Services instance (no Tableau-side number exists anywhere in its tree), and `migrate_estate.py` persists `workbook_luid` but never a **view** LUID, which is exactly the join key `/views/{id}/data` needs. Captures raw, display-formatted CSV (`"19.5%"`, not `0.195`) plus advisory format hints - normalising at capture would bake a comparison decision into the evidence. **Passes an explicit, validated `maxAge`** (default **1 minute**, configurable via **`--max-age <minutes>`**, minimum 1) on `/data`, `/image` and `/pdf` requests as well as `--reference-best` capability probes (#473) so captures are freshly computed rather than silently served from Tableau Server's query cache (governed by server/site query cache policy when omitted) — the chosen `max_age_minutes` is persisted in manifest run provenance, per-view records, leg records, and capability evidence. Classifies failures: transient (gateway 5xx/429/reset) retries with jittered backoff; a `FederatedDataSourceException` is a **credential block**, never retried, exit code 2; a version-gated SVG leg is `unsupported_api_version`, never filed as a broken view, and carries a `cause` resolving to exactly **one of three states** (#474) - `server_meets_floor` (set `TABLEAU_REST_API_VERSION` to **exactly 3.29**, never "3.29 or later": on a server advertising *exactly* the floor - what Server 2026.2 reports - every "later" value is above its own ceiling, which is the same impossible configuration one case in from the edge, caught in review of the fix, #475), `server_below_floor` (**the .env knob cannot help**: SVG needs REST 3.29 / Server 2026.2 and this server advertises less, so capture PDF instead), or `ceiling_not_established` (the conditional, never a confident instruction). ⚠️ A customer on `2025.3.3 / 3.27` was previously told to set `TABLEAU_REST_API_VERSION=3.29`, which is arithmetically impossible on their server; the manifest now records `advertised_rest_api_version` and `server_product_version` beside the client preference, and a plain `--svg` run establishes the ceiling too via the unauthenticated, unmetered `/serverinfo` - ⚠️ *establishes*, not merely *asks*: a version is taken only from a **successful** response and only when it is numerically a version, so an error page's body or a `garbage-999` leaves the cause `ceiling_not_established` rather than becoming a ceiling (#475); a 200 carrying the WRONG format is `format_mismatch` and **no file is written**; and a 200 whose body echoes our own **PAT secret or session token** is `credential_reflected` and **nothing is persisted at all** - the bytes reach `data/<luid>.csv` before any manifest exists, so refusing them is the only thing that can protect the file (the PAT *name* is redacted instead of refused, deliberately: it does not authenticate on its own and refusing it would kill a legitimate estate over a column heading). ⚠️ **Artifact filenames are built from the view LUID alone**, whose UUID shape is verified in full - `safe_slug(view_name)` is deleted, because a reflected token arriving as a view NAME was slugged and truncated into a filename no redactor could then match (#405 round 6). `_oracle/data/` therefore lists LUIDs; the manifest maps `view_name` -> `path`. ⚠️ An UNCERTIFIED but successful body (no `Content-Type`, or `text/plain`) keeps `status: ok` and its bytes, written to `_oracle/unassessable/<luid>.bin` and named `retained_path` - never under `data/`, never `.csv`, never under `path` - so a numeric consumer that gates on `status == "ok" and data["path"]`, which all four of them do, cannot read it as evidence (#480). Exit **5** when `--reference-best` was requested and NO reference was obtained - otherwise an UNDETERMINED probe requests nothing, every data leg succeeds, and the run exits 0 having captured zero images. ⚠️ **A failed `/data` no longer skips the renders** (#423): they are different endpoints, and a field capture proved they can disagree — a view whose data leg timed out twice (`HTTP 0`, `TimeoutError: read operation timed out`) produced a 905,098-byte PNG on the third batch, while a neighbour failed the same way three times across two days and has **no `image` key in any record**, which makes an equivalent visual-fidelity defect on that page *unfalsifiable* rather than merely unverified. Cost is bounded three ways: a salvage render (one whose data leg already failed) gets **one attempt and no retry budget**; the first salvage leg that fails for a reason the VIEW controls stops the rest (recorded `not_attempted`); and ⚠️ **all salvage legs share ONE deadline** (`SALVAGE_BUDGET_MULTIPLIER`, 2× the request timeout), admitting a leg only while a *whole* request still fits **and carrying that instant into the transport as an end-to-end deadline covering the WHOLE request** — a watchdog aborts the connection in any phase. That last part is not belt-and-braces: `urllib`'s timeout is per socket *operation*, so neither a trickling body (HTTP 200 after **0.479 s** on a 0.1 s timeout) nor trickling **headers** (**1.378 s** against a 0.15 s deadline) ever trips it, and a deadline applied only after `urlopen()` returns leaves connect, status line and headers unbounded. ⚠️ **The watchdog covers every phase FROM THE FIRST LIVE SOCKET ONWARD, which is not the same as "any phase"** — armed after the connection sequence, as it first was, a proxy trickling its `CONNECT` response ran **1.241 s against a 0.20 s ceiling** with the timer not yet started. The TLS handshake is bounded by the socket timeout rather than the watchdog, measurably so (a well-formed trickling record refused at **0.204 s on a 0.2 s timeout**, because `SSLSocket` applies the timeout to the handshake as a whole where a `makefile()` read applies it per `recv`). ⚠️ **Before that first socket nothing of ours applies, and that is TWO phases, not one:** name resolution, and — measured in round 5, and *not* DNS — `socket.create_connection` walking every resolved address with the full timeout applied to **each**, so N unreachable A/AAAA records cost N timeouts (**0.177 s against a 0.110 s ceiling**, timer not yet armed). **So the transport deadline is hardening on top of the retry-admission budget, not a wall-clock guarantee**; the enforced ceiling is admission. The per-phase table in `tableau_http._request` is the single statement of this, and the residual is pinned by `test_multiple_addresses_are_a_known_unbounded_phase` rather than by prose — four successive versions of that sentence each named a smaller residual than the truth. The first two bound ATTEMPTS, not wall clock: three `format_mismatch` legs — a status that deliberately does not short-circuit — were each attempted with no cross-leg limit, **539.7 s against a stated 180 s bound**. The ceiling is now hard and independent of tier count, and a `session_lost` on a leg with no attempt left no longer triggers a full `sign_in` (which runs on the SESSION policy) that the export could not have used anyway. `unsupported_api_version` and `format_mismatch` do **not** stop the rest: both are configuration faults answered instantly. ⚠️ **Every REQUESTED leg now carries a record**, so an absent key means “not requested” and nothing else, and the manifest counts AND names the views with no establishable render (`render_unestablished`) — an unassessable state that reads as a clean one is the failure this capture exists to prevent. **`--rest-timeout`** exposes what was a hardcoded 180s constant; ⚠️ **`--retry-budget` tracks it (2x) by default and must**, because the budget is charged from BEFORE attempt 1: one full-timeout failure spends half of it and two exhaust it (the run then gives up well short of `--max-attempts`, by design), so a budget frozen at 360s while the timeout rose past it would grant ZERO retries to exactly the slow failure the operator was trying to survive. An explicit `--retry-budget` is honoured, never clamped. ⚠️ The default is `max(2× timeout, timeout + first backoff)`, not a flat 2×: the backoff is an absolute 1.0 s, so for any sub-second `--rest-timeout` — which the CLI accepts, it takes a float with no minimum — 2× sits *below* the admission floor and grants ZERO retries to a full-timeout failure, the very footgun the default exists to prevent (measured: `0.25 s → budget 0.5 s vs floor 1.25 s`). ⚠️ But a **credential-only** run is exit **2**, not 3 or 5: renders are not attempted once the data leg is credential-blocked (all four routes share one VizQL render), and a render skipped for that reason inherits `source_credential` instead of counting as an independent failure - collapsing it into 3 tells an operator to debug us when they should be reauthorizing a source in Tableau. Every re-auth and retry is recorded, because silent recovery is how a truncated capture looks complete. | `pbi-migration-validator`, `tableau-migrator` |
| `tableau_render_capability.py` | **Which reference-render route this site can actually do, by probing it.** Walks the ladder `svg` -> `pdf` -> `png_high`, stops at the first rung that answers **with the format it asked for**, and reconciles the **three** numbers that disagree about capability: the `TABLEAU_REST_API_VERSION` we *send* (a client preference), the `restApiVersion` the server *advertises* via unauthenticated `/serverinfo`, and what the endpoint actually *did*. Measured, that gap is real: the same Cloud site moved 2026.2.5/3.29 -> 2026.3.0/3.30 in a week. A version-gated tier is **re-probed at its documented floor**, so "this server supports SVG" is proved rather than inferred from the advertised number - and on a client pinned below the floor the re-probe *recovers* the tier. Three rules stop a confident wrong answer: an **HTTP 200 is not proof of the format** (payload signature + Content-Type; an old server that ignores `format=svg` returns default PNG), a selection above which a rung was indeterminate is **PROVISIONAL** rather than final, and **"no tier available" needs every rung definitively refused** - a mix of gates and blocked routes is UNDETERMINED. Probe details are scrubbed through `tableau_env.redacted_note()` before being printed or serialised, because a proxy echoing `X-Tableau-Auth` puts a live token in an error body - it redacts the WHOLE value first and transforms after, because case-folding, stripping, slicing and splitting each leave a literal-matching redactor hunting a string that is no longer there (four review rounds, four call sites; `tests/test_diagnostic_redaction.py` now gates the rule rather than the sites). Carries `API_RELEASE`, Tableau's published REST-version -> release map, so "you need API 3.29" reads as "you need Server 2026.2"; six rows are **Cloud-only** and unreachable on-prem, and the published table stops at 3.29 while a live site already reports 3.30. Standalone report: `python scripts/tableau_render_capability.py --view <luid>` (exit 0 = a tier was selected). Also owns the **multi-view** orchestration `probe_render_capability()` / `apply_selected_tier()` that `--reference-best` drives - it lives here, next to the ladder, and takes its session duck-typed (`site_id`, `raw_get`, `redact_text`) so the pair stays acyclic. | `capture_tableau_oracle.py`, operators sizing up a new site |
| `capture_tableau_reference.py` | Acquires a **provenance-stamped** reference image of the source dashboard, so the builder can mimic it and the validator can grade against immutable ground truth. Providers resolved by fitness, not availability. ⚠️ **For Tableau Server/Cloud, use `capture_tableau_oracle.py --images` instead** - the `server_rest` provider here is not wired and exits 3; the REST image transport it needs already exists in that sibling script (#194). This script covers Tableau **Public** (Playwright), embedded `.twbx` thumbnails, and manual drops. Design: [`docs/reference-capture.md`](../docs/reference-capture.md). | `tableau-migrator`, `pbi-migration-validator` |
| `tableau_view_types.py` | **Tells a Tableau DASHBOARD from a WORKSHEET, by LUID** (#402). Library half of the oracle capture (no CLI). Tableau REST returns both under `/views` with **no field distinguishing them**, so a captured render could be a whole dashboard composite or one chart and nothing downstream could say which - and because a Power BI page is rebuilt from a *dashboard*, while a dashboard routinely shares its name with its principal worksheet, matching on NAME silently accepts a single visual as evidence for a whole page. The Metadata API discriminates them as separate GraphQL types; this asks it **by `luid`**, so the join is on identity rather than on a name. Two siblings already do this correctly - `assess_estate.py` (`sheets`/`dashboards`) and the engine's `twb_to_pbir.py` (its `placed` set, which is why a worksheet laid onto a dashboard never becomes its own page) - so only the capture path was blind. ⚠️ **Fails closed and never guesses:** a disabled Metadata API, an older schema with no `luid`, a transport error, or a GraphQL `errors` block each yield an EMPTY map plus a stated reason, and every view then records `unknown`. There is deliberately **no name-based fallback** - that is the join this replaces. `unknown` is a real value a consumer must handle, never a synonym for `worksheet`. Travels the one hardened HTTP round trip (`tableau_http`) via the session's `api="metadata"` override rather than opening a client of its own. | `capture_tableau_oracle.py` |
| `tableau_luid_census.py` | **Measures whether a Tableau site actually emits blank view LUIDs** (#402), so the assumption `tableau_view_types.py` rests on stays a measurement rather than a documentation claim. Tableau documents `Sheet.luid` as *blank if the worksheet is hidden*, and REST `/views` omits hidden sheets entirely - so a blank luid names no capturable view. Treating it as malformed refuses the WHOLE response, and because the query scans every workbook on the site, **one hidden sheet in one unrelated workbook turns dashboard/worksheet typing off for every captured view on the site**. ⚠️ That was not hypothetical: measured against our Tableau Cloud trial 2026-09-01, **116 blank sheet LUIDs across 5 of 48 workbooks** - 27.9% of sheets - and the pre-fix rule typed **0** views on that response where the shipped parser types 360. **Read-only and one query**: sign-in, then a single GraphQL request through the one hardened `tableau_http` round trip, no loop, no concurrency. ⚠️ **Reports counts and flags only, enforced rather than promised** - `_emit` refuses to print anything that is not an `int`/`bool`/`None`, so no workbook name, sheet name or LUID can leave it even after a careless edit; the census is built from shapes, never identities. Three verdicts, none of them the 'right' answer: `CONFIRMED` / `NOT-PRESENT` / `CANNOT-TELL`, the last distinct from the second because 'nothing here could have had one' is not evidence about the site. ⚠️ **A verdict requires having actually assessed the site.** If the shared parser refuses the response, or any workbook's collections cannot be read, the run reports `CANNOT-TELL`, exits 2, and stamps `assessable: 0` into `--json` — the counts then describe what we could read, not the site. It did not: a response carrying GraphQL `errors` beside one valid dashboard reported **NOT-PRESENT, exit 0**, which is a permanent measurement artifact calling a site clean that was never assessed. The exit code follows the verdict. | by hand, when verifying #402 on a new site |
| `group_oracle_by_workbook.py` | **Browse-time convenience over a flat oracle capture.** `capture_tableau_oracle.py` writes every view into `_oracle/{images,data}/` with the workbook association only in `oracle-manifest.json` — deliberate, because a LUID-keyed flat layout survives a workbook or view rename. This COPIES (never moves) each workbook's views into `migrations/workbooks/<slug>/reference/{images,data}/` plus a per-workbook manifest subset with counts recomputed, so a partial capture cannot read as complete. Its `data_empty` count and `data_empty_views` list are IMPORTED from `tableau_oracle_manifest.empty_classification` rather than re-implemented (#471) - a second copy of “row_count == 0” is how a subset comes to disagree with the capture it was sliced from, and the copy this replaced raised `KeyError` on an older record that never recorded one. A separate step rather than a capture flag: it re-runs at **zero REST cost** (Tableau meters `/data` and `/image` at 100 calls/hr/Creator) and needs no network, so it is testable offline. **Matches folders that already exist and never slugifies a name into a path** — no folder is reported, not created; an ambiguous name is reported, not resolved. Exit 0/1/2. ⚠️ **`--oracle` is REPEATABLE, and normally should be** (#423): a metered, timing-out capture is re-run in BATCHES, and the same view can succeed in a later batch having failed earlier — field evidence, a view whose data leg failed twice then produced both a data leg and 905,098 bytes of PNG on the third, while the workbook's `reference/` folder only ever cross-referenced the first two. Every batch given is merged **newest-successful-wins per view AND per LEG**, so the two legs may come from different captures; each promoted leg records `source_batch`, and the merged manifest carries `batches` + `merge_order_basis`. A leg wins only if its status is `ok` **and** the artifact it names is on disk — a manifest entry is a claim, and promoting a newer one whose bytes are gone would make the merged set worse than either input. Grouping one directory at a time does not merely miss a later good image: a partial re-run **overwrites** the per-workbook manifest, so a view it never captured disappears. The per-workbook manifest now also carries `render_unestablished` / `render_unestablished_views`, recomputed over the GROUPED views — that manifest is what a fidelity review opens, and the capture-wide count cannot answer it (it spans every workbook, and it is computed before grouping, so it cannot see a leg the capture obtained but the grouping could not place). When no batch established a leg, the newest batch **that has a record for THAT leg** is kept — not the newest batch overall, because a later **data-only** batch has no `image` record and taking its view wholesale erased an older batch's `image: transient`, silently reclassifying a known render gap as “never requested”. Render INTENT is likewise **unioned** across batches (`requested_renders`, `reference_required`; `requested_renders_by_batch` records who asked for what), so a batch that asked for nothing cannot retract another batch's request. ⚠️ `merge_order_basis` has **three** values, not two: `captured_at`; `captured_at, ties broken by argument order` when two candidates for a leg share a timestamp — equal stamps separate nothing, and reversing the arguments picked a different winner while the manifest still claimed time decided it (the tied legs are named in `merge_order_ties`); and `argument order` when a batch carries no timestamp anywhere. All three are warned at the console. ⚠️ An artifact the capture manifest names but that is **not on disk** is a grouping failure, not a warning: the leg is marked `not_copied` (path dropped, `*_ok` counts reduced), the workbook is reported `incomplete`, and the command exits 1 — a grouped manifest must never assert evidence it did not copy. ⚠️ The normalizer drops punctuation and case but never words, so Tableau's `" | Project : X"` suffix fails to match (same blind spot as the engine's `_norm_ds()`, upstream #145). | after `capture_tableau_oracle.py`, by hand |
| [`package_unit.py`](#package-folder-identification-616) | **The per-unit handover package (#446)** - the join nothing else performs. Engine output is keyed by sanitized workbook name (`pbip/`, `reports/`, `handover/`), oracle renders and numbers by bare **view LUID** in a flat tree *outside* the bundle, and the source asset is LUID-prefixed in a third place - so `check_reference_readiness.py` and `check_unit.py` both needed `--source`/`--oracle` arguments that **cannot be derived from the unit path**, and getting one wrong reads as *"this unit is broken"* rather than *"you did not tell me where the workbook is"*. This emits `<out>/<Unit>/` carrying `migration-spec.json` (via `parse_tableau.py`, #443), `assets/`, `fabric/`, `handover/`, the `--brief` copy as `migration-brief.md`, a unit-scoped `report.json` / `source-provenance.json` / `engine-output-receipt.json`, `oracle/{dashboard,worksheet,unknown}/{images,data}/<Object>.<ext>`, and a flat greppable `handover.md` (one finding per line, **emptied visuals first**). ⚠️ **"Unit-scoped" means DESCOPED to what the gates measurably read, then allowlisted at every level.** Three review rounds each closed one level of an allowlist and were followed by a deeper one - the collection boundary (`workbooks[0].future_nested`), then container-valued fields (`workbooks[0].model_facts.future_install_root`, `views[0].image.future_source_path`), then whole artifacts that never entered the mechanism at all (provenance, the handover slice). [`docs/review-throughput-postmortem.md`](../docs/review-throughput-postmortem.md) measured that shape - **66% of round-2+ findings share a defect class with round N-1** - and its rule is *simplify, delete, split or descope*, so round 3 DELETED the surface rather than enumerate it a fourth time. What each gate reads was measured: `report.json` -> `workbooks` must be a list plus `workbooks[].name`/`datasources[].name`; `source-provenance.json` -> `inputs[].input.sha256`, `inputs[].origin.match`, `inputs[].origin.workbook_luid`; the receipt -> `engine.version`. Everything else was engine metadata no gate consumes and is no longer shipped. Measured on the shipped `HR_Dashboard` package: `report.json` **93,175 -> 1,922 bytes**, the receipt **28,577 -> 831**, provenance **4,277 -> 1,429**, and **zero** absolute host paths or foreign unit names in any shipped manifest - with both gates returning identical exit, status and page counts. The remaining `KEEP` leaf is **scalar-only**: a container arriving there raises `UnscopedStructure` at packaging time naming the JSON path, so an unenumerated structure fails loudly instead of shipping its grandchildren. The handover slice is treated differently on purpose - it cannot be allowlisted (engine-owned, volatile, and it IS the deliverable), so its **top level** is descoped to the keys consumers read (all 46 real slices carry `workbook` and `estate`; `estate` holds the whole run's DoD status and gate counts spanning all 48 workbooks, 94,668 bytes estate-wide, and is read by nobody) while its **interior** is closed by VALUE shape, redacting absolute host paths wherever they appear so no new field name can evade it. ⚠️ Named residual: an unknown NON-path field inside `workbook` still ships; measured on the reference estate, 0 of 46 slices carry another unit's business. ⚠️ **The oracle manifest is UNTRUSTED INPUT** - written by a separate tool against a live server - and a declared render path used to be joined straight onto the capture root: `"../outside-secret.png"` copied an arbitrary file into the customer package, and an absolute path copied the file *and* wrote the host path into the packaged manifest. Now non-relative paths are refused, both sides are resolved strictly so a symlink cannot escape, containment is required, only a normalised capture-relative `packaged_from` is recorded, and the declared string is never echoed back. ⚠️ **Repackaging REPLACES, never merges** - a re-run used to leave a previous capture in place, so re-running with an empty oracle kept the old 4-view `oracle-manifest.json` and the entry gate still returned **READY, 4 ready / 0 blind** against evidence that no longer existed; the build is staged and swapped, so a crash cannot replace a good package with half of one either. Its `README.md` is the package map and is gated against the tree the packager actually writes, so prose cannot drift from the emitted `dashboard/`/`worksheet/` (singular - the directory *is* `object_identity`'s kind) or silently omit a shipped file. Every one of these rules is mutation-tested by [`tests/mutate_package_unit.py`](../tests/mutate_package_unit.py) (91 mutations, each run against the single test it must kill, plus a no-op control that must survive; exit 0 = every one caught). **Both gates then run on it with no flags.** It reuses the discovery conventions those gates already have, so it needs **zero changes to either gate** - measured across the 67-unit reference estate, every page metric is identical to the bundle-level run (220 expected / 42 ready / **178 blind**, 47 rejected). WARNING: **attribution is by IDENTITY only, and fail-closed.** One route: the oracle manifest's `workbook_luid`, matched against the LUID `source-provenance.json` records for the asset's sha256 and cross-checked against the asset filename prefix; a disagreement, or one asset mapping onto two LUIDs, attributes **nothing**. A display-name fallback was **deleted, not guarded** - it fired 0 times in 67 units, and #450 measured that class failing OPEN in `check_unit` on 360 of 360 real records. The filename prefix is a cross-check only: on a `.tds` it is a **datasource** LUID, a different namespace. `view_type` is the only type discriminator - `content_url` is `<wb>/sheets/<view>` for both kinds - and `unknown` is carried under `unknown/` and marked, never defaulted. WARNING: **`--out` must sit outside the capture tree**: the readiness gate scans the target's *grandparent*, so a package written beside the flat capture is matched against both and every page silently drops from `ready` to `unverifiable`; that layout is refused with exit 2. ⚠️ **The path budget is measured BEFORE any unit is assembled, and again on what was assembled (#476).** On a live 47-asset estate packaging crashed with `[WinError 206] The filename or extension is too long` having already written **29 of 47** packages: assembly ran in `<out>/.{unit}.staging/`, so every file in a PBIR tree was written **9 characters deeper than its final home**, and the file had no long-path awareness at all. Staging is now a hidden **constant-length** sibling (`.` + 8 hex of the unit digest), so the overhead stops scaling with the unit name the engine already repeats twice inside the tree; the retired tree of a re-package gets the same treatment, because `.{name}.replaced` made a package being REPLACED 10 characters longer than the one anything had measured. The ceilings are **imported** from [`check_path_ceiling.py`](check_path_ceiling.py) (file ≤ 259, directory ≤ 247, in **UTF-16 code units**), never re-declared here, and **all three** roots are measured — the final package, the staging sibling, and the **retired** tree a re-package renames the old one to and `shutil.rmtree` then WALKS — because none of them is unconditionally the deepest: a unit named `B` stages under a 9-character name and retires under a 10-character one. ⚠️ **The projection is the pre-flight, not the guarantee.** Blind review measured it fail OPEN: it modelled only `fabric/`, the handover slice and the root scaffold, so a valid **204-character** customer workbook filename projected a maximum of 102, packaged a **279**-character `assets/` path and exited 0. `assets/` is now resolved through the same two calls the writer makes, `data/` and `oracle/` containers and a generated `expressions.tmdl` are projected too — and whatever a future edit adds is caught regardless, because the assembled staging tree is **re-measured against what was actually written** before the swap, with nothing published when it refuses. On a host that cannot even write the tree, the filesystem's own `[WinError 206]`/`ENAMETOOLONG` is restated with the path and the remedy instead of escaping as a traceback. ⚠️ **A unit may not be named like a scratch directory.** A leading dot is a legal Tableau name, so a unit called `.d72cee2e` *is* the staging directory of a unit called `Victim`: both were reported packaged at exit 0 and only `Victim` existed on disk, because `rmtree(staging)` had deleted a finished package. One predicate now closes it in both directions — the name is refused, and `rmtree` refuses any directory this packager did not name. ⚠️ **Desktop's ceiling belongs to the TAILS, not to the build directory.** Applying 259/247 to an absolute POSIX path refused a 297-character `--out` whose package was valid at 332; absolute paths are now judged against the **host's** limits (`platform_limits()`), the package-relative shape is judged against Desktop's on every host — so a tail no Windows root could ever fit is still refused — and a package that leaves under 40 characters for the root it lands in earns a **WARNING**, never a refusal. Over the ceiling refuses the **whole run** naming the path, its length, the ceiling, the overage and how many characters `--out` must lose — or, when the package-relative tail alone is over, that **no `--out`** can rescue it. ⚠️ Raising the ceiling was rejected, not overlooked: Desktop long-path support is *"no — nothing helps"* ([`docs/windows-path-limits.md`](../docs/windows-path-limits.md)), so `\\?\` would merely turn a loud crash into a package Desktop cannot open. Exit 0 all packaged / 1 a unit the engine lists has no `pbip/` working copy / 2 usage, **including an `--out` too deep for the paths this bundle would produce** / 3 a package carries edits / 4 a unit is not self-contained / 5 a unit raised / 6 a unit - or the whole bundle - cannot be assessed, which includes a requested unit that reached no outcome bucket at all. ⚠️ **The batch is per-unit resilient, and every requested unit lands in exactly one bucket (#478).** Measured on the SES estate (47 assets, 2026-09-03): 29 units packaged, then `IA_Operation_Health_Summary_Dashboard` raised `shutil.Error: [WinError 3]` out of a plain comprehension over `sorted(units)`, and every alphabetically later unit was **never attempted and never reported** - the operator could not tell *"not packaged"* from *"packaged and fine"* without diffing directories by hand. Any raise is now caught per unit (the clause is deliberately broad; `BaseException` still passes through), named in the summary as `FAIL <unit>`, carried in `--json` under `failed[]` **with its traceback**, and the rest of the estate is still attempted. The buckets **partition the request** - `units[]` + `failed[]` + `refused[]`, measured against `requested[]` rather than against whatever survived - and any requested unit that reached none of them is named in `unaccounted[]` and cannot leave the run clean. ⚠️ **A staging directory that SURVIVES cleanup fails its unit rather than being assembled into**, because continuation is what made that reachable: `rmtree(..., ignore_errors=True)` reported a failed removal as a success, and the next build landing on that path assembled *into* it and swapped the combined contents into its own package. Removal is now verified by `exists()` on a bounded retry budget; a residue found while another exception is already propagating is recorded on that failure (`add_note` + a stderr `WARN`) rather than raised, because an exception raised out of a `finally` replaces the root cause the operator actually needs. ⚠️ **The unit's SOURCE half is completed for a datasource too (#562 S2).** A datasource unit has no handover slice, so `resolve_asset` had only a raw-stem match against `input_manifest.json` — and the harvester writes `<luid>_<name>.tdsx` while the engine strips exactly that prefix to derive the unit name, so **every** datasource package shipped `artifacts.asset: null`, no `migration-spec.json` and an empty provenance `inputs` list: nothing a semantic build can start from, reported at exit 0. The stem is now compared with the engine's own transfer-UUID prefix removed (still digest-checked against `input_manifest.json`), and when nothing upstream stamped a provenance row — `stamp_tableau_provenance.py` walks workbooks only — one is written from what THIS packager can establish first-hand: the packaged basename, the digest of the packaged bytes, and the **datasource** LUID the harvester wrote into the filename. A local `.tds` with no prefix keeps its row and earns `not_applicable` for the server LUID rather than losing its identity; the two LUID namespaces never mix. ⚠️ **`--brief` copies the dispatcher's `migration-brief.md` INTO the selected unit's package**, bytes only: the external file is git-ignored and lives outside the package, so a stateless agent handed only the package could not read the one document saying what the migration is FOR — and recording its path instead would disclose a host location while proving no availability. A supplied invalid/shared `--brief` produces one BLOCKED outcome per selected occurrence and exit 5, without package writes; an explicit JSON report is replaced instead of retaining stale success. The scoped receipt keeps `artifacts[].path` (that field alone, this unit's rows only, re-rooted at `fabric/`) so the role verifier can account for the report/model/PBIP roles a package claims. | after `run_estate.py` (+ `capture_tableau_oracle.py`), before dispatching an agent |
| `promote_unit.py` | **The phase 2 -> phase 3 ship step (#458)** - the last hop, and the one that had no tool. Copies a package's `fabric/` working copy into `migrations/{workbooks,datasources}/<slug>/fabric/`, the deliverable a customer opens. It **re-runs `check_unit.py` itself and REFUSES on a non-zero exit** - deliberate duplication, because re-running costs under a second and this is where a defect stops being a working copy and becomes a deliverable; `--force` overrides and both the override and the observed exit code are written into the promotion record, so an unchecked promotion can never look checked afterwards. Two shapes, from [`powerbi-report-gotchas` §3](../.github/skills/powerbi-report-gotchas/SKILL.md): **model per workbook** copies the *contents* of `fabric/` (the folder is named for the WORKBOOK, the model inside for the DATASOURCE, so copying the folder nests them wrongly), while `--datasource-slug <ds>` lands the model **once** under `migrations/datasources/<ds>/fabric/` and rewrites `definition.pbir` to `../../../../datasources/<ds>/fabric/<Name>.SemanticModel`. ⚠️ **The rewritten `byPath` is then resolved ON DISK, and the target must be a REAL model** - measured against `powerbi-report-author` 0.1.4 on `examples/shipping-kpis`, a `.Report` whose `byPath` names a `.SemanticModel` that exists nowhere validates `result: succeeded`, `errorCount: 0`, **exit 0**; it checks reference shape, not target, and a wrong one opens as a report with NO MODEL. "Some directory holding a `definition/`" is not enough either: the target must carry `.SemanticModel`, lie inside the migrations root, and pass the same content check a shipped model does. ⚠️ **Content, not existence — and not a FILE COUNT**: every PBIR document is PARSED (`pages.json`'s `pageOrder`, each `page.json`'s `name`, each `visual.json`'s `visual.visualType`), at the source *and* again at the destination, because an existing file is not a document - an empty `visual.json` used to count as a visual and the record then *asserted* it. Unreadable input is `CANNOT_ASSESS`, never a count and never a pass. ⚠️ **`--slug`/`--datasource-slug` must be a single safe path component** (no separators, `..`, drive letters, absolute paths or reserved device names) and every planned destination is checked for containment on **RESOLVED** paths, both sides: `execute_plan` replaces its destination, so a traversal slug is a delete outside the migration root, not a misfiling — and a *lexical* check was not a containment check at all, because a `migrations/workbooks/<slug>` junction pointing outside the root passed it and the promotion shipped there at exit 0. A failed copy or verification **rolls back**, restoring whatever deliverable was there before. ⚠️ **Content, not existence**: the shipped `definition/pages/` must enumerate real pages carrying real visuals and `definition/tables/` real TMDL tables, checked at the source *and* again at the destination - a folder count is not a content check, and on a 46-asset estate a report folder that had already passed a sign-off held only Desktop-local settings. ⚠️ **The model must also not READ from outside the deliverable** (#461): a promoted model whose partition points into the originating bundle's gitignored, prunable `data/` is the same functionally-empty artifact by a different route, and no existing gate sees it (`check_unit.py` returns its normal verdicts, `powerbi-report-author validate` is clean). Judged as *absolute AND outside*, never by matching `_runs` or a drive letter, so `set_data_folder.py`'s legitimate absolute `<slug>\data\` still promotes; a Databricks `HttpPath = "/sql/1.0/warehouses/<id>"` and a bare `"/"` in a `TableauFormula` annotation are measured false positives it deliberately excludes. Measured over run 408's 62 packaged units: **32 external references across 26 units (42%)**, 11.2 MB - of which the `File.Contents`-only subset is 23 across 17, so the defect is wider than a `File.Contents` scan shows. Findings are **redacted** in every artifact (an absolute path embeds a real USERNAME and this repo is public); the full path goes to stderr only. `--bundle` adds a **drift report** against the originating `pbip/<unit>` tree (#460's silent-loss case); it is divergence only, never provenance, and never fatal. Exit 0 promoted / 1 refused by the gate / **2 CANNOT_ASSESS** (unreadable or ambiguous package - never allowed to collapse into the clean bucket) / 3 refused on content / 4 promotion failed / **5 refused on an external data path** / **6 refused on a host path in a SHIPPED file** / 64 usage. ⚠️ **5 and 6 are separate because their remedies are**: 5 is a model READING from outside the deliverable (remedy: `set_data_folder.py`, or carry the extract in), 6 is any shipped file *carrying* an absolute host path whatever its extension (remedy: delete or sanitize the reference). The host-path scan is keyed on the SHIPMENT, not on `*.tmdl` - a `.pbip`, a `report.json` or a `visual.json` is git-TRACKED under `migrations/**` in a PUBLIC repo - and the SHIPPED half of it allows only the deliverable roots, never the phase-2 package: a promoted artifact still naming `…/packages/<Unit>/…` both leaks that path and cannot refresh on another machine. | at sign-off, after `check_unit.py` is green |
| `manifest_scope.py` | **Library, no CLI**: the ONE allowlist mechanism every shipped handover manifest is projected through, plus the declarative surface of what a package may contain. `project(payload, spec)` returns `(kept, dropped paths)` and recurses, so an unenumerated field is dropped **at whatever depth it appears** and reported by full path (`workbooks[].future_nested`), de-duplicated to one entry however many rows carry it. It exists as its own module because round-2 review found the round-1 allowlist re-implemented per manifest and stopping at the collection boundary — three fail-open leaks of the same class — and `docs/review-throughput-postmortem.md` measured that shape directly: **66% of round-2+ findings were a known class rediscovered one site at a time**. The specs are grounded, not guessed: each row allowlist is the field set measured on `_runs/407-dryrun-gates/bundle`, checked per key for foreign names and absolute paths before approval. |
| `host_paths.py` | **Library, no CLI**: THE single definition of *"does this text disclose a location on a host?"* — imported by the repo's commit gate (`set_data_folder.py --check`), by `manifest_scope.redact_host_paths` and by `package_unit._declares_unsafe_path`, so a package can never ship what a commit could not. It exists because the definition had been *copied* into three guards and two copies drifted into asking a weaker question: one anchored it with `^`, the other parsed the string as a path. Both therefore answered *"IS this a path?"*, which any prefix defeats — measured on PR #480, one host path prefixed with `HTTP 503: ` (as `classify_export_error` writes it into `retry_reasons[]`) was refused by the commit gate and **shipped** by both others. Every predicate here uses `search`, never `match`: a shipped artifact's question is containment, not shape. ⚠️ **Two questions, and which one you want depends on what you are gating.** `discloses_host_path()` is the COMMIT gate's — a *profile* root only, because it is searched over every git-tracked file and this repo's own fixtures and runbooks name build drives, UNC shares and POSIX roots on purpose. `discloses_host_location()` is what everything SHIPPED is judged by: it normalises the spelling away (percent-decoding, one separator, redundant separators, remote-URL excision) and then asks one grammar of the three ways a string can be *rooted on a host* — drive, UNC (administrative shares and extended `\\?\` included), POSIX filesystem root. Round 9 measured why the narrow one cannot serve both: a real profile path re-spelled as `\\server\C$\Users\<account>\…` or percent-encoded shipped straight past it. The two are not independent — the wide one unions the narrow one in, so shipping ⊇ commit by construction, asserted in `tests/test_package_unit.py`. |
| `capture_powerbi_pages.py` | Stable page capture, either standalone (`<report.Report> <outdir> --pid`) or package-local (`iterate --package <package> --pid <pid>`). Package mode emits **neutral receipt-v3**: `final` means sealed evidence, not measurement success. Optional `--refresh`, repeatable `--canary-table` and `--persist` consume invocation-owned A1 observations through one held Desktop/AS/catalogue binding; no supplied success objects, numeric requests or legacy/UI fallback. The post-preparation package revision is frozen before capture. Package capture requires finite positive polling/dwell, repeated identical frames and idle-only dwell; it verifies the report/model/PBIP and PID-scoped bridge binding and reloads only that PID. Subsets remain triage. **Keep `CAPTURE_SHA256`; do not edit `iteration.json`.** Finalize with separate judgement input; the next iterate needs the returned `FINAL_SHA256`. Standalone grammar is unchanged. Stability remains a progressive-render heuristic. Exit 0 evidence written / 1 standalone capture failed / 2 usage / **3 named refusal**, never a unit-completion verdict. | Before retaining Desktop screenshots and review evidence |
| `iteration_receipt.py` | **Library, no CLI; neutral receipt owner.** Reads literal v2 and v3; unknown versions refuse. V2 keeps its `outcome: incomplete` semantics and exact retained bytes. **V3 has no top-level outcome or COMPLETE predicate**; the sole COMPLETE consumer is now `check_unit.py --scope all --receipt-sha256 H`. Code-owned A1 facts may be `observed`, `refused` or `unestablished`; numeric facts are always `unestablished` for every current page/visual, including pages outside a triage capture. Readers/finalization rebuild current artifact, inventory, image and Tableau facts but never refresh, query or recollect measurements. Caller-held checksums pin the observations. The existing exact receipt/PNG sets, atomic publication/rollback and finding identity remain enforced. Central credential/host-location containment covers every string. Validation-grade Tableau evidence can support visual `pass`; oracle evidence supports only `layout_match`. |
| `current_artifact_revision.py` | **Library, no CLI.** Reuses `package_filesystem` for strict JSON, no-follow traversal and canonical portable names; no competing filesystem walker. The working revision covers every package file except `validation/iterations` and the declared model's **exact** separately hashed `.pbi/cache.abf`. All other `.pbi` files remain covered. Artifact revisions distinguish report, model and cache changes. The PBIR denominator enumerates immediate canonical page/visual directories, requires each definition with folder/name agreement, and requires unique nonempty string `pageOrder` entries without coercion. The packaging-time `contents.files` baseline is not used to reject legitimate Phase-2 edits. |
| `build_synthetic_reference.py` | Renders an **honestly-labeled SYNTHETIC** reference (HTML/CSS bar chart from real queried data, screenshotted via Playwright) when no real Tableau capture exists - e.g. the `_probe-lab/` credential-gate fixtures, whose `.twb` is a generated skeleton with nothing to screenshot. Tagged `provider: synthetic_data_chart`, `capabilities: []`, `synthetic: true` - never claims layout/validation fidelity. Deliberately **not** wired into `capture_tableau_reference.py`'s fail-closed provider chain (see its docstring). | manual / test-harness use only |

### Manual visual-reference handoff and fresh package input

After bounded automatic visual recovery, add `--manual-reference-handoff` to the existing
`group_oracle_by_workbook.py` invocation. It records and renders the residual original-Tableau
image request in `oracle-grouping-report.json`; successful visual siblings and data-only
failures are not screenshot requests. Recovery intent stays per view, and any non-grouped outcome
is a repair gap, not a request. Matching request/response context is retained by source,
view and revision, not by caption. Unknown context and uncertain delivery remain explicit.
The closed row/response contract accepts the empty Default site, canonical UUID/server/timestamp
identity, relative retained paths and bounded privacy-screened notes; unsafe/unknown fields refuse.
The caller must actually inspect supplied images and record their manual origin; file presence,
PNG structure and hashes do not prove visual inspection.

`package_unit.py --reference <reference-directory>` accepts the existing manual provider's
`manifest.json` and only its declared image members for exactly one selected workbook.
Admission happens in a **fresh target before normal sealing**, declares
`artifacts.reference = "reference"`, and leaves existing source/grade/readiness checks in charge.
The manifest must first satisfy the closed capture contract: layout/text provider capabilities,
default empty state, null numeric oracle, typed known fields and privacy-safe metadata. Overclaims
and unknown fields are rejected without rewriting; only accepted original bytes can be sealed.
An existing package, including one appearing during assembly, is not replaced; this option
cannot be combined with `--discard-package-edits`. No external override, forged REST record,
source-hash rewrite or edited-package reseal is provided.

The generated package README reports the actual brief's numeric obligation: explicitly `none`
does not make optional numeric comparison a prerequisite to permitted visual work; `required`
remains owed and unknown stays unknown. Images or missing CSV never imply a waiver.
See [reference capture](../docs/reference-capture.md) for the complete handoff and admission route.
**#664 is resolved by #672:** `check_unit` shares canonical manual-reference name/type interpretation
with reference readiness. Accepted layout/text evidence can serve both consumers without an upgrade
of its ceiling. **START_READY is not Phase-2 COMPLETE**; caller-pinned final evidence and all remaining
data, numeric, visual and history obligations still apply.

### Package-local review iteration commands

```text
python scripts\capture_powerbi_pages.py iterate --package <package> --pid <pid>
python scripts\capture_powerbi_pages.py iterate --package <package> --pid <pid> --refresh --canary-table <table> --persist
python scripts\capture_powerbi_pages.py finalize --package <package> --capture-sha256 <returned-CAPTURE_SHA256> --judgement <review.json>
python scripts\capture_powerbi_pages.py iterate --package <package> --pid <pid> --previous-sha256 <returned-FINAL_SHA256>
```

Copy the pending receipt's `judgement` object into `<review.json>` **outside the package**, then edit
that copy. Keep `completed_at: null`; the producer supplies completion time. Keep the printed
checksums outside the package too. They are compare-and-swap tokens, not signatures: recomputing a
checksum from edited evidence does not establish that the producer wrote it. No editable file inside
an iteration, additional registry or signed-proof framework is introduced.

**Receipt-v3 is neutral evidence, not COMPLETE.** `state: final` says the evidence and separate
review were retained and sealed. An observed zero-row canary, a structured measurement refusal,
unestablished data, a visual mismatch or an open finding can all be finalized. `sign_off` continues
to name all-page capture scope, not a successful measurement or completion verdict. First-pass
evidence needs no manufactured review round.

Preparation is optional and explicit. Without the A1 flags, binding/refresh/canary/persistence
facts are `unestablished` with `not_requested`; the existing bridge/PBIP checks still apply.
`--refresh` requests a **full, database-scoped** refresh, not named-table or calculate-only work.
`--canary-table` supplies a nonempty, case-insensitively unique table name per occurrence.
`--persist` requests direct `AMO_ImageSave` and its checked readback; it can align
`database.tmdl` compatibility before the frozen capture revision. The operations run in that order,
through the **same held A1 binding**, with binding rechecks after each operation and after captures.
The existing A1 operations own their deadlines; no replacement native runtime is introduced.

`generated.preparation` retains the exact requested operations and pre-preparation artifact facts;
`generated.artifact` is the post-preparation snapshot. `generated.data_evidence` contains the direct
returned observations or fixed refusal/absence reasons. A missing observation is not zero rows.
Returned canary rows are **not table totals, complete source coverage or numeric-equivalence proof**.
Observed ImageSave/readback must agree with current cache bytes, but its `commitment` remains
`UNESTABLISHED`: no durable-write, cold-reopen, receiving-machine or live/disk-equivalence claim.
These are capture-time observations, not evidence that a later reader's live model is unchanged.

Only **measurement** refusal may be retained. A missing/broken held binding, unsafe package root,
wrong pin, changing frozen snapshot or failed persistence/publication authority refuses the
iteration; it cannot be downgraded into a final refused measurement. Existing local race and
token-authenticity limits still apply.

The retained-role vocabulary is four roles, with only the first two instantiated:

| Role | Cardinality in R1 | Evidence boundary |
|---|---|---|
| Receipt JSON (`iteration.json`) | 1 | Producer-owned, pinned by the externally retained token. |
| Power BI page PNGs (`pages/`) | One per captured page | Exact canonical roles, hashes, sizes and decodable PNGs. |
| Certified Tableau CSV operands | 0 | Explicit absence: this version does not collect them. |
| Typed DAX envelopes | 0 | Explicit absence: this version does not collect them. |

There is no CSV materializer, typed transport, comparator, numeric replay or supplied numeric-result
route. Purported successful numeric roles refuse, as do extra files. Numeric facts remain
`unestablished` regardless of the brief. Neither request nor reviewer can supply `numeric_obligation`;
R1 does not parse or copy numeric-scope policy and has no dependency on PR #624.

Finalization compares the capture token with the **same receipt bytes parsed by the full-chain
reader**, not a separate file hash. All PID/bridge operations precede the last package/chain
snapshot. Publication stages the final bytes beside the receipt, atomically displaces the pending
file into `.iteration.pending`, and publishes without overwriting a concurrently recreated receipt.
It then rederives the package facts and validates the full receipt/PNG chain, including the exact
intended final bytes. A detected post-publication change restores the exact pending receipt and
returns `FINALIZATION_CHANGED`; publication failure returns `FINALIZATION_WRITE_FAILED`.
After retiring the pending sibling, publication calls the same strict `read_chain(package,
expected_sha256)` used by current-final consumers. It requires the producer's final checksum,
rebuilds current package/report/model/cache and reference facts, and verifies the exact entire
receipt/PNG chain. This is the literal final snapshot: **no filesystem-sensitive operation follows
it on success**. A later authoritative read repeats those checks; deleting a marker cannot make
stale artifacts or receipts authoritative.

If rollback cannot restore the pending bytes, `FINALIZATION_ROLLBACK_FAILED` returns no final token.
Keep any pending sibling for recovery, but **do not assume one exists**: marker recreation and final
withdrawal can both fail, leaving a stale final file on disk. Independent authority reads still
refuse it without needing another write. Retain the successfully returned `FINAL_SHA256` outside
the package and pass it to `read_chain`; computing a token from a surviving file is not a trusted
invocation. This is a local snapshot/rollback boundary, not a package lock or a filesystem transaction.

`read_history(package)` is explicitly **non-authoritative**: it checks retained receipt/PNG bytes,
predecessor links and closed file sets, but allows pending captures and historical artifacts that
differ from current work. Allocation, post-capture checking and the pending side of finalization use
it so legitimate edits can start the next iteration. It must not be used to consume current-final
authority; publication and its `finalize` CLI consumer go through the strict reader instead.

`pass` requires an admitted validation-grade Tableau reference and a stable Power BI capture.
`layout_match` records a narrower comparison, including an oracle's layout/text ceiling; it is not a
full visual pass. In v3, reviewer `numeric_results` contain only the `visual_id` reference to
generated numeric evidence and `finding_ids`; numeric statuses/hashes cannot be supplied.
Visual statuses still need a completed judgement; generated numeric absences do not disappear
from the current denominator. PNG validation uses the existing Pillow extra (`uv sync --extra showcase`);
if unavailable it refuses instead of treating a hash as an image certificate.

Literal v2 remains readable/finalizable with its original data `pending`, numeric `unverified`/null
hashes and `outcome: incomplete`. Its semantics are not relabelled as v3. Exactly one unchanged
**final-v2 → v3** successor may retain upgraded evidence without claiming an artifact change.
Unchanged **v3 → v3** refuses, and v3 cannot downgrade to v2 to repeat that exception. An ordinary
changed v3 successor requires both a predecessor open finding and an observable package/report/
model/cache revision delta. This records an **association, not causality**. Retained v2 bytes,
checksums and findings stay unchanged.

Finding identity includes its limitation pointer/hash. To accept a limitation in a later iteration,
carry that binding from its first appearance; do not rewrite an old ID. Legal transitions are
`still_open` to itself, `resolved` or a prebound `accepted_limitation`; terminal states remain
terminal. Prior findings never disappear. A new finding must name the captured inventory.

The Phase-1 role verifier consumes packaging-time `contents.files` and cohort evidence; it is not
reapplied as a gate on mutable Phase-2 revisions. The producer reuses the strict filesystem/JSON
primitives without importing that earlier gate. Working revisions already pin the brief and manifest
bytes without establishing numeric scope. Direct controls in the four existing capture/receipt test
files establish software routing, lifecycle and publication behavior only—not native qualification
or customer fidelity.

### Supervised provenance computation (`run_estate.py`, #576 / PR #603)

One spawned leaf process does the provenance work. A **parent-side daemon transport thread**, not a
second worker, receives length-prefixed UTF-8 JSON and validates into temporary state. The supervising
thread waits only on a one-slot mailbox, checks its absolute monotonic deadline **after validation and
before committing evidence**, and owns the sole atomic publication attempt. No pickle is decoded from
the worker. An incomplete header/body can block the helper, never the supervising thread.

The closed protocol allows at most **4,096 physical inputs**, **4,096 members per input**, **2 MiB per
frame**, **16 MiB of frames per phase**, and **32,800 messages**. Over-limit or malformed data is refused,
not truncated into a success. Discovery is unique; checkpoints are contiguous; operation counters and
order are checked; snapshots and terminal results reconcile to discovery and accepted fingerprints.
After the local pass, a single boolean `lookup-intent` establishes whether live work was requested.
`success` requires sign-in, inventory, paired content progress for every usable input, completed scrub,
an accepted safe snapshot, and completed sign-out before terminal, in that order. Content counters
advance only for distinct attempts; their `total` is the distinct matched LUID count known so far, not
an input count or a forecast. Every result's distinct origin LUID count must be backed by recorded
content attempts, including partial/failed results. Attempts cannot exceed matched identities, matched
identities cannot exceed returned inventory rows, and successful results reconcile to their distinct scrubbed LUIDs.
Cache hits and inventory misses do not invent downloads (#582). `local_only` requires explicit local intent, no live
operation even started, and an independently local-only result. Missing or reordered applicable stages,
unsupported success, and live-to-local relabelling are `worker-protocol-invalid`, published fail-closed
with exit 11.
Terminal is final: the parent requires EOF and refuses trailing messages. Missing fingerprints become
ordinal placeholders. Early checkpoints contain derived data only; copied identity fields are admitted
only in the scrubbed result, never in progress or error diagnostics.

Scrubbed input names are bounded to 255 characters and validated as basenames for the executing
platform, using lexical pure paths only: no open, stat, resolve or other filesystem lookup. POSIX
permits `:` and literal backslash; Windows alone applies its punctuation, device-name and trailing
space/period restrictions. Both reject paths, C0/DEL/C1 controls, empty names and dot segments. The
control rejection is a provenance-identity ceiling, not a claim about POSIX filename legality. Names remain in the
artifact, never in protocol diagnostics or progress.

Spawn/setup is charged to the computation budget; start failures are typed failures. Cooperative
cancellation uses a **lock-free, single-writer shared byte** and is checked before expensive work.
Enforcement remains termination: **0.5 s terminate/join**, then **1.0 s kill/join** if needed, plus a
**0.1 s helper join**. A cleanup exception, unknown liveness or missing exit code is
`worker-reap-failed`, never success; accepted evidence survives as partial and `run_estate` exits 11.
An unaccounted child is removed from CPython's automatic unbounded exit-join set without claiming it
disappeared. The artifact reports that failure.

This is a direct-worker computation bound, **not** descendant supervision, a standalone-stamper
deadline, pagination, or a deadline on the parent's filesystem publication. Path validation still runs
before provenance. Strict-JSON atomic replacement and prior-byte preservation are unchanged.
The successful inventory parse emits exactly one `inventory-facts` event before inventory completion
or content work. It carries bounded numeric/null counts and an `invalid_fields` count distinguishing
malformed metadata from absent metadata; it carries no remote text or classification. A failed request
or parse emits an exclusive `inventory-failed` marker instead. The parent requires exactly one parse
outcome for inventory completion and independently classifies the facts. Offline facts, duplicate or
out-of-order outcomes, contradictory worker claims, and impossible count relationships are protocol
errors even on non-success paths. Once facts or a failure have arrived, no terminal of any status is
accepted before recorded inventory completion. A deadline/cancellation without a terminal still retains
accepted pagination facts and local fingerprints; it does not require a future completion.

The single cached page is complete only when valid first-page facts prove it (including an explicit
1,000/1,000 total), or it is shorter than the requested 1,000 rows without contradictory metadata.
A trustworthy total greater than the returned count proves `truncated` **before** unrelated malformed
page-number/page-size fields are considered. Malformed or contradictory facts never prove `complete`.
The parent retains a typed `inventory-truncated` or `inventory-cannot-establish` finding through later
cancellation, failure or deadline expiry, even if the worker omits it. These outcomes preserve local
fingerprints and exit 11 before later phases; offline and proven-complete inventories gain no pagination
finding. This legacy origin observation remains one inventory request, not multi-page fetching.
Published inputs acquire separate current pages inside their existing content phases, as described
below; these do not overwrite the initial inventory facts or distinct-workbook progress counters.

**Published dependency association — partial #562 prerequisite P.** The only published authority is
the optional `origin.published_dependencies` block inside the existing `source-provenance.json`.
Acquisition never uses a selected provider package, spec addition, sidecar or registry to establish
the datasource LUID. Existing legacy artifacts remain readable, **not START_READY**, without
inventing this block. A newly assessed input may omit it on success **only when the held-byte
assessment completed with zero published occurrences**.

The block has exactly `schema: "tableau-published-dependencies/v1"`, `source_sha256` (the outer
`input.sha256`), `workbook_luid` (the outer origin LUID), `source_match`
(`sha256`, `revision_same`, or `unestablished`), and `rows`. Every published occurrence keeps its
zero-based ordinal in the parser's datasource sequence, including gaps for embedded sources;
the parser's `Parameters` pseudo-source is excluded. Repeated published keys are not deduplicated.
Each row has exactly `source_ordinal`, the parser's exact normalized `published_key`, `state`,
`candidate_count`, and **only for `resolved`**, `datasource_luid`. Ordinals/counts are bounded,
non-boolean integers; only `cannot_establish` has a null count.

| Row state | Required evidence |
|---|---|
| `resolved` | Confirmed held, initial remote and freshly rechecked remote bytes/revision; unique workbook identity; complete independently visible catalog; exactly one candidate; valid datasource LUID and matching detail. |
| `missing` | Legacy artifact-reader representation only. The current acquisition has no independently validated absence mode: neither the producer nor the current worker protocol admits this state, even with consistent private/public zero counts. |
| `ambiguous` | Confirmed source and complete catalog with more than one candidate, including duplicate rows; no chosen LUID. |
| `cannot_establish` | Source identity/revision, visibility, completeness or detail cannot be established; null count, no LUID. |

The stamper fingerprints and parses **one retained immutable byte buffer**, including archive member
fingerprints, with the existing parser identity helpers, not a second name normalizer. A `.twbx`
selects the first `.twb` in archive order, exactly as `parse_tableau.load_twb_root` does; it does not
sort members or silently drop a multi-member workbook. A completed empty assessment is `[]`; unreadable,
malformed, unsupported or unparseable content is `null` in the private checkpoint and a typed
`published-assessment-unavailable` phase error, never clean absence. A physical read failure retains
the existing typed unavailable-input result. Known occurrences without origin/lookup authority produce
`published-authority-unavailable`, also non-success, retaining safe local evidence. A missing, empty,
overlong or control-containing parser key likewise cannot produce a successful standalone capture;
the producer withholds that block, and supervision refuses any malformed authority that still
arrives, rather than sanitizing the key into another datasource identity.

REST uses only the **case-preserved, decoded `derived-from` content URL segment**, on the matching
source site/server. Accepted routes are `<base>/datasources/<content-url>` and
`<base>/t/<site>/datasources/<content-url>`, with no query or a numeric `rev` query (including dotted
revisions). The source site and any URL site must match the lookup site; an omitted/empty source site
is usable only for the configured default site. Scheme, hostname, **effective** port and the full
case-sensitive configured base-path segments must agree. Default ports may be explicit or implicit.
Segments are decoded once and must equal the parser's content URL; malformed escapes, encoded
separators, traversal, double encoding, credentials in URLs, fragments, unrelated routes and unknown
query forms cannot trigger a catalog lookup. Unsupported shapes remain `cannot_establish`, not a
provider/display-name fallback. The unqualified `derived-from` form with a separate `site` attribute
is documented in Tableau/Salesforce
[Downloading a Published Extract Using Tabcmd](https://help.salesforce.com/s/articleView?id=001458254&language=en_US&type=1).
The explicit site route and configured-base variants have synthetic production-path controls, not a
claim of live qualification on every server/proxy topology.

Normalized keys, display names, captions, repository IDs, connection-name fallbacks and provider
choices never become catalog queries. Each eligible physical input first acquires a **fresh workbook
inventory**, rechecking matching-candidate and LUID uniqueness against its input-bound workbook identity.
A new same-named candidate, changed identity, incomplete page or failed read withholds published
authority; matching content alone cannot override those observations. The run-cached legacy origin
remains an initial observation, not a substitute for this current page.
Immediately after catalog/detail acquisition and before the
evidence envelope or public block, the remote workbook content is fetched again without using or
overwriting the initial content cache. Its SHA/revision must agree with **both** the held source and
the initial remote observation; comparable contradictory revisions refuse even if a raw SHA agrees.
The existing revision-key comparison accepts unchanged repacked archives. The held source path is
then rehashed, after the last remote request. Changed, unreadable or uncomparable current content
retains the original occurrences as `unestablished` / `cannot_establish`, with no selected LUID.
An initially unestablished source does not trigger this extra request. Eligible physical published
inputs each need one recheck, including repeated inputs of the same LUID; the existing progress
counter still measures **distinct workbook identities attempted**, not total HTTP downloads.

The existing urllib client uses one non-redirecting opener for every request, including sign-in,
inventory, content, visibility, catalog and detail. **Every 3xx is refused, including same-origin
redirects**; neither the PAT body nor `X-Tableau-Auth` is resent to a redirect target. GET requests
send `Cache-Control: no-cache` and `Pragma: no-cache` to require cache revalidation. Real two-server
loopback controls cover all 300–399 statuses and both token forwarding and forged catalog/detail
authority; they do not substitute a second HTTP client.

Datasource pagination is deliberately stricter than the older workbook inventory rule above:
all first-page facts must be explicit, valid and complete. A short page alone does not suffice.
Even an explicitly complete **empty filtered** page is `cannot_establish` with a null count, never
certified `missing`: search-index lag can hide a current matching datasource. There is no existing
independent authoritative absence lookup in this acquisition path, so it does not invent a detail
LUID from another hint or add a fallback enumeration.
Visibility is established separately by querying the signed-in user's matching REST identity and
requiring a server/site administrator role. Tableau documents that non-administrators see only
datasources they have permission to connect to: see
[Query Data Sources](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_data_sources.htm#query_data_sources)
and [Query User On Site](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_users_and_groups.htm#query_user_on_site).
The selected datasource detail must agree exactly on ID, content URL, name and update timestamp.
REST search-index freshness is not guaranteed; missing, renamed or stale detail therefore refuses.
Visibility, filtered catalog and selected detail are reacquired for each distinct association.
Only duplicate occurrences **within the same physical input block** share that acquisition, including
its failure; a second input or a different content URL makes fresh requests even for the same
datasource LUID. There is no run-global published cache or cache-invalidation state machine.

The private fingerprint checkpoint always binds assessment, even when empty or unassessable, alongside
original ordinals and **digests** of parser keys. A paired `launch_identity` binds digests of the launched
absolute file identity, basename and any harvested workbook LUID **before live work**. The shipping
parent requires the explicit `tableau-provenance-worker/2` capability on discovery. It independently
collects the ordered input paths once, in the existing deadline-bounded transport/validation thread,
and binds each checkpoint to that **parent-owned** launch set before accepting it. It does not derive
the launch set from a second worker message. Tests may supply the same fixed tuple directly.
Swapping checkpoints and all corresponding private payloads therefore cannot move authority between
two physical inputs, even when their bytes are identical. A public authority also requires the
scrubbed basename to retain its parent-bound identity.

After inventory selection,
one private `workbook-identity` event binds the independently observed workbook LUID digest to that
same launched input, before download and origin construction. The supervisor checks the event's
ordinal, phase, uniqueness and file digest, then reconciles the final LUID against this observation
and any confirmed harvested LUID. Altering both final LUID fields does not alter that evidence, even
when two different workbooks have identical bytes.

Each assessed, valid nonempty association emits one `published-evidence` envelope **before** public
block construction, inside that input's content phase and after its identity event. It retains the
held-source SHA, the actual final-path rehash (null when unreadable), `current_remote` (fresh
`sha256` and nullable `revision_key`, or null when the recheck was unavailable/not applicable),
`current_workbook` (fresh numeric pagination facts, matching-candidate/LUID counts and selected
workbook LUID digest, or null on an unavailable/inapplicable read), the initial source-match state,
and each occurrence's ordered key digest, state, count and selected **LUID digest**.
Each private row also carries its `acquisition`: the visibility result, numeric catalog pagination
facts, candidate LUID digest, and independent digests of the candidate/detail ID, content URL, name
and update timestamp. Unavailable observations remain null; an unattempted association has null
acquisition. No URL, path,
catalog row, copied name, credential or response/exception text is included. The parent validates
the closed shape and input/index/phase binding, independently compares the fresh remote observation
to the checkpoint's held source and the initial origin, checks current workbook identity/uniqueness,
and independently requires complete visible catalog facts and agreeing candidate/detail digests for
each resolved private row. It derives workbook/remote/local downgrades and requires the final public
outcomes to reconcile exactly. Editing only snapshot/terminal source-match,
outcome/count or selected LUID cannot supersede the earlier acquisition evidence. This is not a
second REST client or an atomic server snapshot: a later server change still requires another capture.
Legacy artifact reading remains separate and unchanged; it does not grant an old `missing` row entry
to the current framed worker protocol. These changes do not add package or START_READY consumers.

The supervisor reconciles the complete ordered row sequence against the checkpoint and validates
the nested closed shape, SHA, identity, revision evidence and state/cardinality contract. Presence is
bidirectional: a block cannot invent rows after empty/unassessable assessment, and a successful result
cannot lose a known occurrence or an unassessable assessment into legacy absence.
Missing, surplus, duplicate-ordinal, reordered, malformed or unknown nested rows/fields are protocol
faults, not cleaned legacy origins. An occurrence with no valid parser key remains in the private
assessment, with a typed non-success error and no public dependency authority; it is never filled
from another identity hint. Checkpoint-only fields never enter the published artifact, including
on interruption. Legacy artifact normalization remains compatible separately. Explicitly injected
legacy transport stand-ins remain observation-only and cannot issue P authority without parent
input bindings. They are not a fallback for the current shipping worker: deleting its protocol
marker or either/both assessment fields is a protocol fault, not a legacy downgrade.

Configured server URLs are validated **before client construction copies a public origin and before
any live request**. Userinfo, query/fragment delimiters (even empty), malformed origins, unsupported
schemes and ambiguous base paths produce a typed lookup refusal without copying the URL to output.
HTTP loopback, Server/Cloud, decoded base paths and explicit/default ports have controls; real urllib
loopback tests verify both supported calls and zero calls for refused configuration. One C0/DEL/C1
predicate is shared by decoded URL segments, producer keys and supervisor text/identity validation,
without banning ordinary Unicode. All authority-bearing REST JSON (sign-in, inventory, user,
catalog and detail) uses one decoder that rejects duplicate object keys, NaN/Infinity and numeric
overflow to nonfinite floats before consuming any identity.

Before scrub, the producer retains digests of authority-bearing identities and source fields.
After scrub it reconciles them before sending a safe snapshot or publishing standalone output.
Redaction may change display metadata; it may not turn a parser key, workbook/datasource LUID,
server/site or source identity into a different successful association. Such a collision withholds
the live origin and records `published-identity-redacted` as non-success; redaction is never weakened
to keep an identity. The standalone CLI publishes normalized nonempty evidence and returns exit 1
for a non-success phase, including cleanup failures, rather than returning 0 merely for writing a file.

Catalog permission errors, timeouts and unreadable replies produce `cannot_establish` without
destroying otherwise valid origin evidence or copying catalog rows/exception text into diagnostics.
The existing absolute supervisor deadline still covers acquisition; cancellation/expiry retains
accepted safe provenance (or fingerprints before scrub) as non-success. A successful provenance
**phase** does not imply that its dependency rows resolved. These are local observation-time
associations, not an atomic server snapshot or proof against a change after the final recheck.
Catalog/detail caches remain run-local, not continuously refreshed, and an uncooperative intermediary
or server that ignores revalidation is outside this observation-time guarantee.

**Claim ceiling:** P does not thin consumer models, rewrite PBIR, choose a provider package, transport
the association through packaging, make consumers `START_READY`, support `COMPLETE`, or change
promotion. Consumer binding/transport C remains separate and blocked on P; promotion remains #57.
Under the B-refined boundary, the temporary **whole-estate exit 11 stays in place** for known
published occurrences without authority. Narrowing that stop belongs to a separate stacked C/S2
consumer change, not P. The unchanged offline E2E fixture still expects its former exit-0 behavior;
that integration control is explicitly red until the separately scoped consumer/harness work lands.
Passing P's direct suites is not a green whole-repository CI or an integration-readiness claim.

### Package folder identification (#616)

Assembly and package inspect/bind/sanitize share the private, pure
`interpret_folder_parameter(Sequence[tuple[str, bytes]])` decision over **one complete owned model**.
It selects by executable first-argument use, never by caption, declaration order, a `data` substring,
equal values, or a preferred name recorded in the manifest.

Supported forms are a literal-valued named expression `P` (optional parameter metadata),
`File.Contents(P & "tail" [ & "more" ...], ...)`, and `Folder.Files(P, ...)` /
`Folder.Contents(P, ...)`. File tails must concatenate to a safe, nonempty member path, with a
separator supplied by the root or the tail. All literal pieces and mixed separators are retained.
TMDL single-quoted names/doubled apostrophes and M quoted identifiers/doubled quotes join by decoded,
case-sensitive identity; legacy M-quoted declarations remain supported. Only the selected value
bytes change: names, references, metadata, BOM and line endings are not rewritten.
Non-M partition source bodies are skipped to their TMDL indentation boundary, including DAX
identifiers or comments that resemble M headers. Record field names and field selectors are not
executable parameter references or reader calls; references in record **values** still count.

| Decision | Assembly | Package inspect / bind / sanitize |
|---|---|---|
| `selected` | Reuse the single root; existing readability, native/foreign/UNC, member-copy and size gates still apply. Named-file reads copy only named members; folder readers retain whole-folder behavior. | Use the same root and exact value span under existing S1/S2, provider, boundary and transaction checks. |
| `zero` | Keep the existing literal-file/materialization route and collision-free generated parameter allocation; re-interpret the resulting model. | N/A only when neither shipped rows nor binding metadata claims a root is needed. |
| `refused` | Exit **5**, before source enumeration/copy or localization writes. | Exit **1**, without staging or a successful inspection. |
| `unassessable` | Exit **6**, never a silent literal fallback. | Exit **3**, never N/A or a guessed rewrite. |

Multiple roots (`folder_parameter_multiple`), duplicate decoded identities
(`folder_parameter_identity_ambiguous`), conflicting executable roles
(`folder_parameter_role_conflict`), unsafe/computed tails and unsupported values/readers refuse with
fixed codes. Mixing a root with bare absolute file reads refuses as
`folder_parameter_generated_root`; root unification is deferred. Malformed M, failed strict UTF-8
decoding and unextractable carriers yield fixed `folder_parameter_malformed_m`,
`folder_parameter_decode` or `folder_parameter_unextractable_m` codes, without private input text.
This is a finite interpretation, not a general M parser: aliases/functions, dynamic filenames,
M character-escape expansion and unsupported enclosing layouts are not inferred or evaluated.
Nested block comments exceed the reused lexer's contract and are unassessable, not partially scanned.
The bounded unfinished-expression check also refuses trailing `and`, `or`, `as`, `is`, `meta` and
`otherwise` without a right operand. Strings, comments and quoted identifiers do not trigger it.

Use `python scripts\set_data_folder.py --package <absolute-package> --inspect`, bind with the same
command without `--inspect`, then rebind after a move. Before transfer, run it with `--sanitize`
to restore `<PACKAGE_ROOT>`; bind at the recipient. `--package` no longer calls the independent
checkout rewriter. Checkout localize / `--sanitize` / `--check` behavior is unchanged.
No source-access, reference, fidelity or readiness verdict is improved by selection or binding:
assembly remains **ASSEMBLED**, and applicable binding inspections remain **UNVALIDATED**.

### S2 package preparation

The supported dispatcher/`tableau-migrator` Migrate/Continue route owns construction, applicable
binding and the final current package-only check. Keep the explicit selected run and exact unit;
never choose the latest run. Derive paths from that run, not from a stored readiness result.
Construct provider first with each unit's own brief, bind using the same exact provider roots, then
run `check_reference_readiness.py <provider-package> <consumer-package> --json - --quiet` on the
complete current cohort (one package for an owned model). The retained #562 authority and S2 select
providers, with no name/spec fallback. The executable guard is in `tableau-migrator` step 7.
No validator/builder dispatch unless **process exit == 0 AND status == "START_READY"** in exactly one
fresh JSON object. Malformed/multiple/missing JSON, disagreement, ASSEMBLED, BOUND, ordinary READY,
NOT_APPLICABLE, stored status and completed todos block. Preserve edit refusal on Continue.

`package_unit.py --brief` requires **one selected unit**; use a separate invocation and brief per
unit, not one brief broadcast over an estate. The typed unit/scope and whole-message host-location
and credential containment checks run before assembly. Unsafe text is refused without copying,
redacting or echoing it. Source resolution selects one input-manifest row by logical unit identity,
then derives its physical basename from the native `staged_input_path` (a filename-shaped `name` is
the legacy fallback only when that path is absent). The raw handover's portable source leaf must
agree before the separate shipped handover is redacted. The selecting row stays attached to the
walked path through digest validation; wrong/foreign paths, unresolved declared assets and ambiguous
rows or candidates refuse.

A current per-unit brief starts with this v2 metadata, followed by the chosen fidelity/autonomy/
refresh instructions. Replace the unit and scope with the commissioned values. `required` is an
example, not permission to guess the numeric obligation; only an explicit user decision earns `none`.

```toml
+++
schema = "phase1-start-ready/v2"
unit = "<exact-unit>"
scope = "model_and_report"
fallback_authorization = "stop"
numeric_obligation = "required"
+++
```

### Construction status (#614)

This section supersedes the legacy exit/status vocabulary embedded in the long `package_unit.py`
catalog row above; its historical construction, containment and attribution evidence remains valid.

`package_unit.py` is a construction-only command. `--assemble-only` is an explicit alias and status
marker for that behavior, not a second construction path. **Once the bundle and selected cohort
are established**, each original requested occurrence has one constructor-owned terminal
outcome: **ASSEMBLED** or **BLOCKED**. Repeated requests, duplicate engine identities and kind
collisions retain their multiplicity; filtering never shrinks the denominator.

**ASSEMBLED proves this attempt's exact candidate** at the canonical final location: native directory
identity and actual spelling, held candidate bytes, final integrity, and no competing discoverable
transaction marker. An earlier sibling survives a later NTFS alias request; distinct native objects
are not merged because Unicode `casefold()` agrees. An unchanged prior package, retired-only output
or uncertain authority is not this occurrence's successful construction. Failures, edit refusals,
unassessable inputs and unattempted admitted occurrences are BLOCKED. Any BLOCKED occurrence keeps
the command nonzero while preserving valid ASSEMBLED siblings. A missing working copy,
non-self-contained package, unbound data path or absent oracle remains diagnostic output, not an
independent construction failure.

The legacy ordered `units` / `failed` / `refused` / `unaccounted` JSON buckets and their totals
remain present, explicitly marked `construction_only`; they and the `construction` projection derive
from terminal occurrence slots, not independently appended outcomes. `units` and its projection
preserve provider-first publication order. A constructor's completed outcome survives post-return,
post-slot and provider-bookkeeping interruptions; later unattempted occurrences remain named.
The legacy manifest `packaged` boolean remains an engine-working-copy indicator only, with adjacent
`packaged_semantics`; it is not a success label.

**ASSEMBLED can coexist with a cleanup finding, never a clean exit.** If the exact final candidate
is verified and retained scratch is proved nondiscoverable, its directory may remain ASSEMBLED while
`cleanup_findings` explains the nonzero result. This does not add a second raw failure row for the
same occurrence. A competing or unassessable marker prevents ASSEMBLED. Ordinary edit refusals retain
exit 3, modeled construction/cleanup failures use 5, and cannot-assess, accounting and interruption
cases retain exit 6 and their precedence; legacy exits 1 and 4 are not repurposed. Diagnostics retain
typed input roles/vetted basenames and UTF-16 budget measurements, not arbitrary exception text or
host paths.

**Supplied-brief refusal is a modeled construction outcome, exit 5.** A single brief cannot serve
multiple original occurrences: otherwise-unblocked occurrences get `brief_requires_one_unit`;
duplicate-engine, repeated-request and workbook/datasource-collision reasons remain more specific.
A missing, non-file or unreadable supplied brief gets `brief_unreadable`, with the input role but
never the caller's basename/path or exception text. Valid single-unit bytes, unit/scope validation
and whole-message privacy refusal are unchanged. Omitting `--brief` still permits diagnostic
assembly and records the missing brief; it is not the same as supplying an invalid one.

**Brief refusal precedes package writes.** Without `--json`, or with an external `--json`, it creates
no `--out`, staging/candidate tree, unit directory, manifest or package marker. Existing package
bytes and the caller's brief remain unchanged, including with `--discard-package-edits`. Before any
output/report parent creation or publication, `--json` must pass read-only destination admission.
An address equal to `--brief` or `--out`, within a prospective selected-unit package or its
staging/retired roots, within any existing package tree under `--out` (including unselected/nested
packages), or sharing a protected file's native identity is **usage exit 2**, not a modeled brief
refusal. A damaged marker entry (including an empty or nonempty directory replacing
`package-manifest.json`) still protects its package and native file aliases. Beneath `--out`, report
paths may not contain a package-marker name or any reserved scratch segment (`.<digest>` or
`.<digest>~`), whether its unit is selected, unselected or absent. This prevents reporting itself
from creating a package boundary or transaction namespace and poisoning later ordinary reporting.
Symlinks/reparses are not followed, and unassessable boundaries/identities fail closed.
The generic diagnostic exposes no caller path, basename or exception text; rejection changes no
brief, stale target, output directory or package marker.

An ordinary non-package report such as `<out>/reporting/status.json` remains allowed and may create
only its reporting parents/file during brief refusal; benign external reports remain allowed too.
The complete occurrence report replaces stale JSON through a same-directory file replacement after
serialization; this is ordinary writable-report publication, not durable recovery after a hard kill
or power loss. Quiet/assemble-only modes keep the same refusal and reporting semantics.

Syntax errors, missing mandatory switches, an invalid bundle and unknown units remain argparse
usage exit 2, outside occurrence reporting; they do not replace a prior report. Exits 1 and 4 are
not reused for brief refusal.

**ASSEMBLED is never START_READY.** The existing `dispatch_readiness` object records
`availability: AVAILABLE` (the final package checker exists) and `status: NOT_EVALUATED` (this
constructor did not run it). Its message says diagnostic construction dispatched no agent and the
complete current cohort still needs START_READY with process exit 0. No dispatch boolean, schema
or aggregate state is added; construction neither calls nor reimplements the final checker.

Only explicit **Export diagnostics** selects `--assemble-only` in the orchestrated route; ordinary
Migrate/Continue failure must **never fall back** to it. Default and explicit low-level construction
retain the same totals, exits, file sets and nonvolatile bytes, apart from `mode.explicit`.
Default low-level `--quiet` remains silent; explicit quiet diagnostics retain the terminal notice.
The **orchestrator always prints a terminal outcome, even with quiet helpers**: exact run/unit,
discovered inputs, completed stages, blocking stage, authoritative verdict/exit and **one executable
next action** addressing that stage. Source/reference/brief/provider/data authority precedes binding;
an existing manifest or package is not a stored permission to dispatch.

`package_role_identity.py` re-runs no-follow S1 at the S2 entry seam rather than trusting an earlier
clearance. It reads **package-local P through `VerifiedPackage.read_verified_member`**, using the
single provenance row matched to the held source filename/SHA. P's source SHA, workbook-origin LUID,
exact schema, source-match state, row cardinalities and increasing ordinals must agree. The held spec
owns topology and every physical published occurrence: P joins it one-to-one by ordinal and exact key,
with equal counts. Optional spec LUIDs corroborate P; they never select a provider or rescue absent P.

Only supplied datasource packages whose own source identity equals P's acquired datasource LUID
are candidates. Canonical UUID hex is case-insensitive; published keys are exact. Zero candidates
gives `provider_missing` (or `provider_luid_contradiction` for an exact-key foreign LUID), duplicates
give `provider_ambiguous`, and a sole provider with a missing/different key gives
`provider_key_contradiction`. There is **no key-only, display-name, package-name or folder fallback**.
S2-clean provider roles, exactly one resolved model and the actual complete PBIR/model binding
remain required. The selected input ordinal alone flows to data-access inheritance and binding;
caller order and repeated occurrences never pick/deduplicate by name.

Missing P on a published consumer is `CANNOT_ESTABLISH / 3` at `role_identity`
(`published_dependency_authority_missing`); valid cannot-establish rows or unestablished source
matching use `published_dependency_authority_unestablished`. Ambiguity, invalid authority,
contradictions or any established S2 defect are `FINDINGS / 1`, even alongside cannot-establish.
Neither reaches source, data, reference or binding helpers; model-only authorization cannot bypass P.
**Reacquire current authority and repackage legacy published consumers** rather than guessing or
stamping a LUID. No-P owned workbooks/standalone datasources remain supported. P carriage and internal
S2 readiness alone are not final START_READY and do not upgrade reference grades, BOUND/UNVALIDATED
ceilings, or report-only use of a model-only provider.

Evidence consumers receive only exact walked paths under the evidence role. Datasource N/A roles
require verified absence.
The complete role contract and controls are in [reference readiness](../docs/reference-readiness.md).

The #562 prerequisite handoff is **not the final START_READY consumer**. S1 retains an immutable,
exact-root namespace (digests and walk-produced file identities), plus only the small manifest bytes.
`package_filesystem.read_verified_member(root, integrity, relative_path)` holds one member's bytes
after no-follow namespace/identity checks and a digest recheck against that original S1 result.
Only the requested member and manifest content are rehashed; unrelated content requires a fresh S1
verification, not a small-member read. Copied, replaced or reconstructed result objects have no read
capability. This in-process ownership binding is not cryptographic security or an atomic-filesystem
guarantee. `role_result.data_access_handoff(root)` selects only the declared `data-access.json` and
returns its held bytes together with the exact spec already parsed by S2 and
`credential_gate.package_spec_facts()` (opaque sorted direct-live keys, review/refusal and
direct/published-only applicability). This is the sole package source-facts authority. A nonempty set
of strict scalar `sqlproxy` references with valid published identity can be published-only:
the parser's `powerbi_target=live_source` stays verbatim and is not a second direct database leg.
Legacy references may omit that annotation: their exact `sqlproxy` class, mode and published identity
still identify the reference. An explicit unknown/invalid annotation does not qualify.
Direct sources retain canonical keys, including a datasource's own connection alongside
self-publication metadata. Mixed, nested, aggregate, malformed, unknown/review or additional direct
legs never become published-only. No provider permission is inferred. S2 binds its exact issued
result, role/blocker state and facts, validates the held spec
digest, and freshly reads only spec/projection metadata through S1 without reparsing/reclassifying.
These capabilities and fields are nonserialized. The final consumer uses
`parse_data_access()` on those held bytes and the canonical conjunction below; handoff consumption
does not reparse the spec, read an audit or reprobe.

### Current packaged numeric-scope authority (#363)

The existing `package_role_identity.parse_brief_policy` parser accepts a versioned v2 brief with
**exactly five string keys**:

```toml
+++
schema = "phase1-start-ready/v2"
unit = "Exact_Unit"
scope = "model_and_report"
fallback_authorization = "stop"
numeric_obligation = "none"
+++
```

`numeric_obligation` accepts only `none` or `required`; these describe commissioned scope, not a
numeric result. Unit and topology scope must match exactly. Fallback remains `stop` or
`model_only_unvalidated`. Valid `phase1-start-ready/v1` retains its four-key Phase-1 behavior but has
**UNKNOWN numeric authority for Phase 2**, as do absent, identity-only, plain and legacy briefs.
Missing numeric authority never becomes `none`. Duplicate keys/boundaries, malformed TOML, unknown
keys/schema/values, wrong types and identity/scope mismatches refuse without falling back to prose.
No package is automatically upgraded or given a numeric default.

The frozen `BriefPolicy(requested_scope, fallback_authorization, numeric_obligation=None)` preserves
two-argument construction. Its numeric member is included in the existing S2 issued-state comparison;
reconstruction, mutation or fresh-S1 grafting cannot change an issued handoff's numeric scope.

`package_role_identity.read_current_brief_policy(root)` is the narrow **package-bound current read**:
the existing no-follow working-tree utilities establish the boundary before package bytes open;
strict current manifest/declaration parsing requires `artifacts.migration_brief` to name exactly the
walked `migration-brief.md`, with a valid existing `contents.files` digest. The held brief bytes must
match that digest and those same UTF-8 bytes pass through the shared parser, checked against the
manifest's unit/kind and current declared spec topology using S2's existing rule. It accepts no
caller policy document, scope override, receipt label or environment switch.

The reader returns `(None, policy)` only for current valid v2 `none` or `required`; every unknown or
refused case returns a fixed code and no policy. Valid v1 returns `brief_numeric_obligation_unknown`
here without losing its Phase-1 fallback behavior. This is **not fresh whole-package S1 readiness**:
legitimate model/report/spec working edits need not match their packaging baseline, while a changed
brief alone fails its independent digest check. A Phase-2 consumer must separately bind this read
to its checked current snapshot; a new receipt token cannot repair a stale brief digest.

❌ **No COMPLETE claim or consumer is introduced.** R1 receipt-v3 remains neutral and does not copy
or establish numeric scope; no producer/reviewer override, numeric result or numeric-coverage relaxation
is added. Recommissioning scope returns to Phase-1 packaging, not a Phase-2 rewrite. The unsigned
brief/manifest do not authenticate customer agreement, producer identity or latest-ever history.
Detailed contract and limits: [numeric-obligation authority](../docs/reference-readiness.md#numeric-obligation-authority).

### Package data-access producer and binder (#562, #622)

Each new package declares and S1-hashes a strict `data-access.json`, including blocked/cannot-establish
states. The single brief parser above accepts v1's four string policy fields or v2's five, retaining
the same exact unit, topology scope and fallback rules. Plain/legacy/missing policy remains
`brief_policy_not_parsed`, never inferred approval or numeric `none`. The existing packager copies
the validated brief bytes unchanged and declares their canonical role and digest.
Only exact opening/closing `+++` lines are policy boundaries (LF and CRLF supported); malformed or
extra boundaries return `brief_frontmatter_unparseable`.

`package_unit.py --gate-root <exact-original-root>` selects the gate's bundle or parser-spec
directory/file; the default is `load_bundle(bundle).migration_dir`. Repeatable
`--provider-package <exact-package-root>` supplies separate-command providers. Paths stay in memory.
Datasource candidates publish first; only successful publications join explicit providers, never stale
failed outputs or discovered siblings. The exact S2 input ordinal selects the provider, not its name.
Assembly snapshots the provisional S1 namespace/digests and spec/policy/localization facts, then
runs S2 and assesses. Shipped-data roles must remain present. Only exact generated projection,
handover/README and manifest bytes may change. After budgeting and prior-package retirement, the
guarded swap checks the held candidate and final S1/S2 cohort immediately before atomic publication;
failure restores the prior directory without exposing the candidate. Provider manifest/projection
bytes are held through their bound S1 walk and digest, parsed once
before S2 and selected by the same ordinal. Reseals, root swaps or changed candidate/provider facts
cannot create an accepted projection. Every published-only row needs a complete scalar `sqlproxy`
connection and valid dependency; malformed/additional direct legs never disappear from the denominator.
No probe/audit/gate writes or scope downgrade.

Both the producer and binder call
`credential_gate.reconcile_package_data_access(assessment, facts, *, requested_scope, fallback_authorization, provider=None)`.
This pure, read-only conjunction accepts only an already-issued/parsed assessment, canonical
`PackageSpecFacts`, strict `BriefPolicy` scope/fallback values (or truthful absence), and at most one
exact S2-selected provider reference with its own canonically checked assessment. It has no path,
raw-spec/brief, audit, filesystem or provider-search input. The producer still earns the initial
assessment through `assess_data_access`; the binder checks its held projection without earning proof.
Neither adapter maintains another source/policy/provider matrix.

Accepted assessments retain the same fields and ceiling; existing `blocked`/`cannot_establish`
states and codes remain refusals. Inheritance requires the selected direct local/live provider's
exact token, state, source keys, validation and ceiling. Missing policy, contradictory source facts,
recursive inheritance and model-only providers serving reports cannot be repaired or authorized by
the conjunction. The binder retains `binding_data_access_refused` for stored refusals and translates
new canonical contradictions to the existing source-facts, authorization or provider diagnostics.
For example, a changed direct key reports `binding_source_facts_mismatch`, not a generic data refusal.
Blocked remains exit 1 and cannot-establish exit 3; neither rewrites the stored projection.

✅ **Final consumer:** `check_reference_readiness.py <package> [...]` now conjoins current
boundary/S1/S2, the issued v2 brief, exact source, canonical data assessment, reference policy and
fresh read-only binding inspection. Only the complete package-only cohort returns `START_READY / 0`.
Bind separately first with `set_data_folder.py --package <absolute-package>`; for shared models,
bind the provider, bind the consumer with explicit `--provider-package`, then check **both roots in
one invocation**. No provider discovery, spec reparse, gate write or package mutation occurs.

Package invocations emit one privacy-safe JSON verdict with ordinal-addressed stage/data/binding
results; ordinary targets retain reference-only output. Malformed/unknown inputs are nonzero.
`--json` accepts **only `-`**, including under `--quiet`; file-valued JSON is refused before target reads.
`BOUND` remains `UNVALIDATED`; model-only
authorization remains unvalidated and cannot be inherited by a report. Construction
`ASSEMBLED`/`NOT_EVALUATED`, reference `READY` and successful binding are not final dispatch.
The v2 numeric obligation is metadata, not a Phase-2 decision. Full workflow, codes and unchanged
reference ceilings: [final START_READY](../docs/reference-readiness.md#final-package-start_ready-562-622).

## Migration feedback (Phase 1)

Entry point: [repo-local migration-feedback skill](../.github/skills/migration-feedback/SKILL.md).
The session performs collection and controlled reproduction; this helper validates **recorded**
evidence offline. It does not invoke the engine, a shell, a probe, a live service or publication.
Its one existing subprocess dependency is the shared private-output guard's **local Git reads**.
It reuses `object_identity.revision_key`, `engine_source`, `work_dirs.check_run_location`, the
engine receipt format and `harvest_estate_assets.unignored_output_paths`, not new authorities.

```powershell
python -B scripts\build_migration_feedback.py --input <private-request.json> --run <absolute-run>
python -B scripts\build_migration_feedback.py --input <private-request.json> --out <absolute-private-new-directory>
```

Default output is `<run>\deliverables\migration-feedback\feedback-<UTC>\`. Without a run, `--out`
must be supplied by the skill/session. Existing output, network/device/reparse paths and any
destination Git would offer to commit are refused; there is no unsafe-output override.

### Closed request

The session writes JSON with exactly these keys (no arbitrary public title/body/extensions):

| Key | Values / purpose |
|---|---|
| `schema_version` | Integer `1`. |
| `flow` | `workbook`, `datasource`, `script`. |
| `source_mode` | `local_download`, `remote_capture`, `not_applicable` (script only). |
| `claim_scope` | `local_artifact` or `remote_state`. |
| `owner` | `engine`, `repository`, `external`, `unknown` — a hypothesis checked against evidence, not a route override. |
| `engine_involved` | Boolean; engine route requires true, repository route with true requires a passing baseline. |
| `contrast` | `feature_removed`, `corrected_input`, `known_good_case`, `configuration_changed`. |
| `evidence` | Role → `{path, size_bytes, sha256}`. Paths are explicit absolute paths or relative to this request; lowercase 64-hex hashes pin the bytes. |
| `reproducer` | Optional. `{authorship: "fictitious_from_scratch", redistributable: true, reviewed_sha256: {candidate_input: H, candidate_negative_input: H}}`. No publication approval is represented. |
| `private_notes` | Optional private text. Never part of the public projection. |

Common evidence roles: `predicate`, `owner` (the actual Python entrypoint), `runtime` (the exact
invoked executable), `oracle` (independent executable expectation code), and `positive_input`, `positive_output`, `positive_record`,
`negative_input`, `negative_output`, `negative_record`.
Candidate roles: `candidate_input/output/record` and `candidate_negative_input/output/record`.
Each of those four prefixes additionally requires `_witness`, `_oracle_record`, and `_oracle_result`.
The private and candidate pairs must have distinct input bytes, matching code/oracle/predicate
hashes and the same invocation; positive fails the predicate, negative passes.
All declarations are identity-bound, regular single-link files. Reusing a physical file across
roles (including alternate spellings), or a hardlink outside the declared set, is refused.

Engine roles: `engine_receipt`, `input_manifest`, `engine_report`, `fresh_output`;
`baseline_output` additionally for a repository regression over an engine baseline.
Workbook/datasource requires `migration_spec`; optional `source_provenance` is the existing
`tableau-source-provenance/1` document, joined by exact input hash/size and revision key, never
caption/LUID guesses. `local_download` records raw identity and the existing normalized key
even with `origin.status: not_provided`; attempted unavailable provenance is `origin_unavailable`.
Producer-shaped failed provenance with `inputs: []`, integer `input_count: 0`, and
`phase.status: failed` retains exact local attribution with `origin_unavailable`.
Only **remote-state** claims require successful provenance with confirmed comparable revision,
server/site/LUID, **Tableau product version and REST API version**. Missing values are never inferred. Updated time
and differing archive hashes never substitute for normalized revision agreement.

Context-only roles: `run_status`, `package_manifest`, `gate_results`, `parse_sweep`,
`engine_gap_report`. These are privately indexed, **not recertified**; a captured status must
name the selected run when `--run` is supplied. Existing exported diagnostics may supply these
same files. There is no new diagnostics inventory or readiness authority.

The `predicate` document has `schema_version: 1`, `defined_at` (timezone-aware ISO timestamp),
`kind`, `expected`, `failure_class` and, for JSON, `pointer` (JSON Pointer).
Kinds: `json_equals` fails on unequal values; a missing assertion path is unestablished.
`json_missing` with `expected: true` fails on an absent property.
`text_contains` fails on its exact nonempty `expected` signature, never an evaluated expression.
Failure classes: `incorrect_output`, `missing_output`, `unexpected_refusal`, `runtime_failure`,
`external_block`. Predicate and oracle expectations use the same type-sensitive canonical JSON:
object keys are sorted recursively, list order is preserved, booleans differ from numeric 0/1,
and integers differ from floats (`1` differs from `1.0`). Equivalent decoded float spellings
(`1.00`, `1e0`) agree; signed float zero remains distinct. Encoding uses compact separators,
escaped Unicode and UTF-8, with non-finite values refused. This comparison never canonicalizes
raw evidence bytes or changes their size/SHA-256 identity.

Every **subject observation record is now version 2**, with exactly:
`schema_version: 2`, `input_sha256`, `output_sha256`, `owner_sha256`, `oracle_sha256`,
`predicate_sha256`, `runtime_sha256`, `witness_sha256`, `oracle_record_sha256`,
`command`, `cwd` (absolute), `started_at`, `finished_at`, `exit_code` (integer),
`setup: "ready"`, and `input_binding`.
The request and payload remain version 1; old observations cannot establish participation.

`input_binding` is exactly `{role, kind, argument_index}`. The role is `<prefix>_input`;
`kind` is `file` (integer index `3`) or `directory` (integer index `4`).
Accepted subject command arrays are closed, not interpreted as arbitrary CLI syntax:

- `[<pinned-runtime>, "-B", <pinned-owner.py>, <exact-input-file>]`
- `[<pinned-runtime>, "-B", <pinned-owner.py>, "--input", <input-file-parent>, "--output", <output-directory>]`

The output artifact must be inside that directory. Duplicate/extra options or positional arguments,
`--option=value`, shell strings, `-c`, `-m`, arbitrary interpreter flags and general wrapper chains
are **unestablished**, not guessed. Relative paths resolve only against the recorded `cwd`.
An owner appearing later as inert argv does not establish invocation.
The predicate must predate the original observations; candidate runs follow original controls.
An import/setup error with nonzero exit cannot earn a reproduction.

The `_witness` is a **producer-emitted or independently observed participation result**, not a
session-authored restatement of the request. Its exact fields are `schema_version: 1`,
`command`, `cwd`, `owner_sha256`, `input_sha256`, `output_sha256`. It must identify the code
actually run and the bytes actually read/produced. The fixtures capture it from the child process's
stderr. If a real tool exposes no such evidence, report incomplete; there is no new collector,
instrumentation hook or permission to fabricate a witness.

The independent `_oracle_record` has exactly `schema_version: 1`, `command`, `cwd`,
`started_at`, `finished_at`, `exit_code: 0`, `setup: "ready"`, `runtime_sha256`,
`input_sha256`, `output_sha256`, `oracle_sha256`, `predicate_sha256`, `input_binding`.
Its command is exactly `[<runtime>, "-B", <oracle.py>, <exact-input-file>, <predicate.json>]`
and its binding is `file` at index 3. Its separately pinned `_oracle_result` is the actual
oracle stdout: `schema_version: 1`, `command`, `cwd`, `oracle_sha256`, `input_sha256`,
`predicate_sha256`, `expected`. The result, invocation, consumed input, executed code and
predicate expectation must all agree. Nonempty prose or code byte inequality earns nothing.
The oracle computes the expectation independently, not by echoing `predicate.expected`.

All version/index/exit/size/count boundaries use strict integers: JSON `true` is never `1`.
JSON must be UTF-8/UTF-8-sig, without duplicate keys or non-finite numbers.

`fresh_output` is a **before/after observation**, not a retroactively guessed directory state:
`output_dir`, `observed_absent_at`, `started_at`, `finished_at`, `before_state: "absent"`,
`scope: "full"`, `receipt_sha256`, `input_sha256`, `engine_root`, `engine_version`,
`command` (the exact validated invocation, not a separate path-mention claim), `exit_code`.
Optional `cwd` supplies the absolute base for a recorded relative invocation.
The installed root/version and receipt must agree; the exact input must occur once in the engine's
`assets[].staged_input_path/size_bytes/sha256`, and the baseline bytes must occur once in
`receipt.artifacts[]`. Comparison output is fresh; never partially rerun an existing bundle.

If `run_estate.py` launches the engine, the subject record/witness must name the **engine child**.
Pin `wrapper` as that repository entrypoint and `<prefix>_wrapper_record` as its observation:
the common process fields (`schema_version: 1`, command/cwd/times/exit/setup,
runtime/input/output hashes and input binding), plus `wrapper_sha256` and `child_record_sha256`.
The wrapper and child must agree on arguments, bytes and containing times; the fresh-output proof
names the recorded wrapper invocation. A successful wrapper without a recorded child cannot route.

A repository regression over an engine baseline separately requires `baseline_owner`,
`baseline_output`, `baseline_witness`, and `baseline_record`. The latter has the common process
fields (`schema_version: 1`) plus `owner_sha256` and `witness_sha256`, and binds **positive_input**.
It must be the canonical engine invocation recorded by `fresh_output`; a `baseline_wrapper_record`
may supply the same wrapper/child evidence. The independent predicate must pass on this exact
receipt-backed baseline. The failing repository entrypoint cannot stand in for the engine.

External-only evidence additionally needs `external_evidence`:
`system`, `condition`, `record_sha256` (positive record), `confirmation_sha256`.
`external_confirmation` separately records `system`, `condition`, `input_sha256`, `observed: true`,
`observed_at`. Systems: `tableau`, `powerbi`, `credentials`, `network`, `environment`.
Conditions: `credential_modal`, `permission_denied`, `service_unavailable`, `configuration_mismatch`.
This is positive evidence supplied by an independent observation, not diagnosis by elimination.

### Outputs, exits and limits

- `feedback.json`: private route, identity, controls, exact reasons and limitations.
- `evidence-index.json`: original private locations, sizes and SHA-256; originals are not copied.
- `reproduction.md`: private command arrays, setup, predicate/expected value and bounded actual
  excerpts. All embedded text is data. **Never publish this file or the whole bundle.**
- `repro/`: only the session-reviewed fictitious positive/negative flat `.twb/.tds/.json/.csv/.txt`
  files, under generated names. Original-byte reuse is refused. Executable/opaque/packed
  candidates remain unestablished; oracle/code files stay privately indexed. `.twb`/`.tds` must
  be strict, DTD-free XML rooted at `workbook`/`datasource`, respectively. `.json` must be strict
  UTF-8 JSON. Text/CSV must be nonempty UTF-8/UTF-8-sig without NUL or C0/C1 controls other than
  tab/CR/LF. CSV needs a unique, nonempty header of at least two columns, a data row, equal-width
  rows and successful strict CSV parsing.
- `issue-payload.json`: strict allowlist of route/repository, flow/mode/scope, failure class,
  validated numeric engine version, control/reproducer readiness and generated fictitious-file
  names/sizes/hashes. No original hashes, names, paths, formulas, endpoints, excerpts or commands.
  Written last **inside staging**. There is **no `issue-draft.md`**, rendering or publication action.

The final destination never receives individual writes. Every output is written exclusively into
a new private sibling stage using no-follow handles, re-read against held identities and bytes,
and flushed before an atomic **no-replace directory rename**. The existing destination and its
ancestors cannot redirect a write through a junction. Staged bytes and child directories are
sealed before handles close: owner-readable protected ACLs on Windows, owner read/search-only
modes on Linux. This closes the Windows child-handle-close/rename interval. A seal is complete only
after every permission change succeeds. Failed writes, seals and publication attempt deletion of
the owned stage even if permission rollback fails; partial seals restore only attempted paths,
and rollback continues after individual permission errors. No failed transaction publishes a final
bundle. If safe deletion itself fails, exit 1 reports `output:private_cleanup_failed` and the
retained private stage path **only to the local operator**, never in the public payload or a success
summary. An unassessable/swapped namespace is refused, not traversed for cleanup. The parent must
accommodate a private sibling stage, not merely an ignored final leaf. Local Windows and Linux
`renameat2` are supported; unsupported atomic
publication/filesystem capabilities refuse rather than downgrade.

The completed bundle remains read-only. Regenerate it rather than editing behind its hashes;
its owner can explicitly change permissions for disposal. No protection against an administrator
or owner deliberately changing permissions is claimed, nor power-loss durability or protection
after someone explicitly unseals the result.

Exits: **0** established (including non-fileable external/configuration results);
**1** privacy/integrity/filesystem refusal; **2** usage; **3** incomplete.
Missing route evidence yields `CANNOT_ESTABLISH`. A changed or unproven candidate yields
`reproducer_not_established` and exit 3 while valid private attribution may remain.
An **omitted** optional reproducer has that same exit-3 private result; a **present malformed**
or unsafe declaration is an exit-1 refusal. Neither copies the original input.
`public_filing_ready` is false for either, and is never publication consent.
Input/destination refusals can happen before a bundle exists; the CLI prints the exact reason.

This validates recorded producer witnesses and invocation/result consistency, not signed execution history, live connectivity, complete
migration fidelity or the honesty of the evidence producer. Fictitious authorship/redistribution
is an explicit **session review of pinned bytes**, not automatic classification. Never derive it
from customer text. A future publication gate must consume the payload after explicit user
approval and separately review any proposed attachments. Phase 1 ends here.

The fictitious controls in `tests/test_migration_feedback.py` run a deliberately mutated synthetic
engine and local CLI against independent expectation code. They prove this feedback contract,
not a defect in the installed production engine or any customer's system.

## Read-only run status

```powershell
python -B scripts\run_status.py --run C:\short\_runs\001-estate
python -B scripts\run_status.py --run C:\short\_runs\001-estate --json
```

Select the actual existing local run; there is no latest-run search or allocation. **`-B` is part
of the read-only entrypoint**, not an optional optimization: without Python's startup flag, imports
may create `.pyc` files outside the selected run before script-level controls can take effect.

The reader rejects UNC/device/network spellings lexically before filesystem access, checks local
ancestors and entries without following reparse points (including Windows junctions), and never
reads below a rejected boundary. It reuses `work_dirs.check_run_location` and
`package_filesystem.verify_package`; it does not run readiness gates. Both engine-report collections
(`workbooks` and `datasources`) must exist and be lists. Explicit empty lists mean an empty
diagnostic inventory, **not** readiness. Unestablished report scope does not discard retained
working copies or package-only observations.

Both outputs begin with the same `LOCATIONS` projection: absolute paths for the toolkit checkout,
the selected run, its standard `bundle/`, `oracle/` and `packages/` roots, and each already-discovered
package plus that package's fixed `fabric/` working copy. Human output prints each path once on its
own `path:` line with native separators, so it can be copied without JSON's doubled backslashes;
JSON carries the identical records. `expected` marks the standard documented location (or a
`discovered` package directory) and never asserts existence; `observed` is the only existence claim
and is closed to `present`, `missing` and `cannot_establish`, with the detailed unassessable state
and its finding retained in `canonical_subdirs`/`findings`. Nothing is created.
The toolkit root comes from this script's own checkout location, never the caller's working
directory or the run's recorded metadata, and the derived roots always follow the accepted selected
run. `relationship` uses whole-component containment, so a lookalike name prefix remains
`outside_toolkit`; it explains spelling only and is not an identity, safety or readiness authority.
An unestablished or unsafe selection yields only the toolkit location plus a `cannot_establish`
selected run with no path; a path that could carry control characters is withheld. Location safety
is independent of package seal integrity in both directions. See the
[layout explanation](../_runs/README.md#where-the-toolkit-and-the-selected-run-actually-are) for the
optional VS Code **File > Add Folder to Workspace…** display of the active run; this command never
launches an editor, writes a workspace file or relocates a run.

Package discovery stops at each marker, including malformed markers, and ignores ordinary files in
grouping directories. It supports flat packages and retained grouping layouts up to three directory
levels below `packages`. Unmarked directories are not inferred to be completed packages. Handover
JSON is matched by **unique exact embedded workbook identity**, never by guessing a sanitized
filename. Duplicate identities are reported; when a producer overwrote a colliding filename, only
the retained identity is observable and the other unit remains missing.

All statuses are closed projections. Arbitrary manifest prose, commands, exception text and nested
readiness objects are not echoed. Stored readiness is a list of **last-observed** records with
validated timestamps; every current certification remains `NOT_CHECKED`. The actual packager's
`oracle.objects`/`omissions` record presence, missing/omitted evidence or an unassessable shape;
object counts do not prove coverage or fidelity. A recorded report-free datasource can be reference
`not_applicable`. Human output uses escaped, record-per-line JSON values under readable headings,
so it carries exactly the JSON output's observations without control-character line spoofing.

Exit **0** means the consumed diagnostic inputs were assessable, including known pending binding,
missing references and recorded package edits. Exit **1** means some requested evidence could not
be assessed; other retained observations are still shown. Exit **2** rejects an unsafe/nonlocal or
relative input spelling (and is argparse's usage exit). Missing optional evidence is distinguished
from malformed, unreadable and non-directory evidence.

Limits: this is a read-time diagnostic, not an atomic filesystem snapshot. It inherits the package
reader's documented `lstat`/read replacement-race limit, does not identify overwritten history,
inspect unmarked package trees or prove that a local drive/mount is physically local, and makes no
claim about interpreter activity before `-B` takes effect. It never changes run files, invokes
processes/network clients, opens Desktop, refreshes, promotes, deploys or cleans up.

## Phase-2 COMPLETE — one caller-pinned current-snapshot check

After binding and public START_READY, use tokenless/layer checks for diagnostic work. The only
COMPLETE authority is:

```powershell
python scripts\check_unit.py <package> --scope all --receipt-sha256 <caller-held-final-sha256>
python scripts\promote_unit.py --package <package> --slug <slug> --receipt-sha256 <caller-held-final-sha256>
```

The caller supplies the **identical exact lowercase ASCII 64-hex H**, without `sha256:`, trimming
or case normalization. Never discover it from files, history, environment, newest mtime or a prior
promotion. Retain the final producer's returned H outside the mutable package. An all-scope call
without H remains useful diagnostics but ends `CANNOT_ESTABLISH`/nonzero; layer scopes can return
`AUTOMATED_CHECKS_PASS`/0, **never COMPLETE**. `--json <file>` output must be outside the pinned
package. Explicit external reference/oracle overrides are diagnostic-only, not COMPLETE inputs.

The checker requires the current final **literal v3**, `sign_off`, `all_pages` chain under H;
the **original issued W** current source/data handoff at that exact R/target; an independently read
current strict `phase1-start-ready/v2` brief; and canonical direct data reconciliation. Only
`local_import_ready`/`live_data_ok`, validated at `model_and_report` with `data_validated` ceiling,
can satisfy that leg. It does not renew packaging-time S1/S2 or reconstruct W.

The initial class is **one owned workbook/report/PBIP/package-local model**, with nonempty,
fully accounted canonical `definition/tables/*.tmdl` tables and explicit partition-level M/import
throughout. DirectQuery/live, dual, Direct Lake, mixed storage, calculated/implicit, unknown,
ambiguous, omitted or unreadable partitions and zero partitions cannot COMPLETE. Existing source
fidelity gates remain mandatory: an allowed partition category alone is not source proof, and an
existing `SKIPPED/nothing_to_check` gate is still blocking.

Every existing gate remains in the fold: page/source identity and exact denominators, bindings,
AI descriptions/domains/CustomInstructions/Q&A, source fidelity, paths, orphans and native gates.
Current names must equal the current declared files plus the manifest, exact files of **every**
validated iteration and the selected model's exact receipt-admitted cache — never a whole `.pbi`
or iteration wildcard. Existing working TMDL/PBIR edits need not match their packaging-time hashes.

Required A1 observations are a requested full database refresh, explicit named canaries with
positive returned rows, one binding/catalogue, and ImageSave/readback matching current cache bytes.
Zero rows is a finding; missing/refused/unestablished observations are non-success. Every whole
page and visual must have a `pass` judgement over stable valid PNGs and source-digest-bound
**validation-grade** references. Oracle/layout-text, `layout_match`, subsets and unverified
judgements are insufficient. Findings and current limitation references remain adjudicated and
history-bound. H, R/target, original W, current brief, namespace and model class are rechecked before
the terminal `finalized` fold.

Only current v2 **`numeric_obligation = "none"`** waives numeric comparison. Raw numeric counts
and missing lists remain visible, with this exact disclosure:

> Exact Tableau-versus-Power-BI numeric comparison was not performed because the commissioned brief explicitly waived it.

Required, missing, legacy/v1, malformed, mismatched or stale numeric authority remains
**`CANNOT_ESTABLISH(NUMERIC)`**, saying **Phase-2 COMPLETE was not established.** CSV presence or
reviewer labels cannot override the brief. Precedence stays **4 precondition → 1 findings → 2
not checked → 0 COMPLETE**; numeric refusal remains visible when a higher-priority safe finding
wins. Argparse syntax errors use 2; a nonexistent ordinary directory uses 64.

Successful checks preserve exactly:

> Phase-2 COMPLETE at this check for the package snapshot pinned by the supplied final-receipt SHA-256, under the documented Phase-2 evidence contract. This checker does not authenticate the token's producer or establish that this is the latest snapshot ever produced.

This is **current-snapshot**, not original commissioning: an old package with matching old H may
remain valid; a coherent rewrite with a newly caller-accepted H is assessed as that snapshot.
There is no second token, authenticated producer, latest-ever history, hostile-writer isolation,
required-numeric success, Desktop/native qualification, deployment or Phase-3 sign-off claim.
Persistence commitment remains **UNESTABLISHED**: no cold-reopen, durability or receiving-machine
proof. Promotion omits Desktop-local cache/state and does not extend the package's evidence claim.

The promoter invokes the public checker with identical H and records its exit/result, disclosures
and warnings (host paths remain redacted). The stored H is observed linkage, **never future
authority**. `--force` and H are mutually exclusive at parse time, before resolution or effects.
Force alone can run tokenless diagnostics and ship only under the existing non-forceable
content/containment/external-data/host-path/model-reference/transaction guards. It prints and records:

> PROMOTED unchecked; Phase-2 COMPLETE was not established.

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

For bridge/Desktop smoke tests, use a PBIP whose model has a persisted `.pbi/cache.abf`. A reference
bundle that points at a live SQL Server unavailable in this tenant will always stop at the credential
dialog and can make a healthy bridge look broken.

## Gates (CI and agent Definition of Done)

| Script | What it enforces |
|---|---|
| `validate_spec.py` | Re-validates `migration-spec.json` after agents append `limitations_encountered`; error paths name the offending entry/field so the append can be fixed in place. Read-only unless you pass `--repair`, which rewrites the spec to drop exact duplicates. |
| `check_unit.py` | Per-unit façade (#291/#247): runs automated gates by `--scope model`, `report`, `integration`, or `all` (default) without merging native implementations. **Only caller-pinned all-scope can COMPLETE/0** under the [Phase-2 evidence contract](#phase-2-complete--one-caller-pinned-current-snapshot-check); tokenless all-scope is non-success and layer-only 0 remains `AUTOMATED_CHECKS_PASS`. Findings include owner hints, evidence snippets, omitted checks for scoped runs, visible compromise counts (signed exemptions and declared downgrades), scaffold-partition blockers from handover, and native rerun commands. ⚠️ **The expected page set is dashboards PLUS orphan worksheets, never dashboards alone** — `twb_to_pbir.py` emits a page per dashboard and a page per sheet no dashboard placed, so a dashboard-less workbook (19 of 43 in a real 2.339.0 estate run) used to expect *nothing*. The spec is validated **whole** before any page is derived: both schema-required arrays present and well shaped, every entry identifiable, every zone tree walkable — a partially malformed spec must never become a smaller *trusted* denominator. ⚠️ **Evidence of an absence is not acceptance of an absence.** `viz_fidelity[].tier == "empty"` proves the *engine* emitted no faithful visual; it never approves shipping without that page. Every expected page must be rebuilt or carry a **signed** `page-parity` exemption, and only an *applied* signature counts as a compromise. The one exception is derived from the SOURCE, not the engine: a worksheet with every encoding shelf empty and no filters renders blank in Tableau too, so it owes no page (`no-source-content`; 2 exist in the measured estate). ⚠️ **Every join runs through [`object_identity.py`](object_identity.py)** — the same `(kind, exact_name)` type PR #428 uses, carried here byte-identical and **gated** by `tests/test_shared_identity_pin.py` (offline SHA-256 pin, runs under CI's shallow checkout) plus `tests/verify_shared_identity_pin.py` (compares against the sibling ref; exits 2 when it *cannot* compare). Drop evidence, page pairing, exemption resolution and oracle coverage all resolve through one `EngineIndex`: a worksheet row never settles a same-named dashboard, a kind-less name claimed by more than one expected page attributes to **neither**, and one exemption naming `Sales` cannot sign both a dashboard and a worksheet — sign the page **id** for that. Proof of non-emission requires **both** `tier == "empty"` and `visual_type == "unsupported"` (measured: all 46 empty-tier rows in one estate run carry `unsupported`, and no dashboard/filter/workbook-scope row ever does). Never `pbip_warnings[]`, never `status: "warned"`, never `evidence: "emitted+linted"`, never reason text. ⚠️ **A page with zero visuals renders nothing and never certifies a candidate as rebuilt**; the engine's crash-guard page (`page-empty*` + `No visuals rebuilt`) is the one expected blank page and any other is a finding. ⚠️ **Renaming makes attribution ambiguous, and ambiguity suspends every name-only signature** — while a rendered page matches no expected page it might BE a renamed candidate, so declare it with an `extra:<page>` exemption first. ⚠️ **`oracle-coverage` never falls back to the artifact's own pages** — an expected set that cannot be established is a blocking `NOT_CHECKED`, not a pass. The oracle capture is auto-discovered under **both** documented names, `_oracle/` (the `capture_tableau_oracle.py --out _oracle` convention) and `oracle/` (the canonical `_runs/<NNN>-<slug>/oracle/` layout in `work_dirs.CANONICAL_SUBDIRS`), beside the unit, the target, or the target's parent; `--oracle-dir` overrides both for diagnostics only. Handover roots are **resolved before de-duplication**, because `_unit_dir` resolves while the CLI target keeps the caller's spelling — with a relative invocation the same directory was scanned twice, every evidence row was indexed twice, and each became an ambiguous resolution whose declared reason vanished. The same ordering now governs **reference and oracle discovery**, where it was missing and invisible: collapsing producer records into a `{slug: bool}` map hid the double-read, and preserving multiplicity surfaced one manifest producing two records that then refused each other. ⚠️ **Oracle and reference records keep their identity too** — name, multiplicity, declared KIND and *producing workbook*. A record may only satisfy a page of the **same kind**, and a record whose kind cannot be established satisfies **nothing**: `capture_tableau_oracle.py` writes `view_type` from the Metadata API (#402) and uses the literal `unknown` when it could not tell, which is a refusal rather than a third kind. Measured before this, a record explicitly typed `worksheet` gave a full oracle PASS to the *dashboard* of the same name — and a Tableau dashboard sharing its name with its principal worksheet is the ordinary case, not an edge one. Reference-manifest entries are dashboards **by construction** (`capture_tableau_reference.py` builds them from the spec's `dashboards[]`), so a worksheet page genuinely has no reference oracle instead of being certified by a dashboard picture. Name matching is **exact only** — a view name and a page name are both source-owned, with no filesystem between them, so nothing legitimately re-spells one into the other. The workbook fallback *is* justified (the unit side is a sanitised artifact stem) but must resolve uniquely on **both** sides: measured, records declaring `Bo ok` and `Bo-ok` were both admitted to unit workbook `Book`, because uniqueness was checked among unit workbooks only. Discarded evidence is disclosed rather than silently absent — `admitted_evidence`, `kindless_evidence`, `unattributed_evidence`, `foreign_workbook_evidence` — so a reader can tell "nobody captured that page" from "the capture could not say what it was a picture of". ⚠️ **The lossy key `_slug()` cannot decide anything on its own**: every lossy join goes through `NormalizedIndex`, whose candidates are stored as a list and whose only accessor is `unique()`, returning `None` for **0 and for >1**. That replaced a census of `_slug` *call sites*, which round 7 proved vacuous in all three of its classes — *a census that pins where a function is called cannot prove how its result is used*; an unrelated `== 1` satisfied it, a `set()` one line later was invisible to it, and a reporting-only value could be promoted into a decision without touching a pinned line. `tests/test_slug_call_site_census.py` now proves three narrower things that can each fail: every `_slug` call is inside `NormalizedIndex` or in a documented reporting allowlist; the index refuses both failure directions and exposes no way to read a bucket; and the allowlisted sites are **behaviourally non-interfering** — blanking the reporting map must change no verdict while visibly changing the explanatory text. Signatures for `scaffold-partitions` and `stub-measures` resolve **globally, exactly once**: one entry named `A-B` used to exempt both table `A-B` and table `A B` and report two exemptions from one signature, so an entry matching more than one finding now applies to **none** and is reported as contested. The workbook-binding fallback is guarded on **both** sides — exactly one handover workbook *and* exactly one distinct shipped-artifact stem may carry the key (a report and its model share a stem and are one name, not two owners). The page rules above are mutation-tested by `tests/mutate_check_unit.py` (87 mutations, each run against the single test it must kill, plus a no-op control that must survive; exit 0 = every one caught). The estate-scale result is committed rather than narrated: `tests/estate_page_gate_digest.py --bundle <engine bundle> --specs <parsed specs>` re-runs both halves over every staged unit through the documented **relative** CLI path and compares a SHA-256 of the summary against `tests/estate_page_gate_expected.json` — 0 matches, 1 differs, **3 could not be measured** (no bundle, or nothing staged). It is not a pytest test because CI has no estate bundle; run it beside a real one. `check_path_ceiling.py` runs here too (refs #235) so the Windows-path question is answered by the same command as the rest — **`all` scope only**, because the scan walks the whole target tree and cannot be attributed to a layer (a model-scoped run would be judging report paths, and vice versa); scoped runs therefore name `path-ceiling` in `omitted checks` rather than dropping it silently. It stays **gating**, like the native gate: a bundle Power BI Desktop cannot open is not done, whoever caused it — so the row is routed to the **orchestrator** (install-root length + engine-side name duplication), not to a builder who cannot act on it. Because the verdict is measured against *this* checkout root, the row states `root_budget` (the longest install root the tree tolerates) **on every status, including PASS**, and says which direction it breaks in — `root budget 153 - breaches above a 153-char installation root`. That is deliberately unconditional rather than gated on `root_budget_is_tight`: measured on a byte-identical tree, root length 65 passed with budget **79** (`root_budget_is_tight` **false**, advisory 40) and the same tree breached at root length 94, so a threshold tuned for "alarming" cannot also mean "safe to relocate" — and a number shown only sometimes cannot be told from a dropped annotation. `TIGHT` is appended when the advisory does fire. Other exits: 1 findings, 2 not fully checked or argparse syntax error, 4 page-parity precondition failed, 64 missing ordinary directory. |
| `check_agent_capabilities.py` | Ensures every shipped capability listed in `docs/agent-capability-wiring.md` has its load-bearing token in an agent-reachable file (`AGENTS.md`, `docs/INDEX.md`, or a persona). It also scans tracked `scripts/*.py` files whose docstring declares `usage:` and requires each to be named in agent-reachable script guidance (`AGENTS.md`, `docs/INDEX.md`, `scripts/README.md`, or a persona) or explicitly marked internal with an `internal-reason:`. The check strips fenced code and HTML comments before matching, so a hidden mention cannot satisfy it; registry wording is guidance, not a verbatim match. |
| `check_datamodel.py` | Semantic-model gate: Power Query M syntax, low-noise TMDL structural checks, and the TMDL oracle. Exits **3 (unassessable)** — never 0 — if the oracle cannot run; `--no-oracle` is the explicit opt-out. |
| `tmdl_checks.py` | Library half of `check_datamodel.py` (no CLI): the text-side TMDL checks — duplicate scalar properties, measure/column name collisions, empty measure expressions, compact CALCULATE filters, BOM/decoding. Whether a document PARSES deliberately lives in `tmdl_oracle.py` instead. Run it via `check_datamodel.py`. |
| `tmdl_oracle.py` | Library half of `check_datamodel.py` (no CLI): the TMDL **oracle** (#254). Hands each model to `TmdlSerializer.DeserializeDatabaseFromFolder` (AMO 19.84.1, via `tools/tmdl_oracle/`) — the parser Power BI Desktop itself uses — so a layout the parser refuses is reported as `TMDL_PARSER_REJECTED` with its own document and line, instead of being guessed at by a re-implemented grammar that three attempts could not make false-positive-free. It checks the helper's reported AMO version against the pin in `tmdl_oracle.csproj`, because a verdict is only as good as the parser behind it. Silent absorption is deliberately out of scope and undecidable from the parse (issue #404). Needs the .NET SDK. Run it via `check_datamodel.py`. |
| `check_desktop_orphans.py` | Run-owned Desktop leak gate: reads a unit's `.desktop-instance-audit.log`, compares recorded PIDs plus process start times to live `PBIDesktop.exe`, and fails only when a process this unit opened is still alive without a `desktop-kept` handoff. |
| `check_empty_model.py` | Offline gate against a model that opens and loads **zero rows**: classifies every emitted TMDL partition and blocks (`run_estate.py` exit 6, `EXIT_EMPTY_MODEL`) on an Import over a flat file that is missing, foreign-path, or empty. Never judges live/DirectQuery, remote imports, calculated tables, or needs-review stubs. |
| `check_connection_fidelity.py` | **Cross-artifact** gate (#328) against a **silent live-to-flat-file downgrade**: a Tableau source that must connect to a live upstream system whose shipped model instead reads a CSV/Excel/inline file, so it can never refresh and freezes the customer's data at export time. **A pass requires PROVEN provenance, not a connector name.** `partition_provenance` requires the **complete** source expression to be one top-level `let … in <identifier>` (no nested `let`, no leading branch), then walks backwards through `name = expr` bindings demanding the engine's canonical shape: one connector-call root, pure navigation steps, and **at most one** `Value.NativeQuery` whose first argument is a **usable** handle — a collection-returning root such as `Snowflake.Databases` must be drilled first, which `storage_mode.py` documents. **Every step must consume its whole binding** (balanced-delimiter matching): a native query concatenated with inline rows, or `Sql.Database(…) = null`, is a prefix match and is refused. This is reachability, not an M interpreter — no operator precedence, no types, no evaluation. It exists because *attribution is not provenance*: knowing which table belongs to a source never established which expression supplies its rows, and a connector token in a string literal, an unused lazy binding, an unreachable `else let …` branch, a native query on a root collection, and a zero-row query concatenated with `#table` all certified rows that came from somewhere else. Measured over both real bundles (280 partitions, 8 shapes, 49 live): every generated live partition matches, so the shape gate **costs zero passes**. Anything hand-edited is `NOT_CHECKED`. Reads **both migration tiers** (#366): a parser `migration-spec.json` unit and an **engine datasource unit** (`report.json` → `datasources[]`, carrying real `tables`) are judged per declared table (`SCOPE_TABLE`); an **engine workbook unit** carries only a `table_count`, so at `SCOPE_MODEL` the gate emits a **finding or a refusal, never a pass** — a capability ceiling, closable only by per-table provenance (upstream `tableau-fabric-skills` #182). A finding still fires there: "no such connector anywhere **and** rows come off disk" needs no attribution. **`report.json` is the coverage census** — validated for structure, not just parseability; slices never replace it; an unreadable/malformed census, an unreadable slice, and two units sharing a name are all `not_evaluated`, never a silent shrink. Bundle dir, `report.json` and a direct slice report **identical coverage**. A `sqlproxy` workbook is a **pointer** naming the datasource unit to check; an unresolvable source gets its own `NOT_CHECKED` verdict and is never dropped. ⚠️ It does **not** flag flat files as such — at model scope a file-backed partition a declared flat-file sibling could own is `NOT_CHECKED`, not a finding. **A `SKIPPED` unit says which kind it is** (`[nothing to check]` vs `[NOT EVALUATED]`) and every verdict prints `COVERAGE: n of m unit(s) examined`. Measured on a real 52-unit estate: 7 datasource units examined and clean (6 Snowflake, 1 Databricks), 30 workbooks `nothing_to_check` (all-flat-file, zero false), 15 `not_evaluated`. Exit 0 pass / 1 findings / 3 skipped. | `pbi-semantic-builder`, and `check_unit --scope integration` |
| `check_sqlproxy_connections.py` | Offline gate (#282) against shipped semantic models that still contain Tableau published-datasource proxy parameters. Reads `definition/expressions.tmdl` only, keys on `Server_sqlproxy*` / `Database_sqlproxy*` names (not `localhost`), and reports each pair because `Database_sqlproxy*` names the published datasource that must be migrated or rebound. Exit 0 clean / 1 sqlproxy connection present / 3 no semantic model measured. Native engine `report.json` workbooks with `binding_signal.secondary_datasources` and `pbip_status: built` are surfaced as non-blocking warnings: at-risk telemetry, not proof that the shipped model is broken. |
| `check_m_syntax.py` | Compatibility shim for the pre-rename model gate; forwards to `check_datamodel.py` until external callers and old migration briefs have moved. |
| `check_pbir_valid.py` | Report-side twin of `check_empty_model.py`: runs the **first-party** `powerbi-report-author validate` over every shipping `pbip/` report and blocks (`run_estate.py` exit 7, `EXIT_INVALID_PBIR`) when one is structurally invalid. Exists because the engine's own definition of done never validates its own output — measured 2026-08-18, a `PBIR_ROLE_REQUIRED_MISSING` report was graded `warn`/`0 error`/`Viz=built` (#220, #221). Delegates rather than reimplementing the role catalog; degrades to `SKIPPED` without the CLI and to a non-blocking `ERROR` when the validator itself cannot form an opinion. |
| `check_field_bindings.py` | **Cross-layer** gate (#236, #258). Two questions in ONE pass: (a) every PBIR field reference must resolve to a real column, measure or hierarchy level in the semantic model it ships with, and (b) every visual's grouping columns must agree on a table set Power BI can actually **join**. (a) catches a model-layer rename (folding Snowflake identifiers with `Text.Upper`) that leaves both layers clean and Desktop throwing "Fields that need to be fixed"; **case-only near-misses are a separate, labelled category printing BOTH spellings**, because the fix is mechanical. (b) catches the field bound to the *wrong* table of the same name — it resolves, so (a) is silent, and it surfaces only as Desktop's `InvalidUnconstrainedJoin`. Both are reported together on purpose: the field report that raised #258 was really about the **ordering** — the table disagreement stayed masked until the genuinely-missing fields were fixed, so "clean bill of health" arrived one round too early. Offline, no CLI, no Desktop; `pbip/` only, model resolved from the report's own `definition.pbir` `byPath`. |
| ↳ what "coherent" means | Measures are **excluded** (a measure aggregates across the model; its home table is an organisational choice, and a `_Measures` table is disconnected on purpose). Tables joined by an **active** relationship path — direct or transitive — are one set, so a fact + dimension visual stays silent. **Field parameters** and **calculation groups** are exempt (substituted at query generation, never joined); a plain disconnected slicer table is **not**. A name that resolves on more than one table is named explicitly, with candidates restricted to tables the rest of the visual can already reach. New top-level status **`INCOHERENT`** (exit 1, like `UNRESOLVED`) — gate on the **exit code**, not the string. |
| `read_handover.py` | Reads the engine's per-workbook handover slice (`handover/<workbook>.json`) and prints the residual work queue an agent actually has to author. Reduces a 347 KB file to ~1.6 KB, mainly by **de-duplicating `category_guidance`**, which the engine emits per *request* rather than per *category* (44,775 bytes / 12.6% in the worked example; verified one distinct string per category across all 38 handovers). Its highest-value output is **`pbip_ref_drops[].emptied`** — visuals whose *every* field binding was dropped, so they render blank on a report that validates clean; `--severity` never hides them. Counts are engine handover requests, not the same denominator as `check_stub_measures.py`'s TMDL placeholder-body census. `requests[]`, not the 5-field `needs_review[]`, is the list carrying `formula`. `--list` triages a whole estate; `--category`/`--viz` scope to one agent's layer; truncation over `--max-bytes` names omitted items while they fit, then explicitly counts any remaining unnamed tail. ⚠️ **Ergonomics and triage, not a correctness fix** — a plain read of the file fails loudly rather than silently, and an agent that parses the JSON itself gets the same answers (measured; see `powerbi-semantic-model-gotchas` §8 for the retracted claim and why it is kept). |
| `check_identity_normalization.py` | Quarantine rule (#421 round 4): fails if anything outside `object_identity.py` calls `object_identity.normalize()`. ⚠️ **This is the difference between a convention and a guarantee.** `IdentityIndex` makes ambiguity unrepresentable *inside itself*, but a plain `dict` keyed on `normalize(name)` is still one line away — and this defect defeated a convention at **five** successive layers, so a sixth author reaching for a raw dict is the single most likely way layer six arrives. Deliberately **narrow**: it bans one call, resolved through per-file import aliases, and does **not** attempt to detect "any lossy join" — that is undecidable, and a rule that cries wolf gets switched off, at which point it protects nothing. So another module's own `normalize()`, `unicodedata.normalize`, and occurrences inside string literals (the mutation harness carries them on purpose) are all silent. The failure message names the **fix** — `IdentityIndex`, or `shares_name()` for report-only comparisons — because a rule that only refuses teaches nothing. An unparseable file is a finding, not a skip. Exit 0 clean / 1 violations. ⚠️ Writing it found **two violations neither the reviewer nor I had enumerated**, including a lossy comparison on *workbook* identity in `Evidence.is_for`; when you want to claim "X is gone from every Y", enumerate the Ys with a machine. |
| `check_reference_readiness.py` | **ENTRY** gate (#421, #562, #622), after deterministic emission and before agentic work. Package-only **`START_READY / 0`** requires current boundary/S1/S2, v2 brief, exact source, canonical data access, reference readiness and fresh binding inspection. It never binds or earns proof. Ordinary targets retain reference-only `READY`/`NOT_APPLICABLE`; page completeness, evidence, grade and provider ceilings are unchanged. Exit 1 findings / 3 `CANNOT_ESTABLISH` are not passes; usage exits 2. [Full contract](../docs/reference-readiness.md#final-package-start_ready-562-622). |
| ↳ why it does not reuse `check_unit.expected_pages()` | That helper is wrong three ways and is not imported (`check_unit.py` is owned elsewhere). Its docstring says "dashboards only, never worksheets" (`:572`) but the engine emits a page per dashboard **and** per orphan worksheet (`twb_to_pbir.py:14040`) — measured, it expects 0 where the engine correctly emitted 3. It reads `migration-spec.json`, which **does not exist in an engine bundle**, so it returns `None`. Its consumer is then circular: `check_oracle_coverage:925` does `expected_pages(target) or actual_pages(target)`, grading the output against itself, so a dropped page cannot be counted as missing evidence. This gate derives its own expectation and **never** falls back to what was built. |
| ↳ candidates are not emitted pages | "dashboards + orphan worksheets" names the *candidates*. The engine deliberately drops a page in three further cases, each with a recorded warning: a dashboard with no supported visuals (`:14529`), an orphan worksheet with `VT_UNSUPPORTED` (`:14558`), one with no usable field bindings (`:14562`). A naive diff would therefore raise a finding on every **correct** bundle — the false-positive direction, and how a gate gets muted. Drops are split into `dropped_explained` (a matching `viz_fidelity[]` row exists) and `dropped_unexplained`; only the second is a finding, and both counts are always printed. `pbip_warnings[]` is deliberately **not** the explanation channel — `_warn("dashboard", name, …)` produces a reason string that does not contain the name, so matching on it would let one dashboard's excuse cover every dropped dashboard. |
| ↳ identity, not name slug | `check_unit.py:265` matches on `_slug(view_name)`, and in Tableau a dashboard routinely shares its name with its principal worksheet — so a worksheet render satisfies a dashboard page, which is the **normal** case. Live today: `capture_tableau_reference.py:199` files `embedded_thumbnail` records (worksheet renders — "dashboards are not thumbnailed per se") under the manifest's `dashboards` key. Two defences: page ids are reproduced from the engine's own `_sanitize` md5 (`Revenue by Region` → `page-ws-Revenuebb7d27f78` as a worksheet, `page-RevenuebyRe2b117987` as a dashboard), and every piece of evidence carries a **scope** that must match the page's kind. Scope that cannot be established is `unknown` and satisfies nothing. PR #422's oracle `view_type` is consumed when present; absent or `unknown` is "cannot establish", never either type. |
| ↳ fail closed | `blind`, `unverifiable` and `insufficient-grade` are distinct from `ready` and **none exits 0** — a readiness gate that green-lights on absent evidence is worse than no gate, because it launches an agent to build confidently against nothing. The mechanism is that **unverified evidence is unrepresentable**: `Evidence` is only reachable through `Evidence.build()`, which returns either a verified record or a `RejectedEvidence` that can never be matched (rejections are counted and printed, so a capture that does not count says why). Preconditions, each replacing a measured fail-open: a render that **parses** (`Path.is_file()` let a **zero-byte** PNG reach READY) and clears a 64 px legibility floor; **non-empty capabilities from a closed allowlist** (`capabilities: []` produced `ready [unknown]`); **workbook identity** (one synthetic record made two *different* units report `2/2 READY`); and **source revision** — a manifest whose `source_workbook_sha256` no longer matches the resolved source is stale, and a stale capture is worse than a missing one because it looks like evidence. ⚠️ **There is deliberately no `--warn-only`**: the flag was measured returning exit 0 on a bundle whose own output said `CANNOT_ESTABLISH is NOT a pass`. Advisory consumers read `--json`. `NOT_APPLICABLE` is **earned** from the engine's `report.json`, never from "I found no pages" and never from "some semantic model exists" — both were measured granting a clean exit to a workbook whose report generation had **failed**, which is now a finding. The 0/1/2/3 scheme is `check_connection_fidelity.py:160-163`'s, adopted rather than invented: its `:165` comment records issue **#366**, where nine unexamined workbooks read as a clean bill of health. |
| ↳ full rationale | [`docs/reference-readiness.md`](../docs/reference-readiness.md) — why `check_unit.expected_pages()` cannot be reused, why candidates are not emitted pages, why a drop explanation must match in **kind** as well as name, the cryptographic page-identity join **and its 8-hex-digit collision limit**, the provider→scope table, the readable-page-mapping requirement, and the grade ceiling. |
| `reference_evidence.py` | The **evidence layer** the gate above is built on, split out because it answers a different question: not "is this bundle ready" but "is this a picture I may believe, and of what". `Evidence` is reachable only through `Evidence.build()`, which returns a verified record or a `RejectedEvidence` that can never be matched — round-2 review found three more fail-open paths one level below the round-1 restructure, all from validity being checked at call sites. Preconditions: a **structurally complete** render (full PNG chunk walk with CRCs, IHDR/IDAT/IEND — a 24-byte blob used to pass while Pillow called it truncated); a match against the **`sha256`/`bytes`/`dimensions` the producers already record** and this gate ignored (zeroed hashes and 1×1 dimensions still returned `READY 3/3`); a grade capped by **`PROVIDER_CEILING`**, so `embedded_thumbnail` cannot claim `validation_grade`; and workbook identity carrying **both** LUID and name, with the LUID trusted only when `source-provenance.json` says `match: "sha256"` (`name_only` means the bytes differ and figures will not reproduce). ⚠️ **Grade never sets kind** — round 3 measured a validation-grade `manual` record being promoted to a kind matching *both* dashboards and worksheets, re-creating the founding defect; a `manual` record must **declare `view_type`** in the manifest to be usable, and the gate says so in its output rather than guessing. |
| `object_identity.py` | The identity abstraction every join runs through (#421 round 3). One defect recurred at **five** layers — routing, matching, normalization, manual-kind, unit-join — always as "one object's evidence or excuse covering another"; each round closed the layer found and left the shape available one level along. This is the missing abstraction rather than a sixth patch. Four properties, each enforced by construction: `ObjectIdentity` is `(kind, exact_name)` and buildable **only** from an engine artifact, so a normalized or provider-supplied string can never become a key; a producer's name yields a `Candidate`, not an identity. `IdentityIndex.resolve()` returns a `Resolution` whose only reader **raises** unless exactly one match exists — there is no `.first()`, no indexing and no truthiness, so "take the first candidate" is not expressible. `add()` appends and never overwrites, and no `set()` is taken where identity is derived (that is what silently deleted a workbook collision). And **normalization is a property of the index, not a call site**: `IdentityIndex(normalized=False)` has no lossy table at all, so an engine-to-engine join cannot slip into one. The test of success is that a *future* join cannot express the ambiguous case. |
| `check_relationship_health.py` | Model-owner gate (#277) for sparse semantic models where a date-bearing non-detached table is disconnected from every Date/Calendar table. Complements `check_field_bindings.py`: the field-binding gate already catches a visual that mixes unrelated grouping tables, while this one surfaces the model-level relationship risk before or independently of a visual exposing it. Reuses `check_field_bindings.py`'s relationship components and `detached_ok` exemptions for field parameters/calculation groups; reports active relationship count, components, Date tables, and stranded date columns. Exit 0 clean / 1 missing relationship / 3 no semantic model measured. |
| `check_pbir_layout.py` | Narrow PBIR layout gate (#278) for a dense main-content column uniformly displaced downward while a separate full-height sidebar masks the vacated Y-range. It is deliberately **not** a generic whitespace or page-height check: a bottom dead zone alone, a single spaced visual, or many low visuals without a sidebar stay clean. Detects the corrected customer shape only; the earlier bottom-dead-zone measurement remains unconfirmed. Exit 0 clean / 1 displaced main column / 3 no positioned report visuals measured. |
| `check_stub_measures.py` | Census (#257) of the `= BLANK()` placeholders the engine emits for calcs it cannot translate — the single largest remaining body of hand-authoring on a live estate. Per-table and model-wide ratios in the `64/89 (72%)` shape, split into **ACTIONABLE** (the source formula survived as `annotation TableauFormula`, so translate it in place) and **DEAD END** (nothing survived; recover it from the Tableau workbook). Detection asks *"is the WHOLE expression a `BLANK()` call"* — comments stripped, whitespace collapsed, redundant outer parens removed, then a **full match** — because a substring search reports `IF(ISBLANK([x]), BLANK(), [y])` as stubbed, which is exactly how an ad-hoc sweep got a real measure wrong. Reads all three TMDL expression forms (inline, indented block, ``` ```-fenced). **Exit 0 even with stubs** — mid-migration they are the expected state and a gate that always fails gets muted; `--strict` opts into exit 1, and exit 3 means nothing was measured. |
| `sync_agent_conventions.py` | Regenerates the shared-conventions block into all four personas; `--check` fails on drift **and prints each persona's size against the 30,000-char cap**. All four currently sit at ~99%, so this is the budget alarm. |
| `check_navigation_index.py` | Verifies `docs/INDEX.md` as the bidirectional navigation contract: every indexed/excluded path exists, and every eligible Markdown/Power BI KB JSON/check gate is indexed once or explicitly excluded with a reason. Runs in CI whenever agent-facing docs or gates move. |
| `check_path_ceiling.py` | Gate (#235) against a bundle **Power BI Desktop cannot open**. Boundary measured end-to-end (Desktop 2.157.828.0, `LongPathsEnabled=1`): a byte-identical PBIP at **file 259 / dir 247 opened** and answered the Desktop Bridge; at **file 260 / dir 248 it was refused**. Hence **file ≤ 259, directory ≤ 247**, in **UTF-16 code units** — .NET counts code units, Python counts code points, so a path with emoji measures short in Python and is refused by Desktop. ⚠️ Deliberately **one tighter than `PBIProjectUtils.EnsureNotLong`**, which was loaded from `Microsoft.PowerBI.Packaging.dll` and invoked directly: it compares with `>` and **allows** 260/248, throwing only at 261/249 — so the observed refusal comes from a *different* guard (most plausibly the Win32 `MAX_PATH` limit, since `PBIDesktop.exe` is not `longPathAware`; that attribution is inferred). Reports **both** opt-ins — `LongPathsEnabled` *and* `git config core.longpaths` — because one being set while the other was not is exactly how this hid; neither reaches the verdict, which is pure string arithmetic (Linux CI gives identical numbers). ⚠️ git on the same bundle: with `core.longpaths` unset, `git add -A` staged **0 of 179** files and exited 128; and when the overlong path is a **directory** git only *warns*, so `add`+`commit` both exit 0 having **silently dropped the contents**. Portable verdict is the **minimum** remaining budget across every path against *its own* ceiling (a short filename makes the directory rule decisive), with the **binding path named**; `TIGHT ROOT BUDGET` below 40, `--min-root-budget` gates it. Estate `_runs/estate-2.339.0-20260829`: **183 over ceiling (82 files > 259 + 101 dirs > 247), longest 287, root budget 62**, driven by the unit name duplicated in `pbip\<NAME>\<NAME>.Report\`. Unmeasurable paths — including a `surrogateescape`d POSIX filename — are `unknown`, never passing. Exit 0 clean / 1 over ceiling / 2 usage / 3 could not evaluate. **Wired into `check_unit.py` (`all` scope, orchestrator-owned), so it needs no separate command to remember.** Detail: [`docs/windows-path-limits.md`](../docs/windows-path-limits.md). |
| `probe_desktop_credential.ps1` | A bounded Desktop dialog observation: a positive credential prompt (`CREDENTIAL_MISSING`, exit 1), a non-clean/indeterminate dialog (`REFRESH_IN_PROGRESS` / `DIALOG_NEEDS_HUMAN` / `DIALOG_UNRECOGNIZED` / `DIALOG_UNREADABLE`, exit 3), or no detected prompt (`CREDENTIAL_PRESENT`, exit 0 — an absence of evidence, not proof of a cached credential). The 1-row data probe remains the gate of record. |

## Toolkit maintenance

| Script | What it does |
|---|---|
| `build_plugin.py` | Generates the `powerbi-playbook` marketplace plugin from `.github/skills/`. `--check` fails on drift. **Re-run and re-publish after editing any shipped bundle** — the plugin copy shadows the repo copy for a subagent, so an unpublished edit is served stale and silently. |
| `bundle_corpus.py` | Shared helper for locating shipping `.Report` and `.SemanticModel` folders. Keeps the `pbip/`-first bundle rule in one place so artifact gates and `check_unit.py` do not grow copy-pasted `find_reports()` / `find_models()` variants. Also holds `classify_target()`, the no-follow **package-boundary classifier** every gate consults before it resolves or discovers anything. |
| `package_source.py` | Internal pure projection (#558), consumed only by `check_reference_readiness.py`: S2's root-bound handoff returns the exact declared Tableau asset role, kind and SHA, or preserves a prerequisite refusal. No discovery, file reads, hashing or ancestor lookup. Workbook consumers keep their own workbook source even when their model belongs to a datasource provider. Public source paths are package-relative; package `--source` is a fixed usage refusal before any following check. |
| `package_filesystem.py` | Library only, imported by `check_reference_readiness.py`. Proves a self-contained package's root `package-manifest.json` is strict readable JSON whose `contents.files` describes **exactly** the regular files under it, and that each still hashes to the recorded SHA-256. Strict parse (duplicate keys refused at any depth, `NaN`/`Infinity` refused), canonical package-relative POSIX keys (backslash, absolute, UNC, drive-qualified, `..`, control characters, trailing dot/space, reserved devices incl. `COM¹`, case/trailing aliases, and every character no Windows filename may hold — `< > : " \| ? *` — all refused, by **ordinal** so an unsafe spelling is never echoed), then one top-down `os.scandir`/`lstat` walk where a reparse point is a finding **and** a dead end. It never calls `resolve`/`rglob`/`is_file`/`is_dir`/`exists` and never opens a path built from a manifest key, so bytes outside the package are never read. Unassessable is a state of its own, never clean. ⚠️ The manifest is unsigned and excludes itself, so this detects accidental damage and confused composition, **not** an adversary who rewrites a file and its manifest entry together. |
| `package_role_identity.py` | Library only, imported by `check_reference_readiness.py`. The **role and identity** half of the entry gate (#562 S2): it runs after the boundary classifier and after `package_filesystem.py`, and answers whether a package carries exactly the roles its kind and topology require and whether every stable identity claim those roles make agrees. It verifies a **cohort**, because a workbook that points at a Tableau PUBLISHED datasource has no model of its own and cannot prove its provider from one package - `check_reference_readiness.py <provider-package> <consumer-package>` is one operator command that gives the verifier the set it needs. Role states are `resolved` / `not_applicable` / `missing` / `ambiguous` / `mismatch`, and only `resolved` or an **earned** `not_applicable` passes. ⚠️ **A role is a DECLARATION the bytes confirm, never a discovery**: deleting `artifacts.asset` while the file remains is `missing`, and the file is not rediscovered by scanning `assets/`, by reading the handover slice's `source_id` or by matching a display name - that rediscovery is the fail-open this slice closes (measured on master: the entry gate returned `READY 4/4`, exit 0). ⚠️ **The two Tableau LUID namespaces are typed and never interchangeable** - a workbook LUID in a datasource's provenance (or the reverse) is a category error, not a spelling difference. Provider matching is datasource LUID first, then the exact `<site>/<name>` published key only when a LUID is genuinely unavailable on both sides; `bound_datasource`, `published_ds_name`, folder stems and captions are diagnostics and admit nothing. It **returns no source `Path` and performs no source search** - that is #558 - writes nothing into a package, and refuses rather than raises, so no host path escapes in a traceback. It re-runs the no-follow walk and opens only paths that walk produced, so a manifest key is never joined onto the root. | consumed by the entry gate; called directly only by its own tests |
| `path_flavour.py` | **Answers a path question in the flavour of the LITERAL, not of the host** — imported by `package_unit.py` and `set_data_folder.py`, library only. Containment, separator choice, composition and leaf extraction all change answer with flavour, so a packager that reads a customer's `.tmdl` on one platform and ships it to another must not let the machine decide. Three measured defects share that shape (blind review of #463 round 2): `_inside()` used `PureWindowsPath` unconditionally, whose comparison is **case-insensitive**, so on Linux a source at `/data/Extract.csv` was judged inside a package at `/DATA` and was skipped by localization *and* by the post-rewrite scan — silence on a data-loss-shaped question; `_classify_source()` used the host `Path`, and on Windows `Path("/Users/<name>/README.md").is_file()` resolves against the **current drive**, so a foreign macOS literal matched local bytes that were then packaged as the customer's source; and `set_data_folder.py` composed with a literal backslash, writing `/tmp/package\data\...` on POSIX as one segment, reporting the folder missing and exiting 1 *after* the file was already rewritten. ⚠️ **Nothing here touches the filesystem** — probing a UNC literal blocks on SMB name resolution for minutes (#462 measured one test module going 30 s → 52 min), and `Path.resolve()` on a foreign literal is the reinterpretation above. Callers that must probe ask `is_host_native()` first. |
| `engine_source.py` | **The one place the deterministic conversion engine is resolved** (issue #107). Returns the installed `tableau-fabric-skills@tableau-collection` plugin — the single canonical source — and **raises** rather than falling back to a second copy, because a silent fallback is what let 2.113.0 and 2.126.0 build one pipeline between them (deprecated Bing `shapeMap` and a dropped density-map worksheet on one side, `azureMap` + heat layer on the other, with nothing in the output saying which ran). Also names every non-canonical tree it can see, which is how `preflight.ps1` blocks on a second install, and supplies the provenance block (`root`/`version`/`canonical`) that `run_estate.py` stamps into every bundle's `engine-output-receipt.json`. `--json` is preflight's input. |
| `check_engine_receipts.py` | Walks bundle receipts and WARNs when their recorded `engine.version` differs from the installed canonical engine. `preflight.ps1` surfaces this advisory check, so operators can re-run stale bundles between migrations without blocking work already in flight. |
| `skill_plugin_source.py` | **The one place the installed skill plugin is resolved** — the same job `engine_source.py` does for the conversion engine. Discovers the plugin by scanning `~/.copilot/installed-plugins/*/*/skills/` for this repo's shipped bundle names, rather than hard-coding a marketplace/plugin name: the published name has already changed once (`powerbi-playbook` → `powerbi-playbook`), and the hard-coded path is why `sync_installed_skills.py` silently errored instead of syncing. **Fails loudly when more than one install carries the bundles** — two copies of a skill means one shadows the other, the same hazard `engine_source.py` blocks on. `--plugin-root` / `POWERBI_SKILLS_PLUGIN_ROOT` override it, so the next rename needs no code change. |
| `sync_engine_plugin.py` | Brings the **installed engine plugin** up to date **in place, mid-session** from a checkout, when `copilot plugin update` is blocked by a running session's file lock. Content of `skills/tableau-migration` only — the plugin's own manifest/version still needs a real `plugin update` between sessions. **Refuses a downgrade** unless `--allow-downgrade`: walking the canonical engine backwards turns a cleanup into a map-output regression. `--check` reports drift and exits 1. |
| `sync_installed_skills.py` | Brings the **installed** plugin's bundles up to date **in place, mid-session** — the fix when `copilot plugin update` returns `Access is denied. (os error 5)`. The reference is built from the locally available merged/default-branch ref (`origin/master` by default), never from the caller's feature worktree; use the loud `--from-worktree` opt-in only to test unmerged skill content deliberately. That lock only blocks renaming the top two plugin directories; files inside stay writable, and `plugin update` fails solely because it swaps the directory. Content only: a manifest/version/MCP change still needs a real `plugin update` between sessions. `--check` reports drift and exits 1. |
| `update_playbook_plugin.ps1` | The between-sessions path: kills every Copilot CLI process (and its children) so the plugin directory unlocks, then runs `marketplace update` + `plugin update` and verifies with `preflight.ps1`. **Run from a plain PowerShell window, not from inside Copilot** — it kills the session you would be typing into. Prefer `sync_installed_skills.py` when only bundle content changed. |
| `work_dirs.py` | **The single run-path authority and CLI setup step.** `python -B scripts/work_dirs.py <slug> --json` allocates the canonical `_runs/<NNN>-<slug>/` tree and automatically attempts to write the ignored, checkout-local `_MIGRATION.md` navigation snapshot. Allocation can succeed with a navigation warning; do not reallocate for it. `--select-run ABSOLUTE-EXISTING-RUN` validates one explicit local/no-reparse existing run and refreshes that note without allocation or migration. Paths derive from `RunPaths`, not caller CWD; display controls are visibly escaped without changing filesystem identities or JSON paths. The note lists expected bundle/oracle/package destinations and the portable `<package>\fabric` convention; it is not status, readiness, liveness or current-run authority. Library allocation/listing and `--verify` do not write it. Unmarked collisions, including an empty/partial note with a lost generator marker, are preserved: inspect and preserve the file, then explicitly clear the collision before setup reselects the existing run. `--runs-parent PATH` and `--repo-root PATH` are mutually exclusive aliases for an external run parent. Repo-local runs are covered by this checkout's `/_*` rule; an external run does not inherit that protection. `deliverables/` remains lazy and separate from disposable `scratch/`. | any script needing a run-scoped path |
| `probe_lab.py` | Agent-behaviour test harness. `make` generates **minimal** Tableau fixtures (one live source, two columns, no calculations) so the "probe the source before building" decision is reached in ~2 min instead of ~20; `watch` polls a running migration and returns PASS/FAIL/TIMEOUT so a deviation can be killed on sight. Writes to the gitignored `_probe-lab/`. Use when changing any instruction whose effect is only visible in agent behaviour. |

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
| `trace_customer_text.py` | The artifact table in [`docs/customer-text-exposure.md`](../docs/customer-text-exposure.md) — **which artifacts carry the customer's field names, formulas and titles into an agent's context.** `inject` stamps uniquely-greppable, instruction-shaped sentinels into a `.twb`/`.tds`/`.twbx`; `trace` reports where they landed, in file NAMES as well as content, plus the sentinels that reached nothing (the negative result that bounds the claim). Answers a privacy question and a prompt-injection one with the same run, and neither answer survives an engine upgrade — the engine is not pinned, so re-run it rather than trusting the committed table. |
| `provision_tableau_estate.py` | **The Tableau site as code** — the one direction the rest of the toolkit does not go. `capture` enumerates a site, downloads every asset and writes a re-appliable manifest; `apply` recreates projects (shallowest-first), groups, then **datasources before workbooks**, because a workbook published first silently rebinds to nothing; `seed` gives every empty leaf project something migratable, and `--refresh-seeded` republishes existing seeds after a template change. Projects are keyed by path **segments, never a joined string** — the trial site has a project literally named `R/D`, which made a `/`-joined identity ambiguous and mislabelled its seed. Asset filenames come from what TSC *returns*, never guessed: it appends its own suffix (`X.twbx.twbx`) and hands back `.twb` for an unpackaged workbook. `capture` **refuses an `--out` git does not ignore** (`--allow-unignored-out` overrides): `manifest.json` names every project, workbook and datasource on a live site, and this repo is public. `--refresh-seeded` **downloads a candidate before overwriting it** — `Seed - ` is a naming convention, not proof of authorship, so a genuine workbook of that name is refused rather than replaced by the stub, and `--dry-run` runs the same check so the plan reports the refusal the run would make. Permission RULES and group **membership** are deliberately not reproduced — LUIDs differ on a fresh site, and a half-correct permission model is worse than an absent one; ownership, tags, certification, workbook descriptions and extract data are not reproduced either, and `apply` prints that whole list. `content_permissions` and `show_tabs` **are** captured and re-applied on create. | after any hand-edit of the trial site, and before a trial lapses |
| `make_seed_workbook.py` | Builds the ~1.8 KB self-contained `.twbx` that `provision_tableau_estate.py seed` publishes: one CSV, one worksheet, one dashboard. Exists because **an empty project is an inert fixture** — the trial site's 11-deep `ZZ Deep` chain and every deliberately hostile project name (`R/D`, `R+D`, `Trailing dot.`, `Ventes françaises`) held nothing, so the path handling they were built to stress was never exercised. Two non-obvious requirements, both measured against Tableau Cloud: a `<windows>` entry per sheet (or publish fails *"not associated with any window"*), and `<metadata-records>` for every column (without them the engine cannot type a model and the whole estate comes back `DOD_FAILED, bound=0/N`). Every attribute in the template is **single-quoted**, so names are escaped with quotes included — `saxutils.escape` alone covers only `&`/`<`/`>`, and a project named `L'Équipe` stopped the file being XML. Sheet names truncate the **raw** base then append the suffix: slicing an escaped string splits entities, and composing first made the worksheet and dashboard collide at 54 characters. The internal `.twb` entry name is sanitised, decoupled from the display name, so a `/` in the name cannot create a directory inside the archive. | called by `provision_tableau_estate.py seed` |
