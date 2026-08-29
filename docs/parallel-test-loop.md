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

Measured on 22 logical cores, machine shared with other agents throughout - so these wall times
carry real external load, which is the condition this loop exists for.

**Before the `timing` exclusion below** (2564 node ids):

| run | wall | outcome |
|---|---|---|
| serial, `pytest -q` | **681 s** | 3 failed, 2554 passed, 7 skipped |
| `-n auto --dist loadfile`, 8 runs | **160 - 305 s** | 7 identical to serial; 1 had one extra failure |

**After** (2568 node ids; tier 1 deselects 6):

| run | wall | outcome |
|---|---|---|
| tier 2, serial `pytest -q` | **646 s** | 3 failed, 2558 passed, 7 skipped |
| tier 1, 3 runs | **297 / 307 / 329 s** | 3 failed, 2552 passed, 7 skipped - **identical node id by node id** |

**2.0 - 4.3x faster**, depending entirely on what else the machine is doing. The three standing
failures are the deliberate `tests/test_upstream_repro_pins.py` engine-drift tripwires (engine pinned
2.260.0, installed 2.339.0); they fail identically on `master`.

The runs were compared **by node id**, not by summary totals, because a summary hides the failure
mode that matters: a test that skips or short-circuits under contention leaves `N passed` looking
healthy. Across the eight pre-change runs, exactly **one** node id ever disagreed (below). Across the
three post-change tier-1 runs, **none** did - and tier 2 differs from tier 1 by exactly the six
deselected `timing` tests and nothing else.

Why it parallelises so well: most of the ~2,500 tests spawn a subprocess
(`subprocess.run([sys.executable, ...])`), so the suite is process-spawn and I/O bound, not GIL
bound. Contention also matters - the same serial suite took 4m37s on a quiet machine and 12m20s with
nine agents competing, so shortening the window shortens everybody's.

## The one node id that disagreed, and what it was not

`test_credential_modal_detection.py::test_refresh_main_returns_credential_missing_fast_at_t0` took
**0.941 s against a 0.5 s budget** on worker `gw9`, in the slowest of the eight parallel runs (305 s,
against a 160-217 s median - the machine was busiest then). Its externals are all monkeypatched; the
assertion is purely a wall clock.

Follow-up experiments, to find out whether parallelism caused it:

| condition | samples | failures |
|---|---|---|
| whole suite, serial | 1 | 0 |
| whole suite, `-n auto --dist loadfile` | 8 | 1 |
| owning file only, serial, quiet machine | 15 | 0 |
| owning file only, serial, **beside a live 22-worker suite** | 15 | 0 |

So this is **not** a `--dist loadfile` isolation failure. No shared state raced; no fixture collided;
no file was split across workers. It is CPU starvation of a wall-clock assertion, and it needs the
test's own process to be one of 22 competing workers - running it serially beside 22 busy workers did
not reproduce it in 15 attempts.

That also makes it a **different hazard from the one this was expected to find** (see the live-test
section below), and it is the one that actually exists in the tree today.

## The `timing` marker: what tier 1 excludes, and why it is not silent

`pyproject.toml` registers three markers with deliberately narrow, different meanings:

| marker | means | applied |
|---|---|---|
| `slow` | takes a long time | by hand |
| `serial` | must not run beside another test wanting the same singleton external resource | by hand |
| `timing` | asserts a **sub-second wall-clock budget**, so a saturated box fails it | **automatically**, by the root `conftest.py` |

Six tests carry `timing`, all in `test_credential_modal_detection.py`, all asserting a 0.5 s or 1.0 s
budget. Tier 1 deselects them; **tier 2 still runs them**, and the deselection is visible in tier 1's
own summary line (`... 6 deselected`), so nothing disappears quietly.

Two looser budgets are deliberately **left in** the parallel tier, because a bigger margin is not the
same hazard and neither has ever been observed failing: `tests/test_credential_gate.py`'s 5.0 s hook
budget and `test_refresh_wall_clock.py`'s 15 s bound.

The list is hand-written node ids, which a rename would silently break - so
`tests/test_parallel_test_loop.py` re-derives the set from the suite's own AST (an
`assert <expr> < <number under 2>` whose left side reads `time.monotonic()`/`perf_counter()`) and
fails on drift in **both** directions. A new sub-second budget test added anywhere fails that gate
until it is listed.

