# Engine-gap harvest — turning the `reports/` vs `pbip/` delta into evidence

`scripts/harvest_engine_gaps.py` answers issue #274's question —
***"what did a human or agent have to change after the deterministic engine ran?"*** — and, just as
importantly, refuses to answer it when the bundle cannot support the claim.

```
python scripts/harvest_engine_gaps.py <bundle> --json harvest.json --markdown harvest.md
```

| exit | status | meaning |
|---:|---|---|
| 0 | `complete` | every artifact paired, every difference attributed, baseline verified pristine |
| 1 | `untrustworthy` | the engine baseline itself drifted — the delta is not engine behaviour, it is a lie |
| 2 | — | usage error |
| 3 | `incomplete` | the numbers are real but do not cover everything: unpaired artifacts, unreadable paths, **an unattributed difference**, or no usable attribution baseline (including one that could not be read or decoded) |

---

## 1. The correction this tool exists to make

Issue #274 proposes a pair-wise `git diff --no-index --stat` per workbook and reads the result as the
engine's gap list. `AGENTS.md` says the same thing: keeping `reports/` pristine *"is what makes that
an exact answer to 'what did our tier change versus what the engine produced?'"*

**Measured, that reading is wrong — and it is wrong in the direction that generates noise.**

On the 52-asset estate run `_runs/estate-2.339.0-20260829/` (harvested 2026-08-30):

| observation | figure |
|---|---|
| report pairs | 44 |
| byte-identical | 7 (16 %) |
| differ | 37 (84 %) |
| differing files | 500 |
| **files written by the ENGINE itself** | **500 (100 %)** |
| **files a human or agent changed** | **0** |

Every differing byte in that bundle still hash-matches the engine's own record. All 2481 entries in
`input_manifest.json → generated_artifacts.files` (1556 `pbip/`, 862 `reports/`, 63
`semantic_models/`) verify: **2481 checked, 0 mismatched, 0 missing**. No `_build/` directory exists
anywhere under `pbip/`; no generated-edit declaration was written. Nobody edited that bundle.

So the raw delta does not answer *"what did the engine get wrong?"*. It answers *"what does the
engine change between its own reference emission and its own bound emission?"* — a by-design
difference. Only the **attributed** subset answers issue #274, and this tool reports the two
separately and never merges them.

> This is the same trap issue #274's own comment thread flagged from the other direction: its first
> real dataset (12 workbooks, 30,333 insertions) came from a bundle whose `--tamper` check returned
> `NO_BASELINE`, so it was *"usable signal, not cryptographically attested signal"*. The fix for both
> is the same — attribute before you count.

---

## 2. Axis 1 — provenance, DELEGATED to the tamper gate

**This module does not arbitrate provenance itself.** It calls
`check_migration_progress.adjudicate_generated_drift()` — the structured core of the `--tamper` gate
this repo already ships — and interprets the answer. That is a mechanism change, not a refactor: a
blind review of the first version found four independent defects in its own hand-rolled attribution,
and measured the existing gate answering all four correctly on the same bundles.

| probe (measured 2026-08-30) | harvest v1 | `tamper_check()` on the same bundle |
|---|---|---|
| `reports/WB.Report` replaced with a copy of `pbip/WB/WB.Report` | `complete`, **0** differing files | `DRIFT` — undeclared change to `reports/` |
| a working file created after the engine ran | `complete`, `unattributed` | `DRIFT` — undeclared `added` |
| a working file deleted after the engine ran | `complete`, `engine_internal` | `DRIFT` — undeclared `missing` |
| a working-**only** file deleted after the engine ran | `complete`, **0** differing files | `DRIFT` — undeclared `missing` |
| a declaration invalidated by a later edit | `complete`, credited to that script | `DRIFT` — classified **UNDECLARED** |

`run_estate.py` records a sha256 per bundle-relative path for **both** sides, and the gate compares
the *whole recorded set plus the whole current set* — not merely the paths a delta happens to touch.
That is what makes the first and fourth rows above detectable at all. What this module adds is the
reading the gate does not make: **which side** a drifting path sits on.

