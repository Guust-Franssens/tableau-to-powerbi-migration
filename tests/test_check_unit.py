"""Tests for scripts/check_unit.py - the per-unit derive-first façade from #291.

Fixtures use the real emitted artifact shapes: migration-spec dashboards, PBIR page/page-order JSON,
reference capture manifests, and Tableau Server oracle manifests. The tests avoid treating command
execution failures as caught mutations; when a subprocess is used, the assertion checks the intended
exit code and output shape rather than any non-zero result.
"""

from __future__ import annotations

import importlib.util
import importlib
import copy
import io
import hashlib
import inspect
import json
import os
import time
import shutil
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import fields

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import bundle_corpus  # noqa: E402  # pylint: disable=wrong-import-position
import check_unit as cu  # noqa: E402  # pylint: disable=wrong-import-position
import check_field_bindings  # noqa: E402  # pylint: disable=wrong-import-position
import object_identity as oid  # noqa: E402  # pylint: disable=wrong-import-position
import read_handover  # noqa: E402  # pylint: disable=wrong-import-position
import run_estate  # noqa: E402  # pylint: disable=wrong-import-position

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


UNIT_LUID = "adc431bb-aeeb-43fe-8ecb-092d4bae8bfa"
OTHER_LUID = "007f70ac-bf40-4838-9d73-134d40f504db"


def _write_spec(unit: Path, names: list[str]) -> None:
    """A dashboards-only migration spec, with the schema-required empty `worksheets` array."""
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "migration-spec.json").write_text(
        json.dumps(
            {
                "source": {"file_name": f"{UNIT_LUID}_Book.twbx"},
                "dashboards": [{"id": f"dash.{index}", "name": name} for index, name in enumerate(names)],
                "worksheets": [],
            }
        ),
        encoding="utf-8",
    )


def _write_report(unit: Path, names: list[str], *, visuals: int = 1, name: str = "Book") -> Path:
    """A PBIR report with `visuals` visual.json files per page (a real page has at least one)."""
    report = unit / "fabric" / f"{name}.Report"
    pages = report / "definition" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    order = []
    for index, page_name in enumerate(names):
        page_id = f"p{index + 1}"
        order.append(page_id)
        page = pages / page_id
        page.mkdir()
        (page / "page.json").write_text(
            json.dumps({"name": page_id, "displayName": page_name, "width": 1600, "height": 900}),
            encoding="utf-8",
        )
        _write_visuals(page, visuals)
    (pages / "pages.json").write_text(json.dumps({"pageOrder": order}), encoding="utf-8")
    return report


def _write_exemptions(unit: Path, entries: list[dict[str, str]]) -> None:
    """A signed exemptions file; ``reason``/``decided_by`` default so a test can name only the item."""
    unit.mkdir(parents=True, exist_ok=True)
    payload = [
        {"reason": "accepted for this proof of concept", "decided_by": "migration lead", **entry} for entry in entries
    ]
    (unit / "unit-check-exemptions.json").write_text(json.dumps({"exemptions": payload}), encoding="utf-8")


def _write_visuals(page: Path, count: int) -> None:
    for index in range(count):
        visual = page / "visuals" / f"v{index}"
        visual.mkdir(parents=True, exist_ok=True)
        (visual / "visual.json").write_text(json.dumps({"name": f"v{index}"}), encoding="utf-8")


def _png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)


def _csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a\n1\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_unit_source(unit: Path, blob: bytes, *, name: str = f"{UNIT_LUID}_Book.twbx", handover: bool = True) -> Path:
    """The Tableau asset this unit was built from, recorded where BOTH producers record it.

    A handover slice's ``workbook.source_id`` and `migration-spec.json`'s ``source.file_name`` are
    the two independent claims `check_unit._unit_source_claims` reads, so the fixture writes both -
    a fixture that recorded only one could not tell a working resolver from one that happened to
    read the other.
    """
    asset = unit / "assets" / name
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(blob)
    if handover:
        slices = unit / "handover"
        slices.mkdir(parents=True, exist_ok=True)
        (slices / "Book.json").write_text(
            json.dumps({"estate": {}, "workbook": {"name": "Book", "source_id": f"assets/{name}"}}), encoding="utf-8"
        )
    spec = unit / "migration-spec.json"
    if spec.is_file():
        payload = json.loads(spec.read_text(encoding="utf-8"))
        payload["source"] = {"file_name": name}
        spec.write_text(json.dumps(payload), encoding="utf-8")
    return asset


def _write_reference_manifest(
    unit: Path,
    names: list[str],
    *,
    numeric: bool = True,
    workbook: str | None = None,
    source_sha: str | None = "auto",
    workbook_luid: str | None = None,
) -> None:
    """A `reference/manifest.json`.

    ⚠️ ``source_sha="auto"`` writes the unit's source asset and records ITS hash, which is what
    `capture_tableau_reference.py:234` does. Round-3 review, B-B: a display name no longer certifies
    anything, so a fixture identifying a reference record by ``workbook`` alone stopped representing
    any real capture - it now represents a hand-edited one, which is a different test.
    """
    if source_sha == "auto":
        # No handover slice: the migration spec's `source.file_name` already establishes the
        # asset, and writing a slice here would put an unrelated check into every test that
        # merely wanted a reference manifest.
        source_sha = _sha256(_write_unit_source(unit, b"the workbook this unit was built from", handover=False))
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
    payload: dict[str, object] = {"dashboards": dashboards}
    if workbook is not None:
        payload["workbook"] = workbook
    if workbook_luid is not None:
        payload["workbook_luid"] = workbook_luid
    if source_sha is not None:
        payload["source_workbook_sha256"] = source_sha
    (unit / "reference" / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_oracle_manifest(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    unit: Path,
    names: list[str],
    *,
    images: bool = True,
    data: bool = True,
    workbook: str | None = "Book",
    view_type: str | None = "dashboard",
    workbook_luid: str | None = UNIT_LUID,
    workbook_name: str | None = None,
) -> None:
    """An oracle manifest.

    ⚠️ ``workbook`` writes a TOP-LEVEL ``workbook`` key, which is a synthetic shape: the real producer
    writes ``workbook_luid`` and ``workbook_name`` PER VIEW and no ``workbook`` anywhere. That gap is
    issue #450 - the gate read the key the fixtures wrote instead of the ones the capture writes, so
    every record on a live capture arrived ownerless (measured 360 of 360) and the guard never fired.
    ``workbook_luid``/``workbook_name`` write the real shape; both are kept so the synthetic override
    stays testable too.
    """
    records = []
    for index, name in enumerate(names):
        image_path = f"images/{name}__{index}.png"
        data_path = f"data/{name}__{index}.csv"
        if images:
            _png(unit / "_oracle" / image_path)
        if data:
            _csv(unit / "_oracle" / data_path)
        record: dict[str, object] = {
            "view_name": name,
            # ⚠️ `certification` is not decoration. Since #480 round 3 a `row_count` alone no longer
            # licenses an evidence `path` -- every pre-certification manifest carries one, so
            # trusting it is a gate that never fires on real data -- and this fixture stands for a
            # CURRENT, certified capture. `test_a_legacy_uncertified_record_is_not_numeric_evidence`
            # holds the other end.
            "data": {"status": "ok", "certification": "certified", "path": data_path, "row_count": 1}
            if data
            else {"status": "failed"},
            "image": {"status": "ok", "path": image_path} if images else {"status": "failed"},
        }
        if view_type is not None:
            record["view_type"] = view_type
        if workbook_luid is not None:
            record["workbook_luid"] = workbook_luid
        if workbook_name is not None:
            record["workbook_name"] = workbook_name
        records.append(record)
    (unit / "_oracle").mkdir(exist_ok=True)
    payload: dict[str, object] = {"views": records}
    if workbook is not None:
        payload["workbook"] = workbook
    (unit / "_oracle" / "oracle-manifest.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture(autouse=True)
def no_native_gates(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Most tests isolate check_unit's new logic; native gate wiring is tested separately."""
    if request.node.name.startswith("test_r2_"):
        return
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
    assert f"source{os.sep}migration-spec.json" in rendered
    assert f"PowerBI{os.sep}Admin_Insights_Starter.Report" in rendered
    assert f"PowerBI{os.sep}Admin_Insights_Starter.SemanticModel" in rendered
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


def test_brownfield_canonical_bundle_proposes_no_reorganisation() -> None:
    """A recognisable bundle is never told to reorganise itself.

    This test used to assert the whole BROWNFIELD block was absent, and it passed only because
    ``page-parity`` raised a FALSE ``PRECONDITION_FAILED`` on this fixture (its ``migration-spec.json``
    is the placeholder ``{"workbook": ...}``, which declares no pages, so "0 expected vs 1 emitted"
    read as an extra page) and the run aborted before any NOT_CHECKED row existed. ``_render_brownfield``
    deliberately prints the inventory whenever a check reports a missing input - a NOT_CHECKED row
    often IS a misplaced input - so no correct page-parity can keep that block hidden here. What the
    test actually cared about is asserted directly instead: recognised shape, and no reorganisation
    plan.
    """
    fixture = REPO_ROOT / "tests" / "fixtures" / "check-gates-dirty"

    report = cu.run_all(fixture)
    rendered = cu.render(report)

    assert report["brownfield"]["recognized_target_shape"] is True
    assert report["brownfield"]["plan"] == []
    assert "reorganisation plan (not applied): no recognised artifacts to place" in rendered
    parity = next(check for check in report["checks"] if check["id"] == "page-parity")
    assert parity["status"] == cu.STATUS_NOT_CHECKED, "a placeholder spec declares nothing; that is not an extra page"
    assert report["stopped_after"] is None


def _write_full_spec(
    unit: Path,
    dashboards: list[tuple[str, list[str]]],
    worksheets: list[tuple[str, str]],
) -> None:
    """Write a migration-spec with dashboard zone trees, as parse_tableau.py emits them.

    ``dashboards`` is ``[(dashboard name, [worksheet ids placed on it])]`` and ``worksheets`` is
    ``[(worksheet id, worksheet name)]``. The placed ids are nested one level deep so the walk is
    exercised on a tree, not a flat list - the real parser nests zones several layers.
    """
    unit.mkdir(parents=True, exist_ok=True)
    spec_dashboards = []
    for index, (name, placed) in enumerate(dashboards):
        children = [
            {"id": f"z{position}", "worksheet_id": ws_id, "children": []} for position, ws_id in enumerate(placed)
        ]
        spec_dashboards.append(
            {
                "id": f"dash.{index}",
                "name": name,
                "zones": {
                    "id": "root",
                    "worksheet_id": None,
                    "children": [{"id": "flow", "worksheet_id": None, "children": children}],
                },
            }
        )
    (unit / "migration-spec.json").write_text(
        json.dumps(
            {
                "source": {"file_name": f"{UNIT_LUID}_Book.twbx"},
                "dashboards": spec_dashboards,
                "worksheets": [{"id": ws_id, "name": name} for ws_id, name in worksheets],
            }
        ),
        encoding="utf-8",
    )


def _write_viz_fidelity_handover(
    unit: Path,
    rows: list[dict[str, object]],
    pbip_warnings: list[str] | None = None,
    workbook_name: str = "Book",
) -> None:
    """Handover slice carrying engine-declared per-page rebuild rows.

    ``workbook_name`` defaults to ``Book`` because that is the stem of the ``Book.Report`` folder
    ``_write_report`` creates: a slice only explains pages for a workbook this unit actually ships.
    """
    workbook: dict[str, object] = {"name": workbook_name, "viz_fidelity": rows}
    if pbip_warnings is not None:
        workbook["pbip_warnings"] = pbip_warnings
    _write_handover(unit, workbook)


def _empty_row(name: str, reason: str = "manual attention required: unsupported") -> dict[str, object]:
    """A viz_fidelity row that structurally asserts NO page was emitted (tier 'empty')."""
    return {"worksheet": name, "visual_type": "unsupported", "status": "warned", "tier": "empty", "reason": reason}


def test_expected_pages_counts_orphan_worksheets_not_just_dashboards(tmp_path: Path) -> None:
    """Kills: the dashboards-only page rule the engine has never used.

    twb_to_pbir.py (2.339.0) emits a page per dashboard AND a page per worksheet that no dashboard
    placed (:14557-14558 skip-if-placed, :14709 page_order.append). Measured on a real 2.339.0
    estate run, 19 of 43 workbooks have ZERO dashboards, so the old rule returned an empty expected
    set for nearly half the estate.
    """
    _write_full_spec(
        tmp_path,
        dashboards=[("Exec", ["ws.placed"])],
        worksheets=[("ws.placed", "Placed Sheet"), ("ws.loose", "Loose Sheet")],
    )

    names = [page["name"] for page in cu.expected_pages(tmp_path) or []]

    assert names == ["Exec", "Loose Sheet"], "a dashboard's own sheets are not pages; a loose sheet is"


def test_expected_pages_finds_placed_worksheets_at_any_zone_depth(tmp_path: Path) -> None:
    """Kills: a non-recursive zone walk that calls every nested sheet an orphan.

    The loose sheet is here so the assertion cannot also be satisfied by the old dashboards-only
    rule: that rule returns ``["Exec"]``, which is neither this expectation nor the flat-walk one.
    """
    _write_full_spec(
        tmp_path,
        dashboards=[("Exec", ["ws.deep"])],
        worksheets=[("ws.deep", "Deep Sheet"), ("ws.loose", "Loose Sheet")],
    )

    assert [page["name"] for page in cu.expected_pages(tmp_path) or []] == ["Exec", "Loose Sheet"]


def test_workbook_with_no_dashboards_expects_its_worksheets(tmp_path: Path) -> None:
    """A dashboard-less workbook is the common engine case, not an empty expectation."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A", "B"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PASS
    assert parity["expected_count"] == 2


def test_engine_evidence_explains_an_omission_but_does_not_accept_it(tmp_path: Path) -> None:
    """Kills: treating proof of an absence as acceptance of one.

    tier 'empty' proves the ENGINE emitted no faithful visual. It says nothing about whether a human
    agreed to ship without that page. Measured before this: a unit missing page B returned PASS from
    parity AND validation-grade oracle coverage, with compromises=0.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(tmp_path, [_empty_row("B")])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED
    assert [row["disposition"] for row in parity["unsigned_omissions"]] == [cu.OMISSION_DECLARED]
    assert parity["applied_exemptions"] == []
    assert "does not accept it" in parity["unsigned_omissions"][0]["why"]


def test_a_signed_omission_with_engine_evidence_passes_and_counts_as_a_compromise(tmp_path: Path) -> None:
    """The other half: a human signature accepts the omission, and it is VISIBLE as a compromise."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(tmp_path, [_empty_row("B")])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {"exemptions": [{"check": "page-parity", "item": "B", "reason": "unsupported mark", "decided_by": "gf"}]}
        ),
        encoding="utf-8",
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_REPORT)
    parity = next(check for check in report["checks"] if check["id"] == "page-parity")

    assert parity["status"] == cu.STATUS_PASS
    assert [page["name"] for page in parity["applied_exemptions"]] == ["B"]
    assert cu._compromise_count(report) == 1  # pylint: disable=protected-access


def test_oracle_coverage_still_expects_a_page_the_engine_merely_declared(tmp_path: Path) -> None:
    """The same rule on the oracle denominator: only a SIGNED omission takes a page out."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_oracle_manifest(tmp_path, ["A"], view_type="worksheet")
    _write_viz_fidelity_handover(tmp_path, [_empty_row("B")])

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["pages"] == 2, "B is declared, not accepted; it stays in the denominator"
    assert [page["name"] for page in oracle["visual_missing"]] == ["B"]
    assert oracle["status"] == cu.STATUS_NOT_CHECKED


def test_oracle_coverage_drops_a_signed_omission_from_the_denominator(tmp_path: Path) -> None:
    """A page a human accepted losing has nothing to hold against a reference."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_oracle_manifest(tmp_path, ["A"], view_type="worksheet")
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "B", "reason": "dropped", "decided_by": "gf"}]}),
        encoding="utf-8",
    )

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["pages"] == 1
    assert [page["name"] for page in oracle["excluded_omissions"]] == ["B"]
    assert oracle["status"] == cu.STATUS_PASS


def test_two_same_named_candidates_cannot_share_one_rendered_page(tmp_path: Path) -> None:
    """Kills: a kind-less page name satisfying more than one expected page.

    A PBIR page names an object without saying what KIND it is. With dashboard 'Sales' and worksheet
    'Sales' both expected, one rendered 'Sales' page used to satisfy BOTH: PASS at expected_count=2,
    emitted_count=1. It attributes to neither.
    """
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[("ws.sales", "Sales")])
    _write_report(tmp_path, ["Sales"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["contested_names"] == ["Sales"]
    assert [row["name"] for row in parity["omissions"]] == ["Sales", "Sales"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_one_oracle_row_cannot_cover_two_same_named_candidates(tmp_path: Path) -> None:
    """The same rule on the oracle side: contested names take no evidence at all.

    One reference row named 'Sales' was counted as 2-of-2 coverage for a dashboard AND a worksheet.
    """
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[("ws.sales", "Sales")])
    _write_report(tmp_path, ["Sales"])
    _write_oracle_manifest(tmp_path, ["Sales"], view_type=None)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["pages"] == 2
    assert oracle["visual_present"] == 0, "one picture cannot prove two different objects"
    assert oracle["contested_names"] == ["Sales", "Sales"]
    assert oracle["status"] == cu.STATUS_NOT_CHECKED


def test_typed_oracle_records_cover_same_named_dashboard_and_worksheet(tmp_path: Path) -> None:
    """Typed evidence resolves the expected page by its kind, not by its contested bare name."""
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[("ws.sales", "Sales")])
    _write_report(tmp_path, ["Sales", "Sales"])
    _write_oracle_manifest(tmp_path, ["Sales"], view_type="dashboard")

    manifest_path = tmp_path / "_oracle" / "oracle-manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    image = "images/Sales__worksheet.png"
    data = "data/Sales__worksheet.csv"
    _png(tmp_path / "_oracle" / image)
    _csv(tmp_path / "_oracle" / data)
    payload["views"].append(
        {
            "view_name": "Sales",
            "view_type": "worksheet",
            "workbook_luid": UNIT_LUID,
            "image": {"status": "ok", "path": image},
            # ⚠️ `certification` for the same reason `_write_oracle_manifest` writes it: since #480
            # round 3 a `row_count` alone no longer licenses an evidence `path`, and `read_manifest`
            # demotes an uncertified leg to `retained_path` -- so a hand-built record without one is
            # a LEGACY capture, and would make this test assert the certification rule rather than
            # the typed-resolution rule it exists for. The uncertified end is held, deliberately, by
            # `test_a_legacy_uncertified_record_is_not_numeric_evidence`.
            "data": {"status": "ok", "certification": "certified", "path": data, "row_count": 1},
        }
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_PASS
    assert oracle["visual_present"] == 2
    assert oracle["numeric_present"] == 2
    assert oracle["contested_names"] == []


def test_unknown_typed_oracle_record_stays_contested_for_same_named_pages(tmp_path: Path) -> None:
    """Unknown kind is a refusal and cannot choose between same-named expected pages."""
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[("ws.sales", "Sales")])
    _write_report(tmp_path, ["Sales", "Sales"])
    _write_oracle_manifest(tmp_path, ["Sales"], view_type="unknown")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["contested_names"] == ["Sales", "Sales"]
    assert oracle["kindless_evidence"] == 1


def test_typed_oracle_record_cannot_cover_ambiguous_same_kind_pages(tmp_path: Path) -> None:
    """Typed evidence still refuses when more than one expected page has that exact identity."""
    _write_full_spec(tmp_path, dashboards=[("Sales", []), ("Sales", [])], worksheets=[])
    _write_report(tmp_path, ["Sales", "Sales"])
    _write_oracle_manifest(tmp_path, ["Sales"], view_type="dashboard")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["numeric_present"] == 0
    assert oracle["contested_names"] == ["Sales", "Sales"]


def test_one_name_only_signature_cannot_sign_two_omissions(tmp_path: Path) -> None:
    """Kills: a bare-name exemption accepting every candidate that happens to share the name.

    Deliberately built so nothing else is ambiguous - 'A' is paired, so there is no unmatched
    rendered page - leaving the contested SIGNATURE as the only thing that can turn this test.
    """
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[("ws.sales", "Sales"), ("ws.a", "A")])
    _write_report(tmp_path, ["A"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "Sales", "reason": "x", "decided_by": "gf"}]}),
        encoding="utf-8",
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_REPORT)
    parity = next(check for check in report["checks"] if check["id"] == "page-parity")

    assert parity["applied_exemptions"] == []
    assert len(parity["unsigned_omissions"]) == 2
    assert [row["disposition"] for row in parity["unapplied_exemptions"]] == [cu.EXEMPTION_AMBIGUOUS] * 2
    assert cu._compromise_count(report) == 0  # pylint: disable=protected-access
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_signature_naming_the_page_id_resolves_a_contested_name(tmp_path: Path) -> None:
    """The way out: an id is per-object, so signing the id accepts exactly one of the two."""
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[("ws.sales", "Sales"), ("ws.a", "A")])
    _write_report(tmp_path, ["A"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {
                "exemptions": [
                    {"check": "page-parity", "item": "dash.0", "reason": "dashboard cut", "decided_by": "gf"},
                    {"check": "page-parity", "item": "ws.sales", "reason": "sheet cut", "decided_by": "gf"},
                ]
            }
        ),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert {row["kind"] for row in parity["applied_exemptions"]} == {"dashboard", "worksheet"}
    assert parity["unsigned_omissions"] == []
    assert parity["status"] == cu.STATUS_PASS


def test_evidence_is_found_from_a_relative_target_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: de-duplicating handover roots BEFORE resolving them.

    `_unit_dir` resolves its return value while `target` keeps the caller's spelling, so with the
    documented relative CLI invocation the same directory was scanned twice, every evidence row was
    indexed twice, and each became an AMBIGUOUS resolution whose declared reason vanished. The
    absolute form happened to work, which is how it survived review.
    """
    unit = tmp_path / "unit"
    _write_full_spec(unit, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(unit, ["A"])
    _write_viz_fidelity_handover(unit, [_empty_row("B")])
    monkeypatch.chdir(tmp_path)

    absolute = cu.check_page_parity(unit.resolve(), cu.load_exemptions(unit.resolve()))
    relative = cu.check_page_parity(Path("unit"), cu.load_exemptions(Path("unit")))

    assert absolute["omissions"][0]["declared_reason"] is not None
    assert relative["omissions"][0]["declared_reason"] == absolute["omissions"][0]["declared_reason"]
    assert relative["omissions"][0]["disposition"] == cu.OMISSION_DECLARED


def _complete_worksheet(ws_id: str, name: str, **overrides: object) -> dict[str, object]:
    """A worksheet carrying EVERY property the committed spec schema declares, all content empty.

    Completeness is the point: `_source_empty` now refuses a partial structure, because treating an
    incomplete one as proof of emptiness is what let a Text worksheet with `title_text: "Important
    instructions"` pass as owing no page. Real parser output carries all eleven properties, so this
    is what a genuinely blank sheet looks like on disk.
    """
    worksheet: dict[str, object] = {
        "id": ws_id,
        "name": name,
        "title_text": None,
        "data_source_ids": [],
        "mark_type": "Automatic",
        "encodings": {
            "rows": [],
            "columns": [],
            "color": None,
            "size": None,
            "shape": None,
            "label": [],
            "detail": [],
            "tooltip": [],
        },
        "reference_lines": [],
        "filters": [],
        "manual_sort": [],
        "measure_names_values_pivot": None,
        "customized_tooltip_text": None,
    }
    worksheet.update(overrides)
    return worksheet


def _spec_with_worksheets(unit: Path, worksheets: list[dict[str, object]]) -> None:
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "migration-spec.json").write_text(
        json.dumps(
            {
                "migration_spec_version": "1.0",
                "source": {"file_name": f"{UNIT_LUID}_Book.twbx"},
                "dashboards": [],
                "worksheets": worksheets,
            }
        ),
        encoding="utf-8",
    )


def test_a_source_empty_worksheet_owes_no_page(tmp_path: Path) -> None:
    """A worksheet with every schema channel empty renders blank in Tableau too, so it owes no page.

    Corrects a round-2 claim of mine that this case did not occur: two exist in the measured estate
    (`Meridian Multi-Source (3 systems)/Probe Sheet`, `vishnu_dashboard/Sheet 3`), both complete and
    entirely empty. Established from the SPEC, never from an engine tier.
    """
    _spec_with_worksheets(
        tmp_path,
        [
            _complete_worksheet("ws.a", "A", encodings={"rows": ["x"], "columns": [], "color": None}),
            _complete_worksheet("ws.probe", "Probe Sheet"),
        ],
    )
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [row["name"] for row in parity["source_empty_omissions"]] == ["Probe Sheet"]
    assert parity["unsigned_omissions"] == []
    assert parity["status"] == cu.STATUS_PASS


def test_an_emptied_oracle_denominator_still_names_why_it_emptied(tmp_path: Path) -> None:
    """Kills: dropping the exclusion list on the not-assessable early return.

    A workbook whose only expected page is source-empty empties the denominator legitimately. The
    early return used to report `excluded_omissions: []`, so a page BOTH halves had accepted looked
    like the two halves disagreeing. Found by the estate cross-check, not by a test.
    """
    _spec_with_worksheets(tmp_path, [_complete_worksheet("ws.b", "B")])
    _write_report(tmp_path, ["B"], visuals=0)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert [page["name"] for page in oracle["excluded_omissions"]] == ["B"]
    assert "owes no output" in oracle["detail"]


def test_a_source_empty_page_owes_no_oracle_evidence_either(tmp_path: Path) -> None:
    """Kills: page parity and the oracle denominator disagreeing about the same page.

    Parity accepted a source-empty omission while the oracle still demanded a visual AND a numeric
    oracle for it, so one page could PASS one half of the gate and be NOT_CHECKED in the other. Both
    now read the same disposition.
    """
    _spec_with_worksheets(
        tmp_path,
        [
            _complete_worksheet("ws.a", "A", encodings={"rows": ["x"], "columns": [], "color": None}),
            _complete_worksheet("ws.b", "B"),
        ],
    )
    _write_report(tmp_path, ["A"])
    _write_oracle_manifest(tmp_path, ["A"], view_type="worksheet")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert [page["name"] for page in oracle["excluded_omissions"]] == ["B"]
    assert oracle["pages"] == 1
    assert oracle["status"] == cu.STATUS_PASS


def test_a_visible_title_is_content_even_with_every_shelf_empty(tmp_path: Path) -> None:
    """Kills: inspecting only the encoding shelves, and missing a visible channel.

    A Text worksheet whose title reads "Important instructions" draws something. It passed as
    source-empty, and because that disposition needs no signature the false positive was a silent PASS.
    """
    _spec_with_worksheets(
        tmp_path,
        [
            _complete_worksheet("ws.a", "A", encodings={"rows": ["x"], "columns": [], "color": None}),
            _complete_worksheet("ws.b", "B", title_text="Important instructions"),
        ],
    )
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["source_empty_omissions"] == []
    assert [row["name"] for row in parity["unsigned_omissions"]] == ["B"]


def test_an_incomplete_worksheet_structure_is_not_proof_of_emptiness(tmp_path: Path) -> None:
    """Kills: reading a partial structure as proof. Unknown is not empty."""
    _spec_with_worksheets(
        tmp_path,
        [
            _complete_worksheet("ws.a", "A", encodings={"rows": ["x"], "columns": [], "color": None}),
            {"id": "ws.b", "name": "B", "encodings": {"rows": []}},
        ],
    )
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["source_empty_omissions"] == []
    assert [row["name"] for row in parity["unsigned_omissions"]] == ["B"]


def test_a_worksheet_missing_one_schema_key_is_not_proof_of_emptiness(tmp_path: Path) -> None:
    """The completeness rule is per-KEY: dropping `filters` alone must still refuse the claim."""
    partial = _complete_worksheet("ws.b", "B")
    del partial["filters"]
    _spec_with_worksheets(
        tmp_path,
        [_complete_worksheet("ws.a", "A", encodings={"rows": ["x"], "columns": [], "color": None}), partial],
    )
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["source_empty_omissions"] == []
    assert [row["name"] for row in parity["unsigned_omissions"]] == ["B"]


def test_an_unrecognised_spec_version_cannot_be_classified_as_empty(tmp_path: Path) -> None:
    """The classification is derived against one schema version; another cannot be read with it."""
    (tmp_path / "migration-spec.json").write_text(
        json.dumps(
            {
                "migration_spec_version": "2.0",
                "dashboards": [],
                "worksheets": [
                    _complete_worksheet("ws.a", "A", encodings={"rows": ["x"], "columns": [], "color": None}),
                    _complete_worksheet("ws.b", "B"),
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["source_empty_omissions"] == []
    assert [row["name"] for row in parity["unsigned_omissions"]] == ["B"]


def test_a_punctuation_variant_name_is_not_the_same_signature(tmp_path: Path) -> None:
    """Kills: deciding a signature through the lossy slug, where 'A-B' and 'A B' are one name."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.1", "A-B"), ("ws.2", "A B"), ("ws.3", "C")])
    _write_report(tmp_path, ["C"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "A-B", "reason": "cut", "decided_by": "gf"}]}),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [page["name"] for page in parity["applied_exemptions"]] == ["A-B"]
    assert [row["name"] for row in parity["unsigned_omissions"]] == ["A B"], "'A B' was never signed"
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_an_extra_signature_matches_the_page_name_exactly(tmp_path: Path) -> None:
    """The same rule for `extra:`: a punctuation variant does not account for a rendered page."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_report(tmp_path, ["A", "Bonus-Page"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {"exemptions": [{"check": "page-parity", "item": "extra:Bonus Page", "reason": "x", "decided_by": "gf"}]}
        ),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [page["name"] for page in parity["unaccounted_extra_pages"]] == ["Bonus-Page"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_spec_that_reuses_a_page_id_cannot_be_graded(tmp_path: Path) -> None:
    """An id is the only way to sign one of two same-named objects, so a colliding id is fatal.

    Measured: two pages sharing id 'ws.same' were BOTH signed by one 'ws.same' entry, and passed.
    """
    (tmp_path / "migration-spec.json").write_text(
        json.dumps(
            {
                "dashboards": [],
                "worksheets": [{"id": "ws.same", "name": "A"}, {"id": "ws.same", "name": "B"}],
            }
        ),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["A"])

    assert cu.expected_pages(tmp_path) is None
    detail = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))["detail"]
    assert "reuses page id(s) ws.same" in detail


def test_a_worksheet_with_any_encoding_still_owes_a_page(tmp_path: Path) -> None:
    """One populated shelf is content. Everything else is complete, so only that shelf can turn this."""
    _spec_with_worksheets(
        tmp_path,
        [
            _complete_worksheet("ws.a", "A", encodings={"rows": ["x"], "columns": [], "color": None}),
            _complete_worksheet(
                "ws.b",
                "B",
                encodings={
                    "rows": [],
                    "columns": [],
                    "color": "Region",
                    "size": None,
                    "shape": None,
                    "label": [],
                    "detail": [],
                    "tooltip": [],
                },
            ),
        ],
    )
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["source_empty_omissions"] == []
    assert [row["name"] for row in parity["unsigned_omissions"]] == ["B"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_worksheet_whose_encodings_are_not_an_object_is_not_called_empty(tmp_path: Path) -> None:
    """Unknown is not empty, and the type guard is what keeps a malformed spec from crashing the gate.

    The list carries exactly the schema's channel NAMES, so the key-set check cannot refuse it - only
    the `isinstance` guard can. Without it the gate reaches `.values()` on a list.
    """
    _spec_with_worksheets(
        tmp_path,
        [
            _complete_worksheet("ws.a", "A", encodings={"rows": ["x"], "columns": [], "color": None}),
            _complete_worksheet(
                "ws.b",
                "B",
                encodings=["rows", "columns", "color", "size", "shape", "label", "detail", "tooltip"],
            ),
        ],
    )
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["source_empty_omissions"] == []
    assert [row["name"] for row in parity["unsigned_omissions"]] == ["B"]


def test_a_complete_worksheet_with_partial_encodings_is_not_proof_of_emptiness(tmp_path: Path) -> None:
    """The encodings key-set check on its own: every worksheet key is present, only the shelves are not."""
    _spec_with_worksheets(
        tmp_path,
        [
            _complete_worksheet("ws.a", "A", encodings={"rows": ["x"], "columns": [], "color": None}),
            _complete_worksheet("ws.b", "B", encodings={"rows": []}),
        ],
    )
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["source_empty_omissions"] == []
    assert [row["name"] for row in parity["unsigned_omissions"]] == ["B"]


def test_a_source_empty_sheet_that_still_has_a_filter_owes_a_page(tmp_path: Path) -> None:
    """A filter is content the author placed. Only the filter differs from the blank case above."""
    _spec_with_worksheets(
        tmp_path,
        [
            _complete_worksheet("ws.a", "A", encodings={"rows": ["x"], "columns": [], "color": None}),
            _complete_worksheet("ws.b", "B", filters=[{"field": "Region"}]),
        ],
    )
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["source_empty_omissions"] == []
    assert [row["name"] for row in parity["unsigned_omissions"]] == ["B"]


def test_one_item_matching_two_objects_across_namespaces_signs_neither(tmp_path: Path) -> None:
    """Kills: resolving a signature per PAGE instead of once GLOBALLY.

    Exactness without globality still lets one item match two objects, because nothing asks how many
    things the item matches in TOTAL. Here 'collide' is page A's id AND page B's name; it used to
    sign both omissions and record one compromise.
    """
    (tmp_path / "migration-spec.json").write_text(
        json.dumps(
            {
                "dashboards": [],
                "worksheets": [
                    {"id": "collide", "name": "A"},
                    {"id": "ws.b", "name": "collide"},
                    {"id": "ws.c", "name": "C"},
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["C"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "collide", "reason": "r", "decided_by": "gf"}]}),
        encoding="utf-8",
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_REPORT)
    parity = next(check for check in report["checks"] if check["id"] == "page-parity")

    assert parity["applied_exemptions"] == []
    assert {row["name"] for row in parity["unsigned_omissions"]} == {"A", "collide"}
    assert cu._compromise_count(report) == 0  # pylint: disable=protected-access
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_an_item_matching_one_object_through_both_namespaces_still_applies(tmp_path: Path) -> None:
    """The other half: a page whose id EQUALS its own name is one object, so its signature applies."""
    (tmp_path / "migration-spec.json").write_text(
        json.dumps({"dashboards": [], "worksheets": [{"id": "Solo", "name": "Solo"}, {"id": "ws.c", "name": "C"}]}),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["C"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "Solo", "reason": "r", "decided_by": "gf"}]}),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [page["name"] for page in parity["applied_exemptions"]] == ["Solo"]
    assert parity["status"] == cu.STATUS_PASS


def test_one_extra_signature_cannot_account_for_two_same_named_pages(tmp_path: Path) -> None:
    """Kills: an `extra:` item accepting every rendered page that happens to share the name."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    pages = tmp_path / "fabric" / "Book.Report" / "definition" / "pages"
    _write_report(tmp_path, ["A", "Bonus"])
    second = pages / "p3"
    second.mkdir()
    (second / "page.json").write_text(json.dumps({"name": "p3", "displayName": "Bonus"}), encoding="utf-8")
    _write_visuals(second, 1)
    (pages / "pages.json").write_text(json.dumps({"pageOrder": ["p1", "p2", "p3"]}), encoding="utf-8")
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {"exemptions": [{"check": "page-parity", "item": "extra:Bonus", "reason": "r", "decided_by": "gf"}]}
        ),
        encoding="utf-8",
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_REPORT)
    parity = next(check for check in report["checks"] if check["id"] == "page-parity")

    assert [page["name"] for page in parity["unaccounted_extra_pages"]] == ["Bonus", "Bonus"]
    assert cu._compromise_count(report) == 0  # pylint: disable=protected-access
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_the_oracle_denominator_removes_by_identity_not_by_display_name(tmp_path: Path) -> None:
    """Kills: reducing dispositioned identity rows back to a set of display NAMES.

    A SIGNED dashboard 'Sales' and an UNSIGNED worksheet 'Sales'. Parity distinguishes them; the
    oracle removed BOTH from its denominator and reported pages=0 while listing one exclusion.
    """
    (tmp_path / "migration-spec.json").write_text(
        json.dumps(
            {
                "dashboards": [{"id": "d.sales", "name": "Sales", "zones": {}}],
                "worksheets": [{"id": "ws.sales", "name": "Sales"}],
            }
        ),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["Other"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {
                "exemptions": [
                    {"check": "page-parity", "item": "d.sales", "reason": "cut", "decided_by": "gf"},
                    {"check": "page-parity", "item": "extra:Other", "reason": "new", "decided_by": "gf"},
                ]
            }
        ),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))
    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert [page["id"] for page in parity["applied_exemptions"]] == ["d.sales"]
    assert [row["id"] for row in parity["unsigned_omissions"]] == ["ws.sales"]
    assert [page["id"] for page in oracle["excluded_omissions"]] == ["d.sales"]
    assert oracle["pages"] == 1, "the unsigned worksheet still owes evidence"


def test_both_halves_agree_on_whether_attribution_is_ambiguous(tmp_path: Path) -> None:
    """Kills: parity and the oracle computing the same predicate differently.

    Parity subtracted accounted-for `extra:` pages before deciding ambiguity and the oracle did not,
    so the same unit was ambiguous in one half and not the other - and a signed omission left one
    denominator but not the other. Found by the round-5 reproduction, not by a test.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["Renamed"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {
                "exemptions": [
                    {"check": "page-parity", "item": "extra:Renamed", "reason": "new", "decided_by": "gf"},
                    {"check": "page-parity", "item": "A", "reason": "cut", "decided_by": "gf"},
                    {"check": "page-parity", "item": "B", "reason": "cut", "decided_by": "gf"},
                ]
            }
        ),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))
    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert parity["attribution_ambiguous"] is False
    assert {page["name"] for page in parity["applied_exemptions"]} == {"A", "B"}
    assert {page["name"] for page in oracle["excluded_omissions"]} == {"A", "B"}


def test_a_stale_signature_is_reported_even_when_a_same_named_page_is_omitted(tmp_path: Path) -> None:
    """Kills: comparing omitted pages by display name when deciding which signatures are stale."""
    (tmp_path / "migration-spec.json").write_text(
        json.dumps(
            {
                "dashboards": [{"id": "d.sales", "name": "Sales", "zones": {}}],
                "worksheets": [{"id": "ws.sales", "name": "Sales"}],
            }
        ),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["Sales"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "d.sales", "reason": "r", "decided_by": "gf"}]}),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["contested_names"] == ["Sales"], "one page, two claimants"
    assert [row["id"] for row in parity["unapplied_exemptions"]] == ["d.sales"]
    assert parity["applied_exemptions"] == []


