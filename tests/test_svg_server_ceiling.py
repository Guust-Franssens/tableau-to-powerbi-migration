"""
purpose: gate the three-state contract for "why did `?format=svg` fail" (#474) -- the message this
         repo prints, the `remedy` it persists per view, and the render ceiling the ASSESSMENT now
         reports before anyone captures anything.
usage:   pytest tests/test_svg_server_ceiling.py -q

Why this exists
---------------
A customer on an on-prem Tableau Server (``productVersion 2025.3.3`` / ``restApiVersion 3.27``) was
told, in the loudest line of the run:

    Set TABLEAU_REST_API_VERSION=3.29 in .env and re-run

Their server's ceiling is **3.27**. Raising a *client* preference cannot make a 3.27 server export
SVG, so the one actionable-looking instruction in the run was false for them -- and the code that
printed it already had the advertised ceiling in scope and ignored it.

Every assertion below therefore names WHICH of the three states fired, never merely that "a warning
was emitted". This codebase is fail-closed-heavy: several guards can satisfy a broad "it refused"
assertion at once, and a test that cannot distinguish them is credited as coverage while covering
nothing. Each state also carries a **negative control** -- a case that must NOT produce it.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import assess_estate  # noqa: E402  # pylint: disable=wrong-import-position
import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_oracle_manifest as verdict  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_render_capability as capability  # noqa: E402  # pylint: disable=wrong-import-position

# The customer site behind #474: below the SVG floor, and by only two minor versions -- which is the
# point. It is not an ancient server, it is a current on-prem one.
SES = {"product_version": "2025.3.3", "rest_api_version": "3.27"}
# The Cloud site this repo measured on 2026-08-30. Above the floor.
CLOUD = {"product_version": "2026.3.0", "rest_api_version": "3.30"}
# A server sitting EXACTLY on the SVG floor -- Tableau Server 2026.2, which advertises 3.29 and
# nothing higher. Its ceiling and the floor are the same number, so it is the site where "3.29 or
# later" and "3.29" stop being the same instruction (#475 review, finding 1).
EXACT_FLOOR = {"product_version": "2026.2.0", "rest_api_version": "3.29"}

# The server's own refusal text below 3.29, verbatim.
SVG_TOO_OLD = (
    "<error code='400000'><summary>Bad Request</summary><detail>SVG export requires API version 3.29 "
    "or later. Please upgrade your API version to use this feature.. (0x5CE10192 : SVG export requires "
    "API version 3.29 or later.)</detail></error>"
)

# Substrings that identify ONE branch each. Asserting on these rather than on "a warning fired" is the
# measured countermeasure in this repo: a broad assertion is satisfied by whichever guard happens to
# run first, so a collapsed branch still reads green.
#
# ⚠️ `RAISE_THE_PIN` names the FLOOR EXACTLY, and that literal is the fix for the #475 review's first
# finding. It used to read "set it to 3.29 or later in .env", which on a server advertising exactly
# 3.29 -- what Server 2026.2 reports -- recommends a range whose every other member is above that
# server's own ceiling: the impossible configuration #474 exists to remove, one case in from the edge.
RAISE_THE_PIN = "set it to exactly 3.29 in .env"
# The wording that was wrong. Kept as an explicit forbidden marker rather than deleted, because "the
# new phrase is present" does not by itself exclude the old one coming back beside it.
UNBOUNDED_PIN = "or later in .env"
SERVER_CANNOT = "at any client setting"
NOT_ESTABLISHED = "was NOT established on this run"
ALREADY_PINNED = "raising the pin further will not help"


def advice(server: dict | None, configured: str | None = "3.21", **gate_kwargs) -> capability.SvgGateAdvice:
    """The verdict for one site, assembled the way `write_manifest` assembles it."""
    return capability.svg_gate_advice(
        capability.SvgGate(
            advertised=(server or {}).get("rest_api_version"),
            configured=configured,
            product_version=(server or {}).get("product_version"),
            **gate_kwargs,
        )
    )


# --------------------------------------------------------------- state B: the server cannot, ever


def test_a_server_below_the_floor_is_classified_as_the_server_not_the_client_pin():
    assert advice(SES).cause == capability.SVG_CAUSE_SERVER_BELOW_FLOOR


def test_a_server_below_the_floor_is_NEVER_told_to_raise_the_client_pin():
    """The whole defect, in one assertion: this is the message the customer actually received."""
    remedy = advice(SES).remedy
    assert RAISE_THE_PIN not in remedy
    assert SERVER_CANNOT in remedy
    assert "raising TABLEAU_REST_API_VERSION above the server's advertised ceiling is not a fix" in remedy


def test_a_server_below_the_floor_quotes_ITS_numbers_and_the_floor_it_misses():
    """A customer thinks in releases, not API numbers, so both halves have to be there."""
    remedy = advice(SES).remedy
    assert "3.27" in remedy and "2025.3.3" in remedy
    assert "3.29" in remedy and "Server 2026.2" in remedy


def test_a_server_below_the_floor_is_routed_to_the_next_rung_that_works():
    remedy = advice(SES).remedy
    assert "PDF" in remedy and "2.8" in remedy
    assert "--pdf" in remedy and "--reference-best" in remedy


def test_the_server_cannot_branch_does_NOT_fire_for_a_site_above_the_floor():
    """Negative control. Without it, a mutation that always returns state B still reads green."""
    assert SERVER_CANNOT not in advice(CLOUD).remedy
    assert advice(CLOUD).cause != capability.SVG_CAUSE_SERVER_BELOW_FLOOR


# ------------------------------------------------------- state A: the server clears the floor


def test_a_server_at_or_above_the_floor_names_the_env_knob():
    result = advice(CLOUD)
    assert result.cause == capability.SVG_CAUSE_SERVER_MEETS_FLOOR
    assert RAISE_THE_PIN in result.remedy
    assert UNBOUNDED_PIN not in result.remedy


def test_exactly_at_the_floor_counts_as_meeting_it():
    """`supports` is >=, and an off-by-one here silently demotes every 2026.2 on-prem site.

    ⚠️ This test asserted ONLY the cause, and that was the hole. The #475 blind reviewer mutated the
    exact-floor arm's remedy text, watched the mutation execute (HITS=1), and watched this test and
    the above-floor one both stay green -- because neither could see a remedy. The cause is the
    classification; the remedy is what a customer acts on, and a suite that gates one and not the
    other is credited for coverage it does not have. Both are asserted here now.
    """
    result = advice(EXACT_FLOOR)
    assert result.cause == capability.SVG_CAUSE_SERVER_MEETS_FLOOR
    assert RAISE_THE_PIN in result.remedy


def test_an_exact_floor_server_is_NEVER_advised_above_its_own_advertised_ceiling():
    """Finding 1, in one assertion. `3.29` is the ceiling AND the floor -- every "later" is impossible.

    Reproduced by the reviewer against the production classifier: ``advertised="3.29"``,
    ``configured="3.21"`` returned *"set it to 3.29 or later in .env and re-run"*. Every value that
    phrase licenses above 3.29 exceeds what this server advertises, which is the same defect #474
    exists to eliminate -- it merely stopped being visible once the server met the floor.
    """
    remedy = advice(EXACT_FLOOR).remedy
    assert UNBOUNDED_PIN not in remedy
    assert "or later" not in remedy
    assert RAISE_THE_PIN in remedy


@pytest.mark.parametrize("advertised", ["3.29", "3.30", "9.99"])
def test_no_remedy_names_a_REST_VERSION_the_advertised_ceiling_cannot_serve(advertised):
    """The invariant behind finding 1, checked as a relationship rather than as a literal.

    A literal assertion ("says 3.29") is satisfied by a message that ALSO says something impossible;
    this extracts every REST-version-shaped token the remedy names and holds each one against the
    ceiling. It is scoped to `server_meets_floor` deliberately: that is the only state that issues a
    pin INSTRUCTION. State B names 3.29 too, but as the floor this server misses -- the opposite of a
    recommendation -- and state C names it inside an `if`.

    Release NAMES are excluded before scanning, because they are version-shaped and are not REST API
    versions: `release_for("3.29")` is *"Tableau Cloud June 2026 / Server 2026.2"*, whose `2026.2`
    would otherwise read as a REST version far above any ceiling.
    """
    result = advice({"rest_api_version": advertised}, configured="3.21")
    assert result.cause == capability.SVG_CAUSE_SERVER_MEETS_FLOOR
    named = _rest_versions_named(result.remedy, exclude=[capability.release_for(advertised)])
    ceiling = capability.api_tuple(advertised)
    assert named, "a remedy that names no version at all cannot be instructing anyone"
    for token in named:
        assert capability.api_tuple(token) <= ceiling, f"{token} is above the advertised ceiling {advertised}"
    # ...and the floor is actually recommended, so a mutation that simply stops naming a version
    # cannot satisfy the loop above by vacuity.
    assert RAISE_THE_PIN in result.remedy


def _rest_versions_named(text: str, *, exclude: list[str]) -> set[str]:
    """Every REST-API-version-shaped token in ``text``, minus phrases that merely look like one."""
    for phrase in exclude:
        text = text.replace(phrase, " ")
    return set(re.findall(r"\b\d+\.\d+\b", text))


def test_the_advertised_number_alone_never_claims_the_tier_WORKS():
    """The discipline `_add_pin_warnings` already keeps: advertised is a CLAIM, a re-probe is proof."""
    remedy = advice(CLOUD).remedy
    assert "not proof that SVG works here" in remedy
    assert "PROVED" not in remedy


def test_a_floor_reprobe_that_answered_is_the_one_thing_that_may_claim_it_works():
    remedy = advice(CLOUD, proved_by_reprobe=True).remedy
    assert "PROVED the tier answers on this server" in remedy


def test_the_reprobe_proof_does_NOT_appear_without_a_reprobe():
    """Negative control for the sentence above."""
    assert "PROVED the tier answers on this server" not in advice(CLOUD).remedy


def test_a_pin_that_ALREADY_clears_the_floor_is_not_told_to_raise_it_further():
    """A second false remedy, one case over: the knob is already turned.

    Version comparison is numeric, never lexicographic -- ``"3.9" > "3.10"`` as strings -- so this
    also pins that `supports` is being used for the client half rather than a string compare.
    """
    result = advice(CLOUD, configured="3.30")
    assert ALREADY_PINNED in result.remedy
    assert RAISE_THE_PIN not in result.remedy


def test_the_already_pinned_branch_does_NOT_fire_for_a_pin_below_the_floor():
    """Negative control."""
    assert ALREADY_PINNED not in advice(CLOUD, configured="3.21").remedy


# --------------------------------------------------- state C: the ceiling was never established


def test_no_ceiling_at_all_is_its_own_state_not_a_fall_through_into_either_answer():
    result = advice(None)
    assert result.cause == capability.SVG_CAUSE_CEILING_NOT_ESTABLISHED
    assert NOT_ESTABLISHED in result.remedy


def test_the_unknown_state_gives_the_CONDITIONAL_never_a_confident_instruction():
    """`IF ... fixes it; if it advertises less, ...` -- both limbs, and neither asserted."""
    remedy = advice(None).remedy
    assert "IF this site advertises 3.29 or later" in remedy
    assert "if it advertises less, SVG is unavailable at any client setting" in remedy
    assert RAISE_THE_PIN not in remedy


def test_the_unknown_state_says_how_to_establish_the_ceiling():
    remedy = advice(None).remedy
    assert "--reference-best" in remedy
    assert "/serverinfo" in remedy


def test_the_unknown_state_does_NOT_fire_once_a_ceiling_is_known():
    """Negative control, both directions -- a mutation that always returns state C fails here."""
    assert NOT_ESTABLISHED not in advice(SES).remedy
    assert NOT_ESTABLISHED not in advice(CLOUD).remedy


def test_an_empty_string_ceiling_is_unknown_rather_than_below_the_floor():
    """`server_info` returns None for an unparsable body; a caller may hand through ''."""
    assert advice({"rest_api_version": ""}).cause == capability.SVG_CAUSE_CEILING_NOT_ESTABLISHED


# ------------------------- #475 review finding 2: a "version" that is not a version at all --------

# Every one of these produced a CONFIDENT verdict from the production functions before the fix,
# because `api_tuple` pulled arbitrary digit runs out of whatever it was handed. The first two are
# the reviewer's own rows and they failed in OPPOSITE directions -- `garbage-999` became `(999,)` and
# therefore "clears the SVG floor, best rung SVG", while `not-a-version` became `(0,)` and therefore
# "below every floor, no reference render reachable at all". "It fails safe" was never available as a
# defence: the same input class produced both the most optimistic and the most pessimistic answer.
NOT_A_VERSION = ["not-a-version", "garbage-999", "3.x", "3", "v3.29", "3.29-beta", "3.", ".29", "", "   "]


@pytest.mark.parametrize("value", NOT_A_VERSION)
def test_api_tuple_refuses_anything_that_is_not_an_api_version(value):
    assert capability.api_tuple(value) is None


def test_api_tuple_refuses_None_as_well_as_junk():
    assert capability.api_tuple(None) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3.29", (3, 29)), ("2.8", (2, 8)), ("3.30", (3, 30)), ("9.99", (9, 99)), ("3.29.1", (3, 29, 1))],
)
def test_api_tuple_accepts_a_real_version_including_one_the_published_table_never_heard_of(value, expected):
    """Negative control for the refusals above: a mutation that refuses everything fails here."""
    assert capability.api_tuple(value) == expected


def test_api_tuple_still_orders_numerically_rather_than_lexicographically():
    """The original reason this function exists -- `"3.9" > "3.10"` as strings -- must survive."""
    assert capability.api_tuple("3.9") < capability.api_tuple("3.10")


@pytest.mark.parametrize("value", NOT_A_VERSION)
def test_supports_reports_the_UNKNOWN_third_state_for_a_non_version(value):
    assert capability.supports(value, "3.29") is None


def test_supports_raises_when_OUR_OWN_floor_is_not_a_version():
    """A bad floor is a bug in the ladder, not a fact about a server, so it must not fail quiet.

    Returning `None` here would mark every rung unknown on every site and read as caution.
    """
    with pytest.raises(ValueError):
        capability.supports("3.29", "not-a-floor")


def test_a_numeric_but_UNPUBLISHED_future_version_stays_established_and_above_the_floor():
    """⚠️ The one way finding 2 must NOT be fixed: by requiring membership in `API_RELEASE`.

    That table is a release-NAME lookup, not the capability boundary, and it stops at 3.29 -- while a
    live Cloud site measured on 2026-08-30 already advertised 3.30. A membership test would classify
    every real server past the documentation as unassessable.
    """
    assert "9.99" not in capability.API_RELEASE
    assert capability.supports("9.99", "3.29") is True
    assert advice({"rest_api_version": "9.99"}).cause == capability.SVG_CAUSE_SERVER_MEETS_FLOOR


@pytest.mark.parametrize("value", ["not-a-version", "garbage-999", "3.x"])
def test_a_ceiling_that_is_not_a_version_is_the_unknown_state_never_a_confident_one(value):
    """The reviewer's three classifier rows, which used to land on two different confident causes."""
    result = advice({"rest_api_version": value})
    assert result.cause == capability.SVG_CAUSE_CEILING_NOT_ESTABLISHED
    assert NOT_ESTABLISHED in result.remedy
    assert RAISE_THE_PIN not in result.remedy
    # ...and the junk is never quoted back at the operator as though it were this site's ceiling.
    assert value not in result.remedy


