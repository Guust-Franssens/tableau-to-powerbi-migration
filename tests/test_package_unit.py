"""Packaging must ARRANGE evidence, never create it (issue #446).

`package_unit.py` is the join nothing else performs: engine output keyed by sanitized workbook name,
oracle renders keyed by bare view LUID in a flat tree, and a LUID-prefixed source asset in a third
place. The only way that join can be harmful is by attributing a render to the wrong object, so every
test here pins one of the three ways it could:

1. **guessing a workbook** - copying a render "because it was in the same capture" (issue #438 in a
   new place). Attribution has exactly two admissible routes, and both mirror
   `reference_evidence.Evidence.is_for`, so packaging can never widen what the gate would accept;
2. **guessing an object KIND** - `view_type` may legitimately be `unknown`, and `content_url` is
   `<wb>/sheets/<view>` for dashboards AND worksheets, so there is nothing to fall back to; and
3. **manufacturing coverage in the human layer** - a selected view with no usable render must not be
   greppable as `ORACLE_RENDER`, or `grep -c` over-reports the reference an agent thinks it has.

Fixtures are reused from `test_check_reference_readiness.py` on purpose: its `write_png` emits a
genuine, parseable, per-file-distinct PNG because round-1 review measured an 8-byte signature stub
being asserted as evidence. A packaging test that wrote stubs would prove nothing about a gate that
verifies recorded hashes and dimensions.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import package_unit as pkg  # noqa: E402  # pylint: disable=wrong-import-position
import reference_evidence as rev  # noqa: E402  # pylint: disable=wrong-import-position
from test_check_reference_readiness import (  # noqa: E402  # pylint: disable=wrong-import-position
    write_engine_report,
    write_handover,
    write_oracle,
    write_png,
    write_report,
    write_workbook,
)

UNIT = "Book"
WB_LUID = "11111111-2222-3333-4444-555555555555"
OTHER_LUID = "99999999-8888-7777-6666-555555555555"


def _view(name: str, luid: str, *, workbook_luid: str, workbook_name: str, view_type: str | None) -> dict:
    view = {
        "view_luid": luid,
        "view_name": name,
        "content_url": f"{workbook_name}/sheets/{name}",
        "workbook_luid": workbook_luid,
        "workbook_name": workbook_name,
    }
    if view_type is not None:
        view["view_type"] = view_type
    return view


def _bundle(  # pylint: disable=too-many-arguments
    tmp_path: Path,
    *,
    worksheets: tuple[str, ...] = ("Sales", "Profit"),
    views: list[dict] | None = None,
    provenance_luid: str | None = WB_LUID,
    provenance_match: str = "sha256",
    asset_prefix: str | None = WB_LUID,
    datasources: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    """`(bundle, oracle)` shaped exactly as a real estate run, with one workbook unit."""
    bundle = tmp_path / "bundle"
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    name = f"{asset_prefix}_{UNIT}.twb" if asset_prefix else f"{UNIT}.twb"
    source = write_workbook(assets / name, worksheets=list(worksheets))
    write_engine_report(bundle, workbooks=[UNIT], datasources=list(datasources))
    write_handover(bundle, UNIT, source_id=str(Path("_runs") / "999-x" / "assets" / name))
    write_report(bundle, UNIT, _page_ids(source))
    if provenance_luid:
        (bundle / "source-provenance.json").write_text(
            json.dumps(
                {
                    "inputs": [
                        {
                            "input": {"file": name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                            "origin": {
                                "workbook_luid": provenance_luid,
                                "workbook_name": "Book",
                                "match": provenance_match,
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
    oracle = write_oracle(bundle.parent, views if views is not None else _default_views())
    return bundle, oracle


def _page_ids(source: Path) -> list[str]:
    import check_reference_readiness as crr  # pylint: disable=import-outside-toplevel

    return [obj.page_id for obj in crr.source_objects(source) or []]


def _default_views() -> list[dict]:
    return [
        _view(
            "Sales",
            "aaaaaaaa-0000-0000-0000-000000000001",
            workbook_luid=WB_LUID,
            workbook_name="Book",
            view_type="worksheet",
        ),
        _view(
            "Landing",
            "aaaaaaaa-0000-0000-0000-000000000002",
            workbook_luid=WB_LUID,
            workbook_name="Book",
            view_type="dashboard",
        ),
        _view(
            "Foreign",
            "bbbbbbbb-0000-0000-0000-000000000003",
            workbook_luid=OTHER_LUID,
            workbook_name="Other Book",
            view_type="worksheet",
        ),
    ]


def _out(tmp_path: Path) -> Path:
    """Package root, deliberately TWO levels below `tmp_path`.

    `check_reference_readiness._collect_evidence` scans the target's grandparent, so a package at
    `<tmp>/out/<Unit>` would also match the fixture capture at `<tmp>/_oracle` - the shadowing
    `package_unit.conflicting_evidence_dirs` refuses. Nesting one level deeper keeps `--oracle`
    auto-discovery (which looks beside the BUNDLE) exercised without tripping it.
    """
    return tmp_path / "packages" / "out"


def _package(tmp_path: Path, bundle: Path, oracle: Path, unit: str = UNIT) -> dict:
    return pkg.package_unit(bundle, unit, _out(tmp_path), oracle_dir=oracle, assets_dir=bundle.parent / "assets")


def _lines(tmp_path: Path, unit: str = UNIT) -> list[str]:
    return (_out(tmp_path) / unit / "handover.md").read_text(encoding="utf-8").splitlines()


def _images(tmp_path: Path, unit: str = UNIT) -> set[str]:
    root = _out(tmp_path) / unit / "oracle"
    return {str(path.relative_to(root)).replace("\\", "/") for path in root.rglob("*") if path.is_file()}


# --------------------------------------------------------------------------------------------
# 1. attribution - the fail-closed rule
# --------------------------------------------------------------------------------------------


def test_only_this_workbooks_views_are_copied_in(tmp_path: Path) -> None:
    """A capture holding two workbooks yields THIS unit's views only, via the LUID route."""
    bundle, oracle = _bundle(tmp_path)
    result = _package(tmp_path, bundle, oracle)
    assert result["oracle"]["route"] == "workbook_luid"
    assert sorted(obj["name"] for obj in result["oracle"]["objects"]) == ["Landing", "Sales"]
    assert not any("Foreign" in path for path in _images(tmp_path))


