# Upstream engine repro fixtures

Small, text-only Tableau workbook fixtures for upstream `Yarbrdab000/tableau-fabric-skills` issues. These are intentionally minimal inputs for the deterministic engine; generated `migration-spec.json` and engine output are not committed.

## Pinning test semantics

`tests/test_upstream_repro_pins.py` runs these fixtures through the canonical installed engine and pins the behaviour observed at the engine version named in that test. Some pins intentionally expect a known upstream defect: while the engine is broken, the test passes; when upstream fixes the defect, the test fails so we notice the change, verify the new output, and update both the expectation and pinned engine version. Do not "repair" a failing pin without checking whether it means an upstream fix landed.

Tested with canonical engine 2.260.0 via:

```powershell
$py = ".\.venv\Scripts\python.exe"
$json = & $py scripts\engine_source.py --json | ConvertFrom-Json
& $py ($json.scripts + "\migrate_estate.py") -i fixtures\upstream-repros -o _repro_runs\engine-b
```

## Fixtures

| Issue | Fixture | 2.260.0 result |
|---:|---|---|
| #166 / #164 | `issue-166-custom-sql-disambiguation` | Meaningful negative: model tables disambiguate, but report fails closed/skips fields; no wrong PBIR binding emitted. |
| #168 | `issue-168-case-one-bad-branch` | Reproduces: one unresolved CASE branch stubs the whole dispatcher measure to `BLANK()`. |
| #171 | `issue-171-measure-names-parameter` | Partial: parameter calc translates, but virtual Measure Names remains unresolved/deferred and no field parameter is emitted. |
