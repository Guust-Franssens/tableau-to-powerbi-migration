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

.PARAMETER CheckUpstream
  Opt-in (~3s of network) and ADVISORY. Every other check compares an installed version against a
  hard-coded number, which answers "is what I have good enough" but never "has the world moved".
  This asks npm for the latest bridge CLIs and GitHub for the conversion engine's upstream VERSION.
  It never upgrades and never fails the run.

.NOTES
  Run:  powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1 [-Update]
  Exit: 0 if every CRITICAL item is present; 1 if any CRITICAL item is missing.
        RECOMMENDED and OPTIONAL items are surfaced as warnings but do not stop a migration.
#>
#Requires -Version 5.1
param([switch]$Update, [switch]$CheckUpstream)

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
# Both are REQUIRED for the agents to work as written: `powerbi-authoring` supplies the planning/
# design/authoring/semantic-model skills the builder personas chain, and `powerbi-migration-skills`
# republishes this repo's own two bundles so a subagent can invoke them BY NAME. Measured 2026-07-31:
# a custom subagent does get a `skill` tool and CAN invoke both plugin and repo-local skills - so a
# missing plugin is a real capability loss, not a cosmetic one.
$cfg = Read-CopilotJson 'config.json'
$migrationPlugin = $null
foreach ($p in @(
        @{ name = 'powerbi-authoring'; market = 'fabric-collection'; tier = 'critical'
            hint = 'In Copilot: /plugin -> add marketplace microsoft/skills-for-fabric -> enable powerbi-authoring. See AGENTS.md.' },
        @{ name = 'powerbi-migration-skills'; market = 'powerbi-migration-collection'; tier = 'recommended'
            hint = 'In Copilot: /plugin -> add marketplace Guust-Franssens/powerbi-migration-skills -> enable powerbi-migration-skills. See AGENTS.md.' }
    )) {
    $plugin = $null
    if ($cfg -and $cfg.installedPlugins) {
        $plugin = $cfg.installedPlugins | Where-Object { $_.name -eq $p.name -and $_.marketplace -eq $p.market } | Select-Object -First 1
    }
    $pluginOk = $plugin -and (Test-Path $plugin.cache_path)
    Add-Check "plugin: $($p.name)@$($p.market)" $p.tier ([bool]$pluginOk) `
        $(if ($pluginOk) { "v$($plugin.version)" } else { 'not installed/enabled' }) $p.hint
    if ($pluginOk -and $p.name -eq 'powerbi-migration-skills') { $migrationPlugin = $plugin }
}

# --- Skill-bundle drift (the plugin copy SHADOWS .github/skills/ for a subagent) ---
# Measured 2026-07-31: a subagent invoking `powerbi-ai-readiness` by name loaded the PLUGIN copy under
# ~/.copilot/installed-plugins, NOT the repo copy - even with the repo copy present and the cwd inside
# this repo. So editing .github/skills/ without re-publishing serves subagents stale guidance, and
# nothing in the skill registry or the tool output flags the divergence. This check is the only thing
# that would catch it.
#
# It checks BOTH failure shapes, because they are different mistakes:
#   MISSING - a bundle listed in build_plugin.py's SHIPPED_SKILLS that is not in the installed plugin
#             (you added or published a bundle but never re-installed; a pairwise hash check alone is
#             blind to this, since there is nothing to pair with).
#   STALE   - a bundle present in both whose bytes differ (you edited the repo copy without publishing).
# Bundles deliberately NOT shipped (e.g. sentinel-probe) are absent from SHIPPED_SKILLS and so ignored.
if ($migrationPlugin) {
    $buildScript = Join-Path $repoRoot 'scripts\build_plugin.py'
    $shipped = @()
    if (Test-Path $buildScript) {
        $src = Get-Content $buildScript -Raw
        if ($src -match '(?s)SHIPPED_SKILLS\s*=\s*\((.*?)\)') {
            $shipped = [regex]::Matches($Matches[1], '"([^"]+)"') | ForEach-Object { $_.Groups[1].Value }
        }
    }
    $missing = @()
    $drift = @()
    foreach ($name in $shipped) {
        $mine = Join-Path $repoRoot ".github\skills\$name\SKILL.md"
        $theirs = Join-Path $migrationPlugin.cache_path "skills\$name\SKILL.md"
        if (-not (Test-Path $theirs)) { $missing += $name; continue }
        if ((Test-Path $mine) -and ((Get-FileHash $mine).Hash -ne (Get-FileHash $theirs).Hash)) { $drift += $name }
    }
    # TIERING MATTERS. A 'recommended' check must not produce the same exit as a missing 'critical'
    # dependency: preflight is step 0 for every migration, and agents stop on a non-zero exit. These
    # bundle checks are not advisory, though: AGENTS.md documents both NOT INSTALLED and STALE as
    # blockers, because a stale installed bundle means a subagent executes different code than the
    # repo shows, which already invalidated one measurement in this repo.
    #
    # The two shapes are still split because they mean different repairs:
    #   NOT INSTALLED -> a shipped bundle absent locally is a real capability
    #                    loss: the agent cannot invoke that skill by name at all.
    #   STALE         -> this was 'optional' until 2026-08-01, on the theory
    #                    that an older revision is "a degradation, not a correctness break". That is
    #                    FALSE, and it cost a whole experiment. The plugin copy SHADOWS .github/skills,
    #                    so a stale bundle means the agent silently executes DIFFERENT CODE than the
    #                    repo shows - with no diff to notice. Measured: refresh_pbip_model.py had just
    #                    gained an XMLA CommandTimeout in the repo; the plugin copy had none, the agent
    #                    ran the copy without it, and the resulting timing was used to "prove" a
    #                    conclusion about behaviour the executed code did not even contain. Keep this
    #                    warning visible and actionable, but do not encode a warning tier as exit 1.
    $detail = if ($missing.Count) { "NOT INSTALLED: $($missing -join ', ')" } else { "$($shipped.Count) bundle(s) present" }
    Add-Check 'skill bundles installed' 'critical' ($missing.Count -eq 0) $detail `
        'copilot plugin install powerbi-migration-skills@powerbi-migration-collection (BETWEEN sessions - a running Copilot session file-locks the plugin dir).'

    Add-Check 'skill bundles match published plugin' 'critical' ($drift.Count -eq 0) `
        $(if ($drift.Count) { "STALE in plugin: $($drift -join ', ')" } else { 'in sync' }) `
        'The plugin copy SHADOWS .github/skills, so agents run the OLDER code, not what this repo shows. FIX IT NOW, mid-session: python scripts/sync_installed_skills.py (the lock behind "os error 5" only blocks renaming the plugin dir - files inside stay writable). Then publish so other machines get it: python scripts/build_plugin.py --out <clone of powerbi-migration-skills>, commit+push. Do not trust a measurement taken against a stale bundle.'
}

# Recommended means "warn, do not halt." A check is critical if any persona's Definition of Done
# depends on it, even when the dependency only fails later at handoff/validation time. Audited
# 2026-08-10 under that exit semantics:
#   * powerbi-migration-skills plugin: repo-local skills still load in this repo; the critical bundle
#     checks above enforce correctness when the installed plugin is present and shadowing the repo.
#   * powerbi-modeling-mcp: useful authoring accelerator; local PBIP/TMDL edits can still proceed.
#   * Power BI Desktop version drift: advisory re-verification trigger only; the exact bridge target
#     is enforced by the critical PBI_DESKTOP_PATH pin below.
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
$pathPinned = [bool]($env:PBI_DESKTOP_PATH -and (Test-Path $env:PBI_DESKTOP_PATH))
Add-Check 'PBI_DESKTOP_PATH (bridge exe pin)' 'critical' $pathPinned `
    $(if ($pathPinned) { $env:PBI_DESKTOP_PATH } else { 'not set - the bridge is using its own version-pinned discovery' }) `
    $(if ($desktop) { "setx PBI_DESKTOP_PATH `"$desktop`"   (then reopen the shell)" } else { 'install Power BI Desktop first' })

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
