"""Prove the window-spawning tests are hidden from every ordinary run WITHOUT being lost.

Issue #447. Some tests must spawn a real top-level window - UI Automation cannot be exercised
against a mock - and one of them was stealing focus on an operator's machine mid-demo, because
`testpaths` includes `.github/skills` so a bare `pytest` collects them.

WHY THIS FILE EXISTS AT ALL
---------------------------
A marker that hides a test is how coverage dies silently, and this repo has already done it once:
issue #435, "CI runs none of the 15 engine-dependent tests - they SKIP silently, and a skip is not
a pass". So asserting "the suite is green" would be worthless here - a suite is equally green when
the tests passed and when they vanished.

WHAT THE FIRST VERSION OF THIS FILE GOT WRONG, AND WHY EACH CONTROL BELOW IS SHAPED AS IT IS
--------------------------------------------------------------------------------------------
The first version hardcoded ONE bundle path and ONE marked file, and every one of its assertions
was about that file. An independent review then measured four ways round it, all reproduced here
before this rewrite:

* `pytest tests/` runs `test_skills.py`, which copies the bundle OUT of the repo and runs a nested
  pytest there. `pyproject.toml` does not travel with the copy, so `addopts` never applied. With
  every spawn site instrumented to raise, that nested run reached **all ten** of them - and the
  outer summary reported **zero** deselections, so it was invisible;
* three tests spawn native `CreateWindowExW` windows and carried no marker at all;
* an explicit `-m` REPLACES an ini `addopts` marker expression rather than composing with it, so
  `-m "not slow"` - which `docs/offline-mock-harness.md` recommends - collected **309/309** of the
  bundle's tests, every window included;
* the controls covered one file, so a gui test added anywhere else was gated by nothing.

Hence: nothing below names a test file. The gui set is enumerated **by collection** over the roots
`pyproject.toml` declares, the window-spawning set is derived **from the AST** of every test file
under those roots, and the CI command is read from the workflow and **executed** rather than
pattern-matched. Collection only - `--collect-only` never runs a test, so this file never opens a
window.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from _pytest.mark.expression import Expression

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "checks.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

GUI_MARKER = "gui"
RUN_GUI_FLAG = "--run-gui"
RUN_GUI_ENV = "T2P_RUN_GUI"

# A test SPAWNS A WINDOW if it reaches one of these, directly or through a helper in its own module.
# Both are call-context only, which is the difference between detecting a spawn and detecting a
# mention: `tests/test_parallel_test_loop.py` contains the literal "-ReadyFile" inside its own AST
# scanner, and matching bare constants flagged three of its meta-tests as window-spawners.
WINDOW_SPAWN_CALLS = {"CreateWindowExW"}
WINDOW_SPAWN_ARGV = {"-ReadyFile"}
PROCESS_LAUNCHERS = {"Popen", "run", "call", "check_call", "check_output"}


def _testpath_roots() -> list[str]:
    """The collection roots, read from `pyproject.toml` rather than restated here."""
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["tool"]["pytest"]["ini_options"]["testpaths"]


VERBOSITY_ONLY = re.compile(r"^(-q+|-v+|--quiet|--verbose|--verbosity(=.*)?)$")


def _without_verbosity_flags(argv: list[str]) -> list[str]:
    """Drop presentation-only flags from a caller's argv.

    ⚠️ Not cosmetic, and found by this file's own CI check failing. `--collect-only` prints node ids
    at `-q` and **`path: count`** at `-qq` (`TerminalReporter._printcollecteditems` branches on
    `verbose < -1`), so appending our `-q` to a workflow command that already carries one silently
    changed the output format and every node id vanished - which read as "CI reaches none of them".
    Verbosity cannot affect SELECTION, which is the only thing these checks measure, so normalising
    it keeps the command's meaning while making its transcript parseable.
    """
    kept: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if VERBOSITY_ONLY.match(token):
            skip_next = token == "--verbosity"
            continue
        kept.append(token)
    return kept


def _collect(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None):
    """`--collect-only -q` with `argv`; never executes a test, so never opens a window."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *_without_verbosity_flags(argv)],
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
        env=env,
        check=False,
    )


