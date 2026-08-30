"""
purpose: gate the SVG reference-render route added to capture_tableau_oracle.py for issue #403 --
         its geometry/text census, its version gate, and the manifest accounting that now has to
         count TWO render legs instead of one.
usage:   pytest tests/test_capture_tableau_oracle_svg.py -q

Every constant here is a live measurement against site `fabric-migration-lab` at REST 3.29, not an
invented fixture: the mm->px law (`mm * 96 / 25.4 == the /image pixel size`) held for all 52
capturable dashboards, and the sub-3.29 refusal text is the server's own wording.
"""

from __future__ import annotations

import json
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position

# The real root element of a `?format=svg` capture of `HR Dashboard | HR | Summary` (declared
# 1400x800 px), trimmed to the parts this module reasons about. 370.681mm * 96 / 25.4 == 1400.99,
# and the SVG viewport is measured to be the declared size + 1 px in each axis (52/52 dashboards).
HR_SVG = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    '<svg width="370.681mm" height="211.931mm" xmlns="http://www.w3.org/2000/svg" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1">'
    '<text x="10" y="20">Active Employees</text><text x="10" y="40">7,984</text>'
    '<path d="M0 0 L10 10"/><image xlink:href="data:image/png;base64,iVBORw0KGgo="/>'
    "</svg>"
).encode()

# Same shape, but the raster sub-element points back at the server: not durable offline evidence.
LEAKY_SVG = HR_SVG.replace(b'xlink:href="data:image/png;base64,iVBORw0KGgo="', b'xlink:href="/vizql/tile.png"')

SVG_TOO_OLD = (
    "<error code='400000'><summary>Bad Request</summary><detail>SVG export requires API version 3.29 "
    "or later. Please upgrade your API version to use this feature.. (0x5CE10192 : SVG export requires "
    "API version 3.29 or later.)</detail></error>"
)
SOURCES_DISCONNECTED = (
    "<error code='400074'><summary>Bad Request</summary><detail>There was a problem querying the image "
    "for view 'x'.. (0x5CE10192 : com.tableausoftware.domain.vizexport.exceptions.ExportViewException: "
    "Error: data sources not connected)</detail></error>"
)


def _png(width: int, height: int) -> bytes:
    """A minimal but genuinely valid PNG, so the IHDR reader is exercised rather than mocked."""
    ihdr = b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    chunks = b"\x89PNG\r\n\x1a\n" + len(ihdr[4:]).to_bytes(4, "big") + ihdr
    chunks += zlib.crc32(ihdr).to_bytes(4, "big")
    return chunks


class _Session(oracle.TableauSession):
    """Scripted HTTP layer keyed by the query string, so leg ordering cannot silently change."""

    def __init__(self, responses: dict[str, tuple[int, bytes]]):
        super().__init__(
            oracle.SiteCredentials(
                base="https://example.online.tableau.com",
                site="site",
                pat_name="name",
                pat_secret="a-long-enough-secret",
                version="3.29",
            )
        )
        self.responses = responses
        self.paths: list[str] = []
        self.token, self.site_id = "tok", "sid"

    def _request(self, method, path, *, body=None, accept=None, authed=True):  # noqa: ARG002
        self.paths.append(path)
        for suffix, (status, payload) in self.responses.items():
            if path.endswith(suffix):
                return status, payload, {}
        raise AssertionError(f"unscripted path {path}")

    def sign_in(self):
        self.token, self.site_id = "tok", "sid"


VIEW = {"id": "eb00995d-1ff1-4a42-9ac9-28846f861d31", "name": "HR | Summary", "workbook": {"id": "wb"}}


# --------------------------------------------------------------------------- geometry census


def test_svg_root_millimetres_are_converted_to_the_viewport_pixel_size():
    """Rounded, not truncated: the true value lands at 1400.99, so int() is off by one erratically."""
    facts = oracle.svg_facts(HR_SVG)
    assert (facts["width_px"], facts["height_px"]) == (1401, 801)


def test_the_svg_viewport_is_the_declared_dashboard_size_plus_one_pixel_per_axis():
    """The measured DASHBOARD law across 52 views -- what lines the SVG up with the PNG.

    Scoped deliberately: one worksheet measured offset 0, so this is not asserted for worksheets.
    """
    facts = oracle.svg_facts(HR_SVG)
    declared_px_from_the_image_endpoint = (1400, 800)
    assert (facts["width_px"] - 1, facts["height_px"] - 1) == declared_px_from_the_image_endpoint


