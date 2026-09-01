"""
purpose: decide a Desktop dialog verdict from COLLECTED windows - the one decision implementation
usage:   python decide_dialog.py --windows <windows.json> [--in-flight]

Issue #417. `probe_desktop_credential.ps1` used to re-implement candidate selection and classification
in PowerShell, and two independent divergences survived a review round aimed specifically at removing
divergence:

  * a title veto applied to the PowerShell half only - measured, an owned dialog titled `Password:`
    with benign body content gave `DIALOG_UNRECOGNIZED` (exit 3) in PowerShell and
    `CREDENTIAL_PRESENT` (exit 0) in Python, which is the gate of record silently clearing a sign-in
    modal;
  * case-sensitivity differing at the LANGUAGE level - PowerShell's `-ne` and `-notcontains` are
    case-insensitive, Python's `==` and `in` are not - so title `Refresh` with body `REFRESH` gave
    `DIALOG_UNREADABLE` (exit 3) against `CREDENTIAL_PRESENT` (exit 0).

The second is why this file exists rather than a third round of alignment: a language-level difference
cannot be fixed once. Every future string comparison in either implementation would be a fresh chance
to reintroduce it. So PowerShell COLLECTS - Win32 attributes plus UI Automation text, which this module
cannot read - and everything that is a JUDGEMENT happens here, once.

⚠️ **Fails CLOSED.** Any malformed input, unknown field or internal error exits 3 with a verdict of
`DIALOG_UNREADABLE`, never 0. "We could not decide" must never be reportable as "no modal appeared".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position
from _credential_modal import (  # noqa: E402
    DesktopWindow,
    VERDICT_CREDENTIAL_MISSING,
    VERDICT_DIALOG_UNREADABLE,
    dialog_candidates,
    dialog_guidance,
    dialog_verdict,
    main_frame,
    match_credential_modal,
)

EXIT_CREDENTIAL_MISSING = 1
EXIT_INDETERMINATE = 3
# The one guidance line the decider emits for itself. Everything else comes from
# `_credential_modal.DIALOG_KIND_GUIDANCE`, so the collector never has to hold a second copy.
UNREADABLE_GUIDANCE = "the dialog state could not be established at all - look at the Desktop screen"


class WindowSchemaError(ValueError):
    """A collected record that cannot be trusted enough to classify.

    Raised BEFORE a :class:`DesktopWindow` exists, so it lands in :func:`main`'s handler and becomes
    ``DIALOG_UNREADABLE`` at exit 3 - the module's documented fail-closed promise, which until the
    2026-09-01 blind review was a claim the code did not implement.
    """


# The window contract, field by field. `probe_desktop_credential.ps1`'s `ConvertTo-ProbeWindow` is the
# ONLY producer, and `test_the_collector_emits_exactly_the_fields_the_decider_requires` compares its
# emitted property set against this dict in both directions.
#
# ⚠️ Types are matched EXACTLY, not with `isinstance`, and that is load-bearing: `bool` is a subclass
# of `int` in Python, so an `isinstance(value, int)` check accepts `true` for `Width` - which is
# precisely the class of silent coercion this validator exists to stop.
WINDOW_FIELDS: dict[str, tuple[type, ...]] = {
    "Title": (str,),
    "ClassName": (str,),
    "Width": (int,),
    "Height": (int,),
    "Hwnd": (int,),
    "OwnerHwnd": (int,),
    "Minimized": (bool,),
    "OwnerEnabled": (bool, type(None)),
    "Texts": (list,),
    "InteractiveTexts": (list,),
    "HarvestReason": (str,),
}

# ⚠️ **`HarvestComplete` is deliberately outside the schema, and this is the one exception.** It is not
# structure; it is the collector's SELF-REPORT about the quality of its own read, and it already has a
# stricter rule than any type check could express - only a real Boolean `True` counts. A malformed
# self-report therefore has a better answer than "unreadable": `benign-unverified` is the same exit-3
# band AND still tells the operator that the content looked like progress. Rejecting it would lose
# that. Gated by `test_only_a_real_boolean_true_can_authorise_suppression` (nine shapes, including an
# absent field) and by `test_a_malformed_self_report_is_unverified_not_unreadable`.
TOLERATED_FIELDS = frozenset({"HarvestComplete"})


def _has_exact_type(value: object, allowed: tuple[type, ...]) -> bool:
    """Exact-type membership. `isinstance` is WRONG here - see :data:`WINDOW_FIELDS`."""
    return any(type(value) is expected for expected in allowed)  # pylint: disable=unidiomatic-typecheck


def validate_window(raw: object) -> dict:
    """Reject anything that is not exactly one well-formed collected window, or return it unchanged.

    Measured on the pre-fix build, all three through ``Invoke-DialogDecision`` and all three **exit 0**:

    * a record whose only field was ``TotallyUnknown`` was accepted as an empty window, and - because
      absent geometry and ownership became ``0`` - :func:`_credential_modal.renders_nothing` then
      declared it *proven non-rendering* and dropped it before classification;
    * ``Texts`` supplied as a JSON **object** had its KEYS read as window text, and those keys
      authorised in-flight suppression;
    * a window missing every geometry and ownership field still reached the credential prepass.

    An unknown field is rejected rather than ignored on purpose. There is exactly one producer, so an
    unrecognised field means the collector and the decider are not the pair we think they are, and
    "decide anyway on the fields I happen to recognise" is the shape of every defect on this branch.
    """
    if not isinstance(raw, dict):
        raise WindowSchemaError(f"window record is {type(raw).__name__}, expected an object")
    unknown = sorted(set(raw) - set(WINDOW_FIELDS) - TOLERATED_FIELDS)
    if unknown:
        raise WindowSchemaError(f"unknown field(s) {unknown} - collector and decider have drifted")
    missing = sorted(set(WINDOW_FIELDS) - set(raw))
    if missing:
        raise WindowSchemaError(f"missing required field(s) {missing}")
    for name, allowed in WINDOW_FIELDS.items():
        value = raw[name]
        if not _has_exact_type(value, allowed):
            expected = "|".join(kind.__name__ for kind in allowed)
            raise WindowSchemaError(f"{name} is {type(value).__name__}, expected {expected}")
    for name in ("Texts", "InteractiveTexts"):
        # Exact type again, for the same reason as :data:`WINDOW_FIELDS` - `isinstance` would accept a
        # `str` subclass, and more importantly would accept `True` for an `int`-typed sibling check.
        offenders = sorted({type(item).__name__ for item in raw[name] if not _has_exact_type(item, (str,))})
        if offenders:
            raise WindowSchemaError(f"{name} holds non-string element(s) of type {offenders}")
    return raw


def _window_from(raw: dict) -> DesktopWindow:
    """Build a :class:`DesktopWindow` from one VALIDATED collected record.

    ⚠️ **``HarvestComplete`` is the field that authorises a dismissal, so ONLY a real Boolean ``True``
    survives here.** JSON round-trips widen types, a caller can predate the field, and a future
    collector can rename it - and PowerShell's own ``-eq $true`` is COERCIVE, which review proved by
    clearing a window with integer ``1`` and with the string ``"true"``. Absent, ``null``, ``0``,
    ``1``, ``"true"``, ``"false"``, ``""`` and ``[]`` are every one of them an UNKNOWN window shape,
    and an unknown shape collapses to ``False`` - "the read did not report itself finished" - which
    :func:`_credential_modal.classify_dialog` turns into ``benign-unverified``, exit 3.

    Note the asymmetry with the dataclass default, which is ``None``: in-process Win32 enumeration
    either reads a window fully or raises, so there the question does not apply. It applies to
    everything that arrives over this boundary, which is why the coercion lives HERE and not in the
    classifier - a classifier that treated ``None`` as incomplete would make every Python-native
    progress dialog unverifiable.

    Every OTHER field is now schema-checked by :func:`validate_window` before this runs, so the
    defensive ``or 0`` / ``str(...)`` coercions that used to stand in for validation are gone: a
    missing width is a rejected record, not a zero one.
    """
    validate_window(raw)
    return DesktopWindow(
        title=raw["Title"],
        class_name=raw["ClassName"],
        width=raw["Width"],
        height=raw["Height"],
        texts=tuple(text for text in raw["Texts"] if text),
        minimized=raw["Minimized"],
        hwnd=raw["Hwnd"],
        owner_hwnd=raw["OwnerHwnd"],
        owner_enabled=raw["OwnerEnabled"],
        interactive_texts=tuple(text for text in raw["InteractiveTexts"] if text),
        harvest_complete=raw.get("HarvestComplete") is True,
        harvest_reason=raw["HarvestReason"],
    )


def decide(windows: list[DesktopWindow], *, in_flight: bool) -> dict:
    """The whole decision: credential prepass, then the folded dialog verdict.

    Returns a JSON-able record. ``exit_code`` is 1 only for a credential wall, 3 for every
    indeterminate state, and 0 ONLY when nothing needed reporting.
    """
    frame = main_frame(windows)
    hit = match_credential_modal(windows, frame=frame)
    if hit is not None:
        return {
            "verdict": VERDICT_CREDENTIAL_MISSING,
            "kind": "credential",
            "exit_code": EXIT_CREDENTIAL_MISSING,
            "evidence": hit.matched_text,
            "guidance": None,
            "candidates": len(dialog_candidates(windows, frame=frame)),
            "credential": hit.matched_text,
            "window": None,
            "candidate_hwnds": [],
        }
    finding = dialog_verdict(windows, operation_in_flight=in_flight, frame=frame)
    return {
        "verdict": None if finding is None else finding.verdict,
        "kind": None if finding is None else finding.kind,
        "exit_code": 0 if finding is None else EXIT_INDETERMINATE,
        "evidence": None if finding is None else finding.evidence,
        # The operator's next step travels WITH the verdict. The collector used to hold a `switch` of
        # its own over the same kinds, which is the divergence surface #417 exists to close.
        "guidance": None if finding is None else dialog_guidance(finding),
        "candidates": len(dialog_candidates(windows, frame=frame)),
        "credential": None,
        "window": None
        if finding is None
        else {
            "Title": finding.window.title,
            "ClassName": finding.window.class_name,
            "Width": finding.window.width,
            "Height": finding.window.height,
            "HarvestComplete": finding.window.harvest_complete,
            "HarvestReason": finding.window.harvest_reason,
            "Texts": len(finding.window.texts),
        },
        "candidate_hwnds": [],
    }


def main(argv: list[str] | None = None) -> int:
    """Read collected windows, print one JSON verdict line, and return its exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", required=True, help="path to the collector's JSON window array")
    parser.add_argument("--in-flight", action="store_true", help="this process already started the refresh")
    parser.add_argument(
        "--candidates-only",
        action="store_true",
        help="return only which windows are worth a UIA harvest - the collector must not decide that itself",
    )
    args = parser.parse_args(argv)
    try:
        # `utf-8-sig`, not `utf-8`: Windows PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM,
        # and `json.loads` rejects it. Measured - every decision degraded to DIALOG_UNREADABLE, which is
        # the fail-closed direction working, but it is still a total loss of function. The decider is
        # the right place to absorb its collector's encoding quirks.
        raw = json.loads(Path(args.windows).read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            # PowerShell 5.1 has no `ConvertTo-Json -AsArray`, so a single-element array serialises as
            # a bare object. That is a SHAPE quirk of the transport, not a malformed record, so it is
            # normalised here and then validated exactly like any other.
            raw = [raw]
        if not isinstance(raw, list):
            raise WindowSchemaError(f"payload is {type(raw).__name__}, expected an array of windows")
        # ⚠️ Validation runs BEFORE the `--candidates-only` branch on purpose. That branch decides which
        # windows get a UIA harvest, so a record accepted here but malformed is a window that never gets
        # enriched - a silent loss of the only text that can convict. Both branches fail closed.
        windows = [_window_from(item) for item in raw]
        if args.candidates_only:
            frame = main_frame(windows)
            picked = dialog_candidates(windows, frame=frame)
            result = {
                "verdict": None,
                "kind": None,
                "exit_code": 0,
                "evidence": None,
                "guidance": None,
                "candidates": len(picked),
                "credential": None,
                "window": None,
                "candidate_hwnds": [w.hwnd for w in picked],
            }
        else:
            result = decide(windows, in_flight=args.in_flight)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        result = {
            "verdict": VERDICT_DIALOG_UNREADABLE,
            "kind": "unreadable",
            "exit_code": EXIT_INDETERMINATE,
            "evidence": f"decision failed: {type(exc).__name__}: {exc}",
            "guidance": UNREADABLE_GUIDANCE,
            "candidates": 0,
            "credential": None,
            "window": None,
            "candidate_hwnds": [],
        }
    print("DECISION:" + json.dumps(result))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
