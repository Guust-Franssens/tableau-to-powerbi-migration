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
import http
import json
import logging
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
# `redacted_note` is the chokepoint every attacker-influenced diagnostic in this module goes through,
# so it is a hard module-level dependency rather than one of the lazy imports below.
from tableau_capture_policy import (  # noqa: E402  # pylint: disable=wrong-import-position
    DEFAULT_MAX_AGE_MINUTES,
    validate_max_age,
)
from tableau_env import env_redactor, redact, redacted_note  # noqa: E402  # pylint: disable=wrong-import-position

# ⚠️ Imported as a plain NAME on purpose, twice over. `tableau_http._request(...)` is
# `protected-access` to pylint (W0212, measured), and any alias would rename the call away from
# `TAINTING_CALLS` in `tests/test_diagnostic_redaction.py`, silently un-tainting every call site while
# the gate still reports green. This module makes **no** other HTTP call: rounds 7, 8 and 9 each found
# a different hole in a hand-rolled copy, so there is now one implementation and no second `try`.
from tableau_http import (  # noqa: E402  # pylint: disable=wrong-import-position
    NETWORK_ERROR_STATUS,
    _request,
    header_value,
)

LOG = logging.getLogger("tableau-render-capability")

SERVERINFO_TIMEOUT_SEC = 30
SIGNIN_TIMEOUT_SEC = 60
# The standalone report path's own export timeout. A render can take a while on a large dashboard,
# and unlike the oracle's capture loop there is no retry here to absorb an over-tight cut.
CLI_FETCH_TIMEOUT_SEC = 120
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


# How much of the REDACTED result each diagnostic shows. These bound the OUTPUT only: nothing is cut
# before the redactor has seen the whole value -- see `tableau_env.redacted_note`, which is the only
# sanctioned way to put attacker-influenced text into a message this module prints or persists.
_DIAGNOSTIC_CHARS = 16
_CONTENT_TYPE_CHARS = 120
# Room for a Tableau error body's `<detail>` element to survive redaction intact. Tableau's error
# bodies are well under 1 KB, so this only ever cuts a pathological one -- and cutting it loses
# diagnostic text, never a secret, because it applies to the redacted copy.
_ERROR_BODY_CHARS = 4000


def format_matches(kind: str, body: bytes, content_type: str | None, *, redactor=None) -> tuple[bool, str]:
    """Does this payload really carry ``kind``? Returns ``(ok, why_not)``.

    Checked in this order on purpose: the **payload** is decisive, because bytes cannot lie about what
    they are. A content-type mismatch alone is only reported when the payload could not settle it,
    since proxies rewrite headers and some servers omit the charset.

    ⚠️ ``why_not`` quotes two attacker-influenced values -- the received ``Content-Type`` and the
    response's own leading bytes -- so ``redactor`` is scrubbing a *credential*, not tidying output.
    Both go through :func:`tableau_env.redacted_note`, which redacts the whole value before anything
    truncates, strips, folds or quotes it. Four review rounds each found one call site that had done
    those in the other order; the chokepoint exists so the order cannot be expressed here at all.

    Classification still reads the RAW values. ``head`` below and the case-folded ``mime`` are used
    ONLY to decide the verdict and are never interpolated into the message, so redaction cannot change
    an answer and a transformation cannot leak a credential.
    """
    head = body.lstrip()[:16]
    if kind == "svg":
        if not looks_like_svg(body):
            note = redacted_note(body, redactor, limit=_DIAGNOSTIC_CHARS, quote=True)
            return False, f"expected an <svg> root, got {_identify(head)} ({note})"
    else:
        signatures = FORMAT_SIGNATURES.get(kind, ())
        if signatures and not any(head.startswith(sig) for sig in signatures):
            note = redacted_note(body, redactor, limit=_DIAGNOSTIC_CHARS, quote=True)
            return False, f"expected {kind} payload, got {_identify(head)} ({note})"
    raw_type = content_type or ""
    mime = raw_type.split(";")[0].strip().lower()
    expected = CONTENT_TYPES.get(kind)
    if mime and expected and mime != expected:
        received = redacted_note(raw_type, redactor, limit=_CONTENT_TYPE_CHARS)
        return False, f"expected Content-Type {expected}, got {received}"
    return True, ""