def test_two_workbooks_whose_names_slug_alike_do_not_bind_interchangeably(tmp_path: Path) -> None:
    """Kills: binding a handover slice to a unit through the lossy slug when it is not unique.

    Both names slug to the report stem's slug ('book') and NEITHER matches it exactly, so only the
    uniqueness guard can decide. Found by enumerating every place an identity becomes a string.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    run_estate.slice_handovers(
        {
            "tool": "t",
            "generated_at": "now",
            "workbooks": [
                {"name": "Bo ok", "viz_fidelity": [_empty_row("B")]},
                {"name": "Bo-ok", "viz_fidelity": []},
            ],
        },
        tmp_path,
    )

    explanations = cu.page_drop_explanations(tmp_path)

    assert explanations["bound_workbooks"] == [], "'Bo ok' and 'Bo-ok' slug alike; neither may bind"
    assert explanations["unbound_workbooks"] == ["Bo ok", "Bo-ok"]


def test_a_filesystem_sanitised_workbook_name_still_binds_when_unambiguous(tmp_path: Path) -> None:
    """The other half: a single lossy candidate is the fallback the sanitised folder name needs."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(tmp_path, [_empty_row("B")], workbook_name="Book!")

    explanations = cu.page_drop_explanations(tmp_path)

    assert explanations["bound_workbooks"] == ["Book!"]


def test_a_worksheet_row_can_never_explain_a_same_named_dashboard(tmp_path: Path) -> None:
    """Kills: evidence about object X settling a question about object Y of a different KIND.

    A workbook with dashboard 'Sales' and a placed worksheet 'Sales'. The worksheet's tier-'empty'
    row used to explain the DASHBOARD's absence, because evidence was keyed on name alone. Identity
    is (kind, exact name), so the dashboard resolves against nothing.
    """
    _write_full_spec(tmp_path, dashboards=[("Sales", ["ws.sales"])], worksheets=[("ws.sales", "Sales")])
    _write_report(tmp_path, ["Other"])
    _write_viz_fidelity_handover(
        tmp_path,
        [
            _empty_row("Sales", "manual attention required: worksheet Sales unsupported"),
            {
                "worksheet": "Sales",
                "visual_type": "dashboard",
                "status": "warned",
                "tier": "degraded",
                "reason": "manual attention required: no supported visuals on this dashboard",
            },
        ],
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))
    omission = next(row for row in parity["unsigned_omissions"] if row["name"] == "Sales")

    assert omission["kind"] == "dashboard"
    assert omission["declared_reason"] is None, "a worksheet row proves nothing about a dashboard"
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_filter_scope_row_cannot_explain_a_same_named_worksheet(tmp_path: Path) -> None:
    """visual_type is load-bearing on its own: a non-object scope row proves nothing about a page.

    Real shape, from the HR Dashboard estate slice: a 'filter'-scope row named 'Location' sits beside
    a worksheet also named 'Location'. Without the visual_type requirement, that filter row would be
    indexed as worksheet evidence and explain the worksheet's omission.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.loc", "Location")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(
        tmp_path,
        [
            {
                "worksheet": "Location",
                "visual_type": "filter",
                "status": "warned",
                "tier": "empty",
                "reason": "manual attention required: applied selection reduced to null members",
            }
        ],
    )

    omission = next(
        row
        for row in cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))["omissions"]
        if row["name"] == "Location"
    )

    assert omission["declared_reason"] is None, "a filter-scope row is not evidence about a worksheet"
    assert omission["disposition"] == cu.OMISSION_UNEXPLAINED


def test_a_dashboard_kind_row_claiming_empty_tier_still_proves_nothing(tmp_path: Path) -> None:
    """Both evidence fields are load-bearing: an empty-tier row must also be visual_type 'unsupported'.

    Measured on a 2.339.0 estate run, all 46 empty-tier rows carry 'unsupported' and no
    dashboard-scope row ever does - so a row claiming both is not engine output, and must not be
    allowed to identify a dashboard.
    """
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[])
    _write_report(tmp_path, ["Other"])
    _write_viz_fidelity_handover(
        tmp_path,
        [{"worksheet": "Sales", "visual_type": "dashboard", "status": "warned", "tier": "empty", "reason": "x"}],
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))
    omission = next(row for row in parity["unsigned_omissions"] if row["name"] == "Sales")

    assert omission["declared_reason"] is None
    assert omission["disposition"] != cu.OMISSION_SIGNED


def test_a_degraded_warning_asserts_a_rendered_visual_and_explains_nothing(tmp_path: Path) -> None:
    """migrate_estate._fidelity_tier: 'degraded' is a RENDERED visual - the opposite of non-emission."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(
        tmp_path,
        [
            {
                "worksheet": "B",
                "visual_type": "bar",
                "status": "warned",
                "tier": "degraded",
                "evidence": "emitted+linted",
                "reason": "manual attention required: data labels deferred",
            }
        ],
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))
    omission = parity["unsigned_omissions"][0]

    assert omission["disposition"] == cu.OMISSION_UNEXPLAINED
    assert omission["declared_reason"] is None
    assert "proves nothing about a worksheet" in omission["why"]


def test_a_row_with_no_tier_at_all_leaves_the_omission_unexplained(tmp_path: Path) -> None:
    """An engine too old to publish `tier` is 'cannot tell', not 'intentionally dropped'."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(
        tmp_path,
        [{"worksheet": "B", "visual_type": "unsupported", "status": "warned", "reason": "unsupported"}],
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["unsigned_omissions"][0]["disposition"] == cu.OMISSION_UNEXPLAINED
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_another_workbooks_row_cannot_explain_this_units_omission(tmp_path: Path) -> None:
    """Kills: borrowing evidence across workbook boundaries."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(tmp_path, [_empty_row("B")], workbook_name="Different Workbook")

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED
    assert parity["unsigned_omissions"][0]["declared_reason"] is None
    assert parity["drop_explanations"]["bound_workbooks"] == []
    assert parity["drop_explanations"]["unbound_workbooks"] == ["Different Workbook"]
    assert "Different Workbook" in parity["unsigned_omissions"][0]["why"]


def test_drop_evidence_comes_from_viz_fidelity_not_pbip_warnings(tmp_path: Path) -> None:
    """pbip_warnings[] is bare strings with no page name; 193 entries explained 0 of 23 absences."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(
        tmp_path,
        [{"worksheet": "A", "visual_type": "bar", "status": "rebuilt", "tier": "rebuilt", "reason": None}],
        pbip_warnings=["manual attention required: mark class 'Bar' not supported -> no visual emitted"],
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [row["name"] for row in parity["unsigned_omissions"]] == ["B"]
    assert parity["unsigned_omissions"][0]["declared_reason"] is None


def test_an_emitted_page_is_never_an_omission(tmp_path: Path) -> None:
    """'A' carries a tier-'empty' row AND is emitted; only an absent candidate can be an omission."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(tmp_path, [_empty_row("A")])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [row["name"] for row in parity["omissions"]] == ["B"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_missing_handover_says_evidence_was_unavailable(tmp_path: Path) -> None:
    """'No declared reason' and 'could not read the declarations' are different states."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["drop_explanations"]["available"] is False
    assert parity["drop_explanations"]["source"] == "handover viz_fidelity[]"
    assert "no handover slice was readable" in parity["unsigned_omissions"][0]["why"]


def test_two_identical_evidence_rows_resolve_to_nothing_rather_than_the_first(tmp_path: Path) -> None:
    """Ambiguous identity is refused, not silently resolved to match[0]."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(tmp_path, [_empty_row("B", "first"), _empty_row("B", "second")])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["unsigned_omissions"][0]["declared_reason"] is None
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_malformed_required_collection_is_unassessable_not_a_smaller_denominator(tmp_path: Path) -> None:
    """Kills: silently skipping a malformed required array and trusting what is left."""
    (tmp_path / "migration-spec.json").write_text(
        json.dumps({"dashboards": [{"id": "dash.a", "name": "A", "zones": {}}], "worksheets": {"oops": 1}}),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["A"])
    _write_reference_manifest(tmp_path, ["A"])

    assert cu.expected_pages(tmp_path) is None
    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))
    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert parity["status"] == cu.STATUS_NOT_CHECKED
    assert "'worksheets' is dict, not the array the schema requires" in parity["detail"]
    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["grade"] != "validation-grade"


def test_a_spec_entry_with_no_usable_identity_is_unassessable(tmp_path: Path) -> None:
    """A page entry that cannot be identified at all must refuse the whole spec, not shrink it."""
    (tmp_path / "migration-spec.json").write_text(
        json.dumps({"dashboards": [{"id": "dash.a", "name": "A"}, {"size": {}}], "worksheets": []}),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["A"])

    assert cu.expected_pages(tmp_path) is None
    assert "entry #2 has no usable name" in cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))["detail"]


def test_an_id_only_spec_entry_stays_in_the_denominator(tmp_path: Path) -> None:
    """An identifiable-but-unnamed entry is kept, so the count cannot silently shrink."""
    (tmp_path / "migration-spec.json").write_text(
        json.dumps({"dashboards": [{"id": "dash.a", "name": "A"}, {"id": "dash.b"}], "worksheets": []}),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["A"])

    assert [page["name"] for page in cu.expected_pages(tmp_path) or []] == ["A", "dash.b"]
    assert cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))["status"] == cu.STATUS_PRECONDITION_FAILED


def test_an_unwalkable_zone_tree_is_unassessable(tmp_path: Path) -> None:
    """A zone tree that cannot be walked means placed worksheets are unknown, not that there are none."""
    (tmp_path / "migration-spec.json").write_text(
        json.dumps(
            {
                "dashboards": [{"id": "dash.a", "name": "A", "zones": "not-a-tree"}],
                "worksheets": [{"id": "ws.a", "name": "Sheet"}],
            }
        ),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["A"])

    assert cu.expected_pages(tmp_path) is None
    assert "zone tree, which cannot be walked" in cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))["detail"]


def test_a_page_with_no_visuals_does_not_certify_a_candidate_as_rebuilt(tmp_path: Path) -> None:
    """Kills: counting a page that renders nothing as evidence the candidate was rebuilt."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_report(tmp_path, ["A"], visuals=0)

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED
    assert parity["emitted_count"] == 0
    assert [page["name"] for page in parity["blank_pages"]] == ["A"]
    assert "render nothing" in parity["detail"]


def test_oracle_coverage_without_an_expected_set_is_blocking_not_a_pass(tmp_path: Path) -> None:
    """Kills the circular denominator: ``expected_pages(target) or actual_pages(target)``."""
    _write_report(tmp_path, ["Executive", "Detail"])
    _write_reference_manifest(tmp_path, ["Executive", "Detail"])

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["pages"] == 0
    assert oracle["visual_present"] == 0
    assert "cannot assess oracle coverage" in oracle["detail"]
    assert "no migration-spec.json found" in oracle["detail"]


def test_unassessable_oracle_coverage_fails_the_whole_run_closed(tmp_path: Path) -> None:
    """The unassessable case must reach a non-zero exit, not just a quiet row."""
    _write_report(tmp_path, ["Executive"])
    _write_reference_manifest(tmp_path, ["Executive"])

    report = cu.run_all(tmp_path, scope=cu.SCOPE_REPORT)

    assert report["exit_code"] == 2, "an unestablished expected set is NOT_CHECKED, never a pass"
    assert report["status"] == cu.STATUS_NOT_CHECKED
    oracle = next(check for check in report["checks"] if check["id"] == "oracle-coverage")
    assert oracle["status"] == cu.STATUS_NOT_CHECKED


def test_spec_declaring_no_pages_cannot_be_graded(tmp_path: Path) -> None:
    """A spec with neither dashboards nor worksheets is unassessable, not a zero-work pass."""
    (tmp_path / "migration-spec.json").write_text(json.dumps({"workbook": "Book"}), encoding="utf-8")
    _write_report(tmp_path, ["Executive"])
    _write_reference_manifest(tmp_path, ["Executive"])

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert "has no 'dashboards' or 'worksheets' array" in oracle["detail"]


def test_oracle_capture_is_discovered_under_the_canonical_run_layout(tmp_path: Path) -> None:
    """Kills: looking only for `_oracle/` and missing `_runs/<NNN>-<slug>/oracle/`.

    work_dirs.CANONICAL_SUBDIRS puts a run's capture in a sibling `oracle/` beside `bundle/`, while
    capture_tableau_oracle.py is documented as `--out _oracle`. Both are real; discovery must accept
    both or a capture that exists reads as "no oracle manifest found".
    """
    run_root = tmp_path / "042-unit"
    bundle = run_root / "bundle"
    _write_spec(bundle, ["Executive"])
    _write_report(bundle, ["Executive"])
    _write_oracle_manifest(run_root, ["Executive"])
    (run_root / "_oracle").rename(run_root / "oracle")

    assert "oracle" in {path.name for path in cu._oracle_dirs(bundle, None)}  # pylint: disable=protected-access
    oracle = cu.check_oracle_coverage(bundle, None, None)
    assert oracle["status"] == cu.STATUS_PASS
    assert oracle["grade"] == "layout/text only (oracle capture, default view state)"


def test_underscore_oracle_directory_is_still_discovered(tmp_path: Path) -> None:
    """The documented `--out _oracle` convention keeps working beside the canonical layout."""
    _write_spec(tmp_path, ["Executive"])
    _write_report(tmp_path, ["Executive"])
    _write_oracle_manifest(tmp_path, ["Executive"])

    assert "_oracle" in {path.name for path in cu._oracle_dirs(tmp_path, None)}  # pylint: disable=protected-access
    assert cu.check_oracle_coverage(tmp_path, None, None)["status"] == cu.STATUS_PASS


def test_engine_crash_guard_placeholder_is_not_a_blank_page(tmp_path: Path) -> None:
    """Kills: reporting the engine's declared crash-guard page as a page that renders nothing.

    twb_to_pbir.py:14719-14727 ships ONE synthetic visual-less page when every candidate was dropped,
    because a PBIR with an empty pageOrder crashes Power BI Desktop on open. The omission itself is
    signed here, so the placeholder classification is the only thing this test can turn.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "Sheet 1")])
    _write_placeholder_page(tmp_path, visuals=0)
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {"exemptions": [{"check": "page-parity", "item": "Sheet 1", "reason": "no mark", "decided_by": "gf"}]}
        ),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [page["name"] for page in parity["engine_placeholder_pages"]] == ["No visuals rebuilt"]
    assert parity["blank_pages"] == [], "the declared placeholder is not an unexplained blank page"
    assert parity["status"] == cu.STATUS_PASS


def _write_placeholder_page(unit: Path, *, visuals: int, display: str = "No visuals rebuilt") -> None:
    """The engine's crash-guard page shape: id `page-empty*`, displayName `No visuals rebuilt`."""
    pages = unit / "fabric" / "Book.Report" / "definition" / "pages"
    page = pages / "page-emptyb0302807"
    page.mkdir(parents=True)
    (page / "page.json").write_text(
        json.dumps({"name": "page-emptyb0302807", "displayName": display}), encoding="utf-8"
    )
    _write_visuals(page, visuals)
    (pages / "pages.json").write_text(json.dumps({"pageOrder": ["page-emptyb0302807"]}), encoding="utf-8")


def test_a_blank_page_titled_like_the_placeholder_is_still_blank(tmp_path: Path) -> None:
    """The id prefix decides on its own: a zero-visual page with an ORDINARY id renders nothing."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_report(tmp_path, ["No visuals rebuilt"], visuals=0)

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["engine_placeholder_pages"] == [], "p1 is an ordinary page id, not page-empty*"
    assert [page["name"] for page in parity["blank_pages"]] == ["No visuals rebuilt"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_retitled_placeholder_id_is_still_a_blank_page(tmp_path: Path) -> None:
    """The display name decides on its own: a page-empty* id retitled by an author is not declared."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_placeholder_page(tmp_path, visuals=0, display="A")

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["engine_placeholder_pages"] == [], "a page-empty* id retitled to 'A' is not the placeholder"
    assert [page["name"] for page in parity["blank_pages"]] == ["A"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_placeholder_id_holding_real_visuals_is_a_rebuilt_page(tmp_path: Path) -> None:
    """Kills: discarding a page-empty* page that an author actually filled with visuals.

    It carries three visuals, so it is a rendered page. Its title matches no expected page, which is
    accounted for by an ``extra:`` signature - the explicit way to declare a page the source never had.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_report(tmp_path, ["A"])
    _write_placeholder_page_beside(tmp_path, visuals=3)
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {
                "exemptions": [
                    {
                        "check": "page-parity",
                        "item": "extra:No visuals rebuilt",
                        "reason": "author filled the placeholder",
                        "decided_by": "gf",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["emitted_count"] == 2, "a page with visuals is rendered, whatever its id"
    assert parity["blank_pages"] == []
    assert parity["status"] == cu.STATUS_PASS


def _write_placeholder_page_beside(unit: Path, *, visuals: int) -> None:
    """Add a crash-guard-shaped page next to an existing report's pages."""
    pages = unit / "fabric" / "Book.Report" / "definition" / "pages"
    page = pages / "page-emptyb0302807"
    page.mkdir(parents=True)
    (page / "page.json").write_text(
        json.dumps({"name": "page-emptyb0302807", "displayName": "No visuals rebuilt"}), encoding="utf-8"
    )
    _write_visuals(page, visuals)
    order = json.loads((pages / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
    (pages / "pages.json").write_text(json.dumps({"pageOrder": order + ["page-emptyb0302807"]}), encoding="utf-8")


def test_a_blank_page_alone_fails_the_gate_even_when_every_page_is_paired(tmp_path: Path) -> None:
    """Kills: reporting a page that renders nothing without letting it fail anything.

    Constructed so the blank page is the ONLY problem: the one candidate has its rendered page, and
    the extra zero-visual page is declared with an ``extra:`` signature so it is not an unaccounted
    extra either.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    pages = tmp_path / "fabric" / "Book.Report" / "definition" / "pages"
    _write_report(tmp_path, ["A"])
    blank = pages / "p2"
    blank.mkdir()
    (blank / "page.json").write_text(json.dumps({"name": "p2", "displayName": "Extra"}), encoding="utf-8")
    (pages / "pages.json").write_text(json.dumps({"pageOrder": ["p1", "p2"]}), encoding="utf-8")
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {"exemptions": [{"check": "page-parity", "item": "extra:Extra", "reason": "note", "decided_by": "gf"}]}
        ),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["unsigned_omissions"] == [], "A is paired; only the blank page is wrong"
    assert parity["unaccounted_extra_pages"] == []
    assert [page["name"] for page in parity["blank_pages"]] == ["Extra"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_missing_page_is_named_by_content_not_by_position(tmp_path: Path) -> None:
    """'A' is absent and 'C' is present; a positional tail slice used to name 'C'."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B"), ("ws.c", "C")])
    _write_report(tmp_path, ["B", "C"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [page["name"] for page in parity["unsigned_omissions"]] == ["A"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_an_exemption_naming_a_present_page_accepts_nothing(tmp_path: Path) -> None:
    """Kills: a signed exemption absorbing a DIFFERENT page's absence.

    Only 'A' is absent; 'C' is present. Applying the signature unconditionally shrank the expected
    count, balanced the books and hid A. It is now reported as having accepted nothing, and must not
    count as a compromise.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B"), ("ws.c", "C")])
    _write_report(tmp_path, ["B", "C"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "C", "reason": "merged", "decided_by": "review"}]}),
        encoding="utf-8",
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_REPORT)
    parity = next(check for check in report["checks"] if check["id"] == "page-parity")

    assert [page["name"] for page in parity["unsigned_omissions"]] == ["A"], "C is present; it cannot be missing"
    assert [page["name"] for page in parity["unapplied_exemptions"]] == ["C"]
    assert parity["unapplied_exemptions"][0]["disposition"] == cu.EXEMPTION_STALE
    assert cu._compromise_count(report) == 0  # pylint: disable=protected-access
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_an_exemption_naming_the_actually_missing_page_is_honoured(tmp_path: Path) -> None:
    """The other half: signing for the page that IS absent clears the gate."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B"), ("ws.c", "C")])
    _write_report(tmp_path, ["B", "C"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {"exemptions": [{"check": "page-parity", "item": "A", "reason": "merged into B", "decided_by": "review"}]}
        ),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["unsigned_omissions"] == []
    assert [page["name"] for page in parity["applied_exemptions"]] == ["A"]
    assert parity["status"] == cu.STATUS_PASS


def test_a_rename_makes_attribution_ambiguous_and_suspends_every_signature(tmp_path: Path) -> None:
    """Kills: applying a name-only exemption while a rendered page could BE the renamed candidate.

    Expected A, B, C; rendered a renamed 'A' plus B, so C is genuinely missing. Signing for A used to
    turn PRECONDITION_FAILED into PASS with no stale exemption reported at all.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B"), ("ws.c", "C")])
    _write_report(tmp_path, ["A renamed", "B"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "A", "reason": "merged", "decided_by": "review"}]}),
        encoding="utf-8",
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_REPORT)
    parity = next(check for check in report["checks"] if check["id"] == "page-parity")

    assert parity["attribution_ambiguous"] is True
    assert parity["applied_exemptions"] == []
    assert [page["disposition"] for page in parity["unapplied_exemptions"]] == [cu.EXEMPTION_AMBIGUOUS]
    assert {page["name"] for page in parity["unsigned_omissions"]} == {"A", "C"}
    assert cu._compromise_count(report) == 0  # pylint: disable=protected-access
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_rename_suspends_signatures_on_the_oracle_denominator_too(tmp_path: Path) -> None:
    """The ambiguity guard exists on BOTH sides: a suspended signature cannot shrink oracle coverage."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A renamed"])
    _write_reference_manifest(tmp_path, ["A renamed"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "B", "reason": "dropped", "decided_by": "gf"}]}),
        encoding="utf-8",
    )

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["excluded_omissions"] == [], "attribution is ambiguous; no signature applies"
    assert oracle["pages"] == 2
    assert oracle["status"] == cu.STATUS_NOT_CHECKED


