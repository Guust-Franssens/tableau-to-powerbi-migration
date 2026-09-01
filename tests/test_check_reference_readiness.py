"""Tests for the reference-readiness ENTRY gate (issue #421).

The load-bearing property is **fail closed**: `blind`, `unverifiable` and `insufficient-grade` are
all distinct from `ready`, and none may exit 0. A readiness gate that green-lights on absent or
unattributable evidence is worse than no gate, because it launches an agent to build confidently
against nothing.

Round-1 review of PR #428 found eight ways it exited 0 on evidence it should refuse. Each has a test
below naming its finding number, and each is mutation-proved by
`tests/mutation_reference_readiness.py`.

⚠️ Two fixture rules exist because round 1 measured the fixtures themselves encoding the defect:

* **renders are REAL images.** The first version used an 8-byte PNG signature as "evidence" and
  asserted readiness, so the suite could not have caught a zero-byte render being promoted to READY.
  `write_png` emits a genuine, parseable PNG of a stated size.
* **evidence carries workbook identity.** Without `source_workbook_sha256`, one record satisfied two
  different units, and no fixture would have noticed.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
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

# Verified: only 8 md5 hex digits survive `_sanitize`, so these two distinct worksheet names both
# produce `page-ws-Collisioc5d9dc9d`.
COLLIDING_NAMES = ("Collision030344", "Collision079370")


def write_png(path: Path, width: int = 320, height: int = 240) -> Path:
    """A genuine, parseable PNG - not a signature stub.

    The round-1 fixtures wrote 8 bytes and asserted READY, so they encoded the very assumption the
    gate was supposed to refuse. Anything claiming to be evidence in this file is a real image.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes((x * 7 + y * 13) % 256 for x in range(width * 3)) for y in range(height))
    blob = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def write_workbook(path: Path, *, worksheets: list[str], dashboards: dict[str, list[str]] | None = None) -> Path:
    """A minimal `.twb`. ``dashboards`` maps a dashboard name to the worksheets placed on it."""
    ws_xml = "".join(f"<worksheet name='{name}' />" for name in worksheets)
    db_xml = ""
    for db_name, placed in (dashboards or {}).items():
        zones = "".join(f"<zone name='{name}' />" for name in placed)
        db_xml += f"<dashboard name='{db_name}'><zones><zone>{zones}</zone></zones></dashboard>"
    path.parent.mkdir(parents=True, exist_ok=True)
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
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.json").write_text(
        json.dumps(
            {
                "workbooks": [{"name": name} for name in workbooks],
                "datasources": [{"name": name} for name in datasources or []],
            }
        ),
        encoding="utf-8",
    )


def write_handover(
    root: Path,
    unit: str,
    *,
    source_id: str,
    viz_fidelity: list[dict] | None = None,
    pbip_warnings: list[str] | None = None,
) -> None:
    """The engine's per-workbook handover slice.

    `pbip_warnings` is populated by the routing tests on purpose: round-1 review found that the test
    claiming to pin the `viz_fidelity[]`-over-`pbip_warnings[]` routing never supplied
    `pbip_warnings` at all, so a mutation adding a flat-warning fallback survived the whole suite.
    """
    handover = root / "handover"
    handover.mkdir(parents=True, exist_ok=True)
    (handover / f"{unit}.json").write_text(
        json.dumps(
            {
                "workbook": {
                    "source_id": source_id,
                    "viz_fidelity": viz_fidelity or [],
                    "pbip_warnings": pbip_warnings or [],
                }
            }
        ),
        encoding="utf-8",
    )


def write_reference(  # pylint: disable=too-many-arguments,too-many-locals
    root: Path,
    entries: list[tuple[str, str, list[str]]],
    *,
    source_sha: str | None = None,
    size: tuple[int, int] = (320, 240),
    render_bytes: bytes | None = None,
    record_integrity: bool = True,
    view_type: str | None = None,
) -> Path:
    """A `reference/manifest.json`. Each entry is ``(name, provider, capabilities)``.

    Mirrors the real producer, which records `sha256` and `dimensions` per state
    (`capture_tableau_reference.py:246-257`). Round-2 review measured the gate ignoring both, so a
    captured image could be swapped wholesale and readiness survived; a fixture that omitted them
    could not have caught it. `record_integrity=False` exists to test that omission is a rejection.

    Note the manifest's top-level key is `dashboards`, but `capture_tableau_reference.py:199` files
    WORKSHEET thumbnails there too - which is why the key cannot be evidence of scope.
    """
    reference = root / "reference"
    reference.mkdir(parents=True, exist_ok=True)
    dashboards = []
    for index, (name, provider, capabilities) in enumerate(entries):
        image = f"shot-{index}.png"
        target = reference / image
        if render_bytes is None:
            write_png(target, *size)
        else:
            target.write_bytes(render_bytes)
        blob = target.read_bytes()
        state = {
            "state_slug": "default",
            "image": image,
            "provider": provider,
            "capabilities": capabilities,
            "numeric_oracle": None,
        }
        if view_type is not None:
            state["view_type"] = view_type
        if record_integrity:
            state |= {
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob),
                "dimensions": {"w": size[0], "h": size[1], "dpr": 2},
            }
        dashboards.append({"name": name, "states": [state]})
    (reference / "manifest.json").write_text(
        json.dumps({"source_workbook_sha256": source_sha, "dashboards": dashboards}), encoding="utf-8"
    )
    return reference


def write_oracle(root: Path, views: list[dict], *, size: tuple[int, int] = (320, 240)) -> Path:
    """An `_oracle/oracle-manifest.json`. Each view dict may carry `view_type` (PR #422).

    Records `sha256`, `bytes` and `dimensions_px` per leg, as the real producer does
    (`capture_tableau_oracle.py:687-705`).
    """
    oracle = root / "_oracle"
    (oracle / "images").mkdir(parents=True, exist_ok=True)
    records = []
    for index, view in enumerate(views):
        image = f"images/view-{index}.png"
        write_png(oracle / image, *size)
        blob = (oracle / image).read_bytes()
        records.append(
            {
                **view,
                "image": {
                    "status": "ok",
                    "path": image,
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "bytes": len(blob),
                    "dimensions_px": {"w": size[0], "h": size[1]},
                },
            }
        )
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


