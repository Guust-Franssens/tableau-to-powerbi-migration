"""
purpose: describe what a Tableau REST payload actually IS -- row counts, formats, geometry, vector-ness
usage:   from tableau_payload_facts import summarise_csv, svg_facts, pdf_facts, png_dimensions

Pure functions over bytes, with no session, no HTTP and no filesystem. Split out of
``capture_tableau_oracle`` because they are a different concern from talking to Tableau -- and because
this is where #405 round 5's leak lived: ``summarise_csv`` copies a CSV's first row into
``data.columns``, which is response text reaching the manifest by a route nobody had classified as a
diagnostic. Keeping the parsers in one small module makes that surface enumerable; the taint gate in
``tests/test_diagnostic_redaction.py`` covers this file for exactly that reason.

⚠️ **Describing a payload is not the same as certifying it, and :func:`payload_is_complete` and
:func:`certify_csv` are the only functions here that certify.** Everything else is best-effort:
``png_dimensions`` reads an IHDR that may be the only chunk present, ``svg_facts`` counts ``<text`` in
a string that need not be well-formed, ``pdf_facts`` regex-scans for a ``/MediaBox`` that may never be
closed -- and ``summarise_csv`` will happily report a row count for an HTML error page. That is right
for a *description* and fatal for a *verdict*, which is exactly how a truncated render came to be
recorded as ``status: ok`` with a SHA-256 beside it, and how a 200 ``text/html`` body came to be
recorded as two rows of data.
"""

from __future__ import annotations

import csv
import io
import re
import zlib
from typing import Any
from xml.parsers import expat

_PERCENT = re.compile(r"^-?[\d,.]+%$")
_CURRENCY = re.compile(r"^-?[$\u00a3\u20ac\u00a5]\s?[\d,.]+$")
_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_SVG_ROOT_MM = re.compile(r'width="([\d.]+)mm"\s+height="([\d.]+)mm"')
_SVG_HREF = re.compile(r'(?:xlink:)?href="([^"]{0,120})')
_PDF_MEDIABOX = re.compile(rb"/MediaBox\s*\[([^\]]*)\]")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The object header of a cross-reference STREAM, which is what `startxref` points at in a PDF 1.5+
# file that has no `xref` table. Matched at the offset the trailer names, never searched for.
_PDF_XREF_STREAM = re.compile(rb"\d+\s+\d+\s+obj")
# How much is read at that offset to identify it. Enough for `<n> <g> obj`, and bounded so a hostile
# offset cannot make this slice a large buffer.
_PDF_XREF_PEEK = 64


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


# What a `/data` export may DECLARE and still be read as CSV. `text/plain` and `application/csv` are
# here because a transcoding proxy relabels an export and some servers spell it the second way;
# anything else is the server positively stating the body is NOT CSV, which is decisive.
CSV_MIME_TYPES = frozenset({"text/csv", "application/csv", "text/plain"})

# The verdicts :func:`certify_csv` may return. A CLOSED vocabulary this repo authors: nothing here is
# built from the payload or from the received header, so a verdict can be logged, recorded and
# branched on without any of it being response text.
CSV_CERTIFIED = "certified"
#: No `Content-Type` at all. The structure passed every check, but CSV has no signature -- there is no
#: byte sequence that PROVES a body is CSV the way `%PDF-` proves a PDF -- so the declaration is the
#: only certificate available and its absence leaves the payload UNASSESSABLE, never clean.
CSV_CONTENT_TYPE_ABSENT = "content_type_absent"
CSV_CONTENT_TYPE_NOT_CSV = "content_type_not_csv"
CSV_NOT_TABULAR = "payload_not_tabular"
CSV_MALFORMED = "payload_malformed_csv"
CSV_RAGGED = "payload_ragged_rows"

#: A verdict that REFUSES the payload outright: these bytes were never established to be a CSV, so
#: nothing may be derived from them -- not a row count, not a header, and above all not a diagnosis.
CSV_REFUSALS = frozenset({CSV_CONTENT_TYPE_NOT_CSV, CSV_NOT_TABULAR, CSV_MALFORMED, CSV_RAGGED})
#: Every value `certify_csv` can produce, so a consumer reading one off an older manifest can check
#: it against a closed set instead of trusting whatever string is in the field.
CSV_VERDICTS = frozenset({CSV_CERTIFIED, CSV_CONTENT_TYPE_ABSENT}) | CSV_REFUSALS

