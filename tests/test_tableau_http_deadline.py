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

import contextlib
import http.client
import inspect
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tableau_http as th  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_http import (  # noqa: E402  # pylint: disable=wrong-import-position
    NETWORK_ERROR_STATUS,
    _read_bounded,
    _request,
)
import capture_tableau_oracle as oracle  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_oracle_manifest as verdict  # noqa: E402  # pylint: disable=wrong-import-position

pytestmark = pytest.mark.timing

LUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

GAP_SEC = 0.08
BODY_BYTES = 12
SOCKET_TIMEOUT_SEC = 0.1
# The whole trickled body takes BODY_BYTES * GAP_SEC ~= 0.96s, so a deadline well under that must cut
# it short, and "no deadline" must run to completion. Both directions are asserted.
DEADLINE_SEC = 0.3
# The slow-HEADER fixture's own gap, 3x inside the socket timeout rather than 1.25x. Its ~72 bytes
# take ~2.2s in total, comfortably past DEADLINE_SEC in the unbounded direction.
HEADER_GAP_SEC = 0.03


def test_response_framing_requires_an_unambiguous_usable_content_length():
    headers = Message()
    headers.add_header("Content-Length", "5")
    headers.add_header("Content-Length", "10")

    assert th.response_framing(headers) == "close_delimited"
    assert th.response_framing({"Content-Length": "abc"}) == "close_delimited"
    assert th.response_framing({"Content-Length": "5"}) == "content_length"


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


def _serve_slow_headers(listener: socket.socket) -> None:
    """Answer one request byte-by-byte from the STATUS LINE onward, never idling past the timeout.

    Deliberately a raw socket rather than ``BaseHTTPRequestHandler``: the handler writes its status
    line and headers as whole buffered writes, which is exactly why every earlier fixture in this file
    sends headers instantly and could not reach the phase that was unbounded.

    ⚠️ ``HEADER_GAP_SEC`` is its own constant, well under the socket timeout rather than merely under
    it. At the body fixture's 0.08s against a 0.1s timeout the margin is 25%, and a loaded box
    overshoots that -- the unbounded control flaked with a spurious ``status 0``, which would have
    read as "the timeout caught it" and hidden the very thing the control exists to show.
    """
    conn, _addr = listener.accept()
    try:
        conn.recv(4096)
        response = f"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Length: {BODY_BYTES}\r\n\r\n"
        for char in response + "a" * BODY_BYTES:
            time.sleep(HEADER_GAP_SEC)
            conn.sendall(char.encode())
    except OSError:
        pass  # the client aborted at its deadline, which is the behaviour under test
    finally:
        conn.close()


@pytest.fixture(name="slow_header_url")
def _slow_header_url():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threading.Thread(target=_serve_slow_headers, args=(listener,), daemon=True).start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/data"
    finally:
        listener.close()


def test_slow_HEADERS_are_bounded_by_the_deadline_too(slow_header_url):
    """⚠️ Review round 3, finding 1 -- reproduced, then fixed. And the reason it was reachable at all.

    The deadline used to be enforced only AFTER ``urlopen()`` returned, so connection, status line,
    headers and redirects ran under the per-socket-operation timeout alone. A server trickling its
    HEADERS one byte at a time never trips that. Measured on the production ``_request``: a 0.15s
    deadline with a 0.05s socket timeout returned after **1.378s**, 6.9x the claimed ceiling.

    ⚠️ **The eight tests above could not catch it**, and the reason is worth more than the fix: every
    one of them sends headers immediately and trickles only the BODY, so their deadline-removed
    control proves the body is bounded and is structurally blind to the pre-body phase. The control
    was real; the claim it was read as supporting was larger than the fixture could reach. Same shape
    as the virtual clock two rounds earlier -- **ask what the control cannot see**.
    """
    started = time.monotonic()
    status, _body, _headers = _request(
        urllib.request.Request(slow_header_url),
        timeout=SOCKET_TIMEOUT_SEC,
        redactor=lambda text: text,
        deadline=started + DEADLINE_SEC,
    )
    elapsed = time.monotonic() - started

    assert status == NETWORK_ERROR_STATUS
    assert elapsed < DEADLINE_SEC + SOCKET_TIMEOUT_SEC + GAP_SEC * 4, (
        f"the headers ran past the deadline: {elapsed:.3f}s against {DEADLINE_SEC}s"
    )


def test_slow_headers_are_UNBOUNDED_without_the_deadline(slow_header_url):
    """The discriminating control for the test above. Without a deadline the same fixture runs to
    completion, so the bound is attributable to the deadline rather than to the fixture being quick."""
    started = time.monotonic()
    status, _body, _headers = _request(
        urllib.request.Request(slow_header_url), timeout=SOCKET_TIMEOUT_SEC, redactor=lambda text: text
    )
    elapsed = time.monotonic() - started

    assert status == 200
    assert elapsed > DEADLINE_SEC * 2, (
        f"the fixture completed in {elapsed:.3f}s, too fast to show the deadline is doing the work"
    )


