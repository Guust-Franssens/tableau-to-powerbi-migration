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

.PARAMETER LoadDetectorsOnly
  Dot-source seam for the test suite: define the pure window classifiers, then return WITHOUT loading
  UI Automation, compiling the Win32 shim, or touching any process. The classifiers are the part that
  decides a hard stop, so they must be exercisable against synthesised windows on a machine with no
  Power BI Desktop. Not for interactive use.

.OUTPUTS
  A single final `VERDICT:` line, and an exit code in three bands:

    exit 1  CREDENTIAL_MISSING   a window's text matched the credential signature. THIS IS THE HARD
                                 STOP - a human must sign in once; no automation can fill it.
    exit 0  CREDENTIAL_PRESENT   a refresh was invoked and ran to the deadline with no credential
                                 modal and no unclassifiable dialog. Still not the gate of record for
                                 a serverless source - confirm with the one-row data probe.
    exit 3  UNKNOWN              no window for the pid, a minimized owner, or no Refresh control was
                                 ever invoked (with nothing invoked, "no modal appeared" proves
                                 nothing - reporting PRESENT would be a fail-open arbiter).
    exit 3  REFRESH_IN_PROGRESS  a refresh progress dialog was ALREADY up at t=0. Desktop is busy, so
                                 the credential state cannot be probed; wait for it, or cancel the
                                 stale refresh. Do not stack a second refresh on top of it.
    exit 3  DIALOG_UNRECOGNIZED  a dialog is up whose text matched NEITHER signature. We read it and
                                 it is not a credential prompt - so it is not a credential wall - but
                                 we cannot say what it is. A human should look at the screen.
    exit 3  DIALOG_UNREADABLE    a dialog is up whose CONTENT could not be read - either it exposes no
                                 text at all, or its caption matched the progress signature while its
                                 content stayed unread. Distinct from DIALOG_UNRECOGNIZED on purpose:
                                 "we could not read it" is a weaker state of knowledge than "we read
                                 it and it did not match", and the two must not collapse into one.

  ⚠️ `BLOCKED_BY_DIALOG` is deliberately NOT emitted here any more (issue #367). It used to be, on a
  size-only test - ANY visible non-main window >=100x100 - which a Power BI Refresh progress dialog
  satisfies trivially; a field report on 2026-08-28 caught it reporting exactly that under three
  concurrent refreshes. This script has no way to establish that a dialog is *blocking*, so every
  `BLOCKED_BY_DIALOG` it emitted was an inference dressed as a finding, in the one verdict class the
  toolkit treats as a hard stop. The Python fast check (`_credential_modal.blocking_dialog_candidates`)
  still emits that token on its own path; this arbiter now names what it actually observed instead.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/probe_desktop_credential.ps1 -DesktopPid 42532
#>
[CmdletBinding(DefaultParameterSetName = 'Probe')]
param(
  [Parameter(Mandatory = $true, ParameterSetName = 'Probe')][int]$DesktopPid,
  [Parameter(ParameterSetName = 'Probe')][int]$TimeoutSec = 75,
  [Parameter(Mandatory = $true, ParameterSetName = 'Detectors')][switch]$LoadDetectorsOnly
)

# --------------------------------------------------------------------------------------------------
# Pure detectors. No Win32, no UI Automation, no process access - they take window objects and return
# a classification, so `-LoadDetectorsOnly` can dot-source this far and the suite can drive them with
# synthesised windows. Everything below the `if ($LoadDetectorsOnly) { return }` line needs a live
# Desktop and is therefore only reachable in a real probe run.
# --------------------------------------------------------------------------------------------------

# Connector credential-dialog signature text (covers Databricks / SQL / Snowflake / generic OAuth).
# Shared with the Python t=0/poll detector so the fast path and this arbiter cannot drift.
$sig = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'credential_modal_signature.regex') -Raw).Trim()

