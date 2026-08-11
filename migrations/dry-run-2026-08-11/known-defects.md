# Pre-registered known defects

**Registered 2026-08-11, BEFORE the run starts.** That timing is the point: a defect classified
after it appears is unfalsifiable — anything can be called "expected" in hindsight. Anything here is
`known-engine` and **not a UX finding**. Anything not here that goes wrong is a genuine finding.

Engine under test: `Yarbrdab000/tableau-fabric-skills` **`main` @ `c6f8831`, skill version 2.113.0**
(the installed plugin now matches, updated 2026-08-11 from 2.86.0).

## The big one: 16 fixes are not on `main`

Verified 2026-08-11 with `git merge-base --is-ancestor` on every cited commit — all `False`.

| branch | skill version | tip |
|---|---|---|
| `main` | **2.113.0** | `c6f8831` |
| `yarbrdab000/integrate-all-lanes` | **2.126.0** | `f8e8efd` |

13 commits ahead, 13 behind, no open PR. Raised as `Yarbrdab000/tableau-fabric-skills#113`.

**We run `main` deliberately** — it is what a user gets. But it means the run will re-encounter
defects that are already fixed on a branch. Those are `known-engine`, and re-reporting them upstream
would be noise.

## Defects expected to appear

| # | Defect | Expected symptom in this run |
|---|---|---|
| **#112 / #106** | Deprecated Bing map visuals; no `azureMap` on `main` (`git grep -c azureMap` → 0) | Any map worksheet emits `filledMap`/`shapeMap`. Desktop shows a *"Bing maps are going away"* modal the bridge does not surface. |
| **#112** | `Heatmap` mark class emits **no page at all** | A density-map worksheet silently vanishes from the report. |
| **#112** | Pie-on-map degrades to a plain `pieChart` | Geography dropped, not degraded — looks finished. |
| **#110** | Generated M omits the culture argument | **Wrong numbers, silently.** On this `en-BE` machine a comma-decimal source inflates by `10^decimals` (measured 493× / 6,285× in one table). Model builds, refreshes, passes every structural gate. |
| **#108** | `Excel.Workbook` nav by `[Item=…, Kind="Sheet"]` fails for legacy `.xls` | *"The key didn't match any rows"* — **at refresh only**, long after gates pass. |
| **#99** | `estate_survey` turns a connection failure into `conns[luid] = []` | ⚠️ **Live risk in phase 1.** A PAT session loss mid-survey yields a short, plausible dependency list with **no error**. See the guard below. |
| **#103** | `viz_fidelity` reports `rebuilt` for a visual bound to the **wrong table** | A false success claim in the handover. |
| **#111** | Dual-axis map with 3 mark layers flattened into one visual | Layers silently dropped, no warning. |
| **#105** | `sqlproxy` workbook skipped even when its published datasource is available | A workbook disappears from the run. |
| **#109** | Joined flat-file datasource fails the definition of done | DoD failure on a multi-table extract. |
| **#89** | *(fixed on `main`)* background-image z-order | Should **not** recur. If it does, that is a regression and a real finding. |

## Ours, not the engine's

| Source | Defect | Note |
|---|---|---|
| our tier | Multi-field Location well collapses every mark to one | Our Bing→azureMap conversion introduced this, then fixed it. Do not re-file upstream. |
| our tier | `Series`/legend well vetoes data-bound `referenceLayer.polygonFillColor` | Render-verified. Choropleth + category legend are mutually exclusive on one `azureMap`. |
| our tier | Measure-based visual filter silently drops marks | Verified: 5–6 marks drawn against a target of 10. |
| repo | **No `.env.example`** | **Finding #1, already banked.** Required variable names (`TABLEAU_SERVER_URL`, `TABLEAU_SITE`, `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET`) exist only inside `assess_estate.py`. |
| repo | `check_datamodel` does not check TMDL **indentation** | PR #79 pending. TMDL requires tabs; a space-indented file fails to open in Desktop and the gate reports clean. |

## Site-specific conditions

| Condition | Consequence |
|---|---|
| **0 lifetime view events** across 38 workbooks | The coverage curve is explicitly **unproven**. The agent must scope by hand and must **not** tier on usage. If it presents the curve as evidence, that is a finding. |
| **1 Tableau Prep flow** | Its own dependency chain; lands before the extracts it produces. |
| **IAM hard cases** | `data_export_split_from_read` (86), `local_groups_without_entra` (7). Exported, never mapped — mapping needs a workspace topology decision, which is human (issue #57). |
| Mixed Snowflake/Databricks auth | Some sources authenticated in this machine's Desktop credential store, some not. Both paths expected in one run. |

## Guard against the #99 trap

`estate_survey` cannot distinguish "no dependencies" from "the session died". Before trusting phase 1
output:

1. Check the dependency count is plausible against **38** workbooks.
2. The prior assessment recorded `dependencies: 0` with `survey_supplied: false` — so **any** non-zero
   result is new information, and a zero result should be treated as suspect rather than as fact.
3. Tableau Cloud PATs idle out. If the survey takes long enough for that, re-run rather than plan
   against a truncated result.

**A short, clean, plausible answer is the failure mode here** — not an error.
