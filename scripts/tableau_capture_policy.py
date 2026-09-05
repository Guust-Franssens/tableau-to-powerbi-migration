"""
purpose: the bounds and status vocabulary the capture runs under -- nothing here touches a socket
usage:   imported by scripts/capture_tableau_oracle.py; no CLI of its own

Split out of ``capture_tableau_oracle`` for the third time that module hit its 1200-line pylint
ceiling, and on a real seam rather than a convenient one: this is the arithmetic that decides HOW
LONG anything may take, and every number in it relates to every other. Two review findings landed
squarely here -- a default budget that sat below its own admission floor for sub-second timeouts,
and a salvage ceiling that bounded attempts while claiming to bound wall clock -- and both were
hard to see precisely because the relationships were scattered through a 1200-line file.

⚠️ Nothing here talks to Tableau, holds a credential, or touches a response. That is what keeps it
out of ``tests/test_diagnostic_redaction.py``'s MODULES and what makes the seam acyclic: the
transport imports nothing from here, and ``capture_tableau_oracle`` imports these names back so
every existing caller and test keeps working.

The vocabulary matters and is easy to conflate, so it is stated once:

``timeout``
    a **socket-operation** timeout. It bounds how long one read may block with no data arriving. It
    does NOT bound a request -- a trickling response never times out (measured: HTTP 200 at 4.8x a
    0.1s timeout), which is why an end-to-end deadline is a separate mechanism in ``tableau_http``.
``budget_sec``
    a retry-**admission** deadline. It gates whether another attempt STARTS; an attempt already in
    flight may legitimately overrun it.
``SALVAGE_BUDGET_MULTIPLIER``
    an admission budget for the salvage sequence, paired with an end-to-end deadline so the
    wall-clock claim is enforced rather than assumed.
"""

from __future__ import annotations

import logging
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tableau_http import NETWORK_ERROR_STATUS  # noqa: E402  # pylint: disable=wrong-import-position

LOG = logging.getLogger("tableau-oracle")

DEFAULT_MAX_AGE_MINUTES = 1

REST_TIMEOUT_SEC = 180
SESSION_LOST_CODE = "401002"
# A **successful** response that echoes our own credential is refused outright rather than persisted.
# It is not a diagnostic status: nothing is written, because the alternative is a live credential in a
# `.csv` or `.svg` on disk, which no downstream redaction can reach.
CREDENTIAL_REFLECTED = "credential_reflected"
MAX_REAUTH_PER_VIEW = 2
DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE_SEC = 1.0
BACKOFF_CAP_SEC = 30.0
# The retry budget is a RETRY-ADMISSION DEADLINE, not a hard wall-clock cap. export() charges it from
# monotonic() BEFORE the first attempt, and only AFTER an attempt returns does it admit another retry
# -- while monotonic() is still inside the deadline. A socket timeout takes the whole REST_TIMEOUT_SEC
# to return, and _request() reports it as a *transient* NETWORK_ERROR_STATUS (it does not raise), so
# it is retry-eligible -- yet a budget at or below one REST_TIMEOUT_SEC is already spent by the time
# that full-timeout failure returns, so THAT failure (the commonest on a slow/proxied/VPN link) gets
# ZERO retries at any --max-attempts (issue #197, field-reported, reproduced with a virtual clock).
# The floor below which a full-timeout failure cannot be retried is therefore ONE timeout plus the
# first backoff -- NOT 2x. 2x is neither the minimum needed to admit a retry (a budget between 1x and
# 2x retries a slow failure fine) nor enough to fit two COMPLETE full-timeout attempts once backoff is
# counted (2*180 + backoff > 360). Faster transients -- an immediate 5xx, a short Retry-After -- still
# retry until the budget is spent, so a smaller budget is a deliberate choice, not a bug. The default
# sits comfortably above the floor; both are expressed via REST_TIMEOUT_SEC so they cannot drift.


def retry_admission_floor(timeout_sec: float) -> float:
    """The budget below which a failure that blocks for the FULL request timeout gets zero retries.

    A function rather than only a constant, because ``--rest-timeout`` makes the timeout a per-run
    value (#423). The floor tracks whatever timeout is actually in force;
    ``RETRY_ADMISSION_FLOOR_SEC`` is this function evaluated at the default, kept as a name because
    tests and prose refer to it.
    """
    return timeout_sec + BACKOFF_BASE_SEC


