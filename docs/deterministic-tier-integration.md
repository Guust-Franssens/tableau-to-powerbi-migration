# Integrating the deterministic tier (`Yarbrdab000/tableau-fabric-skills`)

**Status:** DRAFT **v3** — revised after two adversarial critique rounds by Claude Opus 5 and GPT-5.6 Sol
**Branch:** `feat/deterministic-tier-integration`
**Evidence root:** `~/.copilot/session-state/5d5a3c63-…/files/integration-analysis/`
**Ground truth:** `C:\pbip\airline-fresh\` (a complete Tier-0 run, skill 2.40.0)

---

## 0a. What changed in v3 (critique round 2 — knowledge architecture)

Round 2 asked: *"can we drop persona instructions by adopting his tier?"* **Measured answer: no.**

| # | v2 claimed | measured reality | impact |
|---|---|---|---|
| 1 | *"the numeric oracle — his docs reference a reconciliation oracle **he does not ship**"* (§5 BUILD-7) | **He ships it.** `scripts/fidelity_oracle.py` = **227,813 bytes**, with `DEFAULT_VALUE_TOLERANCE = 0.005`, `_normalize_expected(expected)`, a `dax_value_tier` (ADOMD against a live model) and an `image_tier`; plus `resources/fidelity-oracle.md` (30 KB). What he does **not** ship is the `expected` dict — the Tableau-side ground truth | ❌ v2 authorised rebuilding a 227 KB comparator. **Our differentiator is the `expected` producer** (`capture_tableau_reference.py`, `migrations/*/reference/`), not the comparator |
| 2 | Tier 1b = all **105** `model_object_parameter` stubs need his `parameters.py`; ceiling **62.0 %** | **63 of 105 are misfiled.** Only **42** carry `fallback_reason: "parameter reference … (unmodeled)"`; 60 carry `"unresolved/ambiguous field [Calculation_2324420414664089602]"`. `translation_router.py:195-207` classifies *any* request touching a parameter as `model_object_parameter` | ✅ **Corrected `--approved-dax` ceiling: 84.8 %**, not 62 %. Tier 1b is ~42 objects, not 105 |
| 3 | persona prose shrinks by adopting Tier 0 | Tier 0 supersedes **~4,500** chars of build prose but **adds ~6,000** (`--approved-dax` loop, Tier 1b, I1-I6, repair DoD). **Net +600** across three personas; `pbi-semantic-builder` goes **30,674 → ~33,900** | ❌ integration alone makes the cap **worse** |

**The keystone.** A single unresolved object, `Calculation_2324420414664089602`, is referenced by **124 of
188** requests and is the stated `fallback_reason` for **60**. It is **not itself in the queue**.
Resolving it is the highest-leverage single action available and is now Phase 6's fixture.

**Why persona prose does not shrink** (the round-2 self-scepticism check): our personas never contained
Tableau→DAX translation prose — they *delegate* to `docs/tableau-dax-translation-guide.md`. So
`category_guidance` shaves **the guide, not the persona**. The drift surface is therefore exactly one
file, which is tractable.

**The two levers that actually work are orthogonal to his tier:**
1. **A 5th skill bundle `.github/skills/deterministic-tier/`** holding the whole Tier-0 contract
   (`--approved-dax` mechanics, `approved_dax.json` shape, I1-I6, the `parameters.py` sequence, the
   `report.json` field map, `guidance_sha256`). This is the `AGENTS.md:175` precedent and is the only
   thing that makes the +6,000 affordable.
2. **Split the 5,649-char shared-conventions block** — it is **22,596 chars across four personas,
   24.6 % of the total agent budget**, and *none* of it is affected by Tier 0. Hard stops (credential
   stop, layer ownership) stay verbatim inline; rationale (~3,500-4,000) moves behind a mandatory
   step-0 invoke. **This single change buys more headroom than the entire integration.**

**Split `pbi-semantic-builder` into build + repair personas** — not for cap reasons, but because the
modes have **contradictory defaults on ambiguity**: build says *author something*, repair says *change
nothing*. One prompt cannot hold both safely. `pbi-report-builder` stays unified (its repair surface
*is* authoring).

**New integration hazards found in round 2:**
- ⚠️ **PBIR `$schema` divergence** — all **232** of his `visual.json` declare `visualContainer/1.0.0`;
  our cookbook declares **2.11.0** (25) / 2.1.0 (2). Zero overlap. Pasting a cookbook fragment into his
  container is unflagged.
- ⚠️ **Visual-type vocabulary divergence** — ours `cardVisual`/`columnChart`/`barChart`, his
  `card`/`clusteredColumnChart`/`clusteredBarChart`. Run a `catalog list` deprecation check before
  standardising.
- ⚠️ **`limitations_encountered` has no Tier-0 analogue** (0 occurrences under `C:\pbip\airline-fresh`)
  and **AI-readiness is wholly uncontested upstream** (`CustomInstructions`, `qnaEnabled`, `synonym`,
  `ai-instructions` = **0 hits** across his `SKILL.md` + 21 `resources/*.md`). Both erode by neglect:
  AI-prep is the *last* build phase and repair mode has no natural slot for it, so **it will simply
  stop running** unless the orchestrator owns it.

**The 16 examples are the A/B corpus — freeze them, never rebuild.**
`examples/airline-alliance-activity/fabric/` is the **same workbook** as `C:\pbip\airline-fresh`:
**198 visuals / 108 measures (ours, from scratch)** vs ~126 visuals / 88 translated + 188 stubs (his).
That is a ready-made Phase 0.5 hold-out set with no new tooling. They are also live test infrastructure
(`tests/test_repo_layout.py`, `tests/test_skills.py:191-201`, `tests/test_check_m_syntax.py:149-155`)
and source workbooks are gitignored (`README.md:235-249`), so they are **not** reproducible from a
clean clone. Add `examples/*/deterministic/` alongside; never overwrite.
⚠️ The **198 → 126 visual gap** (mostly textbox 107→9, card 51→13) is an **unexplained finding**, not
noise. Determine whether he consolidated or dropped before accepting "Tier 0 becomes the builder".

**Cookbook verdict: (c) still the source of truth, for a disjoint surface.** His 126 visuals span 10
types; our 28 cookbook entries overlap on **card and donutChart only**. Everything the cookbook exists
for — `azureMap` (**0** hits in his 60,934-char `viz-rebuild.md`), `kpi`, `waterfallChart`, `funnel`,
`decompositionTreeVisual`, `shape`, plus the 6 non-type idioms — he does not emit at all. And the
warned set maps straight onto it: 14 `empty` = *"mark class 'Shape' … not supported"* → `shape`;
13 `degraded` = default continuous palette → `table-cond-format`.

---

## 0. What changed in v2, and why

Both critiques verified their claims against the real `report.json`; I re-verified every finding
before accepting it. **v1 had four load-bearing errors.**

| # | v1 claimed | measured reality | impact |
|---|---|---|---|
| 1 | Phase 7 reaches **>70 %** calc coverage via `--approved-dax` | `model_object_parameter` is **105/188 (56 %)**, and the seam accepts only `{name → DAX string}` — never a field-parameter *table*. Ceiling `(88+83)/276` = **62.0 %** | ❌ target arithmetically unreachable |
| 2 | post-passes (clamp, paths, Tier 3), then Tier-1 landing | `--approved-dax` **re-runs `migrate_estate.py` into the same bundle**, *exempt from the stale-output guard* (`second-compiler.md:333-345`); it regenerates all 126 `visual.json` and `_Measures.tmdl` | ❌ v1's order destroys its own work |
| 3 | portability = rewrite absolute M paths to relative | `connection_to_m.py:2294-2297`: *"Power BI's `File.Contents` rejects a relative path … OPENS but loads NO data"* | ❌ v1's fix causes the failure it prevents |
| 4 | `fields[]` is a dependency closure; guidance is per-request | **0 resolved `field` records** (451 `unresolved`, 171 `parameter`, 130 `calc`); only **6 unique** guidance strings; `has_suggestion` false for **all 188** | ⚠️ instruction-shaving materially weakened |

**Corrected structural facts** (v1 §3.3 would have raised `KeyError`):
`triage.shapes` → **`triage.summary.shapes`**; `triage.irreducible` is a **6-key dict** (the count 125
lives at `triage.summary.irreducible`); `workbooks[0]` has **25** keys, not 24; personas are
**already over** the 30,000-char cap (30,674 / 30,611 / 30,403), not "at ~99 %".

**New blocking finding:** `verify()` filters artifacts on `suffix in {.tmdl,.pbism,.pbir,.pbip}`
(`scripts/credential_gate.py` L500, L515). This run materialized **two 110 MB CSVs of source data**;
`.csv` is not in that set, so a live-source run can land 220 MB of customer rows on disk and the
credential gate still reports green. **Materialized data is a strictly larger harm than a `.tmdl`.**

---

## 1. Thesis (unchanged, and sharpened)

> **He builds the model. We finish it.**

His pipeline has an **agent-shaped socket with no agent** — `second-compiler.md`: *"you ARE the second
compiler … There is no script that authors the tail for you."*

| tier | what | executor |
|---|---|---|
| Tier 0 | deterministic → TMDL + PBIR + `report.json` | his `migrate_estate.py` (pinned) |
| Tier 1a | stubbed calcs → DAX via `--approved-dax` | ⭐ our repair persona — **ceiling 84.8 %** (v3 correction) |
| Tier 1b | genuine unmodeled Tableau parameters → model objects | ⚠️ his `parameters.py`, **in-process** — **~42 objects**, not 105 |
| Tier 3 | warned visuals | ⚠️ **no on-disk seam exists** (§5.4) |
| Tier ∞ | fidelity vs Tableau, credentials, portability, gate | ⭐ only us |

⚠️ **Tier 1b is a different integration mode than Tier 1a** — an in-process Python import, not a CLI
call — and it covers the majority of the work. v1 never contemplated this.

---

## 2. Invariants (new — constraints, not preferences)

Each exists because a critique proved v1 violated it.

- **I1 — Immutable source.** His output lands at `<migration>/deterministic/` and is never edited.
  Work happens on a copy at `<migration>/fabric/`.
- **I2 — Single writer.** One process per bundle. Tier-1 landing completes and is verified **before**
  any post-pass or Tier-3 edit. *(`--approved-dax` rebuilds the bundle; two agents = data race.)*
- **I3 — Tier-1 landing is a barrier.** Deterministic post-passes run **after** the final re-run.
- **I4 — Branch only on `pending_gates[]`.** `summary.*` is display-only: verified contradiction —
  `summary.workbooks_viz_warned = 0` while `summary.visuals_warned = 56`.
- **I5 — Silence is not correctness.** `translated: true` / `rebuilt` are the emitter's self-report.
- **I6 — Identity before landing.** No approval keyed by bare calc name (his loader matches
  case-insensitively across the estate). Ours carry
  `{unit_id, object_id, role, target_table, source_formula_sha256}` and fail closed on ambiguity.

---

## 3. What Tier 0 hands us — corrected inventory

Verified against `C:\pbip\airline-fresh\report.json` (594.6 KB).

### 3.1 `pending_gates[]` — the only routing signal (I4)

```json
{ "gate":"second_compiler", "count":188, "trigger":"summary.needs_review_total",
  "runbook":"resources/second-compiler.md", "skill_step":3, "offer":"…" }
{ "gate":"dashboard_audit",  "count":56,  "trigger":"summary.visuals_warned",
  "runbook":"resources/dashboard-audit.md", "skill_step":5, "offer":"…" }
