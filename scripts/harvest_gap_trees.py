"""
purpose: read two directory trees and report how they differ, keeping unreadable entries apart from
         identical ones and RETAINING the digests the comparison actually consumed.
usage:   imported by scripts/harvest_engine_gaps.py; not a user-facing CLI

Split out of `harvest_engine_gaps.py` for the same two reasons `harvest_gap_shapes.py` was: this
answers an independent question - "how do these two directories differ?" - needing neither the
engine's hash baseline nor any notion of provenance, and the seam buys both modules headroom under
pylint's `max-module-lines`.

⚠️ **`TreeDelta` carries `baseline_digests` / `working_digests`, and they are load-bearing, not
diagnostics.** Blind review round 7 of PR #399: the harvest adjudicated provenance from one read of
the bundle and then compared trees in a second, so an ABA edit - change a working visual before the
scan, let the scan read it, restore the original bytes before any later check - left both endpoint
reads identical while the comparison had consumed the CHANGED bytes. The run reported
`status=complete`, `snapshot_race.count=0` and `engine_internal=1` for a real tier edit. Comparing
two independent re-reads cannot see that; comparing what adjudication ASSUMED against what the scan
ACTUALLY READ can, because there is no window between the observation and the evidence - the
observation *is* the evidence. So the digests leave this module rather than dying inside it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from migration_bundle import sha256_file

# `Path.relative_to` renders a tree's own root as ".", so a traversal failure AT the root carries
# that as its relative path. It is not a file name; it means "everything here".
TREE_ROOT = "."


class TreeDelta(NamedTuple):
    """The raw content comparison of two trees, with unreadable entries kept apart.

    `baseline_digests` / `working_digests` are every digest `hash_tree` produced, keyed by
    tree-relative POSIX path - the exact bytes this delta was computed from, so a caller can prove
    its own earlier assumptions describe the same read (see the module header).
    """

    added: list[str]
    removed: list[str]
    changed: list[str]
    unassessable: list[dict[str, str]]
    baseline_files: int
    working_files: int
    longest_path: int
    scoped: bool = True
    baseline_digests: dict[str, str] = {}
    working_digests: dict[str, str] = {}
    blocked: frozenset[str] = frozenset()


def safe_text(text: str) -> str:
    """A console/JSON-safe rendering of a path that may carry undecodable bytes."""
    return text.encode("utf-8", "backslashreplace").decode("ascii", "replace")


def hash_tree(root: Path) -> tuple[dict[str, str], list[dict[str, str]], int]:
    """Hash every file under `root`, returning (by-relative-path, unreadable, longest full path).

    Unreadable entries are returned SEPARATELY and are never given a digest, because a file that
    cannot be read is not a file that is the same - the single defect shape this repo keeps
    re-introducing. Each carries `relative` when one could be computed, so the caller can withdraw
    that path from BOTH sides of a comparison rather than letting it masquerade as an addition.
    """
    digests: dict[str, str] = {}
    unreadable: list[dict[str, str]] = []
    longest = 0
    root_str = str(root)

    def on_error(exc: OSError) -> None:
        failed = str(getattr(exc, "filename", "") or root_str)
        record = {"path": safe_text(failed), "reason": f"{type(exc).__name__}: {exc.strerror or exc}"}
        try:
            record["relative"] = Path(failed).relative_to(root).as_posix()
        except ValueError:
            pass
        unreadable.append(record)

    for dirpath, dirnames, filenames in os.walk(root_str, onerror=on_error):
        for name in list(dirnames) + list(filenames):
            longest = max(longest, len(os.path.join(dirpath, name)))
        for name in filenames:
            full = Path(dirpath) / name
            relative = None
            try:
                relative = full.relative_to(root).as_posix()
                digests[relative] = sha256_file(full)
            except (OSError, ValueError) as exc:
                record = {"path": safe_text(str(full)), "reason": f"{type(exc).__name__}: {exc}"}
                if relative is not None:
                    record["relative"] = relative
                unreadable.append(record)
    return digests, unreadable, longest


def withdraw(keys: set[str], blocked: frozenset[str]) -> set[str]:
    """Drop every key that IS a blocked path or lives BENEATH one.

    ⚠️ Exact-equality withdrawal is not enough, and that gap fabricated evidence. `os.walk` reports
    only the DIRECTORY it could not enter, so an unreadable `pages/blocked/` withdrew exactly one
    key - `pages/blocked` - while every descendant visible on the *other* side (`pages/blocked/
    visual.json`) stayed in the comparison and was counted as an addition. Measured (blind review of
    PR #399): an injected `PermissionError` on one baseline directory produced an unassessable record
    for the directory AND a fabricated `delta.added` entry beneath it.

    ⚠️ A failure at the TREE ROOT relativises to `"."`, which no key is prefixed by, so the same
    round-2 fix still let a blocked root through: the second review measured `status=incomplete` with
    both working files nonetheless counted as additions and attributed `engine_internal`. `"."` means
    the whole tree, and is handled as such.
    """
    if TREE_ROOT in blocked:
        return set()
    return {key for key in keys if key not in blocked and not any(key.startswith(f"{p}/") for p in blocked)}


def compare_trees(baseline: Path, working: Path) -> TreeDelta:
    """Content-compare two trees without git, so a long path is assessed rather than skipped."""
    a, a_bad, a_longest = hash_tree(baseline)
    b, b_bad, b_longest = hash_tree(working)
    unassessable = a_bad + b_bad
    # A path that could not be read on EITHER side is withdrawn from BOTH key sets, with everything
    # beneath it, so it can never masquerade as an addition or a removal. Matching is done on the
    # POSIX relative path, not the rendered absolute one: `Path(root) / "a/b"` stringifies to
    # `root\a/b` on Windows and would never match the `root\a\b` that `os.walk` produced.
    blocked = frozenset(record["relative"] for record in unassessable if "relative" in record)
    # A failure whose relative path could not be computed at all cannot be scoped, so nothing about
    # this pair can be trusted: the caller suppresses its difference records entirely rather than
    # reporting a subset that looks complete.
    scoped = not any("relative" not in record for record in unassessable)
    a_keys = withdraw(set(a), blocked)
    b_keys = withdraw(set(b), blocked)
    return TreeDelta(
        added=sorted(b_keys - a_keys),
        removed=sorted(a_keys - b_keys),
        changed=sorted(k for k in a_keys & b_keys if a[k] != b[k]),
        unassessable=unassessable,
        baseline_files=len(a),
        working_files=len(b),
        longest_path=max(a_longest, b_longest),
        scoped=scoped,
        baseline_digests=a,
        working_digests=b,
        blocked=blocked,
    )
