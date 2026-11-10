"""Gates for the two-tier test loop (issue #387).

The loop is only worth having if it cannot silently decay into something faster and wrong. Three
things can decay independently, so each gets its own gate:

1. `pytest-xdist` can drop out of `pyproject.toml`, and every worktree venv then loses it silently -
   agents run `uv sync` per worktree, so an undeclared tool is absent everywhere at once.
2. The root `conftest.py` guard can be neutered, and `-n auto` (which means `--dist load`) starts
   spreading a single file across workers - the configuration nobody measured.
3. `docs/parallel-test-loop.md` can drift from the guard, which is worse than having no doc: an
   agent copies a documented command that the guard then rejects, and concludes the guard is broken.

The nested-pytest tests below deliberately copy the REAL root `conftest.py` bytes into a scratch
tree rather than importing it, so they exercise the file an operator actually gets.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import operator
import os
import re
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from _pytest.mark.expression import Expression

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_CONFTEST = REPO_ROOT / "conftest.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOOP_DOC = REPO_ROOT / "docs" / "parallel-test-loop.md"

TRIVIAL_TEST = "def test_ok():\n    assert True\n"

# A budget above this is not the hazard: `assert elapsed < 15` has room for a loaded box, and was
# never observed failing. The two loosest budgets in the suite (5.0s and 15s) sit above it on
# purpose, and stay in the parallel tier.
SUB_SECOND_CEILING = 2.0
CLOCK_FUNCTIONS = {"monotonic", "perf_counter"}

# A budget above SUB_SECOND_CEILING earns `timing` only on measured evidence, never by inference.
# This one gives two child processes 10s to reach a rendezvous barrier; under two concurrent
# whole-suite parallel runs (44 workers on 22 cores) they did not both start in time (issue #387).
MEASURED_LARGER_BUDGETS = frozenset(
    {
        "tests/test_check_migration_progress.py::test_declare_wrapper_concurrent_writers_keep_both_declarations",
        # #741: the 0.1s API deadline is an implicit rendezvous budget, not an elapsed assertion.
        # It failed before worker entry under concurrent validation; the quiet tier passes.
        ".github/skills/pbip-model-refresh/tests/test_canary_data_gate.py::"
        "test_read_result_waits_for_worker_termination_not_queue_publication",
    }
)

BUNDLE_TESTS = ".github/skills/pbip-model-refresh/tests/test_desktop_observer.py"


def _pyproject() -> dict:
    """Parsed `pyproject.toml`."""
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _scratch_suite(root: Path, *, nested: bool) -> Path:
    """Lay out a miniature suite under `root` carrying the real root-conftest bytes.

    `nested` puts the test file two directories down, mimicking `.github/skills/<skill>/tests/` -
    the second entry in `testpaths`, and the one a `tests/conftest.py` would not reach.
    """
    shutil.copy2(ROOT_CONFTEST, root / "conftest.py")
    target = root / "skills" / "bundle" / "tests" if nested else root
    target.mkdir(parents=True, exist_ok=True)
    (target / "test_scratch.py").write_text(TRIVIAL_TEST, encoding="utf-8")
    return target


def _run_pytest(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run a nested pytest, insulated from this run's own options."""
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    # Under an outer xdist run a worker's env would make the nested run look distributed too.
    for key in [k for k in env if k.startswith("PYTEST_XDIST_")]:
        env.pop(key)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_pytest_xdist_is_a_declared_dev_dependency() -> None:
    """Every agent gets its own worktree venv, so an undeclared tool is missing from all of them."""
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    assert any(req.replace("_", "-").startswith("pytest-xdist") for req in dev), (
        f"pytest-xdist is not in the dev extra ({dev}) - `uv sync --all-extras` would not install it, "
        "and the documented fast loop would fail with 'unrecognized arguments: -n'"
    )


def test_the_timing_marker_is_registered() -> None:
    """`timing` is applied automatically, which makes registering it more important, not less."""
    markers = _pyproject()["tool"]["pytest"]["ini_options"]["markers"]
    assert any(marker.startswith("timing:") for marker in markers), (
        f"no `timing` marker registered in [tool.pytest.ini_options].markers ({markers}); "
        "the root conftest applies it to every wall-clock-budget test"
    )


