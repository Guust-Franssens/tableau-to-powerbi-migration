"""purpose: read-only, exact-identity Desktop observations and private short-lived image leases.
usage:   import observe, watch from the portable skill; callers retain operation/proof authority.
internal: true
internal-reason: Portable import authority and private image child; existing query/refresh entry points remain public.

Python alone classifies native facts. _desktop_uia.ps1 only harvests, in one bounded child.
READY is observation completeness, never source reachability, row evidence or a gate clear.
"""
# pylint: disable=too-many-branches

from __future__ import annotations

# One portable authority includes the unchanged private-image lease; do not split its copy-out unit.
# pylint: disable=too-many-lines

import atexit
import ctypes
import hashlib
import json
import math
import os
import queue
import re
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib
from ctypes import wintypes
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

# Sibling module, resolved once the caller puts this scripts/ dir on sys.path (probe, refresh, and the
# test conftest all do). Reused rather than reimplemented for issue #158's zero-window liveness split:
# its bias errs toward "alive" on any ambiguity, so an uncertain Desktop routes to UNKNOWN-latched and
# never to a false crash - and it is already load-bearing and tested for stale-lock reclaim.
from _lock import _process_alive

SIGNATURE_PATH = Path(__file__).resolve().with_name("credential_modal_signature.regex")
BENIGN_SIGNATURE_PATH = Path(__file__).resolve().with_name("benign_dialog_signature.regex")
BENIGN_CHROME_SIGNATURE_PATH = Path(__file__).resolve().with_name("benign_chrome_signature.regex")
BLOCKING_SIGNATURE_PATH = Path(__file__).resolve().with_name("blocking_prompt_signature.regex")
POLL_INTERVAL_SECONDS = 5.0
CONNECTOR_SOURCE_RE = re.compile(
    r"\b(?P<kind>Sql\.Database|Snowflake\.Databases|Databricks\.[A-Za-z]+|Odbc\.DataSource|Web\.Contents)\s*\("
    r"\s*(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)')?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DesktopWindow:
    """Visible top-level Power BI Desktop window plus descendant Win32 text and its MODALITY facts."""

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
    complete: bool = True
    reason: str = "complete"
    items: tuple[tuple[str, str, str], ...] = ()
    message_id: str | None = None
    pid: str | None = None
    process_started_at: str | None = None


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
    """A candidate dialog we could NOT dismiss, plus the classification that says why."""

    kind: str
    verdict: str
    window: DesktopWindow
    evidence: str = ""

    @property
    def excerpt(self) -> str:
        """Short UI text excerpt suitable for a verdict line."""
        return self.evidence[:80]


@dataclass(frozen=True)
class Observation:
    """Immutable observation; transport success and READY establish no operation outcome."""

    # These are one immutable observation plus its private facts, not independent state owners.
    # pylint: disable=too-many-instance-attributes

    modal: CredentialModal | None = None
    dialog: DialogFinding | None = None
    unknown_reason: str | None = None
    desktop_unready: str | None = None
    process_gone: str | None = None
    windows: tuple[DesktopWindow, ...] = ()
    pid: str | None = None
    process_started_at: str | None = None
    complete: bool = True
    reason: str = "complete"
    operation_id: str | None = None
    observation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    elapsed_ms: float = 0.0
    findings: tuple[DialogFinding, ...] = ()
    target_window: DesktopWindow | None = None

    @property
    def state(self) -> str:
        """Closed observer vocabulary, folded in safety precedence order."""
        # pylint: disable=too-many-return-statements
        if self.modal is not None:
            return "CREDENTIAL_REQUIRED"
        if self.process_gone is not None:
            return "PROCESS_GONE"
        if self.dialog is not None:
            if self.dialog.window.message_id == "package_session":
                return "DESKTOP_SESSION_ERROR"
            return {
                DIALOG_KIND_CREDENTIAL: "CREDENTIAL_REQUIRED",
                DIALOG_KIND_NEEDS_HUMAN: "HUMAN_ACTION_REQUIRED",
                DIALOG_KIND_BENIGN: "REFRESH_IN_PROGRESS",
                DIALOG_KIND_MIXED_CONTENT: "UNKNOWN_DIALOG",
                DIALOG_KIND_UNRECOGNIZED: "UNKNOWN_DIALOG",
            }.get(self.dialog.kind, "UNREADABLE_DIALOG")
        if self.desktop_unready is not None:
            return "NO_WINDOW"
        if self.unknown_reason is not None or not self.complete:
            return "UNREADABLE_DIALOG"
        return "READY"

    def to_json(self) -> str:
        """Serialize only closed metadata; raw window text never crosses the public boundary."""
        window = self.modal.window if self.modal else (self.dialog.window if self.dialog else self.target_window)
        raw = json.dumps(
            {
                "schema": "desktop.observation.v1",
                "observation_id": self.observation_id,
                "operation_id": self.operation_id,
                "pid": self.pid,
                "process_started_at": self.process_started_at,
                "hwnd": _handle_string(window.hwnd) if window else None,
                "owner_hwnd": _handle_string(window.owner_hwnd) if window else None,
                "observed_at": self.observed_at,
                "elapsed_ms": self.elapsed_ms,
                "complete": self.complete,
                "state": self.state,
                "reason": self.reason,
                "findings": [
                    {
                        "hwnd": _handle_string(finding.window.hwnd),
                        "owner_hwnd": _handle_string(finding.window.owner_hwnd),
                        "state": Observation(dialog=finding).state,
                        "reason": _observation_reason(Observation(dialog=finding)),
                        "message_id": finding.window.message_id,
                        "complete": finding.window.complete,
                    }
                    for finding in self.findings
                ],
                "image": None,
                "cannot_prove": ["source_reachability", "credential_validity", "data_correctness"],
            },
            allow_nan=False,
        )
        observation_token(raw)
        return raw


_LEGACY_TOKENS = {
    "READY": None,
    "CREDENTIAL_REQUIRED": "CREDENTIAL_MISSING",
    "NATIVE_QUERY_APPROVAL": "DIALOG_NEEDS_HUMAN",
    "HUMAN_ACTION_REQUIRED": "DIALOG_NEEDS_HUMAN",
    "DESKTOP_SESSION_ERROR": "DESKTOP_UNREADY",
    "UNKNOWN_DIALOG": "DIALOG_UNRECOGNIZED",
    "UNREADABLE_DIALOG": "DIALOG_UNREADABLE",
    "NO_WINDOW": "DESKTOP_UNREADY",
    "REFRESH_IN_PROGRESS": "REFRESH_IN_PROGRESS",
    "PROCESS_GONE": "DESKTOP_GONE",
}
_TERMINAL_STATES = frozenset(
    {
        "CREDENTIAL_REQUIRED",
        "NATIVE_QUERY_APPROVAL",
        "HUMAN_ACTION_REQUIRED",
        "DESKTOP_SESSION_ERROR",
        "PROCESS_GONE",
    }
)


def legacy_token(observation: Observation) -> str | None:
    """The sole trusted observer-to-operation vocabulary adapter."""
    return _LEGACY_TOKENS[observation.state]


def measurement_refusal(code: str) -> str:
    """Explicit adaptation to unchanged receipt v2/v3 refusal enums; unknown codes reject."""
    if code in {"DESKTOP_GONE", "REFRESH_IN_PROGRESS"}:
        return "TOOL_UNAVAILABLE"
    if code in {
        "CREDENTIAL_MISSING",
        "CREDENTIAL_UNKNOWN",
        "DESKTOP_UNREADY",
        "DIALOG_NEEDS_HUMAN",
        "DIALOG_UNREADABLE",
        "DIALOG_UNRECOGNIZED",
        "IDENTITY_UNESTABLISHED",
        "WRONG_PID_PORT",
        "PID_REUSED",
        "CATALOGUE_UNESTABLISHED",
        "CATALOGUE_CHANGED",
        "CANARIES_REQUIRED",
        "TIMEOUT",
        "TOOL_UNAVAILABLE",
        "MODEL_LOCK_TIMEOUT",
    }:
        return code
    raise ValueError("unknown observation refusal")


def observation_token(raw: str) -> str | None:
    """Validate a public event before adapting it; malformed/unknown evidence is never clean."""
    # Closed protocol fields are checked explicitly, rather than delegated to a second schema owner.
    # pylint: disable=too-many-branches
    if len(raw.encode("utf-8")) > 131072:
        raise ValueError("observation size")
    payload = json.loads(raw, object_pairs_hook=_unique_object)
    keys = {
        "schema",
        "observation_id",
        "operation_id",
        "pid",
        "process_started_at",
        "hwnd",
        "owner_hwnd",
        "observed_at",
        "elapsed_ms",
        "complete",
        "state",
        "reason",
        "findings",
        "image",
        "cannot_prove",
    }
    if not isinstance(payload, dict) or set(payload) != keys or payload["schema"] != "desktop.observation.v1":
        raise ValueError("observation shape")
    for key in ("observation_id", "operation_id"):
        if key == "operation_id" and payload[key] is None:
            continue
        if not isinstance(payload[key], str) or str(uuid.UUID(payload[key])) != payload[key]:
            raise ValueError("observation id")
    if not _decimal_identity(payload["pid"]) or not 0 < int(payload["pid"]) < 2**32:
        raise ValueError("observation pid")
    if type(payload["complete"]) is not bool or payload["state"] not in _LEGACY_TOKENS:  # pylint: disable=unidiomatic-typecheck
        raise ValueError("observation state")
    if payload["process_started_at"] is not None and not _decimal_identity(payload["process_started_at"]):
        raise ValueError("observation start")
    if payload["complete"] and payload["process_started_at"] is None:
        raise ValueError("missing process start")
    if payload["state"] == "READY" and not payload["complete"]:
        raise ValueError("incomplete ready")
    if payload["reason"] not in _REASONS:
        raise ValueError("observation reason")
    _handle_value(payload["hwnd"])
    _handle_value(payload["owner_hwnd"])
    elapsed = payload["elapsed_ms"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (float, int)) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("observation elapsed")
    if not isinstance(payload["observed_at"], str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?(?:Z|\+00:00)", payload["observed_at"]
    ):
        raise ValueError("observation timestamp")
    datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
    findings = payload["findings"]
    if not isinstance(findings, list) or len(findings) > 4000:
        raise ValueError("observation findings")
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {
            "hwnd",
            "owner_hwnd",
            "state",
            "reason",
            "message_id",
            "complete",
        }:
            raise ValueError("observation finding")
        _handle_value(finding["hwnd"], nullable=False)
        _handle_value(finding["owner_hwnd"])
        # Exact types reject bools and coercible values at the native identity boundary.
        # pylint: disable=unidiomatic-typecheck
        if (
            finding["state"] not in _LEGACY_TOKENS
            or finding["reason"] not in _REASONS
            or finding["message_id"] not in {None, "privacy_warning", "package_session"}
            or type(finding["complete"]) is not bool  # pylint: disable=unidiomatic-typecheck
        ):
            raise ValueError("observation finding enum")
    if payload["complete"] and any(not finding["complete"] for finding in findings):
        raise ValueError("observation completeness")
    if payload["state"] == "READY" and (findings or payload["reason"] != "complete"):
        raise ValueError("unexplained ready")
    if payload["image"] is not None:
        _validate_public_image(payload["image"])
    if payload["cannot_prove"] != ["source_reachability", "credential_validity", "data_correctness"]:
        raise ValueError("observation authority")
    return _LEGACY_TOKENS[payload["state"]]


def _validate_public_image(image: dict) -> None:
    """Accept opaque lease metadata only; image acquisition has no observation/proof authority."""
    if not isinstance(image, dict) or set(image) != {
        "id",
        "basename",
        "status",
        "width",
        "height",
        "sha256",
        "expires_at",
        "cleanup",
    }:
        raise ValueError("image shape")
    if not isinstance(image["id"], str) or str(uuid.UUID(image["id"])) != image["id"]:
        raise ValueError("image id")
    if not isinstance(image["basename"], str) or not _IMAGE_NAME.fullmatch(image["basename"]):
        raise ValueError("image basename")
    if image["status"] not in {
        "ACQUIRED",
        "TARGET_CHANGED",
        "BLANK_OR_INCOMPLETE",
        "CAPTURE_FAILED",
        "WRITE_FAILED",
        "UNSUPPORTED",
        "CAPTURE_CLEANUP_FAILED",
    } or image["cleanup"] not in {"leased", "deleted", "failed"}:
        raise ValueError("image enum")
    for key in ("width", "height"):
        if image[key] is not None and (type(image[key]) is not int or not 0 < image[key] <= 32768):  # pylint: disable=unidiomatic-typecheck
            raise ValueError("image extent")
    if image["sha256"] is not None and (
        not isinstance(image["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", image["sha256"])
    ):
        raise ValueError("image hash")
    expiry = image["expires_at"]
    if expiry is not None and (
        not isinstance(expiry, str)
        or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?(?:Z|\+00:00)", expiry)
    ):
        raise ValueError("image expiry")
    if expiry is not None:
        datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    if image["status"] == "ACQUIRED" and any(image[key] is None for key in ("width", "height", "sha256", "expires_at")):
        raise ValueError("image acquisition")


def emit_observation(observation: Observation, *, stream=None) -> None:
    """Emit validated closed metadata on a diagnostic channel, never operation success."""
    raw = observation.to_json()
    observation_token(raw)
    print("DESKTOP_OBSERVATION " + raw, file=stream or sys.stderr, flush=True)


def observation_refusal_events(events: Iterable[str], *, terminal_only: bool = False) -> str | None:
    """Fold validated events with the same first-terminal/retained-uncertainty lattice as watch."""
    latched = None
    for raw in events:
        try:
            token = observation_token(raw)
            terminal = json.loads(raw)["state"] in _TERMINAL_STATES
        except (ValueError, TypeError, KeyError):
            token, terminal = "DIALOG_UNREADABLE", False
        if terminal:
            return token
        if token not in {None, "REFRESH_IN_PROGRESS"} and latched is None:
            latched = token
    return None if terminal_only else latched


class DesktopRefusal(RuntimeError):
    """Closed operation adapter refusal, never reflected provider text."""

    def __init__(self, observation: Observation) -> None:
        self.observation = observation
        self.code = legacy_token(observation)
        if self.code is None:
            raise ValueError("READY is not a refusal or an operation success")
        super().__init__(self.code)


@dataclass
class _ObservationLatch:
    """One bounded lattice for watch and legacy worker waits; raw history is not accumulated."""

    terminal: Observation | None = None
    snapshots: tuple[Observation, ...] = ()
    desktop_unready: str | None = None
    dialog: DialogFinding | None = None
    unknown: str | None = None

    def add(self, snapshot: Observation) -> Observation:
        """Keep first terminal, first legacy refusal per band, and one snapshot per closed state."""
        state = snapshot.state
        if state not in {"READY", "REFRESH_IN_PROGRESS"} and not any(old.state == state for old in self.snapshots):
            self.snapshots += (snapshot,)
        if self.terminal is None and state in _TERMINAL_STATES:
            self.terminal = snapshot
        if self.desktop_unready is None and snapshot.desktop_unready:
            self.desktop_unready = snapshot.desktop_unready
        if self.dialog is None and snapshot.dialog is not None and state != "REFRESH_IN_PROGRESS":
            self.dialog = snapshot.dialog
        if self.unknown is None and snapshot.unknown_reason:
            self.unknown = snapshot.unknown_reason
        return self.terminal or snapshot


class CredentialMissingError(RuntimeError):
    """Power BI Desktop is showing a data-source credential dialog."""

    def __init__(self, pid: int, modal: CredentialModal, source_hint: str | None = None) -> None:
        self.pid = pid
        self.modal = modal
        self.source_hint = source_hint
        super().__init__(describe_modal(modal, source_hint))


class DialogFoundError(RuntimeError):
    """Power BI Desktop is showing a dialog that could NOT be shown to be harmless (issue #376)."""

    def __init__(self, pid: int, finding: DialogFinding) -> None:
        self.pid = pid
        self.finding = finding
        super().__init__(describe_dialog_finding(finding))


class CredentialUnknownError(RuntimeError):
    """Power BI Desktop's blocking-dialog state stayed indeterminate right up to the deadline."""

    def __init__(self, pid: int, reason: str) -> None:
        self.pid = pid
        self.reason = reason
        super().__init__(f"desktop dialog state indeterminate for pid {pid}: {reason}")


class DesktopGoneError(RuntimeError):
    """Power BI Desktop enumerated ZERO windows AND the process is no longer running (issue #158)."""

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
    """Compile the enumerated allowlist of window chrome that cannot be a prompt (#376 review, 5)."""
    return _compile_signature(BENIGN_CHROME_SIGNATURE_PATH)


def blocking_prompt_signature() -> re.Pattern[str]:
    """Compile the shared signature for KNOWN human-blocking prompts.

    Not "prompts that are not sign-in prompts": the shared alternatives span the native-database-query
    approval modal AND an ``Authentication (is )?required`` notice, so a match establishes that a human
    must act at the Desktop screen, never WHICH action. The native-query modal is the reason the
    signature exists - migrated custom-SQL sources emit exactly the shape that triggers it, and this
    bundle's own guidance says to check for it before concluding anything about a data source. Matched
    BEFORE the benign signature, so one progress element in the same window cannot erase it.
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
    prose = normalize_texts(
        text
        for text, role, source in window.items
        if role in {"ControlType.Text", "ControlType.Document"} and source in {"Name", "TextPattern"}
    )
    if prose:
        all_texts += (" ".join(prose),)
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
    """Classify ONE candidate window. Only ``benign`` is dismissible, and only it needs positive proof."""
    # pylint: disable=too-many-return-statements,too-many-branches
    title, all_texts, content = dialog_text_set(window)
    signature_hit = _match_dialog_signatures(window, all_texts)
    if signature_hit is not None:
        return signature_hit
    if not window.complete:
        return _finding(DIALOG_KIND_UNREADABLE, window, "")
    if window.message_id:
        return _finding(DIALOG_KIND_NEEDS_HUMAN, window, "")

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
    """The application FRAME - the window dialogs block - or ``None`` when its identity is AMBIGUOUS."""
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
    """Is ``window`` incapable of showing a human anything, on TWO independent grounds?"""
    return not window.owner_hwnd and (window.width <= 0 or window.height <= 0)


def dialog_candidates(
    windows: Iterable[DesktopWindow],
    *,
    frame: DesktopWindow | None = None,
) -> list[DesktopWindow]:
    """Every visible window worth CLASSIFYING - which is every one not POSITIVELY proven harmless."""
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
    frame: DesktopWindow | None = None,
) -> DialogFinding | None:
    """Fold factual classifications; the caller alone decides whether progress is its own."""
    return _fold_findings(_window_findings(tuple(windows), frame))


def _window_findings(
    windows: tuple[DesktopWindow, ...],
    frame: DesktopWindow | None,
) -> tuple[DialogFinding, ...]:
    """Classify once; positive prompts survive every one-way modality exoneration."""
    frame = frame if frame is not None else main_frame(windows)
    candidates = {id(window) for window in dialog_candidates(windows, frame=frame)}
    findings = []
    for window in windows:
        if window is frame:
            continue
        finding = classify_dialog(window)
        if id(window) in candidates or finding.kind in {DIALOG_KIND_CREDENTIAL, DIALOG_KIND_NEEDS_HUMAN}:
            findings.append(finding)
    return tuple(findings)


def _fold_findings(findings: Iterable[DialogFinding]) -> DialogFinding | None:
    """The sole same-snapshot precedence fold, shared by observations and pure policy controls."""
    found: dict[str, DialogFinding] = {}
    for finding in findings:
        if finding.kind == DIALOG_KIND_CREDENTIAL:
            return finding
        found.setdefault(finding.kind, finding)
    for kind in DIALOG_KIND_PRECEDENCE:
        if kind not in found:
            continue
        return found[kind]
    return None


def match_desktop_observer(
    windows: Iterable[DesktopWindow],
    *,
    frame: DesktopWindow | None = None,
) -> CredentialModal | None:
    """First credential-signature match across every window EXCEPT the application frame."""
    finding = dialog_verdict(windows, frame=frame)
    if finding is not None and finding.kind == DIALOG_KIND_CREDENTIAL:
        return CredentialModal(matched_text=finding.evidence, window=finding.window)
    return None


def _hwnd_value(hwnd) -> int:
    """Normalize ctypes callback HWND values (plain int on this Python, ctypes object on others)."""
    value = getattr(hwnd, "value", hwnd)
    return int(value or 0)


def _configure_user32(user32) -> None:
    """Set Win32 signatures explicitly so 64-bit handles are not truncated."""
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
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL


def _enumerate_pid_windows_with_count(pid: int) -> tuple[list[DesktopWindow], int]:
    """Read backend facts for native qualification; production callers use observe with held identity."""
    try:
        _, windows = _harvest(str(pid), 8.0, None)
        return list(windows), len(windows)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise Win32EnumerationError("backend unavailable") from error


def enumerate_pid_windows(pid: int) -> list[DesktopWindow]:
    """Enumerate visible top-level/owned windows for ``pid`` using Win32 ``EnumWindows``."""
    windows, _visited = _enumerate_pid_windows_with_count(pid)
    return windows


def inspect_desktop_observer(
    pid: int,
    enumerate_windows: WindowEnumerator = enumerate_pid_windows,
    process_is_alive: ProcessLivenessCheck = _process_alive,
) -> Observation:
    """Fold exact-window facts; this pure-policy injection seam never suppresses progress."""
    # pylint: disable=too-many-return-statements
    try:
        windows = tuple(enumerate_windows(pid))
    except Win32EnumerationError as exc:
        return Observation(unknown_reason=f"window enumeration failed: {exc}", complete=False, reason="backend_failed")
    frame = main_frame(windows)
    findings = _window_findings(windows, frame)
    finding = _fold_findings(findings)
    if finding is not None:
        complete = all(item.window.complete for item in findings)
        if finding.kind == DIALOG_KIND_CREDENTIAL:
            return Observation(
                modal=CredentialModal(finding.evidence, finding.window),
                windows=windows,
                findings=findings,
                complete=complete,
            )
        return Observation(dialog=finding, windows=windows, findings=findings, complete=complete)
    if frame is not None and frame.minimized:
        return Observation(
            unknown_reason=(
                "Power BI Desktop owner window is minimized; owned modal dialogs are hidden from enumeration"
            ),
            windows=windows,
            complete=False,
            reason="minimized",
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
            return Observation(
                desktop_unready=(
                    "Power BI Desktop enumerated no windows while its process is still running; a "
                    "window-less process is starting up or wedged and its dialog state cannot be read"
                ),
                windows=windows,
                complete=False,
                reason="no_window",
            )
        return Observation(
            process_gone=(
                "Power BI Desktop enumerated no windows and its process is no longer running; it "
                "exited or crashed before any dialog state could be observed"
            ),
            windows=windows,
            complete=False,
            reason="process_gone",
        )
    return Observation(windows=windows)


def describe_modal(modal: CredentialModal, source_hint: str | None = None) -> str:  # pylint: disable=unused-argument
    """Closed metadata only; neither source hints nor provider text are safe public evidence."""
    return f"prompt_match; hwnd={_handle_string(modal.window.hwnd)}; text withheld"


def describe_dialog_finding(finding: DialogFinding) -> str:
    """Closed metadata only: unknown UI prose is not safe verdict-line evidence."""
    kind = finding.kind if finding.kind in DIALOG_KIND_VERDICTS else "unrecognized"
    return f"kind={kind}, hwnd={finding.window.hwnd}; text withheld"


_DIALOG_TEMPLATES = {
    "privacy_warning": (
        "Potential security risk",
        "This file uses multiple data sources. Information in one data source might be shared with other "
        "data sources without your knowledge. Only open this file if you trust the sender. "
        "Do you want to open this file?",
        frozenset({"OK", "Cancel"}),
    ),
    "package_session": (
        "Something went wrong",
        "Could not find a PackageSession for the given sessionID.",
        frozenset({"Close", "Copy details to clipboard", "Report this issue", "Cancel"}),
    ),
}
_DIALOG_MESSAGES = {
    "privacy_warning": (
        "Potential security risk: multiple-source sharing/trust warning (OK / Cancel). "
        "The operator must decide whether to trust and open this file. "
        "This is a file-trust decision, not evidence about the data source."
    ),
    "package_session": (
        "Something went wrong: Desktop could not find a PackageSession for the given sessionID "
        "(Close / Copy details to clipboard / Report this issue / Cancel). "
        "This is a Desktop session error. Check unsaved-state safety before reopening; "
        "source state remains unestablished."
    ),
    "WITHHELD": "Dialog text withheld; inspect this Desktop dialog locally. Source state unestablished.",
    "CANNOT_READ": (
        "Dialog text could not be read safely; inspect this Desktop dialog locally. Source state unestablished."
    ),
}


def _diagnostic_template(payload: dict) -> str | None:
    """Match the entire title/instruction/button structure, never keywords in arbitrary fields."""
    title = " ".join(payload["Title"].split())
    prose, buttons = [], set()
    for item in payload["Items"]:
        text = " ".join(item["Text"].split())
        role, source = item["Role"], item["Source"]
        if role == "ControlType.Button" and source == "Name":
            buttons.add(text)
        elif role in {"ControlType.Text", "ControlType.Document"} and source in {"Name", "TextPattern"}:
            if text != title and text not in prose:
                prose.append(text)
        elif text:
            return None
    prose = [text for text in prose if text not in buttons]
    for message_id, (expected_title, instruction, expected_buttons) in _DIALOG_TEMPLATES.items():
        if title == expected_title and " ".join(prose) == instruction and buttons == expected_buttons:
            return message_id
    return None


def diagnose_dialog(pid: int, window: DesktopWindow, *, timeout_seconds: float = 8.0) -> tuple[str, str | None, str]:
    """Explain the already harvested snapshot; never launch a second diagnostic observer."""
    # Exact identity types deliberately reject bools and coercible values.
    # pylint: disable=unidiomatic-typecheck
    if (
        type(pid) is not int
        or type(window.hwnd) is not int
        or pid <= 0
        or window.hwnd <= 0
        or not 0 < timeout_seconds <= 8.0
    ):  # pylint: disable=unidiomatic-typecheck
        return "CANNOT_READ", None, "invalid_target"
    if window.pid != str(pid) or not window.complete or not window.items:
        return "CANNOT_READ", None, "incomplete_read"
    message_id = _diagnostic_template(
        {"Title": window.title, "Items": [dict(zip(("Text", "Role", "Source"), item)) for item in window.items]}
    )
    return ("OBSERVED", message_id, "template_match") if message_id else ("WITHHELD", None, "unknown_template")


def print_dialog_diagnostic(pid: int, window: DesktopWindow, *, stream=None) -> None:
    """Print only closed diagnostic fields and predefined prose, never harvested text or stderr."""
    status, message_id, reason = diagnose_dialog(pid, window)
    record = {
        "status": status,
        "message_id": message_id,
        "reason": reason,
        "pid": str(pid),
        "hwnd": str(window.hwnd),
        "message": _DIALOG_MESSAGES[message_id or status],
    }
    # As with LOCAL_IMAGE, string-encoded numbers must not trip whole-transcript numeric scanners.
    print("DIALOG_DIAGNOSTIC " + json.dumps(record).replace("0", r"\u0030"), file=stream, flush=True)


# Per-kind operator guidance. Every string here is deliberately MARKER-FREE (issue #153): it must not
# contain any `probe_live_source.CREDENTIAL_MARKERS` substring - "credential", "sign in", "signed in",
# "authentication", "login", "oauth", "access token", "unauthorized" - because the parent classifier
# scans a failing child's whole transcript as free text. These verdicts mean "we could not probe", and
# prose that reads as a sign-on problem would relabel them the very hard stop they exist to avoid.
# `tests/test_probe_live_source_verdict.py` drives the real emitters through the real classifier so
# this property is gated rather than merely intended.
DIALOG_KIND_GUIDANCE = {
    DIALOG_KIND_NEEDS_HUMAN: (
        "this matched a KNOWN human-blocking prompt signature (blocking_prompt_signature.regex), whose "
        "alternatives span the native database query approval AND a notice that a person must complete "
        "something at the screen - so a person must act on the prompt Power BI Desktop is showing; this "
        "detector does not establish WHICH action, so read the prompt before assuming"
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
        "its text was read but matched no prompt signature this detector knows, and that list is not "
        "exhaustive - so this window is unaccounted for and nothing here was settled either way; look "
        "at the Desktop screen"
    ),
    DIALOG_KIND_BENIGN: (
        "a refresh is already running on this pid - wait for it, or cancel the stale one; do not stack "
        "a second refresh on it"
    ),
}


def dialog_guidance(finding: DialogFinding) -> str:
    """Operator guidance for ``finding``, or a safe generic line for an unmapped kind."""
    return DIALOG_KIND_GUIDANCE.get(finding.kind, "look at the Desktop screen before continuing")


_OPERATION_MESSAGES = {
    "CREDENTIAL_MISSING": "A positive prompt was observed; a human must act locally. Source state unestablished.",
    "CREDENTIAL_UNKNOWN": "Dialog evidence stayed indeterminate to the deadline; inspect Desktop before retrying.",
    "DESKTOP_GONE": (
        "Power BI Desktop enumerated no windows and its process is no longer running; it exited or crashed "
        "before any dialog state could be observed"
    ),
    "DESKTOP_UNREADY": (
        "Power BI Desktop enumerated no windows while its process is still running; a window-less process "
        "is starting up or wedged and its dialog state cannot be read"
    ),
}


def print_operation_refusal(
    prefix: str,
    pid: int,
    code: str,
    *,
    finding: DialogFinding | None = None,
    reason: str | None = None,
) -> None:
    """Format existing operation tokens only; exit bands and operation results stay with callers."""
    if prefix not in {"REFRESH", "PREFLIGHT"} or type(pid) is not int or not 0 < pid < 2**32:  # pylint: disable=unidiomatic-typecheck
        raise ValueError("operation diagnostic identity")
    if finding is not None:
        if DIALOG_KIND_VERDICTS.get(finding.kind) != code:
            raise ValueError("operation diagnostic vocabulary")
        detail = describe_dialog_finding(finding)
    elif code in _OPERATION_MESSAGES:
        detail = _OPERATION_MESSAGES[code]
        if code == "DESKTOP_UNREADY" and reason == "session_error":
            detail = "Local Desktop session error; inspect this Desktop locally. Source state unestablished."
    else:
        raise ValueError("operation diagnostic vocabulary")
    print(f"{prefix}: {code} pid={pid}; {detail}")
    if finding is not None:
        print_dialog_diagnostic(pid, finding.window)
        print(f"  {dialog_guidance(finding)}")


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
    """Print the no-dialog, self-bounded refresh warning before the wait starts."""
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
    interpolates ``reason``, a caller-supplied string from ``inspect_desktop_observer`` that the
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
    """Loudly note the first time the wait latches an indeterminate window state (issues #154, #158)."""
    print(
        f"PID {pid} entered an indeterminate window state mid-wait ({reason}); latching this "
        "observation - a later clear cannot un-see it, because the evidence a blocking dialog would "
        "leave is no longer readable. This run ends at the deadline with a distinct indeterminate "
        "verdict, never a slow-source timeout.",
        flush=True,
    )


def print_dialog_observed_notice(pid: int, finding: DialogFinding) -> None:
    """Loudly note the first time a bounded wait sees a dialog it could not dismiss (issue #376).

    Latched rather than acted on: it did not match the hard-stop signature, so it must not abort a
    refresh that is otherwise healthy and may still finish - but it also means "no modal appeared" is
    no longer established, so a quiet deadline must not erase it either. Not matching that signature
    rules nothing IN or OUT about the source; it only says this observation is not the hard stop.
    Marker-free, and not a ``REFRESH:`` verdict line, so the parent classifier cannot mistake it for a
    verdict (issue #153).
    """
    print(
        f"PID {pid} has a dialog up that could not be shown to be harmless ({finding.kind}); latching "
        f"it - {dialog_guidance(finding)}. The wait continues, because this window did not match the "
        "hard-stop signature and an otherwise healthy refresh must not be aborted by it; it surfaces "
        "again at the deadline.",
        flush=True,
    )


def raise_terminal_detection(pid: int, state: Observation, source_hint: str | None = None) -> None:
    """Raise for the two observations that end a bounded wait IMMEDIATELY."""
    if state.state == "CREDENTIAL_REQUIRED":
        modal = state.modal or CredentialModal("", state.dialog.window)
        raise CredentialMissingError(pid, modal, source_hint)
    if state.state == "DESKTOP_SESSION_ERROR":
        raise DesktopUnreadyError(pid, "session_error")
    if state.state in {"HUMAN_ACTION_REQUIRED", "NATIVE_QUERY_APPROVAL"} and state.dialog is not None:
        raise DialogFoundError(pid, state.dialog)
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


# pylint: disable=too-many-arguments
def join_with_credential_poll(
    worker,
    *,
    pid: int | None,
    total_timeout: float,
    heartbeat_seconds: float,
    poll_seconds: float,
    source_hint: str | None = None,
    detector: Callable[[int], Observation] | None = None,
    initial_state: Observation | None = None,
    deadline: float | None = None,
    before_wait: Callable[[], None] | None = None,
    heartbeat: Callable[[float, float], None] | None = print_refresh_heartbeat,
    wait_limit: Callable[[], float] | None = None,
    notices: bool = True,
    completed_at: Callable[[], float | None] | None = None,
) -> bool:
    """Wait for ``worker`` while polling for a late credential dialog."""
    # Waived, not shaved: the locals are eight injected parameters plus one latch per observation this
    # wait must not lose (unknown / desktop-unready / dialog). Folding the latches into a container
    # would hide exactly the thing this function exists to keep visible.
    # pylint: disable=too-many-locals,too-many-statements
    deadline = time.monotonic() + total_timeout if deadline is None else deadline
    if detector is None and pid is not None:

        def inspect(target: int) -> Observation:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("observation deadline exhausted before start")
            return observe(
                target,
                timeout_s=min(8.0, remaining),
                expected_process_start=initial_state.process_started_at if initial_state else None,
            )

        detector = inspect
    latch = _ObservationLatch()
    if initial_state is not None and pid is not None:
        raise_terminal_detection(pid, latch.add(initial_state), source_hint)
    started = deadline - total_timeout
    next_heartbeat = heartbeat_seconds
    while worker.is_alive():
        elapsed = time.monotonic() - started
        remaining = max(0.0, total_timeout - elapsed)
        if remaining <= 0:
            break
        if before_wait is not None:
            before_wait()
        worker.join(
            min(
                remaining,
                poll_seconds,
                max(0.01, next_heartbeat - elapsed),
                max(0.01, wait_limit()) if wait_limit is not None else remaining,
            )
        )
        elapsed = time.monotonic() - started
        if not worker.is_alive():
            break
        if time.monotonic() >= deadline:
            break
        if pid is not None:
            try:
                state = detector(pid)
            except TimeoutError:
                break
            if time.monotonic() >= deadline and state.state in _TERMINAL_STATES:
                state = Observation(pid=str(pid), complete=False, unknown_reason="timeout", reason="timeout")
            prior_dialog, prior_unknown = latch.dialog, latch.unknown
            raise_terminal_detection(pid, latch.add(state), source_hint)
            if notices and prior_dialog is None and latch.dialog is not None:
                print_dialog_observed_notice(pid, latch.dialog)
            if notices and prior_unknown is None and latch.unknown is not None:
                print_indeterminate_state_notice(pid, latch.unknown)
        if elapsed >= next_heartbeat and worker.is_alive():
            if heartbeat is not None:
                heartbeat(elapsed, total_timeout)
            next_heartbeat += heartbeat_seconds
    settled_at = completed_at() if completed_at is not None else None
    if not worker.is_alive() and settled_at is not None and settled_at < deadline:
        return True
    if worker.is_alive() or time.monotonic() >= deadline:
        if pid is not None:
            if latch.terminal is not None:
                raise_terminal_detection(pid, latch.terminal, source_hint)
            raise_latched_verdict(
                pid,
                desktop_unready=latch.desktop_unready,
                dialog=latch.dialog,
                unknown=latch.unknown,
            )
        return False
    return True


# pylint: enable=too-many-arguments


_HARVEST_KEYS = {
    "schema",
    "pid",
    "process_started_at_before",
    "process_started_at_after",
    "complete",
    "reason",
    "windows",
}
_WINDOW_KEYS = {
    "hwnd",
    "owner_hwnd",
    "target_before",
    "target_after",
    "owner_before",
    "owner_after",
    "visible",
    "minimized",
    "enabled",
    "owner_enabled",
    "width",
    "height",
    "title",
    "texts",
    "items",
    "truncated",
    "patterns_incomplete",
}
_REASONS = frozenset(
    {
        "complete",
        "prompt_match",
        "template_match",
        "progress_match",
        "mixed_content",
        "unknown_content",
        "caption_only",
        "no_text",
        "timeout",
        "truncated",
        "patterns_incomplete",
        "target_changed",
        "minimized",
        "ambiguous",
        "unsupported",
        "backend_failed",
        "invalid_payload",
        "no_window",
        "process_gone",
    }
)


def _handle_string(value: int) -> str | None:
    return hex(value) if value else None


def _decimal_identity(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _handle_value(value: object, *, nullable: bool = True) -> int:
    if value is None and nullable:
        return 0
    if not isinstance(value, str) or not re.fullmatch(r"0x[1-9a-f][0-9a-f]{0,15}", value):
        raise ValueError("handle")
    return int(value, 16)


def _window_fact(payload: dict) -> DesktopWindow:
    """Validate native facts before interpreting private text; identity loss always refuses."""
    # pylint: disable=too-many-branches,unidiomatic-typecheck
    if not isinstance(payload, dict) or set(payload) != _WINDOW_KEYS:
        raise ValueError("window shape")
    hwnd = _handle_value(payload["hwnd"], nullable=False)
    owner = _handle_value(payload["owner_hwnd"])
    if any(payload[key] is not True for key in ("target_before", "target_after")) or any(
        _handle_value(payload[key]) != owner for key in ("owner_before", "owner_after")
    ):
        raise ValueError("target_changed")
    if (
        any(
            type(payload[key]) is not bool
            for key in (  # pylint: disable=unidiomatic-typecheck
                "visible",
                "minimized",
                "enabled",
                "truncated",
                "patterns_incomplete",
            )
        )
        or payload["visible"] is not True
    ):
        raise ValueError("flags")
    if payload["owner_enabled"] is not None and type(payload["owner_enabled"]) is not bool:  # pylint: disable=unidiomatic-typecheck
        raise ValueError("owner")
    if not owner and payload["owner_enabled"] is not None:
        raise ValueError("owner")
    if any(type(payload[key]) is not int or payload[key] < 0 for key in ("width", "height")):  # pylint: disable=unidiomatic-typecheck
        raise ValueError("extent")
    if not isinstance(payload["title"], str) or len(payload["title"]) > 8000:
        raise ValueError("title")
    items, texts = payload["items"], payload["texts"]
    if not isinstance(items, list) or not isinstance(texts, list) or len(items) + len(texts) > 4000:
        raise ValueError("elements")
    if any(not isinstance(text, str) or len(text) > 8000 for text in texts):
        raise ValueError("texts")
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != {"Text", "Role", "Source"}
            or any(not isinstance(value, str) or len(value) > 8000 for value in item.values())
        ):
            raise ValueError("item")
        if item["Source"] not in {"Name", "TextPattern"} or not item["Role"].startswith("ControlType."):
            raise ValueError("role")
        if item["Role"] in {"ControlType.Edit", "ControlType.ComboBox"}:
            raise ValueError("editable")
    complete = not (
        payload["truncated"] or payload["patterns_incomplete"] or (owner and payload["owner_enabled"] is None)
    )
    message = _diagnostic_template({"Title": payload["title"], "Items": items}) if complete else None
    return DesktopWindow(
        title=payload["title"],
        class_name="",
        width=payload["width"],
        height=payload["height"],
        texts=tuple(texts) + tuple(item["Text"] for item in items),
        minimized=payload["minimized"],
        hwnd=hwnd,
        owner_hwnd=owner,
        owner_enabled=payload["owner_enabled"],
        complete=complete,
        reason="truncated" if payload["truncated"] else ("patterns_incomplete" if not complete else "complete"),
        items=tuple((item["Text"], item["Role"], item["Source"]) for item in items),
        message_id=message,
    )


def _harvest_payload(
    raw: str, pid: str, expected_process_start: str | None
) -> tuple[str | None, tuple[DesktopWindow, ...]]:
    if len(raw.encode("utf-8")) > 131072:
        raise ValueError("payload size")
    payload = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or set(payload) != _HARVEST_KEYS:
        raise ValueError("harvest shape")
    if payload["schema"] != "desktop.harvest.v1" or payload["pid"] != pid or not isinstance(payload["pid"], str):
        raise ValueError("identity")
    before, after = payload["process_started_at_before"], payload["process_started_at_after"]
    if payload["complete"] is not True or payload["reason"] != "complete":
        raise ValueError("incomplete")
    if before is None and after is None and payload["windows"] == []:
        return None, ()
    if (
        not _decimal_identity(before)
        or before != after
        or (expected_process_start is not None and before != expected_process_start)
    ):
        raise ValueError("target_changed")
    windows = payload["windows"]
    if not isinstance(windows, list) or len(windows) > 4000:
        raise ValueError("windows")
    if (
        sum(
            len(window.get("items", [])) + len(window.get("texts", []))
            for window in windows
            if isinstance(window, dict)
        )
        > 4000
    ):
        raise ValueError("elements")
    result = tuple(replace(_window_fact(window), pid=pid, process_started_at=before) for window in windows)
    if len({window.hwnd for window in result}) != len(result):
        raise ValueError("duplicate window")
    return before, result


def _harvest(
    pid: str,
    timeout_s: float,
    expected_process_start: str | None,
    *,
    _owner=None,
) -> tuple[str | None, tuple[DesktopWindow, ...]]:
    """One bounded child, no nested processes or native calls on uncancellable Python threads."""
    if os.name != "nt":
        raise OSError("unsupported")
    deadline = time.monotonic() + timeout_s
    start = subprocess.Popen if _owner is None else _owner.spawn
    child = start(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(Path(__file__).with_name("_desktop_uia.ps1")),
            "-DesktopPid",
            pid,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        output, _ = child.communicate(timeout=max(0.0, deadline - time.monotonic()))
        if child.returncode != 0:
            raise ValueError("backend_failed")
        return _harvest_payload(output.decode("utf-8-sig"), pid, expected_process_start)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        if _owner is not None:
            _owner.unregister(child)


def observe(
    pid: int | str,
    *,
    hwnd: int | str | None = None,
    expected_process_start: str | None = None,
    timeout_s: float = 8.0,
    evidence=None,
    _owner=None,
) -> Observation:
    """Read one immutable snapshot; callers holding an identity must supply its exact start token."""
    started = time.monotonic()
    if isinstance(pid, int) and not isinstance(pid, bool):
        pid = str(pid)
    if not _decimal_identity(pid) or not 0 < int(pid) < 2**32:
        raise ValueError("pid")
    if expected_process_start is not None and not _decimal_identity(expected_process_start):
        raise ValueError("process start")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or not 0 < timeout_s <= 8
    ):
        raise ValueError("timeout")
    target = _handle_value(hwnd) if isinstance(hwnd, str) else hwnd
    if target is not None and (type(target) is not int or target <= 0):  # pylint: disable=unidiomatic-typecheck
        raise ValueError("hwnd")
    state = Observation(pid=pid, complete=False, reason="unsupported", unknown_reason="unsupported")
    try:
        process_start, windows = (
            _harvest(pid, timeout_s, expected_process_start)
            if _owner is None
            else _harvest(pid, timeout_s, expected_process_start, _owner=_owner)
        )
        if process_start is None:
            state = Observation(pid=pid, complete=False, reason="process_gone", process_gone="process_gone")
        elif target is not None and not any(window.hwnd == target for window in windows):
            state = Observation(
                pid=pid,
                process_started_at=process_start,
                complete=False,
                reason="target_changed",
                unknown_reason="target_changed",
            )
        else:
            state = inspect_desktop_observer(int(pid), lambda _: windows)
            state = replace(
                state,
                pid=pid,
                process_started_at=process_start,
                reason=_observation_reason(state),
                target_window=next((window for window in windows if window.hwnd == target), None),
            )
            finding = state.modal or state.dialog
            if target is not None and finding is not None and finding.window.hwnd != target:
                state = Observation(
                    pid=pid,
                    process_started_at=process_start,
                    complete=False,
                    unknown_reason="ambiguous",
                    reason="ambiguous",
                    findings=state.findings,
                    target_window=state.target_window,
                )
    except subprocess.TimeoutExpired:
        state = replace(state, reason="timeout", unknown_reason="timeout")
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        reason = str(error) if str(error) in _REASONS else "invalid_payload"
        state = replace(state, reason=reason, unknown_reason=reason)
    elapsed_ms = max(0.0, (time.monotonic() - started) * 1000)
    if elapsed_ms > timeout_s * 1000:
        state = Observation(pid=pid, complete=False, reason="timeout", unknown_reason="timeout")
    state = replace(state, elapsed_ms=elapsed_ms)
    if evidence is not None:
        evidence.enqueue(int(pid), state)
    return state


def _observation_reason(state: Observation) -> str:
    if state.modal:
        return "prompt_match"
    if state.dialog:
        if not state.dialog.window.complete:
            return state.dialog.window.reason
        return {
            DIALOG_KIND_NEEDS_HUMAN: "template_match" if state.dialog.window.message_id else "prompt_match",
            DIALOG_KIND_BENIGN: "progress_match",
            DIALOG_KIND_MIXED_CONTENT: "mixed_content",
            DIALOG_KIND_BENIGN_TITLE_ONLY: "caption_only",
            DIALOG_KIND_UNREADABLE: "no_text",
        }.get(state.dialog.kind, "unknown_content")
    if state.unknown_reason:
        return "minimized"
    if state.desktop_unready:
        return "no_window"
    return "complete"


@dataclass
class Watch:
    """Invocation-local latch lattice; no background callback may mutate a settled observation."""

    # One invocation owns these locks, children, identity, deadline and retained evidence together.
    # pylint: disable=too-many-instance-attributes

    pid: int | str
    deadline: float
    operation_id: str
    expected_process_start: str | None = None
    evidence: object = None
    _latch: _ObservationLatch = field(default_factory=_ObservationLatch, repr=False)
    closed: bool = False
    _children: list = field(default_factory=list, repr=False)
    _children_lock: object = field(default_factory=threading.Lock, repr=False)
    _observe_lock: object = field(default_factory=threading.Lock, repr=False)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def latched(self) -> tuple[Observation, ...]:
        """Representative immutable uncertainty records, bounded by the closed state vocabulary."""
        return self._latch.snapshots

    @property
    def terminal(self) -> Observation | None:
        """The first established terminal observation."""
        return self._latch.terminal

    def spawn(self, *args, **kwargs):
        """Atomically admit one owned native child or refuse after the settlement barrier."""
        with self._children_lock:
            if self.closed:
                raise OSError("timeout")
            child = subprocess.Popen(*args, **kwargs)  # pylint: disable=consider-using-with
            self._children.append(child)
            return child

    def unregister(self, child) -> None:
        """Forget a helper only after its owning harvest has reaped it."""
        with self._children_lock:
            if child in self._children:
                self._children.remove(child)

    def close(self) -> None:
        """Settle only this invocation's harvests; Desktop is never a termination target."""
        with self._children_lock:
            self.closed = True
            for child in self._children:
                if child.poll() is None:
                    child.kill()
                child.wait()
            self._children.clear()

    def observe(self, *, timeout_s: float = 8.0) -> Observation:
        """Serialize snapshots against one caller deadline and held process identity."""
        remaining = self.deadline - time.monotonic()
        # A context manager cannot enforce the remaining acquisition deadline.
        acquired = self._observe_lock.acquire(timeout=max(0.0, min(timeout_s, remaining)))  # pylint: disable=consider-using-with
        try:
            remaining = self.deadline - time.monotonic()
            if not acquired or self.closed or remaining <= 0:
                return Observation(pid=str(self.pid), complete=False, unknown_reason="timeout", reason="timeout")
            snapshot = observe(
                self.pid,
                expected_process_start=self.expected_process_start,
                timeout_s=min(timeout_s, remaining),
                evidence=self.evidence,
                _owner=self,
            )
            with self._children_lock:
                if self.closed:
                    return Observation(
                        pid=str(self.pid),
                        complete=False,
                        reason="timeout",
                        unknown_reason="timeout",
                        operation_id=self.operation_id,
                    )
                if self.expected_process_start is None and snapshot.process_started_at is not None:
                    self.expected_process_start = snapshot.process_started_at
                return replace(snapshot, operation_id=self.operation_id)
        finally:
            if acquired:
                self._observe_lock.release()

    def record(self, observation: Observation) -> Observation:
        """First terminal wins; uncertainty never disappears behind later quiet evidence."""
        with self._children_lock:
            if self.closed:
                raise RuntimeError("watch settled")
            if type(observation) is not Observation:  # pylint: disable=unidiomatic-typecheck
                raise ValueError("observation")
            if observation.pid != str(self.pid) or (
                observation.state not in {"UNREADABLE_DIALOG", "PROCESS_GONE"}
                and not _decimal_identity(observation.process_started_at)
            ):
                observation = Observation(
                    pid=str(self.pid), complete=False, reason="target_changed", unknown_reason="target_changed"
                )
            if time.monotonic() >= self.deadline:
                observation = Observation(pid=str(self.pid), complete=False, unknown_reason="timeout", reason="timeout")
            observation = replace(observation, operation_id=self.operation_id)
            if (
                self.expected_process_start is not None
                and observation.process_started_at != self.expected_process_start
                and (observation.process_started_at is not None or observation.complete)
                and observation.state != "PROCESS_GONE"
            ):
                observation = Observation(
                    pid=str(self.pid),
                    complete=False,
                    unknown_reason="target_changed",
                    reason="target_changed",
                    operation_id=self.operation_id,
                )
            return self._latch.add(observation)

    def __iter__(self):
        if not self.closed and time.monotonic() >= self.deadline:
            yield self.record(
                Observation(pid=str(self.pid), complete=False, reason="timeout", unknown_reason="timeout")
            )
            return
        while not self.closed and time.monotonic() < self.deadline:
            yield self.record(self.observe(timeout_s=min(8.0, max(0.001, self.deadline - time.monotonic()))))
            if self.terminal is not None:
                return
            time.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, self.deadline - time.monotonic())))


