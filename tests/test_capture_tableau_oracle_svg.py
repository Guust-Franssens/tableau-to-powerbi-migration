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

    def __init__(self, responses: dict[str, tuple[int, bytes]], *, pat_secret: str = "a-long-enough-secret"):
        super().__init__(
            oracle.SiteCredentials(
                base="https://example.online.tableau.com",
                site="site",
                pat_name="name",
                pat_secret=pat_secret,
                version="3.29",
            )
        )
        self.responses = responses
        self.paths: list[str] = []
        self.token, self.site_id = "tok", "sid"

    def _request(self, method, path, *, body=None, accept=None, authed=True, api=None):  # noqa: ARG002
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
    """Stands in for `run.session`. It carries a REAL redactor, not a pass-through.

    `write_manifest` scrubs the whole manifest through `run.session.redact_text` immediately before
    serialising (the round-5 sink), so a double without one would switch that guard off silently in
    every test here.
    """

    reauth_count = 0
    retry_count = 0

    def __init__(self, secret: str = "a-long-enough-secret"):
        self._session = _Session({}, pat_secret=secret)

    def redact_text(self, text: str) -> str:
        return self._session.redact_text(text)


def _manifest(records, tmp_path, run_kwargs=None, capability=None):
    env = {"TABLEAU_SERVER_URL": "https://s", "TABLEAU_SITE": "site", "TABLEAU_REST_API_VERSION": "3.29"}
    run = oracle.CaptureRun(_Counter(), env, tmp_path, 0.0, **(run_kwargs or {}))
    code = oracle.write_manifest(records, run, capability)
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


# ------------------------------------------- #405 finding 2: a 200 is not proof of the format


def test_wrong_format_bytes_are_refused_rather_than_persisted_as_the_requested_type(tmp_path):
    """An older server that ignores `format=svg` returns default PNG; writing it as .svg is a lie."""
    session = _Session({"/data": (200, b"a\n1\n"), "image?format=svg": (200, _png(2000, 1600))})
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    assert record["svg"]["status"] == "format_mismatch"
    assert "vector" not in record["svg"]
    assert not list((tmp_path / "images").glob("*.svg"))


def test_a_format_mismatch_counts_as_a_failure_not_a_success(tmp_path):
    code, manifest = _manifest([_ok_record(svg={"status": "format_mismatch", "detail": "got png"})], tmp_path)
    assert code == 3
    assert manifest["svg_ok"] == 0


def test_a_genuine_svg_is_still_captured_and_graded(tmp_path):
    session = _Session({"/data": (200, b"a\n1\n"), "image?format=svg": (200, HR_SVG)})
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    assert record["svg"]["status"] == "ok" and record["svg"]["vector"] is True


# ------------------------------------------- #405 finding 5: no secret may reach the manifest


def test_raw_get_body_is_only_reported_through_the_session_redactor():
    """`raw_get` returns RAW bytes by contract (classification needs them); `redact_text` is the gate."""
    token = "SYNTHETIC_SESSION_TOKEN_123"
    body = f"<error><detail>echo X-Tableau-Auth: {token}</detail></error>".encode()
    session = _Session({"image?format=svg": (401, body)})
    session.token = token
    status, payload, _ctype = session.raw_get("/sites/sid/views/v/image?format=svg")
    assert status == 401 and token.encode() in payload  # raw, by design
    assert token not in session.redact_text(payload.decode())
    assert "[REDACTED]" in session.redact_text(payload.decode())


# ------------------------------------------- #405 finding 4: a required reference cannot be silent


def test_a_requested_render_that_never_arrived_is_a_failure(tmp_path):
    """Absent-because-not-asked-for is fine; absent-when-asked-for is the exit-0-with-nothing hole."""
    assert oracle._render_statuses(_ok_record()) == ("ok", "ok", "ok")  # pylint: disable=protected-access
    assert "not_captured" in oracle._render_statuses(_ok_record(), frozenset({"svg"}))  # pylint: disable=protected-access


def test_reference_best_that_selected_no_tier_exits_5_rather_than_0(tmp_path):
    code, manifest = _manifest(
        [_ok_record()],
        tmp_path,
        run_kwargs={"requested_renders": frozenset(), "reference_required": True},
        capability={"selected_tier": None, "warnings": ["capability UNDETERMINED"], "tiers": []},
    )
    assert code == 5
    assert manifest["reference_missing"] is True
    assert manifest["reference_required"] is True


def test_reference_best_that_did_capture_something_is_not_flagged(tmp_path):
    code, manifest = _manifest(
        [_ok_record(svg={"status": "ok"})],
        tmp_path,
        run_kwargs={"requested_renders": frozenset({"svg"}), "reference_required": True},
    )
    assert code == 0
    assert manifest["reference_missing"] is False


def test_a_data_only_run_without_reference_best_still_exits_zero(tmp_path):
    """The flag is what makes a missing reference an error; a plain oracle run is unaffected."""
    code, manifest = _manifest([_ok_record()], tmp_path)
    assert code == 0
    assert manifest["reference_required"] is False


