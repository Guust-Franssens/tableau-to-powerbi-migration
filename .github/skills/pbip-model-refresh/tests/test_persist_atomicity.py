"""The persisted cache write must be atomic, and a failed persist must not leave the project bumped.

Two guarantees this file pins, both from #113:

1. **Atomic swap.** `ImageSave` opens `cache.abf` with `FileMode.Create`, which TRUNCATES a good
   cache the instant the write begins - so a write that then fails half way leaves the project WORSE
   than before (no fresh cache and no old one). `_staged_image_write` writes `cache.abf.tmp` and only
   `os.replace`s it over the original once it exists and is non-empty, so a failed/partial write can
   never destroy an existing good cache.
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
from pathlib import Path

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


def test_a_raising_write_with_no_output_leaves_the_cache_intact(tmp_path: Path) -> None:
    """`write_image` is allowed to RAISE (the AMO client throws mid-write); judge by the file, not the
    exception - and a raise that produced nothing must still not clobber the existing cache."""
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"GOOD-EXISTING-CACHE")

    def raise_without_writing(_staging: Path) -> None:
        raise RuntimeError("The server sent an unrecognizable response")

    assert refresh_pbip_model._staged_image_write(cache, raise_without_writing) is False
    assert cache.read_bytes() == b"GOOD-EXISTING-CACHE"
    assert not cache.with_name(cache.name + ".tmp").exists()


def test_a_raising_write_that_still_produced_bytes_counts_as_a_write(tmp_path: Path) -> None:
    """The real client raises WHILE writing correctly, so a non-empty staging file is a success even
    though the call raised - success is the FILE, never the absence of an exception."""
    cache = _model(tmp_path)

    def raise_after_writing(staging: Path) -> None:
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"NEW-CACHE-BYTES")
        raise RuntimeError("The server sent an unrecognizable response")

    assert refresh_pbip_model._staged_image_write(cache, raise_after_writing) is True
    assert cache.read_bytes() == b"NEW-CACHE-BYTES"
    assert not cache.with_name(cache.name + ".tmp").exists(), "the staging file must be swapped, not left"


def test_a_successful_write_swaps_the_new_cache_in(tmp_path: Path) -> None:
    """The happy path: staging is written, then atomically replaces the destination."""
    cache = _model(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"OLD")

    def write_new(staging: Path) -> None:
        staging.write_bytes(b"NEW-CACHE-BYTES")

    assert refresh_pbip_model._staged_image_write(cache, write_new) is True
    assert cache.read_bytes() == b"NEW-CACHE-BYTES"


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

    def good_write(staging: Path) -> None:
        staging.write_bytes(b"CACHE")

    ok, message = refresh_pbip_model._persist_image(cache, model_dir, 1702, good_write)
    assert ok is True
    assert "1702" in message
    assert "compatibilityLevel: 1702" in database_tmdl.read_text(encoding="utf-8")
    assert cache.read_bytes() == b"CACHE"


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
