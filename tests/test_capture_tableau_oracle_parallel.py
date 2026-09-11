"""Focused controls for bounded one-session Tableau oracle capture parallelism (#619)."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_payload_facts import CSV_CERTIFIED  # noqa: E402  # pylint: disable=wrong-import-position

# These tests intentionally exercise the private orchestration seams used directly by main().
# pylint: disable=protected-access

LUID_1 = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
LUID_2 = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
LUID_3 = "cccccccc-3333-4333-8333-cccccccccccc"
WB_1 = "11111111-1111-4111-8111-111111111111"
WB_2 = "22222222-2222-4222-8222-222222222222"
WB_3 = "33333333-3333-4333-8333-333333333333"
SESSION_LOST = (
    b"<?xml version='1.0'?><tsResponse><error code='401002'><summary>Unauthorized Access</summary></error></tsResponse>"
)


def _creds() -> oracle.SiteCredentials:
    return oracle.SiteCredentials(
        base="https://example.online.tableau.com",
        site="site",
        pat_name="parallel-capture-pat",
        pat_secret="parallel-capture-secret",
        version="3.29",
    )


def _session() -> oracle.TableauSession:
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=3, budget_sec=60))
    session.token, session.site_id = "initial-session-token", "site-id"
    return session


def _view(luid: str, workbook_luid: str, name: str | None = None) -> dict:
    return {
        "id": luid,
        "name": name or luid[:8],
        "workbook": {"id": workbook_luid},
        "project": {"name": "Project"},
    }


def _record(view: dict) -> dict:
    luid = view["id"]
    return {
        "view_luid": luid,
        "view_name": view["name"],
        "workbook_luid": view["workbook"]["id"],
        "data": {
            "status": "ok",
            "certification": CSV_CERTIFIED,
            "path": f"data/{luid}.csv",
            "row_count": 1,
            "columns": ["value"],
            "elapsed_sec": 0.0,
            "reauths": 0,
            "retries": 0,
            "retry_reasons": [],
        },
    }


def _capture_selected(
    session: oracle.TableauSession,
    views: list[dict],
    out_dir: Path,
    workers: int,
) -> list[dict]:
    return list(
        oracle._capture_selected_views(
            session,
            views,
            out_dir=out_dir,
            wants=frozenset(),
            api_overrides={},
            max_age=oracle.DEFAULT_MAX_AGE_MINUTES,
            workers=workers,
        )
    )


class _MainSession:
    """Small main()-level session double; workers must all receive this exact instance."""

    version = "3.29"

    def __init__(self) -> None:
        self.site_id = "site-id"
        self.reauth_count = 0
        self.retry_count = 0
        self.signins = 0
        self.signouts = 0

    def sign_in(self) -> None:
        """Count the pool's one initial sign-in."""
        self.signins += 1

    def sign_out(self) -> None:
        """Count finally-path cleanup."""
        self.signouts += 1

    @staticmethod
    def redact_text(text: str) -> str:
        """No credentials occur in this main-level fixture."""
        return text


def _main_env() -> dict[str, str]:
    """Complete, non-secret-shaped configuration for main() tests."""
    return {
        "TABLEAU_SERVER_URL": "https://example.online.tableau.com",
        "TABLEAU_SITE": "site",
        "TABLEAU_PAT_NAME": "parallel-capture-pat",
        "TABLEAU_PAT_SECRET": "parallel-capture-secret",
        "TABLEAU_REST_API_VERSION": "3.29",
    }


def test_workers_default_to_serial_and_values_outside_one_through_four_are_rejected(tmp_path, capsys):
    """The compatibility default is one, and both bounds fail as usage errors."""
    parser = oracle.build_parser()
    assert parser.parse_args(["--out", str(tmp_path)]).workers == 1

    for value in ("0", "5", "not-a-number"):
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["--out", str(tmp_path), "--workers", value])
        assert excinfo.value.code == 2
        error = capsys.readouterr().err
        assert "--workers" in error
        assert "1" in error and "4" in error


def test_workers_one_is_serial(monkeypatch, tmp_path):
    """A second selected view cannot enter capture while workers=1 is blocked on the first."""
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    views = [_view(LUID_1, WB_1), _view(LUID_2, WB_2)]

    def capture(_session, view, *_args, **_kwargs):
        if view["id"] == LUID_1:
            first_started.set()
            assert release_first.wait(2)
        else:
            second_started.set()
        return _record(view)

    monkeypatch.setattr(oracle, "capture_view", capture)
    monkeypatch.setattr(oracle, "log_progress", lambda *_args: None)

    with ThreadPoolExecutor(max_workers=1) as harness:
        future = harness.submit(_capture_selected, _session(), views, tmp_path, 1)
        assert first_started.wait(2)
        assert not second_started.is_set()
        release_first.set()
        records = future.result(timeout=2)

    assert second_started.is_set()
    assert [record["view_luid"] for record in records] == [LUID_1, LUID_2]


