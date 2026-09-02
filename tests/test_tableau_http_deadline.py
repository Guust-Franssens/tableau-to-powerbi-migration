"""The transport's END-TO-END deadline, proved against a REAL socket -- because a virtual clock cannot.

⚠️ This file exists because of an instrument failure, not merely a code defect. Review round 2 of #423
found that the salvage ceiling rested on an assumption nobody had tested: **`urllib`'s ``timeout`` is
a socket-OPERATION timeout, not an end-to-end deadline**, so a response that keeps trickling never
times out. Measured against the production ``tableau_http._request`` with a local server sending one
byte every 0.08s: ``timeout=0.1`` returned **HTTP 200 after 0.479s** -- 4.8x its nominal timeout, no
error at any layer.

The part worth remembering is *why it survived a mutation-proven test suite*: the virtual clock that
"proved" the bound models every request as consuming exactly the duration its script declares, so it
**encodes the same false assumption the bound rests on**. The observer shared the defect with the
observed and structurally could not fail. Every test here therefore uses a real socket and real wall
clock; none may be rewritten onto the virtual clock, whatever the speed cost.

⚠️ No live site. The server is a one-request ``ThreadingHTTPServer`` on ``127.0.0.1`` with an
ephemeral port. Nothing here reads ``.env``, holds a credential, or leaves the loopback interface.

These are ``timing`` tests: they assert wall-clock budgets, so a saturated box can fail them (see
``docs/parallel-test-loop.md``). The budgets are deliberately generous multiples of the trickle gap
rather than tight bounds -- what is being pinned is "bounded vs unbounded", not a millisecond.
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from tableau_http import NETWORK_ERROR_STATUS, _request  # noqa: E402  # pylint: disable=wrong-import-position
import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position

pytestmark = pytest.mark.timing

LUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

GAP_SEC = 0.08
BODY_BYTES = 12
SOCKET_TIMEOUT_SEC = 0.1
# The whole trickled body takes BODY_BYTES * GAP_SEC ~= 0.96s, so a deadline well under that must cut
# it short, and "no deadline" must run to completion. Both directions are asserted.
DEADLINE_SEC = 0.3


class _Trickle(BaseHTTPRequestHandler):
    """A slow but never-IDLE body: every gap is under the socket timeout, so no read ever times out.

    This is the shape that matters. A stalled connection is already handled -- the socket timeout
    fires. What was unhandled is a connection making steady, tiny progress forever, which is exactly
    what a struggling VizQL render or a throttling proxy produces.
    """

    protocol_version = "HTTP/1.1"
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(BODY_BYTES))
        self.end_headers()
        for _ in range(BODY_BYTES):
            time.sleep(GAP_SEC)
            try:
                self.wfile.write(b"a")
                self.wfile.flush()
            except OSError:
                return  # the client hung up at its deadline, which is the behaviour under test

    def log_message(self, *args):  # noqa: ARG002
        return


class _TrickleError(_Trickle):
    """The same trickle, but as a 5xx -- `HTTPError.read()` is a separate code path in the transport."""

    status = 503


@pytest.fixture(name="trickling_url")
def _trickling_url():
    yield from _serve(_Trickle)


@pytest.fixture(name="trickling_error_url")
def _trickling_error_url():
    yield from _serve(_TrickleError)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/data"
    finally:
        server.shutdown()
        server.server_close()


def _fetch(url: str, deadline_in: float | None) -> tuple[int, bytes, float]:
    started = time.monotonic()
    deadline = None if deadline_in is None else started + deadline_in
    status, body, _headers = _request(
        urllib.request.Request(url), timeout=SOCKET_TIMEOUT_SEC, redactor=lambda text: text, deadline=deadline
    )
    return status, body, time.monotonic() - started


# ----------------------------------------------------------------- the defect, pinned as a property


def test_a_socket_timeout_does_not_bound_a_trickling_response(trickling_url):
    """⚠️ The premise the salvage ceiling rested on, and it is FALSE.

    Without a deadline the request succeeds far past its nominal timeout. This is not a bug in the
    transport -- it is what a socket timeout means -- and it is pinned as a PROPERTY so that any
    future wall-clock claim built on `timeout` alone fails here first.
    """
    status, body, elapsed = _fetch(trickling_url, deadline_in=None)

    assert status == 200
    assert body == b"a" * BODY_BYTES
    assert elapsed > SOCKET_TIMEOUT_SEC * 3, (
        f"the trickle finished in {elapsed:.3f}s, too close to the {SOCKET_TIMEOUT_SEC}s timeout to "
        f"demonstrate anything -- widen GAP_SEC or BODY_BYTES"
    )


def test_the_deadline_abandons_a_trickling_body(trickling_url):
    """The fix: with a deadline the same response is cut short, near it rather than at the body's end."""
    status, _body, elapsed = _fetch(trickling_url, deadline_in=DEADLINE_SEC)

    assert status == NETWORK_ERROR_STATUS
    assert elapsed < DEADLINE_SEC + SOCKET_TIMEOUT_SEC + GAP_SEC * 4, (
        f"the deadline did not bound the read: {elapsed:.3f}s against a {DEADLINE_SEC}s deadline"
    )
    assert elapsed < BODY_BYTES * GAP_SEC, "the whole body arrived, so nothing was abandoned"