# ------------------------------------------------- `/serverinfo` itself: status, then grammar


class _Serverinfo:
    """The slice of `http.client.HTTPResponse` that `tableau_http._request` touches."""

    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self._payload = payload
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        """`urllib.error.HTTPError` closes the body it was handed."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _serverinfo_xml(version: str) -> bytes:
    """A real `/serverinfo` body, trimmed to the three elements `server_info` parses."""
    return (
        b'<?xml version="1.0" encoding="UTF-8"?><tsResponse><serverInfo>'
        b'<productVersion build="20253.25.0904.1234">2025.3.3</productVersion>'
        + f"<restApiVersion>{version}</restApiVersion>".encode()
        + b"</serverInfo></tsResponse>"
    )


def _probe(monkeypatch, status: int, body: bytes, redactor=None) -> dict:
    """`server_info` over a scripted transport -- the real parse, the real status handling."""

    def fake_urlopen(request, timeout=None):  # noqa: ARG001
        if status == 200:
            return _Serverinfo(200, body)
        raise urllib.error.HTTPError(request.full_url, status, "nope", {}, _Serverinfo(status, body))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return capability.server_info("https://s", timeout=1, redactor=redactor or (lambda text: text))


@pytest.mark.parametrize(("status", "version"), [(500, "3.30"), (404, "3.27")])
def test_a_version_carried_by_an_UNSUCCESSFUL_serverinfo_is_not_a_ceiling(monkeypatch, status, version):
    """The parse used to run regardless of HTTP status, so an error page's body became the ceiling.

    Both rows were reproduced by the reviewer against the production function: a 500 body carrying
    `<restApiVersion>3.30</restApiVersion>` was read as REST 3.30, and a 404 carrying 3.27 as 3.27.
    A proxy error page and a cached body can both have that shape; neither is the server's own
    account of itself.
    """
    info = _probe(monkeypatch, status, _serverinfo_xml(version))
    assert info["rest_api_version"] is None
    assert info["product_version"] is None
    # The status IS the diagnostic and must survive -- that is what "NOT established" quotes.
    assert info["status"] == status


def test_a_SUCCESSFUL_serverinfo_is_still_read_normally(monkeypatch):
    """Negative control: a mutation that drops every version fails here rather than reading green."""
    info = _probe(monkeypatch, 200, _serverinfo_xml("3.27"))
    assert (info["rest_api_version"], info["product_version"]) == ("3.27", "2025.3.3")
    assert info["build"] == "20253.25.0904.1234"
    assert "invalid_rest_api_version" not in info


def test_a_200_reporting_something_that_is_not_a_version_yields_no_ceiling_but_keeps_the_evidence(monkeypatch):
    """Unknown, and it says WHAT came back -- an operator told only "unknown" re-runs the same probe."""
    info = _probe(monkeypatch, 200, _serverinfo_xml("garbage-999"))
    assert info["rest_api_version"] is None
    assert info["invalid_rest_api_version"] == "garbage-999"
    assert info["status"] == 200


def test_the_offending_version_text_goes_through_the_redaction_chokepoint(monkeypatch):
    """It is response-derived like every other quoted value, and it lands in a customer artifact."""
    token = "SYNTHETIC_SESSION_TOKEN_42_LONG_ENOUGH"
    info = _probe(monkeypatch, 200, _serverinfo_xml(token), redactor=lambda text: text.replace(token, "[REDACTED]"))
    assert info["rest_api_version"] is None
    assert token not in info["invalid_rest_api_version"]
    assert "[REDACTED]" in info["invalid_rest_api_version"]


def test_the_FREE_FORM_product_and_build_fields_are_redacted_at_the_parse_boundary(monkeypatch):
    """The round-2 High finding: the redactor was applied to one field out of four.

    `productVersion` and its `build` attribute are unconstrained -- a version number is what a
    well-behaved server puts there, not what the format requires -- and unlike `restApiVersion` they
    are never grammar-checked, so nothing else downstream can refuse them. They are redacted HERE
    rather than at the three places that print them, because a consumer-side fix protects the
    consumers that exist today and no others.
    """
    token = "SYNTHETIC_SESSION_TOKEN_42_LONG_ENOUGH"
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?><tsResponse><serverInfo>'
        + f'<productVersion build="{token}">{token}</productVersion>'.encode()
        + b"<restApiVersion>3.27</restApiVersion></serverInfo></tsResponse>"
    )
    info = _probe(monkeypatch, 200, body, redactor=lambda text: text.replace(token, "[REDACTED]"))
    assert token not in json.dumps(info)
    assert info["product_version"] == "[REDACTED]" and info["build"] == "[REDACTED]"
    # The probe still SUCCEEDS -- redaction is not refusal, and the ceiling is unaffected.
    assert info["rest_api_version"] == "3.27"


def test_an_ABSENT_product_version_stays_None_rather_than_becoming_an_empty_string(monkeypatch):
    """`redacted_note` maps a missing value onto `""`; "absent" and "empty" are different facts.

    Every consumer tests truthiness, so `""` would render the same -- but `assessment.json` would
    start claiming the server sent an empty product version, which it did not.
    """
    body = (
        b'<?xml version="1.0"?><tsResponse><serverInfo><restApiVersion>3.27</restApiVersion></serverInfo></tsResponse>'
    )
    info = _probe(monkeypatch, 200, body)
    assert info["product_version"] is None and info["build"] is None
    assert info["rest_api_version"] == "3.27"


def test_a_NUMERIC_credential_reflected_as_the_VERSION_is_redacted_but_still_yields_a_CEILING(monkeypatch):
    """The exemption that had to go: no response string leaves `server_info` unredacted, version included.

    `restApiVersion` used to be returned untransformed because it had passed a numeric grammar "no
    credential can satisfy". Nothing enforces that -- a Tableau session token has **no** validated
    shape (`assess_estate.Site` binds `creds["token"]` as it arrives) -- so a token that is literally
    `3.27` satisfies the grammar and was published as the site's ceiling on three surfaces.

    The fix is NOT a tighter grammar: that would narrow the overlap between credential shapes and
    version shapes while leaving the assumption in place. The raw value is compared **inside**
    `server_info` and the derived capability travels separately from the displayable text, so
    suppressing the number costs the printed digits and nothing else.
    """
    info = _probe(monkeypatch, 200, _serverinfo_xml("3.27"), redactor=lambda text: text.replace("3.27", "[REDACTED]"))
    assert info["rest_api_version"] == "[REDACTED]"
    assert info["rest_api_version_reflected"] is True
    # The capability is intact, and was derived from the RAW value rather than from the string above.
    assert info["ceiling_established"] is True
    assert info["rung_support"] == {"svg": False, "pdf": True, "png_high": True}


def test_the_SAME_probe_with_no_credential_to_reflect_is_untouched(monkeypatch):
    """Negative control: suppression fires only when the REDACTOR says so.

    A mutation that always redacts the version, or always sets `reflected`, fails here -- and so
    would a "fix" that suppressed the number unconditionally, which would cost every honest run the
    one number this block exists to report.
    """
    info = _probe(monkeypatch, 200, _serverinfo_xml("3.27"))
    assert info["rest_api_version"] == "3.27"
    assert info["rest_api_version_reflected"] is False
    assert info["rung_support"] == {"svg": False, "pdf": True, "png_high": True}


def test_the_derived_capability_never_carries_the_version_STRING(monkeypatch):
    """`rung_support` is what may be published when the value it came from may not be.

    Three booleans against three published floors: a consumer cannot reconstruct `3.27` from them.
    The residual -- that they narrow a suppressed version to `[2.8, 3.29)` -- is stated in
    `rung_support`'s docstring and in its redaction certification rather than hidden.
    """
    info = _probe(monkeypatch, 200, _serverinfo_xml("3.27"), redactor=lambda text: text.replace("3.27", "[REDACTED]"))
    support = info["rung_support"]
    assert set(support) == {tier.name for tier in capability.LADDER}
    assert all(isinstance(value, bool) for value in support.values())
    assert "3.27" not in json.dumps(info)


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [
        (None, {"svg": None, "pdf": None, "png_high": None}),
        ((2, 4), {"svg": False, "pdf": False, "png_high": False}),
        ((3, 29), {"svg": True, "pdf": True, "png_high": True}),
    ],
)
def test_rung_support_is_three_valued_exactly_like_supports(ceiling, expected):
    """An unknown ceiling stays unknown per rung -- the third state, carried in the new channel."""
    assert capability.rung_support(ceiling) == expected


def test_the_assessment_suppresses_the_RELEASE_NAME_with_the_number_it_names(monkeypatch):
    """`API_RELEASE` is a bijection, so a release name hands back the digits redaction removed."""
    ceiling = _ceiling(
        monkeypatch,
        {
            "status": 200,
            "rest_api_version": "[REDACTED]",
            "rest_api_version_reflected": True,
            "ceiling_established": True,
            "rung_support": {"svg": False, "pdf": True, "png_high": True},
            "product_version": "2025.3.3",
        },
    )
    lines = "\n".join(assess_estate._render_server_ceiling(ceiling))  # pylint: disable=protected-access
    assert ceiling["advertised_release"] is None
    assert "Tableau 2025.3" not in lines
    assert "matched a credential this run holds" in lines
    # The verdicts survive: this is a redaction, not a refusal.
    assert ceiling["established"] is True and ceiling["best_reference_render"] == "pdf"
    assert "| rung | route |" in lines


# ---------------------------------------------------------------- the three states are a partition


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        (SES, capability.SVG_CAUSE_SERVER_BELOW_FLOOR),
        (CLOUD, capability.SVG_CAUSE_SERVER_MEETS_FLOOR),
        (None, capability.SVG_CAUSE_CEILING_NOT_ESTABLISHED),
    ],
)
def test_no_state_emits_another_states_CONFIDENT_claim(server, expected):
    """No confident claim may be reachable from two states: that is how a false remedy travels.

    ⚠️ Scoped to *confident* claims deliberately. State C names both outcomes -- it has to, that is
    what a conditional is -- so it legitimately contains the words "unavailable at any client
    setting" inside an ``if``. What it must never contain is the bare instruction, which is asserted
    both here and, with its framing, in the state-C block above.
    """
    result = advice(server)
    assert result.cause == expected
    forbidden = {
        capability.SVG_CAUSE_SERVER_BELOW_FLOOR: (RAISE_THE_PIN, NOT_ESTABLISHED),
        capability.SVG_CAUSE_SERVER_MEETS_FLOOR: (SERVER_CANNOT, NOT_ESTABLISHED),
        capability.SVG_CAUSE_CEILING_NOT_ESTABLISHED: (RAISE_THE_PIN,),
    }[expected]
    for marker in forbidden:
        assert marker not in result.remedy


def test_the_floor_the_message_quotes_is_the_ladders_own_number():
    """Two literals for one floor is how a rung is raised in one place and left stale in the other."""
    assert verdict.SVG_MIN_API_VERSION == capability.TIER_BY_NAME["svg"].min_api


# ------------------------------------------------------- assembling the gate from what a run has


def test_the_ceiling_is_read_from_a_bare_serverinfo_when_there_is_no_capability_probe():
    """The plain `--svg` run: no probe report exists, and the ceiling must still be established."""
    gate = verdict.svg_gate(None, SES, "3.21")
    assert gate.advertised == "3.27"
    assert gate.product_version == "2025.3.3"


def test_the_ceiling_is_read_from_the_capability_report_when_there_is_one():
    report = {"advertised_api_version": "3.27", "server": SES}
    assert verdict.svg_gate(report, None, "3.21").advertised == "3.27"


def test_the_gate_is_unknown_when_neither_source_answered():
    assert verdict.svg_gate(None, None, "3.21").advertised is None


def test_a_floor_reprobe_recorded_by_the_ladder_reaches_the_gate():
    report = {
        "advertised_api_version": "3.30",
        "server": CLOUD,
        "tiers": [{"tier": "svg", "floor_reprobe": {"api": "3.29", "verdict": "available"}}],
    }
    assert verdict.svg_gate(report, None, "3.21").proved_by_reprobe is True


def test_a_floor_reprobe_that_did_NOT_answer_is_not_reported_as_proof():
    """Negative control: `indeterminate` is not `available`, and the difference is the whole claim."""
    report = {
        "advertised_api_version": "3.30",
        "server": CLOUD,
        "tiers": [{"tier": "svg", "floor_reprobe": {"api": "3.29", "verdict": "indeterminate"}}],
    }
    assert verdict.svg_gate(report, None, "3.21").proved_by_reprobe is False


# ------------------------------------------------------------- the manifest, which is the evidence


class _Counter:
    """Stands in for `run.session`. Carries a REAL redactor, like the other manifest suites."""

    reauth_count = 0
    retry_count = 0

    @staticmethod
    def redact_text(text: str) -> str:
        return text


def _write(records, tmp_path, capability_report=None, server_info=None, configured="3.21"):
    env = {"TABLEAU_SERVER_URL": "https://s", "TABLEAU_SITE": "site", "TABLEAU_REST_API_VERSION": configured}
    run = verdict.CaptureRun(_Counter(), env, tmp_path, 0.0, frozenset({"svg"}))
    code = verdict.write_manifest(records, run, capability_report, server_info)
    return code, json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))


def _stale_record():
    return {
        "view_name": "v",
        "data": {"status": "ok", "certification": "certified", "row_count": 1, "elapsed_sec": 0.1},
        "svg": {"status": verdict.SVG_UNSUPPORTED_STATUS, "detail": "SVG export requires API version 3.29"},
    }


def test_the_manifest_records_the_advertised_ceiling_beside_the_client_preference(tmp_path):
    """Three numbers, and the evidence file must hold the two it never carried."""
    _code, manifest = _write([_stale_record()], tmp_path, server_info=SES)
    assert manifest["rest_api_version"] == "3.21"
    assert manifest["advertised_rest_api_version"] == "3.27"
    assert manifest["server_product_version"] == "2025.3.3"


def test_an_unestablished_ceiling_is_null_in_the_manifest_never_an_invented_number(tmp_path):
    _code, manifest = _write([_stale_record()], tmp_path)
    assert manifest["advertised_rest_api_version"] is None
    assert manifest["server_product_version"] is None


def test_the_per_view_remedy_in_the_manifest_is_the_one_that_can_actually_work(tmp_path):
    """Defect 2: the false remedy was serialised PER VIEW into the run's evidence artifact."""
    _code, manifest = _write([_stale_record()], tmp_path, server_info=SES)
    leg = manifest["views"][0]["svg"]
    assert leg["cause"] == capability.SVG_CAUSE_SERVER_BELOW_FLOOR
    assert RAISE_THE_PIN not in leg["remedy"]
    assert SERVER_CANNOT in leg["remedy"]


