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
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bundle_corpus  # noqa: E402  # pylint: disable=wrong-import-position
import check_reference_readiness as crr  # noqa: E402  # pylint: disable=wrong-import-position
import package_filesystem  # noqa: E402  # pylint: disable=wrong-import-position
import package_role_identity  # noqa: E402  # pylint: disable=wrong-import-position

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
    """A genuine, parseable PNG - not a signature stub, and distinct per file.

    The round-1 fixtures wrote 8 bytes and asserted READY, so they encoded the very assumption the
    gate was supposed to refuse. Anything claiming to be evidence in this file is a real image.

    ⚠️ The pixels are seeded from the file NAME so two fixture renders are never byte-identical.
    Round 5 keys exclusivity on the verified content digest, and real captures of two different
    worksheets do not collide - a fixture that emitted identical bytes for every page would trip
    exclusivity everywhere and model a situation that does not occur.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    seed = zlib.crc32(path.name.encode("utf-8")) & 0xFF
    raw = b"".join(b"\x00" + bytes((x * 7 + y * 13 + seed) % 256 for x in range(width * 3)) for y in range(height))
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


def write_report(root: Path, unit: str, page_ids: list[str], *, base: str = "pbip") -> Path:
    """A PBIR report shipping the given page ids under ``<root>/<base>/<unit>.Report``.

    ``base`` is ``pbip`` for an engine bundle and ``fabric`` for a handover package - the packager
    copies `pbip/<Unit>/` to `fabric/`, and `bundle_corpus.shipping_reports` scans `pbip/` in
    preference when it exists, so a fixture carrying both would be a shape nothing produces.
    """
    report = root / base / unit / f"{unit}.Report" if base == "pbip" else root / base / f"{unit}.Report"
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
    workbook_luid: str | None = None,
    entry_luid: str | None = None,
    state_luid: str | None = None,
) -> Path:
    """A `reference/manifest.json`. Each entry is ``(name, provider, capabilities)``.

    Mirrors the real producer, which records `sha256` and `dimensions` per state
    (`capture_tableau_reference.py:246-257`). Round-2 review measured the gate ignoring both, so a
    captured image could be swapped wholesale and readiness survived; a fixture that omitted them
    could not have caught it. `record_integrity=False` exists to test that omission is a rejection.

    Note the manifest's top-level key is `dashboards`, but `capture_tableau_reference.py:199` files
    WORKSHEET thumbnails there too - which is why the key cannot be evidence of scope.

    ``workbook_luid`` writes the manifest-level LUID key `check_unit._declared_workbook` already
    reads. The producer does not write it today; a manifest carrying one is enriched or hand-edited,
    which is exactly the shape the round-N contradiction finding used.

    ``entry_luid`` and ``state_luid`` write the SAME key at the two narrower scopes both gates also
    read. They exist because round 2 of PR #454 found the readers selecting one scope's claim by
    precedence and discarding the others - a fixture that could only write one scope cannot express
    a multi-scope contradiction, and so could not have caught it.
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
        if state_luid is not None:
            state["workbook_luid"] = state_luid
        if record_integrity:
            state |= {
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob),
                "dimensions": {"w": size[0], "h": size[1], "dpr": 2},
            }
        entry: dict = {"name": name, "states": [state]}
        if entry_luid is not None:
            entry["workbook_luid"] = entry_luid
        dashboards.append(entry)
    payload: dict = {"source_workbook_sha256": source_sha, "dashboards": dashboards}
    if workbook_luid is not None:
        payload["workbook_luid"] = workbook_luid
    (reference / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return reference


UNIT_LUID = "adc431bb-aeeb-43fe-8ecb-092d4bae8bfa"
OTHER_LUID = "007f70ac-bf40-4838-9d73-134d40f504db"


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


def write_package_manifest(package: Path, *, files: dict[str, str] | None = None, **manifest: Any) -> Path:
    """Give a package the truthful `contents.files` map its producer would have written.

    The entry gate now verifies that map before it reads any evidence (issue #562 S1), so a package
    fixture whose marker is an empty `{}` is a DAMAGED package rather than a shorthand for "this is a
    package". Every fixture below that must reach the current behaviour therefore declares its own
    bytes. The manifest excludes itself, exactly as `package_unit.py` does.

    ``manifest`` carries the rest of the producer's record - `unit`, `kind`, `artifacts`,
    `model_binding` - which S2 reads as this package's ROLE declarations. A fixture that omits them
    is a package that declares no roles, which is a legitimate (and blocked) shape rather than a
    shorthand for a complete one.
    """
    if files is None:
        files = {
            str(path.relative_to(package).as_posix()): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(package.rglob("*"))
            if path.is_file() and path.name != bundle_corpus.PACKAGE_MARKER
        }
    marker = package / bundle_corpus.PACKAGE_MARKER
    payload = {"unit": package.name, **manifest, "contents": {"files": files}}
    marker.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return marker


def build_package(  # pylint: disable=too-many-locals
    package: Path,
    unit: str,
    *,
    worksheets: list[str],
    dashboards: dict[str, list[str]] | None = None,
    oracle_views: list[dict] | None = None,
    luid: str | None = UNIT_LUID,
) -> str:
    """A ROLE-COMPLETE package: `build_unit`'s bundle content plus every role #562 S2 requires.

    Returns the source sha256, exactly as :func:`build_unit` does.

    ⚠️ Written as one builder rather than sprinkled through the fixtures on purpose. The entry gate
    now composes three questions - boundary, bytes, roles - and a fixture that answers only the
    first two is not "a package"; it is a package that would be refused, which makes it useless as
    the positive control the tests below need. Each negative control turns exactly ONE of these
    knobs off, so what it proves stays legible.
    """
    package.mkdir(parents=True, exist_ok=True)
    (package.parent / "assets").mkdir(parents=True, exist_ok=True)
    sha = build_unit(package, unit, worksheets=worksheets, dashboards=dashboards, luid=luid, base="fabric")

    asset = package.parent / "assets" / f"{unit}.twb"
    (package / "assets").mkdir(exist_ok=True)
    shutil.copy2(asset, package / "assets" / asset.name)
    (package / "migration-spec.json").write_text(
        json.dumps({"source": {"file_name": asset.name}, "data_sources": []}), encoding="utf-8"
    )
    (package / "migration-spec.schema.json").write_text(json.dumps({"$id": "migration-spec"}), encoding="utf-8")
    (package / "migration-brief.md").write_text(
        f'+++\nschema = "phase1-start-ready/v1"\nunit = "{unit}"\nscope = "model_and_report"\n+++\n\nMigrate it.\n',
        encoding="utf-8",
    )
    _stamp_scope(package / "report.json", unit)
    _stamp_scope(package / "source-provenance.json", unit)
    _stamp_scope(package / "handover" / f"{unit}.json", unit)

    model = package / "fabric" / f"{unit}.SemanticModel" / "definition"
    model.mkdir(parents=True, exist_ok=True)
    (model / "model.tmdl").write_text("model Model\n", encoding="utf-8")
    (package / "fabric" / f"{unit}.Report" / "definition.pbir").write_text(
        json.dumps({"version": "4.0", "datasetReference": {"byPath": {"path": f"../{unit}.SemanticModel"}}}),
        encoding="utf-8",
    )
    (package / "fabric" / f"{unit}.pbip").write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
    (package / "engine-output-receipt.json").write_text(
        json.dumps(
            {
                "engine": {"version": "2.339.0"},
                "artifacts": [
                    {"path": path.relative_to(package).as_posix()}
                    for path in sorted((package / "fabric").rglob("*"))
                    if path.is_file()
                ],
                "scope": {"unit": unit},
            }
        ),
        encoding="utf-8",
    )
    if oracle_views is not None:
        write_oracle(package, oracle_views)
    return sha


def seal_package(package: Path, unit: str) -> Path:
    """Write the manifest LAST, declaring every role and every byte now in ``package``."""
    return write_package_manifest(
        package,
        unit=unit,
        kind="workbook",
        artifacts={
            "migration_spec": "migration-spec.json",
            "migration_spec_schema": "migration-spec.schema.json",
            "migration_brief": "migration-brief.md",
            "asset": f"assets/{unit}.twb",
            "report": f"fabric/{unit}.Report",
            "model": f"fabric/{unit}.SemanticModel",
            "handover": f"handover/{unit}.json",
        },
        model_binding={"kind": "byPath", "path": f"../{unit}.SemanticModel", "resolves_in_package": True},
    )


def _stamp_scope(path: Path, unit: str) -> None:
    """Add the packager's own `scope.unit` stamp - the claim that this artifact is THIS unit's."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["scope"] = {"unit": unit}
    path.write_text(json.dumps(payload), encoding="utf-8")


def build_unit(  # pylint: disable=too-many-arguments
    bundle: Path,
    unit: str,
    *,
    worksheets: list[str],
    dashboards: dict[str, list[str]] | None = None,
    page_ids: list[str] | None = None,
    viz_fidelity: list[dict] | None = None,
    pbip_warnings: list[str] | None = None,
    luid: str | None = UNIT_LUID,
    base: str = "pbip",
) -> str:
    """Wire a complete workbook unit and return its source sha256, which evidence must carry."""
    source = write_workbook(bundle.parent / "assets" / f"{unit}.twb", worksheets=worksheets, dashboards=dashboards)
    write_engine_report(bundle, workbooks=[unit])
    write_handover(bundle, unit, source_id=str(source), viz_fidelity=viz_fidelity, pbip_warnings=pbip_warnings)
    if page_ids is None:
        page_ids = [obj.page_id for obj in crr.source_objects(source) or []]
    write_report(bundle, unit, page_ids, base=base)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if luid is not None:
        (bundle / "source-provenance.json").write_text(
            json.dumps(
                {
                    "inputs": [
                        {
                            "input": {"file": source.name, "sha256": digest},
                            "origin": {
                                "workbook_luid": luid,
                                "workbook_name": unit,
                                "matched_by": "luid",
                                "match": "name_only",
                                "revision_match": "same",
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
    return digest


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
        render_digest="deadbeef",
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
    write_oracle(bundle, [{"view_name": "Revenue Trend", "workbook_luid": UNIT_LUID}])

    page = crr.scan(bundle)["units"][0]["pages"][0]
    assert page["readiness"] == "unverifiable"
    assert "unknown" in page["matched_by"]


def test_an_oracle_record_typed_unknown_cannot_satisfy_a_page(bundle: Path) -> None:
    """PR #422 fails closed to `unknown` when the Metadata API is disabled; so must this."""
    build_unit(bundle, "WB", worksheets=["Revenue Trend"])
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "unknown", "workbook_luid": UNIT_LUID}])

    assert crr.scan(bundle)["units"][0]["pages"][0]["readiness"] == "unverifiable"


