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
# Dialog family (issue #376), from `refresh_pbip_model._emit_dialog_finding` /
# `probe_desktop_query._emit_dialog_finding`. The child looked at a visible Desktop dialog and is
# saying, authoritatively, "this is NOT a sign-in wall and I could not probe".
#
# It MUST be matched structurally and BEFORE the free-text scans (#400 review, finding 2). Without
# this, the parent fell through to `CREDENTIAL_MARKERS`, which scans the whole transcript - so
# `DIALOG_NEEDS_HUMAN` carrying the evidence excerpt `Authentication required` was relabelled
# `NO_CREDENTIAL` on the word "authentication", overriding the child's explicit statement to the
# contrary and firing the "a human must sign in; terminate the run" directive. Measured in review.
#
# Adding the tokens to CREDENTIAL_STOP_VERDICT_RE instead would have been the other wrong answer: it
# would assert the very credential wall the child says it did not see. These map to ERROR - "the probe
# itself could not run" - which keeps the gate armed and claims nothing about the source.
# `tests/test_probe_live_source_verdict.py` gates this list against the detector's own verdict table,
# so a token added there cannot stay unknown here.
DIALOG_VERDICT_RE = re.compile(
    r"^\s*(?:REFRESH|PREFLIGHT|PROBE):\s+"
    r"(?P<token>REFRESH_IN_PROGRESS|DIALOG_NEEDS_HUMAN|DIALOG_UNRECOGNIZED|DIALOG_UNREADABLE)\b"
)


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


def _dialog_verdict_token(text: str) -> str | None:
    """The dialog verdict token on a machine-readable verdict LINE, or ``None`` (issue #376).

    Matched on the verdict LINE, never a substring, for the same reason as every other family here -
    but this one is load-bearing in the opposite direction: it exists so the parent STOPS free-text
    scanning once the child has authoritatively said "a dialog is up, and it is not a sign-in wall".
    The child's own evidence excerpt can legitimately contain a credential keyword (the blocking
    signature includes `Authentication required`), and the unanchored scan then contradicted the
    verdict it was quoting.
    """
    for line in text.splitlines():
        match = DIALOG_VERDICT_RE.match(line)
        if match is not None:
            return match.group("token")
    return None


def _has_dialog_verdict(text: str) -> bool:
    """True when a line is a machine-readable dialog verdict (issue #376)."""
    return _dialog_verdict_token(text) is not None


# Every authoritative non-success verdict the child can emit. A run that carries ANY of them did not
# earn a clear, whatever else its transcript says. Kept as one list so a new verdict family is wired
# into the success gate by adding it here, not by lengthening a boolean chain someone has to read.
NON_SUCCESS_VERDICT_CHECKS = (
    _has_credential_stop_verdict,
    _has_desktop_gone_verdict,
    _has_desktop_unready_verdict,
    _has_dialog_verdict,
)


def _is_earned_success(text: str, table: str, *, returncode: int) -> bool:
    """Did this child run earn a clear for ``table``?

    Both channels must agree (issue #152): a zero exit code AND a machine-readable success verdict for
    the very table we asked about - and no authoritative non-success verdict anywhere in the
    transcript. The child prints its reassuring no-dialog banner on failure paths too, so the text can
    look fine while the run failed; and a non-zero exit must never read as success even beside a stale
    OK line.
    """
    if returncode != 0:
        return False
    if any(check(text) for check in NON_SUCCESS_VERDICT_CHECKS):
        return False
    return _has_data_ok_verdict(text, table)


def classify_child_verdict(text: str, raw: str) -> tuple[str, str] | None:
    """Map an AUTHORITATIVE child verdict line to ``(verdict, detail)``, or ``None``.

    These are checked before any free-text scan, because the child looked at the machine and is
    telling us what it saw; a substring scan of the transcript can only contradict it. All three map
    to ``ERROR`` - "the probe itself could not run" - which keeps the gate armed and claims nothing
    about the data source.

    Extracted from ``probe_live_source._classify_failure`` when the #376 dialog family pushed that
    module past pylint's ``max-module-lines``. The seam is real rather than convenient: everything
    here is decided by a verdict LINE, and everything left there is decided by free text.
    """
    if _has_desktop_gone_verdict(text):
        return (
            "ERROR",
            "Power BI Desktop exited or crashed before the probe could query the source: it "
            "enumerated no windows and its process was no longer running, so nothing was learned "
            "about the data source. This is a LOCAL tooling failure - do not report it as "
            "UNREACHABLE or a connection/credential problem. Re-open the model in Power BI Desktop, "
            "confirm it is running, and re-run the probe. Raw: " + raw,
        )
    if _has_desktop_unready_verdict(text):
        return (
            "ERROR",
            "Power BI Desktop was running without any window, so the probe could not inspect its "
            "local state or query the source. Wait for Desktop to finish starting, or restart it if "
            "it is wedged, then re-run the probe. Raw: " + raw,
        )
    token = _dialog_verdict_token(text)
    if token is not None:
        return (
            "ERROR",
            f"Power BI Desktop has a dialog up that the probe could not account for ({token}), so the "
            "refresh never established anything about the data source. The child classified the "
            "window and reports that it is NOT a sign-in prompt - do not send anyone to "
            "re-authenticate on the strength of this. Look at the Desktop screen, settle whatever it "
            "is showing, and re-run the probe. Raw: " + raw,
        )
    return None


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
