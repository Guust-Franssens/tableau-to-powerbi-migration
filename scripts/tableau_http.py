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


def _request(req: urllib.request.Request, *, timeout: float, redactor) -> tuple[int, bytes, dict[str, str]]:
    """One HTTP round trip. **Never raises** for anything the network or the server can do.

    Returns ``(status, body, headers)``. ``status`` is :data:`NETWORK_ERROR_STATUS` when no HTTP
    response arrived at all, and the real status otherwise -- including when reading the body failed
    mid-stream, because a 503 whose body read timed out is still usefully a 503 and stays
    retry-eligible.

    ``redactor`` is a **required** keyword, not an optional nicety: every caller either holds a
    credential or can be handed :func:`tableau_env.redact` itself, whose header rule scrubs a
    reflected ``X-Tableau-Auth`` value even with no secrets configured. Making it optional is how a
    call site forgets.

    Three exception surfaces, and all three have drawn blood:

    ``HTTPError``
        an ordinary 4xx/5xx. Reading its body is *itself* a socket read, so it is guarded too --
        Python does **not** route an exception raised inside one ``except`` clause to a sibling
        ``except`` of the same ``try``, so an unguarded ``exc.read()`` escapes this function entirely.
    ``OSError``
        covers ``URLError`` and so DNS failure, refused connection and timeout.
    ``http.client.HTTPException``
        ⚠️ **not an OSError**, and the round-9 finding. ``BadStatusLine`` carries the server's raw
        status line and ``InvalidURL`` carries a redirect's host/port -- both fully server-controlled,
        both raised straight through ``urlopen``, and neither caught by
        ``except (OSError, urllib.error.URLError)``. ``RemoteDisconnected`` and ``IncompleteRead``
        arrive here too.
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except (OSError, http.client.HTTPException) as read_exc:
            body = _describe(read_exc, redactor)
        return exc.code, body, dict(exc.headers or {})
    except (OSError, http.client.HTTPException) as exc:
        return NETWORK_ERROR_STATUS, _describe(exc, redactor), {}
