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

The two legs are INDEPENDENT, and the timeout is yours to set (issue #423)
-------------------------------------------------------------------------
A failed ``/data`` no longer skips the renders. Field evidence, reported by a customer and reproduced
offline: *Daily Monitoring* failed ``/data`` twice with ``HTTP 0 / TimeoutError: read operation timed
out`` -- and because the render loop sat behind ``if data.status != "ok": return``, no image was ever
ATTEMPTED. On a third batch **both** succeeded, 905,098 bytes of PNG. Its neighbour *Availability
Summary by Tail* failed identically three times across two days and therefore has no ``image`` key in
any record at all, which is worse than a missing image: with no reference render, an equivalent
visual-fidelity defect on that page is not merely unverified, it is **unfalsifiable**.

Two knobs and one guard rail come out of that:

* **``--rest-timeout``** exposes what was a hardcoded 180s module constant. Three identical
  ``TimeoutError`` failures across two days is not a network blip; it is a view whose query cannot
  export server-side in the time allowed.
* **``--retry-budget`` now tracks ``--rest-timeout`` (2x) instead of being frozen at 360s.** WARNING: The
  budget is charged from BEFORE attempt 1, so ONE full-timeout failure spends half of it and TWO
  exhaust it -- the run gives up well short of ``--max-attempts 5``, **by design**. Freezing the
  budget while the timeout rose would have put it *below* one timeout, removing every retry from
  precisely the slow failure the operator raised the timeout to survive.
* A salvage render (one whose data leg already failed) gets **one attempt and no retry budget**, the
  first one that fails for a reason the view controls stops the rest, and every salvage leg shares
  ONE budget of ``2 x --rest-timeout``.

WARNING: the salvage bound is the shared admission budget plus a small settling margin, and the two
things that make that true are BOTH necessary. A per-request timeout bounds one socket OPERATION, not
a request: measured against a local server trickling one byte every 0.08s, ``timeout=0.1`` returned
**HTTP 200 after 0.479s** -- 4.8x nominal, no error at any layer. And trickling HEADERS are worse,
because they run before any body check exists: a 0.15s deadline returned after **1.378s**. So a
salvage leg carries an absolute deadline into the transport that covers the WHOLE request -- a
watchdog aborts the connection at that instant whatever phase it is in, and the body read checks the
same clock between chunks. Other legs carry no deadline on purpose: the data leg streams a real
export and must not be truncated for making slow progress. What is still NOT bounded is name
resolution, which happens before a socket exists.

WARNING: **A requested render leg now ALWAYS gets a record**, even when it is deliberately not attempted
(``not_attempted``, or the data leg's own ``source_credential``). An absent key therefore means "not
requested" and nothing else. The manifest additionally counts and NAMES the views for which no
requested render was obtained (``render_unestablished``), because an unassessable state that reads as
a clean one is the failure mode this whole capture exists to prevent.
"""

# This module went over the 1200-line cap at the merge of #475 (the SVG version gate) and #480 (CSV
# certification), not in either change alone: master sat at 1195 lines, five short of the cap, so the
# first branch to integrate pays for both. The cap is a proxy for "this module does too much" and
# that reading is fair here -- it carries a near-duplicate of the Tableau REST client
# `assess_estate.py` also carries, and extracting that shared transport is the real fix. It is a
# refactor of two files that four branches are editing concurrently, so it must not ride along with a
# customer-defect fix. Trimming the explanatory comments to squeeze back under would trade documented
# knowledge for a number, which is the wrong trade in this codebase. Suppressed deliberately.
# pylint: disable=too-many-lines

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tableau_render_capability as capability  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_view_types  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_payload_facts import (  # noqa: E402  # pylint: disable=wrong-import-position
    CSV_CERTIFIED,
    CSV_REFUSAL_DETAIL,
    CSV_REFUSALS,
    CSV_TRANSPORT_UNSUPPORTED_CONTENT_ENCODING,
    certify_csv,
    payload_is_complete,
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
    secret_forms,
)

# The verdict layer: records -> manifest -> exit code. It imports nothing from here, so the pair is
# acyclic; it takes the session duck-typed for its two counters and the redactor.
from tableau_oracle_manifest import (  # noqa: E402  # pylint: disable=wrong-import-position
    NOT_ATTEMPTED,
    SVG_MIN_API_VERSION,
    SVG_UNSUPPORTED_STATUS,
    SVG_VERSION_MARKER,
    CaptureRun,
    data_leg_fields,
    log_progress,
    write_manifest,
)

# Every bound the run works under, re-exported so callers and tests keep reading them off this
# module. Split out when this file hit its line ceiling a third time -- see that module's docstring
# for why the three time-related words here mean three different things.
from tableau_capture_policy import (  # noqa: E402  # pylint: disable=wrong-import-position
    CREDENTIAL_REFLECTED,
    DEFAULT_MAX_AGE_MINUTES,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_BUDGET_SEC,
    MAX_REAUTH_PER_VIEW,
    REST_TIMEOUT_SEC,
    SALVAGE_BUDGET_MULTIPLIER,
    SALVAGE_RETRY,
    SESSION_LOST_CODE,
    TRANSIENT_STATUSES,
    RetryPolicy,
    backoff_delay,
    build_retry_policy,
    validate_max_age,
)

# Re-exported, not used here: callers and tests read these off THIS module
# (`oracle.BACKOFF_BASE_SEC`, `oracle.retry_admission_floor`) -- nineteen references across three
# suites, which is a public surface whether or not it was ever declared one. `__all__` is what makes
# that surface explicit to BOTH linters and stops either one deleting a name that three files import
# at module load: ruff honours the redundant-alias form, pylint rejects it as a useless alias, and
# only `__all__` satisfies both.
__all__ = [
    "BACKOFF_BASE_SEC",
    "BACKOFF_CAP_SEC",
    "DEFAULT_MAX_AGE_MINUTES",
    "RETRY_ADMISSION_FLOOR_SEC",
    "default_retry_budget",
    "retry_admission_floor",
    "validate_max_age",
]

from tableau_capture_policy import (  # noqa: E402  # pylint: disable=wrong-import-position
    BACKOFF_BASE_SEC,
    BACKOFF_CAP_SEC,
    RETRY_ADMISSION_FLOOR_SEC,
    default_retry_budget,
    retry_admission_floor,
)

# ⚠️ Imported as plain NAMES, not reached through the module. `tableau_http._request(...)` is
# `protected-access` to pylint (W0212, measured), and any alias would rename the call away from
# `TAINTING_CALLS` in `tests/test_diagnostic_redaction.py`, silently un-tainting every call site.
# The module-level `_request` and this class's `_request` method are deliberately the same name: the
# method is now a thin adapter that builds the Request and delegates to the one hardened round trip.
from tableau_http import (  # noqa: E402  # pylint: disable=wrong-import-position
    CONTENT_ENCODING_UNSUPPORTED,
    FRAMING_CHUNKED,
    FRAMING_CONTENT_LENGTH,
    NETWORK_ERROR_STATUS,
    RESPONSE_CONTENT_ENCODING_HEADER,
    RESPONSE_FRAMING_HEADER,
    _request,
    header_value,
    response_content_encoding,
    response_framing,
)

LOG = logging.getLogger("tableau-oracle")

# A Tableau LUID is a UUID. Checkable in full, so a value matching it is provably not a credential --
# which is what lets `artifact_stem` be an allowlist rather than one more screen.
_LUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class SiteCredentials:
    """Everything needed to reach one Tableau site. The PAT secret is never logged or serialised."""

    base: str
    site: str
    pat_name: str
    pat_secret: str
    version: str


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


class TableauSession:
    """Minimal stdlib Tableau REST client that survives mid-loop session loss and transient faults."""

    def __init__(
        self, creds: SiteCredentials, retry: RetryPolicy | None = None, timeout_sec: float | None = None
    ) -> None:
        self._creds = creds
        self.retry = retry or RetryPolicy()
        # Per-request socket timeout. An instance attribute rather than a module constant because
        # #423 measured a real view that cannot export within 180s: "Availability Summary by Tail"
        # returned `HTTP 0 / TimeoutError: read operation timed out` on three separate runs across two
        # days, and an operator had no way to grant it more time short of editing this file.
        self.timeout_sec = REST_TIMEOUT_SEC if timeout_sec is None else timeout_sec
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
        deadline: float | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        """One HTTP round trip. Never raises for a network failure -- returns status 0 when no HTTP
        response arrived at all (reset/DNS/refused/timeout), so the retry loop can treat a reset
        connection and a gateway 503 the same way. When a response DID arrive but reading its body
        failed mid-stream, the real HTTP status is kept (a 503 is still usefully a 503) and the body
        read error is reported in the payload -- either way, this method does not raise.

        ``api`` overrides the client api-version for this one call. Only the capability probe uses it,
        to re-measure a version-gated feature at its documented floor rather than inferring support.

        ``deadline`` is an absolute ``time.monotonic`` instant and is the ONLY end-to-end bound
        available: ``timeout_sec`` bounds one socket operation, so a response that keeps trickling
        outlives it indefinitely (measured: HTTP 200 after 4.8x a 0.1s timeout). Opt-in, because a
        deadline belongs to a caller's policy -- the data leg streams a real export and must not be
        truncated for making slow progress.

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
        return _request(req, timeout=self.timeout_sec, redactor=self._redact_response, deadline=deadline)

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
    def export(  # pylint: disable=too-many-locals
        self,
        path: str,
        *,
        api: str | None = None,
        retry: RetryPolicy | None = None,
        hard_deadline: float | None = None,
    ) -> tuple[bytes, float, dict[str, Any]]:
        """GET a content-export endpoint, recovering from session loss and transient failures.

        ``api`` overrides the client api-version for this export. It exists because the capability
        probe can RECOVER a tier by re-probing at its documented floor: without honouring the version
        that actually answered, the run is told "svg is available" and then fetches at the configured
        version, where the same request is still refused (measured: floor 3.29 ``available``, the same
        request at the configured 3.21 ``unsupported``).

        ``hard_deadline`` is an absolute ``time.monotonic`` instant passed to the transport, and it is
        what makes a wall-clock claim about this export true rather than assumed: the per-request
        timeout bounds one socket operation only, so a trickling response outlives it (measured:
        HTTP 200 after 4.8x a 0.1s timeout). Only the salvage path supplies one. ⚠️ It is deliberately
        NOT called ``deadline``: the local of that name below is the retry-ADMISSION deadline, which
        an in-flight attempt may legitimately overrun. Two deadlines with opposite guarantees sharing
        one name is how the distinction got lost in the first place.

        ``retry`` overrides the session policy for THIS export only. The budget is therefore per-leg,
        never a pool the legs draw down: a salvage render after a failed data leg gets one attempt and
        no budget (:data:`SALVAGE_RETRY`), while the data leg that preceded it kept the full session
        policy.

        ⚠️ Re-authentication is bounded by ``MAX_REAUTH_PER_VIEW`` and by ``sign_in``'s own attempts
        rather than by the admission deadline -- deliberately, because abandoning a view mid-re-auth
        after a *recoverable* session loss throws away estate progress for no gain. But it is now ALSO
        refused on the policy's FINAL attempt, and that is a fix, not a tightening (review round 1,
        finding 3): the loop ``continue``s into an attempt that does not exist, so the re-auth could
        not possibly be used. Measured on a one-attempt salvage leg: one render request, one full
        ``sign_in`` -- which runs on the SESSION policy and can consume several more request timeouts
        -- and then the export failed anyway. That is unbounded work in service of nothing, and it is
        what made the stated "at most one timeout" salvage bound false.

        Returns ``(body, elapsed_sec, stats)`` where ``stats`` records how much recovery was needed --
        ``reauths``, ``retries`` and the reasons -- plus the response's declared ``content_type``,
        body framing and content coding.
        Recovery is deliberately **recorded, not silent**: a capture that quietly healed itself looks
        identical to one that never had a problem, which is exactly how a partially-truncated result
        comes to be trusted. ``content_type`` rides here because a CSV carries no signature, so the
        declaration is the only thing that can certify a ``/data`` body as data. ``response_framing``
        rides beside it because syntactically valid CSV has no terminator: Tableau Cloud was measured
        as the exact decoded chunked shape on 6/6 data/image exports, and that chunk terminator -- not
        a Content-Length Tableau does not send there -- is what makes early EOF detectable. The content
        coding is reduced to ``identity`` versus unsupported rather than decompressed in this PR.

        Raises :class:`ExportFailed` for anything not worth retrying, so a genuinely broken view is
        never recorded as an empty success.
        """
        policy = retry or self.retry
        reauths = 0
        retries: list[str] = []
        deadline = time.monotonic() + policy.budget_sec
        for attempt in range(1, policy.max_attempts + 1):
            started = time.perf_counter()
            status, payload, headers = self._request("GET", path, api=api, deadline=hard_deadline)
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
                stats = {"reauths": reauths, "retries": len(retries), "retry_reasons": retries}
                # Response data, so both callers POP it before merging the rest into a leg record.
                # It rides in `stats` so every caller keeps unpacking three values.
                stats["content_type"] = header_value(headers, "Content-Type")
                stats["response_framing"] = header_value(headers, RESPONSE_FRAMING_HEADER) or response_framing(headers)
                content_encoding = header_value(headers, RESPONSE_CONTENT_ENCODING_HEADER)
                stats["content_encoding"] = content_encoding or response_content_encoding(headers)
                return payload, elapsed, stats
            raw = payload.decode("utf-8", "replace")
            # ONE call, with the redactor inside. `classify_export_error` classifies on the raw text
            # -- redaction is handed the human-chosen PAT NAME, and a short one mangles `401002` into
            # something read as a permanent credential failure rather than the recoverable session
            # loss it is -- while building every reported detail from the redacted copy. The previous
            # shape called it twice and threw away the raw call's detail, which was safe only by
            # convention: one keystroke (`kind, detail = classify(status, raw)`) reinstated the leak.
            kind, detail = classify_export_error(status, raw, redactor=self._redact_response)
            if kind == "session_lost" and reauths < MAX_REAUTH_PER_VIEW and attempt < policy.max_attempts:
                # Re-auth is a SEPARATE recovery path from transient retry, and is deliberately NOT
                # gated by the admission deadline. It is bounded instead by MAX_REAUTH_PER_VIEW (and
                # sign_in's own max_attempts), because abandoning a view mid-re-auth after a
                # recoverable session loss throws away estate-capture progress for no gain. Since the
                # budget is a retry-admission deadline and not a hard wall-clock cap (see RetryPolicy),
                # a sign_in that blocks can push this view past the deadline -- consistent, not a
                # violation. The very next iteration's transient check charges any elapsed re-auth
                # time against the deadline, so a slow re-auth still curtails FURTHER transient retries.
                #
                # ⚠️ `attempt < policy.max_attempts` is the round-1 finding-3 fix, and it is a fix
                # rather than a tightening: re-authenticating on the FINAL attempt `continue`s into an
                # iteration the range does not contain, so the new token is never used by this export.
                # Measured on a one-attempt salvage leg -- one render request plus a full `sign_in`
                # (which runs on the SESSION policy, so several more request timeouts) and then the
                # export failed anyway. Unbounded work in service of nothing, and the reason the
                # "at most one timeout" salvage bound did not hold.
                reauths += 1
                self.reauth_count += 1
                retries.append("session_lost")
                LOG.debug("session lost (401002); re-authenticating (%d)", self.reauth_count)
                self.sign_in()
                continue
            if kind == "session_lost" and attempt >= policy.max_attempts:
                raise ExportFailed(
                    f"GET {path} -> HTTP {status}",
                    kind,
                    f"the session was lost on attempt {attempt} of {policy.max_attempts}, the last this "
                    f"policy allows, so re-authenticating could not have been used by this export and "
                    f"was skipped. The next export re-authenticates on its own first attempt.",
                )

            if kind == "transient" and attempt < policy.max_attempts and time.monotonic() < deadline:
                delay = backoff_delay(attempt, header_value(headers, "Retry-After"))
                if time.monotonic() + delay > deadline:
                    raise ExportFailed(f"GET {path} -> retry budget exhausted", "transient", detail)
                self.retry_count += 1
                retries.append(detail[:80])
                LOG.warning("  transient (%s); retry %d/%d in %.1fs", detail[:60], attempt, policy.max_attempts, delay)
                time.sleep(delay)
                continue

            raise ExportFailed(
                f"GET {path} -> HTTP {status}",
                kind,
                detail or redacted_note(payload, self._redact_response, limit=200),
            )
        raise ExportFailed(f"GET {path} -> exhausted {policy.max_attempts} attempts", "transient", "")


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


def capture_view(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    session: TableauSession,
    view: dict[str, Any],
    out_dir: Path,
    wants: frozenset[str] = frozenset(),
    api_overrides: dict[str, str] | None = None,
    max_age: int = DEFAULT_MAX_AGE_MINUTES,
) -> dict[str, Any]:
    """Capture one view's data plus every requested render, keyed by view LUID.

    ``wants`` is a set drawn from ``_RENDER_ROUTES`` ("png", "svg", "pdf") rather than a boolean per
    format: three parallel booleans made the call site unreadable at the third one, and every new
    route would have added another positional flag that some caller forgets to pass.

    ``api_overrides`` maps a kind to the api-version that tier was PROVED to answer at. The capability
    probe can recover a tier by re-probing at its documented floor, and a capture that ignores that
    version fetches at the configured one and fails on a tier the manifest already promised.
    """
    max_age = validate_max_age(max_age)
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
        # `dashboard` / `worksheet` / `unknown` (#402). REST cannot tell these apart, so this is
        # joined from the Metadata API BY LUID and stamped onto the view upstream. `unknown` is a
        # real, expected value - an older server or a disabled Metadata API produces it - and a
        # consumer must treat it as "cannot establish", never as either type.
        "view_type": view.get(tableau_view_types.VIEW_TYPE_KEY, tableau_view_types.UNKNOWN),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "max_age_minutes": max_age,
    }
    try:
        stem = artifact_stem(view_luid)
    except ValueError as exc:
        # A view whose identifier is not a LUID is not one we will invent a filename for. Every other
        # candidate string on this record came out of a Tableau response.
        record["data"] = {
            "status": "failed",
            "error": str(exc),
            "detail": "unusable view identifier",
            "max_age_minutes": max_age,
        }
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
            "max_age_minutes": max_age,
        }
        return record

    record["data"] = _capture_data(session, view_luid, out_dir, stem, max_age=max_age)
    _capture_renders(
        session,
        record,
        wants,
        _RenderTargets(out_dir, stem, api_overrides or {}, max_age=max_age),
    )
    return record