| baseline side | working side | verdict | means |
|---|---|---|---|
| matches record | matches record | `engine_internal` | the engine wrote both bytes — **not** a tier fix, **not** a defect |
| matches record | drifted (changed / added / missing) | `tier_edit` | changed after the engine ran — **this is the evidence** |
| **any drift under `reports/` or `semantic_models/`** | any | `baseline_tampered` | the delta for this artifact is a lie; the run refuses (exit 1) |
| unrecorded / no usable baseline | any | `unattributed` | honest ignorance, never folded into either bucket |

`baseline_tampered` is not hypothetical: this repo has already retracted an upstream defect report
because a fix pass had rewritten `reports/` and the diff was read as engine behaviour.

Two policy divergences from the tamper gate, both **stricter**, both deliberate:

* a `slice_only_backfill` baseline is **unavailable** here, not merely caveated. The gate asks *"did
  anything change since the baseline was recorded"*; this module asks *"who wrote this byte relative
  to the **engine** boundary"*, and a backfill has no engine boundary behind it to answer with.
* drift under `reports/` + `semantic_models/` is `untrustworthy` **even when declared**. Those trees
  are "NEVER edited, by anyone" (AGENTS.md); a declaration makes an edit visible, not legitimate.

When a bundle carries no usable baseline (`check_migration_progress.py --tamper` → `NO_BASELINE`),
**every** difference is `unattributed` and the run is `incomplete`. The delta is still printed,
because it is still real; what is withheld is the claim about who caused it. The same applies to an
`input_manifest.json` that cannot be read or decoded — reported as `incomplete` (**exit 3**), never
as the exit 1 that means *"a tampered baseline was positively detected"*.

**Coverage is reported as a number, not implied.** `attribution.coverage` carries
`paths_compared` / `paths_attributed`, and a single unattributed difference forces `incomplete`.
The first version checked only that an attribution object *existed*: a manifest whose
`generated_artifacts.files` was `{}` returned `complete` with 2 of 2 differences unattributed.

`tier_edit` records carry `declared_by`, populated **only** where the gate accepted the declaration
as proof — matching run id, baseline hash, operation and resulting hash. A declaration that merely
names the target is not evidence; a file edited again after being declared is `undeclared`.

Every record also carries `baseline_path` / `working_path` (bundle-relative), so a consumer can join
the harvest back to the gate's own output without reassembling paths from `unit` + `artifact`.

---

## 3. Axis 2 — shape, derived from the corpus

Shape classification lives in its sibling module `scripts/harvest_gap_shapes.py`; the two axes meet
only in the report. Every changed JSON file is diffed structurally and each difference mapped to a
shape by its JSON pointer; TMDL and other text files are diffed by line and mapped by line kind.

The buckets were chosen **by measuring first**. An earlier pass left three pointer families in the
residue (`/pageOrder`, `/activePageName`, `/resourcePackages`); they were inspected and promoted to
named shapes rather than left as noise. The `UNCLASSIFIED` bucket is retained and reported — it is
0 on this corpus, which is a measurement, not a guarantee.

### The refinement that stops it crying wolf — and the leaf-level correction

`reports/` is a **reference-only, unbound** emission. Measured: its `datasetReference.byPath` fails
to resolve in **45 of 45** baselines, because no `.SemanticModel` sits beside it. Its visuals
therefore name entities that exist nowhere — `Extract`, `HumanResources`,
`Orders.csv_110301F4E5AB49989DD4872054A4FFED`.

So a rename from a baseline entity to a working entity is usually *the binding being resolved*, not a
repair. ⚠️ **But that verdict is now reached per changed LEAF, never per file.** The first version
decided it once from the file's global before/after entity sets and then discarded **every**
`MODEL_OBJECT_NAMES` shape in that file. Blind-review probe: one visual carrying `Extract → Orders`
(invalid → valid), `Orders → Returns` (valid → valid) and a property `A → B` was reported as
`BINDING_RESOLUTION` alone — one benign rebind laundered the two findings sitting next to it.

`BINDING_RESOLUTION` is now claimed for a leaf only when the model is known and **that leaf's own**
before-value is absent from the bound model while its after-value is present. `Property` and
`nativeQueryRef` can never be demonstrated (only TABLE names are read from the bound model), so they
always stay `MODEL_OBJECT_NAMES` — the direction that over-reports rather than the one that excuses.

**Re-measured on the estate after the correction, and it is not a rounding difference.** Files
carrying at least one *unexplained* name change went from **27 → 270**, because a genuine rebind was
excusing real substitutions in the same file. The retained leaves, counted:

