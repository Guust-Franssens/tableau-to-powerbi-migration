"""Tests for the reference-readiness ENTRY gate (issue #421).

The load-bearing property is **fail closed**: `unknown` and `missing` must both be distinct from
`ready`, and neither may exit 0. A readiness gate that green-lights on absent evidence is worse than
no gate, because it launches an agent to build confidently against nothing.

The headline regression is `test_a_worksheet_render_does_not_make_a_dashboard_page_ready`. It is
paired with `test_a_worksheet_render_does_satisfy_a_worksheet_page` on purpose: without the second
test, the first would also pass if the matcher simply never matched anything, and a test that cannot
distinguish "correctly refused" from "broken" is not coverage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_reference_readiness as crr  # noqa: E402  # pylint: disable=wrong-import-position

# Page ids observed in the real engine bundle
# `_runs/406-meridian-smoke-2-339-0-20260901/bundle/pbip/Meridian Revenue by Region/...`,
# built by engine 2.339.0. They pin `engine_page_id` against the engine, not against itself.
MERIDIAN_PAGE_IDS = {
    "Revenue by Region": "page-ws-Revenuebb7d27f78",
    "Revenue Trend": "page-ws-RevenueTfd9cb617",
    "Regional Share": "page-ws-Regional05286155",
}


def write_workbook(path: Path, *, worksheets: list[str], dashboards: dict[str, list[str]] | None = None) -> Path:
    """A minimal `.twb`. ``dashboards`` maps a dashboard name to the worksheets placed on it."""
    ws_xml = "".join(f"<worksheet name='{name}' />" for name in worksheets)
    db_xml = ""
    for db_name, placed in (dashboards or {}).items():
        zones = "".join(f"<zone name='{name}' />" for name in placed)
        db_xml += f"<dashboard name='{db_name}'><zones><zone>{zones}</zone></zones></dashboard>"
    path.write_text(
        f"<?xml version='1.0'?><workbook><worksheets>{ws_xml}</worksheets><dashboards>{db_xml}</dashboards></workbook>",
        encoding="utf-8",
    )
    return path


def write_report(root: Path, unit: str, page_ids: list[str]) -> Path:
    """A PBIR report shipping the given page ids under ``<root>/pbip/<unit>/<unit>.Report``."""
    report = root / "pbip" / unit / f"{unit}.Report"
    pages = report / "definition" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "pages.json").write_text(json.dumps({"pageOrder": page_ids}), encoding="utf-8")
    for page_id in page_ids:
        page_dir = pages / page_id
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "page.json").write_text(json.dumps({"name": page_id, "displayName": page_id}), encoding="utf-8")
    return report


def write_engine_report(root: Path, *, workbooks: list[str], datasources: list[str] | None = None) -> None:
    """The engine's `report.json`, which is what classifies a unit as workbook vs datasource."""
    (root / "report.json").write_text(
        json.dumps(
            {
                "workbooks": [{"name": name} for name in workbooks],
                "datasources": [{"name": name} for name in datasources or []],
            }
        ),
        encoding="utf-8",
    )


def write_handover(root: Path, unit: str, *, source_id: str, viz_fidelity: list[dict] | None = None) -> None:
    """The engine's per-workbook handover slice, carrying `source_id` and `viz_fidelity[]`."""
    handover = root / "handover"
    handover.mkdir(parents=True, exist_ok=True)
    (handover / f"{unit}.json").write_text(
        json.dumps({"workbook": {"source_id": source_id, "viz_fidelity": viz_fidelity or []}}),
        encoding="utf-8",
    )


def write_reference(root: Path, entries: list[tuple[str, str, list[str]]]) -> Path:
    """A `reference/manifest.json`. Each entry is ``(name, provider, capabilities)``.

    Note the manifest's top-level key is `dashboards` even for WORKSHEET renders - that is exactly
    what `capture_tableau_reference.py:199` does for `embedded_thumbnail`, and is the reason the
    key cannot be treated as evidence of scope.
    """
    reference = root / "reference"
    reference.mkdir(parents=True, exist_ok=True)
    dashboards = []
    for index, (name, provider, capabilities) in enumerate(entries):
        image = f"shot-{index}.png"
        (reference / image).write_bytes(b"\x89PNG\r\n\x1a\n")
        dashboards.append(
            {
                "name": name,
                "states": [
                    {
                        "state_slug": "default",
                        "image": image,
                        "provider": provider,
                        "capabilities": capabilities,
                        "numeric_oracle": None,
                    }
                ],
            }
        )
    (reference / "manifest.json").write_text(json.dumps({"dashboards": dashboards}), encoding="utf-8")
    return reference


