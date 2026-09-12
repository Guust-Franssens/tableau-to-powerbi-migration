"""Focused controls for bounded one-session Tableau oracle capture parallelism (#619)."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from email.utils import formatdate
from pathlib import Path
from types import GeneratorType

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
HANG_GUARD_SEC = 5


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


def _configure_main(monkeypatch, session, views, out_dir, workbook_names=None) -> None:
    """Keep new coordinator controls on main's real consumption and finally paths."""
    monkeypatch.setattr(oracle, "resolve_env", lambda _path: _main_env())
    monkeypatch.setattr(oracle, "TableauSession", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(oracle, "select_views", lambda *_args, **_kwargs: (views, workbook_names or {}))
    monkeypatch.setattr(oracle.tableau_view_types, "resolve_and_stamp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["capture_tableau_oracle.py", "--out", str(out_dir), "--workers", "2"],
    )


@pytest.mark.parametrize(("workers", "view_count"), [(1, 0), (4, 0), (1, 2)])
def test_empty_and_serial_iteration_are_closeable_without_an_executor(monkeypatch, tmp_path, workers, view_count):
    """Empty and serial iteration share the close contract without creating pool resources."""
    captured = []
    views = [_view(LUID_1, WB_1), _view(LUID_2, WB_2)][:view_count]

    def no_executor(*_args, **_kwargs):
        pytest.fail("empty/serial iteration must not create an executor")

    def capture(_session, view, *_args, **_kwargs):
        captured.append(view["id"])
        return _record(view)

    monkeypatch.setattr(oracle, "ThreadPoolExecutor", no_executor)
    monkeypatch.setattr(oracle, "capture_view", capture)
    records = oracle._capture_selected_views(
        _session(), views, out_dir=tmp_path, wants=frozenset(), api_overrides={}, max_age=1, workers=workers
    )
    assert isinstance(records, GeneratorType)
    assert not captured
    try:
        if views:
            assert next(records)["view_luid"] == LUID_1
            assert captured == [LUID_1], "serial capture must yield before starting the next view"
        else:
            assert not list(records)
    finally:
        records.close()
    assert not list(records)
    assert captured == ([LUID_1] if views else [])


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_closing_before_first_next_allocates_no_executor(monkeypatch, tmp_path, workers):
    """Even a nonempty parallel generator owns no executor before iteration begins."""

    def no_executor(*_args, **_kwargs):
        pytest.fail("an unstarted generator must own no executor")

    monkeypatch.setattr(oracle, "ThreadPoolExecutor", no_executor)
    records = oracle._capture_selected_views(
        _session(),
        [_view(LUID_1, WB_1), _view(LUID_2, WB_2)],
        out_dir=tmp_path,
        wants=frozenset(),
        api_overrides={},
        max_age=1,
        workers=workers,
    )
    assert isinstance(records, GeneratorType)
    records.close()
    assert not list(records)


def test_first_view_progress_is_visible_while_later_sibling_is_blocked(  # pylint: disable=too-many-locals
    monkeypatch, tmp_path, caplog
):
    """The real progress logger runs before a later selected export is allowed to finish."""
    session = _MainSession()
    second_started = threading.Event()
    release_second = threading.Event()
    first_progress = threading.Event()
    progress = []
    out_dir = tmp_path / "oracle"
    views = [_view(LUID_1, WB_1, "First"), _view(LUID_2, WB_2, "Second")]
    real_progress = oracle.log_progress

    def capture(_session, view, *_args, **_kwargs):
        if view["id"] == LUID_1:
            assert second_started.wait(HANG_GUARD_SEC)
        else:
            second_started.set()
            assert release_second.wait(HANG_GUARD_SEC)
        return _record(view)

    def observe_progress(index, total, record, redactor):
        real_progress(index, total, record, redactor)
        progress.append(record["view_luid"])
        if index == 1:
            first_progress.set()

    _configure_main(monkeypatch, session, views, out_dir)
    monkeypatch.setattr(oracle, "capture_view", capture)
    monkeypatch.setattr(oracle, "log_progress", observe_progress)

    with caplog.at_level(logging.INFO, logger=oracle.LOG.name), ThreadPoolExecutor(max_workers=1) as harness:
        run = harness.submit(oracle.main)
        try:
            assert first_progress.wait(HANG_GUARD_SEC), "first-view progress must not wait for the blocked sibling"
            assert progress == [LUID_1]
            assert any("First" in message and "1/2" in message for message in caplog.messages)
            assert session.signouts == 0
            assert not (out_dir / "oracle-manifest.json").exists()
        finally:
            release_second.set()
        assert run.result(timeout=HANG_GUARD_SEC) == 0
    assert progress == [LUID_1, LUID_2]
    assert session.signouts == 1


@pytest.mark.parametrize("fault_site", ["enrichment", "log_progress"])
def test_coordinator_interrupt_drains_before_signout(  # pylint: disable=too-many-locals,too-many-statements
    monkeypatch, tmp_path, fault_site
):
    """Either consumer fault must enter drain before any active worker exits or sign-out occurs."""
    second_started = threading.Event()
    fault_raised = threading.Event()
    drain_entered = threading.Event()
    release_workers = threading.Event()
    signed_out = threading.Event()
    state_lock = threading.Lock()
    events = []
    active_workers = set()
    active_at_signout = []
    data_requests = []
    post_signout_requests = []
    iterators = []
    out_dir = tmp_path / "oracle"
    views = [_view(LUID_1, WB_1), _view(LUID_2, WB_2), _view(LUID_3, WB_3)]
    real_worker = oracle._capture_worker
    real_iterator = oracle._capture_selected_views

    class HeldFirstSlot(Future):
        """Keep the first worker occupied after its slot is published, leaving view three pending."""

        def set_result(self, result) -> None:
            super().set_result(result)
            if result["view_luid"] == LUID_1:
                assert release_workers.wait(HANG_GUARD_SEC)

    class ObservedExecutor(ThreadPoolExecutor):
        """Observe the existing shutdown seam, not a substitute scheduler."""

        def shutdown(self, wait=True, *, cancel_futures=False) -> None:
            assert wait and cancel_futures
            with state_lock:
                events.append("drain")
            drain_entered.set()
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def observed_worker(*args, **kwargs):
        identity = threading.get_ident()
        with state_lock:
            active_workers.add(identity)
        try:
            return real_worker(*args, **kwargs)
        finally:
            with state_lock:
                active_workers.remove(identity)
                events.append("worker-exit")

    def retained_iterator(*args, **kwargs):
        records = real_iterator(*args, **kwargs)
        iterators.append(records)
        return records

    def transport(req, **_kwargs):
        if req.full_url.endswith("/auth/signin"):
            return 200, json.dumps({"credentials": {"token": "pool-token", "site": {"id": "site-id"}}}).encode(), {}
        if req.full_url.endswith("/auth/signout"):
            with state_lock:
                active_at_signout.append(len(active_workers))
                events.append("signout")
            signed_out.set()
            return 204, b"", {}
        with state_lock:
            data_requests.append(req.full_url)
            if signed_out.is_set():
                post_signout_requests.append(req.full_url)
        if LUID_1 in req.full_url:
            assert second_started.wait(HANG_GUARD_SEC)
        elif LUID_2 in req.full_url:
            second_started.set()
            assert release_workers.wait(HANG_GUARD_SEC)
        return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}

    def interrupt(*_args, **_kwargs):
        assert second_started.is_set()
        with state_lock:
            assert len(active_workers) == 2
            events.append("fault")
        fault_raised.set()
        raise KeyboardInterrupt(f"controlled {fault_site} interruption")

    class InterruptingNames(dict):
        """Inject at workbook-name enrichment independently of the progress call."""

        def get(self, key, default=None):
            return interrupt(key, default)

    def no_manifest(*_args, **_kwargs):
        pytest.fail("a coordinator interruption must not publish a manifest")

    session = _session()
    names = InterruptingNames({WB_1: "One"}) if fault_site == "enrichment" else {WB_1: "One"}
    _configure_main(monkeypatch, session, views, out_dir, names)
    monkeypatch.setattr(oracle, "_request", transport)
    monkeypatch.setattr(oracle, "Future", HeldFirstSlot)
    monkeypatch.setattr(oracle, "ThreadPoolExecutor", ObservedExecutor)
    monkeypatch.setattr(oracle, "_capture_worker", observed_worker)
    monkeypatch.setattr(oracle, "_capture_selected_views", retained_iterator)
    monkeypatch.setattr(oracle, "write_manifest", no_manifest)
    if fault_site == "log_progress":
        monkeypatch.setattr(oracle, "log_progress", interrupt)

    with ThreadPoolExecutor(max_workers=1) as harness:
        run = harness.submit(oracle.main)
        try:
            assert fault_raised.wait(HANG_GUARD_SEC)
            assert drain_entered.wait(HANG_GUARD_SEC), "the consumer must close its iterator before sign-out"
            assert not signed_out.is_set()
        finally:
            release_workers.set()
            try:
                with pytest.raises(KeyboardInterrupt, match=f"controlled {fault_site} interruption"):
                    run.result(timeout=HANG_GUARD_SEC)
            finally:
                for records in iterators:
                    records.close()

    assert events == ["fault", "drain", "worker-exit", "worker-exit", "signout"]
    assert active_at_signout == [0]
    assert not active_workers
    assert len(data_requests) == 2
    assert all(LUID_3 not in path for path in data_requests)
    assert not post_signout_requests
    assert not (out_dir / "oracle-manifest.json").exists()


