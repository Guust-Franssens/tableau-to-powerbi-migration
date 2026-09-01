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

.PARAMETER HarvestTimeoutSec
  Wall-clock cap on ONE window-text harvest. 0 (default) derives it from -TimeoutSec, clamped to 2..8s.

.PARAMETER HarvestMaxElements
  Cap on UI Automation elements inspected per window. Hitting it marks the harvest TRUNCATED, which is
  a distinct outcome from "read it all and found nothing" - see -LoadDetectorsOnly's note on proof.

.PARAMETER LoadDetectorsOnly
  Dot-source seam for the test suite: define the collector helpers and the decision seam, then return
  WITHOUT loading UI Automation, compiling the Win32 shim, or touching any process. `Invoke-DialogDecision`
  is the part that reaches the verdict, so it must be exercisable against synthesised windows on a
  machine with no Power BI Desktop. Not for interactive use.

.PARAMETER HarvestHwnd
  Internal. Re-invokes THIS script as a short-lived child process that harvests one window's UI
  Automation text and prints it as JSON. `AutomationElement.FindAll` is a synchronous cross-process
  call: a hung provider cannot be cancelled in-process, and neither `try/catch` nor a background
  thread helps. A child process can be killed, which is the only way this probe can promise a verdict
  within a bounded time (measured in review 2026-08-29: a blocked provider held a `-TimeoutSec 1` run
  for 15.1s and produced NO verdict at all).