# Progress-dialog text. Matching this means "Power BI is working", NOT "a human is needed".
# ⚠️ Provenance: INFERRED from Power BI Desktop's refresh UI, not measured against a live capture -
# see SKILL.md. It is deliberately not load-bearing: a miss downgrades REFRESH_IN_PROGRESS to
# DIALOG_UNRECOGNIZED (both exit 3, neither a credential stop), so a wrong guess costs specificity,
# never correctness. Keep the alternatives narrow and anchored - a broad pattern here is the one way
# this file could hide a genuine blocker.
$benignSig = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'benign_dialog_signature.regex') -Raw).Trim()

function Get-NormalizedText {
  <# Whitespace-normalised, de-duplicated window text, optionally plus a JOINED variant.

  The joined variant exists because a WPF dialog splits one sentence across several visual elements,
  so `Enter your credentials` can arrive as `Enter your` + `credentials` and match neither fragment.

  ⚠️ It is applied ASYMMETRICALLY, and that is the whole point: the credential signature searches the
  joined text (broadest possible recall for the detector that CONVICTS), while the benign signature
  searches the individual elements only (narrowest possible reach for the detector that EXONERATES).
  Joining can in principle manufacture a phrase - two adjacent table names reading `Account` `Key`
  would join to the signature `Account Key` - and the direction that error takes matters: on the
  credential path it produces a LOUD false stop a human resolves by looking at the screen, whereas on
  the benign path it would produce a SILENT false clear. Never give the benign path the joined text.
  #>
  param([object[]]$Texts, [switch]$IncludeJoined)
  $clean = @()
  foreach ($t in $Texts) {
    if ($null -eq $t) { continue }
    $normalized = (([string]$t) -replace '\s+', ' ').Trim()
    if ($normalized -and ($clean -notcontains $normalized)) { $clean += $normalized }
  }
  if ($IncludeJoined -and $clean.Count -gt 1) { $clean += ($clean -join ' ') }
  return $clean
}

function Test-CredentialModal {
  <# First matching credential-signature text across EVERY window, any class, any size. #>
  param([object[]]$Windows)
  foreach ($w in $Windows) {
    foreach ($n in (Get-NormalizedText -Texts $w.Texts -IncludeJoined)) {
      if ($n -match $sig) { return $n }
    }
  }
  return $null
}