def test_normal_exhaustion_drains_before_manifest(monkeypatch, tmp_path):
    """Successful records are progressive, but manifest publication waits for worker exit and shutdown."""
    session = _MainSession()
    release_workers = threading.Event()
    all_progress = threading.Event()
    events = []
    real_worker = oracle._capture_worker
    views = [_view(LUID_1, WB_1), _view(LUID_2, WB_2)]

    class ObservedExecutor(ThreadPoolExecutor):
        """Record completed shutdown, including its normal-exhaustion cancellation policy."""

        def shutdown(self, wait=True, *, cancel_futures=False) -> None:
            assert wait and not cancel_futures
            super().shutdown(wait=wait, cancel_futures=cancel_futures)
            events.append("shutdown")

    def held_worker(*args, **kwargs):
        real_worker(*args, **kwargs)
        assert release_workers.wait(HANG_GUARD_SEC)
        events.append("worker-exit")

    def progress(index, total, *_args):
        if index == total:
            all_progress.set()

    def manifest(*_args, **_kwargs):
        assert events == ["worker-exit", "worker-exit", "shutdown"]
        events.append("manifest")
        return 0

    _configure_main(monkeypatch, session, views, tmp_path / "oracle")
    monkeypatch.setattr(oracle, "ThreadPoolExecutor", ObservedExecutor)
    monkeypatch.setattr(oracle, "_capture_worker", held_worker)
    monkeypatch.setattr(oracle, "capture_view", lambda _session, view, *_args, **_kwargs: _record(view))
    monkeypatch.setattr(oracle, "log_progress", progress)
    monkeypatch.setattr(oracle, "write_manifest", manifest)

    with ThreadPoolExecutor(max_workers=1) as harness:
        run = harness.submit(oracle.main)
        try:
            assert all_progress.wait(HANG_GUARD_SEC)
            assert not events
            assert session.signouts == 0
        finally:
            release_workers.set()
        assert run.result(timeout=HANG_GUARD_SEC) == 0
    assert events == ["worker-exit", "worker-exit", "shutdown", "manifest"]
    assert session.signouts == 1


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


