"""Tests for scripts/check_unit.py - the per-unit derive-first façade from #291.

Fixtures use the real emitted artifact shapes: migration-spec dashboards, PBIR page/page-order JSON,
reference capture manifests, and Tableau Server oracle manifests. The tests avoid treating command
execution failures as caught mutations; when a subprocess is used, the assertion checks the intended
exit code and output shape rather than any non-zero result.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_unit as cu  # noqa: E402  # pylint: disable=wrong-import-position

ORIGINAL_CHECK_OCCLUSION = cu.check_occlusion
ORIGINAL_GATES = cu.GATES


def _load_script_module(script_name: str):
    name = script_name[:-3]
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / script_name)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _gate_by_id(check_id: str) -> cu.Gate:
    matches = [gate for gate in ORIGINAL_GATES if gate.check_id == check_id]
    assert len(matches) == 1
    return matches[0]


def _freshen_clean_fixture_cache() -> Path:
    fixture = REPO_ROOT / "tests" / "fixtures" / "check-unit-clean-integration"
    cache = fixture / "pbip" / "Book" / "Book.SemanticModel" / ".pbi" / "cache.abf"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("cache placeholder\n", encoding="utf-8")
    future = time.time() + 60
    os.utime(cache, (future, future))
    return fixture


def _write_spec(unit: Path, names: list[str]) -> None:
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "migration-spec.json").write_text(
        json.dumps({"dashboards": [{"id": f"dash.{index}", "name": name} for index, name in enumerate(names)]}),
        encoding="utf-8",
    )


def _write_report(unit: Path, names: list[str]) -> Path:
    report = unit / "fabric" / "Book.Report"
    pages = report / "definition" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    order = []
    for index, name in enumerate(names):
        page_id = f"p{index + 1}"
        order.append(page_id)
        page = pages / page_id
        page.mkdir()
        (page / "page.json").write_text(
            json.dumps({"name": page_id, "displayName": name, "width": 1600, "height": 900}),
            encoding="utf-8",
        )
    (pages / "pages.json").write_text(json.dumps({"pageOrder": order}), encoding="utf-8")
    return report


def _png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)


def _csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a\n1\n", encoding="utf-8")


def _write_reference_manifest(unit: Path, names: list[str], *, numeric: bool = True) -> None:
    states = []
    dashboards = []
    for name in names:
        image = f"{name}.png"
        _png(unit / "reference" / image)
        numeric_path = f"{name}.csv"
        if numeric:
            _csv(unit / "reference" / numeric_path)
        states = [
            {
                "image": image,
                "numeric_oracle": numeric_path if numeric else None,
                "capabilities": ["layout_grade", "text_readable", "validation_grade"],
            }
        ]
        dashboards.append({"name": name, "states": states})
    (unit / "reference" / "manifest.json").write_text(json.dumps({"dashboards": dashboards}), encoding="utf-8")


def _write_oracle_manifest(unit: Path, names: list[str], *, images: bool = True, data: bool = True) -> None:
    records = []
    for index, name in enumerate(names):
        image_path = f"images/{name}__{index}.png"
        data_path = f"data/{name}__{index}.csv"
        if images:
            _png(unit / "_oracle" / image_path)
        if data:
            _csv(unit / "_oracle" / data_path)
        records.append(
            {
                "view_name": name,
                "data": {"status": "ok", "path": data_path, "row_count": 1} if data else {"status": "failed"},
                "image": {"status": "ok", "path": image_path} if images else {"status": "failed"},
            }
        )
    (unit / "_oracle").mkdir(exist_ok=True)
    (unit / "_oracle" / "oracle-manifest.json").write_text(json.dumps({"views": records}), encoding="utf-8")


@pytest.fixture(autouse=True)
def no_native_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests isolate check_unit's new logic; native gate wiring is tested separately."""
    monkeypatch.setattr(cu, "GATES", ())
    monkeypatch.setattr(cu, "check_engine_receipt", lambda _target: {"id": "engine-receipt", "status": cu.STATUS_PASS})
    monkeypatch.setattr(cu, "check_occlusion", lambda *_args: {"id": "occlusion", "status": cu.STATUS_PASS})
    monkeypatch.setattr(
        cu, "check_ai_descriptions", lambda _target: {"id": "ai-descriptions", "status": cu.STATUS_PASS}
    )
    monkeypatch.setattr(
        cu, "check_ai_instructions", lambda _target: {"id": "ai-instructions", "status": cu.STATUS_PASS}
    )
    monkeypatch.setattr(
        cu, "check_cache_freshness", lambda _target: {"id": "cache-freshness", "status": cu.STATUS_PASS}
    )
    monkeypatch.setattr(cu, "claimed_only_checks", lambda: [])


