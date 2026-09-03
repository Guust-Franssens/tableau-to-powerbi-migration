"""Transport-failure resilience for scripts/assess_estate.py (issue #193).

Split from ``test_assess_estate.py`` on subject, not duplicated: that file owns the four REFUSALS
(never retire on a metric, never guess a dependency, never claim a usage window, never map IAM).
This one owns the HTTP layer - what happens when the server answers late, badly, or not at all.

Everything here injects the failure at ``urllib.request.urlopen``; nothing touches a network, and
nothing reads the real ``.env``. The distinction under test throughout is the one the live defect
turned on: a status code is an ANSWER (a 403 means "you may not see this"), while a timeout is NO
answer, and only the first may be reported as a fact about the estate.
"""

import http.client
import importlib.util
import json
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("assess_estate", SCRIPTS / "assess_estate.py")
assess_estate = importlib.util.module_from_spec(spec)
sys.modules["assess_estate"] = assess_estate
spec.loader.exec_module(assess_estate)

ENV = {
    "TABLEAU_SERVER_URL": "https://tableau.invalid",
    "TABLEAU_SITE": "acme",
    "TABLEAU_PAT_NAME": "assessor",
    "TABLEAU_PAT_SECRET": "not-a-real-secret-0123456789",
}

# Every transport failure shape the live runs produced, plus the two that urlopen does not wrap.
TRANSPORT_FAILURES = [
    TimeoutError("timed out"),
    socket.timeout("timed out"),
    urllib.error.URLError("[Errno 11001] getaddrinfo failed"),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"),
]


