"""Regression contract: the per-export retry budget vs the per-request socket timeout (issue #197).

``export`` computes its retry deadline as ``monotonic() + budget_sec`` BEFORE the first attempt. A
socket timeout takes the whole ``REST_TIMEOUT_SEC`` (180s) to manifest and is classified *transient*
-- ``_request`` returns ``NETWORK_ERROR_STATUS`` instead of raising -- so it is eligible for retry,
yet the field-reported default budget (120s) was already spent by the time that first failure
returned. Result: the single most common transient failure on a slow / proxied / VPN link was
STRUCTURALLY unretryable at any ``--max-attempts``.

The budget is a RETRY-ADMISSION DEADLINE, not a hard wall-clock cap: it gates whether the NEXT retry
is admitted, so an already-started attempt (or a nested re-auth) may finish past it. The floor below
which a full-timeout failure can no longer be re-admitted is therefore ONE timeout plus the first
backoff (``RETRY_ADMISSION_FLOOR_SEC`` = ``REST_TIMEOUT_SEC + BACKOFF_BASE_SEC``), NOT 2x: a budget
between 1x and 2x retries a slow failure fine, and 2x is not even enough to fit two COMPLETE
full-timeout attempts once backoff is counted. ``build_retry_policy`` therefore WARNS below the floor
rather than clamping OR rejecting -- a sub-floor budget is a deliberate, useful choice for FAST-failing
transients (the sibling suite's ``test_retry_budget_stops_a_slow_failure_before_max_attempts`` passes
``budget_sec=10.0`` to prove exactly that), so clamping would silently defeat it and rejecting would
forbid it.

Every assertion here pins a RELATIONSHIP (the floor is one timeout plus a backoff; a full-timeout
failure is still retried), never a literal like ``== 360`` that would keep passing if someone raised
``REST_TIMEOUT_SEC`` later and silently re-opened the bug.

Two kinds of harness live here, on purpose:
  * ``TimedSession`` overrides ``_request`` to model wall-clock without a socket -- it drives the
    deadline arithmetic in ``export`` / ``sign_in``.
  * The Finding-1 tests drive the REAL ``_request`` with only ``urlopen`` stubbed, because a network
    failure WHILE READING an HTTP error body is raised inside ``_request`` itself, and ``TimedSession``
    (which replaces ``_request``) structurally cannot see it.

No test here touches the network or sleeps for real: a virtual clock stands in for the module's time
source, and each scripted attempt advances that clock by the wall-clock it would have consumed.
"""

from __future__ import annotations

import http.client
import json
import logging
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position


class VirtualClock:
    """A ``monotonic`` / ``perf_counter`` / ``sleep`` stand-in whose time moves only when we say so.

    ``export`` reads time three ways -- ``monotonic()`` (the deadline), ``perf_counter()`` (elapsed)
    and ``sleep()`` (backoff) -- so all three share one virtual ``t``. ``advance`` is how a scripted
    request consumes wall-clock without a real socket or a real wait, keeping the suite instant.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def perf_counter(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _creds() -> oracle.SiteCredentials:
    return oracle.SiteCredentials(
        base="https://example.online.tableau.com",
        site="site",
        pat_name="name",
        pat_secret="secret",
        version="3.29",
    )


class TimedSession(oracle.TableauSession):
    """Scripted session whose HTTP layer also consumes virtual wall-clock.

    Each scripted response is ``(duration_sec, status, body, headers)``: ``duration_sec`` is how long
    that attempt blocks before returning (a socket timeout blocks for the whole ``REST_TIMEOUT_SEC``),
    charged against the shared virtual clock exactly as a real slow request would be. Unlike the
    sibling suite's ``FakeSession`` this deliberately does NOT stub ``sign_in`` -- one test exercises
    the real sign-in retry loop -- and it does NOT model instant requests, which is precisely the gap
    that hid this defect.
    """

    def __init__(self, clock: VirtualClock, responses, retry=None) -> None:
        super().__init__(_creds(), retry)
        self._clock = clock
        self.responses = list(responses)
        self.calls: list[str] = []
        self.token, self.site_id = "tok", "sid"

    def _request(self, method, path, *, body=None, accept=None, authed=True):  # noqa: ARG002
        self.calls.append(path)
        duration, status, payload, headers = self.responses.pop(0)
        self._clock.advance(duration)
        return status, payload.encode() if isinstance(payload, str) else payload, headers


@pytest.fixture(name="clock")
def _clock(monkeypatch) -> VirtualClock:
    """Install a virtual clock as the module's time source for the duration of a test."""
    virtual = VirtualClock()
    monkeypatch.setattr(oracle, "time", virtual)
    return virtual


