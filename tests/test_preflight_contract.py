"""Source-contract tests for ``scripts/preflight.ps1`` severity tiers.

Two kinds of test live here. Most are **source contracts**: CI runs on Ubuntu while the script
deliberately depends on Windows/Power BI Desktop primitives, so the tiers and hints are asserted by
reading the source. The tests at the bottom are **executed**, because the two things they cover are
invisible to any amount of source reading: a base64url padding bug would make the wrong-tenant check
silently answer "cannot decode" on exactly the tokens it exists to inspect, and the comparison that
produces the verdict is one line whose two most damaging mutations (``$tenantOk = $true``; an
inverted ``-ne``) both survived a 15-mutation sweep against source-string assertions alone.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "scripts" / "preflight.ps1"

# The OTHER reader of the same file. `deploy_estate.py` resolves `FABRIC_WORKSPACE_ID` from `.env`,
# so preflight's promises about spelling are only worth what the deployer also honours.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import deploy_estate as de  # noqa: E402  # pylint: disable=wrong-import-position

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


def test_bundle_engine_receipt_drift_is_surfaced_as_an_advisory_check() -> None:
    """A stale bundle must name its receipt version without blocking a migration in flight."""
    source = _preflight_source()
    block = source[source.index("# --- Engine receipt drift") : source.index("# --- Skill plugins ---")]
    assert "check_engine_receipts.py" in block
    assert "--root $repoRoot" in block
    _assert_add_check_tier(block, "engine: bundle receipt versions", "optional")


# --------------------------------------------------------------------------------------------------
# The wrong-tenant check (#124). A token that MINTS successfully can still be for another tenant, and
# Fabric then answers WorkspaceNotFound for a workspace that exists - measured four times across two
# operators, ~15 minutes each, every time read as a 404 rather than as an identity problem.
# --------------------------------------------------------------------------------------------------


def _tenant_block(source: str) -> str:
    return source[source.index("# --- Fabric token TENANT") : source.index("$odbc = ")]


def test_the_tenant_variable_is_documented_where_an_operator_would_look_for_it() -> None:
    """`FABRIC_TENANT_ID` is the one input this check has, and the hint text points at `.env`.

    It was previously named only inside `preflight.ps1` itself - so the hint said "put it in .env"
    and `.env.example`, the file that lists what goes in `.env`, did not mention it.

    The value must stay EMPTY. A placeholder like `<your-tenant-id>` would be copied into every
    clone's `.env`, where it becomes a declared-but-uncomparable intent: a permanent warning line
    on every run, for a tenant nobody chose.
    """
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    declarations = [ln for ln in lines if ln.startswith("FABRIC_TENANT_ID=")]
    assert declarations == ["FABRIC_TENANT_ID="], f"expected one empty declaration, saw {declarations}"
    assert any("deploy_estate.py" in ln for ln in lines), "say what the value is FOR, not just its name"


def test_workspace_configuration_is_documented_and_preflight_checks_it() -> None:
    """The required deploy destination must persist next to the optional tenant configuration."""
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    declarations = [ln for ln in lines if ln.startswith("FABRIC_WORKSPACE_ID=")]
    assert declarations == ["FABRIC_WORKSPACE_ID="]
    source = _preflight_source()
    assert "(Get-DotEnvValue 'FABRIC_WORKSPACE_ID')" in source
    assert "Add-Check 'Fabric landing-zone workspace'" in source
    assert 'Invoke-WebRequest -Uri "$fabricResource/v1/workspaces/$workspace"' in source


def test_the_tenant_check_reports_exactly_once_whatever_happens() -> None:
    """One unconditional emission, and a hint for every outcome the verdict can return.

    Four outcomes became six (a malformed intended id, and the verdict itself), and the previous
    shape - one ``Add-Check`` per branch - meant a new outcome could be added with no emission at
    all, which renders as nothing and reads as a check that passed. A single top-level call cannot
    be skipped; what CAN drift is the hint switch, so every ``Kind`` the function returns must have
    a branch there.
    """
    block = _tenant_block(_preflight_source())
    assert block.count(f"Add-Check '{TENANT_CHECK}'") == 1, "the tenant check must be emitted from exactly one place"
    assert re.search(rf"(?m)^Add-Check '{re.escape(TENANT_CHECK)}' ", block), (
        "that one emission must be unconditional (top level), not nested in a branch"
    )
    kinds = set(re.findall(r"Kind = '([a-z-]+)'", block))
    assert len(kinds) >= 6, f"expected every outcome to name a Kind, saw {sorted(kinds)}"
    hint_branches = set(re.findall(r"(?m)^\s{4}'([a-z-]+)' \{", block))
    assert kinds - {"match"} <= hint_branches, f"no hint branch for {sorted(kinds - {'match'} - hint_branches)}"
    # And the emission must carry the VERDICT, not a constant. This one stays a source contract
    # because the wiring can only be executed by running preflight itself, which probes npm, Desktop
    # and the plugin cache and cannot run in CI - but it is exactly where a correct verdict can still
    # be thrown away: rewriting `$verdict.Tier` to `'optional'` leaves every executed test green.
    assert re.search(
        rf"(?m)^Add-Check '{re.escape(TENANT_CHECK)}' \$verdict\.Tier \$verdict\.Ok \$verdict\.Detail \$tenantHint$",
        block,
    ), "the rendered tier/status/detail must come from the verdict object, not from a literal"


def test_critical_is_reserved_for_a_mismatch_declared_on_this_run() -> None:
    """Exit 1 must follow deploy INTENT, and only ``-Tenant`` declares it for the run in hand.

    ``FABRIC_TENANT_ID`` in ``.env`` is persisted configuration: set once for a deploy, it then
    outlives that run and would block every later parse-only estate sweep - whose steps 1-6 never
    call Fabric - re-creating from the other side the false blocker this change set removed (a
    Desktop-only pin failing a run that never opens Desktop). ``-Tenant <id>`` is a statement about
    THIS invocation, so a wrong token there is unambiguously a blocker.

    The tier mapping itself is executed by `test_the_verdict_tiers_follow_where_the_intent_came_from`;
    this pins that ``critical`` appears nowhere else in the block, so no other outcome can acquire it.
    """
    block = _tenant_block(_preflight_source())
    assigns = [ln.strip() for ln in block.splitlines() if re.search(r"\$tier\s*=", ln)]
    assert assigns == ["$tier = if ($IntentIsExplicit) { 'critical' } else { 'recommended' }"], (
        f"exactly one line may decide the tier, and only from explicit intent, saw {assigns}"
    )
    others = [ln.strip() for ln in block.splitlines() if "'critical'" in ln and not re.search(r"\$tier\s*=", ln)]
    assert all("-eq 'critical'" in ln for ln in others), f"'critical' is assigned somewhere else too: {others}"
    assert "IsExplicit = [bool]$candidates[0]" in block, (
        "explicit intent means the -Tenant parameter - the FIRST channel - not an ambient environment variable"
    )
    assert "-IntentIsExplicit $intent.IsExplicit" in block, "the verdict must be told which channel actually won"


def test_the_tenant_check_costs_nothing_until_a_comparable_tenant_is_declared() -> None:
    """No declaration, no `az` call: preflight runs before EVERY migration, including parse-only ones.

    A mandatory token mint here would tax every run for a check that cannot say anything useful
    without an intended tenant to compare against - the same reasoning that keeps `-CheckUpstream`
    opt-in. Resolving a declared DOMAIN now costs a second `az` call, so the contract is about the
    top-level FLOW rather than the whole block: every `az` invocation the flow reaches must sit
    inside a branch that has already established something was declared.

    There are now TWO things worth minting a token for - a comparable tenant, or a landing-zone
    workspace to probe - so the guard is a disjunction. Both disjuncts are still declarations, which
    is the property being pinned: a parse-only machine that declares neither reaches no `az` call.
    """
    block = _tenant_block(_preflight_source())
    flow = block[block.index("$intent = Resolve-IntendedTenant") :]
    first_guard = flow.index("if ($intendedTenant")
    for line in flow.splitlines():
        if "& az" in line and not line.strip().startswith("#"):
            assert line.startswith((" ", "\t")), f"an unguarded `az` invocation at the top of the flow: {line}"
    assert first_guard < flow.index("& az"), "no `az` invocation may precede the declaration guard"
    mint_guard = "if ((($intendedTenant -and (Test-TenantIdShape $intendedTenant)) -or $workspace) -and $azPresent) {"
    guard = flow.index(mint_guard)
    assert guard < flow.index("'get-access-token'"), "the token mint must sit behind the declaration guard"
    # Resolution runs only for a declaration that is NOT already a GUID, so the ordinary path pays
    # for one `az` call, not two.
    assert "if ($intendedTenant -and -not (Test-TenantIdShape $intendedTenant) -and $azPresent) {" in flow, (
        "domain resolution must be guarded on a declared, non-GUID intent"
    )
    # And its answer must be USED. The resolver itself is executed by
    # `test_a_declared_domain_is_resolved_rather_than_given_up_on`; this one line of wiring can only
    # be executed by running preflight, which probes npm, Desktop and the plugin cache and cannot run
    # in CI - and it is exactly where a correct resolution can still be thrown away, silently
    # restoring the "not compared" verdict this change removed.
    assert "if ($resolved) { $intendedTenant = $resolved }" in flow, (
        "a successful resolution must replace the declared spelling for comparison"
    )


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


def test_a_warning_only_mismatch_says_how_to_make_it_blocking() -> None:
    """A WARN that does not explain how to get the blocker is a downgrade, not a design.

    An operator who is about to deploy needs to know that the same mismatch fails preflight once the
    run declares `-Tenant`, or the recommended tier just reads as "we decided this matters less".
    """
    block = _tenant_block(_preflight_source())
    escalation = re.search(r"\$escalation = .*", block)
    assert escalation, "a warning-only mismatch must carry an escalation note"
    assert "-Tenant" in escalation.group(0), "the note must name the flag that makes it blocking"


def test_no_output_path_can_carry_the_token() -> None:
    """The token is a bearer credential: it must reach the decoder and the Fabric API, nothing else.

    Enumerating the allowed uses (rather than grepping `Write-Host` for it) is what makes this
    mutation-proof: any new line that touches the token - a debug print, a richer Detail string, an
    error message echoing `az` output - is a line this test has never seen and therefore fails.

    The token now also lives in `$fabricToken`, because the landing-zone check has to present it to
    Fabric. Watching the new name is the point of re-arming this: a filter that still looked only
    for `tokenJson`/`accessToken` would have stopped watching the variable that actually holds the
    credential - a silent disarm that passes.
    """
    block = _tenant_block(_preflight_source())
    allowed = (
        re.compile(r"^#"),  # commentary
        re.compile(r"^\$tokenJson = & az @azArgs 2>\$null$"),
        re.compile(r"^if \(\$tokenJson\) \{$"),
        # In-process only: az's already-captured stdout is parsed and bound to a variable. `Out-String`
        # feeds the pipeline into `ConvertFrom-Json`, not the host, and the assignment consumes it, so
        # this line has no output stream to escape into.
        re.compile(r"^\$fabricToken = \(\(\$tokenJson \| Out-String\) \| ConvertFrom-Json\)\.accessToken$"),
        re.compile(r"^\$actualTenant = Get-JwtTenantId \$fabricToken$"),  # the decoder, as before
        re.compile(r"^\$fabricToken = ''$"),  # declared/cleared: carries no value
        re.compile(r"^catch \{ \$actualTenant = ''; \$fabricToken = '' \}$"),  # cleared on a parse failure
        re.compile(r"^elseif \(-not \$fabricToken\) \{$"),  # truthiness only, never the value
        # The one OUTBOUND use, and it is what the token was minted for: the Authorization header of a
        # request to $fabricResource itself - the audience of the token - built into a local hashtable
        # that is passed only to that call, whose success body is discarded (`Out-Null`) and whose
        # failure path reads an HTTP status code and nothing else.
        re.compile(r"^\$workspaceHeaders\['Authorization'\] = 'Bearer ' \+ \$fabricToken$"),
    )
    carriers = ("tokenJson", "accessToken", "fabricToken")
    for line in block.splitlines():
        stripped = line.strip()
        if not any(name in stripped for name in carriers):
            continue
        assert any(pattern.match(stripped) for pattern in allowed), f"the token escapes into: {stripped}"


def test_the_closing_line_names_the_tenant_when_something_is_wrong_with_it() -> None:
    """A count is not a diagnosis, and the tenant line scrolls off before the verdict is read.

    Measured: the WRONG TENANT warning sat at line 22 of 38 output lines, with 16 `[OK]` lines after
    it; on an 80x24 terminal the only text still visible read "Ready to migrate. 3 recommended
    warning(s) present." Both exit paths therefore carry the tenant summary - the failing one too,
    because a run that is already blocking on something else must still say the tenant was wrong.
    """
    source = _preflight_source()
    summary_lines = [ln for ln in source.splitlines() if 'Write-Host "PREFLIGHT:' in ln]
    assert len(summary_lines) == 2, f"expected the two exit summaries, saw {summary_lines}"
    assert all("$tenantNote" in ln for ln in summary_lines), (
        f"every closing line must be able to name the tenant: {summary_lines}"
    )
    assert "$tenantNote = if ($verdict.Summary)" in source, "the note must come from the verdict, not be re-derived"


# Two things here cannot be established by reading the source:
#   * the base64url decode - a padding or charset bug fails CLOSED in the worst way, reporting "could
#     not decode" for real tokens so the check never fires, while every source contract still passes;
#   * the verdict itself - `$tenantOk = $true` and an inverted comparison both survived a 15-mutation
#     sweep, i.e. the one line that decides OK-vs-MISS was the least covered line in the file.
# So the decision lives in `Get-TenantVerdict`, and the harness below runs it for real.

_PS = shutil.which("pwsh") or shutil.which("powershell")

# Extract the functions under test from the script by AST and dot-source only those, so running these
# tests never executes preflight itself (which probes npm, Desktop and the plugin cache).
_EXTRACT_FUNCS = """
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:PREFLIGHT_PS1, [ref]$null, [ref]$null)
$defs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in ($env:PREFLIGHT_FUNCS -split ',')) {
    $fn = $defs | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { Write-Output "FUNCTION-NOT-FOUND:$name"; exit 3 }
    . ([scriptblock]::Create($fn.Extent.Text))
}
"""

# Values are emitted between markers so a stray leading/trailing space is a FAILURE rather than
# something the test quietly strips - the whole point of these cases is that whitespace and quoting
# must be normalized by the script, not by the harness.
_EMIT = "function Emit($v) { if ($null -eq $v) { Write-Output '<<NULL>>' } else { Write-Output ('<<' + $v + '>>') } }\n"

# A synthetic tenant id that deliberately contains hex LETTERS. An all-digit GUID makes every
# casing test vacuous - measured here: with `11111111-2222-...`, mutating the comparison to the
# case-SENSITIVE `-ceq` survived the whole suite, because `.upper()` of a digit string is itself.
_TID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
_OTHER_TID = "99999999-8888-7777-6666-555555555555"
_FIXTURE_ENV = REPO_ROOT / "tests" / "fixtures" / "dotenv-spellings.env"


def _run_ps(functions: str, body: str, extra_env: dict[str, str]) -> str:
    # -EncodedCommand rather than piping to `-Command -`: PowerShell 7 consumes stdin line by line,
    # which silently truncates a multi-line script block (measured: empty output, exit 0). Base64
    # UTF-16LE is also the one form that needs no shell quoting on either platform.
    script = _EXTRACT_FUNCS + _EMIT + body
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    result = subprocess.run(  # noqa: S603
        [_PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PREFLIGHT_PS1": str(PREFLIGHT), "PREFLIGHT_FUNCS": functions, **extra_env},
    )
    assert "FUNCTION-NOT-FOUND" not in result.stdout, f"renamed or removed: {result.stdout.strip()}"
    assert result.returncode == 0, f"harness failed: {result.stderr.strip()[:400]}"
    return result.stdout.strip()


def _emitted(raw: str) -> str:
    marked = re.search(r"<<(.*)>>", raw, re.DOTALL)
    assert marked, f"harness produced no marked value: {raw!r}"
    return marked.group(1)


def _verdict(
    intended: str,
    actual: str = _TID,
    *,
    explicit: bool = False,
    az_present: bool = True,
    declared_as: str = "",
) -> dict:
    raw = _run_ps(
        "Get-TenantVerdict,Remove-SurroundingQuotes,Test-TenantIdShape",
        "$v = Get-TenantVerdict -IntendedTenant $env:T_INTENDED -ActualTenant $env:T_ACTUAL "
        "-IntentIsExplicit ([bool]::Parse($env:T_EXPLICIT)) -AzPresent ([bool]::Parse($env:T_AZ)) -Scope '' "
        "-DeclaredAs $env:T_DECLARED\n"
        "Emit ($v | ConvertTo-Json -Compress)\n",
        {
            "T_INTENDED": intended,
            "T_ACTUAL": actual,
            "T_EXPLICIT": str(explicit),
            "T_AZ": str(az_present),
            "T_DECLARED": declared_as,
        },
    )
    return json.loads(_emitted(raw))


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
def test_a_matching_tenant_passes_and_a_mismatched_one_does_not() -> None:
    """The verdict line, executed in both directions.

    This is the test the mutation sweep was missing: with it, `$tenantOk = $true` (the check can
    never fire) fails on the mismatch case, and `$tenantOk = ($ActualTenant -ne $IntendedTenant)`
    (fires on every correct machine) fails on the match case. Neither was detectable before, because
    the verdict was asserted by matching the source string that contains it.
    """
    match = _verdict(_TID, _TID)
    assert (match["Kind"], match["Ok"], match["Tier"]) == ("match", True, "optional")
    assert _TID in match["Detail"]

    mismatch = _verdict(_OTHER_TID, _TID, explicit=True)
    assert (mismatch["Kind"], mismatch["Ok"]) == ("mismatch", False)
    assert "WRONG TENANT" in mismatch["Detail"]
    assert _OTHER_TID in mismatch["Detail"] and _TID in mismatch["Detail"], (
        "both tenants must be named, or the operator cannot tell which end is wrong"
    )


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    ("label", "declared"),
    [
        ("double-quoted, as dotenv convention teaches", f'"{_TID}"'),
        ("single-quoted", f"'{_TID}'"),
        ("padded with whitespace", f"  {_TID}  "),
        ("quoted AND padded", f'  "{_TID}"  '),
        ("uppercased, as the Entra portal shows it", _TID.upper()),
    ],
)
def test_a_correct_tenant_spelled_differently_is_still_correct(label: str, declared: str) -> None:
    """Be liberal in what you accept; be strict only about what you compare.

    Measured 2026-08-13 on the version this replaces: `FABRIC_TENANT_ID="72f988bf-..."` naming the
    CORRECT tenant produced `[MISS] WRONG TENANT ... intended "72f988bf-..."` and exit 1 on a fully
    configured machine - a hard blocker, in front of a customer, caused by a pair of quotes that
    every dotenv consumer strips. The casing case is here for the same reason: the portal and the
    `tid` claim disagree on it, and PowerShell's `-eq` is case-insensitive - a well-meaning "fix" to
    `-ceq` would reintroduce exactly this class of false blocker.
    """
    verdict = _verdict(declared, _TID)
    assert verdict["Ok"] is True, f"{label} must still match: {verdict['Detail']}"
    assert verdict["Tier"] == "optional"
    assert '"' not in verdict["Detail"] and "'" not in verdict["Detail"], (
        "the normalized id, not the raw spelling, belongs in the output"
    )


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
def test_the_verdict_tiers_follow_where_the_intent_came_from() -> None:
    """Blocking follows an in-run declaration; persisted configuration warns (#124 follow-up).

    `-Tenant <id>` says "this run points at that tenant" - a token for another one is a blocker.
    `FABRIC_TENANT_ID` in `.env` is a standing preference that outlives the deploy it was set for,
    and a parse-only estate run (steps 1-6 never call Fabric) must not exit 1 because of it.
    """
    assert _verdict(_OTHER_TID, _TID, explicit=True)["Tier"] == "critical"
    ambient = _verdict(_OTHER_TID, _TID, explicit=False)
    assert ambient["Tier"] == "recommended", "persisted configuration must warn, not block"
    assert re.search(r"(?i)warning only|-Tenant", ambient["Detail"]), (
        "a warning-only mismatch must say so, or it reads as an ordinary WARN"
    )


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    "declared",
    [
        "contoso.onmicrosoft.com",  # a real default domain, but for a tenant this machine cannot see
        "contoso.com",  # a VANITY domain - never the default domain, so never resolvable
        "<your-tenant-id>",  # copied out of a .env.example and never filled in
        "72f988bf86f141af91ab2d7cd011db47",  # a GUID with the hyphens lost
        "not-a-guid",
    ],
)
def test_an_uncomparable_tenant_id_never_blocks(declared: str) -> None:
    """A `tid` is always a GUID, so anything else is "cannot compare", never "wrong tenant".

    Comparing a domain name or a placeholder against a GUID answers WRONG TENANT and exits 1 - the
    same false blocker as the quoting bug, arriving by a different route. Note the caller RESOLVES a
    default domain before it gets here (`Resolve-TenantIdFromDomain`); what reaches this function is
    the residue that resolution could not turn into a GUID, and for that "cannot compare" is honest.
    """
    verdict = _verdict(declared, _TID)
    assert (verdict["Kind"], verdict["Ok"], verdict["Tier"]) == ("malformed", False, "optional")
    assert declared in verdict["Detail"], "name the value that could not be compared"


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    ("label", "kwargs", "expected_kind"),
    [
        ("nothing declared", {"intended": "", "actual": _TID}, "no-intent"),
        ("az not installed", {"intended": _TID, "actual": "", "az_present": False}, "no-az"),
        ("declared, az present, no token", {"intended": _TID, "actual": ""}, "no-token"),
        ("declared as an unresolvable name", {"intended": "contoso.com", "actual": _TID}, "malformed"),
    ],
)
def test_every_reason_the_check_could_not_run_is_advisory(label: str, kwargs: dict, expected_kind: str) -> None:
    """ "Could not run" is not evidence of a problem, and must never spend exit 1.

    Each still reports a line, though: a check that renders as nothing reads as a check that passed,
    which is the false-green shape this whole script exists to prevent.
    """
    verdict = _verdict(**kwargs)
    assert verdict["Kind"] == expected_kind, label
    assert verdict["Ok"] is False, f"{label}: an unrun check is not a pass"
    assert verdict["Tier"] != "critical", f"{label}: must not block"
    assert verdict["Detail"], f"{label}: must still say something"


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("unresolvable spelling", {"intended": "contoso.com", "actual": _TID}),
        ("az not installed", {"intended": _TID, "actual": "", "az_present": False}),
        ("no token could be minted", {"intended": _TID, "actual": ""}),
    ],
)
def test_a_declared_tenant_that_could_not_be_verified_is_not_filed_under_optional(label: str, kwargs: dict) -> None:
    """A check the operator ASKED FOR that did not run is a blank space, not a pass.

    Measured: `preflight.ps1 -Tenant contoso.onmicrosoft.com` printed its one line under
    `== OPTIONAL ==`, beneath a green "Ready to migrate" - i.e. an explicit, blocking-channel
    declaration of intent bought exactly zero verification and said so in the tier nobody reads.
    Provenance decides how loud that is, exactly as it does for a mismatch: `-Tenant` is a statement
    about THIS run, `.env` is a standing preference. The generalisation past the reviewed case
    (`malformed`) is deliberate - "az is missing" and "no token" leave a declared intent just as
    unverified as an unresolvable name does.
    """
    ambient = _verdict(**kwargs, explicit=False)
    explicit = _verdict(**kwargs, explicit=True)
    assert ambient["Tier"] == "optional", f"{label}: configured intent stays optional"
    assert explicit["Tier"] == "recommended", f"{label}: declared intent must be visible"
    assert explicit["Tier"] != "critical", f"{label}: could-not-verify is still not a blocker"
    assert "NOT VERIFIED" in explicit["Summary"], f"{label}: the closing line must say it was not verified"
    assert ambient["Summary"] == "", f"{label}: a standing preference must not shout on every run"


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    ("label", "kwargs", "expect_summary"),
    [
        ("a match says nothing", {"intended": _TID, "actual": _TID}, False),
        ("nothing declared says nothing", {"intended": "", "actual": _TID}, False),
        ("a mismatch always speaks", {"intended": _OTHER_TID, "actual": _TID}, True),
        ("an explicit mismatch speaks", {"intended": _OTHER_TID, "actual": _TID, "explicit": True}, True),
    ],
)
def test_the_summary_speaks_only_when_it_has_something_to_say(label: str, kwargs: dict, expect_summary: bool) -> None:
    """The closing line is the last thing an operator reads; it must stay quiet on the happy path.

    A mismatch names the tenant whatever its provenance - a WARN that scrolled off the top is the
    exact failure mode this exists for - while a match, and a run with nothing declared, add nothing.
    """
    verdict = _verdict(**kwargs)
    assert bool(verdict["Summary"]) is expect_summary, f"{label}: Summary={verdict['Summary']!r}"
    if expect_summary:
        assert _OTHER_TID in verdict["Summary"] and _TID in verdict["Summary"], (
            "both ends must be named, or the summary cannot be acted on without scrolling back"
        )


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
def test_a_resolved_domain_is_compared_but_still_reported_the_way_it_was_written() -> None:
    """The operator typed a domain; the verdict must be about the GUID yet legible to them.

    Without `DeclaredAs`, a resolved domain produces a message naming two GUIDs the operator never
    typed, and no way to connect either to what they wrote.
    """
    resolved = _verdict(_OTHER_TID, _TID, explicit=True, declared_as="contoso.onmicrosoft.com")
    assert resolved["Kind"] == "mismatch" and resolved["Tier"] == "critical"
    assert "declared as contoso.onmicrosoft.com" in resolved["Detail"]
    assert "declared as contoso.onmicrosoft.com" in resolved["Summary"]
    # When nothing was translated, the same string twice is noise.
    plain = _verdict(_TID, _TID, declared_as=_TID)
    assert "declared as" not in plain["Detail"], "only a RESOLVED spelling is worth repeating"


def _resolve_intent(flag: str = "", env: str = "", dotenv: str = "") -> dict:
    raw = _run_ps(
        "Resolve-IntendedTenant,Remove-SurroundingQuotes",
        "$i = Resolve-IntendedTenant $env:T_FLAG $env:T_ENV $env:T_DOTENV\nEmit ($i | ConvertTo-Json -Compress)\n",
        {"T_FLAG": flag, "T_ENV": env, "T_DOTENV": dotenv},
    )
    return json.loads(_emitted(raw))


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    ("label", "channels", "expected", "expected_explicit"),
    [
        ("the flag wins over both", {"flag": _TID, "env": _OTHER_TID, "dotenv": _OTHER_TID}, _TID, True),
        ("the exported variable beats .env", {"env": _TID, "dotenv": _OTHER_TID}, _TID, False),
        (".env is the last resort", {"dotenv": _TID}, _TID, False),
        ("nothing declared", {}, "", False),
        # Every channel is normalized, not just the one with a reader function of its own.
        ("a quoted flag", {"flag": f'"{_TID}"'}, _TID, True),
        ("a quoted exported variable", {"env": f'"{_TID}"'}, _TID, False),
        ("a padded flag", {"flag": f"  {_TID} "}, _TID, True),
        # An empty higher channel must not shadow a populated lower one, and must not claim intent.
        ("an empty flag falls through", {"flag": "", "env": _TID}, _TID, False),
        ("a whitespace-only flag falls through", {"flag": "   ", "env": _TID}, _TID, False),
    ],
)
def test_the_three_channels_resolve_in_order_and_are_all_normalized(
    label: str, channels: dict, expected: str, expected_explicit: bool
) -> None:
    """Precedence AND normalization, executed - the pipeline they share was previously only grepped.

    Two mutations survived a sweep here: dropping the normalization step, and inverting the
    precedence. The first is not cosmetic - a quoted `$env:FABRIC_TENANT_ID` then fails the GUID
    guard and degrades to "no Fabric token could be minted", the quoting bug re-entering through the
    one channel `Get-DotEnvValue` does not cover. The second silently makes a persisted `.env` beat
    an in-run `-Tenant`, which also flips the tier from critical to recommended.
    """
    intent = _resolve_intent(**channels)
    assert intent["Tenant"] == expected, label
    assert intent["IsExplicit"] is expected_explicit, f"{label}: only -Tenant declares intent for this run"


def _resolve_domain(domain: str, az_stdout: str) -> str:
    # `az` is shadowed by a FUNCTION here: PowerShell resolves functions before executables, so the
    # resolver runs against canned JSON with no CLI, no network and no credentials involved.
    raw = _run_ps(
        "Resolve-TenantIdFromDomain,Test-TenantIdShape",
        "function az { $env:T_AZOUT }\n$r = Resolve-TenantIdFromDomain $env:T_DOMAIN\nEmit $r\n",
        {"T_DOMAIN": domain, "T_AZOUT": az_stdout},
    )
    return _emitted(raw)


_AZ_ACCOUNTS = json.dumps(
    [
        {"id": "sub-1", "tenantId": _OTHER_TID, "tenantDefaultDomain": "other.onmicrosoft.com"},
        {"id": "sub-2", "tenantId": _TID, "tenantDefaultDomain": "contoso.onmicrosoft.com"},
    ]
)


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    ("label", "domain", "az_stdout", "expected"),
    [
        ("the default domain resolves to its tenant", "contoso.onmicrosoft.com", _AZ_ACCOUNTS, _TID),
        ("DNS names are case-insensitive", "CONTOSO.OnMicrosoft.COM", _AZ_ACCOUNTS, _TID),
        ("the right row is chosen, not the first", "other.onmicrosoft.com", _AZ_ACCOUNTS, _OTHER_TID),
        ("a vanity domain is not the default domain", "contoso.com", _AZ_ACCOUNTS, ""),
        ("a tenant never signed in to", "fabrikam.onmicrosoft.com", _AZ_ACCOUNTS, ""),
        ("az said nothing (not logged in)", "contoso.onmicrosoft.com", "", ""),
        ("az said something that is not JSON", "contoso.onmicrosoft.com", "ERROR: please run az login", ""),
        ("no domain to resolve", "", _AZ_ACCOUNTS, ""),
    ],
)
def test_a_declared_domain_is_resolved_rather_than_given_up_on(
    label: str, domain: str, az_stdout: str, expected: str
) -> None:
    """ "Cannot compare" was a choice, not a necessity: the CLI already holds the mapping.

    `contoso.onmicrosoft.com` is accepted by `az --tenant` and by `deploy_estate.py --tenant`, so an
    operator has every reason to use it here too - and before this, that spelling bought zero
    verification. `az account list --all` returns `tenantDefaultDomain` beside `tenantId`, so the
    honest answer is available for the asking.

    The failure rows matter just as much: resolution is best-effort, and every way it can fail must
    return "" (fall back to "cannot compare") rather than throw or guess. A vanity domain is the
    common one - it is not the DEFAULT domain and will never appear in that mapping.
    """
    assert _resolve_domain(domain, az_stdout) == expected, label


def _dotenv(key: str) -> str:
    raw = _run_ps(
        "Get-DotEnvValue,ConvertFrom-DotEnvValue,Remove-SurroundingQuotes",
        "Emit (Get-DotEnvValue $env:T_KEY $env:T_PATH)\n",
        {"T_KEY": key, "T_PATH": str(_FIXTURE_ENV)},
    )
    return _emitted(raw)


# The spellings `.env` promises to accept, and the ONE table both readers of that file are pinned to
# (see the two tests below). Written down once on purpose: when the PowerShell and Python readers each
# carried their own idea of a `.env` value, they diverged silently and preflight blessed an id the
# deployer then mangled.
_DOTENV_SPELLINGS = [
    ("PLAIN", _TID),
    ("DOUBLE_QUOTED", _TID),
    ("SINGLE_QUOTED", _TID),
    ("PADDED", _TID),
    ("QUOTED_AND_PADDED", _TID),
    # An inline note next to a GUID nobody recognizes by sight is the natural thing to write.
    ("COMMENTED", _TID),
    ("COMMENTED_TIGHT", _TID),
    ("QUOTED_AND_COMMENTED", _TID),
    # ...but inside quotes a '#' is DATA, and one with no whitespace in front of it is part of
    # the value. Over-stripping corrupts a declaration just as quietly as not stripping at all.
    ("HASH_INSIDE_QUOTES", "ab #cd"),
    ("HASH_IS_DATA", "abc#def"),
    # The closer is the first quote that actually ENDS the value: here the `#` right after it
    # opens a comment, so the value stops at `ab`...
    ("HASH_AFTER_INNER_QUOTE", "ab"),
    # ...whereas here the first inner quote is followed by more value, so the scan keeps going
    # and the '#' it passed over stays data.
    ("HASH_STILL_INSIDE", 'a #b" c'),
    ("ONLY_A_COMMENT", ""),
    # Only a MATCHED outer pair is a quoting convention. Everything else is the value.
    ("UNMATCHED_QUOTE", f'"{_TID}'),
    ("INNER_QUOTES", 'ab"cd"ef'),
    ("HAS_EQUALS", "a=b=c"),
    # The closing quote must END the value. These two stay visibly malformed rather than
    # decoding to '' or to a truncated fragment - a silent downgrade to "nothing was declared"
    # would be worse than the complaint.
    ("DOUBLE_QUOTED_TWICE", f'"{_TID}"'),
    ("QUOTE_THEN_TAIL", 'a"b'),
    # Present-but-empty and absent are both "no value" to every caller, which filters falsy;
    # the PowerShell reader still distinguishes them ('' vs $null) rather than inventing one.
    ("EMPTY", ""),
    ("ABSENT_FROM_THE_FILE", "NULL"),
]


@pytest.mark.skipif(_PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(("key", "expected"), _DOTENV_SPELLINGS)
def test_the_dotenv_reader_accepts_every_ordinary_spelling(key: str, expected: str) -> None:
    """`.env` is the documented home for a customer tenant id, so its parse decides the verdict.

    This is where the false blocker actually lived: the reader mirrored
    `scripts/tableau_env.py:load_env` exactly - `value.strip()`, no quote handling - and the comment
    saying so was the argument for keeping it. The two parsers agreed by being wrong in the same way.
    `load_env` is owned elsewhere and still reads values that way; nothing asks it for a Fabric key,
    so that divergence stays inert. The Python reader that is NOT inert is
    `deploy_estate.py:_dotenv_value`, pinned to this same table by the test below.
    """
    assert _dotenv(key) == expected


@pytest.mark.parametrize(("key", "expected"), _DOTENV_SPELLINGS)
def test_the_python_reader_of_the_same_file_agrees_with_preflight(key: str, expected: str) -> None:
    """One `.env`, two readers: preflight VERIFIES the workspace id, `deploy_estate.py` USES it.

    The divergence stopped being inert the moment Python started reading a Fabric key. Measured on
    `FABRIC_WORKSPACE_ID=<guid>\\t# customer landing zone`, preflight reported the workspace
    reachable while the deployer kept the comment and failed with the late `WorkspaceNotFound` that
    the preflight check exists to prevent - two of our own tools contradicting each other, using a
    value one of them had just blessed.

    Pinning both readers to ONE table is what keeps that closed: a spelling either works in both or
    fails in both, and neither can drift alone. This half runs everywhere, PowerShell or not.

    `$null` (absent) and `''` (present but empty) collapse to `""` in Python. Every caller filters on
    falsiness, so nothing is lost - the deployer asks "is a workspace declared", not "which way was
    it left out".
    """
    assert de.dotenv_value(key, _FIXTURE_ENV) == ("" if expected == "NULL" else expected)


# --- The base64url decode --------------------------------------------------------------------------

# Its own id, unrelated to the verdict tests: what matters here is only that whatever went into the
# payload comes back out byte-for-byte.
_TOKEN_TID = "11111111-2222-3333-4444-555555555555"


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
        payload = {"tid": _TOKEN_TID, "pad": "x" * size}
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
            payload = {"tid": _TOKEN_TID, "pad": filler * size}
            segment = _jwt(payload).split(".")[1]
            if "-" in segment and "_" in segment:
                return payload
    raise AssertionError("no payload found that exercises the base64url alphabet")


def _decode(token: str) -> str:
    return _emitted(
        _run_ps(
            "Get-JwtTenantId", "Emit (Get-JwtTenantId $env:PREFLIGHT_TEST_TOKEN)\n", {"PREFLIGHT_TEST_TOKEN": token}
        )
    )


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
    assert _decode(_jwt(payload)) == _TOKEN_TID, label


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
