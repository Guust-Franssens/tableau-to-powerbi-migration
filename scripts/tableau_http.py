"""
purpose: the ONE hardened HTTP round trip every Tableau REST caller in this repo makes, so a
         server-controlled status line, redirect header or truncated error body can never reach an
         uncaught traceback carrying a reflected credential.
usage:   from tableau_http import NETWORK_ERROR_STATUS, _request, header_value

Why this module exists at all
-----------------------------
It is not factoring for its own sake. ``capture_tableau_oracle`` had a hardened request path and
``tableau_render_capability`` had **three** hand-rolled ones, and rounds 7, 8 and 9 of the #405 review
each found a different hole in the unhardened copies -- the HTTP reason phrase, a credential split
across the reason and the body, and finally ``http.client.HTTPException``, which is **not** an
``OSError`` and therefore slipped through every ``except (OSError, urllib.error.URLError)``. Measured
against a local one-request server on 2026-09-01, before this module existed:

===========================================  ============================================
probe                                        result
===========================================  ============================================
``HTTP/1.1 <PAT>`` (malformed status line)   exit 1, ``BadStatusLine`` **with the PAT**
``302`` with the PAT in ``Location``'s port  exit 1, ``InvalidURL`` **with the PAT**
the same shapes via the oracle's ``_request`` exit 1, ``[REDACTED]``, no leak
===========================================  ============================================

Three rounds of point-fixes each closed one call site and left the others. The defect was never a
missing exception type; it was that a second HTTP client existed. So there is now exactly one, and
adding a fourth caller means importing it rather than writing a fourth ``try``.

Why the primitive is named ``_request``
---------------------------------------
⚠️ **Load-bearing, not a style accident.** The redaction gate in ``tests/test_diagnostic_redaction.py``
decides what is response-derived from the *call name* -- ``TAINTING_CALLS`` contains ``"_request"``,
and ``taint_module`` seeds that name's return value as tainted. Rename this function, or import it
under an alias, and every call site silently stops being tainted: the gate keeps passing while
covering nothing. ``test_the_shared_request_primitive_keeps_the_name_the_gate_taints`` pins it.
Callers therefore use ``from tableau_http import _request`` -- a plain name, never
``tableau_http._request``, which pylint rejects as ``protected-access`` (W0212, measured).

What is and is NOT redacted here
--------------------------------
* **Response bodies pass through RAW.** Classification must see the unmodified text: ``redact`` is
  handed the human-chosen PAT *name*, and a short one rewrites Tableau's own error codes, so a
  ``401002`` mangled mid-string stops being recognisable. Redacting here would break control flow in
  ``classify_probe`` and ``classify_export_error``. Their callers redact at the point of *reporting*,
  through :func:`tableau_env.redacted_note`.
* **Every string this module AUTHORS is redacted**, because those are the ones that escape as an
  exception message with nobody downstream to scrub them. They are the whole finding.

Residual, stated rather than implied
------------------------------------
The exception *message* is server-controlled and is reported, redacted -- which is exactly what
``origin/master``'s ``_request`` does, so this is parity, not a regression. Literal redaction cannot
survive a credential the server SPLITS (``Location: http://<half1>:<half2>/`` puts only ``half2`` in
``InvalidURL``'s message). That residual is a property of :func:`tableau_env.redact` and is unchanged
here; the type name beside it is Python's, never the server's, so it cannot be steered.
"""

from __future__ import annotations

import http.client
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tableau_env import redacted_note  # noqa: E402  # pylint: disable=wrong-import-position

# No HTTP response arrived at all (reset, DNS failure, refused, timeout, malformed status line,
# unusable redirect). Distinct from every real status so a caller's retry policy can treat a reset
# connection and a gateway 503 alike without conflating either with a 2xx.
NETWORK_ERROR_STATUS = 0

# An output cap on a diagnostic this module authors. It bounds the OUTPUT of redaction, never its
# input -- `redacted_note` has already seen the whole message by the time this applies, so cutting it
# can only lose diagnostic text and can never leave a secret behind.
_EXC_CHARS = 500