#: Why a `/data` body was refused, keyed by :func:`certify_csv`'s verdict. Sentences this repo
#: authors, so a refusal can be recorded and printed without quoting a single byte the server sent --
#: which is the same property that makes the verdict itself safe to log. Beside the vocabulary rather
#: than beside the caller, because a new verdict and its explanation must be added together.
CSV_REFUSAL_DETAIL = {
    CSV_CONTENT_TYPE_NOT_CSV: (
        "the export returned HTTP 200 but declared a Content-Type that is not CSV, so the body is "
        "not data. Nothing was recorded from it: a row count read off a non-CSV payload is fiction "
        "with a number attached, and a classification read off its first line is confidently wrong."
    ),
    CSV_NOT_TABULAR: (
        "the export returned HTTP 200 whose body opens a tag or a JSON object rather than a table -- "
        "an error page or an API envelope, not data. No row count or header was taken from it."
    ),
    CSV_MALFORMED: (
        "the export returned HTTP 200 whose body does not parse as CSV -- an unterminated quoted "
        "field or a strict-parse error, both of which a truncated export produces. A non-strict "
        "reader would have reported the surviving prefix as complete rows."
    ),
    CSV_RAGGED: (
        "the export returned HTTP 200 whose rows do not all carry the header's field count, which a "
        "complete Tableau CSV export does. The body was not certified, so no row count was recorded."
    ),
}

# A body whose first non-space byte opens a tag or a JSON object is a document, not a table. Checked
# because Content-Type is the only positive CSV certificate and a proxy can strip it: `<html>` then
# parses as a one-column CSV with two "rows", which is how an error page came to be recorded as data.
# `[` is deliberately NOT here -- a Tableau field name can legitimately be bracketed.
_NOT_TABULAR_OPENERS = ("<", "{")


def certify_csv(payload: bytes, content_type: str | None) -> str:
    """May these bytes be read as CSV rows at all? One value from :data:`CSV_VERDICTS`.

    ⚠️ **This is the only function here that CERTIFIES a `/data` payload, and it exists because
    describing one is not the same as establishing it.** ``summarise_csv`` decodes with ``replace``
    and reads a non-strict ``csv.reader``, so *any* first line becomes a "header" and *any* further
    line becomes a "row". Measured on this branch before the check existed: an HTTP 200 ``text/html``
    error page (``<html>/<body>Error</body>/</html>``) was recorded ``status: ok`` with
    ``columns: ["<html>"]`` and **``row_count: 2``**; and a 200 ``application/octet-stream`` body
    reading ``not CSV at all`` was recorded ``row_count: 0`` and then classified
    ``empty_query_no_rows`` -- a *specific diagnosis* ("the query ran and returned nothing") about a
    payload never shown to be CSV at all. Confidently wrong is worse than unknown.

    The order is deliberate. The DECLARATION is decisive first, unlike
    :func:`tableau_render_capability.format_matches` where the payload is: a PNG or a PDF carries a
    signature that settles the question whatever the header says, and **a CSV carries nothing**. So a
    server saying ``text/html`` is the strongest evidence available and is believed; a server saying
    nothing leaves the body uncertifiable however well-formed it looks, which is
    :data:`CSV_CONTENT_TYPE_ABSENT` -- reported as unassessable rather than waved through.

    Never echoes the payload or the received header. The return value is one of this module's own
    literals, which is what lets a caller put it in a manifest and a log line without redaction.
    """
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared and declared not in CSV_MIME_TYPES:
        return CSV_CONTENT_TYPE_NOT_CSV
    text = payload.decode("utf-8-sig", "replace")
    if text.lstrip()[:1] in _NOT_TABULAR_OPENERS:
        return CSV_NOT_TABULAR
    # An odd number of quotes means one was opened and never closed -- a truncated export, or a body
    # that is not CSV at all. `csv.reader` does NOT raise for it even under `strict=True`: it reads to
    # EOF and hands back a field, which is how `Region,Sales\r\nWest,"unterminated` was recorded as
    # one complete row. CSV escapes a literal quote by doubling it, so a well-formed export always
    # carries an even count.
    if text.count('"') % 2:
        return CSV_MALFORMED
    try:
        rows = [row for row in csv.reader(io.StringIO(text), strict=True) if row]
    except csv.Error:
        return CSV_MALFORMED
    if rows and any(len(row) != len(rows[0]) for row in rows[1:]):
        return CSV_RAGGED
    return CSV_CERTIFIED if declared else CSV_CONTENT_TYPE_ABSENT