def test_an_abandoned_body_is_a_transient_failure_never_a_partial_success(trickling_url):
    """⚠️ A truncated CSV reported as a 200 would manufacture exactly the false evidence this whole
    capture exists to prevent -- worse than the unbounded read, because it is silent. The abandoned
    read must surface as `NETWORK_ERROR_STATUS`, which the retry classifier treats as transient."""
    status, body, _elapsed = _fetch(trickling_url, deadline_in=DEADLINE_SEC)

    assert status == NETWORK_ERROR_STATUS
    assert b"a" * BODY_BYTES not in body, "the partial body must not be returned as though complete"
    assert b"TimeoutError" in body


def test_an_error_body_is_bounded_too(trickling_error_url):
    """`HTTPError.read()` is a SEPARATE code path -- it is reached from inside an `except` clause,
    where a sibling `except` of the same `try` cannot catch what it raises. A deadline that covered
    only the success path would leave the transport unbounded on every 4xx/5xx."""
    status, _body, elapsed = _fetch(trickling_error_url, deadline_in=DEADLINE_SEC)

    assert status == 503, "the real status must survive a bounded body read"
    assert elapsed < BODY_BYTES * GAP_SEC, "the error body was followed to the end"


def test_no_deadline_leaves_every_other_caller_byte_for_byte_unchanged(trickling_url):
    """The discriminating control. A deadline on the DATA leg would truncate a large export that is
    making real progress, so the default must read the whole body exactly as before."""
    status, body, _elapsed = _fetch(trickling_url, deadline_in=None)

    assert (status, body) == (200, b"a" * BODY_BYTES)


def test_a_deadline_already_passed_refuses_before_reading(trickling_url):
    """A backstop, not the main path -- admission already guarantees room. It must still refuse
    rather than read one chunk 'for free', or the bound leaks by one chunk per leg."""
    status, _body, elapsed = _fetch(trickling_url, deadline_in=-1.0)

    assert status == NETWORK_ERROR_STATUS
    assert elapsed < GAP_SEC * 3, f"a passed deadline still read the body for {elapsed:.3f}s"


# ------------------------------------------- the SALVAGE bound, end to end, over a real socket


class _FastFailDataThenTrickle(BaseHTTPRequestHandler):
    """A view whose ``/data`` fails immediately and whose renders trickle forever.

    Exactly the shape the salvage path exists for -- and exactly the shape a virtual clock cannot
    model, because the render's duration is a property of the socket rather than of a script.
    """

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/data"):
            self.send_response(503)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"gw")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(BODY_BYTES))
        self.end_headers()
        for _ in range(BODY_BYTES):
            time.sleep(GAP_SEC)
            try:
                self.wfile.write(b"a")
                self.wfile.flush()
            except OSError:
                return

    def log_message(self, *args):  # noqa: ARG002
        return


