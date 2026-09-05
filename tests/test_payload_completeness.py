"""Structural completeness: a payload that STARTS right is not a payload that IS right (#423).

⚠️ The fail-open this file exists for. ``_capture_render`` credited any render whose first bytes
matched a magic number, so a peer that answered ``200``, ``Content-Type: image/png``,
``Content-Length: 1024`` and then sent only the 8-byte PNG signature produced::

    image.status == "ok", sha256 recorded, path written, render_unestablished == 0

A capture gap that reports itself as evidence is strictly worse than one that reports itself as a
gap: the fidelity defect it hides stops being merely unverified and becomes *believed verified*.

⚠️ **Two fixtures in this repository asserted that the broken behaviour was correct**, so a fix
validated against either would have been unfalsifiable -- and one of them carried the docstring "a
minimal but genuinely valid PNG" over signature + IHDR + CRC with no IDAT and no IEND. Both now come
from ``tests/png_fixtures.py``; ``test_the_shared_png_fixture_is_genuinely_valid`` checks that
builder against properties ``payload_is_complete`` never looks at, so the fixture is not certified by
the code it is used to test.

Every check here is paired: a positive control that must PASS and a mutation of it that must FAIL. A
completeness checker that accepts everything passes any test written only in the negative direction.
"""

from __future__ import annotations

import io
import sys
import zlib
from pathlib import Path

import pypdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from png_fixtures import valid_png  # noqa: E402  # pylint: disable=wrong-import-position
from pdf_fixtures import incremental_pdf, valid_pdf  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_payload_facts import payload_is_complete, png_dimensions  # noqa: E402  # pylint: disable=C0413
from tableau_render_capability import looks_like_svg  # noqa: E402  # pylint: disable=wrong-import-position

VALID_SVG = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
    b'<svg width="370.681mm" height="211.931mm" xmlns="http://www.w3.org/2000/svg" version="1.1">'
    b'<text x="10" y="20">Active Employees</text></svg>'
)
VALID_PDF = valid_pdf()


def test_the_shared_png_fixture_is_genuinely_valid():
    """⚠️ The fixture's INDEPENDENT oracle -- deliberately not `payload_is_complete`.

    Certifying the fixture with the code under test would be circular, and circularity is precisely
    how the previous fixtures came to pin a defect. These are properties the completeness checker
    never inspects: the IDAT really inflates, and it inflates to exactly the raw scanline length the
    IHDR implies (one filter byte plus three bytes per pixel, per row). A builder that emitted
    plausible-looking chunk framing over junk would satisfy the checker and fail this.
    """
    width, height = 8, 6
    payload = valid_png(width, height)
    offset, idat = 8, b""
    while offset + 8 <= len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        if payload[offset + 4 : offset + 8] == b"IDAT":
            idat = payload[offset + 8 : offset + 8 + length]
        offset += 12 + length

    assert idat, "the fixture has no IDAT chunk at all"
    assert len(zlib.decompress(idat)) == height * (1 + width * 3), (
        "the fixture's image data does not inflate to the size its own IHDR implies, so it is not a "
        "real PNG -- it merely looks like one to a chunk walker"
    )
    assert png_dimensions(payload) == {"width": width, "height": height}


def test_the_shared_png_fixture_is_decodable_by_an_independent_parser():
    """Pillow, which shares no code with this repository, must open it and agree on its size."""
    from PIL import Image  # noqa: PLC0415  # an oracle for the fixture, not a runtime dependency

    with Image.open(io.BytesIO(valid_png(8, 6))) as image:
        image.load()

        assert image.size == (8, 6)
        assert image.format == "PNG"


@pytest.mark.parametrize(
    ("label", "payload"),
    [("612x792", valid_pdf()), ("822x672 fitted", valid_pdf(822, 672)), ("incremental", incremental_pdf()[0])],
)
def test_the_shared_pdf_fixture_is_accepted_by_an_independent_parser(label, payload):
    """⚠️ STRICT `pypdf`, because this is the round that found four fixtures which were not PDFs.

    All four previous positive fixtures failed here with `PdfReadError: startxref not found` -- they
    carried a `%PDF-` header and a `%%EOF` and nothing in between, which is exactly what a tail-window
    check accepts and a parser does not. `strict=True` is the point: a lenient reader reconstructs a
    broken xref and would have passed them too.
    """
    reader = pypdf.PdfReader(io.BytesIO(payload), strict=True)

    assert len(reader.pages) == 1, label
    assert reader.metadata is not None and reader.metadata.get("/Title"), label


