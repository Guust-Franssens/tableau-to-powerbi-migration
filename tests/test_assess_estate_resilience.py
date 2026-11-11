"""Transport-failure resilience for scripts/assess_estate.py (issue #193)."""

import http.client
import importlib.util
import json
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
    http.client.RemoteDisconnected("Remote end closed connection without response"),
]


class _Response:
    """The slice of ``http.client.HTTPResponse`` that ``Site._raw`` actually touches. ⚠️ Plus ``headers``, which
    ``Site._raw`` does NOT touch and ``tableau_http._request`` does. Refs #474."""

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
# The numbers are the customer site behind #474: an on-prem Server whose ceiling is BELOW the SVG
# floor, so the assessment must resolve to "best rung is PDF" rather than to an .env remedy.
SES_SERVERINFO = (
    b'<?xml version="1.0" encoding="UTF-8"?><tsResponse><serverInfo>'
    b'<productVersion build="20253.25.0904.1234">2025.3.3</productVersion>'
    b"<restApiVersion>3.27</restApiVersion></serverInfo></tsResponse>"
)


def serverinfo_reporting(version: str) -> bytes:
    """The same body with an arbitrary ``restApiVersion`` -- including one that is not a version. Refs #475."""
    return SES_SERVERINFO.replace(b"<restApiVersion>3.27<", f"<restApiVersion>{version}<".encode())


# ⚠️ A session token that is ALSO a valid REST API version. Nothing in this repo -- or in Tableau's
# API -- constrains a token's shape: ``Site`` binds ``creds["token"]`` exactly as it arrives. So this
# is not a contrived string, it is the shape that defeats any "a credential cannot look like a
# version" argument, and it is deliberately equal to the version ``SES_SERVERINFO`` reports.
NUMERIC_SECRET = "3.27"


class FakeTableau:
    """A scripted Tableau site. ``fail`` maps a URL fragment to an exception, a status, or a list
    of those consumed one per call, so a test injects a fault on ONE endpoint and nothing else."""

    SIGNIN = {"credentials": {"token": "session-token", "site": {"id": "site-1"}}}

    def __init__(
        self,
        fail: dict | None = None,
        *,
        serverinfo: bytes | None = SES_SERVERINFO,
        serverinfo_status: int = 200,
    ) -> None:
        self.fail = {key: (value if isinstance(value, list) else [value]) for key, value in (fail or {}).items()}
        self.calls: list[tuple[str, float | None]] = []
        # ``None`` = this site does not answer ``/serverinfo`` at all, which is a real on-prem shape
        # (a reverse proxy in front of Tableau can refuse it) and the state-C input.
        self.serverinfo = serverinfo
        # ⚠️ A non-200 that still carries a BODY -- a proxy error page, a cached response. Distinct
        # from ``serverinfo=None`` (no body at all), because the defect being pinned is that the body
        # was parsed anyway (#475 review, finding 2).
        self.serverinfo_status = serverinfo_status

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
            if self.serverinfo_status != 200:
                raise urllib.error.HTTPError(
                    url, self.serverinfo_status, "nope", {}, _Response(self.serverinfo_status, self.serverinfo)
                )
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


# --- 2. the retry budget is bounded, and an auth refusal is never retried ------------------------


def test_a_transient_failure_is_retried_up_to_the_bound(monkeypatch, no_sleep):
    server = FakeTableau({"/customviews?": TimeoutError("timed out")})
    site = _site(server, monkeypatch, max_attempts=3)
    rows, error = site.paged("/sites/site-1/customviews", "customViews", "customView")
    assert (rows, error["attempts"]) == ([], 3)
    assert server.count("/customviews?") == 3
    assert len(no_sleep) == 2  # two backoffs between three attempts


@pytest.mark.parametrize("status", [401, 404])
def test_an_auth_refusal_or_a_missing_endpoint_is_a_FINAL_answer_and_is_never_retried(status, monkeypatch, no_sleep):
    """AGENTS.md: a missing credential is not transient. Retrying burns the budget and cannot win."""
    server = FakeTableau({"/customviews?": status})
    site = _site(server, monkeypatch, max_attempts=5)
    _, error = site.paged("/sites/site-1/customviews", "customViews", "customView")
    assert server.count("/customviews?") == 1
    assert error["attempts"] == 1 and no_sleep == []


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


