"""The persisted cache write must be atomic, and a failed persist must not leave the project bumped.

Four guarantees this file pins:

1. **Atomic swap (#113).** `ImageSave` opens `cache.abf` with `FileMode.Create`, which TRUNCATES a
   good cache the instant the write begins - so a write that then fails half way leaves the project
   WORSE than before (no fresh cache and no old one). `_staged_image_write` writes a per-run staging
   file and only `os.replace`s it over the original once it exists and is a COMPLETE ABF backup, so a
   failed or partial write can never destroy an existing good cache. A raise, or a clean return that
   produced only partial bytes, both preserve the existing cache (#113, round-2 blocker 1).
2. **Compat rollback (#113).** Saving raises `database.tmdl`'s declared compatibilityLevel to the live
   level. That edit is written eagerly (so the serialized cache matches the project), but it is
   PROVISIONAL: if the ImageSave that follows does not land, `_persist_image` restores
   `database.tmdl` (and the generated-edit ledger) exactly, so a mid-failure never leaves the model
   declaring a level that was never actually written to a cache.
3. **Interrupt-safe rollback (#113 route 2).** The rollback in (2) must run when the write is
   INTERRUPTED, not only when it fails with an ordinary exception. `KeyboardInterrupt` is a
   `BaseException`, not an `Exception`, so a Ctrl+C during alignment or ImageSave used to propagate
   straight past BOTH the commit check and the rollback, leaving `database.tmdl` bumped for a cache
   that was never written - the same brick by a different route. `_persist_image` now catches
   `BaseException`, rolls back, and re-raises the interrupt; or, if the rollback ITSELF fails, raises
   `CompatRollbackError` in its place (a bricked project matters more than a tidy Ctrl+C).
4. **Concurrency-safe persist (#114).** Two runs against one model must not corrupt each other. They
   shared a single fixed `cache.abf.tmp` staging file AND the provisional compat edit on
   `database.tmdl`; a hostile interleaving deleted one run's staging, made it roll compat back under
   the other's freshly written cache, and left compat declaring a level no present cache matched.
   Staging is now a per-run PRIVATE name (`_staging_path`), and the WHOLE transaction is serialised by
   a per-model interprocess lock (`_lock.model_lock`) whose dead-holder locks are reclaimed rather
   than wedging the tool forever. Both defences are needed - unique names alone leave the shared
   compat edit unprotected.

Plus a docs-vs-code guard: the SKILL.md frontmatter's persistence default must match the argparse
default, so the two cannot silently drift again (the #113 doc bug: frontmatter said "opt-in" while
the code persisted by default).
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

# `conftest.py` next to this file puts the skill's own `scripts/` on `sys.path`.
# ruff: noqa: E402  (the conftest-provided path must be in place before these imports)
import refresh_pbip_model

SKILL_ROOT = Path(__file__).resolve().parents[1]


def _model(root: Path, name: str = "MyMigration", compat: int | None = None) -> Path:
    """A minimal `<Name>.SemanticModel` on disk; returns the `cache.abf` destination inside it."""
    definition = root / f"{name}.SemanticModel" / "definition"
    (definition / "tables").mkdir(parents=True)
    (definition / "tables" / "Orders.tmdl").write_text("table 'Orders'\n\n\tcolumn X\n", encoding="utf-8")
    if compat is not None:
        (definition / "database.tmdl").write_text(f"database\n\tcompatibilityLevel: {compat}\n", encoding="utf-8")
    return root / f"{name}.SemanticModel" / ".pbi" / "cache.abf"


# The REAL cache.abf format, restated here independently of the code under test.
#
# Round 3 asserted a cache.abf was a Microsoft Compound File Binary and THESE FIXTURES synthesised
# CFBF containers - so the suite agreed with the code and both were wrong. `_is_complete_abf` accepted
# 0 of the 13 real caches on the machine that produced them, `_staged_image_write` therefore never
# swapped, and persist-by-default (the entire subject of #113) silently stopped working on every run
# (round-4 blocker 1). The lesson is in where these constants come from: they are hard-coded from a
# hex dump of real caches, NOT imported from `refresh_pbip_model`, because a fixture built out of the
# predicate's own constants can only ever confirm the predicate - never contradict it.
#
#   0..99    UTF-16LE "This backup was created using XPress9 compression." (exactly 100 bytes)
#   100..101 uint16 pad (0 in all 13 measured files)
#   102..    block chain; each block is uint32 uncompressedBytes, uint32 lengthFromTheMagic,
#            4-byte magic, payload. Next header = offset + 8 + length. The chain ends EXACTLY at EOF.
_ABF_PREAMBLE = "This backup was created using XPress9 compression.".encode("utf-16-le")
_ABF_BLOCK_MAGIC = b"\x2a\xd7\x86\x4e"
_ABF_MAX_BLOCK_BYTES = 2 * 1024 * 1024
# Bytes 100..113 of a REAL cache written by this toolkit's OWN ImageSave - `health-tracker`,
# 116,237 B, the very artefact `refresh_pbip_model`'s docstring cites as its ImageSave proof. The
# golden test below rebuilds those 14 bytes from the builder, so a builder that drifts from the real
# format fails loudly instead of quietly re-inventing round 3's mistake.
_REAL_ABF_HEADER_HEX = "000000c00b009fc501002ad7864e"
_REAL_ABF_UNCOMPRESSED = 770_048
_REAL_ABF_PAYLOAD_BYTES = 116_123
_REAL_ABF_TOTAL_BYTES = 116_237


def _abf_bytes(blocks: tuple[tuple[int, int], ...] = ((770_048, 500),), seed: int = 0x5A) -> bytes:
    """A complete Analysis Services backup image: preamble, pad, then `(uncompressed, payload)` blocks.

    `seed` varies the payload so two valid images can be told apart byte-for-byte.
    """
    blob = bytearray(_ABF_PREAMBLE)
    blob += struct.pack("<H", 0)
    for index, (uncompressed, payload_bytes) in enumerate(blocks):
        blob += struct.pack("<II", uncompressed, len(_ABF_BLOCK_MAGIC) + payload_bytes)
        blob += _ABF_BLOCK_MAGIC
        blob += bytes(((seed + index + i) % 256 for i in range(payload_bytes)))
    return bytes(blob)


def _cfbf_bytes() -> bytes:
    """A genuine, minimal 3-sector Microsoft Compound File Binary - kept ONLY as a negative control.

    Round 3 believed this was what a cache.abf looks like. It is not, and a test that pins the
    REJECTION of a structurally valid CFBF is what stops that belief coming back: any future predicate
    that starts accepting compound files has re-acquired the round-3 defect.
    """
    sector = 512
    endofchain, freesect, fatsect, nostream = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF

    header = bytearray(sector)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 24, 0x003E)  # minor version
    struct.pack_into("<H", header, 26, 3)  # major version 3 -> 512-byte sectors
    struct.pack_into("<H", header, 28, 0xFFFE)  # byte-order mark
    struct.pack_into("<H", header, 30, 9)  # sector shift (1<<9 == 512)
    struct.pack_into("<H", header, 32, 6)  # mini sector shift (fixed)
    struct.pack_into("<I", header, 44, 1)  # num FAT sectors
    struct.pack_into("<I", header, 48, 1)  # first directory sector -> sector 1
    struct.pack_into("<I", header, 56, 4096)  # mini-stream cutoff
    struct.pack_into("<I", header, 60, endofchain)  # first mini-FAT sector
    struct.pack_into("<I", header, 68, endofchain)  # first DIFAT sector
    for i in range(109):  # DIFAT: FAT lives at sector 0, the rest are free
        struct.pack_into("<I", header, 76 + i * 4, 0 if i == 0 else freesect)

    fat = bytearray(sector)
    struct.pack_into("<I", fat, 0, fatsect)  # sector 0 is the FAT itself
    struct.pack_into("<I", fat, 4, endofchain)  # sector 1 (directory) ends its chain
    for i in range(2, sector // 4):
        struct.pack_into("<I", fat, i * 4, freesect)

    directory = bytearray(sector)
    name = "Root Entry".encode("utf-16-le")
    directory[0 : len(name)] = name
    struct.pack_into("<H", directory, 64, len(name) + 2)  # name length incl. terminating NUL
    directory[66] = 5  # object type: root storage
    directory[67] = 1  # colour: black
    struct.pack_into("<I", directory, 68, nostream)  # left sibling
    struct.pack_into("<I", directory, 72, nostream)  # right sibling
    struct.pack_into("<I", directory, 76, nostream)  # child
    struct.pack_into("<I", directory, 116, endofchain)  # mini-stream start sector
    return bytes(header) + bytes(fat) + bytes(directory)


def _real_cache_abf_files() -> list[Path]:
    """Real `cache.abf` files on this machine, smallest first - the ground-truth corpus, or [].

    Real caches are gitignored (`.gitignore`: `**/.pbi/cache.abf`), so they are ground truth when
    present and simply absent in a clean clone or when this bundle is copied elsewhere - hence the
    tests that use them SKIP rather than fail, which is what keeps the bundle portable. Set
    `PBIP_REFRESH_REAL_ABF` to a cache.abf (or a directory holding some) to point the corpus anywhere.
    """
    override = os.environ.get("PBIP_REFRESH_REAL_ABF")
    found: list[Path] = []
    if override:
        target = Path(override)
        found = [target] if target.is_file() else sorted(target.rglob("cache.abf"))
    else:
        for ancestor in SKILL_ROOT.parents:
            roots = [ancestor / name for name in ("examples", "migrations", "_probe-lab")]
            if not any(root.is_dir() for root in roots):
                continue
            for root in roots:
                if root.is_dir():
                    found.extend(root.rglob(".pbi/cache.abf"))
            break
    return sorted((path for path in found if path.is_file()), key=lambda path: path.stat().st_size)


def _valid_abf_bytes() -> bytes:
    """Bytes that pass `_is_complete_abf`: a small but structurally REAL Analysis Services backup."""
    return _abf_bytes()


def _no_staging_files(cache: Path) -> bool:
    """No per-run staging file may be left behind in the cache directory.

    Staging names are now UNIQUE per run (`cache.abf.<pid>-<token>.tmp`, #114) so two runs can't
    delete each other's, so this globs the directory for any leftover `*.tmp` rather than checking a
    single fixed name - a check against the old fixed `cache.abf.tmp` would silently pass while a
    unique-named staging file leaked.
    """
    return not list(cache.parent.glob("*.tmp"))


def test_the_builder_reproduces_a_real_cache_abf_header() -> None:
    """The fixtures' oracle: rebuild a REAL cache's first block header and total size, byte for byte.

    Without this, `_abf_bytes` and `_abf_rejection_reason` could drift together into a second private
    format nobody has ever written - exactly how round 3's CFBF fixtures kept a broken predicate
    green. The numbers come from `health-tracker`'s 116,237-byte cache, written by this script's own
    ImageSave: block 1 declares 770,048 uncompressed bytes and 116,127 bytes from the magic onward.
    """
    blob = _abf_bytes(((_REAL_ABF_UNCOMPRESSED, _REAL_ABF_PAYLOAD_BYTES),))
    assert blob[:100] == _ABF_PREAMBLE
    assert blob[100:114].hex() == _REAL_ABF_HEADER_HEX
    assert len(blob) == _REAL_ABF_TOTAL_BYTES


def test_is_complete_abf_accepts_a_single_block_backup(tmp_path: Path) -> None:
    """The positive control the old suite never had: a real-format backup is ACCEPTED."""
    good = tmp_path / "cache.abf"
    good.write_bytes(_abf_bytes())
    assert refresh_pbip_model._is_complete_abf(good) is True


def test_is_complete_abf_accepts_a_multi_block_backup(tmp_path: Path) -> None:
    """A chain of blocks is walked to EOF: full 2 MiB blocks, then a short final one (the measured shape)."""
    good = tmp_path / "cache.abf"
    good.write_bytes(_abf_bytes(((_ABF_MAX_BLOCK_BYTES, 4096), (_ABF_MAX_BLOCK_BYTES, 4096), (131_072, 2048))))
    assert refresh_pbip_model._is_complete_abf(good) is True


def test_is_complete_abf_rejects_a_compound_file_binary(tmp_path: Path) -> None:
    """The round-3 regression pin: a structurally valid CFBF is NOT a cache.abf and must be rejected.

    Round 3 asserted the opposite and shipped a predicate that accepted only compound files - which no
    real cache is - so the staged write never swapped. If this test ever starts failing, the predicate
    has re-acquired that defect.
    """
    blob = tmp_path / "cache.abf"
    blob.write_bytes(_cfbf_bytes())
    assert refresh_pbip_model._is_complete_abf(blob) is False
    assert "preamble" in (refresh_pbip_model._abf_rejection_reason(blob) or "")


def test_is_complete_abf_rejects_a_preamble_only_file(tmp_path: Path) -> None:
    """The preamble alone is a write that died before its first block header."""
    stub = tmp_path / "cache.abf"
    stub.write_bytes(_ABF_PREAMBLE + b"\x00\x00")
    assert refresh_pbip_model._is_complete_abf(stub) is False


def test_is_complete_abf_rejects_a_payload_truncation(tmp_path: Path) -> None:
    """A block whose declared length runs past EOF is a torn write - the case the predicate exists for."""
    truncated = tmp_path / "cache.abf"
    truncated.write_bytes(_abf_bytes()[:-1])
    assert refresh_pbip_model._is_complete_abf(truncated) is False
    assert "truncated" in (refresh_pbip_model._abf_rejection_reason(truncated) or "")


def test_is_complete_abf_rejects_a_truncation_on_a_block_boundary(tmp_path: Path) -> None:
    """The truncation a chain-walk ALONE cannot see: the file ends exactly where a block ends.

    Every one of the 60 measured non-final blocks declares a full 2 MiB uncompressed and every one of
    the 13 final blocks declares less, so a file whose LAST block is full-sized stopped on a chunk
    boundary with more still due. Dropping that rule is a mutation the suite must catch: without it
    this file walks cleanly to EOF and is accepted.
    """
    full = _abf_bytes(((_ABF_MAX_BLOCK_BYTES, 4096), (131_072, 2048)))
    boundary = tmp_path / "cache.abf"
    boundary.write_bytes(full[: 102 + 8 + 4 + 4096])
    assert refresh_pbip_model._is_complete_abf(boundary) is False
    assert "boundary" in (refresh_pbip_model._abf_rejection_reason(boundary) or "")


@pytest.mark.parametrize("chunk", (512 * 1024, 1024 * 1024, _ABF_MAX_BLOCK_BYTES, 4 * 1024 * 1024))
def test_boundary_truncated_abf_is_rejected_for_review_chunk_sizes(tmp_path: Path, chunk: int) -> None:
    """Pin the four measured boundary truncations: 512 KiB, 1 MiB, 2 MiB, and 4 MiB chunks."""
    boundary = tmp_path / "cache.abf"
    boundary.write_bytes(_abf_bytes(((chunk, 64), (chunk, 64), (chunk, 64))))
    assert refresh_pbip_model._is_complete_abf(boundary) is False


def test_every_proper_prefix_of_a_valid_backup_is_rejected(tmp_path: Path) -> None:
    """Exhaustive: for a two-block image, EVERY prefix short of the whole file is a partial write.

    A truncated write can stop at any byte, so spot-checking a few lengths proves little. Only the
    complete file may be accepted.
    """
    blob = _abf_bytes(((_ABF_MAX_BLOCK_BYTES, 700), (65_536, 300)))
    probe = tmp_path / "cache.abf"
    for length in range(len(blob)):
        probe.write_bytes(blob[:length])
        assert refresh_pbip_model._is_complete_abf(probe) is False, f"prefix of {length} byte(s) was accepted"
    probe.write_bytes(blob)
    assert refresh_pbip_model._is_complete_abf(probe) is True


def test_the_rejection_reason_names_the_defect(tmp_path: Path) -> None:
    """A rejection must SAY why. Round 3's bug survived a whole round because the predicate could only
    say "no": every run silently fell back to the UI save with no clue as to the cause."""
    blob = tmp_path / "cache.abf"
    blob.write_bytes(b"not a backup at all")
    reason = refresh_pbip_model._abf_rejection_reason(blob)
    assert reason and "preamble" in reason
    assert refresh_pbip_model._abf_rejection_reason(tmp_path / "cache.abf") is not None
    good = tmp_path / "good.abf"
    good.write_bytes(_abf_bytes())
    assert refresh_pbip_model._abf_rejection_reason(good) is None


def test_a_rejected_staged_write_prints_the_reason(tmp_path: Path, capsys) -> None:
    """The reason has to reach the operator, not just the function's caller."""
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"GOOD-EXISTING-CACHE")

    def write_rubbish(staging: Path) -> None:
        staging.write_bytes(b"rubbish")

    assert refresh_pbip_model._staged_image_write(cache, write_rubbish) is False
    assert "REJECTED" in capsys.readouterr().out
    assert cache.read_bytes() == b"GOOD-EXISTING-CACHE"


