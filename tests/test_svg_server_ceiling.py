"""purpose: gate the three-state contract for "why did `?format=svg` fail" (#474) -- the message this repo prints,
the `remedy` it persists per view, and the render ceiling the ASSESSMENT now reports before anyone captures
anything. usage:   pytest tests/test_svg_server_ceiling.py -q"""

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


def test_a_server_below_the_floor_is_told_the_server_cannot_never_to_raise_the_client_pin():
    """The whole defect: this is the message the customer actually received."""
    result = advice(SES)
    assert result.cause == capability.SVG_CAUSE_SERVER_BELOW_FLOOR
    remedy = result.remedy
    assert RAISE_THE_PIN not in remedy and NOT_ESTABLISHED not in remedy
    assert SERVER_CANNOT in remedy
    assert "raising TABLEAU_REST_API_VERSION above the server's advertised ceiling is not a fix" in remedy
    assert "3.27" in remedy and "2025.3.3" in remedy
    assert "3.29" in remedy and "Server 2026.2" in remedy
    assert "PDF" in remedy and "2.8" in remedy
    assert "--pdf" in remedy and "--reference-best" in remedy


# ------------------------------------------------------- state A: the server clears the floor


def test_a_server_above_the_floor_names_the_env_knob_but_never_claims_the_tier_works():
    result = advice(CLOUD)
    assert result.cause == capability.SVG_CAUSE_SERVER_MEETS_FLOOR
    assert RAISE_THE_PIN in result.remedy
    for marker in (UNBOUNDED_PIN, SERVER_CANNOT, NOT_ESTABLISHED, ALREADY_PINNED, "PROVED"):
        assert marker not in result.remedy
    assert "not proof that SVG works here" in result.remedy
    assert "PROVED the tier answers on this server" in advice(CLOUD, proved_by_reprobe=True).remedy


def test_an_exact_floor_server_is_NEVER_advised_above_its_own_advertised_ceiling():
    """#475 finding 1: `3.29` is the ceiling AND the floor, so every "later" is impossible. Every REST-version-shaped
    token the remedy names is held against the ceiling."""
    result = advice(EXACT_FLOOR)
    assert result.cause == capability.SVG_CAUSE_SERVER_MEETS_FLOOR
    assert RAISE_THE_PIN in result.remedy and "or later" not in result.remedy
    text = result.remedy.replace(capability.release_for("3.29"), " ")
    named = set(re.findall(r"\b\d+\.\d+\b", text))
    assert named and all(capability.api_tuple(token) <= (3, 29) for token in named)


def test_a_pin_that_ALREADY_clears_the_floor_is_not_told_to_raise_it_further():
    """Compared numerically: ``"3.9" > "3.10"`` as strings."""
    result = advice(CLOUD, configured="3.30")
    assert ALREADY_PINNED in result.remedy
    assert RAISE_THE_PIN not in result.remedy


# --------------------------------------------------- state C: the ceiling was never established


@pytest.mark.parametrize("server", [None, {"rest_api_version": ""}, {"rest_api_version": "garbage-999"}])
def test_no_ceiling_is_its_own_state_with_a_CONDITIONAL_never_a_confident_instruction(server):
    result = advice(server)
    assert result.cause == capability.SVG_CAUSE_CEILING_NOT_ESTABLISHED
    remedy = result.remedy
    assert NOT_ESTABLISHED in remedy
    assert "IF this site advertises 3.29 or later" in remedy
    assert "if it advertises less, SVG is unavailable at any client setting" in remedy
    assert "--reference-best" in remedy and "/serverinfo" in remedy
    assert RAISE_THE_PIN not in remedy and "garbage-999" not in remedy


# ------------------------- #475 review finding 2: a "version" that is not a version at all --------

# `garbage-999` once became `(999,)` ("clears the floor") and `not-a-version` `(0,)` ("below every
# floor"): the same input class produced both the most optimistic and the most pessimistic answer.
NOT_A_VERSION = [None, "", "garbage-999", "v3.29", "3.29-beta", "3", "3.", ".29"]


@pytest.mark.parametrize("value", NOT_A_VERSION)
def test_api_tuple_refuses_anything_that_is_not_an_api_version(value):
    assert capability.api_tuple(value) is None


def test_api_tuple_accepts_real_versions_and_orders_them_numerically():
    assert capability.api_tuple("3.29") == (3, 29)
    assert capability.api_tuple("3.29.1") == (3, 29, 1)
    assert capability.api_tuple("3.9") < capability.api_tuple("3.10")