def test_a_trickling_salvage_sequence_is_bounded_end_to_end(tmp_path):
    """⚠️ The claim the whole decoupling rests on, proved over a REAL socket.

    Three render tiers are requested and every one of them trickles. Unbounded, that is three bodies
    of ``BODY_BYTES * GAP_SEC`` each. The bound that must hold is the ADMISSION budget
    (``SALVAGE_BUDGET_MULTIPLIER x timeout``) plus at most one socket timeout for the in-flight chunk
    -- and the ``+ one socket timeout`` is honest, not hedging: a read already in progress cannot be
    interrupted.

    ⚠️ This test is the reason the file exists. The virtual-clock version of it passed while the code
    was unbounded, because the clock advances only by the duration each scripted response declares --
    it modelled every request as bounded by its nominal timeout, which is the very assumption that
    turned out to be false. An instrument that shares a defect with the thing it measures cannot fail.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FastFailDataThenTrickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        session = oracle.TableauSession(
            oracle.SiteCredentials(
                base=f"http://127.0.0.1:{port}",
                site="site",
                pat_name="a-long-enough-pat-name",
                pat_secret="a-long-enough-pat-secret",
                version="3.29",
            ),
            oracle.RetryPolicy(max_attempts=1, budget_sec=0.0),
            timeout_sec=SOCKET_TIMEOUT_SEC,
        )
        session.token, session.site_id = "tok", "sid"

        started = time.monotonic()
        record = oracle.capture_view(
            session,
            {"id": LUID, "name": "Availability Summary by Tail", "workbook": {"id": "wb-1"}},
            tmp_path,
            frozenset({"png", "svg", "pdf"}),
            None,
        )
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()

    assert record["data"]["status"] == "transient", "the fixture must reach the SALVAGE path"
    salvage_budget = oracle.SALVAGE_BUDGET_MULTIPLIER * SOCKET_TIMEOUT_SEC
    unbounded = 3 * BODY_BYTES * GAP_SEC
    assert elapsed < salvage_budget + SOCKET_TIMEOUT_SEC + GAP_SEC * 4, (
        f"{elapsed:.3f}s of salvage against a {salvage_budget:.2f}s admission budget plus one "
        f"{SOCKET_TIMEOUT_SEC}s socket timeout"
    )
    assert elapsed < unbounded / 2, f"{elapsed:.3f}s is not meaningfully below the unbounded {unbounded:.2f}s"
    attempted = [leg for leg in ("image", "svg", "pdf") if record[leg].get("attempted") is not False]
    assert attempted, "at least one render must be ATTEMPTED, or this is the old skip, not a bound"
    assert all(record[leg]["status"] != "ok" for leg in ("image", "svg", "pdf")), (
        "a body abandoned at its deadline must never be recorded as a successful render"
    )


def test_the_same_sequence_is_UNBOUNDED_without_the_deadline(tmp_path, monkeypatch):
    """The discriminating control, and the one that makes the test above mean something.

    Neutralise only the end-to-end deadline -- leaving admission, the one-attempt policy and the
    short-circuit all in place -- and the same fixture runs far longer. Without this, a passing bound
    could be explained by the fixture simply being fast.
    """
    monkeypatch.setattr(oracle, "_capture_render", _render_without_deadline(oracle._capture_render))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FastFailDataThenTrickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        session = oracle.TableauSession(
            oracle.SiteCredentials(
                base=f"http://127.0.0.1:{port}",
                site="site",
                pat_name="a-long-enough-pat-name",
                pat_secret="a-long-enough-pat-secret",
                version="3.29",
            ),
            oracle.RetryPolicy(max_attempts=1, budget_sec=0.0),
            timeout_sec=SOCKET_TIMEOUT_SEC,
        )
        session.token, session.site_id = "tok", "sid"
        started = time.monotonic()
        oracle.capture_view(
            session,
            {"id": LUID, "name": "Availability Summary by Tail", "workbook": {"id": "wb-1"}},
            tmp_path,
            frozenset({"png", "svg", "pdf"}),
            None,
        )
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()

    assert elapsed > BODY_BYTES * GAP_SEC, (
        f"without the deadline the first trickling render should run to completion, but the whole "
        f"capture took only {elapsed:.3f}s -- the control is not exercising what it claims"
    )


def _render_without_deadline(original):
    def render(session, view_luid, path, kind, options):
        return original(session, view_luid, path, kind, oracle._RenderOptions(options.api, options.retry, None))

    return render
