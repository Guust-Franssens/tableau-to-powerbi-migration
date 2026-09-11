"""Exercise the published reactive signing recovery, not an automatic bootstrap (#610).

Only generated files and child-process policies are used. Group Policy, AppLocker and WDAC are
not changed or emulated; managed-policy routing is a documentation contract.
Run: pytest -q tests/test_preflight_signing_policy.py --basetemp _preflight_signing_tests
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path

import pytest

import test_openability_claim_citations as persona_pins

ROOT = Path(__file__).resolve().parents[1]
ORIGINS = {
    "AGENTS.md": ("-Update", "-CheckUpstream"),
    ".github/copilot-instructions.md": ("-Update", "-CheckUpstream"),
    "README.md": (),
    "docs/operator-runbook.md": ("-Update", "-CheckUpstream"),
    "docs/start-with-one-workbook.md": ("-Update",),
    "scripts/README.md": ("-Update", "-CheckUpstream"),
    ".github/agents/tableau-migrator.agent.md": (),
    ".github/agents/dry-run-operator.agent.md": (),
}
PERSONAS = ("tableau-migrator.agent.md", "dry-run-operator.agent.md")
ENTRY = re.compile(r"powershell -ExecutionPolicy Bypass -File scripts[\\/]preflight\.ps1[^\r\n`]*")
ROUTE = re.compile(
    r"\[preflight cannot start\]\((?:[^)]*/operator-runbook\.md|operator-runbook\.md)?#preflight-cannot-start\)"
)
REFUSAL = "only after an actual unsigned/executionpolicy startup refusal"
SKIP_REASONS = {
    "platform": "requires Windows PowerShell/PowerShell 7 on Windows and NTFS",
    "powershell": "powershell is not installed",
    "pwsh": "pwsh is not installed",
    "managed": "Group Policy is configured; process-only controls must not override or change it",
}
CONTROL = """
param([switch]$Update, [switch]$CheckUpstream, [string]$Tenant, [string]$Subscription, [int]$Code = 0)
Set-Content -LiteralPath (Join-Path $PSScriptRoot 'started.txt') -Value 'CONTROL_STARTED'
[ordered]@{ Update = [bool]$Update; CheckUpstream = [bool]$CheckUpstream; Tenant = $Tenant;
   Subscription = $Subscription } | ConvertTo-Json -Compress
