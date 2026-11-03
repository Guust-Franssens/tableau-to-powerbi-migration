"""
purpose: resolve THE deterministic conversion engine - the installed Copilot plugin, and only that -
         and fail loudly instead of silently falling back to a second copy on the same machine.
usage:   python scripts/engine_source.py [--json]
         from engine_source import engine_root, engine_scripts_dir, engine_version, engine_provenance

Why this exists (issue #107)
----------------------------
The engine is a separate project that this repo does not pin, and it resolves at RUNTIME. Until this
module existed, three different files each carried their own candidate list and each preferred a
different tree:

    harvest_estate_assets.py   installed plugin, else a sibling clone   (first hit wins, silently)
    dax_oracle_server.py       installed plugin, else a sibling clone   (first hit wins, silently)
    run_estate.py              ~/vscode-projects/tableau-fabric-skills  (argparse default)
    transpile_tableau_calc.py  installed plugin only (hard-coded)

Measured 2026-08-12, that machine had the plugin at 2.113.0 and the sibling clone at 2.126.0, so ONE
pipeline could survey the site with 2.113.0 and convert with 2.126.0 - and nothing in the output said
so. The versions are not equivalent: 2.113.0 emits deprecated Bing `shapeMap`/`filledMap` visuals and
drops a density-map worksheet entirely, where 2.126.0 emits `azureMap` with a heat-map layer.

The owner's decision: **the installed plugin is the single canonical engine.** So this module has
exactly two jobs, and neither of them is "find an engine somewhere":

1. return the plugin's path/version, or RAISE - there is no fallback, by construction;
2. name every OTHER engine tree it can see, so `preflight.ps1` can FAIL on it. A second copy is not a
   convenience; it is the defect.

Keep the candidate list in `ALTERNATIVE_ENGINE_ROOTS` honest. A tree that is not listed is a tree
preflight cannot warn about - and an unlisted tree is exactly how #107 happened.
"""
# pylint: disable=too-many-arguments,too-many-boolean-expressions
# pylint: disable=too-many-branches,too-many-locals,too-many-return-statements,too-many-statements

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from urllib.request import url2pathname

from build_plugin import PLUGIN_NAME as BUNDLE_PLUGIN_NAME
from build_plugin import SHIPPED_SKILLS
from copilot_home import copilot_home

REPO_ROOT = Path(__file__).resolve().parent.parent

# Test simulation hook: when set, engine resolution acts as if the plugin is not installed.
SIMULATE_ENGINE_ABSENT_ENV = "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS"

# The one canonical location: the installed Copilot plugin `tableau-fabric-skills@tableau-collection`.
DEFAULT_PLUGIN_ENGINE_ROOT = copilot_home() / "installed-plugins" / "tableau-collection" / "tableau-fabric-skills"
PLUGIN_ENGINE_ROOT = DEFAULT_PLUGIN_ENGINE_ROOT

# Where the engine lives inside a `tableau-fabric-skills` tree, plugin or clone alike.
ENGINE_SKILL = Path("skills") / "tableau-migration"

# Upstream, for the advisory `preflight.ps1 -CheckUpstream` check. A raw VERSION fetch answers "has
# the world moved" in one HTTP GET; comparing git SHAs cannot, because the plugin is an unpacked
# marketplace copy with no `.git` at all.
UPSTREAM_REPO = "Yarbrdab000/tableau-fabric-skills"
UPSTREAM_VERSION_URL = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/main/{ENGINE_SKILL.as_posix()}/VERSION"

# Trees that MUST NOT exist, listed so preflight can block on them. These are the real locations this
# project has actually resolved an engine from; add to it rather than trusting that nobody will clone
# the engine somewhere new.
ALTERNATIVE_ENGINE_ROOTS = (
    REPO_ROOT.parent / "tableau-fabric-skills",
    Path.home() / "vscode-projects" / "tableau-fabric-skills",
    Path.home() / "tableau-fabric-skills",
    Path.home() / "source" / "repos" / "tableau-fabric-skills",
)

INSTALL_HINT = (
    "Install it BETWEEN Copilot sessions (a running session file-locks the plugin dir): "
    "copilot plugin install tableau-fabric-skills@tableau-collection"
)
PLUGIN_HOST_ENV = "T2P_PLUGIN_HOST"
AUTHORING_INSTALL_HINT = "Install or repair powerbi-authoring@fabric-collection between Copilot sessions."
VSCODE_INSTALL_HINT = "Install the required agent plugin in VS Code and restart the agent session."

