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
| #166 / #164 | [`issue-166-custom-sql-disambiguation`](issue-166-custom-sql-disambiguation/README.md) | Meaningful negative: model tables disambiguate, but report fails closed/skips fields; no wrong PBIR binding emitted. |
| #168 | [`issue-168-case-one-bad-branch`](issue-168-case-one-bad-branch/README.md) | Reproduces: one unresolved CASE branch stubs the whole dispatcher measure to `BLANK()`. |
| #171 | [`issue-171-measure-names-parameter`](issue-171-measure-names-parameter/README.md) | Partial: parameter calc translates, but virtual Measure Names remains unresolved/deferred and no field parameter is emitted. |
| #424 | [`issue-424-automatic-mark-discrete-date`](issue-424-automatic-mark-discrete-date/README.md) | Reproduces **at 2.339.0**: an `Automatic` mark over a *discrete* date part emits a stacked `columnChart` where Tableau draws a line — silently (`tier: rebuilt`, no warning, empty worklist). Two controls emit `lineChart`. |

⚠️ The #424 fixture is pinned by its own module, **`tests/test_issue_424_chart_type_pin.py`**, not by
`tests/test_upstream_repro_pins.py`. The shared harness asserts `PINNED_ENGINE_VERSION` before it
asserts anything else, so every test in it fails the moment the canonical plugin moves — which it has
(installed 2.339.0 vs the pinned 2.260.0). A pin added there would have shipped unrun. The #424
module reports the observed engine version in its failure text instead of asserting one, so it stays
checkable on whatever engine is installed. Refreshing the shared pin is separate work.
