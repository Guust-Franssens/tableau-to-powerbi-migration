"""
purpose: discover the installed Copilot plugin that carries this repo's reusable Power BI skills.
usage:   python scripts/skill_plugin_source.py [--json] [--plugin-root PATH]
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from build_plugin import SHIPPED_SKILLS
from copilot_home import copilot_home
from engine_source import BUNDLE_INSTALL_HINT as DEFAULT_INSTALL_HINT
from engine_source import discover_installed_plugin

PLUGIN_ROOT_ENV = "POWERBI_SKILLS_PLUGIN_ROOT"


@dataclass(frozen=True)
class SkillPluginDiscovery:  # pylint: disable=too-many-instance-attributes
    """Discovery verdict for the installed skill plugin."""

    status: str
    plugin_root: Path | None
    skills_dir: Path | None
    candidates: tuple[Path, ...]
    identity: str
    install_hint: str
    detail: str
    host: str | None
    override: bool = False
    host_advisory: str = ""

    @property
    def ok(self) -> bool:
        """Whether discovery identified exactly one usable plugin root."""
        return self.status == "found"

    def to_jsonable(self) -> dict[str, object]:
        """Return a JSON-serialisable representation for PowerShell preflight."""
        return {
            "status": self.status,
            "ok": self.ok,
            "plugin_root": str(self.plugin_root) if self.plugin_root else None,
            "skills_dir": str(self.skills_dir) if self.skills_dir else None,
            "candidates": [str(path) for path in self.candidates],
            "identity": self.identity,
            "install_hint": self.install_hint,
            "detail": self.detail,
            "host": self.host,
            "override": self.override,
            "host_advisory": self.host_advisory,
            "shipped_skills": list(SHIPPED_SKILLS),
        }


def _identity_from_root(plugin_root: Path) -> str:
    """Infer ``plugin@marketplace`` from the installed-plugins two-level layout."""
    plugin = plugin_root.name
    marketplace = plugin_root.parent.name
    if marketplace == "installed-plugins":
        return plugin
    return f"{plugin}@{marketplace}"


def _normalise_plugin_root(path: Path) -> Path:
    """Accept a plugin root, or a direct path to its ``skills`` directory for operator convenience."""
    resolved = path.expanduser().resolve()
    if resolved.name == "skills":
        return resolved.parent
    return resolved


def _skills_dir(plugin_root: Path) -> Path:
    return plugin_root / "skills"


def discover_skill_plugin(
    installed_plugins_root: Path | None = None,
    plugin_root_override: Path | None = None,
    env: dict[str, str] | None = None,
) -> SkillPluginDiscovery:
    """Find the installed plugin root that carries this repo's shipped skill bundles.

    Discovery is by content, not by marketplace name: the plugin was previously published under a
    different name, and hard-coding that identity is exactly how stale bundles went unnoticed.
    """
    environment = os.environ if env is None else env
    override_value = plugin_root_override or (
        Path(environment[PLUGIN_ROOT_ENV]) if environment.get(PLUGIN_ROOT_ENV) else None
    )
    if override_value:
        plugin_root = _normalise_plugin_root(override_value)
        skills_dir = _skills_dir(plugin_root)
        if not skills_dir.is_dir():
            return SkillPluginDiscovery(
                status="missing",
                plugin_root=plugin_root,
                skills_dir=skills_dir,
                candidates=(),
                identity=_identity_from_root(plugin_root),
                install_hint=f"{PLUGIN_ROOT_ENV} points at {plugin_root}, but {skills_dir} does not exist.",
                detail=f"override has no skills directory: {skills_dir}",
                host=None,
                override=True,
            )
        return SkillPluginDiscovery(
            status="found",
            plugin_root=plugin_root,
            skills_dir=skills_dir,
            candidates=(plugin_root,),
            identity=_identity_from_root(plugin_root),
            install_hint=DEFAULT_INSTALL_HINT,
            detail=f"override: {plugin_root}",
            host=None,
            override=True,
        )

    resolver_env = dict(environment)
    if env is None and installed_plugins_root is None:
        resolver_env["COPILOT_HOME"] = str(copilot_home())
    evidence = discover_installed_plugin("bundles", installed_plugins_root, resolver_env)
    plugin_root = evidence.plugin_root
    return SkillPluginDiscovery(
        status=evidence.status,
        plugin_root=plugin_root,
        skills_dir=_skills_dir(plugin_root) if plugin_root else None,
        candidates=evidence.candidates,
        identity=evidence.identity,
        install_hint=evidence.install_hint,
        detail=evidence.detail,
        host=evidence.host,
        host_advisory=evidence.host_advisory,
    )


def main(argv: list[str] | None = None) -> int:
    """Print the discovered plugin root as JSON or human-readable text."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--installed-plugins-root",
        type=Path,
        help="override installed-plugins under the Copilot home (COPILOT_HOME or ~/.copilot)",
    )
    parser.add_argument("--plugin-root", type=Path, help=f"explicit plugin root; also supported via {PLUGIN_ROOT_ENV}")
    parser.add_argument("--json", action="store_true", help="emit a JSON verdict for scripts/preflight.ps1")
    args = parser.parse_args(argv)

    verdict = discover_skill_plugin(args.installed_plugins_root, args.plugin_root)
    if args.json:
        print(json.dumps(verdict.to_jsonable(), indent=2))
    elif verdict.ok:
        print(f"FOUND: {verdict.plugin_root} ({verdict.identity})")
        if verdict.host_advisory:
            print(f"HOST ADVISORY: {verdict.host_advisory}")
    else:
        print(f"{verdict.status.upper()}: {verdict.detail}")
        print(verdict.install_hint)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
