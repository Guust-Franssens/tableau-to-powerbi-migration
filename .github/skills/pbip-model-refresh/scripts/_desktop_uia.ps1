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
function Get-ElementIdentity {
  param($Element, $Root, [IntPtr]$Handle)
  $facts = [Collections.Generic.List[string]]::new()
  $native = [IntPtr]::Zero
  $elementPid = $null
  $node = $Element
  $reachedRoot = $false
  for ($depth = 0; $depth -lt 64 -and $null -ne $node; $depth++) {
    $runtime = $node.GetRuntimeId()
    if ($null -eq $runtime -or $runtime.Length -eq 0) { throw 'missing runtime identity' }
    # Current.ProcessId defaults to zero. Never confuse that with the NotSupported sentinel.
    # https://learn.microsoft.com/dotnet/api/system.windows.automation.automationelement.processidproperty
    $provider = $node.GetCurrentPropertyValue([Windows.Automation.AutomationElement]::ProcessIdProperty, $true)
    $unsupported = [object]::ReferenceEquals($provider, [Windows.Automation.AutomationElement]::NotSupported)
    if (-not $unsupported -and (($provider -isnot [int]) -or $provider -lt 0)) { throw 'invalid provider identity' }
    if ($depth -eq 0) { $elementPid = if ($unsupported) { 0 } else { $provider } }
    $nodeHandle = [IntPtr]::new([long]$node.Current.NativeWindowHandle)
    $start = if (-not $unsupported -and $provider -gt 0) { [DesktopHarvest]::ProcessStart($provider) } else { '-' }
    $facts.Add(($runtime -join ',') + ":$unsupported`:$(if ($unsupported) { '-' } else { $provider })`:$start`:$nodeHandle")
    if ($native -eq [IntPtr]::Zero -and $nodeHandle -ne [IntPtr]::Zero) {
      # A missing/zero PID on a virtual node inherits only a supported, live native anchor.
      $anchorPid = [uint32]0
      if ($unsupported -or $provider -le 0 -or
          [DesktopHarvest]::GetWindowThreadProcessId($nodeHandle, [ref]$anchorPid) -eq 0 -or
          $anchorPid -ne $provider -or ($elementPid -gt 0 -and $elementPid -ne $anchorPid)) {
        throw 'unattested native provider'
      }
      $native = $nodeHandle
    }
    if ([Windows.Automation.Automation]::Compare($node, $Root)) {
      if ($unsupported -or $provider -ne [uint32]$DesktopPid -or $nodeHandle -ne $Handle) { throw 'root identity' }
      $reachedRoot = $true
      break
    }
    $node = [Windows.Automation.TreeWalker]::RawViewWalker.GetParent($node)
  }
  if (-not $reachedRoot -or $native -eq [IntPtr]::Zero) { throw 'missing root ancestry' }
  for ($depth = 0; $depth -lt 64 -and $native -ne [IntPtr]::Zero; $depth++) {
    $nativePid = [uint32]0
    $thread = [DesktopHarvest]::GetWindowThreadProcessId($native, [ref]$nativePid)
    if ($thread -eq 0 -or $nativePid -eq 0) { throw 'missing native identity' }
    $facts.Add("$native`:$thread`:$nativePid`:$([DesktopHarvest]::ProcessStart($nativePid))")
    if ($native -eq $Handle) {
      if ($nativePid -ne [uint32]$DesktopPid) { throw 'native root identity' }
      return $facts -join '|'
    }
    $native = [DesktopHarvest]::ChildParent($native)
  }
  throw 'missing native containment'
}
function Get-AutomationHarvest {
  param([IntPtr]$Handle)
  $items = [Collections.Generic.List[object]]::new()
  $truncated = $false
  $incomplete = $false
  try {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($Handle)
    if ($null -eq $root -or $root.Current.ProcessId -ne [uint32]$DesktopPid -or
        $root.Current.NativeWindowHandle -ne $Handle.ToInt64() -or $root.Current.IsPassword) {
      throw 'target'
    }
    $rootIdentity = Get-ElementIdentity $root $root $Handle
    $descendants = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($element in $descendants) {
      if ($script:remaining -le 0) { $truncated = $true; break }
      $script:remaining--
      try {
        $current = $element.Current
        $identity = Get-ElementIdentity $element $root $Handle
        $role = [string]$current.ControlType.ProgrammaticName
        # Editable/password content is neither read nor considered complete evidence.
        if ($current.IsPassword -or $role -in @('ControlType.Edit', 'ControlType.ComboBox')) {
          $incomplete = $true
          continue
        }
        $document = ''
        if ($role -ne 'ControlType.Button') {
          $pattern = $null
          if ($element.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pattern)) {
            $readOnly = $pattern.DocumentRange.GetAttributeValue([System.Windows.Automation.TextPattern]::IsReadOnlyAttribute)
            if (($readOnly -isnot [bool]) -or -not $readOnly) { $incomplete = $true; continue }
            $document = $pattern.DocumentRange.GetText(8001)
            if ($document.Length -gt 8000) { $truncated = $true; continue }
          }
        }
        $name = [string]$current.Name
        if ($name.Length -gt 8000) { $truncated = $true; continue }
        if ($current.ProcessId -eq 0 -and -not $name -and -not $document) {
          $incomplete = $true
          continue
        }
        if ((Get-ElementIdentity $element $root $Handle) -cne $identity -or
            (Get-ElementIdentity $root $root $Handle) -cne $rootIdentity) { throw 'identity changed' }
        if ($name) { $items.Add([ordered]@{Text=$name; Role=$role; Source='Name'}) }
        if ($document) { $items.Add([ordered]@{Text=[string]$document; Role=$role; Source='TextPattern'}) }
      } catch { $incomplete = $true }
    }
    if ((Get-ElementIdentity $root $root $Handle) -cne $rootIdentity) { throw 'root changed' }
  } catch { $incomplete = $true }
  return [ordered]@{items=@($items.ToArray()); truncated=$truncated; patterns_incomplete=$incomplete}
}

try {
  $before = Get-ProcessStart
  $script:remaining = $HarvestMaxElements
  $windows = @()
  if ($null -ne $before) {
    foreach ($window in [DesktopHarvest]::Windows([uint32]$DesktopPid)) {
      $targetBefore = [DesktopHarvest]::Target($window.Hwnd, [uint32]$DesktopPid)
      $ownerBefore = [DesktopHarvest]::GetWindow($window.Hwnd, 4)
      $harvest = Get-AutomationHarvest $window.Hwnd
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
  }
  $after = Get-ProcessStart
  $payload = [ordered]@{schema='desktop.harvest.v1'; pid=$DesktopPid
    process_started_at_before=$before; process_started_at_after=$after
    complete=$true; reason='complete'; windows=@($windows)}
  $json = ConvertTo-Json $payload -Depth 8 -Compress
  if ([Text.Encoding]::UTF8.GetByteCount($json) -gt 131072) { exit 4 }
  Write-Output $json
} catch { exit 4 }
