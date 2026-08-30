"""
purpose: gate the render-capability ladder -- the part that must NOT trust a version string, must NOT
         trust an HTTP 200, and must NOT persist a secret.
usage:   pytest tests/test_tableau_render_capability.py -q

The distinctions under test come from measurements against a live Tableau Cloud site
(`fabric-migration-lab`) on 2026-08-30, plus Tableau's published version table:

* the site advertised `restApiVersion 3.30 / productVersion 2026.3.0` while the SAME site had
  reported 3.29 / 2026.2.5 a week earlier -- Cloud is force-upgraded, so a version read once and
  cached is wrong by the next run;
* `?format=svg` was refused at client api 3.15 / 3.21 / 3.25 with "SVG export requires API version
  3.29 or later" and accepted at 3.29;
* `?resolution=high` and `/pdf` answered 200 at every one of those client versions;
* a workbook whose sources are disconnected fails EVERY route with the same 400.

Findings 1, 2, 3 and 5 of the #405 review each have a named regression below.
"""

from __future__ import annotations

import json
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
GATEWAY = b"<error><summary>Gateway</summary><detail>gateway timeout</detail></error>"

SVG_BODY = b'<?xml version="1.0"?><svg width="264.848mm" height="211.931mm"><text>x</text></svg>'
PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
PDF_BODY = b"%PDF-1.4\n/MediaBox [0 0 822 672]\n"

SVG = ("image", "?format=svg")
PDF = ("pdf", "?type=Unspecified")
PNG = ("image", "?resolution=high")


def scripted(responses, content_types=None):
    """Fetcher keyed by ``(endpoint, query)``, or ``(endpoint, query, api)`` for a floor re-probe."""
    content_types = content_types or {}
    calls: list[tuple] = []

    def fetch(endpoint: str, query: str, api: str | None = None):
        calls.append((endpoint, query, api))
        key = (endpoint, query) if api is None else (endpoint, query, api)
        status, body = responses[key]
        return status, body, content_types.get((endpoint, query))

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


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


# ------------------------------------------------- finding 2: HTTP 200 is not proof of the format


@pytest.mark.parametrize(
    "kind,body",
    [("svg", SVG_BODY), ("pdf", PDF_BODY), ("png", PNG_BODY), ("svg", b"<svg xmlns='x'/>")],
)
def test_a_matching_payload_signature_is_accepted(kind, body):
    assert cap.format_matches(kind, body, None)[0] is True


def test_png_bytes_are_rejected_for_an_svg_request():
    """The on-prem trap: an old server ignores `format=svg` and returns its default PNG."""
    ok, why = cap.format_matches("svg", PNG_BODY, None)
    assert ok is False
    assert "expected svg payload, got png" in why


def test_a_mismatched_content_type_is_rejected_even_when_bytes_look_right():
    ok, why = cap.format_matches("svg", SVG_BODY, "image/png")
    assert ok is False
    assert "Content-Type" in why


def test_a_missing_content_type_does_not_fail_a_correct_payload():
    """Proxies drop headers; the payload signature is the authoritative check."""
    assert cap.format_matches("pdf", PDF_BODY, None)[0] is True
    assert cap.format_matches("pdf", PDF_BODY, "application/pdf; charset=binary")[0] is True


def test_a_200_carrying_the_wrong_format_is_indeterminate_not_available():
    verdict, detail = cap.classify_probe(200, PNG_BODY, kind="svg", content_type="image/png")
    assert verdict == "indeterminate"
    assert "HTTP 200 but" in detail


def test_the_ladder_skips_a_rung_that_returned_the_wrong_format():
    fetch = scripted({SVG: (200, PNG_BODY), PDF: (200, PDF_BODY)})
    report = cap.detect(fetch, "l", cap.ApiVersions("3.29", "3.30"))
    assert report["selected_tier"] == "pdf"
    assert report["tiers"][0]["verdict"] == "indeterminate"


# ------------------------------------------------- finding 5: secrets must never reach the report


def test_the_probe_detail_is_redacted_before_it_can_be_serialised():
    """A proxy echoing X-Tableau-Auth puts a LIVE token in an error body that we write to disk."""
    token = "SYNTHETIC_SESSION_TOKEN_123"
    body = f"<error><detail>echo X-Tableau-Auth: {token}</detail></error>".encode()
    report = cap.detect(
        scripted({SVG: (401, body), PDF: (401, body), PNG: (401, body)}),
        "l",
        cap.ApiVersions("3.29", "3.30"),
        redactor=lambda text: text.replace(token, "[REDACTED]"),
    )
    assert token not in json.dumps(report)
    assert "[REDACTED]" in json.dumps(report)


def test_classification_reads_the_RAW_body_while_reporting_the_redacted_one():
    """Redaction must never mutate syntax control flow depends on -- a short PAT name mangles codes."""
    body = b"<error><detail>SVG export requires API version 3.29 or later. tok=SECRET</detail></error>"
    verdict, detail = cap.classify_probe(400, body, kind="svg", redactor=lambda t: t.replace("SVG", "XX"))
    assert verdict == "unsupported"  # classified from the raw text, which still says "SVG"
    assert "XX export requires" in detail  # reported from the redacted copy


# ------------------------------------------------- finding 3: indeterminate must stay indeterminate


def test_a_version_gate_is_a_permanent_no_for_that_tier():
    assert cap.classify_probe(400, SVG_TOO_OLD, kind="svg")[0] == "unsupported"


def test_a_disconnected_source_says_nothing_about_capability():
    assert cap.classify_probe(400, DISCONNECTED, kind="svg")[0] == "indeterminate"


