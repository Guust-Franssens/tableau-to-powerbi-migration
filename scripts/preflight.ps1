<#
.SYNOPSIS
  Preflight environment check for the Tableau -> Power BI migration pipeline.

.DESCRIPTION
  Written in PowerShell on purpose (the one committed script in this repo that is not Python):
  it is the FIRST thing the `tableau-migrator` agent runs, and it must work on a machine that does
  not have Python installed yet -- because one of the things it checks for IS Python. A Python
  bootstrap check would be a chicken-and-egg. PowerShell ships with every supported Windows, and the
  whole pipeline targets Power BI Desktop (Windows-only) and uses Windows-specific facilities
  (Get-AppxPackage for the Desktop MSIX, Get-OdbcDriver, the JSONC Copilot home config), so PowerShell
  is the correct, dependency-free bootstrap.

  Verifies: Python + the parser's Python deps, the deterministic conversion engine (the installed
  `tableau-fabric-skills` plugin, which is its SINGLE canonical source - a second copy anywhere is a
  hard failure, see issue #107), both skill plugins
  (powerbi-authoring@fabric-collection and powerbi-playbook@powerbi-playbook-collection),
  the MCP servers, Power BI Desktop + its Bridge CLI, npx, the .NET SDK, the npm CLI version matrix,
  and - when you declare an intended tenant - that the Fabric token this machine mints is actually
  for THAT tenant. Prints a per-item status (OK / WARN / MISS) with an install hint for anything
  absent.

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

.PARAMETER CheckUpstream
  Opt-in (~3s of network) and ADVISORY. Every other check compares an installed version against a
  hard-coded number, which answers "is what I have good enough" but never "has the world moved".
  This asks npm for the latest bridge CLIs and GitHub for the conversion engine's upstream VERSION.
  It never upgrades and never fails the run.

