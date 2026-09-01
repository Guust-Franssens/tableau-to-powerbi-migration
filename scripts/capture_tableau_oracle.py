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
import hashlib
import json
import logging
import random
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tableau_render_capability as capability  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_payload_facts import (  # noqa: E402  # pylint: disable=wrong-import-position
    pdf_facts,
    png_dimensions,
    summarise_csv,
    svg_facts,
)
from tableau_env import (  # noqa: E402  # pylint: disable=wrong-import-position
    pat_secret,
    redact,
    redacted_note,
    require,
    resolve_env,
    scrub_tree,
    secret_forms,
)

# ⚠️ Imported as plain NAMES, not reached through the module. `tableau_http._request(...)` is
# `protected-access` to pylint (W0212, measured), and any alias would rename the call away from
# `TAINTING_CALLS` in `tests/test_diagnostic_redaction.py`, silently un-tainting every call site.
# The module-level `_request` and this class's `_request` method are deliberately the same name: the
# method is now a thin adapter that builds the Request and delegates to the one hardened round trip.
from tableau_http import (  # noqa: E402  # pylint: disable=wrong-import-position
    NETWORK_ERROR_STATUS,
    _request,
    header_value,
)

LOG = logging.getLogger("tableau-oracle")

REST_TIMEOUT_SEC = 180
SESSION_LOST_CODE = "401002"
# A **successful** response that echoes our own credential is refused outright rather than persisted.
# It is not a diagnostic status: nothing is written, because the alternative is a live credential in a
# `.csv` or `.svg` on disk, which no downstream redaction can reach.
CREDENTIAL_REFLECTED = "credential_reflected"
MAX_REAUTH_PER_VIEW = 2
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
# Defined once, in `tableau_http`, beside the code that returns it; re-exported here because callers
# and tests read it off this module.
TRANSIENT_STATUSES = frozenset({NETWORK_ERROR_STATUS, 429, 500, 502, 503, 504})