def _node_ids(output: str) -> set[str]:
    """Every node id in a `--collect-only -q` transcript."""
    return {line.strip() for line in output.splitlines() if "::" in line and not line.startswith(("E ", "ERROR"))}


def _collected_node_ids(argv: list[str], **kwargs) -> set[str]:
    """The node ids a given pytest invocation would actually run."""
    return _node_ids(_collect(argv, **kwargs).stdout)


@functools.lru_cache(maxsize=1)
def _gui_node_ids() -> frozenset[str]:
    """Every `gui`-marked test in the repository, BY COLLECTION, over the declared testpaths.

    Enumerated from `pyproject.toml`'s roots and never from the CI command, so that the CI coverage
    check below compares two independently-derived things. Deriving both from the workflow would
    make that check a tautology that passes however narrow the command becomes.
    """
    found: set[str] = set()
    for root in _testpath_roots():
        found |= _collected_node_ids([RUN_GUI_FLAG, "-m", GUI_MARKER, root])
    return frozenset(found)


def _spawn_evidence(node: ast.AST) -> str | None:
    """Why this AST subtree opens a window, or None. Call context only - see WINDOW_SPAWN_CALLS."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        called = child.func.attr if isinstance(child.func, ast.Attribute) else getattr(child.func, "id", "")
        if called in WINDOW_SPAWN_CALLS:
            return called
        if called in PROCESS_LAUNCHERS:
            for argument in ast.walk(child):
                if isinstance(argument, ast.Constant) and argument.value in WINDOW_SPAWN_ARGV:
                    return f"subprocess argv {argument.value}"
    return None


def _module_definitions(tree: ast.Module) -> dict[str, ast.AST]:
    """Every name in a module that a test could call into, mapped to its definition."""
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _reaches_a_spawn(node: ast.AST, definitions: dict[str, ast.AST], seen: set[str]) -> str | None:
    """Whether `node` opens a window directly or through anything it calls in the same module.

    Transitive on purpose. Seven of the ten go through `_run_probe_against_wpf_modal` and two through
    `_NativeWindowProbe`, so a body-only scan sees three of them and calls the job done - which is
    the shape of the defect this control exists to catch.
    """
    direct = _spawn_evidence(node)
    if direct:
        return direct
    for child in ast.walk(node):
        name = child.id if isinstance(child, ast.Name) else None
        if name in definitions and name not in seen:
            seen.add(name)
            deeper = _reaches_a_spawn(definitions[name], definitions, seen)
            if deeper:
                return f"{name} -> {deeper}"
    return None


def _window_spawning_tests() -> dict[str, str]:
    """Every test that opens a real window, by node id, mapped to the evidence that says so."""
    found: dict[str, str] = {}
    for root in _testpath_roots():
        for path in sorted((REPO_ROOT / root).rglob("test_*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # deliberately malformed fixture files exist in this repo
                continue
            definitions = _module_definitions(tree)
            relative = path.relative_to(REPO_ROOT).as_posix()
            for func in tree.body:
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not func.name.startswith("test_"):
                    continue
                why = _reaches_a_spawn(func, definitions, {func.name})
                if why:
                    found[f"{relative}::{func.name}"] = why
    return found


def _run_commands() -> list[str]:
    """Every `run:` command in the CI workflow, one per line."""
    return [
        line.split("run:", 1)[1].strip()
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if re.match(r"\s*run:", line)
    ]


def _selects_gui(command: str) -> bool:
    """Whether a command's `-m` expression SELECTS `gui`, judged by pytest's own semantics.

    Never by substring. The first version of this file asserted `"-m gui" in workflow`, and the
    mutation that deleted the opt-in from the run line SURVIVED, because the phrase still appeared
    in the explanatory comment beside it.
    """
    tokens = shlex.split(command)
    if "pytest" not in tokens or "-m" not in tokens:
        return False
    expression = tokens[tokens.index("-m") + 1]
    try:
        compiled = Expression.compile(expression)
    except Exception:  # pylint: disable=broad-except
        return False
    return bool(compiled.evaluate(lambda name: name == GUI_MARKER))


def _ci_gui_invocations() -> list[list[str]]:
    """The CI commands that opt into the gui tests, as argv we can run locally.

    `uv run` is stripped and `pytest` replaced by this interpreter's; everything else is kept
    VERBATIM, including `--run-gui` and any path arguments. That is the point: the check below runs
    the real command rather than reasoning about its text, so deleting the opt-in flag or narrowing
    the paths changes what gets collected and is caught.
    """
    invocations: list[list[str]] = []
    for command in _run_commands():
        if not _selects_gui(command):
            continue
        tokens = shlex.split(command)
        invocations.append(tokens[tokens.index("pytest") + 1 :])
    return invocations


def _gui_tests_missed_by(argv: list[str]) -> set[str]:
    """Which of the repository's gui tests a given pytest argv would NOT run.

    THE predicate. Both the CI check and its own inverse-completeness proof call this, so the proof
    cannot drift away from the thing it is proving.
    """
    return set(_gui_node_ids()) - _collected_node_ids(argv)


def _repo_free_env() -> dict[str, str]:
    """A child environment with nothing of this repo - or this run's opt-in - leaking into it."""
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS", RUN_GUI_ENV):
        env.pop(name, None)
    return env


