"""
purpose: build GENUINELY valid render payloads for the capture tests, so a fixture cannot pin a defect
usage:   from png_fixtures import valid_png, truncate_png

⚠️ **This module exists because two fixtures asserted that broken bytes were correct.** Before the
completeness check landed, `_capture_render` credited any payload whose first 8 bytes were the PNG
signature, and both fixtures aimed at that code were themselves incomplete PNGs:

* ``test_capture_tableau_oracle_leg_decoupling.py`` -- signature + a partial IHDR: no IHDR CRC, no
  IDAT, no IEND, and a declared 13-byte IHDR of which only 10 bytes were present.
* ``test_capture_tableau_oracle_svg.py`` -- a docstring promising "a minimal but genuinely valid PNG"
  over signature + IHDR + its CRC, and nothing else: no image data and no terminator.

Each asserted the broken behaviour was correct, so a fix validated against either would have been
unfalsifiable. They are shared from here rather than duplicated because a fixture that is *wrong in
the same way in two files* is exactly how one gets corrected and the other does not.

Nothing here imports the code under test. The completeness checker is not consulted to decide whether
these bytes are valid -- that would be circular; ``test_png_fixture_integrity`` checks the built PNG
against properties the checker never looks at (the IDAT actually inflates, and to exactly the raw
scanline length the IHDR implies).
"""

from __future__ import annotations

import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    """``length type data crc`` -- the PNG chunk framing, with a real CRC32 over ``type + data``."""
    body = kind + data
    return len(data).to_bytes(4, "big") + body + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")


def valid_png(width: int = 8, height: int = 6) -> bytes:
    """A complete, standards-valid 8-bit RGB PNG: IHDR, one deflated IDAT, IEND, every CRC correct.

    Small by default on purpose. The dimensions are still real -- ``png_dimensions`` reads them out of
    the IHDR -- so a test that asserts on geometry gets a true answer rather than a mocked one.
    """
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


def truncate_png(payload: bytes, keep: int) -> bytes:
    """The same PNG, cut short -- what a peer that closes mid-body actually delivers."""
    return payload[:keep]
