<#
.SYNOPSIS
  Private, read-only desktop.harvest.v1 structural collector. Python owns policy and the deadline.
.DESCRIPTION
  One killable process enumerates exact-PID visible Win32 roots. No UI text, UIA trees, invocation,
  focus, Refresh, polling, persistence or gate access occurs here.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidatePattern('^[1-9][0-9]{0,9}$')][string]$DesktopPid
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class DesktopHarvest {
  public sealed class Window {
    public IntPtr Hwnd;
    public IntPtr Owner;
    public bool Visible, Minimized, Enabled, IEHosted;
    public bool? OwnerEnabled, OwnerSamePid;
    public int Width, Height;
    public string ClassName;
  }
  private delegate bool Callback(IntPtr hwnd, IntPtr param);
  [DllImport("user32.dll")] private static extern bool EnumWindows(Callback callback, IntPtr param);
  [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr hwnd, Callback callback, IntPtr param);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd, uint command);
  [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] private static extern bool IsWindowEnabled(IntPtr hwnd);
  [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
  [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd, StringBuilder value, int count);
  [StructLayout(LayoutKind.Sequential)] private struct RECT { public int Left, Top, Right, Bottom; }
  public static bool Target(IntPtr hwnd, uint expected) {
    uint actual;
    return GetWindowThreadProcessId(hwnd, out actual) != 0 && actual == expected;
  }
  private static string ClassName(IntPtr hwnd) {
    var value = new StringBuilder(256);
    if (GetClassName(hwnd, value, value.Capacity) == 0) throw new InvalidOperationException();
    return value.ToString();
  }
  private static bool IEHosted(IntPtr hwnd) {
    bool found = false;
    EnumChildWindows(hwnd, delegate(IntPtr child, IntPtr ignored) {
      if (ClassName(child) == "Internet Explorer_Server") { found = true; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
  [DllImport("kernel32.dll", SetLastError=true)] private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
  [DllImport("kernel32.dll", SetLastError=true)] private static extern uint GetProcessId(IntPtr process);
  [DllImport("kernel32.dll", SetLastError=true)] private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
  [DllImport("kernel32.dll", SetLastError=true)] private static extern bool GetProcessTimes(
    IntPtr process, out long creation, out long exit, out long kernel, out long user);
  [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
  // Same string as Win32_Process.CreationDate.ToUniversalTime().Ticks. null means only "no such process";
  // a process that exists but cannot be read throws, because the caller treats null/null as Desktop gone.
  public static string CreationStart(uint pid) {
    // QUERY_LIMITED_INFORMATION | SYNCHRONIZE: no elevation or process-modification rights.
    IntPtr handle = OpenProcess(0x101000, false, pid);
    if (handle == IntPtr.Zero) {
      int error = Marshal.GetLastWin32Error();
      if (error == 87) return null;
      throw new System.ComponentModel.Win32Exception(error);
    }
    try {
      // OpenProcess ignores a PID's low two bits, so pid+1..+3 open pid; CIM has no such alias.
      uint actual = GetProcessId(handle);
      if (actual == 0) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      if (actual != pid) return null;
      long creation, exit, kernel, user;
      uint wait = WaitForSingleObject(handle, 0);
      if (wait == 0) return null;
      if (wait != 258) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      if (!GetProcessTimes(handle, out creation, out exit, out kernel, out user))
        throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      wait = WaitForSingleObject(handle, 0);
      if (wait == 0) return null;
      if (wait != 258) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      long ticks = DateTime.FromFileTimeUtc(creation).Ticks;
      // Win32_Process.CreationDate (DMTF) preserves microseconds, not the last 100 ns digit.
      return (ticks - ticks % 10).ToString(System.Globalization.CultureInfo.InvariantCulture);
    } finally { CloseHandle(handle); }
  }
  public static List<Window> Windows(uint expected) {
    var result = new List<Window>();
    bool ok = EnumWindows(delegate(IntPtr hwnd, IntPtr ignored) {
      if (!Target(hwnd, expected) || !IsWindowVisible(hwnd)) return true;
      RECT rect;
      if (!GetWindowRect(hwnd, out rect)) throw new InvalidOperationException();
      IntPtr owner = GetWindow(hwnd, 4);
      var window = new Window {
        Hwnd=hwnd, Owner=owner, Visible=true, Enabled=IsWindowEnabled(hwnd),
        Minimized=IsIconic(hwnd), OwnerEnabled=owner == IntPtr.Zero ? (bool?)null : IsWindowEnabled(owner),
        OwnerSamePid=owner == IntPtr.Zero ? (bool?)null : Target(owner, expected),
        Width=Math.Max(0, rect.Right-rect.Left), Height=Math.Max(0, rect.Bottom-rect.Top),
        ClassName=ClassName(hwnd), IEHosted=IEHosted(hwnd)
      };
      if (!Target(hwnd, expected) || GetWindow(hwnd, 4) != owner ||
          IsWindowEnabled(hwnd) != window.Enabled) throw new InvalidOperationException();
      result.Add(window);
      return true;
    }, IntPtr.Zero);
    if (!ok) throw new InvalidOperationException();
    return result;
  }
}
"@

function Get-ProcessStart {
  return [DesktopHarvest]::CreationStart([uint32]$DesktopPid)
}
function Get-HandleString {
  param([IntPtr]$Handle)
  if ($Handle -eq [IntPtr]::Zero) { return $null }
  return '0x' + $Handle.ToInt64().ToString('x')
}

try {
  $before = Get-ProcessStart
  $windows = @()
  if ($null -ne $before) {
    foreach ($window in [DesktopHarvest]::Windows([uint32]$DesktopPid)) {
      $windows += [ordered]@{
        hwnd=(Get-HandleString $window.Hwnd); owner_hwnd=(Get-HandleString $window.Owner)
        visible=$window.Visible; minimized=$window.Minimized; enabled=$window.Enabled
        owner_enabled=$window.OwnerEnabled; owner_same_pid=$window.OwnerSamePid
        width=$window.Width; height=$window.Height
        class_name=$window.ClassName; ie_hosted=$window.IEHosted
      }
    }
  }
  $after = Get-ProcessStart
  [ordered]@{
    schema='desktop.harvest.v1'; pid=$DesktopPid
    process_started_at_before=$before; process_started_at_after=$after
    complete=$true; reason='complete'; windows=$windows
  } | ConvertTo-Json -Depth 8 -Compress
  exit 0
} catch {
  # Never echo native exception messages or private environment paths.
  exit 4
}