def build_unit(  # pylint: disable=too-many-arguments
    bundle: Path,
    unit: str,
    *,
    worksheets: list[str],
    dashboards: dict[str, list[str]] | None = None,
    page_ids: list[str] | None = None,
    viz_fidelity: list[dict] | None = None,
    pbip_warnings: list[str] | None = None,
) -> str:
    """Wire a complete workbook unit and return its source sha256, which evidence must carry."""
    source = write_workbook(bundle.parent / "assets" / f"{unit}.twb", worksheets=worksheets, dashboards=dashboards)
    write_engine_report(bundle, workbooks=[unit])
    write_handover(bundle, unit, source_id=str(source), viz_fidelity=viz_fidelity, pbip_warnings=pbip_warnings)
    if page_ids is None:
        page_ids = [obj.page_id for obj in crr.source_objects(source) or []]
    write_report(bundle, unit, page_ids)
    return hashlib.sha256(source.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------------
# Vocabulary pins - without these, every comparison against a constant is vacuous
# --------------------------------------------------------------------------------------------


def test_the_status_and_exit_vocabulary_is_pinned_to_its_literal_values() -> None:
    """Pin every constant the rest of this file compares against.

    Without this the suite is vacuous in one direction: `main(...) == crr.EXIT_CANNOT_ESTABLISH`
    compares the code's answer against the code's own constant, so redefining the constant to 0
    changes BOTH sides and the assertion still holds.

    ⚠️ Round-1 review found this pin INCOMPLETE: `GRADE_ORACLE` was omitted, and the oracle test
    compared against that same mutable constant, so `GRADE_ORACLE = GRADE_VALIDATION` survived the
    whole suite. Every grade string is pinned now, for exactly that reason.

    The 0/1/2/3 values are `check_connection_fidelity.py:160-163`'s, deliberately shared across gates.
    """
    assert (crr.EXIT_OK, crr.EXIT_FINDINGS, crr.EXIT_USAGE, crr.EXIT_CANNOT_ESTABLISH) == (0, 1, 2, 3)
    assert (crr.READY, crr.BLIND, crr.UNVERIFIABLE) == ("ready", "blind", "unverifiable")
    assert crr.INSUFFICIENT_GRADE == "insufficient-grade"
    assert (crr.STATUS_READY, crr.STATUS_FINDINGS) == ("READY", "FINDINGS")
    assert (crr.STATUS_NOT_APPLICABLE, crr.STATUS_CANNOT_ESTABLISH) == ("NOT_APPLICABLE", "CANNOT_ESTABLISH")
    assert (crr.KIND_DASHBOARD, crr.KIND_WORKSHEET, crr.KIND_UNKNOWN) == ("dashboard", "worksheet", "unknown")
    assert (crr.PAGE_EMITTED, crr.PAGE_DROPPED_EXPLAINED, crr.PAGE_DROPPED_UNEXPLAINED) == (
        "emitted",
        "dropped_explained",
        "dropped_unexplained",
    )
    assert crr.GRADE_VALIDATION == "validation-grade"
    assert crr.GRADE_ORACLE == "layout/text only (oracle capture, default view state)"
    assert crr.GRADE_UNKNOWN == "unknown"
    assert crr.GRADE_ORACLE != crr.GRADE_VALIDATION
    # Round-3 finding 1: there is deliberately NO kind that a grade can promote a record into.
    # Grade says how good a picture is; it can never say what the picture is OF.
    assert not hasattr(crr, "KIND_ASSERTED")
    assert crr.AMBIGUOUS == "ambiguous"
    # Round-2 finding 2: the ceiling is what stops a producer grading itself above what it can
    # capture, so the ceilings themselves are pinned. `manual` is the ONLY route to validation grade.
    assert crr.PROVIDER_CEILING["embedded_thumbnail"] == frozenset({"layout_grade"})
    assert crr.PROVIDER_CEILING["public_playwright"] == frozenset({"layout_grade", "text_readable"})
    assert crr.PROVIDER_CEILING["oracle_capture"] == frozenset({"layout_grade", "text_readable"})
    assert crr.CAP_VALIDATION in crr.PROVIDER_CEILING["manual"]
    assert {p for p, caps in crr.PROVIDER_CEILING.items() if crr.CAP_VALIDATION in caps} == {"manual"}
    assert crr.MIN_RENDER_EDGE == 64


def test_there_is_no_flag_that_can_soften_the_verdict(tmp_path: Path) -> None:
    """Round-1 finding 1: `--warn-only` returned exit 0 on a CANNOT_ESTABLISH bundle.

    An entry gate that can be asked to say yes is not an entry gate, so the flag is gone rather than
    fixed. Argparse must reject it - otherwise a caller's muscle memory silently re-opens the hole.
    """
    with pytest.raises(SystemExit) as excinfo:
        crr.main(["--warn-only", str(tmp_path)])
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------------------------
# Identity: the engine's own page naming, reproduced
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("worksheet", "page_id"), sorted(MERIDIAN_PAGE_IDS.items()))
def test_engine_page_id_reproduces_the_real_engine_output(worksheet: str, page_id: str) -> None:
    """Pinned against page ids a real 2.339.0 bundle actually contains."""
    assert crr.engine_page_id(f"page-ws-{worksheet}") == page_id


def test_a_dashboard_and_a_same_named_worksheet_get_different_page_ids() -> None:
    """The identity join that a name slug cannot make."""
    as_worksheet = crr.SourceObject(name="Regional Share", kind="worksheet").page_id
    as_dashboard = crr.SourceObject(name="Regional Share", kind="dashboard").page_id
    assert as_worksheet == "page-ws-Regional05286155"
    assert as_dashboard != as_worksheet