def test_a_view_whose_ceiling_is_known_to_clear_the_floor_keeps_the_env_remedy(tmp_path):
    """Negative control for the test above: state A is still allowed to name the knob."""
    _code, manifest = _write([_stale_record()], tmp_path, server_info=CLOUD)
    leg = manifest["views"][0]["svg"]
    assert leg["cause"] == capability.SVG_CAUSE_SERVER_MEETS_FLOOR
    assert RAISE_THE_PIN in leg["remedy"]


def test_a_leg_that_failed_for_any_OTHER_reason_is_not_stamped_with_an_svg_verdict(tmp_path):
    """Negative control on the selector: only a version-gated SVG leg carries this vocabulary."""
    record = {
        "view_name": "v",
        "data": {"status": "ok", "certification": "certified", "row_count": 1, "elapsed_sec": 0.1},
        "svg": {"status": "failed", "detail": "data sources not connected"},
    }
    _code, manifest = _write([record], tmp_path, server_info=SES)
    assert "cause" not in manifest["views"][0]["svg"]
    assert "remedy" not in manifest["views"][0]["svg"]


def test_the_console_and_the_manifest_carry_the_SAME_remedy(tmp_path, caplog):
    """One wording, two surfaces. A reader who sees only one must reach the same conclusion."""
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        _code, manifest = _write([_stale_record()], tmp_path, server_info=SES)
    persisted = manifest["views"][0]["svg"]["remedy"]
    assert persisted in caplog.text
    assert capability.SVG_CAUSE_SERVER_BELOW_FLOOR in caplog.text