def test_every_real_cache_abf_on_this_machine_is_accepted() -> None:
    """Ground truth. The corpus that disproved round 3: every real cache must be ACCEPTED.

    Round 3's predicate scored 0 out of 13 here, and no synthetic fixture could have shown that -
    which is why this test reads the artefacts Desktop and this very script actually produced.
    """
    corpus = _real_cache_abf_files()
    if not corpus:
        pytest.skip("no real cache.abf on this machine (they are gitignored); set PBIP_REFRESH_REAL_ABF")
    rejected = {str(path): refresh_pbip_model._abf_rejection_reason(path) for path in corpus}
    assert not [path for path, reason in rejected.items() if reason], rejected


def test_a_real_cache_abf_is_staged_and_swapped_in(tmp_path: Path) -> None:
    """End to end on a REAL cache: the staged write must actually commit it.

    This is the failure the round-3 predicate produced - not a crash, but `_staged_image_write`
    returning False forever, so `_persist_image` always reported "not persisted" and every run fell
    back to the UI save. Copies the real bytes into tmp_path; the source cache is never touched.
    """
    corpus = _real_cache_abf_files()
    if not corpus:
        pytest.skip("no real cache.abf on this machine (they are gitignored); set PBIP_REFRESH_REAL_ABF")
    real_bytes = corpus[0].read_bytes()
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"OLD-CACHE")

    def write_real(staging: Path) -> None:
        staging.write_bytes(real_bytes)

    assert refresh_pbip_model._staged_image_write(cache, write_real) is True
    assert cache.read_bytes() == real_bytes
    assert _no_staging_files(cache)


