<#
.SYNOPSIS
  Preflight environment check for the Tableau -> Power BI migration pipeline.

.DESCRIPTION
  Written in PowerShell on purpose (the one committed script in this repo that is not Python):
  it is the FIRST thing the `tableau-migrator` agent runs, and it must work on a machine that does
  not have Python installed yet -- because one of the things it checks for IS Python. A Python
  bootstrap check would be a chicken-and-egg. PowerShell ships with every supported Windows, and the
  whole pipeline targets Power BI Desktop (Windows-only) and uses Windows-specific facilities
  (Get-AppxPackage for the Desktop MSIX, Get-OdbcDriver, the JSONC ~/.copilot config), so PowerShell
  is the correct, dependency-free bootstrap.

  Verifies: Python + the parser's Python deps, the powerbi-authoring@fabric-collection skill plugin,
  the MCP servers, Power BI Desktop + its Bridge CLI, npx, the .NET SDK, and the npm CLI version
  matrix. Prints a per-item status (OK / WARN / MISS) with an install hint for anything absent.

.PARAMETER Update
  Session-start only. Upgrades the npm bridge CLIs, but ONLY when they are below the correctness
  FLOOR (see $cliFloor) -- a floor check, not a blind `@latest`. Being above the floor is fine and is
  left alone; being above the recorded known-good matrix raises a WARN instead, because that is when
  the version-specific Gotchas in .github/agents/ need re-verification.

  TIMING RULE (the `when` is what makes this safe):
    - Session start, nothing in flight -> `-Update` is safe. The floor is a correctness floor.
    - Migration start (orchestrator step 0) -> plain preflight, NEVER `-Update`.
    - Mid-migration -> never. Swapping the validator under a half-built report is worse than a
      slightly old CLI.

.NOTES
  Run:  powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1 [-Update]
  Exit: 0 if every CRITICAL + RECOMMENDED item is present; 1 if any is missing.
#>
#Requires -Version 5.1
param([switch]$Update)

$ErrorActionPreference = 'SilentlyContinue'
$copilot = Join-Path $HOME '.copilot'
$results = New-Object System.Collections.Generic.List[object]

function Add-Check([string]$Name, [string]$Tier, [bool]$Ok, [string]$Detail, [string]$Hint = '') {
    $results.Add([pscustomobject]@{ Name = $Name; Tier = $Tier; Ok = $Ok; Detail = $Detail; Hint = $Hint })
}

function Read-CopilotJson([string]$File) {
    # ~/.copilot/*.json are JSONC (leading // comment lines). URL strings start with '"', not '//',
    # so dropping comment-only lines is safe across PowerShell versions.
    $p = Join-Path $copilot $File
    if (-not (Test-Path $p)) { return $null }
    try {
        $clean = (Get-Content $p | Where-Object { $_.TrimStart() -notmatch '^//' }) -join "`n"
        return $clean | ConvertFrom-Json
    }
    catch { return $null }
}

function Add-Cli([string]$Cmd, [string]$Tier, [string]$Hint) {
    $c = Get-Command $Cmd -ErrorAction SilentlyContinue
    Add-Check "cli: $Cmd" $Tier ($null -ne $c) $(if ($c) { $c.Source } else { 'not on PATH' }) $Hint
}

# --- Python (the bootstrap-critical one this whole file exists to check without needing Python) ---
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    $ver = (& python --version 2>&1) -replace 'Python\s*', ''
    # Actually compare - a bare $true here let Python 3.9 (or the Store alias stub, which prints nothing)
    # report OK while pyproject.toml requires >=3.11.
    $pyOk = $false
    try { $pyOk = [version](($ver -split '\s')[0]) -ge [version]'3.11' } catch { $pyOk = $false }
    Add-Check 'Python >= 3.11' 'critical' $pyOk `
        $(if ($ver) { $ver } else { 'no version reported (Microsoft Store alias stub?)' }) `
        'Install Python 3.11+ (pyproject requires >=3.11); ensure it precedes the Store alias on PATH.'
    foreach ($m in @(
            @('lxml', 'critical', 'uv sync - the deterministic parser imports lxml.etree.'),
            @('jsonschema', 'critical', 'uv add jsonschema (validates migration-spec.json against the schema).'),
            @('tableauhyperapi', 'optional', 'uv sync --extra extract (materializes .hyper extracts to CSV).'),
            @('playwright', 'optional', 'uv add playwright (harvester + validator screenshots).'),
            @('PIL', 'optional', 'uv add pillow (showcase gallery composition).'))) {
        & python -c "import $($m[0])" 2>$null
        Add-Check "python: $($m[0])" $m[1] ($LASTEXITCODE -eq 0) $(if ($LASTEXITCODE -eq 0) { 'importable' } else { 'not importable' }) $m[2]
    }
}
else {
    Add-Check 'Python >= 3.11' 'critical' $false 'not on PATH' 'Install Python 3.11+ (the deterministic parser and all scripts/ need it).'
}

