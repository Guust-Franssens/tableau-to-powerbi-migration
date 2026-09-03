"""
purpose: build GENUINELY valid PDFs for the capture tests, with real cross-reference offsets
usage:   from pdf_fixtures import valid_pdf, incremental_pdf

⚠️ **This module exists because all four positive PDF fixtures in this repository were invalid**, and
each of them asserted that a truncated render is acceptable evidence. Strict ``pypdf`` rejected every
one with ``PdfReadError: startxref not found``:

* ``tests/test_payload_completeness.py`` -- ``%PDF-1.4 ... trailer %%EOF`` with no xref at all
* ``tests/test_capture_tableau_oracle_leg_decoupling.py`` -- the same bytes
* ``tests/test_capture_tableau_oracle_svg.py`` -- ``PDF_FITTED`` and ``PDF_LETTER``, a MediaBox and a
  ``%%EOF`` with no object structure between them

That is the THIRD round in which a fixture pinned the defect under review: round 1 found the PNG
fixture incomplete, round 2's fix found `_png` and the two PDF bodies, and round 3 found these. The
pattern is always the same -- a fixture built to satisfy the check being tested rather than the format
being claimed -- so these are built to the format and verified by a parser that is not ours.

``tests/test_payload_completeness.py::test_the_shared_pdf_fixture_is_accepted_by_an_independent_parser``
hands every fixture to ``pypdf`` in STRICT mode. That is the independent oracle; nothing here consults
``payload_is_complete``, which would be circular.
"""

from __future__ import annotations

PDF_HEADER = b"%PDF-1.4\n"


def _objects(width: int, height: int, title: bytes) -> list[bytes]:
    """The seven objects a minimal one-page PDF needs, including a font file and an image XObject.

    The font descriptor and the image are not decoration: ``pdf_facts`` reports ``fontfile_count`` and
    ``image_xobjects``, and a fixture without them could not exercise the fidelity note those fields
    exist for -- a Tableau PDF embeds its fonts, which is why that rung is worth capturing at all.
    """
    return [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d.000000 %d.000000] "
        b"/Resources << /Font << /F1 4 0 R >> /XObject << /Im0 5 0 R >> >> >>" % (width, height),
        b"<< /Type /FontDescriptor /FontName /Tableau /Flags 4 /FontFile2 6 0 R >>",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 1 >>\nstream\n\x00\nendstream",
        b"<< /Length 4 >>\nstream\n\x00\x01\x02\x03\nendstream",
        b"<< /Title (%s) >>" % title,
    ]


def _assemble(header: bytes, bodies: list[bytes], first_number: int) -> tuple[bytes, list[int]]:
    """Serialise numbered objects after ``header``; return ``(bytes, offset per object)``.

    The offsets are the REAL byte positions, computed as the buffer grows. That is what makes the
    cross-reference table true rather than plausible, and it is the property the fixture-integrity
    test checks -- one this repository's own completeness check never looks at, since it follows only
    the LAST ``startxref``.
    """
    out = header
    offsets: list[int] = []
    for index, body in enumerate(bodies):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (first_number + index, body)
    return out, offsets


def _xref_table(offsets: list[int], *, first_number: int, with_free_entry: bool) -> bytes:
    """A classic cross-reference section. Every entry is exactly 20 bytes, as the format requires."""
    entries = b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    if with_free_entry:
        return b"xref\n0 %d\n0000000000 65535 f \n%s" % (len(offsets) + 1, entries)
    return b"xref\n%d %d\n%s" % (first_number, len(offsets), entries)


def valid_pdf(width: int = 612, height: int = 792, title: bytes = b"ORIGINAL") -> bytes:
    """A complete, single-revision PDF whose trailer resolves: xref, startxref, %%EOF, in that order."""
    body, offsets = _assemble(PDF_HEADER, _objects(width, height, title), first_number=1)
    xref_at = len(body)
    xref = _xref_table(offsets, first_number=1, with_free_entry=True)
    trailer = b"trailer\n<< /Size %d /Root 1 0 R /Info 7 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(offsets) + 1,
        xref_at,
    )
    return body + xref + trailer


def incremental_pdf(width: int = 612, height: int = 792) -> tuple[bytes, bytes]:
    """``(complete, truncated)`` -- a two-revision PDF, cut inside the SECOND revision.

    ⚠️ The shape the tail-window check could not see. An incremental update appends a revised object,
    its own cross-reference section and its own trailer; the ORIGINAL ``%%EOF`` stays in the file
    forever. Cutting before the newest ``startxref``/``%%EOF`` therefore leaves a perfectly good
    earlier marker close to the end -- measured 105 bytes -- while the newest revision is lost. An
    independent parser reads the truncated file as the PRIOR revision (``/Title (ORIGINAL)``) where
    the complete one says ``LATEST-REVISION``: not a corrupt file, a file describing something the
    customer no longer has.
    """
    first = valid_pdf(width, height, title=b"ORIGINAL")
    update, offsets = _assemble(b"", [b"<< /Title (LATEST-REVISION) >>"], first_number=7)
    update_at = len(first) + offsets[0]
    second_xref_at = len(first) + len(update)
    second_xref = _xref_table([update_at], first_number=7, with_free_entry=False)
    previous_xref_at = int(first[first.rfind(b"startxref") + len(b"startxref") : first.rfind(b"%%EOF")].strip())
    second_trailer = b"trailer\n<< /Size 8 /Root 1 0 R /Info 7 0 R /Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (
        previous_xref_at,
        second_xref_at,
    )
    complete = first + update + second_xref + second_trailer
    return complete, first + update + second_xref
