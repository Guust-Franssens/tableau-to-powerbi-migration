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

  Verifies: Python + the parser's Python deps, the deterministic conversion engine (the installed
  `tableau-fabric-skills` plugin, which is its SINGLE canonical source - a second copy anywhere is a
  hard failure, see issue #107), both skill plugins
  (powerbi-authoring@fabric-collection and powerbi-migration-skills@powerbi-migration-collection),
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
  (`FABRIC_TENANT_ID="72f9..."` is ordinary dotenv spelling, not a wrong tenant).

  WHERE the declaration came from decides how loudly a mismatch lands, because it is the only
  evidence preflight has of deploy INTENT for THIS run:
    * `-Tenant` on the command line  -> a mismatch is CRITICAL (exit 1). You said, in this
      invocation, that you are pointing at a tenant; a token for another one is a blocker.
    * `$env:FABRIC_TENANT_ID` / `.env` -> a mismatch is RECOMMENDED (a visible WARN, exit 0).
      Persisted configuration is a standing preference, not a statement that this run deploys -
      and steps 1-6 of an estate run never touch Fabric at all.

.PARAMETER Subscription
  Optional subscription id/name passed verbatim to `az account get-access-token --subscription`,
  so the tenant check can verify the NON-MUTATING fix for a wrong-tenant token before you rely on
  it. `az account set` fixes the same problem by rewriting the CLI's on-disk profile, which every
  other process on this machine then inherits.

.NOTES
  Run:  powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1 [-Update] [-Tenant <id>]
  Exit: 0 if every CRITICAL item is present; 1 if any CRITICAL item is missing.
        RECOMMENDED and OPTIONAL items are surfaced as warnings but do not stop a migration.
#>
#Requires -Version 5.1
param([switch]$Update, [switch]$CheckUpstream, [string]$Tenant, [string]$Subscription)

