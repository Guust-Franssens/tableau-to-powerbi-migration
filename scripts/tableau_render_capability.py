"""
purpose: decide which Tableau reference-render route is actually available on THIS site, by probing
         the endpoint rather than trusting a version string, and record the grade that was obtained.
usage:   imported by scripts/capture_tableau_oracle.py; run directly for a one-shot report:
         python scripts/tableau_render_capability.py --view <view-luid> [--env .env]

Why probe instead of reading a version
--------------------------------------
Three different numbers claim to answer "can this site export SVG?", and they disagree:

1. ``TABLEAU_REST_API_VERSION`` in ``.env`` -- a **client preference** we send in the URI. It is what
   we *ask for*, and asking for 3.21 against a 3.30 server loses SVG with no error that names the
   real cause.
2. ``/api/{v}/serverinfo`` ``restApiVersion`` -- the **server's advertised ceiling**. Unauthenticated,
   answers at any supported api-version (measured: 200 at 2.4, 3.15, 3.21, 3.29 alike; 404 at 3.99),
   and always reports the server's own number rather than echoing the one asked for.
3. What the endpoint **actually does**.

Only (3) is authoritative, and the gap between them is not hypothetical: between two runs a week
apart the same Tableau Cloud site moved from ``2026.2.5 / 3.29`` to ``2026.3.0 / 3.30`` with no
warning. Cloud is force-upgraded; on-prem Server is not. So this module probes, and reports all three
so a disagreement is visible instead of silently costing a tier.

The ladder (documented floors, from Tableau's REST reference)
------------------------------------------------------------
======== ================================ ============ ===================================
tier     route                             API floor    Tableau release
======== ================================ ============ ===================================
svg      ``/image?format=svg``             **3.29**     Cloud June 2026 / **Server 2026.2**
pdf      ``/pdf``                          **2.8**      **Server 10.5** (2018)
png_high ``/image?resolution=high``        **2.5**      **Server 10.2** (2017)
======== ================================ ============ ===================================

An on-prem customer on 2023.x-2025.x (API 3.19-3.25) therefore has **no SVG at all** but does have
PDF and PNG, which is why the ladder exists rather than a single recommendation. Below the ladder sits
the offline ``.twb`` embedded thumbnail (192x192, no server) handled by ``capture_tableau_reference.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("tableau-render-capability")

SERVERINFO_TIMEOUT_SEC = 30
SIGNIN_TIMEOUT_SEC = 60
# `serverinfo` is version-agnostic in practice, but a number still has to go in the URI. 3.4 is old
# enough that any server in support answers it, which is the point: this call must not itself need
# the capability it is being used to discover.
SERVERINFO_PROBE_VERSION = "3.4"

# Tableau's published REST-API-version -> release map, transcribed from
# https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_concepts_versions.htm
# (retrieved 2026-08-30). An API number alone is not actionable in a customer conversation -- "you
# need 3.29" means nothing until it reads "you need Server 2026.2". Rows marked CLOUD-ONLY have no
# on-prem Server release at all, so an on-prem site can never reach them.
#
# ⚠️ The published table STOPS AT 3.29. A live Cloud site probed on 2026-08-30 advertised
# `restApiVersion 3.30` / `productVersion 2026.3.0`, so the documentation lags the product -- which is
# precisely why this table is a translation aid and never the capability test.
API_RELEASE: dict[str, str] = {
    "3.29": "Tableau Cloud June 2026 / Server 2026.2",
    "3.28": "Tableau 2026.1",
    "3.27": "Tableau 2025.3",
    "3.26": "Tableau 2025.2 (CLOUD-ONLY)",
    "3.25": "Tableau 2025.1",
    "3.24": "Tableau 2024.3 (CLOUD-ONLY)",
    "3.23": "Tableau 2024.2",
    "3.22": "Tableau 2024.1 (CLOUD-ONLY)",
    "3.21": "Tableau 2023.3",
    "3.20": "Tableau 2023.2 (CLOUD-ONLY)",
    "3.19": "Tableau 2023.1",
    "3.18": "Tableau 2022.4 (CLOUD-ONLY)",
    "3.17": "Tableau 2022.3",
    "3.16": "Tableau 2022.2 (CLOUD-ONLY)",
    "3.15": "Tableau 2022.1",
    "3.14": "Tableau 2021.4",
    "3.13": "Tableau 2021.3",
    "3.12": "Tableau 2021.2",
    "3.11": "Tableau 2021.1",
    "3.10": "Tableau 2020.4",
    "3.9": "Tableau 2020.3",
    "3.8": "Tableau 2020.2",
    "3.7": "Tableau 2020.1",
    "3.6": "Tableau 2019.4",
    "3.5": "Tableau 2019.3",
    "3.4": "Tableau 2019.2",
    "3.3": "Tableau 2019.1",
    "3.2": "Tableau 2018.3",
    "3.1": "Tableau 2018.2",
    "3.0": "Tableau 2018.1",
    "2.8": "Tableau Server 10.5",
    "2.7": "Tableau Server 10.4",
    "2.6": "Tableau Server 10.3",
    "2.5": "Tableau Server 10.2",
    "2.4": "Tableau Server 10.1",
    "2.3": "Tableau Server 10.0",
}


def release_for(api_version: str) -> str:
    """Translate a REST API version into the Tableau release a customer would recognise."""
    return API_RELEASE.get(api_version, f"unknown release (API {api_version} is not in the published table)")


# Marker Tableau returns when the CLIENT api-version in the URI is below the feature's floor. It names
# the version, so it is distinguishable from a broken view or a dead credential.
VERSION_GATE_MARKER = "requires API version"
# VizQL could not render at all -- upstream of the output format, so it says nothing about capability.
# Measured identical on /image, /image?format=svg, /pdf and /data for one blocked workbook.
UNRENDERABLE_MARKER = "data sources not connected"

# What each rung's payload must actually look like. HTTP 200 is NOT proof that the requested format
# came back: an older server that does not know `format=svg` can ignore the unknown parameter and
# return its default PNG, which is exactly the on-prem case this ladder exists to protect. The
# signature is authoritative (a server cannot fake magic bytes); the content type corroborates.
FORMAT_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    "png": (b"\x89PNG\r\n\x1a\n",),
}
CONTENT_TYPES: dict[str, str] = {"svg": "image/svg+xml", "pdf": "application/pdf", "png": "image/png"}

# SVG cannot be settled by a leading signature the way a binary format can: it is XML, so Tableau's
# own error bodies begin with the very same `<?xml` declaration as a valid drawing. Accepting that
# declaration let `<?xml version="1.0"?><error/>` classify as a usable SVG -- the original
# wrong-format defect surviving in a narrower form. The ROOT ELEMENT is what settles it.
_XML_PROLOGUE = re.compile(
    rb"^(?:\xef\xbb\xbf)?\s*(?:<\?xml[^>]*\?>\s*|<!--.*?-->\s*|<!DOCTYPE[^>\[]*(?:\[[^\]]*\])?>\s*)*", re.S
)


def looks_like_svg(body: bytes) -> bool:
    """Is the ROOT element ``<svg>``, once any BOM, XML declaration, comments and DOCTYPE are skipped?"""
    head = _XML_PROLOGUE.sub(b"", body[:4096], count=1).lstrip()
    return head.startswith(b"<svg") and (len(head) == 4 or head[4:5] in b" \t\r\n/>")


def _identify(head: bytes) -> str:
    """Best-effort name for whatever actually arrived, so the diagnostic is actionable."""
    for other, sigs in FORMAT_SIGNATURES.items():
        if any(head.startswith(sig) for sig in sigs):
            return other
    if head.startswith((b"<?xml", b"<")):
        return "xml/html (not an <svg> root)"
    return "unrecognised bytes"


# How much of an attacker-influenced value is REDACTED before any of it is quoted, and how much of the
# redacted result is then shown. The order is the whole point -- see `_quote` below.
_REDACTION_WINDOW_BYTES = 256
_DIAGNOSTIC_CHARS = 16
_CONTENT_TYPE_CHARS = 120


def _quote_head(body: bytes, redactor) -> str:
    """A quotable rendering of a response's first bytes that a redactor can actually cover.

    Three things here are load-bearing, and the previous ``{head[:8]!r}`` got all three wrong:

    1. **Redact BEFORE truncating.** Slicing eight bytes off the front of a longer secret leaves a
       fragment the literal-matching redactor cannot see, so it survives verbatim. A generous window
       is decoded and scrubbed first; only the scrubbed text is then cut down.
    2. **Redact TEXT, not a ``bytes`` repr.** ``repr(b"...")`` escapes quotes, backslashes and every
       non-ASCII byte, so a secret containing any of them reaches the report in an escaped form that
       the redactor never matched.
    3. **Quote with ``ascii()``.** The decode is lossy (``errors="replace"``), and a literal U+FFFD in
       a message that later prints to a cp1252 console raises ``UnicodeEncodeError``.
    """
    text = body.lstrip()[:_REDACTION_WINDOW_BYTES].decode("utf-8", "replace")
    return ascii((redactor(text) if redactor else text)[:_DIAGNOSTIC_CHARS])


def format_matches(kind: str, body: bytes, content_type: str | None, *, redactor=None) -> tuple[bool, str]:
    """Does this payload really carry ``kind``? Returns ``(ok, why_not)``.

    Checked in this order on purpose: the **payload** is decisive, because bytes cannot lie about what
    they are. A content-type mismatch alone is only reported when the payload could not settle it,
    since proxies rewrite headers and some servers omit the charset.

    ⚠️ ``why_not`` quotes two attacker-influenced values -- the received ``Content-Type`` and the
    response's own first bytes -- so ``redactor`` is scrubbing a *credential*, not tidying output.
    It is applied to each raw value **individually, before** that value is case-folded, split or
    truncated, because every one of those transforms defeats a literal-matching redactor:

    * ``.lower()`` on the Content-Type is what let ``image/SYNTHETIC_SESSION_TOKEN_123`` reach the
      report as ``image/synthetic_session_token_123`` -- the caller's redactor ran afterwards and
      deliberately does not match case-changed secrets;
    * ``.split(";")[0]`` would cut a secret containing a semicolon; and
    * slicing the body's first bytes leaves a prefix of a longer secret (see ``_quote_head``).

    Classification itself still reads the RAW values -- the case-insensitive MIME comparison is
    performed on the unredacted header -- so redaction can never change a verdict.
    """
    head = body.lstrip()[:16]
    if kind == "svg":
        if not looks_like_svg(body):
            return False, f"expected an <svg> root, got {_identify(head)} ({_quote_head(body, redactor)})"
    else:
        signatures = FORMAT_SIGNATURES.get(kind, ())
        if signatures and not any(head.startswith(sig) for sig in signatures):
            return False, f"expected {kind} payload, got {_identify(head)} ({_quote_head(body, redactor)})"
    raw_type = content_type or ""
    mime = raw_type.split(";")[0].strip().lower()
    expected = CONTENT_TYPES.get(kind)
    if mime and expected and mime != expected:
        received = (redactor(raw_type) if redactor else raw_type).strip()[:_CONTENT_TYPE_CHARS]
        return False, f"expected Content-Type {expected}, got {received}"
    return True, ""


@dataclass(frozen=True)
class RenderTier:
    """One rung of the reference-render ladder, with the evidence grade it can support."""

    name: str
    endpoint: str
    query: str
    extension: str
    vector: bool
    min_api: str
    note: str

    @property
    def min_release(self) -> str:
        """The Tableau release a customer must be on, derived from the published version table."""
        return release_for(self.min_api)


LADDER: tuple[RenderTier, ...] = (
    RenderTier(
        name="svg",
        endpoint="image",
        query="?format=svg",
        extension="svg",
        vector=True,
        min_api="3.29",
        note="resolution-independent; <text> elements carry the literal labels",
    ),
    RenderTier(
        name="pdf",
        endpoint="pdf",
        query="?type=Unspecified",
        extension="pdf",
        vector=True,
        min_api="2.8",
        note="vector with embedded fonts; needs a PDF rasteriser to view as an image",
    ),
    RenderTier(
        name="png_high",
        endpoint="image",
        query="?resolution=high",
        extension="png",
        vector=False,
        min_api="2.5",
        note="universal, but capped at 2x a dashboard's declared size",
    ),
)


def api_tuple(version: str) -> tuple[int, ...]:
    """Comparable form of a REST version. Never compare these as strings: ``"3.9" > "3.10"``."""
    return tuple(int(part) for part in re.findall(r"\d+", version)) or (0,)


def tier_priority(name: str | None, tiers: tuple[RenderTier, ...] = LADDER) -> int:
    """How good a selected tier is, as a comparable number -- **higher is better**, ``0`` = none.

    ``LADDER`` is ordered best-first, so a raw index sorts backwards; a caller that compares indexes
    directly picks the WORST tier. Derived from the ladder rather than written out, so a new rung
    cannot be added in one place and forgotten in the comparison.
    """
    names = [tier.name for tier in tiers]
    return len(names) - names.index(name) if name in names else 0


def supports(available: str | None, required: str) -> bool | None:
    """Is ``available`` at or above ``required``? ``None`` when the server did not say."""
    if not available:
        return None
    return api_tuple(available) >= api_tuple(required)


def server_info(base: str, *, timeout: int = SERVERINFO_TIMEOUT_SEC) -> dict[str, Any]:
    """The server's own account of itself. **Unauthenticated** -- callable before any sign-in.

    Deliberately fails soft: a site that will not answer ``serverinfo`` is not a reason to abandon a
    capture, because the endpoint probe below is the authoritative check anyway. This only supplies
    the *advertised* number, whose whole value is being compared against what actually happens.
    """
    url = f"{base.rstrip('/')}/api/{SERVERINFO_PROBE_VERSION}/serverinfo"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read().decode("utf-8", "replace"), exc.code
    except (OSError, urllib.error.URLError) as exc:
        return {"status": 0, "error": f"{type(exc).__name__}: {exc}"}

    def grab(pattern: str) -> str | None:
        match = re.search(pattern, body)
        return match.group(1) if match else None

    return {
        "status": status,
        "product_version": grab(r"<productVersion[^>]*>([^<]+)<"),
        "build": grab(r'<productVersion[^>]*build="([^"]+)"'),
        "rest_api_version": grab(r"<restApiVersion>([^<]+)<"),
    }


def classify_probe(
    status: int,
    body: bytes,
    *,
    kind: str | None = None,
    content_type: str | None = None,
    redactor=None,
) -> tuple[str, str]:
    """Turn one probe response into ``(verdict, detail)``.

    Three outcomes have to stay distinct, and collapsing any two of them is the failure this whole
    module exists to prevent:

    ``available``    the route answered **with the format that was asked for**;
    ``unsupported``  the SERVER refused the feature by version -- a real, permanent "no" for this tier;
    ``indeterminate``something else happened (a blocked view, a dead credential, a gateway blip, or a
                     200 carrying the WRONG format), so this probe says **nothing** about the tier.
                     Reporting that as "no" would silently demote a site that is perfectly capable,
                     which is the unassessable-collapsing-into-a-clean-answer shape.

    ``redactor`` scrubs secrets from ``detail``. Classification runs on the RAW text and reporting on
    the redacted copy, never the reverse: redaction is handed the human-chosen PAT *name*, and a short
    one rewrites Tableau's own error codes, so a ``401002`` mangled mid-string stops being recognisable.
    Redaction must never mutate syntax that control flow depends on.

    **Every** returned ``detail`` passes through ``_scrub`` -- including the HTTP 200 wrong-format
    diagnostic, which quotes the received ``Content-Type``. That header is attacker-influenced and a
    reflecting proxy can put a live token in it; the first version of this check returned that string
    before any redaction ran.
    """

    def _scrub(text: str) -> str:
        return redactor(text) if redactor else text

    if status == 200:
        if kind:
            ok, why = format_matches(kind, body, content_type, redactor=redactor)
            if not ok:
                # A 200 with the wrong payload is the on-prem trap: an older server ignores an unknown
                # `format=svg` and hands back its default PNG. Selecting this rung would persist PNG
                # bytes in a `.svg` and call them vector.
                #
                # `why` is ALREADY redacted value-by-value (see `format_matches`): the outer `_scrub`
                # cannot undo case-folding or truncation that has already happened, which is exactly
                # how a mixed-case token used to survive this line.
                return "indeterminate", _scrub(f"HTTP 200 but {why}")[:180]
        return "available", ""
    text = body.decode("utf-8", "replace")
    raw_detail = (re.findall(r"<detail>(.*?)</detail>", text, re.S) or [text])[0]
    detail = _scrub(raw_detail)[:180].strip()
    if VERSION_GATE_MARKER in text:
        return "unsupported", detail
    if UNRENDERABLE_MARKER in text:
        return "indeterminate", detail
    return "indeterminate", detail or f"HTTP {status}"


def _probe_tier(fetch, tier: RenderTier, redactor, api: str | None = None) -> tuple[str, str]:
    """One rung, one call. ``api`` overrides the client api-version for a floor re-probe."""
    status, body, content_type = fetch(tier.endpoint, tier.query, api)
    return classify_probe(status, body, kind=tier.extension, content_type=content_type, redactor=redactor)


@dataclass(frozen=True)
class ApiVersions:
    """The two version CLAIMS, kept together because the reconciliation is the whole point.

    ``configured`` is what we send in the URI -- a client preference. ``advertised`` is what
    ``/serverinfo`` says the server can do. Neither is evidence of what an endpoint will actually do,
    and this pair exists so no caller can pass one while forgetting the other.
    """

    configured: str | None = None
    advertised: str | None = None


def detect(
    fetch,
    view_luid: str,
    versions: ApiVersions | None = None,
    *,
    redactor=None,
    tiers: tuple[RenderTier, ...] = LADDER,
) -> dict[str, Any]:
    """Walk the ladder against ONE view and report which rung actually answers.

    ``fetch(endpoint, query, api) -> (status, body, content_type)`` is injected so this is testable
    without a network and so the caller keeps its own retry/re-auth policy. ``api`` is an optional
    per-call override of the client api-version, used only for the floor re-probe below.

    **A selection can be PROVISIONAL.** Probing stops at the first rung that answers, but if a rung
    ABOVE it was indeterminate -- a gateway blip, a blocked view, a 200 in the wrong format -- then a
    better tier may exist and simply could not be measured. Reporting that as a settled answer is the
    same collapse this module rejects one level down, so the result carries ``provisional`` and
    ``capability_complete``, and the caller is expected to try another view.

    Returns the chosen tier, the per-tier verdicts, and the **three-way version reconciliation**.
    """
    versions = versions or ApiVersions()
    verdicts, chosen, chosen_api, unknown_above = _walk_ladder(fetch, tiers, redactor, versions)

    definite_no = all(v["verdict"] == "unsupported" for v in verdicts)
    report: dict[str, Any] = {
        "probe_view_luid": view_luid,
        "configured_api_version": versions.configured,
        "advertised_api_version": versions.advertised,
        "selected_tier": chosen,
        # The api-version the SELECTED tier answered at. `None` = the configured one. A caller that
        # captures the selected tier MUST honour this, or it fetches at a version that refuses it.
        "selected_api_version": chosen_api,
        # PROVISIONAL: a rung answered, but a better one could not be measured on this view.
        "provisional": bool(chosen and unknown_above),
        # False whenever any rung's capability is still unknown -- the honest "we do not know yet".
        "capability_complete": bool(chosen and not unknown_above) or definite_no,
        "tiers": verdicts,
        "warnings": [],
    }
    if chosen and unknown_above:
        blocked = [v["tier"] for v in verdicts if v["verdict"] == "indeterminate"]
        report["warnings"].append(
            f"selected tier '{chosen}' is PROVISIONAL: {', '.join(blocked)} could not be measured on this "
            f"view, so a better tier may exist. Re-probe with a different view before treating this as "
            f"the site's ceiling."
        )
    if chosen is None:
        if definite_no:
            report["warnings"].append("no render tier is available on this site")
        else:
            # Never say "no tier is available" when nothing definitively refused us. A mix of
            # version gates and blocked routes leaves the answer UNKNOWN, not negative.
            report["warnings"].append(
                "capability UNDETERMINED: at least one route failed for a reason that is not a version "
                "gate (most likely this probe view's data sources are not connected). Re-probe with a "
                "different view before concluding anything about this site."
            )

    _add_pin_warnings(report, verdicts, tiers, versions)
    return report


def _walk_ladder(fetch, tiers, redactor, versions: ApiVersions):
    """Probe rungs in order until one answers. Returns ``(verdicts, chosen, chosen_api, unknown_above)``."""
    verdicts: list[dict[str, Any]] = []
    chosen: str | None = None
    chosen_api: str | None = None
    unknown_above = False
    for tier in tiers:
        if chosen is not None:
            verdicts.append({"tier": tier.name, "verdict": "not_probed", "detail": "a better tier already answered"})
            continue
        entry = _walk_one_tier(fetch, tier, redactor, versions)
        if entry["verdict"] == "available":
            chosen, chosen_api = tier.name, entry.get("answered_api")
        elif entry["verdict"] == "indeterminate":
            unknown_above = True
        verdicts.append(entry)
    return verdicts, chosen, chosen_api, unknown_above


def _walk_one_tier(fetch, tier: RenderTier, redactor, versions: ApiVersions) -> dict[str, Any]:
    """Probe one rung, re-probing at the tier's documented floor when OUR pin may be the cause."""
    verdict, detail = _probe_tier(fetch, tier, redactor)
    entry: dict[str, Any] = {
        "tier": tier.name,
        "verdict": verdict,
        "detail": detail,
        "min_api": tier.min_api,
        "min_release": tier.min_release,
        # The api-version this rung actually ANSWERED at. `None` means the configured one. A capture
        # that ignores this fetches at the configured version and fails on a tier the report promised.
        "answered_api": None if verdict == "available" else None,
    }
    # A version gate is the ONE case where the client's own pin may be the cause. Re-probe at the
    # tier's floor rather than inferring support from the advertised number: "the server advertises
    # 3.29" is not the same claim as "SVG works here", and this module's whole thesis is that the
    # second must be measured. Costs one extra call, and only when it can change the outcome.
    if (
        verdict == "unsupported"
        and supports(versions.configured, tier.min_api) is False
        and supports(versions.advertised, tier.min_api)
    ):
        floor_verdict, floor_detail = _probe_tier(fetch, tier, redactor, api=tier.min_api)
        entry["floor_reprobe"] = {"api": tier.min_api, "verdict": floor_verdict, "detail": floor_detail}
        entry["verdict"] = floor_verdict
        if floor_verdict == "available":
            # Promote the FLOOR version with the tier. Without this the run is told 'svg is available'
            # and then captures at the configured version, where the same request is still refused.
            entry["answered_api"] = tier.min_api
    return entry


