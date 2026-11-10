"""
purpose: Verify shipped capabilities are wired into agent-reachable guidance and every script has a tier.
usage:   python scripts/check_agent_capabilities.py
"""

from __future__ import annotations

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
HTML_COMMENT_RE = VISIBLE_TEXT_PATTERNS[0]
TIERS = ("core", "advanced", "internal")
CORE_ROUTES = ("AGENTS.md", "docs/INDEX.md")
DISPATCHER_DOCS = ("AGENTS.md", ".github/copilot-instructions.md", "docs/INDEX.md")


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
class ScriptClassification:
    """One registry row assigning a tracked script its tier."""

    path: str
    tier: str
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


def parse_script_inventory(registry_path: Path = REGISTRY_PATH) -> list[ScriptClassification]:
    """Read every script tier row from the registry, keeping duplicates so the gate can name them."""
    rows: list[ScriptClassification] = []
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
        rows.append(
            ScriptClassification(
                path=_normalise_slashes(_strip_inline_code(cells[0])),
                tier=_strip_inline_code(cells[1]).lower(),
                reason=cells[2],
                line_number=line_number,
            )
        )
    return rows


def _is_agent_reachable(path: str) -> bool:
    """Files a subagent can be directed to read or receives in its persona."""
    return path in {"AGENTS.md", "docs/INDEX.md"} or bool(AGENT_FILE_RE.match(path))


def _visible_text(text: str) -> str:
    """Remove locations that make a token visible to grep but not to a reading agent."""
    visible = text
    for pattern in VISIBLE_TEXT_PATTERNS:
        visible = pattern.sub("", visible)
    return visible


def _tracked_script_paths(repo_root: Path) -> list[str]:
    """Return tracked scripts, falling back to the filesystem for fixture repos."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "scripts/*.py", "scripts/*.ps1"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return sorted(_normalise_slashes(path) for path in result.stdout.splitlines())
    scripts_dir = repo_root / "scripts"
    return sorted(f"scripts/{path.name}" for pattern in ("*.py", "*.ps1") for path in scripts_dir.glob(pattern))


def _routing_texts(repo_root: Path) -> dict[str, str]:
    """Dispatcher docs and personas as an agent reads them: fences count, HTML comments do not."""
    paths = list(DISPATCHER_DOCS)
    paths.extend(
        f".github/agents/{path.name}" for path in sorted((repo_root / ".github" / "agents").glob("*.agent.md"))
    )
    return {
        path: HTML_COMMENT_RE.sub("", (repo_root / path).read_text(encoding="utf-8"))
        for path in paths
        if (repo_root / path).is_file()
    }


def _names_script(text: str, script_path: str) -> bool:
    """Whether text names the script file, bare or behind a path prefix."""
    return re.search(r"(?<![\w.-])" + re.escape(Path(script_path).name), text) is not None


def _validate_script_tiers(repo_root: Path, registry_path: Path) -> list[str]:
    """R1 one valid tier row per tracked script; R2-R4 tiers decide where a script may be named."""
    errors: list[str] = []
    registry_name = _normalise_slashes(str(registry_path.relative_to(repo_root)))
    tracked = _tracked_script_paths(repo_root)
    rows = parse_script_inventory(registry_path)
    tiers: dict[str, str] = {}
    listed: set[str] = set()
    for row in rows:
        if row.path in listed:
            errors.append(f"line {row.line_number}: {row.path} is listed twice in the script capability inventory")
        elif row.tier not in TIERS:
            errors.append(f"line {row.line_number}: {row.path} has unknown tier {row.tier!r}; use {', '.join(TIERS)}")
        else:
            tiers[row.path] = row.tier
        listed.add(row.path)
        if row.path not in tracked:
            errors.append(f"line {row.line_number}: {row.path} has a tier row but is not a tracked script")
    texts = _routing_texts(repo_root)
    for script in tracked:
        if script not in listed:
            errors.append(f"{script} has no tier row in {registry_name}'s script capability inventory")
        tier = tiers.get(script)
        if tier is None:
            continue
        if tier == "core" and not any(_names_script(texts.get(doc, ""), script) for doc in CORE_ROUTES):
            errors.append(f"core script {script} is not named in AGENTS.md or docs/INDEX.md")
        forbidden = {"internal": list(texts), "advanced": list(DISPATCHER_DOCS)}.get(tier, [])
        for doc in forbidden:
            if doc in texts and _names_script(texts[doc], script):
                errors.append(f"{tier} script {script} named in {doc}")
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
        errors.extend(_validate_script_tiers(repo_root, registry))
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