# How much body is read per socket operation when a deadline is in force. Big enough that a healthy
# response costs a handful of reads, small enough that the clock is consulted often on a slow one.
# It only affects the DEADLINE path; without a deadline the body is still read in one call.
_BODY_CHUNK_BYTES = 65536


def _describe(exc: BaseException, redactor) -> bytes:
    """``Type: redacted message`` as bytes, for a caller expecting a response body.

    The type name is Python's own and cannot be influenced by the server. The message is
    attacker-influenceable and therefore goes through :func:`tableau_env.redacted_note`, which redacts
    the WHOLE value before anything truncates or strips it -- the ordering rule four earlier rounds
    each got wrong in a different place.

    Type-only was considered and rejected: it would discard the DNS/refused/timeout distinction,
    which is the common real failure an operator has to act on, and it would be *stricter* than
    ``origin/master`` rather than merely as strict, for a fragment residual master already carries.
    """
    return f"{type(exc).__name__}: {redacted_note(str(exc), redactor, limit=_EXC_CHARS)}".encode()


def header_value(headers: dict[str, str], name: str) -> str | None:
    """One header, matched case-insensitively.

    HTTP header names are case-insensitive on the wire, and ``http.client``'s own ``HTTPMessage``
    honours that. Flattening it to a plain ``dict`` here does not, so a server answering
    ``content-type: image/svg+xml`` would return ``None`` from a plain ``headers.get("Content-Type")``
    and the format check would silently lose its Content-Type evidence. The dict is kept (it is what
    callers store and compare) and the lookup is fixed instead.
    """
    if name in headers:
        return headers[name]
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


