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
import shutil
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_unit as cu  # noqa: E402  # pylint: disable=wrong-import-position
import check_field_bindings  # noqa: E402  # pylint: disable=wrong-import-position
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


def _write_spec(unit: Path, names: list[str]) -> None:
    """A dashboards-only migration spec, with the schema-required empty `worksheets` array."""
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "migration-spec.json").write_text(
        json.dumps(
            {
                "dashboards": [{"id": f"dash.{index}", "name": name} for index, name in enumerate(names)],
                "worksheets": [],
            }
        ),
        encoding="utf-8",
    )


def _write_report(unit: Path, names: list[str], *, visuals: int = 1) -> Path:
    """A PBIR report with `visuals` visual.json files per page (a real page has at least one)."""
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
        _write_visuals(page, visuals)
    (pages / "pages.json").write_text(json.dumps({"pageOrder": order}), encoding="utf-8")
    return report


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


def test_engine_declared_drop_is_explained_and_does_not_fail_parity(tmp_path: Path) -> None:
    """Kills: failing a unit for a page the engine PROVED produced no visual (tier 'empty')."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(
        tmp_path,
        [
            {"worksheet": "A", "visual_type": "bar", "status": "rebuilt", "tier": "rebuilt", "reason": None},
            _empty_row("B", "manual attention required: mark class 'Bar' / shelf layout not supported"),
        ],
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PASS
    assert [page["name"] for page in parity["dropped_explained"]] == ["B"]
    assert parity["dropped_explained"][0]["evidence_tier"] == "empty"
    assert parity["dropped_unexplained"] == []
    assert parity["effective_expected_count"] == 1


def test_a_degraded_warning_asserts_a_rendered_visual_and_excuses_nothing(tmp_path: Path) -> None:
    """Kills: letting any warned row excuse a missing page.

    migrate_estate._fidelity_tier defines 'degraded' as "a rendered visual whose warning is a genuine
    degradation" - the OPPOSITE of non-emission - and 'evidence: emitted+linted' appears on 45
    empty-tier rows in the same estate run, so neither field can decide this.
    """
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

    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED
    assert parity["dropped_explained"] == []
    assert [page["name"] for page in parity["dropped_unexplained"]] == ["B"]
    assert "does not assert non-emission" in parity["dropped_unexplained"][0]["why_unexplained"]


def test_a_row_with_no_tier_at_all_leaves_the_drop_unexplained(tmp_path: Path) -> None:
    """An engine too old to publish `tier` is 'cannot tell', not 'intentionally dropped'."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(
        tmp_path,
        [
            {
                "worksheet": "B",
                "visual_type": "unsupported",
                "status": "warned",
                "reason": "manual attention required: unsupported",
            }
        ],
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED
    assert [page["name"] for page in parity["dropped_unexplained"]] == ["B"]