def default_retry_budget(timeout_sec: float) -> float:
    """The default retry-admission deadline for a given request timeout.

    ⚠️ It MUST move with ``--rest-timeout``. The two are expressed as a ratio precisely so they cannot
    drift, and pinning the budget at 360s while the operator raises the timeout to 600s would leave a
    budget BELOW one timeout -- which admits zero retries after the slow failure they raised the
    timeout to survive. That is the second of the two surprises #423 names.

    ⚠️ But 2x is NOT always above the admission floor, and review round 1 measured where it stops
    being: the floor is ``timeout + first backoff``, and the backoff is an ABSOLUTE 1.0s, so for any
    timeout below one second ``2 x timeout`` is under it -- ``0.25s -> budget 0.5s vs floor 1.25s``
    admits exactly one attempt, i.e. ZERO retries, which is the very footgun the default exists to
    avoid. ``--rest-timeout`` takes a float with no minimum, so this is reachable from the CLI.
    Taking the max of the two rules keeps the ratio where it dominates (every realistic timeout) and
    the floor where it does not, without forbidding a small timeout an operator may genuinely want.
    """
    return max(2.0 * timeout_sec, retry_admission_floor(timeout_sec))


RETRY_ADMISSION_FLOOR_SEC = retry_admission_floor(REST_TIMEOUT_SEC)
DEFAULT_RETRY_BUDGET_SEC = default_retry_budget(REST_TIMEOUT_SEC)

# Status 0 is our own marker for a network-level failure (reset, DNS, gateway timeout) that never
# produced an HTTP status at all. Tableau Cloud sits behind a gateway that intermittently 502/504s.
# Defined once, in `tableau_http`, beside the code that returns it; re-exported here because callers
# and tests read it off this module.
TRANSIENT_STATUSES = frozenset({NETWORK_ERROR_STATUS, 429, 500, 502, 503, 504})


def backoff_delay(attempt: int, retry_after: str | None = None, *, jitter: bool = True) -> float:
    """Exponential backoff with full jitter, honouring a server-supplied ``Retry-After``.

    Jitter matters even for a sequential capture: without it, a whole estate run that trips a rate
    limit retries in lockstep with any other client behind the same gateway.
    """
    if retry_after:
        try:
            return min(float(retry_after), BACKOFF_CAP_SEC)
        except ValueError:
            pass
    delay = min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
    return delay * (0.5 + random.random() / 2) if jitter else delay