def test_an_oracle_record_typed_worksheet_still_cannot_satisfy_a_dashboard_page(bundle: Path) -> None:
    """The scope join applies to the oracle route too, not only to `reference/`."""
    build_unit(bundle, "WB", worksheets=["Ops"], dashboards={"Ops": ["Ops"]})
    write_oracle(bundle, [{"view_name": "Ops", "view_type": "worksheet", "workbook_luid": UNIT_LUID}])

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
    write_oracle(bundle, [{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_luid": UNIT_LUID}])

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


def test_the_same_render_under_two_names_is_still_one_render(bundle: Path) -> None:
    """Exclusivity keys on the VERIFIED CONTENT DIGEST, so it needs no path and no filesystem trick.

    Round 4 keyed on `evidence_path` text and left both pages ready when one physical PNG was named
    two ways. Round 5 found the replacement fell back to a resolved path whenever `st_ino` was
    unavailable - which cannot see a hard link or a drive alias - and, worse, that the covering test
    SKIPPED on exactly the filesystems where the fallback ran. Content identity has no fallback and
    no skip: it is the same everywhere.
    """
    sha = build_unit(bundle, "WB", worksheets=["Alpha", "Beta"])
    reference = write_reference(
        bundle,
        [("Alpha", "embedded_thumbnail", ["layout_grade"]), ("Beta", "embedded_thumbnail", ["layout_grade"])],
        source_sha=sha,
    )
    assert crr.scan(bundle)["status"] == "READY"

    # Two DISTINCT files whose bytes are identical - the case a path can never detect.
    manifest = json.loads((reference / "manifest.json").read_text(encoding="utf-8"))
    (reference / "shot-1.png").write_bytes((reference / "shot-0.png").read_bytes())
    manifest["dashboards"][1]["states"][0]["sha256"] = manifest["dashboards"][0]["states"][0]["sha256"]
    manifest["dashboards"][1]["states"][0]["bytes"] = manifest["dashboards"][0]["states"][0]["bytes"]
    (reference / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert not (reference / "shot-0.png").samefile(reference / "shot-1.png")

    rows = {page["source_object"]: page for page in crr.scan(bundle)["units"][0]["pages"]}
    assert rows["Alpha"]["readiness"] != "ready"
    assert rows["Beta"]["readiness"] != "ready"
    assert crr.main([str(bundle), "--quiet"]) == 1


def test_exclusivity_never_falls_back_to_comparing_paths(bundle: Path) -> None:
    """The render key must be content, not a path - measured as a property, not read from the code.

    `C:\\Windows\\System32\\notepad.exe` and `C:\\Windows\\notepad.exe` are hard links whose resolved,
    case-folded paths DIFFER, so any path-derived key splits one physical file into two. The key this
    gate uses does not contain a path at all.
    """
    sha = build_unit(bundle, "WB", worksheets=["Solo"])
    reference = write_reference(bundle, [("Solo", "embedded_thumbnail", ["layout_grade"])], source_sha=sha)
    page = crr.scan(bundle)["units"][0]["pages"][0]

    digest = hashlib.sha256((reference / "shot-0.png").read_bytes()).hexdigest()
    assert page["render_key"] == digest
    assert "shot-0" not in page["render_key"]
    assert str(reference).casefold() not in page["render_key"].casefold()


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

    Both manifests are individually valid - correct source sha, honest hash, real image - and each is
    legitimately attributable to its own unit. Only the CROSS-UNIT view shows one render credited
    twice, which is why the check runs once over every row.
    """
    first = write_workbook(bundle.parent / "assets" / "One.twb", worksheets=["Shared"])
    second = write_workbook(bundle.parent / "assets" / "Two.twb", worksheets=["Shared"])
    # The two workbooks must genuinely DIFFER, or they hash identically and each manifest attaches to
    # both units - which makes the pages ambiguous and the test pass without reaching exclusivity.
    # Measured: that is exactly what the first version of this fixture did.
    second.write_text(second.read_text(encoding="utf-8") + "<!-- second -->", encoding="utf-8")
    assert hashlib.sha256(first.read_bytes()).hexdigest() != hashlib.sha256(second.read_bytes()).hexdigest()
    write_engine_report(bundle, workbooks=["One", "Two"])
    for unit, source in (("One", first), ("Two", second)):
        write_handover(bundle, unit, source_id=str(source))
        write_report(bundle, unit, [obj.page_id for obj in crr.source_objects(source) or []])

    # `_default_dirs` looks in <bundle>/reference and <bundle>/../reference, so two manifests can
    # coexist - one per unit - while naming renders with IDENTICAL content.
    render = bundle / "reference" / "shot.png"
    write_png(render, 320, 240)
    write_manifest_for(bundle / "reference", "Shared", render, hashlib.sha256(first.read_bytes()).hexdigest())
    sibling = bundle.parent / "reference"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "shot.png").write_bytes(render.read_bytes())
    write_manifest_for(sibling, "Shared", sibling / "shot.png", hashlib.sha256(second.read_bytes()).hexdigest())

    report = crr.scan(bundle)
    rows = {(unit["unit"], page["source_object"]): page for unit in report["units"] for page in unit["pages"]}
    assert rows[("One", "Shared")]["readiness"] != "ready"
    assert rows[("Two", "Shared")]["readiness"] != "ready"
    assert report["pages_ready"] == 0
    assert crr.main([str(bundle), "--quiet"]) == 1


# --------------------------------------------------------------------------------------------
# Evidence discovery: the walk-up is a UNION, and a self-contained package must stop it (#451)
# --------------------------------------------------------------------------------------------


def test_a_non_packaged_target_still_finds_an_ancestors_oracle(tmp_path: Path) -> None:
    """The control that makes the package test below meaningful rather than a deletion.

    `_default_dirs` looks beside the target, beside its parent AND beside its grandparent, because an
    un-packaged unit under `<bundle>/pbip/<Unit>/` finds the run's flat capture that way. Removing
    the walk-up would break the ordinary case, so it has to keep working IN THE SAME RUN as the
    package that must not inherit.
    """
    target = tmp_path / "run" / "bundle" / "unit"
    target.mkdir(parents=True)
    (tmp_path / "run" / "_oracle").mkdir()

    assert crr._default_dirs(target, "_oracle") == [tmp_path / "run" / "_oracle"]


def test_a_self_contained_package_does_not_inherit_an_ancestors_oracle(tmp_path: Path) -> None:
    """A `package-manifest.json` beside the target stops the walk (issue #451).

    `package_unit.py` writes a unit-scoped `oracle/oracle-manifest.json` holding THIS unit's views
    with rewritten paths. A package assembled INSIDE a run directory therefore saw its own copy AND
    the run's flat capture two levels up, every view matched twice, and the gate refused the pair as
    "2 records share this name once normalized" - taking every page from ready to unverifiable,
    silently, so packaging was strictly worse than not packaging.
    """
    target = tmp_path / "run" / "packages" / "unit"
    (target / "oracle").mkdir(parents=True)
    (target / "package-manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "run" / "oracle").mkdir()

    assert crr._default_dirs(target, "oracle") == [target / "oracle"]


def test_a_package_with_no_evidence_of_its_own_still_inherits_nothing(tmp_path: Path) -> None:
    """Kills: "stop only when the package has its own copy", which re-opens the union by accident.

    A package that omitted a render because it could not attribute it must NOT then pick that render
    up from the ancestor - that is the omission being undone by the consumer, which is how a
    fail-closed packaging decision would turn back into a fail-open one.
    """
    target = tmp_path / "run" / "packages" / "unit"
    target.mkdir(parents=True)
    (target / "package-manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "run" / "oracle").mkdir()

    assert crr._default_dirs(target, "oracle") == []


def test_a_packaged_unit_reads_only_its_own_manifest_end_to_end(tmp_path: Path) -> None:
    """The same defect at gate level: the doubled record must not reach the readiness verdict.

    Both manifests describe the SAME view of the SAME workbook, which is what a package inside a run
    directory produces; the ancestor copy is the one that must be ignored.
    """
    package = tmp_path / "run" / "unit"
    sha = build_package(
        package,
        "WB",
        worksheets=["Revenue Trend"],
        oracle_views=[{"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_luid": UNIT_LUID}],
    )
    view = {"view_name": "Revenue Trend", "view_type": "worksheet", "workbook_luid": UNIT_LUID}
    write_oracle(tmp_path / "run", [view])
    assert crr.scan(package)["units"][0]["pages"][0]["readiness"] == "unverifiable"

    seal_package(package, "WB")

    report = crr.scan(package)
    assert report["units"][0]["pages"][0]["readiness"] == "ready"
    assert report["evidence_records"] == 1
    assert sha


def test_a_unit_three_levels_below_the_run_still_inherits_the_flat_capture(tmp_path: Path) -> None:
    """MEDIUM 1 from round-1 review of PR #454, at the canonical depth.

    `capture_tableau_oracle.py` writes `_runs/<NNN>-<slug>/oracle/` while an un-packaged unit sits at
    `_runs/<NNN>-<slug>/bundle/pbip/<Unit>/` - THREE ancestors below it. Stopping at one (the exit
    gate) or two (this one) makes a real capture invisible for the ordinary engine-bundle shape, so
    the depth is part of the shared rule rather than each gate's guess.
    """
    unit = tmp_path / "run" / "bundle" / "pbip" / "Unit"
    unit.mkdir(parents=True)
    (tmp_path / "run" / "oracle").mkdir()

    assert crr._default_dirs(unit, "oracle") == [tmp_path / "run" / "oracle"]


def test_the_attribution_census_survives_a_multi_path_scan(tmp_path: Path) -> None:
    """MEDIUM 2: `_merge_scans` inherits `dict(reports[0])`, so later refusals used to vanish.

    Measured on two bundles: the second refused 7 records as another workbook's, and the merged
    report said `foreign=0`. A refusal counter that silently zeroes is worse than none, because it
    reads as "nothing was refused" exactly where the guard did the most work.
    """
    clean, dirty = tmp_path / "clean", tmp_path / "dirty"
    for root in (clean, dirty):
        (root / "bundle").mkdir(parents=True)
        (root / "assets").mkdir(parents=True)
    build_unit(clean / "bundle", "Book", worksheets=["Revenue"])
    write_oracle(clean / "bundle", [{"view_name": "Revenue", "view_type": "worksheet", "workbook_name": "Book"}])
    build_unit(dirty / "bundle", "Other", worksheets=["Revenue"])
    write_oracle(
        dirty / "bundle",
        [{"view_name": f"V{i}", "view_type": "worksheet", "workbook_luid": OTHER_LUID} for i in range(7)],
    )

    merged = crr._merge_scans([crr.scan(clean / "bundle"), crr.scan(dirty / "bundle")])

    assert merged["evidence_attributed"]["foreign"] == 7
    assert merged["evidence_attributed"]["name"] == 1
    assert "refused 7 as another" in crr.render(merged)


def test_every_integer_counter_survives_a_merge_by_construction(tmp_path: Path) -> None:
    """B-C, fixed as a CLASS: the merge must not carry a hand-written list of keys to sum.

    ⚠️ This is the third instance of one shape in PR #454 - `evidence_attributed` zeroed on merge
    (round-1 MEDIUM 2), then `pages_revision_unconfirmed` did the same because it was added after the
    list was written. Patching a third key by name would guarantee a fourth, so this asserts the
    PROPERTY rather than any key: every integer counter in a single scan is summed, whatever it is
    called and whenever it was added.
    """
    first, second = tmp_path / "one", tmp_path / "two"
    for root, unit in ((first, "One"), (second, "Two")):
        (root / "bundle").mkdir(parents=True)
        (root / "assets").mkdir(parents=True)
        build_unit(root / "bundle", unit, worksheets=["Revenue"])
        # Strip `revision_match` so the page is REVISION-UNCONFIRMED: the counter this test exists
        # for must be NON-ZERO, or `0 != 0 + 0` is false and the assertion proves nothing.
        provenance = root / "bundle" / "source-provenance.json"
        payload = json.loads(provenance.read_text(encoding="utf-8"))
        del payload["inputs"][0]["origin"]["revision_match"]
        provenance.write_text(json.dumps(payload), encoding="utf-8")
        write_oracle(root / "bundle", [{"view_name": "Revenue", "view_type": "worksheet", "workbook_luid": UNIT_LUID}])
    reports = [crr.scan(first / "bundle"), crr.scan(second / "bundle")]

    merged = crr._merge_scans(reports)

    counters = {key for key, value in reports[0].items() if isinstance(value, int) and not isinstance(value, bool)}
    assert reports[0]["pages_revision_unconfirmed"] == 1, "the counter must be non-zero or the check is vacuous"
    assert "pages_revision_unconfirmed" in counters, "the counter this was written for must be in scope"
    assert len(counters) > 10, "sanity: the report really does carry a family of counters"
    unsummed = {key for key in counters if merged[key] != reports[0][key] + reports[1][key]}
    assert unsummed == set(), f"a counter silently dropped on merge: {sorted(unsummed)}"
    # `bool` is an `int` subclass, so the conjunction must NOT have been tallied into 2.
    assert merged["all_evidence_validation_grade"] in (True, False)


# --------------------------------------------------------------------------------------------
# Package-boundary classification runs BEFORE resolve and before any discovery (issue #562)
# --------------------------------------------------------------------------------------------


class _Continued(Exception):
    """Raised at the exact call site where `scan` continued past an unsafe classification.

    Deliberately not `AssertionError`: it is caught inside the patched window so the patches are
    undone before pytest formats anything (see :func:`_scan_forbidding_discovery`).
    """


def _link_directory(link: Path, target: Path) -> None:
    """A junction (Windows) or a directory symlink (POSIX) - a reparse point either way."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        completed = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if completed.returncode != 0:
            pytest.skip(f"could not create junction: {completed.stderr.decode(errors='replace').strip()}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege-dependent
        pytest.skip("this platform/account cannot create symlinks without elevation")


def _forbid_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explode on every step `scan` must not reach for an unsafe target.

    `Path.resolve` is included deliberately: the ORDER is the invariant. `resolve()` follows a
    junction, so classifying after it would already have answered the boundary question about a
    directory the caller never named.
    """

    def boom(*_args: object, **_kwargs: object) -> object:
        raise _Continued("scan continued past an unsafe package classification")

    monkeypatch.setattr(Path, "resolve", boom)
    monkeypatch.setattr(crr, "_collect_evidence", boom)
    monkeypatch.setattr(crr, "_engine_report", boom)
    monkeypatch.setattr(crr, "shipping_reports", boom)
    monkeypatch.setattr(crr, "resolve_source", boom)


def _scan_forbidding_discovery(unit: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict | None, str]:
    """Scan with every downstream step armed, then disarm before any assertion can escape.

    ⚠️ `Path.resolve` is patched globally and **pytest uses it while formatting a failure
    traceback**, so a kill that escaped the patched window would surface as an `INTERNALERROR` -
    infrastructure breakage rather than this test failing. Measured while mutation-testing these
    controls; the sentinel is caught here and the patches are undone before anything is asserted.
    """
    _forbid_discovery(monkeypatch)
    try:
        return crr.scan(unit), ""
    except _Continued as exc:
        return None, str(exc)
    finally:
        monkeypatch.undo()


def _main_forbidding_following(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int | None, str]:
    """Run the CLI with every FOLLOWING pre-check armed, disarming before anything is asserted.

    ⚠️ `is_dir()` and `is_file()` are the CLI's own pre-checks and both dereference, so arming them
    is what proves the ORDER: a refusal that comes back while they are still armed cannot have
    consulted them. Same sentinel discipline as :func:`_scan_forbidding_discovery` - pytest formats
    tracebacks with these very primitives.
    """

    def boom(*_args: object, **_kwargs: object) -> object:
        raise _Continued("main ran a following pre-check before classifying the target")

    monkeypatch.setattr(Path, "is_dir", boom)
    monkeypatch.setattr(Path, "is_file", boom)
    monkeypatch.setattr(Path, "resolve", boom)
    try:
        return crr.main(argv), ""
    except _Continued as exc:
        return None, str(exc)
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize("spelling", [("packages", "Minimal"), ("packages", "batch1", "Minimal")])
def test_main_refuses_a_missing_package_shaped_root_with_the_typed_exit_instead_of_argparse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: tuple[str, ...]
) -> None:
    """Kills: pre-checking with `is_dir()` before classifying.

    The CLI used to reach `path.is_dir()` first, so a package-shaped root with no boundary left via
    `parser.error` - **exit 2, with the supplied path echoed** - instead of the typed exit 3.
    """
    unit = tmp_path.joinpath("run", *spelling)

    code, followed = _main_forbidding_following([str(unit), "--quiet"], monkeypatch)

    assert followed == "", followed
    assert code == crr.EXIT_CANNOT_ESTABLISH


def test_main_refuses_an_unassessable_package_shaped_root_with_the_typed_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An lstat error is indeterminate, so it is exit 3 - never argparse's exit 2, never a pass."""
    unit = tmp_path / "run" / "packages" / "Minimal"
    (unit / "fabric").mkdir(parents=True)
    real_lstat = bundle_corpus.os.lstat

    def deny(path, *args, **kwargs):
        if Path(path) == unit:
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(bundle_corpus.os, "lstat", deny)

    code, followed = _main_forbidding_following([str(unit), "--quiet"], monkeypatch)

    assert followed == "", followed
    assert code == crr.EXIT_CANNOT_ESTABLISH


def test_main_refuses_an_unsafe_root_alias_before_its_own_pre_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An aliased root is refused by classification, not by a pre-check that would have followed it."""
    destination = tmp_path / "real" / "packages" / "Minimal"
    (destination / "fabric").mkdir(parents=True)
    (destination / bundle_corpus.PACKAGE_MARKER).write_text("{}\n", encoding="utf-8")
    alias = tmp_path / "alias" / "packages" / "Minimal"
    _link_directory(alias, destination)

    code, followed = _main_forbidding_following([str(alias), "--quiet"], monkeypatch)

    assert followed == "", followed
    assert code == crr.EXIT_CANNOT_ESTABLISH


def test_main_prints_the_stable_code_and_no_supplied_path_for_a_refused_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rendered CLI verdict is what gets pasted into an issue; the target can be secret-bearing."""
    unit = tmp_path / "customer-secret-server" / "packages" / "Minimal"

    code = crr.main([str(unit)])
    printed = capsys.readouterr().out

    assert code == crr.EXIT_CANNOT_ESTABLISH
    assert bundle_corpus.CODE_PACKAGE_ROOT_MISSING in printed
    assert "customer-secret-server" not in printed
    assert str(tmp_path) not in printed


def test_main_keeps_argparses_verdict_for_an_ORDINARY_missing_path(tmp_path: Path) -> None:
    """Compatibility control: only a package-shaped or unsafe target is diverted.

    ⚠️ Without this, classifying everything as refusable would satisfy the controls above while
    silently swallowing the ordinary typo case that argparse is right to reject.
    """
    with pytest.raises(SystemExit) as exit_info:
        crr.main([str(tmp_path / "run" / "bundle" / "NeverBuilt"), "--quiet"])

    assert exit_info.value.code == 2


def test_a_package_shaped_target_with_no_marker_blocks_before_resolve_and_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flat placement, missing boundary: CANNOT_ESTABLISH, and nothing downstream may fire."""
    unit = tmp_path / "run" / "packages" / "Minimal"
    (unit / "fabric").mkdir(parents=True)

    report, continued = _scan_forbidding_discovery(unit, monkeypatch)

    assert continued == "", continued
    assert report is not None
    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert report["units_cannot_establish"] == 1
    assert report["pages_expected"] == 0
    assert bundle_corpus.CODE_PACKAGE_MARKER_MISSING in report["units"][0]["detail"]


def test_a_nested_package_shaped_target_with_no_marker_blocks_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nested `<...>/packages/<batch>/<Unit>` spelling is the same boundary claim."""
    unit = tmp_path / "run" / "packages" / "batch1" / "Minimal"
    (unit / "fabric").mkdir(parents=True)

    report, continued = _scan_forbidding_discovery(unit, monkeypatch)

    assert continued == "", continued
    assert report is not None
    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert bundle_corpus.CODE_PACKAGE_MARKER_MISSING in report["units"][0]["detail"]


def test_a_marker_that_is_a_directory_blocks_before_anything_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-regular marker is damaged, never non-package - so it never falls back to bundle handling."""
    unit = tmp_path / "run" / "packages" / "Minimal"
    (unit / bundle_corpus.PACKAGE_MARKER).mkdir(parents=True)

    report, continued = _scan_forbidding_discovery(unit, monkeypatch)

    assert continued == "", continued
    assert report is not None
    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert bundle_corpus.CODE_PACKAGE_MARKER_NOT_REGULAR in report["units"][0]["detail"]


def test_a_linked_root_blocks_even_when_its_destination_is_a_valid_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strongest control: following the alias would have reported an INTACT boundary."""
    destination = tmp_path / "real" / "packages" / "Minimal"
    (destination / "fabric").mkdir(parents=True)
    (destination / bundle_corpus.PACKAGE_MARKER).write_text("{}\n", encoding="utf-8")
    alias = tmp_path / "alias" / "packages" / "Minimal"
    _link_directory(alias, destination)

    report, continued = _scan_forbidding_discovery(alias, monkeypatch)

    assert continued == "", continued
    assert report is not None
    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert bundle_corpus.CODE_TARGET_ROOT_REPARSE in report["units"][0]["detail"]


def test_an_aliased_ORDINARY_bundle_fails_closed_and_the_real_path_still_works(tmp_path: Path) -> None:
    """The documented compatibility cost of refusing a caller-supplied root alias.

    Fail-closed by choice: the operator passes the real path, which is loud and recoverable. The
    fail-open alternative decides a boundary about a directory nobody named.
    """
    bundle = tmp_path / "run" / "bundle"
    bundle.mkdir(parents=True)
    (tmp_path / "run" / "assets").mkdir()
    build_unit(bundle, "Minimal", worksheets=["Revenue"])
    write_oracle(bundle, [{"view_name": "Revenue", "view_type": "worksheet", "workbook_luid": UNIT_LUID}])
    alias = tmp_path / "alias-bundle"
    _link_directory(alias, bundle)

    assert crr.main([str(alias), "--quiet"]) == crr.EXIT_CANNOT_ESTABLISH
    assert crr.main([str(bundle), "--quiet"]) == crr.EXIT_OK


def test_an_unassessable_root_blocks_rather_than_reading_as_an_ordinary_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No exception-shaped success: an lstat error is non-clean, not 'ordinary'."""
    unit = tmp_path / "run" / "packages" / "Minimal"
    (unit / "fabric").mkdir(parents=True)
    real_lstat = bundle_corpus.os.lstat

    def deny(path, *args, **kwargs):
        if Path(path) == unit:
            raise PermissionError(13, "denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(bundle_corpus.os, "lstat", deny)

    report, continued = _scan_forbidding_discovery(unit, monkeypatch)

    assert continued == "", continued
    assert report is not None
    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert bundle_corpus.CODE_TARGET_ROOT_UNASSESSABLE in report["units"][0]["detail"]


def test_the_refusal_detail_carries_a_stable_code_and_no_host_path(tmp_path: Path) -> None:
    """These verdicts get pasted into issues; the supplied target can be secret-bearing."""
    unit = tmp_path / "customer-secret-server" / "packages" / "Minimal"
    (unit / "fabric").mkdir(parents=True)

    detail = crr.scan(unit)["units"][0]["detail"]

    assert bundle_corpus.CODE_PACKAGE_MARKER_MISSING in detail
    assert "customer-secret-server" not in detail
    assert str(tmp_path) not in detail


def test_a_SAFE_package_continues_into_the_current_behaviour_unchanged(tmp_path: Path) -> None:
    """The classifier is a precondition, not a new verdict: an intact package still reports READY.

    ⚠️ This is the vacuity control for every block above. Without it, classifying EVERYTHING as
    unsafe would satisfy them all - and, since #562, refusing every package on integrity or
    role/identity grounds would too. This package is TRUTHFUL and role-complete, so all three
    preconditions are satisfied and the gate must behave exactly as it did before any were added.
    """
    unit = tmp_path / "run" / "packages" / "Minimal"
    build_package(
        unit,
        "Minimal",
        worksheets=["Revenue"],
        oracle_views=[{"view_name": "Revenue", "view_type": "worksheet", "workbook_luid": UNIT_LUID}],
    )
    seal_package(unit, "Minimal")

    report = crr.scan(unit)

    assert report["status"] == crr.STATUS_READY
    assert report["pages_ready"] == report["pages_expected"] == 1
    # A clean package RECORDS its verification rather than leaving the field absent: "not a package"
    # and "a package that verified clean" must not share one representation.
    assert [block["status"] for block in report["package_integrity"]] == [package_filesystem.STATUS_CLEAN]
    assert [block["verdict"] for block in report["role_identity"]] == ["START_READY"]


# --------------------------------------------------------------------------------------------
# Package filesystem/manifest integrity at ENTRY (issue #562, slice S1)
#
# The classifier above answers "is this a package boundary, and is it intact enough to reason
# about". These answer the next question, and only that one: does the manifest still describe the
# bytes in the package? A package that has gained, lost or changed a file is refused BEFORE any
# evidence is collected and before source resolution can rescue it with an asset the manifest never
# accounted for.
# --------------------------------------------------------------------------------------------


def _packaged_unit(tmp_path: Path) -> Path:
    """An intact, ROLE-COMPLETE, evidence-carrying package describing exactly its own bytes.

    Role-complete since #562 S2: the entry gate now refuses a package whose roles or identity do not
    hold, so a fixture that only satisfied S1 would be refused for a reason these tests are not
    about - and every S1 assertion below would then be vacuous.
    """
    unit = tmp_path / "run" / "packages" / "Minimal"
    build_package(
        unit,
        "Minimal",
        worksheets=["Revenue"],
        oracle_views=[{"view_name": "Revenue", "view_type": "worksheet", "workbook_luid": UNIT_LUID}],
    )
    seal_package(unit, "Minimal")
    return unit


def test_an_extra_file_in_a_package_blocks_before_resolve_and_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ORDER claim, at the integrity hook: nothing downstream may fire for a damaged package.

    A file nobody declared means this composition is not the one the producer described, so evidence
    found inside it cannot be attributed - and source resolution must never get the chance to find
    the undeclared asset and call the package usable.
    """
    unit = _packaged_unit(tmp_path)
    (unit / "_oracle" / "stray.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    report, continued = _scan_forbidding_discovery(unit, monkeypatch)

    assert continued == "", continued
    assert report is not None
    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert report["units_cannot_establish"] == 1
    assert report["pages_expected"] == 0
    assert package_filesystem.CODE_FILE_UNDECLARED in report["units"][0]["detail"]


def test_a_changed_file_in_a_package_is_refused_at_entry(tmp_path: Path) -> None:
    """Presence is not integrity: the recorded digest is what makes the manifest a description."""
    unit = _packaged_unit(tmp_path)
    assert crr.main([str(unit), "--quiet"]) == crr.EXIT_OK

    (unit / "report.json").write_text('{"workbooks": [{"name": "Tampered"}]}', encoding="utf-8")

    assert crr.main([str(unit), "--quiet"]) == crr.EXIT_CANNOT_ESTABLISH


def test_a_malformed_manifest_is_refused_at_entry(tmp_path: Path) -> None:
    """A manifest that cannot be parsed describes nothing, so nothing about this package is known."""
    unit = _packaged_unit(tmp_path)
    (unit / bundle_corpus.PACKAGE_MARKER).write_text('{"contents": {"files": {', encoding="utf-8")

    report = crr.scan(unit)

    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert package_filesystem.CODE_MANIFEST_NOT_JSON in report["units"][0]["detail"]
    assert [block["status"] for block in report["package_integrity"]] == [package_filesystem.STATUS_FINDINGS]


def test_the_integrity_refusal_carries_stable_codes_and_no_host_path(tmp_path: Path) -> None:
    """These verdicts are pasted into issues; the supplied target can be secret-bearing."""
    unit = _packaged_unit(tmp_path / "customer-secret-server")
    (unit / "extra.txt").write_text("x\n", encoding="utf-8")

    report = crr.scan(unit)
    printed = crr.render(report)

    assert package_filesystem.CODE_FILE_UNDECLARED in report["units"][0]["detail"]
    assert "customer-secret-server" not in printed
    assert str(tmp_path) not in printed


def test_the_package_verifier_runs_exactly_once_for_a_safe_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once, before evidence - not once per unit, not again inside a later rescue path.

    A second invocation would mean a second place where the answer could differ from the one the
    gate acted on, which is the shape the classifier slice was written to remove.
    """
    unit = _packaged_unit(tmp_path)
    calls: list[str] = []
    real = crr.verify_package

    def counted(root, classification):
        calls.append(classification.code)
        return real(root, classification)

    monkeypatch.setattr(crr, "verify_package", counted)

    assert crr.scan(unit)["status"] == crr.STATUS_READY
    assert calls == [bundle_corpus.CODE_PACKAGE_BOUNDARY_OK]


def test_an_ORDINARY_bundle_never_reaches_the_package_verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compatibility control: an un-packaged bundle has no manifest and must keep behaving as before."""
    bundle = tmp_path / "run" / "bundle"
    bundle.mkdir(parents=True)
    (tmp_path / "run" / "assets").mkdir()
    build_unit(bundle, "Minimal", worksheets=["Revenue"])
    write_oracle(bundle, [{"view_name": "Revenue", "view_type": "worksheet", "workbook_luid": UNIT_LUID}])

    def boom(*_args, **_kwargs):
        raise AssertionError("an ordinary bundle was sent to the package verifier")

    monkeypatch.setattr(crr, "verify_package", boom)

    assert crr.scan(bundle)["status"] == crr.STATUS_READY


def test_a_DAMAGED_boundary_is_refused_by_the_classifier_and_never_reinterpreted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing marker is the classifier's verdict, and the integrity slice must not restate it.

    ⚠️ Two guards that can both answer "refuse" for the same target are one guard too many: whichever
    reason reaches the operator first becomes the one they act on, and the other quietly rots.
    """
    unit = tmp_path / "run" / "packages" / "Minimal"
    (unit / "fabric").mkdir(parents=True)

    def boom(*_args, **_kwargs):
        raise AssertionError("a damaged boundary was re-judged by the integrity verifier")

    monkeypatch.setattr(crr, "verify_package", boom)
    report = crr.scan(unit)

    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert bundle_corpus.CODE_PACKAGE_MARKER_MISSING in report["units"][0]["detail"]


def test_a_package_whose_manifest_omits_ROLES_is_still_CLEAN_to_the_bytes_verifier(tmp_path: Path) -> None:
    """The slice boundary, made observable: S1 judges BYTES, S2 judges the role graph.

    Both answers are taken on the same package, in one test, because that is what makes them
    distinguishable: the manifest below describes its bytes perfectly (S1 clean) and declares no
    roles at all (S2 blocked). A reviewer can therefore tell which invariant a future failure
    belongs to, which a single merged verdict would hide.
    """
    unit = _packaged_unit(tmp_path)
    files = json.loads((unit / bundle_corpus.PACKAGE_MARKER).read_text(encoding="utf-8"))["contents"]["files"]
    (unit / bundle_corpus.PACKAGE_MARKER).write_text(
        json.dumps(
            {"unit": "Minimal", "artifacts": {}, "workbook_identity": {"luid": None}, "contents": {"files": files}}
        ),
        encoding="utf-8",
    )

    classification = bundle_corpus.classify_target(unit)
    assert package_filesystem.verify_package(unit, classification).status == package_filesystem.STATUS_CLEAN

    report = crr.scan(unit)
    assert report["status"] == crr.STATUS_FINDINGS
    assert [block["status"] for block in report["package_integrity"]] == [package_filesystem.STATUS_CLEAN]
    assert report["role_identity"][0]["verdict"] == "BLOCKED"


# --------------------------------------------------------------------------------------------
# Required roles and cross-artifact identity at ENTRY (issue #562, slice S2)
#
# S1 answers "do these bytes match the manifest". These answer the next question, and only that
# one: are the roles this package's kind and topology require actually present and declared, and do
# their stable identity claims agree? A package that fails is refused BEFORE any evidence is
# collected, because a render attributed to a unit whose identity does not hold is worse than none.
# The role matrix itself is tested directly in `tests/test_package_role_identity.py`; what belongs
# here is the WIRING: the order, the cohort, and the shape of the verdict this gate renders.
# --------------------------------------------------------------------------------------------


def test_a_role_blocked_package_stops_before_resolve_and_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ORDER claim at the S2 hook: nothing downstream may fire for a package whose roles fail.

    Evidence discovery and source resolution are exactly the steps that would otherwise "rescue" a
    package by attributing renders to a unit it cannot prove it is.
    """
    unit = _packaged_unit(tmp_path)
    manifest = json.loads((unit / bundle_corpus.PACKAGE_MARKER).read_text(encoding="utf-8"))
    manifest["artifacts"]["asset"] = None
    (unit / bundle_corpus.PACKAGE_MARKER).write_text(json.dumps(manifest), encoding="utf-8")

    report, continued = _scan_forbidding_discovery(unit, monkeypatch)

    assert continued == "", continued
    assert report is not None
    assert report["status"] == crr.STATUS_FINDINGS
    assert report["pages_expected"] == 0
    assert report["evidence_records"] == 0
    assert report["role_identity"][0]["verdict"] == "BLOCKED"
    assert package_role_identity.CODE_ROLE_UNDECLARED in report["role_identity"][0]["blockers"]


def test_the_role_verdict_is_reported_as_FINDINGS_not_CANNOT_ESTABLISH(tmp_path: Path) -> None:
    """The two refusals are different answers and must keep different exits.

    S1 refuses because the package cannot be described at all (exit 3, "I have no opinion"). S2
    refuses because it describes itself perfectly well and what it describes is wrong (exit 1, "here
    is the defect"). Collapsing them would tell an operator to investigate when they should fix.
    """
    unit = _packaged_unit(tmp_path)
    manifest = json.loads((unit / bundle_corpus.PACKAGE_MARKER).read_text(encoding="utf-8"))
    manifest["artifacts"]["migration_brief"] = None
    (unit / "migration-brief.md").unlink()
    manifest["contents"]["files"].pop("migration-brief.md")
    (unit / bundle_corpus.PACKAGE_MARKER).write_text(json.dumps(manifest), encoding="utf-8")

    assert crr.main([str(unit), "--quiet"]) == crr.EXIT_FINDINGS


def test_the_role_verifier_runs_ONCE_over_the_whole_cohort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A published consumer cannot prove its provider alone, so the SET is what gets verified.

    One invocation, every clean package in it. Verifying per target would make
    `<provider> <consumer>` mean the same as two separate commands, which is precisely the shape
    that cannot resolve a shared datasource.
    """
    first = _packaged_unit(tmp_path / "a")
    second = _packaged_unit(tmp_path / "b")
    cohorts: list[list[str]] = []
    real = crr.verify_phase1_role_identity

    def counted(roots, **kwargs):
        cohorts.append([Path(root).name for root in roots])
        return real(roots, **kwargs)

    monkeypatch.setattr(crr, "verify_phase1_role_identity", counted)

    assert crr.main([str(first), str(second), "--quiet"]) == crr.EXIT_OK
    assert cohorts == [["Minimal", "Minimal"]], "the cohort must be verified in one call"


def test_a_clean_first_package_does_not_hide_a_role_blocked_SECOND_one(tmp_path: Path) -> None:
    """Same list-of-blocks discipline as `package_integrity`, and for the same measured reason."""
    clean = _packaged_unit(tmp_path / "first")
    blocked = _packaged_unit(tmp_path / "second")
    manifest = json.loads((blocked / bundle_corpus.PACKAGE_MARKER).read_text(encoding="utf-8"))
    manifest["artifacts"]["asset"] = None
    (blocked / bundle_corpus.PACKAGE_MARKER).write_text(json.dumps(manifest), encoding="utf-8")

    merged = crr._merge_scans([crr.scan(clean), crr.scan(blocked)])

    blocks = merged["role_identity"]
    assert merged["status"] == crr.STATUS_FINDINGS
    assert [block["ordinal"] for block in blocks] == [0, 1]
    assert [block["verdict"] for block in blocks] == ["START_READY", "BLOCKED"]


def test_the_role_refusal_carries_stable_codes_and_no_host_path(tmp_path: Path) -> None:
    """These verdicts are pasted into issues; the supplied target can be secret-bearing."""
    unit = _packaged_unit(tmp_path / "customer-secret-server")
    manifest = json.loads((unit / bundle_corpus.PACKAGE_MARKER).read_text(encoding="utf-8"))
    manifest["artifacts"]["asset"] = None
    (unit / bundle_corpus.PACKAGE_MARKER).write_text(json.dumps(manifest), encoding="utf-8")

    printed = crr.render(crr.scan(unit))

    assert package_role_identity.CODE_ROLE_UNDECLARED in printed
    assert "customer-secret-server" not in printed
    assert str(tmp_path) not in printed


def test_an_ORDINARY_bundle_never_reaches_the_role_verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compatibility control: roles are a PACKAGE contract, and a bundle declares none."""
    bundle = tmp_path / "run" / "bundle"
    bundle.mkdir(parents=True)
    (tmp_path / "run" / "assets").mkdir()
    build_unit(bundle, "Minimal", worksheets=["Revenue"])
    write_oracle(bundle, [{"view_name": "Revenue", "view_type": "worksheet", "workbook_luid": UNIT_LUID}])

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an ordinary bundle was sent to the role verifier")

    monkeypatch.setattr(crr, "verify_phase1_role_identity", boom)

    report = crr.scan(bundle)
    assert report["status"] == crr.STATUS_READY
    assert report["role_identity"] == []


# --------------------------------------------------------------------------------------------
# Structured package-integrity evidence survives a MULTI-TARGET merge
#
# `_merge_scans` starts from `dict(reports[0])`, so a single-block field written only on refusal was
# inherited from the FIRST scan and every later target's typed rows were dropped - a clean first
# target hid a damaged second one entirely, and two damaged targets kept only one set of codes. The
# merged status was still CANNOT_ESTABLISH, which is what made the loss quiet: the verdict was right
# and its evidence was missing. The field is now a LIST of per-target blocks, always present, each
# carrying the target `ordinal` and the same safe `unit` label the rest of the report prints.
# --------------------------------------------------------------------------------------------


def _damaged_package_at(tmp_path: Path, name: str, *, damage: str) -> Path:
    """A package that fails S1 for a NAMED reason, so two of them can be told apart in one merge."""
    unit = tmp_path / name / "packages" / "Minimal"
    build_package(
        unit,
        "Minimal",
        worksheets=["Revenue"],
        oracle_views=[{"view_name": "Revenue", "view_type": "worksheet", "workbook_luid": UNIT_LUID}],
    )
    seal_package(unit, "Minimal")
    if damage == "extra":
        (unit / "stray.txt").write_text("undeclared\n", encoding="utf-8")
    elif damage == "changed":
        (unit / "report.json").write_text('{"workbooks": [{"name": "Tampered"}]}', encoding="utf-8")
    else:  # pragma: no cover - a typo in a test's own parameter must not pass silently
        raise AssertionError(f"unknown damage {damage!r}")
    return unit


def test_a_clean_first_target_does_not_hide_a_damaged_SECOND_one(tmp_path: Path) -> None:
    """The exact loss: `dict(reports[0])` carried the clean report's field over the damaged one."""
    clean = _packaged_unit(tmp_path / "first")
    damaged = _damaged_package_at(tmp_path, "second", damage="extra")

    merged = crr._merge_scans([crr.scan(clean), crr.scan(damaged)])

    assert merged["status"] == crr.STATUS_CANNOT_ESTABLISH
    blocks = merged["package_integrity"]
    assert [block["ordinal"] for block in blocks] == [0, 1]
    assert [block["status"] for block in blocks] == [
        package_filesystem.STATUS_CLEAN,
        package_filesystem.STATUS_FINDINGS,
    ]
    assert [row["code"] for row in blocks[1]["findings"]] == [package_filesystem.CODE_FILE_UNDECLARED]
    assert blocks[1]["findings"][0]["path"] == "stray.txt"


def test_two_damaged_targets_each_keep_their_OWN_codes_and_relative_evidence(tmp_path: Path) -> None:
    """Two blocks, two reasons, two package-relative paths - not one overwriting the other.

    The damage differs on purpose: an extra file and a changed byte produce different codes, so a
    merge that kept one block twice, or kept the first twice, is distinguishable from one that kept
    both.
    """
    first = _damaged_package_at(tmp_path, "first", damage="extra")
    second = _damaged_package_at(tmp_path, "second", damage="changed")

    merged = crr._merge_scans([crr.scan(first), crr.scan(second)])

    blocks = merged["package_integrity"]
    assert len(blocks) == 2
    assert [block["ordinal"] for block in blocks] == [0, 1]
    assert [row["code"] for row in blocks[0]["findings"]] == [package_filesystem.CODE_FILE_UNDECLARED]
    assert [row["code"] for row in blocks[1]["findings"]] == [package_filesystem.CODE_DIGEST_MISMATCH]
    assert blocks[0]["findings"][0]["path"] == "stray.txt"
    assert blocks[1]["findings"][0]["path"] == "report.json"
    assert all(block["unit"] == "Minimal" for block in blocks)


def test_an_ORDINARY_target_contributes_no_integrity_block(tmp_path: Path) -> None:
    """Absence is meaningful: a non-package target was never assessed, so it claims nothing.

    Paired with the clean-package assertion above, this is what makes the field unambiguous - empty
    means "not assessed", a `clean` block means "assessed and correct".
    """
    bundle = tmp_path / "run" / "bundle"
    bundle.mkdir(parents=True)
    (tmp_path / "run" / "assets").mkdir()
    build_unit(bundle, "Minimal", worksheets=["Revenue"])
    write_oracle(bundle, [{"view_name": "Revenue", "view_type": "worksheet", "workbook_luid": UNIT_LUID}])
    damaged = _damaged_package_at(tmp_path, "second", damage="extra")

    single = crr.scan(bundle)
    merged = crr._merge_scans([single, crr.scan(damaged)])

    assert single["package_integrity"] == []
    assert [block["ordinal"] for block in merged["package_integrity"]] == [1]


def test_an_unsafe_root_contributes_no_integrity_block_either(tmp_path: Path) -> None:
    """The verifier never ran, so it has nothing to say - the classifier's code is the whole verdict."""
    unit = tmp_path / "run" / "packages" / "Minimal"
    (unit / "fabric").mkdir(parents=True)

    report = crr.scan(unit)

    assert report["status"] == crr.STATUS_CANNOT_ESTABLISH
    assert report["package_integrity"] == []


def test_the_integrity_blocks_carry_no_target_path_in_either_shape(tmp_path: Path) -> None:
    """Single-target and merged JSON alike: ordinals, relative paths and the unit label only."""
    damaged = _damaged_package_at(tmp_path / "customer-secret-server", "second", damage="extra")

    single = crr.scan(damaged)
    merged = crr._merge_scans([single, crr.scan(damaged)])

    for rendered in (json.dumps(single["package_integrity"]), json.dumps(merged["package_integrity"])):
        assert "customer-secret-server" not in rendered
        assert str(tmp_path) not in rendered
        assert "stray.txt" in rendered


def test_the_merged_integrity_evidence_is_deterministic(tmp_path: Path) -> None:
    """Same inputs, same bytes - twice, and in both the single and the merged shape.

    A field a consumer diffs has to be stable, and the walk that produces it uses a stack, so ordering
    is a property that must be asserted rather than assumed.
    """
    first = _damaged_package_at(tmp_path, "first", damage="extra")
    second = _damaged_package_at(tmp_path, "second", damage="changed")

    runs = [
        json.dumps(crr._merge_scans([crr.scan(first), crr.scan(second)])["package_integrity"], sort_keys=False)
        for _ in range(2)
    ]

    assert runs[0] == runs[1]
    assert json.dumps(crr.scan(first)["package_integrity"]) == json.dumps(crr.scan(first)["package_integrity"])