def test_the_console_line_states_the_png_pdf_floors_without_promising_a_post_change_state(tmp_path, caplog):
    """The surviving half of the old sentence: true of the ROUTES' floors, not of a future run."""
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        _write([_stale_record()], tmp_path, server_info=SES)
    assert "PNG and PDF do not depend on the SVG floor" in caplog.text
    assert "2.5 and 2.8" in caplog.text
    assert "unaffected" not in caplog.text


def test_nothing_claims_that_over_pinning_breaks_other_calls(tmp_path, caplog):
    """Plausible, documented by Tableau, and NOT measured here -- so it is not asserted anywhere."""
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        _code, manifest = _write([_stale_record()], tmp_path, server_info=SES)
    text = caplog.text + json.dumps(manifest)
    for claim in ("break", "will fail", "other calls", "rejects every"):
        assert claim not in text


def test_a_run_with_no_version_gated_leg_prints_no_svg_verdict_at_all(tmp_path, caplog):
    """Negative control: the classifier must not narrate on a clean run."""
    record = {
        "view_name": "v",
        "data": {"status": "ok", "certification": "certified", "row_count": 1, "elapsed_sec": 0.1},
        "svg": {"status": "ok"},
    }
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        _write([record], tmp_path, server_info=SES)
    assert "could not produce SVG" not in caplog.text


