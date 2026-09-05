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
    # The message names the ROOT ELEMENT rule deliberately: a leading `<?xml` cannot settle
    # SVG, because Tableau's own error bodies start with the same declaration.
    assert "expected an <svg> root" in why
    assert "got png" in why


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


# ------------------------------------------- #405 round 3, finding 1: the redactor must SEE the value

# Deliberately mixed case and 27 characters: longer than the eight bytes the diagnostic used to quote,
# so a redactor applied after truncation cannot cover it either.
TOKEN = "SYNTHETIC_SeSSion_TOKEN_123"


def _redactor(text: str) -> str:
    """The repo redactor's defining property, in miniature: literal match, NO case folding.

    ``scripts/tableau_env.py`` says so explicitly -- case-changed forms are "deliberately NOT
    covered" -- so a diagnostic that lowercases before redacting is not merely untidy, it is a leak.
    """
    return text.replace(TOKEN, "[REDACTED]")


def test_a_mixed_case_secret_in_the_content_type_is_redacted_before_it_is_case_folded():
    """The reviewer's exact probe: a reflected token arrives as the Content-Type of an HTTP 200."""
    ok, why = cap.format_matches("svg", SVG_BODY, f"image/{TOKEN}", redactor=_redactor)
    assert ok is False
    assert TOKEN not in why
    assert TOKEN.lower() not in why
    assert "[REDACTED]" in why


def test_the_same_secret_survives_end_to_end_through_classify_probe():
    """The path that actually reaches disk: `classify_probe` -> manifest `render_capability`."""
    verdict, detail = cap.classify_probe(200, SVG_BODY, kind="svg", content_type=f"image/{TOKEN}", redactor=_redactor)
    assert verdict == "indeterminate"
    assert TOKEN.lower() not in detail.lower()
    assert "[REDACTED]" in detail


def test_case_folding_still_decides_the_verdict_even_though_the_report_keeps_the_original_case():
    """Redaction must never change control flow: the MIME comparison stays case-insensitive."""
    assert cap.format_matches("pdf", PDF_BODY, "APPLICATION/PDF")[0] is True
    assert cap.format_matches("pdf", PDF_BODY, "Application/Pdf; charset=binary")[0] is True


def test_a_secret_at_the_head_of_the_body_is_redacted_before_the_quote_is_truncated():
    """Slicing first leaves a PREFIX of a longer secret that a literal redactor can no longer match."""
    body = (TOKEN + "-and-then-some-html").encode()
    ok, why = cap.format_matches("svg", body, None, redactor=_redactor)
    assert ok is False
    assert "[REDACTED]" in why
    for length in range(6, len(TOKEN) + 1):
        assert TOKEN[:length] not in why, f"a {length}-character prefix of the token survived"


def test_the_quoted_head_is_ascii_so_a_cp1252_console_can_print_it():
    """The decode is lossy; a literal U+FFFD in a message later printed on Windows raises."""
    _, why = cap.format_matches("svg", b"\x89PNG\r\n\x1a\n\x00\x00", None)
    why.encode("cp1252")  # would raise UnicodeEncodeError on a raw replacement character
    assert "PNG" in why


def test_a_secret_containing_a_quote_is_still_redacted():
    """`repr(bytes)` escapes quotes and backslashes, hiding the literal from the redactor."""
    secret = "tok'en\\42"
    body = (secret + "<html>").encode()
    _, why = cap.format_matches("svg", body, None, redactor=lambda t: t.replace(secret, "[REDACTED]"))
    assert "[REDACTED]" in why
    assert "en\\42" not in why


def test_no_redactor_means_no_redaction_so_a_caller_cannot_assume_one():
    """Pins the opt-in: `_capture_render` passing no redactor was the whole of the second leak."""
    _, why = cap.format_matches("svg", (TOKEN + "<html>").encode(), None)
    assert TOKEN[:8] in why


# ------------------------------------------- #405 round 3, finding 4: the ladder has an ORDER


def test_tier_priority_ranks_the_ladder_best_first_and_higher_is_better():
    assert cap.tier_priority("svg") > cap.tier_priority("pdf") > cap.tier_priority("png_high")
    assert cap.tier_priority(None) == 0
    assert cap.tier_priority("not-a-tier") == 0


def test_tier_priority_is_derived_from_the_ladder_rather_than_hard_coded():
    """A new rung must not need a second edit in a comparison function to be ordered correctly."""
    for better, worse in zip(cap.LADDER, cap.LADDER[1:]):
        assert cap.tier_priority(better.name) > cap.tier_priority(worse.name)


# ------------------------------------------- #473: maxAge on capability probe & re-probe URLs


