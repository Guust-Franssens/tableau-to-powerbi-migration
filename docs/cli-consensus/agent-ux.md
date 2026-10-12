# `t2p` consensus — agent/human UX

**Lens:** caller experience. ✅ verified; ⚠️ proposed/inferred; ❌ known gap. Examples are proposed.

## 1. Discovery

✅ Today: this script toolkit (`pyproject.toml:45-50`) routes to many scripts (`docs/INDEX.md:5-20`;
`scripts/README.md:31-77`); live-site `start_migration.py` is the front door, not `t2p` (`scripts/README.md:38-52,60-65`).
❌ A cold caller cannot discover commands from `t2p`; no such console command is documented.
⚠️ Proposal: registry-backed `t2p --help` and `t2p <noun> --help` (≤40 lines each), `t2p help <topic>` and
`t2p explain <code>` (≤20); show purpose, inputs, outputs, next command, evidence owner and docs anchor,
never source or a giant README.

### Proposed command tree

```text
t2p doctor [--paths] [--run <path>]                 CORE; wraps preflight.ps1/work_dirs (scripts/README.md:26,1228)
t2p init <slug> [--runs-parent <short-root>]        ADVANCED; wraps work_dirs.py (scripts/README.md:34-36,1228)
t2p start [--workbook|--folder|--server ...] [--run <path>] CORE; composes route (scripts/README.md:38-46,64)
t2p survey [--server ... --site ...]                ADVANCED; wraps run_engine_survey.py (AGENTS.md:215)
t2p plan                                             ADVANCED; wraps assess_estate.py + tableau_lineage.py (AGENTS.md:215)
t2p harvest                                          ADVANCED; wraps harvest_estate_assets.py (AGENTS.md:215)
t2p capture reference|oracle                         ADVANCED; wraps capture_tableau_reference.py / capture_tableau_oracle.py (AGENTS.md:246-260)
t2p convert                                          ADVANCED; wraps parse_tableau.py or run_estate.py (AGENTS.md:216-217; scripts/README.md:76)
t2p build                                            ADVANCED; NEW handoff, not a second converter
t2p check [--scope model|report|integration|all]     CORE; wraps check_unit.py (docs/INDEX.md:8)
t2p status|next [--run <path>] | why <unit|gate|code> CORE status wraps run_status.py; why ADVANCED (scripts/README.md:77)
t2p brief <unit>                                     ADVANCED; NEW, renders existing brief + evidence
t2p resume --run <path>                              ADVANCED; NEW, selects existing run (scripts/README.md:54-57)
t2p deploy --run <path> --workspace <name> [--dry-run] CORE; wraps deploy_estate.py (AGENTS.md:323; docs/operator-runbook.md:1137-1153)
t2p credentials list|reprobe --run <path>            ADVANCED; wraps credential_gate.py / reprobe_blocked.py (docs/INDEX.md:10-17)
t2p runs list|verify                                 ADVANCED; wraps work_dirs.py (scripts/README.md:33-57,1228)
t2p completion {bash,zsh,fish,powershell}             ADVANCED; NEW, same registry as help
```

