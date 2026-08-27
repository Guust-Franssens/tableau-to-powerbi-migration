"""Deciding and recording an EARNED credential-gate clear.

The third seam split out of `probe_live_source.py` under its documented "SPLIT, not waive"
module-size strategy, after `_verdict_lines.py` (verdict matchers) and `_probe_pbip.py` (PBIP
scaffold writers). Argued on its own merits: *reaching* a source and *deciding whether that earned a
clear* are different jobs. The probe knows what it contacted; the gate knows what it was armed over;
only this module needs both, and it is the piece that keeps growing as new fail-open shapes are
found. Re-exported by `probe_live_source` because the seam tests reach it through that module.

Why the guards are shaped the way they are - three fail-opens, all measured, all in one week:

* **#346** - the probe passed a human-readable count ("2 live source(s)") as `--sources`.
  `clear_block` diffs that against the marker's real names, matches none, and takes its
  partial-clear branch: gate left ARMED while a `probe-cleared` audit entry is written and the
  process exits 0. The earned route was broken on every real estate, so `authorize` - which marks a
  build permanently UNVALIDATED - looked like the only thing that worked.
* **#348** - the first fix derived proof from `set(live) >= set(all_live)`. A superset test is
  vacuously true against an empty set, so a bundle with **0** live sources cleared the gate having
  proved nothing at all. Fail-OPEN, and worse than the bug it replaced.
* **#353** - the gate is armed per connection **leg** (`_classify_legs` walks
  `connection.connections[]`, because one Tableau datasource can join Azure SQL + Snowflake +
  Databricks) while the probe enumerates **datasources** and reads one outer connection. A marker
  naming three legs cleared in full after contacting the outer `federated` connection, which carries
  no server at all.

The common shape, and the thing to check in review: **the clearing side reasoning in a key space the
marker does not use.** Counts, indices and datasources are all wrong units; the marker holds names.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("probe_live_source")

MARKER_NAME = ".credential-gate-BLOCKED.json"


def marker_named_count(migration: Path) -> int:
    """How many sources the blocking marker NAMES. 0 when absent, unreadable or unnamed.

    Read straight from the marker rather than through `credential_gate`, which the probe only ever
    shells out to. `0` is the safe answer for an unreadable marker: it cannot then trigger a refusal
    on garbage, and `clear_block` still refuses to clear names it cannot match.
    """
    try:
        payload = json.loads((migration / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    names = payload.get("sources") if isinstance(payload, dict) else None
    return len(names) if isinstance(names, list) else 0


def lift_gate(migration: Path, what: str, proved_all: bool, contacted: int) -> bool:
    """Record an EARNED clear if - and only if - both guards pass. Returns whether one was requested.

    1. **`proved_all`** - exactly `source_index is None`. Keep it that simple (#348).
    2. **Cardinality** - the marker must not NAME more sources than the probe CONTACTED (#353).

    Cardinality is a COUNT, not a name match: naming one leg is still unsolved (#347). It is not the
    set arithmetic that caused #348 - endpoints reached and names claimed are both quantities of
    proof - and it is fail-safe in the case that broke #348: 0 contacted vs N named refuses.

    ⚠️ `what` is a human-readable count and must NEVER be passed as `--sources` (#346).

    Full rationale and measurements: `docs/credential-gate.md`.
    """
    if not proved_all:
        log.warning(
            "PROBE: gate NOT lifted - a --source-index run proves only one source (%s), and a clear "
            "would lift a gate covering sources never contacted. Re-run without --source-index to "
            "earn a clear. See issues #346 and #347.",
            what,
        )
        return False
    named = marker_named_count(migration)
    if named > contacted:
        log.warning(
            "PROBE: gate NOT lifted - the marker names %d source(s) but only %d endpoint(s) were "
            "contacted. A federated datasource joins several systems behind ONE source entry, so "
            "clearing here would lift a gate covering systems nobody reached. Probe each system, or "
            "authorize a build-only migration explicitly. See issue #353.",
            named,
            contacted,
        )
        return False
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "credential_gate.py"),
            "clear",
            str(migration),
            "--reason",
            f"probe-cleared: DATA_OK from {what}",
            "--earned",
        ],
        capture_output=True,
        check=False,
    )
    return True
