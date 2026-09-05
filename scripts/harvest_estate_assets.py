"""
purpose: download every workbook and published datasource on a Tableau site, then run BOTH tiers'
         parsers over them, to get a failure distribution the estate can be reasoned about — and
         from which upstream feature requests can be written with evidence instead of anecdote.
usage:   python scripts/harvest_estate_assets.py --out <dir> [--env .env] [--limit N]
                                                 [--skip-download] [--workbooks-only]
                                                 [--project NAME] [--project-id LUID]
                                                 [--allow-unignored-out]

Exit codes
----------
`0` every in-scope asset reached BOTH parsers (a parse FAILURE is the report this script exists to
produce, so it stays `0`); `1` NOTHING COULD BE ASSESSED — no asset reached a parser at all; `2`
refused to write into a committable `--out`; `3` PARTIAL — some assets were assessed and at least
one never downloaded. `1` and `3` are kept apart on purpose: automation must be able to tell "six of
forty-seven failed" from "the whole harvest failed".

Where the output goes
---------------------
`--out` is `_sweep` by convention (`.gitignore`: `/_sweep*/`; `_harvest*` belongs to the OTHER
harvester, `harvest_tableau_public.py`). Anything under `--out` is a real customer's workbooks and
their names, and THIS REPO IS PUBLIC, so when the target sits inside a git work tree this script
refuses to start unless git already ignores it -- see `unignored_output_paths` below. A target
outside any work tree is fine and runs unguarded.

What the guard does and does not cover (issue #374). It judges BOTH the path git's own working-tree
walk would see (`--out` made absolute, nothing expanded or followed) AND the path the bytes actually
land on (`~` expanded, every junction/symlink followed), refuses if EITHER is committable, and the
run then writes to the second one -- so the path that was checked is the path that is written. It
does NOT detect an `--out` that names a junction's TARGET directly: the write lands outside the
checkout, but a junction elsewhere in the checkout still exposes it to `git add -A`. Finding that
needs reparse-point enumeration of the whole checkout and is deliberately out of scope.

Why this exists
---------------
Both tiers are normally exercised on whatever workbook is in front of us, which selects for the
shapes we already know. An estate-wide pass selects for nothing, so it finds the shapes nobody
thought to try — and it is cheap: parsing is offline, needs no Power BI Desktop, no Fabric capacity
and no data-source credential (a LIVE connection is only contacted at refresh, never at parse).

It runs BOTH parsers on purpose. They answer different questions and their disagreements are the
interesting part:

* ours (`parse_tableau.py` -> `migration-spec.json`) is the FIDELITY spec — mark types, encodings,
  shelves, palettes: what the viz meant and looked like;
* his (`connection_to_m.describe_datasource`) is the CONVERSION descriptor — relations, columns,
  connection routing: what can be rebuilt.

A workbook one parses and the other refuses is a finding by construction, and which way round it
fails says which tier owns it (see `docs/migration-programme.md` §0).

⚠️ Downloads are the session-fragile part. Tableau Cloud drops a session intermittently and the
failure is a `401002` mid-loop, so each asset is fetched with its OWN sign-in rather than a shared
token: measured on this site, a shared token truncated a 58-asset run repeatedly while
fresh-per-asset completed 8/8. Slower, and the only thing that finishes.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Windows defaults stdout/stderr to the legacy cp1252 codec, which cannot encode the non-ASCII
# characters (e.g. the warning glyph above) in this module's own docstring -- argparse's --help
# crashes with UnicodeEncodeError before printing anything. Force UTF-8 so --help and any print()
# of the same characters work the same on every platform. This runs BEFORE the import below so a
# failure there is reportable rather than itself crashing the encoder.
for _stream in (sys.stdout, sys.stderr):
    # pylint: disable-next=no-member  # astroid mis-infers TextIOWrapper.encoding as a class here
    if _stream is not None and _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

from engine_source import EngineNotFoundError, engine_scripts_dir  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_env import engine_child_env, pat_secret, redact, require, resolve_env  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("harvest_estate_assets")

# --- exit codes: "could not assess" must never collapse into the clean bucket --------------------
#
# The final line used to be `return 0 if results else 1`, which counts ROWS -- and a failed download
# appends a row. So a harvest in which NOTHING reached either parser exited 0. Measured on a
# one-workbook estate with `download()` returning `(False, "network dead")`:
#
#   **1 asset(s)** - ours failed 0, his failed 0, both parsed 0, never downloaded 1.
#   exit_code: 0
#
# That is the fail-open shape AGENTS.md singles out: automation downstream cannot tell a total
# harvest failure from a clean sweep, and the customer's 47-asset run (41 ok / 6 failed) reported
# "ours failed 0, his failed 0" for the same reason (issue #472, blind review of #482).
#
# The contract. It is deliberately ADDITIVE: no existing code changes meaning, because a code that
# already means something is read by things this branch cannot see -- measured, `1` is asserted for
# the unreadable-`estate.db` path by `tests/test_harvest_output_guard.py`, a file this change does
# not own. So the bug is fixed by moving TOTAL failure out of `0` into the `1` it always documented,
# and the previously-invisible PARTIAL case gets a NEW number.
#
#   0  every in-scope asset reached BOTH parsers. A parse FAILURE stays 0: the failure distribution
#      is the artifact this script exists to produce, not a run failure.
#   1  NOTHING COULD BE ASSESSED -- no asset reached a parser at all: every download failed (the new
#      case), no asset was in scope, the engine is missing, or `estate.db` could not be read. This is
#      what `main()` has documented since it was written; only the first case is new.
#   2  refused to write into a committable `--out` (unchanged; nothing was downloaded).
#   3  PARTIAL -- at least one asset was assessed AND at least one never downloaded (the customer's
#      41-ok/6-failed shape). Non-zero because those six are absent from every bucket in the report
#      and an operator reading the tally alone would call the run clean. A new number rather than a
#      reuse of `1`: "some of the estate is missing" and "the estate was never looked at" need
#      opposite responses, and 3 is the repo's established slot for a verdict kept apart from 0/1
#      (`check_datamodel.EXIT_UNASSESSABLE`, `assess_estate.py`'s incomplete-primary-listing code).
EXIT_OK = 0
EXIT_NOTHING_ASSESSED = 1
EXIT_REFUSED_UNIGNORED_OUT = 2
EXIT_PARTIAL = 3

# The download watchdog (below) pushes this module past pylint's 1200-line limit. The precedent in
# this repo is a module-level waiver (`assess_estate.py`, `run_estate.py`, `deploy_estate.py` and
# three others carry the same one) rather than a cosmetic split; the watchdog is nonetheless a
# genuine seam and would be the thing to lift out if this file grows again.
# pylint: disable=too-many-lines

# The FILES this script writes under `--out`, probed as files on purpose. Two reasons:
#   * a directory-only rule (`/_sweep*/`, or a hand-written `_myout/assets/`) is applied by
#     `git check-ignore` only to a path it knows is a directory, and a not-yet-created `--out` is
#     not - so probing `assets` as a bare name reports a false "not ignored" for exactly the rule
#     shape people write. A file path underneath makes every parent a directory by construction.
#   * it is the honest question: these are the paths a `git add -A` would stage.
# `assets/` holds the downloaded workbooks/datasources; the sweep files record every asset's NAME
# and LUID even under `--skip-download`, so all of them are checked, not just the downloads. Both
# the PACKAGED (`.twbx`/`.tdsx`) and PLAIN (`.twb`/`.tds`) extensions are represented: the production
# fetcher writes plain XML whenever the REST download is not a zip (`existing_asset()`'s docstring
# measured this at 18/38 workbooks on a real harvest), so a guard that only knows the packaged
# extension would miss most of what actually lands on disk (review of the #526 follow-up).
OUTPUT_ARTIFACTS = (
    "assets/harvested-workbook.twbx",
    "assets/harvested-datasource.tdsx",
    "assets/harvested-workbook.twb",
    "assets/harvested-datasource.tds",
    "parse-sweep.json",
    "parse-sweep.md",
    "parse-sweep-totals.json",
)

# The remedy sentence, kept separate from the diagnosis so another tool can reuse the guard without
# advertising THIS tool's ignored folder convention. `provision_tableau_estate.py capture` writes a
# manifest naming every project/workbook/datasource on a live site plus the downloads themselves, so
# it needs the same refusal - and an operator told to `--out _sweep` there would be misdirected.
DEFAULT_UNIGNORED_HINT = (
    "Fix: use the ignored convention `--out _sweep` (any `_sweep*` variant works, e.g. "
    "`_sweep-2026-08-13`), point --out outside the checkout, or add a rule to .gitignore."
)


class OutputPathNotIgnoredError(RuntimeError):
    """`--out` is inside a git work tree that would commit it, or git cannot prove otherwise."""


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | None:
    """Run git in `cwd`. Returns None when git itself could not be run at all."""
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _lexical(path: Path) -> Path:
    """`path` made absolute with `.`/`..` collapsed TEXTUALLY, following no junction or symlink.

    `Path.absolute()` keeps `..` verbatim (measured on Python 3.11.9 and 3.13.2, Windows), and a
    surviving `..` then rides into `git check-ignore`, which answers about a directory the operator
    never named: `<repo>\\..\\outside\\out` still passes a `relative_to(<repo>)` test, so the
    documented "point --out outside the checkout" remedy was refused in its most natural relative
    spelling. `os.path.abspath` collapses it the way git itself does - and only textually, because
    `resolve()` here would dereference the junction and re-open the hole below.
    """
    return Path(os.path.abspath(path))


def _resolved(path: Path) -> Path:
    """`path` with `~` expanded and every junction/symlink followed. Isolated so the guard has one
    place to fail closed when a path cannot be resolved at all."""
    return path.expanduser().resolve()


def _same_dir(a: Path, b: Path) -> bool | None:
    """Do these two spellings name the SAME directory on disk? Never a string comparison.

    Three-valued on purpose. `None` means the FILESYSTEM could not answer - a permission failure, a
    disconnected share - which is not the same as "no", and collapsing it to `False` is how an
    unignored in-repository output was allowed through: with every comparison erroring, containment
    looked disproven rather than unproven and the guard proceeded (measured: `main()` exit 0, a
    customer workbook name written to `parse-sweep.json`, `git status` staging it).
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return None


def _identity_relative(node: Path, root: Path) -> tuple[list[str] | None, bool]:
    """`(segments from root down to node, every comparison answered)` - by FILE IDENTITY, not text.

    The second element is what separates "walked the whole ancestry and this really is not inside
    `root`" from "could not tell". Only the first is safe to act on.

    Windows can spell one directory several ways that are not lexically relative to each other, and
    both `absolute()` and `resolve()` preserve every one of them, so comparing `--out` against
    `git rev-parse --show-toplevel` as TEXT let three spellings write into the checkout unrefused
    while the plain spelling of the same target was correctly refused:

        \\\\?\\<repo>\\leak                     extended-length prefix
        \\\\localhost\\c$\\...\\<repo>\\leak       UNC admin-share alias
        <8.3 repo>\\link-out\\leak             8.3 short name PLUS an outbound junction

    The third defeats the obvious fix: 8.3 alone is expanded by `resolve()` and caught, but combined
    with a junction the lexical form stays short and the resolved form lands outside, so BOTH forms
    pass. `os.path.samefile` answers the question the spellings obscure.
    """
    segments: list[str] = []
    current = node
    answered = True
    while True:
        same = _same_dir(current, root)
        if same is None:
            answered = False
        elif same:
            return list(reversed(segments)), answered
        if current.parent == current:
            return None, answered
        segments.append(current.name)
        current = current.parent