def watch(
    pid: int | str, *, deadline: float, operation_id: str, expected_process_start: str | None = None, evidence=None
) -> Watch:
    """Create one observation supervisor without starting native work at import/construction time."""
    canonical_pid = str(pid) if type(pid) is int else pid  # pylint: disable=unidiomatic-typecheck
    if not _decimal_identity(canonical_pid) or not 0 < int(canonical_pid) < 2**32:
        raise ValueError("pid")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ValueError("deadline")
    if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
        raise ValueError("operation id")
    if expected_process_start is not None and not _decimal_identity(expected_process_start):
        raise ValueError("process start")
    return Watch(pid, deadline, operation_id, expected_process_start, evidence)


IMAGE_CAPTURE_SECONDS = 8.0
IMAGE_LIFETIME_SECONDS = 60.0
IMAGE_DIRECTORY_ENV = "PBIP_EVIDENCE_DIR"
IMAGE_SCHEMA = "pbip.window-image.v1"
_MAX_IMAGE_BYTES = 20_000_000
_IMAGE_NAME = re.compile(r"_ui-image-[1-9a-fx]{32}\.png")
_IMAGE_CHECKS = ("before", "after_render", "after_write")
_IMAGE_RESULTS = {
    "ACQUIRED",
    "TARGET_CHANGED",
    "BLANK_OR_INCOMPLETE",
    "CAPTURE_FAILED",
    "WRITE_FAILED",
    "UNSUPPORTED",
    "CAPTURE_CLEANUP_FAILED",
}