# ---------------------------------------------------------------------------------------------
# The marked set, and its agreement with the set that actually opens windows
# ---------------------------------------------------------------------------------------------


def test_the_repository_has_gui_marked_tests_at_all() -> None:
    """Kills: the whole mechanism selecting nothing, which makes every check below vacuous."""
    assert len(_gui_node_ids()) >= 10, (
        "expected at least the ten known window-spawning tests to be collectable under "
        f"`{RUN_GUI_FLAG} -m {GUI_MARKER}`, got {sorted(_gui_node_ids())}"
    )


def test_every_window_spawning_test_declares_itself_gui() -> None:
    """Kills the defect the first version could not see: a test that opens a window and is unmarked.

    Derived from the suite's own AST, transitively, so it is not a list anyone has to remember to
    update. Measured before this control existed: three tests created real `WS_VISIBLE` top-level
    windows through `CreateWindowExW` and carried no marker, and replacing each spawn site with a
    raising sentinel showed all three reached on the DEFAULT selection.
    """
    spawners = _window_spawning_tests()
    assert len(spawners) >= 10, f"the AST scan found only {sorted(spawners)} - it can no longer see the spawn sites"
    unmarked = sorted(node for node in spawners if node not in _gui_node_ids())
    assert not unmarked, (
        "these tests open a real top-level window but are not collected under "
        f"`{RUN_GUI_FLAG} -m {GUI_MARKER}`, so an ordinary run hijacks the operator's desktop: "
        + ", ".join(f"{node} [{spawners[node]}]" for node in unmarked)
    )


def test_no_gui_marker_outlives_the_window_it_was_added_for() -> None:
    """The other direction: a marker left behind hides a test from every tier for no reason."""
    stale = sorted(set(_gui_node_ids()) - set(_window_spawning_tests()))
    assert not stale, (
        "these tests carry @pytest.mark.gui but no longer open a window, so they are excluded from "
        f"every run - and only CI runs them - for nothing: {stale}"
    )


# ---------------------------------------------------------------------------------------------
# The exclusion itself: it must COMPOSE with whatever the caller typed
# ---------------------------------------------------------------------------------------------


