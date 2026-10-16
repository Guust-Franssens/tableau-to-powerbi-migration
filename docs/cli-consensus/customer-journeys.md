# Customer/operator journeys for a core CLI

## 1. Verdict

Propose a thin `t2p` lifecycle facade over the existing scripts and gates, with explicit run location, evidence-derived `status`/`next`, and native gate ownership visible in every result. The facade concept in #734 is sound; the design needs a blocking addition: pre-allocation path-budget assessment before any Tableau survey or harvest. Local `.tds`/`.tdsx`, full-server fan-out, and safe resume also need explicit journeys; current front doors do not prove all three.

## 2. Scope and method

Read `AGENTS.md`, `docs/INDEX.md`, the named runbook/phase/start/reference/path docs, `scripts/README.md`, and the named implementation scripts. Drafted the command set and journeys before opening issue #734 or its comments; then compared the draft with the proposal and the path-location requirement. Repository behavior below is marked ✅ verified; ⚠️ inferred/proposed; ❌ known gap. Citations identify the source of each behavior. All target values are placeholders.

## 3. Journeys

The phase table applies to **each** journey below; journey rows call out scope-specific decisions and deviations. Ask the five questions once in the initial brief—scope/ordering/destination, autonomy, fidelity bar, fallback, refresh strategy—and include conditional Gate-B answers there (`AGENTS.md:331-381,435-443`). Current guidance asks Gate B after parse/probe; ⚠️ proposed UX is to show new evidence at that gate and apply the pre-authorized answer, not ask again. A physical sign-in prompt remains a human stop.

| Phase | User decision / agent asks | Evidence vs claim |
|---|---|---|
| Preflight/setup | Decide the operator host and session-start update vs plain migration-start check; ask before changing tools. | ✅ Matrix/exit 0 or 1 proves dependency readiness only; it does not prove a migration can complete (`scripts/preflight.ps1:6-39`; `docs/operator-runbook.md:119-129`). |
| Survey/assess | Put target scope in the initial brief; ask once whether partial/degraded inventory is acceptable. | ✅ Survey JSON + assessment JSON/DB; sparse usage makes tiers unproven and degraded primary listings block scoping. “All usage is known” is not established (`docs/operator-runbook.md:553-632`). |
| Plan/ordering | Confirm what to migrate and model-first order in the initial brief. | ✅ REST dependencies plus lineage plan; Metadata API-only “unused” does not prove unused (`docs/operator-runbook.md:639-668`). |
| Harvest | Approve included workbooks/datasources and local artifact retention up front. | ✅ Parse-sweep identities/counts and downloaded files; exit 0 alone does not prove scope identity or complete harvesting (`docs/operator-runbook.md:670-708,722-789`). |
| Reference capture | Choose manual screenshots or Tableau capture and accept evidence grade/state in the brief. | ✅ Hashed manifest/capabilities; screenshot bytes do not prove depicted object/state, and oracle images are default-state layout/text evidence (`docs/reference-capture.md:59-77,343-345`; `scripts/capture_tableau_reference.py:585-607`). |
| Conversion | Confirm storage policy and exact scope before conversion. | ✅ Bundle, handover, input-scope bridge, engine receipt; engine success alone is not safe-consumption/fidelity proof (`docs/operator-runbook.md:709-808`; `scripts/run_estate.py:18-27,76-89`). |
| Build/fix | Select faithful vs modernized report and approve residuals; record in brief before agent handoff. | ✅ Shipped `pbip/` or package bytes + declared/replayable edits; builder narration or a render alone does not prove fidelity (`AGENTS.md:505-543`; `docs/migration-phases.md:120-142`). |
| Validate/gates | Agree up front to act on authoritative findings; no new preference question. | ✅ Entry `START_READY` is input readiness; only all-scope `check_unit` with caller-held receipt SHA can claim `COMPLETE`. A green structural validator alone is not correctness (`docs/migration-phases.md:356-380`). |
| Refresh/credentials | Choose scripted/operator/XMLA strategy up front; human sign-in remains a stop, never agent approval. | ✅ Probe/audit evidence is source-specific; a credential marker is not reachability, and `authorize` means build-only/unvalidated (`AGENTS.md:349-381,435-443`; `scripts/credential_gate.py:1144-1159,1234-1247`). |
| Package/promote | Confirm unit slug/kind and exact checked snapshot before promotion. | ✅ Package manifest + receipt-pinned gate + promotion envelope; promotion is not deployment or proof of successful open (`docs/migration-phases.md:146-183,365-420`; `scripts/promote_unit.py:115-122`). |
| Deploy/verify | Confirm existing target workspace, dry-run item count, and deployment authorization. | ✅ Deploy journal and read-only binding verification; neither proves visual fidelity or source-data parity (`scripts/deploy_estate.py:169-182,1915-1975`; `scripts/verify_bindings.py:585-630`). |
| Resume/teardown | Select exact run; preserve hand edits; delete nothing except run-owned scratch. | ✅ Run manifest/location verification and artifact-derived status; diagnostic status is not readiness, and only `scratch/` is disposable (`docs/migration-phases.md:41-48,58-98`; `scripts/run_status.py:880-902,942-987`). |

