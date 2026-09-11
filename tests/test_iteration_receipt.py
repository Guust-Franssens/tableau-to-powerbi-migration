"""Independent direct controls for PR #605's package-local receipt producer corrections.

The fixture is a coherent PBIP/report/model, not the former empty decoy PBIP. PNG positives are
also read by Pillow, independently of the production structural parser. Every negative names its
refusal; mutation selectors isolate the relevant boundary from unrelated fail-closed guards.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ruff: noqa: E402
import capture_powerbi_pages as capture
import current_artifact_revision as rev
import iteration_receipt as receipt
import reference_evidence as evidence
from png_fixtures import valid_png

LUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PAGE = "page-main"
SECOND = "page-trend"
PID = 1234


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _oracle_view(package: Path, name: str, height: int) -> dict:
    blob = valid_png(320, height)
    path = package / "oracle" / "images" / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return {
        "view_name": name,
        "view_type": "dashboard",
        "workbook_luid": LUID,
        "workbook_name": "Unit",
        "image": {
            "status": "ok",
            "path": f"images/{name}.png",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob),
            "dimensions_px": {"w": 320, "h": height},
        },
    }


def build_package(root: Path) -> Path:
    package = root / "packages" / "Unit"
    report = package / "fabric" / "Unit.Report"
    for page, display, visuals in ((PAGE, "main", ("v-1", "v-2")), (SECOND, "trend", ("v-3",))):
        directory = report / "definition" / "pages" / page
        write_json(directory / "page.json", {"name": page, "displayName": display})
        for visual in visuals:
            write_json(
                directory / "visuals" / visual / "visual.json", {"name": visual, "visual": {"visualType": "card"}}
            )
    write_json(report / "definition" / "pages" / "pages.json", {"pageOrder": [PAGE, SECOND]})
    write_json(report / "definition.pbir", {"datasetReference": {"byPath": {"path": "../Unit.SemanticModel"}}})
    model = package / "fabric" / "Unit.SemanticModel" / "definition" / "model.tmdl"
    model.parent.mkdir(parents=True)
    model.write_text("model Model\n", encoding="utf-8")
    write_json(package / "fabric" / "Unit.pbip", {"artifacts": [{"report": {"path": "Unit.Report"}}]})
    asset = package / "assets" / f"{LUID}_Unit.twb"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"<workbook/>")
    write_json(
        package / "source-provenance.json",
        {
            "inputs": [
                {
                    "input": {"sha256": hashlib.sha256(asset.read_bytes()).hexdigest()},
                    "origin": {"workbook_luid": LUID, "matched_by": "luid", "revision_match": "same"},
                }
            ]
        },
    )
    write_json(package / "migration-spec.json", {"limitations_encountered": [{"issue": "source table calculation"}]})
    write_json(
        package / "oracle" / "oracle-manifest.json",
        {
            "views": [
                _oracle_view(package, "main", 240),
                _oracle_view(package, "trend", 200),
            ]
        },
    )
    write_json(
        package / "package-manifest.json",
        {
            "unit": "Unit",
            "kind": "workbook",
            "artifacts": {
                "report": "fabric/Unit.Report",
                "model": "fabric/Unit.SemanticModel",
                "asset": f"assets/{LUID}_Unit.twb",
            },
        },
    )
    return package


@pytest.fixture(name="package")
def package_fixture(tmp_path: Path) -> Path:
    return build_package(tmp_path)


def _reference(package: Path) -> None:
    """Real manual validation-grade producer shape, revision-bound to this source's bytes."""
    shutil.rmtree(package / "oracle")
    rows = []
    for name, height in (("main", 240), ("trend", 200)):
        blob = valid_png(320, height)
        path = package / "reference" / f"tableau-{name}.png"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(blob)
        rows.append(
            {
                "name": f"tableau-{name}",
                "view_type": "dashboard",
                "states": [
                    {
                        "provider": "manual",
                        "image": path.name,
                        "sha256": hashlib.sha256(blob).hexdigest(),
                        "bytes": len(blob),
                        "dimensions": {"w": 320, "h": height},
                        "capabilities": ["layout_grade", "text_readable", "validation_grade"],
                    }
                ],
            }
        )
    asset = next((package / "assets").iterdir())
    write_json(
        package / "reference" / "manifest.json",
        {
            "source_workbook_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
            "dashboards": rows,
        },
    )


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _status(package: Path, *, pid: int = PID, path: Path | None = None) -> dict:
    return {
        "status": "ready",
        "instances": [
            {
                "pid": pid,
                "bridgeStatus": "connected",
                "currentFilePath": str((path or package / "fabric" / "Unit.pbip").absolute()),
                "hasUnsavedChanges": False,
            }
        ],
    }


def _runtime(package: Path, *, blob: bytes | None = None) -> capture.CaptureRuntime:
    clock = ManualClock()

    def shot(page: str, _pid: str, path: Path) -> bool:
        path.write_bytes(blob if blob is not None else valid_png(100, 80 if page == PAGE else 90))
        return True

    return capture.CaptureRuntime(shot, clock.sleep, clock, lambda _pid: _status(package), lambda _pid: True)


def _options(**overrides: object) -> capture.CaptureOptions:
    return capture.CaptureOptions(**{"poll": 1.0, "stable_seconds": 2.0, "max_wait": 10.0, **overrides})


def _iterate(
    package: Path,
    *,
    previous: dict | None = None,
    options: capture.CaptureOptions | None = None,
    runtime: capture.CaptureRuntime | None = None,
    **kwargs: object,
) -> dict:
    request = capture.IterationRequest(
        package,
        str(PID),
        session_id="session-1",
        previous_sha256=receipt.receipt_sha256(previous) if previous else None,
        **kwargs,
    )
    return capture.run_iteration(request, options or _options(), runtime or _runtime(package))


def _path(package: Path, number: str = "001") -> Path:
    return receipt.iterations_root(package) / number / receipt.RECEIPT_NAME


def _review(payload: dict, *, status: str = "unverified", findings: list | None = None) -> dict:
    judgement = copy.deepcopy(payload["judgement"])
    judgement["completed_at"] = None
    for row in judgement["pages"]:
        row["whole_page_status"] = status
        for visual in row["visual_results"]:
            visual["status"] = status
        for numeric in row["numeric_results"]:
            numeric["status"] = "unverified"
    judgement["findings"] = findings or []
    return judgement