# ------------------------------------------------------------------ the capture-time record itself


class _Session(oracle.TableauSession):
    """Scripted HTTP layer keyed by the query string, as in `test_capture_tableau_oracle_svg.py`."""

    def __init__(self, responses: dict[str, tuple[int, bytes]]):
        super().__init__(
            oracle.SiteCredentials(
                base="https://example.online.tableau.com",
                site="site",
                pat_name="name",
                pat_secret="a-long-enough-secret",
                version="3.21",
            )
        )
        self.responses = responses
        self.paths: list[str] = []
        self.token, self.site_id = "tok", "sid"

    def _request(self, method, path, *, body=None, accept=None, authed=True, api=None, deadline=None):  # noqa: ARG002
        self.paths.append(path)
        bare_path = path.split("?")[0]
        for suffix, (status, payload) in self.responses.items():
            if bare_path.endswith(suffix) or suffix in path:
                return status, payload, {}
        raise AssertionError(f"unscripted path {path}")

    def sign_in(self):
        self.token, self.site_id = "tok", "sid"


VIEW = {"id": "eb00995d-1ff1-4a42-9ac9-28846f861d31", "name": "HR | Summary", "workbook": {"id": "wb"}}


def test_the_capture_time_record_admits_the_ceiling_is_not_in_scope_there(tmp_path):
    """`_capture_render` cannot know the site's ceiling, so it must not pretend to.

    This is state C by construction rather than by accident: the advertised number is a property of
    the SITE, and nothing at that call carries it. `write_manifest` upgrades it.
    """
    session = _Session({"/data": (200, b"a\n1\n"), "image?format=svg": (400, SVG_TOO_OLD.encode())})
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    assert record["svg"]["status"] == verdict.SVG_UNSUPPORTED_STATUS
    assert record["svg"]["cause"] == capability.SVG_CAUSE_CEILING_NOT_ESTABLISHED
    assert NOT_ESTABLISHED in record["svg"]["remedy"]