def _add_pin_warnings(report, verdicts, tiers, versions: ApiVersions) -> None:
    """Flag a tier the SERVER can do that our own client pin is throwing away.

    Only ever claims support that was **proved by the floor re-probe**. Saying "this server supports
    it" from ``advertised >= min_api`` alone would be the very inference this module forbids -- and it
    is wrong in exactly the case that could not be verified: an on-prem 2026.2 advertising 3.29 where
    SVG may not actually have shipped.
    """
    for tier in tiers:
        entry = next((v for v in verdicts if v["tier"] == tier.name), None)
        reprobe = (entry or {}).get("floor_reprobe")
        if not reprobe or supports(versions.configured, tier.min_api) is not False:
            continue
        if reprobe["verdict"] == "available":
            report["warnings"].append(
                f"tier '{tier.name}' WORKS on this server -- proved by re-probing at API {tier.min_api} -- "
                f"but TABLEAU_REST_API_VERSION is pinned to {versions.configured}; set it to "
                f"{tier.min_api} or later to use it"
            )
        else:
            report["warnings"].append(
                f"tier '{tier.name}' is refused at the pinned API {versions.configured}; a re-probe at "
                f"its floor {tier.min_api} was {reprobe['verdict']} ({reprobe['detail'][:80]}), so "
                f"whether this server (advertises {versions.advertised}) supports it is UNKNOWN"
            )


