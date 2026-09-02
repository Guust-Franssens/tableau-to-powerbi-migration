"""
purpose: discover the installed Copilot plugin that carries this repo's reusable Power BI skills,
         by PROVEN ownership - never by guessing from the bundles a directory happens to contain.
usage:   python scripts/skill_plugin_source.py [--json] [--plugin-root PATH]

Why ownership is proved rather than inferred (issue #410, round-2 finding 1)
---------------------------------------------------------------------------
This module used to select "any installed plugin carrying any bundle from the inventory", and
`sync_installed_skills.py` then WROTE into whatever it selected. Content is exactly the wrong
evidence for that decision, because content is what a feature branch - or anyone who can drop a
directory into `~/.copilot/installed-plugins` - controls. Measured, in throw-away plugin roots:

    plain sync, a foreign plugin carrying ONE current bundle name
        origin/master  -> exit 0, its SKILL.md overwritten, its private.txt DELETED
        this branch    -> exit 0, its SKILL.md overwritten (the deletion was already bounded)
    --from-worktree, a bundle added only on the branch, our own plugin half-installed
        this branch    -> exit 0, the STRANGER selected, its SKILL.md overwritten,
                          its private.txt DELETED, our bundles written into it

So a destination must carry one of three PROOFS, none of which is a bundle name:

  ``explicit``   the operator named it (``--plugin-root`` / ``POWERBI_SKILLS_PLUGIN_ROOT``);
  ``marker``     it holds `.skill-sync-owner.json` naming this repo's publish URL - written by a
                 previous successful publish, so it is this tool's own provenance record;
  ``identity``   the Copilot CLI's own install record (`~/.copilot/config.json` `installedPlugins`,
                 which is **JSONC** - see `_load_jsonc` - falling back to the `<marketplace>/<plugin>`
                 directory layout the CLI creates) names it as one of
                 `build_plugin.KNOWN_PLUGIN_IDENTITIES`.

With no proof, this returns ``unproven`` and the caller must write NOTHING. Content is still read,
but only to tell ``unproven`` (something LOOKS like ours - say so, and name it) from ``missing``
(nothing resembling it is installed). Content can therefore only ever cause a REFUSAL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath

from build_plugin import KNOWN_PLUGIN_IDENTITIES, MARKETPLACE_NAME, PLUGIN_NAME, PUBLISH_REPO, SHIPPED_SKILLS

PLUGIN_ROOT_ENV = "POWERBI_SKILLS_PLUGIN_ROOT"
DEFAULT_IDENTITY = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
DEFAULT_INSTALL_HINT = (
    f"Install it once between sessions: copilot plugin marketplace add "
    f"{PUBLISH_REPO.rsplit('github.com/', 1)[-1]} && copilot plugin install {DEFAULT_IDENTITY}"
)

# The provenance record this tool writes into the plugin it publishes to. A dotfile at the plugin
# ROOT, deliberately: `skills/` is scanned for bundles, and the plugin root is where the CLI's own
# `.mcp.json` / `plugin.json` already live, so nothing reads this as bundle content.
OWNER_MARKER_NAME = ".skill-sync-owner.json"
OWNER_MARKER_SCHEMA = 1

# Characters no bundle DIRECTORY may contain. Windows forbids them outright, and `:` is the one that
# bites: NTFS reads `foreign::$DATA` as alternate-data-stream syntax, so it is a real path to a real
# directory whose mere `exists()` raises PermissionError. Deliberately stricter than POSIX allows -
# this record is copied between machines that must all read it the same way, and no bundle this repo
# ships could ever want one.
_RESERVED_CHARS = frozenset('<>:"|?*') | frozenset(chr(code) for code in range(32))

# The Copilot CLI writes its own config as JSONC. Measured on a real machine, `~/.copilot/config.json`
# OPENS with `// User settings belong in settings.json`, so `json.loads` raises on the first
# character and the `identity`-by-REGISTRY proof - the one that is supposed to survive a rename the
# directory layout cannot predict - silently never fires. It failed closed (fewer proofs, never more
# writes), which is exactly why nothing noticed.
_LINE_COMMENT = re.compile(r"^[ \t]*//.*$", re.MULTILINE)


def _load_jsonc(text: str) -> object | None:
    """Parse JSON, then JSON with whole-line `//` comments; None when neither parses.

    Strict first, so a plain JSON file is never rewritten before being read. Only lines whose first
    non-whitespace characters are `//` are dropped, and in valid JSON such a line can only be a
    comment: a string cannot span lines, so no `"https://..."` value can begin one.
    """
    for candidate in (text, _LINE_COMMENT.sub("", text)):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


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
    override: bool = False
    proof: str | None = None

    @property
    def ok(self) -> bool:
        """Whether discovery PROVED exactly one usable plugin root."""
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
            "override": self.override,
            "proof": self.proof,
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


def _carries_shipped_skill(skills_dir: Path, bundles: Sequence[str]) -> bool:
    return any((skills_dir / skill / "SKILL.md").is_file() for skill in bundles)


def marker_path(plugin_root: Path) -> Path:
    """Where this tool records that it owns `plugin_root`."""
    return plugin_root / OWNER_MARKER_NAME


def read_owner_marker(plugin_root: Path | None) -> dict | None:
    """Return the ownership marker, or None when it is absent or unreadable.

    Unreadable degrades to absent on purpose: a corrupt marker must mean "prove ownership another
    way", never an exception in the middle of a publish.
    """
    if plugin_root is None:
        return None
    try:
        loaded = json.loads(marker_path(plugin_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def marker_bundle_problems(names: object, skills_dir: Path) -> list[str]:
    """Why a marker's recorded inventory cannot be acted on, or `[]` when every entry is safe.

    The marker is a DATA FILE, and `sync_installed_skills._apply` deletes what its `bundles` array
    names. In `pathlib`, ``Path("/abs") / x`` and ``installed / ".."`` both ESCAPE by construction,
    so an absolute path, a `..`, a separator or an empty string in that array turns a sync into an
    arbitrary recursive delete that still exits 0 - strictly worse than the in-plugin deletion this
    whole fix exists to stop. Measured through public `sync.main` in review: a valid-looking marker
    naming an absolute path deleted an unrelated directory and reported success.

    ⚠️ Structure and containment are NOT enough, because the name that reaches the delete is not
    always the name that was checked. Measured on Windows, with only `foreign/` on disk:

        entry        exists  (skills/entry).resolve().name   direct child of skills/
        'FOREIGN'    True    'foreign'                       yes
        'foreign.'   True    'foreign'                       yes
        'foreign '   True    'foreign'                       yes

    Windows compares case-insensitively and strips trailing dots and spaces, so all three are
    genuine direct children - containment holds - and each would have deleted a bundle the marker
    does not own. Note `resolve()` normalises only paths that EXIST ('GONE.' and 'gone ' come back
    unchanged when nothing is there), so resolution alone cannot be the test.

    So an entry that names something on disk must equal the REAL directory entry exactly, compared
    against the actual listing rather than against anything derived from the string itself; and the
    caller deletes the RESOLVED path (`marker_bundle_target`), never `skills_dir / raw`.

    The caller must refuse the WHOLE marker on any problem: a marker that lies about one name is
    not evidence for the others.
    """
    if not isinstance(names, list):
        return [f"`bundles` must be a list, not {type(names).__name__}"]
    problems: list[str] = []
    try:
        parent = skills_dir.resolve()
        actual = {child.name for child in skills_dir.iterdir()} if skills_dir.is_dir() else set()
    except (OSError, ValueError) as exc:
        return [f"{skills_dir} could not be listed, so no recorded name can be checked against it: {exc}"]
    for entry in names:
        if not isinstance(entry, str) or not entry.strip():
            problems.append(f"{entry!r} is not a non-empty string")
            continue
        component = PurePath(entry)
        if len(component.parts) != 1 or component.anchor or entry in (".", "..") or "/" in entry or "\\" in entry:
            problems.append(f"{entry!r} is not a single path component")
            continue
        if entry != entry.strip() or entry.endswith("."):
            problems.append(f"{entry!r} has leading/trailing whitespace or a trailing dot, which Windows strips")
            continue
        if set(entry) & _RESERVED_CHARS:
            problems.append(
                f"{entry!r} contains a character no bundle directory may hold; NTFS reads `name::$DATA` as "
                "stream syntax, and merely asking whether it EXISTS raises PermissionError"
            )
            continue
        try:
            resolved = (skills_dir / entry).resolve()
            names_something = (skills_dir / entry).exists()
        except (OSError, ValueError) as exc:
            # Both the resolution AND the existence probe are inside the guard. The probe used to
            # sit outside it, so `foreign::$DATA` raised PermissionError past `UnsafeMarkerError`
            # and the CLI exited 1 with a traceback and NO json verdict - preflight then saw
            # "did not report" rather than the promised refusal (round-5 finding 2). It failed
            # closed, but "cannot assess" must still arrive as an ANSWER.
            problems.append(f"{entry!r} could not be inspected under {skills_dir}: {exc}")
            continue
        if resolved.parent != parent:
            problems.append(f"{entry!r} resolves outside {skills_dir} (to {resolved})")
            continue
        if names_something and entry not in actual:
            problems.append(
                f"{entry!r} is not the on-disk name of what it points at ({resolved.name!r}); a record that "
                "does not spell the directory it claims to have installed is not describing that directory"
            )
    return problems


def marker_bundle_target(skills_dir: Path, name: str) -> Path:
    """The one path a recorded bundle name may name: RESOLVED and re-validated, never `dir / raw`.

    Deleting `skills_dir / name` would delete whatever the OS decides that string means, which is
    the alias hole above. Callers delete THIS path, and re-validating here rather than trusting the
    planning-time check means a marker that became unsafe in between cannot slip through.
    """
    problems = marker_bundle_problems([name], skills_dir)
    if problems:
        raise ValueError("; ".join(problems))
    return (skills_dir / name).resolve()


def write_owner_marker(plugin_root: Path, **fields: object) -> Path:
    """Record this tool's ownership of `plugin_root`, and the inventory it just published.

    The inventory is the other half of the record, and it is what makes a RETIRED bundle visible:
    extra-file detection scoped to the CURRENT inventory can never see a bundle that has left it, so
    a retired bundle stayed installed forever while `--check` reported `in_sync` (#410 round-2
    finding 3). What this file remembers is precisely "what we put there last time".
    """
    target = marker_path(plugin_root)
    payload = {
        "schema": OWNER_MARKER_SCHEMA,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **fields,
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def registry_identities(registry: Path) -> dict[Path, str]:
    """`cache_path` -> ``plugin@marketplace`` from the Copilot CLI's own install record."""
    try:
        loaded = _load_jsonc(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    found: dict[Path, str] = {}
    for entry in loaded.get("installedPlugins") or []:
        cache_path, name = entry.get("cache_path"), entry.get("name")
        if not cache_path or not name:
            continue
        marketplace = entry.get("marketplace") or ""
        try:
            resolved = Path(cache_path).expanduser().resolve()
        except (OSError, ValueError):  # pragma: no cover - only on a malformed path string
            continue
        found[resolved] = f"{name}@{marketplace}" if marketplace else name
    return found


def marker_is_usable(marker: dict | None, *, publish_repo: str, skills_dir: Path) -> bool:
    """Whether this record may be ACTED ON at all - as ownership proof, or as an inventory.

    ONE predicate for both consumers, deliberately. They had drifted: `_recorded_inventory` refused
    to interpret an unknown `schema`, while `prove_ownership` accepted the very same file as proof
    of the DESTINATION because its `publish_repo` matched. So an uninterpretable record still chose
    which plugin got written to. Measured through public `sync.main` with a `schema: 99` marker
    planted in an arbitrarily named foreign plugin: selected with proof `marker`, its `SKILL.md`
    overwritten, its `private.txt` DELETED, the marker rewritten as schema 1, exit 0.

    The lesson is structural, not local: a data file with two consumers needs one notion of
    "usable", or fixing the consumer under review leaves the other one holding the door open.

    Usable means all of: a JSON object; a `schema` this build knows; a `publish_repo` naming this
    repo; and a `bundles` inventory that is a list of names safe to act on. An unusable record is
    not evidence of anything - ownership must then come from `explicit`, the CLI registry, or a
    known `<marketplace>/<plugin>` layout, none of which a dropped file can forge.
    """
    if not isinstance(marker, dict):
        return False
    if marker.get("schema") != OWNER_MARKER_SCHEMA:
        return False
    if not publish_repo or marker.get("publish_repo") != publish_repo:
        return False
    names = marker.get("bundles")
    return isinstance(names, list) and not marker_bundle_problems(names, skills_dir)


def prove_ownership(
    plugin_root: Path,
    *,
    publish_repo: str,
    identities: Sequence[str],
    registry_map: dict[Path, str],
) -> str | None:
    """Return the PROOF kind for `plugin_root`, or None when ownership cannot be established."""
    marker = read_owner_marker(plugin_root)
    if marker_is_usable(marker, publish_repo=publish_repo, skills_dir=_skills_dir(plugin_root)):
        return "marker"
    allowed = set(identities)
    registered = registry_map.get(plugin_root)
    if registered is not None:
        return "identity" if registered in allowed else None
    # No CLI registry entry for this directory: the two-level `<marketplace>/<plugin>` layout is
    # still the CLI's own naming rather than bundle content, so it is provenance of the same kind -
    # just weaker evidence, and it keeps machines whose config.json predates the plugin working.
    if _identity_from_root(plugin_root) in allowed:
        return "identity"
    return None


def _override_verdict(override_value: Path) -> SkillPluginDiscovery:
    """The operator named the destination outright; that assertion IS the proof."""
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
        override=True,
        proof="explicit",
    )