### 3.1 One `.twb` / `.twbx`, optional supplied screenshots

`start --input <workbook> [--reference <dir>]` → parse → convert → build/fix → gates. User chooses whether screenshots exist and their provenance/state; screenshot naming/manifest acceptance is evidence, not automatic fidelity sign-off (`docs/start-with-one-workbook.md:66-102,104-165`). ✅ `parse_tableau.py` accepts either workbook extension; optional images do not block deterministic conversion (`scripts/parse_tableau.py:1-6`; `docs/start-with-one-workbook.md:136-149`). ⚠️ CLI should retain the source and manifest paths in the run.

### 3.2 Standalone `.tds` / `.tdsx` (model-first)

`start --input <datasource>` creates a **model-only** unit; ask whether to materialize data or stop at an unvalidated/model-only path only when parsing/probing establishes that need. ✅ Parser accepts `.tds`/`.tdsx`; datasource packages carry source/spec and have no report/handover slice (`scripts/parse_tableau.py:1-6`; `docs/migration-phases.md:186-190`). ❌ Current `start_migration.py` is workbook-centric and does not support datasource-only project scopes (`scripts/README.md:38-49`).

### 3.3 Folder of workbooks

`start --input <folder>` runs one pipeline run, with per-workbook units; confirm the folder is the intended scope and ordering derives from parsed specs. ✅ `run_estate.py` is the folder entry point and emits per-workbook handovers (`AGENTS.md:213-218`; `scripts/README.md:76`). Local screenshots are optional reference inputs. Survey/assessment/harvest are not implied by a local folder.

### 3.4 Tableau Server/Cloud scopes

| Scope | Proposed journey / decision | Current coverage |
|---|---|---|
| One workbook | `start --server <server> --site <site> --workbook <name-or-LUID>`; confirm exact identity. | ✅ `start_migration.py` accepts workbook selection and checks selected LUIDs against survey/harvest/reference artifacts (`scripts/start_migration.py:71-80,451-550,585-649`). |
| One project | Same with `--project <name-or-LUID>`; confirm exact project and its dependent published datasources. | ✅ Selector exists; ⚠️ assessment still site-wide and project capture is scoped via derived identities (`scripts/start_migration.py:74-80,480-499`). |
| Whole site | Omit selectors; confirm the configured site and full site scope. | ✅ Survey/assessment/harvest/reference/bundle front door supports one configured site; exit 0 means bundle + references captured, not readiness (`scripts/start_migration.py:1-8,451-538`; `scripts/README.md:38-49`). |
| Full server | `plan --server <server> --scope server`, then explicit confirmation; execute as site-scoped child runs. | ❌ No full-server/multi-site front door found; current wrapper uses configured server/site and one invocation is one site/environment (`scripts/start_migration.py:480-489`; `scripts/README.md:41-49`). |