def test_colliding_page_ids_cannot_be_attributed(bundle: Path) -> None:
    """Round-1 finding 6b: the identity join is strong but NOT collision-free.

    Only 8 md5 hex digits survive `_sanitize`, and these two names collide. One physical page must
    never satisfy two expected pages, so this is `CANNOT_ESTABLISH` rather than a silent double
    count.
    """
    first, second = COLLIDING_NAMES
    assert crr.engine_page_id(f"page-ws-{first}") == crr.engine_page_id(f"page-ws-{second}")
    build_unit(bundle, "WB", worksheets=list(COLLIDING_NAMES))

    report = crr.scan(bundle)
    assert report["status"] == "CANNOT_ESTABLISH"
    assert "page-ws-Collisioc5d9dc9d" in report["units"][0]["detail"]
    assert crr.main([str(bundle), "--quiet"]) == 3


# --------------------------------------------------------------------------------------------
# Question 1: completeness, against the engine's real rule
# --------------------------------------------------------------------------------------------


def test_orphan_worksheets_are_expected_pages(tmp_path: Path) -> None:
    """0 dashboards + 3 worksheets = 3 pages - the shape `check_unit.expected_pages` gets wrong."""
    source = write_workbook(tmp_path / "wb.twb", worksheets=list(MERIDIAN_PAGE_IDS))
    objects = crr.source_objects(source)
    assert objects is not None
    assert {obj.name for obj in objects} == set(MERIDIAN_PAGE_IDS)
    assert {obj.kind for obj in objects} == {"worksheet"}


def test_a_worksheet_placed_on_a_dashboard_is_not_an_orphan(tmp_path: Path) -> None:
    """A worksheet laid onto a dashboard gets no page of its own - the engine's `placed` set."""
    source = write_workbook(tmp_path / "wb.twb", worksheets=["Placed", "Loose"], dashboards={"Main": ["Placed"]})
    objects = crr.source_objects(source)
    assert objects is not None
    assert {(obj.name, obj.kind) for obj in objects} == {("Main", "dashboard"), ("Loose", "worksheet")}


def test_an_unreadable_source_is_none_not_an_empty_expectation(tmp_path: Path) -> None:
    """`None` and `[]` must stay distinct all the way to the exit code."""
    broken = tmp_path / "broken.twb"
    broken.write_text("<workbook><unclosed>", encoding="utf-8")
    assert crr.source_objects(broken) is None


def test_an_unreadable_page_definition_is_not_a_page(bundle: Path) -> None:
    """Round-1 finding 6a: completeness passed with no readable page mapping at all.

    The old `actual_page_ids` fell back to the containing directory's name, so corrupting every
    `page.json` still yielded three pages and READY. A page whose definition cannot be read is a
    problem, not a page.
    """
    sha = build_unit(bundle, "WB", worksheets=list(MERIDIAN_PAGE_IDS))
    write_reference(bundle, [(n, "embedded_thumbnail", ["layout_grade"]) for n in MERIDIAN_PAGE_IDS], source_sha=sha)
    assert crr.scan(bundle)["status"] == "READY"

    pages = bundle / "pbip" / "WB" / "WB.Report" / "definition" / "pages"
    for page_json in pages.rglob("page.json"):
        page_json.write_text("{ not json", encoding="utf-8")

    assert crr.scan(bundle)["status"] == "CANNOT_ESTABLISH"
    assert crr.main([str(bundle), "--quiet"]) == 3


def test_pages_json_disagreeing_with_the_page_definitions_cannot_be_judged(bundle: Path) -> None:
    """`pages.json` is the report's own statement of which pages exist; a disagreement voids the join."""
    build_unit(bundle, "WB", worksheets=["Solo"])
    pages = bundle / "pbip" / "WB" / "WB.Report" / "definition" / "pages"
    (pages / "pages.json").write_text(json.dumps({"pageOrder": ["page-that-does-not-exist"]}), encoding="utf-8")

    assert crr.scan(bundle)["status"] == "CANNOT_ESTABLISH"


def test_a_page_the_engine_dropped_with_a_reason_is_accounted_for(bundle: Path) -> None:
    """`dropped_explained` must not read as a conversion gap - that is the cry-wolf direction."""
    build_unit(
        bundle,
        "WB",
        worksheets=["Kept", "Dropped"],
        page_ids=[crr.SourceObject(name="Kept", kind="worksheet").page_id],
        viz_fidelity=[
            {"worksheet": "Dropped", "status": "warned", "reason": "manual attention required: unsupported visual type"}
        ],
    )
    report = crr.scan(bundle)
    rows = {page["source_object"]: page for page in report["units"][0]["pages"]}
    assert rows["Dropped"]["page_status"] == "dropped_explained"
    assert report["pages_dropped_unexplained"] == 0
    assert report["pages_dropped_explained"] == 1


