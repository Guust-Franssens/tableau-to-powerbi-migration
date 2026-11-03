"""Synthetic host parity controls for #727, never read an operator profile.

Registry transcription: VS Code 1.144.0, commit 2c33b50b16c472efe58357d1d2a8a5414bc23fbf,
src/vs/workbench/contrib/chat/test/common/plugins/fileBackedInstalledPluginsStore.test.ts
lines 166-172 and 282-311. The latter preserves literal spaces, # and %20 on relocation.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import engine_source as engine  # noqa: E402  # pylint: disable=wrong-import-position
import skill_plugin_source as bundles  # noqa: E402  # pylint: disable=wrong-import-position
import t2p  # noqa: E402  # pylint: disable=wrong-import-position
from build_plugin import SHIPPED_SKILLS  # noqa: E402  # pylint: disable=wrong-import-position

PS = shutil.which("pwsh") or shutil.which("powershell")
KINDS = ("engine", "authoring", "bundles")
NAMES = dict(zip(KINDS, ("tableau-fabric-skills", "powerbi-authoring", "synthetic-bundles"), strict=True))


@pytest.fixture(name="profile")
def isolated_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Every default-profile read, including subprocesses, goes to synthetic paths."""
    environment = {key: str(tmp_path / key.lower()) for key in ("HOME", "USERPROFILE", "APPDATA", "COPILOT_HOME")}
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    for key in ("T2P_PLUGIN_HOST", bundles.PLUGIN_ROOT_ENV, engine.SIMULATE_ENGINE_ABSENT_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(engine, "DEFAULT_PLUGIN_ENGINE_ROOT", _cli_root(environment, "engine"))
    monkeypatch.setattr(engine, "PLUGIN_ENGINE_ROOT", engine.DEFAULT_PLUGIN_ENGINE_ROOT)
    monkeypatch.setattr(engine, "ALTERNATIVE_ENGINE_ROOTS", ())
    return environment


def _cli_root(environment: dict[str, str], kind: str) -> Path:
    marketplace = {"engine": "tableau-collection", "authoring": "fabric-collection", "bundles": "synthetic"}[kind]
    return Path(environment["COPILOT_HOME"]) / "installed-plugins" / marketplace / NAMES[kind]


def _cache(environment: dict[str, str]) -> Path:
    return Path(environment["APPDATA"]) / "Code" / "User" / "agent-plugins"


def _content(root: Path, kind: str) -> Path:
    manifest = root / ".github" / "plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": NAMES[kind], "version": "0.3.19"}), encoding="utf-8")
    skills = {
        "engine": ("tableau-migration",),
        "authoring": ("powerbi-report-cli", "powerbi-model-cli"),
        "bundles": SHIPPED_SKILLS,
    }[kind]
    for skill in skills:
        leaf = root / "skills" / skill / "SKILL.md"
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text(f"# Synthetic {skill}\n", encoding="utf-8")
    if kind == "engine":
        skill = root / "skills" / "tableau-migration"
        (skill / "VERSION").write_text("2.126.0", encoding="utf-8")
        (skill / "scripts").mkdir(exist_ok=True)
        (skill / "scripts" / "migrate_estate.py").write_text("# synthetic\n", encoding="utf-8")
    return root