@dataclass(frozen=True)
class _RenderTargets:
    """Where a view's renders are written, and at which api-version each tier was proved to answer.

    Three values that only ever travel together, bundled so ``_capture_renders`` keeps a signature a
    reader can hold: the output root, the LUID-derived filename stem, and the per-kind api override.
    """

    out_dir: Path
    stem: str
    api_overrides: dict[str, str]
    max_age: int = DEFAULT_MAX_AGE_MINUTES


def _capture_renders(
    session: TableauSession, record: dict[str, Any], wants: frozenset[str], targets: _RenderTargets
) -> None:
    """Attempt every REQUESTED render, and record a status for every one that is not attempted.

    ⚠️ **A failed ``/data`` no longer skips the renders** (#423). They are different endpoints, and a
    field capture proved the two can disagree: "Daily Monitoring" failed ``/data`` twice with
    ``HTTP 0 / TimeoutError: read operation timed out`` and never had its image attempted, then on a
    third batch produced BOTH -- 905,098 bytes of PNG. Its neighbour "Availability Summary by Tail"
    failed the same way three times across two days and therefore has no ``image`` key in any record
    at all. That is the expensive half: with no reference render, an equivalent visual-fidelity defect
    on that page is not merely unverified, it is **unfalsifiable**.

    Three things bound the cost, because a view whose ``/data`` is slow may well be slow to render too:

    * a salvage render gets **one attempt and no retry budget** (``RetryPolicy(max_attempts=1,
      budget_sec=0)``). Its job is to establish whether an image EXISTS, and re-asking a view that
      just spent a full budget failing is the same slow question again. The budget is per-leg, and
      the salvage leg's is zero -- the data leg's own behaviour is unchanged.
    * the first salvage leg that fails **for a reason the view controls** stops the remaining ones,
      recorded ``not_attempted``. All render routes come from the same VizQL render (measured:
      ``image``, ``image?format=svg``, ``pdf`` and ``data`` fail identically on ``data sources not
      connected``). ``unsupported_api_version`` and ``format_mismatch`` do NOT stop them: both are
      configuration faults answered instantly, not evidence the view is unwell.
    * ⚠️ **one deadline SHARED by every salvage leg** (:data:`SALVAGE_BUDGET_MULTIPLIER`), enforced
      twice over. The rules above bound *attempts*, not wall clock: three ``format_mismatch`` legs
      were each attempted with no cross-leg limit, **539.7 s against a stated 180 s bound**. A leg is
      now ADMITTED only while a whole request timeout still remains, AND carries that same instant
      into the transport as an **end-to-end deadline** covering the whole request -- because admission
      alone bounds nothing when one request is unbounded, and it is: a per-request timeout bounds one
      socket OPERATION, so neither a trickling body (**HTTP 200 after 0.479 s** on a 0.1 s timeout)
      nor trickling HEADERS (**1.378 s** against a 0.15 s deadline) ever trip it.

    So salvage costs at most ``SALVAGE_BUDGET_MULTIPLIER x`` the request timeout of admission plus a
    small settling margin, whatever the tier count -- one timeout in practice, since reaching the
    second admission needs the first leg to fail almost instantly. ⚠️ **What is ENFORCED is the
    admission budget; the transport deadline is hardening on top of it, not a wall-clock guarantee.**
    Everything from the first live socket onward is watchdogged; everything BEFORE it is not. That is
    name resolution *and* -- measured in round 5, and not the same thing -- ``socket.create_connection``
    walking every resolved address with the full timeout applied to each, **0.177s against a 0.110s
    ceiling with the timer not yet armed**. See :func:`tableau_http._request`'s phase table, which is
    the one place this is stated per phase.

    ⚠️ **FIVE earlier statements of this bound were wrong, differently, and all five are worth
    keeping.** A flat "one timeout" ignored that nothing capped the legs collectively and that a
    ``session_lost`` on a one-attempt leg triggered a full ``sign_in`` on the SESSION policy (now
    refused on the final attempt -- see :meth:`TableauSession.export`). A "hard 2x" rested on a
    ``urllib`` property that does not hold -- and the virtual-clock tests that "proved" it modelled
    every request as bounded by its nominal timeout, so the instrument shared the defect with the
    thing it measured and structurally could not fail. Then ``admission + one socket timeout`` was
    still wrong, because the deadline covered only the BODY: every real-socket test sent its headers
    instantly and trickled only the body, so the fixture could not reach the phase that was unbound.
    Then the watchdog was armed **after** the connection sequence, so a proxy trickling its
    ``CONNECT`` response ran for **1.241s against a 0.20s ceiling** with the timer not yet started --
    and no fixture tunnelled or negotiated TLS, so once again nothing could reach it. Then "DNS is
    the only residual" hid address iteration behind it, and every fixture connected to ONE localhost
    address so none could observe an all-address failure.

    ⚠️ **Five rounds, five findings, each one the phase immediately BEFORE wherever the watchdog was
    then armed. That is not five defects; it is one wrong shape of claim.** Guarding points in a
    connection sequence nobody can enumerate by reading will keep losing to the next point. The claim
    is therefore stated as what is enforced -- admission, plus a watchdog from the first live socket
    -- and the residual is pinned by an executable test rather than a sentence, because the sentence
    has now been wrong four times. The question that found every one of them is still the one to ask
    of any bound here: **what can the control not see?**

    ⚠️ ``source_credential`` and ``credential_reflected`` skip the renders ENTIRELY, and the skipped
    legs inherit the data leg's status rather than a failure of their own. A credential block is a
    shared root cause no render can get past, and inventing an independent failure for each leg put a
    purely credential-blocked view into ``blocked`` AND ``failed`` at once, where ``failed`` wins and
    the run exits 3 instead of the human-actionable 2 (see ``_render_statuses``). A reflected
    credential is a security refusal: more exports while something is echoing our request data is
    exactly the wrong move.

    ⚠️ Every requested leg gets a key **whichever branch it takes**. An absent leg therefore means
    "not requested" and nothing else -- before this, absent meant that OR "never attempted", and a
    view with no establishable image was indistinguishable from a data-only capture.
    """
    data_status = record["data"]["status"]
    if not wants:
        return
    max_age = validate_max_age(targets.max_age)
    if data_status in _SHARED_ROOT_CAUSE:
        for kind in _RENDER_ROUTES:
            if kind in wants:
                record[_LEG_OF[kind]] = {
                    "status": data_status,
                    "attempted": False,
                    "reason": (
                        "the data leg was blocked at the source, and every render route comes from "
                        "the same VizQL render, so no render could have succeeded"
                    ),
                    "max_age_minutes": max_age,
                }
        return
    salvage = data_status != "ok"
    timeout = float(getattr(session, "timeout_sec", REST_TIMEOUT_SEC))
    deadline = time.monotonic() + SALVAGE_BUDGET_MULTIPLIER * timeout
    blocked_by = ""
    for kind in _RENDER_ROUTES:
        if kind not in wants:
            continue
        leg = _LEG_OF[kind]
        refusal = blocked_by or (_salvage_exhausted(deadline, timeout) if salvage else "")
        if refusal:
            record[leg] = {
                "status": NOT_ATTEMPTED,
                "attempted": False,
                "reason": refusal,
                "max_age_minutes": max_age,
            }
            continue
        record[leg] = _capture_render(
            session,
            record["view_luid"],
            targets.out_dir / "images" / f"{targets.stem}.{_RENDER_EXTENSIONS[kind]}",
            kind,
            _RenderOptions(
                targets.api_overrides.get(kind),
                SALVAGE_RETRY if salvage else None,
                deadline if salvage else None,
                max_age=max_age,
            ),
        )
        if salvage and record[leg]["status"] in _VIEW_HEALTH_FAILURES:
            blocked_by = (
                f"the {leg} render came from the same VizQL render and failed, so this tier was not asked for as well"
            )