def test_the_https_twin_resolves_connect_to_the_watchdog():
    """⚠️ No fixture here can complete a TLS handshake, so the HTTPS path is pinned STATICALLY.

    Base order decides this, and the natural order is the wrong one: with
    ``(HTTPSConnection, _DeadlineHTTPConnection)`` the MRO finds ``HTTPSConnection.connect`` first, the
    watchdog never arms, and **every real Tableau request** -- all of which are HTTPS -- is silently
    unbounded while every loopback test in this file passes. That is the same shape as the finding
    this file exists for: the fixture cannot reach the case that fails.

    ⚠️ The assertion used to be ``_DeadlineHTTPSConnection.connect is _DeadlineHTTPConnection.connect``
    -- identity, which was true only while the HTTPS twin defined no ``connect`` of its own. It now
    defines one (the handshake had to move inside the deadline), so identity would fail for a change
    that keeps the property perfectly intact. What actually matters is the MRO POSITION: whichever
    ``connect`` runs first, ``super()`` from it must reach `_DeadlineHTTPConnection.connect` -- the one
    that arms the watchdog -- BEFORE it reaches ``HTTPSConnection.connect``. That is asserted directly,
    plus that the HTTPS twin's own ``connect`` really does delegate rather than re-implement.
    """
    mro = th._DeadlineHTTPSConnection.__mro__
    assert mro.index(th._DeadlineHTTPConnection) < mro.index(http.client.HTTPSConnection), (
        "HTTPS resolves `connect` to the stdlib implementation first, so the deadline watchdog never arms"
    )
    assert issubclass(th._DeadlineHTTPSConnection, http.client.HTTPSConnection), "TLS must still happen"
    after_https_twin = mro[mro.index(th._DeadlineHTTPSConnection) + 1 :]
    assert next(base for base in after_https_twin if "connect" in vars(base)) is th._DeadlineHTTPConnection, (
        "`super().connect()` from the HTTPS twin no longer lands on the arming implementation"
    )
    source = inspect.getsource(th._DeadlineHTTPSConnection.connect)
    assert "super().connect()" in source, (
        "the HTTPS twin re-implements the connection sequence instead of delegating to the arming one; "
        "the watchdog and the stdlib's proxy/TLS setup have silently forked"
    )


def test_the_deadline_subclass_carries_the_instant_without_a_constructor():
    """`_with_deadline` exists because a guarded module may not use `*args`/`**kwargs` -- the taint
    analyser cannot follow them, and `tests/test_diagnostic_redaction.py` refuses them. This pins that
    the class attribute actually arrives, so the no-constructor route is not merely lint-shaped."""
    deadline = time.monotonic() + 5.0
    made = th._with_deadline(th._DeadlineHTTPConnection, deadline)

    assert made._t2p_deadline == deadline
    assert issubclass(made, th._DeadlineHTTPConnection)
    assert th._DeadlineHTTPConnection._t2p_deadline is None, "the base must stay unbound"


