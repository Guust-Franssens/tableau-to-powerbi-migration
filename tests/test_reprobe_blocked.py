"""Tests for the credential-gate re-probe sweep (`scripts/reprobe_blocked.py`).

These are hermetic: no Power BI Desktop, no live database, no cached credential. The one
Desktop-touching seam (`_default_probe_runner` actually launching `probe_live_source.py`) is NOT
exercised here - tests inject a fake runner and drive every classification/exit branch from canned
outcomes plus REAL marker-file transitions in a tmp dir. The probe's verdict/exit contract this code
depends on (SKIPPED exit 0, UNREACHABLE exit 1, verdict on a `PROBE: <VERDICT>` line, a trailing
non-verdict `PROBE: source index N failed` summary line) was verified manually against the real probe;
see `reprobe_blocked.py`'s module docstring.

The load-bearing behaviours pinned here, each written so a mutation to the code makes a test FAIL:
- the tool NEVER lifts a gate itself - it only runs the probe, whose own machinery earns the clear;
- ground truth (the gate transition) beats the probe's self-reported verdict, so a DATA_OK that did
  NOT lower the gate is an `anomaly`, never a false `newly-earned` and never `still-blocked`;
- NO_CREDENTIAL and UNREACHABLE are reported as DIFFERENT problems;
- the verdict parser takes the real verdict, not the trailing `PROBE: source index N failed` line;
- exit-code precedence forged > anomaly/errored > blocked > clean.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import credential_gate  # noqa: E402  # pylint: disable=wrong-import-position
import reprobe_blocked as rb  # noqa: E402  # pylint: disable=wrong-import-position

MARKER = credential_gate.MARKER
OVERRIDE = credential_gate.OVERRIDE
AUDIT = credential_gate.AUDIT


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------
def _blocked_unit(root: Path, name: str = "unit", sources: list[str] | None = None) -> Path:
    """A directory that looks BLOCKED to the gate: it carries the marker file."""
    unit = root / name
    unit.mkdir(parents=True, exist_ok=True)
    (unit / MARKER).write_text(
        json.dumps({"writes_blocked": True, "sources": sources or ["snowflake_x"]}), encoding="utf-8"
    )
    return unit


def _clean_unit(root: Path, name: str = "clean") -> Path:
    """A directory with no marker - not gated."""
    unit = root / name
    unit.mkdir(parents=True, exist_ok=True)
    return unit


def _options(apply: bool = True) -> rb.SweepOptions:
    return rb.SweepOptions(apply=apply, refresh_timeout_sec=None, per_unit_timeout_sec=0)


def _runner_that(verdict: str | None, *, clears: bool, returncode: int | None = 0, timed_out: bool = False):
    """A fake probe runner. If `clears`, it removes the marker to simulate the probe's earned clear."""

    def runner(unit: Path, _options: rb.SweepOptions) -> rb.ProbeOutcome:
        if clears:
            (unit / MARKER).unlink(missing_ok=True)
        return rb.ProbeOutcome(returncode=returncode, verdict=verdict, timed_out=timed_out, detail=f"detail:{verdict}")

    return runner


# --------------------------------------------------------------------------------------------------
# _parse_verdict / _verdict_line
# --------------------------------------------------------------------------------------------------
def test_parse_verdict_ignores_the_trailing_source_index_summary_line() -> None:
    """The LAST `PROBE:` line is a non-verdict summary; the parser must return the real verdict."""
    text = (
        "PROBE: UNREACHABLE 'x' does not resolve in DNS. check server\n"
        "PROBE: source index 0 failed - not lifting the gate\n"
    )
    assert rb._parse_verdict(text) == "UNREACHABLE"


def test_parse_verdict_takes_the_last_known_verdict() -> None:
    text = "PROBE: BAD_TABLE t not found\nPROBE: DATA_OK all 1 live source(s) reachable\n"
    assert rb._parse_verdict(text) == "DATA_OK"


def test_parse_verdict_returns_none_without_a_recognised_verdict() -> None:
    assert rb._parse_verdict("nothing here\nPROBE: source index 0 failed\n") is None
    assert rb._parse_verdict("") is None