# A capability probe costs metered export calls (Tableau meters ~100/hour/Creator), so it is bounded.
# More than one is still needed because a single blocked view fails every route and would otherwise be
# read as "this site cannot render", which is exactly the wrong conclusion.
MAX_CAPABILITY_PROBE_VIEWS = 3


def probe_render_capability(session, env: dict[str, str], views: list[dict[str, Any]]) -> dict[str, Any]:
    """Ask the SITE what it can render, by probing, and reconcile that with both version strings.

    ``session`` is duck-typed on purpose -- anything exposing ``site_id``, ``raw_get(path, api=...)``
    and ``redact_text(text)`` will do. That is what lets this orchestration live beside the ladder it
    drives without importing ``capture_tableau_oracle``, which imports THIS module.

    The probe view matters. A workbook whose data sources are not connected fails every route
    identically, so probing one would report "no tier available" for a site that is perfectly capable
    -- so try successive views until one gives a determinate answer, capped at
    ``MAX_CAPABILITY_PROBE_VIEWS`` because each attempt costs metered export calls.
    """
    info = server_info(env["TABLEAU_SERVER_URL"])
    configured = env.get("TABLEAU_REST_API_VERSION", "3.21")
    advertised = info.get("rest_api_version")
    LOG.info(
        "site reports product=%s build=%s, advertises REST %s; we are asking as %s",
        info.get("product_version"),
        info.get("build"),
        advertised,
        configured,
    )

    def fetcher(view_luid: str):
        def fetch(endpoint: str, query: str, api: str | None = None) -> tuple[int, bytes, str | None]:
            # Deliberately the RAW request, not `export()`: a version gate is a permanent answer and
            # must not be run through a retry/re-auth ladder built for transient faults.
            return session.raw_get(f"/sites/{session.site_id}/views/{view_luid}/{endpoint}{query}", api=api)

        return fetch

    best: dict[str, Any] = {}
    tried: list[str] = []
    for view in views[:MAX_CAPABILITY_PROBE_VIEWS]:
        tried.append(view["id"])
        report = detect(
            fetcher(view["id"]),
            view["id"],
            ApiVersions(configured=configured, advertised=advertised),
            # A proxy echoing X-Tableau-Auth puts a LIVE session token in the error body, and this
            # report is written to disk inside the manifest. Classification still sees raw text.
            redactor=session.redact_text,
        )
        report["probe_view_name"] = view.get("name")
        if not best or _capability_rank(report) > _capability_rank(best):
            best = report
        # Keep going while the answer is UNDETERMINED *or* merely PROVISIONAL: a rung that answered
        # while a better rung was blocked on this view is not the site's ceiling, and stopping there
        # is how a capable site gets silently demoted.
        if report.get("selected_tier") and report.get("capability_complete"):
            break
    best["server"] = info
    # COUNTED, never derived from the cap. `min(len(views), MAX_CAPABILITY_PROBE_VIEWS)` reported how
    # many views were ELIGIBLE, so a first view that answered completely and broke out of the loop was
    # written up as "3" -- one probe presented as three independent corroborations. The LUIDs make the
    # claim auditable rather than merely honest: a reader can go and re-run exactly those views.
    best["probe_views_tried"] = len(tried)
    best["probe_view_luids"] = tried
    LOG.info(
        "render capability: best tier = %s%s",
        best.get("selected_tier") or "UNDETERMINED",
        " (PROVISIONAL)" if best.get("provisional") else "",
    )
    return best