def test_a_deadline_already_passed_never_opens_a_connection(slow_header_url):
    """The pre-request check. If the budget is spent before the request is issued, the honest move is
    to refuse rather than to open a socket and abort it a moment later."""
    started = time.monotonic()
    status, body, _headers = _request(
        urllib.request.Request(slow_header_url),
        timeout=SOCKET_TIMEOUT_SEC,
        redactor=lambda text: text,
        deadline=started - 1.0,
    )
    elapsed = time.monotonic() - started

    assert status == NETWORK_ERROR_STATUS
    assert b"already passed" in body
    assert elapsed < GAP_SEC * 2, f"a spent deadline still did network work for {elapsed:.3f}s"


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
    read must surface as `NETWORK_ERROR_STATUS`, which the retry classifier treats as transient.

    ⚠️ **THREE mechanisms can abandon, and asserting on any one specifically is how this test has now
    broken twice.** Which one wins is a race between the deadline instant, the next chunk, and how the
    platform reports an aborted socket -- so the marker list is a UNION and the property under test is
    the outcome, never the mechanism:

    * the body clock check raises `TimeoutError`;
    * the lifecycle watchdog aborts the socket, arriving as `ConnectionAbortedError`/`OSError`;
    * the abort lands as a clean EOF with bytes still outstanding under `Content-Length`, which is
      now refused as `IncompleteRead`.

    The third is new, and CI found it on Linux where local Windows runs could not: `shutdown(SHUT_RDWR)`
    raises on Windows but yields a clean EOF on Linux -- the platform divergence `tableau_http`'s own
    EOF branch records -- so on Linux the outstanding-length check fires first. All three are correct
    refusals; `IncompleteRead` is the most precise of them, because it names the byte count the peer
    still owed.
    """
    status, body, _elapsed = _fetch(trickling_url, deadline_in=DEADLINE_SEC)

    assert status == NETWORK_ERROR_STATUS
    assert b"a" * BODY_BYTES not in body, "the partial body must not be returned as though complete"
    assert any(marker in body for marker in (b"TimeoutError", b"Error", b"IncompleteRead")), (
        f"the diagnostic does not name why the read was abandoned: {body[:120]!r}"
    )


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


# -------------------------------------------- `_read_bounded` as an INDEPENDENT requirement
#
# ⚠️ Once the lifecycle watchdog landed, three mutations aimed at the body check SURVIVED: the
# watchdog aborts the socket first, so neutering `_read_bounded` no longer changed any socket-level
# outcome. That is this repository's "a clause implied by its siblings is unkillable" mode, and its
# documented remedy is to pin the clause as an independent requirement or delete it -- never to ship
# it undefended. It is kept, because it is what turns an abandoned read into a diagnostic that names
# the deadline and the byte count instead of `ConnectionAbortedError`, and because a stream is not
# always a socket. So it is pinned HERE, with no socket and no watchdog anywhere in the fixture.


class _FakeTrickleStream:
    """A stream that yields one byte per `read1`, slowly, and blocks for the whole body on `read`.

    Models the property that made `read1` necessary: `HTTPResponse.read(n)` waits for n bytes, so a
    chunked loop written with `read` consults its clock exactly once.
    """

    def __init__(self, total: int = BODY_BYTES, gap: float = GAP_SEC) -> None:
        self.remaining, self.gap, self.reads = total, gap, 0

    def read1(self, _size: int) -> bytes:
        self.reads += 1
        if not self.remaining:
            return b""
        time.sleep(self.gap)
        self.remaining -= 1
        return b"a"

    def read(self, _size: int = -1) -> bytes:
        self.reads += 1
        time.sleep(self.gap * self.remaining)
        out, self.remaining = b"a" * self.remaining, 0
        return out


def test_read_bounded_abandons_a_stream_that_outlives_its_deadline():
    """No socket, no watchdog: the body check alone must refuse to follow a trickling stream."""
    stream = _FakeTrickleStream()
    started = time.monotonic()
    with pytest.raises(TimeoutError) as excinfo:
        _read_bounded(stream, started + DEADLINE_SEC, SOCKET_TIMEOUT_SEC)
    elapsed = time.monotonic() - started

    assert elapsed < DEADLINE_SEC + GAP_SEC * 3, f"{elapsed:.3f}s against a {DEADLINE_SEC}s deadline"
    assert stream.reads > 2, "it consulted the clock once and then read everything -- `read`, not `read1`"
    assert "deadline" in str(excinfo.value)


def test_read_bounded_never_returns_a_partial_body():
    """The silent-corruption case. Returning what arrived would report a truncated CSV as complete."""
    with pytest.raises(TimeoutError):
        _read_bounded(_FakeTrickleStream(), time.monotonic() + DEADLINE_SEC, SOCKET_TIMEOUT_SEC)


def test_read_bounded_without_a_deadline_reads_the_whole_stream():
    """The control: the default path is unchanged, so the data leg still streams a slow export."""
    assert _read_bounded(_FakeTrickleStream(total=3, gap=0.0), None, SOCKET_TIMEOUT_SEC) == b"aaa"


class _EofOnAbortStream:
    """A stream whose read BLOCKS ACROSS the deadline and then returns a clean EOF -- Linux behaviour.

    ⚠️ Written from a CI failure, not from imagination. ``_abort_socket`` uses
    ``shutdown(SHUT_RDWR)``; on Windows an in-flight read raises ``ConnectionAbortedError``, on Linux
    it returns ``b""``. The transport read that as a complete body and reported **HTTP 200** for an
    aborted trickle -- silent corruption, green on the machine it was written on, red on the machine
    that gates the merge.

    ⚠️ The read must CROSS the deadline rather than start after it, or the loop's top-of-iteration
    check fires first and the EOF branch is never reached -- which is what the first version of this
    fixture did, passing for the wrong reason.
    """

    def __init__(self, deadline: float) -> None:
        self._deadline = deadline
        self.reads = 0

    def read1(self, _size: int) -> bytes:
        self.reads += 1
        remaining = self._deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining + 0.01)
        return b""


def test_an_eof_at_the_deadline_is_not_treated_as_a_complete_body():
    """⚠️ The platform-divergence case, pinned so it cannot depend on which OS ran the suite.

    Reporting a truncated CSV as a 200 is worse than the unbounded read this mechanism replaced,
    because it is silent -- and it is precisely what an abandoned read looks like where ``shutdown``
    yields EOF rather than an error.
    """
    deadline = time.monotonic() + DEADLINE_SEC
    stream = _EofOnAbortStream(deadline)
    with pytest.raises(TimeoutError) as excinfo:
        _read_bounded(stream, deadline, SOCKET_TIMEOUT_SEC)

    assert stream.reads == 1, "the read must have been ENTERED, or the EOF branch was never reached"
    assert "cannot be assumed" in str(excinfo.value)


def test_a_body_that_genuinely_finishes_in_time_still_returns():
    """The discriminating control. Refusing every EOF would turn the check into "always fail", which
    would pass the test above and break every real capture."""
    stream = _FakeTrickleStream(total=3, gap=0.0)
    assert _read_bounded(stream, time.monotonic() + DEADLINE_SEC, SOCKET_TIMEOUT_SEC) == b"aaa"


class _TrickleHTTPError(urllib.error.HTTPError):
    """A 5xx whose body trickles. `HTTPError.read` is reached from inside an `except` clause."""

    def __init__(self) -> None:
        super().__init__("http://x/y", 503, "Service Unavailable", {}, None)
        self._stream = _FakeTrickleStream()

    def read1(self, size: int) -> bytes:
        return self._stream.read1(size)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def test_the_error_body_path_is_bounded_without_any_socket(monkeypatch):
    """⚠️ The HTTPError branch is a SEPARATE call site, and a deadline applied only to the success
    path leaves it unbounded. Pinned with `_open` stubbed, so neither a socket nor the watchdog can
    stand in for the body check and mask a bypass."""

    def _raise(_req, _timeout, _deadline):
        raise _TrickleHTTPError()

    monkeypatch.setattr("tableau_http._open", _raise)
    started = time.monotonic()
    status, body, _headers = _request(
        urllib.request.Request("http://127.0.0.1:1/x"),
        timeout=SOCKET_TIMEOUT_SEC,
        redactor=lambda text: text,
        deadline=started + DEADLINE_SEC,
    )
    elapsed = time.monotonic() - started

    assert status == 503, "the real status must survive a bounded error-body read"
    assert elapsed < DEADLINE_SEC + GAP_SEC * 4, f"the error body was followed for {elapsed:.3f}s"
    assert b"a" * BODY_BYTES not in body


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


# ------------------------------- the PREMATURE CLOSE: a truncated render credited as evidence
#
# ⚠️ This is the fail-OPEN the decoupling created, and it inverts the point of the whole change.
# Decoupling the render leg is only worth doing if the render then fails LOUDLY; a truncated render
# recorded as `ok` is strictly worse than the suppression it replaced, because the entry gate reports
# the view as covered and a fidelity bug on it becomes not merely unverified but believed verified.
#
# Measured on the unfixed code, against the server below (200, image/png, Content-Length: 1024, then
# the 8-byte PNG signature, then close):
#
#     _request with the salvage deadline:  status=200, 8 bytes, format_matches("png", ...) == True
#     _request without the deadline:       status=0,   IncompleteRead(8 bytes read, 1016 more expected)
#     capture_view after a /data 400:      image.status == "ok", SHA-256 recorded, unestablished == 0
#
# So the DEADLINE path -- the one the salvage leg always takes -- was weaker than the plain read it
# wraps. `HTTPResponse.read1` does not raise `IncompleteRead` at a premature close; it calls
# `_close_conn()` and returns b"", which `_read_bounded` took for a completed body.


class _TruncatedRender(BaseHTTPRequestHandler):
    """`/data` fails generically, then every render declares 1 KiB and sends only its magic bytes.

    A generic 400 on `/data` is what routes this into the salvage path -- the same route the field
    evidence takes -- so the render legs are reached exactly as they are in production.
    """

    protocol_version = "HTTP/1.1"
    DECLARED = 1024
    PREFIXES = {"format=svg": b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"', "pdf": b"%PDF-1.7"}

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/data"):
            self.send_response(400)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"no")
            return
        prefix = next((body for marker, body in self.PREFIXES.items() if marker in self.path), b"\x89PNG\r\n\x1a\n")
        self.send_response(200)
        self.send_header("Content-Length", str(self.DECLARED))
        self.end_headers()
        self.wfile.write(prefix)
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args):  # noqa: ARG002
        return


def _capture_against(handler, tmp_path):
    """Run `capture_view` for all three render tiers against a one-shot local server."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        session = oracle.TableauSession(
            oracle.SiteCredentials(
                base=f"http://127.0.0.1:{server.server_address[1]}",
                site="site",
                pat_name="a-long-enough-pat-name",
                pat_secret="a-long-enough-pat-secret",
                version="3.29",
            ),
            oracle.RetryPolicy(max_attempts=1, budget_sec=0.0),
            timeout_sec=1.0,
        )
        session.token, session.site_id = "tok", "sid"
        return oracle.capture_view(
            session,
            {"id": LUID, "name": "Availability Summary by Tail", "workbook": {"id": "wb-1"}},
            tmp_path,
            frozenset({"png", "svg", "pdf"}),
            None,
        )
    finally:
        server.shutdown()
        server.server_close()