Add-Cli 'powerbi-report-author' 'critical' 'npm install -g @microsoft/powerbi-report-authoring-cli (needs Node >= 20); provides validate + catalog/formatting/preview-*.'

# --- npm CLI versions: correctness FLOOR + known-good matrix (see AGENTS.md) ---
# These CLIs are unpinned GLOBAL installs (the official skill installs them with @latest), so they can
# change under you without any repo diff. Two distinct thresholds:
#   * FLOOR      - below this is a CORRECTNESS bug, not a nicety. powerbi-report-author < 0.1.4 returns
#                  errorCount:0 for PBIR that Desktop cannot open (e.g. report.json missing the
#                  schema-required `reportVersionAtImport`) -- a stale CLI silently green-lights a
#                  broken report. `-Update` repairs this, and only this.
#   * KNOWN-GOOD - the version the agent Gotchas were verified against. ABOVE it is not an error, but
#                  it does mean version-specific prose may be stale -> WARN, don't "fix" it.
$cliFloor     = @{ 'powerbi-report-author' = '0.1.4'; 'powerbi-desktop' = '0.1.2' }
$cliKnownGood = @{ 'powerbi-report-author' = '0.1.4'; 'powerbi-desktop' = '0.1.2' }
$cliPackage   = @{ 'powerbi-report-author' = '@microsoft/powerbi-report-authoring-cli'
                   'powerbi-desktop'       = '@microsoft/powerbi-desktop-bridge-cli' }

function Get-CliVersion([string]$Cmd) {
    if (-not (Get-Command $Cmd -ErrorAction SilentlyContinue)) { return $null }
    $raw = (& $Cmd --version 2>&1 | Select-Object -First 1) -replace '[^0-9.]', ''
    if ($raw -match '^\d+(\.\d+)+$') { return $raw } else { return $null }
}

foreach ($cliName in $cliFloor.Keys) {
    $actual = Get-CliVersion $cliName
    if (-not $actual) { continue }

    # -Update: repair ONLY a below-floor install. Never a blind upgrade to @latest.
    if ($Update -and ([version]$actual -lt [version]$cliFloor[$cliName])) {
        Write-Host ("  [..  ] $cliName $actual is below the {0} correctness floor - upgrading ..." -f $cliFloor[$cliName])
        & npm install -g "$($cliPackage[$cliName])@latest" 2>&1 | Out-Null
        $actual = (Get-CliVersion $cliName), $actual | Where-Object { $_ } | Select-Object -First 1
    }

    $floorOk = [version]$actual -ge [version]$cliFloor[$cliName]
    $drifted = $floorOk -and ([version]$actual -ne [version]$cliKnownGood[$cliName])
    $detail = if (-not $floorOk) { "$actual is BELOW the $($cliFloor[$cliName]) correctness floor" }
              elseif ($drifted)  { "$actual (above known-good $($cliKnownGood[$cliName]))" }
              else               { "$actual (known-good)" }
    $hint = if (-not $floorOk) {
        "Run this script with -Update at SESSION START, or: npm install -g $($cliPackage[$cliName])@latest"
    } else {
        "Version moved past the matrix in AGENTS.md - re-verify the version-specific Gotchas in .github/agents/ before trusting them."
    }
    # Below the floor is a real (recommended-tier) failure; above known-good is only an advisory.
    Add-Check "version: $cliName" $(if ($floorOk) { 'optional' } else { 'recommended' }) $(-not $drifted -and $floorOk) $detail $hint
}