def _mentions_clock(node: ast.AST) -> bool:
    """Whether an expression reads a monotonic clock anywhere inside it."""
    return any(isinstance(child, ast.Attribute) and child.attr in CLOCK_FUNCTIONS for child in ast.walk(node))


def _collected_test_files() -> list[Path]:
    """Every `test_*.py` under the roots `testpaths` names."""
    roots = _pyproject()["tool"]["pytest"]["ini_options"]["testpaths"]
    files: list[Path] = []
    for root in roots:
        files.extend(sorted((REPO_ROOT / root).rglob("test_*.py")))
    return files


def _marked(func: ast.FunctionDef, marker: str) -> bool:
    """Whether a test function carries `@pytest.mark.<marker>` in its own source."""
    for decorator in func.decorator_list:
        for node in ast.walk(decorator):
            if isinstance(node, ast.Attribute) and node.attr == marker:
                return True
    return False


def _marked_timing(func: ast.FunctionDef) -> bool:
    """Whether a test function carries `@pytest.mark.timing` in its own source."""
    return _marked(func, "timing")


def _marked_tests(marker: str) -> set[str]:
    """Every test in the suite carrying `@pytest.mark.<marker>`, by node id."""
    found: set[str] = set()
    for path, tree in _parsed_test_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for func in ast.walk(tree):
            if isinstance(func, ast.FunctionDef) and func.name.startswith("test_") and _marked(func, marker):
                found.add(f"{rel}::{func.name}")
    return found


def test_no_timing_marker_outlives_the_budget_it_was_added_for() -> None:
    """The other direction: a marker left behind after its assertion was removed or loosened.

    The forward gate cannot see this. Delete the clock assertion and keep the marker, and the test
    simply drops out of the derived set - the remaining entries still satisfy the non-vacuity check
    and everything passes, while that test and all its OTHER assertions vanish from tier 1 and from
    the nested bundle run. Coverage disappearing quietly is the exact failure this PR exists to avoid
    creating, so it is gated from both sides.
    """
    stale = sorted(_marked_tests("timing") - _sub_second_budget_tests() - MEASURED_LARGER_BUDGETS)
    assert not stale, (
        "these tests carry @pytest.mark.timing but assert no sub-second wall-clock budget, so they "
        "are excluded from the parallel tier for no reason - remove the marker, or add it to "
        f"MEASURED_LARGER_BUDGETS with the measurement that earned it: {stale}"
    )


def _sub_second_budget_tests() -> set[str]:
    """Every test that asserts a sub-second wall-clock budget, by node id."""
    return set(_budget_tests_with_marks())


def _parsed_test_files() -> list[tuple[Path, ast.Module]]:
    """Every `test_*.py` under the `testpaths` roots, parsed once."""
    parsed: list[tuple[Path, ast.Module]] = []
    for path in _collected_test_files():
        try:
            parsed.append((path, ast.parse(path.read_text(encoding="utf-8"))))
        except SyntaxError:  # deliberately malformed fixture files exist in this repo
            continue
    return parsed


def _budget_tests_with_marks() -> dict[str, bool]:
    """Every sub-second wall-clock budget test, mapped to whether it is marked `timing`."""
    found: dict[str, bool] = {}
    for path, tree in _parsed_test_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef) or not func.name.startswith("test_"):
                continue
            clock_locals = {
                target.id
                for node in ast.walk(func)
                if isinstance(node, ast.Assign) and _mentions_clock(node.value)
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(func):
                if not isinstance(node, ast.Assert) or not isinstance(node.test, ast.Compare):
                    continue
                compare = node.test
                if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.Lt):
                    continue
                right = compare.comparators[0]
                if not isinstance(right, ast.Constant) or not isinstance(right.value, (int, float)):
                    continue
                if right.value >= SUB_SECOND_CEILING:
                    continue
                reads_clock = _mentions_clock(compare.left) or any(
                    isinstance(name, ast.Name) and name.id in clock_locals for name in ast.walk(compare.left)
                )
                if reads_clock:
                    found[f"{rel}::{func.name}"] = _marked_timing(func)
    return found