def test_svg_facts_counts_the_literal_text_elements_that_make_content_readable():
    facts = oracle.svg_facts(HR_SVG)
    assert facts["text_elements"] == 2
    assert facts["path_elements"] == 1
    assert facts["image_elements"] == 1


def test_a_data_uri_raster_child_is_self_contained_but_a_server_href_is_not():
    assert oracle.svg_facts(HR_SVG)["external_refs"] == 0
    assert oracle.svg_facts(LEAKY_SVG)["external_refs"] == 1


def test_svg_without_a_millimetre_root_reports_no_geometry_rather_than_guessing():
    facts = oracle.svg_facts(b'<svg viewBox="0 0 100 50"><text>x</text></svg>')
    assert "width_px" not in facts and "height_px" not in facts


@pytest.mark.parametrize("size", [(1300, 1600), (2800, 1600), (3000, 1700)])
def test_png_dimensions_records_the_2x_raster_ceiling(size):
    assert oracle.png_dimensions(_png(*size)) == {"width": size[0], "height": size[1]}


def test_png_dimensions_returns_none_for_a_non_png_rather_than_a_bogus_number():
    assert oracle.png_dimensions(HR_SVG) is None
    assert oracle.png_dimensions(b"") is None


# --------------------------------------------------------------------------- capture wiring


def test_svg_leg_is_only_fetched_when_asked_for(tmp_path):
    session = _Session({"/data": (200, b"a\n1\n")})
    oracle.capture_view(session, VIEW, tmp_path, frozenset())
    assert not any("format=svg" in p for p in session.paths)


def test_svg_capture_writes_the_file_and_grades_it_in_the_record(tmp_path):
    session = _Session({"/data": (200, b"a\n1\n"), "image?format=svg": (200, HR_SVG)})
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    svg = record["svg"]
    assert svg["status"] == "ok"
    assert svg["vector"] is True
    assert svg["width_px"] == 1401
    assert svg["text_elements"] == 2
    assert (tmp_path / svg["path"]).read_bytes() == HR_SVG


def test_png_and_svg_are_captured_side_by_side_from_the_same_endpoint(tmp_path):
    session = _Session(
        {"/data": (200, b"a\n1\n"), "image?resolution=high": (200, _png(2800, 1600)), "image?format=svg": (200, HR_SVG)}
    )
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"png", "svg"}))
    assert record["image"]["dimensions_px"] == {"width": 2800, "height": 1600}
    assert record["image"]["vector"] is False
    assert record["svg"]["vector"] is True
    assert sum("/views/" in p and "/image" in p for p in session.paths) == 2


def test_a_failed_png_no_longer_swallows_the_svg_leg(tmp_path):
    """The pre-#403 code returned early on an image failure; with two legs that hid the second."""
    session = _Session(
        {
            "/data": (200, b"a\n1\n"),
            "image?resolution=high": (400, SOURCES_DISCONNECTED.encode()),
            "image?format=svg": (200, HR_SVG),
        }
    )
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"png", "svg"}))
    assert record["image"]["status"] != "ok"
    assert record["svg"]["status"] == "ok"


# --------------------------------------------------------------------------- version gate


def test_a_pre_329_site_is_reported_as_a_configuration_fault_not_a_broken_view(tmp_path):
    session = _Session({"/data": (200, b"a\n1\n"), "image?format=svg": (400, SVG_TOO_OLD.encode())})
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    assert record["svg"]["status"] == "unsupported_api_version"
    assert oracle.SVG_MIN_API_VERSION in record["svg"]["remedy"]
    assert "TABLEAU_REST_API_VERSION" in record["svg"]["remedy"]


def test_a_disconnected_source_keeps_its_own_classification_and_is_not_relabelled(tmp_path):
    session = _Session({"/data": (200, b"a\n1\n"), "image?format=svg": (400, SOURCES_DISCONNECTED.encode())})
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    assert record["svg"]["status"] == "failed"
    assert "remedy" not in record["svg"]


# --------------------------------------------------------------------------- manifest accounting


class _Counter:
    reauth_count = 0
    retry_count = 0


def _manifest(records, tmp_path):
    env = {"TABLEAU_SERVER_URL": "https://s", "TABLEAU_SITE": "site", "TABLEAU_REST_API_VERSION": "3.29"}
    code = oracle.write_manifest(records, oracle.CaptureRun(_Counter(), env, tmp_path, 0.0))
    return code, json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))


def _ok_record(**legs):
    record = {"view_name": "v", "workbook_name": "w", "data": {"status": "ok", "row_count": 1, "elapsed_sec": 0.1}}
    record.update(legs)
    return record


