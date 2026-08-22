"""
purpose: Verify docs/INDEX.md is a complete navigation contract for agent-facing repo knowledge.
usage:   python scripts/check_navigation_index.py
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "docs" / "INDEX.md"
PROMPT_AGENT_DIR = ".github/agents/"
SENTINEL_SKILL = ".github/skills/sentinel-probe/SKILL.md"
SELF_PATH = "docs/INDEX.md"


@dataclass(frozen=True)
class Entry:
    """One indexed or excluded repo-relative path from docs/INDEX.md."""

    path: str
    line_number: int
    reason: str
    link: str | None = None


def _git_files() -> set[str]:
    """Return tracked plus untracked, non-ignored files so local additions fail before commit."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return {item.replace("\\", "/") for item in result.stdout.decode("utf-8").split("\0") if item}


def _is_candidate(path: str) -> bool:
    """Whether a file is in the index's promised coverage universe.

    Inclusion rule: all Markdown knowledge/navigation files, all Power BI visual-cookbook JSON fixtures,
    and every `scripts/check_*.py` gate are eligible. Candidates must be either indexed exactly once or
    explicitly excluded exactly once with a reason in docs/INDEX.md.
    """
    if path.endswith(".md"):
        return True
    if path.startswith(".github/pbi.kb/") and path.endswith(".json"):
        return True
    return path.startswith("scripts/check_") and path.endswith(".py")


def eligible_files() -> set[str]:
    """All files the navigation contract must account for."""
    return {path for path in _git_files() if _is_candidate(path)}


def _split_table_row(line: str) -> list[str]:
    """Split a simple Markdown table row without supporting escaped pipes."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _extract_link(cell: str, line_number: int) -> tuple[str, str | None]:
    """Extract [`path`](target) or `path` from an index path cell."""
    if cell.startswith("[`"):
        close = cell.find("`](")
        end = cell.rfind(")")
        if close == -1 or end == -1 or end < close:
            raise ValueError(f"line {line_number}: malformed path link cell: {cell}")
        return cell[2:close], cell[close + 3 : end]
    if cell.startswith("`") and cell.endswith("`"):
        return cell[1:-1], None
    raise ValueError(f"line {line_number}: path cell must be [`path`](link) or `path`: {cell}")


def parse_index() -> tuple[list[Entry], list[Entry]]:
    """Return (indexed_entries, explicit_exclusions)."""
    indexed: list[Entry] = []
    excluded: list[Entry] = []
    in_exclusions = False

    for line_number, line in enumerate(INDEX_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith("## Explicit exclusions"):
            in_exclusions = True
            continue
        if line.startswith("## ") and not line.startswith("## Explicit exclusions"):
            in_exclusions = False
        if not line.startswith("|"):
            continue
        cells = _split_table_row(line)
        if len(cells) < 3 or cells[0] in {"Task", "---"} or set(cells[0]) == {"-"}:
            continue
        path, link = _extract_link(cells[1], line_number)
        reason = cells[2].strip()
        entry = Entry(path=path, line_number=line_number, reason=reason, link=link)
        if in_exclusions:
            excluded.append(entry)
        else:
            indexed.append(entry)
    return indexed, excluded


def _target_exists(path: str) -> bool:
    return (REPO_ROOT / path).exists()


def _link_matches_path(entry: Entry) -> bool:
    if entry.link is None:
        return True
    target = (INDEX_PATH.parent / entry.link).resolve()
    expected = (REPO_ROOT / entry.path).resolve()
    return target == expected


def _duplicates(entries: list[Entry]) -> dict[str, list[int]]:
    seen: dict[str, list[int]] = {}
    for entry in entries:
        seen.setdefault(entry.path, []).append(entry.line_number)
    return {path: lines for path, lines in seen.items() if len(lines) > 1}


def _entry_errors(label: str, entries: list[Entry]) -> list[str]:
    """Validate duplicate entries, reasons, filesystem targets, and markdown links."""
    errors: list[str] = []
    for path, lines in _duplicates(entries).items():
        errors.append(f"{path} appears more than once in {label} entries (lines {lines})")
    for entry in entries:
        if not entry.reason or entry.reason == "—":
            errors.append(f"line {entry.line_number}: {entry.path} has no read/exclusion reason")
        if not _target_exists(entry.path):
            errors.append(f"line {entry.line_number}: referenced path does not exist: {entry.path}")
        if not _link_matches_path(entry):
            errors.append(f"line {entry.line_number}: link target does not resolve to {entry.path}: {entry.link}")
    return errors


def _coverage_errors(indexed_paths: set[str], excluded_paths: set[str], eligible: set[str]) -> list[str]:
    """Validate reverse coverage: every eligible file is indexed or explicitly excluded."""
    errors: list[str] = []
    for path in sorted(indexed_paths & excluded_paths):
        errors.append(f"{path} is both indexed and explicitly excluded")
    for path in sorted(eligible - indexed_paths - excluded_paths):
        errors.append(f"eligible file is missing from docs/INDEX.md: {path}")
    for path in sorted((indexed_paths | excluded_paths) - eligible - {SELF_PATH, SENTINEL_SKILL}):
        errors.append(f"non-eligible path is listed; update the inclusion rule or remove it: {path}")
    return errors


def _entrypoint_errors(indexed_paths: set[str], eligible: set[str]) -> list[str]:
    """Validate the two runtime entry-point families are reachable from the index."""
    errors: list[str] = []
    for required in ("AGENTS.md", ".github/copilot-instructions.md"):
        if required in eligible and required not in indexed_paths:
            errors.append(f"required root entry is not indexed: {required}")
    for agent in sorted(path for path in eligible if path.startswith(PROMPT_AGENT_DIR)):
        if agent not in indexed_paths:
            errors.append(f"subagent persona is not indexed: {agent}")
    return errors


def validate() -> list[str]:
    """Return validation errors; an empty list means the index is complete."""
    indexed, excluded = parse_index()
    indexed_paths = {entry.path for entry in indexed}
    excluded_paths = {entry.path for entry in excluded}
    eligible = eligible_files()

    errors = _entry_errors("indexed", indexed)
    errors.extend(_entry_errors("excluded", excluded))
    errors.extend(_coverage_errors(indexed_paths, excluded_paths, eligible))
    errors.extend(_entrypoint_errors(indexed_paths, eligible))
    return errors


def main() -> int:
    """CLI entry point."""
    errors = validate()
    if errors:
        print("Navigation index check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Navigation index check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