.PARAMETER Tenant
  The Entra tenant id (a GUID) you INTEND to deploy into - the same id you pass to
  `deploy_estate.py --tenant`. Declaring it turns on the wrong-tenant check (see the "Fabric token
  tenant" block below); omitting it costs nothing and skips that check, so preflight never pays for
  `az` on a machine that is only parsing workbooks.

  It can also come from `$env:FABRIC_TENANT_ID`, or `FABRIC_TENANT_ID` in the git-ignored `.env` -
  which is the right home for a real customer tenant id, because this repository is PUBLIC.
  Surrounding whitespace and a matched pair of quotes are accepted from any of the three
  (`FABRIC_TENANT_ID="72f9..."` is ordinary dotenv spelling, not a wrong tenant), and a `.env` value
  may carry a trailing `# comment` - which, next to a GUID nobody recognizes by sight, is exactly
  where one belongs.

  A default DOMAIN (`contoso.onmicrosoft.com`) is accepted too and resolved to its GUID via
  `az account list --all`, because `az --tenant` and `deploy_estate.py --tenant` both take that form.
  A vanity domain, or a tenant this machine has never signed in to, cannot be resolved - preflight
  then says so rather than guessing, and never blocks on it.

  WHERE the declaration came from decides how loudly a mismatch lands, because it is the only
  evidence preflight has of deploy INTENT for THIS run:
    * `-Tenant` on the command line  -> a mismatch is CRITICAL (exit 1). You said, in this
      invocation, that you are pointing at a tenant; a token for another one is a blocker. If it
      could not be verified at all (unresolvable spelling, no `az`, no token), that is a RECOMMENDED
      warning: a declared check that did not run is not a pass.
    * `$env:FABRIC_TENANT_ID` / `.env` -> a mismatch is RECOMMENDED (a visible WARN, exit 0), and a
      failure to verify is OPTIONAL. Persisted configuration is a standing preference, not a
      statement that this run deploys - and steps 1-6 of an estate run never touch Fabric at all.

.PARAMETER Subscription
  Optional subscription id/name passed verbatim to `az account get-access-token --subscription`,
  so the tenant check can verify the NON-MUTATING fix for a wrong-tenant token before you rely on
  it. `az account set` fixes the same problem by rewriting the CLI's on-disk profile, which every
  other process on this machine then inherits.

The landing-zone workspace is read from `$env:FABRIC_WORKSPACE_ID` or `FABRIC_WORKSPACE_ID` in
`.env`. When declared, preflight checks it is reachable with the current Fabric token before a
deploy discovers a stale id or missing access.

.NOTES
  Run:  powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1 [-Update] [-Tenant <id>]
  Exit: 0 if every CRITICAL item is present; 1 if any CRITICAL item is missing.
        RECOMMENDED and OPTIONAL items are surfaced as warnings but do not stop a migration.
#>
#Requires -Version 5.1
param([switch]$Update, [switch]$CheckUpstream, [string]$Tenant, [string]$Subscription, [switch]$DoctorDiagnostics)

$ErrorActionPreference = 'SilentlyContinue'
$copilot = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { Join-Path $HOME '.copilot' }
$copilotOrigin = if ($env:COPILOT_HOME) { 'COPILOT_HOME' } else { 'default' }
$repoRoot = Split-Path -Parent $PSScriptRoot
$results = New-Object System.Collections.Generic.List[object]

function Add-Check([string]$Name, [string]$Tier, [bool]$Ok, [string]$Detail, [string]$Hint = '',
    [string]$DoctorId = '', [string]$DoctorReason = '', [string]$DoctorRemedy = '') {
    $blocker = if ($Tier -eq 'critical' -and -not $Ok) {
        [ordered]@{ id = $DoctorId; source = $null; reason = $DoctorReason; remedy = $DoctorRemedy }
    } else { $null }
    $results.Add([pscustomobject]@{ Name = $Name; Tier = $Tier; Ok = $Ok; Detail = $Detail; Hint = $Hint
        DoctorBlocker = $blocker })
}

function Get-PreflightSummary($Checks) {
    $critical = @($Checks | Where-Object { $_.Tier -eq 'critical' -and -not $_.Ok })
    [pscustomobject]@{ critical_missing = $critical.Count
        recommended_warnings = @($Checks | Where-Object { $_.Tier -eq 'recommended' -and -not $_.Ok }).Count
        blockers = @($critical | ForEach-Object { $_.DoctorBlocker }) }
}

if (-not $DoctorDiagnostics) { Write-Host "Copilot home: $copilot (origin: $copilotOrigin)" }
if ($env:COPILOT_HOME) {
    Add-Check 'Copilot home' 'CRITICAL' (Test-Path -LiteralPath $copilot -PathType Container) `
        "COPILOT_HOME: $copilot" 'Create the configured directory or correct COPILOT_HOME; no default-profile fallback.' `
        -DoctorId 'copilot_home' -DoctorReason 'configured_directory_missing' -DoctorRemedy 'verify_copilot_home'
}

function Read-CopilotJson([string]$File) {
    # Copilot home *.json are JSONC (leading // comment lines). URL strings start with '"', not '//',
    # so dropping comment-only lines is safe across PowerShell versions.
    $p = Join-Path $copilot $File
    if (-not (Test-Path $p)) { return $null }
    try {
        $clean = (Get-Content $p | Where-Object { $_.TrimStart() -notmatch '^//' }) -join "`n"
        return $clean | ConvertFrom-Json
    }
    catch { return $null }
}

function Add-Cli([string]$Cmd, [string]$Tier, [string]$Hint, [string]$DoctorId = '', [string]$DoctorRemedy = '') {
    $c = Get-Command $Cmd -ErrorAction SilentlyContinue
    Add-Check "cli: $Cmd" $Tier ($null -ne $c) $(if ($c) { $c.Source } else { 'not on PATH' }) $Hint `
        -DoctorId $DoctorId -DoctorReason 'command_missing' -DoctorRemedy $DoctorRemedy
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
        'Install Python 3.11+ (pyproject requires >=3.11); ensure it precedes the Store alias on PATH.' `
        -DoctorId 'python_runtime' -DoctorReason 'runtime_unavailable_or_old' -DoctorRemedy 'install_python'
    foreach ($m in @(
            @('lxml', 'critical', 'uv sync - the deterministic parser imports lxml.etree.'),
            @('jsonschema', 'critical', 'uv add jsonschema (validates migration-spec.json against the schema).'),
            @('tableauhyperapi', 'optional', 'uv sync --extra extract (materializes .hyper extracts to CSV).'),
            @('playwright', 'optional', 'uv add playwright (harvester + validator screenshots).'),
            @('PIL', 'optional', 'uv add pillow (showcase gallery composition).'))) {
        & python -c "import $($m[0])" 2>$null
        Add-Check "python: $($m[0])" $m[1] ($LASTEXITCODE -eq 0) $(if ($LASTEXITCODE -eq 0) { 'importable' } else { 'not importable' }) $m[2] `
            -DoctorId "python_$($m[0])" -DoctorReason 'import_failed' -DoctorRemedy 'sync_python_dependencies'
    }
}
else {
    Add-Check 'Python >= 3.11' 'critical' $false 'not on PATH' 'Install Python 3.11+ (the deterministic parser and all scripts/ need it).' `
        -DoctorId 'python_runtime' -DoctorReason 'runtime_unavailable_or_old' -DoctorRemedy 'install_python'
}

function Add-NodeRuntimeCheck([string]$Raw) {
    # Compare the numeric core: [version] rejects a prerelease suffix, so a Node 27 alpha used to fail this
    # critical floor. Semver orders X.Y.Z-pre before X.Y.Z, so a prerelease AT the floor still fails.
    # A prerelease above the floor passes it and gets a non-blocking warning.
    $text = if ($null -eq $Raw) { '' } else { $Raw.Trim() }
    $detail = if ($text) { $text -replace '^v', '' } else { 'no version reported' }
    $ok = $false
    $prerelease = $false
    if ($text -cmatch '\Av?([0-9]+\.[0-9]+\.[0-9]+)(-[0-9A-Za-z.-]+)?\z') {
        $prerelease = [bool]$Matches[2]
        try {
            $core = [version]$Matches[1]
            $ok = ($core -gt [version]'20.0.0') -or ($core -eq [version]'20.0.0' -and -not $prerelease)
        }
        catch { $ok = $false }
    }
    Add-Check 'Node.js >= 20' 'critical' $ok $detail 'Install Node.js 20+; npx and the Power BI bridge CLIs depend on it.' `
        -DoctorId 'node_runtime' -DoctorReason 'runtime_unavailable_or_old' -DoctorRemedy 'install_node'
    if ($ok -and $prerelease) {
        Add-Check 'Node.js release build' 'recommended' $false "$detail is a prerelease build" `
            'Install the current Node.js LTS release; prerelease builds are not exercised by this toolkit.'
    }
}

$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    Add-NodeRuntimeCheck ([string](& node --version 2>&1 | Select-Object -First 1))
}
else {
    Add-Check 'Node.js >= 20' 'critical' $false 'node not on PATH' 'Install Node.js 20+; npx and the Power BI bridge CLIs depend on it.' `
        -DoctorId 'node_runtime' -DoctorReason 'runtime_unavailable_or_old' -DoctorRemedy 'install_node'
}

Add-Cli 'powerbi-report-author' 'critical' 'npm install -g @microsoft/powerbi-report-authoring-cli (needs Node >= 20); provides validate + catalog/formatting/preview-*.' `
    -DoctorId 'report_author' -DoctorRemedy 'install_report_author'

# --- npm CLI versions: correctness FLOOR + known-good matrix (see AGENTS.md) ---
# These CLIs are unpinned GLOBAL installs (the official skill installs them with @latest), so they can
# change under you without any repo diff. Two distinct thresholds:
#   * FLOOR      - below this is a CORRECTNESS bug, not a nicety. powerbi-report-author < 0.1.4 returns
#                  errorCount:0 for PBIR that Desktop cannot open (e.g. a report.json whose
#                  themeCollection entries are missing the schema-required `reportVersionAtImport` --
#                  it belongs INSIDE each themeCollection entry, where it is required, and is
#                  FORBIDDEN at the top level of report.json, which answers "must NOT have additional
#                  properties". Ground truth:
#                  examples/shipping-kpis/fabric/ShippingKPIs.Report/definition/report.json)
#                  -- a stale CLI silently green-lights a broken report.
#                  `-Update` repairs this, and only this.
#   * KNOWN-GOOD - the version the agent Gotchas were verified against. ABOVE it is not an error, but
#                  it does mean version-specific prose may be stale -> WARN, don't "fix" it.
$cliFloor     = @{ 'powerbi-report-author' = '0.4.0'; 'powerbi-desktop' = '1.0.0' }
$cliKnownGood = @{ 'powerbi-report-author' = '0.5.0'; 'powerbi-desktop' = '1.0.0' }
$cliPackage   = @{ 'powerbi-report-author' = '@microsoft/powerbi-report-authoring-cli'
                   'powerbi-desktop'       = '@microsoft/powerbi-desktop-bridge-cli' }

function ConvertFrom-CliVersionOutput([string[]]$Lines) {
    $raw = ($Lines -join "`n").Trim()
    # Match the WHOLE output: noise followed by a valid version is not evidence of a clean command.
    if ($raw -cmatch '\Av?((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?)\z') {
        return $Matches[1]
    }
    return $null
}

function Get-VersionFloorVerdict([string]$Installed, [string]$Floor, [string]$KnownGood, [string]$FixHint) {
    $v = [pscustomobject]@{ Kind = 'unreadable'; Tier = 'critical'; Ok = $false
        Detail = 'version unreadable (expected one plain release version)'; Hint = $FixHint }
    if ($Installed -cmatch '\A[0-9]+\.[0-9]+\.[0-9]+-[0-9A-Za-z.-]+\z') {
        $v.Kind = 'prerelease'; $v.Detail = "$Installed is a prerelease, not a plain release"
        return $v
    }
    if ($Installed -cnotmatch '\A(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\z') { return $v }
    try { $number = [version]$Installed } catch { return $v }
    if (-not ($number -ge [version]$Floor)) {
        $v.Kind = 'below-floor'; $v.Detail = "$Installed is BELOW the $Floor correctness floor"
        return $v
    }
    $v.Tier = 'optional'; $v.Hint = ''
    if ($number -lt [version]$KnownGood) {
        $v.Kind = 'between'; $v.Detail = "$Installed (below known-good $KnownGood; allowed, unmeasured)"
    }
    elseif ($number -eq [version]$KnownGood) {
        $v.Kind = 'known-good'; $v.Ok = $true; $v.Detail = "$Installed (known-good)"
    }
    else {
        $v.Kind = 'above-known-good'; $v.Detail = "$Installed (above known-good $KnownGood; re-verify version-specific behaviour)"
    }
    return $v
}

function Get-CliVersion([string]$Cmd) {
    try {
        $lines = @(& $Cmd --version 2>&1)
        if (-not $? -or $LASTEXITCODE -ne 0) { return $null }
        return ConvertFrom-CliVersionOutput $lines
    }
    catch { return $null }
}

foreach ($cliName in $cliFloor.Keys) {
    if (-not (Get-Command $cliName -ErrorAction SilentlyContinue)) { continue }
    $fixHint = "At SESSION START only: run this script with -Update, or inspect the CLI and install a plain release of $($cliPackage[$cliName]) meeting the floor. See docs/operator-runbook.md."
    $v = Get-VersionFloorVerdict (Get-CliVersion $cliName) $cliFloor[$cliName] $cliKnownGood[$cliName] $fixHint

    # -Update: repair ONLY a below-floor install. Never a blind upgrade to @latest.
    if ($Update -and $v.Kind -eq 'below-floor') {
        if (-not $DoctorDiagnostics) { Write-Host ("  [..  ] $cliName is below the {0} correctness floor - upgrading ..." -f $cliFloor[$cliName]) }
        & npm install -g "$($cliPackage[$cliName])@latest" 2>&1 | Out-Null
        $v = Get-VersionFloorVerdict (Get-CliVersion $cliName) $cliFloor[$cliName] $cliKnownGood[$cliName] $fixHint
    }

    $doctorCliId = if ($cliName -eq 'powerbi-report-author') { 'report_author_version' } else { 'desktop_bridge_version' }
    Add-Check "version: $cliName" $v.Tier $v.Ok $v.Detail $v.Hint `
        -DoctorId $doctorCliId -DoctorReason $v.Kind -DoctorRemedy 'inspect_cli_release'
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

# --- The deterministic conversion engine: ONE copy, and it is the plugin (issue #107) -------------
#
# The engine is a separate project this repo does not pin, so it resolves at RUNTIME. Measured
# 2026-08-12, this machine had it installed TWICE at different versions - the plugin at 2.113.0 and a
# sibling clone at 2.126.0 - and different pipeline steps resolved different trees. They are not
# equivalent: 2.113.0 emits deprecated Bing shapeMap/filledMap visuals and drops a density-map
# worksheet entirely; 2.126.0 emits azureMap with a heat layer. Nothing in the run output said which
# one had run.
#
# The decision: the INSTALLED PLUGIN is the single canonical engine, and engine_source.py never falls
# back to another tree. A missing or unverifiable plugin is critical. A second tree can no longer run,
# so since #727 it is a recommended diagnostic, not a mandatory deletion.
#
# The candidate list lives in scripts/engine_source.py, not here, deliberately: duplicating it in
# PowerShell would recreate the two-definitions-of-one-thing problem this check exists to catch.
$engineStatus = $null
if ($py) {
    $engineRaw = & python (Join-Path $repoRoot 'scripts\engine_source.py') --json 2>$null
    try { $engineStatus = (($engineRaw | Out-String) | ConvertFrom-Json) } catch { $engineStatus = $null }
}
if ($engineStatus) {
    $alternatives = @($engineStatus.alternatives)
    Add-Check 'engine: plugin installed' 'critical' ([bool]$engineStatus.present) `
        $(if ($engineStatus.present) {
            "VERSION $($engineStatus.version) at $($engineStatus.root)" + $(if ($engineStatus.host -eq 'vscode') { ' - installed (enablement not verified)' })
        } else { $engineStatus.detail }) `
        $engineStatus.install_hint `
        -DoctorId 'engine_plugin' -DoctorReason 'plugin_missing' -DoctorRemedy 'install_engine_plugin'
    Add-Check 'engine: single source' 'recommended' ($alternatives.Count -eq 0) `
        $(if ($alternatives.Count) { "ALTERNATIVE COPY: $($alternatives -join '; ')" } else { 'plugin only' }) `
        'Diagnostic only (#727): every script runs the installed plugin, never this copy (#107). Removing it is optional; first confirm it has no uncommitted/unpushed work: git -C <path> status --porcelain; git -C <path> log --branches --not --remotes.'
}
else {
    Add-Check 'engine: single source' 'critical' $false `
        $(if ($py) { 'engine_source.py did not report - is scripts/engine_source.py present?' } else { 'not verified (Python unavailable)' }) `
        'Install Python 3.11+, then re-run. The engine single-source rule is enforced by scripts/engine_source.py --json.' `
        -DoctorId 'engine_source' -DoctorReason 'verdict_unavailable' -DoctorRemedy 'inspect_engine_source'
}

# --- Engine receipt drift: bundles retain their provenance, so read it back --------------------------
# A bundle built before the plugin updated is structurally indistinguishable from one built today until
# its receipt is compared. This is advisory: the timing rule forbids an engine upgrade mid-migration,
# so an actionable warning must never block the run that discovers it.
if ($engineStatus -and $engineStatus.present -and $py) {
    $receiptOutput = & python (Join-Path $repoRoot 'scripts\check_engine_receipts.py') --root $repoRoot 2>&1 | Out-String
    Add-Check 'engine: bundle receipt versions' 'optional' ($LASTEXITCODE -eq 0) `
        $receiptOutput.Trim() `
        'A listed bundle was built with a different engine version. Between migrations, re-run it with the installed canonical engine; do not upgrade the engine mid-migration.'
}

# --- Skill plugins ---
# This repo's reusable Power BI bundles are different: their marketplace/plugin name has changed once,
# and a hard-coded name let the installed copy drift silently. Discover that plugin by content instead:
# engine_source.py selects installation evidence for every host; preflight keeps the CLI verdict below.
# Exactly one discovered bundle install is acceptable; more than one is a critical shadowing hazard.
$authoring = $engineStatus.authoring_plugin
$cfg = [pscustomobject]@{ installedPlugins = @($authoring.entries) }
$pluginPolicy = @{ name = 'powerbi-authoring'; market = 'fabric-collection'
    floor = '0.3.19'; knownGood = '0.3.19'; skill = 'powerbi-report-cli'
    upstreamUrl = 'https://raw.githubusercontent.com/microsoft/skills-for-fabric/main/.github/plugin/marketplace.json'
    hint = 'Plugin repair happens BETWEEN Copilot sessions (Access is denied / os error 5 while the directory is locked). Close every session, then from a plain shell: copilot plugin marketplace update fabric-collection; copilot plugin update powerbi-authoring@fabric-collection. Update this plugin only. Confirm enabled state/version/content with copilot plugin list --json; an exit code alone can hide a stale catalog. See docs/operator-runbook.md, Updating the Power BI plugin and CLIs.' }

function Get-PluginVerdict($Entries, [bool]$CacheExists, $Manifest, [bool]$SkillPresent, $Policy) {
    $entriesArray = @($Entries | Where-Object { $null -ne $_ })
    $v = [pscustomobject]@{ Kind = 'missing'; Tier = 'critical'; Ok = $false
        Detail = 'expected exactly one powerbi-authoring@fabric-collection registry entry'; Hint = $Policy.hint }
    if ($entriesArray.Count -ne 1) { return $v }
    $entry = $entriesArray[0]
    if ($entry.name -cne $Policy.name -or $entry.marketplace -cne $Policy.market) { return $v }
    if ($entry.enabled -isnot [bool] -or $entry.enabled -ne $true) {
        $v.Kind = 'disabled'; $v.Detail = 'plugin is not enabled'; return $v
    }
    if (-not $CacheExists) { $v.Kind = 'cache-missing'; $v.Detail = 'plugin cache directory missing'; return $v }
    if (-not $Manifest -or $Manifest.name -cne $Policy.name) {
        $v.Kind = 'manifest-unreadable'; $v.Detail = 'effective .github/plugin/plugin.json unreadable or wrong name'; return $v
    }
    $v = Get-VersionFloorVerdict $entry.version $Policy.floor $Policy.knownGood $Policy.hint
    $mv = Get-VersionFloorVerdict $Manifest.version $Policy.floor $Policy.knownGood $Policy.hint
    if ($mv.Kind -in @('unreadable', 'prerelease')) { return $mv }
    if ($entry.version -cne $Manifest.version) {
        return [pscustomobject]@{ Kind = 'manifest-mismatch'; Tier = 'critical'; Ok = $false
            Detail = 'registry and effective manifest versions disagree'; Hint = $Policy.hint }
    }
    if ($v.Tier -eq 'critical') { return $v }
    if (-not $SkillPresent) {
        $v.Kind = 'skill-missing'; $v.Tier = 'critical'; $v.Ok = $false
        $v.Detail = 'required skills/powerbi-report-cli/SKILL.md leaf missing'; $v.Hint = $Policy.hint
    }
    return $v
}

function Get-VscodePluginVerdict($Evidence, $Policy) {
    # No host (T2P_PLUGIN_HOST refused, or copies in both hosts) or VS Code evidence. VS Code records
    # cannot establish enablement, and engine_source.py already required the report-cli skill leaf.
    $v = [pscustomobject]@{ Kind = 'missing'; Tier = 'critical'; Ok = $false
        Detail = $Evidence.detail; Hint = $Evidence.install_hint }
    if (-not $Evidence.ok) { return $v }
    try { $manifest = Get-Content -LiteralPath (Join-Path $Evidence.plugin_root '.github\plugin\plugin.json') -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop }
    catch { $manifest = $null }
    if (-not $manifest -or $manifest.name -cne $Policy.name) {
        $v.Kind = 'manifest-unreadable'; $v.Detail = 'effective .github/plugin/plugin.json unreadable or wrong name'; return $v
    }
    $v = Get-VersionFloorVerdict $manifest.version $Policy.floor $Policy.knownGood $Evidence.install_hint
    if ($v.Tier -ne 'critical') { $v.Detail += ' - installed (enablement not verified)' }
    return $v
}

$pluginEntries = @($cfg.installedPlugins | Where-Object {
    $_.name -ceq $pluginPolicy.name -and $_.marketplace -ceq $pluginPolicy.market
})
$plugin = $pluginEntries | Select-Object -First 1
$cacheExists = $plugin -and $plugin.cache_path -and (Test-Path -LiteralPath $plugin.cache_path -PathType Container)
$manifest = $null
$skillPresent = $false
if ($cacheExists) {
    # Copilot's GitHub-native manifest takes precedence over .claude-plugin.
    try { $manifest = Get-Content -LiteralPath (Join-Path $plugin.cache_path '.github\plugin\plugin.json') -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop }
    catch { $manifest = $null }
    $skillPresent = Test-Path -LiteralPath (Join-Path $plugin.cache_path "skills\$($pluginPolicy.skill)\SKILL.md") -PathType Leaf
}
$pv = Get-PluginVerdict $pluginEntries ([bool]$cacheExists) $manifest $skillPresent $pluginPolicy
if (-not $authoring) {
    $pv.Detail = $(if ($py) { 'not verified (engine_source.py did not report)' } else { 'not verified (Python unavailable)' })
}
elseif ($authoring.host -ne 'cli') {
    $pv = Get-VscodePluginVerdict $authoring $pluginPolicy
}
Add-Check 'plugin: powerbi-authoring@fabric-collection' $pv.Tier $pv.Ok $pv.Detail $pv.Hint `
    -DoctorId 'authoring_plugin' -DoctorReason $pv.Kind -DoctorRemedy 'repair_authoring_plugin_between_sessions'

$skillPlugin = $null
$skillRaw = $null
if ($py) { $skillRaw = & python (Join-Path $repoRoot 'scripts\skill_plugin_source.py') --json 2>$null }
try { $skillPlugin = (($skillRaw | Out-String) | ConvertFrom-Json) } catch { $skillPlugin = $null }

if (-not $skillPlugin) {
    Add-Check 'plugin: reusable Power BI skill bundles' 'recommended' $false `
        'not verified (skill_plugin_source.py did not report)' `
        'Run python scripts\skill_plugin_source.py --json. Without this plugin, repo-local skills may still work here, but subagents in other repos cannot invoke the bundles by name.'
}
elseif ($skillPlugin.status -eq 'multiple') {
    # Two copies (in one host, or one per host) create an ambiguous shadowing order; the resolver's hint
    # names the fix (remove one, or choose a host with T2P_PLUGIN_HOST).
    Add-Check 'plugin: reusable Power BI skill bundles' 'critical' $false `
        $skillPlugin.detail $skillPlugin.install_hint `
        -DoctorId 'reusable_skill_plugins' -DoctorReason 'multiple_installs' -DoctorRemedy 'inspect_duplicate_skill_plugins'
}
elseif ($skillPlugin.status -ne 'found') {
    # Deliberately recommended, not critical: with NO installed copy, this repo's local .github/skills
    # bundles remain available in this checkout. That is a capability/distribution gap for other repos,
    # not the measured false-green bug. STALE remains critical below because it silently executes code
    # from a different tree than the repo being inspected. T2P_PLUGIN_HOST does not change this severity.
    Add-Check "plugin: $($skillPlugin.identity)" 'recommended' $false `
        $(if ($skillPlugin.status -eq 'missing') { 'not installed/enabled' } else { $skillPlugin.detail }) `
        $skillPlugin.install_hint
}
else {
    Add-Check "plugin: $($skillPlugin.identity)" 'recommended' $true `
        $(if ($skillPlugin.host -eq 'vscode') { "$($skillPlugin.plugin_root) - installed (enablement not verified)" } else { $skillPlugin.plugin_root }) `
        'Discovered by scanning installed-plugins for this repo''s shipped skill bundles.'

    $missing = @()
    $drift = @()
    foreach ($name in @($skillPlugin.shipped_skills)) {
        $mine = Join-Path $repoRoot ".github\skills\$name\SKILL.md"
        $theirs = Join-Path $skillPlugin.skills_dir "$name\SKILL.md"
        if (-not (Test-Path $theirs)) { $missing += $name; continue }
        if ((Test-Path $mine) -and ((Get-FileHash $mine).Hash -ne (Get-FileHash $theirs).Hash)) { $drift += $name }
    }

    # Severity decision (2026-08-13): a completely missing plugin is warning-only in this repo because
    # there is no installed copy shadowing local .github/skills. But once an installed plugin exists,
    # partial or stale content is critical: subagents resolve the plugin copy first, so they silently run
    # bytes that differ from the repo and can invalidate measurements.
    $detail = if ($missing.Count) { "NOT INSTALLED in discovered plugin: $($missing -join ', ')" } else { "$(@($skillPlugin.shipped_skills).Count) bundle(s) present" }
    Add-Check 'skill bundles installed' 'critical' ($missing.Count -eq 0) $detail `
        'Refresh the installed copy in place: python scripts\sync_installed_skills.py. If the bundle is new, install/update the plugin BETWEEN sessions.' `
        -DoctorId 'skill_bundles' -DoctorReason 'bundle_missing' -DoctorRemedy 'sync_installed_skills'

    Add-Check 'skill bundles match published plugin' 'critical' ($drift.Count -eq 0) `
        $(if ($drift.Count) { "STALE in plugin: $($drift -join ', ')" } else { 'in sync' }) `
        'The plugin copy SHADOWS .github/skills, so agents run the OLDER code, not what this repo shows. FIX IT NOW, mid-session: python scripts/sync_installed_skills.py (the lock behind "os error 5" only blocks renaming the plugin dir - files inside stay writable). Then publish so other machines get it: python scripts/build_plugin.py --out <clone of the marketplace repo>, commit+push. Do not trust a measurement taken against a stale bundle.' `
        -DoctorId 'published_skill_bundles' -DoctorReason 'content_stale' -DoctorRemedy 'sync_and_publish_skills'
}

# Recommended means "warn, do not halt." A check is critical if any persona's Definition of Done
# depends on it, even when the dependency only fails later at handoff/validation time. Audited
# 2026-08-10 under that exit semantics:
#   * powerbi-playbook plugin: repo-local skills still load in this repo; the critical bundle
#     checks above enforce correctness when the installed plugin is present and shadowing the repo.
#   * powerbi-modeling-mcp: useful authoring accelerator; local PBIP/TMDL edits can still proceed.
#   * Power BI Desktop version drift: no check; open_desktop.py looks up the installed Desktop at
#     each launch, so an update needs no action.
# --- MCP servers ---
$userMcp = Read-CopilotJson 'mcp-config.json'
$workspaceMcp = $null
$workspaceMcpPath = Join-Path $repoRoot '.mcp.json'
if (Test-Path -LiteralPath $workspaceMcpPath -PathType Leaf) {
    try {
        $workspaceMcp = Get-Content -LiteralPath $workspaceMcpPath -Raw | ConvertFrom-Json -ErrorAction Stop
    }
    catch { $workspaceMcp = $null }
}
foreach ($srv in @(@('powerbi-modeling-mcp', 'recommended'), @('powerbi-remote', 'optional'))) {
    $workspaceHas = [bool]($workspaceMcp -and $workspaceMcp.mcpServers.($srv[0]))
    $userHas = [bool]($userMcp -and $userMcp.mcpServers.($srv[0]))
    $has = $workspaceHas -or $userHas
    Add-Check "mcp: $($srv[0])" $srv[1] $has `
        $(if ($workspaceHas) { 'configured in .mcp.json' } elseif ($userHas) { "configured in $copilot\mcp-config.json" } else { 'not configured' }) `
        "Add via /mcp, or configure in .mcp.json (repository) or $copilot\mcp-config.json (user)."
}

Add-Cli 'npx' 'critical' 'Install Node.js; npx runs the powerbi-modeling MCP and the Desktop Bridge CLI.' `
    -DoctorId 'npx' -DoctorRemedy 'install_node'
Add-Cli 'powerbi-desktop' 'critical' 'npm install -g @microsoft/powerbi-desktop-bridge-cli - Desktop Bridge for open/reload/screenshot verification.' `
    -DoctorId 'desktop_bridge' -DoctorRemedy 'install_desktop_bridge'

# --- Is anything NEWER available upstream? (-CheckUpstream, opt-in) -------------------------------
#
# Everything above compares an installed version against a HARD-CODED number. That answers "is what I
# have good enough", never "has the world moved". Measured 2026-08-06, that gap bit twice in one day:
#
#   * the deterministic engine went 2.60.0 -> 2.72.0 unnoticed, and issues were nearly filed against
#     behaviour it had already replaced -- caught only by running `git fetch` by hand;
#   * Power BI Desktop auto-updated and silently broke the bridge's exe discovery (see below).
#
# Deliberately OPT-IN and deliberately ADVISORY:
#   * it needs the network and costs ~5s (measured), and the orchestrator runs plain preflight on
#     EVERY invocation - a mandatory network round trip there would be a tax on every migration;
#   * being behind is NOT an error. The timing rule still governs: upgrading mid-migration is worse
#     than being slightly old. So this never fails the run and never upgrades anything - it tells you
#     what to look at BETWEEN migrations.
function Get-UpstreamVerdict([string]$Installed, [string]$Upstream, [string]$UpdateHint) {
    $v = [pscustomobject]@{ Kind = 'unknown'; Tier = 'optional'; Ok = $false
        Detail = 'unknown (upstream or installed version unavailable/invalid)'; Hint = '' }
    $iv = Get-VersionFloorVerdict $Installed '0.0.0' '0.0.0' ''
    $uv = Get-VersionFloorVerdict $Upstream '0.0.0' '0.0.0' ''
    if ($iv.Tier -eq 'critical' -or $uv.Tier -eq 'critical') { return $v }
    if ([version]$Installed -lt [version]$Upstream) {
        $v.Kind = 'behind'; $v.Detail = "$Installed installed, $Upstream available"; $v.Hint = $UpdateHint
    }
    elseif ([version]$Installed -eq [version]$Upstream) {
        $v.Kind = 'current'; $v.Ok = $true; $v.Detail = "$Installed (current)"
    }
    else { $v.Kind = 'ahead'; $v.Ok = $true; $v.Detail = "$Installed (ahead of upstream $Upstream)" }
    return $v
}

if ($CheckUpstream) {
    foreach ($cliName in $cliFloor.Keys) {
        $installed = Get-CliVersion $cliName
        if (-not $installed) { continue }
        $latest = ConvertFrom-CliVersionOutput @(& npm view $cliPackage[$cliName] version 2>$null)
        $uv = Get-UpstreamVerdict $installed $latest "Between migrations only: npm install -g $($cliPackage[$cliName])@latest, then re-verify version-specific behaviour."
        Add-Check "upstream: $cliName" $uv.Tier $uv.Ok $uv.Detail $uv.Hint
    }

    $latestPlugin = $null
    try {
        $catalog = (Invoke-WebRequest -Uri $pluginPolicy.upstreamUrl -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop).Content | ConvertFrom-Json -ErrorAction Stop
        $matches = @($catalog.plugins | Where-Object { $_.name -ceq $pluginPolicy.name })
        if ($matches.Count -eq 1) { $latestPlugin = $matches[0].version }
    }
    catch { $latestPlugin = $null }
    $uv = Get-UpstreamVerdict $plugin.version $latestPlugin $pluginPolicy.hint
    Add-Check 'upstream: powerbi-authoring plugin' $uv.Tier $uv.Ok $uv.Detail $uv.Hint

    # The deterministic engine is an unpacked marketplace plugin with no `.git`, so there is no local
    # SHA to compare. Ask upstream for the VERSION file itself - one HTTP GET, and it compares the
    # thing that actually changes behaviour rather than a commit id nobody can interpret.
    if ($engineStatus -and $engineStatus.present) {
        $latestEngine = $null
        try {
            $latestEngine = ((Invoke-WebRequest -Uri $engineStatus.upstream_version_url -UseBasicParsing -TimeoutSec 15).Content).Trim()
        }
        catch { $latestEngine = $null }
        if ($latestEngine -match '^\d+(\.\d+)+$' -and $engineStatus.version -match '^\d+(\.\d+)+$') {
            $behindEngine = [version]$engineStatus.version -lt [version]$latestEngine
            Add-Check 'upstream: conversion engine' 'optional' (-not $behindEngine) `
                $(if ($behindEngine) { "$($engineStatus.version) installed, $latestEngine upstream" } else { "$($engineStatus.version) (current)" }) `
                'Between migrations only, and BETWEEN Copilot sessions (the plugin dir is file-locked while one runs): copilot plugin update tableau-fabric-skills@tableau-collection. Mid-session content-only refresh: python scripts/sync_engine_plugin.py --source <checkout>. Then RE-VERIFY any open issue against the new build before citing it.'
        }
    }
}


# Get-DesktopPinVerdict: PBI_DESKTOP_PATH is optional. open_desktop.py and the Desktop check below use
# it only when its file exists; otherwise they look up the installed Desktop at each launch. A versioned
# Store path dies on every Desktop auto-update, and a dead pin is ignored, so it is a WARN, never a
# blocker. A function so tests/test_preflight_contract.py can execute all three states (unset / valid
# / dead) without a real Desktop install, mirroring Get-TenantVerdict below.
function Get-DesktopPinVerdict([bool]$Configured, [bool]$Valid) {
    $dead = $Configured -and -not $Valid
    $detail = if ($Valid) {
        'set; its file is used at each launch'
    } elseif ($dead) {
        'ignored; the installed Desktop is used'
    } else {
        'not set; the installed Desktop is looked up at each launch'
    }
    $hint = if ($dead) { 'Unset PBI_DESKTOP_PATH, or point it at an existing PBIDesktop.exe.' } else { '' }
    [pscustomobject]@{ Tier = 'recommended'; Ok = -not $dead; Detail = $detail; Hint = $hint }
}

# Launch Desktop through open_desktop.py, never through the bridge's `open`. Measured on bridge 1.0.0
# (`powerbi-desktop`): its exe lookup tries PBI_DESKTOP_PATH, then fixed Program Files and
# %LOCALAPPDATA%\Microsoft\WindowsApps paths, and does not find a Store install. open_desktop.py looks
# up the installed Desktop at each launch: a valid pin, else the Store install, else the classic one.
# This check uses the same order.
$desktop = $null
$desktopVia = ''
$desktopPathConfigured = [bool]$env:PBI_DESKTOP_PATH
$desktopPathValid = $desktopPathConfigured -and (Test-Path -LiteralPath $env:PBI_DESKTOP_PATH -PathType Leaf)
if ($desktopPathValid) { $desktop = $env:PBI_DESKTOP_PATH; $desktopVia = 'PBI_DESKTOP_PATH' }
if (-not $desktop) {
    $loc = (Get-AppxPackage Microsoft.MicrosoftPowerBIDesktop -ErrorAction SilentlyContinue).InstallLocation
    if ($loc -and (Test-Path (Join-Path $loc 'bin\PBIDesktop.exe'))) {
        $desktop = (Join-Path $loc 'bin\PBIDesktop.exe')
        $desktopVia = 'Store install'
    }
}
if (-not $desktop) {
    $programFiles = if ($env:ProgramFiles) { $env:ProgramFiles } else { 'C:\Program Files' }
    $classic = Join-Path $programFiles 'Microsoft Power BI Desktop\bin\PBIDesktop.exe'
    if (Test-Path $classic) { $desktop = $classic; $desktopVia = 'classic install' }
}
Add-Check 'Power BI Desktop' 'critical' ([bool]$desktop) `
    $(if ($desktop) { "$desktop (via $desktopVia)" } else { 'not found' }) `
    'Install Power BI Desktop (Store/MSIX preferred) - needed for the refresh + screenshot verification loop.' `
    -DoctorId 'desktop_install' -DoctorReason 'executable_missing' -DoctorRemedy 'install_desktop'

$pinVerdict = Get-DesktopPinVerdict -Configured $desktopPathConfigured -Valid $desktopPathValid
Add-Check 'PBI_DESKTOP_PATH (optional Desktop pin)' $pinVerdict.Tier $pinVerdict.Ok $pinVerdict.Detail $pinVerdict.Hint

# --- Privacy Levels: a MANUAL prerequisite this script cannot verify -------------------------------
# Opening a model that spans more than one data source raises a modal ("Potential security risk: This
# file uses multiple data sources...") BEFORE the model loads. Federated datasources are normal in real
# Tableau workbooks, so most migrations hit it.
#
# It is worse for an agent than for a person: it blocks at LOAD, so it stalls before any refresh call
# and no automation can dismiss it. Measured 2026-08-05, a run sat past 450s on this while
# refresh_pbip_model.py's own refresh timeout never fired - that timeout covers the refresh, not the
# open. To a supervising agent it looks like a hang with no error.
#
# This is stated rather than checked ON PURPOSE. Desktop ships as an MSIX package and keeps the setting
# in the package's private settings.dat hive, which needs SeRestorePrivilege to load and is locked while
# Desktop runs; there is no supported read path. Asserting a check we cannot actually perform would be
# worse than an honest reminder.
Add-Check 'Privacy Levels (manual)' 'optional' $true `
    'VERIFY BY HAND: Options > Global > Privacy > "Always ignore Privacy Level settings"' `
    'Without it, any MULTI-SOURCE model blocks on a modal at open and an unattended refresh hangs with no error.'

# --- .NET SDK (builds tools/tmdl_oracle, the TMDL gate's parser) ---
# NOTE: this replaced an older check for Microsoft.AnalysisServices.Tabular.dll under
# the Copilot home's installed-plugins. The powerbi-authoring plugin no longer bundles Tabular Editor, so that
# check could never pass. The .NET SDK is needed only to build/run the offline TMDL oracle; the
# Desktop probe/refresh loads the ADOMD client that ships with Desktop (checked below).
#
# Since #254 this is load-bearing for a GATE, not only for an optional validator:
# `check_datamodel.py` runs the TMDL oracle (tools/tmdl_oracle) to ask TmdlSerializer itself whether
# each model parses. Without `dotnet` the gate reports UNASSESSABLE (exit 3) instead of a pass.
Add-Cli 'dotnet' 'critical' 'Install the .NET SDK - needed to build/run the TMDL oracle (tools/tmdl_oracle) that check_datamodel.py uses. Issue #413 retired 4 of the 5 redundant per-example tmdl_validate copies; the quadruple-axis-charts one remains (its unique DAX [bracket]-reference check has no replacement yet) - dotnet is needed for both.' `
    -DoctorId 'dotnet' -DoctorRemedy 'install_dotnet'

# --- ADOMD client bundled with Desktop (the pbip-model-refresh skill's live-Desktop probe + refresh) ---
# probe_desktop_query.py and refresh_pbip_model.py load Microsoft.PowerBI.AdomdClient.dll from beside
# the running Desktop, through pythonnet on .NET Framework. So the check is the same load, run by
# `open_desktop.py --check` in the Python that runs the toolkit, against the Desktop this script found.
#
# Severity: CRITICAL, by this file's own rule above — "critical if any persona's Definition of Done
# depends on it, even when the dependency only fails later at handoff/validation time". It does, and
# unconditionally: `pbi-semantic-builder`'s DoD step 8 is a HANDOFF GATE that runs
# refresh_pbip_model.py to refresh and SAVE `.pbi/cache.abf`, and `pbi-report-builder`'s step 1
# refuses to open Desktop without that file. So every migration that ends in a report needs ADOMD.
# A preflight that prints "Ready to migrate" while the probe cannot load the client is the silent
# false-green #199 was filed to remove. Do NOT downgrade this to recommended without also removing
# the refresh/save gate from those two personas.
$adomdOk = $false
$adomdReason = 'pythonnet_missing'
$adomdDetail = 'not verified (Python unavailable)'
if ($py) {
    & python -c "import pythonnet" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $adomdDetail = 'pythonnet is not installed in this Python'
    } else {
        $adomdReason = 'assembly_missing'
        $adomdLines = @(& python (Join-Path $repoRoot 'scripts\open_desktop.py') --check 2>$null | Where-Object { "$_".Trim() })
        $adomdLast = if ($adomdLines.Count) { "$($adomdLines[-1])".Trim() } else { 'open_desktop.py --check reported nothing' }
        $adomdDetail = $adomdLast
        if ($LASTEXITCODE -eq 0) {
            try {
                $adomdVersions = $adomdLast | ConvertFrom-Json
                $adomdDetail = "Desktop $($adomdVersions.desktop_version) ($($adomdVersions.exe_source)), ADOMD $($adomdVersions.adomd_version)"
                $adomdOk = $true
            } catch { $adomdOk = $false }
        }
    }
}
Add-Check 'ADOMD client (bundled with Desktop)' 'critical' $adomdOk $adomdDetail `
    'Install pythonnet into the Python that runs the toolkit; otherwise install or repair Power BI Desktop. Then re-run.' `
    -DoctorId 'adomd_client' -DoctorReason $adomdReason -DoctorRemedy 'restore_adomd'

Add-Cli 'uv' 'optional' 'Install uv for env/dependency management (uv venv && uv sync).'
Add-Cli 'az' 'optional' 'Azure CLI - only for Fabric REST / token-based operations.'

# --- Fabric token TENANT: a token that mints successfully can still be for the WRONG tenant (#124) --
#
# Measured 2026-08-13, FOUR times across two independent operators, ~15 minutes each: on a
# multi-account machine `az account get-access-token` succeeds and hands back a token for whatever
# tenant the CLI's default context points at. `GET /workspaces/{id}` then answers `WorkspaceNotFound`
# for a workspace that exists and was just filled with 74 items - a 404 that reads as "your deploy
# went somewhere else" rather than as an identity problem, and it lands in the phase where you are
# reassuring a customer. The runbook's whole Fabric-side credential guidance was "the token must
# mint", which is exactly the thing that was already true.
#
# This belongs in preflight rather than in a doc note because it is the class of failure preflight
# exists for: deterministic, checkable BEFORE any work, and expensive to diagnose afterwards.
#
# The TOKEN is the ground truth, not the CLI's profile: `tid` is the tenant the resource will
# actually see. A JWT's payload is its middle dot-separated segment, base64url-encoded, so .NET
# decodes it with no new dependency.
#
# NEVER print, log or truncate the token itself. Only the decoded `tid` - an identifier, not a
# secret - reaches the output.
function Get-JwtTenantId([string]$Token) {
    # base64url is not base64: it swaps '+/' for '-_' and drops the '=' padding, both of which
    # [Convert]::FromBase64String rejects. A length of 1 mod 4 cannot be valid base64 at all.
    if (-not $Token) { return $null }
    $seg = ($Token -split '\.')[1]
    if (-not $seg) { return $null }
    $b64 = $seg.Replace('-', '+').Replace('_', '/')
    switch ($b64.Length % 4) { 1 { return $null } 2 { $b64 += '==' } 3 { $b64 += '=' } }
    try { return ((([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))) | ConvertFrom-Json).tid) }
    catch { return $null }
}

function Remove-SurroundingQuotes([string]$Value) {
    # Be LIBERAL in what you accept, strict in what you compare. `KEY="value"` is ordinary dotenv
    # spelling - it is what `.env.example` files teach and what python-dotenv, docker and every
    # shell `source` accept - so a quoted value is an operator SPELLING, never a different value.
    # Measured 2026-08-13: without this, `FABRIC_TENANT_ID="72f988bf-..."` naming the CORRECT tenant
    # produced `MISS WRONG TENANT: token is for 72f988bf-..., intended "72f988bf-..."` and exit 1 on
    # a perfectly configured machine - a false blocker in front of a customer, which is worse than
    # the false blocker this same change set removed.
    # Only a MATCHED pair is stripped, and only the outermost one, so a value that legitimately
    # contains a quote survives untouched.
    if (-not $Value) { return '' }
    $v = $Value.Trim()
    if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) {
        $v = $v.Substring(1, $v.Length - 2).Trim()
    }
    return $v
}

function ConvertFrom-DotEnvValue([string]$Raw) {
    # A .env value, read the way every other dotenv consumer reads one: matched surrounding quotes
    # are a SPELLING, and an inline `# comment` after the value is a comment. Both are ordinary -
    # `FABRIC_TENANT_ID=72f9... # customer tenant` is the natural thing to write next to a GUID
    # nobody can recognize by sight - and neither is part of the value.
    #
    # Inside quotes, a '#' is DATA. Outside them, a comment starts at the beginning of the value or
    # after whitespace, so `abc#def` stays whole: over-stripping a legitimate value would be the
    # same class of bug (quiet corruption of a declaration) as not stripping at all.
    #
    # The quoted form only wins when the closing quote actually ENDS the value (bar whitespace or a
    # comment) - so the closer is the first candidate that satisfies that, which is what every
    # dotenv reader does. `""guid""` and `"a"b"` therefore have no valid closer until their final
    # quote and stay visibly malformed, rather than silently decoding to '' or to a fragment.
    $v = ([string]$Raw).Trim()
    if ($v.Length -ge 2) {
        $q = $v.Substring(0, 1)
        if ($q -eq '"' -or $q -eq "'") {
            for ($i = 1; $i -lt $v.Length; $i++) {
                if ($v[$i] -ne $q) { continue }
                $tail = $v.Substring($i + 1).Trim()
                if (-not $tail -or $tail.StartsWith('#')) { return $v.Substring(1, $i - 1) }
            }
        }
    }
    $comment = [regex]::Match($v, '(^|\s)#')
    if ($comment.Success) { $v = $v.Substring(0, $comment.Index) }
    return (Remove-SurroundingQuotes $v)
}

function Get-DotEnvValue([string]$Key, [string]$Path) {
    # The same minimal KEY=VALUE scan as scripts/tableau_env.py's load_env (trim, skip blank/'#',
    # split on the FIRST '='), plus the deliberate divergence in ConvertFrom-DotEnvValue above.
    # That divergence is the fix, not an accident - the two parsers previously agreed by being wrong
    # in the same way, and "identical to Python" was the argument that kept it.
    # `scripts/tableau_env.py:load_env` still reads values the old way; it is owned elsewhere and
    # nothing asks it for a Fabric key, so that divergence stays inert. The Python reader that is NOT
    # inert is `deploy_estate.py:_dotenv_value`, which mirrors THIS function on purpose and is pinned
    # to the same tests/fixtures/dotenv-spellings.env table - measured while it was not: preflight
    # reported a workspace reachable that the deploy then mangled into a late WorkspaceNotFound.
    # .env is git-ignored, which is where a real customer tenant id belongs: this repository is public.
    if (-not $Path) { $Path = Join-Path $repoRoot '.env' }
    if (-not (Test-Path $Path)) { return $null }
    foreach ($line in (Get-Content $Path)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#') -or ($trimmed -notmatch '=')) { continue }
        $pair = $trimmed -split '=', 2
        if ($pair[0].Trim() -eq $Key) { return (ConvertFrom-DotEnvValue $pair[1]) }
    }
    return $null
}

function Resolve-IntendedTenant([string]$FromFlag, [string]$FromEnv, [string]$FromDotEnv) {
    # Declaration of intent, in the order a caller expects to win: flag > exported env > .env.
    # A function rather than an inline pipeline because WHICH channel won decides whether a mismatch
    # blocks, and because normalizing only two of the three channels is a silent way to reintroduce
    # the quoting bug through the third (measured: a quoted $env:FABRIC_TENANT_ID then fails the
    # GUID guard and degrades to "no token could be minted" - the same false negative wearing a
    # different message). Here it is one line, executed by the tests.
    $candidates = @($FromFlag, $FromEnv, $FromDotEnv) | ForEach-Object { Remove-SurroundingQuotes $_ }
    $tenant = $candidates | Where-Object { $_ } | Select-Object -First 1
    # "$tenant" rather than [string]$tenant: an empty pipeline yields AutomationNull, whose [string]
    # cast survives as null through ConvertTo-Json and would make "nothing declared" indistinguishable
    # from a serialization accident to any caller inspecting the object.
    return [pscustomobject]@{ Tenant = "$tenant"; IsExplicit = [bool]$candidates[0] }
}

function Test-TenantIdShape([string]$Value) {
    # The `tid` claim is ALWAYS a GUID, so only a GUID can be compared against it.
    return [bool]($Value -match '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
}

function Resolve-TenantIdFromDomain([string]$Domain) {
    # `contoso.onmicrosoft.com` is a legitimate spelling everywhere else in this toolchain - both
    # `az --tenant` and `deploy_estate.py --tenant` accept it - so an operator has every reason to
    # put one in -Tenant or FABRIC_TENANT_ID. Answering "cannot compare" to that is a CHOICE, not a
    # necessity: the CLI already holds the mapping, and `az account list --all` returns
    # tenantDefaultDomain next to tenantId. Giving up would leave a declared, blocking-channel
    # intent completely unverified under a green "Ready to migrate".
    #
    # Resolution is best-effort by design: a VANITY domain (contoso.com) is not the default domain
    # and will not be found, and a tenant the profile has never seen cannot be mapped. Those fall
    # back to "cannot compare", which is honest - it is silence that is not.
    if (-not $Domain) { return '' }
    $listed = & az account list --all -o json 2>$null
    if (-not $listed) { return '' }
    try { $accounts = (($listed | Out-String) | ConvertFrom-Json) } catch { return '' }
    # -eq is case-insensitive, which is right for a DNS name.
    $hit = $accounts | Where-Object { $_.tenantDefaultDomain -eq $Domain } | Select-Object -First 1
    if ($hit -and (Test-TenantIdShape $hit.tenantId)) { return [string]$hit.tenantId }
    return ''
}

function Get-TenantVerdict {
    <#
      THE decision. Everything above this gathers evidence; this function alone turns it into
      Ok/Tier/Detail/Summary, so the one line that decides the verdict can be EXECUTED by the test
      harness rather than matched as a source string. That matters more than usual here: CI cannot
      run PowerShell, and a mutation test showed both `$tenantOk = $true` (the check can never fire)
      and an inverted comparison (fires on every correct machine) surviving the whole suite while it
      was inline.

      Kind is part of the contract: the caller maps it to a hint, and a Kind with no hint branch is
      a check that renders as silence - the false-green shape this script exists to prevent.
    #>
    param(
        [string]$IntendedTenant,
        [string]$ActualTenant,
        [bool]$IntentIsExplicit,
        [bool]$AzPresent,
        [string]$Scope = '',
        [string]$DeclaredAs = ''
    )
    # Normalized HERE, not only at the call site, so the comparator is correct in isolation: it is
    # the unit the tests execute, and a caller that forgets cannot manufacture a false WRONG TENANT.
    # Idempotent, so doing it twice costs nothing.
    $IntendedTenant = Remove-SurroundingQuotes $IntendedTenant
    $ActualTenant = Remove-SurroundingQuotes $ActualTenant
    $DeclaredAs = Remove-SurroundingQuotes $DeclaredAs
    # Only worth showing when the operator wrote something other than the id being compared - i.e.
    # when a domain was resolved to a GUID. Otherwise it is the same string twice.
    $as = if ($DeclaredAs -and $DeclaredAs -ne $IntendedTenant) { " (declared as $DeclaredAs)" } else { '' }

    # An intent DECLARED FOR THIS RUN that could not be verified is not a clean bill of health: it
    # is a blank space where the operator asked for a check. OPTIONAL is where such a line goes to
    # be ignored - measured: `-Tenant contoso.onmicrosoft.com` produced zero verification, filed
    # under OPTIONAL, beneath "Ready to migrate". Configuration-declared intent stays optional,
    # because it is a standing preference rather than a statement about this run.
    $unverified = if ($IntentIsExplicit) { 'recommended' } else { 'optional' }

    if (-not $IntendedTenant) {
        return [pscustomobject]@{ Kind = 'no-intent'; Ok = $false; Tier = 'optional'; Summary = ''
            Detail                    = 'no intended tenant declared - not checked'
        }
    }
    if (-not (Test-TenantIdShape $IntendedTenant)) {
        return [pscustomobject]@{ Kind = 'malformed'; Ok = $false; Tier = $unverified
            Detail                     = "intended '$IntendedTenant' is not a tenant GUID and could not be resolved to one - not compared"
            Summary                    = if ($IntentIsExplicit) { "tenant NOT VERIFIED: '$IntendedTenant' could not be resolved to a GUID" } else { '' }
        }
    }
    if (-not $AzPresent) {
        return [pscustomobject]@{ Kind = 'no-az'; Ok = $false; Tier = $unverified
            Detail                     = "intended $IntendedTenant$as - not verified (az not on PATH)"
            Summary                    = if ($IntentIsExplicit) { "tenant NOT VERIFIED: az not on PATH (intended $IntendedTenant)" } else { '' }
        }
    }
    if (-not $ActualTenant) {
        return [pscustomobject]@{ Kind = 'no-token'; Ok = $false; Tier = $unverified
            Detail                     = "intended $IntendedTenant$as - no Fabric token could be minted or decoded$Scope"
            Summary                    = if ($IntentIsExplicit) { "tenant NOT VERIFIED: no Fabric token could be minted (intended $IntendedTenant)" } else { '' }
        }
    }

    # The verdict. `-eq` on strings is case-insensitive in PowerShell, which is exactly right for a
    # GUID: the portal shows one casing, the `tid` claim another, and neither is a wrong tenant.
    # Do NOT "tighten" this to -ceq.
    $tenantOk = ($ActualTenant -eq $IntendedTenant)

    if ($tenantOk) {
        return [pscustomobject]@{ Kind = 'match'; Ok = $true; Tier = 'optional'; Summary = ''
            Detail                     = "tid matches intended $IntendedTenant$as$Scope"
        }
    }
    # A mismatch blocks only when THIS run declared the tenant on the command line. The check is
    # opt-in either way, but the two opt-ins are not the same statement: `-Tenant <id>` is "I am
    # pointing at that tenant right now", while FABRIC_TENANT_ID in `.env` is a persisted preference
    # that survives every later run - including a parse-only estate sweep whose steps 1-6 never call
    # Fabric. Blocking those would re-create, from the other side, the false blocker this change set
    # removed (a Desktop-only pin failing a run that never opens Desktop). A WARN still names the
    # problem loudly, and the deploy path - which does pass --tenant - is where exit 1 belongs.
    $tier = if ($IntentIsExplicit) { 'critical' } else { 'recommended' }
    $detail = "WRONG TENANT: token is for $ActualTenant, intended $IntendedTenant$as$Scope"
    if (-not $IntentIsExplicit) { $detail += ' [warning only: declared by configuration, not by -Tenant on this run]' }
    return [pscustomobject]@{ Kind = 'mismatch'; Ok = $false; Tier = $tier; Detail = $detail
        Summary                     = "WRONG TENANT: token is for $ActualTenant, intended $IntendedTenant$as"
    }
}

# Mirrors deploy_estate.py's FABRIC_RESOURCE. Duplicating a well-known constant is a smaller risk
# than making this PowerShell bootstrap depend on importing Python (see the engine block above for
# the case where re-deriving a LIST would have been the real defect); if it ever changed, the deploy
# would fail loudly on its own.
$fabricResource = 'https://api.fabric.microsoft.com'

$intent = Resolve-IntendedTenant $Tenant $env:FABRIC_TENANT_ID (Get-DotEnvValue 'FABRIC_TENANT_ID')
$intendedTenant = $intent.Tenant
$declaredAs = $intent.Tenant
$workspace = @($env:FABRIC_WORKSPACE_ID, (Get-DotEnvValue 'FABRIC_WORKSPACE_ID')) |
    ForEach-Object { Remove-SurroundingQuotes $_ } | Where-Object { $_ } | Select-Object -First 1
$azPresent = [bool](Get-Command az -ErrorAction SilentlyContinue)
$scoped = if ($Subscription) { " (scoped: --subscription $Subscription)" } else { ' (default az context)' }
$actualTenant = ''
$fabricToken = ''

# A declared non-GUID gets RESOLVED before it gets given up on (see Resolve-TenantIdFromDomain).
# Still behind the declaration guard, so an undeclared run pays nothing.
if ($intendedTenant -and -not (Test-TenantIdShape $intendedTenant) -and $azPresent) {
    $resolved = Resolve-TenantIdFromDomain $intendedTenant
    if ($resolved) { $intendedTenant = $resolved }
}

# The mint sits behind the same guard: no declared tenant - or one that still cannot be compared -
# means no token call at all, so an operator who only parses workbooks pays nothing. Preflight runs
# before EVERY migration, so a mandatory token mint here would tax every run for a check that has
# nothing to compare against.
if ((($intendedTenant -and (Test-TenantIdShape $intendedTenant)) -or $workspace) -and $azPresent) {
    $azArgs = @('account', 'get-access-token', '--resource', $fabricResource, '-o', 'json')
    # --subscription scopes ONE call. It is offered here so the fix can be verified with the same
    # command shape that applies it, rather than by mutating the CLI profile and hoping.
    if ($Subscription) { $azArgs += @('--subscription', $Subscription) }
    # $tokenJson holds a BEARER TOKEN. It is never echoed, never added to a Detail, and az's own
    # stderr is discarded rather than surfaced, so no code path can leak it.
    $tokenJson = & az @azArgs 2>$null
    if ($tokenJson) {
        try {
            $fabricToken = (($tokenJson | Out-String) | ConvertFrom-Json).accessToken
            $actualTenant = Get-JwtTenantId $fabricToken
        }
        catch { $actualTenant = ''; $fabricToken = '' }
    }
}

$verdict = Get-TenantVerdict -IntendedTenant $intendedTenant -ActualTenant $actualTenant `
    -IntentIsExplicit $intent.IsExplicit -AzPresent $azPresent -Scope $scoped -DeclaredAs $declaredAs

# One hint per Kind. Anything that needs an extra `az` call lives in its own branch, so the failure
# path is the only one that pays for it.
$tenantHint = switch ($verdict.Kind) {
    'no-intent' {
        'Declare the tenant you deploy into and this becomes automatic: -Tenant <id> (blocking, for a run that is about to deploy), $env:FABRIC_TENANT_ID, or FABRIC_TENANT_ID in the git-ignored .env (both warn-only). Same id you pass to deploy_estate.py --tenant. Until then, a WorkspaceNotFound on a workspace you know exists may simply be a token for another tenant.'
    }
    'malformed' {
        'Preflight resolves a default domain (contoso.onmicrosoft.com) against `az account list --all`, so this one is either a VANITY domain, a tenant this machine has never signed in to, or a placeholder that was never filled in. Give the GUID instead: az account show --query tenantId -o tsv (or Entra > Overview). Note `az --tenant` and deploy_estate.py DO accept a domain, so this is a preflight-only requirement - and it never blocks.'
    }
    'no-az' {
        'Install the Azure CLI to verify which tenant this machine actually mints Fabric tokens for.'
    }
    'no-token' {
        'Run `az login`, then re-run preflight. (Preflight never prints the token - only its decoded `tid` claim.)'
    }
    'mismatch' {
        # Turn the advice into something copy-pasteable: name a subscription this machine can
        # already see INSIDE the intended tenant. Costs one extra `az` call, on this path only.
        $exampleSub = (& az account list --all --query "[?tenantId=='$intendedTenant'].id" -o tsv 2>$null | Select-Object -First 1)
        $subHint = if ($exampleSub) { "e.g. --subscription $exampleSub" } else { '--subscription <a subscription inside that tenant>' }
        $escalation = if ($verdict.Tier -eq 'critical') { '' } else { ' This is a WARNING rather than a blocker because the intended tenant came from configuration ($env:FABRIC_TENANT_ID or .env), not from -Tenant on this run; pass -Tenant <id> on the run that actually deploys and the same mismatch fails preflight.' }
        "This is an IDENTITY problem, not a missing workspace: Fabric will answer WorkspaceNotFound/EntityNotFound for items that exist. Prefer the non-mutating fix, which scopes a single call: az account get-access-token --resource $fabricResource $subHint (re-run preflight with -Subscription <same> to confirm). ``az account set`` also works but rewrites the CLI profile on disk, so every other process on this machine silently follows you - restore it afterwards if you use it. Note deploy_estate.py --tenant is passed to az verbatim and inherits the same ambiguity (az may resolve a different ACCOUNT against that tenant and answer AADSTS90072).$escalation"
    }
    default { '' }
}

Add-Check 'Fabric token tenant' $verdict.Tier $verdict.Ok $verdict.Detail $tenantHint `
    -DoctorId 'fabric_tenant' -DoctorReason 'tenant_mismatch' -DoctorRemedy 'scope_intended_tenant'

$workspaceCheck = if (-not $workspace) {
    [pscustomobject]@{ Tier = 'optional'; Ok = $false; Detail = 'no landing-zone workspace declared - not checked'
        Hint = 'Set FABRIC_WORKSPACE_ID in the git-ignored .env so deploy_estate.py can use it and preflight can verify it.' }
}
elseif ($workspace -notmatch '^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$') {
    [pscustomobject]@{ Tier = 'recommended'; Ok = $false; Detail = "workspace '$workspace' is not a GUID - not checked"
        Hint = 'Copy the landing-zone workspace id from the Fabric URL or workspace settings into FABRIC_WORKSPACE_ID.' }
}
elseif (-not $azPresent) {
    [pscustomobject]@{ Tier = 'recommended'; Ok = $false; Detail = "workspace $workspace - not verified (az not on PATH)"
        Hint = 'Install the Azure CLI and run az login so preflight can verify the landing-zone workspace.' }
}
elseif (-not $fabricToken) {
    [pscustomobject]@{ Tier = 'recommended'; Ok = $false; Detail = "workspace $workspace - no Fabric token could be minted"
        Hint = 'Run az login, then re-run preflight to verify the landing-zone workspace.' }
}
else {
    try {
        $workspaceHeaders = @{}
        $workspaceHeaders['Authorization'] = 'Bearer ' + $fabricToken
        Invoke-WebRequest -Uri "$fabricResource/v1/workspaces/$workspace" `
            -Headers $workspaceHeaders `
            -UseBasicParsing -ErrorAction Stop | Out-Null
        [pscustomobject]@{ Tier = 'optional'; Ok = $true; Detail = "workspace $workspace is reachable"; Hint = '' }
    }
    catch {
        $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        $detail = switch ($status) {
            404 { "workspace $workspace does not exist (or this identity cannot see it)" }
            403 { "no access to workspace $workspace - this identity needs the Contributor role" }
            default { "workspace $workspace could not be verified (HTTP $status)" }
        }
        [pscustomobject]@{ Tier = 'recommended'; Ok = $false; Detail = $detail
            Hint = 'Verify FABRIC_WORKSPACE_ID and the current Azure CLI identity, then re-run preflight.' }
    }
}
Add-Check 'Fabric landing-zone workspace' $workspaceCheck.Tier $workspaceCheck.Ok $workspaceCheck.Detail $workspaceCheck.Hint

$odbc = (Get-OdbcDriver -ErrorAction SilentlyContinue | Where-Object Name -like '*SQL Server*').Name
Add-Check 'ODBC Driver 18 (SQL)' 'optional' ($odbc -contains 'ODBC Driver 18 for SQL Server') `
    $(if ($odbc) { ($odbc | Select-Object -Unique) -join '; ' } else { 'none' }) `
    'Only for direct SQL Analytics Endpoint / Warehouse queries.'

# --- One aggregate feeds both presentations and the unchanged native exit. ---
$summary = Get-PreflightSummary $results
$criticalMissing = $summary.critical_missing
$recommendedWarnings = $summary.recommended_warnings
if ($DoctorDiagnostics) {
    Write-Output (ConvertTo-Json -InputObject $summary -Depth 4 -Compress)
    exit ([int]($criticalMissing -gt 0))
}

# --- Render ---
foreach ($tier in @('critical', 'recommended', 'optional')) {
    Write-Host ''
    Write-Host "== $($tier.ToUpper()) =="
    foreach ($r in ($results | Where-Object { $_.Tier -eq $tier })) {
        $mark = if ($r.Ok) { 'OK  ' } elseif ($tier -eq 'critical') { 'MISS' } else { 'WARN' }
        Write-Host ("  [{0}] {1,-44} {2}" -f $mark, $r.Name, $r.Detail)
        if (-not $r.Ok -and $tier -eq 'critical') { Write-Host "         -> $($r.Hint)" }
        elseif (-not $r.Ok -and $tier -eq 'recommended') { Write-Host "         (recommended) $($r.Hint)" }
        elseif (-not $r.Ok -and $r.Hint) { Write-Host "         (optional) $($r.Hint)" }
    }
}
Write-Host ''
# The tenant verdict is one line in the middle of ~40, and the OK lines that follow it push it off
# an 80x24 terminal: measured, a WRONG TENANT warning sat at line 22 of 38 while the only text still
# on screen read "Ready to migrate. 3 recommended warning(s) present." A count is not a diagnosis -
# name the tenant on the line that survives scrolling. Empty for a match, for an undeclared run, and
# for a merely configured intent that could not be verified, so the common case stays quiet.
$tenantNote = if ($verdict.Summary) { " $($verdict.Summary)." } else { '' }
if ($criticalMissing -gt 0) {
    $suffix = if ($recommendedWarnings -gt 0) { " ($recommendedWarnings recommended warning(s) also present)." } else { '' }
    Write-Host "PREFLIGHT: $criticalMissing critical item(s) missing - resolve before migrating.$suffix$tenantNote"
    exit 1
}
$suffix = if ($recommendedWarnings -gt 0) { " $recommendedWarnings recommended warning(s) present; review before relying on affected capabilities." } else { '' }
Write-Host "PREFLIGHT: all critical dependencies present. Ready to migrate.$suffix$tenantNote"
exit 0
