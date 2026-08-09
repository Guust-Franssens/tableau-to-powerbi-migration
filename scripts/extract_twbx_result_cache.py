#!/usr/bin/env python
"""purpose: recover Tableau's OWN computed values from a packaged workbook's embedded result cache.
usage:   python scripts/extract_twbx_result_cache.py <book.twbx> --out _oracle/twbx-cache.json

Why this exists
---------------
``scripts/capture_tableau_oracle.py`` fills the "Tableau's own numbers" socket, but it needs a live
Tableau Cloud/Server site and a PAT. For an offline ``.twbx`` -- a teaching workbook, a customer file
dropped in a folder, anything with no server behind it -- that socket stayed empty, and verification
degraded to "re-derive the value ourselves", which is exactly the self-consistency trap the oracle
exists to avoid: a wrong grain assumption hides on both sides of the comparison and the numbers agree.

But a ``.twbx`` saved from Desktop frequently *carries Tableau's answers with it*. Under
``TwbxExternalCache/TwbxResultsCacheV3/<hash>/`` each query Desktop ran is stored as a pair:

* ``.key`` -- the cache key: readable XML naming the query's fields, including
  ``has-lod-calcs='true'`` when the query involved an LOD expression.
* ``.bin`` -- the **result tuples Tableau computed**, aggregated exactly as displayed.

That is a true independent oracle: values produced by Tableau's own query engine, immune to any
assumption this toolkit makes about LOD grain.

Binary format (reverse-engineered, Tableau 19.1-2022.x ``TwbxResultsCacheV3``)
-----------------------------------------------------------------------------
A 16-byte file header, then a flat stream of length-prefixed records::

    <uint16 tag> <uint16 0xFFFF> <uint32 length> <payload[length]>

``tag == 2`` is a UTF-16LE string -- first the ``<metadata-record class='column'>`` blocks (one per
output column, in column order), then string cell values. ``tag == 1`` is an 8-byte little-endian
IEEE **double**; ``tag == 0`` is an 8-byte little-endian **int64** (integer measures, and date parts
such as a ``Year`` derivation, which arrive as the plain year number). Cells stream in row-major
order, so the column count from the metadata records re-shapes the flat cell stream into tuples.

That tag-0/tag-1 split is the one trap worth calling out: an int64 cell read as a double comes back
as a denormal that prints as ``0.000000`` rather than raising, so a decoder that handles only tag 1
silently drops every integer and date-part column and then mis-shapes every row after it. The
symptom is plausible-looking numbers in the wrong columns -- the exact failure mode an oracle exists
to prevent.

Caveats worth stating in any finding built on this
--------------------------------------------------
* The cache holds queries Desktop *happened to run* before the last save. A worksheet never opened in
  that session may have no cache entry -- absence is not evidence.
* Values are raw doubles as computed, NOT display-formatted (unlike the REST ``/data`` endpoint), so
  no de-formatting is needed, but neither is any rounding applied.
* Entries are keyed by query shape, not by worksheet; ``--match`` filters by field name.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import struct
import sys
import zipfile
from typing import Any

CACHE_DIR = "TwbxExternalCache/TwbxResultsCacheV3"
_HEADER_BYTES = 16
_MARKER = 0xFFFF
_TAG_INT = 0
_TAG_NUMBER = 1
_TAG_STRING = 2

_ALIAS_RE = re.compile(r"<remote-alias>(.*?)</remote-alias>", re.S)
_LOCALTYPE_RE = re.compile(r"<local-type>(.*?)</local-type>", re.S)
_AGG_RE = re.compile(r"<aggregation>(.*?)</aggregation>", re.S)
_KEY_FIELD_RE = re.compile(r"&lt;(?:field|output) column=&apos;\[[^\]]+\]\.\[([^\]]+)\]&apos;")


def _records(blob: bytes):
    """Yield (tag, payload) records from a ``.bin`` cache body."""
    off = _HEADER_BYTES
    n = len(blob)
    while off + 8 <= n:
        tag, marker, length = struct.unpack_from("<HHI", blob, off)
        if marker != _MARKER or off + 8 + length > n:
            off += 1  # resync: tolerate unknown header padding rather than abort
            continue
        yield tag, blob[off + 8 : off + 8 + length]
        off += 8 + length


def decode_bin(blob: bytes) -> dict[str, Any]:
    """Decode one ``.bin`` into {columns: [...], rows: [[...]]}."""
    columns: list[dict[str, str | None]] = []
    cells: list[Any] = []
    in_metadata = True
    for tag, payload in _records(blob):
        if tag == _TAG_STRING:
            text = payload.decode("utf-16-le", "replace")
            if in_metadata and "<metadata-record" in text:
                columns.append(
                    {
                        "alias": (m.group(1) if (m := _ALIAS_RE.search(text)) else None),
                        "local_type": (m.group(1) if (m := _LOCALTYPE_RE.search(text)) else None),
                        "aggregation": (m.group(1) if (m := _AGG_RE.search(text)) else None),
                    }
                )
                continue
            in_metadata = False
            cells.append(text)
        elif tag == _TAG_NUMBER and len(payload) == 8:
            in_metadata = False
            cells.append(struct.unpack("<d", payload)[0])
        elif tag == _TAG_INT and len(payload) == 8:
            # int64: integer measures and date-part derivations (a 'Year' arrives as 2018, not a serial)
            in_metadata = False
            cells.append(struct.unpack("<q", payload)[0])
        else:
            in_metadata = False
            cells.append(None)  # unknown cell tag: keep the slot so row shape survives

    width = len(columns) or 1
    rows = [cells[i : i + width] for i in range(0, len(cells), width)]
    ragged = bool(rows) and len(rows[-1]) != width
    if ragged:  # never emit a partial tuple as if it were complete
        rows = rows[:-1]
    return {"columns": columns, "rows": rows, "ragged_tail_dropped": ragged}


def _typecheck(entry: dict[str, Any]) -> list[str]:
    """Loudly flag any column whose decoded python type contradicts its declared local-type.

    A silent format drift is the whole risk with a reverse-engineered container, so this turns
    'the numbers look plausible' into a checkable claim.
    """
    expected = {"real": float, "integer": int, "date": int, "datetime": int}
    problems: list[str] = []
    for idx, col in enumerate(entry["columns"]):
        want = expected.get(str(col.get("local_type")))
        if want is None:
            continue
        for row in entry["rows"]:
            got = row[idx]
            if got is not None and not isinstance(got, want):
                problems.append(
                    f"column {idx} ({col.get('alias')}) declared {col.get('local_type')} "
                    f"but decoded {type(got).__name__}"
                )
                break
    return problems


def read_key(text: str) -> dict[str, Any]:
    """Parse a cache entry's `.key` sidecar: whether it carries LOD calcs, and its field names."""
    return {
        "has_lod_calcs": "has-lod-calcs=&apos;true&apos;" in text,
        "fields": sorted(set(_KEY_FIELD_RE.findall(text))),
    }


