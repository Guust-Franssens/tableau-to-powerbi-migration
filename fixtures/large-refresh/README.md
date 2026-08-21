# Large refresh fixture

Issue #262 needs a local model whose refresh takes long enough to observe over time. This fixture is
credential-free and network-free: it reads a generated CSV from `fixtures/large-refresh/data/orders.csv`.
The CSV and the machine-local `SourceFolder` parameter binding are intentionally gitignored.

## Generate data and bind the model

```powershell
.\.venv\Scripts\python.exe scripts\make_refresh_fixture.py --rows 2_000_000 --seed 262 --print-hash --force
```

The command writes:

- `fixtures/large-refresh/data/orders.csv` — generated, deterministic payload; do not commit.
- `fixtures/large-refresh/fabric/LargeRefresh.SemanticModel/definition/expressions.tmdl` — local path
  binding for the Power Query `SourceFolder` parameter; do not commit.

Use smaller row counts for smoke tests, for example `--rows 25_000`. Use larger counts when probing
long-refresh behavior. Same `--rows` and `--seed` produce byte-identical CSV bytes.

## Open and refresh

Open `fixtures/large-refresh/fabric/LargeRefresh.pbip` in Power BI Desktop, then refresh the semantic
model. The model imports the same CSV through four deliberately different Power Query shapes:

1. `Orders` — typed base import.
2. `OrdersTypedAgain` — second full scan with extra row-level calculations.
3. `CustomerDateRollup` — full scan plus grouping by customer/date/status.
4. `CustomerStatusMerge` — two grouped scans joined back together.

The repeated scans and grouped merge are intentional: raw CSV volume alone may still refresh too fast,
so the model adds deterministic local M work without credentials or network calls.

## Measured calibration

Record your local timing here when you refresh; hardware and Desktop build matter. The default
`--rows 2_000_000` is the intended long-refresh probe size.

| Date | Desktop | Rows | CSV size | Refresh time | Notes |
|---|---|---:|---:|---:|---|
| 2026-08-20 | 2.157.828.0 | 2,000,000 | 469.9 MB | 124.3 s | Full `refresh_pbip_model.py --pid 39500 --canaries Orders OrdersTypedAgain CustomerDateRollup CustomerStatusMerge`; cache persisted at 198,523.6 KB. |
