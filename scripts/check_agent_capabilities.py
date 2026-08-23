"""
purpose: Verify shipped capabilities are wired into agent-reachable guidance.
usage:   python scripts/check_agent_capabilities.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "docs" / "agent-capability-wiring.md"
AGENT_FILE_RE = re.compile(r"^\.github/agents/[^/]+\.agent\.md$")
VISIBLE_TEXT_PATTERNS = (
    re.compile(r"<!--.*?-->", re.DOTALL),
    re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE),
)


@dataclass(frozen=True)
class Capability:
    """One capability token that must be visible in a file agents can actually see."""

    token: str
    why: str
    agent: str
    reachable_in: str
    wording: str
    line_number: int


def _split_table_row(line: str) -> list[str]:
    """Split the registry's simple Markdown table row."""
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _strip_inline_code(value: str) -> str:
    """Convert a whole-cell inline-code value to its text form."""
    if value.startswith("`") and value.endswith("`"):
        return value[1:-1]
    return value


def _normalise_slashes(path: str) -> str:
    """Use POSIX-style repo-relative paths in the registry on every platform."""
    return path.replace("\\", "/")


def parse_registry(registry_path: Path = REGISTRY_PATH) -> list[Capability]:
    """Read capability rows from docs/agent-capability-wiring.md."""
    capabilities: list[Capability] = []
    in_registry = False
    for line_number, line in enumerate(registry_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped == "## Capability registry":
            in_registry = True
            continue
        if in_registry and stripped.startswith("## "):
            break
        if not in_registry or not stripped.startswith("|"):
            continue
        cells = _split_table_row(stripped)
        if len(cells) != 5 or cells[0] in {"Token", "---"} or set(cells[0]) == {"-"}:
            continue
        capabilities.append(
            Capability(
                token=_strip_inline_code(cells[0]),
                why=cells[1],
                agent=_strip_inline_code(cells[2]),
                reachable_in=_normalise_slashes(_strip_inline_code(cells[3])),
                wording=cells[4],
                line_number=line_number,
            )
        )
    return capabilities


def _is_agent_reachable(path: str) -> bool:
    """Files a subagent can be directed to read or receives in its persona."""
    return path in {"AGENTS.md", "docs/INDEX.md"} or bool(AGENT_FILE_RE.match(path))


def _visible_text(text: str) -> str:
    """Remove locations that make a token visible to grep but not to a reading agent."""
    visible = text
    for pattern in VISIBLE_TEXT_PATTERNS:
        visible = pattern.sub("", visible)
    return visible


def validate(repo_root: Path = REPO_ROOT, registry_path: Path | None = None) -> list[str]:
    """Return human-actionable capability wiring failures."""
    registry = registry_path or repo_root / "docs" / "agent-capability-wiring.md"
    errors: list[str] = []
    capabilities = parse_registry(registry)
    if not capabilities:
        return [f"{registry.relative_to(repo_root)} has no capability rows"]

    for capability in capabilities:
        target = repo_root / capability.reachable_in
        if not _is_agent_reachable(capability.reachable_in):
            errors.append(
                f"line {capability.line_number}: {capability.token} targets {capability.reachable_in}, "
                "which is not agent-reachable (use a persona, AGENTS.md, or docs/INDEX.md)"
            )
            continue
        if not target.exists():
            errors.append(
                f"line {capability.line_number}: {capability.token} targets missing file {capability.reachable_in}"
            )
            continue
        if capability.token not in _visible_text(target.read_text(encoding="utf-8")):
            errors.append(
                f"{capability.token} is not wired into {capability.reachable_in}. "
                f"Why: {capability.why} Needed by: {capability.agent}. "
                f"Suggested visible wording: {capability.wording}"
            )
    return errors


def main() -> int:
    """CLI entry point."""
    errors = validate()
    if errors:
        print("Agent capability wiring check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Agent capability wiring check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
