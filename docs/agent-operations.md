# Agent operations — monitoring delegated work, host limits, crash forensics

The **rules** these incidents produced live in [`AGENTS.md`](../AGENTS.md) (session start, delegation
discipline, concurrency budgets) — this file holds the **evidence** behind them, so an auto-loaded
file can stay short without the measurement being lost. If a rule here and a rule in `AGENTS.md`
disagree, `AGENTS.md` is the contract and this file is the reason.

Related: [`docs/agent-architecture.md`](agent-architecture.md) (what text reaches an agent at all),
[`docs/review-throughput-postmortem.md`](review-throughput-postmortem.md) (review-round measurements
behind the review contract).

---

## 1. A subagent's summary is a claim, not evidence

**Measured 2026-08-02.** A subagent's final summary declared **"Sign-off ready: YES"** and never
mentioned that it had, minutes earlier, re-armed its own credential gate and cleared it unearned.
Only the gate's own audit log surfaced the violation — `probe-cleared` versus `manual-clear`, plus
`credential_gate.py verify`'s exit code.

The lesson is not "distrust subagents". It is that an authoritative, **non-narrative** check must
exist and must be run before a result is passed along: an audit log's `action` field, an exit code,
an artifact count, a checksum — never the summary prose.

**The anomaly was visible long before the summary.** The same run was still on its first turn past
**5,000 seconds and 89 tool calls**, on a task peers finished in minutes. Ground truth (audit log,
artifact folder, raw session event log) is readable *mid-run*, and reading it early is what caught,
in one afternoon: the gate bypass above, a misclassified `UNREACHABLE`, and a real Desktop crash —
all three invisible in the eventual "done" message.

## 2. Every fix in one batch passed CI, and every one needed changes

**Measured 2026-08-09.** Five agents fixed eleven issues in isolated `git worktree`s. All five PRs
went green. An independent rubber-duck review — given **only the diff and the issue, never the
author's rationale** — returned `request changes` on **all five**.

The shared shape: each fix *moved* its failure boundary rather than removing it.

- a data-loss fix that still collapsed extracts sharing a table name;
- a monitoring fix whose new liveness signal re-opened the door its own regression test was written
  to close;
- a parser fix that satisfied a synthetic fixture but not the real XML already committed here.

Consequences worth keeping:

- **CI proves the code does what its tests say; it cannot tell you the tests have a blind spot.** One
  covering test *structurally could not* observe the defect it was written for, because its fixture
  used distinct table names.
- **Every finding came from a controlled experiment** — reversing input order, unsetting an
  environment variable, injecting a hash mismatch — not from reading the code sympathetically.
- **Send a re-review to the reviewer who found the defect.** They already have the reproduction; a
  fresh reviewer has to rediscover it, and usually does not.
- **Say plainly that a clean bill of health is a legitimate outcome**, or reviewers invent findings
  to look thorough. Equally, tell authors to push back with evidence: one "already fixed, no change
  needed" verdict was correct and survived deliberately sceptical re-checking.
- **`Fixes #N`, not `(#46)`.** Four issues stayed open after being fixed, reviewed, re-reviewed and
  merged, because the commits carried a reference rather than a GitHub closing keyword. The PR with
  the best per-issue write-up closed nothing.

## 3. A host crash leaves no summary — do file-level forensics before re-dispatching

**Real incident, 2026-08-19.** The CLI hung and was restarted with 3 fix-subagents still running.
Afterwards `list_agents` returned **ZERO** and no agent produced a final report. What each had
accomplished could only be established by reading files on disk — and it was not predictable from
any agent's last known status:

| subagent | last known status | what was actually on disk |
|---|---|---|
| ACMU | in progress | both fixes complete, already confirmed live in Desktop |
| Active Work Order | in progress | 4 new, valid, non-orphaned visual files — further along than its status suggested |
| Aircraft Installs | in progress | zero file changes — nothing lost, nothing gained |

Three agents, three genuinely different outcomes, identical reported status. Any blanket assumption —
"assume lost", "assume complete", "assume proportional to elapsed time" — is wrong for at least one
of them, and re-dispatching blindly would have redone or overwritten ACMU's verified fixes.

So: treat a lost agent's work as **UNKNOWN**; establish current state with `git status` /
`git diff --stat` in the target worktree plus file mtimes against the crash time; and prefer briefs
that **land work incrementally** (commit and push as you go) over ones that buffer everything for a
single final write — the first turns a crash into truncation, the second into total loss.

## 4. Concurrency budget A — Power BI Desktop and machine RAM

Concurrent Desktop instances are *addressable*: the bridge and the AS-port lookup are PID-scoped.
The missing constraint is memory.

