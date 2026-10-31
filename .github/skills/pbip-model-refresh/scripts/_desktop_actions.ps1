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
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("kernel32.dll")] private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
  [DllImport("kernel32.dll")] private static extern bool GetProcessTimes(
    IntPtr process, out long creation, out long exit, out long kernel, out long user);
  [DllImport("kernel32.dll")] private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
  [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
  public static string GetProcessStart(uint pid) {
    // QUERY_LIMITED_INFORMATION | SYNCHRONIZE: no elevation or process-modification rights.
    IntPtr handle = OpenProcess(0x101000, false, pid);
    if (handle == IntPtr.Zero) return null;
    try {
      long creation, exit, kernel, user;
      if (WaitForSingleObject(handle, 0) != 258 ||
          !GetProcessTimes(handle, out creation, out exit, out kernel, out user) ||
          WaitForSingleObject(handle, 0) != 258) return null;
      long ticks = DateTime.FromFileTimeUtc(creation).Ticks;
      // Win32_Process.CreationDate (DMTF) preserves microseconds, not the last 100 ns digit.
      return (ticks - ticks % 10).ToString(System.Globalization.CultureInfo.InvariantCulture);
    } finally { CloseHandle(handle); }
  }
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint first, uint second, bool attach);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hwnd);
  [DllImport("user32.dll", SetLastError = true)] public static extern bool SetForegroundWindow(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
  public static bool BelongsTo(IntPtr hwnd, uint expected) {
    uint actual;
    return IsWindow(hwnd) && GetWindowThreadProcessId(hwnd, out actual) != 0 && actual == expected;
  }
}
"@

$script:processChains = @{}

function Get-ProviderChain {
  param([uint32]$ProviderPid)
  if (-not $script:processChains.ContainsKey($ProviderPid)) {
    $chain = [System.Collections.Generic.List[object]]::new()
    $seen = [System.Collections.Generic.HashSet[uint32]]::new()
    $childStart = [Int64]::MaxValue
    $processPid = $ProviderPid
    for ($depth = 0; $depth -lt 16; $depth++) {
      if (-not $seen.Add($processPid)) { return $null }
      $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processPid"
      if ($null -eq $process -or $null -eq $process.CreationDate) { return $null }
      $start = $process.CreationDate.ToUniversalTime().Ticks
      if ($start -gt $childStart) { return $null }
      $startText = $start.ToString([Globalization.CultureInfo]::InvariantCulture)
      $chain.Add([pscustomobject]@{Pid=$processPid; Start=$startText})
      if ($processPid -eq [uint32]$DesktopPid) {
        if ($startText -cne $ExpectedProcessStart) { return $null }
        break
      }
      $childStart = $start
      $processPid = [uint32]$process.ParentProcessId
    }
    if ($chain[$chain.Count - 1].Pid -ne [uint32]$DesktopPid) { return $null }
    $script:processChains[$ProviderPid] = $chain
  }
  # Parentage is immutable only within an incarnation. Recheck EVERY cached edge endpoint.
  $identities = [System.Collections.Generic.List[string]]::new()
  foreach ($process in $script:processChains[$ProviderPid]) {
    if ([DesktopActionNative]::GetProcessStart($process.Pid) -cne $process.Start) { return $null }
    $identities.Add("$($process.Pid)/$($process.Start)")
  }
  return $identities -join ';'
}

function Get-ControlOwnership {
  param($Element, [hashtable]$ScanChains = $null, [switch]$Prepare, $PreparedBinding = $null)
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
  $binding = "$chain|$($renderer.ToInt64())"
  if ($null -ne $PreparedBinding) { $PreparedBinding.Value = $binding }
  # Native hit testing is meaningful only after the target is foreground.
  if ($Prepare) { return $binding }
  $point = [DesktopActionNative+Point]::new()
  $point.X = [int][Math]::Floor($bounds.Left + $bounds.Width / 2)
  $point.Y = [int][Math]::Floor($bounds.Top + $bounds.Height / 2)
  $hit = [DesktopActionNative]::WindowFromPoint($point)
  if (-not [DesktopActionNative]::BelongsTo($hit, $providerPid) -or
      ($hit -ne $renderer -and -not [DesktopActionNative]::IsChild($renderer, $hit))) { return $null }
  # Read the start chain again: geometry/native ownership cannot excuse PID reuse.
  if ($null -eq $ScanChains -and (Get-ProviderChain $providerPid) -cne $chain) { return $null }
  return "$binding|$($hit.ToInt64())"
}