def test_every_sub_second_wall_clock_test_declares_itself_timing() -> None:
    """A budget test that is not marked runs in the parallel tier, and eventually flakes there.

    Derived from the suite's own AST rather than a list, so it fails in both directions that matter:
    a new sub-second budget test nobody marked, and a marked test whose budget was removed. The
    first is the one that costs everyone re-runs.
    """
    budget_tests = _budget_tests_with_marks()
    assert budget_tests, "no sub-second wall-clock budget tests found at all - the gate is vacuous"
    unmarked = sorted(node for node, marked in budget_tests.items() if not marked)
    assert not unmarked, (
        "these tests assert a sub-second wall-clock budget but do not carry @pytest.mark.timing, "
        f"so the parallel tier would still run them: {unmarked}"
    )


def test_the_markers_actually_deselect_those_tests() -> None:
    """The markers are inert unless `-m` really removes those node ids from a collected run."""
    excluded = _sub_second_budget_tests() | MEASURED_LARGER_BUDGETS
    assert excluded, "nothing derived - this test would pass vacuously"
    target = sorted(excluded)[0].split("::")[0]
    in_target = sorted(node for node in excluded if node.startswith(target + "::"))
    result = _run_pytest(REPO_ROOT, ["-q", "--collect-only", "-m", "not timing", target])
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"{len(in_target)} deselected" in output, (
        f"expected {len(in_target)} deselected from {target}, got:\n{output[-2000:]}"
    )
    for node in in_target:
        assert node not in output, f"{node} survived the -m filter:\n{output[-2000:]}"


def test_the_timing_marker_travels_with_the_bundle_that_uses_it() -> None:
    """`-m "not timing"` has to work in the COPIED bundle, where this repo's pyproject is absent."""
    bundle_conftest = REPO_ROOT / ".github/skills/pbip-model-refresh/tests/conftest.py"
    body = bundle_conftest.read_text(encoding="utf-8")
    for marker in ("timing:",):
        assert "addinivalue_line" in body and f'"{marker}' in body, (
            f"{bundle_conftest.relative_to(REPO_ROOT).as_posix()} does not register `{marker[:-1]}`; "
            "copied out of this repo the marker is unregistered and the filter silently stops meaning anything"
        )
    # `serial` was retired (#831); a half-removed registration would revive a marker nothing gates.
    assert '"serial:' not in body, f"{bundle_conftest.relative_to(REPO_ROOT).as_posix()} still registers `serial`"
    markers = _pyproject()["tool"]["pytest"]["ini_options"]["markers"]
    assert not any(marker.startswith("serial:") for marker in markers), f"`serial` is still registered: {markers}"


def test_the_nested_bundle_run_inherits_the_parallel_tier_exclusion(monkeypatch: object) -> None:
    """`test_skills.py` re-runs the bundle in a temp dir, where the root conftest cannot reach.

    Measured (issue #387): during a parallel campaign the outer suite deselected
    `test_refresh_main_returns_credential_missing_fast_at_t0`, and the nested copy of that same test
    failed at 0.519s against its 0.5s budget - the flake walked straight through the exclusion.

    Driven behaviourally rather than by grepping the file. A first draft asserted on the presence of
    the two strings anywhere in the source; deleting the propagation entirely still passed it,
    because both strings also appear in the prose that explains them.
    """
    skills = REPO_ROOT / "tests" / "test_skills.py"
    spec = importlib.util.spec_from_file_location("t2p_test_skills", skills)
    assert spec is not None and spec.loader is not None, f"cannot load {skills}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert module._nested_marker_filter() == [], "a serial outer run must still execute every bundled test"
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    assert module._nested_marker_filter() == ["-m", "not timing"], (
        "the nested bundle run does not inherit the parallel-tier exclusion, so a wall-clock-budget "
        "test still runs under 22 workers"
    )

    tree = ast.parse(skills.read_text(encoding="utf-8"))
    spliced = any(
        isinstance(node, ast.Starred)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "_nested_marker_filter"
        for node in ast.walk(tree)
    )
    assert spliced, "_nested_marker_filter() is never spliced into the nested pytest argv"


def test_a_parallel_run_without_loadfile_is_refused(tmp_path: Path) -> None:
    """`-n auto` alone means `--dist load`, which was never measured here. It must not run."""
    _scratch_suite(tmp_path, nested=False)
    result = _run_pytest(tmp_path, ["-q", "-n", "2"])
    output = result.stdout + result.stderr
    assert result.returncode != 0, f"a parallel run without --dist loadfile was accepted:\n{output}"
    assert "--dist loadfile" in output and "issue #387" in output, (
        f"exited non-zero, but not because of the guard - the message does not name it:\n{output}"
    )


