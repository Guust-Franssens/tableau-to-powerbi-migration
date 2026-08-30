"""
purpose: capture Tableau's OWN computed values per view (the numeric oracle) plus a durable
         view-identity manifest keyed by view LUID, from a live Tableau Cloud/Server site.
usage:   python scripts/capture_tableau_oracle.py --out _oracle [--workbook "Superstore"] [--images] [--svg]

Why this exists
---------------
Two sockets in the migration toolchain are empty, and only a live site can fill them:

1. **Tableau's own numbers.** The deterministic engine's ``fidelity_oracle.py`` has a value tier, but
   it reads *Power BI* values from a local Analysis Services instance -- there is no Tableau-side
   number anywhere in the pipeline. ``/views/{id}/data`` returns the aggregated, as-displayed values
   Tableau itself computed, which is strictly better evidence than re-deriving them ourselves: it is
   immune to the self-consistency trap where a shared assumption hides in both sides of a comparison.

2. **A durable view identity.** ``migrate_estate.py`` keys by workbook name and carries
   ``workbook_luid`` (its ``by_workbook_luid`` index maps to the emitted report folder), but nothing
   persists a **view** LUID -- reference images land as ``<worksheet name>.png``. View LUID is exactly
   the join key ``/views/{id}/data`` needs, so it has to survive capture or the oracle cannot bind a
   Tableau number back to the visual it came from.

Capture is deliberately **raw**: values are stored exactly as Tableau returned them. ``/data`` yields
*display-formatted* text (``"19.5%"``, ``"$12"``), not raw floats, and includes Tableau-generated
fields (``Latitude (generated)``) that have no counterpart in a migrated model. Normalising here would
bake a comparison decision into the evidence; instead the manifest records advisory format hints and
leaves normalisation to whoever compares.

Tableau Cloud session behaviour (measured, see repo memory + upstream issue #97)
-------------------------------------------------------------------------------
A single REST session can start returning ``401002 Unauthorized Access`` on view-export endpoints
after an unpredictable number of exports (observed after 1, 2, 3 and 6 in one sitting -- yet also 58
consecutive exports with none, so it is intermittent, not a fixed quota). Once it starts, even
metadata calls on that token fail. This script re-authenticates on ``401002``.

Failure handling is classified, because the right response differs completely:

============== ============================================== ==========================
class          example                                        response
============== ============================================== ==========================
session_lost   ``401002`` mid-loop                             re-authenticate, retry
transient      gateway ``502/503/504``, ``429``, conn. reset   exponential backoff + jitter
source_credent ``400081`` FederatedDataSourceException         **STOP** -- ask a human
failed         anything else                                   record and move on
============== ============================================== ==========================

A missing credential is **not** transient: no number of retries conjures one, so it fails fast with a
named host and remedy and sets exit code 2. Every recovery is **recorded** in the manifest
(``reauths``, ``retries``, ``retry_reasons``) -- a capture that silently healed itself looks identical
to one that never had a problem, which is exactly how a truncated result comes to be trusted.

Reference RENDERS: two routes, one endpoint (issue #403)
--------------------------------------------------------
``--images`` and ``--svg`` both call ``/views/{id}/image``; only the query differs. Measured across
all 52 capturable dashboards on the trial site, with no exception:

* ``?resolution=high`` returns **exactly 2x the dashboard's declared size** -- 1300x1600 for a 650x800
  dashboard, 2800x1600 for a 1400x800 one. There is no parameter that raises it: ``resolution`` accepts
  only ``high`` (``standard``, ``veryhigh`` and even ``HIGH`` are HTTP 400), and ``vizWidth``/
  ``vizHeight`` are silently ignored (byte-identical responses). **That is a hard raster ceiling**, and
  it is why a text-dense dashboard -- ``Superstore | Order Details`` carries 410 labels in 1600x1600 --
  can be structurally legible and content-illegible at the same time.
* ``?format=svg`` (REST **3.29+**) returns vector: resolution-independent, self-contained (raster
  sub-elements arrive as ``data:`` URIs, external refs measured 0), and its ``<text>`` elements hold
  the literal label strings, so a consumer can read the dashboard's CONTENT without rendering at all.
  Below 3.29 the server refuses with an explicit ``SVG export requires API version 3.29 or later``
  400 -- it never silently downgrades to PNG.

Neither survives a workbook whose data sources are not connected: ``image``, ``image?format=svg``,
``pdf`` and ``data`` all return the same HTTP 400 ``ExportViewException: Error: data sources not
connected``. That failure is upstream of the output format, and only a human can clear it.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import io
import json
import logging
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tableau_render_capability as capability  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_env import pat_secret, redact, require, resolve_env  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("tableau-oracle")

REST_TIMEOUT_SEC = 180
SESSION_LOST_CODE = "401002"
MAX_REAUTH_PER_VIEW = 2
# A capability probe costs metered export calls (Tableau meters ~100/hour/Creator), so it is bounded.
# More than one is still needed because a single blocked view fails every route and would otherwise be
# read as "this site cannot render", which is exactly the wrong conclusion.
MAX_CAPABILITY_PROBE_VIEWS = 3
DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE_SEC = 1.0
BACKOFF_CAP_SEC = 30.0
# The retry budget is a RETRY-ADMISSION DEADLINE, not a hard wall-clock cap. export() charges it from
# monotonic() BEFORE the first attempt, and only AFTER an attempt returns does it admit another retry
# -- while monotonic() is still inside the deadline. A socket timeout takes the whole REST_TIMEOUT_SEC
# to return, and _request() reports it as a *transient* NETWORK_ERROR_STATUS (it does not raise), so
# it is retry-eligible -- yet a budget at or below one REST_TIMEOUT_SEC is already spent by the time
# that full-timeout failure returns, so THAT failure (the commonest on a slow/proxied/VPN link) gets
# ZERO retries at any --max-attempts (issue #197, field-reported, reproduced with a virtual clock).
# The floor below which a full-timeout failure cannot be retried is therefore ONE timeout plus the
# first backoff -- NOT 2x. 2x is neither the minimum needed to admit a retry (a budget between 1x and
# 2x retries a slow failure fine) nor enough to fit two COMPLETE full-timeout attempts once backoff is
# counted (2*180 + backoff > 360). Faster transients -- an immediate 5xx, a short Retry-After -- still
# retry until the budget is spent, so a smaller budget is a deliberate choice, not a bug. The default
# sits comfortably above the floor; both are expressed via REST_TIMEOUT_SEC so they cannot drift.
RETRY_ADMISSION_FLOOR_SEC = REST_TIMEOUT_SEC + BACKOFF_BASE_SEC
DEFAULT_RETRY_BUDGET_SEC = 2.0 * REST_TIMEOUT_SEC

# Status 0 is our own marker for a network-level failure (reset, DNS, gateway timeout) that never
# produced an HTTP status at all. Tableau Cloud sits behind a gateway that intermittently 502/504s.
NETWORK_ERROR_STATUS = 0
TRANSIENT_STATUSES = frozenset({NETWORK_ERROR_STATUS, 429, 500, 502, 503, 504})

_PERCENT = re.compile(r"^-?[\d,.]+%$")
_CURRENCY = re.compile(r"^-?[$£€¥]\s?[\d,.]+$")
_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")

# SVG export is gated by REST version. Below 3.29 the server refuses with this phrase and a 400 --
# it does NOT silently fall back to PNG (measured on 3.21 / 3.24 / 3.28), so the sniff is safe.
SVG_MIN_API_VERSION = "3.29"
SVG_VERSION_MARKER = "SVG export requires API version"
_SVG_ROOT_MM = re.compile(r'width="([\d.]+)mm"\s+height="([\d.]+)mm"')
_SVG_HREF = re.compile(r'(?:xlink:)?href="([^"]{0,120})')
_PDF_MEDIABOX = re.compile(rb"/MediaBox\s*\[([^\]]*)\]")


class ExportFailed(RuntimeError):
    """A view export did not return data. ``kind`` classifies what a caller should do about it."""

    def __init__(self, message: str, kind: str, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def classify_export_error(status: int, text: str) -> tuple[str, str]:
    """Map a Tableau failure to an actionable class. The distinction drives whether we retry.

    Order matters. ``401002`` is our session dying and is fixed by re-authenticating. A transient
    status (gateway 5xx, 429, or a network-level failure) is fixed by waiting. A
    ``FederatedDataSourceException`` naming an expired OAuth token or a connection that "needs
    attention" is Tableau itself being unable to query the underlying source -- **a missing credential
    is not transient**, so retrying burns time and still cannot succeed; only a human can fix it.
    Transient is checked *before* the credential markers so a 503 whose body happens to mention
    authentication is still retried rather than misfiled as a permanent credential block.
    """
    if SESSION_LOST_CODE in text:
        return "session_lost", ""
    if status in TRANSIENT_STATUSES:
        label = "network error" if status == NETWORK_ERROR_STATUS else f"HTTP {status}"
        return "transient", f"{label}: {text[:150]}"
    credential_markers = (
        "FederatedDataSourceException",
        "OAuth refresh token",
        "need attention",
        "needs attention",
        "Invalid username or password",
        "authentication",
    )
    if any(marker.lower() in text.lower() for marker in credential_markers):
        match = re.search(r"([\w.-]+\.(?:com|net|io|azuredatabricks\.net)[^:\s]*):\s*(Tableau[^<\n]{0,180})", text)
        detail = f"{match.group(1)}: {match.group(2).strip()}" if match else text[:200]
        return "source_credential", detail.split("tableau_error_source=")[0].strip()
    return "failed", f"HTTP {status}: {text[:200]}"


def backoff_delay(attempt: int, retry_after: str | None = None, *, jitter: bool = True) -> float:
    """Exponential backoff with full jitter, honouring a server-supplied ``Retry-After``.

    Jitter matters even for a sequential capture: without it, a whole estate run that trips a rate
    limit retries in lockstep with any other client behind the same gateway.
    """
    if retry_after:
        try:
            return min(float(retry_after), BACKOFF_CAP_SEC)
        except ValueError:
            pass
    delay = min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
    return delay * (0.5 + random.random() / 2) if jitter else delay


@dataclass(frozen=True)
class SiteCredentials:
    """Everything needed to reach one Tableau site. The PAT secret is never logged or serialised."""

    base: str
    site: str
    pat_name: str
    pat_secret: str
    version: str


@dataclass(frozen=True)
class RetryPolicy:
    """Bounds on recovery. Both bounds matter: attempts alone cannot stop a slow-failing endpoint
    from eating an estate run, and a wall-clock budget alone would allow an unbounded fast loop.

    ``budget_sec`` is a RETRY-ADMISSION DEADLINE, not a hard wall-clock cap: charged from before the
    first attempt, it gates whether ANOTHER retry is admitted, so an already-started attempt (or a
    nested re-auth) may finish past it. A value at or below one ``REST_TIMEOUT_SEC`` admits zero
    retries after a full-timeout failure (see the constant note above)."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    budget_sec: float = DEFAULT_RETRY_BUDGET_SEC