def test_the_pdf_fixture_builder_writes_TRUE_cross_reference_offsets():
    """The property `payload_is_complete` never checks, which is what makes it an independent oracle.

    The completeness check follows only the LAST `startxref`. This walks EVERY entry in the table and
    asserts each offset lands on the object header it claims -- so a builder that emitted a plausible
    table over wrong offsets would satisfy the check and fail here.
    """
    payload = valid_pdf()
    xref_at = int(payload[payload.rfind(b"startxref") + len(b"startxref") : payload.rfind(b"%%EOF")].strip())
    # `xref` / `0 8` / the free entry, then one 20-byte in-use entry per object, then `trailer`.
    lines = payload[xref_at:].split(b"\n")[3:]
    checked = 0
    for entry in lines:
        if entry.startswith(b"trailer"):
            break
        if not entry.strip():
            continue
        checked += 1
        offset = int(entry.split()[0])
        assert payload[offset:].startswith(b"%d 0 obj" % checked), (
            f"the xref entry for object {checked} points at byte {offset}, which is not its header"
        )

    assert checked == 7, f"expected 7 in-use objects, walked {checked} -- the fixture changed shape"


@pytest.mark.parametrize(
    ("kind", "payload"),
    [("png", valid_png()), ("svg", VALID_SVG), ("pdf", VALID_PDF)],
)
def test_a_complete_payload_is_accepted(kind, payload):
    """The positive control. Without it, "reject everything" would pass every test below."""
    ok, why = payload_is_complete(kind, payload)

    assert ok is True, f"a valid {kind} payload was refused: {why}"
    assert why == ""


@pytest.mark.parametrize("keep", [8, 16, 24, 33])
def test_a_png_cut_short_at_any_offset_is_refused(keep):
    """⚠️ 8 is the exact field case: signature only, `Content-Length` said 1024.

    The others walk the cut forward through the IHDR so the refusal is not an artefact of one offset:
    a partial IHDR, a complete IHDR with no IDAT, and a cut mid-length-prefix all have to fail.
    """
    ok, why = payload_is_complete("png", valid_png()[:keep])

    assert ok is False, f"{keep} bytes of a PNG were accepted as a complete render"
    assert why, "a refusal with no reason tells an operator nothing"


def test_a_png_whose_chunk_CRC_is_wrong_is_refused():
    """Corruption in flight, not truncation -- the chunk stream is intact and the data is not.

    A proxy that rewrites bytes, or a partial write recovered from cache, produces exactly this: the
    framing survives and the payload does not. A length-only check cannot see it. The flipped byte is
    inside IDAT's DATA, so every chunk length and type stays valid and only the CRC disagrees.
    """
    payload = bytearray(valid_png())
    idat_data_at = payload.index(b"IDAT") + 4
    payload[idat_data_at] ^= 0xFF

    ok, why = payload_is_complete("png", bytes(payload))

    assert ok is False and "CRC" in why, why


def test_a_png_missing_only_its_IEND_is_refused():
    """The subtlest truncation: every chunk present and correct, the terminator gone."""
    payload = valid_png()
    ok, why = payload_is_complete("png", payload[: -len(b"\x00\x00\x00\x00IEND\xaeB`\x82")])

    assert ok is False, "a PNG with no IEND was accepted; that is a download that stopped early"
    assert why


def test_an_svg_cut_short_is_refused_although_its_root_element_is_perfect():
    """⚠️ Why a full parse and not a root-element check.

    A truncated SVG still opens with a flawless `<svg ...>`, which is all the previous check looked
    at. The assertion below uses this repository's OWN root-element check -- the one the old code
    relied on -- so the claim is "the previous guard is satisfied and the document is still broken",
    measured rather than asserted.
    """
    truncated = VALID_SVG[: VALID_SVG.index(b"<text")]

    assert looks_like_svg(truncated), "the fixture must still satisfy the old root-element check"

    ok, why = payload_is_complete("svg", truncated)

    assert ok is False and "well-formed" in why, why


def test_an_xml_document_that_is_not_an_svg_is_refused():
    """A Tableau error body is well-formed XML. Parsing is necessary, not sufficient."""
    ok, why = payload_is_complete("svg", b'<?xml version="1.0"?><error code="400"><detail>nope</detail></error>')

    assert ok is False and "root element" in why, why