PluginKind = Literal["engine", "authoring", "bundles"]


@dataclass(frozen=True)
class InstalledPluginDiscovery:  # pylint: disable=too-many-instance-attributes
    """Evidence for one installed plugin capability."""

    status: str
    plugin_root: Path | None
    candidates: tuple[Path, ...]
    identity: str
    host: str | None
    detail: str
    manifest: dict[str, object] | None
    entries: tuple[dict[str, object], ...]
    install_hint: str

    @property
    def ok(self) -> bool:
        """Whether exactly one usable installation was found."""
        return self.status == "found"

    def to_jsonable(self) -> dict[str, object]:
        """Return stable JSON evidence consumed by preflight."""
        return {
            "status": self.status,
            "ok": self.ok,
            "plugin_root": str(self.plugin_root) if self.plugin_root else None,
            "candidates": [str(path) for path in self.candidates],
            "identity": self.identity,
            "host": self.host,
            "detail": self.detail,
            "manifest": self.manifest,
            "entries": list(self.entries),
            "install_hint": self.install_hint,
        }


class EngineNotFoundError(RuntimeError):
    """The canonical engine plugin is not installed. Deliberately fatal - never fall back."""


class NonCanonicalEngineError(RuntimeError):
    """A caller asked for an engine that is not the canonical plugin."""


def _hint(kind: PluginKind, host: str | None = None) -> str:
    if kind == "engine":
        return INSTALL_HINT if host != "vscode" else VSCODE_INSTALL_HINT
    if kind == "authoring":
        return AUTHORING_INSTALL_HINT if host != "vscode" else VSCODE_INSTALL_HINT
    return (
        "Install it once between sessions: copilot plugin marketplace add "
        "Guust-Franssens/powerbi-playbook && "
        "copilot plugin install powerbi-playbook@powerbi-playbook-collection"
    )


def _identity(kind: PluginKind) -> str:
    return {
        "engine": "tableau-fabric-skills@tableau-collection",
        "authoring": "powerbi-authoring@fabric-collection",
        "bundles": "powerbi-playbook@powerbi-playbook-collection",
    }[kind]


def _evidence(
    kind: PluginKind,
    status_value: str,
    *,
    host: str | None,
    detail: str,
    plugin_root: Path | None = None,
    candidates: tuple[Path, ...] = (),
    identity: str | None = None,
    manifest: dict[str, object] | None = None,
    entries: tuple[dict[str, object], ...] = (),
) -> InstalledPluginDiscovery:
    return InstalledPluginDiscovery(
        status=status_value,
        plugin_root=plugin_root,
        candidates=candidates,
        identity=identity or _identity(kind),
        host=host,
        detail=detail,
        manifest=manifest,
        entries=entries,
        install_hint=_hint(kind, host),
    )


def _environment_home(environment: dict[str, str]) -> Path:
    configured = environment.get("COPILOT_HOME")
    if configured:
        return Path(configured)
    home = environment.get("HOME") or environment.get("USERPROFILE")
    return Path(home) / ".copilot" if home else Path(".copilot")


def _read_json(path: Path, *, leading_comments: bool = False) -> object:
    text = path.read_text(encoding="utf-8-sig")
    if leading_comments:
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    return json.loads(text)


def _is_agent_plugin_schema(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("https://agent-plugins.org/schemas/")
        and value.endswith("/plugin.schema.json")
    )


def _manifest_paths(root: Path) -> list[tuple[Path, bool]]:
    root_manifest = root / "plugin.json"
    paths: list[tuple[Path, bool]] = []
    if root_manifest.is_file():
        try:
            parsed = _read_json(root_manifest)
            schema = parsed.get("$schema") if isinstance(parsed, dict) else None
            if _is_agent_plugin_schema(schema):
                paths.append((root_manifest, True))
        except (OSError, UnicodeError, json.JSONDecodeError):
            paths.append((root_manifest, True))
    paths.extend(
        (path, False)
        for path in (
            root / ".plugin" / "plugin.json",
            root / ".claude-plugin" / "plugin.json",
            root_manifest,
            root / ".github" / "plugin" / "plugin.json",
        )
        if path.is_file() and not any(existing == path for existing, _ in paths)
    )
    return paths


