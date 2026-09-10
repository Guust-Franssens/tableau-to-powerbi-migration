"""Regression tests for probe_live_source's machine-readable verdict parsing.

Two seams are pinned here:
  * #152 - the parent (``probe_live_source``) must accept the verdict family the child
    (``refresh_pbip_model`` via ``_verdict``) actually emits for the argv the parent actually builds,
    honouring the child's exit code. The old suite hand-wrote ``REFRESH: DATA_OK`` - output the
    production caller CANNOT produce - so it stayed green while the gate was un-liftable.
  * #153 - the child's reassuring no-dialog banner must not classify as ``NO_CREDENTIAL``.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import subprocess
import sys
from pathlib import Path

import pytest

# pylint: disable=import-outside-toplevel,protected-access,no-member

REPO = Path(__file__).resolve().parent.parent


def _import_probe_live_source():
    """Import the script module from the repo-local scripts folder."""
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import probe_live_source  # noqa: PLC0415

    return probe_live_source


def _import_skill_modules():
    """Import the CHILD-side modules the probe shells out to, from the SAME dir the probe resolves.

    Derived from ``probe_live_source.SKILL_SCRIPTS`` so the seam test binds to the real child, not a
    hand-picked path. Returns (refresh_pbip_model, _verdict, _credential_modal).
    """
    probe_live_source = _import_probe_live_source()
    skill_scripts = str(probe_live_source.SKILL_SCRIPTS)
    if skill_scripts not in sys.path:
        sys.path.insert(0, skill_scripts)
    import _credential_modal  # noqa: PLC0415
    import _verdict  # noqa: PLC0415
    import refresh_pbip_model  # noqa: PLC0415

    return refresh_pbip_model, _verdict, _credential_modal


def test_requires_data_ok_verdict_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray DATA_OK substring after CREDENTIAL_MISSING must not clear the live-source gate."""
    probe_live_source = _import_probe_live_source()
    stdout = (
        "PREFLIGHT: CREDENTIAL_MISSING pid=111; window title='(empty title)'\n"
        "PREFLIGHT: DATA_OK_FROM_WORKER_AFTER_RELEASE\n"
    )
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(args=["refresh"], returncode=1, stdout=stdout, stderr=""),
    )

    assert probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False) == (  # noqa: SLF001
        1,
        "NO_CREDENTIAL",
    )


def test_probe_argv_fed_through_real_emitter_is_accepted_by_the_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEAM (#152): the argv the probe ACTUALLY builds, parsed by refresh_pbip_model's REAL parser and
    run through _verdict's REAL emitter on a successful single-row refresh, must produce a verdict line
    _has_data_ok_verdict accepts.

    This is the test CI was missing. The old fixture hand-wrote ``REFRESH: DATA_OK``, which the
    production caller CANNOT elicit - it always passes ``--tables`` and never ``--canaries``, so the
    child emits ``TABLES_OK``. Deriving the fixture from BOTH real sides is the only way to catch a
    vocabulary desync across the parent/child seam.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, verdict_mod, _ = _import_skill_modules()

    # 1. Capture the EXACT argv the production caller builds, without running a real refresh.
    captured: dict[str, list[str]] = {}

    def _capture(cmd, *_a, **_k):  # noqa: ANN001, ANN002, ANN003
        captured["argv"] = list(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(probe_live_source.subprocess, "run", _capture)
    probe_live_source._refresh_and_classify(123, "shipment", 1, network_fault_observed=False)
    argv = captured["argv"]

    # The exact desync #152 named: the probe always narrows with --tables and never asks for --canaries.
    assert "--tables" in argv and "--canaries" not in argv
    # Issue #146: the probe must disable progress tracing so the child uses the legacy XMLA timeout
    # path (300s + 30s = 330s), not the 3600s progress absolute backstop that exceeds the parent's
    # 390s subprocess kill budget. Without --no-progress the child never reaches its own deadline.
    assert "--no-progress" in argv

    # 2. Parse the child flags (everything after `python refresh_pbip_model.py`) with the REAL parser.
    child_flags = argv[2:]
    args = refresh_pbip_model._build_arg_parser().parse_args(child_flags)
    assert args.tables == ["shipment"]
    (table,) = args.tables

    # 3. Run the REAL emitter on a successful single-row refresh; capture its verdict line verbatim.
    implicit = not verdict_mod._canary_tables(args)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = verdict_mod._emit_data_verdict(None, 0.0, args, [(table, 1)], implicit)
    emitted = buffer.getvalue()

    assert exit_code == 0, f"the child returns success on a good refresh; got exit {exit_code}"
    assert "TABLES_OK" in emitted, f"sanity: the production argv elicits TABLES_OK, not DATA_OK: {emitted!r}"
    # 4. The parent MUST accept what the child actually emits, for the table it actually probed.
    assert probe_live_source._has_data_ok_verdict(emitted, table), (
        f"verdict-vocabulary desync across the seam: child emitted {emitted!r}, parent rejected it"
    )


def test_probe_clears_gate_on_verbatim_live_databricks_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #152 reproduction, VERBATIM from the 2026-08-14 live Databricks run: child exit 0 and a
    ``TABLES_OK`` for the probed table. This exact input returned NO_CREDENTIAL at exit 1 before the fix.
    """
    probe_live_source = _import_probe_live_source()
    stdout = "  refresh: refreshed shipment\n  data   : 1 row(s) in 'shipment'\nREFRESH: TABLES_OK 'shipment'\n"
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(args=["refresh"], returncode=0, stdout=stdout, stderr=""),
    )

    assert probe_live_source._refresh_and_classify(123, "shipment", 1, network_fault_observed=False) == (  # noqa: SLF001
        0,
        "DATA_OK",
    )


def test_has_data_ok_verdict_accepts_model_level_data_ok() -> None:
    """A whole-model ``DATA_OK`` stays a valid clear for any probed table (it certifies every source)."""
    probe_live_source = _import_probe_live_source()
    assert probe_live_source._has_data_ok_verdict("REFRESH: DATA_OK\n", "anything")
    assert probe_live_source._has_data_ok_verdict("REFRESH: DATA_OK + PERSISTED\n", "anything")


def test_scoped_table_verdicts_clear_only_for_the_probed_table() -> None:
    """#152 + #115: TABLE_OK/TABLES_OK clear the gate for the table the probe asked to refresh, and ONLY
    that one. A verdict naming some OTHER table is a false certificate and must not count."""
    probe_live_source = _import_probe_live_source()
    # Positive - the probed table is the one certified (single, multi-table membership, and TABLE_OK).
    assert probe_live_source._has_data_ok_verdict("REFRESH: TABLES_OK 'shipment'\n", "shipment")
    assert probe_live_source._has_data_ok_verdict("REFRESH: TABLES_OK 'a', 'shipment' + PERSISTED\n", "shipment")
    assert probe_live_source._has_data_ok_verdict("REFRESH: TABLE_OK 'shipment'\n", "shipment")
    # Negative control - a TABLES_OK naming a DIFFERENT table must NOT clear the gate for 'shipment'.
    assert not probe_live_source._has_data_ok_verdict("REFRESH: TABLES_OK 'some_other_table'\n", "shipment")
    # The anchoring lesson still holds: a stray verdict-looking substring in prose is not a verdict line.
    assert not probe_live_source._has_data_ok_verdict("note: TABLES_OK 'shipment' happened earlier\n", "shipment")


def test_nonzero_exit_is_never_read_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """#152: honour the child's exit code - a non-zero exit must not clear the gate even when the text
    carries a success-looking verdict line for the probed table."""
    probe_live_source = _import_probe_live_source()
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=["refresh"], returncode=1, stdout="REFRESH: TABLES_OK 'shipment'\n", stderr=""
        ),
    )

    exit_code, verdict = probe_live_source._refresh_and_classify(123, "shipment", 1, network_fault_observed=False)
    assert exit_code == 1 and verdict != "DATA_OK"


def test_wrong_table_tables_ok_does_not_clear_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """#115 end-to-end: even at exit 0, a TABLES_OK for a DIFFERENT table must leave the gate closed."""
    probe_live_source = _import_probe_live_source()
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=["refresh"], returncode=0, stdout="REFRESH: TABLES_OK 'some_other_table'\n", stderr=""
        ),
    )

    exit_code, verdict = probe_live_source._refresh_and_classify(123, "shipment", 1, network_fault_observed=False)
    assert exit_code == 1 and verdict != "DATA_OK"


