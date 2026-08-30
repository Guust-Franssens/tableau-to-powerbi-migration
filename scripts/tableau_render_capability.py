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


def classify_probe(status: int, body: bytes) -> tuple[str, str]:
    """Turn one probe response into ``(verdict, detail)``.

    Three outcomes have to stay distinct, and collapsing any two of them is the failure this whole
    module exists to prevent:

    ``available``    the route answered with content;
    ``unsupported``  the SERVER refused the feature by version -- a real, permanent "no" for this tier;
    ``indeterminate``something else went wrong (a blocked view, a dead credential, a gateway blip), so
                     this probe says **nothing** about the tier. Reporting that as "no" would silently
                     demote a site that is perfectly capable, which is the unassessable-collapsing-
                     into-a-clean-answer shape.
    """
    if status == 200:
        return "available", ""
    text = body.decode("utf-8", "replace")
    detail = (re.findall(r"<detail>(.*?)</detail>", text, re.S) or [text])[0][:180].strip()
    if VERSION_GATE_MARKER in text:
        return "unsupported", detail
    if UNRENDERABLE_MARKER in text:
        return "indeterminate", detail
    return "indeterminate", detail or f"HTTP {status}"


def detect(
    fetch,
    view_luid: str,
    *,
    configured_api: str | None = None,
    advertised_api: str | None = None,
    tiers: tuple[RenderTier, ...] = LADDER,
) -> dict[str, Any]:
    """Walk the ladder against ONE view and report which rung actually answers.

    ``fetch(endpoint, query) -> (status, body)`` is injected so this is testable without a network and
    so the caller keeps its own retry/re-auth policy. Probing stops at the first ``available`` rung --
    the ladder is ordered by fidelity, so a lower rung cannot change the answer, and every extra call
    is metered by Tableau (~100 exports/hour/Creator).

    Returns the chosen tier, the per-tier verdicts, and the **three-way version reconciliation**, so a
    consumer can see the difference between "this server cannot" and "we did not ask properly".
    """
    verdicts, chosen = [], None
    for tier in tiers:
        if chosen is not None:
            verdicts.append({"tier": tier.name, "verdict": "not_probed", "detail": "a better tier already answered"})
            continue
        status, body = fetch(tier.endpoint, tier.query)
        verdict, detail = classify_probe(status, body)
        verdicts.append(
            {
                "tier": tier.name,
                "verdict": verdict,
                "detail": detail,
                "min_api": tier.min_api,
                "min_release": tier.min_release,
            }
        )
        if verdict == "available":
            chosen = tier.name

    report: dict[str, Any] = {
        "probe_view_luid": view_luid,
        "configured_api_version": configured_api,
        "advertised_api_version": advertised_api,
        "selected_tier": chosen,
        "tiers": verdicts,
        "warnings": [],
    }
    if chosen is None:
        # Never say "no tier is available" when nothing actually refused us. An all-indeterminate walk
        # means the PROBE VIEW was unusable (a workbook whose sources are disconnected fails every
        # route identically), not that the site lacks the feature.
        if all(v["verdict"] == "indeterminate" for v in verdicts):
            report["selected_tier"] = None
            report["warnings"].append(
                "capability UNDETERMINED: every route failed for a reason that is not a version gate "
                "(most likely this probe view's data sources are not connected). Re-probe with a "
                "different view before concluding anything about this site."
            )
        else:
            report["warnings"].append("no render tier is available on this site")

    # The reconciliation that a version string alone cannot give you: the server can do it, but we
    # asked in a way that forbids it. Silent otherwise -- the endpoint's own error names the feature's
    # floor, not the fact that OUR pin is what put us below it.
    for tier in tiers:
        refused = next((v for v in verdicts if v["tier"] == tier.name and v["verdict"] == "unsupported"), None)
        if refused and supports(advertised_api, tier.min_api) and supports(configured_api, tier.min_api) is False:
            report["warnings"].append(
                f"tier '{tier.name}' is supported by this server (advertises API {advertised_api}) but "
                f"TABLEAU_REST_API_VERSION is pinned to {configured_api}; set it to {tier.min_api} or "
                f"later to use it"
            )
    return report


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
    """Fetcher for the standalone report path."""

    def fetch(endpoint: str, query: str) -> tuple[int, bytes]:
        url = f"{base.rstrip('/')}/api/{api}/sites/{site_id}/views/{view_luid}/{endpoint}{query}"
        req = urllib.request.Request(url)
        req.add_header("X-Tableau-Auth", token)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (OSError, urllib.error.URLError) as exc:
            return 0, f"{type(exc).__name__}: {exc}".encode()

    return fetch


def main(argv: list[str] | None = None) -> int:
    """One-shot capability report for a site, so an operator can answer 'what will I get here?'."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--view", required=True, help="view LUID to probe (use one that renders)")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials file")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tableau_env import pat_secret, require, resolve_env  # pylint: disable=import-outside-toplevel

    env = resolve_env(args.env)
    require(env)
    base = env["TABLEAU_SERVER_URL"]
    configured = env.get("TABLEAU_REST_API_VERSION", "3.21")
    info = server_info(base)
    LOG.info(
        "server: product=%s build=%s advertises REST %s (%s); we ask as %s (%s)",
        info.get("product_version"),
        info.get("build"),
        info.get("rest_api_version"),
        release_for(info.get("rest_api_version") or ""),
        configured,
        release_for(configured),
    )
    token, site_id = sign_in(base, env["TABLEAU_SITE"], env["TABLEAU_PAT_NAME"], pat_secret(env), configured)
    report = detect(
        _cli_fetch(base, configured, site_id, args.view, token),
        args.view,
        configured_api=configured,
        advertised_api=info.get("rest_api_version"),
    )
    report["server"] = info
    print(json.dumps(report, indent=2))
    for warning in report["warnings"]:
        LOG.warning("! %s", warning)
    return 0 if report["selected_tier"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