def test_the_capture_time_record_does_NOT_carry_a_confident_env_instruction(tmp_path):
    """Negative control: the pre-#474 record said "set TABLEAU_REST_API_VERSION=3.29 ... in .env"."""
    session = _Session({"/data": (200, b"a\n1\n"), "image?format=svg": (400, SVG_TOO_OLD.encode())})
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    assert RAISE_THE_PIN not in record["svg"]["remedy"]


# ------------------------------------------------------------- main(): does the ceiling get probed


def test_a_plain_svg_run_probes_serverinfo_so_the_cause_is_established(tmp_path, monkeypatch):
    """Defect 3: without `--reference-best` there was NO ceiling at all, so state C was the ceiling.

    `/serverinfo` is unauthenticated and costs no metered export call, so a plain `--svg` run can and
    now does establish it. This drives the real `main()`, so a mutation that drops the probe -- or
    drops it from the `write_manifest` call -- fails here rather than only in a unit.
    """
    session = _Session(
        {"/data": (200, b"a\n1\n"), "image?format=svg": (400, SVG_TOO_OLD.encode()), "/auth/signout": (204, b"")}
    )
    probed: list[str] = []

    def fake_server_info(base, **_kwargs):
        probed.append(base)
        return dict(SES, status=200)

    monkeypatch.setattr(sys, "argv", ["capture_tableau_oracle.py", "--out", str(tmp_path), "--svg"])
    monkeypatch.setattr(oracle, "resolve_env", lambda _path: dict(_ENV))
    monkeypatch.setattr(oracle, "require", lambda _env: None)
    monkeypatch.setattr(oracle, "TableauSession", lambda *_a, **_k: session)
    monkeypatch.setattr(oracle, "select_views", lambda *_a, **_k: ([VIEW], {"wb": "HR"}))
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", lambda *_a, **_k: None)
    monkeypatch.setattr(oracle.capability, "server_info", fake_server_info)

    oracle.main()

    assert probed == ["https://example.online.tableau.com"]
    manifest = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["advertised_rest_api_version"] == "3.27"
    leg = manifest["views"][0]["svg"]
    assert leg["cause"] == capability.SVG_CAUSE_SERVER_BELOW_FLOOR
    assert RAISE_THE_PIN not in leg["remedy"]


def test_a_run_that_asks_for_no_svg_does_not_pay_for_the_probe(tmp_path, monkeypatch):
    """Negative control on the probe's trigger: it is free, not weightless."""
    session = _Session({"/data": (200, b"a\n1\n"), "/auth/signout": (204, b"")})
    probed: list[str] = []
    monkeypatch.setattr(sys, "argv", ["capture_tableau_oracle.py", "--out", str(tmp_path)])
    monkeypatch.setattr(oracle, "resolve_env", lambda _path: dict(_ENV))
    monkeypatch.setattr(oracle, "require", lambda _env: None)
    monkeypatch.setattr(oracle, "TableauSession", lambda *_a, **_k: session)
    monkeypatch.setattr(oracle, "select_views", lambda *_a, **_k: ([VIEW], {"wb": "HR"}))
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", lambda *_a, **_k: None)
    monkeypatch.setattr(oracle.capability, "server_info", lambda base, **_k: probed.append(base))

    oracle.main()
    assert probed == []


_ENV = {
    "TABLEAU_SERVER_URL": "https://example.online.tableau.com",
    "TABLEAU_SITE": "site",
    "TABLEAU_PAT_NAME": "name",
    "TABLEAU_PAT_SECRET": "a-long-enough-secret",
    "TABLEAU_REST_API_VERSION": "3.21",
}


# --------------------------------------------------------- the assessment, where an operator looks


class _Site:
    """The two attributes `server_ceiling` reads off `assess_estate.Site`, plus its scrubber."""

    def __init__(self, base="https://s", version="3.21"):
        self.base = base
        self.version = version

    @staticmethod
    def scrub_text(text: str) -> str:
        return text


def _ceiling(monkeypatch, info, version="3.21"):
    monkeypatch.setattr(assess_estate.render_capability, "server_info", lambda *_a, **_k: info)
    return assess_estate.server_ceiling(_Site(version=version))


def _rung(ceiling, name):
    """One rung out of the verdict list, by tier name."""
    return next(r for r in ceiling["rungs"] if r["tier"] == name)


def test_the_assessment_reports_the_three_numbers_it_insists_are_different(monkeypatch):
    ceiling = _ceiling(monkeypatch, dict(SES, status=200))
    assert ceiling["client_api_version"] == "3.21"
    assert ceiling["advertised_api_version"] == "3.27"
    assert ceiling["best_reference_render"] == "pdf"


def test_the_assessment_translates_the_api_number_into_a_release_a_customer_knows(monkeypatch):
    assert _ceiling(monkeypatch, dict(SES, status=200))["advertised_release"] == "Tableau 2025.3"


def test_the_assessment_carries_a_VERDICT_PER_RUNG_not_two_numbers_to_do_arithmetic_on(monkeypatch):
    """The point of surfacing the version here is the IMPLICATION, read per rung, not the number."""
    ceiling = _ceiling(monkeypatch, dict(SES, status=200))
    assert [(r["tier"], r["verdict"]) for r in ceiling["rungs"]] == [
        ("svg", assess_estate.UNAVAILABLE),
        ("pdf", assess_estate.AVAILABLE),
        ("png_high", assess_estate.AVAILABLE),
    ]