# SVG export is gated by REST version. Below 3.29 the server refuses with this phrase and a 400 --
# it does NOT silently fall back to PNG (measured on 3.21 / 3.24 / 3.28), so the sniff is safe.
SVG_MIN_API_VERSION = "3.29"
SVG_VERSION_MARKER = "SVG export requires API version"
# A Tableau LUID is a UUID. Checkable in full, so a value matching it is provably not a credential --
# which is what lets `artifact_stem` be an allowlist rather than one more screen.
_LUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ExportFailed(RuntimeError):
    """A view export did not return data. ``kind`` classifies what a caller should do about it."""

    def __init__(self, message: str, kind: str, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def classify_export_error(status: int, text: str, *, redactor=None) -> tuple[str, str]:
    """Map a Tableau failure to an actionable class. The distinction drives whether we retry.

    Order matters. ``401002`` is our session dying and is fixed by re-authenticating. A transient
    status (gateway 5xx, 429, or a network-level failure) is fixed by waiting. A
    ``FederatedDataSourceException`` naming an expired OAuth token or a connection that "needs
    attention" is Tableau itself being unable to query the underlying source -- **a missing credential
    is not transient**, so retrying burns time and still cannot succeed; only a human can fix it.
    Transient is checked *before* the credential markers so a 503 whose body happens to mention
    authentication is still retried rather than misfiled as a permanent credential block.

    ``redactor`` splits the two jobs this function does. **Classification reads ``text`` raw**, because
    redaction is handed the human-chosen PAT *name* and a short one rewrites Tableau's own error codes
    -- a ``401002`` mangled mid-string reads as a permanent credential failure instead of the
    recoverable session loss it is. **The reported detail is built from ``safe``**, the fully redacted
    copy, so every slice, regex and ``split`` below runs on text a secret has already left.

    That replaces the previous two-call dance (``classify(raw)[0]`` for the kind, ``classify(text)[1]``
    for the detail), which was correct only because the raw call's detail happened to be discarded --
    one keystroke from a leak, and unprovable from this function alone.
    """
    safe = redactor(text) if redactor is not None else text
    if SESSION_LOST_CODE in text:
        return "session_lost", ""
    if status in TRANSIENT_STATUSES:
        label = "network error" if status == NETWORK_ERROR_STATUS else f"HTTP {status}"
        return "transient", f"{label}: {safe[:150]}"
    credential_markers = (
        "FederatedDataSourceException",
        "OAuth refresh token",
        "need attention",
        "needs attention",
        "Invalid username or password",
        "authentication",
    )
    if any(marker.lower() in text.lower() for marker in credential_markers):
        match = re.search(r"([\w.-]+\.(?:com|net|io|azuredatabricks\.net)[^:\s]*):\s*(Tableau[^<\n]{0,180})", safe)
        detail = f"{match.group(1)}: {match.group(2).strip()}" if match else safe[:200]
        return "source_credential", detail.split("tableau_error_source=")[0].strip()
    return "failed", f"HTTP {status}: {safe[:200]}"


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
        to re-measure a version-gated feature at its documented floor rather than inferring support.

        This is now a **thin adapter**: it shapes the outbound request and hands it to
        ``tableau_http._request``, the one hardened round trip in the repository. The exception
        handling that used to live here moved there wholesale -- unchanged, and now shared with
        ``tableau_render_capability``, whose three hand-rolled copies of it each leaked a reflected
        credential in a different review round. The bare ``_request`` below is that module-level
        import, not recursion into this method."""
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
        return _request(req, timeout=REST_TIMEOUT_SEC, redactor=self._redact_response)

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
            # #97. `redacted_note` is what guarantees redaction precedes the 200-character cut --
            # slicing first can leave a secret's tail, or its head, in the retained window.
            last = redacted_note(payload, self._redact_response, limit=200)
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
        return status, payload, header_value(headers, "Content-Type")

    def redact_text(self, text: str) -> str:
        """Public scrubber for anything derived from a response body that will be persisted."""
        return self._redact_response(text)

    def reflected_credential(self, payload: bytes) -> str | None:
        """Which AUTHENTICATING credential a **successful** body echoed back, or ``None``.

        Searched as bytes, so it costs one substring scan and works for a CSV and a PNG alike, and
        covers the same wire spellings :func:`tableau_env.redact` knows about.

        ⚠️ **The PAT *name* is deliberately NOT grounds for refusal**, and that asymmetry is the whole
        design. The secret and the session token are machine-generated and high-entropy, so a match is
        a reflection rather than a coincidence, and their exposure is unrecoverable -- refusing costs
        one view, keeping it costs a credential in a file. The PAT name is human-chosen, visible in
        Tableau's own UI, does not authenticate on its own, and a name like ``Migration`` colliding
        with a real column heading would refuse a legitimate estate. It is handled one layer down
        instead, by the manifest-boundary scrub: a mangled label, not a refused capture.
        """
        for label, secret in (("PAT secret", self._creds.pat_secret), ("session token", self.token or "")):
            if secret and any(form.encode("utf-8") in payload for form in secret_forms(secret)):
                return label
        return None

    def get_json(self, path: str) -> dict[str, Any]:
        """GET a metadata endpoint as JSON, retrying transient failures."""
        for attempt in range(1, self.retry.max_attempts + 1):
            status, payload, _ = self._request("GET", path, accept="application/json")
            if status == 200:
                return json.loads(payload)
            if status not in TRANSIENT_STATUSES or attempt == self.retry.max_attempts:
                raise RuntimeError(
                    f"GET {path} -> HTTP {status}: {redacted_note(payload, self._redact_response, limit=200)}"
                )
            self.retry_count += 1
            time.sleep(backoff_delay(attempt))
        raise RuntimeError(f"GET {path} exhausted {self.retry.max_attempts} attempts")

    # One recovery ladder, and it now holds the raw body and its redacted copy side by side so
    # classification and reporting cannot be confused for each other.
    def export(self, path: str, *, api: str | None = None) -> tuple[bytes, float, dict[str, Any]]:  # pylint: disable=too-many-locals
        """GET a content-export endpoint, recovering from session loss and transient failures.

        ``api`` overrides the client api-version for this export. It exists because the capability
        probe can RECOVER a tier by re-probing at its documented floor: without honouring the version
        that actually answered, the run is told "svg is available" and then fetches at the configured
        version, where the same request is still refused (measured: floor 3.29 ``available``, the same
        request at the configured 3.21 ``unsupported``).

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
            status, payload, headers = self._request("GET", path, api=api)
            elapsed = time.perf_counter() - started
            if status == 200:
                # A SUCCESSFUL body is the one thing this class hands back for PERSISTING -- to
                # `data/<view>.csv`, to `images/<view>.svg`, and to every field derived from it. It is
                # therefore the seam, and the only place a reflected credential can be stopped before
                # it becomes a file. Redacting here instead would be wrong twice over: it would
                # corrupt the customer's own data, and a manifest-boundary scrub (which we also do)
                # structurally cannot reach a `.csv` already written to disk.
                reflected = self.reflected_credential(payload)
                if reflected:
                    raise ExportFailed(
                        f"GET {path} -> HTTP 200, but the response body echoed our {reflected}",
                        CREDENTIAL_REFLECTED,
                        f"the {reflected} was found in a SUCCESSFUL response. Nothing was written: a "
                        f"payload carrying our own credential is not evidence worth keeping, and "
                        f"persisting it would put the credential in a .csv/.svg on disk. Something "
                        f"between this process and Tableau is reflecting request data -- investigate "
                        f"the proxy/WAF in front of the site, and rotate the credential.",
                    )
                return payload, elapsed, {"reauths": reauths, "retries": len(retries), "retry_reasons": retries}
            raw = payload.decode("utf-8", "replace")
            # ONE call, with the redactor inside. `classify_export_error` classifies on the raw text
            # -- redaction is handed the human-chosen PAT NAME, and a short one mangles `401002` into
            # something read as a permanent credential failure rather than the recoverable session
            # loss it is -- while building every reported detail from the redacted copy. The previous
            # shape called it twice and threw away the raw call's detail, which was safe only by
            # convention: one keystroke (`kind, detail = classify(status, raw)`) reinstated the leak.
            kind, detail = classify_export_error(status, raw, redactor=self._redact_response)
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
                delay = backoff_delay(attempt, header_value(headers, "Retry-After"))
                if time.monotonic() + delay > deadline:
                    raise ExportFailed(f"GET {path} -> retry budget exhausted", "transient", detail)
                self.retry_count += 1
                retries.append(detail[:80])
                LOG.warning(
                    "  transient (%s); retry %d/%d in %.1fs", detail[:60], attempt, self.retry.max_attempts, delay
                )
                time.sleep(delay)
                continue

            raise ExportFailed(
                f"GET {path} -> HTTP {status}",
                kind,
                detail or redacted_note(payload, self._redact_response, limit=200),
            )
        raise ExportFailed(f"GET {path} -> exhausted {self.retry.max_attempts} attempts", "transient", "")


def list_views(session: TableauSession) -> list[dict[str, Any]]:
    """Every view on the site, with the identity fields the oracle needs to bind results back."""
    payload = session.get_json(f"/sites/{session.site_id}/views?pageSize=1000")
    return payload.get("views", {}).get("view", [])


def artifact_stem(view_luid: str) -> str:
    """The ONLY way an artifact filename is built, and it takes a LUID and nothing else.

    ⚠️ This is a **closed allowlist**, and it replaces ``safe_slug(view["name"])`` -- deleted rather
    than fixed. The name is response data: a reflected session token arriving as a view NAME was
    slugged and truncated into ``data/<60-char-token-prefix>__<luid>.csv``, and because the truncated
    prefix is no longer the literal the redactor searches for, no downstream scrub could recognise it.
    Redacting before slugging would have fixed that one site; six review rounds say the next site is
    the problem, not this one.

    So nothing is screened here. A filename is composed of a value whose shape we can VERIFY -- a
    Tableau LUID is a UUID, checkable in full, and cannot contain a credential -- plus our own
    constants. A response-derived string cannot reach a path because there is no longer any code that
    puts one there.

    The cost is real and is the right trade: ``_oracle/data/`` now lists LUIDs rather than view names.
    The manifest still maps ``view_name`` -> ``path`` for every view, so the readable index moved one
    file over instead of disappearing.
    """
    if not _LUID_RE.match(view_luid or ""):
        raise ValueError(
            f"refusing to build an artifact path from a non-LUID view identifier ({len(view_luid or '')} chars)"
        )
    return view_luid.lower()


def capture_view(
    session: TableauSession,
    view: dict[str, Any],
    out_dir: Path,
    wants: frozenset[str] = frozenset(),
    api_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Capture one view's data plus every requested render, keyed by view LUID.

    ``wants`` is a set drawn from ``_RENDER_ROUTES`` ("png", "svg", "pdf") rather than a boolean per
    format: three parallel booleans made the call site unreadable at the third one, and every new
    route would have added another positional flag that some caller forgets to pass.

    ``api_overrides`` maps a kind to the api-version that tier was PROVED to answer at. The capability
    probe can recover a tier by re-probing at its documented floor, and a capture that ignores that
    version fetches at the configured one and fails on a tier the manifest already promised.
    """
    view_luid = view["id"]
    workbook = view.get("workbook", {}) or {}
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
    try:
        stem = artifact_stem(view_luid)
    except ValueError as exc:
        # A view whose identifier is not a LUID is not one we will invent a filename for. Every other
        # candidate string on this record came out of a Tableau response.
        record["data"] = {"status": "failed", "error": str(exc), "detail": "unusable view identifier"}
        return record
    if session.reflected_credential(stem.encode("utf-8")):
        # Closes the ONE residual the LUID allowlist leaves: a credential that is itself UUID-shaped
        # AND returned as a view id. Measured against the live site, none of ours is close -- the PAT
        # secret, PAT name and session token are 57/20/92 characters and none is hex-and-dash-only --
        # so this is belt and braces. It is worth the two lines because it turns "no credential we
        # happen to hold can pass the allowlist" into "no credential can", which is the difference
        # between a claim that is true today and one that is true by construction.
        record["data"] = {
            "status": CREDENTIAL_REFLECTED,
            "error": "the view identifier IS one of our own credentials",
            "detail": "refusing to build an artifact path from it; investigate what is reflecting request data",
        }
        return record

    record["data"] = _capture_data(session, view_luid, out_dir / "data" / f"{stem}.csv", out_dir)
    if record["data"]["status"] != "ok":
        return record

    for kind in ("png", "svg", "pdf"):
        if kind in wants:
            leg = "image" if kind == "png" else kind
            record[leg] = _capture_render(
                session,
                view_luid,
                out_dir / "images" / f"{stem}.{_RENDER_EXTENSIONS[kind]}",
                kind,
                api=(api_overrides or {}).get(kind),
            )
    return record


def _capture_data(session: TableauSession, view_luid: str, path: Path, out_dir: Path) -> dict[str, Any]:
    """The numeric oracle for one view: Tableau's own aggregated, display-formatted values."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload, elapsed, stats = session.export(f"/sites/{session.site_id}/views/{view_luid}/data")
    except ExportFailed as exc:
        return {"status": exc.kind, "error": str(exc), "detail": exc.detail}
    path.write_bytes(payload)
    return {
        "status": "ok",
        "path": str(path.relative_to(out_dir)).replace("\\", "/"),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "elapsed_sec": round(elapsed, 2),
        **stats,
        **summarise_csv(payload),
    }


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


def _capture_render(
    session: TableauSession, view_luid: str, path: Path, kind: str, api: str | None = None
) -> dict[str, Any]:
    """Fetch one rendered form of a view and describe what was actually obtained.

    All three rungs come from the same VizQL render, so none survives a workbook whose data sources
    are not connected -- measured, ``image``, ``image?format=svg``, ``pdf`` and ``data`` all return the
    same HTTP 400 ``ExportViewException: Error: data sources not connected``. What differs is the
    CEILING and the REACH: PNG is capped at 2x a dashboard's declared size but works back to API 2.5;
    SVG is resolution-independent but needs 3.29; PDF is vector with embedded fonts from API 2.8.

    ``api`` is the version this tier was PROVED to answer at, when a floor re-probe recovered it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    endpoint, query = _RENDER_ROUTES[kind]
    try:
        payload, elapsed, stats = session.export(
            f"/sites/{session.site_id}/views/{view_luid}/{endpoint}{query}", api=api
        )
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
    #
    # The redactor is NOT optional here: `why` quotes the response's own leading bytes, and this
    # record is serialised into `oracle-manifest.json`. A reflecting proxy, or a source whose export
    # simply begins with credential-shaped text, put those bytes on disk verbatim while this call
    # passed no redactor at all.
    matches, why = capability.format_matches(kind, payload, None, redactor=session.redact_text)
    if not matches:
        # `why` is FULLY redacted by `format_matches`' chokepoint, and a second `session.redact_text`
        # here was deleted rather than kept as defence in depth: it received only the already
        # TRANSFORMED 16-character fragment, so it could not match a secret that truncation or
        # stripping had already rewritten -- which is precisely how the round-4 leak survived a guard
        # that looked like one. A guard that cannot guard invites the confidence that hid it.
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


def log_progress(index: int, total: int, record: dict[str, Any], redactor=None) -> None:
    """One line per view: proof of rows captured, or a loud, classified failure.

    ⚠️ The console is the THIRD artifact, after the manifest and the files. A view NAME is response
    data -- a reflected token can arrive as one -- and this line used to slice it to 34 characters
    before anything scrubbed it, which is the round-4 defect at a boundary round 4 never looked at.
    CI keeps its logs, so "only the terminal" is not a mitigation.
    """
    data = record.get("data", {})
    name = redacted_note(record.get("view_name"), redactor, limit=34)
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

    ⚠️ A render leg is absent for TWO different reasons and they must not collapse into one.
    ``capture_view`` returns before attempting any render once the **data** leg has failed -- all four
    routes come from the same VizQL render, so being refused three more times costs metered calls to
    learn nothing. Those renders are absent *because of their prerequisite*, and inventing an
    independent ``not_captured`` failure for each put a purely credential-blocked view into
    ``blocked`` **and** ``failed`` at once, where ``failed`` wins and the run exits 3 instead of the
    human-actionable 2. The prerequisite's own status is propagated instead, so one root cause is
    counted once -- and a genuinely broken data leg still yields failing renders.
    """
    data_status = (record.get("data") or {}).get("status")
    absent = "not_captured" if data_status in (None, "ok") else data_status
    statuses = []
    for kind, leg in (("png", "image"), ("svg", "svg"), ("pdf", "pdf")):
        if leg in record:
            statuses.append(record[leg].get("status"))
        elif kind in requested:
            statuses.append(absent)
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

    ⚠️ Code 5 must NOT swallow code 2. When every selected view is credential-blocked no render could
    have been produced by anything we control, and the one actionable instruction is "a human must
    reauthorize the source in Tableau" -- code 2. Code 5 there points the operator at our capability
    probe instead: the same debug-the-wrong-system cost that made 3 wrong for the same input. A
    *partial* block still yields 5, because the absence is then not explained by the credential.
    """
    sets = _partition(records, run.requested_renders)
    blocked, failed, complete = sets["blocked"], sets["failed"], sets["complete"]
    rendered = sum(1 for r in records if any(r.get(leg, {}).get("status") == "ok" for leg in ("image", "svg", "pdf")))
    # "Nothing rendered, and the credential explains ALL of it" -- the one case where an absent
    # reference is code 2's problem rather than code 5's.
    credential_only = rendered == 0 and bool(blocked) and len(blocked) == len(records)
    reference_missing = run.reference_required and rendered == 0 and not credential_only
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
    # THE SINK. Everything above this line is a source, and five review rounds went one source at a
    # time: `raw_get`, the 200-mismatch diagnostic, a case-folded Content-Type, a truncated body quote,
    # a `<detail>` capture group -- and then a field that was never a diagnostic at all, a successful
    # CSV's own header row copied into `data.columns`. Guarding sources one at a time cannot terminate,
    # because the next leak is by definition the one nobody enumerated. So the manifest is scrubbed as
    # a WHOLE, immediately before it is serialised, and every string in it is covered regardless of
    # how it got there.
    manifest, sink_hits = scrub_tree(manifest, run.session.redact_text)
    # Firing is itself a defect report: it means a source let something reach the sink. Recorded IN
    # the artifact, and named, so the finding survives the terminal scrollback.
    manifest["credential_scrubbed_at_sink"] = sink_hits
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if sink_hits:
        LOG.error(
            "\nThe manifest sink had to redact %d field(s) -- %s. A credential reached the manifest "
            "through a path that should have scrubbed it upstream; the file is safe, the code is not. "
            "Do NOT assume which credential: a reflected SESSION TOKEN can arrive as a view name from "
            "an authenticated metadata call, which the export seam never sees. Find the source before "
            "deciding this is the cosmetic PAT-name case.",
            len(sink_hits),
            ", ".join(sink_hits[:8]),
        )

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
    _log_blocked_and_stale(records, blocked, capability_report, run.session.redact_text)
    if reference_missing:
        LOG.error(
            "\nA reference render was REQUIRED (--reference-best) but NONE was captured across %d "
            "view(s). The capability probe did not settle on a tier, so nothing was requested. This "
            "run has data only and must not be treated as a complete capture; re-run once the probe "
            "can reach a renderable view, or name a tier explicitly with --images/--svg/--pdf.",
            len(records),
        )
    elif run.reference_required and credential_only:
        # Deliberately NOT code 5: nothing rendered, but the cause is entirely upstream of us and the
        # blocked list above already names the whole fix.
        LOG.error(
            "\nA reference render was REQUIRED (--reference-best) and none was captured, because ALL "
            "%d selected view(s) are credential-blocked on the Tableau side. That is exit code 2, not "
            "5: no render route could have succeeded, and re-probing our capability ladder cannot "
            "help. Reauthorize the source(s) named above in Tableau and re-run.",
            len(records),
        )
    if not records:
        return 4
    if reference_missing:
        return 5
    if failed:
        return 1 if complete else 3
    return 2 if blocked else 0


def _log_blocked_and_stale(
    records: list[dict[str, Any]], blocked: list[dict[str, Any]], capability_report: dict[str, Any] | None, redactor
) -> None:
    """The two loud, differently-actionable warning classes, plus the probe's own warnings.

    Every response-derived name goes through the chokepoint: these lines run BEFORE `scrub_tree` has
    been applied to anything (it returns a scrubbed copy, it does not mutate `records`), so the
    console would otherwise print the one thing the manifest was careful not to.
    """
    if blocked:
        LOG.warning(
            "\n%d view(s) need a credential ON THE TABLEAU SIDE - no retry can fix this, a human must "
            "reauthorize the source in Tableau:",
            len(blocked),
        )
        for record in blocked:
            detail = next(
                (
                    record.get(leg, {}).get("detail")
                    for leg in ("data", "image", "svg", "pdf")
                    if record.get(leg, {}).get("detail")
                ),
                None,
            )
            LOG.warning(
                "  - %s (%s): %s",
                redacted_note(record.get("view_name"), redactor, limit=60),
                redacted_note(record.get("workbook_name"), redactor, limit=60),
                redacted_note(detail, redactor, limit=200),
            )
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
    api_overrides: dict[str, str] = {}
    if args.reference_best and views:
        capability_report = capability.probe_render_capability(session, env, views)
        capability.apply_selected_tier(capability_report, wants, api_overrides, env)

    records, started = [], time.perf_counter()
    for index, view in enumerate(views, 1):
        record = capture_view(session, view, out_dir, frozenset(wants), api_overrides)
        record["workbook_name"] = workbook_names.get(record["workbook_luid"])
        records.append(record)
        log_progress(index, len(views), record, session.redact_text)

    exit_code = write_manifest(
        records,
        CaptureRun(session, env, out_dir, started, frozenset(wants), bool(args.reference_best)),
        capability_report,
    )
    session.sign_out()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