def _refresh_banner_funcs(credential_modal) -> dict:
    """Discover every pre-wait refresh banner by naming convention (``print_refresh_*banner``).

    Introspection, NOT a hand-list: a newly-added banner is covered the instant it lands, with no edit
    here. ``print_refresh_heartbeat`` is intentionally excluded (it is not a ``*banner``).
    """
    return {
        name: obj
        for name, obj in vars(credential_modal).items()
        if callable(obj) and name.startswith("print_refresh_") and name.endswith("banner")
    }


def _detector_unknown_reasons(credential_modal) -> list[str]:
    """The REAL ``unknown_reason`` strings the detector emits, harvested by driving each UNKNOWN branch.

    Derived from the detector, never hand-written - that is the #152/#153 lesson: a fixture the
    production code cannot actually produce hides the very defect the test exists to catch. The old
    #153 banner test hand-wrote ``reason='owner window is minimized'``; the string the detector really
    emits contained the word 'credential' and self-classified as ``NO_CREDENTIAL``.
    """

    def _enumeration_raises(_pid: int):
        raise credential_modal.Win32EnumerationError("boom")

    minimized_main = credential_modal.DesktopWindow(
        title="Report",
        class_name=credential_modal.DESKTOP_MAIN_CLASS_PREFIX + ".app.0",
        width=1200,
        height=800,
        minimized=True,
    )
    # (enumerate_windows, process_is_alive) per UNKNOWN branch the detector can emit: enumeration
    # failed and owner minimized. Zero windows while alive is a distinct local DESKTOP_UNREADY state.
    scenarios = (
        (_enumeration_raises, lambda _pid: True),
        (lambda _pid: [minimized_main], lambda _pid: True),
    )

    reasons: list[str] = []
    for enumerate_windows, process_is_alive in scenarios:
        reason = credential_modal.inspect_credential_modal(
            111, enumerate_windows=enumerate_windows, process_is_alive=process_is_alive
        ).unknown_reason
        assert reason, "scenario failed to drive the detector into an UNKNOWN state"
        reasons.append(reason)

    # Backstop for 'catches a newly-added reason without editing a list': if inspect_credential_modal
    # grows a THIRD unknown_reason branch, this trips so whoever adds it also adds a driver scenario.
    branches = inspect.getsource(credential_modal.inspect_credential_modal).count("unknown_reason=")
    assert branches == len(reasons), (
        f"detector emits {branches} unknown_reason branch(es) but this harness exercised {len(reasons)}; "
        "add a driver scenario in _detector_unknown_reasons so #153 stays covered."
    )
    return reasons


def test_no_refresh_banner_and_detector_reason_classifies_as_no_credential() -> None:
    """#153 (structural): NO refresh banner may fabricate a credential stop from its own prose - for ANY
    ``unknown_reason`` the detector can actually emit.

    Both inputs are DISCOVERED, not enumerated: the banner set from the module by naming convention, and
    the reason set from the real detector. That is what makes this catch (a) a newly-added banner and
    (b) a newly-added reason without anyone editing a parametrize list - the exact blind-spot class that
    let hand-written fixtures stay green while the seam was broken. The concrete bug this pins: the
    minimized-owner reason literally read '... owned credential dialogs are hidden'; while that word was
    ``credential`` (a ``CREDENTIAL_MARKER``), a slow/timeout refresh through the UNKNOWN path was
    mislabelled ``NO_CREDENTIAL`` and sent an operator to re-enter credentials for a merely-slow
    warehouse. ``print_refresh_unknown_banner`` interpolates that reason, so the banner's own static
    prose being clean was NOT enough - the fix is at the detector, and this test reads it from there.
    """
    probe_live_source = _import_probe_live_source()
    _, _, credential_modal = _import_skill_modules()

    banners = _refresh_banner_funcs(credential_modal)
    reasons = _detector_unknown_reasons(credential_modal)
    assert len(banners) >= 2, f"expected to discover both refresh banners; got {sorted(banners)}"
    assert len(reasons) >= 2, f"expected >=2 real unknown_reason strings; got {reasons}"

    base = (63824, 300, 30)
    failures: list[tuple[str, tuple, str]] = []
    for name, banner in sorted(banners.items()):
        takes_reason = "reason" in inspect.signature(banner).parameters
        arg_sets = [(*base, reason) for reason in reasons] if takes_reason else [base]
        for args in arg_sets:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                banner(*args)
            text = buffer.getvalue()
            verdict, _ = probe_live_source._classify_failure(text, network_fault_observed=False)
            if verdict == "NO_CREDENTIAL":
                failures.append((name, args[3:], text))
    assert not failures, "banner(s) fabricated a credential stop from their own prose: " + "; ".join(
        f"{name}{extra}: {text!r}" for name, extra, text in failures
    )


