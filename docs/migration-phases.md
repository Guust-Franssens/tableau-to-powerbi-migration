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
            │  PHASE 3 — ship   ⚠️ NO TOOL: manual copy today (issue #458), and the source
            │                      location is itself unsettled (issue #460)
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

⚠️ **`--verify` is a GATE, so it never silently drops what it cannot assess.** A run directory whose
`run.json` is missing, empty, truncated, malformed, locked, or holds valid JSON that is not an object
counts as `unverifiable` and exits 3. It used to be skipped in silence and counted in *no* bucket at
all, so a half-written run — exactly what an allocation interrupted between `mkdir` and the manifest
write leaves behind — printed `0 run(s): 0 intact, 0 moved, 0 unverifiable`, exit 0.

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
| `deliverables/` | *nothing* | a safety convention, not a stage — see below | **empty in every run on this machine** |
| `scratch/` | whoever needs it | disposable, run-owned; the only subdir a future `--prune` may ever delete | |

✅ Verified 2026-09-03 by direct measurement of `_runs/408-…` (`oracle-manifest.json` `view_count` /
`view_types`; `assets/parse-sweep.md`; `assessment/estate_survey.json` `summary`; directory counts
under `bundle/`).

⚠️ **`assessment/estate_survey.json` counts *required* datasources (11), `assets/` counts *harvested*
ones (19).** They answer different questions — "which published datasources does a workbook depend
on" versus "what did we download" — so a mismatch between them is not a defect.

### Two things about `bundle/` that mislead people

**1. `reports/` is the engine-truth BASELINE and must never be edited; `pbip/` is a working copy.**
There is **no `out/` level** — a bundle is `{pbip,reports,semantic_models,handover,data}`. Per
`AGENTS.md:773` agents edit `pbip/`; `reports/` stays pristine so the engine-gap delta remains
attributable. ⚠️ It is not the *only* documented working copy — the phase-2 package ships a
`copytree` of it that its own README also calls the place to edit (#460, phase 3 below). Compare the
baseline/working pair with `git diff --no-index --stat`, never PowerShell's `diff` (which is an alias
for `Compare-Object` and compares the two path *strings*). Source:
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
not a link — so edits made here do NOT appear in the bundle, and vice versa.** The package's own
README says to edit here; `AGENTS.md:773` says to edit in `<bundle>/pbip/`. Which one is
authoritative is an open design question, **#460**; it matters most at phase 3, where promoting from
the wrong one silently ships unedited engine output. See phase 3 below before you copy anything.

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

❌ **There is no tool for the phase 2 → phase 3 hop. It is a manual copy today**, tracked as **#458**.
It is a high-risk hop for two evidenced reasons: the copy is where `definition.pbir`'s `byPath` stops
resolving, and no gate checks that the *target* resolves (below); and **which location you copy FROM
is currently unsettled** (below, #460).

### ⚠️ First decide WHERE you are promoting from — the repo currently gives two answers

❌ **Open design question, tracked as #460 — do not treat either location as settled.** The toolkit
documents two different places an agent edits, and they are physical copies of each other:

| what says it | where it says agents edit |
|---|---|
| `AGENTS.md:773` (shared conventions) | `<bundle>/pbip/` — *"agents edit **here**"* |
| the package README `package_unit.py` writes (`scripts/package_unit.py:785`) | `<package>/fabric/` — *"the engine WORKING COPY - edit here"* |

The package's `fabric/` is a **`shutil.copytree`** of `<bundle>/pbip/<unit>/`
(`scripts/package_unit.py:847`), not a link, so an edit in one is invisible in the other. ✅ Proven by
sentinel edit during review of this document: after editing inside the package,
`PACKAGE_EDIT_EXISTS=True` / `BUNDLE_EDIT_EXISTS=False`. ✅ Corroborated here — immediately after
packaging the two trees are byte-identical (`git diff --no-index` exit 0, no output), so they can
only diverge by someone editing one of them. Promoting from `<bundle>/pbip/` when the agent worked in
the package ships **unchanged engine output and loses every agent-authored TMDL/PBIR change,
silently, with no gate firing**.

**Interim instruction until #460 is decided: promote from wherever the edits actually are, and verify
both before and after.** Do not assume — check:

```powershell
# BEFORE promoting — have the two candidate sources diverged at all?
git diff --no-index --stat <bundle>\reports\<WB>.Report <bundle>\pbip\<WB>\<WB>.Report
git diff --no-index --stat <bundle>\pbip\<WB> <run>\packages\<batch>\<Unit>\fabric

# AFTER promoting, model per workbook — ONE comparison
git diff --no-index --stat <source-you-chose> migrations\workbooks\<slug>\fabric

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
and your own knowledge of where the agent was told to work. **If you cannot establish it, stop and do
not promote** — an unresolved provenance question is exactly the silent-loss case this section exists
to prevent.

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

⚠️ **`deliverables/` is empty in every run on this machine, and no script in `scripts/` writes to
it.** ✅ Verified 2026-09-03: of nine directories under `_runs/`, the three allocated through
`work_dirs.py` have a `deliverables/` and all three contain **0 items**; the six legacy directories
have no such subdir at all. The only `scripts/` matches for the word are prose in
`check_migration_progress.py` and `run_estate.py`, not writes to this path.

It exists purely as a **preventive convention** from issue #322: an operator-facing
`connections.json`/`.md` naming real customer servers once landed unprefixed at the **repo root**,
one `git add -A` away from being committed to this public repository. `deliverables/` is a landing
zone that `.gitignore`'s root-anchored `/_*` covers by construction.

| | `packages/` | `deliverables/` |
|---|---|---|
| consumer | the **agent** (machine-read) | a **human / the customer** |
| produced by | `package_unit.py` | by hand, or a one-off script |
| granularity | one folder per unit | whole-run, ad hoc |
| a pipeline stage? | **yes** | **no** |

⚠️ **"Deliverable" unfortunately means two different things in this repo**: this run-local
`deliverables/` subdirectory (an operator's ad-hoc output, never git) and phase 3's
`migrations/**/fabric/` (the customer's actual Power BI deliverable, which *is* trackable). Renaming
the subdirectory — to something like `operator-output/` — is worth considering; it has no current
user, so the rename is cheap today and gets more expensive the moment one appears.

---

## Known gaps

| # | gap | status |
|---|---|---|
| 1 | No tool for phase 2 → phase 3; the `byPath` rewrite is manual and a wrong one validates clean | ❌ open, tracked as **#458** |
| 2 | Two documented edit locations — `<bundle>/pbip/` (`AGENTS.md:773`) and `<package>/fabric/` (`package_unit.py:785`) — diverge because the package is a `copytree`, so promoting from the wrong one silently loses agent work | ❌ open design question, tracked as **#460**; promote from wherever the edits are and verify both ways |
| 3 | `semantic_models/` baselines exist for only 18 of 62 units on the reference run, so model churn cannot be measured for the rest | ❌ report as **BASELINE UNAVAILABLE** (#274, #359) |
| 4 | `deliverables/` is a convention with no writer, and its name collides with phase 3's meaning | ⚠️ documented, not fixed |
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