def test_declaring_the_renamed_page_resolves_the_ambiguity(tmp_path: Path) -> None:
    """The explicit way out: account for the unmatched page, and signatures apply again."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B"), ("ws.c", "C")])
    _write_report(tmp_path, ["A renamed", "B"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps(
            {
                "exemptions": [
                    {"check": "page-parity", "item": "extra:A renamed", "reason": "A retitled", "decided_by": "gf"},
                    {"check": "page-parity", "item": "A", "reason": "retitled", "decided_by": "gf"},
                ]
            }
        ),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["attribution_ambiguous"] is False
    assert [page["name"] for page in parity["applied_exemptions"]] == ["A"]
    assert [page["name"] for page in parity["unsigned_omissions"]] == ["C"], "C is still missing and still reported"
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_an_extra_page_is_named_by_content_not_by_position(tmp_path: Path) -> None:
    """An emitted page with no Tableau counterpart is identified by NAME, so its signature can match."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_report(tmp_path, ["Bonus Page", "A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [page["name"] for page in parity["unaccounted_extra_pages"]] == ["Bonus Page"], "'A' has a counterpart"
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_page_count_mismatch_is_a_precondition_and_stops_before_oracle(tmp_path: Path) -> None:
    """Kills: treating missing pages as just another row and continuing into noisy page checks."""
    _write_spec(tmp_path, ["A", "B"])
    _write_report(tmp_path, ["A"])

    report = cu.run_all(tmp_path)

    assert report["status"] == cu.STATUS_PRECONDITION_FAILED
    assert report["exit_code"] == cu.EXIT_PRECONDITION_FAILED
    assert report["stopped_after"] == "page-parity"
    assert [check["id"] for check in report["checks"]] == ["page-parity", "numeric-obligation", "finalized"]


def test_early_stop_marks_compromise_channel_not_evaluated(tmp_path: Path) -> None:
    """A precondition stop is loud, but downstream compromise channels are unknown, not zero."""
    _write_spec(tmp_path, ["A", "B"])
    _write_report(tmp_path, ["A"])

    rendered = cu.render(cu.run_all(tmp_path, scope=cu.SCOPE_ALL))

    assert "stopped after failed precondition: page-parity" in rendered
    assert "compromises=0; compromises_not_evaluated=1" in rendered


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
    assert [page["name"] for page in report["checks"][0]["applied_exemptions"]] == ["B"]
    assert report["checks"][0]["applied_exemptions"][0]["kind"] == "dashboard"
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
    rendered = cu.render(cu.run_all(tmp_path))

    summary = next(line for line in rendered.splitlines() if line.startswith("SUMMARY:"))
    assert summary == (
        "SUMMARY: blockers=4; compromises=0; compromises_not_evaluated=0; findings_by_owner=model=1; "
        "not_checked_external=0; not_checked_missing_input=3; ladder=FINDINGS exit=1"
    )


def _write_handover(unit: Path, workbook: dict[str, object]) -> Path:
    """Write the same {estate, workbook} envelope as run_estate.slice_handovers."""
    report = {"tool": "test", "generated_at": "now", "workbooks": [workbook]}
    return run_estate.slice_handovers(report, unit)[0]


def test_scaffold_partitions_are_blockers_until_exempted(tmp_path: Path) -> None:
    """An engine-recorded empty M partition is outstanding work, not a hidden compromise."""
    _write_handover(
        tmp_path,
        {
            "name": "Unit",
            "partitions_needs_review": [
                {
                    "kind": "m_partition",
                    "table": "Orders",
                    "reason": "custom SQL native query for this connector is not verified",
                }
            ],
        },
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)
    rendered = cu.render(report)

    assert report["status"] == cu.STATUS_FINDINGS
    assert report["exit_code"] == cu.EXIT_FINDINGS
    assert "scaffold-partitions: FINDINGS (0 exempted, 1 unexempted)" in rendered
    assert "partition scaffold(s) need manual completion" in rendered
    assert "blockers=" in rendered and "compromises=0" in rendered


def test_scaffold_partitions_agree_with_read_handover_on_engine_slice(tmp_path: Path) -> None:
    """Acceptance: the facade and read_handover unwrap the same engine-shaped slice."""
    path = _write_handover(
        tmp_path,
        {
            "name": "Unit",
            "partitions_needs_review": [
                {
                    "kind": "m_partition",
                    "table": "Orders",
                    "reason": "custom SQL native query for this connector is not verified",
                }
            ],
        },
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    _name, workbook, _source = read_handover._workbooks_from_payload(payload, path)[0]  # pylint: disable=protected-access
    status, rows = read_handover.partitions_needs_review_status(workbook)

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)
    scaffold = next(check for check in report["checks"] if check["id"] == "scaffold-partitions")

    assert status == read_handover.PARTITION_REVIEW_PRESENT
    assert len(rows) == 1
    assert scaffold["status"] == cu.STATUS_FINDINGS
    assert scaffold["unexempted_scaffolds"] == len(rows)


def test_scaffold_partitions_keep_good_slice_when_stray_json_is_unreadable(tmp_path: Path) -> None:
    """A drop-zone stray JSON must not crash or hide a neighbouring scaffold finding."""
    _write_handover(
        tmp_path,
        {
            "name": "Unit",
            "partitions_needs_review": [
                {
                    "kind": "m_partition",
                    "table": "Orders",
                    "reason": "custom SQL native query for this connector is not verified",
                }
            ],
        },
    )
    (tmp_path / "handover" / "estate-summary.json").write_text(
        json.dumps({"estate": {"tool": "test"}}), encoding="utf-8"
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)
    rendered = cu.render(report)
    scaffold = next(check for check in report["checks"] if check["id"] == "scaffold-partitions")

    assert report["exit_code"] == cu.EXIT_FINDINGS
    assert scaffold["status"] == cu.STATUS_FINDINGS
    assert scaffold["unexempted_scaffolds"] == 1
    assert "estate-summary.json" in scaffold["invalid_handover_keys"][0]
    assert "scaffold-partitions: FINDINGS (0 exempted, 1 unexempted)" in rendered
    assert "unreadable handover:" in rendered
    assert "SUMMARY:" in rendered


def test_scaffold_partitions_unreadable_only_is_a_finding(tmp_path: Path) -> None:
    """A malformed handover-like file alone is a gate finding, not a missing-input skip."""
    handover = tmp_path / "handover"
    handover.mkdir()
    (handover / "estate-summary.json").write_text(json.dumps({"estate": {"tool": "test"}}), encoding="utf-8")

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)
    rendered = cu.render(report)
    scaffold = next(check for check in report["checks"] if check["id"] == "scaffold-partitions")

    assert report["exit_code"] == cu.EXIT_FINDINGS
    assert scaffold["status"] == cu.STATUS_FINDINGS
    assert scaffold["unexempted_scaffolds"] == 0
    assert "estate-summary.json" in scaffold["invalid_handover_keys"][0]
    assert "unreadable handover:" in rendered


def test_empty_workbooks_handover_is_not_silent_absence(tmp_path: Path) -> None:
    """A handover file that resolves zero workbooks is still a visible malformed handover."""
    handover = tmp_path / "handover"
    handover.mkdir()
    (handover / "only.json").write_text(json.dumps({"tool": "t", "workbooks": []}), encoding="utf-8")

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)
    scaffold = next(check for check in report["checks"] if check["id"] == "scaffold-partitions")

    assert report["exit_code"] == cu.EXIT_FINDINGS
    assert scaffold["status"] == cu.STATUS_FINDINGS
    assert "only.json" in scaffold["invalid_handover_keys"][0]


def test_scaffold_partitions_accept_signed_exemptions(tmp_path: Path) -> None:
    """A signed scaffold exemption is a visible compromise and keeps the scaffold gate from failing."""
    _write_handover(
        tmp_path,
        {
            "name": "Unit",
            "partitions_needs_review": [
                {
                    "kind": "m_partition",
                    "table": "Orders",
                    "reason": "flat-file source; set the file path manually",
                }
            ],
        },
    )
    (tmp_path / "unit-check-exemptions.json").write_text(
        json.dumps(
            {
                "exemptions": [
                    {
                        "check": "scaffold-partitions",
                        "item": "Orders",
                        "reason": "customer accepted static table for this proof of concept",
                        "decided_by": "migration lead",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)
    rendered = cu.render(report)

    scaffold = next(check for check in report["checks"] if check["id"] == "scaffold-partitions")
    assert scaffold["status"] == cu.STATUS_PASS
    assert "scaffold-partitions: PASS (1 exempted, 0 unexempted)" in rendered
    assert "documented why-not exemptions: 1 accepted, 0 invalid" in rendered
    assert "compromises=1" in rendered


def test_handover_missing_scaffold_key_is_not_zero_scaffolds(tmp_path: Path) -> None:
    """MISSING is its own state: old handovers did not record scaffold status at all."""
    path = _write_handover(tmp_path, {"name": "Unit"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    _name, workbook, _source = read_handover._workbooks_from_payload(payload, path)[0]  # pylint: disable=protected-access
    status, rows = read_handover.partitions_needs_review_status(workbook)

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)
    rendered = cu.render(report)

    assert status == read_handover.PARTITION_REVIEW_MISSING
    assert rows == []
    scaffold = next(check for check in report["checks"] if check["id"] == "scaffold-partitions")
    assert scaffold["status"] == cu.STATUS_NOT_CHECKED
    assert "partition scaffold status not recorded in handover" in rendered
    assert "scaffold-partitions: PASS" not in rendered


def test_declared_connection_downgrade_is_a_visible_compromise() -> None:
    """A declared downgrade must not render byte-identically to a genuinely connected source."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_unit.py"),
            str(REPO_ROOT / "tests" / "fixtures" / "connection-fidelity" / "declared-downgrade"),
            "--scope",
            "integration",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert "connection-fidelity: PASS (native OK exit 0; 1 declared downgrade compromise(s))" in result.stdout
    assert "compromises=1" in result.stdout
    assert "compromises_not_evaluated=0" in result.stdout


def test_skipped_connection_fidelity_marks_compromise_channel_unknown() -> None:
    """A skipped declared-downgrade channel is UNKNOWN/unevaluated, not zero compromises."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_unit.py"),
            str(REPO_ROOT / "tests" / "fixtures" / "check-unit-clean-integration"),
            "--scope",
            "integration",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert "connection-fidelity: NOT_CHECKED" in result.stdout
    assert "compromises=0; compromises_not_evaluated=1" in result.stdout


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
    report = _write_report(tmp_path, ["Executive"], visuals=2)
    (report / "definition" / "pages" / "pages.json").unlink()

    pages = cu.actual_pages(tmp_path)

    assert pages == [
        {
            "id": "p1",
            "name": "Executive",
            "report": str(report),
            "path": str(report / "definition" / "pages" / "p1" / "page.json"),
            "visuals": 2,
        }
    ]


def test_actual_pages_counts_zero_visuals_for_a_page_with_none(tmp_path: Path) -> None:
    """The visual count is measured, not assumed.

    The ``visuals/`` folder EXISTS here but holds no ``visual.json``. A fixture with no folder at all
    is answered by the ``is_dir()`` guard and never reaches the counting line, so it cannot kill a
    mutation that hard-codes a count.
    """
    _write_report(tmp_path, ["Executive"], visuals=0)
    (tmp_path / "fabric" / "Book.Report" / "definition" / "pages" / "p1" / "visuals").mkdir()

    assert [page["visuals"] for page in cu.actual_pages(tmp_path)] == [0]


def test_clean_diagnostics_without_a_pin_cannot_complete(tmp_path: Path) -> None:
    """Clean ordinary gates cannot substitute for caller-pinned final evidence."""
    _write_spec(tmp_path, ["Executive"])
    _write_report(tmp_path, ["Executive"])
    _write_reference_manifest(tmp_path, ["Executive"])
    report = cu.run_all(tmp_path)

    assert report["status"] == cu.STATUS_NOT_CHECKED
    assert report["exit_code"] == cu.EXIT_NOT_CHECKED
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
    """The default scope preserves ordinary diagnostics and blocking final evidence requirements."""
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

    fidelity = _load_script_module("check_connection_fidelity.py")
    gate = _gate_by_id("connection-fidelity")
    assert gate.pass_statuses == {fidelity.STATUS_OK}
    assert gate.pass_exit_codes == {fidelity.EXIT_OK}
    assert gate.finding_statuses == {fidelity.STATUS_DOWNGRADED}
    assert gate.finding_exit_codes == {fidelity.EXIT_DOWNGRADED}
    assert gate.not_checked_statuses == {fidelity.STATUS_SKIPPED}
    assert gate.not_checked_exit_codes == {fidelity.EXIT_SKIPPED}


def test_cli_model_scope_reports_not_checked_for_unattributable_connection_fixture() -> None:
    """Subprocess-level proof that model scope runs real native gate wiring end to end.

    This fixture's only table (`Sales`) is an inline `#table(...)` literal with no `data_sources`
    entry in its spec at all - it was built to exercise the OTHER model-scope gates cheaply, not to
    model a real Tableau connection. `check_connection_fidelity` has no honest spec counterpart to
    check it against (an inline literal maps to no Tableau connection class), so it correctly reports
    SKIPPED/NOT_CHECKED rather than fabricating a PASS - and `check_unit` correctly keeps that from
    being silently absorbed into AUTOMATED_CHECKS_PASS (issue #328). Every other native gate on this
    fixture still proves PASS on real wiring, so this is not a regression in what was already checked.
    """
    fixture = _freshen_clean_fixture_cache()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_unit.py"), str(fixture), "--scope", "model"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == cu.EXIT_NOT_CHECKED, result.stdout + result.stderr
    assert "UNIT CHECK (model scope): NOT_CHECKED" in result.stdout
    assert "connection-fidelity: NOT_CHECKED (native SKIPPED exit 3)" in result.stdout
    assert "cache-freshness: PASS - mtime-only partial check" in result.stdout
    assert "data-model: PASS" in result.stdout


def test_cli_integration_scope_reports_not_checked_for_unattributable_connection_fixture() -> None:
    """Subprocess-level proof with real native gate wiring, not monkeypatched passes.

    Same fixture and reasoning as the model-scope counterpart above: the `Sales` table's inline
    `#table(...)` literal has no data source to attribute a connection to, so connection-fidelity
    SKIPS honestly and the unit is legitimately NOT_CHECKED rather than a false AUTOMATED_CHECKS_PASS.
    """
    fixture = REPO_ROOT / "tests" / "fixtures" / "check-unit-clean-integration"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_unit.py"), str(fixture), "--scope", "integration"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == cu.EXIT_NOT_CHECKED, result.stdout + result.stderr
    assert "UNIT CHECK (integration scope): NOT_CHECKED" in result.stdout
    assert "connection-fidelity: NOT_CHECKED (native SKIPPED exit 3)" in result.stdout
    assert "blank-placeholders: PASS (native OK exit 0)" in result.stdout
    assert "field-bindings: PASS (native OK exit 0)" in result.stdout
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


# --- issue #317: a shared/published datasource model lands once and each report byPath-hops to it. ---
# check_unit built its model inventory with a local *.SemanticModel glob and never resolved
# definition.pbir byPath, so for a split shared datasource (model under datasources/<ds>/fabric/, each
# report under workbooks/<wb>/fabric/) eight model-layer gates called the model absent while
# check_field_bindings resolved and PASSed it in the SAME run. These hand-written minimal fixtures pin
# all four states a report unit's model can be in - the negative (genuinely modelless) one included on
# purpose, so "model lives elsewhere, by design" and "this unit has no model" never look the same:
#
#   fixture                          state     check_unit.py --scope model
#   -------------------------------- --------- ------------------------------------------------------
#   model-local/                     LOCAL     model gates run for real (data-model: PASS); external=0
#   external-resolves/.../sales-wb   EXTERNAL  8 gates NOT_CHECKED "model is EXTERNAL", field-bindings
#                                              PASS, not_checked_external=9, brownfield EVIDENCED
#                                              (external); exit 2
#   external-broken/.../sales-wb     BROKEN    model-reference FINDINGS "byPath does not resolve";
#                                              exit 1
#   no-model/                        NONE      ai-descriptions "no semantic model found", no
#                                              model-reference row, external=0; exit 2
#
# The report references Sales[Order Date] and Sales[Total Revenue] so field-bindings genuinely resolves
# and PASSes against the external model. The golden snapshot below locks the actionable EXTERNAL
# wording; it is normalized so it is portable across Windows and the Linux CI runner.
SHARED_DS = REPO_ROOT / "tests" / "fixtures" / "shared-datasource"
STATE_TARGETS = {
    "model-local": SHARED_DS / "model-local",
    "external-resolves": SHARED_DS / "external-resolves" / "workbooks" / "sales-wb",
    "external-broken": SHARED_DS / "external-broken" / "workbooks" / "sales-wb",
    "no-model": SHARED_DS / "no-model",
}


def _run_unit(target: Path, scope: str) -> CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_unit.py"), str(target), "--scope", scope],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_model_location_classifies_all_four_states() -> None:
    """The resolver tells 'model here', 'model elsewhere', 'reference broken', and 'no model' apart."""
    local = cu._model_location(STATE_TARGETS["model-local"])  # pylint: disable=protected-access
    external = cu._model_location(STATE_TARGETS["external-resolves"])  # pylint: disable=protected-access
    broken = cu._model_location(STATE_TARGETS["external-broken"])  # pylint: disable=protected-access
    none = cu._model_location(STATE_TARGETS["no-model"])  # pylint: disable=protected-access

    assert local.state == cu.MODEL_LOC_LOCAL
    assert external.state == cu.MODEL_LOC_EXTERNAL
    assert external.model_path is not None and external.model_path.is_dir()
    external_unit = STATE_TARGETS["external-resolves"]
    assert not cu._path_within(external.model_path, external_unit)  # pylint: disable=protected-access
    assert broken.state == cu.MODEL_LOC_BROKEN
    assert broken.declared == "../../../../datasources/sales-ds/fabric/Sales.SemanticModel"
    assert broken.model_path is None
    assert none.state == cu.MODEL_LOC_NONE


def test_external_model_reuses_field_bindings_resolver() -> None:
    """The fix must not fork byPath resolution: it resolves through the gate that already works."""
    report = STATE_TARGETS["external-resolves"] / "fabric" / "Sales.Report"
    loc = cu._model_location(STATE_TARGETS["external-resolves"])  # pylint: disable=protected-access

    assert cu.model_for_report is check_field_bindings.model_for_report
    assert loc.model_path == cu.model_for_report(report)


def test_external_model_is_reported_external_not_missing() -> None:
    """Kills the #317 defect: eight model gates calling a resolvable model absent in one run."""
    result = _run_unit(STATE_TARGETS["external-resolves"], "model")

    assert result.returncode == cu.EXIT_NOT_CHECKED, result.stdout + result.stderr
    # field-bindings resolved the very model the model gates are being told to check elsewhere.
    assert "field-bindings: PASS" in result.stdout
    for gate in ("sqlproxy-connections", "data-model", "empty-model", "stub-measures", "ai-descriptions"):
        assert f"{gate}: NOT_CHECKED - model is EXTERNAL (shared datasource)" in result.stdout
    assert "no semantic model found" not in result.stdout
    assert "check it with: python scripts/check_unit.py" in result.stdout
    assert "not_checked_external=9" in result.stdout
    # 2, not 1: `connection-fidelity` (#328) also cannot check this fixture, but for a DIFFERENT
    # reason than externality - the fixture carries no migration-spec.json, so that gate has no
    # declared connection to compare against and honestly reports missing input rather than EXTERNAL.
    assert "not_checked_missing_input=2" in result.stdout


def test_external_model_brownfield_is_evidenced_not_missing() -> None:
    """Brownfield discovery must credit an external model, not report it as absent."""
    brownfield = cu.inspect_brownfield(STATE_TARGETS["external-resolves"])
    phase = next(row for row in brownfield["phases"] if row["phase"] == "semantic models")

    assert phase["status"] == "EVIDENCED (external)"
    assert phase["paths"] == [
        "tests/fixtures/shared-datasource/external-resolves/datasources/sales-ds/fabric/Sales.SemanticModel"
    ]


def test_broken_bypath_is_a_finding_not_not_checked() -> None:
    """A dangling byPath is a genuine defect: it must exit as a finding, never a silent NOT_CHECKED."""
    result = _run_unit(STATE_TARGETS["external-broken"], "model")

    assert result.returncode == cu.EXIT_FINDINGS, result.stdout + result.stderr
    assert "model-reference: FINDINGS" in result.stdout
    assert "byPath does not resolve" in result.stdout
    # A broken reference is NOT an external deferral, so it must not populate that bucket.
    assert "not_checked_external=0" in result.stdout


def test_genuinely_modelless_unit_is_not_called_external() -> None:
    """The negative case: no model reference at all stays 'no semantic model found', never EXTERNAL."""
    result = _run_unit(STATE_TARGETS["no-model"], "model")

    assert result.returncode == cu.EXIT_NOT_CHECKED, result.stdout + result.stderr
    assert "ai-descriptions: NOT_CHECKED - no semantic model found" in result.stdout
    assert "model is EXTERNAL" not in result.stdout
    assert f"{cu.MODEL_REFERENCE_ID}:" not in result.stdout
    assert "not_checked_external=0" in result.stdout


def test_local_model_unit_is_unchanged_by_the_fix() -> None:
    """A per-workbook unit whose model ships beside it still runs its model gates for real."""
    result = _run_unit(STATE_TARGETS["model-local"], "model")

    assert result.returncode == cu.EXIT_NOT_CHECKED, result.stdout + result.stderr
    assert "data-model: PASS" in result.stdout
    assert "sqlproxy-connections: PASS" in result.stdout
    assert "model is EXTERNAL" not in result.stdout
    assert f"{cu.MODEL_REFERENCE_ID}:" not in result.stdout
    assert "not_checked_external=0" in result.stdout


def _normalize_unit_stdout(text: str, target: Path) -> str:
    """Make check_unit stdout portable: strip machine paths, the interpreter, and OS separators."""
    return (
        text.replace("\r\n", "\n")
        .replace(sys.executable, "<PY>")
        .replace(str(target), "<UNIT>")
        .replace(str(REPO_ROOT), "<REPO>")
        .replace("\\", "/")
    )


def test_external_resolves_scope_model_matches_golden() -> None:
    """Lock the actionable EXTERNAL wording, per-gate rows, summary buckets, and brownfield line."""
    target = STATE_TARGETS["external-resolves"]
    golden = REPO_ROOT / "tests" / "golden" / "shared-datasource" / "external-resolves.model.stdout"

    result = _run_unit(target, "model")

    assert result.returncode == cu.EXIT_NOT_CHECKED, result.stdout + result.stderr
    expected = golden.read_text(encoding="utf-8")
    old = "omitted checks: desktop-orphans, engine-receipt, finalized,"
    new = (
        "omitted checks: current-snapshot, current-source-data, current-working-namespace, data-evidence, "
        "desktop-orphans, engine-receipt, finalized, iteration-findings, iteration-history, model-class, numeric-obligation,"
    )
    assert old in expected
    actual = _normalize_unit_stdout(result.stdout, target)
    assert next(line for line in actual.splitlines() if line.startswith("SUMMARY:")) == (
        "SUMMARY: blockers=2; compromises=0; compromises_not_evaluated=1; findings_by_owner=none; "
        "not_checked_external=9; not_checked_missing_input=2; ladder=NOT_CHECKED exit=2"
    )
    assert [line for line in actual.splitlines() if not line.startswith("SUMMARY:")] == [
        line for line in expected.replace(old, new).splitlines() if not line.startswith("SUMMARY:")
    ]


# --- path-ceiling: whole-unit shippability, wired into the facade (refs #235) -------------------
#
# The gate answers "would this unit survive on a stock Windows machine", which is a property of the
# whole target tree, so it is registered ALL-scope only and owned by the orchestrator. These tests
# pin the two decisions that are easy to reverse by accident: that an over-ceiling unit is a FINDING
# (not a quiet pass, and not demoted to advisory), and that anything unmeasurable is NOT_CHECKED.


def _path_ceiling_gate() -> cu.Gate:
    return _gate_by_id("path-ceiling")


def _path_ceiling_gate_thresholds() -> tuple[object, ...]:
    """The registered gate's status/exit sets, reused so a real-scanner test cannot drift from it."""
    gate = _path_ceiling_gate()
    return (
        gate.pass_statuses,
        gate.pass_exit_codes,
        gate.finding_statuses,
        gate.finding_exit_codes,
        gate.not_checked_statuses,
        gate.not_checked_exit_codes,
    )


def _deep_tree(root: Path) -> Path:
    """A small real tree, so the native scanner has something to measure."""
    leaf = root / "fabric" / "Book.Report" / "definition" / "pages" / "51c062066e7c504dcbb5"
    leaf.mkdir(parents=True, exist_ok=True)
    (leaf / "page.json").write_text("{}", encoding="utf-8")
    return root


def test_path_ceiling_is_registered_all_scope_only() -> None:
    """Whole-unit shippability cannot be attributed to a layer, so it runs only under --scope all."""
    gate = _path_ceiling_gate()

    assert gate.script == "check_path_ceiling.py"
    assert "path-ceiling" in cu.ALL_ONLY_CHECK_IDS
    assert cu._in_scope("path-ceiling", cu.SCOPE_ALL)  # pylint: disable=protected-access
    for scope in (cu.SCOPE_MODEL, cu.SCOPE_REPORT, cu.SCOPE_INTEGRATION):
        assert not cu._in_scope("path-ceiling", scope)  # pylint: disable=protected-access
        assert "path-ceiling" in cu._omitted_checks(scope)  # pylint: disable=protected-access


def test_path_ceiling_owner_is_the_orchestrator_not_a_builder() -> None:
    """A breach is driven by install-root length and engine-side naming; no builder persona can fix it."""
    hint = cu.OWNER_HINTS["path-ceiling"]

    assert hint.startswith("orchestrator")
    assert hint not in {"model", "report"}


def test_path_ceiling_gate_names_the_native_skip_statuses() -> None:
    """Pinned as a contract, because exit 3 alone would mask a renamed status behind the same verdict."""
    gate = _path_ceiling_gate()

    assert {"unknown_paths", "no_paths"} <= gate.not_checked_statuses
    assert gate.pass_statuses == frozenset({"ok"})
    assert gate.finding_statuses == frozenset({"over_ceiling"})


def test_path_ceiling_over_ceiling_is_a_finding_not_a_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The facade must never be the one place a bundle Desktop cannot open reads as done."""
    gate = _path_ceiling_gate()
    (tmp_path / "path-ceiling.json").write_text(
        json.dumps({"status": "over_ceiling", "counted": {"over_ceiling": 183, "measured": 12043}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cu, "_run_simple", lambda argv: _completed(argv, 1))

    check = cu._run_cli_gate(gate, tmp_path, tmp_path)  # pylint: disable=protected-access

    assert check["status"] == cu.STATUS_FINDINGS
    assert check["native_status"] == "over_ceiling"
    assert check["native_exit"] == 1


def test_path_ceiling_clean_scan_is_a_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A measured, in-budget tree passes - the gate must not be permanently red."""
    gate = _path_ceiling_gate()
    (tmp_path / "path-ceiling.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    monkeypatch.setattr(cu, "_run_simple", lambda argv: _completed(argv, 0))

    check = cu._run_cli_gate(gate, tmp_path, tmp_path)  # pylint: disable=protected-access

    assert check["status"] == cu.STATUS_PASS


@pytest.mark.parametrize("native_status", ["unknown_paths", "no_paths"])
def test_path_ceiling_unmeasured_tree_is_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_status: str
) -> None:
    """Unmeasurable and unmeasured are both 'no opinion', never clean."""
    gate = _path_ceiling_gate()
    (tmp_path / "path-ceiling.json").write_text(json.dumps({"status": native_status}), encoding="utf-8")
    monkeypatch.setattr(cu, "_run_simple", lambda argv: _completed(argv, 3))

    check = cu._run_cli_gate(gate, tmp_path, tmp_path)  # pylint: disable=protected-access

    assert check["status"] == cu.STATUS_NOT_CHECKED
    assert check["native_status"] == native_status


def test_path_ceiling_gate_accepts_the_native_scripts_real_over_ceiling_contract(tmp_path: Path) -> None:
    """Run the REAL scanner and feed it through the REGISTERED gate, so a renamed status is caught.

    The ceiling is tightened rather than building a genuinely 260-character path: the status strings
    and exit codes under test are the contract, and they are identical either way.
    """
    target = _deep_tree(tmp_path / "unit")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    gate = cu.Gate(
        "path-ceiling",
        "check_path_ceiling.py",
        ("--ceiling", "40", "--dir-ceiling", "40"),
        *_path_ceiling_gate_thresholds(),
    )

    check = cu._run_cli_gate(gate, target, output_dir)  # pylint: disable=protected-access

    assert check["native_status"] == "over_ceiling", check
    assert check["native_exit"] == 1
    assert check["status"] == cu.STATUS_FINDINGS


def test_path_ceiling_gate_accepts_the_native_scripts_real_ok_contract(tmp_path: Path) -> None:
    """The same real scanner, in budget: 'ok'/0 must satisfy the registered pass sets."""
    target = _deep_tree(tmp_path / "unit")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    gate = cu.Gate(
        "path-ceiling",
        "check_path_ceiling.py",
        ("--ceiling", "4000", "--dir-ceiling", "4000", "--warn-at", "3999"),
        *_path_ceiling_gate_thresholds(),
    )

    check = cu._run_cli_gate(gate, target, output_dir)  # pylint: disable=protected-access

    assert check["native_status"] == "ok", check
    assert check["native_exit"] == 0
    assert check["status"] == cu.STATUS_PASS


def test_path_ceiling_detail_carries_root_budget_and_says_it_is_host_relative() -> None:
    """--quiet hides the numbers, so the facade row must state what makes the verdict judgeable."""
    check = cu._annotate_path_ceiling(  # pylint: disable=protected-access
        {
            "detail": None,
            "payload": {
                "status": "over_ceiling",
                "root_length": 74,
                "root_budget": 62,
                "longest": {"length": 287},
                "counted": {"measured": 12043, "over_ceiling": 183, "unknown": 0},
            },
        }
    )

    assert "root budget 62" in check["detail"]
    assert "root length 74" in check["detail"]
    assert "183 of 12043 paths over ceiling" in check["detail"]
    assert "longest 287" in check["detail"]
    assert "shorter installation root may pass" in check["detail"]
    assert check["root_budget"] == 62


def test_path_ceiling_detail_preserves_an_existing_diagnostic() -> None:
    """The annotation adds numbers; it must not overwrite why the facade could not form an opinion."""
    check = cu._annotate_path_ceiling(  # pylint: disable=protected-access
        {"detail": "native JSON output missing", "payload": {"counted": {"unknown": 4}}}
    )

    assert check["detail"].startswith("native JSON output missing; ")
    assert "4 unmeasurable" in check["detail"]


def test_path_ceiling_annotation_invents_no_census_when_the_scan_never_ran() -> None:
    """A failed scan must not be dressed up as '0 of 0 paths over ceiling'."""
    check = cu._annotate_path_ceiling(  # pylint: disable=protected-access
        {"detail": "native JSON output missing", "payload": {"status": "ERROR"}}
    )

    assert check["detail"] == "native JSON output missing"
    assert "over ceiling" not in check["detail"]
    assert "root_budget" not in check


def test_path_ceiling_finding_names_the_offending_paths() -> None:
    """A breach the reader cannot locate is not actionable; worst_offenders must reach the render."""
    payload = {
        "status": "over_ceiling",
        "worst_offenders": [
            {"path": "C:/x/pbip/NAME/NAME.Report/definition/pages/aaa/visuals/bbb", "kind": "directory", "length": 287}
        ],
    }

    findings = cu._payload_findings(payload)  # pylint: disable=protected-access

    assert findings, "over-ceiling paths must render as findings"
    assert "NAME.Report" in findings[0]


def test_path_ceiling_runs_end_to_end_on_a_real_unit(tmp_path: Path) -> None:
    """The wiring must survive the real CLI: registered, run under --scope all, and annotated.

    The JSON assertion is what pins the ``_annotate_path_ceiling`` hook - on a PASS row the console
    never renders ``detail``, so a stdout-only test would not notice the hook being dropped.
    """
    target = REPO_ROOT / "examples" / "shipping-kpis"
    report_json = tmp_path / "unit.json"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_unit.py"),
            str(target),
            "--scope",
            "all",
            "--json",
            str(report_json),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert "path-ceiling: PASS (native ok exit 0; root budget " in result.stdout, result.stdout
    payload = json.loads(report_json.read_text(encoding="utf-8"))
    row = next(check for check in payload["checks"] if check["id"] == "path-ceiling")
    assert row["status"] == cu.STATUS_PASS
    assert isinstance(row["root_budget"], int)
    assert "root budget" in row["detail"]


# --- the PASS row must carry the relocation risk (PR #398 review) ------------------------------
#
# `_render_actionable_detail` renders `detail` for non-clean rows ONLY, so the annotation above is
# invisible on exactly the row nobody reads twice. Measured on a byte-identical tree: root length 65
# passes with root_budget 79; root length 94 breaches. The headline clause is therefore rendered at
# every status, and unconditionally rather than only when `root_budget_is_tight` - which was False
# (79 >= the advisory 40) for that very reproduction.


def _pass_row(budget: int, *, tight: bool = False) -> dict[str, object]:
    return cu._annotate_path_ceiling(  # pylint: disable=protected-access
        {
            "detail": None,
            "payload": {
                "status": "ok",
                "root_length": 65,
                "root_budget": budget,
                "root_budget_is_tight": tight,
                "shipping_root_budget_advisory": 40,
                "longest": {"length": 243},
                "counted": {"measured": 58, "over_ceiling": 0, "unknown": 0},
            },
        }
    )


def test_path_ceiling_pass_row_headline_carries_the_root_budget() -> None:
    """A clean row that says only PASS hides the whole result of this particular scan."""
    note = cu._path_ceiling_budget_note(_pass_row(79))  # pylint: disable=protected-access

    assert "root budget 79" in note
    assert "breaches above a 79-char installation root" in note


def test_path_ceiling_budget_is_shown_even_when_not_tight() -> None:
    """The tight flag would have stayed silent on the exact tree that motivated this finding."""
    check = _pass_row(79, tight=False)

    assert check["root_budget_is_tight"] is False
    assert cu._path_ceiling_budget_note(check)  # pylint: disable=protected-access
    assert "TIGHT" not in cu._path_ceiling_budget_note(check)  # pylint: disable=protected-access


def test_path_ceiling_tight_budget_is_escalated_in_the_headline() -> None:
    """Always showing the number does not cost the advisory its teeth."""
    note = cu._path_ceiling_budget_note(_pass_row(30, tight=True))  # pylint: disable=protected-access

    assert "root budget 30 TIGHT" in note


def test_path_ceiling_headline_is_silent_when_there_is_no_budget() -> None:
    """A scan that never ran has no budget to report, and must not invent one."""
    assert cu._path_ceiling_budget_note({"id": "path-ceiling"}) == ""  # pylint: disable=protected-access


def _render_one(check: dict[str, object]) -> str:
    """Render a single-check report, so renderer behaviour is testable without a real unit."""
    return cu.render(
        {
            "version": 1,
            "target": "unit",
            "scope": cu.SCOPE_ALL,
            "omitted_checks": [],
            "status": cu.STATUS_AUTOMATED_PASS,
            "exit_code": 0,
            "stopped_after": None,
            "exemptions": {"path": None, "accepted": 0, "invalid": 0},
            "checks": [check],
            "brownfield": {},
        }
    )


def test_path_ceiling_render_shows_the_budget_on_a_passing_row() -> None:
    """The whole point of the fix: a PASS row must not hide the relocation risk.

    ``_render_actionable_detail`` returns nothing for a clean row, so before this the console printed
    only 'path-ceiling: PASS (native ok exit 0)' for a bundle that breaks on relocation.
    """
    row = _pass_row(79)
    row.update({"id": "path-ceiling", "status": cu.STATUS_PASS, "native_status": "ok", "native_exit": 0})

    out = _render_one(row)

    assert "path-ceiling: PASS (native ok exit 0; root budget 79" in out, out
    assert "breaches above a 79-char installation root" in out


def test_path_ceiling_render_shows_the_budget_even_when_not_tight() -> None:
    """Gating the line on root_budget_is_tight would have stayed silent on the motivating tree."""
    row = _pass_row(79, tight=False)
    row.update({"id": "path-ceiling", "status": cu.STATUS_PASS, "native_status": "ok", "native_exit": 0})
    assert row["root_budget_is_tight"] is False

    assert "root budget 79" in _render_one(row)


def test_path_ceiling_impossible_budget_says_no_root_can_hold_it() -> None:
    """A negative budget is not 'relocate somewhere shorter' - nothing anywhere would open."""
    check = cu._annotate_path_ceiling(  # pylint: disable=protected-access
        {
            "detail": None,
            "payload": {
                "status": "over_ceiling",
                "root_length": 65,
                "root_budget": -5,
                "longest": {"length": 320},
                "counted": {"measured": 58, "over_ceiling": 2, "unknown": 0},
            },
        }
    )

    assert "NO installation root can hold this tree" in check["detail"]
    assert "NO installation root can hold this tree" in cu._path_ceiling_budget_note(check)  # pylint: disable=protected-access


def test_path_ceiling_wording_is_status_aware() -> None:
    """A pass and a breach need opposite sentences; 'a shorter root may pass' is vacuous on a pass."""
    passing = _pass_row(79)["detail"]
    breaching = cu._annotate_path_ceiling(  # pylint: disable=protected-access
        {
            "detail": None,
            "payload": {
                "status": "over_ceiling",
                "root_length": 94,
                "root_budget": 79,
                "longest": {"length": 272},
                "counted": {"measured": 58, "over_ceiling": 2, "unknown": 0},
            },
        }
    )["detail"]

    assert "LONGER than 79 characters WILL breach" in passing
    assert "may pass" not in passing
    assert "at most 79 characters and this one is 94" in breaching
    assert "shorter installation root may pass" in breaching


def test_path_ceiling_pass_then_relocate_breaches_and_the_pass_said_so(tmp_path: Path) -> None:
    """The reviewer's reproduction, committed: one tree, two roots, and the PASS row warned about it.

    Both the tree depth and the relocation distance are DERIVED from a calibration scan rather than
    hard-coded. Providing explicit ceilings sized relative to the runner's temp-directory length makes
    the test deterministic across arbitrary temp paths (preventing skips when pytest run numbers change
    temp dir length) without encoding scanner layout internals. Overshooting the budget by one keeps
    the binding path one unit over its OWN ceiling, so nothing created here needs Windows long-path
    support.
    """
    short_root = tmp_path / "s"
    root_len = len(str(short_root))
    ceiling_args = ["--ceiling", str(root_len + 150), "--dir-ceiling", str(root_len + 138)]
    pad = _pad_for_headroom(short_root, tmp_path, headroom=12, extra_args=ceiling_args)
    assert pad is not None, "controlled headroom calibration must succeed"

    short = _scan_paths(_deep_tree_with_pad(short_root, pad), tmp_path / "short.json", extra_args=ceiling_args)
    assert short["status"] == "ok", short
    budget, root_length = short["root_budget"], short["root_length"]
    assert budget >= root_length

    long_root = tmp_path / ("s" + "x" * (budget - root_length + 1))
    after = _scan_paths(_deep_tree_with_pad(long_root, pad), tmp_path / "long.json", extra_args=ceiling_args)

    assert after["status"] == "over_ceiling", after
    note = cu._path_ceiling_budget_note(  # pylint: disable=protected-access
        cu._annotate_path_ceiling({"detail": None, "payload": short})  # pylint: disable=protected-access
    )
    assert f"breaches above a {budget}-char installation root" in note


def _deep_tree_with_pad(root: Path, pad: int) -> Path:
    """A PBIR-shaped unit whose deepest path is driven by one padded page-id component."""
    unit = root / "u"
    leaf = unit / "fabric" / "Book.Report" / "definition" / "pages" / ("p" * pad)
    leaf.mkdir(parents=True, exist_ok=True)
    (leaf / "page.json").write_text("{}", encoding="utf-8")
    return unit


def _pad_for_headroom(root: Path, scratch: Path, headroom: int, extra_args: list[str] | None = None) -> int | None:
    """Calibrate the padding that leaves exactly ``headroom`` characters of root budget.

    ``root_budget`` falls one-for-one with the padded component, so one measured probe fixes the
    constant without this test knowing the scanner's ceilings or path layout.
    """
    probe = _scan_paths(_deep_tree_with_pad(root, 1), scratch / "probe.json", extra_args=extra_args)
    pad = probe["root_budget"] + 1 - probe["root_length"] - headroom
    shutil.rmtree(root, ignore_errors=True)
    return pad if 1 <= pad <= 200 else None


def _scan_paths(unit: Path, json_path: Path, extra_args: list[str] | None = None) -> dict:
    """Run the real scanner and return its machine-readable report."""
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "check_path_ceiling.py"),
        str(unit),
        "--json",
        str(json_path),
        "--quiet",
    ]
    if extra_args:
        cmd.extend(extra_args)
    subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(json_path.read_text(encoding="utf-8"))


# --- round 6: the _slug audit adjudicated -----------------------------------------------------
# Three sites the round-5 prose audit classified "safe" were not. Each test below is the measured
# reproduction, inverted.


def test_one_scaffold_signature_cannot_exempt_two_findings(tmp_path: Path) -> None:
    """Kills: a signature named 'A-B' exempting BOTH table 'A-B' and table 'A B' from one entry.

    Measured before this: the gate flipped to PASS and reported *two* exemptions from *one* signature,
    because ``_exempted`` compared slugs per finding and never asked how many findings the raw entry
    matched in total.
    """
    _write_handover(
        tmp_path,
        {
            "name": "Unit",
            "partitions_needs_review": [
                {"kind": "m_partition", "table": "A-B", "reason": "flat-file source"},
                {"kind": "m_partition", "table": "A B", "reason": "flat-file source"},
            ],
        },
    )
    _write_exemptions(tmp_path, [{"check": "scaffold-partitions", "item": "A-B"}])

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)
    scaffold = next(check for check in report["checks"] if check["id"] == "scaffold-partitions")

    assert scaffold["status"] == cu.STATUS_FINDINGS
    assert scaffold["scaffold_exemptions"] == 1
    assert scaffold["unexempted_scaffolds"] == 1


def test_a_scaffold_signature_matching_two_findings_only_by_slug_applies_to_neither(tmp_path: Path) -> None:
    """Kills: applying a lossy signature that names no finding exactly but slugs onto two."""
    _write_handover(
        tmp_path,
        {
            "name": "Unit",
            "partitions_needs_review": [
                {"kind": "m_partition", "table": "A-B", "reason": "flat-file source"},
                {"kind": "m_partition", "table": "A B", "reason": "flat-file source"},
            ],
        },
    )
    _write_exemptions(tmp_path, [{"check": "scaffold-partitions", "item": "a/b"}])

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)
    scaffold = next(check for check in report["checks"] if check["id"] == "scaffold-partitions")

    assert scaffold["status"] == cu.STATUS_FINDINGS
    assert scaffold["scaffold_exemptions"] == 0
    assert scaffold["unexempted_scaffolds"] == 2
    assert scaffold["contested_exemptions"] == ["a/b"]