def test_free_text_credential_marker_without_a_verdict_line_still_stops() -> None:
    """A revoked Databricks PAT returns a 403/socket-reset with NO modal and NO verdict line, so the
    deliberately-unanchored CREDENTIAL_MARKERS path is the only thing that catches it. Pin that #153's
    structural change did NOT remove that free-text path."""
    probe_live_source = _import_probe_live_source()
    verdict, _ = probe_live_source._classify_failure(
        "DataSource.Error: the connection was forcibly closed by the remote host (10054)",
        network_fault_observed=False,
    )
    assert verdict == "NO_CREDENTIAL"


def test_a_dialog_finding_keeps_the_gate_shut_without_claiming_a_credential_wall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#376: the child's dialog verdicts must keep the gate shut, but must NOT read as a sign-in wall.

    Master emitted ``BLOCKED_BY_DIALOG`` at exit 1 from a SIZE-ONLY test, and this matcher maps that
    token to ``NO_CREDENTIAL`` - whose directive is "you may NOT build; a human must sign in;
    TERMINATE THE RUN NOW". A Power BI Refresh progress dialog trips that, so a working refresh could
    halt a migration and send someone to a screen showing nothing of the sort.

    The child line is HARVESTED from the real emitter with a real classification, never hand-written:
    that is the #152/#153 discipline, and it is the only way this test can notice if the emitted
    wording drifts back into the classifier's free-text credential markers.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()

    for texts in ((), ("Save changes?", "Discard"), ("Refresh",), ("Refresh", "Evaluating...")):
        finding = credential_modal.classify_dialog(
            credential_modal.DesktopWindow("Refresh" if "Refresh" in texts else "", "Cls", 702, 355, texts)
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            refresh_pbip_model._emit_dialog_finding(111, finding)
        stdout = buffer.getvalue()
        monkeypatch.setattr(
            probe_live_source.subprocess,
            "run",
            lambda *_args, _out=stdout, **_kwargs: subprocess.CompletedProcess(
                args=["refresh"], returncode=3, stdout=_out, stderr=""
            ),
        )

        rc, verdict = probe_live_source._refresh_and_classify(  # noqa: SLF001
            123, "Orders", 1, network_fault_observed=False
        )

        assert rc == 1, f"{finding.verdict} must keep the gate shut"
        assert verdict != "DATA_OK"
        assert verdict != "NO_CREDENTIAL", (
            f"{finding.verdict} asserted a credential wall we never observed; emitted line: {stdout!r}"
        )
        assert not probe_live_source._has_credential_stop_verdict(stdout)


def test_every_dialog_guidance_string_is_marker_free() -> None:
    """#376 x #153: the guidance printed under a dialog verdict must not fabricate a credential stop.

    The kinds are DISCOVERED from the detector's own table rather than listed here, so a kind added
    later is covered without anyone editing this test - the same "catches a newly-added X" discipline
    as ``_detector_unknown_reasons``. This matters because ``_classify_failure`` scans a failing
    child's WHOLE transcript as free text: one stray "credential"/"sign in"/"authentication" in the
    reassuring half of the message would relabel "we could not probe" as the hard stop it exists to
    avoid, exactly as the #153 banner did.
    """
    probe_live_source = _import_probe_live_source()
    _, _, credential_modal = _import_skill_modules()

    guidance = credential_modal.DIALOG_KIND_GUIDANCE
    assert len(guidance) >= 5, f"expected the detector's full guidance table; got {sorted(guidance)}"

    offenders = {
        kind: [marker for marker in probe_live_source.CREDENTIAL_MARKERS if marker in text.lower()]
        for kind, text in guidance.items()
        if any(marker in text.lower() for marker in probe_live_source.CREDENTIAL_MARKERS)
    }

    assert not offenders, f"guidance prose carries free-text credential markers: {offenders}"


def test_a_dialog_verdict_is_recognised_structurally_and_is_not_a_credential_stop() -> None:
    """#400 review, finding 2 (HIGH): the parent replaced the child's token with a free-text guess.

    ``probe_live_source`` did not recognise the dialog tokens, so it fell through to
    ``CREDENTIAL_MARKERS`` - an unanchored scan of the WHOLE transcript. ``DIALOG_NEEDS_HUMAN``
    carrying its own evidence excerpt ``Authentication required`` (an alternative that genuinely lives
    in ``blocking_prompt_signature.regex``) was therefore relabelled ``NO_CREDENTIAL``, firing "a human
    must sign in; terminate the run" off one word of the excerpt it was quoting. Measured on the
    PR-#400 build.

    What is pinned here is STRUCTURAL PRESERVATION, not a finding about the source: the child's token
    survives into the parent's classification, and the parent may not upgrade an ambiguous dialog to
    the credential-stop family. Only ``CREDENTIAL_MISSING`` is credential-specific; these four tokens
    are outside that family because of what their EVIDENCE supports, which is not the same as ruling a
    sign-in prompt out (issue #146).

    Every line here is harvested from the real emitter with a real classification, so a reworded
    verdict or a new token is caught here rather than in production.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()

    windows = {
        "DIALOG_NEEDS_HUMAN": ("Authentication required",),
        "DIALOG_NEEDS_HUMAN/native": ("Permission is required to run this native database query",),
        "DIALOG_UNREADABLE": (),
        "DIALOG_UNRECOGNIZED": ("Save changes?", "Discard"),
        "REFRESH_IN_PROGRESS": ("Refresh", "Evaluating..."),
    }
    for label, texts in windows.items():
        window = credential_modal.DesktopWindow("Refresh" if "Refresh" in texts else "", "Cls", 702, 355, texts)
        finding = credential_modal.classify_dialog(window)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            refresh_pbip_model._emit_dialog_finding(111, finding)
        text = buffer.getvalue()

        verdict, _ = probe_live_source._classify_failure(text, network_fault_observed=False)

        assert probe_live_source._has_dialog_verdict(text), f"{label}: not recognised structurally"
        assert verdict == "ERROR", f"{label}: classified {verdict}, not the honest 'could not probe'"
        assert verdict != "NO_CREDENTIAL", (
            f"{label}: the parent invented a credential wall none of these tokens establishes"
        )
        assert not probe_live_source._has_credential_stop_verdict(text), (
            f"{label}: a dialog verdict must never JOIN the credential-stop family - that family's "
            "name is consumed elsewhere, and none of these tokens carries credential-specific evidence"
        )


def _dialog_window(credential_modal, texts: tuple[str, ...]):
    """One synthesised candidate window, shaped exactly as the other dialog tests here shape theirs."""
    return credential_modal.DesktopWindow("Refresh" if "Refresh" in texts else "", "Cls", 702, 355, texts)


# One window per token the dialog family can produce, plus the two kinds that FOLD into a token
# (mixed-content -> DIALOG_UNRECOGNIZED, caption-only -> DIALOG_UNREADABLE). Keyed by label so a
# failure names the case; the token is read back from the emitted line, never assumed.
DIALOG_TOKEN_WINDOWS = {
    "DIALOG_NEEDS_HUMAN/authentication-notice": ("Authentication required",),
    "DIALOG_NEEDS_HUMAN/native-query": ("Permission is required to run this native database query",),
    "DIALOG_UNREADABLE/no-text": (),
    "DIALOG_UNREADABLE/caption-only": ("Refresh",),
    "DIALOG_UNRECOGNIZED/read-nothing-matched": ("Save changes?", "Discard"),
    "DIALOG_UNRECOGNIZED/mixed-content": ("Evaluating...", "Delete these 4 tables?"),
    "REFRESH_IN_PROGRESS/progress-content": ("Refresh", "Evaluating..."),
}

# What the CHILD's own guidance line may and may not say, per token (issue #146). The classification,
# the tokens and the exit codes are unchanged; only the sentence beside them is under test.
#
#   DIALOG_NEEDS_HUMAN  matched `blocking_prompt_signature.regex`, whose alternatives span the
#                       native-query approval AND `Authentication (is )?required`. So it may say a
#                       human must act; it may NOT prescribe approving, and may not rule sign-in out.
#   DIALOG_UNREADABLE / DIALOG_UNRECOGNIZED  matched nothing, or read nothing. They settle the
#                       credential question in NEITHER direction and send a human to the screen.
#   REFRESH_IN_PROGRESS positively read progress content: wait or cancel, never stack.
CHILD_GUIDANCE_CONTRACT = {
    "DIALOG_NEEDS_HUMAN": {
        "required": ("known human-blocking prompt", "does not establish which action"),
        "forbidden": (
            "approve it",
            "approve whatever",
            "no account details are implied",
            "not a data-source sign-on prompt",
            "not a sign-in",
            "not a credential",
        ),
    },
    "DIALOG_UNREADABLE": {
        "required": ("look at the desktop screen",),
        "forbidden": (
            "approve",
            "not a sign-in",
            "not a credential",
            "supply account details",
            "no account details",
        ),
    },
    "DIALOG_UNRECOGNIZED": {
        "required": ("look at the desktop screen",),
        "forbidden": (
            "approve",
            "not a sign-in",
            "not a credential",
            "supply account details",
            "no account details",
        ),
    },
    "REFRESH_IN_PROGRESS": {
        "required": ("a refresh is already running on this pid", "do not stack"),
        "forbidden": ("approve", "not a sign-in", "not a credential"),
    },
}

# What the PARENT (`_verdict_lines.classify_child_verdict`) may and may not say for the same token.
# The verdict stays ERROR in every row - only the operator sentence differs.
PARENT_DETAIL_CONTRACT = {
    "DIALOG_NEEDS_HUMAN": {
        "required": ("known human-blocking prompt", "not which action", "do what the prompt visible there asks"),
        "forbidden": ("not a sign-in prompt", "approve whatever", "do not send anyone to re-authenticate"),
    },
    "DIALOG_UNREADABLE": {
        "required": ("could not classify", "do not assume sign-in is not needed"),
        "forbidden": ("not a sign-in prompt", "approve whatever"),
    },
    "DIALOG_UNRECOGNIZED": {
        "required": ("could not classify", "do not assume sign-in is not needed"),
        "forbidden": ("not a sign-in prompt", "approve whatever"),
    },
    "REFRESH_IN_PROGRESS": {
        "required": ("already has a refresh running on this pid", "never stack a second refresh"),
        # The generic could-not-classify branch is the WRONG home for a positively-read progress
        # dialog: it offers a possible-authentication hypothesis this token has already ruled out as
        # the observation it made.
        "forbidden": ("could not classify", "could be a connector authentication form"),
    },
}


def emitted_dialog_verdict(refresh_pbip_model, credential_modal, texts: tuple[str, ...]) -> tuple[str, str]:
    """Drive the REAL detector + REAL child emitter for ``texts``; return ``(token, transcript)``.

    Harvested, never hand-written (the #152/#153 discipline): a reworded guidance string or a changed
    fold order is caught here rather than in production.
    """
    finding = credential_modal.classify_dialog(_dialog_window(credential_modal, texts))
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        refresh_pbip_model._emit_dialog_finding(111, finding)
    transcript = buffer.getvalue()
    return finding.verdict, transcript


def assert_message_matches_its_token(contract: dict, token: str, message: str, *, label: str) -> None:
    """The whole prose invariant, in one place, so the mutation tests can assert it FAILS."""
    assert token in contract, f"{label}: no message contract for token {token!r}"
    blob = " ".join(message.split()).lower()
    for phrase in contract[token]["required"]:
        assert phrase in blob, f"{label} ({token}) must say {phrase!r}; it said: {message!r}"
    for phrase in contract[token]["forbidden"]:
        assert phrase not in blob, (
            f"{label} ({token}) claimed {phrase!r}, which its evidence cannot support: {message!r}"
        )


def test_each_dialog_token_says_only_what_its_evidence_supports() -> None:
    """Issue #146, the runtime half: child guidance AND parent detail, token by token.

    Both halves are driven through the real detector, the real emitter and the real classifier, so
    this fails on a reworded string rather than on a restated copy of one. Nothing about the
    classification is asserted here beyond the token itself - that is pinned by its own tests.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()

    seen = set()
    for label, texts in DIALOG_TOKEN_WINDOWS.items():
        token, transcript = emitted_dialog_verdict(refresh_pbip_model, credential_modal, texts)
        assert label.startswith(token), f"{label}: emitted {token}, so the fixture no longer covers what it names"
        seen.add(token)

        guidance = transcript.splitlines()[-1]
        assert_message_matches_its_token(CHILD_GUIDANCE_CONTRACT, token, guidance, label=label)

        verdict, detail = probe_live_source._classify_failure(transcript, network_fault_observed=False)
        assert verdict == "ERROR", f"{label}: classification changed - {verdict}"
        assert token in detail, f"{label}: the parent dropped the child's token from its own message: {detail!r}"
        assert_message_matches_its_token(PARENT_DETAIL_CONTRACT, token, detail, label=label)

    assert seen == set(CHILD_GUIDANCE_CONTRACT), f"a dialog token lost its fixture: {sorted(seen)}"


def test_unreadable_dialog_verdict_does_not_assert_not_a_sign_in_prompt() -> None:
    """Issue #146: 'NOT a sign-in prompt' is not available to ANY of these tokens.

    The original defect: DIALOG_UNREADABLE / DIALOG_UNRECOGNIZED could not be classified at all, so
    the assertion was stronger than the evidence. Measured 2026-09-06: a connector authentication form
    had no readable Win32 text, classified DIALOG_UNRECOGNIZED, and the parent asserted 'NOT a sign-in
    prompt' when sign-in WAS required.

    DIALOG_NEEDS_HUMAN is now in the same list rather than exempt from it. Its signature spans the
    native-query approval AND `Authentication (is )?required`, so 'NOT a sign-in prompt' is false for
    part of its own match set: the token establishes that a human must act, never which action. The
    case-specific native-query prompt may still be answered with an approval - that is a statement
    about the visible text, not about the token.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()

    for label, texts in DIALOG_TOKEN_WINDOWS.items():
        _, transcript = emitted_dialog_verdict(refresh_pbip_model, credential_modal, texts)
        _, detail = probe_live_source._classify_failure(transcript, network_fault_observed=False)

        assert "NOT a sign-in prompt" not in detail, (
            f"{label}: the parent rules a sign-in prompt OUT on evidence that cannot support it (issue #146)"
        )
        assert "not a sign-in prompt" not in transcript.lower(), (
            f"{label}: the child rules a sign-in prompt OUT on evidence that cannot support it"
        )


def test_the_progress_token_gets_its_own_parent_guidance_not_the_generic_branch() -> None:
    """REFRESH_IN_PROGRESS is a POSITIVE reading, so it may not share the could-not-classify text.

    Another refresh owns the pid: the action is wait or cancel, never stack a second refresh on it.
    Routing it to the generic branch offered a 'this COULD be a connector authentication form'
    hypothesis instead, which sends an operator to the wrong screen entirely.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()

    token, transcript = emitted_dialog_verdict(
        refresh_pbip_model, credential_modal, DIALOG_TOKEN_WINDOWS["REFRESH_IN_PROGRESS/progress-content"]
    )
    assert token == "REFRESH_IN_PROGRESS"

    verdict, detail = probe_live_source._classify_failure(transcript, network_fault_observed=False)
    _, unreadable_detail = probe_live_source._classify_failure(
        emitted_dialog_verdict(refresh_pbip_model, credential_modal, ())[1], network_fault_observed=False
    )

    assert verdict == "ERROR", "the classification is unchanged; only the guidance is dedicated"
    assert detail != unreadable_detail, "REFRESH_IN_PROGRESS fell back into the generic dialog branch"
    assert_message_matches_its_token(PARENT_DETAIL_CONTRACT, token, detail, label="REFRESH_IN_PROGRESS")


def test_mutation_swapping_the_dialog_guidance_table_breaks_the_named_assertion() -> None:
    """Mutation: give each token its sibling's guidance. The token-specific assertion must fail.

    Monkeypatch-free by construction - the mutation is applied to the detector's own table and driven
    through the real emitter, in the same shape as the caption-accounting Python mutation in the skill
    suite. Without it, a contract of only-forbidden phrases would pass on any wrong-but-clean string.
    """
    refresh_pbip_model, _, credential_modal = _import_skill_modules()

    original = dict(credential_modal.DIALOG_KIND_GUIDANCE)
    swapped = dict(original)
    swapped[credential_modal.DIALOG_KIND_NEEDS_HUMAN] = original[credential_modal.DIALOG_KIND_BENIGN]
    swapped[credential_modal.DIALOG_KIND_BENIGN] = original[credential_modal.DIALOG_KIND_NEEDS_HUMAN]
    credential_modal.DIALOG_KIND_GUIDANCE.update(swapped)
    try:
        token, transcript = emitted_dialog_verdict(
            refresh_pbip_model, credential_modal, DIALOG_TOKEN_WINDOWS["DIALOG_NEEDS_HUMAN/native-query"]
        )
        guidance = transcript.splitlines()[-1]
        with pytest.raises(AssertionError, match="known human-blocking prompt"):
            assert_message_matches_its_token(CHILD_GUIDANCE_CONTRACT, token, guidance, label="mutated")
    finally:
        credential_modal.DIALOG_KIND_GUIDANCE.clear()
        credential_modal.DIALOG_KIND_GUIDANCE.update(original)


def test_mutation_collapsing_progress_into_the_generic_branch_breaks_the_named_assertion() -> None:
    """Mutation: route REFRESH_IN_PROGRESS back through the generic could-not-classify text.

    The generic detail is HARVESTED from the real classifier (an unreadable dialog) rather than
    hand-written, so this reproduces exactly what removing the dedicated branch would print.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()

    _, unreadable_transcript = emitted_dialog_verdict(refresh_pbip_model, credential_modal, ())
    _, generic_detail = probe_live_source._classify_failure(unreadable_transcript, network_fault_observed=False)
    collapsed = generic_detail.replace("DIALOG_UNREADABLE", "REFRESH_IN_PROGRESS")

    with pytest.raises(AssertionError, match="already has a refresh running on this pid"):
        assert_message_matches_its_token(PARENT_DETAIL_CONTRACT, "REFRESH_IN_PROGRESS", collapsed, label="mutated")


def test_mutation_restoring_the_retired_approval_prose_breaks_the_named_assertion() -> None:
    """Mutation: put master's 'approve it / not a sign-on prompt' wording back, both halves.

    Child guidance and parent detail are mutated separately, because the retired sentence existed in
    both and each is read by a different operator surface. The parent half mutates the SHIPPED
    sentence (harvested from the real classifier) so exactly one clause changes, and asserts the
    mutation landed - a mutation that misses its target proves nothing about the assertion it claims
    to exercise.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()

    retired_child = (
        "this is a KNOWN human-blocking prompt (e.g. the native database query approval), not a "
        "data-source sign-on prompt - approve it at the Desktop screen; no account details are implied"
    )
    with pytest.raises(AssertionError, match="does not establish which action"):
        assert_message_matches_its_token(CHILD_GUIDANCE_CONTRACT, "DIALOG_NEEDS_HUMAN", retired_child, label="mutated")

    token, transcript = emitted_dialog_verdict(
        refresh_pbip_model, credential_modal, DIALOG_TOKEN_WINDOWS["DIALOG_NEEDS_HUMAN/authentication-notice"]
    )
    _, detail = probe_live_source._classify_failure(transcript, network_fault_observed=False)
    prescriptive = detail.replace(
        "NOT which action, and NOT that re-authenticating is unnecessary",
        "the remedy is to approve whatever it is showing",
    )
    assert prescriptive != detail, "the mutation did not land on the shipped sentence"

    with pytest.raises(AssertionError, match="not which action"):
        assert_message_matches_its_token(PARENT_DETAIL_CONTRACT, token, prescriptive, label="mutated")


def test_permission_refusals_stay_access_denied_ahead_of_the_credential_markers() -> None:
    """A 403 is ACCESS_DENIED and stays ACCESS_DENIED: the server authenticated us, then refused.

    Branch order is the whole control - ``ACCESS_DENIED_MARKERS`` is tested before the unanchored
    ``CREDENTIAL_MARKERS`` scan, and the texts below carry BOTH kinds of word on purpose. Signing in
    again cannot repair a permission refusal, so classifying one as a credential problem sends an
    operator to a screen that will not help and invites an unchanged retry.
    """
    probe_live_source = _import_probe_live_source()

    for text in (
        "DataSource.Error: the remote server returned an error: (403) Forbidden",
        "Authentication succeeded but the token does not have permission to access this warehouse",
        "AccessDenied: insufficient privileges for the credential in use",
    ):
        verdict, _ = probe_live_source._classify_failure(text, network_fault_observed=False)

        assert verdict == "ACCESS_DENIED", f"{text!r} classified {verdict}, not the permission-specific verdict"
        assert verdict != "NO_CREDENTIAL", "a permission refusal must not be routed to a sign-in"


def test_the_parent_knows_every_dialog_token_the_detector_can_emit() -> None:
    """Anti-drift: a token added to the detector must not stay unknown to the parent classifier.

    Without this the two halves of finding 2's fix can separate silently - a new verdict would fall
    straight back through to the free-text scan that caused the defect. The list is DISCOVERED from
    the detector's own table rather than restated here.
    """
    probe_live_source = _import_probe_live_source()
    _, _, credential_modal = _import_skill_modules()

    tokens = {
        verdict
        for kind, verdict in credential_modal.DIALOG_KIND_VERDICTS.items()
        if kind != credential_modal.DIALOG_KIND_CREDENTIAL
    }
    assert len(tokens) >= 4, f"expected the detector's dialog tokens; got {sorted(tokens)}"

    unknown = [
        token for token in sorted(tokens) if not probe_live_source._has_dialog_verdict(f"REFRESH: {token} pid=1; x")
    ]

    assert not unknown, f"probe_live_source does not recognise these dialog verdicts: {unknown}"


def test_a_dialog_verdict_cannot_clear_the_live_source_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth for finding 2: a dialog verdict blocks success even on a zero exit code.

    The exit code alone already blocks it, but the child's success gate lists every authoritative
    non-success verdict explicitly, and this one belongs there with its siblings. Every token in the
    family is driven, with the success line AFTER the dialog line: an active dialog vetoes a stale or
    late DATA_OK-looking line, whichever order they arrive in.
    """
    probe_live_source = _import_probe_live_source()
    for token in ("DIALOG_UNREADABLE", "DIALOG_UNRECOGNIZED", "DIALOG_NEEDS_HUMAN", "REFRESH_IN_PROGRESS"):
        stdout = f"REFRESH: {token} pid=111; kind=x\nREFRESH: TABLES_OK 'Orders'\nREFRESH: DATA_OK\n"
        monkeypatch.setattr(
            probe_live_source.subprocess,
            "run",
            lambda *_args, _out=stdout, **_kwargs: subprocess.CompletedProcess(
                args=["refresh"],
                returncode=0,
                stdout=_out,
                stderr="",
            ),
        )

        rc, verdict = probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False)

        assert (rc, verdict) != (0, "DATA_OK"), f"{token}: a dialog verdict must veto a late success line"
        assert verdict == "ERROR", f"{token}: classified {verdict}"


def test_timeout_verdict_is_recognised_structurally_not_as_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #146: the child's TIMEOUT verdict prose names 'sign-in' and must not become NO_CREDENTIAL.

    End-to-end: drives the real child timeout emitter, captures its exact transcript/exit code,
    and feeds them into the real parent ``_refresh_and_classify``. Without the structural
    ``TIMEOUT`` recognition, the diagnostic prose ("a human must sign in once") trips the
    unanchored ``CREDENTIAL_MARKERS`` scan and the parent reports ``NO_CREDENTIAL`` without any
    observed auth prompt.

    The mutation control: removing the structural ``TIMEOUT`` match must make this test fail on
    the ``NO_CREDENTIAL`` assertion, not on an unrelated check.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, _ = _import_skill_modules()

    # Reproduce the child's REAL timeout emitter output. The child hits this path when a
    # TimeoutError is raised during refresh and the exception text contains "timeout".
    # We use the exact format from refresh_pbip_model.py main() lines 1717-1734, with the
    # CREDENTIAL_PROBE path resolved from the real module.
    credential_probe = refresh_pbip_model.CREDENTIAL_PROBE
    timeout_text = "TimeoutError: XMLA CommandTimeout was 300s and did not fire"
    child_stdout_lines = [
        f"REFRESH: TIMEOUT - no result within configured refresh deadline ({timeout_text})",
        "  CAUSE UNKNOWN - this script cannot distinguish these two, and they need",
        "  opposite responses:",
        "    (a) SLOW: a very large model. Refresh only what you need with --tables;",
        "        do NOT simply wait longer.",
        "    (b) BLOCKED: Desktop is showing a data-source sign-in modal no automation",
        "        can fill. Retrying cannot dismiss it; a human must sign in once.",
        "  SETTLE IT - run the arbiter that ships beside this script, do not guess:",
        f'    powershell -File "{credential_probe}" -DesktopPid 111',
    ]
    child_stdout = "\n".join(child_stdout_lines) + "\n"
    child_exit = 3  # the real emitter returns 3

    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=["refresh"],
            returncode=child_exit,
            stdout=child_stdout,
            stderr="",
        ),
    )

    rc, verdict = probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False)

    # The timeout verdict MUST be recognised structurally and mapped to ERROR.
    assert probe_live_source._has_timeout_verdict(child_stdout), (
        "the structural TIMEOUT recogniser did not match the child's real output"
    )
    assert verdict == "ERROR", (
        f"expected ERROR for an ambiguous timeout; got {verdict}. "
        "If NO_CREDENTIAL: the unanchored credential-marker scan read the diagnostic prose."
    )
    assert verdict != "NO_CREDENTIAL", (
        "TIMEOUT must never classify as NO_CREDENTIAL: the child could not distinguish "
        "a slow source from a credential modal"
    )


def test_timeout_verdict_cannot_clear_the_live_source_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: a TIMEOUT verdict blocks success even beside a stale DATA_OK."""
    probe_live_source = _import_probe_live_source()
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=["refresh"],
            returncode=0,
            stdout="REFRESH: TIMEOUT - deadline\nREFRESH: TABLES_OK 'Orders'\n",
            stderr="",
        ),
    )

    rc, verdict = probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False)

    assert (rc, verdict) != (0, "DATA_OK"), "a TIMEOUT verdict must veto a stale-looking success line"
    assert verdict == "ERROR"