function Get-ProcessStart {
  return [DesktopActionNative]::GetProcessStart([uint32]$DesktopPid)
}

function Get-WindowIdentity {
  param([IntPtr]$Hwnd)
  $windowPid = [uint32]0
  if (-not [DesktopActionNative]::IsWindow($Hwnd) -or
      [DesktopActionNative]::GetWindowThreadProcessId($Hwnd, [ref]$windowPid) -eq 0) { return $null }
  $start = [DesktopActionNative]::GetProcessStart($windowPid)
  if (-not $start) { return $null }
  return "$windowPid/$start"
}

function Set-VerifiedForeground {
  param([IntPtr]$Destination, [IntPtr]$PermittedForeground, [string]$DestinationIdentity, [switch]$Handback)
  $attached = [System.Collections.Generic.List[object]]::new()
  try {
    if (-not (Test-Target -IdentityOnly:$Handback) -or
        (Get-WindowIdentity $Destination) -cne $DestinationIdentity -or
        [DesktopActionNative]::GetForegroundWindow() -ne $PermittedForeground) { return $false }
    $currentThread = [DesktopActionNative]::GetCurrentThreadId()
    $ignoredPid = [uint32]0
    $foregroundThread = [DesktopActionNative]::GetWindowThreadProcessId($PermittedForeground, [ref]$ignoredPid)
    $targetThread = [DesktopActionNative]::GetWindowThreadProcessId($Destination, [ref]$ignoredPid)
    if ($currentThread -eq 0 -or $foregroundThread -eq 0 -or $targetThread -eq 0) { return $false }
    $seen = [System.Collections.Generic.HashSet[uint32]]::new()
    foreach ($thread in @($foregroundThread, $targetThread)) {
      if ($thread -eq $currentThread -or -not $seen.Add($thread)) { continue }
      if (-not (Test-Target -IdentityOnly:$Handback) -or
          (Get-WindowIdentity $Destination) -cne $DestinationIdentity -or
          [DesktopActionNative]::GetForegroundWindow() -ne $PermittedForeground) { return $false }
      if (-not [DesktopActionNative]::AttachThreadInput($currentThread, $thread, $true)) { return $false }
      $attached.Add(@($currentThread, $thread))
    }
    if (-not (Test-Target -IdentityOnly:$Handback) -or
        (Get-WindowIdentity $Destination) -cne $DestinationIdentity -or
        [DesktopActionNative]::GetForegroundWindow() -ne $PermittedForeground) { return $false }
    [void][DesktopActionNative]::BringWindowToTop($Destination)
    # BringWindowToTop may itself activate the destination. A third window is a human switch.
    if (-not (Test-Target -IdentityOnly:$Handback) -or
        (Get-WindowIdentity $Destination) -cne $DestinationIdentity) { return $false }
    $foreground = [DesktopActionNative]::GetForegroundWindow()
    if ($foreground -ne $PermittedForeground -and $foreground -ne $Destination) { return $false }
    [void][DesktopActionNative]::SetForegroundWindow($Destination)
    return $true
  } finally {
    for ($index = $attached.Count - 1; $index -ge 0; $index--) {
      [void][DesktopActionNative]::AttachThreadInput($attached[$index][0], $attached[$index][1], $false)
    }
  }
}

