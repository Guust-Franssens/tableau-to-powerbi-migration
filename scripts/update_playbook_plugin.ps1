<#
.SYNOPSIS
    Kill every Copilot CLI process, then re-install the powerbi-playbook plugin.

.DESCRIPTION
    `copilot plugin install` fails with "Access is denied. (os error 5)" while ANY Copilot CLI
    session is running, because the session file-locks ~/.copilot/installed-plugins. There is no
    in-session workaround: the plugin copy SHADOWS .github/skills, so until this runs, agents keep
    executing the OLD skill code no matter what the repo says.

    This is PowerShell rather than Python deliberately: it is Windows process-tree management
    (CIM parent/child walking, Stop-Process) and it must run with no venv activated.

    RUN THIS FROM A PLAIN POWERSHELL WINDOW, NOT FROM INSIDE COPILOT.
    It kills the very session you would be typing into.

.PARAMETER Force
    Skip the countdown and kill immediately.

.PARAMETER SkipInstall
    Only kill the processes; don't re-install. Useful if you want to install by hand afterwards.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/update_playbook_plugin.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/update_playbook_plugin.ps1 -Force
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
$plugin = 'powerbi-playbook@powerbi-playbook-collection'

function Write-Step { param($m) Write-Host "`n== $m" -ForegroundColor Cyan }

# --- 1. Find the Copilot CLI processes ------------------------------------------------------------
# Match on the executable PATH, not just the name. 'M365Copilot.exe' (the Office hub) also matches a
# name-based filter and has nothing to do with the CLI - killing it would be a rude surprise.
Write-Step 'Finding Copilot CLI processes'

$cli = @(Get-Process -Name 'copilot' -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path -notmatch 'M365Copilot' })

if (-not $cli) {
    Write-Host '   none running - nothing to kill' -ForegroundColor Green
}
else {
    # Walk the process tree: the CLI spawns node children (MCP servers, LSP, bridge CLIs) that hold
    # their own handles. Killing only the parent can leave the directory locked.
    $all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name
    $targets = [System.Collections.Generic.List[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    foreach ($p in $cli) { $targets.Add($p.Id); $queue.Enqueue($p.Id) }

    while ($queue.Count -gt 0) {
        $parent = $queue.Dequeue()
        foreach ($child in $all | Where-Object { $_.ParentProcessId -eq $parent }) {
            if (-not $targets.Contains([int]$child.ProcessId)) {
                $targets.Add([int]$child.ProcessId)
                $queue.Enqueue([int]$child.ProcessId)
            }
        }
    }

    Write-Host "   $($cli.Count) CLI session(s) found:"
    foreach ($p in $cli) {
        $age = [int]((Get-Date) - $p.StartTime).TotalMinutes
        Write-Host ("     pid {0,-7} started {1}  ({2} min ago)" -f $p.Id, $p.StartTime.ToString('HH:mm:ss'), $age)
    }
    Write-Host "   plus $($targets.Count - $cli.Count) child process(es) (node/MCP/LSP/shells) holding handles"

    if (-not $Force) {
        Write-Host "`n   WARNING: this kills EVERY Copilot CLI session listed above, not just yours." -ForegroundColor Yellow
        Write-Host '   If a session is mid-migration, let it finish first - there is no resume.' -ForegroundColor Yellow
        Write-Host '   Ctrl-C to abort. Continuing in' -NoNewline -ForegroundColor Yellow
        foreach ($i in 5..1) { Write-Host " $i" -NoNewline -ForegroundColor Yellow; Start-Sleep -Seconds 1 }
        Write-Host ''
    }

    Write-Step 'Killing'
    foreach ($id in $targets) {
        try { Stop-Process -Id $id -Force -ErrorAction Stop; Write-Host "   killed $id" }
        catch { Write-Host "   could not kill $id ($($_.Exception.Message))" -ForegroundColor DarkYellow }
    }

    # The lock is released asynchronously; installing too fast still hits os error 5.
    Write-Host '   waiting for file locks to release...'
    Start-Sleep -Seconds 5
}

if ($SkipInstall) {
    Write-Step 'SkipInstall set - stopping here'
    exit 0
}

# --- 2. Re-install --------------------------------------------------------------------------------
Write-Step "Updating $plugin"

$copilot = (Get-Command copilot -ErrorAction SilentlyContinue).Source
if (-not $copilot) {
    Write-Host '   copilot not on PATH - install by hand once a shell can see it' -ForegroundColor Red
    exit 1
}

# Order matters. `marketplace update` refreshes the cached catalog/clone; without it `plugin update`
# happily reinstalls the same old commit and reports success. Measured: `marketplace update` does NOT
# need the lock and works mid-session, only `plugin update` does - so if you are ever stuck, that
# first call is safe to run from anywhere.
& $copilot plugin marketplace update powerbi-playbook-collection 2>&1 | Out-Host
& $copilot plugin update $plugin 2>&1 | Out-Host

if ($LASTEXITCODE -ne 0) {
    # A half-removed plugin can make `update` fail where a clean re-add succeeds.
    Write-Host '   update failed - retrying as uninstall + install' -ForegroundColor DarkYellow
    & $copilot plugin uninstall $plugin 2>&1 | Out-Host
    & $copilot plugin install   $plugin 2>&1 | Out-Host
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "`n   FAILED. If it still says 'Access is denied. (os error 5)', a Copilot process" -ForegroundColor Red
    Write-Host '   survived - re-run this script, or reboot if it persists.' -ForegroundColor Red
    exit 1
}

# --- 3. Verify ------------------------------------------------------------------------------------
# Don't trust the installer's exit code. preflight hashes each shipped bundle against the installed
# copy, which is the only check that actually proves the shadowing copy is current.
Write-Step 'Verifying with preflight'

$preflight = Join-Path $PSScriptRoot 'preflight.ps1'
if (Test-Path $preflight) {
    & powershell -ExecutionPolicy Bypass -File $preflight |
        Select-String -Pattern 'skill bundles|STALE|NOT INSTALLED|READY' | Out-Host
    Write-Host "`nIf 'skill bundles match published plugin' still says STALE, the marketplace repo"
    Write-Host "itself is behind. Publish first:  python scripts/build_plugin.py --out <clone>"
    Write-Host 'then commit+push there, and re-run this script.'
}
else {
    Write-Host "   preflight.ps1 not found next to this script - verify by hand" -ForegroundColor DarkYellow
}

Write-Step 'Done - start a new Copilot session to pick up the new skills'
