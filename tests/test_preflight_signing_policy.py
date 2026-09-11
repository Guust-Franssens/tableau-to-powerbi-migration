"""Guard the manual signing bootstrap and exercise process-only Windows controls (#610).

No persistent policy or Group Policy is changed. The candidate CMD exists only in a test checkout:
it is evidence for the bounded fallback, not a second production entrypoint or a policy override.
Run: pytest -q tests/test_preflight_signing_policy.py --basetemp _preflight_signing_tests
"""

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    "AGENTS.md",
    ".github/copilot-instructions.md",
    "README.md",
    "docs/operator-runbook.md",
    "docs/start-with-one-workbook.md",
    "scripts/README.md",
    ".github/agents/tableau-migrator.agent.md",
    ".github/agents/dry-run-operator.agent.md",
)
ENTRY = re.compile(r"powershell -ExecutionPolicy Bypass -File scripts[\\/]preflight\.ps1")
ROUTE = re.compile(
    r"\[preflight cannot start\]\((?:[^)]*/operator-runbook\.md|operator-runbook\.md)?#preflight-cannot-start\)"
)
PS = shutil.which("powershell.exe") if os.name == "nt" else None
WINDOWS = pytest.mark.skipif(PS is None, reason="requires Windows PowerShell and NTFS alternate streams")
CMD_CANDIDATE = (
    "@echo off\n"
    "setlocal DisableDelayedExpansion\n"
    'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0preflight.ps1" %*\n'
    "exit /b %errorlevel%\n"
)
CONTROL = """
param([switch]$Update, [switch]$CheckUpstream, [string]$Tenant, [string]$Subscription, [int]$Code = 0)
Write-Output 'CONTROL_STARTED'
[ordered]@{ Update = [bool]$Update; CheckUpstream = [bool]$CheckUpstream; Tenant = $Tenant;
   Subscription = $Subscription } | ConvertTo-Json -Compress
[Console]::Error.WriteLine('CONTROL_STDERR')
exit $Code
"""


def _assert_entry_route(text: str) -> None:
    entry = ENTRY.search(text)
    route = ROUTE.search(text)
    assert entry is not None, "direct/internal entrypoint missing"
    assert route is not None and route.start() < entry.start(), "bootstrap route missing before first PS1"
    prefix = " ".join(text[: entry.start()].split()).lower()
    for fact in ("policy precedence", "zone.identifier", "allsigned", "stop for it", "cannot_establish"):
        assert fact in prefix, f"bootstrap fact missing: {fact}"


@pytest.mark.parametrize("document", DOCS)
def test_each_entry_requires_the_same_bootstrap_first(document: str) -> None:
    """One canonical route, before the command, including independently invoked personas."""
    _assert_entry_route((ROOT / document).read_text(encoding="utf-8"))


@pytest.mark.parametrize("document", DOCS)
def test_removing_the_bootstrap_link_fails_its_intended_assertion(document: str) -> None:
    """A shorter entry doc must not silently delete the signing-policy gate."""
    text = (ROOT / document).read_text(encoding="utf-8")
    _assert_entry_route(text)
    with pytest.raises(AssertionError, match="bootstrap route missing before first PS1"):
        _assert_entry_route(ROUTE.sub("preflight cannot start", text))


def test_manual_routes_preserve_policy_precedence_and_narrow_remediation() -> None:
    """The allowed and refused rows must remain distinct; unknown is never proof of MOTW."""
    text = (ROOT / "docs" / "operator-runbook.md").read_text(encoding="utf-8")
    route = text.split("#### Preflight cannot start\n", 1)[1].split("\n### 1.2", 1)[0]
    assert "MachinePolicy → UserPolicy → Process → CurrentUser → LocalMachine" in route
    assert 'powershell -NoProfile -Command "Get-ExecutionPolicy -List"' in route
    assert "Get-Item -LiteralPath '.\\scripts\\preflight.ps1' -Stream Zone.Identifier" in route
    assert "Select-Object Stream" in route
    assert "Select-String -Pattern '^ZoneId=[34]$' | Select-Object -ExpandProperty Line" in route
    managed = next(line for line in route.splitlines() if "| **STOP — MANAGED_ALLSIGNED." in line)
    assert "approved signed distribution/path or IT action" in managed
    assert "Unblock-File" not in managed and "No retry" in managed
    motw = next(line for line in route.splitlines() if "| **MOTW_REMOTESIGNED." in line)
    for fact in ("Both policy scopes are Undefined", "effective policy was RemoteSigned", "ZoneId=3 or ZoneId=4"):
        assert fact in motw
    assert "Verify the repository source" in motw and "Prefer a fresh `git clone`" in motw
    assert "MOTW alone cannot explain this failure" in route and "AppLocker/WDAC" in route
    assert "PowerShell is unavailable" in route and "does not claim an automatic run-or-diagnose entrypoint" in route
    assert route.count("Unblock-File -LiteralPath '.\\scripts\\preflight.ps1'") == 1
    assert "Only for the MOTW_REMOTESIGNED row" in route
    assert not re.search(r"Set-ExecutionPolicy|Invoke-Expression|\biex\b|-EncodedCommand|ScriptBlock\s*::", route, re.I)


def _native_env() -> dict[str, str]:
    # pwsh's native powershell.exe adapter removes its incompatible module path. Python/cmd do not.
    # Isolate that host difference in the test child, never in a production policy/dispatch hook.
    return {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}


def _ps(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PS, "-NoProfile", "-NonInteractive", *arguments],
        env=_native_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _policies() -> list[dict]:
    result = _ps("-Command", "Get-ExecutionPolicy -List | ConvertTo-Json -Compress")
    assert result.returncode == 0, "cannot establish Windows PowerShell policy for these controls"
    return json.loads(result.stdout)


