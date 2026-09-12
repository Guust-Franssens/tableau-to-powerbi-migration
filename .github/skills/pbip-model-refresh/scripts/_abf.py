"""Cache-file integrity and atomic staged writes for ``refresh_pbip_model``.

A persisted ``<Name>.SemanticModel/.pbi/cache.abf`` is an Analysis Services backup: a UTF-16LE
preamble followed by a chain of XPress9-compressed blocks (see ``_abf_rejection_reason`` for the
measured layout). This module holds the self-contained primitives that make persisting one SAFE
(issue #113): proving a staged file is a COMPLETE backup before it may replace a good cache, swapping
it in atomically, and restoring a provisional compatibility alignment when the write does not land.
``refresh_pbip_model._persist_image`` orchestrates these with the compat/manifest policy layer, and
every public name here is re-exported from ``refresh_pbip_model`` so callers and tests reach them
through that module.

Extracted from ``refresh_pbip_model`` so that module stays under its line cap. The observation-request
guard is shared with the bundled probe; cache validation itself requires no native libraries.
"""

from __future__ import annotations

import os
import hashlib
import struct
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from probe_desktop_query import _validate_observation_request

# THE FORMAT, MEASURED - not assumed. An earlier round asserted a cache.abf was a Microsoft Compound
# File Binary (OLE2/CFBF, magic `D0 CF 11 E0 ...`). It is NOT, and the cost of that guess was total:
# `_is_complete_abf` returned False for every real cache, so the staged write NEVER swapped and
# persist-by-default silently stopped working, falling back to the UI save on every single run.
# Ground truth, from all 13 `cache.abf` files on this machine (17,478 B -> 60,688,851 B; 1 -> 47
# blocks; written by Power BI Desktop AND by this script's own ImageSave):
#
#   0..99    UTF-16LE "This backup was created using XPress9 compression." (exactly 100 bytes)
#   100..101 uint16 pad, 0 in all 13
#   102..    block chain, each block: uint32 uncompressedBytes, uint32 blockBytes, 4-byte magic,
#            then payload. `blockBytes` is measured FROM THE MAGIC, so the next block header starts
#            at (headerOffset + 8 + blockBytes). The chain ends EXACTLY at EOF in all 13 files.
_ABF_PREAMBLE = "This backup was created using XPress9 compression.".encode("utf-16-le")
_ABF_FIRST_BLOCK_OFFSET = len(_ABF_PREAMBLE) + 2  # 100-byte preamble + the 2-byte pad
_ABF_BLOCK_MAGIC = b"\x2a\xd7\x86\x4e"
_ABF_BLOCK_HEADER_BYTES = 8 + len(_ABF_BLOCK_MAGIC)  # uint32 uncompressed + uint32 length + magic
# Every non-final block measured declares exactly 2 MiB uncompressed, and every final block declares
# less. That fixed interior size is load-bearing: without it, a write truncated exactly on a block
# boundary can land cleanly on EOF and look complete.
_ABF_MAX_BLOCK_BYTES = 2 * 1024 * 1024
_ABF_READ_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ImageObservation:
    """Checked stage/readback facts, without a proven native durable-write barrier."""

    intended_sha256: str
    intended_size: int
    installed_sha256: str
    installed_size: int
    commitment: Literal["UNESTABLISHED"] = field(default="UNESTABLISHED", init=False)


@dataclass
class _ImageInstallation:
    """Transaction control only: a returned replace is independent of its optional observation."""

    installed: bool = False
    size: int = 0
    ambiguous: bool = False