def _scan(root: Path, inventory: Sequence[str], prove) -> tuple[list[tuple[Path, str]], list[Path]]:
    """Split the installed plugins into (proven, look-alikes) - never into "probably ours"."""
    proven: list[tuple[Path, str]] = []
    lookalikes: list[Path] = []
    if not root.is_dir():
        return proven, lookalikes
    for skills_dir in sorted(root.glob("*/*/skills")):
        if not skills_dir.is_dir():
            continue
        plugin_root = skills_dir.parent
        proof = prove(plugin_root)
        if proof:
            proven.append((plugin_root, proof))
        elif _carries_shipped_skill(skills_dir, inventory):
            lookalikes.append(plugin_root)
    return proven, lookalikes


def _unproven_verdict(lookalikes: Sequence[Path]) -> SkillPluginDiscovery:
    listed = "; ".join(str(path) for path in lookalikes)
    return SkillPluginDiscovery(
        status="unproven",
        plugin_root=None,
        skills_dir=None,
        candidates=tuple(lookalikes),
        identity=DEFAULT_IDENTITY,
        install_hint=(
            "Ownership could not be proved, so NOTHING was written. If one of these is this repo's "
            f"plugin, name it once with --plugin-root <path>; the publish then records "
            f"{OWNER_MARKER_NAME} in it and no further naming is needed. Candidates: {listed}"
        ),
        detail=f"carries these bundles but ownership is UNPROVEN: {listed}",
    )


