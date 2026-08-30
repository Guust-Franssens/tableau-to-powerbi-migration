"""
purpose: gate the render-capability ladder -- the part that must NOT trust a version string.
usage:   pytest tests/test_tableau_render_capability.py -q

The distinctions under test all come from measurements against a live Tableau Cloud site
(`fabric-migration-lab`) on 2026-08-30, plus Tableau's published version table:

* the site advertised `restApiVersion 3.30 / productVersion 2026.3.0` while the SAME site had
  reported 3.29 / 2026.2.5 a week earlier -- Cloud is force-upgraded, so a version read once and
  cached is wrong by the next run;
* `?format=svg` was refused at client api 3.15 / 3.21 / 3.25 with "SVG export requires API version
  3.29 or later" and accepted at 3.29;
* `?resolution=high` and `/pdf` answered 200 at every one of those client versions;
* a workbook whose sources are disconnected fails EVERY route with the same 400, which is why an
  all-indeterminate walk must not be reported as "this site cannot render".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tableau_render_capability as cap  # noqa: E402  # pylint: disable=wrong-import-position

SVG_TOO_OLD = (
    b"<error code='400000'><summary>Bad Request</summary><detail>SVG export requires API version "
    b"3.29 or later. Please upgrade your API version to use this feature.</detail></error>"
)
DISCONNECTED = (
    b"<error code='400074'><summary>Bad Request</summary><detail>There was a problem querying the "
    b"image for view 'x'.. (ExportViewException: Error: data sources not connected)</detail></error>"
)
DEAD_TOKEN = (
    b"<error code='401002'><summary>Unauthorized Access</summary><detail>Invalid authentication "
    b"credentials were provided.</detail></error>"
)


def scripted(responses: dict[tuple[str, str], tuple[int, bytes]]):
    """A fetcher keyed by (endpoint, query), recording what was actually asked."""
    calls: list[tuple[str, str]] = []

    def fetch(endpoint: str, query: str) -> tuple[int, bytes]:
        calls.append((endpoint, query))
        return responses[(endpoint, query)]

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


SVG = ("image", "?format=svg")
PDF = ("pdf", "?type=Unspecified")
PNG = ("image", "?resolution=high")


# --------------------------------------------------------------------------- version arithmetic


@pytest.mark.parametrize(
    "low,high",
    [("3.9", "3.10"), ("2.8", "3.0"), ("3.21", "3.29"), ("3.29", "3.30"), ("2.5", "2.8")],
)
def test_api_versions_compare_numerically_not_as_strings(low, high):
    """`"3.9" > "3.10"` as strings. Getting this wrong silently demotes a capable site."""
    assert cap.api_tuple(low) < cap.api_tuple(high)
    assert cap.supports(high, low) is True
    assert cap.supports(low, high) is False


def test_an_unknown_server_version_is_none_not_false():
    """`None` means 'the server did not say', which must not be read as 'the server cannot'."""
    assert cap.supports(None, "3.29") is None
    assert cap.supports("", "3.29") is None


@pytest.mark.parametrize(
    "api,expected",
    [("3.29", "Server 2026.2"), ("3.21", "2023.3"), ("2.8", "10.5"), ("2.5", "10.2"), ("3.25", "2025.1")],
)
def test_api_versions_translate_to_the_tableau_release_a_customer_would_recognise(api, expected):
    assert expected in cap.release_for(api)


def test_cloud_only_releases_are_marked_because_on_prem_can_never_reach_them():
    for api in ("3.26", "3.24", "3.22", "3.20", "3.18", "3.16"):
        assert "CLOUD-ONLY" in cap.release_for(api), api


def test_an_api_version_past_the_published_table_says_so_rather_than_inventing_a_release():
    """A live site already advertises 3.30 while Tableau's published table stops at 3.29."""
    assert "not in the published table" in cap.release_for("3.30")


def test_every_ladder_tier_resolves_to_a_known_release():
    for tier in cap.LADDER:
        assert "not in the published table" not in tier.min_release, tier.name


def test_the_ladder_is_ordered_best_first():
    """Probing stops at the first hit, so a mis-ordered ladder silently returns a worse reference."""
    assert [t.name for t in cap.LADDER] == ["svg", "pdf", "png_high"]
    assert [t.vector for t in cap.LADDER] == [True, True, False]


# --------------------------------------------------------------------------- probe classification


def test_a_version_gate_is_a_permanent_no_for_that_tier():
    assert cap.classify_probe(400, SVG_TOO_OLD)[0] == "unsupported"


def test_a_disconnected_source_says_nothing_about_capability():
    """The route failed upstream of the format; calling that 'unsupported' demotes a capable site."""
    assert cap.classify_probe(400, DISCONNECTED)[0] == "indeterminate"