def _read_manifest(root: Path) -> tuple[dict[str, object] | None, str | None]:
    manifests: list[tuple[Path, dict[str, object]]] = []
    for path, is_agent in _manifest_paths(root):
        try:
            value = _read_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return None, f"manifest is unreadable: {path} ({exc})"
        if not isinstance(value, dict):
            return None, f"manifest is not an object: {path}"
        if is_agent:
            schema = value.get("$schema")
            if not _is_agent_plugin_schema(schema):
                return None, f"unsupported Agent Plugin manifest: {path}"
        manifests.append((path, value))
    if not manifests:
        return None, f"no recognized plugin manifest under {root}"

    first_path, selected = manifests[0]
    for path, other in manifests[1:]:
        for field in ("name", "version"):
            left, right = selected.get(field), other.get(field)
            if left is not None and right is not None and left != right:
                return None, f"conflicting {field} in {first_path} and {path}"
        for field in ("skills", "components", "extensions"):
            if field in selected and field in other and selected[field] != other[field]:
                return None, f"conflicting {field} in {first_path} and {path}"
    return selected, None


def _read_cli_authoring_manifest(root: Path) -> tuple[dict[str, object] | None, str | None]:
    path = root / ".github" / "plugin" / "plugin.json"
    try:
        value = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"effective manifest is unreadable: {path} ({exc})"
    if not isinstance(value, dict):
        return None, f"effective manifest is not an object: {path}"
    return value, None


def _has_content(kind: PluginKind, root: Path, *, strict_engine: bool) -> bool:
    if kind == "engine":
        skill = root / ENGINE_SKILL
        if strict_engine:
            return all(
                path.is_file()
                for path in (
                    skill / "VERSION",
                    skill / "SKILL.md",
                    skill / "scripts" / "migrate_estate.py",
                )
            )
        return is_engine_tree(root)
    if kind == "authoring":
        return all(
            (root / "skills" / skill / "SKILL.md").is_file() for skill in ("powerbi-report-cli", "powerbi-model-cli")
        )
    return any((root / "skills" / skill / "SKILL.md").is_file() for skill in SHIPPED_SKILLS)


def _cli_discovery(
    kind: PluginKind,
    installed_plugins_root: Path | None,
    environment: dict[str, str],
) -> InstalledPluginDiscovery:
    home = _environment_home(environment)
    installed = Path(installed_plugins_root) if installed_plugins_root is not None else home / "installed-plugins"
    if kind == "engine":
        root = installed / "tableau-collection" / "tableau-fabric-skills"
        if not root.exists():
            return _evidence(kind, "missing", host="cli", detail=f"not installed at {root}")
        if not _has_content(kind, root, strict_engine=False):
            return _evidence(
                kind,
                "invalid",
                host="cli",
                plugin_root=root,
                candidates=(root,),
                detail=f"incomplete engine plugin at {root}",
            )
        manifest, _ = _read_manifest(root)
        return _evidence(
            kind,
            "found",
            host="cli",
            plugin_root=root,
            candidates=(root,),
            detail=f"found {root}",
            manifest=manifest,
        )

    if kind == "bundles":
        roots: list[Path] = []
        if installed.is_dir():
            for skills in sorted(installed.glob("*/*/skills")):
                if skills.is_dir() and _has_content(kind, skills.parent, strict_engine=False):
                    roots.append(skills.parent.resolve())
        evidence = _roots_evidence(kind, "cli", roots)
        if evidence.status == "missing":
            return _evidence(
                kind,
                "missing",
                host="cli",
                detail=f"no installed plugin under {installed.resolve()} carries any shipped skill bundle",
            )
        return evidence

    config = home / "config.json"
    if not config.is_file():
        return _evidence(kind, "missing", host="cli", detail=f"registry is absent: {config}")
    try:
        value = _read_json(config, leading_comments=True)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _evidence(kind, "invalid", host="cli", detail=f"registry is unreadable: {config} ({exc})")
    if not isinstance(value, dict) or not isinstance(value.get("installedPlugins"), list):
        return _evidence(kind, "invalid", host="cli", detail=f"registry has invalid installedPlugins: {config}")

    matching: list[dict[str, object]] = []
    malformed = False
    for entry in value["installedPlugins"]:
        if not isinstance(entry, dict):
            malformed = True
            continue
        if entry.get("name") == "powerbi-authoring" and entry.get("marketplace") == "fabric-collection":
            matching.append(entry)
    entries = tuple(matching)
    if malformed:
        return _evidence(
            kind, "invalid", host="cli", entries=entries, detail=f"registry contains malformed entries: {config}"
        )
    if not matching:
        return _evidence(kind, "missing", host="cli", entries=entries, detail="authoring registry entry is absent")
    if len(matching) > 1:
        roots = tuple(Path(path) for entry in matching if isinstance((path := entry.get("cache_path")), str) and path)
        return _evidence(
            kind,
            "multiple",
            host="cli",
            candidates=roots,
            entries=entries,
            detail="multiple powerbi-authoring@fabric-collection registry entries",
        )
    cache_path = matching[0].get("cache_path")
    if not isinstance(cache_path, str) or not cache_path:
        return _evidence(kind, "invalid", host="cli", entries=entries, detail="authoring cache_path is missing")
    root = Path(cache_path).expanduser().resolve()
    if not root.is_dir():
        return _evidence(
            kind,
            "invalid",
            host="cli",
            plugin_root=root,
            candidates=(root,),
            entries=entries,
            detail=f"authoring cache directory is missing: {root}",
        )
    manifest, error = _read_cli_authoring_manifest(root)
    if error or manifest is None or manifest.get("name") != "powerbi-authoring":
        return _evidence(
            kind,
            "invalid",
            host="cli",
            plugin_root=root,
            candidates=(root,),
            entries=entries,
            detail=error or f"authoring manifest has wrong name at {root}",
            manifest=manifest,
        )
    if not (root / "skills" / "powerbi-report-cli" / "SKILL.md").is_file():
        return _evidence(
            kind,
            "invalid",
            host="cli",
            plugin_root=root,
            candidates=(root,),
            entries=entries,
            detail=f"authoring required skills are missing at {root}",
            manifest=manifest,
        )
    return _evidence(
        kind,
        "found",
        host="cli",
        plugin_root=root,
        candidates=(root,),
        entries=entries,
        detail=f"found {root}",
        manifest=manifest,
    )