def test_a_render_with_no_attributable_workbook_is_omitted_with_a_reason(tmp_path: Path) -> None:
    """No workbook LUID copies NOTHING - #438 in a new place."""
    views = [
        _view(
            "Sales",
            "aaaaaaaa-0000-0000-0000-000000000001",
            workbook_luid=OTHER_LUID,
            workbook_name="A Different Book",
            view_type="worksheet",
        )
    ]
    bundle, oracle = _bundle(tmp_path, views=views, provenance_luid=None, asset_prefix=None)
    result = _package(tmp_path, bundle, oracle)
    assert result["oracle"]["objects"] == []
    assert "no workbook LUID for this unit" in result["oracle"]["reason"]
    assert not (_out(tmp_path) / UNIT / "oracle").exists()


def test_a_matching_display_name_is_never_enough_to_attribute_a_render(tmp_path: Path) -> None:
    """A display NAME is not an identity, even when it matches exactly and uniquely (issue #450).

    There WAS an exact-name fallback here, mirroring `reference_evidence.Evidence.is_for`. It was
    deleted rather than further guarded: it fired **0 times in 67 units** on the reference estate, and
    #450 measured the same class failing OPEN in `check_unit` on **360 of 360** real records, where a
    foreign workbook's render is admitted as this unit's evidence. Two projects can hold workbooks
    with the same name; the LUID is the identity and the name is decoration.
    """
    views = [
        _view(
            "Sales",
            "aaaaaaaa-0000-0000-0000-000000000001",
            workbook_luid=WB_LUID,
            workbook_name=UNIT,
            view_type="worksheet",
        )
    ]
    bundle, oracle = _bundle(tmp_path, views=views, provenance_luid=None, asset_prefix=None)
    result = _package(tmp_path, bundle, oracle)
    assert result["oracle"]["objects"] == []
    assert result["oracle"]["route"] is None
    assert "a display name is not an identity" in result["oracle"]["reason"]
    assert not (_out(tmp_path) / UNIT / "oracle").exists()


