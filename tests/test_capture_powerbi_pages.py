"""Tests for the stable Power BI page capture helper."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede this import)
import capture_powerbi_pages as capture
from probe_desktop_query import DesktopIdentity
from refresh_pbip_model import ImageObservation
from test_iteration_receipt import (
    PAGE,
    PID,
    _code,
    _finalize,
    _iterate,
    _options,
    _path,
    _runtime,
    build_package,
    receipt,
)


class ManualClock:
    """Tiny controllable clock for convergence tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _workspace(name: str) -> Path:
    root = REPO_ROOT / ".test-work" / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root


def test_capture_stable_ignores_a_partial_plateau_until_final_frame_dwells() -> None:
    """One unchanged poll can be a partial-render plateau, not convergence."""
    out_dir = _workspace("capture-converges")
    dest = out_dir / "Map.png"
    frames = [
        b"partial-west",
        b"partial-west",
        b"complete-nationwide",
        b"complete-nationwide",
        b"complete-nationwide",
    ]
    clock = ManualClock()

    def fake_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        frame.write_bytes(frames.pop(0))
        return True

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=1.0, stable_seconds=2.0, max_wait=10.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert result.captured
        assert result.converged
        assert result.frames == 5
        assert dest.read_bytes() == b"complete-nationwide"
        assert not (out_dir / ".Map.frames").exists()
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_stable_does_not_count_screenshot_duration_as_dwell() -> None:
    """A slow screenshot call must not turn one unchanged poll into convergence."""
    out_dir = _workspace("capture-slow-screenshot")
    dest = out_dir / "Map.png"
    frames = [
        b"partial-west",
        b"partial-west",
        b"complete-nationwide",
        b"complete-nationwide",
        b"complete-nationwide",
    ]
    clock = ManualClock()

    def fake_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        if len(frames) == 5:
            clock.sleep(5.0)
        frame.write_bytes(frames.pop(0))
        return True

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=4.0, stable_seconds=8.0, max_wait=30.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert result.captured
        assert result.converged
        assert result.frames == 5
        assert dest.read_bytes() == b"complete-nationwide"
        assert not (out_dir / ".Map.frames").exists()
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_stable_flags_newest_frame_when_page_never_converges() -> None:
    """A page still changing at max-wait is kept for inspection but fails the gate."""
    out_dir = _workspace("capture-unstable")
    dest = out_dir / "Map.png"
    frame_number = 0
    clock = ManualClock()

    def fake_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        nonlocal frame_number
        frame.write_bytes(f"frame-{frame_number}".encode("utf-8"))
        frame_number += 1
        return True

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=1.0, stable_seconds=2.0, max_wait=2.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert result.captured
        assert not result.converged
        assert result.frames == 2
        assert dest.read_bytes() == b"frame-1"
        assert not (out_dir / ".Map.frames").exists()
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_stable_discards_partial_frames_after_a_later_failure() -> None:
    """A failed page never promotes a plausible partial frame to the evidence path."""
    out_dir = _workspace("capture-later-failure")
    dest = out_dir / "Map.png"
    calls = 0

    def flaky_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            frame.write_bytes(f"PARTIAL-FRAME-{calls}".encode("utf-8"))
            return True
        return False

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=0.0, stable_seconds=20.0, max_wait=75.0),
            capture.CaptureRuntime(flaky_screenshot, lambda _seconds: None, lambda: 0.0),
        )

        assert result == capture.CaptureResult(captured=False, converged=False, seconds=0.0, frames=3)
        assert not dest.exists()
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_stable_discards_a_partial_file_from_the_first_failed_screenshot() -> None:
    """A bridge error after writing a truncated image cannot leave output evidence."""
    out_dir = _workspace("capture-first-failure")
    dest = out_dir / "Map.png"

    def partial_then_fail(_page_id: str, _pid: str, frame: Path) -> bool:
        frame.write_bytes(b"TRUNCATED-FRAME")
        return False

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=0.0, stable_seconds=20.0, max_wait=75.0),
            capture.CaptureRuntime(partial_then_fail, lambda _seconds: None, lambda: 0.0),
        )

        assert result == capture.CaptureResult(captured=False, converged=False, seconds=0.0, frames=1)
        assert not dest.exists()
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_stable_preserves_prior_evidence_when_a_new_capture_fails() -> None:
    """A failed recapture cannot delete a previously settled output PNG."""
    out_dir = _workspace("capture-preserves-prior-evidence")
    dest = out_dir / "Map.png"
    dest.write_bytes(b"PREVIOUSLY-SETTLED")

    def partial_then_fail(_page_id: str, _pid: str, frame: Path) -> bool:
        frame.write_bytes(b"TRUNCATED-FRAME")
        return False

    try:
        result = capture.capture_stable(
            "ReportSection1",
            "1234",
            dest,
            capture.CaptureOptions(poll=0.0, stable_seconds=20.0, max_wait=75.0),
            capture.CaptureRuntime(partial_then_fail, lambda _seconds: None, lambda: 0.0),
        )

        assert not result.captured
        assert dest.read_bytes() == b"PREVIOUSLY-SETTLED"
        assert not (out_dir / ".Map.capturing.png").exists()
    finally:
        shutil.rmtree(out_dir.parent, ignore_errors=True)


