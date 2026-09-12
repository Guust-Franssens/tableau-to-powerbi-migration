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
import hashlib
import inspect
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, replace
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

# `conftest.py` next to this file puts the skill's own `scripts/` on `sys.path`.
# ruff: noqa: E402  (the conftest-provided path must be in place before these imports)
import refresh_pbip_model
import _abf
from probe_desktop_query import BoundDesktop, DesktopIdentity, ObservationUnavailable

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


@pytest.mark.parametrize("mode", ["normal", "commit-raise", "wrong-image", "failed-replace", "truncated"])
def test_image_observation_does_not_infer_commitment_from_replacement_bytes(tmp_path, monkeypatch, mode):
    cache = _model(tmp_path, compat=1604)
    content = _abf_bytes()
    cache.parent.mkdir()
    cache.write_bytes(content)  # An old identical image cannot stand in for this operation.
    stamp = cache.stat().st_mtime_ns
    native_replace = os.replace
    published = []

    def replace_cache(source, destination):
        if Path(destination) == cache:
            if mode == "failed-replace":
                raise OSError("replace failed")
            native_replace(source, destination)
            if mode == "wrong-image":
                Path(destination).write_bytes(_abf_bytes(seed=1))
            os.utime(destination, ns=(stamp, stamp))  # Successful writes need not change mtime.
            if mode == "commit-raise":
                raise OSError("moved, then raised")
        else:
            native_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace_cache)

    def persist():
        return refresh_pbip_model._persist_image(
            cache,
            cache.parent.parent,
            1606,
            lambda path: path.write_bytes(content[:-1] if mode == "truncated" else content),
            return_observation=True,
        )

    if mode in ("wrong-image", "commit-raise"):
        with pytest.raises(refresh_pbip_model.CompatRollbackError, match="^TOOL_UNAVAILABLE$"):
            published.append(persist())
    elif mode in ("failed-replace", "truncated"):
        with pytest.raises(ObservationUnavailable, match="^TOOL_UNAVAILABLE$"):
            published.append(persist())
    else:
        published.append(persist())
    if mode == "normal":
        digest = hashlib.sha256(content).hexdigest()
        assert published == [refresh_pbip_model.ImageObservation(digest, len(content), digest, len(content))]
        assert published[0].commitment == "UNESTABLISHED"
        assert cache.read_bytes() == content
    else:
        assert published == []
    level = "1606" if mode in ("normal", "commit-raise", "wrong-image") else "1604"
    assert f"compatibilityLevel: {level}" in (cache.parent.parent / "definition" / "database.tmdl").read_text()
    assert _no_staging_files(cache)


@pytest.mark.parametrize("catalogues", ["one", "missing", "ambiguous", "wrong"])
def test_imagesave_observation_is_bound_and_exports_only_native_facts(tmp_path, monkeypatch, catalogues):
    cache = _model(tmp_path, compat=1604)
    identity = refresh_pbip_model.BoundDesktop(
        DesktopIdentity(111, "100", 222, "101", 52001),
        "11111111-2222-3333-4444-555555555555",
    )
    database = SimpleNamespace(ID=identity.catalogue, CompatibilityLevel=1606)
    databases = {
        "one": [database],
        "missing": [],
        "ambiguous": [database, database],
        "wrong": [SimpleNamespace(ID="other", CompatibilityLevel=1606)],
    }[catalogues]
    writes = []

    def write(catalogue, stream):
        writes.append(catalogue)
        stream.path.write_bytes(_abf_bytes())
        database.ID = identity.catalogue

    def file_stream(path, *_):
        database.ID = "22222222-2222-3333-4444-555555555555"
        return SimpleNamespace(path=Path(path), Close=lambda: None)

    server = SimpleNamespace(Databases=databases, Connect=lambda _: None, Disconnect=lambda: None, ImageSave=write)
    monkeypatch.setattr(refresh_pbip_model, "_load_amo", lambda: lambda: server)
    monkeypatch.setattr(refresh_pbip_model, "desktop_identity", lambda _: identity.identity)
    monkeypatch.setitem(
        sys.modules,
        "System.IO",
        SimpleNamespace(
            FileAccess=SimpleNamespace(Write=1),
            FileMode=SimpleNamespace(Create=2),
            FileStream=file_stream,
        ),
    )

    if catalogues != "one":
        code = "CATALOGUE_CHANGED" if catalogues == "wrong" else "CATALOGUE_UNESTABLISHED"
        with pytest.raises(refresh_pbip_model.ObservationUnavailable, match=f"^{code}$"):
            refresh_pbip_model.image_save(52001, cache, cache.parent.parent, bound=identity, return_observation=True)
        assert writes == []
        return
    observation = refresh_pbip_model.image_save(
        52001, cache, cache.parent.parent, bound=identity, return_observation=True
    )
    assert writes == [identity.catalogue]
    assert observation.catalogue == identity.catalogue and observation.method == "AMO_ImageSave"
    assert observation.compatibility_level == 1606 and observation.identity == identity.identity
    assert {item.name for item in fields(observation)} == {
        "catalogue",
        "compatibility_level",
        "image",
        "identity",
        "method",
    }
    assert observation.image.commitment == "UNESTABLISHED"
    with pytest.raises(FrozenInstanceError):
        observation.catalogue = "caller mutation"
    assert not cache.with_name("cache.abf.lock").exists()
    with pytest.raises(refresh_pbip_model.ObservationUnavailable, match="^WRONG_PID_PORT$"):
        refresh_pbip_model.image_save(52002, cache, bound=identity, return_observation=True)
    with pytest.raises(refresh_pbip_model.ObservationUnavailable, match="^IDENTITY_UNESTABLISHED$"):
        refresh_pbip_model.image_save(52001, cache, return_observation=True)