Project reference capture has no project selector of its own; pass the selected workbook LUIDs in one oracle invocation because its manifest is replaced wholesale on each invocation (`docs/reference-capture.md:92-123`; `scripts/capture_tableau_oracle.py:1615-1673`).

### 3.5 Brownfield: prior run, partial bundle, dropped session, hand edits

`status --run <run>` → `next --run <run>`; select the physical run explicitly after a dropped session. ⚠️ `next` should propose—not silently perform—the missing legal stage after checking artifacts and receipts. Preserve package `fabric/` edits; do not repackage over edits or rerun an existing mixed-version bundle (`docs/migration-phases.md:120-142,408-420`; `AGENTS.md:278-289`). ✅ Existing `run_status.py` is read-only/non-certifying; `work_dirs --select-run` selects an explicit existing run, while verify detects moved/unverifiable locations (`scripts/README.md:54-58,77`; `scripts/work_dirs.py:59-89,1300-1397`). A session transcript or status string alone is not evidence.

## 4. Proposed core CLI

`t2p` should be a thin facade, not a second state/policy authority. JSON includes `status`, owning gate, artifact references, and `native_exit_code`; preserve meaningful findings/unknown/incomplete outcomes. A successful capture command must never print “migration complete.” ✅ Native exit contracts differ today, so flattening them would be lossy (below).

| Command | Syntax | Journeys served | Existing scripts wrapped (path:line) | Exit codes / outputs to preserve |
|---|---|---|---|---|
| doctor | `t2p doctor [--session-start]` | All | `scripts/preflight.ps1:23-39`; `docs/operator-runbook.md:119-129` | 0 ready / 1 critical miss; `--Update` only at session start. |
| start | `t2p start --input <file-or-folder> [--reference <dir>] [--runs-parent <short-root>]` or `t2p start --server <server> --site <site> [--project … \| --workbook …]` | Local files, folder, datasource, server scopes | `scripts/parse_tableau.py:1-6`; `scripts/run_estate.py:3493-3548`; `scripts/start_migration.py:71-80,451-538` | Preserve parse/run-estate `2`, run-estate `0–11`; live wrapper `0` is capture-only, failures/non-established evidence remain visible (`scripts/start_migration.py:7-8,451-680`). |
| status / next / paths | `t2p status|next|paths --run <path> [--json]` | Resume and all run locations | `scripts/run_status.py:2,983-987`; `scripts/work_dirs.py:1308-1397`; `scripts/check_path_ceiling.py:608-640` | Diagnostic success is not gate pass. Keep location verify 0 intact / 1 moved / 2 duplicate / 3 unverifiable (`scripts/work_dirs.py:80-89`). |
| evidence | `t2p evidence capture --run <path> [--manual <dir> \| --tableau]` | Single/folder/server references | `scripts/capture_tableau_reference.py:571-615`; `scripts/capture_tableau_oracle.py:1615-1673,2000-2006` | Preserve `--force`, `--manual-validation-grade`, `--manual-object-type`, `--reference-best`; oracle 0/1/2/3/4/5 retain meanings including “no views selected” (`scripts/capture_tableau_oracle.py:2000-2006`). |
| unit prepare | `t2p unit prepare --run <path> --unit <id> --brief <file>` | Build/fix and package handoff | `scripts/package_unit.py:397-403,6889-6949` | 0 assembled; preserve package `1–6`; never discard package edits without explicit `--discard-package-edits`. |
| validate | `t2p validate --run <path> [--unit <id>] [--scope model|report|integration|all]` | All, before handoff/promote | `scripts/check_reference_readiness.py:2216-2295`; `scripts/check_unit.py:4152-4169` | Reference 0 ready / 1 findings / 3 cannot establish; unit all-scope COMPLETE only with `--receipt-sha256 <H>`; layer 0 is not COMPLETE. |
| credentials / refresh | `t2p credentials list|reprobe --run <path>`; `t2p refresh --run <path> --mode scripted|operator|xmla` | Live sources and brownfield resume | `scripts/credential_gate.py:1144-1159,1185-1247`; `scripts/refresh_pbip_model.py:1-35`; `AGENTS.md:362-381` | List/status 0/1/3 meanings; missing credentials stop for a human; `authorize` is not earned reachability. |
| promote | `t2p promote --run <path> --unit <id> --dry-run` then explicit apply | Package to deliverable | `scripts/promote_unit.py:115-122,1414-1455` | Preserve 0–6 and usage 64; dry-run is no-write; carry exact caller receipt SHA. |
| deploy / verify | `t2p deploy --run <path> --workspace <id> --dry-run` then explicit apply; `t2p verify --workspace <id>` | Deploy/verify | `scripts/deploy_estate.py:169-182,1915-1975`; `scripts/verify_bindings.py:118-120,590-630` | Deploy 0 success / 1 failed / 2 preflight refusal / 3 knowingly incomplete; verify 0/1/2. Preserve journal, item count and read-only verify output. |

