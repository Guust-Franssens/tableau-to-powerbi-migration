"""Credential modal detection shared by the PBIP refresh/query scripts.

All three regexes in ``*_signature.regex`` beside this file are the same signatures used by
``probe_desktop_credential.ps1``. Keep the detector's matching rules here and the signatures in those
resources so the fast Python checks and the PowerShell arbiter cannot drift.

⚠️ **A big window is NOT evidence of anything (issue #376).** Until this module was fixed,
``inspect_credential_modal`` returned the FIRST visible non-main window >= 100x100 as a
``blocking_dialog``, and both callers mapped that to ``BLOCKED_BY_DIALOG`` at **exit 1** - the same
hard-stop band as a real credential wall, which ``probe_live_source`` then classifies ``NO_CREDENTIAL``
("you may NOT build; a human must sign in"). A Power BI Refresh progress dialog satisfies >= 100x100
trivially, so a working refresh could halt a migration and send someone to a sign-in screen that was
never on show. That is the same defect #367 removed from the PowerShell arbiter, on a more dangerous
path: this detector feeds ``probe_desktop_query.py``, the gate of record.

**The burden of proof runs ONE WAY, exactly as in the arbiter:** a dialog is dismissed only when its
CONTENT positively reads as recognised progress status. It is never dismissed because the caption
looked reassuring, and never because we merely read *something*. Everything we could not account for -
unreadable, caption-only, unrecognised, or progress text mixed with prose - surfaces as its own
non-credential finding at **exit 3**, which is loud and recoverable, and never as a credential wall.

Three deliberate divergences from ``probe_desktop_credential.ps1``, each because this detector has
strictly LESS evidence than the arbiter (Win32 child-HWND text only; no UI Automation):

* **No prose join.** The arbiter joins non-interactive elements before matching the credential
  signature, which needs a control-type signal to exclude an interposed ``Cancel``. Win32 gives no
  control type, so the only join available here is the naive whole-window one the arbiter discarded in
  review - and a join can MANUFACTURE a phrase (two adjacent labels ``Account`` + ``Key`` join to the
  signature ``Account Key``). That error would land on the one verdict issue #376 says to err away
  from, so matching here stays per-element. The recall this costs routes to ``unrecognized`` /
  ``unreadable`` (exit 3, loud), and the arbiter - which HAS the control-type signal - is the
  escalation path.
* **No ``benign-unverified`` kind.** The arbiter needs it because a UIA harvest can be truncated while
  still returning text. Here a text read that throws fails the WHOLE enumeration
  (``Win32EnumerationError`` -> ``unknown_reason``), so a partial read cannot reach the classifier in
  the first place.

⚠️ **Blocking is decided from MODALITY, never from class, size or name (#400 review round 3).** An
earlier pass answered *"is this window blocking a human?"* with three correlates in turn - a
``>= 100x100`` size test, a ``WindowsForms10.Window.8`` class prefix, and an
``Internet Explorer_Hidden`` name allowlist - and native Win32 experiments defeated all three, each
time by collapsing a real blocker into the healthy state. Win32 answers the question directly: a modal
disables its owner. So :func:`main_frame`, :func:`is_proven_non_blocking` and :func:`renders_nothing`
decide from ``GetWindow(GW_OWNER)`` and ``IsWindowEnabled(owner)``, and the arbiter's one-way
enabled-owner exoneration is now ported here rather than skipped.

⚠️ **Frame identity is ENUMERATED, and ambiguity excludes nothing (#400 review round 5).** The one
thing this module does with the frame is EXCLUDE it, so a wrong identity is a silently missed prompt.
:func:`main_frame` therefore counts every rendering unowned window as a possible root - not only those
reachable through ownership chains, which is how an unowned credential host owning one enabled tooltip
was crowned the application - and returns ``None`` for anything else. ``Process.MainWindowHandle`` is
no authority either: measured, it is a Z-ORDER answer (see :func:`main_frame`).
"""

from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import wintypes
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

# Sibling module, resolved once the caller puts this scripts/ dir on sys.path (probe, refresh, and the
# test conftest all do). Reused rather than reimplemented for issue #158's zero-window liveness split:
# its bias errs toward "alive" on any ambiguity, so an uncertain Desktop routes to UNKNOWN-latched and
# never to a false crash - and it is already load-bearing and tested for stale-lock reclaim.
from _lock import _process_alive