def test_a_render_the_peer_TRUNCATED_is_never_recorded_as_evidence(tmp_path):
    """⚠️ The blocker: 8 bytes with a valid magic number were persisted as a complete render.

    Every assertion here failed on the unfixed code -- the record read `status: ok` with a `sha256`
    and a `path`, and `render_unestablished` was 0 for a view with no usable reference at all.
    """
    record = _capture_against(_TruncatedRender, tmp_path)

    assert record["data"]["status"] != "ok", "the fixture must reach the SALVAGE path"
    for leg in ("image", "svg", "pdf"):
        assert record[leg]["status"] != "ok", (
            f"a truncated {leg} render was credited as evidence: {record[leg]!r}. The peer declared "
            f"{_TruncatedRender.DECLARED} bytes and sent only its magic number"
        )
        assert "sha256" not in record[leg], f"a digest of a truncated {leg} body was recorded as provenance"
        assert "path" not in record[leg], f"a truncated {leg} body was written to disk"
    assert not list(tmp_path.rglob("*.png")) and not list(tmp_path.rglob("*.svg")), (
        "truncated bytes reached the filesystem, where nothing downstream can tell them from a render"
    )

    unestablished = verdict.render_unestablished([record], frozenset({"png", "svg", "pdf"}))

    assert len(unestablished) == 1, (
        "the entry gate reports this view as having an established render. That is the fail-open: a "
        "fidelity defect on it is then not merely unverified but believed verified"
    )


