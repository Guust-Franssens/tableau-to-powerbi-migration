# Reference readiness — the entry gate (issue #421)

`scripts/check_reference_readiness.py` is the only **entry** gate in this toolkit. Every other gate
answers whether work is *done*; this one answers whether there is enough visual evidence to **start**.

```
python scripts/check_reference_readiness.py <bundle> [--require-validation-grade] [--json <file>]
```

Run it **before dispatching any builder** — it is step 1 of `docs/INDEX.md`'s per-unit route.

---

## Why it exists

A customer audit (SES) found a shipped `columnChart` that should have been a `lineChart`, stacking
five airlines' 95/92/88/97/90 % into one ~462 % bar. Their own conclusion is the argument:

> *"This was only catchable because a Tableau reference image for that page happens to exist. The
> same class of bug on 'Availability Summary by Tail' would be completely invisible — there is
> nothing to compare against."*

So wherever a capture gap exists, an equivalent fidelity bug is **structurally unfalsifiable**, not
merely unverified. The gate makes that gap visible up front, per page, with its grade.

## Three questions, in order

1. **Completeness** — does the emitted report have a page for every source object the engine's own
   rule says it should? A missing page is a *conversion* gap the agent must know about before it
   starts, not a fidelity gap discovered later.
2. **Evidence** — is there a usable reference render that is provably OF this source object, in THIS
   workbook, at THIS revision?
3. **Grade** — `validation-grade`, `layout/text only`, or unknown?

## Exit codes

The 0/1/2/3 shape is `check_connection_fidelity.py:160-163`'s, adopted rather than invented. Its
comment at `:165` records issue #366 — *"'nothing to compare here' and 'this unit could not be
examined' printed identically, and nine unexamined workbooks read as a clean bill of health"* — which
is precisely the failure this gate exists to prevent.

| exit | status | meaning |
|---|---|---|
| 0 | `READY` / `NOT_APPLICABLE` | every expected page has usable, attributable evidence — or the unit is a datasource-only migration with no Tableau views at all |
| 1 | `FINDINGS` | a page is blind, its evidence is unusable/unattributable/stale, it was dropped with no engine explanation, its grade is below the required bar, or a workbook shipped no report |
| 2 | — | usage error (argparse). A missing path never produces a verdict |
| 3 | `CANNOT_ESTABLISH` | the expectation or the page mapping could not be derived, so the gate has no opinion — **do not read that as a pass** |

Findings outrank cannot-establish, and both counts always print so neither hides the other.

⚠️ **There is deliberately NO `--warn-only`.** Every sibling gate has one, and this gate had one until
round-1 review of PR #428 measured it returning exit **0** on a bundle whose own output said
*"CANNOT_ESTABLISH is NOT a pass"*. An entry gate that can be asked to say yes is not an entry gate,
and a dispatch decision reading that exit code would launch an agent to build blind — the exact
outcome this exists to prevent, delivered by a flag. Advisory consumers read `--json`, whose `status`
always carries the true verdict.

---

## Fail closed — the one design rule

`blind`, `unverifiable` and `insufficient-grade` are all distinct from `ready`, and **none exits 0**.

The mechanism is that **unverified evidence is unrepresentable**. `Evidence` is only reachable through
`Evidence.build()`, which returns either a fully verified record or a `RejectedEvidence` that can never
be matched. Round-1 review found three separate fail-open paths — a zero-byte render, an empty
`capabilities` list, and evidence attributed to the wrong workbook — precisely because validity was
re-checked at three call sites instead of being a construction precondition.

Rejections are counted and **printed**, so a capture that does not count says why rather than vanishing.

### What `Evidence.build()` requires, and why

