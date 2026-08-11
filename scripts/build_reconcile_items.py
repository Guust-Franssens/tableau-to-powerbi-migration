"""
purpose: turn a captured Tableau oracle (view CSVs) into reconcile-ready items keyed by grain,
         so the deterministic engine's translation_reconcile can compare Tableau's number against
         the migrated model's number.
usage:   python scripts/build_reconcile_items.py --oracle _oracle --out _oracle/items.json

Why this exists
---------------
The engine's ``translation_reconcile.reconcile_all`` accepts a batch of
``{name, dax, tableau_value, grain_filters}`` items and compares each Tableau value against a value
obtained by executing DAX. It has **two injection points and neither has ever been filled** -- the
Tableau side is supplied by the caller (its CLI reads a ``--ground-truth truth.json`` you provide),
and nothing in that toolchain ever fetches a Tableau number (measured: zero calls to the view-data
endpoint anywhere in its tree).

``capture_tableau_oracle.py`` produces the missing half, but in the wrong shape: a CSV per *view*,
one row per mark. This module is the mapper between them.

The correspondence, and why it is not a guess
---------------------------------------------
A captured row::

    Country/Region, State/Province, Profit Ratio
    Canada,         Alberta,        19.5%

becomes one item::

    {"name": "Profit Ratio",
     "grain_filters": {"Country/Region": "Canada", "State/Province": "Alberta"},
     "tableau_value": 0.195}

Splitting dimensions from measures is done from the Tableau **field role** (``DIMENSION`` /
``MEASURE``) read from the Metadata API, not from guessing at the data. A header with no matching
field is recorded as ``unmapped`` rather than assumed -- that is how Tableau's generated fields
(``Latitude (generated)``) are dropped: they are genuinely absent from the datasource's field list.

⚠️ **The residual risk this CANNOT solve.** A view-level filter leaves *no trace in the exported
rows* -- if the view excludes a region, the CSV simply has fewer rows and nothing says why. So a
grain-matched comparison is only valid if the Power BI side is evaluated under the same view filters.
Items therefore carry ``filter_context_known: false`` and the caller must not treat a match as proof
of parity until the view's filters are supplied. Silence about filters is the single most likely
source of confident garbage here.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

# Windows defaults stdout/stderr to the legacy cp1252 codec, which cannot encode the non-ASCII
# characters (e.g. the warning glyph above) in this module's own docstring -- argparse's --help
# crashes with UnicodeEncodeError before printing anything. Force UTF-8 so --help and any print()
# of the same characters work the same on every platform.
for _stream in (sys.stdout, sys.stderr):
    # pylint: disable-next=no-member  # astroid mis-infers TextIOWrapper.encoding as a class here
    if _stream is not None and _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

LOG = logging.getLogger("reconcile-items")

_PERCENT = re.compile(r"^\s*(-?[\d,]*\.?\d+)\s*%\s*$")
_CURRENCY = re.compile(r"^\s*(-?)\s*[$£€¥]\s*([\d,]*\.?\d+)\s*$")
_PLAIN = re.compile(r"^\s*-?[\d,]*\.?\d+\s*$")


def normalise_value(text: str) -> tuple[float | str | None, str]:
    """Convert a display-formatted Tableau cell to a comparable value.

    ``/views/{id}/data`` returns what the view *shows*, not raw numbers -- ``"19.5%"`` rather than
    ``0.195``, ``"$12"`` rather than ``12``. Comparing those as strings against a DAX result would
    fail on formatting alone; coercing them silently would hide a real type mismatch. So the applied
    conversion is returned alongside the value and recorded per item.
    """
    if text is None:
        return None, "none"
    raw = text.strip()
    if raw == "":
        return None, "blank"
    match = _PERCENT.match(raw)
    if match:
        return float(match.group(1).replace(",", "")) / 100.0, "percent"
    match = _CURRENCY.match(raw)
    if match:
        sign = -1.0 if match.group(1) == "-" else 1.0
        return sign * float(match.group(2).replace(",", "")), "currency"
    if _PLAIN.match(raw):
        return float(raw.replace(",", "")), "number"
    return raw, "text"


def classify_columns(headers: list[str], roles: dict[str, str]) -> dict[str, list[str]]:
    """Split headers into grain / value / unmapped using Tableau's own field roles.

    Never infers a role from the data. A header absent from ``roles`` is ``unmapped``, which is both
    how generated fields are excluded and how an aliased column header announces itself instead of
    being silently mis-bound.
    """
    grain: list[str] = []
    value: list[str] = []
    unmapped: list[str] = []
    for header in headers:
        role = roles.get(header) or roles.get(header.strip())
        if role == "DIMENSION":
            grain.append(header)
        elif role == "MEASURE":
            value.append(header)
        else:
            unmapped.append(header)
    return {"grain": grain, "value": value, "unmapped": unmapped}


def items_for_view(rows: list[dict[str, str]], columns: dict[str, list[str]], view: dict[str, Any]) -> list[dict]:
    """One item per (row, measure column). Non-numeric measure cells are skipped, loudly."""
    items = []
    for index, row in enumerate(rows):
        grain_filters = {col: row.get(col, "") for col in columns["grain"]}
        for col in columns["value"]:
            value, applied = normalise_value(row.get(col, ""))
            if not isinstance(value, float):
                continue
            items.append(
                {
                    "name": col,
                    "tableau_value": value,
                    "grain_filters": grain_filters,
                    "source": {
                        "view_luid": view.get("view_luid"),
                        "view_name": view.get("view_name"),
                        "workbook_name": view.get("workbook_name"),
                        "row": index,
                        "raw": row.get(col, ""),
                        "normalisation": applied,
                    },
                    # The exported rows carry no record of the view's own filters, so a grain match
                    # is necessary but not sufficient. The consumer must supply the filter context.
                    "filter_context_known": False,
                }
            )
    return items


def load_roles(path: Path | None) -> dict[str, dict[str, str]]:
    """Field roles per workbook: ``{workbook_name: {field_name: DIMENSION|MEASURE}}``."""
    if not path or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build(oracle_dir: Path, roles_by_workbook: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Map every captured view into reconcile-ready items."""
    manifest = json.loads((oracle_dir / "oracle-manifest.json").read_text(encoding="utf-8"))
    items: list[dict] = []
    unmapped: list[dict] = []
    skipped: list[dict] = []

    for view in manifest.get("views", []):
        data = view.get("data") or {}
        if data.get("status") != "ok" or not data.get("path"):
            skipped.append({"view": view.get("view_name"), "reason": data.get("status", "missing")})
            continue
        roles = roles_by_workbook.get(view.get("workbook_name") or "", {})
        if not roles:
            skipped.append({"view": view.get("view_name"), "reason": "no field roles for workbook"})
            continue
        text = (oracle_dir / data["path"]).read_text(encoding="utf-8-sig")
        rows = list(csv.DictReader(text.splitlines()))
        if not rows:
            skipped.append({"view": view.get("view_name"), "reason": "empty csv"})
            continue
        columns = classify_columns(list(rows[0].keys()), roles)
        if not columns["value"]:
            skipped.append({"view": view.get("view_name"), "reason": "no measure column resolved"})
        for col in columns["unmapped"]:
            unmapped.append({"view": view.get("view_name"), "column": col})
        items.extend(items_for_view(rows, columns, view))

    return {
        "schema": "tableau-reconcile-items/1",
        "source_manifest": str((oracle_dir / "oracle-manifest.json").as_posix()),
        "captured_at": manifest.get("captured_at"),
        "site": manifest.get("site"),
        "item_count": len(items),
        "unmapped_columns": unmapped,
        "skipped_views": skipped,
        "items": items,
    }


def main() -> int:
    """Build reconcile items. Exit 1 if nothing could be mapped -- an empty batch is not a success."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--oracle", required=True, type=Path, help="directory written by capture_tableau_oracle.py")
    parser.add_argument("--roles", type=Path, help="JSON {workbook: {field: DIMENSION|MEASURE}} from Tableau metadata")
    parser.add_argument("--out", required=True, type=Path, help="output items JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = build(args.oracle, load_roles(args.roles))
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    LOG.info(
        "%d item(s) from %d view(s) skipped=%d unmapped_columns=%d -> %s",
        result["item_count"],
        len(result["skipped_views"]),
        len(result["skipped_views"]),
        len(result["unmapped_columns"]),
        args.out,
    )
    if result["unmapped_columns"]:
        LOG.warning("unmapped columns (not guessed at - supply roles or check aliases):")
        for entry in result["unmapped_columns"][:10]:
            LOG.warning("  %s / %s", entry["view"], entry["column"])
    return 0 if result["item_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