def test_supports_is_three_valued_and_raises_on_OUR_OWN_bad_floor():
    """A bad floor is a ladder bug; returning `None` would mark every rung unknown and read as caution."""
    assert capability.supports("garbage-999", "3.29") is None
    with pytest.raises(ValueError):
        capability.supports("3.29", "not-a-floor")


def test_a_numeric_but_UNPUBLISHED_future_version_stays_established_and_above_the_floor():
    """`API_RELEASE` is a release-NAME lookup, not the capability boundary: never require membership."""
    assert "9.99" not in capability.API_RELEASE
    assert capability.supports("9.99", "3.29") is True
    assert advice({"rest_api_version": "9.99"}).cause == capability.SVG_CAUSE_SERVER_MEETS_FLOOR


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


def test_a_version_carried_by_an_UNSUCCESSFUL_serverinfo_is_not_a_ceiling(monkeypatch):
    """A proxy error page carrying `<restApiVersion>` is not the server's own account of itself."""
    info = _probe(monkeypatch, 500, _serverinfo_xml("3.30"))
    assert info["rest_api_version"] is None and info["product_version"] is None
    assert info["status"] == 500


def test_a_SUCCESSFUL_serverinfo_is_read_and_left_untouched_without_a_reflected_credential(monkeypatch):
    info = _probe(monkeypatch, 200, _serverinfo_xml("3.27"))
    assert (info["rest_api_version"], info["product_version"]) == ("3.27", "2025.3.3")
    assert info["build"] == "20253.25.0904.1234"
    assert "invalid_rest_api_version" not in info
    assert info["rest_api_version_reflected"] is False
    assert info["rung_support"] == {"svg": False, "pdf": True, "png_high": True}


def test_a_200_non_version_yields_no_ceiling_but_keeps_its_REDACTED_evidence(monkeypatch):
    token = "SYNTHETIC_SESSION_TOKEN_42_LONG_ENOUGH"
    info = _probe(monkeypatch, 200, _serverinfo_xml(token), redactor=lambda text: text.replace(token, "[REDACTED]"))
    assert info["rest_api_version"] is None and info["status"] == 200
    assert info["invalid_rest_api_version"] == "[REDACTED]"


def test_the_FREE_FORM_product_and_build_fields_are_redacted_at_the_parse_boundary(monkeypatch):
    """They are never grammar-checked, so nothing downstream can refuse them."""
    token = "SYNTHETIC_SESSION_TOKEN_42_LONG_ENOUGH"
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?><tsResponse><serverInfo>'
        + f'<productVersion build="{token}">{token}</productVersion>'.encode()
        + b"<restApiVersion>3.27</restApiVersion></serverInfo></tsResponse>"
    )
    info = _probe(monkeypatch, 200, body, redactor=lambda text: text.replace(token, "[REDACTED]"))
    assert token not in json.dumps(info)
    assert info["product_version"] == "[REDACTED]" and info["build"] == "[REDACTED]"
    assert info["rest_api_version"] == "3.27"


def test_an_ABSENT_product_version_stays_None_rather_than_becoming_an_empty_string(monkeypatch):
    body = (
        b'<?xml version="1.0"?><tsResponse><serverInfo><restApiVersion>3.27</restApiVersion></serverInfo></tsResponse>'
    )
    info = _probe(monkeypatch, 200, body)
    assert info["product_version"] is None and info["build"] is None
    assert info["rest_api_version"] == "3.27"


def test_a_NUMERIC_credential_reflected_as_the_VERSION_is_redacted_but_still_yields_a_CEILING(monkeypatch):
    """A session token has no validated shape, so one that is literally `3.27` satisfies the grammar."""
    info = _probe(monkeypatch, 200, _serverinfo_xml("3.27"), redactor=lambda text: text.replace("3.27", "[REDACTED]"))
    assert info["rest_api_version"] == "[REDACTED]"
    assert info["rest_api_version_reflected"] is True
    assert info["ceiling_established"] is True
    assert info["rung_support"] == {"svg": False, "pdf": True, "png_high": True}
    assert "3.27" not in json.dumps(info)
    assert capability.rung_support(None) == {"svg": None, "pdf": None, "png_high": None}


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
    assert ceiling["established"] is True and ceiling["best_reference_render"] == "pdf"
    assert "| rung | route |" in lines


# ------------------------------------------------------- assembling the gate from what a run has