def _capability_rank(report: dict[str, Any]) -> tuple[int, int, int]:
    """Order two probe reports: a settled answer beats a provisional one beats no answer at all.

    ⚠️ The third element is not decoration. With only ``(selected, complete)`` two PROVISIONAL reports
    tie, ``>`` is false, and the FIRST view examined wins **regardless of which tier it found** -- so a
    view whose SVG and PDF both blew up transiently and fell through to PNG beat a later view that
    actually proved PDF, and ``--reference-best`` chose a raster reference on a site where vector was
    demonstrably available.

    Completeness still outranks tier quality: a *complete* report means every rung above the chosen one
    was **definitively** refused, so its selection is the measured ceiling, whereas a provisional one is
    only a lower bound. The two can disagree only if one rung came back ``available`` on one view and
    version-gated on another -- which a version gate, being a property of the server rather than the
    view, cannot do.
    """
    return (
        1 if report.get("selected_tier") else 0,
        1 if report.get("capability_complete") else 0,
        tier_priority(report.get("selected_tier")),
    )


def apply_selected_tier(
    report: dict[str, Any], wants: set[str], api_overrides: dict[str, str], env: dict[str, str]
) -> None:
    """Turn a probe verdict into what the run will actually fetch, INCLUDING the api version.

    Without the version half, a floor re-probe that recovered a tier leaves the run claiming
    ``selected_tier='svg'`` and then capturing at the configured version, where the very same request
    is still refused -- measured: floor 3.29 ``available``, configured 3.21 ``unsupported``. The
    report would promise a tier the capture cannot fetch.
    """
    tier = report.get("selected_tier")
    if not tier:
        return
    # The ladder names the PNG rung `png_high` (it is `?resolution=high`, not the plain render); the
    # capture kinds are keyed by file format. One mapping, stated once.
    kind = {"png_high": "png"}.get(tier, tier)
    wants.add(kind)
    if report.get("selected_api_version"):
        api_overrides[kind] = report["selected_api_version"]
        LOG.info(
            "  capturing '%s' at API %s (recovered by floor re-probe; configured is %s)",
            kind,
            api_overrides[kind],
            env.get("TABLEAU_REST_API_VERSION", "3.21"),
        )


