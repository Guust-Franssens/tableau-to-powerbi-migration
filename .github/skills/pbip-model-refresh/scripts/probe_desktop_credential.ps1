<#
.SYNOPSIS
  Detect whether a credential prompt is blocking the live data source(s) in the currently open Power
  BI Desktop model, WITHOUT the agent being able to type the credential itself.

.DESCRIPTION
  Live database sources (Databricks, SQL Server, Snowflake, ...) need a credential that is NOT in the
  committed model files. In Power BI Desktop that credential is cached per-Windows-user (DPAPI) after
  the user authenticates once in a modal dialog. The Desktop Bridge cannot fill that modal, so before
  the agent's build/render/validate loop can work against live data, a human must sign in once.

  This probe classifies what it can OBSERVE inside ONE bounded window, so the migrator has evidence
  rather than a guess. The two directions are not symmetric: only positive, authentication-specific
  evidence establishes a missing credential, while CREDENTIAL_PRESENT establishes no opposite state -
  it records that nothing recognizable came up before the deadline, and nothing more. NOTE: for a
  *serverless* source that cold-starts, the modal can appear only AFTER this probe's timeout, yielding a
  false CREDENTIAL_PRESENT; treat the one-row data probe (DATA_OK vs NO_DATA) as the gate of record and
  use this modal probe only to explain a NO_DATA. That probe ships inside the pbip-model-refresh skill:
  .github/skills/pbip-model-refresh/scripts/probe_desktop_query.py (scripts/probe_desktop_query.py is a
  forwarding shim kept for existing callers).
    * If a credential modal is already open, or appears within -TimeoutSec of a refresh -> MISSING.
    * If NO credential modal and no other recognized dialog is DETECTED within -TimeoutSec of the
      refresh being invoked -> PRESENT. That token records an OBSERVATION about this probe's window
      and nothing more: it does not establish that a credential exists or is cached, that the refresh
      proceeded or completed, that Desktop stayed alive, that no dialog went unseen, or that an
      unsupervised loop is safe. Confirm with the one-row data probe before relying on it.

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
  Dot-source seam for the test suite: define the pure window classifiers, then return WITHOUT loading
  UI Automation, compiling the Win32 shim, or touching any process. The classifiers are the part that
  decides a hard stop, so they must be exercisable against synthesised windows on a machine with no
  Power BI Desktop. Not for interactive use.

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
    exit 0  CREDENTIAL_PRESENT   a refresh was invoked and NO credential modal and no other recognized
                                 dialog were detected before this probe's deadline. That is an absence
                                 of evidence, not proof of a cached credential: it does not establish
                                 that a credential exists, that the refresh proceeded or completed,
                                 that Desktop stayed alive and responsive, that no dialog went unseen,
                                 or that an unsupervised loop is safe - a process that dies mid-poll
                                 enumerates zero windows and lands here. Never the gate of record -
                                 confirm with the one-row data probe.
    exit 3  UNKNOWN              no verdict was established - no window for the pid, a minimized
                                 owner, or no Refresh control was ever invoked (with nothing invoked,
                                 "no modal appeared" proves nothing - reporting PRESENT would be a
                                 fail-open arbiter). The credential state was not determined in either
                                 direction.
    exit 3  REFRESH_IN_PROGRESS  a dialog whose CONTENT positively reads as refresh progress - all of
                                 it, not just its first element - was already up at t=0. Desktop is
                                 busy, so the credential state cannot be probed; wait for it, or
                                 cancel the stale refresh.
    exit 3  DIALOG_NEEDS_HUMAN   a KNOWN blocking prompt matched: the native database query approval
                                 modal, an approval request, or an "authentication required" notice.
                                 Not exit 1, because the credential signature did not match - but the
                                 remedy is NOT established either, since that signature spans both an
                                 approval and an authentication notice. Read the prompt on screen.
                                 Never suppressed, and it outranks any progress text in the same
                                 window.
    exit 3  DIALOG_UNRECOGNIZED  a dialog is up whose text matched NO signature, or which shows
                                 progress text ALONGSIDE prose that is not progress status. We read
                                 it and could not account for it. The signatures are a known-shapes
                                 list, not an exhaustive one, so a matched-nothing read settles
                                 NOTHING about the credential state in either direction: a human must
                                 look at the screen and say what the window is.
    exit 3  DIALOG_UNREADABLE    a dialog is up that could not be shown to be harmless: no text at
                                 all, or only a reassuring CAPTION, or benign-looking content read
                                 from an INCOMPLETE harvest. Its content was never established, so no
                                 conclusion about the credential state is available; a human must look
                                 at the screen. "We could not establish it" is a weaker state of
                                 knowledge than "we read it and it did not match", and the two must
                                 not collapse into one verdict.

  ⚠️ `BLOCKED_BY_DIALOG` is deliberately NOT emitted here any more (issue #367). It used to be, on a
  size-only test - ANY visible non-main window >=100x100 - which a Power BI Refresh progress dialog
  satisfies trivially; a field report on 2026-08-28 caught it reporting exactly that under three
  concurrent refreshes. This script has no way to establish that a dialog is *blocking*, so every
  `BLOCKED_BY_DIALOG` it emitted was an inference dressed as a finding, in the one verdict class the
  toolkit treats as a hard stop. The Python fast check (`_credential_modal`) retired the token on the
  same grounds (issue #376) and now speaks this arbiter's vocabulary - `CREDENTIAL_MISSING`,
  `DIALOG_NEEDS_HUMAN`, `DIALOG_UNREADABLE`, `DIALOG_UNRECOGNIZED`, `REFRESH_IN_PROGRESS` - so neither
  path emits it any more; each names what it actually observed instead.

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
# Pure detectors. No Win32, no UI Automation, no process access - they take window objects and return
# a classification, so `-LoadDetectorsOnly` can dot-source this far and the suite can drive them with
# synthesised windows. Everything below the `if ($LoadDetectorsOnly) { return }` line needs a live
# Desktop and is therefore only reachable in a real probe run.
# --------------------------------------------------------------------------------------------------

# Connector credential-dialog signature text (covers Databricks / SQL / Snowflake / generic OAuth).
# Shared with the Python t=0/poll detector so the fast path and this arbiter cannot drift.
$sig = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'credential_modal_signature.regex') -Raw).Trim()