def _read_bounded(stream, deadline: float | None, timeout: float) -> bytes:
    """Read a response body, optionally under an END-TO-END deadline.

    ⚠️ ``timeout`` is a **socket-operation** timeout, not a deadline. It bounds how long one read may
    block with no data arriving; it says nothing about how long a response that keeps trickling may
    take in total. Measured against a local server sending one byte every 0.08s: ``timeout=0.1``
    returned **HTTP 200 after 0.420-0.479s**, 4-5x its nominal timeout, with no error at any layer.
    Any ceiling built on "one request cannot outlive its timeout" is therefore not a ceiling.

    With ``deadline`` supplied this reads in chunks and checks the clock between them, so a trickling
    body is abandoned instead of followed forever.

    ⚠️ **This bounds the BODY, and that is not the same as bounding the request** -- a distinction
    that cost a review round. Everything before the first body byte (connect, status line, headers,
    redirects) happens inside ``_open``, and is bounded by :class:`_DeadlineHTTPConnection`'s
    watchdog, not here. Read this docstring as the body half of a two-part mechanism; the request-level
    claim lives on :func:`_request`.

    Since the watchdog also aborts the socket at the same instant, this check is **defence in depth
    rather than an independent bound** on a real connection: it is what turns an abandonment into a
    diagnostic naming the deadline and the byte count instead of a bare ``ConnectionAbortedError``,
    and it is the only mechanism when the stream is not a socket at all. It is pinned on its own
    terms in ``tests/test_tableau_http_deadline.py`` with no socket in the fixture, because three
    mutations aimed at it SURVIVED once the watchdog existed -- a guard implied by a stronger sibling
    is unkillable, and this repository's rule is to pin such a guard independently or delete it.

    A body abandoned this way is NOT returned as a partial success: the ``TimeoutError`` raised here
    is an ``OSError``, so :func:`_request`'s existing handler turns it into
    :data:`NETWORK_ERROR_STATUS` -- a transient, retry-eligible failure. Reporting a truncated CSV as
    a 200 would manufacture exactly the false evidence this repository exists to prevent.

    ``deadline=None`` is the default and reads the whole body in one call, byte-for-byte as before, so
    no existing caller changes behaviour. That matters: the data leg legitimately streams a large
    export, and a deadline applied to it would truncate a capture that is making real progress.
    """
    if deadline is None:
        return stream.read()
    # ⚠️ `read1`, not `read`. `HTTPResponse.read(n)` blocks until it has n bytes or the stream ends,
    # so with a 64 KiB chunk a trickling 12-byte body is delivered in ONE call and the loop below
    # never gets to consult the clock -- measured: 0.970s against a 0.3s deadline, i.e. the deadline
    # did nothing. `read1` performs at most one underlying read and returns what has arrived, which is
    # what makes the check reachable. The fallback keeps a stream without `read1` working, with a
    # correspondingly looser bound; every stream this transport sees today has it.
    #
    # ⚠️ Both branches call the read primitive BY NAME, and that is load-bearing rather than stylistic.
    # `TAINTING_CALLS` in `tests/test_diagnostic_redaction.py` keys on the call name, so hoisting this
    # to `read_once = getattr(stream, "read1", None) or stream.read` -- which is what this was first
    # written as -- silently un-taints the response body and the gate stops covering it. Measured: the
    # `chunk` certification went STALE, meaning the body was no longer tracked at all. Same trap the
    # module docstring records for `_request`.
    has_read1 = callable(getattr(stream, "read1", None))
    chunks: list[bytes] = []
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"the response body was still arriving at its end-to-end deadline after "
                f"{sum(len(c) for c in chunks)} byte(s); a socket timeout of {timeout:.1f}s does not "
                f"bound a trickling response, so the read was abandoned rather than followed"
            )
        # Written as an if/else rather than a ternary on purpose: the taint analyser reads the CALL of
        # an assignment's value, and an `IfExp` is not a call, so `chunk = a() if p else b()` propagates
        # nothing. Caught by `test_the_body_read_primitive_keeps_the_name_the_gate_taints`, which is
        # the second time in this one function that ordinary refactoring quietly un-tainted the body.
        if has_read1:
            chunk = stream.read1(_BODY_CHUNK_BYTES)
        else:
            chunk = stream.read(_BODY_CHUNK_BYTES)
        if not chunk:
            # ⚠️ EOF is NOT proof the body is complete, and this check exists because CI proved it on
            # a platform this was not developed on. `_abort_socket` calls `shutdown(SHUT_RDWR)`; on
            # Windows an in-flight read then raises `ConnectionAbortedError`, but on Linux the read
            # returns **b"" -- a clean EOF**. So an aborted trickle was reported as a COMPLETE body
            # with `status 200`: the silent-corruption outcome, worse than the unbounded read this
            # whole mechanism replaced, and green on the developer's machine.
            #
            # The deadline is therefore re-checked at EOF as well as before each read. A body that
            # genuinely finished before the deadline still returns; one whose EOF arrived at or after
            # it cannot be told apart from an abort, so it is refused. Conservative on purpose: being
            # wrong here costs a retry, and being wrong the other way records a truncated CSV.
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"the response body ended at or after its end-to-end deadline, after "
                    f"{sum(len(c) for c in chunks)} byte(s). An abandoned read reaches EOF on some "
                    f"platforms rather than raising, so a complete body cannot be assumed here"
                )
            return b"".join(chunks)
        chunks.append(chunk)


