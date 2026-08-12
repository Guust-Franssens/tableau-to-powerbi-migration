"""
purpose: One place to read a git-ignored ``.env`` and to bridge Tableau credentials into the
         deterministic engine's own scripts.
usage:   from tableau_env import resolve_env, require, pat_secret, engine_child_env

Our tier authenticates to Tableau by **Personal Access Token only** (owner decision, 2026-08-11).
The engine keeps whatever it supports (JWT/Connected Apps, keyring); we deliberately do not mirror
it. One auth mode means one failure mode and one set of variables. The knowing trade: a PAT inherits
its owner's permissions and cannot be scoped down, there is no create-PAT API (Tableau answers HTTP
405), and a tenant whose policy forbids PATs cannot run our assessment at all.

Why this exists
----------------
The two tiers disagree on the name of the Tableau PAT *secret* env var: ours is documented (and used
by ``assess_estate.py``, ``capture_tableau_oracle.py``, ``stamp_tableau_provenance.py``) as
``TABLEAU_PAT_SECRET``; the engine's ``fetch_tds.py`` reads ``TABLEAU_PAT_VALUE``.
``TABLEAU_PAT_NAME`` is identical across both, so only the secret needs bridging.

That bridge already existed - correctly - in exactly one place (``harvest_estate_assets.py``, which
shells out to the engine), and nowhere else, so any caller of an engine script that did not go
through ``harvest_estate_assets.py`` hit the mismatch: a ``.env`` written from OUR docs works against
OUR scripts and fails authentication against the ENGINE's.

Four scripts each carried their own copy of ``load_env`` before this module existed
(``assess_estate.py``, ``capture_tableau_oracle.py``, ``harvest_estate_assets.py``,
``stamp_tableau_provenance.py``). This is the one shared implementation; ``pat_secret`` makes the read
tolerant of either name, and ``engine_child_env`` promotes the bridge from a local fix to the shared
rule for any future caller of an engine script.

Two scripts were then found broken in OPPOSITE directions, which is why ``resolve_env`` exists:
``tableau_lineage.py`` read ``os.environ`` directly and so ignored ``.env`` entirely, while
``assess_estate.py`` read only the file and raised ``KeyError`` when the same values were exported
into the shell instead. ``resolve_env`` layers the file over the process environment so either setup
works, and normalises the ``TABLEAU_SERVER`` / ``TABLEAU_SERVER_URL`` split that made the lineage
script reject a ``.env`` written from our own ``.env.example``.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

# The secret half of the PAT credential, named differently by each tier. Read tolerant of either;
# when a ``.env`` sets both, ours wins because it is the name we document and the one most likely to
# have been deliberately set (e.g. after copying from an older `.env`).
_PAT_SECRET_KEYS = ("TABLEAU_PAT_SECRET", "TABLEAU_PAT_VALUE")

# Canonical first, accepted aliases after. An alias is honoured (an existing `.env` must keep
# working) but warned about, so a second name never becomes a second convention.
_SERVER_URL_KEYS = ("TABLEAU_SERVER_URL", "TABLEAU_SERVER")

# The variable set our tier documents. `tests/test_tableau_env.py` pins this: three different
# divergences (`TABLEAU_PAT_VALUE`, `TABLEAU_SERVER`, and whatever is next) have already reached
# main, so the guard against a fourth matters more than any individual fix.
CANONICAL_ENV_KEYS = (
    "TABLEAU_SERVER_URL",
    "TABLEAU_SITE",
    "TABLEAU_PAT_NAME",
    "TABLEAU_PAT_SECRET",
    "TABLEAU_REST_API_VERSION",
    "TABLEAU_PRODUCT_VERSION",
)

# Every accepted spelling, canonical or alias. A name outside this set in a Tableau-auth script is a
# new divergence.
ACCEPTED_ENV_KEYS = frozenset(CANONICAL_ENV_KEYS) | {"TABLEAU_SERVER", "TABLEAU_PAT_VALUE"}


def load_env(path: Path) -> dict[str, str]:
    """Read a git-ignored KEY=VALUE file. Secrets are never logged or written to the store.

    A missing file is not an error - env vars may already be set in the process environment - so this
    returns an empty dict rather than raising.
    """
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def pat_secret(env: dict[str, str]) -> str:
    """Return the PAT secret regardless of which tier's variable name supplied it.

    Empty string, not KeyError, when neither is set - callers that require it (e.g. building a
    Tableau ``Site`` client) already raise their own clear error keyed on the FIRST name they check;
    this only removes the tier-naming trap from that check.
    """
    for key in _PAT_SECRET_KEYS:
        if env.get(key):
            return env[key]
    return ""


def server_url(env: dict[str, str]) -> str:
    """Return the Tableau base URL, accepting the deprecated ``TABLEAU_SERVER`` spelling.

    Canonical is ``TABLEAU_SERVER_URL``. ``TABLEAU_SERVER`` was used by ``tableau_lineage.py`` alone
    and is honoured with a warning so an existing `.env` keeps working; empty string when neither is
    set, so ``require`` can report every missing name at once instead of one per run.
    """
    for key in _SERVER_URL_KEYS:
        if env.get(key):
            if key != _SERVER_URL_KEYS[0]:
                warnings.warn(
                    f"{key} is deprecated; rename it to {_SERVER_URL_KEYS[0]} (see .env.example).",
                    DeprecationWarning,
                    stacklevel=2,
                )
            return env[key].rstrip("/")
    return ""


def _normalise(env: dict[str, str]) -> dict[str, str]:
    """Resolve alias spellings to canonical names WITHIN one source.

    Normalising per source is what makes precedence work. Merging the raw dicts first and
    normalising afterwards lets a canonical key in the LOSING source beat an alias in the winning
    one: a stale `.env` holding ``TABLEAU_SERVER_URL`` silently outranked a freshly exported
    ``TABLEAU_SERVER``, which defeats the whole rotation argument for process-wins.
    """
    out = dict(env)
    if not out.get("TABLEAU_SERVER_URL"):
        resolved = server_url(out)
        if resolved:
            out["TABLEAU_SERVER_URL"] = resolved
    if not out.get("TABLEAU_PAT_SECRET"):
        secret = pat_secret(out)
        if secret:
            out["TABLEAU_PAT_SECRET"] = secret
    return out


def resolve_env(path: Path | None = None, *, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Read credentials from a `.env` file, with the process environment taking precedence.

    This is what every Tableau-auth script should call. ``load_env`` alone reads only the file, which
    raised ``KeyError`` for anyone who exported the variables instead; reading only ``os.environ``
    ignores the `.env` we tell people to write. Both shapes shipped, in different scripts.

    Precedence: the file supplies **defaults**, an exported variable **overrides** them. Both orders
    have a stale-source failure, so the tie-break is which stale source is more recoverable: a shell
    export dies with the shell, while a `.env` persists indefinitely. The motivating case is token
    rotation -- a user who exports a freshly issued PAT to supersede a revoked one in `.env` must not
    silently keep authenticating with the revoked token. This also matches `python-dotenv`, which
    does not override an existing variable by default. Use ``env_source`` to report WHICH source won
    (never the value), because the real defect in either order is silence, not precedence.

    Each source is normalised BEFORE the overlay, so precedence holds even when the two sources spell
    the same value differently (a `.env` using our name, an export using the engine's).

    ``TABLEAU_SITE`` is canonicalised to ``""`` when absent: an empty site IS the documented Default
    site, so it is legitimately optional and ``require()`` does not demand it -- but two callers
    indexed it directly, turning a valid Default-site setup into a raw ``KeyError`` after
    ``require()`` had already passed.
    """
    base = dict(os.environ) if environ is None else dict(environ)
    merged = {**_normalise(load_env(path) if path is not None else {}), **_normalise(base)}
    merged.setdefault("TABLEAU_SITE", "")
    return merged


