"""
purpose: describe what a Tableau REST payload actually IS -- row counts, formats, geometry, vector-ness
usage:   from tableau_payload_facts import summarise_csv, svg_facts, pdf_facts, png_dimensions

Pure functions over bytes, with no session, no HTTP and no filesystem. Split out of
``capture_tableau_oracle`` because they are a different concern from talking to Tableau -- and because
this is where #405 round 5's leak lived: ``summarise_csv`` copies a CSV's first row into
``data.columns``, which is response text reaching the manifest by a route nobody had classified as a
diagnostic. Keeping the parsers in one small module makes that surface enumerable; the taint gate in
``tests/test_diagnostic_redaction.py`` covers this file for exactly that reason.

⚠️ **Describing a payload is not the same as certifying it, and :func:`payload_is_complete` is the
only function here that certifies.** Everything else is best-effort: ``png_dimensions`` reads an IHDR
that may be the only chunk present, ``svg_facts`` counts ``<text`` in a string that need not be
well-formed, ``pdf_facts`` regex-scans for a ``/MediaBox`` that may never be closed. That is right for
a *description* and fatal for a *verdict*, which is exactly how a truncated render came to be recorded
as ``status: ok`` with a SHA-256 beside it.
"""

from __future__ import annotations

import csv
import io
import re
import zlib
from typing import Any
from xml.etree import ElementTree

_PERCENT = re.compile(r"^-?[\d,.]+%$")
_CURRENCY = re.compile(r"^-?[$\u00a3\u20ac\u00a5]\s?[\d,.]+$")
_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_SVG_ROOT_MM = re.compile(r'width="([\d.]+)mm"\s+height="([\d.]+)mm"')
_SVG_HREF = re.compile(r'(?:xlink:)?href="([^"]{0,120})')
_PDF_MEDIABOX = re.compile(rb"/MediaBox\s*\[([^\]]*)\]")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# A DTD may only appear before the root element, so the entity scan stops at `<svg` rather than
# sweeping a 21 MB drawing whose own <text> could legitimately contain the literal string.
_PROLOG_SCAN_BYTES = 65536
_ENTITY_DECLARATION = re.compile(rb"<!ENTITY", re.I)
# `%%EOF` is the last line of a well-formed PDF; the slack absorbs a producer's trailing whitespace.
_PDF_TAIL_BYTES = 2048


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


def _png_chunk_census(payload: bytes) -> tuple[list[bytes], bool, bool]:
    """Walk the PNG chunk stream: ``(chunk types, every CRC matched, ended exactly at the last chunk)``.

    Each chunk is ``length(4) type(4) data(length) crc(4)``, and the CRC covers ``type + data``. A
    declared length larger than what arrived is the truncation signature, and it is checked BEFORE any
    slice is taken -- a hostile 4 GiB length must cost a comparison, never an allocation.
    """
    offset, types, crcs_ok = 8, [], True
    while offset + 8 <= len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        end = offset + 12 + length
        if end > len(payload):
            return types, crcs_ok, False
        chunk = payload[offset + 4 : end - 4]
        crcs_ok = crcs_ok and int.from_bytes(payload[end - 4 : end], "big") == zlib.crc32(chunk) & 0xFFFFFFFF
        types.append(chunk[:4])
        offset = end
    return types, crcs_ok, offset == len(payload)


def _png_is_complete(payload: bytes) -> tuple[bool, str]:  # pylint: disable=too-many-return-statements
    """A PNG is whole when its chunks run IHDR -> ... -> IDAT -> ... -> IEND with every CRC intact.

    One guard clause per failure mode, deliberately: each returns a DIFFERENT reason, and the reason
    is what an operator acts on -- "no IEND" means the download stopped, "CRC mismatch" means the
    bytes were rewritten in flight, and folding them into one "invalid PNG" would lose the distinction
    that decides whether to re-capture or to look at the network.
    """
    if not payload.startswith(PNG_SIGNATURE):
        return False, "the PNG signature is missing"
    types, crcs_ok, ended_cleanly = _png_chunk_census(payload)
    if not ended_cleanly:
        return False, f"the PNG chunk stream ends mid-chunk after {len(types)} complete chunk(s)"
    if not types or types[0] != b"IHDR":
        return False, "the PNG carries no IHDR header chunk"
    if b"IDAT" not in types:
        return False, f"the PNG carries no IDAT image data across its {len(types)} chunk(s)"
    if types[-1] != b"IEND":
        return False, f"the PNG has no IEND terminator after {len(types)} chunk(s), so it was cut short"
    if not crcs_ok:
        return False, "a PNG chunk CRC does not match its own data"
    return True, ""


