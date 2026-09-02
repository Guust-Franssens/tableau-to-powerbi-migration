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
from manifest_scope import KEEP, REPORT_ALLOW, Rows, project  # noqa: E402  # pylint: disable=wrong-import-position
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


# --------------------------------------------------------------------------------------------
# 4a. the scoped report as a DATA-EGRESS boundary (round-1 finding 1)
#
# `_scope_report` used to copy every key it did not explicitly filter. Measured by the reviewer on
# `HR_Dashboard` in the 48-workbook reference bundle: `workbooks[]` held 1 entry while **11 of the 13
# top-level fields were byte-identical to the whole-estate report** - 67 `input_manifest.assets`, 62
# `openable_outputs`, and (unnamed in the report, found while fixing it) 48 `definition_of_done`
# rows. A customer package named other customers' workbooks.
#
# ⚠️ Two controls that look adequate and are NOT, both measured while writing these tests:
#
# * **`workbooks[] == 1`** passes on the DEFECTIVE code. Every test above section 4a does, which is
#   why the leak survived a full green suite.
# * **a whole-file hash diff between two packages** also passes on the defective code - the files
#   genuinely differ, because `workbooks[]` is filtered. Scoping is only provable FIELD BY FIELD.
#
# So the control is a sentinel planted in every aggregate field, asserted absent, plus a per-field
# identity comparison. The sentinel is a full token rather than a bare word on purpose: probing the
# real fix for the literal `Groups` reported a leak that was actually `'Age Groups'`, a worksheet
# INSIDE the unit. A substring sentinel manufactures findings.
# --------------------------------------------------------------------------------------------

#: Every top-level field of a real engine `report.json`, measured 2026-09-02 on the 48-workbook
#: reference bundle `_runs/407-dryrun-gates/bundle/report.json` (2,102,605 bytes, N=13). Pinned so
#: the fixture below cannot silently stop covering a field the engine actually writes.
ENGINE_REPORT_FIELDS = (
    "datasources",
    "definition_of_done",
    "environment",
    "fallbacks",
    "generated_at",
    "input_manifest",
    "openable_outputs",
    "pending_gates",
    "repair_queue",
    "source",
    "summary",
    "tool",
    "workbooks",
)

#: A token no real Tableau object can collide with, and that no substring of this unit's own content
#: can produce. `_scope_report` must not emit it anywhere.
FOREIGN = "Zz_Foreign_Unit_Sentinel"

#: A SECOND sentinel, planted only in rows this unit RETAINS. Round-2 review found the round-1
#: fixture structurally unable to see blocker 1: it planted sentinels exclusively in FOREIGN rows,
#: which are guaranteed to be filtered out whole, so it never exercised a retained row's unknown
#: extensions - and `workbooks[0].future_nested` shipped unnoticed. A guard that cannot fail is
#: worse than no guard, because it is credited as coverage.
RETAINED_EXTENSION = "Zz_Retained_Row_Extension_Sentinel"


def _verbatim_top_level() -> set[str]:
    """Top-level `report.json` fields the allowlist carries verbatim, read from the spec itself."""
    return {key for key, spec in REPORT_ALLOW.items() if spec is KEEP}


