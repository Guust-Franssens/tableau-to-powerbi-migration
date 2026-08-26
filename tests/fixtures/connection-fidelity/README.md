# connection-fidelity fixtures

A committed, inspectable copy of the a live customer estate defect (issue #328). The Tableau source
`ds.snowflake_scorecard` is a **Snowflake** custom-SQL data source stamped
`connection.powerbi_target = live_source` — the packaged `.hyper` is only Tableau's cache, so the
migrated model must connect to Snowflake.

Instead, `FLIGHTS.tmdl` emits a `Csv.Document(File.Contents(...))` partition: the live connection was
silently materialised to a flat file. `check_empty_model` classifies it green (rows land / M is
well-formed) — only `check_connection_fidelity.py` catches the downgrade, by comparing the emitted M
against what the source actually was.

The one `limitations_encountered` entry is a `stage: parse` note (the parser observing an extract), so
it does **not** excuse the downgrade — only a build-stage decision would. Expected verdict:
`DOWNGRADED`, exit 1.

The remaining truth-table rows (live-connected PASS, legitimately-CSV PASS, unknown NOT_CHECKED, the
mixed discrimination case, and the declared escape hatch) are built in `tests/test_check_connection_fidelity.py`.


## The four committed states

Each is a real spec + real TMDL you can run by hand:

```
python scripts/check_connection_fidelity.py tests/fixtures/connection-fidelity/<state>
```

| state | what it encodes | exit |
|---|---|---|
| `live-preserved` | Snowflake source, model emits `Value.NativeQuery` against it — the connection survived | **0** |
| `mixed-live-and-flat-file` | a preserved live source **beside** a legitimately-CSV source | **0** |
| `declared-downgrade` | the same downgrade as below, but RECORDED in `limitations_encountered` at `stage: semantic_build` | **0** |
| `silent-downgrade` | live Snowflake shipped as `Csv.Document(...)`, nobody recorded it | **1** |

### Why `mixed-...` exists, and why it contains a live source

It is the discrimination the whole gate turns on. In the estate that prompted #328, **3 CSV-backed
tables were legitimately CSV in the Tableau source** while 3 others were live Snowflake silently
materialised to CSV. A gate that flagged CSV would fire on the correct half, get muted, and then miss
the real one.

It deliberately pairs both in **one** unit. A flat-file-only fixture would prove nothing: with no live
source there is nothing to measure, and the gate honestly returns `SKIPPED` (exit 3) — an accurate
verdict that says nothing about whether flat files are discriminated from downgrades.

### Why `declared-` vs `silent-` is the whole escape hatch

The gate asks **"was this downgrade recorded?"**, never *"was it reasonable?"* — the second needs
judgement and would make it a review rather than a gate. ⚠️ The excusing stage is **`semantic_build`**;
a `parse`-stage limitation does **not** excuse, because noticing an extract at parse time is not a
decision to ship one. The two specs differ in exactly that field.
