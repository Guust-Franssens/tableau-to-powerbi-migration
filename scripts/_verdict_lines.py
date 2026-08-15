"""Structural matchers for the child refresh's machine-readable verdict LINES.

Extracted from ``probe_live_source`` (issue #159 deliberately pinned that module one line under
pylint's ``max-module-lines`` ceiling, and the #154/#158 verdict work then pushed it over). These are
pure, Desktop-independent functions and the regexes they use: each recognises a
``REFRESH:``/``PREFLIGHT:``/``PROBE:`` verdict LINE and never a substring of the surrounding
transcript (issues #152/#153), so a reassuring banner or a late worker log can never be mistaken for
the verdict. ``probe_live_source`` imports these so ``_classify_failure`` can stay focused on mapping a
recognised failure to a verdict; the seam tests reach them re-exported through ``probe_live_source``,
so these public names are load-bearing.
"""

from __future__ import annotations

import re

# The child (`refresh_pbip_model.py` / `probe_desktop_query.py`) speaks in machine-readable verdict
# LINES: `REFRESH:`/`PREFLIGHT:`/`PROBE:` followed by a token. These regexes match those lines
# structurally so the classifier never mistakes prose that merely NAMES a token (a reassuring banner,
# a doc excerpt, a late worker log) for the verdict itself. Kept as module constants so the matchers
# below - and the seam tests - can reference them by name.
#
# Success family, from `_verdict._emit_data_verdict`:
#   REFRESH: DATA_OK[ + PERSISTED]          model-level (whole-database refresh, canaries had rows)
#   REFRESH: TABLE_OK '<name>'[ + ...]       implicit single-table probe
#   REFRESH: TABLES_OK '<name>'[, '<n2>']    a --tables-narrowed refresh (what THIS probe elicits)
# The trailing `(?:\s+...)?$` keeps the anchoring lesson: `DATA_OK_FROM_WORKER` is NOT `DATA_OK`.
DATA_OK_VERDICT_RE = re.compile(
    r"^\s*(?:REFRESH|PREFLIGHT|PROBE):\s+(?P<token>DATA_OK|TABLE_OK|TABLES_OK)(?:\s+(?P<rest>.*))?$"
)
# Credential-stop family, from `_emit_credential_missing` / `_emit_blocked_by_dialog` /
# `_emit_credential_unknown`. Anchored on purpose (issue #153): a banner that lists these tokens in
# prose must NOT fabricate the verdict.
# `CREDENTIAL_UNKNOWN` (issue #154) is the latched, unrecoverable indeterminate outcome: the Desktop
# owner window went iconic, hiding its owned modal dialogs, and that evidence provably does not come
# back. It is a credential STOP (a human must settle it), so it is matched here STRUCTURALLY on the
# verdict line - never via the free-text CREDENTIAL_MARKERS scan in `probe_live_source` - and its
# detector reason is marker-free so it cannot be misclassified as BAD_TABLE / ACCESS_DENIED first.
CREDENTIAL_STOP_VERDICT_RE = re.compile(
    r"^\s*(?:REFRESH|PREFLIGHT|PROBE):\s+(?:CREDENTIAL_MISSING|CREDENTIAL_UNKNOWN|BLOCKED_BY_DIALOG)\b"
)
# Desktop-gone family (issue #158), from `refresh_pbip_model._emit_desktop_gone`. Anchored on the
# verdict line for the same reason as the credential family: the detector's marker-free reason prose
# must never be mistaken for the verdict. A DESKTOP_GONE verdict means the tracked process enumerated
# zero windows AND is no longer running, so the probe never reached the source - a LOCAL failure, not
# a fact about the warehouse. It is deliberately NOT in the credential-stop family (no human sign-in
# will fix a dead process); the classifier maps it to ERROR, never UNREACHABLE or NO_CREDENTIAL.
DESKTOP_GONE_VERDICT_RE = re.compile(r"^\s*(?:REFRESH|PREFLIGHT|PROBE):\s+DESKTOP_GONE\b")
# Desktop-unready family (issue #158), from `refresh_pbip_model._emit_desktop_unready` /
# `probe_desktop_query._emit_desktop_unready`. The live twin of DESKTOP_GONE: zero enumerated windows
# while the process is still RUNNING, so Desktop is starting up or wedged and its local state cannot
# be read. Also a LOCAL failure the classifier maps to ERROR - deliberately outside the credential-stop
# family, because no human sign-in makes a window-less process produce a window.
DESKTOP_UNREADY_VERDICT_RE = re.compile(r"^\s*(?:REFRESH|PREFLIGHT|PROBE):\s+DESKTOP_UNREADY\b")


