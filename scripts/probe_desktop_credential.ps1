<#
.SYNOPSIS
  Forwarding shim. The real credential arbiter now ships INSIDE the `pbip-model-refresh` skill, at
  `.github/skills/pbip-model-refresh/scripts/probe_desktop_credential.ps1`, so the skill stays one
  self-contained copyable unit.

.DESCRIPTION
  This path used to hold a SECOND, DIVERGED copy of the arbiter, and the divergence was not cosmetic:
  the root copy scanned only the FIRST top-level window for the credential modal (the modal is a
  sibling window, so it could be missed), and - worse - it reported `CREDENTIAL_PRESENT` even when it
  had never managed to invoke a Refresh at all. "No modal appeared" proves nothing if nothing was
  triggered, so that was a FAIL-OPEN arbiter: it told an agent a credential was cached when it had
  tested precisely nothing. The bundled copy scans every window and returns `UNKNOWN` (exit 3) in that
  case. Meanwhile `scripts/README.md` and `docs/data-source-credentials.md` still point HERE, so the
  documented path was the broken one.

  Forwarding rather than deleting keeps every existing caller working while guaranteeing there is only
  ONE arbiter. Arguments and the exit code pass straight through.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/probe_desktop_credential.ps1 -DesktopPid 42532
#>

$target = Join-Path $PSScriptRoot '..\.github\skills\pbip-model-refresh\scripts\probe_desktop_credential.ps1'
if (-not (Test-Path -LiteralPath $target)) {
  Write-Output "bundled credential probe not found at $target"
  Write-Output "VERDICT: UNKNOWN"
  exit 3
}

& $target @args
exit $LASTEXITCODE
