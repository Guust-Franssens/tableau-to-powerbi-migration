# Probe: does subagentStart fire, and does additionalContext reach the subagent?
# Logs the raw payload, then injects a sentinel the agent can be asked to quote.
$ErrorActionPreference = "Stop"
$raw = [Console]::In.ReadToEnd()
$log = Join-Path $PSScriptRoot "..\..\_hook_probe.log"
"[$(Get-Date -Format o)] $raw" | Add-Content -LiteralPath $log -Encoding utf8
$ctx = "HOOK_SENTINEL_ORBIT_58231 - injected by subagentStart at $(Get-Date -Format o)."
@{ additionalContext = $ctx } | ConvertTo-Json -Compress