def test_an_auth_denied_grant_listing_stays_silent_but_is_counted(monkeypatch, no_sleep, tmp_path):
    """ "You may not see this" is a fact about the estate; "no answer" is not. Only the second
    degrades, or every locked-down project would raise a false alarm."""
    site = _site(FakeTableau({"/permissions": 403}), monkeypatch)
    rows, errors, denied = assess_estate._grants(site, "workbook", "wb-1", "Sales", "/workbooks/wb-1")
    assert (rows, errors, denied) == ([], [], True)
    permissions, iam_errors, denied_listings = assess_estate._collect_iam(site, [{"id": "p-1", "name": "Finance"}], [])
    assert (permissions, iam_errors, denied_listings) == ([], [], 1)
    raw = _raw_fixture()
    raw["permission_denied_listings"] = denied_listings
    store = assess_estate.write_store(tmp_path, raw, assess_estate.assemble(raw, 0.99))
    with assess_estate.sqlite3.connect(store) as connection:
        assert connection.execute("SELECT denied_listings FROM permission_collection").fetchone() == (1,)
    assert no_sleep == []


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), 500], ids=["timeout", "500"])
def test_an_unreadable_grant_IS_degraded(failure, monkeypatch, no_sleep):
    """Only auth refusals are a useful permissions answer; other failures mean the grant rows are unknown."""
    site = _site(FakeTableau({"/permissions": failure}), monkeypatch, max_attempts=1)
    _, errors, denied = assess_estate._grants(site, "workbook", "wb-1", "Sales", "/workbooks/wb-1")
    assert errors[0]["severity"] == assess_estate.SECONDARY
    assert "Sales" in errors[0]["listing"] and denied is False and no_sleep == []


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
        "permission_denied_listings": 0,
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


def test_a_secondary_failure_degrades_and_names_the_listing():
    assembled = assess_estate.assemble(_raw_fixture([_error()]), 0.99)
    assert (assembled["degraded"], assembled["degraded_primary"]) == (True, False)
    assert assembled["summary"]["degraded"] is True
    report = assess_estate.render_report(assembled, _raw_fixture(), 0.99)
    assert "[WARN]" in report and "custom_views" in report
    assert "[ACTION] this assessment is DEGRADED" in report
    assert "PRIMARY listing" not in report
    # One wording, two surfaces: a reader who sees only one must reach the same conclusion.
    for line in assess_estate._warn_lines(assembled):
        assert line in report


def test_a_primary_failure_leads_the_report_and_says_what_is_missing():
    assembled = assess_estate.assemble(_raw_fixture([_error("workbooks", assess_estate.PRIMARY)]), 0.99)
    assert assembled["degraded_primary"] is True
    report = assess_estate.render_report(assembled, _raw_fixture(), 0.99)
    assert report.startswith("# ⚠️ DEGRADED")
    assert "PRIMARY listing is INCOMPLETE" in report.splitlines()[0]
    assert "workbooks" in report and "[ACTION] a PRIMARY listing failed" in report


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


def test_a_200_with_a_non_object_json_body_returns_a_recorded_error(monkeypatch, no_sleep):
    """The recovery ladder must never return ``(None, None)`` or a non-object payload on HTTP 200."""

    class NonObjectBody(FakeTableau):
        """Return a syntactically valid but unusable JSON value for one endpoint."""

        def urlopen(self, request, timeout=None):
            if "/customviews?" in request.full_url:
                self.calls.append((request.full_url, timeout))
                return _Response(200, b"[]")
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


def test_reflected_site_id_is_scrubbed_in_the_initial_listing_log(monkeypatch, no_sleep, caplog):
    """The outbound path stays raw, but its pre-request progress log must not disclose it."""

    class ReflectedSite(FakeTableau):
        SIGNIN = {"credentials": {"token": "session-token", "site": {"id": ENV["TABLEAU_PAT_SECRET"]}}}

    with caplog.at_level("INFO"):
        site = _site(ReflectedSite(), monkeypatch, max_attempts=1)
        assess_estate._listing(site, assess_estate.INVENTORY[0])  # pylint: disable=protected-access
    assert ENV["TABLEAU_PAT_SECRET"] not in caplog.text