# Progress-dialog text. Matching this means "Power BI is working", NOT "a human is needed" - but only
# when it matches CONTENT, never the caption alone.
# ⚠️ Provenance: INFERRED from Power BI Desktop's refresh UI, not measured against a live capture -
# see SKILL.md. Keep the alternatives narrow and anchored: this is the only file that can cause a
# dialog to be ignored, so a broad pattern here is the one way to hide a genuine blocker.
$benignSig = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'benign_dialog_signature.regex') -Raw).Trim()

# Enumerated allowlist of window chrome control labels that carry no prompt (Cancel/OK/Close).
# Every non-empty content element must be either recognised status or matched by this allowlist;
# length is not evidence and short unknown text (e.g. "Password:") is never excused as benign.
$chromeSig = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'benign_chrome_signature.regex') -Raw).Trim()

# Prompts that are KNOWN to block a human. The set is not "everything except a sign-in prompt": its
# alternatives span the native-database-query approval modal AND an `Authentication (is )?required`
# notice, so a match establishes that a human must act at the screen, never WHICH action they must
# take. Migrated custom-SQL sources emit exactly the shape that triggers the approval modal, and
# SKILL.md's standing instruction is to check for it before concluding anything about credentials; a
# probe that suppressed it would make this bundle contradict its own documentation. Matched BEFORE
# benign, so one progress element in the same window cannot erase it (review round 3).
$blockingSig = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'blocking_prompt_signature.regex') -Raw).Trim()

function Get-NormalizedText {
  <# Whitespace-normalised, de-duplicated, order-preserving text. #>
  param([object[]]$Texts)
  $clean = @()
  foreach ($t in $Texts) {
    if ($null -eq $t) { continue }
    $normalized = (([string]$t) -replace '\s+', ' ').Trim()
    if ($normalized -and ($clean -notcontains $normalized)) { $clean += $normalized }
  }
  return $clean
}