def _harvest_minimized_reason(credential_modal) -> str:
    """The REAL minimized-owner ``unknown_reason``, harvested by driving the detector (not hand-written).

    Same #152/#153 discipline as ``_detector_unknown_reasons``: the parent must be tested against the
    exact string the child emits, so that a reworded reason (or a reason that grows a forbidden marker)
    is caught here rather than in production.
    """
    minimized_main = credential_modal.DesktopWindow(
        title="Report",
        class_name=credential_modal.DESKTOP_MAIN_CLASS_PREFIX + ".app.0",
        width=1200,
        height=800,
        minimized=True,
    )
    reason = credential_modal.inspect_credential_modal(
        111, enumerate_windows=lambda _pid: [minimized_main]
    ).unknown_reason
    assert reason, "detector did not report the minimized-owner UNKNOWN reason"
    return reason


def test_credential_unknown_verdict_classifies_as_no_credential() -> None:
    """#154: a latched CREDENTIAL_UNKNOWN is a credential STOP, matched STRUCTURALLY (not by free text).

    The child line is harvested from the real ``refresh_pbip_model._emit_credential_unknown`` with a
    real detector reason, so the parent is exercised against the exact bytes the child emits. It must
    both satisfy the anchored verdict-line matcher (``_has_credential_stop_verdict``) and classify as
    ``NO_CREDENTIAL`` - never ``SLOW_SOURCE``/timeout, which was the #154 defect.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()
    reason = _harvest_minimized_reason(credential_modal)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        refresh_pbip_model._emit_credential_unknown(111, reason)
    emitted = buffer.getvalue()

    assert "REFRESH: CREDENTIAL_UNKNOWN" in emitted
    assert probe_live_source._has_credential_stop_verdict(emitted), "must match the anchored verdict line, not prose"
    verdict, _ = probe_live_source._classify_failure(emitted, network_fault_observed=False)
    assert verdict == "NO_CREDENTIAL"


def test_credential_unknown_full_child_transcript_classifies_as_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """#154 end-to-end at the parent seam: the child's WHOLE CREDENTIAL_UNKNOWN transcript classifies
    ``NO_CREDENTIAL``.

    The transcript is assembled from the real child functions the #154 path prints - the t=0 UNKNOWN
    banner, the iconic-transition notice, and the emit line - so the classifier sees the exact
    concatenation production produces. This also guards the ordering trap: none of that prose may trip
    the earlier BAD_TABLE / ACCESS_DENIED branches before the structural credential-stop check runs.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()
    reason = _harvest_minimized_reason(credential_modal)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        credential_modal.print_refresh_unknown_banner(111, 300, 30, reason)
        credential_modal.print_indeterminate_state_notice(111, reason)
        refresh_pbip_model._emit_credential_unknown(111, reason)
    transcript = buffer.getvalue()

    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(args=["refresh"], returncode=3, stdout=transcript, stderr=""),
    )

    assert probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False) == (  # noqa: SLF001
        1,
        "NO_CREDENTIAL",
    )