def _finalize(package: Path, payload: dict, *, review: dict | None = None) -> dict:
    return receipt.finalize(
        package,
        receipt.receipt_sha256(payload),
        review if review is not None else _review(payload),
        state_reader=lambda _pid: _status(package),
    )


def _finding(**overrides: object) -> dict:
    return {
        "id": "F-001",
        "page_id": PAGE,
        "visual_id": "v-1",
        "kind": "visual",
        "severity": "high",
        "status": "still_open",
        "detail": "axis title missing",
        "limitation_ref": None,
        **overrides,
    }


def _code(callback: Callable[[], Any]) -> str:
    with pytest.raises(receipt.ReceiptError) as caught:
        callback()
    return caught.value.code


def test_clean_first_iteration_has_real_images_and_honest_data_state(package: Path) -> None:
    _reference(package)
    pending = _iterate(package)
    assert [row["expected_visual_ids"] for row in pending["generated"]["pages"]] == [["v-1", "v-2"], ["v-3"]]
    assert all(row["tableau"]["grade"] == evidence.GRADE_VALIDATION for row in pending["generated"]["pages"])
    for page in pending["generated"]["pages"]:
        image = _path(package).parent / page["powerbi"]["path"]
        with Image.open(image) as independent:
            independent.load()
            assert independent.format == "PNG" and min(independent.size) > 0
        facts = page["powerbi"]["capture"]
        assert facts["frames"] == 3 and facts["stable_elapsed_seconds"] == 2
    assert hashlib.sha256(_path(package).read_bytes()).hexdigest() == receipt.receipt_sha256(pending)
    final = _finalize(package, pending, review=_review(pending, status="pass"))
    assert final["state"] == "final" and final["outcome"] == "incomplete"
    assert final["generated"]["data_evidence"] == {"status": "pending", "reason": receipt.DATA_PENDING_REASON}
    assert final["judgement"]["completed_at"] is not None
    assert final["generated"] == pending["generated"]
    assert len(receipt.read_chain(package)) == 1


def test_valid_second_iteration_preserves_finding_identity_and_evolution(package: Path) -> None:
    first = _iterate(package)
    finding = _finding()
    final = _finalize(package, first, review=_review(first, findings=[finding]))
    visual = package / "fabric" / "Unit.Report" / "definition" / "pages" / PAGE / "visuals" / "v-1" / "visual.json"
    write_json(visual, {"name": "v-1", "visual": {"visualType": "card"}, "title": "restored"})
    second = _iterate(package, previous=final, runtime=_runtime(package, blob=valid_png(110, 80)))
    resolved = dict(finding, status="resolved")
    sealed = _finalize(package, second, review=_review(second, findings=[resolved]))
    assert sealed["generated"]["previous"] == {"iteration": "001", "receipt_sha256": receipt.receipt_sha256(final)}
    assert any(row["before_sha256"] != row["after_sha256"] for row in sealed["generated"]["changes_from_previous"])
    assert sealed["judgement"]["findings"] == [resolved]
    assert sealed["outcome"] == "incomplete"  # Data is still not independently proven.
    assert [item.name for item in receipt.read_chain(package)] == ["001", "002"]


@pytest.mark.parametrize(
    "field,expected",
    [
        ("state", "SCHEMA"),
        ("data", "SCHEMA"),
        ("unit", "CAPTURE_CHANGED"),
        ("reviewer", "CAPTURE_CHANGED"),
        ("time", "CAPTURE_CHANGED"),
        ("frames", "SCHEMA"),
        ("scope", "STATE_INVALID"),
    ],
)
def test_generated_state_forgery_is_rejected_by_the_capture_pin(package: Path, field: str, expected: str) -> None:
    original = _iterate(package)
    forged = copy.deepcopy(original)
    if field == "state":
        forged.update(state="final", outcome="complete")
    elif field == "data":
        forged["generated"]["data_evidence"]["status"] = "accepted"
    elif field == "unit":
        forged["generated"]["artifact"]["unit"] = "other"
    elif field == "reviewer":
        forged["generated"]["review"]["reviewer"] = "other-reviewer"
    elif field == "time":
        forged["generated"]["generated_at"] = "2026-01-01T00:00:00Z"
    elif field == "frames":
        forged["generated"]["pages"][0]["powerbi"]["capture"]["frames"] = -1
    else:
        forged["generated"]["scope"] = "subset"
    write_json(_path(package), forged)
    assert _code(lambda: _finalize(package, original)) == expected


def test_pending_state_cannot_have_a_final_outcome(package: Path) -> None:
    payload = _iterate(package)
    payload["outcome"] = "incomplete"
    assert _code(lambda: receipt.validate_receipt(payload)) == "STATE_INVALID"


def test_data_state_has_no_caller_authored_accepted_form(package: Path) -> None:
    payload = _iterate(package)
    payload["generated"]["data_evidence"]["status"] = "accepted"
    assert _code(lambda: receipt.validate_receipt(payload)) == "SCHEMA"


@pytest.mark.parametrize("value", [True, -1, receipt.MAX_COUNT + 1, float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["cache_byte_count", "byte_count", "entry_count"])
def test_generated_counts_reject_booleans_nonfinite_and_out_of_range(package: Path, field: str, value: object) -> None:
    payload = _iterate(package)
    if field == "cache_byte_count":
        payload["generated"]["artifact"].update(cache_sha256="1" * 64, cache_byte_count=value)
    elif field == "byte_count":
        payload["generated"]["pages"][0]["powerbi"]["byte_count"] = value
    else:
        payload["generated"]["limitations"]["entry_count"] = value
    assert _code(lambda: receipt.validate_receipt(payload)) in {"SCHEMA", "NONFINITE_NUMBER"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("frames", True),
        ("frames", -1),
        ("frames", 0),
        ("frames", receipt.MAX_FRAMES + 1),
        ("stable_seconds", True),
        ("stable_seconds", 0),
        ("stable_seconds", -1),
        ("stable_seconds", float("nan")),
        ("poll_seconds", float("inf")),
        ("settled_seconds", -1),
        ("settled_seconds", receipt.MAX_SECONDS + 1),
        ("max_wait_seconds", False),
        ("stable_elapsed_seconds", float("-inf")),
    ],
)
def test_capture_observation_numbers_are_strict_and_bounded(package: Path, field: str, value: object) -> None:
    payload = _iterate(package)
    payload["generated"]["pages"][0]["powerbi"]["capture"][field] = value
    assert _code(lambda: receipt.validate_receipt(payload)) in {"SCHEMA", "NONFINITE_NUMBER"}


