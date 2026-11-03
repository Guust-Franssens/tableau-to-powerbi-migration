<#
.SYNOPSIS
  Perform one bounded, named UIA Refresh or Save action against an exact Desktop window.
.DESCRIPTION
  This child owns only action facts. It does not inspect engine completion, classify prompts,
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
  [DllImport("user32.dll")] public static extern IntPtr ChildWindowFromPointEx(IntPtr parent, Point point, uint flags);
  [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr hwnd, ref Point point);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd, uint command);
  private delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr parameter);
  [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);
  public static IntPtr[] TopLevelSnapshot() {
    // Every top-level window, top-down, with no early stop: a FALSE return is a failure, never a stop.
    var windows = new System.Collections.Generic.List<IntPtr>();
    return EnumWindows((hwnd, parameter) => { windows.Add(hwnd); return true; }, IntPtr.Zero)
      ? windows.ToArray() : new IntPtr[0];
  }
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

function Get-ProviderIdentity {
  param([uint32]$ProviderPid)
  $start = [DesktopActionNative]::GetProcessStart($ProviderPid)
  if (-not $start) { return $null }
  return "$ProviderPid/$start"
}

function Get-ControlRenderer {
  param($Element)
  $node = $Element
  for ($depth = 0; $depth -lt 64 -and $null -ne $node; $depth++) {
    if ($node.Current.NativeWindowHandle -ne 0) {
      return [IntPtr]::new([Int64]$node.Current.NativeWindowHandle)
    }
    $node = [System.Windows.Automation.TreeWalker]::RawViewWalker.GetParent($node)
  }
  return [IntPtr]::Zero
}

function Test-ControlRenderer {
  param($Element, [uint32]$ProviderPid, [IntPtr]$Renderer)
  if ([uint32]$Element.Current.ProcessId -ne $ProviderPid -or
      $Renderer -eq [IntPtr]::Zero -or
      (Get-ControlRenderer $Element) -ne $Renderer -or
      -not [DesktopActionNative]::BelongsTo($Renderer, $ProviderPid)) { return $false }
  return $Renderer -eq $targetHwnd -or [DesktopActionNative]::IsChild($targetHwnd, $Renderer)
}

function Get-ControlNativeChain {
  param([IntPtr]$Renderer, [string]$ProviderIdentity)
  $chain = [System.Collections.Generic.List[string]]::new()
  $seen = [System.Collections.Generic.HashSet[Int64]]::new()
  $node = $Renderer
  for ($depth = 0; $depth -lt 64 -and $node -ne [IntPtr]::Zero; $depth++) {
    if (-not $seen.Add($node.ToInt64()) -or -not [DesktopActionNative]::IsWindow($node)) { return $null }
    $identity = Get-WindowIdentity $node
    if (-not $identity -or
        ($node -eq $Renderer -and $identity -cne $ProviderIdentity) -or
        ($node -ne $Renderer -and $identity -cne "$DesktopPid/$ExpectedProcessStart" -and
          $identity -cne $ProviderIdentity)) { return $null }
    $chain.Add("$($node.ToInt64())/$identity")
    if ($node -eq $targetHwnd) {
      if ($identity -cne "$DesktopPid/$ExpectedProcessStart") { return $null }
      return $chain -join '>'
    }
    $node = [DesktopActionNative]::GetParent($node)
  }
  return $null
}

function Find-DescentHit {
  param($Point)
  # Descend Desktop's own child z-order from the held root; the foreground window is irrelevant.
  # 0x3 skips invisible and disabled children, never WS_EX_TRANSPARENT ones: the live renderer has it.
  $node = $targetHwnd
  for ($depth = 0; $depth -lt 32; $depth++) {
    $client = [DesktopActionNative+Point]::new()
    $client.X = $Point.X
    $client.Y = $Point.Y
    if (-not [DesktopActionNative]::ScreenToClient($node, [ref]$client)) { return [IntPtr]::Zero }
    $child = [DesktopActionNative]::ChildWindowFromPointEx($node, $client, 3)
    if ($child -eq [IntPtr]::Zero) { return [IntPtr]::Zero }
    if ($child -eq $node) { return $node }
    $node = $child
  }
  return [IntPtr]::Zero
}