def _estate_report(unit: str) -> dict:
    """A `report.json` in the REAL engine's 13-field shape, sentinel-planted in EVERY aggregate field.

    Shapes are copied from the reference bundle, not invented: `input_manifest.assets[]` entries
    carry `name`/`staged_input_path`/`sha256`, `openable_outputs[]` carry absolute `pbip`/
    `model_folder` paths, `definition_of_done.workbooks[]` carry `workbook`/`pbip_folder`/`reason`,
    and `fallbacks[]` carry `datasource`/`source_id`. A fixture that planted the sentinel only in a
    field the engine does not really write would prove nothing about a real package.

    ⚠️ It plants TWO sentinels, in two structurally different places. `FOREIGN` goes in rows and
    containers belonging to other units, which filtering removes wholesale. `RETAINED_EXTENSION`
    goes inside the rows this unit KEEPS - `workbooks[0].future_nested` and
    `definition_of_done.workbooks[0].future_nested` - which only a NESTED allowlist can remove, and
    which the round-1 fixture had no case for at all.
    """
    return {
        "tool": "migrate_estate",
        "generated_at": "2026-09-02T07:04:07Z",
        "workbooks": [
            {
                "name": unit,
                "pbip_status": "ok",
                "pbip_folder": f"pbip/{unit}/{unit}.pbip",
                "future_nested": RETAINED_EXTENSION,
            },
            {"name": FOREIGN, "pbip_status": "failed", "pbip_folder": f"pbip/{FOREIGN}/{FOREIGN}.pbip"},
        ],
        "datasources": [{"name": f"{FOREIGN}_ds", "source_id": f"assets/{FOREIGN}.tds"}],
        "definition_of_done": {
            "applicable": True,
            "status": "failed",
            "reports_bound": 44,
            "reports_failed": 18,
            "reports_warned": 14,
            "workbooks": [
                {
                    "workbook": unit,
                    "status": "warn",
                    "reason": "1 visual(s) rebuilt with warnings",
                    "future_nested": RETAINED_EXTENSION,
                },
                {"workbook": FOREIGN, "status": "failed", "pbip_folder": f"pbip/{FOREIGN}", "reason": "it failed"},
            ],
        },
        "environment": {"findings": [f"{FOREIGN}: gateway unreachable"]},
        "fallbacks": [{"datasource": FOREIGN, "source_id": f"_runs/999-x/assets/{FOREIGN}.tds", "reason": "no schema"}],
        "input_manifest": {
            "root": f"C:/estate/{FOREIGN}/assets",
            "source_kind": "LocalFilesSource",
            "assets": [{"name": FOREIGN, "staged_input_path": f"C:/estate/assets/{FOREIGN}.tds", "sha256": "0" * 64}],
        },
        "openable_outputs": [
            {"kind": "workbook", "name": FOREIGN, "pbip": f"C:/estate/bundle/pbip/{FOREIGN}/{FOREIGN}.pbip"}
        ],
        "pending_gates": [{"gate": "second_compiler", "count": 220, "offer": f"220 stubs, mostly in {FOREIGN}"}],
        "repair_queue": {"path": "repair-queue.json", "requests": 220, "subjects": 22, "top_subject": FOREIGN},
        "source": {"kind": "LocalFilesSource", "root": f"C:/estate/{FOREIGN}/assets"},
        "summary": {"workbooks_total": 48, "connectors_seen": ["snowflake", FOREIGN]},
    }


def _packaged_report(tmp_path: Path, unit: str = UNIT) -> dict:
    return json.loads((_out(tmp_path) / unit / "report.json").read_text(encoding="utf-8"))


