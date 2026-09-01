"""Tests for the EVIDENCE layer: is a captured render usable, and is it OF this workbook?

Split from 	est_check_reference_readiness.py to match the module split - the gate asks "is this
bundle ready to build against", scripts/reference_evidence.py asks "is this a picture I may
believe, and of what".

Every test here names the review round and finding it pins. WARNING: the fixtures themselves are
load-bearing - round 1 used an 8-byte PNG signature as "evidence" and asserted readiness, and round 2
found the manifests carrying no integrity metadata, so neither suite could have caught a swapped or
truncated render. write_png emits a genuine, parseable image and write_reference/write_oracle
record the sha256/ytes/dimensions the real producers write.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_reference_readiness as crr  # noqa: E402  # pylint: disable=wrong-import-position

from test_check_reference_readiness import (  # noqa: E402  # pylint: disable=wrong-import-position
    build_unit,
    bundle_fixture,
    write_oracle,
    write_png,
    write_reference,
)

__all__ = ["bundle_fixture"]

# --------------------------------------------------------------------------------------------
# Question 2: evidence must be USABLE and ATTRIBUTABLE (round-1 findings 3 and 4)
# --------------------------------------------------------------------------------------------


def test_a_zero_byte_render_is_rejected_not_promoted(bundle: Path) -> None:
    """Round-1 finding 3a: validity was `Path.is_file()`, so an empty file reached READY."""
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=sha, render_bytes=b"")

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert any("zero bytes" in item["reason"] for item in report["evidence_rejected"])
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_truncated_render_is_rejected(bundle: Path) -> None:
    """A PNG signature with no IHDR is exactly what the round-1 fixtures used as evidence."""
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(
        bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=sha, render_bytes=b"\x89PNG\r\n\x1a\n"
    )

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert any("did not parse" in item["reason"] for item in report["evidence_rejected"])


def test_an_illegibly_small_render_is_rejected(bundle: Path) -> None:
    """A real PNG, but a 16x16 favicon is not a reference anyone can compare against."""
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=sha, size=(16, 16))

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert any("legibility floor" in item["reason"] for item in report["evidence_rejected"])


def test_the_192px_embedded_thumbnail_route_still_counts(bundle: Path) -> None:
    """Discriminating control for the legibility floor.

    Tableau's embedded thumbnails are typically 192x192 (`extract_twb_thumbnails.py`), and they are a
    genuine evidence route. A floor that rejected them would make the gate refuse real captures.
    """
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=sha, size=(192, 192))

    assert crr.scan(bundle)["status"] == "READY"


def test_empty_capabilities_are_rejected_not_graded_unknown(bundle: Path) -> None:
    """Round-1 finding 3b: `capabilities: []` produced `ready [unknown]` and exit 0."""
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(bundle, [("Solo", "embedded_thumbnail", [])], source_sha=sha)

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "blind"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_an_unrecognised_capability_is_rejected(bundle: Path) -> None:
    """A capability outside the allowlist means a manifest this gate does not understand."""
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(bundle, [("Solo", "embedded_thumbnail", ["looks_fine_to_me"])], source_sha=sha)

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "blind"


def test_evidence_with_no_workbook_identity_is_rejected(bundle: Path) -> None:
    """Round-1 finding 4a: a manifest with no `source_workbook_sha256` cannot be attributed."""
    build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=None)

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert any("cannot be attributed" in item["reason"] for item in report["evidence_rejected"])


def test_evidence_for_another_workbook_does_not_satisfy_this_one(bundle: Path) -> None:
    """Round-1 finding 4b: one synthetic record made two different units report READY."""
    build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha="deadbeef" * 8)

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "blind"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_stale_capture_stops_counting_when_the_source_changes(bundle: Path) -> None:
    """A stale picture is worse than a missing one, because it looks like evidence."""
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=sha)
    assert crr.scan(bundle)["status"] == "READY"

    source = bundle.parent / "assets" / "WB.twb"
    source.write_text(source.read_text(encoding="utf-8") + "<!-- edited -->", encoding="utf-8")

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "blind"


def test_oracle_evidence_is_scoped_by_workbook_name(bundle: Path) -> None:
    """Oracle records carry `workbook_name`/`workbook_luid`; one for another workbook must not count."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_name": "Other Book"}])

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "blind"


def test_oracle_evidence_for_this_workbook_does_count(bundle: Path) -> None:
    """Discriminating twin of the scoping test above."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_name": "WB"}])

    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["readiness"] == "ready"
    assert page["grade"] == "layout/text only (oracle capture, default view state)"


# --------------------------------------------------------------------------------------------
# Round-2 finding 1: a render must be COMPLETE and must be the file the producer captured
# --------------------------------------------------------------------------------------------


def test_a_24_byte_blob_is_not_a_png(bundle: Path) -> None:
    """The previous parse read only the signature, `IHDR` marker and 8 dimension bytes.

    Measured: a 24-byte blob produced valid `Evidence` while Pillow rejected the same bytes with
    `Truncated File Read`. The whole chunk stream is walked now - lengths, CRCs, a 13-byte IHDR, and
    both IDAT and IEND required.
    """
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 320, 240)
    assert len(blob) == 24
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=sha, render_bytes=blob)

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_png_with_a_corrupted_crc_is_rejected(bundle: Path) -> None:
    """A real PNG whose bytes were tampered with no longer passes the chunk walk."""
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    reference = write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=sha)
    shot = reference / "shot-0.png"
    blob = bytearray(shot.read_bytes())
    blob[20] ^= 0xFF  # inside IHDR, so its CRC no longer agrees
    shot.write_bytes(bytes(blob))

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "blind"


def test_a_swapped_image_no_longer_counts(bundle: Path) -> None:
    """Round-2 finding 1: the recorded `sha256` was never read, so any image could be substituted.

    Measured on the real bundle: zeroing every manifest hash and setting dimensions to 1x1 still
    returned `READY 3/3` with zero rejections. Here the manifest is honest and the FILE is swapped
    for a different, perfectly valid render - which must stop counting.
    """
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    reference = write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=sha)
    assert crr.scan(bundle)["status"] == "READY"

    write_png(reference / "shot-0.png", 400, 300)

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert any("recorded sha256" in item["reason"] for item in report["evidence_rejected"])
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_manifest_with_no_recorded_hash_cannot_be_trusted(bundle: Path) -> None:
    """Both producers always write a sha256, so its absence means integrity nothing can confirm."""
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=sha, record_integrity=False)

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert any("records no sha256" in item["reason"] for item in report["evidence_rejected"])


def test_oracle_evidence_is_integrity_checked_too(bundle: Path) -> None:
    """The oracle route records `sha256`/`bytes`/`dimensions_px` and they are checked identically."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    oracle = write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_name": "WB"}])
    assert crr.scan(bundle)["status"] == "READY"

    write_png(oracle / "images" / "view-0.png", 400, 300)
    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "blind"
