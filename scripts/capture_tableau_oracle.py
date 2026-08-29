"""
purpose: capture Tableau's OWN computed values per view (the numeric oracle) plus a durable
         view-identity manifest keyed by view LUID, from a live Tableau Cloud/Server site.
usage:   python scripts/capture_tableau_oracle.py --out _oracle [--workbook "Superstore"] [--images]

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
from tableau_env import pat_secret, redact, require, resolve_env  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("tableau-oracle")

REST_TIMEOUT_SEC = 180
SESSION_LOST_CODE = "401002"
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
NETWORK_ERROR_STATUS = 0
TRANSIENT_STATUSES = frozenset({NETWORK_ERROR_STATUS, 429, 500, 502, 503, 504})

_PERCENT = re.compile(r"^-?[\d,.]+%$")
_CURRENCY = re.compile(r"^-?[$£€¥]\s?[\d,.]+$")
_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


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

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        accept: str | None = None,
        authed: bool = True,
    ) -> tuple[int, bytes, dict[str, str]]:
        """One HTTP round trip. Never raises for a network failure -- returns status 0 when no HTTP
        response arrived at all (reset/DNS/refused/timeout), so the retry loop can treat a reset
        connection and a gateway 503 the same way. When a response DID arrive but reading its body
        failed mid-stream, the real HTTP status is kept (a 503 is still usefully a 503) and the body
        read error is reported in the payload -- either way, this method does not raise."""
        req = urllib.request.Request(
            f"{self._creds.base.rstrip('/')}/api/{self._creds.version}{path}",
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


def capture_view(session: TableauSession, view: dict[str, Any], out_dir: Path, want_images: bool) -> dict[str, Any]:
    """Capture one view's data (and optionally its rendered image), keyed by view LUID."""
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

    if want_images:
        image_path = out_dir / "images" / f"{stem}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            png, elapsed, stats = session.export(f"/sites/{session.site_id}/views/{view_luid}/image?resolution=high")
        except ExportFailed as exc:
            record["image"] = {"status": exc.kind, "error": str(exc), "detail": exc.detail}
            return record
        image_path.write_bytes(png)
        record["image"] = {
            "status": "ok",
            "path": str(image_path.relative_to(out_dir)).replace("\\", "/"),
            "bytes": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            "elapsed_sec": round(elapsed, 2),
            **stats,
        }
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


def write_manifest(
    records: list[dict[str, Any]], session: TableauSession, env: dict[str, str], out_dir: Path, started: float
) -> int:
    """Write the manifest and return the process exit code.

    Codes: 0 all selected views captured, 1 partial non-credential failure, 2 credential-blocked,
    3 total non-credential failure, 4 no views selected.
    """
    ok = [r for r in records if r.get("data", {}).get("status") == "ok"]
    empty = [r for r in ok if r["data"]["row_count"] == 0]
    complete = [
        r
        for r in records
        if r.get("data", {}).get("status") == "ok" and r.get("image", {"status": "ok"}).get("status") == "ok"
    ]
    blocked = [
        r for r in records if "source_credential" in {r.get("data", {}).get("status"), r.get("image", {}).get("status")}
    ]
    failed = [
        r
        for r in records
        if any(
            status not in {"ok", "source_credential"}
            for status in (r.get("data", {}).get("status"), r.get("image", {"status": "ok"}).get("status"))
        )
    ]
    manifest = {
        "schema": "tableau-oracle/1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": env["TABLEAU_SERVER_URL"],
        "site": env["TABLEAU_SITE"],
        "rest_api_version": env.get("TABLEAU_REST_API_VERSION"),
        "view_count": len(records),
        "captured_complete": len(complete),
        "data_ok": len(ok),
        "data_empty": len(empty),
        "credential_blocked": len(blocked),
        "failed": len(failed),
        "total_reauths": session.reauth_count,
        "total_retries": session.retry_count,
        "elapsed_sec": round(time.perf_counter() - started, 1),
        "views": records,
    }
    manifest_path = out_dir / "oracle-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    LOG.info(
        "\n%d/%d captured (%d empty), %d credential-blocked, %d failed, %d re-auth(s), %d retr(ies), %.0fs -> %s",
        len(complete),
        len(records),
        len(empty),
        len(blocked),
        len(failed),
        session.reauth_count,
        session.retry_count,
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
            blocked_detail = record.get("data", {}).get("detail") or record.get("image", {}).get("detail")
            LOG.warning("  - %s (%s): %s", record["view_name"], record["workbook_name"], blocked_detail)
    if not records:
        return 4
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


def main() -> int:
    """Capture the oracle for every selected view.

    Exit codes: ``0`` all selected views captured, ``1`` partial non-credential failure,
    ``2`` some selected view needs a credential on the Tableau side (actionable only by a human --
    never by a retry), ``3`` total non-credential failure, ``4`` no views selected.
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

    records, started = [], time.perf_counter()
    for index, view in enumerate(views, 1):
        record = capture_view(session, view, out_dir, args.images)
        record["workbook_name"] = workbook_names.get(record["workbook_luid"])
        records.append(record)
        log_progress(index, len(views), record)

    exit_code = write_manifest(records, session, env, out_dir, started)
    session.sign_out()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
