# AGENTS.md — shared conventions for this repo's Copilot agents

This file is auto-loaded by GitHub Copilot CLI (and other agent runtimes) for every session in this
repository. It has two jobs: (1) tell you (or a fresh contributor) exactly which Copilot **plugins and
MCP servers** this toolkit needs so the repo is self-configuring, and (2) hold the **canonical copy of
the shared agent conventions**, which `scripts/sync_agent_conventions.py` generates into every
`.github/agents/*.agent.md`.

> **Why generated, not inherited:** a custom-agent **subagent** receives ONLY its own persona file —
> this file, `.github/copilot-instructions.md` and user-global instructions do **not** reach it
> (verified 2026-07-30 with a sentinel experiment; all four agents confirmed independently). There is
> no `include`/`extends` mechanism. So the conventions below are *duplicated* into each agent on
> purpose, and CI fails if a copy drifts. **Edit them here, then run
> `python scripts/sync_agent_conventions.py`** — never edit the copy inside an agent file.
>
> `--check` fails on three things, and the last two exist because consistency alone was not enough:
> **drift**, a persona **over the 30,000-char cap** (measured on the whole file — a body-only count
> read 98 % for a 30,132-char file), and a documented `<bundle>/…` path that **is not a real bundle
> directory** (`<bundle>/out/pbip/` was wrong in all five copies for weeks, and agreeing with itself
> was the only thing anyone checked). It reports **all three in one run, the path first**: a wrong path
> in `AGENTS.md` also makes every persona stale, so failing on drift first pointed at the symptom, and
> the obvious "fix" was to sync the wrong path into all four files. The scan covers the block **and
> each persona in full** — a wrong path in a persona's own `## Gotchas` used to be invisible — and
> **write mode exits non-zero too**, because that run has propagated the error, not merely proposed it.
> `--bundle <dir>` additionally resolves on disk the paths written as a **location**
> (`<bundle>/reports/`); the `{pbip,reports,…}` enumeration is vocabulary, so an estate with no
> flat-file extracts, and therefore no `data/`, is not failed for it.

> **VS Code users:** VS Code Copilot auto-loads `.github/copilot-instructions.md`, *not* this file.
> That pointer file duplicates only the session-start step below and defers everything else here, so
> the two cannot drift.

**Navigation:** use [`docs/INDEX.md`](docs/INDEX.md) as the tier-2 map before searching blindly.
Subagents are told in their generated preamble to read it as step 0; `scripts/check_navigation_index.py`
checks the index bidirectionally.

---

## Session start, do this first (before any other work)

```
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1 -Update
```

`-Update` repairs the npm bridge CLIs **only when they are below the correctness floor** — it is a
floor check, not a blind `@latest`, so at or above the floor it costs nothing.

**Why a floor:** `powerbi-report-author` **>= 0.1.4** is a *correctness* floor. Older builds returned
`errorCount: 0` for PBIR that Power BI Desktop cannot open (e.g. a `report.json` whose
`themeCollection` entries are missing `reportVersionAtImport`) — a stale CLI silently green-lights a
broken report.

⚠️ **`reportVersionAtImport` is location-dependent — and the two theme entries do not even fail the
same way.** Re-measured 2026-08-13 against 0.1.4, by mutating a scratch copy of
`examples/shipping-kpis/fabric/ShippingKPIs.Report`:

| mutation | what `validate` actually emits |
|---|---|
| remove from `baseTheme` | **`PBIR_SCHEMA_VALIDATION_ERROR` only** — *"/themeCollection/baseTheme must have required property 'reportVersionAtImport'"* (errorCount 1) |
| remove from `customTheme` | **both** `PBIR_THEME_VERSION_AT_IMPORT_MISSING` **and** `PBIR_SCHEMA_VALIDATION_ERROR` (errorCount 2) |
| add at the **top level** | `PBIR_SCHEMA_VALIDATION_ERROR` — *"/ must NOT have additional properties (property: "reportVersionAtImport")"* |

So the substance is: **required inside each `themeCollection` entry, forbidden at the top level** of
`report.json` — but do not attach the named code to both entries. The CLI raises
`PBIR_THEME_VERSION_AT_IMPORT_MISSING` from `validateCustomTheme` alone (its only emit site in
`dist/index.js`), so grepping for it after a `baseTheme` failure finds nothing and reads as "not our
problem". Ground truth, a committed deliverable:
`examples/shipping-kpis/fabric/ShippingKPIs.Report/definition/report.json` — top-level keys are
`$schema`, `themeCollection`, `resourcePackages`, `settings`, and *both* theme entries carry
`name`, `type`, `reportVersionAtImport`. Saying only "schema-required" is what put it at the top level
in a hand-written scaffold five days later; name the location every time.

**Above the known-good matrix is a WARN, not an error.** It means the version-specific Gotchas in
`.github/agents/` were verified against an older build and may be stale — re-verify the prose; never
"fix" it by downgrading.

**The timing rule is what makes this safe:**

| When | Run | Why |
|---|---|---|
| Session start (nothing in flight) | `preflight.ps1 -Update -CheckUpstream` | Safe; the CLI floor is a correctness floor, and this is the one moment upgrading is allowed |
| Migration start (orchestrator step 0) | `preflight.ps1` (plain) | Confirm READY without swapping tooling mid-flow — and without a network round trip on every migration |
| Mid-migration | **don't upgrade the installed tooling** | The variable is not the calendar but *work already validated by the current CLI*: swap the validator mid-build and earlier results are no longer covered by the same check. A version-*comparison* run into a fresh output dir is not an upgrade (engine section) and is not forbidden here. |

**`-CheckUpstream` answers a question the version matrix cannot.** Every other check compares an
installed version against a **hard-coded** number — that says "is what I have good enough", never
"has the world moved". `-CheckUpstream` asks npm for the latest `powerbi-report-author` /
`powerbi-desktop`, and asks GitHub for the deterministic engine's upstream `VERSION` (the plugin is
an unpacked marketplace copy with no `.git`, so there is no local SHA to compare — and a VERSION
comparison names the thing that actually changes behaviour). It is **opt-in**
(~3s of network) and **advisory** — it never upgrades and never fails the run, because being behind
is not an error; the timing rule above still decides *when* acting on it is safe.

Measured 2026-08-06, this gap bit twice in one day: the engine moved **2.60.0 → 2.72.0** unnoticed
(issues were nearly filed against behaviour it had already replaced, caught only by a manual
`git fetch`), and **Power BI Desktop auto-updated** and silently broke the bridge's exe discovery
while preflight still reported "Ready to migrate".

