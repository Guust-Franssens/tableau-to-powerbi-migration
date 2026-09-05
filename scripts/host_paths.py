"""
purpose: THE single definition of "this text discloses a location on a host". One question, one
         normalisation, one module - imported by the repo's commit gate (`set_data_folder.py
         --check`), by the manifest redactor (`manifest_scope.redact_host_paths`) and by the package
         containment guard (`package_unit._declares_unsafe_path`), so a package can never ship what a
         commit could not.
usage:   from host_paths import discloses_host_path, discloses_host_location, HOST_PROFILE_PATH_RE
"""

from __future__ import annotations

import re
from urllib.parse import unquote

# A leaked absolute path under a user profile. Covers the forms that actually show up in this repo's
# artifacts: a drive-letter profile root in either separator, its JSON-escaped double-separator
# spelling, the UNC form, and the two POSIX profile roots.
#
# ⚠️ **`search`, never `match`, and that is the whole point of this module.** Three predicates in
# this repo each answered "IS this string a path?" and each was defeated by a prefix. Measured on
# PR #480 round 7, one string, one host path, three verdicts::
#
#     bare               -> refused
#     "HTTP 503: " + it  -> SHIPPED     (`classify_export_error` prepends the status, #480 B1)
#     '"' + it + '"'     -> SHIPPED     (quoted by a diagnostic)
#     file:/// + it      -> SHIPPED     (a URL form of the same location)
#
# A path parser is the wrong tool for prose: `retry_reasons[]` carries server-response text and is
# exactly where a real customer path arrives wrapped in a sentence. The question a shipped artifact
# has to answer is CONTAINMENT - "does this text disclose a host path ANYWHERE in it?"
#
# Only *syntactically unambiguous* placeholders are exempt - `...`, `<anything>`, `%ANY_VAR%` -
# because SECURITY.md and the READMEs have to show the pattern they warn about. Bare words like
# `user`, `you` or `username` are NOT exempt: they are all real, registrable account names.
#
# ⚠️ **This is the REPO COMMIT GATE's question, and it is deliberately NARROWER than the shipping
# one below** (round 9). It is `search`ed over every git-tracked file, so widening it to "any
# absolute location" would fail this repo on its own fixtures, docstrings and runbooks - which name
# build drives, UNC shares and POSIX roots on purpose. What ships to a CUSTOMER is a different and
# stricter question, and it is :func:`discloses_host_location`. The invariant this module exists for
# is one-directional and still holds by construction: the shipping predicate is a strict superset of
# this one (unioned in below, and asserted in `tests/test_package_unit.py`), so a package can never
# ship what a commit could not.
#
# ⚠️ The pattern is written so that it does NOT match its own source text, which is why this module
# needs no exemption from the gate it powers - `set_data_folder._check`'s allowlist is deliberately
# two names long and `tests/test_repo_layout.py` pins it at exactly those two. Verified by running
# `python scripts/set_data_folder.py --check` with this file tracked.
HOST_PROFILE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]{1,2}Users|\\\\[^\\/\"']+[\\/]{1,2}Users|(?<![\w.])/Users|(?<![\w.])/home)"
    r"[\\/]{1,2}(?!\.\.\.|<|%)[^\\/\"'\s]+",
    re.IGNORECASE,
)


def discloses_host_path(text: str) -> bool:
    """True when ``text`` reveals an absolute path under a user profile, WHEREVER it sits in it.

    Deliberately a containment test over the whole string rather than a parse of it, so a prefix
    (`HTTP 503: `), a suffix, a quote or a `file:///` wrapper cannot hide the disclosure.

    ⚠️ **This is the COMMIT GATE's question, not the shipping question** (round 9). It recognises a
    *profile* root only, and a profile root is a spelling rather than the property that matters to a
    customer deliverable. Use :func:`discloses_host_location` for anything that SHIPS; use this one
    only when gating the repo's own tracked files.
    """
    return isinstance(text, str) and bool(HOST_PROFILE_PATH_RE.search(text))