def test_a_scaffold_signature_still_applies_when_it_names_exactly_one_finding(tmp_path: Path) -> None:
    """The refusal above must not swallow the ordinary case: one signature, one finding, applied."""
    _write_handover(
        tmp_path,
        {
            "name": "Unit",
            "partitions_needs_review": [
                {"kind": "m_partition", "table": "A-B", "reason": "flat-file source"},
                {"kind": "m_partition", "table": "Orders", "reason": "flat-file source"},
            ],
        },
    )
    _write_exemptions(tmp_path, [{"check": "scaffold-partitions", "item": "a b"}])

    report = cu.run_all(tmp_path, scope=cu.SCOPE_MODEL)
    scaffold = next(check for check in report["checks"] if check["id"] == "scaffold-partitions")

    assert scaffold["scaffold_exemptions"] == 1
    assert scaffold["contested_exemptions"] == []
    assert [row["table"] for row in scaffold["scaffolds"] if row["exempted"]] == ["A-B"]


def test_two_artifacts_slugging_alike_refuse_a_single_handover_workbook(tmp_path: Path) -> None:
    """Kills: the uniqueness guard held on the handover side only.

    Measured: a unit shipping ``Bo ok.Report`` and ``Bo-ok.Report`` collapsed to ONE target key
    ``book``, and a single handover workbook ``Book`` was accepted even though either artifact could
    own it. The guard has to hold on both sides of the join.
    """
    _write_spec(tmp_path, ["Sales"])
    _write_report(tmp_path, ["Sales"], name="Bo ok")
    _write_report(tmp_path, ["Sales"], name="Bo-ok")
    _write_viz_fidelity_handover(tmp_path, [_empty_row("Sales")], workbook_name="Book")

    explanations = cu.page_drop_explanations(tmp_path)

    assert explanations["available"] is False
    assert explanations["unbound_workbooks"] == ["Book"]


def test_one_artifact_still_binds_a_slugged_handover_workbook(tmp_path: Path) -> None:
    """The fallback survives its guard: one artifact, one handover, differently spelled, binds."""
    _write_spec(tmp_path, ["Sales"])
    _write_report(tmp_path, ["Sales"], name="Bo ok")
    _write_viz_fidelity_handover(tmp_path, [_empty_row("Sales")], workbook_name="Bo-ok")

    explanations = cu.page_drop_explanations(tmp_path)

    assert explanations["available"] is True
    assert explanations["unbound_workbooks"] == []


def test_reference_evidence_never_satisfies_a_differently_spelled_page(tmp_path: Path) -> None:
    """Kills: reference evidence named 'A B' giving a validation-grade PASS to expected page 'A-B'.

    Round 6 narrowed this to a uniqueness-guarded fallback; round 7 removed it. A view name and a
    page name are BOTH source-owned - they come out of the same Tableau workbook with no filesystem
    in between - so no mechanism re-spells one into the other, and a guard on a fallback with no
    mechanism behind it only narrows a match that was never justified.
    """
    _write_spec(tmp_path, ["A-B"])
    _write_report(tmp_path, ["A-B"])
    _write_reference_manifest(tmp_path, ["A B"])

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["refused_evidence"] == []


def test_reference_evidence_satisfies_the_page_it_names_exactly(tmp_path: Path) -> None:
    """The other half: removing the fallback must not stop exact evidence counting."""
    _write_spec(tmp_path, ["A-B"])
    _write_report(tmp_path, ["A-B"])
    _write_reference_manifest(tmp_path, ["A-B"])

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_PASS
    assert oracle["visual_present"] == 1


def test_exact_reference_evidence_beats_a_same_named_sibling(tmp_path: Path) -> None:
    """A dashboard and a worksheet named alike: evidence certifies the one whose KIND it declares."""
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[("ws.other", "Other")])
    _write_report(tmp_path, ["Sales", "Other"])
    _write_reference_manifest(tmp_path, ["Sales"])

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 1
    assert [row["page"]["name"] for row in oracle["rows"] if row["visual"]] == ["Sales"]


def test_oracle_evidence_from_a_different_workbook_satisfies_nothing(tmp_path: Path) -> None:
    """Kills: an oracle record for 'Revenue' produced by 'Different Workbook' passing this unit.

    Drop evidence was bound to its producing workbook in round 2. Oracle evidence never was, so the
    round-2 cross-workbook defect survived at the oracle layer.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_oracle_manifest(tmp_path, ["Revenue"], workbook=None, workbook_luid=OTHER_LUID)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["numeric_present"] == 0
    assert oracle["foreign_workbook_evidence"] == [f"luid='{OTHER_LUID}'"]


def test_oracle_evidence_from_this_workbook_still_counts(tmp_path: Path) -> None:
    """The workbook guard must not reject the unit's own evidence."""
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_oracle_manifest(tmp_path, ["Revenue"], workbook="Book")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 1
    assert oracle["foreign_workbook_evidence"] == []


# --------------------------------------------------------------------------------------------
# Issue #450: the workbook guard read a key no capture producer writes, and failed OPEN
# --------------------------------------------------------------------------------------------


def test_a_record_whose_workbook_cannot_be_established_certifies_nothing(tmp_path: Path) -> None:
    """Kills issue #450's fail-open, which is what this test used to ASSERT.

    ⚠️ It previously read *"declaring no workbook is admitted but flagged"*, on the premise that "real
    oracle manifests carry no workbook field, so refusing them outright would reject every capture in
    the estate". The premise was false in the only way that mattered: a real manifest carries
    ``workbook_luid`` and ``workbook_name`` per view - it carries no ``workbook`` key, which is a
    defect in the READER, not a property of the producer. Measured on a real 360-view capture, every
    record was therefore "unattributed" and admitted anyway, so the guard advertised in
    :class:`OracleEvidence` had never once fired and a foreign workbook's render could certify any
    page whose name it shared.

    A record that establishes no workbook at all now certifies nothing. It is still counted, because
    a refusal nobody can see is not a guard.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_oracle_manifest(tmp_path, ["Revenue"], workbook=None, workbook_luid=None)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["unattributed_evidence"] == 1
    assert oracle["admitted_evidence"] == 0
    # Counted ONCE, and not as another workbook's: "I cannot tell whose this is" and "I can tell, and
    # it is not yours" are different operator actions - re-capture with typing vs ignore it - and a
    # record that lands in both buckets tells the reader neither. It is also the only observable
    # difference left if the explicit refusal here is deleted, because the lossy rescue below refuses
    # an unestablished record too; measured by mutation, that deletion is otherwise silent.
    assert oracle["foreign_workbook_evidence"] == []
    assert "1 record(s) establish no producing workbook" in oracle["grade"]


def test_a_display_name_alone_never_certifies_however_real_the_capture(tmp_path: Path) -> None:
    """The positive half of #450: `capture_tableau_oracle.py` writes `workbook_name` PER VIEW.

    Without this the fix above is indistinguishable from "refuse everything", which would make the
    gate report zero coverage on every real capture in the estate.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_oracle_manifest(tmp_path, ["Revenue"], workbook=None, workbook_luid=None, workbook_name="Book")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 0, "a display name is decoration, not identity"
    assert oracle["admitted_evidence"] == 0
    assert oracle["name_only_evidence"] == ["name='Book'"]
    assert any("NAME-ONLY EVIDENCE REFUSED" in caveat for caveat in oracle["known_gap_caveats"])


def test_a_foreign_display_name_is_refused_too_and_counted_separately(tmp_path: Path) -> None:
    """The discriminating twin: reading the right field must REFUSE as readily as it admits."""
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_oracle_manifest(tmp_path, ["Revenue"], workbook=None, workbook_luid=None, workbook_name="Different Workbook")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 0
    # A name that DIFFERS was compared and disagreed, so it is `foreign`; a name that MATCHES is
    # `name_only`, because agreement on decoration establishes nothing. Both refuse; they are
    # counted apart because they call for different operator actions.
    assert oracle["foreign_workbook_evidence"] == ["name='Different Workbook'"]
    assert oracle["name_only_evidence"] == []


def test_a_foreign_luid_is_refused_even_when_the_workbook_name_matches_exactly(tmp_path: Path) -> None:
    """Kills: a weaker axis rescuing a record a stronger one has already rejected.

    Two projects may hold workbooks with the same display name - the ambiguity `_runs/<NNN>-<slug>/`
    numbering exists to avoid - so a name that agrees after a LUID that does not is exactly the case
    where the name must not be consulted. The unit's own LUID comes from its handover slice's
    `workbook.source_id`, whose basename is `harvest_estate_assets.py`'s `<luid>_<name>`.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_handover(tmp_path, {"name": "Book", "source_id": "assets/adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_Book.twbx"})
    _write_oracle_manifest(
        tmp_path,
        ["Revenue"],
        workbook=None,
        workbook_luid="007f70ac-bf40-4838-9d73-134d40f504db",
        workbook_name="Book",
    )

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 0
    assert oracle["foreign_workbook_evidence"] == ["luid='007f70ac-bf40-4838-9d73-134d40f504db', name='Book'"]
    assert oracle["known_gap_caveats"] == [], "a LUID disagreement is not rescuable by a lossy name"


def test_a_foreign_luid_is_refused_when_this_unit_cannot_establish_a_luid_at_all(tmp_path: Path) -> None:
    """BLOCKER 1 at the exit gate, and the case the test above structurally cannot reach.

    ⚠️ The negative test beside this one gives the unit a LUID, so it only ever exercises *LUID vs
    LUID*. Round-1 review of PR #454 measured the gap: with NO unit LUID the guard skipped the
    record's LUID as "not shared" and admitted it on an equal display name - `PASS`, visual AND
    numeric, from a foreign workbook. Real oracle records always carry both fields, so this was the
    ordinary case rather than an edge one.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    spec = json.loads((tmp_path / "migration-spec.json").read_text(encoding="utf-8"))
    del spec["source"]
    (tmp_path / "migration-spec.json").write_text(json.dumps(spec), encoding="utf-8")
    _write_oracle_manifest(
        tmp_path,
        ["Revenue"],
        workbook=None,
        workbook_luid="007f70ac-bf40-4838-9d73-134d40f504db",
        workbook_name="Book",
    )

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["numeric_present"] == 0
    assert oracle["admitted_evidence"] == 0
    assert oracle["unattributed_evidence"] == 1


def test_a_windows_recorded_source_id_still_yields_its_luid(tmp_path: Path) -> None:
    """BLOCKER 4 at this gate's call site: a handover records a path from ANOTHER host.

    ⚠️ This assertion is a no-op on Windows and load-bearing on Linux, and that asymmetry is the
    defect rather than a flaw in the test. `WindowsPath` accepts both separators, so a Windows
    workstation reports the guard working while a Linux CI runner - where those backslashes are
    ordinary filename characters - gets no LUID at all and falls back to a weaker axis. CI runs on
    Linux, so this test is exercised exactly where the divergence bites; the flavour-explicit proof
    that runs anywhere is `test_workbook_identity.test_the_recorded_path_parse_does_not_use_the_running_hosts_flavour`.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_handover(
        tmp_path,
        {
            "name": "Book",
            "source_id": "_runs\\407-dryrun-gates\\assets\\adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_Book.twbx",
        },
    )

    assert set(cu._unit_source_claims(tmp_path)[1]) == {"adc431bb-aeeb-43fe-8ecb-092d4bae8bfa"}
    assert [identity.luid for identity in cu._unit_workbook_identities(tmp_path)] == [
        "adc431bb-aeeb-43fe-8ecb-092d4bae8bfa"
    ]


def test_a_matching_luid_admits_a_record_whose_display_name_the_filesystem_changed(tmp_path: Path) -> None:
    """The positive control for the LUID route, and symptom A of #450 in this gate.

    A unit ships `Book.Report` while the site publishes `Bo ok`; the stem is a sanitised spelling, not
    the name. The LUID is what bridges them, and it is exact.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_handover(tmp_path, {"name": "Book", "source_id": "assets/adc431bb-aeeb-43fe-8ecb-092d4bae8bfa_Book.twbx"})
    _write_oracle_manifest(
        tmp_path,
        ["Revenue"],
        workbook=None,
        workbook_luid="ADC431BB-AEEB-43FE-8ECB-092D4BAE8BFA",
        workbook_name="Bo ok",
    )

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 1
    assert oracle["foreign_workbook_evidence"] == []
    assert oracle["known_gap_caveats"] == [], "an exact LUID match is not a loose attribution"


def test_a_unit_local_reference_manifest_is_not_certified_by_its_location(tmp_path: Path) -> None:
    """BLOCKER 3 from round-1 review of PR #454. This test asserted the OPPOSITE.

    ⚠️ It read *"a reference manifest inside the unit is attributable by location"*, on the argument
    that `_reference_dirs` never walks up so such a manifest is this unit's by construction. Measured
    consequence: the guard was SKIPPED for those records, so a manifest with no workbook identity at
    all was admitted (`admitted_evidence=1`, visual AND numeric certified) - and so was one whose
    `source_workbook_sha256` named a **different workbook** entirely.

    Location controls DISCOVERY; it never substitutes for identity. A manifest that establishes no
    workbook now certifies nothing, and is counted.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_reference_manifest(tmp_path, ["Revenue"], workbook=None, source_sha=None)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["numeric_present"] == 0
    assert oracle["admitted_evidence"] == 0
    assert oracle["unattributed_evidence"] == 1


def test_a_unit_local_reference_manifest_certifies_when_its_recorded_sha_is_this_source(tmp_path: Path) -> None:
    """The positive twin: the identity a reference manifest DOES carry is `source_workbook_sha256`.

    `capture_tableau_reference.py:234` writes it, so the fix for blocker 3 is to hash the unit's own
    source asset and compare - not to refuse the whole producer. Without this twin, "refuse every
    reference record" would pass the test above and delete the only validation-grade route in the
    toolkit.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    source = _write_unit_source(tmp_path, b"the workbook this unit was built from")
    _write_reference_manifest(tmp_path, ["Revenue"], workbook=None, source_sha=_sha256(source))

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_PASS
    assert oracle["visual_present"] == 1
    assert oracle["admitted_evidence"] == 1
    assert oracle["unattributed_evidence"] == 0


def test_a_unit_local_reference_manifest_recording_another_workbooks_sha_is_refused(tmp_path: Path) -> None:
    """The negative twin, and the sharper half of blocker 3: a MISMATCHING sha was admitted too."""
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_unit_source(tmp_path, b"the workbook this unit was built from")
    _write_reference_manifest(tmp_path, ["Revenue"], workbook=None, source_sha="de" * 32)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 0
    assert oracle["admitted_evidence"] == 0
    assert oracle["foreign_workbook_evidence"] == ["sha256=dededededede..."]