def test_one_asset_mapped_onto_two_workbook_luids_attributes_nothing(tmp_path: Path) -> None:
    """Byte-identical uploads in two projects: provenance holds two LUIDs for one sha256.

    Neither may win. This is the multi-owner case that the deleted name route used to have its own
    single-owner guard for; with one identity route the same refusal lives in exactly one place.
    """
    bundle, oracle = _bundle(tmp_path)
    payload = json.loads((bundle / "source-provenance.json").read_text(encoding="utf-8"))
    same_sha = payload["inputs"][0]["input"]["sha256"]
    payload["inputs"].append(
        {
            "input": {"file": "same_bytes_elsewhere.twb", "sha256": same_sha},
            "origin": {"workbook_luid": OTHER_LUID, "workbook_name": "Book", "match": "sha256"},
        }
    )
    (bundle / "source-provenance.json").write_text(json.dumps(payload), encoding="utf-8")

    result = _package(tmp_path, bundle, oracle)
    assert result["workbook_identity"]["luid"] is None
    assert "2 workbook LUIDs" in result["workbook_identity"]["reason"]
    assert result["oracle"]["objects"] == []


def test_a_datasource_filename_luid_is_never_promoted_to_a_workbook_identity(tmp_path: Path) -> None:
    """`harvest_estate_assets.py` prefixes a `.tds` with its DATASOURCE LUID - a different namespace.

    Measured on the reference estate: **all 19** units carrying a filename LUID with no provenance
    entry are datasources. Promoting the prefix would feed a datasource LUID into a `workbook_luid`
    comparison, which buys nothing (those 19 have no views) and fails OPEN if the namespaces collide.
    """
    views = [
        _view(
            "Sales",
            "aaaaaaaa-0000-0000-0000-000000000001",
            workbook_luid=OTHER_LUID,
            workbook_name="Anything",
            view_type="worksheet",
        )
    ]
    bundle, oracle = _bundle(tmp_path, views=views, provenance_luid=None, asset_prefix=OTHER_LUID)
    result = _package(tmp_path, bundle, oracle)
    assert pkg.filename_luid(Path(f"{OTHER_LUID}_x.tds")) == OTHER_LUID
    assert result["workbook_identity"]["luid"] is None
    assert result["oracle"]["objects"] == []


def test_a_provenance_luid_contradicting_the_asset_filename_fails_closed(tmp_path: Path) -> None:
    """Two identities that disagree resolve to NEITHER, rather than to whichever is read first.

    This once caught a real fail-open path: refusing the LUID was not enough while an exact-name
    fallback still attributed the same renders. That fallback is now deleted outright (#450), so a
    contradiction has nowhere to fall through to - which is the simplification, not another guard.
    """
    bundle, oracle = _bundle(tmp_path, provenance_luid=OTHER_LUID, asset_prefix=WB_LUID)
    result = _package(tmp_path, bundle, oracle)
    assert result["workbook_identity"]["luid"] is None
    assert "asset filename declares LUID" in result["workbook_identity"]["reason"]
    assert result["oracle"]["objects"] == []
    assert "no workbook LUID for this unit" in result["oracle"]["reason"]