# --------------------------------------------------------------------------------------------
# the SHIPPING question: an absolute location on some host, in any spelling
# --------------------------------------------------------------------------------------------
#
# ⚠️ **#480 round 9. The predicate above matches a SPELLING, not the PROPERTY.** Rounds 3-8 each
# widened the guard by one shape and a different spelling escaped each time, because every widening
# added an alternative to a pattern instead of removing the ambiguity the pattern reads. Measured on
# the round-8 tip, one wrapper (`HTTP 503: ` - which is how `classify_export_error` writes
# `retry_reasons[]`) over locations that are all absolute::
#
#     D:\builds\out\secret.log                                -> SHIPPED
#     \\customer-server\finance-share\secret.log              -> SHIPPED
#     /var/lib/tableau/secret.log                             -> SHIPPED
#     \\server\C$\Users\<a real account>\private\secret.log   -> SHIPPED  (the account name too)
#     C%3A%5CUsers%5C<a real account>%5Cprivate%5Csecret.log  -> SHIPPED  (the same, re-alphabeted)
#
# The last two are the decisive ones: a REAL profile path with a REAL account name, spelled as an
# administrative-share UNC and as a percent-encoded string. The profile regex is a spelling test, so
# the same secret walks past it in a different alphabet. The first also contradicts this PR's own
# anchor, which states that a build drive names the operator's machine and must be refused - and a
# location cannot become acceptable merely because a status prefix precedes it.
#
# **So: NORMALISE FIRST, THEN ASK ONE QUESTION** - never a special case per spelling, which is what
# produced six rounds. Normalisation folds the alphabet (percent-encoding, both separator
# conventions, redundant separators, remote URLs); the grammar then names the three ways a string can
# be *rooted on a host*, which is a closed set:
#
#     drive root   X:/...             any drive, either separator, any number of them
#     UNC root     //host/share/...   administrative shares and the extended `//?/...` form included
#     POSIX root   /var/... /home/... rooted at a filesystem root NAME
#
# The POSIX arm is the only one that needs a vocabulary, and it needs one for a measured reason: a
# bare `/segment/` is indistinguishable from a REST route (`/api/2.4/sites/...`), which this repo's
# own oracle capture writes into its diagnostics. The vocabulary is the FHS top level plus the macOS
# roots - a closed, published list, not an enumeration of observed leaks.

#: The POSIX filesystem roots: FHS top level + the macOS roots. A rooted path starting at one of
#: these names a location on a host; a rooted path starting at anything else is far likelier to be a
#: REST route, which is why this arm is a vocabulary rather than `/[^/]+/`.
_POSIX_ROOTS = (
    "Applications",
    "Library",
    "System",
    "Users",
    "Volumes",
    "bin",
    "boot",
    "dev",
    "etc",
    "home",
    "lib",
    "media",
    "mnt",
    "opt",
    "private",
    "proc",
    "root",
    "run",
    "sbin",
    "srv",
    "sys",
    "tmp",
    "usr",
    "var",
)

#: A URL whose authority is a NETWORK HOST names a remote resource, not a location on a machine, so
#: it is excised before the question is asked - otherwise `https://host/var/lib/x` reads as a POSIX
#: path and the operator loses a legitimate diagnostic. Two deliberate limits:
#:
#: * `file:` is NOT excised. A `file:` URL is by definition a local path, and `file:///` + a profile
#:   path is one of the four spellings round 7 measured escaping.
#: * the token stops at `?` and `#`, so a query string cannot carry a location out inside a URL.
#: * the scheme must be at least TWO characters. A one-letter "scheme" before `//` is a drive root
#:   with a redundant separator, which Windows resolves as a drive root and this excision would
#:   otherwise delete outright - measured: `D://builds//out/x.log` was excised as a URL and shipped.
_REMOTE_URL_RE = re.compile(r"(?!file:)[A-Za-z][A-Za-z0-9+.\-]+:/{2}(?!/)[^\s\"'<>?#]*", re.IGNORECASE)