def test_a_truncated_real_cache_abf_is_rejected_and_the_old_cache_survives(tmp_path: Path) -> None:
    """The other half of the job: a partially written REAL cache must never replace a good one."""
    corpus = _real_cache_abf_files()
    if not corpus:
        pytest.skip("no real cache.abf on this machine (they are gitignored); set PBIP_REFRESH_REAL_ABF")
    real_bytes = corpus[0].read_bytes()
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"GOOD-EXISTING-CACHE")

    def write_torn(staging: Path) -> None:
        staging.write_bytes(real_bytes[: len(real_bytes) // 2])

    assert refresh_pbip_model._staged_image_write(cache, write_torn) is False
    assert cache.read_bytes() == b"GOOD-EXISTING-CACHE"
    assert _no_staging_files(cache)


def test_a_failed_write_does_not_destroy_an_existing_good_cache(tmp_path: Path) -> None:
    """FileMode.Create truncates on open; staging is what keeps a failed write from erasing the cache."""
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"GOOD-EXISTING-CACHE")

    def write_nothing(_staging: Path) -> None:
        # A write that produces no file - e.g. the engine refused - must not touch the live cache.
        return None

    assert refresh_pbip_model._staged_image_write(cache, write_nothing) is False
    assert cache.read_bytes() == b"GOOD-EXISTING-CACHE", "a failed write must leave the old cache intact"
    assert _no_staging_files(cache), "no staging file may be left behind"


def test_a_raising_write_with_no_output_propagates_and_leaves_the_cache_intact(tmp_path: Path) -> None:
    """`_staged_image_write` judges success by the FILE, and any raise is a REAL failure.

    The one benign AMO response-parser error is absorbed a layer down (inside `image_save`'s writer
    closure); by the time an exception reaches `_staged_image_write` it means the write genuinely
    failed, so it must PROPAGATE (the caller then falls back to the UI save) and must not touch the
    existing good cache. Round-1 suppressed every exception here and returned False, which hid real
    disk/permission failures (#113, round-2 blocker 1).
    """
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"GOOD-EXISTING-CACHE")

    def raise_without_writing(_staging: Path) -> None:
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        refresh_pbip_model._staged_image_write(cache, raise_without_writing)
    assert cache.read_bytes() == b"GOOD-EXISTING-CACHE"
    assert _no_staging_files(cache)