It cannot update the **skill bundles**: `copilot plugin update` hits a file lock while any Copilot
session is running. That lock is narrower than it looks, though — it blocks renaming the plugin
directory, not writing inside it — so a *content* refresh needs no restart:
`python scripts/sync_installed_skills.py`.

---

## Required Copilot setup (self-configuring dependencies)

The environment contract lives in **`scripts/preflight.ps1`**. It is the gate, not this prose: it
checks the required tools, plugins, MCP servers, Python dependencies, Desktop bridge assumptions and
version floors, prints the install or repair hint beside the failing item, and exits non-zero for any
critical miss.

```
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1
```

**If setup guidance is missing or too thin, improve the preflight hint rather than adding another
install recipe here.** An agent that never reads this section should still be stopped by the exit code
before it can migrate with a broken environment.

Keep only the judgement the gate cannot carry:

- **Timing rule (installed tooling):** `-Update` is for session start only, before any migration is in
  flight; at migration start run plain preflight; mid-migration, do not re-arm it. What is at risk is
  not the calendar position but *un-checkpointed work already validated by the current CLI* — swap the
  validator underneath it and earlier results are no longer covered by the same check. This governs
  *upgrading the installed CLIs*; running a second engine version into a fresh output dir to
  **compare** is a different, always-safe operation (see the engine timing rule below).
- **`powerbi-report-author >= 0.1.4` is a correctness floor, not hygiene.** Older builds returned
  `errorCount: 0` for PBIR that Power BI Desktop cannot open, so being below the floor silently
  green-lights broken reports. `-Update` repairs only below-floor npm bridge CLIs; it is not a blind
  `@latest`.
- **Above the known-good matrix is a WARN, not an error.** Do not downgrade to make the warning go
  away. Re-verify the version-specific gotchas, then continue.
- **Skill bundles can be stale even when repo-local `.github/skills/` is correct.** In this repo an
  installed plugin copy shadows the local bundle with the same name, so preflight blocks on `STALE in
  plugin`. Fix content drift immediately with `python scripts/sync_installed_skills.py`; a running
  Copilot session keeps the old in-memory copy, but new sessions and newly spawned subagents see the
  refreshed files. A real `copilot plugin update` still belongs between sessions because the plugin
  directory is file-locked for rename/swap while Copilot is running.
- **PBIR theme-version location is report-authoring knowledge, not setup.** The rule
  (`reportVersionAtImport` required inside each `themeCollection` entry and forbidden at the top
  level of `report.json`) lives in `powerbi-report-gotchas` so the report owner sees it when authoring
  PBIR.

---

## Monitoring delegated work (orchestrator discipline, not persona content)

This section is deliberately **outside** the synced block below — it is not something every persona
needs baked into its own context (three of the four are already at ~99% of their character cap), it
is what *whoever is delegating* — the top-level session, or `tableau-migrator` orchestrating
`pbi-semantic-builder`/`pbi-report-builder` — owes the work once it hands a task to a subagent.

**A subagent's own final summary is a claim, not evidence — verify it before repeating it.**
Measured 2026-08-02: a subagent's summary declared **"Sign-off ready: YES"** and never mentioned that
it had, minutes earlier, re-armed its own credential gate and cleared it unearned; only reading the
gate's own audit log (`probe-cleared` vs `manual-clear`, and `credential_gate.py verify`) surfaced the
violation. This is not a reason to distrust subagents generally — it is a reason to always have an
authoritative, non-narrative check available and to run it before passing a result along: an audit
log's `action` field, `verify`'s exit code, an artifact count, a checksum — never the summary prose
alone.

**An anomaly in elapsed time or tool-call count is a signal, not noise.** The same run above was
still on its first turn past 5000 seconds and 89 tool calls into a task that peers finished in
minutes — that alone was reason enough to stop and read what it had actually done, before it
finished and self-reported success. Don't wait for the final summary to investigate a run that looks
stuck or unusually long; the ground truth (audit log, artifact folder, raw session event log) is
readable mid-run, and reading it early is what caught the bypass, a misclassified `UNREACHABLE`, and
a real Desktop crash — all in one afternoon, all invisible in the eventual "done" message.

**When a summary and the ground truth disagree, ground truth wins, unconditionally.** Don't average
them, don't soften the finding to be polite about the subagent's framing — restate the verdict from
the evidence and say so plainly.

**Every fix in this batch passed CI and every one needed changes.** Measured 2026-08-09: five agents
fixed eleven issues in isolated `git worktree`s, all five PRs went green, and an independent
rubber-duck review — given **only the diff and the issue, never the author's rationale** — returned
`request changes` on **all five**. The shared shape was that each fix *moved* its failure boundary
rather than removing it: a data-loss fix that still collapsed extracts sharing a table name, a
monitoring fix whose new liveness signal re-opened the door its own regression test was written to
close, a parser fix that satisfied a synthetic fixture but not the real XML already committed to this
repo. So:

- **A green CI is the start of review, not the end of it.** CI proves the code does what its tests
  say; it cannot tell you the tests have a blind spot. One covering test here *structurally could
  not* observe the defect it was written for, because its fixture used distinct table names.
- **Review the diff without the author's explanation.** A reviewer handed the reasoning tends to
  ratify it. Every finding in that batch came from a *controlled experiment* — reversing input order,
  unsetting an environment variable, injecting a hash mismatch — not from reading the code.
- **Send the re-review to the reviewer who found the defect.** They already have the reproduction
  built; a fresh reviewer has to rediscover it, and usually doesn't.
- **Tell reviewers plainly that a clean bill of health is a legitimate outcome**, or they invent
  findings to look thorough. Equally, tell authors to **push back with evidence** rather than comply:
  one "already fixed, no change needed" verdict was correct, and survived deliberately sceptical
  re-checking.
- **Require `Fixes #N` in the commit message, not merely "reference the issue".** Four issues stayed
  open after being fixed, reviewed, re-reviewed and merged, because the commits said `(#46)` — a
  reference, not a GitHub closing keyword. The PR with the *best* per-issue write-up was the one that
  did not close anything.

**A host crash with subagents in flight leaves no summary at all — do file-level forensics before
re-dispatching.** Everything above assumes a subagent eventually reports back; a crashed host never
gets that far. Real incident, 2026-08-19: the CLI hung and was restarted with 3 fix-subagents still
running. Afterwards `list_agents` returned **ZERO** and no agent produced a final report. What each
had actually accomplished could only be established by reading files on disk — and the outcome was
not predictable from any agent's last known status:

| subagent | last known status | what was actually on disk |
|---|---|---|
| ACMU | in progress | both fixes complete, already confirmed live in Desktop |
| Active Work Order | in progress | 4 new, valid, non-orphaned visual files — further along than its status suggested |
| Aircraft Installs | in progress | zero file changes — nothing lost, nothing gained |

Three agents, three genuinely different outcomes, identical reported status. Any blanket assumption —
"assume lost", "assume complete", "assume proportional to elapsed time" — is wrong for at least one of
them. Re-dispatching blindly would have redone or overwritten ACMU's already-verified fixes.

- **In-flight subagent progress is at risk until it lands on disk.** Treat a lost agent's work as
  **UNKNOWN** — never as lost, never as complete — until you have checked.
- **After a host restart with agents in flight, do file-level forensics BEFORE re-dispatching:**
  `git status` / `git diff --stat` in the target worktree, plus file modification times against the
  crash time. Agent status does not answer this question; the files do.
- **Prefer briefs that land work incrementally over ones that hold everything until a final write.**
  A brief that commits/saves as it goes turns a crash into a truncation of progress; a brief that
  buffers everything for one final write turns the same crash into a total loss.
- **Re-dispatch only after establishing current state.** Re-running a completed fix can overwrite a
  verified-good artifact — ACMU's fixes above were already confirmed live in Desktop; blindly
  re-dispatching that unit would have redone (and risked corrupting) work that was already done.

## Desktop concurrency budget: RAM, not addressability

The shared cleanup rule below is still true: concurrent Power BI Desktop instances are addressable
because the bridge and AS-port lookup are PID-scoped. The missing constraint is memory. ⚠️ Inferred
from a 2026-08-19 field incident, not a controlled reproduction: Desktop crashed with
`Microsoft.Mashup.Host.Document.PlatformDependentOptions` while 4–5 instances were open and the
machine had about 3.1 GB free of 31.7 GB. Deleting the model's 313 MB `.pbi/cache.abf` and rebuilding
it did not fix the crash, which argues against treating this signature as simple cache-file
corruption; each instance's resident `msmdsrv` model is the RAM-pressure hypothesis. Confirming that
mechanism would require a controlled reproduction varying free RAM and instance count. Until then,
keep large-model concurrency low, check free RAM before opening another instance, and close instances
as soon as their handoff is complete.

**There is a SECOND concurrency budget, and it is a different resource entirely — the agent host.**
Do not conflate the two: the one above is machine RAM consumed by Desktop's `msmdsrv` processes; this
one is the **V8 heap of `copilot.exe` itself**, and it is reached with no Power BI Desktop running at
all. ⚠️ **Observed 2026-08-20, three times in one session** — with **six concurrent `opus-5`
subagents**, the CLI host died and wrote a crash dump into the repo root
(`report.<yyyymmdd>.<hhmmss>.<pid>.0.001.json`) naming the cause:

```json
{ "event": "Allocation failed - JavaScript heap out of memory", "trigger": "OOMError" }
```

**Marked observed rather than measured, deliberately.** The dump was read at the time but not
retained, so this is not reproducible from anything committed here. If it happens again, **keep the
dump** (it is gitignored, not auto-deleted) and upgrade this claim.

Equally, be careful what the number means. **Six failed. Four was run repeatedly the same night
without incident — which is not the same as four being safe**, and no one has bisected it. Treat
"keep the wave small" as the rule and any specific ceiling as unproven.

Two consequences, both of which cost real work that night:

- ⚠️ **Advice to "dispatch the whole wave at once" — common in delegation guidance, including the
  user-level instruction files some runtimes load — optimises coordination cost and is silent about
  host memory.** It is not in this document, and this document does not endorse it: cap the wave.
- **A crash takes every subagent's UNPUSHED work.** Committing is not enough — one agent came one
  crash away from losing four good commits it had never pushed. Brief agents to **`git push`
  incrementally**, and read the crash dump first after a restart: it names the trigger and timestamps
  the crash, which is the reference point the file-mtime forensics above depend on.

---

## Starting a migration — the DISPATCHER's job

**Who this is for:** the regular Copilot CLI session that auto-loads this file — the one the human
actually talks to. It is **not** `tableau-migrator`. That persona is a *per-unit worker*: it migrates
one workbook against a plan someone else already made. Deciding **what** to migrate, **in what
order**, and **to where** happens up here, before any persona is invoked.

That distinction is easy to get wrong (this section's first draft duplicated the whole intake into
`tableau-migrator` and pushed it to 100 % of its character cap). The rule: **the dispatcher decides
and writes the brief; `tableau-migrator` reads the brief and executes.**

### Step 1 — work out what you are actually pointing at

Do not ask "which workbook?" until you know what kind of thing you were handed. The four input shapes
take genuinely different first moves, and picking the wrong one is expensive rather than merely wrong
— a site migrated workbook-by-workbook rebuilds a near-identical semantic model N times, and those
copies then drift.

| You were given | First move | Because |
|---|---|---|
| **A Tableau Server/Cloud site** (URL + PAT) | **`python scripts/run_engine_survey.py --server <host> --site <slug> --pat-name <name> --env-file .env --json _assessment/estate_survey.json`** → `python scripts/assess_estate.py --out _assessment --survey _assessment/estate_survey.json` → `python scripts/tableau_lineage.py --plan` → **`python scripts/harvest_estate_assets.py --out <dir>`** → `python scripts/run_estate.py --input <dir>/assets --output <bundle>` | Assess emits *a decision, not an inventory*: what exists, what is **actually used**, how hard each workbook is, who can see it — but without `--survey` it reports migration **order as unknown**, and a workbook whose published datasource has not landed first rebuilds to an **empty report**. Harvest is the seam: it downloads every workbook and published datasource to `<out>/assets/` as `.twbx`/`.tdsx`, exactly what `run_estate.py --input` consumes. This is a deliberate **two-step** flow, not a gap — the engine's `LiveTableauSource` is an explicit stub ("network calls NOT built yet"), so there is no one-button live-site→PBIP path. |
| **A folder of `.twb`/`.twbx`** | `python scripts/run_estate.py --input <folder> --output <bundle>` | Sweeps the whole folder through the deterministic tier and emits per-workbook handover slices. No server, so ordering is derived from the parsed specs rather than Tableau's metadata API. |
| **One `.twb`/`.twbx`** | `python scripts/parse_tableau.py <file> -o <spec>` → dispatch `@tableau-migrator` | The simple path. Still write a brief. |
| **A `.tds`/`.tdsx`** (data source, no workbook) | `parse_tableau.py` accepts it directly | This is **phase 1** of a model-first estate: a semantic model with **no report**. Also the fix for a `sqlproxy` published source, whose calcs live on the server and are therefore *under-reported* by any workbook that merely points at it. |

