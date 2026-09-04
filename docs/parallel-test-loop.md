# The two-tier test loop

The full suite is the dominant cost in every agent iteration, and agents run it repeatedly. This page
is the measured answer to "how do I run it fast without making it lie to me?".

**Short version:**

```bash
# Tier 1 - fast loop, run this while you iterate
uv run pytest -q -n auto --dist loadfile -m "not (serial or timing)"

# Tier 2 - pre-PR gate, run this once before you open the PR
uv run pytest -q
```

Both must be green. Tier 1 is not a substitute for tier 2, and the reason is below.

---

## The measurement

`pytest-xdist` is a declared **dev dependency** (`[project.optional-dependencies].dev` in
`pyproject.toml`), so `uv sync --all-extras` puts it in every worktree venv. Agents each work in
their own `git worktree` with their own `.venv`; anything not declared there is absent everywhere.

Measured on 22 logical cores, machine shared with other agents throughout - so these wall times carry
real external load, which is the condition this loop exists for. 2702 node ids; tier 1 deselects 14.

| run | wall | outcome |
|---|---|---|
| tier 2, serial `pytest -q` | **789 s** | 3 failed, 2692 passed, 7 skipped |
| tier 1, documented command | **239 s** | 3 failed, 2678 passed, 7 skipped |
| tier 1, **bare `-n auto --dist loadfile`, no `-m` at all** | **235 s** | identical to the row above, **node id by node id** |

**~3.3x faster**, and the last row is the point of the mechanism below: forgetting the filter no
longer changes what runs.

⚠️ **The "three standing failures" baseline is RETIRED as of 2026-09-03 (issue #486). The expected
number is now ZERO.** Re-brief anyone working against the old figure — including the later
"expect exactly six" variant, which was these three plus three in
`tests/test_issue_424_chart_type_pin.py`.

The three recorded here were described as *"the deliberate `tests/test_upstream_repro_pins.py`
engine-drift tripwires (engine pinned 2.260.0, installed 2.339.0); they fail identically on
`master`"* — and that framing is the defect, not the record of it. `_run_engine_once()` `assert`ed
the pinned version, and **every** behaviour pin in that file reaches the engine through it, so a
version bump did not report drift: it made the #166, #168 and #171 pins fail *before their own
assertions ran*. Those three behaviours were therefore **unevaluated** from the moment the plugin
passed 2.260.0, while still costing three red tests that everyone had been taught to expect. Version
drift is now a non-fatal `UserWarning`; with the gate removed, #166 and #171 **pass** (both still
OPEN upstream) and #168 had genuinely been fixed without anyone noticing.

The generalisable lesson, and the reason this is written here rather than only in a commit message:
**an "expected failures" baseline converts a designed alarm into background noise.** The `#424` pin's
own failure text said *"this failing may mean upstream FIXED #424; confirm the emitted type is
lineChart, then retire this pin."* It was correct, it was sitting in the output, and it was skipped
for days because the surrounding instruction said to expect it. A permanently-red test does not warn;
it trains. If a pin is expected to fail, either fix it or delete it — do not document the failure.

Current expectation: `tests/test_upstream_repro_pins.py` and `tests/test_issue_424_chart_type_pin.py`
report **30 passed, 0 failed** on canonical engine 2.356.0.

Every run was compared **by node id**, not by summary totals, because a summary hides the failure
mode that matters: a test that skips or short-circuits under contention leaves `N passed` looking
healthy. Three campaigns were run in total - before the live UI tests landed on `master`, after they
landed, and again after the fixes below.

⚠️ **Sequential runs are not a hard enough test.** Two of the three real defects found here appear
only when **two whole-suite parallel runs are started concurrently** - the condition two agents on
one machine actually create. If you re-measure, measure that.

## The hazard that is real: wall-clock budgets, not shared state

Across eight parallel runs of the first campaign, exactly **one** node id ever disagreed:
`test_credential_modal_detection.py::test_refresh_main_returns_credential_missing_fast_at_t0`, which
took **0.941 s against a 0.5 s budget** on worker `gw9`. Isolation experiments:

| condition | samples | failures |
|---|---|---|
| whole suite, serial | 1 | 0 |
| whole suite, `-n auto --dist loadfile` | 8 | 1 |
| owning file only, serial, quiet machine | 15 | 0 |
| owning file only, serial, **beside a live 22-worker suite** | 15 | 0 |

So this is **not** a `--dist loadfile` isolation failure. No shared state raced; no file was split
across workers. It is CPU starvation, and it needs the test's own process to be one of 22 competing
workers.

**Widening the bound is not a fix.** That operation takes **0.03 s** serially. A 0.5 s budget is
already 16x headroom, and it still blew at 0.941 s - a **31x** inflation of a very short measured
window, which is scheduler starvation of a short sample rather than proportional slowdown. There is
no bound that is both tight enough to catch the regression it exists for and loose enough to survive
a saturated box. Excluding it from the contended tier is the honest answer.

## The `timing` marker, and where it has to live

`pyproject.toml` registers three markers with deliberately narrow, different meanings:

| marker | means | applied |
|---|---|---|
| `slow` | takes a long time | by hand |
| `serial` | must not run beside another test wanting the same singleton external resource | by hand, in the test's own source |
| `timing` | asserts a **wall-clock budget**, so a saturated box fails it | by hand, in the test's own source |

Seven tests carry `serial` and seven carry `timing`. Tier 1 deselects both; **tier 2 still runs
them**, and the deselection is visible in tier 1's own summary (`... 14 deselected`).

`timing`'s automatic floor is **sub-second**: every test asserting a budget under 2 s must carry it,
gated from the suite's AST. A larger budget earns the marker on measured evidence instead - the 10 s
process-rendezvous barrier in `test_check_migration_progress.py` did, after failing under 44 workers.
Two budgets are deliberately **left in** the parallel tier, having never been observed to fail:
`tests/test_credential_gate.py`'s 5.0 s hook budget and `test_refresh_wall_clock.py`'s 15 s bound.

Three things make the marker hold up, and each is gated by `tests/test_parallel_test_loop.py`:

1. **The marker is in the test's own source**, not applied from outside. `test_parallel_test_loop.py`
   re-derives the budget set from the suite's **AST** (an `assert <expr> < <number under 2>` whose
   left side reads `time.monotonic()`/`perf_counter()`) and fails when any of them lacks
   `@pytest.mark.timing`. A new budget test added anywhere fails that gate until it is marked.
2. **The bundle registers `timing` in its own `conftest.py`.** `pbip-model-refresh` is meant to be
   copied into another repo, where this repo's `pyproject.toml` does not exist; without local
   registration the copied bundle raises an unknown-mark warning and `-m "not timing"` quietly stops
   meaning anything. Verified by copying the folder out and running it standalone: `235 passed,
   3 skipped, 6 deselected`, with `PytestUnknownMarkWarning` promoted to an error.
3. **`tests/test_skills.py` propagates the exclusion into its nested run** - see below.

**The durable fix still belongs in those tests**, which should assert against something they control
(a call count, an injected clock, a monotonic floor) rather than their share of a contended machine.
This exclusion is an interim, and the list should shrink.

### The nested run: where the first draft of this leaked

`tests/test_skills.py` copies each skill bundle to a temp directory and runs its tests there, to
execute the portability promise rather than assert it. That nested pytest is **serial**, but its
process competes with every other xdist worker - and the copied tree contains no root `conftest.py`
and no `pyproject.toml`, so an exclusion expressed at repo level **structurally cannot reach it**.

Measured: in one concurrent-pair stress run, the outer suite correctly deselected
`test_refresh_main_returns_credential_missing_fast_at_t0`, and the nested copy of that same test
failed at **0.519 s** against its 0.5 s budget. Tier 1 was still ~1-in-7 flaky, through a different
door. The fix moves the failure boundary unless the nested run inherits the filter, so it does -
conditional on `PYTEST_XDIST_WORKER`, which is set only inside an xdist worker (verified: `None`
serially, `'gw0'` under `-n 2`). Tier 2 therefore still executes every test the bundle ships.

## The live Power BI Desktop / UI-Automation tests: measured, and they DO need `serial`

