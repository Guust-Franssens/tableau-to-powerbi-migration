"""Source-contract tests for ``scripts/preflight.ps1`` severity tiers."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "scripts" / "preflight.ps1"


def _preflight_source() -> str:
    return PREFLIGHT.read_text(encoding="utf-8")


def _assert_add_check_tier(source: str, check_name: str, tier: str) -> None:
    """Assert EVERY emission of ``check_name`` carries ``tier`` - not merely one of them.

    A check is often emitted from more than one branch (the engine block emits ``engine: single
    source`` from both the verdict path and the could-not-verify path). Asserting "some occurrence is
    critical" lets a real downgrade hide behind a sibling branch: a mutation that weakened the
    verdict-path tier to ``optional`` survived that weaker assertion (measured 2026-08-13).
    """
    found = re.findall(rf"Add-Check\s+'{re.escape(check_name)}'\s+'(\w+)'(?=\s|$)", source)
    assert found, f"{check_name!r} must be emitted by preflight"
    assert all(seen == tier for seen in found), f"{check_name!r} must be tiered {tier!r} everywhere, saw {found}"


def _assert_add_cli_tier(source: str, command: str, tier: str) -> None:
    """Assert every emission of a CLI check carries ``tier`` (see `_assert_add_check_tier`)."""
    found = re.findall(rf"Add-Cli\s+'{re.escape(command)}'\s+'(\w+)'(?=\s|$)", source)
    assert found, f"cli: {command!r} must be emitted by preflight"
    assert all(seen == tier for seen in found), f"cli: {command!r} must be tiered {tier!r} everywhere, saw {found}"


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
        "engine: plugin installed",
        "engine: single source",
        "Power BI Desktop",
        "PBI_DESKTOP_PATH (bridge exe pin)",
    ):
        _assert_add_check_tier(source, check_name, "critical")

    for command in ("npx", "powerbi-desktop", "dotnet"):
        _assert_add_cli_tier(source, command, "critical")


def test_the_engine_check_asks_the_one_resolver_rather_than_listing_paths_itself() -> None:
    """A second copy of the candidate list IS the bug (#107) - in PowerShell as much as in Python.

    Preflight must delegate to `scripts/engine_source.py --json`, so there is exactly one definition
    of "where an engine can be", and no way for the check and the pipeline to disagree about it.
    """
    source = _preflight_source()
    assert "engine_source.py') --json" in source, "preflight must read the verdict from engine_source.py"
    assert "tableau-fabric-skills/skills/tableau-migration" not in source, (
        "preflight is re-deriving an engine path; that list belongs only in scripts/engine_source.py"
    )


def test_an_unverifiable_engine_check_fails_rather_than_being_skipped() -> None:
    """If the verdict cannot be obtained, preflight must MISS, never quietly omit the check.

    A silently absent check reads exactly like a passing one in the rendered output, which is the
    same false-green shape this whole script exists to prevent.
    """
    source = _preflight_source()
    block = source[source.index("$engineStatus = $null") : source.index("# --- Skill plugins ---")]
    assert "else {" in block, "no else-branch: an unobtainable engine verdict would be skipped silently"
    _assert_add_check_tier(block, "engine: single source", "critical")
    assert block.count("Add-Check 'engine: single source'") == 2, (
        "both the verdict path and the fallback path must emit the single-source check"
    )


def test_the_upstream_engine_check_stays_opt_in_and_advisory() -> None:
    """Being behind upstream is not an error, and preflight must not pay for the network by default.

    The orchestrator runs plain preflight on EVERY migration; a mandatory round trip there is a tax
    on every run, and the timing rule already says upgrading mid-migration is the worse mistake.
    """
    source = _preflight_source()
    upstream_block = source[source.index("if ($CheckUpstream) {") :]
    assert "Add-Check 'upstream: conversion engine' 'optional'" in upstream_block
    assert "upstream_version_url" in upstream_block, "the URL belongs to engine_source.py, not to preflight"
