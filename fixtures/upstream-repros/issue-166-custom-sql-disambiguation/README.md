# Issue #166 / #164 — custom-SQL disambiguated report binding

Upstream:

- https://github.com/Yarbrdab000/tableau-fabric-skills/issues/166
- related/consolidated: https://github.com/Yarbrdab000/tableau-fabric-skills/issues/164

This minimal `.twb` encodes two custom-SQL relations in one datasource:

- `Custom SQL Query`
- `Custom SQL Query (Upgrade Aircraft Installs)`

Both expose overlapping field captions (`TAIL`, `TECHNOLOGY`). The worksheet deliberately binds the disambiguated `TAIL (Custom SQL Query (Upgrade Aircraft Installs))` and `NEW_TECHNOLOGY` fields. The repro question is whether PBIR binds those fields to the disambiguated model table or falls back to the base `Custom SQL Query` table.

Measured on canonical engine 2.260.0: **does not reproduce the wrong-binding symptom with this synthetic shape**. The model emits both tables, including `Custom SQL Query (Upgrade Aircraft Installs)` with `TAIL`, `NEW_TECHNOLOGY`, and `OLD_TECHNOLOGY`, but the report layer fails closed: `report.json` records `could not resolve field 'none:TAIL (Custom SQL Query (Upgrade Aircraft Installs)):nk' (skipped)` and `could not resolve field 'none:NEW_TECHNOLOGY:nk' (skipped)`. No PBIR visual was emitted with a wrong `SourceRef.Entity`.

This is still a useful negative fixture: it bounds #166/#164 to a Tableau XML shape narrower than merely "two custom-SQL relations with overlapping column names".
