<#
.SYNOPSIS
  Private, read-only desktop.harvest.v1 fact collector. Python owns policy and the absolute deadline.
.DESCRIPTION
  One killable process harvests exact-PID Win32 windows and role-bearing UIA text. No classification,
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
    $descendants = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($element in $descendants) {
      if ($script:remaining -le 0) { $truncated = $true; break }
      $script:remaining--
      try {
        $current = $element.Current
        if ($current.ProcessId -ne [uint32]$DesktopPid) { $incomplete = $true; continue }
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
        if ($name) { $items.Add([ordered]@{Text=$name; Role=$role; Source='Name'}) }
        if ($document) { $items.Add([ordered]@{Text=[string]$document; Role=$role; Source='TextPattern'}) }
      } catch { $incomplete = $true }
    }
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