def test_the_manifest_records_what_was_requested_not_only_what_arrived(tmp_path):
    _, manifest = _manifest(
        [_ok_record(svg={"status": "ok"})],
        tmp_path,
        run_kwargs={"requested_renders": frozenset({"svg", "pdf"}), "reference_required": True},
    )
    assert manifest["requested_renders"] == ["pdf", "svg"]


# ------------------------------------ #405 round 3, finding 1: the format-mismatch detail is a leak

REFLECTED_SECRET = "SECRET42-the-rest-of-a-real-pat"


def test_a_format_mismatch_detail_is_redacted_before_it_reaches_the_manifest(tmp_path):
    """`_capture_render` serialised `format_matches`' diagnostic, which quotes the response's own
    leading bytes, with no redactor at all. A source whose export begins with credential-shaped text
    -- or a proxy reflecting one -- wrote it into `oracle-manifest.json` verbatim.

    ⚠️ Planted as the PAT **name**, not the secret. Round 5 added a seam that REFUSES a successful body
    echoing the PAT secret or session token, which would retire this test by making its route
    unreachable. The name is deliberately redacted rather than refused (`reflected_credential`), so it
    is what keeps the round-3 diagnostic fix under test.
    """
    body = f"{REFLECTED_SECRET} is what a reflecting export returned".encode()
    session = _Session(
        {"/data": (200, b"a\n1\n"), "image?format=svg": (200, body)}, pat_secret="an-unrelated-long-secret"
    )
    session._creds = oracle.SiteCredentials(  # pylint: disable=protected-access
        base="https://example.online.tableau.com",
        site="site",
        pat_name=REFLECTED_SECRET,
        pat_secret="an-unrelated-long-secret",
        version="3.29",
    )
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    assert record["svg"]["status"] == "format_mismatch"
    serialized = json.dumps(record)
    assert "SECRET42" not in serialized
    assert "[REDACTED]" in serialized


def test_a_successful_body_echoing_the_pat_secret_is_refused_not_persisted(tmp_path):
    """Round 5's seam, on the render leg: nothing is written, and the status names the cause."""
    session = _Session(
        {"/data": (200, b"a\n1\n"), "image?format=svg": (200, f"<svg>{REFLECTED_SECRET}</svg>".encode())},
        pat_secret=REFLECTED_SECRET,
    )
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    assert record["svg"]["status"] == oracle.CREDENTIAL_REFLECTED
    assert not list((tmp_path / "images").glob("*")) if (tmp_path / "images").exists() else True
    assert "SECRET42" not in json.dumps(record)


# ---------------------------- #405 round 3, finding 3: a credential block is not a hard failure


def _blocked_record(**legs):
    record = {"view_name": "v", "workbook_name": "w", "data": {"status": "source_credential", "detail": "oauth"}}
    record.update(legs)
    return record


def test_a_render_never_attempted_because_its_data_leg_was_blocked_inherits_that_status():
    """`capture_view` returns before any render once data fails -- all four routes share one VizQL
    render, so re-asking three times costs metered calls to learn the same thing. Inventing an
    independent `not_captured` for each made ONE credential fault look like a credential fault AND
    three hard failures."""
    statuses = oracle._render_statuses(_blocked_record(), frozenset({"svg"}))  # pylint: disable=protected-access
    assert statuses == ("ok", "source_credential", "ok")


def test_a_genuinely_broken_data_leg_still_fails_its_requested_renders():
    """The other half of the same rule: propagation must not turn every absence into 'blocked'."""
    record = {"view_name": "v", "data": {"status": "failed", "detail": "boom"}}
    statuses = oracle._render_statuses(record, frozenset({"svg", "pdf"}))  # pylint: disable=protected-access
    assert statuses == ("ok", "failed", "failed")


def test_a_credential_only_run_exits_2_rather_than_3(tmp_path):
    """Exit 2 is the human-actionable code -- 'reauthorize the source in Tableau'. Collapsing it into
    3 sends the operator to debug our capture instead."""
    code, manifest = _manifest(
        [_blocked_record()],
        tmp_path,
        run_kwargs={"requested_renders": frozenset({"svg"})},
    )
    assert code == 2
    assert (manifest["credential_blocked"], manifest["failed"]) == (1, 0)


def test_a_credential_only_run_under_reference_best_exits_2_rather_than_5(tmp_path):
    """Nothing rendered, but no render COULD have: code 5 would point at our capability probe."""
    code, manifest = _manifest(
        [_blocked_record()],
        tmp_path,
        run_kwargs={"requested_renders": frozenset({"svg"}), "reference_required": True},
    )
    assert code == 2
    assert manifest["reference_missing"] is False


def test_a_partial_block_that_still_rendered_nothing_is_a_missing_reference(tmp_path):
    """Something renderable was reachable and nothing came back, so the absence is NOT explained by
    the credential -- code 5 is right here, and must not be swallowed by the guard above."""
    code, _ = _manifest(
        [_blocked_record(), _ok_record()],
        tmp_path,
        run_kwargs={"requested_renders": frozenset(), "reference_required": True},
    )
    assert code == 5


