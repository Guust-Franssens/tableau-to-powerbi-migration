"""
purpose: TMDL oracle driver - ask the real parser (AMO TmdlSerializer) whether a semantic model
         loads at all, which is the failure issue #254 actually reported.
usage:   imported by scripts/check_datamodel.py; run that, not this.
internal: true
internal-reason: a library with no CLI of its own. `python scripts/check_datamodel.py` is the
                 agent-facing entry point and already carries this gate.

Why an oracle instead of a scanner
----------------------------------
Issue #254 is a model that Power BI Desktop REFUSES TO OPEN: `TMDL Format Error: Unexpected line
type: Other!`, no file named, no line named. Deciding which layouts do that means knowing the TMDL
grammar exactly, and two hand-written attempts at it (a property-name allowlist, then the documented
indentation contract) each shipped false negatives AND false positives - rejecting valid TMDL such as
a case-different `IsHidden` or a second `tablePermission` in one role.

Re-implementing someone else's grammar is a completeness claim, and a completeness claim over a
parser you do not own cannot be finished by patching. So this module makes no such claim. It asks
`TmdlSerializer.DeserializeDatabaseFromFolder` - the parser Desktop itself uses - and reports what it
says. Whatever AMO accepts is accepted here, by construction: false positives are structurally
impossible.

What this deliberately does NOT do
----------------------------------
It does not detect SILENT ABSORPTION - a property written at the wrong indent, swallowed into the
preceding DAX/M while the document stays well-formed. That is not a gap in this implementation, it
is measurably outside what any readback can decide: an absorbed `isHidden` and an `isHidden` that is
ordinary expression content one level deeper produce a BYTE-IDENTICAL parse (same Expression string,
same IsHidden=False). The only carrier of the distinction is source indentation, which the parser
normalises away. Issue #404 tracks it, with the measurement and the three mechanisms already falsified.

Requires the .NET SDK. `scripts/preflight.ps1` checks for it. If it is missing the gate reports
UNASSESSABLE (a distinct exit code) rather than a pass - see check_datamodel.EXIT_UNASSESSABLE.

Locking (issue #415, first-build part only - bounded, fail-closed serialization)
----------------------------------------------------------------------------------
`ensure_built()` used to be an unlocked check-then-build: every parallel pytest worker that saw the
DLL missing or stale would launch its own `dotnet build` into the SAME output directory. On Windows
that is file-locking roulette - one or more builds fail even though the source is valid, and the
usual "fix" is a confused rerun that only passes because one process happened to finish the shared
build first. `_acquire_build_lock`/`_release_build_lock` below serialize just that critical section.

Two earlier revisions used a PATHNAME as the lock: `O_CREAT|O_EXCL` to create it, then read-and-
compare JSON content written into it to decide who owned it and whether it was safe to delete. Both
were TOCTOU-shaped by construction - a lock's identity was a name plus a content comparison, and
between reading that content and acting on it (deleting a stale lock, or checking a token before
unlinking) another process could replace what lived at that name. A reviewer-reproduced sequence
made the second revision's own release routine unlink a SUCCESSOR's freshly-acquired lock. Patching
that again would only add a third compare-then-act step to the same shape.

So this revision does not use the pathname, or anything written inside the file, to decide ownership
at all. `_acquire_build_lock` opens (creating if needed) a PERSISTENT file at `path` and takes an OS
advisory lock on the OPEN FILE DESCRIPTOR itself - `fcntl.flock` on POSIX, a locked byte range via
`msvcrt.locking` on Windows (the two platforms' native primitives for exactly this, chosen once in
`_platform_lock_ops`, not a new hand-rolled protocol). Ownership is then just "do I hold the lock on
the descriptor I have open" - nothing to read, nothing to compare, nothing for another process's
write to race against. A crash simply closes the descriptor, and the OS releases the lock with it;
no separate "is this stale" heuristic is needed, and the pathname is never unlinked by this code at
all (an operator's manual delete-and-retry, named in the timeout message below, is still available,
but the code itself never automates it - see the module's #529/#539 review history). Whatever JSON a
successful acquirer writes into the file afterwards is diagnostics for a human inspecting a stuck
lock - never re-read by any code here, so malformed/binary/non-UTF-8 bytes already on disk cannot
affect locking at all, let alone crash it.

`flock`/`msvcrt.locking` block indefinitely by default, which is unusable for a BOUNDED wait, so
acquisition polls a NON-blocking attempt (`LOCK_NB` / `LK_NBLCK`) against a deadline instead. Timeout
raises `OracleUnavailable` naming the lock path - never hangs, never returns having failed to lock.
Release (`_release_build_lock`) unlocks and closes that same descriptor and nothing else; since two
independent `open()` calls on one path are independent OS lock instances, releasing OUR descriptor
cannot free or otherwise affect a different process's descriptor on the same path, even if the
pathname was deleted and recreated in between (a dedicated regression test reproduces that exact
sequence). Any failure while unlocking or closing is swallowed rather than raised, so a release
never masks whatever `ensure_built` is already propagating - most importantly, a build failure.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tmdl_checks import TmdlFinding

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_DIR = REPO_ROOT / "tools" / "tmdl_oracle"
PROJECT_FILE = PROJECT_DIR / "tmdl_oracle.csproj"
DLL = PROJECT_DIR / "bin" / "Release" / "net9.0" / "tmdl_oracle.dll"

# A first build restores the AMO package from nuget.org; later runs reuse it and take ~2s.
BUILD_TIMEOUT_SECONDS = 600
RUN_TIMEOUT_SECONDS = 600
# Keep a command line comfortably inside the Windows 32 767-character limit.
BATCH_SIZE = 32

# A waiter must be able to outlast a legitimate first build (which restores AMO from nuget.org and
# can legitimately run the whole BUILD_TIMEOUT_SECONDS) without producing a false "unavailable"
# verdict - so the wait ceiling is the build timeout plus a margin, never unbounded.
BUILD_LOCK_WAIT_SECONDS = BUILD_TIMEOUT_SECONDS + 60
BUILD_LOCK_POLL_SECONDS = 0.2

log = logging.getLogger("tmdl_oracle")

_AMO_VERSION_RE = re.compile(r'Include="Microsoft\.AnalysisServices\.NetCore[^"]*"\s+Version="([^"]+)"')


class OracleUnavailable(RuntimeError):
    """The oracle could not be run at all - never confuse this with a clean model."""


def pinned_amo_version() -> str:
    """The AMO version tools/tmdl_oracle is pinned to, read from the csproj itself.

    Single source of truth on purpose: a verdict is only as trustworthy as the parser that produced
    it, so the version is not duplicated into a constant that can drift away from the build.
    """
    match = _AMO_VERSION_RE.search(PROJECT_FILE.read_text(encoding="utf-8"))
    if not match:
        raise OracleUnavailable(f"could not read the pinned AMO version from {PROJECT_FILE}")
    return match.group(1)


def _check_amo_version(reported: str | None) -> None:
    """Refuse a payload that did not come from the pinned parser.

    Without this, anything that prints plausible JSON on stdout - a stub on TMDL_ORACLE_DOTNET, a
    stale build, a machine-wide AMO upgrade - is accepted as an authoritative verdict.
    """
    expected = pinned_amo_version()
    parts = (reported or "").split(".")
    if ".".join(parts[:3]) != expected:
        raise OracleUnavailable(
            f"the TMDL oracle reported AMO version {reported!r}, but tools/tmdl_oracle is pinned to "
            f"{expected}. A verdict from an unknown parser is not evidence; rebuild the helper "
            f"(`dotnet build tools/tmdl_oracle -c Release`) or reconcile the pin."
        )


def dotnet_executable() -> str | None:
    """The `dotnet` the oracle should run under, or None when the SDK is absent."""
    override = os.environ.get("TMDL_ORACLE_DOTNET")
    if override:
        return override if Path(override).exists() else None
    return shutil.which("dotnet")


def _sources_newer_than_build() -> bool:
    """Whether the helper needs rebuilding."""
    if not DLL.exists():
        return True
    stamp = DLL.stat().st_mtime
    return any(source.stat().st_mtime > stamp for source in PROJECT_DIR.glob("*.cs")) or (
        PROJECT_FILE.stat().st_mtime > stamp
    )


def _platform_lock_ops():
    """Return a `(lock, unlock)` pair of callables for the OS's own advisory file locking.

    Resolved through this ONE seam (rather than importing `fcntl`/`msvcrt` at call sites) for two
    reasons: the platform-specific module can only be imported on its own platform, so pylint on
    this repo's Linux runner would flag a top-level `import msvcrt` as unresolvable even though it
    is correctly guarded - `scripts/check_path_ceiling.py`'s `winreg` import uses the same
    function-local pattern for the same reason; and it gives tests a single place to substitute
    both functions (`monkeypatch.setattr(tmdl_oracle, "_platform_lock_ops", ...)`) to deterministically
    simulate a lock/unlock failure on any platform, including this one.
    """
    if sys.platform == "win32":
        import msvcrt  # pylint: disable=import-outside-toplevel,import-error

        def _lock(fd: int) -> None:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

        def _unlock(fd: int) -> None:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    else:
        import fcntl  # pylint: disable=import-outside-toplevel,import-error

        def _lock(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def _unlock(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_UN)

    return _lock, _unlock


def _ensure_lockable_byte(fd: int) -> None:
    """Make sure the open file has at least one byte at offset 0 to lock.

    POSIX `flock` locks the whole file regardless of size, but Windows `msvcrt.locking` locks a
    BYTE RANGE starting at the file's current position, and an empty file has no byte there to lock
    at all. Writing this unconditionally on both platforms (rather than branching) keeps the lock
    file identical everywhere and keeps `_platform_lock_ops` this module's only platform branch.
    """
    if os.fstat(fd).st_size < 1:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)


def _write_diagnostics(fd: int) -> None:
    """Write best-effort diagnostics (pid, timestamp) for a human inspecting a stuck lock.

    NEVER a decision input: nothing in this module ever reads this back to decide ownership (see
    the module docstring), so a write failure here, or the bytes already on disk being unreadable
    garbage from a previous acquisition, must never affect - or be allowed to fail - locking itself.
    """
    with contextlib.suppress(OSError, ValueError):
        payload = json.dumps({"pid": os.getpid(), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}).encode(
            "utf-8"
        )
        os.lseek(fd, 1, os.SEEK_SET)  # offset 0 holds the lock byte itself - never overwritten here
        os.write(fd, payload)
        os.ftruncate(fd, 1 + len(payload))
        os.lseek(fd, 0, os.SEEK_SET)


def _acquire_build_lock(
    path: Path, *, wait: float = BUILD_LOCK_WAIT_SECONDS, poll: float = BUILD_LOCK_POLL_SECONDS
) -> int:
    """Take an OS advisory lock on `path` as a bounded cross-process mutex, returning the held fd.

    The caller must pass the returned descriptor back to `_release_build_lock` - ownership here is
    the OS lock on THIS open file description, never the pathname or any content written into the
    file, so there is nothing for another process to race by replacing what lives at `path` (see
    the module docstring). Blocks (bounded), rather than failing on first contention: a second
    process wanting the SAME build should wait for the winner and reuse its DLL.

    `flock`/`msvcrt.locking` have no built-in timeout, so this polls the NON-blocking variant
    against a deadline. Raises `OracleUnavailable` on timeout, naming the lock path and the manual
    recovery, never hangs, never returns having failed to lock.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR)
    except OSError as exc:
        raise OracleUnavailable(f"could not open the build lock {path}: {exc}") from exc
    try:
        _ensure_lockable_byte(fd)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise OracleUnavailable(f"could not prepare the build lock {path}: {exc}") from exc

    lock, _unlock = _platform_lock_ops()
    deadline = time.monotonic() + wait
    while True:
        try:
            lock(fd)
        except OSError:
            if time.monotonic() >= deadline:
                with contextlib.suppress(OSError):
                    os.close(fd)
                try:
                    age_note = f" ({time.time() - path.stat().st_mtime:.0f}s old)"
                except OSError:
                    age_note = ""
                raise OracleUnavailable(
                    f"timed out after {wait:.0f}s waiting for the build lock {path}{age_note} - "
                    "another process appears to be building the TMDL oracle, or a previous builder "
                    "crashed while holding it. The OS releases a crashed process's lock the instant "
                    f"that process exits; if nothing is actually building, delete {path} and re-run."
                ) from None
            time.sleep(poll)
            continue
        _write_diagnostics(fd)
        return fd