@pytest.fixture(name="control")
def script_control(tmp_path: Path) -> Iterator[Path]:
    """Use only generated scripts; never change or remove a marker on repository files."""
    before = _policies()
    # The numeric scopes/policies come from Windows PowerShell's serialized enums:
    # MachinePolicy=4, UserPolicy=3, Undefined=5. An enforced policy is not a test setup lever.
    if any(row["Scope"] in (3, 4) and row["ExecutionPolicy"] != 5 for row in before):
        pytest.skip("Group Policy is configured; these controls must not override or modify it")
    script = tmp_path / "preflight.ps1"
    script.write_text(CONTROL, encoding="utf-8")
    yield script
    assert _policies() == before, "process-only controls changed the policy snapshot"


@WINDOWS
@pytest.mark.parametrize(
    ("policy", "marked", "expected"),
    [("RemoteSigned", False, 23), ("RemoteSigned", True, 1), ("Bypass", True, 23), ("AllSigned", False, 1)],
)
def test_real_process_policies_and_download_marker(control: Path, policy: str, marked: bool, expected: int) -> None:
    """Real engine controls: MOTW matters to RemoteSigned, not a winning process Bypass."""
    stream = Path(str(control) + ":Zone.Identifier")
    if marked:
        stream.write_text("[ZoneTransfer]\nZoneId=3\n", encoding="ascii")
    result = _ps("-ExecutionPolicy", policy, "-File", str(control), "-Code", "23")
    assert result.returncode == expected
    assert ("CONTROL_STARTED" in result.stdout) is (expected == 23)
    if expected == 1:
        assert "UnauthorizedAccess" in result.stderr and "SecurityError" in result.stderr
    if marked:
        assert stream.read_text(encoding="ascii") == "[ZoneTransfer]\nZoneId=3\n", "a control auto-unblocked the file"


@WINDOWS
@pytest.mark.parametrize("spaced", [False, True])
@pytest.mark.parametrize("code", [0, 1, 23])
def test_cmd_candidate_preserves_arguments_outputs_and_failure_codes(control: Path, spaced: bool, code: int) -> None:
    """Measure the dispatch candidate without shipping a speculative stderr classifier."""
    folder = control.parent / ("repo with spaces" if spaced else "repo")
    folder.mkdir()
    script = folder / "preflight.ps1"
    shutil.copyfile(control, script)
    wrapper = folder / "preflight.cmd"
    wrapper.write_text(CMD_CANDIDATE, encoding="ascii")
    arguments = ["-Update", "-CheckUpstream", "-Tenant", "tenant with spaces", "-Subscription", "sub with spaces"]
    direct = _ps("-ExecutionPolicy", "Bypass", "-File", str(script), *arguments, "-Code", str(code))
    tail = ' -Update -CheckUpstream -Tenant "tenant with spaces" -Subscription "sub with spaces"'
    delegated = subprocess.run(
        f'"{os.environ["COMSPEC"]}" /d /s /c ""{wrapper}"{tail} -Code {code}"',
        env=_native_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert direct.returncode == code
    assert "CONTROL_STDERR" in direct.stderr and "CONTROL_STARTED" in direct.stdout
    assert (delegated.returncode, delegated.stdout, delegated.stderr) == (
        direct.returncode,
        direct.stdout,
        direct.stderr,
    )
    values = json.loads(direct.stdout.splitlines()[1])
    assert values == {
        "Update": True,
        "CheckUpstream": True,
        "Tenant": "tenant with spaces",
        "Subscription": "sub with spaces",
    }


@WINDOWS
def test_startup_and_runtime_security_errors_share_exit_and_error_identifiers(control: Path) -> None:
    """Counterexample to an exit/FQID-based classifier: the script really ran in one case."""
    control.write_text(
        "throw [System.Management.Automation.PSSecurityException]::new('CONTROL_RUNTIME_SECURITY')\n",
        encoding="utf-8",
    )
    blocked = _ps("-ExecutionPolicy", "AllSigned", "-File", str(control))
    executed = _ps("-ExecutionPolicy", "Bypass", "-File", str(control))
    assert blocked.returncode == executed.returncode == 1
    assert blocked.stdout == executed.stdout == ""
    for result in (blocked, executed):
        assert "UnauthorizedAccess" in result.stderr and "SecurityError" in result.stderr
    assert "CONTROL_RUNTIME_SECURITY" not in blocked.stderr
    assert "CONTROL_RUNTIME_SECURITY" in executed.stderr


@WINDOWS
def test_readonly_diagnostics_work_under_process_allsigned_without_exposing_stream_urls(control: Path) -> None:
    """Inline built-in inspection remains possible when an unsigned PS1 is refused."""
    stream = Path(str(control) + ":Zone.Identifier")
    stream.write_text("[ZoneTransfer]\nZoneId=3\nHostUrl=CONTROL_SECRET_URL\n", encoding="ascii")
    inspect = (
        "Get-ExecutionPolicy -List; "
        "Get-Item -LiteralPath $env:CONTROL_FILE -Stream Zone.Identifier | Select-Object Stream; "
        "Get-Content -LiteralPath $env:CONTROL_FILE -Stream Zone.Identifier "
        "| Select-String -Pattern '^ZoneId=[34]$' | Select-Object -ExpandProperty Line"
    )
    result = subprocess.run(
        [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "AllSigned", "-Command", inspect],
        env={**_native_env(), "CONTROL_FILE": str(control)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0
    assert "MachinePolicy" in result.stdout and "AllSigned" in result.stdout
    assert "Zone.Identifier" in result.stdout and "ZoneId=3" in result.stdout
    assert "CONTROL_SECRET_URL" not in result.stdout + result.stderr
    assert str(control) not in result.stdout + result.stderr