# --------------------------------------------------------- Finding-1 helpers: drive the REAL _request


class _ErrorBodyReadFails(urllib.error.HTTPError):
    """An ``HTTPError`` whose STATUS LINE arrived but whose body read then fails mid-stream.

    This is the exact shape Finding 1 is about: ``urlopen`` raises ``HTTPError`` for a 5xx, and the
    subsequent ``exc.read()`` -- itself a socket read -- times out (``TimeoutError`` is an ``OSError``)
    or truncates (``http.client.IncompleteRead``). The failure is raised INSIDE ``_request``'s
    ``except HTTPError`` handler, where a sibling ``except`` of the same ``try`` cannot catch it.
    """

    def __init__(self, code: int, read_error: Exception) -> None:
        super().__init__("https://example.online.tableau.com/api", code, "err", {}, None)
        self._read_error = read_error

    def read(self, *_args, **_kwargs):
        raise self._read_error


class _FakeResponse:
    """A minimal ``urlopen`` success result usable as a context manager (``with urlopen(...) as r``)."""

    def __init__(self, status: int, body: bytes, headers: dict) -> None:
        self.status = status
        self._body = body
        self.headers = headers

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


# --------------------------------------------------------------------------- the constants


def test_default_budget_exceeds_one_request_timeout():
    """The core invariant, as RELATIONSHIPS not literals.

    A retry can only be decided AFTER an attempt returns, and one attempt (a socket timeout) blocks
    for the full ``REST_TIMEOUT_SEC``. So both the admission floor and the default budget must exceed
    one request timeout, and the default must sit at or above the floor. Asserting the relationships
    keeps them valid if ``REST_TIMEOUT_SEC`` is raised; ``== 360`` would not.
    """
    assert oracle.RETRY_ADMISSION_FLOOR_SEC > oracle.REST_TIMEOUT_SEC
    assert oracle.DEFAULT_RETRY_BUDGET_SEC > oracle.REST_TIMEOUT_SEC
    assert oracle.DEFAULT_RETRY_BUDGET_SEC >= oracle.RETRY_ADMISSION_FLOOR_SEC


def test_admission_floor_is_one_timeout_plus_backoff_not_two():
    """The floor is ONE timeout plus the first backoff -- the true point below which a full-timeout
    failure can no longer be re-admitted -- NOT the old ``2 * REST_TIMEOUT_SEC``.

    The upper bound ``< 2 * REST_TIMEOUT_SEC`` is the anti-regression: it fails if anyone restores the
    2x model, which warned against budgets (e.g. 300s) that in fact retry a slow failure perfectly and
    promised room for two full attempts that ``2 * 180`` + backoff cannot actually fit.
    """
    assert oracle.RETRY_ADMISSION_FLOOR_SEC == oracle.REST_TIMEOUT_SEC + oracle.BACKOFF_BASE_SEC
    assert oracle.RETRY_ADMISSION_FLOOR_SEC < 2 * oracle.REST_TIMEOUT_SEC


# --------------------------------------------------------------------------- the behaviour