def summarise_csv(payload: bytes) -> dict[str, Any]:
    """Row/column shape plus per-column format hints, so a capture can be proven non-empty.

    ⚠️ **Describes; does not certify.** Call :func:`certify_csv` first and do not call this at all on
    a payload it refused -- every field below is derived from whatever bytes arrived, and on a
    non-CSV body they are fiction with a number attached.
    """
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


class _EntityDeclarationRefused(Exception):
    """Raised out of an expat handler when the document declares an entity. Never escapes this module.

    ⚠️ Refusing at the DECLARATION, from inside the parser, is the whole point. The previous guard
    scanned the bytes before the first ``<svg`` for ``<!ENTITY`` -- and an attacker moves the boundary
    simply by writing ``<svg`` inside a comment or a processing instruction ahead of the DTD.
    Measured: with ``<!-- harmless <svg decoy> -->`` first, the real ``<!ENTITY`` block sat 45 bytes
    PAST the boundary, expat expanded the entity to 1000 characters, and the artifact was recorded
    ``status: ok`` with a digest and a path. The parser knows where the DTD is; a substring search
    cannot.
    """


def _svg_is_complete(payload: bytes) -> tuple[bool, str]:
    """An SVG is whole when the WHOLE document parses, declares no entities, and its root is ``<svg>``.

    ⚠️ **Parsed, never scanned.** A truncated SVG still opens with a flawless ``<svg ...>``, and a
    document that hides its DTD behind a decoy still expands its entities -- neither is visible to a
    prefix match or a window scan. Expat is asked to consume the buffer to its last byte, which is the
    only thing that can say the document ENDS where it claims to.

    Three refusals, each structural rather than lexical:

    * **entity declarations** -- :class:`_EntityDeclarationRefused` is raised from ``EntityDeclHandler``,
      so a declaration is refused wherever it sits and nothing is ever expanded. A Tableau REST export
      carries no DTD, so this costs nothing real and removes the resource-exhaustion class outright.
    * **external entity references** -- refused for the same reason. Expat does not fetch external
      parameter entities by default (``XML_PARAM_ENTITY_PARSING_NEVER``), so this is belt and braces
      rather than the only guard, and it means no parse of ours can reach the network.
    * **anything not well-formed to the end** -- truncation, junk after the root element, an undefined
      entity. All arrive as ``ExpatError``.

    ⚠️ **The encoding exceptions are a SEPARATE family and expat does not unify them** -- an assumption
    worth measuring rather than believing, because the first draft of this fix assumed it did. Measured
    on CPython 3.13: an unrecognised ``encoding=`` raises ``LookupError`` **carrying the declared name
    verbatim**, and ``utf-32``/``utf-7``/``shift_jis``/``big5``/``gb18030`` raise ``ValueError:
    multi-byte encodings are not supported``. Neither is an ``ExpatError``, so both previously escaped
    this function and crashed the capture instead of becoming a non-``ok`` leg. Caught here, and
    neither message is forwarded.

    ⚠️ Namespace processing is deliberately OFF. ``ElementTree.fromstring`` is namespace-aware and
    rejects an undeclared prefix, so a drawing using ``xlink:href`` without declaring ``xmlns:xlink``
    would have been refused as broken. Non-namespace parsing accepts it -- measured -- which makes this
    strictly more permissive than what it replaces on well-formed documents, while being strictly
    stricter on the two bypasses. The root's prefix is stripped for the ``svg`` comparison.
    """
    root: list[str] = []

    # ⚠️ Every handler names its parameters in full rather than taking `*args`: the taint gate in
    # `tests/test_diagnostic_redaction.py` refuses star-args in a guarded module, because the analyser
    # cannot follow them and would go silently blind on this function. The signatures are expat's, so
    # the unused parameters and the arity are dictated rather than chosen -- hence the disables.
    def start_element(name, attributes):  # pylint: disable=unused-argument
        if not root:
            root.append(name)

    def entity_declaration(  # pylint: disable=unused-argument,too-many-arguments,too-many-positional-arguments
        entity_name,
        is_parameter_entity,
        value,
        base,
        system_id,
        public_id,
        notation_name,
    ):
        raise _EntityDeclarationRefused

    def external_entity_reference(context, base, system_id, public_id):  # pylint: disable=unused-argument
        raise _EntityDeclarationRefused

    parser = expat.ParserCreate()
    parser.StartElementHandler = start_element
    parser.EntityDeclHandler = entity_declaration
    parser.ExternalEntityRefHandler = external_entity_reference
    try:
        parser.Parse(payload, True)
    except _EntityDeclarationRefused:
        return False, "the SVG declares XML entities, which this parser refuses to expand"
    except expat.ExpatError as exc:
        # ⚠️ `str(exc)` is NOT quoted. Measured across ten malformed shapes, no `ExpatError` message
        # echoes document text -- but that is a property of today's expat, not a guarantee, and the
        # numeric code and position say where it broke without repeating a byte of it either way.
        return False, f"the SVG is not well-formed XML (expat error {exc.code} at line {exc.lineno})"
    except (LookupError, ValueError):
        # The encoding family. `LookupError`'s message quotes the declared encoding verbatim, so it is
        # replaced rather than forwarded; `ValueError` covers the multibyte codecs expat cannot stream.
        return False, "the SVG declares an encoding this parser cannot decode"
    if not root:
        return False, "the SVG document contains no element at all"
    if root[0].rpartition(":")[2] != "svg":
        return False, "the SVG document parses but its root element is not <svg>"
    return True, ""