def _canonical_probe_target(out: Path) -> tuple[Path, Path] | None:
    """`(work tree root, out re-spelled beneath it)`, or None when no work tree holds `out`.

    Walks the LEXICAL ancestors, because an existing ancestor can be reached through a junction and
    `git rev-parse` run there answers about somewhere else entirely (issue #374). Measured on Windows
    with `mklink /J <repo>\\linkdir <outside>`: `rev-parse` with cwd=`<repo>\\linkdir` exits 128 "not
    a git repository" -- the OS resolved the junction for the child process's cwd -- so a guard
    anchored there concludes "outside any work tree" and passes, while `git add -A` at `<repo>`
    stages `linkdir/sweepout/...`. Asked from `<repo>` the same git answers correctly.

    Containment is then decided by `_identity_relative`, never by path spelling, and the probe is
    rebuilt from the ROOT's own spelling so git is asked about a path it can actually see. That
    rebuild is load-bearing: with `subst Z: <repo>`, `rev-parse` at `Z:\\` reports the long
    `C:\\...` toplevel and `check-ignore` answers the canonical path (exit 0) while refusing the
    `Z:\\` spelling outright (exit 128) - so probing what the caller typed would refuse a plainly
    ignored output.

    EVERY unanswerable probe here is a refusal, not a shrug. Three of them were fail-open:

    * a `.git` entry found with `exists()`, which FOLLOWS a reparse point - a dangling `.git`
      junction reported absent while `rev-parse` failed, so the broken checkout was skipped and an
      unignored output was written and staged. `os.path.lexists` asks about the ENTRY.
    * an identity comparison that errored, collapsed to "not contained" (see `_same_dir`).
    * an ancestry with no examinable directory at all - a nonexistent drive, a disconnected share -
      where nothing could be looked at and the run proceeded anyway.
    """
    lexical = _lexical(out)
    tail: list[str] = []
    node = lexical
    examined = False
    while True:
        if node.is_dir():
            examined = True
            probe = _git(["rev-parse", "--show-toplevel"], node)
            root = (
                Path(probe.stdout.strip())
                if probe is not None and probe.returncode == 0 and probe.stdout.strip()
                else None
            )
            if root is None:
                if os.path.lexists(node / ".git"):
                    detail = "git could not be run" if probe is None else (probe.stderr.strip() or "no work tree")
                    raise OutputPathNotIgnoredError(
                        f"{node} holds a .git entry but git could not identify a work tree there "
                        f"({detail}), so {lexical} cannot be proven ignored"
                    )
            else:
                relative, answered = _identity_relative(node, root)
                if relative is not None:
                    return root, root.joinpath(*relative, *reversed(tail))
                if not answered:
                    raise OutputPathNotIgnoredError(
                        f"the filesystem could not say whether {node} lies inside the work tree at "
                        f"{root}, so {lexical} cannot be proven ignored"
                    )
        if node.parent == node:
            break
        tail.append(node.name)
        node = node.parent
    if not examined:
        raise OutputPathNotIgnoredError(
            f"no directory in the ancestry of {lexical} could be examined, so it cannot be proven ignored"
        )
    return None


def unignored_output_paths(out: Path, artifacts: Sequence[str] = OUTPUT_ARTIFACTS) -> list[Path]:
    """Which artifacts under `out` git would offer to commit. Empty list means safe to write.

    `out` is judged EXACTLY as given, beyond collapsing `.`/`..`: the caller decides which form of
    the path to ask about, because the forms disagree and both matter - see `refuse_unignored_output`,
    which asks about all of them. The returned paths are the CANONICAL spelling git was asked about,
    which is the one that says where the bytes would actually be staged from.

    Two measured details decide the probe itself, and getting either wrong yields a guard that
    silently always passes - worse than no guard, because it also reassures:

    * **Ask about paths INSIDE `out`, never `out` itself.** `/_sweep*/` is a directory-only pattern,
      and `git check-ignore` applies such a pattern only to a path it knows is a directory - which,
      for an `--out` that does not exist yet, it does not (measured: `_assessment-x` -> exit 1,
      `_assessment-x/assets/f.twbx` -> exit 0, same rule, same repo). A trailing component makes the
      parent a directory by construction, so the rule applies without creating anything on disk.
    * **NEVER append a trailing slash to work around that.** On git 2.55.0.windows.3,
      `git check-ignore -- 'zzz_not_ignored/'` exits 0 reporting an EMPTY matched pattern: with a
      trailing slash EVERY path looks ignored.

    Raises `OutputPathNotIgnoredError` when git is present but cannot answer, is absent while a
    `.git` checkout is in scope, or finds a `.git` it cannot read: an unprovable path is treated as
    unsafe, never as safe.
    """
    found = _canonical_probe_target(out)
    if found is None:
        return []  # outside any work tree: nothing here can be committed by accident
    root, base = found

    unignored: list[Path] = []
    for artifact in artifacts:
        target = base / artifact
        probe = _git(["check-ignore", "-q", "--", str(target)], root)
        # 0 = ignored, 1 = not ignored, anything else (128, or no git) = no answer, so do not guess.
        if probe is None or probe.returncode not in (0, 1):
            detail = "git could not be run" if probe is None else (probe.stderr.strip() or f"exit {probe.returncode}")
            raise OutputPathNotIgnoredError(f"could not ask git whether {target} is ignored: {detail}")
        if probe.returncode == 1:
            unignored.append(target)
    return unignored


def output_path_forms(out: Path) -> list[Path]:
    """Every form of `--out` that must pass before customer content is written (issue #374).

    A guard that judges one form while the writer uses another proves nothing. Both of these are
    real, and each catches what the other cannot:

    * **lexical** - absolute, `.`/`..` collapsed, nothing dereferenced. This is the string git's own
      working-tree walk sees, so it is the honest answer to "would `git add -A` stage this?" for an
      `--out` that traverses a junction OUT of the checkout, and for a literal `~` (measured: from
      cmd.exe, a quoted PowerShell argument, or any programmatic call, `~` reaches argv unexpanded,
      and its absolute form is `<cwd>/~/x` - inside the checkout, unignored).
    * **resolved** - `~` expanded and every junction/symlink followed. This is where the bytes land,
      and it is what catches an `--out` that looks external but is a junction pointing INTO the
      checkout (measured: already refused before this function existed, precisely because the guard
      resolved - which is why the resolved form is kept rather than replaced).

    An unresolvable path raises rather than degrading to the lexical form alone: a form we cannot
    compute is a question we cannot answer, and unanswerable means unsafe.
    """
    lexical = _lexical(out)
    try:
        resolved = _resolved(out)
    except (OSError, RuntimeError, ValueError) as exc:
        raise OutputPathNotIgnoredError(f"cannot resolve {out}, so it cannot be proven ignored: {exc}") from exc
    return list(dict.fromkeys((lexical, resolved)))


def refuse_unignored_output(
    out: Path,
    allow_unignored: bool,
    *,
    artifacts: Sequence[str] = OUTPUT_ARTIFACTS,
    hint: str = DEFAULT_UNIGNORED_HINT,
) -> bool:
    """True when the run must STOP before downloading anything. Logs the reason either way.

    Takes `--out` AS THE OPERATOR GAVE IT, not a pre-normalised copy: normalising before the call
    discards the very form that catches a literal `~` (issue #374). Every form from
    `output_path_forms` must pass; the caller then writes to the resolved one.

    `artifacts` and `hint` exist so a second tool that downloads customer content can reuse this one
    implementation rather than growing a near-copy that drifts. Pass the FILES that tool writes: the
    probe must name a file, never a bare directory (see `unignored_output_paths`).
    """
    try:
        unignored = list(
            dict.fromkeys(path for form in output_path_forms(out) for path in unignored_output_paths(form, artifacts))
        )
    except OutputPathNotIgnoredError as exc:
        message = str(exc)
    else:
        if not unignored:
            return False
        message = (
            f"git does not ignore {', '.join(str(p) for p in unignored)}. This run downloads a real "
            "site's .twbx/.tdsx and records every workbook name, and this repo is PUBLIC, so a "
            f"`git add -A` would stage customer content (issue #125). {hint}"
        )
    if allow_unignored:
        LOG.warning("--allow-unignored-out: proceeding anyway, but %s", message)
        return False
    LOG.error("REFUSING to write customer content into %s: %s", out, message)
    LOG.error("Nothing was downloaded. Pass --allow-unignored-out to override this deliberately.")
    return True


# --- download watchdog: telling a STALLED download from a merely SLOW one -----------------------
#
# There are TWO nested timeouts and they catch opposite failures. The engine's `fetch_tds.py` passes
# `timeout=300` to `urlopen` (`:405`, `:443`), which is a PER-SOCKET-READ timeout: it fires when a
# connection goes quiet. Ours was `subprocess.run(..., timeout=600)`, a TOTAL WALL CLOCK: it fires on
# a download that is progressing perfectly well and merely large. A customer's 47-asset harvest lost
# `IA Redemptions by Campaign Report` to the second one twice (issue #472) — very likely a healthy
# download we killed. Raising 600 to 1200 only moves that cliff, so the ceiling is not the fix:
# knowing whether bytes are still arriving is. The ceiling survives ONLY as the fallback for when
# that knowledge is unavailable, and it is now derived from the child's own retry budget rather than
# from history — see `DEFAULT_DOWNLOAD_TIMEOUT` below.
#
# ⚠️ A FILE-GROWTH watchdog cannot work here, and this is a property of the engine, not a guess.
# `_http` ends `return resp.status, dict(resp.headers), resp.read()` — ONE unbounded read, the whole
# asset buffered in RAM — and `save_outputs(raw, ...)` writes only afterwards. The destination file
# is therefore 0 bytes until the download has already finished.
#
# ⚠️ `urlopen` is never written here immediately followed by an open paren, and that is deliberate.
# The credential-handling detector in `tests/test_diagnostic_redaction.py` scans RAW SOURCE —
# comments included — and that one literal is enough to reclassify this module as a credentialed
# HTTP client. It is not one: it spawns the engine's fetcher, which makes the request (measured —
# the module contains no HTTP call at all in code). Spelling it the natural way turned CI red on
# #482, and `test_this_module_still_makes_no_http_call_of_its_own` now pins both halves.
#
# What DOES work, measured 2026-09-03 on Windows against a local slow-stream server (a child running
# the same one-shot `urlopen` + `.read()` shape), 15 MB at ~1 MB/s versus a socket that goes quiet
# after 3 chunks:
#
#   sampled                                   healthy download        stalled download
#   Popen.pid `OtherTransferCount`            Δ 0 (frozen)            Δ 0 (frozen)
#   real interpreter `ReadTransferCount`      Δ 0 after startup       Δ 0
#   real interpreter `OtherTransferCount`     moves every second      frozen for 19s
#   `PagefileUsage`                           frozen after startup    frozen
#
# Three things that table settles, each of which would have produced a WRONG fix on its own:
#
#  1. Socket bytes land in Windows' OTHER I/O bucket, not Read, and they are counted as per-operation
#     overhead (~160 B/s observed while ~1 MB/s flowed). So this is a LIVENESS signal, never a byte
#     count — do not report it as "bytes downloaded".
#  2. The RAM-footprint half of the hypothesis is UNSOUND: `PagefileUsage` did not move while 15 MB
#     was buffered. Committed memory is taken in large chunks up front, so it is not a per-byte
#     signal. Measured, not assumed.
#  3. **`Popen.pid` is the WRONG PROCESS under a uv venv.** `.venv/Scripts/python.exe` is a
#     trampoline: measured `Popen.pid = 35152` while the child's own `os.getpid()` reported `20856`.
#     Sampling the handle Popen hands you measures a stub whose counters never move, so the naive
#     design would have declared EVERY download stalled and killed all of them. The probe therefore
#     sums the whole process SUBTREE.
#
# And because a signal that can be wrong must fail in the safe direction: a flatline is only ever
# acted on after movement has been observed AT LEAST ONCE for this child. No movement ever seen
# (unsupported platform, denied handle, trampoline we could not walk) means no stall verdict — the
# wall-clock ceiling applies instead, i.e. exactly today's behaviour.