def test_parse_verdict_survives_a_bare_probe_line() -> None:
    """Finding #1: a line that strips to exactly `PROBE:` must NOT raise (it once IndexError'd and
    aborted the whole sweep). The real verdict before it still wins."""
    assert rb._parse_verdict("PROBE: NO_CREDENTIAL x\nPROBE:\n") == "NO_CREDENTIAL"
    assert rb._parse_verdict("PROBE:\n") is None


def test_verdict_line_survives_a_bare_probe_line() -> None:
    assert rb._verdict_line("PROBE:\nPROBE: DATA_OK all reachable\n") == "PROBE: DATA_OK all reachable"
    assert rb._verdict_line("PROBE:\n") == ""


# --------------------------------------------------------------------------------------------------
# read_units_source
# --------------------------------------------------------------------------------------------------
def test_read_units_source_parses_the_list_json_shape() -> None:
    payload = json.dumps(
        {
            "root": "r",
            "units": [
                {"unit": "/a/one", "relative": "one", "state": "BLOCKED"},
                {"unit": "/a/two", "relative": "two", "state": "cleared-earned"},
            ],
        }
    )
    units = rb.read_units_source(payload)
    assert [(u.path, u.state) for u in units] == [(Path("/a/one"), "BLOCKED"), (Path("/a/two"), "cleared-earned")]


def test_read_units_source_parses_a_bare_json_array_of_paths() -> None:
    units = rb.read_units_source(json.dumps(["/a/one", "/a/two"]))
    assert [u.path for u in units] == [Path("/a/one"), Path("/a/two")]
    assert all(u.state is None for u in units)


def test_read_units_source_parses_newline_paths_and_skips_comments() -> None:
    units = rb.read_units_source("/a/one\n# a comment\n\n/a/two\n")
    assert [u.path for u in units] == [Path("/a/one"), Path("/a/two")]


def test_read_units_source_raises_on_truncated_json_rather_than_guessing_paths() -> None:
    """Finding #3: input that clearly meant to be JSON (begins { or [) but is malformed must NOT be
    silently reinterpreted as newline paths, which would mask a truncated pipe as a clean estate."""
    with pytest.raises(rb.InputError):
        rb.read_units_source('{"root":"r","units":[{"unit":"/a/one",')
    with pytest.raises(rb.InputError):
        rb.read_units_source('[{"unit":"/a/one"')


def test_read_units_source_empty_is_empty_not_an_error() -> None:
    assert rb.read_units_source("") == []
    assert rb.read_units_source("   \n  ") == []


# --------------------------------------------------------------------------------------------------
# select
# --------------------------------------------------------------------------------------------------
def test_select_probes_a_bare_blocked_path_and_skips_a_clean_one(tmp_path: Path) -> None:
    blocked = _blocked_unit(tmp_path)
    clean = _clean_unit(tmp_path)
    to_probe, skipped, forged = rb.select([rb.UnitInput(path=blocked), rb.UnitInput(path=clean)])
    assert [u.path for u in to_probe] == [blocked.resolve()]
    assert [s.unit for s in skipped] == [clean.resolve()]
    assert forged is False


def test_select_honours_a_blocked_state_but_reverifies_the_marker(tmp_path: Path) -> None:
    """A unit LISTED as BLOCKED whose marker is already gone must not be re-probed."""
    gone = _clean_unit(tmp_path, "gone")  # no marker, but list says BLOCKED
    to_probe, skipped, _ = rb.select([rb.UnitInput(path=gone, state="BLOCKED")])
    assert not to_probe
    assert "marker is gone" in skipped[0].reason


def test_select_does_not_probe_non_blocked_states(tmp_path: Path) -> None:
    """A non-BLOCKED state stops the probe - but when the marker is present it is LOUD, not silent.

    This test previously asserted a silent `skipped` for exactly the disagreement it builds (marker
    on disk, `list` claiming `cleared-earned`). Blind review 2026-08-27: that is the shape that
    reports exit 0 - "clean, nothing left blocked" - over a still-armed gate, so the verdict is now
    `anomaly`. The no-probe half of the original intent is unchanged and still asserted; the sibling
    test below covers the benign case where the marker really is gone.
    """
    unit = _blocked_unit(tmp_path)  # marker present, but list says already cleared
    to_probe, skipped, forged = rb.select([rb.UnitInput(path=unit, state="cleared-earned")])
    assert not to_probe
    assert skipped[0].category == rb.CAT_ANOMALY
    assert forged is False


