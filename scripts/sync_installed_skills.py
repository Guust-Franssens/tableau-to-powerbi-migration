"""
purpose: sync the INSTALLED copilot plugin's skill bundles from this repo, in place, without the
         `copilot plugin update` that a running Copilot session blocks.
usage:   python scripts/sync_installed_skills.py [--check] [--verbose]

Why this exists
---------------
`copilot plugin update` fails with `Access is denied. (os error 5)` while a Copilot CLI session is
running. The obvious reading is "the whole plugin directory is locked, so nothing can be done until
you exit" - and that is what the toolkit assumed for weeks.

It is wrong. Measured 2026-08-01, the lock is narrower than the error implies:

    <base>                                  RENAME BLOCKED   (a running session holds it)
    <base>/powerbi-playbook         RENAME BLOCKED
    <base>/powerbi-playbook/skills  renameable
    any file inside                         freely writable, creatable and deletable

`plugin update` fails because it replaces the plugin by swapping the top-level directory, which is
exactly the one operation that is blocked. Copying files into place needs none of that. So the
bundles can be brought up to date mid-session; only the plugin's own version metadata cannot.

This matters because the plugin copy SHADOWS `.github/skills/`: until the installed copy is current,
subagents execute the OLD code no matter what the repo says, and nothing surfaces the mismatch.
Being stuck behind a session restart made that likely to be deferred, which is how a stale bundle
once silently invalidated a measurement.

Scope, deliberately: this syncs bundle CONTENT only. It is not a substitute for a real
`copilot plugin update` when the plugin's manifest, version or MCP/agent wiring changes - run that
between sessions. For the common case (a skill's prose or scripts changed) this is the whole job.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLED = (
    Path.home() / ".copilot" / "installed-plugins" / "powerbi-playbook-collection" / "powerbi-playbook" / "skills"
)


def build_reference_copy(workdir: Path) -> Path:
    """Generate the canonical bundles with build_plugin.py, so this script defines no layout itself.

    Reusing the generator is the point: if it ever changes what ships (a new bundle, a renamed
    file), this script follows automatically instead of drifting into a second, subtly different
    definition of "what the plugin contains".
    """
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "build_plugin.py"), "--out", str(workdir)],
        check=True,
        capture_output=True,
        text=True,
    )
    built = workdir / "plugins" / "powerbi-playbook" / "skills"
    if not built.is_dir():
        raise SystemExit(f"build_plugin.py did not produce {built}")
    return built


def diff_tree(src: Path, dst: Path) -> tuple[list[Path], list[Path]]:
    """Return (files needing copy, files present in dst but not src), as paths relative to src."""
    changed: list[Path] = []
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(src)
        target = dst / rel
        # shallow=False: compare CONTENT, not size+mtime. An install copies files with fresh
        # mtimes, so a shallow compare reports differences that do not exist - and worse, could
        # miss a same-size edit.
        if not target.exists() or not filecmp.cmp(path, target, shallow=False):
            changed.append(rel)

    extra: list[Path] = []
    if dst.is_dir():
        src_files = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
        extra = sorted(
            p.relative_to(dst) for p in dst.rglob("*") if p.is_file() and p.relative_to(dst) not in src_files
        )
    return changed, extra


def main(argv: list[str] | None = None) -> int:
    """Sync the installed bundles from the repo, or report drift under --check."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report drift and exit 1; change nothing")
    parser.add_argument("--verbose", action="store_true", help="list every file")
    args = parser.parse_args(argv)

    if not INSTALLED.is_dir():
        print(f"SYNC: ERROR - plugin not installed at {INSTALLED}")
        print(
            "      Install it once between sessions: copilot plugin install "
            "powerbi-playbook@powerbi-playbook-collection"
        )
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        src = build_reference_copy(Path(tmp))
        changed, extra = diff_tree(src, INSTALLED)

        if not changed and not extra:
            print(f"SYNC: IN_SYNC - {INSTALLED}")
            return 0

        for rel in changed:
            print(f"  differs: {rel.as_posix()}")
        for rel in extra:
            print(f"  stale (not in build): {rel.as_posix()}")

        if args.check:
            print(f"SYNC: DRIFT - {len(changed)} file(s) differ, {len(extra)} stale")
            return 1

        for rel in changed:
            target = INSTALLED / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, target)
        for rel in extra:
            (INSTALLED / rel).unlink()

        # Re-diff rather than trusting the copies. The whole reason this script exists is a lock
        # that makes some filesystem operations fail, so "it did not raise" is not evidence.
        still, _ = diff_tree(src, INSTALLED)
        if still:
            print(f"SYNC: ERROR - {len(still)} file(s) still differ after copying")
            return 3

        print(f"SYNC: UPDATED - {len(changed)} file(s) copied, {len(extra)} removed")
        print("      Skills are snapshotted at session start, so a RUNNING session keeps the old")
        print("      copy in memory. New sessions (and subagents they spawn) get this one.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