def test_a_page_the_engine_dropped_silently_is_a_finding(bundle: Path) -> None:
    """No engine explanation means a real conversion gap, and it must not exit 0."""
    build_unit(
        bundle,
        "WB",
        worksheets=["Kept", "Vanished"],
        page_ids=[crr.SourceObject(name="Kept", kind="worksheet").page_id],
    )
    rows = {page["source_object"]: page for page in crr.scan(bundle)["units"][0]["pages"]}
    assert rows["Vanished"]["page_status"] == "dropped_unexplained"
    assert rows["Vanished"]["readiness"] == "blind"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_worksheet_warning_cannot_excuse_a_missing_dashboard(bundle: Path) -> None:
    """Round-1 finding 5: the `pbip_warnings[]` defect, one level down.

    `drop_explanations` keyed on the normalized name alone, so a WORKSHEET warning for `Ops` made a
    genuinely missing DASHBOARD named `Ops` read as `dropped_explained` and the unit went READY.
    Sharing a name between a dashboard and its principal worksheet is the normal Tableau case, so
    this is not an edge case.
    """
    build_unit(
        bundle,
        "WB",
        worksheets=["Ops"],
        dashboards={"Ops": []},
        page_ids=[crr.SourceObject(name="Ops", kind="worksheet").page_id],
        viz_fidelity=[
            {
                "worksheet": "Ops",
                "visual_type": "unsupported",
                "status": "warned",
                "reason": "manual attention required: unsupported visual type",
            }
        ],
    )
    rows = {(p["source_type"], p["source_object"]): p for p in crr.scan(bundle)["units"][0]["pages"]}
    assert rows[("dashboard", "Ops")]["page_status"] == "dropped_unexplained"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_dashboard_scope_warning_does_explain_a_missing_dashboard(bundle: Path) -> None:
    """Discriminating twin: the kind-aware key must still ACCEPT a correctly scoped explanation.

    `migrate_estate.py:1201-1204` writes dashboard-scope warnings with `visual_type` set to the scope
    string `"dashboard"`. Without this test the previous one would also pass if explanations never
    matched anything.
    """
    build_unit(
        bundle,
        "WB",
        worksheets=["Ops"],
        dashboards={"Ops": []},
        page_ids=[crr.SourceObject(name="Ops", kind="worksheet").page_id],
        viz_fidelity=[
            {
                "worksheet": "Ops",
                "visual_type": "dashboard",
                "status": "warned",
                "reason": "manual attention required: no supported visuals on this dashboard",
            }
        ],
    )
    rows = {(p["source_type"], p["source_object"]): p for p in crr.scan(bundle)["units"][0]["pages"]}
    assert rows[("dashboard", "Ops")]["page_status"] == "dropped_explained"


def test_a_flat_pbip_warning_cannot_explain_any_drop(bundle: Path) -> None:
    """Why `viz_fidelity[]` is the channel and `pbip_warnings[]` is not.

    ⚠️ Round-1 review found the previous version of this test supplied no `pbip_warnings` at all, so
    a mutation adding a flat-warning fallback SURVIVED - the test claiming to pin the routing did not
    pin it. The warnings below are real, nameless ones the engine emits
    (`_warn("dashboard", name, ...)` drops the name), and they must not account for anything.
    """
    build_unit(
        bundle,
        "WB",
        worksheets=["A", "B"],
        dashboards={"DashA": ["A"], "DashB": ["B"]},
        page_ids=[crr.SourceObject(name="DashA", kind="dashboard").page_id],
        pbip_warnings=[
            "manual attention required: no supported visuals on this dashboard",
            "manual attention required: unsupported visual type",
        ],
    )
    rows = {page["source_object"]: page for page in crr.scan(bundle)["units"][0]["pages"]}
    assert rows["DashB"]["page_status"] == "dropped_unexplained"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_an_unrelated_engine_warning_does_not_explain_a_drop(bundle: Path) -> None:
    """Only the three deliberate-drop reasons account for a missing page."""
    build_unit(
        bundle,
        "WB",
        worksheets=["Kept", "Gone"],
        page_ids=[crr.SourceObject(name="Kept", kind="worksheet").page_id],
        viz_fidelity=[
            {
                "worksheet": "Gone",
                "status": "warned",
                "reason": "manual attention required: field 'Region' bound by caption fallback",
            }
        ],
    )
    rows = {page["source_object"]: page for page in crr.scan(bundle)["units"][0]["pages"]}
    assert rows["Gone"]["page_status"] == "dropped_unexplained"


# --------------------------------------------------------------------------------------------
# Scope: a worksheet render can never satisfy a dashboard page
# --------------------------------------------------------------------------------------------


def test_a_worksheet_scope_can_never_satisfy_a_dashboard_page() -> None:
    """The scope join itself, isolated from any fixture."""
    dashboard = crr.SourceObject(name="Ops", kind="dashboard")
    worksheet_render = crr.Evidence(
        name="Ops",
        kind="worksheet",
        grade="layout_grade",
        origin="reference",
        provider="embedded_thumbnail",
        path="x.png",
        width=320,
        height=240,
        workbook_sha="abc",
        workbook_luid=None,
        workbook_name=None,
    )
    match, lookalikes = crr.match_evidence(dashboard, [worksheet_render])
    assert match is None
    # Round-4: the report carries DESCRIPTIONS, never the evidence objects, so a caller cannot
    # quietly promote a lookalike into a match. The previous helper returned a bool and so worked
    # perfectly well as a resolution predicate, bypassing the whole boundary.
    assert lookalikes == [crr.oid.Lookalike(name="Ops", kind="worksheet")]
    assert not any(isinstance(item, crr.Evidence) for item in lookalikes)


def test_a_worksheet_render_does_not_make_a_dashboard_page_ready(bundle: Path) -> None:
    """THE regression test (issue #421).

    A Tableau `<thumbnail>` is a WORKSHEET render, yet `capture_tableau_reference.py:199` files it
    under the manifest's `dashboards` key, where `check_unit.py`'s `_slug` match then lets it satisfy
    a same-named DASHBOARD page.
    """
    sha = build_unit(bundle, "WB", worksheets=["Regional Share"], dashboards={"Regional Share": ["Regional Share"]})
    write_reference(bundle, [("Regional Share", "embedded_thumbnail", ["layout_grade"])], source_sha=sha)

    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["source_type"] == "dashboard"
    assert page["readiness"] == "unverifiable"
    assert "worksheet" in page["matched_by"]
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_worksheet_render_does_satisfy_a_worksheet_page(bundle: Path) -> None:
    """Discriminating twin: without it, the regression would also pass if nothing ever matched."""
    sha = build_unit(bundle, "WB", worksheets=["Regional Share"])
    write_reference(bundle, [("Regional Share", "embedded_thumbnail", ["layout_grade"])], source_sha=sha)

    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["source_type"] == "worksheet"
    assert page["readiness"] == "ready"
    assert page["grade"] == "layout_grade"
    assert crr.main([str(bundle), "--quiet"]) == 0