def test_brownfield_empty_folder_says_expected_shape(tmp_path: Path) -> None:
    """Wrong-shape NOT_CHECKED output names the expected bundle/unit shapes."""
    report = cu.run_all(tmp_path)
    rendered = cu.render(report)

    assert report["status"] == cu.STATUS_NOT_CHECKED
    assert report["brownfield"]["found_count"] == 0
    assert "not_checked_missing_input=" in rendered
    assert "expected a migration unit or engine bundle shaped as one of:" in rendered
    assert "reorganisation plan (not applied): no recognised artifacts to place" in rendered


def test_brownfield_rearranged_real_artifacts_emit_plan() -> None:
    """Real artifacts in someone else's folders are reported with concrete proposed destinations."""
    fixture = REPO_ROOT / "tests" / "fixtures" / "check-unit-brownfield-rearranged"

    report = cu.run_all(fixture)
    rendered = cu.render(report)

    assert report["brownfield"]["found_count"] >= 4
    assert not report["brownfield"]["recognized_target_shape"]
    assert "source\\migration-spec.json" in rendered
    assert "PowerBI\\Admin_Insights_Starter.Report" in rendered
    assert "PowerBI\\Admin_Insights_Starter.SemanticModel" in rendered
    assert "reorganisation plan (not applied):" in rendered
    assert "working copy:" in rendered
    assert "engine truth:" in rendered


def test_brownfield_partial_pbip_reports_evidenced_and_missing_phases() -> None:
    """A partial migration is not called missing; evidenced phases and absent phases are separated."""
    fixture = REPO_ROOT / "tests" / "fixtures" / "check-unit-brownfield-partial-pbip"

    report = cu.run_all(fixture)
    rendered = cu.render(report)

    assert report["brownfield"]["recognized_target_shape"]
    assert "PBIR reports: EVIDENCED" in rendered
    assert "semantic models: EVIDENCED" in rendered
    assert "source intent: NOT_EVIDENCED (no migration-spec.json found)" in rendered
    assert "handover queue: NOT_EVIDENCED (no handover/*.json slices found)" in rendered


def test_brownfield_canonical_bundle_stays_quiet() -> None:
    """A recognisable bundle does not gain brownfield guidance noise."""
    fixture = REPO_ROOT / "tests" / "fixtures" / "check-gates-dirty"

    rendered = cu.render(cu.run_all(fixture))

    assert "BROWNFIELD DISCOVERY" not in rendered


def test_page_count_mismatch_is_a_precondition_and_stops_before_oracle(tmp_path: Path) -> None:
    """Kills: treating missing pages as just another row and continuing into noisy page checks."""
    _write_spec(tmp_path, ["A", "B"])
    _write_report(tmp_path, ["A"])

    report = cu.run_all(tmp_path)

    assert report["status"] == cu.STATUS_PRECONDITION_FAILED
    assert report["exit_code"] == cu.EXIT_PRECONDITION_FAILED
    assert report["stopped_after"] == "page-parity"
    assert [check["id"] for check in report["checks"]] == ["page-parity"]


def test_page_count_deviation_requires_a_complete_exemption(tmp_path: Path) -> None:
    """The documented-why-not file is not a rubber stamp: missing fields are findings."""
    _write_spec(tmp_path, ["A", "B"])
    _write_report(tmp_path, ["A"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "B", "reason": "merged"}]}),
        encoding="utf-8",
    )

    report = cu.run_all(tmp_path)

    assert report["status"] == cu.STATUS_PRECONDITION_FAILED
    assert report["checks"][0]["id"] == "exemptions"
    assert report["checks"][0]["invalid"], "decided_by is required so exemptions are attributable"
    assert report["checks"][1]["status"] == cu.STATUS_PRECONDITION_FAILED


