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
| Mid-migration | **never** | Swapping the validator under a half-built report is worse than a slightly old one |

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

This toolkit is not self-contained: its agents build on Microsoft's official **Fabric / Power BI
skills** (published as a Copilot *plugin*) and talk to Power BI through **MCP servers**. A clone needs
all three layers below. The agent files under `.github/agents/` are already committed and load
automatically; `.github/skills/` is an enabled skill location (see `.vscode/settings.json`) — the
repo-local skills committed there load automatically too.

### 1. Skill plugins — `powerbi-authoring@fabric-collection` + `powerbi-playbook@powerbi-playbook-collection`

Two plugins, both needed. **Measured 2026-07-31: a custom subagent *does* get a `skill` tool and can
invoke plugin skills by name** (see [`docs/agent-architecture.md`](docs/agent-architecture.md) §6.1) —
so a missing plugin is a real capability loss for the builder personas, not a cosmetic warning. Worse,
it fails **silently**: the persona still loads, it just can't reach the skill.

**a. `powerbi-authoring`** — the `pbi-report-builder` and `pbi-semantic-builder` agents chain the
official `powerbi-report-planning` / `powerbi-report-design` / `powerbi-report-authoring` /
`semantic-model-authoring` skills, which ship in this plugin from the **`fabric-collection`**
marketplace (GitHub repo [`microsoft/skills-for-fabric`](https://github.com/microsoft/skills-for-fabric)).

**b. `powerbi-playbook`** — republishes *this repo's own* reusable bundles
(`powerbi-ai-readiness`, `pbip-model-refresh`, `powerbi-report-gotchas`,
`powerbi-semantic-model-gotchas`) from
[`Guust-Franssens/powerbi-playbook`](https://github.com/Guust-Franssens/powerbi-playbook),
generated by [`scripts/build_plugin.py`](scripts/build_plugin.py).

**Why publish at all, when repo-local `.github/skills/` already works?** Fair question — §6.1 proved a
subagent can invoke a repo-local skill by name, so *this* repo gains nothing from the plugin. The
plugin exists for **other** repos. All four bundles are deliberately source-tool agnostic and their
input is already a Power BI model, so they apply to any Fabric/Power BI work — a Qlik or Cognos
migration, or an unrelated engagement. Without the plugin, reuse means copying folders into each new
repo, which forks the knowledge on day one. It is a **distribution** decision, not a capability one.

**And why a separate repo?** `marketplace add` clones the whole repo: this one is ~170 MB of tracked
files + git history to deliver ~60 KB of skills.

**Be aware of the asymmetry:** the *benefit* is collected in other repos, but the *cost* — the
shadowing hazard below, and the preflight gate written to police it — is paid here, where both copies
coexist. That is the trade, and it is worth it only because the bundles genuinely travel.

⚠️ **Installing/updating a plugin only works BETWEEN sessions — but a *content* refresh does not have
to wait.** A running Copilot session file-locks the plugin directory, so `copilot plugin install`
fails with `Access is denied. (os error 5)`. Measured 2026-08-01, that lock is **narrower than the
error implies**: it blocks *renaming* the top two plugin directories, while files inside stay freely
writable. `plugin update` fails only because it swaps the directory wholesale. So when just a
bundle's prose or scripts changed, run **`python scripts/sync_installed_skills.py`** and the installed
copy is current immediately — no restart. A manifest/version/MCP change still needs the real
`plugin update` between sessions (`scripts/update_playbook_plugin.ps1` automates that path).
Either way, skills are snapshotted at session start, so a *running* session keeps the old copy in
memory; new sessions and the subagents they spawn get the new one. Preflight reports
`NOT INSTALLED: <bundle>` when a shipped bundle is missing locally.

Install once, in Copilot CLI:

```
/plugin
```

then add each marketplace and install/enable its plugin (the broader `fabric-skills` bundle from
`fabric-collection` is optional — it adds cross-workload Fabric agents like FabricIQ). Equivalent
settings, if you edit `~/.copilot/settings.json` by hand:

```jsonc
{
  "extraKnownMarketplaces": {
    "fabric-collection": { "source": { "source": "github", "repo": "microsoft/skills-for-fabric" } },
    "powerbi-playbook-collection": { "source": { "source": "github", "repo": "Guust-Franssens/powerbi-playbook" } }
  },
  "enabledPlugins": {
    "powerbi-authoring@fabric-collection": true,
    "powerbi-playbook@powerbi-playbook-collection": true
    // optional: "fabric-skills@fabric-collection": true
  }
}
```

⚠️ **The plugin copy SHADOWS `.github/skills/`.** Where both supply the same name, the registry lists
it **once** (measured: one entry per name, no duplicates) and a subagent invoking it gets the
**plugin** copy — the loaded base directory was under `~/.copilot/installed-plugins/`, even with the
repo copy present and the cwd inside this repo. So there is no "installed twice" problem; the problem
is the reverse. Editing `.github/skills/` without re-publishing serves subagents stale guidance from
the copy you did *not* edit, and nothing in the skill registry or the tool output flags it.
`scripts/preflight.ps1` hashes every shipped bundle and **blocks** on either failure shape —
`STALE in plugin` (edited but not published) or `NOT INSTALLED` (published but not re-installed).
That check is the only thing standing between you and a silently stale skill.

**STALE blocks rather than warns because it silently invalidates measurements, not just guidance**
(2026-08-01): a timeout was added to `refresh_pbip_model.py` in the repo, an agent ran the *plugin*
copy that did not have it, and the resulting timing was used to draw a confident conclusion about
behaviour the executed code did not contain. **Never trust a measurement taken against a STALE
bundle** — the code that ran is not the code you are reading.

> Do **not** use the deprecated `RuiRomano/powerbi-agentic-plugins` marketplace — it is superseded by
> `powerbi-authoring@fabric-collection`.

### 2. MCP servers — see [`.vscode/mcp.json`](.vscode/mcp.json)

`.vscode/mcp.json` (auto-read by VS Code Copilot) declares the two servers the pipeline uses:
- **`powerbi-modeling-mcp`** (stdio, `@microsoft/powerbi-modeling-mcp`) — semantic-model writes.
- **`powerbi-remote`** (http) — read-only schema inspection + DAX `EVALUATE`.

Copilot CLI users register the same servers with `/mcp`, or copy them into
`~/.copilot/mcp-config.json` under an `mcpServers` key.

### 3. Repo-local skills & agents (already committed, zero setup)

- Agents: [`.github/agents/*.agent.md`](.github/agents/) — `tableau-migrator` (orchestrator),
  `pbi-semantic-builder`, `pbi-report-builder`, `pbi-migration-validator`.
- Any repo-specific skills live under `.github/skills/` (already an enabled skill location via
  `.vscode/settings.json`). Committed today:
  - [`pbip-model-refresh`](.github/skills/pbip-model-refresh/SKILL.md) — refresh a local PBIP/TMDL model
    in Power BI Desktop and persist it to `.pbi/cache.abf` (AMO `ImageSave`, UIA fallback), plus the
    edit-then-refresh-then-save ordering rule and the pid-binding rule.
  - [`powerbi-ai-readiness`](.github/skills/powerbi-ai-readiness/SKILL.md) — make a semantic model
    answer natural-language questions correctly: descriptions, enumerated domains, model-level AI
    instructions (`CustomInstructions`), and the `qnaEnabled` switch that silently voids all of it.
    Includes the writing guide (`docs/ai-instructions-authoring-guide.md` is now a stub pointing here)
    and the scoped gate `set_ai_instructions.py --check --strict --model <model>`.
  - [`powerbi-report-gotchas`](.github/skills/powerbi-report-gotchas/SKILL.md) — ~18 KB of hard-won
    PBIR authoring and Desktop-verification knowledge: validation-invisible rendering bugs,
    conditional-formatting encodings, azureMap/scatter/matrix recipes, and the Desktop refresh/bridge
    mechanics. Extracted from `pbi-report-builder` (153% of the 30,000-char cap, i.e. this knowledge
    sat in the region a hosted run truncates first). The persona now invokes it as step 0, gates on it
    in its Definition of Done, and keeps a section index so it can tell when it needs it.
  - [`powerbi-semantic-model-gotchas`](.github/skills/powerbi-semantic-model-gotchas/SKILL.md) — the
    same treatment for `pbi-semantic-builder` (141% → 99%): TMDL pitfalls that crash Desktop on open,
    the field-parameter `sourceColumn` bracket trap, the `'Table'[Col] = [Measure]` PLACEHOLDER error,
    MCP/Desktop operational rules, and the offline model-integrity checks `TmdlSerializer` misses.

  **All four personas are now under the cap** (`sync_agent_conventions.py --check` exits 0). New craft
  learnings belong in these bundles, not back in a persona — that is what keeps it that way.

  Both are deliberately source-tool agnostic so they port to Qlik/Cognos migrations or a global skill
  location, and packaged to match: each folder carries its own `scripts/` and `tests/`, so **copying
  that one folder** takes the whole procedure with it. `tests/test_skills.py` gates their links and
  executes that claim — it copies each folder to a temp dir and runs its bundled tests with this repo
  unimportable. `scripts/probe_desktop_query.py`, `scripts/refresh_pbip_model.py`,
  `scripts/set_ai_instructions.py` and `scripts/check_ai_readiness.py` remain as forwarding shims for
  callers that predate the moves.

  **These reach subagents and are invocable by name** (measured 2026-07-31 — a subagent invoked
  `sentinel-probe`, which exists *only* in `.github/skills/`, and quoted its sentinel token). Both are
  also republished as the `powerbi-playbook` plugin (§1); where a name exists in both, the
  **plugin copy wins**, so keep them in sync — preflight enforces that.
- **Visual cookbook: [`.github/pbi.kb/visual-cookbook.md`](.github/pbi.kb/visual-cookbook.md) +
  [`.github/pbi.kb/visuals/`](.github/pbi.kb/visuals/)** — a committed library of worked,
  `validate`-passing PBIR `visual.json` encodings, each with roles, the Tableau idiom it maps to, and a
  confidence tier. **Coverage note:** the `visuals/` folder holds the *harder/rarer* encodings (combo,
  waterfall, ribbon, treemap, small multiples, azureMap, shape…); the common core types
  (`columnChart`/`barChart`/`lineChart`/`cardVisual`/`tableEx`/`slicer`/`textbox`) are proven **in situ**
  and are copied from the migration paths cited in the cookbook's 🟢 table, not from a local file.
  `pbi-report-builder` copies these instead of guessing visual JSON; new ground-truth encodings are
  added back here so every migration compounds.

### 4. Node.js + the Power BI CLIs

Two npm CLIs do real work in the loop and are **not** bundled with the plugin — install them globally
(needs **Node >= 20**):

```
npm install -g @microsoft/powerbi-report-authoring-cli @microsoft/powerbi-desktop-bridge-cli
```

- `powerbi-report-author` — `validate`, `catalog`, `formatting`, `expr`, `theme`, plus
  `preview-visuals` / `preview-pages` / `preview-filters` / `preview-themes` and `doctor`.
- `powerbi-desktop` — the Desktop Bridge: `status`, `manifest`, `open`, `reload`, `screenshot`,
  `screenshot-all`.

**Known-good version matrix** (verified 2026-07-27). These are *unpinned global* installs, so they can
change under you with no repo diff — and several agent Gotchas encode version-specific tool behaviour.
`scripts/preflight.ps1` prints the installed versions and WARNs on drift:

| Component | Known-good |
|---|---|
| `@microsoft/powerbi-report-authoring-cli` | 0.1.4 |
| `@microsoft/powerbi-desktop-bridge-cli` | 0.1.2 |
| `powerbi-authoring@fabric-collection` | 0.3.9 |
| Node.js | >= 20 (26.2.0 tested) |
| Python | >= 3.11 (3.11.9 tested) |

If a version differs, re-verify the version-specific Gotchas in `.github/agents/` before trusting them.
For **Power BI Desktop specifically**, treat the matrix as "the build this machine's bridge already
answers on, discovered empirically" rather than a portable version string. Field reports on
2026-08-18 found one machine silently launching an older side-by-side Desktop while the bridge sat at
`NO_BRIDGE`, and another where the documented Desktop build was not installed at all. Pin the exe with
`PBI_DESKTOP_PATH` **before launch**, then verify the running process by PID (`powerbi-desktop status`
→ `Get-CimInstance Win32_Process ... CommandLine`); the bridge CLI reports no exe path or version.

### 5. Python tooling

`uv venv && uv sync --all-extras` — the deterministic parser (`scripts/parse_tableau.py`), harvester,
showcase, and validation scripts (`--all-extras` pulls `tableauhyperapi`/`playwright`/`pillow`, which
several documented scripts import). Lint/format with `ruff`; the parser has a `pytest` regression suite
in `tests/`, and a skill bundle keeps its own suite next to its scripts (`pyproject.toml`'s
`testpaths` covers both — pytest skips dot-directories by default). **`uv.lock` is deliberately
gitignored** — see the note in `.gitignore`.

**⚠️ `ruff` alone does NOT predict CI — always run `pylint` too.** CI runs both, and they do not
*fully* overlap — each enforces gates the other does not. (They do agree on some checks: ruff `F401`
≈ pylint `W0611`, `F821` ≈ `E0602`. The gate below is one neither of those covers.)
This repo's ruff selection is `select = ["E4", "E7", "E9", "F"]`, which **excludes `E501`
(line-too-long)**, while `[tool.pylint.format]` sets `max-line-length = 120`. So a 121-character line
makes `ruff check` print **"All checks passed!"** and CI then fail with `C0301` — and `ruff format`
cannot rescue you, because it will not split a long string literal or comment. Measured: PR #155 sat
red ~1h50m on exactly this — `.github/skills/pbip-model-refresh/scripts/_credential_modal.py:282`,
`C0301: Line too long (121/120)`, score 9.99/10,
[run 31839056611](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/actions/runs/31839056611)
— while the mandated ruff ritual reported clean. Fix pattern: wrap the literal in parentheses across
two lines (the runtime string stays byte-identical).

**And `pylint` means all THREE roots, not just `scripts/`.** [`checks.yml`](.github/workflows/checks.yml)
invokes it three times — `scripts`, `.github/skills/pbip-model-refresh/scripts`,
`.github/skills/powerbi-ai-readiness/scripts`. They are *separate* invocations because
`scripts/probe_desktop_query.py` is a forwarding shim sharing a module name with the bundled script it
forwards to, so one combined invocation resolves the import to the shim (measured: 7 × `E0611`).
Strictly, that collision only forces `scripts` and `pbip-model-refresh/scripts` apart;
`powerbi-ai-readiness/scripts` has no colliding name and is separate by the one-root-per-invocation
convention. What
let #155 through is simpler than that mechanism: `pylint scripts` alone passes **10.00/10**, and the
`C0301` came from the *second* invocation. Lint the root that contains the file you changed, not the
one you reach for first.

**`max-module-lines = 1200` is the same trap one level up, and it is worse** — nothing hints at it
until you cross it. Pylint scores **10.00/10** right up to the boundary, then fails with
`C0302: Too many lines in module (1202/1200)`, exit 16: a red CI whose message has nothing to do with
your change. The boundary is `> 1200` — a 1200-line module passes at 10.00/10. Measured 2026-08-15 by
controlled experiment on `scripts/probe_live_source.py`, which a **comment-only** PR (#159) pushed to
exactly 1200 at its pre-merge head `97691af`; it was tightened to 1196 before merging, so master
records 1196 and the near-miss is invisible in its history — squash-merge discards the intermediate
commit. Cite a measurement against something that survives the merge, or say plainly that it does not.
Before adding lines to a long module, check its length
against the cap; if you land within a few lines of it, buy the headroom back rather than leaving the
landmine for the next author.

**But shaving is only one of three moves, and often the worst — there is a sanctioned waiver, and
three modules already use it.** `# pylint: disable=too-many-lines` is carried today by
`scripts/parse_tableau.py` (1709), `scripts/deploy_estate.py` (1903) and
`.github/skills/pbip-model-refresh/scripts/refresh_pbip_model.py` (1305). So on hitting C0302 choose
deliberately between **shave** (fine for a handful of lines), **split** (best when a genuinely
cohesive seam exists — extracting the pure verdict-line matchers out of `probe_live_source.py` is a
worked example) and
**waive**. Waiving is a legitimate engineering answer when the module is one coherent procedure whose
length is documented knowledge rather than sprawl; `parse_tableau.py`'s header argues exactly that,
names the real fix it is deferring, and `deploy_estate.py` cites it as precedent. **A waiver MUST
carry a one-line reason immediately above it** — a bare pragma is indistinguishable from a deadline
hack, and one of the three has none. Know all three options *before* spending a cycle shaving:
`probe_live_source.py` hit the cap three times in two days, and paid in CI failures and a module
split, while a 1903-line neighbour passed on one comment line.

So the ritual that actually predicts CI is `ruff format` → `ruff check --fix` → **`pylint` (all three
roots)** → the targeted tests. Every step of that is load-bearing: skipping `pylint` hides `C0301` and
`C0302`, and running it on only one root hides anything living in a skill bundle.

⚠️ **Run pylint as `.\.venv\Scripts\python.exe -m pylint`, never the bare `pylint` on PATH.** The
global install (`uv tool install pylint`) cannot see the project's optional extras, so it invents
findings CI never sees. Measured 2026-08-18 on an unchanged tree: bare `pylint scripts` reported
**9.95/10, exit 10** with **13 findings** — nine `E0401 Unable to import` (`tableauhyperapi`,
`playwright`, `PIL`, `lxml`, `cryptography`) plus pre-existing `R0913`s — while
`.\.venv\Scripts\python.exe -m pylint scripts` on the same bytes returned **10.00/10, exit 0**. The
two differ *only* in which interpreter resolves imports. Chasing the phantom nine is pure waste, and
worse, the real signal is buried among them; if a finding names a third-party import, check which
pylint you ran before touching anything.

### 6. Preflight — verify everything above in one command

```
powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1
```

The `tableau-migrator` agent runs this first on every invocation. It is a PowerShell bootstrap (not
Python) so it works even before Python is installed — checking FOR Python is one of its jobs. It checks
the parser deps, **both skill plugins** (§1) **and whether the published bundles still match
`.github/skills/`**, the MCP servers, Power BI Desktop + Bridge CLI, `npx`,
the .NET SDK, and the CLI version matrix (plus `powerbi-report-author doctor`), printing an install hint
for anything missing (exit 0 = ready). Run it yourself after
cloning to confirm the machine is configured.

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
regression above. Never upgrade the engine mid-migration.

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

**Where does the output go?** Be honest about this rather than promising: **local PBIP is the only
supported target today.** There is no publish step in this repo — `pbi-deployer` is phase 2 and does
not exist yet. Getting a finished model/report into a Fabric workspace is a manual `fab import` or a
Desktop publish. So the target question is not "local or Fabric?" but *"note the destination
workspace in the brief so the manual publish is unambiguous."* Do not imply an automated deploy.

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

> **Running the pipeline by hand, or standing behind someone who is?**
> [`docs/operator-runbook.md`](docs/operator-runbook.md) is the command-by-command version of this
> section: the day-before checklist, expected timings, the failure playbook (the `estate_survey.py`
> credential hang, the `exit 3` DoD gate, the storage-decision/union skip), the verification
> checklist and — importantly — what that checklist does **not** prove.

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
  | engine truth | `<bundle>/reports/`, `<bundle>/semantic_models/` | **NEVER edited, by anyone** — a free pristine baseline the engine writes anyway |
  | working copy | `<bundle>/pbip/` | agents edit **here**; every edit re-runnable from `_build/` and declared |
  | deliverable | `migrations/{workbooks,datasources}/<slug>/fabric/` | **COPIED at sign-off**, so the bundle survives as evidence |

  A bundle is `<bundle>/{pbip,reports,semantic_models,handover,data}` — **no `out/` level** — and the
  two sides differ in shape, so compare the matching **pair**, with **git** (✅ measured 2026-08-13;
  bare `diff` on Windows is a PowerShell alias for `Compare-Object`, which given two directories
  compares the two path *strings* and prints a confident non-answer):

  `git diff --no-index --stat <bundle>/reports/<WB>.Report <bundle>/pbip/<WB>/<WB>.Report`
  → *98 files changed, 2013 insertions(+), 553 deletions(-)*; **exit 1 = they differ** — but git also
  exits 1 on `error: Could not access`, the likely slip here, so **check for a stat line**, not the code.

  Keeping `reports/` pristine is what makes that an exact answer to *"what did our tier change versus
  what the engine produced?"* — that cost a retracted upstream bug on 2026-08-10 (our fix pass had
  rewritten `reports/`, and the diff was read as engine behaviour).
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
- **Clean up after yourself when you finish.** (a) **Close any Power BI Desktop instance you opened.**
  **Concurrent instances are fine** — the Desktop Bridge addresses one by `--pid` natively and every
  port lookup is PID-scoped, so this is a **leak** rule, not a concurrency limit: each live instance
  holds an `msmdsrv` with the model in RAM, so orphans exhaust the **machine**. Requirement: **name
  your PID** (an unnamed lookup with several instances is a deliberate error, not a coin flip), and
  close what you opened: `Stop-Process -Id <your literal pid> -Force` (map instance→migration
  by `MainWindowTitle`; the shell guard rejects looped/variable `-Id`, and `$pid` is read-only,
  so use literal PIDs). **Never** close a sibling's instance, and don't close one
  mid-handoff that a peer still needs (e.g. a validator awaiting a semantic-builder's fix). (b) **Remove
  scratch/temp files you created** (ajv harnesses in `%TEMP%`, `.pbip` cache/backups, one-off probe
  scripts) — keep only committed deliverables plus the re-runnable `_build/` scripts; confirm nothing
  scratch leaked into git before reporting done.
<!-- END:shared-conventions -->