def test_a_default_run_selects_no_gui_test() -> None:
    """Kills: deleting the collection hook - an ordinary `pytest` opens windows again."""
    leaked = sorted(_gui_node_ids() & _collected_node_ids([]))
    assert not leaked, f"a default run collected window-spawning tests: {leaked}"


def test_no_marker_expression_a_caller_can_type_re_enables_the_gui_tests() -> None:
    """Kills the fail-open `addopts` this replaced. An explicit `-m` REPLACES an ini expression.

    Measured against `addopts = "-m 'not gui'"` on the bundle's 309 tests:

        default                  302/309
        -m gui                     7/309
        -m "not slow"            309/309   <- every window-spawning test back
        -m "not something_else"  309/309

    `not slow` is not a hypothetical: `docs/offline-mock-harness.md` documents it. A collection hook
    runs AFTER pytest has applied the caller's `-m`, so it composes instead of being replaced.
    """
    leaked: dict[str, list[str]] = {}
    for expression in ("not slow", "not something_else", "not (serial or timing)", GUI_MARKER, "serial"):
        collected = _gui_node_ids() & _collected_node_ids(["-m", expression])
        if collected:
            leaked[expression] = sorted(collected)
    assert not leaked, f"these marker expressions re-enabled the window-spawning tests: {leaked}"


@contextlib.contextmanager
def _gui_test_outside_every_bundle():
    """A synthetic `gui` test placed INSIDE the repo but outside every bundle, then removed.

    It has to be inside the repo: pytest only loads a `conftest.py` for paths beneath it, so a file
    in the OS temp directory is judged by neither hook and proves nothing about either. The `_`
    prefix is what keeps it out of git (`.gitignore` has `/_*`) and out of `testpaths`, so no other
    run can collect it.
    """
    folder = REPO_ROOT / f"_gui_gate_probe_{os.getpid()}"
    folder.mkdir(exist_ok=True)
    probe = folder / "test_root_hook_probe.py"
    probe.write_text(
        "import pytest\n\n\n@pytest.mark.gui\ndef test_root_hook_probe() -> None:\n"
        '    raise AssertionError("this synthetic test must never be executed")\n',
        encoding="utf-8",
    )
    try:
        yield probe
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_the_root_hook_deselects_a_gui_test_that_is_in_no_bundle() -> None:
    """Kills: deleting the ROOT hook's gui branch - which nothing else here can see.

    Measured, and it is the reason this control exists: every gui test in the repository today lives
    in ONE bundle, and that bundle's own `conftest.py` deselects them as well. So disabling the root
    hook's gui branch left the bundle collecting 299/309 exactly as before and every other control
    passed - a guard with no observable failure mode. A gui test added anywhere else would have run,
    with windows, and nothing would have said so. This puts one there, transiently, and requires the
    root hook to deselect it by default and the opt-in to bring it back.
    """
    with _gui_test_outside_every_bundle() as probe:
        default = _collected_node_ids([str(probe)])
        opted_in = _collected_node_ids([RUN_GUI_FLAG, str(probe)])
    assert not default, (
        "a gui test outside every bundle was collected by a default run, so the root conftest hook "
        f"is not deselecting it: {sorted(default)}"
    )
    assert opted_in, "the same test could not be selected with the opt-in either, so it is orphaned"


def test_the_opt_in_flag_selects_exactly_the_gui_tests() -> None:
    """Kills: orphaning them - marked, deselected everywhere, and selectable by nothing."""
    collected = _collected_node_ids([RUN_GUI_FLAG, "-m", GUI_MARKER])
    # ⚠️ The non-vacuity assert comes FIRST and is not decoration. Both sides of the equality below
    # are collected with `--run-gui`, so a mutation that breaks the opt-in outright empties BOTH and
    # the equality holds - a differential assertion whose arms move together proves nothing.
    assert collected, (
        f"`{RUN_GUI_FLAG} -m {GUI_MARKER}` collected NOTHING, so the opt-in no longer works and the "
        "window-spawning tests are orphaned: excluded from every tier and selectable by nobody"
    )
    assert collected == set(_gui_node_ids()), (
        f"`{RUN_GUI_FLAG} -m {GUI_MARKER}` collected {sorted(collected)}, expected {sorted(_gui_node_ids())}"
    )