def test_a_run_that_is_both_blocked_and_broken_is_still_a_failure(tmp_path):
    code, manifest = _manifest(
        [_blocked_record(), _ok_record(svg={"status": "failed", "detail": "boom"})],
        tmp_path,
        run_kwargs={"requested_renders": frozenset({"svg"})},
    )
    assert code == 3
    assert (manifest["credential_blocked"], manifest["failed"]) == (1, 1)


# ------------------- #405 round 3, findings 4 and 6: cross-view probing must not lie about either
# which tier it found or how hard it looked

GATEWAY_504 = b"<error><summary>Gateway</summary><detail>gateway timeout</detail></error>"


class _ProbeSession(oracle.TableauSession):
    """Scripted probe layer keyed by ``(view luid, route)``, so views can disagree with each other."""

    def __init__(self, responses: dict[tuple[str, str], tuple[int, bytes]]):
        super().__init__(
            oracle.SiteCredentials(
                base="https://example.online.tableau.com",
                site="site",
                pat_name="a-long-enough-pat-name",
                pat_secret="a-long-enough-secret",
                version="3.29",
            )
        )
        self.responses = responses
        self.probed: list[tuple[str, str]] = []
        self.token, self.site_id = "tok", "sid"

    def _request(self, method, path, *, body=None, accept=None, authed=True, api=None):  # noqa: ARG002
        luid, route = path.split("/views/")[1].split("/", 1)
        self.probed.append((luid, route))
        return (*self.responses[(luid, route)], {})


def _probe(monkeypatch, responses, view_count):
    monkeypatch.setattr(
        oracle.capability, "server_info", lambda *_a, **_k: {"rest_api_version": "3.30", "product_version": "2026.3.0"}
    )
    session = _ProbeSession(responses)
    env = {"TABLEAU_SERVER_URL": "https://s", "TABLEAU_REST_API_VERSION": "3.29"}
    views = [{"id": f"v{i}", "name": f"View {i}"} for i in range(1, view_count + 1)]
    return session, oracle.capability.probe_render_capability(session, env, views)


SVG_ROUTE, PDF_ROUTE, PNG_ROUTE = "image?format=svg", "pdf?type=Unspecified", "image?resolution=high"


def test_a_later_view_that_proves_a_better_tier_beats_an_earlier_provisional_one(monkeypatch):
    """Both selections are provisional, so `(selected, complete)` tied and the FIRST view won
    regardless of tier quality: view 1's transient SVG/PDF failures fell through to PNG, and
    `--reference-best` then captured a raster reference on a site that had just proved PDF."""
    _, report = _probe(
        monkeypatch,
        {
            ("v1", SVG_ROUTE): (504, GATEWAY_504),
            ("v1", PDF_ROUTE): (504, GATEWAY_504),
            ("v1", PNG_ROUTE): (200, _png(2000, 1600)),
            ("v2", SVG_ROUTE): (504, GATEWAY_504),
            ("v2", PDF_ROUTE): (200, PDF_FITTED),
        },
        view_count=2,
    )
    assert report["selected_tier"] == "pdf"
    assert report["provisional"] is True


def test_a_settled_answer_still_beats_a_provisional_one_at_the_same_tier(monkeypatch):
    """Tier quality is the TIE-BREAK, not the primary key: completeness still ranks above it."""
    _, report = _probe(
        monkeypatch,
        {
            ("v1", SVG_ROUTE): (504, GATEWAY_504),
            ("v1", PDF_ROUTE): (200, PDF_FITTED),
            ("v2", SVG_ROUTE): (400, SVG_TOO_OLD.encode()),
            ("v2", PDF_ROUTE): (200, PDF_FITTED),
        },
        view_count=2,
    )
    assert report["selected_tier"] == "pdf"
    assert report["capability_complete"] is True


def test_probe_views_tried_counts_the_iterations_actually_performed(monkeypatch):
    """It reported `min(len(views), MAX_CAPABILITY_PROBE_VIEWS)` -- the number of ELIGIBLE views. A
    first view that answered completely and broke out of the loop was written up as three, making one
    probe read as three independent corroborations."""
    session, report = _probe(monkeypatch, {("v1", SVG_ROUTE): (200, HR_SVG)}, view_count=5)
    assert report["probe_views_tried"] == 1
    assert report["probe_view_luids"] == ["v1"]
    assert {luid for luid, _ in session.probed} == {"v1"}


def test_probe_views_tried_records_every_view_it_really_walked(monkeypatch):
    """And is still capped: the probe costs metered export calls."""
    disconnected = SOURCES_DISCONNECTED.encode()
    responses = {
        (f"v{i}", route): (400, disconnected) for i in range(1, 6) for route in (SVG_ROUTE, PDF_ROUTE, PNG_ROUTE)
    }
    _, report = _probe(monkeypatch, responses, view_count=5)
    assert report["probe_views_tried"] == oracle.capability.MAX_CAPABILITY_PROBE_VIEWS
    assert report["probe_view_luids"] == ["v1", "v2", "v3"]