| precondition | what it replaced |
|---|---|
| **A structurally complete render.** The whole PNG chunk stream is walked — every length and CRC verified, a 13-byte IHDR required, IDAT and IEND required. SVG parses `width`/`height` or `viewBox`; a PDF has no cheap dimension read, so it is accepted on a `%PDF-` header plus a size floor. Both edges must clear `MIN_RENDER_EDGE` (64 px). | `Path.is_file()` — a **zero-byte** PNG reached `READY` (round 1). Then a **24-byte blob** did, because the parse read only the signature, the `IHDR` marker and 8 dimension bytes; Pillow rejects the same bytes as `Truncated File Read` (round 2) |
| **A match against the producer's own recorded facts** — `sha256`, `bytes`, `dimensions`. A recorded hash is *required*: both producers always write one, so its absence means a manifest nothing can confirm. | Measured on the real bundle: zeroing every manifest hash and setting dimensions to `1x1` still returned `READY 3/3` with **zero rejected records**, so a captured image could be swapped wholesale. The integrity data needed to catch it was already recorded and simply unread |
| **A grade capped by `PROVIDER_CEILING`**, derived from what the producer can physically capture. A claim above the ceiling is a rejection; an unrecognised provider has an **empty** ceiling. | Grade came from the self-reported capability list alone, so an `embedded_thumbnail` record — a 192×192 worksheet render — claimed `validation_grade`, reached `READY` under `--require-validation-grade`, and **suppressed the ceiling warning** |
| **Workbook identity**, carrying **both** LUID and name. Reference evidence uses `source_workbook_sha256`; oracle evidence uses a LUID when provenance is byte-confirmed, else the workbook name. | One synthetic `Overview` record made **two different** units report `2/2 READY`. Separately, a record carrying a LUID *discarded* its name, so removing source provenance made correctly-named records return `0/3 blind` |
| **Trusted provenance only.** A LUID counts only when the stamped input hash is this file **and** `origin.match == "sha256"`. | `stamp_tableau_provenance.py` writes `match: "name_only"` when local and server bytes differ and says figures **will not reproduce** — yet that LUID made oracle evidence ready. Repo provenance today: **26 `sha256`, 15 `name_only`, 6 unmatched**, so this is the common case |
| **Source revision.** A manifest whose `source_workbook_sha256` no longer matches the resolved source does not match that unit. | a **stale** capture is worse than a missing one, because it looks like evidence |

The 64 px floor is set below Tableau's 192×192 embedded thumbnails (`extract_twb_thumbnails.py`), which
are a genuine low-fidelity evidence route, so it rejects placeholders without rejecting real captures.

### Provider ceilings — and the one walkable route to validation grade

| provider | may claim | scope |
|---|---|---|
| `embedded_thumbnail` | `layout_grade` | worksheet — Tableau `<thumbnail>` blocks are per-worksheet renders |
| `public_playwright` | `layout_grade`, `text_readable` | dashboard — driven from the spec's dashboard list |
| `oracle_capture` | `layout_grade`, `text_readable` | PR #422's `view_type`; absent or `unknown` ⇒ cannot establish |
| `manual` | `layout_grade`, `text_readable`, **`validation_grade`** | unknown — *unless* the operator asserted validation grade, which is an explicit claim about this object |
| `server_rest` | *nothing* (not wired) | — |
| anything else | *nothing* | — |

⚠️ **`manual` is the only route to validation grade**, via `capture_tableau_reference.py
--manual-validation-grade`, which logs a warning naming what it did **not** verify. That route was
also *unwalkable* until round 2: `collect_manual` globs `tableau-*.png` and names each record from the
file stem, so every name carried a `tableau-` prefix and matched nothing. The prefix is stripped now,
so a file dropped as `tableau-<object>.png` resolves. The ceiling note in the output names each
provider's ceiling and this route, rather than merely saying validation grade is rare.

### The page mapping must be readable

An unreadable `page.json` is a **problem, not a page** — the first version fell back to the containing
directory's name, so forcing every read to fail still produced three pages and `READY`.

⚠️ `pages.json` is **required**, and must declare a list `pageOrder`. The cross-check originally ran
only when `pageOrder` *happened to be* a list, so absent, unreadable, wrong-shaped JSON or a non-list
`pageOrder` all reported no problem and every discovered `page.json` was trusted — measured, failing
**only** the `pages.json` reads still produced `READY 3/3`.

### Ambiguity is a refusal, not a resolution

One defect recurred at three successive layers, each time one object's excuse covering another's:

