# issue-424 — `<mark class='Automatic'/>` over a **discrete** date part becomes a stacked `columnChart`

Three workbooks that are byte-identical except for **one** thing each. They isolate what actually
decides `columnChart` vs `lineChart` in the deterministic engine, and show that the engine's gate is
narrower than Tableau's own rule.

| fixture | the one difference | engine 2.339.0 emits | Tableau renders |
|---|---|---|---|
| `issue-424-a-discrete-date-part.twb` | mark `Automatic`, **discrete** date part (`derivation='Year'`, pill `[yr:DATES:ok]`) | **`columnChart`** ❌ | **line** |
| `issue-424-b-continuous-date-trunc.twb` | mark `Automatic`, **continuous** truncation (`derivation='Month-Trunc'`, pill `[tmn:DATES:qk]`) | `lineChart` ✅ | line |
| `issue-424-c-explicit-line-mark.twb` | explicit `<mark class='Line'/>`, same discrete date as A | `lineChart` ✅ | line |

All three carry a colour dimension (`AIRLINE_CODE`) on the marks card, so the emitted chart has a
**Series** well. `columnChart` is Power BI's *stacked* column, so variant A does not merely pick the
wrong glyph — it **sums the series**. With percentage availability ratios on `Y` (the real-world
shape this came from) five airlines at 95/92/88/97/90 % stack into one ~462 % bar.

## Why this is an engine defect and not a judgement call

Tableau's own documentation, *[Change the Type of Marks in the
View](https://help.tableau.com/current/pro/desktop/en-us/viewparts_marks_marktypes.htm)* (fetched
2026-09-01), states the rule twice and neither statement mentions continuity:

> **Line** — "The Line mark type is selected when there is **a date field** and a measure as the
> inner fields on the Rows and Columns shelves."
>
> **Bar** — "…you place a dimension and a measure as the inner fields on the Rows and Columns
> shelves. **If the dimension is a date dimension, the Line mark is used instead.**"

The engine gates on continuity instead of date-ness. `twb_to_pbir.py:2366` `_has_continuous_date`
returns true only for a `*-Trunc` derivation, and its docstring states the belief the code encodes —
*"a discrete date PART (Year / Month, derivation in `_DATE_PARTS`) is NOT continuous. Under an
Automatic mark Tableau renders a continuous date + a measure as a LINE (a discrete date -> bars)."*
The parenthetical is what Tableau's docs contradict. The predicate is consumed at
`twb_to_pbir.py:2505-2508`; every derivation in `_DATE_PARTS` (`twb_to_pbir.py:394`) falls through to
`VT_COLUMN`.

**The engine emits no warning.** Measured on variant A: `viz_fidelity` = `tier: rebuilt,
status: rebuilt`, empty `reason`, and a `remediation_worklist` with **0** items — stacking is
structurally valid, so nothing downstream flags it either.

## What this fixture also disproves

Issue #424 offered, as its strongest evidence, that the *same visual id*
(`v-page-Detail8fe63b4fcec`) appeared in two different workbooks with different `visualType`s, and
read that as "an inconsistency inside the engine's own output". **It is not.** A PBIR visual name is
`_sanitize(f"v-{page_name}-{i}-{ws['name']}")` (`twb_to_pbir.py:14280`), and `_sanitize`
(`twb_to_pbir.py:748`) is a 16-char prefix plus an 8-char md5 of the full string — so the id is a
deterministic function of **(dashboard name, zone index, worksheet name) only**. It encodes nothing
about mark type, shelf encodings or data.

All three fixtures here share a dashboard named `Detail`, one zone, and one worksheet name, and
therefore emit the **identical** visual name `v-page-Detail8fe20137fae` with **two different**
`visualType`s — the reported symptom, reproduced from an input difference alone.

## Reproduce

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py scripts\run_estate.py --input fixtures\upstream-repros\issue-424-automatic-mark-discrete-date `
                            --output _runs\424-repro\bundle
Get-ChildItem _runs\424-repro\bundle\pbip -Recurse -Filter visual.json |
  ForEach-Object { $j = Get-Content $_.FullName -Raw | ConvertFrom-Json
                   "{0,-40} {1,-26} {2}" -f $_.FullName.Split('\')[-8], $j.name, $j.visual.visualType }
```

Observed (engine 2.339.0):

```
issue-424-a-discrete-date-part           v-page-Detail8fe20137fae   columnChart
issue-424-b-continuous-date-trunc        v-page-Detail8fe20137fae   lineChart
issue-424-c-explicit-line-mark           v-page-Detail8fe20137fae   lineChart
```

Pinned by `tests/test_issue_424_chart_type_pin.py`, which is a **defect-direction** pin: while
upstream is broken it passes; when upstream fixes it the test fails so the change is noticed.

## Fixing it downstream is not a one-line `visualType` swap

A `columnChart` and a `lineChart` do not share their `dataPoint` vocabulary, and a leftover property
is a **hard** `validate` error rather than a silent render bug. Measured against
`powerbi-report-author` on this fixture's own output:

| mutation | `validate` exit |
|---|---|
| `lineChart` + `dataPoint.fillTransparency` | **1** — `PBIR_FORMATTING_PROP_UNKNOWN`, *Unknown property "fillTransparency" in formatting object "dataPoint" for lineChart* |
| `columnChart` + `dataPoint.transparency` | **1** — same code |
| `lineChart` + `labels.labelOverflow` | **1** — same code |
| `lineChart` + `dataPoint.transparency` | 0 |
| `columnChart` + `dataPoint.fillTransparency` | 0 |

See `powerbi-report-gotchas` §3 (type-flip property sets) and §8 (the `Automatic` mark rule).
