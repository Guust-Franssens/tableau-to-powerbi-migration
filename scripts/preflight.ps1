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

$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    $nodeRaw = (& node --version 2>&1 | Select-Object -First 1) -replace '^v', ''
    $nodeOk = $false
    try { $nodeOk = [version]$nodeRaw -ge [version]'20.0.0' } catch { $nodeOk = $false }
    Add-Check 'Node.js >= 20' 'critical' $nodeOk `
        $(if ($nodeRaw) { $nodeRaw } else { 'no version reported' }) `
        'Install Node.js 20+; npx and the Power BI bridge CLIs depend on it.'
}
else {
    Add-Check 'Node.js >= 20' 'critical' $false 'node not on PATH' 'Install Node.js 20+; npx and the Power BI bridge CLIs depend on it.'
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

# Get-SkillBundleVerdict turns ONE `sync_installed_skills.py --check --json` verdict into this
# script's rows. Extracted to a function (not inlined) so tests/test_sync_installed_skills.py can
# EXECUTE the decision with a controlled plugin, mirroring Get-DesktopPinVerdict: review measured
# that mutating the inline comparison to its inverse survived the entire new test file, because
# source-string assertions cannot see which way a comparison points.
#
# It is driven ENTIRELY by that one verdict, and no longer by skill_plugin_source.py, because
# discovery itself used to import the CURRENT WORKTREE's SHIPPED_SKILLS - so preflight held a second,
# branch-dependent notion of which bundles exist and where they live (issue #410 review finding 2).
# The sync verdict derives that inventory from the merged commit before it discovers anything.
function Get-SkillBundleVerdict($Sync) {
    if (-not $Sync) {
        return @{ Mode = 'unreported'
                  Merged = @{ Ok = $false; Detail = 'not verified (sync_installed_skills.py --check --json did not report)'
                              Hint = 'Run python scripts\sync_installed_skills.py --check --json and read the error. Until it reports, the installed bundles that SHADOW .github/skills are unverified.' } }
    }
    if ($Sync.status -eq 'multiple_plugins') {
        return @{ Mode = 'multiple'
                  Plugin = @{ Ok = $false; Detail = "MULTIPLE installs carry these bundles: $(@($Sync.candidates) -join '; ')"
                              Hint = 'Two installed copies of the same skill create an ambiguous shadowing order. Remove the duplicate before trusting skill output.' } }
    }
    if ($Sync.status -eq 'no_plugin') {
        # Deliberately recommended, not critical: with NO installed copy, this repo's local
        # .github/skills bundles remain available in this checkout. That is a capability/distribution
        # gap for other repos, not the measured false-green bug.
        return @{ Mode = 'missing'; Identity = 'reusable Power BI skill bundles'
                  Plugin = @{ Ok = $false; Detail = 'not installed/enabled'; Hint = $Sync.install_hint } }
    }
    if ($Sync.status -eq 'no_ref') {
        # Distinct from drift on purpose: with no resolvable merged ref there is no authority to
        # compare against, so the installed copy is UNVERIFIED, not stale. Reporting that as
        # "in sync with <blank>" while failing the check is the confusing shape this branch removes.
        return @{ Mode = 'noref'
                  Merged = @{ Ok = $false; Detail = "cannot resolve the merged ref: $($Sync.detail)"
                              Hint = 'The installed bundles SHADOW .github/skills and could not be checked against what is merged. Add the origin remote and fetch, or pass --ref <ref> to python scripts\sync_installed_skills.py --check.' } }
    }
    if ($Sync.status -eq 'unverified_default') {
        # Review finding 2: the remote renamed its default branch, `git fetch` left the local
        # `origin/HEAD` marker pointing at the old one, and the sync published the OLD branch while
        # reporting in_sync. The sync now refuses instead of guessing - and preflight must carry that
        # through as UNVERIFIED, because a green row here is exactly the false green being removed.
        return @{ Mode = 'unverified'
                  Merged = @{ Ok = $false; Detail = "merged default branch UNVERIFIED: $($Sync.detail)"
                              Hint = 'Which branch is authoritative could not be established, so the installed bundles that SHADOW .github/skills are unverified. Reconnect and re-run python scripts\sync_installed_skills.py --check, or pin it with --ref refs/remotes/origin/<branch>.' } }
    }
    if ($Sync.status -eq 'unproven_plugin') {
        # Review finding 1: destination ownership used to be inferred from CONTENT, so a foreign
        # plugin that merely carried one bundle name was selected, overwritten, and had a file inside
        # it deleted. The sync now writes nothing it cannot prove it owns; naming it once is a
        # deliberate operator action, so this blocks rather than silently skipping the check.
        return @{ Mode = 'unproven'
                  Plugin = @{ Ok = $false; Detail = "ownership UNPROVEN, nothing was written: $($Sync.detail)"
                              Hint = $Sync.install_hint } }
    }

    if ($Sync.status -eq 'unsafe_marker') {
        # Round-3 finding 2: the ownership marker's recorded inventory is fed to shutil.rmtree, and
        # an entry that is not a single filename component escapes the plugin entirely. The sync
        # refuses (exit 8) having written nothing. This needs its own branch, not the fall-through:
        # that payload carries no `missing`/`changed`/`bundles`, so the generic rows below would
        # read GREEN off absent evidence - the "unassessable input lands in the clean bucket" shape.
        return @{ Mode = 'unsafe_marker'
                  Plugin = @{ Ok = $false; Detail = "ownership marker REFUSED, nothing was written: $($Sync.detail)"
                              Hint = 'Every bundle the marker records must be a single directory name inside the plugin''s skills/ folder, because `installed / <name>` is passed to shutil.rmtree. Delete or repair .skill-sync-owner.json in the plugin root, then re-run python scripts\sync_installed_skills.py --check.' } }
    }

    if ($Sync.status -eq 'unsafe_install') {
        # Round-7 finding 1: a reparse point inside an owned bundle redirects `copy2`/`unlink` out
        # of the plugin. Measured on Python 3.13.2, an external file was DELETED and another
        # OVERWRITTEN while the run reported `updated` and exited 0. The sync now refuses (exit 9)
        # having written nothing, and this needs its own branch for the same reason `unsafe_marker`
        # does: the payload carries no inventory, so the generic rows below would read GREEN off
        # evidence that is simply absent.
        return @{ Mode = 'unsafe_install'
                  Plugin = @{ Ok = $false; Detail = "install tree REFUSED, nothing was written: $($Sync.detail)"
                              Hint = 'A junction or symlink inside a bundle this tool owns makes every write and delete under it land somewhere else. Remove the link (rmdir <path> for a junction) and re-run python scripts\sync_installed_skills.py --check.' } }
    }

    $missing = @($Sync.missing) | Where-Object { $_ }
    $stale = @($Sync.changed) + @($Sync.extra) | Where-Object { $_ }
    $localEdits = @($Sync.local_unmerged) | Where-Object { $_ }
    # Round-7 finding 2. `recorded` means origin did not answer on THIS run and the tool reused the
    # branch an earlier run confirmed. Measured: an online run recorded `master`, the remote's HEAD
    # then moved to `main` with different content, and offline this row reported merged_ok about
    # bytes from the abandoned branch - a false CERTIFICATION, not merely a stale comparison.
    #
    # Severity is a judgement and both directions fail. Green certifies old content as merged.
    # Critical fails EVERY offline session start - and preflight is documented as not requiring the
    # network by default - so the row would fire routinely for a benign reason, get bypassed, and
    # stop being read at all: the same false green by a slower route. So: never green, never
    # silent, WARN rather than halt, and worded CANNOT ESTABLISH so it can never be mistaken for
    # STALE, which is a different state with a different fix.
    #
    # The downgrade is deliberately narrow: it applies only when the recorded proof is the SOLE
    # reason the row is not green. Anything genuinely stale or unreconciled stays critical.
    $recordedOnly = ($Sync.default_verified -ne $true) -and ($Sync.default_proof -eq 'recorded') `
                    -and ($Sync.status -eq 'in_sync') -and ($stale.Count -eq 0)
    return @{
        Mode = 'compared'
        Identity = $Sync.identity
        Plugin = @{ Ok = $true; Detail = $Sync.plugin_root
                    Hint = "Ownership was PROVED ($($Sync.proof)), not inferred from the bundles a directory happens to carry." }
        Installed = @{
            Ok = ($missing.Count -eq 0)
            Detail = if ($missing.Count) { "NOT INSTALLED in discovered plugin: $($missing -join ', ')" } else { "$(@($Sync.bundles).Count) bundle(s) present" }
            Hint = 'Refresh the installed copy in place: python scripts\sync_installed_skills.py. If the bundle is new, install/update the plugin BETWEEN sessions.'
        }
        # `default_verified` is part of the DECISION, not decoration. Review measured a run that was
        # genuinely byte-identical to `origin/master` while the remote's default branch had been
        # renamed to `main` with different content: status `in_sync`, and the mismatch reported only
        # as an "alternative" nobody read. In sync with the WRONG branch is not in sync.
        Merged = @{
            Ok = (($Sync.status -eq 'in_sync') -and ($Sync.default_verified -eq $true))
            Tier = if ($recordedOnly) { 'recommended' } else { 'critical' }
            Detail = if ($stale.Count) { "STALE in plugin vs $($Sync.described): $($stale -join ', ')" } elseif ($Sync.status -eq 'ownership_drift') { "ownership RECORD does not match $($Sync.described), though no file differs" } elseif ($recordedOnly) { "CANNOT ESTABLISH the merged default branch: content matches `"$($Sync.described)`", but origin did not answer on this run - that branch is what a PREVIOUS run verified at $($Sync.default_verified_at), not one confirmed now" } elseif ($Sync.default_verified -ne $true) { "UNVERIFIED default branch, so `"$($Sync.described)`" may not be the merged one" } else { "in sync with $($Sync.described)" }
            Hint = 'The plugin copy SHADOWS .github/skills, so agents run bytes that differ from the MERGED repo. FIX IT NOW, mid-session: python scripts/sync_installed_skills.py (the lock behind "os error 5" only blocks renaming the plugin dir - files inside stay writable). Then publish so other machines get it: python scripts/build_plugin.py --out <clone of the marketplace repo>, commit+push. Do not trust a measurement taken against a stale bundle.'
        }
        # Informational, deliberately NOT a failure. Your unmerged skill edits are not what a subagent
        # reads - that is the design, not a fault - but you still need to know, because a measurement
        # taken while assuming otherwise is wrong in the same way a stale bundle makes one wrong.
        LocalEdits = @{
            Ok = ($localEdits.Count -eq 0)
            Detail = if ($localEdits.Count) { "$($localEdits.Count) unmerged local change(s): $($localEdits -join ', ')" } elseif ($Sync.local_unmerged_error) { $Sync.local_unmerged_error } else { 'none' }
            Hint = 'Subagents read the MERGED copy, so these edits are NOT live. To test them deliberately: python scripts/sync_installed_skills.py --from-worktree --plugin-root <path> (it serves unreviewed guidance, and says so). Restore with a plain sync.'
        }
    }
}