def _release_build_lock(fd: int) -> None:
    """Unlock and close the descriptor `_acquire_build_lock` returned - nothing else.

    Tied solely to that held descriptor, never to the pathname or to any content read from the
    file: two independent `open()` calls on one path are independent OS lock instances, so this can
    never free or otherwise affect a different process's (or a successor's) lock on the same path,
    even if the pathname was deleted and recreated in between (a dedicated regression test
    reproduces that exact sequence). Any failure to unlock or close is swallowed rather than
    raised, so a release never masks whatever `ensure_built` is already propagating - most
    importantly, a build failure.
    """
    _, unlock = _platform_lock_ops()
    with contextlib.suppress(OSError):
        unlock(fd)
    with contextlib.suppress(OSError):
        os.close(fd)


def ensure_built(dotnet: str) -> Path:
    """Build tools/tmdl_oracle once, returning the assembly path.

    Lock-free on the fast path by construction: once a build exists and nothing is newer, this
    returns without touching the lock file at all - only the actual build/restore is the shared
    critical section multiple concurrent `dotnet build` invocations corrupt or fail on Windows
    (issue #415).
    """
    if not _sources_newer_than_build():
        return DLL
    lock_path = PROJECT_DIR / ".oracle-build.lock"
    fd = _acquire_build_lock(lock_path)
    try:
        # Recheck: another process may have finished the build while we waited for the lock - a
        # waiter must reuse that DLL rather than rebuild it.
        if not _sources_newer_than_build():
            return DLL
        log.info("Building the TMDL oracle (tools/tmdl_oracle) - first run also restores AMO from nuget.org.")
        try:
            completed = subprocess.run(
                [dotnet, "build", str(PROJECT_DIR), "-c", "Release", "--nologo", "-v", "quiet"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=BUILD_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OracleUnavailable(f"could not run `dotnet build`: {exc}") from exc
        if completed.returncode != 0 or not DLL.exists():
            raise OracleUnavailable(
                f"`dotnet build {PROJECT_DIR}` failed (exit {completed.returncode}):\n"
                f"{(completed.stdout + completed.stderr).strip()[:2000]}"
            )
        return DLL
    finally:
        _release_build_lock(fd)


def _run_batch(dotnet: str, dll: Path, definitions: list[Path]) -> dict:
    """Invoke the helper over one batch of definition folders."""
    try:
        completed = subprocess.run(
            [dotnet, str(dll), *[str(path) for path in definitions]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OracleUnavailable(f"could not run the TMDL oracle: {exc}") from exc
    if completed.returncode != 0:
        raise OracleUnavailable(
            f"the TMDL oracle exited {completed.returncode}: {(completed.stdout + completed.stderr).strip()[:2000]}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OracleUnavailable(f"the TMDL oracle produced output that is not JSON: {exc}") from exc
    _check_amo_version(payload.get("amoVersion"))
    return payload


def _resolve_document(definition: Path, document: str | None) -> Path:
    """Turn AMO's `./tables/Date` document id into a path a user can open."""
    if not document:
        return definition
    relative = document.lstrip("./").replace("\\", "/")
    candidate = definition / f"{relative}.tmdl"
    return candidate if candidate.exists() else definition


def rejection(model: dict) -> TmdlFinding:
    """Report a model the real parser refuses - the model does not open in Desktop either."""
    definition = Path(model["definition"])
    error = model.get("error") or {}
    return TmdlFinding(
        "TMDL_PARSER_REJECTED",
        f"{error.get('type', 'error')}: {error.get('message', 'no message')}\n      This is the "
        f"verdict of TmdlSerializer, the parser Power BI Desktop uses, so Desktop will not open "
        f"this model either.",
        _resolve_document(definition, error.get("document")),
        int(error.get("line") or 1),
    )


def check_models(model_dirs: list[Path]) -> tuple[list[TmdlFinding], int]:
    """Hand every `.SemanticModel` to the real parser; returns (findings, models inspected).

    Raises OracleUnavailable when the oracle could not run - the caller must NOT read that as a
    pass. Models without a `definition/` folder are skipped and excluded from the count, so
    "nothing was inspected" stays visible instead of masquerading as a clean result.
    """
    dotnet = dotnet_executable()
    if not dotnet:
        raise OracleUnavailable(
            "the .NET SDK is not on PATH. Install it (https://dotnet.microsoft.com/download) or set "
            "TMDL_ORACLE_DOTNET; `scripts/preflight.ps1` checks for it too."
        )
    definitions = [model / "definition" for model in model_dirs if (model / "definition").is_dir()]
    if not definitions:
        return [], 0
    dll = ensure_built(dotnet)
    findings: list[TmdlFinding] = []
    inspected = 0
    for start in range(0, len(definitions), BATCH_SIZE):
        payload = _run_batch(dotnet, dll, definitions[start : start + BATCH_SIZE])
        for model in payload.get("models", []):
            inspected += 1
            if not model.get("ok"):
                findings.append(rejection(model))
    return findings, inspected