⚠️ Core (6): doctor/start/status/next/check/deploy; advanced: the other listed commands. Internal
helpers (`engine_source.py`, `tableau_env.py`, `migration_bundle.py`) stay out of help/personas; the
facade invokes them, not as 116 script commands (issue #734; `scripts/README.md:67-72,1222`).

⚠️ Completion shares help's registry; `NO_COLOR`/non-TTY suppress color/spinners. Stdout is data,
stderr prose; commands print created absolute paths. `start` prints physical run path first/last.
Prefer Python except preflight, which precedes Python (`scripts/README.md:11-26`); Windows spelling is not budget proof (§6).

## 2. Inputs and references

✅ Local single-workbook route uses `.twb/.twbx` plus `reference/tableau-<exact dashboard name>.png`
(`docs/start-with-one-workbook.md:66-102`); folders use `run_estate.py --input <folder> --output <bundle>`,
sites use `--server`/`--site` then survey/assessment/lineage/harvest (`AGENTS.md:213-218`).
✅ PAT secret comes from `.env` or exported environment, never CLI arguments (`AGENTS.md:262-263`;
`docs/operator-runbook.md:349-364`).
✅ `capture_tableau_reference.py` adopts local/Public images; `capture_tableau_oracle.py` exports live
Server/Cloud images (`AGENTS.md:236-247`). Oracle exit 4 means no views selected, often a published-name
filter matched none—not impossible capture; read the per-view manifest (`AGENTS.md:249-260`).
⚠️ Proposal: `t2p start --workbook|--folder|--server --site --env-file .env`; accept
`--pat-name` as a non-secret selector, but read the PAT secret only from environment/`.env`.
`t2p capture reference --images <dir>` is for local/Public sources; `t2p capture oracle` is for a
configured site. On oracle exit 4, name the unmatched filter and offer the unfiltered selection
command, not a retry.

## 3. Live progress

✅ No common stream today: site survey emits nothing until done (`docs/operator-runbook.md:570-575`);
deploy has an intent/outcome journal (`docs/operator-runbook.md:1144-1153,1162-1168`); refresh may run
3600 s with conditional row-count/heartbeat evidence (`AGENTS.md:362-379`).
❌ No common progress format is promised across these steps.
⚠️ Proposal: human progress on stderr; `--json-events` NDJSON on stdout or a chosen file. Events:
`run`, `stage`, `state`, `elapsed_s`, `paths`, evidence reference. Otherwise stdout is data. Show
elapsed time after ~60 s. Time-box external waits (~2 min or 3 attempts, unless the script is its own
timer), then ask; missing credentials are final (`AGENTS.md:549-564`).

## 4. `status`, `next`, `why`

✅ `run_status.py --run <absolute-local-run> [--json]` is a read-only, non-certifying inventory with
paths and one action (`scripts/README.md:77`; `scripts/run_status.py:976-986`); run layout has
`run.json`, bundle, oracle and journals (`AGENTS.md:452-473`).
⚠️ Proposal: `status` renders observations; `next` surfaces existing `next_action`; `why` links finding
to owner/command. Help links policy; status does not cache/restate gates; JSON preserves native verdict.
No certification or duplicated policy. Print recorded/physical paths,
`runs-parent`, repo-local/external, git visibility/commit safety, longest file/directory, remaining
budget and pre-engine pass/over/unknown; repeat paths in `status`/`next`, never move an old run
(issue #734 comment).

| Fact | Single authority | `t2p` reads |
|---|---|---|
| Run identity/location | `work_dirs.py` / `run.json` | recorded and resolved absolute paths |
| Parsed source scope | `migration-spec.json` | fields as emitted; never infer missing scope |
| Engine provenance | `engine-output-receipt.json` | recorded engine/version/root |
| Deploy intent/outcome | deploy journal | last recorded item outcome; do not infer from silence |
| Readiness/completion | owning reference, credential, `check_unit.py` gates | native verdict + owner link; no cached verdict |
| Limitations | final artifact `limitations_encountered` | current artifact values, not CLI-maintained copy |

✅ Receipt and spec owners: `run_estate.py` and parser (`scripts/README.md:67,76`). ⚠️ Reader only.

## 5. Stable JSON

❌ No common envelope today. ⚠️ Proposed global `--json` for every command, using `schema: "t2p.v1"`:
`{schema,ok,verdict,exit_code,paths,next,warnings,limitations}`. Add optional fields only in v1;
breaking shape/meaning requires v2. `help json` documents codes/fields. `paths` references outputs;
wrap `migration-spec.json`, receipt, `run.json`, journals and gate results without copying content or
policy (`docs/migration-spec.md:1-3`; `scripts/README.md:76-77`; `AGENTS.md:452-473`).

## 6. Exact paths and Windows budget

✅ Desktop ceilings: 259 UTF-16 file / 247 directory (`docs/windows-path-limits.md:267-271`). Before
survey/harvest, `work_dirs.py <slug> --runs-parent <short-parent> --json` allocates a canonical run;
its paths are authoritative; never move the old run (`AGENTS.md:476-486`).
⚠️ `t2p doctor --paths --slug <slug>` predicts before external work; on overshoot show limits,
predicted longest path, `OVER`/`UNKNOWN`, and exact `t2p init <slug> --runs-parent <short-root>` retry.
Old run remains evidence. If `subst` is active, show both recorded and alias paths; the alias is
temporary/non-authoritative and never persisted (`docs/windows-path-limits.md:319-356`; issue #734 comment).

## 7. Errors and exits

⚠️ Every error: stable code, `REFUSE`/`CANNOT_ESTABLISH`/`TRANSIENT`, cause, object, exact next
command, docs anchor, native source/code. Missing credential/modal sign-in is final; autopilot cannot
override it (`AGENTS.md:558-564`).

| `t2p` exit | Meaning | Compatibility note |
|---:|---|---|
| 0 | Command succeeded; not a blanket fidelity/readiness claim | preserve native result in envelope |
| 1 | Owner refused / findings / blocked credential | native exit remains visible |
| 2 | Cannot establish result or target | distinguish from syntax internally |
| 3 | Transient/operational failure | only retry if owner says plainly transient |
| 64 | CLI usage error | normalize wrapper usage; direct scripts unchanged |

✅ Codes collide: oracle 0–5 (`2` reauth, `3` total failure, `4` no views); `run_estate.py` uses `3` for
DoD and `2` for usage (`AGENTS.md:254-260`; `scripts/run_estate.py:204-215`; `docs/operator-runbook.md:1236-1248`).
Preflight miss is nonzero (`AGENTS.md:72-80`); bindings use 1 findings/2 unable (`docs/operator-runbook.md:1577`).
Preserve callers; map at `t2p` by owner verdict, retain native code/source command; never infer by number.

## 8. Subagent handoff

✅ Subagents receive only persona + task, not `AGENTS.md` or parent conversation
(`docs/agent-architecture.md:25-43`); generated shared conventions stay in personas because silent
loss is harmful (`docs/agent-architecture.md:160-165,228-244`). Brief path:
`migrations/workbooks/<name>/migration-brief.md` (`AGENTS.md:385-390`).
⚠️ New `t2p brief <unit>` prints paths, scope, owner persona, gate evidence/source, next action and
limitations as self-contained dispatch text; reference authorities, do not copy policy.

## 9. Proposed transcript acceptance

⚠️ Acceptance: a cold agent completes each journey using only help and command output, not source or
README. Every example ends with next-step. Outputs illustrative.

```text
$ t2p start --workbook ./source/Orders.twbx --reference-dir ./reference
C:\repo\_runs\001-orders (first output line)
1 image adopted, not fidelity proof | NEXT: t2p convert --run C:\repo\_runs\001-orders
C:\repo\_runs\001-orders (last output line) | exit 0
$ t2p start --folder ./workbooks
Run C:\repo\_runs\002-estate; 3 units | NEXT: t2p check --run C:\repo\_runs\002-estate | exit 0
$ t2p start --server https://tableau.example --site sales --env-file .env
Survey→assessment→harvest→reference→bundle; secret from .env | NEXT: t2p brief sales | exit 0
$ t2p resume --run C:\repo\_runs\002-estate
Existing artifacts/journal re-read; no state recreated | NEXT: t2p next --run C:\repo\_runs\002-estate | exit 0
$ t2p credentials reprobe --run C:\repo\_runs\002-estate
REFUSE CREDENTIAL_REQUIRED; autopilot cannot fill modal | NEXT: human signs in, then t2p credentials reprobe --run C:\repo\_runs\002-estate | exit 1
$ t2p doctor --paths --slug regional-sales
OVER: file 271/259, dir 252/247; old run untouched | NEXT: t2p init regional-sales --runs-parent C:\r | exit 2
$ t2p init regional-sales --runs-parent C:\r
Run C:\r\0001-regional-sales | NEXT: t2p start --workbook ./Regional.twbx --run C:\r\0001-regional-sales | exit 0
$ t2p deploy --run C:\repo\_runs\002-estate --workspace Landing --dry-run
Planned: 2 illustrative items; confirm first | NEXT: t2p deploy --run C:\repo\_runs\002-estate --workspace Landing | exit 0
$ t2p deploy --run C:\repo\_runs\002-estate --workspace Landing
Deployed 2; journal recorded | NEXT: t2p check bindings --run C:\repo\_runs\002-estate --workspace Landing | exit 0
$ t2p check bindings --run C:\repo\_runs\002-estate --workspace Landing
All reports resolve | NEXT: t2p status --run C:\repo\_runs\002-estate | exit 0
```

✅ Dry-run reports item count before mutation; binding verification is separate
(`docs/operator-runbook.md:1144-1153,1577`).

## Open questions, cuts, verdict

⚠️ Not verified: flags, event schema, Windows installation/completion, path projection, or lossless exit mapping. No issue/brief conflict found; the latest #734 comment's path fields are covered in §4/6.
❌ Scope conflict: `docs/INDEX.md:45-47` requires indexing eligible Markdown, but this one-file deliverable leaves INDEX unchanged; validation is not claimed.

**Cut:** per-script verbs, cached status, duplicated policy, second database, source browsing.
**Verdict:** Essential: help, authoritative path/status/next, progress, stable errors, handoff.
Optional: shell completion and `why` enrichment. No new state store/proof mechanism; descope if needed.
