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

A migration's reference lives at `migrations/<slug>/reference/` (git-ignored — see *Governance*) and is
described by a `manifest.json` so consumers never treat a 200px thumbnail and a full-res Server render
as interchangeable ground truth.

```jsonc
// migrations/<slug>/reference/manifest.json   (⚠️ schema implemented; some fields still TODO)
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
- **Immutable + hashed.** The producer writes each image + its SHA-256; **neither consumer may
  regenerate, crop, or annotate it** (a builder-curated original silently destroys validator
  independence). Per-worksheet crops, if needed, are produced by the producer, not the builder.

## Providers — resolve by *fitness*, not availability

The most important correction from the review: **REST is not automatically "highest fidelity."** A
Server/Cloud render is fidelity to *the published view, for that PAT user, at that moment* — which can
diverge from the handed-over `.twbx` via a newer published revision, an extract refresh, row-level
security, or personalized custom views. So there is **no global precedence ladder**; a resolver picks
the best provider *for the requested purpose* and records which it used and why.

| Provider | Typical role | Status |
|---|---|---|
| **Server/Cloud REST** (`/views/{id}/image?resolution=high`, `?vf_<field>=<value>` for state) | Canonical when the published view *is* the source and revision/state can be pinned | ❌ specified-only (no Server to test against) |
| **Authenticated browser** (Playwright w/ session) | States/actions/extensions REST can't reproduce | ❌ specified-only |
| **Public Playwright** | Tableau **Public** only, after capture QA | ⚠️ works (this repo's demos); hardening TODO |
| **Guided manual export from the exact `.twbx`** (Tableau Desktop/Reader) | Extract-only workbooks with no live Server view — can be *validation-grade* | ❌ specified-only (guided prompts) |
| **Embedded `.twbx` thumbnail** | Layout/style *hint only* | ⚠️ extractor implemented; only ~4% of workbooks carry them |
| **User-supplied screenshots** | Always-available floor; must be *guided* (exact filenames, reset state, viewport) | ⚠️ folder convention only |

### Default = fail **closed**

If no provider can produce a reference, the pipeline **blocks before report *planning* and asks for a
source** — it does **not** "proceed with a warning" (a buried warning recreates the exact
build-blind bug this design fixes). The only escape hatch is an explicit, user-acknowledged
**`structural-only` mode** that (a) may still build the semantic model + a provisional report, but
(b) **cannot claim visual fidelity** and (c) **cannot receive normal migration sign-off** — the
validator is told up front that gestalt grading is impossible. In non-interactive/CI runs, fail with an
actionable "missing reference" manifest instead of hanging on input.

**Configured-but-auth-failed ≠ not-configured.** If `TABLEAU_SERVER_*` is set but the PAT is dead/expired,
**halt with a specific credential error** — never silently fall through to public scraping (there is
usually no public URL for a Server workbook).

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

## Governance (customer-data safety)

A reference screenshot is a **picture of (potentially customer) data** — as sensitive as the `.twbx`
already git-ignored, and can be large (a 19 MB IronViz full-scroll capture).

- ✅ `migrations/*/reference/*.png|jpg` is git-ignored (kept local by default). Curated,
  customer-agnostic before/after images for the showcase go under `docs/showcase/` instead.
- Never embed image bytes (base64) in the shareable spec — paths/metadata only.
- Confirm that sending screenshots to a configured vision model is permitted before doing so; protect
  any CI reference artifacts with access controls + short retention.

## Enterprise traps checklist (for the Server/Cloud providers)

- **Negotiate the REST API version** (`/api/<v>/serverinfo`) — don't hardcode; `vizWidth/vizHeight`
  needs newer versions.
- **Server-side image caching** can serve a stale render after a recent edit; **429s** and Tableau
  Cloud's ~20 concurrent long-running-export limit need retry/backoff.
- **Detect disabled image export / missing Read+download permissions** and surface it, don't silently
  degrade.
- **Record the PAT principal** — RLS can materially change what the reference shows.
- **Pin** workbook revision + extract-refresh time + `.twbx` SHA-256 so you never compare different data
  snapshots.
- **Normalize** viewport/device layout, locale, timezone, fonts, DPI to the dashboard's **declared
  size** (the parser has it) or you get false "proportion" discrepancies from capture geometry alone.
- Treat dashboard **extensions / web objects / maps** as provider capability checks.
- A reference is only valid for the source it was captured from — **re-capture if the source changed**.

## Implementation status

- ✅ Governance gitignore; the public Playwright capture technique (documented in
  `pbi-migration-validator.agent.md` Gotchas: `domcontentloaded` + explicit timeouts, dismiss OneTrust,
  fixed viewport, full-page).
- ⚠️ `scripts/capture_tableau_reference.py`: public-Playwright + embedded-thumbnail + manual providers,
  manifest writing, fail-closed default, `structural-only` flag; Server-REST + authenticated-browser
  providers are **stubs** that raise a clear "not implemented — no Server available to test against"
  error rather than pretending to work.
- ❌ Multi-state capture, numeric-oracle export, API-version negotiation, and the agent-file contract
  edits (builder input contract, orchestrator step-reorder, migration-mode declaration) are the next
  increments.

---
*Design credit: consensus of a two-model architecture review (Claude opus-4.8, GPT-5.6-sol),
2026-07-19. Gemini was attempted and returned empty output.*