def test_a_raising_write_that_produced_partial_bytes_does_not_replace_the_cache(tmp_path: Path) -> None:
    """A disk-full/interrupted write that left PARTIAL bytes must not be mistaken for a success.

    This is the exact outcome #113 was filed to prevent: round-1 treated any non-empty staging file
    as a completed write and swapped it in, so an interrupted write destroyed the existing good cache.
    Now the raise propagates, the partial staging file is discarded, and the old cache survives.
    """
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"GOOD-EXISTING-CACHE")

    def raise_after_partial_write(staging: Path) -> None:
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"PARTIAL")
        raise RuntimeError("The server sent an unrecognizable response")

    with pytest.raises(RuntimeError):
        refresh_pbip_model._staged_image_write(cache, raise_after_partial_write)
    assert cache.read_bytes() == b"GOOD-EXISTING-CACHE", "a partial write must not replace the good cache"
    assert _no_staging_files(cache), "the partial staging file must be removed"


def test_a_clean_write_of_incomplete_bytes_is_rejected(tmp_path: Path) -> None:
    """Even a clean return must not swap in a staging file that is not a complete ABF.

    The completeness check is what makes 'success' mean a loadable backup, not merely 'some bytes
    landed'. Round-1 swapped on non-empty, so a truncated file that returned cleanly overwrote the
    good cache; now it is discarded and the old cache is preserved (#113, round-2 blocker 1).
    """
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"GOOD-EXISTING-CACHE")

    def write_incomplete(staging: Path) -> None:
        staging.write_bytes(b"NOT-A-CFBF-BACKUP")

    assert refresh_pbip_model._staged_image_write(cache, write_incomplete) is False
    assert cache.read_bytes() == b"GOOD-EXISTING-CACHE", "an incomplete staged file must not replace the cache"
    assert _no_staging_files(cache)


def test_a_successful_write_swaps_the_new_cache_in(tmp_path: Path) -> None:
    """The happy path: a COMPLETE ABF staging file is written, then atomically replaces the destination."""
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"OLD")
    new_bytes = _valid_abf_bytes()

    def write_new(staging: Path) -> None:
        staging.write_bytes(new_bytes)

    assert refresh_pbip_model._staged_image_write(cache, write_new) is True
    assert cache.read_bytes() == new_bytes


def test_benign_imagesave_error_is_recognised_but_a_real_failure_is_not(tmp_path: Path) -> None:
    """Only AMO's known response-parser message is benign; every other failure must be treated as real.

    This is the discriminator that lets `image_save`'s writer closure swallow the one error the client
    raises even on a correct write, while re-raising a disk-full/permission failure so a partial write
    is never mistaken for a success (#113, round-2 blocker 1).
    """
    _ = tmp_path  # unused; keeps a uniform signature with the file's other tests
    benign = RuntimeError("The server sent an unrecognizable response")
    real = RuntimeError("There is not enough space on the disk")
    assert refresh_pbip_model._is_benign_imagesave_response_error(benign) is True
    assert refresh_pbip_model._is_benign_imagesave_response_error(real) is False