@pytest.mark.parametrize("refresh_owner", ["one", "two"])
def test_two_workers_losing_one_token_generation_reauthenticate_once(monkeypatch, refresh_owner):
    """Either worker may own the refresh; both exports must count their own admitted recovery."""
    signin_count = 0
    count_lock = threading.Lock()
    old_token_requests = threading.Barrier(2)
    refresh_completed = threading.Event()
    requests = []
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
        with count_lock:
            requests.append((req.full_url.rsplit("/", 3)[-2], token))
        if token == "old-session-token":
            old_token_requests.wait(timeout=2)
            if f"/{refresh_owner}/" not in req.full_url:
                assert refresh_completed.wait(HANG_GUARD_SEC)
            return 401, SESSION_LOST, {}
        if token == "new-session-token":
            with count_lock:
                new_token_requests.append(req.full_url)
            return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}
        raise AssertionError(f"unexpected authentication token: {token!r}")

    monkeypatch.setattr(oracle, "_request", transport)
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=3, budget_sec=60))
    session.sign_in()
    real_refresh = session._reauthenticate_if_current

    def observed_refresh(generation):
        replaced = real_refresh(generation)
        if replaced:
            refresh_completed.set()
        return replaced

    monkeypatch.setattr(session, "_reauthenticate_if_current", observed_refresh)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(session.export, ("/views/one/data", "/views/two/data")))

    stats = [result[2] for result in results]
    assert signin_count == 2, "one initial sign-in plus exactly one shared reauthentication"
    assert session.reauth_count == 1
    assert [item["reauths"] for item in stats] == [1, 1]
    assert [item["retries"] for item in stats] == [1, 1]
    assert [item["retry_reasons"] for item in stats] == [["session_lost"], ["session_lost"]]
    assert session.retry_count == 0
    assert sorted(requests) == [
        ("one", "new-session-token"),
        ("one", "old-session-token"),
        ("two", "new-session-token"),
        ("two", "old-session-token"),
    ]
    assert len(new_token_requests) == 2