def test_page_count_deviation_with_attributed_exemption_continues(tmp_path: Path) -> None:
    """A named, reasoned, attributed dropped page is counted and does not block parity."""
    _write_spec(tmp_path, ["A", "B"])
    _write_report(tmp_path, ["A"])
    _write_reference_manifest(tmp_path, ["A"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {"exemptions": [{"check": "page-parity", "item": "B", "reason": "merged into A", "decided_by": "review"}]}
        ),
        encoding="utf-8",
    )

    report = cu.run_all(tmp_path)

    assert report["checks"][0]["status"] == cu.STATUS_PASS
    assert report["checks"][0]["exemptions"] == [{"id": "dash.1", "name": "B"}]
    assert report["exemptions"]["accepted"] == 1


def test_reference_manifest_reports_validation_grade(tmp_path: Path) -> None:
    """Kills: reporting oracle presence without the grade that says what it proves."""
    _write_spec(tmp_path, ["Executive"])
    _write_report(tmp_path, ["Executive"])
    _write_reference_manifest(tmp_path, ["Executive"])

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_PASS
    assert oracle["visual_present"] == 1
    assert oracle["numeric_present"] == 1
    assert oracle["grade"] == "validation-grade"


def test_oracle_capture_is_layout_text_only_and_counts_missing_numeric(tmp_path: Path) -> None:
    """Server oracle images are default-state layout/text evidence, not validation-grade proof."""
    _write_spec(tmp_path, ["Executive", "Detail"])
    _write_report(tmp_path, ["Executive", "Detail"])
    _write_oracle_manifest(tmp_path, ["Executive", "Detail"], images=True, data=False)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 2
    assert oracle["numeric_present"] == 0
    assert oracle["grade"] == "layout/text only (oracle capture, default view state)"


def test_stub_exemptions_subtract_only_named_attributed_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unexamined stubs and accepted untranslatable stubs are visibly different states."""
    payload = {
        "status": "STUBS",
        "models": [
            {"findings": [{"kind": "measure", "table": "Sales", "name": "Cannot Translate"}]},
            {"findings": [{"kind": "measure", "table": "Sales", "name": "Still Unexamined"}]},
        ],
    }
    check = {
        "id": "stub-measures",
        "status": cu.STATUS_FINDINGS,
        "native_status": "STUBS",
        "native_exit": 1,
        "payload": payload,
    }
    exemptions = {
        "entries": [
            {
                "check": "stub-measures",
                "item": "measure:Sales[Cannot Translate]",
                "reason": "source calc uses unsupported custom extension",
                "decided_by": "validator",
            }
        ]
    }

    updated = cu._apply_stub_exemptions(check, exemptions)  # pylint: disable=protected-access

    assert updated["status"] == cu.STATUS_FINDINGS
    assert updated["stub_exemptions"] == 1
    assert updated["unexempted_stubs"] == 1


def test_native_gate_skipped_is_not_a_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: the #276 false-green shape where an unrun sub-gate is folded into PASS."""
    _write_spec(tmp_path, ["Executive"])
    _write_report(tmp_path, ["Executive"])
    _write_reference_manifest(tmp_path, ["Executive"])
    monkeypatch.setattr(cu, "GATES", (_gate("pbir-valid", "check_pbir_valid.py"),))
    monkeypatch.setattr(
        cu,
        "_run_cli_gate",
        lambda *_args: {
            "id": "pbir-valid",
            "status": cu.STATUS_NOT_CHECKED,
            "native_status": "SKIPPED",
            "native_exit": 0,
        },
    )

    report = cu.run_all(tmp_path)

    assert report["status"] == cu.STATUS_NOT_CHECKED
    assert report["exit_code"] == cu.EXIT_NOT_CHECKED