def _checked_image(path: Path) -> tuple[str, int]:
    """Validate and hash one sequential pass, including pad/payload/EOF, in bounded memory.

    The returned facts are unavailable until the reader's context exits successfully. File size is
    only a bound: consuming every declared byte and checking EOF must independently agree with it.
    """
    with path.open("rb") as handle:
        size = path.stat().st_size
        digest = hashlib.sha256()
        prefix = handle.read(_ABF_FIRST_BLOCK_OFFSET)
        digest.update(prefix)
        if len(prefix) != _ABF_FIRST_BLOCK_OFFSET or prefix[: len(_ABF_PREAMBLE)] != _ABF_PREAMBLE:
            raise ValueError("not a complete AS backup preamble")
        reason = _walk_abf_blocks(handle, size, digest=digest)
        if reason is not None:
            raise ValueError(reason)
        checked = digest.hexdigest(), size
    return checked


def _abf_rejection_reason(path: Path) -> str | None:
    """Why `path` is not a complete cache.abf, or ``None`` when it is one.

    Walks the block chain described above and requires it to land EXACTLY on EOF. That is what makes
    a partial write detectable: a torn write leaves a block header whose declared length runs past
    the end of the file, and only a complete file consumes itself precisely.

    Returns the reason rather than a bare bool so a rejection is DIAGNOSABLE. The CFBF bug above was
    invisible for a whole round precisely because the predicate could only say "no"; a caller that
    prints "preamble mismatch (got 54 00 68 00 ...)" names the defect in one run.

    **Fails CLOSED on any uncertainty:** rejecting a valid cache only routes the save to the slower
    UI-Automation fallback (and now says why), while accepting a truncated one destroys the existing
    cache. The one known false reject that policy buys is an image whose uncompressed size is an exact
    multiple of 2 MiB, whose final block would then be full-sized; none of the 13 measured files is
    such a file, and the cost if one appears is the fallback, not data loss.
    """
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            preamble = handle.read(len(_ABF_PREAMBLE))
            if preamble != _ABF_PREAMBLE:
                return f"not an AS backup preamble (first {len(preamble)} byte(s): {preamble[:16].hex(' ')})"
            return _walk_abf_blocks(handle, size)
    except OSError as exc:
        return f"unreadable ({type(exc).__name__})"


def _abf_block_header_problem(header: bytes, ordinal: int, offset: int, size: int) -> str | None:
    """Why this block header is not well formed, or None. Split out to keep the chain walk flat."""
    if len(header) < _ABF_BLOCK_HEADER_BYTES:
        return f"truncated inside block {ordinal}'s header at offset {offset} (size {size})"
    if header[8:] != _ABF_BLOCK_MAGIC:
        return f"block {ordinal} at offset {offset} has magic {header[8:].hex(' ')}, not an AS block"
    uncompressed, length = struct.unpack_from("<II", header, 0)
    if not 0 < uncompressed <= _ABF_MAX_BLOCK_BYTES:
        return f"block {ordinal} declares {uncompressed} uncompressed byte(s) (max {_ABF_MAX_BLOCK_BYTES})"
    if length <= len(_ABF_BLOCK_MAGIC):
        return f"block {ordinal} declares {length} byte(s), too short to hold any payload"
    return None


def _hash_abf_payload(handle, digest, remaining: int) -> str | None:
    """Consume exactly this payload, without seeking, retaining chunks or trusting a stat alone."""
    while remaining:
        chunk = handle.read(min(remaining, _ABF_READ_BYTES))
        digest.update(chunk)
        if not chunk or len(chunk) > remaining:
            return "payload byte count disagrees with the declared block length"
        remaining -= len(chunk)
    return None