def test_a_missing_or_failing_flush_barrier_never_becomes_durable_evidence(tmp_path, monkeypatch):
    cache = _model(tmp_path, compat=1604)
    flushes = []

    def failing_flush(_descriptor):
        flushes.append(1)
        raise OSError("durable flush failed")

    monkeypatch.setattr(os, "fsync", failing_flush)
    observation = refresh_pbip_model._persist_image(
        cache,
        cache.parent.parent,
        1606,
        lambda path: path.write_bytes(_abf_bytes()),
        return_observation=True,
    )
    assert observation.commitment == "UNESTABLISHED"
    assert flushes == []  # Exact review control: close/replace/readback did not exercise a barrier.
    assert not hasattr(_abf, "ImageCommit")

    old = cache.read_bytes()
    published = []

    def writer_with_barrier(path):
        with path.open("wb") as handle:
            handle.write(_abf_bytes(seed=2))
            handle.flush()
            os.fsync(handle.fileno())

    with pytest.raises(ObservationUnavailable, match="^TOOL_UNAVAILABLE$"):
        published.append(
            refresh_pbip_model._persist_image(
                cache, cache.parent.parent, 1702, writer_with_barrier, return_observation=True
            )
        )
    assert flushes == [1] and published == [] and cache.read_bytes() == old
    assert "compatibilityLevel: 1606" in (cache.parent.parent / "definition" / "database.tmdl").read_text()


def test_removed_stage_and_raised_replace_cannot_reuse_identical_old_target(tmp_path, monkeypatch):
    cache = _model(tmp_path, compat=1604)
    cache.parent.mkdir()
    content = _abf_bytes()
    cache.write_bytes(content)
    published = []
    replace_file = os.replace

    def failed_replace(source, destination):
        if Path(destination) == cache:
            Path(source).unlink()
            raise OSError("stage removed but target untouched")
        return replace_file(source, destination)

    monkeypatch.setattr(os, "replace", failed_replace)
    with pytest.raises(refresh_pbip_model.CompatRollbackError, match="^TOOL_UNAVAILABLE$"):
        published.append(
            refresh_pbip_model._persist_image(
                cache, cache.parent.parent, 1606, lambda path: path.write_bytes(content), return_observation=True
            )
        )
    assert published == [] and cache.read_bytes() == content
    assert "compatibilityLevel: 1606" in (cache.parent.parent / "definition" / "database.tmdl").read_text()


@pytest.mark.parametrize("fault", ["read", "hash", "reader-exit", "interrupt"])
@pytest.mark.parametrize("observe", [False, True], ids=["legacy", "observing"])
def test_post_swap_observation_faults_never_roll_compatibility_back(tmp_path, monkeypatch, fault, observe):
    cache = _model(tmp_path, compat=1604)
    cache.parent.mkdir()
    cache.write_bytes(_abf_bytes(seed=1))
    intended = _abf_bytes(seed=2)
    attempted = []
    swapped = False
    replace_file, open_file, digest = os.replace, Path.open, hashlib.sha256

    def install(source, destination):
        nonlocal swapped
        result = replace_file(source, destination)
        if Path(destination) == cache:
            swapped = True
        return result

    def fail():
        attempted.append(fault)
        raise KeyboardInterrupt() if fault == "interrupt" else OSError(f"post-swap {fault}")

    @contextmanager
    def open_reader(path, *args, **kwargs):
        with open_file(path, *args, **kwargs) as handle:
            if swapped and path == cache and fault in ("read", "interrupt"):
                fail()
            yield handle
        if swapped and path == cache and fault == "reader-exit":
            fail()

    def hash_bytes(*args, **kwargs):
        if swapped and fault == "hash":
            fail()
        return digest(*args, **kwargs)

    result, error = None, None
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", install)
        patch.setattr(Path, "open", open_reader)
        patch.setattr(hashlib, "sha256", hash_bytes)
        try:
            result = refresh_pbip_model._persist_image(
                cache,
                cache.parent.parent,
                1606,
                lambda path: path.write_bytes(intended),
                return_observation=observe,
            )
        except BaseException as caught:
            error = caught
    assert swapped and cache.read_bytes() == intended
    assert "compatibilityLevel: 1606" in (cache.parent.parent / "definition" / "database.tmdl").read_text()
    if observe:
        assert result is None and type(error) is (KeyboardInterrupt if fault == "interrupt" else ObservationUnavailable)
        assert attempted == [fault]
    else:
        assert error is None and result[0] is True
        assert attempted == []


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_legacy_success_uses_installation_even_when_the_stage_call_raises_afterwards(tmp_path, monkeypatch, error_type):
    cache = _model(tmp_path, compat=1604)
    stage = _abf._staged_image_write

    def installed_then_error(*args, **kwargs):
        assert stage(*args, **kwargs) is True
        raise error_type("after returned replace")

    monkeypatch.setattr(refresh_pbip_model, "_staged_image_write", installed_then_error)
    result, error = None, None
    try:
        result = refresh_pbip_model._persist_image(
            cache, cache.parent.parent, 1606, lambda path: path.write_bytes(_abf_bytes())
        )
    except BaseException as caught:
        error = caught
    assert error is None and result[0] is True
    assert "compatibilityLevel: 1606" in (cache.parent.parent / "definition" / "database.tmdl").read_text()