.OUTPUTS
  A single final `VERDICT:` line, and an exit code in three bands:

    exit 1  CREDENTIAL_MISSING   a window's text matched the credential signature. THIS IS THE HARD
                                 STOP - a human must sign in once; no automation can fill it.
    exit 0  CREDENTIAL_PRESENT   a refresh was invoked and ran to the deadline with no credential
                                 modal and nothing unclassifiable up. Still not the gate of record for
                                 a serverless source - confirm with the one-row data probe.
    exit 3  UNKNOWN              no window for the pid, a minimized owner, or no Refresh control was
                                 ever invoked (with nothing invoked, "no modal appeared" proves
                                 nothing - reporting PRESENT would be a fail-open arbiter).
    exit 3  REFRESH_IN_PROGRESS  a dialog whose CONTENT positively reads as refresh progress - all of
                                 it, not just its first element - was already up at t=0. Desktop is
                                 busy, so the credential state cannot be probed; wait for it, or
                                 cancel the stale refresh.
    exit 3  DIALOG_NEEDS_HUMAN   a KNOWN human-blocking prompt that is not a credential prompt - the
                                 native database query approval modal above all. Not exit 1, because
                                 the remedy is an approval, not a sign-in. Never suppressed, and it
                                 outranks any progress text in the same window.
    exit 3  DIALOG_UNRECOGNIZED  a dialog is up whose text matched NO signature, or which shows
                                 progress text ALONGSIDE content that is neither progress status nor
                                 enumerated chrome. We read it and it is not a credential prompt - so
                                 it is not a credential wall - but we cannot account for all of it. A
                                 human should look at the screen.
    exit 3  DIALOG_UNREADABLE    a dialog is up that could not be shown to be harmless: no text at
                                 all, or only a reassuring CAPTION, or benign-looking content read
                                 from an INCOMPLETE harvest. "We could not establish it" is a weaker
                                 state of knowledge than "we read it and it did not match", and the
                                 two must not collapse into one verdict. The evidence line names WHY
                                 the harvest stopped (`truncated` / `patterns-incomplete` /
                                 `no-payload` / `bad-schema`) and how many texts were read - measured,
                                 a cap truncation and a killed provider were otherwise indis-
                                 tinguishable, and they need opposite responses.

  ⚠️ `BLOCKED_BY_DIALOG` is deliberately NOT emitted here any more (issue #367). It used to be, on a
  size-only test - ANY visible non-main window >=100x100 - which a Power BI Refresh progress dialog
  satisfies trivially; a field report on 2026-08-28 caught it reporting exactly that under three
  concurrent refreshes. This script has no way to establish that a dialog is *blocking*, so every
  `BLOCKED_BY_DIALOG` it emitted was an inference dressed as a finding, in the one verdict class the
  toolkit treats as a hard stop. The Python fast check (`_credential_modal.blocking_dialog_candidates`)
  still emits that token on its own path; this arbiter now names what it actually observed instead.

  ⚠️ THE BURDEN OF PROOF RUNS ONE WAY: a dialog is suppressed only when we POSITIVELY READ benign
  CONTENT. It is never suppressed because we read *something* and the caption looked reassuring. The
  first two attempts at #367 both got this wrong, and the second was worse than the first:

    attempt 1  benign by CAPTION. A WPF modal captioned `Refresh` whose content read
               `Enter your credentials` was suppressed -> CREDENTIAL_PRESENT, exit 0.
    attempt 2  benign by caption + "we harvested SOME text" (`ContentRead`). A `Cancel` button was
               enough to satisfy that, so the same modal cleared again whenever the credential text
               itself was missed - via `TextPattern`-only content, past the element cap, or split by an
               interposed element. Three separate exploits, one root cause: `ContentRead` was a PROXY
               for "we read the credential-bearing content", and no proxy can carry that weight.
    attempt 3  benign by "everything left over is SHORT" (`$MinPromptWords = 5`, issue #406). A window
               reading `Refresh` + `Evaluating` + `Please enter your password` classified `benign` and
               was suppressed to NOTHING under -RefreshInFlight; `Password:` too. Size was the proxy
               this time, and it failed for the same reason the other two did.

  ⚠️ **CREDENTIAL_PRESENT IS NARROWER BECAUSE OF THAT THIRD FIX, AND THAT IS THE INTENDED TRADE.**
  Closing #406 means any content element that is neither recognised progress status nor enumerated
  chrome VETOES suppression. If Power BI's own refresh dialog exposes bare table names (`Orders`,
  `Customers`), this probe now latches DIALOG_UNRECOGNIZED (exit 3) where it used to reach
  CREDENTIAL_PRESENT (exit 0). That capability is worth less than it looks: `CREDENTIAL_PRESENT` is
  documented here and in docs/data-source-credentials.md as NOT the gate of record - the one-row data
  probe is - and it is already untrustworthy on its own against a serverless source. Losing it costs
  an extra loud exit 3; keeping the amnesty cost a SILENT suppressed password prompt. ⚠️ Whether the
  real dialog lists table names is INFERRED, never measured here (no Desktop in this corpus), so the
  cost may be zero in practice - but it is written down rather than assumed away either way.

  There is no reliable way to prove a UIA harvest saw everything - `LegacyIAccessiblePattern` is not
  even exposed by the managed `System.Windows.Automation` API (verified 2026-08-29: the type is
  missing, it is UIA-COM only), so some MSAA-bridged content is structurally unreadable from here.
  That is exactly why completeness is no longer claimed. Under this rule a coverage gap costs RECALL on
  the credential path (exit 3 instead of exit 1 - loud, and a human looks at the screen) and can never
  produce a silent clear. Master's size-only behaviour was accidentally right for the same reason: it
  never trusted text it had not read.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/probe_desktop_credential.ps1 -DesktopPid 42532
#>
[CmdletBinding(DefaultParameterSetName = 'Probe')]
param(
  [Parameter(Mandatory = $true, ParameterSetName = 'Probe')][int]$DesktopPid,
  [Parameter(ParameterSetName = 'Probe')][int]$TimeoutSec = 75,
  [Parameter(ParameterSetName = 'Probe')][int]$HarvestTimeoutSec = 0,
  [Parameter(ParameterSetName = 'Probe')]
  [Parameter(ParameterSetName = 'Harvest')][int]$HarvestMaxElements = 2000,
  [Parameter(Mandatory = $true, ParameterSetName = 'Detectors')][switch]$LoadDetectorsOnly,
  [Parameter(Mandatory = $true, ParameterSetName = 'Harvest')][long]$HarvestHwnd
)

# --------------------------------------------------------------------------------------------------
# COLLECTOR-ONLY SECTION. No Win32, no UI Automation, no process access - so `-LoadDetectorsOnly` can
# dot-source this far and the suite can drive the real decision seam with synthesised windows.
# Everything below the `if ($LoadDetectorsOnly) { return }` line needs a live Desktop and is therefore
# only reachable in a real probe run.
# --------------------------------------------------------------------------------------------------

# The four shared signature resources.
#
# ⚠️ **This script no longer READS any of them for judgement** - `decide_dialog.py` does, and that is
# the whole of issue #417. What it still does is fail CLOSED when one is missing: a collector shipped
# without the vocabulary its decider needs cannot produce a verdict at all, and naming the missing file
# here is far cheaper to diagnose than the `DIALOG_UNREADABLE` it would otherwise degrade to. The list
# is also the contract - `test_the_arbiter_and_the_python_detector_share_one_vocabulary` mutates
# `benign_chrome_signature.regex` in a scratch copy of this folder and requires BOTH entry paths to
# flip together, which is only possible because there is exactly one reader.
#
#   credential_modal_signature.regex  connector sign-in prompt text (Databricks / SQL / Snowflake / OAuth)
#   benign_dialog_signature.regex     refresh PROGRESS text: "Power BI is working", never "a human is
#                                     needed", and only when it matches CONTENT, never the caption alone
#   blocking_prompt_signature.regex   prompts KNOWN to need a human but NOT a sign-in - the native
#                                     database query approval modal above all
#   benign_chrome_signature.regex     an ENUMERATED whole-element allowlist (`Cancel`/`OK`/`Close`)
#
# ⚠️ The last two files are the ONLY things that can cause a dialog to be ignored. Keep them TINY and
# anchored: every alternative added is a string that can never again veto a suppression. The chrome
# allowlist replaced `$MinPromptWords = 5` (issue #406), which excused every unmatched content element
# under five words - so `Refresh` + `Evaluating` + `Please enter your password` classified `benign` and
# was SUPPRESSED under -RefreshInFlight, and `Password:` (two words) with it. Length is not evidence; a
# positive claim about specific strings is.
foreach ($resource in @(
    'credential_modal_signature.regex',
    'benign_dialog_signature.regex',
    'blocking_prompt_signature.regex',
    'benign_chrome_signature.regex'
  )) {
  $resourcePath = Join-Path $PSScriptRoot $resource
  if (-not (Test-Path -LiteralPath $resourcePath)) {
    throw "missing shared signature resource: $resourcePath - the decider cannot classify without it"
  }
}

# --------------------------------------------------------------------------------------------------
# ⚠️ THE DECISION LIVES IN PYTHON NOW (issue #417). `Test-CredentialModal`, `Select-DialogCandidate`,
# `Get-MainFrame`, `Test-RendersNothing`, `Get-DialogClassification`, `Get-DialogTextSet`,
# `Get-NormalizedText` and `Test-HarvestComplete` USED to be here, re-implementing `_credential_modal.py`.
# Two independent divergences survived a review round aimed specifically at removing divergence:
#
#   * a title veto applied to THIS half only - an owned dialog titled `Password:` with benign body
#     content gave DIALOG_UNRECOGNIZED (exit 3) here and CREDENTIAL_PRESENT (exit 0) in Python, i.e.
#     the gate of record silently clearing a sign-in modal;
#   * case-sensitivity differing at the LANGUAGE level: PowerShell's `-ne`/`-notcontains` are
#     case-INSENSITIVE, Python's `==`/`in` are not, so title `Refresh` + body `REFRESH` gave
#     DIALOG_UNREADABLE (exit 3) here and CREDENTIAL_PRESENT (exit 0) there.
#
# The second is the reason this is a seam and not a third alignment pass: a language-level difference
# cannot be fixed once, so every future string comparison would be a fresh chance to reintroduce it.
# This script now COLLECTS - Win32 attributes plus UI Automation text, which Python cannot read - and
# forwards them to `decide_dialog.py`, which JUDGES. There is one implementation, so there is no drift.
# --------------------------------------------------------------------------------------------------

function Resolve-PythonExe {
  <# The interpreter that runs the decider. Fails CLOSED - a missing Python is an INDETERMINATE probe,
  never a clean one, so callers must treat $null as "could not decide". #>
  if ($script:PythonExe) { return $script:PythonExe }
  foreach ($candidate in @($env:PBIP_REFRESH_PYTHON, 'python', 'python3', 'py')) {
    if (-not $candidate) { continue }
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $script:PythonExe = $found.Source; return $script:PythonExe }
  }
  return $null
}

