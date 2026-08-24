# connection-fidelity fixture: `silent-downgrade`

A committed, inspectable copy of the SES Airborne Services defect (issue #328). The Tableau source
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