def test_summary_line_counts_findings_and_not_checked_classes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The final line gives reviewers one stable aggregate to compare between runs."""
    _write_spec(tmp_path, ["Executive"])
    _write_report(tmp_path, ["Executive"])
    _write_reference_manifest(tmp_path, ["Executive"])
    monkeypatch.setattr(cu, "GATES", (_gate("sqlproxy-connections", "unused.py"),))
    monkeypatch.setattr(
        cu,
        "_run_cli_gate",
        lambda gate, *_args: {
            "id": gate.check_id,
            "status": cu.STATUS_FINDINGS,
            "native_status": "STUBS",
            "native_exit": 1,
        },
    )
    monkeypatch.setattr(
        cu,
        "claimed_only_checks",
        lambda: [{"id": "finalized", "status": cu.STATUS_NOT_CHECKED, "verification": "CLAIMED_ONLY"}],
    )

    rendered = cu.render(cu.run_all(tmp_path))

    assert rendered.splitlines()[-1] == (
        "SUMMARY: findings_by_owner=model=1; not_checked_structural=1; "
        "not_checked_missing_input=0; ladder=FINDINGS exit=1"
    )


def _gate(check_id: str = "x", script: str = "x.py") -> cu.Gate:
    return cu.Gate(
        check_id,
        script,
        (),
        frozenset({"OK"}),
        frozenset({0}),
        frozenset({"BAD"}),
        frozenset({1}),
    )


def _completed(argv: list[str], code: int, stdout: str = "", stderr: str = "") -> CompletedProcess[str]:
    return CompletedProcess(argv, code, stdout, stderr)


def test_registered_checks_are_scoped_without_vanishing() -> None:
    """Every check belongs somewhere, and all is exactly the full registry."""
    union = set()
    for scope in (cu.SCOPE_MODEL, cu.SCOPE_REPORT, cu.SCOPE_INTEGRATION):
        ids = cu._scope_check_ids(scope)  # pylint: disable=protected-access
        assert ids
        union.update(ids)
    assert union <= cu._scope_check_ids(cu.SCOPE_ALL)  # pylint: disable=protected-access
    assert cu._scope_check_ids(cu.SCOPE_ALL) == cu._all_check_ids()  # pylint: disable=protected-access
    assert cu.INTEGRATION_CHECK_IDS <= cu._scope_check_ids(cu.SCOPE_MODEL)  # pylint: disable=protected-access
    assert cu.INTEGRATION_CHECK_IDS <= cu._scope_check_ids(cu.SCOPE_REPORT)  # pylint: disable=protected-access


def test_cli_gate_missing_json_is_not_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing native JSON is an error state, never synthesized into PASS."""
    gate = _gate()
    monkeypatch.setattr(cu, "_run_simple", lambda argv: _completed(argv, 1, stderr="boom"))

    check = cu._run_cli_gate(gate, tmp_path, tmp_path)  # pylint: disable=protected-access

    assert check["status"] == cu.STATUS_NOT_CHECKED
    assert check["native_status"] == "ERROR"
    assert "missing" in check["detail"]


def test_cli_gate_invalid_json_is_not_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed native JSON means the facade could not form an opinion."""
    gate = _gate()
    (tmp_path / "x.json").write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(cu, "_run_simple", lambda argv: _completed(argv, 0))

    check = cu._run_cli_gate(gate, tmp_path, tmp_path)  # pylint: disable=protected-access

    assert check["status"] == cu.STATUS_NOT_CHECKED
    assert check["native_status"] == "ERROR"


def test_cli_gate_unknown_status_is_not_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A renamed native status must be registered before it can pass."""
    gate = _gate()
    (tmp_path / "x.json").write_text(json.dumps({"status": "RENAMED"}), encoding="utf-8")
    monkeypatch.setattr(cu, "_run_simple", lambda argv: _completed(argv, 0))

    check = cu._run_cli_gate(gate, tmp_path, tmp_path)  # pylint: disable=protected-access

    assert check["status"] == cu.STATUS_NOT_CHECKED
    assert "unexpected native status" in check["detail"]