# The engine's own per-socket-read timeout (`fetch_tds.py:405,443` — `_http_download(url, token,
# dest, timeout=300, ...)` passes it to `urlopen`). It is the contract for how long a HEALTHY
# transfer is allowed to go quiet between reads: urllib tolerates a 299s gap and raises at 300s.
ENGINE_READ_TIMEOUT_SECONDS = 300.0
# ⚠️ The blind fallback ceiling is DERIVED from the child's own retry budget, never chosen. 600 was
# inherited from the pre-#472 `subprocess.run(..., timeout=600)` and is not a number about anything:
# it is BELOW the child's bounded timer, so on any run where the progress probe is unreadable
# (unsupported platform, denied handle, a subtree we cannot walk) we killed the fetcher before it
# could produce its own authoritative verdict, and recorded `timeout after 600s` where the child
# would have reported the real HTTP failure. That is precisely the "do not kill a tool that IS the
# bounded timer" rule in AGENTS.md (blind review of #482).
#
# The arithmetic, read off installed engine 2.356.0's `fetch_tds.py` so the next person can
# re-derive it (`python scripts/engine_source.py` locates the tree):
#
#   `_http_download(url, token, dest, timeout=300, *, max_attempts=4, ...)`  (`:405`)
#     * up to `max_attempts` = 4 attempts, each of which may burn a full `timeout` = 300s read
#       timeout before `urlopen` raises                                         -> 4 x 300 = 1200s
#     * `sleeper(_retry_after_seconds(resp_headers, delay))` between attempts (`:487`). The
#       unheaded default backoff is only 1+2+4 = 7s, but `_retry_after_seconds` honours a server
#       `Retry-After` and clamps it to 60s (`:317-328`), so 60s is the worst case per gap, and
#       there are `max_attempts - 1` gaps                                       ->  3 x  60 =  180s
#     * one sign-in POST before the download (`_http_json(..., timeout=120)`)   ->            120s
#     * interpreter start-up for the child                                      ->             30s
#
# `fetch_tds.main()` never overrides the 300 (it calls `download_workbook`/`download_datasource`
# with their default `timeout=300`), so 300 is what actually runs, not merely what is available.
ENGINE_DOWNLOAD_ATTEMPTS = 4
ENGINE_BACKOFF_CAP_SECONDS = 60.0
ENGINE_SIGNIN_TIMEOUT_SECONDS = 120.0
CHILD_STARTUP_GRACE_SECONDS = 30.0
# What the child may legitimately spend before it produces its OWN verdict. Our ceiling must never
# sit below this, or we pre-empt the bounded timer and report `timeout after Ns` in place of the
# real HTTP failure the fetcher was about to name.
ENGINE_DOWNLOAD_BUDGET_SECONDS = (
    ENGINE_DOWNLOAD_ATTEMPTS * ENGINE_READ_TIMEOUT_SECONDS + (ENGINE_DOWNLOAD_ATTEMPTS - 1) * ENGINE_BACKOFF_CAP_SECONDS
)  # 1200 + 180 = 1380s
DEFAULT_DOWNLOAD_TIMEOUT = ENGINE_DOWNLOAD_BUDGET_SECONDS + ENGINE_SIGNIN_TIMEOUT_SECONDS + CHILD_STARTUP_GRACE_SECONDS
# = 1530s
# ⚠️ This MUST NOT sit below `ENGINE_READ_TIMEOUT_SECONDS`. The first version of this fix defaulted
# to 120s, by analogy with `refresh_pbip_model.py`'s liveness timer -- but that one WARNS and this
# one KILLS, which is a different thing entirely. A bursty-but-healthy source pausing 120-300s
# between chunks satisfies urllib and was killed by us and reported as hung: the wall-clock cliff
# this whole change exists to remove had become a shorter inactivity cliff (blind review of #482,
# reproduced: `BURSTY_HEALTHY stalled 0.49`). 300s + a 120s grace, so the child's own read timeout
# always fires first and reports the real error rather than being pre-empted by ours.
DEFAULT_STALL_TIMEOUT = ENGINE_READ_TIMEOUT_SECONDS + 120.0
PROGRESS_POLL_SECONDS = 2.0
# "NEVER block silently on an external system" (AGENTS.md): anything past a minute reports elapsed
# time, so a stall is visible as a stall rather than looking like work.
HEARTBEAT_SECONDS = 60.0

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
TH32CS_SNAPPROCESS = 0x2

_HANDLE = ctypes.c_void_p
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32