def test_a_multi_generation_jump_is_one_local_recovery(monkeypatch):
    """A late old-token response reuses two replacements but admits just one recovery of its own."""
    both_old = threading.Barrier(2)
    two_replacements_ready = threading.Event()
    signins = []
    requests = {"leader": [], "late": []}

    def transport(req, **_kwargs):
        if req.full_url.endswith("/auth/signin"):
            token = f"generation-{len(signins) + 1}-token"
            signins.append(token)
            return 200, json.dumps({"credentials": {"token": token, "site": {"id": "site-id"}}}).encode(), {}
        name = req.full_url.rsplit("/", 3)[-2]
        token = req.get_header("X-tableau-auth")
        requests[name].append(token)
        if token == "generation-1-token":
            both_old.wait(timeout=HANG_GUARD_SEC)
            if name == "late":
                assert two_replacements_ready.wait(HANG_GUARD_SEC)
            return 401, SESSION_LOST, {}
        if name == "leader" and token == "generation-2-token":
            return 401, SESSION_LOST, {}
        assert token == "generation-3-token"
        two_replacements_ready.set()
        return 200, b"value\n1\n", {"Content-Type": "text/csv"}

    monkeypatch.setattr(oracle, "_request", transport)
    session = _session()
    session.sign_in()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(session.export, ("/views/leader/data", "/views/late/data")))

    stats = [result[2] for result in results]
    assert [item["reauths"] for item in stats] == [2, 1]
    assert [item["retries"] for item in stats] == [2, 1]
    assert [item["retry_reasons"] for item in stats] == [["session_lost", "session_lost"], ["session_lost"]]
    assert session.reauth_count == 2
    assert session.retry_count == 0
    assert signins == ["generation-1-token", "generation-2-token", "generation-3-token"]
    assert requests == {
        "leader": ["generation-1-token", "generation-2-token", "generation-3-token"],
        "late": ["generation-1-token", "generation-3-token"],
    }


def test_reusing_replacements_still_exhausts_the_local_recovery_cap(monkeypatch):  # pylint: disable=too-many-locals
    """After two reused replacements the third 401002 must refuse, not buy a third recovery as owner."""
    assert oracle.MAX_REAUTH_PER_VIEW == 2
    stale_requests = [threading.Event(), threading.Event()]
    replacements = [threading.Event(), threading.Event()]
    signins = []
    requests = {"owner": [], "reuser": []}
    recoveries = {"owner": [], "reuser": []}
    caller = threading.local()

    def transport(req, **_kwargs):
        if req.full_url.endswith("/auth/signin"):
            token = f"generation-{len(signins) + 1}-token"
            signins.append(token)
            return 200, json.dumps({"credentials": {"token": token, "site": {"id": "site-id"}}}).encode(), {}
        name = caller.name
        token = req.get_header("X-tableau-auth")
        attempt = len(requests[name])
        requests[name].append(token)
        if attempt < 2:
            if name == "reuser":
                stale_requests[attempt].set()
                assert replacements[attempt].wait(HANG_GUARD_SEC)
            else:
                assert stale_requests[attempt].wait(HANG_GUARD_SEC)
            return 401, SESSION_LOST, {}
        if name == "reuser" and attempt == 2:
            return 401, SESSION_LOST, {}
        return 200, b"value\n1\n", {"Content-Type": "text/csv"}

    monkeypatch.setattr(oracle, "_request", transport)
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=4, budget_sec=60))
    session.sign_in()
    real_refresh = session._reauthenticate_if_current

    def observed_refresh(generation):
        recoveries[caller.name].append(generation)
        replaced = real_refresh(generation)
        if replaced and generation <= 2:
            replacements[generation - 1].set()
        return replaced

    def export(name):
        caller.name = name
        return session.export(f"/views/{name}/data")

    monkeypatch.setattr(session, "_reauthenticate_if_current", observed_refresh)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(export, "owner")
        reuser = pool.submit(export, "reuser")
        owner_stats = owner.result(timeout=HANG_GUARD_SEC)[2]
        with pytest.raises(oracle.ExportFailed) as excinfo:
            reuser.result(timeout=HANG_GUARD_SEC)

    assert excinfo.value.kind == "session_lost"
    assert owner_stats["reauths"] == 2
    assert owner_stats["retry_reasons"] == ["session_lost", "session_lost"]
    assert recoveries == {"owner": [1, 2], "reuser": [1, 2]}
    assert requests == {
        "owner": ["generation-1-token", "generation-2-token", "generation-3-token"],
        "reuser": ["generation-1-token", "generation-2-token", "generation-3-token"],
    }
    assert signins == ["generation-1-token", "generation-2-token", "generation-3-token"]
    assert session.reauth_count == 2
    assert session.retry_count == 0