SIGNATURE_PATH = Path(__file__).resolve().with_name("credential_modal_signature.regex")
BENIGN_SIGNATURE_PATH = Path(__file__).resolve().with_name("benign_dialog_signature.regex")
BENIGN_CHROME_SIGNATURE_PATH = Path(__file__).resolve().with_name("benign_chrome_signature.regex")
BLOCKING_SIGNATURE_PATH = Path(__file__).resolve().with_name("blocking_prompt_signature.regex")
CONNECTOR_SOURCE_RE = re.compile(
    r"\b(?P<kind>Sql\.Database|Snowflake\.Databases|Databricks\.[A-Za-z]+|Odbc\.DataSource|Web\.Contents)\s*\("
    r"\s*(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)')?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DesktopWindow:
    """Visible top-level Power BI Desktop window plus descendant Win32 text and its MODALITY facts.

    ``owner_hwnd`` / ``owner_enabled`` carry the only evidence that actually answers *"is this window
    blocking a human?"* (#400 review round 3). They are THREE-valued on purpose:

    * ``owner_enabled is True``  - the owner is enabled, which PROVES this window blocks nothing. A
      modal disables its owner; an enabled owner is therefore a positive exoneration.
    * ``owner_enabled is False`` - the owner is disabled. This does NOT convict: Power BI's own refresh
      dialog disables the owner too. It only means the exoneration does not apply.
    * ``owner_enabled is None``  - no owner, so the test did not apply. Not the same as passing it.

    Nine fields, waived rather than split: this is one Win32 window record and every field is read
    straight from the API. Grouping the modality pair behind a nested object would put an indirection
    between a reader and the two values three review rounds turned on.
    """

    # pylint: disable=too-many-instance-attributes
    title: str
    class_name: str
    width: int
    height: int
    texts: tuple[str, ...] = ()
    minimized: bool = False
    hwnd: int = 0
    owner_hwnd: int = 0
    owner_enabled: bool | None = None


@dataclass(frozen=True)
class CredentialModal:
    """Evidence that Power BI Desktop is blocked on a data-source credential dialog."""

    matched_text: str
    window: DesktopWindow

    @property
    def excerpt(self) -> str:
        """Short UI text excerpt suitable for a verdict line."""
        return self.matched_text[:80]


@dataclass(frozen=True)
class DialogFinding:
    """A candidate dialog we could NOT dismiss, plus the classification that says why.

    Replaces the old ``BlockingDialog`` (issue #376). The rename is the finding: this module can
    establish that a window is UP and what it SAYS, never that it BLOCKS anything - so every
    ``BLOCKED_BY_DIALOG`` it used to emit was an inference dressed as a finding, in the one verdict
    class the toolkit treats as a hard stop.

    ``kind`` is the classifier's own vocabulary (see :func:`classify_dialog`); ``verdict`` is the
    machine-readable token the callers print. Both are carried so a caller can print the token and a
    reader can still see which evidence produced it.
    """

    kind: str
    verdict: str
    window: DesktopWindow
    evidence: str = ""

    @property
    def excerpt(self) -> str:
        """Short UI text excerpt suitable for a verdict line."""
        return self.evidence[:80]


@dataclass(frozen=True)
class CredentialDetection:
    """Credential-dialog inspection result for a Desktop PID."""

    modal: CredentialModal | None = None
    dialog: DialogFinding | None = None
    unknown_reason: str | None = None
    desktop_unready: str | None = None
    process_gone: str | None = None
    windows: tuple[DesktopWindow, ...] = ()


class CredentialMissingError(RuntimeError):
    """Power BI Desktop is showing a data-source credential dialog."""

    def __init__(self, pid: int, modal: CredentialModal, source_hint: str | None = None) -> None:
        self.pid = pid
        self.modal = modal
        self.source_hint = source_hint
        super().__init__(describe_modal(modal, source_hint))


class DialogFoundError(RuntimeError):
    """Power BI Desktop is showing a dialog that could NOT be shown to be harmless (issue #376).

    Deliberately not named "blocked": this detector reads Win32 child-HWND text and can establish that
    a window is up and what it says, never that it blocks anything. It replaces ``DialogBlockedError``,
    whose ``BLOCKED_BY_DIALOG`` verdict sat in the exit-1 hard-stop band and was reached from a
    SIZE-ONLY test - so a Power BI Refresh progress dialog produced the same verdict as a sign-in
    prompt. Every outcome here is now exit 3: a human should look at the screen, but no sign-in is
    implied and nothing about the data source has been established.
    """

    def __init__(self, pid: int, finding: DialogFinding) -> None:
        self.pid = pid
        self.finding = finding
        super().__init__(describe_dialog_finding(finding))


class CredentialUnknownError(RuntimeError):
    """Power BI Desktop's blocking-dialog state stayed indeterminate right up to the deadline.

    Raised by :func:`join_with_credential_poll` when it LATCHED an indeterminate observation - the
    owner window went iconic, which hides its owned modal dialogs from enumeration - that a later
    ``none`` could not erase. It is a mandatory THIRD outcome, distinct from a detected block
    (:class:`CredentialMissingError` / :class:`DialogFoundError`) and from a healthy deadline:
    minimizing then restoring the owner destroys the dialog evidence for good (measured 2026-08-14,
    issue #154), so ``none`` after the fact is indistinguishable from a genuinely healthy Desktop and
    is NOT proof of health. Reporting a bare timeout here would blame a slow source for what is really
    an unobservable dialog no automation can fill.

    ``reason`` is the detector's own marker-free string, carried verbatim so the caller's verdict line
    cannot smuggle a ``CREDENTIAL_MARKER`` into the parent classifier's free-text scan (issue #153).
    """

    def __init__(self, pid: int, reason: str) -> None:
        self.pid = pid
        self.reason = reason
        super().__init__(f"desktop dialog state indeterminate for pid {pid}: {reason}")


class DesktopGoneError(RuntimeError):
    """Power BI Desktop enumerated ZERO windows AND the process is no longer running (issue #158).

    A live, working Desktop always owns at least its main window, so an empty enumeration is never
    evidence of health - it means the process has exited, crashed, or (while still starting) has not
    yet created a window. This class is the DEFINITIVE half of that split: the liveness check in
    :func:`inspect_credential_modal` has confirmed the process is gone, so there is nothing left to
    wait for. It is distinct from :class:`CredentialUnknownError` (process still ALIVE but momentarily
    window-less - indeterminate, latched) and must never be reported as a slow source: the data source
    is not implicated at all when Desktop itself has died.

    ``reason`` is the detector's own marker-free string, carried verbatim so the caller's verdict line
    cannot smuggle a ``CREDENTIAL_MARKER`` into the parent classifier's free-text scan (issue #153).
    """

    def __init__(self, pid: int, reason: str) -> None:
        self.pid = pid
        self.reason = reason
        super().__init__(f"power bi desktop process {pid} is gone: {reason}")


class DesktopUnreadyError(RuntimeError):
    """Power BI Desktop is alive but has no window, so its local state cannot be inspected."""

    def __init__(self, pid: int, reason: str) -> None:
        self.pid = pid
        self.reason = reason
        super().__init__(f"Power BI Desktop process {pid} is not ready: {reason}")


WindowEnumerator = Callable[[int], Iterable[DesktopWindow]]
ProcessLivenessCheck = Callable[[int], bool]

DESKTOP_MAIN_CLASS_PREFIX = "WindowsForms10.Window.8"

# `GetWindow(hwnd, GW_OWNER)`. The owner of a top-level window - the window a modal disables.
GW_OWNER = 4

# Classifier vocabulary, shared verbatim with the PowerShell arbiter's `Get-DialogClassification` so a
# reader moving between the two detectors meets one set of words, not two.
DIALOG_KIND_CREDENTIAL = "credential"
DIALOG_KIND_NEEDS_HUMAN = "needs-human"
DIALOG_KIND_MIXED_CONTENT = "mixed-content"
DIALOG_KIND_BENIGN = "benign"
DIALOG_KIND_BENIGN_TITLE_ONLY = "benign-title-only"
DIALOG_KIND_UNREADABLE = "unreadable"
DIALOG_KIND_UNRECOGNIZED = "unrecognized"

# Machine-readable verdict tokens, also shared with the arbiter. `BLOCKED_BY_DIALOG` is deliberately
# absent (issue #376): it is the token this module used to emit at exit 1 from a size-only test.
VERDICT_CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
VERDICT_DIALOG_NEEDS_HUMAN = "DIALOG_NEEDS_HUMAN"
VERDICT_DIALOG_UNREADABLE = "DIALOG_UNREADABLE"
VERDICT_DIALOG_UNRECOGNIZED = "DIALOG_UNRECOGNIZED"
VERDICT_REFRESH_IN_PROGRESS = "REFRESH_IN_PROGRESS"

# kind -> the token a caller prints for it. The unreadable BAND (nothing readable, or a reassuring
# CAPTION with no content behind it) is deliberately NOT folded into `unrecognized`: "we could not
# establish it" is a weaker state of knowledge than "we read it and it did not match", and collapsing
# the two is the bug this repo keeps finding (absent is not empty).
DIALOG_KIND_VERDICTS = {
    DIALOG_KIND_CREDENTIAL: VERDICT_CREDENTIAL_MISSING,
    DIALOG_KIND_NEEDS_HUMAN: VERDICT_DIALOG_NEEDS_HUMAN,
    DIALOG_KIND_UNREADABLE: VERDICT_DIALOG_UNREADABLE,
    DIALOG_KIND_BENIGN_TITLE_ONLY: VERDICT_DIALOG_UNREADABLE,
    DIALOG_KIND_MIXED_CONTENT: VERDICT_DIALOG_UNRECOGNIZED,
    DIALOG_KIND_UNRECOGNIZED: VERDICT_DIALOG_UNRECOGNIZED,
    DIALOG_KIND_BENIGN: VERDICT_REFRESH_IN_PROGRESS,
}

# Fold order for `dialog_verdict`, after `credential` (which short-circuits). It is not arbitrary:
# `benign` is the ONLY kind carrying positive evidence of harmlessness, so it must never outrank a
# window we could not account for - otherwise one progress dialog masks a real modal. The unreadable
# band outranks `unrecognized` because it is the weaker state of knowledge, and the weaker state is
# what must stay visible.
DIALOG_KIND_PRECEDENCE = (
    DIALOG_KIND_NEEDS_HUMAN,
    DIALOG_KIND_UNREADABLE,
    DIALOG_KIND_BENIGN_TITLE_ONLY,
    DIALOG_KIND_MIXED_CONTENT,
    DIALOG_KIND_UNRECOGNIZED,
    DIALOG_KIND_BENIGN,
)


class Win32EnumerationError(RuntimeError):
    """Win32 window enumeration failed before a reliable modal verdict could be formed."""


def _winenumproc_type():
    """Return the Win32 callback type lazily so the module imports on non-Windows CI."""
    if os.name != "nt":
        raise Win32EnumerationError("Win32 callback types are only available on Windows")
    return ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _compile_signature(path: Path) -> re.Pattern[str]:
    """Compile one shared ``*_signature.regex`` resource."""
    return re.compile(path.read_text(encoding="utf-8").strip(), re.IGNORECASE)


def credential_signature() -> re.Pattern[str]:
    """Compile the shared credential-dialog signature."""
    return _compile_signature(SIGNATURE_PATH)


def benign_dialog_signature() -> re.Pattern[str]:
    """Compile the shared progress-dialog ("Power BI is working") signature.

    ⚠️ This file and :func:`benign_chrome_signature` are the ONLY two that can cause a dialog to be
    dismissed, so a broad pattern in either is the one way to hide a genuine blocker. Its alternatives
    are whole-element and anchored on purpose: ``\\bLoading data\\b`` once matched inside
    *"Loading data requires authentication"*.
    """
    return _compile_signature(BENIGN_SIGNATURE_PATH)


def benign_chrome_signature() -> re.Pattern[str]:
    """Compile the enumerated allowlist of window chrome that cannot be a prompt (#376 review, 5).

    This exists because the rule it replaced was a **length heuristic**, and length is not evidence.
    ``MIN_PROMPT_WORDS = 5`` accepted any unmatched element under five words, so
    ``Refresh`` + ``Evaluating...`` + ``Please enter your password`` classified ``benign`` and was
    SUPPRESSED in flight - a real prompt swallowed in silence, on the very code path added to stop
    that happening. Measured in review; ``Password:`` (two words) did it too.

    The replacement is a **positive claim about specific strings**, not about their size: each
    alternative is a whole-element, anchored control label that carries no prompt at all, so a window
    whose only unexplained content is one of these still shows a human nothing to act on. Anything
    else - a table name, a status phrase nobody recognised, a four-word prompt - VETOES dismissal and
    lands in the exit-3 indeterminate band, which is exactly what that band is for.

    Keep it tiny. Every alternative added here is a string that can never again veto a dismissal.
    """
    return _compile_signature(BENIGN_CHROME_SIGNATURE_PATH)


def blocking_prompt_signature() -> re.Pattern[str]:
    """Compile the shared signature for KNOWN human-blocking prompts that are not sign-in prompts.

    The native-database-query approval modal above all: migrated custom-SQL sources emit exactly the
    shape that triggers it, and this bundle's own guidance says to check for it before concluding
    anything about a data source. Matched BEFORE the benign signature, so one progress element in the
    same window cannot erase it.
    """
    return _compile_signature(BLOCKING_SIGNATURE_PATH)


def normalize_texts(texts: Iterable[str]) -> tuple[str, ...]:
    """Whitespace-normalised, de-duplicated, order-preserving text (mirrors ``Get-NormalizedText``)."""
    clean: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        normalized = re.sub(r"\s+", " ", str(text)).strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            clean.append(normalized)
    return tuple(clean)


def dialog_text_set(window: DesktopWindow) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Return ``(title, all_texts, content_texts)`` for ``window``.

    ``content`` is everything that is NOT the caption, and it is the ONLY view the benign signature may
    read. A caption cannot establish that a dialog is harmless: a real owned WPF modal captioned
    ``Refresh`` whose content read ``Enter your credentials`` was dismissed on exactly that mistake in
    the arbiter's review. Dropping content that merely EQUALS the caption errs in the safe direction -
    it can only make a window harder to dismiss, never easier.
    """
    title = re.sub(r"\s+", " ", window.title or "").strip()
    raw_texts = ([title] if title else []) + list(window.texts or ())
    all_texts = normalize_texts(raw_texts)
    title_key = title.casefold()
    content = tuple(text for text in all_texts if text.casefold() != title_key)
    return title, all_texts, content


def _match_dialog_signatures(window: DesktopWindow, texts: Iterable[str]) -> DialogFinding | None:
    """First ``credential`` then ``needs-human`` signature hit across ``texts``, or ``None``.

    Order is load-bearing: a window can carry both, and the credential signature is the only one that
    earns a hard stop. ``needs-human`` is tested BEFORE any benign scan so one progress element in the
    same window cannot erase a known human-blocking prompt (the round-3 defect in #390).
    """
    all_texts = tuple(texts)
    signature = credential_signature()
    for text in all_texts:
        if signature.search(text):
            return _finding(DIALOG_KIND_CREDENTIAL, window, text)
    blocking = blocking_prompt_signature()
    for text in all_texts:
        if blocking.search(text):
            return _finding(DIALOG_KIND_NEEDS_HUMAN, window, text)
    return None


def classify_dialog(window: DesktopWindow) -> DialogFinding:
    """Classify ONE candidate window. Only ``benign`` is dismissible, and only it needs positive proof.

    Kinds, in the order they are tested (see the module docstring for the divergences from
    ``probe_desktop_credential.ps1``):

    ``credential``        the credential signature matched a text element -> hard stop.
    ``needs-human``       a KNOWN human-blocking prompt that is not a sign-in prompt. Tested BEFORE
                          benign so a progress element in the same window cannot erase it.
    ``mixed-content``     progress text AND content nobody could account for, in the same window. A
                          first-match-wins loop would let one benign element erase everything after
                          it, which is how ``Evaluating`` beside *"Permission is required to run this
                          native database query"* cleared the arbiter at exit 0.
    ``benign``            EVERY content element is either recognised progress status or enumerated
                          chrome, and at least one IS progress status. The one dismissible kind.
    ``benign-title-only`` the CAPTION matched the benign signature and no content did. A caption is not
                          content, so this joins the unreadable band.
    ``unreadable``        no readable text at all.
    ``unrecognized``      readable text that matched no signature: we looked, and it is not a sign-in
                          prompt. Distinct from ``unreadable`` on purpose - absent is not empty.

    ⚠️ **There is no length amnesty, and there must never be one again (#376 review, finding 5).**
    This used to accept any unmatched element of fewer than ``MIN_PROMPT_WORDS`` words, which meant
    ``benign`` did not mean *positively benign*: ``Please enter your password`` (4 words) and
    ``Password:`` (2) both classified ``benign`` beside ``Evaluating...``, and ``dialog_verdict`` then
    SUPPRESSED them in flight. That is the same defect class this module was written to remove,
    reintroduced on the new code path - and in that shape strictly worse than the bug it replaced,
    because the repo's rule is that a credential modal is never worked around.
    """
    title, all_texts, content = dialog_text_set(window)
    signature_hit = _match_dialog_signatures(window, all_texts)
    if signature_hit is not None:
        return signature_hit

    benign = benign_dialog_signature()
    chrome = benign_chrome_signature()
    benign_hit: str | None = None
    unaccounted: str | None = None

    if title:
        if not (benign.search(title) or chrome.search(title)):
            unaccounted = title

    for text in content:
        if benign.search(text):
            if benign_hit is None:
                benign_hit = text
            continue
        if chrome.search(text):
            continue
        if unaccounted is None:
            unaccounted = text
    if benign_hit is not None:
        if unaccounted is not None:
            return _finding(DIALOG_KIND_MIXED_CONTENT, window, unaccounted)
        return _finding(DIALOG_KIND_BENIGN, window, benign_hit)
    if title and benign.search(title):
        return _finding(DIALOG_KIND_BENIGN_TITLE_ONLY, window, title)
    if not all_texts:
        return _finding(DIALOG_KIND_UNREADABLE, window, "")
    evidence = unaccounted if unaccounted is not None else all_texts[0]
    return _finding(DIALOG_KIND_UNRECOGNIZED, window, evidence)


def _finding(kind: str, window: DesktopWindow, evidence: str) -> DialogFinding:
    """Build a :class:`DialogFinding` with the verdict token this ``kind`` maps to."""
    return DialogFinding(kind=kind, verdict=DIALOG_KIND_VERDICTS[kind], window=window, evidence=evidence)


def _ownership_root(window: DesktopWindow, by_hwnd: dict[int, DesktopWindow]) -> DesktopWindow | None:
    """Walk ``window``'s owner links to the UNOWNED root, or ``None`` if the chain cannot be resolved.

    Returns ``None`` when a link points at a window this enumeration did not see (an owner in another
    process, or one that is not top-level) or when the links form a cycle. Both mean the root is unknown
    from here, and an unknown root must not be guessed: whatever :func:`main_frame` picks is excluded
    from the credential prepass AND from classification.
    """
    seen = {id(window)}
    current = window
    while current.owner_hwnd:
        owner = by_hwnd.get(current.owner_hwnd)
        if owner is None or id(owner) in seen:
            return None
        seen.add(id(owner))
        current = owner
    return current


def main_frame(windows: Iterable[DesktopWindow]) -> DesktopWindow | None:
    """The application FRAME - the window dialogs block - or ``None`` when its identity is AMBIGUOUS.

    Four review rounds have defeated four ways of *inferring* which window is the application: its SIZE
    (round 1), a CLASS prefix plus a helper-name allowlist (round 2), the FIRST ownership edge (round
    3), and transitive ownership that collected only the roots reachable from OWNED windows (round 4's
    fix, broken in round 5 - an UNOWNED credential host owning one enabled tooltip was the only root
    anybody collected, so it was crowned the frame and excluded from the prepass AND from
    classification, while the real frame's progress text was suppressed in flight). Measured:
    ``modal=None dialog=None unknown_reason=None`` - a false clean on the gate of record.

    So this stops inferring and ENUMERATES. A window is a possible root in exactly two ways: it is
    UNOWNED and renders something (:func:`renders_nothing` is the only filter, and it is the modality
    rule rather than a size test); or it is the unowned root of some owned window's chain, walked
    TRANSITIVELY - an owned window can own another popup, so "first owner" was itself a proxy for "the
    root". An ownership-derived root gets NO priority over the rest; that is the round-5 fix, because a
    root reached through a tooltip is not better evidence than a window rendering pixels.

    Exactly one possible root identifies the frame. **Everything else returns ``None``, and ``None``
    excludes NOTHING.** Excluding nothing costs a spurious ``DIALOG_UNRECOGNIZED`` (exit 3) - or, for a
    report titled like a prompt, a spurious ``CREDENTIAL_MISSING`` (exit 1); both are LOUD. Excluding
    the WRONG window is silent, and finishes a model for a source nobody reached.

    ⚠️ **There is no authority to ask, and that was MEASURED (#400 review round 5).** .NET's
    ``Process.MainWindowHandle`` is ``EnumWindows`` stopping at the first VISIBLE, UNOWNED window of the
    pid - this function's own former fallback, in another process, minus the :func:`renders_nothing`
    guard. Pinned by ``test_the_process_main_window_handle_is_a_z_order_answer_not_an_identity``: on
    round 5's topology it named the CREDENTIAL HOST in 5 of 6 runs, it named an unowned **0x0** window
    created last, and raising either of two windows to ``HWND_TOPMOST`` moved the answer - it reports
    Z-ORDER. Only a genuinely OWNED modal gets a right answer out of it, where the ownership walk
    already agrees, so it is not consulted. Full evidence: ``SKILL.md``, "Round 5".
    """
    windows = list(windows)
    by_hwnd = {window.hwnd: window for window in windows if window.hwnd}
    roots: list[DesktopWindow] = []
    for window in windows:
        if window.owner_hwnd:
            root = _ownership_root(window, by_hwnd)
            if root is None:
                # Chain leaves this enumeration, or loops: the root is UNKNOWN, and an unknown root must
                # not be guessed at - the failure mode is excluding the wrong window.
                return None
        elif renders_nothing(window):
            continue
        else:
            root = window
        if not any(root is known for known in roots):
            roots.append(root)
    return roots[0] if len(roots) == 1 else None


def is_proven_non_blocking(window: DesktopWindow) -> bool:
    """Does POSITIVE evidence show ``window`` blocks nothing?

    Modality is a ONE-WAY test. An ENABLED owner proves this window is not blocking it, because a modal
    disables its owner. The converse does not hold - Power BI's refresh dialog also disables the owner -
    so a disabled owner never convicts, and ``None`` (no owner) means the test did not apply, which is
    not the same as passing it.
    """
    return window.owner_enabled is True


def renders_nothing(window: DesktopWindow) -> bool:
    """Is ``window`` incapable of showing a human anything, on TWO independent grounds?

    Zero area alone is NOT sufficient and must never be used alone (#400 review round 3, finding 3): a
    native experiment built a real ``WS_VISIBLE`` **owned 0x0** window and disabled its owner -
    ``owned-visible=True owner-win32-enabled=False rect=0x0`` - which is a genuinely blocking shape. On
    the unbounded query-poll path suppressing it leaves the worker blocked while every poll reports no
    finding: a false clear that is also a hang.

    So this requires BOTH: no owner to disable, AND no pixels to display. A window with neither
    mechanism cannot block anyone.
    """
    return not window.owner_hwnd and (window.width <= 0 or window.height <= 0)


def dialog_candidates(
    windows: Iterable[DesktopWindow],
    *,
    frame: DesktopWindow | None = None,
) -> list[DesktopWindow]:
    """Every visible window worth CLASSIFYING - which is every one not POSITIVELY proven harmless.

    ⚠️ **Three proxies died here across two review rounds. Do not add a fourth.** Size (>= 100x100),
    class prefix, and a helper-class NAME allowlist were each an attempt to answer *"is this window
    blocking a human?"* by looking at something else, and each one collapsed a real blocker into the
    healthy state:

    | proxy | what it hid |
    |---|---|
    | ``>= 100x100`` | a visible 80x60 unreadable owned dialog |
    | ``WindowsForms10.Window.8`` prefix | an owned ``FixedDialog`` sharing its owner's exact class |
    | ``Internet Explorer_Hidden`` name | the AAD sign-in host, whatever it displayed |

    Win32 answers the question directly, so ask it directly. The three exclusions below are the only
    ones, and each is a POSITIVE claim rather than a correlate:

    * the identified :func:`main_frame` - it is the application, and the thing dialogs block. When
      identity is AMBIGUOUS that function returns ``None`` and this exclusion simply does not happen
      (#400 review round 5): a spurious finding on the real frame is loud and recoverable, whereas
      excluding the wrong window is how a credential prompt disappears;
    * :func:`is_proven_non_blocking` - an enabled owner proves this window blocks nothing;
    * :func:`renders_nothing` - unowned AND zero-area: no owner to disable and no pixels to show.

    Nothing is excluded for its size, its class, or its name.
    """
    windows = list(windows)
    frame = frame if frame is not None else main_frame(windows)
    return [
        window
        for window in windows
        if window is not frame and not is_proven_non_blocking(window) and not renders_nothing(window)
    ]


def dialog_verdict(
    windows: Iterable[DesktopWindow],
    *,
    operation_in_flight: bool = False,
    frame: DesktopWindow | None = None,
) -> DialogFinding | None:
    """Fold per-window classifications into ONE finding, or ``None`` when nothing needs reporting.

    ``operation_in_flight`` is set only once THIS process has started the refresh/query it is waiting
    on. There, a PROVEN-benign progress dialog is our own and is ignored - and nothing else is. At t=0
    it is somebody else's, and stacking a second refresh on it is exactly what the 2026-08-28 field
    report had to unpick by hand.
    """
    found: dict[str, DialogFinding] = {}
    for window in dialog_candidates(windows, frame=frame):
        finding = classify_dialog(window)
        if finding.kind == DIALOG_KIND_CREDENTIAL:
            return finding
        found.setdefault(finding.kind, finding)
    for kind in DIALOG_KIND_PRECEDENCE:
        if kind not in found:
            continue
        if kind == DIALOG_KIND_BENIGN and operation_in_flight:
            continue
        return found[kind]
    return None


def match_credential_modal(
    windows: Iterable[DesktopWindow],
    *,
    frame: DesktopWindow | None = None,
) -> CredentialModal | None:
    """First credential-signature match across every window EXCEPT the application frame.

    Not restricted to :func:`dialog_candidates` (issue #376): it used to be, and the 100x100 filter
    therefore gated the HARD STOP as well as the classification, so a credential prompt in a smaller
    window returned no finding at all - a silent false negative on the one verdict that matters most.
    It reads windows classification skips, too: an owned zero-area window, or one whose owner is
    enabled, can still be carrying credential text.

    ⚠️ The application frame is excluded (#376 review, finding 4). An unrestricted scan read the
    Desktop caption and its child text, so a report legitimately named ``Account Key``,
    ``Personal Access Token`` or ``Databricks Client Credentials`` produced ``CREDENTIAL_MISSING`` at
    exit 1. The frame is identified by :func:`main_frame` - by OWNERSHIP, never by class - because a
    class-prefix test also excluded a real owned credential dialog that shared its owner's class
    (#400 review round 3, finding 2).
    """
    windows = list(windows)
    frame = frame if frame is not None else main_frame(windows)
    signature = credential_signature()
    for window in windows:
        if window is frame:
            continue
        for text in normalize_texts(window.texts):
            if signature.search(text):
                return CredentialModal(matched_text=text, window=window)
    return None


def _hwnd_value(hwnd) -> int:
    """Normalize ctypes callback HWND values (plain int on this Python, ctypes object on others)."""
    value = getattr(hwnd, "value", hwnd)
    return int(value or 0)


def _configure_user32(user32) -> None:
    """Set Win32 signatures explicitly so 64-bit handles are not truncated."""
    callback_type = _winenumproc_type()
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL


def _window_text(user32, hwnd: int) -> str:
    """Best-effort Win32 text for ``hwnd``."""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class_name(user32, hwnd: int) -> str:
    """Win32 class name for ``hwnd``."""
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _child_texts(user32, hwnd: int) -> tuple[str, ...]:
    """Text from child HWNDs of a candidate top-level window."""
    texts: list[str] = []
    errors: list[BaseException] = []

    def callback(child_hwnd, _lparam):
        try:
            text = _window_text(user32, _hwnd_value(child_hwnd))
            if text:
                texts.append(text)
        except BaseException as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            errors.append(exc)
        return True

    enum_child_proc = _winenumproc_type()(callback)
    user32.EnumChildWindows(hwnd, enum_child_proc, 0)
    if errors:
        raise Win32EnumerationError(f"child window enumeration callback failed: {errors[0]}") from errors[0]
    return tuple(texts)


def _enumerate_pid_windows_with_count(pid: int) -> tuple[list[DesktopWindow], int]:
    """Enumerate visible top-level/owned windows for ``pid`` using Win32 ``EnumWindows``.

    UI Automation root-child discovery misses Power BI connector dialogs because they are owned
    windows, not root children. Win32 ``EnumWindows`` sees that shape and still scopes by PID, so
    concurrent Desktop instances do not cross-contaminate.
    """
    if os.name != "nt":
        raise Win32EnumerationError("Win32 window enumeration is only available on Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    _configure_user32(user32)
    windows: list[DesktopWindow] = []
    errors: list[BaseException] = []
    visited = 0

    def callback(hwnd, _lparam):
        nonlocal visited
        visited += 1
        try:
            hwnd_int = _hwnd_value(hwnd)
            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd_int, ctypes.byref(owner_pid))
            if owner_pid.value != pid or not user32.IsWindowVisible(hwnd_int):
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd_int, ctypes.byref(rect))
            title = _window_text(user32, hwnd_int)
            texts = tuple(text for text in (title, *_child_texts(user32, hwnd_int)) if text)
            # The modality facts. `GW_OWNER` is the window a modal disables, and its enabled state is
            # the only direct evidence of whether this window is blocking anyone (#400 review round 3).
            # Deliberately three-valued: no owner means the test did not apply, which is NOT "passed".
            owner_hwnd = _hwnd_value(user32.GetWindow(hwnd_int, GW_OWNER))
            owner_enabled = bool(user32.IsWindowEnabled(owner_hwnd)) if owner_hwnd else None
            windows.append(
                DesktopWindow(
                    title=title,
                    class_name=_class_name(user32, hwnd_int),
                    width=max(0, rect.right - rect.left),
                    height=max(0, rect.bottom - rect.top),
                    texts=texts,
                    minimized=bool(user32.IsIconic(hwnd_int)),
                    hwnd=hwnd_int,
                    owner_hwnd=owner_hwnd,
                    owner_enabled=owner_enabled,
                )
            )
        except BaseException as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            errors.append(exc)
        return True

    enum_windows_proc = _winenumproc_type()(callback)
    if not user32.EnumWindows(enum_windows_proc, 0):
        error_code = ctypes.get_last_error()
        raise Win32EnumerationError(f"EnumWindows failed with Win32 error {error_code}")
    if errors:
        raise Win32EnumerationError(f"window enumeration callback failed: {errors[0]}") from errors[0]
    if visited == 0:
        raise Win32EnumerationError("EnumWindows returned no callbacks")
    return windows, visited


def enumerate_pid_windows(pid: int) -> list[DesktopWindow]:
    """Enumerate visible top-level/owned windows for ``pid`` using Win32 ``EnumWindows``."""
    windows, _visited = _enumerate_pid_windows_with_count(pid)
    return windows


def inspect_credential_modal(
    pid: int,
    enumerate_windows: WindowEnumerator = enumerate_pid_windows,
    process_is_alive: ProcessLivenessCheck = _process_alive,
    *,
    operation_in_flight: bool = False,
) -> CredentialDetection:
    """Inspect ``pid`` for a credential modal, preserving indeterminate states.

    Order is load-bearing and mirrors the arbiter's main flow: credential signature (every window, any
    size) -> a classified dialog finding -> minimized owner -> zero windows. ``operation_in_flight``
    is forwarded to :func:`dialog_verdict`; see :func:`inspect_credential_modal_in_flight`.
    """
    # pylint: disable=too-many-return-statements
    try:
        windows = tuple(enumerate_windows(pid))
    except Win32EnumerationError as exc:
        return CredentialDetection(unknown_reason=f"window enumeration failed: {exc}")
    frame = main_frame(windows)
    modal = match_credential_modal(windows, frame=frame)
    if modal is not None:
        return CredentialDetection(modal=modal, windows=windows)
    finding = dialog_verdict(windows, operation_in_flight=operation_in_flight, frame=frame)
    if finding is not None:
        return CredentialDetection(dialog=finding, windows=windows)
    if frame is not None and frame.minimized:
        return CredentialDetection(
            unknown_reason=(
                "Power BI Desktop owner window is minimized; owned modal dialogs are hidden from enumeration"
            ),
            windows=windows,
        )
    if not windows:
        # A live, working Desktop always owns at least its main window, so ZERO windows is never proof
        # of health (issue #158). Split it by liveness, into TWO terminal states - neither of which is
        # the minimized case's latch-and-keep-waiting: an alive-but-window-less process is starting up
        # or wedged (`desktop_unready` -> DESKTOP_UNREADY, exit 2: its local state is unreadable, so
        # the source was never tested), while a gone process exited or crashed (`process_gone` ->
        # DESKTOP_GONE, exit 2). Both are LOCAL failures that must never be blamed on a slow source or
        # routed to the credential layer. `unknown_reason` is deliberately NOT set for either.
        if process_is_alive(pid):
            return CredentialDetection(
                desktop_unready=(
                    "Power BI Desktop enumerated no windows while its process is still running; a "
                    "window-less process is starting up or wedged and its dialog state cannot be read"
                ),
                windows=windows,
            )
        return CredentialDetection(
            process_gone=(
                "Power BI Desktop enumerated no windows and its process is no longer running; it "
                "exited or crashed before any dialog state could be observed"
            ),
            windows=windows,
        )
    return CredentialDetection(windows=windows)


def describe_modal(modal: CredentialModal, source_hint: str | None = None) -> str:
    """Human-readable modal evidence for the verdict line."""
    window = modal.window
    size = f"{window.width}x{window.height}" if window.width and window.height else "unknown size"
    source = f" source={source_hint};" if source_hint else ""
    title = window.title or "(empty title)"
    return f"{source} window title={title!r}, class={window.class_name!r}, size={size}, text={modal.excerpt!r}"


def describe_dialog_finding(finding: DialogFinding) -> str:
    """Human-readable evidence for a dialog we could not dismiss."""
    window = finding.window
    size = f"{window.width}x{window.height}" if window.width and window.height else "unknown size"
    title = window.title or "(empty title)"
    return (
        f"kind={finding.kind}, window title={title!r}, class={window.class_name!r}, "
        f"size={size}, evidence={finding.excerpt!r}"
    )


# Per-kind operator guidance. Every string here is deliberately MARKER-FREE (issue #153): it must not
# contain any `probe_live_source.CREDENTIAL_MARKERS` substring - "credential", "sign in", "signed in",
# "authentication", "login", "oauth", "access token", "unauthorized" - because the parent classifier
# scans a failing child's whole transcript as free text. These verdicts mean "we could not probe", and
# prose that reads as a sign-on problem would relabel them the very hard stop they exist to avoid.
# `tests/test_probe_live_source_verdict.py` drives the real emitters through the real classifier so
# this property is gated rather than merely intended.
DIALOG_KIND_GUIDANCE = {
    DIALOG_KIND_NEEDS_HUMAN: (
        "this is a KNOWN human-blocking prompt (e.g. the native database query approval), not a "
        "data-source sign-on prompt - approve it at the Desktop screen; no account details are implied"
    ),
    DIALOG_KIND_MIXED_CONTENT: (
        "it shows refresh progress AND prose that is not progress status - a progress dialog does not "
        "explain the rest of this window; look at the Desktop screen"
    ),
    DIALOG_KIND_BENIGN_TITLE_ONLY: (
        "its CAPTION looks like a progress dialog, but no content confirmed it - a caption is not "
        "content; look at the Desktop screen"
    ),
    DIALOG_KIND_UNREADABLE: (
        "this window exposes no readable text, so it could not be classified at all - look at the Desktop screen"
    ),
    DIALOG_KIND_UNRECOGNIZED: (
        "its text matched no known prompt signature, so nothing here says a person must supply account "
        "details - look at the Desktop screen"
    ),
    DIALOG_KIND_BENIGN: (
        "a refresh is already running on this pid - wait for it, or cancel the stale one; do not stack "
        "a second refresh on it"
    ),
}


def dialog_guidance(finding: DialogFinding) -> str:
    """Operator guidance for ``finding``, or a safe generic line for an unmapped kind."""
    return DIALOG_KIND_GUIDANCE.get(finding.kind, "look at the Desktop screen before continuing")


def inspect_credential_modal_in_flight(
    pid: int,
    enumerate_windows: WindowEnumerator = enumerate_pid_windows,
    process_is_alive: ProcessLivenessCheck = _process_alive,
) -> CredentialDetection:
    """:func:`inspect_credential_modal` for a poll running AFTER this process started the operation.

    The only difference is that a PROVEN-benign progress dialog is ignored: at that point it is almost
    certainly the refresh we ourselves triggered, and aborting on it would abort a healthy run. Nothing
    else is ignored - an unreadable, caption-only, mixed or unrecognised dialog still surfaces.
    """
    return inspect_credential_modal(
        pid,
        enumerate_windows,
        process_is_alive,
        operation_in_flight=True,
    )


def source_hint_from_model(model_dir: Path | None) -> str | None:
    """Best-effort source hint from TMDL/M expressions, without requiring Desktop."""
    if model_dir is None:
        return None
    definition = model_dir / "definition"
    if not definition.is_dir():
        return None
    for path in sorted(definition.rglob("*.tmdl")):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        match = CONNECTOR_SOURCE_RE.search(text)
        if not match:
            continue
        source = match.group("double") or match.group("single")
        kind = match.group("kind")
        return f"{kind}({source})" if source else kind
    return None


def print_refresh_banner(
    pid: int,
    timeout_sec: int,
    grace_sec: int | float,
    operation: str = "refresh",
) -> None:
    """Print the no-dialog, self-bounded refresh warning before the wait starts.

    Deliberately does NOT name the verdict tokens it may later print, and interpolates NO caller-supplied
    text - so unlike :func:`print_refresh_unknown_banner`, this banner's marker-free guarantee is total
    and self-contained. This banner is emitted on the HEALTHY no-dialog path and is captured verbatim by
    ``probe_live_source``'s classifier; naming ``CREDENTIAL_MISSING`` / ``BLOCKED_BY_DIALOG`` here (or
    even the bare word "credential") made the reassuring message classify a successful refresh as
    ``NO_CREDENTIAL`` (issue #153). The classifier now matches verdict LINES structurally, but keeping
    control tokens out of free prose is the belt-and-braces half of that fix - so a future helpful edit
    cannot re-arm the landmine.
    """
    total = timeout_sec + grace_sec
    if operation == "calculate":
        prefix = f"No blocking dialog on PID {pid}. Calculate in progress, bounded at "
        wait_note = "this mode recalculates formulas without reading source rows, so long waits are unusual. "
    else:
        prefix = f"No blocking dialog on PID {pid}. Refreshing, bounded at "
        wait_note = "a long wait here is expected for a serverless cold start. "
    print(
        f"{prefix}{timeout_sec}s XMLA + {grace_sec}s grace ({total}s total); {wait_note}"
        "DO NOT kill this process - at the deadline it re-checks and prints one final machine-readable "
        "verdict line. Killing it early yields NO verdict.",
        flush=True,
    )


def print_refresh_unknown_banner(pid: int, timeout_sec: int, grace_sec: int | float, reason: str) -> None:
    """Print the bounded-refresh warning when the t=0 modal check is indeterminate.

    This banner's OWN static prose names no control tokens and not the bare word "credential" - but it
    interpolates ``reason``, a caller-supplied string from ``inspect_credential_modal`` that the
    classifier scans along with everything else. So the marker-free guarantee for the UNKNOWN path is
    owned by the DETECTOR, not by this banner alone: the minimized-owner ``unknown_reason`` was reworded
    off the word "credential" (a ``CREDENTIAL_MARKER``) for exactly this reason (issue #153) - otherwise
    a slow/timeout refresh here was mislabelled ``NO_CREDENTIAL``. The prefix reads "Blocking-dialog
    check ... UNKNOWN" rather than the old "Credential dialog check" as the belt-and-braces static half.
    """
    total = timeout_sec + grace_sec
    print(
        f"Blocking-dialog check on PID {pid} is UNKNOWN ({reason}). Refreshing, bounded at "
        f"{timeout_sec}s XMLA + {grace_sec}s grace ({total}s total); a minimized owner can hide owned "
        "dialogs from enumeration. DO NOT kill this process - at the deadline it re-checks with the "
        "dialog arbiter and prints one final machine-readable verdict line. Killing it early "
        "yields NO verdict.",
        flush=True,
    )


def print_refresh_heartbeat(elapsed: float, total: float) -> None:
    """Print an elapsed/total countdown without claiming progress."""
    print(f"still refreshing, {int(elapsed)}s / {int(total)}s", flush=True)


def print_indeterminate_state_notice(pid: int, reason: str) -> None:
    """Loudly note the first time the wait latches an indeterminate window state (issues #154, #158).

    Two observations land here, and they mean the same thing - the dialog state can no longer be read
    reliably: the owner window went iconic (minimize -> restore destroys the owned-dialog evidence for
    good, measured 2026-08-14), or enumeration returned no windows while the process was still alive
    (starting up or wedged, issue #158). Either way the wait latches it so a later ``none`` cannot pass
    for health; this notice makes the transition visible in the transcript instead of silent.

    Like every string this module prints into the refresh transcript, it is deliberately MARKER-FREE
    (no ``CREDENTIAL_MARKER`` token, not even the bare word "credential") and is NOT a ``REFRESH:``
    verdict line, so the parent classifier cannot mistake it for a verdict (issue #153). ``reason`` is
    the detector's own marker-free string and carries the specific cause.
    """
    print(
        f"PID {pid} entered an indeterminate window state mid-wait ({reason}); latching this "
        "observation - a later clear cannot un-see it, because the evidence a blocking dialog would "
        "leave is no longer readable. This run ends at the deadline with a distinct indeterminate "
        "verdict, never a slow-source timeout.",
        flush=True,
    )


def print_dialog_observed_notice(pid: int, finding: DialogFinding) -> None:
    """Loudly note the first time a bounded wait sees a dialog it could not dismiss (issue #376).

    Latched rather than acted on: it is not a sign-on prompt, so it must not abort a refresh that is
    otherwise healthy and may still finish - but it also means "no modal appeared" is no longer
    established, so a quiet deadline must not erase it either. Marker-free, and not a ``REFRESH:``
    verdict line, so the parent classifier cannot mistake it for a verdict (issue #153).
    """
    print(
        f"PID {pid} has a dialog up that could not be shown to be harmless ({finding.kind}); latching "
        f"it - {dialog_guidance(finding)}. The wait continues, because this is not a prompt for account "
        "details and an otherwise healthy refresh must not be aborted by it.",
        flush=True,
    )


def raise_terminal_detection(pid: int, state: CredentialDetection, source_hint: str | None = None) -> None:
    """Raise for the two observations that end a bounded wait IMMEDIATELY.

    Only a matched credential signature and a confirmed-dead process qualify. A :class:`DialogFinding`
    deliberately does NOT (issue #376): it is latched by the caller and surfaced at the deadline via
    :func:`raise_latched_verdict`, so a dialog we could not read cannot cut short a refresh that was
    going to succeed.

    Public because ``refresh_pbip_model`` has a SECOND wait loop - the progress-monitor branch - which
    must behave identically. It used to call the t=0 helper instead, so a proven-benign progress dialog
    belonging to the current refresh aborted that refresh (#376 review, finding 1).
    """
    if state.modal is not None:
        raise CredentialMissingError(pid, state.modal, source_hint)
    if state.process_gone is not None:
        raise DesktopGoneError(pid, state.process_gone)


def raise_latched_verdict(
    pid: int,
    *,
    desktop_unready: str | None = None,
    dialog: DialogFinding | None = None,
    unknown: str | None = None,
) -> None:
    """Raise whichever latched observation a bounded wait ended with, in precedence order.

    Local Desktop failure first (nothing was learned about the source at all), then a dialog we could
    not account for, then the indeterminate credential state. Shared by both wait loops so they cannot
    disagree - the progress-monitor branch used to compute its latches and then DISCARD them, ending
    on a bare ``TimeoutError`` that the parent blames on a slow source.
    """
    if desktop_unready is not None:
        raise DesktopUnreadyError(pid, desktop_unready)
    if dialog is not None:
        raise DialogFoundError(pid, dialog)
    if unknown is not None:
        raise CredentialUnknownError(pid, unknown)


def _raise_detection(pid: int, state: CredentialDetection, source_hint: str | None) -> None:
    """Backwards-compatible alias for :func:`raise_terminal_detection`."""
    raise_terminal_detection(pid, state, source_hint)


# pylint: disable=too-many-arguments
def join_with_credential_poll(
    worker,
    *,
    pid: int,
    total_timeout: float,
    heartbeat_seconds: float,
    poll_seconds: float,
    source_hint: str | None = None,
    detector: Callable[[int], CredentialDetection] = inspect_credential_modal_in_flight,
    initial_state: CredentialDetection | None = None,
) -> bool:
    """Wait for ``worker`` while polling for a late credential dialog.

    Returns True when the worker finished before the wall-clock deadline, False when the deadline
    elapsed with the dialog state observably HEALTHY throughout. Raises immediately when the shared
    detector matches the credential signature, or confirms Desktop is gone.

    The default ``detector`` is the IN-FLIGHT variant: by the time this runs, the refresh being waited
    on is ours, so a PROVEN-benign progress dialog is ours too and is ignored. Nothing else is.

    A :class:`DialogFinding` - a dialog we could not dismiss - is LATCHED, not raised (issue #376). It
    is not a prompt for account details, so it must not abort a refresh that may still finish; but it
    also means "no modal appeared" is no longer established, so it surfaces at the deadline as
    :class:`DialogFoundError` rather than as a bare timeout blaming a slow source. Before #376 this
    raised at once, on a SIZE-ONLY test, at the same exit code as a real sign-on wall - so a Power BI
    refresh progress dialog aborted the very refresh it was reporting on.

    Indeterminate (``unknown_reason``) observations are LATCHED, not ignored (issue #154). The owner
    window going iconic hides its owned modal dialogs from enumeration, and - measured 2026-08-14 -
    restoring it does NOT bring them back: minimize -> restore destroys the evidence permanently, after
    which the detector reports ``none``, the SAME state as a genuinely healthy Desktop. A run that ever
    went indeterminate therefore ends at the deadline with :class:`CredentialUnknownError`, so a later
    ``none`` can never launder it into a bare timeout that blames a slow source.

    Latch-and-keep-waiting (rather than ending the instant UNKNOWN is first seen) is deliberate and is
    the false-positive-free choice: a worker that FINISHES clears the wait via ``return True`` below
    regardless of any latch, so a refresh that completes within the deadline is never overridden - the
    only run that ever surfaces the latched verdict is one that was going to hit the deadline anyway.
    Ending early instead would let a single transient enumeration hiccup, or a healthy-but-minimized
    slow refresh, cut off a run that would have succeeded.

    A ``process_gone`` observation is different: it is TERMINAL, raising :class:`DesktopGoneError`
    immediately (issue #158). Zero enumerated windows plus a confirmed-dead PID is definitive - Desktop
    has exited or crashed, there is nothing left to wait for, and the data source is not implicated -
    so unlike the latched-and-waited indeterminate case there is no value in running out the clock.

    ``desktop_unready`` - zero windows while the process is still ALIVE - is the third shape, and sits
    between the two: it is LATCHED like ``unknown_reason`` (a starting-up Desktop may still produce a
    window and finish the refresh, so ending early would be a false positive), but at the deadline it
    surfaces as :class:`DesktopUnreadyError`, a terminal local-error verdict, ahead of the credential
    family. It is not a credential signal: no human sign-in fixes a window-less process, so routing it
    to ``CredentialUnknownError`` would send someone to the wrong layer. Both zero-window states are
    seeded from ``initial_state`` so an observation made only by the caller's t=0 pre-check cannot be
    lost before the first poll.
    """
    # Waived, not shaved: the locals are eight injected parameters plus one latch per observation this
    # wait must not lose (unknown / desktop-unready / dialog). Folding the latches into a container
    # would hide exactly the thing this function exists to keep visible.
    # pylint: disable=too-many-locals
    started = time.monotonic()
    next_heartbeat = heartbeat_seconds
    latched_unknown = initial_state.unknown_reason if initial_state else None
    latched_desktop_unready = initial_state.desktop_unready if initial_state else None
    latched_dialog = initial_state.dialog if initial_state else None
    while worker.is_alive():
        elapsed = time.monotonic() - started
        remaining = max(0.0, total_timeout - elapsed)
        if remaining <= 0:
            break
        worker.join(min(remaining, poll_seconds, max(0.0, next_heartbeat - elapsed)))
        elapsed = time.monotonic() - started
        state = detector(pid)
        _raise_detection(pid, state, source_hint)
        if state.dialog is not None and latched_dialog is None:
            latched_dialog = state.dialog
            print_dialog_observed_notice(pid, state.dialog)
        if state.desktop_unready and latched_desktop_unready is None:
            latched_desktop_unready = state.desktop_unready
        if state.unknown_reason and latched_unknown is None:
            latched_unknown = state.unknown_reason
            print_indeterminate_state_notice(pid, state.unknown_reason)
        if elapsed >= next_heartbeat and worker.is_alive():
            print_refresh_heartbeat(elapsed, total_timeout)
            next_heartbeat += heartbeat_seconds
    if worker.is_alive():
        state = detector(pid)
        _raise_detection(pid, state, source_hint)
        raise_latched_verdict(
            pid,
            desktop_unready=latched_desktop_unready or state.desktop_unready,
            dialog=latched_dialog or state.dialog,
            unknown=latched_unknown or state.unknown_reason,
        )
        return False
    return True


# pylint: enable=too-many-arguments
