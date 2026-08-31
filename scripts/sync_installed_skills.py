"""
purpose: sync the INSTALLED copilot plugin's skill bundles from the MERGED commit, in place, without
         the `copilot plugin update` that a running Copilot session blocks.
usage:   python scripts/sync_installed_skills.py [--check] [--json] [--verbose]
                                                 [--ref REF] [--fetch] [--from-worktree]
                                                 [--plugin-root PATH]

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

Why the MERGED commit and not this working tree (issue #410)
------------------------------------------------------------
This script used to publish whatever the working tree that ran it happened to contain. With several
worktrees carrying unmerged edits to the same bundle, "in sync" became branch-dependent, unstable,
and a race. Measured 2026-08-30, four versions of one shipped file existed at once, and the INSTALLED
one - the copy every newly spawned subagent reads - was `master + 79 lines`: content on no merged
branch. It thrashed three times in one day, each sync overwriting the last and turning every other
worktree's `--check` red. Nobody was wrong; the tool had no notion of WHICH version was authoritative.

A stale bundle silently invalidates a measurement. A too-new one has the same property with the
opposite sign, and it is worse: a measurement taken against unmerged guidance is reproducible from
NO commit at all. So the authoritative version is the merged one, and both the comparison and the
publish read it from a ref (`origin/HEAD` -> `origin/master` -> `origin/main`, or `--ref`), never
from the current checkout. Consequences, all deliberate:

* a feature branch with unmerged skill edits does NOT fail `--check`; it prints a NOTE, because the
  installed copy is correctly the merged one and the operator needs to know their edits are not what
  a subagent reads;
* the verdict is identical from every worktree, including a detached HEAD, because HEAD is not read;
* publishing is a post-merge action, not a per-branch one.

`--from-worktree` is the deliberate, loud opt-in for testing unmerged skill content with a subagent.

Deliberately OFFLINE by default. `preflight.ps1` runs this check on every migration start, and
`AGENTS.md` already settled that a mandatory network round trip there is a tax on every run; being
behind is not an error. The two failure modes are also asymmetric: a stale local `origin/master`
publishes content that is still on a real merged commit, so a measurement against it stays
reproducible from a commit - the property this whole design exists to protect. `--fetch` refreshes
the ref when you want it, and never fails the run if the network is unavailable.

Scope, deliberately: this syncs bundle CONTENT only. It is not a substitute for a real
`copilot plugin update` when the plugin's manifest, version or MCP/agent wiring changes - run that
between sessions. For the common case (a skill's prose or scripts changed) this is the whole job.
"""

from __future__ import annotations

import argparse
import filecmp
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from skill_plugin_source import DEFAULT_INSTALL_HINT, PLUGIN_ROOT_ENV, discover_skill_plugin

REPO = Path(__file__).resolve().parent.parent

# Ordered candidates for "the merged commit". `origin/HEAD` is the remote's own declared default
# branch, so it stays right when the default branch is renamed; the two explicit names are the
# fallback for clones that never got a symbolic `origin/HEAD`, which plenty of workflows never set.
PUBLISH_REF_CANDIDATES = (
    "refs/remotes/origin/HEAD",
    "refs/remotes/origin/master",
    "refs/remotes/origin/main",
)

# Exported from the ref so the MERGED tree defines the WHOLE publish, not merely its content: which
# bundles ship, and the layout they ship in, are `build_plugin.py`'s decisions. Taking those from the
# working tree while taking content from the ref would invent a third, hybrid notion of "what ships"
# - which is the ambiguity issue #410 is about. `scripts` rather than just `build_plugin.py`, so a
# future sibling import inside the generator cannot break this silently.
EXPORT_PATHS = (".github/skills", "scripts")

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_NO_PLUGIN = 2
EXIT_COPY_FAILED = 3
EXIT_MULTIPLE_PLUGINS = 4
EXIT_NO_REF = 5


class PublishRefError(RuntimeError):
    """No merged ref could be resolved, and guessing one is not allowed."""


@dataclass(frozen=True)
class PublishSource:
    """Where the authoritative bundle content is being read from."""

    kind: str  # "ref" | "worktree"
    ref: str | None
    commit: str | None
    described: str

    @property
    def from_worktree(self) -> bool:
        """Whether this is the deliberate unmerged-content override."""
        return self.kind == "worktree"


