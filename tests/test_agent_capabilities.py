"""Tests for the agent capability wiring gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


def _write_file(root: Path, relative: str, text: str = "VALUE = 1\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _tier_fixture(tmp_path: Path, script_rows: str) -> Path:
    registry = _write_registry(
        tmp_path, "Use example-flag only after the model edit cannot change source rows.", script_rows=script_rows
    )
    _write_agent(tmp_path, "# Agent\n\nReach for example-flag only for formula-only work.\n")
    return registry


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


@pytest.mark.parametrize(
    ("files", "rows", "expected"),
    [
        pytest.param(
            ["tool.ps1"], "", "scripts/tool.ps1 has no tier row in docs/agent-capability-wiring.md", id="unlisted-ps1"
        ),
        pytest.param(
            ["lib.py"], "", "scripts/lib.py has no tier row in docs/agent-capability-wiring.md", id="unlisted-library"
        ),
        pytest.param(
            ["lib.py"],
            "| `scripts/lib.py` | `internal` | |\n| `scripts/lib.py` | `internal` | |\n",
            "scripts/lib.py is listed twice in the script capability inventory",
            id="duplicate-row",
        ),
        pytest.param(
            ["lib.py"],
            "| `scripts/lib.py` | `agent-facing` | Old status. |\n",
            "scripts/lib.py has unknown tier 'agent-facing'",
            id="retired-tier",
        ),
        pytest.param(
            [],
            "| `scripts/gone.py` | `internal` | |\n",
            "scripts/gone.py has a tier row but is not a tracked script",
            id="stale-row",
        ),
    ],
)
def test_every_tracked_script_needs_one_valid_tier_row(
    tmp_path: Path, files: list[str], rows: str, expected: str
) -> None:
    """R1: every tracked .py/.ps1 script, with or without usage:, has exactly one valid tier row."""
    registry = _tier_fixture(tmp_path, rows)
    for name in files:
        _write_file(tmp_path, f"scripts/{name}")

    errors = cac.validate(repo_root=tmp_path, registry_path=registry)

    assert len(errors) == 1, errors
    assert expected in errors[0]


def test_core_script_named_only_in_scripts_readme_fails(tmp_path: Path) -> None:
    """R2: the full script catalog is not a default route, so it cannot carry a core script."""
    registry = _tier_fixture(tmp_path, "| `scripts/core_tool.py` | `core` | |\n")
    _write_file(tmp_path, "scripts/core_tool.py")
    _write_file(tmp_path, "scripts/README.md", "| `core_tool.py` | Does the core thing. | agent |\n")

    errors = cac.validate(repo_root=tmp_path, registry_path=registry)

    assert errors == ["core script scripts/core_tool.py is not named in AGENTS.md or docs/INDEX.md"]
    _write_file(tmp_path, "AGENTS.md", "Run `python scripts/core_tool.py`.\n")
    assert cac.validate(repo_root=tmp_path, registry_path=registry) == []


def test_advanced_script_in_dispatcher_doc_fails_even_inside_a_fence(tmp_path: Path) -> None:
    """R4: agents read fenced commands, so a fence still routes; an HTML comment does not."""
    registry = _tier_fixture(tmp_path, "| `scripts/adv_tool.py` | `advanced` | |\n")
    _write_file(tmp_path, "scripts/adv_tool.py")
    index = _write_file(tmp_path, "docs/INDEX.md", "# Index\n\n<!-- python scripts/adv_tool.py -->\n")
    assert cac.validate(repo_root=tmp_path, registry_path=registry) == []

    index.write_text("# Index\n\n```text\npython scripts/adv_tool.py\n```\n", encoding="utf-8")
    errors = cac.validate(repo_root=tmp_path, registry_path=registry)

    assert errors == ["advanced script scripts/adv_tool.py named in docs/INDEX.md"]


def test_persona_may_name_advanced_but_not_internal(tmp_path: Path) -> None:
    """R3: personas are specialised on-demand routes for advanced scripts, never for internal ones."""
    registry = _write_registry(
        tmp_path,
        "Use example-flag only after the model edit cannot change source rows.",
        script_rows="| `scripts/adv_tool.py` | `advanced` | |\n| `scripts/int_tool.py` | `internal` | |\n",
    )
    _write_agent(tmp_path, "# Agent\n\nUse example-flag. Run `adv_tool.py`, never `scripts/int_tool.py`.\n")
    _write_file(tmp_path, "scripts/adv_tool.py")
    _write_file(tmp_path, "scripts/int_tool.py")

    errors = cac.validate(repo_root=tmp_path, registry_path=registry)

    assert errors == ["internal script scripts/int_tool.py named in .github/agents/pbi-semantic-builder.agent.md"]