class _DeadlineHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection whose socket is shut at an absolute deadline, whatever phase it is in.

    ⚠️ This exists because ``_read_bounded`` bounds the BODY and the claim was about the REQUEST.
    Everything before the first body byte -- DNS, connect, the status line, headers, redirects --
    happened under the per-socket-operation timeout alone, and a server trickling its HEADERS one
    byte at a time never trips that. Measured against a local server at one header byte per 0.02s
    with a 0.05s socket timeout: a 0.15s deadline returned after **1.378s**, 6.9x the ceiling being
    claimed. The eight real-socket tests could not see it, because every one of them sends headers
    immediately and only trickles the body -- the fixture could not reach the phase that was unbound.

    A watchdog rather than per-read timeout arithmetic, deliberately: ``http.client`` reads the status
    line and headers through a buffered file object created by ``makefile``, so re-arming
    ``settimeout`` per read would mean interposing on ``SocketIO`` and re-implementing its refcount
    contract. Aborting the socket is one call, works identically in every phase, and surfaces as an
    ``OSError`` that :func:`_request` already handles -- so an abandoned request is a transient
    failure, never a partial success.

    ⚠️ The deadline arrives as a CLASS attribute, set by :func:`_with_deadline`, rather than through
    an ``__init__`` taking ``**kwargs``. That is not style: ``tests/test_diagnostic_redaction.py``
    refuses ``*args``/``**kwargs`` in a guarded module because the taint analyser cannot follow them,
    and it caught the first version of this class. Forwarding by naming every parameter of
    ``HTTPConnection.__init__`` would duplicate a stdlib signature that varies by version; a
    per-request subclass needs no signature at all.
    """

    _t2p_deadline: float | None = None
    _t2p_timer: threading.Timer | None = None

    def connect(self) -> None:
        super().connect()
        self._t2p_timer = _arm_watchdog(self.sock, self._t2p_deadline)

    def close(self) -> None:
        if self._t2p_timer is not None:
            self._t2p_timer.cancel()
            self._t2p_timer = None
        super().close()


class _DeadlineHTTPSConnection(_DeadlineHTTPConnection, http.client.HTTPSConnection):
    """The TLS twin. The watchdog is armed after the handshake, on the wrapped SSL socket.

    ⚠️ Base order is load-bearing and was wrong once. With ``(HTTPSConnection, _DeadlineHTTPConnection)``
    the MRO finds ``HTTPSConnection.connect`` first, so the watchdog is never armed and every HTTPS
    request -- which is every real Tableau request -- is silently unbounded while the loopback HTTP
    tests pass. This order resolves ``connect`` to :class:`_DeadlineHTTPConnection`, whose
    ``super().connect()`` IS ``HTTPSConnection.connect``, so TLS still happens and the watchdog arms
    after it. ``test_the_https_twin_resolves_connect_to_the_watchdog`` pins the resolution, because no
    fixture here can complete a TLS handshake.
    """


def _with_deadline(base: type, deadline: float) -> type:
    """A one-off subclass of ``base`` carrying ``deadline``, so no constructor has to accept it."""
    return type(f"_Deadlined{base.__name__}", (base,), {"_t2p_deadline": deadline})


class _DeadlineHTTPHandler(urllib.request.HTTPHandler):
    """Routes ``http://`` through :class:`_DeadlineHTTPConnection`, carrying the deadline."""

    def __init__(self, deadline: float) -> None:
        super().__init__()
        self._deadline = deadline

    def http_open(self, req):
        return self.do_open(_with_deadline(_DeadlineHTTPConnection, self._deadline), req)


class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    """The ``https://`` twin. ``context`` is forwarded so certificate verification is unchanged."""

    def __init__(self, deadline: float) -> None:
        super().__init__()
        self._deadline = deadline

    def https_open(self, req):
        return self.do_open(_with_deadline(_DeadlineHTTPSConnection, self._deadline), req, context=self._context)


def _abort_socket(sock) -> None:
    """Force a blocked read on ``sock`` to fail, from another thread.

    ⚠️ ``close()`` alone is not enough on Windows, and that is measured rather than assumed: with
    ``close`` the watchdog fired, the timer was armed, and the header read still ran to completion --
    0.878s against a 0.15s deadline, with the eventual failure coming from the body check rather than
    the watchdog. Closing a socket decrements a handle; it does not interrupt a peer-blocked ``recv``.
    ``shutdown(SHUT_RDWR)`` tears down the connection in both directions and makes the pending read
    return immediately, on Windows and POSIX alike.

    Both calls are guarded: the socket may already be closed by the normal path, and a watchdog that
    raised here would surface on a timer thread where nothing can catch it.
    """
    for call in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
        try:
            call()
        except OSError:
            pass


def _arm_watchdog(sock, deadline: float | None) -> threading.Timer | None:
    """Abort ``sock`` at ``deadline``. Returns the timer so the caller can cancel it."""
    if deadline is None:
        return None
    timer = threading.Timer(max(deadline - time.monotonic(), 0.0), _abort_socket, args=(sock,))
    timer.daemon = True
    timer.start()
    return timer