function Invoke-DialogDecision {
  <# Forward collected windows to `decide_dialog.py` and return its verdict record.

  THE ONLY decision path (issue #417). Fails CLOSED: any failure to reach or parse the decider yields
  the indeterminate band, never a clear. "We could not decide" must never read as "no modal appeared".
  #>
  param([object[]]$Windows, [switch]$RefreshInFlight, [switch]$CandidatesOnly)

  $exe = Resolve-PythonExe
  if (-not $exe) {
    return [pscustomobject]@{
      Verdict = 'DIALOG_UNREADABLE'; Kind = 'unreadable'; ExitCode = 3
      Evidence = 'no Python interpreter found to run decide_dialog.py (set PBIP_REFRESH_PYTHON)'
      Guidance = 'the dialog state could not be established at all - look at the Desktop screen'
      Candidates = 0; Credential = $null; Window = $null; CandidateHwnds = @()
    }
  }
  $payload = [System.IO.Path]::GetTempFileName()
  try {
    # NOT `-AsArray` - that is PowerShell 7+, and this ships against Windows PowerShell 5.1 where it
    # is a parameter-binding error that silently degraded every decision to DIALOG_UNREADABLE.
    # A single-element array serialises as a bare object here; the decider normalises that.
    (ConvertTo-Json -InputObject @($Windows) -Depth 6) | Set-Content -LiteralPath $payload -Encoding UTF8
    $decider = Join-Path $PSScriptRoot 'decide_dialog.py'
    $argv = @($decider, '--windows', $payload)
    if ($RefreshInFlight) { $argv += '--in-flight' }
    if ($CandidatesOnly) { $argv += '--candidates-only' }
    $raw = & $exe @argv 2>&1
    $line = @($raw | Where-Object { "$_" -like 'DECISION:*' })
    if ($line.Count -eq 0) {
      return [pscustomobject]@{
        Verdict = 'DIALOG_UNREADABLE'; Kind = 'unreadable'; ExitCode = 3
        Evidence = "decider produced no verdict: $raw"
        Guidance = 'the dialog state could not be established at all - look at the Desktop screen'
        Candidates = 0; Credential = $null; Window = $null; CandidateHwnds = @()
      }
    }
    $parsed = ConvertFrom-Json ("$($line[0])".Substring(9))
    return [pscustomobject]@{
      Verdict    = $parsed.verdict
      Kind       = $parsed.kind
      ExitCode   = [int]$parsed.exit_code
      Evidence   = $parsed.evidence
      # The operator's next step is decided WITH the verdict, never re-derived here: a `switch` over
      # the same kinds in this file is precisely the divergence surface issue #417 closed.
      Guidance   = $parsed.guidance
      Candidates = [int]$parsed.candidates
      Credential = $parsed.credential
      Window     = $parsed.window
      CandidateHwnds = @($parsed.candidate_hwnds)
    }
  }
  catch {
    return [pscustomobject]@{
      Verdict = 'DIALOG_UNREADABLE'; Kind = 'unreadable'; ExitCode = 3
      Evidence = "decider failed: $_"
      Guidance = 'the dialog state could not be established at all - look at the Desktop screen'
      Candidates = 0; Credential = $null; Window = $null; CandidateHwnds = @()
    }
  }
  finally { Remove-Item -LiteralPath $payload -Force -ErrorAction SilentlyContinue }
}

function ConvertTo-HarvestResult {
  <# Validate a harvest child's payload before any of it is believed.

  Round 3's MEDIUM, and the third instance on this branch of MISSING EVIDENCE READ AS GOOD EVIDENCE:
  the parent checked only that the payload was valid JSON, then computed
  `(-not $p.Truncated) -and (-not $p.PatternsIncomplete)`. A missing property is `$null`, and
  `-not $null` is `$true`, so a well-formed-but-schema-incomplete payload became
  `HarvestComplete = $true` - a real Boolean, which then sailed through the strict Boolean guard
  downstream because the coercion had already happened upstream of it.

  Both flags must EXIST and be actual Booleans, and the child must have exited 0. Items are still
  merged when they are present and the flags are not - unread text lowers credential recall, so
  keeping it costs nothing and can only help - but `Complete` stays `$false`, so a malformed payload
  can never authorise suppression.

  `Reason` names WHY a harvest was not complete, and it exists because the single token `INCOMPLETE`
  was measured to be ambiguous in a way that hid a real regression (issue #406 follow-up). Forcing a
  cap truncation (`-HarvestMaxElements 400` against a 451-element modal) printed output BYTE-IDENTICAL
  to a contention-killed harvest: same `harvest=INCOMPLETE`, same `VERDICT: DIALOG_UNREADABLE`, same
  exit 3. Those two need opposite responses - one is a defect in the element-cap logic, the other is a
  busy machine - so a test that skips on `INCOMPLETE` would silently stop being able to fail for its
  own subject.

  ⚠️ `Reason` is DIAGNOSTIC ONLY. Nothing may branch a verdict on it: `Complete` remains the single
  strict Boolean that governs suppression, and since issue #417 its ONLY reader is
  `decide_dialog._window_from`, which accepts a real `$true` and collapses every other shape - absent,
  `$null`, `0`, `1`, `'true'`, `''` - to "the read did not report itself finished". A reason that could
  grant the right to suppress would be the fourth instance of the proxy mistake this script has already
  made three times.
  #>
  param([object]$Payload, [int]$ExitCode)

  if ($ExitCode -ne 0 -or $null -eq $Payload) { return $null }
  $properties = $Payload.PSObject.Properties
  $hasTruncated = $null -ne $properties['Truncated']
  $hasPatterns = $null -ne $properties['PatternsIncomplete']
  $truncated = if ($hasTruncated) { $Payload.Truncated } else { $null }
  $patterns = if ($hasPatterns) { $Payload.PatternsIncomplete } else { $null }
  $schemaOk = $hasTruncated -and $hasPatterns -and ($truncated -is [bool]) -and ($patterns -is [bool])
  $items = @()
  if ($null -ne $properties['Items'] -and $null -ne $Payload.Items) { $items = @($Payload.Items) }
  $complete = $schemaOk -and (-not $truncated) -and (-not $patterns)
  $reason = 'complete'
  if (-not $schemaOk) { $reason = 'bad-schema' }
  elseif ($truncated -and $patterns) { $reason = 'truncated+patterns-incomplete' }
  elseif ($truncated) { $reason = 'truncated' }
  elseif ($patterns) { $reason = 'patterns-incomplete' }
  return [pscustomobject]@{ Items = $items; Complete = [bool]$complete; Reason = $reason }
}

function Format-DialogEvidence {
  <# One-line description of the window the DECIDER reported on.

  `harvest=` carries the REASON, not just the fact, and `items=` the count actually read. Measured:
  with only `complete|INCOMPLETE`, a cap truncation and a contention-killed provider were
  indistinguishable, and they need opposite responses.
  #>
  param([object]$Window)
  if ($null -eq $Window) { return '(no window reported)' }
  $title = if ($Window.Title) { $Window.Title } else { '(empty title)' }
  $harvest = if ($Window.HarvestComplete -is [bool] -and $Window.HarvestComplete) {
    'complete'
  }
  elseif ($Window.HarvestReason) { [string]$Window.HarvestReason }
  else { 'INCOMPLETE' }
  return ("class={0} title='{1}' size={2}x{3} harvest={4} items={5}" -f
    $Window.ClassName, $title, $Window.Width, $Window.Height, $harvest, $Window.Texts)
}

if ($LoadDetectorsOnly) { return }

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase

# Control types whose text labels an ACTION rather than forming prose. Excluded from the prose join
# only - their text is still searched element-by-element and still counts as content.
$InteractiveControlTypes = @(
  'Button', 'CheckBox', 'RadioButton', 'ComboBox', 'MenuItem', 'TabItem',
  'ListItem', 'Hyperlink', 'SplitButton', 'TreeItem', 'ScrollBar', 'Thumb'
)

function Get-AutomationHarvest {
  <# Every text-bearing UI Automation property under one window, plus HOW COMPLETE the read was.

  Win32 `EnumChildWindows` is not enough and this is not a corner case: WPF renders its visual tree
  into ONE HWND, so a WPF dialog's entire content is invisible to child-HWND enumeration and only its
  caption survives.

  Three text-bearing sources are read: `Name`, `ValuePattern`, and `TextPattern`. `TextPattern` is not
  optional - review 2026-08-29 built a modal whose credential text lived in a read-only `RichTextBox`
  with an EMPTY `Name`, reachable only through `TextPattern`.
  `LegacyIAccessiblePattern` is deliberately absent: the type does not exist in the managed
  `System.Windows.Automation` API (verified - it is UIA-COM only), so MSAA-bridged-only content cannot
  be read from here at all. That gap is survivable ONLY because completeness is never assumed:
  `Truncated`/`PatternsIncomplete` withhold the right to suppress, they do not grant it.
  #>
  param([long]$Hwnd, [int]$MaxElements = 2000)

  $items = @()
  $truncated = $false
  $incomplete = $false
  try {
    $element = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
    if ($null -eq $element) { return $null }
    $descendants = $element.FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition)
  } catch {
    return $null
  }
  $seen = 0
  foreach ($d in $descendants) {
    if ($seen -ge $MaxElements) { $truncated = $true; break }
    $seen++
    $typeName = ''
    try { $typeName = [string]$d.Current.ControlType.ProgrammaticName } catch { $incomplete = $true }
    $isInteractive = $false
    foreach ($known in $InteractiveControlTypes) {
      if ($typeName -like "*.$known") { $isInteractive = $true; break }
    }
    $texts = @()
    try { if ($d.Current.Name) { $texts += [string]$d.Current.Name } } catch { $incomplete = $true }
    try {
      $valuePattern = $null
      if ($d.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$valuePattern)) {
        if ($valuePattern.Current.Value) { $texts += [string]$valuePattern.Current.Value }
      }
    } catch { $incomplete = $true }
    try {
      $textPattern = $null
      if ($d.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$textPattern)) {
        $document = $textPattern.DocumentRange.GetText(8000)
        if ($document) { $texts += [string]$document }
      }
    } catch { $incomplete = $true }
    foreach ($t in $texts) {
      $items += [pscustomobject]@{ Text = $t; Interactive = $isInteractive }
    }
  }
  return [pscustomobject]@{ Items = $items; Truncated = $truncated; PatternsIncomplete = $incomplete }
}