def test_select_flags_a_forged_override_even_when_the_directory_is_absent(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    to_probe, skipped, forged = rb.select([rb.UnitInput(path=missing, state="FORGED-OVERRIDE")])
    assert not to_probe
    assert forged is True
    assert "verify" in skipped[0].reason.lower()


def test_select_deduplicates_by_resolved_path(tmp_path: Path) -> None:
    blocked = _blocked_unit(tmp_path)
    to_probe, _, _ = rb.select([rb.UnitInput(path=blocked), rb.UnitInput(path=blocked)])
    assert len(to_probe) == 1


def test_select_errors_on_a_nonexistent_explicit_unit(tmp_path: Path) -> None:
    """Finding #3: a typo'd/moved/missing explicit path must be `errored` (exit 5), not `skipped`,
    so a broken input cannot masquerade as a clean estate by exiting 0."""
    missing = tmp_path / "typo-does-not-exist"
    to_probe, pre_results, _ = rb.select([rb.UnitInput(path=missing)])
    assert not to_probe
    assert pre_results[0].category == rb.CAT_ERRORED


def test_select_errors_on_a_unit_that_is_a_file_not_a_directory(tmp_path: Path) -> None:
    afile = tmp_path / "a-file.txt"
    afile.write_text("x", encoding="utf-8")
    _, pre_results, _ = rb.select([rb.UnitInput(path=afile)])
    assert pre_results[0].category == rb.CAT_ERRORED


# --------------------------------------------------------------------------------------------------
# classify (pure) - ground truth beats the self-reported verdict
# --------------------------------------------------------------------------------------------------
def test_classify_newly_earned_when_the_gate_went_down() -> None:
    category, verdict, _ = rb.classify(rb.ProbeOutcome(0, "DATA_OK", False, "ok"), blocked_after=False)
    assert category == rb.CAT_NEWLY_EARNED
    assert verdict == "DATA_OK"


def test_classify_gate_down_without_data_ok_is_an_out_of_band_anomaly_not_earned() -> None:
    """If the gate is down but the probe did NOT report DATA_OK (e.g. a concurrent authorize), it was
    NOT earned by this probe - crediting it as newly-earned would launder unearned into earned."""
    category, _, reason = rb.classify(rb.ProbeOutcome(1, "NO_CREDENTIAL", False, "x"), blocked_after=False)
    assert category == rb.CAT_ANOMALY
    assert "out-of-band" in reason


def test_classify_data_ok_but_gate_still_up_is_an_anomaly_not_a_clear_and_not_blocked() -> None:
    """The measured `_lift_gate` defect: DATA_OK yet the gate stayed armed. Must be `anomaly`."""
    category, verdict, reason = rb.classify(rb.ProbeOutcome(0, "DATA_OK", False, "ok"), blocked_after=True)
    assert category == rb.CAT_ANOMALY
    assert verdict == "DATA_OK"
    assert "still armed" in reason.lower()


def test_classify_skipped_on_an_armed_unit_is_an_anomaly() -> None:
    category, _, _ = rb.classify(rb.ProbeOutcome(0, "SKIPPED", False, "no live"), blocked_after=True)
    assert category == rb.CAT_ANOMALY


def test_classify_no_credential_and_unreachable_are_distinct_still_blocked_reasons() -> None:
    cat_cred, v_cred, reason_cred = rb.classify(rb.ProbeOutcome(1, "NO_CREDENTIAL", False, "x"), blocked_after=True)
    cat_dns, v_dns, reason_dns = rb.classify(rb.ProbeOutcome(1, "UNREACHABLE", False, "x"), blocked_after=True)
    assert cat_cred == cat_dns == rb.CAT_STILL_BLOCKED
    assert (v_cred, v_dns) == ("NO_CREDENTIAL", "UNREACHABLE")
    assert reason_cred != reason_dns
    assert "sign in" in reason_cred.lower()
    assert "sign-in" in reason_dns.lower() or "no sign" in reason_dns.lower()


def test_classify_operator_required_stays_blocked() -> None:
    category, _, _ = rb.classify(rb.ProbeOutcome(4, "OPERATOR_REQUIRED", False, "x"), blocked_after=True)
    assert category == rb.CAT_STILL_BLOCKED


def test_classify_timeout_is_errored() -> None:
    category, _, reason = rb.classify(rb.ProbeOutcome(None, None, True, "killed at backstop"), blocked_after=True)
    assert category == rb.CAT_ERRORED
    assert "backstop" in reason


def test_classify_unrecognised_verdict_is_errored() -> None:
    category, _, _ = rb.classify(rb.ProbeOutcome(1, None, False, "no verdict line"), blocked_after=True)
    assert category == rb.CAT_ERRORED


# --------------------------------------------------------------------------------------------------
# sweep - end to end with an injected runner, driving REAL marker transitions
# --------------------------------------------------------------------------------------------------
def test_sweep_apply_reports_newly_earned_when_the_probe_lowers_the_gate(tmp_path: Path) -> None:
    unit = _blocked_unit(tmp_path)
    report = rb.sweep([rb.UnitInput(path=unit)], _options(apply=True), runner=_runner_that("DATA_OK", clears=True))
    result = report.results[0]
    assert result.category == rb.CAT_NEWLY_EARNED
    assert not (unit / MARKER).exists()
    assert rb.exit_code(report) == rb.EXIT_CLEAN


def test_sweep_apply_reports_still_blocked_on_no_credential(tmp_path: Path) -> None:
    unit = _blocked_unit(tmp_path)
    report = rb.sweep(
        [rb.UnitInput(path=unit)],
        _options(apply=True),
        runner=_runner_that("NO_CREDENTIAL", clears=False, returncode=1),
    )
    assert report.results[0].category == rb.CAT_STILL_BLOCKED
    assert report.results[0].verdict == "NO_CREDENTIAL"
    assert (unit / MARKER).exists()
    assert rb.exit_code(report) == rb.EXIT_BLOCKED


def test_sweep_apply_surfaces_the_stuck_gate_anomaly(tmp_path: Path) -> None:
    """DATA_OK but the runner left the marker: ground truth wins, so `anomaly`, exit 5."""
    unit = _blocked_unit(tmp_path)
    report = rb.sweep([rb.UnitInput(path=unit)], _options(apply=True), runner=_runner_that("DATA_OK", clears=False))
    assert report.results[0].category == rb.CAT_ANOMALY
    assert rb.exit_code(report) == rb.EXIT_ANOMALY


def test_sweep_dry_run_does_not_call_the_runner(tmp_path: Path) -> None:
    unit = _blocked_unit(tmp_path)
    calls: list[Path] = []

    def spy(probe_unit: Path, _options: rb.SweepOptions) -> rb.ProbeOutcome:
        calls.append(probe_unit)
        return rb.ProbeOutcome(0, "DATA_OK", False, "ok")

    report = rb.sweep([rb.UnitInput(path=unit)], _options(apply=False), runner=spy)
    assert calls == []
    assert report.results[0].category == rb.CAT_WOULD_PROBE
    assert (unit / MARKER).exists()
    assert rb.exit_code(report) == rb.EXIT_BLOCKED


def test_sweep_probes_each_blocked_unit_exactly_once(tmp_path: Path) -> None:
    units = [_blocked_unit(tmp_path, f"u{i}") for i in range(3)]
    calls: list[Path] = []

    def spy(unit: Path, _options: rb.SweepOptions) -> rb.ProbeOutcome:
        calls.append(unit)
        return rb.ProbeOutcome(1, "NO_CREDENTIAL", False, "x")

    rb.sweep([rb.UnitInput(path=u) for u in units], _options(apply=True), runner=spy)
    assert sorted(calls) == sorted(u.resolve() for u in units)


def test_sweep_skips_a_unit_cleared_between_listing_and_probe_time(tmp_path: Path) -> None:
    """A unit selected as blocked but cleared before its turn is skipped, not probed."""
    unit = _blocked_unit(tmp_path)
    (unit / MARKER).unlink()  # cleared after selection would have picked it, before probe
    report = rb.sweep(
        [rb.UnitInput(path=unit, state="BLOCKED")], _options(apply=True), runner=_runner_that("DATA_OK", clears=True)
    )
    # select() re-verifies the marker, so it is skipped before probing.
    assert report.results[0].category == rb.CAT_SKIPPED


# --------------------------------------------------------------------------------------------------
# the ONE hard rule: the sweep never lifts a gate itself
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "verdict,clears,returncode",
    [
        ("NO_CREDENTIAL", False, 1),  # still-blocked path
        ("DATA_OK", True, 0),  # newly-earned path (marker vanishes via the runner, NOT the sweep)
        ("DATA_OK", False, 0),  # anomaly / stuck-gate path
    ],
)
def test_sweep_writes_no_gate_files_of_its_own(tmp_path: Path, verdict: str, clears: bool, returncode: int) -> None:
    """The sweep must never write an override or audit line - on ANY verdict.

    Finding #5: the original test only covered NO_CREDENTIAL, so a mutant that forged
    `.credential-gate-AUTHORIZED` only on DATA_OK survived. Parametrising over both DATA_OK paths
    (earned and stuck) closes that hole - the DATA_OK cases are exactly where injecting an unearned
    override would do the most damage.
    """
    unit = _blocked_unit(tmp_path)
    rb.sweep(
        [rb.UnitInput(path=unit)],
        _options(apply=True),
        runner=_runner_that(verdict, clears=clears, returncode=returncode),
    )
    assert not (unit / OVERRIDE).exists(), f"sweep must never write an override ({verdict})"
    assert not (unit / AUDIT).exists(), f"sweep must never write an audit entry itself ({verdict})"
    if not clears:
        assert (unit / MARKER).exists(), "the sweep must not remove the marker itself"