def test_the_portable_env_var_opts_in_too() -> None:
    """Kills: the copied-out bundle becoming unrunnable - a nested pytest cannot be handed a flag.

    `tests/test_skills.py` launches the bundle as a subprocess outside this repo, so `--run-gui` is
    not registered there. The environment variable is the only opt-in that survives the copy, which
    is why it is a mechanism and not a convenience.
    """
    env = _repo_free_env()
    env[RUN_GUI_ENV] = "1"
    collected = _collected_node_ids(["-m", GUI_MARKER], env=env)
    assert collected, f"`{RUN_GUI_ENV}=1` selected nothing, so the copied-out bundle cannot be run at all"
    assert collected == set(_gui_node_ids()), (
        f"`{RUN_GUI_ENV}=1 -m {GUI_MARKER}` collected {sorted(collected)}, expected {sorted(_gui_node_ids())}"
    )


# ---------------------------------------------------------------------------------------------
# The copy that leaves the repository behind
# ---------------------------------------------------------------------------------------------


def _bundles_with_gui_tests() -> list[Path]:
    """Every bundled skill that ships a window-spawning test, derived from the gui node ids."""
    bundles = set()
    for node in _gui_node_ids():
        path = REPO_ROOT / node.split("::", 1)[0]
        if ".github/skills/" in path.as_posix():
            bundles.add(path.parents[1])
    return sorted(bundles)


def _gui_test_names_in(bundle: Path) -> set[str]:
    """The bare test names a bundle's gui tests carry, which survive being copied elsewhere."""
    prefix = bundle.relative_to(REPO_ROOT).as_posix() + "/"
    return {node.split("::", 1)[1] for node in _gui_node_ids() if node.startswith(prefix)}


def _copy_bundle(bundle: Path, destination: Path) -> Path:
    """Copy a skill folder exactly as its README tells a reader to."""
    return Path(
        shutil.copytree(
            bundle, destination / bundle.name, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
        )
    )


def test_a_bundle_copied_out_of_this_repo_still_deselects_its_gui_tests(tmp_path: Path) -> None:
    """Kills the invisible one. `pyproject.toml` provably does not travel with a copied bundle.

    `tests/test_skills.py` copies the bundle to a temp directory and runs a nested pytest there with
    `cwd` outside the repo and `PYTHONPATH`/`PYTEST_ADDOPTS` cleared, so the root `conftest.py` and
    `pyproject.toml` are both absent by construction. Measured with every spawn site instrumented to
    raise, before the bundle carried its own hook: `10 failed, 279 passed` inside that nested run,
    while the OUTER summary reported zero deselections. This reproduces the same copy.
    """
    bundles = _bundles_with_gui_tests()
    assert bundles, "no bundled skill ships a gui test - this control would pass vacuously"
    for bundle in bundles:
        copied = _copy_bundle(bundle, tmp_path)
        names = _gui_test_names_in(bundle)
        assert names, f"{bundle.name} was selected as a gui-bearing bundle but yielded no test names"
        result = _collect([str(copied / "tests")], cwd=tmp_path, env=_repo_free_env())
        leaked = sorted(node for node in _node_ids(result.stdout) if node.split("::", 1)[-1] in names)
        assert not leaked, (
            f"{bundle.name} copied out of the repo still collects its window-spawning tests, so the "
            f"nested run in tests/test_skills.py opens windows:\n{leaked}"
        )
        assert re.search(r"\b(\d+) deselected", result.stdout), (
            f"{bundle.name} copied out of the repo deselected nothing at all, so its own conftest "
            f"hook is not running:\n{result.stdout[-1500:]}"
        )