def test_persist_rolls_back_the_compat_bump_when_the_write_fails(tmp_path: Path) -> None:
    """A failed persist must undo the provisional compatibilityLevel alignment, byte-for-byte.

    Otherwise database.tmdl - part of the deployable artifact - is left declaring a level that was
    never actually written to a cache, and the generated-edit ledger records a change that did not
    stick. An engine-run manifest is present so the alignment DOES append a ledger entry, letting us
    prove that entry is rolled back too (not just database.tmdl).
    """
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent
    database_tmdl = model_dir / "definition" / "database.tmdl"
    before = database_tmdl.read_bytes()
    before_hash = refresh_pbip_model.sha256_file(database_tmdl)
    (tmp_path / "input_manifest.json").write_text(
        json.dumps(
            {
                "generated_artifacts": {
                    "version": 1,
                    "run_id": "engine-run",
                    "recorded_at": "2026-08-10T08:00:00+00:00",
                    "report_sha256": "report-hash",
                    "files": {"MyMigration.SemanticModel/definition/database.tmdl": before_hash},
                }
            }
        ),
        encoding="utf-8",
    )

    def failing_write(_staging: Path) -> None:
        return None

    ok, message = refresh_pbip_model._persist_image(cache, model_dir, 1702, failing_write)
    assert ok is False
    assert "rolled back" in message
    assert database_tmdl.read_bytes() == before, "a failed persist must restore database.tmdl exactly"
    ledger = tmp_path / "_build" / "generated-edit-declarations.json"
    assert not ledger.exists(), "the generated-edit ledger entry must be rolled back too"
    assert not cache.exists()


def test_persist_aligns_and_writes_on_success(tmp_path: Path) -> None:
    """On a successful write the alignment STAYS: the cache is a 1702 image, so the project must
    declare 1702 or the reopen hits the compatibility-downgrade crash."""
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent
    database_tmdl = model_dir / "definition" / "database.tmdl"
    new_bytes = _valid_abf_bytes()

    def good_write(staging: Path) -> None:
        staging.write_bytes(new_bytes)

    ok, message = refresh_pbip_model._persist_image(cache, model_dir, 1702, good_write)
    assert ok is True
    assert "1702" in message
    assert "compatibilityLevel: 1702" in database_tmdl.read_text(encoding="utf-8")
    assert cache.read_bytes() == new_bytes


def test_persist_rolls_back_the_compat_bump_when_os_replace_raises(tmp_path: Path, monkeypatch) -> None:
    """An EXCEPTION on the write/replace path must roll the compat bump back, not just a False return.

    Round-1 rolled back only when `_staged_image_write` returned False; an `os.replace` that raises
    (a Windows sharing-violation on a locked cache is the real case) bypassed the rollback entirely,
    leaving database.tmdl declaring 1702 for a cache that was never written - state the caller then
    carried into the UI Save (#113, round-2 blocker 2). The staging write here is a COMPLETE ABF, so
    the failure is purely the replace, isolating the exception path.

    The patch is SCOPED to the cache swap (dst == cache.abf), because the rollback ITSELF now restores
    atomically via `os.replace` (round-3 blocker 2); a blanket patch would break the very rollback the
    test means to observe.
    """
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent
    database_tmdl = model_dir / "definition" / "database.tmdl"
    before = database_tmdl.read_bytes()

    def good_write(staging: Path) -> None:
        staging.write_bytes(_valid_abf_bytes())

    real_replace = refresh_pbip_model.os.replace

    def raising_replace(src, dst):
        if str(dst).endswith("cache.abf"):
            raise PermissionError("The process cannot access the file because it is being used")
        return real_replace(src, dst)

    monkeypatch.setattr(refresh_pbip_model.os, "replace", raising_replace)

    with pytest.raises(PermissionError):
        refresh_pbip_model._persist_image(cache, model_dir, 1702, good_write)
    assert database_tmdl.read_bytes() == before, "an exception on replace must restore database.tmdl exactly"
    assert not cache.exists(), "no cache may be left behind when the replace failed"
    assert _no_staging_files(cache), "the staging file must be cleaned up"


def test_a_commit_then_raise_keeps_the_new_cache_and_the_aligned_compat(tmp_path: Path, monkeypatch) -> None:
    """If `os.replace` MOVED the cache but still raised, that is a COMMIT - keep the aligned compat.

    Commit is judged by the filesystem, not the writer's return flag (round-3 blocker 2). Here the
    replace installs the new cache and then raises; the round-2 code saw the exception, treated the
    write as "not committed", and rolled the compatibility level back UNDER the freshly installed
    cache - a 1702 image in a project re-declared 1604, the downgrade crash on reopen. The alignment
    must STAY at 1702 and no exception may escape.
    """
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent
    database_tmdl = model_dir / "definition" / "database.tmdl"
    new_bytes = _valid_abf_bytes()

    def good_write(staging: Path) -> None:
        staging.write_bytes(new_bytes)

    real_replace = refresh_pbip_model.os.replace

    def commit_then_raise(src, dst):
        if str(dst).endswith("cache.abf"):
            real_replace(src, dst)  # actually install the cache...
            raise PermissionError("moved, then lost the handle")  # ...then surface an error anyway
        return real_replace(src, dst)

    monkeypatch.setattr(refresh_pbip_model.os, "replace", commit_then_raise)

    ok, message = refresh_pbip_model._persist_image(cache, model_dir, 1702, good_write)
    assert ok is True, "a moved-then-raised replace is a commit, judged by the filesystem"
    assert "1702" in message
    assert cache.read_bytes() == new_bytes, "the installed cache must be kept"
    assert "compatibilityLevel: 1702" in database_tmdl.read_text(encoding="utf-8"), (
        "compat must NOT be rolled back under a committed cache"
    )
    assert _no_staging_files(cache)


