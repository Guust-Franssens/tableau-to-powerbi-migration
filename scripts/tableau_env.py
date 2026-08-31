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
The two tiers use different names for the Tableau PAT *secret*: ours is ``TABLEAU_PAT_SECRET`` and
the engine's ``fetch_tds.py`` reads ``TABLEAU_PAT_VALUE``. ``TABLEAU_PAT_SECRET`` is the one
documented spelling; the engine spelling remains accepted for existing environments and is mirrored
silently for the engine.

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
import re
import warnings
from pathlib import Path
from xml.etree import ElementTree

# The secret half of the PAT credential. TABLEAU_PAT_SECRET is canonical; the engine's historical
# TABLEAU_PAT_VALUE spelling stays accepted so existing environments keep working.
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
#
# `DATASOURCE_CREDENTIAL_KEYS` is a deliberately separate category: these are not how a script signs
# in to TABLEAU, they are the warehouse credentials a *published datasource* needs embedding at
# publish time (`provision_tableau_estate.py`). They share the `TABLEAU_` prefix only because that is
# how `.env` groups everything belonging to the trial site.
DATASOURCE_CREDENTIAL_KEYS = frozenset({"TABLEAU_SF_USER", "TABLEAU_SF_PASSWORD", "TABLEAU_DBX_TOKEN"})
ACCEPTED_ENV_KEYS = frozenset(CANONICAL_ENV_KEYS) | {"TABLEAU_SERVER", "TABLEAU_PAT_VALUE"} | DATASOURCE_CREDENTIAL_KEYS

_TABLEAU_AUTH_HEADER_RE = re.compile(r"(?i)([\"']?x-tableau-auth[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;<>]+)")

# Markers tried in order. A marker that CONTAINS a supplied secret would re-emit the credential it
# is meant to hide -- `redact("credential=[REDACTED]", "[REDACTED]")` used to return its input
# unchanged, and a two-character secret like "ED" survived inside every marker. The ladder shrinks
# the alphabet at each step and ends in the empty string, which cannot contain anything, so a marker
# is always available and deletion is the guaranteed-safe terminal case.
_MARKERS = ("[REDACTED]", "[HIDDEN]", "[###]", "")

# The length below which a secret starts to collide with ordinary words. It is a NOISE threshold and
# nothing else: `redact` warns here, it never skips. It used to be a skip, which meant a human-chosen
# warehouse password shorter than this was silently published verbatim (#381).
NOISY_SECRET_LEN = 8


def _wire_forms(secret: str) -> list[str]:
    """The shapes a secret takes on the wire, as the SUPPORTED transport serializer writes it.

    ``provision_tableau_estate.py`` hands warehouse credentials to ``tableauserverclient``, which
    builds its request with :mod:`xml.etree.ElementTree`. ``ElementTree.tostring`` defaults to
    ``us-ascii``, so a configured value holding a non-ASCII character (U+00E4, say) reaches the wire
    as a numeric character reference -- ``SYNTH-&#228;-PASS`` -- and searching for the literal finds
    nothing. XML metacharacters (``& < > "``) escape the same way. A reversible encoding of a
    credential in a persisted diagnostic is a leak, so both the attribute and text-node forms are
    redacted alongside the literal.

    **Deliberately NOT covered**, so the promise matches the code: case-changed, Unicode-normalised
    (NFD), percent-encoded, base64 and newline-split forms. Each survives redaction, and no call
    site in this repository emits one -- every caller feeds ``redact`` either an HTTP response body
    or an exception message from this XML client. Adding a speculative encoding ladder would be
    guesswork; adding one because a new transport appears would not.
    """
    forms = [secret]
    try:
        attribute = ElementTree.tostring(ElementTree.Element("s", {"v": secret})).decode("ascii")
        node = ElementTree.Element("s")
        node.text = secret
        text_node = ElementTree.tostring(node).decode("ascii")
    except (ValueError, TypeError):
        # A control character ElementTree refuses to serialise cannot reach a Tableau request body
        # either, so the literal is the whole of the exposure.
        return forms
    opening = attribute.index('v="') + 3
    forms.append(attribute[opening : attribute.index('"', opening)])
    forms.append(text_node[text_node.index(">") + 1 : text_node.rindex("<")])
    return forms