function Get-DialogTextSet {
  <# The three views of a window's text that a classification needs.

  `All`      every text, caption included. Evidence, and the per-element credential scan.
  `Content`  everything that is NOT the caption. The ONLY view the benign signature may read - a
             caption cannot establish that a dialog is harmless (review 2026-08-29).
  `Search`   `All` plus the PROSE JOIN: non-interactive texts joined in tree order. WPF splits one
             sentence across visual elements, so `Enter your` + `credentials` matches nothing on its
             own; and interactive elements are excluded because an interposed `Cancel` button between
             those two fragments defeated a naive whole-window join.

  The join is applied to `Search` ONLY, and `Search` is read ONLY by the credential signature. That
  asymmetry is a safety property, not an accident: joining can manufacture a phrase (two adjacent
  table names `Account` `Key` join to the signature `Account Key`), and on the credential path that
  error is a LOUD false stop a human resolves by looking at the screen, whereas on the benign path it
  would be a SILENT false clear. Never let the benign signature read a join.
  #>
  param([Parameter(Mandatory = $true)][object]$Window)

  $title = (([string]$Window.Title) -replace '\s+', ' ').Trim()
  $rawTexts = @()
  if ($title) { $rawTexts += $title }
  if ($Window.Texts) { $rawTexts += $Window.Texts }
  $all = Get-NormalizedText -Texts $rawTexts
  $interactive = Get-NormalizedText -Texts $Window.InteractiveTexts
  $content = @($all | Where-Object { $_ -ne $title })
  $prose = @($all | Where-Object { $interactive -notcontains $_ })
  $search = @($all)
  if ($prose.Count -gt 1) { $search += ($prose -join ' ') }
  return [pscustomobject]@{ Title = $title; All = $all; Content = $content; Prose = $prose; Search = $search }
}

function Test-HarvestComplete {
  <# A REAL Boolean `$true`, nothing coercible to one.

  `-eq $true` is coercive: integer `1` and the string `"true"` both pass it, and both produced a clear
  in review. Anything that is not a Boolean is an unknown window shape, and an unknown shape must not
  be able to authorise suppression.
  #>
  param([object]$Value)
  return (($Value -is [bool]) -and $Value)
}