def extract(twbx: pathlib.Path) -> list[dict[str, Any]]:
    """Return every decoded result-cache entry in `twbx`, LOD-bearing ones first."""
    entries: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(twbx) as z:
        for name in z.namelist():
            if not name.startswith(CACHE_DIR):
                continue
            parts = name.split("/")
            if len(parts) < 4:
                continue
            slot = entries.setdefault(parts[2], {"id": parts[2]})
            if name.endswith(".key"):
                slot["key"] = z.read(name).decode("utf-8", "replace")
            elif name.endswith(".bin"):
                slot["bin"] = z.read(name)

    out: list[dict[str, Any]] = []
    for slot in entries.values():
        if "bin" not in slot:
            continue
        rec: dict[str, Any] = {"cache_id": slot["id"]}
        rec.update(read_key(slot.get("key", "")))
        rec.update(decode_bin(slot["bin"]))
        rec["decode_warnings"] = _typecheck(rec)
        out.append(rec)
    out.sort(key=lambda r: (not r["has_lod_calcs"], r["cache_id"]))
    return out


def main() -> int:
    """CLI entry point: decode a .twbx's result cache and print (or write) the entries."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("twbx", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, help="write JSON here (default: stdout summary only)")
    ap.add_argument("--match", help="only entries whose field names match this regex (case-insensitive)")
    ap.add_argument("--lod-only", action="store_true", help="only entries whose key says has-lod-calcs=true")
    ap.add_argument("--max-rows", type=int, default=25, help="rows to print per entry (default 25)")
    args = ap.parse_args()

    if not args.twbx.exists():
        print(f"[FAIL] no such file: {args.twbx}", file=sys.stderr)
        return 2

    entries = extract(args.twbx)
    if args.lod_only:
        entries = [e for e in entries if e["has_lod_calcs"]]
    if args.match:
        pat = re.compile(args.match, re.I)
        entries = [e for e in entries if any(pat.search(f) for f in e["fields"])]

    if not entries:
        print(
            "[WARN] no result-cache entries matched -- this .twbx may have been saved without a "
            "query cache, or the worksheets were never rendered before saving."
        )
        return 1

    for e in entries:
        aliases = [c["alias"] for c in e["columns"]]
        print(f"\n=== cache {e['cache_id']}  lod={e['has_lod_calcs']}  rows={len(e['rows'])}")
        print("    columns: " + " | ".join(str(a) for a in aliases))
        for w in e["decode_warnings"]:
            print(f"    [WARN] {w}")
        if e.get("ragged_tail_dropped"):
            print("    [WARN] a ragged trailing tuple was dropped (cell stream did not divide evenly)")
        for row in e["rows"][: args.max_rows]:
            print("      " + " | ".join(f"{v:.4f}" if isinstance(v, float) else str(v) for v in row))
        if len(e["rows"]) > args.max_rows:
            print(f"      ... {len(e['rows']) - args.max_rows} more row(s)")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "kind": "tableau-twbx-result-cache-oracle",
                    "version": 1,
                    "source": str(args.twbx),
                    "note": "Values computed by Tableau Desktop itself and cached inside the .twbx; "
                    "absence of an entry is not evidence of absence.",
                    "entries": entries,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n[OK] wrote {args.out}  ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