def test_the_truncation_is_caught_by_the_TRANSPORT_not_only_by_the_format_check(tmp_path):
    """Which layer refuses it matters, so the two are separated rather than assumed to agree.

    ⚠️ Positive-and-negative control on the `Content-Length` check specifically. The transport must
    refuse the body outright -- returning `NETWORK_ERROR_STATUS`, retry-eligible -- rather than handing
    up 8 bytes for a later completeness check to reject. If only the completeness check fired, the
    truncation would be invisible to every OTHER caller of `_request`, including the data leg, whose
    CSV has no structure to check at all.

    The control is the same server read WITHOUT a deadline: both paths must classify it identically.
    Before the fix they did not, and the deadline path was the weaker one.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TruncatedRender)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        outcomes = {
            label: _request(
                urllib.request.Request(f"http://127.0.0.1:{port}/image"),
                timeout=1.0,
                redactor=lambda text: text,
                deadline=deadline,
            )
            for label, deadline in (("bounded", time.monotonic() + 5.0), ("unbounded", None))
        }
    finally:
        server.shutdown()
        server.server_close()

    for label, (status, body, _headers) in outcomes.items():
        assert status == NETWORK_ERROR_STATUS, (
            f"the {label} read reported HTTP {status} for a body the peer cut short; a truncated "
            "payload must be a transient failure, never a response"
        )
        assert b"IncompleteRead" in body, (status, body)
    assert outcomes["bounded"] == outcomes["unbounded"], (
        "the deadline path and the plain read classify a premature close differently. The deadline "
        "path was measured to be the WEAKER of the two -- 200 with 8 bytes against IncompleteRead"
    )


class _CloseDelimitedTruncatedRender(_TruncatedRender):
    """The same truncation, framed so the TRANSPORT cannot possibly detect it.

    ⚠️ This fixture exists because a mutation SURVIVED. Removing the completeness check from
    `_capture_render` entirely left `test_a_render_the_peer_TRUNCATED_is_never_recorded_as_evidence`
    green -- the transport's `Content-Length` check had already refused the body, so the capture-layer
    guard was never reached and was, in this repository's words, "a guard implied by a stronger
    sibling", which is unkillable and therefore untested.

    The two layers are NOT redundant, and this is the case that proves it. With no `Content-Length`
    the body is delimited by the close itself, so `HTTPResponse.length` is `None` and EOF is the
    legitimate, correct end of a complete response. The transport must accept it; only structure can
    say the PNG is 8 bytes of signature and nothing else. Chunked framing that ends early is caught by
    `http.client` itself, but close-delimited framing has no terminator at all -- and HTTP/1.0 clients,
    proxies and error paths still produce it.
    """

    protocol_version = "HTTP/1.0"

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/data"):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"no")
            return
        prefix = next((body for marker, body in self.PREFIXES.items() if marker in self.path), b"\x89PNG\r\n\x1a\n")
        self.send_response(200)
        self.end_headers()  # no Content-Length: the close IS the framing
        self.wfile.write(prefix)
        self.wfile.flush()
        self.close_connection = True


def test_a_STRUCTURALLY_incomplete_render_is_refused_even_when_the_transport_is_satisfied(tmp_path):
    """⚠️ The independent half of blocker 1, on the surface where the transport is blind.

    The transport is CORRECT to accept this body -- close-delimited framing has no declared length, so
    EOF is the end of a complete response. `payload_is_complete` is the only thing that can say the
    PNG carries no IHDR, no IDAT and no IEND. Both assertions below fail if the capture-layer check is
    removed, which is what makes it a guard rather than decoration.
    """
    record = _capture_against(_CloseDelimitedTruncatedRender, tmp_path)

    for leg in ("image", "svg", "pdf"):
        assert record[leg]["status"] == "truncated", (
            f"a close-delimited, structurally incomplete {leg} render was recorded as "
            f"{record[leg].get('status')!r}. The transport cannot see this one -- there is no declared "
            "length to fall short of -- so the capture-layer completeness check is the only guard"
        )
        assert "sha256" not in record[leg] and "path" not in record[leg], record[leg]
    assert not list(tmp_path.rglob("*.png")), "truncated bytes were written to disk"


# ---------------------------------------------------------------------------------------------
# Review round 4, finding 1: the watchdog was armed AFTER proxy tunnelling and TLS setup.
#
# ⚠️ The point of this whole section is not the assertions -- it is that no fixture above can
# REACH these phases. The slow-header server proves the phase after the connection is bounded;
# it arms the timer the instant plain TCP is up and never tunnels or negotiates. So a defect in
# the connection sequence itself was invisible to all eight of them, for the third round running:
# body-bounded (headers sent instantly), MRO (every fixture is HTTP), arming point (nothing
# reaches TLS or a proxy). A fixture that cannot produce the failure mode cannot refute it.
# ---------------------------------------------------------------------------------------------

PROXY_ESTABLISHED = "HTTP/1.1 200 Connection established\r\n\r\n"


def _serve_trickling_proxy(listener: socket.socket) -> None:
    """A proxy that answers CONNECT one byte at a time, each byte inside the socket timeout.

    This is the reviewer''s measured reproduction: `_tunnel()` reads the proxy''s response through
    `makefile()`, so the per-operation timeout never fires and the exchange runs unbounded.
    """
    conn, _addr = listener.accept()
    try:
        conn.recv(4096)  # the CONNECT request line and its headers
        for char in PROXY_ESTABLISHED:
            time.sleep(HEADER_GAP_SEC)
            conn.sendall(char.encode())
    except OSError:
        pass  # the client aborted at its deadline, which is the behaviour under test
    finally:
        conn.close()


def _listening(server) -> tuple[str, int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threading.Thread(target=server, args=(listener,), daemon=True).start()
    return listener


@pytest.fixture(name="trickling_proxy")
def _trickling_proxy():
    listener = _listening(_serve_trickling_proxy)
    try:
        yield "127.0.0.1", listener.getsockname()[1]
    finally:
        listener.close()


def _tunnel_through(proxy: tuple[str, int], deadline: float | None) -> float:
    """Open a tunnelled connection through `proxy`, returning how long the attempt took."""
    host, port = proxy
    factory = (
        th._DeadlineHTTPConnection if deadline is None else th._with_deadline(th._DeadlineHTTPConnection, deadline)
    )
    conn = factory(host, port, timeout=SOCKET_TIMEOUT_SEC)
    conn.set_tunnel("example.invalid", 443)
    started = time.monotonic()
    try:
        conn.connect()
    except (OSError, http.client.HTTPException):
        pass  # an abandoned tunnel, which is what a bounded connect looks like
    finally:
        elapsed = time.monotonic() - started
        conn.close()
    return elapsed


def test_a_proxy_that_trickles_its_CONNECT_response_is_bounded(trickling_proxy):
    """⚠️ Round 4, finding 1 -- reproduced and fixed. Measured 1.241s against a 0.20s ceiling.

    `connect()` armed the watchdog only after `super().connect()` returned, and with the corrected
    HTTPS MRO that `super()` is `HTTPSConnection.connect`: TCP setup, the proxy CONNECT exchange and
    the entire TLS handshake ran outside it, with `timer_armed = False` throughout.
    """
    elapsed = _tunnel_through(trickling_proxy, time.monotonic() + DEADLINE_SEC)

    assert elapsed < DEADLINE_SEC + SOCKET_TIMEOUT_SEC + GAP_SEC * 4, (
        f"the proxy exchange ran past the deadline: {elapsed:.3f}s against {DEADLINE_SEC}s"
    )


def test_a_trickling_proxy_is_UNBOUNDED_without_the_deadline(trickling_proxy):
    """The discriminating control. Without a deadline the same proxy runs to completion, so the bound
    above is attributable to the watchdog rather than to the fixture finishing quickly on its own."""
    elapsed = _tunnel_through(trickling_proxy, None)

    assert elapsed > DEADLINE_SEC * 2, (
        f"the tunnel completed in {elapsed:.3f}s, too fast to show the deadline is doing the work"
    )


def _serve_idle(listener: socket.socket) -> None:
    """Accept one connection and hold it open, sending nothing. Enough to reach the TLS phase."""
    conn, _addr = listener.accept()
    time.sleep(5.0)
    conn.close()


class _FakeSSLSocket(socket.socket):
    """A socket that answers ``do_handshake()``, which a plain ``socket`` does not.

    ⚠️ Needed because the production path now calls ``do_handshake()`` explicitly -- that IS the fix.
    Recording the call is what makes the fixture prove the handshake still happens: deferring must MOVE
    ``do_handshake()``, never drop it, and a stub silently lacking the method would let "no handshake
    at all" pass as "deferred handshake".
    """

    handshakes = 0
    timeout_at_handshake = None

    def do_handshake(self) -> None:
        type(self).handshakes += 1
        type(self).timeout_at_handshake = self.gettimeout()
        raise OSError("this fixture never negotiates; the recorded facts are the point")


# How long the proxy sits on the CONNECT before answering. It is the whole point of the TLS fixture:
# the defect was a per-phase socket timeout RESTARTING at the handshake, so the overrun is exactly the
# budget spent before it. A fixture that reaches TLS instantly cannot show the difference.
TLS_PROXY_DELAY_SEC = 0.3
TLS_DEADLINE_SEC = 0.6
# Generous on purpose: with a socket timeout tighter than the deadline, the socket timeout would do
# the bounding in BOTH arms and the deadline's contribution would be unobservable in either.
TLS_SOCKET_TIMEOUT_SEC = 5.0


class _RecordingContext:
    """Stands in for the SSL context, so the TLS phase is reachable without a certificate.

    ⚠️ It records whether the watchdog was ALREADY armed when TLS negotiation began -- which is the
    claim under test, and the one thing `test_the_https_twin_resolves_connect_to_the_watchdog` cannot
    show. That test proves WHICH `connect` runs; this proves WHEN its watchdog is armed.

    ⚠️ **And this fake is exactly why that was not enough.** Its `wrap_socket` returns immediately, so
    the test samples the timer either side of the blocking phase rather than DURING it. A fixture that
    cannot block cannot show a missing bound -- which is how the handshake stayed unbounded through the
    round that added this test. `test_a_trickling_TLS_handshake_is_bounded_by_the_deadline` is the
    counterpart that does block; keep both, and do not let this one stand alone again.

    `do_handshake_on_connect` is accepted because the production path now passes it: the handshake is
    deferred so the `SSLSocket` exists, and is abortable, before it starts. Recording the value is the
    point -- a future edit that stops deferring would silently restore the unbounded phase.
    """

    def __init__(self, replacement: socket.socket) -> None:
        self.connection = None
        self.replacement = replacement
        self.armed_at_entry = None
        self.server_hostname = None
        self.deferred_handshake = None

    def wrap_socket(
        self, sock: socket.socket, server_hostname: str | None = None, do_handshake_on_connect: bool = True
    ) -> socket.socket:
        self.armed_at_entry = self.connection._t2p_timer is not None
        self.server_hostname = server_hostname
        self.deferred_handshake = do_handshake_on_connect is False
        sock.close()
        return self.replacement


def test_the_watchdog_is_armed_before_TLS_negotiation():
    """The phase no fixture in this file could reach before: `wrap_socket` on a real connection."""
    listener = _listening(_serve_idle)
    replacement = _FakeSSLSocket()
    # A real `wrap_socket` preserves the raw socket's timeout, so the fixture must too -- otherwise
    # the `min()` below has nothing to choose between and the narrowing assertion proves nothing.
    replacement.settimeout(SOCKET_TIMEOUT_SEC)
    _FakeSSLSocket.handshakes = 0
    context = _RecordingContext(replacement)
    try:
        deadline = time.monotonic() + 5.0
        factory = th._with_deadline(th._DeadlineHTTPSConnection, deadline)
        conn = factory("127.0.0.1", listener.getsockname()[1], timeout=SOCKET_TIMEOUT_SEC, context=context)
        context.connection = conn
        with contextlib.suppress(OSError):
            conn.connect()  # the fixture's `do_handshake` refuses, after recording what matters
        armed_at_entry, armed_sock = context.armed_at_entry, conn._t2p_armed_sock
        deferred, hostname = context.deferred_handshake, context.server_hostname
        conn.close()
    finally:
        listener.close()
        replacement.close()

    assert armed_at_entry is True, "TLS negotiation began with no watchdog armed"
    assert armed_sock is replacement, (
        "the watchdog still points at the raw socket `wrap_socket` detached, so it is inert from the "
        "handshake onwards -- where the status line, the headers and the body are read"
    )
    assert deferred is True, (
        "`wrap_socket` was asked to handshake inline again, so the handshake runs before the watchdog "
        "can be re-pointed and is bounded only by a per-phase socket timeout -- measured 0.167s over a "
        "0.16s deadline"
    )
    assert _FakeSSLSocket.handshakes == 1, (
        "the deferred handshake was never performed. Deferring must MOVE `do_handshake()`, not drop "
        "it -- dropping it would skip certificate and hostname verification entirely"
    )
    assert _FakeSSLSocket.timeout_at_handshake == SOCKET_TIMEOUT_SEC, (
        f"the handshake ran with a {_FakeSSLSocket.timeout_at_handshake!r} socket timeout. With 5s of "
        f"budget left and a {SOCKET_TIMEOUT_SEC}s per-operation timeout, the tighter of the two must "
        "win -- narrowing must never LOOSEN an existing timeout"
    )
    assert hostname == "127.0.0.1", (
        "the server hostname stopped being forwarded, so certificate hostname verification -- which "
        "happens inside the deferred `do_handshake()` -- would silently no longer check anything"
    )


def test_the_handshake_timeout_is_narrowed_to_the_REMAINING_budget():
    """The other half of that arithmetic, and the half the defect was made of.

    ⚠️ The test above shows a tighter per-operation timeout is honoured. This shows the opposite case,
    which is the one that was broken: when the budget left is SMALLER than the connection's nominal
    timeout, the handshake must get the budget -- not a fresh full-length per-phase timeout. Leaving
    the per-phase value is exactly how a 0.16s deadline came to fail at 0.327s.
    """
    listener = _listening(_serve_idle)
    replacement = _FakeSSLSocket()
    replacement.settimeout(5.0)  # a generous per-operation timeout, as `_open` would leave it
    _FakeSSLSocket.handshakes = 0
    context = _RecordingContext(replacement)
    try:
        factory = th._with_deadline(th._DeadlineHTTPSConnection, time.monotonic() + 0.5)
        conn = factory("127.0.0.1", listener.getsockname()[1], timeout=5.0, context=context)
        context.connection = conn
        with contextlib.suppress(OSError):
            conn.connect()
        conn.close()
    finally:
        listener.close()
        replacement.close()

    assert _FakeSSLSocket.handshakes == 1
    assert 0 < _FakeSSLSocket.timeout_at_handshake <= 0.5, (
        f"the handshake was given {_FakeSSLSocket.timeout_at_handshake!r}s with only 0.5s of deadline "
        "left, so a slow handshake would outlive the deadline by the whole difference"
    )


def test_the_deferred_context_forwards_verification_settings_untouched():
    """Deferring the handshake must not weaken it. This is a STRUCTURAL control, and says so.

    ⚠️ Honest scope: no fixture in this file completes a real handshake, so this cannot demonstrate
    that a bad certificate is rejected. What it CAN establish is that nothing about verification was
    changed on the way past -- `check_hostname` and `verify_mode` are read straight off the real
    context through `__getattr__`, and the only argument the wrapper adds is
    `do_handshake_on_connect=False`, which moves `do_handshake()` a few lines later rather than
    skipping it. Verification lives inside that call, so moving it cannot disable it.
    """
    real = ssl.create_default_context()
    proxy = th._DeferredHandshakeContext(real)

    assert proxy.check_hostname is real.check_hostname is True
    assert proxy.verify_mode is real.verify_mode is ssl.CERT_REQUIRED
    assert proxy.protocol is real.protocol
    source = inspect.getsource(th._DeferredHandshakeContext.wrap_socket)
    assert "do_handshake_on_connect=False" in source
    for weakening in ("check_hostname", "verify_mode", "CERT_NONE"):
        assert weakening not in source, f"the deferred wrapper now touches {weakening}"


def _serve_delayed_proxy_then_trickling_tls(listener: socket.socket) -> None:
    """Delay the CONNECT, then answer the ClientHello with a WELL-FORMED, endlessly trickling record.

    ⚠️ This is the fixture `_RecordingContext` could not be, and both halves are load-bearing. The
    DELAY spends real budget before TLS starts -- which is the whole defect, a per-phase socket timeout
    RESTARTING at the handshake rather than counting down what is left. The TRICKLE then keeps every
    handshake byte inside the socket timeout, and a well-formed record header means OpenSSL waits for
    the payload it was promised rather than erroring out.
    """
    conn, _addr = listener.accept()
    try:
        conn.recv(4096)  # the CONNECT request line and its headers
        time.sleep(TLS_PROXY_DELAY_SEC)
        conn.sendall(PROXY_ESTABLISHED.encode())
        conn.recv(4096)  # the ClientHello
        conn.sendall(b"\x16\x03\x03\x20\x00")  # handshake record, TLS 1.2, 8192 bytes to follow
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            time.sleep(HEADER_GAP_SEC)
            conn.sendall(b"\x00")
    except OSError:
        pass  # the client abandoned the handshake, which is the behaviour under test
    finally:
        conn.close()


def _handshake_through(port: int, deadline: float | None) -> float:
    """Tunnel then negotiate TLS against `port`, returning how long it took before giving up.

    The socket timeout is narrowed exactly as `_open` does it, so this measures the production
    arithmetic rather than a friendlier version of it.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    started = time.monotonic()
    if deadline is None:
        conn = th._DeadlineHTTPSConnection("127.0.0.1", port, timeout=TLS_SOCKET_TIMEOUT_SEC, context=context)
    else:
        factory = th._with_deadline(th._DeadlineHTTPSConnection, deadline)
        narrowed = min(TLS_SOCKET_TIMEOUT_SEC, max(deadline - started, 0.01))
        conn = factory("127.0.0.1", port, timeout=narrowed, context=context)
    conn.set_tunnel("example.invalid", 443)
    try:
        conn.connect()
    except (OSError, ssl.SSLError, http.client.HTTPException):
        pass
    finally:
        elapsed = time.monotonic() - started
        conn.close()
    return elapsed