def _salvage_exhausted(deadline: float, timeout: float) -> str:
    """``""`` while a whole request timeout still fits inside the shared salvage budget, else why not.

    ⚠️ The admission test is ``remaining >= timeout``, not ``now < deadline``, and that difference is
    what makes the bound HARD rather than advisory. Admitting a leg merely because the deadline has
    not passed lets a request start at ``deadline - epsilon`` and then block for a whole timeout, so
    the real ceiling would be budget + timeout and would creep with every tier added. Requiring a
    whole timeout to remain means the budget is never exceeded by more than the rounding on one
    ``monotonic()`` read.

    This is the OPPOSITE choice from ``RetryPolicy.budget_sec``, which is documented as an *admission*
    deadline that an in-flight attempt may legitimately overrun. That is right for retries of one
    request, where abandoning a nearly-finished recovery wastes what it already spent; it is wrong
    here, because the whole point of the salvage budget is a ceiling a caller can state.
    """
    remaining = deadline - time.monotonic()
    if remaining >= timeout:
        return ""
    return (
        f"the shared salvage budget ({SALVAGE_BUDGET_MULTIPLIER:.0f}x the {timeout:.0f}s request "
        f"timeout) has {max(remaining, 0.0):.0f}s left, which cannot fit another whole request. The "
        f"data leg already failed, so this tier was not attempted rather than letting one slow view "
        f"cost a timeout per tier."
    )


