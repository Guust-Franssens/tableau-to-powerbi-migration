"""Synthetic host parity controls for #727, never read an operator profile.

Registry transcription: VS Code 1.144.0, commit 2c33b50b16c472efe58357d1d2a8a5414bc23fbf,
src/vs/workbench/contrib/chat/test/common/plugins/fileBackedInstalledPluginsStore.test.ts
lines 166-172 and 282-311. The latter preserves literal spaces, # and %20 on relocation.
"""

from __future__ import annotations

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
MARKETPLACES = dict(zip(KINDS, ("tableau-collection", "fabric-collection", "synthetic"), strict=True))
AUTHORING = "plugin: powerbi-authoring@fabric-collection"
BUNDLES = "plugin: reusable Power BI skill bundles"
SELECTORS = ("T2P_PLUGIN_HOST=cli", "T2P_PLUGIN_HOST=vscode")


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
    return Path(environment["USERPROFILE"]) / ".vscode" / "agent-plugins"


def _content(root: Path, kind: str) -> Path:
    manifest = root / ".github" / "plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": NAMES[kind], "version": "0.3.19"}), encoding="utf-8")
    skills = {"engine": ("tableau-migration",), "authoring": ("powerbi-report-cli",), "bundles": SHIPPED_SKILLS}
    for skill in skills[kind]:
        leaf = root / "skills" / skill / "SKILL.md"
        leaf.parent.mkdir(parents=True, exist_ok=True)
        # Bundles carry the repo's bytes so preflight's drift check is not what a test observes.
        shipped = ROOT / ".github" / "skills" / skill / "SKILL.md"
        leaf.write_bytes(shipped.read_bytes() if kind == "bundles" else f"# Synthetic {skill}\n".encode())
    if kind == "engine":
        (root / "skills" / "tableau-migration" / "VERSION").write_text("2.126.0", encoding="utf-8")
    return root


def _write_config(environment: dict[str, str], entries: list[object]) -> None:
    config = Path(environment["COPILOT_HOME"]) / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    payload = {"url": "https://example.invalid/catalog", "installedPlugins": entries}
    config.write_text("// synthetic JSONC\n" + json.dumps(payload), encoding="utf-8")


def _cli_entry(root: Path) -> dict[str, object]:
    return {
        "name": NAMES["authoring"],
        "marketplace": "fabric-collection",
        "enabled": True,
        "version": "0.3.19",
        "cache_path": str(root),
    }


def _install(environment: dict[str, str], host: str, kind: str) -> Path:
    if host == "cli":
        root = _content(_cli_root(environment, kind), kind)
        if kind == "authoring":
            _write_config(environment, [_cli_entry(root)])
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


def _rewrite_registry(environment: dict[str, str], edit) -> None:
    registry = _cache(environment) / "installed.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    edit(payload)
    registry.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("kind", KINDS)