def test_a_manifest_whose_luid_contradicts_its_matching_sha_certifies_nothing(tmp_path: Path) -> None:
    """BLOCKING FINDING B at the EXIT gate. Verbatim before this fix::

        CONTRADICTORY_EXIT={"status":"PASS","visual_present":1,"numeric_present":1,
          "admitted_evidence":1,"foreign_workbook_evidence":[],"unattributed_evidence":0}

    The unit's own LUID comes from its spec's `<luid>_<name>` asset filename and its sha256 from that
    asset's bytes, so this manifest agrees with one and contradicts the other. `_attribute_record` is
    asserted directly because it names WHICH guard refused: `visual_present == 0` alone is produced
    by at least four other guards in this gate, and `unattributed_evidence`/`name_only_evidence` are
    pinned to zero so an accidental downgrade to a weaker refusal fails here too.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    source = _write_unit_source(tmp_path, b"the workbook this unit was built from")
    _write_reference_manifest(
        tmp_path,
        ["Revenue"],
        workbook=None,
        source_sha=_sha256(source),
        workbook_luid="007f70ac-bf40-4838-9d73-134d40f504db",
    )

    records, _ = cu._reference_oracles(tmp_path, None)
    verdict = cu._attribute_record(cu._unit_workbook_identities(tmp_path), records[0])
    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert verdict.route == oid.WB_CONFLICT
    assert verdict.admitted is False
    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert (oracle["visual_present"], oracle["numeric_present"], oracle["admitted_evidence"]) == (0, 0, 0)
    assert oracle["unattributed_evidence"] == 0 and oracle["name_only_evidence"] == []
    assert oracle["foreign_workbook_evidence"] != [], "a refusal nobody can see is not a guard"


def test_a_manifest_whose_luid_agrees_with_its_matching_sha_still_certifies(tmp_path: Path) -> None:
    """The positive control: two agreeing machine axes are the STRONGEST evidence, not a conflict."""
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    source = _write_unit_source(tmp_path, b"the workbook this unit was built from")
    _write_reference_manifest(tmp_path, ["Revenue"], workbook=None, source_sha=_sha256(source), workbook_luid=UNIT_LUID)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_PASS
    assert (oracle["visual_present"], oracle["admitted_evidence"]) == (1, 1)


# --------------------------------------------------------------------------------------------
# Round 2 of PR #454: conflicting LUID declarations at different SCOPES were collapsed to one
# --------------------------------------------------------------------------------------------


def _luid_at_scopes(unit: Path, *, entry: str | None = None, state: str | None = None) -> None:
    """Stamp `workbook_luid` at the dashboard-entry and/or state scope of a written manifest."""
    manifest = unit / "reference" / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for dashboard in payload["dashboards"]:
        if entry is not None:
            dashboard["workbook_luid"] = entry
        for recorded in dashboard.get("states", []) if state is not None else []:
            recorded["workbook_luid"] = state
    manifest.write_text(json.dumps(payload), encoding="utf-8")


def test_an_entry_luid_does_not_silently_supersede_a_contradicting_manifest_luid(tmp_path: Path) -> None:
    """THE round-2 finding at the EXIT gate. Verbatim before this fix::

        B_EXIT_MULTISCOPE {"status":"PASS","visual_present":1,"numeric_present":1,
          "admitted_evidence":1,"foreign_workbook_evidence":[]}

    `_declared_workbook` read ``entry.get("workbook_luid") or outer.get("workbook_luid")``, so an
    entry-level LUID naming this unit discarded a manifest-level LUID naming another workbook and
    the contradiction never reached `WorkbookIdentity.attribute`. No schema makes the narrower scope
    an override; every non-blank claim must agree.

    ``_attribute_record`` is asserted directly because it names WHICH guard refused - at least four
    others in this gate also produce ``visual_present == 0``.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    source = _write_unit_source(tmp_path, b"the workbook this unit was built from")
    _write_reference_manifest(
        tmp_path, ["Revenue"], workbook=None, source_sha=_sha256(source), workbook_luid=OTHER_LUID
    )
    _luid_at_scopes(tmp_path, entry=UNIT_LUID)

    records, _ = cu._reference_oracles(tmp_path, None)
    verdict = cu._attribute_record(cu._unit_workbook_identities(tmp_path), records[0])
    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert verdict.route == oid.WB_CONFLICT
    assert verdict.admitted is False
    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert (oracle["visual_present"], oracle["numeric_present"], oracle["admitted_evidence"]) == (0, 0, 0)
    assert oracle["foreign_workbook_evidence"] != [], "a refusal nobody can see is not a guard"


def test_a_state_scope_luid_is_read_at_this_gate_too(tmp_path: Path) -> None:
    """The near neighbour: the entry gate reads the STATE scope and this one used not to.

    A scope one gate compares while the other ignores it is a hole by construction - measured on this
    branch before the fix, ``PASS route=sha256 admitted=1`` on a manifest whose only state declared
    another workbook's LUID. States are collapsed into one record here, so their claims are claims
    about that record and must agree with the entry's and the manifest's.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    source = _write_unit_source(tmp_path, b"the workbook this unit was built from")
    _write_reference_manifest(tmp_path, ["Revenue"], workbook=None, source_sha=_sha256(source))
    _luid_at_scopes(tmp_path, state=OTHER_LUID)

    records, _ = cu._reference_oracles(tmp_path, None)
    verdict = cu._attribute_record(cu._unit_workbook_identities(tmp_path), records[0])
    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert verdict.route == oid.WB_CONFLICT
    assert (oracle["status"], oracle["visual_present"], oracle["admitted_evidence"]) == (cu.STATUS_NOT_CHECKED, 0, 0)


def test_scopes_that_agree_or_stay_silent_still_certify_at_the_exit_gate(tmp_path: Path) -> None:
    """THE positive control: refusing every multi-scope manifest would pass both tests above.

    Every ordinary shape must still PASS on the matching sha256 - all three scopes agreeing, a case
    difference (a LUID is a machine id), a blank scope beside a real one, and no LUID at all. An
    absent claim is SILENT; only an actively contradicting one refuses.
    """
    for label, luids in (
        ("all three agree", {"manifest": UNIT_LUID, "entry": UNIT_LUID, "state": UNIT_LUID}),
        ("case differs only", {"manifest": UNIT_LUID, "state": UNIT_LUID.upper()}),
        ("blank is absence", {"manifest": UNIT_LUID, "entry": "   ", "state": None}),
        ("no luid anywhere", {}),
    ):
        unit = tmp_path / label.replace(" ", "-")
        unit.mkdir()
        _write_spec(unit, ["Revenue"])
        _write_report(unit, ["Revenue"])
        source = _write_unit_source(unit, b"the workbook this unit was built from")
        _write_reference_manifest(
            unit, ["Revenue"], workbook=None, source_sha=_sha256(source), workbook_luid=luids.get("manifest")
        )
        _luid_at_scopes(unit, entry=luids.get("entry"), state=luids.get("state"))

        records, _ = cu._reference_oracles(unit, None)
        verdict = cu._attribute_record(cu._unit_workbook_identities(unit), records[0])
        oracle = cu.check_oracle_coverage(unit, None, None)

        assert verdict.route == oid.WB_SHA, label
        assert oracle["status"] == cu.STATUS_PASS, label
        assert (oracle["visual_present"], oracle["numeric_present"], oracle["admitted_evidence"]) == (1, 1, 1), label


def test_a_sha_bearing_record_is_refused_when_the_unit_cannot_hash_its_source(tmp_path: Path) -> None:
    """Fail-CLOSED on unknown: no locatable source means the recorded sha cannot be checked.

    This is the cost of blocker 3's fix and it is stated rather than hidden - a unit that ships no
    resolvable Tableau asset cannot use sha-bearing evidence, and the refusal is disclosed in the
    grade rather than silently dropped.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_reference_manifest(tmp_path, ["Revenue"], workbook=None, source_sha="ab" * 32)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 0
    assert oracle["unattributed_evidence"] == 1
    assert "establish no producing workbook" in oracle["grade"]


def test_a_non_packaged_unit_still_reads_an_ancestors_oracle_capture(tmp_path: Path) -> None:
    """The control that makes the package test below meaningful rather than a deletion.

    `_oracle_dirs` looks beside the unit AND beside its parent, because `capture_tableau_oracle.py`
    writes one flat capture per run while a unit sits under it. Removing that walk-up would make a
    real capture invisible, which is the defect the `oracle/`-name search was added to fix.
    """
    unit = tmp_path / "unit"
    _write_spec(unit, ["Revenue"])
    _write_report(unit, ["Revenue"])
    _write_oracle_manifest(tmp_path, ["Revenue"], workbook="Book")

    assert cu.check_oracle_coverage(unit, None, None)["visual_present"] == 1


def test_a_self_contained_package_does_not_also_read_the_runs_flat_capture(tmp_path: Path) -> None:
    """Issue #451's defect ONE GATE ALONG - found here, not in the brief, and measured before fixing.

    A package at `_runs/<run>/<unit>/` beside the run's flat capture read BOTH manifests, so every
    view matched twice and this gate refused each page as "2 producer records are named 'Revenue'":
    0 visual coverage, silently, making packaging strictly worse than not packaging. Same class as
    the entry gate's "2 records share this name once normalized", so the rule lives once in
    `bundle_corpus.is_self_contained` rather than being fixed a second time here.
    """
    unit = tmp_path / "unit"
    _write_spec(unit, ["Revenue"])
    _write_report(unit, ["Revenue"])
    _write_oracle_manifest(unit, ["Revenue"], workbook="Book")
    _write_oracle_manifest(tmp_path, ["Revenue"], workbook="Book")
    assert cu.check_oracle_coverage(unit, None, None)["refused_evidence"] == ["2 producer records are named 'Revenue'"]

    (unit / "package-manifest.json").write_text("{}", encoding="utf-8")

    oracle = cu.check_oracle_coverage(unit, None, None)
    assert oracle["status"] == cu.STATUS_PASS
    assert oracle["visual_present"] == 1
    assert oracle["refused_evidence"] == []


def test_two_reference_records_with_one_name_satisfy_nothing_and_say_why(tmp_path: Path) -> None:
    """Multiplicity is the point of keeping records: two claims about one name settle nothing."""
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_reference_manifest(tmp_path, ["Revenue", "Revenue"])

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["refused_evidence"] == ["2 producer records are named 'Revenue'"]


def test_one_reference_directory_is_read_once_even_when_named_two_ways(tmp_path: Path) -> None:
    """Kills: resolving candidate reference directories AFTER deduplicating them.

    ``_unit_dir(target)`` and ``target`` are the same directory under two spellings, so the manifest
    was read twice and every dashboard produced two records - which then refused each other as "2
    producer records are named X". The same defect was fixed for handover discovery in round 3 and
    for oracle discovery; reference discovery was left behind, and was invisible until multiplicity
    stopped being collapsed.
    """
    _write_spec(tmp_path, ["Revenue"])
    _write_report(tmp_path, ["Revenue"])
    _write_reference_manifest(tmp_path, ["Revenue"])

    records, _grades = cu._reference_oracles(tmp_path, None)

    assert len(records) == 1
    assert cu.check_oracle_coverage(tmp_path, None, None)["visual_present"] == 1


def test_two_producer_records_naming_one_page_satisfy_nothing(tmp_path: Path) -> None:
    """Multiplicity is the point of keeping records: two claims about one page settle nothing."""
    _write_spec(tmp_path, ["A-B"])
    _write_report(tmp_path, ["A-B"])
    _write_reference_manifest(tmp_path, ["A-B", "A-B"])

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["refused_evidence"] == ["2 producer records are named 'A-B'"]


def test_a_relative_target_reads_one_reference_directory_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: deduplicating candidate reference directories BEFORE resolving them.

    ``_unit_dir`` resolves its result while ``target`` stays as the caller spelled it, so under the
    documented relative-path CLI shape the two candidates are unequal Path values that name one
    directory. Deduplicating first cannot see that; resolving first can. Round 3 measured the same
    ordering defect in handover discovery, where it turned a declared reason into 'ambiguous'.
    """
    unit = tmp_path / "unit"
    _write_spec(unit, ["Revenue"])
    _write_report(unit, ["Revenue"])
    _write_reference_manifest(unit, ["Revenue"])
    monkeypatch.chdir(tmp_path)

    records, _grades = cu._reference_oracles(Path("unit"), None)

    assert len(records) == 1
    assert cu.check_oracle_coverage(Path("unit"), None, None)["visual_present"] == 1


def test_a_report_and_its_model_sharing_a_stem_are_one_owner_not_a_collision(tmp_path: Path) -> None:
    """Kills: counting artifact FILES rather than distinct stems on the target side of the guard.

    Every ordinary unit ships ``<Name>.Report`` beside ``<Name>.SemanticModel``. Counting the files
    made the shared stem look like two competing owners, so the slugged fallback refused to bind for
    the whole estate - measured, it turned 19 engine-declared omissions into 21 unexplained ones. A
    report and its model are one name.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"], name="Book!")
    (tmp_path / "fabric" / "Book!.SemanticModel" / "definition").mkdir(parents=True)
    _write_viz_fidelity_handover(tmp_path, [_empty_row("B")], workbook_name="Book?")

    exact, stem_index = cu._unit_workbook_keys(tmp_path)

    assert exact == {"Book!"}
    assert stem_index.unique("Book?") == "Book!"
    assert stem_index.count("book") == 1
    assert cu.page_drop_explanations(tmp_path)["bound_workbooks"] == ["Book?"]


# --- round 7: kind-bound oracle evidence, two-sided workbook identity -------------------------


def test_a_worksheet_typed_record_cannot_certify_a_same_named_dashboard(tmp_path: Path) -> None:
    """Kills: `view_type` present in the manifest and thrown away here.

    Measured: a record explicitly carrying ``view_type: "worksheet"`` for `Sales` gave a full oracle
    PASS to the DASHBOARD `Sales`. A Tableau dashboard routinely shares its name with its principal
    worksheet, so this accepts one visual as evidence for a whole page - and that is the ordinary
    case, not an edge one.
    """
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[])
    _write_report(tmp_path, ["Sales"])
    _write_oracle_manifest(tmp_path, ["Sales"], view_type="worksheet")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["numeric_present"] == 0


def test_a_legacy_uncertified_record_is_not_numeric_evidence(tmp_path: Path) -> None:
    """#480 round 3, at `check_unit`'s numeric gate.

    ⚠️ The record here is EXACTLY what `origin/master`'s capture wrote for every HTTP 200 -- a
    `row_count` from `summarise_csv(payload)` and no `certification` -- so this is the shape a live
    customer's `_oracle/` holds, not a synthetic edge case. `check_unit` still gates on
    `status == "ok" and data["path"]` and knows nothing about certification; what changes is that
    `read_manifest` hands it a record with no `path`.

    The VISUAL half must be unaffected, which is what separates "the numeric claim is withheld" from
    "the whole capture stopped counting".
    """
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[])
    _write_report(tmp_path, ["Sales"])
    _write_oracle_manifest(tmp_path, ["Sales"], view_type="dashboard")
    path = tmp_path / "_oracle" / "oracle-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for view in manifest["views"]:
        view["data"].pop("certification", None)
    path.write_text(json.dumps(manifest), encoding="utf-8")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["numeric_present"] == 0, "a row count nothing certified must not count as numeric evidence"
    assert oracle["visual_present"] == 1, "the render evidence is a separate claim and is untouched"
    assert oracle["status"] == cu.STATUS_NOT_CHECKED


def test_a_dashboard_typed_record_certifies_the_dashboard(tmp_path: Path) -> None:
    """The kind guard must not reject evidence that DOES declare the right kind."""
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[])
    _write_report(tmp_path, ["Sales"])
    _write_oracle_manifest(tmp_path, ["Sales"], view_type="dashboard")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_PASS
    assert oracle["visual_present"] == 1
    assert oracle["kindless_evidence"] == 0