def test_a_full_timeout_length_failure_is_still_retried(clock):
    """THE regression, under the DEFAULT policy the field report ran with.

    Attempt 1 is a socket timeout: it blocks for the whole ``REST_TIMEOUT_SEC`` and comes back as
    ``NETWORK_ERROR_STATUS`` (transient). Pre-fix, the 120s deadline had already expired by the time
    this returned, so ``export`` raised with ZERO retries -- matching the customer's manifest, which
    recorded no second attempt. Post-fix the default budget exceeds one request timeout, so the slow
    failure is retried and the next attempt succeeds.
    """
    session = TimedSession(
        clock,
        [
            (float(oracle.REST_TIMEOUT_SEC), oracle.NETWORK_ERROR_STATUS, "RemoteDisconnected", {}),
            (0.5, 200, "a,b\n1,2\n", {}),
        ],
        retry=oracle.RetryPolicy(),
    )

    payload, _, stats = session.export("/views/x/data")

    assert payload == b"a,b\n1,2\n"
    assert stats["retries"] >= 1, "a full-timeout-length transient failure must be retried, not abandoned"
    assert len(session.calls) == 2


def test_a_fast_transient_still_retries_and_is_recorded(clock):
    """Control: keeping the default budget must not disturb ordinary fast-failure retries.

    Two quick gateway 503s then success: the export should record two retries. This also proves the
    virtual-clock harness itself is sound -- a healed retry returns a populated ``retries`` count
    rather than raising.
    """
    session = TimedSession(
        clock,
        [
            (2.0, 503, "gateway", {}),
            (2.0, 503, "gateway", {}),
            (0.5, 200, "a\n1\n", {}),
        ],
        retry=oracle.RetryPolicy(),
    )

    payload, _, stats = session.export("/views/x/data")

    assert payload == b"a\n1\n"
    assert stats["retries"] == 2


def test_a_budget_between_one_and_two_timeouts_still_retries_a_slow_failure(clock):
    """A budget ABOVE the floor but BELOW the old 2x model must still retry a full-timeout failure.

    This is the measurement that proved the old warning false in one direction: with a 270s budget
    (1.5x timeout) a 180s socket timeout is followed by a successful retry. The admission check runs
    AFTER the first attempt returns at t=180, still inside the 270 deadline, so the retry is admitted.
    """
    session = TimedSession(
        clock,
        [
            (float(oracle.REST_TIMEOUT_SEC), oracle.NETWORK_ERROR_STATUS, "timeout", {}),
            (0.5, 200, "a\n1\n", {}),
        ],
        retry=oracle.RetryPolicy(budget_sec=1.5 * oracle.REST_TIMEOUT_SEC),
    )

    payload, _, stats = session.export("/views/x/data")

    assert payload == b"a\n1\n"
    assert stats["retries"] == 1
    assert len(session.calls) == 2


def test_two_full_timeout_failures_stop_when_the_deadline_is_spent_not_at_max_attempts(clock):
    """Honest admission-deadline behaviour: under the default budget, two full-timeout failures spend
    the deadline, so the export stops after two calls even though ``max_attempts`` is 5.

    The deadline, not the attempt count, is the binding limit here -- which is the whole point of
    having a budget. (The reviewer cited this as evidence the old 2x floor was wrong; under the
    admission-deadline model it is simply correct: the budget was legitimately exhausted.)
    """
    session = TimedSession(
        clock,
        [
            (float(oracle.REST_TIMEOUT_SEC), oracle.NETWORK_ERROR_STATUS, "timeout", {}),
            (float(oracle.REST_TIMEOUT_SEC), oracle.NETWORK_ERROR_STATUS, "timeout", {}),
            (0.5, 200, "unreached\n", {}),
        ],
        retry=oracle.RetryPolicy(max_attempts=5),
    )

    with pytest.raises(oracle.ExportFailed):
        session.export("/views/x/data")

    assert len(session.calls) == 2, "the deadline, not max_attempts, bounds full-timeout failures"


def test_sign_in_retries_a_full_timeout_transient_because_it_has_no_deadline(clock):
    """``sign_in`` is NOT affected by the deadline-vs-timeout bug, because it has no deadline.

    Its retry loop is bounded by ``max_attempts`` alone -- it never computes ``monotonic() + budget``
    -- so there is no deadline-vs-timeout arithmetic to get wrong. A full-timeout transient is
    retried even with a deliberately tiny ``budget_sec``, proving the budget is irrelevant here.
    """
    signin_ok = json.dumps({"credentials": {"token": "t", "site": {"id": "s"}}})
    session = TimedSession(
        clock,
        [
            (float(oracle.REST_TIMEOUT_SEC), oracle.NETWORK_ERROR_STATUS, "timeout", {}),
            (0.5, 200, signin_ok, {}),
        ],
        retry=oracle.RetryPolicy(max_attempts=3, budget_sec=1.0),
    )

    session.sign_in()

    assert session.token == "t"
    assert session.retry_count == 1


