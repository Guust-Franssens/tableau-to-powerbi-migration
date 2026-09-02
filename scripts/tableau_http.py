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
import sys
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
    body is abandoned instead of followed forever. The composition is what makes the bound real: each
    individual ``read`` is bounded by the socket timeout, and the loop is bounded by the deadline, so
    the total cannot exceed **deadline + one socket timeout** -- the in-flight chunk cannot be
    interrupted, and pretending otherwise would be the same false precision this fixes.

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
            return b"".join(chunks)
        chunks.append(chunk)


def _request(
    req: urllib.request.Request, *, timeout: float, redactor, deadline: float | None = None
) -> tuple[int, bytes, dict[str, str]]:
    """One HTTP round trip. **Never raises** for anything the network or the server can do.

    Returns ``(status, body, headers)``. ``status`` is :data:`NETWORK_ERROR_STATUS` when no HTTP
    response arrived at all, and the real status otherwise -- including when reading the body failed
    mid-stream, because a 503 whose body read timed out is still usefully a 503 and stays
    retry-eligible.

    ``deadline`` is an absolute :func:`time.monotonic` instant, and is the only thing here that
    bounds a request end to end -- ``timeout`` bounds one socket operation and nothing more (see
    :func:`_read_bounded`). It is opt-in because a deadline is a caller's policy, not the transport's:
    the data leg streams a real export and must not be truncated for making slow progress.

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
        covers ``URLError`` and so DNS failure, refused connection and timeout -- and now the
        deadline abandonment, which is a ``TimeoutError`` and therefore already handled here.
    ``http.client.HTTPException``
        ⚠️ **not an OSError**, and the round-9 finding. ``BadStatusLine`` carries the server's raw
        status line and ``InvalidURL`` carries a redirect's host/port -- both fully server-controlled,
        both raised straight through ``urlopen``, and neither caught by
        ``except (OSError, urllib.error.URLError)``. ``RemoteDisconnected`` and ``IncompleteRead``
        arrive here too.
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _read_bounded(resp, deadline, timeout), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = _read_bounded(exc, deadline, timeout)
        except (OSError, http.client.HTTPException) as read_exc:
            body = _describe(read_exc, redactor)
        return exc.code, body, dict(exc.headers or {})
    except (OSError, http.client.HTTPException) as exc:
        return NETWORK_ERROR_STATUS, _describe(exc, redactor), {}
