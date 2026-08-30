"""Guard against merge-conflict markers reaching a tracked file.

This exists because one did. ``scripts/README.md`` carried a full conflict block
(``<<<<<<< HEAD`` / ``=======`` / ``>>>>>>> d8ef6c6``) on master from PR #392, and
**every gate stayed green**: ``tests/test_repo_layout.py`` only asserts that each tracked
``scripts/`` file is *listed* in the README, and a conflict block lists both sides, so the
malformed file satisfied it. That is the defect class this repo keeps finding -- unassessable
or malformed input collapsing into the "clean" bucket -- applied to CI itself.

Detection deliberately ignores the bare ``=======`` separator: it is also a valid Markdown
setext H1 underline, so flagging it would fire on ordinary prose. The opening and closing
markers are unambiguous on their own, and a conflict block always carries both.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Built at runtime so this file never contains a literal marker and cannot flag itself.
OPEN_MARKER = "<" * 7 + " "
CLOSE_MARKER = ">" * 7 + " "


def tracked_files() -> list[str]:
    """Every file git tracks, which is the set any gate is allowed to read."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def test_no_tracked_file_contains_a_merge_conflict_marker() -> None:
    """A tracked file carrying a conflict marker is a failed merge, never intended content."""
    offenders: list[str] = []
    for name in tracked_files():
        path = REPO_ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary or unreadable: not a conflict-marker candidate. Recorded as skipped
            # rather than silently dropped, so the denominator stays honest.
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if line.startswith(OPEN_MARKER) or line.startswith(CLOSE_MARKER):
                offenders.append(f"{name}:{number}: {line[:60]}")

    assert not offenders, "merge-conflict markers found in tracked files:\n" + "\n".join(offenders)
