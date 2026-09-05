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

## Retention and privacy

Everything under `_runs/` is gitignored by `.gitignore` (`/_*`), protecting customer workbooks, credentials, manifests, and reference captures from accidental commits.