def test_a_dead_token_is_indeterminate_not_unsupported():
    assert cap.classify_probe(401, DEAD_TOKEN)[0] == "indeterminate"


def test_content_is_available():
    assert cap.classify_probe(200, b"\x89PNG\r\n\x1a\n")[0] == "available"


# --------------------------------------------------------------------------- ladder walk


def test_svg_wins_and_the_lower_rungs_are_never_probed():
    fetch = scripted({SVG: (200, b"<svg/>")})
    report = cap.detect(fetch, "luid", configured_api="3.29", advertised_api="3.30")
    assert report["selected_tier"] == "svg"
    assert fetch.calls == [SVG]
    assert [t["verdict"] for t in report["tiers"]] == ["available", "not_probed", "not_probed"]


def test_an_on_prem_site_below_329_falls_back_to_pdf():
    """The whole point of the ladder: Server 2023.x-2025.x has no SVG but does have PDF."""
    fetch = scripted({SVG: (400, SVG_TOO_OLD), PDF: (200, b"%PDF-1.4")})
    report = cap.detect(fetch, "luid", configured_api="3.21", advertised_api="3.21")
    assert report["selected_tier"] == "pdf"
    assert fetch.calls == [SVG, PDF]


def test_when_pdf_is_also_refused_the_universal_png_rung_answers():
    fetch = scripted({SVG: (400, SVG_TOO_OLD), PDF: (400, SVG_TOO_OLD), PNG: (200, b"\x89PNG\r\n\x1a\n")})
    report = cap.detect(fetch, "luid", configured_api="3.15", advertised_api="3.15")
    assert report["selected_tier"] == "png_high"


def test_an_all_indeterminate_walk_reports_UNDETERMINED_not_incapable():
    """A blocked probe view fails every route identically; that is not a statement about the site."""
    fetch = scripted({SVG: (400, DISCONNECTED), PDF: (400, DISCONNECTED), PNG: (400, DISCONNECTED)})
    report = cap.detect(fetch, "luid", configured_api="3.29", advertised_api="3.30")
    assert report["selected_tier"] is None
    assert any("UNDETERMINED" in w for w in report["warnings"])
    assert not any("no render tier is available" in w for w in report["warnings"])


def test_a_genuine_refusal_of_every_tier_is_reported_as_incapable():
    fetch = scripted({SVG: (400, SVG_TOO_OLD), PDF: (400, SVG_TOO_OLD), PNG: (400, SVG_TOO_OLD)})
    report = cap.detect(fetch, "luid", configured_api="3.15", advertised_api="3.15")
    assert report["selected_tier"] is None
    assert any("no render tier is available" in w for w in report["warnings"])
    assert not any("UNDETERMINED" in w for w in report["warnings"])


# --------------------------------------------------------------------------- three-way reconciliation


def test_a_client_pin_that_costs_a_tier_the_server_supports_is_called_out():
    """The failure a version string cannot show you: the SERVER can, but WE asked as 3.21."""
    fetch = scripted({SVG: (400, SVG_TOO_OLD), PDF: (200, b"%PDF-1.4")})
    report = cap.detect(fetch, "luid", configured_api="3.21", advertised_api="3.30")
    assert any("TABLEAU_REST_API_VERSION is pinned to 3.21" in w for w in report["warnings"])
    assert any("3.29" in w for w in report["warnings"])


def test_no_pin_warning_when_the_server_genuinely_cannot_do_it():
    """An on-prem 2023.3 server is not misconfigured; telling its operator to raise a pin is noise."""
    fetch = scripted({SVG: (400, SVG_TOO_OLD), PDF: (200, b"%PDF-1.4")})
    report = cap.detect(fetch, "luid", configured_api="3.21", advertised_api="3.21")
    assert not any("pinned to" in w for w in report["warnings"])


def test_no_pin_warning_when_the_server_version_is_unknown():
    """serverinfo failing must not manufacture a confident configuration accusation."""
    fetch = scripted({SVG: (400, SVG_TOO_OLD), PDF: (200, b"%PDF-1.4")})
    report = cap.detect(fetch, "luid", configured_api="3.21", advertised_api=None)
    assert not any("pinned to" in w for w in report["warnings"])


def test_the_report_carries_both_version_strings_so_a_reader_can_see_them_disagree():
    fetch = scripted({SVG: (200, b"<svg/>")})
    report = cap.detect(fetch, "luid", configured_api="3.29", advertised_api="3.30")
    assert report["configured_api_version"] == "3.29"
    assert report["advertised_api_version"] == "3.30"
    assert report["probe_view_luid"] == "luid"