$sync = $null
if ($py) {
    $syncRaw = & python (Join-Path $repoRoot 'scripts\sync_installed_skills.py') --check --json 2>$null
    try { $sync = (($syncRaw | Out-String) | ConvertFrom-Json) } catch { $sync = $null }
}
$skills = Get-SkillBundleVerdict $sync

if ($skills.Mode -eq 'unreported' -or $skills.Mode -eq 'noref' -or $skills.Mode -eq 'unverified') {
    Add-Check 'skill bundles match published plugin' 'critical' $skills.Merged.Ok $skills.Merged.Detail $skills.Merged.Hint
}
elseif ($skills.Mode -eq 'multiple' -or $skills.Mode -eq 'unproven') {
    Add-Check 'plugin: reusable Power BI skill bundles' 'critical' $skills.Plugin.Ok $skills.Plugin.Detail $skills.Plugin.Hint
}
elseif ($skills.Mode -eq 'missing') {
    Add-Check "plugin: $($skills.Identity)" 'recommended' $skills.Plugin.Ok $skills.Plugin.Detail $skills.Plugin.Hint
}
else {
    Add-Check "plugin: $($skills.Identity)" 'recommended' $skills.Plugin.Ok $skills.Plugin.Detail $skills.Plugin.Hint

    # Severity decision (2026-08-13): a completely missing plugin is warning-only in this repo because
    # there is no installed copy shadowing local .github/skills. But once an installed plugin exists,
    # partial or stale content is critical: subagents resolve the plugin copy first, so they silently run
    # bytes that differ from the repo and can invalidate measurements.
    Add-Check 'skill bundles installed' 'critical' $skills.Installed.Ok $skills.Installed.Detail $skills.Installed.Hint
    Add-Check 'skill bundles match published plugin' 'critical' $skills.Merged.Ok $skills.Merged.Detail $skills.Merged.Hint
    Add-Check 'skill bundles: local edits vs merged' 'optional' $skills.LocalEdits.Ok $skills.LocalEdits.Detail $skills.LocalEdits.Hint
}