def _capture_data(  # pylint: disable=too-many-locals
    session: TableauSession,
    view_luid: str,
    out_dir: Path,
    stem: str,
    max_age: int = DEFAULT_MAX_AGE_MINUTES,
) -> dict[str, Any]:
    """The numeric oracle for one view: Tableau's own aggregated, display-formatted values.

    ⚠️ **An HTTP 200 is not evidence until the body is certified as CSV** (:func:`certify_csv`): a
    refusal is ``format_mismatch``, with nothing written and no shape recorded. An uncertified but
    successful body keeps ``ok`` and its bytes, written to ``unassessable/<stem>.bin`` under
    :data:`RETAINED_PATH_KEY` -- never ``data/<stem>.csv``, never under ``path``, so no numeric
    consumer can read it as evidence. Why it is structural, not another flag:
    :func:`tableau_oracle_manifest.withhold_uncertified_evidence`.
    """
    max_age = validate_max_age(max_age)
    try:
        payload, elapsed, stats = session.export(f"/sites/{session.site_id}/views/{view_luid}/data?maxAge={max_age}")
    except ExportFailed as exc:
        return {"status": exc.kind, "error": str(exc), "detail": exc.detail, "max_age_minutes": max_age}
    framing = stats.get("response_framing")
    content_encoding = stats.get("content_encoding")
    content_type = stats.pop("content_type", None)
    if content_encoding == CONTENT_ENCODING_UNSUPPORTED:
        certification = CSV_TRANSPORT_UNSUPPORTED_CONTENT_ENCODING
    elif framing not in {FRAMING_CHUNKED, FRAMING_CONTENT_LENGTH}:
        certification = str(framing)
    else:
        certification = certify_csv(payload, content_type)
    if certification in CSV_REFUSALS:
        return {
            "status": "format_mismatch",
            "certification": certification,
            "detail": CSV_REFUSAL_DETAIL[certification],
            "bytes": len(payload),
            "elapsed_sec": round(elapsed, 2),
            "max_age_minutes": max_age,
            **stats,
        }
    path, naming = data_leg_fields(out_dir, stem, certification)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    record = {
        "status": "ok",
        "certification": certification,
        **naming,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "elapsed_sec": round(elapsed, 2),
        "max_age_minutes": max_age,
        **stats,
    }
    if certification == CSV_CERTIFIED:
        record.update(summarise_csv(payload))
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
# The record key each tier is written under. `png` is spelled `image` for historical reasons: it was
# the only render there was, and renaming the key now would orphan every manifest already captured.
_LEG_OF = {"png": "image", "svg": "svg", "pdf": "pdf"}