`.github/skills/pbip-model-refresh/tests/` carries **seven live tests** that launch a real WPF
application and drive it through UI Automation, including a wedged-UI-thread regression whose whole
point is a `< 25 s` wall-clock bound. Two independent reports had them degrading under contention -
one live harvest returning `DIALOG_UNREADABLE`, and one live test skipping when run beside the full
skills suite.

**The first verdict here was that they were stable, and it was wrong.** Across seven parallel runs -
three sequential, plus two concurrent pairs - not one of them failed or skipped, and this page said
so. Three *more* concurrent pairs found the failure:

| condition | runs | live-test failures |
|---|---|---|
| tier 1, sequential | 3 | 0 |
| tier 1, concurrent pairs (44 workers on 22 cores) | 8 | **3** |

`test_credential_text_beyond_the_element_cap_convicts_when_the_cap_allows_it` failed in **3 of 6**
runs of the later rounds - twice in the *same pair*, both sides at once - every time like this:

```
a dialog was up during the refresh: ... size=650x400 harvest=INCOMPLETE
  it matched no credential-prompt signature, so this is NOT a credential wall
  but a window we could not account for was open
VERDICT: DIALOG_UNREADABLE
```

That is the documented hazard verbatim: the UIA harvest could not finish, the probe **degraded
safely**, and the test's stricter assertion correctly refused the degraded verdict. The test took
**30.6 s against 8.08 s serially**. Three sequential runs would have shipped this as "stable" - the
condition it needs is *two live suites at once*, which is exactly what two agents on one machine do.

So all seven now carry **`@pytest.mark.serial`**. Marking only the one that was observed would move
the boundary rather than remove it - they share one mechanism and one singleton resource. Run them
deliberately, on their own:

```bash
uv run pytest -q --run-gui -m serial
```