def test_cli_gate_subprocess_import_failure_is_not_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Python import failure before JSON write is infrastructure failure, not findings or pass."""
    gate = _gate()
    monkeypatch.setattr(cu, "_run_simple", lambda argv: _completed(argv, 1, stderr="ModuleNotFoundError: nope"))

    check = cu._run_cli_gate(gate, tmp_path, tmp_path)  # pylint: disable=protected-access

    assert check["status"] == cu.STATUS_NOT_CHECKED
    assert "ModuleNotFoundError" in check["stderr"]


def test_cli_gate_nonzero_with_clean_payload_is_not_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Payload/exit disagreement cannot be accepted as clean."""
    gate = _gate()
    (tmp_path / "x.json").write_text(json.dumps({"status": "OK"}), encoding="utf-8")
    monkeypatch.setattr(cu, "_run_simple", lambda argv: _completed(argv, 1))

    check = cu._run_cli_gate(gate, tmp_path, tmp_path)  # pylint: disable=protected-access

    assert check["status"] == cu.STATUS_NOT_CHECKED
    assert "unexpected native status" in check["detail"]


def test_cli_gate_timeout_is_not_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeouts are infrastructure failures, never passes."""
    gate = _gate()

    def raise_timeout(argv: list[str]) -> CompletedProcess[str]:
        raise TimeoutExpired(argv, 1, output="partial", stderr="slow")

    monkeypatch.setattr(cu, "_run_simple", raise_timeout)

    check = cu._run_cli_gate(gate, tmp_path, tmp_path)  # pylint: disable=protected-access

    assert check["status"] == cu.STATUS_NOT_CHECKED
    assert check["native_exit"] == 124
    assert "timed out" in check["detail"]


def test_occlusion_missing_output_after_nonzero_is_not_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Occlusion cannot pass when the detector failed before producing JSON."""
    report = _write_report(tmp_path, ["Executive"])
    monkeypatch.setattr(cu, "check_occlusion", ORIGINAL_CHECK_OCCLUSION)
    monkeypatch.setattr(cu, "shipping_reports", lambda _target: [report])
    monkeypatch.setattr(cu, "_run_simple", lambda argv: _completed(argv, 1, stderr="import failed"))

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    check = cu.check_occlusion(tmp_path, output_dir)

    assert check["status"] == cu.STATUS_NOT_CHECKED
    assert check["reports"][0]["error"] == "native JSON output missing"


def test_occlusion_malformed_output_is_not_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed occlusion JSON is infrastructure failure, not clean output."""
    report = _write_report(tmp_path, ["Executive"])
    monkeypatch.setattr(cu, "check_occlusion", ORIGINAL_CHECK_OCCLUSION)
    monkeypatch.setattr(cu, "shipping_reports", lambda _target: [report])

    def write_bad_json(argv: list[str]) -> CompletedProcess[str]:
        Path(argv[-1]).write_text("not-json", encoding="utf-8")
        return _completed(argv, 1, stderr="bad json")

    monkeypatch.setattr(cu, "_run_simple", write_bad_json)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    check = cu.check_occlusion(tmp_path, output_dir)

    assert check["status"] == cu.STATUS_NOT_CHECKED
    assert "unreadable" in check["reports"][0]["error"]


def test_actual_pages_falls_back_to_page_directories_when_order_is_missing(tmp_path: Path) -> None:
    """Kills broad mutations of small helper returns that leave ordered fixtures unaffected."""
    report = _write_report(tmp_path, ["Executive"])
    (report / "definition" / "pages" / "pages.json").unlink()

    pages = cu.actual_pages(tmp_path)

    assert pages == [
        {
            "id": "p1",
            "name": "Executive",
            "report": str(report),
            "path": str(report / "definition" / "pages" / "p1" / "page.json"),
        }
    ]


def test_clean_input_exits_zero_even_with_claimed_only_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 0 must be reachable when only structurally-unverifiable claimed-only phases remain."""
    _write_spec(tmp_path, ["Executive"])
    _write_report(tmp_path, ["Executive"])
    _write_reference_manifest(tmp_path, ["Executive"])
    monkeypatch.setattr(
        cu,
        "claimed_only_checks",
        lambda: [
            {
                "id": "finalized",
                "status": cu.STATUS_NOT_CHECKED,
                "verification": "CLAIMED_ONLY",
                "detail": "no machine-readable completion artifact exists",
            }
        ],
    )

    report = cu.run_all(tmp_path)

    assert report["status"] == cu.STATUS_AUTOMATED_PASS
    assert report["exit_code"] == cu.EXIT_OK
    assert [check["id"] for check in report["checks"]][-1] == "finalized"