```

### 3.2 `workbooks[0].model_translation_handoff.requests[]` — 188 entries

Fields: `name`, `role`, `target_table`, `category`, `category_guidance`, `fallback_reason`,
`formula`, `has_suggestion`, `fields[]`.

⚠️ **Corrected characterisation:**
- `fields[]` is **not** a resolved dependency closure. Measured kinds: `unresolved` **451**,
  `parameter` 171, `calc` 130, **`field` 0**. Sample: `{"caption":"date","kind":"unresolved"}` — a
  bare caption, no table/column binding. Only the 130 `calc` entries carry `references_formula`.
- **6 unique** `category_guidance` strings (one per category), 793-913 chars — category-level advice,
  not per-request instruction.
- `has_suggestion` is **false for all 188**: he ships **zero** candidate DAX.
- 10 requests carry no `fields[]` at all.

### 3.3 `triage` — corrected

```
triage.cascadable          : list[63]   -> ALL 63 are category `type_or_shape_mismatch`
triage.irreducible         : dict[6]    (shape -> list)      <- NOT the count
triage.summary.irreducible : 125
triage.summary.shapes      : date_shape 48 | param 41 | other 19 | conditional_countd 9
                             | simple_count 7 | lod 1        (sums to 125 = irreducible only)
```

⚠️ **v1's "order of magnitude reduction" claim is withdrawn.** Verified:
- **Zero** of the 105 `model_object_parameter` requests cascade — the multiplier applies only to the
  *smaller* category.
- Shapes classify only the 125 irreducible and cut **across** categories: `shapes.param = 41` vs
  `categories.model_object_parameter = 105`, so ~64 parameter-driven calcs sit under
  `date_shape`/`other`. "Fix `date_shape` once" is false.
- **K (keystone count) is unmeasured.** "63 cascade behind K keystones" is meaningless until K is
  derived from the dependency graph. Deriving K is now part of Phase 6.

Categories: `model_object_parameter` **105**, `type_or_shape_mismatch` 66, `unsupported_other` 11,
`unresolved_reference` 3, `missing_addressing_intent` 2, `dax_language_gap` 1.

### 3.4 `viz_fidelity[]` — 107 entries

`{worksheet, visual_type, status, tier, reason}`; tiers `rebuilt 50 / degraded 26 /
rebuilt_with_deferrals 17 / empty 14`.

⚠️ **Narrowed claim:** `viz_fidelity` carries no PBIR id (verified: `A320` appears in 0 of 126
`visual.json`). But `pbip_ref_drops[].visual` **does** carry ids that resolve to real folders — the
emitter knows them and simply omits them here. That strengthens §8.1 as an upstream ask.

---

## 4. Architecture

```
.twbx --+-- OUR parse_tableau.py --> migration-spec.json      (source intent)
        |
        +-- HIS migrate_estate.py (pinned) --> deterministic/  [I1 IMMUTABLE]
                                                    |
                                            copy ---+--> fabric/  [only writable copy]
                                                         |
   +-----------------------------------------------------+
   |                    |                                |
