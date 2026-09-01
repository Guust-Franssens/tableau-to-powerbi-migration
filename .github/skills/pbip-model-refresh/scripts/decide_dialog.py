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
    dialog_verdict,
    main_frame,
    match_credential_modal,
)

EXIT_CREDENTIAL_MISSING = 1
EXIT_INDETERMINATE = 3


def _window_from(raw: dict) -> DesktopWindow:
    """Build a :class:`DesktopWindow` from one collected record.

    Every field is read defensively: the collector is a different language and a different process, so
    a missing or mistyped field is a real possibility and must degrade toward "we know less", never
    toward "this window is fine".
    """

    def _texts(key: str) -> tuple[str, ...]:
        value = raw.get(key) or []
        if isinstance(value, str):
            value = [value]
        return tuple(str(item) for item in value if item)

    owner_enabled = raw.get("OwnerEnabled")
    if not isinstance(owner_enabled, bool):
        owner_enabled = None
    harvest_complete = raw.get("HarvestComplete")
    if not isinstance(harvest_complete, bool):
        harvest_complete = None
    return DesktopWindow(
        title=str(raw.get("Title") or ""),
        class_name=str(raw.get("ClassName") or ""),
        width=int(raw.get("Width") or 0),
        height=int(raw.get("Height") or 0),
        texts=_texts("Texts"),
        minimized=bool(raw.get("Minimized")),
        hwnd=int(raw.get("Hwnd") or 0),
        owner_hwnd=int(raw.get("OwnerHwnd") or 0),
        owner_enabled=owner_enabled,
        interactive_texts=_texts("InteractiveTexts"),
        harvest_complete=harvest_complete,
        harvest_reason=str(raw.get("HarvestReason") or ""),
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
            "candidates": len(dialog_candidates(windows, frame=frame)),
            "credential": hit.matched_text,
        }
    finding = dialog_verdict(windows, operation_in_flight=in_flight, frame=frame)
    return {
        "verdict": None if finding is None else finding.verdict,
        "kind": None if finding is None else finding.kind,
        "exit_code": 0 if finding is None else EXIT_INDETERMINATE,
        "evidence": None if finding is None else finding.evidence,
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
            raw = [raw]
        windows = [_window_from(item) for item in raw]
        if args.candidates_only:
            frame = main_frame(windows)
            picked = dialog_candidates(windows, frame=frame)
            result = {
                "verdict": None,
                "kind": None,
                "exit_code": 0,
                "evidence": None,
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
            "candidates": 0,
            "credential": None,
            "window": None,
            "candidate_hwnds": [],
        }
    print("DECISION:" + json.dumps(result))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
