"""Guard against merge-conflict markers reaching a tracked file.

This exists because one did. ``scripts/README.md`` carried a full conflict block
(``<<<<<<< HEAD`` / ``=======`` / ``>>>>>>> d8ef6c6``) on master from PR #392, and
**every gate stayed green**: ``tests/test_repo_layout.py`` only asserts that each tracked
``scripts/`` file is *listed* in the README, and a conflict block lists both sides, so the
malformed file satisfied it. That is the defect class this repo keeps finding -- unassessable
or malformed input collapsing into the "clean" bucket -- applied to CI itself.

Two deliberate design choices, both of which the first version of this file got wrong:

**Scan RAW BYTES, never decoded text.** Conflict markers are ASCII, so decoding buys nothing and
costs correctness: git will happily merge non-UTF-8 content and insert ASCII markers into it. The
first version decoded as UTF-8 and ``continue``d on failure, which silently skipped 110 tracked
files and would have missed a marker in any of them -- while carrying a comment claiming the
opposite. Reading bytes removes the failure mode rather than reporting it.

**An unreadable path FAILS; it is never skipped.** A file we cannot read is not a file we have
cleared. It is named in the failure so the reason is actionable.

Detection deliberately ignores the bare ``=======`` separator: it is also a valid Markdown setext
H1 underline, so flagging it would fire on ordinary prose. The opening and closing markers are
unambiguous on their own, and a conflict block always carries both.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Built at runtime so this file never contains a literal marker and cannot flag itself.
OPEN_MARKER = b"<" * 7 + b" "
CLOSE_MARKER = b">" * 7 + b" "

# All three line-ending conventions, CRLF first so it counts as ONE separator and line
# numbers stay correct. Splitting on b"\n" alone let a lone-CR file hide a marker mid-"line".
LINE_BREAK = re.compile(rb"\r\n|\r|\n")


def tracked_files(root: Path = REPO_ROOT) -> list[str]:
    """Every file git tracks, which is the set any gate in this repo is allowed to read."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return [name for name in result.stdout.decode("utf-8", "surrogateescape").split("\0") if name]


def scan_for_markers(root: Path, names: Iterable[str]) -> tuple[list[str], list[str]]:
    """Scan ``names`` under ``root`` for conflict markers.

    Returns ``(offenders, unreadable)``. Both are failures -- ``unreadable`` is reported
    separately only so the message says *why* a path could not be cleared.
    """
    offenders: list[str] = []
    unreadable: list[str] = []
    for name in names:
        try:
            blob = (root / name).read_bytes()
        except OSError as exc:
            unreadable.append(f"{name}: {exc.__class__.__name__}: {exc}")
            continue
        for number, line in enumerate(LINE_BREAK.split(blob), start=1):
            if line.startswith(OPEN_MARKER) or line.startswith(CLOSE_MARKER):
                shown = line[:60].decode("utf-8", "replace")
                offenders.append(f"{name}:{number}: {shown}")
    return offenders, unreadable


def test_no_tracked_file_contains_a_merge_conflict_marker() -> None:
    """A tracked file carrying a conflict marker is a failed merge, never intended content."""
    offenders, unreadable = scan_for_markers(REPO_ROOT, tracked_files())

    problems: list[str] = []
    if offenders:
        problems.append("merge-conflict markers found in tracked files:\n" + "\n".join(offenders))
    if unreadable:
        problems.append("tracked files could not be read, so they are NOT cleared:\n" + "\n".join(unreadable))
    assert not problems, "\n\n".join(problems)


def test_a_marker_in_undecodable_bytes_is_still_found(tmp_path: Path) -> None:
    """Git merges non-UTF-8 content too, and inserts ASCII markers into it."""
    (tmp_path / "latin1.txt").write_bytes(b"caf\xe9\n" + OPEN_MARKER + b"HEAD\n")

    offenders, unreadable = scan_for_markers(tmp_path, ["latin1.txt"])

    assert unreadable == []
    assert len(offenders) == 1 and offenders[0].startswith("latin1.txt:2:")


def test_a_marker_in_binary_content_is_still_found(tmp_path: Path) -> None:
    """Nothing is exempt by file type; the scan never decodes, so nothing can be skipped."""
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\n" + CLOSE_MARKER + b"deadbee\n")

    offenders, unreadable = scan_for_markers(tmp_path, ["blob.bin"])

    assert unreadable == []
    assert len(offenders) == 1 and offenders[0].startswith("blob.bin:2:")


def test_an_unreadable_path_is_reported_not_skipped(tmp_path: Path) -> None:
    """A file we cannot read is not a file we have cleared."""
    offenders, unreadable = scan_for_markers(tmp_path, ["does-not-exist.txt"])

    assert offenders == []
    assert len(unreadable) == 1 and unreadable[0].startswith("does-not-exist.txt: FileNotFoundError")


def test_a_bare_setext_underline_is_not_a_finding(tmp_path: Path) -> None:
    """``=======`` is valid Markdown, so flagging it would fire on ordinary prose."""
    (tmp_path / "doc.md").write_bytes(b"Heading\n" + b"=" * 7 + b"\n\nbody\n")

    offenders, unreadable = scan_for_markers(tmp_path, ["doc.md"])

    assert offenders == []
    assert unreadable == []


def test_every_line_ending_convention_is_scanned(tmp_path: Path) -> None:
    """Splitting on b"\\n" alone let a lone-CR file hide a marker mid-"line"."""
    for name, blob in {
        "lf.txt": b"a\n" + OPEN_MARKER + b"HEAD\n",
        "crlf.txt": b"a\r\n" + OPEN_MARKER + b"HEAD\r\n",
        "cr.txt": b"a\r" + OPEN_MARKER + b"HEAD\r",
    }.items():
        (tmp_path / name).write_bytes(blob)
        offenders, unreadable = scan_for_markers(tmp_path, [name])

        assert unreadable == []
        assert len(offenders) == 1, f"{name} escaped the scan"
        # CRLF must count as ONE separator, or reported line numbers drift.
        assert offenders[0].startswith(f"{name}:2:"), offenders[0]


def test_a_marker_not_at_line_start_is_not_a_finding(tmp_path: Path) -> None:
    """Prose may legitimately quote a marker; only a line-start occurrence is a failed merge."""
    (tmp_path / "prose.md").write_bytes(b"see " + OPEN_MARKER + b"HEAD in the docs\n")

    offenders, unreadable = scan_for_markers(tmp_path, ["prose.md"])

    assert offenders == []
    assert unreadable == []