Flags not to silently drop: survey `--server --site --json` (`scripts/run_engine_survey.py:3-17,52-61`); assessment `--survey --out` (`scripts/assess_estate.py:1691-1702`); lineage `--plan --survey` (`scripts/tableau_lineage.py:999-1017`); harvest `--db --out` (`scripts/harvest_estate_assets.py:2190-2248`); run-estate `--scope-survey --storage-decision --dry-run` (`scripts/run_estate.py:3493-3549`); reference `--force --structural-only --manual-validation-grade --manual-object-type` (`scripts/capture_tableau_reference.py:585-607`); oracle `--workbook-id --reference-best --retry-failed-from` (`scripts/capture_tableau_oracle.py:1615-1673`); deploy `--dry-run --journal --skip` (`scripts/deploy_estate.py:1915-1945`). Never put a PAT/secret in CLI arguments (`scripts/run_engine_survey.py:7-10,58-61`).

Native outcomes are not interchangeable: preserve the child code and gate owner in JSON. ✅ Survey wrapper returns the engine's exit (`scripts/run_engine_survey.py:15-17,52-61`); assessment uses 0/1/3 (`scripts/assess_estate.py:1858-1862`); lineage 0/1 (`scripts/tableau_lineage.py:1050-1127`); run-estate 0–11 (`docs/operator-runbook.md:793-808`); reference capture 0/1/2/3 (`scripts/capture_tableau_reference.py:503-568`); oracle 0–5 (`scripts/capture_tableau_oracle.py:2000-2006`); path scan 0/1/2/3 (`scripts/check_path_ceiling.py:232-240`); package 0–6 (`scripts/package_unit.py:397-403`); promotion 0–6/64 (`scripts/promote_unit.py:115-122`); deploy 0–3 (`scripts/deploy_estate.py:169-182`); verify 0/1/2 (`scripts/verify_bindings.py:118-120`).

## 5. Path-location budget design

