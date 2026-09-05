"""
purpose: assess a Tableau estate before migrating any of it - what exists, what is actually used,
         how hard each workbook is, and who can see it - and emit a decision, not an inventory.
usage:   python scripts/assess_estate.py --out _assessment [--survey <estate_survey.json>]
                                         [--coverage-target 0.99] [--env .env]
                                         [--rest-timeout 180] [--graphql-timeout 300]
                                         [--max-attempts 3] [--retry-budget 300]
                                         [--deadline 7200] [--max-consecutive-transient-failures 3]

Why this exists
---------------
Every entry point in this toolchain begins AFTER the hardest question has been answered. The
deterministic engine opens at *"D2 - SCOPE: name the workbooks"*; our coordinator takes a folder.
At a customer with 400 workbooks, **which ones** is the engagement.

This is Phase 0. Its output is a **decision** - migrate / consolidate / archive / retire, and a
coverage curve the customer picks a point on - rather than a catalogue.

Four things it will not do, each for a measured reason
-----------------------------------------------------
1. **It does not derive dependencies from the Metadata API.** Measured 2026-08-06: the Metadata API
   reported ``upstreamDatasources`` for **0 of 13** workbooks where REST ``connections`` showed
   ``type: sqlproxy`` on **9**. An estate plan built on it concludes "migrate in any order" and
   produces empty reports. Pass ``--survey`` with the engine's ``estate_survey.py --json`` output,
   which reads REST ground truth. Without it, ordering is reported as **unknown**, never as "none".

2. **It exports IAM; it does not map it.** Every practitioner source agrees permissions can only be
   mapped once the Power BI workspace topology is decided, and that decision is human. A tool that
   maps permissions before topology produces confident nonsense, so this emits the raw grants and
   the specific *hard cases* (Deny, per-view grants, `ViewUnderlyingData` split from `Read`, local
   groups with no Entra counterpart) as decisions someone has to take.

3. **It does not retire anything on a metric.** Usage proposes; the owner disposes. A quarterly board
   pack has near-zero views and is business-critical, so low usage produces a *candidate*, never a
   verdict, and anything carrying a subscription, alert or custom view is held back from the
   retire tier regardless of view count.

4. **It does not claim a usage window it does not have.** Tableau **Cloud** REST returns
   ``usage.totalViewCount`` as a LIFETIME figure - there is no "last 90 days" without Admin Insights
   or (on Server) the Postgres repository. It is labelled ``lifetime`` everywhere.

Failure handling: a listing that cannot be read is DEGRADED, never silent
------------------------------------------------------------------------
``get()`` was always meant to be resilient - "one 403 on one workbook's permissions must not void an
estate-wide assessment" - but that resilience was **status-code shaped**, so a connection that timed
out or dropped never produced a status at all, bypassed the whole recovery ladder and killed the
process. Three consecutive customer runs died that way on three different SECONDARY endpoints, each
after minutes of real work, discarding a completed inventory (issue #193).

Every read now ends in one of three places, kept apart deliberately:

* **read** - rows, no error.
* **degraded** - the rows we did get, plus a ``listing_errors`` entry, the top-level ``degraded``
  flag in ``assessment.json`` and a ``[WARN]`` line in the report. A SECONDARY listing
  (subscriptions, alerts, custom views, group membership, flows) degrades alone, exit 0.
* **loud** - a PRIMARY listing (workbooks, views, datasources, projects, structure) is what every
  number downstream is computed from, so its incompleteness leads the report and exits **3**. A
  degraded assessment mistaken for a clean one is worse than the crash this replaced: a crash
  cannot be mistaken for success.

Retries are bounded and classified (the split ``capture_tableau_oracle.py`` uses): a timeout, reset
connection or gateway 5xx is transient and earns backoff; **an auth or permission refusal is a final
answer and is never retried** - no number of retries conjures a credential.

Two run-level bounds sit above the per-call retry budget. A circuit breaker opens after consecutive
transient endpoint failures, and a deadline stops starting new requests once the whole assessment has
used its budget. Both degrade the result instead of raising, so the inventory already collected is
still written and the partial assessment is explicitly marked incomplete.

Exit codes: ``0`` assessed (possibly degraded on secondary listings), ``1`` nothing could be
assessed, ``3`` a PRIMARY listing was incomplete - the assessment is not a complete inventory.
"""

# This module is over the 1200-line cap. The cap is a proxy for "this module does too much", and the
# honest answer is that it was already at 68% of it before #193 forced a real transport layer into a
# script that had one `try/except`. The real fix is extracting the Tableau REST client that
# `capture_tableau_oracle.py` already carries a near-duplicate of, into one shared module - a
# refactor that must not ride along with a hotfix for a blocked customer engagement. Trimming the
# explanatory docstrings to squeeze under would trade documented knowledge for a number, which is
# the wrong trade in this codebase. Suppressed deliberately, not accidentally.
# pylint: disable=too-many-lines

from __future__ import annotations

import argparse
import http.client
import json
import logging
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve_datasource_target as target_resolution  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_render_capability as render_capability  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_env import env_source, pat_secret, redact, require, resolve_env, scrub_tree  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("assess")

SESSION_LOST = "401002"
MAX_REAUTH = 2

# Timeouts, named and configurable (--rest-timeout / --graphql-timeout). They were two undocumented
# magic numbers, and 180 s is exactly how long each of the three failed customer runs took to die.
DEFAULT_REST_TIMEOUT_SEC = 180.0
DEFAULT_GRAPHQL_TIMEOUT_SEC = 300.0
GRAPHQL_ROOT = "/api/metadata/graphql"

# Recovery bounds. BOTH are needed: attempts alone let a slow-failing endpoint eat an estate run
# (3 attempts x a 180 s timeout is 9 minutes on ONE listing), and a wall-clock budget alone would
# allow an unbounded fast loop against a connection that is refused instantly.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BUDGET_SEC = 300.0
DEFAULT_RUN_DEADLINE_SEC = 7200.0
DEFAULT_MAX_CONSECUTIVE_TRANSIENT_FAILURES = 3
BACKOFF_BASE_SEC = 1.0
BACKOFF_CAP_SEC = 30.0

# Status 0 is our own marker for a failure that never produced an HTTP status at all - a timeout, a
# reset connection, DNS. It is the case the whole status-code ladder used to miss.
NETWORK_ERROR_STATUS = 0
TRANSIENT_STATUSES = frozenset({NETWORK_ERROR_STATUS, 429, 500, 502, 503, 504})
AUTH_STATUSES = frozenset({401, 403})

# How badly a missing listing hurts the assessment. PRIMARY feeds every number downstream - the
# coverage curve, the complexity score, the tier - so it is loud; SECONDARY degrades on its own.
PRIMARY = "primary"
SECONDARY = "secondary"

# Tableau calc idioms that drive migration effort. LOD and table calcs are weighted heaviest because
# they are what DAX translation actually struggles with - the same weighting an existing open-source
# complexity scorer arrived at independently.
LOD_RE = re.compile(r"\{\s*(fixed|include|exclude)\b", re.I)
TABLE_CALC_RE = re.compile(
    r"\b(window_\w+|running_\w+|index|rank|rank_dense|rank_modified|rank_unique|lookup|total|"
    r"first|last|size|previous_value|script_\w+)\s*\(",
    re.I,
)
WEIGHTS = {"sheets": 1.0, "dashboards": 2.0, "calcs": 1.0, "lods": 5.0, "table_calcs": 5.0}


@dataclass(frozen=True)
class HttpPolicy:
    """Timeouts and recovery bounds for one run. Every value is CLI-settable, none is a magic number."""

    rest_timeout: float = DEFAULT_REST_TIMEOUT_SEC
    graphql_timeout: float = DEFAULT_GRAPHQL_TIMEOUT_SEC
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_budget_sec: float = DEFAULT_RETRY_BUDGET_SEC
    run_deadline_sec: float = DEFAULT_RUN_DEADLINE_SEC
    max_consecutive_transient_failures: int = DEFAULT_MAX_CONSECUTIVE_TRANSIENT_FAILURES


@dataclass(frozen=True)
class Listing:
    """One paged REST listing, and how badly the assessment is hurt when it cannot be read."""

    label: str
    path: str
    collection: str
    item: str
    severity: str = SECONDARY


# Pass 1. The expensive inventory, and the only listings whose absence invalidates the numbers:
# `views` is PRIMARY because the coverage curve - the artifact the whole decision is taken on - is
# built entirely from view usage, so a partial view listing produces a confident wrong curve.
INVENTORY = (
    Listing("workbooks", "/workbooks", "workbooks", "workbook", PRIMARY),
    Listing("views", "/views?includeUsageStatistics=true", "views", "view", PRIMARY),
    Listing("datasources", "/datasources", "datasources", "datasource", PRIMARY),
    Listing("projects", "/projects", "projects", "project", PRIMARY),
    Listing("groups", "/groups", "groups", "group"),
    Listing("flows", "/flows", "flows", "flow"),
)

# Pass 1b. Deliberate-use signals: each one can only ADD a workbook to the migrate tier, never
# remove one, so a missing signal understates deliberate use rather than inventing it. Recorded and
# reported, not fatal - two of the three failed customer runs died here, on `customviews`.
SIGNALS = (
    Listing("subscriptions", "/subscriptions", "subscriptions", "subscription"),
    Listing("alerts", "/dataAlerts", "dataAlerts", "dataAlert"),
    Listing("custom_views", "/customviews", "customViews", "customView"),
)


def classify(status: int, text: str) -> str:
    """Map one REST outcome to what the caller should DO about it.

    Order matters. ``401002`` is our own session dying mid-run and is fixed by re-authenticating. A
    transient status (gateway 5xx, 429, or a network-level failure that never produced a status) is
    fixed by waiting. Anything else - 401, 403, 404 - is a **final answer**: retrying an auth or
    permission refusal burns the budget and still cannot succeed, and only a human can fix it
    (AGENTS.md: "a MISSING CREDENTIAL is not transient"). Transient is checked BEFORE the auth
    statuses so a gateway 503 is never misfiled as a permanent block.
    """
    if status == 200:
        return "ok"
    if SESSION_LOST in text:
        return "session_lost"
    if status in TRANSIENT_STATUSES:
        return "transient"
    if status in AUTH_STATUSES:
        return "denied"
    return "failed"