def test_interrupt_after_replace_returns_before_installed_flag_preserves_alignment(tmp_path):
    cache = _model(tmp_path, compat=1604)
    cache.parent.mkdir()
    cache.write_bytes(_abf_bytes(seed=1))
    intended = _abf_bytes(seed=2)
    published, interrupted = [], []
    stage = _abf._staged_image_write
    lines, first_line = inspect.getsourcelines(stage)
    flag_line = first_line + next(
        offset for offset, line in enumerate(lines) if line.strip() == "installation.installed = True"
    )

    def interrupt(frame, event, _argument):
        if frame.f_code is stage.__code__ and event == "line" and frame.f_lineno == flag_line and not interrupted:
            state = frame.f_locals["installation"]
            interrupted.append(
                (state.installed, state.ambiguous, cache.read_bytes() == intended, frame.f_locals["staging"].exists())
            )
            raise KeyboardInterrupt("returned replace; installed flag not yet recorded")
        return interrupt

    previous_trace = sys.gettrace()
    error = None
    try:
        sys.settrace(interrupt)
        published.append(
            refresh_pbip_model._persist_image(
                cache,
                cache.parent.parent,
                1606,
                lambda path: path.write_bytes(intended),
                return_observation=True,
            )
        )
    except BaseException as caught:
        error = caught
    finally:
        sys.settrace(previous_trace)
    assert len(interrupted) == 1 and interrupted[0][2:] == (True, False), "must interrupt after a real swap"
    assert cache.read_bytes() == intended
    assert "compatibilityLevel: 1606" in (cache.parent.parent / "definition" / "database.tmdl").read_text()
    assert interrupted[0][:2] == (False, True)
    assert isinstance(error, refresh_pbip_model.CompatRollbackError) and error.args == ("TOOL_UNAVAILABLE",)
    assert published == []
    assert not hasattr(_abf, "ImageCommit")
    assert _no_staging_files(cache)


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


def test_a_raised_replace_refuses_success_without_rolling_alignment_under_a_possible_install(
    tmp_path: Path, monkeypatch
) -> None:
    """A raised replace is unestablished, not success; preserve alignment for a possibly installed image."""
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

    with pytest.raises(refresh_pbip_model.CompatRollbackError, match="unestablished"):
        refresh_pbip_model._persist_image(cache, model_dir, 1702, good_write)
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


def _native_imagesave(monkeypatch, cache, *, identity=None, content=None):
    """Native doubles only: retain the real persistence transaction, validators and rechecks."""
    bound = BoundDesktop(
        identity or DesktopIdentity(111, "100", 222, "101", 52001),
        "11111111-2222-3333-4444-555555555555",
    )
    events, calls = [], []
    content = _abf_bytes(seed=3) if content is None else content

    def stream(path, *_args):
        events.append("stream-open")
        return SimpleNamespace(path=Path(path), Close=lambda: events.append("stream-close"))

    def write(catalogue, handle):
        events.append("write")
        calls.append(catalogue)
        handle.path.write_bytes(content)

    server = SimpleNamespace(
        Databases=[SimpleNamespace(ID=bound.catalogue, CompatibilityLevel=1606)],
        Connect=lambda _: events.append("connect"),
        Disconnect=lambda: events.append("amo-disconnect"),
        ImageSave=write,
    )
    native_io = SimpleNamespace(
        FileAccess=SimpleNamespace(Write=1), FileMode=SimpleNamespace(Create=2), FileStream=stream
    )
    monkeypatch.setitem(sys.modules, "System.IO", native_io)
    monkeypatch.setattr(refresh_pbip_model, "_load_amo", lambda: lambda: server)
    monkeypatch.setattr(refresh_pbip_model, "desktop_identity", lambda *_: events.append("identity") or bound.identity)
    return SimpleNamespace(bound=bound, server=server, io=native_io, events=events, calls=calls, content=content)


def _save_observed(cache, native, **options):
    return refresh_pbip_model.image_save(
        native.bound.identity.port, cache, cache.parent.parent, bound=native.bound, return_observation=True, **options
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit-false"])
def test_imagesave_preserves_the_legacy_tuple(monkeypatch, tmp_path, explicit):
    cache = _model(tmp_path, compat=1604)
    native = _native_imagesave(monkeypatch, cache)
    options = {"return_observation": False} if explicit else {}
    result = refresh_pbip_model.image_save(52001, cache, cache.parent.parent, **options)
    assert result == (
        True,
        f"persisted via AMO ImageSave ({len(native.content) / 1024:.1f} KB, compatibilityLevel 1606)",
    )
    assert native.calls == [native.bound.catalogue] and native.events[-1] == "amo-disconnect"


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", 333),
        ("process_start", "200"),
        ("as_pid", 444),
        ("as_process_start", "201"),
        ("port", 52002),
    ],
)
def test_persistence_keeps_every_identity_component_even_for_one_catalogue(monkeypatch, tmp_path, field, value):
    cache = _model(tmp_path, compat=1604)
    first = _native_imagesave(monkeypatch, cache)
    prior = _save_observed(cache, first)
    second = _native_imagesave(monkeypatch, cache, identity=replace(first.bound.identity, **{field: value}))
    current = _save_observed(cache, second)
    assert prior.catalogue == current.catalogue and prior.image == current.image
    assert current.identity == second.bound.identity and prior != current, f"persistence must retain {field}"