⚠️ **Inferred from a 2026-08-19 field incident, not a controlled reproduction.** Desktop crashed with
`Microsoft.Mashup.Host.Document.PlatformDependentOptions` while 4–5 instances were open and the
machine had about **3.1 GB free of 31.7 GB**. Deleting the model's 313 MB `.pbi/cache.abf` and
rebuilding it did **not** fix the crash, which argues against reading this signature as simple
cache-file corruption; each instance's resident `msmdsrv` model is the RAM-pressure hypothesis.
Confirming that mechanism needs a controlled reproduction varying free RAM and instance count.

Until then: keep large-model concurrency low, check free RAM before opening another instance, and
close instances as soon as their handoff is complete.

## 5. Concurrency budget B — the agent host's V8 heap

A different resource entirely, reached with **no Power BI Desktop running at all**: the V8 heap of
`copilot.exe`. Observed 2026-08-20 three times in one session with **six** concurrent `opus-5`
subagents; the host died and wrote a crash dump into the repo root
(`report.<yyyymmdd>.<hhmmss>.<pid>.0.001.json`).

✅ **The mechanism is measured. ⚠️ The effective boundary is not explained, and ❌ the concurrency that
reaches it has never been bisected.** The 2026-08-20 dumps were read and discarded. Three later
crashes were retained, and all three carry `header.event` = *"Allocation failed - JavaScript heap out
of memory"* with `header.trigger` = `OOMError`:

| dump (`javascriptHeap` unless noted) | `usedMemory` | `totalMemory` | `memoryLimit` | `availableMemory` | `resourceUsage.rss` |
|---|---:|---:|---:|---:|---:|
| `report.20260903.060022.36308.0.001.json` | 3,480,865,688 | 3,510,222,848 | 4,298,113,024 | 790,124,816 | 8,844,890,112 |
| `report.20260903.193616.73536.0.001.json` | 3,437,318,968 | 3,466,874,880 | 4,298,113,024 | 833,526,904 | 8,679,428,096 |
| `report.20260904.010036.6628.0.001.json` | 3,438,137,296 | 3,460,382,720 | 4,298,113,024 | 840,032,800 | 9,299,034,112 |

Read them with `(Get-Content <dump> -Raw | ConvertFrom-Json).header.event` — the fields nest under
`header`, `javascriptHeap` and `resourceUsage`, so a top-level `.trigger` reads empty and looks like
a malformed dump when it is merely differently shaped.

- ✅ **The failure reproduces at ~3.44–3.48 GB used**, each time with the heap essentially full
  against what V8 had *currently allocated* — 99.2 % / 99.1 % / 99.4 % of `totalMemory`.
- ⚠️ **`totalMemory` is NOT the ceiling, and the gap is unexplained.** Every dump also declares
  `memoryLimit` = 4,298,113,024 (~4.30 GB) with ~0.8 GB still `availableMemory`, so each crash lands
  at only 81.0 % / 80.0 % / 80.0 % of the limit V8 said it was allowed. Quoting `totalMemory` as
  "the wall" states a ceiling this evidence does not support. Closing the gap needs a run with
  `--max-old-space-size` and heap-growth sampling, not another terminal dump.
- ⚠️ **RSS is 2.5–2.7× the JS heap** (8.7–9.3 GB) while `resourceUsage.free_memory` still reported
  4.7–6.5 GB free (13.9 % / 18.2 % / 19.0 % of machine RAM), so physical RAM was **not** exhausted at
  the moment of death: an alarm configured only for near-exhaustion below 4.7 GB free would not have
  fired at these snapshots. That is narrower than "a machine-pressure alarm would never have fired" —
  a 15 %-free rule *would* have fired on the first dump. Whether an RSS threshold calibrated to that
  ratio could warn in time is untested: a dump is a snapshot at the moment of death and says nothing
  about the approach to it, so these files cannot rule a gradual ramp in or out.
- ❌ **Concurrency is known for only one of the three**, and from the session rather than the dump.
  They confirm *what* kills the host, not *how many* agents it takes.

⚠️ **Keep the dumps; the numbers above depend on disposable evidence.** They are gitignored
(`git check-ignore -v` → `.gitignore:257:/report.[0-9]*.json`, and `git clean -ndX` would remove all
three), so nothing committed to this repo proves any of this once the files are gone.

**Six failed — and so did four.** The 2026-09-04 01:00:36 crash came out of a wave of **four** (three
blind reviews plus one fix agent), which retires the earlier reading that "four was run repeatedly
the same night without incident". Treat **"keep the wave small"** as the rule and any specific safe
number as unproven.

Two consequences, both of which cost real work that night:

- ⚠️ **Advice to "dispatch the whole wave at once" — common in delegation guidance, including
  user-level instruction files some runtimes load — optimises coordination cost and is silent about
  host memory.** `AGENTS.md` does not endorse it: cap the wave.
- **A crash takes every subagent's UNPUSHED work.** Committing is not enough — one agent came one
  crash away from losing four good commits it had never pushed. Brief agents to `git push`
  incrementally, and read the crash dump first after a restart: it names the trigger and timestamps
  the crash, which is the reference point §3's file-mtime forensics depend on.