def test_different_workbooks_overlap_with_two_workers(monkeypatch, tmp_path):
    """Two independent selected views reach a barrier together when workers=2."""
    entered = threading.Barrier(3)
    release = threading.Event()
    views = [_view(LUID_1, WB_1), _view(LUID_2, WB_2)]

    def capture(_session, view, *_args, **_kwargs):
        entered.wait(timeout=2)
        assert release.wait(2)
        return _record(view)

    monkeypatch.setattr(oracle, "capture_view", capture)
    monkeypatch.setattr(oracle, "log_progress", lambda *_args: None)

    with ThreadPoolExecutor(max_workers=1) as harness:
        future = harness.submit(_capture_selected, _session(), views, tmp_path, 2)
        entered.wait(timeout=2)
        release.set()
        records = future.result(timeout=2)

    assert [record["view_luid"] for record in records] == [LUID_1, LUID_2]


def test_same_workbook_views_overlap_without_affinity(monkeypatch, tmp_path):
    """The minimal pool does not serialize views merely because they share a workbook."""
    entered = threading.Barrier(3)
    release = threading.Event()
    views = [_view(LUID_1, WB_1), _view(LUID_2, WB_1)]

    def capture(_session, view, *_args, **_kwargs):
        entered.wait(timeout=2)
        assert release.wait(2)
        return _record(view)

    monkeypatch.setattr(oracle, "capture_view", capture)
    monkeypatch.setattr(oracle, "log_progress", lambda *_args: None)

    with ThreadPoolExecutor(max_workers=1) as harness:
        future = harness.submit(_capture_selected, _session(), views, tmp_path, 2)
        entered.wait(timeout=2)
        release.set()
        records = future.result(timeout=2)

    assert [record["view_luid"] for record in records] == [LUID_1, LUID_2]


def test_partial_pool_startup_cancels_and_drains_submitted_workers(monkeypatch, tmp_path):
    """A failed later submit cannot return while an earlier authenticated worker is still alive."""
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    second_submit_failed = threading.Event()
    attempted: list[str] = []
    shutdown_calls: list[tuple[bool, bool]] = []
    views = [_view(LUID_1, WB_1), _view(LUID_2, WB_2)]

    def capture(_session, view, *_args, **_kwargs):
        attempted.append(view["id"])
        worker_started.set()
        try:
            assert release_worker.wait(2)
            return _record(view)
        finally:
            worker_finished.set()

    class FailingSecondSubmitExecutor:
        """Delegate one worker to the real executor, then fail startup deterministically."""

        def __init__(  # pylint: disable=unused-argument
            self, max_workers, thread_name_prefix
        ):
            self._inner = ThreadPoolExecutor(max_workers=max_workers)
            self._submits = 0

        def submit(self, function, *args):
            """Start the first worker and fail before a second one can be registered."""
            self._submits += 1
            if self._submits == 2:
                second_submit_failed.set()
                raise RuntimeError("controlled second submit failure")
            return self._inner.submit(function, *args)

        def shutdown(self, *, wait, cancel_futures):
            """Record and delegate the cleanup contract."""
            shutdown_calls.append((wait, cancel_futures))
            self._inner.shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(oracle, "capture_view", capture)
    monkeypatch.setattr(oracle, "ThreadPoolExecutor", FailingSecondSubmitExecutor)

    with ThreadPoolExecutor(max_workers=1) as harness:
        run = harness.submit(_capture_selected, _session(), views, tmp_path, 2)
        assert worker_started.wait(2)
        assert second_submit_failed.wait(2)
        release_worker.set()
        with pytest.raises(RuntimeError, match="controlled second submit failure"):
            run.result(timeout=2)

    assert worker_finished.is_set()
    assert attempted == [LUID_1]
    assert shutdown_calls == [(True, True)]