function Get-ControlOwnership {
  param(
    $Element,
    [hashtable]$ScanChains = $null,
    [hashtable]$ScanProviders = $null,
    [switch]$Prepare,
    $PreparedBinding = $null
  )
  if ([string]$Element.Current.Name -cne $Action -or
      -not $Element.Current.IsEnabled -or $Element.Current.IsOffscreen) { return $null }
  if (-not (Test-Target -IdentityOnly)) { return $null }
  $providerPid = [uint32]$Element.Current.ProcessId
  $providerIdentity = Get-ProviderIdentity $providerPid
  if (-not $providerIdentity) { return $null }
  if ($null -ne $ScanProviders) {
    if ($ScanProviders.ContainsKey($providerPid)) {
      if ($ScanProviders[$providerPid] -cne $providerIdentity) { return $null }
    } else {
      $ScanProviders[$providerPid] = $providerIdentity
    }
  }
  if ($null -ne $ScanChains) {
    if (-not $ScanChains.ContainsKey($providerPid)) {
      $ScanChains[$providerPid] = Get-ProviderChain $providerPid
    }
    $chain = $ScanChains[$providerPid]
  } else {
    $chain = Get-ProviderChain $providerPid
  }
  $renderer = Get-ControlRenderer $Element
  if (-not (Test-ControlRenderer $Element $providerPid $renderer)) { return $null }
  $nativeChain = Get-ControlNativeChain $renderer $providerIdentity
  if (-not $nativeChain) { return $null }
  if ($chain) {
    $providerRoute = "ancestry:$chain"
  } elseif ($providerPid -ne [uint32]$DesktopPid -and
      $renderer -ne $targetHwnd -and [DesktopActionNative]::IsChild($targetHwnd, $renderer)) {
    # Some Desktop builds share a UIA provider across windows. Trust that provider only
    # through its live incarnation and native renderer HWND beneath this exact root.
    $providerRoute = "shared:$providerIdentity"
  } else {
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
  $culture = [Globalization.CultureInfo]::InvariantCulture
  $geometry = (@($bounds.Left, $bounds.Top, $bounds.Right, $bounds.Bottom) |
    ForEach-Object { ([double]$_).ToString('R', $culture) }) -join ','
  $rectIdentity = "$($rect.Left),$($rect.Top),$($rect.Right),$($rect.Bottom)"
  $binding = "$DesktopPid/$ExpectedProcessStart|$RootHandle|$providerIdentity|$providerRoute|$nativeChain|$geometry|$rectIdentity"
  if ($null -ne $PreparedBinding) { $PreparedBinding.Value = $binding }
  # Native hit testing runs only in the act phase.
  if ($Prepare) {
    if (-not (Test-Target -IdentityOnly) -or
        (Get-ProviderIdentity $providerPid) -cne $providerIdentity -or
        -not (Test-ControlRenderer $Element $providerPid $renderer) -or
        (Get-ControlNativeChain $renderer $providerIdentity) -cne $nativeChain) { return $null }
    return $binding
  }
  $point = [DesktopActionNative+Point]::new()
  $point.X = [int][Math]::Floor($bounds.Left + $bounds.Width / 2)
  $point.Y = [int][Math]::Floor($bounds.Top + $bounds.Height / 2)
  $hit = Find-DescentHit $point
  if (-not [DesktopActionNative]::BelongsTo($hit, $providerPid) -or
      ($hit -ne $renderer -and -not [DesktopActionNative]::IsChild($renderer, $hit))) { return $null }
  $latestBounds = $Element.Current.BoundingRectangle
  $latestRect = [DesktopActionNative+Rect]::new()
  $latestGeometry = (@($latestBounds.Left, $latestBounds.Top, $latestBounds.Right, $latestBounds.Bottom) |
    ForEach-Object { ([double]$_).ToString('R', $culture) }) -join ','
  if ($latestGeometry -cne $geometry -or
      -not [DesktopActionNative]::GetWindowRect($renderer, [ref]$latestRect) -or
      "$($latestRect.Left),$($latestRect.Top),$($latestRect.Right),$($latestRect.Bottom)" -cne $rectIdentity -or
      -not (Test-Target -IdentityOnly) -or
      (Get-ProviderIdentity $providerPid) -cne $providerIdentity -or
      -not (Test-ControlRenderer $Element $providerPid $renderer) -or
      (Get-ControlNativeChain $renderer $providerIdentity) -cne $nativeChain) { return $null }
  $latestHit = Find-DescentHit $point
  if ($latestHit -ne $hit -or
      -not [DesktopActionNative]::BelongsTo($latestHit, $providerPid) -or
      ($latestHit -ne $renderer -and -not [DesktopActionNative]::IsChild($renderer, $latestHit)) -or
      -not (Test-Target -IdentityOnly) -or
      (Get-ProviderIdentity $providerPid) -cne $providerIdentity -or
      (Get-ControlNativeChain $renderer $providerIdentity) -cne $nativeChain) { return $null }
  if ($null -eq $ScanChains -and $chain -and
      (Get-ProviderChain $providerPid) -cne $chain) { return $null }
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
    [object]$ActionTicks
  )
  return [ordered]@{
    schema='pbip.desktop-action.v2'
    status=$Status
    action=$Action
    desktop_pid=$DesktopPid
    process_started_at=$ExpectedProcessStart
    root_hwnd=$RootHandle
    invoked=$Invoked
    reason=$Reason
    action_started_ticks=$ActionTicks
  }
}