function Select-DialogCandidate {
  <# Windows big enough, and of the right class, to be worth CLASSIFYING.

  Size selects what to look at; it is not itself evidence of anything (issue #367). The old
  `Test-BlockingDialog` returned the first window past this filter as a blocking dialog, which is why
  a Refresh progress dialog read as a credential wall.
  #>
  param([object[]]$Windows)
  $candidates = @()
  foreach ($w in $Windows) {
    if ($w.ClassName -and $w.ClassName.StartsWith('WindowsForms10.Window.8')) { continue }
    if ($w.Width -lt 100 -or $w.Height -lt 100) { continue }
    $candidates += $w
  }
  return $candidates
}

function Get-DialogClassification {
  <# Classify ONE candidate window from what it says, and from whether it disables its owner.

  Kinds, in the order they are tested:
    credential        text matched the credential signature -> the hard stop. Searched over the JOINED
                      text too, so a signature split across WPF elements still convicts.
    benign            a text ELEMENT matched the progress signature AND the window's content was
                      actually read -> Power BI is working.
    benign-title-only the progress signature matched, but `ContentRead` is not `$true`: the caption
                      says "Refresh" and the CONTENT was never read. Reported as indeterminate, not
                      suppressed. This is the review defect of 2026-08-29: a WPF modal titled
                      `Refresh` whose visual content read `Enter your credentials` was classified
                      benign from its caption, suppressed in flight, and the probe exited 0 -
                      a SILENT false negative on a hard stop, strictly worse than the false positive
                      #367 set out to remove. `absent != empty`, one level down: content we did not
                      read cannot be evidence of harmlessness.
    non-blocking      the owner window is ENABLED. Modality is a ONE-WAY test: a modal dialog disables
                      its owner, so an enabled owner PROVES this window is not blocking anything. The
                      converse does not hold - Power BI's refresh dialog also disables the owner - so a
                      disabled owner is never used to convict. `$null` (no owner) proves nothing.
    unreadable        no readable text at all: we could not classify it.
    unrecognized      readable text that matched neither signature: we looked, and it is not a
                      credential prompt. Distinct from `unreadable` on purpose - absent is not empty.
  #>
  param([Parameter(Mandatory = $true)][object]$Window)

  $elements = Get-NormalizedText -Texts $Window.Texts
  $searchable = Get-NormalizedText -Texts $Window.Texts -IncludeJoined
  foreach ($t in $searchable) {
    if ($t -match $sig) { return [pscustomobject]@{ Kind = 'credential'; Evidence = $t } }
  }
  foreach ($t in $elements) {
    if ($t -match $benignSig) {
      # `$null`/missing ContentRead fails SAFE: a window whose collector never said it read the
      # content is treated as unread, so a synthesised or future window shape cannot silently
      # re-open the suppression path.
      if ($Window.ContentRead -eq $true) { return [pscustomobject]@{ Kind = 'benign'; Evidence = $t } }
      return [pscustomobject]@{ Kind = 'benign-title-only'; Evidence = $t }
    }
  }
  if ($Window.OwnerEnabled -eq $true) {
    return [pscustomobject]@{ Kind = 'non-blocking'; Evidence = 'owner window is enabled' }
  }
  if ($elements.Count -eq 0) {
    return [pscustomobject]@{ Kind = 'unreadable'; Evidence = '' }
  }
  return [pscustomobject]@{ Kind = 'unrecognized'; Evidence = $elements[0] }
}

function Format-DialogEvidence {
  <# One-line window description for a verdict line. #>
  param([object]$Window)
  $title = if ($Window.Title) { $Window.Title } else { '(empty title)' }
  $read = if ($Window.ContentRead -eq $true) { 'read' } else { 'UNREAD' }
  return ("class={0} title='{1}' size={2}x{3} content={4}" -f $Window.ClassName, $title, $Window.Width, $Window.Height, $read)
}

function Get-DialogVerdict {
  <# Fold per-window classifications into ONE verdict, or `$null` when nothing needs reporting.

  Precedence is credential > unreadable > unrecognized > benign, and it is not arbitrary:

    * credential is the only terminal finding, so it short-circuits.
    * `benign` is the only classification carrying POSITIVE evidence of harmlessness, so it must never
      outrank a window we could not classify - otherwise one progress dialog masks a real modal.
    * `unreadable` outranks `unrecognized` because it is the weaker state of knowledge, and the weaker
      state is the one that must stay visible.
    * `benign-title-only` joins the `unreadable` band, NOT the benign one. Its content was never read,
      so it is an indeterminate wearing a reassuring caption - and it is the one shape that must not be
      suppressible, because suppression is what turned a real credential modal into exit 0.

  `-RefreshInFlight` is set only inside the poll loop, i.e. after THIS script invoked the refresh.
  There, a progress dialog is our own and is ignored. At t=0 it is somebody else's, and stacking a
  second refresh on it is exactly what the 2026-08-28 field report had to unpick by hand.
  #>
  param([object[]]$Windows, [switch]$RefreshInFlight)

  $unreadable = $null
  $unrecognized = $null
  $benign = $null
  foreach ($w in @(Select-DialogCandidate -Windows $Windows)) {
    $c = Get-DialogClassification -Window $w
    $found = [pscustomobject]@{ Kind = $c.Kind; Verdict = ''; ExitCode = 3; Window = $w; Evidence = $c.Evidence }
    switch ($c.Kind) {
      'credential' {
        $found.Verdict = 'CREDENTIAL_MISSING'
        $found.ExitCode = 1
        return $found
      }
      'unreadable' { if ($null -eq $unreadable) { $found.Verdict = 'DIALOG_UNREADABLE'; $unreadable = $found } }
      'benign-title-only' { if ($null -eq $unreadable) { $found.Verdict = 'DIALOG_UNREADABLE'; $unreadable = $found } }
      'unrecognized' { if ($null -eq $unrecognized) { $found.Verdict = 'DIALOG_UNRECOGNIZED'; $unrecognized = $found } }
      'benign' { if ($null -eq $benign) { $found.Verdict = 'REFRESH_IN_PROGRESS'; $benign = $found } }
      default { }
    }
  }
  if ($null -ne $unreadable) { return $unreadable }
  if ($null -ne $unrecognized) { return $unrecognized }
  if ($null -ne $benign -and -not $RefreshInFlight) { return $benign }
  return $null
}

if ($LoadDetectorsOnly) { return }

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class Win32CredentialWindows {
  public sealed class WindowInfo {
    public IntPtr Hwnd;
    public string Title = "";
    public string ClassName = "";
    public int Width;
    public int Height;
    public bool Minimized;
    public bool? OwnerEnabled;
    public List<string> Texts = new List<string>();
  }

  private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr hWndParent, EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] private static extern bool IsWindowEnabled(IntPtr hWnd);
  [DllImport("user32.dll")] private static extern IntPtr GetWindow(IntPtr hWnd, uint uCmd);
  [DllImport("user32.dll")] private static extern int GetWindowTextLength(IntPtr hWnd);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr hWnd, StringBuilder className, int count);
  [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);

  [StructLayout(LayoutKind.Sequential)]
  private struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

  private static string Text(IntPtr hWnd) {
    int length = GetWindowTextLength(hWnd);
    if (length <= 0) { return ""; }
    var builder = new StringBuilder(length + 1);
    GetWindowText(hWnd, builder, builder.Capacity);
    return builder.ToString();
  }

  private static string ClassNameOf(IntPtr hWnd) {
    var builder = new StringBuilder(256);
    GetClassName(hWnd, builder, builder.Capacity);
    return builder.ToString();
  }

  public static List<WindowInfo> GetPidWindows(int pid) {
    const uint GW_OWNER = 4;
    var windows = new List<WindowInfo>();
    EnumWindows(delegate (IntPtr hWnd, IntPtr lParam) {
      uint ownerPid;
      GetWindowThreadProcessId(hWnd, out ownerPid);
      if (ownerPid != (uint)pid || !IsWindowVisible(hWnd)) { return true; }
      RECT rect;
      GetWindowRect(hWnd, out rect);
      IntPtr owner = GetWindow(hWnd, GW_OWNER);
      var info = new WindowInfo {
        Hwnd = hWnd,
        Title = Text(hWnd),
        ClassName = ClassNameOf(hWnd),
        Width = Math.Max(0, rect.Right - rect.Left),
        Height = Math.Max(0, rect.Bottom - rect.Top),
        Minimized = IsIconic(hWnd),
        // null when the window has NO owner: "we could not apply the test" must stay distinct from
        // "the test said the owner is disabled". Only `true` is ever acted on (see
        // Get-DialogClassification) - modality can exonerate a window, never convict one.
        OwnerEnabled = (owner == IntPtr.Zero) ? (bool?)null : IsWindowEnabled(owner)
      };
      if (!String.IsNullOrEmpty(info.Title)) { info.Texts.Add(info.Title); }
      EnumChildWindows(hWnd, delegate (IntPtr child, IntPtr childParam) {
        string text = Text(child);
        if (!String.IsNullOrEmpty(text)) { info.Texts.Add(text); }
        return true;
      }, IntPtr.Zero);
      windows.Add(info);
      return true;
    }, IntPtr.Zero);
    return windows;
  }
}
"@