@dataclass(frozen=True)
class RenderTier:  # pylint: disable=too-many-instance-attributes
    """One rung of the reference-render ladder, with the evidence grade it can support.

    ⚠️ The ``too-many-instance-attributes`` suppression is scoped to this class and is the right
    answer rather than a raised global ceiling: this is a frozen RECORD describing one rung of a
    published capability table (route, floor, payload kind, and what the rung resolves to), not an
    object accumulating state. Splitting eight related constants across two dataclasses to satisfy a
    default of seven would put a rung's floor and a rung's ceiling in different objects, which is
    exactly the separation that let #474 happen. Raising ``max-attributes`` in ``pyproject.toml``
    would instead loosen the check for every class in the repo.
    """

    name: str
    endpoint: str
    query: str
    extension: str
    vector: bool
    min_api: str
    note: str
    # What this rung can RESOLVE to, in one clause -- the other half of "is it available?", and the
    # half that is easy to over-trust. Kept on the rung so a consumer that reports availability can
    # report the ceiling from the same object instead of writing the number out a second time.
    ceiling: str = ""

    @property
    def min_release(self) -> str:
        """The Tableau release a customer must be on, derived from the published version table."""
        return release_for(self.min_api)

    @property
    def route(self) -> str:
        """The REST route this rung asks for, as an operator would type it."""
        return f"/{self.endpoint}{self.query}"


LADDER: tuple[RenderTier, ...] = (
    RenderTier(
        name="svg",
        endpoint="image",
        query="?format=svg",
        extension="svg",
        vector=True,
        min_api="3.29",
        note="resolution-independent; <text> elements carry the literal labels",
        ceiling="unbounded (vector)",
    ),
    RenderTier(
        name="pdf",
        endpoint="pdf",
        query="?type=Unspecified",
        extension="pdf",
        vector=True,
        min_api="2.8",
        note="vector with embedded fonts; needs a PDF rasteriser to view as an image",
        ceiling="unbounded (vector)",
    ),
    RenderTier(
        name="png_high",
        endpoint="image",
        query="?resolution=high",
        extension="png",
        vector=False,
        min_api="2.5",
        note="universal, but capped at 2x a dashboard's declared size",
        # ⚠️ A DASHBOARD claim, and deliberately worded as one. `docs/reference-capture.md` records
        # both halves: 2x declared on 52/52 dashboards with `standard`/`veryhigh` returning HTTP 400
        # and `vizWidth`/`vizHeight` ignored -- but on a WORKSHEET `vizHeight` IS honoured
        # (361x835 -> 361x1535). The over-general version of this sentence has already been
        # corrected once in that document; do not reintroduce it here.
        ceiling="exactly 2x a DASHBOARD's declared size (52/52), and no parameter raises it",
    ),
)


# A REST API version is numeric and at least ``MAJOR.MINOR`` -- Tableau has published exactly that
# shape from 2.0 to 3.30. The trailing group tolerates a hypothetical third component rather than
# rejecting a real future server; nothing else is admitted.
#
# ⚠️ This is a GRAMMAR, not a membership test, and the difference is load-bearing. `API_RELEASE` is a
# release-name lookup table that stops at 3.29, so requiring membership would classify a real 3.30
# Cloud site -- measured on 2026-08-30 -- as unassessable. A numeric but unpublished version is
# established and compares normally; only text that is not a version at all is refused.
_API_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)*")


def api_tuple(version: str | None) -> tuple[int, ...] | None:
    """Comparable form of a REST version, or ``None`` when ``version`` is not one.

    Never compare these as strings: ``"3.9" > "3.10"``.

    ⚠️ ``None`` rather than a best-effort tuple, because the best effort was measurably worse than
    no answer. This used to pull *arbitrary digit runs* out of whatever it was handed, which turned
    unassessable text into a confident bucket in both directions: ``"garbage-999"`` became ``(999,)``
    and therefore "this server clears the SVG floor, best rung SVG", while ``"not-a-version"`` became
    ``(0,)`` and therefore "below every floor, no reference render is reachable at all". Neither
    server said either thing. A value that is not a version cannot be compared against a floor, and
    :func:`supports` turns that into the ``unknown`` third state instead of guessing.
    """
    if not version:
        return None
    candidate = version.strip()
    if not _API_VERSION_RE.fullmatch(candidate):
        return None
    return tuple(int(part) for part in candidate.split("."))


def tier_priority(name: str | None, tiers: tuple[RenderTier, ...] = LADDER) -> int:
    """How good a selected tier is, as a comparable number -- **higher is better**, ``0`` = none.

    ``LADDER`` is ordered best-first, so a raw index sorts backwards; a caller that compares indexes
    directly picks the WORST tier. Derived from the ladder rather than written out, so a new rung
    cannot be added in one place and forgotten in the comparison.
    """
    names = [tier.name for tier in tiers]
    return len(names) - names.index(name) if name in names else 0


