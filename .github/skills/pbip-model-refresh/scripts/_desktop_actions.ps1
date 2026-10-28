<#
.SYNOPSIS
  Perform one bounded, named UIA Refresh or Save action against an exact Desktop window.
.DESCRIPTION
  This child owns only foreground/action facts. It does not inspect engine completion, classify prompts,
  or retry an action. Python supervises and reaps this process.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidateSet('Refresh', 'Save')][string]$Action,
  [Parameter(Mandatory = $true)][ValidatePattern('^[1-9][0-9]{0,9}$')][string]$DesktopPid,
  [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]{1,20}$')][string]$ExpectedProcessStart,
  [Parameter(Mandatory = $true)][ValidatePattern('^[1-9][0-9]{0,19}$')][string]$RootHandle
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class DesktopActionNative {
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd, uint command);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", SetLastError = true)] public static extern bool SetForegroundWindow(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
  public static bool BelongsTo(IntPtr hwnd, uint expected) {
    uint actual;
    return IsWindow(hwnd) && GetWindowThreadProcessId(hwnd, out actual) != 0 && actual == expected;
  }
}
"@

function Get-ProcessStart {
  $process = Get-CimInstance Win32_Process -Filter "ProcessId=$DesktopPid"
  if ($null -eq $process) { return $null }
  return $process.CreationDate.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
}

function Test-Target {
  $start = Get-ProcessStart
  $root = [IntPtr]::new([Int64]$RootHandle)
  $expectedPid = [uint32]$DesktopPid
  if ($start -ne $ExpectedProcessStart) {
    $script:targetFailureReason = 'process_identity_changed'
    return $false
  }
  if (-not [DesktopActionNative]::BelongsTo($root, $expectedPid)) {
    $script:targetFailureReason = 'root_process_identity_changed'
    return $false
  }
  if (-not [DesktopActionNative]::IsWindowVisible($root) -or
      -not [DesktopActionNative]::IsWindowEnabled($root) -or
      [DesktopActionNative]::IsIconic($root) -or
      [DesktopActionNative]::GetWindow($root, 4) -ne [IntPtr]::Zero) {
    $script:targetFailureReason = 'root_window_not_actionable'
    return $false
  }
  try {
    $element = [System.Windows.Automation.AutomationElement]::FromHandle($root)
    if ($null -eq $element -or
        $element.Current.ProcessId -ne $expectedPid -or
        $element.Current.NativeWindowHandle -ne $root.ToInt64()) {
      $script:targetFailureReason = 'root_uia_identity_changed'
      return $false
    }
    return $element
  } catch {
    $script:targetFailureReason = 'root_uia_unavailable'
    return $false
  }
}

function New-Result {
  param(
    [string]$Status,
    [bool]$Invoked,
    [string]$Reason,
    [int]$Attempts,
    [object]$ActionTicks,
    [bool]$RestoreRequested,
    [Nullable[bool]]$RestoreVerified,
    [bool]$HandbackOk
  )
  return [ordered]@{
    schema='pbip.desktop-action.v1'
    status=$Status
    action=$Action
    desktop_pid=$DesktopPid
    process_started_at=$ExpectedProcessStart
    root_hwnd=$RootHandle
    invoked=$Invoked
    reason=$Reason
    focus_attempts=$Attempts
    action_started_ticks=$ActionTicks
    restore_requested=$RestoreRequested
    restore_verified=$RestoreVerified
    handback_ok=$HandbackOk
  }
}

function Get-ActionControl {
  param([System.Windows.Automation.AutomationElement]$RootElement)
  $buttonType = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button)
  $splitButtonType = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::SplitButton)
  $buttonElements = $RootElement.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants, $buttonType)
  $splitElements = $RootElement.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants, $splitButtonType)
  $named = [System.Collections.Generic.List[object]]::new()
  foreach ($element in $buttonElements) {
    if ([string]$element.Current.Name -ceq $Action) { $named.Add($element) }
  }
  foreach ($element in $splitElements) {
    if ([string]$element.Current.Name -ceq $Action) { $named.Add($element) }
  }
  if ($named.Count -ne 1) {
    $script:reason = if ($named.Count -gt 1) { 'ambiguous_named_control' } else { 'named_control_missing' }
    throw 'control'
  }
  $element = $named[0]
  if ($element.Current.ProcessId -ne [uint32]$DesktopPid -or
      -not $element.Current.IsEnabled -or $element.Current.IsOffscreen) {
    $script:reason = 'named_control_not_actionable'
    throw 'control'
  }
  return $element
}