# ---------------------------------------------------- Finding 1: the real _request never raises


def test_request_keeps_the_status_when_the_error_body_read_times_out(monkeypatch):
    """A 503 whose error-body read times out must come back as a retryable 503, never an exception.

    ``_request`` documents "never raises for a network failure". The body read is itself a socket
    read; when it times out (``TimeoutError`` is an ``OSError``) the exception is raised inside the
    ``except HTTPError`` handler, which a sibling ``except`` cannot catch. The status line already
    arrived, so the authoritative 503 is preserved (and stays retry-eligible via ``TRANSIENT_STATUSES``);
    the read error is reported in the body. ``TimedSession`` replaces ``_request``, so only this
    real-``urlopen`` path can observe the guard.
    """
    session = oracle.TableauSession(_creds())
    session.token = "tok"

    def _raise(_req, timeout=None):
        raise _ErrorBodyReadFails(503, TimeoutError("read timed out"))

    monkeypatch.setattr(oracle.urllib.request, "urlopen", _raise)

    try:
        status, body, _headers = session._request("GET", "/x")
    except BaseException as exc:  # the whole contract: it must NOT escape
        pytest.fail(f"_request raised {type(exc).__name__} instead of returning a transient result: {exc}")

    assert status == 503, "the status line arrived, so the 503 must be preserved for the retry loop"
    assert status in oracle.TRANSIENT_STATUSES
    assert b"TimeoutError" in body


def test_request_survives_a_truncated_error_body(monkeypatch):
    """The ``IncompleteRead`` sibling of the above: a truncated 504 body must not escape either."""
    session = oracle.TableauSession(_creds())
    session.token = "tok"

    def _raise(_req, timeout=None):
        raise _ErrorBodyReadFails(504, http.client.IncompleteRead(b"partial"))

    monkeypatch.setattr(oracle.urllib.request, "urlopen", _raise)

    try:
        status, body, _headers = session._request("GET", "/x")
    except BaseException as exc:
        pytest.fail(f"_request raised {type(exc).__name__} instead of returning a transient result: {exc}")

    assert status == 504
    assert b"IncompleteRead" in body


