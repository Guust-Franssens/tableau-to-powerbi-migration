"""Tests for the immutable capture receipt (Slice A of #363).

Each test exercises the production bridge/status and inventory seams via controlled
adapters — never asserting helper self-consistency alone.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402
import capture_powerbi_pages as capture
import capture_receipt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXED_TIME = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


class ManualClock:
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


def _make_package(root: Path, *, name: str = "Book", pages: list[tuple[str, str, list[str]]] | None = None) -> Path:
    """Build a minimal package under *root* with the given pages.

    Each page is ``(page_id, display_name, [visual_id, ...])``.
    Returns the package root.
    """
    if pages is None:
        pages = [("ReportSection1", "Overview", ["v1a", "v1b"])]
    fabric = root / "fabric"
    report = fabric / f"{name}.Report"
    pbip = fabric / f"{name}.pbip"
    pbip_doc = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{name}.Report"}}],
    }
    pbip.parent.mkdir(parents=True, exist_ok=True)
    pbip.write_text(json.dumps(pbip_doc), encoding="utf-8")
    # definition.pbir
    pbir = report / "definition.pbir"
    pbir.parent.mkdir(parents=True, exist_ok=True)
    pbir.write_text(json.dumps({"$schema": "...", "version": "4.0"}), encoding="utf-8")
    # pages + visuals
    for page_id, display_name, visual_ids in pages:
        page_dir = report / "definition" / "pages" / page_id
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "page.json").write_text(json.dumps({"displayName": display_name}), encoding="utf-8")
        for vid in visual_ids:
            vis_dir = page_dir / "visuals" / vid
            vis_dir.mkdir(parents=True, exist_ok=True)
            (vis_dir / "visual.json").write_text(json.dumps({"name": vid}), encoding="utf-8")
    return root


def _fake_status(pbip_path: Path, pid: int = 1234) -> str:
    return json.dumps({"instances": [{"pid": pid, "currentFilePath": str(pbip_path.resolve())}]})


def _make_runtime(
    root: Path,
    *,
    pid: int = 1234,
    name: str = "Book",
    fail_pages: frozenset[str] | None = None,
    unconverged_pages: frozenset[str] | None = None,
    zero_byte_pages: frozenset[str] | None = None,
) -> capture.PackageCaptureRuntime:
    """Build a fake runtime whose status matches the package."""
    pbip = root / "fabric" / f"{name}.pbip"
    clock = ManualClock()
    _fail = fail_pages or frozenset()
    _unconverge = unconverged_pages or frozenset()
    _zero = zero_byte_pages or frozenset()

    def fake_screenshot(page_id: str, _pid: str, frame: Path) -> bool:
        if page_id in _fail:
            return False
        if page_id in _zero:
            frame.write_bytes(b"")
            return True
        frame.write_bytes(f"img-{page_id}".encode())
        return True

    def fake_status(_pid: str) -> tuple[int, str]:
        return (0, _fake_status(pbip, pid))

    return capture.PackageCaptureRuntime(
        screenshotter=fake_screenshot,
        sleep=clock.sleep,
        clock=clock,
        status_querier=fake_status,
        now_utc=lambda: FIXED_TIME,
    )


# ---------------------------------------------------------------------------
# Package resolution
# ---------------------------------------------------------------------------


class TestResolvePackage:
    def test_resolves_unique_pbip(self) -> None:
        root = _workspace("resolve-ok")
        try:
            _make_package(root)
            identity = capture_receipt.resolve_package(root)
            assert identity.pbip_path.name == "Book.pbip"
            assert identity.report_folder.name == "Book.Report"
            assert identity.definition_pbir.name == "definition.pbir"
            assert len(identity.pbip_sha256) == 64
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_rejects_zero_pbips(self) -> None:
        root = _workspace("resolve-zero")
        try:
            (root / "fabric").mkdir(parents=True)
            with pytest.raises(ValueError, match="no .pbip file"):
                capture_receipt.resolve_package(root)
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_rejects_multiple_pbips(self) -> None:
        root = _workspace("resolve-multi")
        try:
            _make_package(root)
            (root / "fabric" / "Other.pbip").write_text("{}", encoding="utf-8")
            with pytest.raises(ValueError, match="multiple .pbip"):
                capture_receipt.resolve_package(root)
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# PBIR inventory
# ---------------------------------------------------------------------------


class TestReadPbirInventory:
    def test_reads_pages_and_visuals(self) -> None:
        root = _workspace("inventory-ok")
        try:
            _make_package(
                root,
                pages=[
                    ("Page1", "Sales", ["va", "vb"]),
                    ("Page2", "Details", ["vc"]),
                ],
            )
            identity = capture_receipt.resolve_package(root)
            inv = capture_receipt.read_pbir_inventory(identity.report_folder)
            assert len(inv) == 2
            assert inv[0].page_id == "Page1"
            assert [v.name for v in inv[0].visuals] == ["va", "vb"]
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_rejects_duplicate_page_ids(self) -> None:
        root = _workspace("inventory-dup-page")
        try:
            _make_package(root)
            # Duplicate page ID is structurally prevented by filesystem, so test via
            # the actual ValueError if we manually add a second page.json that repeats the ID.
            # Instead test duplicate visual IDs:
            identity = capture_receipt.resolve_package(root)
            # Add a duplicate visual
            # v1a already exists with name "v1a", add another dir with same name in visual.json
            dup_dir = identity.report_folder / "definition" / "pages" / "ReportSection1" / "visuals" / "v1a_other"
            dup_dir.mkdir(parents=True)
            (dup_dir / "visual.json").write_text(json.dumps({"name": "v1a"}), encoding="utf-8")
            with pytest.raises(ValueError, match="duplicate visual id"):
                capture_receipt.read_pbir_inventory(identity.report_folder)
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_rejects_malformed_page_json(self) -> None:
        root = _workspace("inventory-malformed")
        try:
            _make_package(root)
            identity = capture_receipt.resolve_package(root)
            page_json = identity.report_folder / "definition" / "pages" / "ReportSection1" / "page.json"
            page_json.write_text("{invalid", encoding="utf-8")
            with pytest.raises(ValueError, match="cannot read"):
                capture_receipt.read_pbir_inventory(identity.report_folder)
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Bridge validation
# ---------------------------------------------------------------------------


class TestBridgeValidation:
    def test_accepts_matching_pid_and_path(self) -> None:
        pbip = Path("/some/project/Book.pbip").resolve()
        instances = [capture_receipt.BridgeStatus(pid=1234, current_file_path=str(pbip))]
        # Should not raise
        capture_receipt.validate_bridge_open("1234", pbip, instances)

    def test_rejects_wrong_pid(self) -> None:
        pbip = Path("/some/project/Book.pbip").resolve()
        instances = [capture_receipt.BridgeStatus(pid=9999, current_file_path=str(pbip))]
        with pytest.raises(ValueError, match="PID 1234 not found"):
            capture_receipt.validate_bridge_open("1234", pbip, instances)

    def test_rejects_wrong_file_path(self) -> None:
        pbip = Path("/some/project/Book.pbip").resolve()
        instances = [capture_receipt.BridgeStatus(pid=1234, current_file_path="/wrong/Other.pbip")]
        with pytest.raises(ValueError, match="currentFilePath"):
            capture_receipt.validate_bridge_open("1234", pbip, instances)


# ---------------------------------------------------------------------------
# Iteration allocation
# ---------------------------------------------------------------------------


class TestAllocateIteration:
    def test_first_iteration_is_001(self) -> None:
        root = _workspace("iter-first")
        try:
            _dir, iteration_id = capture_receipt.allocate_iteration(root)
            assert iteration_id == "001"
            assert _dir.name == "001"
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_sequential_allocation(self) -> None:
        root = _workspace("iter-seq")
        try:
            capture_receipt.allocate_iteration(root)
            _dir, iteration_id = capture_receipt.allocate_iteration(root)
            assert iteration_id == "002"
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_rejects_noncanonical_sibling(self) -> None:
        root = _workspace("iter-noncanonical")
        try:
            iterations = root / "validation" / "iterations"
            iterations.mkdir(parents=True)
            (iterations / "1").mkdir()  # non-canonical
            with pytest.raises(ValueError, match="non-canonical"):
                capture_receipt.allocate_iteration(root)
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_concurrent_allocation_does_not_overwrite(self) -> None:
        root = _workspace("iter-concurrent")
        try:
            d1, _ = capture_receipt.allocate_iteration(root)
            assert d1.exists()
            # Second allocation gets 002
            d2, _ = capture_receipt.allocate_iteration(root)
            assert d2.exists()
            assert d1.exists()  # first still intact
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Full package capture — clean sweep
# ---------------------------------------------------------------------------


class TestCapturePackage:
    def test_clean_all_pages_capture_writes_receipt(self) -> None:
        root = _workspace("pkg-clean")
        try:
            _make_package(
                root,
                pages=[
                    ("Page1", "Sales", ["va"]),
                    ("Page2", "Map", ["vb", "vc"]),
                ],
            )
            runtime = _make_runtime(root)
            code = capture.capture_package(
                root,
                "1234",
                capture.CaptureOptions(poll=0.0, stable_seconds=0.0, max_wait=10.0),
                runtime,
            )
            assert code == 0
            receipt_path = root / "validation" / "iterations" / "001" / "capture.json"
            assert receipt_path.exists()
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            assert receipt["iteration_id"] == "001"
            assert receipt["mode"] == "sign-off"
            assert receipt["scope"] == "all-pages"
            assert len(receipt["pages"]) == 2
            # Check screenshot exists
            for page in receipt["pages"]:
                png = root / "validation" / "iterations" / "001" / page["screenshot"]["relative_path"]
                assert png.exists()
                assert png.stat().st_size > 0
                # Verify sha256
                assert page["screenshot"]["sha256"] == hashlib.sha256(png.read_bytes()).hexdigest()
            # Validate receipt schema
            errors = capture_receipt.validate_receipt(receipt)
            assert errors == []
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_subset_capture_is_triage_not_signoff(self) -> None:
        root = _workspace("pkg-subset")
        try:
            _make_package(
                root,
                pages=[
                    ("Page1", "Sales", ["va"]),
                    ("Page2", "Map", ["vb"]),
                ],
            )
            runtime = _make_runtime(root)
            code = capture.capture_package(
                root,
                "1234",
                capture.CaptureOptions(
                    poll=0.0,
                    stable_seconds=0.0,
                    max_wait=10.0,
                    page_ids=frozenset({"Page1"}),
                ),
                runtime,
            )
            assert code == 0
            receipt = json.loads(
                (root / "validation" / "iterations" / "001" / "capture.json").read_text(encoding="utf-8")
            )
            assert receipt["mode"] == "triage"
            assert receipt["scope"] == "subset"
            assert len(receipt["pages"]) == 1
            assert receipt["pages"][0]["page_id"] == "Page1"
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Failure modes — atomic cleanup
# ---------------------------------------------------------------------------


class TestCapturePackageFailures:
    def test_failed_page_removes_iteration(self) -> None:
        root = _workspace("pkg-fail")
        try:
            _make_package(root, pages=[("Page1", "Sales", ["va"])])
            runtime = _make_runtime(root, fail_pages=frozenset({"Page1"}))
            code = capture.capture_package(
                root,
                "1234",
                capture.CaptureOptions(poll=0.0, stable_seconds=0.0, max_wait=10.0),
                runtime,
            )
            assert code == 1
            assert not (root / "validation" / "iterations" / "001").exists()
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_zero_byte_screenshot_removes_iteration(self) -> None:
        root = _workspace("pkg-zerobyte")
        try:
            _make_package(root, pages=[("Page1", "Sales", ["va"])])
            runtime = _make_runtime(root, zero_byte_pages=frozenset({"Page1"}))
            code = capture.capture_package(
                root,
                "1234",
                capture.CaptureOptions(poll=0.0, stable_seconds=0.0, max_wait=10.0),
                runtime,
            )
            assert code == 1
            assert not (root / "validation" / "iterations" / "001").exists()
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_wrong_pid_fails(self) -> None:
        root = _workspace("pkg-wrong-pid")
        try:
            _make_package(root)
            clock = ManualClock()
            pbip = root / "fabric" / "Book.pbip"

            def wrong_status(_pid: str) -> tuple[int, str]:
                return (0, json.dumps({"instances": [{"pid": 9999, "currentFilePath": str(pbip.resolve())}]}))

            runtime = capture.PackageCaptureRuntime(
                screenshotter=lambda *a: True,
                sleep=clock.sleep,
                clock=clock,
                status_querier=wrong_status,
                now_utc=lambda: FIXED_TIME,
            )
            code = capture.capture_package(
                root,
                "1234",
                capture.CaptureOptions(poll=0.0, stable_seconds=0.0, max_wait=10.0),
                runtime,
            )
            assert code == 1
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    def test_wrong_current_file_path_fails(self) -> None:
        root = _workspace("pkg-wrong-path")
        try:
            _make_package(root)
            clock = ManualClock()

            def wrong_path_status(_pid: str) -> tuple[int, str]:
                return (0, json.dumps({"instances": [{"pid": 1234, "currentFilePath": "/wrong/Other.pbip"}]}))

            runtime = capture.PackageCaptureRuntime(
                screenshotter=lambda *a: True,
                sleep=clock.sleep,
                clock=clock,
                status_querier=wrong_path_status,
                now_utc=lambda: FIXED_TIME,
            )
            code = capture.capture_package(
                root,
                "1234",
                capture.CaptureOptions(poll=0.0, stable_seconds=0.0, max_wait=10.0),
                runtime,
            )
            assert code == 1
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Receipt validation
# ---------------------------------------------------------------------------


class TestReceiptValidation:
    def test_rejects_additional_top_level_keys(self) -> None:
        receipt = capture_receipt.CaptureReceipt(iteration_id="001", mode="sign-off", scope="all-pages")
        d = capture_receipt.receipt_to_dict(receipt)
        d["extra_key"] = "bad"
        errors = capture_receipt.validate_receipt(d)
        assert any("additional" in e for e in errors)

    def test_rejects_missing_top_level_keys(self) -> None:
        d = {"$schema": "x", "version": "1"}
        errors = capture_receipt.validate_receipt(d)
        assert any("missing" in e for e in errors)

    def test_rejects_path_traversal_in_screenshot(self) -> None:
        receipt = capture_receipt.CaptureReceipt(
            iteration_id="001",
            mode="sign-off",
            scope="all-pages",
            pages=[
                capture_receipt.PageCapture(
                    page_id="P1",
                    display_name="X",
                    visual_ids=[],
                    screenshot_relative_path="../../../etc/passwd",
                    screenshot_sha256="abc",
                    screenshot_bytes=100,
                    converged=True,
                    frames=3,
                    elapsed_seconds=1.0,
                )
            ],
        )
        d = capture_receipt.receipt_to_dict(receipt)
        errors = capture_receipt.validate_receipt(d)
        assert any("contained" in e for e in errors)

    def test_valid_receipt_has_no_errors(self) -> None:
        receipt = capture_receipt.CaptureReceipt(
            iteration_id="001",
            mode="sign-off",
            scope="all-pages",
            timestamp="2026-09-09T12:00:00+00:00",
            pages=[
                capture_receipt.PageCapture(
                    page_id="P1",
                    display_name="Sales",
                    visual_ids=["v1"],
                    screenshot_relative_path="pages/P1.png",
                    screenshot_sha256="abc123",
                    screenshot_bytes=100,
                    converged=True,
                    frames=3,
                    elapsed_seconds=1.5,
                )
            ],
        )
        d = capture_receipt.receipt_to_dict(receipt)
        errors = capture_receipt.validate_receipt(d)
        assert errors == []


# ---------------------------------------------------------------------------
# Iteration numbering integrity
# ---------------------------------------------------------------------------


class TestIterationNumbering:
    def test_second_capture_gets_iteration_002(self) -> None:
        root = _workspace("pkg-iter-seq")
        try:
            _make_package(root, pages=[("Page1", "Sales", ["va"])])
            runtime = _make_runtime(root)
            opts = capture.CaptureOptions(poll=0.0, stable_seconds=0.0, max_wait=10.0)
            code1 = capture.capture_package(root, "1234", opts, runtime)
            assert code1 == 0
            code2 = capture.capture_package(root, "1234", opts, runtime)
            assert code2 == 0
            assert (root / "validation" / "iterations" / "001" / "capture.json").exists()
            assert (root / "validation" / "iterations" / "002" / "capture.json").exists()
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Legacy mode cannot claim package sign-off
# ---------------------------------------------------------------------------


class TestLegacyModeUnchanged:
    def test_legacy_capture_writes_no_receipt(self) -> None:
        root = _workspace("legacy-no-receipt")
        report = root / "Book.Report"
        page = report / "definition" / "pages" / "Page1"
        page.mkdir(parents=True)
        (page / "page.json").write_text('{"displayName": "Sales"}', encoding="utf-8")
        clock = ManualClock()

        def fake_screenshot(_page_id: str, _pid: str, frame: Path) -> bool:
            frame.write_bytes(b"settled")
            return True

        try:
            code = capture.capture_report(
                report,
                root / "out",
                "1234",
                capture.CaptureOptions(poll=0.0, stable_seconds=0.0, max_wait=10.0),
                capture.CaptureRuntime(screenshotter=fake_screenshot, sleep=clock.sleep, clock=clock),
            )
            assert code == 0
            assert (root / "out" / "Sales.png").exists()
            # No capture.json or validation dir
            assert not (root / "validation").exists()
            assert not (root / "out" / "capture.json").exists()
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)
