# Issue #168 — CASE dispatcher with one bad branch

Upstream: https://github.com/Yarbrdab000/tableau-fabric-skills/issues/168

This minimal `.twb` encodes the reported shape: a parameter-driven `CASE` dispatcher where several branches return valid aggregate measures and one branch references an unresolved field. The purpose is to observe whether the engine preserves the valid branches or stubs the entire dispatcher.

Measured on canonical engine 2.260.0: **reproduces**. The generated semantic model contains `measure 'Selected KPI' = BLANK()`, while `report.json` records exactly one `model_translation_handoff.requests[]` entry with `fallback_reason: "unresolved/ambiguous field [MISSING_METRIC]"`. The three valid branches are not preserved in the emitted measure.