| Concern | Proposed contract |
|---|---|
| Choose/show root | Default repo-local `_runs/<NNN>-<slug>/`; `--runs-parent <short-root>` allocates the same canonical tree externally. `start`, `status`, `next`, `paths` show root and parent in text/JSON; `start` prints physical root at beginning/end (`scripts/work_dirs.py:17-27,1311-1325`; `scripts/start_migration.py:497-503`). |
| Three path identities | `run.json.allocated_abs_path` is the recorded canonical absolute path and must match the actual physical run location; report both, plus repo-local/external and git-ignore/visibility. `subst` is a temporary same-user alias only—show separately, never record it or treat it as portable (`scripts/work_dirs.py:74-89`; `docs/windows-path-limits.md:339-365,408-417`). |
| When and what to measure | At allocation, **before survey or harvest**, project full expected PBIP file and directory paths (including longest emitted-unit tail), in UTF-16 code units; show longest file/dir and remaining budgets against file 259 / directory 247. Do not measure just the root (`docs/windows-path-limits.md:100-110,265-268`; `scripts/check_path_ceiling.py:207-240,374-469`). |
| Failure behavior | Over-ceiling **or cannot-establish projection**: refuse before network/download, retain no false “ready” run, and suggest `t2p start … --runs-parent <short-root>` as a **new** allocation. Never symlink/junction and never weaken ceilings. ✅ Current `run_estate` does pre-engine projection and actual emitted-tree scan, but the live front door reaches that refusal after survey/harvest; ⚠️ move/add the required allocation-time estimate (`scripts/run_estate.py:492-585,3615-3711`; `scripts/start_migration.py:585-649`; `docs/windows-path-limits.md:466-485`). |
| Resume / alias | Resume only when recorded and physical location positively match; moved or unverifiable run is a refusal, not a rewrite. A `subst` alias may aid a same-user Desktop open, but gates resolve/check the physical tree and it never waives path, package, or promotion gates (`scripts/work_dirs.py:74-89`; `docs/windows-path-limits.md:351-365,408-417`). A short-root retry is a **new** run; retain the old run as evidence (`docs/windows-path-limits.md:366-371`). |

## 6. Agreements / disagreements with the existing proposal

- **Agree:** thin facade over existing authorities; lifecycle tiering rather than one command per script; artifact-derived `status`/`next`; human-readable plus JSON; live child output; no second ledger. This matches #734’s proposal and `scripts/run_status.py:2,942-987`’s non-certifying role.
- **Agree with owner comment:** `paths` is a core command (or equally visible in every status); expose recorded vs physical path, root kind, full-file/full-directory budget and rerun action. The additional pre-survey/harvest check is necessary because the current `start_migration.py` only encounters `run_estate`’s path gate after collecting inputs (`scripts/start_migration.py:585-649`; `scripts/run_estate.py:3617-3622,3664-3694`).
- **Disagree / refine:** the independently drafted `resume` command is unnecessary if `status` + `next` select a safe next command without mutation. `unit prepare`/`validate` should be core because packaging and exact-snapshot gates are not optional cleanup. Do not imply `start` covers full-server or standalone datasource yet; introduce those routes explicitly and stage multi-site server work as separate site runs.
- **Exit design:** #734 requests consistent CLI exits; keep a small stable outer status vocabulary, but always include the exact native child/gate code and owner. A normalized “command ran” exit must not convert capture-only success, incomplete deploy, findings, or `CANNOT_ESTABLISH` into migration success.

## 7. Risks, open questions, what I could not verify

- ❌ Full-server enumeration/fan-out and cross-site run ownership are not present in the reviewed front door; confirm whether v1 explicitly excludes them.
- ❌ Standalone datasource-only execution through `start_migration.py` is not supported. Prove model-only package, refresh and completion semantics before calling it end-to-end.
- ⚠️ Allocation-time exact path prediction needs converter output-name evidence before server harvest. If it cannot conservatively establish the worst file **and** directory, fail closed and ask for a shorter physical root.
- ⚠️ Public capture’s Server REST flag is not wired; use the oracle route and do not upgrade its evidence grade (`scripts/capture_tableau_reference.py:579-583`; `docs/reference-capture.md:343-345`).
- ⚠️ A preflight could not be run here: `powershell` is unavailable; no PowerShell startup refusal occurred. The claim is based on inspected docs/source, not a successful environment check.
- ⚠️ This deliverable is docs-only; no tests/build run. Check whether `docs/INDEX.md` should later point to this consensus file without violating the one-file scope of this review.

## 8. Next step

Proceed with a small facade design review against these journeys; make allocation-time full-path projection and the supported-scope matrix acceptance blockers. Keep `t2p` a navigation/composition layer, then validate one local workbook, one datasource, one scoped site, and one interrupted hand-edited run before retiring script-level agent guidance.