$attempts = 0
$invoked = $false
$restoreRequested = $false
$restoreVerified = $null
$handbackOk = $false
$actionTicks = $null
$status = 'refused'
$reason = 'target_unavailable'
$priorForeground = [DesktopActionNative]::GetForegroundWindow()
$targetHwnd = [IntPtr]::new([Int64]$RootHandle)

try {
  $rootElement = Test-Target
  if (-not $rootElement) {
    $reason = $script:targetFailureReason
    throw 'target'
  }
  $target = Get-ActionControl $rootElement
  $invokePattern = $null
  if (-not $target.TryGetCurrentPattern(
      [System.Windows.Automation.InvokePattern]::Pattern, [ref]$invokePattern)) {
    $reason = 'named_control_not_actionable'
    throw 'control'
  }

  for ($attempt = 1; $attempt -le 3; $attempt++) {
    if (-not (Test-Target)) {
      $reason = $script:targetFailureReason
      throw 'identity'
    }
    $foreground = [DesktopActionNative]::GetForegroundWindow()
    if ($attempt -gt 1 -and $foreground -ne $priorForeground -and $foreground -ne $targetHwnd) {
      $reason = 'foreground_changed'
      throw 'focus'
    }
    $attempts = $attempt
    [void][DesktopActionNative]::SetForegroundWindow($targetHwnd)
    Start-Sleep -Milliseconds 150
    if (-not (Test-Target)) {
      $reason = $script:targetFailureReason
      throw 'identity'
    }
    if ([DesktopActionNative]::GetForegroundWindow() -eq $targetHwnd) { break }
  }
  if ([DesktopActionNative]::GetForegroundWindow() -ne $targetHwnd) {
    $reason = 'focus_not_verified'
    throw 'focus'
  }
  if (-not (Test-Target) -or [DesktopActionNative]::GetForegroundWindow() -ne $targetHwnd) {
    if (-not (Test-Target)) { $reason = $script:targetFailureReason }
    else { $reason = 'target_or_foreground_changed' }
    throw 'identity'
  }
  $target = Get-ActionControl (Test-Target)
  $invokePattern = $null
  if (-not $target.TryGetCurrentPattern(
      [System.Windows.Automation.InvokePattern]::Pattern, [ref]$invokePattern)) {
    $reason = 'named_control_not_actionable'
    throw 'control'
  }

  $actionTicks = [DateTime]::UtcNow.Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
  try { $invokePattern.Invoke() } catch {
    $reason = 'invoke_failed'
    throw
  }
  $invoked = $true
  $status = 'invoked'
  $reason = 'invoke_returned'

  $foregroundAfter = [DesktopActionNative]::GetForegroundWindow()
  if ($foregroundAfter -eq $targetHwnd) {
    if ($priorForeground -eq [IntPtr]::Zero -or $priorForeground -eq $targetHwnd) {
      $handbackOk = $true
      $reason = 'invoke_returned'
    } else {
      $restoreRequested = $true
      if ([DesktopActionNative]::IsWindow($priorForeground)) {
        [void][DesktopActionNative]::SetForegroundWindow($priorForeground)
        Start-Sleep -Milliseconds 150
        $restoreVerified = [DesktopActionNative]::GetForegroundWindow() -eq $priorForeground
        $handbackOk = [bool]$restoreVerified
        if (-not $handbackOk) { $reason = 'handback_not_verified' }
      } else {
        $restoreVerified = $false
        $reason = 'prior_foreground_unavailable'
      }
    }
  } else {
    $reason = 'foreground_changed_after_invoke'
    $handbackOk = $false
  }
} catch {
  if ($invoked) {
    if (-not $reason -or $reason -eq 'invoke_returned') { $reason = 'handback_not_verified' }
  } elseif ($reason -eq 'target_unavailable') {
    $reason = 'uia_or_control_unavailable'
  }
}

$result = New-Result $status $invoked $reason $attempts $actionTicks `
  $restoreRequested $restoreVerified $handbackOk
Write-Output (ConvertTo-Json -InputObject $result -Compress)