class TableauSession:
    """Minimal stdlib Tableau REST client that survives mid-loop session loss and transient faults."""

    def __init__(self, creds: SiteCredentials, retry: RetryPolicy | None = None) -> None:
        self._creds = creds
        self.retry = retry or RetryPolicy()
        self.token: str | None = None
        self.site_id: str | None = None
        self.reauth_count = 0
        self.retry_count = 0

    @property
    def version(self) -> str:
        """REST API version in use, for logging and the manifest."""
        return self._creds.version

    def _redact_response(self, text: str) -> str:
        """Scrub every Tableau credential known at this point before text leaves the HTTP layer."""
        return redact(text, self._creds.pat_secret, self._creds.pat_name, self.token or "")

    # A single HTTP round trip whose parameters map 1:1 to distinct HTTP concerns -- verb, path,
    # entity body, Accept header, auth header, API-version segment. Grouping any two of them would be
    # arbitrary, and every internal call site sets a different subset, so a shaping object would add
    # noise without removing a decision. Waived deliberately rather than restructured.
    def _request(  # pylint: disable=too-many-arguments
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        accept: str | None = None,
        authed: bool = True,
        api: str | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        """One HTTP round trip. Never raises for a network failure -- returns status 0 when no HTTP
        response arrived at all (reset/DNS/refused/timeout), so the retry loop can treat a reset
        connection and a gateway 503 the same way. When a response DID arrive but reading its body
        failed mid-stream, the real HTTP status is kept (a 503 is still usefully a 503) and the body
        read error is reported in the payload -- either way, this method does not raise.

        ``api`` overrides the client api-version for this one call. Only the capability probe uses it,
        to re-measure a version-gated feature at its documented floor rather than inferring support."""
        req = urllib.request.Request(
            f"{self._creds.base.rstrip('/')}/api/{api or self._creds.version}{path}",
            data=json.dumps(body).encode() if body else None,
            method=method,
        )
        if accept:
            req.add_header("Accept", accept)
        if body:
            req.add_header("Content-Type", "application/json")
        if authed and self.token:
            req.add_header("X-Tableau-Auth", self.token)
        try:
            with urllib.request.urlopen(req, timeout=REST_TIMEOUT_SEC) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            # Reading the error body is itself a socket read: on a 5xx it can time out (TimeoutError
            # is a subclass of OSError) or arrive truncated (http.client.IncompleteRead). That
            # failure is raised INSIDE this handler, and Python does NOT route an exception raised in
            # one except clause to a sibling except of the same try -- so without this guard it would
            # escape _request uncaught, breaking the documented "never raises for a network failure"
            # contract and denying the retry loop its turn. Keep the authoritative HTTP status (a 503
            # whose body read timed out is still usefully a 503, and stays retry-eligible via
            # TRANSIENT_STATUSES) and substitute a describing body instead of raising.
            try:
                body = exc.read()
            except (OSError, http.client.HTTPException) as read_exc:
                body = f"{type(read_exc).__name__}: {read_exc}".encode()
            return exc.code, body, dict(exc.headers or {})
        except (OSError, http.client.HTTPException) as exc:
            # URLError (a subclass of OSError) covers DNS/refused/timeout; HTTPException covers
            # RemoteDisconnected and IncompleteRead, which urlopen does not always wrap.
            return NETWORK_ERROR_STATUS, f"{type(exc).__name__}: {exc}".encode(), {}

    def sign_in(self) -> None:
        """Exchange the PAT for a session token, retrying transient failures.

        Sign-in is retried too: a gateway blip here would otherwise abort an entire estate capture
        before it started, and re-authentication is also the recovery path for mid-loop session loss.
        """
        last = ""
        for attempt in range(1, self.retry.max_attempts + 1):
            status, payload, _ = self._request(
                "POST",
                "/auth/signin",
                accept="application/json",
                authed=False,
                body={
                    "credentials": {
                        "personalAccessTokenName": self._creds.pat_name,
                        "personalAccessTokenSecret": self._creds.pat_secret,
                        "site": {"contentUrl": self._creds.site},
                    }
                },
            )
            if status == 200:
                creds = json.loads(payload)["credentials"]
                self.token, self.site_id = creds["token"], creds["site"]["id"]
                return
            # Redact the response body before it becomes an exception message: this is a sign-in
            # POST whose request body CONTAINS the PAT, so any reflecting proxy, WAF or debug
            # endpoint echoes it straight back. Measured with a local echo server during review of
            # #97. Redact first, truncate second -- slicing first can leave a secret suffix.
            last = self._redact_response(payload.decode("utf-8", "replace"))[:200]
            if status not in TRANSIENT_STATUSES or attempt == self.retry.max_attempts:
                break
            self.retry_count += 1
            delay = backoff_delay(attempt)
            LOG.warning("sign-in transient failure (HTTP %s); retrying in %.1fs", status, delay)
            time.sleep(delay)
        raise RuntimeError(f"Tableau sign-in failed: HTTP {status}. Check the PAT NAME and SECRET (two values). {last}")

    def sign_out(self) -> None:
        """Release the session. Best-effort; a failed sign-out is not worth aborting a capture."""
        if self.token:
            self._request("POST", "/auth/signout")
            self.token = None

    def raw_get(self, path: str, *, api: str | None = None) -> tuple[int, bytes, str | None]:
        """ONE unretried GET, for capability probing. Returns ``(status, body, content_type)``.

        Separate from :meth:`export` on purpose. ``export`` exists to survive *transient* faults, and
        a capability probe is asking a question whose "no" is **permanent** -- a version gate is not
        fixed by waiting, and retrying it five times with backoff only burns a metered export budget
        to learn the same thing. It also must not re-authenticate: a probe that silently healed a dead
        session would report a capability the real capture then cannot use.

        ``api`` overrides the client api-version for one call, so a version-gated tier can be
        re-probed at its documented floor instead of having its support *inferred* from a version
        string. The body is returned RAW; :meth:`redact_text` is the caller's obligation before any of
        it is printed or serialised (classification must see the raw text -- see
        ``classify_export_error``).
        """
        status, payload, headers = self._request("GET", path, api=api)
        return status, payload, headers.get("Content-Type")

    def redact_text(self, text: str) -> str:
        """Public scrubber for anything derived from a response body that will be persisted."""
        return self._redact_response(text)

    def get_json(self, path: str) -> dict[str, Any]:
        """GET a metadata endpoint as JSON, retrying transient failures."""
        for attempt in range(1, self.retry.max_attempts + 1):
            status, payload, _ = self._request("GET", path, accept="application/json")
            if status == 200:
                return json.loads(payload)
            if status not in TRANSIENT_STATUSES or attempt == self.retry.max_attempts:
                raise RuntimeError(
                    f"GET {path} -> HTTP {status}: {self._redact_response(payload.decode('utf-8', 'replace'))[:200]}"
                )
            self.retry_count += 1
            time.sleep(backoff_delay(attempt))
        raise RuntimeError(f"GET {path} exhausted {self.retry.max_attempts} attempts")

    # One recovery ladder, and it now holds the raw body and its redacted copy side by side so
    # classification and reporting cannot be confused for each other.
    def export(self, path: str) -> tuple[bytes, float, dict[str, Any]]:  # pylint: disable=too-many-locals
        """GET a content-export endpoint, recovering from session loss and transient failures.

        Returns ``(body, elapsed_sec, stats)`` where ``stats`` records how much recovery was needed --
        ``reauths``, ``retries`` and the reasons. Recovery is deliberately **recorded, not silent**:
        a capture that quietly healed itself looks identical to one that never had a problem, which is
        exactly how a partially-truncated result comes to be trusted.

        Raises :class:`ExportFailed` for anything not worth retrying, so a genuinely broken view is
        never recorded as an empty success.
        """
        reauths = 0
        retries: list[str] = []
        deadline = time.monotonic() + self.retry.budget_sec
        for attempt in range(1, self.retry.max_attempts + 1):
            started = time.perf_counter()
            status, payload, headers = self._request("GET", path)
            elapsed = time.perf_counter() - started
            if status == 200:
                return payload, elapsed, {"reauths": reauths, "retries": len(retries), "retry_reasons": retries}
            raw = payload.decode("utf-8", "replace")
            text = self._redact_response(raw)
            # CLASSIFY the raw body, REPORT the redacted one. `_redact_response` is handed the PAT
            # NAME, which is human-chosen: a short one rewrites Tableau's own error codes, and a
            # `401002` mangled into `4[REDACTED]1[REDACTED][REDACTED]2` is read as a permanent
            # source-credential failure instead of the recoverable session loss it is, so the view
            # is abandoned and never re-authenticated. Redaction must never mutate syntax that
            # control flow depends on. `detail` still comes from the redacted copy because
            # `classify_export_error` truncates, and slicing an unredacted body can leave a secret's
            # tail in the retained window.
            kind = classify_export_error(status, raw)[0]
            detail = classify_export_error(status, text)[1]
            if kind == "session_lost" and reauths < MAX_REAUTH_PER_VIEW:
                # Re-auth is a SEPARATE recovery path from transient retry, and is deliberately NOT
                # gated by the admission deadline. It is bounded instead by MAX_REAUTH_PER_VIEW (and
                # sign_in's own max_attempts), because abandoning a view mid-re-auth after a
                # recoverable session loss throws away estate-capture progress for no gain. Since the
                # budget is a retry-admission deadline and not a hard wall-clock cap (see RetryPolicy),
                # a sign_in that blocks can push this view past the deadline -- consistent, not a
                # violation. The very next iteration's transient check charges any elapsed re-auth
                # time against the deadline, so a slow re-auth still curtails FURTHER transient retries.
                reauths += 1
                self.reauth_count += 1
                retries.append("session_lost")
                LOG.debug("session lost (401002); re-authenticating (%d)", self.reauth_count)
                self.sign_in()
                continue

            if kind == "transient" and attempt < self.retry.max_attempts and time.monotonic() < deadline:
                delay = backoff_delay(attempt, headers.get("Retry-After"))
                if time.monotonic() + delay > deadline:
                    raise ExportFailed(f"GET {path} -> retry budget exhausted", "transient", detail)
                self.retry_count += 1
                retries.append(detail[:80])
                LOG.warning(
                    "  transient (%s); retry %d/%d in %.1fs", detail[:60], attempt, self.retry.max_attempts, delay
                )
                time.sleep(delay)
                continue

            raise ExportFailed(f"GET {path} -> HTTP {status}", kind, detail or text[:200])
        raise ExportFailed(f"GET {path} -> exhausted {self.retry.max_attempts} attempts", "transient", "")


def list_views(session: TableauSession) -> list[dict[str, Any]]:
    """Every view on the site, with the identity fields the oracle needs to bind results back."""
    payload = session.get_json(f"/sites/{session.site_id}/views?pageSize=1000")
    return payload.get("views", {}).get("view", [])


def detect_format(values: list[str]) -> str | None:
    """Advisory hint: does this column arrive display-formatted rather than as a raw number?"""
    sample = [v for v in values if v][:50]
    if not sample:
        return None
    if all(_PERCENT.match(v) for v in sample):
        return "percent"
    if all(_CURRENCY.match(v) for v in sample):
        return "currency"
    if all(_THOUSANDS.match(v) for v in sample):
        return "thousands_separated"
    return None


def summarise_csv(payload: bytes) -> dict[str, Any]:
    """Row/column shape plus per-column format hints, so a capture can be proven non-empty."""
    text = payload.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {"row_count": 0, "columns": [], "format_hints": {}}
    header, body = rows[0], rows[1:]
    hints = {}
    for idx, name in enumerate(header):
        fmt = detect_format([r[idx] for r in body if idx < len(r)])
        if fmt:
            hints[name] = fmt
    return {"row_count": len(body), "columns": header, "format_hints": hints}


def safe_slug(text: str) -> str:
    """Filesystem-safe stem. Lossy by design, which is why the LUID is appended by the caller."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:60] or "view"


def png_dimensions(payload: bytes) -> dict[str, int] | None:
    """Width/height from the IHDR chunk. Recorded so the manifest states the reference's RESOLUTION.

    ``resolution=high`` is not an open-ended quality dial: measured over all 52 capturable dashboards
    on the trial site it returns **exactly 2x the dashboard's declared size**, with no exception and
    no parameter that raises it. A 650x800 dashboard therefore tops out at 1300x1600 forever. Writing
    the number down is what lets a consumer judge whether the reference can carry a content-level
    verdict, instead of inferring it from the fact that a PNG exists (issue #403).
    """
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        return None
    return {"width": int.from_bytes(payload[16:20], "big"), "height": int.from_bytes(payload[20:24], "big")}


def svg_facts(payload: bytes) -> dict[str, Any]:
    """Geometry and machine-readable-text census for a REST SVG export.

    Two properties make this the higher-fidelity reference, and both are asserted here rather than
    assumed. (1) The root carries the dashboard's size in millimetres at 96 dpi, so the vector can be
    rasterised at any scale with no geometry guess. Measured over all 52 capturable dashboards on the
    trial site, ``round(mm * 96 / 25.4)`` is the dashboard's declared pixel size **plus exactly 1 px
    in each axis** (a 1400x800 dashboard reports 370.681x211.931mm -> 1401x801) -- 52/52, no
    exception. ``round`` and not ``int``: the true value lands just under the integer (1400.99), so
    truncation is off by one for some dashboards and not others (measured: three different offsets
    across the same 52), which is exactly the kind of silent inconsistency that makes a geometry field
    untrustworthy.

    ``width_px``/``height_px`` are the SVG's **own viewport**, not a restated ``/image`` size. For a
    DASHBOARD that is ``/image?resolution=high`` / 2 + 1 per axis. Do not carry the +1 over to a
    worksheet: one measured worksheet (``BAN Hired``, PNG 1584x1584) reported 792x792, i.e. offset 0,
    and a single sample is not a law. Compare with the offset in mind rather than assuming equality.

    (2) Labels arrive as real ``<text>`` elements holding the literal strings ("7,984", "Active
    Employees"), so a consumer can read the dashboard's CONTENT without rendering anything at all.
    ``text_elements`` is also the cost signal: a crosstab-shaped worksheet measured 37,439 of them in
    a 21 MB SVG against a 4.5 MB PNG, so ``--svg`` is not free on text-dense views.

    ``external_refs`` is the self-containment check: Tableau inlines raster sub-elements (maps, logos)
    as ``data:`` URIs, so a non-zero count means the file needs the server to render and must not be
    treated as durable offline evidence.
    """
    text = payload.decode("utf-8", "replace")
    facts: dict[str, Any] = {
        "text_elements": text.count("<text"),
        "image_elements": text.count("<image"),
        "path_elements": text.count("<path"),
        "external_refs": len([h for h in _SVG_HREF.findall(text) if not h.startswith(("data:", "#"))]),
    }
    match = _SVG_ROOT_MM.search(text[:2000])
    if match:
        facts["width_px"] = round(float(match.group(1)) * 96 / 25.4)
        facts["height_px"] = round(float(match.group(2)) * 96 / 25.4)
    return facts


def capture_view(
    session: TableauSession,
    view: dict[str, Any],
    out_dir: Path,
    wants: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Capture one view's data plus every requested render, keyed by view LUID.

    ``wants`` is a set drawn from ``_RENDER_ROUTES`` ("png", "svg", "pdf") rather than a boolean per
    format: three parallel booleans made the call site unreadable at the third one, and every new
    route would have added another positional flag that some caller forgets to pass.
    """
    view_luid = view["id"]
    workbook = view.get("workbook", {}) or {}
    stem = f"{safe_slug(view.get('name', ''))}__{view_luid[:8]}"
    record: dict[str, Any] = {
        "view_luid": view_luid,
        "view_name": view.get("name"),
        "view_url_name": view.get("viewUrlName"),
        "content_url": view.get("contentUrl"),
        "workbook_luid": workbook.get("id"),
        "project": (view.get("project") or {}).get("name"),
        "updated_at": view.get("updatedAt"),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    data_path = out_dir / "data" / f"{stem}.csv"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload, elapsed, stats = session.export(f"/sites/{session.site_id}/views/{view_luid}/data")
    except ExportFailed as exc:
        record["data"] = {"status": exc.kind, "error": str(exc), "detail": exc.detail}
        return record
    data_path.write_bytes(payload)
    record["data"] = {
        "status": "ok",
        "path": str(data_path.relative_to(out_dir)).replace("\\", "/"),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "elapsed_sec": round(elapsed, 2),
        **stats,
        **summarise_csv(payload),
    }

    for kind in ("png", "svg", "pdf"):
        if kind in wants:
            leg = "image" if kind == "png" else kind
            record[leg] = _capture_render(
                session, view_luid, out_dir / "images" / f"{stem}.{_RENDER_EXTENSIONS[kind]}", kind
            )
    return record


# Route per tier. `pdf` uses `type=Unspecified`, which sizes the page to the viz instead of a paper
# size -- MEASURED to work and to give `0.75 * declared_px + 72pt` on 52/52 dashboards, but NOT in
# Tableau's documented value list (`A3, A4, A5, B5, Executive, Folio, Ledger, Legal, Letter, Note,
# Quarto, Tabloid`), so it is an observed behaviour, not a contract. `pdf_facts` records the page it
# actually got, so a server that silently ignored the value is visible rather than assumed.
_RENDER_ROUTES = {
    "png": ("image", "?resolution=high"),
    "svg": ("image", "?format=svg"),
    "pdf": ("pdf", "?type=Unspecified"),
}
_RENDER_EXTENSIONS = {"png": "png", "svg": "svg", "pdf": "pdf"}


def pdf_facts(payload: bytes) -> dict[str, Any]:
    """Page geometry and vector-ness of a REST PDF export, stdlib only.

    ``/pdf`` reaches back to **API 2.8 / Tableau Server 10.5**, which is why it is the portable rung of
    the ladder -- but "it returned a PDF" is not the same as "it returned the page I asked for", and
    ``type=Unspecified`` is undocumented. Recording the ``MediaBox`` is what distinguishes the two: a
    server that ignored the value falls back to a paper size (measured default **612x792 = Letter
    portrait**, notwithstanding the docs' claim that the default is ``Legal`` = 612x1008).

    ``fontfile_count`` is the fidelity note: unlike the SVG, a Tableau PDF **embeds** its fonts, so it
    renders with the workbook's real typefaces on a machine that does not have them installed.
    """
    facts: dict[str, Any] = {
        "vector": True,
        "fontfile_count": len(re.findall(rb"/FontFile\d?", payload)),
        "image_xobjects": len(re.findall(rb"/Subtype\s*/Image", payload)),
    }
    boxes = sorted({m.decode().strip() for m in _PDF_MEDIABOX.findall(payload)})
    if boxes:
        parts = boxes[0].split()
        if len(parts) >= 4:
            facts["page_pt"] = {"width": round(float(parts[2])), "height": round(float(parts[3]))}
    return facts


def _capture_render(session: TableauSession, view_luid: str, path: Path, kind: str) -> dict[str, Any]:
    """Fetch one rendered form of a view and describe what was actually obtained.

    All three rungs come from the same VizQL render, so none survives a workbook whose data sources
    are not connected -- measured, ``image``, ``image?format=svg``, ``pdf`` and ``data`` all return the
    same HTTP 400 ``ExportViewException: Error: data sources not connected``. What differs is the
    CEILING and the REACH: PNG is capped at 2x a dashboard's declared size but works back to API 2.5;
    SVG is resolution-independent but needs 3.29; PDF is vector with embedded fonts from API 2.8.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    endpoint, query = _RENDER_ROUTES[kind]
    try:
        payload, elapsed, stats = session.export(f"/sites/{session.site_id}/views/{view_luid}/{endpoint}{query}")
    except ExportFailed as exc:
        record = {"status": exc.kind, "error": str(exc), "detail": exc.detail}
        if kind == "svg" and SVG_VERSION_MARKER in exc.detail:
            # A version gate is a CONFIGURATION fault, not a broken view: retrying cannot fix it and
            # neither can a Tableau-side credential, so say which knob to turn rather than filing it
            # under the generic failure bucket a reader will chase into the data source.
            record["status"] = "unsupported_api_version"
            record["remedy"] = f"set TABLEAU_REST_API_VERSION={SVG_MIN_API_VERSION} or later in .env"
        return record
    # HTTP 200 is not proof the requested format came back. An older server that does not recognise
    # `format=svg` can ignore the unknown parameter and return its default PNG -- and writing those
    # bytes to a `.svg` labelled `vector: true` manufactures exactly the false evidence this capture
    # exists to prevent. Refuse to persist a mislabelled file at all.
    matches, why = capability.format_matches(kind, payload, None)
    if not matches:
        return {
            "status": "format_mismatch",
            "requested_format": kind,
            "detail": why,
            "bytes": len(payload),
            "elapsed_sec": round(elapsed, 2),
            **stats,
        }
    path.write_bytes(payload)
    record = {
        "status": "ok",
        "format": kind,
        "path": str(path.relative_to(path.parent.parent)).replace("\\", "/"),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "elapsed_sec": round(elapsed, 2),
        **stats,
    }
    if kind == "svg":
        record.update(svg_facts(payload))
        record["vector"] = True
    elif kind == "pdf":
        record.update(pdf_facts(payload))
    else:
        record["vector"] = False
        dimensions = png_dimensions(payload)
        if dimensions:
            record["dimensions_px"] = dimensions
    return record


def build_parser() -> argparse.ArgumentParser:
    """CLI surface."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path, help="output directory (should be git-ignored)")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials file")
    parser.add_argument(
        "--workbook",
        action="append",
        default=None,
        help=(
            "published Tableau workbook caption filter; exact, case-insensitive match, not the migration slug "
            "(repeatable)"
        ),
    )
    parser.add_argument("--images", action="store_true", help="also capture /image?resolution=high per view")
    parser.add_argument(
        "--svg",
        action="store_true",
        help=(
            f"also capture /image?format=svg per view -- resolution-independent, and its <text> "
            f"elements carry the dashboard's literal labels. Requires REST API >= {SVG_MIN_API_VERSION}"
        ),
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="also capture /pdf?type=Unspecified per view -- vector with EMBEDDED fonts, and available "
        "back to REST API 2.8 (Tableau Server 10.5), so it is the portable choice for on-prem sites",
    )
    parser.add_argument(
        "--reference-best",
        action="store_true",
        help="PROBE the site and capture the best render tier it actually supports (svg > pdf > "
        "png_high), instead of assuming one from a version string. Records the tier and why in the "
        "manifest. Combines with the explicit flags above, which are always honoured as well",
    )
    parser.add_argument("--limit", type=int, default=0, help="stop after N views (0 = all)")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"attempts per export before giving up (default {DEFAULT_MAX_ATTEMPTS})",
    )
    parser.add_argument(
        "--retry-budget",
        type=float,
        default=DEFAULT_RETRY_BUDGET_SEC,
        help=(
            f"seconds to admit retries for ONE export -- a deadline for admitting the NEXT retry, "
            f"charged from before attempt 1, NOT a hard wall-clock cap (default "
            f"{DEFAULT_RETRY_BUDGET_SEC:.0f}). At or below one {REST_TIMEOUT_SEC}s request timeout, a "
            f"failure that blocks for the full timeout cannot be retried; faster transient failures "
            f"still retry until it is spent"
        ),
    )
    return parser


def select_views(
    session: TableauSession, workbooks: list[str] | None, limit: int
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Resolve the views to capture, plus a workbook-LUID -> name index for the manifest."""
    views = list_views(session)
    payload = session.get_json(f"/sites/{session.site_id}/workbooks?pageSize=1000")
    names = {wb["id"]: wb["name"] for wb in payload.get("workbooks", {}).get("workbook", [])}
    if workbooks:
        wanted = {w.lower() for w in workbooks}
        views = [v for v in views if names.get((v.get("workbook") or {}).get("id"), "").lower() in wanted]
    if limit:
        views = views[:limit]
    return views, names


def log_progress(index: int, total: int, record: dict[str, Any]) -> None:
    """One line per view: proof of rows captured, or a loud, classified failure."""
    data = record.get("data", {})
    name = (record.get("view_name") or "")[:34]
    status = data.get("status")
    if status == "ok":
        marks = []
        if data.get("reauths"):
            marks.append(f"re-auth x{data['reauths']}")
        if data.get("retries"):
            marks.append(f"retry x{data['retries']}")
        suffix = f"  ({', '.join(marks)})" if marks else ""
        LOG.info(
            "  %2d/%d  %-34s %5d rows  %6.1fs%s", index, total, name, data["row_count"], data["elapsed_sec"], suffix
        )
    elif status == "source_credential":
        LOG.warning("  %2d/%d  %-34s NEEDS CREDENTIAL: %s", index, total, name, data.get("detail"))
    else:
        LOG.warning("  %2d/%d  %-34s FAILED (%s): %s", index, total, name, status, data.get("detail"))


def _render_statuses(record: dict[str, Any], requested: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """Status of every RENDER leg for this view, judged against what was actually ASKED FOR.

    An absent key normally means the leg was not requested, which must read as ``ok`` -- otherwise a
    plain data-only capture (no ``--images``, ``--svg`` or ``--pdf``) would count itself as failed.
    But when a leg WAS requested and is nevertheless absent, "absent" is a real failure: without
    ``requested`` the capture silently degrades to data-only and still reports success, which is
    exactly the exit-0-with-no-reference hole. Returning a tuple keeps the aggregate sets below
    reading the same legs, so adding a fourth output format cannot be counted by one and missed by
    the others.
    """
    statuses = []
    for kind, leg in (("png", "image"), ("svg", "svg"), ("pdf", "pdf")):
        if leg in record:
            statuses.append(record[leg].get("status"))
        elif kind in requested:
            statuses.append("not_captured")
        else:
            statuses.append("ok")
    return tuple(statuses)


def _partition(
    records: list[dict[str, Any]], requested: frozenset[str] = frozenset()
) -> dict[str, list[dict[str, Any]]]:
    """Split records into the four sets the manifest and the exit code both read.

    One function so the sets cannot drift apart: they must all consult the same render legs, and the
    bug this replaces was three list comprehensions where only two had been taught about a new leg.
    """
    ok = [r for r in records if r.get("data", {}).get("status") == "ok"]
    return {
        "ok": ok,
        "empty": [r for r in ok if r["data"]["row_count"] == 0],
        "complete": [
            r
            for r in records
            if r.get("data", {}).get("status") == "ok" and all(s == "ok" for s in _render_statuses(r, requested))
        ],
        "blocked": [
            r
            for r in records
            if "source_credential" in {r.get("data", {}).get("status"), *_render_statuses(r, requested)}
        ],
        "failed": [
            r
            for r in records
            if any(
                status not in {"ok", "source_credential"}
                for status in (r.get("data", {}).get("status"), *_render_statuses(r, requested))
            )
        ],
    }


@dataclass(frozen=True)
class CaptureRun:
    """Where and when one capture happened -- the provenance half of the manifest.

    Bundled because ``write_manifest`` needs all four together and nothing else needs any of them
    individually; passing them as loose positional parameters is what pushed the signature past the
    readable limit as soon as capability reporting was added.

    ``requested_renders`` is what the caller ASKED for, which is not the same as what came back --
    that gap is the point. ``reference_required`` records that ``--reference-best`` was used, so a run
    whose capability probe returned UNDETERMINED (and therefore requested nothing) is still judged
    against the operator's intent rather than against its own empty plan.
    """

    session: TableauSession
    env: dict[str, str]
    out_dir: Path
    started: float
    requested_renders: frozenset[str] = frozenset()
    reference_required: bool = False


def write_manifest(
    records: list[dict[str, Any]],
    run: CaptureRun,
    capability_report: dict[str, Any] | None = None,
) -> int:
    """Write the manifest and return the process exit code.

    Codes: 0 all selected views captured, 1 partial non-credential failure, 2 credential-blocked,
    3 total non-credential failure, 4 no views selected, **5 a reference render was required but none
    was obtained**.

    Code 5 exists because the alternative is silence. With ``--reference-best`` and an UNDETERMINED
    probe, no render kind is requested at all, every view's data still succeeds, and the run would
    otherwise exit **0 having captured zero reference images** -- a caller gating on the exit code
    would read that as a complete capture.
    """
    sets = _partition(records, run.requested_renders)
    blocked, failed, complete = sets["blocked"], sets["failed"], sets["complete"]
    rendered = sum(1 for r in records if any(r.get(leg, {}).get("status") == "ok" for leg in ("image", "svg", "pdf")))
    reference_missing = run.reference_required and rendered == 0
    manifest = {
        "schema": "tableau-oracle/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": run.env["TABLEAU_SERVER_URL"],
        "site": run.env["TABLEAU_SITE"],
        "rest_api_version": run.env.get("TABLEAU_REST_API_VERSION"),
        "view_count": len(records),
        "captured_complete": len(complete),
        "data_ok": len(sets["ok"]),
        "data_empty": len(sets["empty"]),
        "image_ok": sum(1 for r in records if r.get("image", {}).get("status") == "ok"),
        "svg_ok": sum(1 for r in records if r.get("svg", {}).get("status") == "ok"),
        "pdf_ok": sum(1 for r in records if r.get("pdf", {}).get("status") == "ok"),
        "requested_renders": sorted(run.requested_renders),
        "reference_required": run.reference_required,
        "reference_missing": reference_missing,
        # #403's surviving half: the manifest must STATE the grade of evidence it holds, so a
        # downstream validator reads it instead of inferring it from the fact that a file exists.
        "render_capability": capability_report,
        "credential_blocked": len(blocked),
        "failed": len(failed),
        "total_reauths": run.session.reauth_count,
        "total_retries": run.session.retry_count,
        "elapsed_sec": round(time.perf_counter() - run.started, 1),
        "views": records,
    }
    manifest_path = run.out_dir / "oracle-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    LOG.info(
        "\n%d/%d captured (%d empty), %d credential-blocked, %d failed, %d re-auth(s), %d retr(ies), %.0fs -> %s",
        len(complete),
        len(records),
        len(sets["empty"]),
        len(blocked),
        len(failed),
        run.session.reauth_count,
        run.session.retry_count,
        manifest["elapsed_sec"],
        manifest_path,
    )
    if blocked:
        LOG.warning(
            "\n%d view(s) need a credential ON THE TABLEAU SIDE - no retry can fix this, a human must "
            "reauthorize the source in Tableau:",
            len(blocked),
        )
        for record in blocked:
            blocked_detail = (
                record.get("data", {}).get("detail")
                or record.get("image", {}).get("detail")
                or record.get("svg", {}).get("detail")
                or record.get("pdf", {}).get("detail")
            )
            LOG.warning("  - %s (%s): %s", record["view_name"], record["workbook_name"], blocked_detail)
    stale_api = [r for r in records if r.get("svg", {}).get("status") == "unsupported_api_version"]
    if stale_api:
        # Loud and separate from `blocked`: this one is fixed by an .env line, not by a human
        # reauthorizing a data source in Tableau, and conflating the two sends the reader hunting
        # in the wrong system.
        LOG.warning(
            "\n%d view(s) could not produce SVG: this site's REST API version is below %s. "
            "Set TABLEAU_REST_API_VERSION=%s in .env and re-run; the PNG and PDF captures are "
            "unaffected (they reach back to API 2.5 and 2.8 respectively).",
            len(stale_api),
            SVG_MIN_API_VERSION,
            SVG_MIN_API_VERSION,
        )
    for warning in (capability_report or {}).get("warnings", []):
        LOG.warning("! %s", warning)
    if reference_missing:
        LOG.error(
            "\nA reference render was REQUIRED (--reference-best) but NONE was captured across %d "
            "view(s). The capability probe did not settle on a tier, so nothing was requested. This "
            "run has data only and must not be treated as a complete capture; re-run once the probe "
            "can reach a renderable view, or name a tier explicitly with --images/--svg/--pdf.",
            len(records),
        )
    if not records:
        return 4
    if reference_missing:
        return 5
    if failed:
        return 1 if complete else 3
    return 2 if blocked else 0


def build_retry_policy(max_attempts: int, budget_sec: float) -> RetryPolicy:
    """Build the retry policy, warning when the budget is too small to retry a full-timeout failure.

    ``budget_sec`` is a retry-admission deadline, charged from before the first attempt, which can
    itself block for the full ``REST_TIMEOUT_SEC`` on a socket timeout. Below
    ``RETRY_ADMISSION_FLOOR_SEC`` (one timeout plus the first backoff) the deadline is already spent
    when such a failure returns, so it is retried zero times -- the issue #197 footgun.

    This warns rather than clamps OR rejects, on purpose. A sub-floor budget is NOT incoherent: it is
    a deliberate, useful choice for FAST-failing transients (a tight budget cutting a long Retry-After
    loop short is exactly what ``test_retry_budget_stops_a_slow_failure_before_max_attempts`` relies
    on), so clamping would silently defeat it and rejecting would forbid it. The warning is therefore
    scoped to the one thing that is actually broken -- a failure that blocks for the *full* per-request
    timeout -- and says so, rather than the old, false blanket claim that nothing below 2x is retried.
    """
    if budget_sec < RETRY_ADMISSION_FLOOR_SEC:
        LOG.warning(
            "--retry-budget %.0fs is below the %.0fs needed to retry a failure that blocks for the "
            "full %ds request timeout (one timeout plus the first backoff), so such a failure will "
            "NOT be retried; faster transient failures still retry until the budget is spent",
            budget_sec,
            RETRY_ADMISSION_FLOOR_SEC,
            REST_TIMEOUT_SEC,
        )
    return RetryPolicy(max_attempts=max_attempts, budget_sec=budget_sec)


def probe_render_capability(
    session: TableauSession, env: dict[str, str], views: list[dict[str, Any]]
) -> dict[str, Any]:
    """Ask the SITE what it can render, by probing, and reconcile that with both version strings.

    The probe view matters. A workbook whose data sources are not connected fails every route
    identically, so probing one would report "no tier available" for a site that is perfectly capable
    -- so try successive views until one gives a determinate answer, capped at
    ``MAX_CAPABILITY_PROBE_VIEWS`` because each attempt costs metered export calls.
    """
    info = capability.server_info(env["TABLEAU_SERVER_URL"])
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
    for view in views[:MAX_CAPABILITY_PROBE_VIEWS]:
        report = capability.detect(
            fetcher(view["id"]),
            view["id"],
            capability.ApiVersions(configured=configured, advertised=advertised),
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
    best["probe_views_tried"] = min(len(views), MAX_CAPABILITY_PROBE_VIEWS)
    LOG.info(
        "render capability: best tier = %s%s",
        best.get("selected_tier") or "UNDETERMINED",
        " (PROVISIONAL)" if best.get("provisional") else "",
    )
    return best


def _capability_rank(report: dict[str, Any]) -> tuple[int, int]:
    """Order two probe reports: a settled answer beats a provisional one beats no answer at all."""
    return (1 if report.get("selected_tier") else 0, 1 if report.get("capability_complete") else 0)


def main() -> int:
    """Capture the oracle for every selected view.

    Exit codes: ``0`` all selected views captured, ``1`` partial non-credential failure,
    ``2`` some selected view needs a credential on the Tableau side (actionable only by a human --
    never by a retry), ``3`` total non-credential failure, ``4`` no views selected, ``5`` a reference
    render was required (``--reference-best``) but none was obtained.
    """
    args = build_parser().parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = resolve_env(args.env)
    require(env)
    session = TableauSession(
        SiteCredentials(
            base=env["TABLEAU_SERVER_URL"],
            site=env["TABLEAU_SITE"],
            pat_name=env["TABLEAU_PAT_NAME"],
            pat_secret=pat_secret(env),
            version=env.get("TABLEAU_REST_API_VERSION", "3.21"),
        ),
        build_retry_policy(args.max_attempts, args.retry_budget),
    )
    session.sign_in()
    LOG.info("signed in to site %r (api %s)", env["TABLEAU_SITE"], session.version)

    views, workbook_names = select_views(session, args.workbook, args.limit)
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG.info("capturing %d view(s) -> %s", len(views), out_dir)

    capability_report = None
    wants = {kind for kind, on in (("png", args.images), ("svg", args.svg), ("pdf", args.pdf)) if on}
    if args.reference_best and views:
        capability_report = probe_render_capability(session, env, views)
        tier = capability_report.get("selected_tier")
        if tier:
            # The ladder names the PNG rung `png_high` (it is `?resolution=high`, not the plain
            # render); the capture kinds are keyed by file format. One mapping, stated once.
            wants.add({"png_high": "png"}.get(tier, tier))

    records, started = [], time.perf_counter()
    for index, view in enumerate(views, 1):
        record = capture_view(session, view, out_dir, frozenset(wants))
        record["workbook_name"] = workbook_names.get(record["workbook_luid"])
        records.append(record)
        log_progress(index, len(views), record)

    exit_code = write_manifest(
        records,
        CaptureRun(session, env, out_dir, started, frozenset(wants), bool(args.reference_best)),
        capability_report,
    )
    session.sign_out()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