if ($PSCmdlet.ParameterSetName -eq 'Harvest') {
  # Child-process mode. Kept above the Win32 `Add-Type` so the child compiles nothing it does not need
  # - this runs once per candidate per poll.
  $harvested = Get-AutomationHarvest -Hwnd $HarvestHwnd -MaxElements $HarvestMaxElements
  if ($null -eq $harvested) { exit 4 }
  Write-Output ('HARVEST:' + (ConvertTo-Json $harvested -Compress -Depth 5))
  exit 0
}

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
    // `GetWindow(hwnd, GW_OWNER)`. Needed as well as `OwnerEnabled` so ownership can be walked
    // TRANSITIVELY to the unowned root, which is how the frame is identified (issue #406 review,
    // finding 2). Without the handle the arbiter could only ask "does it have an owner", which is not
    // enough to tell an application frame from a dialog.
    public IntPtr OwnerHwnd;
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
        OwnerHwnd = owner,
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

# One harvest's wall-clock cap. Derived from the caller's own budget rather than fixed, so a
# deliberately short probe cannot be held open by a wedged provider for an order of magnitude longer
# than it asked for. The probe's total is therefore always finite: (cap x polls) + TimeoutSec.
$harvestBudget = if ($HarvestTimeoutSec -gt 0) { $HarvestTimeoutSec } else { [Math]::Max(2, [Math]::Min(8, $TimeoutSec)) }