| round | layer | fix |
|---|---|---|
| — | **routing** | `viz_fidelity[]` instead of `pbip_warnings[]`, whose reason strings drop the object name |
| 1 | **matching** | key on `(kind, name)`, not `name` — a *worksheet* warning was excusing a missing *dashboard* |
| 2 | **normalization** | `_norm` collapsed case and repeated whitespace, so `Ops  Summary` and `Ops Summary` — which take **different** engine page ids — shared one key |

Rather than adding a fourth key component, the join changed shape:

- **Drop explanations match EXACTLY.** Both sides are engine/source artifacts and byte-exact:
  `viz_fidelity[].worksheet` is the IR's own object name, and `SourceObject.name` comes from the same
  workbook XML. There is no normalization to do.
- **Evidence names may still normalize**, because external providers spell them in ways this repo does
  not control — but only when unambiguous. More than one candidate is `AMBIGUOUS`, reported as
  `unverifiable`, because picking one would be a guess.
- **A normalized collision among the EXPECTED objects is `CANNOT_ESTABLISH`**, since it is
  unresolvable by construction: one evidence record would match both.

`NOT_APPLICABLE` is **earned** from the engine's own `report.json` (the unit is listed under
`datasources[]`, not `workbooks[]`) — never inferred from "I found no pages", and never from "some
semantic model exists". Round-1 review measured both of those granting a clean exit to a workbook
whose report generation had **failed**.

---

## Why it does not reuse `check_unit.expected_pages()`

It cannot, three ways, all measured. (`check_unit.py` is owned by another change and is untouched.)

- Its docstring says *"dashboards only, never worksheets"* (`check_unit.py:572`), but the engine emits
  a page per dashboard **and** per orphan worksheet (`twb_to_pbir.py:14040`). On the Meridian workbook
  — 0 dashboards, 3 worksheets — it expects **0** where the engine correctly emitted **3**.
- It reads `migration-spec.json` (`:285`), which **does not exist in an engine bundle**, so it returns
  `None`.
- Its consumer is then circular: `check_oracle_coverage:925` does
  `expected_pages(target) or actual_pages(target)`, grading the output against itself, so a page the
  engine dropped cannot be counted as missing evidence.

This gate derives its own expectation from the source workbook and **never** falls back to what was
built. No expectation means `CANNOT_ESTABLISH`.

## Candidates are not emitted pages

"dashboards + orphan worksheets" names the *candidates*. `twb_to_pbir.py` deliberately drops a page in
three further cases, each with a recorded warning:

| engine site | condition | warning |
|---|---|---|
| `:14529` | a dashboard whose zones yield no supported visuals | `"no supported visuals on this dashboard"` |
| `:14558` | an orphan worksheet with `visual_type == VT_UNSUPPORTED` | `"unsupported visual type"` |
| `:14562` | an orphan worksheet whose query state is incomplete | `"… no usable field bindings (skipped)"` |

A gate that simply diffs candidates against emitted pages therefore raises a completeness finding on
every **correct** bundle — the false-positive direction, and how a gate gets muted and stops
protecting anything. So drops are split into `dropped_explained` and `dropped_unexplained`; both are
counted, only the second is a finding.

### An explanation must match the dropped object, in kind as well as name

`pbip_warnings[]` is **not** the explanation channel: `_warn("dashboard", name, "no supported visuals
on this dashboard")` yields a reason string that does not contain the dashboard's name
(`twb_to_pbir.py:6428-6430`), so matching it would let one dashboard's excuse cover every dropped
dashboard. The structured `viz_fidelity[]` rows carry the name.

⚠️ Name alone is still not enough — that is the same defect one level down, and round-1 review measured
it: a *worksheet* warning for `Ops` excused a genuinely missing *dashboard* named `Ops`, and the unit
went `READY`. Explanations are keyed by **`(kind, normalized name)`**. The kind is recoverable because
`migrate_estate.py:1201-1204` writes dashboard-scope warnings with `visual_type` set to the *scope*
string `"dashboard"`, while a real worksheet row carries an actual visual type.

---

## Identity, not name slug

`check_unit.py:265` matches on `_slug(view_name)`. In Tableau a dashboard routinely shares its name
with its principal worksheet, so a worksheet render satisfies a dashboard page — the **normal** case,
and live today: `capture_tableau_reference.py:199` files `embedded_thumbnail` records (worksheet
renders — *"dashboards are not thumbnailed per se"*) under the manifest's `dashboards` key.

