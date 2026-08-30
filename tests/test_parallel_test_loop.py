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


def _launches_the_live_desktop(func: ast.FunctionDef) -> bool:
    """Whether a test drives a real WPF window through UI Automation.

    Two spellings exist and both are stable: calling the module's `_run_probe_against_wpf_modal`
    helper, or launching the fixture app directly, which is identifiable by the `-ReadyFile` argument
    the app uses to announce that its window is up. Deriving it beats a name list - a new live
    regression added next to these gets caught rather than silently joining the parallel tier.
    """
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_run_probe_against_wpf_modal":
                return True
        if isinstance(node, ast.Constant) and node.value == "-ReadyFile":
            return True
    return False


def _live_desktop_tests_with_marks() -> dict[str, bool]:
    """Every live WPF/UI-Automation test, mapped to whether it is marked `serial`."""
    found: dict[str, bool] = {}
    for path, tree in _parsed_test_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef) or not func.name.startswith("test_"):
                continue
            if _launches_the_live_desktop(func):
                found[f"{rel}::{func.name}"] = _marked(func, "serial")
    return found


def test_every_live_ui_test_declares_itself_serial() -> None:
    """The interactive desktop and its UIA provider are a singleton; two live tests degrade each other.

    Measured (issue #387) across three rounds of two whole-suite parallel runs started concurrently:
    `test_credential_text_beyond_the_element_cap_convicts_when_the_cap_allows_it` failed in 3 of 6,
    every time with `harvest=INCOMPLETE` / `VERDICT: DIALOG_UNREADABLE` - the probe degrading safely
    and the test's stricter assertion correctly refusing it. Marking only the one that was observed
    would move the boundary rather than remove it: all of them share the mechanism.
    """
    live = _live_desktop_tests_with_marks()
    assert len(live) >= 7, f"expected the bundle's live WPF regressions to be found, got {sorted(live)}"
    unmarked = sorted(node for node, marked in live.items() if not marked)
    assert not unmarked, (
        "these tests drive a real WPF window through UI Automation but do not carry "
        f"@pytest.mark.serial, so two concurrent parallel runs would race them: {unmarked}"
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
    excluded = _sub_second_budget_tests() | set(_live_desktop_tests_with_marks())
    assert excluded, "nothing derived - this test would pass vacuously"
    target = sorted(excluded)[0].split("::")[0]
    in_target = sorted(node for node in excluded if node.startswith(target + "::"))
    result = _run_pytest(REPO_ROOT, ["-q", "--collect-only", "-m", "not (serial or timing)", target])
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert f"{len(in_target)} deselected" in output, (
        f"expected {len(in_target)} deselected from {target}, got:\n{output[-2000:]}"
    )
    for node in in_target:
        assert node not in output, f"{node} survived the -m filter:\n{output[-2000:]}"


def test_the_timing_marker_travels_with_the_bundle_that_uses_it() -> None:
    """`-m "not (serial or timing)"` has to work in the COPIED bundle, where this repo's pyproject is absent."""
    bundle_conftest = REPO_ROOT / ".github/skills/pbip-model-refresh/tests/conftest.py"
    body = bundle_conftest.read_text(encoding="utf-8")
    for marker in ("timing:", "serial:"):
        assert "addinivalue_line" in body and f'"{marker}' in body, (
            f"{bundle_conftest.relative_to(REPO_ROOT).as_posix()} does not register `{marker[:-1]}`; "
            "copied out of this repo the marker is unregistered and the filter silently stops meaning anything"
        )


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
    assert module._nested_marker_filter() == ["-m", "not (serial or timing)"], (
        "the nested bundle run does not inherit the parallel-tier exclusion, so a wall-clock-budget "
        "or live UI test still runs under 22 workers"
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