def test_a_name_only_provenance_match_still_carries_its_renders_but_says_so(tmp_path: Path) -> None:
    """`match=name_only` means the server bytes DIFFER; the picture is still useful, the claim is not.

    The readiness gate refuses to trust a LUID in this state
    (`check_reference_readiness._provenance_luid`), so it will report those pages BLIND. Packaging
    must neither hide the renders nor overstate them - the marker is on the `ORACLE_ATTRIBUTION` line.
    """
    bundle, oracle = _bundle(tmp_path, provenance_match="name_only")
    result = _package(tmp_path, bundle, oracle)
    assert len(result["oracle"]["objects"]) == 2
    attribution = [line for line in _lines(tmp_path) if line.startswith("ORACLE_ATTRIBUTION")]
    assert attribution and "match=name_only" in attribution[0]


# --------------------------------------------------------------------------------------------
# 2. object kind - carried but marked, never defaulted
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("declared", [None, "unknown", "", "UNKNOWN", "sheet"])
def test_an_unresolved_view_type_is_filed_under_unknown_and_never_as_a_kind(tmp_path: Path, declared) -> None:
    """`capture_tableau_oracle.py`'s type resolver is non-fatal by design, so this is a real value."""
    views = [
        _view(
            "Sales",
            "aaaaaaaa-0000-0000-0000-000000000001",
            workbook_luid=WB_LUID,
            workbook_name=UNIT,
            view_type=declared,
        )
    ]
    bundle, oracle = _bundle(tmp_path, views=views)
    result = _package(tmp_path, bundle, oracle)
    assert [obj["view_type"] for obj in result["oracle"]["objects"]] == ["unknown"]
    files = _images(tmp_path)
    assert any(path.startswith("unknown/images/") for path in files)
    assert not any(path.startswith(("dashboard/", "worksheet/")) for path in files)
    assert any(line.startswith("UNTYPED_RENDER") for line in _lines(tmp_path))


def test_content_url_is_not_used_as_a_type_discriminator(tmp_path: Path) -> None:
    """`content_url` is `<wb>/sheets/<view>` for BOTH kinds; only `view_type` decides."""
    views = [
        _view(
            "Landing",
            "aaaaaaaa-0000-0000-0000-000000000001",
            workbook_luid=WB_LUID,
            workbook_name=UNIT,
            view_type="dashboard",
        ),
        _view(
            "Sales",
            "aaaaaaaa-0000-0000-0000-000000000002",
            workbook_luid=WB_LUID,
            workbook_name=UNIT,
            view_type="worksheet",
        ),
    ]
    bundle, oracle = _bundle(tmp_path, views=views)
    _package(tmp_path, bundle, oracle)
    files = _images(tmp_path)
    assert "dashboard/images/Landing.png" in files
    assert "worksheet/images/Sales.png" in files


def test_a_dashboard_and_a_worksheet_sharing_a_name_do_not_collide(tmp_path: Path) -> None:
    """Different kinds, same name: two files, both kept."""
    views = [
        _view(
            "Sales",
            "aaaaaaaa-0000-0000-0000-000000000001",
            workbook_luid=WB_LUID,
            workbook_name=UNIT,
            view_type="dashboard",
        ),
        _view(
            "Sales",
            "aaaaaaaa-0000-0000-0000-000000000002",
            workbook_luid=WB_LUID,
            workbook_name=UNIT,
            view_type="worksheet",
        ),
    ]
    bundle, oracle = _bundle(tmp_path, views=views)
    _package(tmp_path, bundle, oracle)
    assert {"dashboard/images/Sales.png", "worksheet/images/Sales__aaaaaaaa.png"} <= _images(tmp_path)