def test_write_store_scrubs_every_sqlite_input(monkeypatch, no_sleep, tmp_path):
    """Raw identities may support assembly, but the SQLite sink receives only scrubbed copies."""
    site = _site(FakeTableau(), monkeypatch, max_attempts=1)
    raw = assess_estate.collect(site, None)
    secret = ENV["TABLEAU_PAT_SECRET"]
    raw["projects"][0]["name"] = secret
    raw["groups"][0]["name"] = secret
    raw["datasources"][0]["name"] = secret
    raw["structure"]["publishedDatasources"] = [{"name": secret, "upstreamTables": [{"fullName": secret}]}]
    raw["permissions"] = [
        {
            "object_type": secret,
            "object_luid": secret,
            "object_name": secret,
            "grantee_type": secret,
            "grantee_luid": secret,
            "capability": secret,
            "mode": secret,
        }
    ]
    raw["collection_errors"] = [
        {
            "listing": secret,
            "severity": assess_estate.SECONDARY,
            "status": 500,
            "path": secret,
            "page": 1,
            "attempts": 1,
            "elapsed_sec": 0,
            "transport": False,
            "error": secret,
        }
    ]
    assembled = assess_estate.assemble(raw, 0.99)
    store = assess_estate.write_store(tmp_path, raw, assembled, site.scrub_text)
    assert secret.encode() not in store.read_bytes()
    assert all(secret not in path.read_text(encoding="utf-8") for path in (tmp_path / "raw").glob("*.json"))


def test_checkpoint_scrubs_reflected_response_before_writing_raw_json(monkeypatch, no_sleep, tmp_path):
    """Pass-one checkpoint artifacts use the same redaction boundary as final raw output."""
    site = _site(FakeTableau(), monkeypatch, max_attempts=1)
    secret = ENV["TABLEAU_PAT_SECRET"]
    assess_estate._checkpoint(tmp_path, {"workbooks": [{"name": secret}]}, site.scrub_text)  # pylint: disable=protected-access
    assert secret not in (tmp_path / "raw" / "workbooks.json").read_text(encoding="utf-8")


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


# --- #474: the site's render ceiling is a property of the SITE, so the ASSESSMENT reports it -----


def test_a_clean_run_exits_0_and_reports_the_render_ceiling_in_both_artifacts(monkeypatch, no_sleep, tmp_path):
    """An operator learns "this site tops out at PDF" HERE, not as a capture-time warning later."""
    code = _run_main(monkeypatch, tmp_path, FakeTableau())
    out = tmp_path / "_assessment"
    assessment = json.loads((out / "assessment.json").read_text(encoding="utf-8"))
    report = (out / "report.md").read_text(encoding="utf-8")
    ceiling = assessment["server_ceiling"]
    assert code == 0
    assert report.startswith("# Estate assessment")
    assert "DEGRADED" not in report and assessment["degraded"] is False
    # The three numbers this repo insists are different things...
    assert (ceiling["client_api_version"], ceiling["advertised_api_version"]) == ("3.21", "3.27")
    # ...and, the point of the whole block, the IMPLICATION per rung rather than the arithmetic.
    assert [(r["tier"], r["verdict"]) for r in ceiling["rungs"]] == [
        ("svg", "unavailable"),
        ("pdf", "available"),
        ("png_high", "available"),
    ]
    assert ceiling["best_reference_render"] == "pdf"
    assert "| rung | route |" in report
    assert "UNAVAILABLE on this server" in report
    assert "Bottom line: `--reference-best` should resolve to `pdf`" in report
    assert "at any client setting" in report
    # The raster ceiling, quoted from this repo's own measurement.
    assert "1300×1600" in report
    assert (ceiling["product_version"], ceiling["build"]) == ("2025.3.3", "20253.25.0904.1234")
    assert ceiling["advertised_release"] == "Tableau 2025.3"
    assert ceiling["advertised_api_version_reflected"] is False
    assert "product `2025.3.3`" in report and "REST `3.27`" in report
    assert "matched a credential this run holds" not in report
    assert no_sleep == []


def test_a_site_that_refuses_serverinfo_reports_UNKNOWN_and_still_assesses_cleanly(monkeypatch, no_sleep, tmp_path):
    """Fail soft: an unanswered probe is the third state, never a degraded inventory."""
    code = _run_main(monkeypatch, tmp_path, FakeTableau(serverinfo=None))
    out = tmp_path / "_assessment"
    assessment = json.loads((out / "assessment.json").read_text(encoding="utf-8"))
    report = (out / "report.md").read_text(encoding="utf-8")
    ceiling = assessment["server_ceiling"]
    assert code == 0
    assert assessment["degraded"] is False and assessment["listing_errors"] == []
    assert ceiling["established"] is False
    assert {r["verdict"] for r in ceiling["rungs"]} == {"unknown"}
    assert ceiling["best_reference_render"] is None
    assert "was NOT established" in report
    assert "No per-rung verdict is shown" in report
    assert "| rung | route |" not in report
    assert "Bottom line" not in report
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