| retained `MODEL_OBJECT_NAMES` leaf | changes |
|---|---:|
| `queryRef` (e.g. `Orders.Order_Date` → `Date.Year`) | 574 |
| `nativeQueryRef` (e.g. `Order_Date` → `Year`) | 99 |
| `Property` (e.g. `Order_Date` → `Month`) | 78 |
| `Entity` between two tables both valid in the model (e.g. `Orders` → `Date`) | 9 |

That top row is the point: a projection moving to a **different table *and* column** is a model-shape
change, not a binding being resolved, and the whole-file rule was reporting 265 files as "by design"
partly on its strength. Excusing them would have been the easy and wrong move.

### Shape distribution, estate run 2.339.0 (500 differing files)

| shape | files | artifacts | share |
|---|---:|---:|---:|
| `BINDING_RESOLUTION` | 273 | 22 | 55 % |
| `MODEL_OBJECT_NAMES` | 270 | 19 | 54 % |
| `VISUAL_ADDED` | 79 | 13 | 16 % |
| `QUERY_SHAPE` | 38 | 13 | 8 % |
| `LAYOUT` | 29 | 3 | 6 % |
| `FILE_ADDED` | 27 | 3 | 5 % |
| `REBIND_TARGET` | 23 | 23 | 5 % |
| `FILTER` | 20 | 5 | 4 % |
| `PAGE_ADDED` | 16 | 8 | 3 % |
| `VISUAL_REMOVED` | 15 | 3 | 3 % |
| `PAGE_ORDER` | 8 | 8 | 2 % |
| `FORMATTING` | 8 | 3 | 2 % |
| `PAGE_REMOVED` | 5 | 5 | 1 % |
| `RESOURCES` | 3 | 3 | 1 % |
| `TMDL_ANNOTATION` / `TMDL_LINEAGE_TAG` / `TMDL_MEASURE` | 3 each | 3 | 1 % each |
| `UNCLASSIFIED` | **0** | 0 | — |

Shares sum past 100 % because one file can carry several shapes — which is precisely what the
leaf-level correction restored: `BINDING_RESOLUTION` and `MODEL_OBJECT_NAMES` now co-occur on the
files where both are true, instead of the first silently deleting the second.

`TMDL_MEASURE` is the whole model-layer signal, and it is a real one: three shared published-
datasource models gain a workbook-local measure in their working copy
(`measure 'Avg Order Value' = DIVIDE(SUM('FACT_ORDERS'[TOTAL_PRICE]), …)`). Classifying TMDL as
binary would have hidden it — the module treats `.tmdl` as text for exactly this reason.

### Cost of validating the baseline independently

Hashing the whole recorded inventory up front, rather than lazily hashing only the paths the delta
touches, is what makes a wholesale baseline rewrite detectable. It is not free. Measured on
`_runs/estate-2.339.0-20260829` (2548 files, 13.6 MB), three interleaved warm runs each:
**median 8.2 s → 11.1 s (+35 %)**. The spread on this machine is wide — one v1 run took 37.6 s and
one cold first pass took 126 s — so treat the median as the signal and the outliers as disk/AV noise,
not as a regression.


---

## 4. Coverage is a finding, not an inconvenience

Both layers are harvested and each reports its **own denominator**, because they differ enormously
and an estate-level average silently hides it:

| layer | artifacts | assessed | identical | differs | no baseline | no working copy |
|---|---:|---:|---:|---:|---:|---:|
| report | 52 | 44 (85 %) | 7 | 37 | 7 | 1 |
| model | 51 | 16 (31 %) | 13 | 3 | 35 | 0 |

**35 of 51 model working copies have no engine `semantic_models/` baseline at all.** That is issue
#179 at estate scale, and it means any single blended "engine agreement rate" would be silently
report-biased. An artifact with no baseline is `unpaired_no_baseline` and is *never* counted as
agreeing with the engine.

⚠️ **Pair the model layer by MODEL name, not unit name.** `pbip/HR Dashboard/` holds
`HumanResources.SemanticModel`; only 7 of the 51 units hold a model named after themselves. Pairing
by unit name reported 21 artifacts as baseline-less when 20 of them are not — a coverage figure wrong
by a factor of three. `tests/test_harvest_engine_gaps.py::test_model_layer_pairs_on_model_name_not_unit_name`
pins this.