def test_main_keeps_setup_serial_and_uses_one_initial_signin(monkeypatch, tmp_path):
    """Sign-in, inventory, capability probing and Metadata stamping each run once before workers."""
    session = _MainSession()
    views = [_view(LUID_1, WB_1), _view(LUID_2, WB_2)]
    calls = {"factory": 0, "inventory": 0, "probe": 0, "metadata": 0, "manifest": 0}

    def session_factory(*_args, **_kwargs):
        calls["factory"] += 1
        return session

    def inventory(*_args, **_kwargs):
        calls["inventory"] += 1
        return views, {WB_1: "One", WB_2: "Two"}

    def probe(*_args, **_kwargs):
        calls["probe"] += 1
        return {"server": None}

    def stamp(*_args, **_kwargs):
        calls["metadata"] += 1

    def capture(received_session, view, *_args, **_kwargs):
        assert received_session is session
        return _record(view)

    def manifest(records, *_args, **_kwargs):
        calls["manifest"] += 1
        assert [record["view_luid"] for record in records] == [LUID_1, LUID_2]
        return 0

    monkeypatch.setattr(oracle, "resolve_env", lambda _path: _main_env())
    monkeypatch.setattr(oracle, "TableauSession", session_factory)
    monkeypatch.setattr(oracle, "select_views", inventory)
    monkeypatch.setattr(oracle.capability, "probe_render_capability", probe)
    monkeypatch.setattr(oracle.capability, "apply_selected_tier", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", stamp)
    monkeypatch.setattr(oracle, "capture_view", capture)
    monkeypatch.setattr(oracle, "log_progress", lambda *_args: None)
    monkeypatch.setattr(oracle, "write_manifest", manifest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_tableau_oracle.py",
            "--out",
            str(tmp_path / "oracle"),
            "--reference-best",
            "--workers",
            "2",
        ],
    )

    assert oracle.main() == 0
    assert calls == {"factory": 1, "inventory": 1, "probe": 1, "metadata": 1, "manifest": 1}
    assert session.signins == 1
    assert session.signouts == 1


def test_one_signin_precedes_overlapping_authenticated_requests(monkeypatch, tmp_path):
    """A barrier proves request overlap on one token, while the sign-in path runs exactly once."""
    signin_count = 0
    active_signins = 0
    max_active_signins = 0
    state_lock = threading.Lock()
    request_barrier = threading.Barrier(2)
    authenticated_tokens: list[str | None] = []
    views = [_view(LUID_1, WB_1), _view(LUID_2, WB_1)]

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        nonlocal signin_count, active_signins, max_active_signins
        if req.full_url.endswith("/auth/signin"):
            with state_lock:
                signin_count += 1
                active_signins += 1
                max_active_signins = max(max_active_signins, active_signins)
            payload = json.dumps({"credentials": {"token": "one-pool-token", "site": {"id": "site-id"}}}).encode()
            with state_lock:
                active_signins -= 1
            return 200, payload, {}
        if "/data?" in req.full_url:
            with state_lock:
                assert signin_count == 1
                assert active_signins == 0
                authenticated_tokens.append(req.get_header("X-tableau-auth"))
            request_barrier.wait(timeout=2)
            return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}
        if req.full_url.endswith("/auth/signout"):
            return 204, b"", {}
        raise AssertionError(f"unexpected request: {req.full_url}")

    monkeypatch.setattr(oracle, "_request", transport)
    monkeypatch.setattr(oracle, "resolve_env", lambda _path: _main_env())
    monkeypatch.setattr(
        oracle,
        "select_views",
        lambda *_args, **_kwargs: (views, {WB_1: "One Workbook"}),
    )
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["capture_tableau_oracle.py", "--out", str(tmp_path / "oracle"), "--workers", "2"],
    )

    assert oracle.main() == 0
    assert signin_count == 1
    assert max_active_signins == 1
    assert authenticated_tokens == ["one-pool-token", "one-pool-token"]


def test_two_workers_losing_one_token_generation_reauthenticate_once(monkeypatch):
    """Two 401002 responses for the old generation share one refresh and both reuse its token."""
    signin_count = 0
    count_lock = threading.Lock()
    old_token_requests = threading.Barrier(2)
    new_token_requests: list[str] = []

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        nonlocal signin_count
        if req.full_url.endswith("/auth/signin"):
            with count_lock:
                signin_count += 1
                token = "old-session-token" if signin_count == 1 else "new-session-token"
            payload = json.dumps({"credentials": {"token": token, "site": {"id": "site-id"}}}).encode()
            return 200, payload, {}

        token = req.get_header("X-tableau-auth")
        if token == "old-session-token":
            old_token_requests.wait(timeout=2)
            return 401, SESSION_LOST, {}
        if token == "new-session-token":
            with count_lock:
                new_token_requests.append(req.full_url)
            return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}
        raise AssertionError(f"unexpected authentication token: {token!r}")

    monkeypatch.setattr(oracle, "_request", transport)
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=3, budget_sec=60))
    session.sign_in()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(session.export, ("/views/one/data", "/views/two/data")))

    stats = [result[2] for result in results]
    assert signin_count == 2, "one initial sign-in plus exactly one shared reauthentication"
    assert session.reauth_count == 1
    assert sorted(item["reauths"] for item in stats) == [0, 1]
    assert [item["retry_reasons"] for item in stats] == [["session_lost"], ["session_lost"]]
    assert len(new_token_requests) == 2