# Recommended means "warn, do not halt." A check is critical if any persona's Definition of Done
# depends on it, even when the dependency only fails later at handoff/validation time. Audited
# 2026-08-10 under that exit semantics:
#   * powerbi-playbook plugin: repo-local skills still load in this repo; the critical bundle
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


# Get-DesktopPinVerdict decides PBI_DESKTOP_PATH's severity from operator BELIEF, not from whether
# Desktop was eventually found via some OTHER path. Extracted to a function (not inlined) so the
# three states (#86: unset / valid / dead) can be executed directly by
# tests/test_preflight_contract.py without a real Desktop install, mirroring Get-TenantVerdict below.
#
# UNSET tells the truth: nothing is pinned, MSIX discovery is exactly what the operator gets and
# expects (#124) - RECOMMENDED, a visible WARN, never a blocker.
# VALID means the bridge and this script resolve the SAME exe - OK.
# DEAD is a different belief, not a stricter version of unset: the operator set PBI_DESKTOP_PATH
# deliberately, so they believe a specific build is selected and handled - and it silently is not.
# The MSIX fallback papers over that belief with a DIFFERENT exe than the one named, which is
# precisely the false-green class this whole script exists to prevent (#86) - CRITICAL, exit 1.
function Get-DesktopPinVerdict([bool]$Configured, [bool]$Valid, [string]$ConfiguredPath, [string]$InstalledDesktop, [string]$FallbackExe) {
    $dead = $Configured -and -not $Valid
    $tier = if ($dead) { 'critical' } else { 'recommended' }
    $detail = if ($Valid) {
        $ConfiguredPath
    } elseif ($dead) {
        "PBI_DESKTOP_PATH points at `"$ConfiguredPath`" which is not on disk; installed Desktop: $InstalledDesktop. The bridge honours this variable and will fail to launch until it is re-pinned."
    } else {
        'not set - the bridge is using its own version-pinned discovery; needed only for the Desktop refresh/screenshot phase, not for the estate pipeline'
    }
    # The hint must work IN THE SHELL THAT READS IT. `setx` writes the user profile and is inherited
    # only by processes started LATER - and an agent's tool shells inherit the environment of a
    # parent that is already running, so "then reopen the shell" is advice they cannot act on.
    # `$env:` is the fix that takes effect immediately; `setx` is offered second, for persistence,
    # correctly labelled. It fires for BOTH the unset and the dead case (whenever Valid is false) - a
    # critical dead pin needs the same actionable fix as a recommended unset one, just with a harder
    # stop attached to it.
    $hint = if ($FallbackExe) {
        "THIS shell (takes effect now): `$env:PBI_DESKTOP_PATH = `"$FallbackExe`"   |   persist for NEW shells only (does NOT affect this one): setx PBI_DESKTOP_PATH `"$FallbackExe`""
    } else {
        'install Power BI Desktop first'
    }
    [pscustomobject]@{ Tier = $tier; Ok = $Valid; Detail = $detail; Hint = $hint }
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
$desktopPathConfigured = [bool]$env:PBI_DESKTOP_PATH
$desktopPathValid = $desktopPathConfigured -and (Test-Path $env:PBI_DESKTOP_PATH)
$desktopPathDead = $desktopPathConfigured -and -not $desktopPathValid
if ($desktopPathValid) { $desktop = $env:PBI_DESKTOP_PATH; $desktopVia = 'PBI_DESKTOP_PATH' }
$appx = Get-AppxPackage Microsoft.MicrosoftPowerBIDesktop -ErrorAction SilentlyContinue
if (-not $desktop) {
    $loc = $appx.InstallLocation
    if ($loc -and (Test-Path (Join-Path $loc 'bin\PBIDesktop.exe'))) {
        $desktop = (Join-Path $loc 'bin\PBIDesktop.exe')
        $desktopVia = if ($desktopPathDead) { 'MSIX discovery (PBI_DESKTOP_PATH is set but dead)' } else { 'MSIX discovery' }
    }
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
# unset means the bridge is guessing from a version-pinned list and may already be wrong. See
# Get-DesktopPinVerdict above for the tier decision (#124 unset -> recommended; #86 dead -> critical).
$installedDesktop = if ($desktop -and $appx) { "$($appx.Version) at $desktop" } elseif ($desktop) { $desktop } else { 'not found' }
$pinVerdict = Get-DesktopPinVerdict -Configured $desktopPathConfigured -Valid $desktopPathValid `
    -ConfiguredPath $env:PBI_DESKTOP_PATH -InstalledDesktop $installedDesktop -FallbackExe $desktop
Add-Check 'PBI_DESKTOP_PATH (bridge exe pin)' $pinVerdict.Tier $pinVerdict.Ok $pinVerdict.Detail $pinVerdict.Hint

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

# --- .NET SDK (builds tools/tmdl_oracle, the TMDL gate's parser) ---
# NOTE: this replaced an older check for Microsoft.AnalysisServices.Tabular.dll under
# ~/.copilot/installed-plugins. The powerbi-authoring plugin no longer bundles Tabular Editor, so that
# check could never pass. The .NET SDK is still needed to build/run the offline validator, but it does
# NOT prove AMO/TOM or ADOMD assemblies exist in the NuGet cache; those file checks live below.
#
# Since #254 this is load-bearing for a GATE, not only for an optional validator:
# `check_datamodel.py` runs the TMDL oracle (tools/tmdl_oracle) to ask TmdlSerializer itself whether
# each model parses. Without `dotnet` the gate reports UNASSESSABLE (exit 3) instead of a pass.
Add-Cli 'dotnet' 'critical' 'Install the .NET SDK - needed to build/run the TMDL oracle (tools/tmdl_oracle) that check_datamodel.py uses, and the per-example tmdl_validate helpers.'

# --- AMO/TOM client assembly (the pbip-model-refresh skill's progress trace + ImageSave persist) ---
# `dotnet` being on PATH proves only that a restore COULD run. It does not prove the restored package is
# already present on a firewalled machine. Mirror the ADOMD lesson below: console text is not proof;
# presence of the DLL on disk is. Hence a file check, not a restore attempt.
#
# Severity: RECOMMENDED, not critical. AMO/TOM is needed for the Desktop refresh/save phase: without it
# scripted refresh loses row-count progress and non-fatal liveness warnings, and ImageSave falls back
# to the UI save path. The estate pipeline's parse/convert steps never open Desktop, so failing the
# whole run here would recreate #124's false-blocker shape for a Desktop-only dependency. After #283,
# the refresh still keeps its 3600s absolute backstop when AMO trace setup fails; the missing
# capability is scripted observability and AMO ImageSave, not the timeout fix. This warning exists so
# the operator chooses before work starts: restore AMO first, or choose the operator refresh strategy.
$nugetPackagesRoot = if ($env:NUGET_PACKAGES) { $env:NUGET_PACKAGES } else { Join-Path $HOME '.nuget\packages' }
$amoDll = Get-ChildItem -Path (Join-Path $nugetPackagesRoot 'microsoft.analysisservices.netcore.retail.amd64*') `
    -Recurse -Filter 'Microsoft.AnalysisServices.Tabular.dll' -ErrorAction SilentlyContinue | Select-Object -First 1
Add-Check 'AMO/TOM client (Desktop progress/ImageSave)' 'recommended' ([bool]$amoDll) `
    $(if ($amoDll) { $amoDll.FullName } else { 'not in the nuget cache - progress reporting and liveness are unavailable, so scripted refresh runs blind; ImageSave falls back to UI; the 3600s absolute refresh backstop remains active' }) `
    'Before a scripted refresh, restore AMO/TOM into the active NuGet global-packages cache, or prefer the operator refresh strategy and watch Desktop''s own row counter: dotnet new console -o $env:TEMP\amo --framework net8.0; dotnet add $env:TEMP\amo package Microsoft.AnalysisServices.NetCore.retail.amd64 --version 19.84.1  (throwaway project; the restore populates the cache this preflight and refresh_pbip_model.py read).'

# --- ADOMD.NET client assembly (the pbip-model-refresh skill's live-Desktop probe + refresh) --------
# AMO/TOM (Microsoft.AnalysisServices.NetCore.retail.amd64) and ADOMD.NET
# (Microsoft.AnalysisServices.AdomdClient.NetCore.retail.amd64) are SEPARATE nuget packages - a machine
# can have one and still be missing the other. That was the silent field failure this check exists for:
# on a colleague's box the AdomdClient assembly was absent, so probe_desktop_query.py could not run a
# live EVALUATE, and nothing flagged it up front. `dotnet add package` had even printed "Restored ... 0
# Errors" while landing ZERO PackageReference (a net10.0 scratch project silently no-op'd the add) - so
# console text is not proof; presence of the DLL on disk is. Hence a file check, not a restore attempt.
#
# It resolves the EXACT cache precedence the Python entry points use: `$env:NUGET_PACKAGES` when
# non-empty, otherwise `$HOME/.nuget/packages`. That makes preflight predict probe_desktop_query.py's
# ADOMD lookup and refresh_pbip_model.py's AMO/TOM lookup instead of checking a different cache. The
# old Path.home()/os.path.expanduser("~") defaults were equivalent; the fixed gap is that none of the
# three sites honoured dotnet's NUGET_PACKAGES override.
#
# Severity: CRITICAL, by this file's own rule above — "critical if any persona's Definition of Done
# depends on it, even when the dependency only fails later at handoff/validation time". It does, and
# unconditionally: `pbi-semantic-builder`'s DoD step 8 is a HANDOFF GATE that runs
# refresh_pbip_model.py (which imports `_load_adomd`) to refresh and SAVE `.pbi/cache.abf`, and
# `pbi-report-builder`'s step 1 refuses to open Desktop without that file. So every migration that
# ends in a report needs ADOMD — this is not the live-source-only scope it first appears to be.
# Measured 2026-08-17: with only this lookup suppressed on an otherwise healthy machine, preflight
# printed "Ready to migrate" and exited 0 while the probe exited 2 — reproducing exactly the silent
# false-green #199 was filed to remove. Do NOT downgrade this to recommended without also removing
# the refresh/save gate from those two personas.
$nugetPackagesRoot = if ($env:NUGET_PACKAGES) { $env:NUGET_PACKAGES } else { Join-Path $HOME '.nuget\packages' }
$adomdDll = Get-ChildItem -Path (Join-Path $nugetPackagesRoot 'microsoft.analysisservices.adomdclient.netcore*') `
    -Recurse -Filter 'Microsoft.AnalysisServices.AdomdClient.dll' -ErrorAction SilentlyContinue | Select-Object -First 1
Add-Check 'ADOMD.NET client (Desktop probe/refresh)' 'critical' ([bool]$adomdDll) `
    $(if ($adomdDll) { $adomdDll.FullName } else { 'not in the nuget cache - probe_desktop_query.py / refresh_pbip_model.py cannot reach an open Desktop model' }) `
    'Restore the ADOMD.NET client (a DIFFERENT nuget package from the TOM/AMO one the .NET-SDK check covers), forcing a supported TFM so the add cannot silently no-op on a net10 default: dotnet new console -o $env:TEMP\adomd --framework net8.0; dotnet add $env:TEMP\adomd package Microsoft.AnalysisServices.AdomdClient.NetCore.retail.amd64 --version 19.84.1  (throwaway project; the restore populates the active NuGet global-packages cache the probe reads).'

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

Add-Check 'Fabric token tenant' $verdict.Tier $verdict.Ok $verdict.Detail $tenantHint

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