def test_a_data_only_capture_is_still_complete_when_no_render_was_requested(tmp_path):
    code, manifest = _manifest([_ok_record()], tmp_path)
    assert code == 0
    assert manifest["captured_complete"] == 1
    assert manifest["image_ok"] == 0 and manifest["svg_ok"] == 0


def test_a_failed_svg_leg_makes_the_view_incomplete_and_the_run_partial(tmp_path):
    code, manifest = _manifest(
        [_ok_record(image={"status": "ok"}, svg={"status": "failed", "detail": "boom"})],
        tmp_path,
    )
    assert code == 3
    assert manifest["captured_complete"] == 0
    assert manifest["image_ok"] == 1 and manifest["svg_ok"] == 0


def test_a_credential_block_on_the_svg_leg_is_counted_as_blocked_not_merely_failed(tmp_path):
    code, manifest = _manifest([_ok_record(svg={"status": "source_credential", "detail": "d"})], tmp_path)
    assert manifest["credential_blocked"] == 1
    assert manifest["failed"] == 0
    assert code == 2


def test_both_legs_ok_counts_once_in_each_column(tmp_path):
    code, manifest = _manifest(
        [_ok_record(image={"status": "ok"}, svg={"status": "ok"})],
        tmp_path,
    )
    assert code == 0
    assert (manifest["image_ok"], manifest["svg_ok"], manifest["captured_complete"]) == (1, 1, 1)


# --------------------------------------------------------------------------- the PDF rung

# Real bytes from `/pdf?type=Unspecified` on the 1000x800 `Seed - 92 - Viz Gauntlet Dashboard`:
# 0.75 * 1000 + 72 = 822pt wide, 0.75 * 800 + 72 = 672pt tall.
PDF_FITTED = b"%PDF-1.4\n/Type/Page /MediaBox [0 0 822.000000 672.000000]\n/FontFile2 /Subtype /Image\n"
# What a server that ignored the undocumented `type=Unspecified` falls back to: measured 612x792
# (US Letter portrait), NOT the 612x1008 Legal the documentation claims is the default.
PDF_LETTER = b"%PDF-1.4\n/Type/Page /MediaBox [0 0 612.000000 792.000000]\n/FontFile\n"


def test_pdf_facts_records_the_page_actually_returned_not_the_one_requested():
    """`type=Unspecified` is undocumented, so the fitted page must be verified, never assumed."""
    assert oracle.pdf_facts(PDF_FITTED)["page_pt"] == {"width": 822, "height": 672}
    assert oracle.pdf_facts(PDF_LETTER)["page_pt"] == {"width": 612, "height": 792}


def test_a_pdf_is_always_marked_vector_and_its_embedded_fonts_counted():
    """Embedded fonts are the PDF rung's one advantage over SVG, so the count is evidence."""
    facts = oracle.pdf_facts(PDF_FITTED)
    assert facts["vector"] is True
    assert facts["fontfile_count"] == 1
    assert facts["image_xobjects"] == 1


def test_pdf_capture_uses_the_fitted_page_route(tmp_path):
    session = _Session({"/data": (200, b"a\n1\n"), "pdf?type=Unspecified": (200, PDF_FITTED)})
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"pdf"}))
    assert record["pdf"]["status"] == "ok"
    assert record["pdf"]["vector"] is True
    assert record["pdf"]["page_pt"] == {"width": 822, "height": 672}
    assert (tmp_path / record["pdf"]["path"]).read_bytes() == PDF_FITTED


def test_all_three_rungs_can_be_captured_together(tmp_path):
    session = _Session(
        {
            "/data": (200, b"a\n1\n"),
            "image?resolution=high": (200, _png(2000, 1600)),
            "image?format=svg": (200, HR_SVG),
            "pdf?type=Unspecified": (200, PDF_FITTED),
        }
    )
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"png", "svg", "pdf"}))
    assert [record[leg]["status"] for leg in ("image", "svg", "pdf")] == ["ok", "ok", "ok"]


def test_a_failed_pdf_leg_is_counted_like_the_others(tmp_path):
    code, manifest = _manifest([_ok_record(pdf={"status": "failed", "detail": "boom"})], tmp_path)
    assert code == 3
    assert manifest["pdf_ok"] == 0
    assert manifest["captured_complete"] == 0


def test_render_routes_and_extensions_stay_in_step():
    """A kind present in one map and missing from the other writes a file with the wrong suffix."""
    assert set(oracle._RENDER_ROUTES) == set(oracle._RENDER_EXTENSIONS)  # pylint: disable=protected-access