def _walk_abf_blocks(handle, size: int, *, digest=None) -> str | None:
    """Check the same block chain in both modes: legacy header seeks, or sequential hashing."""
    offset = _ABF_FIRST_BLOCK_OFFSET
    blocks = 0
    while True:
        if digest is None:
            handle.seek(offset)
        header = handle.read(_ABF_BLOCK_HEADER_BYTES)
        if digest is not None:
            digest.update(header)
        problem = _abf_block_header_problem(header, blocks + 1, offset, size)
        if problem is not None:
            return problem
        uncompressed, length = struct.unpack_from("<II", header, 0)
        blocks += 1
        end = offset + 8 + length
        if end > size:
            return f"block {blocks} needs {end} byte(s) but the file is {size} - truncated write"
        if digest is not None:
            problem = _hash_abf_payload(handle, digest, length - len(_ABF_BLOCK_MAGIC))
            if problem is not None:
                return problem
        if end < size:
            if uncompressed != _ABF_MAX_BLOCK_BYTES:
                return (
                    f"block {blocks} is non-final but declares {uncompressed} uncompressed byte(s), "
                    f"not the required {_ABF_MAX_BLOCK_BYTES}"
                )
            offset = end
            continue
        if uncompressed == _ABF_MAX_BLOCK_BYTES:
            problem = (
                f"ends on a full {_ABF_MAX_BLOCK_BYTES}-byte block boundary after {blocks} block(s) - more was due"
            )
        elif digest is not None:
            trailer = handle.read(1)
            digest.update(trailer)
            if trailer:
                problem = "backup has bytes beyond its declared size"
        return problem


def _is_complete_abf(path: Path) -> bool:
    """Is `path` a fully-written cache.abf, not a truncated or partial one? See `_abf_rejection_reason`."""
    return _abf_rejection_reason(path) is None


def _staging_path(cache_path: Path) -> Path:
    """A per-run staging filename, unique across concurrent runs against the SAME cache (issue #114).

    Every run used to stage to one fixed ``cache.abf.tmp``. Two runs against a single model then
    shared that path, and :func:`_staged_image_write`'s own ``if staging.exists(): unlink`` (below)
    would delete the OTHER run's in-flight staging file. The victim run, seeing its staging gone,
    concludes it did not commit and rolls its compatibility bump back UNDER the other run's freshly
    written cache - leaving ``database.tmdl`` declaring a level no present cache matches, the #114
    brick. Tagging the name with the PID and a random token makes each run's staging private, so no
    run can disturb another's. That is necessary but NOT sufficient: the compatibility edit on
    ``database.tmdl`` is also shared, so the whole transaction is additionally serialised by the
    per-model lock in ``_lock``. Both defences are needed.
    """
    return cache_path.with_name(f"{cache_path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")


def _staged_image_write(  # pylint: disable=too-many-arguments
    cache_path: Path,
    write_image,
    staging: Path | None = None,
    *,
    observations: list[ImageObservation] | None = None,
    installation: _ImageInstallation | None = None,
    return_observation: bool = False,
) -> bool | tuple[str, int]:
    """Write the cache to a staging file and swap it in atomically. Returns True only on a COMPLETE write.

    `FileMode.Create` on the live `cache.abf` truncates a good cache the instant the write begins, so
    an ImageSave that then fails half way leaves the project WORSE than before -- no fresh cache and
    no old one. Staging to a PER-RUN file (`_staging_path`, unique per process so concurrent runs
    cannot delete each other's -- issue #114) and only `os.replace`-ing it over the original once it
    is a COMPLETE backup means a failed or partial write cannot destroy an existing good cache. Two
    rules make "complete" mean what it says (#113): `write_image` is expected NOT to raise
    (`image_save` absorbs the one benign AMO error and re-raises everything else), so any exception
    reaching here -- including from `os.replace` -- is a REAL failure that PROPAGATES after the
    staging file is removed, letting the caller roll the compatibility bump back; and even on a clean
    return the staged file must look like a backup (`_is_complete_abf`) before it is swapped in.

    Observation mode returns only the checked staged digest and byte count, never installed evidence.
    The caller must declare compatibility, check the installed bytes once, and finish its contexts.
    A normal replace return establishes installation, not durability. A raised replace never produces
    an observation; if its stage
    vanished, the outcome is ambiguous and the caller must not roll compatibility back underneath a
    possibly installed image. No durable commitment is inferred from close/replace/readback.

    A staged file that is REJECTED is announced, not swallowed. A silent "did not swap" is how a
    wrong acceptance predicate hid for a whole review round while persist-by-default was dead.
    """
    _validate_observation_request(return_observation, observations)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if staging is None:
        staging = _staging_path(cache_path)
    if staging.exists():
        staging.unlink()
    installation = installation if installation is not None else _ImageInstallation()
    intended = None
    try:
        write_image(staging)
        if return_observation:
            intended = _checked_image(staging)
            reason = None
        else:
            reason = _abf_rejection_reason(staging)
        if reason is None:
            installation.size = intended[1] if return_observation else staging.stat().st_size
            # Cover an interrupt after replace returns but before its success flag is stored.
            installation.ambiguous = True
            try:
                os.replace(staging, cache_path)
            except BaseException:
                installation.ambiguous = True
                installation.ambiguous = not staging.exists()
                raise
            installation.installed = True
            installation.ambiguous = False
        else:
            print(f"  save   : staged cache REJECTED - {reason}")
    finally:
        # Cleanup cannot establish installation: the caller retains the pre-cleanup outcome.
        if not installation.installed:
            _cleanup_staging(staging)
    return intended if return_observation else installation.installed