def write_oracle(root: Path, views: list[dict]) -> Path:
    """An `_oracle/oracle-manifest.json`. Each view dict may carry `view_type` (PR #422)."""
    oracle = root / "_oracle"
    images = oracle / "images"
    images.mkdir(parents=True, exist_ok=True)
    records = []
    for index, view in enumerate(views):
        image = f"images/view-{index}.png"
        (oracle / image).write_bytes(b"\x89PNG\r\n\x1a\n")
        records.append({**view, "image": {"status": "ok", "path": image}})
    (oracle / "oracle-manifest.json").write_text(
        json.dumps({"view_count": len(records), "views": records}), encoding="utf-8"
    )
    return oracle


@pytest.fixture(name="bundle")
def bundle_fixture(tmp_path: Path) -> Path:
    """An engine-bundle-shaped root with an assets/ sibling, as `run_estate.py` produces."""
    root = tmp_path / "bundle"
    root.mkdir()
    (tmp_path / "assets").mkdir()
    return root


def test_the_status_and_exit_vocabulary_is_pinned_to_its_literal_values() -> None:
    """Pin every constant the rest of this file compares against.

    Without this the suite is vacuous in one direction: `assert main(...) == crr.EXIT_CANNOT_ESTABLISH`
    compares the code's answer against the code's own constant, so redefining the constant to 0
    changes BOTH sides and the assertion still holds. One pin catches every such mutation, and lets
    the other tests keep reading in names rather than magic numbers.

    The 0/1/2/3 values are `check_connection_fidelity.py:160-163`'s, deliberately shared across gates.
    """
    assert (crr.EXIT_OK, crr.EXIT_FINDINGS, crr.EXIT_USAGE, crr.EXIT_CANNOT_ESTABLISH) == (0, 1, 2, 3)
    assert (crr.READY, crr.BLIND, crr.UNVERIFIABLE) == ("ready", "blind", "unverifiable")
    assert (crr.STATUS_READY, crr.STATUS_FINDINGS) == ("READY", "FINDINGS")
    assert (crr.STATUS_NOT_APPLICABLE, crr.STATUS_CANNOT_ESTABLISH) == ("NOT_APPLICABLE", "CANNOT_ESTABLISH")
    assert (crr.KIND_DASHBOARD, crr.KIND_WORKSHEET, crr.KIND_UNKNOWN) == ("dashboard", "worksheet", "unknown")
    assert (crr.PAGE_EMITTED, crr.PAGE_DROPPED_EXPLAINED, crr.PAGE_DROPPED_UNEXPLAINED) == (
        "emitted",
        "dropped_explained",
        "dropped_unexplained",
    )
    assert crr.GRADE_VALIDATION == "validation-grade"


def test_a_worksheet_scope_can_never_satisfy_a_dashboard_page() -> None:
    """The scope join itself, isolated from any fixture.

    Asserted directly on `match_evidence` so the property is pinned even if a future refactor
    changes how bundles are walked - and so this cannot pass because a fixture never reached the
    branch under test.
    """
    dashboard = crr.SourceObject(name="Ops", kind="dashboard")
    worksheet_render = crr.Evidence(
        name="Ops", kind="worksheet", grade="layout_grade", origin="reference", provider="embedded_thumbnail"
    )
    match, name_only = crr.match_evidence(dashboard, [worksheet_render])
    assert match is None
    assert name_only == [worksheet_render]