def _install(environment: dict[str, str], host: str, kind: str) -> Path:
    if host == "cli":
        root = _content(_cli_root(environment, kind), kind)
        if kind == "authoring":
            (Path(environment["COPILOT_HOME"]) / "config.json").write_text(
                "// synthetic JSONC\n"
                + json.dumps(
                    {
                        "url": "https://example.invalid/catalog",
                        "installedPlugins": [
                            {
                                "name": NAMES[kind],
                                "marketplace": "fabric-collection",
                                "enabled": True,
                                "version": "0.3.19",
                                "cache_path": str(root),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
        return root
    cache = _cache(environment)
    relative = f"github.com/synthetic/plugins/sha_0123456789abcdef/plugins/{NAMES[kind]}"
    root = _content(cache / relative, kind)
    registry = cache / "installed.json"
    entries = json.loads(registry.read_text(encoding="utf-8"))["installed"] if registry.exists() else []
    entries.append(
        {
            "pluginUri": (cache.parent / "old-home" / relative).as_uri(),
            "relativePath": relative,
            "enablementUri": (cache.parent / "old-home" / relative).as_uri(),
            "marketplace": "synthetic/plugins",
            "name": NAMES[kind],
        }
    )
    registry.write_text(json.dumps({"version": 1, "installed": entries}), encoding="utf-8")
    return root


@pytest.mark.parametrize("host", ["cli", "vscode"])
@pytest.mark.parametrize("kind", KINDS)
def test_shared_selection_reaches_public_consumers(profile: dict[str, str], host: str, kind: str) -> None:
    """Public consumers must resolve the same selected physical installation."""
    root = _install(profile, host, kind)
    environment = {**profile, "T2P_PLUGIN_HOST": host}
    evidence = engine.discover_installed_plugin(kind, env=environment)
    assert evidence.ok, evidence.detail
    assert evidence.plugin_root == root
    if kind == "bundles":
        assert bundles.discover_skill_plugin(env=environment).plugin_root == root
    else:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("T2P_PLUGIN_HOST", host)
            if kind == "engine":
                assert engine.engine_root() == root
                assert engine.status()["root"] == str(root)
                assert engine.engine_provenance()["root"] == str(root)
                assert engine.engine_provenance()["canonical"] is True
            else:
                assert engine.status()["authoring_plugin"]["plugin_root"] == str(root)


@pytest.mark.parametrize("kind", KINDS)
def test_explicit_missing_host_has_no_fallback(profile: dict[str, str], kind: str) -> None:
    """A positive installation in another host must not rescue a selected missing host."""
    _install(profile, "cli", kind)
    assert engine.discover_installed_plugin(kind, env={**profile, "T2P_PLUGIN_HOST": "cli"}).ok
    verdict = engine.discover_installed_plugin(kind, env={**profile, "T2P_PLUGIN_HOST": "vscode"})
    assert not verdict.ok, "explicit missing host must not fall back"
    assert verdict.plugin_root is None


@pytest.mark.parametrize("kind", KINDS)
def test_two_distinct_roots_refuse_even_equal_versions(profile: dict[str, str], kind: str) -> None:
    """Equal versions are not evidence of one physical installation."""
    roots = {_install(profile, host, kind) for host in ("cli", "vscode")}
    for host in ("cli", "vscode"):
        assert engine.discover_installed_plugin(kind, env={**profile, "T2P_PLUGIN_HOST": host}).ok
    verdict = engine.discover_installed_plugin(kind, env=profile)
    assert verdict.status == "multiple", "distinct installed roots must be ambiguous"
    assert set(verdict.candidates) == roots
    assert not verdict.ok


def test_identical_physical_roots_deduplicate(profile: dict[str, str]) -> None:
    """Two registry routes to the same directory do not create a second installation."""
    root = _install(profile, "vscode", "engine")
    alias = _cli_root(profile, "engine")
    alias.parent.mkdir(parents=True)
    if os.name == "nt":
        child = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(root)], capture_output=True, text=True, check=False
        )
        assert child.returncode == 0, child.stderr
    else:
        alias.symlink_to(root, target_is_directory=True)
    verdict = engine.discover_installed_plugin("engine", env=profile)
    assert verdict.ok, verdict.detail
    assert verdict.plugin_root.resolve() == root


def test_explicit_installed_root_does_not_probe_other_profile(profile: dict[str, str]) -> None:
    """The existing CLI-directory override must not depend on an unrelated editor profile."""
    root = _install(profile, "cli", "bundles")
    installed = Path(profile["COPILOT_HOME"]) / "installed-plugins"
    assert bundles.discover_skill_plugin(installed_plugins_root=installed, env=profile).plugin_root == root
    _install(profile, "vscode", "bundles")
    (_cache(profile) / "installed.json").write_text("{", encoding="utf-8")
    verdict = bundles.discover_skill_plugin(installed_plugins_root=installed, env=profile)
    assert verdict.ok, verdict.detail
    assert verdict.plugin_root == root
    assert not bundles.discover_skill_plugin(
        installed_plugins_root=installed, env={**profile, "T2P_PLUGIN_HOST": "vscode"}
    ).ok


@pytest.mark.parametrize("selector", ["", "auto", "VSCode", "vscode ", "newest"])
def test_invalid_selector_refuses_after_positive_setup(profile: dict[str, str], selector: str) -> None:
    """Invalid selection must refuse rather than silently reverting to automatic discovery."""
    _install(profile, "cli", "engine")
    assert engine.discover_installed_plugin("engine", env=profile).ok
    verdict = engine.discover_installed_plugin("engine", env={**profile, "T2P_PLUGIN_HOST": selector})
    assert not verdict.ok, "invalid selector must refuse"
    assert verdict.status == "invalid"


@pytest.mark.parametrize(
    "state", ["malformed", "wrong-schema", "wrong-type", "traversal", "missing-cache", "invalid-uri"]
)
def test_invalid_registry_cannot_use_other_host(profile: dict[str, str], state: str) -> None:
    """Malformed or incomplete installed evidence cannot trigger cross-host fallback."""
    _install(profile, "cli", "engine")
    root = _install(profile, "vscode", "engine")
    environment = {**profile, "T2P_PLUGIN_HOST": "vscode"}
    assert engine.discover_installed_plugin("engine", env=environment).ok
    registry = _cache(profile) / "installed.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    if state == "malformed":
        registry.write_text("{", encoding="utf-8")
    else:
        if state == "wrong-schema":
            payload["version"] = 2
        elif state == "wrong-type":
            payload["installed"] = {}
        elif state == "traversal":
            payload["installed"][0]["relativePath"] = "../outside"
        elif state == "invalid-uri":
            payload["installed"][0]["pluginUri"] = "https://example.invalid/not-a-local-install"
        else:
            shutil.rmtree(root)
        registry.write_text(json.dumps(payload), encoding="utf-8")
    assert not engine.discover_installed_plugin("engine", env=environment).ok
    assert not engine.discover_installed_plugin("engine", env=profile).ok
    assert engine.discover_installed_plugin("engine", env={**profile, "T2P_PLUGIN_HOST": "cli"}).ok


@pytest.mark.parametrize("kind", KINDS)
def test_conflicting_manifests_refuse(profile: dict[str, str], kind: str) -> None:
    """Contradictory manifests are not a usable installed identity."""
    root = _install(profile, "vscode", kind)
    preferred = root / ".plugin/plugin.json"
    preferred.parent.mkdir()
    preferred.write_text(json.dumps({"name": NAMES[kind], "version": "0.3.19"}), encoding="utf-8")
    environment = {**profile, "T2P_PLUGIN_HOST": "vscode"}
    assert engine.discover_installed_plugin(kind, env=environment).ok
    manifest = root / ".github/plugin/plugin.json"
    manifest.write_text(json.dumps({"name": NAMES[kind], "version": "9.9.9"}), encoding="utf-8")
    assert not engine.discover_installed_plugin(kind, env=environment).ok, "conflicting manifests must refuse"


def test_registered_cache_not_newest(profile: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Only registered cached bytes, not a newer sibling directory, may build a bundle."""
    registered = _install(profile, "vscode", "engine")
    newer = _content(_cache(profile) / "github.com/synthetic/plugins/sha_ffffffff/plugins/newer", "engine")
    (newer / "skills/tableau-migration/VERSION").write_text("9.9.9", encoding="utf-8")
    monkeypatch.setenv("T2P_PLUGIN_HOST", "vscode")
    assert engine.engine_root() == registered
    assert engine.engine_provenance()["version"] == "2.126.0"


def test_cli_effective_manifest_policy_is_unchanged(profile: dict[str, str]) -> None:
    """CLI native metadata wins even when a lower-priority manifest disagrees."""
    root = _install(profile, "cli", "authoring")
    (root / "skills/powerbi-model-cli/SKILL.md").unlink()
    lower = root / ".claude-plugin/plugin.json"
    lower.parent.mkdir()
    lower.write_text('{"name":"unrelated","version":"9.9.9"}', encoding="utf-8")
    verdict = engine.discover_installed_plugin("authoring", env={**profile, "T2P_PLUGIN_HOST": "cli"})
    assert verdict.ok, verdict.detail
    assert verdict.manifest == {"name": "powerbi-authoring", "version": "0.3.19"}
    assert verdict.entries[0]["enabled"] is True


def test_official_literal_relative_path(profile: dict[str, str]) -> None:
    """Preserve the official fixture's literal relative path during relocation."""
    root = _install(profile, "vscode", "engine")
    registry = _cache(profile) / "installed.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    relative = "github.com/synthetic/plugins/plugins/my plugin#%20"
    relocated = _cache(profile) / relative
    relocated.parent.mkdir(parents=True, exist_ok=True)
    root.rename(relocated)
    payload["installed"][0]["relativePath"] = relative
    registry.write_text(json.dumps(payload), encoding="utf-8")
    verdict = engine.discover_installed_plugin("engine", env={**profile, "T2P_PLUGIN_HOST": "vscode"})
    assert verdict.ok, verdict.detail
    assert verdict.plugin_root == relocated


def test_uri_only_path_is_decoded_once(profile: dict[str, str]) -> None:
    """Encoded literal percent text must not be interpreted a second time."""
    root = _install(profile, "vscode", "engine")
    registry = _cache(profile) / "installed.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    relocated = root.parent / "literal %20# cache"
    root.rename(relocated)
    entry = payload["installed"][0]
    del entry["relativePath"]
    entry["pluginUri"] = relocated.as_uri()
    registry.write_text(json.dumps(payload), encoding="utf-8")
    verdict = engine.discover_installed_plugin("engine", env={**profile, "T2P_PLUGIN_HOST": "vscode"})
    assert verdict.ok, verdict.detail
    assert verdict.plugin_root == relocated


@pytest.mark.parametrize("host", ["cli", "vscode"])
def test_process_json_entrypoints(profile: dict[str, str], host: str) -> None:
    """Process-start discovery and preflight helpers must agree on the selected caches."""
    expected = {kind: _install(profile, host, kind) for kind in KINDS}
    for script in ("engine_source.py", "skill_plugin_source.py"):
        child = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--json"],
            env={**os.environ, **profile, "T2P_PLUGIN_HOST": host},
            capture_output=True,
            text=True,
            check=False,
        )
        assert child.returncode == 0, child.stdout + child.stderr
        result = json.loads(child.stdout)
        if script == "engine_source.py":
            assert result["root"] == str(expected["engine"])
            assert result["authoring_plugin"]["plugin_root"] == str(expected["authoring"])
        else:
            assert result["plugin_root"] == str(expected["bundles"])


def _preflight(environment: dict[str, str]) -> dict:
    """Execute only production plugin wiring, no full preflight or external operations."""
    source = (ROOT / "scripts/preflight.ps1").read_text(encoding="utf-8")
    engine_block = source[source.index("$engineStatus = $null") : source.index("# --- Engine receipt drift")]
    authoring = source[source.index("$cfg = [pscustomobject]") : source.index("$skillPlugin = $null")]
    script = (
        """
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TEST_PREFLIGHT,[ref]$null,[ref]$null)
$defs = $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]},$true)
foreach ($name in @('Add-Check','ConvertFrom-CliVersionOutput','Get-VersionFloorVerdict','Get-PreflightSummary')) {
    $fn = $defs | Where-Object {$_.Name -eq $name} | Select-Object -First 1
    if (-not $fn) {throw "Missing function $name"}
    . ([scriptblock]::Create($fn.Extent.Text))
}
$results = New-Object System.Collections.Generic.List[object]
$repoRoot = $env:TEST_REPO
$py = $true
function python { & $env:TEST_PYTHON @args }
"""
        + engine_block
        + authoring
        + """
@{checks=@($results.ToArray());doctor=(Get-PreflightSummary $results)} | ConvertTo-Json -Depth 12 -Compress
"""
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    child = subprocess.run(
        [PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        env={
            **os.environ,
            **environment,
            "TEST_PREFLIGHT": str(ROOT / "scripts/preflight.ps1"),
            "TEST_REPO": str(ROOT),
            "TEST_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout)


@pytest.mark.skipif(PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize("host", ["cli", "vscode"])
def test_preflight_doctor_shared_evidence(profile: dict[str, str], host: str) -> None:
    """Production PowerShell wiring accepts installed evidence without false host failures."""
    root = _install(profile, host, "engine")
    _install(profile, host, "authoring")
    result = _preflight({**profile, "T2P_PLUGIN_HOST": host})
    checks = {row["Name"]: row for row in result["checks"]}
    assert checks["engine: plugin installed"]["Ok"] is True
    assert str(root) in checks["engine: plugin installed"]["Detail"]
    assert checks["engine: single source"]["Ok"] is True
    authoring = checks["plugin: powerbi-authoring@fabric-collection"]
    assert authoring["Ok"] is True
    if host == "vscode":
        assert "installed (enablement not verified)" in authoring["Detail"]
    else:
        assert authoring["Detail"] == "0.3.19 (known-good)", "CLI-only output must remain byte-compatible"
    assert result["doctor"]["blockers"] == []
    assert (
        t2p._doctor_preflight(  # pylint: disable=protected-access
            json.dumps(result["doctor"]).encode(), b"", 0
        )["availability"]
        == "available"
    )


@pytest.mark.skipif(PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize("state", ["missing", "conflict", "below-floor", "missing-skill", "invalid-selector"])
def test_refusals_are_doctor_owner_blockers(profile: dict[str, str], state: str) -> None:
    """Host refusals remain closed, safe records accepted by the installed doctor."""
    _install(profile, "vscode", "engine")
    root = _install(profile, "vscode", "authoring")
    environment = {**profile, "T2P_PLUGIN_HOST": "vscode"}
    assert _preflight(environment)["doctor"]["critical_missing"] == 0
    if state == "missing":
        shutil.rmtree(root)
    elif state == "conflict":
        manifest = root / ".plugin/plugin.json"
        manifest.parent.mkdir()
        manifest.write_text('{"name":"wrong","version":"0.3.19"}', encoding="utf-8")
    elif state == "below-floor":
        (root / ".github/plugin/plugin.json").write_text(
            '{"name":"powerbi-authoring","version":"0.3.18"}', encoding="utf-8"
        )
    elif state == "missing-skill":
        (root / "skills/powerbi-model-cli/SKILL.md").unlink()
    else:
        environment["T2P_PLUGIN_HOST"] = "newest"
    result = _preflight(environment)["doctor"]
    assert result["critical_missing"] > 0
    assert any(row["id"] == "authoring_plugin" for row in result["blockers"])
    assert (
        t2p._doctor_preflight(  # pylint: disable=protected-access
            json.dumps(result).encode(), b"", 1
        )["availability"]
        == "available"
    )


def test_no_second_preflight_discovery_policy() -> None:
    """PowerShell must not maintain its own host root list or registry discovery."""
    source = (ROOT / "scripts/preflight.ps1").read_text(encoding="utf-8")
    assert "Read-CopilotJson 'config.json'" not in source
    assert "agent-plugins" not in source
    assert "installed.json" not in source
    assert "$engineStatus.authoring_plugin" in source