#: `%XX` decoding is looped so a double-encoded spelling (`%255C`) folds too. Bounded and monotone:
#: it stops as soon as a pass changes nothing, so it always terminates.
_PERCENT_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_MAX_DECODE_PASSES = 3

#: ONE rooted location, captured whole so the placeholder test below can read the segments that
#: follow the root. The trailing charset stops at whitespace and quotes only - `<` and `>` stay
#: inside the token on purpose, because a `<placeholder>` segment is what makes a template a
#: template. The leading lookbehind is what keeps a dotted version string, a relative `../logs` and a
#: mid-token colon from rooting anything.
_HOST_LOCATION_RE = re.compile(
    r"(?<![A-Za-z0-9.])"
    r"(?P<location>"
    r"(?:"
    r"[A-Za-z]:/+"
    r"|/{2,}[^\s\"'/]+/+"
    r"|/+(?:" + "|".join(_POSIX_ROOTS) + r")/+"
    r")"
    r"[^\s\"'/][^\s\"']*"
    r")",
    re.IGNORECASE,
)

#: A location containing a syntactically unambiguous placeholder is a TEMPLATE and discloses nothing
#: - `SECURITY.md` and the READMEs have to be able to show the shape they warn about, and the
#: packager must exempt it identically or the two definitions have re-diverged.
#:
#: ⚠️ **Named residual, stated rather than implied.** This exempts a placeholder ANYWHERE in the
#: location, so a hostile manifest could still ship a build root decorated with one. It cannot rescue
#: a *profile* disclosure, because :func:`discloses_host_path` is unioned in below and applies its
#: own, root-adjacent exemption. The alternative - exempting only a root-adjacent placeholder - would
#: refuse the `<drive>:\Users\<account>\data` spelling this repo publishes in SECURITY.md, so it
#: would trade a documented false negative for an undocumented false positive.
_PLACEHOLDER_RE = re.compile(r"\.\.\.|<[^<>]*>|%[A-Za-z_][A-Za-z0-9_]*%")


def _normalised(text: str) -> str:
    """``text`` with the alphabet folded: percent-decoded, one separator, remote URLs excised.

    Everything a spelling test can be defeated by is removed HERE, exactly once, so the grammar in
    :data:`_HOST_LOCATION_RE` can describe locations rather than the ways of writing them.
    """
    decoded = text
    for _ in range(_MAX_DECODE_PASSES):
        if not _PERCENT_RE.search(decoded):
            break
        widened = unquote(decoded)
        if widened == decoded:
            break
        decoded = widened
    return _REMOTE_URL_RE.sub(" ", decoded.replace("\\", "/"))


def discloses_host_location(text: str) -> bool:
    """True when ``text`` contains an ABSOLUTE LOCATION ON SOME HOST, in any spelling.

    This is the question every customer-shipped artifact is judged by, and it is a strict superset of
    :func:`discloses_host_path`: a profile path is one kind of absolute location, and the narrow
    predicate is unioned in rather than reimplemented, so the two can never disagree about it.

    *Absolute* means rooted on a host, which is three shapes and no more - a drive root, a UNC root,
    a POSIX filesystem root. It is deliberately NOT "is this string a path" (a prefix defeats that,
    and that was rounds 3-7) and deliberately NOT a list of observed spellings (a new spelling
    defeats that, and that was round 8): the spelling is folded away by :func:`_normalised` first,
    and then one grammar is asked once.
    """
    if not isinstance(text, str):
        return False
    if HOST_PROFILE_PATH_RE.search(text):
        return True
    return any(
        not _PLACEHOLDER_RE.search(found.group("location")) for found in _HOST_LOCATION_RE.finditer(_normalised(text))
    )