def test_probe_unit_isolates_a_runner_that_raises_and_the_sweep_continues(tmp_path: Path) -> None:
    """Finding #1 (defense-in-depth): one unit whose probe RAISES must become `errored` without
    discarding the other units' results."""
    boom = _blocked_unit(tmp_path, "boom")
    ok = _blocked_unit(tmp_path, "ok")

    def runner(unit: Path, _options: rb.SweepOptions) -> rb.ProbeOutcome:
        if unit == boom.resolve():
            raise ValueError("kaboom from the probe")
        return rb.ProbeOutcome(1, "NO_CREDENTIAL", False, "x")

    report = rb.sweep([rb.UnitInput(path=boom), rb.UnitInput(path=ok)], _options(apply=True), runner=runner)
    by_unit = {r.unit: r.category for r in report.results}
    assert by_unit[boom.resolve()] == rb.CAT_ERRORED
    assert by_unit[ok.resolve()] == rb.CAT_STILL_BLOCKED


def test_default_probe_runner_builds_a_probe_command_and_never_a_clear_or_authorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin 'never clears itself' at the command-construction level: only the probe is ever spawned.

    ⚠️ Records EVERY spawned command, not just the last one. Blind review 2026-08-27 proved the
    single-value form (`captured["cmd"] = cmd`) could not fail: injecting a `credential_gate.py
    clear --earned` BEFORE the probe left this test green, because the probe's own command
    overwrote the evidence. A guard on the ACL boundary that cannot fail is worse than no guard,
    because it is credited as coverage.
    """
    captured: dict[str, list[list[str]]] = {"cmds": []}

    class _Proc:
        returncode = 1
        stdout = "PROBE: NO_CREDENTIAL needs sign-in\n"
        stderr = ""

    def fake_run(cmd, **_kwargs):
        captured["cmds"].append(list(cmd))
        return _Proc()

    monkeypatch.setattr(rb.subprocess, "run", fake_run)
    outcome = rb._default_probe_runner(
        Path("/x/unit"), rb.SweepOptions(apply=True, refresh_timeout_sec=None, per_unit_timeout_sec=0)
    )
    assert len(captured["cmds"]) == 1, f"expected exactly one spawned command, got {captured['cmds']}"
    cmd = " ".join(captured["cmds"][0])
    assert "probe_live_source.py" in cmd
    assert "--bundle" in cmd
    for spawned in captured["cmds"]:
        joined = " ".join(spawned)
        assert "credential_gate.py" not in joined, f"the sweep must never spawn the gate: {joined}"
        assert "clear" not in spawned, f"the sweep must never clear: {joined}"
        assert "authorize" not in spawned, f"the sweep must never authorize: {joined}"
    assert " clear" not in cmd and "authorize" not in cmd
    assert outcome.verdict == "NO_CREDENTIAL"


def test_default_probe_runner_passes_refresh_timeout_only_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, list[str]] = {}

    class _Proc:
        returncode = 0
        stdout = "PROBE: DATA_OK ok\n"
        stderr = ""

    def fake_run(cmd, **_kwargs):
        seen.setdefault("cmds", []).append(cmd)
        return _Proc()

    monkeypatch.setattr(rb.subprocess, "run", fake_run)
    rb._default_probe_runner(Path("/x"), rb.SweepOptions(True, None, 0))
    rb._default_probe_runner(Path("/x"), rb.SweepOptions(True, 123, 0))
    assert "--refresh-timeout-sec" not in seen["cmds"][0]
    assert "--refresh-timeout-sec" in seen["cmds"][1] and "123" in seen["cmds"][1]


def test_default_probe_runner_reports_timeout_as_errored(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **_kwargs):
        raise rb.subprocess.TimeoutExpired(cmd=cmd, timeout=1.0)

    monkeypatch.setattr(rb.subprocess, "run", fake_run)
    outcome = rb._default_probe_runner(Path("/x"), rb.SweepOptions(True, None, 5))
    assert outcome.timed_out is True
    assert outcome.returncode is None


# --------------------------------------------------------------------------------------------------
# exit_code precedence
# --------------------------------------------------------------------------------------------------
def _report(categories: list[str], forged: bool = False) -> rb.SweepReport:
    results = [rb.UnitResult(Path(f"/u{i}"), category, None, "r", 0.0) for i, category in enumerate(categories)]
    return rb.SweepReport(results=results, forged=forged, applied=True)


def test_exit_code_forged_outranks_everything() -> None:
    assert rb.exit_code(_report([rb.CAT_NEWLY_EARNED, rb.CAT_ANOMALY], forged=True)) == rb.EXIT_FORGED


def test_exit_code_anomaly_and_errored_outrank_still_blocked() -> None:
    assert rb.exit_code(_report([rb.CAT_STILL_BLOCKED, rb.CAT_ANOMALY])) == rb.EXIT_ANOMALY
    assert rb.exit_code(_report([rb.CAT_STILL_BLOCKED, rb.CAT_ERRORED])) == rb.EXIT_ANOMALY


def test_exit_code_still_blocked_and_would_probe_are_exit_one() -> None:
    assert rb.exit_code(_report([rb.CAT_NEWLY_EARNED, rb.CAT_STILL_BLOCKED])) == rb.EXIT_BLOCKED
    assert rb.exit_code(_report([rb.CAT_WOULD_PROBE])) == rb.EXIT_BLOCKED


def test_exit_code_all_earned_is_clean() -> None:
    assert rb.exit_code(_report([rb.CAT_NEWLY_EARNED, rb.CAT_NEWLY_EARNED])) == rb.EXIT_CLEAN
    assert rb.exit_code(_report([])) == rb.EXIT_CLEAN


# --------------------------------------------------------------------------------------------------
# main - CLI surface (dry-run and injected-runner apply)
# --------------------------------------------------------------------------------------------------
def test_main_usage_error_when_no_units(capsys: pytest.CaptureFixture[str]) -> None:
    assert rb.main(["--json"]) == rb.EXIT_USAGE


def test_main_dry_run_reports_blocked_and_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    unit = _blocked_unit(tmp_path)
    code = rb.main(["--unit", str(unit), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == rb.EXIT_BLOCKED
    assert payload["applied"] is False
    assert payload["summary"].get("would-probe") == 1


def test_main_apply_with_injected_runner_earns_a_clear(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    unit = _blocked_unit(tmp_path)
    code = rb.main(["--unit", str(unit), "--apply", "--json"], runner=_runner_that("DATA_OK", clears=True))
    payload = json.loads(capsys.readouterr().out)
    assert code == rb.EXIT_CLEAN
    assert payload["summary"].get("newly-earned") == 1
    assert not (unit / MARKER).exists()


def test_main_apply_surfaces_anomaly_exit_five(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    unit = _blocked_unit(tmp_path)
    code = rb.main(["--unit", str(unit), "--apply"], runner=_runner_that("DATA_OK", clears=False))
    assert code == rb.EXIT_ANOMALY


def test_main_nonexistent_unit_exits_five_not_zero(tmp_path: Path) -> None:
    """Finding #3: a typo'd --unit must NOT silently exit 0 as if the estate were clean."""
    code = rb.main(["--unit", str(tmp_path / "typo")])
    assert code == rb.EXIT_ANOMALY


