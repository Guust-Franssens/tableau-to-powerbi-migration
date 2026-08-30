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
* **#353** - the gate is armed per connection **leg** while the probe enumerated **datasources** and
  read one outer connection. A marker naming three legs cleared in full after contacting the outer
  `federated` connection, which carries no server at all.

The common shape, and the thing to check in review: **the clearing side reasoning in a key space the
marker does not use.** Counts, indices and datasources are all wrong units; the marker holds names.
This helper now accepts ONLY the marker's stable source keys, already proven by the probe.
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
    """How many source keys the blocking marker still names. 0 when absent or unreadable."""
    try:
        payload = json.loads((migration / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    names = payload.get("sources") if isinstance(payload, dict) else None
    return len(names) if isinstance(names, list) else 0


def lift_gate(migration: Path, what: str, source_names: list[str]) -> bool:
    """Record an EARNED clear for the exact marker source keys the probe reached.

    The previous #357 cardinality guard refused when the marker named more sources than the probe
    contacted. That was safe while the probe still spoke datasource counts, but wrong once the probe
    speaks marker keys: a partial source-aware clear is legitimate and is handled by
    `credential_gate.py clear --sources`.

    An empty list is never proof. This is the non-negotiable #348 fail-open guard: zero contacted
    endpoints must not clear a marker just because an empty set comparison happens to pass.
    """
    if not source_names:
        log.warning(
            "PROBE: gate NOT lifted - no marker source keys were proven. Zero proof must never "
            "clear a credential gate. See issues #348 and #353.",
        )
        return False
    named = marker_named_count(migration)
    if named > len(source_names):
        log.warning(
            "PROBE: gate NOT lifted - the marker names %d source key(s) but only %d were proven. "
            "Keeping the conservative #357 refusal: partial proof is a loud, recoverable block; "
            "over-clearing ships an unvalidated model.",
            named,
            len(source_names),
        )
        return False
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "credential_gate.py"),
            "clear",
            str(migration),
            "--reason",
            f"probe-cleared: DATA_OK from {what}",
            "--earned",
            "--sources",
            *source_names,
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        log.warning("PROBE: gate clear command failed; gate is still authoritative.")
        return False
    return True