def test_an_oracle_record_with_no_view_type_cannot_satisfy_a_page(bundle: Path) -> None:
    """PR #422's field absent = cannot establish, never "it could be either"."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(bundle, [{"view_name": "Revenue Trend", "workbook_name": "WB"}])

    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["readiness"] == "unverifiable"
    assert "unknown" in page["matched_by"]


def test_an_oracle_record_typed_unknown_cannot_satisfy_a_page(bundle: Path) -> None:
    """PR #422 fails closed to `unknown` when the Metadata API is disabled; so must this."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "unknown", "workbook_name": "WB"}])

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "unverifiable"


def test_an_oracle_record_typed_worksheet_still_cannot_satisfy_a_dashboard_page(bundle: Path) -> None:
    """The scope join applies to the oracle route too, not only to `reference/`."""
    build_unit(bundle, "WB", worksheets=["Ops"], dashboards={"Ops": ["Ops"]})
    write_oracle(bundle, [{"view_name": "Ops", "view_type": "worksheet", "workbook_name": "WB"}])

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "unverifiable"


def test_a_page_with_no_evidence_at_all_is_blind_not_unverifiable(bundle: Path) -> None:
    """`blind` and `unverifiable` are different operator actions: capture one, or identify one."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["readiness"] == "blind"
    assert page["evidence"] == "absent"


# --------------------------------------------------------------------------------------------
# Question 3: grade (round-1 finding 7)
# --------------------------------------------------------------------------------------------


def test_validation_grade_is_reported_when_present(bundle: Path) -> None:
    """The one route to `validation_grade`, and it must declare its object TYPE to be usable.

    WARNING: round-3 finding 1. Making this route work by promoting a validation-grade `manual`
    record to a kind matching BOTH dashboards and worksheets re-created the founding defect - one
    image made a dashboard `Ops` and a worksheet `Ops` ready at once. The flag asserts GRADE;
    `capture_tableau_reference.py:264-266` says the tool cannot know "even that it is a screenshot of
    this dashboard". So the manifest must DECLARE `view_type`, and the grade never touches kind.
    """
    sha = build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_reference(
        bundle,
        [("tableau-Revenue Trend", "manual", ["layout_grade", "text_readable", "validation_grade"])],
        source_sha=sha,
        view_type="worksheet",
    )

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["grade"] == "validation-grade"
    assert report["all_evidence_validation_grade"] is True
    assert crr.GRADE_CEILING_NOTE not in crr.render(report)
    assert crr.main([str(bundle), "--quiet", "--require-validation-grade"]) == 0


def test_a_grade_can_never_widen_an_evidence_kind(bundle: Path) -> None:
    """Round-3 finding 1: the same manual record, WITHOUT a declared type, satisfies nothing.

    Grade and kind are independent axes. If the only difference between "satisfies nothing" and
    "satisfies everything" is a quality flag, the flag has become an identity claim.
    """
    sha = build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_reference(
        bundle,
        [("tableau-Revenue Trend", "manual", ["layout_grade", "text_readable", "validation_grade"])],
        source_sha=sha,
    )

    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["readiness"] != "ready"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_validation_grade_manual_record_cannot_satisfy_both_kinds(bundle: Path) -> None:
    """The measured shape: one `tableau-Ops` image made a dashboard AND a worksheet `Ops` ready."""
    sha = build_unit(bundle, "WB", worksheets=["Ops"], dashboards={"Ops": []})
    write_reference(
        bundle,
        [("tableau-Ops", "manual", ["layout_grade", "text_readable", "validation_grade"])],
        source_sha=sha,
        view_type="worksheet",
    )

    rows = {(p["source_type"], p["source_object"]): p for p in crr.scan(bundle)["units"][0]["pages"]}
    assert rows[("worksheet", "Ops")]["readiness"] == "ready"
    assert rows[("dashboard", "Ops")]["readiness"] != "ready"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_one_render_cannot_make_two_pages_ready(bundle: Path) -> None:
    """Round-3 finding 1, second half: the prefix alias created a name with no uniqueness check.

    One genuine image made two DISTINCT worksheets (`Ops` and `tableau-Ops`) ready. Identity is not
    enough on its own - evidence must be EXCLUSIVE, so a render claimed twice invalidates both
    claims rather than satisfying both.
    """
    sha = build_unit(bundle, "WB", worksheets=["Ops", "tableau-Ops"])
    write_reference(
        bundle,
        [("tableau-Ops", "manual", ["layout_grade", "text_readable", "validation_grade"])],
        source_sha=sha,
        view_type="worksheet",
    )

    rows = {page["source_object"]: page for page in crr.scan(bundle)["units"][0]["pages"]}
    assert rows["Ops"]["readiness"] != "ready"
    assert rows["tableau-Ops"]["readiness"] != "ready"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_low_grade_provider_cannot_promote_itself(bundle: Path) -> None:
    """Round-2 finding 2: grade came from the self-reported list, with no provider ceiling.

    An `embedded_thumbnail` record is a 192x192 worksheet render by construction. Claiming
    `validation_grade` made it READY under `--require-validation-grade` AND suppressed the ceiling
    warning - the weakest-provenance producer outranking every honest one.
    """
    sha = build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_reference(
        bundle, [("Revenue Trend", "embedded_thumbnail", ["layout_grade", "validation_grade"])], source_sha=sha
    )

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert any("can only produce" in item["reason"] for item in report["evidence_rejected"])
    assert report["all_evidence_validation_grade"] is False
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_an_unrecognised_provider_can_claim_nothing(bundle: Path) -> None:
    """An unknown producer has no ceiling, so nothing bounds what it may claim."""
    sha = build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_reference(bundle, [("Revenue Trend", "some_new_tool", ["layout_grade"])], source_sha=sha)

    report = crr.scan(bundle)
    assert report["units"][0]["pages"][0]["readiness"] == "blind"
    assert any("unrecognised capture provider" in item["reason"] for item in report["evidence_rejected"])


def test_one_validation_grade_page_does_not_silence_the_ceiling_for_the_rest(bundle: Path) -> None:
    """Round-1 finding 7b: the warning keyed on `any`, so one good capture hid every other page.

    `Good` is a genuine validation-grade record - declared type, manual provider - so this test
    discriminates: it would pass vacuously if NO page reached validation grade.
    """
    sha = build_unit(bundle, "WB", worksheets=["Good", "Weak"])
    reference = write_reference(
        bundle,
        [
            ("tableau-Good", "manual", ["layout_grade", "text_readable", "validation_grade"]),
            ("Weak", "embedded_thumbnail", ["layout_grade"]),
        ],
        source_sha=sha,
        view_type="worksheet",
    )
    manifest = json.loads((reference / "manifest.json").read_text(encoding="utf-8"))
    # `view_type` applies per entry in the real manifest; the thumbnail's own provider already
    # implies worksheet, so dropping it here keeps the fixture honest about what each producer says.
    del manifest["dashboards"][1]["states"][0]["view_type"]
    (reference / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = crr.scan(bundle)
    rows = {page["source_object"]: page for page in report["units"][0]["pages"]}
    assert rows["Good"]["grade"] == "validation-grade"
    assert rows["Weak"]["grade"] == "layout_grade"
    assert report["all_evidence_validation_grade"] is False
    assert crr.GRADE_CEILING_NOTE in crr.render(report)


def test_require_validation_grade_changes_page_readiness_not_just_the_unit(bundle: Path) -> None:
    """Round-1 finding 7a: the unit said 1/2 while the top level said 2/2 ready.

    The bar now lands on the PAGE, so every count agrees.
    """
    sha = build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_reference(bundle, [("Revenue Trend", "embedded_thumbnail", ["layout_grade"])], source_sha=sha)

    assert crr.scan(bundle)["status"] == "READY"
    strict = crr.scan(bundle, require_validation_grade=True)
    assert strict["status"] == "FINDINGS"
    assert strict["pages_ready"] == 0
    assert strict["pages_insufficient_grade"] == 1
    assert strict["units"][0]["detail"].startswith("0/1")
    assert crr.main([str(bundle), "--quiet", "--require-validation-grade"]) == 1


def test_oracle_grade_is_below_the_validation_bar(bundle: Path) -> None:
    """Round-1 finding 8a: `GRADE_ORACLE = GRADE_VALIDATION` survived the entire suite.

    Nothing exercised the oracle grade against the bar, and the only oracle assertion compared it to
    that same mutable constant.
    """
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_name": "WB"}])

    strict = crr.scan(bundle, require_validation_grade=True)
    assert strict["pages_insufficient_grade"] == 1
    assert strict["status"] == "FINDINGS"


# --------------------------------------------------------------------------------------------
# Fail-closed: nothing unassessable may collapse into the clean bucket
# --------------------------------------------------------------------------------------------


def test_an_unresolvable_source_cannot_establish_and_does_not_exit_zero(bundle: Path) -> None:
    """No source workbook = no expectation. That is exit 3, and it is NOT a pass."""
    write_engine_report(bundle, workbooks=["WB"])
    write_report(bundle, "WB", ["page-ws-anything"])

    assert crr.scan(bundle)["status"] == "CANNOT_ESTABLISH"
    assert crr.main([str(bundle), "--quiet"]) == 3


def test_the_expectation_never_falls_back_to_the_pages_that_were_built(bundle: Path) -> None:
    """The circularity in `check_oracle_coverage:925`, refused."""
    source = write_workbook(bundle.parent / "assets" / "WB.twb", worksheets=[])
    write_engine_report(bundle, workbooks=["WB"])
    write_handover(bundle, "WB", source_id=str(source))
    write_report(bundle, "WB", ["page1", "page2", "page3"])

    report = crr.scan(bundle)
    assert report["status"] == "CANNOT_ESTABLISH"
    assert report["pages_expected"] == 0
    assert crr.main([str(bundle), "--quiet"]) == 3


def test_a_datasource_only_unit_is_not_applicable(bundle: Path) -> None:
    """Legitimately reference-free work must not be blocked."""
    write_engine_report(bundle, workbooks=[], datasources=["Shared DS"])
    write_report(bundle, "Shared DS", ["page1"])

    assert crr.scan(bundle)["status"] == "NOT_APPLICABLE"
    assert crr.main([str(bundle), "--quiet"]) == 0


def test_a_workbook_whose_report_never_shipped_is_a_finding(bundle: Path) -> None:
    """Round-1 finding 2: any semantic model anywhere granted NOT_APPLICABLE and exit 0.

    A workbook whose report generation FAILED is the loudest possible signal that work cannot start,
    and it read as legitimately reference-free.
    """
    write_engine_report(bundle, workbooks=["WB"], datasources=["Shared DS"])
    (bundle / "pbip" / "WB" / "Model.SemanticModel" / "definition").mkdir(parents=True)

    report = crr.scan(bundle)
    assert report["status"] == "FINDINGS"
    assert report["units_not_applicable"] == 0
    assert "no report ships for it" in report["units"][0]["detail"]
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_not_applicable_is_earned_from_the_engine_report_not_from_an_empty_page_list(bundle: Path) -> None:
    """A workbook unit that emitted no pages is unassessable, not `NOT_APPLICABLE`."""
    write_engine_report(bundle, workbooks=["WB"], datasources=["Shared DS"])
    write_report(bundle, "WB", [])

    report = crr.scan(bundle)
    assert report["status"] == "CANNOT_ESTABLISH"
    assert report["units_not_applicable"] == 0


def test_an_empty_target_is_cannot_establish(tmp_path: Path) -> None:
    """An empty directory has nothing to measure, and that must never read as a pass."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert crr.scan(empty)["status"] == "CANNOT_ESTABLISH"
    assert crr.main([str(empty), "--quiet"]) == 3


