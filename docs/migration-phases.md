# The three-phase migration pipeline

Where everything lives, what moves between the locations, and which gate guards each hop.

Read this when you are picking up a migration and need to know *where to find what*. The
command-by-command procedure — flags, timings, exit codes, failure playbook — is
[`docs/operator-runbook.md`](operator-runbook.md); this document explains the **shape** that runbook
walks through.

The pipeline has three locations, and the direction is one-way:

```
   TABLEAU SITE  /  .twb .twbx .tds .tdsx
            │
            │  PHASE 1 — collect and convert  (deterministic; scripts only)
            ▼
   _runs/<NNN>-<slug>/
       assessment/   what exists, what is used, migration ORDER
       assets/       the downloaded workbooks and datasources
       bundle/       the engine's conversion output
       oracle/       Tableau's OWN renders and numbers
       packages/     ┐
       deliverables/ ├─ see phase 2 and the "not a stage" note below
       scratch/      ┘
            │
            │  PHASE 2 — package for the agent  (scripts/package_unit.py)
            ▼
   _runs/<NNN>-<slug>/packages/<batch>/<Unit>/
       one self-contained folder per migration unit; BOTH gates accept it with NO flags
            │            entry gate: check_reference_readiness.py   (ready / blind)
            │            exit  gate: check_unit.py                  (is this unit done?)
            │
            │  PHASE 3 — ship   (scripts/promote_unit.py, #458/#462) — FROM the package,
            │                      which is the canonical edit location (#460, settled)
            ▼
   migrations/{workbooks,datasources}/<slug>/fabric/
       the deliverable a customer opens in Power BI Desktop
```

Everything under `_runs/` is gitignored by construction — `.gitignore:162` is a root-anchored `/_*`
— so phases 1 and 2 never risk a commit. Phase 3 is the only location where committing is possible,
which is also why it is the one with a privacy footgun (see below).

⚠️ **Gitignored is not the same as disposable.** A run directory holds the agent's editable working
copy *and* the evidence trail behind every verdict. Only `scratch/` is disposable — `work_dirs.py`
calls it "the only subdir a future `--prune` may ever delete". A whole run becomes safe to delete
only **after** its units have been promoted to phase 3 and that promotion has been verified.

Measured figures below come from **one real cold run** against a 48-workbook Tableau Cloud site,
`_runs/408-…`, 2026-09-03. They are *our* reference estate, not a customer's — never quote them as a
customer's numbers.

---

## Phase 1 — collect and convert