@pytest.mark.parametrize("already_replaced", [False, True])
def test_final_attempt_neither_refreshes_nor_reuses_a_generation(monkeypatch, already_replaced):
    """Even a reusable newer generation cannot admit recovery when no request attempt remains."""
    session = _session()
    requests = []

    def transport(req, **_kwargs):
        requests.append(req.full_url)
        if already_replaced:
            session._publish_auth("replacement-token", "site-id")
        return 401, SESSION_LOST, {}

    def no_recovery(_generation):
        pytest.fail("the final attempt cannot admit refresh or reuse")

    monkeypatch.setattr(oracle, "_request", transport)
    monkeypatch.setattr(session, "_reauthenticate_if_current", no_recovery)
    with pytest.raises(oracle.ExportFailed, match="HTTP 401") as excinfo:
        session.export("/views/one/data", retry=oracle.RetryPolicy(max_attempts=1))
    assert excinfo.value.kind == "session_lost"
    assert "the last this policy allows" in excinfo.value.detail
    assert len(requests) == 1
    assert session.reauth_count == session.retry_count == 0


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


@pytest.mark.parametrize("route", ["snapshot", "direct", "export-admit", "export-refuse"])
def test_post_auth_admission_credits_measured_external_wait_once(  # pylint: disable=too-many-locals,too-many-statements
    monkeypatch, route
):
    """A cooldown published during auth wins; only its actual wait, not auth time, buys retry budget."""
    clock = _VirtualClock()
    auth_waiting = threading.Event()
    caller = threading.local()
    auth_acquisitions = 0
    admissions = []
    transports = []
    pool_sleeps = []
    monkeypatch.setattr(oracle.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(oracle.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(oracle.time, "sleep", clock.sleep)
    monkeypatch.setattr(oracle, "backoff_delay", lambda *_args, **_kwargs: 1.0)
    budget = 4.75 if route == "export-refuse" else 5.0
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=2, budget_sec=budget))
    session.token, session.site_id = "initial-session-token", "site-id"

    class ObservedAuthLock:
        """Signal attempted auth acquisition before blocking behind the publishing thread."""

        def __init__(self) -> None:
            self.lock = threading.RLock()

        def __enter__(self):
            nonlocal auth_acquisitions
            if getattr(caller, "target", False):
                auth_acquisitions += 1
                auth_waiting.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_args) -> None:
            self.lock.release()

    def pool_sleep(seconds):
        pool_sleeps.append(seconds)
        clock.sleep(seconds)
        clock.advance(0.25)  # Credit measured sleep, including overshoot, rather than advertised delay.

    real_admission = session._wait_for_pool_cooldown

    def observed_admission(deadline=None, owner=None):
        started = clock.monotonic()
        credit = real_admission(deadline, owner)
        admissions.append((started, deadline, credit))
        return credit

    def transport(req, *, deadline, **_kwargs):
        if not transports:
            assert auth_acquisitions == 1, "the export snapshot must not reacquire auth in the request adapter"
        transports.append((clock.monotonic(), deadline, req.get_header("X-tableau-auth")))
        if route.startswith("export-") and len(transports) == 1:
            clock.advance(1.0)
            return 503, b"gateway", {}
        return 200, b"value\n1\n", {"Content-Type": "text/csv"}

    def request():
        caller.target = True
        if route == "direct":
            return session._request("GET", "/views/one/data", deadline=20.0)
        if route == "snapshot":
            return session._request_with_generation("/views/one/data", deadline=20.0, export_id=object())
        return session.export("/views/one/data", hard_deadline=20.0)

    monkeypatch.setattr(session, "_auth_lock", ObservedAuthLock())
    monkeypatch.setattr(session, "_sleep", pool_sleep)
    monkeypatch.setattr(session, "_wait_for_pool_cooldown", observed_admission)
    monkeypatch.setattr(oracle, "_request", transport)
    session._request_context.export_id = object()
    session._observe_rate_limit(429, {"Retry-After": "5"})
    with ThreadPoolExecutor(max_workers=1) as harness:
        with session._auth_lock:
            run = harness.submit(request)
            assert auth_waiting.wait(HANG_GUARD_SEC)
            clock.advance(3.0)
            session._publish_auth("post-auth-token", "site-id")
            session._observe_rate_limit(429, {"Retry-After": "7"})
        del session._request_context.export_id
        if route == "export-refuse":
            with pytest.raises(oracle.ExportFailed, match="retry budget exhausted"):
                run.result(timeout=HANG_GUARD_SEC)
        else:
            result = run.result(timeout=HANG_GUARD_SEC)
            if route == "snapshot":
                assert result[3:5] == (1, 7.25)
            elif route == "export-admit":
                assert result[2]["retries"] == 1

    expected_credit = 0.0 if route == "direct" else 7.25
    expected_admissions = [(3.0, 20.0, expected_credit)]
    expected_transports = [(10.25, 20.0, "post-auth-token")]
    if route == "export-admit":
        expected_admissions.append((12.25, 20.0, 0.0))
        expected_transports.append((12.25, 20.0, "post-auth-token"))
    assert admissions == expected_admissions, "each request must have exactly one authoritative post-auth admission"
    assert transports == expected_transports, "external credit must never move the absolute hard deadline"
    assert pool_sleeps == [7.0]
    assert clock.sleeps == ([7.0, 1.0] if route == "export-admit" else [7.0])
    assert session.retry_count == (1 if route == "export-admit" else 0)


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


