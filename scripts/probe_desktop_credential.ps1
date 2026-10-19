<#
.SYNOPSIS
  Retired compatibility command: always refuses, never observes or invokes Refresh.
.DESCRIPTION
  Every old parameter set is accepted for safe retirement, including DesktopPid, TimeoutSec,
  HarvestTimeoutSec, HarvestMaxElements, LoadDetectorsOnly, HarvestHwnd and DiagnosticPid.
  Arguments are deliberately not reflected. Use refresh_pbip_model.py / probe_desktop_query.py.
#>
[CmdletBinding()]
param(
  [string]$DesktopPid,
  [string]$TimeoutSec,
  [string]$HarvestTimeoutSec,
  [string]$HarvestMaxElements,
  [switch]$LoadDetectorsOnly,
  [string]$HarvestHwnd,
  [string]$DiagnosticPid
)
Write-Output 'VERDICT: UNKNOWN'
Write-Output 'Retired command: no observation or Refresh performed. Use refresh_pbip_model.py or probe_desktop_query.py.'
exit 3
