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
2. name every OTHER engine tree it can see, so `preflight.ps1` can report it. Since #727 an unused
   checkout is an advisory diagnostic, not a mandatory deletion: (1) already guarantees it never runs.

Keep the candidate list in `ALTERNATIVE_ENGINE_ROOTS` honest. A tree that is not listed is a tree
preflight cannot warn about - and an unlisted tree is exactly how #107 happened.

Installed plugin hosts (#727)
-----------------------------
The engine, `powerbi-authoring` and this repo's skill bundles install through the Copilot CLI
(`COPILOT_HOME`) or VS Code agent plugins (`%APPDATA%/Code/User/agent-plugins/installed.json`).
`discover_installed_plugin` is the one resolver for all three. `T2P_PLUGIN_HOST=cli|vscode` selects a
host with no fallback; an explicit installed-plugins root is an override that skips host selection.
Unset, the CLI is used unless it holds no usable copy (so CLI-only and dual-host
machines behave as before #727), VS Code only then, and whatever VS Code holds beside a CLI copy is
returned as `host_advisory`. VS Code records cannot prove enablement.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from urllib.request import url2pathname

from build_plugin import MARKETPLACE_NAME, PLUGIN_NAME, PUBLISH_REPO, SHIPPED_SKILLS
from copilot_home import COPILOT_HOME_ENV, copilot_home

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

# Trees that should not exist, listed so preflight can report them (#727: a diagnostic, since the plugin
# is the only engine that runs). These are the real locations this project has actually resolved an
# engine from; add to it rather than trusting that nobody will clone the engine somewhere new.
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
BUNDLE_INSTALL_HINT = (
    f"Install it once between sessions: copilot plugin marketplace add "
    f"{PUBLISH_REPO.rsplit('github.com/', 1)[-1]} && copilot plugin install {PLUGIN_NAME}@{MARKETPLACE_NAME}"
)
PLUGIN_HOST_ENV = "T2P_PLUGIN_HOST"
VSCODE_INSTALL_HINT = "Install the required agent plugin in VS Code and restart the agent session."

PluginKind = Literal["engine", "authoring", "bundles"]
# kind -> (plugin, marketplace, Copilot CLI install hint)
_PLUGINS: dict[str, tuple[str, str, str]] = {
    "engine": ("tableau-fabric-skills", "tableau-collection", INSTALL_HINT),
    "authoring": (
        "powerbi-authoring",
        "fabric-collection",
        "Install or repair powerbi-authoring@fabric-collection between Copilot sessions.",
    ),
    "bundles": (PLUGIN_NAME, MARKETPLACE_NAME, BUNDLE_INSTALL_HINT),
}


@dataclass(frozen=True)
class InstalledPluginDiscovery:
    """Evidence for one installed plugin capability: found, missing, multiple or invalid."""

    kind: PluginKind
    status: str
    host: str | None
    detail: str
    candidates: tuple[Path, ...] = ()
    entries: tuple[dict[str, object], ...] = ()
    host_advisory: str = ""

    @property
    def ok(self) -> bool:
        """Whether exactly one usable installation was found."""
        return self.status == "found"

    @property
    def plugin_root(self) -> Path | None:
        """The selected installation, only when exactly one was found."""
        return self.candidates[0] if self.ok else None

    @property
    def identity(self) -> str:
        """``plugin@marketplace``; CLI skill bundles are named by the directory that carries them."""
        root = self.plugin_root
        if self.kind == "bundles" and self.host == "cli" and root:
            return f"{root.name}@{root.parent.name}"
        name, marketplace, _ = _PLUGINS[self.kind]
        return f"{name}@{marketplace}"

    @property
    def install_hint(self) -> str:
        """A repair in the host that was examined; never a CLI install for a VS Code refusal."""
        if self.host is None:
            return f"Set {PLUGIN_HOST_ENV} to cli or vscode, or unset it for automatic selection."
        if self.status == "multiple":
            return "Remove or disable duplicate installed plugins so only one copy can shadow the repo."
        return VSCODE_INSTALL_HINT if self.host == "vscode" else _PLUGINS[self.kind][2]

    def to_jsonable(self) -> dict[str, object]:
        """Return stable JSON evidence consumed by preflight."""
        return {
            "status": self.status,
            "ok": self.ok,
            "host": self.host,
            "detail": self.detail,
            "plugin_root": str(self.plugin_root) if self.plugin_root else None,
            "candidates": [str(path) for path in self.candidates],
            "identity": self.identity,
            "install_hint": self.install_hint,
            "entries": list(self.entries),
            "host_advisory": self.host_advisory,
        }


class EngineNotFoundError(RuntimeError):
    """The canonical engine plugin is not installed. Deliberately fatal - never fall back."""


class NonCanonicalEngineError(RuntimeError):
    """A caller asked for an engine that is not the canonical plugin."""


def _read_json(path: Path, *, leading_comments: bool = False) -> object:
    text = path.read_text(encoding="utf-8-sig")
    if leading_comments:
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    return json.loads(text)


def _carries(kind: PluginKind, root: Path) -> bool:
    """Whether `root` holds the files this toolkit runs from a `kind` plugin."""
    if kind == "engine":
        return is_engine_tree(root)
    skills = ("powerbi-report-cli",) if kind == "authoring" else SHIPPED_SKILLS
    return any((root / "skills" / skill / "SKILL.md").is_file() for skill in skills)


def _select(
    kind: PluginKind, host: str, roots: list[Path], missing: str, entries: tuple = ()
) -> InstalledPluginDiscovery:
    """One physical root is found; distinct roots are multiple (`_vscode_root` resolves aliases to one)."""
    found = tuple(dict.fromkeys(roots))
    if len(found) > 1:
        detail = "multiple installed plugins: " + "; ".join(map(str, found))
        return InstalledPluginDiscovery(kind, "multiple", host, detail, found, entries)
    if found:
        return InstalledPluginDiscovery(kind, "found", host, f"found {found[0]}", found, entries)
    return InstalledPluginDiscovery(kind, "missing", host, missing, (), entries)


def _cli(
    kind: PluginKind, installed_plugins_root: Path | None, environment: dict[str, str]
) -> InstalledPluginDiscovery:
    # copilot_home()'s rule, applied to the caller's environment rather than this process's.
    home = Path(environment.get(COPILOT_HOME_ENV) or Path.home() / ".copilot")
    installed = Path(installed_plugins_root) if installed_plugins_root is not None else home / "installed-plugins"
    if kind == "engine":
        root = installed / "tableau-collection" / "tableau-fabric-skills"
        return _select(kind, "cli", [root] if is_engine_tree(root) else [], f"not installed at {root}")
    if kind == "bundles":
        installed = installed.expanduser().resolve()
        roots = [path.parent for path in sorted(installed.glob("*/*/skills")) if _carries(kind, path.parent)]
        return _select(kind, "cli", roots, f"no installed plugin under {installed} carries any shipped skill bundle")
    # Authoring: locate registered caches only; preflight's Get-PluginVerdict owns the CLI verdict.
    config = home / "config.json"
    try:
        registry = _read_json(config, leading_comments=True)
    except (OSError, ValueError):
        registry = {}
    listed = registry.get("installedPlugins") if isinstance(registry, dict) else None
    name, marketplace, _ = _PLUGINS[kind]
    entries = tuple(
        entry
        for entry in (listed if isinstance(listed, list) else [])
        if isinstance(entry, dict) and entry.get("name") == name and entry.get("marketplace") == marketplace
    )
    roots = [Path(path) for entry in entries if isinstance(path := entry.get("cache_path"), str) and path]
    missing = f"no {name}@{marketplace} entry with installed skills in {config}"
    return _select(kind, "cli", [root for root in roots if _carries(kind, root)], missing, entries)


def _vscode_root(entry: object, cache: Path) -> Path | None:
    """The cached directory a VS Code registry entry names, or None unless it is a local path in `cache`."""
    if not isinstance(entry, dict) or not isinstance(entry.get("pluginUri"), str):
        return None
    relative = entry.get("relativePath")
    try:
        if isinstance(relative, str):
            path = cache / relative
        else:
            uri = urlparse(entry["pluginUri"])
            if uri.scheme != "file":
                return None
            path = Path(url2pathname(uri.path))
        path, cache = path.resolve(), cache.resolve()
    except (OSError, ValueError):
        return None
    return path if path.is_relative_to(cache) else None


def _vscode(kind: PluginKind, environment: dict[str, str]) -> InstalledPluginDiscovery:
    if not environment.get("APPDATA"):
        return InstalledPluginDiscovery(kind, "missing", "vscode", "APPDATA is not configured")
    cache = Path(environment["APPDATA"]) / "Code" / "User" / "agent-plugins"
    registry = cache / "installed.json"
    if not registry.is_file():
        return InstalledPluginDiscovery(kind, "missing", "vscode", f"registry is absent: {registry}")
    try:
        value = _read_json(registry)
    except (OSError, ValueError) as exc:
        return InstalledPluginDiscovery(kind, "invalid", "vscode", f"registry is unreadable: {registry} ({exc})")
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("installed"), list):
        return InstalledPluginDiscovery(kind, "invalid", "vscode", f"registry schema is not version 1: {registry}")
    located = [_vscode_root(entry, cache) for entry in value["installed"]]
    return _select(
        kind,
        "vscode",
        [root for root in located if root is not None and _carries(kind, root)],
        f"no entry in {registry} carries the {kind} plugin",
    )


def discover_installed_plugin(
    kind: PluginKind,
    installed_plugins_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> InstalledPluginDiscovery:
    """Select one installed `kind` plugin across the Copilot CLI and VS Code hosts (module docstring)."""
    environment = dict(os.environ) if env is None else dict(env)
    if installed_plugins_root is not None:
        # A deliberate override, like POWERBI_SKILLS_PLUGIN_ROOT: it names one CLI layout, so no host is chosen.
        return _cli(kind, installed_plugins_root, environment)
    selector = environment.get(PLUGIN_HOST_ENV)
    if PLUGIN_HOST_ENV in environment and selector not in ("cli", "vscode"):
        return InstalledPluginDiscovery(kind, "invalid", None, f"invalid {PLUGIN_HOST_ENV}: {selector!r}")
    if selector:
        return _vscode(kind, environment) if selector == "vscode" else _cli(kind, None, environment)
    cli, vscode = _cli(kind, None, environment), _vscode(kind, environment)
    if "missing" in (cli.status, vscode.status):
        return vscode if cli.status == "missing" and vscode.status != "missing" else cli
    advisory = (
        f"{kind}: using cli ({cli.status}); vscode is {vscode.status}: {vscode.detail}. "
        f"Set {PLUGIN_HOST_ENV}=cli or {PLUGIN_HOST_ENV}=vscode to choose explicitly."
    )
    return replace(cli, host_advisory=advisory)


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


def _engine_evidence() -> InstalledPluginDiscovery:
    """The engine installation; an injected `PLUGIN_ENGINE_ROOT` stays authoritative, as before #727."""
    if PLUGIN_ENGINE_ROOT != DEFAULT_PLUGIN_ENGINE_ROOT:
        root = PLUGIN_ENGINE_ROOT
        return _select("engine", "cli", [root] if is_engine_tree(root) else [], f"not installed at {root}")
    if os.environ.get(SIMULATE_ENGINE_ABSENT_ENV):
        return InstalledPluginDiscovery("engine", "missing", "cli", f"not installed at {PLUGIN_ENGINE_ROOT}")
    return discover_installed_plugin("engine")


def _selected_engine_root() -> Path:
    return _engine_evidence().plugin_root or PLUGIN_ENGINE_ROOT


def engine_root() -> Path:
    """The canonical engine root, or raise. There is no second candidate, on purpose."""
    # When simulating engine absent, default plugin root is treated as absent, but an explicitly injected
    # or monkeypatched non-default root in tests remains authoritative.
    evidence = _engine_evidence()
    if evidence.plugin_root:
        return evidence.plugin_root
    raise EngineNotFoundError(
        f"the deterministic conversion engine is not installed at {PLUGIN_ENGINE_ROOT} ({evidence.detail}). "
        f"{evidence.install_hint}. This does NOT fall back to another copy: a second tree is what issue #107 "
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
        return Path(root).resolve() == _selected_engine_root().resolve()
    except OSError:  # pragma: no cover - resolve() on a broken mount
        return False


def alternative_engine_roots() -> list[Path]:
    """Every NON-canonical engine tree that currently exists, reported by preflight as a diagnostic."""
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
    return {
        "root": str(root),
        "version": engine_version(root),
        "canonical": canonical,
        "source": "plugin" if canonical else "override",
        "plugin_root": str(_selected_engine_root()),
    }


def status() -> dict[str, object]:
    """The machine-readable single-source verdict `preflight.ps1` renders."""
    evidence = _engine_evidence()
    root = evidence.plugin_root or PLUGIN_ENGINE_ROOT
    return {
        "root": str(root),
        "present": evidence.ok,
        "version": engine_version(root) if evidence.ok else None,
        "scripts": str(root / ENGINE_SKILL / "scripts"),
        "alternatives": [str(path) for path in alternative_engine_roots()],
        "upstream_version_url": UPSTREAM_VERSION_URL,
        "install_hint": evidence.install_hint,
        "host": evidence.host,
        "detail": evidence.detail,
        "host_advisory": evidence.host_advisory,
        "authoring_plugin": discover_installed_plugin("authoring").to_jsonable(),
        # Alternative trees are diagnostics (#727): the installed plugin is the only engine that can run.
        "ok": evidence.ok,
    }


def main(argv: list[str] | None = None) -> int:
    """Print the single-source verdict. Exit 0 when the installed engine plugin resolves."""
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
        print(f"  ALTERNATIVE COPY : {path}  (diagnostic only; the installed plugin is the engine, #107)")
    if verdict["host_advisory"]:
        print(f"  HOST ADVISORY    : {verdict['host_advisory']}")
    if not verdict["present"]:
        print(f"  -> {verdict['install_hint']}")
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
