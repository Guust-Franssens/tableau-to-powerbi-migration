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

Why the MERGED commit and not this working tree (issue #410)
------------------------------------------------------------
This script used to publish whatever the working tree that ran it happened to contain. With several
worktrees carrying unmerged edits to the same bundle, "in sync" became branch-dependent, unstable,
and a race. Measured 2026-08-30, four versions of one shipped file existed at once, and the INSTALLED
one - the copy every newly spawned subagent reads - was `master + 79 lines`: content on no merged
branch. So the authoritative version is the merged one, and both the comparison and the publish read
it from a ref, never from the current checkout.

Three round-2 review findings, and what each one changed
--------------------------------------------------------
**1. The DESTINATION is now proved, never inferred.** Discovery used to select any installed plugin
carrying any bundle from the inventory, and this script then wrote into it. Reproduced in throw-away
plugin roots: a plain sync overwrote a foreign plugin's `SKILL.md` merely because it shared one
current bundle name (and on `origin/master`, deleted a private file inside it); `--from-worktree`
with a branch-invented bundle name selected an unrelated plugin outright, overwrote it, deleted a
file inside it, and left the intended plugin untouched. Ownership now needs a PROOF that content
cannot fake - see `skill_plugin_source.py` - and where it cannot be proved this exits non-zero
having written nothing. `--from-worktree` additionally REQUIRES an explicit `--plugin-root`, so a
branch can no longer choose a destination at all.

**2. The remote's ADVERTISED default branch is asked for, and never guessed.** `origin/HEAD` is a
local marker `git fetch` does not refresh, so after a default-branch rename this published the old
branch and reported `in_sync`. Resolution now asks `ls-remote --symref`, fetches that exact ref (a
single-branch clone never had it), and REFUSES rather than falling back to `origin/master` ->
`origin/main`. Offline it uses the branch a previous online run recorded; with no record and no
remote it reports `unverified_default`, which preflight must never read as green.

**3. A RETIRED bundle is now visible.** Extra-file detection was scoped to the CURRENT inventory, so
a bundle removed from `SHIPPED_SKILLS` stopped being "owned", stayed installed forever, and `--check`
still said `in_sync`. The ownership marker records the inventory each publish installed, so a bundle
that has since left it is reported as drift and removed. Bundles that were never ours stay untouched.

Containment is a RESOLVED question, at every depth (round-7 finding 1)
----------------------------------------------------------------------
Rounds 2-6 built the destination boundary out of top-level bundle NAMES: an absolute path, a `..`, a
Windows alias, an outward junction and a same-named junction to a sibling are all refused. Every one
of those is depth 0, and what enforces the boundary for file operations is a prefix test on
`rel.parts[0]`.

A prefix test is not containment when any component of the path can be a reparse point. Measured on
Python 3.13.2 with a junction ONE level deeper than any of those rounds probed::

    rglob descends:       ['bundle', 'bundle/SKILL.md', 'bundle/nested', 'bundle/nested/keep.txt']
    keep after unlink:    False        <- an EXTERNAL file, deleted
    sentinel after copy2: 'published'  <- an EXTERNAL file, overwritten

`rglob` walks straight through the junction; the relative path still starts with an owned bundle; and
`unlink()`/`copy2()` then follow it out of the plugin, with the run reporting `updated` and exit 0.

Three things now close the CLASS rather than that one depth:

* `walk_files` replaces `rglob` on both sides and does not descend into a reparse point, so what
  lies under a junction is never a deletion candidate;
* `install_reparse_points` REPORTS what the walk refused to enter - silently skipping would move the
  fail-open from "it deleted the wrong thing" to "it reported in_sync about a tree it could not
  fully read", which is the same defect wearing the fix's clothes;
* `contained_target` re-resolves every copy and every delete target immediately before the operation
  and refuses anything landing outside the RESOLVED install. The copy set comes from the SOURCE
  tree, so no amount of destination scoping can reach it - only resolution can.

Offline, a RECORD is not a CONFIRMATION (round-7 finding 2)
-----------------------------------------------------------
Finding 2 above made the ONLINE path ask the remote. Offline the run falls back to the branch a
previous run recorded - and used to report `default_verified: true` about it, which preflight read as
`merged_ok`. Measured: an online run recorded `master`, the remote's HEAD then moved to `main` with
different content, and offline the tool certified the OLD content as merged. A recorded default is
evidence of what the remote said LAST TIME; it cannot certify what the remote says NOW. It is now
reported as `default_verified: false` with `default_unconfirmed` naming the reason, and preflight
carries it as an explicit CANNOT-ESTABLISH row that is never green.

Network cost, deliberately re-decided: the default run now makes ONE `ls-remote --symref` call
(bounded, ~1s) because there is no offline way to detect a default-branch rename, and a false
`in_sync` is the failure this whole design exists to prevent. It still never runs a full `git fetch`
unless `--fetch` is passed, and offline it falls back to the RECORDED verified default rather than
failing, so only a machine that has never once reached the remote is blocked.

Scope, deliberately: this syncs bundle CONTENT only. It is not a substitute for a real
`copilot plugin update` when the plugin's manifest, version or MCP/agent wiring changes - run that
between sessions.
"""

from __future__ import annotations

import argparse
import filecmp
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from skill_plugin_source import (
    DEFAULT_INSTALL_HINT,
    OWNER_MARKER_NAME,
    PLUGIN_ROOT_ENV,
    _is_link as is_reparse_point,  # the ONE measured answer to "symlink, junction, or neither"
    discover_skill_plugin,
    marker_bundle_problems,
    marker_bundle_target,
    marker_is_usable,
    read_owner_marker,
    write_owner_marker,
)

REPO = Path(__file__).resolve().parent.parent

# Exported from the ref so the MERGED tree defines the WHOLE publish, not merely its content: which
# bundles ship, the layout they ship in, and which plugin identities this repo owns are all
# `build_plugin.py`'s decisions. Taking any of them from the working tree while taking content from
# the ref would invent a third, hybrid notion of "what ships" - the ambiguity issue #410 is about.
EXPORT_PATHS = (".github/skills", "scripts")

# Where a verified default branch is remembered, so an offline run is not forced to guess. The git
# COMMON dir, not the working tree: it is shared by every worktree, is never committed, and already
# holds exactly this kind of remote-derived state (FETCH_HEAD, packed-refs).
DEFAULT_RECORD_NAME = "skill-sync-default.json"

LS_REMOTE_TIMEOUT_SECONDS = 8
FETCH_TIMEOUT_SECONDS = 60

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_NO_PLUGIN = 2
EXIT_COPY_FAILED = 3
EXIT_MULTIPLE_PLUGINS = 4
EXIT_NO_REF = 5
EXIT_UNPROVEN_PLUGIN = 6
EXIT_UNVERIFIED_DEFAULT = 7
EXIT_UNSAFE_MARKER = 8
EXIT_UNSAFE_INSTALL = 9


class PublishRefError(RuntimeError):
    """No merged ref could be resolved, and guessing one is not allowed."""


class UnverifiedDefaultError(RuntimeError):
    """The remote's advertised default branch could not be established, so nothing is authoritative."""


class UnsafeMarkerError(RuntimeError):
    """The ownership marker's recorded inventory could name a deletion target outside the plugin."""


class UnsafeInstallError(RuntimeError):
    """A reparse point inside an owned bundle would carry a write or a delete out of the plugin."""


@dataclass(frozen=True)
class PublishSource:  # pylint: disable=too-many-instance-attributes
    """Where the authoritative bundle content is being read from."""

    kind: str  # "ref" | "worktree"
    ref: str | None
    commit: str | None
    described: str
    default_verified: bool = False
    default_proof: str = "none"  # "explicit" | "remote" | "recorded" | "worktree"
    default_verified_at: str | None = None
    alternatives: tuple[str, ...] = ()
    advertised_commit: str | None = None
    # Why `default_verified` is false when the run still produced a usable ref. It is a distinct
    # field rather than an overload of `detail` because the two states need OPPOSITE handling:
    # "cannot establish" still publishes and still compares, "unverified_default" refuses outright.
    default_unconfirmed: str | None = None

    @property
    def from_worktree(self) -> bool:
        """Whether this is the deliberate unmerged-content override."""
        return self.kind == "worktree"


@dataclass(frozen=True)
class PublishIdentity:
    """The ownership evidence, read from the PINNED tree rather than from this checkout."""

    publish_repo: str
    identities: tuple[str, ...]


@dataclass
class SyncPlan:  # pylint: disable=too-many-instance-attributes
    """Everything the publish/report step needs, assembled once so no step re-derives it."""

    source: PublishSource
    src: Path
    bundles: list[str]
    owned: list[str]
    formerly_owned: list[str]
    discovery: object
    identity: PublishIdentity
    marker_present: bool
    inventory_stale: bool
    workdir: Path
    base: dict = field(default_factory=dict)


def _git(args: list[str], *, repo: Path | None = None, binary: bool = False, timeout: float | None = None):
    """Run git in `repo` (default: this checkout) and return the completed process, never raising."""
    try:
        return subprocess.run(  # pylint: disable=subprocess-run-check
            ["git", *args],
            cwd=str(repo or REPO),
            capture_output=True,
            check=False,
            timeout=timeout,
            **({} if binary else {"text": True}),
        )
    except (subprocess.TimeoutExpired, OSError):
        return subprocess.CompletedProcess(args, 1, b"" if binary else "", b"" if binary else "")


def _rev_parse(ref: str, repo: Path | None = None) -> str | None:
    probe = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo=repo)
    commit = probe.stdout.strip()
    return commit if probe.returncode == 0 and commit else None