def test_another_workbooks_warning_cannot_excuse_this_units_missing_page(tmp_path: Path) -> None:
    """Kills: borrowing an excuse across workbook boundaries.

    This unit ships Book.Report, so only a handover slice for 'Book' may explain its pages. Without
    the binding, a tier-'empty' row from 'Different Workbook' silently removed a genuinely missing
    page from both the parity and the oracle denominator.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(tmp_path, [_empty_row("B")], workbook_name="Different Workbook")

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED
    assert parity["dropped_explained"] == []
    assert parity["drop_explanations"]["bound_workbooks"] == []
    assert parity["drop_explanations"]["unbound_workbooks"] == ["Different Workbook"]
    assert "Different Workbook" in parity["dropped_unexplained"][0]["why_unexplained"]


def test_drop_reasons_come_from_viz_fidelity_not_pbip_warnings(tmp_path: Path) -> None:
    """Kills: attributing drops from pbip_warnings[], which carries no page name at all.

    pbip_warnings[] is a flat list of prefixed strings with no scope/name field, so a warning there
    cannot be mapped back to a page. Measured on a real 2.339.0 estate run: 193 pbip_warnings
    entries across 43 workbooks explained ZERO of the 23 absent candidates.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(
        tmp_path,
        [{"worksheet": "A", "visual_type": "bar", "status": "rebuilt", "tier": "rebuilt", "reason": None}],
        pbip_warnings=["manual attention required: mark class 'Bar' / shelf layout not supported -> no visual emitted"],
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED
    assert [page["name"] for page in parity["dropped_unexplained"]] == ["B"]
    assert parity["dropped_explained"] == []


def test_warned_but_emitted_page_is_not_treated_as_a_drop(tmp_path: Path) -> None:
    """Kills: subtracting every explained-looking row, which hides a genuinely missing page.

    'A' carries a tier-'empty' row here AND is emitted with a visual. Only a candidate ABSENT from
    the rendered pages can be an explained drop, so A must not be subtracted while B still fails.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_viz_fidelity_handover(tmp_path, [_empty_row("A")])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED
    assert parity["dropped_explained"] == [], "A was emitted, so its row explains no drop"
    assert [page["name"] for page in parity["dropped_unexplained"]] == ["B"]


def test_missing_handover_says_drop_reasons_were_unavailable(tmp_path: Path) -> None:
    """'No declared reason' and 'could not read the declarations' are different states."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED
    assert parity["drop_explanations"]["available"] is False
    assert parity["drop_explanations"]["source"] == "handover viz_fidelity[]"
    assert "no handover slice was readable" in parity["dropped_unexplained"][0]["why_unexplained"]


def test_malformed_required_collection_is_unassessable_not_a_smaller_denominator(tmp_path: Path) -> None:
    """Kills: silently skipping a malformed required array and trusting what is left.

    docs/migration-spec.schema.json requires both as arrays. A spec with one valid dashboard and a
    `worksheets` OBJECT used to yield a one-page expected set that page parity and oracle coverage
    both graded PASS, oracle at grade=validation-grade - the circular denominator one layer earlier.
    """
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
    """A page entry that cannot be identified at all must refuse the whole spec, not shrink it.

    ``name`` -> ``title`` -> ``id`` is a deliberate fallback: an entry with only an ``id`` still
    OCCUPIES a slot in the denominator, so keeping it is the conservative choice (it will simply
    never match a page and fail closed). An entry with none of the three is different - the old code
    skipped it, and skipping shrinks the denominator, which is the fail-open shape.
    """
    (tmp_path / "migration-spec.json").write_text(
        json.dumps({"dashboards": [{"id": "dash.a", "name": "A"}, {"size": {}}], "worksheets": []}),
        encoding="utf-8",
    )
    _write_report(tmp_path, ["A"])

    assert cu.expected_pages(tmp_path) is None
    assert "entry #2 has no usable name" in cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))["detail"]


def test_an_id_only_spec_entry_stays_in_the_denominator(tmp_path: Path) -> None:
    """The other half: an identifiable-but-unnamed entry is kept, so the count cannot silently shrink."""
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
    """Kills: counting a page that renders nothing as evidence the candidate was rebuilt.

    Renaming the engine's empty crash-guard page to an expected page's title used to PASS parity and
    report oracle grade=validation-grade for a report that draws nothing.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_report(tmp_path, ["A"], visuals=0)

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED
    assert parity["emitted_count"] == 0
    assert [page["name"] for page in parity["blank_pages"]] == ["A"]
    assert "render nothing" in parity["detail"]


def test_oracle_coverage_without_an_expected_set_is_blocking_not_a_pass(tmp_path: Path) -> None:
    """Kills the circular denominator: ``expected_pages(target) or actual_pages(target)``.

    With no migration-spec.json the old code graded the artifact against ITSELF - every emitted page
    matched its own reference row and coverage reported a perfect PASS regardless of what was
    missing. There is no fallback now.
    """
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


def test_oracle_coverage_excludes_engine_declared_drops_from_the_denominator(tmp_path: Path) -> None:
    """A page the engine proved produced nothing has nothing to hold against a reference."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B")])
    _write_report(tmp_path, ["A"])
    _write_reference_manifest(tmp_path, ["A"])
    _write_viz_fidelity_handover(
        tmp_path, [_empty_row("B", "manual attention required: empty worksheet -> nothing to rebuild")]
    )

    oracle = cu.check_oracle_coverage(tmp_path, None, None)

    assert oracle["status"] == cu.STATUS_PASS
    assert oracle["pages"] == 1
    assert [page["name"] for page in oracle["excluded_explained_drops"]] == ["B"]


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