def test_failed_generation_refresh_is_reused_as_one_terminal_failure(monkeypatch):
    """Stale waiters re-raise one failed refresh instead of signing in again and later succeeding."""
    signin_count = 0
    count_lock = threading.Lock()
    old_token_requests = threading.Barrier(2)
    new_token_requests: list[str] = []

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        nonlocal signin_count
        if req.full_url.endswith("/auth/signin"):
            with count_lock:
                signin_count += 1
                attempt = signin_count
            if attempt == 1:
                payload = json.dumps(
                    {"credentials": {"token": "old-session-token", "site": {"id": "site-id"}}}
                ).encode()
                return 200, payload, {}
            if attempt == 2:
                return 401, b"replacement sign-in denied", {}
            payload = json.dumps({"credentials": {"token": "new-session-token", "site": {"id": "site-id"}}}).encode()
            return 200, payload, {}

        token = req.get_header("X-tableau-auth")
        if token == "old-session-token":
            old_token_requests.wait(timeout=2)
            return 401, SESSION_LOST, {}
        if token == "new-session-token":
            new_token_requests.append(req.full_url)
            return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}
        raise AssertionError(f"unexpected authentication token: {token!r}")

    def export_error(session, path):
        try:
            session.export(path)
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            return exc
        raise AssertionError("a failed generation refresh must not yield later worker success")

    monkeypatch.setattr(oracle, "_request", transport)
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=3, budget_sec=60))
    session.sign_in()

    with ThreadPoolExecutor(max_workers=2) as pool:
        errors = list(
            pool.map(
                lambda path: export_error(session, path),
                ("/views/one/data", "/views/two/data"),
            )
        )

    assert signin_count == 2, "one initial sign-in plus one failed replacement attempt"
    assert errors[0] is errors[1], "all stale waiters must re-raise the same terminal refresh failure"
    assert session.reauth_count == 0
    assert not new_token_requests


def test_concurrent_transient_retries_keep_exact_pool_and_per_view_counts(monkeypatch):
    """Concurrent increments cannot lose a retry from either the view facts or manifest aggregate."""
    attempts: dict[str, int] = {}
    attempts_lock = threading.Lock()

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        path = req.full_url.rsplit("/api/3.29", maxsplit=1)[-1]
        with attempts_lock:
            attempts[path] = attempts.get(path, 0) + 1
            attempt = attempts[path]
        if attempt == 1:
            return 503, b"gateway", {}
        return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}

    monkeypatch.setattr(oracle, "_request", transport)
    monkeypatch.setattr(oracle.time, "sleep", lambda _seconds: None)
    session = _session()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(session.export, ("/views/one/data", "/views/two/data")))

    assert [result[2]["retries"] for result in results] == [1, 1]
    assert session.retry_count == 2
    assert attempts == {"/views/one/data": 2, "/views/two/data": 2}


class _VirtualClock:
    """Thread-safe no-sleep clock for pool-wide Retry-After control."""

    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []
        self.lock = threading.Lock()

    def monotonic(self) -> float:
        """Return the shared virtual instant."""
        with self.lock:
            return self.value

    def perf_counter(self) -> float:
        """Use the same virtual timeline for elapsed request measurements."""
        return self.monotonic()

    def sleep(self, seconds: float) -> None:
        """Advance rather than sleeping in real time."""
        with self.lock:
            self.sleeps.append(seconds)
            self.value += seconds

    def advance(self, seconds: float) -> None:
        """Move an already-in-flight response later on the monotonic timeline."""
        with self.lock:
            self.value += seconds


def test_retry_after_stops_later_pool_requests_until_shared_cooldown(monkeypatch):
    """A 429 observed by one request delays a later worker at the session boundary."""
    clock = _VirtualClock()
    request_times: list[tuple[str, float]] = []

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        request_times.append((req.full_url, clock.monotonic()))
        if req.full_url.endswith("/first"):
            return 429, b"slow down", {"Retry-After": "7"}
        return 200, b"ok", {}

    monkeypatch.setattr(oracle, "_request", transport)
    session = _session()
    session._monotonic = clock.monotonic
    session._sleep = clock.sleep

    session._request("GET", "/first")
    with ThreadPoolExecutor(max_workers=1) as other_worker:
        status, _, _ = other_worker.submit(session._request, "GET", "/second").result(timeout=2)

    assert status == 200
    assert request_times[0][1] == 0.0
    assert request_times[1][1] == 7.0
    assert clock.sleeps == [7.0]