@pytest.mark.parametrize("field", ["pid", "process_start", "as_pid", "as_process_start", "port", "catalogue"])
def test_persistence_final_recheck_refuses_each_changed_identity(monkeypatch, tmp_path, field):
    cache = _model(tmp_path, compat=1604)
    native = _native_imagesave(monkeypatch, cache)
    checked = refresh_pbip_model._checked_image
    entered = []

    def change_after_readback(path):
        result = checked(path)
        assert path == cache, "the mutation must occur after installed, not staged, readback"
        entered.append(field)
        if field == "catalogue":
            native.server.Databases[0].ID = "22222222-2222-3333-4444-555555555555"
        else:
            value = "200" if "start" in field else 777
            monkeypatch.setattr(
                refresh_pbip_model, "desktop_identity", lambda *_: replace(native.bound.identity, **{field: value})
            )
        return result

    monkeypatch.setattr(refresh_pbip_model, "_checked_image", change_after_readback)
    code = "CATALOGUE_CHANGED" if field == "catalogue" else "PID_REUSED"
    with pytest.raises(ObservationUnavailable, match=f"^{code}$"):
        _save_observed(cache, native)
    assert entered == [field] and native.calls == [native.bound.catalogue]
    assert native.events[-1] == "amo-disconnect" and cache.read_bytes() == native.content
    assert refresh_pbip_model.read_declared_compatibility(cache.parent.parent)[0] == 1606


def test_caller_mutation_and_failed_retry_cannot_supply_persistence_observations(monkeypatch, tmp_path):
    cache = _model(tmp_path, compat=1604)
    native = _native_imagesave(monkeypatch, cache)
    prior = _save_observed(cache, native)
    entered, release = threading.Event(), threading.Event()
    published = []
    write = native.server.ImageSave

    def blocked_write(*args):
        write(*args)
        entered.set()
        assert release.wait(5)

    native.server.ImageSave = blocked_write
    caller = threading.Thread(target=lambda: published.append(_save_observed(cache, native)))
    caller.start()
    try:
        assert entered.wait(3), "must mutate old caller state while this invocation is inside ImageSave"
        object.__setattr__(prior, "catalogue", "forged prior")
        object.__setattr__(prior.image, "installed_sha256", "forged digest")
        assert published == [] and caller.is_alive()
    finally:
        release.set()
        caller.join(5)
    assert not caller.is_alive() and len(published) == 1
    assert published[0].catalogue == native.bound.catalogue
    assert published[0].image.installed_sha256 == hashlib.sha256(native.content).hexdigest()

    def failed_retry(*_args):
        entered.clear()
        raise RuntimeError("retry failed")

    native.server.ImageSave = failed_retry
    with pytest.raises(ObservationUnavailable, match="^TOOL_UNAVAILABLE$"):
        published.append(_save_observed(cache, native))
    assert not entered.is_set() and len(published) == 1, "failed retry must enter the writer but publish nothing"


class _TrackedCacheReader:
    def __init__(self, handle, role, events):
        self.handle, self.role, self.events = handle, role, events
        self.reads, self.seeks = [], []

    def __enter__(self):
        self.handle.__enter__()
        self.events.append(f"{self.role}-open")
        return self

    def read(self, size=-1):
        assert 0 <= size <= 1024 * 1024, "cache reads must request at most 1 MiB"
        offset = self.handle.tell()
        chunk = self.handle.read(size)
        self.reads.append((offset, size, len(chunk)))
        return chunk

    def seek(self, offset):
        self.seeks.append(offset)
        return self.handle.seek(offset)

    def __exit__(self, *args):
        result = self.handle.__exit__(*args)
        self.events.append(f"{self.role}-exit")
        return result


@pytest.mark.parametrize("observe", [False, True], ids=["legacy-headers-only", "two-sequential-observation-passes"])
def test_persistence_cache_traversal_and_publication_order_are_exact(monkeypatch, tmp_path, observe):
    cache = _model(tmp_path, compat=1604)
    (tmp_path / "input_manifest.json").write_text(
        json.dumps({"generated_artifacts": {"version": 1, "run_id": "test-run"}}), encoding="utf-8"
    )
    payload = 1024 * 1024 + 13
    content = _abf_bytes(((_ABF_MAX_BLOCK_BYTES, payload), (128, 17)))
    native = _native_imagesave(monkeypatch, cache, content=content)
    events, readers, hashes = native.events, [], []
    open_file, replace_file = Path.open, os.replace
    declaration, lock = refresh_pbip_model._append_generated_edit_declaration, refresh_pbip_model.model_lock

    def open_path(path, mode="r", *args, **kwargs):
        handle = open_file(path, mode, *args, **kwargs)
        if mode == "rb" and path.name.startswith("cache.abf"):
            reader = _TrackedCacheReader(handle, "installed" if path == cache else "stage", events)
            readers.append(reader)
            return reader
        return handle

    def install(source, destination):
        result = replace_file(source, destination)
        if Path(destination) == cache:
            events.append("replace")
        return result

    def declare(*args):
        result = declaration(*args)
        events.append("declare")
        return result

    @contextmanager
    def model_lock(*args, **kwargs):
        with lock(*args, **kwargs):
            yield
        events.append("lock-exit")

    class CountingHash:
        def __init__(self):
            self.digest, self.count = hashlib.sha256(), 0
            hashes.append(self)

        def update(self, data):
            self.count += len(data)
            self.digest.update(data)

        def hexdigest(self):
            return self.digest.hexdigest()

    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(os, "replace", install)
    monkeypatch.setattr(refresh_pbip_model, "_append_generated_edit_declaration", declare)
    monkeypatch.setattr(refresh_pbip_model, "model_lock", model_lock)
    monkeypatch.setattr(_abf, "hashlib", SimpleNamespace(sha256=CountingHash))
    result = refresh_pbip_model.image_save(
        52001, cache, cache.parent.parent, bound=native.bound, return_observation=observe
    )
    if observe:
        next_header = 102 + 12 + payload
        expected = [
            (0, 102, 102),
            (102, 12, 12),
            (114, 1024 * 1024, 1024 * 1024),
            (114 + 1024 * 1024, 13, 13),
            (next_header, 12, 12),
            (next_header + 12, 17, 17),
            (len(content), 1, 0),
        ]
        assert [reader.role for reader in readers] == ["stage", "installed"], "exactly two cache opens/passes"
        assert all(reader.reads == expected and reader.seeks == [] for reader in readers), (
            "no skips, rewinds or rereads"
        )
        assert [digest.count for digest in hashes] == [len(content), len(content)], (
            "hash every consumed byte exactly once"
        )
        assert events == [
            "identity",
            "connect",
            "stream-open",
            "write",
            "stream-close",
            "stage-open",
            "stage-exit",
            "replace",
            "declare",
            "installed-open",
            "installed-exit",
            "identity",
            "lock-exit",
            "amo-disconnect",
        ]
        assert result.image.intended_sha256 == result.image.installed_sha256 == hashlib.sha256(content).hexdigest()
        assert result.image.intended_size == result.image.installed_size == len(content)
        assert result.image.commitment == "UNESTABLISHED"
    else:
        assert len(readers) == 1 and readers[0].role == "stage" and hashes == []
        assert readers[0].reads == [(0, 100, 100), (102, 12, 12), (102 + 12 + payload, 12, 12)]
        assert readers[0].seeks == [102, 102 + 12 + payload], "legacy validation must not traverse payload bytes"
        assert result[0] is True
    assert events[-2:] == ["lock-exit", "amo-disconnect"], "publish only after the contexts and teardown"