def _harvest_zero_window_alive_reason(credential_modal) -> str:
    """The REAL zero-window-but-alive readiness reason, harvested from the detector (issue #158).

    Same #152/#153 discipline as ``_harvest_minimized_reason``: assert against the exact string the
    detector really emits when enumeration returns an empty list while the process is still alive.
    """
    reason = credential_modal.inspect_credential_modal(
        111, enumerate_windows=lambda _pid: [], process_is_alive=lambda _pid: True
    ).desktop_unready
    assert reason, "detector did not report the zero-window alive DESKTOP_UNREADY reason"
    return reason


def _harvest_desktop_gone_reason(credential_modal) -> str:
    """The REAL ``process_gone`` terminal string, harvested by driving the detector (issue #158)."""
    reason = credential_modal.inspect_credential_modal(
        111, enumerate_windows=lambda _pid: [], process_is_alive=lambda _pid: False
    ).process_gone
    assert reason, "detector did not report the process-gone terminal reason"
    return reason


def test_desktop_gone_verdict_classifies_as_error() -> None:
    """#158: a DESKTOP_GONE verdict line classifies as ERROR, never UNREACHABLE or a credential stop.

    A dead Desktop is a LOCAL failure - the source was never contacted - so it must be ERROR (the probe
    could not run), never UNREACHABLE (which would send someone to fix a reachable server) and never
    NO_CREDENTIAL (no human sign-in revives a dead process). The child line is harvested from the real
    ``refresh_pbip_model._emit_desktop_gone`` with a real detector reason, so the parent is exercised
    against the exact bytes the child emits. Matched STRUCTURALLY on the verdict line, not by free text.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()
    reason = _harvest_desktop_gone_reason(credential_modal)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        refresh_pbip_model._emit_desktop_gone(111, reason)
    emitted = buffer.getvalue()

    assert "REFRESH: DESKTOP_GONE" in emitted
    assert probe_live_source._has_desktop_gone_verdict(emitted), "must match the anchored verdict line, not prose"
    verdict, _ = probe_live_source._classify_failure(emitted, network_fault_observed=False)
    assert verdict == "ERROR"


def test_desktop_gone_and_zero_window_detector_strings_are_marker_free() -> None:
    """#158/#153: the two new detector strings must contain NO classifier marker token.

    Harvested from the real detector (never eyeballed) and tested against the REAL marker tuples the
    parent scans. A stray marker in either string would let the free-text scan misclassify a dead or
    window-less Desktop as NO_CREDENTIAL / BAD_TABLE / ACCESS_DENIED instead of its true verdict - that
    was the #153 failure mode, so it is pinned here against the actual imported tuples.
    """
    probe_live_source = _import_probe_live_source()
    _, _, credential_modal = _import_skill_modules()
    markers = (
        probe_live_source.CREDENTIAL_MARKERS
        + probe_live_source.BAD_TABLE_MARKERS
        + probe_live_source.ACCESS_DENIED_MARKERS
    )
    strings = {
        "zero_window_alive_desktop_unready": _harvest_zero_window_alive_reason(credential_modal),
        "process_gone": _harvest_desktop_gone_reason(credential_modal),
    }
    offenders = {name: [m for m in markers if m in text.lower()] for name, text in strings.items()}
    offenders = {name: hits for name, hits in offenders.items() if hits}
    assert not offenders, f"new detector strings contain classifier markers: {offenders}"


def test_desktop_gone_full_child_transcript_classifies_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """#158 end-to-end at the parent seam: the child's WHOLE DESKTOP_GONE transcript classifies ERROR.

    The transcript is the real child emitter's whole output (verdict line + marker-free guidance), fed
    through ``_refresh_and_classify`` with the child's planned exit 2, so the classifier sees the exact
    concatenation production produces. It must resolve to ``ERROR`` - never UNREACHABLE, and none of the
    guidance prose may trip the BAD_TABLE / ACCESS_DENIED / credential branches first.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()
    reason = _harvest_desktop_gone_reason(credential_modal)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        refresh_pbip_model._emit_desktop_gone(111, reason)
    transcript = buffer.getvalue()

    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(args=["refresh"], returncode=2, stdout=transcript, stderr=""),
    )

    assert probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False) == (  # noqa: SLF001
        1,
        "ERROR",
    )