def test_engine_crash_guard_placeholder_is_not_an_extra_page(tmp_path: Path) -> None:
    """Kills: calling the engine's zero-page crash-guard page an unaccounted-for extra.

    twb_to_pbir.py:14719-14727 ships ONE synthetic VISUAL-LESS page when every candidate was
    dropped, because a PBIR with an empty pageOrder crashes Power BI Desktop on open. Measured: 2 of
    44 workbooks in a real 2.339.0 estate run do this, and both failed page parity without this.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "Sheet 1")])
    _write_placeholder_page(tmp_path, visuals=0)
    _write_viz_fidelity_handover(
        tmp_path, [_empty_row("Sheet 1", "manual attention required: mark class 'Shape' not supported")]
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["status"] == cu.STATUS_PASS
    assert [page["name"] for page in parity["engine_placeholder_pages"]] == ["No visuals rebuilt"]
    assert parity["blank_pages"] == [], "the declared placeholder is not an unexplained blank page"
    assert parity["unexempted_extra"] == []


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


def test_a_real_page_titled_like_the_placeholder_is_still_a_page(tmp_path: Path) -> None:
    """The id prefix is load-bearing on its own: a zero-visual page with an ORDINARY id is a blank page.

    The zero-visual clause cannot mask this one - the page here has no visuals, so only the id
    distinguishes "the engine's declared crash-guard page" from "a page that ships and renders
    nothing". Getting it wrong hides the blank page instead of reporting it.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_report(tmp_path, ["No visuals rebuilt"], visuals=0)

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["engine_placeholder_pages"] == [], "p1 is an ordinary page id, not page-empty*"
    assert [page["name"] for page in parity["blank_pages"]] == ["No visuals rebuilt"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_blank_page_alone_fails_the_gate_even_when_the_counts_balance(tmp_path: Path) -> None:
    """Kills: reporting a page that renders nothing without letting it fail anything.

    Deliberately constructed so a blank page is the ONLY problem: one candidate, one rendered page
    matching it, and one extra zero-visual page that keeps the missing/extra counts at zero.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    pages = tmp_path / "fabric" / "Book.Report" / "definition" / "pages"
    _write_report(tmp_path, ["A"])
    blank = pages / "p2"
    blank.mkdir()
    (blank / "page.json").write_text(json.dumps({"name": "p2", "displayName": "Extra"}), encoding="utf-8")
    (pages / "pages.json").write_text(json.dumps({"pageOrder": ["p1", "p2"]}), encoding="utf-8")

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["unexempted_missing"] == [], "the counts balance; only the blank page is wrong"
    assert parity["unexempted_extra"] == []
    assert [page["name"] for page in parity["blank_pages"]] == ["Extra"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_placeholder_id_holding_real_visuals_is_a_rebuilt_page(tmp_path: Path) -> None:
    """Kills: refusing a page-empty* page that an author actually filled with visuals.

    The placeholder is defined by having NO visuals. Classifying by id+name alone discarded a page
    carrying real content from the emitted count and produced a false PRECONDITION_FAILED.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_placeholder_page(tmp_path, visuals=3)

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["engine_placeholder_pages"] == [], "it has 3 visuals, so it is not a placeholder"
    assert parity["emitted_count"] == 1
    assert parity["status"] == cu.STATUS_PASS


def test_a_blank_page_that_is_not_the_engine_placeholder_is_reported(tmp_path: Path) -> None:
    """A zero-visual page nobody declared renders nothing and is a finding, not a silent pass."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_placeholder_page(tmp_path, visuals=0, display="A")

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert parity["engine_placeholder_pages"] == [], "a page-empty* id renamed to 'A' is not the placeholder"
    assert [page["name"] for page in parity["blank_pages"]] == ["A"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_a_missing_page_is_named_by_content_not_by_position(tmp_path: Path) -> None:
    """Kills: naming a shortfall by slicing the tail of the expected list.

    'A' is absent and 'C' is present. A positional tail slice names 'C' - so every downstream
    consumer, including exemption matching, is pointed at the wrong page. No exemption is involved
    here, so this pins the attribution rule on its own.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B"), ("ws.c", "C")])
    _write_report(tmp_path, ["B", "C"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [page["name"] for page in parity["unexempted_missing"]] == ["A"]
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_an_exemption_excuses_the_page_it_names_and_no_other(tmp_path: Path) -> None:
    """Kills: a signed exemption absorbing a DIFFERENT page's absence.

    Only 'A' is genuinely absent; 'C' is present. Applying the exemption unconditionally shrank the
    expected count by one, which balanced the books and hid A entirely. The exemption is now recorded
    as stale instead of being load-bearing, and A is named.
    """
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A"), ("ws.b", "B"), ("ws.c", "C")])
    _write_report(tmp_path, ["B", "C"])
    (tmp_path / cu.EXEMPTIONS_FILE).write_text(
        json.dumps({"exemptions": [{"check": "page-parity", "item": "C", "reason": "merged", "decided_by": "review"}]}),
        encoding="utf-8",
    )

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [page["name"] for page in parity["unexempted_missing"]] == ["A"], "C is present; it cannot be missing"
    assert [page["name"] for page in parity["stale_exemptions"]] == ["C"]
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

    assert parity["unexempted_missing"] == []
    assert parity["status"] == cu.STATUS_PASS


def test_an_extra_page_is_named_by_content_not_by_position(tmp_path: Path) -> None:
    """An emitted page with no Tableau counterpart is identified by NAME, so its exemption can match."""
    _write_full_spec(tmp_path, dashboards=[], worksheets=[("ws.a", "A")])
    _write_report(tmp_path, ["Bonus Page", "A"])

    parity = cu.check_page_parity(tmp_path, cu.load_exemptions(tmp_path))

    assert [page["name"] for page in parity["unexempted_extra"]] == ["Bonus Page"], "'A' has a counterpart"
    assert parity["status"] == cu.STATUS_PRECONDITION_FAILED


def test_page_count_mismatch_is_a_precondition_and_stops_before_oracle(tmp_path: Path) -> None:
    """Kills: treating missing pages as just another row and continuing into noisy page checks."""
    _write_spec(tmp_path, ["A", "B"])
    _write_report(tmp_path, ["A"])

    report = cu.run_all(tmp_path)

    assert report["status"] == cu.STATUS_PRECONDITION_FAILED
    assert report["exit_code"] == cu.EXIT_PRECONDITION_FAILED
    assert report["stopped_after"] == "page-parity"
    assert [check["id"] for check in report["checks"]] == ["page-parity"]


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

    # not_checked_external is a third bucket (issue #317) so a model deferred to its datasource unit
    # stops inflating missing_input; here nothing is external, so the bucket is 0.
    assert rendered.splitlines()[-1] == (
        "SUMMARY: blockers=1; compromises=0; compromises_not_evaluated=0; findings_by_owner=model=1; "
        "not_checked_structural=1; not_checked_external=0; not_checked_missing_input=0; ladder=FINDINGS exit=1"
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
    assert _normalize_unit_stdout(result.stdout, target) == golden.read_text(encoding="utf-8")


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
    hard-coded, so the test holds at any temp-directory depth (measured: 107 characters on this
    Windows runner, ~50 on Linux) without encoding the scanner's ceiling arithmetic. Overshooting the
    budget by one keeps the binding path one unit over its OWN ceiling, so nothing created here needs
    Windows long-path support.
    """
    short_root = tmp_path / "s"
    pad = _pad_for_headroom(short_root, tmp_path, headroom=12)
    if pad is None:
        pytest.skip("temp directory too deep to build a tree with controlled headroom")

    short = _scan_paths(_deep_tree_with_pad(short_root, pad), tmp_path / "short.json")
    assert short["status"] == "ok", short
    budget, root_length = short["root_budget"], short["root_length"]
    assert budget >= root_length

    long_root = tmp_path / ("s" + "x" * (budget - root_length + 1))
    after = _scan_paths(_deep_tree_with_pad(long_root, pad), tmp_path / "long.json")

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


def _pad_for_headroom(root: Path, scratch: Path, headroom: int) -> int | None:
    """Calibrate the padding that leaves exactly ``headroom`` characters of root budget.

    ``root_budget`` falls one-for-one with the padded component, so one measured probe fixes the
    constant without this test knowing the scanner's ceilings or path layout.
    """
    probe = _scan_paths(_deep_tree_with_pad(root, 1), scratch / "probe.json")
    pad = probe["root_budget"] + 1 - probe["root_length"] - headroom
    shutil.rmtree(root, ignore_errors=True)
    return pad if 1 <= pad <= 200 else None


def _scan_paths(unit: Path, json_path: Path) -> dict:
    """Run the real scanner and return its machine-readable report."""
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_path_ceiling.py"),
            str(unit),
            "--json",
            str(json_path),
            "--quiet",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(json_path.read_text(encoding="utf-8"))
