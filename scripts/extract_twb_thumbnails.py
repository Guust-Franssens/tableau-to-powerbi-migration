#!/usr/bin/env python
"""
purpose: extract the Tableau-rendered PNG thumbnails that a ``.twb``/``.twbx`` embeds, one per
         worksheet, into a reference folder the validator can use as ground truth.
usage:   python scripts/extract_twb_thumbnails.py <workbook.twbx|.twb> -o <reference-dir>

Why this exists
---------------
``capture_tableau_reference.py`` and ``capture_tableau_oracle.py`` both need a **live** Tableau
Cloud/Server site or a Tableau Public URL. For a packaged ``.twbx`` handed over offline -- which is the
common case in a migration engagement -- every agent in the pipeline concluded "no reference images
exist" and fell back to structural-only critique, explicitly disclaiming any visual-fidelity claim.

That conclusion was wrong, and it cost a real migration a wrong verdict. A Tableau workbook embeds a
``<thumbnails>`` block near the end of the XML holding one base64 PNG per worksheet -- an actual
Tableau render, produced by Tableau, with no server, no credential and no network call.

Measured on ``book_5-1-Table-Calcs.twbx`` (2026-08-08): a static reading of ``<mark class='Automatic'/>``
plus a discrete ``:ok`` date pill led two independent agents to conclude the two "Bump" worksheets were
**column** charts, and a report was built and signed off that way. The embedded thumbnail shows ~17
crossing **lines** with rank 1 at the top -- a textbook bump chart. The stacked-column rebuild summed
17 rank values to ~153, i.e. it was not merely a different shape but numerically meaningless. One
192x192 PNG settled in seconds what XML inference had gotten backwards.

Rule of thumb this encodes: **Tableau's ``Automatic`` mark resolves to Line when a date field is on
Columns, even a discrete date part.** Do not infer mark type from ``<mark class='Automatic'/>`` alone
when a thumbnail is available -- look.

Caveats (deliberately stated, because over-claiming here is the failure mode this file exists to fix)
-----------------------------------------------------------------------------------------------------
* Thumbnails are typically **192x192**. They are decisive for *shape*: mark type, layering, axis
  direction, presence of markers/labels, header text format, grand-total rows. They are NOT evidence
  for fonts, exact colours, spacing or pixel parity -- keep claiming those ``unverifiable-without-
  reference``.
* They reflect the state at the workbook's **last save**, not necessarily the current data.
* A worksheet never displayed before saving may be missing or blank; ``--strict`` fails on zero.
* Dashboards are not thumbnailed per se -- these are worksheet renders.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import pathlib
import re
import sys
import zipfile
from xml.etree import ElementTree as ET

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _safe(name: str) -> str:
    """Filesystem-safe stem preserving readability (worksheet names allow / \\ : * ? etc.)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "unnamed"


def read_twb_bytes(src: pathlib.Path) -> bytes:
    """Return the .twb XML bytes from either a .twb or a packaged .twbx."""
    if src.suffix.lower() == ".twbx":
        with zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".twb")]
            if not names:
                raise SystemExit(f"[FAIL] no .twb inside {src}")
            # A .twbx holds exactly one .twb at the archive root; prefer the shallowest.
            names.sort(key=lambda n: (n.count("/"), len(n)))
            return zf.read(names[0])
    return src.read_bytes()


def extract(src: pathlib.Path, out_dir: pathlib.Path) -> list[tuple[str, pathlib.Path, int]]:
    """Write every worksheet thumbnail in `src` to `out_dir`; return (name, path, bytes) per PNG."""
    root = ET.fromstring(read_twb_bytes(src))
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, pathlib.Path, int]] = []
    seen: dict[str, int] = {}
    for node in root.iter("thumbnail"):
        name = node.get("name") or "unnamed"
        payload = (node.text or "").strip()
        if not payload:
            print(f"[skip] {name}: empty thumbnail element", file=sys.stderr)
            continue
        try:
            raw = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as exc:
            print(f"[skip] {name}: undecodable base64 ({exc})", file=sys.stderr)
            continue
        if not raw.startswith(PNG_MAGIC):
            print(f"[skip] {name}: not a PNG (magic {raw[:8].hex()})", file=sys.stderr)
            continue
        stem = _safe(name)
        seen[stem] = seen.get(stem, 0) + 1
        if seen[stem] > 1:  # two worksheets can sanitise to the same stem
            stem = f"{stem}__{seen[stem]}"
        path = out_dir / f"{stem}.png"
        path.write_bytes(raw)
        written.append((name, path, len(raw)))
    return written


def main() -> int:
    """CLI entry point: extract worksheet thumbnails from a .twb/.twbx into a reference folder."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", type=pathlib.Path, help="path to a .twb or .twbx")
    ap.add_argument(
        "-o",
        "--out",
        type=pathlib.Path,
        required=True,
        help="output folder for the PNGs (e.g. migrations/workbooks/<slug>/reference)",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 if the workbook embeds no usable thumbnails",
    )
    args = ap.parse_args()

    if not args.workbook.exists():
        print(f"[FAIL] no such workbook: {args.workbook}", file=sys.stderr)
        return 2

    written = extract(args.workbook, args.out)
    for name, path, size in written:
        print(f"  {name:<30} -> {path.name:<34} {size:,} bytes")
    print(f"[OK] {len(written)} worksheet thumbnail(s) -> {args.out}")
    if not written:
        print(
            "[WARN] no thumbnails embedded. Worksheets never displayed before the last save are "
            "not thumbnailed; fall back to structural-only critique and say so.",
            file=sys.stderr,
        )
        if args.strict:
            return 1
    else:
        print(
            "[NOTE] ~192x192: decisive for mark type / layering / axis direction / labels, "
            "NOT for fonts, exact colours or pixel parity."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
