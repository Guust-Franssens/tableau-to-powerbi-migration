# Shared-datasource fixture (issue #317)

Minimal hand-written PBIP units covering **all four states a report unit's semantic model can be in**.
For a shared/published datasource, `AGENTS.md` splits the deliverable: the model lands once under
`datasources/<ds>/fabric/`, each consuming report under `workbooks/<wb>/fabric/`, and the report's
`definition.pbir` `byPath` hops four levels to reach it. `check_unit.py` used to build its model
inventory with a local glob, so eight model-layer gates called the model absent while
`check_field_bindings.py` resolved and PASSed it in the same run.

The negative state (`no-model`) is committed **beside** the positive ones on purpose: "this unit's
model lives elsewhere, by design" and "this unit has no model" must never look the same.

| Unit under test | State | `_model_location` | `check_unit.py --scope model` |
|---|---|---|---|
| `model-local/` | model ships beside the report (sibling) | `LOCAL` | model gates run for real (`data-model: PASS`); `not_checked_external=0` |
| `external-resolves/workbooks/sales-wb/` | `byPath` hops four levels and **resolves** | `EXTERNAL` | eight model gates report `NOT_CHECKED - model is EXTERNAL`, `field-bindings: PASS`, `not_checked_external=9`, brownfield `EVIDENCED (external)`, exit 2 |
| `external-broken/workbooks/sales-wb/` | `byPath` is declared but **dangles** | `BROKEN` | `model-reference: FINDINGS` (a genuine defect, not a silent NOT_CHECKED), exit 1 |
| `no-model/` | report bound to no dataset, no sibling | `NONE` | `ai-descriptions: NOT_CHECKED - no semantic model found`, no `model-reference` row, `not_checked_external=0` |

The models/reports are hand-written minimal TMDL/PBIR (one `Sales` table, one page, one `tableEx`
visual referencing `Sales[Order Date]` and `Sales[Total Revenue]`), so `field-bindings` genuinely
resolves and PASSes against the external model.

`tests/golden/shared-datasource/external-resolves.model.stdout` locks the actionable EXTERNAL wording,
the per-gate rows, the summary buckets, and the brownfield line. It is normalized (machine paths,
interpreter, and OS separators stripped) so it is portable across Windows and the Linux CI runner.

Limitations: like `check-gates-dirty`, this is a harness for stable behaviour, not a claim the wording
is optimal. Snapshot/exit-code tests make drift visible; review still decides whether output is useful.