def test_scope_model_runs_only_model_layer_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A model-scope pass must not run report/orchestration gates or imply full unit sign-off."""
    _write_spec(tmp_path, ["Executive"])
    _write_report(tmp_path, ["Executive"])
    _write_reference_manifest(tmp_path, ["Executive"])
    monkeypatch.setattr(
        cu,
        "GATES",
        (
            _gate("stub-measures", "unused.py"),
            _gate("pbir-valid", "unused.py"),
        ),
    )
    monkeypatch.setattr(
        cu,
        "_run_cli_gate",
        lambda gate, *_args: {"id": gate.check_id, "status": cu.STATUS_PASS, "native_status": "OK", "native_exit": 0},
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)

    assert report["exit_code"] == cu.EXIT_OK
    assert "pbir-valid" in report["omitted_checks"]
    assert [check["id"] for check in report["checks"]] == [
        "stub-measures",
        "ai-descriptions",
        "ai-instructions",
        "cache-freshness",
    ]
    assert "omitted checks:" in cu.render(report)


def test_scope_report_runs_only_report_layer_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A report-scope run owns page/oracle/PBIR checks and skips model readiness checks."""
    _write_spec(tmp_path, ["Executive"])
    _write_report(tmp_path, ["Executive"])
    _write_reference_manifest(tmp_path, ["Executive"])
    monkeypatch.setattr(
        cu,
        "GATES",
        (
            _gate("stub-measures", "unused.py"),
            _gate("pbir-valid", "unused.py"),
        ),
    )
    monkeypatch.setattr(
        cu,
        "_run_cli_gate",
        lambda gate, *_args: {"id": gate.check_id, "status": cu.STATUS_PASS, "native_status": "OK", "native_exit": 0},
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_REPORT)

    assert report["exit_code"] == cu.EXIT_OK
    assert "empty-model" in report["omitted_checks"]
    assert [check["id"] for check in report["checks"]] == [
        "page-parity",
        "oracle-coverage",
        "pbir-valid",
        "occlusion",
    ]


def test_scope_all_keeps_model_report_and_orchestration_checks(tmp_path: Path) -> None:
    """The default scope preserves the historical aggregate view plus all-only claimed phases."""
    _write_spec(tmp_path, ["Executive"])
    _write_report(tmp_path, ["Executive"])
    _write_reference_manifest(tmp_path, ["Executive"])

    report = cu.run_all(tmp_path, scope=cu.SCOPE_ALL)

    ids = [check["id"] for check in report["checks"]]
    assert "page-parity" in ids
    assert "oracle-coverage" in ids
    assert "ai-descriptions" in ids
    assert "cache-freshness" in ids
    assert "desktop-orphans" in ids
    assert report["omitted_checks"] == []


