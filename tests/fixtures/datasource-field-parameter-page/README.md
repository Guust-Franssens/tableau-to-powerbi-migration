# `datasource-field-parameter-page` — a standalone datasource that is NOT a thin shell

**What it exists to refute:** *"a `.tds`/`.tdsx` unit emits only a one-page report shell, so the
PBIP path-ceiling projection can give datasource units a visual-free envelope."* That belief is
false, and a projection built on it would be **fail-open** — it would pass a tree Power BI Desktop
cannot open.

## The measurement

Run 409's pre-conversion refusal (`run_estate.project_estate_path_ceiling`, file 275 / dir 263 at a
92-character output root) was driven **entirely** by the longest unit name,
`Meridian_Calc_Gauntlet__Live_Snowflake_` — a standalone datasource. Rebasing the 437 paths that
canonical engine 2.368.0 actually emitted from the same three inputs onto that same root gives
**max file 237 / max dir 225, 0 offenders**: the tree would have fitted. The projector over-projected
that one unit by **+48 file / +54 dir**, while both workbook units were projected accurate to +4.

The tempting conclusion — *give a datasource unit a thin, visual-free envelope* — is wrong.
`migrate_estate.py` passes `swap_specs` into `write_local_pbip` on the **datasource** branch, so
`assemble_model.build_swap_report_parts` → `twb_to_pbir.build_field_parameter_page` emits a real
self-service page whenever the datasource carries field-swap calcs.

Measured on canonical engine **2.368.0** from this fixture:

| | emitted |
|---|---|
| page directory | `pageSelfService` (15 UTF-16 units, a constant in `build_field_parameter_page`) |
| visuals | `fptable-pageSelf51410740`, `fpslicer-pageSel24d4262c`, `fpslicer-pageSel3815a7d3` — **24 units each** |
| deepest report tail | `definition/pages/pageSelfService/visuals/<24>/visual.json` = **77** |
| a visual-free tail | `definition/pages/page1/page.json` = **32** |
| **understatement** | **45 characters** |

A datasource unit is therefore only **13** characters cheaper than a workbook unit
(page 15 vs 24, same 24-unit visual cap from `twb_to_pbir._sanitize`), not 45+.

⚠️ And a *sound* kind-aware envelope still does not rescue run 409: at that root, the datasource
projects **262 / 250** with `pageSelfService` + a 24-unit visual — still over 259 / 247. The real
tree fitted only because that particular datasource had **no** field-swap calcs, which is a
**data**-dependent property, not a **kind**-dependent one.

## Provenance

Derived from the committed `tests/fixtures/CustomSQL_Parameter_And_Doubled_Operators.tds` (which
alone emits a thin `page1` shell), with two calculated columns **and the two parameters they
control** appended:

```
Metric Swap    (measure)   CASE [Parameters].[Metric]   WHEN "Sales" THEN [Sales] WHEN "Profit" THEN [Profit] END
Grouping Swap  (dimension) CASE [Parameters].[Grouping] WHEN "Order" THEN [Order ID] WHEN "Customer" THEN [Customer Name] END
```

⚠️ **The parameters are declared, not merely referenced.** `parameters.detect_field_swap` matches
only the *formula*, so a fixture can reach the self-service branch while its controller exists
nowhere — which would make this a test of the engine's regex tolerance rather than of a real Tableau
shape. Round-1 review caught exactly that. `<datasource-dependencies datasource='Parameters'>` now
declares both controllers as `param-domain-type='list'` columns whose `<members>` are the swap's own
branch labels and whose `value` default is one of them:

| parameter | domain | default | members |
|---|---|---|---|
| `Metric` | `list` | `Sales` | `Sales`, `Profit` |
| `Grouping` | `list` | `Order` | `Order`, `Customer` |

Verified with the engine's own parser (canonical 2.368.0):

```
Min Profit Threshold | any  | default 100.    | members []
Metric               | list | default "Sales" | members ['Sales', 'Profit']    | aliases {'"Sales"': 'Sales', '"Profit"': 'Profit'}
Grouping             | list | default "Order" | members ['Order', 'Customer']  | aliases {'"Order"': 'Order', '"Customer"': 'Customer'}
```

Both shapes are what `detect_field_swap` accepts: a `[Parameters].[X]`-driven `CASE` whose every
branch is a **bare** field reference, with at least two branches. One measure-role and one
dimension-role swap, so the emitted page carries the field-parameter table **and** one `listSlicer`
per parameter — the slicer count is what scales with the source, the identifier length is not.

## The three-shape matrix this fixture completes

`tests/test_datasource_path_envelope.py` runs the canonical engine **once** over three committed
sources and pins each emitted shape, so no conclusion rests on a single artifact:

| source | unit | emitted page | visuals | deepest report tail |
|---|---|---|---:|---:|
| `tests/fixtures/CustomSQL_Parameter_And_Doubled_Operators.tds` | ordinary datasource | `page1` | 0 | 32 |
| this fixture | swap datasource | `pageSelfService` | 3 @ 24 units | 77 |
| `fixtures/upstream-repros/issue-424-automatic-mark-discrete-date/issue-424-d-explicit-bar-mark.twb` | workbook | `page-Detail8fea6fd9` | 1 @ 24 units | 81 |

The workbook arm is **borrowed**, not newly authored: long-name path arithmetic is already owned by
`tests/test_run_estate.py`, and duplicating it here would be redundant proof.

Pinned by `tests/test_datasource_path_envelope.py`.