def test_capture_report_exits_nonzero_when_any_page_is_unstable() -> None:
    """The process-level gate must fail when a page capture never converges."""
    root = _workspace("capture-report-unstable")
    report = root / "Book.Report"
    page = report / "definition" / "pages" / "ReportSection1"
    page.mkdir(parents=True)
    (page / "page.json").write_text('{"displayName": "Map"}', encoding="utf-8")
    out_dir = root / "out"
    clock = ManualClock()
    frame_number = 0

    def fake_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
        nonlocal frame_number
        frame.write_bytes(f"frame-{frame_number}".encode("utf-8"))
        frame_number += 1
        return True

    try:
        code = capture.capture_report(
            report,
            out_dir,
            "1234",
            capture.CaptureOptions(poll=1.0, stable_seconds=2.0, max_wait=2.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert code == 1
        assert (out_dir / "Map.png").read_bytes() == b"frame-1"
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_capture_report_limits_capture_to_requested_page_ids() -> None:
    """The --pages selection captures only the exact PBIR page folder IDs."""
    root = _workspace("capture-report-selected-page")
    report = root / "Book.Report"
    for page_id, display_name in (
        ("ReportSectionOverview", "Overview"),
        ("ReportSectionMap", "Map"),
    ):
        page = report / "definition" / "pages" / page_id
        page.mkdir(parents=True)
        (page / "page.json").write_text(f'{{"displayName": "{display_name}"}}', encoding="utf-8")
    captured_page_ids: list[str] = []
    clock = ManualClock()

    def fake_screenshot(page_id: str, _pid: str, frame: Path) -> bool:
        captured_page_ids.append(page_id)
        frame.write_bytes(b"settled")
        return True

    try:
        code = capture.capture_report(
            report,
            root / "out",
            "1234",
            capture.CaptureOptions(
                poll=1.0,
                stable_seconds=0.0,
                max_wait=2.0,
                page_ids=frozenset({"ReportSectionMap"}),
            ),
            capture.CaptureRuntime(
                screenshotter=fake_screenshot,
                sleep=clock.sleep,
                clock=clock,
            ),
        )

        assert code == 0
        assert captured_page_ids == ["ReportSectionMap", "ReportSectionMap"]
        assert (root / "out" / "Map.png").read_bytes() == b"settled"
        assert not (root / "out" / "Overview.png").exists()
        assert not list((root / "out").glob(".*.frames"))
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_parse_args_accepts_comma_separated_page_ids() -> None:
    """The CLI exposes the exact page-ID filter to callers."""
    args = capture.parse_args(
        [
            "Book.Report",
            "out",
            "--pid",
            "1234",
            "--pages",
            "ReportSectionOverview, ReportSectionMap",
        ]
    )

    assert args.pages == frozenset({"ReportSectionOverview", "ReportSectionMap"})


def test_capture_report_rejects_unknown_page_id_before_capturing(capsys: pytest.CaptureFixture[str]) -> None:
    """A misspelled PBIR page ID is an argument error, never a silent empty success."""
    root = _workspace("capture-report-unknown-page")
    report = root / "Book.Report"
    page = report / "definition" / "pages" / "ReportSectionMap"
    page.mkdir(parents=True)
    (page / "page.json").write_text('{"displayName": "Map"}', encoding="utf-8")
    screenshot_called = False

    def fake_screenshot(_page_id: str, _pid: str, _frame: Path) -> bool:
        nonlocal screenshot_called
        screenshot_called = True
        return True

    try:
        code = capture.capture_report(
            report,
            root / "out",
            "1234",
            capture.CaptureOptions(
                poll=1.0,
                stable_seconds=0.0,
                max_wait=2.0,
                page_ids=frozenset({"Map"}),
            ),
            capture.CaptureRuntime(
                screenshotter=fake_screenshot,
                sleep=ManualClock().sleep,
                clock=ManualClock(),
            ),
        )

        assert code == 2
        assert not screenshot_called
        assert "requested page id(s) not found: Map" in capsys.readouterr().out
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_capture_report_full_sweep_captures_every_page() -> None:
    """Omitting --pages retains the ordinary full-sweep page traversal."""
    root = _workspace("capture-report-full-sweep")
    report = root / "Book.Report"
    for page_id, display_name in (
        ("ReportSectionOverview", "Overview"),
        ("ReportSectionMap", "Map"),
    ):
        page = report / "definition" / "pages" / page_id
        page.mkdir(parents=True)
        (page / "page.json").write_text(f'{{"displayName": "{display_name}"}}', encoding="utf-8")
    captured_page_ids: list[str] = []
    clock = ManualClock()

    def fake_screenshot(page_id: str, _pid: str, frame: Path) -> bool:
        captured_page_ids.append(page_id)
        frame.write_bytes(page_id.encode("utf-8"))
        return True

    try:
        code = capture.capture_report(
            report,
            root / "out",
            "1234",
            capture.CaptureOptions(poll=1.0, stable_seconds=0.0, max_wait=2.0),
            capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
        )

        assert code == 0
        assert captured_page_ids == [
            "ReportSectionMap",
            "ReportSectionMap",
            "ReportSectionOverview",
            "ReportSectionOverview",
        ]
        assert (root / "out" / "Map.png").read_bytes() == b"ReportSectionMap"
        assert (root / "out" / "Overview.png").read_bytes() == b"ReportSectionOverview"
        assert not list((root / "out").glob(".*.frames"))
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


def test_screenshot_timeout_treats_a_hung_bridge_call_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Python timeout caps a bridge subprocess that does not return itself."""
    observed_timeout: int | None = None

    def fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal observed_timeout
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, int)
        observed_timeout = timeout
        raise subprocess.TimeoutExpired("powerbi-desktop", timeout)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)

    assert not capture.screenshot("ReportSectionMap", "1234", Path("unused.png"))
    assert observed_timeout == capture.SCREENSHOT_TIMEOUT_SECONDS
    assert observed_timeout > capture.BRIDGE_WAIT_SECONDS


def test_capture_report_exits_nonzero_when_report_has_no_pages() -> None:
    """A typo or invalid .Report path must fail closed, not report 0/0 success."""
    root = _workspace("capture-report-empty")
    report = root / "Missing.Report"

    try:
        code = capture.capture_report(
            report,
            root / "out",
            "1234",
            capture.CaptureOptions(poll=1.0, stable_seconds=2.0, max_wait=2.0),
        )

        assert code == 1
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


@pytest.fixture(name="a1")
def a1_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Direct A1 returns from distinct calls, not a supplied evidence document or native proof."""
    package = build_package(tmp_path)
    bound = capture.BoundDesktop(
        DesktopIdentity(PID, "100", PID + 1, "101", 55001), "11111111-2222-3333-4444-555555555555"
    )
    events, returned = [], {}

    def bind(pid: int) -> capture.BoundDesktop:
        assert pid == PID
        events.append("bind")
        return bound

    def recheck(held: capture.BoundDesktop, operation) -> None:
        assert held is bound
        events.append("recheck")
        assert operation(object()) is None

    def refresh(port: int, tables: object, **kwargs: object) -> capture.RefreshObservation:
        assert port == 55001 and tables is None
        assert kwargs == {"refresh_type": "full", "desktop_pid": PID, "bound": bound, "return_observation": True}
        assert kwargs["bound"] is bound
        events.append("refresh")
        result = capture.RefreshObservation(bound.catalogue, "full", "database", (), bound.identity)
        returned["refresh"] = result
        return result

    def probe(held: capture.BoundDesktop, canaries: list[str]) -> tuple:
        assert held is bound and canaries == ["Sales", "Returns"]
        events.append("canaries")
        result = tuple(
            capture.CanaryObservation(bound.catalogue, table, f"EVALUATE TOPN(1, '{table}')", 1, bound.identity)
            for table in canaries
        )
        returned["canaries"] = result
        return result

    def persist(port: int, cache: Path, model: Path, **kwargs: object) -> capture.PersistenceObservation:
        assert port == 55001
        assert model == package / "fabric" / "Unit.SemanticModel" and cache == model / ".pbi" / "cache.abf"
        assert kwargs == {"bound": bound, "return_observation": True} and kwargs["bound"] is bound
        events.append("persistence")
        blob = b"synthetic invocation-owned cache bytes, not native ABF evidence"
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(blob)
        (model / "definition" / "database.tmdl").write_bytes(b"database\n\tcompatibilityLevel: 1604\n")
        digest = hashlib.sha256(blob).hexdigest()
        result = capture.PersistenceObservation(
            bound.catalogue, 1604, ImageObservation(digest, len(blob), digest, len(blob)), bound.identity
        )
        returned["persistence"] = result
        return result

    for name, operation in (
        ("bind_desktop", bind),
        ("bound_call", recheck),
        ("refresh", refresh),
        ("probe_observations", probe),
        ("image_save", persist),
    ):
        monkeypatch.setattr(capture, name, operation)
    return package, bound, events, returned


def _prepared_iteration(package: Path, **kwargs: object) -> dict:
    return _iterate(package, refresh=True, persist=True, canaries=("Sales", "Returns"), **kwargs)


def test_a1_calls_share_one_held_binding_and_freeze_after_preparation(a1: tuple) -> None:
    package, bound, events, returned = a1
    runtime = _runtime(package)

    def shot(page: str, pid: str, dest: Path) -> bool:
        assert (package / "fabric" / "Unit.SemanticModel" / ".pbi" / "cache.abf").is_file()
        events.append(f"shot:{page}")
        return runtime.screenshotter(page, pid, dest)

    pending = _prepared_iteration(package, runtime=replace(runtime, screenshotter=shot))
    assert events == [
        "bind",
        "refresh",
        "recheck",
        "canaries",
        "recheck",
        "persistence",
        "recheck",
        *[f"shot:{PAGE}"] * 3,
        *["shot:page-trend"] * 3,
        "recheck",
    ]
    assert returned["refresh"] is not returned["persistence"]
    assert returned["canaries"][0] is not returned["canaries"][1]
    generated = pending["generated"]
    assert generated["preparation"]["requested"] == {"refresh": True, "persist": True, "canaries": ["Sales", "Returns"]}
    before, after = generated["preparation"]["artifact_before"], generated["artifact"]
    assert before["cache_sha256"] is None and before["model_revision"] != after["model_revision"]
    cache = package / "fabric" / "Unit.SemanticModel" / ".pbi" / "cache.abf"
    assert after["cache_sha256"] == hashlib.sha256(cache.read_bytes()).hexdigest()
    assert after["cache_byte_count"] == len(cache.read_bytes())
    facts = generated["data_evidence"]
    assert all(fact["status"] == "observed" for fact in facts.values())
    assert facts["binding"]["observation"]["identity"] == {
        "pid": PID,
        "process_start": "100",
        "as_pid": PID + 1,
        "as_process_start": "101",
        "port": 55001,
    }
    assert facts["refresh"]["observation"]["scope"] == "database"
    assert facts["refresh"]["observation"]["refresh_type"] == "full"
    assert facts["persistence"]["observation"]["image"]["commitment"] == "UNESTABLISHED"
    assert facts["persistence"]["observation"]["catalogue"] == bound.catalogue
    for row, name in zip(facts["canaries"]["observation"], ["Sales", "Returns"]):
        expected_query = f"EVALUATE TOPN(1, '{name}')"
        assert (
            row["query"] == expected_query
            and row["query_sha256"] == hashlib.sha256(expected_query.encode()).hexdigest()
        )
        assert row["returned_rows"] == 1
    final = _finalize(package, pending)
    assert final["generated"] == generated and final["state"] == "final" and "outcome" not in final


@pytest.mark.parametrize("rows", [0, 1])
def test_a1_zero_and_positive_returned_rows_are_both_observed(
    a1: tuple, monkeypatch: pytest.MonkeyPatch, rows: int
) -> None:
    package, _, _, _ = a1
    probe = capture.probe_observations
    monkeypatch.setattr(
        capture, "probe_observations", lambda *args: tuple(replace(row, returned_rows=rows) for row in probe(*args))
    )
    final = _finalize(package, _prepared_iteration(package))
    fact = final["generated"]["data_evidence"]["canaries"]
    assert fact["status"] == "observed" and [row["returned_rows"] for row in fact["observation"]] == [rows, rows]
    assert final["state"] == "final" and "outcome" not in final


@pytest.mark.parametrize(
    "operation,field", [("refresh", "refresh"), ("probe_observations", "canaries"), ("image_save", "persistence")]
)
@pytest.mark.parametrize("result", ["refused", "missing"])
def test_a1_unsuccessful_measurements_can_seal_with_valid_authority(
    a1: tuple, monkeypatch: pytest.MonkeyPatch, operation: str, field: str, result: str
) -> None:
    package, _, events, _ = a1

    def unavailable(*_args: object, **_kwargs: object) -> None:
        events.append(result)
        if result == "refused":
            raise capture.ObservationUnavailable("TOOL_UNAVAILABLE")

    monkeypatch.setattr(capture, operation, unavailable)
    pending = _prepared_iteration(package)
    assert events[events.index(result) + 1] == "recheck"
    final = _finalize(package, pending)
    assert final["generated"]["data_evidence"][field] == {
        "status": "refused" if result == "refused" else "unestablished",
        "reason": "TOOL_UNAVAILABLE" if result == "refused" else "observation_unavailable",
        "observation": None,
    }
    assert final["state"] == "final" and "outcome" not in final


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", PID + 2),
        ("process_start", "99"),
        ("as_pid", PID + 3),
        ("as_process_start", "102"),
        ("port", 55002),
        ("catalogue", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    ],
)
def test_a1_return_from_another_binding_refuses_publication(
    a1: tuple, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    package, _, _, _ = a1
    refresh = capture.refresh

    def swapped(*args: object, **kwargs: object) -> capture.RefreshObservation:
        result = refresh(*args, **kwargs)
        return (
            replace(result, catalogue=value)
            if field == "catalogue"
            else replace(result, identity=replace(result.identity, **{field: value}))
        )

    monkeypatch.setattr(capture, "refresh", swapped)
    assert _code(lambda: _prepared_iteration(package)) == "A1_BINDING_MISMATCH"
    assert not _path(package).exists()


@pytest.mark.parametrize("moment", ["bind", "refresh", "refusal-recheck", "capture-recheck"])
def test_a1_authority_failure_is_not_retained_as_measurement_refusal(
    a1: tuple, monkeypatch: pytest.MonkeyPatch, moment: str
) -> None:
    package, _, _, _ = a1

    def broken(*_args: object, **_kwargs: object) -> None:
        raise capture.ObservationUnavailable("PID_REUSED")

    if moment in {"bind", "refresh"}:
        monkeypatch.setattr(capture, "bind_desktop" if moment == "bind" else "refresh", broken)
    else:
        recheck, calls = capture.bound_call, []

        def lose_binding(*args: object) -> None:
            calls.append(True)
            if len(calls) == (1 if moment == "refusal-recheck" else 4):
                broken()
            recheck(*args)

        monkeypatch.setattr(capture, "bound_call", lose_binding)
        if moment == "refusal-recheck":

            def refuse(*_args: object, **_kwargs: object) -> None:
                raise capture.ObservationUnavailable("TOOL_UNAVAILABLE")

            monkeypatch.setattr(capture, "refresh", refuse)
    assert _code(lambda: _prepared_iteration(package)) == "A1_BINDING_UNESTABLISHED"
    assert not _path(package).parent.exists()


@pytest.mark.parametrize("field", ["intended_sha256", "installed_sha256", "intended_size", "installed_size"])
def test_a1_persistence_readback_must_agree_with_actual_cache(
    a1: tuple, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    package, _, _, _ = a1
    persist = capture.image_save

    def mismatched(*args: object, **kwargs: object) -> capture.PersistenceObservation:
        result = persist(*args, **kwargs)
        value = "f" * 64 if field.endswith("sha256") else result.image.installed_size + 1
        return replace(result, image=replace(result.image, **{field: value}))

    monkeypatch.setattr(capture, "image_save", mismatched)
    assert _code(lambda: _prepared_iteration(package)) == "A1_CACHE_MISMATCH"
    assert not _path(package).parent.exists()


def test_a1_retained_facts_are_never_recollected_by_readers_or_finalization(
    a1: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    package, _, _, _ = a1
    pending = _prepared_iteration(package)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("native preparation or observation replay is forbidden")

    for name in ("bind_desktop", "bound_call", "refresh", "probe_observations", "image_save"):
        monkeypatch.setattr(capture, name, forbidden)
    final = _finalize(package, pending)
    assert receipt.read_history(package)[-1].payload == final
    assert receipt.read_chain(package, receipt.receipt_sha256(final))[-1].payload == final


def test_no_requested_a1_operations_performs_no_native_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = build_package(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an unrequested A1 operation was executed")

    for name in ("bind_desktop", "bound_call", "refresh", "probe_observations", "image_save"):
        monkeypatch.setattr(capture, name, forbidden)
    final = _finalize(package, _iterate(package, options=_options(page_ids=frozenset({PAGE}))))
    assert [page["page_id"] for page in final["generated"]["numeric_evidence"]] == [PAGE, "page-trend"]
    assert [[row["visual_id"] for row in page["visuals"]] for page in final["generated"]["numeric_evidence"]] == [
        ["v-1", "v-2"],
        ["v-3"],
    ]
    assert all(page["whole_page"]["status"] == "unestablished" for page in final["generated"]["numeric_evidence"])
    assert final["generated"]["retained_roles"] == {
        "receipt_json": {"count": 1},
        "powerbi_png": {"count": 1},
        "certified_tableau_csv": {
            "count": 0,
            "reason": "certified CSV operands are not collected by this receipt version",
        },
        "typed_dax_envelope": {"count": 0, "reason": "typed DAX envelopes are not collected by this receipt version"},
    }


@pytest.mark.parametrize("value", [True, {"status": "observed"}, (True, "legacy success")])
def test_supplied_or_legacy_a1_results_cannot_become_observations(
    a1: tuple, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    package, _, _, _ = a1
    monkeypatch.setattr(capture, "refresh", lambda *_args, **_kwargs: value)
    assert _code(lambda: _prepared_iteration(package)) == "A1_OBSERVATION_INVALID"
    assert not _path(package).exists()


@pytest.mark.parametrize(
    "field,value", [("refresh", 1), ("persist", "true"), ("canaries", ("Sales", "sales")), ("canaries", (" ",))]
)
def test_bad_a1_request_is_rejected_before_any_native_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    package = build_package(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid request reached binding")

    monkeypatch.setattr(capture, "bind_desktop", forbidden)
    expected = "CANARIES_REQUIRED" if field == "canaries" else "SCHEMA"
    assert _code(lambda: _iterate(package, **{field: value})) == expected
    assert not _path(package).exists()


@pytest.mark.parametrize("field", ["numeric_obligation", "data_evidence", "numeric_results", "retained_roles"])
def test_request_has_no_caller_supplied_evidence_fields(field: str) -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        capture.IterationRequest(Path("unit"), str(PID), **{field: "successful"})


def test_cli_exposes_only_explicit_a1_preparation() -> None:
    args = capture.parse_args(
        [
            "iterate",
            "--package",
            "unit",
            "--pid",
            str(PID),
            "--refresh",
            "--persist",
            "--canary-table",
            "Sales",
            "--canary-table",
            "Returns",
        ]
    )
    assert args.refresh is True and args.persist is True and args.canary_table == ["Sales", "Returns"]


def test_bad_predecessor_pin_refuses_before_a1_preparation(a1: tuple) -> None:
    package, _, events, _ = a1
    request = capture.IterationRequest(
        package, str(PID), previous_sha256="f" * 64, refresh=True, persist=True, canaries=("Sales", "Returns")
    )
    assert _code(lambda: capture.run_iteration(request, _options(), _runtime(package))) == "PREVIOUS_RECEIPT_MISMATCH"
    assert not events and not _path(package).parent.exists()


@pytest.mark.parametrize("kind", ["report", "cache"])
def test_changes_after_a1_preparation_refuse_the_frozen_snapshot(a1: tuple, kind: str) -> None:
    package, _, _, _ = a1
    runtime = _runtime(package)
    changes = []

    def shot(page: str, pid: str, dest: Path) -> bool:
        if not changes:
            if kind == "report":
                target = package / "fabric" / "Unit.Report" / "definition" / "report.json"
            else:
                target = package / "fabric" / "Unit.SemanticModel" / ".pbi" / "cache.abf"
            target.write_bytes(b"{}")
            changes.append(kind)
        return runtime.screenshotter(page, pid, dest)

    assert (
        _code(lambda: _prepared_iteration(package, runtime=replace(runtime, screenshotter=shot))) == "GENERATED_CHANGED"
    )
    assert changes == [kind] and not _path(package).parent.exists()


@pytest.mark.parametrize("change", ["scope", "refresh_type", "canary_set"])
def test_a1_return_must_describe_the_requested_operations(
    a1: tuple, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    package, _, _, _ = a1
    refresh, probe = capture.refresh, capture.probe_observations
    if change == "canary_set":
        monkeypatch.setattr(capture, "probe_observations", lambda *args: probe(*args)[:1])
    else:
        value = "tables" if change == "scope" else "calculate"
        monkeypatch.setattr(
            capture, "refresh", lambda *args, **kwargs: replace(refresh(*args, **kwargs), **{change: value})
        )
    assert _code(lambda: _prepared_iteration(package)) == ("A1_CANARY_SET" if change == "canary_set" else "SCHEMA")
    assert not _path(package).parent.exists()