function Get-BoundedAutomationHarvest {
  <# `Get-AutomationHarvest` in a KILLABLE child process, or `$null` if it did not finish in time.

  A hung UIA provider blocks `FindAll` inside a cross-process COM call. `try/catch` cannot interrupt
  that, and neither can a background thread - the call is not cancellable. Killing a child process is.
  #>
  param([long]$Hwnd, [int]$TimeoutSec, [int]$MaxElements)

  $outFile = [System.IO.Path]::GetTempFileName()
  try {
    $exe = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    $argv = @(
      '-NoProfile', '-ExecutionPolicy', 'Bypass',
      '-File', ('"{0}"' -f $PSCommandPath),
      '-HarvestHwnd', $Hwnd,
      '-HarvestMaxElements', $MaxElements
    )
    $child = Start-Process -FilePath $exe -ArgumentList $argv -NoNewWindow -PassThru -RedirectStandardOutput $outFile
    if (-not $child.WaitForExit($TimeoutSec * 1000)) {
      try { $child.Kill() } catch { }
      return $null
    }
    $raw = Get-Content -LiteralPath $outFile -Raw -ErrorAction SilentlyContinue
    if (-not $raw) { return $null }
    $line = @($raw -split "`r?`n" | Where-Object { $_ -like 'HARVEST:*' })
    if ($line.Count -eq 0) { return $null }
    $payload = $null
    try { $payload = ConvertFrom-Json $line[0].Substring(8) } catch { return $null }
    # Valid JSON is not a valid payload. Schema and exit code are checked before anything is believed.
    return ConvertTo-HarvestResult -Payload $payload -ExitCode $child.ExitCode
  } catch {
    return $null
  } finally {
    Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue
  }
}

