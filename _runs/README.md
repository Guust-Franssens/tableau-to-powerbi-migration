# Canonical `_runs/` directory layout

Single source of truth for pipeline run outputs and scratch workspaces. Allocated and verified by
[`scripts/work_dirs.py`](../scripts/work_dirs.py).

## Run directory structure

Each pipeline run is allocated under a dedicated numbered directory `_runs/<NNN>-<slug>/`:

```
_runs/<NNN>-<slug>/
    run.json          <- authoritative run manifest (number, unit key, absolute path, timestamps)
    assessment/       <- assess_estate.py / run_engine_survey.py outputs (estate.db, report.md, estate_survey.json)
    assets/           <- harvest_estate_assets.py downloads (.twbx / .tdsx) and parse-sweep.md/.json
    bundle/           <- run_estate.py conversion output (pbip/, reports/, semantic_models/, handover/, data/)
    oracle/           <- capture_tableau_oracle.py visual and numeric reference captures
    packages/         <- package_unit.py per-unit self-contained handover packages (packages/<Unit>/)
    deliverables/     <- operator-facing outputs meant for the customer (created lazily on first use)
    scratch/          <- disposable, run-owned workspace; the only subdir a future --prune may delete
```

## Packages layout and self-containment

Phase 2 packages each unit into a self-contained directory:

```powershell
python scripts\package_unit.py --bundle _runs\<NNN>-<slug>\bundle `
    --out _runs\<NNN>-<slug>\packages `
    --json _runs\<NNN>-<slug>\packages\packaging.json
```

- **Flat layout:** `--out <run>/packages` writes per-unit packages directly to `<run>/packages/<Unit>/`.
- **Nested compatibility:** Nested batch layouts (e.g. `<run>/packages/<batch>/<Unit>/`) remain fully supported.
- **Self-contained isolation:** Completed packages carry `package-manifest.json`. The check gates (`check_reference_readiness.py` and `check_unit.py`) inspect `bundle_corpus.is_self_contained` and stop the ancestor evidence walk when the manifest is present, ensuring the package evaluates only its own scoped evidence and never borrows omitted renders or double-matches against run-root captures at `_runs/<NNN>-<slug>/oracle/`.
- **Fail closed:** An incomplete or failed package without `package-manifest.json` fails closed when ancestor evidence is present.


## Read-only run status

To inspect a known run without changing it, use the explicit absolute run path:

```powershell
python -B scripts\run_status.py --run C:\short\_runs\001-estate
python -B scripts\run_status.py --run C:\short\_runs\001-estate --json
```

`run_status.py` is a diagnostic inventory slice only. It reads `run.json` plus the selected run's
canonical subdirectories, reports retained `bundle/pbip` working copies, package manifests (including
package-only or ambiguous packages), recorded phases, and one non-destructive next action.
**`-B` is required for the read-only invocation**: it prevents Python import caches, including those
outside the selected run. The command does **not** select the latest run, discover sibling roots,
allocate, repair, write a cache/status file, launch Power BI, invoke network clients, rebuild,
promote, delete, or certify readiness. It rejects UNC/device spellings before filesystem access and
does not follow symlinks/junctions or read below a rejected boundary.

Only known statuses and validated timestamps appear as `last_observed`; arbitrary stored objects
and prose are not echoed, and current certification is always `NOT_CHECKED`. Human and JSON output
contain the same normalized records, occurrence multiplicity, integrity codes and next-action
details. Human values are escaped against line/control spoofing. Oracle observations describe
recorded presence or omissions, not completed fidelity; report-free datasources can legitimately
be `not_applicable`.

Exit `0` means the requested diagnostic inventory was assessable, not that the migration is ready.
Exit `1` means evidence is unassessable (including malformed phases, unreadable listings, a file
where a directory belongs, or a moved run). Exit `2` rejects nonlocal/unsafe/relative input spellings
or invalid CLI usage. Both `workbooks` and `datasources` must be present and list-typed in
`bundle/report.json`; `{}` does not establish an empty estate. Genuine empty lists remain diagnostic
only. Other safely readable package-only work stays visible even when that report is unestablished.
Known pending binding/reference work and changed package bytes are reported without certifying them.

Discovery is bounded and marker-based; ordinary grouping files are ignored, and a malformed marker
still stops descent. Handover association uses exact embedded identity, not its sanitized filename;
duplicates are ambiguous and overwritten history cannot be reconstructed. This is not an atomic
snapshot or proof that a local drive/mount is physically local. The inherited no-follow/read race
and the exact projection limits are documented in
[`scripts/README.md`](../scripts/README.md#read-only-run-status).

## Retention and privacy

Everything under `_runs/` is gitignored by `.gitignore` (`/_*`), protecting customer workbooks, credentials, manifests, and reference captures from accidental commits.

## Short-root escape (issue #479)

If this repo's own checkout path is deep enough that `_runs/<NNN>-<slug>/bundle/pbip/...` projects
over Power BI Desktop's path ceiling (`run_estate.py` exit `10`, `EXIT_PATH_CEILING`), allocate the
same canonical tree under a short EXTERNAL parent instead of a second, hand-maintained tree:

```powershell
python scripts\work_dirs.py <unit-name> --runs-parent C:\short\path --json
python scripts\work_dirs.py --verify --runs-parent C:\short\path
```

`--runs-parent` is a same-plumbing alias for `--repo-root` — it need not be a git checkout, and the
run it allocates has an identical `run.json`, subdirectory layout and `--verify` contract to a
repo-local run.