def test_zero_window_alive_emitter_and_parent_classify_as_local_error() -> None:
    """#158: a live window-less Desktop is not evidence of a credential problem.

    The verdict alone cannot carry this test. Measured 2026-08-15 by mutation: deleting the whole
    ``DESKTOP_UNREADY`` branch from ``_classify_failure`` leaves the verdict byte-identical at
    ``ERROR``, because the catch-all fallback at the end of the function also returns ``ERROR`` - so a
    verdict-only assertion is a test that cannot fail, credited as coverage for a regex it never
    exercises. What actually changes is the DETAIL: the branch's own readiness guidance degrades to
    *"unclassified refresh failure"*, which sends a reader looking for a source problem that does not
    exist. Both halves are therefore asserted here, on the exact strings production emits.
    """
    probe_live_source = _import_probe_live_source()
    refresh_pbip_model, _, credential_modal = _import_skill_modules()
    state = credential_modal.inspect_credential_modal(
        111, enumerate_windows=lambda _pid: [], process_is_alive=lambda _pid: True
    )

    assert state.unknown_reason is None
    assert state.desktop_unready
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        refresh_pbip_model._emit_desktop_unready(111, state.desktop_unready)
    emitted = buffer.getvalue()

    assert "REFRESH: DESKTOP_UNREADY" in emitted
    assert "minimiz" not in emitted.lower()
    assert "sign in" not in emitted.lower()
    verdict, detail = probe_live_source._classify_failure(emitted, network_fault_observed=False)
    assert verdict == "ERROR"
    assert "running without any window" in detail
    assert "could not inspect its local state or query the source" in detail
    assert "unclassified refresh failure" not in detail, (
        "the DESKTOP_UNREADY branch was not taken - the catch-all fallback answered instead, which "
        "returns the same ERROR verdict but names no cause"
    )