def has_origin(repo: Path | None = None) -> bool:
    """Whether an `origin` remote exists at all - a different failure from "it did not answer"."""
    return _git(["remote", "get-url", "origin"], repo=repo).returncode == 0


def remote_default(repo: Path | None = None) -> tuple[str | None, str | None]:
    """Ask the REMOTE which branch it advertises as HEAD, and at which COMMIT - in ONE call.

    `refs/remotes/origin/HEAD` is a *local* symbolic ref, and `git fetch` does NOT refresh it when
    the remote's default branch is renamed. Reproduced in review: a bare remote moved `master` ->
    `main` with different skill content, `git fetch` exited 0, the local marker still said
    `origin/master`, and the sync published master as if it were the default. `ls-remote --symref`
    is the one question that cannot go stale - and unlike `git remote set-head --auto` it writes
    nothing into the repository.

    The COMMIT half exists because verifying only the branch NAME leaves a second staleness intact:
    if the local remote-tracking ref already exists, nothing fetches it, so a same-branch advance
    (A -> B on `master`) stays invisible while the run still reports `default_verified` (round-3
    finding 3). `ls-remote --symref origin HEAD` already prints that sha beside `HEAD`, so this
    costs no extra network call.
    """
    listed = _git(["ls-remote", "--symref", "origin", "HEAD"], repo=repo, timeout=LS_REMOTE_TIMEOUT_SECONDS)
    if listed.returncode != 0:
        return None, None
    ref: str | None = None
    head: str | None = None
    for line in listed.stdout.splitlines():
        parts = line.split()
        if line.startswith("ref:"):
            if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                ref = "refs/remotes/origin/" + parts[1][len("refs/heads/") :]
        elif len(parts) >= 2 and parts[1] == "HEAD":
            head = parts[0]
    return ref, head


def remote_default_ref(repo: Path | None = None) -> str | None:
    """The advertised default branch as a remote-tracking ref name, without its commit."""
    return remote_default(repo)[0]


def _record_path(repo: Path | None = None) -> Path | None:
    common = _git(["rev-parse", "--git-common-dir"], repo=repo)
    if common.returncode != 0 or not common.stdout.strip():
        return None
    raw = Path(common.stdout.strip())
    return (raw if raw.is_absolute() else (repo or REPO) / raw) / DEFAULT_RECORD_NAME


def read_default_record(repo: Path | None = None) -> dict | None:
    """The last default branch a run actually confirmed with the remote, or None."""
    path = _record_path(repo)
    if path is None:
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) and loaded.get("ref") else None