def build_unit(  # pylint: disable=too-many-arguments
    bundle: Path,
    unit: str,
    *,
    worksheets: list[str],
    dashboards: dict[str, list[str]] | None = None,
    page_ids: list[str] | None = None,
    viz_fidelity: list[dict] | None = None,
) -> None:
    """Wire a complete workbook unit: source asset, engine report.json, handover and PBIR pages."""
    source = write_workbook(bundle.parent / "assets" / f"{unit}.twb", worksheets=worksheets, dashboards=dashboards)
    write_engine_report(bundle, workbooks=[unit])
    write_handover(bundle, unit, source_id=str(source), viz_fidelity=viz_fidelity)
    if page_ids is None:
        objects = crr.source_objects(source) or []
        page_ids = [obj.page_id for obj in objects]
    write_report(bundle, unit, page_ids)


# --------------------------------------------------------------------------------------------
# Identity: the engine's own page naming, reproduced
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("worksheet", "page_id"), sorted(MERIDIAN_PAGE_IDS.items()))
def test_engine_page_id_reproduces_the_real_engine_output(worksheet: str, page_id: str) -> None:
    """Pinned against page ids a real 2.339.0 bundle actually contains."""
    assert crr.engine_page_id(f"page-ws-{worksheet}") == page_id


def test_a_dashboard_and_a_same_named_worksheet_get_different_page_ids() -> None:
    """The identity join that a name slug cannot make.

    In Tableau a dashboard routinely shares its name with its principal worksheet. `check_unit.py`'s
    `_slug` collapses them; the engine's md5-over-the-prefixed-string does not.
    """
    as_worksheet = crr.SourceObject(name="Regional Share", kind=crr.KIND_WORKSHEET).page_id
    as_dashboard = crr.SourceObject(name="Regional Share", kind=crr.KIND_DASHBOARD).page_id
    assert as_worksheet == "page-ws-Regional05286155"
    assert as_dashboard != as_worksheet


# --------------------------------------------------------------------------------------------
# Question 1: completeness, against the engine's real rule
# --------------------------------------------------------------------------------------------


def test_orphan_worksheets_are_expected_pages(tmp_path: Path) -> None:
    """0 dashboards + 3 worksheets = 3 pages - the shape `check_unit.expected_pages` gets wrong."""
    source = write_workbook(tmp_path / "wb.twb", worksheets=list(MERIDIAN_PAGE_IDS))
    objects = crr.source_objects(source)
    assert objects is not None
    assert {obj.name for obj in objects} == set(MERIDIAN_PAGE_IDS)
    assert {obj.kind for obj in objects} == {crr.KIND_WORKSHEET}


def test_a_worksheet_placed_on_a_dashboard_is_not_an_orphan(tmp_path: Path) -> None:
    """A worksheet laid onto a dashboard gets no page of its own - the engine's `placed` set."""
    source = write_workbook(
        tmp_path / "wb.twb",
        worksheets=["Placed", "Loose"],
        dashboards={"Main": ["Placed"]},
    )
    objects = crr.source_objects(source)
    assert objects is not None
    assert {(obj.name, obj.kind) for obj in objects} == {
        ("Main", crr.KIND_DASHBOARD),
        ("Loose", crr.KIND_WORKSHEET),
    }


def test_an_unreadable_source_is_none_not_an_empty_expectation(tmp_path: Path) -> None:
    """`None` and `[]` must stay distinct all the way to the exit code."""
    broken = tmp_path / "broken.twb"
    broken.write_text("<workbook><unclosed>", encoding="utf-8")
    assert crr.source_objects(broken) is None


def test_a_page_the_engine_dropped_with_a_reason_is_accounted_for(bundle: Path) -> None:
    """`dropped_explained` must not read as a conversion gap - that is the cry-wolf direction."""
    build_unit(
        bundle,
        "WB",
        worksheets=["Kept", "Dropped"],
        page_ids=[crr.SourceObject(name="Kept", kind=crr.KIND_WORKSHEET).page_id],
        viz_fidelity=[
            {
                "worksheet": "Dropped",
                "status": "warned",
                "reason": "manual attention required: unsupported visual type",
            }
        ],
    )
    report = crr.scan(bundle)
    rows = {page["source_object"]: page for page in report["units"][0]["pages"]}
    assert rows["Dropped"]["page_status"] == crr.PAGE_DROPPED_EXPLAINED
    assert report["pages_dropped_unexplained"] == 0
    assert report["pages_dropped_explained"] == 1