class _LogicalImage:
    """More than 3 GiB of logical ABF data, backed by one reusable 1 MiB payload buffer."""

    block_span = 2 * 1024 * 1024
    full_blocks = 1536
    last_payload = 31

    def __init__(self):
        self.size = 102 + self.full_blocks * self.block_span + 12 + self.last_payload
        self.position, self.consumed, self.maximum, self.eofs, self.closed = 0, 0, 0, 0, False
        self.payload = memoryview(bytes(1024 * 1024))

    def stat(self):
        return SimpleNamespace(st_size=self.size)

    def open(self, mode):
        assert mode == "rb" and self.position == 0, "one fresh sequential read only"
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def read(self, size):
        assert 0 < size <= 1024 * 1024, "multi-GiB input must never request a whole-cache allocation"
        self.maximum = max(self.maximum, size)
        if self.position == self.size:
            self.eofs += 1
            return b""
        if self.position < 102:
            data = (_ABF_PREAMBLE + b"\0\0")[self.position : self.position + size]
        else:
            index, within = divmod(self.position - 102, self.block_span)
            final = index == self.full_blocks
            payload = self.last_payload if final else self.block_span - 12
            if within < 12:
                header = struct.pack("<II", 128 if final else 2 * 1024 * 1024, payload + 4) + b"\x2a\xd7\x86\x4e"
                data = header[within : within + size]
            else:
                data = self.payload[: min(size, payload - (within - 12))]
        self.position += len(data)
        self.consumed += len(data)
        return data

    def seek(self, *_args):
        raise AssertionError("logical cache cannot be rewound or skipped")

    def read_bytes(self):
        raise AssertionError("whole-cache reads are forbidden")


def test_multigib_image_uses_bounded_sequential_reads_and_a_counting_hash(monkeypatch):
    logical = _LogicalImage()
    hashes = []

    class CountingHash:
        def __init__(self):
            self.count = 0
            hashes.append(self)

        def update(self, data):
            self.count += len(data)

        def hexdigest(self):
            return f"{self.count:064x}"

    monkeypatch.setattr(_abf, "hashlib", SimpleNamespace(sha256=CountingHash))
    digest, size = _abf._checked_image(logical)
    assert size == logical.size > 3 * 1024**3 and digest == f"{size:064x}"
    assert logical.consumed == size and logical.eofs == 1 and logical.closed
    assert logical.maximum == 1024 * 1024 and len(logical.payload) == 1024 * 1024
    assert len(hashes) == 1 and hashes[0].count == size, "hash byte-count must agree with EOF, not a stat proxy"


def test_small_image_uses_the_real_sha256_digest_and_byte_count(tmp_path):
    cache = tmp_path / "cache.abf"
    content = _abf_bytes(((_ABF_MAX_BLOCK_BYTES, 19), (128, 31)), seed=7)
    cache.write_bytes(content)
    assert _abf._checked_image(cache) == (hashlib.sha256(content).hexdigest(), len(content))


