"""
purpose: choose the canary set and print the machine-readable verdict for a refresh run
usage:   imported by refresh_pbip_model.py (re-exported there; not a CLI of its own)

Split out of `refresh_pbip_model.py` for the same reason as `_abf` and `_lock`: that module is at its
line cap, and this is a self-contained layer - it decides WHICH tables get verified and turns the
results into the one line a calling agent parses. It touches no engine and no filesystem beyond
stat()ing the cache, so it is cheap to test directly.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _canary_tables(args: argparse.Namespace) -> list[str] | None:
    """The tables to VERIFY, which is deliberately NOT the same knob as the tables to REFRESH.

    Coupling them (the round-3 behaviour: ``--tables`` was both) built a trap. The only documented way
    to earn a model-level ``DATA_OK`` was to name canaries in ``--tables`` - which simultaneously
    narrowed the refresh to just those tables, certifying a MODEL-level verdict over a PARTIALLY
    refreshed model. An agent following the host-repo gate would have done exactly that, and the
    "next agent gets an empty model" failure this bundle exists to prevent would have shipped wearing
    a green verdict. ``--canaries`` verifies without narrowing; ``--tables`` still supplies the canary
    set when it is the only flag given, so existing callers keep working - but see
    :func:`_emit_data_verdict`, which no longer lets a narrowed refresh claim a model-level verdict.
    """
    if args.canaries:
        return list(args.canaries)
    return list(args.tables) if args.tables else None


def _emit_data_verdict(
    cache: Path | None,
    before_stamp: float,
    args: argparse.Namespace,
    results: list[tuple[str, int]],
    implicit: bool,
) -> int:
    """Print the data/cache lines and the machine-readable verdict; return the process exit code.

    Split out of `main()` so the mutating path (identity gate + refresh + persist) and the reporting
    path stay individually simple.
    """
    after_stamp = cache.stat().st_mtime if cache and cache.exists() else 0.0
    persisted = cache is not None and after_stamp > before_stamp

    for table, rows in results:
        print(f"  data   : {rows} row(s) in '{table}'")
    # Say what HAPPENED, not where a file would go. Printing the path alone reads as "written" -
    # it misled a reader on 2026-08-05 into believing a probe run had persisted a 1-row cache.
    # For a probe that distinction matters twice over: a persisted 1-row `cache.abf` is a trap, and
    # a cache whose compatibility level disagrees with the project's makes the PBIP unopenable (see
    # `--no-save`; `image_save` prevents that by aligning `database.tmdl`).
    if persisted:
        print(f"  cache  : PERSISTED -> {cache}")
    elif cache is None:
        print("  cache  : not persisted (no cache path resolved)")
    elif args.no_save:
        print("  cache  : not persisted (--no-save; the project is byte-identical)")
    else:
        # Persisting was requested (the default) and nothing landed. Naming the wrong reason here
        # sent me looking in the wrong place for ten minutes; the real one is on the 'save' line.
        print("  cache  : not persisted (the write did not land - see 'save' above)")

    empty = [table for table, rows in results if rows <= 0]
    if empty:
        print(f"REFRESH: NO_DATA (empty: {', '.join(empty)} - check the source and credentials)")
        return 1
    wanted_save = not args.no_save and not args.verify_only
    if wanted_save and not persisted:
        print("REFRESH: NOT_PERSISTED (model has data in memory, but cache.abf did not update)")
        return 1
    suffix = " + PERSISTED" if wanted_save else ""
    if implicit:
        # No canaries were named, so only the first queryable table was probed. That is NOT a
        # model-level guarantee (a static parameter/CSV table can pass while a live source never
        # loaded), so the verdict names the single table actually probed instead of claiming DATA_OK.
        only = results[0][0]
        print(f"REFRESH: TABLE_OK '{only}'{suffix}")
        print(
            f"  note   : single-table probe of '{only}' only - NOT a model-level DATA_OK. A static "
            "parameter/CSV table can return rows while a live source never loaded. Pass "
            "--canaries <one per live source> to certify every source WITHOUT narrowing the refresh "
            "(this mirrors the powerbi-semantic-model-gotchas rule: prove a REAL read per live source)."
        )
        return 0
    if args.tables and not args.verify_only:
        # Canaries all returned rows, but --tables narrowed the REFRESH, so the tables outside that
        # list still hold whatever they held before (possibly nothing). A model-level DATA_OK over a
        # partially refreshed model is exactly the false certificate #115 was filed about, so the
        # verdict is scoped to what was actually refreshed and verified.
        named = ", ".join(f"'{table}'" for table, _ in results)
        print(f"REFRESH: TABLES_OK {named}{suffix}")
        print(
            "  note   : --tables narrowed the REFRESH, so this is NOT a model-level DATA_OK - tables "
            "outside that list were not reloaded. Re-run without --tables (add --canaries <one per "
            "live source>) to certify the whole model."
        )
        return 0
    print(f"REFRESH: DATA_OK{suffix}")
    return 0
