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


def test_blocked_by_dialog_does_not_clear_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic Desktop dialog is a human-blocked source, not reachable data."""
    probe_live_source = _import_probe_live_source()
    monkeypatch.setattr(
        probe_live_source.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["refresh"],
            returncode=1,
            stdout="REFRESH: BLOCKED_BY_DIALOG pid=111; window title='(empty title)'\n",
            stderr="",
        ),
    )

    assert probe_live_source._refresh_and_classify(123, "Orders", 1, network_fault_observed=False) == (  # noqa: SLF001
        1,
        "NO_CREDENTIAL",
    )


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
    """#158: a live window-less Desktop is not evidence of a credential problem."""
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
    assert probe_live_source._classify_failure(emitted, network_fault_observed=False)[0] == "ERROR"


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
