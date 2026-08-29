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
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

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


def test_the_serial_marker_is_registered() -> None:
    """`serial` is the hook the live Desktop/UIA tests hang off; an unregistered marker is a warning."""
    markers = _pyproject()["tool"]["pytest"]["ini_options"]["markers"]
    assert any(marker.startswith("serial:") for marker in markers), (
        f"no `serial` marker registered in [tool.pytest.ini_options].markers ({markers}); "
        'the documented fast loop filters on -m "not (serial or timing)"'
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


def _sub_second_budget_tests() -> set[str]:
    """Re-derive, from the suite's own AST, every test that asserts a sub-second wall-clock budget.

    The shape being looked for is `elapsed = time.monotonic() - started; assert elapsed < 0.5`, in
    either spelling - the comparison may read the clock inline, or through a local assigned from it.
    Deriving it beats trusting a hand-written list: a rename or a brand-new budget test both show up
    as drift instead of quietly leaving a flaky test in the parallel tier.
    """
    found: set[str] = set()
    for path in _collected_test_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # deliberately malformed fixture files exist in this repo
            continue
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
                    found.add(f"{rel}::{func.name}")
    return found


def _declared_timing_tests() -> set[str]:
    """The node ids the root conftest excludes from the parallel tier.

    Loaded from the file by path under a distinct module name rather than `import conftest`, so this
    reads the same bytes whether or not pytest itself loaded the root conftest for this run - the
    mutation harness deliberately runs with `--confcutdir=tests`, where it has not.
    """
    spec = importlib.util.spec_from_file_location("t2p_root_conftest", ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None, f"cannot load {ROOT_CONFTEST}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.TIMING_BUDGET_TESTS)


def test_the_conftest_timing_list_matches_the_suite_it_claims_to_describe() -> None:
    """A hand-written node-id list rots on the first rename; re-deriving it is what stops that.

    Fails in both directions on purpose: an entry that no longer exists (renamed, deleted) and a new
    sub-second budget test that nobody added. The second direction is the one that matters - it is
    how a fresh flaky test gets caught before it starts costing everyone re-runs.
    """
    derived = _sub_second_budget_tests()
    declared = _declared_timing_tests()
    assert declared == derived, (
        "conftest.TIMING_BUDGET_TESTS is out of step with the suite.\n"
        f"  declared but not found in the suite: {sorted(declared - derived)}\n"
        f"  found in the suite but not declared: {sorted(derived - declared)}"
    )


def test_the_timing_marker_actually_deselects_those_tests() -> None:
    """The list is inert unless the marker lands before pytest's own `-m` filtering runs."""
    declared = sorted(_declared_timing_tests())
    assert declared, "nothing declared - this test would pass vacuously"
    target = declared[0].split("::")[0]
    result = _run_pytest(REPO_ROOT, ["-q", "--collect-only", "-m", "not (serial or timing)", target])
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"{len(declared)} deselected" in output, (
        f"expected {len(declared)} deselected from {target}, got:\n{output[-2000:]}"
    )
    for node in declared:
        assert node not in output, f"{node} survived the -m filter:\n{output[-2000:]}"


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

    `testpaths` names two roots, and the live Power BI Desktop / UI-Automation tests live under the
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


def _is_parallel(command: str) -> bool:
    """Whether a documented command asks xdist for workers."""
    return " -n " in command or "--numprocesses" in command


def test_the_loop_doc_never_documents_parallel_without_loadfile() -> None:
    """A documented command the guard rejects reads as "the guard is broken", not "the doc is wrong"."""
    commands = _documented_pytest_commands()
    assert commands, f"no pytest commands found in {LOOP_DOC.name} - the doc cannot be checked"
    offenders = [cmd for cmd in commands if _is_parallel(cmd) and "--dist loadfile" not in cmd]
    assert not offenders, f"documented parallel commands that the root conftest guard would reject: {offenders}"


def test_the_loop_doc_never_documents_a_parallel_run_that_keeps_the_timing_tests() -> None:
    """The measured flake only reaches an operator through a documented command that forgot the filter."""
    offenders = [cmd for cmd in _documented_pytest_commands() if _is_parallel(cmd) and "timing" not in cmd]
    assert not offenders, (
        "documented parallel commands that would still run the wall-clock-budget tests "
        f"(measured to fail ~1 run in 8 at 22 workers): {offenders}"
    )


def test_the_loop_doc_documents_both_tiers() -> None:
    """One green parallel run is not proof of isolation, so the serial gate must stay written down."""
    commands = _documented_pytest_commands()
    assert any("-n auto" in cmd and "--dist loadfile" in cmd for cmd in commands), (
        f"{LOOP_DOC.name} documents no fast tier: {commands}"
    )
    whole_suite_serial = [cmd for cmd in commands if not _is_parallel(cmd) and " -m " not in cmd]
    assert whole_suite_serial, (
        f"{LOOP_DOC.name} documents no plain whole-suite serial tier - a filtered or parallel run "
        f"cannot be the gate of record: {commands}"
    )