def test_the_gate_reads_the_ceiling_from_whichever_source_answered():
    gate = verdict.svg_gate(None, SES, "3.21")
    assert (gate.advertised, gate.product_version) == ("3.27", "2025.3.3")
    assert verdict.svg_gate({"advertised_api_version": "3.27", "server": SES}, None, "3.21").advertised == "3.27"
    assert verdict.svg_gate(None, None, "3.21").advertised is None


@pytest.mark.parametrize(("reprobe", "proved"), [("available", True), ("indeterminate", False)])
def test_only_an_answered_floor_reprobe_reaches_the_gate_as_proof(reprobe, proved):
    report = {
        "advertised_api_version": "3.30",
        "server": CLOUD,
        "tiers": [{"tier": "svg", "floor_reprobe": {"api": "3.29", "verdict": reprobe}}],
    }
    assert verdict.svg_gate(report, None, "3.21").proved_by_reprobe is proved


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


def _record(svg: dict) -> dict:
    return {
        "view_name": "v",
        "data": {"status": "ok", "certification": "certified", "row_count": 1, "elapsed_sec": 0.1},
        "svg": svg,
    }


def _stale_record():
    return _record({"status": verdict.SVG_UNSUPPORTED_STATUS, "detail": "SVG export requires API version 3.29"})