def _roots_evidence(kind: PluginKind, host: str, roots: list[Path]) -> InstalledPluginDiscovery:
    unique = tuple(dict.fromkeys(root.resolve() for root in roots))
    if len(unique) == 1:
        return _evidence(
            kind,
            "found",
            host=host,
            plugin_root=unique[0],
            candidates=unique,
            identity=(f"{unique[0].name}@{unique[0].parent.name}" if kind == "bundles" and host == "cli" else None),
            detail=f"found {unique[0]}",
        )
    if len(unique) > 1:
        return _evidence(
            kind,
            "multiple",
            host=host,
            candidates=unique,
            detail="multiple installed plugins: " + "; ".join(map(str, unique)),
        )
    return _evidence(kind, "missing", host=host, detail=f"no installed {kind} plugin found")


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _uri_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.query or parsed.fragment or parsed.netloc not in ("", "localhost"):
        return None
    native = url2pathname(parsed.path)
    if re.match(r"^/[A-Za-z]:/", native):
        native = native[1:]
    return Path(native)


def _vscode_entry_root(entry: dict[str, object], home: Path) -> tuple[Path | None, str | None]:
    uri = entry.get("pluginUri")
    if not isinstance(uri, str) or not uri or _uri_path(uri) is None:
        return None, "pluginUri is not a local file URI"
    relative = entry.get("relativePath")
    if relative is not None:
        if not isinstance(relative, str):
            return None, "relativePath is not a string"
        path = home / Path(relative)
    else:
        decoded = _uri_path(uri)
        assert decoded is not None
        path = decoded
    try:
        resolved_home = home.resolve()
        resolved = path.resolve()
    except OSError as exc:
        return None, f"plugin path cannot be resolved: {exc}"
    if not _is_within(resolved_home, resolved):
        return None, f"plugin path escapes {resolved_home}"
    return resolved, None


