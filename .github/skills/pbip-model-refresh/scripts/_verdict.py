"""
purpose: choose the canary set and print the machine-readable verdict for a refresh run
usage:   imported by refresh_pbip_model.py (re-exported there; not a CLI of its own)

Split out of `refresh_pbip_model.py` to keep that module under its line cap. This self-contained
layer decides WHICH tables get verified and turns the results into the one line a calling agent
parses. It touches no engine and no filesystem beyond stat()ing the cache, so it is cheap to test
directly.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class DataVerdict:
    """Legacy data/scope vocabulary, not source coverage or a persistence observation."""

    code: Literal["NO_DATA", "TABLE_OK", "DATA_OK"]
    empty_tables: tuple[str, ...]


def derive_data_verdict(results: list[tuple[str, int]], implicit: bool) -> DataVerdict:
    """Derive the shared legacy classification; observation producers validate their inputs.

    Keep historical CLI semantics here, including repeated canaries. The stricter observation
    API refuses empty/duplicate caller sets and non-integer counts before reaching this layer.
    No cache timestamps, UI flags or save booleans enter this record.
    """
    empty = tuple(table for table, rows in results if rows <= 0)
    code = "NO_DATA" if empty else "TABLE_OK" if implicit else "DATA_OK"
    return DataVerdict(code, empty)


def _canary_tables(args: argparse.Namespace) -> list[str] | None:
    """The tables to verify: the named canaries, or None so the first queryable table is probed."""
    return list(args.canaries) if args.canaries else None


def _emit_data_verdict(
    cache: Path | None,
    before_stamp: float,
    args: argparse.Namespace,
    results: list[tuple[str, int]],
    implicit: bool,
) -> int:
    """Print the data/cache lines and the machine-readable verdict; return the process exit code.

    The cache timestamp check is compatibility output only, never a persistence observation.
    `--save-only` earns `SAVE_OK`, never a claim of fresh source data.
    """
    after_stamp = cache.stat().st_mtime if cache and cache.exists() else 0.0
    persisted = cache is not None and after_stamp > before_stamp

    for table, rows in results:
        print(f"  data   : {rows} row(s) in '{table}'")
    # An external timestamp change is not evidence of an unrequested save by this invocation.
    if args.verify_only:
        print("  cache  : persistence not requested (--verify-only; no refresh or save was run)")
    elif args.no_save:
        print("  cache  : persistence not requested (--no-save; refreshed data remains in memory only)")
    elif persisted:
        print(f"  cache  : legacy timestamp check observed a newer cache -> {cache} (cold reopen not tested)")
    elif cache is None:
        print("  cache  : not persisted (no cache path resolved)")
    else:
        print("  cache  : legacy timestamp check did not observe a newer cache (write outcome unconfirmed)")

    verdict = derive_data_verdict(results, implicit)
    if verdict.code == "NO_DATA":
        empty = verdict.empty_tables
        print(f"REFRESH: NO_DATA (empty: {', '.join(empty)} - check the source and credentials)")
        return 1
    wanted_save = not args.no_save and not args.verify_only
    if wanted_save and not persisted:
        print(
            "REFRESH: NOT_PERSISTED (legacy timestamp check did not observe a newer cache.abf; "
            "write outcome unconfirmed)"
        )
        return 1
    if getattr(args, "save_only", False):
        print("REFRESH: SAVE_OK")
        return 0
    suffix = " + PERSISTED" if wanted_save else ""
    if verdict.code == "TABLE_OK":
        # No canaries were named, so only the first queryable table was probed. That is NOT a
        # model-level guarantee (a static parameter/CSV table can pass while a live source never
        # loaded), so the verdict names the single table actually probed instead of claiming DATA_OK.
        only = results[0][0]
        print(f"REFRESH: TABLE_OK '{only}'{suffix}")
        print(
            f"  note   : single-table probe of '{only}' only - NOT a model-level DATA_OK. A static "
            "parameter/CSV table can return rows while a live source never loaded. Pass "
            "--canaries <one per live source> to certify every source "
            "(this mirrors the powerbi-semantic-model-gotchas rule: prove a REAL read per live source)."
        )
        return 0
    print(f"REFRESH: DATA_OK{suffix}")
    return 0
