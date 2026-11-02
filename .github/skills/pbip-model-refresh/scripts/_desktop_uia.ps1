<#
.SYNOPSIS
  Private, read-only desktop.harvest.v1 fact collector. Python owns policy and the absolute deadline.
.DESCRIPTION
  One killable process harvests exact-PID Win32 roots and native-root-attested UIA text. No classification,
  regex, invocation, focus, Refresh, polling, persistence or gate access occurs here. Text is private
  pipe data, never a public diagnostic. The caller kills/reaps this process if a provider blocks.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidatePattern('^[1-9][0-9]{0,9}$')][string]$DesktopPid,
  [ValidateRange(1, 4000)][int]$HarvestMaxElements = 4000
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class DesktopHarvest {
  public sealed class Window {
    public IntPtr Hwnd;
    public IntPtr Owner;
    public bool Visible, Minimized, Enabled;
    public bool? OwnerEnabled;
    public int Width, Height;
    public string Title;
  }
  private delegate bool Callback(IntPtr hwnd, IntPtr param);
  [DllImport("user32.dll")] private static extern bool EnumWindows(Callback callback, IntPtr param);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd, uint command);
  [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
  [DllImport("user32.dll", EntryPoint="GetWindowLongW")] private static extern int GetWindowStyle(IntPtr hwnd, int index);
  [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] private static extern bool IsWindowEnabled(IntPtr hwnd);
  [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
  [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr hwnd, StringBuilder value, int count);
  [StructLayout(LayoutKind.Sequential)] private struct RECT { public int Left, Top, Right, Bottom; }
  public static bool Target(IntPtr hwnd, uint expected) {
    uint actual;
    return GetWindowThreadProcessId(hwnd, out actual) != 0 && actual == expected;
  }
  public static bool Enabled(IntPtr hwnd) { return IsWindowEnabled(hwnd); }
  public static IntPtr ChildParent(IntPtr hwnd) {
    // GA_PARENT alone also walks a top-level window; only genuine WS_CHILD links attest containment.
    return (GetWindowStyle(hwnd, -16) & 0x40000000) != 0 ? GetAncestor(hwnd, 1) : IntPtr.Zero;
  }
  public static string ProcessStart(uint pid) {
    if (pid == 0) throw new InvalidOperationException();
    using (var process = System.Diagnostics.Process.GetProcessById(checked((int)pid))) {
      if (process.HasExited) throw new InvalidOperationException();
      return process.StartTime.ToUniversalTime().Ticks.ToString(System.Globalization.CultureInfo.InvariantCulture);
    }
  }
  public static List<Window> Windows(uint expected) {
    var result = new List<Window>();
    bool ok = EnumWindows(delegate(IntPtr hwnd, IntPtr ignored) {
      if (!Target(hwnd, expected) || !IsWindowVisible(hwnd)) return true;
      RECT rect;
      if (!GetWindowRect(hwnd, out rect)) throw new InvalidOperationException();
      var text = new StringBuilder(8002);
      GetWindowText(hwnd, text, text.Capacity);
      IntPtr owner = GetWindow(hwnd, 4);
      result.Add(new Window { Hwnd=hwnd, Owner=owner, Visible=true, Enabled=IsWindowEnabled(hwnd),
        Minimized=IsIconic(hwnd), OwnerEnabled=owner == IntPtr.Zero ? (bool?)null : IsWindowEnabled(owner),
        Width=Math.Max(0, rect.Right-rect.Left), Height=Math.Max(0, rect.Bottom-rect.Top), Title=text.ToString() });
      return true;
    }, IntPtr.Zero);
    if (!ok) throw new InvalidOperationException();
    return result;
  }
}
"@

function Get-ProcessStart {
  $process = Get-CimInstance Win32_Process -Filter "ProcessId=$DesktopPid"
  if ($null -eq $process) { return $null }
  return $process.CreationDate.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
}
function Get-HandleString {
  param([IntPtr]$Handle)
  if ($Handle -eq [IntPtr]::Zero) { return $null }
  return '0x' + $Handle.ToInt64().ToString('x')
}
function Get-IdentitySnapshot {
  param($Root, [IntPtr]$Handle, [hashtable]$WindowRoots = @{})
  if ($WindowRoots.Count) {
    $currentRoots = [DesktopHarvest]::Windows([uint32]$DesktopPid)
    if ($currentRoots.Count -ne $WindowRoots.Count) { throw 'native root set changed' }
    $seen = @{}
    foreach ($window in $currentRoots) {
      if ($seen.ContainsKey($window.Hwnd) -or -not $WindowRoots.ContainsKey($window.Hwnd) -or
          $WindowRoots[$window.Hwnd] -ne $window.Owner) { throw 'native root set changed' }
      $seen[$window.Hwnd] = $true
    }
  }
  # Cache only identity metadata, never Name/Value/Text. Each pass has fresh native/process bindings.
  # Raw Subtree keeps ALL descendants, including the frame; window/classification selection stays in Python.
  # https://learn.microsoft.com/dotnet/framework/ui-automation/caching-in-ui-automation-clients
  $request = [Windows.Automation.CacheRequest]::new()
  $request.TreeScope = [Windows.Automation.TreeScope]::Subtree
  $request.TreeFilter = [Windows.Automation.Condition]::TrueCondition
  foreach ($property in @(
      [Windows.Automation.AutomationElement]::RuntimeIdProperty,
      [Windows.Automation.AutomationElement]::ProcessIdProperty,
      [Windows.Automation.AutomationElement]::NativeWindowHandleProperty)) { $request.Add($property) }
  $cached = $Root.GetUpdatedCache($request)
  $entries = @{}
  $nodes = [Collections.Generic.List[object]]::new()
  $starts = @{}
  $nativeFacts = @{}
  $pending = [Collections.Generic.Stack[object]]::new()
  $pending.Push(@{node=$cached; parent=$null; anchor=[IntPtr]::Zero; anchor_pid=0; depth=0})
  while ($pending.Count) {
    $visit = $pending.Pop()
    $node = $visit.node
    if ($visit.depth -ge 64 -or $entries.Count -gt $script:remaining) { throw 'identity bound' }
    $runtime = $node.GetCachedPropertyValue([Windows.Automation.AutomationElement]::RuntimeIdProperty, $true)
    if ($runtime -isnot [int[]] -or $runtime.Length -eq 0) { throw 'missing runtime identity' }
    $key = $runtime -join ','
    if ($entries.ContainsKey($key)) { throw 'duplicate runtime identity' }
    $provider = $node.GetCachedPropertyValue([Windows.Automation.AutomationElement]::ProcessIdProperty, $true)
    # A zero integer is distinct from unsupported: the latter still refuses BEFORE any private read.
    if ($provider -isnot [int] -or $provider -lt 0) { throw 'invalid provider identity' }
    if ($provider -gt 0 -and -not $starts.ContainsKey($provider)) {
      $starts[$provider] = [DesktopHarvest]::ProcessStart($provider)
    }
    $nodeHandle = [IntPtr]::new([long]$node.GetCachedPropertyValue(
      [Windows.Automation.AutomationElement]::NativeWindowHandleProperty))
    # UIA raw children can include owned TOP-LEVEL windows. Their separately enumerated root owns
    # their harvest; ownership never authorizes reading them through this root's WS_CHILD boundary.
    if ($nodeHandle -ne $Handle -and $WindowRoots.ContainsKey($nodeHandle)) {
      $owner = $WindowRoots[$nodeHandle]
      $nativePid = [uint32]0
      $thread = [DesktopHarvest]::GetWindowThreadProcessId($nodeHandle, [ref]$nativePid)
      if ($null -eq $visit.parent -or $owner -ne $Handle -or $thread -eq 0 -or
          $nativePid -ne [uint32]$DesktopPid -or $provider -ne [int]$nativePid -or
          [DesktopHarvest]::ChildParent($nodeHandle) -ne [IntPtr]::Zero -or
          [DesktopHarvest]::GetWindow($nodeHandle, 4) -ne $owner) { throw 'changed separate root' }
      $entries[$key] = "$($visit.parent)`:$provider`:$($starts[$provider])`:$nodeHandle`:separate`:$owner`:$thread"
      continue
    }
    $anchor = $visit.anchor
    $anchorPid = $visit.anchor_pid
    if ($nodeHandle -ne [IntPtr]::Zero) {
      if (-not $nativeFacts.ContainsKey($nodeHandle)) {
        $facts = [Collections.Generic.List[string]]::new()
        $native = $nodeHandle
        $anchorPid = 0
        $reached = $false
        for ($depth = 0; $depth -lt 64 -and $native -ne [IntPtr]::Zero; $depth++) {
          $nativePid = [uint32]0
          $thread = [DesktopHarvest]::GetWindowThreadProcessId($native, [ref]$nativePid)
          if ($thread -eq 0 -or $nativePid -eq 0) { throw 'missing native identity' }
          if ($depth -eq 0) { $anchorPid = $nativePid }
          if (-not $starts.ContainsKey([int]$nativePid)) {
            $starts[[int]$nativePid] = [DesktopHarvest]::ProcessStart($nativePid)
          }
          $facts.Add("$native`:$thread`:$nativePid`:$($starts[[int]$nativePid])")
          if ($native -eq $Handle) {
            if ($nativePid -ne [uint32]$DesktopPid) { throw 'native root identity' }
            $reached = $true
            break
          }
          $native = [DesktopHarvest]::ChildParent($native)
        }
        if (-not $reached) { throw 'missing native containment' }
        $nativeFacts[$nodeHandle] = @{pid=$anchorPid; identity=($facts -join '|')}
      }
      $anchorPid = $nativeFacts[$nodeHandle].pid
      if ($provider -le 0 -or $provider -ne $anchorPid) { throw 'unattested native provider' }
      $anchor = $nodeHandle
    }
    if ($anchor -eq [IntPtr]::Zero -or ($provider -gt 0 -and $provider -ne $anchorPid)) {
      throw 'unattested native provider'
    }
    if ($null -eq $visit.parent -and ($provider -ne [uint32]$DesktopPid -or $nodeHandle -ne $Handle)) {
      throw 'root identity'
    }
    $start = if ($provider -gt 0) { $starts[$provider] } else { '-' }
    $entries[$key] = "$($visit.parent)`:$provider`:$start`:$nodeHandle`:$anchor`:$($nativeFacts[$anchor].identity)"
    if ($null -ne $visit.parent) { $nodes.Add(@{element=$node; key=$key; provider=$provider}) }
    foreach ($child in $node.CachedChildren) {
      $parent = $child.CachedParent
      if ($null -ne $parent) {
        $parentRuntime = $parent.GetCachedPropertyValue(
          [Windows.Automation.AutomationElement]::RuntimeIdProperty, $true)
        if ($parentRuntime -isnot [int[]] -or ($parentRuntime -join ',') -cne $key) { throw 'raw parent changed' }
      }
      $pending.Push(@{node=$child; parent=$key; anchor=$anchor; anchor_pid=$anchorPid; depth=($visit.depth+1)})
    }
  }
  return @{entries=$entries; nodes=$nodes}
}
function Get-AutomationHarvest {
  param([IntPtr]$Handle, [hashtable]$WindowRoots = @{})
  $items = [Collections.Generic.List[object]]::new()
  $truncated = $false
  $incomplete = $false
  try {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($Handle)
    if ($null -eq $root -or $root.Current.ProcessId -ne [uint32]$DesktopPid -or
        $root.Current.NativeWindowHandle -ne $Handle.ToInt64() -or $root.Current.IsPassword) {
      throw 'target'
    }
    $snapshot = Get-IdentitySnapshot $root $Handle $WindowRoots
    $limit = $script:remaining
    foreach ($entry in $snapshot.nodes) {
      if ($script:remaining -le 0) { $truncated = $true; break }
      $script:remaining--
      try {
        $element = $entry.element
        $current = $element.Current
        $role = [string]$current.ControlType.ProgrammaticName
        # Editable/password content is neither read nor considered complete evidence.
        if ($current.IsPassword -or $role -in @('ControlType.Edit', 'ControlType.ComboBox')) {
          $incomplete = $true
          continue
        }
        $document = ''
        if ($role -ne 'ControlType.Button') {
          $provider = $element.GetCurrentPropertyValue(
            [Windows.Automation.AutomationElement]::ProcessIdProperty, $true)
          if ($provider -isnot [int] -or $provider -lt 0 -or $provider -ne $entry.provider) {
            throw 'invalid live provider identity'
          }
          $pattern = $null
          if ($element.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pattern)) {
            $range = $pattern.DocumentRange
            $readOnly = $range.GetAttributeValue([System.Windows.Automation.TextPattern]::IsReadOnlyAttribute)
            if (($readOnly -isnot [bool]) -or -not $readOnly) { $incomplete = $true; continue }
            $document = $range.GetText(8001)
            if ($document.Length -gt 8000) { $truncated = $true; continue }
          }
        }
        # Cached attestation cannot authorize a private read after live PID support changes.
        $provider = $element.GetCurrentPropertyValue(
          [Windows.Automation.AutomationElement]::ProcessIdProperty, $true)
        if ($provider -isnot [int] -or $provider -lt 0 -or $provider -ne $entry.provider) {
          throw 'invalid live provider identity'
        }
        $name = [string]$current.Name
        if ($name.Length -gt 8000) { $truncated = $true; continue }
        if ($current.ProcessId -eq 0 -and -not $name -and -not $document) {
          $incomplete = $true
          continue
        }
        if ($name) { $items.Add([ordered]@{Text=$name; Role=$role; Source='Name'}) }
        if ($document) { $items.Add([ordered]@{Text=[string]$document; Role=$role; Source='TextPattern'}) }
      } catch { $incomplete = $true }
    }
    $remaining = $script:remaining
    try {
      $script:remaining = $limit
      $after = Get-IdentitySnapshot $root $Handle $WindowRoots
      if ($snapshot.entries.Count -ne $after.entries.Count) { throw 'tree changed' }
      foreach ($key in $snapshot.entries.Keys) {
        if (-not $after.entries.ContainsKey($key) -or $snapshot.entries[$key] -cne $after.entries[$key]) {
          throw 'identity changed'
        }
      }
    } finally { $script:remaining = $remaining }
  } catch {
    if ($_.Exception.Message -eq 'identity bound') { $truncated = $true }
    $incomplete = $true
    $items.Clear()
  }
  return [ordered]@{items=@($items.ToArray()); truncated=$truncated; patterns_incomplete=$incomplete}
}