def test_the_manifest_and_console_carry_the_SAME_remedy_and_the_three_numbers(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        _code, manifest = _write([_stale_record()], tmp_path, server_info=SES)
    assert manifest["rest_api_version"] == "3.21"
    assert manifest["advertised_rest_api_version"] == "3.27"
    assert manifest["server_product_version"] == "2025.3.3"
    leg = manifest["views"][0]["svg"]
    assert leg["cause"] == capability.SVG_CAUSE_SERVER_BELOW_FLOOR
    assert RAISE_THE_PIN not in leg["remedy"] and SERVER_CANNOT in leg["remedy"]
    assert leg["remedy"] in caplog.text
    assert capability.SVG_CAUSE_SERVER_BELOW_FLOOR in caplog.text
    assert "PNG and PDF do not depend on the SVG floor" in caplog.text and "2.5 and 2.8" in caplog.text
    # Plausible, documented by Tableau, and NOT measured here -- so nothing may claim it.
    text = caplog.text + json.dumps(manifest)
    for claim in ("unaffected", "break", "will fail", "other calls", "rejects every"):
        assert claim not in text


def test_an_unestablished_ceiling_is_null_in_the_manifest_never_an_invented_number(tmp_path):
    _code, manifest = _write([_stale_record()], tmp_path)
    assert manifest["advertised_rest_api_version"] is None
    assert manifest["server_product_version"] is None


def test_a_view_whose_ceiling_clears_the_floor_keeps_the_env_remedy(tmp_path):
    _code, manifest = _write([_stale_record()], tmp_path, server_info=CLOUD)
    leg = manifest["views"][0]["svg"]
    assert leg["cause"] == capability.SVG_CAUSE_SERVER_MEETS_FLOOR
    assert RAISE_THE_PIN in leg["remedy"]


def test_only_a_version_gated_svg_leg_carries_an_svg_verdict(tmp_path, caplog):
    records = [_record({"status": "failed", "detail": "data sources not connected"}), _record({"status": "ok"})]
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        _code, manifest = _write(records, tmp_path, server_info=SES)
    for view in manifest["views"]:
        assert "cause" not in view["svg"] and "remedy" not in view["svg"]
    clean = tmp_path / "clean"
    clean.mkdir()
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        code, _manifest = _write([_record({"status": "ok"})], clean, server_info=SES)
    assert code == 0
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
    """`_capture_render` cannot know the site's ceiling; `write_manifest` upgrades it."""
    session = _Session({"/data": (200, b"a\n1\n"), "image?format=svg": (400, SVG_TOO_OLD.encode())})
    record = oracle.capture_view(session, VIEW, tmp_path, frozenset({"svg"}))
    assert record["svg"]["status"] == verdict.SVG_UNSUPPORTED_STATUS
    assert record["svg"]["cause"] == capability.SVG_CAUSE_CEILING_NOT_ESTABLISHED
    assert NOT_ESTABLISHED in record["svg"]["remedy"]
    assert RAISE_THE_PIN not in record["svg"]["remedy"]


# ------------------------------------------------------------- main(): does the ceiling get probed


def _main(tmp_path, monkeypatch, session, argv, server_info) -> None:
    monkeypatch.setattr(sys, "argv", ["capture_tableau_oracle.py", "--out", str(tmp_path), *argv])
    monkeypatch.setattr(oracle, "resolve_env", lambda _path: dict(_ENV))
    monkeypatch.setattr(oracle, "require", lambda _env: None)
    monkeypatch.setattr(oracle, "TableauSession", lambda *_a, **_k: session)
    monkeypatch.setattr(oracle, "select_views", lambda *_a, **_k: ([VIEW], {"wb": "HR"}))
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", lambda *_a, **_k: None)
    monkeypatch.setattr(oracle.capability, "server_info", server_info)
    oracle.main()


def test_a_plain_svg_run_probes_serverinfo_so_the_cause_is_established(tmp_path, monkeypatch):
    """Without `--reference-best` there used to be NO ceiling, so state C was the ceiling."""
    session = _Session(
        {"/data": (200, b"a\n1\n"), "image?format=svg": (400, SVG_TOO_OLD.encode()), "/auth/signout": (204, b"")}
    )
    probed: list[str] = []

    def fake_server_info(base, **_kwargs):
        probed.append(base)
        return dict(SES, status=200)

    _main(tmp_path, monkeypatch, session, ["--svg"], fake_server_info)
    assert probed == ["https://example.online.tableau.com"]
    manifest = json.loads((tmp_path / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["advertised_rest_api_version"] == "3.27"
    leg = manifest["views"][0]["svg"]
    assert leg["cause"] == capability.SVG_CAUSE_SERVER_BELOW_FLOOR
    assert RAISE_THE_PIN not in leg["remedy"]


def test_a_run_that_asks_for_no_svg_does_not_pay_for_the_probe(tmp_path, monkeypatch):
    session = _Session({"/data": (200, b"a\n1\n"), "/auth/signout": (204, b"")})
    probed: list[str] = []
    _main(tmp_path, monkeypatch, session, [], lambda base, **_k: probed.append(base))
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


def _report(ceiling) -> str:
    return "\n".join(assess_estate._render_server_ceiling(ceiling))  # pylint: disable=protected-access


def test_a_below_floor_assessment_carries_per_rung_verdicts_report_and_console(monkeypatch, caplog):
    ceiling = _ceiling(monkeypatch, dict(SES, status=200))
    assert ceiling["client_api_version"] == "3.21"
    assert ceiling["advertised_api_version"] == "3.27"
    assert ceiling["advertised_release"] == "Tableau 2025.3"
    assert ceiling["best_reference_render"] == "pdf"
    assert [(r["tier"], r["verdict"]) for r in ceiling["rungs"]] == [
        ("svg", assess_estate.UNAVAILABLE),
        ("pdf", assess_estate.AVAILABLE),
        ("png_high", assess_estate.AVAILABLE),
    ]
    svg = _rung(ceiling, "svg")
    assert (svg["min_api"], svg["route"]) == ("3.29", "/image?format=svg")
    assert "Server 2026.2" in svg["min_release"]

    lines = _report(ceiling)
    assert "| rung | route |" in lines
    assert "No per-rung verdict is shown" not in lines and "was NOT established" not in lines
    assert "what we send" in lines and "what the server advertises" in lines
    assert "UNAVAILABLE on this server" in lines
    assert "Bottom line: `--reference-best` should resolve to `pdf`" in lines
    assert "at any client setting" in lines
    assert "only **vector** rung available here" in lines
    # `?resolution=high` is exactly 2x declared (docs/reference-capture.md), and only for DASHBOARDS.
    assert "exactly 2× the dashboard's declared size" in lines
    assert "650×800" in lines and "1300×1600" in lines
    assert "structurally legible and content-illegible" in lines
    assert "worksheet" in lines and "dashboard claim" in lines
    assert "derived from the **advertised** number" in lines and "has not been asked to honour" in lines

    with caplog.at_level(logging.INFO, logger="assess"):
        assess_estate._log_server_ceiling(ceiling)  # pylint: disable=protected-access
    assert "needs REST 3.29" in caplog.text
    assert "reference-best should resolve to PDF" in caplog.text
    assert "no rung verdict is shown" not in caplog.text


@pytest.mark.parametrize("server", [CLOUD, {"rest_api_version": "9.99", "product_version": "2099.1"}])
def test_an_above_floor_or_future_ceiling_expects_svg(monkeypatch, server):
    ceiling = _ceiling(monkeypatch, dict(server, status=200))
    assert ceiling["established"] is True
    assert _rung(ceiling, "svg")["verdict"] == assess_estate.AVAILABLE
    assert ceiling["best_reference_render"] == "svg"
    assert assess_estate.UNKNOWN not in {r["verdict"] for r in ceiling["rungs"]}
    lines = _report(ceiling)
    assert "Bottom line: `--reference-best` should resolve to `svg`" in lines
    assert "only **vector** rung available here" not in lines
    assert "at any client setting" not in lines


def test_a_site_below_every_rung_is_told_no_server_reference_exists_at_all(monkeypatch):
    ceiling = _ceiling(monkeypatch, {"status": 200, "product_version": "10.1", "rest_api_version": "2.4"})
    assert {r["verdict"] for r in ceiling["rungs"]} == {assess_estate.UNAVAILABLE}
    assert ceiling["best_reference_render"] is None
    lines = _report(ceiling)
    assert "NO reference render rung is reachable" in lines
    assert "192×192" in lines and "layout-grade, never validation-grade" in lines
    assert "1300×1600" not in lines


# ---------------------------------------------- state C in the ASSESSMENT: no rung verdict at all


def test_an_unreachable_ceiling_marks_every_rung_UNKNOWN_and_prints_no_rung_verdict(monkeypatch, caplog):
    """`unknown` is a value in the SAME field a consumer reads, not an absent key."""
    ceiling = _ceiling(monkeypatch, {"status": 0, "error": "URLError: unreachable"})
    assert ceiling["established"] is False
    assert {r["verdict"] for r in ceiling["rungs"]} == {assess_estate.UNKNOWN}
    assert ceiling["best_reference_render"] is None
    lines = _report(ceiling)
    assert "was NOT established" in lines and "`/serverinfo` answered `0`" in lines
    assert "No per-rung verdict is shown" in lines
    assert "is not a REST API version" not in lines
    for marker in (
        "| rung | route |",
        "Bottom line",
        "available",
        "UNAVAILABLE on this server",
        "/image?format=svg",
        "REST `3.29`",
    ):
        assert marker not in lines
    with caplog.at_level(logging.INFO, logger="assess"):
        assess_estate._log_server_ceiling(ceiling)  # pylint: disable=protected-access
    assert "NOT ESTABLISHED" in caplog.text
    assert "UNKNOWN -- no rung verdict is shown" in caplog.text
    for marker in ("AVAILABLE", "needs REST 3.29", "reference-best should resolve to"):
        assert marker not in caplog.text


def test_a_ceiling_that_is_not_a_version_is_UNKNOWN_in_the_assessment_too(monkeypatch):
    """The assessment classifies for itself rather than inheriting `server_info`'s refusal."""
    ceiling = _ceiling(monkeypatch, {"status": 200, "rest_api_version": "garbage-999", "product_version": "x"})
    assert ceiling["established"] is False
    assert ceiling["advertised_api_version"] is None
    assert {r["verdict"] for r in ceiling["rungs"]} == {assess_estate.UNKNOWN}
    assert ceiling["best_reference_render"] is None


def test_a_ceiling_from_an_UNSUCCESSFUL_probe_is_UNKNOWN_in_the_assessment_too(monkeypatch):
    def fake_urlopen(request, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, _Serverinfo(500, _serverinfo_xml("3.30")))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ceiling = assess_estate.server_ceiling(_Site())
    assert ceiling["established"] is False
    assert ceiling["advertised_api_version"] is None
    assert ceiling["probe_status"] == 500
    assert {r["verdict"] for r in ceiling["rungs"]} == {assess_estate.UNKNOWN}


def test_the_report_and_console_name_the_value_that_was_not_a_version(monkeypatch, caplog):
    ceiling = _ceiling(monkeypatch, {"status": 200, "invalid_rest_api_version": "garbage-999"})
    lines = _report(ceiling)
    assert "was NOT established" in lines
    assert "garbage-999" in lines and "is not a REST API version" in lines
    assert "| rung | route |" not in lines
    with caplog.at_level(logging.INFO, logger="assess"):
        assess_estate._log_server_ceiling(ceiling)  # pylint: disable=protected-access
    assert "NOT ESTABLISHED" in caplog.text
    assert "garbage-999" in caplog.text and "not a REST API version" in caplog.text
    assert "AVAILABLE" not in caplog.text


def test_the_assessment_section_is_absent_when_no_probe_was_run():
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
    ceiling = assess_estate.assemble(raw, 0.99)["server_ceiling"]
    assert ceiling["advertised_api_version"] == "3.27"
    assert _rung(ceiling, "svg")["verdict"] == assess_estate.UNAVAILABLE
    assert ceiling["best_reference_render"] == "pdf"