⚠️ `--run-gui` is not optional here, and `-m serial` alone will report *fewer* tests than you expect.
All seven also carry **`gui`** (issue #447: they open a real top-level window and steal focus), and
the root `conftest.py` deselects `gui` in every tier unless asked. Leave the flag off and you get the
`serial` tests that are not `gui` - a green run that ran none of the live UI regressions.

⚠️ `--dist loadfile` is what keeps them from racing *inside* one run (all seven live in one file, so
one worker owns them). It cannot help *across* runs, and neither can any scheduler flag. The marker
is the only lever.

A second, unrelated test was caught in the same rounds and is marked `timing`:
`test_declare_wrapper_concurrent_writers_keep_both_declarations` gives two child processes **10 s**
to reach a rendezvous barrier, and under 44 workers they did not both start in time. Its budget is
far above the sub-second rule below, so it is marked on **measured** evidence rather than by the
automatic gate.

## `--dist loadfile` is not optional, and the repo now enforces that

Plain `-n auto` means `--dist load`, which distributes **individual tests**, so two tests from one
file can run on different workers at the same time. `--dist loadfile` keeps every test in one file on
one worker - which is exactly what keeps the seven live UI tests above from running beside each
other. Every measurement here was taken **with** `loadfile`; `--dist load` has never been measured.

`-n auto` is shorter to type than `-n auto --dist loadfile`, so the root `conftest.py` refuses it:

```
$ uv run pytest -q -n auto
ERROR: pytest-xdist is active with --dist 'load'. This suite is only measured safe under
--dist loadfile (issue #387).
```

That is a `pytest.UsageError`, **exit code 4**. The guard is at the repo root, not in
`tests/conftest.py`, because `pyproject.toml` sets `testpaths = ["tests", ".github/skills"]` and a
conftest only covers paths beneath it - a `tests/`-only guard would leave
`pytest .github/skills/pbip-model-refresh/tests -n 4` ungated, which is the single run that most
needs gating.

To use a different scheduler, edit the guard deliberately and re-measure. That is the point of it.

## The markers are enforced by a MECHANISM, not by remembering to type them

A marker on its own is a convention. It only protects anything if the caller supplies
`-m "not (serial or timing)"` - and the guard above validates `--dist loadfile`, not the filter. Drop
half of it, and every live UI test is collected again:

```
$ pytest --collect-only -n 2 --dist loadfile -m "not timing" <the bundle's credential tests>
94/100 tests collected (6 deselected)     # <- all seven live WPF/UIA tests SELECTED
```

So the root `conftest.py` now **deselects every `serial` and `timing` test whenever xdist is active**,
whatever `-m` says. The same command today reports `87/100 tests collected (13 deselected)`, and the
documented tier-1 filter is belt-and-braces rather than the only line of defence.

Deliberately stress-testing those tests under parallelism is still possible, explicitly:

```bash
uv run pytest -q -n auto --dist loadfile --include-contended
```

That flag exists for measuring this behaviour, not for normal use - it re-enables exactly the
configuration measured to fail.

Two details make the mechanism hold up, and both are gated:

- **It must fire inside the workers.** A worker sees `numprocesses=None dist='no'` (measured), so a
  check on `numprocesses` alone would deselect nothing in a real run while still looking correct
  under `--collect-only`, which the controller can answer.
- **The documentation gate parses the marker expression** with pytest's own compiler and asks whether
  it excludes each marker. A substring check could not tell `not (serial or timing)` from
  `not timing`: the earlier draft of that gate returned `3 passed` for the mutated command.

## Why tier 2 exists

**One green parallel run is not proof of isolation.** A test that quietly changed behaviour under
concurrency - took a different branch, skipped instead of running, degraded to a safer verdict - can
still report at the summary level as though nothing happened. And tier 1 deliberately deselects the
`serial` and `timing` tests, so it is a strictly smaller suite by construction: 2688 node ids against
tier 2's 2702, plus fourteen more inside the nested bundle run.

So the plain serial `pytest -q` stays the gate of record before a PR. It is slower and it is the one
whose result you quote. Tier 1 buys iteration speed; tier 2 buys the claim.

When comparing two runs yourself, compare **per test**, not per total:

```bash
uv run pytest -q -m "not (serial or timing)" --junitxml=serial.xml
uv run pytest -q -n auto --dist loadfile -m "not (serial or timing)" --junitxml=parallel.xml
```

then diff the `classname`/`name`/outcome triples. A skip that replaced a pass is invisible in the
summary line and obvious in the XML.

## One thing this loop does NOT fix: two pytest processes in ONE working tree

Running two concurrent whole-suite campaigns in the *same checkout* also surfaced
`tests/test_check_unit.py::test_cli_model_scope_reports_not_checked_for_unattributable_connection_fixture`
failing on the **second** process, deterministically, 3 pairs out of 3 - while the first passed every
time. That is not a parallelism defect and no marker or scheduler flag can address it:
`_freshen_clean_fixture_cache()` mutates a **git-tracked fixture inside the repo**
(`tests/fixtures/check-unit-clean-integration/.../cache.abf`) and stamps it with a 60-second future
mtime. Two processes sharing that path overwrite each other's freshness window. `--dist loadfile` is
irrelevant: only one file touches that fixture, so it never leaves a single worker within a run.

**It does not affect the documented workflow**, because agents each work in their own `git worktree`
with their own `.venv`, and that fixture path is then not shared. It is recorded here so the next
person who reproduces it does not spend an afternoon blaming xdist. If you deliberately run two
suites in one checkout, expect it.

## The `serial` marker

`serial` means a test contends for a **singleton external resource** - a real Power BI Desktop
instance, its UI-Automation tree, a live credential dialog - such that two of them running at once
degrade each other. A test opts in with `@pytest.mark.serial`. Seven tests carry it today, all of
them the live WPF/UIA regressions measured above. Run them deliberately, on their own, when you have
touched the credential probe:

```bash
uv run pytest -q --run-gui -m serial
```

That is not part of either tier. Tier 1 excludes them because two agents running tier 1 at once
raced them; **tier 2 no longer includes them either.** All seven also carry `gui` (issue #447), and
the root `conftest.py` deselects `gui` unless `--run-gui` / `T2P_RUN_GUI=1` asks for it - so a plain
`pytest -q` never opens a window on the machine you are working on, and the command above (or CI's
Windows leg) is now the only thing that runs them. That is a deliberate trade: the live regressions
were never observed to fail in a serial whole-suite run, but a serial whole-suite run is also what an
agent types twenty times a day beside a human who is using the desktop.

⚠️ **Absence of failure in a short campaign proves nothing here.** Seven runs - including two
concurrent pairs - found none of it. It took eight concurrent pairs to see three failures. The
instability is **real but rare**, so "I ran it three times and it was fine" is not evidence that the
markers can come off. If you want to challenge them, use `--include-contended` and run *many*
concurrent pairs.

The exclusion no longer depends on anyone typing the right filter - the root `conftest.py` deselects
`serial` and `timing` whenever xdist is active (see above). A hand-typed
`pytest -n auto --dist loadfile` is therefore safe too.

## CI stays serial, deliberately

`.github/workflows/checks.yml` keeps `uv run pytest -q`. Both jobs - `ubuntu-latest` and the
`windows-latest` bundle job - are GitHub-hosted standard runners, which for a public repository are
**4 vCPU**, so `-n auto` would pick 4 workers rather than an absurd number. That makes parallel CI
*plausible*, not *measured*:

- every xdist worker re-imports and re-collects ~2,690 tests, and that fixed cost is a much larger
  fraction of the total on 4 cores than on 22;
- the defect this work found is **CPU starvation of a wall-clock assertion**. A 4-vCPU runner running
  4 workers is exactly the shape that makes that worse, and CI is where a flaky red costs the most;
- CI is the shared trust anchor. A flaky gate is worse than a slow one for the same reason tier 2
  exists;
- the cost this issue is about is the **agent iteration loop on a developer machine**, which is where
  the 3.6x lands. CI wall time was never the complaint.

If CI time does become the complaint, the change is one line per job plus a measurement on the
runners themselves - and the root-`conftest.py` guard already makes it impossible to add `-n` there
without `--dist loadfile`.

## The `gui` marker — a THIRD exclusion, and it is not part of either tier

`serial` and `timing` are about the **parallel** tier: they are excluded from tier 1 and were
restored by tier 2. `gui` is different in kind, so it is a different marker and a different
mechanism. A `gui` test spawns a **real top-level window**, which steals focus on whatever machine
runs it. Issue #447 is an operator watching the `Fake Desktop` WinForms app "opening and closing for
the last 2 days" mid-demo, because `testpaths` includes `.github/skills` and a bare `pytest`
collected it.

Ten tests carry it: the seven live WPF/UIA regressions above, plus three that create native
`WS_VISIBLE` windows with `CreateWindowExW`. They must keep opening a window - UI Automation cannot
be exercised against a mock - so the fix is opt-in, not weaker tests:

```bash
uv run pytest -q --run-gui -m gui     # or T2P_RUN_GUI=1, which a nested pytest can inherit
```

⚠️ **It is a collection hook, NOT `addopts = "-m 'not gui'"`, and the difference is measured.** A
command-line `-m` **replaces** an ini marker expression instead of composing with it. Under `addopts`,
on the bundle's 309 tests:

| command | selected |
|---|---|
| default | 302/309 |
| `-m gui` | 7/309 |
| `-m "not slow"` | **309/309 — every window back** |
| `-m "not something_else"` | **309/309** |

`-m "not slow"` is documented in `docs/offline-mock-harness.md`, so following the repo's own
instructions re-opened the windows. `pytest_collection_modifyitems` runs *after* pytest has applied
the caller's `-m`, so nothing a caller types can re-enable them; only the opt-in can.

The same hook is duplicated in `.github/skills/pbip-model-refresh/tests/conftest.py`, because
`tests/test_skills.py` copies that bundle out of the repo and runs a nested pytest where neither the
root `conftest.py` nor `pyproject.toml` exists. Measured with every spawn site instrumented to raise:
that nested run reached **all ten** of them, and the outer summary reported **zero** deselections.

CI's `windows-latest` leg runs `uv run pytest -q --run-gui -m gui` with **no path argument**, so
`testpaths` applies and it covers the whole repository. `tests/test_gui_marker_gate.py` executes that
very command under `--collect-only` and fails if it does not reach every `gui` test in the repo.
