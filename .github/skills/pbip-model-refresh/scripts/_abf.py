"""Cache-file integrity and atomic staged writes for ``refresh_pbip_model``.

A persisted ``<Name>.SemanticModel/.pbi/cache.abf`` is an Analysis Services backup, i.e. a Microsoft
Compound File Binary (OLE2/CFBF) container. This module holds the self-contained primitives that make
persisting one SAFE (issue #113): proving a staged file is a COMPLETE CFBF container before it may
replace a good cache, swapping it in atomically, and restoring a provisional compatibility alignment
when the write does not land. ``refresh_pbip_model._persist_image`` orchestrates these with the
compat/manifest policy layer, and every public name here is re-exported from ``refresh_pbip_model`` so
callers and tests reach them through that module.

Extracted from ``refresh_pbip_model`` so that module stays under its line cap; nothing here depends on
the rest of the bundle, only on ``os``/``struct``/``pathlib``.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

# A persisted `cache.abf` is an Analysis Services backup = a Microsoft Compound File Binary
# (OLE2/CFBF) container. `_is_complete_abf` validates the 512-byte CFBF header STRUCTURE (per MS-CFB),
# not just the 8-byte magic: a torn write can keep the magic yet be rubble past it, and swapping that
# over a good cache is the data loss #113 exists to prevent.
_CFBF_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_CFBF_HEADER_BYTES = 512
_CFBF_BYTE_ORDER_LE = 0xFFFE  # header offset 28: little-endian mark; the only value CFBF permits
_CFBF_MINI_SECTOR_SHIFT = 6  # header offset 32: fixed at 6 (64-byte mini sectors)
# major version (offset 26) -> sector shift (offset 30): v3 = 512-byte sectors, v4 = 4096. No other pairing.
_CFBF_VERSION_TO_SECTOR_SHIFT = {3: 9, 4: 12}


def _is_complete_abf(path: Path) -> bool:
    """Is `path` a fully-written cache.abf, not a truncated or partial one?

    A cache.abf is an Analysis Services backup, i.e. a Microsoft Compound File Binary (OLE2/CFBF)
    container, so this validates the 512-byte CFBF header STRUCTURE against MS-CFB - not merely the
    8-byte magic. A torn write keeps the magic and the first-written header fields yet loses the
    sectors they point at, becoming rubble past the header; checking the fixed fields, whole-sector
    length, and that every referenced sector is inside the file catches that (#113).

    **Fails CLOSED on any uncertainty:** rejecting a valid cache only routes the save to the slower
    UI-Automation fallback, while accepting a truncated one destroys the existing cache.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(_CFBF_HEADER_BYTES)
    except OSError:
        return False
    if size < _CFBF_HEADER_BYTES or len(header) < _CFBF_HEADER_BYTES or header[: len(_CFBF_MAGIC)] != _CFBF_MAGIC:
        return False
    try:
        major = struct.unpack_from("<H", header, 26)[0]
        byte_order = struct.unpack_from("<H", header, 28)[0]
        sector_shift = struct.unpack_from("<H", header, 30)[0]
        mini_shift = struct.unpack_from("<H", header, 32)[0]
        num_fat = struct.unpack_from("<I", header, 44)[0]
        first_dir = struct.unpack_from("<I", header, 48)[0]
        difat = struct.unpack_from("<109I", header, 76)
    except struct.error:
        return False
    if (
        byte_order != _CFBF_BYTE_ORDER_LE
        or mini_shift != _CFBF_MINI_SECTOR_SHIFT
        or _CFBF_VERSION_TO_SECTOR_SHIFT.get(major) != sector_shift
    ):
        return False
    sector_size = 1 << sector_shift
    # A complete container is a whole number of sectors and holds at least the header plus one
    # referenced sector; a non-aligned length or a header-only file is a torn write (rejects the
    # 600-byte and sector-aligned truncations). Every referenced sector must sit inside the file -
    # a truncation keeps these header indices but loses the sectors they name (now past EOF), and
    # num_fat must be 1..109 (a cache.abf never needs the 0 or DIFAT-sector cases we do not walk).
    if size % sector_size != 0 or size < 2 * sector_size:
        return False
    total_sectors = size // sector_size - 1
    return (
        1 <= num_fat <= len(difat)
        and first_dir < total_sectors
        and all(entry < total_sectors for entry in difat[:num_fat])
    )


def _staged_image_write(cache_path: Path, write_image) -> bool:
    """Write the cache to a staging file and swap it in atomically. Returns True only on a COMPLETE write.

    `FileMode.Create` on the live `cache.abf` truncates a good cache the instant the write begins, so
    an ImageSave that then fails half way leaves the project WORSE than before -- no fresh cache and
    no old one. Staging to `cache.abf.tmp` and only `os.replace`-ing it over the original once it is a
    COMPLETE backup means a failed or partial write cannot destroy an existing good cache. Two rules
    make "complete" mean what it says (#113): `write_image` is expected NOT to raise (`image_save`
    absorbs the one benign AMO error and re-raises everything else), so any exception reaching here --
    including from `os.replace` -- is a REAL failure that PROPAGATES after the staging file is removed,
    letting the caller roll the compatibility bump back; and even on a clean return the staged file
    must look like a backup (`_is_complete_abf`) before it is swapped in.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    staging = cache_path.with_name(cache_path.name + ".tmp")
    if staging.exists():
        staging.unlink()
    swapped = False
    try:
        write_image(staging)
        if _is_complete_abf(staging):
            os.replace(staging, cache_path)
            swapped = True
    finally:
        # Never leave a partial cache.abf.tmp behind. `finally` does not suppress the exception, so a
        # real write/replace error still propagates to _persist_image, which rolls the compat back.
        if not swapped and staging.exists():
            staging.unlink()
    return swapped


class CompatRollbackError(RuntimeError):
    """The cache write failed AND the provisional compatibility alignment could not be undone.

    A FATAL, distinct condition (round-3 blocker 2): ``database.tmdl`` now declares a level no cache
    was written at, so the caller MUST NOT fall through to the UI Save (which would persist the
    mismatch) - it must stop and have the level restored from source control.
    """


def _cache_fingerprint(path: Path) -> tuple[int, int] | None:
    """A cheap identity for a cache file - ``(size, mtime_ns)`` - or ``None`` if it does not exist."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def _cache_committed(cache_path: Path, staging: Path, before: tuple[int, int] | None) -> bool:
    """Did the staged write actually replace the cache? Judged by the FILESYSTEM, not a return flag.

    ``os.replace`` is atomic, but a crash could move the file yet still surface an exception
    ("commit-then-raise"). So commit is decided by OBSERVING the result: staging is gone, the cache is
    now a complete ABF, and its fingerprint changed. Trusting only the writer's boolean return
    misclassified that case and rolled compat back UNDER a freshly installed cache (round-3 blocker 2).
    """
    if staging.exists() or not _is_complete_abf(cache_path):
        return False
    return _cache_fingerprint(cache_path) != before


def _cleanup_staging(staging: Path) -> None:
    """Best-effort removal of the staging file; a leftover temp must never mask the real outcome."""
    try:
        if staging.exists():
            staging.unlink()
    except OSError:
        pass


def _restore_rollback_snapshot(snapshot: dict[Path, bytes | None]) -> list[Path]:
    """Restore each snapshotted path to its pre-alignment state ATOMICALLY. Returns the FAILED paths.

    Three properties the round-2 version lacked (round-3 blocker 2): **atomic** (via a sibling
    ``.rollback.tmp`` + ``os.replace``, so a crash mid-restore cannot itself tear ``database.tmdl``);
    **all paths attempted** (the old loop raised on the first failure, leaving a subset reverted); and
    **failures reported, not swallowed** (the returned list lets the caller treat a residual failure
    as FATAL rather than pretend success).
    """
    failures: list[Path] = []
    for path, original in snapshot.items():
        try:
            if original is None:
                if path.exists():
                    path.unlink()
                continue
            if path.exists() and path.read_bytes() == original:
                continue
            tmp = path.with_name(path.name + ".rollback.tmp")
            try:
                tmp.write_bytes(original)
                os.replace(tmp, path)
            finally:
                # A failed os.replace leaves the staging copy behind; never litter the model folder.
                if tmp.exists():
                    tmp.unlink()
        except OSError:
            failures.append(path)
    return failures
