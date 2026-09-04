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

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Test simulation hook: when set, engine resolution acts as if the plugin is not installed.
SIMULATE_ENGINE_ABSENT_ENV = "T2P_SIMULATE_ENGINE_ABSENT_FOR_TESTS"

# The one canonical location: the installed Copilot plugin `tableau-fabric-skills@tableau-collection`.
DEFAULT_PLUGIN_ENGINE_ROOT = (
    Path.home() / ".copilot" / "installed-plugins" / "tableau-collection" / "tableau-fabric-skills"
)
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


class EngineNotFoundError(RuntimeError):
    """The canonical engine plugin is not installed. Deliberately fatal - never fall back."""


class NonCanonicalEngineError(RuntimeError):
    """A caller asked for an engine that is not the canonical plugin."""


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
    if not simulated_absent and is_engine_tree(PLUGIN_ENGINE_ROOT):
        return PLUGIN_ENGINE_ROOT
    raise EngineNotFoundError(
        f"the deterministic conversion engine is not installed at {PLUGIN_ENGINE_ROOT}. "
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
        return Path(root).resolve() == PLUGIN_ENGINE_ROOT.resolve()
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
    return {
        "root": str(root),
        "version": engine_version(root),
        "canonical": canonical,
        "source": "plugin" if canonical else "override",
        "plugin_root": str(PLUGIN_ENGINE_ROOT),
    }


def status() -> dict[str, object]:
    """The machine-readable single-source verdict `preflight.ps1` renders."""
    present = is_engine_tree(PLUGIN_ENGINE_ROOT)
    alternatives = alternative_engine_roots()
    return {
        "root": str(PLUGIN_ENGINE_ROOT),
        "present": present,
        "version": engine_version(PLUGIN_ENGINE_ROOT) if present else None,
        "scripts": str(PLUGIN_ENGINE_ROOT / ENGINE_SKILL / "scripts"),
        "alternatives": [str(path) for path in alternatives],
        "upstream_version_url": UPSTREAM_VERSION_URL,
        "install_hint": INSTALL_HINT,
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