---

## 5. Why the harvest does not use git

`AGENTS.md` mandates `git diff --no-index --stat <baseline> <working>` and warns that git also exits
1 on *"error: Could not access"*, so a caller must check for a stat line rather than the exit code.
At estate scale that warning is not enough — the failure is systematic, not rare:

| measurement | result |
|---|---|
| report pairs attempted with the mandated git form | 44 |
| pairs producing **no stat line** (exit 1) | **3** — worst path 261 / 285 / 287 |
| worst path among the 41 git could read | **259** |
| `worst_path > 259` as a predictor of the failures | **3/3**, no false positives |
| git vs this module's differ/identical verdict, on the 41 git could read | **41/41 agree** |

git cannot read them because `core.longpaths` is unset (its default) and the engine duplicates the
unit name in the PBIP path. Python 3.6+ declares `longPathAware`, so a content comparison in Python
reads all three: **UNASSESSABLE falls from 3 to 0 while every git-reachable verdict is reproduced
exactly.** The blind spot is still reported (`git_blind_spot`) because it is evidence about the
mandated command, and the artifacts it names are the same ones `check_path_ceiling.py` blocks on.

⚠️ `UNASSESSABLE` remains a real, retained state. A file the harvest cannot read is counted, listed,
withdrawn from **both** sides of the comparison — **together with every descendant**, because
`os.walk` reports only the *directory* it could not enter — so it can never masquerade as an addition
or a removal, and it forces a non-zero exit. Blind-review probe: exact-path withdrawal produced an
unassessable record for the blocked directory *and* a fabricated `delta.added` entry beneath it.
A traversal failure whose relative path cannot be computed at all cannot be scoped, so every
difference record for that pair is suppressed rather than reported as a subset that looks complete.

---

## 6. Why it is standalone and not a `run_estate.py` phase

Deliberate. A phase inside the run can only ever observe a bundle the tier has not touched yet —
exactly the degenerate case above, where `tier_edit` is 0 *by construction*. The measurement that
answers issue #274 has to be taken **after** the fix passes, possibly weeks later, against bundles
built by older engine versions, and it is useful mid-flight. Wiring a per-unit entry into
`scripts/check_unit.py` is a reasonable follow-up and is deliberately not done here.

---

## 7. What this does **not** tell you

- **Not effort.** File and line counts are not hours. A systematic reformat and a hard fidelity fix
  land identically. Issue #274's own first dataset makes this point about itself; it applies here too.
- **Not why.** Provenance says who, shape says what. The reason lives in the handover,
  `limitations_encountered`, and the declared-edit record (surfaced as `declared_by` when present).
- **Not a defect list.** `engine_internal` differences are by construction not defect evidence. The
  only engine-directed claim the tool makes is structural and separately labelled: the baseline's
  dataset reference does not resolve (45/45).
- **Not verified against a NATURALLY OCCURRING tier edit.** No bundle on the machine where this was
  built had one — all five `_runs/` bundles have zero `_build/` directories and zero receipt
  mismatches. The `tier_edit` and `baseline_tampered` paths *are* proven on **real engine artifacts**:
  a copy of the `HR Dashboard` unit (87 differing files, all `engine_internal` as copied) reported
  exactly `tier_edit 1` after one injected `position.x` change, `untrustworthy` / exit 1 after one
  injected change to its `reports/` baseline, and `unattributed 87` with `input_manifest.json`
  removed. What is untested is the field case where a fix pass produced the edit. Treat the first
  real `tier_edit > 0` run as the confirming measurement.
- **Not an attestation.** The hash baseline is written by the same pipeline it audits; it is an audit
  record, not a security boundary — the same caveat `check_migration_progress.py` carries.

---

## 8. Filing upstream from it

`--markdown` writes a summary shaped for an upstream issue: frequencies with denominators, the
coverage table, and the explicit non-claims. Engine defects go to
`Yarbrdab000/tableau-fabric-skills`, never to this repo's tracker — and the two numbering ranges do
not overlap in meaning, so cite the repo with every issue number.

Before quoting any figure from a harvest, check `status`: a `untrustworthy` run's numbers describe a
corrupted baseline, and an `incomplete` run's numbers have a denominator smaller than the estate.