def test_two_same_kind_views_sharing_a_name_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """A collision disambiguates by LUID rather than silently keeping the last render."""
    views = [
        _view(
            "Sales",
            "aaaaaaaa-0000-0000-0000-000000000001",
            workbook_luid=WB_LUID,
            workbook_name=UNIT,
            view_type="worksheet",
        ),
        _view(
            "Sales",
            "cccccccc-0000-0000-0000-000000000002",
            workbook_luid=WB_LUID,
            workbook_name=UNIT,
            view_type="worksheet",
        ),
    ]
    bundle, oracle = _bundle(tmp_path, views=views)
    _package(tmp_path, bundle, oracle)
    images = {path for path in _images(tmp_path) if path.endswith(".png")}
    assert len(images) == 2, images


# --------------------------------------------------------------------------------------------
# 3. the copy itself - bytes and claims must both survive
# --------------------------------------------------------------------------------------------


def test_a_packaged_render_still_verifies_against_its_recorded_hash(tmp_path: Path) -> None:
    """`reference_evidence.render_facts` re-hashes the bytes, so a re-encoded copy would be rejected."""
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    usable, rejected = rev.oracle_evidence([_out(tmp_path) / UNIT / "oracle"])
    assert sorted(item.name for item in usable) == ["Landing", "Sales"]
    assert rejected == []


def test_a_leg_claiming_ok_whose_file_is_absent_cannot_become_evidence(tmp_path: Path) -> None:
    """An interrupted capture claims renders it never wrote; the packaged manifest must not repeat it."""
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    (oracle / manifest["views"][0]["image"]["path"]).unlink()
    result = _package(tmp_path, bundle, oracle)

    assert [omission["leg"] for omission in result["oracle"]["omissions"]] == ["image"]
    packaged = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    absent = next(view for view in packaged["views"] if view["view_name"] == "Sales")
    assert absent["image"]["status"] == pkg.OMITTED_STATUS
    usable, _ = rev.oracle_evidence([_out(tmp_path) / UNIT / "oracle"])
    assert [item.name for item in usable] == ["Landing"]


