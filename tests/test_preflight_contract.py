"""Source-contract tests for ``scripts/preflight.ps1`` severity tiers."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "scripts" / "preflight.ps1"


def _preflight_source() -> str:
    return PREFLIGHT.read_text(encoding="utf-8")


def _assert_add_check_tier(source: str, check_name: str, tier: str) -> None:
    pattern = rf"Add-Check\s+'{re.escape(check_name)}'\s+'{tier}'(?=\s|$)"
    assert re.search(pattern, source), f"{check_name!r} must be tiered {tier!r}"


def _assert_add_cli_tier(source: str, command: str, tier: str) -> None:
    pattern = rf"Add-Cli\s+'{re.escape(command)}'\s+'{tier}'(?=\s|$)"
    assert re.search(pattern, source), f"cli: {command!r} must be tiered {tier!r}"


def test_known_blocking_preflight_checks_are_critical() -> None:
    """Checks documented as migration blockers must not silently become warning-only.

    The renderer exits non-zero only for CRITICAL checks. These names are source-contract tests rather
    than a full PowerShell harness because CI runs on Ubuntu, while the script intentionally depends on
    Windows/Power BI Desktop primitives.
    """
    source = _preflight_source()

    for check_name in (
        "skill bundles installed",
        "skill bundles match published plugin",
        "Power BI Desktop",
        "PBI_DESKTOP_PATH (bridge exe pin)",
    ):
        _assert_add_check_tier(source, check_name, "critical")

    _assert_add_cli_tier(source, "powerbi-desktop", "critical")