def test_a_trickling_TLS_handshake_is_bounded_by_the_deadline():
    """⚠️ The last unbounded blocking phase, reproduced and fixed.

    Before this, `wrap_socket` handshook INSIDE `HTTPSConnection.connect`, while the raw socket the
    watchdog pointed at had already been detached (`fileno() == -1`, so every abort call was swallowed)
    and the `SSLSocket` did not yet exist. The only bound left was the socket timeout, which restarts
    at the handshake instead of counting down the remaining budget. Measured through a proxy delaying
    `CONNECT` by 0.10s, with `_open`'s own narrowing applied: a 0.16s deadline failed at **0.327s,
    0.167s over**, the watchdog firing on `fileno=-1` throughout. After: **0.165s, 0.005s over**.

    ⚠️ The ceiling below is deliberately tighter than this file's usual slack, and that is what makes
    the test discriminating rather than decorative. The unfixed code costs
    `TLS_PROXY_DELAY_SEC + TLS_DEADLINE_SEC` = 0.9s, so anything looser than ~0.8s would pass on it.
    Loosening this ceiling silently removes the test's ability to fail for its own reason.
    """
    listener = _listening(_serve_delayed_proxy_then_trickling_tls)
    try:
        elapsed = _handshake_through(listener.getsockname()[1], time.monotonic() + TLS_DEADLINE_SEC)
    finally:
        listener.close()

    assert elapsed < TLS_DEADLINE_SEC + TLS_PROXY_DELAY_SEC * 0.6, (
        f"the TLS handshake ran {elapsed:.3f}s against a {TLS_DEADLINE_SEC}s deadline. The unfixed "
        f"code costs the {TLS_PROXY_DELAY_SEC}s spent before TLS PLUS the full deadline again, because "
        "the socket timeout restarts at the handshake instead of counting down what is left"
    )