def test_findings_outrank_cannot_establish_but_both_stay_visible(bundle: Path) -> None:
    """Neither count may hide the other; a fixed finding must still reveal the unassessable unit."""
    build_unit(bundle, "WB", worksheets=["Loose"])
    write_engine_report(bundle, workbooks=["WB", "Orphaned"])
    write_report(bundle, "Orphaned", ["page1"])

    report = crr.scan(bundle)
    assert report["status"] == "FINDINGS"
    assert report["units_cannot_establish"] == 1
    assert "CANNOT_ESTABLISH" in crr.render(report)


def test_a_missing_path_is_a_usage_error_not_a_verdict(tmp_path: Path) -> None:
    """A bad path must exit 2, never produce a readiness opinion about nothing."""
    with pytest.raises(SystemExit) as excinfo:
        crr.main([str(tmp_path / "does-not-exist"), "--quiet"])
    assert excinfo.value.code == 2


def test_the_json_verdict_always_carries_the_true_status(bundle: Path, tmp_path: Path) -> None:
    """`--json` is the advisory route now that `--warn-only` is gone; it must never soften."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    out = tmp_path / "verdict.json"

    assert crr.main([str(bundle), "--quiet", "--json", str(out)]) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "FINDINGS"


# --------------------------------------------------------------------------------------------
# Round-2 finding 4: pages.json is required, not optional
# --------------------------------------------------------------------------------------------


def test_an_unreadable_pages_json_is_not_a_valid_mapping(bundle: Path) -> None:
    """Measured: failing ONLY the `pages.json` reads still produced `READY 3/3`."""
    sha = build_unit(bundle, "WB", worksheets=list(MERIDIAN_PAGE_IDS))
    write_reference(bundle, [(n, "embedded_thumbnail", ["layout_grade"]) for n in MERIDIAN_PAGE_IDS], source_sha=sha)
    assert crr.scan(bundle)["status"] == "READY"

    pages = bundle / "pbip" / "WB" / "WB.Report" / "definition" / "pages"
    (pages / "pages.json").write_text("{ not json", encoding="utf-8")

    assert crr.scan(bundle)["status"] == "CANNOT_ESTABLISH"
    assert crr.main([str(bundle), "--quiet"]) == 3


def test_a_missing_pages_json_is_not_a_valid_mapping(bundle: Path) -> None:
    """Absent is the same as unreadable: the report states no page set to check against."""
    build_unit(bundle, "WB", worksheets=["Solo"])
    (bundle / "pbip" / "WB" / "WB.Report" / "definition" / "pages" / "pages.json").unlink()

    assert crr.scan(bundle)["status"] == "CANNOT_ESTABLISH"


def test_a_non_list_page_order_is_not_a_valid_mapping(bundle: Path) -> None:
    """A wrong-shaped `pageOrder` used to skip the cross-check entirely."""
    build_unit(bundle, "WB", worksheets=["Solo"])
    pages = bundle / "pbip" / "WB" / "WB.Report" / "definition" / "pages"
    (pages / "pages.json").write_text(json.dumps({"pageOrder": "page1"}), encoding="utf-8")

    assert crr.scan(bundle)["status"] == "CANNOT_ESTABLISH"


# --------------------------------------------------------------------------------------------
# Round-2 finding 5: normalization collapse - the third layer of one recurring defect
# --------------------------------------------------------------------------------------------


def test_names_differing_only_by_whitespace_cannot_be_attributed(bundle: Path) -> None:
    """`Ops  Summary` and `Ops Summary` take DIFFERENT page ids but collapsed to one key.

    One evidence record marked both ready, and one deliberate-drop warning classified the other as
    `dropped_explained`. This is the same "one object's excuse covering another" defect that was
    fixed at the routing level, then the matching level; ambiguity is now a refusal.
    """
    doubled, single = "Ops  Summary", "Ops Summary"
    assert crr.engine_page_id(f"page-ws-{doubled}") != crr.engine_page_id(f"page-ws-{single}")
    build_unit(bundle, "WB", worksheets=[doubled, single])

    report = crr.scan(bundle)
    assert report["status"] == "CANNOT_ESTABLISH"
    assert "differ only by case or repeated whitespace" in report["units"][0]["detail"]
    assert crr.main([str(bundle), "--quiet"]) == 3


def test_a_drop_warning_matches_the_exact_object_name_only(bundle: Path) -> None:
    """Both sides of the drop join are engine artifacts and byte-exact, so no normalization runs."""
    build_unit(
        bundle,
        "WB",
        worksheets=["Ops Summary"],
        page_ids=[],
        viz_fidelity=[
            {
                "worksheet": "ops summary",
                "visual_type": "unsupported",
                "status": "warned",
                "reason": "manual attention required: unsupported visual type",
            }
        ],
    )
    rows = {page["source_object"]: page for page in crr.scan(bundle)["units"][0]["pages"]}
    assert rows["Ops Summary"]["page_status"] == "dropped_unexplained"


def test_an_exact_drop_warning_still_explains_its_own_object(bundle: Path) -> None:
    """Discriminating twin: exact matching must not break the legitimate case."""
    build_unit(
        bundle,
        "WB",
        worksheets=["Ops Summary"],
        page_ids=[],
        viz_fidelity=[
            {
                "worksheet": "Ops Summary",
                "visual_type": "unsupported",
                "status": "warned",
                "reason": "manual attention required: unsupported visual type",
            }
        ],
    )
    rows = {page["source_object"]: page for page in crr.scan(bundle)["units"][0]["pages"]}
    assert rows["Ops Summary"]["page_status"] == "dropped_explained"


def test_two_evidence_records_sharing_a_normalized_name_are_ambiguous(bundle: Path) -> None:
    """Evidence names come from external providers, so a normalized fallback survives - but only
    when it is unambiguous. Two candidates is a refusal, because picking one would be a guess."""
    sha = build_unit(bundle, "WB", worksheets=["Ops Summary"])
    write_reference(
        bundle,
        [
            ("ops summary", "embedded_thumbnail", ["layout_grade"]),
            ("OPS  SUMMARY", "embedded_thumbnail", ["layout_grade"]),
        ],
        source_sha=sha,
    )

    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["readiness"] == "unverifiable"
    assert "picking one would be a guess" in page["matched_by"]
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_a_single_differently_spelled_evidence_record_still_matches(bundle: Path) -> None:
    """Discriminating twin: an unambiguous normalized fallback must still work for one record."""
    sha = build_unit(bundle, "WB", worksheets=["Ops Summary"])
    write_reference(bundle, [("ops summary", "embedded_thumbnail", ["layout_grade"])], source_sha=sha)

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "ready"


# --------------------------------------------------------------------------------------------
# Round-4 HIGH: exclusivity by FILE identity, enforced across all units
# --------------------------------------------------------------------------------------------


def test_the_same_file_under_two_spellings_is_still_one_render(bundle: Path) -> None:
    """Measured: exclusivity compared `evidence_path` STRINGS, not physical render identity.

    On Windows the same PNG referenced as `shot.png` and `SHOT.PNG` - `Path.samefile()` True - left
    both pages ready, because the stored strings differed. `render_key` is filesystem identity now.
    """
    sha = build_unit(bundle, "WB", worksheets=["Alpha", "Beta"])
    reference = write_reference(
        bundle,
        [("Alpha", "embedded_thumbnail", ["layout_grade"]), ("Beta", "embedded_thumbnail", ["layout_grade"])],
        source_sha=sha,
    )
    manifest = json.loads((reference / "manifest.json").read_text(encoding="utf-8"))
    # Point the second entry at the SAME file, spelled differently.
    (reference / "shot-1.png").unlink()
    manifest["dashboards"][1]["states"][0]["image"] = "SHOT-0.PNG"
    manifest["dashboards"][1]["states"][0]["sha256"] = manifest["dashboards"][0]["states"][0]["sha256"]
    manifest["dashboards"][1]["states"][0]["bytes"] = manifest["dashboards"][0]["states"][0]["bytes"]
    (reference / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if not (reference / "SHOT-0.PNG").exists():  # case-insensitive filesystems resolve this already
        pytest.skip("filesystem is case-sensitive, so the two spellings are genuinely two files")
    assert (reference / "SHOT-0.PNG").samefile(reference / "shot-0.png")

    rows = {page["source_object"]: page for page in crr.scan(bundle)["units"][0]["pages"]}
    assert rows["Alpha"]["readiness"] != "ready"
    assert rows["Beta"]["readiness"] != "ready"
    assert crr.main([str(bundle), "--quiet"]) == 1


def write_manifest_for(directory: Path, name: str, render: Path, source_sha: str) -> None:
    """A reference manifest pointing at an EXISTING render file, honestly hashed."""
    directory.mkdir(parents=True, exist_ok=True)
    blob = render.read_bytes()
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "source_workbook_sha256": source_sha,
                "dashboards": [
                    {
                        "name": name,
                        "states": [
                            {
                                "state_slug": "default",
                                "image": render.name,
                                "provider": "embedded_thumbnail",
                                "capabilities": ["layout_grade"],
                                "sha256": hashlib.sha256(blob).hexdigest(),
                                "bytes": len(blob),
                                "dimensions": {"w": 320, "h": 240},
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_one_render_cannot_satisfy_a_page_in_each_of_two_units(bundle: Path) -> None:
    """Measured: exclusivity ran independently INSIDE each unit, so the same render satisfied one
    page in each of two units and the bundle reported `READY 2/2`.

    Both manifests are individually valid - correct source sha, honest hash, real image - and each
    is legitimately attributable to its own unit. Only the CROSS-UNIT view shows that one physical
    file is being credited twice, which is why the check has to run once over every row.
    """
    first = write_workbook(bundle.parent / "assets" / "One.twb", worksheets=["Shared"])
    second = write_workbook(bundle.parent / "assets" / "Two.twb", worksheets=["Shared"])
    # The two workbooks must genuinely DIFFER, or they hash identically and each manifest attaches to
    # both units - which makes the pages ambiguous and the test pass without ever reaching
    # exclusivity. Measured: that is exactly what the first version of this fixture did.
    second.write_text(second.read_text(encoding="utf-8") + "<!-- second -->", encoding="utf-8")
    assert hashlib.sha256(first.read_bytes()).hexdigest() != hashlib.sha256(second.read_bytes()).hexdigest()
    write_engine_report(bundle, workbooks=["One", "Two"])
    for unit, source in (("One", first), ("Two", second)):
        write_handover(bundle, unit, source_id=str(source))
        write_report(bundle, unit, [obj.page_id for obj in crr.source_objects(source) or []])

    # `_default_dirs` looks in <bundle>/reference and <bundle>/../reference, so two manifests can
    # coexist - one per unit - while naming the SAME physical render.
    render = bundle / "reference" / "shot.png"
    write_png(render, 320, 240)
    write_manifest_for(bundle / "reference", "Shared", render, hashlib.sha256(first.read_bytes()).hexdigest())
    sibling = bundle.parent / "reference"
    sibling.mkdir(parents=True, exist_ok=True)
    if hasattr(Path, "hardlink_to"):
        (sibling / "shot.png").hardlink_to(render)
    if not (sibling / "shot.png").exists():  # pragma: no cover - platform without hard links
        pytest.skip("hard links unavailable, so the two manifests cannot name one physical file")
    write_manifest_for(sibling, "Shared", sibling / "shot.png", hashlib.sha256(second.read_bytes()).hexdigest())
    assert (sibling / "shot.png").samefile(render)

    report = crr.scan(bundle)
    rows = {(unit["unit"], page["source_object"]): page for unit in report["units"] for page in unit["pages"]}
    assert rows[("One", "Shared")]["readiness"] != "ready"
    assert rows[("Two", "Shared")]["readiness"] != "ready"
    assert report["pages_ready"] == 0
    assert crr.main([str(bundle), "--quiet"]) == 1