def test_a_failed_rollback_is_fatal_and_raises_compat_rollback_error(tmp_path: Path, monkeypatch) -> None:
    """If the write did not land AND the compat rollback itself fails, that is FATAL, not a soft return.

    A partial state where database.tmdl was bumped but the cache was never written, and the bump
    cannot be undone, must NOT be quietly converted into a UI-save fallback (round-3 blocker 2): saving
    would persist the mismatch. `_persist_image` raises `CompatRollbackError` so the caller can stop.
    Here the write cleanly does nothing (not committed) and the rollback's `os.replace` onto
    database.tmdl is blocked.
    """
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent

    def failing_write(_staging: Path) -> None:
        return None

    real_replace = refresh_pbip_model.os.replace

    def block_rollback(src, dst):
        if str(dst).endswith("database.tmdl"):
            raise PermissionError("database.tmdl is locked")
        return real_replace(src, dst)

    monkeypatch.setattr(refresh_pbip_model.os, "replace", block_rollback)

    with pytest.raises(refresh_pbip_model.CompatRollbackError):
        refresh_pbip_model._persist_image(cache, model_dir, 1702, failing_write)


def test_every_rollback_path_is_attempted_even_when_the_first_fails(tmp_path: Path, monkeypatch) -> None:
    """A failure on one rollback path must not abandon the others (round-3 blocker 2).

    The alignment touches TWO files - database.tmdl and the generated-edit ledger. If restoring the
    first fails, the second must STILL be restored (the round-2 loop stopped at the first failure,
    leaving a subset reverted). We block database.tmdl's restore and assert the ledger was rolled back
    regardless, while the overall failure is still surfaced as fatal.
    """
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent
    database_tmdl = model_dir / "definition" / "database.tmdl"
    before_hash = refresh_pbip_model.sha256_file(database_tmdl)
    (tmp_path / "input_manifest.json").write_text(
        json.dumps(
            {
                "generated_artifacts": {
                    "version": 1,
                    "run_id": "engine-run",
                    "recorded_at": "2026-08-10T08:00:00+00:00",
                    "report_sha256": "report-hash",
                    "files": {"MyMigration.SemanticModel/definition/database.tmdl": before_hash},
                }
            }
        ),
        encoding="utf-8",
    )

    def failing_write(_staging: Path) -> None:
        return None

    real_replace = refresh_pbip_model.os.replace

    def block_database_tmdl(src, dst):
        if str(dst).endswith("database.tmdl"):
            raise PermissionError("database.tmdl is locked")
        return real_replace(src, dst)

    monkeypatch.setattr(refresh_pbip_model.os, "replace", block_database_tmdl)

    ledger = tmp_path / "_build" / "generated-edit-declarations.json"
    with pytest.raises(refresh_pbip_model.CompatRollbackError):
        refresh_pbip_model._persist_image(cache, model_dir, 1702, failing_write)
    assert not ledger.exists(), "the ledger must be rolled back even though database.tmdl's restore failed"


# --------------------------------------------------------------------------------------------------
# BLOCKER 1 (#118) - a KeyboardInterrupt must NOT bypass the compat rollback.
#
# `KeyboardInterrupt` inherits from `BaseException`, not `Exception`. The round-3 transaction caught
# only `Exception`, so a Ctrl+C during alignment or ImageSave propagated straight past the commit
# check AND the rollback (which are not in a `finally`), leaving `database.tmdl` declaring the bumped
# level for a cache that was never written - the same brick as #113, by a different route. The fix
# catches `BaseException`, and each test below FAILS on a revert to `except Exception` because the
# interrupt then skips the rollback and `database.tmdl` is left at the bumped level.
# --------------------------------------------------------------------------------------------------


def test_a_keyboard_interrupt_during_imagesave_rolls_back_the_compat_bump_and_reraises(tmp_path: Path) -> None:
    """Ctrl+C during the ImageSave: the 1702 bump must be undone AND the interrupt must propagate.

    The alignment has already written 1702 to `database.tmdl` by the time `write_image` runs, so this
    is the exact bricking window: bump on disk, no cache yet, interrupt raised. Reverting the fix to
    `except Exception` leaves `database.tmdl` at 1702 (the interrupt skips the rollback), failing the
    `== before` assertion even though the interrupt still propagates.
    """
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent
    database_tmdl = model_dir / "definition" / "database.tmdl"
    before = database_tmdl.read_bytes()

    def interrupted_write(_staging: Path) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        refresh_pbip_model._persist_image(cache, model_dir, 1702, interrupted_write)

    assert database_tmdl.read_bytes() == before, "an interrupt must roll the compat bump back, not leave it declared"
    assert not cache.exists(), "no cache may be left behind"
    assert _no_staging_files(cache), "the staging file must be cleaned up on interrupt"


def test_a_keyboard_interrupt_right_after_alignment_rolls_back_and_reraises(tmp_path: Path, monkeypatch) -> None:
    """Ctrl+C the instant the 1702 bump lands, before the cache write even starts.

    Injected by wrapping `align_declared_compatibility` so the real bump is written and THEN the
    interrupt is raised - the narrowest possible bricking window. On the fixed code the bump is rolled
    back and the interrupt re-raised; reverted to `except Exception`, the interrupt escapes with
    `database.tmdl` still at 1702.
    """
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent
    database_tmdl = model_dir / "definition" / "database.tmdl"
    before = database_tmdl.read_bytes()

    real_align = refresh_pbip_model.align_declared_compatibility

    def align_then_interrupt(path: Path, level: int) -> None:
        real_align(path, level)  # the 1702 bump actually lands on disk...
        raise KeyboardInterrupt  # ...then Ctrl+C arrives before anything writes the cache

    monkeypatch.setattr(refresh_pbip_model, "align_declared_compatibility", align_then_interrupt)

    def unreached_write(_staging: Path) -> None:
        raise AssertionError("the cache write must not be reached after an interrupt during alignment")

    with pytest.raises(KeyboardInterrupt):
        refresh_pbip_model._persist_image(cache, model_dir, 1702, unreached_write)

    assert database_tmdl.read_bytes() == before, "the 1702 bump must be rolled back after an interrupt"
    assert not cache.exists()