def _retry_after_value(kind: str, epoch: float) -> str:
    """Equivalent seven-second Retry-After in either standards-valid wire form."""
    return "7" if kind == "delay-seconds" else formatdate(epoch + 7, usegmt=True)


@pytest.mark.parametrize("kind", ["delay-seconds", "http-date"])
def test_numeric_and_http_date_retry_after_are_equally_refused_by_an_insufficient_budget(monkeypatch, kind):
    """Both seven-second forms are terminal when only 2.5 seconds remain."""
    clock = _VirtualClock()
    epoch = 1_800_000_000.0
    calls = 0
    value = _retry_after_value(kind, epoch)

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 429, b"slow down", {"Retry-After": value}
        return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}

    monkeypatch.setattr(oracle, "_request", transport)
    monkeypatch.setattr(oracle.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(oracle.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(oracle.time, "sleep", clock.sleep)
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=2, budget_sec=2.5))
    session.token, session.site_id = "initial-session-token", "site-id"
    session._wall_time = lambda: epoch + clock.monotonic()

    with pytest.raises(oracle.ExportFailed, match="retry budget exhausted"):
        session.export("/views/one/data")

    assert calls == 1
    assert not clock.sleeps


@pytest.mark.parametrize("kind", ["delay-seconds", "http-date", "huge", "zero", "invalid"])
def test_retry_after_is_parsed_once_and_uses_one_wait(  # pylint: disable=too-many-locals
    monkeypatch, kind
):
    """One parser result controls admission and waiting, including exact zero and missing-delay fallback."""
    clock = _VirtualClock()
    epoch = 1_800_000_000.0
    calls = 0
    value = {"huge": "9" * 100_000, "zero": "0", "invalid": "7suffix"}.get(kind, _retry_after_value(kind, epoch))
    pool_sleeps: list[float] = []
    export_sleeps: list[float] = []
    fallback_calls = []
    parser_calls = 0

    def fallback(attempt):
        fallback_calls.append(attempt)
        return 1.0

    def pool_sleep(seconds: float) -> None:
        pool_sleeps.append(seconds)
        clock.advance(seconds)

    def export_sleep(seconds: float) -> None:
        export_sleeps.append(seconds)
        clock.advance(seconds)

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 429, b"slow down", {"Retry-After": value}
        return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}

    monkeypatch.setattr(oracle, "_request", transport)
    monkeypatch.setattr(oracle.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(oracle.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(oracle.time, "sleep", export_sleep)
    monkeypatch.setattr(oracle, "backoff_delay", fallback)
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=2, budget_sec=31.0))
    session.token, session.site_id = "initial-session-token", "site-id"
    session._sleep = pool_sleep
    session._wall_time = lambda: epoch + clock.monotonic()
    strict_parser = session._retry_after_delay

    def counted_parser(headers):
        nonlocal parser_calls
        if oracle.header_value(headers, "Retry-After") is not None:
            parser_calls += 1
        return strict_parser(headers)

    session._retry_after_delay = counted_parser

    payload, _, stats = session.export("/views/one/data")

    assert payload == b"value\n1\n"
    assert calls == 2
    assert stats["retries"] == 1
    delay = {"huge": 30.0, "zero": 0.0, "invalid": 1.0}.get(kind, 7.0)
    shared = kind not in {"zero", "invalid"}
    assert pool_sleeps == ([delay] if shared else [])
    assert export_sleeps == ([] if shared else [delay])
    assert fallback_calls == ([1] if kind == "invalid" else [])
    assert clock.monotonic() == delay
    assert parser_calls == 1