function Test-CredentialModal {
  <# First matching credential-signature text across EVERY window, any class, any size. #>
  param([object[]]$Windows)
  foreach ($w in $Windows) {
    foreach ($n in (Get-DialogTextSet -Window $w).Search) {
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
  <# Classify ONE candidate window. Only `benign` is suppressible, and only it needs positive proof.

  Kinds, in the order they are tested:
    credential        the credential signature matched a text element or the prose join -> hard stop.
    needs-human       a KNOWN blocking prompt whose text did not match the credential signature - the
                      native database query approval modal, an approval request, or an "authentication
                      required" notice. Exit 3, not 1: a human must act, and this probe does not
                      establish which action, because the signature spans both an approval and an
                      authentication notice.
    mixed-content     progress text AND unaccounted prose in the same window. Round 3's defect: the
                      FIRST content element matching the benign regex used to classify the whole
                      window, so `Evaluating` beside
                      `Permission is required to run this native database query` cleared it at exit 0.
                      The entire content is now scanned before benign can be concluded.
    benign            every content element is either recognised progress status or enumerated
                      chrome, at least one IS progress status, AND the harvest
                      completed -> Power BI is working. The one suppressible kind.
    benign-unverified as `benign`, but the harvest was truncated, timed out, or hit a pattern it could
                      not read. Benign-LOOKING is not benign.
    non-blocking      the owner window is ENABLED. Modality is a ONE-WAY test: a modal dialog disables
                      its owner, so an enabled owner PROVES this window blocks nothing. The converse
                      does not hold - Power BI's refresh dialog also disables the owner - so a disabled
                      owner never convicts. `$null` (no owner) proves nothing either way. It is tested
                      AFTER `needs-human` on purpose: a known blocking prompt outranks the exoneration.
    benign-title-only the CAPTION matched the benign signature and no content did. A caption is not
                      content.
    unreadable        no readable text at all.
    unrecognized      readable text that matched no signature: we looked, and nothing we know accounts
                      for it. The signature list is not exhaustive, so this is not a finding ABOUT the
                      credential state - it is the absence of one. Distinct from `unreadable` on
                      purpose - absent is not empty.

  Everything except `credential` and `benign` lands in the exit-3 band, so the failure mode of every
  uncertainty here is a LOUD stop-and-look, never a silent clear.
  #>
  param([Parameter(Mandatory = $true)][object]$Window)

  $sets = Get-DialogTextSet -Window $Window
  foreach ($t in $sets.Search) {
    if ($t -match $sig) { return [pscustomobject]@{ Kind = 'credential'; Evidence = $t } }
  }
  foreach ($t in $sets.Search) {
    if ($t -match $blockingSig) { return [pscustomobject]@{ Kind = 'needs-human'; Evidence = $t } }
  }

  # Scan ALL of the content, and account for Title (caption).
  $benignHit = $null
  $unaccounted = $null
  if ($sets.Title) {
    if (-not ($sets.Title -match $benignSig) -and -not ($sets.Title -match $chromeSig)) {
      $unaccounted = $sets.Title
    }
  }
  foreach ($t in $sets.Content) {
    if ($t -match $benignSig) {
      if ($null -eq $benignHit) { $benignHit = $t }
      continue
    }
    if ($t -match $chromeSig) {
      continue
    }
    if ($null -eq $unaccounted) {
      $unaccounted = $t
    }
  }
  if ($benignHit) {
    if ($unaccounted) { return [pscustomobject]@{ Kind = 'mixed-content'; Evidence = $unaccounted } }
    if (Test-HarvestComplete -Value $Window.HarvestComplete) {
      return [pscustomobject]@{ Kind = 'benign'; Evidence = $benignHit }
    }
    return [pscustomobject]@{ Kind = 'benign-unverified'; Evidence = $benignHit }
  }
  if ($Window.OwnerEnabled -eq $true) {
    return [pscustomobject]@{ Kind = 'non-blocking'; Evidence = 'owner window is enabled' }
  }
  if ($sets.Title -and $sets.Title -match $benignSig) {
    return [pscustomobject]@{ Kind = 'benign-title-only'; Evidence = $sets.Title }
  }
  if ($sets.All.Count -eq 0) {
    return [pscustomobject]@{ Kind = 'unreadable'; Evidence = '' }
  }
  return [pscustomobject]@{ Kind = 'unrecognized'; Evidence = if ($unaccounted) { $unaccounted } else { $sets.All[0] } }
}

function ConvertTo-HarvestResult {
  <# Validate a harvest child's payload before any of it is believed.

  Round 3's MEDIUM, and the third instance on this branch of MISSING EVIDENCE READ AS GOOD EVIDENCE:
  the parent checked only that the payload was valid JSON, then computed
  `(-not $p.Truncated) -and (-not $p.PatternsIncomplete)`. A missing property is `$null`, and
  `-not $null` is `$true`, so a well-formed-but-schema-incomplete payload became
  `HarvestComplete = $true` - a real Boolean, which then sailed through the strict
  `Test-HarvestComplete` guard because the coercion had already happened upstream of it.

  Both flags must EXIST and be actual Booleans, and the child must have exited 0. Items are still
  merged when they are present and the flags are not - unread text lowers credential recall, so
  keeping it costs nothing and can only help - but `Complete` stays `$false`, so a malformed payload
  can never authorise suppression.
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
  return [pscustomobject]@{ Items = $items; Complete = [bool]$complete }
}

function Format-DialogEvidence {
  <# One-line window description for a verdict line. #>
  param([object]$Window)
  $title = if ($Window.Title) { $Window.Title } else { '(empty title)' }
  $harvest = if (Test-HarvestComplete -Value $Window.HarvestComplete) { 'complete' } else { 'INCOMPLETE' }
  return ("class={0} title='{1}' size={2}x{3} harvest={4}" -f
    $Window.ClassName, $title, $Window.Width, $Window.Height, $harvest)
}

function Get-DialogVerdict {
  <# Fold per-window classifications into ONE verdict, or `$null` when nothing needs reporting.

  Precedence: credential > (unreadable band) > unrecognized > benign. It is not arbitrary:

    * credential is the only terminal finding, so it short-circuits.
    * `benign` is the only kind carrying POSITIVE evidence of harmlessness, so it must never outrank a
      window we could not account for - otherwise one progress dialog masks a real modal.
    * the unreadable band (`unreadable`, `benign-unverified`, `benign-title-only`) outranks
      `unrecognized` because it is the weaker state of knowledge, and the weaker state is what must
      stay visible.

  `-RefreshInFlight` is set only inside the poll loop, i.e. after THIS script invoked the refresh.
  There, a proven-benign progress dialog is our own and is ignored - and nothing else is. At t=0 it is
  somebody else's, and stacking a second refresh on it is exactly what the 2026-08-28 field report had
  to unpick by hand.
  #>
  param([object[]]$Windows, [switch]$RefreshInFlight)

  $needsHuman = $null
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
      'needs-human' { if ($null -eq $needsHuman) { $found.Verdict = 'DIALOG_NEEDS_HUMAN'; $needsHuman = $found } }
      'unreadable' { if ($null -eq $unreadable) { $found.Verdict = 'DIALOG_UNREADABLE'; $unreadable = $found } }
      'benign-unverified' { if ($null -eq $unreadable) { $found.Verdict = 'DIALOG_UNREADABLE'; $unreadable = $found } }
      'benign-title-only' { if ($null -eq $unreadable) { $found.Verdict = 'DIALOG_UNREADABLE'; $unreadable = $found } }
      'mixed-content' { if ($null -eq $unrecognized) { $found.Verdict = 'DIALOG_UNRECOGNIZED'; $unrecognized = $found } }
      'unrecognized' { if ($null -eq $unrecognized) { $found.Verdict = 'DIALOG_UNRECOGNIZED'; $unrecognized = $found } }
      'benign' { if ($null -eq $benign) { $found.Verdict = 'REFRESH_IN_PROGRESS'; $benign = $found } }
      default { }
    }
  }
  if ($null -ne $needsHuman) { return $needsHuman }
  if ($null -ne $unreadable) { return $unreadable }
  if ($null -ne $unrecognized) { return $unrecognized }
  if ($null -ne $benign -and -not $RefreshInFlight) { return $benign }
  return $null
}