def test_credential_stop_precedes_success_when_verdict_lines_contradict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A contradictory exit-0 transcript fails closed even though real producers cannot emit it."""
    probe_live_source = _import_probe_live_source()
    transcript = "REFRESH: TABLES_OK 'Orders'\nREFRESH: CREDENTIAL_UNKNOWN pid=111; indeterminate"
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(args=["refresh"], returncode=0, stdout=transcript, stderr=""),
    )

    assert probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False) == (
        1,
        "NO_CREDENTIAL",
    )


def _import_child_refresh():
    """Import the child refresh module from the skill's own scripts folder."""
    scripts = str(REPO / ".github" / "skills" / "pbip-model-refresh" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import refresh_pbip_model  # noqa: PLC0415

    return refresh_pbip_model


def test_probe_timeout_strictly_outlasts_child_ceiling() -> None:
    """#156: the parent's refresh bound MUST strictly outlast the child's own deadline.

    The inversion this guards against: with the old default of 180s the parent SIGKILLed the child
    150s BEFORE the child's own ceiling (300 + 30 = 330s) could fire, so the child's far better
    verdict (a credential re-check, then a TimeoutError naming the mashup-modal signature) was dead
    code from the probe path. Computed by importing BOTH sides so any future change to either
    constant re-checks the ordering - hard-coding 390/330 here would not catch a drift in the child.
    """
    probe_live_source = _import_probe_live_source()
    child = _import_child_refresh()
    child_ceiling = child.REFRESH_TIMEOUT_SECONDS + child.REFRESH_WALL_CLOCK_GRACE_SECONDS
    assert probe_live_source.PROBE_TIMEOUT_SECONDS > child_ceiling, (
        f"parent PROBE_TIMEOUT_SECONDS={probe_live_source.PROBE_TIMEOUT_SECONDS} must strictly exceed the "
        f"child's own deadline {child_ceiling}s (REFRESH_TIMEOUT_SECONDS + REFRESH_WALL_CLOCK_GRACE_SECONDS), "
        "or the child's deadline classification never fires before the parent kills it."
    )


def test_probe_timeout_is_derived_from_child_not_retyped() -> None:
    """#156: the parent's bound must equal the child's constants plus the documented margin.

    Enforcing derivation-by-construction is the point: a bound picked as an independent literal can
    silently drift back into the inversion the moment someone bumps the child's ceiling.
    """
    probe_live_source = _import_probe_live_source()
    child = _import_child_refresh()
    expected = (
        child.REFRESH_TIMEOUT_SECONDS
        + child.REFRESH_WALL_CLOCK_GRACE_SECONDS
        + probe_live_source.PROBE_KILL_MARGIN_SECONDS
    )
    assert probe_live_source.PROBE_TIMEOUT_SECONDS == expected
    # The parent must reuse the child's own objects, not a private copy that could diverge.
    assert probe_live_source.REFRESH_TIMEOUT_SECONDS == child.REFRESH_TIMEOUT_SECONDS
    assert probe_live_source.REFRESH_WALL_CLOCK_GRACE_SECONDS == child.REFRESH_WALL_CLOCK_GRACE_SECONDS


def test_probe_timeout_default_threads_all_the_way_to_run_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#156 (the WIRING, not just the constant): the derived default must be the value that actually
    reaches the refresh subprocess.

    The two tests above pin the constant and its derivation, but neither observes what ``main()`` hands
    to ``run_probe`` -> ``subprocess.run(..., timeout=timeout_sec)``. Reverting only the argparse default
    to 180 (leaving PROBE_TIMEOUT_SECONDS=390) restores the exact inversion #156 exists to prevent while
    both constant tests stay green. This drives ``main()`` with ``run_probe`` captured, so it fails the
    moment the default drifts off the constant. No live Desktop or real bundle: ``run_probe`` is replaced
    and an existing empty directory satisfies the path-exists guard.
    """
    probe_live_source = _import_probe_live_source()
    captured: dict[str, int] = {}

    def _capture(_bundle: Path, _source_index: int | None, timeout_sec: int, _keep: bool) -> int:
        captured["timeout_sec"] = timeout_sec
        return 0

    monkeypatch.setattr(probe_live_source, "run_probe", _capture)
    rc = probe_live_source.main(["--bundle", str(tmp_path)])

    assert rc == 0
    assert captured["timeout_sec"] == probe_live_source.PROBE_TIMEOUT_SECONDS, (
        f"main() handed run_probe timeout_sec={captured.get('timeout_sec')}, but the derived default is "
        f"{probe_live_source.PROBE_TIMEOUT_SECONDS}; the argparse default has drifted off the constant, so the "
        "child would be SIGKILLed before its own deadline can fire (issue #156 inversion)."
    )