def _pdf_is_complete(payload: bytes) -> tuple[bool, str]:  # pylint: disable=too-many-return-statements
    """A PDF is whole when its FINAL revision's trailer is intact and terminates the file.

    ⚠️ **The final revision, not tail membership.** The previous check accepted any ``%%EOF`` within
    the last 2 KiB -- and a PDF carries one ``%%EOF`` per incremental revision, so a download cut
    during a short later revision still held an earlier marker. Measured on a two-revision file cut
    before the newest ``startxref``: the surviving ``%%EOF`` sat **105 bytes from the end**, this
    function returned ``(True, "")``, and an independent parser read the file as the PRIOR revision --
    ``/Title (ORIGINAL)`` where the complete file says ``LATEST-REVISION``. So the bytes were credited
    as evidence while describing a document the customer no longer has.

    What is checked instead follows the trailer the file itself names:

    1. ``%%EOF`` is the last non-whitespace token -- trailing whitespace is legal, trailing anything
       else means the file did not end here;
    2. the ``startxref`` immediately before it carries a numeric byte offset;
    3. that offset lies inside the file and points at a cross-reference -- the ``xref`` keyword, or an
       object header for a PDF 1.5+ cross-reference stream.

    Together those say the last thing in the file is a complete pointer to a cross-reference that
    exists. It is deliberately NOT a claim that the whole reference graph resolves; that needs a real
    PDF parser, and the failure mode here is a cut-short download, which this does catch.
    """
    if not payload.startswith(b"%PDF-"):
        return False, "the PDF header is missing"
    tail = payload.rstrip()
    if not tail.endswith(b"%%EOF"):
        return False, "the PDF does not END with %%EOF, so it was cut short"
    before_eof = tail[: -len(b"%%EOF")]
    marker = before_eof.rfind(b"startxref")
    if marker < 0:
        return False, "the PDF's final revision has no startxref, so its trailer is incomplete"
    offset_token = before_eof[marker + len(b"startxref") :].strip()
    if not offset_token.isdigit():
        return False, "the PDF's final startxref does not carry a byte offset"
    offset = int(offset_token)
    if not 0 < offset < len(payload):
        return False, f"the PDF's final startxref points outside its own {len(payload)} bytes"
    target = payload[offset : offset + _PDF_XREF_PEEK].lstrip()
    if not (target.startswith(b"xref") or _PDF_XREF_STREAM.match(target)):
        return False, "the PDF's final startxref does not point at a cross-reference"
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

    ⚠️ **THE RULE, because getting it wrong cost three review rounds in three different formats:
    PARSE the payload and prove it ENDS where it claims to. Never scan for a marker.** A substring or
    tail search is satisfied by bytes ANYWHERE in the buffer, so it structurally cannot answer the
    only question being asked. Every instance of the class was the same shape:

    ==========================  ==================================================================
    the scan                    what it could not tell apart
    ==========================  ==================================================================
    PNG signature prefix        an 8-byte truncation from a whole image
    ``<!ENTITY`` before         a DTD from a DTD hidden behind ``<!-- <svg decoy> -->`` or a
    the first ``<svg``          processing instruction -- the boundary is attacker-movable
    ``%%EOF`` in the last       the FINAL revision's trailer from an EARLIER revision's, so a
    2 KiB                       download cut in a later revision looked complete
    ==========================  ==================================================================

    So each format is settled by something that consumes the bytes: PNG by a chunk walk with CRCs that
    must land exactly on the end, SVG by an expat parse to the last byte with entity declarations
    refused, PDF by following the trailer the file itself names. The cross-format consequence is one
    property, and it is worth stating because it is testable in one line per format: **appending any
    non-whitespace byte to a complete payload must make it incomplete.** A marker scan passes that
    for small suffixes; a parser cannot.

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
