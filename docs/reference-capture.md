# Reference-image capture architecture

How the pipeline obtains a **rendered image of the original Tableau dashboard**, and how that image
flows to the two agents that need it. This design is the consensus of a two-model review
(Claude opus-4.8 + GPT-5.6-sol) of an earlier "just Playwright-scrape the public URL" approach; the
sections below mark what is **✅ implemented**, **⚠️ partial**, or **❌ specified-only** so the
enterprise story stays honest.

## Why this exists (the two consumers)

`migration-spec.json` captures *structure* (worksheets, encodings, dashboard zones, theme hexes, mark
types) but **not appearance**. A picture of the source is needed by **two** stages, for two different
reasons:

1. **`pbi-report-builder` — to mimic.** Its workflow compares a positional wireframe skeleton against
   the whole-dashboard reference *before binding any fields*, and picks slicer/parameter defaults to
   match the source state. Without the image it builds the layout blind.
2. **`pbi-migration-validator` — as independent ground truth.** It grades fidelity figure-by-figure.
   Its independence comes from **not seeing the builder's reasoning**, *not* from being blind to the
   original — so both agents consuming the same original is correct.

### The core reframe

The reference is **not "an image the pipeline fetches."** It is a **versioned, provenance- and
state-stamped evidence artifact with one producer and two read-only consumers.** Nearly every problem
below dissolves once it is modelled that way.

## The reference bundle

A migration's reference lives at `migrations/workbooks/<slug>/reference/` (git-ignored — see *Governance*) and is
described by a `manifest.json` so consumers never treat a 200px thumbnail and a full-res Server render
as interchangeable ground truth.

```jsonc
// migrations/workbooks/<slug>/reference/manifest.json   (⚠️ schema implemented; some fields still TODO)
{
  "captured_at": "2026-07-19T20:43:01Z",
  "source_workbook_sha256": "…",          // ties the image to an exact .twbx
  "dashboards": [
    {
      "name": "Price of Prosperity",
      "states": [
        {
          "state_slug": "default",
          "state": { "Year": 2020, "Region": "All" },   // filters/params pinned at capture (⚠️)
          "image": "Price of Prosperity/default.png",
          "provider": "public_playwright",
          "capabilities": ["layout_grade", "text_readable"],  // NOT validation_grade for public scrape
          "dimensions": { "w": 1600, "h": 2986, "dpr": 2 },
          "sha256": "…",
          "numeric_oracle": null            // optional CSV/crosstab captured at the SAME state
        }
      ]
    }
  ]
}
```

- **Capability flags, not a fidelity rank.** A provider advertises what its output is fit *for*:
  `layout_grade`, `text_readable`, `state_reproducible`, `revision_bound`, `validation_grade`. The
  validator refuses to sign off visual fidelity against anything lacking `validation_grade`.

⚠️ **A user-dropped screenshot is NOT `validation_grade` by default** — pass
`--manual-validation-grade` to assert it. Until 2026-08-18 the `manual` provider hardcoded
`validation_grade`, so any PNG left in `reference/` silently claimed the tier the validator signs off
on, with nothing verifying resolution, filter-state pinning, or even that the image came from the
handed-over workbook rather than a newer published revision. It failed **open**: the provider with
the weakest provenance claimed the strongest guarantee, by default, with no operator action and no
log line — and it outranked a live Tableau Server REST render, which is honestly graded layout+text
because `capture_tableau_oracle.py --images` captures the view's **default state** with no `?vf_`
pinning. Reported from a real estate run; the flag keeps the legitimate case (a human who did capture
full-resolution, state-pinned renders) while making the claim explicit and attributable.
- **Immutable + hashed.** The producer writes each image + its SHA-256; **neither consumer may
  regenerate, crop, or annotate it** (a builder-curated original silently destroys validator
  independence). Per-worksheet crops, if needed, are produced by the producer, not the builder.

### The oracle capture is FLAT, and grouping it is a separate step

`scripts/capture_tableau_oracle.py` does **not** write into the bundle above. It writes every view of
every workbook flat into `_oracle/images/<view>__<luid8>.png` and `_oracle/data/`, with the workbook
association living only in `oracle-manifest.json` (`workbook_luid` / `workbook_name` per view).

**That is deliberate and is not changing.** A LUID-keyed flat layout survives a workbook or view
rename; a folder-per-workbook layout is coupled to a name and silently splits a capture in two when
someone renames the workbook upstream. The flat capture stays the authoritative artifact.

⚠️ **Capture a whole batch in ONE invocation — `oracle-manifest.json` is rewritten wholesale per run,
never appended.** A second invocation into the same `--out` replaces the manifest, so a
workbook-at-a-time loop silently ends up with a manifest describing only the last workbook while the
image and data files from every earlier run are still on disk. That is the worst shape: the artifacts
look complete and the index says otherwise.