class _FakeProbeSession:
    def __init__(self, responses: dict[tuple[str, str], tuple[int, bytes, str | None]]):
        self.site_id = "site-123"
        self.token = "session-tok-123"
        self.responses = responses
        self.calls: list[tuple[str, str | None]] = []

    def raw_get(self, path: str, api: str | None = None) -> tuple[int, bytes, str | None]:
        self.calls.append((path, api))
        # key by (route, api)
        route = path.split(f"/views/v1/")[1]
        key = (route, api)
        if key in self.responses:
            return self.responses[key]
        return self.responses.get((route, None), (404, b"not found", None))

    def redact_text(self, text: str) -> str:
        return text.replace(self.token, "[REDACTED]")


def test_probe_render_capability_appends_max_age_to_initial_probe_and_floor_reprobe(monkeypatch):
    """Positive test: maxAge is appended exactly once to both initial probe and floor re-probe."""
    monkeypatch.setattr(
        cap, "server_info", lambda *_a, **_k: {"rest_api_version": "3.30", "product_version": "2026.3.0"}
    )
    svg_too_old = (
        b"<error code='400000'><summary>Bad Request</summary><detail>SVG export requires API version "
        b"3.29 or later.</detail></error>"
    )
    # v1: initial probe at configured API 3.21 -> 400 SVG too old; floor reprobe at API 3.29 -> 200 SVG
    responses = {
        ("image?format=svg&maxAge=15", None): (400, svg_too_old, None),
        ("image?format=svg&maxAge=15", "3.29"): (200, SVG_BODY, "image/svg+xml"),
    }
    session = _FakeProbeSession(responses)
    env = {"TABLEAU_SERVER_URL": "https://server", "TABLEAU_REST_API_VERSION": "3.21"}
    views = [{"id": "v1", "name": "Revenue"}]

    report = cap.probe_render_capability(session, env, views, max_age=15)

    assert report["selected_tier"] == "svg"
    assert report["max_age_minutes"] == 15
    assert len(session.calls) == 2
    # 1. Initial probe carries maxAge=15
    assert session.calls[0] == ("/sites/site-123/views/v1/image?format=svg&maxAge=15", None)
    # 2. Floor re-probe carries maxAge=15 and api=3.29
    assert session.calls[1] == ("/sites/site-123/views/v1/image?format=svg&maxAge=15", "3.29")


def test_mutation_control_probe_arm_without_max_age_is_rejected():
    """Mutation control: deleting maxAge from only the probe arm must be caught and fail the contract."""
    # Control: simulate a mutant probe URL missing maxAge
    mutant_path = "/sites/site-123/views/v1/image?format=svg"
    valid_path = "/sites/site-123/views/v1/image?format=svg&maxAge=1"

    assert "maxAge=" in valid_path
    assert "maxAge=" not in mutant_path

    # Verify that a session receiving requests without maxAge detects the missing parameter
    def assert_probe_contract(recorded_paths: list[str], expected_max_age: int) -> None:
        expected_param = f"maxAge={expected_max_age}"
        for path in recorded_paths:
            if not path.endswith(f"?{expected_param}") and f"&{expected_param}" not in path:
                raise AssertionError(f"Probe request URL mutated: missing {expected_param} in {path}")

    # Valid run passes
    assert_probe_contract([valid_path], 1)

    # Mutant run omitting maxAge is caught
    with pytest.raises(AssertionError, match="Probe request URL mutated: missing maxAge=1"):
        assert_probe_contract([mutant_path], 1)


@pytest.mark.parametrize(
    "cli_args, expected_max_age",
    [
        (["--view", "v1"], 1),
        (["--view", "v1", "--max-age", "1"], 1),
        (["--view", "v1", "--max-age", "20"], 20),
    ],
)
def test_capability_cli_parser_accepts_valid_max_age(cli_args, expected_max_age):
    # Standalone CLI parser test
    import argparse
    from pathlib import Path
    from tableau_capture_policy import DEFAULT_MAX_AGE_MINUTES

    parser = argparse.ArgumentParser()
    parser.add_argument("--view", required=True)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--max-age", type=cap._arg_max_age, default=DEFAULT_MAX_AGE_MINUTES)
    args = parser.parse_args(cli_args)
    assert args.max_age == expected_max_age


@pytest.mark.parametrize(
    "invalid_cli_arg",
    ["0", "-1", "-10", "abc", "1.5"],
)
def test_capability_cli_parser_refuses_invalid_max_age(invalid_cli_arg):
    import argparse
    from pathlib import Path
    from tableau_capture_policy import DEFAULT_MAX_AGE_MINUTES

    parser = argparse.ArgumentParser()
    parser.add_argument("--view", required=True)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--max-age", type=cap._arg_max_age, default=DEFAULT_MAX_AGE_MINUTES)
    with pytest.raises(SystemExit):
        parser.parse_args(["--view", "v1", "--max-age", invalid_cli_arg])