def test_main_empty_but_provided_source_is_clean_exit_zero(tmp_path: Path) -> None:
    """Finding #3: `list --json` of a fully un-gated estate is `{"units":[]}` - a CLEAN result (exit
    0), which must not collapse into the no-source usage error (exit 2)."""
    empty = tmp_path / "empty.json"
    empty.write_text('{"root":"r","units":[]}', encoding="utf-8")
    assert rb.main(["--units-from", str(empty)]) == rb.EXIT_CLEAN


def test_main_truncated_json_source_exits_five_not_zero(tmp_path: Path) -> None:
    truncated = tmp_path / "truncated.json"
    truncated.write_text('{"root":"r","units":[{"unit":"/a/one",', encoding="utf-8")
    assert rb.main(["--units-from", str(truncated)]) == rb.EXIT_ANOMALY


def test_main_missing_units_from_file_exits_five_not_crash(tmp_path: Path) -> None:
    assert rb.main(["--units-from", str(tmp_path / "no-such-file.txt")]) == rb.EXIT_ANOMALY


def test_negative_per_unit_timeout_is_rejected(tmp_path: Path) -> None:
    """Finding #4: a fat-fingered negative must be rejected, not silently disable the backstop."""
    with pytest.raises(SystemExit):
        rb.main(["--unit", str(_blocked_unit(tmp_path)), "--per-unit-timeout-sec", "-5"])


