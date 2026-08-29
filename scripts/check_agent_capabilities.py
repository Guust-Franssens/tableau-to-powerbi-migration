"""
purpose: Verify shipped capabilities are wired into agent-reachable guidance.
usage:   python scripts/check_agent_capabilities.py
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "docs" / "agent-capability-wiring.md"
AGENT_FILE_RE = re.compile(r"^\.github/agents/[^/]+\.agent\.md$")
SCRIPT_INVENTORY_HEADING = "## Script capability inventory"
VISIBLE_TEXT_PATTERNS = (
    re.compile(r"<!--.*?-->", re.DOTALL),
    re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE),
)
USAGE_LINE_RE = re.compile(r"^\s*usage\s*:", re.IGNORECASE | re.MULTILINE)
INTERNAL_MARKER_RE = re.compile(r"^\s*internal\s*:\s*true\s*$", re.IGNORECASE | re.MULTILINE)
INTERNAL_REASON_RE = re.compile(r"^\s*internal-reason\s*:\s*(\S.*)$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Capability:
    """One capability token that must be visible in a file agents can actually see."""

    token: str
    why: str
    agent: str
    reachable_in: str
    wording: str
    line_number: int


@dataclass(frozen=True)
class ScriptCapability:
    """One runnable script declared by its module docstring."""

    path: str
    internal: bool
    internal_reason: str | None


@dataclass(frozen=True)
class ScriptClassification:
    """One registry row classifying a runnable script."""

    status: str
    reason: str
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


def parse_script_inventory(registry_path: Path = REGISTRY_PATH) -> dict[str, ScriptClassification]:
    """Read explicit script-capability classifications from the registry."""
    classifications: dict[str, ScriptClassification] = {}
    in_inventory = False
    for line_number, line in enumerate(registry_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped == SCRIPT_INVENTORY_HEADING:
            in_inventory = True
            continue
        if in_inventory and stripped.startswith("## "):
            break
        if not in_inventory or not stripped.startswith("|"):
            continue
        cells = _split_table_row(stripped)
        if len(cells) != 3 or cells[0] in {"Script", "---"} or set(cells[0]) == {"-"}:
            continue
        classifications[_normalise_slashes(_strip_inline_code(cells[0]))] = ScriptClassification(
            status=_strip_inline_code(cells[1]).lower(),
            reason=cells[2],
            line_number=line_number,
        )
    return classifications


def _is_agent_reachable(path: str) -> bool:
    """Files a subagent can be directed to read or receives in its persona."""
    return path in {"AGENTS.md", "docs/INDEX.md"} or bool(AGENT_FILE_RE.match(path))


def _script_wiring_paths(repo_root: Path) -> list[Path]:
    """Files whose visible prose can route agents to runnable script capabilities."""
    paths = [
        repo_root / "AGENTS.md",
        repo_root / "docs" / "INDEX.md",
        repo_root / "scripts" / "README.md",
    ]
    paths.extend(sorted((repo_root / ".github" / "agents").glob("*.agent.md")))
    return paths


def _visible_text(text: str) -> str:
    """Remove locations that make a token visible to grep but not to a reading agent."""
    visible = text
    for pattern in VISIBLE_TEXT_PATTERNS:
        visible = pattern.sub("", visible)
    return visible


def _tracked_script_paths(repo_root: Path) -> list[Path]:
    """Return tracked Python scripts, falling back to the filesystem for fixture repos."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "scripts/*.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return [repo_root / Path(path) for path in result.stdout.splitlines()]
    return sorted((repo_root / "scripts").glob("*.py"))


def _module_docstring(path: Path) -> str:
    """Return a Python file's module docstring, or an empty string when it has none."""
    try:
        return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    except (OSError, SyntaxError):
        return ""


def _declared_script_capabilities(repo_root: Path) -> list[ScriptCapability]:
    """Find scripts whose docstring declares a runnable usage line."""
    scripts: list[ScriptCapability] = []
    for script_path in _tracked_script_paths(repo_root):
        docstring = _module_docstring(script_path)
        if not USAGE_LINE_RE.search(docstring):
            continue
        reason_match = INTERNAL_REASON_RE.search(docstring)
        scripts.append(
            ScriptCapability(
                path=_normalise_slashes(str(script_path.relative_to(repo_root))),
                internal=bool(INTERNAL_MARKER_RE.search(docstring)),
                internal_reason=reason_match.group(1).strip() if reason_match else None,
            )
        )
    return scripts


def _agent_reachable_script_text(repo_root: Path) -> str:
    """Concatenate visible prose from script-routing documents that exist in this checkout."""
    chunks: list[str] = []
    for path in _script_wiring_paths(repo_root):
        if path.is_file():
            chunks.append(_visible_text(path.read_text(encoding="utf-8")))
    return "\n".join(chunks)


def _script_is_wired(script: ScriptCapability, visible_text: str) -> bool:
    """Whether a runnable script is named in visible script-routing prose."""
    return script.path in visible_text or Path(script.path).name in visible_text


def _validate_script_capabilities(repo_root: Path, registry_path: Path) -> list[str]:
    """Require every runnable script to be wired or explicitly internal."""
    errors: list[str] = []
    visible_text = _agent_reachable_script_text(repo_root)
    scripts = _declared_script_capabilities(repo_root)
    classifications = parse_script_inventory(registry_path)
    for script in scripts:
        classification = classifications.get(script.path)
        if classification is None and not script.internal:
            errors.append(
                f"{script.path} declares usage: in its module docstring, but is absent from "
                f"{registry_path.relative_to(repo_root)}'s script capability inventory"
            )
            continue
        if script.internal:
            if not script.internal_reason:
                errors.append(f"{script.path} declares usage: and internal: true, but has no internal-reason line")
            continue
        if classification.status not in {"agent-facing", "internal"}:
            errors.append(
                f"line {classification.line_number}: {script.path} has unknown script inventory status "
                f"{classification.status!r}; use agent-facing or internal"
            )
            continue
        if not classification.reason:
            errors.append(f"line {classification.line_number}: {script.path} has no script inventory reason")
            continue
        if classification.status == "internal":
            continue
        if not _script_is_wired(script, visible_text):
            errors.append(
                f"{script.path} declares usage: in its module docstring, but is not wired into "
                "agent-reachable script guidance and is not marked internal with an internal-reason"
            )
    declared_paths = {script.path for script in scripts}
    for script_path in sorted(set(classifications) - declared_paths):
        errors.append(f"{script_path} is listed in the script capability inventory but is not a usage-declared script")
    return errors


def validate(repo_root: Path = REPO_ROOT, registry_path: Path | None = None) -> list[str]:
    """Return human-actionable capability wiring failures."""
    registry = registry_path or repo_root / "docs" / "agent-capability-wiring.md"
    errors: list[str] = []
    registry_text = registry.read_text(encoding="utf-8")
    capabilities = parse_registry(registry)
    if not capabilities:
        errors.append(f"{registry.relative_to(repo_root)} has no capability rows")
    if SCRIPT_INVENTORY_HEADING not in registry_text:
        errors.append(
            f"{registry.relative_to(repo_root)} is missing {SCRIPT_INVENTORY_HEADING}; "
            "script capability scan was not evaluated"
        )

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
    if SCRIPT_INVENTORY_HEADING in registry_text:
        errors.extend(_validate_script_capabilities(repo_root, registry))
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