[Console]::Error.WriteLine('CONTROL_STDERR')
exit $Code
"""


def _normalized(text: str) -> str:
    return " ".join(text.replace("**", "").split())


def _runbook_recovery() -> str:
    text = (ROOT / "docs" / "operator-runbook.md").read_text(encoding="utf-8")
    return text.split("#### Preflight cannot start\n", 1)[1].split("\n### 1.2", 1)[0]


def _published_commands() -> list[str]:
    commands = re.findall(r"```powershell\n(.*?)\n```", _runbook_recovery(), re.S)
    assert len(commands) == 3, "expected policy, file/ADS diagnostic, and optional trusted-file unblock"
    return commands


def _route_row(verdict: str) -> str:
    return next(line for line in _runbook_recovery().splitlines() if f"| **{verdict}." in line)


def _assert_entry_route(text: str, arguments: tuple[str, ...]) -> str:
    entry = ENTRY.search(text)
    route = ROUTE.search(text)
    assert entry is not None, "direct preflight invocation missing"
    assert route is not None and entry.end() < route.start(), "fallback must follow the direct invocation"
    expected = " ".join(("powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1", *arguments))
    assert entry.group().strip().replace("\\", "/") == expected, "originating command arguments changed"
    recovery = _normalized(text[entry.end() : route.end() + 400]).lower()
    assert REFUSAL in recovery, "recovery must require an actual startup refusal"
    assert "exact originating command and arguments" in recovery, "recovery lost its originating command"
    assert not re.search(r"Get-ExecutionPolicy|Zone\.Identifier|Before any PS1", text[: entry.start()], re.I)
    return entry.group().strip()


@pytest.mark.parametrize("document", ORIGINS)
def test_direct_preflight_precedes_reactive_recovery(document: str) -> None:
    """Every entry keeps its original first command, including independently invoked personas."""
    _assert_entry_route((ROOT / document).read_text(encoding="utf-8"), ORIGINS[document])


@pytest.mark.parametrize("document", ORIGINS)
def test_moving_recovery_before_preflight_fails_the_order_assertion(document: str) -> None:
    """A policy-listing gate must not replace the first action again."""
    text = (ROOT / document).read_text(encoding="utf-8")
    _assert_entry_route(text, ORIGINS[document])
    link = ROUTE.search(text).group()
    with pytest.raises(AssertionError, match="fallback must follow the direct invocation"):
        _assert_entry_route(link + "\n" + ROUTE.sub("preflight cannot start", text), ORIGINS[document])


@pytest.mark.parametrize("persona", PERSONAS)
def test_migration_update_substitution_fails_the_origin_assertion(persona: str) -> None:
    """Following the fallback must not change a migration call into session-start repair."""
    text = (ROOT / ".github" / "agents" / persona).read_text(encoding="utf-8")
    command = _assert_entry_route(text, ())
    with pytest.raises(AssertionError, match="originating command arguments changed"):
        _assert_entry_route(text.replace(command, command + " -Update -CheckUpstream", 1), ())


@pytest.mark.parametrize("persona", PERSONAS)
def test_existing_persona_pin_detects_a_recovery_instruction_mutation(persona: str) -> None:
    """Both changed personas must be pinned, and each changed instruction must fail that same pin."""
    text = persona_pins.read_source(ROOT / ".github" / "agents" / persona)
    blocks = persona_pins.segment(text, collapse_generated=True)
    assert not persona_pins.findings(persona, blocks), "committed persona pin is stale"
    changed = text.replace("exact originating command and arguments", "session-start update command", 1)
    assert changed != text, "the intended recovery instruction was not mutated"
    found = persona_pins.findings(persona, persona_pins.segment(changed, collapse_generated=True))
    assert {finding.kind for finding in found} == {"added", "removed"}, "persona pin missed the instruction edit"


def test_managed_remotesigned_without_refusal_uses_the_original_command() -> None:
    """A managed permissive policy is not itself a refusal; no Group Policy simulation is claimed."""
    row = _normalized(_route_row("USE_ORIGINAL_COMMAND"))
    for fact in ("No startup refusal", "managed RemoteSigned", "MOTW_ABSENT", "MachinePolicy/UserPolicy"):
        assert fact in row
    assert "use the exact originating command and arguments" in row
    assert "not a reason to stop or diagnose" in row and "STOP" not in row


def test_managed_signing_refusal_and_unknown_blocks_remain_distinct() -> None:
    """Actual managed AllSigned/Restricted refusals stop; an unknown block is not a diagnosis."""
    row = _normalized(_route_row("STOP — MANAGED_SIGNING_POLICY"))
    for fact in ("Actual startup refusal", "MachinePolicy/UserPolicy", "AllSigned", "Restricted", "No retry"):
        assert fact in row
    assert "IT action / an approved signed distribution" in row
    assert "not merely a signature" in row
    unknown = _normalized(_runbook_recovery())
    assert "MOTW alone cannot explain this failure" in unknown and "Process Bypass" in unknown
    assert "PowerShell is unavailable" in unknown and "AppLocker/WDAC" in unknown
    assert "not an AppLocker/WDAC diagnosis" in unknown and "CANNOT_ESTABLISH" in unknown


def test_motw_recovery_requires_refusal_source_review_and_narrow_unblock() -> None:
    """No policy weakening, automatic unblock, or alternate script-content execution."""
    route = _runbook_recovery()
    row = _normalized(_route_row("MOTW_REMOTESIGNED"))
    for fact in (
        "Actual unsigned startup refusal",
        "effective policy was RemoteSigned",
        "FILE_READABLE",
        "ZoneId=3 or ZoneId=4",
        "Verify the repository source and review the file first",
        "Prefer a fresh `git clone`",
        "Never auto-unblock",
    ):
        assert fact in row
    assert "MachinePolicy → UserPolicy → Process → CurrentUser → LocalMachine" in route
    assert "Only for the MOTW_REMOTESIGNED row" in route
    assert route.count("Unblock-File -LiteralPath '.\\scripts\\preflight.ps1'") == 1
    assert not re.search(r"Set-ExecutionPolicy|Invoke-Expression|\biex\b|-EncodedCommand|ScriptBlock\s*::", route, re.I)
    assert "Automatic bootstrap remains unsupported (#610)" in route
    assert not (ROOT / "scripts" / "preflight.cmd").exists()


def test_shared_fallback_returns_to_the_exact_origin_not_a_command_above() -> None:
    """The shared page must preserve phase-specific flags as well as user-supplied arguments."""
    route = _normalized(_runbook_recovery())
    assert "retry the exact originating command and arguments once" in route
    assert "command above" not in route.lower()
    assert "retain `-Update -CheckUpstream`" in route
    assert "return to plain preflight, with neither update flag" in route
    assert "retain its `-Update`" in route
    assert "`-Tenant`, `-Subscription` or other arguments too" in route


def _native_env() -> dict[str, str]:
    # Python does not perform pwsh's Windows PowerShell module-path adaptation for a native child.
    return {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}


def _ps(shell: str, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", *arguments],
        cwd=cwd,
        env=_native_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.fixture(name="shell", scope="module", params=("powershell", "pwsh"))
def installed_shell(request: pytest.FixtureRequest) -> str:
    """Run on both installed Windows engines; other platforms cannot exercise NTFS or policy."""
    if os.name != "nt":
        pytest.skip(SKIP_REASONS["platform"])
    executable = shutil.which(request.param)
    if executable is None:
        pytest.skip(SKIP_REASONS[request.param])
    return executable


def _policies(shell: str) -> list[dict[str, str]]:
    result = _ps(
        shell,
        "-Command",
        "Get-ExecutionPolicy -List | ForEach-Object { "
        "[ordered]@{Scope = [string]$_.Scope; Policy = [string]$_.ExecutionPolicy} } | ConvertTo-Json -Compress",
    )
    assert result.returncode == 0, "cannot establish policy for child-process controls"
    return json.loads(result.stdout)


@pytest.fixture(name="process_shell", scope="module")
def unmanaged_process_shell(shell: str) -> Iterator[str]:
    """Never change an enforced policy to make a local control possible."""
    before = _policies(shell)
    if any(row["Scope"] in ("MachinePolicy", "UserPolicy") and row["Policy"] != "Undefined" for row in before):
        pytest.skip(SKIP_REASONS["managed"])
    yield shell
    assert _policies(shell) == before, "child-process controls changed the host policy snapshot"


@pytest.fixture(name="control")
def script_control(tmp_path: Path) -> Path:
    """A generated checkout with spaces; production scripts and their streams stay untouched."""
    script = tmp_path / "repo with spaces" / "scripts" / "preflight.ps1"
    script.parent.mkdir(parents=True)
    script.write_text(CONTROL, encoding="utf-8")
    return script


def _published(
    shell: str, index: int, control: Path, *, policy: str | None = None, default_child: bool = False
) -> subprocess.CompletedProcess[str]:
    command = _published_commands()[index]
    if not default_child:
        command = command.replace("powershell ", Path(shell).stem + " ", 1)
    arguments = ("-ExecutionPolicy", policy) if policy is not None else ()
    return _ps(shell, *arguments, "-Command", command + "\nexit $LASTEXITCODE", cwd=control.parent.parent)


@pytest.mark.parametrize("default_child", (False, True), ids=("originating-engine", "published-powershell"))
def test_exact_diagnostic_absent_ads_is_success(shell: str, control: Path, default_child: bool) -> None:
    """Run the exact code, including the documented pwsh substitution and both caller shells."""
    assert control.is_file() and not Path(str(control) + ":Zone.Identifier").exists()
    result = _published(shell, 1, control, default_child=default_child)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["FILE_READABLE", "MOTW_ABSENT"]
    assert not result.stderr and not control.with_name("started.txt").exists()


@pytest.mark.parametrize("target", ("missing", "directory"))
def test_exact_diagnostic_missing_or_nonfile_is_not_absent_ads(shell: str, control: Path, target: str) -> None:
    """A wrong checkout cannot be mislabeled a clean, unmarked script."""
    control.unlink()
    if target == "directory":
        control.mkdir()
    result = _published(shell, 1, control)
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["CANNOT_ESTABLISH_FILE"]
    assert not result.stderr


def test_exact_diagnostic_unreadable_existing_file_is_not_absent_ads(shell: str, control: Path) -> None:
    """A real exclusive Windows handle denies reads without changing any file ACL or policy."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(control), 0x80000000, 0, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value, f"exclusive control handle failed: {ctypes.get_last_error()}"
    try:
        assert control.is_file()
        with pytest.raises(PermissionError):
            control.read_bytes()
        result = _published(shell, 1, control)
    finally:
        assert kernel.CloseHandle(handle)
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["CANNOT_ESTABLISH_FILE"]
    assert not result.stderr