def test_selected_vscode_remedy_never_installs_into_cli(
    profile: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """A refused explicit VS Code selection must offer a repair in the same host, never a CLI fallback."""
    _install(profile, "cli", kind)
    assert engine.discover_installed_plugin(kind, env={**profile, "T2P_PLUGIN_HOST": "cli"}).ok
    environment = {**profile, "T2P_PLUGIN_HOST": "vscode"}
    evidence = engine.discover_installed_plugin(kind, env=environment)
    assert not evidence.ok, "an explicit missing host must not fall back"
    assert evidence.plugin_root is None
    assert "VS Code" in evidence.install_hint
    assert "copilot plugin" not in evidence.install_hint
    if kind == "bundles":
        assert bundles.discover_skill_plugin(env=environment).install_hint == evidence.install_hint
    if kind == "engine":
        monkeypatch.setenv("T2P_PLUGIN_HOST", "vscode")
        with pytest.raises(engine.EngineNotFoundError, match="VS Code"):
            engine.engine_root()
        assert engine.main([]) == 1
        assert evidence.install_hint in capsys.readouterr().out


@pytest.mark.parametrize("kind", KINDS)
def test_selected_cli_never_borrows_vscode(profile: dict[str, str], kind: str) -> None:
    """An explicit CLI selection refuses rather than falling back to a VS Code copy."""
    _install(profile, "vscode", kind)
    assert engine.discover_installed_plugin(kind, env=profile).ok
    verdict = engine.discover_installed_plugin(kind, env={**profile, "T2P_PLUGIN_HOST": "cli"})
    assert (verdict.ok, verdict.host) == (False, "cli")


@pytest.mark.parametrize("host", ["cli", "vscode"])
def test_simulated_absence_hides_the_installed_engine(
    profile: dict[str, str], monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """The test-only absence hook still wins over either installed host after the #727 move."""
    root = _install(profile, host, "engine")
    assert engine.engine_root() == root
    monkeypatch.setenv(engine.SIMULATE_ENGINE_ABSENT_ENV, "1")
    with pytest.raises(engine.EngineNotFoundError):
        engine.engine_root()
    assert not engine.status()["ok"]


@pytest.mark.parametrize("host", ["cli", "vscode"])
@pytest.mark.parametrize("kind", KINDS)
def test_shared_selection_reaches_public_consumers(profile: dict[str, str], host: str, kind: str) -> None:
    """Public consumers must resolve the same selected physical installation."""
    root = _install(profile, host, kind)
    environment = {**profile, "T2P_PLUGIN_HOST": host}
    evidence = engine.discover_installed_plugin(kind, env=environment)
    assert evidence.ok, evidence.detail
    assert evidence.plugin_root == root
    assert engine.discover_installed_plugin(kind, env=profile).plugin_root == root, "one host needs no selector"
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
def test_dual_host_refuses_and_names_the_selector(
    profile: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """#727: "refuse distinct roots even at equal version strings"; the refusal says how to choose a host."""
    cli, vscode = (_install(profile, host, kind) for host in ("cli", "vscode"))
    verdict = engine.discover_installed_plugin(kind, env=profile)
    assert (verdict.status, verdict.host, verdict.plugin_root) == ("multiple", None, None)
    assert all(selector in verdict.install_hint for selector in SELECTORS), verdict.install_hint
    assert str(cli) in verdict.detail and str(vscode) in verdict.detail
    for host, root in (("cli", cli), ("vscode", vscode)):
        assert engine.discover_installed_plugin(kind, env={**profile, "T2P_PLUGIN_HOST": host}).plugin_root == root
    if kind == "bundles":
        refused = bundles.discover_skill_plugin(env=profile)
        assert (refused.status, refused.install_hint) == ("multiple", verdict.install_hint)
        assert bundles.main([]) == 1
        assert verdict.install_hint in capsys.readouterr().out
        monkeypatch.setenv("T2P_PLUGIN_HOST", "vscode")
        assert bundles.main([]) == 0
    elif kind == "authoring":
        assert engine.status()["authoring_plugin"]["install_hint"] == verdict.install_hint
    else:
        with pytest.raises(engine.EngineNotFoundError, match="T2P_PLUGIN_HOST=vscode"):
            engine.engine_root()
        assert engine.main([]) == 1
        out = capsys.readouterr().out
        assert verdict.install_hint in out and str(vscode) in out, "the human output names both copies and the fix"
        monkeypatch.setenv("T2P_PLUGIN_HOST", "cli")
        assert engine.engine_root() == cli


@pytest.mark.parametrize("kind", KINDS)
def test_one_tree_registered_by_both_hosts_is_one_install(profile: dict[str, str], kind: str) -> None:
    """#727 deduplicates identical physical roots: one tree seen by both hosts is not ambiguous."""
    cache = _cache(profile)
    environment = {**profile, "COPILOT_HOME": str(cache / "copilot-home")}
    root = _install(environment, "cli", kind)
    entry = {
        "pluginUri": root.as_uri(),
        "relativePath": root.relative_to(cache).as_posix(),
        "marketplace": "synthetic/plugins",
        "name": NAMES[kind],
    }
    (cache / "installed.json").write_text(json.dumps({"version": 1, "installed": [entry]}), encoding="utf-8")
    assert engine.discover_installed_plugin(kind, env={**environment, "T2P_PLUGIN_HOST": "vscode"}).plugin_root == root
    verdict = engine.discover_installed_plugin(kind, env=environment)
    assert (verdict.status, verdict.plugin_root) == ("found", root), verdict.detail


@pytest.mark.parametrize("kind", ["engine", "authoring"])
def test_vscode_identity_is_the_registered_name_not_the_files(profile: dict[str, str], kind: str) -> None:
    """Re-review repro: an `evil-plugin` entry carrying engine files must not become the engine."""
    root = _install(profile, "vscode", kind)
    impostor = _content(root.parent / "evil-plugin", kind)
    if kind != "bundles":
        (impostor / ".github" / "plugin" / "plugin.json").write_text(
            json.dumps({"name": "evil-plugin"}), encoding="utf-8"
        )
    _rewrite_registry(
        profile,
        lambda payload: payload["installed"].extend(
            [
                {"pluginUri": impostor.as_uri(), "name": "evil-plugin"},
                {"pluginUri": impostor.as_uri()},
                {"pluginUri": impostor.as_uri(), "marketplace": MARKETPLACES[kind]},
            ]
        ),
    )
    selected = {**profile, "T2P_PLUGIN_HOST": "vscode"}
    verdict = engine.discover_installed_plugin(kind, env=selected)
    assert (verdict.status, verdict.plugin_root) == ("found", root), verdict.detail
    _rewrite_registry(profile, lambda payload: payload["installed"].pop(0))
    verdict = engine.discover_installed_plugin(kind, env=selected)
    assert (verdict.status, verdict.plugin_root) == ("missing", None), "a wrong or absent name is not identity"


@pytest.mark.parametrize("kind", ["engine", "authoring"])
def test_vscode_registry_supports_default_directory_and_unnamed_entries(profile: dict[str, str], kind: str) -> None:
    """Resolve the real per-user registry layout when VS Code omits the optional name."""
    root = _install(profile, "vscode", kind)
    assert _cache(profile) == Path(profile["USERPROFILE"]) / ".vscode" / "agent-plugins"
    _rewrite_registry(profile, lambda payload: payload["installed"][0].pop("name"))
    verdict = engine.discover_installed_plugin(kind, env={**profile, "T2P_PLUGIN_HOST": "vscode"})
    assert (verdict.status, verdict.plugin_root) == ("found", root), verdict.detail


def test_vscode_reports_an_existing_registry_without_the_target_plugin(profile: dict[str, str]) -> None:
    """Distinguish a valid registry with another plugin from an absent registry."""
    cache = _cache(profile)
    cache.mkdir(parents=True)
    (cache / "installed.json").write_text(
        json.dumps({"version": 1, "installed": [{"pluginUri": "file:///other", "marketplace": "azure-skills"}]}),
        encoding="utf-8",
    )
    verdict = engine.discover_installed_plugin("engine", env={**profile, "T2P_PLUGIN_HOST": "vscode"})
    assert verdict.status == "missing"
    assert "registry is absent" not in verdict.detail
    assert str(cache / "installed.json") in verdict.detail


def test_both_hosts_missing_reports_the_cli_repair(profile: dict[str, str]) -> None:
    """With nothing installed anywhere, the CLI (the pre-#727 host) owns the remedy."""
    verdict = engine.discover_installed_plugin("engine", env=profile)
    assert (verdict.status, verdict.host) == ("missing", "cli")
    assert "copilot plugin install tableau-fabric-skills@tableau-collection" in verdict.install_hint


def test_cli_missing_surfaces_the_vscode_refusal(profile: dict[str, str]) -> None:
    """A broken VS Code registry is reported as such, not hidden behind a CLI 'missing'."""
    _install(profile, "vscode", "engine")
    (_cache(profile) / "installed.json").write_text("{", encoding="utf-8")
    verdict = engine.discover_installed_plugin("engine", env=profile)
    assert (verdict.status, verdict.host) == ("invalid", "vscode")


def test_no_appdata_means_no_vscode_host(profile: dict[str, str]) -> None:
    """Non-Windows profiles have no APPDATA; automatic selection must still resolve the CLI."""
    root = _install(profile, "cli", "engine")
    environment = {key: value for key, value in profile.items() if key != "APPDATA"}
    verdict = engine.discover_installed_plugin("engine", env=environment)
    assert (verdict.status, verdict.plugin_root) == ("found", root)
    assert engine.discover_installed_plugin("engine", env={**environment, "T2P_PLUGIN_HOST": "vscode"}).status == (
        "missing"
    )


@pytest.mark.parametrize(
    "state",
    ["absent", "malformed", "stale", "non-object", "other-marketplace", "other-name", "no-cache-path", "empty-cache"],
)
def test_unusable_cli_registry_does_not_block_vscode(
    profile: dict[str, str], state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed, stale or foreign CLI config.json entry is not a CLI installation, so VS Code is used."""
    root = _install(profile, "vscode", "authoring")
    config = Path(profile["COPILOT_HOME"]) / "config.json"
    foreign_root = _content(Path(profile["COPILOT_HOME"]) / "foreign", "authoring")
    foreign = _cli_entry(foreign_root)
    if state == "empty-cache":
        # An empty cache_path would otherwise resolve against whatever directory the caller runs in.
        monkeypatch.chdir(foreign_root)
        _write_config(profile, [{**foreign, "cache_path": ""}])
    if state == "malformed":
        config.write_text("{", encoding="utf-8")
    elif state == "stale":
        _write_config(profile, [_cli_entry(Path(profile["COPILOT_HOME"]) / "deleted-cache")])
    elif state == "non-object":
        _write_config(profile, [42, "powerbi-authoring", None])
    elif state == "other-marketplace":
        _write_config(profile, [{**foreign, "marketplace": "other-collection"}])
    elif state == "other-name":
        _write_config(profile, [{**foreign, "name": "other-plugin"}])
    elif state == "no-cache-path":
        _write_config(profile, [{key: value for key, value in foreign.items() if key != "cache_path"}])
    verdict = engine.discover_installed_plugin("authoring", env=profile)
    assert verdict.ok, verdict.detail
    assert (verdict.host, verdict.plugin_root) == ("vscode", root)


def test_non_object_cli_entries_are_ignored(profile: dict[str, str]) -> None:
    """Only the matching registry object reaches preflight's CLI verdict, as before #727."""
    root = _install(profile, "cli", "authoring")
    _write_config(profile, [42, "powerbi-authoring", None, _cli_entry(root)])
    verdict = engine.discover_installed_plugin("authoring", env=profile)
    assert verdict.ok, verdict.detail
    assert verdict.entries == (_cli_entry(root),)


def test_explicit_installed_root_does_not_probe_other_profile(profile: dict[str, str]) -> None:
    """The existing CLI-directory override must not depend on an unrelated editor profile."""
    root = _install(profile, "cli", "bundles")
    installed = Path(profile["COPILOT_HOME"]) / "installed-plugins"
    _install(profile, "vscode", "bundles")
    verdict = bundles.discover_skill_plugin(installed_plugins_root=installed, env=profile)
    assert (verdict.status, verdict.plugin_root) == ("found", root), "a VS Code copy beside it is not ambiguity"
    (_cache(profile) / "installed.json").write_text("{", encoding="utf-8")
    verdict = bundles.discover_skill_plugin(installed_plugins_root=installed, env=profile)
    assert verdict.ok, verdict.detail
    assert verdict.plugin_root == root
    for selector in ("vscode", "newest"):
        explicit = bundles.discover_skill_plugin(
            installed_plugins_root=installed, env={**profile, "T2P_PLUGIN_HOST": selector}
        )
        assert explicit.plugin_root == root, "an explicit root is an override, like POWERBI_SKILLS_PLUGIN_ROOT"


@pytest.mark.parametrize("selector", ["", "auto", "VSCode", "vscode ", "newest"])
def test_invalid_selector_refuses_after_positive_setup(profile: dict[str, str], selector: str) -> None:
    """Invalid selection must refuse rather than silently reverting to automatic discovery."""
    _install(profile, "cli", "engine")
    assert engine.discover_installed_plugin("engine", env=profile).ok
    verdict = engine.discover_installed_plugin("engine", env={**profile, "T2P_PLUGIN_HOST": selector})
    assert (verdict.status, verdict.host) == ("invalid", None)
    assert "T2P_PLUGIN_HOST" in verdict.install_hint


@pytest.mark.parametrize(
    "state", ["malformed", "wrong-schema", "wrong-type", "traversal", "non-file-uri", "missing-cache"]
)
def test_invalid_registry_refuses_selected_vscode(profile: dict[str, str], state: str) -> None:
    """Malformed, escaping or incomplete VS Code evidence refuses; it never borrows the CLI copy."""
    cli = _install(profile, "cli", "engine")
    root = _install(profile, "vscode", "engine")
    environment = {**profile, "T2P_PLUGIN_HOST": "vscode"}
    assert engine.discover_installed_plugin("engine", env=environment).ok
    registry = _cache(profile) / "installed.json"
    if state == "malformed":
        registry.write_text("{", encoding="utf-8")
    elif state == "wrong-schema":
        _rewrite_registry(profile, lambda payload: payload.update(version=2))
    elif state == "wrong-type":
        _rewrite_registry(profile, lambda payload: payload.update(installed={}))
    elif state == "traversal":
        _content(_cache(profile).parent / "outside", "engine")
        _rewrite_registry(profile, lambda payload: payload["installed"][0].update(relativePath="../outside"))
    elif state == "non-file-uri":
        # The path still names the cached copy, so only the scheme check can refuse it.
        uri = root.as_uri().replace("file://", "https://example.invalid", 1)

        def remote(payload: dict) -> None:
            del payload["installed"][0]["relativePath"]
            payload["installed"][0]["pluginUri"] = uri

        _rewrite_registry(profile, remote)
    else:
        shutil.rmtree(root)
    assert not engine.discover_installed_plugin("engine", env=environment).ok
    automatic = engine.discover_installed_plugin("engine", env=profile)
    assert (automatic.ok, automatic.plugin_root) == (True, cli), "unusable VS Code evidence is no second copy"


def test_vscode_duplicates_are_multiple_and_aliases_are_one(profile: dict[str, str]) -> None:
    """Two registry routes to one directory are one install; two directories are ambiguous."""
    root = _install(profile, "vscode", "engine")
    environment = {**profile, "T2P_PLUGIN_HOST": "vscode"}
    _rewrite_registry(
        profile, lambda payload: payload["installed"].append({"pluginUri": root.as_uri(), "name": NAMES["engine"]})
    )
    verdict = engine.discover_installed_plugin("engine", env=environment)
    assert verdict.ok, verdict.detail
    assert verdict.plugin_root == root
    second = _content(root.parent / "second", "engine")
    _rewrite_registry(
        profile, lambda payload: payload["installed"].append({"pluginUri": second.as_uri(), "name": NAMES["engine"]})
    )
    verdict = engine.discover_installed_plugin("engine", env=environment)
    assert verdict.status == "multiple"
    assert set(verdict.candidates) == {root, second}
    assert "duplicate" in verdict.install_hint


def test_unusable_vscode_entries_are_skipped_not_fatal(profile: dict[str, str]) -> None:
    """Schema-invalid or unresolvable entries name no installation and must not crash a CLI-primary machine."""
    cli = _install(profile, "cli", "engine")
    root = _install(profile, "vscode", "engine")
    relative = root.relative_to(_cache(profile)).as_posix()
    named = NAMES["engine"]  # named, so each entry reaches the URI handling rather than the identity filter
    _rewrite_registry(
        profile,
        lambda payload: payload.update(
            installed=[
                42,
                {"pluginUri": 42, "name": named},
                {"relativePath": relative, "name": named},
                {"pluginUri": "file://[bad/x", "name": named},  # urlparse raises ValueError
                {"pluginUri": "file:///C:/a:b:c", "name": named},  # url2pathname raises OSError
            ]
        ),
    )
    selected = engine.discover_installed_plugin("engine", env={**profile, "T2P_PLUGIN_HOST": "vscode"})
    assert selected.status == "missing", selected.detail
    automatic = engine.discover_installed_plugin("engine", env=profile)
    assert (automatic.status, automatic.plugin_root) == ("found", cli)


def test_registered_cache_not_newest(profile: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Only registered cached bytes, not a newer sibling directory, may build a bundle."""
    registered = _install(profile, "vscode", "engine")
    newer = _content(_cache(profile) / "github.com/synthetic/plugins/sha_ffffffff/plugins/newer", "engine")
    (newer / "skills/tableau-migration/VERSION").write_text("9.9.9", encoding="utf-8")
    monkeypatch.setenv("T2P_PLUGIN_HOST", "vscode")
    assert engine.engine_root() == registered
    assert engine.engine_provenance()["version"] == "2.126.0"


def test_official_literal_relative_path(profile: dict[str, str]) -> None:
    """Preserve the official fixture's literal relative path during relocation."""
    root = _install(profile, "vscode", "engine")
    relative = "github.com/synthetic/plugins/plugins/my plugin#%20"
    relocated = _cache(profile) / relative
    relocated.parent.mkdir(parents=True, exist_ok=True)
    root.rename(relocated)
    _rewrite_registry(profile, lambda payload: payload["installed"][0].update(relativePath=relative))
    verdict = engine.discover_installed_plugin("engine", env={**profile, "T2P_PLUGIN_HOST": "vscode"})
    assert verdict.ok, verdict.detail
    assert verdict.plugin_root == relocated


def test_uri_only_path_is_decoded_once(profile: dict[str, str]) -> None:
    """Encoded literal percent text must not be interpreted a second time."""
    root = _install(profile, "vscode", "engine")
    relocated = root.parent / "literal %20# cache"
    root.rename(relocated)

    def uri_only(payload: dict) -> None:
        del payload["installed"][0]["relativePath"]
        payload["installed"][0]["pluginUri"] = relocated.as_uri()

    _rewrite_registry(profile, uri_only)
    verdict = engine.discover_installed_plugin("engine", env={**profile, "T2P_PLUGIN_HOST": "vscode"})
    assert verdict.ok, verdict.detail
    assert verdict.plugin_root == relocated


@pytest.mark.parametrize("host", ["cli", "vscode"])
def test_process_json_entrypoints(profile: dict[str, str], host: str) -> None:
    """Process-start discovery tells the three plugins apart by content and agrees with preflight."""
    expected = {kind: _install(profile, host, kind) for kind in KINDS}
    for script in ("engine_source.py", "skill_plugin_source.py"):
        child = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--json"],
            env={**os.environ, **profile},
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


def _preflight(environment: dict[str, str], *, python: str = "present") -> dict:
    """Execute only production plugin wiring, no full preflight or external operations.

    `python` is "present", "absent" (not on PATH) or "silent" (on PATH, but engine_source.py prints nothing).
    """
    source = (ROOT / "scripts/preflight.ps1").read_text(encoding="utf-8")
    engine_block = source[source.index("$engineStatus = $null") : source.index("# --- Engine receipt drift")]
    plugin_block = source[source.index("# --- Skill plugins ---") : source.index("# Recommended means")]
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
$py = $env:TEST_PY -ne 'absent'
function python { if ($env:TEST_PY -ne 'silent') { & $env:TEST_PYTHON @args } }
"""
        + engine_block
        + plugin_block
        + """
@{checks=@($results.ToArray());doctor=(Get-PreflightSummary $results)} | ConvertTo-Json -Depth 12 -Compress
"""
    )
    wiring = Path(environment["HOME"]).parent / "preflight-wiring.ps1"
    wiring.write_text(script, encoding="utf-8-sig")
    child = subprocess.run(
        [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wiring)],
        env={
            **os.environ,
            **environment,
            "TEST_PREFLIGHT": str(ROOT / "scripts/preflight.ps1"),
            "TEST_REPO": str(ROOT),
            "TEST_PYTHON": sys.executable,
            "TEST_PY": python,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    result["rows"] = {row["Name"]: row for row in result["checks"]}
    return result


@pytest.mark.skipif(PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize("host", ["cli", "vscode"])
def test_preflight_doctor_shared_evidence(profile: dict[str, str], host: str) -> None:
    """Production PowerShell wiring accepts installed evidence without false host failures."""
    roots = {kind: _install(profile, host, kind) for kind in KINDS}
    result = _preflight({**profile, "T2P_PLUGIN_HOST": host})
    rows = result["rows"]
    assert str(roots["engine"]) in rows["engine: plugin installed"]["Detail"]
    unverified = "installed (enablement not verified)"
    assert (unverified in rows["engine: plugin installed"]["Detail"]) == (host == "vscode")
    bundle_rows = [row for name, row in rows.items() if name.startswith("plugin: ") and name != AUTHORING]
    assert [(row["Ok"], unverified in row["Detail"]) for row in bundle_rows] == [(True, host == "vscode")]
    assert rows["engine: single source"]["Ok"] is True
    authoring = rows[AUTHORING]
    assert authoring["Ok"] is True
    if host == "vscode":
        assert authoring["Detail"] == "0.3.19 (known-good) - installed (enablement not verified)"
    else:
        assert authoring["Detail"] == "0.3.19 (known-good)", "CLI-only output must remain byte-compatible"
    assert rows["skill bundles installed"]["Ok"] is True
    assert result["doctor"]["blockers"] == []
    safe = t2p._doctor_preflight(json.dumps(result["doctor"]).encode(), b"", 0)  # pylint: disable=protected-access
    assert safe["availability"] == "available"


@pytest.mark.skipif(PS is None, reason="no PowerShell on PATH")
def test_preflight_dual_host_refuses_with_the_selector_hint(profile: dict[str, str]) -> None:
    """#727 refuses distinct dual installations; each blocked row says how to choose, and choosing clears it."""
    for host in ("cli", "vscode"):
        for kind in KINDS:
            _install(profile, host, kind)
    result = _preflight(profile)
    rows = result["rows"]
    blocked = {"engine_plugin": "engine: plugin installed", "authoring_plugin": AUTHORING}
    blocked["reusable_skill_plugins"] = BUNDLES
    assert sorted(row["id"] for row in result["doctor"]["blockers"]) == sorted(blocked)
    for name in blocked.values():
        assert "installed for both" in rows[name]["Detail"], name
        assert all(selector in rows[name]["Hint"] for selector in SELECTORS), rows[name]["Hint"]
    safe = t2p._doctor_preflight(json.dumps(result["doctor"]).encode(), b"", 1)  # pylint: disable=protected-access
    assert safe["availability"] == "available"
    assert _preflight({**profile, "T2P_PLUGIN_HOST": "cli"})["doctor"]["blockers"] == []


@pytest.mark.skipif(PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    ("python", "why"), [("absent", "Python unavailable"), ("silent", "engine_source.py did not report")]
)
def test_preflight_without_engine_evidence_stays_closed_and_says_why(
    profile: dict[str, str], python: str, why: str
) -> None:
    """The authoring verdict needs engine_source.py's evidence; without it, it is unverified, not absent."""
    _install(profile, "cli", "authoring")
    result = _preflight(profile, python=python)
    authoring = result["rows"][AUTHORING]
    assert (authoring["Tier"], authoring["Ok"]) == ("critical", False)
    assert authoring["Detail"] == f"not verified ({why})"
    assert any(row["id"] == "authoring_plugin" for row in result["doctor"]["blockers"])


@pytest.mark.skipif(PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize("selector", [None, "cli", "vscode", "newest"])
def test_preflight_missing_bundles_severity_ignores_the_selector(profile: dict[str, str], selector: str | None) -> None:
    """A missing or refused bundle plugin is the same recommended gap however the host was chosen."""
    environment = profile if selector is None else {**profile, "T2P_PLUGIN_HOST": selector}
    rows = _preflight(environment)["rows"]
    bundle_rows = [row for name, row in rows.items() if name.startswith("plugin: ") and name != AUTHORING]
    assert [(row["Tier"], row["Ok"]) for row in bundle_rows] == [("recommended", False)]
    assert {"newest": "invalid T2P_PLUGIN_HOST"}.get(selector, "not installed/enabled") in bundle_rows[0]["Detail"]
    assert "skill bundles installed" not in rows
    # The engine row names the refusal of the host that was examined, not only a CLI path.
    reason = {"vscode": "registry is absent", "newest": "invalid T2P_PLUGIN_HOST"}.get(selector, "not installed at")
    assert reason in rows["engine: plugin installed"]["Detail"]


@pytest.mark.skipif(PS is None, reason="no PowerShell on PATH")
def test_preflight_alternative_engine_copy_is_a_diagnostic(profile: dict[str, str]) -> None:
    """#727: "Keep nonselected sibling checkouts visible as advisory diagnostics rather than requiring deletion."""
    _install(profile, "cli", "engine")
    _install(profile, "cli", "authoring")
    # engine_source.py lists siblings under Path.home(): USERPROFILE on Windows, HOME elsewhere.
    home = profile["USERPROFILE"] if os.name == "nt" else profile["HOME"]
    sibling = _content(Path(home) / "tableau-fabric-skills", "engine")
    result = _preflight(profile)
    row = result["rows"]["engine: single source"]
    assert (row["Tier"], row["Ok"]) == ("recommended", False)
    assert str(sibling) in row["Detail"]
    assert result["doctor"]["blockers"] == []


@pytest.mark.skipif(PS is None, reason="no PowerShell on PATH")
@pytest.mark.parametrize(
    "state", ["missing", "wrong-name", "below-floor", "missing-skill", "multiple", "invalid-selector"]
)
def test_refusals_are_doctor_owner_blockers(profile: dict[str, str], state: str) -> None:
    """Host refusals remain closed, safe records accepted by the installed doctor."""
    _install(profile, "vscode", "engine")
    root = _install(profile, "vscode", "authoring")
    environment = {**profile, "T2P_PLUGIN_HOST": "vscode"}
    assert _preflight(environment)["doctor"]["critical_missing"] == 0
    manifest = root / ".github/plugin/plugin.json"
    if state == "missing":
        shutil.rmtree(root)
    elif state == "wrong-name":
        manifest.write_text('{"name":"wrong","version":"0.3.19"}', encoding="utf-8")
    elif state == "below-floor":
        manifest.write_text('{"name":"powerbi-authoring","version":"0.3.18"}', encoding="utf-8")
    elif state == "missing-skill":
        (root / "skills/powerbi-report-cli/SKILL.md").unlink()
    elif state == "multiple":
        second = _content(root.parent / "second", "authoring")
        _rewrite_registry(
            profile,
            lambda payload: payload["installed"].append({"pluginUri": second.as_uri(), "name": NAMES["authoring"]}),
        )
    else:
        environment["T2P_PLUGIN_HOST"] = "newest"
    preflight = _preflight(environment)
    reason = {
        "missing": "carries the authoring plugin",
        "wrong-name": "unreadable or wrong name",
        "below-floor": "0.3.18",
        "missing-skill": "carries the authoring plugin",
        "multiple": "multiple installed plugins",
        "invalid-selector": "invalid T2P_PLUGIN_HOST",
    }[state]
    assert reason in preflight["rows"][AUTHORING]["Detail"], "the refusal names the examined host's reason"
    result = preflight["doctor"]
    assert result["critical_missing"] > 0
    assert any(row["id"] == "authoring_plugin" for row in result["blockers"])
    safe = t2p._doctor_preflight(json.dumps(result).encode(), b"", 1)  # pylint: disable=protected-access
    assert safe["availability"] == "available"


def test_no_second_preflight_discovery_policy() -> None:
    """PowerShell must not maintain its own host root list or registry discovery."""
    source = (ROOT / "scripts/preflight.ps1").read_text(encoding="utf-8")
    assert "Read-CopilotJson 'config.json'" not in source
    assert "agent-plugins" not in source
    assert "installed.json" not in source
    assert "$engineStatus.authoring_plugin" in source