def test_another_views_shared_cooldown_does_not_spend_this_exports_retry_budget(monkeypatch):
    """A five-second admission wait cannot erase an otherwise recoverable 503-to-200 retry."""
    clock = _VirtualClock()
    calls = 0

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            clock.advance(1)
            return 503, b"gateway", {}
        clock.advance(0.1)
        return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}

    monkeypatch.setattr(oracle, "_request", transport)
    monkeypatch.setattr(oracle.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(oracle.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(oracle.time, "sleep", clock.sleep)
    monkeypatch.setattr(oracle, "backoff_delay", lambda *_args, **_kwargs: 1.0)
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=2, budget_sec=2.5))
    session.token, session.site_id = "initial-session-token", "site-id"
    other_export = object()
    session._request_context.export_id = other_export
    session._observe_rate_limit(429, {"Retry-After": "5"})
    del session._request_context.export_id

    payload, _, stats = session.export("/views/one/data")

    assert payload == b"value\n1\n"
    assert calls == 2
    assert stats["retries"] == 1
    assert clock.sleeps == [5.0, 1.0]


def test_external_cooldown_credit_does_not_hide_this_exports_request_and_backoff_cost(monkeypatch):
    """Only the shared admission wait is credited; this export's request and next backoff still bind."""
    clock = _VirtualClock()
    calls = 0

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            clock.advance(2)
            return 503, b"gateway", {}
        return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}

    monkeypatch.setattr(oracle, "_request", transport)
    monkeypatch.setattr(oracle.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(oracle.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(oracle.time, "sleep", clock.sleep)
    monkeypatch.setattr(oracle, "backoff_delay", lambda *_args, **_kwargs: 1.0)
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=2, budget_sec=2.5))
    session.token, session.site_id = "initial-session-token", "site-id"
    session._request_context.export_id = object()
    session._observe_rate_limit(429, {"Retry-After": "5"})
    del session._request_context.export_id

    with pytest.raises(oracle.ExportFailed, match="retry budget exhausted"):
        session.export("/views/one/data")

    assert calls == 1
    assert clock.sleeps == [5.0]


def test_valid_retry_after_extends_but_never_shortens_the_monotonic_cooldown(monkeypatch):
    """Later valid 429s may move the shared deadline out, never pull it in."""
    clock = _VirtualClock()
    request_times: list[float] = []

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        request_times.append(clock.monotonic())
        return 200, b"ok", {}

    monkeypatch.setattr(oracle, "_request", transport)
    session = _session()
    session._monotonic = clock.monotonic
    session._sleep = clock.sleep

    session._observe_rate_limit(429, {"Retry-After": "7"})
    clock.advance(1)
    session._observe_rate_limit(429, {"Retry-After": "10"})
    clock.advance(1)
    session._observe_rate_limit(429, {"Retry-After": "2"})
    session._request("GET", "/after-extensions")

    assert request_times == [11.0]
    assert clock.sleeps == [9.0]


def test_http_date_retry_after_creates_a_monotonic_pool_cooldown(monkeypatch):
    """A standards-valid HTTP-date is converted through wall time into a monotonic wait."""
    clock = _VirtualClock()
    epoch = 1_800_000_000.0
    request_times: list[float] = []

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        request_times.append(clock.monotonic())
        return 200, b"ok", {}

    monkeypatch.setattr(oracle, "_request", transport)
    session = _session()
    session._monotonic = clock.monotonic
    session._sleep = clock.sleep
    session._wall_time = lambda: epoch + clock.monotonic()
    retry_at = formatdate(epoch + 7, usegmt=True)

    session._observe_rate_limit(429, {"Retry-After": retry_at})
    session._request("GET", "/after-http-date")

    assert request_times == [7.0]
    assert clock.sleeps == [7.0]


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (429, {}),
        (429, {"Retry-After": "not-a-number"}),
        (429, {"Retry-After": "-1"}),
        (429, {"Retry-After": "nan"}),
        (503, {"Retry-After": "7"}),
    ],
)
def test_missing_invalid_or_non_429_retry_after_creates_no_shared_cooldown(monkeypatch, status, headers):
    """Only a valid Retry-After carried by HTTP 429 can delay another pool request."""
    clock = _VirtualClock()
    request_times: list[float] = []

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        request_times.append(clock.monotonic())
        return 200, b"ok", {}

    monkeypatch.setattr(oracle, "_request", transport)
    session = _session()
    session._monotonic = clock.monotonic
    session._sleep = clock.sleep

    session._observe_rate_limit(status, headers)
    session._request("GET", "/no-shared-cooldown")

    assert request_times == [0.0]
    assert not clock.sleeps