def test_a_copied_out_bundle_registers_the_gui_marker_itself(tmp_path: Path) -> None:
    """Kills: `PytestUnknownMarkWarning` in someone else's repo - one `--strict-markers` from an error.

    Judged by running the copy under `--strict-markers`, which turns an unregistered mark into a
    collection error and therefore a non-zero exit, rather than by grepping for a warning string.
    Measured before the bundle registered it: the nested run emitted seven of those warnings.
    """
    for bundle in _bundles_with_gui_tests():
        copied = _copy_bundle(bundle, tmp_path)
        result = _collect([str(copied / "tests"), "--strict-markers"], cwd=tmp_path, env=_repo_free_env())
        assert result.returncode == 0, (
            f"{bundle.name} does not register every marker it uses in its own conftest, so it warns "
            f"(and under --strict-markers fails) wherever it is copied:\n{result.stdout[-2000:]}"
        )


# ---------------------------------------------------------------------------------------------
# CI still runs them - and runs ALL of them
# ---------------------------------------------------------------------------------------------


def test_ci_opts_into_the_gui_tests_explicitly() -> None:
    """Kills issue #435 in a new place - a marker that hides tests from CI as well as from a desktop."""
    assert _ci_gui_invocations(), (
        "no CI `run:` command selects the `gui` marker. The collection hook deselects these tests "
        "everywhere, so without an explicit opt-in step they run NOWHERE - trading a focus-stealing "
        "window for silently-lost coverage of a fail-open credential guard (issue #435). pytest "
        f"commands found: {[c for c in _run_commands() if 'pytest' in c]}"
    )


def test_the_ci_command_reaches_every_gui_test_in_the_repository() -> None:
    """The inverse-completeness half, and the one the first version of this file did not have.

    It asserted that some `run:` line contained `pytest -m gui` - which is satisfied by a command
    scoped to ONE directory, so a gui test added anywhere else was deselected by default and run by
    nobody. This instead EXECUTES the workflow's own command (with `--collect-only`) and compares
    what it collects against the gui tests found across `testpaths`. Two independently-derived sets:
    narrowing the command's paths, or dropping its `--run-gui`, moves only one of them.
    """
    for argv in _ci_gui_invocations():
        collected = _collected_node_ids(argv)
        assert collected, (
            f"the CI opt-in command `pytest {' '.join(argv)}` collects NOTHING. `-m gui` alone "
            f"selects the marked tests and the collection hook then deselects them again, so the "
            f"step reports 'no tests ran' and stays green - it needs `{RUN_GUI_FLAG}` as well"
        )
        missed = sorted(_gui_tests_missed_by(argv))
        assert not missed, (
            f"the CI opt-in command `pytest {' '.join(argv)}` does not reach these window-spawning "
            f"tests, so nothing runs them anywhere: {missed}"
        )


def test_the_ci_coverage_check_notices_a_gui_test_the_command_cannot_reach() -> None:
    """Proves the check above CAN fail, using the same predicate rather than a re-implementation.

    An inverse-completeness control that is itself vacuous is worse than none: the reviewer's own
    mutation - add a brand-new top-level `@pytest.mark.gui` test - left all four of the previous
    controls passing. So this runs `_gui_tests_missed_by` against a deliberately narrowed argv and
    requires it to report the misses.
    """
    roots = _testpath_roots()
    barren = [root for root in roots if not _collected_node_ids([RUN_GUI_FLAG, "-m", GUI_MARKER, root])]
    assert barren, (
        "every declared testpath root contains a gui test, so no root can stand in for a command "
        f"that misses one; roots={roots}"
    )
    missed = _gui_tests_missed_by([RUN_GUI_FLAG, "-m", GUI_MARKER, barren[0]])
    assert missed == set(_gui_node_ids()), (
        f"a command scoped to {barren[0]!r} reaches no gui test at all, yet the coverage predicate "
        f"reported only {sorted(missed)} missing - it cannot detect an unreachable gui test"
    )
