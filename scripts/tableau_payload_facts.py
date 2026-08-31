"""
purpose: describe what a Tableau REST payload actually IS -- row counts, formats, geometry, vector-ness
usage:   from tableau_payload_facts import summarise_csv, svg_facts, pdf_facts, png_dimensions

Pure functions over bytes, with no session, no HTTP and no filesystem. Split out of
``capture_tableau_oracle`` because they are a different concern from talking to Tableau -- and because
this is where #405 round 5's leak lived: ``summarise_csv`` copies a CSV's first row into
``data.columns``, which is response text reaching the manifest by a route nobody had classified as a
diagnostic. Keeping the parsers in one small module makes that surface enumerable; the taint gate in
``tests/test_diagnostic_redaction.py`` covers this file for exactly that reason.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

_PERCENT = re.compile(r"^-?[\d,.]+%$")
_CURRENCY = re.compile(r"^-?[$\u00a3\u20ac\u00a5]\s?[\d,.]+$")
_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_SVG_ROOT_MM = re.compile(r'width="([\d.]+)mm"\s+height="([\d.]+)mm"')
_SVG_HREF = re.compile(r'(?:xlink:)?href="([^"]{0,120})')
_PDF_MEDIABOX = re.compile(rb"/MediaBox\s*\[([^\]]*)\]")


def detect_format(values: list[str]) -> str | None:
    """Advisory hint: does this column arrive display-formatted rather than as a raw number?"""
    sample = [v for v in values if v][:50]
    if not sample:
        return None
    if all(_PERCENT.match(v) for v in sample):
        return "percent"
    if all(_CURRENCY.match(v) for v in sample):
        return "currency"
    if all(_THOUSANDS.match(v) for v in sample):
        return "thousands_separated"
    return None


def summarise_csv(payload: bytes) -> dict[str, Any]:
    """Row/column shape plus per-column format hints, so a capture can be proven non-empty."""
    text = payload.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {"row_count": 0, "columns": [], "format_hints": {}}
    header, body = rows[0], rows[1:]
    hints = {}
    for idx, name in enumerate(header):
        fmt = detect_format([r[idx] for r in body if idx < len(r)])
        if fmt:
            hints[name] = fmt
    return {"row_count": len(body), "columns": header, "format_hints": hints}


def png_dimensions(payload: bytes) -> dict[str, int] | None:
    """Width/height from the IHDR chunk. Recorded so the manifest states the reference's RESOLUTION.

    ``resolution=high`` is not an open-ended quality dial: measured over all 52 capturable dashboards
    on the trial site it returns **exactly 2x the dashboard's declared size**, with no exception and
    no parameter that raises it. A 650x800 dashboard therefore tops out at 1300x1600 forever. Writing
    the number down is what lets a consumer judge whether the reference can carry a content-level
    verdict, instead of inferring it from the fact that a PNG exists (issue #403).
    """
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        return None
    return {"width": int.from_bytes(payload[16:20], "big"), "height": int.from_bytes(payload[20:24], "big")}


def svg_facts(payload: bytes) -> dict[str, Any]:
    """Geometry and machine-readable-text census for a REST SVG export.

    Two properties make this the higher-fidelity reference, and both are asserted here rather than
    assumed. (1) The root carries the dashboard's size in millimetres at 96 dpi, so the vector can be
    rasterised at any scale with no geometry guess. Measured over all 52 capturable dashboards on the
    trial site, ``round(mm * 96 / 25.4)`` is the dashboard's declared pixel size **plus exactly 1 px
    in each axis** (a 1400x800 dashboard reports 370.681x211.931mm -> 1401x801) -- 52/52, no
    exception. ``round`` and not ``int``: the true value lands just under the integer (1400.99), so
    truncation is off by one for some dashboards and not others (measured: three different offsets
    across the same 52), which is exactly the kind of silent inconsistency that makes a geometry field
    untrustworthy.

    ``width_px``/``height_px`` are the SVG's **own viewport**, not a restated ``/image`` size. For a
    DASHBOARD that is ``/image?resolution=high`` / 2 + 1 per axis. Do not carry the +1 over to a
    worksheet: one measured worksheet (``BAN Hired``, PNG 1584x1584) reported 792x792, i.e. offset 0,
    and a single sample is not a law. Compare with the offset in mind rather than assuming equality.

    (2) Labels arrive as real ``<text>`` elements holding the literal strings ("7,984", "Active
    Employees"), so a consumer can read the dashboard's CONTENT without rendering anything at all.
    ``text_elements`` is also the cost signal: a crosstab-shaped worksheet measured 37,439 of them in
    a 21 MB SVG against a 4.5 MB PNG, so ``--svg`` is not free on text-dense views.

    ``external_refs`` is the self-containment check: Tableau inlines raster sub-elements (maps, logos)
    as ``data:`` URIs, so a non-zero count means the file needs the server to render and must not be
    treated as durable offline evidence.
    """
    text = payload.decode("utf-8", "replace")
    facts: dict[str, Any] = {
        "text_elements": text.count("<text"),
        "image_elements": text.count("<image"),
        "path_elements": text.count("<path"),
        "external_refs": len([h for h in _SVG_HREF.findall(text) if not h.startswith(("data:", "#"))]),
    }
    match = _SVG_ROOT_MM.search(text[:2000])
    if match:
        facts["width_px"] = round(float(match.group(1)) * 96 / 25.4)
        facts["height_px"] = round(float(match.group(2)) * 96 / 25.4)
    return facts


def pdf_facts(payload: bytes) -> dict[str, Any]:
    """Page geometry and vector-ness of a REST PDF export, stdlib only.

    ``/pdf`` reaches back to **API 2.8 / Tableau Server 10.5**, which is why it is the portable rung of
    the ladder -- but "it returned a PDF" is not the same as "it returned the page I asked for", and
    ``type=Unspecified`` is undocumented. Recording the ``MediaBox`` is what distinguishes the two: a
    server that ignored the value falls back to a paper size (measured default **612x792 = Letter
    portrait**, notwithstanding the docs' claim that the default is ``Legal`` = 612x1008).

    ``fontfile_count`` is the fidelity note: unlike the SVG, a Tableau PDF **embeds** its fonts, so it
    renders with the workbook's real typefaces on a machine that does not have them installed.
    """
    facts: dict[str, Any] = {
        "vector": True,
        "fontfile_count": len(re.findall(rb"/FontFile\d?", payload)),
        "image_xobjects": len(re.findall(rb"/Subtype\s*/Image", payload)),
    }
    boxes = sorted({m.decode().strip() for m in _PDF_MEDIABOX.findall(payload)})
    if boxes:
        parts = boxes[0].split()
        if len(parts) >= 4:
            facts["page_pt"] = {"width": round(float(parts[2])), "height": round(float(parts[3]))}
    return facts