class _FixtureSession(oracle.TableauSession):
    """Deterministic CSV source that can force the second selected view to finish first."""

    def __init__(self, force_out_of_order: bool) -> None:
        super().__init__(_creds())
        self.token, self.site_id = "fixture-session-token", "site-id"
        self.force_out_of_order = force_out_of_order
        self.second_completed = threading.Event()
        self.completion_order: list[str] = []
        self._completion_lock = threading.Lock()
        self.signouts = 0

    def sign_in(self) -> None:
        """The controlled fixture is already authenticated."""

    def sign_out(self) -> None:
        """Record main()'s finally cleanup without opening a socket."""
        self.signouts += 1

    def export(self, path, *, api=None, retry=None, hard_deadline=None):  # noqa: ARG002
        luid = next(candidate for candidate in (LUID_1, LUID_2) if candidate in path)
        if self.force_out_of_order and luid == LUID_1:
            assert self.second_completed.wait(2)
        payload = f"value\n{1 if luid == LUID_1 else 2}\n".encode()
        if self.force_out_of_order and luid == LUID_2:
            self.second_completed.set()
        with self._completion_lock:
            self.completion_order.append(luid)
        return (
            payload,
            0.01,
            {
                "reauths": 0,
                "retries": 0,
                "retry_reasons": [],
                "content_type": "text/csv",
                "response_framing": oracle.FRAMING_CONTENT_LENGTH,
                "content_encoding": "identity",
            },
        )


def _stable_record_facts(record: dict) -> dict:
    """Fields that must be identical between controlled serial and parallel captures."""
    data = record["data"]
    return {
        "view_luid": record["view_luid"],
        "workbook_luid": record["workbook_luid"],
        "workbook_name": record["workbook_name"],
        "status": data["status"],
        "certification": data["certification"],
        "path": data["path"],
        "sha256": data["sha256"],
        "row_count": data["row_count"],
        "reauths": data["reauths"],
        "retries": data["retries"],
        "retry_reasons": data["retry_reasons"],
    }