def test_every_rung_carries_its_own_floor_and_route_so_a_consumer_never_re_derives_them(monkeypatch):
    svg = _rung(_ceiling(monkeypatch, dict(SES, status=200)), "svg")
    assert (svg["min_api"], svg["route"]) == ("3.29", "/image?format=svg")
    assert "Server 2026.2" in svg["min_release"]


def test_the_assessment_expects_svg_only_where_the_server_advertises_it(monkeypatch):
    """Negative control against the rung table above -- both directions of the same derivation."""
    ceiling = _ceiling(monkeypatch, dict(CLOUD, status=200))
    assert _rung(ceiling, "svg")["verdict"] == assess_estate.AVAILABLE
    assert ceiling["best_reference_render"] == "svg"


def test_a_site_below_every_rung_reports_no_reachable_render_rather_than_the_lowest_one(monkeypatch):
    """`best` is the best AVAILABLE rung, not the last one in the ladder."""
    ceiling = _ceiling(monkeypatch, {"status": 200, "product_version": "10.1", "rest_api_version": "2.4"})
    assert {r["verdict"] for r in ceiling["rungs"]} == {assess_estate.UNAVAILABLE}
    assert ceiling["best_reference_render"] is None


# ---------------------------------------------- state C in the ASSESSMENT: no rung verdict at all


def test_an_unestablished_ceiling_marks_every_rung_UNKNOWN_never_available(monkeypatch):
    """The highest-value refusal in this change, in the field a consumer actually reads.

    ⚠️ `unknown` is a value, not an absent key. A downstream tool that reads
    `verdict != "unavailable"` as "usable" must be made to see the third state in the SAME field --
    an unassessable state that reads as a clean one is the defect class this whole change exists for.
    """
    ceiling = _ceiling(monkeypatch, {"status": 0, "error": "URLError: unreachable"})
    assert ceiling["established"] is False
    assert {r["verdict"] for r in ceiling["rungs"]} == {assess_estate.UNKNOWN}
    assert ceiling["best_reference_render"] is None


def test_an_ESTABLISHED_ceiling_marks_no_rung_unknown(monkeypatch):
    """Negative control: a mutation that always returns UNKNOWN fails here."""
    for server in (SES, CLOUD):
        ceiling = _ceiling(monkeypatch, dict(server, status=200))
        assert assess_estate.UNKNOWN not in {r["verdict"] for r in ceiling["rungs"]}


@pytest.mark.parametrize("value", ["not-a-version", "garbage-999", "3.x"])
def test_a_ceiling_that_is_not_a_version_is_UNKNOWN_in_the_assessment_too(monkeypatch, value):
    """Finding 2 where an operator reads it. Before the fix these three disagreed with each other.

    Measured against the production path: `garbage-999` reported `established: True`, every rung
    `available` and `best_reference_render: "svg"`; `not-a-version` reported `established: True` and
    every rung `unavailable`; `3.x` reported `best_reference_render: "pdf"`. Three confident,
    mutually contradictory answers from three strings that are all simply not versions.

    `server_ceiling` is asserted here with a hand-assembled `info`, which is deliberate: it proves
    the assessment layer classifies for itself rather than inheriting `server_info`'s refusal, so a
    future caller cannot reintroduce the collapse by assembling the dict some other way.
    """
    ceiling = _ceiling(monkeypatch, {"status": 200, "rest_api_version": value, "product_version": "x"})
    assert ceiling["established"] is False
    assert ceiling["advertised_api_version"] is None
    assert {r["verdict"] for r in ceiling["rungs"]} == {assess_estate.UNKNOWN}
    assert ceiling["best_reference_render"] is None


def test_a_ceiling_from_an_UNSUCCESSFUL_probe_is_UNKNOWN_in_the_assessment_too(monkeypatch):
    """The other half of finding 2, end to end through the real transport rather than a stub info."""

    def fake_urlopen(request, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, _Serverinfo(500, _serverinfo_xml("3.30")))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ceiling = assess_estate.server_ceiling(_Site())
    assert ceiling["established"] is False
    assert ceiling["advertised_api_version"] is None
    assert ceiling["probe_status"] == 500
    assert {r["verdict"] for r in ceiling["rungs"]} == {assess_estate.UNKNOWN}


def test_a_future_unpublished_version_is_still_ESTABLISHED_in_the_assessment(monkeypatch):
    """Negative control for both tests above, and the deliberate non-goal restated at this layer."""
    ceiling = _ceiling(monkeypatch, {"status": 200, "rest_api_version": "9.99", "product_version": "2099.1"})
    assert ceiling["established"] is True
    assert ceiling["best_reference_render"] == "svg"
    assert assess_estate.UNKNOWN not in {r["verdict"] for r in ceiling["rungs"]}


def test_the_report_says_the_reported_version_was_not_a_version_rather_than_only_the_status(monkeypatch):
    """ "`/serverinfo` answered 200" beside "ceiling not established" reads as a bug in THIS tool."""
    ceiling = _ceiling(monkeypatch, {"status": 200, "invalid_rest_api_version": "garbage-999"})
    lines = "\n".join(assess_estate._render_server_ceiling(ceiling))  # pylint: disable=protected-access
    assert "was NOT established" in lines
    assert "garbage-999" in lines and "is not a REST API version" in lines
    assert "| rung | route |" not in lines


def test_the_report_does_NOT_invent_that_clause_when_the_probe_simply_did_not_answer(monkeypatch):
    """Negative control: a 0/404 says only what it knows -- there was no version to reject."""
    lines = "\n".join(
        assess_estate._render_server_ceiling(_ceiling(monkeypatch, {"status": 0, "error": "URLError"}))  # pylint: disable=protected-access
    )
    assert "is not a REST API version" not in lines
    assert "`/serverinfo` answered `0`" in lines


def test_the_console_also_names_the_value_that_was_not_a_version(monkeypatch, caplog):
    """One wording, two surfaces -- the console must not be the vaguer of the two."""
    ceiling = _ceiling(monkeypatch, {"status": 200, "invalid_rest_api_version": "garbage-999"})
    with caplog.at_level(logging.INFO, logger="assess"):
        assess_estate._log_server_ceiling(ceiling)  # pylint: disable=protected-access
    assert "NOT ESTABLISHED" in caplog.text
    assert "garbage-999" in caplog.text and "not a REST API version" in caplog.text
    assert "AVAILABLE" not in caplog.text