def test_zero_per_unit_timeout_is_accepted(tmp_path: Path) -> None:
    args = rb._parse_args(["--unit", str(tmp_path), "--per-unit-timeout-sec", "0"])
    assert args.per_unit_timeout_sec == 0


def test_lowercase_blocked_state_is_normalised_and_still_probed(tmp_path: Path) -> None:
    """A `list` state differing only in CASE must not fall through to the not-blocked branch."""
    unit = _blocked_unit(tmp_path)
    probe_it, pre, forged = rb._classify_selection(rb.UnitInput(path=unit, state="blocked"), unit)
    assert probe_it is True, "a lowercase 'blocked' must normalise to BLOCKED and be probed"
    assert pre is None
    assert forged is False


def test_non_blocked_state_never_silently_skips_a_unit_whose_marker_is_present(tmp_path: Path) -> None:
    """Ground truth outranks a caller-supplied state; the disagreement is loud, not a skip."""
    unit = _blocked_unit(tmp_path)
    probe_it, pre, _ = rb._classify_selection(rb.UnitInput(path=unit, state="clean"), unit)
    assert probe_it is False
    assert pre is not None
    assert pre.category == rb.CAT_ANOMALY, f"expected anomaly, got {pre.category}"
    assert "marker is present NOW" in pre.reason


