"""
purpose: bring the INSTALLED deterministic-engine plugin's `skills/tableau-migration` up to date from
         a checkout, in place, without the `copilot plugin update` that a running session blocks.
usage:   python scripts/sync_engine_plugin.py --source <tableau-fabric-skills checkout> [--check]
                                              [--allow-downgrade] [--verbose]

Why this exists
---------------
The installed plugin is the SINGLE canonical conversion engine (issue #107). That decision only holds
if the plugin can actually be kept current - and the obvious way to do that, `copilot plugin update`,
fails with `Access is denied. (os error 5)` while any Copilot CLI session is running.

That lock is narrower than the error implies (measured 2026-08-01, and re-confirmed here): it blocks
RENAMING the plugin directory, which is how `plugin update` swaps a plugin wholesale. Files inside
stay freely writable. `sync_installed_skills.py` already exploits that for this repo's own bundles;
this is the same move for the engine.

Scope, deliberately narrow:
  * CONTENT of `skills/tableau-migration` only - that is the engine. The plugin's own manifest,
    version metadata and the three sibling skills are left alone, because changing those is exactly
    what needs a real `plugin update` between sessions.
  * It REFUSES a downgrade unless `--allow-downgrade`. Measured 2026-08-12: the plugin held 2.113.0
    while a sibling clone held 2.126.0, and 2.113.0 emits deprecated Bing map visuals and silently
    drops a density-map worksheet. Quietly walking the canonical engine backwards would turn a
    cleanup into a regression.

The preferred path is still `copilot plugin update tableau-fabric-skills@tableau-collection` between
sessions - it also refreshes the manifest and the marketplace bookkeeping. This is the mid-session
escape hatch, and the way to pin the engine to a KNOWN-GOOD checkout rather than whatever upstream
happens to be today.

Getting a source checkout without leaving a second engine tree behind (preflight blocks on one):

    git clone --depth 1 https://github.com/Yarbrdab000/tableau-fabric-skills <scratch>
    python scripts/sync_engine_plugin.py --source <scratch>
    Remove-Item -Recurse -Force <scratch>
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# pylint: disable-next=wrong-import-position
from engine_source import ENGINE_SKILL, PLUGIN_ENGINE_ROOT, engine_version, is_engine_tree, version_tuple  # noqa: E402


def _is_noise(rel: Path) -> bool:
    """Build/tooling debris that is not part of the engine and must never be synced.

    Bytecode especially: copying a `.pyc` compiled elsewhere into the plugin ships an artifact whose
    provenance nobody can read. Stale ones in the destination are purged instead - Python's
    mtime+size check already invalidates them once the source lands, and deleting is unambiguous.
    """
    parts = set(rel.parts)
    return bool(parts & {"__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".venv"}) or rel.suffix in {
        ".pyc",
        ".pyo",
    }


def diff_tree(src: Path, dst: Path) -> tuple[list[Path], list[Path]]:
    """Return (files needing copy, files present in dst but not src), as paths relative to src."""
    changed: list[Path] = []
    src_files = {
        path.relative_to(src) for path in src.rglob("*") if path.is_file() and not _is_noise(path.relative_to(src))
    }
    for rel in sorted(src_files):
        target = dst / rel
        # shallow=False: compare CONTENT. A fresh install/copy leaves new mtimes, so a shallow
        # compare both invents differences and can miss a same-size edit.
        if not target.exists() or not filecmp.cmp(src / rel, target, shallow=False):
            changed.append(rel)

    extra: list[Path] = []
    if dst.is_dir():
        extra = sorted(
            path.relative_to(dst)
            for path in dst.rglob("*")
            if path.is_file() and not _is_noise(path.relative_to(dst)) and path.relative_to(dst) not in src_files
        )
    return changed, extra


def purge_bytecode(dst: Path) -> int:
    """Delete every `__pycache__` under the destination so no 2.113.0 bytecode outlives its source."""
    removed = 0
    for cache_dir in sorted(dst.rglob("__pycache__"), reverse=True):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
            removed += 1
    return removed


def validate_endpoints(source_root: Path, allow_downgrade: bool) -> int:
    """Check both ends before touching anything. Returns an exit code; 0 means proceed."""
    if not is_engine_tree(source_root):
        print(f"SYNC: ERROR - {source_root} is not a tableau-fabric-skills tree (no {ENGINE_SKILL.as_posix()})")
        return 2
    if not PLUGIN_ENGINE_ROOT.is_dir():
        print(f"SYNC: ERROR - the engine plugin is not installed at {PLUGIN_ENGINE_ROOT}")
        print("      Install it between sessions: copilot plugin install tableau-fabric-skills@tableau-collection")
        return 2

    installed = engine_version(PLUGIN_ENGINE_ROOT)
    incoming = engine_version(source_root)
    print(f"SYNC: installed={installed or 'unknown'}  incoming={incoming or 'unknown'}  ({source_root})")
    if version_tuple(incoming) < version_tuple(installed) and not allow_downgrade:
        print(f"SYNC: REFUSED - {incoming} is OLDER than the installed {installed}")
        print("      An older engine changes real output (2.113.0 emits deprecated Bing maps and drops")
        print("      a density-map worksheet where 2.126.0 emits azureMap). Pass --allow-downgrade if")
        print("      you mean it.")
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    """Sync the installed engine from a checkout, or report drift under --check."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, required=True, help="a tableau-fabric-skills checkout to sync FROM")
    parser.add_argument("--check", action="store_true", help="report drift and exit 1; change nothing")
    parser.add_argument("--allow-downgrade", action="store_true", help="permit syncing an OLDER engine VERSION")
    parser.add_argument("--verbose", action="store_true", help="list every differing file")
    args = parser.parse_args(argv)

    source_root = args.source.resolve()
    refusal = validate_endpoints(source_root, args.allow_downgrade)
    if refusal:
        return refusal

    src = source_root / ENGINE_SKILL
    dst = PLUGIN_ENGINE_ROOT / ENGINE_SKILL
    changed, extra = diff_tree(src, dst)
    if not changed and not extra:
        print(f"SYNC: IN_SYNC - {dst}")
        return 0

    if args.verbose or args.check:
        for rel in changed:
            print(f"  differs: {rel.as_posix()}")
        for rel in extra:
            print(f"  stale (not in source): {rel.as_posix()}")

    if args.check:
        print(f"SYNC: DRIFT - {len(changed)} file(s) differ, {len(extra)} stale")
        return 1

    for rel in changed:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, target)
    for rel in extra:
        (dst / rel).unlink()
    purged = purge_bytecode(dst)

    # Re-diff rather than trusting the copies. The whole reason this script exists is a lock that
    # makes some filesystem operations fail, so "it did not raise" is not evidence.
    still, still_extra = diff_tree(src, dst)
    if still or still_extra:
        print(f"SYNC: ERROR - {len(still)} file(s) still differ, {len(still_extra)} still stale after copying")
        return 3

    print(f"SYNC: UPDATED - {len(changed)} file(s) copied, {len(extra)} removed, {purged} __pycache__ purged")
    print(f"      engine VERSION is now {engine_version(PLUGIN_ENGINE_ROOT)}")
    print("      This syncs CONTENT only; the plugin's own manifest/version metadata still needs a")
    print("      real `copilot plugin update` between sessions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