# A data-leg status for which attempting a render is provably wasted, so the renders are skipped and
# INHERIT this status rather than inventing one. `source_credential` is a shared root cause -- all
# four routes come from the same VizQL render and fail identically on `data sources not connected` --
# and `credential_reflected` is a security refusal, where more exports is the wrong move.
_SHARED_ROOT_CAUSE = frozenset({"source_credential", CREDENTIAL_REFLECTED})

# A render-leg status that says the VIEW could not answer, as opposed to one that says our request
# was misconfigured. Only the former stops the remaining salvage legs: `unsupported_api_version` is a
# version gate refused instantly with a 400, and `format_mismatch` arrived as a 200, so neither is
# evidence that asking the next tier will be slow or futile.
_VIEW_HEALTH_FAILURES = frozenset({"transient", "failed", "session_lost", "source_credential", CREDENTIAL_REFLECTED})


@dataclass(frozen=True)
class _RenderOptions:
    """HOW one render leg is fetched: at which api-version, with how much recovery, until when, and with what maxAge.

    Bundled rather than passed loose because they only ever travel together, and because ``kind``
    -- the WHAT -- must stay a first-class parameter: the redaction gate taints a whole constructed
    object from any tainted field, and folding the (clean) tier name in beside a ``retry`` derived
    from the data leg's status would have marked the tier name response-derived, which it is not.

    ``hard_deadline`` is the SHARED salvage deadline, and it is what turns a bound that was assumed
    into one that is enforced -- see :func:`_capture_renders`.
    """

    api: str | None = None
    retry: RetryPolicy | None = None
    hard_deadline: float | None = None
    max_age: int = DEFAULT_MAX_AGE_MINUTES