def _svg_is_complete(payload: bytes) -> tuple[bool, str]:
    """An SVG is whole when the WHOLE document parses and its root really is ``<svg>``.

    ⚠️ A full parse, not a prefix match, and that is the entire point: a truncated SVG still begins
    with a perfectly good ``<svg ...>`` and only expat can tell you the document never ends.

    ⚠️ Entity declarations are REFUSED rather than expanded. ``xml.etree.ElementTree`` is explicitly
    not hardened against maliciously constructed data, and a nested-entity payload ("billion laughs")
    is a memory-exhaustion primitive against any expat parse. A Tableau REST export carries no DTD, so
    refusing one costs nothing real and removes the class without a new dependency. The scan stops at
    the root element because a DTD may only appear before it -- sweeping the whole document would
    misfire on a drawing whose own ``<text>`` happens to contain the literal string.

    ⚠️ **``LookupError`` is caught because it is NOT a ``ParseError``, and it is the one message that
    echoes the document.** Measured on CPython 3.13 -- and this corrects the assumption the first
    version of this function was written on. Across ten malformed shapes (undefined entity, mismatched
    tag, unbound prefix, junk after the root, duplicate attribute, unclosed token, invalid token, an
    encoding mismatch and a mis-declared UTF-16 document) every ``ParseError`` message is fixed
    vocabulary plus a line/column: **none quotes document text**, including the undefined-entity case
    the guard was originally aimed at. But a document declaring
    ``<?xml version="1.0" encoding="<anything>"?>`` raises ``LookupError: unknown encoding:
    <anything>`` -- attacker-chosen text, verbatim -- and that is a ``LookupError``, so a bare
    ``except ElementTree.ParseError`` lets it out of this module entirely, into a traceback carrying
    the document's own bytes. Neither message is forwarded either way; the numeric code and position
    say where it broke without repeating a byte of it.
    """
    root_at = payload.find(b"<svg")
    prolog = payload[: root_at if root_at >= 0 else _PROLOG_SCAN_BYTES]
    if _ENTITY_DECLARATION.search(prolog):
        return False, "the SVG declares XML entities, which this parser refuses to expand"
    try:
        root = ElementTree.fromstring(payload)  # noqa: S314  # entity declarations refused above
    except ElementTree.ParseError as exc:
        line, column = exc.position
        return False, f"the SVG is not well-formed XML (expat error {exc.code} at line {line}, column {column})"
    except LookupError:
        return False, "the SVG declares an encoding this parser does not know"
    if root.tag.rsplit("}", 1)[-1] != "svg":
        return False, "the SVG document parses but its root element is not <svg>"
    return True, ""


def _pdf_is_complete(payload: bytes) -> tuple[bool, str]:
    """A PDF is whole when the ``%%EOF`` trailer is present in its final bytes.

    Truncation removes the trailer, which is what makes it the check that matters here. This does not
    claim the cross-reference table resolves -- that would need a real PDF parser -- so it is stated as
    the bound it is: it catches a cut-short download, not a semantically broken document.
    """
    if not payload.startswith(b"%PDF-"):
        return False, "the PDF header is missing"
    if b"%%EOF" not in payload[-_PDF_TAIL_BYTES:]:
        return False, f"the PDF has no %%EOF trailer in its final {_PDF_TAIL_BYTES} byte(s), so it was cut short"
    return True, ""


_COMPLETENESS_CHECKS = {"png": _png_is_complete, "svg": _svg_is_complete, "pdf": _pdf_is_complete}


def payload_is_complete(kind: str, payload: bytes) -> tuple[bool, str]:
    """Is this payload structurally WHOLE? Returns ``(ok, why_not)``.

    ⚠️ **A leading signature is not evidence of a complete file, and treating it as one was a
    fail-open.** Measured against a loopback server answering ``200``, ``Content-Type: image/png``,
    ``Content-Length: 1024`` and then sending only the 8-byte PNG signature before closing: the 8
    bytes passed the signature check, were written to disk, and were recorded ``status: ok`` with a
    SHA-256 beside them and ``render_unestablished == 0``. A capture gap that reports itself as
    evidence is strictly worse than one that reports itself as a gap -- the fidelity bug it hides is
    then not merely unverified but believed verified.

    ⚠️ ``why_not`` is FIXED VOCABULARY plus integers, and must stay that way: it is persisted into
    ``oracle-manifest.json``, and every other diagnostic on this path had to be routed through
    :func:`tableau_env.redacted_note` because it quoted response bytes. This one quotes none, which is
    why it needs no redactor -- a property the taint gate checks rather than takes on trust.

    An unknown ``kind`` returns ``True``: this function refuses what it can PROVE is broken and never
    invents a verdict about a format it cannot read. The CSV data leg is deliberately absent for the
    same reason -- a CSV has no terminator, so truncation there is caught by the transport's
    ``Content-Length`` check in :func:`tableau_http._read_bounded`, not by parsing.
    """
    check = _COMPLETENESS_CHECKS.get(kind)
    if check is None:
        return True, ""
    return check(payload)