@pytest.mark.parametrize("changes", [{"frames": 1}, {"stable_elapsed_seconds": 1}, {"settled_seconds": 1}])
def test_convergence_semantics_require_frames_and_earned_dwell(package: Path, changes: dict) -> None:
    payload = _iterate(package)
    payload["generated"]["pages"][0]["powerbi"]["capture"].update(changes)
    assert _code(lambda: receipt.validate_receipt(payload)) == "CAPTURE_INVALID"


def test_zero_dwell_policy_refuses_without_a_downstream_guard() -> None:
    assert _code(lambda: capture._validate_options(_options(stable_seconds=0), package=True)) == "CAPTURE_POLICY"


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), 3601])
@pytest.mark.parametrize("field", ["poll", "stable_seconds", "max_wait"])
def test_bad_package_policy_refuses_before_any_capture(package: Path, field: str, value: object) -> None:
    assert _code(lambda: _iterate(package, options=_options(**{field: value}))) == "CAPTURE_POLICY"
    assert not receipt.iterations_root(package).exists()


def test_standalone_zero_dwell_still_needs_two_identical_frames(tmp_path: Path) -> None:
    clock = ManualClock()
    calls = []

    def shot(_page: str, _pid: str, path: Path) -> bool:
        calls.append(1)
        path.write_bytes(b"same")
        return True

    result = capture.capture_stable(
        PAGE, "1234", tmp_path / "x.png", _options(stable_seconds=0), capture.CaptureRuntime(shot, clock.sleep, clock)
    )
    assert result.frames == 2 and len(calls) == 2 and result.converged


def test_zero_dwell_does_not_confuse_two_different_frames_with_stability(tmp_path: Path) -> None:
    clock = ManualClock()
    frames = iter((b"first", b"second", b"second"))

    def shot(_page: str, _pid: str, path: Path) -> bool:
        path.write_bytes(next(frames))
        return True

    result = capture.capture_stable(
        PAGE,
        "1234",
        tmp_path / "x.png",
        _options(stable_seconds=0),
        capture.CaptureRuntime(shot, clock.sleep, clock),
    )
    assert result.frames == 3 and result.converged


def test_deleted_visual_definition_cannot_shrink_inventory(package: Path) -> None:
    target = receipt.resolve_package(package)
    broken = target.report_dir / "definition" / "pages" / PAGE / "visuals" / "v-2" / "visual.json"
    assert len(rev.report_inventory(target.report_dir)[0].visual_ids) == 2
    broken.unlink()
    with pytest.raises(rev.RevisionError) as caught:
        rev.report_inventory(target.report_dir)
    assert caught.value.code == "VISUAL_DEFINITION_MISSING"


@pytest.mark.parametrize("order", [[PAGE, PAGE, SECOND], [PAGE, True], [PAGE, 1], [PAGE, ""], [PAGE, PAGE + " "], []])
def test_page_order_is_unique_nonempty_strings_without_coercion(package: Path, order: list) -> None:
    report = receipt.resolve_package(package).report_dir
    write_json(report / "definition" / "pages" / "pages.json", {"pageOrder": order})
    assert _code(lambda: receipt.report_inventory(report)) == "PAGE_ORDER_INVALID"


@pytest.mark.parametrize("kind", ["page", "visual"])
def test_inventory_requires_exact_folder_name_agreement(package: Path, kind: str) -> None:
    report = receipt.resolve_package(package).report_dir
    path = report / "definition" / "pages" / PAGE / "page.json"
    if kind == "visual":
        path = path.parent / "visuals" / "v-1" / "visual.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["name"] = "unrelated"
    write_json(path, doc)
    assert _code(lambda: receipt.report_inventory(report)) == (
        "PAGE_ID_MISMATCH" if kind == "page" else "VISUAL_ID_MISMATCH"
    )


def test_nested_extra_visual_definition_is_not_a_second_inventory(package: Path) -> None:
    report = receipt.resolve_package(package).report_dir
    write_json(report / "definition" / "pages" / PAGE / "extra" / "visual.json", {"name": "v-other"})
    assert _code(lambda: receipt.report_inventory(report)) == "NONCANONICAL_DEFINITION"


@pytest.mark.parametrize(
    "relative",
    [
        "..\\..\\..\\fabric\\Unit.pbip",
        "../../../fabric/Unit.pbip",
        "pages\\page.png",
        "/pages/page.png",
        "Z:page.png",
        "Z:/page.png",
        "pages/./page.png",
        "pages/../page.png",
        "pages//page.png",
        "pages/page.png.",
        "pages/CON.png",
        "pages/alias.png",
    ],
)
def test_screenshot_role_rejects_backslashes_traversal_and_aliases(relative: str) -> None:
    assert _code(lambda: receipt.screenshot_role(PAGE, relative)) == "SCREENSHOT_PATH"


def test_non_png_substitution_with_matching_hash_and_size_is_refused(package: Path) -> None:
    pending = _iterate(package)
    page = pending["generated"]["pages"][0]
    directory = _path(package).parent
    blob = (package / "fabric" / "Unit.pbip").read_bytes()
    (directory / page["powerbi"]["path"]).write_bytes(blob)
    page["powerbi"].update(sha256=hashlib.sha256(blob).hexdigest(), byte_count=len(blob))
    assert _code(lambda: receipt.image_facts(directory, PAGE, page["powerbi"]["path"])) == "SCREENSHOT_NOT_PNG"