# --- PBIR JSON-schema reachability (does `validate` actually run its schema layer this session?) ---
# `validate` reports 0 errors even when it could NOT fetch the visualContainer schema (PBIR_SCHEMA_UNREACHABLE),
# so this answers mechanically whether a green validate means "schema-checked" or only "structure-checked".
if (Get-Command 'powerbi-report-author' -ErrorAction SilentlyContinue) {
    $doc = & powerbi-report-author doctor 2>&1 | Out-String
    $docOk = $doc -match '"ok"\s*:\s*true'
    Add-Check 'powerbi-report-author doctor' 'optional' $docOk `
        $(if ($docOk) { 'self-checks OK (node, ajv, metadata provider)' } else { 'doctor reported a problem' }) `
        'Run `powerbi-report-author doctor` for detail. If schema fetch fails, treat a 0-error `validate` as STRUCTURE-ONLY (see AGENTS.md).'
}

# --- Fabric/Power BI skill plugin ---
$cfg = Read-CopilotJson 'config.json'
$plugin = $null
if ($cfg -and $cfg.installedPlugins) {
    $plugin = $cfg.installedPlugins | Where-Object { $_.name -eq 'powerbi-authoring' -and $_.marketplace -eq 'fabric-collection' } | Select-Object -First 1
}
$pluginOk = $plugin -and (Test-Path $plugin.cache_path)
Add-Check 'plugin: powerbi-authoring@fabric-collection' 'critical' ([bool]$pluginOk) `
    $(if ($pluginOk) { "v$($plugin.version)" } else { 'not installed/enabled' }) `
    'In Copilot: /plugin -> add marketplace microsoft/skills-for-fabric -> enable powerbi-authoring. See AGENTS.md.'

# --- MCP servers ---
$mcp = Read-CopilotJson 'mcp-config.json'
foreach ($srv in @(@('powerbi-modeling-mcp', 'recommended'), @('powerbi-remote', 'optional'))) {
    $has = $mcp -and $mcp.mcpServers.($srv[0])
    Add-Check "mcp: $($srv[0])" $srv[1] ([bool]$has) `
        $(if ($has) { 'configured' } else { 'not in ~/.copilot/mcp-config.json' }) `
        'Add via /mcp, or copy from .vscode/mcp.json into ~/.copilot/mcp-config.json (mcpServers).'
}

Add-Cli 'npx' 'recommended' 'Install Node.js; npx runs the powerbi-modeling MCP and the Desktop Bridge CLI.'
Add-Cli 'powerbi-desktop' 'recommended' 'npm install -g @microsoft/powerbi-desktop-bridge-cli - Desktop Bridge for open/reload/screenshot verification.'

# --- Power BI Desktop (Windows-only; this is why the bootstrap is PowerShell) ---
# TWO separate questions, and conflating them produced a real FALSE GREEN:
#   (a) is Desktop installed?           -> this script can find an MSIX install via Get-AppxPackage.
#   (b) can the Desktop BRIDGE find it? -> the bridge CANNOT enumerate AppX. For a Store/MSIX install
#       it resolves the exe from $env:PBI_DESKTOP_PATH, and its built-in fallbacks only cover the
#       classic (non-Store) install locations.
# Because the MSIX path embeds the VERSION, every Store auto-update invalidates that pinned value.
# Measured 2026-07-31: PBI_DESKTOP_PATH still pointed at ...PowerBIDesktop_2.156.879.0... while
# 2.157.480.0 was installed, so `powerbi-desktop open` died with DESKTOP_EXE_NOT_FOUND and the whole
# refresh/render/screenshot loop was dead - yet this script printed "Ready to migrate", because it
# silently fell back to the AppX path that only IT can see. Now we check the bridge's own resolution.
$appxExe = $null
$appxLoc = (Get-AppxPackage Microsoft.MicrosoftPowerBIDesktop).InstallLocation
if ($appxLoc -and (Test-Path (Join-Path $appxLoc 'bin\PBIDesktop.exe'))) { $appxExe = Join-Path $appxLoc 'bin\PBIDesktop.exe' }
$classicExe = 'C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe'
if (-not (Test-Path $classicExe)) { $classicExe = $null }

$desktop = $appxExe
if (-not $desktop) { $desktop = $classicExe }
if (-not $desktop -and $env:PBI_DESKTOP_PATH -and (Test-Path $env:PBI_DESKTOP_PATH)) { $desktop = $env:PBI_DESKTOP_PATH }
Add-Check 'Power BI Desktop' 'recommended' ([bool]$desktop) `
    $(if ($desktop) { $desktop } else { 'not found' }) `
    'Install Power BI Desktop (Store/MSIX preferred) - needed for the refresh + screenshot verification loop.'