def test_a_worksheet_typed_record_certifies_a_worksheet_page(tmp_path: Path) -> None:
    """And the other kind, so the guard is not just 'dashboard or nothing'."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.sales", "Sales")])
    _write_report(tmp_path, ["Sales"])
    _write_oracle_manifest(tmp_path, ["Sales"], view_type="worksheet")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_PASS
    assert oracle["visual_present"] == 1


@pytest.mark.parametrize("declared", [None, "unknown", "story", ""])
def test_a_record_whose_kind_is_unestablished_certifies_nothing(tmp_path: Path, declared: str | None) -> None:
    """`unknown` is a REFUSAL, not a third kind - and neither is an unrecognised string.

    `capture_tableau_oracle.py` writes the literal `"unknown"` when the Metadata API could not be
    reached or exposed no LUID (#402). Treating it - or any out-of-vocabulary value, or an absent key
    from a pre-#402 capture - as either kind is exactly the guess the kind guard exists to refuse.
    """
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[])
    _write_report(tmp_path, ["Sales"])
    _write_oracle_manifest(tmp_path, ["Sales"], view_type=declared)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    assert oracle["visual_present"] == 0
    assert oracle["kindless_evidence"] == 1
    assert oracle["admitted_evidence"] == 0, "a kind-less record must not even enter the index"
    assert "establish no dashboard/worksheet kind" in oracle["grade"]


def test_reference_manifest_entries_are_dashboards_by_construction(tmp_path: Path) -> None:
    """`capture_tableau_reference.py` builds `dashboards[]` from the spec's dashboards.

    So the kind is structurally known for a reference capture and does not wait on #402 - but it is
    only ever `dashboard`, which means a worksheet page genuinely has no reference oracle rather than
    being certified by a dashboard picture.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.sales", "Sales")])
    _write_report(tmp_path, ["Sales"])
    _write_reference_manifest(tmp_path, ["Sales"])

    records, _grades = cu._reference_oracles(tmp_path, None)
    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert [record.kind for record in records] == ["dashboard"]
    assert oracle["visual_present"] == 0, "a dashboard capture is not evidence about a worksheet"


def test_two_producing_workbooks_slugging_alike_are_both_refused(tmp_path: Path) -> None:
    """Kills: uniqueness checked among UNIT workbooks only.

    Measured: records declaring `Bo ok` and `Bo-ok` were both admitted to unit workbook `Book`,
    because the guard asked "is this key unique among my artifacts?" and never "is it unique among
    the things claiming it?". Uniqueness of a lossy key on one side is not identity.
    """
    _write_full_spec(tmp_path, dashboards=[("Sales", []), ("Ops", [])], worksheets=[])
    _write_report(tmp_path, ["Sales", "Ops"])
    _write_oracle_manifest(tmp_path, ["Sales"], workbook="Bo ok", workbook_luid=None)
    manifest = tmp_path / "_oracle" / "oracle-manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    _png(tmp_path / "_oracle" / "images/Ops__1.png")
    payload["views"].append(
        {
            "view_name": "Ops",
            "view_type": "dashboard",
            "workbook": "Bo-ok",
            "image": {"status": "ok", "path": "images/Ops__1.png"},
            "data": {"status": "failed"},
        }
    )
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 0
    # ⚠️ Round-3 B-B: the two-sided uniqueness question these were written to pose is moot - the
    # lossy rescue they guarded is deleted, so a sanitised spelling can no longer admit anything at
    # all. They are refused for differing from this unit's stem, which is a stronger statement.
    assert oracle["foreign_workbook_evidence"] == ["name='Bo ok'", "name='Bo-ok'"]


def test_a_sanitised_spelling_no_longer_binds_because_a_name_is_not_identity(tmp_path: Path) -> None:
    """The lossy workbook fallback survives its second guard.

    It is justified here and only here: the unit side is a filesystem-sanitised artifact STEM, so
    something really may have re-spelled it. One producer, one artifact, one key.
    """
    _write_full_spec(tmp_path, dashboards=[("Sales", [])], worksheets=[])
    _write_report(tmp_path, ["Sales"], name="Bo ok")
    _write_oracle_manifest(tmp_path, ["Sales"], workbook="Bo-ok")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 1
    assert oracle["foreign_workbook_evidence"] == []


def test_the_normalized_index_is_what_bindable_workbooks_consults(tmp_path: Path) -> None:
    """The round-6 both-sides guard survives the move into `NormalizedIndex`."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"], name="Bo ok")
    _write_report(tmp_path, ["A"], name="Bo-ok")
    _write_viz_fidelity_handover(tmp_path, [_empty_row("B")], workbook_name="Book")

    explanations = cu.page_drop_explanations(tmp_path)

    assert explanations["available"] is False
    assert explanations["unbound_workbooks"] == ["Book"]


# --- known-gap disclosure (issue #450) ---------------------------------------------------------
# ⚠️ What is left of the #438 disclosure after its KIND half was fixed. The two tests that pinned
# the kind caveat are gone WITH that caveat - a disclosure outliving its gap manufactures doubt as
# falsely as a missing one manufactures confidence. These two remain because #450 remains: a record
# admitted on a lossy workbook key rests on a weaker join than an exact match. When #450 lands,
# delete `_oracle_caveats`, these tests and their two DISCLOSURE mutations together.


def test_a_name_only_refusal_carries_a_caveat_naming_it(tmp_path: Path) -> None:
    """An operator must be told WHICH producer was refused for carrying only a name, by name."""
    _write_spec(tmp_path, ["Sales"])
    _write_report(tmp_path, ["Sales"], name="Bo ok")
    _write_oracle_manifest(tmp_path, ["Sales"], workbook="Bo ok", workbook_luid=None)

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_NOT_CHECKED
    caveats = oracle["known_gap_caveats"]
    assert len(caveats) == 1
    assert "NAME-ONLY EVIDENCE REFUSED" in caveats[0]
    assert "'Bo ok'" in caveats[0], "the caveat must name the producer, not disclaim generically"


def test_an_exactly_attributed_workbook_prints_no_caveat(tmp_path: Path) -> None:
    """Kills a generic disclaimer: an exact workbook match is not affected, so nothing is said."""
    _write_spec(tmp_path, ["Sales"])
    _write_report(tmp_path, ["Sales"])
    _write_oracle_manifest(tmp_path, ["Sales"], workbook="Book")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 1
    assert oracle["known_gap_caveats"] == []


def test_a_run_that_certified_nothing_prints_no_caveat(tmp_path: Path) -> None:
    """With no page certified, nothing rests on the lossy join and nothing is claimed about it."""
    _write_spec(tmp_path, ["Sales"])
    _write_report(tmp_path, ["Sales"], name="Bo ok")

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["visual_present"] == 0
    assert oracle["known_gap_caveats"] == []


def test_the_caveats_reach_the_rendered_cli_output(tmp_path: Path) -> None:
    """A payload key nobody prints is not a disclosure."""
    _write_spec(tmp_path, ["Sales"])
    _write_report(tmp_path, ["Sales"], name="Bo ok")
    _write_oracle_manifest(tmp_path, ["Sales"], workbook="Bo ok", workbook_luid=None)

    rendered = cu.render(cu.run_all(tmp_path, scope=cu.SCOPE_REPORT))

    assert "NAME-ONLY EVIDENCE REFUSED" in rendered
    assert "'Bo ok'" in rendered


# ---------------------------------------------------------------------------
# #559 — safe printing on a CP1252/ASCII console
# ---------------------------------------------------------------------------


class TestSafePrintCP1252:
    """_safe_print must not raise UnicodeEncodeError on a restricted stream."""

    @staticmethod
    def _cp1252_stream():
        """Return an in-memory text stream that behaves like a CP1252 console."""
        import io

        buf = io.BytesIO()
        return io.TextIOWrapper(buf, encoding="cp1252", errors="strict")

    def test_cp1252_stream_no_traceback(self):
        """The warning glyph must not crash; text must arrive."""
        stream = self._cp1252_stream()
        text = "⚠️ NAME-ONLY EVIDENCE REFUSED: 1 record(s)"
        cu._safe_print(text, stream=stream)
        stream.flush()
        stream.seek(0)
        output = stream.read()
        assert "NAME-ONLY EVIDENCE REFUSED" in output

    def test_utf8_stream_preserves_glyph(self):
        """On a capable stream the original text is untouched."""
        import io

        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="utf-8")
        text = "⚠️ hello"
        cu._safe_print(text, stream=stream)
        stream.flush()
        stream.seek(0)
        assert stream.read().strip() == text

    def test_main_exit_code_survives_encoding_failure(self, tmp_path: Path, monkeypatch):
        """main() returns report['exit_code'] even when stdout cannot encode the output."""
        import io

        _write_spec(tmp_path, ["D1"])
        buf = io.BytesIO()
        fake_stdout = io.TextIOWrapper(buf, encoding="ascii", errors="strict")
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        rc = cu.main([str(tmp_path)])
        assert isinstance(rc, int)
        assert rc in {cu.EXIT_OK, cu.EXIT_FINDINGS, cu.EXIT_NOT_CHECKED, cu.EXIT_PRECONDITION_FAILED}

    def test_io_error_is_not_swallowed(self):
        """A real I/O failure must propagate — only UnicodeEncodeError is caught."""
        import io

        class BrokenStream(io.StringIO):
            encoding = "utf-8"

            def write(self, s):
                raise OSError("disk full")

        with pytest.raises(OSError, match="disk full"):
            cu._safe_print("hello", stream=BrokenStream())

    def test_no_duplicated_prefix_on_cp1252(self):
        """Encode-before-write means the report is emitted exactly once."""
        stream = self._cp1252_stream()
        text = "PREFIX ⚠️ SUFFIX"
        cu._safe_print(text, stream=stream)
        stream.flush()
        stream.seek(0)
        output = stream.read()
        assert output.count("PREFIX") == 1
        assert "SUFFIX" in output

    def test_os_error_on_first_write_propagates_immediately(self):
        """An OSError must propagate on the first write; no second write attempted.

        A mutation broadening the catch to Exception would retry and hit the
        second (accepting) write, so write_count > 1 kills the mutation.
        """
        import io

        class FirstWriteFails(io.StringIO):
            encoding = "utf-8"

            def __init__(self):
                super().__init__()
                self.write_count = 0

            def write(self, s):
                self.write_count += 1
                if self.write_count == 1:
                    raise OSError("transient")
                return super().write(s)

        stream = FirstWriteFails()
        with pytest.raises(OSError, match="transient"):
            cu._safe_print("hello", stream=stream)
        assert stream.write_count == 1


# ---------------------------------------------------------------------------------------------
# Package-boundary ordering in the EXIT gate (issue #562, follow-up to PR #590)
# ---------------------------------------------------------------------------------------------
#
# The invariant under test: `check_unit` classifies the ORIGINAL caller-supplied target exactly once,
# BEFORE `_unit_dir`, any `resolve`/`is_dir`/`is_file`/`rglob`, oracle/reference discovery, manifest
# read or ancestor walk. PR #590 shipped the classifier and the ENTRY gate; the exit gate still
# reached its package handling through `_oracle_dirs()` -> `_unit_dir()` -> `target.resolve()`, so a
# caller-supplied symlink or junction was classified on its DESTINATION and consumed that
# destination's evidence through the alias.


class _Followed(Exception):
    """Raised at the exact call site where the exit gate dereferenced or discovered too early.

    ⚠️ Deliberately not `AssertionError`, and deliberately caught inside the patched window: `Path.exists`
    is armed here and **pytest calls it while formatting a traceback**, so letting the failure escape
    with the patch installed turns a genuine kill into an INTERNALERROR that reads as infrastructure
    breakage rather than as this test failing.
    """


#: Everything the exit gate must not have reached before classification. `_unit_dir` is named
#: explicitly because it is the documented location of the defect: it resolves first.
_FORBIDDEN_PATH_PRIMITIVES = ("resolve", "is_file", "is_dir", "exists", "rglob", "stat", "open")
_FORBIDDEN_GATE_HELPERS = (
    "_unit_dir",
    "inspect_brownfield",
    "load_exemptions",
    "check_page_parity",
    "check_oracle_coverage",
)


def _run_all_without_following(target: Path, scope: str = cu.SCOPE_ALL) -> tuple[dict | None, str]:
    """Run the exit gate with every follower and discovery helper armed to explode.

    Uses its own `MonkeyPatch.context` rather than the test's `monkeypatch` fixture: undoing that
    one would also undo the autouse `no_native_gates` patches and silently re-enable the real
    subprocess gates for the rest of the test.
    """

    def boom(*_args: object, **_kwargs: object) -> object:
        raise _Followed("the exit gate followed or discovered before classifying the supplied target")

    with pytest.MonkeyPatch.context() as mp:
        for name in _FORBIDDEN_PATH_PRIMITIVES:
            mp.setattr(Path, name, boom, raising=True)
        mp.setattr("builtins.open", boom, raising=True)
        for name in _FORBIDDEN_GATE_HELPERS:
            mp.setattr(cu, name, boom, raising=True)
        try:
            return cu.run_all(target, scope=scope), ""
        except _Followed as exc:
            return None, str(exc)


def _package(root: Path, *, marker: bool = True) -> Path:
    """A migration unit with local oracle evidence, optionally declaring its package boundary."""
    _write_spec(root, ["Revenue"])
    _write_report(root, ["Revenue"])
    _write_oracle_manifest(root, ["Revenue"], workbook="Book")
    if marker:
        (root / bundle_corpus.PACKAGE_MARKER).write_text("{}\n", encoding="utf-8")
    return root


def _link_directory(link: Path, target: Path) -> None:
    """A junction (Windows) or a directory symlink (POSIX) - a reparse point either way."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if completed.returncode != 0:
            pytest.skip(f"could not create junction: {completed.stderr.decode(errors='replace').strip()}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege-dependent
        pytest.skip("this platform/account cannot create symlinks without elevation")


def _symlink_directory(link: Path, target: Path) -> None:
    """A real directory SYMLINK on every platform; skips where it needs elevation (Windows)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform/account cannot create symlinks without elevation")


def _boundary_row(report: dict) -> dict:
    rows = [check for check in report["checks"] if check["id"] == cu.PACKAGE_BOUNDARY_CHECK_ID]
    assert len(rows) == 1, f"expected exactly one boundary row, got {[c['id'] for c in report['checks']]}"
    return rows[0]


def test_the_destination_evidence_is_readable_when_the_real_path_is_supplied(tmp_path: Path) -> None:
    """The POSITIVE control that makes every refusal below a refusal rather than a vacuous pass.

    Handed its real path, this package's own oracle capture certifies its one page. So the refusals
    that follow are withholding evidence that demonstrably exists and would otherwise be consumed.
    """
    package = _package(tmp_path / "run" / "packages" / "Unit")

    report = cu.run_all(package)

    oracle = next(check for check in report["checks"] if check["id"] == "oracle-coverage")
    assert oracle["visual_present"] == 1
    assert not [check for check in report["checks"] if check["id"] == cu.PACKAGE_BOUNDARY_CHECK_ID]


@pytest.mark.parametrize("make_link", [_link_directory, _symlink_directory], ids=["junction", "symlink"])
def test_an_aliased_root_is_refused_before_unit_dir_and_reads_no_destination_evidence(
    tmp_path: Path, make_link
) -> None:
    """Kills: classifying after `_unit_dir()`/`resolve()` - the exact residual PR #590 left open.

    The alias is not lexically package-shaped, so following it is the ONLY way to reach the package
    verdict; the previous ordering did exactly that and then read the destination's oracle manifest.
    Two independent assertions: the armed run proves nothing was followed or discovered at all, and
    the ordinary run proves the destination's evidence produced no row.
    """
    package = _package(tmp_path / "run" / "packages" / "Unit")
    alias = tmp_path / "alias"
    make_link(alias, package)

    armed, followed = _run_all_without_following(alias)
    report = cu.run_all(alias)

    assert followed == "", followed
    assert armed is not None and armed["exit_code"] == cu.EXIT_NOT_CHECKED
    assert report["status"] == cu.STATUS_NOT_CHECKED
    assert report["exit_code"] == cu.EXIT_NOT_CHECKED
    assert report["stopped_after"] == cu.PACKAGE_BOUNDARY_CHECK_ID
    assert _boundary_row(report)["code"] == bundle_corpus.CODE_TARGET_ROOT_REPARSE
    assert [check["id"] for check in report["checks"]] == [cu.PACKAGE_BOUNDARY_CHECK_ID]
    assert isinstance(report["brownfield"], dict) and not report["brownfield"]


@pytest.mark.parametrize(
    ("relative", "marker", "code", "placement"),
    [
        (("run", "packages", "Unit"), None, "CODE_PACKAGE_MARKER_MISSING", "PLACEMENT_FLAT"),
        (("run", "packages", "batch", "Unit"), None, "CODE_PACKAGE_MARKER_MISSING", "PLACEMENT_NESTED"),
        (("run", "packages", "Unit"), "dir", "CODE_PACKAGE_MARKER_NOT_REGULAR", "PLACEMENT_FLAT"),
    ],
    ids=["flat-missing", "nested-missing", "flat-non-regular"],
)
def test_a_package_shaped_root_without_a_regular_marker_is_refused_before_discovery(
    tmp_path: Path, relative: tuple[str, ...], marker: str | None, code: str, placement: str
) -> None:
    """Kills: falling back to ordinary bundle handling for an unproven boundary.

    Both placements and both damaged-marker shapes ride one parametrization: a DIRECTORY named
    `package-manifest.json` declares exactly as little as no marker at all.
    """
    unit = _package(tmp_path.joinpath(*relative), marker=False)
    if marker == "dir":
        (unit / bundle_corpus.PACKAGE_MARKER).mkdir()

    armed, followed = _run_all_without_following(unit)
    report = cu.run_all(unit)

    assert followed == "", followed
    assert armed is not None
    assert report["exit_code"] == cu.EXIT_NOT_CHECKED
    row = _boundary_row(report)
    assert row["code"] == getattr(bundle_corpus, code)
    assert row["placement"] == getattr(bundle_corpus, placement)
    assert [check["id"] for check in report["checks"]] == [cu.PACKAGE_BOUNDARY_CHECK_ID]


def test_an_unassessable_root_refuses_rather_than_succeeding_exception_shaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root whose `lstat` is denied is UNKNOWN, and unknown is never clean - and never a traceback."""
    unit = _package(tmp_path / "run" / "packages" / "Unit")
    real_lstat = os.lstat

    def denying(path, *args, **kwargs):
        if Path(path) == unit:
            raise PermissionError(13, "permission denied by the test")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(bundle_corpus.os, "lstat", denying)

    report = cu.run_all(unit)
    rendered = cu.render(report)

    assert report["exit_code"] == cu.EXIT_NOT_CHECKED
    assert _boundary_row(report)["code"] == bundle_corpus.CODE_TARGET_ROOT_UNASSESSABLE
    assert "PermissionError" not in rendered
    assert "permission denied by the test" not in rendered


def test_run_all_classifies_its_own_argument_and_accepts_no_injected_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills: a caller-supplied `classification`, and classifying the RESOLVED target.

    Two halves, both named. **At the API level** there is no parameter through which one path's
    verdict could clear another - round-1 review of PR #593 rejected the earlier `classification=`
    kwarg for exactly that reason, so a signature assertion is the only construction that makes the
    mismatch impossible rather than merely unused. **At the call level** the target is spelled
    `<...>/Unit/fabric/..`, which `resolve()` collapses to `<...>/Unit`: a recorded argument equal to
    the supplied spelling can only have been taken before resolution, and a single entry can only
    mean the exit gate never asked a second time.

    ⚠️ Scope stated: this counts `check_unit`'s OWN calls. The direct helpers `run_all` then invokes
    each re-classify the ALREADY-CLEARED root by design (issue #562) - that is asserted positively
    below rather than allowed silently, because "somebody asked" is only safe when every ask is bound
    to the asker's own argument. `bundle_corpus.evidence_dirs` classifies again for the same reason.
    """
    assert "classification" not in inspect.signature(cu.run_all).parameters

    package = _package(tmp_path / "run" / "packages" / "Unit")
    spelling = package / "fabric" / ".."
    seen: list[Path] = []
    real = cu.classify_target

    def recording(target: Path):
        seen.append(target)
        return real(target)

    monkeypatch.setattr(cu, "classify_target", recording)

    report = cu.run_all(spelling)

    assert seen[0] == spelling, "the gate's FIRST question is about the supplied spelling, not the resolved path"
    assert set(seen[1:]) == {package.resolve()}, "every later ask is a helper re-asking about the cleared root"
    assert report["target"] == str(package.resolve())
    assert not [check for check in report["checks"] if check["id"] == cu.PACKAGE_BOUNDARY_CHECK_ID]


def test_the_cli_preclassifies_only_for_its_own_is_dir_check_and_run_all_reclassifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's own verdict guards its following pre-check and is NOT handed on as authority.

    Two classifications of the SAME original path is the intended shape: the second is bound to
    `run_all`'s own argument by construction, which an injected object never could be.
    """
    package = _package(tmp_path / "run" / "packages" / "Unit")
    seen: list[Path] = []
    real = cu.classify_target

    def recording(target: Path):
        seen.append(target)
        return real(target)

    monkeypatch.setattr(cu, "classify_target", recording)

    assert cu.main([str(package), "--quiet"]) != cu.EXIT_USAGE, "the safe pre-check must not fire here"
    assert seen[:2] == [package, package], "the CLI asks, then run_all asks again about the SAME original path"
    assert set(seen[2:]) <= {package.resolve()}, "later asks are direct helpers re-asking about the cleared root"


def test_resolution_is_gated_on_the_clearance_rather_than_merely_ordered_after_it(tmp_path: Path) -> None:
    """Kills: deleting the refusal and letting an unsafe classification fall through to `resolve()`."""
    package = _package(tmp_path / "run" / "packages" / "Unit")
    damaged = _package(tmp_path / "run" / "packages" / "Other", marker=False)

    safe = bundle_corpus.classify_target(package)
    unsafe = bundle_corpus.classify_target(damaged)

    assert cu._cleared_target(package, safe) == package.resolve()  # pylint: disable=protected-access
    with pytest.raises(ValueError, match=bundle_corpus.CODE_PACKAGE_MARKER_MISSING):
        cu._cleared_target(damaged, unsafe)  # pylint: disable=protected-access


_DIRECT_ENTRIES = ("load_exemptions", "page_expectation", "check_page_parity", "check_oracle_coverage")

#: Everything a DIRECT helper must not have reached before classifying its own argument. Wider than
#: `_FORBIDDEN_PATH_PRIMITIVES`: these helpers read files, so the readers are armed too.
_FORBIDDEN_DIRECT_PRIMITIVES = (
    "resolve",
    "is_file",
    "is_dir",
    "exists",
    "rglob",
    "glob",
    "iterdir",
    "read_text",
    "read_bytes",
    "open",
)


def _without_following(call, delegates: tuple[str, ...] = ()) -> tuple[object, str]:
    """Run one direct helper call with every follower, reader and the oracle loader armed.

    ``delegates`` additionally arms the helpers this entry would otherwise LEAN ON for its refusal.
    Without that, a guard deleted from `check_page_parity` survives every observable assertion,
    because `page_expectation`'s guard refuses one frame deeper and the shape comes out identical -
    the "moved boundary" shape. Arming them is what makes each guard independently necessary.

    Same `MonkeyPatch.context` reasoning as `_run_all_without_following`, and the same reason for a
    non-`AssertionError` signal: `Path.exists` is armed and pytest calls it while formatting.
    """

    def boom(*_args: object, **_kwargs: object) -> object:
        raise _Followed("a direct helper followed, read, discovered or delegated before classifying")

    with pytest.MonkeyPatch.context() as mp:
        for name in _FORBIDDEN_DIRECT_PRIMITIVES:
            mp.setattr(Path, name, boom, raising=True)
        mp.setattr("builtins.open", boom, raising=True)
        mp.setattr(cu.tableau_oracle_manifest, "read_manifest", boom, raising=True)
        mp.setattr(cu, "_unit_dir", boom, raising=True)
        mp.setattr(cu, "shipping_reports", boom, raising=True)
        for name in delegates:
            mp.setattr(cu, name, boom, raising=True)
        try:
            return call(), ""
        except _Followed as exc:
            return None, str(exc)


def _unsafe_root(tmp_path: Path, shape: str) -> tuple[Path, Path]:
    """One unsafe root plus the REAL directory holding the oracle evidence it must not consume.

    `alias` is a reparse point onto an intact package - not lexically package-shaped, so following it
    is the only way to reach a verdict. `damaged` is lexically package-shaped with no marker. Built
    lazily per shape: `_link_directory` skips where the account cannot create a link, and the damaged
    case must not skip for a link it never needed.
    """
    if shape == "alias":
        package = _package(tmp_path / "run" / "packages" / "Unit")
        alias = tmp_path / "alias"
        _link_directory(alias, package)
        return alias, package / "_oracle"
    damaged = _package(tmp_path / "run" / "packages" / "Other", marker=False)
    return damaged, damaged / "_oracle"


def _refused_exemptions(result: dict) -> None:
    """`load_exemptions`: the existing non-clean schema, and an UNREAD sidecar is not an empty one."""
    assert set(result) == {"path", "entries", "invalid"}
    assert result["path"] is None and result["entries"] == []
    assert [row["item"] for row in result["invalid"]] == [cu.REFUSED_TARGET_LABEL]


def _refused_expectation(result: dict) -> None:
    """`page_expectation`: the existing unassessable shape, with nothing read into it."""
    assert result["assessable"] is False and result["reason"]
    assert result["actual"] == [] and result["rendered"] == []
    assert result["candidates"] is None and result["omissions"] == [] and result["contested_names"] == []


def _refused_parity(result: dict) -> None:
    """`check_page_parity`: the existing blocking NOT_CHECKED page-parity row."""
    assert result["id"] == "page-parity" and result["status"] == cu.STATUS_NOT_CHECKED
    assert result["expected_pages"] is None and result["actual_pages"] == []
    assert result["applied_exemptions"] == [] and result["unapplied_exemptions"] == []


def _refused_oracle(result: dict) -> None:
    """`check_oracle_coverage`: the existing not-assessable coverage row, certifying nothing."""
    assert result["id"] == "oracle-coverage" and result["status"] == cu.STATUS_NOT_CHECKED
    assert result["pages"] == 0 and result["visual_present"] == 0 and result["numeric_present"] == 0
    assert result["rows"] == []


#: Each entry observed on its OWN, so deleting one guard fails exactly one test rather than a
#: four-in-one assertion that cannot say which helper regressed. The third element names the
#: delegates that entry must NOT lean on for its refusal.
_DIRECT_REFUSALS = {
    "load_exemptions": (lambda root: cu.load_exemptions(root), _refused_exemptions, ()),
    "page_expectation": (
        lambda root: cu.page_expectation(root),
        _refused_expectation,
        ("actual_pages", "_spec_pages", "page_drop_explanations"),
    ),
    "check_page_parity": (
        lambda root: cu.check_page_parity(root, {"entries": []}),
        _refused_parity,
        ("page_expectation",),
    ),
    "check_oracle_coverage": (
        lambda root: cu.check_oracle_coverage(root, None, None),
        _refused_oracle,
        ("page_expectation", "_reference_oracles", "_oracle_capture_oracles"),
    ),
}


@pytest.mark.parametrize("shape", ["alias", "damaged"])
@pytest.mark.parametrize("helper", sorted(_DIRECT_REFUSALS))
def test_a_direct_helper_refuses_an_unsafe_root_before_following_or_reading_anything(
    tmp_path: Path, helper: str, shape: str
) -> None:
    """Kills: classifying after `_unit_dir()`/`shipping_reports`/`resolve()` on the DIRECT surface.

    The inverse of the residual PR #593 left open: `check_oracle_coverage(alias, ...)` used to return
    a full visual PASS on a package the caller never named, and a package-shaped root with no marker
    measured `assessable=True`, parity `PASS`, `visual_present=1`. Every follower, every reader, the
    oracle manifest loader AND this entry's own delegates are armed, so reaching *any* of them is the
    failure - not merely producing a wrong verdict, and not a refusal borrowed one frame deeper.
    """
    root, _evidence = _unsafe_root(tmp_path, shape)
    call, expect_refused, delegates = _DIRECT_REFUSALS[helper]

    result, followed = _without_following(lambda: call(root), delegates)

    assert followed == "", followed
    expect_refused(result)


@pytest.mark.parametrize("shape", ["alias", "damaged"])
def test_an_explicit_oracle_directory_does_not_bypass_the_capture_guard(tmp_path: Path, shape: str) -> None:
    """Kills: guarding only `_unit_dir`/`_oracle_dirs`.

    An explicit oracle directory skips both, so a `_unit_dir`-based guard would never fire - measured,
    a direct call read one record with `_unit_dir` armed to fail and never called. The manifest loader
    is armed here: the typed refusal must be raised before it, and it must carry no classification
    object, no target and no destination.
    """
    root, evidence = _unsafe_root(tmp_path, shape)
    assert (evidence / "oracle-manifest.json").is_file(), "the evidence being withheld must exist"

    with pytest.raises(cu._DirectTargetRefused) as raised:  # pylint: disable=protected-access
        _without_following(  # pylint: disable=protected-access
            lambda: cu._oracle_capture_oracles(root, evidence), ("_oracle_dirs",)
        )

    refusal = raised.value
    assert refusal.code and refusal.placement
    assert set(vars(refusal)) == {"code", "detail", "placement"}, "no classification, target or destination"


def test_a_refusal_leaks_no_supplied_component_through_any_channel(tmp_path: Path) -> None:
    """Both the alias and the destination are secret-bearing; neither may appear anywhere.

    Dicts, their JSON rendering, and the exception's `str`/`repr` are all checked, because the
    refusal is the object a caller pastes into an issue.
    """
    package = _package(tmp_path / "run" / "packages" / "Contoso-Secret")
    alias = tmp_path / "Fabrikam-Confidential"
    _link_directory(alias, package)

    returned = json.dumps(
        [
            cu.load_exemptions(alias),
            cu.page_expectation(alias),
            cu.check_page_parity(alias, {"entries": []}),
            cu.check_oracle_coverage(alias, None, None),
        ],
        default=str,
    )
    with pytest.raises(cu._DirectTargetRefused) as raised:  # pylint: disable=protected-access
        cu._oracle_capture_oracles(alias, None)  # pylint: disable=protected-access
    everywhere = returned + str(raised.value) + repr(raised.value)

    assert bundle_corpus.CODE_TARGET_ROOT_REPARSE in everywhere, "the stable classifier code must survive"
    for supplied in (str(alias), str(package), str(tmp_path), alias.name, package.name):
        assert supplied not in everywhere, supplied


def test_no_direct_helper_accepts_a_caller_supplied_boundary_verdict(tmp_path: Path) -> None:
    """Kills: adding a `classification`/`_CheckedTarget`/clearance parameter to any of the five.

    A parameter that decides whether a boundary is safe is exactly the parameter a caller must not be
    able to supply - one path's answer would clear a different path (PR #593 round-1 review). The
    factory is pinned to one positional `target` for the same reason.
    """
    forbidden = {"classification", "clearance", "checked", "context", "target_classification"}
    entries = [getattr(cu, name) for name in _DIRECT_ENTRIES] + [cu._oracle_capture_oracles]  # pylint: disable=protected-access
    for entry in entries:
        assert not forbidden & set(inspect.signature(entry).parameters), entry.__name__
    assert list(inspect.signature(cu._checked_direct_target).parameters) == ["target"]  # pylint: disable=protected-access

    package = _package(tmp_path / "run" / "packages" / "Unit")
    assert cu._checked_direct_target(package) == package.resolve()  # pylint: disable=protected-access


def test_the_five_direct_helpers_are_unchanged_for_a_safe_package_and_an_ordinary_unit(tmp_path: Path) -> None:
    """The positive control that makes every refusal above a withholding rather than a vacuum.

    Handed their real paths, both an intact package and an ordinary unit still read their own
    evidence through all five entries: the exemption sidecar's path, an assessable expectation, a
    parity PASS, oracle coverage of the one page, and the one capture record.
    """
    for root in (_package(tmp_path / "plain" / "unit", marker=False), _package(tmp_path / "run" / "packages" / "Unit")):
        exemptions = cu.load_exemptions(root)
        expectation = cu.page_expectation(root)
        parity = cu.check_page_parity(root, exemptions)
        oracle = cu.check_oracle_coverage(root, None, None)
        records, grades = cu._oracle_capture_oracles(root, None)  # pylint: disable=protected-access

        assert exemptions == {"path": str(root.resolve() / cu.EXEMPTIONS_FILE), "entries": [], "invalid": []}
        assert expectation["assessable"] is True and [page["name"] for page in expectation["candidates"]] == ["Revenue"]
        assert parity["status"] == cu.STATUS_PASS
        assert oracle["status"] == cu.STATUS_PASS and oracle["visual_present"] == 1 and oracle["pages"] == 1
        assert [record.name for record in records] == ["Revenue"] and grades


def test_a_safe_package_and_an_ordinary_unit_keep_their_existing_verdicts(tmp_path: Path) -> None:
    """The regression control: nothing about unaliased, undamaged targets changed.

    Two unrelated consumers ride along here on purpose - the `desktop-orphans` row still runs, and a
    safe package still evaluates only its OWN evidence, which is the pre-existing package behaviour
    the boundary ordering must not disturb.
    """
    ordinary = _package(tmp_path / "plain" / "unit", marker=False)
    package = _package(tmp_path / "run" / "packages" / "Unit")

    ordinary_report = cu.run_all(ordinary)
    package_report = cu.run_all(package)

    assert [check["id"] for check in ordinary_report["checks"]] == [check["id"] for check in package_report["checks"]]
    assert "desktop-orphans" in [check["id"] for check in package_report["checks"]]
    for report, root in ((ordinary_report, ordinary), (package_report, package)):
        assert report["target"] == str(root.resolve())
        assert report["stopped_after"] is None
        assert next(c for c in report["checks"] if c["id"] == "oracle-coverage")["visual_present"] == 1


def test_the_cli_refuses_an_aliased_target_without_printing_any_supplied_component(tmp_path: Path) -> None:
    """Exit state and every output channel: no supplied path, **no supplied path COMPONENT**, no traceback.

    Round-1 review of PR #593: printing the target's final component as a "label" is a disclosure,
    not a label - a unit folder is routinely the customer's name. Both the alias and the package it
    points at are named distinctively here so a leak of *either* component fails a named assertion,
    and the constant label is asserted positively so deleting the whole `target` key cannot pass.
    """
    package = _package(tmp_path / "run" / "packages" / "Contoso-Secret")
    alias = tmp_path / "Fabrikam-Confidential"
    _link_directory(alias, package)
    json_path = tmp_path / "verdict.json"

    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_unit.py"), str(alias), "--json", str(json_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    written = json_path.read_text(encoding="utf-8")
    everywhere = completed.stdout + completed.stderr + written

    assert completed.returncode == cu.EXIT_NOT_CHECKED, completed.stdout + completed.stderr
    assert bundle_corpus.CODE_TARGET_ROOT_REPARSE in everywhere
    assert cu.REFUSED_TARGET_LABEL in everywhere
    assert json.loads(written)["target"] == cu.REFUSED_TARGET_LABEL
    for supplied in (str(alias), str(package), str(tmp_path), alias.name, package.name):
        assert supplied not in everywhere, supplied
    assert "Traceback" not in everywhere
    assert "ERROR: not a directory" not in everywhere


# R2 composes the real authorities here. Existing clean/skeleton fixtures are intentionally not
# promoted to completion evidence, and the C fixture keeps its independently commissioned "required".
R2_DISCLAIMER = (
    "Phase-2 COMPLETE at this check for the package snapshot pinned by the supplied final-receipt SHA-256, "
    "under the documented Phase-2 evidence contract. This checker does not authenticate the token's producer "
    "or establish that this is the latest snapshot ever produced."
)
R2_WAIVER = "Exact Tableau-versus-Power-BI numeric comparison was not performed because the commissioned brief explicitly waived it."
R2_MODEL = "fabric/Book.SemanticModel"
R2_REPORT = "fabric/Book.Report"
R2_TABLE = f"{R2_MODEL}/definition/tables/Sales.tmdl"
R2_SQL = (
    "\tpartition Sales = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\tlet\n"
    '\t\t\t    Source = Sql.Database("source.example", "db"),\n'
    '\t\t\t    Sales = Source{[Schema="dbo", Item="Sales"]}[Data]\n'
    "\t\t\tin\n"
    "\t\t\t    Sales\n"
)


def _r2_put(package: Path, name: str, content: object) -> None:
    path = package / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        content if isinstance(content, bytes) else (json.dumps(content, ensure_ascii=True) + "\n").encode("utf-8")
    )


def _r2_declare(package: Path, *names: str) -> None:
    """An explicit fixture commissioning/co-edit, never a production token or manifest repair."""
    manifest = json.loads((package / "package-manifest.json").read_bytes())
    for name in names:
        manifest["contents"]["files"][name] = _sha256(package / name)
    _r2_put(package, "package-manifest.json", manifest)


def _r2_package(root: Path, *, numeric: str = "none") -> Path:
    from png_fixtures import valid_png

    package = root / "packages" / "Book"
    asset = f"assets/{UNIT_LUID}_Book.twb"
    _r2_put(package, asset, b"<workbook name='Book'/>\n")
    _r2_put(
        package,
        "migration-spec.json",
        {
            "source": {"file_name": asset.split("/")[-1]},
            "data_sources": [
                {
                    "id": "Sales",
                    "name": "Sales",
                    "connection": {
                        "class": "sqlserver",
                        "server": "source.example",
                        "database": "db",
                        "powerbi_target": "live_source",
                    },
                    "tables": [{"name": "Sales"}],
                    "fields": [],
                }
            ],
            "dashboards": [{"id": "dash", "name": "Executive"}],
            "worksheets": [],
            "limitations_encountered": [],
        },
    )
    _r2_put(package, "migration-spec.schema.json", {"type": "object", "required": ["source", "data_sources"]})
    _r2_put(
        package,
        "data-access.json",
        {
            "schema": "phase1-data-access/v1",
            "state": "live_data_ok",
            "source_keys": ["source-key:ab1baa4b3f77bb70"],
            "provider_unit": None,
            "provider_state": None,
            "validation": "validated",
            "effective_scope": "model_and_report",
            "max_phase2_claim": "data_validated",
            "codes": ["probe-cleared", "probe-data-ok"],
        },
    )
    _r2_put(
        package,
        "source-provenance.json",
        {
            "scope": {"unit": "Book"},
            "inputs": [
                {
                    "input": {"file": asset.split("/")[-1], "sha256": _sha256(package / asset)},
                    "origin": {"workbook_luid": UNIT_LUID, "matched_by": "luid", "revision_match": "same"},
                }
            ],
        },
    )
    _r2_put(
        package,
        "report.json",
        {
            "scope": {"unit": "Book"},
            "workbooks": [{"name": "Book", "model_translation_handoff": {"requests": []}}],
            "datasources": [],
        },
    )
    _r2_put(package, "engine-output-receipt.json", {"engine": {"version": "2.368.0", "canonical": True}})
    _r2_put(
        package,
        "migration-brief.md",
        (
            '+++\nschema = "phase1-start-ready/v2"\nunit = "Book"\nscope = "model_and_report"\n'
            f'fallback_authorization = "stop"\nnumeric_obligation = "{numeric}"\n+++\n'
        ).encode(),
    )
    _r2_put(package, "fabric/Book.pbip", {"version": "1.0", "artifacts": [{"report": {"path": "Book.Report"}}]})
    _r2_put(
        package,
        f"{R2_REPORT}/definition.pbir",
        {"version": "4.0", "datasetReference": {"byPath": {"path": "../Book.SemanticModel"}}},
    )
    _r2_put(package, f"{R2_REPORT}/definition/pages/pages.json", {"pageOrder": ["p1"]})
    _r2_put(
        package,
        f"{R2_REPORT}/definition/pages/p1/page.json",
        {"name": "p1", "displayName": "Executive", "width": 1280, "height": 720, "displayOption": "FitToPage"},
    )
    _r2_put(
        package,
        f"{R2_REPORT}/definition/pages/p1/visuals/v1/visual.json",
        {
            "name": "v1",
            "position": {"x": 0, "y": 0, "z": 0, "width": 600, "height": 300, "tabOrder": 0},
            "visual": {
                "visualType": "card",
                "query": {
                    "queryState": {
                        "Values": {
                            "projections": [
                                {
                                    "field": {
                                        "Measure": {
                                            "Expression": {"SourceRef": {"Entity": "Sales"}},
                                            "Property": "Total",
                                        }
                                    },
                                    "queryRef": "Sales.Total",
                                    "nativeQueryRef": "Total",
                                }
                            ]
                        }
                    }
                },
            },
        },
    )
    _r2_put(package, f"{R2_MODEL}/definition/database.tmdl", b"database\n\tcompatibilityLevel: 1604\n")
    _r2_put(
        package,
        f"{R2_MODEL}/definition/model.tmdl",
        b"model Model\n\tculture: en-US\n\tref table Sales\n\tref cultureInfo en-US\n",
    )
    _r2_put(
        package,
        R2_TABLE,
        (
            "/// One row per sale.\ntable Sales\n"
            "\t/// One of: Open, Closed.\n\tcolumn Status\n\t\tdataType: string\n\t\tsourceColumn: Status\n\t\tsummarizeBy: none\n"
            "\t/// Sale amount in USD.\n\tcolumn Amount\n\t\tdataType: double\n\t\tsourceColumn: Amount\n"
            "\t/// Sales amount total in USD.\n\tmeasure Total = SUM(Sales[Amount])\n\t\tformatString: 0.00\n" + R2_SQL
        ).encode(),
    )
    instructions = "Sales totals use [Total]. Default to all rows of 'Sales'. Never invent a time window."
    _r2_put(
        package,
        f"{R2_MODEL}/definition/cultures/en-US.tmdl",
        (
            "cultureInfo en-US\n\tlinguisticMetadata = "
            + json.dumps({"Version": "2.0.0", "Language": "en-US", "CustomInstructions": instructions})
            + "\n\t\tcontentType: json\n"
        ).encode(),
    )
    _r2_put(package, f"{R2_MODEL}/definition.pbism", {"version": "4.2", "settings": {"qnaEnabled": True}})
    blob = valid_png(320, 240)
    _r2_put(package, "reference/tableau-Executive.png", blob)
    _r2_put(
        package,
        "reference/manifest.json",
        {
            "source_workbook_sha256": _sha256(package / asset),
            "dashboards": [
                {
                    "name": "Executive",
                    "view_type": "dashboard",
                    "states": [
                        {
                            "provider": "manual",
                            "image": "tableau-Executive.png",
                            "sha256": hashlib.sha256(blob).hexdigest(),
                            "bytes": len(blob),
                            "dimensions": {"w": 320, "h": 240},
                            "capabilities": ["layout_grade", "text_readable", "validation_grade"],
                        }
                    ],
                }
            ],
        },
    )
    _r2_put(
        package,
        "package-manifest.json",
        {
            "unit": "Book",
            "kind": "workbook",
            "artifacts": {
                "asset": asset,
                "migration_spec": "migration-spec.json",
                "migration_spec_schema": "migration-spec.schema.json",
                "data_access": "data-access.json",
                "migration_brief": "migration-brief.md",
                "report": R2_REPORT,
                "model": R2_MODEL,
            },
            "model_binding": {"kind": "byPath", "path": "../Book.SemanticModel", "resolves_in_package": True},
            "contents": {
                "files": {
                    path.relative_to(package).as_posix(): _sha256(path) for path in package.rglob("*") if path.is_file()
                }
            },
        },
    )
    return package


def _r2_expand_report(package: Path) -> None:
    """A literal two-page/three-visual denominator, so first-only/any-pass folds are killable."""
    from png_fixtures import valid_png

    p1 = f"{R2_REPORT}/definition/pages/p1"
    p2 = f"{R2_REPORT}/definition/pages/p2"
    visual = json.loads((package / p1 / "visuals/v1/visual.json").read_bytes())
    visual["name"], visual["position"]["x"] = "v2", 650
    _r2_put(package, f"{p1}/visuals/v2/visual.json", visual)
    visual["name"], visual["position"]["x"] = "v3", 0
    _r2_put(package, f"{p2}/visuals/v3/visual.json", visual)
    _r2_put(
        package,
        f"{p2}/page.json",
        {
            "name": "p2",
            "displayName": "Details",
            "width": 1280,
            "height": 720,
            "displayOption": "FitToPage",
        },
    )
    _r2_put(package, f"{R2_REPORT}/definition/pages/pages.json", {"pageOrder": ["p1", "p2"]})
    spec = json.loads((package / "migration-spec.json").read_bytes())
    spec["dashboards"].append({"id": "details", "name": "Details"})
    _r2_put(package, "migration-spec.json", spec)
    blob = valid_png(321, 240)
    _r2_put(package, "reference/tableau-Details.png", blob)
    reference = json.loads((package / "reference/manifest.json").read_bytes())
    reference["dashboards"].append(
        {
            "name": "Details",
            "view_type": "dashboard",
            "states": [
                {
                    "provider": "manual",
                    "image": "tableau-Details.png",
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "bytes": len(blob),
                    "dimensions": {"w": 321, "h": 240},
                    "capabilities": ["layout_grade", "text_readable", "validation_grade"],
                }
            ],
        }
    )
    _r2_put(package, "reference/manifest.json", reference)
    _r2_declare(
        package,
        "migration-spec.json",
        f"{R2_REPORT}/definition/pages/pages.json",
        f"{p1}/visuals/v2/visual.json",
        f"{p2}/visuals/v3/visual.json",
        f"{p2}/page.json",
        "reference/manifest.json",
        "reference/tableau-Details.png",
    )


def _r2_seal(
    package: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    requests: dict | None = None,
    rows: int = 1,
    review_status: str = "pass",
    findings: list | None = None,
    previous: str | None = None,
    unavailable: str | None = None,
    page_ids: frozenset[str] | None = None,
) -> tuple[str, dict, list]:
    """Exercise real capture/finalization with separate invocation-owned A1 software observations."""
    import capture_powerbi_pages as capture
    import iteration_receipt as receipt
    from probe_desktop_query import DesktopIdentity
    from png_fixtures import valid_png
    from refresh_pbip_model import ImageObservation

    events = []
    bound = capture.BoundDesktop(
        DesktopIdentity(1234, "100", 1235, "101", 55001), "11111111-2222-3333-4444-555555555555"
    )

    def status(_pid: int) -> dict:
        return {
            "status": "ready",
            "instances": [
                {
                    "pid": 1234,
                    "bridgeStatus": "connected",
                    "currentFilePath": str(package / "fabric" / "Book.pbip"),
                    "hasUnsavedChanges": False,
                }
            ],
        }

    def refresh(port: int, tables: object, **kwargs: object) -> object:
        assert (port, tables, kwargs) == (
            55001,
            None,
            {"refresh_type": "full", "desktop_pid": 1234, "bound": bound, "return_observation": True},
        )
        events.append("full-database-refresh")
        if unavailable == "refresh":
            raise capture.ObservationUnavailable("TOOL_UNAVAILABLE")
        return capture.RefreshObservation(bound.catalogue, "full", "database", (), bound.identity)

    def canaries(held: object, names: list[str]) -> object:
        assert held is bound and names == ["Sales"]
        events.append("named-canary")
        if unavailable == "canaries":
            return None
        return (capture.CanaryObservation(bound.catalogue, "Sales", "EVALUATE TOPN(1, 'Sales')", rows, bound.identity),)

    def persist(port: int, cache: Path, model: Path, **kwargs: object) -> object:
        assert port == 55001 and kwargs == {"bound": bound, "return_observation": True}
        assert model == package / R2_MODEL and cache == package / R2_MODEL / ".pbi" / "cache.abf"
        events.append("image-readback")
        if unavailable == "persistence":
            raise capture.ObservationUnavailable("TOOL_UNAVAILABLE")
        blob = b"R2 synthetic A1 cache bytes; software observations, not native ABF qualification."
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(blob)
        digest = hashlib.sha256(blob).hexdigest()
        return capture.PersistenceObservation(
            bound.catalogue, 1604, ImageObservation(digest, len(blob), digest, len(blob)), bound.identity
        )

    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def shot(page: str, pid: str, dest: Path) -> bool:
        assert page in ("p1", "p2") and pid == "1234"
        events.append("frame")
        dest.write_bytes(valid_png(320, 240 if page == "p1" else 241))
        return True

    def recheck(held: object, operation) -> None:
        assert held is bound and operation(object()) is None

    with monkeypatch.context() as native:
        native.setattr(capture, "bind_desktop", lambda pid: bound if pid == 1234 else pytest.fail("wrong PID"))
        native.setattr(capture, "bound_call", recheck)
        native.setattr(capture, "refresh", refresh)
        native.setattr(capture, "probe_observations", canaries)
        native.setattr(capture, "image_save", persist)
        pending = capture.run_iteration(
            capture.IterationRequest(
                package,
                "1234",
                previous_sha256=previous,
                **({"refresh": True, "persist": True, "canaries": ("Sales",)} if requests is None else requests),
            ),
            capture.CaptureOptions(1.0, 2.0, 10.0, page_ids),
            capture.CaptureRuntime(shot, sleep, lambda: clock[0], status, lambda _pid: True),
        )
    judgement = copy.deepcopy(pending["judgement"])
    for page in judgement["pages"]:
        page["whole_page_status"] = review_status
        for visual in page["visual_results"]:
            visual["status"] = review_status
    judgement["findings"] = findings or []
    final = receipt.finalize(package, receipt.receipt_sha256(pending), judgement, state_reader=status)
    token = receipt.receipt_sha256(final)
    held = (package / "validation/iterations" / final["iteration"] / receipt.RECEIPT_NAME).read_bytes()
    assert hashlib.sha256(held).hexdigest() == token
    return token, final, events


@pytest.fixture
def r2_gate_runtime(monkeypatch: pytest.MonkeyPatch) -> list:
    """Keep every registered gate and its real main; double only AMO, schema CLI and installed engine."""
    datamodel = importlib.import_module("check_datamodel")
    pbir = importlib.import_module("check_pbir_valid")
    engine = importlib.import_module("check_engine_receipts")
    events = []

    def amo(models: list[Path]) -> tuple:
        assert len(models) == 1 and models[0].name == "Book.SemanticModel"
        events.append("AMO-boundary")
        return [], 1

    def schema(report: Path, _cli: str) -> dict:
        assert report.name == "Book.Report"
        events.append("PBIR-schema-boundary")
        return {"report": str(report), "status": "valid", "exit_code": 0, "codes": [], "errors": 0, "warnings": 0}

    monkeypatch.setattr(datamodel, "check_models", amo)
    monkeypatch.setattr(pbir, "find_cli", lambda _explicit=None: "fixture-schema-cli")
    monkeypatch.setattr(pbir, "validate_one", schema)
    monkeypatch.setattr(engine, "engine_root", lambda: REPO_ROOT)
    monkeypatch.setattr(engine, "engine_version", lambda _root: "2.368.0")

    def invoke(argv: list[str], _timeout: int = 300) -> CompletedProcess:
        script = Path(argv[1])
        events.append(script.name)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), monkeypatch.context() as process:
            process.setattr(sys, "argv", argv[1:])
            if script.name == "set_ai_instructions.py":
                spec = importlib.util.spec_from_file_location(
                    "r2_ai_instructions",
                    REPO_ROOT / ".github/skills/powerbi-ai-readiness/scripts/set_ai_instructions.py",
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            else:
                module = importlib.import_module(script.stem)
            try:
                code = module.main(argv[2:]) if inspect.signature(module.main).parameters else module.main()
            except SystemExit as exit_error:
                code = exit_error.code
        return CompletedProcess(argv, code, stdout.getvalue(), stderr.getvalue())

    monkeypatch.setattr(cu, "_run_simple", invoke)
    return events


def _r2_check(report: dict, name: str) -> dict:
    return next(row for row in report["checks"] if row["id"] == name)


def test_r2_complete_reaches_every_gate_with_exact_pin_and_no_numeric_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, capsys: pytest.CaptureFixture
) -> None:
    import iteration_receipt as receipt
    from PIL import Image

    package = _r2_package(tmp_path)
    _r2_expand_report(package)
    token, final, events = _r2_seal(package, monkeypatch)
    assert events == ["full-database-refresh", "named-canary", "image-readback", *["frame"] * 6]
    assert [(page["page_id"], page["expected_visual_ids"]) for page in final["generated"]["pages"]] == [
        ("p1", ["v1", "v2"]),
        ("p2", ["v3"]),
    ]
    head = receipt.read_chain(package, token)[-1]
    assert head.receipt_bytes == (head.directory / receipt.RECEIPT_NAME).read_bytes()
    assert final["generated"]["data_evidence"]["persistence"]["observation"]["image"]["commitment"] == "UNESTABLISHED"
    with Image.open(package / "reference/tableau-Executive.png") as image:
        image.load()
        assert image.size == (320, 240)
    output = tmp_path / "checked.json"
    code = cu.main([str(package), "--scope", "all", "--receipt-sha256", token, "--json", str(output)])
    report = json.loads(output.read_bytes())
    assert code == 0, cu.render(report)
    assert report["status"] == "COMPLETE" and _r2_check(report, "finalized")["status"] == "PASS"
    text = capsys.readouterr().out
    assert R2_DISCLAIMER in text and R2_WAIVER in text
    assert R2_DISCLAIMER in json.dumps(report) and R2_WAIVER in json.dumps(report)
    assert {gate.check_id for gate in ORIGINAL_GATES} <= {row["id"] for row in report["checks"]}
    assert all(row["status"] == "PASS" for row in report["checks"])
    coverage = _r2_check(report, "oracle-coverage")
    assert coverage["numeric_present"] == 0 and len(coverage["numeric_missing"]) == 2
    assert coverage["visual_present"] == 2 and not list(package.rglob("*.csv"))
    assert "AMO-boundary" in r2_gate_runtime and "PBIR-schema-boundary" in r2_gate_runtime
    assert _r2_check(report, "connection-fidelity")["payload"]["status"] == "OK"


@pytest.mark.parametrize(
    "token", [None, "", "f" * 64, "A" * 64, " " + "a" * 64, "a" * 64 + "\n", "g" * 64, "sha256:" + "a" * 64]
)
def test_r2_token_is_exact_caller_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, token: str | None
) -> None:
    package = _r2_package(tmp_path)
    _r2_seal(package, monkeypatch)
    report = cu.run_all(package, scope="all", receipt_sha256=token)
    assert report["exit_code"] == 2
    assert _r2_check(report, "finalized")["status"] == "NOT_CHECKED"
    assert "Phase-2 COMPLETE was not established." in cu.render(report)
    if token is not None:
        assert not r2_gate_runtime, "bad caller authority must stop before native/gate operations"
    else:
        assert "check_connection_fidelity.py" in r2_gate_runtime, "tokenless checks remain useful diagnostics"


