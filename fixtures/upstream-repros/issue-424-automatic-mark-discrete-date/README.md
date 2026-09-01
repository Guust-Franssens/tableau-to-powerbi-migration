# issue-424 — `<mark class='Automatic'/>` over a **discrete** date becomes a stacked `columnChart`

Eight workbooks generated from one template, each differing from the others by **exactly one**
variable. They isolate what decides `columnChart` vs `lineChart` in the deterministic engine, show
the engine's gate is narrower than Tableau's own rule — and, just as importantly, bound how a fix may
be written.

Filed upstream as
[`Yarbrdab000/tableau-fabric-skills#184`](https://github.com/Yarbrdab000/tableau-fabric-skills/issues/184).

| fixture | the one variable | engine 2.339.0 | correct? |
|---|---|---|---|
| `a-discrete-date-part` | mark `Automatic`, discrete date PART (`Year`, `[yr:DATES:ok]`) | `columnChart` | ❌ **defect** |
| `b-continuous-date-trunc` | mark `Automatic`, continuous truncation (`Month-Trunc`) | `lineChart` | ✅ invariant |
| `c-explicit-line-mark` | explicit `<mark class='Line'/>`, discrete date | `lineChart` | ✅ invariant |
| `d-explicit-bar-mark` | explicit `<mark class='Bar'/>`, discrete date | `columnChart` | ✅ **invariant** |
| `e-discrete-exact-date` | mark `Automatic`, discrete EXACT date (`MDY`) | `columnChart` | ❌ **defect** |
| `f-datetime-date-part` | mark `Automatic`, discrete `Year` over a **`datetime`** column | `columnChart` | ❌ **defect** |
| `g-date-valued-calc` | mark `Automatic`, discrete `Year` over a date-valued **calculated field** | `columnChart` | ❌ **defect** |
| `h-non-date-dimension` | mark `Automatic`, a **non-date string** dimension | `columnChart` | ✅ **invariant** |

All eight carry a colour dimension (`AIRLINE_CODE`) on the marks card, so the emitted chart has a
**Series** well. `columnChart` is Power BI's *stacked* column, so the defective variants do not merely
pick the wrong glyph — they **sum the series**. With percentage availability ratios on `Y` (the
real-world shape this came from) five airlines at 95/92/88/97/90 % stack into one ~462 % bar.

⚠️ The "correct?" column's Tableau half is **doc-derived, not render-verified** — no Tableau install
was used. The engine half is measured.

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
`twb_to_pbir.py:2505-2508`, and it is `endswith("-Trunc")` — so **by construction** nothing in
`_DATE_PARTS` (`:394`) or `_DATE_EXACT_DERIVATIONS` (`:410`) can satisfy it.

`e-discrete-exact-date` is the sharpest case: `MDY` is not a numeric part at all, but the date value
at day grain, which the engine's own comment at `:404` calls *"an ORDINARY date column — the same
underlying date as a continuous exact-date pill, only rendered as discrete members"*.

**The engine emits no warning.** Measured on variant A: `viz_fidelity` = `tier: rebuilt,
status: rebuilt`, empty `reason`, and a `remediation_worklist` with **0** items — stacking is
structurally valid, so nothing downstream flags it either.

## The fixture set discriminates between candidate remedies

This is why there are eight and not three. A pin that only proves "the defect exists" cannot tell a
correct fix from a wrong one. Five candidate remedies were injected into the canonical 2.339.0
classifier at runtime (monkeypatching `_has_continuous_date` / `_visual_type`; **the engine was not
modified**) and run over all eight fixtures. `column` = `columnChart`, `LINE` = `lineChart`:

```
remedy                    A        B        C        D        E        F        G        H
as-shipped 2.339.0        column   LINE     LINE     column   column   column   column   column
CORRECT: date-aware       LINE     LINE     LINE     column   LINE     LINE     LINE     column
wrong: year-only          LINE     LINE     LINE     column   column   LINE     LINE     column
wrong: datatype=='date'   LINE     LINE     LINE     column   LINE     column   LINE     column
wrong: base-columns-only  LINE     LINE     LINE     column   LINE     LINE     column   column
wrong: mark-agnostic      LINE     LINE     LINE     LINE     LINE     LINE     LINE     column
wrong: any-discrete       LINE     LINE     LINE     column   LINE     LINE     LINE     LINE
```

Each wrong remedy is separated from the correct one by **exactly one** fixture, and none is
redundant:

| wrong remedy | what it gets wrong | caught only by |
|---|---|---|
| `year-only` | special-cases `Year`, ignores the rest of `_DATE_PARTS` / `_DATE_EXACT_DERIVATIONS` | **E** |
| `datatype=='date'` | requires the literal `date` datatype, forgets `datetime` | **F** |
| `base-columns-only` | resolves base columns but not a date-valued calculated field | **G** |
| `mark-agnostic` | rewrites *every* date-on-Columns chart to a line | **D** |
| `any-discrete` | keys on "discrete dimension" rather than "date" | **H** |

⚠️ **A three-fixture set of A/B/C would have passed all five.** That is the round-1 review finding
this set exists to answer: on A, B and C every candidate — right or wrong — produces identical
output.

**D and H are therefore permanent.** An explicit `Bar` mark must keep emitting `columnChart` (Tableau
says of the Bar mark that *"Marks are automatically stacked"*, so that rebuild is faithful and
flipping it would *introduce* a defect), and a non-date string dimension with a measure is a genuine
bar chart. `tests/test_issue_424_chart_type_pin.py` marks both as `PERMANENT_INVARIANTS`, which are
**not** to be retired with the rest of the pin when upstream fixes the predicate.

⚠️ **Still open:** a Tableau **date bin** or date **parameter** is not covered — no real serialization
of either was available, and a synthetic date-typed bin crossed to `lineChart` under a
datatype-keyed predicate, so that boundary is untested rather than settled.

## What this fixture set also disproves

Issue #424 offered, as its strongest evidence, that the *same visual id*
(`v-page-Detail8fe63b4fcec`) appeared in two different workbooks with different `visualType`s, and
read that as "an inconsistency inside the engine's own output". **It is not.** A PBIR visual name is
`_sanitize(f"v-{page_name}-{i}-{ws['name']}")` (`twb_to_pbir.py:14280`), and `_sanitize`
(`twb_to_pbir.py:748`) is a 16-char prefix plus an 8-char md5 of the full string — so the id is a
deterministic function of **(dashboard name, zone index, worksheet name) only**. It encodes nothing
about mark type, shelf encodings or data.

All eight fixtures share a dashboard named `Detail`, one zone, and one worksheet name, and therefore
emit the **identical** visual name `v-page-Detail8fe20137fae` with **two different** `visualType`s —
the reported symptom, reproduced from an input difference alone.

## Reproduce

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py scripts\run_estate.py --input fixtures\upstream-repros\issue-424-automatic-mark-discrete-date `
                            --output _runs\424-repro\bundle
Get-ChildItem _runs\424-repro\bundle\pbip -Recurse -Filter visual.json |
  ForEach-Object { $j = Get-Content $_.FullName -Raw | ConvertFrom-Json
                   "{0,-40} {1,-26} {2}" -f $_.FullName.Split('\')[-8], $j.name, $j.visual.visualType }
```

To re-derive the remedy matrix, import the engine's `twb_to_pbir` (resolve it with
`scripts/engine_source.py`), replace `_has_continuous_date` or `_visual_type` with a candidate, and
call `migrate_twb_to_pbir(<twb text>)` per fixture — its `parts` mapping holds each
`.../visual.json` payload. That experiment monkeypatches **private** engine internals, so it is kept
out of the committed tests deliberately; the table above is its recorded result.

Pinned by `tests/test_issue_424_chart_type_pin.py`, which separates `DEFECT_PINS` (flip to
`lineChart` when fixed) from `PERMANENT_INVARIANTS` (must never move).

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

⚠️ The reverse does **not** hold: `validate` is blind to a *missing* property. A `lineChart` carrying
`dataPoint.defaultColor` with no `lineStyles.strokeColor` exits **0**, as does either alone. See
`powerbi-report-gotchas` §3 (type-flip property sets) and §8 (the `Automatic` mark rule).