def _override_target(plugin_root_override: Path | None, env: dict[str, str] | None) -> Path | None:
    """The operator's explicit destination, from the flag or the environment variable."""
    environment = os.environ if env is None else env
    if plugin_root_override:
        return plugin_root_override
    return Path(environment[PLUGIN_ROOT_ENV]) if environment.get(PLUGIN_ROOT_ENV) else None


def _found_verdict(plugin_root: Path, proof: str) -> SkillPluginDiscovery:
    return SkillPluginDiscovery(
        status="found",
        plugin_root=plugin_root,
        skills_dir=_skills_dir(plugin_root),
        candidates=(plugin_root,),
        identity=_identity_from_root(plugin_root),
        install_hint=DEFAULT_INSTALL_HINT,
        detail=f"found {plugin_root} (ownership proved by {proof})",
        proof=proof,
    )


def _multiple_verdict(roots: Sequence[Path]) -> SkillPluginDiscovery:
    return SkillPluginDiscovery(
        status="multiple",
        plugin_root=None,
        skills_dir=None,
        candidates=tuple(roots),
        identity=DEFAULT_IDENTITY,
        install_hint="Remove or disable duplicate installed skill plugins so only one copy can shadow the repo.",
        detail="MULTIPLE skill plugin installs: " + "; ".join(str(path) for path in roots),
    )


