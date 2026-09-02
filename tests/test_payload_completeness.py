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

import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from png_fixtures import valid_png  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_payload_facts import payload_is_complete, png_dimensions  # noqa: E402  # pylint: disable=C0413
from tableau_render_capability import looks_like_svg  # noqa: E402  # pylint: disable=wrong-import-position

VALID_SVG = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
    b'<svg width="370.681mm" height="211.931mm" xmlns="http://www.w3.org/2000/svg" version="1.1">'
    b'<text x="10" y="20">Active Employees</text></svg>'
)
VALID_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


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


def test_an_unknown_kind_is_never_refused():
    """The checker states what it can prove and invents nothing about a format it cannot read.

    The CSV data leg is the live case: it has no terminator, so truncation there is the transport's
    `Content-Length` check to catch, not this one's.
    """
    assert payload_is_complete("csv", b"a,b\n1,2\n") == (True, "")
    assert payload_is_complete("", b"") == (True, "")


def test_a_declared_chunk_length_beyond_the_payload_costs_no_allocation():
    """⚠️ A hostile length must be compared, never trusted into a slice or a read.

    A PNG chunk header declaring 4 GiB with four bytes behind it is one line away from an allocation
    the size of the declaration. The bound check runs before any slice, so this returns rather than
    consuming memory -- and it returns a REFUSAL, because a chunk that cannot fit is a truncation.
    """
    payload = valid_png()[:8] + (0xFFFFFFFF).to_bytes(4, "big") + b"IDAT" + b"\x00" * 4

    ok, why = payload_is_complete("png", payload)

    assert ok is False and "mid-chunk" in why, why