def write_default_record(ref: str, repo: Path | None = None) -> None:
    """Remember a default branch the remote itself just advertised.

    This is what keeps the offline path honest without a guess: an offline run reports the branch
    some earlier run VERIFIED, with the timestamp, rather than whichever local ref happens to exist.
    """
    path = _record_path(repo)
    if path is None:
        return
    payload = {
        "schema": 1,
        "ref": ref,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:  # pragma: no cover - a read-only .git is not worth failing a publish over
        pass


def _default_alternatives(commit: str, repo: Path | None = None) -> tuple[str, ...]:
    """Local default-branch refs whose commit DIFFERS from the chosen one - i.e. a possible rename."""
    found = []
    for name in ("refs/remotes/origin/master", "refs/remotes/origin/main"):
        other = _rev_parse(name, repo)
        if other and other != commit:
            found.append(name)
    return tuple(found)


def fetch_origin(repo: Path | None = None) -> tuple[bool, str]:
    """Refresh remote refs. Advisory: a failure is reported, never fatal."""
    done = _git(["fetch", "--quiet", "origin"], repo=repo, timeout=FETCH_TIMEOUT_SECONDS)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "git fetch failed").strip().splitlines()
        return False, detail[-1] if detail else "git fetch failed"
    return True, "ok"


def fetch_exact_ref(ref: str, repo: Path | None = None) -> bool:
    """Fetch the ADVERTISED branch by explicit refspec.

    A single-branch clone's configured refspec covers only the branch it was cloned with, so a plain
    `git fetch origin` reports success and silently never brings down the branch the remote actually
    advertises. Measured in review: `--fetch` printed "ok", published `origin/master`, and emitted no
    warning at all while the remote's default was `main`.
    """
    branch = ref.rsplit("/", 1)[-1]
    done = _git(
        ["fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        repo=repo,
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    return done.returncode == 0


def _explicit_source(explicit: str, repo: Path | None) -> PublishSource:
    commit = _rev_parse(explicit, repo)
    if not commit:
        raise PublishRefError(f"--ref {explicit} does not resolve to a commit")
    return PublishSource(
        kind="ref",
        ref=explicit,
        commit=commit,
        described=f"{explicit} @ {commit[:12]}",
        default_verified=True,
        default_proof="explicit",
    )


def _advertised_default(repo: Path | None) -> tuple[str, str, str | None, str | None]:
    """(ref, proof, verified_at, advertised commit) for the default branch, or raise rather than guess."""
    advertised, head = remote_default(repo)
    if advertised:
        write_default_record(advertised, repo)
        return advertised, "remote", datetime.now(timezone.utc).isoformat(timespec="seconds"), head
    record = read_default_record(repo)
    if record:
        return str(record["ref"]), "recorded", record.get("verified_at"), None
    raise UnverifiedDefaultError(
        "origin did not answer `ls-remote --symref`, and no earlier run recorded a verified default "
        "branch, so which branch is authoritative is UNKNOWN"
    )


def resolve_publish_ref(
    explicit: str | None = None,
    *,
    repo: Path | None = None,
    fetch: bool = False,
) -> PublishSource:
    """Resolve the merged COMMIT whose bundles are authoritative, or raise.

    Refusing to fall back is the point, twice over. Falling back to the working tree would publish
    unmerged content on precisely the machines whose git layout is unusual; falling back to a list of
    likely branch names would publish the OLD default after a rename - both silently. The commit is
    what everything downstream uses; the ref name is only a display label, because a ref moves.
    """
    if explicit:
        return _explicit_source(explicit, repo)
    if not has_origin(repo):
        raise PublishRefError("this repository has no `origin` remote, so no merged ref can be resolved")

    ref, proof, verified_at, advertised = _advertised_default(repo)
    commit = _rev_parse(ref, repo)
    # Verifying the branch NAME is only half of it. If the local remote-tracking ref already exists,
    # nothing fetches it, so a same-branch advance stays invisible and stale guidance reports
    # `in_sync` with `default_verified=True` (round-3 finding 3). Fetch whenever the local tip is
    # ABSENT, DIFFERENT from the sha the remote just advertised, or `--fetch` asked for it.
    stale = advertised is not None and commit != advertised
    if proof == "remote" and (commit is None or fetch or stale):
        fetch_exact_ref(ref, repo)
        commit = _rev_parse(ref, repo)
    if commit is None:
        raise UnverifiedDefaultError(
            f"{ref} is the default branch to publish ({proof}), but it could not be fetched or "
            "resolved locally, and falling back to another branch would publish the wrong one"
        )
    if advertised is not None and commit != advertised:
        raise UnverifiedDefaultError(
            f"{ref} advertises {advertised[:12]} but this clone still resolves it to {commit[:12]} "
            "after fetching that exact refspec, so the authoritative content is UNKNOWN"
        )
    # A RECORD is not a CONFIRMATION (round-7 finding 2). `recorded` means origin did not answer
    # just now and this run reused the branch an earlier run confirmed. That is enough to publish
    # something rather than nothing, and it is NOT enough to certify: measured, an online run
    # recorded `master`, the remote's HEAD then moved to `main` with different content, and offline
    # the tool reported `default_verified: true` - which preflight reads as `merged_ok` - about
    # bytes from the branch the remote had abandoned.
    #
    # Deliberately NOT an error, and deliberately not `unverified_default`. Refusing here would make
    # every offline session start a hard failure and would strand the only machines that cannot
    # re-confirm; the honest verdict is "compared, and the authority behind the comparison could not
    # be re-established", which is a third state rather than either of the two that existed.
    unconfirmed = None
    if proof == "recorded":
        unconfirmed = (
            f"origin did not answer `ls-remote --symref`, so {ref} is the default a previous run "
            f"verified at {verified_at or 'an unrecorded time'}, not one confirmed now; a rename or a "
            "new commit on the remote since then is invisible from here"
        )
    return PublishSource(
        kind="ref",
        ref=ref,
        commit=commit,
        described=f"{ref} @ {commit[:12]}",
        default_verified=unconfirmed is None,
        default_proof=proof,
        default_verified_at=verified_at,
        alternatives=_default_alternatives(commit, repo),
        advertised_commit=advertised,
        default_unconfirmed=unconfirmed,
    )


def export_ref_tree(ref: str, dest: Path, *, repo: Path | None = None) -> Path:
    """Materialise `EXPORT_PATHS` at `ref` into `dest`, with git as the only reader of the ref.

    Git does the export, so nothing here reimplements git's checkout conversion. That matters more
    than it looks: measured 2026-08-31 on this repo (`core.autocrlf=true`, no `.gitattributes`),
    `git archive` DOES apply the CRLF conversion, so switching the source from the working tree to a
    ref changes WHICH commit is published without changing the line endings.
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


def pinned_identity(source_root: Path) -> PublishIdentity:
    """Read the OWNERSHIP evidence from `source_root`'s build_plugin.py, not from this checkout.

    Which plugin identities this repo owns is a merged decision exactly like which bundles ship. Read
    from the working tree it would be branch-controlled, which is the defect being removed: a branch
    could name someone else's installed plugin and have this script write into it.
    """
    path = source_root / "scripts" / "build_plugin.py"
    if not path.is_file():
        raise SystemExit(f"no build_plugin.py at {path}")
    spec = importlib.util.spec_from_file_location("_pinned_build_plugin", path)
    if spec is None or spec.loader is None:  # pragma: no cover - only on an unreadable file
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        derived = f"{module.PLUGIN_NAME}@{module.MARKETPLACE_NAME}"
        known = tuple(getattr(module, "KNOWN_PLUGIN_IDENTITIES", ()))
        return PublishIdentity(
            publish_repo=str(getattr(module, "PUBLISH_REPO", "")),
            identities=tuple(dict.fromkeys((*known, derived))),
        )
    finally:
        sys.modules.pop(spec.name, None)


def build_reference_copy(workdir: Path, source_root: Path) -> Path:
    """Generate the canonical bundles with `source_root`'s build_plugin.py.

    Reusing the generator is the point: if it ever changes what ships (a new bundle, a renamed
    file), this script follows automatically instead of drifting into a second, subtly different
    definition of "what the plugin contains".
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


def walk_files(root: Path) -> Iterator[Path]:
    """Every regular file under `root`, WITHOUT descending into a reparse point.

    `Path.rglob` follows junctions. Measured here on Python 3.13.2, with a junction one level below
    an owned bundle::

        rglob descends:   ['bundle', 'bundle/SKILL.md', 'bundle/nested', 'bundle/nested/keep.txt']

    That last path is the whole of round-7 finding 1: it is relative to the install, its first
    component is a genuinely owned bundle, so the `scope` prefix test passes - and `unlink()` then
    deletes a file that was never in the plugin at all.

    Stopping the walk is the primitive that says "the plugin's own file set ends HERE", and it is
    the right one for both sides. What lies under a junction belongs to the junction's TARGET, so
    those files are not the install's content: they must never become deletion candidates, and the
    walk must not spend a `realpath` per file to work that out. Validating after resolution instead
    would answer the same question one step too late - the candidate would already be in the plan,
    and every later consumer would have to remember to re-check it.

    A prefix test on `rel.parts[0]` is not containment when ANY component can be a reparse point, so
    this is paired with two other things rather than trusted alone: `install_reparse_points` reports
    what the walk refused to enter (silently skipping is how "cannot assess" collapses into the
    clean bucket), and `_apply` re-resolves every target immediately before it writes.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:  # pragma: no cover - an unreadable directory contributes no files
            continue
        for entry in entries:
            if is_reparse_point(entry):
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                yield entry


def install_reparse_points(installed: Path | None, owned: Sequence[str]) -> list[str]:
    """Reparse points at ANY depth inside the OWNED bundle directories, as `/`-joined relatives.

    Depth 0 is included deliberately: a bundle directory that is ITSELF a junction carries the copy
    out of the plugin just as surely as one nested below it, and the marker rules that already
    refuse such a name only ever run on RETIRED names (`marker_bundle_problems`), never on the
    bundles currently being published into.
    """
    if installed is None or not installed.is_dir():
        return []
    found: list[str] = []
    for name in owned:
        bundle = installed / name
        if is_reparse_point(bundle):
            found.append(name)
            continue
        stack = [bundle]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir())
            except OSError:  # pragma: no cover - nothing to descend into
                continue
            for entry in entries:
                if is_reparse_point(entry):
                    found.append(entry.relative_to(installed).as_posix())
                elif entry.is_dir():
                    stack.append(entry)
    return sorted(found)


def contained_target(installed: Path, installed_real: Path, rel: Path) -> Path:
    """`installed / rel`, proven to still be inside the RESOLVED install - or raise.

    The belt to `walk_files`'s braces, and it is aimed at the operation rather than at the plan:
    `shutil.copy2` and `Path.unlink` both follow a junction in the path they are handed, and the
    plan is built from the SOURCE tree, so scoping the destination walk cannot reach the copy side
    at all. Resolution is the only thing that answers "where will this write actually land".

    ⚠️ Resolve, then compare RESOLVED against RESOLVED. `Path.resolve()` is non-strict, so a leaf
    that does not exist yet still resolves its existing prefix - measured, `<skills>/b/nested/new.md`
    with `nested` a junction resolves to `<outside>/new.md` and `is_relative_to(<skills>)` is False,
    which is exactly the discrimination this needs.
    """
    target = installed / rel
    try:
        real = target.resolve()
    except OSError as exc:  # pragma: no cover - an unresolvable path is refused, not guessed
        raise UnsafeInstallError(f"{rel.as_posix()} could not be resolved under {installed}: {exc}") from exc
    if not real.is_relative_to(installed_real):
        raise UnsafeInstallError(
            f"{rel.as_posix()} resolves to {real}, which is OUTSIDE {installed_real} - a reparse point in "
            "the path would carry this operation out of the plugin"
        )
    return target


def diff_tree(src: Path, dst: Path, scope: Sequence[str] | None = None) -> tuple[list[Path], list[Path]]:
    """Return (files needing copy, files present in dst but not src), as paths relative to src.

    `scope` bounds the DELETION set to the named top-level bundle directories. Without it, `extra`
    swept the whole destination, so a plugin carrying bundles of its own lost them. `scope` is the
    OWNED inventory - what the merged tree ships plus what this tool's own marker records having
    installed before - so a retired bundle is cleaned up while a stranger's is never touched.

    Both walks are `walk_files`, not `rglob`, so neither side descends into a reparse point: `scope`
    is a prefix test on the first component, and a prefix test cannot contain a path whose LATER
    components are pointers elsewhere (round-7 finding 1).
    """
    changed: list[Path] = []
    for path in sorted(walk_files(src)):
        rel = path.relative_to(src)
        target = dst / rel
        # shallow=False: compare CONTENT, not size+mtime. An install copies files with fresh
        # mtimes, so a shallow compare reports differences that do not exist - and worse, could
        # miss a same-size edit.
        if not target.exists() or not filecmp.cmp(path, target, shallow=False):
            changed.append(rel)

    extra: list[Path] = []
    if dst.is_dir():
        owned = set(scope) if scope is not None else None
        src_files = {p.relative_to(src) for p in walk_files(src)}
        extra = sorted(
            rel
            for rel in (p.relative_to(dst) for p in walk_files(dst))
            if rel not in src_files and (owned is None or (rel.parts and rel.parts[0] in owned))
        )
    return changed, extra


def local_divergence(merged_src: Path, workdir: Path) -> tuple[list[str], str | None]:
    """Paths where a build from THIS working tree would differ from the merged build.

    This is the NOTE, not the verdict. Comparing two BUILDS rather than running `git diff` over the
    merged bundle directories is what makes it complete: a path that does not exist at the merged
    commit is in no merged bundle's pathspec, so a branch-added bundle was invisible.
    """
    try:
        mine = build_reference_copy(workdir / "worktree", REPO)
    except (subprocess.CalledProcessError, SystemExit) as exc:
        return [], f"this working tree's build_plugin.py did not run, so divergence is unknown: {exc}"
    changed, extra = diff_tree(merged_src, mine)
    return sorted({f".github/skills/{rel.as_posix()}" for rel in (*changed, *extra)}), None


def worktree_banner() -> list[str]:
    """The loud header printed whenever unmerged working-tree content is being served."""
    return [
        "SYNC: !!! --from-worktree: reading UNMERGED working-tree content !!!",
        "      Every subagent spawned afterwards reads guidance that is on NO merged commit, so a",
        "      measurement taken against it is reproducible from no commit at all.",
        "      Restore the merged copy when done: python scripts/sync_installed_skills.py",
    ]


def _emit(args: argparse.Namespace, payload: dict, lines: list[str], exit_code: int) -> int:
    """Emit either the JSON verdict (preflight's input) or the human lines, and return `exit_code`."""
    if args.json:
        print(json.dumps({**payload, "exit_code": exit_code}, indent=2))
    else:
        for line in lines:
            print(line)
    return exit_code


def _discovery_failure(args: argparse.Namespace, discovery, base: dict) -> int | None:
    """Return an exit code when the installed plugin cannot be written to, else None."""
    if discovery.status == "multiple":
        return _emit(
            args,
            {**base, "status": "multiple_plugins", "candidates": [str(c) for c in discovery.candidates]},
            [
                "SYNC: ERROR - multiple installed plugins are owned by this repo",
                *(f"      {candidate}" for candidate in discovery.candidates),
                "      Remove the duplicate; otherwise one copy can silently shadow another.",
            ],
            EXIT_MULTIPLE_PLUGINS,
        )
    if discovery.status == "unproven":
        return _emit(
            args,
            {
                **base,
                "status": "unproven_plugin",
                "detail": discovery.detail,
                "candidates": [str(c) for c in discovery.candidates],
                "install_hint": discovery.install_hint,
            },
            [
                "SYNC: ERROR - ownership could not be PROVED, so nothing was written",
                *(f"      candidate: {candidate}" for candidate in discovery.candidates),
                "      A bundle name is not proof of ownership: a foreign plugin that merely shares",
                "      one was overwritten, and a file inside it deleted (#410 review finding 1).",
                f"      {discovery.install_hint}",
            ],
            EXIT_UNPROVEN_PLUGIN,
        )
    if not discovery.ok or not discovery.skills_dir:
        return _emit(
            args,
            {
                **base,
                "status": "no_plugin",
                "detail": discovery.detail,
                "install_hint": discovery.install_hint or DEFAULT_INSTALL_HINT,
            },
            [
                f"SYNC: ERROR - plugin not installed ({discovery.detail})",
                f"      {discovery.install_hint or DEFAULT_INSTALL_HINT}",
            ],
            EXIT_NO_PLUGIN,
        )
    return None


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface, kept out of `main` so the flow below reads as one decision sequence."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report drift and exit 1; change nothing")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable verdict for preflight.ps1")
    parser.add_argument("--verbose", action="store_true", help="list every file the reference build contains")
    parser.add_argument("--ref", help="merged ref to publish (default: the remote's advertised default branch)")
    parser.add_argument("--fetch", action="store_true", help="refresh remote refs first; never fatal if offline")
    parser.add_argument(
        "--from-worktree",
        action="store_true",
        help="publish THIS working tree instead of the merged ref - serves unreviewed guidance, and "
        "REQUIRES --plugin-root so a branch can never choose the destination",
    )
    parser.add_argument(
        "--plugin-root",
        type=Path,
        help=f"explicit installed plugin root; also supported via {PLUGIN_ROOT_ENV}",
    )
    parser.add_argument(
        "--installed-plugins-root",
        type=Path,
        help="override the ~/.copilot/installed-plugins tree that is scanned for the bundles",
    )
    return parser


def _worktree_needs_explicit_root(args: argparse.Namespace, env: dict | None = None) -> bool:
    """`--from-worktree` may only write where the OPERATOR pointed it.

    Review reproduced the whole finding through this flag: a bundle added on a branch entered the
    inventory, the inventory chose the destination, and an unrelated plugin was selected, overwritten
    and partially deleted. Requiring an explicit destination removes the branch's influence entirely,
    which is stronger than pinning discovery to the merged identity would have been.
    """
    environment = os.environ if env is None else env
    return args.from_worktree and not (args.plugin_root or environment.get(PLUGIN_ROOT_ENV))


def _resolve_source(args: argparse.Namespace) -> PublishSource:
    """Pick the authoritative content source, honouring --from-worktree / --ref."""
    if args.from_worktree:
        return PublishSource(
            kind="worktree",
            ref=None,
            commit=None,
            described=f"working tree at {REPO}",
            default_proof="worktree",
        )
    return resolve_publish_ref(args.ref, fetch=args.fetch)


def _source_failure(args: argparse.Namespace, exc: Exception, fetch_note: dict | None) -> int:
    """Turn a refusal to guess into a verdict preflight can read - never into a silent fallback."""
    if isinstance(exc, UnverifiedDefaultError):
        return _emit(
            args,
            {"status": "unverified_default", "detail": str(exc), "fetch": fetch_note, "default_verified": False},
            [
                f"SYNC: ERROR - {exc}",
                "      Publishing the wrong default branch is silent and reports `in_sync`, so this",
                "      refuses instead (issue #410 review finding 2).",
                "      Reconnect and re-run, or pin it: --ref refs/remotes/origin/<branch>.",
            ],
            EXIT_UNVERIFIED_DEFAULT,
        )
    return _emit(
        args,
        {"status": "no_ref", "detail": str(exc), "fetch": fetch_note, "default_verified": False},
        [
            f"SYNC: ERROR - {exc}",
            "      The installed copy must be what is MERGED, so this refuses to guess (issue #410).",
            "      Add the remote and fetch, pass --ref <ref>, or publish this checkout deliberately",
            "      with --from-worktree --plugin-root <path>, which serves UNREVIEWED guidance.",
        ],
        EXIT_NO_REF,
    )


def _plan(args: argparse.Namespace, source: PublishSource, workdir: Path, fetch_note: dict | None) -> SyncPlan:
    """Export the pinned tree, build it, and prove which installed plugin may be written to."""
    pinned = str(source.commit) if source.commit else ""
    source_root = REPO if source.from_worktree else export_ref_tree(pinned, workdir / "ref-src")
    src = build_reference_copy(workdir / "reference", source_root)
    identity = pinned_identity(source_root)

    bundles = sorted(p.name for p in src.iterdir() if p.is_dir())
    discovery = discover_skill_plugin(
        installed_plugins_root=args.installed_plugins_root,
        plugin_root_override=args.plugin_root,
        bundles=bundles,
        identities=identity.identities,
        publish_repo=identity.publish_repo,
    )
    marker = read_owner_marker(discovery.plugin_root)
    recorded = _recorded_inventory(marker, discovery.skills_dir, identity.publish_repo)
    formerly = sorted(name for name in set(recorded) - set(bundles))
    plan = SyncPlan(
        source=source,
        src=src,
        bundles=bundles,
        owned=sorted({*bundles, *formerly}),
        formerly_owned=formerly,
        discovery=discovery,
        identity=identity,
        marker_present=marker is not None,
        # ⚠️ `marker is None` counts as STALE, deliberately. Every install that predates this
        # record - i.e. the entire installed base - has no marker, and without one a retirement is
        # invisible: measured, a markerless byte-identical install exited 0, a bundle was retired,
        # and the next run exited 0 again with the retired bundle still installed. A record that
        # does not exist is not a record that agrees (round-4 finding 2).
        inventory_stale=marker is None or sorted(recorded) != bundles,
        workdir=workdir,
    )
    plan.base = {
        "source": source.kind,
        "ref": source.ref,
        "commit": source.commit,
        "described": source.described,
        "bundles": bundles,
        "owned": plan.owned,
        "formerly_owned": formerly,
        "owner_marker": "present" if marker else "absent",
        "proof": discovery.proof,
        "default_verified": source.default_verified,
        "default_proof": source.default_proof,
        "default_verified_at": source.default_verified_at,
        "default_unconfirmed": source.default_unconfirmed,
        "default_alternatives": list(source.alternatives),
        "advertised_commit": source.advertised_commit,
        "fetch": fetch_note,
    }
    return plan


def _recorded_inventory(marker: dict | None, skills_dir: Path | None, publish_repo: str) -> list[str]:
    """The bundles a previous publish recorded, or raise when acting on them would be unsafe.

    Every name here reaches a deletion. The old filter kept any `str`, which is not a safety
    property at all: `installed / "C:/stranger-data"` is that absolute path, and `installed / ".."`
    is the plugin's parent. Measured through public `sync.main` in review, a marker naming an
    absolute path deleted an unrelated directory and exited 0.

    A bad entry does not merely get skipped. A marker that lies about one name is not trustworthy
    about the others, so the whole run refuses and writes nothing.

    An UNKNOWN SCHEMA is the same question one layer out, and gets the safe answer rather than a
    guess: if some future writer changes what `bundles` means, this build cannot interpret it, so
    it acts on NOTHING (no deletions) and reports the record as unreconciled, which rewrites it at
    the schema this build does understand. Fail-closed, and self-healing.
    """
    if marker is None or skills_dir is None:
        return []
    if not marker_is_usable(marker, publish_repo=publish_repo, skills_dir=skills_dir):
        # Unusable is not the same as EMPTY, and the difference decides the exit code. A record
        # whose entries could delete the wrong thing is a refusal (exit 8, loud); one this build
        # merely cannot interpret - an unknown schema, another repo's marker - acts on nothing and
        # is rewritten by the reconcile path. Both act on NOTHING; only one is an error.
        problems = marker_bundle_problems(marker.get("bundles", []), skills_dir)
        if problems:
            raise UnsafeMarkerError("; ".join(problems))
        return []
    return [str(name) for name in marker["bundles"]]


def _notes(plan: SyncPlan, unmerged: list[str], unmerged_error: str | None) -> list[str]:
    """The informational lines: unmerged local edits, and any bundle this tool must now retire."""
    lines: list[str] = []
    if plan.source.default_unconfirmed:
        # Loud in the HUMAN output too, not only in the JSON preflight parses. A person running this
        # by hand offline gets `SYNC: IN_SYNC` and exit 0, and without this line nothing on screen
        # says the branch behind that verdict was never re-confirmed.
        lines += [
            "SYNC: CANNOT ESTABLISH - the merged default branch was not confirmed on this run:",
            f"        {plan.source.default_unconfirmed}",
            "      The content comparison above is still real; what is unverified is WHICH branch",
            "      it compared against. Reconnect and re-run, or pin it with --ref <ref>.",
        ]
    if plan.formerly_owned:
        lines += [
            f"SYNC: NOTE - {len(plan.formerly_owned)} bundle(s) this tool installed are no longer shipped "
            f"by {plan.source.described}:",
            *(f"        {name}" for name in plan.formerly_owned),
            "      They are removed, because the marker records that we put them there. Bundles this",
            "      tool never installed are left alone.",
        ]
    if unmerged:
        lines += [
            f"SYNC: NOTE - a build from this working tree would differ from {plan.source.described} in "
            f"{len(unmerged)} shipped file(s):",
            *(f"        {path}" for path in unmerged),
            "      Subagents read the MERGED copy, not these edits - deliberately (issue #410).",
            "      To test them: python scripts/sync_installed_skills.py --from-worktree --plugin-root <path>",
        ]
    elif unmerged_error:
        lines.append(f"SYNC: NOTE - {unmerged_error}")
    return lines


def _record_ownership(plan: SyncPlan) -> None:
    """Stamp the plugin with what was just installed, so a later retirement is visible."""
    write_owner_marker(
        plan.discovery.plugin_root,
        publish_repo=plan.identity.publish_repo,
        identity=plan.discovery.identity,
        bundles=plan.bundles,
        source=plan.source.kind,
        ref=plan.source.ref,
        commit=plan.source.commit,
    )


def _apply(plan: SyncPlan, changed: list[Path], extra: list[Path]) -> list[Path]:
    """Copy the reference files in, remove the OWNED files that are no longer shipped, and re-verify."""
    installed = plan.discovery.skills_dir
    # Resolve and RE-VALIDATE every target BEFORE anything is written. Three reasons, in order of
    # how they were measured: the raw recorded string must never reach the filesystem (Windows
    # aliases `FOREIGN`, `foreign.` and `foreign ` onto a real `foreign/`); a marker that became
    # unsafe between planning and applying must abort while the install is still untouched rather
    # than half-published; and a reparse point ANYWHERE in a target's path silently redirects the
    # operation out of the plugin, which no name-level rule can see (round-7 finding 1).
    installed_real = installed.resolve()
    try:
        retire = [marker_bundle_target(installed, name) for name in plan.formerly_owned]
    except ValueError as exc:
        raise UnsafeMarkerError(str(exc)) from exc
    copies = [(plan.src / rel, contained_target(installed, installed_real, rel)) for rel in changed]
    removals = [contained_target(installed, installed_real, rel) for rel in extra]
    # `shutil.rmtree` is measured NOT to follow a junction on Python 3.13.2 - but it did on older
    # interpreters, and this repo's floor is 3.11, so the tree is proved link-free rather than the
    # behaviour assumed. Scanning is cheap; a wrong answer here deletes someone else's directory.
    nested = install_reparse_points(installed, [target.name for target in retire])
    if nested:
        raise UnsafeInstallError(
            f"a bundle being RETIRED contains a reparse point ({', '.join(nested)}), and a recursive "
            "delete that follows it leaves the plugin"
        )
    for source, target in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for target in removals:
        target.unlink()
    for target in retire:
        shutil.rmtree(target, ignore_errors=True)
    # Re-diff rather than trusting the copies. The whole reason this script exists is a lock that
    # makes some filesystem operations fail, so "it did not raise" is not evidence.
    still, _ = diff_tree(plan.src, installed, scope=plan.owned)
    return still


def _report(args: argparse.Namespace, plan: SyncPlan) -> int:  # pylint: disable=too-many-locals
    """Compare, then either report drift (`--check`) or publish and verify."""
    installed = plan.discovery.skills_dir
    # BEFORE the comparison, not after: `walk_files` refuses to descend into a reparse point, so
    # without this the escaping files would simply be absent from the diff and `--check` would
    # report `in_sync` off a tree it could not fully assess - the "unassessable input lands in the
    # clean bucket" shape, arriving by way of the very fix that removed the deletion (round 7).
    escapes = install_reparse_points(installed, plan.owned)
    if escapes:
        raise UnsafeInstallError(f"{len(escapes)} reparse point(s) inside owned bundle(s): {', '.join(escapes)}")
    changed, extra = diff_tree(plan.src, installed, scope=plan.owned)
    missing = [name for name in plan.bundles if not (installed / name / "SKILL.md").is_file()]
    unmerged, unmerged_error = ([], None) if plan.source.from_worktree else local_divergence(plan.src, plan.workdir)
    note = _notes(plan, unmerged, unmerged_error)
    inventory = (
        [f"  in build: {p.relative_to(plan.src).as_posix()}" for p in sorted(walk_files(plan.src))]
        if args.verbose
        else []
    )
    payload = {
        **plan.base,
        "status": "in_sync",
        "skills_dir": str(installed),
        "identity": plan.discovery.identity,
        "plugin_root": str(plan.discovery.plugin_root) if plan.discovery.plugin_root else None,
        "changed": [rel.as_posix() for rel in changed],
        "extra": [rel.as_posix() for rel in extra],
        "missing": missing,
        "local_unmerged": unmerged,
        "local_unmerged_error": unmerged_error,
    }

    if not changed and not extra:
        # A retirement whose directory is ALREADY gone leaves nothing for the diff to see, so this
        # used to return `in_sync` with the marker untouched - and because `marker_present` was
        # true, nothing ever rewrote it. The tool then claimed that name forever, and deleted
        # whatever appeared under it next (round-3 finding 1). Marker inventory drift is WORK.
        #
        # The condition is `inventory_stale`, not `formerly_owned`, because the record can be wrong
        # in BOTH directions and the other one is the same bug displaced in time: a marker that
        # UNDER-claims (a name we installed is missing from it) cannot retire that bundle later,
        # which is round-2 finding 3 all over again. Over-claiming is the dangerous direction and is
        # named separately in the message; either way the fix is the same rewrite.
        if plan.inventory_stale:
            claimed = plan.formerly_owned
            if not plan.marker_present:
                headline = (
                    f"there is NO ownership record for this install, so a bundle {plan.source.described} "
                    "later retires could never be recognised as ours to remove"
                )
            elif claimed:
                headline = f"the marker still claims {len(claimed)} bundle(s) {plan.source.described} no longer ships"
            else:
                headline = f"the marker does not record what is installed from {plan.source.described}"
            if args.check:
                return _emit(
                    args,
                    {**payload, "status": "ownership_drift"},
                    [
                        *inventory,
                        f"SYNC: DRIFT - {headline}, and no file differs, so only the record is wrong",
                        *(f"        still claimed: {name}" for name in claimed),
                        "      Reconcile it: python scripts/sync_installed_skills.py",
                        "      Until then a NEW bundle appearing under a claimed name would be deleted",
                        "      as formerly-owned, and a bundle missing from the record could never be",
                        "      retired at all.",
                        *note,
                    ],
                    EXIT_DRIFT,
                )
            _record_ownership(plan)
            return _emit(
                args,
                {**payload, "status": "ownership_reconciled"},
                [
                    *inventory,
                    f"SYNC: RECONCILED - {headline}; the record now matches, and no file needed "
                    f"copying or removing at {installed}",
                    *note,
                ],
                EXIT_OK,
            )
        return _emit(
            args,
            payload,
            [
                *inventory,
                f"SYNC: IN_SYNC - {installed} ({plan.discovery.identity}, proof: {plan.discovery.proof}) "
                f"from {plan.source.described}",
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
                f"SYNC: DRIFT - {len(changed)} file(s) differ, {len(extra)} stale, vs {plan.source.described}",
                "      Publish the merged copy: python scripts/sync_installed_skills.py",
                "      (If you published with --from-worktree, that same command restores it.)",
                *note,
            ],
            EXIT_DRIFT,
        )

    still = _apply(plan, changed, extra)
    if still:
        return _emit(
            args,
            {**payload, "status": "copy_failed", "still_differ": [r.as_posix() for r in still]},
            [f"SYNC: ERROR - {len(still)} file(s) still differ after copying"],
            EXIT_COPY_FAILED,
        )
    _record_ownership(plan)
    return _emit(
        args,
        {**payload, "status": "updated"},
        [
            *drift_lines,
            f"SYNC: UPDATED - {len(changed)} file(s) copied, {len(extra)} removed at {installed} "
            f"({plan.discovery.identity}, proof: {plan.discovery.proof}) from {plan.source.described}",
            "      Skills are snapshotted at session start, so a RUNNING session keeps the old",
            "      copy in memory. New sessions (and subagents they spawn) get this one.",
            *note,
        ],
        EXIT_OK,
    )


def main(argv: list[str] | None = None) -> int:
    """Sync the installed bundles from the merged ref, or report drift under --check."""
    args = build_parser().parse_args(argv)
    if _worktree_needs_explicit_root(args):
        return _emit(
            args,
            {"status": "worktree_needs_explicit_root", "default_verified": False},
            [
                "SYNC: ERROR - --from-worktree requires --plugin-root (or " + PLUGIN_ROOT_ENV + ")",
                "      Unmerged content must never also choose its own destination: review measured",
                "      a branch-invented bundle name selecting an UNRELATED plugin, overwriting it",
                "      and deleting a file inside it (#410 review finding 1).",
            ],
            EXIT_UNPROVEN_PLUGIN,
        )

    # The fetch happens before anything else, so its outcome survives into the failure payload too -
    # and so its human line can be suppressed under --json, which preflight PARSES: a single stray
    # line ahead of the JSON reads to preflight as "did not report", i.e. an unverified bundle.
    fetch_note: dict | None = None
    if args.fetch and not args.from_worktree:
        ok, detail = fetch_origin()
        fetch_note = {"ok": ok, "detail": detail}
        if not args.json:
            print(f"SYNC: fetch origin - {'ok' if ok else 'FAILED, using the local ref: ' + detail}")

    try:
        source = _resolve_source(args)
    except (PublishRefError, UnverifiedDefaultError) as exc:
        return _source_failure(args, exc, fetch_note)

    if not args.json and source.from_worktree:
        for line in worktree_banner():
            print(line)

    build_dir = REPO / "_build"
    build_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="skill-plugin-", dir=build_dir))
    try:
        try:
            plan = _plan(args, source, workdir, fetch_note)
            failed = _discovery_failure(args, plan.discovery, plan.base)
            return failed if failed is not None else _report(args, plan)
        except UnsafeMarkerError as exc:
            return _emit(
                args,
                {"status": "unsafe_marker", "detail": str(exc), "default_verified": source.default_verified},
                [
                    "SYNC: ERROR - the ownership marker names a deletion target this run cannot",
                    "      prove it owns, so NOTHING was removed.",
                    f"      {exc}",
                    "      Every recorded bundle must be a single directory name that spells an",
                    "      existing child of the plugin's skills/ folder EXACTLY - Windows aliases",
                    "      `FOREIGN`, `foreign.` and `foreign ` onto a real `foreign/`.",
                    f"      Delete or repair {OWNER_MARKER_NAME} in the plugin root, then re-run.",
                ],
                EXIT_UNSAFE_MARKER,
            )
        except UnsafeInstallError as exc:
            return _emit(
                args,
                {"status": "unsafe_install", "detail": str(exc), "default_verified": source.default_verified},
                [
                    "SYNC: ERROR - a reparse point (junction or symlink) sits inside a bundle this",
                    "      tool owns, so NOTHING was copied or removed.",
                    f"      {exc}",
                    "      Every write and every delete here is `<skills>/<owned bundle>/...`, and a",
                    "      junction ANYWHERE along that path silently redirects it out of the plugin:",
                    "      measured, `unlink()` deleted an external file and `copy2` overwrote an",
                    "      external one, both while the run reported `updated` and exited 0.",
                    "      Remove the link (`rmdir <path>` for a junction) and re-run; if the content",
                    "      really belongs in the bundle, copy it in rather than pointing at it.",
                ],
                EXIT_UNSAFE_INSTALL,
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