def supports(available: str | None, required: str) -> bool | None:
    """Is ``available`` at or above ``required``? ``None`` when the server did not say.

    "Did not say" now covers three inputs that are the same thing to a caller and were not the same
    thing here: absent, empty, **and present but not an API version**. A ``restApiVersion`` of
    ``garbage-999`` is not evidence that a server clears the SVG floor, and the third state exists so
    that it does not have to be pretended into one of the other two.

    ``required`` is always one of this module's own ladder floors, so an unparsable one is a bug in
    the ladder rather than a fact about a server -- it raises instead of silently marking every rung
    unknown, which is how a broken ladder would otherwise ship looking merely cautious.
    """
    floor = api_tuple(required)
    if floor is None:
        raise ValueError(f"not a REST API version: {required!r}")
    reached = api_tuple(available)
    if reached is None:
        return None
    return reached >= floor


TIER_BY_NAME: dict[str, RenderTier] = {tier.name: tier for tier in LADDER}


def rung_support(ceiling: tuple[int, ...] | None, tiers: tuple[RenderTier, ...] = LADDER) -> dict[str, bool | None]:
    """What a comparable ceiling MEANS per rung -- the capability, separated from the version text.

    ⚠️ **This exists so that a derived capability can be published when the value it came from
    cannot.** ``restApiVersion`` is a server-controlled string, and the previous exemption for it
    ("returned untransformed, because it matched a numeric grammar no credential can satisfy") was an
    *unenforced assumption*, not an enforced property: nothing validates the shape of a Tableau
    session token -- ``assess_estate.Site`` takes ``creds["token"]`` as-is -- so a server or an
    intermediary can issue a token that is literally ``3.27`` and reflect it back here. It satisfies
    the grammar, and it used to be published as this site's advertised ceiling in
    ``assessment.json``, ``report.md`` and the console. Measured.

    The lesson is the same one that retired the ``UNAUTHENTICATED-SOURCE`` category: an exemption may
    rest only on something the code ENFORCES. So the raw string is compared inside
    :func:`server_info` and left there, and this -- three booleans against three published,
    repo-authored floors -- is what travels.

    ⚠️ **Residual, stated rather than hidden.** These booleans do narrow a suppressed version to an
    interval (``svg: False, pdf: True`` places it in ``[2.8, 3.29)``). That is unavoidable for any
    honest report: the operator's question *is* "which rungs can this site reach", and answering it
    at all constrains the value when the value and the credential are the same string. An interval
    across a documented range is not a credential; the exact string was.
    """
    return {tier.name: None if ceiling is None else ceiling >= api_tuple(tier.min_api) for tier in tiers}


# ------------------------------------------------- why one `?format=svg` request came back refused

# The three states any "SVG did not render" message must resolve to. They are the three values of
# `supports(advertised, svg_floor)` and nothing else, so the partition is total by construction
# rather than by a chain of `if`s somebody has to keep exhaustive.
#
# ⚠️ The middle one is named for the EVIDENCE ("the server advertises at or above the floor"), not
# for a cause ("the client pin is too low"), and that is deliberate. When the pin is ALSO at or above
# the floor, "raise TABLEAU_REST_API_VERSION" is itself a remedy that cannot work -- the same defect
# one step over -- so the state names what was measured and the remedy branches inside it.
SVG_CAUSE_SERVER_MEETS_FLOOR = "server_meets_floor"
SVG_CAUSE_SERVER_BELOW_FLOOR = "server_below_floor"
SVG_CAUSE_CEILING_NOT_ESTABLISHED = "ceiling_not_established"

# Output bound for a version string quoted in one of those messages, and for every free-form field
# `server_info` returns. Like every other limit in this repo it bounds the OUTPUT of `redacted_note`,
# never its input. 40 is comfortably above a real `productVersion` ("2025.3.3") and its `build`
# ("20253.25.0904.1234"); a server that sends something longer is not describing itself.
_VERSION_CHARS = 40


@dataclass(frozen=True)
class SvgGate:
    """The numbers that decide WHY ``?format=svg`` was refused, kept together like ``ApiVersions``.

    ``advertised`` is the server's own ceiling from ``/serverinfo``; ``configured`` is the client
    preference we send. Passing one while forgetting the other is exactly how a remedy gets written
    that raises a client pin above a ceiling that cannot move, so they travel as one value.

    ``proved_by_reprobe`` is the ONLY thing that licenses saying the tier works on this server: the
    ladder's floor re-probe actually asked. An advertised number at or above the floor licenses
    "try raising the pin", never "SVG works here" -- see :func:`_add_pin_warnings`.
    """

    advertised: str | None = None
    configured: str | None = None
    product_version: str | None = None
    proved_by_reprobe: bool = False