class IoCounters(ctypes.Structure):  # pylint: disable=too-few-public-methods
    """`IO_COUNTERS` — cumulative per-process I/O, as `GetProcessIoCounters` fills it in."""

    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class ProcessEntry32(ctypes.Structure):  # pylint: disable=too-few-public-methods
    """`PROCESSENTRY32` — only `th32ProcessID`/`th32ParentProcessID` are read, but the layout must
    be complete or `Process32First` fills the wrong offsets."""

    _fields_ = [
        ("dwSize", _DWORD),
        ("cntUsage", _DWORD),
        ("th32ProcessID", _DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", _DWORD),
        ("cntThreads", _DWORD),
        ("th32ParentProcessID", _DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", _DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


_KERNEL32: Any = None


def kernel32() -> Any:
    """`kernel32` with `argtypes` SET, or None off Windows.

    ⚠️ The `argtypes` are not decoration. Without them ctypes passes a 64-bit `HANDLE` as `c_int`,
    and the failure is silent-looking rather than loud: measured, `GetProcessIoCounters` returned 0
    for `GetCurrentProcess()`'s `-1` pseudo-handle while a small real handle "succeeded" with values
    that never moved — which reads exactly like a stalled download.
    """
    global _KERNEL32  # pylint: disable=global-statement
    if _KERNEL32 is None:
        if sys.platform != "win32":
            return None
        lib = ctypes.windll.kernel32  # pylint: disable=no-member  # Windows-only, guarded above
        lib.OpenProcess.argtypes = [_DWORD, _BOOL, _DWORD]
        lib.OpenProcess.restype = _HANDLE
        lib.CloseHandle.argtypes = [_HANDLE]
        lib.CloseHandle.restype = _BOOL
        lib.TerminateProcess.argtypes = [_HANDLE, ctypes.c_uint32]
        lib.TerminateProcess.restype = _BOOL
        lib.GetProcessIoCounters.argtypes = [_HANDLE, ctypes.POINTER(IoCounters)]
        lib.GetProcessIoCounters.restype = _BOOL
        lib.CreateToolhelp32Snapshot.argtypes = [_DWORD, _DWORD]
        lib.CreateToolhelp32Snapshot.restype = _HANDLE
        lib.Process32First.argtypes = [_HANDLE, ctypes.POINTER(ProcessEntry32)]
        lib.Process32First.restype = _BOOL
        lib.Process32Next.argtypes = [_HANDLE, ctypes.POINTER(ProcessEntry32)]
        lib.Process32Next.restype = _BOOL
        _KERNEL32 = lib
    return _KERNEL32


def windows_descendants(pid: int) -> list[int]:
    """Every descendant PID of `pid`, walked from one process snapshot. Empty off Windows."""
    lib = kernel32()
    if lib is None:
        return []
    snapshot = lib.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == -1:
        return []
    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(ProcessEntry32)  # pylint: disable=invalid-name,attribute-defined-outside-init
    by_parent: dict[int, list[int]] = {}
    try:
        found = lib.Process32First(snapshot, ctypes.byref(entry))
        while found:
            by_parent.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            found = lib.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        lib.CloseHandle(snapshot)
    seen: list[int] = []
    stack = [pid]
    while stack:
        for child in by_parent.get(stack.pop(), []):
            if child not in seen and child != pid:
                seen.append(child)
                stack.append(child)
    return seen


def process_tree(pid: int) -> list[int]:
    """`pid` and its descendants. The descendants are the point: see the trampoline note above."""
    if sys.platform == "win32":
        return [pid, *windows_descendants(pid)]
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").glob("*/stat") if Path("/proc").is_dir() else []:
        try:
            fields = entry.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[-1].split()
            children.setdefault(int(fields[1]), []).append(int(entry.parent.name))
        except (OSError, ValueError, IndexError):
            continue
    seen: list[int] = [pid]
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen:
                seen.append(child)
                stack.append(child)
    return seen


def transferred_bytes(pid: int) -> int | None:
    """A monotonic-ish I/O counter summed over `pid`'s SUBTREE, or None when unobtainable.

    LIVENESS, not volume: on Windows the socket payload is not what moves this (see the measurement
    table above), so compare it with its own previous value and never report it as bytes downloaded.

    ⚠️ None means "this platform/process cannot tell us", which is a DIFFERENT answer from 0 and must
    never be treated as a stall. It is returned for a PARTIAL reading too, not only for a total
    failure: if the descendant carrying the network I/O becomes unreadable while the trampoline
    stays readable, the sum silently flatlines at the trampoline's constant, which is exactly a
    stalled download's signature. A sum we cannot vouch for is not a smaller sum; it is no answer.
    """
    if sys.platform == "win32":
        lib = kernel32()
        if lib is None:  # pragma: no cover - unreachable while sys.platform is win32
            return None
        total: int | None = None
        for target in process_tree(pid):
            handle = lib.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, target)
            if not handle:
                return None
            counters = IoCounters()
            ok = lib.GetProcessIoCounters(handle, ctypes.byref(counters))
            lib.CloseHandle(handle)
            if not ok:
                return None
            total = (total or 0) + int(
                counters.ReadTransferCount + counters.WriteTransferCount + counters.OtherTransferCount
            )
        return total
    total = None
    for target in process_tree(pid):
        try:
            io_text = Path(f"/proc/{target}/io").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in io_text.splitlines():
            key, _, value = line.partition(":")
            if key in ("rchar", "wchar"):
                total = (total or 0) + int(value.strip() or 0)
    return total


def terminate_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the child AND its descendants, deepest first — the trampoline again.

    `Popen.kill()` terminates only the process Popen holds. Under a uv venv that is the trampoline,
    which would leave the real interpreter running with the socket still open, so the very download
    we just gave up on would keep consuming the session we are about to re-establish.
    """
    descendants = windows_descendants(proc.pid) if sys.platform == "win32" else process_tree(proc.pid)[1:]
    lib = kernel32()
    for target in reversed(descendants):
        if lib is not None:
            handle = lib.OpenProcess(PROCESS_TERMINATE, False, target)
            if handle:
                lib.TerminateProcess(handle, 1)
                lib.CloseHandle(handle)
            continue
        try:
            os.kill(target, 9)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


@dataclass
class WatchedRun:  # pylint: disable=too-few-public-methods
    """What a watched subprocess did: its result, or WHY we stopped waiting for it."""

    returncode: int | None
    stdout: str
    stderr: str
    elapsed: float
    verdict: str  # "" (ran to completion), "stalled", or "ceiling"
    detail: str  # operator-facing failure text; empty unless `verdict` is set
    progress_observed: bool


def run_watched(  # pylint: disable=too-many-locals,too-many-branches,too-many-arguments,too-many-statements
    cmd: list[str],
    env: dict[str, str],
    *,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
    stall_timeout: float = DEFAULT_STALL_TIMEOUT,
    label: str = "download",
    probe: Callable[[int], int | None] = transferred_bytes,
    poll_interval: float = PROGRESS_POLL_SECONDS,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> WatchedRun:
    """Run `cmd`, killing it when it STOPS MAKING PROGRESS rather than when it takes a long time.

    Two deadlines, and which one is armed depends on evidence rather than on configuration:

    * `stall_timeout` — armed only while the probe is CURRENTLY readable AND has been seen to move
      for this child. A flatline is then a genuine stall and killing is right.
    * `timeout` — the wall clock, armed whenever we cannot tell slow from hung: never observed, or
      observed and then LOST. It is measured from the moment we went blind, not from process start,
      so a transfer that progressed for ten minutes and then lost its probe gets a fresh `timeout`
      of blindness rather than being killed on the spot. `0` disables it.

    Availability and history are tracked separately on purpose. Treating "cannot read the counter"
    as "no bytes moved" kills healthy downloads on access denial, on a descendant that exits between
    enumeration and read, and — the nastiest one — on a PARTIAL subtree read, where the readable
    trampoline flatlines while the unreadable descendant is the one doing the work (blind review of
    #482, reproduced: `PROBE_LOST_AFTER_PROGRESS stalled 0.32`).

    A download that keeps progressing past `timeout` is NOT killed; it is announced loudly instead,
    because "elapsed time is not progress" cuts both ways.
    """
    started = time.perf_counter()
    # pylint: disable-next=consider-using-with  # the whole point is to supervise it while it runs
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    collected: dict[str, str] = {"out": "", "err": ""}

    def drain() -> None:
        # communicate() in its own thread: the pipes must be read while the watchdog polls, or a
        # chatty child fills a pipe buffer and blocks -- which would look exactly like a stall.
        out, err = proc.communicate()
        collected["out"], collected["err"] = out or "", err or ""

    reader = threading.Thread(target=drain, name="download-drain", daemon=True)
    reader.start()

    last_value = probe(proc.pid)
    signal_live = last_value is not None
    progress_observed = False
    last_change = started
    last_beat = started
    # When we last had a trustworthy MOVING signal. None means we have one right now; otherwise the
    # wall clock is measured from here. It starts at `started`, so a run that never sees a signal
    # behaves exactly as it did before this whole change.
    blind_since: float | None = started
    announced_over_ceiling = False
    announced_signal_lost = False
    verdict = ""
    since_progress = 0.0
    while True:
        reader.join(poll_interval)
        now = time.perf_counter()
        elapsed = now - started
        if not reader.is_alive():
            break
        value = probe(proc.pid)
        if value is None:
            # Unreadable or partial: we are blind, not stalled. Disarm the stall deadline.
            signal_live = False
            last_value = None
            if blind_since is None:
                blind_since = now
            if progress_observed and not announced_signal_lost:
                announced_signal_lost = True
                LOG.warning(
                    "%s: lost the download-progress signal after %.0fs of movement — falling back "
                    "to the --download-timeout wall clock (%.0fs from now) rather than calling it "
                    "stalled.",
                    label,
                    elapsed,
                    timeout,
                )
        else:
            if not signal_live:
                # Re-acquired. The blind window is not evidence of no progress, so the stall
                # deadline restarts from here rather than counting the gap against the download.
                signal_live = True
                last_change = now
            elif last_value is not None and value != last_value:
                progress_observed = True
                last_change = now
                blind_since = None
            last_value = value
        since_progress = now - last_change
        stall_armed = progress_observed and signal_live and blind_since is None
        blind_for = now - (blind_since if blind_since is not None else now)
        if now - last_beat >= heartbeat:
            last_beat = now
            LOG.info(
                "%s still running: elapsed=%.0fs, %s",
                label,
                elapsed,
                (
                    f"last progress {since_progress:.0f}s ago"
                    if stall_armed
                    else f"no usable progress signal for {blind_for:.0f}s (wall-clock ceiling applies)"
                ),
            )
        if stall_armed:
            if since_progress > stall_timeout:
                verdict = "stalled"
                break
            if timeout and elapsed > timeout and not announced_over_ceiling:
                announced_over_ceiling = True
                LOG.warning(
                    "%s has run %.0fs, past --download-timeout %.0fs, but is still transferring — "
                    "NOT killing it; it will be killed only after %.0fs with no progress.",
                    label,
                    elapsed,
                    timeout,
                    stall_timeout,
                )
        elif timeout and blind_for > timeout:
            verdict = "ceiling"
            break

    elapsed = time.perf_counter() - started
    if verdict:
        terminate_tree(proc)
        reader.join(30)
        blindness = "progress was seen and then the signal was lost" if progress_observed else "blind the whole time"
        detail = (
            (
                f"stalled: no progress for {since_progress:.0f}s (elapsed {elapsed:.0f}s). The "
                f"download was moving, was still observable, and stopped — so this is a hung "
                f"transfer, not a slow one. Raise --download-stall-timeout above "
                f"{stall_timeout:g}s if the source is merely bursty, or retry the asset."
            )
            if verdict == "stalled"
            else (
                f"timeout after {timeout:g}s without a usable download-progress signal (elapsed "
                f"{elapsed:.1f}s, {blindness}); a slow-but-healthy transfer cannot be told from a "
                f"hang while blind. Raise --download-timeout (or pass 0 to remove the ceiling and "
                f"rely on --download-stall-timeout alone)."
            )
        )
        return WatchedRun(None, collected["out"], collected["err"], elapsed, verdict, detail, progress_observed)
    return WatchedRun(proc.returncode, collected["out"], collected["err"], elapsed, "", "", progress_observed)


def download(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    kind: str,
    luid: str,
    out_file: Path,
    env: dict[str, str],
    scripts: Path,
    *,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
    stall_timeout: float = DEFAULT_STALL_TIMEOUT,
    label: str = "",
) -> tuple[bool, str]:
    """Fetch one asset BY LUID via the deterministic tier's fetcher. Returns (ok, detail).

    By LUID, never by name: Tableau permits duplicate names across projects, and name-keyed identity
    has already produced four separate defects in this codebase.

    `timeout` is no longer a plain wall clock — see `run_watched`. A transfer that keeps moving is
    left alone however long it takes; one that stops moving is killed after `stall_timeout`.
    """
    flag = "--workbook-luid" if kind == "workbook" else "--datasource-luid"
    cmd = [
        sys.executable,
        str(scripts / "fetch_tds.py"),
        "--server",
        env["TABLEAU_SERVER_URL"],
        "--site",
        env.get("TABLEAU_SITE", ""),
        flag,
        luid,
        "--include-extract",
        "--no-prompt",
        "--out",
        str(out_file),
    ]
    child = engine_child_env(env)
    run = run_watched(
        cmd,
        child,
        timeout=timeout,
        stall_timeout=stall_timeout,
        label=label or f"{kind} {luid}",
    )
    if run.verdict:
        return False, run.detail
    if run.returncode != 0:
        # Redact BEFORE truncating. Slicing first can cut through the secret and leave a suffix in
        # the retained text, which is then both logged and persisted -- measured: the full secret was
        # absent while its tail survived at the start of the slice. Order matters more than the
        # scrub itself here, because the wrong order still passes a test whose sentinel happens to
        # fall inside the window.
        raw = redact((run.stderr or run.stdout or "").strip(), pat_secret(env), env.get("TABLEAU_PAT_NAME", ""))
        return False, raw[-300:]
    return True, ""


def parse_ours(path: Path) -> dict[str, Any]:
    """Run OUR parser. Returns {ok, error, sheets, dashboards, calcs, data_sources}."""
    try:
        # Imported here, not at module scope, deliberately: an ImportError from the parser IS one of
        # the findings this sweep collects, so it must be caught by the handler below rather than
        # killing the process at import time.
        from parse_tableau import parse_workbook  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        spec = parse_workbook(path)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # A parser crash IS the finding this sweep exists to collect, so it must be recorded and
        # stepped over. Narrowing here would abort the run on the first interesting input.
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-800:]}
    return {
        "ok": True,
        "sheets": len(spec.get("worksheets") or []),
        "dashboards": len(spec.get("dashboards") or []),
        "calcs": sum(len(d.get("calculated_fields") or []) for d in spec.get("data_sources") or []),
        "data_sources": len(spec.get("data_sources") or []),
        "limitations": len(spec.get("limitations_encountered") or []),
    }


def parse_theirs(path: Path, scripts: Path) -> dict[str, Any]:
    """Run HIS descriptor over the same asset, in a subprocess so a hard failure cannot kill us."""
    # His public entry point is `parse_tds(xml_text)` (connection_to_m.py:1931) - it takes the XML
    # TEXT, so a packaged .tdsx/.twbx must be unzipped first. The first attempt guessed
    # `describe_datasource`, which reported "his parser failed 3/3" when nothing of his had run at
    # all. A harness error and a real finding are indistinguishable unless you read the error text.
    snippet = (
        "import json,sys,zipfile\n"
        f"sys.path.insert(0, r'{scripts}')\n"
        "import connection_to_m as cm\n"
        f"p = r'{path}'\n"
        "try:\n"
        "    if p.lower().endswith(('.twbx','.tdsx')):\n"
        "        z = zipfile.ZipFile(p)\n"
        "        inner = [n for n in z.namelist() if n.endswith(('.twb','.tds'))][0]\n"
        "        xml = z.read(inner).decode('utf-8','replace')\n"
        "    else:\n"
        "        xml = open(p, encoding='utf-8', errors='replace').read()\n"
        "    d = cm.parse_tds(xml)\n"
        "    rels = d.get('relations') or []\n"
        "    print(json.dumps({'ok':True,'relations':len(rels),"
        "'untyped':len([r for r in rels if not r.get('columns')]),"
        "'unsupported':d.get('unsupported_reasons') or [],"
        "'connection_class':d.get('connection_class'),"
        "'named_connection_count':d.get('named_connection_count')}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'ok':False,'error':'%s: %s' % (type(e).__name__, e)}))\n"
    )
    try:
        proc = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, timeout=180, check=False)
        return json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Same reason, one layer out: a harness fault must be labelled as such, never as a parser
        # verdict - reporting 'his parser failed' for our own bug already happened once here.
        return {"ok": False, "error": f"harness: {type(exc).__name__}: {exc}"}


# Sentinel carried in the `workbook_project` slot of an edge tuple when the workbook end could not
# be resolved to a SINGLE identity: the dependency's own `workbook_name` matched more than one real
# workbook (different projects, no LUID to pick between them). Never a real project name - a null
# byte cannot appear in Tableau project names - so it can never collide with a genuine project
# called, say, "Ambiguous" (review of the #483 follow-up: "must not fuzzy-match... or guess").
_AMBIGUOUS_WORKBOOK = "\x00AMBIGUOUS"


def _normalized_name(name: str | None) -> str:
    """Same equivalence the SQL fallback joins already use (`LOWER(TRIM(x))`), done in Python so the
    workbook-name index below and the SQL text agree on what counts as "the same name"."""
    return (name or "").strip().casefold()


def dependency_edges(con: sqlite3.Connection) -> list[tuple[str, str, str, str, str]]:
    """Every `(workbook_luid, workbook_name, workbook_project, datasource_luid, datasource_name)`
    binding on the site.

    The workbook end used to be an INNER JOIN on `workbook.luid = dependency.workbook_luid` with no
    fallback at all - so a dependency row surveyed with a blank/unresolved `workbook_luid` (but a
    real `workbook_name`) was dropped from the result BEFORE `orphaned_dependents()` ever saw it,
    which defeated its documented project-plus-name/`UNIDENTIFIED-N` fallback entirely: there was no
    row left to apply it to (review of the #483 follow-up, "the real query returns `edges=[]`").

    Resolution order, same as the identity ladder `orphaned_dependents()` already implements:
    1. exact `workbook_luid` match against the `workbook` table - unchanged, still wins outright;
    2. else an EXACT (never fuzzy) match of `dependency.workbook_name` against `workbook.name`:
       unique -> resolved to that workbook's own LUID/project; more than one workbook shares the
       name (no LUID to pick between them) -> `_AMBIGUOUS_WORKBOOK`, an explicit "cannot tell which"
       marker, never a guess at either one;
    3. else the dependency row is still returned - workbook_luid/project both empty - so the caller
       can report it as unidentified rather than the row silently vanishing.

    Every raw dependency ROW is returned as its own edge, even when several rows are textually
    identical (blank LUID, same name, same datasource): the caller, not this function, decides
    whether that multiplicity is real occurrences or a data-quality duplicate, and it can only do
    that if the count survives to `orphaned_dependents()` (review of the #526 follow-up: `SELECT
    DISTINCT` used to fold N unresolved dependency rows into one before that fallback ever ran).
    """
    workbook_rows = list(
        con.execute(
            """
            SELECT workbook.luid, workbook.name, project.name
            FROM workbook
            LEFT JOIN project ON project.luid = workbook.project_luid
            """
        )
    )
    by_luid = {luid: (name or "", project or "") for luid, name, project in workbook_rows if luid}
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    for luid, name, project in workbook_rows:
        by_name.setdefault(_normalized_name(name), []).append((luid, name or "", project or ""))

    # No `DISTINCT` here on purpose (review of the #526 follow-up): the workbook end being unresolved
    # does not make two dependency rows THE SAME occurrence - they could be two genuinely different,
    # equally-unattributable bindings that merely look identical because there is no LUID to tell
    # them apart. `DISTINCT` collapsed exactly that case to one row before `orphaned_dependents()`
    # ever saw it, so it never had raw material to report "N unresolved dependencies share this name"
    # - it always saw N=1. A resolved (LUID-matched) row still naturally collapses downstream, in
    # `orphaned_dependents()`, because its identity is real and duplicate real edges key to the same
    # identity there regardless of how many raw rows produced them.
    raw = con.execute(
        """
        SELECT dependency.workbook_luid, dependency.workbook_name, datasource.luid, datasource.name
        FROM dependency
        JOIN datasource ON dependency.datasource_luid = datasource.luid
            OR (
                COALESCE(dependency.datasource_luid, '') = ''
                AND LOWER(TRIM(dependency.datasource_name)) = LOWER(TRIM(datasource.name))
            )
        ORDER BY datasource.name, dependency.workbook_name
        """
    )
    edges: list[tuple[str, str, str, str, str]] = []
    for workbook_luid, dep_workbook_name, datasource_luid, datasource_name in raw:
        resolved = by_luid.get(workbook_luid) if workbook_luid else None
        if resolved:
            name, project = resolved
            edges.append((workbook_luid, name or dep_workbook_name or "", project, datasource_luid, datasource_name))
            continue
        candidates = by_name.get(_normalized_name(dep_workbook_name), []) if dep_workbook_name else []
        if len(candidates) == 1:
            luid, name, project = candidates[0]
            edges.append((luid, name, project, datasource_luid, datasource_name))
        elif len(candidates) > 1:
            edges.append(("", dep_workbook_name or "", _AMBIGUOUS_WORKBOOK, datasource_luid, datasource_name))
        else:
            edges.append(("", dep_workbook_name or "", "", datasource_luid, datasource_name))
    return edges


def orphaned_dependents(
    results: list[dict], edges: list[tuple[str, str, str, str, str]]
) -> list[tuple[str, list[tuple[str, str]]]]:
    """`[(failed datasource name, [(workbook identity, workbook name) that DID land and bind to it])]`.

    A datasource that never downloaded does not fail alone: every workbook bound to it converts to
    an incomplete model, and a report against a model nobody migrated is the documented "empty
    report". The harvester already resolves exactly these edges to DECIDE what to fetch
    (`dependency_datasources`) and then never looked at them again once one had failed, so catching
    it depended on the operator noticing (issue #472). A workbook that itself failed to download is
    left out: it is already in the never-landed list and is not about to be converted.

    Each workbook entry carries its own IDENTITY beside its display name, never a bare name alone
    (issue #483, and its follow-up reviews): two same-named workbooks in different projects must
    stay two entries all the way to the human/machine output, not just inside this function's
    internal grouping. The identity is, in order: the workbook LUID (`dependency_edges` now resolves
    this via an exact name match too, not just the survey's own LUID); else a project-qualified
    `"<project>::<name>"` built from data `dependency_edges` already carries; else, when the name
    matched more than one real workbook and there is no LUID to pick between them, an explicit
    `"AMBIGUOUS::<name>"`; else an explicit `UNIDENTIFIED-N` marker so an edge that resolves nothing
    at all is still reported instead of being silently dropped or coalesced with an unrelated one.

    An `AMBIGUOUS::<name>` group can itself be hit by more than one raw dependency row for the SAME
    datasource (`dependency_edges` no longer collapses those via `SELECT DISTINCT` - review of the
    #526 follow-up). This function must not silently re-collapse them either: it cannot invent a
    second identity for a row it genuinely cannot distinguish from the first, so instead it reports
    ONE ambiguity-group record whose display name states the EXACT occurrence count - "N unresolved
    dependencies share this name and cannot be individually attributed" - rather than the count
    quietly reading as one workbook when the underlying dependency data said N.
    """
    missing_ids = {id(r) for r in never_downloaded(results)}
    failed_datasources = {
        str(r.get("luid", "")): str(r.get("name", "?"))
        for r in results
        if id(r) in missing_ids and r.get("kind") == "datasource"
    }
    landed_workbooks = {
        str(r.get("luid", "")): str(r.get("name", "?"))
        for r in results
        if id(r) not in missing_ids and r.get("kind") == "workbook"
    }
    by_datasource, ambiguous_hits = _group_edges_by_datasource(edges, failed_datasources, landed_workbooks)
    return [
        (key, _finalize_orphan_entries(key, entries, ambiguous_hits)) for key, entries in sorted(by_datasource.items())
    ]


def _group_edges_by_datasource(
    edges: list[tuple[str, str, str, str, str]],
    failed_datasources: dict[str, str],
    landed_workbooks: dict[str, str],
) -> tuple[dict[str, dict[str, tuple[str, str]]], dict[tuple[str, str], int]]:
    """The identity-resolution loop `orphaned_dependents()` used to run inline: pulled out so that
    function's own local-variable count stays readable (pylint's `too-many-locals`), not for reuse.

    Returns `(by_datasource, ambiguous_hits)`: entries grouped per failed datasource, keyed by each
    workbook's resolved identity, plus a separate per-`(datasource, identity)` occurrence count for
    the `AMBIGUOUS::<name>` groups specifically - the real multiplicity `_finalize_orphan_entries()`
    needs to report rather than silently collapsing (review of the #526 follow-up).
    """
    by_datasource: dict[str, dict[str, tuple[str, str]]] = {}
    ambiguous_hits: dict[tuple[str, str], int] = {}
    unidentified = 0
    for workbook_luid, workbook_name, workbook_project, datasource_luid, datasource_name in edges:
        if datasource_luid not in failed_datasources:
            continue
        # A RESOLVED luid must still have landed - a workbook that itself failed to download is
        # already in the never-landed list and is not about to be converted. An UNRESOLVED identity
        # (blank luid: name-only, or genuinely ambiguous) has no real key to check `landed_workbooks`
        # against at all, so it is reported unconditionally rather than dropped a second time - the
        # exact failure mode this follow-up exists to close.
        if workbook_luid and workbook_luid not in landed_workbooks:
            continue
        name = workbook_name or landed_workbooks.get(workbook_luid, "?")
        key = datasource_name or failed_datasources[datasource_luid]
        if workbook_luid:
            identity = workbook_luid
        elif workbook_project == _AMBIGUOUS_WORKBOOK:
            identity = f"AMBIGUOUS::{name}"
            ambiguous_hits[(key, identity)] = ambiguous_hits.get((key, identity), 0) + 1
        elif workbook_project:
            identity = f"{workbook_project}::{name}"
        else:
            unidentified += 1
            identity = f"UNIDENTIFIED-{unidentified}"
        by_datasource.setdefault(key, {})[identity] = (identity, name)
    return by_datasource, ambiguous_hits


def _finalize_orphan_entries(
    key: str, entries: dict[str, tuple[str, str]], ambiguous_hits: dict[tuple[str, str], int]
) -> list[tuple[str, str]]:
    """One datasource's grouped `(identity, name)` entries, with any genuine ambiguity-group
    multiplicity (`ambiguous_hits`) folded into the display name rather than silently dropped."""
    finalized = []
    for identity, name in entries.values():
        occurrences = ambiguous_hits.get((key, identity), 0)
        if occurrences > 1:
            name = f"{name} ({occurrences} unresolved dependencies share this name, cannot be individually attributed)"
        finalized.append((identity, name))
    return sorted(finalized, key=lambda entry: (entry[0], entry[1]))


def _format_orphan_workbook(entry: tuple[str, str]) -> str:
    """`"<name> [<identity>]"` -- human-readable, but the identity is always visible beside it so a
    duplicate display name never reads as a single workbook (issue #483 follow-up)."""
    identity, name = entry
    return f"{name} [{identity}]"


def never_downloaded(results: list[dict]) -> list[dict]:
    """Rows that never reached a parser at all, i.e. the downloads that did not land.

    They are what `len(results)` counted and no bucket claimed: `r.get("ours", {}).get("ok") is
    False` is False for a row with NO `ours` key, so a failed download fell out of every heading
    while still inflating the denominator. A customer read `45/47 succeeded` off a tally whose
    arithmetic did not close (issue #472).
    """
    return [r for r in results if "ours" not in r or "theirs" not in r]


def _strict_bool(value: object) -> bool | None:
    """`True`/`False` only when `value` IS a boolean; `None` for everything else.

    A parser's `ok` is meant to be exactly JSON/Python `true`/`false`. `value is False` (used below
    for the overlapping ours/his-failed counts) already happens to be strict — `None is False` and
    `"no" is False` are both `False` — but nothing previously caught the TRUTHY side: a non-bool
    `ok` such as `"yes"`, `1`, or `["partial"]` passed the old `and`-based `both_ok` check as if it
    were a real success. `bool` is a subclass of `int`, so this must check `isinstance(value, bool)`
    and not merely `isinstance(value, int)`.
    """
    return value if isinstance(value, bool) else None


def _verdict_label(strict_ok: bool | None) -> str:
    """`"ok"` / `"FAIL"` for a real boolean verdict, `"INVALID"` for `_strict_bool`'s `None`.

    Keeps a per-asset log line and `summarise()`'s bucket classification from disagreeing about the
    SAME row (issue #483 follow-up): both now read the verdict through `_strict_bool()` first.
    """
    if strict_ok is None:
        return "INVALID"
    return "ok" if strict_ok else "FAIL"


def indeterminate_parser_outcomes(results: list[dict]) -> list[dict]:
    """Rows that DID download but whose `ours`/`theirs` `ok` is missing or not a strict boolean.

    Before this existed, such a row entered NO bucket in `summarise()` while still inflating
    `len(results)`: the closure arithmetic could read `total 1` while every bucket read `0`, without
    the assertion in `summarise()` ever failing (issue #483). Every asset that reached a parser now
    lands in exactly one of: both parsed, ours only failed, his only failed, both failed, or here.
    """
    missing_ids = {id(r) for r in never_downloaded(results)}
    return [
        r
        for r in results
        if id(r) not in missing_ids
        and (_strict_bool(r.get("ours", {}).get("ok")) is None or _strict_bool(r.get("theirs", {}).get("ok")) is None)
    ]


def failure_shape(message: str) -> str:
    """A grouping key for a failure message, with the varying numbers collapsed.

    Digits have to go, or the very failures worth grouping stay apart: two timeouts differing only
    in `elapsed 601.4s` vs `elapsed 612.9s` are ONE feature request, not two, and the file's own
    rule is to group by shape.
    """
    return re.sub(r"\d+(?:\.\d+)?", "N", str(message)).strip()[:90] or "(no detail)"


def grouped_by_shape(rows: list[dict], detail: Callable[[dict], str]) -> list[tuple[str, list[str]]]:
    """`[(shape, [asset names])]`, most frequent shape first."""
    by_shape: dict[str, list[str]] = {}
    for row in rows:
        by_shape.setdefault(failure_shape(detail(row)), []).append(str(row.get("name", "?")))
    return sorted(by_shape.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def summarise(  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
    results: list[dict], out: Path, orphans: list[tuple[str, list[tuple[str, str]]]] | None = None
) -> str:
    """Group failures by SHAPE, because one root cause repeated 12 times is one feature request."""
    missing = never_downloaded(results)
    missing_ids = {id(r) for r in missing}
    parsed = [r for r in results if id(r) not in missing_ids]
    invalid = indeterminate_parser_outcomes(results)
    invalid_ids = {id(r) for r in invalid}
    valid = [r for r in parsed if id(r) not in invalid_ids]
    # `ours_fail`/`theirs_fail` stay OVERLAPPING counts (an asset that defeats both parsers is in
    # both), used only for the informational header line below -- unchanged from before #483.
    ours_fail = [r for r in parsed if r.get("ours", {}).get("ok") is False]
    theirs_fail = [r for r in parsed if r.get("theirs", {}).get("ok") is False]
    both_fail = [r for r in ours_fail if id(r) in {id(x) for x in theirs_fail}]
    both_fail_ids = {id(r) for r in both_fail}
    # From here down every row is DISJOINT: `valid` guarantees a strict boolean on both sides, so
    # there are exactly four combinations (TT/FF/FT/TF) and each row lands in exactly one.
    both_ok = [r for r in valid if r.get("ours", {}).get("ok") is True and r.get("theirs", {}).get("ok") is True]
    ours_only = [r for r in valid if id(r) not in both_fail_ids and r.get("ours", {}).get("ok") is False]
    theirs_only = [r for r in valid if id(r) not in both_fail_ids and r.get("theirs", {}).get("ok") is False]

    disjoint_total = len(both_ok) + len(ours_only) + len(theirs_only) + len(both_fail) + len(invalid) + len(missing)
    assert disjoint_total == len(results), (
        f"disjoint outcome buckets summed to {disjoint_total}, not {len(results)} asset(s) -- "
        "a row escaped every bucket (issue #483)"
    )

    lines = ["# Estate parse sweep", ""]
    lines.append(
        f"**{len(results)} asset(s)** — ours failed {len(ours_fail)}, his failed {len(theirs_fail)}, "
        f"both parsed {len(both_ok)}, never downloaded {len(missing)}, "
        f"invalid/indeterminate outcome {len(invalid)}."
    )
    lines.append("")
    # The closure line is the audit: `ours failed`/`his failed` OVERLAP when one asset defeats both
    # parsers, so those two numbers cannot be added to anything. These six are disjoint and must
    # sum to the denominator -- which is the property that silently failed before issue #472, and
    # again for a non-boolean `ok` before issue #483.
    lines.append(
        f"Disjoint buckets, which must add up to the {len(results)} above: "
        f"{len(both_ok)} parsed by both + {len(ours_only)} ours only + "
        f"{len(theirs_only)} his only + {len(both_fail)} both parsers + "
        f"{len(invalid)} invalid/indeterminate + "
        f"{len(missing)} never downloaded = "
        f"{disjoint_total}."
    )
    lines.append("")
    lines.append(
        "A failure on ONE side only is the interesting case: it says which tier owns the gap "
        "(`docs/migration-programme.md` §0). A failure on both is a genuinely hard input."
    )
    lines.append("")

    lines.append("## Downloads that never landed")
    lines.append("")
    if not missing:
        lines.append("_none_")
        lines.append("")
    else:
        lines.append(
            f"**{len(missing)} asset(s) never reached a parser**, so nothing below this heading "
            "says anything about them. They are not successes."
        )
        lines.append("")
        for shape, names in grouped_by_shape(missing, lambda r: str(r.get("download_error", "?"))):
            lines.append(f"- **{len(names)}x** `{shape}`")
            lines.append(f"  - {', '.join(names[:8])}{' …' if len(names) > 8 else ''}")
        lines.append("")

    if orphans:
        lines.append("## Do not convert yet — a datasource they bind to never landed")
        lines.append("")
        lines.append(
            "These workbooks downloaded fine, so nothing else here flags them. They would each "
            "convert against a model nobody migrated, which is the documented **empty report**. "
            "Each is named with its identity (LUID, or a project fallback) so two same-named "
            "workbooks in different projects stay distinguishable (issue #483)."
        )
        lines.append("")
        for datasource, workbooks in orphans:
            lines.append(f"- `{datasource}` (failed) blocks **{len(workbooks)}** workbook(s):")
            labels = [_format_orphan_workbook(w) for w in workbooks]
            lines.append(f"  - {', '.join(labels[:8])}{' …' if len(labels) > 8 else ''}")
        lines.append("")

    for title, rows, key in (
        ("## Our parser failed", ours_fail, "ours"),
        ("## The deterministic tier's descriptor failed", theirs_fail, "theirs"),
    ):
        lines.append(title)
        lines.append("")
        if not rows:
            lines.append("_none_")
            lines.append("")
            continue
        by_shape: dict[str, list[str]] = {}
        for r in rows:
            msg = str(r[key].get("error", "?"))
            head, _, tail = msg.partition(":")
            shape = f"{head}: {tail[:70]}"
            by_shape.setdefault(shape, []).append(r["name"])
        for shape, names in sorted(by_shape.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- **{len(names)}x** `{shape}`")
            lines.append(f"  - {', '.join(names[:8])}{' …' if len(names) > 8 else ''}")
        lines.append("")

    lines.append("## Invalid/indeterminate parser outcome")
    lines.append("")
    if not invalid:
        lines.append("_none_")
        lines.append("")
    else:
        lines.append(
            f"**{len(invalid)} asset(s)** downloaded fine but `ok` was not exactly `true`/`false` on "
            "at least one side. This is NOT a clean parse and NOT a clean failure -- treat it as "
            "broken until a real parser verdict replaces it."
        )
        lines.append("")
        for r in invalid:
            ours_ok = r.get("ours", {}).get("ok")
            theirs_ok = r.get("theirs", {}).get("ok")
            lines.append(f"- `{r.get('name', '?')}` — ours.ok={ours_ok!r}, theirs.ok={theirs_ok!r}")
        lines.append("")

    unsupported: dict[str, list[str]] = {}
    for r in results:
        for reason in (r.get("theirs", {}) or {}).get("unsupported", []) or []:
            shape = str(reason).partition("'")[0].strip() or str(reason)[:60]
            unsupported.setdefault(shape, []).append(r["name"])
    if unsupported:
        lines.append("## Rebuild refusals (his `unsupported_reasons`), grouped by shape")
        lines.append("")
        for shape, names in sorted(unsupported.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- **{len(names)}x** {shape}")
            lines.append(f"  - {', '.join(sorted(set(names))[:8])}")
        lines.append("")

    text = "\n".join(lines) + "\n"
    (out / "parse-sweep.md").write_text(text, encoding="utf-8")
    (out / "parse-sweep.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    # Machine-readable disjoint totals, so a caller can assert the closure without re-parsing the
    # markdown -- the SAME assertion above is what guarantees these six sum to `total` (issue #483).
    totals = {
        "total": len(results),
        "both_ok": len(both_ok),
        "ours_only": len(ours_only),
        "theirs_only": len(theirs_only),
        "both_fail": len(both_fail),
        "invalid": len(invalid),
        "never_downloaded": len(missing),
    }
    (out / "parse-sweep-totals.json").write_text(json.dumps(totals, indent=2) + "\n", encoding="utf-8")
    return text


def dependency_datasources(con: sqlite3.Connection, workbook_luids: list[str]) -> list[tuple[str, str]]:
    """The published datasources those workbooks bind to, even when they live in another project.

    LUID first; an edge the survey could not resolve to a LUID falls back to the normalized name and
    keeps EVERY candidate, because dropping one silently is the "empty report" failure this exists
    to prevent.
    """
    if not workbook_luids:
        return []
    return list(
        con.execute(
            f"""
            SELECT DISTINCT datasource.luid, datasource.name
            FROM datasource
            JOIN dependency ON dependency.datasource_luid = datasource.luid
                OR (
                    COALESCE(dependency.datasource_luid, '') = ''
                    AND LOWER(TRIM(dependency.datasource_name)) = LOWER(TRIM(datasource.name))
                )
            WHERE dependency.workbook_luid IN ({",".join("?" for _ in workbook_luids)})
            ORDER BY datasource.name, datasource.luid
            """,
            workbook_luids,
        )
    )


def scoped_todo(
    con: sqlite3.Connection, project_names: list[str], project_ids: list[str], workbooks_only: bool
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]], int, int, int]:
    """Select everything IN the chosen projects, plus the published sources their edges require.

    Returns `(todo, selected_projects, workbooks, datasources_in_project, datasources_pulled_in)`.

    Both halves are load-bearing and neither substitutes for the other. Following dependency edges
    OUT of the project is what stops a report rebuilding against a model nobody migrated. Selecting
    the datasources that simply LIVE in the project is what makes the model-first phase-1 workflow
    work at all: the issue's own example, `--project "00 - Certified Sources"`, is 3 datasources and
    0 workbooks, so an edges-only scope selects nothing and exits 1 on `0 asset(s) to sweep`.
    """
    if not project_names and not project_ids:
        todo = []
        if not workbooks_only:
            todo.extend(
                ("datasource", luid, name)
                for luid, name in con.execute("SELECT luid, name FROM datasource ORDER BY name")
            )
        todo.extend(
            ("workbook", luid, name) for luid, name in con.execute("SELECT luid, name FROM workbook ORDER BY name")
        )
        return todo, [], 0, 0, 0

    selected = list(
        con.execute(
            f"SELECT luid, name FROM project WHERE name IN ({','.join('?' for _ in project_names) or 'NULL'}) "
            f"OR luid IN ({','.join('?' for _ in project_ids) or 'NULL'}) ORDER BY name, luid",
            [*project_names, *project_ids],
        )
    )
    if not selected:
        raise ValueError("no projects matched --project/--project-id")
    selected_ids = [row[0] for row in selected]
    placeholders = ",".join("?" for _ in selected_ids)
    workbooks = list(
        con.execute(
            f"SELECT luid, name FROM workbook WHERE project_luid IN ({placeholders}) ORDER BY name, luid", selected_ids
        )
    )
    in_project: list[tuple[str, str]] = []
    pulled_in: list[tuple[str, str]] = []
    if not workbooks_only:
        in_project = list(
            con.execute(
                f"SELECT luid, name FROM datasource WHERE project_luid IN ({placeholders}) ORDER BY name, luid",
                selected_ids,
            )
        )
        already = {luid for luid, _ in in_project}
        pulled_in = [row for row in dependency_datasources(con, [row[0] for row in workbooks]) if row[0] not in already]
    datasources = sorted(in_project + pulled_in, key=lambda row: (row[1], row[0]))
    todo = [("datasource", luid, name) for luid, name in datasources]
    todo.extend(("workbook", luid, name) for luid, name in workbooks)
    return todo, selected, len(workbooks), len(in_project), len(pulled_in)


def parse_asset(path: Path, scripts: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run both offline parsers while the main thread starts the next fresh session."""
    return parse_ours(path), parse_theirs(path, scripts)


def safe_component(text: str, limit: int | None = None) -> str:
    """Filename-safe form of a Tableau name or LUID -- `[A-Za-z0-9-_]` only, so it is glob-literal."""
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in text)
    return cleaned[:limit] if limit is not None else cleaned


def asset_path(assets_dir: Path, kind: str, name: str, luid: str) -> Path:
    """The local filename to download INTO: `<luid>_<name><ext>`; display names are not unique.

    ⚠️ The LUID goes in FRONT on purpose. The engine's `migrate_estate.py::asset_name()` strips a
    leading canonical-UUID prefix (`_TRANSFER_UUID_PREFIX`, followed by `-`, `_` or a space), so a
    prefixed file keeps local identity WITHOUT the LUID reaching `bundle/pbip/<stem>/` or
    `migrations/<slug>/`. A trailing `--<luid>` is not stripped and does reach both -- verified
    against engine 2.126.0: `strip_transfer_uuid('<uuid>_Meridian_Revenue_by_Region')` ->
    `'Meridian_Revenue_by_Region'`, while `'Meridian_Revenue_by_Region--<uuid>'` is returned intact.
    """
    extension = ".twbx" if kind == "workbook" else ".tdsx"
    return assets_dir / f"{safe_component(luid)}_{safe_component(name, 60)}{extension}"


def landed_files(assets_dir: Path, stem: str) -> list[Path]:
    """Files that could be this asset's download, PACKAGED form first: a `.twbx` carries data."""
    found = sorted(assets_dir.glob(f"{stem}.tw*")) + sorted(assets_dir.glob(f"{stem}.td*"))
    return sorted(found, key=lambda path: (path.suffix.lower() not in (".twbx", ".tdsx"), path.name))


def existing_asset(assets_dir: Path, kind: str, name: str, luid: str) -> Path | None:
    """The file that ACTUALLY landed for this asset, or None. Never assume the requested extension.

    Two fallbacks, both measured, both load-bearing:

    * **extension.** The engine's `fetch_tds.py::save_outputs` writes `<base>.twb`/`<base>.tds`
      whenever the REST download is not a zip -- across three real full harvests the landed
      extensions were `{'.tdsx': 17, '.twb': 18, '.twbx': 20}`, i.e. **18 of 38 workbooks (47%)
      arrive as `.twb` where `.twbx` was requested**. Matching only the requested extension loses
      them, and the sweep still reports `ours failed 0, his failed 0`, because an asset that never
      reached a parser is not counted as a failure: silent data loss that reads as a clean run.
    * **legacy name.** Assets harvested before the LUID prefix are `<name><ext>`. Without this the
      first run after an upgrade re-downloads the whole estate at one fresh sign-in per asset --
      the opposite of the resume behaviour this is for. Reuse is only as ambiguous as the run that
      wrote it (two same-named assets already shared one legacy file); everything downloaded from
      here on is LUID-unique.
    """
    candidates = [asset_path(assets_dir, kind, name, luid)]
    for stem in (f"{safe_component(luid)}_{safe_component(name, 60)}", safe_component(name, 60)):
        candidates.extend(landed_files(assets_dir, stem))
    return next((path for path in candidates if path.exists()), None)


def progress(finished: int, total: int, started: float) -> str:
    """Elapsed, running average and ETA measured on FINISHED assets only.

    The divisor must be what has actually completed, never the loop index: the download loop and the
    parse drain each count from 1 while `elapsed` keeps accumulating, so `elapsed / index` reported
    `[1/6] ... elapsed=9s avg=9.1s ETA=46s` on a run with **0 s of work left**, and scaled to ~19
    hours announced on a 58-asset run. An ETA that big in front of a customer is worse than none.
    """
    elapsed = time.perf_counter() - started
    if finished <= 0:
        return f"elapsed={elapsed:.0f}s"
    average = elapsed / finished
    return f"elapsed={elapsed:.0f}s avg={average:.1f}s ETA={average * max(total - finished, 0):.0f}s"


def record_parse(
    entry: tuple[int, dict[str, Any], Future[tuple[dict[str, Any], dict[str, Any]]]],
    results: list[dict],
    total: int,
    started: float,
) -> None:
    """Collect one finished offline parse and log it, so progress interleaves with the downloads.

    Uses the SAME `_strict_bool()` verdict as `summarise()`'s bucket classification (issue #483
    follow-up): a truthy-but-non-boolean `ok` (`"true"`, `1`, a list, a dict) used to print as
    `ours=ok`/`his=ok` here — an operator watching the run scroll by would read that as a clean
    parse, while the eventual summary correctly routed the SAME row to the invalid/indeterminate
    bucket. The two had to agree.
    """
    index, row, future = entry
    row["ours"], row["theirs"] = future.result()
    results.append(row)
    ours_ok = _strict_bool(row["ours"].get("ok"))
    theirs_ok = _strict_bool(row["theirs"].get("ok"))
    if ours_ok is None or theirs_ok is None:
        mark = "IND "
    elif ours_ok and theirs_ok:
        mark = "ok "
    else:
        mark = "DIFF"
    LOG.info(
        "[%d/%d] %-46s %s ours=%s his=%s %s",
        index,
        total,
        row["name"][:46],
        mark,
        _verdict_label(ours_ok),
        _verdict_label(theirs_ok),
        progress(len(results), total, started),
    )


def sweep_exit_code(results: list[dict]) -> int:
    """`EXIT_OK` every asset assessed, `EXIT_PARTIAL` some, `EXIT_NOTHING_ASSESSED` none.

    Counts ASSESSED assets, never rows. A failed download is a row too, which is exactly how a
    harvest that assessed nothing exited 0 (issue #472; reproduced in the blind review of #482).
    An empty `results` is `EXIT_NOTHING_ASSESSED` for the same reason it always was non-zero: a
    sweep that looked at nothing has said nothing about the estate.

    An asset with an invalid/indeterminate `ok` (issue #483) is ALSO not a clean sweep: it did not
    fail cleanly and it did not parse cleanly either, so it must not let the run exit 0.
    """
    missing = len(never_downloaded(results))
    assessed = len(results) - missing
    if assessed <= 0:
        return EXIT_NOTHING_ASSESSED
    invalid = len(indeterminate_parser_outcomes(results))
    return EXIT_PARTIAL if missing or invalid else EXIT_OK


def report_failed_downloads(
    results: list[dict], orphans: list[tuple[str, list[tuple[str, str]]]] | None = None
) -> list[dict]:
    """Say out loud, at the END of the run, which assets never landed. Returns those rows.

    The per-asset `DOWNLOAD FAILED` warning has already scrolled past a 47-line run by the time the
    summary prints, and `download_error` was otherwise written into `parse-sweep.json` and surfaced
    NOWHERE an operator reads. The closing tally is the last thing they see, so the exceptions have
    to be beside it or the run reads as clean (issue #472).
    """
    missing = never_downloaded(results)
    if missing:
        LOG.warning(
            "%d of %d asset(s) NEVER DOWNLOADED and were not parsed at all — they are not successes:",
            len(missing),
            len(results),
        )
        for row in missing:
            LOG.warning("  - %s (%s): %s", row.get("name", "?"), row.get("kind", "?"), row.get("download_error", "?"))
    for datasource, workbooks in orphans or []:
        labels = [_format_orphan_workbook(w) for w in workbooks]
        LOG.warning(
            "DO NOT CONVERT YET: '%s' never downloaded, so %d workbook(s) that DID land would "
            "convert against a model nobody migrated: %s",
            datasource,
            len(workbooks),
            ", ".join(labels[:8]) + (" …" if len(labels) > 8 else ""),
        )
    return missing


def main() -> int:  # pylint: disable=too-many-locals,too-many-statements,too-many-branches  # one sweep
    """Harvest and sweep. Exit 0 complete, 1 nothing assessed, 2 committable `--out`, 3 partial."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="output directory (must be git-ignored, see below)")
    ap.add_argument("--env", type=Path, default=REPO_ROOT / ".env")
    ap.add_argument("--db", type=Path, help="assess_estate.py estate.db to take LUIDs from")
    ap.add_argument(
        "--project",
        action="append",
        default=[],
        help="project name to harvest (repeatable): its workbooks AND datasources, plus any "
        "datasource those workbooks depend on, wherever it lives",
    )
    ap.add_argument(
        "--project-id",
        action="append",
        default=[],
        help="project LUID to harvest (repeatable); same selection as --project, matched exactly",
    )
    ap.add_argument("--limit", type=int, help="stop after N assets (for a quick pass)")
    ap.add_argument(
        "--download-timeout",
        type=float,
        default=DEFAULT_DOWNLOAD_TIMEOUT,
        help="wall-clock ceiling per asset, in seconds, applied ONLY while no download-progress "
        f"signal is available (default {DEFAULT_DOWNLOAD_TIMEOUT:.0f}, derived from the fetcher's "
        f"own bounded retry budget: {ENGINE_DOWNLOAD_ATTEMPTS} attempts x "
        f"{ENGINE_READ_TIMEOUT_SECONDS:.0f}s read timeout + backoff + sign-in); 0 removes the "
        "ceiling. A transfer we can see progressing is never killed by this, and a value below the "
        "fetcher's own budget kills it before it can report the real error.",
    )
    ap.add_argument(
        "--download-stall-timeout",
        type=float,
        default=DEFAULT_STALL_TIMEOUT,
        help="seconds a download may make NO observable progress before it is killed as hung "
        f"(default {DEFAULT_STALL_TIMEOUT:.0f}); armed only while the signal is readable AND has "
        f"moved. Below {ENGINE_READ_TIMEOUT_SECONDS:.0f}s it can pre-empt the fetcher's own "
        "per-read timeout and kill a bursty-but-healthy transfer.",
    )
    ap.add_argument("--skip-download", action="store_true", help="reuse whatever is already in --out/assets")
    ap.add_argument("--workbooks-only", action="store_true", help="skip published datasources; sweep workbooks only")
    ap.add_argument(
        "--allow-unignored-out",
        action="store_true",
        help="write to --out even when git does not ignore it (escape hatch; logs a warning instead)",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if 0 < args.download_stall_timeout < ENGINE_READ_TIMEOUT_SECONDS:
        LOG.warning(
            "--download-stall-timeout %.0fs is BELOW the fetcher's own %.0fs per-read timeout "
            "(fetch_tds.py:405,443). urllib tolerates a gap that long on a healthy connection, so "
            "this can kill a bursty-but-healthy download and report it as hung.",
            args.download_stall_timeout,
            ENGINE_READ_TIMEOUT_SECONDS,
        )
    if 0 < args.download_timeout < ENGINE_DOWNLOAD_BUDGET_SECONDS:
        LOG.warning(
            "--download-timeout %.0fs is BELOW the fetcher's own %.0fs bounded retry budget "
            "(%d attempts x %.0fs read timeout + backoff, fetch_tds.py:405). While the "
            "download-progress signal is unreadable this ceiling KILLS the fetcher before its own "
            "timer can report the real error, and you get `timeout after %.0fs` instead.",
            args.download_timeout,
            ENGINE_DOWNLOAD_BUDGET_SECONDS,
            ENGINE_DOWNLOAD_ATTEMPTS,
            ENGINE_READ_TIMEOUT_SECONDS,
            args.download_timeout,
        )
    # Before the engine, the .env, the database and above all the download: a customer's workbooks
    # must never land somewhere this PUBLIC repo would commit them (issue #125). The guard is given
    # `--out` RAW, so it can judge the literal argv form as well as the resolved one; the write then
    # uses the resolved form, which the guard has just passed. Checking one form and writing another
    # is the whole of issue #374 -- measured: `--out ~/sweep` from cmd.exe was judged as
    # `%USERPROFILE%\sweep` (outside any work tree, so allowed) and written to `<checkout>\~\sweep`.
    if refuse_unignored_output(args.out, args.allow_unignored_out):
        return EXIT_REFUSED_UNIGNORED_OUT
    args.out = _resolved(args.out)
    # One resolver, no fallback: the installed plugin is the single canonical engine (issue #107).
    try:
        scripts = engine_scripts_dir()
    except EngineNotFoundError as exc:
        LOG.error("%s", exc)
        return EXIT_NOTHING_ASSESSED

    env = resolve_env(args.env)
    if not args.skip_download:
        require(env)
    assets_dir = args.out / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    db = args.db or (REPO_ROOT / "_assessment" / "estate.db")
    con = sqlite3.connect(db)
    try:
        todo, selected, project_workbooks, project_datasources, pulled_datasources = scoped_todo(
            con, args.project, args.project_id, args.workbooks_only
        )
    except (sqlite3.OperationalError, ValueError) as exc:
        con.close()
        LOG.error("%s; run assess_estate.py with --survey again before using project scoping", exc)
        return EXIT_NOTHING_ASSESSED
    # Captured while the connection is open, and used only at the END: a datasource that fails to
    # download orphans every workbook bound to it, and the edges are the only thing that knows which.
    try:
        edges = dependency_edges(con)
    except sqlite3.OperationalError as exc:
        # An older estate.db with no dependency/workbook table still harvests fine; it just cannot
        # answer the orphan question, and saying so is better than failing the run over it.
        LOG.warning("cannot read dependency edges (%s); a failed datasource will not be traced to its workbooks", exc)
        edges = []
    con.close()
    if selected:
        LOG.info(
            "project(s) %s selected: %d workbook(s) and %d datasource(s) in project, plus %d datasource(s) "
            "pulled in because a selected workbook binds to them",
            ", ".join(f"'{name}' ({luid})" for luid, name in selected),
            project_workbooks,
            project_datasources,
            pulled_datasources,
        )
    if args.limit:
        todo = todo[: args.limit]
    LOG.info(
        "%d asset(s) to sweep (%d datasource, %d workbook)",
        len(todo),
        sum(1 for t in todo if t[0] == "datasource"),
        sum(1 for t in todo if t[0] == "workbook"),
    )

    results: list[dict] = []
    started = time.perf_counter()
    pending: deque[tuple[int, dict[str, Any], Future[tuple[dict[str, Any], dict[str, Any]]]]] = deque()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="offline-parse") as parser:
        for index, (kind, luid, name) in enumerate(todo, 1):
            target = asset_path(assets_dir, kind, name, luid)
            row: dict[str, Any] = {"name": name, "kind": kind, "luid": luid, "file": str(target)}

            # Ask what LANDED, not what was requested, on BOTH sides of the download: before it so an
            # asset already on disk (any extension, LUID-prefixed or legacy) is not re-fetched at a
            # fresh sign-in, and after it because the fetcher decides the extension, not us.
            actual = existing_asset(assets_dir, kind, name, luid)
            if not args.skip_download and actual is None:
                ok, detail = download(
                    kind,
                    luid,
                    target,
                    env,
                    scripts,
                    timeout=args.download_timeout,
                    stall_timeout=args.download_stall_timeout,
                    label=f"[{index}/{len(todo)}] {name[:46]}",
                )
                if not ok:
                    row["download_error"] = detail
                    results.append(row)
                    LOG.warning("[%d/%d] %-46s DOWNLOAD FAILED %s", index, len(todo), name[:46], detail[:80])
                    continue
                actual = existing_asset(assets_dir, kind, name, luid)

            if actual is None:
                row["download_error"] = "fetcher reported success but no file landed"
                results.append(row)
                continue
            row["file"] = str(actual)
            pending.append((index, row, parser.submit(parse_asset, actual, scripts)))
            LOG.info(
                "[%d/%d] %-46s downloaded %s", index, len(todo), name[:46], progress(len(results), len(todo), started)
            )
            # Drain whatever finished parsing while this asset was downloading, so a verdict lands
            # next to the download it belongs to instead of all of them arriving after the sweep.
            while pending and pending[0][2].done():
                record_parse(pending.popleft(), results, len(todo), started)

        while pending:
            record_parse(pending.popleft(), results, len(todo), started)

    args.out.mkdir(parents=True, exist_ok=True)
    orphans = orphaned_dependents(results, edges)
    text = summarise(results, args.out, orphans)
    LOG.info("\n%s", text[: text.index("## ") if "## " in text else len(text)])
    report_failed_downloads(results, orphans)
    LOG.info(
        "swept %d asset(s) in %.0fs -> %s", len(results), time.perf_counter() - started, args.out / "parse-sweep.md"
    )
    code = sweep_exit_code(results)
    assessed = len(results) - len(never_downloaded(results))
    # The verdict, spelled out beside the tally. An exit code nobody prints is an exit code an
    # operator watching a console never sees, and this run's whole defect was a failure that looked
    # like a success.
    if code == EXIT_NOTHING_ASSESSED:
        LOG.error(
            "NOTHING COULD BE ASSESSED: 0 of %d asset(s) reached a parser, so this sweep says "
            "nothing about the estate. Exit %d.",
            len(results),
            code,
        )
    elif code == EXIT_PARTIAL:
        LOG.warning(
            "PARTIAL HARVEST: %d of %d asset(s) assessed, %d never downloaded. Exit %d — the "
            "failure distribution below covers the %d that landed and NOT the rest.",
            assessed,
            len(results),
            len(results) - assessed,
            code,
            assessed,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