$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $DesktopPid)

function Get-AutomationText {
  <# UI Automation descendant Name/Value text for one HWND, or `$null` if the harvest FAILED.

  Win32 `EnumChildWindows` is not enough and this is not a corner case: WPF renders its visual tree
  into ONE HWND, so a WPF dialog's entire content is invisible to child-HWND enumeration and only its
  caption survives. Measured 2026-08-29 in review - an owned WPF modal titled `Refresh` containing
  `Enter your credentials` read as a progress dialog and the probe exited 0. UI Automation sees that
  text; a UIA dump of the same window returned `DescendantNames: Enter your credentials`.

  Three distinct outcomes, kept distinct on purpose: `$null` = the harvest could not run, `@()` = it
  ran and the window genuinely exposes nothing, a populated array = content. The caller turns the
  first two into "content unread", which is an indeterminate, never a clean bill of health.
  #>
  param([long]$Hwnd, [int]$MaxElements = 400)
  $texts = @()
  try {
    $element = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
    if ($null -eq $element) { return $null }
    $descendants = $element.FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition)
    $seen = 0
    foreach ($d in $descendants) {
      if ($seen -ge $MaxElements) { break }
      $seen++
      try {
        if ($d.Current.Name) { $texts += $d.Current.Name }
        $pattern = $null
        if ($d.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
          if ($pattern.Current.Value) { $texts += $pattern.Current.Value }
        }
      } catch {
        # One unreachable element must not discard the text already harvested from its siblings.
        continue
      }
    }
  } catch {
    return $null
  }
  return , $texts
}

