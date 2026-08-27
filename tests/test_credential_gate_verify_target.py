"""Regression guard for issue #354: the final-gate `verify` command must target the audit-bearing
bundle, never the ship-destination copy.

`credential_gate.py verify` reads the audit log at **exactly** the path given, and nowhere else. The
engine flow copies the built artifacts to `migrations/{workbooks,datasources}/<slug>/fabric/` at
sign-off, but the audit log stays in the bundle. So a `verify` pointed at the ship destination finds
no `block` entry, reports a false *"no gate was ever applied"*, and exits 0 on the very artifacts the
bundle flagged unshippable.

`tableau-migrator.agent.md` step 15 shipped exactly that: `verify migrations/workbooks/<name>`. This
test fails if any persona's `credential_gate.py verify` COMMAND targets a ship-destination path again.
It deliberately matches only the literal command form, so the prose warning that *tells* a reader not
to point `verify` at `migrations/.../fabric/` (a `verify` span and a path span, no command prefix
between them) does not trip it.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / ".github" / "agents"

# `... credential_gate.py verify <ARG>` — capture the target, stopping at whitespace or a closing
# backtick. Requires the `credential_gate.py verify ` command prefix, so a bare `verify` mentioned in
# prose does not match.
VERIFY_CMD = re.compile(r"credential_gate\.py\s+verify\s+([^\s`]+)")

# A ship-destination / gated-copy target: the copied deliverable trees, or any `fabric/` path segment.
# The audit log lives in the bundle root, never in these, so a `verify` here proves nothing.
SHIP_TARGET = re.compile(r"migrations/(workbooks|datasources)/|(^|/)fabric(/|$)", re.IGNORECASE)


def _agent_files() -> list[Path]:
    return sorted(AGENTS_DIR.glob("*.agent.md"))


def test_agents_dir_is_populated() -> None:
    """The personas we scan must actually exist, or the guard would pass vacuously."""
    assert _agent_files(), f"no persona files found under {AGENTS_DIR}"


@pytest.mark.parametrize("persona", _agent_files(), ids=lambda p: p.name)
def test_verify_command_never_targets_ship_destination(persona: Path) -> None:
    """No persona may document a `credential_gate.py verify` command aimed at a ship copy (#354)."""
    text = persona.read_text(encoding="utf-8")
    offenders = [arg for arg in VERIFY_CMD.findall(text) if SHIP_TARGET.search(arg)]
    assert not offenders, (
        f"{persona.name}: a `credential_gate.py verify` command points at a ship-destination copy "
        f"{offenders}. The audit log lives in the bundle, so `verify` there returns a false OK "
        f"(#354). Target the `<bundle>` (or the migration/spec dir), not migrations/.../fabric/."
    )


def test_final_gate_documents_the_bundle_target() -> None:
    """tableau-migrator's final gate must document `verify <bundle>`, the audit-bearing target."""
    text = (AGENTS_DIR / "tableau-migrator.agent.md").read_text(encoding="utf-8")
    targets = VERIFY_CMD.findall(text)
    assert targets, "tableau-migrator no longer documents any `credential_gate.py verify` command"
    assert all(not SHIP_TARGET.search(t) for t in targets), (
        f"a documented verify command targets a ship path: {targets}"
    )
    assert "<bundle>" in targets, (
        f"the final gate should verify `<bundle>` (where the engine-path audit history lives); found targets {targets}"
    )
