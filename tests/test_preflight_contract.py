"""Source-contract tests for ``scripts/preflight.ps1`` severity tiers.

Two kinds of test live here. Most are **source contracts**: CI runs on Ubuntu while the script
deliberately depends on Windows/Power BI Desktop primitives, so the tiers and hints are asserted by
reading the source. The JWT-decoder tests at the bottom are **executed**, because a base64url
padding bug is invisible to any amount of source reading and would make the wrong-tenant check
silently answer "cannot decode" on exactly the tokens it exists to inspect.
"""

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "scripts" / "preflight.ps1"

TENANT_CHECK = "Fabric token tenant"
DESKTOP_PIN_CHECK = "PBI_DESKTOP_PATH (bridge exe pin)"


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

    ``PBI_DESKTOP_PATH (bridge exe pin)`` was on this list until #124 and is deliberately no longer
    here: it is a Desktop-only pin, and the estate pipeline never opens Desktop. Its tier is pinned
    from the other side by `test_the_desktop_pin_warns_rather_than_blocking_a_run_that_never_opens_it`,
    so it cannot drift back to critical *or* fade to an invisible optional.
    """
    source = _preflight_source()

    for check_name in (
        "skill bundles installed",
        "skill bundles match published plugin",
        "engine: plugin installed",
        "engine: single source",
        "Power BI Desktop",
    ):
        _assert_add_check_tier(source, check_name, "critical")

    for command in ("npx", "powerbi-desktop", "dotnet"):
        _assert_add_cli_tier(source, command, "critical")


def test_the_desktop_pin_warns_rather_than_blocking_a_run_that_never_opens_it() -> None:
    """A Desktop-only pin must not exit 1 on a pipeline that never opens Desktop (#124).

    Measured: a machine with the engine, both plugins, both CLIs at known-good versions, Desktop
    installed, `az`, `uv` and ODBC 18 was reported NOT READY over this one unset variable - while
    `run_estate.py`'s own docstring says it "never opens Power BI Desktop". Spending exit 1, which the
    runbook defines as "resolve before migrating", on an item the runbook never names is how an
    operator learns to ignore exit 1.

    ``recommended`` is the exact tier this needs: still rendered, still counted in the summary line,
    but not a blocker. ``optional`` would be a downgrade too far - the failure it prevents (Desktop
    auto-updates, the bridge can no longer find the exe) is real.
    """
    _assert_add_check_tier(_preflight_source(), DESKTOP_PIN_CHECK, "recommended")


def test_the_desktop_pin_hint_is_actionable_in_the_shell_that_reads_it() -> None:
    """`setx` cannot fix the session that is reading the hint, so it cannot be the only advice.

    `setx` writes the user profile and is inherited only by processes started later; an agent's tool
    shells inherit an already-running parent's environment. Following the old hint verbatim therefore
    left preflight failing in the very session that printed it. `$env:` is the form that takes effect
    immediately, so it must lead, and any `setx` offered alongside must say it does not affect the
    current shell.
    """
    source = _preflight_source()
    hint = source[source.index(f"Add-Check '{DESKTOP_PIN_CHECK}'") :].split("\n\n")[0]
    assert "$env:PBI_DESKTOP_PATH = " in hint, "the hint must give the form that works in THIS shell"
    if "setx" in hint:
        assert re.search(r"(?i)new shells|does NOT affect this one", hint), (
            "a `setx` hint must state that it does not affect the shell reading it"
        )


def test_the_correctness_floor_says_where_report_version_at_import_belongs() -> None:
    """The floor's justification is load-bearing prose, and stating it imprecisely produced a bug (#129).

    Measured against the known-good validator: `reportVersionAtImport` is REQUIRED inside each
    `themeCollection` entry and FORBIDDEN at the top level of `report.json`, which answers
    "must NOT have additional properties". Three documents called it "schema-required in report.json"
    without saying where, and an agent reading exactly that added it beside `$schema` in
    `scripts/probe_live_source.py` - a scaffold that then failed this repo's own gate. Ground truth is
    committed at examples/shipping-kpis/fabric/ShippingKPIs.Report/definition/report.json.
    """
    source = _preflight_source()
    claim = source[source.index("reportVersionAtImport") - 400 : source.index("reportVersionAtImport") + 400]
    assert "themeCollection" in claim, "say WHERE it is required, or the next reader puts it at the top level"
    assert re.search(r"(?i)forbidden at the top level", claim), (
        "the top-level prohibition is the half that was actually acted on incorrectly"
    )


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


# --------------------------------------------------------------------------------------------------
# The wrong-tenant check (#124). A token that MINTS successfully can still be for another tenant, and
# Fabric then answers WorkspaceNotFound for a workspace that exists - measured four times across two
# operators, ~15 minutes each, every time read as a 404 rather than as an identity problem.
# --------------------------------------------------------------------------------------------------


def _tenant_block(source: str) -> str:
    return source[source.index("# --- Fabric token TENANT") : source.index("$odbc = ")]


def test_the_tenant_check_is_emitted_on_every_branch() -> None:
    """Every path must report, because a check that renders as nothing reads as a check that passed.

    Four outcomes exist - nothing declared, `az` absent, no token minted, and the verdict itself -
    and each must produce a line. This is the same rule the engine block already obeys.
    """
    block = _tenant_block(_preflight_source())
    branches = block.count(f"Add-Check '{TENANT_CHECK}'")
    assert branches == 4, f"expected all four tenant-check branches to report, found {branches}"


def test_only_a_tenant_mismatch_is_critical() -> None:
    """Blocking is reserved for the one outcome that is unambiguously wrong.

    A mismatch can only fire after someone has explicitly declared "I intend to deploy into tenant
    X", so it can never block a machine that never declared one - that opt-in is what makes exit 1
    defensible here. Everything else (undeclared, no `az`, no token) is advisory: those are reasons
    the check could not run, not evidence that anything is wrong, and the estate pipeline does not
    touch Fabric at all.
    """
    block = _tenant_block(_preflight_source())
    tiers = re.findall(rf"Add-Check '{re.escape(TENANT_CHECK)}' (\S+)", block)
    literal = [t for t in tiers if t.startswith("'")]
    conditional = [t for t in tiers if not t.startswith("'")]
    assert literal == ["'optional'"] * 3, f"non-verdict branches must stay advisory, saw {literal}"
    assert len(conditional) == 1, "the verdict branch must choose its tier from the comparison"
    assert "$(if ($tenantOk) { 'optional' } else { 'critical' })" in block, (
        "a mismatch must be critical and a match must not be"
    )


def test_the_tenant_check_costs_nothing_until_a_tenant_is_declared() -> None:
    """No declaration, no `az` call: preflight runs before EVERY migration, including parse-only ones.

    A mandatory token mint here would tax every run for a check that cannot say anything useful
    without an intended tenant to compare against - the same reasoning that keeps `-CheckUpstream`
    opt-in.
    """
    block = _tenant_block(_preflight_source())
    guard = block.index("if (-not $intendedTenant)")
    assert guard < block.index("'get-access-token'"), "the token mint must sit behind the declaration guard"
    resolution = block[block.index("$intendedTenant = ") : guard]
    assert " az " not in resolution, "resolving the intended tenant must not shell out to az"


def test_the_wrong_tenant_hint_prefers_the_non_mutating_fix() -> None:
    """`az account set` rewrites the CLI profile on disk; every other process on the machine follows.

    That is hostile when other work is in flight, so the hint must lead with per-call scoping and
    must not present the global mutation as the plain fix.

    The `--subscription` text is asserted on `$subHint` rather than on the hint literal because the
    hint interpolates it: on a mismatch the script looks up a subscription that actually lives in the
    intended tenant, so the advice is copy-pasteable rather than a `<placeholder>`. Both branches of
    that lookup - one found, none found - must still name the flag.
    """
    block = _tenant_block(_preflight_source())
    sub_hint = re.search(r"\$subHint = .*", block)
    assert sub_hint, "the mismatch hint must build a --subscription suggestion"
    assert sub_hint.group(0).count("--subscription") == 2, (
        "both the found-a-subscription and no-subscription-found branches must name the flag"
    )
    hint = block[block.index("This is an IDENTITY problem") :]
    assert "$subHint" in hint, "the hint must actually offer the per-call, non-mutating fix"
    assert "az account set" in hint and re.search(r"(?i)profile on disk|process on this machine", hint), (
        "if `az account set` is mentioned it must carry the process-global caveat"
    )


def test_no_output_path_can_carry_the_token() -> None:
    """The token is a bearer credential: it must reach the decoder and nothing else.

    Enumerating the allowed uses (rather than grepping `Write-Host` for it) is what makes this
    mutation-proof: any new line that touches the token - a debug print, a richer Detail string, an
    error message echoing `az` output - is a line this test has never seen and therefore fails.
    """
    block = _tenant_block(_preflight_source())
    allowed = (
        re.compile(r"^#"),  # commentary
        re.compile(r"^\$tokenJson = & az @azArgs 2>\$null$"),
        re.compile(r"^if \(\$tokenJson\) \{$"),
        re.compile(r"^try \{ \$actualTenant = Get-JwtTenantId .*accessToken.*$"),
    )
    for line in block.splitlines():
        stripped = line.strip()
        if "tokenJson" not in stripped and "accessToken" not in stripped:
            continue
        assert any(pattern.match(stripped) for pattern in allowed), f"the token escapes into: {stripped}"


# --- Executed, not merely read: the base64url decode ------------------------------------------------
# A padding or charset bug here fails CLOSED in the worst way - the check would report "could not
# decode" for real tokens and never fire, while every source-contract test above still passed.

_PS = shutil.which("pwsh") or shutil.which("powershell")

# Extract the decoder from the script by AST and dot-source only that function, so running these
# tests never executes preflight itself (which probes npm, Desktop and the plugin cache).
_HARNESS = """
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:PREFLIGHT_PS1, [ref]$null, [ref]$null)
$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                            $n.Name -eq 'Get-JwtTenantId' }, $true)
if (-not $fn) { Write-Output 'FUNCTION-NOT-FOUND'; exit 3 }
. ([scriptblock]::Create($fn.Extent.Text))
$r = Get-JwtTenantId $env:PREFLIGHT_TEST_TOKEN
if ([string]::IsNullOrEmpty($r)) { Write-Output 'NULL' } else { Write-Output $r }
"""

_TID = "11111111-2222-3333-4444-555555555555"


def _jwt(payload: dict) -> str:
    """A JWT-shaped string. Only the middle segment is ever read, so header/signature are stubs."""
    body = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode()
    return f"header.{body.rstrip('=')}.signature"


def _payload_with_stripped_padding(remainder: int) -> dict:
    """A payload whose base64url segment is `remainder` mod 4 long once its '=' padding is stripped.

    Real tokens arrive unpadded, and `FromBase64String` rejects an unpadded string, so every
    remainder has to be reconstructed correctly - 0, 2 and 3 are the three that can occur.
    """
    for size in range(64):
        payload = {"tid": _TID, "pad": "x" * size}
        if len(_jwt(payload).split(".")[1]) % 4 == remainder:
            return payload
    raise AssertionError(f"no payload found with segment length {remainder} mod 4")


def _payload_using_the_url_safe_alphabet() -> dict:
    """A payload whose encoding actually contains '-' and '_'.

    base64url substitutes those for '+' and '/', which `FromBase64String` rejects. Most ASCII
    payloads never produce either, so a decoder that forgets the substitution passes every casual
    test and then fails on a real token. Multi-byte UTF-8 fillers are what reliably set the high bits
    that encode to those two characters.
    """
    for filler in ("\u07ff", "\u00ff", "\uffff", "\u0080"):
        for size in range(1, 64):
            payload = {"tid": _TID, "pad": filler * size}
            segment = _jwt(payload).split(".")[1]
            if "-" in segment and "_" in segment:
                return payload
    raise AssertionError("no payload found that exercises the base64url alphabet")


def _decode(token: str) -> str:
    # -EncodedCommand rather than piping to `-Command -`: PowerShell 7 consumes stdin line by line,
    # which silently truncates a multi-line script block (measured: empty output, exit 0). Base64
    # UTF-16LE is also the one form that needs no shell quoting on either platform.
    encoded = base64.b64encode(_HARNESS.encode("utf-16-le")).decode()
    result = subprocess.run(  # noqa: S603
        [_PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PREFLIGHT_PS1": str(PREFLIGHT), "PREFLIGHT_TEST_TOKEN": token},
    )
    assert "FUNCTION-NOT-FOUND" not in result.stdout, "Get-JwtTenantId was renamed or removed"
    assert result.returncode == 0, f"decoder harness failed: {result.stderr.strip()[:400]}"
    return result.stdout.strip()


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("padding 0 mod 4", _payload_with_stripped_padding(0)),
        ("padding 2 mod 4", _payload_with_stripped_padding(2)),
        ("padding 3 mod 4", _payload_with_stripped_padding(3)),
        ("base64url alphabet", _payload_using_the_url_safe_alphabet()),
    ],
)
def test_the_tid_survives_every_base64url_shape(label: str, payload: dict) -> None:
    """The decoded tenant is what the whole check compares against; it must never be approximate."""
    assert _decode(_jwt(payload)) == _TID, label


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-jwt",
        "header..signature",
        "header.@@@@.signature",
        _jwt({"aud": "https://api.fabric.microsoft.com"}),  # a real token shape, minus the tid claim
    ],
)
def test_an_undecodable_token_yields_nothing_rather_than_a_wrong_answer(token: str) -> None:
    """No tid must read as "cannot verify", never as a tenant.

    An exception here would abort the check, and a garbage value would compare unequal to the
    intended tenant and raise a WRONG TENANT alarm on a perfectly good machine. Both are worse than
    the advisory WARN the script emits for an absent tid.
    """
    assert _decode(token) == "NULL"