def _git(args: list[str], *, repo: Path | None = None, binary: bool = False) -> subprocess.CompletedProcess:
    """Run git in `repo` (default: this checkout) and return the completed process, never raising."""
    return subprocess.run(  # pylint: disable=subprocess-run-check
        ["git", *args],
        cwd=str(repo or REPO),
        capture_output=True,
        check=False,
        **({} if binary else {"text": True}),
    )


def resolve_publish_ref(explicit: str | None = None, *, repo: Path | None = None) -> PublishSource:
    """Resolve the merged ref whose bundles are authoritative, or raise.

    Refusing to fall back to the working tree is the point. A silent fallback is the exact shape of
    the bug being replaced: it would publish unmerged content on precisely the machines whose git
    layout is unusual, and say nothing about it.
    """
    candidates = [explicit] if explicit else list(PUBLISH_REF_CANDIDATES)
    for candidate in candidates:
        probe = _git(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], repo=repo)
        commit = probe.stdout.strip()
        if probe.returncode == 0 and commit:
            return PublishSource(kind="ref", ref=candidate, commit=commit, described=f"{candidate} @ {commit[:12]}")
    raise PublishRefError("cannot resolve the merged publish ref (tried: " + ", ".join(map(str, candidates)) + ")")


def fetch_origin(repo: Path | None = None) -> tuple[bool, str]:
    """Refresh remote refs. Advisory: a failure is reported, never fatal.

    Offline must not block the gate. A stale local `origin/master` still names a real merged commit,
    so what it publishes stays reproducible from a commit; that is a far smaller problem than a
    preflight that fails on a train.
    """
    try:
        done = subprocess.run(  # pylint: disable=subprocess-run-check
            ["git", "fetch", "--quiet", "origin"],
            cwd=str(repo or REPO),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "git fetch failed").strip().splitlines()
        return False, detail[-1] if detail else "git fetch failed"
    return True, "ok"