function Test-Occlusion {
  param($Element)
  # One top-down snapshot: a visible Desktop or provider window above the root over the control
  # centre refuses. Foreign windows are ignored; UIA Invoke needs neither focus nor visibility.
  $bounds = $Element.Current.BoundingRectangle
  $x = [int][Math]::Floor($bounds.Left + $bounds.Width / 2)
  $y = [int][Math]::Floor($bounds.Top + $bounds.Height / 2)
  $providerPid = [uint32]$Element.Current.ProcessId
  foreach ($window in [DesktopActionNative]::TopLevelSnapshot()) {
    if ($window -eq $targetHwnd) { return $true }
    $rect = [DesktopActionNative+Rect]::new()
    if ([DesktopActionNative]::IsWindowVisible($window) -and
        ([DesktopActionNative]::BelongsTo($window, [uint32]$DesktopPid) -or
         [DesktopActionNative]::BelongsTo($window, $providerPid)) -and
        [DesktopActionNative]::GetWindowRect($window, [ref]$rect) -and
        $x -ge $rect.Left -and $x -lt $rect.Right -and $y -ge $rect.Top -and $y -lt $rect.Bottom) {
      $script:reason = 'occluded_by_desktop_window'
      return $false
    }
  }
  $script:reason = 'occlusion_walk_incomplete'
  return $false
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
  $scanProviders = @{}
  foreach ($element in $elements) {
    if ([string]$element.Current.Name -cne $Action) { continue }
    $pattern = $null
    $expand = $null
    $actionable = $element.TryGetCurrentPattern(
      [System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern) -and -not $element.TryGetCurrentPattern(
      [System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$expand)
    $ownership = Get-ControlOwnership $element $scanChains -ScanProviders $scanProviders -Prepare
    $named.Add([pscustomobject]@{
      Element=$element
      Ownership=$ownership
      Actionable=$actionable
      ProviderPid=[uint32]$element.Current.ProcessId
    })
  }
  return [pscustomobject]@{
    Controls=$named; Chains=$scanChains; Providers=$scanProviders; RootElement=$RootElement; Condition=$condition
  }
}

function Get-ActionControl {
  param($Candidates)
  # Preparation cannot prove completeness at act time: controls can appear or be renamed.
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
  $scanProviders = @{}
  # Re-read each prepared provider once per pass, not once per control.
  foreach ($candidate in $Candidates.Controls) {
    if (-not $candidate.Ownership) { continue }
    $providerPid = $candidate.ProviderPid
    if ($scanChains.ContainsKey($providerPid)) { continue }
    $freshIdentity = Get-ProviderIdentity $providerPid
    $freshChain = Get-ProviderChain $providerPid
    if (-not $freshIdentity -or $freshIdentity -cne $Candidates.Providers[$providerPid] -or
        $freshChain -cne $Candidates.Chains[$providerPid]) {
      $script:reason = 'provider_identity_changed'
      throw 'identity'
    }
    $scanChains[$providerPid] = $freshChain
    $scanProviders[$providerPid] = $freshIdentity
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
    $ownership = Get-ControlOwnership $element $scanChains -ScanProviders $scanProviders `
      -Prepare:(-not $candidate.Actionable) `
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

$invoked = $false
$actionTicks = $null
$status = 'refused'
$reason = 'target_unavailable'
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
  if (-not (Test-Occlusion $target)) { throw 'occlusion' }
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
  if ($dpiContext -ne [IntPtr]::Zero) {
    [void][DesktopActionNative]::SetThreadDpiAwarenessContext($dpiContext)
  }
}

$result = New-Result $status $invoked $reason $actionTicks
Write-Output (ConvertTo-Json -InputObject $result -Compress)