def test_a_failed_rollback_during_an_interrupt_raises_compat_rollback_error_over_the_interrupt(
    tmp_path: Path, monkeypatch
) -> None:
    """If the interrupt's rollback ITSELF fails, `CompatRollbackError` must win over the interrupt.

    A bricked project (database.tmdl bumped, bump un-undoable) is worse than a tidy Ctrl+C, so the
    fatal, actionable error must be what surfaces. Reverting to `except Exception` makes the interrupt
    escape before any rollback is attempted, so `CompatRollbackError` is never raised and this test's
    `pytest.raises` fails.
    """
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent

    def interrupted_write(_staging: Path) -> None:
        raise KeyboardInterrupt

    real_replace = refresh_pbip_model.os.replace

    def block_rollback(src, dst):
        if str(dst).endswith("database.tmdl"):
            raise PermissionError("database.tmdl is locked")
        return real_replace(src, dst)

    monkeypatch.setattr(refresh_pbip_model.os, "replace", block_rollback)

    with pytest.raises(refresh_pbip_model.CompatRollbackError):
        refresh_pbip_model._persist_image(cache, model_dir, 1702, interrupted_write)


# --------------------------------------------------------------------------------------------------
# BLOCKER 2 (#114) - concurrent runs against one model must not deterministically brick it.
#
# Two defences, both needed: a per-run PRIVATE staging name (so run B can't delete run A's in-flight
# staging file), AND a per-model interprocess lock spanning the whole transaction (so the shared
# compatibility edit on `database.tmdl` can't be interleaved - unique names alone don't cover that).
# --------------------------------------------------------------------------------------------------


def _distinct_valid_abf_bytes() -> bytes:
    """A second, genuinely different, still-complete backup - so a run's own bytes are identifiable.

    Same block geometry, different payload, so the blob stays a valid image while differing
    byte-for-byte from `_valid_abf_bytes()`.
    """
    return _abf_bytes(seed=0xA5)


def _reaped_pid() -> int:
    """A PID that is now dead: spawn a trivial process, wait for it to exit, return its PID."""
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    proc.wait()
    return proc.pid


def test_a_distinct_valid_abf_is_still_complete_but_different(tmp_path: Path) -> None:
    """Guard for the interleaving test's fixtures: the two payloads are both valid yet distinguishable."""
    assert _distinct_valid_abf_bytes() != _valid_abf_bytes()
    probe = tmp_path / "cache.abf"
    probe.write_bytes(_distinct_valid_abf_bytes())
    assert refresh_pbip_model._is_complete_abf(probe) is True


def test_two_interleaved_staged_writes_keep_private_staging_files(tmp_path: Path) -> None:
    """Forced hostile interleaving at the staging seam: run B must not disturb run A's staging file.

    Run A's `write_image` stages its complete backup and THEN drives a second run B fully through
    `_staged_image_write` before A finishes. On the fixed code each run stages to a private
    `cache.abf.<pid>-<token>.tmp`, so B's own `if staging.exists(): unlink` targets B's file and A's
    survives; A then commits its OWN bytes. Reverting to the shared fixed `cache.abf.tmp` makes B
    delete A's staging, so `staging_a.exists()` (and the distinct-path assertion) fail.
    """
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"OLD")
    a_bytes = _valid_abf_bytes()
    b_bytes = _distinct_valid_abf_bytes()
    seen: dict[str, Path] = {}

    def b_write(staging_b: Path) -> None:
        seen["b"] = staging_b
        staging_b.write_bytes(b_bytes)

    def a_write(staging_a: Path) -> None:
        seen["a"] = staging_a
        staging_a.write_bytes(a_bytes)  # A's complete backup is now staged
        # HOSTILE INTERLEAVE: a concurrent run B does its entire staged write mid-A.
        assert refresh_pbip_model._staged_image_write(cache, b_write) is True
        assert staging_a.exists(), "run B must not delete run A's in-flight staging file"

    assert refresh_pbip_model._staged_image_write(cache, a_write) is True
    assert seen["a"] != seen["b"], "each concurrent run must stage to a private path"
    assert cache.read_bytes() == a_bytes, "A committed its OWN bytes; the runs did not cross-contaminate"
    assert _no_staging_files(cache)


def test_staging_path_is_unique_per_call(tmp_path: Path) -> None:
    """`_staging_path` never returns the same name twice, and never the old shared fixed name."""
    cache = tmp_path / "Model.SemanticModel" / ".pbi" / "cache.abf"
    first = refresh_pbip_model._staging_path(cache)
    second = refresh_pbip_model._staging_path(cache)
    assert first != second, "two runs must not share a staging path"
    assert first != cache.with_name(cache.name + ".tmp"), "the fixed shared name is exactly the #114 bug"
    assert first.suffix == ".tmp" and first.parent == cache.parent


def test_model_lock_is_exclusive_and_times_out_then_releases(tmp_path: Path) -> None:
    """The per-model lock is mutually exclusive, bounds the wait, and cleans up on release.

    Reverting the fix removes `model_lock`/`ModelLockTimeout` entirely, so this test errors out.
    """
    lock_path = tmp_path / "cache.abf.lock"
    with refresh_pbip_model.model_lock(lock_path, timeout=2.0, poll=0.02):
        assert lock_path.exists()
        with pytest.raises(refresh_pbip_model.ModelLockTimeout):
            with refresh_pbip_model.model_lock(lock_path, timeout=0.2, poll=0.02):
                raise AssertionError("a second acquisition must not succeed while the lock is held")
    assert not lock_path.exists(), "the lock file is removed on release"
    with refresh_pbip_model.model_lock(lock_path, timeout=2.0, poll=0.02):
        assert lock_path.exists(), "the lock is re-acquirable once released"


def test_model_lock_recovers_from_a_dead_holder(tmp_path: Path) -> None:
    """A lock left behind by a process that has since died must be reclaimable, not a permanent block.

    A lock that can wedge the tool forever is its own outage, so the acquirer detects the dead PID and
    reclaims the lock instead of waiting out the timeout.
    """
    lock_path = tmp_path / "cache.abf.lock"
    dead_pid = _reaped_pid()
    lock_path.write_text(f"{dead_pid}\n{socket.gethostname()}\n{time.time()}\n", encoding="utf-8")

    acquired = False
    with refresh_pbip_model.model_lock(lock_path, timeout=2.0, poll=0.02):
        acquired = True
        assert lock_path.read_text(encoding="utf-8").splitlines()[0].strip() == str(os.getpid()), (
            "the reclaimed lock must now record the live holder"
        )
    assert acquired, "a lock held by a dead process must be reclaimed, not blocked on"


