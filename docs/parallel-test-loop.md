# The two-tier test loop

Use one checkout/worktree and `.venv` per campaign. Install the declared dev dependencies with
`uv sync --all-extras`, then run from that worktree's root:

```bash
# Tier 1: supported parallel iteration command
uv run pytest -q -n auto --dist loadfile -m "not (serial or timing)"

# Tier 2: full serial suite, the gate of record
uv run pytest -q
```

**A green parallel subset is not a full-suite pass.** Quote the serial result for code changes;
documentation-only PRs follow the applicable checks in [CONTRIBUTING](../CONTRIBUTING.md#before-you-open-a-pr).
Neither default command exercises opt-in GUI tests. There is no accepted standing-failure count,
fixed skip total, or promised speedup: compare the same revision, environment and node IDs.

## What each invocation actually selects

The authority is [root `conftest.py`](../conftest.py), together with
[`pyproject.toml`](../pyproject.toml). `testpaths` includes both `tests` and `.github/skills`.

| Reason key | Parallel tier | Plain serial tier |
|---|---|---|
| `serial`: singleton external resource, such as the interactive desktop/UI Automation | Deselected by the collection hook | Selected unless another exclusion, notably `gui`, applies |
| `timing`: wall-clock assertion vulnerable to machine load | Deselected by the collection hook | Selected unless also `gui` |
| `gui`: opens a real top-level window | Deselected without explicit GUI opt-in | Also deselected without GUI opt-in |
| `slow`: duration, not exclusivity | Included; the supported filter does not exclude it | Included |

The xdist hook removes `serial` and `timing` **inside workers**, even if the caller omits or changes
`-m`. Keep the documented filter anyway. `--include-contended` deliberately bypasses that hook;
it is a diagnostic override, not another supported baseline. It does **not** enable `gui`, and an
explicit marker filter still applies. Markers are not locks between separate pytest processes.

`--dist loadfile` keeps all tests from one file on one worker; it does not isolate different files,
different pytest processes, or machine-wide resources. Other parallel schedulers are refused with
`pytest.UsageError`, **exit 4**. For example, this is a rejected invocation, not a product test failure:

```text
uv run pytest -q -n auto
```

[`tests/test_parallel_test_loop.py`](../tests/test_parallel_test_loop.py) checks the guard, source
markers and documented commands. The copied-bundle test in
[`tests/test_skills.py`](../tests/test_skills.py) propagates the parallel marker filter into its
nested, serial pytest process: that child still shares the outer run's CPU load.

### GUI coverage is separate and Windows-specific

On an available Windows desktop, deliberately run the GUI tier **without xdist**:

```bash
uv run pytest -q --run-gui -m gui
```

`T2P_RUN_GUI=1` also opts in where supported; keep it unset for the ordinary tiers. The copied-bundle
portability test removes ambient GUI opt-in. `-m gui` alone cannot bypass the hook and can leave no
tests selected; **exit 5 is not a pass**. `--run-gui -m serial` reaches only the GUI/serial overlap,
not every GUI test. Use `--collect-only` to inspect selection without opening windows, not to claim
that UI Automation ran.

The platform checks and actual skip reasons still apply after opt-in. A Linux pass cannot establish
Win32/UIA behavior. Coordinate with other users/agents before opening windows; a worktree does not
provide a private interactive desktop.

## Expected skips are reason-keyed, not a target count

Deselection removes a collected test from the run; a skip reports a check that did not complete.
Neither is a pass. The exact accepted strings/prefixes live in `EXPECTED_EXACT_SKIP_REASONS` and
`EXPECTED_PREFIX_SKIP_REASONS` in [root `conftest.py`](../conftest.py); do not maintain another count
or broaden a reason to make a run green.

| Exact reason or registry key | What remains untested / how to interpret it |
|---|---|
| `probe_desktop_credential.ps1 is a Windows-only UI Automation arbiter`; `os.mkfifo is POSIX-only` | Platform-specific behavior cannot run on the current host. Keep Windows and non-Windows evidence distinct. |
| `the TMDL oracle needs the .NET SDK; scripts/preflight.ps1 checks for it` | The parser oracle did not execute; a skip is not model-validation evidence. |
| `powerbi-report-author not installed (npm bridge CLI; absent on Linux CI)` | The bridge-backed check did not execute. Schema-fetch failures have their own registered reason, not this one. |
| `no real cache.abf on this machine (they are gitignored); set PBIP_REFRESH_REAL_ABF` | Real-cache controls lack their input; synthetic controls do not replace them. |
| Other exact filesystem, PowerShell, fixture or environment reasons in the registries | Check the emitted reason against the actual prerequisite. Only the listed prefixes permit a variable suffix. |
| `deterministic tier not installed` and the registered canonical-engine-constants reasons | Engine coverage is absent, not an ordinary green skip baseline; use the serial engine checks described below. |

Default reporting is `-rfsE`; failures/errors remain visible even if a caller supplies `-rs`.
An **unregistered skip reason makes the session fail, exit 1**, including under xdist. An accepted
skip can coexist with exit 0, so record node ID **and reason**, not only the process exit.
[`tests/test_expected_skips_gate.py`](../tests/test_expected_skips_gate.py) contains positive and
negative controls for these rules.

### CI remains serial; engine coverage is explicit

The commands in [`.github/workflows/checks.yml`](../.github/workflows/checks.yml) are unchanged:

- The ordinary Linux job runs the full serial suite. It permits engine tests to be **NOT_CHECKED**
  only with `T2P_ENGINE_TESTS_NOT_CHECKED_REASON=covered-by-pinned-engine-integration-job` and the
  exact reviewed node-ID-to-reason map, not merely the same number of skips.
- The separate pinned-engine integration job sets `T2P_REQUIRE_ENGINE_TESTS=1`; skipped engine
  dependencies fail that serial run. The scheduled/manual current-engine job is a separate drift
  signal, not an interchangeable baseline.
- Windows runs the refresh bundle serially, signing-policy controls, and the opt-in GUI command
  above over both test roots.

Do not copy the Linux job's NOT_CHECKED reason into a local run to hide a missing engine. Quote
serial engine evidence, not an xdist green summary, when claiming engine coverage.

## Contention and failure triage

Record the exact command, revision, OS, Python/pytest/xdist and engine versions, relevant environment
overrides, selected node IDs, reasons, exit code and whether another campaign was active.
Append `--junitxml=<run-owned-report-path>` when comparing runs; compare each test's
`classname`/`name`/outcome/**skip reason**, allowing only explained selection differences.
Real xdist output need not aggregate worker deselections into a summary count.

| Signature | Interpretation and next step |
|---|---|
| Assertion or gate failure on the same node serially | Actionable failure, not an accepted parallel baseline. Preserve the assertion and reproduce on the same base/environment before attributing it to this change. |
| Wall-clock bound fails only under load | Timing-sensitive diagnostic, not proof of a product defect or permission to widen the threshold. Reproduce the exact node serially on a quiet machine. |
| UIA harvest is `INCOMPLETE` / verdict `DIALOG_UNREADABLE` | Expected **non-clean degradation** under contention, not proof that contention caused this instance. The probe could not establish the dialog's state. A test requiring a stronger verdict must fail; neither a credential diagnosis nor a clean pass was earned. Reproduce serially with exclusive GUI access. |
| Collection/import error, missing prerequisite, changed filters/root directory or unsupported scheduler | Establish comparable invocation/environment first. No test assertion may have run; do not classify it as a timing flake or retry until green. |

The timing sighting in [issue #415](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/415)
names this `timing`-marked node:

```text
.github/skills/pbip-model-refresh/tests/test_credential_modal_detection.py::test_refresh_main_returns_credential_missing_fast_at_t0
```

The separately reported UIA-contention node carries both `serial` and `gui`:

```text
.github/skills/pbip-model-refresh/tests/test_credential_modal_detection.py::test_credential_text_beyond_the_element_cap_convicts_when_the_cap_allows_it
```

These are different mechanisms. A quiet serial pass does not prove that concurrent GUI campaigns
are safe, and this documentation does not authorize retries or changed timing thresholds.

### Shared resources that file grouping does not isolate

- **Checkout and scratch:** use separate worktrees and per-run scratch/basetemp roots. Concurrent
  processes must not share a writable fixture/cache or a basetemp directory. Moving scratch under
  a checkout can also change nested pytest configuration discovery and node IDs; disclose that
  difference rather than comparing it as the same baseline.
- **Ports and Desktop:** the [Tableau mock](../tests/mocks/tableau.py) requests an ephemeral
  loopback port, not a fixed shared port. That says nothing about live Desktop/Analysis Services
  endpoints: use the intended PID/port binding, never another run's instance. CPU, RAM, windows and
  UIA remain machine-wide even across worktrees.
- **Engine and oracle:** a worktree's `.venv` does not isolate the installed canonical conversion
  plugin. Keep its version fixed during a comparison. The TMDL oracle's first-build lock is
  **completed in [PR #529](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/pull/529)**;
  do not diagnose every oracle failure as that old race. The separate failed-build-generation
  availability question was [closed pending field evidence (#539)](https://github.com/Guust-Franssens/tableau-to-powerbi-migration/issues/539).
  An unavailable oracle still is not a clean parser verdict; retain its diagnostic and reproduce serially.

### Unattributable-connection sighting: unconfirmed

```text
tests/test_check_unit.py::test_cli_model_scope_reports_not_checked_for_unattributable_connection_fixture
```

Issue #415 records a full-parallel sighting, unsuccessful controlled serial/xdist-only reproduction
attempts, and another full baseline that did not reproduce it. The earlier claim here of a
deterministic same-checkout race is **not an established cause**. The fixture helper does write a
cache in a shared fixture directory, which is a reason to isolate worktrees, not proof of causation.

The September 18, 2026 recheck at `7607217826c2dac57e5b5090390ec84b9c2a4e52` also passed this exact
node in serial and loadfile-parallel representative runs. Those were **not simultaneous whole-suite
campaigns** and establish neither a full-suite pass nor a fix. Keep the sighting unconfirmed unless
a controlled reproduction distinguishes its cause; do not add a retry, marker or expected failure.