@dataclass(frozen=True)
class SvgGateAdvice:
    """One classified cause and the ONE remedy that can actually work for it."""

    cause: str
    remedy: str


def svg_gate_advice(gate: SvgGate, *, redactor=None) -> SvgGateAdvice:
    """Say why SVG was refused **and** what would fix it, without ever naming a knob that cannot.

    Measured on a real customer site (an on-prem Tableau Server, ``productVersion 2025.3.3`` /
    ``restApiVersion 3.27``): the message this replaces said *"Set TABLEAU_REST_API_VERSION=3.29 in
    .env and re-run"*, which is arithmetically impossible there -- a client preference cannot lift a
    server's ceiling. It was also the loudest, most actionable-looking line in the run.

    So the three states below are the three values of :func:`supports`, and the *unknown* one is a
    first-class outcome rather than a fall-through into either confident branch: with no
    ``/serverinfo`` answer the cause genuinely is not established, and saying so beats guessing.

    ⚠️ Two things this deliberately does NOT claim. It never says SVG *works* on a server merely
    because the advertised number clears the floor -- only ``proved_by_reprobe`` licenses that. And
    it says nothing about what raising the pin above a server's ceiling does to OTHER calls: Tableau
    documents an unsupported-version error, but nobody here has measured it, and this repo states
    measurements. What is provable is the only thing asserted -- it cannot enable SVG.

    ``redactor`` is not decoration: every version string quoted below is response-derived (it came
    back from ``/serverinfo``), so it goes through :func:`tableau_env.redacted_note`, the chokepoint,
    before anything formats it.
    """
    svg, pdf = TIER_BY_NAME["svg"], TIER_BY_NAME["pdf"]
    advertised = redacted_note(gate.advertised, redactor, limit=_VERSION_CHARS)
    configured = redacted_note(gate.configured, redactor, limit=_VERSION_CHARS)
    product = redacted_note(gate.product_version, redactor, limit=_VERSION_CHARS)
    # Classification reads the RAW value, reporting reads the redacted copy -- never the reverse.
    meets_floor = supports(gate.advertised, svg.min_api)
    next_rung = (
        f"{pdf.name.upper()} -- available from API {pdf.min_api} ({pdf.min_release}), {pdf.note} -- "
        f"with --pdf or --reference-best"
    )
    if meets_floor is None:
        return SvgGateAdvice(
            SVG_CAUSE_CEILING_NOT_ESTABLISHED,
            f"this server's advertised REST ceiling was NOT established on this run, so why it "
            f"refused is UNKNOWN and there is no single remedy. IF this site advertises "
            f"{svg.min_api} or later, raising TABLEAU_REST_API_VERSION to {svg.min_api} in .env "
            f"fixes it; if it advertises less, SVG is unavailable at any client setting and the "
            f"next rung is {next_rung}. Establish the ceiling first: re-run with --reference-best, "
            f"or GET /api/{SERVERINFO_PROBE_VERSION}/serverinfo and read <restApiVersion>.",
        )
    if meets_floor:
        return SvgGateAdvice(SVG_CAUSE_SERVER_MEETS_FLOOR, _meets_floor_remedy(gate, svg, advertised, configured))
    return SvgGateAdvice(
        SVG_CAUSE_SERVER_BELOW_FLOOR,
        f"this server advertises REST {advertised}{f' (product {product})' if product else ''}, and "
        f"SVG needs {svg.min_api} ({svg.min_release}) -- so SVG is UNAVAILABLE on this server at any "
        f"client setting, and raising TABLEAU_REST_API_VERSION above the server's advertised ceiling "
        f"is not a fix. Capture the next rung instead: {next_rung}.",
    )