One numbered run directory per pipeline run, allocated by
[`scripts/work_dirs.py`](../scripts/work_dirs.py) (`allocate_run`). **The number is the identity**
— never renamed, never reused, because generated bundle output embeds absolute self-paths. The slug
is decoration for human navigation; nothing may parse it back into a display name (two projects can
legitimately hold same-named workbooks — issue #234, rule 2).

⚠️ **"Per pipeline run" means per *invocation*, not per workbook.** Run 408 below is **one** run
covering a 48-workbook site; its per-workbook units live inside that run's `bundle/pbip/`, never in
48 sibling run directories. `unit_key` names what the run is *about* (a site, a project, or a single
workbook when that is genuinely the whole job) — it is not a promise that each workbook gets a run.
A customer's agent read `work_dirs.py`, inferred one-run-per-workbook, and renumbered **14** run
directories in the resulting reorg, breaking the absolute self-paths inside each one (issue #470).

⚠️ **Renaming an existing run is destructive, and it is now detectable.** `allocate_run` records both
the directory name and the absolute path it allocated, and `python scripts/work_dirs.py --verify`
reports each run as `intact` (exit 0 — recorded name *and* recorded path both still match), `moved`
(exit 1 — renamed or renumbered since allocation, naming both directories) or `unverifiable`
(exit 3 — the question cannot be answered from the evidence). **`unverifiable` is not a pass.**

⚠️ **`--verify` is a GATE, so it never silently drops what it cannot assess.** It rests on one
invariant, implemented in one discovery function and one classification function: **every child
directory of an existing `_runs/` root is a candidate run, any candidate whose identity cannot be
positively established from its own evidence is `unverifiable`, and `intact` is only ever returned
on positive proof.** Five shapes land there, and every one of them used to exit 0:

| shape | what it printed before |
|---|---|
| `run.json` missing, empty, truncated, malformed, locked, or valid JSON that is not an object | `0 run(s): 0 intact, 0 moved, 0 unverifiable`, exit 0 — a half-written run, exactly what an allocation interrupted between `mkdir` and the manifest write leaves behind |
| the directory renamed out of the `<NNN>-` pattern **and** its manifest deleted; or the `_runs/` root itself unreadable | `(no run directories found)`, exit 0 — discovery filtered on the *current* name and turned an `OSError` on the root into an empty list, while the tree was still on disk |
| `run` contradicts the `<NNN>-<slug>` directory it sits in (`2`, or `10**100`, inside `001-acme`) | `1 intact`, exit 0 — only the *type* of `run` was checked, never its agreement with the directory. The number **is** the identity, so a manifest claiming a different one is contradictory evidence |
| `allocated_abs_path` recorded as a **relative** path | `1 intact`, exit 0 — it was completed against the *verifier's* current directory, so the verdict depended on where the operator was standing |
| recorded name matches but the recorded absolute path does not | see below |

⚠️ **A run that is where it was allocated but at a different absolute path is `unverifiable`, not
`intact`.** A basename is unchanged by a run-only move *and* by a copy, so the name alone cannot
answer "still there": moving `source/_runs/001-acme` to `moved/_runs/001-acme` reported `INTACT`,
exit 0. Whether the whole checkout moved (legitimate) or this one run was moved or copied
(corruption) cannot be told apart from a single run's evidence — and **both** break the absolute
self-paths embedded under `bundle/`, so neither is reported as healthy. Note that a clone and a
fresh `git worktree` carry no runs at all (`_runs/` is git-ignored: `git check-ignore -v --
_runs/001-acme/run.json` → `.gitignore:162:/_*`), so this is not a fresh-clone false alarm.

`CANONICAL_SUBDIRS` (`scripts/work_dirs.py:72`) is the single source of truth for the layout:

| subdir | produced by | contents | measured on run 408 |
|---|---|---|---|
| `assessment/` | `run_engine_survey.py`, then `assess_estate.py` | what exists on the site, what is actually **used**, migration **order**, `estate.db`, `report.md`, `estate_survey.json` | 48 workbooks, 0 listing errors, 11 required published datasources |
| `assets/` | `harvest_estate_assets.py` | the downloads under `assets/assets/`, plus an estate-wide `parse-sweep.md`/`.json` | 67 assets (48 workbooks + 19 published datasources); **0 parse failures on either tier** |
| `bundle/` | `run_estate.py` (wraps the deterministic engine) | `pbip/`, `reports/`, `semantic_models/`, `handover/`, `data/` | 62 `pbip/` units, 48 report baselines, **18** model baselines, 46 handover slices |
| `oracle/` | `capture_tableau_oracle.py` | Tableau's OWN renders and numbers, keyed by **view LUID** | 360 views (60 dashboards / 300 worksheets), 288 captured complete |
| `packages/` | `package_unit.py` | phase 2 — see below | |
| `deliverables/` | *nothing yet* | a safety convention, not a stage — see below | not created until first use (issue #481) |
| `scratch/` | whoever needs it | disposable, run-owned; the only subdir a future `--prune` may ever delete | |

✅ Verified 2026-09-03 by direct measurement of `_runs/408-…` (`oracle-manifest.json` `view_count` /
`view_types`; `assets/parse-sweep.md`; `assessment/estate_survey.json` `summary`; directory counts
under `bundle/`).

⚠️ **`assessment/estate_survey.json` counts *required* datasources (11), `assets/` counts *harvested*
ones (19).** They answer different questions — "which published datasources does a workbook depend
on" versus "what did we download" — so a mismatch between them is not a defect.

### Two things about `bundle/` that mislead people

**1. `reports/` is the pristine engine baseline / model-unbound pass and must never be edited; `pbip/` is the model-bound engine working pass and the source lineage used for packaging.**
There is **no `out/` level** — a bundle is `{pbip,reports,semantic_models,handover,data}`. Per
`AGENTS.md:773` agents edit `pbip/` before packaging, but `reports/` stays pristine so the engine-gap
Delta remains attributable. Once a phase-2 package exists, `<package>/fabric/` is the canonical edit
and ship tree, and phase 3 promotes it via `scripts/promote_unit.py` into
`migrations/{workbooks,datasources}/<slug>/fabric/` (#460, settled; phase 3 below). The actual
agent-edited and shipped bytes live in the package and the promoted destination, not in a potentially
stale bundle copy. `shipped_tree_divergence` is disclosure, not fidelity proof: a report can
legitimately diverge from the baseline and still be broken in the shipped visual, so the validator
must inspect the package or promoted destination and treat divergence as a finding. Compare the
baseline/working pair with `git diff --no-index --stat`, never PowerShell's `diff` (which is an
alias for `Compare-Object` and compares the two path *strings*). Source:
[`.github/skills/powerbi-report-gotchas/SKILL.md` §3](../.github/skills/powerbi-report-gotchas/SKILL.md).

**2. `semantic_models/` is NOT a per-workbook guarantee.** ✅ Measured on run 408: **18 model
baselines for 62 `pbip/` units**. That tree is keyed by *model*, not by workbook, and is emitted for
models not owned by a single workbook (published/shared datasources). A missing counterpart must be
reported as **BASELINE UNAVAILABLE** — never as a clean or no-change diff. The miss is silent:
`git diff --no-index` exits **1** both for *"could not access"* and for *"they differ"*, so an absent
baseline reads as "large diff, do not re-run" (issue #274, #359).

---

## Phase 2 — package for the agent

[`scripts/package_unit.py`](../scripts/package_unit.py) (issues #446 / #451) assembles **one
self-contained folder per migration unit that both gates accept with NO flags.**

That "no flags" property is the entire point. Before it, the three things an agent needs lived in
four naming schemes across two trees — the engine keys `pbip/`, `reports/` and `handover/` by
sanitized workbook name, the oracle keys renders and numbers by bare view LUID in a flat tree
*outside* the bundle, and the source asset is a LUID-prefixed filename in a third place. So both
gates needed `--source`/`--oracle` paths that **cannot be derived from the unit path**, and getting
one wrong reads as *"this unit is broken"* rather than *"you did not tell me where the workbook
is"*.

Per-unit layout (authoritative list: `package_unit.py`'s module docstring):

```
<out>/<Unit>/
    migration-spec.json          parse_tableau.py; check_unit.py's expected page set (#443)
    report.json                  the engine's own classification, SCOPED to this unit
    source-provenance.json       SCOPED; the only trusted route to a workbook LUID
    engine-output-receipt.json   what built this, so version drift stays checkable
    assets/<luid>_<Name>.twb(x)  the source, under the name resolve_source() already looks for
    fabric/<Name>.Report/        the engine WORKING COPY (a copytree of pbip/), never the baseline
    fabric/<Model>.SemanticModel/
    handover/<Unit>.json         the engine's per-workbook slice, verbatim
    handover.md                  flat, one finding per line, emptied visuals FIRST
    oracle/
        oracle-manifest.json     THIS unit's views only, paths rewritten
        dashboard/{images,data}/<Object>.<ext>
        worksheet/{images,data}/<Object>.<ext>
        unknown/{images,data}/<Object>.<ext>
    package-manifest.json        what was packaged, and every omission with its reason
    README.md
```

⚠️ **The oracle kind directories are SINGULAR on purpose** — `dashboard/`, `worksheet/`, `unknown/`
are `object_identity`'s `KIND_*` values *verbatim*, never a pluralised copy. `unknown/` is carried
but **marked** (`UNTYPED_RENDER`), never filed as either kind, because `view_type` is the only type
discriminator and its resolver is non-fatal by design.

⚠️ **Attribution is fail-closed and by LUID only.** A render that cannot be tied to a workbook by
`workbook_luid` is omitted with its reason recorded, never copied in because it was in the same
capture. A display name is not an identity (#450).

❌ **`fabric/` here is a `shutil.copytree` of `<bundle>/pbip/<unit>/` (`scripts/package_unit.py:847`),
not a link — so edits made here do NOT appear in the bundle, and vice versa.** ✅ **Which one is
authoritative is SETTLED (#460): once a package exists, `<package>/fabric/` is canonical.** Two
pieces of code enforce it rather than merely documenting it — `promote_unit.py` ships FROM the
package (*"Promoting FROM the package is settled (#460)"*, `scripts/promote_unit.py:60`), and
`package_unit.py` refuses to repackage over a package that carries edits, checking the digest twice
and again under the swap. `AGENTS.md:773`'s *"agents edit `pbip/`"* governs the window **before**
packaging; after it, work in the package. See phase 3 below for the promotion mechanics.

### Where `packages/` goes, and the constraint that decides it

```powershell
python scripts\package_unit.py --bundle _runs\<NNN>-<slug>\bundle `
    --out _runs\<NNN>-<slug>\packages\<batch> `
    --json _runs\<NNN>-<slug>\packages\<batch>\packaging.json
# then, per unit, with NO flags at all:
python scripts\check_reference_readiness.py _runs\<NNN>-<slug>\packages\<batch>\<Unit>
python scripts\check_unit.py                _runs\<NNN>-<slug>\packages\<batch>\<Unit>
```

⚠️ **`--out` must name a subdirectory INSIDE `packages/`, never `packages/` itself.** The gates
discover evidence by scanning the target, its parent **and its grandparent** for `reference/` /
`oracle/` / `_oracle/`, so `package_unit.conflicting_evidence_dirs`
(`scripts/package_unit.py:819`) refuses an `--out` whose parent already holds one. A run root
*always* holds `oracle/`, because `allocate_run` creates every canonical subdir.

✅ Measured 2026-09-03 against run 408's bundle:

| `--out` | exit | outcome |
|---|---|---|
| `_runs/<NNN>-<slug>/packages` | **2** | refused: *"sits beside evidence the gates also scan … Choose an `--out` outside the capture tree"* |
| `_runs/<NNN>-<slug>/packages/<batch>` | **0** | packaged; the readiness gate then reports **0 unverifiable**, i.e. no shadowing |

Were the bare form used, every page would silently drop from `ready` to `unverifiable` — strictly
worse than not packaging at all — which is why it is refused rather than documented.
`tests/test_work_dirs.py::test_package_out_must_be_a_child_of_packages_not_packages_itself` pins
this so the command above stays runnable.

### The two gates

| gate | question | verdicts |
|---|---|---|
| [`check_reference_readiness.py`](../scripts/check_reference_readiness.py) — the **ENTRY** gate | per report page, is there trustworthy Tableau reference evidence to *start from*? | exit 0 ready / 1 findings / 3 `CANNOT_ESTABLISH`; a page is `ready`, `blind`, `unverifiable`, or below the required grade. Neither 1 nor 3 is a pass |
| [`check_unit.py`](../scripts/check_unit.py) — the **EXIT** gate | *"answer whether one migration unit is done by aggregating existing gates without merging them"* (`check_unit.py:2`) | per-scope `model` / `report` / `integration` / `all` |

A **blind** page means an equivalent fidelity bug there is *structurally unfalsifiable*, not merely
unverified. Detail: [`docs/reference-readiness.md`](reference-readiness.md).

⚠️ An oracle capture is **layout/text grade only** — default view state, no `?vf_` filter pinning —
regardless of render leg. Record that ceiling in `limitations_encountered`; a visual PASS signed off
on oracle imagery alone is overstated (issue #194).

---

## Phase 3 — ship

The deliverable lands in `migrations/workbooks/<slug>/fabric/` (a workbook's report + model) or
`migrations/datasources/<ds-slug>/fabric/` (a shared/published datasource's model).

✅ **The current phase 2 → phase 3 path is `scripts/promote_unit.py`**. It is the package-canonical
ship step and is the current path for promotion. Manual copy remains only as a clearly labelled
fallback/reference for understanding the mechanics or for a hand-checked promotion when the tool is
not available.

The copy is still a high-risk hop for one evidenced reason: `definition.pbir`'s `byPath` can stop
resolving after the move, and the validator must inspect the shipped PBIP rather than assume the
earlier engine pass is the shipped truth.

### WHERE you promote from — settled (#460): the package

✅ **The package's `fabric/` is canonical once phase 2 has run.** Two pieces of code enforce it, so
this is not a documentation preference:

| what enforces it | how |
|---|---|
| [`scripts/promote_unit.py:60`](../scripts/promote_unit.py) | *"Promoting FROM the package is settled (#460)"* — `--package` is the only source it takes; `--bundle` produces a drift REPORT and never a promotion source |
| [`scripts/package_unit.py`](../scripts/package_unit.py) | refuses to repackage over a package that carries edits (`PackageEditsRefused`), checking the digest before assembly **and again under the swap** — the package is the tree it protects |
| [`scripts/check_migration_progress.py`](../scripts/check_migration_progress.py) | `--tamper` and progress mode both scan the packages beside the bundle; an edited canonical package is `PACKAGE_DRIFT` (exit 1), never `CLEAN` |

`AGENTS.md:773`'s *"agents edit `<bundle>/pbip/`"* governs the window **before** packaging — there is
no package yet to edit. After phase 2 the package is where the work goes, and `pbip/` is left as the
engine's own copy.

The package's `fabric/` is still a **`shutil.copytree`** of `<bundle>/pbip/<unit>/`
(`scripts/package_unit.py:847`), not a link, so an edit in one is invisible in the other. ✅ Proven by
sentinel edit during review of this document: after editing inside the package,
`PACKAGE_EDIT_EXISTS=True` / `BUNDLE_EDIT_EXISTS=False`. ✅ Corroborated here — immediately after
packaging the two trees are byte-identical (`git diff --no-index` exit 0, no output), so they can
only diverge by someone editing one of them. Promoting from `<bundle>/pbip/` therefore ships
**unchanged engine output and loses every agent-authored TMDL/PBIR change, silently** — which is what
the rule above exists to prevent, not a live ambiguity.

**Verify anyway, before and after.** A settled rule tells you where the work *should* be; only a diff
tells you where it *is*, and a unit migrated before the rule was settled may have been edited in the
bundle:

```powershell
# BEFORE promoting — has anyone edited the tree we are NOT promoting from?
git diff --no-index --stat <bundle>\reports\<WB>.Report <bundle>\pbip\<WB>\<WB>.Report
git diff --no-index --stat <bundle>\pbip\<WB> <run>\packages\<batch>\<Unit>\fabric

# AFTER promoting, model per workbook — ONE comparison
git diff --no-index --stat <run>\packages\<batch>\<Unit>\fabric migrations\workbooks\<slug>\fabric

# AFTER promoting, shared datasource — TWO comparisons, because the halves split up
git diff --no-index --stat <source>\<Name>.Report        migrations\workbooks\<slug>\fabric\<Name>.Report
git diff --no-index --stat <source>\<Name>.SemanticModel migrations\datasources\<ds-slug>\fabric\<Name>.SemanticModel
```

⚠️ **`--stat` proves DIVERGENCE ONLY; it cannot establish provenance.** Exit 0 with no output means
the two are still identical, so the choice does not matter yet — measured on a fresh package. A
non-empty result means they have diverged, **and nothing more**. In particular the **insertion count
does not identify the edited side**: ✅ measured, a deletion-only edit *inside the package* reports
`1 file changed, 9 deletions(-)` — no extra insertions on the edited side at all; a replacement
yields equal insertions and deletions; and both trees may have been edited. To establish which side
is authoritative, use the **full diff** (drop `--stat`), the `_build/generated-edit-declarations/`
records where they exist ([`powerbi-report-gotchas` §3](../.github/skills/powerbi-report-gotchas/SKILL.md)),
`check_migration_progress.py --tamper` (which now names an edited package by file), and your own
knowledge of where the agent was told to work. **If a diff says the BUNDLE was edited after
packaging, stop and do not promote** — that work is not in the canonical tree and promoting would
drop it.

⚠️ **Never use the whole-tree form on a shared datasource — it calls a correct promotion wrong.**
✅ Measured on a correctly split promotion: source-unit vs workbook destination exits **1**
(`20 files changed, 1272 deletions(-)`) *precisely because the model correctly landed under
`migrations/datasources/`*, while the report half and the model half each exit **0**. The whole-tree
form is right only for the model-per-workbook case (measured: exit **0**).

⚠️ Require a real stat line, not just the exit code — `git diff --no-index` exits **1** both when
trees differ and when a path is wrong. And on Windows use `git diff`, never bare `diff`: that is a
PowerShell alias for `Compare-Object` and compares the two path *strings*.

### The `byPath` mechanics (still correct, and still necessary)

Source: [`.github/skills/powerbi-report-gotchas/SKILL.md` §3](../.github/skills/powerbi-report-gotchas/SKILL.md)
(🟢 verified 2026-08-11). These apply whichever source location you promote from.

**Model per workbook — plain copy, but copy the CONTENTS.** The promoted unit folder already holds
`<Name>.Report/` and `<Name>.SemanticModel/` as **siblings**, with `"path": "../<Name>.SemanticModel"`,
and the delivery folder has the identical shape. So copy the *contents* of that folder, not the folder
itself — it is named for the **workbook**, the model inside for the **datasource**, so copying the
folder nests them wrongly.

**Shared/published datasource — the reference MUST be rewritten, and the two halves SPLIT UP.**
⚠️ The report and model do **not** both stay under the workbook's own `fabric/`: the model lands
**once** in `migrations/datasources/<ds-slug>/fabric/` and is shared, while each downstream report
goes to its own `migrations/workbooks/<slug>/fabric/`. They are no longer siblings, so
`definition.pbir` becomes:

```
"../../../../datasources/<ds-slug>/fabric/<Name>.SemanticModel"
```

— four levels up from inside `<Name>.Report/`. **Verify it resolves on disk after writing it.**

⚠️ **A wrong `byPath` opens as a report with NO MODEL, and `powerbi-report-author validate` returns
`errorCount: 0` for it** — it checks reference *shape*, not *target*. So this failure mode is
invisible to the validator you would reach for.

⚠️ **Never ship `<bundle>/reports/` itself.** It is a reference-only baseline: `reports/` holds only
`*.Report` folders, so the `byPath` beside it has no model to point at, and its exact (unresolvable)
value is engine-version dependent.

⚠️ **A shipped deliverable can be structurally present and functionally EMPTY.** Assert the shipped
`<Name>.Report/definition/pages/` enumerates real pages *with visuals* and the shipped
`<Name>.SemanticModel/definition/tables/` holds real tables. A folder count is not a content check,
and verifying a fix in one location proves nothing about `migrations/**/fabric/`.

### What is actually tracked in git here

⚠️ **Phase 3 artifacts are NOT blanket-gitignored** — this is a common misreading, and it is the
reason the privacy rules exist. ✅ Measured 2026-09-03 with `git check-ignore -v`:

| path | status |
|---|---|
| `migrations/workbooks/<slug>/fabric/<Name>.Report/**` | **trackable** (no ignore rule matches) |
| `migrations/workbooks/<slug>/reference/**` | ignored (`.gitignore:82`) |
| `migrations/workbooks/<slug>/data/**` | ignored (`**/data/`) |
| `migrations/workbooks/<slug>/source/*.twbx` | ignored (`**/source/*.twbx`) |
| `migrations/workbooks/<slug>/migration-brief.md` | ignored (`**/migration-brief.md`) |
| `migrations/{workbooks,datasources}/<prefix>-<slug>/**` for `customer-`, `workshop-`, `engagement-` | ignored (`.gitignore:46-51`) |

So a customer migration is kept out of this public repo by **prefixing the slug**
(`customer-<name>`), not by any rule on `fabric/`. Today exactly **3** files are tracked under
`migrations/` — the three `README.md` navigation files — because no public-safe migration has been
committed there yet.

---

## `packages/` versus `deliverables/` — one is a stage, one is not

✅ **Fixed in issue #481: `deliverables/` is no longer created eagerly.** It used to be — of nine
directories under `_runs/` measured 2026-09-03, the three allocated through `work_dirs.py` each had
an always-empty `deliverables/`, and no script in `scripts/` wrote to it (the only `scripts/`
matches for the word were prose in `check_migration_progress.py` and `run_estate.py`). An
always-empty, prominently-named folder in every run reads to an operator as "something failed" or
"something is missing".

The convention itself is still real (issue #322: an operator-facing `connections.json`/`.md`
naming real customer servers once landed unprefixed at the **repo root**, one `git add -A` away
from being committed to this public repository — `deliverables/` is the designated landing zone
that `.gitignore`'s root-anchored `/_*` covers by construction). What changed is *when* the folder
comes into being: `RunPaths.deliverables` now creates it **lazily, on first access**, instead of
`allocate_run` creating it up front for every run whether or not anything ever writes there. An
absent folder in a run that never used it says nothing; an empty one made a false claim.

⚠️ **This fixes the always-empty folder, not the underlying "no writer" gap.** Nothing in
`scripts/` writes to `run.deliverables` today — see the Known gaps table below.

| | `packages/` | `deliverables/` |
|---|---|---|
| consumer | the **agent** (machine-read) | a **human / the customer** |
| produced by | `package_unit.py` | by hand, or a one-off script |
| granularity | one folder per unit | whole-run, ad hoc |
| a pipeline stage? | **yes** | **no** |
| created | eagerly, at allocation | **lazily, on first access** (issue #481) |

⚠️ **"Deliverable" unfortunately means two different things in this repo**: this run-local
`deliverables/` subdirectory (an operator's ad-hoc output, never git) and phase 3's
`migrations/**/fabric/` (the customer's actual Power BI deliverable, which *is* trackable). Renaming
the subdirectory — to something like `operator-output/` — is worth considering; it has no current
user, so the rename is cheap today and gets more expensive the moment one appears.

---

## Known gaps

| # | gap | status |
|---|---|---|
| 1 | The `byPath` rewrite at phase 2 → phase 3 is a place a wrong reference validates clean | ✅ tooled by `promote_unit.py` (#458, #462), which rewrites and then proves the target resolves ON DISK |
| 2 | Two documented edit locations — `<bundle>/pbip/` and `<package>/fabric/` — that are physical copies | ✅ **settled (#460): the package is canonical once phase 2 has run.** `promote_unit.py` ships from it, `package_unit.py` refuses to repackage over its edits, and `check_migration_progress.py --tamper` reports an edited package as `PACKAGE_DRIFT` rather than `CLEAN` |
| 3 | `semantic_models/` baselines exist for only 18 of 62 units on the reference run, so model churn cannot be measured for the rest | ❌ report as **BASELINE UNAVAILABLE** (#274, #359) |
| 4 | `deliverables/` is a convention with no writer, and its name collides with phase 3's meaning | ⚠️ partially addressed (#481) — the always-empty eager folder is fixed (now created lazily, on first access); it still has **no production writer** and the name collision with phase 3's "deliverable" is unchanged, both still open |
| 5 | Phase 1 stages still default to their own `_assessment*/` / `_oracle*/` / `_bundle*/` paths rather than the `_runs/` layout | ⚠️ deliberate scope limit of issue #234; migrating them is follow-up |
| 6 | Oracle evidence is layout/text grade only; validation grade requires a render you captured yourself | ⚠️ provider limit, not an oversight (#194) |

## See also

- [`docs/operator-runbook.md`](operator-runbook.md) — the command-by-command procedure, timings, exit
  codes and failure playbook.
- [`docs/reference-readiness.md`](reference-readiness.md) — the entry gate in detail.
- [`docs/reference-capture.md`](reference-capture.md) — how Tableau reference evidence is captured
  and graded.
- [`AGENTS.md`](../AGENTS.md) — the dispatcher's intake, the engine-source rule, and the canonical
  work layout this document expands on.