class _ImageUnavailable(RuntimeError):
    """A closed, content-free acquisition result; never a dialog classification."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.checks: dict[str, bool | None] = dict.fromkeys(_IMAGE_CHECKS)


def _same_image_target(user32: ctypes.CDLL, pid: int, window: DesktopWindow) -> bool:
    """Recheck the detected HWND/PID/owner, not a newly selected window."""
    target_pid = wintypes.DWORD(0)
    owner_pid = wintypes.DWORD(0)
    target_thread = user32.GetWindowThreadProcessId(window.hwnd, ctypes.byref(target_pid))
    owner_thread = user32.GetWindowThreadProcessId(window.owner_hwnd, ctypes.byref(owner_pid))
    return bool(
        target_thread
        and owner_thread
        and target_pid.value == pid
        and owner_pid.value == pid
        and window.hwnd != window.owner_hwnd
        and user32.IsWindowVisible(window.hwnd)
        and _hwnd_value(user32.GetWindow(window.hwnd, GW_OWNER)) == window.owner_hwnd
        and not user32.IsWindowEnabled(window.owner_hwnd)
    )


def _image_user32() -> ctypes.CDLL:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    _configure_user32(user32)
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    return user32


def _set_image_dpi_awareness(user32: ctypes.CDLL) -> int:
    """Use physical coordinates on the capture thread, not the observer or Desktop's threads."""
    try:
        setter = user32.SetThreadDpiAwarenessContext
    except AttributeError:
        raise _ImageUnavailable("UNSUPPORTED") from None
    setter.argtypes, setter.restype = [ctypes.c_void_p], ctypes.c_void_p
    # PER_MONITOR_AWARE v1 works from Windows 10 1607; v2 is not needed for a memory-DC capture.
    previous = setter(-3)
    if not previous:
        raise _ImageUnavailable("BLANK_OR_INCOMPLETE")
    return previous


