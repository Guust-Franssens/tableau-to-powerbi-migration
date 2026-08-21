# Issue #171 — parameter-driven `:Measure Names` swap

Upstream: https://github.com/Yarbrdab000/tableau-fabric-skills/issues/171

This minimal `.twb` encodes a Tableau Measure Names/Measure Values view with an explicit workbook parameter `Select Measure`. It is intentionally small and text-only; it does not try to reverse-engineer the customer workbook.

The repro question is whether the engine emits a Power BI field parameter / equivalent selector, or instead leaves the virtual `:Measure Names` binding unresolved/deferred.

Measured on canonical engine 2.260.0: **partially reproduces / bounds the issue**. The parameter-driven calculated measure itself translates successfully to an `IF(EXACT([Select Measure Value], ...))` measure, and a parameter table/value measure is emitted. However, no Power BI field parameter table is emitted (`NAMEOF` / `ParameterMetadata` absent in TMDL), and the Measure Names/Values worksheet is not rebuilt: `report.json` records `Measure Values shelf could not be enumerated to member measures (no member list found; skipped)` plus a dashboard zone left empty.

This fixture does not prove the exact customer parameter-driven Measure Names idiom, but it does provide a small regression input for the current gap: virtual Measure Names remains a manual/deferred report binding rather than a generated field parameter.