@pytest.mark.parametrize("numeric", ["none", "required"])
def test_r2_numeric_authority_does_not_erase_raw_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, numeric: str
) -> None:
    package = _r2_package(tmp_path, numeric=numeric)
    token, _, _ = _r2_seal(package, monkeypatch)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == (0 if numeric == "none" else 2), cu.render(report)
    coverage = _r2_check(report, "oracle-coverage")
    assert coverage["numeric_present"] == 0 and len(coverage["numeric_missing"]) == 1
    assert ("CANNOT_ESTABLISH(NUMERIC)" in cu.render(report)) == (numeric == "required")
    assert (R2_WAIVER in cu.render(report)) == (numeric == "none")


@pytest.mark.parametrize("empty_refusal_diagnostic", [False, True], ids=["production-envelope", "empty-diagnostic"])
def test_r2_numeric_waiver_never_clears_zero_page_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    r2_gate_runtime: list,
    capsys: pytest.CaptureFixture,
    empty_refusal_diagnostic: bool,
) -> None:
    """Signed parity can pass while the real oracle denominator is empty and the review unverified."""
    package = _r2_package(tmp_path)
    page_path = f"{R2_REPORT}/definition/pages/p1/page.json"
    page = json.loads((package / page_path).read_bytes())
    page["displayName"] = "Other"
    _r2_put(package, page_path, page)
    _write_exemptions(
        package,
        [{"check": "page-parity", "item": "dash"}, {"check": "page-parity", "item": "extra:Other"}],
    )
    _r2_declare(package, page_path, cu.EXEMPTIONS_FILE)
    token, final, _ = _r2_seal(package, monkeypatch, review_status="unverified")
    assert final["schema_version"] == 3 and final["state"] == "final"
    assert final["judgement"]["pages"][0]["whole_page_status"] == "unverified"
    assert cu.check_page_parity(package, cu.load_exemptions(package))["status"] == "PASS"
    not_assessable = cu._oracle_not_assessable
    observed = []

    def observe(*args, **kwargs):
        row = not_assessable(*args, **kwargs)
        assert "refused_evidence" not in row
        # An optional empty diagnostic must not change the production zero-page refusal.
        if empty_refusal_diagnostic:
            row["refused_evidence"] = []
        observed.append(copy.deepcopy(row))
        return row

    monkeypatch.setattr(cu, "_oracle_not_assessable", observe)
    raw = cu.check_oracle_coverage(package, None, None)
    assert (raw["status"], raw["pages"], raw["visual_present"], raw["numeric_present"]) == ("NOT_CHECKED", 0, 0, 0)
    assert raw["visual_missing"] == raw["numeric_missing"] == raw["contested_names"] == raw["rows"] == []
    assert [row["name"] for row in raw["excluded_omissions"]] == ["Executive"]
    before = {path.relative_to(package): path.read_bytes() for path in package.rglob("*") if path.is_file()}
    capsys.readouterr()
    try:
        report = cu.run_all(package, receipt_sha256=token)
        output = tmp_path / "zero-page-check.json"
        code = cu.main([str(package), "--scope", "all", "--receipt-sha256", token, "--json", str(output)])
    except (KeyError, TypeError) as error:
        pytest.fail(f"zero-page oracle must return typed non-success, not raise {error!r}")
    cli_report = json.loads(output.read_bytes())
    assert code == 2 and cli_report == report
    assert report["status"] == "NOT_CHECKED" and report["exit_code"] == 2
    coverage = _r2_check(report, "oracle-coverage")
    assert coverage["status"] == "NOT_CHECKED", "a numeric waiver cannot clear an empty oracle denominator"
    assert {key: coverage[key] for key in raw} == raw, "waiving numeric comparison must preserve raw oracle facts"
    assert len(observed) == 3 and all(row == raw for row in observed)
    assert _r2_check(report, "visual-comparison-done")["code"] == "visual_comparison_not_pass"
    final_row = _r2_check(report, "finalized")
    assert (final_row["status"], final_row["stage"], final_row["code"]) == (
        "NOT_CHECKED",
        "OBLIGATIONS",
        "required_obligations_not_satisfied",
    )
    assert not any(row["id"] == "finalized" and row["status"] == "PASS" for row in report["checks"])
    text = capsys.readouterr().out
    assert "CANNOT_ESTABLISH(OBLIGATIONS)" in text and "Phase-2 COMPLETE was not established." in text
    assert R2_DISCLAIMER not in text and "AMO-boundary" in r2_gate_runtime
    assert before == {path.relative_to(package): path.read_bytes() for path in package.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    "field,value",
    [
        *[
            pytest.param(field, ..., id=f"missing-{field}")
            for field in ("pages", "visual_present", "visual_missing", "contested_names", "refused_evidence")
        ],
        *[
            pytest.param(field, value, id=f"{field}-{type(value).__name__}")
            for field in ("visual_missing", "contested_names", "refused_evidence")
            for value in (None, False, 0, "", {}, ())
        ],
        *[
            pytest.param(field, value, id=f"{field}-{value!r}")
            for field in ("pages", "visual_present")
            for value in (None, False, True, 0, -1, 1.0, "1")
        ],
        ("visual_missing", [{"name": "Executive"}]),
        ("contested_names", ["Executive"]),
        ("refused_evidence", ["ambiguous"]),
        ("status", "FINDINGS"),
        ("status", "PRECONDITION_FAILED"),
        ("status", "ERROR"),
    ],
)
def test_r2_numeric_waiver_requires_measured_visual_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, field: str, value: object
) -> None:
    """Vary one field of a real measured row; absent/falsey diagnostics are not clean defaults."""
    package = _r2_package(tmp_path)
    token, _, _ = _r2_seal(package, monkeypatch)
    coverage = cu.check_oracle_coverage
    observed = []

    def changed(*args, **kwargs):
        row = coverage(*args, **kwargs)
        assert (row["status"], row["pages"], row["visual_present"], row["numeric_present"]) == (
            "NOT_CHECKED",
            1,
            1,
            0,
        )
        if value is ...:
            row.pop(field)
        else:
            row[field] = value
        observed.append(copy.deepcopy(row))
        return row

    monkeypatch.setattr(cu, "check_oracle_coverage", changed)
    try:
        report = cu.run_all(package, receipt_sha256=token)
    except (KeyError, TypeError) as error:
        pytest.fail(f"unestablished oracle facts must return typed non-success, not raise {error!r}")
    row = _r2_check(report, "oracle-coverage")
    assert row["status"] == (value if field == "status" else "NOT_CHECKED"), "only measured numeric gaps may be waived"
    assert len(observed) == 1 and {key: row[key] for key in observed[0]} == observed[0]
    if value is ...:
        assert field not in row, "do not manufacture missing coverage facts"
    expected_code = {"FINDINGS": 1, "PRECONDITION_FAILED": 4}.get(row["status"], 2)
    assert report["exit_code"] == expected_code and report["status"] != "COMPLETE"
    assert _r2_check(report, "finalized")["code"] == "required_obligations_not_satisfied"
    assert "AMO-boundary" in r2_gate_runtime


@pytest.mark.parametrize("flag", ["--reference-dir", "--oracle-dir"])
@pytest.mark.parametrize("numeric", ["required", "none"])
@pytest.mark.parametrize("entrypoint", ["run-all", "cli"])
@pytest.mark.parametrize("brief", ["current", "malformed", "stale"])
def test_r2_reference_override_retains_safe_numeric_obligation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    r2_gate_runtime: list,
    capsys: pytest.CaptureFixture,
    flag: str,
    numeric: str,
    entrypoint: str,
    brief: str,
) -> None:
    package = _r2_package(tmp_path, numeric=numeric)
    if brief != "current":
        content = (package / "migration-brief.md").read_text()
        content = (
            content.replace(f'"{numeric}"', f'"{numeric}') if brief == "malformed" else content + "Stale digest.\n"
        )
        _r2_put(package, "migration-brief.md", content.encode())
        if brief == "malformed":
            _r2_declare(package, "migration-brief.md")
    token, _, _ = _r2_seal(package, monkeypatch)
    override = tmp_path / "unread-override"
    before = {path.relative_to(package): path.read_bytes() for path in package.rglob("*") if path.is_file()}
    capsys.readouterr()

    def forbidden(*_args, **_kwargs):
        pytest.fail("a refused reference override must not reach later discovery, gates or completion")

    with monkeypatch.context() as guard:
        for name in ("load_exemptions", "check_oracle_coverage", "_finish_completion", "inspect_brownfield"):
            guard.setattr(cu, name, forbidden)
        for method in ("open", "stat", "lstat", "resolve", "iterdir", "glob", "rglob"):
            original = getattr(Path, method)

            def guarded(path, *args, _original=original, **kwargs):
                assert path != override and override not in path.parents, "supplied override path must remain unread"
                return _original(path, *args, **kwargs)

            guard.setattr(Path, method, guarded)
        if entrypoint == "cli":
            output = tmp_path / "override-check.json"
            code = cu.main(
                [str(package), "--scope", "all", "--receipt-sha256", token, flag, str(override), "--json", str(output)]
            )
            report = json.loads(output.read_bytes())
            text = capsys.readouterr().out
        else:
            report = cu.run_all(package, receipt_sha256=token, **{flag[2:].replace("-", "_"): override})
            code, text = report["exit_code"], cu.render(report)
    assert before == {path.relative_to(package): path.read_bytes() for path in package.rglob("*") if path.is_file()}
    assert r2_gate_runtime == []
    assert code == 2 and report["status"] == "NOT_CHECKED", "an override refusal must never confer COMPLETE"
    numeric_rows = [row for row in report["checks"] if row["id"] == "numeric-obligation"]
    assert len(numeric_rows) == 1, "the safely known numeric obligation must remain machine-visible on refusal"
    row = numeric_rows[0]
    final_row = _r2_check(report, "finalized")
    if brief == "current":
        assert [check["id"] for check in report["checks"] if check["status"] == "PASS"] == [
            "iteration-history",
            "current-source-data",
            "current-working-namespace",
            "model-class",
        ]
        assert (final_row["stage"], final_row["code"]) == ("REFERENCE", "external_evidence_override_not_supported")
        assert row["numeric_obligation"] == numeric
        assert row["code"] == (
            "numeric_required_unsupported" if numeric == "required" else "numeric_waiver_not_applied"
        )
    else:
        if brief == "malformed":
            assert [check["id"] for check in report["checks"]] == ["numeric-obligation", "finalized"]
            assert (final_row["stage"], final_row["code"], row["code"]) == (
                "NUMERIC",
                "brief_frontmatter_unparseable",
                "brief_frontmatter_unparseable",
            )
        else:
            assert [check["id"] for check in report["checks"]] == [
                "current-source-data",
                "numeric-obligation",
                "finalized",
            ]
            assert (final_row["stage"], final_row["code"], row["code"]) == (
                "SOURCE_DATA",
                "package_file_digest_mismatch",
                "numeric_authority_unestablished",
            )
        assert row["numeric_obligation"] is None
        assert "external_evidence_override_not_supported" not in json.dumps(report)
    assert row["status"] == "NOT_CHECKED" and row["stage"] == "NUMERIC" and row["numeric_evidence"] == "unestablished"
    assert final_row["status"] == "NOT_CHECKED"
    assert "CANNOT_ESTABLISH(NUMERIC)" in text and "Phase-2 COMPLETE was not established." in text
    assert "CANNOT_ESTABLISH(NUMERIC)" in json.dumps(report)
    assert R2_DISCLAIMER not in text and R2_WAIVER not in text


@pytest.mark.parametrize("scope", ["model", "report", "integration"])
def test_r2_layer_scopes_never_claim_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, scope: str
) -> None:
    package = _r2_package(tmp_path)
    token, _, _ = _r2_seal(package, monkeypatch)
    report = cu.run_all(package, scope=scope, receipt_sha256=token)
    assert report["status"] != "COMPLETE"
    assert "finalized" in report["omitted_checks"]
    assert R2_DISCLAIMER not in cu.render(report) and R2_WAIVER not in cu.render(report)


@pytest.mark.parametrize("category", ["file_ok", "remote_import", "inline"])
def test_r2_import_class_has_three_positive_categories(tmp_path: Path, category: str) -> None:
    package = _r2_package(tmp_path)
    partition = R2_SQL
    if category != "remote_import":
        expression = (
            'Csv.Document(File.Contents("sales.csv"))' if category == "file_ok" else '#table({"Amount"}, {{7}})'
        )
        partition = (
            "\tpartition Sales = m\n\t\tmode: import\n\t\tsource =\n"
            f"\t\t\tlet\n\t\t\t    Source = {expression}\n\t\t\tin\n\t\t\t    Source\n"
        )
        _r2_put(package, f"{R2_MODEL}/sales.csv", b"Amount\n7\n")
    table = (package / R2_TABLE).read_text()
    _r2_put(package, R2_TABLE, table.replace(R2_SQL, partition).encode())
    result = cu._import_model_class(package, R2_MODEL)
    assert result["status"] == "PASS" and result["partition_count"] == 1, result
    assert result["tables"]["Sales"][0]["category"] == category


@pytest.mark.parametrize(
    "partition,code",
    [
        (R2_SQL.replace("mode: import", "mode: directQuery"), "partition_mode_not_explicit_import"),
        (R2_SQL.replace("mode: import", "mode: dual"), "partition_mode_not_explicit_import"),
        (R2_SQL.replace("mode: import", "mode: directLake"), "partition_mode_not_explicit_import"),
        (R2_SQL.replace("mode: import", "mode: unknown"), "partition_mode_not_explicit_import"),
        (R2_SQL.replace("\t\tmode: import\n", ""), "partition_mode_not_explicit_import"),
        (R2_SQL.replace("mode: import", "mode: import\n\t\tmode: import"), "partition_mode_not_explicit_import"),
        (R2_SQL.replace("mode: import", "mode: import\n\t\tmode: directQuery"), "partition_mode_not_explicit_import"),
        (
            R2_SQL.replace("\t\tmode: import\n", "").replace("\t\t\tlet", "\t\t\tlet\n\t\t\t    mode: import"),
            "partition_mode_not_explicit_import",
        ),
        (R2_SQL.replace("Sales = m", "Sales = calculated"), "partition_kind_unsupported"),
        (R2_SQL.replace("Sales = m", "Sales = entity"), "partition_kind_unsupported"),
        (R2_SQL.replace("Sales = m", "Sales"), "partition_unaccounted"),
        (R2_SQL.replace("Sql.Database", "Mystery.Database"), "partition_category_unsupported"),
        (
            R2_SQL + R2_SQL.replace("partition Sales", "partition Other").replace("mode: import", "mode: dual"),
            "partition_mode_not_explicit_import",
        ),
        ("", "partition_unaccounted"),
    ],
    ids=[
        "direct-query",
        "dual",
        "direct-lake",
        "unknown-mode",
        "absent-mode",
        "duplicate-import",
        "duplicate-mixed",
        "expression-only-mode",
        "calculated",
        "implicit-entity",
        "unrecognized-partition",
        "unknown-category",
        "mixed-storage",
        "zero-partitions",
    ],
)
def test_r2_unsupported_partition_refuses_before_other_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, partition: str, code: str
) -> None:
    package = _r2_package(tmp_path)
    _r2_put(package, R2_TABLE, (package / R2_TABLE).read_text().replace(R2_SQL, partition).encode())
    token, _, _ = _r2_seal(package, monkeypatch)
    result = cu.run_all(package, receipt_sha256=token)
    assert result["exit_code"] == 2
    assert _r2_check(result, "model-class")["code"] == code, result
    assert r2_gate_runtime == [] and _r2_check(result, "finalized")["status"] == "NOT_CHECKED"


@pytest.mark.parametrize(
    "change,code",
    [
        ("unreadable", "model_class_unreadable"),
        ("omitted", "partition_unaccounted"),
        ("wrong-ref", "table_reference_unaccounted"),
        ("duplicate-ref", "table_reference_unaccounted"),
        ("unrecognized-table", "table_declaration_unaccounted"),
        ("zero-tables", "table_set_not_canonical"),
        ("nested", "table_set_not_canonical"),
    ],
)
def test_r2_complete_partition_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, code: str
) -> None:
    package = _r2_package(tmp_path)
    model_text = package / R2_MODEL / "definition/model.tmdl"
    if change == "unreadable":
        _r2_put(package, R2_TABLE, b"\xff")
    elif change == "omitted":
        monkeypatch.setattr(cu.empty_model, "_partition_blocks", lambda _text: [])
    elif change in ("wrong-ref", "duplicate-ref"):
        text = model_text.read_text()
        _r2_put(
            package,
            f"{R2_MODEL}/definition/model.tmdl",
            (
                text.replace("ref table Sales", "ref table Other")
                if change == "wrong-ref"
                else text + "\tref table Sales\n"
            ).encode(),
        )
    elif change == "unrecognized-table":
        _r2_put(package, R2_TABLE, (package / R2_TABLE).read_text().replace("table Sales", "table Sales = ?").encode())
    elif change == "zero-tables":
        (package / R2_TABLE).unlink()
    else:
        _r2_put(package, f"{R2_MODEL}/definition/tables/nested/Other.tmdl", b"table Other\n")
    result = cu._import_model_class(package, R2_MODEL)
    assert result["status"] == "NOT_CHECKED" and result["code"] == code, result


@pytest.mark.parametrize(
    "path",
    [
        "rogue.txt",
        "validation/loose.png",
        f"{R2_MODEL}/.pbi/unappliedChanges.json",
        f"{R2_REPORT}/.pbi/rogue.json",
    ],
)
def test_r2_namespace_rejects_extra_files_even_with_current_valid_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, path: str
) -> None:
    import iteration_receipt as receipt

    package = _r2_package(tmp_path)
    _r2_put(package, path, b"rogue\n")
    token, _, _ = _r2_seal(package, monkeypatch)
    assert receipt.read_chain(package, token)[-1].receipt_sha256 == token
    report = cu.run_all(package, receipt_sha256=token)
    row = _r2_check(report, "current-working-namespace")
    assert report["exit_code"] == 2 and row["extra"] == [path] and row["missing"] == []
    assert not r2_gate_runtime


def test_r2_namespace_missing_declared_file_is_not_a_new_smaller_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    package = _r2_package(tmp_path)
    (package / R2_TABLE).unlink()
    token, _, _ = _r2_seal(package, monkeypatch)
    report = cu.run_all(package, receipt_sha256=token)
    assert _r2_check(report, "current-working-namespace")["missing"] == [R2_TABLE]
    assert report["exit_code"] == 2 and r2_gate_runtime == []


@pytest.mark.parametrize(
    "path",
    [
        "validation/iterations/loose.png",
        "validation/iterations/001/loose.png",
        "validation/iterations/003/iteration.json",
    ],
)
def test_r2_loose_iteration_files_never_gain_wildcard_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, path: str
) -> None:
    package = _r2_package(tmp_path)
    token, _, _ = _r2_seal(package, monkeypatch)
    _r2_put(package, path, b"loose")
    report = cu.run_all(package, receipt_sha256=token)
    final = _r2_check(report, "finalized")
    assert report["exit_code"] == 2 and final["stage"] == "RECEIPT"
    assert final["code"] == ("ITERATION_GAP" if "003" in path else "EXTRA_FILE")
    assert r2_gate_runtime == []


@pytest.mark.parametrize(
    "change",
    [
        "required",
        "v1",
        "plain",
        "missing-field",
        "malformed",
        "duplicate",
        "unknown",
        "scope",
        "unit",
        "stale",
        "missing-brief",
    ],
)
def test_r2_current_brief_is_the_only_numeric_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, change: str
) -> None:
    package = _r2_package(tmp_path)
    original = (package / "migration-brief.md").read_text()
    variants = {
        "required": original.replace('"none"', '"required"'),
        "v1": original.replace("/v2", "/v1").replace('numeric_obligation = "none"\n', ""),
        "plain": "Numeric comparison waived in prose only.\n",
        "missing-field": original.replace('numeric_obligation = "none"\n', ""),
        "malformed": original.replace('"none"', '"none'),
        "duplicate": original.replace(
            'numeric_obligation = "none"', 'numeric_obligation = "none"\nnumeric_obligation = "none"'
        ),
        "unknown": original.replace('"none"', '"optional"'),
        "scope": original.replace('"model_and_report"', '"model_only"'),
        "unit": original.replace('"Book"', '"Other"'),
        "stale": original + "Stale digest.\n",
        "missing-brief": "",
    }
    _r2_put(package, "migration-brief.md", variants[change].encode())
    if change == "missing-brief":
        (package / "migration-brief.md").unlink()
    elif change != "stale":
        _r2_declare(package, "migration-brief.md")
    token, _, _ = _r2_seal(package, monkeypatch)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2, cu.render(report)
    row = _r2_check(report, "numeric-obligation")
    assert row["status"] == "NOT_CHECKED"
    assert "CANNOT_ESTABLISH(NUMERIC)" in cu.render(report) and "Phase-2 COMPLETE was not established." in cu.render(
        report
    )
    assert R2_DISCLAIMER not in cu.render(report) and R2_WAIVER not in cu.render(report)
    if change != "required":
        assert not r2_gate_runtime