def require(env: dict[str, str], *keys: str) -> None:
    """Raise one actionable ``SystemExit`` naming every missing variable, not just the first.

    A credential error is the last thing a user sees before giving up, so it names the file to write,
    every name still missing, and the fact that a PAT cannot be created for them.
    """
    missing = [k for k in (keys or ("TABLEAU_SERVER_URL", "TABLEAU_PAT_NAME", "TABLEAU_PAT_SECRET")) if not env.get(k)]
    if not missing:
        return
    raise SystemExit(
        "Missing Tableau credential(s): "
        + ", ".join(missing)
        + "\n  Write them to a git-ignored .env (see .env.example) or export them:"
        + "\n    TABLEAU_SERVER_URL   e.g. https://10ax.online.tableau.com"
        + "\n    TABLEAU_SITE         site contentUrl ('' for Tableau Server's Default site)"
        + "\n    TABLEAU_PAT_NAME     Personal Access Token name"
        + "\n    TABLEAU_PAT_SECRET   Personal Access Token secret"
        + "\n  We authenticate by PAT only, and cannot create one for you (Tableau's API answers"
        + "\n  HTTP 405): a Tableau user with access must issue it."
    )


def env_source(key: str, path: Path | None = None, *, environ: dict[str, str] | None = None) -> str:
    """Say where ``key`` came from -- ``"environment"``, ``"file"``, or ``"unset"``. Never the value.

    Precedence is only dangerous when it is invisible: whichever order wins, the user who supplied
    the OTHER source believes theirs is in use. Callers log this at sign-in so a revoked-token
    failure names the source that supplied it.

    Alias-aware, and it has to be: it normalises each source exactly as ``resolve_env`` does, so a
    value exported under the engine's spelling is reported as coming from the environment rather
    than being missed and attributed to the file.
    """
    base = _normalise(dict(os.environ) if environ is None else dict(environ))
    if base.get(key):
        return "environment"
    if path is not None and _normalise(load_env(path)).get(key):
        return "file"
    return "unset"


def redact(text: str, *secrets: str) -> str:
    """Replace every occurrence of each secret with a marker, for output about to be persisted.

    We deliberately place a PAT in an engine child process's environment (``engine_child_env``), and
    ``harvest_estate_assets.py`` captures that child's stderr into ``parse-sweep.json``. A Tableau
    sign-in failure can echo the request body -- the engine raises with the first 500 characters of
    the response -- so a proxy, WAF or debug endpoint that reflects the request writes the owner's
    full-permission token to a durable artifact. Measured with an adversarial echo server during
    review of #97, not hypothesised.

    Scrub at the point of capture rather than trusting another tool's logging discipline: the secret
    is ours, the file is ours, and the engine's error text is not something we control.
    """
    for secret in secrets:
        # A short or empty value would redact unrelated text; a real PAT secret is far longer.
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
    return text


def engine_child_env(env: dict[str, str], base: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child process environment for invoking an ENGINE script (e.g. ``fetch_tds.py``).

    Sets ``TABLEAU_PAT_VALUE`` from whichever name our own ``.env``/environment supplied, so an
    engine script that only knows its own variable name still authenticates.
    """
    merged = {**(base if base is not None else dict(os.environ)), **env}
    merged["TABLEAU_PAT_VALUE"] = pat_secret(env) or merged.get("TABLEAU_PAT_VALUE", "")
    return merged