$ErrorActionPreference = 'SilentlyContinue'
$copilot = Join-Path $HOME '.copilot'
$repoRoot = Split-Path -Parent $PSScriptRoot
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
    # Below the floor is a real correctness failure; above known-good is only an advisory.
    Add-Check "version: $cliName" $(if ($floorOk) { 'optional' } else { 'critical' }) $(-not $drifted -and $floorOk) $detail $hint
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
# The decision: the INSTALLED PLUGIN is the single canonical engine. So a second tree is not a
# convenience to warn about, it is THE defect - hence critical, not recommended.
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
        $(if ($engineStatus.present) { "VERSION $($engineStatus.version) at $($engineStatus.root)" } else { "not installed at $($engineStatus.root)" }) `
        $engineStatus.install_hint
    Add-Check 'engine: single source' 'critical' ($alternatives.Count -eq 0) `
        $(if ($alternatives.Count) { "ALTERNATIVE COPY: $($alternatives -join '; ')" } else { 'plugin only' }) `
        'A second engine tree means two versions can build one pipeline with no record of which ran (#107). DELETE the alternative copy after confirming it has no uncommitted/unpushed work: git -C <path> status --porcelain; git -C <path> log --branches --not --remotes.'
}
else {
    Add-Check 'engine: single source' 'critical' $false `
        $(if ($py) { 'engine_source.py did not report - is scripts/engine_source.py present?' } else { 'not verified (Python unavailable)' }) `
        'Install Python 3.11+, then re-run. The engine single-source rule is enforced by scripts/engine_source.py --json.'
}

# --- Skill plugins ---
# `powerbi-authoring` is still checked by configured plugin identity, because it supplies the official
# planning/design/authoring/semantic-model skills the builder personas chain.
#
# This repo's reusable Power BI bundles are different: their marketplace/plugin name has changed once,
# and a hard-coded name let the installed copy drift silently. Discover that plugin by content instead:
# scan ~/.copilot/installed-plugins/*/*/skills for the bundle names emitted by build_plugin.py. Exactly
# one discovered install is acceptable; more than one is a critical shadowing hazard.
$cfg = Read-CopilotJson 'config.json'
$p = @{ name = 'powerbi-authoring'; market = 'fabric-collection'; tier = 'critical'
       hint = 'In Copilot: /plugin -> add marketplace microsoft/skills-for-fabric -> enable powerbi-authoring. See AGENTS.md.' }
$plugin = $null
if ($cfg -and $cfg.installedPlugins) {
    $plugin = $cfg.installedPlugins | Where-Object { $_.name -eq $p.name -and $_.marketplace -eq $p.market } | Select-Object -First 1
}
$pluginOk = $plugin -and (Test-Path $plugin.cache_path)
Add-Check "plugin: $($p.name)@$($p.market)" $p.tier ([bool]$pluginOk) `
    $(if ($pluginOk) { "v$($plugin.version)" } else { 'not installed/enabled' }) $p.hint

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
    Add-Check 'plugin: reusable Power BI skill bundles' 'critical' $false `
        $skillPlugin.detail `
        'Two installed copies of the same skill create an ambiguous shadowing order. Remove the duplicate before trusting skill output.'
}
elseif ($skillPlugin.status -eq 'missing') {
    # Deliberately recommended, not critical: with NO installed copy, this repo's local .github/skills
    # bundles remain available in this checkout. That is a capability/distribution gap for other repos,
    # not the measured false-green bug. STALE remains critical below because it silently executes code
    # from a different tree than the repo being inspected.
    Add-Check "plugin: $($skillPlugin.identity)" 'recommended' $false `
        'not installed/enabled' `
        $skillPlugin.install_hint
}
else {
    Add-Check "plugin: $($skillPlugin.identity)" 'recommended' $true `
        $skillPlugin.plugin_root `
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
        'Refresh the installed copy in place: python scripts\sync_installed_skills.py. If the bundle is new, install/update the plugin BETWEEN sessions.'

    Add-Check 'skill bundles match published plugin' 'critical' ($drift.Count -eq 0) `
        $(if ($drift.Count) { "STALE in plugin: $($drift -join ', ')" } else { 'in sync' }) `
        'The plugin copy SHADOWS .github/skills, so agents run the OLDER code, not what this repo shows. FIX IT NOW, mid-session: python scripts/sync_installed_skills.py (the lock behind "os error 5" only blocks renaming the plugin dir - files inside stay writable). Then publish so other machines get it: python scripts/build_plugin.py --out <clone of the marketplace repo>, commit+push. Do not trust a measurement taken against a stale bundle.'
}

# Recommended means "warn, do not halt." A check is critical if any persona's Definition of Done
# depends on it, even when the dependency only fails later at handoff/validation time. Audited
# 2026-08-10 under that exit semantics:
#   * powerbi-migration-skills plugin: repo-local skills still load in this repo; the critical bundle
#     checks above enforce correctness when the installed plugin is present and shadowing the repo.
#   * powerbi-modeling-mcp: useful authoring accelerator; local PBIP/TMDL edits can still proceed.
#   * Power BI Desktop version drift: advisory re-verification trigger only; the exact bridge target
#     is pinned by the recommended PBI_DESKTOP_PATH check below.
# --- MCP servers ---
$mcp = Read-CopilotJson 'mcp-config.json'
foreach ($srv in @(@('powerbi-modeling-mcp', 'recommended'), @('powerbi-remote', 'optional'))) {
    $has = $mcp -and $mcp.mcpServers.($srv[0])
    Add-Check "mcp: $($srv[0])" $srv[1] ([bool]$has) `
        $(if ($has) { 'configured' } else { 'not in ~/.copilot/mcp-config.json' }) `
        'Add via /mcp, or copy from .vscode/mcp.json into ~/.copilot/mcp-config.json (mcpServers).'
}