@pytest.mark.parametrize(
    "phase",
    [
        "stream-close",
        "stage-read",
        "stage-hash",
        "stage-exit",
        "replace",
        "declaration",
        "installed-read",
        "installed-hash",
        "installed-exit",
        "lock-exit",
    ],
)
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_required_persistence_failure_never_publishes_and_respects_installation(
    monkeypatch, tmp_path, capsys, phase, error_type
):
    cache = _model(tmp_path, compat=1604)
    cache.parent.mkdir()
    old = _abf_bytes(seed=1)
    cache.write_bytes(old)
    (tmp_path / "input_manifest.json").write_text(
        json.dumps({"generated_artifacts": {"version": 1, "run_id": "test-run"}}), encoding="utf-8"
    )
    native = _native_imagesave(monkeypatch, cache)
    attempted, published = [], []
    installed = False
    open_file, replace_file = Path.open, os.replace
    declaration, lock = refresh_pbip_model._append_generated_edit_declaration, refresh_pbip_model.model_lock
    file_stream = native.io.FileStream

    def fail():
        attempted.append(phase)
        raise error_type("PRIVATE_REQUIRED_FAILURE")

    def stream(*args):
        handle = file_stream(*args)
        close = handle.Close

        def finalize():
            close()
            if phase == "stream-close":
                fail()

        handle.Close = finalize
        return handle

    @contextmanager
    def open_path(path, mode="r", *args, **kwargs):
        role = (
            ("installed" if path == cache else "stage") if path.name.startswith("cache.abf") and mode == "rb" else None
        )
        with open_file(path, mode, *args, **kwargs) as handle:
            if role:
                native.events.append(f"{role}-read")
                if phase == f"{role}-read":
                    fail()
            yield handle
        if role:
            native.events.append(f"{role}-exit")
            if phase == f"{role}-exit":
                fail()

    def digest():
        if phase == ("installed-hash" if installed else "stage-hash"):
            fail()
        return hashlib.sha256()

    def install(source, destination):
        nonlocal installed
        if Path(destination) == cache and phase == "replace":
            fail()
        result = replace_file(source, destination)
        if Path(destination) == cache:
            installed = True
            native.events.append("replace")
        return result

    def declare(*args):
        result = declaration(*args)
        native.events.append("declare")
        if phase == "declaration":
            fail()
        return result

    @contextmanager
    def model_lock(*args, **kwargs):
        with lock(*args, **kwargs):
            yield
        native.events.append("lock-exit")
        if phase == "lock-exit":
            fail()

    with monkeypatch.context() as patch:
        patch.setattr(native.io, "FileStream", stream)
        patch.setattr(Path, "open", open_path)
        patch.setattr(os, "replace", install)
        patch.setattr(_abf, "hashlib", SimpleNamespace(sha256=digest))
        patch.setattr(refresh_pbip_model, "_append_generated_edit_declaration", declare)
        patch.setattr(refresh_pbip_model, "model_lock", model_lock)
        expected_type = KeyboardInterrupt if error_type is KeyboardInterrupt else ObservationUnavailable
        with pytest.raises(expected_type) as caught:
            published.append(_save_observed(cache, native))
    assert attempted == [phase] and "stream-close" in native.events, f"must reach the intended {phase} boundary"
    after_install = phase in {"declaration", "installed-read", "installed-hash", "installed-exit", "lock-exit"}
    assert installed is after_install and published == []
    assert cache.read_bytes() == (native.content if after_install else old)
    assert refresh_pbip_model.read_declared_compatibility(cache.parent.parent)[0] == (1606 if after_install else 1604)
    assert native.events[-1] == "amo-disconnect", "required failure must still attempt AMO teardown"
    assert caught.value.args == (() if error_type is KeyboardInterrupt else ("TOOL_UNAVAILABLE",))
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert "PRIVATE_REQUIRED_FAILURE" not in str(capsys.readouterr())


@pytest.mark.parametrize(
    "phase,error_type",
    [
        ("staging-exists", OSError),
        ("staging-exists", RuntimeError),
        ("staging-unlink", OSError),
        ("staging-unlink", RuntimeError),
        ("lock-unlink", OSError),
        ("amo-disconnect", OSError),
        ("amo-disconnect", RuntimeError),
    ],
)
def test_best_effort_persistence_cleanup_does_not_erase_established_facts(
    monkeypatch, tmp_path, capsys, phase, error_type
):
    cache = _model(tmp_path, compat=1604)
    native = _native_imagesave(monkeypatch, cache)
    attempted, stages = [], []
    replace_file, exists, unlink, os_unlink = os.replace, Path.exists, Path.unlink, os.unlink
    installed = False

    def fail():
        attempted.append(phase)
        raise error_type("PRIVATE_BEST_EFFORT_FAILURE")

    def install(source, destination):
        nonlocal installed
        result = replace_file(source, destination)
        if Path(destination) == cache:
            stages.append(Path(source))
            installed = True
            if phase == "staging-unlink":
                Path(source).write_bytes(b"leftover staging, not authority")
        return result

    def path_exists(path):
        if installed and path in stages and phase == "staging-exists":
            fail()
        return exists(path)

    def path_unlink(path, *args, **kwargs):
        if installed and path in stages and phase == "staging-unlink":
            fail()
        return unlink(path, *args, **kwargs)

    def remove(path, *args, **kwargs):
        if installed and Path(path) == cache.with_name("cache.abf.lock") and phase == "lock-unlink":
            fail()
        return os_unlink(path, *args, **kwargs)

    def disconnect():
        native.events.append("amo-disconnect")
        if phase == "amo-disconnect":
            fail()

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", install)
        patch.setattr(Path, "exists", path_exists)
        patch.setattr(Path, "unlink", path_unlink)
        patch.setattr(os, "unlink", remove)
        patch.setattr(native.server, "Disconnect", disconnect)
        result = _save_observed(cache, native)
    assert installed and attempted == [phase], "exercise the actual caught cleanup operation after replacement"
    assert type(result) is refresh_pbip_model.PersistenceObservation and result.identity == native.bound.identity
    assert result.image.installed_sha256 == hashlib.sha256(native.content).hexdigest()
    assert native.events[-1] == "amo-disconnect" and cache.read_bytes() == native.content
    assert "PRIVATE_BEST_EFFORT_FAILURE" not in str(capsys.readouterr())


