"""Two smoke tests for every tracked `scripts/*.py`, both born from one dry run.

Filed as F003/F004 in `migrations/dry-run-2026-08-11/findings.md`: a fresh agent hit both defects in
the first minutes of a real assessment, on the script that is most load-bearing for getting started
(`harvest_estate_assets.py`, the site->folder bridge).

* **F003** - on Windows, `python scripts/<name>.py --help` crashed with `UnicodeEncodeError` for 3 of
  the tracked scripts. Windows defaults stdout/stderr to the legacy cp1252 codec, and several module
  docstrings (used verbatim as the argparse ``description``) contain non-ASCII characters such as the
  warning glyph "warning-sign variation-selector-16" that cp1252 cannot encode - argparse dies while
  printing `--help`, before a user sees anything. This is simulated here via `PYTHONIOENCODING=cp1252`
  rather than requiring an actual Windows runner.
* **F004** - `harvest_estate_assets.py`'s own module docstring documented a `--workbooks-only` flag
  that `argparse` never implemented, so copying the script's own documented invocation failed with
  `error: unrecognized arguments: --workbooks-only`.

Both were "trivially detectable and neither was caught" (the issue's own words) - these tests are the
point, so this class does not recur the next time someone pastes a smart character or a stale flag
into a docstring.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

def _tracked_scripts() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "scripts/*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(REPO_ROOT / rel for rel in tracked)


SCRIPTS = _tracked_scripts()
assert SCRIPTS, "no tracked scripts found under scripts/*.py - this guard now proves nothing"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_help_exits_zero_on_a_cp1252_stdout(script: Path) -> None:
    """`--help` must not crash before it can print anything, on any platform.

    Windows' cp1252 default is reproduced here without a Windows runner by forcing
    `PYTHONIOENCODING=cp1252` on a real subprocess - the same mechanism the issue's own repro used
    (`$env:PYTHONUTF8` unset) to confirm the cause.
    """
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{script.name} --help exited {result.returncode} under a cp1252 stdout:\n{result.stderr[-2000:]}"
    )


def test_transpiler_missing_arguments_prints_usage() -> None:
    """The research transpiler must reject wrong arity without a traceback."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/transpile_tableau_calc.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert "IndexError" not in result.stderr


def _documented_flags(text: str) -> list[str]:
    """Long options (`--foo`) appearing in the module docstring's `usage:` block."""
    match = re.search(r"^usage:(.*?)\n\n", text, re.DOTALL | re.MULTILINE)
    if not match:
        return []
    return sorted(set(re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", match.group(1))))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_documented_flag_is_actually_implemented(script: Path) -> None:
    """Every `--flag` a script's own `usage:` line advertises must appear somewhere in its code.

    `harvest_estate_assets.py` documented `--workbooks-only` in its usage line for a version that
    never added the `argparse.add_argument` for it - the flag existed in exactly ONE place in the
    file (the docstring itself). A flag that is genuinely implemented appears at least twice: once
    in the usage example, once in the `add_argument` call (or subparser) that defines it. A handful
    of scripts are FORWARDING SHIMS to a script bundled inside `.github/skills/` (see
    `tests/test_repo_layout.py`'s TREE_SCANNERS note on the same pattern) - their own file never
    mentions the flag at all, so the shim's declared target is read too before judging it missing.
    """
    text = script.read_text(encoding="utf-8")
    flags = _documented_flags(text)
    if not flags:
        return

    combined = text
    shim_target = re.search(r"\.github/skills/\S+\.py", text)
    if shim_target:
        target_path = REPO_ROOT / shim_target.group(0)
        if target_path.is_file():
            combined += target_path.read_text(encoding="utf-8")

    undocumented = [flag for flag in flags if combined.count(flag) <= 1]
    assert not undocumented, (
        f"{script.name} documents {undocumented} in its usage: line, but the flag appears nowhere "
        "else in the script (or its forwarding target) - implement it or remove it from the docstring."
    )