def _meets_floor_remedy(gate: SvgGate, svg: RenderTier, advertised: str, configured: str) -> str:
    """The remedy when the SERVER clears the floor -- which is not yet a reason to blame the pin.

    ⚠️ **The invariant is that no remedy may ever name a version the advertised ceiling cannot
    serve**, and "at or above the floor" is not the same as "unbounded". This said *"set it to 3.29
    or later"*, which on a server advertising **exactly** 3.29 -- a real shape, that is precisely what
    Server 2026.2 reports -- makes every "later" value the same impossible configuration #474 exists
    to remove, one case in from the edge. Naming the floor itself is the only number that is safe for
    every server in this branch: ``meets_floor`` is true, so ``advertised >= svg.min_api``, so the
    floor is at or below this server's ceiling by construction.
    """
    proof = (
        f"A floor re-probe at API {svg.min_api} PROVED the tier answers on this server."
        if gate.proved_by_reprobe
        else (
            f"The advertised {advertised} is what the server CLAIMS it can do, not proof that SVG "
            f"works here; only the request settles that."
        )
    )
    if supports(gate.configured, svg.min_api):
        # Both numbers already clear the floor, so neither explains the refusal. Printing the .env
        # knob here would be the same false remedy this function exists to remove, one case over.
        return (
            f"this server advertises REST {advertised} and TABLEAU_REST_API_VERSION is already "
            f"{configured} -- both at or above the {svg.min_api} SVG floor -- so this refusal is NOT "
            f"explained by either version and raising the pin further will not help. Report it with "
            f"the response detail recorded beside this leg."
        )
    pin = f"pinned to {configured}" if configured else "not recorded on this run"
    return (
        f"this server advertises REST {advertised} -- {release_for(advertised)} -- at or above the "
        f"{svg.min_api} SVG floor, while TABLEAU_REST_API_VERSION is {pin}: set it to exactly "
        f"{svg.min_api} in .env and re-run -- the SVG floor itself, which is the highest number this "
        f"remedy can name without possibly exceeding the server's own ceiling. {proof}"
    )