def _package_estate_report(tmp_path: Path) -> tuple[dict, dict, str]:
    """`(full report, packaged report, packaged text)` for one unit of a sentinel-planted estate."""
    bundle, oracle = _bundle(tmp_path)
    full = _estate_report(UNIT)
    (bundle / "report.json").write_text(json.dumps(full), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    text = (_out(tmp_path) / UNIT / "report.json").read_text(encoding="utf-8")
    return full, json.loads(text), text


def test_the_sentinel_fixture_covers_every_field_the_engine_actually_writes() -> None:
    """Guards the guard: a fixture that drifted from the engine's shape would test a strawman."""
    assert tuple(sorted(_estate_report(UNIT))) == ENGINE_REPORT_FIELDS


def test_no_foreign_unit_survives_anywhere_in_the_packaged_report(tmp_path: Path) -> None:
    """The round-1 HIGH finding, as a negative control: ONE unit's package names ONE unit."""
    _full, scoped, text = _package_estate_report(tmp_path)
    leaking = [key for key, value in scoped.items() if FOREIGN in json.dumps(value, ensure_ascii=False)]
    assert leaking == [], f"foreign unit leaked through: {leaking}"
    assert FOREIGN not in text


def test_no_engine_report_field_is_copied_unchanged_from_the_estate(tmp_path: Path) -> None:
    """Field by field, because a whole-file hash cannot see this: 11 of 13 fields were verbatim."""
    full, scoped, _text = _package_estate_report(tmp_path)
    verbatim = [
        key
        for key, value in full.items()
        if key not in _verbatim_top_level()
        and key in scoped
        and json.dumps(scoped[key], sort_keys=True) == json.dumps(value, sort_keys=True)
    ]
    assert verbatim == [], f"copied unchanged from the whole-estate report: {verbatim}"
    assert set(scoped) - set(full) == {"scoped_by", "scope"}


def test_a_retained_row_does_not_smuggle_unknown_nested_fields(tmp_path: Path) -> None:
    """Round-2 blocker 1: the allowlist stopped at the collection boundary.

    Filtering `workbooks[]` to this unit removes FOREIGN rows whole - which is why the round-1
    fixture, planting only in foreign rows, could never fail. The row this unit KEEPS was still
    copied wholesale, so `workbooks[0].future_nested` and its `definition_of_done` twin shipped
    automatically. Only a nested allowlist removes them, and only a retained-row sentinel sees it.
    """
    _full, scoped, text = _package_estate_report(tmp_path)
    assert RETAINED_EXTENSION not in text
    assert "future_nested" not in scoped["workbooks"][0]
    assert "future_nested" not in scoped["definition_of_done"]["workbooks"][0]
    assert scoped["workbooks"][0]["name"] == UNIT
    assert scoped["definition_of_done"]["workbooks"][0]["workbook"] == UNIT


def test_a_dropped_nested_field_is_recorded_by_its_full_path(tmp_path: Path) -> None:
    """An omission must be discoverable: the path is engine schema, the value is the estate content."""
    _full, scoped, _text = _package_estate_report(tmp_path)
    dropped = scoped["scope"]["dropped_fields"]
    assert "workbooks[].future_nested" in dropped
    assert "definition_of_done.workbooks[].future_nested" in dropped
    assert "input_manifest" in dropped
    assert "definition_of_done.status" in dropped
    assert not any(RETAINED_EXTENSION in entry or FOREIGN in entry for entry in dropped)


def test_one_unknown_field_across_many_rows_is_reported_once(tmp_path: Path) -> None:
    """48 rows carrying the same unknown field is one finding, not 48 indexed near-duplicates.

    ⚠️ Exercised against `project()` DIRECTLY, on a `Rows` spec at the top level. Routing it through
    a packaged report proved nothing: the mapping branch de-duplicates the accumulated list on the
    way out, so it masked the row branch entirely and the mutation that removes the row-level
    `sorted(set(...))` SURVIVED. The committed mutation campaign is what surfaced that.
    """
    rows = [{"name": UNIT, "future_nested": RETAINED_EXTENSION} for _ in range(24)]
    _kept, dropped = project(rows, Rows({"name": KEEP}), prefix="workbooks")
    assert dropped == ["workbooks[].future_nested"]

    _kept, nested = project({"workbooks": rows}, {"workbooks": Rows({"name": KEEP})})
    assert nested.count("workbooks[].future_nested") == 1


def test_an_unknown_engine_field_is_dropped_rather_than_carried(tmp_path: Path) -> None:
    """Direction: the allowlist fails CLOSED on a field the engine adds later, and says which."""
    bundle, oracle = _bundle(tmp_path)
    full = _estate_report(UNIT)
    full["some_future_estate_field"] = [{"unit": FOREIGN, "detail": "invented by a later engine"}]
    (bundle / "report.json").write_text(json.dumps(full), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    scoped = _packaged_report(tmp_path)
    assert "some_future_estate_field" not in scoped
    assert "some_future_estate_field" in scoped["scope"]["dropped_fields"]


def test_only_unit_neutral_engine_identity_is_carried_verbatim() -> None:
    """Widening this is exactly how the leak returns, so the CONTENT is checked, not the name.

    A scalar cannot carry another unit's name, path or status; every field that leaked in round 1 was
    a list or a dict of per-unit rows.
    """
    full = _estate_report(UNIT)
    verbatim = _verbatim_top_level()
    assert verbatim <= set(full)
    for key in verbatim:
        assert isinstance(full[key], str), f"{key} is not a scalar - it can carry estate content"


def test_the_definition_of_done_is_scoped_and_loses_the_estates_own_verdict(tmp_path: Path) -> None:
    """48 rows on the reference bundle, and `status: failed` is the ESTATE's, not this unit's."""
    _full, scoped, _text = _package_estate_report(tmp_path)
    dod = scoped["definition_of_done"]
    assert [row["workbook"] for row in dod["workbooks"]] == [UNIT]
    assert dod["applicable"] is True
    for estate_only in ("status", "reports_bound", "reports_failed", "reports_warned"):
        assert estate_only not in dod, f"{estate_only} is an estate aggregate and reads as this unit's verdict"


def test_the_scoped_report_still_declares_both_collections_as_lists(tmp_path: Path) -> None:
    """Over-trimming is the opposite defect: both gates reject the file unless `workbooks` is a list.

    `check_reference_readiness._engine_report` returns None without it - which silently costs a
    datasource-only unit its earned `NOT_APPLICABLE` - and `check_unit._is_engine_report` stops
    recognising the package as engine output at all.

    ⚠️ The load-bearing case is a report that OMITS a collection, not one that merely filters to
    empty. An allowlist only emits keys the source actually had, so a report with no `datasources`
    key would ship without one - and the round-2 mutation campaign showed the earlier version of
    this test could not see that, because its fixture always supplied both.
    """
    _full, scoped, _text = _package_estate_report(tmp_path)
    assert isinstance(scoped["workbooks"], list)
    assert isinstance(scoped["datasources"], list)
    assert [entry["name"] for entry in scoped["workbooks"]] == [UNIT]

    bundle, oracle = _bundle(tmp_path)
    (bundle / "report.json").write_text(json.dumps({"tool": "migrate_estate"}), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    sparse = _packaged_report(tmp_path)
    assert sparse["workbooks"] == [] and sparse["datasources"] == []
    assert isinstance(sparse["workbooks"], list), "a report with no workbooks key must still declare one"


# --------------------------------------------------------------------------------------------
# 4b. the generated README as the package MAP (round-1 findings 2 and 3)
#
# Both findings were prose drifting from code, in opposite directions, and both are fail-CLOSED: an
# agent believes the README, cannot find what it names, and concludes the package is broken.
#
# * finding 2 - the README and the module layout comment said `dashboards/` and `worksheets/`; the
#   code emits `object_identity`'s KIND_* values, which are SINGULAR. The code is right (`Path(kind)`,
#   and the committed tests above assert `worksheet/data/Sales.csv`), so the prose was fixed.
# * finding 3 - the table presented itself as the package map while omitting `report.json`,
#   `source-provenance.json` and `engine-output-receipt.json`, all three of which ship in every
#   package and are load-bearing rather than incidental.
#
# So the guard is derived from the package the code ACTUALLY writes, never from a second hand-kept
# list - a list would drift exactly as the prose did.
# --------------------------------------------------------------------------------------------


def _package_with_receipt(tmp_path: Path) -> Path:
    """A package carrying every artifact the packager can emit, including the engine receipt."""
    bundle, oracle = _bundle(tmp_path)
    emitted = bundle / "pbip" / UNIT / f"{UNIT}.Report" / "definition" / "report.json"
    emitted.parent.mkdir(parents=True, exist_ok=True)
    emitted.write_text("{}", encoding="utf-8")
    (bundle / "engine-output-receipt.json").write_text(
        json.dumps({"version": 1, "engine": {"version": "2.339.0"}, "artifacts": []}), encoding="utf-8"
    )
    _package(tmp_path, bundle, oracle)
    return _out(tmp_path) / UNIT


def test_the_generated_readme_names_every_file_the_package_contains(tmp_path: Path) -> None:
    """Finding 3: three files shipped in every package and appeared nowhere in its own map."""
    root = _package_with_receipt(tmp_path)
    readme = (root / "README.md").read_text(encoding="utf-8")
    shipped = sorted(path.name for path in root.iterdir() if path.name != "README.md")
    missing = [name for name in shipped if name not in readme]
    assert missing == [], f"shipped but unnamed in the package's own README: {missing}"
    for load_bearing in ("report.json", "source-provenance.json", "engine-output-receipt.json"):
        assert load_bearing in shipped


def test_the_readme_names_the_oracle_kinds_exactly_as_the_code_emits_them(tmp_path: Path) -> None:
    """Finding 2: the directory IS `object_identity`'s kind value, so a pluralised copy is wrong."""
    root = _package_with_receipt(tmp_path)
    readme = (root / "README.md").read_text(encoding="utf-8")
    emitted = {path.name for path in (root / "oracle").iterdir() if path.is_dir()}
    assert emitted, "the fixture must emit at least one kind directory or this proves nothing"
    assert emitted <= set(pkg.KIND_DIRS)
    for kind in sorted(emitted):
        assert f"`{kind}/`" in readme, f"the README does not name the emitted directory {kind}/"
    assert "dashboards/" not in readme
    assert "worksheets/" not in readme


def test_the_module_layout_comment_names_the_oracle_kinds_the_code_emits() -> None:
    """The same drift, in the other documented copy - `package_unit.py`'s own layout sketch."""
    doc = pkg.__doc__ or ""
    assert "dashboards/{images,data}" not in doc
    assert "worksheets/{images,data}" not in doc
    for kind in pkg.KIND_DIRS:
        assert f"{kind}/{{images,data}}" in doc, f"the layout comment does not name {kind}/"


def test_the_readme_separates_the_png_and_svg_evidence_legs(tmp_path: Path) -> None:
    """They are different evidence, not duplicates: the PNG is looked at, the SVG is grepped.

    ⚠️ Round-2 review: this asserted only "not duplicates", the two extensions and `122`, so
    **deleting the entire zero-text caveat left it green**. The caveat is the half an agent acts on -
    it is what stops "the SVG has no text" being read as "the object has no content" - so it is
    asserted here explicitly, by the worksheets it names.

    The count was also wrong: **four** worksheets in that workbook carry zero `<text>` elements, not
    three. `Terminated By Year` was omitted when the finding was first written up.
    """
    readme = (_package_with_receipt(tmp_path) / "README.md").read_text(encoding="utf-8")
    assert "not duplicates" in readme
    assert "`.png`" in readme and "`.svg`" in readme
    assert "122" in readme
    assert "zero" in readme
    for silent in ("Hired By Year", "Terminated By Year", "Age Groups", "Education Levels"):
        assert silent in readme, f"the zero-text caveat does not name {silent}"


# --------------------------------------------------------------------------------------------
# 4c. the OTHER shipped manifests (round-2 blocker 2)
#
# Round 1 fixed one denylist. `package_oracle()` and `_scope_receipt()` were still denylists in
# their own right - measured on the packaged `HR_Dashboard`, the oracle manifest carried **22 fields
# byte-identical** to the 360-view estate manifest (`view_types` still totalling 360 beside a
# rewritten `view_count: 23`), and the receipt shipped two absolute `C:\\Users\\<user>\\...` paths.
# --------------------------------------------------------------------------------------------

ESTATE_ORACLE_EXTRA = {
    "view_count": 360,
    "view_types": {"dashboard": 60, "worksheet": 300, "unknown": 0},
    "captured_complete": 312,
    "failed": 47,
    "elapsed_sec": 6823.1,
    "total_retries": 8,
    "total_reauths": 0,
    "data_ok": 314,
    "image_ok": 313,
    "svg_ok": 313,
    "credential_scrubbed_at_sink": [],
    "reference_missing": False,
    "reference_required": True,
    "render_capability": {
        "selected_tier": "svg",
        "configured_api_version": "3.29",
        "probe_view_luid": "1e54f1a1-7655-487b-bcf1-a74f55cbacb4",
        "probe_view_name": FOREIGN,
        "probe_view_luids": ["1e54f1a1-7655-487b-bcf1-a74f55cbacb4"],
        "probe_views_tried": 1,
        "warnings": [f"probed against {FOREIGN}"],
    },
    "future_manifest_field": FOREIGN,
}


def _package_with_estate_oracle(tmp_path: Path) -> dict:
    """Package one unit whose capture manifest carries the estate's own aggregates and probe."""
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest.update(ESTATE_ORACLE_EXTRA)
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    return json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))


def test_the_oracle_manifest_recomputes_its_counts_from_the_packaged_views(tmp_path: Path) -> None:
    """`view_count` was rewritten while `view_types` still totalled the whole estate."""
    scoped = _package_with_estate_oracle(tmp_path)
    shipped = scoped["views"]
    assert scoped["view_count"] == len(shipped)
    assert sum(scoped["view_types"].values()) == len(shipped)
    assert scoped["view_types"] != ESTATE_ORACLE_EXTRA["view_types"]
    for leg in ("image", "svg", "pdf", "data"):
        expected = sum(1 for view in shipped if isinstance(view.get(leg), dict) and view[leg].get("status") == "ok")
        assert scoped[f"{leg}_ok"] == expected


def test_the_oracle_manifest_drops_estate_run_stats_and_the_foreign_probe(tmp_path: Path) -> None:
    """The probe view identifies a view in ANOTHER workbook - on the reference estate, `Superstore`."""
    scoped = _package_with_estate_oracle(tmp_path)
    assert FOREIGN not in json.dumps(scoped, ensure_ascii=False)
    for estate_only in ("captured_complete", "failed", "elapsed_sec", "total_retries", "total_reauths"):
        assert estate_only not in scoped
    capability = scoped.get("render_capability") or {}
    assert not [key for key in capability if key.startswith("probe_")]
    assert "warnings" not in capability
    assert capability["selected_tier"] == "svg", "the evidence GRADE must survive - that is the useful half"
    assert "future_manifest_field" in scoped["scope"]["dropped_fields"]


def test_the_receipt_keeps_the_engine_version_and_drops_installation_paths(tmp_path: Path) -> None:
    """Engine provenance is a VERSION, not a location on the machine that happened to build it."""
    bundle, oracle = _bundle(tmp_path)
    (bundle / "engine-output-receipt.json").write_text(
        json.dumps(
            {
                "version": 1,
                "created_at": "2026-09-02T07:05:08+00:00",
                "report_sha256": "a" * 64,
                "input_manifest_sha256": "b" * 64,
                "engine": {
                    "version": "2.339.0",
                    "canonical": True,
                    "source": "plugin",
                    "root": rf"C:\Users\someone\.copilot\installed-plugins\{FOREIGN}",
                    "plugin_root": rf"C:\Users\someone\.copilot\installed-plugins\{FOREIGN}",
                },
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    _package(tmp_path, bundle, oracle)
    scoped = json.loads((_out(tmp_path) / UNIT / "engine-output-receipt.json").read_text(encoding="utf-8"))
    assert scoped["engine"]["version"] == "2.339.0"
    assert "root" not in scoped["engine"] and "plugin_root" not in scoped["engine"]
    assert "report_sha256" not in scoped and "input_manifest_sha256" not in scoped
    assert FOREIGN not in json.dumps(scoped, ensure_ascii=False)
    assert "C:\\Users" not in json.dumps(scoped)
    assert set(scoped["scope"]["dropped_fields"]) >= {"engine.root", "engine.plugin_root", "report_sha256"}


def test_no_shipped_manifest_carries_an_absolute_host_path(tmp_path: Path) -> None:
    """One assertion over the WHOLE package - the round-1 claim was scoped to report.json alone."""
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest.update(ESTATE_ORACLE_EXTRA)
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (bundle / "report.json").write_text(json.dumps(_estate_report(UNIT)), encoding="utf-8")
    (bundle / "engine-output-receipt.json").write_text(
        json.dumps({"version": 1, "engine": {"version": "2.339.0", "root": r"C:\Users\someone\engine"}}),
        encoding="utf-8",
    )
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.json")
        if "C:\\\\Users" in path.read_text(encoding="utf-8") or "C:/Users" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"absolute host paths shipped in: {offenders}"


# --------------------------------------------------------------------------------------------
# 4d. repackaging (round-2 blocker 3) - the worst of the three
#
# An existing `<out>/<unit>` was merged into, never rebuilt, so an artifact the new input no longer
# produces was never removed. Re-running with an EMPTY oracle left the previous capture in place and
# the entry gate still returned READY - an agent builds against evidence that no longer exists, with
# a gate agreeing.
# --------------------------------------------------------------------------------------------


def test_repackaging_removes_evidence_the_new_input_no_longer_produces(tmp_path: Path) -> None:
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    assert (root / "oracle" / "oracle-manifest.json").is_file()
    assert _images(tmp_path)

    empty = tmp_path / "empty-capture"
    empty.mkdir()
    pkg.package_unit(bundle, UNIT, _out(tmp_path), oracle_dir=empty, assets_dir=bundle.parent / "assets")
    assert not (root / "oracle").exists(), "the previous capture survived a re-run that captured nothing"
    manifest = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))
    assert manifest["oracle"]["objects"] == []


def test_repackaging_removes_a_stale_file_from_every_copied_tree(tmp_path: Path) -> None:
    """Not just `oracle/`: stale `assets/` and `fabric/` files persisted the same way."""
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    planted = [
        root / "assets" / "left-over-from-a-previous-run.twb",
        root / "fabric" / f"{UNIT}.Report" / "definition" / "pages" / "stale-page.json",
        root / "handover" / "Someone_Else.json",
    ]
    for path in planted:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    _package(tmp_path, bundle, oracle)
    survivors = [str(path.relative_to(root)) for path in planted if path.exists()]
    assert survivors == [], f"stale artifacts survived repackaging: {survivors}"


def test_a_failed_repackage_leaves_the_previous_package_intact(tmp_path: Path) -> None:
    """Staging exists so a crash mid-build cannot replace a good package with half of one."""
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    before = sorted(path.name for path in root.iterdir())

    boom = pkg._assemble_unit  # pylint: disable=protected-access
    try:
        pkg._assemble_unit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("engine exploded"))
        with pytest.raises(RuntimeError):
            pkg.package_unit(bundle, UNIT, _out(tmp_path), oracle_dir=oracle, assets_dir=bundle.parent / "assets")
    finally:
        pkg._assemble_unit = boom

    assert sorted(path.name for path in root.iterdir()) == before
    assert not [path for path in _out(tmp_path).iterdir() if path.name.startswith(".")], "staging dir left behind"


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