@pytest.mark.parametrize("observe", [False, True], ids=["legacy", "observation"])
@pytest.mark.parametrize("phase", ["load", "write", "stream-close", "disconnect"])
@pytest.mark.parametrize(
    "kind,code",
    [
        ("native", "TOOL_UNAVAILABLE"),
        ("exit", "TOOL_UNAVAILABLE"),
        ("interrupt", None),
        ("known", "PID_REUSED"),
        ("forged", "TOOL_UNAVAILABLE"),
    ],
)
def test_imagesave_error_boundary_is_closed_and_legacy_errors_are_unchanged(
    monkeypatch, tmp_path, capsys, observe, phase, kind, code
):
    cache = _model(tmp_path, compat=1604)
    cache.parent.mkdir()
    old = _abf_bytes(seed=1)
    cache.write_bytes(old)
    native = _native_imagesave(monkeypatch, cache)
    sentinel = "PRIVATE_IMAGE_ENDPOINT_PATH_TOKEN_PAYLOAD"
    error = {
        "native": OSError(sentinel),
        "exit": SystemExit(sentinel),
        "interrupt": KeyboardInterrupt(sentinel),
        "known": ObservationUnavailable("PID_REUSED"),
        "forged": ObservationUnavailable(sentinel),
    }[kind]
    error.add_note(sentinel)
    cause = RuntimeError(sentinel)
    attempted, published = [], []

    def native_failure(*_args):
        attempted.append(phase)
        raise error from cause

    if phase == "load":
        monkeypatch.setattr(refresh_pbip_model, "_load_amo", native_failure)
    elif phase == "write":
        native.server.ImageSave = native_failure
    elif phase == "disconnect":
        native.server.Disconnect = native_failure
    else:
        file_stream = native.io.FileStream

        def stream(*args):
            handle = file_stream(*args)
            handle.Close = native_failure
            return handle

        native.io.FileStream = stream
    suppressed = observe and phase == "disconnect" and isinstance(error, Exception)
    if suppressed:
        published.append(
            refresh_pbip_model.image_save(
                52001, cache, cache.parent.parent, bound=native.bound, return_observation=observe
            )
        )
        assert type(published[0]) is refresh_pbip_model.PersistenceObservation
    else:
        with pytest.raises(BaseException) as caught:
            published.append(
                refresh_pbip_model.image_save(
                    52001, cache, cache.parent.parent, bound=native.bound, return_observation=observe
                )
            )
        assert published == []
        if observe:
            expected = KeyboardInterrupt if kind == "interrupt" else ObservationUnavailable
            assert type(caught.value) is expected and caught.value is not error
            assert caught.value.args == (() if code is None else (code,))
            assert caught.value.__context__ is caught.value.__cause__ is None
            assert not getattr(caught.value, "__notes__", ())
            rendered = "".join(traceback.format_exception(caught.value))
            assert sentinel not in rendered and "native_failure" not in rendered
        else:
            assert caught.value is error and caught.value.__cause__ is cause and caught.value.__notes__ == [sentinel]
    assert attempted == [phase], "privacy control must exercise the specified native phase"
    assert cache.read_bytes() == (native.content if phase == "disconnect" else old)
    assert refresh_pbip_model.read_declared_compatibility(cache.parent.parent)[0] == (
        1606 if phase == "disconnect" else 1604
    )
    if observe:
        assert sentinel not in str(capsys.readouterr())


@pytest.mark.parametrize("observe", [False, True], ids=["legacy", "observation"])
@pytest.mark.parametrize("error_type", [refresh_pbip_model.ModelLockTimeout, refresh_pbip_model.CompatRollbackError])
def test_lock_and_compatibility_errors_keep_only_their_public_type_in_observation_mode(
    monkeypatch, tmp_path, observe, error_type
):
    cache = _model(tmp_path, compat=1604)
    native = _native_imagesave(monkeypatch, cache)
    error = error_type("PRIVATE_LOCK_OR_COMPATIBILITY_DETAIL")
    error.add_note("PRIVATE_NOTE")
    entered = []

    @contextmanager
    def refuse_lock(*_args, **_kwargs):
        entered.append("lock-enter")
        raise error
        yield  # pragma: no cover - make entry failure a real context-manager failure

    monkeypatch.setattr(refresh_pbip_model, "model_lock", refuse_lock)
    with pytest.raises(error_type) as caught:
        refresh_pbip_model.image_save(52001, cache, cache.parent.parent, bound=native.bound, return_observation=observe)
    assert entered == ["lock-enter"] and native.calls == [], "must refuse at lock entry, before ImageSave"
    assert native.events[-1] == "amo-disconnect"
    if observe:
        assert caught.value is not error and caught.value.args == ("TOOL_UNAVAILABLE",)
        assert caught.value.__cause__ is caught.value.__context__ is None
        assert not getattr(caught.value, "__notes__", ())
    else:
        assert caught.value is error and caught.value.__notes__ == ["PRIVATE_NOTE"]