`estate_survey.py` is the deterministic **engine's** script, not ours, so it is not on `PATH` — it
installs under `~/.copilot/installed-plugins/tableau-collection/tableau-fabric-skills/skills/tableau-migration/scripts/estate_survey.py`.
**That installed plugin is the ONE canonical engine** (see the section below); ask
`python scripts/engine_source.py` for the path rather than typing it, and never point a step at a
second copy.

**Three things about that invocation will bite you, all measured:** `--server` is required (there is
no default); `--json` takes a **PATH**, not a bare flag; and — via `run_engine_survey.py` — the PAT
**name** rides through from `.env`, so the `--pat-name` above is optional: it carries the whole file
into the engine's child env (with `--no-prompt`). A *direct* `estate_survey.py` call must supply the
name itself — `--pat-name` or an exported `TABLEAU_PAT_NAME` — because the engine's
`credential_resolver.py` reads only the *secret* from `--env-file`, never the name.

`harvest_estate_assets.py` is worth more than the download: it also runs **both** parsers (ours for
fidelity, the engine's for conversion) over every asset and writes `<out>/parse-sweep.md` /
`parse-sweep.json` — an estate-wide failure distribution, and exactly the evidence an upstream
feature request needs instead of an anecdote.

**Capture the Tableau reference imagery in the SAME trip — this is the dispatcher's job, not a
subagent's.** A fidelity review later needs a picture of the *source*, and nothing downstream produces
it: the Desktop Bridge `screenshot`/`screenshot-all` commands shoot the Power BI *output*, not
Tableau. Capturing it now, while you are already authenticated to the site, is the difference between
one command and a re-authentication against a server that may since have gone dark — which is why it
belongs here and not inside a persona (issue #198: an operator had to ask for it by hand, because the
builder/validator personas each routed capture themselves and stalled on the wrong tool). **Pick the
tool by the source:**

| Source | Capture command | Notes |
|---|---|---|
| **Tableau Public URL, or a local `.twb`/`.twbx`** | `python scripts/capture_tableau_reference.py migrations/workbooks/<slug> [--public-url <url> --view <view>]` | Writes a provenance-stamped `reference/manifest.json` — a `capabilities`-carrying `validation_grade` source; its `manual` provider also adopts user-dropped `tableau-*.png` in `reference/`. An **existing `reference/manifest.json` short-circuits to exit 0** — re-run with `--force`. |
| **Tableau Server/Cloud** (`TABLEAU_SERVER_URL` configured) | `python scripts/capture_tableau_oracle.py --out _oracle --images [--workbook "<published name>"]` | The command on the left **exits 3** on an empty target when the URL is set (URL unset → 1): that is *"wrong tool for this source"*, **NOT** "capture is impossible" — misreading it is the whole of #198. Its `server_rest` provider is a `NotImplementedError` stub. Use the oracle, which does the live REST image export. |

Three things about the oracle bite, all verified in `scripts/capture_tableau_oracle.py`:
- **`--out` is required** — omit it and argparse exits 2, capturing nothing.
- **`--workbook` is an exact, case-insensitive *published-workbook-name* filter** (`select_views`), so
  substituting a migration slug for the published name silently matches **zero** views.
- **Do not trust the oracle's exit 0.** It is computed from per-view *data* status only — image status
  never reaches the exit code, and zero selected views also exits 0 — so `--images` can return 0
  having produced no image. Confirm the capture in `_oracle/oracle-manifest.json` (a non-zero
  `view_count`, plus each view's image status) before believing it happened. It writes renders to
  `_oracle/images/<view>__<luid8>.png`, numbers to `_oracle/data/`, and that manifest.

Credentials for either tool come from `.env` **or exported environment variables** (the latter take
precedence), never CLI arguments.

**The conversion engine has exactly ONE source: the installed plugin.** `tableau-fabric-skills@tableau-collection`,
at `~/.copilot/installed-plugins/tableau-collection/tableau-fabric-skills/`. Not a sibling clone, not
a checkout in `~/vscode-projects`, not "whichever one a script finds first". Every step resolves it
through **`scripts/engine_source.py`**, which **raises rather than falling back** — because a silent
fallback is exactly what issue #107 was.

Why that is a rule and not a preference: measured 2026-08-12, this machine had the engine installed
**twice at different versions** — the plugin at 2.113.0 and a sibling clone at 2.126.0 — and
different steps of one pipeline resolved different trees. They are not equivalent. On the same
workbook, 2.113.0 emitted four deprecated Bing `shapeMap` visuals, turned a pie-on-map into a plain
`pieChart` with the geography discarded, and **emitted no visual at all** for the density map;
2.126.0 emitted `azureMap` throughout, with a heat-map layer. Nothing in the run output said which
one had run.

What now enforces it:

| mechanism | what it does |
|---|---|
| `scripts/engine_source.py` | the single resolver. `engine_root()` returns the plugin or **raises**; `resolve_engine()` refuses a non-plugin `--engine` unless `--allow-noncanonical-engine` is passed |
| `engine-output-receipt.json` | every bundle records `engine.root`, `engine.version` and `engine.canonical` — so an artifact answers *"what built me?"* on its own, months later, without the machine that built it |
| `preflight.ps1` | **critical** checks: the plugin is installed (and prints its `VERSION`), and **no alternative engine tree exists anywhere it could be resolved from**. A second copy is a MISS, not a warning |
| `preflight.ps1 -CheckUpstream` | advisory: compares the installed engine `VERSION` against upstream `main`. Being behind is not an error — the timing rule still decides when acting on it is safe |

Keeping it current: `copilot plugin update tableau-fabric-skills@tableau-collection`, **between
sessions** (a running Copilot session file-locks the plugin directory). Mid-session, only a *content*
refresh is possible — `python scripts/sync_engine_plugin.py --source <checkout>` — and it refuses a
downgrade, because walking the canonical engine backwards is how you would turn a cleanup into the
regression above.

**Whether a version change is safe keys on downstream investment, not the calendar** — specifically,
whether it would mix two engine versions inside one deliverable, or discard hand-authoring layered on
the engine's output. Each verdict is checkable at the moment you decide, which is what lets this
replace the blunt "never mid-migration" it used to read:

| Doing this | Verdict | Check, right now |
|---|---|---|
| **Comparing** two versions — run the second via `--allow-noncanonical-engine` into a fresh `--output` dir, keep the old bundle, diff | ✅ always safe — not an upgrade; the installed canonical engine is untouched | is the installed plugin left as-is and `--output` a new dir? |
| Re-running a unit on a newer engine with **~no hand-authoring since it ran** | ✅ re-run into a fresh dir; nothing hand-made is lost | **both** baseline diffs are ~empty (see below) |
| Re-running a unit with **substantial hand-authored TMDL/PBIR** on top | ⛔ finish or checkpoint that unit first | **either** diff is large |
| **Partial** re-run into an **existing** bundle (some workbooks, not all) | ⛔ **never** | `write_engine_receipt` stamps one `engine.version` over the whole bundle, so it would claim a single version for artifacts two builds produced — the #107 shape, and `check_engine_receipts.py` cannot see an intra-bundle mix |

⚠️ **`reports/` is the REPORT baseline only, and a workbook bundle may have NO paired model baseline.**
The `reports/`-vs-`pbip/` diff is therefore structurally blind to hand-authored TMDL — measured,
`reports/` holds **0** `.tmdl` files — so an agent that authored DAX, relationships, RLS or AI
metadata but left the report alone sees an ~empty diff, reads row 2's "nothing hand-made is lost",
re-runs, and loses all of it. That is exactly what `pbi-semantic-builder` produces, so it is a
first-class case, not an edge one.

⚠️ **Do NOT reach for `<bundle>/semantic_models/<WB>.SemanticModel` as the missing baseline.** That
tree is keyed by model, not workbook, and is emitted for models **not owned by a single workbook**
(published/shared datasources; `bundle_corpus.py:33` — "datasource-only migrations can legitimately
ship a standalone model there"), not as a guaranteed workbook-model pair. The miss is silent:
`git diff --no-index` exits **1** for
*"could not access"* exactly as it does for *"they differ"*, so a missing baseline reads as
"large diff — do not re-run". If there is no counterpart, record **BASELINE UNAVAILABLE**, never a
clean/no-change diff. See #359.

**What actually answers the question, verified:** compare the OLD bundle against a FRESH run of the
new engine, model to model — or, when a standalone/shared model counterpart exists, compare by
`<Model>` name, not `<WB>` name:

```
git diff --no-index --stat <bundle>/reports/<WB>.Report                       <bundle>/pbip/<WB>/<WB>.Report
git diff --no-index --stat <bundle>/semantic_models/<Model>.SemanticModel     <bundle>/pbip/<WB>/<Model>.SemanticModel
git diff --no-index --stat <old-bundle>/pbip/<WB>/<Model>.SemanticModel       <new-bundle>/pbip/<WB>/<Model>.SemanticModel
```

Measured 2.208.0 vs 2.339.0 on Superstore: `13 files changed, 488 insertions(+), 169 deletions(-)`,
with a real stat line — a genuine answer, not an access error. **Check for the stat line, never the
exit code.** Note this answers *"what does the new engine produce differently"* rather than *"what
have I hand-edited"*; for the latter, `_build/generated-edit-declarations.json` records our tier's
changes deliberately and is the better source. A check that is *evaluable but incomplete* is worse
than a blunt prohibition, because it grants false confidence.

So the escape hatch above is usable between migrations, and mid-migration when **both** diffs show
little at risk; the one move that is *always* wrong is the last row — a partial re-run into a live
bundle.

Historic cost of not having this: **three** retracted or nearly-retracted defect reports. The engine
went 2.60.0 → 2.72.0 unnoticed; then 2.113.0 → 2.126.0 (13 releases) *mid-dry-run*; then 2.113.0 →
2.126.0 → upstream 2.135.0 with two copies live at once. Each was caught only by a manual `git fetch`.

**An engine defect is filed UPSTREAM — `gh issue create --repo Yarbrdab000/tableau-fabric-skills`.**
The plugin tree is read-only, so an issue is the only way a finding reaches the person who can fix it.
Thirteen have gone there (#114–#141) and the author closes them, so this works — which is exactly why
filing one in the *wrong* tracker is expensive: it looks filed, it is searchable, and it is invisible
to him forever. ⚠️ **The two numbering ranges do not overlap and that is the trap**: ours passed 200
while upstream was still at ~141, so a bare `#220` reads as plausible in either repo and nothing
flags the mistake. Measured 2026-08-18: **three** engine issues (a bare Column in a Measure-only role,
a stubbed calc dropping a required role, and the DoD gap behind both) sat in *our* tracker for days —
found only when someone asked "shouldn't that be a feature request upstream?", and re-filed as #142–#144.

**The test: who has to change code?** If the fix edits `migrate_estate.py`, `pbir_lint.py` or anything
under the plugin, it is upstream — even when we also ship a mitigation on our side. Our tracker is for
*our* tier (`scripts/`, the personas, the skills, the docs). When both apply, file upstream and keep a
local issue only for the mitigation, cross-linked — never a second copy of the defect report.

**Where does the output go?** A migration produces a **local PBIP bundle**; publishing it is a
separate, deliberate step — and that step has a tool. `scripts/deploy_estate.py` lands a bundle in an
**existing** Fabric landing-zone workspace: models first, each report rebound from `byPath` to
`byConnection` once its model exists, crash-safe by a run journal. Run `--dry-run` first — it reports
the plan and the **item count**, which is the number a customer agrees before you deploy. Mechanics
and the three tenant-measured facts it encodes: `scripts/README.md`; post-deploy check:
`verify_bindings.py`.

What it deliberately does **not** decide is **topology**. It takes ONE `--workspace` and never creates
one, so cross-workspace shared-model binding, permission mapping and promotion out of the landing zone
remain human decisions (#57). So the target question is not "local or Fabric?" but *"name the
destination workspace in the brief, and say whether this run stops at the bundle or goes on to
deploy."*

### Step 2 — five questions, asked ONCE, in one message

**The problem was never that we ask too little. It is that every question arrived too late.** Before
this section existed, the orchestrator had four ask-moments and **all four were mid-flight**: the
published-datasource decision (post-parse), the credential stop (post-probe), a re-parse confirmation,
and the retry cap. Each interrupts work already in progress — and if the user has stepped away, the
run dies there. Measured 2026-08-07: the `4-nocreds` end-to-end run did exactly that.

Meanwhile nothing was asked up front, so fidelity bar and autonomy were inferred silently. From the
outside, an hour of confident work on a wrong assumption is indistinguishable from an hour of correct
work.

Step 1 answers *scope* by investigation, so only these are genuinely questions:

| # | Question | Why it cannot be inferred |
|---|---|---|
| 1 | **Confirm the plan from step 1** — this ordering, these workbooks, this destination? | Assessment says what is *used*; only the human knows what is *wanted*. |
| 2 | **Autonomy** — see the table below. Default `standard`. | The failure modes are symmetric: too autonomous and a run spends 105 minutes saying nothing; too interactive and an overnight run stops on question 1 and achieves nothing. |
| 3 | **Fidelity bar** — faithful re-creation, or modernise where Power BI is better? | It decides real translations (a Tableau dual-axis trick → a native combo chart; a `MAKELINE` route map → endpoint bubbles). Both builders need it. |
| 4 | **If we hit a wall — stop, or degrade?** | Pre-authorising the fallback is what lets an unattended run *survive* one instead of dying at 3 am. |
| 5 | **Who drives the data refreshes?** — see the table below. Default `scripted`. | A refresh is the one long operation with **no progress signal**, so an agent cannot tell "working" from "hung" *while it is happening*. Only you know how big the source is and whether you will be at the keyboard. |

**Autonomy levels, defined by behaviour at a decision point — not by vibe:**

| level | reversible choice | costly or irreversible | credential wall |
|---|---|---|---|
| `guided` | ask | ask | ask |
| **`standard`** (default) | decide, log it | **ask** | ask |
| `autopilot` | decide, log it | decide, flag in the summary | **ask — always** |

**Autonomy governs choices; it cannot govern physics.** No level clears the credential stop: that is a
modal sign-in dialog no automation can fill. Question 4 pre-authorises the *fallback* (build
model-only under `credential_gate.py authorize`, artifacts marked unvalidated) — never pretending a
source was reachable.

**Refresh strategies, and why this is a question rather than a default:**

| strategy | who drives it | ceiling | you can see progress |
|---|---|---|---|
| **`scripted`** (default) | `refresh_pbip_model.py` | **hard 300 s** (330 s with grace), not configurable | ⚠️ an elapsed-time heartbeat only — `still refreshing, 42s / 330s` |
| `operator` | the agent prepares everything, stops, and asks **you** to hit Refresh in Desktop | none | ✅ per-table row counts, live in the UI |
| `xmla` | manual XMLA/TOM against the live instance | none | ⚠️ partial |

**Why it earns a slot in the intake instead of being discovered mid-run.** A refresh is the only
routine step where *"still working"* and *"hung"* produce an identical signal — the scripted path's
heartbeat reports **elapsed time, not work done** (`print_refresh_heartbeat` is documented as
printing "an elapsed/total countdown *without claiming progress*"), so a slow refresh and a stuck one
print the same line. Be precise about what it does catch: a **detected** credential modal does not
heartbeat on, it aborts with a specific error. What survives is the case the detector cannot see —
and the script says so itself on timeout: *"CAUSE UNKNOWN - this script cannot distinguish these two,
and they need opposite responses"* — so the decision lands at the worst possible
moment: mid-flight, under uncertainty, on the
agent. Worse, the two governing rules **point in opposite directions** there. The general rule says
time-box an unresponsive external system at ~2 minutes or 3 attempts; the carve-out says *don't*, if
the tool announces its own deadline — and `refresh_pbip_model.py` is exactly such a tool. An agent
that resolves that tension wrongly either kills a legitimately-running refresh (recording **no
verdict at all** — measured) or waits indefinitely (129 minutes / 298 tool calls — also measured).

And the default's ceiling is **known to be too low for real sources**: measured against Snowflake,
one table family took **~700–750 s** and another **~452 s**, both over the 330 s ceiling. Narrowing
with `--tables` does **not** rescue it when a *single table* is the bottleneck rather than cumulative
cost (proven twice, identical timeout both times). See #253.

So: if the source is large or the run is unattended, say so **now**. `operator` trades start-latency
for a refresh that cannot silently time out and that you can watch. Picking it up front costs one
line in the brief; discovering it at 330 s costs the refresh.

⚠️ `xmla` has a scope constraint worth stating in the brief: it must be **whole-database** scope if a
calculated table depends on a refreshed table (e.g. a `Date` table built with
`CALENDAR(MINX(...), MAXX(...))` over the fact). Refresh the fact alone and the calculated object
does not recompute in the same transaction.

### Step 3 — write the brief, then dispatch

Write the answers to `migrations/workbooks/<name>/migration-brief.md`. **The file is the point**, for
two reasons no persona can solve alone: it survives a **dropped session** (a closed terminal takes
this session's entire working memory with it — measured, 2026-08-08), and it is what a **stateless**
subagent receives instead of re-deriving intent nobody wrote down. Then invoke `@tableau-migrator`
per unit of work, handing it the brief.

**Record the Step-1 capture in the brief — grade included.** A stateless subagent cannot see what you
captured; the brief is how it learns. For each unit write down three things: **where the reference
landed** (`migrations/workbooks/<slug>/reference/` for a `capture_tableau_reference.py` run, or
`_oracle/images/…` for an oracle run), **which tool produced it**, and **what grade of evidence it
is** — the load-bearing part. A `reference/` capture carries a `capabilities` manifest and is a
`validation_grade` source; an **oracle** capture is **not** — its images land outside `reference/`,
carry no `capabilities` manifest, and are taken in the view's **default state only** (no `?vf_` filter
pinning), so they are **layout- and text-grade only**. Say so in the brief, and tell the consumer to
log that ceiling in `limitations_encountered`: a visual PASS signed off on oracle imagery alone is
overstated (issue #194). Do not quietly drop this, and do not inflate it.

**Add `--reference-best` to any oracle run that covers dashboards.** `?resolution=high` is measured to
be **exactly 2× the dashboard's declared size, with no parameter that raises it** — a 650×800 dashboard
tops out at 1300×1600 forever — so a label-dense page can be structurally legible and content-illegible
at once. `--reference-best` **probes** the ladder (`svg` → `pdf` → `png_high`) and takes the best rung
the site actually answers on, then records the tier, the per-rung verdicts and all three version
numbers in the manifest's `render_capability`.

⚠️ **Which rung a customer gets depends on their Tableau version, and Cloud is not representative.**
SVG needs REST **3.29** = Cloud June 2026 / **Server 2026.2**, so an on-prem site on 2023.x–2025.x has
**none** — but `/pdf` reaches back to API **2.8** (Server 10.5, 2018) and is genuinely vector with
*embedded fonts*, and `?resolution=high` to API **2.5**. Never infer capability from a version string:
`TABLEAU_REST_API_VERSION` is a *client preference* we send, the same Cloud site moved 3.29 → 3.30 in a
week, and a client pinned below the floor loses a tier the server supports (now a named warning). None
of this upgrades the grade: still default-state, still outside `reference/`. It raises the ceiling
*within* text-grade. Route survey, the API→release map and the numbers:
[`docs/reference-capture.md`](docs/reference-capture.md) (issues #403, #194).

> **Running the pipeline by hand, or standing behind someone who is?**
> [`docs/operator-runbook.md`](docs/operator-runbook.md) is the command-by-command version of this
> section: the day-before checklist, expected timings, the failure playbook (the `estate_survey.py`
> site-wide silent sweep, the `exit 3` DoD gate, the storage-decision/union skip), the verification
> checklist and — importantly — what that checklist does **not** prove.

### Migration cost attribution

Every migrated workbook or datasource **must have its own** `_runs/<NNN>-<slug>/run.json` before
agentic work starts. Record attribution at dispatch/allocation time, not at completion: a crash after
spend but before stamping the root makes the spend permanently unattributable.

A dedicated Copilot **session** per migration unit is the reliable attribution anchor; record its
`session_id` in `run.json` and do **not** mix unrelated questions or other units into that session. If
unrelated work happens anyway, flag the run as polluted in `run.json`; it remains visible but must not
be silently averaged into customer budget estimates.

A dispatched `@tableau-migrator` root `agent_id` may be recorded as an additional label under
`attribution.roots[]`, but it captures only that agent's own calls, never its descendants.
`parent_tool_call_id` is the agent's own spawning tool call, not a parent-agent id, and no table
available in one store maps that tool-call id back to its issuing agent (`assistant_usage_events` is
local-only; `tool_requests` is cloud-only). So a subtree walk is not reconstructible and spend cannot
be rolled up: issue #364's original premise was right after all, and the session is the only bucket
that contains a dispatched agent and all of its child agents.

A session with no `run.json` is development work for cost-reporting purposes and is excluded.
Retroactive attribution is impossible: old units migrated without a dispatch/allocation anchor cannot
be backfilled honestly.

Report **both** model time and elapsed time. They can differ by a large factor because tool execution
(dependency sync, Desktop work, test suites, file operations) consumes elapsed time outside model
calls. A customer estimate that quotes only one is misleading.

### Gate B — after parse + probe, before building

Some decisions genuinely cannot be front-loaded: you do not know a workbook points at a published
datasource until you parse it, or that a warehouse refuses Power BI until you probe it. Fine — but
`tableau-migrator` must present them as **ONE block, not four serial stops**. Serial stops are the
same questions with strictly more waiting, and each is another chance to catch the user absent.

1. Published datasources → fetch the `.tds` and migrate it first, or proceed knowingly incomplete.
2. Live sources that failed the probe → credential, or the authorised build-only path.
3. Extract-only sources → materialize real rows, or model-only.
4. The high-severity `limitations_encountered` digest → proceed, or narrow scope.

Where step 2 question 4 already answered one, **apply it and say so** — do not re-ask. The brief
exists so each question is answered once per migration, not once per session.

---

### Engine model-baseline availability

`reports/` is reliable engine truth; `semantic_models/` is not a per-workbook guarantee. In a
12-workbook estate audited on 2026-08-24, all report baselines existed but only 4 models (33%) had
an engine-truth counterpart; the other 8 working models were unpaired. This is a baseline-coverage
fact, not evidence that those eight models needed no changes.

Before calculating model churn, check that each working model has an engine-truth counterpart. A
missing counterpart must be reported as **BASELINE UNAVAILABLE**, never as a clean/no-change diff;
only an existing pair may yield “no changes.” Report model-pair coverage beside every engine-gap
distribution and do not generalize model churn from the paired subset to the estate; see
[issue #274](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/274) and the
operational procedure in [`docs/operator-runbook.md`](docs/operator-runbook.md#engine-model-baseline-availability).

---

## Canonical work layout (pre-bundle stages, scratch, deliverables)

Issue #291 named two gaps: the stages **before** `run_estate.py` have no shared convention (each
script invents its own `_assessment*/` / `_sweep*/` / `_oracle*/` name), and per-run **scratch** has
no home or lifecycle — 31 ad-hoc `_*` roots accumulated with nothing recording what any is for.
Issue #234 designed the fix and corrected itself twice in review (own the corrections, not just the
original text): **`_runs/<NNN>-<slug>/`**, never `_work/` — `.gitignore`'s existing `**/_work/` rule
means the OPPOSITE thing (its `.py` transform scripts are deliberately tracked). The number is the
identity (never renamed/reused — bundle output embeds absolute self-paths); the slug is decoration
only, because a display name is never unique (two projects or two workbooks can share one).

```
_runs/<NNN>-<slug>/
    run.json          <- the one authoritative description of this run
    assessment/        assess_estate.py-shaped output
    assets/             harvest_estate_assets.py-shaped downloads
    bundle/              run_estate.py-shaped conversion output
    oracle/               capture_tableau_oracle.py-shaped reference capture
    deliverables/          operator-facing outputs meant for the CUSTOMER, never for git —
                            the `ses-prep/` near-miss (issue #322): a `connections.json`/`.md`
                            naming 17 real customer servers landed unprefixed, unignored, at the
                            repo root, one `git add -A` away from being committed
    scratch/                disposable, run-owned — the only subdir a future `--prune` may delete
```

`/_*` in `.gitignore` already covers the whole tree by construction — verified,
`git check-ignore -v -- _runs/<NNN>-<slug>/deliverables/connections.json` reports `.gitignore:.../_*`
— **without** a trailing slash (a trailing slash makes `git check-ignore` report every path as
ignored, which proves nothing; see `harvest_estate_assets.py`'s own guard for the same trap).

**`scripts/work_dirs.py`** is the single source of truth for these paths — `sanitize_unit_key`,
`allocate_run` (atomic `mkdir`-exclusive, retry on collision, never a read-then-write race),
`RunPaths` (the six subdirs above as properties), and `list_runs`. It resolves the repo root from
its **own file location**, never from `Path.cwd()` — an empty stray `fabric/` was once written at
the repo root by a script that resolved a relative path against whatever CWD an agent happened to
invoke it from, which is exactly the failure a single importable resolver removes.

**Scope landed so far:** the convention plus the helper only. `assess_estate.py`,
`harvest_estate_assets.py`, `capture_tableau_oracle.py` and `run_estate.py` keep their documented
`_assessment*/` / `_sweep*/` / `_oracle*/` / `_bundle*/` defaults **unchanged** — migrating them onto
`work_dirs.py`, generating `_runs/INDEX.md`, and a legacy-migration helper are separate follow-up
work (issue #234's remaining acceptance criteria), deliberately not bundled with the convention
itself so as not to collide with unrelated in-flight changes to those exact files. `_estate/`,
`_build/` and `migrations/` are explicitly **exempt** — persistent test infra, a bundle-internal
replay convention, and committed deliverables respectively, none of them per-run scratch.

---

<!-- BEGIN:shared-conventions -->
## Shared agent conventions (all agents inherit these)

- **Cite your source — and say WHOSE.** Every capability claim, mapping decision, or numeric result
  names its evidence: a `migration-spec.json` field, a TMDL/PBIR path + line, a live `EVALUATE`
  result, or a doc URL. "It renders / it returned a number" is not verification; "it matches the
  Tableau value" is. **A number also names the estate it was measured on** — ours (the reference
  bundle) or the customer's. Never present ours as theirs: measured 2026-08-21, five did in one day.
- **Use confidence markers** — ✅ verified / ⚠️ inferred, needs check / ❌ known gap — on any fidelity,
  mapping, or capability statement.
- **Own your layer; don't cross it.** `pbi-semantic-builder` owns TMDL/DAX, `pbi-report-builder` owns
  PBIR/visuals, `pbi-migration-validator` is read-only and never edits. A subagent never "just fixes"
  a finding another agent owns — it reports; the orchestrator routes.
- **Three locations, one direction: engine truth → working copy → deliverable. Never edit upstream of
  where you are.**
  | stage | location | rule |
  |---|---|---|
  | engine truth | `<bundle>/reports/`; `<bundle>/semantic_models/` (if emitted) | **NEVER edit an existing baseline** |
  | working copy | `<bundle>/pbip/` | agents edit **here**; every edit re-runnable from `_build/` and declared |
  | deliverable | `migrations/{workbooks,datasources}/<slug>/fabric/` | **COPIED at sign-off**, so the bundle survives as evidence |

  A bundle may contain `<bundle>/{pbip,reports,semantic_models,handover,data}` — **no `out/` level**;
  `<bundle>/semantic_models/` is conditional (absent for 8/12 workbooks), and absent baseline ≠ no
  changes — see `AGENTS.md`. Keep `<bundle>/reports/` pristine and compare it with git
  (`powerbi-report-gotchas` §3).

  ⚠️ **The copy must keep
  `definition.pbir`'s `byPath` resolving** — plain copy for a per-workbook model, path rewrite for a
  shared datasource; never ship `<bundle>/reports/` (reference-only: no model beside it). Mechanics:
  `powerbi-report-gotchas` §3.

- **Structural validation is necessary, not sufficient.** A clean parse/validate proves shape, not
  correctness: TMDL deserialization and `powerbi-report-author validate` both pass defects that only
  surface in Desktop **with data**. Never declare something done on a green validator alone. (The
  PBIR and TMDL specifics live in the `powerbi-report-gotchas` and `powerbi-semantic-model-gotchas`
  skills, which the owning agents invoke.)
- **Keep `limitations_encountered` alive** through the whole build **and** fix phase; every bug found
  and fixed later is itself worth recording. Regenerate it from the final artifacts before sign-off so
  stale entries don't mislead the validator.
- **Declare generated edits.** TMDL/PBIR/`.pbip`: file/change/why + replay script + hash record.
- **Surface complexity mismatches proactively.** If the parsed workbook implies more effort than the
  user assumes (many LOD/table-calc fields, extract-only data with no upstream, >20 floating-layout
  worksheets), say so before building rather than discovering it mid-migration.
- **NEVER block silently on an external system — time-box it, then ASK.** Measured, from a real user
  report: an agent sat on "Testing live Snowflake connectivity" for **129 minutes / 298 tool calls**,
  retrying without ever surfacing the problem, until the user intervened. Waiting is not progress.
  - **Cap it: ~2 minutes or 3 attempts, whichever comes first** — for any unresponsive external
    system (database/warehouse/gateway, MCP server, XMLA refresh, the Power BI Desktop bridge). Cap
    *relaunches* at 2 as well; "kill it and retry" is otherwise an unbounded loop.
  - **Unless the tool tells you it IS the timer** — some of our scripts self-bound and announce their
    own deadline. Measured: an agent applied this 2-minute cap to a script that was already the
    bounded timer, killed it at 120 s, and so recorded **no verdict at all** — strictly worse than
    waiting. Read the tool's own output before you decide it has hung.
  - **A MISSING CREDENTIAL is not transient — try ONCE.** The cap above is for *flaky* systems. A
    refusal naming authentication, permissions or a sign-in prompt is a **final answer**; only a
    plainly transient timeout (a serverless warehouse cold-starting) earns one retry.
  - **AUTOPILOT / auto-approve DOES NOT override a credential stop.** "Decide, don't ask" applies to
    *choices*; this is a physical dependency on a human — the credential sits behind a **modal
    sign-in dialog no automation can fill**. Stop and ask **even in an unattended run**, and end the
    turn. A clear question costs minutes; a confidently built, unvalidated model costs the whole run.
  - On hitting the cap, **STOP and ask a specific, actionable question** — name the system, what you
    tried, and the concrete options. Never re-run the same call hoping for a different result. Ask in
    your normal reply — there is no `ask_user` tool.
  - **Report elapsed time** whenever an operation exceeds ~60 s, so a stall is visible rather than
    looking like work.
- **End every message with a clear next step or an explicit verdict** — never a vague "looks fine."
- **Durable learnings go in committed files** (the agent `Gotchas` sections and
  `docs/tableau-dax-translation-guide.md`), never in a git-ignored scratch folder — that is how each
  real migration permanently improves the toolkit.
- **Power BI Desktop cleanup is PID-scoped.** Concurrent instances are fine; never sweep by name.
  Use the literal PID you opened (`Stop-Process -Id <pid> -Force`; `$pid` is a read-only shell
  variable), and never close a sibling's instance or one mid validator↔builder handoff. Run-owned
  leaks are enforced by `check_unit.py`'s `desktop-orphans` gate. Remove scratch/temp files you
  created; keep only committed deliverables plus re-runnable `_build/` scripts, and confirm nothing
  scratch leaked into git before reporting done. ⚠️ **Never `git add -A` after a gapped pull** —
  measured: a merge staged **111** untracked scratch paths (a whole engine bundle, loose `_tmp_*.py`)
  because `-A` cannot tell "files this merge introduces" from "files that happened to be lying
  around". Stage from `git diff --name-status <old-HEAD> origin/master`. If you must undo one,
  `reset --soft HEAD~1` **clears `MERGE_HEAD` even on a merge commit**, so recreate it or the next
  commit is silently single-parent and the ancestry breaks.
<!-- END:shared-conventions -->