def _choose_marker(needles: list[str]) -> str:
    """The first marker that cannot re-emit any of the values being redacted."""
    for marker in _MARKERS:
        if not any(needle in marker for needle in needles):
            return marker
    return ""


def _blank_spans(text: str, needles: list[str]) -> list[tuple[int, int]]:
    """Every span to remove, measured against the ORIGINAL text.

    Both halves must be measured before anything is rewritten. Replacing literals first and then
    running the header rule let a secret inside the header NAME break the rule that protects the
    session token: redacting the ``Tableau`` in ``X-Tableau-Auth:`` stopped the header regex from
    matching, and the token -- which the call site does not separately know -- survived.
    """
    spans: list[tuple[int, int]] = [m.span(2) for m in _TABLEAU_AUTH_HEADER_RE.finditer(text)]
    for needle in needles:
        start = 0
        while (hit := text.find(needle, start)) >= 0:
            spans.append((hit, hit + len(needle)))
            start = hit + 1  # +1, not +len: "aa" must still be found twice inside "aaa"
    return spans


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collapse overlapping and touching spans into one.

    Ordering the alternatives longest-first is not enough, because a regex prefers an earlier match
    to a longer one: with ``"=S"`` and a long secret, ``password=SYNTHETIC-...`` lost its first
    character and kept the rest. Merging intervals is what makes overlap safe, and it is what lets
    the header rule and the literal rule coexist.
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


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
    secret = pat_secret(out)
    if secret:
        out["TABLEAU_PAT_SECRET"] = secret
        out["TABLEAU_PAT_VALUE"] = secret
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

    **There is no minimum length.** There was one -- 8 characters -- and it was sound while the only
    thing in scope was a machine-generated PAT. ``provision_tableau_estate.py`` then put a
    human-chosen warehouse password in scope, and a 7-character ``TABLEAU_SF_PASSWORD`` went straight
    through into ``manifest.json`` on a PUBLIC repository (#381). Measured before removing it, on
    reflected errors of the shape these callers actually persist:

    ============  =====  ==========  =================  ==================
    secret        chars  redactions  collateral (chars) diagnostic tokens
    ============  =====  ==========  =================  ==================
    25-character      25           1                 25  all kept
    ``Tr0ub4d``        7           1                  7  all kept
    ``ci``             2           3                  6  all kept
    ``e``              1          30                 30  HTTP status kept
    ============  =====  ==========  =================  ==================

    So the feared damage does not appear until 1-2 characters, and even there the output is merely
    noisy -- a false redaction is recoverable, a published credential is not. The 8 survives only as
    ``NOISY_SECRET_LEN``, a warning: nothing is silently unprotected any more.

    Two alternatives were measured and rejected. Redacting the whole word around a short match cost
    4-5x more text (24 chars vs 6 for ``ci``) while leaving a known-plaintext oracle almost as intact
    (5/7 vs 7/7 secrets uniquely recovered from a fixed error template), and its natural spelling,
    ``\\w*(?:secret)\\w*``, backtracks quadratically -- 826 ms on 16 KB, still running after 180 s on
    500 KB. Withholding the whole text instead destroys the diagnostic in every case, including the
    common one where the collateral is zero.

    Only ``""`` is skipped -- an optional credential that is simply not set, which every caller
    passes as ``... or ""``. A whitespace-only value **is** redacted: it was skipped here on the
    stated grounds that it "would match everywhere", which is false. Only an EMPTY pattern matches
    between every character; a non-empty whitespace value matches whitespace, and ``resolve_env``
    genuinely preserves a single-space password that the provisioner then treats as truthy.

    Everything is measured against the ORIGINAL text and replaced in ONE pass, because two of the
    three rules here can otherwise disable each other. See ``_blank_spans`` (a secret inside the
    ``X-Tableau-Auth`` header NAME used to stop the header rule protecting the session token),
    ``_merge`` (an overlapping short secret left all but one character of a long one visible) and
    ``_choose_marker`` (a marker that contains the secret re-emits it).
    """
    live = [secret for secret in secrets if secret]
    if not live:
        return _TABLEAU_AUTH_HEADER_RE.sub(r"\1" + _MARKERS[0], text)
    if min(len(secret) for secret in live) < NOISY_SECRET_LEN:
        warnings.warn(
            f"A configured secret is shorter than {NOISY_SECRET_LEN} characters. It IS redacted, "
            "but a short value also matches unrelated text, so persisted diagnostics may be "
            "noisy. Prefer a longer secret.",
            stacklevel=2,
        )
    needles: list[str] = []
    for secret in live:
        needles.extend(form for form in _wire_forms(secret) if form and form not in needles)
    spans = _merge(_blank_spans(text, needles))
    if not spans:
        return text
    marker = _choose_marker(needles)
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        out.append(text[cursor:start])
        out.append(marker)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def secret_forms(secret: str) -> list[str]:
    """Every on-the-wire spelling of ``secret`` that :func:`redact` knows how to find.

    Public because a *detector* needs the same vocabulary as the redactor. ``capture_tableau_oracle``
    refuses a SUCCESSFUL response body that echoes an authenticating credential, and a detector that
    knew fewer spellings than the scrubber would pass a payload the scrubber would then have had to
    clean -- which is the wrong order of defence, and unreachable anyway once the bytes are a file.
    """
    return _wire_forms(secret)


def redacted_note(value: str | bytes | None, redactor=None, *, limit: int, quote: bool = False) -> str:
    """THE ONE WAY attacker-influenced text may enter a diagnostic that is printed, raised or persisted.

    Every leak of a Tableau credential found in review of #405 was the same mistake in a different
    place, and it is a mistake of ORDER, not of omission:

    ==================================================  ==========================================
    what ran before the redactor                        what the redactor then failed to find
    ==================================================  ==========================================
    ``.lower()`` on a reflected ``Content-Type``         ``image/SYNTHETIC_TOKEN`` -> lowercase
    ``[:8]`` on the response's first bytes               a *prefix* of a longer secret
    ``.lstrip()`` on the body                            a secret whose literal begins with a space
    ``[:256]`` window before decoding                    a secret longer than the window
    ==================================================  ==========================================

    ``redact`` matches LITERALS. Case-folding, stripping, slicing and splitting each rewrite the
    needle out of the haystack, so redaction afterwards is looking for a string that is no longer
    there. The fix cannot be another careful call site -- four rounds of careful call sites is what
    produced the table above. It has to be impossible to express the wrong order, which is what this
    function is: the caller hands over the value **untransformed** and receives a finished string. It
    cannot truncate first, because truncation lives in here, after the redactor.

    ``limit`` and ``quote`` therefore describe the OUTPUT, never the input. Nothing shortens the text
    before ``redactor`` sees all of it -- deliberately including very large bodies, because any bound
    that could cut a secret is the defect this replaces, and this path only runs on a failure.

    ``quote`` wraps the result in :func:`ascii`, which is ASCII-safe on purpose: the decode below is
    lossy, and a literal U+FFFD in a message later printed to a cp1252 console raises
    ``UnicodeEncodeError``. Quoting AFTER redaction also fixes the reason ``repr(bytes)`` was wrong --
    it escapes quotes, backslashes and non-ASCII bytes, so a secret containing any of them arrived in
    an escaped form the redactor had never been shown.

    ⚠️ **Decoding is the one transformation that must precede redaction**, because ``redact`` takes
    ``str``. It is lossless for any secret that is valid UTF-8 on the wire -- which is every secret
    this repository handles, since a PAT is what we ourselves sent. A credential re-encoded in
    transit (base64, percent-encoding, NFD, a non-UTF-8 charset) survives this, exactly as
    :func:`redact` already documents; that residual is a property of the REDACTOR, and is unchanged.
    """
    text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else (value or "")
    # Redaction first, on the WHOLE value. Every line below this one is a transformation, and every
    # line above it must remain incapable of shortening or rewriting the text.
    if redactor is not None:
        text = redactor(text)
    text = text.strip()[:limit]
    return ascii(text) if quote else text


def scrub_tree(value, redactor, trail: str = "") -> tuple[object, list[str]]:
    """Redact every string in a JSON-shaped tree -- **keys included** -- returning ``(scrubbed, paths)``.

    A **SINK-side** guard, and deliberately unconditional: it does not know or care which field it is
    looking at, because the field nobody thought about is the one that leaks. Six rounds of review on
    #405 each fixed one SOURCE, and each time the next escape was somewhere nobody had enumerated --
    a successful CSV's first row, then an artifact FILENAME, then a dict KEY.

    Three properties are load-bearing, and the key one was missing until round 6:

    1. **Keys are scrubbed too.** ``format_hints`` is keyed by CSV column name, so a column matching
       the PAT name became a key, and a values-only walk put it in the manifest while dutifully
       redacting the identical string one field over in ``columns``.
    2. **A redaction-induced key COLLISION is resolved and reported, never silently dropped.** Two
       distinct keys can scrub to the same string; ``dict`` would keep the last and lose the rest,
       turning a redaction into data loss.
    3. **The reported path uses the SCRUBBED key.** Building it from the raw key would put the
       credential back into ``credential_scrubbed_at_sink`` -- the guard re-emitting what it caught.

    It returns the changed paths rather than scrubbing silently. A sink that quietly cleans up is
    indistinguishable from one that never had anything to clean, and that is exactly how a source
    defect survives: the artifact looks perfect either way.

    ⚠️ It is a **backstop, and cannot be the guarantee.** Bytes reach ``data/<luid>.csv`` and
    ``images/<luid>.svg`` before any manifest exists, so a scrub here would leave a credential in a
    file it can never reach. Refusing the payload at the seam, and building paths only from a verified
    LUID, are what cover those; the mechanisms are not redundant, they cover disjoint artifacts.
    """
    if isinstance(value, str):
        scrubbed = redactor(value)
        return scrubbed, ([trail or "<root>"] if scrubbed != value else [])
    if isinstance(value, dict):
        out: dict = {}
        hits: list[str] = []
        for key, item in value.items():
            safe_key, key_hit = _scrub_key(key, redactor, out)
            here = f"{trail}.{safe_key}" if trail else str(safe_key)
            if key_hit:
                hits.append(f"{here} (key)")
            out[safe_key], found = scrub_tree(item, redactor, here)
            hits.extend(found)
        return out, hits
    if isinstance(value, list):
        out_list, hits = [], []
        for index, item in enumerate(value):
            scrubbed, found = scrub_tree(item, redactor, f"{trail}[{index}]")
            out_list.append(scrubbed)
            hits.extend(found)
        return out_list, hits
    return value, []


def _scrub_key(key, redactor, taken: dict) -> tuple[object, bool]:
    """Redact one dict key, disambiguating a collision rather than letting a field vanish."""
    if not isinstance(key, str):
        return key, False
    scrubbed = redactor(key)
    if scrubbed == key:
        return key, False
    unique, suffix = scrubbed, 2
    while unique in taken:
        unique, suffix = f"{scrubbed}#{suffix}", suffix + 1
    return unique, True


def engine_child_env(env: dict[str, str], base: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child process environment for invoking an ENGINE script (e.g. ``fetch_tds.py``).

    Mirrors the canonical ``TABLEAU_PAT_SECRET`` and the engine's historical
    ``TABLEAU_PAT_VALUE`` spelling, so either tier and any of its child processes authenticate.
    """
    merged = {**(base if base is not None else dict(os.environ)), **env}
    secret = pat_secret(env) or merged.get("TABLEAU_PAT_SECRET") or merged.get("TABLEAU_PAT_VALUE", "")
    if secret:
        merged["TABLEAU_PAT_SECRET"] = secret
        merged["TABLEAU_PAT_VALUE"] = secret
    return merged