@dataclass(frozen=True)
class RetryPolicy:
    """Bounds on recovery. Both bounds matter: attempts alone cannot stop a slow-failing endpoint
    from eating an estate run, and a wall-clock budget alone would allow an unbounded fast loop.

    ``budget_sec`` is a RETRY-ADMISSION DEADLINE, not a hard wall-clock cap: charged from before the
    first attempt, it gates whether ANOTHER retry is admitted, so an already-started attempt (or a
    nested re-auth) may finish past it. A value at or below one ``REST_TIMEOUT_SEC`` admits zero
    retries after a full-timeout failure (see the constant note above)."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    budget_sec: float = DEFAULT_RETRY_BUDGET_SEC


# One attempt, no retry budget. A salvage render runs only AFTER the data leg has already spent its
# full budget failing, and its job is to establish whether an image exists at all -- not to out-wait
# a view that has just demonstrated it cannot answer.
SALVAGE_RETRY = RetryPolicy(max_attempts=1, budget_sec=0.0)

# The SHARED wall-clock ceiling for all salvage legs of one view, as a multiple of the request
# timeout. `SALVAGE_RETRY` bounds attempts per leg; this bounds the sequence, which is the half review
# round 1 measured missing: three `format_mismatch` legs (a status that deliberately does not
# short-circuit) were attempted with no cross-leg limit -- 539.7s against a stated 180s bound.
#
# 2x rather than 1x, and the reason is the admission rule in `_salvage_exhausted`: a leg is admitted
# only while a WHOLE timeout still fits, so at 1x only the very first leg could ever run and a
# fast-failing tier (a version gate answered in milliseconds) could not be followed by another. At 2x
# a sequence of fast failures still reaches every tier, while any leg that actually consumes a full
# timeout leaves too little for a second. The ADMISSION ceiling is hard and independent of tier
# count. ⚠️ The WALL-CLOCK ceiling is NOT: each salvage leg also carries this instant into the
# transport as an end-to-end deadline, and a watchdog aborts the connection in every phase FROM THE
# FIRST LIVE SOCKET ONWARD -- proxy CONNECT, status line, headers, body. Before that socket exists
# there is nothing to abort, and two phases live there: name resolution, and `create_connection`
# walking every resolved address with the full timeout applied to each (measured, 0.177s against a
# 0.110s ceiling, timer not yet armed). So the transport deadline is HARDENING on top of admission,
# not a wall-clock guarantee -- see `tableau_http._request`'s phase table. The TLS handshake needs no
# watchdog: `SSLSocket` applies the socket timeout to the handshake as a whole, so a well-formed
# trickling record was refused at 0.204s on a 0.2s timeout. Admission alone would not bound wall
# clock at all: `urllib`'s timeout is per socket OPERATION, so neither a trickling body (HTTP 200 at
# 4.8x) nor trickling headers (1.378s against a 0.15s deadline) ever trip it.
SALVAGE_BUDGET_MULTIPLIER = 2.0


def build_retry_policy(
    max_attempts: int, budget_sec: float | None, timeout_sec: float = REST_TIMEOUT_SEC
) -> RetryPolicy:
    """Build the retry policy, warning when the budget is too small to retry a full-timeout failure.

    ``budget_sec`` is a retry-admission deadline, charged from before the first attempt, which can
    itself block for the full request timeout on a socket timeout. Below
    :func:`retry_admission_floor` (one timeout plus the first backoff) the deadline is already spent
    when such a failure returns, so it is retried zero times -- the issue #197 footgun.

    ``budget_sec=None`` means "the operator did not choose one", and it resolves to
    :func:`default_retry_budget` of the timeout ACTUALLY in force. That is the #423 half: with
    ``--rest-timeout`` exposed, a budget frozen at the default's 360s would silently fall below one
    request timeout the moment an operator raised the timeout past 360s -- removing every retry from
    exactly the slow failure they were trying to survive.

    This warns rather than clamps OR rejects, on purpose. A sub-floor budget is NOT incoherent: it is
    a deliberate, useful choice for FAST-failing transients (a tight budget cutting a long Retry-After
    loop short is exactly what ``test_retry_budget_stops_a_slow_failure_before_max_attempts`` relies
    on), so clamping would silently defeat it and rejecting would forbid it. The warning is therefore
    scoped to the one thing that is actually broken -- a failure that blocks for the *full* per-request
    timeout -- and says so, rather than the old, false blanket claim that nothing below 2x is retried.
    """
    if budget_sec is None:
        budget_sec = default_retry_budget(timeout_sec)
    floor = retry_admission_floor(timeout_sec)
    if budget_sec < floor:
        LOG.warning(
            "--retry-budget %.0fs is below the %.0fs needed to retry a failure that blocks for the "
            "full %.0fs request timeout (one timeout plus the first backoff), so such a failure will "
            "NOT be retried; faster transient failures still retry until the budget is spent",
            budget_sec,
            floor,
            timeout_sec,
        )
    return RetryPolicy(max_attempts=max_attempts, budget_sec=budget_sec)


def validate_max_age(value: Any) -> int:
    """Validate that a max-age value is a non-negative integer (in minutes).

    Tableau REST API parameter ``maxAge`` specifies the maximum cache age in minutes.
    Non-integer values, boolean values (since ``bool`` is a subclass of ``int`` in Python),
    and negative values are rejected.
    """
    if isinstance(value, bool):
        raise TypeError(f"max_age must be an integer >= 0, got bool: {value!r}")
    if not isinstance(value, int):
        raise TypeError(f"max_age must be an integer >= 0, got {type(value).__name__}: {value!r}")
    if value < 0:
        raise ValueError(f"max_age must be >= 0, got {value}")
    return value