@pytest.mark.parametrize("blob", [b"", b"\x89PNG\r\n\x1a\n", valid_png(20, 20)[:-8], b"not an image"])
def test_invalid_png_is_refused_during_capture(package: Path, blob: bytes) -> None:
    assert _code(lambda: _iterate(package, runtime=_runtime(package, blob=blob))) == "SCREENSHOT_NOT_PNG"
    assert not _path(package).exists()


def test_png_hardlink_alias_is_refused(package: Path) -> None:
    pending = _iterate(package)
    directory = _path(package).parent
    image = directory / pending["generated"]["pages"][0]["powerbi"]["path"]
    os.link(image, package.parent / "alias.png")
    assert (
        _code(lambda: receipt.image_facts(directory, PAGE, pending["generated"]["pages"][0]["powerbi"]["path"]))
        == "SCREENSHOT_ALIAS"
    )


def test_wrong_definition_pbir_model_is_refused(package: Path) -> None:
    report = receipt.resolve_package(package).report_dir
    write_json(report / "definition.pbir", {"datasetReference": {"byPath": {"path": "../Other.SemanticModel"}}})
    assert _code(lambda: receipt.resolve_package(package)) == "MODEL_BINDING"


@pytest.mark.parametrize(
    "relative,report_path",
    [("fabric/A-decoy.pbip", "Unit.Report"), ("A-decoy.pbip", "fabric/Unit.Report")],
)
def test_decoy_pbip_is_refused_even_when_it_references_the_same_report(
    package: Path, relative: str, report_path: str
) -> None:
    write_json(package.joinpath(*relative.split("/")), {"artifacts": [{"report": {"path": report_path}}]})
    assert _code(lambda: receipt.resolve_package(package)) == "PBIP_IDENTITY"


@pytest.mark.parametrize(
    "artifacts",
    [
        [],
        [{"report": {"path": "Wrong.Report"}}],
        [{"report": {"path": "Unit.Report"}}, {"report": {"path": "Unit.Report"}}],
    ],
)
def test_pbip_must_reference_exactly_the_declared_report(package: Path, artifacts: list) -> None:
    write_json(package / "fabric" / "Unit.pbip", {"artifacts": artifacts})
    assert _code(lambda: receipt.resolve_package(package)) == "PBIP_IDENTITY"


def test_unverified_pid_cannot_borrow_another_instances_current_path(package: Path) -> None:
    target = receipt.resolve_package(package)
    receipt.assert_desktop_binding(target, PID, lambda _pid: _status(package))
    assert (
        _code(lambda: receipt.assert_desktop_binding(target, PID, lambda _pid: _status(package, pid=PID + 1)))
        == "DESKTOP_UNVERIFIED"
    )


def test_runtime_wrong_path_is_refused_without_copying_the_path(package: Path) -> None:
    target = receipt.resolve_package(package)
    wrong = package.parent / "Other.pbip"
    assert (
        _code(lambda: receipt.assert_desktop_binding(target, PID, lambda _pid: _status(package, path=wrong)))
        == "DESKTOP_BINDING_MISMATCH"
    )


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"instances": []},
        {"instances": [{"pid": True, "currentFilePath": "not trusted"}]},
    ],
)
def test_missing_or_boolean_pid_status_is_not_binding(package: Path, state: dict) -> None:
    assert (
        _code(lambda: receipt.assert_desktop_binding(receipt.resolve_package(package), PID, lambda _pid: state))
        == "DESKTOP_UNVERIFIED"
    )


def test_default_runtime_invokes_pid_scoped_status_not_caller_claim(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def run(args: list, **kwargs: object) -> subprocess.CompletedProcess:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps(_status(package)).encode("utf-8"), b"")

    monkeypatch.setattr(receipt.subprocess, "run", run)
    receipt.assert_desktop_binding(receipt.resolve_package(package), PID)
    assert calls[0][0][:4] == ["powerbi-desktop", "status", "--pid", str(PID)]
    assert calls[0][1]["timeout"] == 60


@pytest.mark.parametrize("success,expected", [(True, True), (False, False), (1, False), (None, False)])
def test_reload_uses_the_installed_bridge_structured_success_shape(
    monkeypatch: pytest.MonkeyPatch, success: object, expected: bool
) -> None:
    monkeypatch.setattr(
        receipt, "bridge_json", lambda _command, _pid: {"status": "ok", "pid": PID, "result": {"success": success}}
    )
    assert receipt.bridge_reload(PID) is expected


def test_finalization_rechecks_pid_binding(package: Path) -> None:
    pending = _iterate(package)
    assert (
        _code(
            lambda: receipt.finalize(
                package,
                receipt.receipt_sha256(pending),
                _review(pending),
                state_reader=lambda _pid: _status(package, pid=PID + 1),
            )
        )
        == "DESKTOP_UNVERIFIED"
    )


def test_visual_pass_without_tableau_is_refused_at_evidence_boundary(package: Path) -> None:
    shutil.rmtree(package / "oracle")
    pending = _iterate(package)
    page = pending["generated"]["pages"][0]
    assert page["tableau"] is None
    assert _code(lambda: receipt._assert_visual_status("pass", page)) == "COMPARISON_EVIDENCE_MISSING"
    assert (
        _code(lambda: _finalize(package, pending, review=_review(pending, status="pass")))
        == "COMPARISON_EVIDENCE_MISSING"
    )
    assert _finalize(package, pending)["outcome"] == "incomplete"


def test_numeric_match_requires_independent_producer_evidence(package: Path) -> None:
    _reference(package)
    pending = _iterate(package)
    review = _review(pending, status="pass")
    review["pages"][0]["numeric_results"][0]["status"] = "pass"
    assert (
        _code(lambda: receipt._assert_judgement({**pending, "judgement": review}, None))
        == "NUMERIC_EVIDENCE_UNAVAILABLE"
    )
    assert _code(lambda: _finalize(package, pending, review=review)) == "NUMERIC_EVIDENCE_UNAVAILABLE"


def test_reviewer_supplied_numeric_hashes_do_not_become_producer_evidence(package: Path) -> None:
    _reference(package)
    pending = _iterate(package)
    review = _review(pending, status="pass")
    row = review["pages"][0]["numeric_results"][0]
    row.update(
        status="pass",
        tableau_evidence_sha256=pending["generated"]["pages"][0]["tableau"]["sha256"],
        powerbi_query_sha256="1" * 64,
        powerbi_result_sha256="2" * 64,
    )
    assert _code(lambda: _finalize(package, pending, review=review)) == "NUMERIC_EVIDENCE_UNAVAILABLE"