def test_the_guard_also_covers_the_skills_testpath_root(tmp_path: Path) -> None:
    """The reason the guard is at the repo ROOT and not in `tests/conftest.py`.

    `testpaths` names two roots, and the wall-clock-sensitive Power BI Desktop tests live under the
    second one. A guard scoped to `tests/` would leave the single most dangerous parallel run -
    `pytest .github/skills/<bundle>/tests -n 4` - completely ungated.
    """
    target = _scratch_suite(tmp_path, nested=True)
    result = _run_pytest(tmp_path, ["-q", "-n", "2", str(target.relative_to(tmp_path))])
    output = result.stdout + result.stderr
    assert result.returncode != 0, f"a nested-root parallel run escaped the guard:\n{output}"
    assert "--dist loadfile" in output, f"exited non-zero for some other reason:\n{output}"


def test_a_parallel_run_with_loadfile_is_allowed(tmp_path: Path) -> None:
    """The guard must permit the documented fast loop, or it has simply banned parallelism."""
    _scratch_suite(tmp_path, nested=False)
    result = _run_pytest(tmp_path, ["-q", "-n", "2", "--dist", "loadfile"])
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"the documented fast loop was rejected:\n{output}"
    assert "1 passed" in output, f"the scratch test did not run:\n{output}"


def test_a_serial_run_is_untouched_by_the_guard(tmp_path: Path) -> None:
    """Tier 2 is the gate of record; the guard must be invisible to it."""
    _scratch_suite(tmp_path, nested=False)
    result = _run_pytest(tmp_path, ["-q"])
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"a plain serial run was disturbed by the guard:\n{output}"
    assert "1 passed" in output, output


def _documented_pytest_commands() -> list[str]:
    """Every pytest invocation inside a ```bash fence in the loop doc.

    Deliberately restricted to `bash`-tagged fences: the doc also shows the guard's own rejection of
    `-n auto` inside an untagged fence, and that illustration must not be read as a recommendation.
    """
    text = LOOP_DOC.read_text(encoding="utf-8")
    commands: list[str] = []
    for block in re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL):
        commands.extend(line.strip() for line in block.splitlines() if "pytest" in line)
    return commands


def _uses_xdist(command: str) -> bool:
    """Whether a documented command asks xdist for workers, with no exemptions."""
    return " -n " in command or "--numprocesses" in command


def _is_filter_judged_parallel(command: str) -> bool:
    """A parallel command whose marker filter is subject to the exclusion rules.

    `--include-contended` is the documented opt-out from the automatic deselection, so a command
    carrying it is deliberately running the contended tests. It is exempt from the FILTER rules only
    - it is still a parallel command for every other purpose, which is what the two predicates keep
    apart. Conflating them let a mutation of the tier-2 command slip through: the opt-out example was
    counted as a whole-suite serial run because the single predicate said it was not parallel.
    """
    return _uses_xdist(command) and "--include-contended" not in command


def test_the_loop_doc_never_documents_parallel_without_loadfile() -> None:
    """A documented command the guard rejects reads as "the guard is broken", not "the doc is wrong"."""
    commands = _documented_pytest_commands()
    assert commands, f"no pytest commands found in {LOOP_DOC.name} - the doc cannot be checked"
    offenders = [cmd for cmd in commands if _uses_xdist(cmd) and "--dist loadfile" not in cmd]
    assert not offenders, f"documented parallel commands that the root conftest guard would reject: {offenders}"


def _marker_expression(command: str) -> str | None:
    """The `-m "<expr>"` filter of a documented command, if it has one."""
    match = re.search(r'-m\s+"([^"]*)"', command)
    return match.group(1) if match else None


def test_the_loop_doc_never_documents_a_parallel_run_that_keeps_contended_tests() -> None:
    """Judged by pytest's own expression semantics, not by substring.

    A substring check for "timing" would accept `-m "timing or not timing"` or `-m "not slow and
    timing"`. Compiling the expression and asking whether it excludes the marker is the only check
    that tells those commands apart from the correct one.
    """
    offenders: list[str] = []
    for command in _documented_pytest_commands():
        if not _is_filter_judged_parallel(command):
            continue
        expression = _marker_expression(command)
        if expression is None:
            offenders.append(f"{command!r}: no -m filter at all")
            continue
        compiled = Expression.compile(expression)
        for marker in ("timing",):
            if compiled.evaluate(functools.partial(operator.eq, marker)):
                offenders.append(f"{command!r}: -m {expression!r} does not exclude `{marker}`")
    assert not offenders, "documented parallel commands that would still run contended tests: " + "; ".join(offenders)


