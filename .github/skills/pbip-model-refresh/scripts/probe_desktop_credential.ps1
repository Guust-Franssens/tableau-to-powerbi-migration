<#
.SYNOPSIS
  Detect whether Power BI Desktop already has a cached credential for the live data source(s) in the
  currently open model, WITHOUT the agent being able to type the credential itself.

.DESCRIPTION
  Live database sources (Databricks, SQL Server, Snowflake, ...) need a credential that is NOT in the
  committed model files. In Power BI Desktop that credential is cached per-Windows-user (DPAPI) after
  the user authenticates once in a modal dialog. The Desktop Bridge cannot fill that modal, so before
  the agent's build/render/validate loop can work against live data, a human must sign in once.

  This probe tells the two states apart so the migrator only prompts when needed. NOTE: for a
  *serverless* source that cold-starts, the modal can appear only AFTER this probe's timeout, yielding a
  false CREDENTIAL_PRESENT; treat the one-row data probe (DATA_OK vs NO_DATA) as the gate of record and
  use this modal probe only to explain a NO_DATA. That probe ships inside the pbip-model-refresh skill:
  .github/skills/pbip-model-refresh/scripts/probe_desktop_query.py (scripts/probe_desktop_query.py is a
  forwarding shim kept for existing callers).
    * If a credential modal is already open, or appears within -TimeoutSec of a refresh -> MISSING.
    * If a refresh proceeds with no modal -> PRESENT (a credential is cached machine-wide; the loop
      can run unattended) -- but re-confirm with the one-row data probe for serverless sources.

  It triggers Refresh via UI Automation (the Bridge exposes no refresh verb) and watches every
  top-level window of the target process for the connector credential dialog's signature text.

  Windows-only by necessity (UI Automation / DPAPI). This is the sanctioned PowerShell exception to
  the "committed scripts default to .py/.sh" rule, and it sits alongside scripts/preflight.ps1.

.PARAMETER DesktopPid
  The Power BI Desktop process id to drive (use the pid from `powerbi-desktop open`/`status`).

.PARAMETER TimeoutSec
  How long to wait for the credential modal after triggering refresh. Default 75s; use >=60s because a
  serverless warehouse (e.g. Databricks) can cold-start before the prompt appears.

.OUTPUTS
  Final line `VERDICT: CREDENTIAL_MISSING` (exit 1) or `VERDICT: CREDENTIAL_PRESENT` (exit 0), or
  `VERDICT: UNKNOWN` (exit 3) if no window for the pid can be found OR a Refresh control was never
  invoked (with nothing invoked, "no modal appeared" proves nothing - reporting PRESENT then would be
  a fail-open arbiter, worse than none).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/probe_desktop_credential.ps1 -DesktopPid 42532
#>
param(
  [Parameter(Mandatory = $true)][int]$DesktopPid,
  [int]$TimeoutSec = 75
)

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase

$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $DesktopPid)

# Connector credential-dialog signature text (covers Databricks / SQL / Snowflake / generic OAuth).
$sig = 'You aren.t signed in|Personal Access Token|Databricks Client Credentials|specify how to connect|Account Key|Enter your credentials|Please specify how to connect'

function Get-PidWindows {
  # EVERY top-level window of the target process, not just the first. A credential modal is its own
  # top-level window (a sibling of the main Desktop window, not a child), so scanning only the first
  # would miss it - and the whole point of this arbiter is to catch that modal.
  return $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
}

function Test-CredentialModal {
  foreach ($w in Get-PidWindows) {
    $desc = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($d in $desc) {
      $n = $d.Current.Name
      if ($n -and $n -match $sig) { return $n }
    }
  }
  return $null
}

$windows = Get-PidWindows
if (-not $windows -or $windows.Count -eq 0) { Write-Output "no window for pid $DesktopPid found"; Write-Output "VERDICT: UNKNOWN"; exit 3 }

# 1. If a credential modal is ALREADY open, the credential is missing - report immediately.
$hit = Test-CredentialModal
if ($hit) {
  Write-Output ("credential modal already open: '{0}'" -f $hit.Substring(0, [Math]::Min(80, $hit.Length)))
  Write-Output "VERDICT: CREDENTIAL_MISSING"
  exit 1
}

# 2. Otherwise trigger a refresh and watch for the modal (generous timeout for warehouse cold-start).
# Search every top-level window for an invokable 'Refresh', not just the first.
$invoked = $false
foreach ($w in $windows) {
  $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  foreach ($e in $all) {
    if ($e.Current.Name -eq 'Refresh' -and (($e.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName }) -match 'Invoke')) {
      try { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); $invoked = $true; break } catch {}
    }
  }
  if ($invoked) { break }
}
Write-Output "refresh invoked: $invoked"

# If no Refresh was ever invoked, we did not actually test anything: "no modal appeared" is not
# evidence a credential is cached. Report UNKNOWN rather than a fail-open CREDENTIAL_PRESENT.
if (-not $invoked) {
  Write-Output "no invokable Refresh control found for pid $DesktopPid - cannot probe the credential state"
  Write-Output "VERDICT: UNKNOWN"
  exit 3
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 2000
  $hit = Test-CredentialModal
  if ($hit) {
    Write-Output ("credential modal detected: '{0}'" -f $hit.Substring(0, [Math]::Min(80, $hit.Length)))
    Write-Output "VERDICT: CREDENTIAL_MISSING"
    exit 1
  }
}
Write-Output "no credential modal within ${TimeoutSec}s (refresh proceeded)"
Write-Output "VERDICT: CREDENTIAL_PRESENT"
exit 0