def test_oracle_layout_match_preserves_the_numeric_and_full_visual_ceiling(package: Path) -> None:
    pending = _iterate(package)
    page = pending["generated"]["pages"][0]
    assert page["tableau"]["grade"] == evidence.GRADE_ORACLE
    assert _code(lambda: receipt._assert_visual_status("pass", page)) == "COMPARISON_GRADE"
    final = _finalize(package, pending, review=_review(pending, status="layout_match"))
    assert final["outcome"] == "incomplete"
    assert all(row["status"] == "unverified" for page in final["judgement"]["pages"] for row in page["numeric_results"])


@pytest.mark.parametrize("phase", ["allocation", "finalization"])
def test_altered_prior_png_invalidates_the_chain(package: Path, phase: str) -> None:
    first = _iterate(package)
    final = _finalize(package, first)
    second = _iterate(package, previous=final) if phase == "finalization" else None
    image = _path(package).parent / first["generated"]["pages"][0]["powerbi"]["path"]
    image.write_bytes(valid_png(100, 81))
    operation = (
        (lambda: _finalize(package, second)) if second is not None else (lambda: _iterate(package, previous=final))
    )
    assert _code(operation) == "SCREENSHOT_CHANGED"


@pytest.mark.parametrize("change", ["receipt", "extra-file", "extra-directory", "gap", "missing-image"])
def test_prior_exact_file_set_and_receipt_are_revalidated_on_allocation(package: Path, change: str) -> None:
    pending = _iterate(package)
    final = _finalize(package, pending)
    directory = _path(package).parent
    expected = "EXTRA_FILE"
    if change == "receipt":
        modified = copy.deepcopy(final)
        modified["generated"]["review"]["reviewer"] = "somebody-else"
        write_json(_path(package), modified)
        expected = "PREVIOUS_RECEIPT_MISMATCH"
    elif change == "extra-file":
        (directory / "pages" / "extra.png").write_bytes(valid_png(100, 81))
    elif change == "extra-directory":
        (directory / "pages" / "extra").mkdir()
    elif change == "gap":
        directory.rename(directory.with_name("003"))
        expected = "ITERATION_GAP"
    else:
        (directory / pending["generated"]["pages"][0]["powerbi"]["path"]).unlink()
        expected = "SCREENSHOT_MISSING"
    assert _code(lambda: _iterate(package, previous=final)) == expected


def test_prior_receipt_mutation_after_allocation_breaks_the_pinned_link(package: Path) -> None:
    first = _iterate(package)
    final = _finalize(package, first)
    second = _iterate(package, previous=final)
    _path(package).write_bytes(_path(package).read_bytes() + b" ")
    assert _code(lambda: _finalize(package, second)) == "PREVIOUS_RECEIPT_MISMATCH"


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_id", SECOND),
        ("visual_id", "v-3"),
        ("kind", "numeric"),
        ("severity", "low"),
        ("detail", "a different defect"),
        ("limitation_ref", {"pointer": "/limitations_encountered/0", "sha256": "0" * 64}),
    ],
)
def test_finding_identity_cannot_be_reused_to_hide_a_different_finding(
    package: Path, field: str, value: object
) -> None:
    first = _iterate(package)
    finding = _finding()
    final = _finalize(package, first, review=_review(first, findings=[finding]))
    second = _iterate(package, previous=final)
    changed = {**finding, "status": "resolved", field: value}
    assert (
        _code(lambda: _finalize(package, second, review=_review(second, findings=[changed])))
        == "FINDING_IDENTITY_CHANGED"
    )


def test_finding_disappearance_and_terminal_reopening_are_illegal(package: Path) -> None:
    first = _iterate(package)
    finding = _finding()
    final = _finalize(package, first, review=_review(first, findings=[finding]))
    second = _iterate(package, previous=final)
    assert _code(lambda: _finalize(package, second)) == "FINDING_DISAPPEARED"
    resolved = dict(finding, status="resolved")
    final2 = _finalize(package, second, review=_review(second, findings=[resolved]))
    third = _iterate(package, previous=final2)
    assert _code(lambda: _finalize(package, third, review=_review(third, findings=[finding]))) == "FINDING_TRANSITION"


def test_new_findings_must_reference_the_actual_inventory(package: Path) -> None:
    pending = _iterate(package)
    unknown = _finding(visual_id="imaginary")
    assert _code(lambda: _finalize(package, pending, review=_review(pending, findings=[unknown]))) == "FINDING_TARGET"


def test_prebound_limitation_can_evolve_without_changing_identity(package: Path) -> None:
    entry = json.loads((package / "migration-spec.json").read_text(encoding="utf-8"))["limitations_encountered"][0]
    finding = _finding(
        limitation_ref={"pointer": "/limitations_encountered/0", "sha256": receipt.limitation_entry_sha256(entry)}
    )
    first = _iterate(package)
    final = _finalize(package, first, review=_review(first, findings=[finding]))
    second = _iterate(package, previous=final)
    accepted = dict(finding, status="accepted_limitation")
    assert _finalize(package, second, review=_review(second, findings=[accepted]))["judgement"]["findings"] == [
        accepted
    ]


@pytest.mark.parametrize(
    "location",
    [".pbi/custom.bin", "fabric/Unit.Report/.pbi/unappliedChanges.json", "fabric/Unit.SemanticModel/.pbi/metadata.bin"],
)
def test_arbitrary_pbi_bytes_move_the_current_package_revision(package: Path, location: str) -> None:
    target = receipt.resolve_package(package)
    path = package.joinpath(*location.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"before")
    before = rev.package_working_revision(package, target.model_dir)
    path.write_bytes(b"after")
    assert rev.package_working_revision(package, target.model_dir) != before