def _open(req: urllib.request.Request, timeout: float, deadline: float | None):
    """``urlopen``, or a deadline-bounded equivalent when one is asked for.

    With no deadline this is byte-for-byte the previous behaviour, which is what keeps every other
    caller -- the data leg above all -- streaming a slow but progressing export untouched.

    With one, the request runs through an opener whose connections carry a watchdog, AND the socket
    timeout is narrowed to whatever is left, so the connect phase cannot outlive the deadline either.
    ``build_opener`` installs the same default handler chain ``urlopen`` uses, plus these two, so
    redirect and proxy behaviour is unchanged; a redirect simply opens a new connection, which arms a
    new watchdog against the SAME absolute instant.
    """
    if deadline is None:
        return urllib.request.urlopen(req, timeout=timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("the end-to-end deadline had already passed before the request was issued")
    opener = urllib.request.build_opener(_DeadlineHTTPHandler(deadline), _DeadlineHTTPSHandler(deadline))
    return opener.open(req, timeout=min(timeout, remaining))


def _request(
    req: urllib.request.Request, *, timeout: float, redactor, deadline: float | None = None
) -> tuple[int, bytes, dict[str, str]]:
    """One HTTP round trip. **Never raises** for anything the network or the server can do.

    Returns ``(status, body, headers)``. ``status`` is :data:`NETWORK_ERROR_STATUS` when no HTTP
    response arrived at all, and the real status otherwise -- including when reading the body failed
    mid-stream, because a 503 whose body read timed out is still usefully a 503 and stays
    retry-eligible.

    ``deadline`` is an absolute :func:`time.monotonic` instant and is the only thing here that bounds
    a request END TO END. ``timeout`` bounds one socket operation and nothing more -- neither a
    trickling body (:func:`_read_bounded`) nor trickling HEADERS (:class:`_DeadlineHTTPConnection`),
    and both of those were measured outliving it. It is opt-in because a deadline is a caller's
    policy, not the transport's: the data leg streams a real export and must not be truncated for
    making slow progress.

    ⚠️ What the deadline still does NOT bound, stated rather than implied: **name resolution**.
    ``getaddrinfo`` runs inside ``connect`` before any socket exists to watchdog, and the OS resolver
    ignores socket timeouts. A host that never resolves is bounded by the resolver, not by us.

    ``redactor`` is a **required** keyword, not an optional nicety: every caller either holds a
    credential or can be handed :func:`tableau_env.redact` itself, whose header rule scrubs a
    reflected ``X-Tableau-Auth`` value even with no secrets configured. Making it optional is how a
    call site forgets.

    Three exception surfaces, and all three have drawn blood:

    ``HTTPError``
        an ordinary 4xx/5xx. Reading its body is *itself* a socket read, so it is guarded too --
        Python does **not** route an exception raised inside one ``except`` clause to a sibling
        ``except`` of the same ``try``, so an unguarded ``exc.read()`` escapes this function entirely.
        It is read under the same deadline, because an ERROR body can trickle just as a success body
        can.
    ``OSError``
        covers ``URLError`` and so DNS failure, refused connection and timeout -- and now both
        deadline abandonments: the body check raises ``TimeoutError``, and the watchdog closes the
        socket out from under an in-flight read, which surfaces as an ``OSError`` too.
    ``http.client.HTTPException``
        ⚠️ **not an OSError**, and the round-9 finding. ``BadStatusLine`` carries the server's raw
        status line and ``InvalidURL`` carries a redirect's host/port -- both fully server-controlled,
        both raised straight through ``urlopen``, and neither caught by
        ``except (OSError, urllib.error.URLError)``. ``RemoteDisconnected`` and ``IncompleteRead``
        arrive here too. ⚠️ A watchdog that fires mid-header also lands here, as ``BadStatusLine`` or
        ``IncompleteRead`` -- which is why abandoning must be caught by BOTH clauses, not just OSError.
    """
    try:
        with _open(req, timeout, deadline) as resp:
            return resp.status, _read_bounded(resp, deadline, timeout), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = _read_bounded(exc, deadline, timeout)
        except (OSError, http.client.HTTPException) as read_exc:
            body = _describe(read_exc, redactor)
        return exc.code, body, dict(exc.headers or {})
    except (OSError, http.client.HTTPException) as exc:
        return NETWORK_ERROR_STATUS, _describe(exc, redactor), {}
