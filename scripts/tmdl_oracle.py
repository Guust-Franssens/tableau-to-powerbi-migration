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

Locking (issue #415, first-build part only)
--------------------------------------------
`ensure_built()` used to be an unlocked check-then-build: every parallel pytest worker that saw the
DLL missing or stale would launch its own `dotnet build` into the SAME output directory. On Windows
that is file-locking roulette - one or more builds fail even though the source is valid, and the
usual "fix" is a confused rerun that only passes because one process happened to finish the shared
build first. `_acquire_build_lock`/`_release_build_lock` below serialize just that critical section
using the same `O_CREAT|O_EXCL` atomic-file convention `deploy_estate.RunLock` already uses for the
deploy run lock - not a new lock framework, an extension of the one already in this repo. Unlike
`RunLock`, which fails fast because two overlapping deploys must never both proceed, two processes
racing to build the SAME oracle are not a conflict: waiting for the winner and reusing its DLL is the
whole fix, so this variant blocks (bounded) instead of erroring immediately.

Stale-lock reclaim carries the same ownership-safety requirement `.github/skills/pbip-model-refresh`'s
`_ModelLock._reclaim()` was written to satisfy: a bare "old mtime -> unlink" can delete a SUCCESSOR's
brand-new lock if that successor reclaimed and recreated it in the window between our stat and our
unlink. `_reclaim_stale_lock` closes that by claiming the file at a private path via `os.rename` (only
one racer's rename can win) and then verifying, by CONTENT not merely by path, that what it grabbed is
still the lock it judged stale before discarding it - a mismatch means a live successor's fresh lock
was grabbed by accident, and it is restored untouched. Every acquired lock also carries a unique
token, and `_release_build_lock` deletes it only if that token is still there, so a process a
predecessor mistakenly judged stale can never delete a new owner's lock on its own release either.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
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
# `subprocess.run` inside ensure_built already bounds a live build to BUILD_TIMEOUT_SECONDS, so a
# lock file older than that (plus margin) cannot belong to a still-running build - its owner
# crashed. Reclaiming it is the smallest recovery that keeps a dead builder from blocking every
# later run forever.
BUILD_LOCK_STALE_SECONDS = BUILD_TIMEOUT_SECONDS + 30

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


def _read_lock_payload(path: Path) -> dict | None:
    """The lock file's own claim of who holds it, or None if unreadable/torn/gone."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _reclaim_stale_lock(path: Path, observed: dict | None) -> bool:
    """Attempt to reclaim a lock believed stale. Returns True only if it was genuinely discarded.

    `os.rename` alone is NOT enough to make this safe: it moves whatever currently sits at `path`,
    with no idea whether that is still the exact stale lock we stat'd a moment ago or one a
    successor has since reclaimed-and-recreated for itself (that successor went through this same
    function concurrently and won). So after claiming the file at a private path, its content is
    compared against `observed` - what we read as evidence of staleness - before it is discarded.
    A mismatch means we actually grabbed a LIVE successor's fresh lock out from under it; that must
    be put back untouched, never deleted, so a legitimate new owner is never dispossessed and two
    racers can never both believe they hold the mutex.
    """
    claim = path.with_name(f"{path.name}.reclaim.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        os.rename(path, claim)
    except OSError:
        return False  # someone else already claimed/removed it first - nothing to do here
    if observed is not None and _read_lock_payload(claim) == observed:
        # Genuinely the same lock we judged stale by content, not merely by path - safe to discard.
        with contextlib.suppress(OSError):
            os.unlink(claim)
        return True
    # Either unreadable/torn (never confirmed stale) or a successor's fresh lock we grabbed by
    # accident - restore it untouched so its rightful holder, if any, is not dispossessed.
    try:
        os.rename(claim, path)
    except OSError:
        # `path` is occupied again (a second restore race) - our private claim copy is now
        # redundant either way; whatever lives at `path` is not ours to touch.
        with contextlib.suppress(OSError):
            os.unlink(claim)
    return False


def _acquire_build_lock(
    path: Path, *, wait: float = BUILD_LOCK_WAIT_SECONDS, poll: float = BUILD_LOCK_POLL_SECONDS
) -> str:
    """Take `path` as a bounded cross-process mutex for the build/restore critical section only.

    Returns a per-acquisition ownership token that the caller must pass back to
    `_release_build_lock` - a plain unconditional unlink on release would let a process that has
    since been judged stale (see `_reclaim_stale_lock`) delete a SUCCESSOR's lock instead of its own.

    `O_CREAT | O_EXCL` rather than exists()-then-write, same as `deploy_estate.RunLock.acquire` -
    the check-then-act version has a window two simultaneous builders can both pass, which is
    exactly the failure this exists to prevent. Unlike `RunLock`, this blocks (bounded) rather than
    failing on first contention: a second process wanting the SAME build should wait for the
    winner and reuse its DLL, not be told to go away.

    Raises OracleUnavailable on timeout - never hangs, never returns having failed to lock.
    """
    deadline = time.monotonic() + wait
    while True:
        token = uuid.uuid4().hex
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue  # released between our open() failing and this stat - retry immediately
            if age > BUILD_LOCK_STALE_SECONDS:
                # A live build cannot be this old; whatever took this lock is gone - but see
                # `_reclaim_stale_lock` for why that alone does not license an unconditional delete.
                _reclaim_stale_lock(path, _read_lock_payload(path))
                continue
            if time.monotonic() >= deadline:
                raise OracleUnavailable(
                    f"timed out after {wait:.0f}s waiting for {path} - another process appears to "
                    f"be building the TMDL oracle ({age:.0f}s old). If it has crashed, delete "
                    f"{path} and re-run."
                ) from None
            time.sleep(poll)
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(
                {"pid": os.getpid(), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "token": token},
                stream,
            )
        return token


def _release_build_lock(path: Path, token: str) -> None:
    """Remove the lock only if it still carries OUR token.

    A predecessor that judged this run stale (e.g. after a false "crashed" verdict racing with a
    slow write) could have reclaimed and recreated the lock for a new owner; unconditionally
    unlinking would delete a lock we no longer own rather than our own.
    """
    held = _read_lock_payload(path)
    if held is not None and held.get("token") == token:
        with contextlib.suppress(OSError):
            path.unlink()


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
    token = _acquire_build_lock(lock_path)
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
        _release_build_lock(lock_path, token)


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