function ConvertTo-ProbeWindow {
  <# Win32 `WindowInfo` -> the plain window object the pure classifiers consume.

  `ContentRead` is the field the classifiers gate the benign path on: it is true only when text was
  obtained that is NOT merely the window caption - a Win32 child text (WinForms dialogs expose those)
  or a UIA descendant. A caption on its own can never establish that a dialog is harmless.
  #>
  param([object]$Window, [switch]$Enrich)
  $title = [string]$Window.Title
  $texts = @()
  foreach ($t in $Window.Texts) { if ($t) { $texts += [string]$t } }
  $contentRead = @($texts | Where-Object { $_ -ne $title }).Count -gt 0
  if ($Enrich) {
    $hwnd = if ($Window.Hwnd -is [IntPtr]) { $Window.Hwnd.ToInt64() } else { [long]$Window.Hwnd }
    $harvested = Get-AutomationText -Hwnd $hwnd
    if ($null -ne $harvested) {
      foreach ($t in $harvested) { if ($t -and ($texts -notcontains $t)) { $texts += [string]$t } }
      if (@($harvested | Where-Object { $_ -and $_ -ne $title }).Count -gt 0) { $contentRead = $true }
    }
  }
  return [pscustomobject]@{
    Hwnd         = $Window.Hwnd
    Title        = $title
    ClassName    = [string]$Window.ClassName
    Width        = [int]$Window.Width
    Height       = [int]$Window.Height
    Minimized    = [bool]$Window.Minimized
    OwnerEnabled = $Window.OwnerEnabled
    Texts        = $texts
    ContentRead  = $contentRead
  }
}

function Get-PidWindows {
  # EVERY visible top-level/owned window of the target process, not UIA RootElement children. A
  # Power BI credential modal is an owned window; UIA root-child discovery misses it.
  #
  # The UIA text harvest is applied to CANDIDATE windows only. Walking the main Power BI window's
  # visual tree would cost seconds per poll for text the classifiers never read (it is excluded by
  # class), and this loop runs every 2s.
  $enriched = @()
  foreach ($w in [Win32CredentialWindows]::GetPidWindows($DesktopPid)) {
    $isCandidate = @(Select-DialogCandidate -Windows @($w)).Count -eq 1
    $enriched += (ConvertTo-ProbeWindow -Window $w -Enrich:$isCandidate)
  }
  return $enriched
}

$windows = Get-PidWindows
if (-not $windows -or $windows.Count -eq 0) { Write-Output "no window for pid $DesktopPid found"; Write-Output "VERDICT: UNKNOWN"; exit 3 }