class _Response:
    """The slice of ``http.client.HTTPResponse`` that ``Site._raw`` actually touches.

    ⚠️ Plus ``headers``, which ``Site._raw`` does NOT touch and ``tableau_http._request`` does. The
    assessment probes ``/serverinfo`` through that shared transport (#468), so a double missing the
    attribute fails the probe with ``AttributeError`` -- a fixture gap that would read as "the
    fail-soft path works", because a soft failure is exactly what a broken probe produces.
    """

    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self._payload = payload
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        """``urllib.error.HTTPError`` closes the body it was handed; without this it warns at GC."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


# What a real ``GET /api/<v>/serverinfo`` returns, trimmed to the three elements `server_info` parses.
# The numbers are the customer site behind #468: an on-prem Server whose ceiling is BELOW the SVG
# floor, so the assessment must resolve to "best rung is PDF" rather than to an .env remedy.
SES_SERVERINFO = (
    b'<?xml version="1.0" encoding="UTF-8"?><tsResponse><serverInfo>'
    b'<productVersion build="20253.25.0904.1234">2025.3.3</productVersion>'
    b"<restApiVersion>3.27</restApiVersion></serverInfo></tsResponse>"
)


class FakeTableau:
    """A scripted Tableau site. ``fail`` maps a URL fragment to an exception, a status, or a list
    of those consumed one per call, so a test injects a fault on ONE endpoint and nothing else."""

    SIGNIN = {"credentials": {"token": "session-token", "site": {"id": "site-1"}}}

    def __init__(self, fail: dict | None = None, *, serverinfo: bytes | None = SES_SERVERINFO) -> None:
        self.fail = {key: (value if isinstance(value, list) else [value]) for key, value in (fail or {}).items()}
        self.calls: list[tuple[str, float | None]] = []
        # ``None`` = this site does not answer ``/serverinfo`` at all, which is a real on-prem shape
        # (a reverse proxy in front of Tableau can refuse it) and the state-C input.
        self.serverinfo = serverinfo

    def urlopen(self, request, timeout=None):
        """Stand-in for ``urllib.request.urlopen``."""
        url = request.full_url
        self.calls.append((url, timeout))
        for fragment, queue in self.fail.items():
            if fragment in url and queue:
                outcome = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(outcome, BaseException):
                    raise outcome
                if outcome != 200:
                    raise urllib.error.HTTPError(url, outcome, "nope", {}, None)
        if "/serverinfo" in url:
            # XML, not JSON, and unauthenticated -- exactly as the real endpoint answers.
            if self.serverinfo is None:
                raise urllib.error.HTTPError(url, 404, "nope", {}, None)
            return _Response(200, self.serverinfo)
        return _Response(200, json.dumps(self._payload(url)).encode())

    def paths(self) -> list[str]:
        """Every URL called, in order."""
        return [url for url, _ in self.calls]

    def count(self, fragment: str) -> int:
        """How many calls touched ``fragment`` - the retry budget, measured."""
        return sum(1 for url, _ in self.calls if fragment in url)

    def _payload(self, url: str) -> dict:  # pylint: disable=too-many-return-statements
        if "/auth/signin" in url:
            return self.SIGNIN
        if "/auth/signout" in url:
            return {}
        if "metadata/graphql" in url:
            return {
                "data": {
                    "workbooks": [{"name": "Sales", "sheets": [{}], "dashboards": [], "embeddedDatasources": []}],
                    "publishedDatasources": [],
                }
            }
        if "/permissions" in url:
            return {"permissions": {"granteeCapabilities": []}}
        for fragment, collection, item, rows in self._collections():
            if fragment in url:
                return {"pagination": {"totalAvailable": len(rows)}, collection: {item: rows}}
        return {}

    @staticmethod
    def _collections():
        workbook = {"id": "wb-1", "name": "Sales", "project": {"id": "p-1", "name": "Finance"}}
        return (
            ("/groups/g-1/users", "users", "user", [{"id": "u-1", "name": "ana"}]),
            ("/workbooks?", "workbooks", "workbook", [workbook]),
            ("/views?", "views", "view", [{"id": "v-1", "workbook": {"id": "wb-1"}, "usage": {"totalViewCount": 9}}]),
            ("/datasources?", "datasources", "datasource", [{"id": "ds-1", "name": "Finance Master"}]),
            ("/projects?", "projects", "project", [{"id": "p-1", "name": "Finance", "contentPermissions": "x"}]),
            ("/groups?", "groups", "group", [{"id": "g-1", "name": "Analysts", "domain": {"name": "local"}}]),
            ("/flows?", "flows", "flow", []),
            ("/subscriptions?", "subscriptions", "subscription", []),
            ("/dataAlerts?", "dataAlerts", "dataAlert", []),
            ("/customviews?", "customViews", "customView", []),
        )


@pytest.fixture(name="no_sleep")
def _no_sleep(monkeypatch):
    """Record backoff delays instead of serving them, so a retry test costs no wall-clock."""
    slept: list[float] = []
    monkeypatch.setattr(assess_estate.time, "sleep", slept.append)
    return slept


def _site(server: FakeTableau, monkeypatch, **policy):
    monkeypatch.setattr(assess_estate.urllib.request, "urlopen", server.urlopen)
    site = assess_estate.Site(ENV, assess_estate.HttpPolicy(**policy) if policy else None)
    site.sign_in()
    return site


# --- 1. a transport failure is caught where the status codes already were -----------------------


@pytest.mark.parametrize("failure", TRANSPORT_FAILURES, ids=lambda exc: type(exc).__name__)
def test_transport_failure_returns_a_sentinel_instead_of_propagating(failure, monkeypatch, no_sleep):
    """The whole defect in one assertion: `_raw` caught only HTTPError, so these five killed the run."""
    server = FakeTableau({"/workbooks?": failure})
    site = _site(server, monkeypatch)
    status, payload = site._raw("GET", "/sites/site-1/workbooks?pageSize=1000")  # pylint: disable=protected-access
    assert status == assess_estate.NETWORK_ERROR_STATUS
    assert type(failure).__name__ in payload.decode()
    assert no_sleep == []


def test_get_returns_None_on_a_timeout_rather_than_raising(monkeypatch, no_sleep):
    """``get``'s documented promise - one failure must not void the assessment - now covers
    failures that never produced a status at all."""
    site = _site(FakeTableau({"/permissions": TimeoutError("timed out")}), monkeypatch)
    assert site.get("/sites/site-1/workbooks/wb-1/permissions") is None
    assert no_sleep  # it retried, because a timeout is transient


def test_graphql_survives_an_http_error_it_used_to_die_on(monkeypatch, no_sleep):
    """``graphql`` had no handler at all, so even a plain HTTPError was fatal there."""
    site = _site(FakeTableau({"metadata/graphql": 500}), monkeypatch)
    payload, error = site.graphql("{ workbooks { name } }")
    assert payload == {}
    assert error["status"] == 500 and no_sleep


# --- 2. the retry budget is bounded, and an auth refusal is never retried ------------------------


def test_a_transient_failure_is_retried_up_to_the_bound(monkeypatch, no_sleep):
    server = FakeTableau({"/customviews?": TimeoutError("timed out")})
    site = _site(server, monkeypatch, max_attempts=3)
    rows, error = site.paged("/sites/site-1/customviews", "customViews", "customView")
    assert (rows, error["attempts"]) == ([], 3)
    assert server.count("/customviews?") == 3
    assert len(no_sleep) == 2  # two backoffs between three attempts


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_refusal_is_a_FINAL_answer_and_is_never_retried(status, monkeypatch, no_sleep):
    """AGENTS.md: a missing credential is not transient. Retrying burns the budget and cannot win."""
    server = FakeTableau({"/customviews?": status})
    site = _site(server, monkeypatch, max_attempts=5)
    _, error = site.paged("/sites/site-1/customviews", "customViews", "customView")
    assert server.count("/customviews?") == 1
    assert error["attempts"] == 1 and no_sleep == []


def test_a_not_found_endpoint_is_not_retried_either(monkeypatch, no_sleep):
    server = FakeTableau({"/customviews?": 404})
    site = _site(server, monkeypatch, max_attempts=5)
    site.paged("/sites/site-1/customviews", "customViews", "customView")
    assert server.count("/customviews?") == 1 and no_sleep == []


def test_a_gateway_503_IS_retried_and_then_succeeds(monkeypatch, no_sleep):
    """The counterpart: a 5xx is the server being busy, not the server refusing."""
    server = FakeTableau({"/customviews?": [503, 200]})
    site = _site(server, monkeypatch, max_attempts=3)
    rows, error = site.paged("/sites/site-1/customviews", "customViews", "customView")
    assert error is None and rows == [] and len(no_sleep) == 1


def test_a_bad_PAT_fails_sign_in_on_the_first_attempt(monkeypatch, no_sleep):
    server = FakeTableau({"/auth/signin": 401})
    monkeypatch.setattr(assess_estate.urllib.request, "urlopen", server.urlopen)
    site = assess_estate.Site(ENV, assess_estate.HttpPolicy(max_attempts=4))
    with pytest.raises(RuntimeError, match="sign-in failed"):
        site.sign_in()
    assert server.count("/auth/signin") == 1 and no_sleep == []
    assert site.auth_failed


def test_sign_in_retries_a_transient_failure(monkeypatch, no_sleep):
    """A gateway blip at sign-in would otherwise abort the assessment before it started."""
    server = FakeTableau({"/auth/signin": [TimeoutError("timed out"), 200]})
    monkeypatch.setattr(assess_estate.urllib.request, "urlopen", server.urlopen)
    site = assess_estate.Site(ENV, assess_estate.HttpPolicy(max_attempts=3))
    site.sign_in()
    assert site.token == "session-token" and len(no_sleep) == 1


def test_the_wall_clock_budget_stops_a_slow_failing_endpoint():
    """Attempts alone cannot bound 3 x a 180 s timeout; the budget is the second bound."""
    site = assess_estate.Site(ENV, assess_estate.HttpPolicy(max_attempts=9, retry_budget_sec=0.0))
    assert site._may_retry("transient", 1, assess_estate.time.monotonic(), 0.1) is False  # pylint: disable=protected-access


def test_backoff_grows_and_is_capped():
    delays = [assess_estate.backoff_delay(n, jitter=False) for n in (1, 2, 3, 99)]
    assert delays[0] < delays[1] < delays[2]
    assert delays[3] == assess_estate.BACKOFF_CAP_SEC


def test_a_failed_re_auth_stops_the_run_from_paying_the_budget_on_every_call(monkeypatch, no_sleep):
    """A session that cannot be re-established would otherwise cost every remaining endpoint its
    full retry budget - the unbounded stall the crash at least made obvious."""

    class ExpiredThenDenied(FakeTableau):
        """One expired session on a data call, then a real PAT denial on re-auth."""

        def urlopen(self, request, timeout=None):
            if "/customviews?" in request.full_url:
                self.calls.append((request.full_url, timeout))
                raise urllib.error.HTTPError(request.full_url, 401, "expired", {}, _Response(401, b"401002"))
            return super().urlopen(request, timeout)

    server = ExpiredThenDenied({"/auth/signin": [200, 401]})
    site = _site(server, monkeypatch, max_attempts=3)
    site.paged("/sites/site-1/customviews", "customViews", "customView")
    assert site.auth_failed
    before = len(server.calls)
    site.paged("/sites/site-1/subscriptions", "subscriptions", "subscription")
    assert len(server.calls) == before  # not one further request was made
    assert no_sleep == []


def test_a_transient_re_auth_failure_does_not_latch_auth_failed(monkeypatch, no_sleep):
    """A mid-run transport blip is not a credential failure, and later primary calls must still run."""
    server = FakeTableau({"/customviews?": 401, "/auth/signin": [200, TimeoutError("timed out")]})
    site = _site(server, monkeypatch, max_attempts=1)
    monkeypatch.setattr(assess_estate, "SESSION_LOST", "")  # every 401 body now reads as session loss
    _, error = site.paged("/sites/site-1/customviews", "customViews", "customView")
    assert "re-authentication failed" in error["error"]
    assert "authentication failed; not retrying" not in error["error"]
    assert not site.auth_failed
    before = len(server.calls)
    rows, followup_error = site.paged("/sites/site-1/subscriptions", "subscriptions", "subscription")
    assert rows == [] and followup_error is None
    assert len(server.calls) > before
    assert no_sleep == []


# --- 3. partial listings are returned WITH their error, never as a complete answer ---------------


def test_paged_returns_the_rows_it_got_plus_the_error_naming_the_page(monkeypatch, no_sleep):
    """A truncated list passed off as complete is the failure mode that outranks the crash."""

    class Paging(FakeTableau):
        """Two pages of two rows; the second page times out."""

        def _payload(self, url):
            if "/views?" not in url:
                return super()._payload(url)
            if "pageNumber=1" in url:
                return {"pagination": {"totalAvailable": 4}, "views": {"view": [{"id": "v-1"}, {"id": "v-2"}]}}
            raise TimeoutError("timed out")

    site = _site(Paging(), monkeypatch, max_attempts=1)
    rows, error = site.paged("/sites/site-1/views", "views", "view")
    assert len(rows) == 2
    assert error["page"] == 2 and error["transport"] is True
    assert no_sleep == []


def test_a_403_is_an_ANSWER_and_does_not_degrade_the_iam_export(monkeypatch, no_sleep):
    """ "You may not see this" is a fact about the estate; "no answer" is not. Only the second
    degrades, or every locked-down project would raise a false alarm."""
    site = _site(FakeTableau({"/permissions": 403}), monkeypatch)
    rows, errors = assess_estate._grants(site, "workbook", "wb-1", "Sales", "/workbooks/wb-1")
    assert (rows, errors) == ([], [])
    assert no_sleep == []


def test_an_unreadable_grant_IS_degraded(monkeypatch, no_sleep):
    site = _site(FakeTableau({"/permissions": TimeoutError("timed out")}), monkeypatch, max_attempts=1)
    _, errors = assess_estate._grants(site, "workbook", "wb-1", "Sales", "/workbooks/wb-1")
    assert errors[0]["severity"] == assess_estate.SECONDARY
    assert "Sales" in errors[0]["listing"] and no_sleep == []


@pytest.mark.parametrize("status", [404, 429, 500])
def test_a_permissions_endpoint_failure_degrades_the_iam_export(status, monkeypatch, no_sleep):
    """Only auth refusals are a useful permissions answer; other statuses mean the grant rows are unknown."""
    site = _site(FakeTableau({"/permissions": status}), monkeypatch, max_attempts=1)
    _, errors = assess_estate._grants(site, "project", "p-1", "Finance", "/projects/p-1")
    assert errors and errors[0]["status"] == status
    assert errors[0]["severity"] == assess_estate.SECONDARY
    assert no_sleep == []


def test_an_unreadable_group_records_NULL_members_not_zero(monkeypatch, no_sleep, tmp_path):
    """0 reads as a finding ("this group is empty"); NULL is what we actually know."""
    site = _site(FakeTableau({"/groups/g-1/users": TimeoutError("timed out")}), monkeypatch, max_attempts=1)
    groups = [{"id": "g-1", "name": "Analysts"}]
    errors = assess_estate._group_members(site, groups)
    assert groups[0]["_members"] is None
    assert errors and errors[0]["severity"] == assess_estate.SECONDARY and no_sleep == []
    raw = _raw_fixture(groups=groups)
    store = assess_estate.write_store(tmp_path, raw, assess_estate.assemble(raw, 0.99))
    assert assess_estate.sqlite3.connect(store).execute("SELECT members FROM grp").fetchone() == (None,)


# --- 4. the degraded contract: secondary degrades quietly-but-recorded, primary is loud ----------


def _raw_fixture(errors=None, groups=None):
    return {
        "workbooks": [{"id": "wb-1", "name": "Sales", "project": {"id": "p-1"}}],
        "views": [],
        "datasources": [],
        "projects": [],
        "groups": groups if groups is not None else [],
        "flows": [],
        "subscriptions": [],
        "alerts": [],
        "custom_views": [],
        "structure": {"publishedDatasources": []},
        "structure_by_name": {},
        "permissions": [],
        "survey": None,
        "collection_errors": errors or [],
    }


def _error(listing="custom_views", severity=assess_estate.SECONDARY):
    return {
        "listing": listing,
        "severity": severity,
        "path": f"/sites/site-1/{listing}",
        "page": 1,
        "status": 0,
        "error": "transport: TimeoutError: timed out",
        "attempts": 3,
        "elapsed_sec": 12.0,
        "transport": True,
    }


def test_a_clean_run_is_not_degraded_and_carries_no_banner():
    assembled = assess_estate.assemble(_raw_fixture(), 0.99)
    assert (assembled["degraded"], assembled["degraded_primary"]) == (False, False)
    assert assembled["summary"]["listing_errors"] == 0
    assert assess_estate._render_degraded(assembled) == []
    assert "DEGRADED" not in assess_estate.render_report(assembled, _raw_fixture(), 0.99)


def test_a_secondary_failure_degrades_and_names_the_listing():
    assembled = assess_estate.assemble(_raw_fixture([_error()]), 0.99)
    assert (assembled["degraded"], assembled["degraded_primary"]) == (True, False)
    assert assembled["summary"]["degraded"] is True
    report = assess_estate.render_report(assembled, _raw_fixture(), 0.99)
    assert "[WARN]" in report and "custom_views" in report
    assert "[ACTION] this assessment is DEGRADED" in report
    assert "PRIMARY listing" not in report


def test_a_primary_failure_leads_the_report_and_says_what_is_missing():
    assembled = assess_estate.assemble(_raw_fixture([_error("workbooks", assess_estate.PRIMARY)]), 0.99)
    assert assembled["degraded_primary"] is True
    report = assess_estate.render_report(assembled, _raw_fixture(), 0.99)
    assert report.startswith("# ⚠️ DEGRADED")
    assert "PRIMARY listing is INCOMPLETE" in report.splitlines()[0]
    assert "workbooks" in report and "[ACTION] a PRIMARY listing failed" in report


def test_the_warn_wording_is_shared_by_the_log_and_the_report():
    """One wording, two surfaces: a reader who sees only one must reach the same conclusion."""
    assembled = assess_estate.assemble(_raw_fixture([_error()]), 0.99)
    report = assess_estate.render_report(assembled, _raw_fixture(), 0.99)
    for line in assess_estate._warn_lines(assembled):
        assert line in report


def test_every_secondary_listing_degrades_INDIVIDUALLY(monkeypatch, no_sleep):
    """One dead endpoint must cost one data point, not the pass it sits in."""
    server = FakeTableau({"/customviews?": TimeoutError("timed out")})
    site = _site(server, monkeypatch, max_attempts=1)
    raw = assess_estate.collect(site, None)
    assert [e["listing"] for e in raw["collection_errors"]] == ["custom_views"]
    assert raw["subscriptions"] == [] and raw["alerts"] == []
    assert len(raw["workbooks"]) == 1 and no_sleep == []


def test_consecutive_transient_failures_open_a_run_circuit_and_still_write_partial_results(
    monkeypatch, no_sleep, tmp_path
):
    """A dead site is bounded across the run, not merely one call at a time."""
    server = FakeTableau({"/groups/g-1/users": 500, "/subscriptions?": 500})
    monkeypatch.setattr(assess_estate.urllib.request, "urlopen", server.urlopen)
    monkeypatch.setattr(assess_estate, "resolve_env", lambda *_a, **_k: dict(ENV))
    monkeypatch.setattr(assess_estate, "env_source", lambda *_a, **_k: "test")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "assess_estate.py",
            "--out",
            str(tmp_path / "_assessment"),
            "--max-attempts",
            "1",
            "--max-consecutive-transient-failures",
            "2",
        ],
    )
    assert assess_estate.main() == 3
    out = tmp_path / "_assessment"
    assessment = json.loads((out / "assessment.json").read_text(encoding="utf-8"))
    assert (out / "estate.db").exists() and (out / "raw" / "workbooks.json").exists()
    assert assessment["degraded"] is True
    assert not any("/customviews?" in path for path in server.paths())
    assert any("transient failure circuit opened" in error["error"] for error in assessment["listing_errors"])
    assert no_sleep == []


def test_the_run_deadline_bounds_the_whole_assessment_with_a_virtual_clock(monkeypatch, tmp_path):
    """Successes reset the circuit, so only the run-level deadline can stop many slow successful calls."""

    class Clock:
        """A clock advanced by urlopen and sleep, never by real time."""

        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    class SlowSuccess(FakeTableau):
        """Every HTTP request succeeds but consumes virtual time."""

        def __init__(self, clock: Clock) -> None:
            super().__init__()
            self.clock = clock

        def urlopen(self, request, timeout=None):
            self.clock.now += 5.0
            return super().urlopen(request, timeout)

    clock = Clock()
    server = SlowSuccess(clock)
    monkeypatch.setattr(assess_estate.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(assess_estate.time, "sleep", clock.sleep)
    monkeypatch.setattr(assess_estate.urllib.request, "urlopen", server.urlopen)
    monkeypatch.setattr(assess_estate, "resolve_env", lambda *_a, **_k: dict(ENV))
    monkeypatch.setattr(assess_estate, "env_source", lambda *_a, **_k: "test")
    monkeypatch.setattr(
        sys,
        "argv",
        ["assess_estate.py", "--out", str(tmp_path / "_assessment"), "--max-attempts", "1", "--deadline", "12"],
    )
    assert assess_estate.main() == 3
    out = tmp_path / "_assessment"
    assessment = json.loads((out / "assessment.json").read_text(encoding="utf-8"))
    assert (out / "estate.db").exists() and (out / "raw" / "workbooks.json").exists()
    assert any("run deadline exceeded" in error["error"] for error in assessment["listing_errors"])
    assert len(server.calls) < 6


def test_an_unreadable_structure_call_is_PRIMARY(monkeypatch, no_sleep):
    """A workbook we could not see scores 0 complexity, and 0 is not "simple" - it is "unknown",
    and it feeds the retire-candidate tier directly."""
    site = _site(FakeTableau({"metadata/graphql": TimeoutError("timed out")}), monkeypatch, max_attempts=1)
    raw = assess_estate.collect(site, None)
    structure_errors = [e for e in raw["collection_errors"] if e["listing"] == "structure"]
    assert structure_errors[0]["severity"] == assess_estate.PRIMARY and no_sleep == []


# --- 5. the expensive pass-1 inventory is persisted before anything flakier runs -----------------


def test_pass1_is_checkpointed_before_the_secondary_passes(monkeypatch, no_sleep, tmp_path):
    server = FakeTableau()
    site = _site(server, monkeypatch)
    seen: list[int] = []
    assess_estate.collect(site, None, checkpoint=lambda inv: seen.append(len(server.calls)))
    at_checkpoint = seen[0]
    later = [p for p in server.paths()[at_checkpoint:]]
    assert any("/customviews?" in p for p in later), "the checkpoint must precede the secondary passes"
    assert not any("/customviews?" in p for p in server.paths()[:at_checkpoint])
    assess_estate._checkpoint(tmp_path, {"workbooks": [{"id": "wb-1"}]})
    assert json.loads((tmp_path / "raw" / "workbooks.json").read_text(encoding="utf-8")) == [{"id": "wb-1"}]
    assert no_sleep == []


# --- 6. timeouts are named, configurable, and actually reach the socket --------------------------


def test_the_configured_timeouts_reach_urlopen(monkeypatch, no_sleep):
    server = FakeTableau()
    site = _site(server, monkeypatch, rest_timeout=7.0, graphql_timeout=11.0)
    site.get("/sites/site-1/workbooks?pageSize=1000")
    site.graphql("{ workbooks { name } }")
    rest = [t for url, t in server.calls if "workbooks?" in url]
    gql = [t for url, t in server.calls if "metadata/graphql" in url]
    assert rest == [7.0] and gql == [11.0] and no_sleep == []


def test_the_http_client_does_not_keep_a_write_only_error_ledger(monkeypatch):
    """Failures are surfaced through collection_errors, not duplicated into an unread Site.errors list."""
    server = FakeTableau()
    monkeypatch.setattr(assess_estate.urllib.request, "urlopen", server.urlopen)
    site = assess_estate.Site(ENV)
    assert not hasattr(site, "errors")


def test_the_defaults_are_the_documented_ones():
    policy = assess_estate.HttpPolicy()
    assert (policy.rest_timeout, policy.graphql_timeout) == (180.0, 300.0)
    assert policy.max_attempts == 3
    assert policy.run_deadline_sec == 7200.0
    assert policy.max_consecutive_transient_failures == 3


@pytest.mark.parametrize("body", [b"null", b"[]", b"0"])
def test_a_200_with_a_non_object_json_body_returns_a_recorded_error(body, monkeypatch, no_sleep):
    """The recovery ladder must never return ``(None, None)`` or a non-object payload on HTTP 200."""

    class NonObjectBody(FakeTableau):
        """Return a syntactically valid but unusable JSON value for one endpoint."""

        def urlopen(self, request, timeout=None):
            if "/customviews?" in request.full_url:
                self.calls.append((request.full_url, timeout))
                return _Response(200, body)
            return super().urlopen(request, timeout)

    site = _site(NonObjectBody(), monkeypatch, max_attempts=1)
    payload, error = site.get_checked("/sites/site-1/customviews?pageSize=1000")
    assert payload is None
    assert error and "expected object" in error["error"]
    assert no_sleep == []


# --- 7. end to end: the reproduction from the issue ----------------------------------------------


def _run_main(monkeypatch, tmp_path, server):
    monkeypatch.setattr(assess_estate.urllib.request, "urlopen", server.urlopen)
    monkeypatch.setattr(assess_estate, "resolve_env", lambda *_a, **_k: dict(ENV))
    monkeypatch.setattr(assess_estate, "env_source", lambda *_a, **_k: "test")
    monkeypatch.setattr(
        sys, "argv", ["assess_estate.py", "--out", str(tmp_path / "_assessment"), "--max-attempts", "1"]
    )
    return assess_estate.main()


def test_a_timeout_on_a_secondary_listing_yields_a_degraded_but_COMPLETE_assessment(
    monkeypatch, no_sleep, tmp_path, caplog
):
    """The exact field failure: three runs died on `customviews` and `groups/{id}/users`."""
    server = FakeTableau({"/customviews?": TimeoutError("timed out")})
    with caplog.at_level("WARNING"):
        code = _run_main(monkeypatch, tmp_path, server)
    out = tmp_path / "_assessment"
    assessment = json.loads((out / "assessment.json").read_text(encoding="utf-8"))
    assert code == 0
    assert (assessment["degraded"], assessment["degraded_primary"]) == (True, False)
    assert [e["listing"] for e in assessment["listing_errors"]] == ["custom_views"]
    assert len(assessment["workbooks"]) == 1  # the inventory survived
    assert (out / "estate.db").exists() and (out / "raw" / "workbooks.json").exists()
    assert "[WARN]" in caplog.text and "custom_views" in caplog.text
    assert no_sleep == []


def test_a_timeout_on_a_PRIMARY_listing_exits_3_and_names_what_is_missing(monkeypatch, no_sleep, tmp_path):
    server = FakeTableau({"/views?": TimeoutError("timed out")})
    code = _run_main(monkeypatch, tmp_path, server)
    out = tmp_path / "_assessment"
    report = (out / "report.md").read_text(encoding="utf-8")
    assert code == 3
    assert report.startswith("# ⚠️ DEGRADED")
    assert "views" in report.splitlines()[2]
    assert json.loads((out / "assessment.json").read_text(encoding="utf-8"))["degraded_primary"] is True
    assert no_sleep == []


def test_a_clean_run_still_exits_0_with_no_warning(monkeypatch, no_sleep, tmp_path):
    code = _run_main(monkeypatch, tmp_path, FakeTableau())
    report = (tmp_path / "_assessment" / "report.md").read_text(encoding="utf-8")
    assert code == 0
    assert report.startswith("# Estate assessment")
    assert "DEGRADED" not in report and no_sleep == []


# --- #468: the site's render ceiling is a property of the SITE, so the ASSESSMENT reports it -----


def test_the_render_ceiling_reaches_both_report_md_and_assessment_json(monkeypatch, no_sleep, tmp_path):
    """An operator learns "this site tops out at PDF" HERE, not as a capture-time warning later.

    Driven through the real ``main()`` so a mutation that stops probing in ``collect``, or drops the
    section from ``render_report``, fails -- both survived a unit-only version of this test.
    """
    code = _run_main(monkeypatch, tmp_path, FakeTableau())
    out = tmp_path / "_assessment"
    assessment = json.loads((out / "assessment.json").read_text(encoding="utf-8"))
    report = (out / "report.md").read_text(encoding="utf-8")
    ceiling = assessment["server_ceiling"]
    assert code == 0
    # The three numbers this repo insists are different things.
    assert (ceiling["client_api_version"], ceiling["advertised_api_version"]) == ("3.21", "3.27")
    assert ceiling["expected_reference_render"] == "pdf"
    assert "Best rung expected: PDF" in report
    assert "at any client setting" in report
    assert no_sleep == []


def test_a_site_that_refuses_serverinfo_reports_UNKNOWN_and_still_assesses_cleanly(monkeypatch, no_sleep, tmp_path):
    """Fail soft: an unanswered probe is the third state, never a degraded inventory.

    Also the negative control for the test above -- a run that cannot establish the ceiling must not
    name a rung, and must not turn a clean assessment into a degraded one.
    """
    code = _run_main(monkeypatch, tmp_path, FakeTableau(serverinfo=None))
    out = tmp_path / "_assessment"
    assessment = json.loads((out / "assessment.json").read_text(encoding="utf-8"))
    report = (out / "report.md").read_text(encoding="utf-8")
    assert code == 0
    assert assessment["degraded"] is False and assessment["listing_errors"] == []
    assert assessment["server_ceiling"]["established"] is False
    assert "was NOT established" in report
    assert "Best rung expected" not in report
    assert no_sleep == []


def test_a_mid_run_abort_removes_previous_final_artifacts(monkeypatch, no_sleep, tmp_path):
    """After an abort, an operator must not read a stale report beside fresh raw checkpoints."""
    out = tmp_path / "_assessment"
    out.mkdir()
    for name in ("report.md", "assessment.json", "estate.db"):
        (out / name).write_text("old verdict", encoding="utf-8")

    server = FakeTableau()
    monkeypatch.setattr(assess_estate.urllib.request, "urlopen", server.urlopen)
    monkeypatch.setattr(assess_estate, "resolve_env", lambda *_a, **_k: dict(ENV))
    monkeypatch.setattr(assess_estate, "env_source", lambda *_a, **_k: "test")

    def aborting_collect(_site, _survey, checkpoint=None):
        if checkpoint:
            checkpoint(
                {
                    "workbooks": [{"id": "wb-1"}],
                    "views": [],
                    "datasources": [],
                    "projects": [],
                    "groups": [],
                    "flows": [],
                }
            )
        raise RuntimeError("mid-run abort")

    monkeypatch.setattr(assess_estate, "collect", aborting_collect)
    monkeypatch.setattr(sys, "argv", ["assess_estate.py", "--out", str(out), "--max-attempts", "1"])
    with pytest.raises(RuntimeError, match="mid-run abort"):
        assess_estate.main()
    assert (out / "raw" / "workbooks.json").exists()
    assert not any((out / name).exists() for name in ("report.md", "assessment.json", "estate.db"))
    assert no_sleep == []


def test_the_pat_secret_never_reaches_a_persisted_artifact(monkeypatch, no_sleep, tmp_path):
    """A proxy that echoes the request body would otherwise write the owner's PAT into
    assessment.json and report.md, which are durable (the hazard behind tableau_env.redact)."""

    class Echoing(FakeTableau):
        """A gateway that reflects what it was sent - secret included - in a 500 body."""

        def urlopen(self, request, timeout=None):
            if "/customviews?" in request.full_url:
                body = json.dumps({"error": f"upstream rejected {ENV['TABLEAU_PAT_SECRET']}"}).encode()
                raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, _Response(500, body))
            return super().urlopen(request, timeout)

    code = _run_main(monkeypatch, tmp_path, Echoing())
    written = (tmp_path / "_assessment" / "assessment.json").read_text(encoding="utf-8")
    assert code == 0
    assert ENV["TABLEAU_PAT_SECRET"] not in written
    assert "[REDACTED]" in written
    assert (tmp_path / "_assessment" / "report.md").read_text(encoding="utf-8").count(ENV["TABLEAU_PAT_SECRET"]) == 0
    assert no_sleep == []


# --- 8. #196: scrub the 200-with-`errors` GraphQL body, and mark degradation in estate.db ---------


class _GraphqlLeak(FakeTableau):
    """A Metadata API that answers 200 but reflects the caller's credential in its ``errors`` array.

    The sibling of the ``Echoing`` gateway above, on the ONE path that returns PARSED JSON straight
    from ``_request_json`` and so never met the byte scrubber (issue #196). ``data.workbooks`` stays
    non-empty (inherited from the parent), so the structure failure is SECONDARY and the run exits 0.
    """

    def _payload(self, url: str) -> dict:
        payload = super()._payload(url)
        if "metadata/graphql" in url:
            payload["errors"] = [{"message": f"upstream rejected credential {ENV['TABLEAU_PAT_SECRET']}"}]
        return payload


def test_a_graphql_200_error_body_is_scrubbed_before_it_is_recorded(monkeypatch, no_sleep):
    """A 200 with an `errors` array is recorded via ``_record`` WITHOUT travelling through
    ``_scrub`` - so its text must be scrubbed at the call site, or the PAT lands in the artifacts."""
    site = _site(_GraphqlLeak(), monkeypatch, max_attempts=1)
    _data, errors = assess_estate._pass2_structure(site)
    assert errors and errors[0]["listing"] == "structure"
    recorded = errors[0]["error"]
    assert ENV["TABLEAU_PAT_SECRET"] not in recorded
    assert "[REDACTED]" in recorded
    assert no_sleep == []


def test_estate_db_records_whether_the_run_was_degraded(tmp_path):
    """`estate.db` is read programmatically by harvest/deploy, which never open `assessment.json`.
    Before #196 a survived-but-partial DB was indistinguishable from a clean run of a smaller
    estate; it must now carry the marker itself - the run-level flags and one row per failed
    listing."""
    raw = _raw_fixture([_error("workbooks", assess_estate.PRIMARY)])
    store = assess_estate.write_store(tmp_path, raw, assess_estate.assemble(raw, 0.99))
    conn = assess_estate.sqlite3.connect(store)
    run = conn.execute(
        "SELECT degraded, degraded_primary, workbooks_total, listing_errors FROM assessment_run"
    ).fetchone()
    assert run == (1, 1, 1, 1)
    rows = conn.execute("SELECT listing, severity, error FROM listing_error").fetchall()
    assert rows == [("workbooks", assess_estate.PRIMARY, "transport: TimeoutError: timed out")]


def test_a_clean_run_marks_the_db_as_NOT_degraded(tmp_path):
    """The marker must exist on a CLEAN run too, or its absence is ambiguous (clean vs. an old DB
    written before this table existed). A clean run: flags 0/0, and no `listing_error` rows."""
    raw = _raw_fixture()
    store = assess_estate.write_store(tmp_path, raw, assess_estate.assemble(raw, 0.99))
    conn = assess_estate.sqlite3.connect(store)
    assert conn.execute("SELECT degraded, degraded_primary FROM assessment_run").fetchone() == (0, 0)
    assert conn.execute("SELECT count(*) FROM listing_error").fetchone() == (0,)


def test_a_graphql_error_body_never_reaches_estate_db(monkeypatch, no_sleep, tmp_path):
    """End to end, the two blind spots together: the 200-with-`errors` path AND `estate.db`, which
    the existing secret test covered neither of (it used the HTTPError body and read only JSON/md)."""
    code = _run_main(monkeypatch, tmp_path, _GraphqlLeak())
    out = tmp_path / "_assessment"
    assert code == 0  # workbooks were still returned, so the structure error is SECONDARY
    # Pin the DISCRIMINATING row (1, 0). The other two DB tests only cover (1, 1) and (0, 0), so
    # transposing the two columns - or writing `degraded` from `degraded_primary` - passed the whole
    # suite while claiming a secondary-degraded run was clean. The primary/secondary split is the
    # design's core and this row is the only signal a machine consumer gets, so it must be asserted.
    marker = (
        assess_estate.sqlite3.connect(out / "estate.db")
        .execute("SELECT degraded, degraded_primary FROM assessment_run")
        .fetchone()
    )
    assert marker == (1, 0)
    assert ENV["TABLEAU_PAT_SECRET"].encode() not in (out / "estate.db").read_bytes()
    errs = assess_estate.sqlite3.connect(out / "estate.db").execute("SELECT error FROM listing_error").fetchall()
    assert errs and all(ENV["TABLEAU_PAT_SECRET"] not in row[0] for row in errs)
    assert any("[REDACTED]" in row[0] for row in errs)
    assert no_sleep == []