Add-Cli 'npx' 'critical' 'Install Node.js; npx runs the powerbi-modeling MCP and the Desktop Bridge CLI.'
Add-Cli 'powerbi-desktop' 'critical' 'npm install -g @microsoft/powerbi-desktop-bridge-cli - Desktop Bridge for open/reload/screenshot verification.'

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
if ($CheckUpstream) {
    foreach ($cliName in $cliFloor.Keys) {
        $installed = Get-CliVersion $cliName
        if (-not $installed) { continue }
        $latest = (& npm view $cliPackage[$cliName] version 2>$null | Select-Object -First 1)
        if ($latest -match '^\d+(\.\d+)+$') {
            $behind = [version]$installed -lt [version]$latest
            Add-Check "upstream: $cliName" 'optional' (-not $behind) `
                $(if ($behind) { "$installed installed, $latest available" } else { "$installed (latest)" }) `
                "Between migrations only: npm install -g $($cliPackage[$cliName])@latest, then re-verify the version-specific Gotchas in .github/agents/."
        }
    }

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


#
# TWO things are checked here, and the second one is the one that bites.
#
# 1. Is Desktop installed? (below)
# 2. Can the BRIDGE find it? -- a different question with a different answer. Measured 2026-08-06:
#    Desktop auto-updated 2.157.480.0 -> 2.157.627.0 mid-session. `Get-AppxPackage` (what THIS script
#    uses) followed the move and reported [OK] with the new path, so preflight printed
#    "Ready to migrate" -- and the very next `powerbi-desktop open` died with DESKTOP_EXE_NOT_FOUND,
#    because the bridge resolves the exe from its OWN hard-coded list which still named ...480.0.
#    A green preflight followed immediately by a broken Desktop call is the exact false-green class
#    this script exists to prevent, so it must not happen again.
#
#    The bridge cannot be asked cheaply (only `open` resolves the exe; `status` returns
#    not_connected without touching it), so instead of detecting the mismatch we REMOVE it:
#    PBI_DESKTOP_PATH is honoured by the bridge and wins over its built-in discovery. Setting it
#    makes Desktop auto-updates a non-event for every downstream call.
$desktop = $null
$desktopVia = ''
if ($env:PBI_DESKTOP_PATH -and (Test-Path $env:PBI_DESKTOP_PATH)) { $desktop = $env:PBI_DESKTOP_PATH; $desktopVia = 'PBI_DESKTOP_PATH' }
$appx = Get-AppxPackage Microsoft.MicrosoftPowerBIDesktop -ErrorAction SilentlyContinue
if (-not $desktop) {
    $loc = $appx.InstallLocation
    if ($loc -and (Test-Path (Join-Path $loc 'bin\PBIDesktop.exe'))) { $desktop = (Join-Path $loc 'bin\PBIDesktop.exe'); $desktopVia = 'MSIX discovery' }
}
if (-not $desktop) {
    $classic = 'C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe'
    if (Test-Path $classic) { $desktop = $classic; $desktopVia = 'classic install' }
}
Add-Check 'Power BI Desktop' 'critical' ([bool]$desktop) `
    $(if ($desktop) { "$desktop (via $desktopVia)" } else { 'not found' }) `
    'Install Power BI Desktop (Store/MSIX preferred) - needed for the refresh + screenshot verification loop.'

# The Desktop APP version is tracked like the two npm CLIs are, because it moves on its own schedule
# and takes the bridge's exe discovery with it.
if ($appx) {
    $desktopKnownGood = '2.157.627.0'
    Add-Check 'version: Power BI Desktop' 'recommended' ($appx.Version -eq $desktopKnownGood) `
        "$($appx.Version)$(if ($appx.Version -eq $desktopKnownGood) { ' (known-good)' } else { " (known-good $desktopKnownGood)" })" `
        "Desktop moved to $($appx.Version). Not an error - but re-set PBI_DESKTOP_PATH (below) and re-verify any version-specific Desktop behaviour."
}

# The mismatch-remover. A set PBI_DESKTOP_PATH means the bridge and this script resolve the SAME exe;
# unset means the bridge is guessing from a version-pinned list and may already be wrong.
#
# RECOMMENDED, not critical (#124). It was critical, and that made a machine with the engine, both
# plugins, both CLIs at known-good versions, Desktop installed, az, uv and ODBC 18 report NOT READY
# over one unset variable. Two things are wrong with that:
#   * it is a DESKTOP-only pin, and the estate pipeline (steps 1-6) never opens Desktop -
#     `run_estate.py`'s own docstring says "never opens Power BI Desktop". Blocking a run on a
#     dependency that run cannot use is a false blocker, and the operator who proceeded anyway was
#     right - nothing in the estate pipeline needed it;
#   * exit 1 means "resolve before migrating", so spending it on an item the operator runbook never
#     names teaches people to ignore exit 1 - which is the one signal this script has.
# It stays VISIBLE (a WARN, counted in the summary line) because the failure it prevents is real:
# Desktop auto-updates and the bridge then cannot find the exe. The phase that actually needs it -
# report authoring / Desktop verification - is where it must be resolved, and `powerbi-desktop open`
# fails loudly there rather than silently.
$pathPinned = [bool]($env:PBI_DESKTOP_PATH -and (Test-Path $env:PBI_DESKTOP_PATH))
# The hint must work IN THE SHELL THAT READS IT. `setx` writes the user profile and is inherited only
# by processes started LATER - and an agent's tool shells inherit the environment of a parent that is
# already running, so "then reopen the shell" is advice they cannot act on. `$env:` is the fix that
# takes effect immediately; `setx` is offered second, for persistence, correctly labelled.
Add-Check 'PBI_DESKTOP_PATH (bridge exe pin)' 'recommended' $pathPinned `
    $(if ($pathPinned) { $env:PBI_DESKTOP_PATH } else { 'not set - the bridge is using its own version-pinned discovery; needed only for the Desktop refresh/screenshot phase, not for the estate pipeline' }) `
    $(if ($desktop) { "THIS shell (takes effect now): `$env:PBI_DESKTOP_PATH = `"$desktop`"   |   persist for NEW shells only (does NOT affect this one): setx PBI_DESKTOP_PATH `"$desktop`"" } else { 'install Power BI Desktop first' })

# --- Privacy Levels: a MANUAL prerequisite this script cannot verify -------------------------------
# Opening a model that spans more than one data source raises a modal ("Potential security risk: This
# file uses multiple data sources...") BEFORE the model loads. Federated datasources are normal in real
# Tableau workbooks, so most migrations hit it.
#
# It is worse for an agent than for a person: it blocks at LOAD, so it stalls before any refresh call
# and no automation can dismiss it. Measured 2026-08-05, a run sat past 450s on this while
# refresh_pbip_model.py's own 300s ceiling never fired - that ceiling wraps the XMLA refresh, not the
# open. To a supervising agent it looks like a hang with no error.
#
# This is stated rather than checked ON PURPOSE. Desktop ships as an MSIX package and keeps the setting
# in the package's private settings.dat hive, which needs SeRestorePrivilege to load and is locked while
# Desktop runs; there is no supported read path. Asserting a check we cannot actually perform would be
# worse than an honest reminder.
Add-Check 'Privacy Levels (manual)' 'optional' $true `
    'VERIFY BY HAND: Options > Global > Privacy > "Always ignore Privacy Level settings"' `
    'Without it, any MULTI-SOURCE model blocks on a modal at open and an unattended refresh hangs with no error.'

# --- .NET SDK (builds scripts/tmdl_validate for offline TMDL deserialization) ---
# NOTE: this replaced an older check for Microsoft.AnalysisServices.Tabular.dll under
# ~/.copilot/installed-plugins. The powerbi-authoring plugin no longer bundles Tabular Editor, so that
# check could never pass. TOM now comes from the NuGet package Microsoft.AnalysisServices.NetCore.retail.amd64,
# restored by the tmdl_validate project - so the real machine dependency is the .NET SDK.
Add-Cli 'dotnet' 'critical' 'Install the .NET SDK - needed to build/run the offline TMDL structural validator (tmdl_validate).'

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

function Get-DotEnvValue([string]$Key, [string]$Path) {
    # The same minimal KEY=VALUE parse as scripts/tableau_env.py's load_env (trim, skip blank/'#',
    # split on the FIRST '='), plus ONE deliberate divergence: matched surrounding quotes are
    # stripped. That divergence is the fix, not an accident - the two parsers previously agreed by
    # being wrong in the same way, and "identical to Python" was the argument that kept it.
    # `scripts/tableau_env.py:load_env` should get the same treatment; it is owned elsewhere, so it
    # is flagged rather than edited here. Nothing in Python reads FABRIC_TENANT_ID today, so the
    # divergence is inert until that happens.
    # .env is git-ignored, which is where a real customer tenant id belongs: this repository is public.
    if (-not $Path) { $Path = Join-Path $repoRoot '.env' }
    if (-not (Test-Path $Path)) { return $null }
    foreach ($line in (Get-Content $Path)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#') -or ($trimmed -notmatch '=')) { continue }
        $pair = $trimmed -split '=', 2
        if ($pair[0].Trim() -eq $Key) { return (Remove-SurroundingQuotes $pair[1]) }
    }
    return $null
}

function Test-TenantIdShape([string]$Value) {
    # The `tid` claim is ALWAYS a GUID. An intended tenant that is not one cannot be compared
    # against it, and two spellings that are not GUIDs are entirely plausible here:
    #   * `contoso.onmicrosoft.com` - which `az --tenant` and `deploy_estate.py --tenant` both
    #     accept, so an operator has every reason to think it belongs in FABRIC_TENANT_ID;
    #   * a placeholder like `<your-tenant-id>` copied out of a `.env.example`.
    # Comparing either against a GUID answers "wrong tenant" - the same false blocker as the quoting
    # bug, arriving by a different route. Preflight says "cannot compare" instead, and never blocks.
    return [bool]($Value -match '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
}

function Get-TenantVerdict {
    <#
      THE decision. Everything above this gathers evidence; this function alone turns it into
      Ok/Tier/Detail, so the one line that decides the verdict can be EXECUTED by the test harness
      rather than matched as a source string. That matters more than usual here: CI cannot run
      PowerShell, and a mutation test showed both `$tenantOk = $true` (the check can never fire) and
      an inverted comparison (fires on every correct machine) surviving the whole suite while it was
      inline.

      Kind is part of the contract: the caller maps it to a hint, and a Kind with no hint branch is
      a check that renders as silence - the false-green shape this script exists to prevent.
    #>
    param(
        [string]$IntendedTenant,
        [string]$ActualTenant,
        [bool]$IntentIsExplicit,
        [bool]$AzPresent,
        [string]$Scope = ''
    )
    # Normalized HERE, not only at the call site, so the comparator is correct in isolation: it is
    # the unit the tests execute, and a caller that forgets cannot manufacture a false WRONG TENANT.
    # Idempotent, so doing it twice costs nothing.
    $IntendedTenant = Remove-SurroundingQuotes $IntendedTenant
    $ActualTenant = Remove-SurroundingQuotes $ActualTenant

    if (-not $IntendedTenant) {
        return [pscustomobject]@{ Kind = 'no-intent'; Ok = $false; Tier = 'optional'; Detail = 'no intended tenant declared - not checked' }
    }
    if (-not (Test-TenantIdShape $IntendedTenant)) {
        return [pscustomobject]@{ Kind = 'malformed'; Ok = $false; Tier = 'optional'; Detail = "intended '$IntendedTenant' is not a tenant GUID - not compared" }
    }
    if (-not $AzPresent) {
        return [pscustomobject]@{ Kind = 'no-az'; Ok = $false; Tier = 'optional'; Detail = "intended $IntendedTenant - not verified (az not on PATH)" }
    }
    if (-not $ActualTenant) {
        return [pscustomobject]@{ Kind = 'no-token'; Ok = $false; Tier = 'optional'; Detail = "intended $IntendedTenant - no Fabric token could be minted or decoded$Scope" }
    }

    # The verdict. `-eq` on strings is case-insensitive in PowerShell, which is exactly right for a
    # GUID: the portal shows one casing, the `tid` claim another, and neither is a wrong tenant.
    # Do NOT "tighten" this to -ceq.
    $tenantOk = ($ActualTenant -eq $IntendedTenant)

    if ($tenantOk) {
        return [pscustomobject]@{ Kind = 'match'; Ok = $true; Tier = 'optional'; Detail = "tid matches intended $IntendedTenant$Scope" }
    }
    # A mismatch blocks only when THIS run declared the tenant on the command line. The check is
    # opt-in either way, but the two opt-ins are not the same statement: `-Tenant <id>` is "I am
    # pointing at that tenant right now", while FABRIC_TENANT_ID in `.env` is a persisted preference
    # that survives every later run - including a parse-only estate sweep whose steps 1-6 never call
    # Fabric. Blocking those would re-create, from the other side, the false blocker this change set
    # removed (a Desktop-only pin failing a run that never opens Desktop). A WARN still names the
    # problem loudly, and the deploy path - which does pass --tenant - is where exit 1 belongs.
    $tier = if ($IntentIsExplicit) { 'critical' } else { 'recommended' }
    $detail = "WRONG TENANT: token is for $ActualTenant, intended $IntendedTenant$Scope"
    if (-not $IntentIsExplicit) { $detail += ' [warning only: declared by configuration, not by -Tenant on this run]' }
    return [pscustomobject]@{ Kind = 'mismatch'; Ok = $false; Tier = $tier; Detail = $detail }
}

# Declaration of intent, in the order a caller expects to win: flag > exported env > git-ignored .env.
# Every channel is normalized, because none of them is a place an operator expects to be punished for
# quoting or for a stray trailing space.
$tenantIntentIsExplicit = [bool](Remove-SurroundingQuotes $Tenant)
$intendedTenant = @($Tenant, $env:FABRIC_TENANT_ID, (Get-DotEnvValue 'FABRIC_TENANT_ID')) |
    ForEach-Object { Remove-SurroundingQuotes $_ } | Where-Object { $_ } | Select-Object -First 1

# Mirrors deploy_estate.py's FABRIC_RESOURCE. Duplicating a well-known constant is a smaller risk
# than making this PowerShell bootstrap depend on importing Python (see the engine block above for
# the case where re-deriving a LIST would have been the real defect); if it ever changed, the deploy
# would fail loudly on its own.
$fabricResource = 'https://api.fabric.microsoft.com'

$azPresent = [bool](Get-Command az -ErrorAction SilentlyContinue)
$scoped = if ($Subscription) { " (scoped: --subscription $Subscription)" } else { ' (default az context)' }
$actualTenant = ''

# The mint sits behind the declaration guard: no declared tenant - or one that cannot be compared -
# means no `az` call at all, so an operator who only parses workbooks pays nothing. Preflight runs
# before EVERY migration, so a mandatory token mint here would tax every run for a check that has
# nothing to compare against.
if ($intendedTenant -and (Test-TenantIdShape $intendedTenant) -and $azPresent) {
    $azArgs = @('account', 'get-access-token', '--resource', $fabricResource, '-o', 'json')
    # --subscription scopes ONE call. It is offered here so the fix can be verified with the same
    # command shape that applies it, rather than by mutating the CLI profile and hoping.
    if ($Subscription) { $azArgs += @('--subscription', $Subscription) }
    # $tokenJson holds a BEARER TOKEN. It is never echoed, never added to a Detail, and az's own
    # stderr is discarded rather than surfaced, so no code path can leak it.
    $tokenJson = & az @azArgs 2>$null
    if ($tokenJson) {
        try { $actualTenant = Get-JwtTenantId ((($tokenJson | Out-String) | ConvertFrom-Json).accessToken) } catch { $actualTenant = '' }
    }
}

$verdict = Get-TenantVerdict -IntendedTenant $intendedTenant -ActualTenant $actualTenant `
    -IntentIsExplicit $tenantIntentIsExplicit -AzPresent $azPresent -Scope $scoped

# One hint per Kind. Anything that needs an extra `az` call lives in its own branch, so the failure
# path is the only one that pays for it.
$tenantHint = switch ($verdict.Kind) {
    'no-intent' {
        'Declare the tenant you deploy into and this becomes automatic: -Tenant <id> (blocking, for a run that is about to deploy), $env:FABRIC_TENANT_ID, or FABRIC_TENANT_ID in the git-ignored .env (both warn-only). Same id you pass to deploy_estate.py --tenant. Until then, a WorkspaceNotFound on a workspace you know exists may simply be a token for another tenant.'
    }
    'malformed' {
        'A token''s `tid` claim is always a GUID, so preflight has nothing to compare a domain name or a placeholder against. Use the GUID form: az account show --query tenantId -o tsv (or Entra > Overview). Note `az --tenant` and deploy_estate.py DO accept contoso.onmicrosoft.com, so this is a preflight-only requirement - and it never blocks.'
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

Add-Check 'Fabric token tenant' $verdict.Tier $verdict.Ok $verdict.Detail $tenantHint

$odbc = (Get-OdbcDriver -ErrorAction SilentlyContinue | Where-Object Name -like '*SQL Server*').Name
Add-Check 'ODBC Driver 18 (SQL)' 'optional' ($odbc -contains 'ODBC Driver 18 for SQL Server') `
    $(if ($odbc) { ($odbc | Select-Object -Unique) -join '; ' } else { 'none' }) `
    'Only for direct SQL Analytics Endpoint / Warehouse queries.'

# --- Render ---
$criticalMissing = 0
$recommendedWarnings = 0
foreach ($tier in @('critical', 'recommended', 'optional')) {
    Write-Host ''
    Write-Host "== $($tier.ToUpper()) =="
    foreach ($r in ($results | Where-Object { $_.Tier -eq $tier })) {
        $mark = if ($r.Ok) { 'OK  ' } elseif ($tier -eq 'critical') { 'MISS' } else { 'WARN' }
        Write-Host ("  [{0}] {1,-44} {2}" -f $mark, $r.Name, $r.Detail)
        if (-not $r.Ok -and $tier -eq 'critical') { Write-Host "         -> $($r.Hint)"; $criticalMissing++ }
        elseif (-not $r.Ok -and $tier -eq 'recommended') { Write-Host "         (recommended) $($r.Hint)"; $recommendedWarnings++ }
        elseif (-not $r.Ok -and $r.Hint) { Write-Host "         (optional) $($r.Hint)" }
    }
}
Write-Host ''
if ($criticalMissing -gt 0) {
    $suffix = if ($recommendedWarnings -gt 0) { " ($recommendedWarnings recommended warning(s) also present)." } else { '' }
    Write-Host "PREFLIGHT: $criticalMissing critical item(s) missing - resolve before migrating.$suffix"
    exit 1
}
$suffix = if ($recommendedWarnings -gt 0) { " $recommendedWarnings recommended warning(s) present; review before relying on affected capabilities." } else { '' }
Write-Host "PREFLIGHT: all critical dependencies present. Ready to migrate.$suffix"
exit 0