def test_out_of_order_completion_reduces_to_original_records_progress_and_verdict(  # pylint: disable=too-many-locals
    monkeypatch, tmp_path
):
    """Completion order cannot change record order, progress order, hashes, statuses or exit code."""
    views = [_view(LUID_1, WB_1, "First"), _view(LUID_2, WB_2, "Second")]
    workbook_names = {WB_1: "Workbook One", WB_2: "Workbook Two"}
    progress: dict[str, list[str]] = {"serial": [], "parallel": []}
    active_run = "serial"
    monkeypatch.setattr(
        oracle,
        "log_progress",
        lambda _index, _total, record, _redactor=None: progress[active_run].append(record["view_luid"]),
    )

    serial_dir = tmp_path / "serial"
    serial_session = _FixtureSession(force_out_of_order=False)
    parallel_dir = tmp_path / "parallel"
    parallel_session = _FixtureSession(force_out_of_order=True)
    sessions = iter((serial_session, parallel_session))

    monkeypatch.setattr(oracle, "resolve_env", lambda _path: _main_env())
    monkeypatch.setattr(oracle, "TableauSession", lambda *_args, **_kwargs: next(sessions))
    monkeypatch.setattr(oracle, "select_views", lambda *_args, **_kwargs: (views, workbook_names))
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", lambda *_args, **_kwargs: None)

    monkeypatch.setattr(
        sys,
        "argv",
        ["capture_tableau_oracle.py", "--out", str(serial_dir), "--workers", "1"],
    )
    serial_code = oracle.main()
    active_run = "parallel"
    monkeypatch.setattr(
        sys,
        "argv",
        ["capture_tableau_oracle.py", "--out", str(parallel_dir), "--workers", "2"],
    )
    parallel_code = oracle.main()

    serial_manifest = json.loads((serial_dir / "oracle-manifest.json").read_text(encoding="utf-8"))
    parallel_manifest = json.loads((parallel_dir / "oracle-manifest.json").read_text(encoding="utf-8"))
    serial_records = serial_manifest["views"]
    parallel_records = parallel_manifest["views"]

    expected_order = [LUID_1, LUID_2]
    assert parallel_session.completion_order == [LUID_2, LUID_1]
    assert [record["view_luid"] for record in parallel_records] == expected_order
    assert progress["serial"] == progress["parallel"] == expected_order
    assert [_stable_record_facts(record) for record in parallel_records] == [
        _stable_record_facts(record) for record in serial_records
    ]
    assert serial_code == parallel_code == 0
    assert serial_session.signouts == parallel_session.signouts == 1

    for record in parallel_records:
        path = parallel_dir / record["data"]["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["data"]["sha256"]


def test_duplicate_luids_refuse_before_artifact_writes(monkeypatch, tmp_path):
    """Case variants refuse before capability/Metadata requests, capture calls or output creation."""
    session = _MainSession()
    duplicate = _view(LUID_1.upper(), WB_2)
    views = [_view(LUID_1, WB_1), duplicate]
    after_selection_called = False

    def after_selection(*_args, **_kwargs):
        nonlocal after_selection_called
        after_selection_called = True
        raise AssertionError("no post-selection request or capture may start for duplicate identities")

    monkeypatch.setattr(oracle, "resolve_env", lambda _path: _main_env())
    monkeypatch.setattr(oracle, "TableauSession", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(oracle, "select_views", lambda *_args, **_kwargs: (views, {}))
    monkeypatch.setattr(oracle.capability, "probe_render_capability", after_selection)
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", after_selection)
    monkeypatch.setattr(oracle, "capture_view", after_selection)
    out_dir = tmp_path / "oracle"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_tableau_oracle.py",
            "--out",
            str(out_dir),
            "--reference-best",
            "--workers",
            "2",
        ],
    )

    with pytest.raises(RuntimeError, match="duplicate selected view LUID"):
        oracle.main()

    assert not after_selection_called
    assert not out_dir.exists()
    assert session.signouts == 1


def test_duplicate_derived_output_identity_refuses_before_requests_or_writes(monkeypatch, tmp_path):
    """Distinct selected LUIDs cannot collapse onto one derived artifact identity."""
    session = _MainSession()
    views = [_view(LUID_1, WB_1), _view(LUID_2, WB_2)]
    after_selection_called = False
    real_artifact_stem = oracle.artifact_stem

    def colliding_stem(view_luid):
        if view_luid in {LUID_1, LUID_2}:
            return "same-output-identity"
        return real_artifact_stem(view_luid)

    def after_selection(*_args, **_kwargs):
        nonlocal after_selection_called
        after_selection_called = True
        raise AssertionError("capture must not start for colliding output identities")

    monkeypatch.setattr(oracle, "resolve_env", lambda _path: _main_env())
    monkeypatch.setattr(oracle, "TableauSession", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(oracle, "select_views", lambda *_args, **_kwargs: (views, {}))
    monkeypatch.setattr(oracle, "artifact_stem", colliding_stem)
    monkeypatch.setattr(oracle.capability, "probe_render_capability", after_selection)
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", after_selection)
    monkeypatch.setattr(oracle, "capture_view", after_selection)
    out_dir = tmp_path / "oracle"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture_tableau_oracle.py",
            "--out",
            str(out_dir),
            "--reference-best",
            "--workers",
            "2",
        ],
    )

    with pytest.raises(RuntimeError, match="duplicate derived output identities"):
        oracle.main()

    assert not after_selection_called
    assert not out_dir.exists()
    assert session.signouts == 1


class _CredentialSession(oracle.TableauSession):
    """Capture double that either reflects its token as the LUID or raises source_credential."""

    def __init__(self, reflected: bool = False) -> None:
        super().__init__(_creds())
        self.site_id = "site-id"
        self.token = LUID_1 if reflected else "credential-session-token"
        self.calls: list[str] = []

    def export(self, path, *, api=None, retry=None, hard_deadline=None):  # noqa: ARG002
        """Record the data request and refuse it as a source credential block."""
        self.calls.append(path)
        raise oracle.ExportFailed("source needs attention", "source_credential", "reauthorize")


def test_source_credential_still_skips_every_render(tmp_path):
    """A Tableau-side source credential block makes no render request."""
    session = _CredentialSession()
    record = oracle.capture_view(
        session,
        _view(LUID_1, WB_1),
        tmp_path,
        frozenset({"png", "svg", "pdf"}),
    )

    assert len(session.calls) == 1
    assert "/data?" in session.calls[0]
    assert record["data"]["status"] == "source_credential"
    assert {record[leg]["status"] for leg in ("image", "svg", "pdf")} == {"source_credential"}