def test_a_trickling_TLS_handshake_is_UNBOUNDED_without_the_deadline():
    """The discriminating control: the same fixture, no deadline, runs to the server's own limit.

    Without it the bound above could be the fixture giving up rather than the deadline biting -- which
    is precisely the mistake the previous TLS test made by using a `wrap_socket` that never blocked.
    The socket timeout is generous here on purpose: a timeout tighter than the deadline would do the
    bounding in BOTH arms, and the deadline's contribution would be unobservable.
    """
    listener = _listening(_serve_delayed_proxy_then_trickling_tls)
    try:
        elapsed = _handshake_through(listener.getsockname()[1], None)
    finally:
        listener.close()

    assert elapsed > TLS_DEADLINE_SEC * 3, (
        f"the handshake ended in {elapsed:.3f}s with no deadline, too fast to show the deadline is doing the work above"
    )


def test_the_connection_factory_the_early_arm_hooks_still_exists():
    """⚠️ If a future Python drops `_create_connection`, the early arm silently stops happening.

    It is also pinned as an INSTANCE attribute deliberately: that is why a subclass method of the
    same name would be shadowed and never called, which is the reason `connect()` wraps it in place
    rather than overriding it.
    """
    conn = http.client.HTTPConnection("127.0.0.1", 1)

    assert "_create_connection" in vars(conn), (
        "`HTTPConnection.__init__` no longer assigns `_create_connection`; the watchdog is now armed "
        "after the connection sequence again, and the round-4 defect is back"
    )
    conn.close()


