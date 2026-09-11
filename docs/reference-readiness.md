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
| 1 | `FINDINGS` | a page is blind, its evidence is unusable/unattributable/stale, it was dropped with no engine explanation, its grade is below the required bar, a workbook shipped no report, **or a package's required roles / identity claims do not hold** (#562 S2) |
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
| `manual` | `layout_grade`, `text_readable`, **`validation_grade`** | whatever the manifest's `view_type` declares; unknown if it declares none |
| `server_rest` | *nothing* (not wired) | — |
| anything else | *nothing* | — |

⚠️ **`manual` is the only route to validation grade**, via `capture_tableau_reference.py
--manual-validation-grade`, which logs a warning naming what it did **not** verify. That route was
also *unwalkable* until round 2: `collect_manual` globs `tableau-*.png` and names each record from the
file stem, so every name carried a `tableau-` prefix and matched nothing. The prefix is stripped now,
so a file dropped as `tableau-<object>.png` resolves. The ceiling note in the output names each
provider's ceiling and this route, rather than merely saying validation grade is rare.

⚠️ **Grade does NOT widen scope, and the route stayed unwalkable for a second reason until #519.**
Round 3 removed the grade⇒kind promotion (`reference_evidence.MANUAL_KIND_HINT`), so a `manual` record
satisfies a page only when the manifest entry *declares* `view_type`. Nothing wrote one: measured
2026-09-04, a correctly named, sha256-attributed, `--manual-validation-grade` screenshot still came
back `UNVERIFIABLE - name only; scope unknown cannot satisfy a dashboard page`, and the hint asking
for `view_type` named a field no flag could produce. `capture_tableau_reference.py` now DERIVES it
from `migration-spec.json` — the name join is `object_identity.normalize`'s (whitespace-collapsed,
casefolded, **never slugified**), a name claimed by two kinds is dropped rather than guessed, and
`--manual-object-type` is the explicit fallback when the filename cannot carry the object's name.

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

## Package integrity, before any evidence is read (issue #562, slice S1)

A target that declares a package boundary — a regular, non-reparse `package-manifest.json` at its root
— is verified against **its own manifest** before `_collect_evidence`, before source resolution and
before any legacy or ancestor rescue path. The invariant is narrow and complete: the manifest is
strict readable JSON, and `contents.files` describes **exactly** every regular file in the package and
its SHA-256 bytes. Missing, malformed, duplicate-keyed, non-finite, unsafe or unassessable input is
non-clean; only exact namespace-and-hash equality is clean. Anything else is `CANNOT_ESTABLISH`,
**exit 3** — a package whose composition nobody can describe cannot attribute the renders inside it,
and source resolution must not be able to rescue it by finding an asset the manifest never accounted
for. The rules live in `scripts/package_filesystem.py`; its module docstring is the detail.

Three boundaries are deliberate:

- **The classifier already refused a damaged boundary** (`bundle_corpus.classify_target`), and this
  slice does not restate it. Two guards that can both answer "refuse" for the same target are one
  guard too many: whichever reason reaches the operator first is the one they act on, and the other
  rots. A missing marker still reports `package_marker_missing`, not an integrity code.