def _image_extent(user32: ctypes.CDLL, hwnd: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise _ImageUnavailable("TARGET_CHANGED")
    frame = wintypes.RECT()
    try:
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        result = dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(frame), ctypes.sizeof(frame))
    except (AttributeError, OSError):
        raise _ImageUnavailable("BLANK_OR_INCOMPLETE") from None
    # DWM extended-frame bounds are physical, unlike a virtualized GetWindowRect (#686).
    # Keep the full window rectangle: PrintWindow includes invisible resize borders too.
    if (
        result != 0
        or not rect.left <= frame.left < frame.right <= rect.right
        or not rect.top <= frame.top < frame.bottom <= rect.bottom
    ):
        raise _ImageUnavailable("BLANK_OR_INCOMPLETE")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    # Allocation bounds only. Neither size nor successful rasterisation establishes a prompt.
    if width <= 0 or height <= 0 or width * height > 4_000_000:
        raise _ImageUnavailable("BLANK_OR_INCOMPLETE")
    return width, height


def _render_exact_window(user32: ctypes.CDLL, hwnd: int) -> tuple[int, int, bytes]:
    """Print ONE HWND into a memory DIB. No screen DC, foreground, restore or activation."""
    # The GDI handles and selected object must live until after the synchronous PrintWindow.
    # pylint: disable=too-many-locals
    width, height = _image_extent(user32, hwnd)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    for name, arguments, result in (
        ("CreateCompatibleDC", [wintypes.HDC], wintypes.HDC),
        (
            "CreateDIBSection",
            [
                wintypes.HDC,
                ctypes.c_void_p,
                wintypes.UINT,
                ctypes.POINTER(ctypes.c_void_p),
                wintypes.HANDLE,
                wintypes.DWORD,
            ],
            wintypes.HBITMAP,
        ),
        ("SelectObject", [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
        ("DeleteObject", [wintypes.HANDLE], wintypes.BOOL),
        ("DeleteDC", [wintypes.HDC], wintypes.BOOL),
        ("GdiFlush", [], wintypes.BOOL),
    ):
        function = getattr(gdi, name)
        function.argtypes, function.restype = arguments, result
    dc = gdi.CreateCompatibleDC(None)
    bitmap = previous = None
    try:
        if not dc:
            raise _ImageUnavailable("CAPTURE_FAILED")
        header = ctypes.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0))
        bits = ctypes.c_void_p()
        bitmap = gdi.CreateDIBSection(dc, header, 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise _ImageUnavailable("CAPTURE_FAILED")
        previous = gdi.SelectObject(dc, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            previous = None
            raise _ImageUnavailable("CAPTURE_FAILED")
        # Untouched pixels expose a partial/no-op renderer instead of publishing that as evidence.
        sentinel = b"\xad\x53\xc7\x00"
        ctypes.memmove(bits, sentinel * (width * height), width * height * 4)
        if not user32.PrintWindow(hwnd, dc, 2):  # PW_RENDERFULLCONTENT, never a different HWND
            raise _ImageUnavailable("CAPTURE_FAILED")
        if not gdi.GdiFlush():
            raise _ImageUnavailable("CAPTURE_FAILED")
        if _image_extent(user32, hwnd) != (width, height):
            raise _ImageUnavailable("TARGET_CHANGED")
        raw = ctypes.string_at(bits, width * height * 4)
        rgb = bytearray(width * height * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[0::4]
        if rgb == rgb[:3] * (width * height) or any(
            rgb[index : index + 3] == b"\xc7\x53\xad" for index in range(0, len(rgb), 3)
        ):
            raise _ImageUnavailable("BLANK_OR_INCOMPLETE")
        return width, height, bytes(rgb)
    finally:
        if previous:
            gdi.SelectObject(dc, previous)
        released = [bool(gdi.DeleteObject(bitmap)) if bitmap else True, bool(gdi.DeleteDC(dc)) if dc else True]
        if not all(released):
            raise _ImageUnavailable("CAPTURE_CLEANUP_FAILED")


def _image_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode the memory raster using only the standard library."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\0" + rgb[row * width * 3 : (row + 1) * width * 3] for row in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _open_private_image(path: Path, *, existing: bool = False, delete_on_close: bool = False) -> BinaryIO:
    """Open a shared-delete stream; new files get an atomic protected owner-only DACL.

    The observer retains the delete-on-close handle. Kernel handle cleanup therefore removes the
    image even when the observer is forcibly terminated. Readers must allow FILE_SHARE_DELETE.
    """
    if sys.platform != "win32":
        raise _ImageUnavailable("UNSUPPORTED")
    # Windows-only import and native handle transfer; no optional Python dependency.
    # pylint: disable=import-outside-toplevel
    try:
        import msvcrt

        descriptor_flags = os.O_RDWR | getattr(os, "O_BINARY") | getattr(os, "O_NOINHERIT")
    except (ImportError, AttributeError):
        raise _ImageUnavailable("UNSUPPORTED") from None

    class SecurityAttributes(ctypes.Structure):  # pylint: disable=too-few-public-methods
        """SECURITY_ATTRIBUTES for atomic private file creation."""

        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    descriptor = ctypes.c_void_p()
    handle = None
    try:
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None
        ):
            raise _ImageUnavailable("WRITE_FAILED")
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        access = 0xC0000000 | (0x10000 if delete_on_close else 0)
        flags = 0x100 | (0x04000000 if delete_on_close else 0)
        handle = kernel.CreateFileW(str(path), access, 7, ctypes.byref(attributes), 3 if existing else 1, flags, None)
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise _ImageUnavailable("WRITE_FAILED")
        descriptor_fd = msvcrt.open_osfhandle(handle, descriptor_flags)
        handle = None  # the file descriptor now owns it
        return os.fdopen(descriptor_fd, "r+b")
    finally:
        if handle:
            kernel.CloseHandle(handle)
        if descriptor:
            kernel.LocalFree(descriptor)


def _write_private_image(path: Path, data: bytes, *, existing: bool = False) -> None:
    """Write only the reserved image in production; CreateNew remains the standalone default."""
    with _open_private_image(path, existing=existing) as image:
        image.write(data)
        image.truncate()


def _capture_exact_image(pid: int, window: DesktopWindow, path: Path, *, existing: bool = False) -> dict:
    """Acquisition only. Checks bracket PrintWindow AND the write, without reading any UI text."""
    if sys.platform != "win32":
        raise _ImageUnavailable("UNSUPPORTED")
    user32 = _image_user32()
    checks = dict.fromkeys(_IMAGE_CHECKS)
    previous_dpi = None
    try:
        previous_dpi = _set_image_dpi_awareness(user32)
        checks["before"] = _same_image_target(user32, pid, window)
        if not checks["before"]:
            raise _ImageUnavailable("TARGET_CHANGED")
        width, height, rgb = _render_exact_window(user32, window.hwnd)
        checks["after_render"] = _same_image_target(user32, pid, window)
        if not checks["after_render"]:
            raise _ImageUnavailable("TARGET_CHANGED")
        png = _image_png(width, height, rgb)
        try:
            _write_private_image(path, png, existing=existing)
        except OSError:
            raise _ImageUnavailable("WRITE_FAILED") from None
        checks["after_write"] = _same_image_target(user32, pid, window)
        if not checks["after_write"]:
            raise _ImageUnavailable("TARGET_CHANGED")
    except _ImageUnavailable as exc:
        exc.checks = checks
        raise
    finally:
        if previous_dpi and not user32.SetThreadDpiAwarenessContext(previous_dpi):
            raise _ImageUnavailable("CAPTURE_FAILED")
    return {
        "status": "ACQUIRED",
        "ownership_checks": checks,
        "dimensions": {"width": width, "height": height},
        "sha256": hashlib.sha256(png).hexdigest(),
    }


def _image_child(arguments: list[str]) -> int:
    """Same-file killable child. Its private pipe contains only closed acquisition metadata."""
    payload = {}
    try:
        pid, hwnd, owner = (int(value) for value in arguments[:3])
        if len(arguments) != 4 or min(pid, hwnd, owner) <= 0 or not _IMAGE_NAME.fullmatch(arguments[3]):
            raise _ImageUnavailable("CAPTURE_FAILED")
        window = DesktopWindow("", "", 0, 0, hwnd=hwnd, owner_hwnd=owner, owner_enabled=False)
        payload = _capture_exact_image(pid, window, Path(arguments[3]), existing=True)
    except _ImageUnavailable as exc:
        code = str(exc) if str(exc) in _IMAGE_RESULTS else "CAPTURE_FAILED"
        payload["ownership_checks"] = exc.checks
    except BaseException:  # pylint: disable=broad-exception-caught
        # Never forward native exceptions, file paths, UI text or image bytes into a transcript.
        code = "CAPTURE_FAILED"
    else:
        code = "ACQUIRED"
    payload["status"] = code
    print(json.dumps(payload, separators=(",", ":")), flush=True)
    return 0 if code == "ACQUIRED" else 4


def _evidence_directory(directory: Path, basename: str) -> Path:
    """Require explicit local scratch outside artifacts and, inside Git, positively ignored."""
    directory = directory.absolute()
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDriveTypeW.argtypes, kernel.GetDriveTypeW.restype = [wintypes.LPCWSTR], wintypes.UINT
        if str(directory).startswith("\\\\") or kernel.GetDriveTypeW(directory.anchor) not in (3, 6):
            raise _ImageUnavailable("UNSAFE_EVIDENCE_DIR")
    if not directory.is_dir():
        raise _ImageUnavailable("UNSAFE_EVIDENCE_DIR")
    for ancestor in (directory, *directory.parents):
        attributes = getattr(ancestor.lstat(), "st_file_attributes", 0)
        if ancestor.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise _ImageUnavailable("UNSAFE_EVIDENCE_DIR")
    directory = directory.resolve()
    ancestors = (directory, *directory.parents)
    repo = next((ancestor for ancestor in ancestors if (ancestor / ".git").exists()), None)
    forbidden = {".git", "packages", "deliverables", "fabric", "pbip", "reports", "semantic_models"}
    for ancestor in ancestors:
        name = ancestor.name.casefold()
        if name in forbidden or name.endswith((".report", ".semanticmodel")):
            raise _ImageUnavailable("UNSAFE_EVIDENCE_DIR")
        if any(
            (ancestor / marker).exists() for marker in ("definition.pbir", "definition.pbism", "package-manifest.json")
        ):
            raise _ImageUnavailable("UNSAFE_EVIDENCE_DIR")
        if any(ancestor.glob("*.pbip")):
            raise _ImageUnavailable("UNSAFE_EVIDENCE_DIR")
    if repo is not None:
        checked = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--quiet", "--", str(directory / basename)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if checked.returncode != 0:
            raise _ImageUnavailable("UNSAFE_EVIDENCE_DIR")
    return directory


def _utc_stamp(when: datetime | None = None) -> str:
    return (when or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _image_record(pid: int, state: Observation, window: DesktopWindow, capture_id: str) -> dict:
    frame = main_frame(state.windows)
    return {
        "schema": IMAGE_SCHEMA,
        "event": "owned_modal_image",
        "capture_id": capture_id,
        "timestamp_utc": _utc_stamp(),
        "captured_at_utc": None,
        "desktop_pid": str(pid),
        "main_hwnd": str(frame.hwnd) if frame is not None else None,
        "dialog_hwnd": str(window.hwnd),
        "owner_hwnd": str(window.owner_hwnd),
        "ownership_checks": dict.fromkeys(_IMAGE_CHECKS),
        "dimensions": None,
        "capture_success": False,
        "capture_method": "PrintWindow_PW_RENDERFULLCONTENT",
        "image_basename": None,
        "path": None,
        "sha256": None,
        "expires_at_utc": None,
        "cleanup_state": "not_created",
        "classification_provenance": None,
    }


def _image_notice(record: dict, status: str) -> None:
    """One versioned marker family, with no numeric auth-marker collisions in its wire spelling.

    Identifiers/dimensions/timestamps are strings. Escaping zero preserves their decoded value while
    ensuring a PID/hash containing 10054 or 403 cannot trip the parent's unanchored text classifier.
    This is not a semantic verdict or an arbitrary-text redactor: the record has closed safe fields.
    """
    record.update(status=status, timestamp_utc=_utc_stamp())
    print("LOCAL_IMAGE " + json.dumps(record, separators=(",", ":")).replace("0", r"\u0030"), flush=True)


@dataclass
class _ImageRequest:
    """One bounded launch/capture and its lease, registered BEFORE process creation."""

    path: Path
    lease: BinaryIO
    record: dict
    child: subprocess.Popen[bytes] | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False
    expiry: threading.Timer | None = None


class ModalVisualEvidence:
    """Invocation-private, one-shot asynchronous evidence. Never mutates a detection or its deadline."""

    def __init__(self, directory: Path | None = None, *, deadline: float = float("inf")) -> None:
        self._attempted: set[tuple[int, int]] = set()
        self._requests: list[_ImageRequest] = []
        self._lock = threading.Lock()
        self._closed = False
        self._deadline = deadline
        self._pending: queue.Queue[tuple[int, Observation, float] | None] = queue.Queue(maxsize=1)
        configured = directory if directory is not None else os.environ.get(IMAGE_DIRECTORY_ENV)
        self._directory = Path(configured) if configured else None
        atexit.register(self.close)
        observer = threading.Thread(target=self._consume, name="window-image-observer", daemon=True)
        try:
            observer.start()
        except RuntimeError:
            # Evidence is optional; failure to start its worker cannot change a refresh verdict.
            self._closed = True

    def __enter__(self) -> ModalVisualEvidence:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @staticmethod
    def _remove(request: _ImageRequest, reason: str) -> bool:
        try:
            request.lease.close()
            if request.path.exists():
                request.path.unlink(missing_ok=True)
            if request.path.exists():
                raise OSError
        except OSError:
            request.record["cleanup_state"] = "cleanup_failed"
            _image_notice(request.record, "CLEANUP_FAILED")
            return False
        request.record["cleanup_state"] = reason
        return True

    def enqueue(self, pid: int, state: Observation) -> None:
        """Nonblocking handoff of an immutable detection; no filesystem, subprocess or thread startup."""
        if state.modal is not None or state.dialog is None or state.dialog.verdict != VERDICT_DIALOG_UNREADABLE:
            return
        window = state.dialog.window
        if self._closed or not window.hwnd or not window.owner_hwnd or window.owner_enabled is not False:
            return
        deadline = min(self._deadline, time.monotonic() + IMAGE_CAPTURE_SECONDS)
        try:
            self._pending.put_nowait((pid, state, deadline))
        except queue.Full:
            # One pending exact snapshot is enough; later detector polls can offer a newer HWND.
            pass

    def _consume(self) -> None:
        while not self._closed:
            pending = self._pending.get()
            if pending is None or self._closed:
                return
            pid, state, deadline = pending
            if time.monotonic() < deadline:
                try:
                    self.observe(pid, state, deadline=deadline)
                except Exception:  # pylint: disable=broad-exception-caught
                    # Background tracebacks can expose OS paths/UI text just as child stderr can.
                    record = _image_record(pid, state, state.dialog.window, uuid.uuid4().hex.replace("0", "x"))
                    self._notice(record, "CAPTURE_FAILED")

    def observe(self, pid: int, state: Observation, *, deadline: float) -> None:
        """Background-only acquisition. Startup and capture share the queued attempt's deadline."""
        window = state.dialog.window
        with self._lock:
            key = (pid, window.hwnd)
            repeated = key in self._attempted
            previous_requests = tuple(self._requests)
            self._attempted.add(key)
        for previous in previous_requests:
            if previous.record["cleanup_state"] == "pending" and not previous.path.exists():
                if self._remove(previous, "removed_externally"):
                    self._notice(previous.record, "CLEANED")
        if self._closed or repeated:
            return
        capture_id = uuid.uuid4().hex.replace("0", "x")
        record = _image_record(pid, state, window, capture_id)
        if sys.platform != "win32":
            self._notice(record, "UNSUPPORTED")
            return
        if self._directory is None:
            self._notice(record, "EVIDENCE_DIR_REQUIRED")
            return
        request = self._prepare_image(record, deadline)
        if request is None:
            return
        try:
            if not self._closed and not request.cancelled and time.monotonic() < deadline:
                request.child = subprocess.Popen(  # pylint: disable=consider-using-with
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        str(pid),
                        str(window.hwnd),
                        str(window.owner_hwnd),
                        request.path.name,
                    ],
                    cwd=request.path.parent,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        except OSError:
            pass  # the watcher publishes a closed failure, never the native exception or its paths
        finally:
            request.ready.set()
        if self._closed or request.cancelled:
            self._stop(request)
            self._remove(request, "removed")

    def _prepare_image(self, record: dict, deadline: float) -> _ImageRequest | None:
        request = None
        try:
            basename = f"_ui-image-{record['capture_id']}.png"
            path = _evidence_directory(self._directory, basename) / basename
            if self._closed or time.monotonic() >= deadline:
                self._notice(record, "CAPTURE_TIMEOUT")
                return None
            lease = _open_private_image(path, delete_on_close=True)
            record.update(image_basename=path.name, path=path.name, cleanup_state="pending")
            request = _ImageRequest(path, lease, record)
            with self._lock:
                if not self._closed:
                    self._requests.append(request)
            if self._closed or time.monotonic() >= deadline:
                self._remove(request, "removed")
                self._notice(record, "CAPTURE_TIMEOUT")
                return None
            # The watcher bounds even Popen startup. A late handle is stopped below; its child can
            # only open this reserved name, which timeout/close has already removed.
            threading.Thread(target=self._watch, args=(request, deadline), name="window-image", daemon=True).start()
            return request
        except (OSError, subprocess.TimeoutExpired, _ImageUnavailable, RuntimeError):
            if request is not None:
                self._stop(request)
                self._remove(request, "removed")
            self._notice(record, "EVIDENCE_STORAGE_UNAVAILABLE")
            return None

    def _notice(self, record: dict, status: str) -> None:
        with self._lock:
            if not self._closed:
                _image_notice(record, status)

    def _capture_result(self, request: _ImageRequest, deadline: float) -> str | None:
        # Exact bools are intentional: integer 1 must not certify an ownership check.
        # pylint: disable=unidiomatic-typecheck
        if not request.ready.wait(max(0.0, deadline - time.monotonic())):
            raise subprocess.TimeoutExpired("window-image", IMAGE_CAPTURE_SECONDS)
        if self._closed or request.cancelled:
            return None
        if request.child is None:
            raise OSError
        output, _ = request.child.communicate(timeout=max(0.0, deadline - time.monotonic()))
        payload = json.loads(output)
        code = payload.get("status")
        if code not in _IMAGE_RESULTS or (code == "ACQUIRED" and request.child.returncode != 0):
            code = "CAPTURE_FAILED"
        checks = payload.get("ownership_checks")
        if (
            isinstance(checks, dict)
            and set(checks) == set(_IMAGE_CHECKS)
            and all(value is None or type(value) is bool for value in checks.values())
        ):
            request.record["ownership_checks"] = checks
        if code == "ACQUIRED" and not self._accept_image(request, payload):
            code = "CAPTURE_FAILED"
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired("window-image", IMAGE_CAPTURE_SECONDS)
        return code

    def _watch(self, request: _ImageRequest, deadline: float) -> None:
        try:
            code = self._capture_result(request, deadline)
            if code is None:
                return
        except subprocess.TimeoutExpired:
            code = "CAPTURE_TIMEOUT"
            self._stop(request)
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
            code = "CAPTURE_FAILED"
            self._stop(request)
        with self._lock:
            if self._closed:
                return
            try:
                # Even a failed write can leave a partial file whose first deletion is refused.
                request.expiry = threading.Timer(IMAGE_LIFETIME_SECONDS, self._expire, args=(request,))
                request.expiry.daemon = True
                request.expiry.start()
                if code == "ACQUIRED" and request.path.is_file():
                    acquired = datetime.now(timezone.utc)
                    request.record.update(
                        captured_at_utc=_utc_stamp(acquired),
                        expires_at_utc=_utc_stamp(acquired + timedelta(seconds=IMAGE_LIFETIME_SECONDS)),
                        capture_success=True,
                    )
                    _image_notice(request.record, code)
                    return
            except (OSError, RuntimeError):
                code = "CAPTURE_FAILED"
            if self._remove(request, "removed") and request.expiry is not None:
                request.expiry.cancel()
            _image_notice(request.record, code if code != "ACQUIRED" else "WRITE_FAILED")

    @staticmethod
    def _accept_image(request: _ImageRequest, payload: dict) -> bool:
        """Validate the child metadata against the observer's pinned, shared-delete file handle."""
        # bool is an int subclass, but is not a pixel dimension.
        # pylint: disable=unidiomatic-typecheck
        dimensions = payload.get("dimensions")
        if (
            not isinstance(dimensions, dict)
            or set(dimensions) != {"width", "height"}
            or not all(type(value) is int and value > 0 for value in dimensions.values())
        ):
            return False
        if not all(request.record["ownership_checks"].get(stage) is True for stage in _IMAGE_CHECKS):
            return False
        request.lease.seek(0)
        image = request.lease.read(_MAX_IMAGE_BYTES + 1)
        if not 33 <= len(image) <= _MAX_IMAGE_BYTES or image[:8] != b"\x89PNG\r\n\x1a\n":
            return False
        if struct.unpack(">II", image[16:24]) != (dimensions["width"], dimensions["height"]):
            return False
        digest = hashlib.sha256(image).hexdigest()
        if payload.get("sha256") != digest:
            return False
        request.record.update(dimensions={key: str(value) for key, value in dimensions.items()}, sha256=digest)
        return True

    def _expire(self, request: _ImageRequest) -> None:
        with self._lock:
            if self._remove(request, "expired"):
                _image_notice(request.record, "CLEANED")

    @classmethod
    def _stop(cls, request: _ImageRequest) -> None:
        request.cancelled = True
        request.ready.set()
        try:
            if request.child is not None and request.child.poll() is None:
                request.child.kill()
                request.child.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            _image_notice(request.record, "CAPTURE_CLEANUP_FAILED")

    def close(self) -> None:
        """Cancel without joining the observer or waiting for its filesystem/process startup."""
        with self._lock:
            self._closed = True
            requests = tuple(self._requests)
        try:
            self._pending.put_nowait(None)
        except queue.Full:
            pass
        for request in requests:
            self._stop(request)
            previous = request.record["cleanup_state"]
            removed = self._remove(request, "removed_on_exit")
            if removed and request.expiry is not None:
                request.expiry.cancel()
            if removed and previous in ("pending", "cleanup_failed") and request.record["capture_success"]:
                _image_notice(request.record, "CLEANED")
        atexit.unregister(self.close)


if __name__ == "__main__":
    raise SystemExit(_image_child(sys.argv[1:]))