try {
  $before = Get-ProcessStart
  $script:remaining = $HarvestMaxElements
  $windows = @()
  if ($null -ne $before) {
    $nativeWindows = [DesktopHarvest]::Windows([uint32]$DesktopPid)
    $windowRoots = @{}
    foreach ($window in $nativeWindows) {
      if ($windowRoots.ContainsKey($window.Hwnd)) { throw 'duplicate native root' }
      $windowRoots[$window.Hwnd] = $window.Owner
    }
    foreach ($window in $nativeWindows) {
      $targetBefore = [DesktopHarvest]::Target($window.Hwnd, [uint32]$DesktopPid)
      $ownerBefore = [DesktopHarvest]::GetWindow($window.Hwnd, 4)
      $harvest = Get-AutomationHarvest $window.Hwnd $windowRoots
      if ([DesktopHarvest]::Enabled($window.Hwnd) -ne $window.Enabled) { throw 'target state changed' }
      $ownerEnabled = $window.OwnerEnabled
      if ($window.Owner -ne [IntPtr]::Zero -and [DesktopHarvest]::Enabled($window.Owner) -ne $ownerEnabled) {
        $ownerEnabled = $null
        $harvest.patterns_incomplete = $true
      }
      $windows += [ordered]@{
        hwnd=(Get-HandleString $window.Hwnd); owner_hwnd=(Get-HandleString $window.Owner)
        target_before=$targetBefore; target_after=[DesktopHarvest]::Target($window.Hwnd, [uint32]$DesktopPid)
        owner_before=(Get-HandleString $ownerBefore)
        owner_after=(Get-HandleString ([DesktopHarvest]::GetWindow($window.Hwnd, 4)))
        visible=$window.Visible; minimized=$window.Minimized; enabled=$window.Enabled
        owner_enabled=$ownerEnabled; width=$window.Width; height=$window.Height
        title=$window.Title; texts=@(); items=$harvest.items
        truncated=$harvest.truncated; patterns_incomplete=$harvest.patterns_incomplete
      }
    }
    # No omitted subtree is accepted unless its root remains independently enumerated and harvested.
    $finalWindows = [DesktopHarvest]::Windows([uint32]$DesktopPid)
    if ($finalWindows.Count -ne $windowRoots.Count) { throw 'native root set changed' }
    $seenRoots = @{}
    foreach ($window in $finalWindows) {
      if ($seenRoots.ContainsKey($window.Hwnd) -or -not $windowRoots.ContainsKey($window.Hwnd) -or
          $windowRoots[$window.Hwnd] -ne $window.Owner) { throw 'native root set changed' }
      $seenRoots[$window.Hwnd] = $true
    }
  }
  $after = Get-ProcessStart
  $payload = [ordered]@{schema='desktop.harvest.v1'; pid=$DesktopPid
    process_started_at_before=$before; process_started_at_after=$after
    complete=$true; reason='complete'; windows=@($windows)}
  $json = ConvertTo-Json $payload -Depth 8 -Compress
  if ([Text.Encoding]::UTF8.GetByteCount($json) -gt 131072) { exit 4 }
  Write-Output $json
} catch { exit 4 }