def test_xdist_deselects_contended_tests_without_being_asked() -> None:
    """The mechanism, not the convention: the exclusion must not depend on the caller's `-m`.

    Models the caller who forgot the filter entirely: a parallel run with no `-m` at all.
    """
    collection = _run_pytest(REPO_ROOT, ["-q", "--collect-only", "-m", "timing", BUNDLE_TESTS])
    assert collection.returncode == 0, collection.stdout + collection.stderr
    contended = {line for line in collection.stdout.splitlines() if line.startswith(BUNDLE_TESTS + "::")}
    # The observer file keeps three wall-clock controls (#831 retired the GUI tier and three t0 tests).
    assert len(contended) >= 3, f"expected the bundle's contended tests to be found, got {sorted(contended)}"
    result = _run_pytest(
        REPO_ROOT,
        ["-q", "--collect-only", "-n", "2", "--dist", "loadfile", BUNDLE_TESTS],
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"{len(contended)} deselected" in output, f"expected {len(contended)} deselected:\n{output[-1500:]}"
    for node in sorted(contended):
        assert node not in output, f"{node} was collected under xdist despite carrying a contended marker"


def test_a_real_parallel_run_deselects_them_inside_the_workers(tmp_path: Path) -> None:
    """`--collect-only` can be answered by the controller; a real run collects in the WORKERS.

    A worker sees `numprocesses=None dist='no'` (measured), so a mechanism that only checked
    `numprocesses` would deselect nothing in the run that actually matters while looking correct
    under `--collect-only`. This drives a genuine `-n 2` run to close that blind spot.

    Judged from the JUnit report, not the summary line: xdist does not aggregate the workers'
    deselections into `N deselected`, so a real run that correctly skipped all thirteen still prints
    only `87 passed`. Asserting on the executed node ids is both stronger and unambiguous.
    """
    contended = {node for node in _contended_nodes() if node.startswith(BUNDLE_TESTS + "::")}
    report = tmp_path / "report.xml"
    result = _run_pytest(
        REPO_ROOT, ["-q", "--tb=no", "-n", "2", "--dist", "loadfile", f"--junitxml={report}", BUNDLE_TESTS]
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-2000:]
    executed = {
        f"{case.get('classname').replace('.', '/')}.py::{case.get('name')}".replace("/github/skills", ".github/skills")
        for case in ET.parse(report).getroot().iter("testcase")
    }
    still_running = sorted(node for node in contended if node in executed)
    assert not still_running, f"a real parallel run executed contended tests inside its workers: {still_running}"
    assert executed, f"the run executed nothing at all:\n{output[-1500:]}"


def test_the_opt_out_flag_puts_the_contended_tests_back() -> None:
    """Deliberate stress-testing must remain possible, or the mechanism is a wall rather than a gate."""
    result = _run_pytest(
        REPO_ROOT,
        ["-q", "--collect-only", "-n", "2", "--dist", "loadfile", "--include-contended", BUNDLE_TESTS],
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    # With the `gui` exclusion retired (#831) nothing else deselects, so the original strict check holds.
    assert "deselected" not in output, f"--include-contended left tests deselected:\n{output[-1500:]}"
    for node in sorted(node for node in _contended_nodes() if node.startswith(BUNDLE_TESTS + "::")):
        assert node in output, f"{node} missing even with --include-contended"


def _contended_nodes() -> set[str]:
    """Every node id that carries a marker the parallel tier excludes."""
    return _marked_tests("timing")


def test_the_loop_doc_documents_both_tiers() -> None:
    """One green parallel run is not proof of isolation, so the serial gate must stay written down."""
    commands = _documented_pytest_commands()
    assert any("-n auto" in cmd and "--dist loadfile" in cmd for cmd in commands), (
        f"{LOOP_DOC.name} documents no fast tier: {commands}"
    )
    whole_suite_serial = [cmd for cmd in commands if not _uses_xdist(cmd) and " -m " not in cmd]
    assert whole_suite_serial, (
        f"{LOOP_DOC.name} documents no plain whole-suite serial tier - a filtered or parallel run "
        f"cannot be the gate of record: {commands}"
    )