def test_a_locked_out_persist_cannot_touch_compat_or_cache(tmp_path: Path) -> None:
    """Forced interleaving through `_persist_image`: while run A holds the lock, run B mutates nothing.

    This is the reviewer's compat race: the shared resource is the alignment on `database.tmdl`, not
    just the temp file. With run A holding the per-model lock across its transaction, run B's
    `_persist_image` must be locked out BEFORE it can align or write - so `database.tmdl` and the
    absent cache are exactly as A left them. Reverting the fix removes the lock (and the `lock_timeout`
    parameter), so B is no longer serialised and this test errors/fails.
    """
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent
    database_tmdl = model_dir / "definition" / "database.tmdl"
    before = database_tmdl.read_bytes()
    cache.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache.with_name(cache.name + ".lock")

    def b_write(staging: Path) -> None:
        staging.write_bytes(_valid_abf_bytes())

    with refresh_pbip_model.model_lock(lock_path, timeout=2.0, poll=0.02):
        with pytest.raises(refresh_pbip_model.ModelLockTimeout):
            refresh_pbip_model._persist_image(cache, model_dir, 1606, b_write, lock_timeout=0.3)
        assert database_tmdl.read_bytes() == before, "a locked-out run must not mutate database.tmdl"
        assert not cache.exists(), "a locked-out run must not write a cache"


def test_refresh_refuses_ui_save_after_model_lock_timeout(tmp_path: Path, monkeypatch, capsys) -> None:
    """A live peer holding the persist lock must report NOT_PERSISTED, not fall back outside the lock."""
    cache = _model(tmp_path, compat=1604)
    args = refresh_pbip_model._build_arg_parser().parse_args([])
    calls: list[str] = []

    def locked_image_save(_port: int, _cache: Path, model_dir: Path | None = None):
        calls.append(f"image:{model_dir}")
        raise refresh_pbip_model.ModelLockTimeout("held by pid 123 on host buildbox")

    def unlocked_ui_save(_pid: int) -> tuple[bool, str]:
        calls.append("ui-save")
        return True, "ui save should not run"

    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda *_args: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "image_save", locked_image_save)
    monkeypatch.setattr(refresh_pbip_model, "save", unlocked_ui_save)

    assert refresh_pbip_model._refresh_and_save(456, 789, cache, args) == 1
    out = capsys.readouterr().out
    assert "REFRESH: NOT_PERSISTED" in out
    assert "pid 123" in out
    assert calls == [f"image:{cache.parent.parent}"]


def _frontmatter(text: str) -> str:
    """The YAML frontmatter block between the first pair of `---` fences."""
    assert text.startswith("---"), "SKILL.md must open with a YAML frontmatter fence"
    return text.split("---", 2)[1]


def test_documented_persist_default_matches_the_argparse_default() -> None:
    """The doc and the code cannot drift: the frontmatter's persistence default must equal argparse's.

    This is the #113 bug pinned so it cannot recur - the frontmatter said persisting was "opt-in via
    --save" while `main()` persisted by DEFAULT (`--no-save` opts out). The check is bidirectional:
    whatever the parser actually does, the prose must say the same thing.
    """
    parser = refresh_pbip_model._build_arg_parser()
    defaults = parser.parse_args([])
    # Persisting is the default exactly when the opt-OUT flag defaults to False and there is no
    # separate opt-IN gate (`--save` is an accepted no-op).
    code_persists_by_default = defaults.no_save is False
    assert code_persists_by_default, "guard assumption: the parser must persist by default"

    frontmatter = _frontmatter((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")).lower()
    doc_says_opt_in = "opt-in" in frontmatter or "opt in" in frontmatter
    doc_says_default = "default" in frontmatter
    assert not doc_says_opt_in, "frontmatter must NOT describe persisting as opt-in - the code persists by default"
    assert doc_says_default, "frontmatter must state that persisting is the default"
    # Bidirectional: the prose's claim and the parser's behaviour must agree.
    doc_persists_by_default = doc_says_default and not doc_says_opt_in
    assert doc_persists_by_default == code_persists_by_default


# --------------------------------------------------------------------------------------------------
# Round-4 finding 3: a failed persist must not invalidate the pre-existing good cache VIA MTIME.
# --------------------------------------------------------------------------------------------------


def test_a_failed_persist_preserves_the_definition_mtime(tmp_path: Path) -> None:
    """Restoring `database.tmdl`'s BYTES is not enough - its MTIME is load-bearing.

    The skill's own documented cache-discard trigger is "definition newer than cache": Desktop drops a
    perfectly good 113 KB cache and reopens with NO_DATA when the model files look newer. So a failed
    persist that rewrites database.tmdl with identical bytes still bricks the cache, purely by bumping
    its timestamp - the rollback undoes the edit and destroys the cache in the same motion. The
    snapshot therefore restores atime/mtime as well as content (round-4 finding 3).
    """
    cache = _model(tmp_path, compat=1604)
    model_dir = cache.parent.parent
    database_tmdl = model_dir / "definition" / "database.tmdl"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(_valid_abf_bytes())

    old = time.time() - 600  # the definition is comfortably older than the cache
    for path in (database_tmdl, *(model_dir / "definition" / "tables").glob("*.tmdl")):
        os.utime(path, (old, old))
    assert cache.stat().st_mtime > database_tmdl.stat().st_mtime, "precondition: cache is newer"
    before_bytes = database_tmdl.read_bytes()
    before_mtime = database_tmdl.stat().st_mtime_ns

    def failing_write(_staging: Path) -> None:
        return None  # the engine refused; nothing is staged

    ok, _message = refresh_pbip_model._persist_image(cache, model_dir, 1702, failing_write)
    assert ok is False
    assert database_tmdl.read_bytes() == before_bytes, "bytes must be restored exactly"
    assert database_tmdl.stat().st_mtime_ns == before_mtime, "the mtime must be restored too"
    assert cache.stat().st_mtime > database_tmdl.stat().st_mtime, "the existing cache must stay valid"