def backoff_delay(attempt: int, *, jitter: bool = True) -> float:
    """Exponential backoff with full jitter, capped.

    Jitter matters even for a sequential assessment: without it, a run that trips a rate limit
    retries in lockstep with every other client behind the same gateway.
    """
    delay = min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
    return delay * (0.5 + random.random() / 2) if jitter else delay


def _error_body(exc: urllib.error.HTTPError) -> bytes:
    """Read an error body without letting a SECOND failure escape the handler.

    The connection that just returned 500 is exactly the one likely to drop while its body is read.
    """
    try:
        return exc.read()
    except (OSError, http.client.HTTPException) as inner:  # pragma: no cover - defensive
        return f"{type(inner).__name__}: {inner}".encode()


def _took(started: float) -> str:
    """Elapsed for one endpoint, in ``harvest_estate_assets.py``'s ``elapsed=Ns`` idiom."""
    return f"elapsed={time.monotonic() - started:.1f}s"


class Site:  # pylint: disable=too-many-instance-attributes  # one client: credentials, policy, recovery telemetry
    """Read-only Tableau client. Re-authenticates on mid-run session loss and records that it did.

    It never raises for a failed read. Every failure - status code or transport - comes back as a
    recorded error so the caller can degrade one data point instead of losing the whole run.
    """

    def __init__(self, env: dict[str, str], policy: HttpPolicy | None = None) -> None:
        self.base = env["TABLEAU_SERVER_URL"].rstrip("/")
        self.version = env.get("TABLEAU_REST_API_VERSION", "3.21")
        self.site = env["TABLEAU_SITE"]
        self._pat = (env["TABLEAU_PAT_NAME"], pat_secret(env))
        self.policy = policy or HttpPolicy()
        self.token: str | None = None
        self.site_id: str | None = None
        self.reauths = 0
        self.retries = 0
        self.run_started = time.monotonic()
        self.consecutive_transient_failures = 0
        self.circuit_opened: str | None = None
        # Set once authentication is known to be impossible. Without it, a re-auth that cannot
        # succeed makes every remaining call pay the full retry budget - hundreds of endpoints x
        # minutes each - which is the unbounded stall the crash at least made obvious.
        self.auth_failed = False

    def _raw(self, method: str, path: str, body: dict | None = None, root: str | None = None):
        """One HTTP round trip. Never raises for a transport failure - returns status 0 instead, so
        a dropped connection and a gateway 503 travel the same recovery ladder."""
        url = f"{self.base}{root or f'/api/{self.version}'}{path}"
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method=method)
        request.add_header("Accept", "application/json")
        if body:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("X-Tableau-Auth", self.token)
        timeout = self.policy.graphql_timeout if root == GRAPHQL_ROOT else self.policy.rest_timeout
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, _error_body(exc)
        except (OSError, http.client.HTTPException) as exc:
            # URLError (an OSError subclass) covers DNS/refused/timeout; TimeoutError and
            # ConnectionResetError are OSErrors too; HTTPException covers RemoteDisconnected and
            # IncompleteRead, which urlopen does not always wrap. None of these carries a status.
            return NETWORK_ERROR_STATUS, f"{type(exc).__name__}: {exc}".encode()

    def sign_in(self) -> None:
        """Exchange the PAT for a session token, retrying TRANSIENT failures only.

        A gateway blip here would otherwise abort the whole assessment before it starts. A wrong or
        revoked PAT is a final answer and fails on the first attempt, without burning the budget.
        """
        credentials = {
            "credentials": {
                "personalAccessTokenName": self._pat[0],
                "personalAccessTokenSecret": self._pat[1],
                "site": {"contentUrl": self.site},
            }
        }
        for attempt in range(1, self.policy.max_attempts + 1):
            bound = self._run_bound_detail()
            if bound:
                raise RuntimeError(f"sign-in skipped: {bound}")
            status, payload = self._raw("POST", "/auth/signin", credentials)
            text = self._scrub(payload)
            kind = classify(status, text)
            if status == 200:
                creds = json.loads(payload)["credentials"]
                self.token, self.site_id = creds["token"], creds["site"]["id"]
                return
            if kind != "transient" or attempt == self.policy.max_attempts:
                if kind in {"denied", "failed"}:
                    self.auth_failed = True
                hint = (
                    "the server could not be reached"
                    if status == NETWORK_ERROR_STATUS
                    else "check the PAT NAME and SECRET - two values"
                )
                raise RuntimeError(f"sign-in failed: HTTP {status} after {attempt} attempt(s) ({hint}): {text[:200]}")
            self.retries += 1
            delay = backoff_delay(attempt)
            LOG.warning("  sign-in transient failure (HTTP %s); retry %d in %.1fs", status, attempt, delay)
            time.sleep(delay)

    def sign_out(self) -> None:
        """Best-effort release."""
        if self.token:
            self._raw("POST", "/auth/signout")
            self.token = None

    def _run_bound_detail(self) -> str | None:
        """Return the run-level stop reason, if no further HTTP request should be started."""
        if self.circuit_opened:
            return self.circuit_opened
        elapsed = time.monotonic() - self.run_started
        if elapsed >= self.policy.run_deadline_sec:
            return f"run deadline exceeded after {self.policy.run_deadline_sec:.0f}s"
        return None

    def _fail(self, record: dict[str, Any], *, counts_for_circuit: bool = False) -> dict[str, Any]:
        """Hand one final failure back to the caller and update the run-level circuit breaker."""
        if counts_for_circuit:
            self.consecutive_transient_failures += 1
            if self.consecutive_transient_failures >= self.policy.max_consecutive_transient_failures:
                self.circuit_opened = (
                    f"transient failure circuit opened after "
                    f"{self.consecutive_transient_failures} consecutive endpoint failure(s)"
                )
        else:
            self.consecutive_transient_failures = 0
        return record

    def scrub_text(self, text: str) -> str:
        """Remove every credential known at this point from a string we ASSEMBLED ourselves.

        ``_scrub`` handles a raw response body (bytes); this handles text built from already-parsed
        JSON - notably a GraphQL 200 whose ``errors`` array is a dict, not bytes, so it never
        travelled through ``_scrub`` on its way out of ``_request_json``. Both paths must scrub:
        both reach ``assessment.json``, ``report.md`` and now ``estate.db``, and a proxy or WAF that
        echoes the request writes the owner's PAT (or the live session token) into all three.
        """
        return redact(text, self._pat[0], self._pat[1], self.token or "")

    def _scrub(self, payload: bytes) -> str:
        """Decode a response body with every credential known at this point removed.

        Failure text is now PERSISTED - into ``assessment.json`` and ``report.md`` - so a proxy, WAF
        or debug endpoint that echoes the request body would write the owner's full-permission PAT
        into two durable artifacts. Scrub at the point of capture (the measured hazard behind
        ``tableau_env.redact``), not at the point of writing.
        """
        return self.scrub_text(payload.decode("utf-8", "replace"))

    def _may_retry(self, kind: str, attempt: int, started: float, delay: float) -> bool:
        """Retry only a TRANSIENT fault, and only inside BOTH bounds.

        Attempts alone cannot stop a slow-failing endpoint from eating the run (3 x a 180 s timeout
        is 9 minutes on one listing); a wall-clock budget alone would allow an unbounded fast loop
        against a connection refused instantly. The next delay is counted against the budget, so the
        run never sleeps its way past it.
        """
        return (
            kind == "transient"
            and attempt < self.policy.max_attempts
            and time.monotonic() - started + delay < self.policy.retry_budget_sec
            and time.monotonic() - self.run_started + delay < self.policy.run_deadline_sec
            and not self.circuit_opened
        )

    def _request_json(  # pylint: disable=too-many-return-statements,too-many-locals
        self, method: str, path: str, body: dict | None = None, root: str | None = None
    ):
        """The one recovery ladder -> ``(payload, error)``; exactly one of the two is ever set.

        Re-authenticate on session loss, back off on a transient fault within a bounded budget, and
        return a recorded error for anything a retry cannot change. It never raises: the caller's
        job is to degrade one data point, not to lose an estate-wide assessment.
        """
        started = time.monotonic()
        bound = self._run_bound_detail()
        if bound:
            return None, self._fail(
                _record(path, NETWORK_ERROR_STATUS, f"{bound}; not starting another request", 0, started)
            )
        if self.auth_failed:
            return None, self._fail(_record(path, NETWORK_ERROR_STATUS, "authentication failed; not retrying", 0, 0.0))
        reauths = attempt = 0
        while True:
            attempt += 1
            bound = self._run_bound_detail()
            if bound:
                return None, self._fail(
                    _record(path, NETWORK_ERROR_STATUS, f"{bound}; not starting another request", attempt - 1, started)
                )
            status, payload = self._raw(method, path, body, root)
            text = self._scrub(payload)
            kind = classify(status, text)
            if kind == "ok":
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError as exc:
                    detail = f"HTTP 200 but the body is not JSON ({exc}) - a proxy or portal answered"
                    return None, self._fail(_record(path, status, detail, attempt, started))
                if not isinstance(parsed, dict):
                    typename = type(parsed).__name__
                    detail = f"HTTP 200 but the JSON body is {typename}, expected object"
                    return None, self._fail(_record(path, status, detail, attempt, started))
                self.consecutive_transient_failures = 0
                scrubbed, _paths = scrub_tree(parsed, self.scrub_text)
                return scrubbed, None
            if kind == "session_lost" and reauths < MAX_REAUTH:
                reauths += 1
                self.reauths += 1
                try:
                    self.sign_in()
                    continue
                except RuntimeError as exc:
                    return None, self._fail(_record(path, status, f"re-authentication failed: {exc}", attempt, started))
            detail = f"{'transport' if status == NETWORK_ERROR_STATUS else f'HTTP {status}'}: {text[:200]}"
            delay = backoff_delay(attempt)
            if not self._may_retry(kind, attempt, started, delay):
                return None, self._fail(
                    _record(path, status, detail, attempt, started), counts_for_circuit=kind == "transient"
                )
            self.retries += 1
            LOG.warning("  %s -> %s; retry %d/%d in %.1fs", path, detail[:90], attempt, self.policy.max_attempts, delay)
            time.sleep(delay)

    def get(self, path: str) -> dict[str, Any] | None:
        """GET a metadata endpoint, recovering from mid-run session loss.

        Returns ``None`` on a permission or not-found failure rather than raising: one 403 on one
        workbook's permissions must not void an estate-wide assessment. Since #193 that promise also
        covers a failure that never produced a status at all - a timeout, a reset connection - which
        used to bypass this ladder entirely and take the process down.
        """
        return self._request_json("GET", path)[0]

    def get_checked(self, path: str) -> tuple[dict[str, Any] | None, dict | None]:
        """GET -> ``(payload, error)``, for callers that must tell a REFUSAL from a lost connection.

        ``get`` collapses both to ``None``, which is right where a 403 is expected and benign and
        wrong where the difference decides whether the output is degraded.
        """
        return self._request_json("GET", path)

    def paged(self, path: str, collection: str, item: str) -> tuple[list[dict], dict | None]:
        """Follow REST pagination to completion -> ``(rows, error)``.

        A survey that stops at page 1 under-reports. A page that FAILS is worse, and used to be
        fatal: the rows read so far are returned WITH the error, so the caller reports a partial
        listing loudly instead of passing a truncated list off as complete.
        """
        out: list[dict] = []
        page = 1
        while page <= 1000:
            sep = "&" if "?" in path else "?"
            payload, error = self._request_json("GET", f"{path}{sep}pageSize=1000&pageNumber={page}")
            if error:
                return out, {**error, "page": page}
            block = (payload or {}).get(collection) or {}
            rows = block.get(item) or []
            rows = [rows] if isinstance(rows, dict) else rows
            out.extend(rows)
            total = int(((payload or {}).get("pagination") or {}).get("totalAvailable", 0))
            if not rows or len(out) >= total:
                break
            page += 1
        return out, None

    def graphql(self, query: str) -> tuple[dict[str, Any], dict | None]:
        """One Metadata API call -> ``(payload, error)``. Used for STRUCTURE only (see module doc).

        It had no handler at all, which made it strictly worse than the REST path: even a plain
        ``HTTPError`` was fatal. It now travels the same ladder, with its own longer timeout.
        """
        payload, error = self._request_json("POST", "", {"query": query}, GRAPHQL_ROOT)
        return payload or {}, error