@pytest.mark.timing
def test_the_abort_covers_a_bare_recv_not_only_a_makefile_stream():
    """⚠️ Both calls in `_abort_socket` are load-bearing, in phases that do not overlap.

    Measured on Windows, one abort at 0.25s: against a peer trickling a byte per 0.02s read through
    `makefile()` -- how `http.client` takes the status line and headers -- `shutdown` alone aborts in
    0.257s and `close` alone does **not** abort at all. Against a bare `recv` -- which is what the TLS
    handshake does -- it is exactly the reverse: `close` aborts in 0.254s, `shutdown` alone does not.

    Every other socket test in this file reads through `makefile()`, so dropping `close` would be
    invisible to all of them. This is the one that keeps it killable.
    """
    listener = _listening(_serve_idle)
    try:
        client = socket.create_connection(("127.0.0.1", listener.getsockname()[1]), timeout=2.0)
        threading.Timer(0.15, th._abort_socket, args=(client,)).start()
        started = time.monotonic()
        with pytest.raises(OSError):
            client.recv(16)
        elapsed = time.monotonic() - started
    finally:
        listener.close()

    assert elapsed < 1.0, (
        f"a bare recv ran for {elapsed:.3f}s despite the abort: only `close` interrupts one, so "
        f"`_abort_socket` must keep calling it as well as `shutdown`"
    )


CONNECT_TIMEOUT_SEC = 0.05
# TEST-NET-3 (RFC 5737): reserved for documentation and guaranteed not to be routed, so each of
# these costs a full connect timeout without depending on anyone's network being down.
UNROUTABLE = ("203.0.113.1", "203.0.113.2", "203.0.113.3")


def test_multiple_addresses_are_a_known_unbounded_phase(monkeypatch):
    """⚠️ A PIN ON A RESIDUAL, deliberately, not a bound. Review round 5, and it is NOT DNS.

    `socket.create_connection` walks every `getaddrinfo` result and applies the timeout to EACH
    candidate, so N unreachable A/AAAA records cost N timeouts before any socket -- and therefore any
    watchdog -- exists. `getaddrinfo` has already returned by then, so this is not the resolver
    residual that was documented; it was hidden behind it for three corrections of that prose.

    ⚠️ Every other fixture in this file connects successfully to ONE localhost address and so cannot
    observe an all-address failure. That is the fifth consecutive round in which the finding was the
    phase immediately before wherever the watchdog was armed, and the fifth in which no control could
    reach it. So the residual is written as an executable fact rather than a sentence: a sentence has
    been wrong four times, and **if someone bounds this phase, this test fails and makes them correct
    the claim** instead of leaving a stale one behind.
    """
    resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 9)) for addr in UNROUTABLE]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: resolved)

    deadline_sec = CONNECT_TIMEOUT_SEC + 0.01
    factory = th._with_deadline(th._DeadlineHTTPConnection, time.monotonic() + deadline_sec)
    conn = factory("unreachable.invalid", 9, timeout=CONNECT_TIMEOUT_SEC)
    started = time.monotonic()
    with pytest.raises(OSError):
        conn.connect()
    elapsed = time.monotonic() - started
    armed = conn._t2p_timer is not None
    conn.close()

    assert armed is False, (
        "a watchdog was armed during address iteration -- this phase may now be bounded, in which "
        "case fix the claim in `_request`'s phase table and in scripts/README.md, not this test"
    )
    assert elapsed > deadline_sec + CONNECT_TIMEOUT_SEC, (
        f"{len(UNROUTABLE)} unreachable addresses completed in {elapsed:.3f}s, inside the "
        f"deadline-plus-one-timeout ceiling. Either this phase became bounded -- update the claim -- "
        f"or the fixture stopped costing a timeout per candidate and no longer pins anything"
    )