def sign_in(base: str, site: str, pat_name: str, pat_secret_value: str, api: str) -> tuple[str, str]:
    """Minimal PAT sign-in returning ``(token, site_id)``.

    Deliberately duplicated rather than imported from ``capture_tableau_oracle``: that module imports
    THIS one, and reaching back would make the pair cyclic. It is also what keeps this module usable
    on its own -- the capability question is asked before, and independently of, any capture.
    Twelve lines is a fair price for a module that a different caller can lift wholesale.
    """
    body = json.dumps(
        {
            "credentials": {
                "personalAccessTokenName": pat_name,
                "personalAccessTokenSecret": pat_secret_value,
                "site": {"contentUrl": site},
            }
        }
    ).encode()
    req = urllib.request.Request(f"{base.rstrip('/')}/api/{api}/auth/signin", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=SIGNIN_TIMEOUT_SEC) as resp:
        creds = json.loads(resp.read())["credentials"]
    return creds["token"], creds["site"]["id"]


def _cli_fetch(base: str, api: str, site_id: str, view_luid: str, token: str):
    """Fetcher for the standalone report path. Returns ``(status, body, content_type)``."""

    def fetch(endpoint: str, query: str, api_override: str | None = None) -> tuple[int, bytes, str | None]:
        version = api_override or api
        url = f"{base.rstrip('/')}/api/{version}/sites/{site_id}/views/{view_luid}/{endpoint}{query}"
        req = urllib.request.Request(url)
        req.add_header("X-Tableau-Auth", token)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type") if exc.headers else None
        except (OSError, urllib.error.URLError) as exc:
            return 0, f"{type(exc).__name__}: {exc}".encode(), None

    return fetch