Two independent defences:

**1. Page identity is cryptographic.** The engine names pages `_sanitize("page-" + dashboard)` or
`_sanitize("page-ws-" + worksheet)`, appending an md5 of the **full prefixed string**
(`twb_to_pbir.py:748-761`). Verified against a real 2.339.0 bundle:

| source object | as worksheet | as dashboard |
|---|---|---|
| `Revenue by Region` | `page-ws-Revenuebb7d27f78` | `page-RevenuebyRe2b117987` |

⚠️ **Strong, not collision-free.** Only 8 md5 hex digits are kept, so `Collision030344` and
`Collision079370` both yield `page-ws-Collisioc5d9dc9d` (verified). One physical page must never
satisfy two expected pages, so a duplicate page id among the expected objects is `CANNOT_ESTABLISH`.

**2. Evidence carries a scope** that must match the page's kind:

| provider | scope | why |
|---|---|---|
| `embedded_thumbnail` | worksheet | Tableau `<thumbnail>` blocks are per-worksheet renders |
| `public_playwright` | dashboard | driven from the spec's dashboard list (`capture_tableau_reference.py:135`) |
| `manual` | **unknown** | `_manual_capabilities` says the tool cannot know "even that it is a screenshot of this dashboard" |
| oracle capture | PR #422's `view_type` | absent or `unknown` ⇒ cannot establish, never either type |

An entry's explicit `view_type`/`object_type` overrides the provider inference, so a manifest enriched
by PR #422's view-type join is honoured with no code change here.

## The page mapping must be readable

An unreadable `page.json` is a **problem, not a page**. The first version fell back to the containing
directory's name, so round-1 review measured forcing every read to fail and still getting three pages
and `READY`. `pages.json.pageOrder` is cross-checked for the same reason: it is the report's own
statement of which pages exist, and a disagreement means the join cannot be trusted.

## Grade ceiling — stated, not implied

`validation_grade` is today reachable **only** via `capture_tableau_reference.py
--manual-validation-grade`; even a `reference/` capture records `"state": {}` with a live TODO to pin
parameter defaults, and an oracle capture is default-view-state with no `?vf_` filter pinning. So in
practice nearly everything is layout/text grade.

The ceiling note prints unless **every** evidenced page is validation-grade — one good capture must not
silence the warning for the rest (round-1 finding 7). `--require-validation-grade` opts into treating
anything less as a finding, and the bar lands on the **page**, so every count agrees.

---

## Tests and mutation proof

- `scripts/reference_evidence.py` — the evidence layer, split out because it answers a different
  question from the gate: not "is this bundle ready" but "is this a picture I may believe, and of
  what".
- `tests/test_check_reference_readiness.py` — one test per review finding, each naming its round and
  number, and each paired with a **discriminating twin** so "correctly refused" cannot be confused
  with "broken".
- `tests/mutation_reference_readiness.py` — imports the shared `tests/mutation_harness.py` scoring and
  adds an *expected verdict* per mutation, so it is a gate rather than a report.

⚠️ **Every mutation names its ANCHOR, and that is the point of the file.** It previously ran each
mutation against the whole test file under `-x` and credited whichever test failed first, so two
unrelated mutations were both credited to `test_colliding_page_ids_cannot_be_attributed` simply
because it ran early — and the harness would have stayed green if their real anchors regressed while
an unrelated test failed first. Each entry now declares the node that must **CATCH** it run alone,
plus control nodes that must **SURVIVE** it run alone. 37 mutations, 73 anchor/control checks.

⚠️ Fixture rules, each because a review measured the fixtures themselves encoding the defect:

- renders are **real, parseable images** (the first version used an 8-byte PNG signature and asserted
  readiness);
- evidence **carries workbook identity** (without it, one record satisfied two units);
- evidence **carries the producer's recorded `sha256`/`bytes`/`dimensions`** (without them, a fixture
  could not notice that a swapped image still counted);
- the positive grade test uses the **producer's real shape** — it previously used `embedded_thumbnail`
  + `validation_grade`, an impossible combination, so it encoded the self-promotion bug as expected
  behaviour.