def _record(path: str, status: int, detail: str, attempts: int, started: float) -> dict[str, Any]:
    """One machine-readable failure. ``transport`` separates "no answer" from "an answer we dislike"."""
    return {
        "path": path,
        "status": status,
        "error": detail,
        "attempts": attempts,
        "elapsed_sec": round(time.monotonic() - started, 1) if started else 0.0,
        "transport": status == NETWORK_ERROR_STATUS,
    }


# `role` and `dataType` live on the CONCRETE types, not the Field interface - `fields { role }`
# fails with FieldUndefined. Inline fragments are required.
STRUCTURE_QUERY = """
{ workbooks {
    name projectName
    sheets { name }
    dashboards { name }
    embeddedDatasources {
      name hasUserReference
      fields { name __typename
        ... on ColumnField     { role dataType }
        ... on CalculatedField { role dataType formula } } } }
  publishedDatasources {
    name isCertified hasExtracts extractLastRefreshTime
    upstreamTables { fullName } } }
"""


def score_workbook(node: dict[str, Any]) -> dict[str, Any]:
    """Complexity from the workbook's own structure.

    ⚠️ **Understates any workbook backed by a published datasource**, whose calculated fields live in
    the datasource rather than the workbook - measured: a workbook literally named "Calc Gauntlet"
    scored 0 calcs. `--survey` supplies the dependency edges so those are flagged rather than
    silently trusted.
    """
    calcs = [
        f
        for ds in node.get("embeddedDatasources") or []
        for f in ds.get("fields") or []
        if f.get("__typename") == "CalculatedField" and f.get("formula")
    ]
    lods = [f for f in calcs if LOD_RE.search(f["formula"])]
    table_calcs = [f for f in calcs if TABLE_CALC_RE.search(f["formula"])]
    counts = {
        "sheets": len(node.get("sheets") or []),
        "dashboards": len(node.get("dashboards") or []),
        "calcs": len(calcs),
        "lods": len(lods),
        "table_calcs": len(table_calcs),
    }
    counts["has_user_reference"] = any(ds.get("hasUserReference") for ds in node.get("embeddedDatasources") or [])
    counts["complexity"] = round(sum(w * counts[k] for k, w in WEIGHTS.items()), 1)
    return counts


def liveness(views: list[dict], signals: dict[str, int]) -> dict[str, Any]:
    """Usage evidence for one workbook. ``views_lifetime`` is deliberately named: Cloud has no window."""
    return {
        "views_lifetime": sum(int((v.get("usage") or {}).get("totalViewCount") or 0) for v in views),
        "view_count": len(views),
        "subscriptions": signals.get("subscriptions", 0),
        "alerts": signals.get("alerts", 0),
        "custom_views": signals.get("custom_views", 0),
    }


def tier(live: dict[str, Any], complexity: float, cumulative_share: float, target: float) -> tuple[str, str]:
    """Assign a destination and say why. Never retires on a metric alone.

    Deliberate use - a subscription, an alert, a saved custom view - outranks a view count, because
    somebody chose to receive or personalise it. That is also the only available proxy for the
    seasonal report that has near-zero views and is business critical.
    """
    deliberate = live["subscriptions"] + live["alerts"] + live["custom_views"]
    if deliberate:
        return "migrate", f"deliberate use ({deliberate} subscription/alert/custom-view)"
    if cumulative_share <= target:
        return "migrate", f"inside the {target:.0%} usage cut"
    if live["views_lifetime"] > 0:
        return "archive", "used, but outside the coverage cut - static export retains it"
    if complexity >= 40:
        return "review", "no recorded use, but complex enough that a human should confirm"
    return "retire-candidate", "no recorded use, no deliberate use - CONFIRM WITH THE OWNER"


def coverage_curve(rows: list[dict]) -> list[dict]:
    """Order by usage and accumulate. This is the artifact the strategy decision is made on."""
    ordered = sorted(rows, key=lambda r: r["views_lifetime"], reverse=True)
    total = sum(r["views_lifetime"] for r in ordered) or 1
    running = 0
    out = []
    for index, row in enumerate(ordered, 1):
        running += row["views_lifetime"]
        out.append({**row, "rank": index, "cumulative_share": round(running / total, 6)})
    return out


def iam_hard_cases(permissions: list[dict], groups: list[dict]) -> list[dict]:
    """The permission facts that need a HUMAN decision, not a mapping.

    Each is something Power BI's model cannot express directly, so it becomes a decision in the
    topology design rather than a row in a translation table.
    """
    cases = []
    denies = [p for p in permissions if p["mode"].lower() == "deny"]
    if denies:
        cases.append(
            {
                "case": "explicit_deny",
                "count": len(denies),
                "why": "Power BI has no Deny. Each must be resolved to a grant or an absence, by hand.",
            }
        )
    view_grants = [p for p in permissions if p["object_type"] == "view"]
    if view_grants:
        cases.append(
            {
                "case": "per_view_grants",
                "count": len(view_grants),
                "why": "Power BI shares per REPORT, not per page. Different audiences per sheet forces a report split.",
            }
        )
    underlying = [p for p in permissions if p["capability"] in {"ViewUnderlyingData", "ExportData"}]
    if underlying:
        cases.append(
            {
                "case": "data_export_split_from_read",
                "count": len(underlying),
                "why": "Power BI's Build permission is all-or-nothing; 'see the chart, "
                "not the numbers' is not expressible.",
            }
        )
    local = [g for g in groups if (g.get("domain") or {}).get("name") == "local"]
    if local:
        cases.append(
            {
                "case": "local_groups_without_entra",
                "count": len(local),
                "why": "Local Tableau groups have no Entra counterpart. Creating them "
                "needs an identity owner - the long pole.",
                "names": [g["name"] for g in local][:20],
            }
        )
    return cases


SCHEMA = """
CREATE TABLE IF NOT EXISTS workbook (
  luid TEXT PRIMARY KEY, name TEXT, project TEXT, project_luid TEXT, owner_luid TEXT, size_mb INTEGER,
  created_at TEXT, updated_at TEXT,
  sheets INTEGER, dashboards INTEGER, calcs INTEGER, lods INTEGER, table_calcs INTEGER,
  has_user_reference INTEGER, complexity REAL, complexity_understated INTEGER,
  views_lifetime INTEGER, view_count INTEGER, subscriptions INTEGER, alerts INTEGER,
  custom_views INTEGER, rank INTEGER, cumulative_share REAL, tier TEXT, tier_reason TEXT);
CREATE TABLE IF NOT EXISTS view (
  luid TEXT PRIMARY KEY, workbook_luid TEXT, name TEXT, content_url TEXT,
  views_lifetime INTEGER, updated_at TEXT);
CREATE TABLE IF NOT EXISTS datasource (
  luid TEXT PRIMARY KEY, name TEXT, project TEXT, project_luid TEXT, is_certified INTEGER,
  has_extracts INTEGER, extract_last_refresh TEXT);
CREATE TABLE IF NOT EXISTS upstream_table (datasource_name TEXT, full_name TEXT);
CREATE TABLE IF NOT EXISTS dependency (
  workbook_name TEXT, workbook_luid TEXT, datasource_name TEXT, datasource_luid TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS project (
  luid TEXT PRIMARY KEY, name TEXT, parent_luid TEXT, content_permissions TEXT);
CREATE TABLE IF NOT EXISTS grp (luid TEXT PRIMARY KEY, name TEXT, domain TEXT, members INTEGER);
CREATE TABLE IF NOT EXISTS permission (
  object_type TEXT, object_luid TEXT, object_name TEXT,
  grantee_type TEXT, grantee_luid TEXT, capability TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS flow (luid TEXT PRIMARY KEY, name TEXT, project TEXT);
-- Run-level completeness, added in #196. Before it, a survived-but-partial run (#193) wrote a DB
-- INDISTINGUISHABLE from a clean run of a smaller estate: a consumer cannot tell "0 views because
-- the site has none" from "0 views because the listing died", and has no clean run of the same site
-- to diff against. (Not byte-identical - a primary failure also scores complexity 0 - but a
-- programmatic consumer (harvest_estate_assets.py --db, deploy_estate.py --estate-db) that never
-- opens assessment.json has no signal either way.)
CREATE TABLE IF NOT EXISTS assessment_run (
  assessed_at TEXT, degraded INTEGER, degraded_primary INTEGER,
  workbooks_total INTEGER, listing_errors INTEGER);
-- One row per listing that could not be read in full. ``error`` is SCRUBBED at the point of capture
-- (see Site.scrub_text), never here, so this table cannot carry a credential even if the server
-- echoed one back.
CREATE TABLE IF NOT EXISTS listing_error (
  listing TEXT, severity TEXT, status INTEGER, path TEXT, page INTEGER,
  attempts INTEGER, elapsed_sec REAL, transport INTEGER, error TEXT);
"""