class CompatRollbackError(RuntimeError):
    """The cache/compatibility state cannot safely be reconciled automatically.

    A failed rollback or an ambiguous replacement must not fall through to UI Save. Observation
    failure after an established installation also uses this refusal without undoing the alignment.
    """


def _cleanup_staging(staging: Path) -> None:
    """Best-effort removal of the staging file; a leftover temp must never mask the real outcome."""
    try:
        if staging.exists():
            staging.unlink()
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def _snapshot_rollback_paths(paths: list[Path]) -> dict[Path, tuple[bytes, int, int] | None]:
    """Capture each path's CONTENT **and TIMESTAMPS**, or ``None`` where the file does not exist yet.

    The timestamps are not tidiness. Power BI Desktop discards a ``cache.abf`` that is OLDER than the
    model definition beside it (SKILL.md: "NO_DATA despite a 113 KB cache sitting right there"). A
    rollback that rewrites ``database.tmdl`` byte-for-byte but with a fresh mtime therefore leaves the
    definition NEWER than the good cache the failed persist was supposed to protect - silently arming
    the very cache-discard the bundle exists to prevent, while every byte-level check says "unchanged".
    """
    snapshot: dict[Path, tuple[bytes, int, int] | None] = {}
    for path in paths:
        try:
            stat = path.stat()
            snapshot[path] = (path.read_bytes(), stat.st_atime_ns, stat.st_mtime_ns)
        except OSError:
            snapshot[path] = None
    return snapshot


def _restore_rollback_snapshot(snapshot: dict[Path, tuple[bytes, int, int] | None]) -> list[Path]:
    """Restore each snapshotted path to its pre-alignment state ATOMICALLY. Returns the FAILED paths.

    Four properties (round-3 blocker 2, plus round-4's mtime finding): **atomic** (via a sibling
    ``.rollback.tmp`` + ``os.replace``, so a crash mid-restore cannot itself tear ``database.tmdl``);
    **all paths attempted** (the old loop raised on the first failure, leaving a subset reverted);
    **failures reported, not swallowed** (the returned list lets the caller treat a residual failure
    as FATAL rather than pretend success); and **timestamps restored too**, so "unchanged" means
    unchanged to Desktop's cache-freshness comparison as well as to a byte diff
    (see :func:`_snapshot_rollback_paths`).
    """
    failures: list[Path] = []
    for path, original in snapshot.items():
        try:
            if original is None:
                if path.exists():
                    path.unlink()
                continue
            content, atime_ns, mtime_ns = original
            if not path.exists() or path.read_bytes() != content:
                tmp = path.with_name(path.name + ".rollback.tmp")
                try:
                    tmp.write_bytes(content)
                    os.replace(tmp, path)
                finally:
                    # A failed os.replace leaves the staging copy behind; never litter the model folder.
                    if tmp.exists():
                        tmp.unlink()
            os.utime(path, ns=(atime_ns, mtime_ns))
        except OSError:
            failures.append(path)
    return failures
