"""Tests for the agent capability wiring gate."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_agent_capabilities as cac  # noqa: E402  # pylint: disable=wrong-import-position


def _write_registry(
    root: Path,
    wording: str,
    *,
    include_script_inventory: bool = True,
    script_rows: str = "",
) -> Path:
    registry = root / "docs" / "agent-capability-wiring.md"
    registry.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Agent capability wiring registry\n\n"
        "## Capability registry\n\n"
        "| Token | Why it exists | Agent that needs it | Reachable in | "
        "Suggested agent-facing wording |\n"
        "|---|---|---|---|---|\n"
        "| `example-flag` | Prevents stale data after a fast path is misused. | "
        "`pbi-semantic-builder` | `.github/agents/pbi-semantic-builder.agent.md` | "
        f"{wording} |\n"
    )
    if include_script_inventory:
        content += f"\n## Script capability inventory\n\n| Script | Status | Reason |\n|---|---|---|\n{script_rows}"
    registry.write_text(content, encoding="utf-8")
    return registry


def _write_agent(root: Path, text: str = "example-flag") -> Path:
    agent = root / ".github" / "agents" / "pbi-semantic-builder.agent.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(text, encoding="utf-8")
    return agent


def _write_usage_script(root: Path, name: str, docstring_lines: list[str] | None = None) -> Path:
    script = root / "scripts" / name
    script.parent.mkdir()
    lines = docstring_lines or [
        "purpose: Exercise the script capability inventory.",
        f"usage:   python scripts/{name}",
    ]
    script.write_text('"""\n' + "\n".join(lines) + '\n"""\n\nVALUE = 1\n', encoding="utf-8")
    return script


def test_registry_is_wired_in_current_tree() -> None:
    """The committed registry must be satisfied by visible agent-facing docs."""
    assert cac.validate() == []


def test_comment_or_code_block_mentions_do_not_satisfy_gate(tmp_path: Path) -> None:
    """The gate requires a visible capability token, not any grep-visible token."""
    wording = "Use example-flag only after the model edit cannot change source rows."
    registry = _write_registry(tmp_path, wording)
    _write_agent(
        tmp_path,
        "# Agent\n\n<!-- example-flag -->\n\n```text\nexample-flag\n```\n"
        "Use the fast path only after the model edit cannot change source rows.\n",
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
    _write_agent(tmp_path, "# Agent\n\nReach for example-flag only for formula-only work.\n")

    assert cac.validate(repo_root=tmp_path, registry_path=registry) == []


def test_script_inventory_heading_is_required_even_without_candidate_scripts(tmp_path: Path) -> None:
    """An absent derived-script check is not the same state as an empty derived result."""
    registry = _write_registry(
        tmp_path,
        "Use example-flag only after the model edit cannot change source rows.",
        include_script_inventory=False,
    )
    _write_agent(tmp_path, "# Agent\n\nReach for example-flag only for formula-only work.\n")

    errors = cac.validate(repo_root=tmp_path, registry_path=registry)

    assert len(errors) == 1
    assert "script capability scan was not evaluated" in errors[0]


def test_present_empty_script_inventory_passes_without_candidate_scripts(tmp_path: Path) -> None:
    """A present scan with no usage-declared scripts is an evaluated empty result."""
    registry = _write_registry(tmp_path, "Use example-flag only after the model edit cannot change source rows.")
    _write_agent(tmp_path, "# Agent\n\nReach for example-flag only for formula-only work.\n")

    assert cac.validate(repo_root=tmp_path, registry_path=registry) == []


def test_usage_script_without_agent_reachable_wiring_fails(tmp_path: Path) -> None:
    """A usage-declared script cannot be silently absent from the routing surface."""
    registry = _write_registry(
        tmp_path,
        "Use example-flag only after the model edit cannot change source rows.",
        script_rows="| `scripts/forgotten_tool.py` | `agent-facing` | Should be documented. |\n",
    )
    _write_agent(tmp_path, "# Agent\n\nReach for example-flag only for formula-only work.\n")
    _write_usage_script(tmp_path, "forgotten_tool.py")

    errors = cac.validate(repo_root=tmp_path, registry_path=registry)

    assert len(errors) == 1
    assert "scripts/forgotten_tool.py declares usage:" in errors[0]
    assert "not wired into agent-reachable script guidance" in errors[0]


def test_usage_script_absent_from_inventory_fails_before_wiring(tmp_path: Path) -> None:
    """A usage-declared script must be registered before wiring can be credited."""
    registry = _write_registry(tmp_path, "Use example-flag only after the model edit cannot change source rows.")
    _write_agent(tmp_path, "# Agent\n\nReach for example-flag only for formula-only work.\n")
    _write_usage_script(tmp_path, "forgotten_tool.py")
    readme = tmp_path / "scripts" / "README.md"
    readme.write_text("| `forgotten_tool.py` | Does the documented thing. | agent |\n", encoding="utf-8")

    errors = cac.validate(repo_root=tmp_path, registry_path=registry)

    assert len(errors) == 1
    assert "scripts/forgotten_tool.py declares usage:" in errors[0]
    assert "absent from" in errors[0]
    assert "script capability inventory" in errors[0]


def test_usage_script_wired_in_scripts_readme_passes(tmp_path: Path) -> None:
    """Visible script guidance is the registered present-and-empty success state."""
    registry = _write_registry(
        tmp_path,
        "Use example-flag only after the model edit cannot change source rows.",
        script_rows="| `scripts/documented_tool.py` | `agent-facing` | Documented script. |\n",
    )
    _write_agent(tmp_path, "# Agent\n\nReach for example-flag only for formula-only work.\n")
    _write_usage_script(tmp_path, "documented_tool.py")
    readme = tmp_path / "scripts" / "README.md"
    readme.write_text("| `documented_tool.py` | Does the documented thing. | agent |\n", encoding="utf-8")

    assert cac.validate(repo_root=tmp_path, registry_path=registry) == []


def test_internal_usage_script_needs_reason(tmp_path: Path) -> None:
    """Internal is an explicit reasoned state, not another spelling of absent."""
    registry = _write_registry(tmp_path, "Use example-flag only after the model edit cannot change source rows.")
    _write_agent(tmp_path, "# Agent\n\nReach for example-flag only for formula-only work.\n")
    _write_usage_script(
        tmp_path,
        "private_tool.py",
        [
            "purpose: Exercise the script capability inventory.",
            "usage:   python scripts/private_tool.py",
            "internal: true",
        ],
    )

    errors = cac.validate(repo_root=tmp_path, registry_path=registry)

    assert len(errors) == 1
    assert "scripts/private_tool.py declares usage: and internal: true" in errors[0]
    assert "has no internal-reason line" in errors[0]


def test_internal_usage_script_with_reason_passes(tmp_path: Path) -> None:
    """A reasoned internal marker is the deliberate-exclusion state."""
    registry = _write_registry(tmp_path, "Use example-flag only after the model edit cannot change source rows.")
    _write_agent(tmp_path, "# Agent\n\nReach for example-flag only for formula-only work.\n")
    _write_usage_script(
        tmp_path,
        "private_tool.py",
        [
            "purpose: Exercise the script capability inventory.",
            "usage:   python scripts/private_tool.py",
            "internal: true",
            "internal-reason: called only by a generated fixture harness.",
        ],
    )

    assert cac.validate(repo_root=tmp_path, registry_path=registry) == []