def _vscode_discovery(kind: PluginKind, environment: dict[str, str]) -> InstalledPluginDiscovery:
    appdata = environment.get("APPDATA")
    if not appdata:
        return _evidence(kind, "missing", host="vscode", detail="APPDATA is not configured")
    home = Path(appdata) / "Code" / "User" / "agent-plugins"
    registry = home / "installed.json"
    if not registry.is_file():
        return _evidence(kind, "missing", host="vscode", detail=f"registry is absent: {registry}")
    try:
        value = _read_json(registry)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _evidence(kind, "invalid", host="vscode", detail=f"registry is unreadable: {registry} ({exc})")
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("installed"), list):
        return _evidence(kind, "invalid", host="vscode", detail=f"registry schema is invalid: {registry}")

    valid: list[tuple[Path, dict[str, object], dict[str, object]]] = []
    invalid: list[str] = []
    for raw_entry in value["installed"]:
        if not isinstance(raw_entry, dict):
            invalid.append("registry entry is not an object")
            continue
        if not isinstance(raw_entry.get("pluginUri"), str) or not isinstance(raw_entry.get("marketplace"), str):
            invalid.append("registry entry is missing pluginUri or marketplace")
            continue
        record_name = raw_entry.get("name")
        if record_name is not None and not isinstance(record_name, str):
            invalid.append("registry entry name is not a string")
            continue
        expected = {"engine": "tableau-fabric-skills", "authoring": "powerbi-authoring"}.get(kind)
        claims_target = record_name == (expected or BUNDLE_PLUGIN_NAME)
        root, path_error = _vscode_entry_root(raw_entry, home)
        if path_error or root is None:
            if claims_target:
                invalid.append(path_error or "plugin path is invalid")
            continue
        manifest, manifest_error = _read_manifest(root)
        if manifest_error or manifest is None:
            if claims_target:
                invalid.append(manifest_error or f"manifest is missing at {root}")
            continue
        manifest_name = manifest.get("name")
        carries_content = _has_content(kind, root, strict_engine=True)
        targeted = carries_content or claims_target or manifest_name == (expected or BUNDLE_PLUGIN_NAME)
        if not targeted:
            continue
        if expected and (
            (record_name is not None and record_name != expected)
            or manifest_name != expected
            or (record_name is not None and record_name != manifest_name)
        ):
            invalid.append(f"plugin identity mismatch at {root}")
            continue
        if not carries_content:
            invalid.append(f"required {kind} content is missing at {root}")
            continue
        valid.append((root, raw_entry, manifest))

    if invalid:
        return _evidence(
            kind,
            "invalid",
            host="vscode",
            candidates=tuple(root for root, _, _ in valid),
            detail="; ".join(invalid),
        )
    roots = tuple(dict.fromkeys(root for root, _, _ in valid))
    if len(roots) > 1:
        return _evidence(
            kind,
            "multiple",
            host="vscode",
            candidates=roots,
            detail="multiple installed plugins: " + "; ".join(map(str, roots)),
        )
    if not roots:
        return _evidence(kind, "missing", host="vscode", detail=f"no registered {kind} plugin found")
    root = roots[0]
    _, entry, manifest = next(item for item in valid if item[0] == root)
    identity = str(manifest.get("name") or entry.get("name") or _identity(kind))
    return _evidence(
        kind,
        "found",
        host="vscode",
        plugin_root=root,
        candidates=roots,
        identity=identity,
        detail=f"found {root}; VS Code enablement is not verified",
        manifest=manifest,
    )