@pytest.mark.parametrize("value", ["1.5", "+7", "1e2"])
def test_malformed_delay_seconds_use_export_fallback_without_shared_cooldown(monkeypatch, value):
    """Malformed numeric forms retain exponential fallback and never establish pool admission."""
    clock = _VirtualClock()
    calls = 0
    pool_sleeps: list[float] = []
    export_sleeps: list[float] = []

    def pool_sleep(seconds: float) -> None:
        pool_sleeps.append(seconds)
        clock.advance(seconds)

    def export_sleep(seconds: float) -> None:
        export_sleeps.append(seconds)
        clock.advance(seconds)

    def transport(  # pylint: disable=unused-argument
        req, *, timeout, redactor, deadline=None
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 429, b"slow down", {"Retry-After": value}
        return 200, b"value\n1\n", {"Content-Type": "text/csv", "Content-Length": "8"}

    monkeypatch.setattr(oracle, "_request", transport)
    monkeypatch.setattr(oracle.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(oracle.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(oracle.time, "sleep", export_sleep)
    monkeypatch.setattr(oracle, "backoff_delay", lambda *_args, **_kwargs: 1.0)
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=2, budget_sec=2.5))
    session.token, session.site_id = "initial-session-token", "site-id"
    session._sleep = pool_sleep

    payload, _, stats = session.export("/views/one/data")

    assert payload == b"value\n1\n"
    assert calls == 2
    assert stats["retries"] == 1
    assert not pool_sleeps
    assert export_sleeps == [1.0]


def test_valid_integer_retry_after_keeps_the_existing_cap():
    """A valid large delay-seconds value is still capped at the existing backoff ceiling."""
    session = _session()
    assert session._retry_after_delay({"Retry-After": "999"}) == oracle.BACKOFF_CAP_SEC


@pytest.mark.parametrize("digits", [308, 309, 310, 4300, 4301, 100_000])
def test_arbitrary_ascii_integer_lengths_are_capped_before_conversion(digits):
    """Valid huge tokens reach the cap without float overflow or Python's integer digit limit."""
    session = _session()
    assert session._retry_after_delay({"Retry-After": "9" * digits}) == oracle.BACKOFF_CAP_SEC


@pytest.mark.parametrize("zeros", [4300, 4301, 100_000])
@pytest.mark.parametrize(("suffix", "expected"), [("0", 0.0), ("7", 7.0)])
def test_leading_zeros_preserve_zero_and_small_delays(zeros, suffix, expected):
    """Lexical bounds apply after zero stripping, not to the length of the original token."""
    result = _session()._retry_after_delay({"Retry-After": "0" * zeros + suffix})
    assert result == expected
    assert isinstance(result, float)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0.0),
        (" 000\t", 0.0),
        (" \t0007\r\n", 7.0),
        ("9", 9.0),
        ("29", 29.0),
        ("30", 30.0),
        ("31", 30.0),
        ("100", 30.0),
        ("", None),
        (" \t", None),
        ("7suffix", None),
        ("7\nsuffix", None),
        ("+7", None),
        ("-0", None),
        ("1.5", None),
        ("1e2", None),
        ("７", None),
        ("٧", None),
        ("7٧", None),
        ("⁷", None),
    ],
)
def test_delay_seconds_require_the_entire_trimmed_ascii_token(value, expected):
    """Only a whole trimmed ASCII integer is numeric; zero is distinct from absent or invalid."""
    assert _session()._retry_after_delay({"Retry-After": value}) == expected
    assert _session()._retry_after_delay({}) is None


