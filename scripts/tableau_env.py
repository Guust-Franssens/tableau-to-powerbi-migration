"""
purpose: One place to read a git-ignored ``.env`` and to bridge Tableau credentials into the
         deterministic engine's own scripts.
usage:   from tableau_env import load_env, pat_secret, engine_child_env

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
"""

from __future__ import annotations

import os
from pathlib import Path

# The secret half of the PAT credential, named differently by each tier. Read tolerant of either;
# when a ``.env`` sets both, ours wins because it is the name we document and the one most likely to
# have been deliberately set (e.g. after copying from an older `.env`).
_PAT_SECRET_KEYS = ("TABLEAU_PAT_SECRET", "TABLEAU_PAT_VALUE")


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


def engine_child_env(env: dict[str, str], base: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child process environment for invoking an ENGINE script (e.g. ``fetch_tds.py``).

    Sets ``TABLEAU_PAT_VALUE`` from whichever name our own ``.env``/environment supplied, so an
    engine script that only knows its own variable name still authenticates.
    """
    merged = {**(base if base is not None else dict(os.environ)), **env}
    merged["TABLEAU_PAT_VALUE"] = pat_secret(env) or merged.get("TABLEAU_PAT_VALUE", "")
    return merged
