"""The persisted cache write must be atomic, and a failed persist must not leave the project bumped.

Two guarantees this file pins, both from #113:

1. **Atomic swap.** `ImageSave` opens `cache.abf` with `FileMode.Create`, which TRUNCATES a good
   cache the instant the write begins - so a write that then fails half way leaves the project WORSE
   than before (no fresh cache and no old one). `_staged_image_write` writes `cache.abf.tmp` and only
   `os.replace`s it over the original once it exists and is a COMPLETE ABF backup, so a failed or
   partial write can never destroy an existing good cache. A raise, or a clean return that produced
   only partial bytes, both preserve the existing cache (#113, round-2 blocker 1).
2. **Compat rollback.** Saving raises `database.tmdl`'s declared compatibilityLevel to the live
   level. That edit is written eagerly (so the serialized cache matches the project), but it is
   PROVISIONAL: if the ImageSave that follows does not land, `_persist_image` restores
   `database.tmdl` (and the generated-edit ledger) exactly, so a mid-failure never leaves the model
   declaring a level that was never actually written to a cache.

Plus a docs-vs-code guard: the SKILL.md frontmatter's persistence default must match the argparse
default, so the two cannot silently drift again (the #113 doc bug: frontmatter said "opt-in" while
the code persisted by default).
"""

from __future__ import annotations

import json
import struct
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


def _build_minimal_cfbf() -> bytes:
    """A genuine, minimal 3-sector Microsoft Compound File Binary (v3, 512-byte sectors).

    Layout: a 512-byte header + one FAT sector + one directory sector (1536 bytes total). This is a
    REAL CFBF container - header fields set to their only legal values, the FAT self-referencing, and
    a Root Entry directory record - not merely the magic bytes with zero padding. `_is_complete_abf`
    validates the header STRUCTURE (round-3 blocker 1), so fixtures must be structurally valid, and
    every truncation of THIS blob is a realistic torn write.
    """
    sector = 512
    endofchain, freesect, fatsect, nostream = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF

    header = bytearray(sector)
    header[0:8] = refresh_pbip_model._CFBF_MAGIC
    struct.pack_into("<H", header, 24, 0x003E)  # minor version
    struct.pack_into("<H", header, 26, 3)  # major version 3 -> 512-byte sectors
    struct.pack_into("<H", header, 28, 0xFFFE)  # byte-order mark
    struct.pack_into("<H", header, 30, 9)  # sector shift (1<<9 == 512)
    struct.pack_into("<H", header, 32, 6)  # mini sector shift (fixed)
    struct.pack_into("<I", header, 40, 0)  # num directory sectors (0 for v3)
    struct.pack_into("<I", header, 44, 1)  # num FAT sectors
    struct.pack_into("<I", header, 48, 1)  # first directory sector -> sector 1
    struct.pack_into("<I", header, 56, 4096)  # mini-stream cutoff
    struct.pack_into("<I", header, 60, endofchain)  # first mini-FAT sector
    struct.pack_into("<I", header, 64, 0)  # num mini-FAT sectors
    struct.pack_into("<I", header, 68, endofchain)  # first DIFAT sector
    struct.pack_into("<I", header, 72, 0)  # num DIFAT sectors
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
    struct.pack_into("<Q", directory, 120, 0)  # stream size
    for entry in range(1, 4):  # remaining directory records are unallocated
        base = entry * 128
        struct.pack_into("<I", directory, base + 68, nostream)
        struct.pack_into("<I", directory, base + 72, nostream)
        struct.pack_into("<I", directory, base + 76, nostream)

    return bytes(header) + bytes(fat) + bytes(directory)


def _valid_abf_bytes() -> bytes:
    """Bytes that pass `_is_complete_abf`: a genuine minimal CFBF container, not magic + zero padding.

    A real `cache.abf` is a Microsoft Compound File Binary, and the completeness check validates the
    CFBF header structure (#113, round-3 blocker 1), so happy-path tests must write something that is
    actually a container - the previous magic-plus-zeros blob no longer qualifies (its byte-order mark
    was 0, not 0xFFFE), which is the whole point of the stricter check.
    """
    return _build_minimal_cfbf()


def test_is_complete_abf_accepts_a_genuine_cfbf_container(tmp_path: Path) -> None:
    """The positive control: a structurally valid minimal CFBF is accepted."""
    good = tmp_path / "cache.abf"
    good.write_bytes(_build_minimal_cfbf())
    assert refresh_pbip_model._is_complete_abf(good) is True


def test_is_complete_abf_rejects_magic_with_only_zero_padding(tmp_path: Path) -> None:
    """The old fixture shape - CFBF magic then zeros - is NOT a container and must be rejected.

    Its byte-order mark is 0 rather than 0xFFFE and it references no sectors, so the round-2 check
    (magic + size>=512) waved it through while the structural check rejects it (round-3 blocker 1).
    """
    blob = tmp_path / "cache.abf"
    blob.write_bytes(refresh_pbip_model._CFBF_MAGIC + b"\x00" * (refresh_pbip_model._CFBF_HEADER_BYTES - 8))
    assert refresh_pbip_model._is_complete_abf(blob) is False


def test_is_complete_abf_rejects_a_non_sector_aligned_truncation(tmp_path: Path) -> None:
    """A 600-byte torn write - keeps the magic, not a whole number of sectors - must be rejected.

    This is the reviewer's exact reproduction: 600 bytes passed round-2 (magic + >=512) and reached
    `os.replace`, destroying the old cache (round-3 blocker 1). A complete container is a whole number
    of sectors, so a length that is not is a partial write.
    """
    truncated = tmp_path / "cache.abf"
    truncated.write_bytes(_build_minimal_cfbf()[:600])
    assert refresh_pbip_model._is_complete_abf(truncated) is False


def test_is_complete_abf_rejects_a_sector_aligned_truncation(tmp_path: Path) -> None:
    """Even a sector-ALIGNED truncation is rejected: its header points at sectors now past EOF.

    Truncating the valid 1536-byte container to 1024 keeps the header (which names a directory at
    sector 1) but drops that sector, so the referenced index now sits beyond the file - the signature
    of an interrupted write that a size/alignment-only check cannot catch (round-3 blocker 1).
    """
    truncated = tmp_path / "cache.abf"
    truncated.write_bytes(_build_minimal_cfbf()[:1024])
    assert refresh_pbip_model._is_complete_abf(truncated) is False


def test_is_complete_abf_rejects_a_header_only_file(tmp_path: Path) -> None:
    """A lone 512-byte header (no data sectors) is not a usable backup and is rejected."""
    header_only = tmp_path / "cache.abf"
    header_only.write_bytes(_build_minimal_cfbf()[:512])
    assert refresh_pbip_model._is_complete_abf(header_only) is False


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
    assert not cache.with_name(cache.name + ".tmp").exists(), "no staging file may be left behind"


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
    assert not cache.with_name(cache.name + ".tmp").exists()


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
    assert not cache.with_name(cache.name + ".tmp").exists(), "the partial staging file must be removed"


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
    assert not cache.with_name(cache.name + ".tmp").exists()


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
    assert not cache.with_name(cache.name + ".tmp").exists(), "the staging file must be cleaned up"


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
    assert not cache.with_name(cache.name + ".tmp").exists()


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