def test_r2_numeric_csv_presence_never_overrides_required_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    package = _r2_package(tmp_path, numeric="required")
    _r2_put(package, "reference/tableau-values.csv", b"Amount\n7\n")
    reference = json.loads((package / "reference/manifest.json").read_bytes())
    reference["dashboards"][0]["states"][0]["numeric_oracle"] = "tableau-values.csv"
    _r2_put(package, "reference/manifest.json", reference)
    _r2_declare(package, "reference/tableau-values.csv")
    token, _, _ = _r2_seal(package, monkeypatch)
    report = cu.run_all(package, receipt_sha256=token)
    assert _r2_check(report, "oracle-coverage")["numeric_present"] == 1
    assert _r2_check(report, "numeric-obligation")["status"] == "NOT_CHECKED"
    assert report["exit_code"] == 2 and report["status"] != "COMPLETE"


@pytest.mark.parametrize(
    "change,code",
    [
        ("zero", "a1_zero_returned_rows"),
        ("canaries", "a1_canaries_unestablished"),
        ("refresh", "a1_refresh_refused"),
        ("persistence", "a1_persistence_refused"),
        ("no-refresh", "a1_full_refresh_persistence_named_canaries_required"),
        ("no-canaries", "a1_full_refresh_persistence_named_canaries_required"),
        ("no-persist", "a1_full_refresh_persistence_named_canaries_required"),
    ],
)
def test_r2_neutral_a1_observations_do_not_mean_positive_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, change: str, code: str
) -> None:
    package = _r2_package(tmp_path)
    requests = {"refresh": True, "persist": True, "canaries": ("Sales",)}
    if change.startswith("no-"):
        requests[change[3:]] = () if change == "no-canaries" else False
    token, _, _ = _r2_seal(
        package,
        monkeypatch,
        requests=requests,
        rows=0 if change == "zero" else 1,
        unavailable=change if change in ("refresh", "canaries", "persistence") else None,
    )
    report = cu.run_all(package, receipt_sha256=token)
    assert _r2_check(report, "data-evidence")["code"] == code, cu.render(report)
    assert report["exit_code"] == (1 if change == "zero" else 2)
    assert _r2_check(report, "finalized")["status"] == "NOT_CHECKED"


@pytest.mark.parametrize("state", ["blocked", "cannot_establish", "authorized_model_only", "wrong-source"])
def test_r2_canonical_data_reconciliation_preserves_negative_state_and_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, state: str
) -> None:
    package = _r2_package(tmp_path)
    projection = json.loads((package / "data-access.json").read_bytes())
    if state == "wrong-source":
        projection["source_keys"] = ["source-key:e625ce798a6d19bb"]
    elif state == "authorized_model_only":
        projection.update(
            state=state,
            validation="unvalidated",
            effective_scope="model_only",
            max_phase2_claim="structural_only",
            codes=["brief-model-only", "human-authorize"],
        )
    else:
        projection.update(
            state=state,
            validation="not_established",
            effective_scope=None,
            max_phase2_claim="none",
            codes=["probe-no-credential"] if state == "blocked" else ["spec-unreadable"],
        )
        if state == "cannot_establish":
            projection["source_keys"] = []
    _r2_put(package, "data-access.json", projection)
    _r2_declare(package, "data-access.json")
    token, _, _ = _r2_seal(package, monkeypatch)
    report = cu.run_all(package, receipt_sha256=token)
    assessed = _r2_check(report, "current-source-data")["assessment"]
    assert report["exit_code"] == 2 and r2_gate_runtime == []
    if state in ("blocked", "cannot_establish"):
        assert assessed == projection
    else:
        assert assessed["codes"] == ["source-key-set-changed" if state == "wrong-source" else "authorization-mismatch"]
        assert assessed["max_phase2_claim"] == "none"


@pytest.mark.parametrize("variant", ["missing", "refused", "copied", "reconstructed", "grafted", "tuple", "revision"])
def test_r2_w_must_be_the_original_issued_object_for_this_exact_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, variant: str
) -> None:
    package = _r2_package(tmp_path)
    token, _, _ = _r2_seal(package, monkeypatch)
    original = cu.roles.read_current_source_data_handoff
    calls = []

    def issue(root: Path, *, expected_package_working_revision: str) -> tuple:
        calls.append(expected_package_working_revision)
        if variant == "revision":
            _r2_put(root, R2_TABLE, (root / R2_TABLE).read_bytes() + b"\n")
        code, handoff = original(root, expected_package_working_revision=expected_package_working_revision)
        if variant == "revision":
            assert code == "working_revision_mismatch" and handoff is None
            return code, handoff
        assert code is None and handoff is not None
        if variant == "missing":
            return None, None
        if variant == "refused":
            return "working_handoff_invalid", None
        if variant == "copied":
            return None, copy.copy(handoff)
        if variant in ("reconstructed", "grafted"):
            other = cu.roles.CurrentWorkingSourceDataHandoff(
                **{field.name: getattr(handoff, field.name) for field in fields(handoff) if field.init}
            )
            if variant == "grafted":
                object.__setattr__(other, "_authority", handoff._authority)
            return None, other
        object.__setattr__(handoff, "unit", "Other")
        return None, handoff

    monkeypatch.setattr(cu.roles, "read_current_source_data_handoff", issue)
    reconcile = []
    real_reconcile = cu.credential_gate.reconcile_package_data_access
    monkeypatch.setattr(
        cu.credential_gate,
        "reconcile_package_data_access",
        lambda *args, **kwargs: (reconcile.append(True), real_reconcile(*args, **kwargs))[1],
    )
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2 and len(calls) == 1 and reconcile == [] and r2_gate_runtime == []
    assert _r2_check(report, "finalized")["code"] == (
        "working_revision_mismatch"
        if variant == "revision"
        else "working_target_mismatch"
        if variant == "tuple"
        else "working_handoff_invalid"
    )


def _r2_rewrite_receipt(package: Path, payload: dict) -> str:
    """Negative control: caller pins these exact changed bytes, without claiming producer authenticity."""
    import iteration_receipt as receipt

    raw = receipt.receipt_bytes(payload)
    path = package / "validation/iterations" / payload["iteration"] / receipt.RECEIPT_NAME
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "change,code",
    [
        ("binding", "A1_BINDING_MISMATCH"),
        ("catalogue", "A1_BINDING_MISMATCH"),
        ("image", "A1_CACHE_MISMATCH"),
        ("readback", "A1_CACHE_MISMATCH"),
        ("commitment", "SCHEMA"),
        ("empty-canary", "A1_CANARY_SET"),
        ("refused-canary", "a1_canaries_refused"),
        ("page-judgment", "JUDGEMENT_PAGE_SET"),
        ("visual-judgment", "JUDGEMENT_VISUAL_SET"),
        ("numeric-label", "SCHEMA"),
    ],
)
def test_r2_receipt_observations_and_denominators_cannot_be_relabelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, change: str, code: str
) -> None:
    package = _r2_package(tmp_path)
    _, payload, _ = _r2_seal(package, monkeypatch)
    facts = payload["generated"]["data_evidence"]
    if change == "binding":
        facts["canaries"]["observation"][0]["identity"]["port"] += 1
    elif change == "catalogue":
        facts["refresh"]["observation"]["catalogue"] = OTHER_LUID
    elif change in ("image", "readback", "commitment"):
        image = facts["persistence"]["observation"]["image"]
        image[{"image": "intended_sha256", "readback": "installed_sha256", "commitment": "commitment"}[change]] = (
            "ESTABLISHED" if change == "commitment" else "f" * 64
        )
    elif change == "empty-canary":
        facts["canaries"]["observation"] = []
    elif change == "refused-canary":
        facts["canaries"] = {"status": "refused", "reason": "TOOL_UNAVAILABLE", "observation": None}
    elif change == "page-judgment":
        payload["judgement"]["pages"] = []
    elif change == "visual-judgment":
        payload["judgement"]["pages"][0]["visual_results"] = []
    else:
        payload["judgement"]["pages"][0]["numeric_results"][0]["status"] = "pass"
    token = _r2_rewrite_receipt(package, payload)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2
    if change == "refused-canary":
        assert _r2_check(report, "data-evidence")["code"] == code
    else:
        assert _r2_check(report, "finalized")["code"] == code
        assert r2_gate_runtime == []
    assert R2_DISCLAIMER not in cu.render(report)


@pytest.mark.parametrize(
    "change,code",
    [
        ("pending", "NO_FINAL_ITERATION"),
        ("triage", "final_v3_all_pages_sign_off_required"),
        ("v2", "final_v3_all_pages_sign_off_required"),
        ("revision", "GENERATED_CHANGED"),
        ("tuple", "GENERATED_CHANGED"),
    ],
)
def test_r2_terminal_receipt_must_be_current_final_v3_sign_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, change: str, code: str
) -> None:
    import iteration_receipt as receipt

    package = _r2_package(tmp_path)
    _, payload, _ = _r2_seal(package, monkeypatch)
    generated = payload["generated"]
    if change == "pending":
        payload["state"] = "pending"
        payload["judgement"]["completed_at"] = None
    elif change == "triage":
        payload["mode"] = "triage"
    elif change in ("tuple", "revision"):
        field = "unit" if change == "tuple" else "package_revision"
        value = "Other" if change == "tuple" else "sha256:" + "f" * 64
        generated["artifact"][field] = value
        generated["preparation"]["artifact_before"][field] = value
    else:
        payload.update(schema_version=2, outcome="incomplete")
        generated["review"]["tool_version"] = "2.0.0"
        for name in ("preparation", "numeric_evidence", "retained_roles"):
            del generated[name]
        generated["data_evidence"] = {"status": "pending", "reason": receipt.DATA_PENDING_REASON}
        for page in payload["judgement"]["pages"]:
            for numeric in page["numeric_results"]:
                numeric.update(
                    status="unverified",
                    tableau_evidence_sha256=None,
                    powerbi_query_sha256=None,
                    powerbi_result_sha256=None,
                )
    token = _r2_rewrite_receipt(package, payload)
    if change in ("triage", "v2"):
        assert receipt.read_chain(package, token)[-1].payload["state"] == "final"
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2 and _r2_check(report, "finalized")["code"] == code
    assert r2_gate_runtime == []


@pytest.mark.parametrize("change", ["cache", "missing-cache", "invalid-png", "missing-page", "missing-visual"])
def test_r2_current_bytes_remain_pinned_after_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, change: str
) -> None:
    import iteration_receipt as receipt

    package = _r2_package(tmp_path)
    token, final, _ = _r2_seal(package, monkeypatch)
    if change in ("cache", "missing-cache"):
        cache = package / R2_MODEL / ".pbi/cache.abf"
        if change == "cache":
            cache.write_bytes(b"different-cache")
        else:
            cache.unlink()
        expected = "GENERATED_CHANGED"
    elif change == "invalid-png":
        path = package / "validation/iterations/001" / final["generated"]["pages"][0]["powerbi"]["path"]
        path.write_bytes(b"\x89PNG\r\n\x1a\njunk")
        expected = "SCREENSHOT_NOT_PNG"
    else:
        name = "page.json" if change == "missing-page" else "visuals/v1/visual.json"
        (package / R2_REPORT / "definition/pages/p1" / name).unlink()
        expected = "PAGE_DEFINITION_MISSING" if change == "missing-page" else "VISUAL_DEFINITION_MISSING"
    with pytest.raises(receipt.ReceiptError) as error:
        receipt.read_chain(package, token)
    assert error.value.code == expected
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2 and _r2_check(report, "finalized")["code"] == expected
    assert r2_gate_runtime == []


@pytest.mark.parametrize("status", ["unverified", "mismatch", "layout_match"])
def test_r2_every_page_and_visual_requires_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, status: str
) -> None:
    package = _r2_package(tmp_path)
    token, _, _ = _r2_seal(package, monkeypatch, review_status=status)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == (1 if status == "mismatch" else 2)
    assert _r2_check(report, "visual-comparison-done")["code"] == "visual_comparison_not_pass"
    assert _r2_check(report, "finalized")["status"] == "NOT_CHECKED"


@pytest.mark.parametrize("change", ["low-grade", "wrong-source", "unstable"])
def test_r2_reference_grade_attribution_and_capture_stability_are_not_numeric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, change: str
) -> None:
    package = _r2_package(tmp_path)
    reference = json.loads((package / "reference/manifest.json").read_bytes())
    if change == "low-grade":
        reference["dashboards"][0]["states"][0]["capabilities"] = ["layout_grade", "text_readable"]
    elif change == "wrong-source":
        reference["source_workbook_sha256"] = "f" * 64
    _r2_put(package, "reference/manifest.json", reference)
    token, payload, _ = _r2_seal(package, monkeypatch, review_status="unverified")
    if change == "unstable":
        payload["generated"]["pages"][0]["powerbi"]["capture"]["converged"] = False
        token = _r2_rewrite_receipt(package, payload)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2, cu.render(report)
    assert _r2_check(report, "visual-comparison-done")["status"] == "NOT_CHECKED"
    if change == "unstable":
        assert _r2_check(report, "visual-layer-done")["status"] == "NOT_CHECKED"
    assert R2_DISCLAIMER not in cu.render(report)


@pytest.mark.parametrize(
    "change,check_id",
    [
        ("description", "ai-descriptions"),
        ("domain", "ai-descriptions"),
        ("instructions", "ai-instructions"),
        ("qna", "ai-instructions"),
    ],
)
def test_r2_existing_ai_gaps_stay_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, change: str, check_id: str
) -> None:
    package = _r2_package(tmp_path)
    if change in ("description", "domain"):
        text = (package / R2_TABLE).read_text()
        text = (
            text.replace("/// One row per sale.\n", "")
            if change == "description"
            else text.replace("One of: Open, Closed.", "Sale workflow status.")
        )
        _r2_put(package, R2_TABLE, text.encode())
    elif change == "instructions":
        path = f"{R2_MODEL}/definition/cultures/en-US.tmdl"
        _r2_put(package, path, (package / path).read_text().replace("CustomInstructions", "NoInstructions").encode())
    else:
        _r2_put(package, f"{R2_MODEL}/definition.pbism", {"version": "4.2", "settings": {"qnaEnabled": False}})
    token, _, _ = _r2_seal(package, monkeypatch)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 1, cu.render(report)
    assert _r2_check(report, check_id)["status"] == "FINDINGS"
    assert R2_DISCLAIMER not in cu.render(report)


def _r2_finding(**changes: object) -> dict:
    return {
        "id": "F-001",
        "page_id": "p1",
        "visual_id": "v1",
        "kind": "visual",
        "severity": "high",
        "status": "still_open",
        "detail": "Missing title",
        "limitation_ref": None,
        **changes,
    }


def test_r2_real_history_old_head_refuses_current_head_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    import iteration_receipt as receipt

    package = _r2_package(tmp_path)
    old, _, _ = _r2_seal(package, monkeypatch, findings=[_r2_finding()])
    first = cu.run_all(package, receipt_sha256=old)
    assert first["exit_code"] == 1 and _r2_check(first, "iteration-findings")["code"] == "open_findings"
    _r2_put(package, R2_TABLE, (package / R2_TABLE).read_bytes() + b"\n")
    token, _, _ = _r2_seal(package, monkeypatch, previous=old, findings=[_r2_finding(status="resolved")])
    assert [item.name for item in receipt.read_chain(package, token)] == ["001", "002"]
    r2_gate_runtime.clear()
    refused = cu.run_all(package, receipt_sha256=old)
    assert refused["exit_code"] == 2 and _r2_check(refused, "finalized")["code"] == "FINAL_RECEIPT_MISMATCH"
    assert r2_gate_runtime == []
    current = cu.run_all(package, receipt_sha256=token)
    assert current["exit_code"] == 0, cu.render(current)
    assert _r2_check(current, "current-working-namespace")["extra"] == []


@pytest.mark.parametrize("change", ["receipt", "revision", "brief", "namespace", "class", "w-identity"])
def test_r2_after_gate_changes_refuse_without_selecting_new_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, change: str
) -> None:
    package = _r2_package(tmp_path)
    token, final, _ = _r2_seal(package, monkeypatch)
    calls, originals = [], []
    reader, validator = cu.roles.read_current_source_data_handoff, cu.roles.validate_current_source_data_handoff

    def issued(*args, **kwargs):
        code, original = reader(*args, **kwargs)
        originals.append(original)
        return code, original

    def validate(root, original, **kwargs):
        assert original is originals[0], "the ORIGINAL W must survive, never a successful second issuance"
        calls.append(kwargs["expected_package_working_revision"])
        return validator(root, original, **kwargs)

    monkeypatch.setattr(cu.roles, "read_current_source_data_handoff", issued)
    monkeypatch.setattr(cu.roles, "validate_current_source_data_handoff", validate)
    orphan = cu.check_desktop_orphans

    def changed(root: Path) -> dict:
        row = orphan(root)
        if change == "receipt":
            final["judgement"]["pages"][0]["whole_page_status"] = "unverified"
            _r2_rewrite_receipt(root, final)
        elif change == "brief":
            _r2_put(
                root,
                "migration-brief.md",
                (root / "migration-brief.md").read_text().replace('"none"', '"required"').encode(),
            )
        elif change == "namespace":
            _r2_put(root, "rogue.txt", b"extra")
        elif change == "w-identity":
            # Bytes and R stay identical, but the original held member's physical interval changed.
            path = root / "report.json"
            staged = root / "replacement.json"
            staged.write_bytes(path.read_bytes())
            os.replace(staged, path)
        else:
            text = (root / R2_TABLE).read_text()
            _r2_put(
                root,
                R2_TABLE,
                (text + "\n" if change == "revision" else text.replace("mode: import", "mode: dual")).encode(),
            )
        return row

    monkeypatch.setattr(cu, "check_desktop_orphans", changed)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2 and len(originals) == 1, cu.render(report)
    assert _r2_check(report, "current-snapshot")["status"] == "NOT_CHECKED"
    assert len(calls) == (2 if change == "w-identity" else 1)
    assert R2_DISCLAIMER not in cu.render(report)


def test_r2_old_pinned_snapshot_and_byte_identical_transfer_are_not_latest_ever_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    package = _r2_package(tmp_path)
    token, _, _ = _r2_seal(package, monkeypatch)
    old = tmp_path / "transfer/Book"
    shutil.copytree(package, old)
    other = _r2_package(tmp_path / "other")
    _r2_put(other, R2_TABLE, (other / R2_TABLE).read_bytes() + b"\n")
    new, _, _ = _r2_seal(other, monkeypatch)
    assert new != token
    refused = cu.run_all(other, receipt_sha256=token)
    assert _r2_check(refused, "finalized")["code"] == "FINAL_RECEIPT_MISMATCH"
    accepted = cu.run_all(old, receipt_sha256=token)
    assert accepted["exit_code"] == 0, cu.render(accepted)
    assert R2_DISCLAIMER in cu.render(accepted)


def test_r2_obsolete_completion_escape_hatch_is_absent(tmp_path: Path, r2_gate_runtime: list) -> None:
    package = _r2_package(tmp_path)
    source = (REPO_ROOT / "scripts/check_unit.py").read_text(encoding="utf-8")
    forbidden = "CLAIMED" + "_ONLY"
    assert forbidden not in source and ("claimed" + "_only_checks") not in source
    assert forbidden not in cu.render(cu.run_all(package))


def test_r2_json_output_cannot_invalidate_the_snapshot_it_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    package = _r2_package(tmp_path)
    token, _, _ = _r2_seal(package, monkeypatch)
    original = (package / "package-manifest.json").read_bytes()
    with pytest.raises(SystemExit) as error:
        cu.main(
            [
                str(package),
                "--scope",
                "all",
                "--receipt-sha256",
                token,
                "--json",
                str(package / "package-manifest.json"),
            ]
        )
    assert error.value.code == 2 and (package / "package-manifest.json").read_bytes() == original
    assert r2_gate_runtime == []


@pytest.mark.parametrize(
    "role",
    [
        f"assets/{UNIT_LUID}_Book.twb",
        "migration-spec.json",
        "migration-spec.schema.json",
        "data-access.json",
        "source-provenance.json",
        "report.json",
        "migration-brief.md",
    ],
)
def test_r2_immutable_roles_cannot_hide_behind_a_new_working_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, role: str
) -> None:
    package = _r2_package(tmp_path)
    _r2_put(package, role, (package / role).read_bytes() + b"\n")
    # For a changed asset, use an unverified judgement: the source-bound reference correctly no
    # longer matches. Receipt publication remains neutral; immutable digest admission still refuses.
    token, _, _ = _r2_seal(package, monkeypatch, review_status="unverified" if role.startswith("assets/") else "pass")
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2 and r2_gate_runtime == []
    assert _r2_check(report, "current-source-data")["code"] == "package_file_digest_mismatch"


@pytest.mark.parametrize("disposition", ["unadjudicated", "accepted", "stale"])
def test_r2_current_limitations_require_current_bound_adjudication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, disposition: str
) -> None:
    import iteration_receipt as receipt

    package = _r2_package(tmp_path)
    spec = json.loads((package / "migration-spec.json").read_bytes())
    limitation = {"item": "Executive", "issue": "Font substitution", "severity": "low", "stage": "report"}
    spec["limitations_encountered"] = [limitation]
    _r2_put(package, "migration-spec.json", spec)
    _r2_declare(package, "migration-spec.json")
    ref = {"pointer": "/limitations_encountered/0", "sha256": receipt.limitation_entry_sha256(limitation)}
    findings = [] if disposition == "unadjudicated" else [_r2_finding(status="accepted_limitation", limitation_ref=ref)]
    token, payload, _ = _r2_seal(package, monkeypatch, findings=findings)
    if disposition == "stale":
        payload["judgement"]["findings"][0]["limitation_ref"]["sha256"] = "f" * 64
        token = _r2_rewrite_receipt(package, payload)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == (0 if disposition == "accepted" else 2), cu.render(report)
    row = _r2_check(report, "iteration-findings")
    assert (
        row["code"]
        == {
            "accepted": "resolved_or_bound_limitations",
            "unadjudicated": "limitations_unadjudicated",
            "stale": "ACCEPTED_LIMITATION_UNBOUND",
        }[disposition]
    )


def test_r2_required_numeric_reason_survives_higher_priority_page_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    package = _r2_package(tmp_path, numeric="required")
    spec = json.loads((package / "migration-spec.json").read_bytes())
    spec["dashboards"].append({"id": "missing", "name": "Missing"})
    _r2_put(package, "migration-spec.json", spec)
    _r2_declare(package, "migration-spec.json")
    token, _, _ = _r2_seal(package, monkeypatch)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 4 and report["stopped_after"] == "page-parity"
    assert "CANNOT_ESTABLISH(NUMERIC)" in cu.render(report) and "numeric_required_unsupported" in cu.render(report)
    assert r2_gate_runtime == []


def test_r2_newly_accepted_coherent_snapshot_is_not_original_commissioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    package = _r2_package(tmp_path)
    old, _, _ = _r2_seal(package, monkeypatch)
    asset = f"assets/{UNIT_LUID}_Book.twb"
    _r2_put(package, asset, b"<workbook name='Book' revision='new'/>\n")
    provenance = json.loads((package / "source-provenance.json").read_bytes())
    provenance["inputs"][0]["input"]["sha256"] = _sha256(package / asset)
    _r2_put(package, "source-provenance.json", provenance)
    reference = json.loads((package / "reference/manifest.json").read_bytes())
    reference["source_workbook_sha256"] = _sha256(package / asset)
    _r2_put(package, "reference/manifest.json", reference)
    _r2_declare(package, asset, "source-provenance.json", "reference/manifest.json")
    refused = cu.run_all(package, receipt_sha256=old)
    assert refused["exit_code"] == 2
    # Explicit threat-model control: replace BOTH package authority/history and the caller's H.
    # There is deliberately no claim of original commissioning or authenticated/latest-ever history.
    shutil.rmtree(package / "validation")
    new, _, _ = _r2_seal(package, monkeypatch)
    assert old != new
    accepted = cu.run_all(package, receipt_sha256=new)
    assert accepted["exit_code"] == 0, cu.render(accepted)
    assert R2_DISCLAIMER in cu.render(accepted)


def test_r2_both_chain_reads_use_the_same_caller_pin_and_original_w(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    package = _r2_package(tmp_path)
    token, _, _ = _r2_seal(package, monkeypatch)
    chain_reader = cu.iteration_receipt.read_chain
    w_reader = cu.roles.read_current_source_data_handoff
    w_validator = cu.roles.validate_current_source_data_handoff
    chains, issued, validated = [], [], []

    def read_chain(root, supplied):
        chains.append((root, supplied))
        return chain_reader(root, supplied)

    def issue(root, **kwargs):
        code, handoff = w_reader(root, **kwargs)
        issued.append(handoff)
        return code, handoff

    def validate(root, handoff, **kwargs):
        validated.append(handoff)
        return w_validator(root, handoff, **kwargs)

    monkeypatch.setattr(cu.iteration_receipt, "read_chain", read_chain)
    monkeypatch.setattr(cu.roles, "read_current_source_data_handoff", issue)
    monkeypatch.setattr(cu.roles, "validate_current_source_data_handoff", validate)
    assert cu.run_all(package, receipt_sha256=token)["exit_code"] == 0
    assert chains == [(package, token), (package, token)]
    assert len(issued) == 1 and len(validated) == 2 and all(value is issued[0] for value in validated)


@pytest.mark.parametrize("change", ["upper", "leading", "trailing", "prefix"])
def test_r2_even_the_correct_token_cannot_be_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, change: str
) -> None:
    package = _r2_package(tmp_path)
    token, _, _ = _r2_seal(package, monkeypatch)
    supplied = {"upper": token.upper(), "leading": " " + token, "trailing": token + "\n", "prefix": "sha256:" + token}[
        change
    ]
    assert supplied != token
    reads = []
    reader = cu.iteration_receipt.read_chain

    def counted(root, value):
        reads.append(value)
        return reader(root, value)

    monkeypatch.setattr(cu.iteration_receipt, "read_chain", counted)
    report = cu.run_all(package, receipt_sha256=supplied)
    assert report["exit_code"] == 2 and _r2_check(report, "finalized")["code"] == "receipt_token_invalid"
    assert reads == [] and r2_gate_runtime == []


def test_r2_import_category_never_replaces_actual_connection_fidelity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    package = _r2_package(tmp_path)
    bare = '\tpartition Sales = m\n\t\tmode: import\n\t\tsource =\n\t\t\tSql.Database("source.example", "db")\n'
    _r2_put(package, R2_TABLE, (package / R2_TABLE).read_text().replace(R2_SQL, bare).encode())
    token, _, _ = _r2_seal(package, monkeypatch)
    report = cu.run_all(package, receipt_sha256=token)
    assert _r2_check(report, "model-class")["status"] == "PASS"
    assert _r2_check(report, "connection-fidelity")["status"] == "NOT_CHECKED"
    assert report["exit_code"] == 2 and _r2_check(report, "finalized")["status"] == "NOT_CHECKED"


def test_r2_terminal_fold_does_not_restart_mutable_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    package = _r2_package(tmp_path)
    token, _, _ = _r2_seal(package, monkeypatch)
    monkeypatch.setattr(cu, "inspect_brownfield", lambda *_: pytest.fail("discovery after pinned authority"))
    assert cu.run_all(package, receipt_sha256=token)["exit_code"] == 0


@pytest.mark.parametrize("slot", ["second-page", "second-visual", "last-visual"])
def test_r2_one_nonpass_judgement_cannot_hide_among_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, slot: str
) -> None:
    package = _r2_package(tmp_path)
    _r2_expand_report(package)
    _, final, _ = _r2_seal(package, monkeypatch)
    pages = final["judgement"]["pages"]
    if slot == "second-page":
        pages[1]["whole_page_status"] = "unverified"
    else:
        pages[0 if slot == "second-visual" else 1]["visual_results"][1 if slot == "second-visual" else 0]["status"] = (
            "unverified"
        )
    token = _r2_rewrite_receipt(package, final)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2 and _r2_check(report, "visual-comparison-done")["status"] == "NOT_CHECKED"
    assert _r2_check(report, "visual-layer-done")["status"] == "PASS"


def test_r2_real_subset_receipt_cannot_complete_an_entire_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list
) -> None:
    import iteration_receipt as receipt

    package = _r2_package(tmp_path)
    _r2_expand_report(package)
    token, final, _ = _r2_seal(package, monkeypatch, page_ids=frozenset({"p1"}))
    assert final["mode"] == "triage" and final["generated"]["scope"] == "subset"
    assert len(receipt.read_chain(package, token)) == 1
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2
    assert _r2_check(report, "finalized")["code"] == "final_v3_all_pages_sign_off_required"
    assert r2_gate_runtime == []


@pytest.mark.parametrize("change", ["datasource", "extra-model", "extra-report"])
def test_r2_only_one_owned_workbook_target_can_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r2_gate_runtime: list, change: str
) -> None:
    package = _r2_package(tmp_path)
    if change == "datasource":
        manifest = json.loads((package / "package-manifest.json").read_bytes())
        manifest["kind"] = "datasource"
        _r2_put(package, "package-manifest.json", manifest)
    else:
        (package / "fabric" / ("Other.SemanticModel" if change == "extra-model" else "Other.Report")).mkdir()
    token, _, _ = _r2_seal(package, monkeypatch)
    report = cu.run_all(package, receipt_sha256=token)
    assert report["exit_code"] == 2 and _r2_check(report, "finalized")["code"] == "working_topology_unsupported"
    assert r2_gate_runtime == []