def test_only_the_exact_declared_cache_is_separately_hashed(package: Path) -> None:
    target = receipt.resolve_package(package)
    cache = target.model_dir / ".pbi" / "cache.abf"
    cache.parent.mkdir()
    cache.write_bytes(b"before")
    before = receipt.artifact_facts(target)
    cache.write_bytes(b"after")
    after = receipt.artifact_facts(target)
    assert before["package_revision"] == after["package_revision"]
    assert before["model_revision"] == after["model_revision"]
    assert before["cache_sha256"] != after["cache_sha256"]
    _iterate(package)
    assert rev.package_working_revision(package, target.model_dir) == after["package_revision"]


@pytest.mark.parametrize(
    "relative",
    [
        "fabric/Unit.Report/definition/report.json",
        "fabric/Unit.SemanticModel/definition/model.tmdl",
        "fabric/Unit.Report/.pbi/unappliedChanges.json",
        "migration-spec.json",
    ],
)
def test_finalization_rederives_all_current_artifact_facts(package: Path, relative: str) -> None:
    pending = _iterate(package)
    path = package.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if relative == "migration-spec.json":
        write_json(path, {"limitations_encountered": [{"issue": "changed"}]})
    else:
        path.write_bytes(b"changed")
    assert _code(lambda: _finalize(package, pending)) == "GENERATED_CHANGED"


def test_credential_string_cannot_reach_a_pending_receipt_write(package: Path) -> None:
    pending = _iterate(package)
    pending["judgement"]["findings"] = [_finding(detail="Authorization: fake-review-token")]
    assert _code(lambda: receipt.write_receipt(_path(package).parent, pending)) == "PRIVACY"


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: fake-review-token",
        "Authorization%3A%20fake-review-token",
        "token=synthetic-token",
        "password=synthetic-password",
        "Bearer fake-token",
        "x-tableau-auth: synthetic-session",
        "https://customer.example/site/unit",
        "prefix " + "X:" + chr(92) + "private" + chr(92) + "file",
        "two\nlines",
    ],
)
def test_every_reviewer_string_crosses_central_privacy_containment(package: Path, text: str) -> None:
    pending = _iterate(package)
    assert (
        _code(lambda: _finalize(package, pending, review=_review(pending, findings=[_finding(detail=text)])))
        == "PRIVACY"
    )


def test_generated_strings_are_contained_before_pending_output(package: Path) -> None:
    path = package / "fabric" / "Unit.Report" / "definition" / "pages" / PAGE / "page.json"
    write_json(path, {"name": PAGE, "displayName": "Authorization: fake-server-echo"})
    assert _code(lambda: _iterate(package)) == "PRIVACY"
    assert not _path(package).exists()


@pytest.mark.parametrize("reviewer", ["name with spaces", "reviewer:token", "reviewer\n", "reviewer" + chr(0x1F600)])
def test_reviewer_identifiers_have_a_safe_closed_format(package: Path, reviewer: str) -> None:
    assert _code(lambda: _iterate(package, reviewer=reviewer)) in {"SCHEMA", "PRIVACY"}


def test_reader_invalid_utf8_has_a_fixed_named_refusal(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_bytes(b'{"unit":"\xff"}')
    try:
        rev.read_json(path)
    except Exception as error:  # Deliberately distinguishes the raw decode exception under mutation.
        observed = type(error).__name__, getattr(error, "code", None), str(error)
    else:
        observed = "accepted", None, ""
    assert observed == ("RevisionError", "JSON_NOT_UTF8", "JSON_NOT_UTF8: JSON input is not valid UTF-8")


@pytest.mark.parametrize(
    "relative",
    [
        "package-manifest.json",
        "fabric/Unit.pbip",
        "fabric/Unit.Report/definition.pbir",
        "fabric/Unit.Report/definition/pages/pages.json",
        f"fabric/Unit.Report/definition/pages/{PAGE}/page.json",
        f"fabric/Unit.Report/definition/pages/{PAGE}/visuals/v-1/visual.json",
        "source-provenance.json",
        "migration-spec.json",
        "oracle/oracle-manifest.json",
    ],
)
def test_every_package_json_reader_refuses_invalid_utf8(package: Path, relative: str) -> None:
    package.joinpath(*relative.split("/")).write_bytes(b"\xff")
    assert _code(lambda: _iterate(package)) == "JSON_NOT_UTF8"
    assert not _path(package).exists()


def test_reference_and_history_json_also_refuse_invalid_utf8(package: Path) -> None:
    _reference(package)
    pending = _iterate(package)
    _path(package).write_bytes(b"\xff")
    assert _code(lambda: receipt.read_chain(package)) == "JSON_NOT_UTF8"
    _path(package).write_bytes(receipt.receipt_bytes(pending))
    (package / "reference" / "manifest.json").write_bytes(b"\xff")
    assert _code(lambda: _finalize(package, pending)) == "JSON_NOT_UTF8"


def test_reviewer_file_invalid_utf8_has_a_fixed_cli_refusal(
    package: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pending = _iterate(package)
    review = tmp_path / "review.json"
    review.write_bytes(b"\xff")
    args = capture.parse_args(
        [
            "finalize",
            "--package",
            str(package),
            "--capture-sha256",
            receipt.receipt_sha256(pending),
            "--judgement",
            str(review),
        ]
    )
    assert capture.cmd_finalize(args, _runtime(package)) == capture.EXIT_REFUSED
    output = capsys.readouterr().out
    assert output == "REFUSED: JSON_NOT_UTF8: JSON input is not valid UTF-8\n"


def test_strict_reader_refuses_duplicate_keys_and_nonfinite_tokens(tmp_path: Path) -> None:
    for text in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'):
        path = tmp_path / "input.json"
        path.write_text(text, encoding="utf-8")
        assert _code(lambda: receipt.read_strict_json(path)) == "JSON_INVALID"


def test_console_safe_print_survives_strict_cp1252() -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict", newline="\n")
    try:
        capture._emit("refusal " + chr(0x1F600), stream=stream)
        stream.flush()
        outcome = buffer.getvalue()
    except UnicodeEncodeError:
        outcome = b"unicode-crash"
    assert outcome == b"refusal \\U0001f600\n"
    stream.detach()


def test_cli_invalid_utf8_is_ascii_safe_and_never_leaks_traceback(package: Path) -> None:
    (package / "package-manifest.json").write_bytes(b"\xff")
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "capture_powerbi_pages.py"),
            "iterate",
            "--package",
            str(package),
            "--pid",
            str(PID),
        ],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "cp1252:strict"},
    )
    assert result.returncode == capture.EXIT_REFUSED
    assert b"REFUSED: JSON_NOT_UTF8:" in result.stdout
    assert b"Traceback" not in result.stderr and str(package).encode() not in result.stdout + result.stderr