def test_a_pdf_without_its_trailer_is_refused():
    """`%%EOF` is the terminator, so its absence is the truncation signature."""
    ok, why = payload_is_complete("pdf", VALID_PDF.replace(b"%%EOF\n", b""))

    assert ok is False and "%%EOF" in why, why


def test_a_pdf_whose_trailer_is_far_from_the_end_is_refused():
    """⚠️ Position matters: `%%EOF` anywhere in the bytes is not the same as `%%EOF` at the end.

    A `b"%%EOF" in payload` check would accept a document that was cut short AFTER an earlier
    incremental-update trailer -- which is what a multi-revision PDF looks like mid-download.
    """
    ok, why = payload_is_complete("pdf", VALID_PDF + b"\x00" * 4096)

    assert ok is False and "%%EOF" in why, why


def test_csv_is_the_only_payload_kind_explicitly_without_a_payload_completeness_check():
    """A future leg must not inherit CSV's no-terminator exception silently."""
    assert payload_is_complete("csv", b"a,b\n1,2\n") == (True, "")
    ok, why = payload_is_complete("", b"")
    assert ok is False
    assert why == "no payload completeness check is registered for this kind"


def test_a_declared_chunk_length_beyond_the_payload_costs_no_allocation():
    """⚠️ A hostile length must be compared, never trusted into a slice or a read.

    A PNG chunk header declaring 4 GiB with four bytes behind it is one line away from an allocation
    the size of the declaration. The bound check runs before any slice, so this returns rather than
    consuming memory -- and it returns a REFUSAL, because a chunk that cannot fit is a truncation.
    """
    payload = valid_png()[:8] + (0xFFFFFFFF).to_bytes(4, "big") + b"IDAT" + b"\x00" * 4

    ok, why = payload_is_complete("png", payload)

    assert ok is False and "mid-chunk" in why, why


# ------------------------------------------------------------------ review round 3: THE CLASS
#
# ⚠️ Three rounds found the same defect in three formats -- a raw substring or tail search standing
# in for a structural boundary. The instances were an 8-byte PNG signature accepted as an image, an
# `<!ENTITY` scan bounded by the first raw `<svg`, and a `%%EOF` anywhere in the last 2 KiB. Fixing
# them one at a time is what produced three rounds, so the class gets a test of its own.
#
# The property is the one thing every marker scan structurally cannot satisfy: a check that answers
# "does this payload END correctly" must fail when bytes are appended AFTER the end. A scan passes
# for any suffix short enough to leave its marker inside the window.


@pytest.mark.parametrize("suffix_length", [1, 16, 100, 1024])
@pytest.mark.parametrize(
    ("kind", "payload"),
    [("png", valid_png()), ("svg", VALID_SVG), ("pdf", valid_pdf())],
)
def test_no_completeness_check_can_be_satisfied_by_bytes_that_are_not_the_end(kind, payload, suffix_length):
    """⚠️ The cross-format invariant. Every marker scan in this module's history fails it.

    The short suffixes are the discriminating ones and the reason this is parametrised rather than
    written once: the PDF tail window was 2,048 bytes, so a 4,096-byte suffix was refused and the
    check looked sound. **100 bytes passed.** A test that only ever appends more than the window
    measures the window, not the property.
    """
    ok, why = payload_is_complete(kind, payload + b"X" * suffix_length)

    assert ok is False, (
        f"{suffix_length} byte(s) appended after a complete {kind} payload were accepted. The check is "
        "answering 'do the right bytes appear somewhere' rather than 'does this payload end here'"
    )
    assert why


@pytest.mark.parametrize(("kind", "payload"), [("svg", VALID_SVG), ("pdf", valid_pdf())])
def test_trailing_WHITESPACE_is_still_accepted(kind, payload):
    """The positive control for the invariant above, and it is not a formality.

    Trailing whitespace after a PDF's `%%EOF` is legal and real producers emit it; XML allows it after
    the root element too. "Refuse anything appended" would satisfy every assertion above while
    rejecting perfectly good captures -- the failure direction that costs a re-capture.

    ⚠️ PNG is deliberately EXCLUDED, and that is a format fact rather than an oversight: a PNG ends at
    its IEND chunk and has no trailing-whitespace concept, so a byte after IEND is a byte too many
    whatever it is. Writing this test over all three formats asserted the opposite and failed --
    correctly. Formats differ; a control that pretends otherwise weakens the strictest one.
    """
    assert payload_is_complete(kind, payload + b"\n  \r\n") == (True, "")