def test_gate_registrations_match_native_exit_constants() -> None:
    """Registration drift must fail before a native finding is downgraded or a clean gate cannot pass."""
    blank = _load_script_module("check_blank_placeholders.py")
    empty = _load_script_module("check_empty_model.py")
    sqlproxy = _load_script_module("check_sqlproxy_connections.py")
    relationship = _load_script_module("check_relationship_health.py")
    layout = _load_script_module("check_pbir_layout.py")
    stubs = _load_script_module("check_stub_measures.py")

    gate = _gate_by_id("blank-placeholders")
    assert gate.pass_statuses == {blank.STATUS_OK}
    assert gate.pass_exit_codes == {blank.EXIT_OK}
    assert gate.finding_statuses == {blank.STATUS_REFERENCED, blank.STATUS_UNREFERENCED}
    assert gate.finding_exit_codes == {blank.EXIT_REFERENCED, blank.EXIT_UNREFERENCED}
    assert gate.not_checked_statuses == {blank.STATUS_INCOMPLETE}
    assert gate.not_checked_exit_codes == {blank.EXIT_INCOMPLETE}

    gate = _gate_by_id("empty-model")
    assert gate.pass_statuses == {empty.STATUS_OK}
    assert gate.pass_exit_codes == {empty.EXIT_OK}
    assert gate.finding_statuses == {empty.STATUS_EMPTY_MODELS}
    assert empty.EXIT_EMPTY_MODEL in gate.finding_exit_codes
    assert gate.not_checked_statuses == {empty.STATUS_SKIPPED}
    assert gate.not_checked_exit_codes == {empty.EXIT_SKIPPED}

    for check_id, module, finding_status, finding_exit in (
        ("sqlproxy-connections", sqlproxy, sqlproxy.STATUS_SQLPROXY, sqlproxy.EXIT_SQLPROXY),
        ("relationship-health", relationship, relationship.STATUS_MISSING, relationship.EXIT_MISSING),
        ("pbir-layout", layout, layout.STATUS_DISPLACED, layout.EXIT_DISPLACED),
        ("stub-measures", stubs, stubs.STATUS_STUBS, stubs.EXIT_STRICT),
    ):
        gate = _gate_by_id(check_id)
        assert gate.pass_statuses == {module.STATUS_OK}
        assert gate.pass_exit_codes == {module.EXIT_OK}
        assert gate.finding_statuses == {finding_status}
        assert gate.finding_exit_codes == {finding_exit}
        if hasattr(module, "STATUS_SKIPPED"):
            assert module.STATUS_SKIPPED in gate.not_checked_statuses
            assert module.EXIT_SKIPPED in gate.not_checked_exit_codes

    gate = _gate_by_id("data-model")
    assert gate.pass_statuses == {"OK"}
    assert gate.pass_exit_codes == {0}
    assert gate.finding_statuses == {"FINDINGS"}
    assert gate.not_checked_statuses == {"ERROR"}


def test_cli_model_scope_exits_zero_on_committed_clean_fixture() -> None:
    """Subprocess-level proof that model scope can go green with real native gate wiring."""
    fixture = _freshen_clean_fixture_cache()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_unit.py"), str(fixture), "--scope", "model"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == cu.EXIT_OK, result.stdout + result.stderr
    assert "AUTOMATED_CHECKS_PASS" in result.stdout
    assert "cache-freshness: PASS - mtime-only partial check" in result.stdout
    assert "data-model: PASS" in result.stdout


def test_cli_integration_scope_exits_zero_on_committed_clean_fixture() -> None:
    """Subprocess-level exit-0 proof with real native gate wiring, not monkeypatched passes."""
    fixture = REPO_ROOT / "tests" / "fixtures" / "check-unit-clean-integration"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_unit.py"), str(fixture), "--scope", "integration"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == cu.EXIT_OK, result.stdout + result.stderr
    assert "AUTOMATED_CHECKS_PASS" in result.stdout
    assert "omitted checks:" in result.stdout


def test_cli_model_scope_empty_semantic_model_is_not_a_vacuous_pass(tmp_path: Path) -> None:
    """Subprocess regression for a customer folder containing a cache-only semantic model."""
    model = tmp_path / "CacheOnly.SemanticModel"
    model.mkdir()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_unit.py"), str(model), "--scope", "model"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == cu.EXIT_FINDINGS, result.stdout + result.stderr
    assert "blank-placeholders: NOT_CHECKED" in result.stdout
    assert "sqlproxy-connections: NOT_CHECKED" in result.stdout
    assert "relationship-health: NOT_CHECKED" in result.stdout
    assert "empty-model: NOT_CHECKED" in result.stdout
    assert "ai-descriptions: NOT_CHECKED" in result.stdout
    assert "SUMMARY:" in result.stdout


def test_cli_missing_path_is_usage_not_a_mutation_success(tmp_path: Path) -> None:
    """The mutation harness must distinguish expected usage failure from arbitrary command failure."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_unit.py"), str(tmp_path / "missing")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == cu.EXIT_USAGE
    assert "ERROR: not a directory" in result.stderr
    assert "UNIT CHECK" not in result.stdout