function ConvertTo-ProbeWindow {
  <# Win32 `WindowInfo` (+ optional UIA harvest) -> the plain object `decide_dialog.py` consumes. #>
  param([object]$Window, [switch]$Enrich, [int]$TimeoutSec = 8, [int]$MaxElements = 2000)

  $title = [string]$Window.Title
  $texts = @()
  foreach ($t in $Window.Texts) { if ($t) { $texts += [string]$t } }
  $interactive = @()
  $complete = $false
  # Distinct from every payload-level reason: `$null` back from the bounded harvest means the child
  # never delivered a believable payload at all - killed on timeout, crashed, or unparseable - so no
  # UIA text reached this window and only its Win32 caption survives. A loaded machine produces this,
  # and it must not read the same as "we read it and it was cut off" (issue #406 follow-up).
  $reason = 'not-attempted'
  if ($Enrich) {
    $reason = 'no-payload'
    $hwnd = if ($Window.Hwnd -is [IntPtr]) { $Window.Hwnd.ToInt64() } else { [long]$Window.Hwnd }
    $harvested = Get-BoundedAutomationHarvest -Hwnd $hwnd -TimeoutSec $TimeoutSec -MaxElements $MaxElements
    if ($null -ne $harvested) {
      foreach ($item in @($harvested.Items)) {
        if (-not $item.Text) { continue }
        $texts += [string]$item.Text
        if ($item.Interactive) { $interactive += [string]$item.Text }
      }
      $complete = [bool]$harvested.Complete
      $reason = [string]$harvested.Reason
    }
  }
  return [pscustomobject]@{
    Hwnd             = $Window.Hwnd
    Title            = $title
    ClassName        = [string]$Window.ClassName
    Width            = [int]$Window.Width
    Height           = [int]$Window.Height
    Minimized        = [bool]$Window.Minimized
    OwnerEnabled     = $Window.OwnerEnabled
    Texts            = $texts
    InteractiveTexts = $interactive
    HarvestComplete  = [bool]$complete
    HarvestReason    = $reason
  }
}