def server_info(base: str, *, timeout: int = SERVERINFO_TIMEOUT_SEC, redactor=redact) -> dict[str, Any]:
    """The server's own account of itself. **Unauthenticated** -- callable before any sign-in.

    Deliberately fails soft: a site that will not answer ``serverinfo`` is not a reason to abandon a
    capture, because the endpoint probe below is the authoritative check anyway. This only supplies
    the *advertised* number, whose whole value is being compared against what actually happens.

    ⚠️ Failing soft was a *claim* until this went through the shared primitive. A malformed status
    line raises ``http.client.BadStatusLine``, which is not an ``OSError``, so the previous
    ``except (OSError, urllib.error.URLError)`` let it escape as an uncaught traceback -- the opposite
    of failing soft, measured at exit 1.

    ``redactor`` defaults to bare :func:`tableau_env.redact`, which still applies the
    ``X-Tableau-Auth`` header rule with no secrets configured. This request carries no credential, so
    nothing of ours *can* be reflected here -- but every caller in this module has an env in hand and
    passes :func:`tableau_env.env_redactor` anyway, because "it cannot leak" is an argument that has
    to be re-made every time the call site moves, and passing the redactor is one line.

    ⚠️ **That argument is not merely fragile, it is WRONG, and this function acted on it.** "This
    request is unauthenticated, so the response cannot reflect a credential" ignores who is speaking:
    the same server -- or any intermediary on the path -- already observed the PAT sign-in, and
    nothing stops it echoing that credential in a later unauthenticated response. Measured on this
    branch: a synthetic session token placed in ``productVersion`` and its ``build`` attribute of a
    perfectly ordinary 200 arrived verbatim in ``assessment.json``, in ``report.md`` and on the
    console, because the redactor in hand was applied to one of the four fields.

    ⚠️ **NO response string returned by this function escapes the redactor -- including the version.**
    The second attempt exempted ``restApiVersion`` on the grounds that a numeric API-version grammar
    is a shape "no credential can satisfy". Nothing enforces that: a Tableau session token has no
    validated shape at all (``assess_estate.Site`` accepts ``creds["token"]`` as-is), so a token that
    is literally ``3.27`` passes the grammar and was published as the site's advertised ceiling on
    all three surfaces. Measured, and it is the same defect class as the ``UNAUTHENTICATED-SOURCE``
    exemption that preceded it: an assumption standing where an enforced property was needed.

    So the raw value is compared **inside this function** and never leaves it. What leaves is a
    redacted display string plus a *derived* capability -- :func:`rung_support` and
    ``ceiling_established``, computed from the raw -- so a reflected credential costs the operator
    the printed NUMBER and nothing else: the ceiling is still established and every rung verdict is
    still correct. ``rest_api_version_reflected`` says which happened, so a report can state that the
    number was suppressed instead of printing a redaction marker where a version belongs.

    ⚠️ **A version field is trusted only from a SUCCESSFUL response, and only when it is a version.**
    Both halves were measured missing: the parse ran regardless of HTTP status, so a **500** or a
    **404** whose body happened to carry ``<restApiVersion>3.30</restApiVersion>`` -- the shape a
    proxy error page or a cached body can have -- was reported as this server's advertised ceiling;
    and any nonempty text was accepted, so ``garbage-999`` became a ceiling that clears the SVG
    floor. A non-200 therefore returns the status and **no** version fields, and a 200 whose
    ``restApiVersion`` fails the grammar returns ``rest_api_version: None`` plus
    ``invalid_rest_api_version`` -- the probe stays diagnostic, the ceiling becomes *unknown*, and
    :func:`supports` marks every rung unknown rather than confidently available or confidently
    unavailable.
    """
    url = f"{base.rstrip('/')}/api/{SERVERINFO_PROBE_VERSION}/serverinfo"
    status, payload, _headers = _request(urllib.request.Request(url), timeout=timeout, redactor=redactor)
    if status == NETWORK_ERROR_STATUS:
        return {"status": 0, "error": payload.decode("utf-8", "replace")}
    if status != http.HTTPStatus.OK:
        # An unsuccessful response's body is not the server's account of itself. Keep the status --
        # that IS the diagnostic, and it is what the "not established" wording quotes.
        return {"status": status, "product_version": None, "build": None, "rest_api_version": None}
    body = payload.decode("utf-8", "replace")

    def grab(pattern: str) -> str | None:
        match = re.search(pattern, body)
        return match.group(1) if match else None

    advertised = grab(r"<restApiVersion>([^<]+)<")

    def grab_redacted(pattern: str) -> str | None:
        """A FREE-FORM ``/serverinfo`` field, redacted at the parse boundary. ``None`` stays ``None``.

        `redacted_note` maps a missing value onto ``""``; keeping ``None`` distinct matters because
        "the element was absent" and "the server sent an empty string" are different facts, and
        every consumer here tests truthiness on the result.
        """
        raw = grab(pattern)
        return None if raw is None else redacted_note(raw, redactor, limit=_VERSION_CHARS)

    # The derived capability is computed from the RAW value and travels on its own; the raw value
    # itself never leaves this function. See :func:`rung_support` for why.
    ceiling = api_tuple(advertised)
    shown = None if advertised is None else redacted_note(advertised, redactor, limit=_VERSION_CHARS)
    reflected = advertised is not None and shown != advertised
    info: dict[str, Any] = {
        "status": status,
        # ⚠️ Redacted HERE, not at the three places that print them. `productVersion` and its `build`
        # attribute are unconstrained free-form strings -- a version number is merely what a
        # well-behaved server puts there -- and they reach `assessment.json`, `report.md` and the
        # console. Measured on this branch before the fix: a session token reflected in BOTH fields
        # arrived verbatim in all three. Redacting at the consumers instead would leave the next
        # consumer unprotected, which is how this route stayed open while the neighbouring one was
        # closed twice.
        "product_version": grab_redacted(r"<productVersion[^>]*>([^<]+)<"),
        "build": grab_redacted(r'<productVersion[^>]*build="([^"]+)"'),
        # ⚠️ The DISPLAY value, and nothing else -- it has been through the redactor exactly like the
        # two above. In every ordinary case the redactor changes nothing and this IS the advertised
        # version, byte for byte. When it does change something, that is the redactor telling us the
        # server just quoted a configured credential back at us, and the number is suppressed while
        # the capability below survives.
        "rest_api_version": shown if ceiling is not None else None,
        # The redactor rewrote the advertised version, i.e. the server reported a value that matches
        # a credential we hold. Kept as a first-class fact so a report can SAY the number was
        # suppressed rather than silently print a redaction marker where a version belongs.
        "rest_api_version_reflected": reflected,
        # Derived from the raw value, so it stays true even when the number cannot be shown.
        "ceiling_established": ceiling is not None,
        "rung_support": rung_support(ceiling),
    }
    if advertised is not None and ceiling is None:
        # Kept, redacted, as the reason the ceiling is unknown -- an operator who is told only
        # "unknown" re-runs the same probe; one who is told WHAT came back stops guessing.
        info["invalid_rest_api_version"] = shown
    return info


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

    **Every** attacker-influenced value in a returned ``detail`` comes from
    :func:`tableau_env.redacted_note`, which redacts the whole value before anything truncates,
    strips, folds or extracts from it. The ``<detail>`` extraction below is why that matters here and
    not only in ``format_matches``: pulling a capture group out of the RAW body is a transformation
    like any other, and a secret straddling ``</detail>`` would be split by it, leaving each half
    unmatched by a literal redactor. The regex now runs on the redacted copy.
    """
    if status == 200:
        if kind:
            ok, why = format_matches(kind, body, content_type, redactor=redactor)
            if not ok:
                # A 200 with the wrong payload is the on-prem trap: an older server ignores an unknown
                # `format=svg` and hands back its default PNG. Selecting this rung would persist PNG
                # bytes in a `.svg` and call them vector.
                #
                # `why` arrives FULLY redacted from `format_matches`, so the bound below is an output
                # cap, not a guard. A second redactor call here was deleted rather than kept as
                # defence in depth: it would only ever see already-transformed text, which is exactly
                # the shape that made the previous outer guard incapable of guarding.
                return "indeterminate", f"HTTP 200 but {why}"[:180]
        return "available", ""
    text = body.decode("utf-8", "replace")
    # `_ERROR_BODY_CHARS` bounds the OUTPUT of redaction, not its input. Cutting it can only lose
    # diagnostic text (Tableau's error bodies are well under 1 KB); it can never leave a secret behind,
    # because the redactor has already seen the whole body by the time it applies.
    safe = redacted_note(body, redactor, limit=_ERROR_BODY_CHARS)
    detail = (re.findall(r"<detail>(.*?)</detail>", safe, re.S) or [safe])[0][:180].strip()
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
                f"but TABLEAU_REST_API_VERSION is pinned to {versions.configured}; set it to exactly "
                f"{tier.min_api} to use it"
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


def probe_render_capability(
    session,
    env: dict[str, str],
    views: list[dict[str, Any]],
    max_age: int = DEFAULT_MAX_AGE_MINUTES,
) -> dict[str, Any]:
    """Ask the SITE what it can render, by probing, and reconcile that with both version strings.

    ``session`` is duck-typed on purpose -- anything exposing ``site_id``, ``raw_get(path, api=...)``
    and ``redact_text(text)`` will do. That is what lets this orchestration live beside the ladder it
    drives without importing ``capture_tableau_oracle``, which imports THIS module.

    The probe view matters. A workbook whose data sources are not connected fails every route
    identically, so probing one would report "no tier available" for a site that is perfectly capable
    -- so try successive views until one gives a determinate answer, capped at
    ``MAX_CAPABILITY_PROBE_VIEWS`` because each attempt costs metered export calls.
    """
    valid_max_age = validate_max_age(max_age)
    info = server_info(env["TABLEAU_SERVER_URL"], redactor=env_redactor(env, getattr(session, "token", "") or ""))
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
            delim = "&" if "?" in query else "?"
            full_query = f"{query}{delim}maxAge={valid_max_age}"
            return session.raw_get(f"/sites/{session.site_id}/views/{view_luid}/{endpoint}{full_query}", api=api)

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
    best["max_age_minutes"] = valid_max_age
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

    Deliberately not imported from ``capture_tableau_oracle``: that module imports THIS one, and
    reaching back would make the pair cyclic. It is also what keeps this module usable on its own --
    the capability question is asked before, and independently of, any capture. What is **no longer**
    duplicated is the HTTP round trip itself: it comes from ``tableau_http``, the one hardened path,
    because three review rounds each found a different hole in this function's hand-rolled copy.
    """

    def redactor(text: str) -> str:
        return redact(text, pat_secret_value, pat_name)

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
    # ⚠️ THE REQUEST WE JUST SENT CONTAINS THE PAT, so everything the server hands back is
    # attacker-influenceable -- the status line and the redirect headers included, which is why this
    # goes through `_request` rather than a local `try`. `_request` never raises for anything the
    # network or the server can do, so the only failure that leaves here is the RuntimeError below.
    status, payload, _headers = _request(req, timeout=SIGNIN_TIMEOUT_SEC, redactor=redactor)
    if status == 200:
        creds = json.loads(payload)["credentials"]
        return creds["token"], creds["site"]["id"]
    # The REASON PHRASE is **not reported at all**, and that is a deletion rather than a stricter
    # redactor, because full-literal redaction cannot survive a SPLIT: a proxy that puts half a
    # credential in the reason and half in the body defeats two independent redactors -- neither
    # surface holds the whole literal, both fragments survive, and they are printed side by side.
    # Measured:
    #     HTTP 403 SYNTHETIC_PAT_SECR ET_REASON_SPLIT_42     reconstructs=True
    # Detecting a fragment is not solvable -- a short fragment is indistinguishable from ordinary
    # text -- so the only defence is to emit fewer server-controlled strings. The phrase below is
    # derived from the numeric status by OUR OWN table, carries the same information, and cannot be
    # influenced. `origin/master` reaches the same place by discarding the reason entirely.
    #
    # The BODY is still reported, redacted: it is the one surface that carries Tableau's own
    # actionable error text, and it is now the ONLY attacker-influenced string here, so there is no
    # second surface to split across.
    where = "a network error" if status == NETWORK_ERROR_STATUS else f"HTTP {status} {_canonical_phrase(status)}"
    raise RuntimeError(
        f"Tableau sign-in failed: {where}. "
        f"Check the PAT NAME and SECRET (two values). "
        f"{redacted_note(payload, redactor, limit=200)}"
    ) from None