def test_a_page_the_engine_dropped_silently_is_a_finding(bundle: Path) -> None:
    """No engine explanation means a real conversion gap, and it must not exit 0."""
    build_unit(
        bundle,
        "WB",
        worksheets=["Kept", "Vanished"],
        page_ids=[crr.SourceObject(name="Kept", kind=crr.KIND_WORKSHEET).page_id],
    )
    report = crr.scan(bundle)
    rows = {page["source_object"]: page for page in report["units"][0]["pages"]}
    assert rows["Vanished"]["page_status"] == crr.PAGE_DROPPED_UNEXPLAINED
    assert rows["Vanished"]["readiness"] == crr.BLIND
    assert report["status"] == crr.STATUS_FINDINGS
    assert crr.main([str(bundle), "--quiet"]) == crr.EXIT_FINDINGS


# --------------------------------------------------------------------------------------------
# Questions 2 and 3: evidence and grade, matched by IDENTITY not name
# --------------------------------------------------------------------------------------------


def test_a_worksheet_render_does_not_make_a_dashboard_page_ready(bundle: Path) -> None:
    """THE regression test (issue #421).

    A Tableau `<thumbnail>` is a WORKSHEET render (`extract_twb_thumbnails.py`), yet
    `capture_tableau_reference.py:199` files it under the manifest's `dashboards` key, where
    `check_unit.py`'s `_slug` match then lets it satisfy a same-named DASHBOARD page. The dashboard
    and its principal worksheet sharing a name is the normal case in Tableau, so this is not an edge
    case - it is the default one.
    """
    build_unit(bundle, "WB", worksheets=["Regional Share"], dashboards={"Regional Share": ["Regional Share"]})
    write_reference(bundle, [("Regional Share", "embedded_thumbnail", ["layout_grade"])])

    report = crr.scan(bundle)
    page = report["units"][0]["pages"][0]

    assert page["source_type"] == crr.KIND_DASHBOARD
    assert page["readiness"] != crr.READY
    assert page["readiness"] == crr.UNVERIFIABLE
    assert page["evidence"] == "unverifiable"
    assert "worksheet" in page["matched_by"]
    assert report["status"] == crr.STATUS_FINDINGS
    assert crr.main([str(bundle), "--quiet"]) == crr.EXIT_FINDINGS


def test_a_worksheet_render_does_satisfy_a_worksheet_page(bundle: Path) -> None:
    """Discriminating twin of the test above.

    Without this, the regression test would also pass if the matcher simply never matched anything.
    Same evidence, same provider, same name - only the page's source type differs.
    """
    build_unit(bundle, "WB", worksheets=["Regional Share"])
    write_reference(bundle, [("Regional Share", "embedded_thumbnail", ["layout_grade"])])

    report = crr.scan(bundle)
    page = report["units"][0]["pages"][0]

    assert page["source_type"] == crr.KIND_WORKSHEET
    assert page["readiness"] == crr.READY
    assert page["grade"] == "layout_grade"
    assert report["status"] == crr.STATUS_READY
    assert crr.main([str(bundle), "--quiet"]) == crr.EXIT_OK


def test_an_oracle_record_with_no_view_type_cannot_satisfy_a_page(bundle: Path) -> None:
    """PR #422's field absent = cannot establish, never "it could be either"."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_luid": "abc"}])

    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["readiness"] == crr.UNVERIFIABLE
    assert "unknown" in page["matched_by"]


def test_an_oracle_record_typed_unknown_cannot_satisfy_a_page(bundle: Path) -> None:
    """PR #422 fails closed to `unknown` when the Metadata API is disabled; so must this."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "unknown"}])

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == crr.UNVERIFIABLE