def test_the_report_prints_NO_rung_table_when_the_ceiling_was_not_established(monkeypatch):
    """A rung table from a ceiling nobody established is indistinguishable from a measured one."""
    ceiling = _ceiling(monkeypatch, {"status": 0, "error": "URLError: unreachable"})
    lines = "\n".join(assess_estate._render_server_ceiling(ceiling))  # pylint: disable=protected-access
    assert "was NOT established" in lines
    assert "No per-rung verdict is shown" in lines
    # The table header, the verdict vocabulary and the bottom line must ALL be absent -- asserting on
    # only one of them would pass a renderer that dropped the header and kept the verdicts.
    assert "| rung | route |" not in lines
    assert "Bottom line" not in lines
    for marker in ("available", "UNAVAILABLE on this server", "/image?format=svg", "REST `3.29`"):
        assert marker not in lines


def test_the_report_DOES_print_the_rung_table_when_the_ceiling_IS_established(monkeypatch):
    """Negative control for the refusal above: it must refuse only the unestablished case."""
    lines = "\n".join(assess_estate._render_server_ceiling(_ceiling(monkeypatch, dict(SES, status=200))))  # pylint: disable=protected-access
    assert "| rung | route |" in lines
    assert "No per-rung verdict is shown" not in lines
    assert "was NOT established" not in lines


def test_the_console_prints_NO_rung_verdict_when_the_ceiling_was_not_established(monkeypatch, caplog):
    """The terminal is the surface an operator sees first, so the same refusal has to hold there."""
    ceiling = _ceiling(monkeypatch, {"status": 0, "error": "URLError: unreachable"})
    with caplog.at_level(logging.INFO, logger="assess"):
        assess_estate._log_server_ceiling(ceiling)  # pylint: disable=protected-access
    assert "NOT ESTABLISHED" in caplog.text
    assert "UNKNOWN -- no rung verdict is shown" in caplog.text
    for marker in ("AVAILABLE", "needs REST 3.29", "reference-best should resolve to"):
        assert marker not in caplog.text


def test_the_console_DOES_print_a_rung_verdict_once_the_ceiling_is_established(monkeypatch, caplog):
    """Negative control for the console refusal."""
    with caplog.at_level(logging.INFO, logger="assess"):
        assess_estate._log_server_ceiling(_ceiling(monkeypatch, dict(SES, status=200)))  # pylint: disable=protected-access
    assert "needs REST 3.29" in caplog.text
    assert "reference-best should resolve to PDF" in caplog.text
    assert "no rung verdict is shown" not in caplog.text


# ------------------------------------------------------------- the report block, rendered per rung


def test_the_report_leads_with_the_implication_and_keeps_the_numbers_as_detail(monkeypatch):
    lines = "\n".join(assess_estate._render_server_ceiling(_ceiling(monkeypatch, dict(SES, status=200))))  # pylint: disable=protected-access
    assert "what we send" in lines and "what the server advertises" in lines
    assert "UNAVAILABLE on this server" in lines
    assert "Bottom line: `--reference-best` should resolve to `pdf`" in lines
    assert "at any client setting" in lines


def test_the_report_says_WHY_pdf_is_worth_preferring_when_svg_is_out_of_reach(monkeypatch):
    lines = "\n".join(assess_estate._render_server_ceiling(_ceiling(monkeypatch, dict(SES, status=200))))  # pylint: disable=protected-access
    assert "only **vector** rung available here" in lines


def test_the_report_does_NOT_claim_pdf_is_the_only_vector_rung_where_svg_also_works(monkeypatch):
    """Negative control: on a 3.30 site both vector rungs are reachable, so that claim is false."""
    lines = "\n".join(assess_estate._render_server_ceiling(_ceiling(monkeypatch, dict(CLOUD, status=200))))  # pylint: disable=protected-access
    assert "Bottom line: `--reference-best` should resolve to `svg`" in lines
    assert "only **vector** rung available here" not in lines
    assert "at any client setting" not in lines


def test_the_report_states_the_measured_PNG_CEILING_because_available_is_easy_to_over_trust(monkeypatch):
    """The other half of the implication, quoting this repo's own measurement, not a new one.

    `docs/reference-capture.md` records `?resolution=high` as **exactly 2x declared, 52/52**, with a
    650x800 dashboard capped at 1300x1600 forever. Both numbers appear, so a reviewer can trace them.
    """
    lines = "\n".join(assess_estate._render_server_ceiling(_ceiling(monkeypatch, dict(SES, status=200))))  # pylint: disable=protected-access
    assert "exactly 2× the dashboard's declared size" in lines
    assert "650×800" in lines and "1300×1600" in lines
    assert "structurally legible and content-illegible" in lines


def test_the_PNG_ceiling_is_scoped_to_DASHBOARDS_because_a_worksheet_honours_vizHeight(monkeypatch):
    """This repo already corrected the over-general version of this claim once; do not reintroduce it."""
    lines = "\n".join(assess_estate._render_server_ceiling(_ceiling(monkeypatch, dict(SES, status=200))))  # pylint: disable=protected-access
    assert "worksheet" in lines and "dashboard claim" in lines


def test_the_report_grades_its_own_verdicts_rather_than_presenting_them_as_measurements(monkeypatch):
    """`unavailable` is firm; `available` is a claim the endpoint has not been asked to honour."""
    lines = "\n".join(assess_estate._render_server_ceiling(_ceiling(monkeypatch, dict(SES, status=200))))  # pylint: disable=protected-access
    assert "derived from the **advertised** number" in lines
    assert "has not been asked to honour" in lines


def test_a_site_below_every_rung_is_told_no_server_reference_exists_at_all(monkeypatch):
    ceiling = _ceiling(monkeypatch, {"status": 200, "product_version": "10.1", "rest_api_version": "2.4"})
    lines = "\n".join(assess_estate._render_server_ceiling(ceiling))  # pylint: disable=protected-access
    assert "NO reference render rung is reachable" in lines
    assert "192×192" in lines and "layout-grade, never validation-grade" in lines
    # The raster ceiling is noise on a site that cannot call the raster rung either.
    assert "1300×1600" not in lines


def test_the_assessment_section_is_absent_when_no_probe_was_run():
    """`assemble` carries `None` through for an older raw payload; the report must not invent one."""
    assert assess_estate._render_server_ceiling(None) == []  # pylint: disable=protected-access


def test_the_ceiling_reaches_assessment_json_not_only_the_report(monkeypatch):
    """A programmatic consumer never opens report.md, so the verdicts have to be structured."""
    raw = {
        "workbooks": [],
        "views": [],
        "subscriptions": [],
        "alerts": [],
        "custom_views": [],
        "structure_by_name": {},
        "permissions": [],
        "groups": [],
        "survey": None,
        "server_ceiling": _ceiling(monkeypatch, dict(SES, status=200)),
    }
    assembled = assess_estate.assemble(raw, 0.99)
    ceiling = assembled["server_ceiling"]
    assert ceiling["advertised_api_version"] == "3.27"
    assert _rung(ceiling, "svg")["verdict"] == assess_estate.UNAVAILABLE
    assert ceiling["best_reference_render"] == "pdf"