function Test-Target {
  param([switch]$IdentityOnly)
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
      [DesktopActionNative]::IsIconic($root) -or
      [DesktopActionNative]::GetWindow($root, 4) -ne [IntPtr]::Zero) {
    $script:targetFailureReason = 'root_window_not_actionable'
    return $false
  }
  # Returning focus does not invoke Desktop. A disabled root is not permission to act,
  # but is still the held root; an activated modal/other HWND is never eligible for handback.
  if ($IdentityOnly) { return $true }
  if (-not [DesktopActionNative]::IsWindowEnabled($root)) {
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

function Get-ActionCandidates {
  param([System.Windows.Automation.AutomationElement]$RootElement)
  $buttonType = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button)
  $splitButtonType = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::SplitButton)
  $name = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::NameProperty, $Action)
  $condition = [System.Windows.Automation.AndCondition]::new(
    $name, [System.Windows.Automation.OrCondition]::new($buttonType, $splitButtonType))
  $elements = $RootElement.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants, $condition)
  $named = [System.Collections.Generic.List[object]]::new()
  $scanChains = @{}
  foreach ($element in $elements) {
    if ([string]$element.Current.Name -cne $Action) { continue }
    $pattern = $null
    $expand = $null
    $actionable = $element.TryGetCurrentPattern(
      [System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern) -and -not $element.TryGetCurrentPattern(
      [System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$expand)
    $ownership = Get-ControlOwnership $element $scanChains -Prepare
    $named.Add([pscustomobject]@{
      Element=$element
      Ownership=$ownership
      Actionable=$actionable
      ProviderPid=[uint32]$element.Current.ProcessId
    })
  }
  return [pscustomobject]@{
    Controls=$named; Chains=$scanChains; RootElement=$RootElement; Condition=$condition
  }
}

function Get-ActionControl {
  param($Candidates)
  # Preparation cannot prove completeness after focus: controls can appear or be renamed.
  # Rediscover only the exact Name/Button-or-SplitButton set, without repeating ownership walks.
  $current = @($Candidates.RootElement.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants, $Candidates.Condition))
  if ($current.Count -ne $Candidates.Controls.Count) {
    $script:reason = 'selected_control_changed'
    throw 'control'
  }
  $membershipComparison = [System.Windows.Automation.Automation].GetMethod('Compare', [type[]]@(
    [System.Windows.Automation.AutomationElement], [System.Windows.Automation.AutomationElement]))
  foreach ($element in $current) {
    $matches = 0
    foreach ($candidate in $Candidates.Controls) {
      if ($membershipComparison.Invoke($null, [object[]]@($element, $candidate.Element))) { $matches++ }
    }
    if ($matches -ne 1) {
      $script:reason = 'selected_control_changed'
      throw 'control'
    }
  }
  $scanChains = @{}
  # Re-read each prepared provider once per pass, not once per control.
  foreach ($candidate in $Candidates.Controls) {
    if (-not $candidate.Ownership) { continue }
    $providerPid = $candidate.ProviderPid
    if ($scanChains.ContainsKey($providerPid)) { continue }
    $freshChain = Get-ProviderChain $providerPid
    if (-not $freshChain -or $freshChain -cne $Candidates.Chains[$providerPid]) {
      $script:reason = 'provider_identity_changed'
      throw 'identity'
    }
    $scanChains[$providerPid] = $freshChain
  }
  $named = [System.Collections.Generic.List[object]]::new()
  foreach ($candidate in $Candidates.Controls) {
    $element = $candidate.Element
    $pattern = $null
    $expand = $null
    $actionable = $element.TryGetCurrentPattern(
      [System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern) -and -not $element.TryGetCurrentPattern(
      [System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$expand)
    $binding = $null
    $ownership = Get-ControlOwnership $element $scanChains -Prepare:(-not $candidate.Actionable) `
      -PreparedBinding ([ref]$binding)
    # A stale prepared control invalidates the set; never silently drop it.
    if ($binding -cne $candidate.Ownership -or $actionable -ne $candidate.Actionable) {
      $script:reason = 'selected_control_changed'
      throw 'control'
    }
    if (-not $candidate.Actionable -or -not $candidate.Ownership) { continue }
    if ($ownership) {
      $named.Add([pscustomobject]@{ Element=$element; Ownership=$ownership })
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
$priorIdentity = Get-WindowIdentity $priorForeground
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
  $candidates = Get-ActionCandidates $rootElement
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
    $focusRequested = Set-VerifiedForeground $targetHwnd $foreground "$DesktopPid/$ExpectedProcessStart"
    if (-not $focusRequested) {
      $reason = 'focus_attachment_or_identity_failed'
      throw 'focus'
    }
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
  $selectedControl = Get-ActionControl $candidates
  $selected = $selectedControl.Element
  $selectedOwnership = $selectedControl.Ownership
  $targetControl = Get-ActionControl $candidates
  $target = $targetControl.Element
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
      if ($priorIdentity -and (Get-WindowIdentity $priorForeground) -ceq $priorIdentity -and
          [DesktopActionNative]::GetForegroundWindow() -eq $targetHwnd) {
        $restoreStarted = Set-VerifiedForeground $priorForeground $targetHwnd $priorIdentity -Handback
        Start-Sleep -Milliseconds 150
        $restoreVerified = $restoreStarted -and
          [DesktopActionNative]::GetForegroundWindow() -eq $priorForeground -and
          (Get-WindowIdentity $priorForeground) -ceq $priorIdentity
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