function Get-VerdictGuidance {
  <# The operator-facing prose for ONE machine-readable verdict token (issue #146).

  MESSAGING ONLY. This function decides nothing: it is called AFTER a verdict has been chosen, and it
  never changes the token or the exit code. It exists so every emission site says the same thing about
  the same token, and so what it says can be tested directly instead of only through a live Desktop.

  The rule it enforces: a line may only claim what the verdict's own evidence supports.

    * `CREDENTIAL_MISSING` matched the credential signature - POSITIVE evidence of a sign-in prompt,
      so it may name sign-in as the remedy.
    * `DIALOG_NEEDS_HUMAN` matched a KNOWN blocking-prompt signature, so it may say a human must act.
      It may NOT say which action: that signature covers the native-query approval AND
      `Authentication (?:is )?required`, so "an approval, not a sign-in" is false for part of its own
      match set. It names the signature and sends the human to the prompt.
    * `DIALOG_UNREADABLE` and `DIALOG_UNRECOGNIZED` matched NOTHING. Matching nothing is not evidence
      of absence: the signatures are a known-shapes list, and a harvest is never provably complete
      (`LegacyIAccessiblePattern` is unreachable from managed UIA at all). So neither may say "this is
      not a credential wall", "no sign-in is implied", or that the credential sits behind it. They say
      what was and was not established, and send a human to the screen.
    * `REFRESH_IN_PROGRESS` positively read progress content: wait or cancel, do not stack.
    * `CREDENTIAL_PRESENT` observed only that a refresh was invoked and no credential prompt appeared
      before the deadline. It may NOT claim the refresh ran to the deadline, that Desktop stayed alive
      and responsive, or that no window was left unaccounted for - a process that dies mid-poll
      enumerates zero windows and reaches exactly this line (the token and exit code are unchanged;
      only the prose is bounded).
    * `UNKNOWN` established no verdict at all.
  #>
  param(
    [Parameter(Mandatory = $true)][string]$Verdict,
    [string]$Kind = ''
  )

  $lines = @()
  switch ($Kind) {
    'mixed-content' { $lines += "  it shows refresh progress AND prose that is not progress status - a progress dialog does not explain the rest of this window" }
    'benign-unverified' { $lines += "  its content LOOKS like refresh progress, but the read was truncated or incomplete - benign-looking is not benign" }
    'benign-title-only' { $lines += "  its CAPTION looks like a progress dialog, but no content confirmed it - a caption is not content" }
    'unreadable' { $lines += "  this window exposes no readable text at all" }
    default { }
  }
  switch ($Verdict) {
    'CREDENTIAL_MISSING' {
      $lines += "  its text matched the connector credential-prompt signature - positive evidence of a sign-in prompt"
      $lines += "  a human must sign in ONCE at the Desktop screen; no automation can fill this dialog"
    }
    'DIALOG_NEEDS_HUMAN' {
      $lines += "  its text matched a KNOWN blocking-prompt signature - the native database query approval, an approval request, or an 'authentication required' notice"
      $lines += "  a human must act at the Desktop screen on the prompt shown there; this probe does not establish WHICH action that prompt needs, so read it before assuming"
    }
    'DIALOG_UNREADABLE' {
      $lines += "  its content could not be established, so NOTHING was determined about the credential state - a credential prompt is neither confirmed nor ruled out"
      $lines += "  a human must look at the Desktop screen and say what this window is"
    }
    'DIALOG_UNRECOGNIZED' {
      $lines += "  its content was READ but matched no signature this probe knows, and the signature list is not exhaustive - so this window is unaccounted for and NOTHING was determined about the credential state"
      $lines += "  a human must look at the Desktop screen and say what this window is"
    }
    'REFRESH_IN_PROGRESS' {
      $lines += "  its content positively reads as refresh progress: a refresh is already running on this pid, so the credential state could not be probed"
      $lines += "  wait for it, or cancel the stale one; do not stack a second refresh on it"
    }
    'CREDENTIAL_PRESENT' {
      $lines += "  a refresh was invoked and this probe saw no credential prompt before its own deadline"
      $lines += "  that is an absence of evidence, not proof of a cached credential: nothing here establishes that Desktop stayed alive and responsive, or that every window was accounted for - confirm with the one-row data probe"
    }
    default {
      $lines += "  no verdict was established: the credential state was not determined, in either direction"
      $lines += "  nothing here establishes the credential state; re-probe a responsive Desktop, or look at the Desktop screen"
    }
  }
  return $lines
}