def _has_credential_stop_verdict(text: str) -> bool:
    """True when a line is a machine-readable credential-stop verdict.

    ``CREDENTIAL_MISSING`` / ``BLOCKED_BY_DIALOG`` on a ``REFRESH:``/``PREFLIGHT:``/``PROBE:`` verdict
    line - never a substring of the transcript. Issue #153: the child's own reassuring "no blocking
    dialog" banner used to name these tokens in prose, and an unanchored scan let that message classify
    a successful refresh as ``NO_CREDENTIAL``. This is the structural half of the fix; the banner reword
    (``_credential_modal.print_refresh_banner``) is the belt-and-braces half.
    """
    return any(CREDENTIAL_STOP_VERDICT_RE.match(line) for line in text.splitlines())


def _has_desktop_gone_verdict(text: str) -> bool:
    """True when a line is a machine-readable ``DESKTOP_GONE`` verdict (issue #158).

    ``DESKTOP_GONE`` on a ``REFRESH:``/``PREFLIGHT:``/``PROBE:`` verdict line - never a substring of the
    transcript. The child emits it when Power BI Desktop enumerated zero windows AND its process is no
    longer running, so the probe never reached the source. Matched structurally (like the credential
    family) because the detector's reason prose is deliberately marker-free; keying on the verdict line
    keeps the classification independent of wording (issue #153).
    """
    return any(DESKTOP_GONE_VERDICT_RE.match(line) for line in text.splitlines())


def _has_desktop_unready_verdict(text: str) -> bool:
    """True when a line is a machine-readable ``DESKTOP_UNREADY`` verdict (issue #158).

    Same structural discipline as its DESKTOP_GONE sibling: matched on the verdict LINE, never on a
    substring of the transcript, so the detector's marker-free reason prose cannot be mistaken for the
    verdict itself (issue #153).
    """
    return any(DESKTOP_UNREADY_VERDICT_RE.match(line) for line in text.splitlines())


def _has_data_ok_verdict(text: str, table: str) -> bool:
    """True only when a machine-readable verdict LINE certifies real data for the probed ``table``.

    Accepts the whole success family the child emitter (``_verdict._emit_data_verdict``) can print:

      * ``DATA_OK``            - a model-level verdict (whole-database refresh, canaries returned rows)
      * ``TABLE_OK '<name>'``  - an implicit single-table probe
      * ``TABLES_OK '<name>'`` - a ``--tables``-narrowed refresh (what THIS probe ALWAYS elicits, since
        it always passes ``--tables <table>`` and never ``--canaries``)

    The last point is issue #152: before, this accepted only the literal ``DATA_OK``, which the probe's
    own argv could never make the child produce - so no source, credential, or config ever lifted the
    gate. ``TABLE_OK``/``TABLES_OK`` name the table(s) actually verified, so they are SCOPED: they clear
    the gate only when ``table`` is among the named tables. #115's guarantee is preserved - a verdict
    naming some OTHER table is a false certificate for this one and must not count.

    Matching is anchored to a verdict line, never a substring. A credential-blocked run once printed
    ``CREDENTIAL_MISSING`` while a background worker later printed a ``DATA_OK``-looking string; a
    substring scan would have falsely cleared the gate (see ``test_requires_data_ok_verdict_token``).
    """
    for line in text.splitlines():
        match = DATA_OK_VERDICT_RE.match(line)
        if match is None:
            continue
        if match.group("token") == "DATA_OK":
            return True
        # TABLE_OK / TABLES_OK name one or more single-quoted tables; the probed table must be one.
        # Known limitation (reviewed 2026-08-14, intentionally NOT fixed here): a table name containing
        # a literal apostrophe (e.g. "O'Brien Sales") is emitted UNESCAPED by ``_verdict``, so this
        # findall recovers the wrong tokens and ``table in named`` is False. It FAILS SAFE - it can only
        # ever keep the gate SHUT, never lift it wrongly - and a false lift is unreachable in the
        # single-table probe flow (the probe asks for exactly the table it names). The ambiguity
        # originates in the emitter's non-escaping, and the #115 scoping here is verified-correct, so the
        # matcher is left untouched rather than risk regressing it for a fail-safe, unreachable edge.
        named = re.findall(r"'([^']*)'", match.group("rest") or "")
        if table in named:
            return True
    return False
