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
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_reference_readiness as crr  # noqa: E402  # pylint: disable=wrong-import-position

from test_check_reference_readiness import (  # noqa: E402  # pylint: disable=wrong-import-position
    build_unit,
    bundle_fixture,
    write_engine_report,
    write_handover,
    write_oracle,
    write_png,
    write_reference,
    write_report,
    write_workbook,
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


def test_evidence_attribution_uses_the_exact_workbook_name(bundle: Path) -> None:
    """Found by WRITING the quarantine rule, not by reasoning (issue #421, round 4).

    `Evidence.is_for` compared workbook names through the lossy function, so a capture for
    `Ops  Summary` would have been attributed to a unit named `Ops Summary` - the same collapse
    defect, on WORKBOOK identity, at a layer neither the reviewer nor I had enumerated. If a
    published workbook name genuinely differs from the unit name, the LUID route is the answer.
    """
    build_unit(bundle, "Ops Summary", worksheets=["Revenue Trend"])
    write_oracle(
        bundle,
        [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_name": "Ops  Summary"}],
    )

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "blind"
    assert crr.main([str(bundle), "--quiet"]) == 1


# --------------------------------------------------------------------------------------------
# Round-2 finding 3: provenance explicitly marked non-reproducible must not be trusted
# --------------------------------------------------------------------------------------------


def write_provenance(root: Path, source: Path, *, luid: str, match: str, matched_by: str | None = None) -> None:
    """A `source-provenance.json` as `stamp_tableau_provenance.py` writes it.

    ``matched_by`` and ``match`` are two INDEPENDENT axes and the fixture keeps them separate:
    ``find_origin`` records *how the workbook was found* in the first and *how strongly the bytes
    were confirmed* in the second. Omitting ``matched_by`` - the default here - is the shape of an
    origin that establishes identity by neither route.
    """
    origin: dict[str, object] = {"workbook_luid": luid, "workbook_name": "Published Name", "match": match}
    if matched_by is not None:
        origin["matched_by"] = matched_by
    (root / "source-provenance.json").write_text(
        json.dumps(
            {
                "inputs": [
                    {
                        "input": {"file": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                        "origin": origin,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_a_name_only_provenance_luid_is_not_trusted(bundle: Path) -> None:
    """An origin that establishes identity by NEITHER route yields no LUID.

    ⚠️ Re-aimed by issue #450, and the distinction is the whole point. This fixture records no
    ``matched_by`` at all AND unconfirmed bytes, so nothing says how the workbook was found - it may
    have been a name collision, which ``find_origin`` falls back to and counts in ``same_name_count``.
    Refusing is right. It is NOT right for ``matched_by: "luid"``, which is the discriminating twin
    below: `stamp_tableau_provenance.find_origin` documents the two fields as "two independent axes",
    and reading the revision one as if it were the identity one is what discarded 23 correctly
    attributed renders on the reference estate.
    """
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_provenance(bundle, bundle.parent / "assets" / "WB.twb", luid="luid-1", match="name_only")
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_luid": "luid-1"}])

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "blind"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_luid_matched_provenance_survives_a_changed_revision(bundle: Path) -> None:
    """Issue #450, symptom A: `matched_by: "luid"` establishes identity even when the bytes differ.

    ``find_origin``'s own docstring: "a LUID match with ``name_only`` means 'this is provably the same
    item on the site, and it has changed since we harvested it', which is a different and more useful
    statement than a name collision". Measured on the reference estate, 48 of 78 inputs are
    ``name_only`` while matched BY LUID - the common case - and every one of them lost its identity
    and fell back to comparing an artifact stem against a published display name.
    """
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_provenance(bundle, bundle.parent / "assets" / "WB.twb", luid="luid-1", match="name_only", matched_by="luid")
    write_oracle(
        bundle,
        [
            {
                "view_name": "Revenue Trend",
                "view_type": "worksheet",
                "workbook_luid": "luid-1",
                "workbook_name": "Not WB",
            }
        ],
    )

    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["readiness"] == "ready"
    assert crr.scan(bundle)["evidence_attributed"]["luid"] == 1


def test_a_luid_matched_provenance_still_refuses_another_workbooks_render(bundle: Path) -> None:
    """The discriminating twin of the test above: trusting the LUID must still REFUSE a foreign one.

    Deliberately named identically to this unit, because that is the case a name fallback would get
    wrong: two projects may hold workbooks with the same display name.
    """
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_provenance(bundle, bundle.parent / "assets" / "WB.twb", luid="luid-1", match="name_only", matched_by="luid")
    write_oracle(
        bundle,
        [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_luid": "luid-2", "workbook_name": "WB"}],
    )

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert report["evidence_attributed"]["foreign"] == 1
    assert report["evidence_attributed"]["luid"] == 0


def test_the_asset_filename_luid_prefix_attributes_evidence_with_no_provenance_at_all(bundle: Path) -> None:
    """`harvest_estate_assets.py` names downloads `<luid>_<name>`, which needs no server to read.

    A harvested estate is the ordinary shape (`_runs/<NNN>-<slug>/assets/`), so this is the route a
    unit takes whenever provenance stamping was skipped or could not reach the site.
    """
    luid = "adc431bb-aeeb-43fe-8ecb-092d4bae8bfa"
    build_unit(bundle, f"{luid}_WB", worksheets=["Revenue Trend"])
    write_oracle(
        bundle,
        [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_luid": luid, "workbook_name": "Published"}],
    )

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "ready"
    assert report["evidence_attributed"]["luid"] == 1


def test_a_filename_luid_that_contradicts_provenance_establishes_nothing(bundle: Path) -> None:
    """Two identities that disagree are LESS evidence than none, so the join falls closed.

    Picking whichever was read first is the coin toss issue #438 named; here the render is refused
    and the page reports blind rather than being certified on a contested identity.
    """
    luid = "adc431bb-aeeb-43fe-8ecb-092d4bae8bfa"
    other = "007f70ac-bf40-4838-9d73-134d40f504db"
    build_unit(bundle, f"{luid}_WB", worksheets=["Revenue Trend"])
    write_provenance(
        bundle,
        bundle.parent / "assets" / f"{luid}_WB.twb",
        luid=other,
        match="sha256",
        matched_by="luid",
    )
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_luid": luid}])

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert report["evidence_attributed"]["unknown"] == 1


def test_every_refusal_is_counted_so_it_can_be_told_from_a_missing_capture(bundle: Path) -> None:
    """A refusal nobody can see is indistinguishable from a render that was never taken.

    That ambiguity is exactly how issue #450's inert guard passed for six review rounds: coverage
    numbers alone cannot tell "the guard refused this" from "nobody captured it", so a fix that only
    makes `pages_ready` go up cannot be told apart from one that deleted the guard.
    """
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(
        bundle,
        [
            {"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_name": "WB"},
            {"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_name": "Other Book"},
            {"view_name": "Orphan", "view_type": "worksheet", "workbook_luid": "luid-9"},
        ],
    )

    census = crr.scan(bundle)["evidence_attributed"]
    assert census == {"sha256": 0, "luid": 0, "name": 1, "foreign": 1, "unknown": 1}


def test_a_sha256_confirmed_provenance_luid_is_trusted(bundle: Path) -> None:
    """Discriminating twin: a byte-confirmed LUID must still work, or the route is dead."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_provenance(bundle, bundle.parent / "assets" / "WB.twb", luid="luid-1", match="sha256")
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_luid": "luid-1"}])

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "ready"


def test_a_record_carrying_both_luid_and_name_can_still_fall_back_to_the_name(bundle: Path) -> None:
    """Round-2 finding 3, mirror image: carrying a LUID used to DISCARD the name.

    Removing source provenance then made correctly-named records return `0/3 blind`, so the
    documented name fallback could not be reached by any record a real capture produces.
    """
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(
        bundle,
        [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_luid": "luid-1", "workbook_name": "WB"}],
    )

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "ready"


# --------------------------------------------------------------------------------------------
# Round-3 finding 2: lossy normalization on engine-to-engine joins
# --------------------------------------------------------------------------------------------


def test_two_engine_workbooks_differing_only_by_whitespace_are_both_required(bundle: Path) -> None:
    """`_unit_names` normalized into SETS, so a collision was permanently discarded.

    Measured: an engine report listing distinct workbooks `Ops  Summary` and `Ops Summary`, beside
    only `Ops Summary.Report`, collapsed to the single key `ops summary`, so
    `_units_without_reports` reported NO missing workbook - one ready report made the bundle ready
    while the second workbook shipped nothing at all.
    """
    doubled, single = "Ops  Summary", "Ops Summary"
    source = write_workbook(bundle.parent / "assets" / f"{single}.twb", worksheets=["Solo"])
    write_engine_report(bundle, workbooks=[doubled, single])
    write_handover(bundle, single, source_id=str(source))
    write_report(bundle, single, [obj.page_id for obj in crr.source_objects(source) or []])
    write_reference(
        bundle,
        [("Solo", "embedded_thumbnail", ["layout_grade"])],
        source_sha=hashlib.sha256(source.read_bytes()).hexdigest(),
    )

    report = crr.scan(bundle)
    missing = [unit for unit in report["units"] if "no report ships for it" in unit["detail"]]
    assert [unit["unit"] for unit in missing] == [doubled]
    assert report["status"] == "FINDINGS"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_duplicated_engine_workbook_name_cannot_be_attributed(bundle: Path) -> None:
    """Multiplicity survives: a name listed twice is a refusal, not a silently deduplicated one."""
    source = write_workbook(bundle.parent / "assets" / "WB.twb", worksheets=["Solo"])
    write_engine_report(bundle, workbooks=["WB", "WB"])
    write_handover(bundle, "WB", source_id=str(source))
    write_report(bundle, "WB", [obj.page_id for obj in crr.source_objects(source) or []])

    report = crr.scan(bundle)
    assert any("more than once" in unit["detail"] for unit in report["units"])
    assert report["status"] in {"FINDINGS", "CANNOT_ESTABLISH"}
    assert crr.main([str(bundle), "--quiet"]) != 0


def test_a_datasource_classification_uses_the_exact_name(bundle: Path) -> None:
    """A near-miss datasource name must not exempt a workbook unit from needing evidence."""
    write_engine_report(bundle, workbooks=[], datasources=["Shared  DS"])
    write_report(bundle, "Shared DS", ["page1"])

    report = crr.scan(bundle)
    assert report["status"] != "NOT_APPLICABLE"
    assert crr.main([str(bundle), "--quiet"]) != 0


def test_source_asset_selection_uses_the_exact_stem(bundle: Path) -> None:
    """`resolve_source` normalized the asset stem, so it could select the WRONG workbook."""
    (bundle.parent / "assets" / "Ops  Summary.twb").write_text("<workbook/>", encoding="utf-8")
    write_engine_report(bundle, workbooks=["Ops Summary"])
    write_report(bundle, "Ops Summary", ["page1"])
    (bundle / "input_manifest.json").write_text(
        json.dumps({"assets": [{"name": "Ops  Summary.twb", "staged_input_path": None}]}), encoding="utf-8"
    )

    assert crr.resolve_source(bundle, "Ops Summary", None, None) is None


def test_untyped_evidence_is_reported_with_the_route_to_make_it_usable(bundle: Path) -> None:
    """Round-3: a route that works by guessing is worse than one honestly unavailable AND explained.

    A `manual` record with no declared type cannot satisfy any page. The output must say so and name
    the fix, rather than letting the operator conclude the gate is broken.
    """
    sha = build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_reference(
        bundle,
        [("tableau-Revenue Trend", "manual", ["layout_grade", "text_readable", "validation_grade"])],
        source_sha=sha,
    )

    report = crr.scan(bundle)
    assert report["evidence_untyped"] == 1
    rendered = crr.render(report)
    assert "UNTYPED EVIDENCE" in rendered
    assert "view_type" in rendered