def test_a_selected_view_with_no_render_is_not_greppable_as_a_render(tmp_path: Path) -> None:
    """`grep -c ^ORACLE_RENDER` is the inventory an agent trusts; a render-less view must not inflate it.

    ⚠️ The label keys on the IMAGE legs alone. A view whose `/data` leg succeeded and whose `/image`
    leg failed still carries a CSV, so a label keyed on "any copied file" would call it a render.
    """
    views = [
        _view(
            "Sales",
            "aaaaaaaa-0000-0000-0000-000000000001",
            workbook_luid=WB_LUID,
            workbook_name=UNIT,
            view_type="worksheet",
        )
    ]
    bundle, oracle = _bundle(tmp_path, views=views)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["image"] = {"status": "failed"}
    manifest["views"][0]["data"] = {"status": "ok", "path": "data/sales.csv"}
    (oracle / "data").mkdir(exist_ok=True)
    (oracle / "data" / "sales.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = _package(tmp_path, bundle, oracle)
    assert result["oracle"]["objects"][0]["images"] == []
    assert result["oracle"]["objects"][0]["data"] == "worksheet/data/Sales.csv"
    lines = _lines(tmp_path)
    assert not [line for line in lines if line.startswith("ORACLE_RENDER")]
    no_render = [line for line in lines if line.startswith("ORACLE_NO_RENDER")]
    assert no_render and "data=worksheet/data/Sales.csv" in no_render[0]


# --------------------------------------------------------------------------------------------
# 4. the scoped engine artifacts
# --------------------------------------------------------------------------------------------


def test_the_scoped_report_keeps_only_this_units_engine_classification(tmp_path: Path) -> None:
    """`report.json` is what earns NOT_APPLICABLE; a whole-bundle copy would report 47 phantom units."""
    bundle, oracle = _bundle(tmp_path, datasources=("Shared Extract",))
    write_engine_report(bundle, workbooks=[UNIT, "Other"], datasources=["Shared Extract"])
    _package(tmp_path, bundle, oracle)
    scoped = json.loads((_out(tmp_path) / UNIT / "report.json").read_text(encoding="utf-8"))
    assert [entry["name"] for entry in scoped["workbooks"]] == [UNIT]
    assert scoped["datasources"] == []


def test_the_scoped_receipt_names_files_that_exist_in_the_package(tmp_path: Path) -> None:
    """Re-rooted `pbip/<unit>/` -> `fabric/`, so the receipt attests to what is actually here."""
    bundle, oracle = _bundle(tmp_path)
    emitted = bundle / "pbip" / UNIT / f"{UNIT}.Report" / "definition" / "report.json"
    emitted.write_text("{}", encoding="utf-8")
    (bundle / "engine-output-receipt.json").write_text(
        json.dumps(
            {
                "version": 1,
                "engine": {"version": "2.339.0"},
                "artifacts": [
                    {"path": f"pbip/{UNIT}/{UNIT}.Report/definition/report.json", "size": 1, "sha256": "x"},
                    {"path": "pbip/Other/Other.Report/definition/report.json", "size": 1, "sha256": "y"},
                    {"path": "reports/Other.Report/definition/report.json", "size": 1, "sha256": "z"},
                ],
            }
        ),
        encoding="utf-8",
    )
    result = _package(tmp_path, bundle, oracle)
    receipt = json.loads((_out(tmp_path) / UNIT / "engine-output-receipt.json").read_text(encoding="utf-8"))
    assert result["engine"] == "2.339.0"
    assert [entry["path"] for entry in receipt["artifacts"]] == [f"fabric/{UNIT}.Report/definition/report.json"]
    assert (_out(tmp_path) / UNIT / receipt["artifacts"][0]["path"]).is_file()


def test_the_provenance_is_scoped_by_content_not_by_filename(tmp_path: Path) -> None:
    """`_provenance_luid` looks the unit up by `input.sha256`; a wrong entry would hand it a wrong LUID."""
    bundle, oracle = _bundle(tmp_path)
    payload = json.loads((bundle / "source-provenance.json").read_text(encoding="utf-8"))
    payload["inputs"].append(
        {"input": {"file": "someone_else.twb", "sha256": "0" * 64}, "origin": {"workbook_luid": OTHER_LUID}}
    )
    (bundle / "source-provenance.json").write_text(json.dumps(payload), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    scoped = json.loads((_out(tmp_path) / UNIT / "source-provenance.json").read_text(encoding="utf-8"))
    assert [entry["origin"]["workbook_luid"] for entry in scoped["inputs"]] == [WB_LUID]


# --------------------------------------------------------------------------------------------
# 5. the greppable handover
# --------------------------------------------------------------------------------------------


def test_emptied_visuals_lead_the_handover_and_name_their_page(tmp_path: Path) -> None:
    """They render blank on a report that validates clean, and nothing else in the toolkit shows them.

    The engine's `pbip_ref_drops[]` rows carry only a visual id, so the page is resolved from the
    PBIR tree - a bare `v-page-Dashboard06ca9874` is not something an operator can act on.
    """
    bundle, oracle = _bundle(tmp_path)
    page_id = _page_ids(bundle.parent / "assets" / f"{WB_LUID}_{UNIT}.twb")[0]
    visual = bundle / "pbip" / UNIT / f"{UNIT}.Report" / "definition" / "pages" / page_id / "visuals" / "v-abc"
    visual.mkdir(parents=True)
    (visual / "visual.json").write_text("{}", encoding="utf-8")
    payload = json.loads((bundle / "handover" / f"{UNIT}.json").read_text(encoding="utf-8"))
    payload["workbook"]["pbip_ref_drops"] = [
        {"visual": "v-abc", "emptied": True, "dropped": ["Values:column 'Duration'"]},
        {"visual": "v-kept", "emptied": False, "dropped": ["Tooltip:column 'X'"]},
    ]
    payload["workbook"]["remediation_worklist"] = {
        "items": [{"category": "unsupported_visual", "severity": "blocking", "reason": "r", "remediation": "fix"}]
    }
    (bundle / "handover" / f"{UNIT}.json").write_text(json.dumps(payload), encoding="utf-8")

    _package(tmp_path, bundle, oracle)
    findings = [line for line in _lines(tmp_path) if line and not line.startswith(("#", "UNIT ", "PACKAGE "))]
    assert findings[0] == f"EMPTIED_VISUAL page={page_id} visual=v-abc dropped=Values:column 'Duration'"
    assert findings.index([x for x in findings if x.startswith("WORKLIST")][0]) > 0
    assert not [line for line in findings if "v-kept" in line]


def test_every_finding_is_one_line_with_a_stable_prefix(tmp_path: Path) -> None:
    """A newline inside an engine reason would split one finding across two greppable lines."""
    bundle, oracle = _bundle(tmp_path)
    payload = json.loads((bundle / "handover" / f"{UNIT}.json").read_text(encoding="utf-8"))
    payload["workbook"]["remediation_worklist"] = {
        "items": [{"category": "c", "severity": "blocking", "reason": "line one\nline two", "remediation": "do\nit"}]
    }
    (bundle / "handover" / f"{UNIT}.json").write_text(json.dumps(payload), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    worklist = [line for line in _lines(tmp_path) if line.startswith("WORKLIST")]
    assert len(worklist) == 1
    assert "line one line two" in worklist[0]


# --------------------------------------------------------------------------------------------
# 6. the CLI contract
# --------------------------------------------------------------------------------------------


def test_a_unit_with_no_engine_working_copy_is_still_packaged_and_reported(tmp_path: Path) -> None:
    """Measured on a real estate run: `report.json` lists 48 workbooks, `pbip/` holds 44.

    Deriving the unit list from the filesystem dropped the other four silently - the same class of
    defect this packaging exists to remove. They package for their source, reference and handover,
    `packaged` is false, and the exit code says so.
    """
    bundle, oracle = _bundle(tmp_path)
    write_engine_report(bundle, workbooks=[UNIT, "Never_Emitted"])
    assert "Never_Emitted" in pkg.bundle_units(bundle)
    code = pkg.main(["--bundle", str(bundle), "--out", str(_out(tmp_path)), "--quiet"])
    manifest = json.loads((_out(tmp_path) / "Never_Emitted" / "package-manifest.json").read_text(encoding="utf-8"))
    assert manifest["packaged"] is False
    assert code == 1


def test_packaging_every_emitted_unit_exits_zero(tmp_path: Path) -> None:
    """Also pins `--oracle` auto-discovery: the capture is found beside the bundle, unflagged."""
    bundle, oracle = _bundle(tmp_path)
    assert pkg.main(["--bundle", str(bundle), "--out", str(_out(tmp_path)), "--quiet"]) == 0
    assert (_out(tmp_path) / UNIT / "fabric" / f"{UNIT}.Report").is_dir()
    assert pkg.discover_dir(bundle, ("oracle", "_oracle")) == oracle.resolve()
    assert (_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").is_file()


def test_an_unknown_unit_is_a_usage_error_not_an_empty_package(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        pkg.main(["--bundle", str(bundle), "--out", str(_out(tmp_path)), "--unit", "Nope", "--quiet"])
    assert excinfo.value.code == 2
    assert not (_out(tmp_path) / "Nope").exists()


def test_write_png_is_a_real_image_so_these_fixtures_could_fail(tmp_path: Path) -> None:
    """Pin the fixture itself: an 8-byte stub would make every evidence assertion above vacuous."""
    blob = write_png(tmp_path / "probe.png", 320, 240).read_bytes()
    assert rev.render_facts(
        tmp_path / "probe.png",
        rev.RecordedFacts(sha256=hashlib.sha256(blob).hexdigest(), byte_size=len(blob), width=320, height=240),
    ) == (320, 240)