@pytest.mark.parametrize("form", ["all-zero", "leading-zero-seven", "above-cap"])
def test_numeric_conversion_never_receives_the_arbitrary_original_token(monkeypatch, form):
    """A conversion guard checks the boundary independently of the interpreter's configured digit limit."""
    session = _session()
    values = {
        "all-zero": ("0" * 100_000, 0.0),
        "leading-zero-seven": ("0" * 100_000 + "7", 7.0),
        "above-cap": ("9" * 100_000, 30.0),
    }
    value, expected = values[form]
    integer_limit = sys.get_int_max_str_digits()
    real_int, real_float = int, float
    converted = []

    def check_token(token):
        if isinstance(token, str):
            assert len(token) <= 2 and token == token.lstrip("0"), "only bounded significant digits may be converted"
            converted.append(token)

    def bounded_int(token):
        check_token(token)
        return real_int(token)

    def bounded_float(token):
        check_token(token)
        return real_float(token)

    def no_limit_change(*_args):
        pytest.fail("Retry-After parsing must not alter Python's integer digit limit")

    monkeypatch.setattr(oracle, "int", bounded_int, raising=False)
    monkeypatch.setattr(oracle, "float", bounded_float, raising=False)
    monkeypatch.setattr(sys, "set_int_max_str_digits", no_limit_change)
    assert session._retry_after_delay({"Retry-After": value}) == expected
    assert converted == (["7"] if form == "leading-zero-seven" else [])
    assert sys.get_int_max_str_digits() == integer_limit


def test_zero_cannot_shorten_or_take_ownership_of_an_active_cooldown():
    """An explicit zero delay retains the existing later deadline and its credit owner."""
    clock = _VirtualClock()
    session = _session()
    session._monotonic = clock.monotonic
    session._sleep = clock.sleep
    first_owner = object()
    session._request_context.export_id = first_owner
    assert session._observe_rate_limit(429, {"Retry-After": "7"}) == 7.0
    clock.advance(1)
    second_owner = object()
    session._request_context.export_id = second_owner
    assert session._observe_rate_limit(429, {"Retry-After": "0"}) == 0.0
    assert session._cooldown_until == 7.0
    assert session._cooldown_owner is first_owner
    assert session._wait_for_pool_cooldown(owner=second_owner) == 6.0
    assert clock.sleeps == [6.0]


@pytest.mark.parametrize("seconds", [7, 29, 30, 31, 100])
def test_http_date_and_numeric_cap_parity(seconds):
    """HTTP-date keeps the same positive-delay capping as delay-seconds."""
    session = _session()
    epoch = 1_800_000_000.0
    session._wall_time = lambda: epoch
    expected = min(float(seconds), oracle.BACKOFF_CAP_SEC)
    assert session._retry_after_delay({"Retry-After": str(seconds)}) == expected
    assert session._retry_after_delay({"Retry-After": formatdate(epoch + seconds, usegmt=True)}) == expected
    assert session._retry_after_delay({"Retry-After": formatdate(epoch - 1, usegmt=True)}) is None


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (429, {}),
        (429, {"Retry-After": "not-a-number"}),
        (429, {"Retry-After": "1.5"}),
        (429, {"Retry-After": "+7"}),
        (429, {"Retry-After": "1e2"}),
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