def test_cli_uses_external_capture_pin_and_separate_judgement(
    package: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = capture.parse_args(
        ["iterate", "--package", str(package), "--pid", str(PID), "--poll", "1", "--stable-seconds", "2"]
    )
    assert capture.cmd_iterate(args, _runtime(package)) == 0
    output = capsys.readouterr().out
    token = output.split("CAPTURE_SHA256=", 1)[1].splitlines()[0]
    pending = receipt.read_strict_json(_path(package))
    review = tmp_path / "review.json"
    write_json(review, _review(pending))
    args = capture.parse_args(
        ["finalize", "--package", str(package), "--capture-sha256", token, "--judgement", str(review)]
    )
    assert capture.cmd_finalize(args, _runtime(package)) == 0
    output = capsys.readouterr().out
    assert "outcome incomplete" in output and "FINAL_SHA256=" in output


@pytest.mark.parametrize("option", ["--desktop-file-path", "--data-evidence"])
def test_cli_has_no_caller_authored_proof_flags(option: str) -> None:
    with pytest.raises(SystemExit) as caught:
        capture.parse_args(["iterate", "--package", "unit", "--pid", "1234", option, "forged"])
    assert caught.value.code == capture.EXIT_USAGE


def test_subset_is_triage_and_cannot_be_promoted_by_reviewer(package: Path) -> None:
    pending = _iterate(package, options=_options(page_ids=frozenset({PAGE})))
    assert pending["mode"] == "triage" and pending["generated"]["scope"] == "subset"
    assert _finalize(package, pending)["outcome"] == "incomplete"
    assert (
        _code(lambda: _iterate(package, options=_options(page_ids=frozenset({PAGE})), mode="sign_off"))
        == "SUBSET_CANNOT_SIGN_OFF"
    )


def test_failed_capture_removes_only_its_own_allocation(package: Path) -> None:
    runtime = _runtime(package)
    broken = capture.CaptureRuntime(
        lambda *_args: False, runtime.sleep, runtime.clock, runtime.state_reader, runtime.reload
    )
    assert _code(lambda: _iterate(package, runtime=broken)) == "CAPTURE_FAILED"
    assert not _path(package).parent.exists()


def test_finalization_cannot_be_repeated(package: Path) -> None:
    pending = _iterate(package)
    final = _finalize(package, pending)
    assert _code(lambda: _finalize(package, final)) == "ALREADY_FINAL"


def _pending_successor(package: Path) -> dict:
    cache = package / "fabric" / "Unit.SemanticModel" / ".pbi" / "cache.abf"
    cache.parent.mkdir()
    cache.write_bytes(b"original cache")
    return _iterate(package, previous=_finalize(package, _iterate(package)))


def _change_during_finalization(package: Path, pending: dict, kind: str) -> None:
    report = package / "fabric" / "Unit.Report"
    if kind == "report":
        write_json(report / "definition" / "report.json", {"changed": True})
    elif kind == "page":
        write_json(report / "definition" / "pages" / PAGE / "page.json", {"name": PAGE, "displayName": "changed"})
    elif kind == "model":
        (package / "fabric" / "Unit.SemanticModel" / "definition" / "model.tmdl").write_bytes(b"model Changed\n")
    elif kind == "cache":
        (package / "fabric" / "Unit.SemanticModel" / ".pbi" / "cache.abf").write_bytes(b"changed cache")
    elif kind == "predecessor-receipt":
        _path(package).write_bytes(_path(package).read_bytes() + b" ")
    elif kind == "current-receipt":
        _path(package, "002").write_bytes(receipt.receipt_bytes(pending) + b" ")
    else:
        assert kind in {"predecessor-png", "current-png"}
        directory = _path(package, "001" if kind == "predecessor-png" else "002").parent
        (directory / pending["generated"]["pages"][0]["powerbi"]["path"]).write_bytes(valid_png(100, 81))


def test_read_chain_checksum_is_of_the_exact_bytes_parsed_and_validated(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _iterate(package)
    original = _path(package).read_bytes()
    changed = copy.deepcopy(pending)
    changed["generated"]["review"]["reviewer"] = "replacement"
    validate = receipt.validate_receipt

    def validate_then_swap(payload: dict) -> dict:
        result = validate(payload)
        write_json(_path(package), changed)
        return result

    monkeypatch.setattr(receipt, "validate_receipt", validate_then_swap)
    selected = receipt.read_chain(package)[-1]
    assert selected.payload == pending
    assert selected.receipt_sha256 == hashlib.sha256(original).hexdigest()
    assert selected.receipt_bytes == original
    assert _path(package).read_bytes() != original


def test_receipt_swap_after_checksum_decision_cannot_be_consumed_as_pinned_capture(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _iterate(package)
    changed = copy.deepcopy(pending)
    changed["generated"]["review"]["reviewer"] = "replacement"
    require_pin = receipt._require_pin
    decisions = []

    def pin_then_swap(actual: str, expected: str | None, code: str) -> None:
        require_pin(actual, expected, code)
        decisions.append(code)
        write_json(_path(package), changed)

    monkeypatch.setattr(receipt, "_require_pin", pin_then_swap)
    assert _code(lambda: _finalize(package, pending)) == "GENERATED_CHANGED"
    assert decisions == ["CAPTURE_CHANGED"]
    assert json.loads(_path(package).read_bytes()) == changed


@pytest.mark.parametrize("status_call", [1, 2])
@pytest.mark.parametrize(
    "kind",
    ["report", "page", "model", "cache", "predecessor-png", "predecessor-receipt", "current-png", "current-receipt"],
)
def test_every_pid_status_precedes_the_final_artifact_and_full_chain_snapshot(
    package: Path, monkeypatch: pytest.MonkeyPatch, status_call: int, kind: str
) -> None:
    pending = _pending_successor(package)
    original = _path(package, "002").read_bytes()
    calls = []

    def status(_pid: int) -> dict:
        calls.append(_pid)
        if len(calls) == status_call:
            _change_during_finalization(package, pending, kind)
        return _status(package)

    def never_publish(*_args: object) -> None:
        pytest.fail("a change during PID status reached atomic publication")

    monkeypatch.setattr(receipt, "_publish_final", never_publish)
    code = _code(
        lambda: receipt.finalize(package, receipt.receipt_sha256(pending), _review(pending), state_reader=status)
    )
    expected = {"predecessor-png": "SCREENSHOT_CHANGED", "predecessor-receipt": "PREVIOUS_RECEIPT_MISMATCH"}
    assert code == expected.get(kind, "GENERATED_CHANGED")
    assert len(calls) >= status_call
    assert json.loads(_path(package, "002").read_bytes())["state"] == "pending"
    if kind != "current-receipt":
        assert _path(package, "002").read_bytes() == original


@pytest.mark.parametrize("ordering", ["before", "after"])
@pytest.mark.parametrize(
    "kind",
    ["report", "page", "model", "cache", "predecessor-png", "predecessor-receipt", "current-png", "current-receipt"],
)
def test_mutation_on_either_side_of_atomic_publication_restores_exact_pending(
    package: Path, monkeypatch: pytest.MonkeyPatch, ordering: str, kind: str
) -> None:
    pending = _pending_successor(package)
    path = _path(package, "002")
    original = path.read_bytes()
    link = os.link
    calls = []

    def publish(source: Path, destination: Path) -> None:
        assert destination == path
        calls.append(ordering)
        if ordering == "before":
            _change_during_finalization(package, pending, kind)
        link(source, destination)
        if ordering == "after":
            _change_during_finalization(package, pending, kind)

    monkeypatch.setattr(receipt.os, "link", publish)
    expected = (
        "FINALIZATION_WRITE_FAILED" if (ordering, kind) == ("before", "current-receipt") else "FINALIZATION_CHANGED"
    )
    assert _code(lambda: _finalize(package, pending)) == expected
    assert calls == [ordering]
    assert path.read_bytes() == original
    assert not (path.parent / receipt.PENDING_BACKUP_NAME).exists()
    assert not (path.parent / ".iteration.writing").exists()


def test_post_publication_checks_final_bytes_not_only_parsed_content(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _pending_successor(package)
    path = _path(package, "002")
    original = path.read_bytes()
    link = os.link
    changes = []

    def publish_then_reserialize(source: Path, destination: Path) -> None:
        link(source, destination)
        final = destination.read_bytes()
        assert json.loads(final)["state"] == "final"
        changed = final + b" "
        assert json.loads(changed) == json.loads(final)
        destination.write_bytes(changed)
        changes.append(True)

    monkeypatch.setattr(receipt.os, "link", publish_then_reserialize)
    assert _code(lambda: _finalize(package, pending)) == "FINALIZATION_CHANGED"
    assert changes == [True]
    assert path.read_bytes() == original


def test_unchanged_publication_retains_exact_final_bytes_and_full_predecessor_chain(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _pending_successor(package)
    path = _path(package, "002")
    original = path.read_bytes()
    predecessor = _path(package).read_bytes()
    link = os.link
    published = []

    def publish(source: Path, destination: Path) -> None:
        assert not destination.exists()
        assert (path.parent / receipt.PENDING_BACKUP_NAME).read_bytes() == original
        published.append(source.read_bytes())
        link(source, destination)

    monkeypatch.setattr(receipt.os, "link", publish)
    final = _finalize(package, pending)
    assert len(published) == 1 and path.read_bytes() == published[0] == receipt.receipt_bytes(final)
    assert _path(package).read_bytes() == predecessor
    assert [item.receipt_bytes for item in receipt.read_chain(package)] == [predecessor, published[0]]
    assert final["outcome"] == "incomplete" and final["generated"]["data_evidence"]["status"] == "pending"
    assert all(row["status"] == "unverified" for page in final["judgement"]["pages"] for row in page["numeric_results"])
    assert not (path.parent / receipt.PENDING_BACKUP_NAME).exists()


def test_rollback_rename_failure_retains_original_pending_and_a_refused_chain(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _pending_successor(package)
    path = _path(package, "002")
    backup = path.parent / receipt.PENDING_BACKUP_NAME
    original, predecessor = path.read_bytes(), _path(package).read_bytes()
    link, replace = os.link, os.replace
    rollbacks = []

    def publish(source: Path, destination: Path) -> None:
        link(source, destination)
        _change_during_finalization(package, pending, "page")

    def fail_rollback(source: Path, destination: Path) -> None:
        if source == backup:
            rollbacks.append(True)
            raise PermissionError("synthetic write denial")
        replace(source, destination)

    monkeypatch.setattr(receipt.os, "link", publish)
    monkeypatch.setattr(receipt.os, "replace", fail_rollback)
    assert _code(lambda: _finalize(package, pending)) == "FINALIZATION_ROLLBACK_FAILED"
    assert rollbacks == [True]
    assert backup.read_bytes() == original and _path(package).read_bytes() == predecessor
    assert _code(lambda: receipt.read_chain(package)) == "EXTRA_FILE"
    assert _code(lambda: _finalize(package, pending)) == "EXTRA_FILE"
    assert _code(lambda: receipt.allocate_iteration(package, receipt.receipt_sha256(pending))) == "EXTRA_FILE"


def test_current_receipt_swap_at_displacement_never_leaves_an_authoritative_final(
    package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _pending_successor(package)
    path = _path(package, "002")
    replace = os.replace
    changed = path.read_bytes() + b" "

    def swap_then_displace(source: Path, destination: Path) -> None:
        if source == path:
            source.write_bytes(changed)
        replace(source, destination)

    monkeypatch.setattr(receipt.os, "replace", swap_then_displace)
    assert _code(lambda: _finalize(package, pending)) == "FINALIZATION_ROLLBACK_FAILED"
    assert not path.exists()
    assert (path.parent / receipt.PENDING_BACKUP_NAME).read_bytes() == changed
    assert _code(lambda: receipt.read_chain(package)) == "INPUT_UNREADABLE"