**The durable fix is in the tests, not here.** A budget assertion should measure something it
controls - a monotonic floor, an injected clock, a call count - rather than its share of a contended
machine. This exclusion is an interim owned by the parallel tier, and the list should shrink.

## `--dist loadfile` is not optional, and the repo now enforces that

Plain `-n auto` means `--dist load`, which distributes **individual tests**, so two tests from one
file can run on different workers at the same time. `--dist loadfile` keeps every test in one file
on one worker, which is what makes module-scoped fixtures, per-file caches and shared on-disk
scratch safe. Every measurement above was taken **with** `loadfile`; `--dist load` has never been
measured here.

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

## Why tier 2 exists

**One green parallel run is not proof of isolation.** A test that quietly changed behaviour under
concurrency - took a different branch, skipped instead of running, degraded to a safer verdict - can
still report at the summary level as though nothing happened. `2554 passed` is the same string
whether a test asserted something or bailed out early. And tier 1 deliberately deselects the `timing`
tests, so it is a strictly smaller suite by construction - measured, 2562 node ids against tier 2's
2568.

So the plain serial `pytest -q` stays the gate of record before a PR. It is slower and it is the one
whose result you quote. Tier 1 buys iteration speed; tier 2 buys the claim.

When comparing two runs yourself, compare **per test**, not per total:

```bash
uv run pytest -q -m "not (serial or timing)" --junitxml=serial.xml
uv run pytest -q -n auto --dist loadfile -m "not (serial or timing)" --junitxml=parallel.xml
```

then diff the `classname`/`name`/outcome triples. A skip that replaced a pass is invisible in the
summary line and obvious in the XML.

## The `serial` marker, and the live-test hazard

Some tests contend for a **singleton external resource**: a real Power BI Desktop instance, its
UI-Automation tree, a live credential dialog. Two of them running at once degrade each other. This
is measured, not theoretical (issue #387):

- running the all-skills suite concurrently with the credential suite made one live harvest degrade
  to `DIALOG_UNREADABLE` and fail its stricter assertion; rerunning without the contention passed;
- the same bundle ran **100 passed** alone, and **skipped** one live test when run beside the full
  skills suite.

Note the shape: contention pushes those tests toward the **fail-safe**, not toward a false pass -
and it is already reachable **serially**, whenever two agents run pytest at the same time.
Parallelism inside one invocation is the same hazard with a shorter fuse.

A test opts in with `@pytest.mark.serial`. Tier 1 already excludes them, so the marker takes effect
the moment it is applied. Run them on their own afterwards:

```bash
uv run pytest -q -m serial
```

**Exit code 5 from that command means no test carries the marker yet** - that is "nothing to run",
not a failure.

**Current status, stated plainly:** no test in the tree carries `serial`, and no tracked test drives
a real Desktop or a real UI-Automation tree (`DIALOG_UNREADABLE` appears in zero tracked test files).
The live tests the quotes above describe are in flight against issue #367 in the `pbip-model-refresh`
bundle and are **not on `master`**, so that hazard **could not be reproduced here** and the `serial`
marker is unapplied scaffolding. When those tests land, marking them is a one-line change per test
and needs nothing from this page.

⚠️ One residual gap worth knowing: the exclusion only happens if the command carries
`-m "not (serial or timing)"`. A hand-typed `pytest -n auto --dist loadfile` still runs everything.
The doc-drift tests keep every *documented* command honest; they cannot police what you type.

## CI stays serial, deliberately

`.github/workflows/checks.yml` keeps `uv run pytest -q`. Both jobs - `ubuntu-latest` and the
`windows-latest` bundle job - are GitHub-hosted standard runners, which for a public repository are
**4 vCPU**, so `-n auto` would pick 4 workers rather than an absurd number. That makes parallel CI
*plausible*, not *measured*:

- every xdist worker re-imports and re-collects all ~2,560 tests, and that fixed cost is a much
  larger fraction of the total on 4 cores than on 22;
- the one defect this work found is **CPU starvation of a wall-clock assertion**. A 4-vCPU runner
  running 4 workers is exactly the shape that makes that worse, and CI is where a flaky red costs
  the most;
- CI is the shared trust anchor. A flaky gate is worse than a slow one for the same reason tier 2
  exists;
- the cost this issue is about is the **agent iteration loop on a developer machine**, which is
  where the 3.4x lands. CI wall time was never the complaint.

If CI time does become the complaint, the change is one line per job plus a measurement on the
runners themselves - and the root-`conftest.py` guard already makes it impossible to add `-n` there
without `--dist loadfile`.