def test_an_oracle_record_typed_worksheet_satisfies_a_worksheet_page(bundle: Path) -> None:
    """The discriminating control for the two tests above."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "worksheet"}])

    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["readiness"] == crr.READY
    assert page["grade"] == crr.GRADE_ORACLE


def test_an_oracle_record_typed_worksheet_still_cannot_satisfy_a_dashboard_page(bundle: Path) -> None:
    """The scope join applies to the oracle route too, not only to `reference/`."""
    build_unit(bundle, "WB", worksheets=["Ops"], dashboards={"Ops": ["Ops"]})
    write_oracle(bundle, [{"view_name": "Ops", "view_type": "worksheet"}])

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == crr.UNVERIFIABLE


def test_a_page_with_no_evidence_at_all_is_blind_not_unverifiable(bundle: Path) -> None:
    """`blind` and `unverifiable` are different operator actions: capture one, or identify one."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["readiness"] == crr.BLIND
    assert page["evidence"] == "absent"


def test_validation_grade_is_reported_when_present(bundle: Path) -> None:
    """The one route to `validation_grade` today: an operator-asserted manual capture."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_reference(bundle, [("Revenue Trend", "embedded_thumbnail", ["layout_grade", "validation_grade"])])

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["grade"] == crr.GRADE_VALIDATION
    assert report["validation_grade_present"] is True
    assert crr.GRADE_CEILING_NOTE not in crr.render(report)


def test_the_grade_ceiling_is_stated_when_nothing_is_validation_grade(bundle: Path) -> None:
    """A READY verdict must not imply more evidence than exists."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_reference(bundle, [("Revenue Trend", "embedded_thumbnail", ["layout_grade"])])

    report = crr.scan(bundle)
    assert report["validation_grade_present"] is False
    assert crr.GRADE_CEILING_NOTE in crr.render(report)


def test_require_validation_grade_turns_layout_only_into_a_finding(bundle: Path) -> None:
    """The opt-in strict bar: layout/text evidence is enough to START, not to sign off."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_reference(bundle, [("Revenue Trend", "embedded_thumbnail", ["layout_grade"])])

    assert crr.scan(bundle)["status"] == crr.STATUS_READY
    assert crr.scan(bundle, require_validation_grade=True)["status"] == crr.STATUS_FINDINGS
    assert crr.main([str(bundle), "--quiet", "--require-validation-grade"]) == crr.EXIT_FINDINGS


# --------------------------------------------------------------------------------------------
# Fail-closed: nothing unassessable may collapse into the clean bucket
# --------------------------------------------------------------------------------------------


def test_an_unresolvable_source_cannot_establish_and_does_not_exit_zero(bundle: Path) -> None:
    """No source workbook = no expectation. That is exit 3, and it is NOT a pass."""
    write_engine_report(bundle, workbooks=["WB"])
    write_report(bundle, "WB", ["page-ws-anything"])

    report = crr.scan(bundle)
    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert crr.main([str(bundle), "--quiet"]) == crr.EXIT_CANNOT_ESTABLISH


def test_the_expectation_never_falls_back_to_the_pages_that_were_built(bundle: Path) -> None:
    """The circularity in `check_oracle_coverage:925`, refused.

    A workbook declaring no dashboards and no worksheets, beside a report that ships three pages,
    must NOT grade the artifact against itself and report three ready pages.
    """
    source = write_workbook(bundle.parent / "assets" / "WB.twb", worksheets=[])
    write_engine_report(bundle, workbooks=["WB"])
    write_handover(bundle, "WB", source_id=str(source))
    write_report(bundle, "WB", ["page1", "page2", "page3"])

    report = crr.scan(bundle)
    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert report["pages_expected"] == 0
    assert report["pages_ready"] == 0
    assert crr.main([str(bundle), "--quiet"]) == crr.EXIT_CANNOT_ESTABLISH


def test_a_datasource_only_unit_is_not_applicable(bundle: Path) -> None:
    """Legitimately reference-free work must not be blocked."""
    write_engine_report(bundle, workbooks=[], datasources=["Shared DS"])
    write_report(bundle, "Shared DS", ["page1"])

    report = crr.scan(bundle)
    assert report["status"] == crr.STATUS_NOT_APPLICABLE
    assert crr.main([str(bundle), "--quiet"]) == crr.EXIT_OK


def test_not_applicable_is_earned_from_the_engine_report_not_from_an_empty_page_list(bundle: Path) -> None:
    """A workbook unit that emitted nothing is NOT `NOT_APPLICABLE` - it is unassessable.

    This is the fail-open shape the gate refuses: 'I found no pages, so nothing applies'.
    """
    write_engine_report(bundle, workbooks=["WB"], datasources=["Shared DS"])
    write_report(bundle, "WB", [])

    report = crr.scan(bundle)
    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert report["units_not_applicable"] == 0


def test_an_empty_target_is_cannot_establish(tmp_path: Path) -> None:
    """An empty directory has nothing to measure, and that must never read as a pass."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert crr.scan(empty)["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert crr.main([str(empty), "--quiet"]) == crr.EXIT_CANNOT_ESTABLISH


