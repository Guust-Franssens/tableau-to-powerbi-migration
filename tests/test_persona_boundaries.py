"""Pin the few persona boundaries whose loss would be unsafe (#733); everything else is review's job."""

from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parents[1] / ".github" / "agents"
WRITE_TOOLS = {"edit", "create", "apply_patch", "write", "str_replace_editor", "*"}

SHARED = (
    "AUTOPILOT / auto-approve DOES NOT override a credential stop",
    "never silently patch toolkit/engine to clear a gate",
    "run patched code only with explicit human approval",
    "Never edit upstream of where you are.",
    "`pbi-migration-validator` is read-only and never edits.",
    "it reports; the orchestrator routes.",
    "Never declare something done on a green validator alone.",
    "never sweep by name.",
)

PER_PERSONA = {
    "pbi-migration-validator": (
        "Read-only override:",
        "never self-promote",
        "engine claims, never verification",
    ),
    "tableau-migrator": (
        "no `-Update`",
        "Task/subagent tool ONLY",
        "Even one-line repairs go to the owner.",
        "Only `python scripts\\check_unit.py <package> --scope all --receipt-sha256 <H>` establishes COMPLETE",
    ),
    "dry-run-operator": (
        "no toolkit/engine/model/TMDL/DAX/PBIR repairs or publication",
        "human STOP, even autopilot",
        "git check-ignore",
        "Never publish rows/captures/briefs/originals",
    ),
    "pbi-report-builder": (
        "Never `management`, publish or rebind",
        "no self-refresh",
        "No self-issued COMPLETE",
    ),
    "pbi-semantic-builder": (
        "never edit PBIR",
        "click or type into Desktop",
        "No self-issued AI-ready, fidelity or COMPLETE",
    ),
}


def _read(name: str) -> str:
    return (AGENTS / f"{name}.agent.md").read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.replace("**", "").split())


CASES = [(name, needle) for name in PER_PERSONA for needle in (*SHARED, *PER_PERSONA[name])]


@pytest.mark.parametrize(("name", "needle"), CASES)
def test_persona_keeps_its_boundary_sentence(name: str, needle: str) -> None:
    assert _flat(needle) in _flat(_read(name)), f"{name} lost: {needle}"


def test_validator_can_view_but_has_no_write_tool() -> None:
    front = _read("pbi-migration-validator").split("---")[1]
    line = next(item for item in front.splitlines() if item.startswith("tools:"))
    tools = {t.strip().strip('"') for t in line.removeprefix("tools:").strip().strip("[]").split(",")}
    assert "view" in tools
    assert not tools & WRITE_TOOLS