def discover_skill_plugin(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    installed_plugins_root: Path | None = None,
    plugin_root_override: Path | None = None,
    env: dict[str, str] | None = None,
    bundles: Sequence[str] | None = None,
    identities: Sequence[str] | None = None,
    publish_repo: str | None = None,
    registry: Path | None = None,
) -> SkillPluginDiscovery:
    """Find the installed plugin root this repo can PROVE it owns.

    ``identities`` and ``publish_repo`` are the ownership evidence, and callers that know the
    AUTHORITATIVE ones - `sync_installed_skills.py` reads them from the merged commit - must pass
    them, because the module-level defaults come from the CURRENT WORKING TREE. ``bundles`` is no
    longer a selector at all: it only separates ``unproven`` from ``missing``.
    """
    allowed = tuple(identities) if identities is not None else KNOWN_PLUGIN_IDENTITIES
    repo_url = publish_repo if publish_repo is not None else PUBLISH_REPO
    override_value = _override_target(plugin_root_override, env)
    if override_value:
        return _override_verdict(override_value)

    root = (installed_plugins_root or (Path.home() / ".copilot" / "installed-plugins")).expanduser().resolve()
    registry_map = registry_identities(registry if registry is not None else root.parent / "config.json")
    proven, lookalikes = _scan(
        root,
        tuple(bundles) if bundles is not None else SHIPPED_SKILLS,
        lambda plugin_root: prove_ownership(
            plugin_root, publish_repo=repo_url, identities=allowed, registry_map=registry_map
        ),
    )

    if len(proven) == 1:
        return _found_verdict(*proven[0])
    if len(proven) > 1:
        return _multiple_verdict([path for path, _ in proven])
    if lookalikes:
        return _unproven_verdict(lookalikes)
    return SkillPluginDiscovery(
        status="missing",
        plugin_root=None,
        skills_dir=None,
        candidates=(),
        identity=DEFAULT_IDENTITY,
        install_hint=DEFAULT_INSTALL_HINT,
        detail=f"no installed plugin under {root} is owned by {repo_url}",
    )


def main(argv: list[str] | None = None) -> int:
    """Print the discovered plugin root as JSON or human-readable text."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-plugins-root", type=Path, help="override ~/.copilot/installed-plugins")
    parser.add_argument("--plugin-root", type=Path, help=f"explicit plugin root; also supported via {PLUGIN_ROOT_ENV}")
    parser.add_argument("--json", action="store_true", help="emit a JSON verdict for scripts/preflight.ps1")
    args = parser.parse_args(argv)

    verdict = discover_skill_plugin(args.installed_plugins_root, args.plugin_root)
    if args.json:
        print(json.dumps(verdict.to_jsonable(), indent=2))
    elif verdict.ok:
        print(f"FOUND: {verdict.plugin_root} ({verdict.identity}, proof: {verdict.proof})")
    else:
        print(f"{verdict.status.upper()}: {verdict.detail}")
        print(verdict.install_hint)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