def export_ref_tree(ref: str, dest: Path, *, repo: Path | None = None) -> Path:
    """Materialise `EXPORT_PATHS` at `ref` into `dest`, with git as the only reader of the ref.

    Git does the export, so nothing here reimplements git's checkout conversion. That matters more
    than it looks: measured 2026-08-31 on this repo (`core.autocrlf=true`, no `.gitattributes`),
    `git archive` DOES apply the CRLF conversion - the exported `powerbi-ai-readiness/SKILL.md` is
    19335 bytes and byte-identical to the checked-out one, while the raw blob from `git cat-file` is
    19064 bytes and LF. So switching the source from the working tree to a ref changes WHICH commit
    is published without changing the line endings, and costs no one-off rewrite of every installed
    file. (It also means the published bytes still follow the publishing machine's `core.autocrlf`.
    That is harmless here: an installed plugin copy is per-machine and never shared.)
    """
    archived = _git(["archive", "--format=tar", ref, "--", *EXPORT_PATHS], repo=repo, binary=True)
    if archived.returncode != 0:
        detail = (archived.stderr or b"").decode("utf-8", "replace").strip()
        raise PublishRefError(f"git archive {ref} failed: {detail}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as tar:
        # PEP 706's `data` filter exists from 3.11.4, but not on every 3.11 patch this repo may meet,
        # so it is only passed where present.
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # pragma: no cover - only reachable on Python < 3.11.4
            tar.extractall(dest)  # noqa: S202
    return dest


def build_reference_copy(workdir: Path, source_root: Path) -> Path:
    """Generate the canonical bundles with `source_root`'s build_plugin.py.

    Reusing the generator is the point: if it ever changes what ships (a new bundle, a renamed
    file), this script follows automatically instead of drifting into a second, subtly different
    definition of "what the plugin contains". It is taken from `source_root` for the same reason the
    content is, so a branch that adds a bundle cannot publish it before the branch merges.
    """
    generator = source_root / "scripts" / "build_plugin.py"
    if not generator.is_file():
        raise SystemExit(f"no build_plugin.py at {generator}")
    subprocess.run(
        [sys.executable, str(generator), "--out", str(workdir)],
        check=True,
        capture_output=True,
        text=True,
    )
    built = sorted(p for p in workdir.glob("plugins/*/skills") if p.is_dir())
    if len(built) != 1:
        raise SystemExit(f"expected exactly one plugins/*/skills under {workdir}, found {len(built)}")
    return built[0]


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


def local_skill_edits(ref: str, bundles: list[str], *, repo: Path | None = None) -> list[str]:
    """Shipped-bundle paths where this working tree differs from `ref`, committed or not.

    This is the NOTE, not the verdict. It answers the question an operator editing a skill actually
    has - "is what I am looking at what a subagent reads?" - which the drift check deliberately no
    longer conflates with "is the published copy correct?".
    """
    if not bundles:
        return []
    pathspecs = [f".github/skills/{name}" for name in bundles]
    diff = _git(["diff", "--name-only", ref, "--", *pathspecs], repo=repo)
    if diff.returncode != 0:
        return []
    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def worktree_banner() -> list[str]:
    """The loud header printed whenever unmerged working-tree content is being served."""
    return [
        "SYNC: !!! --from-worktree: reading UNMERGED working-tree content !!!",
        "      Every subagent spawned afterwards reads guidance that is on NO merged commit, so a",
        "      measurement taken against it is reproducible from no commit at all.",
        "      Restore the merged copy when done: python scripts/sync_installed_skills.py",
    ]


def _resolve_source(args: argparse.Namespace) -> PublishSource:
    """Pick the authoritative content source, honouring --from-worktree / --ref."""
    if args.from_worktree:
        return PublishSource(kind="worktree", ref=None, commit=None, described=f"working tree at {REPO}")
    return resolve_publish_ref(args.ref)


def _emit(args: argparse.Namespace, payload: dict, lines: list[str], exit_code: int) -> int:
    """Emit either the JSON verdict (preflight's input) or the human lines, and return `exit_code`."""
    if args.json:
        print(json.dumps({**payload, "exit_code": exit_code}, indent=2))
    else:
        for line in lines:
            print(line)
    return exit_code


def _discovery_failure(args: argparse.Namespace, discovery) -> int | None:
    """Return an exit code when the installed plugin cannot be used, else None."""
    if discovery.status == "multiple":
        return _emit(
            args,
            {"status": "multiple_plugins", "candidates": [str(c) for c in discovery.candidates]},
            [
                "SYNC: ERROR - multiple installed plugins carry these skill bundles",
                *(f"      {candidate}" for candidate in discovery.candidates),
                "      Remove the duplicate; otherwise one copy can silently shadow another.",
            ],
            EXIT_MULTIPLE_PLUGINS,
        )
    if not discovery.ok or not discovery.skills_dir:
        return _emit(
            args,
            {"status": "no_plugin", "detail": discovery.detail},
            [
                f"SYNC: ERROR - plugin not installed ({discovery.detail})",
                f"      {discovery.install_hint or DEFAULT_INSTALL_HINT}",
            ],
            EXIT_NO_PLUGIN,
        )
    return None


def main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-locals,too-many-return-statements
    """Sync the installed bundles from the merged ref, or report drift under --check."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report drift and exit 1; change nothing")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable verdict for preflight.ps1")
    parser.add_argument("--verbose", action="store_true", help="list every file the reference build contains")
    parser.add_argument("--ref", help=f"merged ref to publish (default: first of {', '.join(PUBLISH_REF_CANDIDATES)})")
    parser.add_argument("--fetch", action="store_true", help="refresh remote refs first; never fatal if offline")
    parser.add_argument(
        "--from-worktree",
        action="store_true",
        help="publish THIS working tree instead of the merged ref - deliberately serves unreviewed guidance",
    )
    parser.add_argument(
        "--plugin-root",
        type=Path,
        help=f"explicit installed plugin root; also supported via {PLUGIN_ROOT_ENV}",
    )
    args = parser.parse_args(argv)

    discovery = discover_skill_plugin(plugin_root_override=args.plugin_root)
    failed = _discovery_failure(args, discovery)
    if failed is not None:
        return failed
    installed = discovery.skills_dir

    # The fetch happens here, not inside `_resolve_source`, so its outcome survives into the failure
    # payload too - and so its human line can be suppressed under --json, which preflight PARSES: a
    # single stray line ahead of the JSON reads to preflight as "did not report", i.e. an unverified
    # bundle, which is the false green the whole check exists to prevent.
    fetch_note: dict | None = None
    if args.fetch and not args.from_worktree:
        ok, detail = fetch_origin()
        fetch_note = {"ok": ok, "detail": detail}
        if not args.json:
            print(f"SYNC: fetch origin - {'ok' if ok else 'FAILED, using the local ref: ' + detail}")

    try:
        source = _resolve_source(args)
    except PublishRefError as exc:
        return _emit(
            args,
            {"status": "no_ref", "detail": str(exc), "fetch": fetch_note},
            [
                f"SYNC: ERROR - {exc}",
                "      The installed copy must be what is MERGED, so this refuses to guess (issue #410).",
                "      Add the remote and fetch, pass --ref <ref>, or publish this checkout deliberately",
                "      with --from-worktree, which serves UNREVIEWED guidance to every new subagent.",
            ],
            EXIT_NO_REF,
        )

    banner = worktree_banner() if source.from_worktree else []
    if banner and not args.json:
        for line in banner:
            print(line)

    build_dir = REPO / "_build"
    build_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="skill-plugin-", dir=build_dir))
    try:
        source_root = REPO if source.from_worktree else export_ref_tree(str(source.ref), workdir / "ref-src")
        src = build_reference_copy(workdir / "reference", source_root)
        changed, extra = diff_tree(src, installed)
        bundles = sorted(p.name for p in src.iterdir() if p.is_dir())
        unmerged = [] if source.from_worktree else local_skill_edits(str(source.ref), bundles)

        note = (
            [
                f"SYNC: NOTE - this working tree differs from {source.ref} in {len(unmerged)} shipped skill file(s):",
                *(f"        {path}" for path in unmerged),
                "      Subagents read the MERGED copy, not these edits - deliberately (issue #410).",
                "      To test them with a subagent: python scripts/sync_installed_skills.py --from-worktree",
            ]
            if unmerged
            else []
        )
        inventory = (
            [f"  in build: {p.relative_to(src).as_posix()}" for p in sorted(src.rglob("*")) if p.is_file()]
            if args.verbose
            else []
        )
        payload = {
            "status": "in_sync",
            "source": source.kind,
            "ref": source.ref,
            "commit": source.commit,
            "described": source.described,
            "skills_dir": str(installed),
            "identity": discovery.identity,
            "changed": [rel.as_posix() for rel in changed],
            "extra": [rel.as_posix() for rel in extra],
            "local_unmerged": unmerged,
            "fetch": fetch_note,
        }

        if not changed and not extra:
            return _emit(
                args,
                payload,
                [
                    *inventory,
                    f"SYNC: IN_SYNC - {installed} ({discovery.identity}) from {source.described}",
                    *note,
                ],
                EXIT_OK,
            )

        drift_lines = [
            *inventory,
            *(f"  differs: {rel.as_posix()}" for rel in changed),
            *(f"  stale (not in build): {rel.as_posix()}" for rel in extra),
        ]

        if args.check:
            return _emit(
                args,
                {**payload, "status": "drift"},
                [
                    *drift_lines,
                    f"SYNC: DRIFT - {len(changed)} file(s) differ, {len(extra)} stale, vs {source.described}",
                    "      Publish the merged copy: python scripts/sync_installed_skills.py",
                    "      (If you published with --from-worktree, that same command restores it.)",
                    *note,
                ],
                EXIT_DRIFT,
            )

        for rel in changed:
            target = installed / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, target)
        for rel in extra:
            (installed / rel).unlink()

        # Re-diff rather than trusting the copies. The whole reason this script exists is a lock
        # that makes some filesystem operations fail, so "it did not raise" is not evidence.
        still, _ = diff_tree(src, installed)
        if still:
            return _emit(
                args,
                {**payload, "status": "copy_failed", "still_differ": [r.as_posix() for r in still]},
                [f"SYNC: ERROR - {len(still)} file(s) still differ after copying"],
                EXIT_COPY_FAILED,
            )

        return _emit(
            args,
            {**payload, "status": "updated"},
            [
                *drift_lines,
                f"SYNC: UPDATED - {len(changed)} file(s) copied, {len(extra)} removed at {installed} "
                f"({discovery.identity}) from {source.described}",
                "      Skills are snapshotted at session start, so a RUNNING session keeps the old",
                "      copy in memory. New sessions (and subagents they spawn) get this one.",
                *note,
            ],
            EXIT_OK,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