def test_a_png_with_ANY_trailing_byte_is_refused_including_whitespace():
    """The counterpart: PNG's end is exact, so even a newline after IEND is a truncation signal.

    Stated as its own test rather than folded into the control above, so the asymmetry is a recorded
    decision instead of a parametrize list somebody trims later.
    """
    for suffix in (b"\n", b" ", b"\x00"):
        ok, why = payload_is_complete("png", valid_png() + suffix)
        assert ok is False, f"a PNG with a trailing {suffix!r} was accepted"
        assert why


def test_a_pdf_truncated_inside_a_LATER_revision_is_refused():
    """⚠️ Round-3 blocker 1, reproduced. A tail search cannot see this one at all.

    An incremental update appends a revised object, its own xref and its own trailer; the ORIGINAL
    `%%EOF` stays in the file forever. Cut before the newest `startxref` and a perfectly good earlier
    marker sits close to the end -- measured 105 bytes -- while the newest revision is lost. The file
    is not corrupt; it describes a document the customer no longer has, which is worse, because
    nothing downstream can tell.
    """
    complete, truncated = incremental_pdf()
    surviving_eof_distance = len(truncated) - truncated.rfind(b"%%EOF")

    assert surviving_eof_distance < 2048, (
        f"the surviving %%EOF is {surviving_eof_distance} bytes from the end, outside the 2 KiB window "
        "the old check used -- so this fixture no longer reproduces the defect it was written for"
    )
    assert payload_is_complete("pdf", complete) == (True, "")

    ok, why = payload_is_complete("pdf", truncated)

    assert ok is False and "%%EOF" in why, why


def test_a_pdf_whose_startxref_does_not_resolve_is_refused():
    """`%%EOF` in the right place is not enough -- the pointer before it has to lead somewhere.

    Three ways it can fail, each its own assertion because each has a different remedy: a missing
    `startxref`, a non-numeric offset, and an offset that lands on bytes that are not a
    cross-reference. The last is what a partially-overwritten file looks like.
    """
    body = valid_pdf()
    marker = body.rfind(b"startxref")
    offset_start = marker + len(b"startxref") + 1
    offset_end = body.index(b"\n", offset_start)

    for label, mutated in (
        ("no startxref", body[:marker] + body[offset_end:]),
        ("a non-numeric offset", body[:offset_start] + b"NOTANUMBER" + body[offset_end:]),
        ("an offset past the end", body[:offset_start] + b"999999" + body[offset_end:]),
        ("an offset landing on ordinary bytes", body[:offset_start] + b"20" + body[offset_end:]),
    ):
        ok, why = payload_is_complete("pdf", mutated)
        assert ok is False, f"a PDF with {label} was accepted as complete"
        assert why, f"a PDF with {label} was refused with no reason"


# --------------------------------------------------------- round-3 blocker 2: the entity BYPASS

ENTITY_SUBSET = (
    b'<!DOCTYPE svg [<!ENTITY a "AAAAAAAAAA">'
    b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
    b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
)
DOCUMENT = b'<svg xmlns="http://www.w3.org/2000/svg"><text>&c;</text></svg>'


@pytest.mark.parametrize(
    ("placement", "payload"),
    [
        ("a plain DOCTYPE", ENTITY_SUBSET + DOCUMENT),
        ("behind a COMMENT decoy", b"<!-- harmless <svg decoy> -->\n" + ENTITY_SUBSET + DOCUMENT),
        ("behind a PROCESSING-INSTRUCTION decoy", b'<?xml version="1.0"?><?decoy <svg ?>\n' + ENTITY_SUBSET + DOCUMENT),
        ("behind both", b"<?d <svg ?><!-- <svg -->" + ENTITY_SUBSET + DOCUMENT),
    ],
)
def test_an_entity_declaration_is_refused_WHEREVER_it_sits(placement, payload):
    """⚠️ Round-3 blocker 2: the refusal was a substring search with an attacker-movable boundary.

    The old guard scanned only the bytes before the first raw `<svg`, so writing `<svg` inside a
    comment or a processing instruction moved the boundary in front of the real DTD. Measured: with
    the comment decoy the first `<svg` was at byte 14 and the first `<!ENTITY` at byte 45, expat
    expanded the entity to 1,000 characters, and `_capture_render` recorded `status: ok` with a digest
    and a path -- so the resource-exhaustion defence was bypassable AND the artifact was credited.

    A parser knows where the DTD is. This is the same fixture family for all four placements, so a
    fix that special-cases comments would still fail on the PI.
    """
    ok, why = payload_is_complete("svg", payload)

    assert ok is False, f"an entity declaration {placement} was not refused"
    assert why == "the SVG declares XML entities, which this parser refuses to expand", why