def discover_installed_plugin(
    kind: PluginKind,
    installed_plugins_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> InstalledPluginDiscovery:
    """Discover one installed plugin capability without scanning unregistered VS Code caches."""
    if kind not in ("engine", "authoring", "bundles"):
        raise ValueError(f"unsupported plugin kind: {kind}")
    environment = dict(os.environ) if env is None else dict(env)
    selector_present = PLUGIN_HOST_ENV in environment
    selector = environment.get(PLUGIN_HOST_ENV)
    if selector_present and selector not in ("cli", "vscode"):
        return _evidence(kind, "invalid", host=None, detail=f"invalid {PLUGIN_HOST_ENV}: {selector!r}")
    if installed_plugins_root is not None and not selector_present:
        return _cli_discovery(kind, installed_plugins_root, environment)
    selector = selector or "auto"
    if selector == "cli":
        return _cli_discovery(kind, installed_plugins_root, environment)
    if selector == "vscode":
        return _vscode_discovery(kind, environment)

    cli = _cli_discovery(kind, installed_plugins_root, environment)
    vscode = _vscode_discovery(kind, environment)
    blocking = [item for item in (cli, vscode) if item.status in ("invalid", "multiple")]
    if blocking:
        candidates = tuple(dict.fromkeys(path for item in blocking for path in item.candidates))
        state = "multiple" if any(item.status == "multiple" for item in blocking) else "invalid"
        return _evidence(
            kind,
            state,
            host=blocking[0].host,
            candidates=candidates,
            entries=cli.entries,
            detail="; ".join(f"{item.host}: {item.detail}" for item in blocking),
        )
    found = [item for item in (cli, vscode) if item.ok and item.plugin_root is not None]
    roots = tuple(dict.fromkeys(item.plugin_root.resolve() for item in found if item.plugin_root))
    if len(roots) > 1:
        return _evidence(
            kind,
            "multiple",
            host=None,
            candidates=roots,
            entries=cli.entries,
            detail="multiple installations across hosts: " + "; ".join(map(str, roots)),
        )
    if roots:
        selected = next(item for item in found if item.plugin_root and item.plugin_root.resolve() == roots[0])
        if any(item.host == "cli" and item.plugin_root and item.plugin_root.resolve() == roots[0] for item in found):
            selected = next(item for item in found if item.host == "cli")
        return selected
    return _evidence(
        kind,
        "missing",
        host=None,
        entries=cli.entries,
        detail=f"cli: {cli.detail}; vscode: {vscode.detail}",
    )


def version_tuple(version: str | None) -> tuple[int, ...]:
    """Parse a dotted VERSION into comparable ints; anything unparseable sorts lowest."""
    if not version:
        return ()
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def is_engine_tree(root: Path) -> bool:
    """Whether `root` looks like a `tableau-fabric-skills` checkout/plugin (has the engine skill)."""
    return (root / ENGINE_SKILL / "VERSION").is_file() or (root / ENGINE_SKILL / "scripts").is_dir()


def engine_root() -> Path:
    """The canonical engine root, or raise. There is no second candidate, on purpose."""
    # When simulating engine absent, default plugin root is treated as absent, but an explicitly injected
    # or monkeypatched non-default root in tests remains authoritative.
    simulated_absent = bool(os.environ.get(SIMULATE_ENGINE_ABSENT_ENV)) and (
        PLUGIN_ENGINE_ROOT == DEFAULT_PLUGIN_ENGINE_ROOT
    )
    if PLUGIN_ENGINE_ROOT != DEFAULT_PLUGIN_ENGINE_ROOT:
        if not simulated_absent and is_engine_tree(PLUGIN_ENGINE_ROOT):
            return PLUGIN_ENGINE_ROOT
        evidence = None
    elif not simulated_absent:
        evidence = discover_installed_plugin("engine")
        if evidence.ok and evidence.plugin_root:
            return evidence.plugin_root
    else:
        evidence = None
    detail = f" ({evidence.detail})" if evidence else ""
    raise EngineNotFoundError(
        f"the deterministic conversion engine is not installed at {PLUGIN_ENGINE_ROOT}{detail}. "
        f"{INSTALL_HINT}. This does NOT fall back to another copy: a second tree is what issue #107 "
        "is about, and a silent fallback is how two engine versions built one pipeline."
    )


def engine_skill_dir(root: Path | None = None) -> Path:
    """The `skills/tableau-migration` folder of the canonical engine (or of an explicit root)."""
    return (root or engine_root()) / ENGINE_SKILL


def engine_scripts_dir(root: Path | None = None) -> Path:
    """The engine's `scripts/` folder - `fetch_tds.py`, `estate_survey.py`, `migrate_estate.py`."""
    return engine_skill_dir(root) / "scripts"


def engine_version(root: Path | None = None) -> str | None:
    """The engine's `VERSION` string, or None when the tree has no VERSION file."""
    version_file = engine_skill_dir(root) / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def is_canonical(root: Path) -> bool:
    """Whether a path IS the canonical plugin root (resolved, so a symlink cannot disguise it)."""
    try:
        selected = PLUGIN_ENGINE_ROOT if PLUGIN_ENGINE_ROOT != DEFAULT_PLUGIN_ENGINE_ROOT else engine_root()
        return Path(root).resolve() == selected.resolve()
    except EngineNotFoundError:
        return False
    except OSError:  # pragma: no cover - resolve() on a broken mount
        return False


def alternative_engine_roots() -> list[Path]:
    """Every NON-canonical engine tree that currently exists. Any hit is a preflight failure."""
    found: list[Path] = []
    seen: set[str] = set()
    for candidate in ALTERNATIVE_ENGINE_ROOTS:
        if is_canonical(candidate) or not is_engine_tree(candidate):
            continue
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(candidate)
    return found


def resolve_engine(requested: Path | None = None, allow_noncanonical: bool = False) -> Path:
    """Resolve the engine root a caller should run, refusing a silent non-canonical substitution.

    `requested` exists for tests and for a deliberate, ACKNOWLEDGED override; it is never a fallback.
    Passing one without `allow_noncanonical` raises, because "it worked, on some engine" is precisely
    the failure this module was written to remove.
    """
    if requested is None:
        return engine_root()
    requested = Path(requested)
    if is_canonical(requested) or allow_noncanonical:
        return requested
    raise NonCanonicalEngineError(
        f"--engine {requested} is not the canonical engine plugin ({PLUGIN_ENGINE_ROOT}). "
        "The installed plugin is the single source of the conversion engine (issue #107). "
        "Pass --allow-noncanonical-engine to override deliberately; the bundle receipt will record "
        "the run as non-canonical."
    )


def engine_provenance(root: Path | None = None) -> dict[str, object]:
    """What built this bundle: path, version, and whether it was the canonical plugin.

    Written into `engine-output-receipt.json` so an artifact answers "what built me?" on its own,
    months later, without the machine that built it. Never raises: a bundle with an unresolvable
    engine must still record THAT, rather than losing the receipt entirely.
    """
    if root is None:
        try:
            root = engine_root()
        except EngineNotFoundError:
            return {
                "root": None,
                "version": None,
                "canonical": False,
                "source": "unresolved",
                "plugin_root": str(PLUGIN_ENGINE_ROOT),
            }
    root = Path(root)
    canonical = is_canonical(root)
    try:
        selected_root = engine_root()
    except EngineNotFoundError:
        selected_root = PLUGIN_ENGINE_ROOT
    return {
        "root": str(root),
        "version": engine_version(root),
        "canonical": canonical,
        "source": "plugin" if canonical else "override",
        "plugin_root": str(selected_root),
    }


def status() -> dict[str, object]:
    """The machine-readable single-source verdict `preflight.ps1` renders."""
    if PLUGIN_ENGINE_ROOT != DEFAULT_PLUGIN_ENGINE_ROOT:
        present = is_engine_tree(PLUGIN_ENGINE_ROOT)
        evidence = _evidence(
            "engine",
            "found" if present else "missing",
            host="cli",
            plugin_root=PLUGIN_ENGINE_ROOT if present else None,
            candidates=(PLUGIN_ENGINE_ROOT,) if present else (),
            detail=f"{'found' if present else 'not installed at'} {PLUGIN_ENGINE_ROOT}",
        )
    elif os.environ.get(SIMULATE_ENGINE_ABSENT_ENV):
        evidence = _evidence(
            "engine",
            "missing",
            host=None,
            detail=f"engine absence simulated by {SIMULATE_ENGINE_ABSENT_ENV}",
        )
        present = False
    else:
        evidence = discover_installed_plugin("engine")
        present = evidence.ok and evidence.plugin_root is not None
    selected_root = evidence.plugin_root or PLUGIN_ENGINE_ROOT
    alternatives = alternative_engine_roots()
    return {
        "root": str(selected_root),
        "present": present,
        "version": engine_version(selected_root) if present else None,
        "scripts": str(selected_root / ENGINE_SKILL / "scripts"),
        "alternatives": [str(path) for path in alternatives],
        "upstream_version_url": UPSTREAM_VERSION_URL,
        "install_hint": evidence.install_hint,
        "host": evidence.host,
        "detail": evidence.detail,
        "installation_status": evidence.status,
        "authoring_plugin": discover_installed_plugin("authoring").to_jsonable(),
        "ok": present and not alternatives,
    }


def main(argv: list[str] | None = None) -> int:
    """Print the single-source verdict. Exit 0 only when the plugin is the ONLY engine present."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="emit the verdict as JSON (preflight reads this)")
    args = parser.parse_args(argv)

    verdict = status()
    if args.json:
        print(json.dumps(verdict, indent=2))
        return 0 if verdict["ok"] else 1

    print(f"ENGINE: {'OK' if verdict['ok'] else 'PROBLEM'}")
    print(f"  canonical plugin : {verdict['root']}")
    print(f"  version          : {verdict['version'] or '(not installed)'}")
    for path in verdict["alternatives"]:
        print(f"  ALTERNATIVE COPY : {path}  <- delete it; the plugin is the single source (#107)")
    if not verdict["present"]:
        print(f"  -> {INSTALL_HINT}")
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