def _canonical_phrase(code: int) -> str:
    """OUR name for an HTTP status, never the server's.

    ``HTTPStatus`` is a fixed table in the standard library, so the string is chosen by the status
    number and nothing a proxy sends can steer it.
    """
    try:
        return http.HTTPStatus(code).phrase
    except ValueError:
        return "Unknown Status"


# Six parameters that map 1:1 to distinct concerns -- four URL components, the auth header, and the
# scrubber for this call's diagnostics. Grouping any two would be arbitrary, and the sixth exists
# precisely because hand-picking a subset of the live credentials is the round-9 defect. Waived
# deliberately, in the same spirit as `capture_tableau_oracle.TableauSession._request`.
def _cli_fetch(  # pylint: disable=too-many-arguments
    base: str,
    api: str,
    site_id: str,
    view_luid: str,
    token: str,
    *,
    max_age: int = DEFAULT_MAX_AGE_MINUTES,
    redactor=None,
):
    """Fetcher for the standalone report path. Returns ``(status, body, content_type)``.

    ``redactor`` should cover **every** credential live at this point, not just ``token``: the PAT
    secret went to this same host at sign-in, so a reflecting proxy can echo it into any later
    response. It defaults to a token-only redactor so the function stays usable standalone, and
    ``_build_report`` passes the full one.
    """
    scrub = redactor or (lambda text: redact(text, token))
    valid_max_age = validate_max_age(max_age)

    def fetch(endpoint: str, query: str, api_override: str | None = None) -> tuple[int, bytes, str | None]:
        version = api_override or api
        delim = "&" if "?" in query else "?"
        full_query = f"{query}{delim}maxAge={valid_max_age}"
        url = f"{base.rstrip('/')}/api/{version}/sites/{site_id}/views/{view_luid}/{endpoint}{full_query}"
        req = urllib.request.Request(url)
        req.add_header("X-Tableau-Auth", token)
        # The session token rides in a header a reflecting proxy can echo into a status line or a
        # redirect, so this uses the one hardened path too -- the round-9 finding was that it did not.
        status, body, headers = _request(req, timeout=CLI_FETCH_TIMEOUT_SEC, redactor=scrub)
        return status, body, header_value(headers, "Content-Type")

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
    from tableau_env import pat_secret  # pylint: disable=import-outside-toplevel

    base = env["TABLEAU_SERVER_URL"]
    configured = env.get("TABLEAU_REST_API_VERSION", "3.21")
    token, site_id = sign_in(base, env["TABLEAU_SITE"], env["TABLEAU_PAT_NAME"], pat_secret(env), configured)
    # Everything printed or serialised from here is scrubbed. A proxy or WAF that echoes request
    # headers puts a LIVE session token in an error body, and this report is written to disk. The same
    # redactor goes down into the fetcher, so the HTTP layer's own diagnostics are covered by the
    # identical secret list rather than by whichever subset that call site happened to hold.
    redactor = env_redactor(env, token)
    max_age = validate_max_age(getattr(args, "max_age", DEFAULT_MAX_AGE_MINUTES))
    report = detect(
        _cli_fetch(base, configured, site_id, args.view, token, max_age=max_age, redactor=redactor),
        args.view,
        ApiVersions(configured=configured, advertised=info.get("rest_api_version")),
        redactor=redactor,
    )
    report["server"] = info
    report["max_age_minutes"] = max_age
    return report


def _arg_max_age(val: str) -> int:
    try:
        parsed = int(val)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--max-age must be an integer >= 1, got {val!r}") from exc
    try:
        return validate_max_age(parsed)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    """One-shot capability report for a site, so an operator can answer 'what will I get here?'."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--view", required=True, help="view LUID to probe (use one that renders)")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials file")
    parser.add_argument(
        "--max-age",
        type=_arg_max_age,
        default=DEFAULT_MAX_AGE_MINUTES,
        metavar="MIN",
        help=(
            f"maximum cache age in minutes for Tableau server-side query cache "
            f"(default {DEFAULT_MAX_AGE_MINUTES}, minimum 1)"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tableau_env import require, resolve_env  # pylint: disable=import-outside-toplevel

    env = resolve_env(args.env)
    require(env)
    info = server_info(env["TABLEAU_SERVER_URL"], redactor=env_redactor(env))
    _log_versions(info, env.get("TABLEAU_REST_API_VERSION", "3.21"))
    report = _build_report(env, args, info)
    print(json.dumps(report, indent=2))
    for warning in report["warnings"]:
        LOG.warning("! %s", warning)
    return 0 if report["selected_tier"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
