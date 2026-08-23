"""Tests for the agent capability wiring gate."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_agent_capabilities as cac  # noqa: E402  # pylint: disable=wrong-import-position


def _write_registry(root: Path, wording: str) -> Path:
    registry = root / "docs" / "agent-capability-wiring.md"
    registry.parent.mkdir()
    registry.write_text(
        "# Agent capability wiring registry\n\n"
        "## Capability registry\n\n"
        "| Token | Why it exists | Agent that needs it | Reachable in | "
        "Suggested agent-facing wording |\n"
        "|---|---|---|---|---|\n"
        "| `example-flag` | Prevents stale data after a fast path is misused. | "
        "`pbi-semantic-builder` | `.github/agents/pbi-semantic-builder.agent.md` | "
        f"{wording} |\n",
        encoding="utf-8",
    )
    return registry


def test_registry_is_wired_in_current_tree() -> None:
    """The committed registry must be satisfied by visible agent-facing docs."""
    assert cac.validate() == []


def test_comment_or_code_block_mentions_do_not_satisfy_gate(tmp_path: Path) -> None:
    """The gate requires a visible capability token, not any grep-visible token."""
    wording = "Use example-flag only after the model edit cannot change source rows."
    registry = _write_registry(tmp_path, wording)
    agent = tmp_path / ".github" / "agents" / "pbi-semantic-builder.agent.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(
        "# Agent\n\n<!-- example-flag -->\n\n```text\nexample-flag\n```\n"
        "Use the fast path only after the model edit cannot change source rows.\n",
        encoding="utf-8",
    )

    errors = cac.validate(repo_root=tmp_path, registry_path=registry)

    assert len(errors) == 1
    assert "example-flag is not wired" in errors[0]
    assert "Prevents stale data after a fast path is misused" in errors[0]
    assert "Needed by: pbi-semantic-builder" in errors[0]
    assert "Suggested visible wording:" in errors[0]
    assert "Add this visible wording exactly" not in errors[0]


def test_visible_token_satisfies_gate_even_when_guidance_is_reworded(tmp_path: Path) -> None:
    """Editorial changes should not be indistinguishable from deleting the capability."""
    wording = "Use example-flag only after the model edit cannot change source rows."
    registry = _write_registry(tmp_path, wording)
    agent = tmp_path / ".github" / "agents" / "pbi-semantic-builder.agent.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("# Agent\n\nReach for example-flag only for formula-only work.\n", encoding="utf-8")

    assert cac.validate(repo_root=tmp_path, registry_path=registry) == []