def test_reflected_credential_still_skips_every_export_and_render(tmp_path):
    """A credential-shaped selected LUID is refused before any endpoint is called."""
    session = _CredentialSession(reflected=True)
    record = oracle.capture_view(
        session,
        _view(LUID_1, WB_1),
        tmp_path,
        frozenset({"png", "svg", "pdf"}),
    )

    assert not session.calls
    assert record["data"]["status"] == oracle.CREDENTIAL_REFLECTED


def test_partial_manifest_write_is_removed_on_any_baseexception(monkeypatch, tmp_path):
    """A partially written normal manifest is deleted before publication failure is re-raised."""
    session = _MainSession()
    views = [_view(LUID_1, WB_1)]
    out_dir = tmp_path / "oracle"
    partial_written = threading.Event()
    real_write_text = Path.write_text

    def partial_manifest_write(path, data, *args, **kwargs):
        if path.name == "oracle-manifest.json":
            real_write_text(path, data[:40], *args, **kwargs)
            partial_written.set()
            raise KeyboardInterrupt("controlled manifest publication interruption")
        return real_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(oracle, "resolve_env", lambda _path: _main_env())
    monkeypatch.setattr(oracle, "TableauSession", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(oracle, "select_views", lambda *_args, **_kwargs: (views, {WB_1: "One"}))
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(oracle, "capture_view", lambda _session, view, *_args, **_kwargs: _record(view))
    monkeypatch.setattr(Path, "write_text", partial_manifest_write)
    monkeypatch.setattr(
        sys,
        "argv",
        ["capture_tableau_oracle.py", "--out", str(out_dir), "--workers", "1"],
    )

    with pytest.raises(KeyboardInterrupt, match="controlled manifest publication interruption"):
        oracle.main()

    assert partial_written.is_set()
    assert not (out_dir / "oracle-manifest.json").exists()
    assert session.signouts == 1


def test_unexpected_worker_exception_signs_out_cancels_pending_and_writes_no_manifest(monkeypatch, tmp_path):
    """An unexpected worker failure cancels undispatched work, removes stale success and signs out."""
    session = _MainSession()
    second_started = threading.Event()
    release_second = threading.Event()
    failure_published = threading.Event()
    attempted: list[str] = []
    views = [
        _view(LUID_1, WB_1),
        _view(LUID_2, WB_2),
        _view(LUID_3, WB_3),
    ]

    def capture(_session, view, *_args, **_kwargs):
        attempted.append(view["id"])
        if view["id"] == LUID_1:
            assert second_started.wait(2)
            raise RuntimeError("worker boom")
        if view["id"] == LUID_2:
            second_started.set()
            assert release_second.wait(2)
        return _record(view)

    def manifest(*_args, **_kwargs):
        raise AssertionError("an unexpected worker exception must not publish a normal manifest")

    real_capture_worker = oracle._capture_worker

    def observed_capture_worker(*args, **kwargs):
        try:
            return real_capture_worker(*args, **kwargs)
        except RuntimeError:
            failure_published.set()
            raise

    out_dir = tmp_path / "oracle"
    out_dir.mkdir()
    (out_dir / "oracle-manifest.json").write_text('{"stale": true}\n', encoding="utf-8")

    monkeypatch.setattr(oracle, "resolve_env", lambda _path: _main_env())
    monkeypatch.setattr(oracle, "TableauSession", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(
        oracle,
        "select_views",
        lambda *_args, **_kwargs: (views, {WB_1: "One", WB_2: "Two", WB_3: "Three"}),
    )
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(oracle, "_capture_worker", observed_capture_worker)
    monkeypatch.setattr(oracle, "capture_view", capture)
    monkeypatch.setattr(oracle, "log_progress", lambda *_args: None)
    monkeypatch.setattr(oracle, "write_manifest", manifest)
    monkeypatch.setattr(
        sys,
        "argv",
        ["capture_tableau_oracle.py", "--out", str(out_dir), "--workers", "2"],
    )

    with ThreadPoolExecutor(max_workers=1) as harness:
        run = harness.submit(oracle.main)
        assert failure_published.wait(2)
        release_second.set()
        with pytest.raises(RuntimeError, match="worker boom"):
            run.result(timeout=2)

    assert set(attempted) == {LUID_1, LUID_2}
    assert LUID_3 not in attempted
    assert not (out_dir / "oracle-manifest.json").exists()
    assert session.signouts == 1