@pytest.mark.parametrize(
    ("policy", "marked", "expected"),
    [
        ("RemoteSigned", False, 23),
        ("RemoteSigned", True, 1),
        ("Bypass", True, 23),
        ("AllSigned", False, 1),
        ("Restricted", False, 1),
    ],
)
def test_actual_process_refusal_and_clean_remotesigned_control(
    process_shell: str, control: Path, policy: str, marked: bool, expected: int
) -> None:
    """Real effective policies, not Group Policy emulation; a separate file witnesses execution."""
    stream = Path(str(control) + ":Zone.Identifier")
    if marked:
        stream.write_text("[ZoneTransfer]\nZoneId=3\n", encoding="ascii")
    result = _ps(process_shell, "-ExecutionPolicy", policy, "-File", str(control), "-Code", "23")
    assert result.returncode == expected
    assert control.with_name("started.txt").exists() is (expected == 23)
    if expected == 1:
        # The known policy and independent start witness are the oracle, not rendered error prose.
        assert not result.stdout and result.stderr
    if marked:
        assert stream.read_text(encoding="ascii") == "[ZoneTransfer]\nZoneId=3\n", "control auto-unblocked the file"


def test_exact_policy_diagnostic_displays_scopes_on_a_clean_host(shell: str, control: Path) -> None:
    """Flush the policy table before child exit; a successful but blank listing is not evidence."""
    result = _published(shell, 0, control)
    assert result.returncode == 0
    assert "MachinePolicy" in result.stdout and "UserPolicy" in result.stdout
    assert "CurrentUser" in result.stdout and "LocalMachine" in result.stdout
    assert not result.stderr


