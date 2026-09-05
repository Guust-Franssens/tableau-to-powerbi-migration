"""
purpose: sync the INSTALLED copilot plugin's skill bundles from this repo, in place, without the
         `copilot plugin update` that a running Copilot session blocks.
usage:   python scripts/sync_installed_skills.py [--check] [--verbose] [--plugin-root PATH]

Why this exists
---------------
`copilot plugin update` fails with `Access is denied. (os error 5)` while a Copilot CLI session is
running. The obvious reading is "the whole plugin directory is locked, so nothing can be done until
you exit" - and that is what the toolkit assumed for weeks.

It is wrong. Measured 2026-08-01, the lock is narrower than the error implies:

    <base>                                  RENAME BLOCKED   (a running session holds it)
    <base>/<plugin>                 RENAME BLOCKED
    <base>/<plugin>/skills          renameable
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
import io
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from build_plugin import PLUGIN_NAME
from skill_plugin_source import DEFAULT_INSTALL_HINT, PLUGIN_ROOT_ENV, discover_skill_plugin

REPO = Path(__file__).resolve().parent.parent
REFERENCE_BUILD = REPO / "_build" / "skill-plugin-reference"
DEFAULT_SOURCE_REF = "origin/master"


class SourceRefError(RuntimeError):
    """The requested publication source is not a verified merged/default-branch ref."""


@dataclass(frozen=True)
class ReferenceCopy:
    """Generated reference skill tree and the source it came from."""

    skills_dir: Path
    label: str


def _git(args: list[str], *, repo: Path | None = None) -> str:
    """Run git in ``repo`` and return stdout, raising ``SourceRefError`` on failure."""
    repo_root = REPO if repo is None else repo
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SourceRefError(detail or f"git {' '.join(args)} failed with exit {result.returncode}")
    return result.stdout.strip()


def resolve_source_commit(source_ref: str) -> tuple[str, str]:
    """Resolve the locally available remote-tracking ref used as the publication source."""
    commit = _git(["rev-parse", "--verify", f"{source_ref}^{{commit}}"])
    full_ref = _git(["rev-parse", "--symbolic-full-name", source_ref])
    if not full_ref.startswith("refs/remotes/"):
        raise SourceRefError(
            f"{source_ref!r} resolves to {full_ref!r}, not a remote-tracking default/merged ref"
        )
    _git(["cat-file", "-e", f"{commit}:.github/skills"])
    _git(["cat-file", "-e", f"{commit}:scripts/build_plugin.py"])
    default_commit = _git(["rev-parse", "--verify", f"{DEFAULT_SOURCE_REF}^{{commit}}"])
    if commit != default_commit:
        merged = subprocess.run(
            ["git", "-C", str(REPO), "merge-base", "--is-ancestor", commit, default_commit],
            check=False,
            capture_output=True,
            text=True,
        )
        if merged.returncode != 0:
            raise SourceRefError(f"{source_ref!r} is not proven merged into {DEFAULT_SOURCE_REF!r}")
    return commit, full_ref


def _extract_ref(commit: str, destination: Path) -> None:
    """Extract committed repository bytes for ``commit`` into ``destination``."""
    destination.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", str(REPO), "archive", "--format=tar", commit],
        check=True,
        capture_output=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
        for member in tar:
            rel = Path(member.name)
            if rel.is_absolute() or ".." in rel.parts:
                raise SourceRefError(f"git archive produced unsafe path {member.name!r}")
            target = destination / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise SourceRefError(f"git archive could not extract {member.name!r}")
                with extracted, target.open("wb") as output:
                    shutil.copyfileobj(extracted, output)


def _build_reference_from(workdir: Path, repo_root: Path) -> Path:
    """Generate the canonical bundles with build_plugin.py, so this script defines no layout itself.

    Reusing the generator is the point: if it ever changes what ships (a new bundle, a renamed
    file), this script follows automatically instead of drifting into a second, subtly different
    definition of "what the plugin contains".
    """
    subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "build_plugin.py"), "--out", str(workdir)],
        check=True,
        capture_output=True,
        text=True,
    )
    built = workdir / "plugins" / PLUGIN_NAME / "skills"
    if not built.is_dir():
        raise SystemExit(f"build_plugin.py did not produce {built}")
    return built


def build_reference_copy(
    workdir: Path, *, source_ref: str = DEFAULT_SOURCE_REF, from_worktree: bool = False
) -> ReferenceCopy:
    """Build reference bundles from a verified source ref, or from the worktree by explicit opt-in."""
    if from_worktree:
        head = _git(["rev-parse", "--verify", "HEAD"])
        skills_dir = _build_reference_from(workdir / "reference", REPO)
        return ReferenceCopy(skills_dir, f"WORKTREE {REPO} at HEAD {head}")

    commit, full_ref = resolve_source_commit(source_ref)
    source = workdir / "source"
    _extract_ref(commit, source)
    skills_dir = _build_reference_from(workdir / "reference", source)
    return ReferenceCopy(skills_dir, f"{full_ref} at {commit}")


def diff_tree(src: Path, dst: Path) -> tuple[list[Path], list[Path]]:
    """Return (files needing copy, files present in dst but not src), as paths relative to src."""
    changed: list[Path] = []
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(src)
        target = dst / rel
        # shallow=False: compare CONTENT, not size+mtime. An install copies files with fresh
        # mtimes, so a shallow compare reports differences that do not exist - and worse, could
        # miss a same-size edit.
        if not target.exists() or target.is_symlink() or not filecmp.cmp(path, target, shallow=False):
            changed.append(rel)

    extra: list[Path] = []
    if dst.is_dir():
        src_files = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
        extra = sorted(
            p.relative_to(dst)
            for p in dst.rglob("*")
            if (p.is_file() or p.is_symlink()) and p.relative_to(dst) not in src_files
        )
    return changed, extra


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def destination_is_safe(root: Path, rel: Path, *, writing: bool) -> bool:
    """Return whether touching ``root / rel`` cannot escape the installed skills directory."""
    if rel.is_absolute() or ".." in rel.parts:
        return False
    root_resolved = root.resolve()
    target = root / rel
    if not _is_relative_to(target.parent.resolve(), root_resolved):
        return False
    if writing and target.is_symlink():
        return False
    if writing and target.exists() and not _is_relative_to(target.resolve(), root_resolved):
        return False
    return True


def main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-branches,too-many-return-statements,too-many-statements
    """Sync the installed bundles from the repo, or report drift under --check."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report drift and exit 1; change nothing")
    parser.add_argument("--verbose", action="store_true", help="list every file")
    parser.add_argument(
        "--source-ref",
        default=None,
        help=f"remote-tracking merged/default-branch ref to publish from (default: {DEFAULT_SOURCE_REF})",
    )
    parser.add_argument(
        "--from-worktree",
        action="store_true",
        help="DANGEROUS: publish/check this caller's possibly unmerged working tree instead of --source-ref",
    )
    parser.add_argument(
        "--plugin-root",
        type=Path,
        help=f"explicit installed plugin root; also supported via {PLUGIN_ROOT_ENV}",
    )
    args = parser.parse_args(argv)
    source_ref = args.source_ref or DEFAULT_SOURCE_REF
    if args.from_worktree and args.source_ref is not None:
        print("SYNC: ERROR - --from-worktree and --source-ref are mutually exclusive")
        return 5

    discovery = discover_skill_plugin(plugin_root_override=args.plugin_root)
    if discovery.status == "multiple":
        print("SYNC: ERROR - multiple installed plugins carry these skill bundles")
        for candidate in discovery.candidates:
            print(f"      {candidate}")
        print("      Remove the duplicate; otherwise one copy can silently shadow another.")
        return 4
    if not discovery.ok or not discovery.skills_dir:
        print(f"SYNC: ERROR - plugin not installed ({discovery.detail})")
        print(f"      {discovery.install_hint or DEFAULT_INSTALL_HINT}")
        return 2

    installed = discovery.skills_dir
    if REFERENCE_BUILD.exists():
        shutil.rmtree(REFERENCE_BUILD)
    try:
        if args.from_worktree:
            print("SYNC: WORKTREE SOURCE - explicit --from-worktree is serving unmerged local bytes")
        try:
            reference = build_reference_copy(
                REFERENCE_BUILD,
                source_ref=source_ref,
                from_worktree=args.from_worktree,
            )
        except SourceRefError as exc:
            print(f"SYNC: ERROR - cannot verify publication source {source_ref!r}")
            print(f"      {exc}")
            print(
                "      Refusing to fall back to the caller's working tree; "
                "use --from-worktree explicitly to test it."
            )
            return 5
        src = reference.skills_dir
        print(f"SYNC: SOURCE - {reference.label}")
        changed, extra = diff_tree(src, installed)

        if not changed and not extra:
            print(f"SYNC: IN_SYNC - {installed} ({discovery.identity})")
            return 0

        for rel in changed:
            print(f"  differs: {rel.as_posix()}")
        for rel in extra:
            print(f"  stale (not in build): {rel.as_posix()}")

        if args.check:
            print(f"SYNC: DRIFT - {len(changed)} file(s) differ, {len(extra)} stale")
            return 1

        for rel in changed:
            if not destination_is_safe(installed, rel, writing=True):
                print(f"SYNC: ERROR - unsafe destination path: {rel.as_posix()}")
                return 6
            target = installed / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, target)
        for rel in extra:
            if not destination_is_safe(installed, rel, writing=False):
                print(f"SYNC: ERROR - unsafe stale path: {rel.as_posix()}")
                return 6
            (installed / rel).unlink()

        # Re-diff rather than trusting the copies. The whole reason this script exists is a lock
        # that makes some filesystem operations fail, so "it did not raise" is not evidence.
        still, _ = diff_tree(src, installed)
        if still:
            print(f"SYNC: ERROR - {len(still)} file(s) still differ after copying")
            return 3

        print(
            f"SYNC: UPDATED - {len(changed)} file(s) copied, {len(extra)} removed at {installed} ({discovery.identity})"
        )
        print("      Skills are snapshotted at session start, so a RUNNING session keeps the old")
        print("      copy in memory. New sessions (and subagents they spawn) get this one.")
        return 0
    finally:
        if REFERENCE_BUILD.exists():
            shutil.rmtree(REFERENCE_BUILD)


if __name__ == "__main__":
    raise SystemExit(main())