`--workbook` is `action="append"` and documented *"(repeatable)"* (`capture_tableau_oracle.py:469`),
so the fix needs no new CLI surface — pass one flag per workbook and capture the set in a single run:

```
python scripts/capture_tableau_oracle.py --out _oracle --images \
    --workbook "Sales Overview" --workbook "Ops Detail" --workbook "Exec Summary"
```

There is no `--project` flag, and server-side project filtering is blocked for now: Tableau's numeric
project id (the one in the site's own URL) has no public API mapping (issue #191). So expand the
project to its workbook names first, then pass them all in one invocation.

Only split into batches when you deliberately want crash-isolation on a long run — and then archive
each `oracle-manifest.json` before the next invocation overwrites it, and merge afterwards. Merging
is not automated on purpose: two manifests can disagree about a view's `capabilities`, and silently
picking one would corrupt the evidence grade this whole document exists to protect.

It is, however, the one artifact in this toolkit that does not follow the
`migrations/workbooks/<slug>/{source,fabric,data,reference}` convention, so browsing "what did we
capture for workbook X" otherwise means cross-referencing JSON by hand. Bridge it *after* capture:

```
python scripts/group_oracle_by_workbook.py --oracle _oracle [--migrations migrations/workbooks] [--dry-run]
```

It **copies** (never moves) each workbook's views into `<slug>/reference/{images,data}/` and writes a
per-workbook `oracle-manifest.json` subset beside them, with the per-workbook counts recomputed so a
partial capture cannot read as complete.

Why a post-step rather than a `--group-by-workbook` flag on the capture:

- it re-runs against an **existing** capture at **zero REST cost**. Tableau meters
  `/views/.../data` and `/image` at 100 calls/hr/Creator, so re-capturing merely to change the on-disk
  layout is the expensive way to get bytes you already have.
- capture stays a pure "talk to the API" step and grouping a pure "arrange local files" step, so the
  grouping is testable with no network at all.

**It matches folders that ALREADY EXIST and never slugifies a name into a path.** Both sides are
normalized (lowercased, non-alphanumerics dropped). A workbook with no folder is **reported, not
created**; a name that normalizes onto two folders is **reported ambiguous, not resolved by picking
one**. Exit `0` = everything landed, `1` = grouped what it could (details in
`_oracle/oracle-grouping-report.json`), `2` = the capture could not be read.

⚠️ **The normalizer drops punctuation and case but never words**, so a workbook carrying Tableau's
cross-project disambiguation suffix (`"Sales | Project : Finance"`) does **not** match a `sales`
folder and is reported unmatched. That is the same blind spot as the engine's own `_norm_ds()`, filed
upstream as [tableau-fabric-skills#145](https://github.com/Yarbrdab000/tableau-fabric-skills/issues/145);
reporting it is the honest outcome, and is why the exit code distinguishes "grouped everything" from
"grouped what it could".

> Credit: this pattern was validated independently by a user against a real capture — 38/38 workbooks,
> 95/95 views, zero ambiguous matches, zero name mismatches — before it was upstreamed here.

⚠️ **An oracle capture is NOT `validation_grade`.** Its images land outside `reference/`, carry no
`capabilities` manifest, and are taken in the view's **default state only** (no `?vf_` filter
pinning), so they are **layout- and text-grade only**. Grouping moves the files; it does not upgrade
the evidence. A visual PASS signed off on oracle imagery alone is overstated — log the ceiling in
`limitations_encountered`.

## Providers — resolve by *fitness*, not availability

The most important correction from the review: **REST is not automatically "highest fidelity."** A
Server/Cloud render is fidelity to *the published view, for that PAT user, at that moment* — which can
diverge from the handed-over `.twbx` via a newer published revision, an extract refresh, row-level
security, or personalized custom views. So there is **no global precedence ladder**; a resolver picks
the best provider *for the requested purpose* and records which it used and why.

| Provider | Typical role | Status |
|---|---|---|
| **Server/Cloud REST** (`/views/{id}/image?resolution=high`, `?vf_<field>=<value>` for state) | Canonical when the published view *is* the source and revision/state can be pinned | ⚠️ **transport implemented and live-tested** in [`scripts/capture_tableau_oracle.py`](../scripts/capture_tableau_oracle.py) `--images` (same endpoint, with `401002` re-auth + backoff); **not wired into this provider chain** — no provenance manifest, no state-pinning. See #194 |
| **Authenticated browser** (Playwright w/ session) | States/actions/extensions REST can't reproduce | ❌ specified-only |
| **Public Playwright** | Tableau **Public** only, after capture QA | ⚠️ works (this repo's demos); hardening TODO |
| **Guided manual export from the exact `.twbx`** (Tableau Desktop/Reader) | Extract-only workbooks with no live Server view — can be *validation-grade* | ❌ specified-only (guided prompts) |
| **Embedded `.twbx` thumbnail** | Low-resolution rendered evidence for mark shape, layering, axis direction and labels; XML wins any conflict | ⚠️ extractor implemented; found in 17/17 workbooks in one Superstore-family estate, superseding the older "~4% carry thumbnails" figure for this measurement shape; worksheet coverage was still partial (5/10 in one workbook) |
| **User-supplied screenshots** | Always-available floor; must be *guided* (exact filenames, reset state, viewport) | ⚠️ folder convention only |

### Default = fail **closed**

If no provider can produce a reference, the pipeline **blocks before report *planning* and asks for a
source** — it does **not** "proceed with a warning" (a buried warning recreates the exact
build-blind bug this design fixes). The only escape hatch is an explicit, user-acknowledged
**`structural-only` mode** that (a) may still build the semantic model + a provisional report, but
(b) **cannot claim visual fidelity** and (c) **cannot receive normal migration sign-off** — the
validator is told up front that gestalt grading is impossible. In non-interactive/CI runs, fail with an
actionable "missing reference" manifest instead of hanging on input.

**Configured-but-auth-failed ≠ not-configured.** During an explicit Server request, if
`TABLEAU_SERVER_*` is set but the PAT is dead/expired, **halt with a specific credential error** —
never silently fall through to public scraping (there is usually no public URL for a Server workbook).

Server capture is requested explicitly with **`--server-rest`**; credentials merely present in the
default `.env` do not request it and cannot pre-empt an offline thumbnail or manual reference.
`--env <path>` is the credential-file selector reserved for an explicit Server request (for example,
an engagement-specific file rather than the repository default); the unwired provider does not open
it yet. A requested Server capture still halts instead of degrading. `--structural-only` is the
explicit exception: it may bypass that halt, but its manifest cannot support a visual-fidelity claim
or normal sign-off.

## State-locking (the single-image trap)

A single default-state render manufactures false discrepancies: our own demo defaults to **year 2020**,
so grading a PBI report that defaults elsewhere shows *state* drift, not *fidelity* drift — and the
builder might then bake `Year=2020` in to match. Therefore:

- **Capture pins and records state**, derived from the parser's parameter/filter **defaults** (so the
  reference reproduces the workbook's *own* default state deterministically).
- **State flows downstream:** the builder sets PBI defaults to that state; the validator sets the PBI
  report to the manifest's recorded state *before* comparing. A discrepancy that disappears when state
  is matched is a **state difference, not a fidelity defect**.
- **Separate the oracles.** The image is the **visual oracle**; a CSV/crosstab exported *at the same
  state* is the **numeric oracle** (the validator already prefers exported CSV for numbers). Never read
  numbers off pixels.
- **Bind numeric truth to the workbook's source file, never the estate.** List the bundle's `data/`
  directory before reusing an estate-wide constant. In the measured estate, most workbooks bound the
  9,994-row source (`SUM(Sales) = 2,297,200.8603`), but `book_7-3-LOWESS-Python` bound
  `Sample - EU Superstore.xls` (10,000 rows, `SUM(Sales) = 2,938,089.0615`, 21 columns and no
  `Postal Code`).
- **Bounded multi-state (P1):** baseline + one alternate per important parameter, pairwise-sampled — not
  a Cartesian explosion. `reference/<dashboard>/<state-slug>.png` is used from day one even when only
  `default` is populated, so multi-state drops in without re-architecting.

## Migration mode (builder ↔ validator contract)

The builder is told "feel free to improve the theme"; the validator "permits intent-preserving
redesigns." With no declared mode they can *reasonably disagree*. Every migration therefore declares a
**mode**, and both agents honour it:

- **`strict-fidelity`** — reproduce the look as closely as PBI allows; deviations are defects.
- **`intent-preserving`** *(default)* — faithful to intent + data; PBI-native improvements allowed.
- **`modernize`** — deliberately re-imagine in PBI idioms; fidelity graded on intent, not pixels.

## Secrets policy

- Nothing secret ever lands in `migration-spec.json` (it is the **shareable** artifact) — not a PAT,
  not a session token, not a signed image URL (itself a bearer credential). Only *intrinsic* workbook
  provenance (derived-from, path, revision) belongs in the spec.
- Runtime capture config (server URL, site content-URL, dashboard→view-LUID map, secret **names**)
  lives in a **local, git-ignored** capture config / the reference manifest — never the spec.
- Credentials are read by the **deterministic capture script directly** from env vars or a git-ignored
  `.env.local` (already ignored) — never passed through agent prompts, CLI args, URLs, or logs. Hold the
  short-lived `X-Tableau-Auth` token in memory only and sign out in `finally`. Use a least-privilege,
  POC-specific PAT and revoke it afterward. Tableau forbids concurrent sessions on one PAT — serialize.

## Corrected pipeline ordering

```
parse + triage
        │
        ├───────────────┐                (reference acquisition has NO TMDL dependency)
        ▼               ▼
 reference-acquire   pbi-semantic-builder        ← run in parallel
   (producer)              │
        │  bundle + manifest│  model
        └────────┬─────────┘
                 ▼
         pbi-report-builder     ← receives spec + model + reference bundle FROM ITS PLANNING STEP
                 │                 (fail closed if no bundle and not structural-only)
                 ▼
         pbi-migration-validator ← receives the SAME immutable bundle; does NOT capture it itself
```

Key changes from the previous flow: acquisition moves **before report planning** (planning already
decides page splits, chart types, layout, colour — not just field binding); the **builder gets a formal
`Inputs you require` contract** for the reference (today only the validator has one); and **capture is
removed from the validator's responsibilities** — it consumes an immutable artifact, it does not
produce one.

## Governance (source-data safety)

A reference screenshot is a picture of the source dashboard's data. **In this repo the sources are
public Tableau Public workbooks, so reference images are committed** as showcase material (kept
reasonably sized — downscale big infographic captures). The caution below applies when you **fork the
toolkit to migrate real customer dashboards**:

- In a customer fork, add `**/reference/` back to `.gitignore` so customer screenshots stay
  local; commit only curated, customer-agnostic before/after images.
- Never embed image bytes (base64) in the shareable spec — paths/metadata only.
- The capture bundle's scratch (`reference/_thumbnails/`, `reference/manifest.json`) is git-ignored
  here regardless.
- Confirm that sending screenshots to a configured vision model is permitted before doing so; protect
  any CI reference artifacts with access controls + short retention.

## Enterprise traps checklist (for the Server/Cloud providers)

> **Several of these are already solved** — in `scripts/capture_tableau_oracle.py`, which talks to the
> same REST endpoints against a live site. Reuse that code; do not reimplement it. Items below are
> marked ✅ where it ships a working answer.

- **Negotiate the REST API version** (`/api/<v>/serverinfo`) — don't hardcode; `vizWidth/vizHeight`
  needs newer versions.
- ✅ **429s / retry-backoff** — `capture_tableau_oracle.py` classifies transient (gateway 5xx, 429,
  connection reset) vs session-lost (`401002`, re-authenticates) vs credential
  (`FederatedDataSourceException`, never retried), with exponential backoff + full jitter honouring
  `Retry-After`, and a retry budget. **Server-side image caching** serving a stale render after a
  recent edit is still unaddressed.
- ✅ **Don't silently degrade** — the same script records `reauths`, `retries` and `retry_reasons` per
  view, on the stated principle that a capture which silently healed itself is indistinguishable from a
  clean one. Detecting *disabled image export / missing Read+download permissions* specifically is
  still open.
- **Record the PAT principal** — RLS can materially change what the reference shows.
- **Pin** workbook revision + extract-refresh time + `.twbx` SHA-256 so you never compare different data
  snapshots.
- **Normalize** viewport/device layout, locale, timezone, fonts, DPI to the dashboard's **declared
  size** (the parser has it) or you get false "proportion" discrepancies from capture geometry alone.
- Treat dashboard **extensions / web objects / maps** as provider capability checks.
- ⚠️ `.twbx` result-cache `tmn:` columns decoding to `None` is not characterised yet and needs a
  fixture before this trap can be classified as decoder bug vs source limitation.
- A reference is only valid for the source it was captured from — **re-capture if the source changed**.

## Implementation status

- ✅ Governance gitignore; the public Playwright capture technique (documented in
  `pbi-migration-validator.agent.md` Gotchas: `domcontentloaded` + explicit timeouts, dismiss OneTrust,
  fixed viewport, full-page).
- ⚠️ `scripts/capture_tableau_reference.py`: public-Playwright + embedded-thumbnail + manual providers,
  manifest writing, fail-closed default, `structural-only` flag. The Server-REST and
  authenticated-browser providers are **not wired here** and raise a clear error — but note the
  Server-REST **transport is already implemented and live-tested** in
  [`scripts/capture_tableau_oracle.py`](../scripts/capture_tableau_oracle.py) `--images`; only this
  provider's contract (provenance manifest + state-pinning) is outstanding (#194).
- ❌ Multi-state capture, numeric-oracle export, API-version negotiation, and the agent-file contract
  edits (builder input contract, orchestrator step-reorder, migration-mode declaration) are the next
  increments.

---
*Design credit: consensus of a two-model architecture review (Claude opus-4.8, GPT-5.6-sol),
2026-07-19. Gemini was attempted and returned empty output.*
