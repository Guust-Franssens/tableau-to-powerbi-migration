# Dirty gate fixture

Minimal dirty PBIP bundle for exercising gate output against committed, reproducible findings. The artifacts are pruned from `_bundle-208/pbip/Admin_Insights_Starter`; they contain Tableau vendor-sample names only (`Admin_Insights_Starter`, `10ax.online.tableau.com`, `TSEvents`).

| Finding class | Source fragment | Gate | Expected verdict |
|---|---|---|---|
| `= BLANK()` stub measure with `annotation TableauFormula` | `Admin_Insights_Starter.SemanticModel/definition/tables/_Measures.tmdl`, `Group Sort` | `check_stub_measures.py --strict` | `STUBS`; `Group Sort` is ACTIONABLE. |
| `= BLANK()` stub measure without `annotation TableauFormula` | Minimal mutation of the same real `_Measures.tmdl` shape; `_bundle-208` has zero dead-end stubs by `check_stub_measures.py` | `check_stub_measures.py --strict` | `STUBS`; `Dead End Stub` is DEAD END. |
| `Server_sqlproxy` / `Database_sqlproxy` pair | `Admin_Insights_Starter.SemanticModel/definition/expressions.tmdl` | `check_sqlproxy_connections.py` | `SQLPROXY`. |
| Date-bearing table disconnected from `Date` | `sqlproxy` + `Date` + one active `sqlproxy (Groups)` relationship from `relationships.tmdl` | `check_relationship_health.py` | `MISSING_RELATIONSHIP` for `sqlproxy`. |
| PBIR field that does not resolve | `v-page-GroupDrilbd8fd9a2/visual.json`, `sqlproxy[group_name]` | `check_field_bindings.py` | `UNRESOLVED`; one missing field. |
| PBIR case-only near-miss | `v-page-GroupDril524dc45f/visual.json`, `_Measures[avg(0)]` vs model `_Measures[Avg(0)]` | `check_field_bindings.py` | `UNRESOLVED`; one case-only near-miss. |
| BLANK placeholder referenced by report | `Group Sort` handover + PBIR references | `check_blank_placeholders.py` | `REFERENCED`. `_bundle-208` has visual/sort references, not a report-filter reference; `visual-filter/visual.json` is a minimal filter-shaped reference added to cover the gate's filter context. |

Limitations: this fixture is a harness for stable stdout diffs, not a claim that current wording is good. Snapshot tests make output drift visible; review still decides whether the output is useful.