# 1. If a credential modal is ALREADY open, the credential is missing - report immediately.
$hit = Test-CredentialModal -Windows $windows
if ($hit) {
  Write-Output ("credential modal already open: '{0}'" -f $hit.Substring(0, [Math]::Min(80, $hit.Length)))
  Write-Output "VERDICT: CREDENTIAL_MISSING"
  exit 1
}

# 1b. A dialog is up that is not a credential prompt. It is NOT a credential wall - say what it is and
# exit 3 (cannot probe), never exit 1 (human needed). Invoking a Refresh on top of an unclassified
# dialog is how the 2026-08-28 field report ended up with a stale duplicate refresh to cancel.
$blocker = Get-DialogVerdict -Windows $windows
if ($blocker) {
  Write-Output ("dialog already open: {0}" -f (Format-DialogEvidence -Window $blocker.Window))
  if ($blocker.Evidence) {
    Write-Output ("  matched text: '{0}'" -f $blocker.Evidence.Substring(0, [Math]::Min(80, $blocker.Evidence.Length)))
  }
  switch ($blocker.Kind) {
    'benign' { Write-Output "  a refresh is already running on this pid - wait for it, or cancel the stale one; do not stack a second refresh on it" }
    'benign-title-only' { Write-Output "  its CAPTION looks like a progress dialog, but its content could not be read - an unread window is not evidence that it is harmless; look at the Desktop screen" }
    'unreadable' { Write-Output "  this window exposes no readable text, so it could not be classified at all - look at the Desktop screen" }
    default { Write-Output "  its text matches no credential-prompt signature, so this is not a credential wall - look at the Desktop screen" }
  }
  Write-Output ("VERDICT: {0}" -f $blocker.Verdict)
  exit $blocker.ExitCode
}
foreach ($w in $windows) {
  if ($w.Minimized -and $w.ClassName.StartsWith('WindowsForms10.Window.8')) {
    Write-Output "Power BI Desktop owner window is minimized; owned modal dialogs are hidden from enumeration"
    Write-Output "VERDICT: UNKNOWN"
    exit 3
  }
}

# 2. Otherwise trigger a refresh and watch for the modal (generous timeout for warehouse cold-start).
# Search every top-level window for an invokable 'Refresh', not just the first.
$invoked = $false
foreach ($w in $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)) {
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
# From here on a progress dialog is OUR refresh, so it is ignored (-RefreshInFlight). An
# unreadable/unrecognized dialog is LATCHED rather than acted on: it is not a credential prompt, so it
# must not abort a healthy refresh, but it also means "no modal appeared" is no longer established, so
# it must not be erased by a quiet deadline either. Latching is the same shape the Python detector uses
# for its indeterminate states, and it is what keeps the two failure directions both closed.
$latched = $null
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 2000
  $windows = Get-PidWindows
  $hit = Test-CredentialModal -Windows $windows
  if ($hit) {
    Write-Output ("credential modal detected: '{0}'" -f $hit.Substring(0, [Math]::Min(80, $hit.Length)))
    Write-Output "VERDICT: CREDENTIAL_MISSING"
    exit 1
  }
  $observed = Get-DialogVerdict -Windows $windows -RefreshInFlight
  if ($observed -and $null -eq $latched) { $latched = $observed }
}
if ($latched) {
  Write-Output ("a dialog was up during the refresh: {0}" -f (Format-DialogEvidence -Window $latched.Window))
  Write-Output "  it matched no credential-prompt signature, so this is NOT a credential wall and no sign-in is implied"
  Write-Output "  but a window we could not classify was open the whole time, so 'no modal appeared' is not established"
  Write-Output ("VERDICT: {0}" -f $latched.Verdict)
  exit $latched.ExitCode
}
Write-Output "no credential modal within ${TimeoutSec}s (refresh proceeded)"
Write-Output "VERDICT: CREDENTIAL_PRESENT"
exit 0
