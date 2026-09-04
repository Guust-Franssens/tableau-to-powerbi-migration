"""
purpose: THE single definition of "this text discloses a host path". One question, one regex, one
         module - imported by the repo's commit gate (`set_data_folder.py --check`), by the manifest
         redactor (`manifest_scope.redact_host_paths`) and by the package containment guard
         (`package_unit._declares_unsafe_path`), so a package can never ship what a commit could not.
usage:   from host_paths import discloses_host_path, HOST_PROFILE_PATH_RE
"""

from __future__ import annotations

import re

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
    (`HTTP 503: `), a suffix, a quote or a `file:///` wrapper cannot hide the disclosure. It is the
    complement of a path-shaped predicate, not a replacement for one: "may I resolve and copy this?"
    is still a parse question (:func:`package_unit._declares_non_relative`), while "may this text
    ship to a customer?" is this one.
    """
    return isinstance(text, str) and bool(HOST_PROFILE_PATH_RE.search(text))