def _collect_iam(site: Site, projects: list[dict], workbooks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Export grants -> ``(rows, errors)``. Per-item grants are only collected where owners diverge.

    This is the longest loop in the script - one call per project plus one per unlocked workbook -
    so it is also where a flaky connection is most likely to land. An unreadable object degrades the
    IAM export by one row set and is recorded; it never aborts the pass.
    """
    permissions: list[dict] = []
    errors: list[dict] = []
    locked = 0
    started = time.monotonic()
    for project in projects:
        rows, error = _grants(site, "project", project["id"], project.get("name"), f"/projects/{project['id']}")
        permissions.extend(rows)
        errors.extend(error)
        if (project.get("contentPermissions") or "").startswith("LockedToProject"):
            locked += 1
    unlocked = {p["id"] for p in projects if not (p.get("contentPermissions") or "").startswith("LockedToProject")}
    todo = [w for w in workbooks if (w.get("project") or {}).get("id") in unlocked]
    for index, workbook in enumerate(todo, 1):
        rows, error = _grants(site, "workbook", workbook["id"], workbook.get("name"), f"/workbooks/{workbook['id']}")
        permissions.extend(rows)
        errors.extend(error)
        if index % 25 == 0 or index == len(todo):
            LOG.info(
                "  [%d/%d] workbook grants, %d row(s) so far %s", index, len(todo), len(permissions), _took(started)
            )
    LOG.info(
        "  %d project(s) LockedToProject -> per-item grants skipped there; %d grant rows %s",
        locked,
        len(permissions),
        _took(started),
    )
    return permissions, errors


def _listing(site: Site, spec: Listing) -> tuple[list[dict], dict | None]:
    """Read one paged listing, timed, and classify any failure by the listing's SEVERITY.

    The endpoint is named BEFORE the call and the elapsed time after it, because during the run that
    is the only difference between "slow" and "dead" - the failing customer runs looked identical to
    a working one for 180 s, then produced a traceback.
    """
    path = f"/sites/{site.site_id}{spec.path}"
    LOG.info("  %-14s reading %s", spec.label, path)
    started = time.monotonic()
    rows, error = site.paged(path, spec.collection, spec.item)
    if error:
        LOG.warning(
            "  [WARN] %-14s INCOMPLETE - %d row(s) read, then: %s (%s)",
            spec.label,
            len(rows),
            error["error"][:120],
            _took(started),
        )
        return rows, {"listing": spec.label, "severity": spec.severity, **error}
    LOG.info("  %-14s %5d row(s) %s", spec.label, len(rows), _took(started))
    return rows, None


def _collect_listings(site: Site, specs: tuple[Listing, ...]) -> tuple[dict[str, list], list[dict]]:
    """Read a group of listings, keyed by label, collecting each failure INDIVIDUALLY."""
    out: dict[str, list] = {}
    errors: list[dict] = []
    for spec in specs:
        rows, error = _listing(site, spec)
        out[spec.label] = rows
        if error:
            errors.append(error)
    return out, errors


def _group_members(site: Site, groups: list[dict]) -> list[dict]:
    """Count each group's membership. One unreadable group degrades ONE group, not the pass.

    An unread group records ``_members = None`` (SQL NULL), never 0: "we could not see it" and "it
    is empty" are opposite answers, and 0 is the one that reads as a finding.
    """
    errors: list[dict] = []
    started = time.monotonic()
    for group in groups:
        rows, error = site.paged(f"/sites/{site.site_id}/groups/{group['id']}/users", "users", "user")
        group["_members"] = None if error else len(rows)
        if error:
            LOG.warning("  [WARN] group %r membership UNREADABLE: %s", group.get("name"), error["error"][:120])
            errors.append({"listing": f"group members: {group.get('name')}", "severity": SECONDARY, **error})
    LOG.info("  %-14s %5d group(s) %s", "group members", len(groups), _took(started))
    return errors


def _pass2_structure(site: Site) -> tuple[dict[str, Any], list[dict]]:
    """One GraphQL call for the whole estate -> ``(data, errors)``.

    A structure failure is PRIMARY: ``score_workbook`` answers 0 for a workbook it cannot see, and 0
    complexity is not "simple", it is "unknown" - it feeds the retire-candidate tier directly.
    """
    LOG.info("pass 2: structure (one GraphQL call for the whole estate)")
    started = time.monotonic()
    payload, error = site.graphql(STRUCTURE_QUERY)
    data = payload.get("data") or {}
    errors: list[dict] = []
    if error:
        LOG.warning("  [WARN] structure UNREADABLE: %s - every complexity score would be 0", error["error"][:150])
        errors.append({"listing": "structure", "severity": PRIMARY, **error})
    elif payload.get("errors"):
        # ``payload`` is parsed JSON handed straight back by ``_request_json`` on a 200, so unlike
        # the transport/HTTP paths this text never passed through the byte scrubber. Scrub the
        # echoed body HERE (#196): a proxy or WAF can reflect the request, and this string is
        # persisted into assessment.json, report.md and estate.db.
        detail = f"Metadata API errors: {site.scrub_text(str(payload['errors']))[:200]}"
        severity = SECONDARY if data.get("workbooks") else PRIMARY
        LOG.warning("  [WARN] %s (%s)", detail, severity)
        errors.append({"listing": "structure", "severity": severity, **_record(GRAPHQL_ROOT, 200, detail, 1, started)})
    LOG.info("  %-14s %5d workbook node(s) %s", "structure", len(data.get("workbooks") or []), _took(started))
    return data, errors


def collect(site: Site, survey: dict | None, checkpoint=None) -> dict[str, Any]:
    """Run the passes in cost order, cheapest first.

    ``checkpoint`` is called with the pass-1 inventory the moment it is complete, BEFORE the flakier
    per-item and secondary passes run: pass 1 is the expensive part (273 workbooks / 1042 views on
    the estate in #193) and losing it to a failure minutes later is what made the crash so costly.
    """
    LOG.info("pass 1: inventory")
    inventory, errors = _collect_listings(site, INVENTORY)
    LOG.info(
        "  %d workbooks, %d views, %d datasources, %d projects, %d groups, %d flows",
        *(len(inventory[spec.label]) for spec in INVENTORY),
    )
    if checkpoint:
        checkpoint(inventory)

    errors += _group_members(site, inventory["groups"])

    LOG.info("pass 1b: deliberate-use signals")
    signals, signal_errors = _collect_listings(site, SIGNALS)
    errors += signal_errors

    data, structure_errors = _pass2_structure(site)
    errors += structure_errors

    LOG.info("pass 3: IAM (gated on contentPermissions)")
    permissions, iam_errors = _collect_iam(site, inventory["projects"], inventory["workbooks"])
    errors += iam_errors

    return {
        **inventory,
        **signals,
        "structure": data,
        "structure_by_name": {w["name"]: w for w in data.get("workbooks") or []},
        "permissions": permissions,
        "survey": survey,
        # One unauthenticated call, and the only thing in this dict that describes the SERVER rather
        # than its content. Deliberately not part of any listing: it never degrades the assessment.
        "server_ceiling": server_ceiling(site),
        "collection_errors": errors,
    }


def _grants(site: Site, object_type: str, luid: str, name: str | None, path: str) -> tuple[list[dict], list[dict]]:
    """Flatten one object's granteeCapabilities into rows. An auth refusal yields nothing, never an abort.

    Returns ``(rows, errors)``: a 401/403 is a genuine answer ("you may not see this") and stays
    silent, while any other failure is NO useful answer and is recorded, so an IAM export thinned by
    a flaky connection or a bad permissions endpoint cannot be read as an estate with fewer grants.
    """
    payload, error = site.get_checked(f"/sites/{site.site_id}{path}/permissions")
    rows = []
    for grantee in ((payload or {}).get("permissions") or {}).get("granteeCapabilities") or []:
        kind = "group" if "group" in grantee else "user"
        gid = (grantee.get(kind) or {}).get("id")
        for capability in (grantee.get("capabilities") or {}).get("capability") or []:
            rows.append(
                {
                    "object_type": object_type,
                    "object_luid": luid,
                    "object_name": name,
                    "grantee_type": kind,
                    "grantee_luid": gid,
                    "capability": capability.get("name"),
                    "mode": capability.get("mode"),
                }
            )
    if error and error.get("status") not in AUTH_STATUSES:
        return rows, [{"listing": f"{object_type} grants: {name}", "severity": SECONDARY, **error}]
    return rows, []


def _aggregate_signals(raw: dict[str, Any]) -> tuple[dict[str, list], dict[str, dict[str, int]]]:
    """Group views by workbook, and roll every deliberate-use signal up to its workbook.

    A subscription/alert/custom view is attached to a VIEW, but the migration decision is taken per
    WORKBOOK, so a signal anywhere inside a workbook has to count for the whole workbook.
    """
    views_by_wb: dict[str, list] = {}
    for view in raw["views"]:
        views_by_wb.setdefault((view.get("workbook") or {}).get("id"), []).append(view)

    signals: dict[str, dict[str, int]] = {}
    for kind in ("subscriptions", "alerts", "custom_views"):
        for item in raw[kind]:
            luid = (item.get("content") or item.get("view") or {}).get("id")
            if luid:
                signals.setdefault(luid, {}).setdefault(kind, 0)
                signals[luid][kind] += 1

    wb_signals: dict[str, dict[str, int]] = {}
    view_owner = {v["id"]: (v.get("workbook") or {}).get("id") for v in raw["views"]}
    for luid, counts in signals.items():
        owner = view_owner.get(luid, luid)
        for key, value in counts.items():
            wb_signals.setdefault(owner, {}).setdefault(key, 0)
            wb_signals[owner][key] += value
    return views_by_wb, wb_signals


def _parse_dependencies(survey: dict | None) -> tuple[set[str], list[dict]]:
    """Read the REST-derived dependency graph. Raises rather than under-reporting."""
    required: set[str] = set()
    dep_rows: list[dict] = []
    if not survey:
        return required, dep_rows
    for wb in survey.get("workbooks") or []:
        if wb.get("complexity_understated"):
            required.add(wb.get("name"))
        for dep in wb.get("published_dependencies") or []:
            # His schema, verified against estate_survey.py output: `datasource_name`. Read it
            # explicitly rather than with a chain of fallbacks - an earlier version guessed at
            # `datasource`/`name`, parsed ZERO edges, and reported "order unknown", which is the
            # very failure this whole survey exists to prevent. A guess that yields nothing is
            # indistinguishable from a genuine absence, so it must not be possible to guess.
            name = dep.get("datasource_name") if isinstance(dep, dict) else None
            if name:
                dep_rows.append(
                    {
                        "workbook_name": wb.get("name"),
                        "workbook_luid": wb.get("luid"),
                        "datasource_name": name,
                        "datasource_luid": dep.get("luid"),
                        "source": f"sqlproxy/{dep.get('status', 'unknown')}",
                    }
                )
    declared = sum(len(wb.get("published_dependencies") or []) for wb in survey.get("workbooks") or [])
    if declared and not dep_rows:
        raise RuntimeError(
            f"the survey declares {declared} dependency entries but none parsed - its schema has "
            "changed. Refusing to report 'no dependencies', which would sequence the migration wrong."
        )
    return required, dep_rows


def assemble(raw: dict[str, Any], target: float) -> dict[str, Any]:
    """Turn collected facts into a scored, tiered backlog plus the coverage curve."""
    views_by_wb, wb_signals = _aggregate_signals(raw)
    survey = raw.get("survey")
    required, dep_rows = _parse_dependencies(survey)

    rows = []
    for wb in raw["workbooks"]:
        node = raw["structure_by_name"].get(wb["name"], {})
        scored = (
            score_workbook(node)
            if node
            else {k: 0 for k in ("sheets", "dashboards", "calcs", "lods", "table_calcs", "complexity")}
        )
        live = liveness(views_by_wb.get(wb["id"], []), wb_signals.get(wb["id"], {}))
        rows.append(
            {
                "luid": wb["id"],
                "name": wb["name"],
                "project": (wb.get("project") or {}).get("name"),
                "project_luid": (wb.get("project") or {}).get("id"),
                "owner_luid": (wb.get("owner") or {}).get("id"),
                "size_mb": wb.get("size"),
                "created_at": wb.get("createdAt"),
                "updated_at": wb.get("updatedAt"),
                **scored,
                **live,
                "complexity_understated": 1 if wb["name"] in required else 0,
            }
        )

    curve = coverage_curve(rows)
    for row in curve:
        row["tier"], row["tier_reason"] = tier(row, row["complexity"], row["cumulative_share"], target)
    return {
        "workbooks": curve,
        "dependencies": dep_rows,
        "survey_supplied": survey is not None,
        "iam_hard_cases": iam_hard_cases(raw["permissions"], raw["groups"]),
        # The site's render ceiling, carried through verbatim so a programmatic consumer reads the
        # same three numbers the report renders. `None` when the probe was not run at all.
        "server_ceiling": raw.get("server_ceiling"),
        # Computed ONCE, estate-wide (issue #368): a named-target request is guarded per-request by
        # `resolve_datasource_target.py`, but the hazard it would hit is surfaced here BEFORE anyone
        # picks a target, not discovered mid-migration.
        "datasource_hazards": target_resolution.datasource_class_hazards(raw),
        **_degraded_contract(raw.get("collection_errors") or [], len(curve)),
    }


def _degraded_contract(errors: list[dict], workbooks_total: int) -> dict[str, Any]:
    """The machine-readable incompleteness contract, ported from the engine's ``estate_survey.py``.

    One flag every consumer can trust, so "we could not read it" is never taken for "it is not
    there". ``degraded_primary`` is the half that must never be quiet - it drives exit code 3.
    """
    primary = any(e.get("severity") == PRIMARY for e in errors)
    return {
        "listing_errors": errors,
        "degraded": bool(errors),
        "degraded_primary": primary,
        "summary": {
            "workbooks_total": workbooks_total,
            "listing_errors": len(errors),
            "degraded": bool(errors),
            "degraded_primary": primary,
        },
    }


def _write_raw(out: Path, payload: dict[str, Any], redactor=None) -> None:
    """Write raw API responses as evidence. Called twice on purpose - see ``_checkpoint``."""
    (out / "raw").mkdir(parents=True, exist_ok=True)
    scrubbed, _paths = scrub_tree(payload, redactor or (lambda text: text))
    for key, value in scrubbed.items():
        (out / "raw" / f"{key}.json").write_text(json.dumps(value, indent=2), encoding="utf-8")


def _checkpoint(out: Path, inventory: dict[str, list], redactor=None) -> None:
    """Persist the pass-1 inventory the moment it exists, before anything flakier runs.

    Nothing used to be written until ``main`` completed, so a failure in a secondary pass discarded
    a finished inventory of 273 workbooks and 1042 views - three times in one afternoon (#193).
    """
    _write_raw(out, inventory, redactor)
    LOG.info("  checkpoint: pass-1 inventory persisted to %s", out / "raw")


def _clear_final_artifacts(out: Path) -> None:
    """Remove final verdict artifacts from an earlier run before writing fresh raw checkpoints."""
    for name in ("report.md", "assessment.json", "estate.db"):
        path = out / name
        if path.exists():
            path.unlink()


def _write_run_marker(conn: sqlite3.Connection, assembled: dict[str, Any]) -> None:
    """Stamp the run's completeness into the DB itself.

    A programmatic consumer that never opens ``assessment.json`` (``harvest_estate_assets.py --db``,
    ``deploy_estate.py --estate-db``) had no way to tell a degraded run from a clean one: since #193
    the run survives a failed listing and still writes a DB (#196). Additive - existing tables are
    untouched. ``error`` text is already scrubbed at capture (``Site.scrub_text``), so it is written
    verbatim and never carries a credential.
    """
    summary = assembled.get("summary") or {}
    errors = assembled.get("listing_errors") or []
    conn.execute(
        "INSERT INTO assessment_run VALUES (?,?,?,?,?)",
        (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            int(bool(assembled.get("degraded"))),
            int(bool(assembled.get("degraded_primary"))),
            summary.get("workbooks_total", len(assembled.get("workbooks") or [])),
            summary.get("listing_errors", len(errors)),
        ),
    )
    conn.executemany(
        "INSERT INTO listing_error VALUES "
        "(:listing,:severity,:status,:path,:page,:attempts,:elapsed_sec,:transport,:error)",
        [
            {
                "listing": e.get("listing"),
                "severity": e.get("severity"),
                "status": e.get("status"),
                "path": e.get("path"),
                "page": e.get("page"),
                "attempts": e.get("attempts"),
                "elapsed_sec": e.get("elapsed_sec"),
                "transport": int(bool(e.get("transport"))),
                "error": e.get("error"),
            }
            for e in errors
        ],
    )


def write_store(out: Path, raw: dict[str, Any], assembled: dict[str, Any], redactor=None) -> Path:
    """Raw JSON as evidence, SQLite as the query layer.

    Raw responses are kept because an assessment is evidence for a COMMERCIAL decision - "retire
    these 40" must be defensible months later, and an API response is not reproducible once the
    estate moves.
    """
    _write_raw(
        out,
        {
            key: raw[key]
            for key in (
                "workbooks",
                "views",
                "datasources",
                "projects",
                "groups",
                "flows",
                "subscriptions",
                "alerts",
                "custom_views",
                "permissions",
                "structure",
            )
        },
        redactor,
    )

    db_path = out / "estate.db"
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO workbook VALUES (:luid,:name,:project,:project_luid,:owner_luid,:size_mb,:created_at,:updated_at,"
        ":sheets,:dashboards,:calcs,:lods,:table_calcs,:has_user_reference,:complexity,"
        ":complexity_understated,:views_lifetime,:view_count,:subscriptions,:alerts,:custom_views,"
        ":rank,:cumulative_share,:tier,:tier_reason)",
        [{**r, "has_user_reference": int(bool(r.get("has_user_reference")))} for r in assembled["workbooks"]],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO view VALUES (?,?,?,?,?,?)",
        [
            (
                v["id"],
                (v.get("workbook") or {}).get("id"),
                v.get("name"),
                v.get("contentUrl"),
                int((v.get("usage") or {}).get("totalViewCount") or 0),
                v.get("updatedAt"),
            )
            for v in raw["views"]
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO datasource VALUES (?,?,?,?,?,?,?)",
        [
            (
                d["id"],
                d.get("name"),
                (d.get("project") or {}).get("name"),
                (d.get("project") or {}).get("id"),
                None,
                None,
                None,
            )
            for d in raw["datasources"]
        ],
    )
    for ds in raw["structure"].get("publishedDatasources") or []:
        conn.execute(
            "UPDATE datasource SET is_certified=?, has_extracts=?, extract_last_refresh=? WHERE name=?",
            (
                int(bool(ds.get("isCertified"))),
                int(bool(ds.get("hasExtracts"))),
                ds.get("extractLastRefreshTime"),
                ds.get("name"),
            ),
        )
        conn.executemany(
            "INSERT INTO upstream_table VALUES (?,?)",
            [(ds.get("name"), t.get("fullName")) for t in ds.get("upstreamTables") or []],
        )
    conn.executemany(
        "INSERT INTO dependency VALUES (:workbook_name,:workbook_luid,:datasource_name,:datasource_luid,:source)",
        assembled["dependencies"],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO project VALUES (?,?,?,?)",
        [(p["id"], p.get("name"), p.get("parentProjectId"), p.get("contentPermissions")) for p in raw["projects"]],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO grp VALUES (?,?,?,?)",
        # `_members` is NULL, not 0, for a group whose membership could not be read: 0 reads as a
        # finding ("this group is empty"), and that is the opposite of what we know.
        [(g["id"], g.get("name"), (g.get("domain") or {}).get("name"), g.get("_members")) for g in raw["groups"]],
    )
    conn.executemany(
        "INSERT INTO permission VALUES (:object_type,:object_luid,:object_name,:grantee_type,"
        ":grantee_luid,:capability,:mode)",
        raw["permissions"],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO flow VALUES (?,?,?)",
        [(f["id"], f.get("name"), (f.get("project") or {}).get("name")) for f in raw["flows"]],
    )
    _write_run_marker(conn, assembled)
    conn.commit()
    conn.close()
    return db_path


def server_ceiling(site: Site) -> dict[str, Any]:
    """What reference renders this SITE can actually give an operator, rung by rung.

    The render ceiling is a property of the site, so an operator should learn it HERE -- in the first
    thing they run against a new estate -- rather than discovering it much later as a capture-time
    warning. Measured customer case (#474): an on-prem Server at ``productVersion 2025.3.3`` advertises
    ``restApiVersion 3.27``, so SVG (floor 3.29) is unavailable there at any client setting, and the
    best rung it can reach is PDF. Nothing in the assessment said so.

    ⚠️ **The point is the IMPLICATION, not two more numbers in a blob.** Reporting "we send 3.21, the
    server advertises 3.27" leaves the operator to do the arithmetic against a floor table they do not
    have, which is how the false remedy in #474 survived in the first place. So this returns a
    **verdict per rung** -- and the raw numbers as the supporting detail underneath.

    Three numbers, and this repo insists they are different things:

    ``client_api_version``      what we SEND -- ``TABLEAU_REST_API_VERSION``, a client preference;
    ``advertised_api_version``  what ``/serverinfo`` SAYS -- the server's own ceiling;
    ``rungs``                   what that MEANS for a later ``capture_tableau_oracle.py`` run.

    ⚠️ **Fails soft, and reports the third state rather than guessing.** ``server_info`` is
    unauthenticated, never raises, is bounded by its own 30 s timeout, and a site that will not answer
    it is not a reason to degrade an assessment -- so a failure leaves ``established`` false and every
    rung ``unknown``. It is deliberately NOT a ``listing_error``: nothing downstream is computed from
    it, so calling it a degraded inventory would be a false alarm on the one flag this script asks
    consumers to trust.

    ⚠️ **"Answered" is not "established", and three failures land in the same state.** No answer at
    all, an unsuccessful answer (a proxy's 404, the server's own 500) whose body may still contain a
    ``<restApiVersion>`` element, and a 200 reporting something that is not a version -- all three are
    ``unknown`` here, because none of them is a number that can be compared against a rung's floor.
    Measured before the fix (#475 review): a 500 carrying ``3.30`` was reported as this site's ceiling
    with SVG available, and an advertised ``garbage-999`` did the same; ``not-a-version`` went the
    other way and reported that NO reference render was reachable at all. ``advertised_api_version_invalid``
    carries the offending text, redacted, so the report can say which failure this was.

    ⚠️ And every rung verdict is an EXPECTATION from an advertised number, never a measurement. Only
    ``capture_tableau_oracle.py --reference-best`` probes the endpoint, which is the sole
    authoritative answer -- see ``tableau_render_capability``'s module docstring for why the two
    disagree often enough to matter. The two verdicts are not equally strong, and the report says so:
    ``unavailable`` is the site's own ceiling falling below a documented floor, while ``available``
    is a claim the endpoint has not yet been asked to honour.
    """
    info = render_capability.server_info(site.base, redactor=site.scrub_text)
    shown = info.get("rest_api_version")
    reflected = bool(info.get("rest_api_version_reflected"))
    # ⚠️ **The verdicts come from the DERIVED capability, the display from the redacted text, and the
    # two are never the same value.** `rung_support` is computed inside `server_info` from the raw
    # `restApiVersion`, which does not leave that function -- so a server that reflects a
    # credential shaped like a version (`3.27`; nothing validates a Tableau token's shape) costs the
    # operator the printed NUMBER and nothing else. `established` stays true and every rung verdict
    # stays correct, because neither is computed from the string on its way to the report.
    support = info.get("rung_support")
    established = info.get("ceiling_established")
    # Defence in depth for a hand-assembled `info` -- tests, and any future caller: derive from the
    # display-safe value, which is exactly the value it is safe to derive AND display from. A
    # nonempty string is what used to be trusted; the grammar decides, bare truthiness does not.
    if support is None:
        support = render_capability.rung_support(render_capability.api_tuple(shown))
    if established is None:
        established = render_capability.api_tuple(shown) is not None
    # Junk that is neither a version nor a reflected credential is no ceiling at all. A reflected one
    # keeps its redaction marker, so a reader can tell "suppressed" from "never answered".
    if not reflected and render_capability.api_tuple(shown) is None:
        shown = None
    rungs = [_rung_verdict(rung, support.get(rung.name)) for rung in render_capability.LADDER]
    best = next((r for r in rungs if r["verdict"] == AVAILABLE), None)
    return {
        "client_api_version": site.version,
        "advertised_api_version": shown,
        # Suppressed with the number it names: `API_RELEASE` is a bijection, so printing the release
        # of a redacted version would hand back the digits the redaction removed.
        "advertised_release": None if reflected or not shown else render_capability.release_for(shown),
        "advertised_api_version_reflected": reflected,
        # ⚠️ Present ONLY when `/serverinfo` answered 200 with a `restApiVersion` that is not a REST
        # API version at all. It is why the ceiling is unknown, and it is already redacted at the
        # parse boundary. `established` stays false: text that cannot be compared against a floor is
        # not a ceiling, however confidently it was returned (#475 review).
        "advertised_api_version_invalid": info.get("invalid_rest_api_version"),
        "product_version": info.get("product_version"),
        "build": info.get("build"),
        "established": bool(established),
        "probe_status": info.get("status"),
        # One entry per ladder rung, best-first, each carrying its own floor AND its own verdict, so a
        # downstream tool consumes the implication without parsing prose or re-deriving it.
        "rungs": rungs,
        "best_reference_render": best["tier"] if best else None,
    }


# Per-rung verdicts. `UNKNOWN` is a first-class value rather than an absent key: a consumer that reads
# `verdict != UNAVAILABLE` as "usable" must be made to see the third state in the SAME field, because
# an unassessable state that reads as a clean one is the defect class this whole change exists for.
AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"


def _rung_verdict(rung, meets: bool | None) -> dict[str, Any]:
    """One ladder rung against this site's advertised ceiling. Three-valued, never two.

    ``meets`` is the DERIVED capability -- ``render_capability.rung_support``'s answer for this rung,
    computed from the raw advertised version inside ``server_info`` and never from the string that
    reaches the report. That separation is what lets a suppressed (credential-shaped) version still
    produce correct verdicts; see :func:`server_ceiling`.

    The parameter is ``rung``, not ``tier``: this module already has a module-level ``tier()`` that
    scores a workbook, and shadowing it here made pylint's ``redefined-outer-name`` the difference
    between exit 0 and exit 12 on the required gate.
    """
    return {
        "tier": rung.name,
        "route": rung.route,
        "min_api": rung.min_api,
        "min_release": rung.min_release,
        "vector": rung.vector,
        "ceiling": rung.ceiling,
        "verdict": UNKNOWN if meets is None else (AVAILABLE if meets else UNAVAILABLE),
    }


# The measurement behind the PNG rung's ceiling, quoted from `docs/reference-capture.md` (the route
# survey table and the paragraph under it) rather than restated: `?resolution=high` is exactly 2x the
# declared size on 52/52 dashboards, `standard`/`veryhigh` are HTTP 400, and `vizWidth`/`vizHeight` are
# ignored FOR DASHBOARDS. It is stated whenever the rung table is printed because it is the other half
# of "available" and it is the half operators over-trust.
#
# ⚠️ Scoped to dashboards on purpose. That same document records the correction: on a WORKSHEET
# `vizHeight` IS honoured (361x835 -> 361x1535 at `vizHeight=1500`), and it notes the earlier
# "silently ignored" phrasing "was over-general". Do not re-generalise it here.
_PNG_CEILING_NOTE = (
    "⚠️ **A raster rung has a hard ceiling, and it is set by the dashboard's author, not by the "
    "caller.** `?resolution=high` returns **exactly 2× the dashboard's declared size** (measured "
    "52/52; `resolution=standard`/`veryhigh` are HTTP 400 and `vizWidth`/`vizHeight` are ignored *for "
    "dashboards*), so a **650×800** dashboard tops out at **1300×1600, forever** — a label-dense page "
    "can be structurally legible and content-illegible at the same time. That is why a vector rung "
    "matters: it is the only one whose resolution the caller can still choose. (On a *worksheet* "
    "`vizHeight` **is** honoured, so the 2× ceiling is a dashboard claim.) Numbers measured in this "
    "repo's `docs/reference-capture.md`."
)


def _render_server_ceiling(ceiling: dict[str, Any] | None) -> list[str]:
    """The per-rung verdict block: the implication first, the raw numbers as supporting detail."""
    if not ceiling:
        return []
    out = ["## Reference renders — what this site can actually give you", ""]
    if not ceiling.get("established"):
        return out + _render_ceiling_unknown(ceiling)
    product = f", product `{ceiling['product_version']}`" if ceiling.get("product_version") else ""
    release = f" ({ceiling['advertised_release']}{product})" if ceiling.get("advertised_release") else product
    out += [
        f"- **what we send** — `TABLEAU_REST_API_VERSION` = `{ceiling['client_api_version']}` "
        f"(a *client preference*, not a capability)",
        f"- **what the server advertises** — REST `{ceiling['advertised_api_version']}`{release}, from `/serverinfo`",
    ]
    if ceiling.get("advertised_api_version_reflected"):
        # ⚠️ Say WHY the number is missing. A bare redaction marker where a version belongs reads as
        # a bug in this tool, and the operator needs to know their server echoed a credential.
        out.append(
            "- ⚠️ **the version this server reported matched a credential this run holds, so it has "
            "been redacted.** The rung verdicts below are still derived from it and are unaffected — "
            "what is lost is the printed number, not the assessment. A server echoing a credential "
            "back in an unauthenticated response is worth raising with whoever operates it."
        )
    out += [
        "",
        "| rung | route | needs | verdict | resolution ceiling |",
        "|---|---|---|---|---|",
    ]
    out += [_rung_row(rung) for rung in ceiling["rungs"]]
    out += ["", _bottom_line(ceiling), ""]
    # Only where the raster rung is actually reachable: on a site that cannot reach it either, the
    # ceiling of a rung nobody can call is noise on top of a worse answer.
    if any(r["tier"] == "png_high" and r["verdict"] == AVAILABLE for r in ceiling["rungs"]):
        out += [_PNG_CEILING_NOTE, ""]
    return out


def _rung_row(rung: dict[str, Any]) -> str:
    """One table row. The verdict is the column an operator reads; the floor explains it."""
    verdict = {
        AVAILABLE: "✅ **available**",
        UNAVAILABLE: "❌ **UNAVAILABLE on this server**",
        UNKNOWN: "⚠️ **unknown**",
    }[rung["verdict"]]
    kind = "vector" if rung["vector"] else "raster"
    return (
        f"| `{rung['tier']}` ({kind}) | `{rung['route']}` | REST `{rung['min_api']}` "
        f"({rung['min_release']}) | {verdict} | {rung['ceiling']} |"
    )


def _bottom_line(ceiling: dict[str, Any]) -> str:
    """What `--reference-best` should resolve to here, and what that verdict is worth."""
    best = ceiling["best_reference_render"]
    if best is None:
        return (
            f"**Bottom line: NO reference render rung is reachable.** This site advertises REST "
            f"`{ceiling['advertised_api_version']}`, below the lowest rung's floor, so "
            f"`capture_tableau_oracle.py` can obtain no server-side reference at all — the offline "
            f"192×192 `.twb` thumbnail is the only thing left, and a verdict signed off on it is "
            f"layout-grade, never validation-grade."
        )
    unavailable = [r for r in ceiling["rungs"] if r["verdict"] == UNAVAILABLE]
    lost = ""
    if unavailable:
        names = ", ".join(f"`{r['tier']}`" for r in unavailable)
        lost = (
            f" {names} is out of reach here **at any client setting** — its floor is above this "
            f"site's own ceiling, so raising `TABLEAU_REST_API_VERSION` cannot change it."
        )
    vector = next((r for r in ceiling["rungs"] if r["verdict"] == AVAILABLE and r["vector"]), None)
    why = (
        f" `{vector['tier']}` is the only **vector** rung available here, which is what makes it worth "
        f"preferring over the raster one below it."
        if vector and unavailable
        else ""
    )
    return (
        f"**Bottom line: `--reference-best` should resolve to `{best}` on this site.**{lost}{why} "
        f"⚠️ Every verdict above is derived from the **advertised** number: *unavailable* is firm (the "
        f"site's own ceiling is below that rung's floor), *available* is a claim the endpoint has not "
        f"been asked to honour. Only `capture_tableau_oracle.py --reference-best` settles it by asking."
    )


def _why_not_established(ceiling: dict[str, Any]) -> str:
    """The one clause that says WHICH way the probe failed to establish a ceiling.

    Three failures land here and they are not interchangeable to whoever has to act: no answer at all
    (``probe_status`` 0), an unsuccessful answer (404 from a proxy in front of Tableau, 500 from the
    server itself), and a 200 whose ``restApiVersion`` is not a version. Printing only "answered 200"
    for the third would read as a contradiction -- a successful probe with no ceiling -- and send the
    reader hunting for a bug in this tool instead of at the value the server actually returned.
    """
    invalid = ceiling.get("advertised_api_version_invalid")
    status = ceiling.get("probe_status")
    if invalid:
        return (
            f"`/serverinfo` answered `{status}`, but the version it reported (`{invalid}`) is not a "
            f"REST API version, so it cannot be compared against any rung's floor"
        )
    return f"`/serverinfo` answered `{status}`"


def _render_ceiling_unknown(ceiling: dict[str, Any]) -> list[str]:
    """State C. ⚠️ Emits NO per-rung verdicts -- an unestablished ceiling must not read as a known one.

    This is the highest-value refusal in the block: a rung table printed from a ceiling nobody
    established is indistinguishable, at a glance, from one that was measured, and it is exactly the
    unassessable-collapsing-into-a-confident-answer shape the rest of this repo is built to refuse.
    """
    return [
        f"⚠️ **The server's advertised REST ceiling was NOT established** — {_why_not_established(ceiling)}. "
        f"We send `TABLEAU_REST_API_VERSION` = "
        f"`{ceiling['client_api_version']}`, which is a client preference and says nothing about what "
        f"this site can do.",
        "",
        "**No per-rung verdict is shown, deliberately.** Which reference renders this site supports is "
        "**unknown**, not 'probably fine' — printing a rung table from a ceiling nobody established "
        "would be indistinguishable from a measured one. Establish it before promising a customer a "
        "rung: `capture_tableau_oracle.py --reference-best` probes the endpoint, which is the only "
        "authoritative answer, and `GET /api/3.4/serverinfo` returns the advertised ceiling on its own.",
        "",
    ]


def _render_curve(rows: list[dict], target: float) -> list[str]:
    """Header + the sparse-data caveat + the coverage curve."""
    total_views = sum(r["views_lifetime"] for r in rows)
    inside = [r for r in rows if r["cumulative_share"] <= target]
    out = ["# Estate assessment", ""]
    out.append(
        f"**{len(rows)} workbooks**, {sum(r['view_count'] for r in rows)} views, {total_views:,} lifetime view events."
    )
    out.append("")
    # A handful of view events across a whole estate cannot support a tiering decision.
    # Checking `== 0` missed this on a real run (1 event across 13 workbooks) and printed a
    # confident curve built on nothing, which is worse than printing no curve at all.
    if total_views < max(10, len(rows)):
        out.append(
            f"> WARNING: **usage data is too sparse to tier on** ({total_views:,} lifetime view "
            f"event(s) across {len(rows)} workbooks). Either the site is new, or usage statistics "
            "are unavailable. Every tier below is therefore **unproven** - scope by hand, and do "
            "not present this curve to a customer as evidence."
        )
        out.append("")
    out.append(f"## The coverage curve (target {target:.0%})")
    out.append("")
    out.append(f"**{len(inside)} of {len(rows)} workbooks carry {target:.0%} of all usage.**")
    out.append("")
    out.append("| rank | workbook | views (lifetime) | cumulative | complexity | tier |")
    out.append("|---|---|---:|---:|---:|---|")
    for row in rows[:15]:
        out.append(
            f"| {row['rank']} | {row['name']} | {row['views_lifetime']:,} | "
            f"{row['cumulative_share']:.1%} | {row['complexity']:.0f} | {row['tier']} |"
        )
    if len(rows) > 15:
        out.append(f"| … | _{len(rows) - 15} more_ | | | | |")
    out.append("")
    return out


def _render_tiers(rows: list[dict]) -> list[str]:
    """The destinations table and the rule that usage never retires anything on its own."""
    by_tier: dict[str, list] = {}
    for row in rows:
        by_tier.setdefault(row["tier"], []).append(row)
    out = ["## Destinations", "", "| tier | count | meaning |", "|---|---:|---|"]
    meaning = {
        "migrate": "rebuild, validate, sign off",
        "archive": "keep accessible, do not rebuild - static export",
        "review": "a human must decide",
        "retire-candidate": "**candidate only** - confirm with the owner before deleting",
    }
    for name in ("migrate", "archive", "review", "retire-candidate"):
        if name in by_tier:
            out.append(f"| {name} | {len(by_tier[name])} | {meaning[name]} |")
    out.append("")
    out.append(
        "> Usage **proposes**; the owner **disposes**. Nothing here is retired on a metric. A "
        "quarterly board pack has near-zero views and is business-critical, so anything "
        "carrying a subscription, alert or saved custom view is held out of the retire tier "
        "regardless of its view count."
    )
    out.append("")
    return out


def _render_sequencing(assembled: dict[str, Any], rows: list[dict]) -> list[str]:
    """What would make the backlog wrong: understated sizing, and unknown migration order."""
    out: list[str] = []
    understated = [r for r in rows if r["complexity_understated"]]
    if understated:
        out.append(
            f"⚠️ **{len(understated)} workbook(s) have an UNDERSTATED complexity score** - they are "
            "backed by a published datasource whose calculated fields are not counted here. "
            "Size them after the datasource is in scope."
        )
        out.append("")
    if assembled["dependencies"]:
        out.append(f"**{len(assembled['dependencies'])} hard dependency edge(s)** - those datasources migrate first.")
    elif assembled.get("survey_supplied"):
        out.append(
            "**No published-datasource dependencies** - the survey resolved zero edges, so every "
            "workbook is self-contained and may migrate in any order."
        )
    else:
        out.append(
            "⚠️ **Migration ORDER is unknown** - no `--survey` was supplied, so published-datasource "
            "dependencies were not resolved. Reported as unknown rather than as *none*: the Metadata "
            "API answers this question wrongly (measured 0 where REST showed 9), and a workbook whose "
            "datasource has not landed rebuilds to an EMPTY report."
        )
    out.append("")
    return out


def _render_iam(assembled: dict[str, Any], raw: dict[str, Any]) -> list[str]:
    """The grants that will not map themselves, and the refusal to map them here."""
    out = ["## IAM - decisions, not a mapping", ""]
    if assembled["iam_hard_cases"]:
        for case in assembled["iam_hard_cases"]:
            out.append(f"- **{case['case']}** ({case['count']}) — {case['why']}")
    else:
        out.append("- No hard cases detected in the exported grants.")
    out.append("")
    out.append(
        "> Permissions are **exported, not mapped**. Mapping requires the Power BI workspace "
        "topology, and that is a human decision — a tool that maps before topology is fixed "
        "produces confident nonsense. Design the topology against this export, then map."
    )
    out.append("")
    out.append(
        f"Flows (Tableau Prep ETL): **{len(raw['flows'])}** — each is its own dependency chain, "
        "landing before the extracts it produces."
    )
    return out


def _warn_lines(assembled: dict[str, Any]) -> list[str]:
    """The ``[WARN]``/``[ACTION]`` lines - ONE wording, rendered to two surfaces (log and report).

    Written the way the engine's ``estate_survey.py`` writes them: name the listing, name what is
    missing because of it, and end with the action. A reader who sees only one of the two surfaces
    must reach the same conclusion.
    """
    errors = assembled.get("listing_errors") or []
    if not errors:
        return []
    lines = []
    for entry in errors:
        page = f" page {entry['page']}" if entry.get("page") else ""
        lines.append(
            f"[WARN] {entry.get('severity', SECONDARY).upper()} listing INCOMPLETE: "
            f"{entry.get('listing')} at {entry.get('path')!r}{page} after "
            f"{entry.get('attempts')} attempt(s) / {entry.get('elapsed_sec')}s -- {entry.get('error')} "
            "-- rows are MISSING from this assessment"
        )
    if assembled.get("degraded_primary"):
        lines.append(
            "[ACTION] a PRIMARY listing failed, so this is NOT a complete inventory: the coverage "
            "curve, the complexity scores and every tier below are computed from data that is known "
            "to be partial. Do not present it as an estate assessment and do not scope from it -- "
            "re-run, and raise --rest-timeout / --max-attempts if the connection is slow rather "
            "than broken."
        )
    else:
        lines.append(
            "[ACTION] this assessment is DEGRADED -- a secondary listing could not be read, so a "
            "workbook showing no subscription, alert, custom view or group membership here is "
            "UNKNOWN, not unused. Deliberate use can only be UNDER-reported by this run, never "
            "invented, so the retire-candidate tier is the one to distrust."
        )
    return lines


def _render_degraded(assembled: dict[str, Any]) -> list[str]:
    """The incompleteness banner, rendered FIRST so it cannot be scrolled past.

    A degraded assessment silently mistaken for a clean one is worse than the crash this replaced:
    a crash cannot be mistaken for success. So a PRIMARY failure gets the document's first heading
    (and exit 3), while a secondary one gets a blockquote and exit 0.
    """
    lines = _warn_lines(assembled)
    if not lines:
        return []
    if assembled.get("degraded_primary"):
        out = ["# ⚠️ DEGRADED — a PRIMARY listing is INCOMPLETE, this is not the whole estate", ""]
    else:
        out = ["> ⚠️ **DEGRADED** — every primary listing was read in full, but not everything was.", ""]
    out += [f"- {line}" for line in lines]
    out.append("")
    return out


def _render_datasource_hazards(assembled: dict[str, Any]) -> list[str]:
    """Estate-wide datasource-name hazards (issue #368): surfaced once, before anyone picks a target.

    Neither list means "this name is broken" - it means a named-target request for it cannot be
    resolved by name alone, and `resolve_datasource_target.py` will (correctly) refuse it. Naming
    the hazard here is what lets an operator pick a target that is not a landmine in the first
    place, rather than discovering the refusal mid-migration.
    """
    hazards = assembled.get("datasource_hazards") or {}
    cross_class = hazards.get("cross_class") or []
    duplicates = hazards.get("duplicate_within_class") or []
    if not cross_class and not duplicates:
        return []
    out = ["## ⚠️ Datasource name hazards", ""]
    if cross_class:
        out.append(
            f"**{len(cross_class)} name(s) match BOTH published and embedded datasources** - a "
            "named-target request for one of these is AMBIGUOUS ACROSS CLASSES and will be refused:"
        )
        for row in cross_class:
            out.append(f"  - {row['name']!r} - classes: {', '.join(row['classes'])}")
        out.append("")
    if duplicates:
        out.append(
            f"**{len(duplicates)} name(s) are not unique within their own class** - a named-target "
            "request for one of these is AMBIGUOUS and will be refused:"
        )
        for row in duplicates:
            out.append(f"  - {row['name']!r} ({row['class']}) - {row['count']} candidates")
        out.append("")
    return out


def render_report(assembled: dict[str, Any], raw: dict[str, Any], target: float) -> str:
    """The customer-facing summary. Leads with the decision, not the inventory."""
    rows = assembled["workbooks"]
    out = _render_degraded(assembled)
    out += _render_curve(rows, target)
    out += _render_tiers(rows)
    out += _render_sequencing(assembled, rows)
    out += _render_datasource_hazards(assembled)
    out += _render_server_ceiling(assembled.get("server_ceiling"))
    out += _render_iam(assembled, raw)
    return "\n".join(out) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    """CLI. The timeout/retry group exists because two magic numbers cost three customer runs."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path, help="output directory (should be git-ignored)")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="git-ignored KEY=VALUE credentials")
    parser.add_argument("--survey", type=Path, help="estate_survey.py --json output: the REST-derived dependency graph")
    parser.add_argument(
        "--coverage-target",
        type=float,
        default=0.99,
        help="share of usage to keep in scope (1.0 = lift-and-shift). Default 0.99",
    )
    network = parser.add_argument_group("network resilience")
    network.add_argument(
        "--rest-timeout",
        type=float,
        default=DEFAULT_REST_TIMEOUT_SEC,
        help=f"seconds before one REST call is abandoned (default {DEFAULT_REST_TIMEOUT_SEC:.0f})",
    )
    network.add_argument(
        "--graphql-timeout",
        type=float,
        default=DEFAULT_GRAPHQL_TIMEOUT_SEC,
        help=f"seconds for the single Metadata API call (default {DEFAULT_GRAPHQL_TIMEOUT_SEC:.0f})",
    )
    network.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"attempts per call for TRANSIENT failures only (default {DEFAULT_MAX_ATTEMPTS}); "
        "an auth or permission refusal is never retried",
    )
    network.add_argument(
        "--retry-budget",
        type=float,
        default=DEFAULT_RETRY_BUDGET_SEC,
        help=f"wall-clock seconds one call may spend retrying (default {DEFAULT_RETRY_BUDGET_SEC:.0f}); "
        "attempts alone cannot stop a slow-failing endpoint from eating the run",
    )
    network.add_argument(
        "--deadline",
        type=float,
        default=DEFAULT_RUN_DEADLINE_SEC,
        help=f"wall-clock seconds for the whole assessment run (default {DEFAULT_RUN_DEADLINE_SEC:.0f}); "
        "when exceeded, partial results are written as degraded",
    )
    network.add_argument(
        "--max-consecutive-transient-failures",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_TRANSIENT_FAILURES,
        help=f"open the run-level circuit after this many transient endpoint failures (default "
        f"{DEFAULT_MAX_CONSECUTIVE_TRANSIENT_FAILURES})",
    )
    return parser


def _log_server_ceiling(ceiling: dict[str, Any] | None) -> None:
    """The same per-rung verdict on the console, so the terminal and the report agree.

    Rendered from the same dict the report and ``assessment.json`` read. An operator who sees only
    the terminal must reach the same conclusion as one who opens the artifact -- including the
    refusal: an unestablished ceiling prints NO rung verdicts here either.
    """
    if not ceiling:
        return
    if not ceiling.get("established"):
        invalid = ceiling.get("advertised_api_version_invalid")
        LOG.warning(
            "  render ceiling NOT ESTABLISHED (/serverinfo answered %s%s); we ask as %s. Which reference "
            "renders this site supports is UNKNOWN -- no rung verdict is shown. Probe it with "
            "capture_tableau_oracle.py --reference-best",
            ceiling.get("probe_status"),
            f", reporting {invalid!r}, which is not a REST API version" if invalid else "",
            ceiling["client_api_version"],
        )
        return
    LOG.info(
        "  server advertises REST %s (%s, product %s); we ask as %s",
        ceiling["advertised_api_version"],
        ceiling["advertised_release"] or "release not shown",
        ceiling.get("product_version"),
        ceiling["client_api_version"],
    )
    if ceiling.get("advertised_api_version_reflected"):
        LOG.warning(
            "  ⚠️ that version matched a credential this run holds and was REDACTED; the rung "
            "verdicts below are derived from it and are unaffected"
        )
    for rung in ceiling["rungs"]:
        line = LOG.warning if rung["verdict"] == UNAVAILABLE else LOG.info
        line(
            "    %-8s needs REST %-4s (%s) -> %s%s",
            rung["tier"],
            rung["min_api"],
            rung["min_release"],
            rung["verdict"].upper(),
            f" -- {rung['ceiling']}" if rung["verdict"] == AVAILABLE else " on this server AT ANY CLIENT SETTING",
        )
    best = ceiling["best_reference_render"]
    if best:
        LOG.info("  --reference-best should resolve to %s here (expected from the advertised ceiling)", best.upper())
    else:
        LOG.warning(
            "  NO reference render rung is reachable on this site's advertised ceiling (%s) -- the "
            "offline 192x192 .twb thumbnail is the only thing left, which is layout-grade, never "
            "validation-grade",
            ceiling["advertised_api_version"],
        )


def main() -> int:
    """Assess the estate. Exit 1 when nothing could be assessed, 3 when a PRIMARY listing failed."""
    args = _build_parser().parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = resolve_env(args.env)
    require(env)
    LOG.info("credentials: %s from the %s", "TABLEAU_PAT_SECRET", env_source("TABLEAU_PAT_SECRET", args.env))
    policy = HttpPolicy(
        args.rest_timeout,
        args.graphql_timeout,
        args.max_attempts,
        args.retry_budget,
        args.deadline,
        args.max_consecutive_transient_failures,
    )
    site = Site(env, policy)
    site.sign_in()
    LOG.info("signed in to %r (api %s)", site.site, site.version)

    survey = json.loads(args.survey.read_text(encoding="utf-8")) if args.survey else None
    if survey is None:
        LOG.warning("no --survey: migration ORDER will be reported as unknown, never as none")

    started = time.perf_counter()
    args.out.mkdir(parents=True, exist_ok=True)
    _clear_final_artifacts(args.out)
    raw = collect(site, survey, checkpoint=lambda inventory: _checkpoint(args.out, inventory, site.scrub_text))
    scrubbed_raw, _paths = scrub_tree(raw, site.scrub_text)
    assembled = assemble(scrubbed_raw, args.coverage_target)
    db = write_store(args.out, scrubbed_raw, assembled, site.scrub_text)
    site.sign_out()
    report = args.out / "report.md"
    report.write_text(render_report(assembled, scrubbed_raw, args.coverage_target), encoding="utf-8")
    (args.out / "assessment.json").write_text(json.dumps(assembled, indent=2) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for row in assembled["workbooks"]:
        counts[row["tier"]] = counts.get(row["tier"], 0) + 1
    LOG.info(
        "\n%d workbook(s) in %.0fs, %d re-auth(s), %d retry/retries",
        len(assembled["workbooks"]),
        time.perf_counter() - started,
        site.reauths,
        site.retries,
    )
    LOG.info("  tiers: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    _log_server_ceiling(assembled.get("server_ceiling"))
    LOG.info("  %s", db)
    LOG.info("  %s", report)
    for line in _warn_lines(assembled):
        LOG.warning("  %s", line)
    if assembled["degraded_primary"]:
        return 3
    return 0 if assembled["workbooks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