def test_a_dead_token_is_indeterminate_not_unsupported():
    assert cap.classify_probe(401, DEAD_TOKEN, kind="svg")[0] == "indeterminate"


def test_a_transient_failure_above_the_winner_makes_the_selection_provisional():
    """SVG 504 + PDF 200 must not read as a settled 'this site does pdf'."""
    report = cap.detect(scripted({SVG: (504, GATEWAY), PDF: (200, PDF_BODY)}), "l", cap.ApiVersions("3.29", "3.30"))
    assert report["selected_tier"] == "pdf"
    assert report["provisional"] is True
    assert report["capability_complete"] is False
    assert any("PROVISIONAL" in w for w in report["warnings"])


def test_a_clean_win_is_not_provisional():
    report = cap.detect(scripted({SVG: (200, SVG_BODY)}), "l", cap.ApiVersions("3.29", "3.30"))
    assert report["selected_tier"] == "svg"
    assert report["provisional"] is False
    assert report["capability_complete"] is True
    assert report["warnings"] == []


def test_a_version_gate_plus_a_blocked_view_is_UNDETERMINED_not_incapable():
    """Mixed gate + blocked routes leave the answer UNKNOWN. This is finding 3's second shape."""
    report = cap.detect(
        scripted({SVG: (400, SVG_TOO_OLD), PDF: (400, DISCONNECTED), PNG: (400, DISCONNECTED)}),
        "l",
        cap.ApiVersions("3.21", "3.21"),
    )
    assert report["selected_tier"] is None
    assert any("UNDETERMINED" in w for w in report["warnings"])
    assert not any("no render tier is available" in w for w in report["warnings"])


def test_only_a_definitive_refusal_of_every_tier_reports_no_capability():
    report = cap.detect(
        scripted({SVG: (400, SVG_TOO_OLD), PDF: (400, SVG_TOO_OLD), PNG: (400, SVG_TOO_OLD)}),
        "l",
        cap.ApiVersions("3.15", "3.15"),
    )
    assert report["selected_tier"] is None
    assert report["capability_complete"] is True
    assert any("no render tier is available" in w for w in report["warnings"])


def test_an_all_indeterminate_walk_reports_UNDETERMINED():
    report = cap.detect(
        scripted({SVG: (400, DISCONNECTED), PDF: (400, DISCONNECTED), PNG: (400, DISCONNECTED)}),
        "l",
        cap.ApiVersions("3.29", "3.30"),
    )
    assert report["selected_tier"] is None
    assert report["capability_complete"] is False
    assert any("UNDETERMINED" in w for w in report["warnings"])


# ------------------------------------------------- finding 1: prove the pin claim, never infer it


def test_a_version_gated_tier_is_reprobed_at_its_documented_floor():
    """'The server advertises 3.29' is not the claim 'SVG works here'. Measure the second."""
    fetch = scripted({SVG: (400, SVG_TOO_OLD), (*SVG, "3.29"): (200, SVG_BODY)})
    report = cap.detect(fetch, "l", cap.ApiVersions("3.21", "3.30"))
    assert ("image", "?format=svg", "3.29") in fetch.calls
    assert report["tiers"][0]["floor_reprobe"]["verdict"] == "available"
    # The re-probe does not merely inform the warning -- it recovers the tier.
    assert report["selected_tier"] == "svg"
    assert any("proved by re-probing at API 3.29" in w for w in report["warnings"])


def test_an_advertised_but_absent_feature_is_never_claimed_as_supported():
    """The case that could not be verified: on-prem 2026.2 advertising 3.29 where SVG never shipped."""
    fetch = scripted({SVG: (400, SVG_TOO_OLD), (*SVG, "3.29"): (400, SVG_TOO_OLD), PDF: (200, PDF_BODY)})
    report = cap.detect(fetch, "l", cap.ApiVersions("3.21", "3.29"))
    assert report["selected_tier"] == "pdf"
    assert not any("supported by this server" in w or "WORKS on this server" in w for w in report["warnings"])
    assert any("UNKNOWN" in w for w in report["warnings"])


def test_no_floor_reprobe_when_the_server_itself_is_below_the_floor():
    """An on-prem 2023.3 server is not misconfigured; an extra metered call would learn nothing."""
    fetch = scripted({SVG: (400, SVG_TOO_OLD), PDF: (200, PDF_BODY)})
    report = cap.detect(fetch, "l", cap.ApiVersions("3.21", "3.21"))
    assert all(call[2] is None for call in fetch.calls)
    assert not any("pinned to" in w for w in report["warnings"])


def test_no_floor_reprobe_when_the_server_version_is_unknown():
    """serverinfo failing must not manufacture a confident configuration accusation."""
    fetch = scripted({SVG: (400, SVG_TOO_OLD), PDF: (200, PDF_BODY)})
    report = cap.detect(fetch, "l", cap.ApiVersions("3.21", None))
    assert all(call[2] is None for call in fetch.calls)
    assert not any("pinned to" in w for w in report["warnings"])


def test_the_report_carries_both_version_strings_so_a_reader_can_see_them_disagree():
    report = cap.detect(scripted({SVG: (200, SVG_BODY)}), "l", cap.ApiVersions("3.29", "3.30"))
    assert report["configured_api_version"] == "3.29"
    assert report["advertised_api_version"] == "3.30"
    assert report["probe_view_luid"] == "l"


def test_lower_rungs_are_not_probed_once_a_tier_answers_cleanly():
    fetch = scripted({SVG: (200, SVG_BODY)})
    report = cap.detect(fetch, "l", cap.ApiVersions("3.29", "3.30"))
    assert fetch.calls == [("image", "?format=svg", None)]
    assert [t["verdict"] for t in report["tiers"]] == ["available", "not_probed", "not_probed"]