def test_a_stale_non_blocked_state_still_skips_when_the_marker_is_gone(tmp_path: Path) -> None:
    """The anomaly fires on DISAGREEMENT only - an ordinary cleared unit stays a cheap skip."""
    unit = tmp_path / "cleared"
    unit.mkdir()
    probe_it, pre, _ = rb._classify_selection(rb.UnitInput(path=unit, state="clean"), unit)
    assert probe_it is False
    assert pre is not None and pre.category == rb.CAT_SKIPPED


def test_forged_state_is_matched_case_insensitively(tmp_path: Path) -> None:
    """The security signal must not be lost to casing either."""
    unit = _blocked_unit(tmp_path)
    _, pre, forged = rb._classify_selection(rb.UnitInput(path=unit, state="forged-override"), unit)
    assert forged is True
    assert pre is not None and pre.category == rb.CAT_SKIPPED


def test_stdin_is_never_read_implicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """No source + no --stdin is a usage error, NOT a blocking read on an open pipe."""

    class _NeverRead:
        @staticmethod
        def isatty() -> bool:
            return False

        @staticmethod
        def read() -> str:  # pragma: no cover - reaching this IS the failure
            raise AssertionError("gather_units must not read stdin unless --stdin was passed")

    monkeypatch.setattr(rb.sys, "stdin", _NeverRead())
    args = rb._parse_args([])
    units, provided = rb.gather_units(args)
    assert units == []
    assert provided is False