def test_policy_diagnostic_reports_native_allsigned_availability(process_shell: str, control: Path) -> None:
    """An interpreter's own module can be refused too: report cannot-establish, never bypass it."""
    native = _ps(
        process_shell,
        "-ExecutionPolicy",
        "AllSigned",
        "-Command",
        "Get-ExecutionPolicy -List -ErrorAction Stop | Out-String",
    )
    result = _published(process_shell, 0, control, policy="AllSigned")
    if native.returncode == 0:
        assert result.returncode == 0
        assert "MachinePolicy" in result.stdout and "AllSigned" in result.stdout
    else:
        assert native.stderr, "the independent native command failed without diagnostic evidence"
        assert result.returncode == 1
        assert result.stdout.splitlines() == ["CANNOT_ESTABLISH_POLICY"]
    assert not result.stderr


def test_actual_allsigned_refusal_still_allows_exact_file_diagnostic(process_shell: str, control: Path) -> None:
    """Built-in inspection can run after an actual unsigned-file refusal, without running that file."""
    stream = Path(str(control) + ":Zone.Identifier")
    content = "[ZoneTransfer]\nZoneId=3\nHostUrl=CONTROL_SECRET_URL\n"
    stream.write_text(content, encoding="ascii")
    blocked = _ps(process_shell, "-ExecutionPolicy", "AllSigned", "-File", str(control))
    assert blocked.returncode == 1 and not blocked.stdout and blocked.stderr
    assert not control.with_name("started.txt").exists()
    result = _published(process_shell, 1, control, policy="AllSigned")
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["FILE_READABLE", "MOTW_PRESENT", "ZoneId=3"]
    assert not result.stderr and "CONTROL_SECRET_URL" not in result.stdout
    assert str(control) not in result.stdout and not control.with_name("started.txt").exists()
    assert stream.read_text(encoding="ascii") == content


def test_explicit_trusted_file_unblock_after_actual_motw_refusal(process_shell: str, control: Path) -> None:
    """The optional command touches only the reviewed test file, after the expected refusal."""
    stream = Path(str(control) + ":Zone.Identifier")
    stream.write_text("[ZoneTransfer]\nZoneId=3\n", encoding="ascii")
    other = control.with_name("other.ps1")
    other.write_text(CONTROL, encoding="utf-8")
    other_stream = Path(str(other) + ":Zone.Identifier")
    other_stream.write_text("[ZoneTransfer]\nZoneId=3\n", encoding="ascii")
    arguments = ("-ExecutionPolicy", "RemoteSigned", "-File", str(control), "-Code", "23")
    blocked = _ps(process_shell, *arguments)
    assert blocked.returncode == 1 and not control.with_name("started.txt").exists()
    unblocked = _published(process_shell, 2, control)
    assert unblocked.returncode == 0 and not unblocked.stderr
    assert not stream.exists() and other_stream.exists()
    resumed = _ps(process_shell, *arguments)
    assert resumed.returncode == 23 and control.with_name("started.txt").exists()


@pytest.mark.parametrize("document", ORIGINS)
@pytest.mark.parametrize("code", (0, 1))
def test_originating_commands_keep_arguments_outputs_and_exit_codes(
    process_shell: str, control: Path, document: str, code: int
) -> None:
    """Execute each published direct command; an in-script dependency failure is not startup refusal."""
    command = _assert_entry_route((ROOT / document).read_text(encoding="utf-8"), ORIGINS[document])
    command += f' -Tenant "tenant with spaces" -Subscription "sub with spaces" -Code {code}'
    result = _ps(process_shell, "-Command", command + "\nexit $LASTEXITCODE", cwd=control.parent.parent)
    assert result.returncode == code
    assert control.with_name("started.txt").read_text(encoding="utf-8").strip() == "CONTROL_STARTED"
    assert result.stderr.strip() == "CONTROL_STDERR"
    assert json.loads(result.stdout) == {
        "Update": "-Update" in ORIGINS[document],
        "CheckUpstream": "-CheckUpstream" in ORIGINS[document],
        "Tenant": "tenant with spaces",
        "Subscription": "sub with spaces",
    }