def _log_versions(info: dict[str, Any], configured: str) -> None:
    """One line reconciling what the server says with what we are asking for."""
    LOG.info(
        "server: product=%s build=%s advertises REST %s (%s); we ask as %s (%s)",
        info.get("product_version"),
        info.get("build"),
        info.get("rest_api_version"),
        release_for(info.get("rest_api_version") or ""),
        configured,
        release_for(configured),
    )


def _build_report(env, args, info) -> dict[str, Any]:
    """Sign in, probe, and return the capability report. Split out so ``main`` stays a thin shell."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tableau_env import pat_secret, redact  # pylint: disable=import-outside-toplevel

    base = env["TABLEAU_SERVER_URL"]
    configured = env.get("TABLEAU_REST_API_VERSION", "3.21")
    token, site_id = sign_in(base, env["TABLEAU_SITE"], env["TABLEAU_PAT_NAME"], pat_secret(env), configured)
    # Everything printed or serialised from here is scrubbed. A proxy or WAF that echoes request
    # headers puts a LIVE session token in an error body, and this report is written to disk.
    secrets = (pat_secret(env), env["TABLEAU_PAT_NAME"], token)
    report = detect(
        _cli_fetch(base, configured, site_id, args.view, token),
        args.view,
        ApiVersions(configured=configured, advertised=info.get("rest_api_version")),
        redactor=lambda text: redact(text, *secrets),
    )
    report["server"] = info
    return report


def main(argv: list[str] | None = None) -> int:
    """One-shot capability report for a site, so an operator can answer 'what will I get here?'."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--view", required=True, help="view LUID to probe (use one that renders)")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials file")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tableau_env import require, resolve_env  # pylint: disable=import-outside-toplevel

    env = resolve_env(args.env)
    require(env)
    info = server_info(env["TABLEAU_SERVER_URL"])
    _log_versions(info, env.get("TABLEAU_REST_API_VERSION", "3.21"))
    report = _build_report(env, args, info)
    print(json.dumps(report, indent=2))
    for warning in report["warnings"]:
        LOG.warning("! %s", warning)
    return 0 if report["selected_tier"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