identifier_map    pending_gates[]                complement.v1.json
(caption<->id)         |                         (only what he lacks)
                       +-- second_compiler --> Tier 1a  --approved-dax    [I2/I3 barrier]
                       |                       Tier 1b  parameters.py in-process
                       +-- dashboard_audit --> Tier 3   (no on-disk seam, 5.4)
                                                         |
  deterministic post-passes (clamp, rebase) -- AFTER the final re-run -----+
                       |
                       v
        credential_gate: denied_dirs() vs audited_paths()
                       |
                       v
        pbi-migration-validator -- the numeric oracle he does not ship
```

| decision | choice | rationale |
|---|---|---|
| dependency | **DEPEND** on the engine; contract-normalise at *our* boundary | ⚠️ v1's "interoperate on contract" was too strong. `check_candidate_dax` and `--approved-dax` are functions *inside* the pinned engine, and `category_guidance` is **engine-generated prose fed to our LLM**. This is a behaviour dependency; we mitigate it (§6 `guidance_sha256`) rather than pretend it is a wire format. |
| invocation | `migrate_estate.py` directly | the skill mandates a Decision Menu + `GO` |
| pin | `VERSION` file **+ plugin tree hash** | no version string exists anywhere in `report.json` (verified) |
| our spec | keep `migration-spec.json`, **add `identifier_map`** | see §6 |

---

## 5. Consume / build / skip

### ✅ CONSUME
Tier-0 build, `connection_to_m.py`, field-parameter emission, `model_translation_handoff`,
`check_candidate_dax()`, `triage`, `viz_fidelity`, `pbip_ref_drops`, provenance annotations.

### 🔨 BUILD
1. **Height-floor clamp** — 57 % of validate findings, no LLM. *(after the I3 barrier)*
2. **Rebase mechanism** — ⚠️ **not** relative paths. A `SourceFolder` M parameter + a `rebase_bundle`
   command, plus a MAX_PATH budget assertion (our own archive broke at 282 chars).
3. **M-layer detectors** — `M_PARAM_UNDEFINED`, `M_PARAM_COLLISION`, `TABLEAU_TOKEN_IN_NATIVE_SQL`.
4. **`identifier_map`** — caption ↔ sanitized model identifier.
5. **Complement contract** (§6) — much smaller than v1's.
6. **`viz_fidelity` → PBIR resolver.**
7. **The `expected`-value producer** — ⚠️ **v3 correction:** he **ships** the comparator
   (`fidelity_oracle.py`, 227 KB, `dax_value_tier(expected=…)`, tolerance 0.005). We build and feed the
   **Tableau-side ground truth** (`capture_tableau_reference.py`, `migrations/*/reference/`) and the
   validator's adjudication layer. **Do not rebuild the comparator.**
8. **Gate fixes** — `denied_dirs()` vs `audited_paths()`; audit materialized data by content.

### 🚫 SKIP
Our own calc-handoff schema, DAX syntactic gate, cascade analysis, connector-M generator.

### 5.4 ⚠️ Tier 3 has no on-disk seam
`SKILL.md:982` marks `--approved-viz` as future work; today it is in-process only. And
`monotonic_gate.py` scores *presence* of labels/title/legend rather than correctness, permits
feature-only scoring, and keeps ties. **"Provably ≥ deterministic" is overstated.** → Defer Tier 3;
use our own report-builder + validator loop until an upstream versioned seam exists.

---

## 6. The complement — `handover/complement.v1.json`

v1 duplicated `report.json`. **Deleted** (now pointers — copies drift on every re-run):
`input_sha256` → `$.input_manifest.assets[0].sha256` · `artifacts.pbip` → `$.openable_outputs[0].pbip`
· `calcs_untranslated` → `$.summary.needs_review_total` · `duplicate_datasource_candidates` →
`$.workbooks[0].consolidated_datasources` · binary landing → `$.workbooks[0].flatfile_data`.

**Kept / added — each verified absent upstream:**

| field | why | without it |
|---|---|---|
| `producer.skill_version` + `plugin_tree_sha256` | **no version string exists anywhere in `report.json`** | cannot tell a known defect from a regression |
| **`identifier_map[]`** | `second-compiler.md:2` demands *resolved MODEL identifiers, never Tableau captions*; our spec holds captions, his model holds `'…_2022_2025_1#csv'` | the validator compares our X to his Y with no mapping |
| **`guidance_sha256`** per category | guidance is engine prose steering our LLM; a reword ships as a patch | invisible behaviour drift, no canary signal |
| `tables[].landing` per table + `path_is_absolute` | `data_landed` is not binary (1 real + 6 stub observed) | trusts a half-empty model |
| `portability{max_path_length, rebase_required}` | absolute paths are **deliberate**; MAX_PATH bit us | ships a bundle that dies on another machine |
| `credential_receipt.gate_state` | | retries a modal no automation can fill |
| `m_defects[]` | no tier of his gates the M layer | quotes `errorCount: 0` over undefined M params |
| `materialized_data[]` (path, bytes, sha256) | **220 MB of source rows escape `verify()`** | customer data on disk, unaudited, no retention rule |
| **`landing_ledger[]`** | after `--approved-dax` nothing distinguishes our objects from his | cannot audit or revert our own edits |

---

## 7. Instruction shaving — downgraded to a hypothesis with a non-gameable test

v1's criterion ("persona char count decreases while coverage increases") is gameable twice: prose can
be moved into a skill file (this repo has already done exactly that — `AGENTS.md:175`), and
`coverage_pct` counts *landed* objects, not correct ones — `check_candidate_dax`'s own docstring says
passing means *"well-formed DAX … never 'numerically faithful'."*

**Realistic assessment:** of the 6 categories, `dax_language_gap` is a refusal list, and
`model_object_parameter` instructs the agent to execute his private Python library — which *adds*
persona prose. Guidance plausibly replaces category-level prose for ~2 of 6.

**New criterion:** *oracle-verified correct measures on a fixed hold-out set increases, at
constant-or-lower total agent-visible instruction bytes (persona **plus every skill loaded on that
path**).*

⚠️ Personas are **already over** the 30,000 cap (30,674 / 30,611 / 30,403) — a real constraint.

---

## 8. Upstream feedback

1. 🔴 **`viz_fidelity` lacks `page_id`/`visual_id`** — yet `pbip_ref_drops` proves the emitter knows them.
2. 🔴 **Workbook path emits no datasource telemetry** — `datasources: []`, `connectors_seen: []`,
   `tables_translated: 0`, while `workbook_calcs_total: 276` and a 2-partition model was built.
3. 🔴 **No version string anywhere in `report.json`.** Add `producer.skill_version` — cheapest,
   highest-value change for any consumer.
4. 🟠 **`fields[]` resolves no physical fields** — 451 `unresolved`. A transitive closure carrying
   model bindings would make the handoff self-sufficient.
5. 🟠 **The M layer has no gate** — `check_candidate_dax` rejects `[parameters]` only in returned DAX,
   never in emitted M. Run the same scan over emitted M; add `#"Name"` resolution to
   `openability_selfcheck`.
6. 🟠 **No rebase seam** — absolute paths are correct but unrelocatable; a `SourceFolder` parameter
   fixes it upstream for everyone.
7. 🟡 **Contradictory counters** — `workbooks_viz_warned = 0` vs `visuals_warned = 56`.
8. 🟡 **`--approved-dax` keyed by bare calc name**, case-insensitive, estate-wide → collision risk.

---

## 9. Phasing (re-ordered around the invariants)

| # | phase | acceptance criterion (falsifiable) |
|---|---|---|
| **0** | Fix our classifier defects; parse federated `connections[]`; iterate **all** connections in preflight | 8-case agreement test `classify_source` ≡ `connection_target.powerbi_target` (6 currently contradict); artifact 1 → **3** connections; a mixed federated source arms the gate |
| **0.5** | ⭐ **Fidelity spot-check of the UN-WARNED surface.** Fixture: **airline** — `examples/airline-alliance-activity/fabric/` (108 measures, ours) vs `C:\pbip\airline-fresh` (88 translated, his), same source workbook. Feed `expected` values to **his** `fidelity_oracle.dax_value_tier` | 5 of the 88 `translated: true` measures reconcile to Tableau values. **If silence ≠ correctness, STOP — the thesis is wrong** |
| **1** | Gate fixes: split `denied_dirs()` / `audited_paths()`; audit materialized data by content | red-then-green: a migration whose only artifacts are `deterministic/**.pbip` **and** a 110 MB `.csv` makes `verify()` exit non-zero (today: 0 for both) |
| **2** | `identifier_map` emitter | every caption in `migration-spec.json` maps to a model identifier or is explicitly unmatched |
| **3** | M-layer detectors | flags live artifacts 1 & 3 **and** passes all 16 extracts **in the same run** (positive + negative control together) |
| **4** | Complement emitter + `guidance_sha256` | schema-validates; contains no field already in `report.json`; hash changes when a guidance string is edited |
| **5** | Canary: landing behaviour **+ guidance hashes + CLI flag surface** | fails on 2.34, passes on 2.40, fails on a reworded guidance string |
| **6** | ⭐ **Prove the Tier-1 seam.** Fixture: resolve the **keystone** `Calculation_2324420414664089602` (referenced by 124/188, blocks 60) and measure the queue drop; must also include ≥1 genuine `parameter reference` stub | mechanical: `needs_review_total` falls by exactly the count landed. semantic: each landed measure reconciles via `fidelity_oracle`. Re-run preserves unrelated objects (I2/I3) |
| **7a** | repair persona → `--approved-dax` | coverage → **ceiling 84.8 %** on airline (`1 − 42/276`). ⚠️ v2's 62 % was wrong — 63 of 105 `model_object_parameter` are misfiled |
| **7b** | Tier 1b — model objects via his `parameters.py`, in-process | the 105 parameter stubs land as field-parameter tables; Desktop renders ≥1 parameter-driven visual |
| **8** | Deterministic post-passes: height clamp + rebase | *(strictly after the I3 barrier)* validate errors → <40; **a bundle moved to a new folder opens AND refreshes** |
| **9** | `viz_fidelity` resolver + Tier 3 (only if a seam exists) | for ≥N entries the resolver names an existing `visual.json` that is the warned one |
| **10** | Full numeric fidelity | ≥N measures match Tableau across ≥3 workbooks |

**Same-commit:** Phase 1's two halves. **Barrier:** Phase 8 strictly after 7a/7b.

---

## 10. Risk register (re-ranked)

| # | risk | mitigation |
|---|---|---|
| **1** | 🔴 **Silence ≠ correctness** — 88 translated + 51 rebuilt accepted on the emitter's say-so | Phase 0.5, before anything is built on it |
| **2** | 🔴 **Bundle clobbering / data race** — `--approved-dax` rebuilds in place; two agents unguarded | I1 / I2 / I3 |
| **3** | 🔴 **Semantically wrong DAX passing a syntactic gate** | oracle in Phase 6, not Phase 10 |
| **4** | 🔴 **Materialized data escapes the gate** — 220 MB unaudited | Phase 1 `audited_paths()` |
| **5** | 🟠 **Object identity** — approvals keyed by bare name, case-insensitive, estate-wide | I6 |
| **6** | 🟠 **Guidance-prose drift** — engine prose steers our LLM with no schema break | `guidance_sha256` + Phase 5 |
| **7** | 🟠 **No fork protocol for the 56 % path** — Tier 1b mandates his `parameters.py`; a bug there is ours to hit, not to fix | agree a fork/PR protocol before Phase 7b |
| **8** | 🟠 **CLI flag stability** — 4,400-line script, no documented guarantee | Phase 5 canary covers flags |
| **9** | 🟡 Human `GO` gate | ⚠️ *demoted from v1's #1* — §4 already designs it away by calling the script |

---

## 11. Open questions

1. Upstream vs local for the M detectors. *Lean: local first, PR after.*
2. Does Tier 1b (in-process `parameters.py`) violate "DEPEND, don't absorb"? It imports his internals.
3. Retention/cleanup rule for materialized source data (~220 MB per workbook).
4. Does `monotonic_gate` subsume our visual cookbook, or conflict with it?
5. Is `identifier_map` derivable offline, or does it need a live model?

## 12. Not verified

- ❌ **The Tier-1 seam has never been executed** — Phase 6 exists solely for this.
- ❌ **Numeric fidelity vs Tableau — never measured, on any workbook, in any run.**
- ❌ Tier 1b in-process import — never attempted.
- ❌ Tier 3 / `monotonic_gate.py` — read, never run.
- ❌ Whether `--approved-dax` preserves unrelated objects — **assumed false** until Phase 6 proves it.
- ⚠️ n=20 artifacts; K unmeasured; one federated workbook is not a sample.