function Write-VerdictGuidance {
  <# Emit `Get-VerdictGuidance`'s lines. The single runtime path from a token to what we tell a human. #>
  param([Parameter(Mandatory = $true)][string]$Verdict, [string]$Kind = '')
  foreach ($line in (Get-VerdictGuidance -Verdict $Verdict -Kind $Kind)) { Write-Output $line }
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
  <# Win32 `WindowInfo` (+ optional UIA harvest) -> the plain object the pure classifiers consume. #>
  param([object]$Window, [switch]$Enrich, [int]$TimeoutSec = 8, [int]$MaxElements = 2000)

  $title = [string]$Window.Title
  $texts = @()
  foreach ($t in $Window.Texts) { if ($t) { $texts += [string]$t } }
  $interactive = @()
  $complete = $false
  if ($Enrich) {
    $hwnd = if ($Window.Hwnd -is [IntPtr]) { $Window.Hwnd.ToInt64() } else { [long]$Window.Hwnd }
    $harvested = Get-BoundedAutomationHarvest -Hwnd $hwnd -TimeoutSec $TimeoutSec -MaxElements $MaxElements
    if ($null -ne $harvested) {
      foreach ($item in @($harvested.Items)) {
        if (-not $item.Text) { continue }
        $texts += [string]$item.Text
        if ($item.Interactive) { $interactive += [string]$item.Text }
      }
      $complete = [bool]$harvested.Complete
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
  }
}

function Get-PidWindows {
  # EVERY visible top-level/owned window of the target process, not UIA RootElement children. A
  # Power BI credential modal is an owned window; UIA root-child discovery misses it.
  #
  # The UIA harvest is applied to CANDIDATE windows only. Walking the main Power BI window's visual
  # tree would cost seconds per poll for text the classifiers never read (it is excluded by class).
  $enriched = @()
  foreach ($w in [Win32CredentialWindows]::GetPidWindows($DesktopPid)) {
    $isCandidate = @(Select-DialogCandidate -Windows @($w)).Count -eq 1
    $enriched += (ConvertTo-ProbeWindow -Window $w -Enrich:$isCandidate `
        -TimeoutSec $harvestBudget -MaxElements $HarvestMaxElements)
  }
  return $enriched
}

$windows = Get-PidWindows
if (-not $windows -or $windows.Count -eq 0) {
  Write-Output "no window for pid $DesktopPid found"
  Write-VerdictGuidance -Verdict 'UNKNOWN'
  Write-Output "VERDICT: UNKNOWN"
  exit 3
}

# 1. If a credential modal is ALREADY open, the credential is missing - report immediately.
$hit = Test-CredentialModal -Windows $windows
if ($hit) {
  Write-Output ("credential modal already open: '{0}'" -f $hit.Substring(0, [Math]::Min(80, $hit.Length)))
  Write-VerdictGuidance -Verdict 'CREDENTIAL_MISSING'
  Write-Output "VERDICT: CREDENTIAL_MISSING"
  exit 1
}

# 1b. A dialog is up that did not match the credential signature. Say what was OBSERVED and what that
# leaves undetermined, then exit 3 (cannot probe), never exit 1 (the credential-specific hard stop).
# Exit 3 is NOT a "no human needed" band: a known blocking prompt, and a window nobody could account
# for, both land here and both send a human to the screen. Matching nothing is not evidence that the
# credential is cached, so no line here may claim it. Invoking a Refresh on top
# of an unclassified dialog is how the 2026-08-28 field report ended up with a stale duplicate refresh
# to cancel.
$blocker = Get-DialogVerdict -Windows $windows
if ($blocker) {
  Write-Output ("dialog already open: {0}" -f (Format-DialogEvidence -Window $blocker.Window))
  if ($blocker.Evidence) {
    Write-Output ("  matched text: '{0}'" -f $blocker.Evidence.Substring(0, [Math]::Min(80, $blocker.Evidence.Length)))
  }
  Write-VerdictGuidance -Verdict $blocker.Verdict -Kind $blocker.Kind
  Write-Output ("VERDICT: {0}" -f $blocker.Verdict)
  exit $blocker.ExitCode
}
foreach ($w in $windows) {
  if ($w.Minimized -and $w.ClassName.StartsWith('WindowsForms10.Window.8')) {
    Write-Output "Power BI Desktop owner window is minimized; owned modal dialogs are hidden from enumeration"
    Write-VerdictGuidance -Verdict 'UNKNOWN'
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
  Write-VerdictGuidance -Verdict 'UNKNOWN'
  Write-Output "VERDICT: UNKNOWN"
  exit 3
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
# From here on a PROVEN-benign progress dialog is our own refresh, so it is ignored (-RefreshInFlight).
# Nothing else is: an unreadable, unverified, caption-only or unrecognized dialog is LATCHED rather
# than acted on. It did not MATCH the credential signature, so it must not abort a healthy refresh -
# but matching nothing is not evidence of absence, so "no modal appeared" is no longer established
# either and the dialog must not be erased by a quiet deadline. Latching is the same shape the Python
# detector uses for its indeterminate states, and it is what keeps both failure directions closed.
$latched = $null
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 2000
  $windows = Get-PidWindows
  $hit = Test-CredentialModal -Windows $windows
  if ($hit) {
    Write-Output ("credential modal detected: '{0}'" -f $hit.Substring(0, [Math]::Min(80, $hit.Length)))
    Write-VerdictGuidance -Verdict 'CREDENTIAL_MISSING'
    Write-Output "VERDICT: CREDENTIAL_MISSING"
    exit 1
  }
  $observed = Get-DialogVerdict -Windows $windows -RefreshInFlight
  if ($observed -and $null -eq $latched) { $latched = $observed }
}
if ($latched) {
  Write-Output ("a dialog was up during the refresh: {0}" -f (Format-DialogEvidence -Window $latched.Window))
  Write-VerdictGuidance -Verdict $latched.Verdict -Kind $latched.Kind
  Write-Output "  this dialog was OBSERVED during the refresh, so 'no modal appeared' was never established"
  Write-Output ("VERDICT: {0}" -f $latched.Verdict)
  exit $latched.ExitCode
}
Write-Output "no credential modal and no other recognized dialog detected within ${TimeoutSec}s of invoking the refresh"
Write-VerdictGuidance -Verdict 'CREDENTIAL_PRESENT'
Write-Output "VERDICT: CREDENTIAL_PRESENT"
exit 0
