"""Prove the `gui` marker hides the window-spawning tests from an ordinary run WITHOUT losing them.

Issue #447. Some tests must spawn a real top-level window - UI Automation cannot be exercised
against a mock - and one of them was stealing focus on an operator's machine mid-demo, because
`testpaths` includes `.github/skills` so a bare `pytest` collects them.

WHY THIS FILE EXISTS AT ALL
---------------------------
A marker that hides a test is how coverage dies silently, and this repo has already done it once:
issue #435, "CI runs none of the 15 engine-dependent tests - they SKIP silently, and a skip is not
a pass". So asserting "the suite is green" would be worthless here - a suite is equally green when
the tests passed and when they vanished. Each test below asserts a COUNT, in the direction that
would catch its own failure mode:

* the negative control asserts the DESELECTED count is exactly the number of marked tests
* the positive control asserts `-m gui` SELECTS that same non-zero number
* the CI control asserts the workflow invokes `-m gui` explicitly, since the default now excludes it

Collection is done in a subprocess with `--collect-only`, so nothing is executed and no window is
ever spawned by this file.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_TESTS = REPO_ROOT / ".github" / "skills" / "pbip-model-refresh" / "tests"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "checks.yml"
MARKED_FILE = BUNDLE_TESTS / "test_credential_modal_detection.py"


def _collect(*extra: str) -> str:
    """`--collect-only -q` over the bundle tests; never executes a test, so never opens a window."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(BUNDLE_TESTS), *extra],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    return proc.stdout + proc.stderr


def _marked_test_count() -> int:
    """How many tests carry the marker, read from the source rather than from pytest."""
    return len(re.findall(r"^@pytest\.mark\.gui$", MARKED_FILE.read_text(encoding="utf-8"), re.MULTILINE))


def test_the_marker_is_applied_to_at_least_one_test() -> None:
    """Kills: the marker existing in config while no test carries it, making both controls vacuous."""
    assert _marked_test_count() > 0, (
        "no test carries @pytest.mark.gui, so the deselect/select controls below would both pass "
        "trivially and prove nothing"
    )


def test_a_default_run_deselects_every_gui_test() -> None:
    """Kills: dropping `addopts`, or a caller's `-m` silently replacing it -> windows return."""
    output = _collect()
    match = re.search(r"(\d+) deselected", output)
    assert match, f"expected a 'deselected' count in a default collection; got:\n{output[-1500:]}"
    assert int(match.group(1)) == _marked_test_count(), (
        f"default run deselected {match.group(1)} test(s) but {_marked_test_count()} carry the "
        "marker - the default filter and the marked set disagree"
    )


def test_opting_in_selects_exactly_the_marked_tests() -> None:
    """Kills: orphaning the tests - marked, deselected by default, and selectable by nothing."""
    output = _collect("-m", "gui")
    # ⚠️ Try the `selected/total` form FIRST. pytest prints "7/309 tests collected (302 deselected)"
    # when a filter applies, and a bare `(\d+) tests? collected` matches the TOTAL inside that
    # string - which scored this control 309 against 7 and read as a product defect rather than a
    # regex defect.
    match = re.search(r"(\d+)/\d+ tests? collected", output) or re.search(r"(\d+) tests? collected", output)
    assert match, f"expected a collected count under `-m gui`; got:\n{output[-1500:]}"
    assert int(match.group(1)) == _marked_test_count(), (
        f"`-m gui` collected {match.group(1)} test(s) but {_marked_test_count()} carry the marker"
    )


def test_ci_runs_the_gui_tests_explicitly() -> None:
    """Kills: issue #435 in a new place - a marker that hides tests from CI as well as from a desktop."""
    # ⚠️ Assert on the `run:` COMMAND, never on the file text. A first version asserted
    # `"-m gui" in workflow`, and the mutation that deleted the opt-in from the run line SURVIVED -
    # because the phrase still appeared in the explanatory comment beside it. A guard satisfied by
    # prose rather than by the thing it checks is exactly the vacuity this repo keeps finding.
    commands = [
        line.split("run:", 1)[1].strip()
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if re.match(r"\s*run:", line)
    ]
    opted_in = [cmd for cmd in commands if "pytest" in cmd and re.search(r"-m\s+'?gui'?\b", cmd)]
    assert opted_in, (
        "no CI `run:` command invokes pytest with `-m gui`. The default `addopts` now deselects "
        "these tests everywhere, so without an explicit opt-in step they run NOWHERE - trading a "
        "focus-stealing window for silently-lost coverage of a fail-open credential guard "
        f"(issue #435). pytest commands found: {[c for c in commands if 'pytest' in c]}"
    )