def test_findings_outrank_cannot_establish_but_both_stay_visible(bundle: Path) -> None:
    """Neither count may hide the other; a fixed finding must still reveal the unassessable unit."""
    build_unit(bundle, "WB", worksheets=["Loose"])
    write_engine_report(bundle, workbooks=["WB", "Orphaned"])
    write_report(bundle, "Orphaned", ["page1"])

    report = crr.scan(bundle)
    assert report["status"] == crr.STATUS_FINDINGS
    assert report["units_cannot_establish"] == 1
    rendered = crr.render(report)
    assert crr.STATUS_CANNOT_ESTABLISH in rendered


def test_a_missing_path_is_a_usage_error_not_a_verdict(tmp_path: Path) -> None:
    """A bad path must exit 2, never produce a readiness opinion about nothing."""
    with pytest.raises(SystemExit) as excinfo:
        crr.main([str(tmp_path / "does-not-exist"), "--quiet"])
    assert excinfo.value.code == crr.EXIT_USAGE


def test_warn_only_never_hides_the_verdict_in_the_json(bundle: Path, tmp_path: Path) -> None:
    """`--warn-only` may soften the exit code; it must not soften the recorded status."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    out = tmp_path / "verdict.json"

    assert crr.main([str(bundle), "--quiet", "--warn-only", "--json", str(out)]) == crr.EXIT_OK
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == crr.STATUS_FINDINGS


# --------------------------------------------------------------------------------------------
# Drop-explanation channel
# --------------------------------------------------------------------------------------------


def test_a_nameless_dashboard_warning_does_not_explain_every_dropped_dashboard(bundle: Path) -> None:
    """Why `pbip_warnings[]` is not the explanation channel.

    `_warn("dashboard", name, "no supported visuals on this dashboard")` produces a reason string
    that does not contain the dashboard's name (`twb_to_pbir.py:6428-6430`), so matching on the flat
    warning list would attribute one dashboard's explanation to every dropped dashboard. The
    structured `viz_fidelity[]` row carries the name; a row for a DIFFERENT object must not excuse
    this one.
    """
    build_unit(
        bundle,
        "WB",
        worksheets=["A", "B"],
        dashboards={"DashA": ["A"], "DashB": ["B"]},
        page_ids=[crr.SourceObject(name="DashA", kind=crr.KIND_DASHBOARD).page_id],
        viz_fidelity=[
            {
                "worksheet": "DashA",
                "visual_type": "dashboard",
                "status": "warned",
                "reason": "manual attention required: no supported visuals on this dashboard",
            }
        ],
    )
    rows = {page["source_object"]: page for page in crr.scan(bundle)["units"][0]["pages"]}
    assert rows["DashB"]["page_status"] == crr.PAGE_DROPPED_UNEXPLAINED


def test_an_unrelated_engine_warning_does_not_explain_a_drop(bundle: Path) -> None:
    """Only the three deliberate-drop reasons account for a missing page."""
    build_unit(
        bundle,
        "WB",
        worksheets=["Kept", "Gone"],
        page_ids=[crr.SourceObject(name="Kept", kind=crr.KIND_WORKSHEET).page_id],
        viz_fidelity=[
            {
                "worksheet": "Gone",
                "status": "warned",
                "reason": "manual attention required: field 'Region' bound by caption fallback",
            }
        ],
    )
    rows = {page["source_object"]: page for page in crr.scan(bundle)["units"][0]["pages"]}
    assert rows["Gone"]["page_status"] == crr.PAGE_DROPPED_UNEXPLAINED