def _capture_render(  # pylint: disable=too-many-locals
    session: TableauSession, view_luid: str, path: Path, kind: str, options: _RenderOptions
) -> dict[str, Any]:
    """Fetch one rendered form of a view and describe what was actually obtained.

    All three rungs come from the same VizQL render, so none survives a workbook whose data sources
    are not connected -- measured, ``image``, ``image?format=svg``, ``pdf`` and ``data`` all return the
    same HTTP 400 ``ExportViewException: Error: data sources not connected``. What differs is the
    CEILING and the REACH: PNG is capped at 2x a dashboard's declared size but works back to API 2.5;
    SVG is resolution-independent but needs 3.29; PDF is vector with embedded fonts from API 2.8.

    ``options.api`` is the version this tier was PROVED to answer at, when a floor re-probe recovered
    it. ``options.retry`` narrows the recovery for this leg alone -- :data:`SALVAGE_RETRY` when the
    data leg has already failed, so establishing whether an image exists costs one attempt rather
    than a second full retry budget on a view that has just proved it is slow.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    endpoint, query = _RENDER_ROUTES[kind]
    max_age = validate_max_age(options.max_age)
    sep = "&" if "?" in query else "?"
    query_str = f"{query}{sep}maxAge={max_age}"
    try:
        payload, elapsed, stats = session.export(
            f"/sites/{session.site_id}/views/{view_luid}/{endpoint}{query_str}",
            api=options.api,
            retry=options.retry,
            hard_deadline=options.hard_deadline,
        )
    except ExportFailed as exc:
        record = {
            "status": exc.kind,
            "error": str(exc),
            "detail": exc.detail,
            "max_age_minutes": max_age,
        }
        if kind == "svg" and SVG_VERSION_MARKER in exc.detail:
            # A version gate is a CONFIGURATION fault, not a broken view: retrying cannot fix it and
            # neither can a Tableau-side credential, so say which knob to turn rather than filing it
            # under the generic failure bucket a reader will chase into the data source.
            #
            # ⚠️ WHICH knob is not decidable here, and pretending otherwise is #474: the server's
            # advertised ceiling is a property of the SITE, not of this leg, and nothing in scope at
            # this call carries it. So this records the honest "ceiling not established" form; the
            # RUN knows better and `_stamp_svg_gate` upgrades every one of these before the manifest
            # is serialised. Both strings come from the same classifier, so they cannot disagree.
            record["status"] = SVG_UNSUPPORTED_STATUS
            advice = capability.svg_gate_advice(
                capability.SvgGate(configured=session.version), redactor=session.redact_text
            )
            record["cause"] = advice.cause
            record["remedy"] = advice.remedy
        return record
    # Dropped, not merged, and deliberately not consulted: for a RENDER the payload settles the
    # question by itself (`%PDF-`, the PNG signature, an `<svg>` root). The data leg is the opposite.
    stats.pop("content_type", None)
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
            "max_age_minutes": max_age,
            **stats,
        }
    # ⚠️ And a payload that STARTS like the requested format is not a payload that IS one. A leading
    # signature is 8 bytes of evidence about a file that may be 900 KB short: measured against a
    # loopback server declaring `Content-Length: 1024` and sending only the PNG signature before
    # closing, those 8 bytes reached here, passed `format_matches`, were written to disk, and were
    # recorded `status: ok` with a SHA-256 -- while `render_unestablished` stayed 0. That is the
    # decoupling this whole change exists for, INVERTED: a render leg that fails loudly is the point,
    # and a truncated one credited as evidence is worse than the suppression it replaced, because the
    # entry gate then reports the view as covered.
    #
    # `truncated` is deliberately NOT in `_VIEW_HEALTH_FAILURES`, for the same reason
    # `format_mismatch` is not: it arrived as a 200, so it says nothing about whether the VIEW can
    # render, and the remaining salvage tiers are still worth asking.
    complete, why_incomplete = payload_is_complete(kind, payload)
    if not complete:
        return {
            "status": "truncated",
            "requested_format": kind,
            "detail": why_incomplete,
            "bytes": len(payload),
            "elapsed_sec": round(elapsed, 2),
            "max_age_minutes": max_age,
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
        "max_age_minutes": max_age,
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


def _arg_max_age(val: str) -> int:
    try:
        parsed = int(val)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--max-age must be an integer >= 1, got {val!r}") from exc
    try:
        return validate_max_age(parsed)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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
            f"elements carry the dashboard's literal labels. Requires REST API >= "
            f"{SVG_MIN_API_VERSION} ON THE SERVER (Cloud June 2026 / Server 2026.2) as well as in "
            f"TABLEAU_REST_API_VERSION: an on-prem site below that floor cannot export SVG at any "
            f"client setting, and this run says so rather than naming an .env knob that cannot help"
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
        "--max-age",
        type=_arg_max_age,
        default=DEFAULT_MAX_AGE_MINUTES,
        metavar="MIN",
        help=(
            f"maximum cache age in minutes for Tableau server-side query cache (default {DEFAULT_MAX_AGE_MINUTES}, "
            f"minimum 1). Passed as maxAge=<MIN> on /data, /image, and /pdf requests to ensure the captured baseline "
            f"reflects fresh server computations rather than a stale cache."
        ),
    )
    parser.add_argument(
        "--rest-timeout",
        type=float,
        default=REST_TIMEOUT_SEC,
        metavar="SEC",
        help=(
            f"per-request socket timeout in seconds (default {REST_TIMEOUT_SEC}). Raise it for a view "
            f"whose export genuinely cannot finish in time -- the signature is 'HTTP 0' with "
            f"'TimeoutError: read operation timed out', repeatable across runs. WARNING: --retry-budget "
            f"tracks this by default (2x), and it MUST: the budget is charged from BEFORE attempt 1, "
            f"so a budget left at {DEFAULT_RETRY_BUDGET_SEC:.0f}s while this rises above "
            f"{DEFAULT_RETRY_BUDGET_SEC:.0f}s is already spent when the first timeout returns and "
            f"grants ZERO retries -- at any --max-attempts"
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"attempts per export before giving up (default {DEFAULT_MAX_ATTEMPTS})",
    )
    parser.add_argument(
        "--retry-budget",
        type=float,
        default=None,
        metavar="SEC",
        help=(
            f"seconds to admit retries for ONE export -- a deadline for admitting the NEXT retry, "
            f"charged from before attempt 1, NOT a hard wall-clock cap (default: 2x --rest-timeout, "
            f"so {DEFAULT_RETRY_BUDGET_SEC:.0f}s unless you raise the timeout). WARNING: Two consequences "
            f"operators are surprised by, both by design: at or below ONE request timeout a failure "
            f"that blocks for the full timeout cannot be retried at all, and even at 2x only ONE such "
            f"failure fits -- a second exhausts the budget, so the run gives up well short of "
            f"--max-attempts. Faster transient failures still retry until it is spent"
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


def _advertised_ceiling(session, env: dict[str, str], capability_report: dict[str, Any] | None, wants: set[str]):
    """The site's ADVERTISED REST ceiling -- the number that decides WHY a refused SVG was refused.

    ``--reference-best`` already has it: its probe report carries the same ``/serverinfo`` answer. A
    plain ``--svg`` run had NOTHING, so the only honest verdict available to it was "cause not
    established" (#474). This closes that, and the call is free rather than merely cheap:
    ``/serverinfo`` is unauthenticated and costs no metered export call. It also fails soft, so a site
    that will not answer leaves the run exactly where it was -- reporting the cause as unestablished
    rather than guessing at one.

    Not asked for at all when no SVG was requested: free is not weightless, and no other leg has a
    version floor a customer can miss.
    """
    server = (capability_report or {}).get("server")
    if server is not None or "svg" not in wants:
        return server
    return capability.server_info(env["TABLEAU_SERVER_URL"], redactor=session.redact_text)


def main() -> int:  # pylint: disable=too-many-locals
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
        build_retry_policy(args.max_attempts, args.retry_budget, args.rest_timeout),
        timeout_sec=args.rest_timeout,
    )
    session.sign_in()
    LOG.info("signed in to site %r (api %s)", env["TABLEAU_SITE"], session.version)

    views, workbook_names = select_views(session, args.workbook, args.limit)
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    LOG.info("capturing %d view(s) -> %s", len(views), out_dir)

    max_age = validate_max_age(args.max_age)
    capability_report = None
    wants = {kind for kind, on in (("png", args.images), ("svg", args.svg), ("pdf", args.pdf)) if on}
    api_overrides: dict[str, str] = {}
    if args.reference_best and views:
        capability_report = capability.probe_render_capability(session, env, views, max_age=max_age)
        capability.apply_selected_tier(capability_report, wants, api_overrides, env)

    records, started = [], time.perf_counter()
    # Resolved ONCE for the whole run - one Metadata API call for the site, not one per view - and
    # stamped onto each view so `capture_view` needs no extra argument. A failure is not fatal: every
    # record then reads `unknown`, and the reason is warned at the seam rather than carried as a
    # variable somebody has to remember to check (#402).
    tableau_view_types.resolve_and_stamp(session, views, LOG)
    for index, view in enumerate(views, 1):
        record = capture_view(session, view, out_dir, frozenset(wants), api_overrides, max_age=max_age)
        record["workbook_name"] = workbook_names.get(record["workbook_luid"])
        records.append(record)
        log_progress(index, len(views), record, session.redact_text)

    exit_code = write_manifest(
        records,
        CaptureRun(
            session,
            env,
            out_dir,
            started,
            frozenset(wants),
            bool(args.reference_best),
            max_age_minutes=max_age,
        ),
        capability_report,
        _advertised_ceiling(session, env, capability_report, wants),
    )
    session.sign_out()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