@pytest.mark.parametrize("phase", ["stage", "installed"])
@pytest.mark.parametrize("change", ["truncate", "append"])
def test_cache_eof_and_actual_byte_count_are_required_even_after_stat(monkeypatch, tmp_path, phase, change):
    cache = _model(tmp_path, compat=1604)
    cache.parent.mkdir()
    old = _abf_bytes(seed=1)
    cache.write_bytes(old)
    native = _native_imagesave(monkeypatch, cache)
    stat = Path.stat
    touched, published = [], []

    def stale_size(path, *args, **kwargs):
        result = stat(path, *args, **kwargs)
        is_target = (
            path == cache if phase == "installed" else path.name.startswith("cache.abf.") and path.suffix == ".tmp"
        )
        if is_target and not touched:
            touched.append(path)
            content = native.content[:-1] if change == "truncate" else native.content + b"unexpected tail"
            path.write_bytes(content)
        return result

    # Apply the race after the writer has closed, not to an earlier exists()/staging preparation stat.
    file_stream = native.io.FileStream

    def stream(*args):
        handle = file_stream(*args)
        close = handle.Close

        def finalize():
            close()
            monkeypatch.setattr(Path, "stat", stale_size)

        handle.Close = finalize
        return handle

    native.io.FileStream = stream
    with pytest.raises(ObservationUnavailable, match="^TOOL_UNAVAILABLE$"):
        published.append(_save_observed(cache, native))
    assert len(touched) == 1 and "stream-close" in native.events, "must race a real cache read after ImageSave"
    assert published == []
    expected = (
        old
        if phase == "stage"
        else (native.content[:-1] if change == "truncate" else native.content + b"unexpected tail")
    )
    assert cache.read_bytes() == expected
    assert refresh_pbip_model.read_declared_compatibility(cache.parent.parent)[0] == (
        1604 if phase == "stage" else 1606
    )


@pytest.mark.parametrize("observe", [False, True], ids=["legacy", "observation"])
def test_benign_native_imagesave_error_still_requires_a_valid_closed_image(monkeypatch, tmp_path, observe):
    cache = _model(tmp_path, compat=1604)
    native = _native_imagesave(monkeypatch, cache)
    write = native.server.ImageSave

    def benign(*args):
        write(*args)
        raise RuntimeError("The server sent an unrecognizable response")

    native.server.ImageSave = benign
    result = refresh_pbip_model.image_save(
        52001, cache, cache.parent.parent, bound=native.bound, return_observation=observe
    )
    assert native.events[-1] == "amo-disconnect" and "stream-close" in native.events
    if observe:
        assert result.image.installed_sha256 == hashlib.sha256(native.content).hexdigest()
    else:
        assert result[0] is True


@pytest.mark.parametrize("phase", ["staging-exists", "staging-unlink", "lock-unlink"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_persistence_cleanup_interrupts_cannot_publish(monkeypatch, tmp_path, phase, error_type):
    cache = _model(tmp_path, compat=1604)
    native = _native_imagesave(monkeypatch, cache)
    stages, entered, published = [], [], []
    replace_file, exists, unlink, os_unlink = os.replace, Path.exists, Path.unlink, os.unlink

    def fail():
        entered.append(phase)
        raise error_type("PRIVATE_CLEANUP_INTERRUPT")

    def install(source, destination):
        result = replace_file(source, destination)
        if Path(destination) == cache:
            stages.append(Path(source))
            if phase == "staging-unlink":
                Path(source).write_bytes(b"leftover stage")
        return result

    def path_exists(path):
        if path in stages and phase == "staging-exists":
            fail()
        return exists(path)

    def path_unlink(path, *args, **kwargs):
        if path in stages and phase == "staging-unlink":
            fail()
        return unlink(path, *args, **kwargs)

    def remove(path, *args, **kwargs):
        if stages and Path(path) == cache.with_name("cache.abf.lock") and phase == "lock-unlink":
            fail()
        return os_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", install)
        patch.setattr(Path, "exists", path_exists)
        patch.setattr(Path, "unlink", path_unlink)
        patch.setattr(os, "unlink", remove)
        expected = KeyboardInterrupt if error_type is KeyboardInterrupt else ObservationUnavailable
        with pytest.raises(expected) as caught:
            published.append(_save_observed(cache, native))
    assert entered == [phase] and len(stages) == 1, "interrupt must enter actual cleanup after replacement"
    assert published == [] and caught.value.args == (() if error_type is KeyboardInterrupt else ("TOOL_UNAVAILABLE",))
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert cache.read_bytes() == native.content
    assert refresh_pbip_model.read_declared_compatibility(cache.parent.parent)[0] == 1606
    assert native.events[-1] == "amo-disconnect"


@pytest.mark.parametrize("observe", [False, True], ids=["legacy-fallback", "observation-refusal"])
def test_observation_refusal_cannot_fall_through_to_ui_save(monkeypatch, tmp_path, capsys, observe):
    cache = _model(tmp_path, compat=1604)
    native = _native_imagesave(monkeypatch, cache)
    entered, ui_saves = [], []
    image_save = refresh_pbip_model.image_save

    def failed_write(*_args):
        entered.append("native-write")
        raise RuntimeError("PRIVATE_WRITE_FAILURE")

    def selected_mode(port, path, model_dir):
        return image_save(port, path, model_dir, bound=native.bound, return_observation=observe)

    native.server.ImageSave = failed_write
    monkeypatch.setattr(refresh_pbip_model, "image_save", selected_mode)
    monkeypatch.setattr(refresh_pbip_model, "refresh", lambda *_a, **_k: (True, "refreshed"))
    monkeypatch.setattr(refresh_pbip_model, "save", lambda pid: ui_saves.append(pid) or (True, "UI save"))
    args = refresh_pbip_model._build_arg_parser().parse_args(["--pid", "111"])
    result = refresh_pbip_model._refresh_and_save(111, 52001, cache, args)
    assert entered == ["native-write"] and native.events[-1] == "amo-disconnect"
    if observe:
        assert result == 1 and ui_saves == [], "typed observation refusal must stop, never invoke the fallback"
        assert "PRIVATE_WRITE_FAILURE" not in str(capsys.readouterr())
    else:
        assert result is None and ui_saves == [111], "ordinary legacy unavailability still permits the UI path"