function Get-PidWindows {
  # EVERY visible top-level/owned window of the target process, not UIA RootElement children. A
  # Power BI credential modal is an owned window; UIA root-child discovery misses it.
  #
  # The UIA harvest is applied to CANDIDATE windows only. Walking the main Power BI window's visual
  # tree would cost seconds per poll for text the classifiers never read (it is excluded by class).
  $bare = @()
  foreach ($w in [Win32CredentialWindows]::GetPidWindows($DesktopPid)) {
    $bare += (ConvertTo-ProbeWindow -Window $w)
  }
  # Ask the DECIDER which windows are worth a UIA harvest. Enriching everything would walk the main
  # Desktop window's visual tree every poll (seconds); deciding here would re-create the divergence
  # issue #417 removed. So the judgement stays in one place and this only acts on the answer.
  $wanted = @{}
  foreach ($h in @((Invoke-DialogDecision -Windows $bare -CandidatesOnly).CandidateHwnds)) { $wanted[[long]$h] = $true }
  $enriched = @()
  foreach ($w in [Win32CredentialWindows]::GetPidWindows($DesktopPid)) {
    $hw = if ($w.Hwnd -is [IntPtr]) { $w.Hwnd.ToInt64() } else { [long]$w.Hwnd }
    $isCandidate = $wanted.ContainsKey($hw)
    $enriched += (ConvertTo-ProbeWindow -Window $w -Enrich:$isCandidate `
        -TimeoutSec $harvestBudget -MaxElements $HarvestMaxElements)
  }
  return $enriched
}

$windows = Get-PidWindows
if (-not $windows -or $windows.Count -eq 0) { Write-Output "no window for pid $DesktopPid found"; Write-Output "VERDICT: UNKNOWN"; exit 3 }

# 1. If a credential modal is ALREADY open, the credential is missing - report immediately.
$decision = Invoke-DialogDecision -Windows $windows
$hit = $decision.Credential
if ($hit) {
  Write-Output ("credential modal already open: '{0}'" -f $hit.Substring(0, [Math]::Min(80, $hit.Length)))
  Write-Output "VERDICT: CREDENTIAL_MISSING"
  exit 1
}

# 1b. A dialog is up that is not a credential prompt. It is NOT a credential wall - say what it is and
# exit 3 (cannot probe), never exit 1 (human needed). Invoking a Refresh on top of an unclassified
# dialog is how the 2026-08-28 field report ended up with a stale duplicate refresh to cancel.
$blocker = if ($decision.Verdict) { $decision } else { $null }
if ($blocker) {
  Write-Output ("dialog already open: {0}" -f (Format-DialogEvidence -Window $blocker.Window))
  if ($blocker.Evidence) {
    Write-Output ("  matched text: '{0}'" -f $blocker.Evidence.Substring(0, [Math]::Min(80, $blocker.Evidence.Length)))
  }
  # The next step travels WITH the verdict from `decide_dialog.py`. A `switch` over the same kinds
  # here would be a second implementation of the same judgement - the divergence issue #417 closed.
  if ($blocker.Guidance) { Write-Output ("  {0}" -f $blocker.Guidance) }
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
# From here on a PROVEN-benign progress dialog is our own refresh, so it is ignored (-RefreshInFlight).
# Nothing else is: an unreadable, unverified, caption-only or unrecognized dialog is LATCHED rather
# than acted on. It is not a credential prompt, so it must not abort a healthy refresh - but it also
# means "no modal appeared" is no longer established, so it must not be erased by a quiet deadline
# either. Latching is the same shape the Python detector uses for its indeterminate states, and it is
# what keeps both failure directions closed.
$latched = $null
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 2000
  $windows = Get-PidWindows
  $decision = Invoke-DialogDecision -Windows $windows -RefreshInFlight
  $hit = $decision.Credential
  if ($hit) {
    Write-Output ("credential modal detected: '{0}'" -f $hit.Substring(0, [Math]::Min(80, $hit.Length)))
    Write-Output "VERDICT: CREDENTIAL_MISSING"
    exit 1
  }
  $observed = if ($decision.Verdict) { $decision } else { $null }
  if ($observed -and $null -eq $latched) { $latched = $observed }
}
if ($latched) {
  Write-Output ("a dialog was up during the refresh: {0}" -f (Format-DialogEvidence -Window $latched.Window))
  Write-Output "  it matched no credential-prompt signature, so this is NOT a credential wall and no sign-in is implied"
  Write-Output "  but a window we could not account for was open, so 'no modal appeared' is not established"
  Write-Output ("VERDICT: {0}" -f $latched.Verdict)
  exit $latched.ExitCode
}
Write-Output "no credential modal within ${TimeoutSec}s (refresh proceeded)"
Write-Output "VERDICT: CREDENTIAL_PRESENT"
exit 0