def test_export_recovers_when_the_error_body_read_times_out(clock, monkeypatch):
    """End-to-end through the REAL ``_request``: the class of defect ``TimedSession`` cannot see.

    Attempt 1 is a 503 whose body read times out (raised inside ``_request``); attempt 2 succeeds.
    Because ``TimedSession`` REPLACES ``_request``, only a test that drives the real ``urlopen`` path
    exercises the guard -- exactly the gap the reviewer flagged. ``export`` must treat the first
    result as an ordinary retryable transient and recover, not propagate the read error.
    """
    session = oracle.TableauSession(_creds(), oracle.RetryPolicy(max_attempts=3))
    session.token, session.site_id = "tok", "sid"
    calls = {"n": 0}

    def _fake_urlopen(_req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ErrorBodyReadFails(503, TimeoutError("read timed out"))
        return _FakeResponse(200, b"a,b\n1,2\n", {})

    monkeypatch.setattr(oracle.urllib.request, "urlopen", _fake_urlopen)

    payload, _, stats = session.export("/views/x/data")

    assert payload == b"a,b\n1,2\n"
    assert stats["retries"] == 1
    assert calls["n"] == 2


# ------------------------------------------ Finding 3: nested re-auth may exceed the deadline (by design)


def test_reauth_may_exceed_the_admission_deadline_by_design(clock):
    """Characterisation, not a defect: nested re-auth is bounded by ``MAX_REAUTH_PER_VIEW``, NOT by the
    admission deadline, so a slow sign-in during recovery can carry a view PAST its budget.

    This is deliberate and documented at the ``session_lost`` branch: abandoning a view mid-re-auth
    after a recoverable session loss throws away estate-capture progress for no gain, and the budget
    is an admission deadline (see ``RetryPolicy``), not a hard wall-clock cap. The test pins the
    behaviour so a future change that silently starts enforcing the deadline across re-auth has to
    announce itself here.
    """
    session_lost = json.dumps({"error": {"code": oracle.SESSION_LOST_CODE}})
    signin_ok = json.dumps({"credentials": {"token": "t2", "site": {"id": "s"}}})
    session = TimedSession(
        clock,
        [
            (1.0, 401, session_lost, {}),  # export attempt 1 -> session lost
            (float(oracle.REST_TIMEOUT_SEC), oracle.NETWORK_ERROR_STATUS, "timeout", {}),  # sign_in try 1 (slow)
            (0.5, 200, signin_ok, {}),  # sign_in try 2 ok
            (0.5, 200, "a\n1\n", {}),  # export retry ok
        ],
        retry=oracle.RetryPolicy(budget_sec=float(oracle.REST_TIMEOUT_SEC)),  # 180s, below the re-auth cost
    )

    payload, _, stats = session.export("/views/x/data")

    assert payload == b"a\n1\n"
    assert stats["reauths"] == 1
    assert clock.t > session.retry.budget_sec, "re-auth deliberately ran the view past the admission deadline"


# --------------------------------------------------------------------------- the warning


def test_an_explicit_sub_floor_budget_is_warned_not_silently_accepted(caplog):
    """An explicit sub-floor budget is HONOURED (a user may mean it) but its consequence is surfaced.

    The value is not clamped -- clamping would defeat the sibling suite's tight-budget test -- and not
    rejected -- a small budget is valid for fast transients -- so the only defence against a silent
    footgun is a warning that names the floor, the timeout it is measured against, and the concrete
    consequence. Asserting that SUBSTANCE (not merely the string "retry-budget") is what makes the
    test fail if the message is gutted -- a weaker assertion passed even when the reviewer replaced
    the whole message with "--retry-budget nonsense".
    """
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        policy = oracle.build_retry_policy(max_attempts=4, budget_sec=oracle.REST_TIMEOUT_SEC / 6)

    assert policy.budget_sec == oracle.REST_TIMEOUT_SEC / 6, "an explicit budget must be honoured, not clamped"
    assert len(caplog.messages) == 1
    message = caplog.messages[0]
    assert f"{oracle.REST_TIMEOUT_SEC}s" in message, "must name the request timeout the budget is measured against"
    assert f"{oracle.RETRY_ADMISSION_FLOOR_SEC:.0f}s" in message, "must name the floor the budget fell below"
    assert "will NOT be retried" in message, "must state the concrete consequence"


def test_a_budget_at_the_floor_is_not_warned(caplog):
    """The threshold is the floor itself: a budget AT the floor is coherent, so it is silent."""
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        oracle.build_retry_policy(max_attempts=4, budget_sec=oracle.RETRY_ADMISSION_FLOOR_SEC)

    assert not caplog.messages


def test_a_budget_of_exactly_one_request_timeout_is_warned(caplog):
    """A budget of exactly one request timeout is BELOW the floor (which adds the first backoff), so a
    full-timeout failure still could not be retried -- it must warn. Pins the floor strictly above 1x.
    """
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        oracle.build_retry_policy(max_attempts=4, budget_sec=float(oracle.REST_TIMEOUT_SEC))

    assert len(caplog.messages) == 1


def test_a_budget_between_one_and_two_timeouts_is_not_warned(caplog):
    """A budget above the floor but below the old 2x model must be SILENT: it genuinely retries a slow
    failure (see the behavioural test), so warning against it -- as the old 2x floor did -- was false.
    """
    with caplog.at_level(logging.WARNING, logger="tableau-oracle"):
        oracle.build_retry_policy(max_attempts=4, budget_sec=1.5 * oracle.REST_TIMEOUT_SEC)

    assert not caplog.messages
