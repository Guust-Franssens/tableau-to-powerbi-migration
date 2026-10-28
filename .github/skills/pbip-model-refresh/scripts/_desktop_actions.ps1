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
  [StructLayout(LayoutKind.Sequential)] public struct Point { public int X; public int Y; }
  [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left; public int Top; public int Right; public int Bottom; }
  [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
  [DllImport("user32.dll")] public static extern bool IsChild(IntPtr parent, IntPtr child);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(Point point);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
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

function Get-ProviderChain {
  param([uint32]$ProviderPid)
  $chain = [System.Collections.Generic.List[string]]::new()
  $seen = [System.Collections.Generic.HashSet[uint32]]::new()
  $childStart = [Int64]::MaxValue
  for ($depth = 0; $depth -lt 16; $depth++) {
    if (-not $seen.Add($ProviderPid)) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProviderPid"
    if ($null -eq $process) { return $null }
    $start = $process.CreationDate.ToUniversalTime().Ticks
    if ($start -gt $childStart) { return $null }
    $chain.Add("$ProviderPid/$start")
    if ($ProviderPid -eq [uint32]$DesktopPid) {
      if ($start.ToString() -ne $ExpectedProcessStart) { return $null }
      return $chain -join ';'
    }
    $childStart = $start
    $ProviderPid = [uint32]$process.ParentProcessId
  }
  return $null
}

function Get-ControlOwnership {
  param($Element, [hashtable]$ScanChains = $null)
  if ([string]$Element.Current.Name -cne $Action -or
      -not $Element.Current.IsEnabled -or $Element.Current.IsOffscreen) { return $null }
  $providerPid = [uint32]$Element.Current.ProcessId
  if ($null -ne $ScanChains) {
    if (-not $ScanChains.ContainsKey($providerPid)) {
      $ScanChains[$providerPid] = Get-ProviderChain $providerPid
    }
    $chain = $ScanChains[$providerPid]
  } else {
    $chain = Get-ProviderChain $providerPid
  }
  if (-not $chain) { return $null }
  $node = $Element
  $renderer = [IntPtr]::Zero
  for ($depth = 0; $depth -lt 64 -and $null -ne $node; $depth++) {
    if ($node.Current.NativeWindowHandle -ne 0) {
      $renderer = [IntPtr]::new([Int64]$node.Current.NativeWindowHandle)
      break
    }
    $node = [System.Windows.Automation.TreeWalker]::RawViewWalker.GetParent($node)
  }
  if ($renderer -eq [IntPtr]::Zero -or
      -not [DesktopActionNative]::BelongsTo($renderer, $providerPid) -or
      ($renderer -ne $targetHwnd -and -not [DesktopActionNative]::IsChild($targetHwnd, $renderer))) {
    return $null
  }
  $bounds = $Element.Current.BoundingRectangle
  $rect = [DesktopActionNative+Rect]::new()
  if ($bounds.IsEmpty -or $bounds.Width -lt 2 -or $bounds.Height -lt 2 -or
      -not [DesktopActionNative]::GetWindowRect($renderer, [ref]$rect)) { return $null }
  foreach ($value in @($bounds.Left, $bounds.Top, $bounds.Right, $bounds.Bottom)) {
    if ([double]::IsNaN($value) -or [double]::IsInfinity($value)) { return $null }
  }
  if ($bounds.Left -lt $rect.Left -or $bounds.Top -lt $rect.Top -or
      $bounds.Right -gt $rect.Right -or $bounds.Bottom -gt $rect.Bottom) { return $null }
  $point = [DesktopActionNative+Point]::new()
  $point.X = [int][Math]::Floor($bounds.Left + $bounds.Width / 2)
  $point.Y = [int][Math]::Floor($bounds.Top + $bounds.Height / 2)
  $hit = [DesktopActionNative]::WindowFromPoint($point)
  if (-not [DesktopActionNative]::BelongsTo($hit, $providerPid) -or
      ($hit -ne $renderer -and -not [DesktopActionNative]::IsChild($renderer, $hit))) { return $null }
  # Read the start chain again: geometry/native ownership cannot excuse PID reuse.
  if ($null -eq $ScanChains -and (Get-ProviderChain $providerPid) -cne $chain) { return $null }
  return "$chain|$($renderer.ToInt64())|$($hit.ToInt64())"
}

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
  $scanChains = @{}
  $survivorProviders = [System.Collections.Generic.HashSet[uint32]]::new()
  foreach ($element in @($buttonElements) + @($splitElements)) {
    if ([string]$element.Current.Name -cne $Action -or
        -not $element.Current.IsEnabled -or $element.Current.IsOffscreen) { continue }
    $pattern = $null
    $expand = $null
    if (-not $element.TryGetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { continue }
    if ($element.TryGetCurrentPattern(
        [System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$expand)) { continue }
    if (Get-ControlOwnership $element $scanChains) {
      $named.Add($element)
      [void]$survivorProviders.Add([uint32]$element.Current.ProcessId)
    }
  }
  # A changed survivor invalidates the whole scan, never just a reduced candidate set.
  foreach ($providerPid in $survivorProviders) {
    $freshChain = Get-ProviderChain $providerPid
    if (-not $freshChain -or $freshChain -cne $scanChains[$providerPid]) {
      $script:reason = 'provider_identity_changed'
      throw 'identity'
    }
  }
  if ($named.Count -ne 1) {
    $script:reason = if ($named.Count -gt 1) { 'ambiguous_named_control' } else { 'named_control_missing' }
    throw 'control'
  }
  return $named[0]
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
$dpiContext = [IntPtr]::Zero

try {
  $dpiContext = [DesktopActionNative]::SetThreadDpiAwarenessContext([IntPtr]::new(-3))
  if ($dpiContext -eq [IntPtr]::Zero) {
    $reason = 'physical_geometry_unavailable'
    throw 'dpi'
  }
  $rootElement = Test-Target
  if (-not $rootElement) {
    $reason = $script:targetFailureReason
    throw 'target'
  }
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    if (-not (Test-Target)) {
      $reason = $script:targetFailureReason
      throw 'identity'
    }
    $foreground = [DesktopActionNative]::GetForegroundWindow()
    if ($foreground -ne $priorForeground -and $foreground -ne $targetHwnd) {
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
  $selected = Get-ActionControl (Test-Target)
  $selectedOwnership = Get-ControlOwnership $selected
  $target = Get-ActionControl (Test-Target)
  $comparison = [System.Windows.Automation.Automation].GetMethod('Compare', [type[]]@(
    [System.Windows.Automation.AutomationElement], [System.Windows.Automation.AutomationElement]))
  if (-not $comparison.Invoke($null, [object[]]@($selected, $target))) {
    $reason = 'selected_control_changed'
    throw 'control'
  }
  $invokePattern = $null
  if (-not $target.TryGetCurrentPattern(
      [System.Windows.Automation.InvokePattern]::Pattern, [ref]$invokePattern)) {
    $reason = 'named_control_not_actionable'
    throw 'control'
  }

  $ownership = Get-ControlOwnership $target
  if (-not $ownership -or $ownership -cne $selectedOwnership) {
    $reason = 'control_ownership_changed'
    throw 'control'
  }
  if (-not (Test-Target)) {
    $reason = $script:targetFailureReason
    throw 'identity'
  }
  if ([DesktopActionNative]::GetForegroundWindow() -ne $targetHwnd) {
    $reason = 'target_or_foreground_changed'
    throw 'focus'
  }
  $actionTicks = [DateTime]::UtcNow.Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
  try { $invokePattern.Invoke() } catch {
    $reason = 'invoke_failed'
    throw
  }
  $invoked = $true
  $status = 'invoked'
  $reason = 'invoke_returned'

} catch {
  if ($reason -eq 'target_unavailable') { $reason = 'uia_or_control_unavailable' }
} finally {
  # Refusals after acquiring focus need the same respectful handback as an Invoke.
  $foregroundAfter = [DesktopActionNative]::GetForegroundWindow()
  if ($attempts -gt 0) {
  if ($foregroundAfter -eq $targetHwnd) {
    if ($priorForeground -eq [IntPtr]::Zero -or $priorForeground -eq $targetHwnd) {
      $handbackOk = $true
    } else {
      $restoreRequested = $true
      if ([DesktopActionNative]::IsWindow($priorForeground)) {
        [void][DesktopActionNative]::SetForegroundWindow($priorForeground)
        Start-Sleep -Milliseconds 150
        $restoreVerified = [DesktopActionNative]::GetForegroundWindow() -eq $priorForeground
        $handbackOk = [bool]$restoreVerified
        if (-not $handbackOk -and $invoked) { $reason = 'handback_not_verified' }
      } else {
        $restoreVerified = $false
        if ($invoked) { $reason = 'prior_foreground_unavailable' }
      }
    }
  } else {
    if ($invoked) { $reason = 'foreground_changed_after_invoke' }
    $handbackOk = $false
  }
  }
  if ($dpiContext -ne [IntPtr]::Zero) {
    [void][DesktopActionNative]::SetThreadDpiAwarenessContext($dpiContext)
  }
}

$result = New-Result $status $invoked $reason $attempts $actionTicks `
  $restoreRequested $restoreVerified $handbackOk
Write-Output (ConvertTo-Json -InputObject $result -Compress)