def test_the_decoy_really_does_move_a_substring_boundary():
    """The control that makes the test above bite: without it, the decoys could be inert.

    ⚠️ This asserts the PROPERTY the old guard depended on is genuinely violated -- the first raw
    `<svg` really does precede the first `<!ENTITY` -- rather than trusting that the fixture is
    adversarial. If a future edit makes the decoys harmless, this fails and says so instead of
    letting four green tests vouch for nothing.
    """
    for payload in (
        b"<!-- harmless <svg decoy> -->\n" + ENTITY_SUBSET + DOCUMENT,
        b'<?xml version="1.0"?><?decoy <svg ?>\n' + ENTITY_SUBSET + DOCUMENT,
    ):
        assert 0 <= payload.find(b"<svg") < payload.find(b"<!ENTITY"), (
            "the decoy no longer places `<svg` before the DTD, so a prefix-bounded scan would have "
            "found the entity anyway and these fixtures prove nothing"
        )


# ------------------------------------------------ round-3 MEDIUM: the ENCODING exception family

UNSUPPORTED_ENCODINGS = ["utf-32", "utf-7", "shift_jis", "big5", "gb18030"]
# A shared run this long is an echo, not a coincidence -- the same floor `tests/test_diagnostic_
# redaction.py` uses. Shorter runs are exactly how a naive check reported "no" leaking out of the
# word "cannot", which is what this helper exists to stop.
_MIN_ECHO_RUN = 6


def _longest_shared_run(needle: str, haystack: str) -> str:
    """The longest contiguous slice of ``needle`` that reached ``haystack``. ``''`` means none did."""
    lowered = haystack.lower()
    for length in range(len(needle), _MIN_ECHO_RUN - 1, -1):
        for start in range(len(needle) - length + 1):
            run = needle[start : start + length]
            if run.lower() in lowered:
                return run
    return ""


@pytest.mark.parametrize("encoding", [*UNSUPPORTED_ENCODINGS, "NO-SUCH-CODEC-42", "utf-8-but-wrong"])
def test_an_encoding_the_parser_cannot_decode_becomes_a_verdict_not_a_crash(encoding):
    """⚠️ TWO exception families, and neither is an `ExpatError` -- measured, not assumed.

    The first draft of this fix assumed moving to expat would unify them. It does not: an unrecognised
    `encoding=` raises `LookupError` **carrying the declared name verbatim**, and the multibyte codecs
    raise `ValueError: multi-byte encodings are not supported`. Both escaped `payload_is_complete`
    and crashed the capture instead of becoming a non-`ok` leg.

    Both families are covered here on purpose. A test over only the unknown-codec case would pass
    against a fix that caught `LookupError` alone -- which is exactly the state this found.
    """
    body = f'<?xml version="1.0" encoding="{encoding}"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'.encode()

    ok, why = payload_is_complete("svg", body)

    assert ok is False, f"a document declaring encoding={encoding} was accepted"
    assert why == "the SVG declares an encoding this parser cannot decode", why
    assert _longest_shared_run(encoding, why) == "", (
        f"the diagnostic echoes {_longest_shared_run(encoding, why)!r} from the declared encoding "
        f"({why!r}), which is attacker-chosen text persisted unredacted into oracle-manifest.json"
    )


def test_an_encoding_the_parser_CAN_decode_is_still_accepted():
    """The positive control. "Refuse every encoding declaration" would pass every assertion above.

    UTF-16 is a real encoding expat streams natively, and a Tableau export could legitimately arrive
    in it, so refusing it would cost a capture rather than prevent one.
    """
    body = '<?xml version="1.0" encoding="utf-16"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'.encode("utf-16")

    assert payload_is_complete("svg", body) == (True, "")


def test_no_external_entity_can_reach_the_network():
    """A parse of a customer's response must never make an outbound request on their behalf.

    Expat does not fetch external parameter entities by default, so this is belt and braces -- but a
    future edit enabling `SetParamEntityParsing` would silently turn every SVG check into an SSRF
    primitive, and nothing else in this suite would notice.
    """
    body = (
        b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "http://127.0.0.1:1/secret">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>'
    )

    ok, why = payload_is_complete("svg", body)

    assert ok is False and "entities" in why, why