- **It judges bytes, not meaning.** Roles (`artifacts.*`), workbook identity/LUID, oracle semantics,
  verified source resolution (#558) and the post-dispatch working lifecycle are separate invariants
  with separate evidence. A package that omits a role, or whose `workbook_identity` contradicts its
  provenance, is S1-**clean** and is refused (or not) further along.
- **The manifest is unsigned and excludes itself**, so this proves internal consistency: it detects
  accidental damage, a partial copy, a confused composition and an edit made without re-packaging. It
  does **not** detect an adversary who rewrites a file and its manifest entry together — that needs an
  anchor outside the package, which this slice does not invent. A second residual is stated rather
  than papered over: between the `lstat` that proves an entry regular and the `open` that reads it,
  the entry can be replaced; closing that needs non-portable handle-level verification the threat
  model above does not require.

⚠️ **At entry, a package is byte-exact — including `fabric/**`.** That tree becomes legitimately
mutable once an agent has been dispatched (see [`migration-phases.md`](migration-phases.md) phase 2),
and reading that backwards into the entry gate would let every half-finished unit look pristine.

⚠️ **A key must be canonical on EVERY host, not merely legal on the one reading it.** Alongside the
traversal, separator and device rules, a declared key may not hold `<`, `>`, `:`, `"`, `|`, `?` or
`*`. POSIX accepts all seven in a filename, which is the reason rather than an argument against it: a
key holding one describes a package that cannot be unpacked on Windows at all, `?`/`*` make one
declaration match many files wherever they are expanded, `:` is a drive qualifier or an NTFS
alternate data stream, and `<`/`>`/`|`/`"` are how a path ends up interpreted rather than opened.
Ordinary package punctuation — brackets, parentheses, ampersands, commas, apostrophes, `%`, `#`, `@`
— stays usable, and a control asserts it does.

### The structured verdict: `package_integrity`

The JSON verdict carries a **list** of per-target blocks, always present, one for every target whose
package integrity was assessed — clean or not — each with the target's `ordinal`, the same safe
`unit` label the rest of the report prints, and the typed `findings`/`unassessable` rows (stable
codes, package-relative paths, and bare ordinals for an unsafe key). It is empty for an ordinary
target and for an unsafe root, so "not assessed" and "assessed and correct" are distinguishable
rather than sharing one absent key.

⚠️ The list shape is a **review correction, not a preference**. The first version wrote a single
block only on refusal, and `_merge_scans` starts from `dict(reports[0])`: scanning a clean target and
a damaged one in the same invocation inherited the clean field and dropped the damaged target's
evidence entirely, while the merged status stayed `CANNOT_ESTABLISH`. A verdict that is right with
its evidence missing is the quietest failure this gate has. Blocks are now concatenated with the
ordinal rewritten to the target's position, and both the single-target and merged shapes are
deterministic.

Direct tests are `tests/test_package_filesystem.py`; `tests/mutation_package_filesystem.py` proves
each anchor asserts the **named** guard rather than any fail-closed refusal — several mutations leave
the package non-clean for a different reason, which is exactly what a "not clean" assertion would
have missed.

---

## Required roles and cross-artifact identity (issue #562, slice S2)

Intact bytes are not a migration unit. A package can hash perfectly while declaring no source role,
carrying a Tableau LUID that contradicts its own provenance, shipping a render no record accounts
for, or naming a published datasource that no supplied package provides. So after S1 and **still
before any evidence is collected or any page derived**, every package the invocation names is
verified by `scripts/package_role_identity.py`.

It asks one question: does this package carry exactly the roles its **kind** and **topology** require,
and do the identity claims those roles make agree? Each role is `resolved`, an **earned**
`not_applicable`, or one of `missing` / `ambiguous` / `mismatch`. Only the first two pass.

**An earlier S1 observation is not a reusable clearance.** S2 re-runs the existing no-follow S1
verifier at its entry seam, even when the caller supplies `VerifiedPackage`. A changed, missing or
reparse-replaced package is a typed block, not a traceback. This is a fresh entry check, not a
transactional filesystem snapshot or a second revision/digest registry.

**Identity JSON is strict at every read.** Duplicate keys, non-finite/overflowing numbers and
incorrect container/scalar types block. Invalid collection rows are not filtered away: every declared
published dependency keeps a result, including duplicate, malformed and identity-less rows.

The spec's `data_sources` field is **required and list-valued**. The spec schema permits an explicit
empty list for a no-source workbook; missing, null or wrongly typed collections instead block with
`published_dependency_invalid`. A datasource row is non-published only when its `published_datasource`
key is **absent**. An explicitly present null or malformed value keeps a blocking dependency result
with that same code, before reference assessment; it cannot silently become `owned_model`.

| topology | how it is decided | model role | evidence roles |
|---|---|---|---|
| `owned_model` | a workbook whose spec declares no published datasource | exactly 1 `.SemanticModel` | reference and/or oracle, at least one resolved |
| `standalone_datasource` | a datasource no cohort consumer depends on | exactly 1 `.SemanticModel` | earned `not_applicable` |
| `published_provider` | a datasource a cohort consumer resolves to | exactly 1 `.SemanticModel` | earned `not_applicable` |
| `published_consumer` | a workbook whose spec declares a published datasource | **0 owned models** — it reuses the provider's | reference and/or oracle |

Kind comes from the engine's own `report.json`, never from the filesystem: a datasource package may
legitimately emit a self-service `.Report`, and that does not make it a workbook.
Datasource handover/reference/oracle roles earn `not_applicable` only when both the declarations and
the walked file set establish absence. Inapplicable content that is present is `inapplicable_role_present`.

⚠️ **A role is a DECLARATION the bytes confirm, never a discovery.** Deleting `artifacts.asset` while
the file stays in `assets/` is `missing` — the file is not rediscovered by scanning the directory, by
reading the handover slice's `source_id`, or by matching a display name. That rediscovery is the
fail-open this slice closes: measured on the audited master, exactly that package returned `READY
4/4`, exit 0.

⚠️ **Identity is hierarchical, and a weaker axis never replaces an available stronger one.** The
source SHA-256 (S1-verified) joins asset → provenance row → spec filename. The Tableau LUID, when one
exists, must agree across provenance, the harvester's `<luid>_<name>` filename prefix and every
oracle view. The two LUID namespaces are **typed**: a datasource LUID in a workbook's provenance is a
category error, not a spelling difference. A published consumer resolves to its provider by
datasource LUID first and by the exact `<site>/<name>` published key only when a LUID is genuinely
unavailable on both sides; `bound_datasource`, `published_ds_name`, folder stems and captions are
diagnostics and admit nothing. A matching LUID does not erase a conflicting published key. An exact
key cannot admit a LUID-bearing provider when the consumer has no LUID.

⚠️ **A cohort, because a consumer cannot prove its provider alone.** The verifier takes the whole
invocation, so `check_reference_readiness.py <provider-package> <consumer-package>` — still one
operator command — is what closes a shared-datasource pair. A consumer supplied on its own is
BLOCKED, because "I cannot see a provider" and "there is no provider" are the same answer from one
package.

Every provider must first pass its **own S2 roles**. A consumer then reads its walked
`definition.pbir` strictly, cross-checks `model_binding`, and compares the complete normalized
binding with that provider's **declared, resolved model role**. A matching directory basename or a
model discovered in a blocked provider is insufficient.

**Evidence paths cannot reopen discovery.** A reference image or oracle leg path must be canonical
POSIX relative to its evidence directory, and its resulting package-relative key must exactly name a
walked file under that role. Absolute paths, backslashes, dot segments, traversal, aliases and
missing files block. The renderer receives only the walk-produced `Path` with the assessed record;
package invocations do not reread evidence manifests or honor external `--reference`/`--oracle`
overrides. Ordinary bundle handling is unchanged.

**Earned limitations** do not convert a role state; they record why one is `not_applicable`:

- `local_source_no_server_luid` — a genuinely local `.twb`/`.tds`: SHA, filename and spec agree and
  there is no server LUID to agree with;
- `brief_policy_not_parsed` — the packaged `migration-brief.md` carries no strict `+++` TOML
  frontmatter, so only its presence and its bytes are established here. S2 checks brief **identity**
  (`unit`, and `scope` against the topology), plus whole-message privacy containment: reading policy out of
  free-form Markdown would make wording into a gate. The typed policy object is a `START_READY`
  prerequisite, not something inferred.

`package_unit.py --brief` accepts **exactly one selected unit**; package a batch with one invocation
and one brief per unit rather than broadcasting one identity. Unit/scope are checked before assembly.
The complete brief is checked with the existing host-location and credential containment functions,
and unsafe text is refused without copying, redacting or echoing it. Only the bytes that passed
validation are copied, preserving the source file and its original line endings.

**Failing S2 is `FINDINGS`, exit 1 — not `CANNOT_ESTABLISH`.** S1 refuses because the package cannot
be described at all; here it describes itself perfectly well and what it describes is wrong, which is
a defect an operator fixes. Either way no evidence is collected and it is not a pass.

The JSON verdict carries `role_identity`: the same list-of-blocks shape as `package_integrity`, one
per assessed package, each with the target `ordinal`, the safe `unit` label, the per-role states, the
source identity, the resolved dependencies and the stable blocker codes. It returns **no source
`Path`** and performs no source search. Its internal result retains the fresh, root-bound S1
observation and exposes `source_handoff()` for the next slice, without reopening anything.

Direct tests are `tests/test_package_role_identity.py`, which asserts a named **role and state** for
every control rather than a bare "not clean"; the producer half is in `tests/test_package_unit.py`
and `tests/test_package_unit_gates.py`. The correction controls are in
`tests/test_package_role_identity_reproductions.py`, `tests/test_package_unit_reproductions.py` and
`tests/test_check_reference_readiness.py`, including both `[clean, blocked]` and `[blocked, clean]`
target orders and the exact structured S2 finding.

## Package-local source return (issue #558)

✅ After no-follow classification, S1 exact bytes and S2 cohort role/identity acceptance,
`scripts/package_source.py` projects **only** the declared asset role. The input carries S2's own
root, unit, kind, package-relative `PurePosixPath` and SHA-256. The projector performs no discovery,
JSON/provenance/handover parsing, filename matching, hashing, existence checks, registry lookup,
`resolve()` or ancestor traversal. It joins the verified relative role lexically to the bound root.

`check_reference_readiness.py` is the sole production consumer. It projects the source **before**
source parsing, report discovery or reference/oracle grading. Earlier refusals prevent those helpers
from running: S1 retains `CANNOT_ESTABLISH` and its stable codes; S2 retains `FINDINGS` and its
role/provider blockers. A missing or internally inconsistent ready handoff is
`source_handoff_invalid`, never a reason to search for another file.

The S2 result also carries the revision status derived from its already-read provenance by the
existing `reference_evidence.revision_status` function. Reference assessment therefore keeps its
existing revision rules without rehashing the source or reopening provenance to build `UnitIdentity`.
This is not a new evidence grade or a final `START_READY` aggregation.

- A workbook returns its own `.twb`/`.twbx`; redacting diagnostic `handover.workbook.source_id`
  cannot erase that source.
- A datasource returns its own `.tds`/`.tdsx` **before** reference coverage earns
  `NOT_APPLICABLE` from the engine classification.
- A provider/consumer invocation returns **two local sources**: the provider datasource and the
  consumer workbook. A shared Power BI model never substitutes the provider's Tableau source for
  the consumer's page expectations.
- JSON adds `package_source`, one ordinal-addressed result per projected package. Both this block
  and `units[].source` expose only the package-relative role, never the bound absolute source path.

**`--source` is only for ordinary non-package targets.** If any original target is a package or
damaged/package-shaped, the CLI refuses with
`--source is supported only for ordinary non-package targets`, before inspecting the supplied
source. A mixed ordinary/package invocation is refused too. Ordinary explicit-source and ancestor
compatibility remain unchanged.

Direct controls: `tests/test_package_source.py` arms forbidden I/O/search/JSON/hash/registry helpers
and pins all four source extensions and refusal states. `tests/test_check_reference_readiness.py`
pins the historical page denominator, provider/consumer ownership, CLI refusal order, earlier-gate
stops and ordinary compatibility.

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
