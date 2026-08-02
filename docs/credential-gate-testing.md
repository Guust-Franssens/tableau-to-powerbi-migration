# Credential gate — execution spec

> **Status: in progress on `feat/credential-gate`. Do not merge to master until Phase 3 passes.**
>
> This is the working agreement for finishing the live-source credential gate. It exists because the
> first attempt shipped a design flaw to master and had to be reverted — the failure was not the
> mechanism but the *method*, so the method is now written down.

## What went wrong the first time

Worth keeping, because every rule below traces to one of these:

1. **Only the failure path was ever tested.** A gate that blocks *everything* passes a "nothing was
   built" test perfectly. That single blind spot produced both the deadlock (below) and a 4/4
   "success" result that was partly tautological — writes were denied to a folder, then the folder
   was measured empty.
2. **The gate deadlocked the success path.** The one-row probe that *earns* the clear is itself a
   PBIP built in `fabric/` — the folder the gate denied. So every live-source migration dead-ended
   at "a human must authorize an unvalidated build", credentials or not.
3. **Three distinct failure modes were collapsed into one verdict** (see the taxonomy below).
4. **A destructive test destroyed the fixture.** Revoking the PAT to force a negative case killed the
   only working happy-path setup, which needs manual human effort to restore.
5. **Enforcement blocked its own maintenance** — the hook denied edits to itself, the ACL blocked
   deleting scratch, and deleting the probe sandbox bricked the probe.
6. **It was pushed to master before any of it was proven.**

## Invariants

Violating one means stop and report — not improvise around it.

| # | Rule |
|---|---|
| I1 | The happy path is tested **before** any failure path. No conclusion is drawn from a negative-only result. |
| I2 | Every PASS cites **source-system evidence**, not a script's or agent's self-report. |
| I3 | Destructive tests run **last**, and never before a clean full pass. |
| I4 | Enforcement must never block its own maintenance (edit, delete, re-arm). |
| I5 | Nothing reaches master until T1–T7 pass. Verify the **remote head** after every push. |
| I6 | One change at a time, re-tested. No batching fixes into an untested pile. |

## Oracle (I2)

Primary — Databricks itself:

```powershell
databricks query-history list --max-results 5
```

A reachability PASS **must** show the probe's own SQL (`select <col> from <catalog>.<schema>.<table>
limit 1`) with status `FINISHED`. Secondary: warehouse `state` / `num_active_sessions` — a serverless
warehouse sits `STOPPED` with 0 sessions until a real query arrives, so it cannot be faked by an
optimistic summary. Never accept the probe's own `DATA_OK` as sole evidence.

## Design

**Contract:** *no deliverable model exists for a live source unless a real query returned a real row
from it, through Power BI.*

The inversion that matters: **the classifier no longer stops the run; only the probe does.**

| Layer | Job | Explicitly NOT its job |
|---|---|---|
| `parse_tableau.py` | capture connection details, arm the gate | judge reachability |
| `preflight_source_credentials.py` | decide *which* sources need probing | decide GO/STOP |
| `probe_live_source.py` | **measure** reachability | enforce |
| `credential_gate.py` | **enforce** (ACL), audit, verify | decide |
| `.github/hooks/` hook | explain the denial, terminate the run | be the enforcement |
| `tableau-migrator` persona | sequence them | make the judgement call |

### Why the probe cannot be a shell `SELECT 1`

It would test the **wrong credential**, and a false green light is worse than no test. `databricks
sql`, ODBC and `az` all authenticate as the *agent's* shell identity; Power BI uses a credential
cached per-Windows-user in Desktop's DPAPI store. Measured: the `databricks` CLI queried the
warehouse happily while Power BI had never authenticated to it at all. So the probe must go *through*
Power BI, and the smallest thing Power BI executes is a model — one table, `Table.FirstN(…, 1)`,
refresh, require a row.

### Failure taxonomy

| Signature | Verdict | Retry? |
|---|---|---|
| rows returned | `DATA_OK` → clear gate, build | — |
| refresh **hangs** past the timeout | `NO_CREDENTIAL` — a modal waiting on a human | never |
| error names auth / token / sign-in | `NO_CREDENTIAL` — cached but rejected | never |
| model won't load, "no catalog found" | `UNREACHABLE` — bad host/path, a **spec** bug | never |
| socket 10054 / `msmdsrv` crash | `UNPROVEN` — say so; do not claim the source refused us | once |

## Phases

Each phase gates the next.

### Phase 0 — restore & clean

| Step | Action |
|---|---|
| 0.1 | Mint a fresh PAT (`pbi-migration-test`, 48h). Leave any pre-existing token alone. |
| 0.2 | **Human:** reconnect in Desktop (host + HTTP path, Import, PAT auth), load one table |
| 0.3 | Clear every gate **before** deleting anything — a gated `fabric/` cannot be removed |
| 0.4 | Confirm baseline: no DENY ACEs, no gate markers, no stray Desktop, no `_probe-lab` |

### Phase 1 — fix known defects (no testing)

| # | Defect |
|---|---|
| F1 | `probe_dir()` cannot heal after deletion, which bricks the probe |
| F2 | "no catalog found" misreported as a connection failure rather than a config error |
| F3 | Hook control-surface regex fires on read-only mentions (blocked a read-only `verify`) |
| F4 | A gated `fabric/` cannot be deleted — document `clear`-before-teardown |

Gate: `ruff` clean, `pylint` 10.00, existing gate tests pass.

### Phase 2 — component tests

Stop at the first failure, fix it, re-run that test only.

| ID | Scenario | PASS |
|---|---|---|
| T1 | Happy path | `PROBE: DATA_OK`, exit 0, well under the timeout |
| T2 | Gate clears → build proceeds | a deliverable `.tmdl` write succeeds |
| T3 | Bad host | `UNREACHABLE` naming host/path — **not** `NO_CREDENTIAL` |
| T4 | Extract-only source | probe skipped, exit 0, **no gate armed** |
| T5 | Self-maintenance (I4) | delete sandbox then probe; `verify` while gated; edit the hook file |

### Phase 3 — agent end-to-end

| ID | Scenario | PASS |
|---|---|---|
| T6 | **Credentialed migration** | probe ran, `DATA_OK`, gate cleared, model + report built |
| T7 | Uncredentialed migration | probe ran **and failed**, zero artifacts, agent stopped and asked |

T6 is the test that was never run, and the one the deadlock hid.

T7 needs a source Desktop has never authenticated to. **Do not revoke a working PAT to manufacture
it** (I3) — if no such source exists, T7 stands on previously measured evidence and must be labelled
as such rather than presented as a fresh run.

Repeat T6/T7 across several models only *after* a single-model pass.

### Phase 4 — merge

Full suite + lint + cap gate → update `docs/credential-gate.md` → squash-merge → verify remote head →
clean up fixtures, ACLs, Desktop, and only then revoke the test PAT.

## Abort conditions

- a test needs a second unplanned fix → design problem, re-plan rather than patch
- a fix breaks a previously passing test → I6 violated
- more than two consecutive failures on the same test
- anything requiring a credential or manual step an agent cannot perform

## Deliberately out of scope

**Cached-but-revoked credential.** It costs a manual re-sign-in per run, and the *dangerous* form of
that failure (the silent hang) is already covered. A rejected credential is a fast, loud error — the
benign case — and testing it risks destroying the fixture again.