def test_a_NUMERIC_credential_reflected_as_the_REST_VERSION_reaches_no_surface(monkeypatch, no_sleep, tmp_path, caplog):
    """`NUMERIC_REST_TOKEN_200`. The exemption that survived round 2, and why it had to go."""

    class Echoing(FakeTableau):
        """The site's `/serverinfo` reports a version that IS this run's credential."""

        def urlopen(self, request, timeout=None):
            if "/serverinfo" in request.full_url:
                return _Response(200, serverinfo_reporting(NUMERIC_SECRET))
            return super().urlopen(request, timeout)

    monkeypatch.setitem(ENV, "TABLEAU_PAT_SECRET", NUMERIC_SECRET)
    with caplog.at_level("INFO"):
        code = _run_main(monkeypatch, tmp_path, Echoing())
    out = tmp_path / "_assessment"
    written = (out / "assessment.json").read_text(encoding="utf-8")
    report = (out / "report.md").read_text(encoding="utf-8")
    ceiling = json.loads(written)["server_ceiling"]
    assert code == 0
    # ⚠️ A bare `NUMERIC_SECRET not in written` is worthless here -- `3.27` is a plausible substring
    # of a floor, a release name or a date. The claim is that the value is never PRESENTED as this
    # site's advertised ceiling, so each assertion names the field or the rendered phrase.
    assert ceiling["advertised_api_version"] != NUMERIC_SECRET
    assert f'"advertised_api_version": "{NUMERIC_SECRET}"' not in written
    assert f"REST `{NUMERIC_SECRET}`" not in report
    assert f"advertises REST {NUMERIC_SECRET}" not in caplog.text
    # The release name is suppressed WITH the number: `API_RELEASE` is a bijection, so printing
    # "Tableau 2025.3" would hand back the digits the redaction just removed.
    assert ceiling["advertised_release"] is None
    assert "Tableau 2025.3" not in report
    # ...and the capability survives, which is the whole point of separating it from the text.
    assert ceiling["established"] is True and ceiling["advertised_api_version_reflected"] is True
    assert [(r["tier"], r["verdict"]) for r in ceiling["rungs"]] == [
        ("svg", "unavailable"),
        ("pdf", "available"),
        ("png_high", "available"),
    ]
    assert ceiling["best_reference_render"] == "pdf"
    assert "| rung | route |" in report
    # The operator is TOLD the number was suppressed -- a bare marker where a version belongs reads
    # as a bug in this tool, and a server echoing a credential is itself worth escalating.
    assert "matched a credential this run holds" in report
    assert "was REDACTED" in caplog.text
    assert no_sleep == []


# --- 8. #196: scrub the 200-with-`errors` GraphQL body, and mark degradation in estate.db ---------


class _GraphqlLeak(FakeTableau):
    """A Metadata API that answers 200 but reflects the caller's credential in its ``errors`` array. Refs #196."""

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
    """`estate.db` is read programmatically by harvest/deploy, which never open `assessment.json`. Refs #196."""
    raw = _raw_fixture([_error("workbooks", assess_estate.PRIMARY)])
    store = assess_estate.write_store(tmp_path / "degraded", raw, assess_estate.assemble(raw, 0.99))
    conn = assess_estate.sqlite3.connect(store)
    run = conn.execute(
        "SELECT degraded, degraded_primary, workbooks_total, listing_errors FROM assessment_run"
    ).fetchone()
    assert run == (1, 1, 1, 1)
    rows = conn.execute("SELECT listing, severity, error FROM listing_error").fetchall()
    assert rows == [("workbooks", assess_estate.PRIMARY, "transport: TimeoutError: timed out")]
    conn.close()

    raw = _raw_fixture()
    store = assess_estate.write_store(tmp_path / "clean", raw, assess_estate.assemble(raw, 0.99))
    conn = assess_estate.sqlite3.connect(store)
    assert conn.execute("SELECT degraded, degraded_primary FROM assessment_run").fetchone() == (0, 0)
    assert conn.execute("SELECT count(*) FROM listing_error").fetchone() == (0,)
    conn.close()


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