# The bridge-resolution check. Only meaningful for an MSIX install; a classic install is found by the
# bridge's own fallbacks and needs no env var.
$envPath = $env:PBI_DESKTOP_PATH
$bridgeOk = $true
$bridgeDetail = 'classic install - bridge finds it without PBI_DESKTOP_PATH'
if ($appxExe) {
    if (-not $envPath) {
        $bridgeOk = $false
        $bridgeDetail = 'MSIX install but PBI_DESKTOP_PATH is NOT set - the bridge cannot enumerate AppX'
    }
    elseif (-not (Test-Path $envPath)) {
        $bridgeOk = $false
        $bridgeDetail = "PBI_DESKTOP_PATH points at a path that no longer exists (stale after a Store update): $envPath"
    }
    elseif ($envPath -ne $appxExe) {
        $bridgeOk = $false
        $bridgeDetail = "PBI_DESKTOP_PATH points at a DIFFERENT build than the installed one ($envPath)"
    }
    else {
        $bridgeDetail = "matches the installed MSIX build"
    }
}
elseif (-not $classicExe) {
    $bridgeOk = $false
    $bridgeDetail = 'no Power BI Desktop found at all'
}
Add-Check 'Desktop Bridge can resolve Desktop' 'recommended' $bridgeOk $bridgeDetail `
    $(if ($appxExe) { "Run: setx PBI_DESKTOP_PATH `"$appxExe`"  (then restart the shell). The MSIX path embeds the version, so this breaks on EVERY Power BI Desktop update - re-run preflight after one." } else { 'Install Power BI Desktop.' })

# --- .NET SDK (optional; the offline TMDL structural checks now run in Python) ---
# NOTE: this used to say the SDK was needed to build `scripts/tmdl_validate`, an offline TMDL
# deserializer. That project does not exist in this repo - preflight was demanding a dependency for a
# tool that was never built. The structural TMDL gate is now `scripts/check_tmdl.py` (pure Python,
# no .NET, no MCP): it catches duplicate properties, measure/column name collisions and the illegal
# `'T'[Col]=[Measure]` compact filter, all of which are invisible to check_m_syntax (M only) and to
# powerbi-report-author validate (Report item only). A full round-trip via the modeling MCP's
# ConnectFolder remains the deeper check. .NET stays listed because the ADOMD/AMO path used by
# probe_desktop_query.py and refresh_pbip_model.py needs it.
Add-Cli 'dotnet' 'optional' 'Install the .NET SDK - needed by the ADOMD/AMO path (probe_desktop_query.py, refresh_pbip_model.py).'

Add-Cli 'uv' 'optional' 'Install uv for env/dependency management (uv venv && uv sync).'
Add-Cli 'az' 'optional' 'Azure CLI - only for Fabric REST / token-based operations.'

$odbc = (Get-OdbcDriver -ErrorAction SilentlyContinue | Where-Object Name -like '*SQL Server*').Name
Add-Check 'ODBC Driver 18 (SQL)' 'optional' ($odbc -contains 'ODBC Driver 18 for SQL Server') `
    $(if ($odbc) { ($odbc | Select-Object -Unique) -join '; ' } else { 'none' }) `
    'Only for direct SQL Analytics Endpoint / Warehouse queries.'

# --- Render ---
$blocking = 0
foreach ($tier in @('critical', 'recommended', 'optional')) {
    Write-Host ''
    Write-Host "== $($tier.ToUpper()) =="
    foreach ($r in ($results | Where-Object { $_.Tier -eq $tier })) {
        $mark = if ($r.Ok) { 'OK  ' } elseif ($tier -eq 'optional') { 'warn' } else { 'MISS' }
        Write-Host ("  [{0}] {1,-44} {2}" -f $mark, $r.Name, $r.Detail)
        if (-not $r.Ok -and $tier -ne 'optional') { Write-Host "         -> $($r.Hint)"; $blocking++ }
        elseif (-not $r.Ok -and $r.Hint) { Write-Host "         (optional) $($r.Hint)" }
    }
}
Write-Host ''
if ($blocking -gt 0) {
    Write-Host "PREFLIGHT: $blocking critical/recommended item(s) missing - resolve before migrating."
    exit 1
}
Write-Host 'PREFLIGHT: all critical + recommended dependencies present. Ready to migrate.'
exit 0
