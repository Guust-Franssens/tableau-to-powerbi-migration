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

import errno
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_path_ceiling as cpc  # noqa: E402  # pylint: disable=wrong-import-position
import host_paths as hp  # noqa: E402  # pylint: disable=wrong-import-position
import manifest_scope as ms  # noqa: E402  # pylint: disable=wrong-import-position
import package_unit as pkg  # noqa: E402  # pylint: disable=wrong-import-position
import path_flavour as pf  # noqa: E402  # pylint: disable=wrong-import-position
import reference_evidence as rev  # noqa: E402  # pylint: disable=wrong-import-position
import set_data_folder as sdf  # noqa: E402  # pylint: disable=wrong-import-position
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
    # `row_count` present: this fixture is a MEASURED capture, and since #480 a data leg with no
    # measured rows is unassessable and ships no `path` at all -- which would make this test about
    # the wrong thing.
    manifest["views"][0]["data"] = {
        "status": "ok",
        "path": "data/sales.csv",
        "row_count": 1,
        "certification": "certified",
    }
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
# `scope_report` used to copy every key it did not explicitly filter. Measured by the reviewer on
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
#: can produce. `scope_report` must not emit it anywhere.
FOREIGN = "Zz_Foreign_Unit_Sentinel"

#: An absolute host path, ASSEMBLED AT RUNTIME so no tracked file contains the literal.
#:
#: The privacy gate (`scripts/set_data_folder.py --check`, a CI step) scans raw file TEXT and cannot
#: distinguish a fixture that must contain an absolute path in order to prove one is stripped from a
#: real leaked path - and it should not have to. Round-2 CI went red on exactly this: three fixture
#: lines here carried `<drive>:\Users\<account>\...` and were flagged, correctly, as indistinguishable
#: from a leak. Building the string from parts keeps the gate's teeth for real leaks while letting
#: this fixture stay realistic.
_SEP = chr(92)
HOST_PATH_ROOT = f"C:{_SEP}Users{_SEP}a-real-account"
HOST_PATH = f"{HOST_PATH_ROOT}{_SEP}.copilot{_SEP}installed-plugins"


def absolute_host_paths(payload: object, path: str = "") -> list[str]:
    """JSON paths whose STRING VALUE is an absolute host path, found by WALKING the parsed document.

    ⚠️ Deliberately not a substring search over serialized text, which is how the earlier version of
    this assertion was vacuous: `json.dumps` escapes each separator, so the serialized form carries a
    DOUBLED separator and a needle written with a single one can never appear - the assertion passed
    whether or not the path survived. Three separate errors this round came from matching text where
    the artifact is structured (a substring sentinel reporting `'Age Groups'` as the foreign workbook
    `Groups`; a host-path probe returning False against escaped JSON; and this).
    Walk the parse; do not grep the render.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.extend(absolute_host_paths(value, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(absolute_host_paths(value, f"{path}[{index}]"))
    elif isinstance(payload, str) and re.match(r"^[A-Za-z]:[\\/]{1,2}Users[\\/]", payload):
        found.append(path)
    return found


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
    copied wholesale, so `workbooks[0].future_nested` shipped automatically.

    Round 3 settled it by DESCOPING: the shipped row is a name and nothing else, so there is no
    container left to smuggle anything through.
    """
    _full, scoped, text = _package_estate_report(tmp_path)
    assert RETAINED_EXTENSION not in text
    assert list(scoped["workbooks"][0]) == ["name"]
    assert scoped["workbooks"][0]["name"] == UNIT


def test_an_unknown_field_inside_a_RETAINED_container_cannot_ship(tmp_path: Path) -> None:
    """⚠️ The control round 3 said was missing, and without which any fix here is unfalsifiable.

    Rounds 1-3 each closed one level and were followed by a deeper one, and the 60-test suite plus a
    17/17 mutation campaign stayed green throughout because **nothing probed an unknown field inside
    a RETAINED container**. This is that probe, at both depths round 3 exploited:
    `workbooks[].model_facts.future_install_root` in the report, and
    `views[].image.future_source_path` in the oracle manifest.

    It is also the reason `KEEP` is now scalar-only: a container arriving at a scalar leaf raises
    `UnscopedStructure` rather than shipping its grandchildren, so this class fails LOUDLY at
    packaging time instead of being discovered a round later.
    """
    bundle, oracle = _bundle(tmp_path)
    full = _estate_report(UNIT)
    full["workbooks"][0]["model_facts"] = {"tables": 3, "future_install_root": RETAINED_EXTENSION}
    (bundle / "report.json").write_text(json.dumps(full), encoding="utf-8")

    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0].setdefault("image", {"status": "skipped"})["future_source_path"] = RETAINED_EXTENSION
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    for shipped in root.rglob("*.json"):
        assert RETAINED_EXTENSION not in shipped.read_text(encoding="utf-8"), f"leaked via {shipped.name}"
    assert "workbooks[].model_facts" in _packaged_report(tmp_path)["scope"]["dropped_fields"]


def test_a_container_at_a_scalar_leaf_fails_loudly_rather_than_shipping() -> None:
    """A structure with no spec must be a hard error at packaging time, never a silent pass-through."""
    with pytest.raises(ms.UnscopedStructure) as excinfo:
        project({"name": {"nested": RETAINED_EXTENSION}}, {"name": KEEP})
    assert "name is a dict" in str(excinfo.value)
    assert RETAINED_EXTENSION not in str(excinfo.value), "the error must not echo the value it refused"

    with pytest.raises(ms.UnscopedStructure):
        project({"tiers": [{"tier": "svg"}]}, {"tiers": ms.SCALAR_LIST})
    assert project({"tiers": ["png", "svg"]}, {"tiers": ms.SCALAR_LIST}) == ({"tiers": ["png", "svg"]}, [])


def test_a_dropped_nested_field_is_recorded_by_its_full_path(tmp_path: Path) -> None:
    """An omission must be discoverable: the path is engine schema, the value is the estate content."""
    _full, scoped, _text = _package_estate_report(tmp_path)
    dropped = scoped["scope"]["dropped_fields"]
    assert "workbooks[].future_nested" in dropped
    assert "input_manifest" in dropped
    assert "definition_of_done" in dropped
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


def test_the_report_is_descoped_to_the_classification_the_gates_read(tmp_path: Path) -> None:
    """Round 3: DELETE the surface rather than enumerate it a fourth time.

    `definition_of_done` was carried in round 2 as "the engine's own verdict on this unit". It is not
    read by either gate, and on the reference bundle its `workbooks[]` held 48 rows - a second copy
    of the estate. The same is true of every other field. What the gates read is a NAME, so a name is
    all that ships, and with it goes every container-valued field this file could hide anything in.
    """
    _full, scoped, _text = _package_estate_report(tmp_path)
    assert sorted(scoped) == ["datasources", "generated_at", "scope", "scoped_by", "tool", "workbooks"]
    assert "definition_of_done" not in scoped
    assert all(list(row) == ["name"] for row in scoped["workbooks"] + scoped["datasources"])


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
    """A package carrying every artifact the packager can emit: receipt, model, and imported rows.

    The imported CSV is part of the SHARED fixture rather than a private one, because the package
    map guard below is derived from what the packager actually writes - so a fixture whose model
    imports nothing makes `data/` invisible to it. Measured: with a data-less fixture the mutation
    "drop the data/ row from the README" SURVIVED the whole campaign.
    """
    bundle, oracle = _bundle(tmp_path)
    emitted = bundle / "pbip" / UNIT / f"{UNIT}.Report" / "definition" / "report.json"
    emitted.parent.mkdir(parents=True, exist_ok=True)
    emitted.write_text("{}", encoding="utf-8")
    (bundle / "engine-output-receipt.json").write_text(
        json.dumps({"version": 1, "engine": {"version": "2.339.0"}, "artifacts": []}), encoding="utf-8"
    )
    payload = tmp_path / "extract" / "federated_abc" / "Extract_Extract.csv"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text("Employee_ID,Salary\n1,100\n", encoding="utf-8")
    _point_partition_at(bundle, str(payload))
    _package(tmp_path, bundle, oracle)
    return _out(tmp_path) / UNIT


def test_the_generated_readme_names_every_file_the_package_contains(tmp_path: Path) -> None:
    """Finding 3: three files shipped in every package and appeared nowhere in its own map.

    ⚠️ The membership test is a backticked TABLE KEY, not a bare substring. Measured while adding
    the `data/` row for #461: with a substring check, replacing that row's key survived the whole
    mutation campaign, because the README says `oracle/*/data/*.csv` elsewhere and `"data" in
    readme` was true either way. A short shipped name is exactly the case a substring check cannot
    decide, and every shipped name here is short.
    """
    root = _package_with_receipt(tmp_path)
    readme = (root / "README.md").read_text(encoding="utf-8")
    shipped = sorted(path.name for path in root.iterdir() if path.name != "README.md")
    missing = [name for name in shipped if not re.search(rf"`{re.escape(name)}[/`]", readme)]
    assert missing == [], f"shipped but unnamed in the package's own README: {missing}"
    for load_bearing in ("report.json", "source-provenance.json", "engine-output-receipt.json", "data"):
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


def test_the_readme_leads_with_the_csv_numeric_oracle_before_any_image(tmp_path: Path) -> None:
    """The CSVs are the numeric oracle, and the README used to bury them in a table cell.

    Measured on the 2026-09-03 cold run, by the agent that worked from the package: two of its three
    targets would have been near-useless as SVG data oracles (`Cities` carries 9 `<text>` elements,
    `States` 24), while `oracle/worksheet/data/States.csv` handed over `New York 6,270 /
    Michigan 976` plus the `Rank Top 2` boolean with no OCR and no judgement. So the ORDER is the
    finding: whichever evidence the README names first is the one an agent reaches for.
    """
    readme = (_package_with_receipt(tmp_path) / "README.md").read_text(encoding="utf-8")
    csv_at = readme.find("`oracle/*/data/*.csv`")
    assert csv_at != -1, "the README does not name the CSV oracle by its glob"
    assert "NUMERIC oracle" in readme[csv_at : csv_at + 120]
    first_image = min(readme.find("`.png`"), readme.find("`.svg`"))
    assert first_image != -1
    assert csv_at < first_image, "the README still introduces the image legs before the numeric oracle"


def test_the_readme_keeps_the_png_and_svg_legs_distinct_with_the_zero_text_caveat(tmp_path: Path) -> None:
    """They are different evidence: the PNG is looked at, the SVG is grepped - and may be empty.

    ⚠️ Round-2 review of the original guard: it asserted only "not duplicates", the two extensions
    and a measured count, so **deleting the entire zero-text caveat left it green**. The caveat is
    the half an agent acts on - it is what stops "the SVG has no text" being read as "the object has
    no content" - so both halves are asserted, and neither is a count that a re-measurement retires.
    """
    readme = " ".join((_package_with_receipt(tmp_path) / "README.md").read_text(encoding="utf-8").split())
    assert "`.png`" in readme and "`.svg`" in readme
    assert "LOOK at" in readme, "the README no longer says what the PNG leg is FOR"
    assert "`<text>`" in readme, "the README no longer says what the SVG leg is FOR"
    assert "zero text is not zero content" in readme, "the zero-text caveat has been deleted"


def test_the_readme_states_the_page_pairing_contract(tmp_path: Path) -> None:
    """`check_unit.check_page_parity` pairs on the exact page name, and a zero-visual page FAILS.

    The cold-run agent had to read `check_unit.py` to learn this, which is the trip the package
    exists to remove. Verified against the source rather than the brief, three claims:

    * `actual_pages` (:1244) takes ``displayName``, and `page_expectation` (:1148) pairs only
      ``rendered`` pages - those `_page_visual_count` (:1213) finds at least one ``visual.json``
      under. Its docstring names the reason: without it, "renaming an empty page to an expected
      page's title certified it as rebuilt".
    * a zero-visual page that is not the engine's crash-guard placeholder is `blank`
      (`_zero_visual_pages`, :1745), and `blank` is one of the four conditions that force
      ``STATUS_PRECONDITION_FAILED`` (:1527). So it FAILS rather than merely going uncredited -
      asserted separately below, because "at least one visual" alone reads as "not counted".
    * the expected set is dashboards PLUS ORPHAN worksheets (`_spec_pages`, :853-854). Measured in
      `expected_pages`' own docstring: 19 of 43 workbooks in a real estate have zero dashboards, so
      "dashboards only" would grade those against an EMPTY expected set.
    """
    readme = " ".join((_package_with_receipt(tmp_path) / "README.md").read_text(encoding="utf-8").split())
    assert "`displayName`" in readme
    assert "at least one visual" in readme, "the README does not say a zero-visual page is not rebuilt"
    assert "`blank` and FAILS" in readme, "the README does not say a zero-visual page FAILS the gate"
    assert "every worksheet not placed on one" in readme, "the README narrows the expected set to dashboards"


def test_the_package_ships_the_spec_schema_it_tells_an_agent_to_obey(tmp_path: Path) -> None:
    """Shipping the contract beats describing it: the cold-run agent invented a shape and lost six entries.

    The file is asserted to be byte-identical to `docs/migration-spec.schema.json`, because a scoped
    extract is a copy, and a copy of a contract drifts from the contract `validate_spec.py` enforces.
    """
    root = _package_with_receipt(tmp_path)
    shipped = root / "migration-spec.schema.json"
    assert shipped.is_file(), "the package does not ship the spec contract"
    assert shipped.read_bytes() == pkg.SPEC_SCHEMA.read_bytes()
    manifest = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"]["migration_spec_schema"] == "migration-spec.schema.json"

    schema = json.loads(shipped.read_text(encoding="utf-8"))
    limitation = schema["properties"]["limitations_encountered"]["items"]
    assert limitation["additionalProperties"] is False
    assert sorted(limitation["required"]) == ["issue", "item", "severity", "stage"]


def test_the_readme_names_the_provenance_ceiling_it_cannot_lift(tmp_path: Path) -> None:
    """An agent must learn up front which finding it can NEVER clear from inside the package.

    Measured on the shipped `HR_Dashboard` package: `origin.match` is `name_only`, every emitted page
    reports `UNVERIFIABLE - REVISION NOT ESTABLISHED`, and the gate exits 1. The remedy needs Tableau
    Server credentials AND the three `origin` fields `scope.dropped_fields` strips, so it is not
    reachable from here - which is worth a few lines and is otherwise a whole budget spent finding
    out.
    """
    readme = " ".join((_package_with_receipt(tmp_path) / "README.md").read_text(encoding="utf-8").split())
    section = readme.split("## UNFIXABLE FROM THIS PACKAGE", 1)
    assert len(section) == 2, "the README has no UNFIXABLE section"
    body = section[1]
    assert '`origin.match: "name_only"`' in body
    for stripped in ("origin.remote_sha256", "origin.server", "origin.site"):
        assert stripped in body, f"the ceiling does not name the dropped field {stripped}"
    assert "stamp_tableau_provenance.py" in body
    assert "NEVER exit 0" in body, "the consequence is not stated plainly"


def test_AGENTS_md_and_the_package_readme_agree_on_where_an_agent_edits(tmp_path: Path) -> None:
    """Issue #460: two shipped documents named two different canonical edit locations.

    `AGENTS.md`'s three-locations table said `<bundle>/pbip/`; the generated package README said
    `<package>/fabric/` - and `_copy_fabric` makes the second a `shutil.copytree` of the first, so
    they are byte-identical until one is edited and diverge silently thereafter. Promoting from the
    wrong one discards every agent edit.

    The guard is a CROSS-DOCUMENT derivation, not two copies of a string: it reads which row the
    README marks `**edit here**`, takes that row's own path, and requires `AGENTS.md`'s working-copy
    row to name it under `<package>/`. Change either side alone and this fails.
    """
    agents_row = _agents_working_copy_row()
    readme = (_package_with_receipt(tmp_path) / "README.md").read_text(encoding="utf-8")
    edit_rows = [line for line in readme.splitlines() if "**edit here**" in line]
    assert len(edit_rows) == 1, f"the README must mark exactly one tree as the edit location: {edit_rows}"
    edit_path = edit_rows[0].split("|")[1].strip().strip("`")

    assert f"<package>/{edit_path}" in agents_row, (
        f"the package README tells an agent to edit `{edit_path}`, but AGENTS.md's working-copy row "
        f"does not name <package>/{edit_path}: {agents_row.strip()}"
    )
    assert "CANONICAL" in agents_row, "AGENTS.md does not say which tree wins when both exist"
    assert "canonical" in edit_rows[0], "the README does not say the package tree wins"


def test_declared_edit_tooling_is_scoped_to_bundle_work_in_BOTH_documents(tmp_path: Path) -> None:
    """The cheap half of #460: `declare_generated_edit.py` cannot run on package work at all.

    The cold-run agent reported its edits were "undeclarable by construction", so the tamper
    machinery that argued for keeping the bundle canonical was not protecting the package path
    anyway. Both documents must say so, or one of them sends an agent to run a tool that cannot run.
    """
    agents_row = _agents_working_copy_row()
    readme = (_package_with_receipt(tmp_path) / "README.md").read_text(encoding="utf-8")
    for text, where in ((agents_row, "AGENTS.md"), (readme, "the package README")):
        assert "declare_generated_edit.py" in text, f"{where} does not name the declared-edit tool"
        assert "--tamper" in text, f"{where} does not name the tamper check"
        assert "bundle" in text.lower(), f"{where} does not scope the declared-edit tooling to bundle work"


def _agents_working_copy_row() -> str:
    """The one `working copy` row of AGENTS.md's synced three-locations table.

    Read from inside `<!-- BEGIN:shared-conventions -->` on purpose: that block is what
    `sync_agent_conventions.py` copies into every persona, so a row asserted here is a row every
    subagent actually receives.
    """
    text = (Path(__file__).resolve().parents[1] / "AGENTS.md").read_text(encoding="utf-8")
    block = text.split("<!-- BEGIN:shared-conventions -->")[1].split("<!-- END:shared-conventions -->")[0]
    rows = [line for line in block.splitlines() if line.strip().startswith("| working copy |")]
    assert len(rows) == 1, f"expected exactly one working-copy row in the synced block, got {len(rows)}"
    return rows[0]


# --------------------------------------------------------------------------------------------
# 4b-2. self-containment: the model's own rows (issue #461)
#
# `_copy_fabric` copies the engine's working copy verbatim, and the engine writes an ABSOLUTE,
# machine-local path into every import partition, pointing back into the originating
# `<bundle>/data/`. Measured across the 67 packaged units of estate run 408: 23 such references in
# 17 units, 11.2 MB of extracts the package did not carry. No existing gate saw it - `check_unit.py`
# passed page parity and oracle coverage on `HR_Dashboard`, and `powerbi-report-author validate` was
# clean - so these tests are the only thing standing between that defect and its return.
# --------------------------------------------------------------------------------------------


def _model_definition(root: Path) -> Path:
    """The packaged model's `definition/` directory, asserted to exist so a typo cannot vacate a test."""
    models = [path for path in (root / "fabric").iterdir() if path.name.endswith(".SemanticModel")]
    assert len(models) == 1, f"expected exactly one packaged model, got {[path.name for path in models]}"
    definition = models[0] / "definition"
    assert definition.is_dir(), f"no definition/ under {models[0]}"
    return definition


def _absolute_literals(root: Path) -> list[tuple[str, str]]:
    """Every quoted absolute path in every packaged `.tmdl`, as `(file name, literal)`."""
    quoted = re.compile(r"\"([A-Za-z]:[\\/][^\"]*|\\\\[^\"]*|/[A-Za-z][^\"]*)\"")
    return [
        (path.name, match.group(1))
        for path in root.rglob("*.tmdl")
        for match in quoted.finditer(path.read_text(encoding="utf-8"))
    ]


def test_no_packaged_tmdl_points_at_an_absolute_path_OUTSIDE_the_package(tmp_path: Path) -> None:
    """Issue #461's acceptance criterion, tightened by round-2 finding 1.

    The rule used to be "every absolute literal that survives must resolve INSIDE the package", which
    a package satisfied by naming its own build-time location - so it was true on the builder's
    machine and false everywhere the package was actually used. The rule is now stronger and does not
    depend on where the package is: a shipped `.tmdl` names NO absolute path on any machine's
    filesystem at all. Rows are reached through a `<PACKAGE_ROOT>` placeholder the recipient binds.

    The positive control is the bundle the package was built FROM: it must still carry the absolute
    literal, or this asserts nothing about a packager that simply had nothing to repair.
    """
    bundle, oracle = _bundle(tmp_path)
    payload = tmp_path / "extract" / "federated_abc" / "Extract_Extract.csv"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text("Employee_ID,Salary\n1,100\n", encoding="utf-8")
    _point_partition_at(bundle, str(payload))
    before = _absolute_literals(bundle / "pbip" / UNIT)
    assert [value for _name, value in before] == [str(payload)], (
        f"the fixture bundle carries no absolute literal to repair, so this proves nothing: {before}"
    )

    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    assert _absolute_literals(root) == [], "the packaged model still names a path on this machine"
    assert pkg.PACKAGE_ROOT_TOKEN in (_model_definition(root) / pkg.EXPRESSIONS_TMDL).read_text(encoding="utf-8")


def test_a_folder_PARAMETER_pointing_out_of_the_package_is_moved_with_its_files(tmp_path: Path) -> None:
    """The shape a `File.Contents`-only scan cannot see, and it is 9 of the estate's 31 literals.

    Measured on estate run 408: every datasource-only unit carries
    ``expression SourceFolder = "<bundle>\\pbip\\<Unit>\\<Unit>.Data"`` with partitions doing
    ``File.Contents(#"SourceFolder" & "\\Sample - Superstore.xlsx")``. Scanning only for
    ``File.Contents("<absolute>")`` closed 17 of 26 affected units; this shape is the other 9.

    The parameter is REUSED, not replaced - the partitions' concatenation was written against its
    separator convention - so the assertion is that its value moved and the files came with it. Since
    round-2 finding 1 the moved value is placeholder-rooted, so "moved" is asserted against the
    package's own `data/` tail rather than against a machine path.
    """
    bundle, oracle = _bundle(tmp_path)
    folder = tmp_path / "SharedSource.Data"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Sample - Superstore.xlsx").write_text("workbook bytes", encoding="utf-8")
    _point_partition_at(bundle, folder=str(folder), leaf="Sample - Superstore.xlsx")
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT

    value = _folder_parameter(root)
    assert value.startswith(pkg.PACKAGE_ROOT_TOKEN), f"the folder parameter names a machine: {value}"
    assert "SharedSource.Data" in value, f"the parameter lost the tail its partitions read: {value}"
    assert (root / "data" / "SharedSource.Data" / "Sample - Superstore.xlsx").is_file()
    shipped = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))["data_sources"]["shipped"]
    assert [row["path"] for row in shipped] == ["data/SharedSource.Data/Sample - Superstore.xlsx"]


def test_a_POSIX_literal_with_no_file_suffix_is_left_alone(tmp_path: Path) -> None:
    """At face value a POSIX-absolute literal is mostly a FALSE POSITIVE, and acting on one is a bug.

    Measured on estate run 408: 9 POSIX-absolute literals, 8 of them false - a Databricks
    ``HttpPath = "/sql/1.0/warehouses/<id>"`` in three units, and a bare ``"/"`` inside a
    ``TableauFormula`` annotation in two more. Requiring a file suffix keeps the one genuine hit (a
    macOS ``.xlsx``) and drops all eight, so both halves are asserted here from one fixture.

    ⚠️ ``_is_path_literal`` being False is "do not ACT on it", not "definitely not a path": it
    collapses ``NOT_A_PATH`` and ``UNCLASSIFIED``. The endpoint is cleared by its ROLE, in the
    fixture's measured shape - a ``HttpPath`` field, never a ``File.Contents`` argument, which no
    Databricks model writes. The same string in a filesystem position is still reported, and
    ``test_an_unassessable_POSIX_literal_is_RECORDED_not_silently_cleared`` pins that.
    """
    for value in ("/sql/1.0/warehouses/764e5801f0e0fac8", "/", "/mnt/lake/warehouse"):
        assert not pkg._is_path_literal(value), f"{value} would be treated as a file path"  # noqa: SLF001
    for value in ("/Users/<person>/Data/Global Superstore.xlsx", r"C:\data\x.csv", r"\\host\share\x.csv"):
        assert pkg._is_path_literal(value), f"{value} would be ignored"  # noqa: SLF001
    assert pkg._path_verdict("/") == pkg.NOT_A_PATH  # noqa: SLF001
    assert pkg._path_verdict("/mnt/lake/warehouse") == pkg.UNCLASSIFIED  # noqa: SLF001

    bundle, oracle = _bundle(tmp_path)
    _point_partition_at(bundle, http_path="/sql/1.0/warehouses/764e5801f0e0fac8")
    _package(tmp_path, bundle, oracle)
    record = json.loads(((_out(tmp_path) / UNIT) / "package-manifest.json").read_text(encoding="utf-8"))
    assert record["data_sources"] == {
        "parameter": None,
        "shipped": [],
        "omissions": [],
        "bytes": 0,
        "neutralized": [],
        "retained_network": [],
        "binding": None,
        "self_contained": True,
    }


def test_an_unassessable_POSIX_literal_is_RECORDED_not_silently_cleared(tmp_path: Path) -> None:
    """The bucket the endpoint carve-out must not empty (blind-review finding 5).

    ``/mnt/lake/warehouse`` is the SAME shape as the Databricks endpoint cleared above - POSIX
    absolute, no suffix, no trailing separator - and nothing in the string separates them. What
    separates them is the role: here it is the argument of ``File.Contents``, which reads files and
    nothing else, so the literal is a path this packager could not ship and the run must say so.
    Delete the ``UNCLASSIFIED`` branch of ``_external_after_rewrite`` and this goes red while the
    test above stays green - that pairing is the whole contract.
    """
    bundle, oracle = _bundle(tmp_path)
    _point_partition_at(bundle, "/mnt/lake/warehouse")
    _package(tmp_path, bundle, oracle)
    record = json.loads(((_out(tmp_path) / UNIT) / "package-manifest.json").read_text(encoding="utf-8"))
    assert record["data_sources"]["shipped"] == []
    assert [row["file"] for row in record["data_sources"]["omissions"]] == ["warehouse"]
    assert record["data_sources"]["omissions"][0]["reason"] == pkg.UNCLASSIFIED_REASON


def test_a_UNC_literal_is_refused_WITHOUT_being_probed(tmp_path: Path) -> None:
    """Probing a UNC host that does not exist blocks on SMB name resolution, for MINUTES.

    Measured by PR #462: it took one test module from 30 seconds to **52 minutes** and starved a
    subprocess into its 600 s timeout. So containment is judged lexically and a UNC source is
    refused unprobed - loud and instant. The test is time-boxed, because the whole failure mode is
    that it does not come back: a wall-clock assertion is the only one that can observe it.
    """
    started = time.monotonic()
    assert not pkg._inside(tmp_path, r"\\no-such-host-461\share\data.csv")  # noqa: SLF001
    readable, refusal = pkg._classify_source(r"\\no-such-host-461\share\data.csv")  # noqa: SLF001
    elapsed = time.monotonic() - started
    assert readable is None
    assert "UNC" in refusal and "block" in refusal
    assert elapsed < 5, f"the UNC literal was probed: {elapsed:.1f}s"


def test_containment_is_judged_in_the_LITERALS_OWN_flavour_including_its_case_rules() -> None:
    """Round-2 finding 2: `_inside` answered every question through `PureWindowsPath`.

    That class compares case-INSENSITIVELY, which is right for `C:\\Pkg` and wrong for `/pkg`. On
    Linux an external `/data/Extract.csv` was therefore judged INSIDE a package at `/DATA`, and
    "inside the package" is the one verdict that produces total silence: localization skips it,
    the post-rewrite scan skips it, nothing is shipped, nothing is recorded, nothing is rewritten.
    The reviewer measured exactly that - `record={'parameter': None, 'shipped': [], 'omissions': [],
    'bytes': 0}` with `post_scan=[] writes=0`.

    Both directions are asserted from one pair, because a fix that simply made everything
    case-sensitive would break the Windows half, where `C:\\PKG` and `c:\\pkg` ARE one directory.
    """
    assert pkg._inside(PureWindowsPath(r"C:\PKG"), r"c:\pkg\data\x.csv")  # noqa: SLF001
    assert not pkg._inside(PurePosixPath("/DATA"), "/data/Extract.csv")  # noqa: SLF001
    assert pkg._inside(PurePosixPath("/data"), "/data/Extract.csv")  # noqa: SLF001

    # A literal of the other flavour is never contained, whichever way round it is asked.
    assert not pkg._inside(PurePosixPath("/tmp/out/Book"), r"C:\tmp\out\Book\data\x.csv")  # noqa: SLF001
    assert not pkg._inside(PureWindowsPath(r"C:\out\Book"), "/out/Book/data/x.csv")  # noqa: SLF001
    # ... and `..` is still collapsed, so escaping through the package root is not "inside" it.
    assert not pkg._inside(PurePosixPath("/pkg"), "/pkg/../etc/shadow")  # noqa: SLF001
    assert not pkg._inside(PureWindowsPath(r"C:\pkg"), r"C:\pkg\..\Windows\x.csv")  # noqa: SLF001


def test_a_foreign_flavour_source_is_REFUSED_rather_than_reinterpreted_by_the_host() -> None:
    """Round-2 finding 2: `Path` is the host's, and a foreign literal is not refused by it - it is
    RESOLVED, against the current drive.

    On Windows `Path("/Users/<person>/Data/x.xlsx").is_file()` asks about
    `C:\\Users\\<person>\\Data\\x.xlsx`. The reviewer measured a foreign macOS literal being
    `accepted_as` a local path with `refusal=None`, which is not a near miss: whatever bytes happen
    to sit there are packaged as the customer's data source, and the manifest says the source was
    shipped.
    """
    foreign = "/opt/data/customer.xlsx" if os.name == "nt" else r"C:\opt\data\customer.xlsx"
    readable, refusal = pkg._classify_source(foreign)  # noqa: SLF001
    assert readable is None
    assert refusal is not None and "cannot resolve" in refusal
    readable, refusal = pkg._classify_source(foreign, expect_dir=True)  # noqa: SLF001
    assert readable is None and refusal is not None


@pytest.mark.skipif(os.name != "nt", reason="reproduces the WINDOWS half: Path resolves / against the current drive")
def test_a_posix_literal_on_windows_cannot_package_unrelated_local_bytes(tmp_path: Path) -> None:
    """The reviewer's experiment, run on a file that really exists.

    A real local file is addressed by its drive-less, POSIX-slashed form. The host would resolve
    that to the same file and package it as the source the model named; the packager must not.
    """
    local = tmp_path / "unrelated.csv"
    local.write_text("local,bytes\n1,2\n", encoding="utf-8")
    posix_form = str(local).split(":", 1)[1].replace("\\", "/")
    assert Path(posix_form).is_file(), "the host does resolve this, which is the whole hazard"

    readable, refusal = pkg._classify_source(posix_form)  # noqa: SLF001
    assert readable is None, f"unrelated local bytes were accepted as {posix_form}"
    assert refusal is not None


def test_a_source_id_written_with_the_OTHER_separator_still_resolves_its_asset(tmp_path: Path) -> None:
    """Round-2 finding 2: `resolve_asset` split the source id with `Path(...).name`.

    A `source_id` is written by whichever machine ran the harvest, so
    `_runs\\999-x\\assets\\Book.twb` reaching a POSIX packaging host has, to `Path`, no separators
    at all: its `.name` is the whole string, no asset matches, and the unit packages with no source
    - both gates then report CANNOT_ESTABLISH on a unit whose asset was sitting right there.

    ⚠️ This can only FAIL on POSIX (on Windows `Path` reads both separators), which is why it
    asserts the flavour-free helper directly as well - that assertion fails on either platform.
    """
    assert pf.leaf(r"_runs\999-x\assets\Book.twb") == "Book.twb"
    assert pf.leaf("_runs/999-x/assets/Book.twb") == "Book.twb"

    bundle, _oracle = _bundle(tmp_path)
    asset_name = f"{WB_LUID}_{UNIT}.twb"
    for source_id in (f"_runs\\999-x\\assets\\{asset_name}", f"_runs/999-x/assets/{asset_name}"):
        resolved, route = pkg.resolve_asset(
            bundle, UNIT, {"workbook": {"source_id": source_id}}, bundle.parent / "assets"
        )
        assert resolved is not None and resolved.name == asset_name, f"{source_id} did not resolve"
        assert route == "handover.workbook.source_id"

    """ "Absolute AND not under the destination", never "absolute" (finding from PR #462).

    `set_data_folder.py`'s existing convention is an absolute `DataFolder` under the deliverable, and
    it is exactly what both repairs here produce - so a rule that flagged every absolute path would
    condemn its own output and, worse, make the legitimate convention look like a regression.
    """
    root = _package_with_receipt(tmp_path)
    inside = str(root / "data" / "federated_abc" / "Extract_Extract.csv")
    assert pkg._is_path_literal(inside)  # noqa: SLF001
    assert pkg._inside(root, inside)  # noqa: SLF001
    assert pkg._inside(root, str(root / "data") + "\\")  # noqa: SLF001
    assert not pkg._inside(root, str(root.parent / "elsewhere" / "x.csv"))  # noqa: SLF001
    record = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))
    assert record["data_sources"]["omissions"] == [], "the packager reported its own legitimate output"


def _folder_parameter(root: Path) -> str:
    """The value of the model's own folder parameter, whichever name the engine gave it."""
    text = "".join(path.read_text(encoding="utf-8") for path in _model_definition(root).rglob("*.tmdl"))
    match = re.search(r'expression\s+(?:#"[^"]+"|[^\s=]+)\s*=\s*"([^"]*)"\s*meta', text)
    assert match is not None, text
    return match.group(1)


def test_the_rows_the_model_imports_are_shipped_and_the_partition_reads_them(tmp_path: Path) -> None:
    """Self-containment is bytes AND wiring: either half alone leaves the package unusable.

    A copy nothing points at is dead weight; a rewritten partition with no copy behind it fails only
    at refresh time, on someone else's machine, months later. So the shipped file is asserted to
    exist on disk, the partition is asserted to read it through the parameter, and the two are
    joined by the manifest rather than by two independent string checks.
    """
    root = _package_with_receipt(tmp_path)
    record = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))["data_sources"]
    assert record["shipped"], "no data source was shipped, so the fixture does not exercise #461"
    assert record["omissions"] == []
    assert record["parameter"] == pkg.DATA_FOLDER_PARAM

    partitions = "".join(path.read_text(encoding="utf-8") for path in _model_definition(root).rglob("*.tmdl"))
    for row in record["shipped"]:
        shipped = root / row["path"]
        assert shipped.is_file(), f"the manifest claims {row['path']} but nothing is there"
        assert shipped.stat().st_size == row["bytes"]
        relative = row["path"].split(f"{pkg.DATA_DIR}/", 1)[1].replace("/", os.sep)
        assert f'{pkg.DATA_FOLDER_PARAM} & "{relative}"' in partitions, "no partition reads the shipped copy"


def test_the_data_folder_parameter_names_a_PLACEHOLDER_not_the_machine_that_built_it(tmp_path: Path) -> None:
    """Round-2 finding 1: the value used to be the package's absolute build-time location.

    Two failures in one value. It named the STAGING directory in the original defect - a path that
    stops existing the moment packaging succeeds - and, once that was fixed to ``final``, it named
    the builder's own output folder, so moving the handover package left its rows present on disk
    and unreachable, and shipped a `C:\\Users\\<name>\\...` to a customer.

    The shipped value is a placeholder; binding resolves it, and that is asserted here end to end
    rather than trusted, because a placeholder nobody can resolve is not an improvement.
    """
    root = _package_with_receipt(tmp_path)
    expressions = (_model_definition(root) / pkg.EXPRESSIONS_TMDL).read_text(encoding="utf-8")
    value = re.search(rf'expression {pkg.DATA_FOLDER_PARAM} = "([^"]+)"', expressions)
    assert value is not None, expressions
    assert value.group(1).startswith(pkg.PACKAGE_ROOT_TOKEN), value.group(1)
    staging = pkg.staging_dir(root.parent, UNIT).name
    assert staging not in value.group(1), f"the parameter names staging: {value.group(1)}"
    assert str(root) not in value.group(1), "the package names the machine that built it"
    assert str(tmp_path) not in expressions, "some other build-time path survived into the model"
    foreign = "/" if os.sep == "\\" else "\\"
    assert foreign not in value.group(1), f"the parameter mixes path separators: {value.group(1)}"

    binding = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))["data_sources"]["binding"]
    assert binding["state"] == "unbound" and binding["token"] == pkg.PACKAGE_ROOT_TOKEN
    assert sdf._package(root) == 0, "the package could not be bound to its own location"  # noqa: SLF001
    bound = re.search(
        rf'expression {pkg.DATA_FOLDER_PARAM} = "([^"]+)"',
        (_model_definition(root) / pkg.EXPRESSIONS_TMDL).read_text(encoding="utf-8"),
    )
    assert bound is not None
    named = Path(bound.group(1))
    assert named.is_dir(), f"binding named a directory that does not exist: {named}"
    assert named.resolve() == (root / pkg.DATA_DIR).resolve()


def test_a_moved_package_still_reaches_its_rows_once_it_is_BOUND(tmp_path: Path) -> None:
    """The acceptance test for the design decision, and the one the old value could not pass.

    Round-2 finding 1 measured `embedded_path_exists_after_move=False` beside
    `packaged_data_exists_after_move=True`: the rows moved with the folder, and the model kept
    naming where they used to be. A handover package exists to be handed over, so this walks the
    whole route - package here, MOVE the folder, bind it there, and read the file the partition now
    names off disk.
    """
    root = _package_with_receipt(tmp_path)
    moved = tmp_path / "customer" / "delivered" / UNIT
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root), str(moved))

    assert sdf._package(moved) == 0  # noqa: SLF001
    text = (_model_definition(moved) / pkg.EXPRESSIONS_TMDL).read_text(encoding="utf-8")
    value = re.search(rf'expression {pkg.DATA_FOLDER_PARAM} = "([^"]+)"', text)
    assert value is not None and Path(value.group(1)).is_dir()
    tail = re.search(
        rf'{pkg.DATA_FOLDER_PARAM} & "([^"]+)"',
        "".join(path.read_text(encoding="utf-8") for path in _model_definition(moved).rglob("*.tmdl")),
    )
    assert tail is not None, "no partition reads the shipped copy through the parameter"
    reached = Path(value.group(1) + tail.group(1))
    assert reached.is_file(), f"the bound model names rows that are not there: {reached}"
    assert reached.read_text(encoding="utf-8").startswith("Employee_ID")


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        (PureWindowsPath(r"C:\runs\packages\out\Book"), "<PACKAGE_ROOT>\\data\\"),
        (PurePosixPath("/tmp/pytest-0/packages/out/Book"), "<PACKAGE_ROOT>/data/"),
    ],
)
def test_the_data_folder_value_never_mixes_separators_on_EITHER_platform(base: Path, expected: str) -> None:
    """The Linux-only defect that ubuntu CI caught and every Windows run structurally could not.

    The value was composed with a literal ``\\``, so on Linux it read
    ``/tmp/.../out/Book\\data\\`` - ONE path segment with backslashes inside it, naming a directory
    that does not exist. On Windows both separators resolve, so no local run, no Desktop check and
    no artifact-level assertion could see it: the composition has to be exercised against a base of
    the OTHER flavour, which is what this does.

    ``_path_separator`` is asserted directly as well, because it is the whole rule and reading it
    off a composed string would let a half-correct answer pass. The base supplies the FLAVOUR only -
    the value itself is placeholder-rooted since round-2 finding 1 - so the assertion is that none
    of the base's own path survives into it.
    """
    assert pkg._path_separator(str(base)) == ("\\" if isinstance(base, PureWindowsPath) else "/")  # noqa: SLF001
    assert pkg._package_data_folder(base) == expected  # noqa: SLF001
    assert str(base) not in pkg._package_data_folder(base)  # noqa: SLF001
    assert pkg._moved_folder_value(base, "SharedSource.Data", "/some/where/") == (  # noqa: SLF001
        expected + "SharedSource.Data" + ("\\" if isinstance(base, PureWindowsPath) else "/")
    )


def test_a_path_written_in_the_OTHER_flavour_is_still_composed_consistently() -> None:
    """`C:/runs/out` is a Windows path spelled with forward slashes, and the old rule got it wrong.

    "Does the string contain a backslash" was a proxy for flavour, and it fails in both directions:
    a Windows path written with `/` answered `/`, and a POSIX directory whose NAME contains a
    backslash - `/var/tmp/customer\\name/Book`, the reviewer's own case - answered `\\` and produced
    a value that is half one flavour and half the other. Flavour is decided by the ROOT.
    """
    assert pkg._path_separator("C:/runs/out") == "\\"  # noqa: SLF001
    assert pkg._path_separator("/var/tmp/customer\\name/Book") == "/"  # noqa: SLF001
    assert pf.flavour("C:/runs/out") == pf.WINDOWS
    assert pf.flavour("/var/tmp/customer\\name/Book") == pf.POSIX
    assert pf.flavour("relative/path") is None
    assert pf.join("/var/tmp/customer\\name/Book", "data", "Shared.Data", trailing=True) == (
        "/var/tmp/customer\\name/Book/data/Shared.Data/"
    )


def test_a_source_that_cannot_be_shipped_is_a_LOUD_omission_not_a_silent_skip(tmp_path: Path) -> None:
    """Measured on estate run 408: one unit references a `/Users/...` macOS path absent from this
    machine, so "copy it" is not always available and the unshippable case is real rather than
    hypothetical.

    Such a reference is recorded twice over - as a manifest omission carrying its reason, and as a
    package note, which is what `handover.md` renders. Asserting the reason too, because an omission
    with no cause is the silent skip wearing a different name.

    ⚠️ It used to KEEP its original literal, on the argument that the path "still resolves wherever
    it did before". Round-2 finding 1: wherever that was, it was not the customer's machine, and the
    literal carried a user-profile directory into a deliverable while `package_unit.py` exited 0. The
    literal is now neutralized and the run reports exit 4.
    """
    bundle, oracle = _bundle(tmp_path)
    absent = tmp_path / "definitely-not-here" / "Extract_Extract.csv"
    _point_partition_at(bundle, str(absent))
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT

    record = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))
    omissions = record["data_sources"]["omissions"]
    assert [row["file"] for row in omissions] == ["Extract_Extract.csv"]
    assert "not present on the packaging machine" in omissions[0]["reason"]
    assert record["data_sources"]["shipped"] == []
    assert record["data_sources"]["neutralized"] == ["Extract_Extract.csv"]
    assert record["data_sources"]["self_contained"] is False
    assert any("Extract_Extract.csv" in note for note in record["notes"]), record["notes"]
    assert "Extract_Extract.csv" in (root / "handover.md").read_text(encoding="utf-8")

    partitions = "".join(path.read_text(encoding="utf-8") for path in _model_definition(root).rglob("*.tmdl"))
    assert str(absent) not in partitions, "the shipped model still names a directory on this machine"
    assert str(absent.parent) not in partitions
    assert pkg.UNAVAILABLE_TOKEN in partitions
    assert _absolute_literals(root) == []


def test_a_unit_shipping_without_its_rows_is_a_NONZERO_verdict(tmp_path: Path) -> None:
    """Round-2 finding 1: `package_unit_exit=0` while the package could not refresh a partition.

    Exit 0 is the only signal an automated caller reads, so a package that is missing the rows its
    own model names must not earn it. Both directions from one fixture: the same bundle with the
    source PRESENT exits 0, so this cannot pass by refusing everything.
    """
    bundle, oracle = _bundle(tmp_path)
    payload = tmp_path / "extract" / "Extract_Extract.csv"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text("a,b\n1,2\n", encoding="utf-8")
    _point_partition_at(bundle, str(payload))
    argv = ["--bundle", str(bundle), "--out", str(_out(tmp_path)), "--oracle", str(oracle), "--quiet"]
    assert pkg.main(argv) == pkg.EXIT_OK

    payload.unlink()
    assert pkg.main([*argv, "--discard-package-edits"]) == pkg.EXIT_NOT_SELF_CONTAINED


def test_an_oversized_source_is_refused_by_the_ceiling_rather_than_copied(tmp_path: Path) -> None:
    """The ceiling exists so an unbounded case is loud, and it must not fire on a normal one.

    Both directions are asserted from ONE fixture by moving the ceiling, not the data: a test that
    only proves the refusal cannot tell a working ceiling from one set to zero.
    """
    bundle, oracle = _bundle(tmp_path)
    payload = tmp_path / "big.csv"
    payload.write_text("a,b\n1,2\n" * 500, encoding="utf-8")
    _point_partition_at(bundle, str(payload))

    _package(tmp_path, bundle, oracle)
    generous = json.loads(((_out(tmp_path) / UNIT) / "package-manifest.json").read_text(encoding="utf-8"))
    assert generous["data_sources"]["shipped"], "the ceiling refused a source it should have shipped"

    original = pkg.MAX_DATA_BYTES
    try:
        pkg.MAX_DATA_BYTES = 16
        _package(tmp_path, bundle, oracle)
    finally:
        pkg.MAX_DATA_BYTES = original
    refused = json.loads(((_out(tmp_path) / UNIT) / "package-manifest.json").read_text(encoding="utf-8"))
    assert refused["data_sources"]["shipped"] == []
    assert "exceeds the" in refused["data_sources"]["omissions"][0]["reason"]


def test_two_sources_sharing_a_file_name_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """The engine's extract paths are `.../<table>/federated_<hash>/Extract_Extract.csv`.

    Parent folder plus file name is readable but not unique, and two sources landing on one packaged
    file would silently repoint one partition at the other's ROWS - a wrong-numbers defect no
    structural check can see.
    """
    bundle, oracle = _bundle(tmp_path)
    first = tmp_path / "one" / "federated" / "Extract_Extract.csv"
    second = tmp_path / "two" / "federated" / "Extract_Extract.csv"
    for path, body in ((first, "a\n1\n"), (second, "a\n2\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _point_partition_at(bundle, str(first), str(second))

    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    shipped = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))["data_sources"]["shipped"]
    assert len(shipped) == 2, shipped
    bodies = sorted((root / row["path"]).read_text(encoding="utf-8") for row in shipped)
    assert bodies == ["a\n1\n", "a\n2\n"], "one packaged copy overwrote the other"


def _resolves_inside(root: Path, value: str) -> bool:
    """Lexical containment, never `Path.resolve()` - see `package_unit._inside` for the 52-minute UNC hang."""
    return pkg._inside(root, value)  # noqa: SLF001  # pylint: disable=protected-access


def _point_partition_at(bundle: Path, *sources: str, folder: str = "", leaf: str = "", http_path: str = "") -> None:
    """Give the fixture model import partitions shaped as the engine actually emits them.

    Three shapes, because the packager must tell three apart: a bare `File.Contents("<absolute>")`,
    the folder PARAMETER that every datasource-only unit in the estate uses, and a Databricks
    `HttpPath` - a POSIX-absolute literal that is NOT a path at all and must stay silent.
    """
    definition = bundle / "pbip" / UNIT / f"{UNIT}.SemanticModel" / "definition"
    (definition / "tables").mkdir(parents=True, exist_ok=True)
    (definition / "model.tmdl").write_text("model Model\n\tculture: en-US\n\n", encoding="utf-8")
    if http_path:
        (definition / "tables" / "Warehouse.tmdl").write_text(
            "table Warehouse\n"
            "\tpartition 'Warehouse' = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            "\t\t\tlet\n"
            '\t\t\t\tSource = Databricks.Catalogs("adb-1.azuredatabricks.net", '
            f'[HttpPath = "{http_path}", Catalog = null])\n'
            "\t\t\tin\n"
            "\t\t\t\tSource\n",
            encoding="utf-8",
        )
    if folder:
        (definition / "expressions.tmdl").write_text(
            f'expression SourceFolder = "{folder}" meta [IsParameterQuery=true, Type="Text",'
            " IsParameterQueryRequired=true]\n\n",
            encoding="utf-8",
        )
        (definition / "tables" / "Shared.tmdl").write_text(
            "table Shared\n"
            "\tpartition 'Shared' = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            "\t\t\tlet\n"
            f'\t\t\t\tSource = Excel.Workbook(File.Contents(#"SourceFolder" & "\\{leaf}"), null, true)\n'
            "\t\t\tin\n"
            "\t\t\t\tSource\n",
            encoding="utf-8",
        )
    for index, source in enumerate(sources):
        (definition / "tables" / f"Imported{index}.tmdl").write_text(
            f"table Imported{index}\n"
            f"\tpartition 'Imported{index}' = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            "\t\t\tlet\n"
            f'\t\t\t\tSource = Csv.Document(File.Contents("{source}"), [Delimiter=","])\n'
            "\t\t\tin\n"
            "\t\t\t\tSource\n",
            encoding="utf-8",
        )


# --------------------------------------------------------------------------------------------
# 4c. the OTHER shipped manifests (round-2 blocker 2)
#
# Round 1 fixed one denylist. `package_oracle()` and `scope_receipt()` were still denylists in
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
    installed = f"{HOST_PATH}{_SEP}{FOREIGN}"
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
                    "root": installed,
                    "plugin_root": installed,
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
    assert absolute_host_paths(scoped) == []
    assert set(scoped["scope"]["dropped_fields"]) >= {"engine.root", "engine.plugin_root", "report_sha256"}


def test_no_shipped_manifest_carries_an_absolute_host_path(tmp_path: Path) -> None:
    """One assertion over the WHOLE package - the round-1 claim was scoped to report.json alone.

    Each manifest is PARSED and walked rather than grepped: the serialized form doubles every
    separator, so a text search for the single-separator form silently never matches.
    """
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest.update(ESTATE_ORACLE_EXTRA)
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (bundle / "report.json").write_text(json.dumps(_estate_report(UNIT)), encoding="utf-8")
    (bundle / "engine-output-receipt.json").write_text(
        json.dumps({"version": 1, "engine": {"version": "2.339.0", "root": f"{HOST_PATH_ROOT}{_SEP}engine"}}),
        encoding="utf-8",
    )
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    offenders = [
        f"{path.relative_to(root)}{leak}"
        for path in root.rglob("*.json")
        for leak in absolute_host_paths(json.loads(path.read_text(encoding="utf-8")))
    ]
    assert offenders == [], f"absolute host paths shipped in: {offenders}"


def test_the_absolute_path_walker_can_actually_find_one() -> None:
    """The positive control the vacuous text search never had.

    `assert "C:\\\\Users" not in json.dumps(...)` was the earlier form: `json.dumps` escapes the
    separator, so the needle could never appear and the assertion passed regardless. A detector that
    cannot fire is worse than none, so it is fired here on purpose - nested, and in a list.
    """
    assert absolute_host_paths({"engine": {"root": HOST_PATH}}) == [".engine.root"]
    assert absolute_host_paths({"a": [{"b": f"{HOST_PATH_ROOT}{_SEP}x"}]}) == [".a[0].b"]
    assert absolute_host_paths(json.loads(json.dumps({"root": HOST_PATH}))) == [".root"]
    assert absolute_host_paths({"engine": {"version": "2.339.0"}}) == []
    assert absolute_host_paths({"relative": f"pbip{_SEP}Unit{_SEP}Unit.pbip"}) == []


# --------------------------------------------------------------------------------------------
# 4e. the oracle manifest is UNTRUSTED INPUT (round-3 finding 3)
#
# `oracle-manifest.json` is written by a separate tool against a live Tableau server, and
# `_copy_leg` used to join its declared path straight onto the capture root. Round-3 review copied
# an arbitrary file into the customer package with `"../outside-secret.png"`, and an absolute path
# both copied the file AND wrote the host path into the packaged manifest.
#
# This is deliberately NOT an allowlist problem and is not fixed with one: it is containment.
# --------------------------------------------------------------------------------------------


def _capture_with_leg_path(tmp_path: Path, declared: str) -> tuple[Path, Path, Path]:
    """`(bundle, oracle, package root)` where this unit's first view declares ``declared``."""
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["image"] = {"status": "ok", "path": declared, "sha256": "x", "bytes": 3}
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    return bundle, oracle, _out(tmp_path) / UNIT


def _secret_outside(oracle: Path, name: str) -> Path:
    target = (oracle / ".." / name).resolve()
    target.write_bytes(b"\x89PNG\r\n\x1a\nEXFILTRATION-CANARY")
    return target


@pytest.mark.parametrize("declared", ["../outside-secret.png", "sub/../../outside-secret.png"])
def test_a_relative_path_escaping_the_capture_root_is_refused(tmp_path: Path, declared: str) -> None:
    """`../` traversal copied an arbitrary file byte-identically into the customer package.

    ⚠️ The end-to-end half is now defended TWICE: round-6 source containment refuses a `..` component
    before `_resolve_capture_file` ever sees it, so the end-to-end assertions alone can no longer
    tell whether the containment check still exists. The resolver is therefore driven DIRECTLY as
    well - it is the sole mitigation for the shape source containment cannot see, a symlink INSIDE
    the capture pointing out of it, and a check nothing can falsify is a check that will be deleted.
    """
    bundle, oracle = _bundle(tmp_path)
    _secret_outside(oracle, "outside-secret.png")
    resolved, reason = pkg._resolve_capture_file(oracle, declared)  # pylint: disable=protected-access
    assert resolved is None and "escapes the capture root" in str(reason)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["image"] = {"status": "ok", "path": declared, "sha256": "x", "bytes": 3}
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    assert not [p for p in root.rglob("*") if p.is_file() and b"EXFILTRATION-CANARY" in p.read_bytes()]
    assert declared not in json.dumps(
        json.loads((root / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    )


def test_an_absolute_path_is_refused_and_never_reaches_the_manifest(tmp_path: Path) -> None:
    """The absolute form copied the file AND wrote the host path into the packaged manifest.

    ⚠️ The `is_absolute()` guard is defence in depth, not the sole mitigation: the mutation campaign
    showed that removing it still leaves the file uncopied, because the containment check refuses the
    resolved path anyway. Keeping it is deliberate - it is a named requirement and it gives a PRECISE
    diagnosis - so its observable contract is asserted here, otherwise the branch is unfalsifiable
    and could be lost in a refactor without anything going red.

    ⚠️ **Round 7 added a THIRD layer over this same input, and it is why the mutation that neuters
    `_declares_non_relative` now SURVIVES against this test.** `tmp_path` on Windows sits under the
    runner's own profile, so `discloses_host_path` refuses the declared string with the identical
    *"non-relative"* diagnosis and nothing here can tell the two layers apart. What only the parse
    half answers is a NON-PROFILE location, and that is asserted separately in
    :func:`test_a_NON_PROFILE_absolute_location_is_refused_by_the_PARSE_half`, which is where the
    mutation is now anchored. This test is unchanged and still true; it is simply no longer the
    discriminator.
    """
    bundle, oracle = _bundle(tmp_path)
    secret = tmp_path / "absolute-secret.png"
    secret.write_bytes(b"\x89PNG\r\n\x1a\nEXFILTRATION-CANARY")
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["image"] = {"status": "ok", "path": str(secret), "sha256": "x", "bytes": 3}
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    shipped = json.loads((root / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert not [p for p in root.rglob("*") if p.is_file() and b"EXFILTRATION-CANARY" in p.read_bytes()]
    assert absolute_host_paths(shipped) == []
    leg = shipped["views"][0]["image"]
    assert leg["status"] == pkg.OMITTED_STATUS
    assert leg["path"] == pkg.REFUSED_PATH, "the declared path must not be echoed back"
    assert "non-relative" in leg["packaging_reason"], "an absolute path must be diagnosed as such"
    assert str(secret) not in json.dumps(shipped)


def test_a_NON_PROFILE_absolute_location_is_refused_by_the_PARSE_half(tmp_path: Path) -> None:
    """What ONLY `_declares_non_relative` answered, isolated so the branch stays falsifiable.

    Round 7 gave the packager a second, CONTAINMENT-shaped question (does this text disclose a path
    under a user PROFILE?), and for the ordinary case both layers now fire on the same string with
    the same *"non-relative"* diagnosis. That is defence in depth and it is wanted - but it made the
    parse half unobservable, and an unfalsifiable guard is the thing this repo's mutation campaign
    exists to refuse.

    A build drive is the discriminator: `<drive>:\\builds\\out\\secret.png` names the operator's
    machine while disclosing no profile, so `host_paths.discloses_host_path` is silent on it and the
    parse is the only thing that can refuse it. Neuter it and the leg gets a different diagnosis on
    both hosts - *"escapes the capture root"* on Windows, *"does not resolve to a file"* on Linux,
    because `PureWindowsPath`'s drive is what makes the two agree here in the first place.

    ⚠️ **Round 9 MASKED this test the same way round 7 masked its predecessor, and it is kept anyway.**
    `host_paths.discloses_host_location` now refuses every rooted location, a build drive included,
    so this string is refused by two layers again and the mutation SURVIVES against it. The claim is
    still true and still worth pinning - a build drive must not ship - so the test stays and the
    MUTATION moves, to :func:`test_a_DRIVE_RELATIVE_path_is_refused_by_the_PARSE_half_alone`. Deleting
    it would trade a true assertion for nothing; leaving the mutation here would report a green
    campaign for a branch nothing exercises.
    """
    bundle, oracle = _bundle(tmp_path)
    declared = f"D:{_SEP}builds{_SEP}out{_SEP}secret.png"
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["image"] = {"status": "ok", "path": declared, "sha256": "x", "bytes": 3}
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)

    shipped = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    leg = shipped["views"][0]["image"]
    assert leg["status"] == pkg.OMITTED_STATUS
    assert leg["path"] == pkg.REFUSED_PATH, "the declared location must not be echoed back"
    assert "non-relative" in leg["packaging_reason"]
    assert declared not in json.dumps(shipped)


def test_a_DRIVE_RELATIVE_path_is_refused_by_the_PARSE_half_alone(tmp_path: Path) -> None:
    """Round 9's re-isolation of the parse branch, and the pattern behind having to do it twice.

    Each time a wider containment layer lands above `_declares_non_relative`, the proof that the
    parse still runs has to be re-isolated or the branch quietly becomes unfalsifiable. Round 7
    masked the profile anchor; round 8 re-isolated it on a build drive; round 9's predicate covers
    every ROOTED location, build drives included, so a build drive isolates nothing any more.

    What is left to only this branch is a **drive-relative** path - `<drive>:secret.png`, a drive with
    no root separator, which Windows resolves against that drive's current directory. It is a real
    escape route for `_resolve_capture_file` and the containment predicate is silent on it BY
    CONSTRUCTION: nothing about it is rooted, so no grammar of absolute locations can see it.
    Measured on the round-9 tip::

        <drive>:secret.png -> _declares_non_relative=True  discloses_host_location=False

    which is exactly the shape an anchor needs: one branch answers, the other cannot.
    """
    declared = "C:secret.png"
    assert pkg._declares_non_relative(declared) is True  # pylint: disable=protected-access
    assert hp.discloses_host_location(declared) is False, (
        "the containment half must be silent, or this isolates nothing"
    )

    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["image"] = {"status": "ok", "path": declared, "sha256": "x", "bytes": 3}
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)

    shipped = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    leg = shipped["views"][0]["image"]
    assert leg["status"] == pkg.OMITTED_STATUS
    assert leg["path"] == pkg.REFUSED_PATH, "the declared location must not be echoed back"
    assert "non-relative" in leg["packaging_reason"], "only the PARSE half can diagnose this one"
    assert declared not in json.dumps(shipped)


def test_a_symlink_out_of_the_capture_root_is_refused(tmp_path: Path) -> None:
    """Containment is checked on the RESOLVED path, so a link inside the capture cannot escape it."""
    bundle, oracle = _bundle(tmp_path)
    secret = _secret_outside(oracle, "linked-secret.png")
    link = oracle / "images" / "innocent.png"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("this platform/account cannot create symlinks without elevation")
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["image"] = {"status": "ok", "path": "images/innocent.png", "sha256": "x", "bytes": 3}
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    assert not [p for p in root.rglob("*") if p.is_file() and b"EXFILTRATION-CANARY" in p.read_bytes()]


def test_a_LEGACY_data_leg_cannot_ship_a_host_path_under_retained_path(tmp_path: Path) -> None:
    """#480 round 4, finding 1. The containment guard was keyed to a FIELD NAME, not to the manifest.

    `withhold_uncertified_evidence` demotes an uncertified data leg's `path` to `retained_path`
    before the packager looks at it -- correctly, because those bytes are not evidence -- and
    `_copy_leg` then keyed on `path`, found none, and returned the leg verbatim. The review's
    controlled differential drove ONE absolute path through both shapes:

        certified_data.path=<refused-by-packager>
        legacy_data.retained_path=<drive>:\\Users\\<account>\\private\\oracle.csv

    A customer package carrying an absolute host path is #461's class -- it names their server,
    project and operator, and this repo is public.
    """
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    # The REAL legacy shape: a row count and no certification, so it is withheld and renamed.
    manifest["views"][0]["data"] = {
        "status": "ok",
        "path": f"{HOST_PATH_ROOT}{_SEP}private{_SEP}oracle.csv",
        "row_count": 900,
    }
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)

    shipped = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    data = [v for v in shipped["views"] if v.get("flags")][0]["data"]
    assert absolute_host_paths(shipped) == [], "no packaged field may carry an absolute host path"
    assert data["retained_path"] == pkg.REFUSED_PATH, "the demoted field must meet the SAME check as `path`"
    assert "path" not in data, "demotion still holds: uncertified bytes are never named as evidence"


def test_the_containment_guard_is_field_AGNOSTIC_not_a_second_allowlist(tmp_path: Path) -> None:
    """The class-closing half: a THIRD field name must not reopen what round 4 found.

    `retained_path` was the second name; fixing it by name would leave the next one open. This
    drives an absolute path through `packaged_from` on a leg the packager does not copy -- a field
    it normally writes itself, and therefore one nobody would think to guard.
    """
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["image"] = {
        "status": "failed",
        "packaged_from": f"{HOST_PATH_ROOT}{_SEP}private{_SEP}leak.png",
    }
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)

    shipped = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert absolute_host_paths(shipped) == []
    assert shipped["views"][0]["image"]["packaged_from"] == pkg.REFUSED_PATH


# --------------------------------------------------------------------------------------------
# #480 round 5: the guard is a property of the DOCUMENT, at every depth
# --------------------------------------------------------------------------------------------


def _shipped_with(tmp_path: Path, *, view: dict) -> tuple[dict, Path]:
    """Package one bundle whose first oracle view is overwritten with ``view``."""
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0].update(view)
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    return json.loads((root / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8")), root


def test_a_string_INSIDE_a_retained_container_cannot_ship_a_host_path(tmp_path: Path) -> None:
    """#480 round 5, the blocking finding. Round 4 walked `leg.items()`, so a LIST was never opened.

    Review's controlled experiment, reproduced verbatim against the tip before this fix::

        shipped_image={'status': 'failed', 'retry_reasons': ['<drive>:\\\\Users\\\\...\\\\retry.log']}
        absolute_leaks=['.views[0].image.retry_reasons[0]']

    `retry_reasons` carries server-response diagnostics and is allowlisted as `SCALAR_LIST`, so it is
    exactly the text most likely to contain a real customer path -- and this repo is public.
    `dimensions_px` is asserted beside it because fixing `retry_reasons` BY NAME is the fourth escape
    this test exists to refuse: it is a different allowlisted list, guarded by the same walk.
    """
    leak = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}retry.log"
    shipped, _ = _shipped_with(
        tmp_path,
        view={
            "image": {"status": "failed", "retry_reasons": [leak]},
            "svg": {"status": "failed", "dimensions_px": [leak]},
        },
    )
    assert absolute_host_paths(shipped) == [], "no string at any depth may carry an absolute host path"
    assert shipped["views"][0]["image"]["retry_reasons"] == [pkg.REFUSED_PATH]
    assert shipped["views"][0]["svg"]["dimensions_px"] == [pkg.REFUSED_PATH]
    assert leak not in json.dumps(shipped)


def test_a_VIEW_LEVEL_string_cannot_ship_a_host_path_either(tmp_path: Path) -> None:
    """The half the review did not reach: `_copy_leg` guards four legs, and the view is not one.

    Measured on the tip with the leg walk made recursive but still called only from `_copy_leg`::

        E view-level flags[] (allowlisted SCALAR_LIST): LEAK leaks=['.views[0].flags[0]']
        F view_name (scalar, view level):               LEAK leaks=['.views[0].view_name']

    `flags` was allowlisted for #471 and `view_name` has shipped since round 1, so "guard the legs"
    was never the boundary -- the boundary is the packaged DOCUMENT. The refusal is also RECORDED,
    because a scrub nobody can see is indistinguishable from a capture that never said anything.
    """
    leak = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}leak.log"
    shipped, _ = _shipped_with(tmp_path, view={"flags": [leak], "view_name": leak})
    assert absolute_host_paths(shipped) == []
    assert shipped["views"][0]["flags"] == [pkg.REFUSED_PATH]
    assert shipped["views"][0]["view_name"] == pkg.REFUSED_PATH
    assert sorted(shipped["scope"]["refused_fields"]) == ["views[0].flags[0]", "views[0].view_name"]


def test_a_refused_object_NAME_does_not_leak_into_the_other_two_artifacts(tmp_path: Path) -> None:
    """The name leaves the manifest: measured, it reached `handover.md` AND `package-manifest.json`.

    `object_filename` replaces separators, so an absolute `view_name` became the packaged stem
    `C_Users_<account>_private_leak.log` -- still the account, just punctuated differently. Containing
    the naming pair ONCE means the stem, the greppable `object=` field and the `objects[].name` row
    all derive from the contained value instead of each re-deciding.
    """
    leak = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}leak.log"
    _, root = _shipped_with(tmp_path, view={"view_name": leak})
    account = HOST_PATH_ROOT.rsplit(_SEP, 1)[-1]
    leaked = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and account in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == [], f"the account name must not reach any packaged artifact: {leaked}"
    assert not [path for path in root.rglob("*") if account in path.name]


def test_the_ORACLE_RESULT_itself_is_contained_not_only_the_scoped_manifest(tmp_path: Path) -> None:
    """#480 round 6. The document sweep guards ONE artifact; `oracle.objects` is built beside it.

    Rounds 3, 4 and 5 each contained a value at the consumer that had just leaked, and each was
    followed by a consumer that had not been enumerated. Round 5 swept the scoped
    `oracle-manifest.json`, and `package_oracle` went on constructing `objects`/`omissions` from the
    RAW view - which `package-manifest.json` embeds verbatim and `render_handover` interpolates.
    Review's controlled input and its measured result on the tip::

        view_luid = <drive>:\\Users\\<account>\\private\\view-id.log
        view_type = \\\\server\\share\\declared-type.log

        oracle-manifest view_luid:                  <refused-by-packager>   # swept
        package-manifest oracle.objects[0].view_luid: <drive>:\\Users\\...   # NOT swept
        handover.md:  UNTYPED_RENDER ... luid=<drive>:\\Users\\...           # NOT swept

    So this asserts the SOURCE, not a fifth sweep: the view is contained once, before any field is
    read, and all four shipped surfaces are checked together because that is the class - a future
    consumer must inherit containment rather than need its own guard.
    """
    luid_leak = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}view-id.log"
    type_leak = f"{_SEP}{_SEP}server{_SEP}share{_SEP}declared-type.log"
    shipped, root = _shipped_with(tmp_path, view={"view_luid": luid_leak, "view_type": type_leak})

    package = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))
    obj = package["oracle"]["objects"][0]
    assert obj["view_luid"] == pkg.REFUSED_PATH, "objects[] must be built from the CONTAINED view"
    assert obj["declared_view_type"] == pkg.REFUSED_PATH
    assert obj["view_type"] == pkg.KIND_UNKNOWN, "a refused declared type is still untyped, not invented"
    assert absolute_host_paths(package) == [], "package-manifest.json is a shipped artifact too"
    assert shipped["views"][0]["view_luid"] == pkg.REFUSED_PATH

    handover = (root / "handover.md").read_text(encoding="utf-8")
    assert "UNTYPED_RENDER" in handover
    account = HOST_PATH_ROOT.rsplit(_SEP, 1)[-1]
    leaked = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and account in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == [], f"no shipped artifact may carry the account name: {leaked}"
    assert not [path for path in root.rglob("*") if account in path.name], "nor may a filename stem"


def test_a_refused_view_LUID_does_not_reach_an_OMISSION_row(tmp_path: Path) -> None:
    """`omissions[]` is the second structure built beside the sweep, and it carries the LUID too.

    It reaches `package-manifest.json` and the `ORACLE_OMISSION` line, neither of which the scoped
    manifest's sweep sees. Driven with a leg the packager refuses, so an omission row actually
    exists to inspect.
    """
    luid_leak = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}view-id.log"
    _, root = _shipped_with(
        tmp_path,
        view={"view_luid": luid_leak, "image": {"status": "ok", "path": f"{HOST_PATH_ROOT}{_SEP}secret.png"}},
    )
    package = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))
    omissions = package["oracle"]["omissions"]
    assert omissions and omissions[0]["view_luid"] == pkg.REFUSED_PATH
    assert absolute_host_paths(package) == []
    assert "non-relative" in omissions[0]["reason"], "the leg's own diagnosis must survive containment"
    assert luid_leak not in (root / "handover.md").read_text(encoding="utf-8")


def test_an_ORDINARY_capture_is_byte_identical_after_containment(tmp_path: Path) -> None:
    """Positive control for the source containment: a real LUID and a real type must not be mangled.

    A real `view_luid` is a UUID and a real `view_type` is `dashboard`/`worksheet`/`unknown`.
    Containing the whole view at the source touches every field, so the cost of getting the
    predicate wrong is now the whole capture rather than one leg - assert the no-op explicitly.
    """
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    shipped = json.loads((root / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    package = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))
    by_luid = {view["view_luid"]: view for view in shipped["views"]}
    assert sorted(by_luid) == [
        "aaaaaaaa-0000-0000-0000-000000000001",
        "aaaaaaaa-0000-0000-0000-000000000002",
    ], "every LUID survives verbatim"
    assert by_luid["aaaaaaaa-0000-0000-0000-000000000002"]["view_type"] == "dashboard"
    assert by_luid["aaaaaaaa-0000-0000-0000-000000000001"]["view_name"] == "Sales"
    assert "refused_fields" not in shipped["scope"], "an ordinary capture refuses nothing"
    assert [obj["view_luid"] for obj in package["oracle"]["objects"]] == sorted(by_luid)
    assert {obj["view_type"] for obj in package["oracle"]["objects"]} == {"worksheet", "dashboard"}
    assert package["oracle"]["omissions"] == []
    assert "unknown/images" not in _images(tmp_path)


def test_the_WORKBOOK_IDENTITY_document_is_contained_at_its_own_intake_too(tmp_path: Path) -> None:
    """The consumer enumeration's second finding: `source-provenance.json` is untrusted input too.

    `origin.workbook_luid` is server-supplied and three readers take it before anything sweeps it -
    `shippable_provenance` ships it, `render_handover` interpolates it into `ORACLE_ATTRIBUTION
    luid=`, and `workbook_identity`'s conflict diagnosis quotes it INTO A SENTENCE. Measured before
    the intake containment::

        scope.suppressed_reason: ... source-provenance.json records <drive>:\\Users\\<account>\\...
        handover.md: ORACLE_ATTRIBUTION ... reason=... records <drive>:\\Users\\<account>\\...

    ⚠️ `absolute_host_paths` is structurally blind to that second form - the leak is a substring of
    an authored sentence, not a string VALUE - so this asserts on the account name in the shipped
    bytes as well. A walk of the parse is the right tool for a field; it is the wrong tool for prose.
    """
    leak = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}leak.log"
    bundle, oracle = _bundle(tmp_path, provenance_luid=leak)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    provenance = json.loads((root / "source-provenance.json").read_text(encoding="utf-8"))
    assert pkg.REFUSED_PATH in provenance["scope"]["suppressed_reason"], "the diagnosis quotes the CONTAINED value"
    assert absolute_host_paths(provenance) == []
    account = HOST_PATH_ROOT.rsplit(_SEP, 1)[-1]
    leaked = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and account in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == [], f"no shipped artifact may carry the account name: {leaked}"


def test_a_LEGITIMATE_provenance_entry_reaches_the_identity_unchanged(tmp_path: Path) -> None:
    """Positive control for the provenance intake: a real LUID, sha and filename must not be mangled."""
    bundle, oracle = _bundle(tmp_path)
    result = _package(tmp_path, bundle, oracle)
    identity = result["workbook_identity"]
    assert identity["luid"] == WB_LUID and identity["match"] == "sha256" and identity["reason"] is None
    provenance = json.loads((_out(tmp_path) / UNIT / "source-provenance.json").read_text(encoding="utf-8"))
    entry = provenance["inputs"][0]
    assert entry["origin"] == {"workbook_luid": WB_LUID, "match": "sha256"}
    assert entry["input"]["file"] == f"{WB_LUID}_{UNIT}.twb"
    assert len(entry["input"]["sha256"]) == 64


def test_a_refused_nested_string_is_DIAGNOSED_not_silently_scrubbed(tmp_path: Path) -> None:
    """The leg guard's own contract, which the document sweep cannot supply: WHY, and WHERE.

    A scrub with no diagnosis leaves the operator with a `<refused-by-packager>` and no way to tell a
    hostile manifest from a capture that recorded nothing. The reason names the JSON path -- never
    the value -- so the line is actionable without re-leaking what was refused.
    """
    leak = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}retry.log"
    _shipped_with(tmp_path, view={"image": {"status": "failed", "retry_reasons": [leak]}})
    omissions = [line for line in _lines(tmp_path) if line.startswith("ORACLE_OMISSION")]
    assert len(omissions) == 1, omissions
    assert "'retry_reasons[0]'" in omissions[0], "the refusal must name the JSON path it refused"
    assert "non-relative" in omissions[0]
    assert leak not in omissions[0], "a diagnosis must not re-leak the value it refused"


def test_the_containment_walk_reaches_EVERY_depth_and_container() -> None:
    """The class, not the instance: a string is refused wherever it sits, in any nesting.

    Driven directly because the current allowlist has no retained two-level container to drive it
    through -- which is precisely why this is asserted now rather than after a future
    `Rows({"notes": SCALAR_LIST})` reopens the hole. Tuples are covered because the walk must not
    silently convert one to a list either.
    """
    leak = f"{HOST_PATH_ROOT}{_SEP}x"
    payload = {
        "flat": leak,
        "in_list": [leak],
        "list_in_dict_in_list": [{"notes": [leak]}],
        "dict_in_list": [{"src": leak}],
        "in_tuple": (leak, "keep/me"),
        "deep": {"a": [{"b": [[leak]]}]},
        "count": 7,
        "safe": "data/view-0.csv",
    }
    contained, refused = pkg._contain_unsafe_strings(payload)  # pylint: disable=protected-access
    assert leak not in json.dumps(contained, default=list)
    assert contained["count"] == 7 and contained["safe"] == "data/view-0.csv"
    assert isinstance(contained["in_tuple"], tuple) and contained["in_tuple"][1] == "keep/me"
    assert sorted(refused) == [
        "deep.a[0].b[0][0]",
        "dict_in_list[0].src",
        "flat",
        "in_list[0]",
        "in_tuple[0]",
        "list_in_dict_in_list[0].notes[0]",
    ]


def test_a_LEGITIMATE_string_inside_a_retained_container_survives_unmodified(tmp_path: Path) -> None:
    """Positive control. An over-correction that scrubs diagnostics destroys the operator's evidence.

    Three shapes that must all pass through byte-identically: a relative capture path, prose that
    merely MENTIONS something path-like (`/api/views`, `../logs`) without declaring a location, and
    a non-string scalar list. A capture whose only sin is being informative must stay informative.
    """
    prose = [
        "HTTP 503 from GET /api/views/x after 2 retries",
        "renderer wrote nothing (see ../logs on the server)",
        "images/view-0.png was 0 bytes",
    ]
    shipped, root = _shipped_with(
        tmp_path,
        view={
            "image": {"status": "failed", "retry_reasons": list(prose), "dimensions_px": [1300, 1600]},
            "flags": ["data_empty"],
        },
    )
    leg = shipped["views"][0]["image"]
    assert leg["retry_reasons"] == prose, "a diagnostic that is not a declared path must not be mangled"
    assert leg["dimensions_px"] == [1300, 1600]
    assert shipped["views"][0]["flags"] == ["data_empty"]
    assert "refused_fields" not in shipped["scope"], "nothing was refused, so nothing may be recorded"
    assert not [line for line in _lines(tmp_path) if line.startswith("ORACLE_OMISSION") and "retry_reasons" in line]
    assert root.is_dir()


# --------------------------------------------------------------------------------------------
# #480 round 7: a host path WRAPPED IN PROSE, and an untrusted dictionary KEY
# --------------------------------------------------------------------------------------------


def test_a_host_path_WRAPPED_IN_PROSE_is_refused_not_only_a_bare_one(tmp_path: Path) -> None:
    """#480 round-7 finding B1. The predicate PARSED the string, so any prefix hid the answer.

    `classify_export_error` prepends the HTTP status to a server diagnostic and persists the result
    in `retry_reasons[]` (`capture_tableau_oracle.py:278,597`), so the field most likely to carry a
    real customer path is also the field where it arrives wrapped in a sentence. Review's measured
    reproduction against the round-7 tip::

        raw:               <profile>\\private\\retry.log could not be opened
        classified detail: HTTP 503: <profile>\\private\\retry.log could not be opened
        package predicate: False
        shipped artifact:  oracle/oracle-manifest.json

    Four spellings of ONE path, which is the point: a shipped artifact's question is CONTAINMENT
    ("does this text disclose a host path?"), not shape ("is this string a path?"). The account name
    is asserted against the shipped BYTES too, because a walk of the parse is structurally blind to a
    disclosure that sits inside authored prose rather than being a string VALUE.
    """
    leak = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}retry.log"
    account = HOST_PATH_ROOT.rsplit(_SEP, 1)[-1]
    wrapped = [
        f"HTTP 503: {leak} could not be opened",
        f'"{leak}"',
        "file:///" + HOST_PATH_ROOT.replace(_SEP, "/") + "/private/retry.log",
        f"exported nothing; see {leak}",
    ]
    shipped, root = _shipped_with(tmp_path, view={"image": {"status": "failed", "retry_reasons": list(wrapped)}})
    assert shipped["views"][0]["image"]["retry_reasons"] == [pkg.REFUSED_PATH] * len(wrapped)
    assert absolute_host_paths(shipped) == []
    leaked = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and account in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == [], f"no shipped artifact may carry the account name: {leaked}"


def test_a_host_path_WRAPPED_IN_PROSE_is_redacted_in_the_HANDOVER_slice_too(tmp_path: Path) -> None:
    """The same sub-class on the second value-shaped guard, which had the same anchored predicate.

    `redact_host_paths` asked `HOST_PATH_RE.match` - whether the value STARTS as a location - while
    its own docstring claimed it was "the same shape `set_data_folder.py` gates the repo on". It was
    not, and that drift is finding B1 in a second place: fixing only the packager would have left the
    identical escape in the artifact that ships WHOLE.

    ⚠️ **Round 9 DELETED the anchored half rather than keeping it beside the containment one.** Both
    were spelling tests and the union of two spelling tests still missed `HTTP 503: ` + a non-profile
    absolute. `host_paths.discloses_host_location` is a strict superset of the anchored predicate, so
    the `non_profile_root` row below asserts the same verdict for a better reason: it is refused
    because it is ROOTED, not because it happens to be at the start of the string. Moving it into
    prose - which no anchored test could ever have caught - is what pins that.
    """
    bundle, oracle = _bundle(tmp_path)
    path = bundle / "handover" / f"{UNIT}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["workbook"]["wrapped_note"] = f"HTTP 503: {HOST_PATH_ROOT}{_SEP}private{_SEP}x could not be opened"
    payload["workbook"]["non_profile_root"] = f"D:{_SEP}builds{_SEP}out"
    payload["workbook"]["non_profile_in_prose"] = f"the build wrote to D:{_SEP}builds{_SEP}out and stopped"
    path.write_text(json.dumps(payload), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    shipped = json.loads((_out(tmp_path) / UNIT / "handover" / f"{UNIT}.json").read_text(encoding="utf-8"))
    assert shipped["workbook"]["wrapped_note"] == ms.REDACTED, "a host path inside prose must still be redacted"
    assert shipped["workbook"]["non_profile_root"] == ms.REDACTED, "a non-profile location must still be redacted"
    assert shipped["workbook"]["non_profile_in_prose"] == ms.REDACTED, "and it must not be rescued by a prefix"
    account = HOST_PATH_ROOT.rsplit(_SEP, 1)[-1]
    assert account not in json.dumps(shipped)


def test_an_untrusted_handover_KEY_is_redacted_like_a_value(tmp_path: Path) -> None:
    """#480 round-9 leak 2. `redact_host_paths` cleaned VALUES and preserved raw KEYS.

    Round 8 declined this, citing *"engine-authored keys, no reproduction"*. A reproduction now
    exists, and it is built with the same hypothetical-future-field method this slice's own
    value-redaction test (`test_an_absolute_path_anywhere_in_the_handover_slice_is_redacted`) already
    uses - so declining it required the value half and the key half of ONE artifact to be held to two
    different standards of evidence. Measured on the round-8 tip::

        HANDOVER_TOP_KEY_PRESENT=True
        HANDOVER_NESTED_KEY_PRESENT=True
        HANDOVER_RAW_TEXT_PRESENT=True    -> customer-shipped handover/Book.json carries the path

    "Engine-authored" describes a key's ORIGIN; it does not enforce the shipping invariant. The
    packager's own manifest walk had already concluded exactly this one artifact over, in round 7.

    The collision row is not decoration: two distinct unsafe keys must not both redact onto one
    sentinel and let `dict` keep the last, which would turn a redaction into silent data loss inside
    the agent's actual work queue.
    """
    leak_a = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}secret"
    leak_b = f"D:{_SEP}builds{_SEP}out"
    account = HOST_PATH_ROOT.rsplit(_SEP, 1)[-1]
    bundle, oracle = _bundle(tmp_path)
    path = bundle / "handover" / f"{UNIT}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["workbook"][leak_a] = "safe scalar a"
    payload["workbook"][leak_b] = "safe scalar b"
    payload["workbook"]["nested"] = {"deep": [{leak_a: "safe scalar c"}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = _package(tmp_path, bundle, oracle)

    shipped = json.loads((_out(tmp_path) / UNIT / "handover" / f"{UNIT}.json").read_text(encoding="utf-8"))
    rendered = json.dumps(shipped)
    assert leak_a not in rendered and leak_b not in rendered and account not in rendered
    assert ms.REDACTED in shipped["workbook"] and f"{ms.REDACTED}#2" in shipped["workbook"]
    assert sorted([shipped["workbook"][ms.REDACTED], shipped["workbook"][f"{ms.REDACTED}#2"]]) == [
        "safe scalar a",
        "safe scalar b",
    ], "a collision must lose neither field's value"
    assert shipped["workbook"]["nested"]["deep"][0] == {ms.REDACTED: "safe scalar c"}
    assert any("redacted" in note for note in result["notes"]), "a redaction must be reported, not silent"

    leaked = [
        item.relative_to(_out(tmp_path) / UNIT).as_posix()
        for item in (_out(tmp_path) / UNIT).rglob("*")
        if item.is_file() and account in item.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == [], f"an untrusted handover KEY must not reach any packaged artifact: {leaked}"


def test_the_handover_KEY_walk_reports_the_REDACTED_key_and_stays_idempotent() -> None:
    """Driven directly, because two properties of key redaction have no artifact that shows them.

    Copied from `package_unit._contain_unsafe_key`, which solved this for the oracle manifest one
    round earlier, and from `tableau_env.scrub_tree` before that: the reported path must be built
    from the REDACTED key or the record of the catch re-emits what was caught, and a second pass over
    an already-redacted document must not read the sentinel as a fresh, safe key.
    """
    leak_a = f"{HOST_PATH_ROOT}{_SEP}a"
    leak_b = f"{HOST_PATH_ROOT}{_SEP}b"
    payload = {leak_a: "kept-a", leak_b: "kept-b", "rows": [{leak_a: "kept-nested"}], "count": 7}
    cleaned, redacted = ms.redact_host_paths(payload, prefix="handover/Book.json")

    assert sorted(cleaned) == sorted([ms.REDACTED, f"{ms.REDACTED}#2", "rows", "count"])
    assert sorted(value for value in cleaned.values() if isinstance(value, str)) == ["kept-a", "kept-b"]
    assert cleaned["rows"][0] == {ms.REDACTED: "kept-nested"} and cleaned["count"] == 7
    assert sorted(redacted) == [
        f"handover/Book.json.{ms.REDACTED} (key)",
        f"handover/Book.json.{ms.REDACTED}#2 (key)",
        f"handover/Book.json.rows[0].{ms.REDACTED} (key)",
    ]
    assert HOST_PATH_ROOT not in json.dumps(cleaned) + json.dumps(redacted)

    again, redacted_again = ms.redact_host_paths(cleaned, prefix="handover/Book.json")
    assert again == cleaned and sorted(redacted_again) == sorted(redacted), "the walk must be idempotent"


def test_ORDINARY_PROSE_that_merely_MENTIONS_a_path_is_NOT_refused(tmp_path: Path) -> None:
    """Positive control for the broader predicate - and the main risk the fix introduces.

    A CONTAINS test is far likelier to false-positive than an IS test, and over-refusal destroys the
    operator's evidence exactly as under-refusal leaks it. Every shape here mentions something
    path-like while disclosing no host location: a REST route, a relative directory, a `..` inside a
    sentence, `Users` as an English word, an ordinary view name, and the repo's own documented
    `<placeholder>` spelling - which `set_data_folder.py --check` exempts on purpose, so the packager
    must exempt it identically or the two definitions have re-diverged.

    ⚠️ **Round 9 REVERSES one entry of this control, deliberately, and it is the whole point of the
    round.** Round 8 asserted here that `the build wrote to <drive>:\\builds and then stopped` must
    ship, on the reading that a non-profile location in prose is ordinary text. It is not: this PR's
    own parse anchor states that a build drive names the OPERATOR'S MACHINE and must be refused, and
    a location cannot become acceptable merely because a status prefix precedes it. That entry has
    moved to :func:`test_a_NON_PROFILE_absolute_WRAPPED_IN_PROSE_is_refused_in_EVERY_SPELLING`, which
    asserts the opposite verdict on the same string.

    The four entries after the blank line are round 9's own negative controls, one per way the wider
    grammar could over-fire: a URL whose PATH looks POSIX-absolute, a dotted version string, a drive
    letter in prose with no path after it, and a colon-bearing timestamp.
    """
    prose = [
        "HTTP 503 from GET /api/2.4/sites/abc/views/def after 2 retries",
        "renderer wrote nothing (see ../logs on the server)",
        "images/view-0.png was 0 bytes",
        "Users of this dashboard see a blank page",
        f"set DataFolder to C:{_SEP}Users{_SEP}<account>{_SEP}data as SECURITY.md documents",
        "GET https://tableau.example.com/var/lib/x returned 500",
        "engine 2.339.0, powerbi-report-author 0.1.4, node v20.11.1",
        "drive D: is full; free 0 bytes",
        "captured at 2026-09-04T16:30:53+02:00 (12:04:07 elapsed)",
    ]
    shipped, _ = _shipped_with(
        tmp_path,
        view={"image": {"status": "failed", "retry_reasons": list(prose)}, "view_name": "Sales by Users"},
    )
    assert shipped["views"][0]["image"]["retry_reasons"] == prose, "informative prose must stay informative"
    assert shipped["views"][0]["view_name"] == "Sales by Users"
    assert "refused_fields" not in shipped["scope"], "nothing was refused, so nothing may be recorded"


def test_a_NON_PROFILE_absolute_WRAPPED_IN_PROSE_is_refused_in_EVERY_SPELLING(tmp_path: Path) -> None:
    """#480 round-9 leak 1. The containment predicate matched a SPELLING, not the property.

    Rounds 3-8 each widened the guard by one shape, and each time a different spelling escaped -
    because a *profile root* is a way of writing a location, not the property that matters to a
    customer deliverable. Measured on the round-8 tip, one wrapper (`HTTP 503: `, which is how
    `classify_export_error` writes `retry_reasons[]`) over locations that are all absolute::

        <drive>:\\builds\\out\\secret.log                        -> DISCLOSES=False  SHIPPED=True
        \\\\customer-server\\finance-share\\secret.log              -> DISCLOSES=False  SHIPPED=True
        /var/lib/tableau/secret.log                           -> DISCLOSES=False  SHIPPED=True
        \\\\server\\C$\\Users\\<a real account>\\private\\secret.log  -> DISCLOSES=False  SHIPPED=True
        C%3A%5CUsers%5C<a real account>%5Cprivate%5Csecret.log-> DISCLOSES=False  SHIPPED=True

    The last two are the decisive ones and are why the account name is asserted against the shipped
    BYTES: they are a REAL profile path with a REAL account name, re-spelled as an administrative
    share and as percent-encoding. A spelling test lets the same secret through in another alphabet,
    so the predicate now normalises the alphabet away and asks ONE question about every rooted form.
    """
    account = "blind-review-account"
    profile = f"C:{_SEP}Users{_SEP}{account}"
    wrapped = [
        f"HTTP 503: D:{_SEP}builds{_SEP}out{_SEP}secret.log could not be opened",
        f"HTTP 503: {_SEP}{_SEP}customer-server{_SEP}finance-share{_SEP}secret.log could not be opened",
        "HTTP 503: /var/lib/tableau/secret.log could not be opened",
        f"HTTP 503: {_SEP}{_SEP}server{_SEP}C${_SEP}Users{_SEP}{account}{_SEP}private{_SEP}secret.log failed",
        f"HTTP 503: {_SEP}{_SEP}?{_SEP}D:{_SEP}builds{_SEP}out{_SEP}secret.log could not be opened",
        f"HTTP 503: D:{_SEP}{_SEP}builds{_SEP}{_SEP}out{_SEP}secret.log could not be opened",
        f"HTTP 503: C%3A%5CUsers%5C{account}%5Cprivate%5Csecret.log could not be opened",
        "HTTP 503: %2Fvar%2Flib%2Ftableau%2Fsecret.log could not be opened",
        f"the build wrote to D:{_SEP}builds and then stopped",
    ]
    shipped, root = _shipped_with(tmp_path, view={"image": {"status": "failed", "retry_reasons": list(wrapped)}})
    assert shipped["views"][0]["image"]["retry_reasons"] == [pkg.REFUSED_PATH] * len(wrapped)
    assert absolute_host_paths(shipped) == []
    assert profile not in json.dumps(shipped)
    leaked = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and account in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == [], f"no shipped artifact may carry the account name in ANY spelling: {leaked}"


def test_the_SHIPPING_predicate_is_a_STRICT_SUPERSET_of_the_repo_COMMIT_GATE() -> None:
    """The module's stated invariant, asserted rather than asserted-in-prose.

    `host_paths` exists so "a package can never ship what a commit could not" is structurally true.
    Round 9 gives it TWO predicates - a narrow, profile-only one for the repo's own tracked files and
    a wide one for customer artifacts - and that is only safe while the wide one contains the narrow
    one. It is not two independent regexes: `discloses_host_location` unions the narrow predicate in
    rather than reimplementing it, so this asserts a property of the composition, and the
    `<placeholder>` row is the one that would break first if a future edit re-spelled it instead.
    """
    for text in (
        HOST_PATH,
        f"{HOST_PATH_ROOT}{_SEP}private{_SEP}x.log",
        f"HTTP 503: {HOST_PATH_ROOT}{_SEP}x could not be opened",
        f'"{HOST_PATH_ROOT}{_SEP}x"',
        "file:///" + HOST_PATH_ROOT.replace(_SEP, "/") + "/x",
        f"C:{_SEP}Users{_SEP}<account>{_SEP}data",
        "Users of this dashboard see a blank page",
        "/api/2.4/sites/abc/views/def",
    ):
        narrow = hp.discloses_host_path(text)
        assert hp.discloses_host_location(text) or not narrow, f"the shipping predicate narrowed on: {text!r}"


def test_an_untrusted_dictionary_KEY_is_contained_like_a_value(tmp_path: Path) -> None:
    """#480 round-7 finding B2. The walk cleaned VALUES and preserved KEYS.

    `project()`'s whole job is to refuse an unenumerated field - and it then NAMED the refusal using
    that field's own key, so an untrusted key shipped verbatim inside the record of having dropped
    it. Review's injected view field and its measured result on the round-7 tip::

        "<profile>\\private\\secret": "safe scalar"
          -> "scope": {"dropped_fields": ["views[].<profile>\\private\\secret"]}

    The key is contained at the same intake as the values, so `dropped_fields` can only ever name the
    CONTAINED key - and the `views[]` prefix survives, so the operator still learns at which level an
    unnameable field was dropped.
    """
    leak = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}secret"
    account = HOST_PATH_ROOT.rsplit(_SEP, 1)[-1]
    shipped, root = _shipped_with(tmp_path, view={leak: "safe scalar"})
    assert absolute_host_paths(shipped) == []
    assert f"views[].{pkg.REFUSED_PATH}" in shipped["scope"]["dropped_fields"]
    assert leak not in json.dumps(shipped)
    leaked = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and account in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == [], f"an untrusted KEY must not reach any packaged artifact: {leaked}"


def test_NOTHING_is_appended_to_the_oracle_manifest_after_its_last_containment_pass(tmp_path: Path) -> None:
    """The structural half of B2, and the one that makes "contained by construction" true.

    A TOP-LEVEL manifest key never passes through the per-view intake, so it reaches `project()` raw
    and lands in `scope.dropped_fields` - which `stamp_scope` used to append AFTER the document's
    last containment pass. Two keys are driven, because the two guards are complementary rather than
    redundant and this pins which one answers:

    * a **profile** path is redacted by `manifest_scope._safe_path_segment` while it is being built
      into the diagnostic, keeping the `<redacted-absolute-path>` spelling and losing only the key;
    * a **non-profile** location (`D:\\...`) discloses no user profile, so the segment builder is
      silent on it and only the final sweep - which now runs on the STAMPED document - refuses it.

    Neuter either one and this fails, which is exactly the claim: a later stage must not be able to
    append raw text to a shipped structure.
    """
    profile_key = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}secret"
    location_key = f"D:{_SEP}builds{_SEP}out"
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest[profile_key] = "safe scalar"
    manifest[location_key] = "safe scalar"
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)

    shipped = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    dropped = shipped["scope"]["dropped_fields"]
    assert ms.REDACTED in dropped, f"a profile key must be redacted as the diagnostic is built: {dropped}"
    assert pkg.REFUSED_PATH in dropped, f"the final sweep must refuse a non-profile location key: {dropped}"
    assert profile_key not in json.dumps(shipped) and location_key not in json.dumps(shipped)
    assert absolute_host_paths(shipped) == []
    assert any(entry.startswith("scope.dropped_fields[") for entry in shipped["scope"]["refused_fields"]), (
        "the refusal must be RECORDED, and by the path it refused"
    )


def test_the_containment_walk_contains_KEYS_and_reports_the_CONTAINED_key(tmp_path: Path) -> None:
    """Driven directly, because three properties of key containment have no artifact that shows them.

    Copied from `tableau_env.scrub_tree` (:461-478), which already solved this for the credential
    sink and documents why each matters:

    1. a **collision is disambiguated, never silently dropped** - two distinct unsafe keys both
       contain to the sentinel, and `dict` would keep the last, turning containment into data loss;
    2. the **reported path uses the CONTAINED key**, or the guard re-emits what it just caught into
       `scope.refused_fields`;
    3. the walk stays **IDEMPOTENT**, including the `#2` collision spelling - a second pass must not
       read it as a fresh, safe key and stop reporting it, which is what lets containment move to the
       source without a later reader losing what it could previously see.
    """
    leak_a = f"{HOST_PATH_ROOT}{_SEP}a"
    leak_b = f"{HOST_PATH_ROOT}{_SEP}b"
    payload = {leak_a: "kept-a", leak_b: "kept-b", "views": [{leak_a: "kept-nested"}], "count": 7}
    contained, refused = pkg._contain_unsafe_strings(payload)  # pylint: disable=protected-access

    assert sorted(contained) == sorted([pkg.REFUSED_PATH, f"{pkg.REFUSED_PATH}#2", "views", "count"])
    assert sorted(value for value in contained.values() if isinstance(value, str)) == ["kept-a", "kept-b"], (
        "a collision must lose neither field's value"
    )
    assert contained["views"][0] == {pkg.REFUSED_PATH: "kept-nested"} and contained["count"] == 7
    assert sorted(refused) == [
        f"{pkg.REFUSED_PATH} (key)",
        f"{pkg.REFUSED_PATH}#2 (key)",
        f"views[0].{pkg.REFUSED_PATH} (key)",
    ]
    assert HOST_PATH_ROOT not in json.dumps(contained) + json.dumps(refused)

    again, refused_again = pkg._contain_unsafe_strings(contained)  # pylint: disable=protected-access
    assert again == contained and sorted(refused_again) == sorted(refused), "the walk must be idempotent"
    assert tmp_path.is_dir()


def test_a_NON_allowlisted_scalar_list_never_ships_at_all(tmp_path: Path) -> None:
    """The other half of the closure: an unenumerated container is DROPPED before it can be refused.

    Containment and scoping are complementary, and this pins which one answers. A field nobody
    allowlisted cannot ship even a safe value, so a future leaky container has to be added
    deliberately -- and when it is, the walk above already covers it.
    """
    leak = f"{HOST_PATH_ROOT}{_SEP}private{_SEP}leak.log"
    shipped, _ = _shipped_with(tmp_path, view={"image": {"status": "failed", "future_paths": [leak]}})
    assert absolute_host_paths(shipped) == []
    assert "future_paths" not in shipped["views"][0]["image"]
    assert "views[].image.future_paths" in shipped["scope"]["dropped_fields"]


def test_a_LEGITIMATE_relative_retained_path_still_packages_and_says_where_the_bytes_are(
    tmp_path: Path,
) -> None:
    """Positive control for the two above, plus the second half of finding 1.

    A capture-relative `retained_path` must survive -- refusing everything would pass the security
    assertion while destroying the operator's only route back to the retained bytes. It must also
    not read as a file inside the PACKAGE: those bytes are deliberately not copied, so read against
    the package the reference dangles. The leg says which it is.
    """
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["data"] = {"status": "ok", "path": "data/view-0.csv", "row_count": 900}
    (oracle / "data").mkdir(parents=True, exist_ok=True)
    (oracle / "data" / "view-0.csv").write_text("Region\r\nWest\r\n", encoding="utf-8")
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)

    root = _out(tmp_path) / UNIT
    shipped = json.loads((root / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    data = [v for v in shipped["views"] if v.get("flags")][0]["data"]
    assert data["retained_path"] == "data/view-0.csv", "a safe capture-relative pointer must survive"
    assert "capture" in data["packaging_reason"], "the reference must say it names the capture, not this package"
    assert not (root / "oracle" / "data" / "view-0.csv").exists(), "the bytes stay out of the package"
    assert not list((root / "oracle").rglob("*.csv"))


def test_the_per_view_empty_flag_survives_packaging(tmp_path: Path) -> None:
    """#471. A per-view fact must not be dropped silently at the package boundary.

    The estate-wide `data_empty` COUNT is deliberately dropped -- this packager cannot recompute it
    for one unit -- so if the per-view flag were dropped too, a packaged unit would carry a
    zero-row capture with nothing anywhere saying so, and the fix would have moved the failure
    rather than removed it.
    """
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["flags"] = ["data_empty", "empty_cannot_classify"]
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    shipped = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    flagged = [view for view in shipped["views"] if view.get("flags")]
    assert [view["flags"] for view in flagged] == [["data_empty", "empty_cannot_classify"]]


def test_a_packaged_view_from_an_OLDER_capture_is_flagged_by_the_packager_itself(tmp_path: Path) -> None:
    """The legacy half of #480 finding 1, and the reviewer's actual reproduction input.

    A capture written by a current run flags its own views, so carrying the flag is enough for
    those. An older `oracle-manifest.json` predates the flag entirely -- and that record is what the
    review drove through `_scope_oracle_manifest()`, getting `data_ok=1 status=ok row_count absent
    flags absent`, which is what a clean capture looks like. So the per-view rule is DERIVED here
    from the shared predicate, not merely carried; the estate-wide counts stay dropped, because
    those genuinely cannot be reconstructed for one unit.
    """
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    # No `flags` key anywhere: exactly a manifest written before the diagnostic existed.
    manifest["views"][0]["data"] = {"status": "ok", "path": "data/view-0.csv", "columns": ["Region"]}
    (oracle / "data").mkdir(parents=True, exist_ok=True)
    (oracle / "data" / "view-0.csv").write_text("Region\r\n", encoding="utf-8")
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)

    shipped = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    flagged = [v for v in shipped["views"] if v.get("flags")]
    assert [v["flags"] for v in flagged] == [["data_unassessable", "row_count_unrecorded"]]
    # ...and the STRUCTURAL half (#480 round 2): the packager must not copy uncertified bytes into
    # `<kind>/data/<stem>.csv`, where every numeric consumer reads them as evidence.
    assert "path" not in flagged[0]["data"]
    assert flagged[0]["data"]["retained_path"] == "data/view-0.csv"
    assert not list((_out(tmp_path) / UNIT / "oracle").rglob("*.csv"))


def test_the_packager_does_not_flag_a_view_whose_rows_were_measured(tmp_path: Path) -> None:
    """Control: deriving the flag must not mean stamping it on everything that ships."""
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["data"] = {
        "status": "ok",
        # ⚠️ Certified, because this is the control for "a view whose rows were MEASURED" and
        # since #480 round 3 the measurement is the certification, not the number: a `row_count`
        # with no certificate is the shape every pre-#480 capture has, and it is unassessable.
        "certification": "certified",
        "path": "data/view-0.csv",
        "row_count": 7,
        "columns": ["Region"],
    }
    (oracle / "data").mkdir(parents=True, exist_ok=True)
    (oracle / "data" / "view-0.csv").write_text("Region\r\nWest\r\n", encoding="utf-8")
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)

    shipped = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert [v for v in shipped["views"] if v.get("flags")] == []


def test_a_packaged_view_whose_row_count_was_never_recorded_says_so(tmp_path: Path) -> None:
    """#480 finding 1, at the package boundary -- the third consumer the reviewer named.

    Their observation was `data_ok=1 status=ok row_count absent flags absent`: a packaged unit
    shipping a view nothing had measured, with nothing anywhere saying so. The estate-wide counts are
    deliberately dropped here (this packager cannot recompute them for one unit), so the per-view
    flag is the ONLY channel the fact has -- and `certification` is now allowlisted beside it so a
    reader knows WHY, not merely that something is off.

    ⚠️ `data_ok` staying 1 is not the defect and must not be "fixed": the export genuinely succeeded.
    What was missing is everything else on the row.
    """
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    view = manifest["views"][0]
    # The shape `_capture_data` writes when it cannot certify the body: a successful transport, the
    # bytes kept, and NO row count -- which is exactly the record the reviewer drove through here.
    view["data"] = {"status": "ok", "path": "data/view-0.csv", "certification": "content_type_absent"}
    view["flags"] = ["data_unassessable", "content_type_absent"]
    (oracle / "data").mkdir(parents=True, exist_ok=True)
    (oracle / "data" / "view-0.csv").write_text("Region,Sales\r\nWest,10\r\n", encoding="utf-8")
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)

    shipped = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    unassessable = [v for v in shipped["views"] if v.get("flags") == ["data_unassessable", "content_type_absent"]]
    assert len(unassessable) == 1, "the per-view flag is the only channel this fact has after packaging"
    assert unassessable[0]["data"]["certification"] == "content_type_absent"
    assert "row_count" not in unassessable[0]["data"]
    assert unassessable[0]["data"]["status"] == "ok", "the transport succeeded -- keep that distinction"
    # #480 round 2. Flagging it was necessary and not sufficient: the packaged unit must also not
    # OFFER the bytes as numbers. `objects[].data` is what the operator's `ORACLE_*` lines and any
    # numeric consumer read, and it must be empty here.
    assert "path" not in unassessable[0]["data"]
    assert unassessable[0]["data"]["retained_path"] == "data/view-0.csv"
    assert unassessable[0]["data"]["evidence_withheld"]
    assert not list((_out(tmp_path) / UNIT / "oracle").rglob("*.csv"))


def test_allowlisting_the_flag_did_not_open_the_view_to_unknown_fields(tmp_path: Path) -> None:
    """Control for the test above: the allowlist must still be an allowlist.

    Naming one new key is a one-key widening; a fix that reached the same green by carrying whatever
    the capture happened to hold would be the denylist round 2 removed.
    """
    bundle, oracle = _bundle(tmp_path)
    manifest = json.loads((oracle / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["flags"] = ["data_empty"]
    manifest["views"][0]["future_field_nobody_enumerated"] = "estate-wide business"
    (oracle / "oracle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    shipped = json.loads((_out(tmp_path) / UNIT / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    assert any(view.get("flags") for view in shipped["views"]), "the positive half must still hold"
    assert all("future_field_nobody_enumerated" not in view for view in shipped["views"])
    assert "estate-wide business" not in json.dumps(shipped), "the VALUE must not ship"
    # The PATH is reported by design -- `scope.reason` says so -- and reading it back is what proves
    # the field was refused by the allowlist rather than simply absent from the fixture.
    assert "views[].future_field_nobody_enumerated" in shipped["scope"]["dropped_fields"]


def test_a_legitimate_capture_relative_leg_still_copies(tmp_path: Path) -> None:
    """The positive control: containment must not break the ordinary path, or it is a fail-closed bug."""
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    shipped = json.loads((root / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    legs = [view["image"] for view in shipped["views"] if isinstance(view.get("image"), dict)]
    assert legs and all(leg["status"] == "ok" for leg in legs)
    assert all("/" in leg["packaged_from"] or leg["packaged_from"] for leg in legs)
    assert _images(tmp_path)


# --------------------------------------------------------------------------------------------
# 4f. provenance and the handover slice (round-3 finding 2)
# --------------------------------------------------------------------------------------------


def test_a_refused_attribution_ships_no_provenance_entry_at_all(tmp_path: Path) -> None:
    """⚠️ A refusal that does not suppress the artifact is not a refusal.

    Two entries sharing an asset sha make `workbook_identity` refuse - and the package used to ship
    BOTH anyway, foreign `workbook_name` and `project` included. `_provenance_luid` returns on the
    FIRST sha match, so which one a consumer believed was list-order chance.
    """
    bundle, oracle = _bundle(tmp_path)
    payload = json.loads((bundle / "source-provenance.json").read_text(encoding="utf-8"))
    twin = json.loads(json.dumps(payload["inputs"][0]))
    twin["origin"] = {
        "workbook_luid": OTHER_LUID,
        "workbook_name": f"{FOREIGN} Workbook",
        "project": f"{FOREIGN} Project",
        "match": "sha256",
    }
    payload["inputs"].append(twin)
    (bundle / "source-provenance.json").write_text(json.dumps(payload), encoding="utf-8")

    result = _package(tmp_path, bundle, oracle)
    scoped = json.loads((_out(tmp_path) / UNIT / "source-provenance.json").read_text(encoding="utf-8"))
    assert result["workbook_identity"].get("luid") is None
    assert scoped["inputs"] == []
    assert FOREIGN not in json.dumps(scoped, ensure_ascii=False)
    assert scoped["scope"]["suppressed_reason"], "the refusal must stay visible, not just silent"


def test_the_provenance_carries_only_the_three_fields_the_gate_reads(tmp_path: Path) -> None:
    """`workbook_name`/`project` are a foreign-identity channel and are not among them."""
    bundle, oracle = _bundle(tmp_path)
    payload = json.loads((bundle / "source-provenance.json").read_text(encoding="utf-8"))
    payload["future_scan_root"] = FOREIGN
    payload["inputs"][0]["origin"]["future_source_path"] = FOREIGN
    payload["inputs"][0]["origin"]["project"] = f"{FOREIGN} Project"
    (bundle / "source-provenance.json").write_text(json.dumps(payload), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    scoped = json.loads((_out(tmp_path) / UNIT / "source-provenance.json").read_text(encoding="utf-8"))
    assert FOREIGN not in json.dumps(scoped, ensure_ascii=False)
    assert sorted(scoped["inputs"][0]["origin"]) == ["match", "workbook_luid"]
    assert sorted(scoped["inputs"][0]["input"]) == ["file", "sha256"]
    assert scoped["inputs"][0]["origin"]["workbook_luid"] == WB_LUID


def test_the_handover_slice_drops_its_estate_section(tmp_path: Path) -> None:
    """Measured: all 46 real slices carry `estate`, no consumer reads it, and it is estate-wide.

    It holds `definition_of_done_status` for the whole run and `pending_gates` counting 220 stubbed
    calcs and 396 warned visuals across ALL 48 workbooks - the same class as `report.json`'s
    `summary`, shipping in a second place. 94,668 bytes across the reference estate.
    """
    bundle, oracle = _bundle(tmp_path)
    path = bundle / "handover" / f"{UNIT}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["estate"] = {"pending_gates": [{"gate": "second_compiler", "count": 220}], "source": {"root": FOREIGN}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    _package(tmp_path, bundle, oracle)
    shipped = json.loads((_out(tmp_path) / UNIT / "handover" / f"{UNIT}.json").read_text(encoding="utf-8"))
    assert "estate" not in shipped
    assert "workbook" in shipped, "the half every consumer reads must survive"
    assert FOREIGN not in json.dumps(shipped, ensure_ascii=False)


def test_an_absolute_path_anywhere_in_the_handover_slice_is_redacted(tmp_path: Path) -> None:
    """The value-shaped half: a field nobody enumerated cannot smuggle a host path out.

    ⚠️ Named residual, stated rather than implied: this closes the PATH class, not "any unknown
    field". An unknown non-path field inside `workbook` still ships, because `workbook` is this
    unit's own work queue and enumerating the engine's volatile schema would be the fourth allowlist
    in four rounds. Measured on the reference estate: of 46 slices, 0 carry another unit's business;
    2 contain a string that is also another unit's name, and in both it is this workbook's own
    datasource/model binding (`Groups` is a datasource at `binding_signal.secondary_published_datasources`;
    `datasource_test` is `ephemeral_field`'s own `bound_model`).
    """
    bundle, oracle = _bundle(tmp_path)
    path = bundle / "handover" / f"{UNIT}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["workbook"]["future_install_root"] = f"{HOST_PATH}{_SEP}engine"
    payload["workbook"]["nested"] = {"deep": [{"also": f"{HOST_PATH_ROOT}{_SEP}x"}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = _package(tmp_path, bundle, oracle)
    shipped = json.loads((_out(tmp_path) / UNIT / "handover" / f"{UNIT}.json").read_text(encoding="utf-8"))
    assert absolute_host_paths(shipped) == []
    assert shipped["workbook"]["future_install_root"] == ms.REDACTED
    assert shipped["workbook"]["nested"]["deep"][0]["also"] == ms.REDACTED
    assert any("redacted" in note for note in result["notes"]), "a redaction must be reported, not silent"


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


def _adopt_as_previously_packaged(root: Path) -> None:
    """Record the package's CURRENT contents as what packaging wrote, so planted files read as STALE.

    This is the whole distinction the #460 edit guard makes, and it is a distinction about the
    RECORD, not about the bytes. A file packaging wrote last run and the new input no longer
    produces is stale; a file no run ever wrote is an addition, and an addition is what an agent
    editing the canonical `fabric/` tree makes. On disk the two are identical, so a test that plants
    unrecorded files is testing the guard, not staleness - which is exactly why the version of this
    test that did so started failing the moment the guard landed.
    """
    manifest_path = root / pkg.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contents"]["files"] = pkg.package_contents(root)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_repackaging_removes_a_stale_file_from_every_copied_tree(tmp_path: Path) -> None:
    """Not just `oracle/`: stale `assets/` and `fabric/` files persisted the same way.

    The planted files are made stale by RECORDING them, not merely by writing them - see
    `_adopt_as_previously_packaged`. Skip that step and this exercises the #460 edit guard instead,
    and proves nothing at all about replace-not-merge.
    """
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
    _adopt_as_previously_packaged(root)
    assert pkg.package_edits(root) == ([], None), "the fixture planted EDITS, so it would test the guard instead"

    _package(tmp_path, bundle, oracle)
    survivors = [str(path.relative_to(root)) for path in planted if path.exists()]
    assert survivors == [], f"stale artifacts survived repackaging: {survivors}"


# The package is the canonical place to edit (#460), and `replace_dir` above replaces it WHOLE - so
# re-running the same command silently deleted an agent's work. The guard and the removal contract
# above pull in opposite directions on the same tree, and both are load-bearing; the seam between
# them is the digest packaging records of its own output.
# --------------------------------------------------------------------------------------------


def test_repackaging_REFUSES_when_the_package_carries_an_edit(tmp_path: Path) -> None:
    """Silent loss is the one unacceptable outcome, so an unrecorded change stops the re-run.

    Both shapes an agent makes are asserted, because the guard compares a SET of digests and an
    added file and a modified file take different branches of it: a new page under the canonical
    `fabric/` tree, and an appended `limitations_encountered` entry in `migration-spec.json`.
    """
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    added = root / "fabric" / f"{UNIT}.Report" / "definition" / "pages" / "agent-authored-page.json"
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text('{"name": "agent"}', encoding="utf-8")
    spec = root / "migration-spec.json"
    spec.write_text(json.dumps({"limitations_encountered": [{"item": "x"}]}), encoding="utf-8")

    with pytest.raises(pkg.PackageEditsRefused) as refusal:
        _package(tmp_path, bundle, oracle)
    assert refusal.value.reason is None
    assert sorted(refusal.value.changed) == [
        "fabric/Book.Report/definition/pages/agent-authored-page.json",
        "migration-spec.json",
    ]
    assert added.read_text(encoding="utf-8") == '{"name": "agent"}', "the refused run overwrote the edit anyway"
    assert "--discard-package-edits" in str(refusal.value), "the refusal does not name the way out"


def test_an_edit_made_DURING_assembly_is_not_overwritten_by_the_swap(tmp_path: Path) -> None:
    """Round-2 finding 3: the #460 guard checked the digest ONCE, before assembly began.

    Assembly is not instant - it copies a render tree and shells out to `parse_tableau.py` - and
    nothing re-read the package before `replace_dir` deleted it. An edit made anywhere in that
    window was accepted by the filesystem and then destroyed by the swap, with the guard already
    "passed": the reviewer measured `edit_survived_repackage=False`.

    The window is simulated the way the reviewer did, by editing the canonical package from inside
    `_assemble_unit` - that is the only way to land a write in a window that is otherwise a race.
    What is asserted is not the timing but the OUTCOME: the edit is still on disk, and the run
    refused rather than reporting success.
    """
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    marker = '{"name": "written while packaging was running"}'
    edited = root / "fabric" / f"{UNIT}.Report" / "definition" / "pages" / "agent-page.json"

    original = pkg._assemble_unit  # noqa: SLF001

    def _assemble_then_edit(*args: object, **kwargs: object) -> dict:
        result = original(*args, **kwargs)
        edited.parent.mkdir(parents=True, exist_ok=True)
        edited.write_text(marker, encoding="utf-8")
        return result

    pkg._assemble_unit = _assemble_then_edit  # noqa: SLF001
    try:
        with pytest.raises(pkg.PackageEditsRefused) as refusal:
            _package(tmp_path, bundle, oracle)
    finally:
        pkg._assemble_unit = original  # noqa: SLF001

    assert edited.is_file(), "the edit made during assembly was destroyed by the swap"
    assert edited.read_text(encoding="utf-8") == marker
    assert "fabric/Book.Report/definition/pages/agent-page.json" in refusal.value.changed
    assert (root / pkg.MANIFEST_NAME).is_file(), "the package was left half-replaced"
    assert not list(_out(tmp_path).glob(".*.replaced")), "the retired copy was not restored"


def test_the_second_digest_check_does_not_refuse_an_UNEDITED_package(tmp_path: Path) -> None:
    """The negative control for the re-check: a clean re-run must still replace the package.

    A guard that refuses everything would pass the test above and make the tool useless, and the
    check now runs at a moment - after the existing package has been renamed aside - where getting
    the path wrong would refuse every single run.

    ⚠️ It asserts the file SET, not the digests: `migration-spec.json` is not byte-stable between
    runs, which is precisely why the guard compares a package against the digest THAT run recorded
    rather than against a previous run's.
    """
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    first = set(json.loads((root / pkg.MANIFEST_NAME).read_text(encoding="utf-8"))["contents"]["files"])

    _package(tmp_path, bundle, oracle)
    assert set(json.loads((root / pkg.MANIFEST_NAME).read_text(encoding="utf-8"))["contents"]["files"]) == first
    assert pkg.package_edits(root) == ([], None), "the re-run left the package disagreeing with its own record"
    # ⚠️ Asserted against the names the packager ACTUALLY writes. These were `.*.replaced` /
    # `.*.staging`, both retired by #476 (staging is now `.<digest>`, the retired tree
    # `.<digest>~`), so after the merge those globs matched nothing and this leak-check passed
    # without being able to observe a leak at all. The sweep for any hidden sibling is what stops a
    # third scratch name doing the same thing again.
    staging = pkg.staging_dir(_out(tmp_path), UNIT)
    assert not staging.exists(), f"the staging directory survived the run: {staging}"
    assert not staging.with_name(f"{staging.name}~").exists(), "the retired package survived the run"
    leaked = sorted(path.name for path in _out(tmp_path).iterdir() if path.name.startswith("."))
    assert leaked == [], f"packaging left hidden scratch behind in --out: {leaked}"


def test_a_package_with_no_recorded_digest_is_REFUSED_rather_than_assumed_clean(tmp_path: Path) -> None:
    """ "I cannot tell whether this was edited" is not "it was not edited" - it is its own answer.

    A package written before the digest existed, or one whose manifest was removed, carries no
    record to compare against. Treating that as unedited is the collapse this repo keeps re-fixing,
    so it refuses with a REASON and an empty change list rather than overwriting.
    """
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    (root / pkg.MANIFEST_NAME).unlink()

    changed, reason = pkg.package_edits(root)
    assert changed == []
    assert reason is not None and "cannot be established" in reason
    with pytest.raises(pkg.PackageEditsRefused) as refusal:
        _package(tmp_path, bundle, oracle)
    assert refusal.value.reason == reason


def test_discard_package_edits_overwrites_the_package_deliberately(tmp_path: Path) -> None:
    """The override exists so the guard costs a flag rather than a wedged estate.

    Asserted through `main`, not `package_unit`, because the flag is the whole user-facing contract:
    a refusal exits 3 and leaves the package untouched, and the same run with the flag exits 0 and
    rebuilds it.
    """
    bundle, oracle = _bundle(tmp_path)
    _package(tmp_path, bundle, oracle)
    root = _out(tmp_path) / UNIT
    edited = root / "fabric" / f"{UNIT}.Report" / "definition" / "pages" / "agent-authored-page.json"
    edited.parent.mkdir(parents=True, exist_ok=True)
    edited.write_text('{"name": "agent"}', encoding="utf-8")
    argv = ["--bundle", str(bundle), "--out", str(_out(tmp_path)), "--oracle", str(oracle), "--quiet"]

    assert pkg.main([*argv, "--assets", str(bundle.parent / "assets")]) == 3
    assert edited.is_file(), "a refused run destroyed the edit it refused to overwrite"
    assert pkg.main([*argv, "--assets", str(bundle.parent / "assets"), "--discard-package-edits"]) == 0
    assert not edited.exists(), "--discard-package-edits did not rebuild the package"


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


def test_the_receipt_is_descoped_to_the_engine_version(tmp_path: Path) -> None:
    """`artifacts[]` is read by nobody in a package, so it is no longer shipped.

    `check_engine_receipts.py:33-35` reads `engine.version` and nothing else;
    `credential_gate._receipt_artifacts` is only reachable through `_receipt_matches_bundle`, which
    raises OSError on the package's absent `input_manifest.json` first. On the reference bundle that
    list held 3,138 entries, 3,135 of which were not in the package.
    """
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
                ],
            }
        ),
        encoding="utf-8",
    )
    result = _package(tmp_path, bundle, oracle)
    receipt = json.loads((_out(tmp_path) / UNIT / "engine-output-receipt.json").read_text(encoding="utf-8"))
    assert result["engine"] == "2.339.0"
    assert receipt["engine"]["version"] == "2.339.0"
    assert "artifacts" not in receipt
    assert "artifacts" in receipt["scope"]["dropped_fields"]


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


# --------------------------------------------------------------------------------------------
# 7. the path budget (#476) - a package Power BI Desktop cannot open, refused before it is written
# --------------------------------------------------------------------------------------------
#
# The field failure: `[WinError 206] The filename or extension is too long`, thrown mid-assembly on
# unit 30 of 47 of a live estate, naming no path, no limit and no remedy. Every test below is
# ARITHMETIC on the destination side - the projected paths are strings that are never created - so
# the boundary is exercised identically on the Linux and Windows CI runners, neither of which can be
# assumed to write a 270-character path at all. The BUNDLE these read is deliberately short.

#: The two generated id segments from the crash report, kept verbatim so the fixture is the shape
#: that actually failed rather than a contrived deep tree.
FIELD_PAGE_ID = "page-ws-CA-Summadc2995b2"
FIELD_VISUAL_ID = "slicer-page-ws-Cb3f14546"

#: Short enough that the SOURCE tree stays far below any limit on both CI runners, and longer than
#: the staging stem so the FINAL package is the deeper of the two roots. The length under test comes
#: from `--out`, which is the side issue #476 reports.
BUDGET_UNIT = "Unit_Sales"


def _pbir_bundle(tmp_path: Path, *units: str) -> Path:
    """A bundle whose units each have the deepest-path shape that crashed in the field.

    `pbip/<Unit>/<Unit>.Report/definition/pages/<page-id>/visuals/<visual-id>/visual.json` - the unit
    name twice, then the two generated id segments.
    """
    bundle = tmp_path / "bundle"
    names = list(units) or [BUDGET_UNIT]
    write_engine_report(bundle, workbooks=names)
    for unit in names:
        visuals = (
            bundle
            / "pbip"
            / unit
            / f"{unit}.Report"
            / "definition"
            / "pages"
            / FIELD_PAGE_ID
            / "visuals"
            / FIELD_VISUAL_ID
        )
        visuals.mkdir(parents=True)
        (visuals / "visual.json").write_text("{}", encoding="utf-8")
    return bundle


def _deepest_tail(unit: str = BUDGET_UNIT) -> str:
    """The package-relative path of :func:`_pbir_bundle`'s deepest file, built independently.

    Spelled out with `Path` joins rather than read back from `package_unit`, so a test asserting a
    length is not asking the code under test what the length should be.
    """
    return str(
        Path(unit, "fabric", f"{unit}.Report", "definition", "pages", FIELD_PAGE_ID, "visuals", FIELD_VISUAL_ID)
        / "visual.json"
    )


def _padded_path(base: Path, length: int) -> Path:
    """A path under `base` measuring exactly `length` ASCII characters.

    Segments are capped at 60 characters because a single name may not exceed 255 on either CI
    runner, and because a real `--out` is several directories deep rather than one absurd one.
    """
    if len(str(base)) > length:
        pytest.skip(f"{base} is already {len(str(base))} characters; cannot build a {length}-character path")
    out = base
    while len(str(out)) < length:
        deficit = length - len(str(out))
        out = out.with_name(out.name + "d") if deficit == 1 else out / ("d" * min(deficit - 1, 60))
    assert len(str(out)) == length
    return out


def _synthetic_out(length: int) -> Path:
    """A never-created `--out` of exactly `length` characters, rooted at the filesystem anchor.

    The projection is arithmetic on strings and touches no filesystem, which is what lets these tests
    sit exactly on the 259-character boundary. Anchoring at the drive root rather than under
    `tmp_path` is deliberate: pytest's own temp path is already ~104 characters here, so a boundary
    case needing a 102-character `--out` would SKIP on the machine it matters most on - and a skipped
    boundary test proves nothing.
    """
    return _padded_path(Path(Path.cwd().anchor or "/") / "pkg", length)


def _pin_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the CLI judge absolute paths against WINDOWS ceilings, whichever runner is executing.

    `package_unit` asks :func:`package_unit.platform_limits` what the HOST may write (blind-review
    B3), so a test that means "this is what a Windows operator sees" has to say so - otherwise it
    asserts a refusal that the ubuntu runner correctly declines to make, and the two CI legs
    disagree about arithmetic neither of them is wrong about.
    """
    monkeypatch.setattr(pkg, "platform_limits", lambda system=None: pkg.WINDOWS_LIMITS)


def test_staging_is_shallower_than_the_package_it_becomes(tmp_path: Path) -> None:
    """The staging segment sits at the DEEPEST point of every path assembly writes.

    `<out>/.{unit}.staging/` made every one of the hundreds of files in a PBIR tree 9 characters
    longer than its final home - overhead paid per file, in the exact resource that ran out.
    """
    field_unit = "IA_Operation_Health_Summary_Dashboard"
    out = tmp_path / "packages"
    assert len(str(pkg.staging_dir(out, field_unit))) < len(str(out / field_unit))


def test_the_staging_name_costs_a_CONSTANT_never_the_length_of_the_unit_name(tmp_path: Path) -> None:
    """A 37-character unit and a 4-character one stage under names of the same length.

    This is the property that makes the reclaim predictable: the staging overhead stops scaling with
    the name the engine already repeats twice inside the tree.
    """
    out = tmp_path / "packages"
    assert len(pkg.staging_dir(out, "IA_Operation_Health_Summary_Dashboard").name) == len(
        pkg.staging_dir(out, "Unit").name
    )


def test_two_units_do_not_stage_under_the_same_directory(tmp_path: Path) -> None:
    """Shortening must not be bought by making the staging name shared - `--unit A --unit B` into one
    `--out` would then assemble both in the same tree."""
    out = tmp_path / "packages"
    assert pkg.staging_dir(out, "Sales_Dashboard") != pkg.staging_dir(out, "Profit_Dashboard")


def test_nothing_is_assembled_deeper_than_the_pre_fix_staging_tree(tmp_path: Path) -> None:
    """The property that holds for EVERY unit name, stated without overclaiming.

    ⚠️ "staging is never deeper than the final package" is **false** below ~9 characters: a unit
    called `B` stages under a 9-character hidden name, so it is 8 characters deeper than `<out>/B`.
    That is measured, not hidden - and it is still strictly better than `<out>/.B.staging`, which was
    10. What is true for every name is that the overhead is a CONSTANT ceiling instead of
    `len(unit) + 9`, and a unit that short has a short tail anyway (the engine repeats the name
    twice inside the tree).
    """
    out = tmp_path / "packages"
    for unit in ("B", "Unit", "Unit_Sales", "Sales_Dashboard", "IA_Operation_Health_Summary_Dashboard"):
        assert len(pkg.staging_dir(out, unit).name) < len(f".{unit}.staging"), unit


def test_the_retired_package_is_never_named_after_the_package_it_retires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`replace_dir` moves the old package aside and `shutil.rmtree` then WALKS it.

    `.{name}.replaced` made every path in a package being REPLACED 10 characters longer than the one
    anything had measured, so a package that fitted could still fail its second run - in a tree
    nothing looks at.
    """
    staged, final = tmp_path / "s", tmp_path / "Sales_Performance_Dashboard"
    staged.mkdir()
    final.mkdir()
    seen: list[Path] = []
    rename = pkg._rename_retrying  # pylint: disable=protected-access

    def _record(src: Path, dst: Path) -> None:
        seen.append(dst)
        rename(src, dst)

    monkeypatch.setattr(pkg, "_rename_retrying", _record)
    pkg.replace_dir(staged, final)
    assert seen, "replace_dir must move the existing package aside before the swap"
    assert len(seen[0].name) < len(f".{final.name}.replaced"), seen[0]


def test_the_budget_measures_both_the_staged_and_the_final_tree(tmp_path: Path) -> None:
    """Neither root may be assumed to be the deeper one - which of the two wins depends on the name.

    A file that fits its final home and not its staging path is exactly `[WinError 206]`, and a
    package that fits while staged and not once renamed is a package Power BI Desktop refuses.
    """
    bundle = _pbir_bundle(tmp_path)
    out = tmp_path / "packages"
    projected = pkg.projected_paths(bundle, BUDGET_UNIT, out)
    by_root = {
        root: {path.tail for path in projected if path.path.startswith(f"{root}{os.sep}") or path.path == str(root)}
        for root in pkg.package_roots(out, BUDGET_UNIT)
    }
    assert len(by_root) == 3, "the final tree, the staged tree and the retired tree"
    assert all(tails for tails in by_root.values()), "every root must be measured"
    assert len({frozenset(tails) for tails in by_root.values()}) == 1, "the same tails at every root"


def test_a_unit_whose_paths_exceed_the_ceiling_is_refused_BEFORE_anything_is_written(tmp_path: Path) -> None:
    """The customer's shape: 47 units, a canonical `_runs/<NNN>-<slug>/packages/<batch>/` `--out`.

    The old failure wrote 29 packages first and then threw `[WinError 206]` from inside `shutil`.

    ``limits`` is pinned to Windows' rather than inherited from the runner, because that is the
    platform whose arithmetic this test states - see :func:`package_unit.platform_limits` and the
    POSIX case below.
    """
    bundle = _pbir_bundle(tmp_path)
    out = _padded_path(tmp_path, 200)
    with pytest.raises(pkg.PackagePathTooLong) as excinfo:
        pkg.package_unit(bundle, BUDGET_UNIT, out, oracle_dir=None, assets_dir=None, limits=pkg.WINDOWS_LIMITS)
    assert not out.exists(), "the refusal must precede every write, including --out itself"
    assert not (tmp_path / "packages").exists()
    assert excinfo.value.budget.unit == BUDGET_UNIT


def test_the_refusal_names_the_path_its_length_the_ceiling_and_the_characters_to_reclaim(tmp_path: Path) -> None:
    """`[WinError 206]` names none of the four. Each one is separately actionable.

    The arithmetic is checked against a number the test knows independently - the length of the
    `--out` it built - and for internal consistency, so a message that merely looks plausible fails.
    """
    bundle = _pbir_bundle(tmp_path)
    out = _padded_path(tmp_path, 200)
    with pytest.raises(pkg.PackagePathTooLong) as excinfo:
        pkg.package_unit(bundle, BUDGET_UNIT, out, oracle_dir=None, assets_dir=None, limits=pkg.WINDOWS_LIMITS)
    message = str(excinfo.value)

    assert str(out / BUDGET_UNIT) in message or str(pkg.staging_dir(out, BUDGET_UNIT)) in message, (
        f"the offending path is not named: {message}"
    )
    measured = re.search(
        r"deepest: (\d+) UTF-16 units, (\d+) over the (\d+)-character (file|directory) ceiling", message
    )
    assert measured is not None, message
    length, over, ceiling = (int(measured.group(index)) for index in (1, 2, 3))
    assert ceiling in (259, 247), "the ceilings come from check_path_ceiling.py, measured against Desktop"
    assert length - ceiling == over > 0

    remedy = re.search(r"--out is (\d+) character\(s\) long; it must be at most (-?\d+) \((\d+) shorter\)", message)
    assert remedy is not None, message
    current, allowed, reclaim = (int(remedy.group(index)) for index in (1, 2, 3))
    assert current == len(str(out)) == 200, "the length reported for --out is not the one it has"
    assert allowed + reclaim == current
    assert "docs/windows-path-limits.md" in message


def test_the_budget_measures_the_STAGED_tree_too_not_only_the_final_one(tmp_path: Path) -> None:
    """A one-character unit stages under a 9-character name, so staging is the deeper of the two.

    `--out` is sized so the FINAL tree fits the ceiling exactly and only the scratch trees are over:
    a check that measured the destination alone would package this unit and then die assembling it.
    """
    unit = "B"
    bundle = _pbir_bundle(tmp_path, unit)
    out = _synthetic_out(pkg.WINDOWS_LIMITS.file_ceiling - len(_deepest_tail(unit)) - 1)
    budget = pkg.path_budget(bundle, unit, out, limits=pkg.WINDOWS_LIMITS)
    final = str(out / unit)
    assert budget.overruns, "the scratch trees are over the ceiling and the final tree is not"
    assert not [path for path in budget.overruns if path.path.startswith(f"{final}{os.sep}")], [
        path.path for path in budget.overruns
    ]


def test_scratch_overrun_does_not_consume_the_WINDOWS_shipping_budget(tmp_path: Path) -> None:
    """A POSIX host may need a long scratch root while the final package remains relocatable."""
    unit = "B"
    bundle = _pbir_bundle(tmp_path, unit)
    out = _synthetic_out(cpc.POSIX_PATH_CEILING - len(_deepest_tail(unit)) - 1)

    budget = pkg.path_budget(bundle, unit, out, limits=cpc.POSIX_LIMITS)

    assert budget.overruns, "the staging or retired scratch root must exceed the POSIX host limit"
    assert budget.shipping == [], "only the final package is judged for Windows portability"
    final_paths = [
        path
        for path in pkg.projected_paths(bundle, unit, out, limits=cpc.POSIX_LIMITS)
        if path.path.startswith(f"{out / unit}{os.sep}") or path.path == str(out / unit)
    ]
    assert budget.shipping_budget == min(
        (pkg.WINDOWS_LIMITS.dir_ceiling if path.kind == pkg.KIND_DIR else pkg.WINDOWS_LIMITS.file_ceiling)
        - (path.length - pkg.utf16_len(str(out)))
        for path in final_paths
    )

    staging = tmp_path / "staging"
    assembled_file = staging / _deepest_tail(unit)
    assembled_file.parent.mkdir(parents=True)
    assembled_file.write_text("{}", encoding="utf-8")
    assembled = pkg.assembled_budget(unit, staging, out / unit, out, cpc.POSIX_LIMITS)
    assert assembled.overruns
    assert assembled.shipping == []


def test_a_POSIX_scratch_overrun_still_refuses_packaging(tmp_path: Path) -> None:
    """The shipping split must not weaken the host's refusal to write an overlong scratch tree."""
    unit = "B"
    bundle = _pbir_bundle(tmp_path, unit)
    out = _synthetic_out(cpc.POSIX_PATH_CEILING - len(_deepest_tail(unit)) - 1)

    with pytest.raises(pkg.PackagePathTooLong):
        pkg.package_unit(bundle, unit, out, oracle_dir=None, assets_dir=None, limits=cpc.POSIX_LIMITS)


def test_a_unit_that_fits_exactly_at_the_ceiling_is_NOT_refused(tmp_path: Path) -> None:
    """The negative control, one character away from the test above.

    Without it every assertion here is satisfied by a check that refuses everything. The unit name is
    long enough that its own segment, not the staging stem, is the deeper root.
    """
    unit = "Sales_Performance_Dashboard"
    bundle = _pbir_bundle(tmp_path, unit)
    fits = _synthetic_out(pkg.WINDOWS_LIMITS.file_ceiling - len(_deepest_tail(unit)) - 1)
    assert pkg.path_budget(bundle, unit, fits, limits=pkg.WINDOWS_LIMITS).overruns == []
    assert pkg.path_budget(bundle, unit, fits, limits=pkg.WINDOWS_LIMITS).out_root_budget == len(str(fits))
    assert pkg.path_budget(bundle, unit, fits / "x", limits=pkg.WINDOWS_LIMITS).overruns, (
        "one character more must be refused"
    )


def test_a_unit_no_out_can_fit_says_so_instead_of_naming_an_impossible_directory(tmp_path: Path) -> None:
    """When the tail alone is over the ceiling, shortening `--out` cannot rescue it.

    Reproduced with lowered ceilings rather than a 260-character fixture tree, exactly as
    `check_path_ceiling.py`'s own `--ceiling` does - a bundle deep enough to do this for real cannot
    be created on a stock Windows runner, which is the whole reason this defect exists.
    """
    bundle = _pbir_bundle(tmp_path)
    tiny = pkg.Limits(file_ceiling=40, dir_ceiling=28)
    budget = pkg.path_budget(bundle, BUDGET_UNIT, tmp_path / "o", limits=tiny)
    assert budget.out_root_budget < 0
    message = pkg.render_path_budget(budget)
    assert "NO --out can fit this unit" in message
    assert f"{-budget.out_root_budget} character(s) over" in message


def test_main_refuses_a_too_deep_out_before_packaging_ANY_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """29 of 47 packages written, then a crash, was the expensive half of this defect.

    A shorter `--out` moves every unit, so the estate would be repackaged wholesale anyway; the run
    is refused whole and the offenders are named together with the one number that fixes all of them.
    """
    _pin_windows(monkeypatch)
    bundle = _pbir_bundle(tmp_path, BUDGET_UNIT, "Second_Unit")
    out = _padded_path(tmp_path, 200)
    with pytest.raises(SystemExit) as excinfo:
        pkg.main(["--bundle", str(bundle), "--out", str(out), "--quiet"])
    assert excinfo.value.code == 2
    assert list(out.iterdir()) == [], "nothing may be written when the run is refused"


def test_the_batch_refusal_names_every_offending_unit_and_one_number_that_fixes_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unit at a time is how the field run discovered them - 29 packages apart."""
    _pin_windows(monkeypatch)
    bundle = _pbir_bundle(tmp_path, BUDGET_UNIT, "Second_Unit")
    out = _padded_path(tmp_path, 200)
    with pytest.raises(SystemExit):
        pkg.main(["--bundle", str(bundle), "--out", str(out), "--quiet"])
    message = capsys.readouterr().err
    assert "2 of 2 unit(s)" in message
    assert BUDGET_UNIT in message and "Second_Unit" in message
    assert "NOTHING was packaged" in message
    assert re.search(r"it must be at most (-?\d+) \((\d+) shorter\) for every unit to fit", message), message


# --------------------------------------------------------------------------------------------
# 7b. the blind-review blockers against the budget in section 7
#
# Three findings, each reproduced through the real CLI on the branch before it was fixed:
#
#   B2  a unit may be named exactly like another unit's staging directory, because a leading dot is
#       a legal Tableau name. `Victim` and `.d72cee2e` were BOTH reported packaged at exit 0 and only
#       `Victim` existed on disk - `rmtree(staging)` had deleted a finished package. Reporting
#       success while destroying one is the worst outcome this file can produce, so it is first.
#   B1  the projection measured a SUBSET and said exit 0 about the rest: `assets/`, `data/`,
#       `oracle/`, a generated `expressions.tmdl` and the retired tree were all unmeasured. A valid
#       204-character workbook filename projected a maximum of 102 and shipped a 279-character path.
#   B3  Windows ceilings were applied to absolute POSIX paths, refusing a 297-character `--out`
#       whose package was valid at 332. Packages relocate, so that is a false refusal.
# --------------------------------------------------------------------------------------------


# --- B2: a staging name may never be able to name a package ---------------------------------


def test_a_unit_named_like_another_units_staging_directory_cannot_delete_it(tmp_path: Path) -> None:
    """The reproduction, unchanged: two units, one of them named `.<digest of the other>`.

    Before: `main` exited 0 having reported BOTH packaged, and the alias package was gone - deleted
    by `rmtree(staging)` on its way to assembling `Victim`. The run reported success about an
    artifact it had just destroyed, which no downstream gate can detect because the manifest that
    would have said otherwise went with it.
    """
    victim = "Victim"
    alias = pkg.staging_dir(tmp_path, victim).name
    bundle = _pbir_bundle(tmp_path, victim, alias)
    out = _out(tmp_path)
    report = tmp_path / "packaging.json"

    code = pkg.main(["--bundle", str(bundle), "--out", str(out), "--quiet", "--json", str(report)])

    assert code == pkg.EXIT_UNIT_FAILED, "the alias name is refused, and a refusal is never exit 0"
    packaged = [row["unit"] for row in json.loads(report.read_text(encoding="utf-8"))["units"]]
    assert alias not in packaged, "a name that aliases a staging path must never be reported packaged"
    assert all((out / name).is_dir() for name in packaged), (
        f"every unit reported packaged must be on disk: {packaged} vs {sorted(p.name for p in out.iterdir())}"
    )
    assert (out / victim).is_dir(), "the unit that IS packageable still packages"


def test_the_cleanup_refuses_to_delete_a_directory_this_packager_did_not_name(tmp_path: Path) -> None:
    """The tripwire behind the name refusal: `rmtree` may only target a scratch name.

    Prevention is the fix - a unit cannot be named like a staging directory - and this is asserted
    anyway because the consequence is invisible here and lands on someone else, as a package that
    was reported shipped and is not there.
    """
    package = tmp_path / "Victim"
    (package / "fabric").mkdir(parents=True)
    (package / "fabric" / "model.tmdl").write_text("rows", encoding="utf-8")

    with pytest.raises(pkg.PackagingError) as excinfo:
        pkg._discard_scratch(package)  # pylint: disable=protected-access
    assert (package / "fabric" / "model.tmdl").is_file(), "nothing may be deleted when the name is refused"
    assert "Victim" in str(excinfo.value)

    scratch = pkg.staging_dir(tmp_path, "Victim")
    scratch.mkdir()
    pkg._discard_scratch(scratch)  # pylint: disable=protected-access
    assert not scratch.exists(), "the negative control: a scratch name IS swept"


@pytest.mark.parametrize("unit", ["B", "Book", "Victim", "IA_Operation_Health_Summary_Dashboard", "Ünit", "a b"])
def test_every_scratch_name_this_packager_creates_is_one_no_unit_may_be_called(tmp_path: Path, unit: str) -> None:
    """The structural link, in both directions, so the two halves cannot drift apart.

    `staging_dir` and `retired_dir` are the only two generators; whatever they produce has to be a
    name `unit_name_problem` refuses, or the collision B2 reproduced re-opens for a name shape nobody
    thought of. The last assertion is the negative control - a real unit name is not reserved, so
    this is not satisfied by refusing everything.
    """
    for scratch in (pkg.staging_dir(tmp_path, unit), pkg.retired_dir(tmp_path / unit)):
        assert pkg.is_reserved_packaging_name(scratch.name), scratch
        assert pkg.unit_name_problem(scratch.name) is not None, scratch
    assert pkg.unit_name_problem(unit) is None, "a name a customer could really have must still package"


# --- B1: the projection, and the measurement that makes incompleteness impossible ------------


def _asset_bundle(tmp_path: Path, unit: str, asset_name: str) -> tuple[Path, Path]:
    """`(bundle, assets)` where the unit resolves a source asset with a CUSTOMER-chosen filename."""
    bundle = _pbir_bundle(tmp_path, unit)
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / asset_name).write_text("<workbook/>", encoding="utf-8")
    (bundle / "handover").mkdir(parents=True, exist_ok=True)
    (bundle / "handover" / f"{unit}.json").write_text(
        json.dumps({"workbook": {"name": unit, "source_id": f"_runs/999-x/assets/{asset_name}"}}), encoding="utf-8"
    )
    return bundle, assets


def test_the_source_assets_own_filename_is_projected_not_discovered_at_write_time(tmp_path: Path) -> None:
    """204 characters is a legal Windows filename, and the customer chose it - we did not.

    Measured before the fix: `projected maximum = 102, zero overruns`, `main_exit = 0`,
    `asset_written = True`, packaged asset path **279**. A budget that measures a subset and then
    reports exit 0 is the fail-open shape, not the safe direction.
    """
    unit, name = "Book", f"{'A' * 200}.twb"
    bundle, assets = _asset_bundle(tmp_path, unit, name)
    out = _synthetic_out(62)

    projected = pkg.projected_paths(bundle, unit, out, limits=pkg.WINDOWS_LIMITS, assets_dir=assets)
    assert f"assets/{name}" in {path.tail for path in projected}, "the asset the packager copies must be measured"

    with pytest.raises(pkg.PackagePathTooLong) as excinfo:
        pkg.package_unit(bundle, unit, out, oracle_dir=None, assets_dir=assets, limits=pkg.WINDOWS_LIMITS)
    assert excinfo.value.budget.worst.tail == f"assets/{name}"
    assert not out.exists(), "nothing may be written, including --out itself"


def test_an_output_the_projection_never_predicted_is_still_refused_before_the_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that replaces the old "not exhaustive, and deliberately so" comment.

    A projection is a MODEL of the output and blind review measured four real outputs missing from
    it. This walks the tree assembly ACTUALLY produced, before the swap, so an output a future edit
    adds appears in the budget without anyone remembering to declare it. Simulated by making an
    existing writer emit one more file - which is exactly what "somebody adds an output" looks like.
    """
    bundle, _oracle = _bundle(tmp_path)
    out = _out(tmp_path)
    assert pkg.main(["--bundle", str(bundle), "--out", str(out), "--quiet"]) == 0
    before = pkg.package_contents(out / UNIT)

    projected = max(path.length for path in pkg.projected_paths(bundle, UNIT, out))
    limits = pkg.Limits(file_ceiling=projected + 20, dir_ceiling=projected + 20)
    surprise = "z" * (projected - len(str(pkg.staging_dir(out, UNIT))) + 30)
    write_schema = pkg._write_spec_schema  # pylint: disable=protected-access

    def _and_one_more(dest: Path) -> tuple[str | None, str | None]:
        (dest / surprise).write_text("an output nobody added to the budget", encoding="utf-8")
        return write_schema(dest)

    monkeypatch.setattr(pkg, "_write_spec_schema", _and_one_more)
    with pytest.raises(pkg.PackagePathTooLong) as excinfo:
        pkg.package_unit(bundle, UNIT, out, oracle_dir=None, assets_dir=None, limits=limits)

    assert excinfo.value.budget.worst.tail == surprise
    assert pkg.package_contents(out / UNIT) == before, "the package already there must survive untouched"
    assert not pkg.staging_dir(out, UNIT).exists(), "and the staged tree must be gone"


def test_the_RETIRED_tree_is_measured_because_rmtree_WALKS_it(tmp_path: Path) -> None:
    """Three roots, not two: a package being replaced is renamed to `.<digest>~` and then walked.

    `.<digest>~` is 10 characters, staging is 9 and a unit called `B` is 1 - so for a short name the
    retired tree is the DEEPEST thing packaging touches, and `shutil.rmtree` has to be able to open
    every path in it. `--out` here is sized so the staged tree fits exactly and only the retired one
    is over.
    """
    unit = "B"
    bundle = _pbir_bundle(tmp_path, unit)
    out = _synthetic_out(pkg.WINDOWS_LIMITS.file_ceiling - len(_deepest_tail(unit)) - 9)

    assert pkg.retired_dir(out / unit) in pkg.package_roots(out, unit)
    budget = pkg.path_budget(bundle, unit, out, limits=pkg.WINDOWS_LIMITS)
    retired = f"{pkg.retired_dir(out / unit)}{os.sep}"
    assert budget.overruns, "the retired tree is over the ceiling"
    assert all(path.path.startswith(retired) for path in budget.overruns), [
        path.path for path in budget.overruns if not path.path.startswith(retired)
    ]


def test_a_length_refusal_from_the_FILESYSTEM_is_restated_with_a_path_and_a_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop for the machine that cannot even WRITE the tree there is to measure.

    On stock Windows the write throws before there is anything to walk, and `[WinError 206] The
    filename or extension is too long` named no path, no limit and no remedy - it escaped as an
    uncaught traceback whose exit 1 is indistinguishable from `EXIT_NO_WORKING_COPY`.
    """
    bundle, _oracle = _bundle(tmp_path)
    out = _out(tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    offender = str(out / UNIT / ("q" * 200))

    def _too_long(*_args: object, **_kwargs: object) -> dict:
        raise OSError(errno.ENAMETOOLONG, "File name too long", offender)

    monkeypatch.setattr(pkg, "_assemble_unit", _too_long)
    with pytest.raises(pkg.PackagingError) as excinfo:
        pkg.package_unit(bundle, UNIT, out, oracle_dir=None, assets_dir=None)
    assert offender in str(excinfo.value) and "check_path_ceiling.py" in str(excinfo.value)
    assert not list(out.iterdir()), "nothing may survive a refusal"

    def _denied(*_args: object, **_kwargs: object) -> dict:
        raise OSError(errno.EACCES, "Permission denied", offender)

    monkeypatch.setattr(pkg, "_assemble_unit", _denied)
    with pytest.raises(OSError) as unrelated:
        pkg.package_unit(bundle, UNIT, out, oracle_dir=None, assets_dir=None)
    assert not isinstance(unrelated.value, pkg.PackagingError), "somebody else's OSError is not relabelled"


# --- B3: Desktop's ceiling belongs to the TAILS, not to somebody's build directory -----------


def test_a_long_POSIX_out_is_not_refused_by_a_ceiling_that_belongs_to_WINDOWS(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured before the fix: a 297-character POSIX `--out` exited 2, and bypassing only the budget
    produced a valid package at exit 0 whose deepest path was 332.

    259/247 were measured against Power BI Desktop on Windows. Applying them to an absolute path on a
    host that has no such limit refuses a build that would be fine the moment it is relocated - and
    relocation is what a package is FOR. The POSIX half is read through the DEFAULT, because the
    defect was in which ceilings the default picks.
    """
    bundle = _pbir_bundle(tmp_path)
    out = _synthetic_out(297)

    assert pkg.path_budget(bundle, BUDGET_UNIT, out, limits=pkg.WINDOWS_LIMITS).refused

    monkeypatch.setattr(pkg, "platform_limits", lambda system=None: cpc.POSIX_LIMITS)
    posix = pkg.path_budget(bundle, BUDGET_UNIT, out)
    assert not posix.refused, [path.path for path in posix.overruns]
    assert posix.shipping_budget >= 0, "and the package still fits a Windows root once it lands"


def test_a_tail_no_WINDOWS_root_can_fit_is_refused_even_on_a_generous_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half of Desktop's ceiling that is NOT a false positive - and the negative control for B3.

    Without this, "stop applying Windows ceilings on POSIX" would mean a Linux run happily building
    a package no Windows machine can ever open. The ceilings are lowered here rather than a
    260-character tree created, exactly as `check_path_ceiling.py`'s own `--ceiling` does.
    """
    monkeypatch.setattr(pkg, "WINDOWS_LIMITS", pkg.Limits(file_ceiling=40, dir_ceiling=28))
    bundle = _pbir_bundle(tmp_path)
    budget = pkg.path_budget(bundle, BUDGET_UNIT, tmp_path / "o", limits=cpc.POSIX_LIMITS)

    assert budget.overruns == [], "the host itself is content with these paths"
    assert budget.shipping, "no Windows root can fit this unit, so it is refused anyway"
    assert budget.refused and budget.hard_budget < 0
    assert "NO --out can fit this unit" in pkg.render_path_budget(budget)


def test_a_package_that_barely_fits_a_WINDOWS_root_WARNS_and_still_ships(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the B3 split: advisory, never a refusal.

    A tight relocation budget is a shipping hazard rather than a defect - `C:\\Users\\<name>\\
    Documents\\` is ~28 characters before a customer makes one folder - so it is reported and the
    package still ships. The first run is the negative control: a shallow package does not warn.
    """
    bundle, _oracle = _bundle(tmp_path)
    command = ["--bundle", str(bundle), "--out", str(_out(tmp_path)), "--quiet"]

    assert pkg.main(command) == 0
    assert "tolerate a Windows root" not in capsys.readouterr().err

    monkeypatch.setattr(pkg, "SHIPPING_ROOT_BUDGET_ADVISORY", 10_000)
    assert pkg.main(command) == 0, "the advisory must not change the verdict"
    warning = capsys.readouterr().err
    assert "WARN" in warning and UNIT in warning
    assert "tolerate a Windows root of fewer than 10000 characters" in warning


# --------------------------------------------------------------------------------------------
# 8. the blind-review round-4 blockers
#
# Six findings, each reproduced through the real CLI on the branch before it was fixed, and each
# invisible to every gate that was already green. They share one shape: a verdict that reads as a
# pass while the thing it is a verdict ABOUT could not be established, could not be contained, or
# was never even the right file.
# --------------------------------------------------------------------------------------------


def _cli(tmp_path: Path, bundle: Path, *extra: str) -> int:
    """The CLI as an operator runs it, returning the exit CODE - never the printed text."""
    return pkg.main(["--bundle", str(bundle), "--out", str(_out(tmp_path)), "--quiet", *extra])


# --- B6: a source-controlled unit name must not choose where this packager writes ------------


@pytest.mark.parametrize(
    "evil",
    [
        r"..\escaped-package",
        "../escaped-posix",
        r"sub\..\..\escaped-nested",
        "sub/../../escaped-nested-posix",
        r"C:\escaped-absolute",
        "/escaped-rooted",
        "..",
    ],
)
def test_a_unit_name_that_escapes_the_out_directory_is_refused(tmp_path: Path, evil: str) -> None:
    """Reproduced: a workbook named `..\\escaped-package` wrote a full package OUTSIDE `--out`.

    The name is not ours - `bundle_units` reads it from the engine's `report.json`
    (`workbooks[].name`) and from `pbip/` directory names, both of which come from the customer's
    Tableau estate, so this is source-controlled input reaching a filesystem write. Measured before
    the fix: `requested_out=...\\packages\\out`, `written=...\\packages\\escaped-package`,
    `written_is_inside_out=False`, exit 1, empty stderr.

    Both separators are exercised on BOTH platforms on purpose: a packaging host is not necessarily
    the harvest host, and `..\\x` is one innocent filename to `PurePosixPath` and a traversal to
    Windows.
    """
    bundle, _ = _bundle(tmp_path)
    write_engine_report(bundle, workbooks=[UNIT, evil])
    assert evil in pkg.bundle_units(bundle), "the fixture must actually offer the hostile name"

    before = {path for path in _out(tmp_path).parent.rglob("*")} if _out(tmp_path).parent.exists() else set()
    code = _cli(tmp_path, bundle, "--unit", evil)
    after = {path for path in _out(tmp_path).parent.rglob("*")}

    assert code == pkg.EXIT_UNIT_FAILED
    out = _out(tmp_path)
    escaped = sorted(path for path in after - before if path != out and out not in path.parents)
    assert not escaped, f"packaging wrote outside --out: {escaped}"


def test_the_unit_name_guard_names_the_problem_rather_than_sanitizing_it(tmp_path: Path) -> None:
    """Refused, not rewritten: every later join is keyed by the unit name.

    Silently mapping `..\\x` to `_x` would package a unit under a name that matches nothing in the
    bundle - no handover slice, no oracle attribution, and `promote_unit.py` reading a manifest kind
    for a unit the engine never emitted.
    """
    assert pkg.unit_name_problem(UNIT) is None
    assert pkg.unit_name_problem("Sales - 2026 (final).v2") is None
    for evil in (r"..\x", "../x", "", "   ", ".", "..", r"C:\x", "/x", r"a\b", "a/b"):
        assert pkg.unit_name_problem(evil) is not None, f"{evil!r} was accepted as a unit name"

    # Each branch has to be independently observable, or a mutation that deletes one is caught only
    # by the catch-all below it and the deleted branch is credited as covered. Measured while
    # writing this: removing the separator branch entirely left every rejection intact, because
    # `PurePath(...).name` rejects the same strings with a vaguer reason.
    assert "path separator" in (pkg.unit_name_problem(r"a\b") or "")
    assert "path separator" in (pkg.unit_name_problem("a/b") or "")
    assert "relative directory reference" in (pkg.unit_name_problem("..") or "")
    assert "drive-qualified" in (pkg.unit_name_problem("C:") or "")
    assert "empty" in (pkg.unit_name_problem("   ") or "")

    with pytest.raises(pkg.UnsafeUnitName):
        pkg.assert_package_destination(tmp_path, r"..\escape")
    assert pkg.assert_package_destination(tmp_path, UNIT) == tmp_path / UNIT


# --- B5: "cannot assess" is its own blocking state, distinct from clean AND from failed -------


def test_a_truncated_engine_report_is_refused_rather_than_packaged_unclassified(tmp_path: Path) -> None:
    """Reproduced: `exit 0  OK Book [unclassified]`, with no notes at all.

    `read_json` returns None for absent AND for corrupt, so a `report.json` nobody could parse read
    as "this bundle classifies nothing" - and the unit was packaged with a classification invented
    by the fallback.
    """
    bundle, _ = _bundle(tmp_path)
    (bundle / "report.json").write_text('{"workbooks": [{"name": "Book"', encoding="utf-8")
    assert _cli(tmp_path, bundle, "--unit", UNIT) == pkg.EXIT_CANNOT_ASSESS
    assert not (_out(tmp_path) / UNIT).exists(), "a package was written from input that could not be read"


def test_a_truncated_handover_slice_is_refused_rather_than_silently_absent(tmp_path: Path) -> None:
    """Reproduced: `exit 0`, and the handover slice simply not in the package."""
    bundle, _ = _bundle(tmp_path)
    (bundle / "handover" / f"{UNIT}.json").write_text('{"workbook": {"source_id"', encoding="utf-8")
    assert _cli(tmp_path, bundle, "--unit", UNIT) == pkg.EXIT_CANNOT_ASSESS
    assert not (_out(tmp_path) / UNIT).exists()


@pytest.mark.parametrize("name", ["source-provenance.json", "engine-output-receipt.json", "input_manifest.json"])
def test_every_bundle_input_that_must_be_read_is_refused_when_it_is_corrupt(tmp_path: Path, name: str) -> None:
    """The whole enumerated surface, not just the two the reviewer happened to truncate."""
    bundle, _ = _bundle(tmp_path)
    (bundle / name).write_text("{ not json", encoding="utf-8")
    assert _cli(tmp_path, bundle, "--unit", UNIT) == pkg.EXIT_CANNOT_ASSESS
    assert not (_out(tmp_path) / UNIT).exists()


def test_an_ABSENT_input_still_packages_because_absence_is_not_corruption(tmp_path: Path) -> None:
    """The negative control the refusal above must not swallow.

    `source-provenance.json`, `engine-output-receipt.json` and `input_manifest.json` are all
    legitimately absent in real bundle shapes, and a packager that refused an absence would refuse
    most of the reference estate. Corruption is what cannot be assessed; absence is a fact.
    """
    bundle, _ = _bundle(tmp_path)
    for name in ("source-provenance.json", "engine-output-receipt.json", "input_manifest.json"):
        (bundle / name).unlink(missing_ok=True)
    assert _cli(tmp_path, bundle, "--unit", UNIT) == 0
    assert (_out(tmp_path) / UNIT / "package-manifest.json").is_file()


def test_a_workbook_whose_source_asset_vanished_cannot_be_assessed(tmp_path: Path) -> None:
    """Reproduced: `exit 0  OK Book`, for a package on which BOTH gates return CANNOT_ESTABLISH.

    Without the source, `check_unit` cannot derive an expected page set (#443) and the entry gate
    reports `CANNOT_ESTABLISH`, so every per-page verdict the package would yield is "I do not
    know". Saying `OK` for it is the clean-verdict-from-unreadable-input class exactly.
    """
    bundle, _ = _bundle(tmp_path)
    for path in (tmp_path / "assets").iterdir():
        path.unlink()
    assert _cli(tmp_path, bundle, "--unit", UNIT) == pkg.EXIT_CANNOT_ASSESS
    assert not (_out(tmp_path) / UNIT).exists()


def test_a_datasource_unit_with_no_asset_still_packages_because_it_claims_no_pages(tmp_path: Path) -> None:
    """The negative control for the rule above - 18 of 67 units in the reference run are this shape.

    A datasource-only unit ships a model and no report, so it makes no page claim and its missing
    asset blinds nothing. Refusing it would refuse a quarter of the estate.
    """
    bundle, oracle = _bundle(tmp_path, datasources=("Shared_Extract",))
    model = bundle / "pbip" / "Shared_Extract" / "Shared_Extract.SemanticModel" / "definition"
    model.mkdir(parents=True, exist_ok=True)
    (model / "model.tmdl").write_text("model Model\n", encoding="utf-8")
    result = pkg.package_unit(
        bundle, "Shared_Extract", _out(tmp_path), oracle_dir=oracle, assets_dir=bundle.parent / "assets"
    )
    assert result["artifacts"]["asset"] is None
    assert (_out(tmp_path) / "Shared_Extract" / "fabric" / "Shared_Extract.SemanticModel").is_dir()


def test_a_bundle_that_names_no_units_at_all_never_reports_success(tmp_path: Path) -> None:
    """Reproduced: `packaged 0/0` at exit 0 - true, useless, and read by a caller as "estate done"."""
    bundle, _ = _bundle(tmp_path)
    write_engine_report(bundle, workbooks=[], datasources=[])
    shutil.rmtree(bundle / "pbip")
    assert pkg.bundle_units(bundle) == []
    report = tmp_path / "packaging.json"
    assert _cli(tmp_path, bundle, "--json", str(report)) == pkg.EXIT_CANNOT_ASSESS
    assert json.loads(report.read_text(encoding="utf-8"))["cannot_assess"]


def test_cannot_assess_outranks_every_verdict_about_content(tmp_path: Path) -> None:
    """One unassessable unit beside a perfectly good one is still a cannot-assess RUN.

    The good unit is still packaged - one bad input must not cost an estate its other 66 units - but
    the run's own code says "there is something here I could not read", because that is the only
    honest thing a caller can act on.
    """
    bundle, _ = _bundle(tmp_path)
    write_engine_report(bundle, workbooks=[UNIT, "Never_Emitted"])
    (bundle / "handover" / "Never_Emitted.json").write_text("{ truncated", encoding="utf-8")
    assert _cli(tmp_path, bundle) == pkg.EXIT_CANNOT_ASSESS
    assert (_out(tmp_path) / UNIT / "package-manifest.json").is_file()
    assert not (_out(tmp_path) / "Never_Emitted").exists()


def test_a_TRUNCATED_ORACLE_still_packages_so_the_entry_gate_can_report_those_pages_blind(tmp_path: Path) -> None:
    """The deliberate hole in the cannot-assess surface, pinned so it cannot be closed by accident.

    An oracle that is missing, absent or truncated must still PACKAGE: a unit whose oracle has no
    render for a page is the negative control the whole packaging contract is written around, and it
    has to reach `check_reference_readiness.py` as exit 1 FINDINGS rather than never existing. The
    oracle is evidence ABOUT the unit; the four bundle inputs above are what the unit IS.
    """
    bundle, oracle = _bundle(tmp_path)
    (oracle / "oracle-manifest.json").write_text('{"views": [', encoding="utf-8")
    assert _cli(tmp_path, bundle, "--unit", UNIT) in {0, pkg.EXIT_NO_WORKING_COPY}
    assert (_out(tmp_path) / UNIT / "package-manifest.json").is_file()


# --- B3: the declared digest is enforced, and a staged path is read in its OWN flavour --------


def test_a_source_contradicting_the_declared_digest_refuses_instead_of_shipping(tmp_path: Path) -> None:
    """Reproduced: `expected 5d65d756...`, `shipped 54a6036a...`, `package_exit=0`.

    The manifest digest exists for exactly this and nothing consulted it. A package whose `assets/`
    holds a different workbook silently invalidates every page verdict both gates then compute from
    it, so the refusal is total: nothing is written.
    """
    bundle, _ = _bundle(tmp_path)
    name = f"{WB_LUID}_{UNIT}.twb"
    other = write_workbook(tmp_path / "other" / name, worksheets=["Something", "Else"])
    (bundle / "input_manifest.json").write_text(
        json.dumps({"assets": [{"name": name, "sha256": hashlib.sha256(other.read_bytes()).hexdigest()}]}),
        encoding="utf-8",
    )
    assert _cli(tmp_path, bundle, "--unit", UNIT) == pkg.EXIT_UNIT_FAILED
    assert not (_out(tmp_path) / UNIT).exists()


def test_a_source_MATCHING_the_declared_digest_packages_normally(tmp_path: Path) -> None:
    """The control: enforcing a digest must not refuse the estate it was measured on."""
    bundle, _ = _bundle(tmp_path)
    name = f"{WB_LUID}_{UNIT}.twb"
    real = tmp_path / "assets" / name
    (bundle / "input_manifest.json").write_text(
        json.dumps({"assets": [{"name": name, "sha256": hashlib.sha256(real.read_bytes()).hexdigest()}]}),
        encoding="utf-8",
    )
    assert _cli(tmp_path, bundle, "--unit", UNIT) == 0
    shipped = _out(tmp_path) / UNIT / "assets" / name
    assert hashlib.sha256(shipped.read_bytes()).digest() == hashlib.sha256(real.read_bytes()).digest()


def test_a_manifest_declaring_no_digest_is_an_absence_not_a_mismatch(tmp_path: Path) -> None:
    """`sha256` is optional in the shapes measured here; an absent declaration cannot contradict."""
    bundle, _ = _bundle(tmp_path)
    name = f"{WB_LUID}_{UNIT}.twb"
    (bundle / "input_manifest.json").write_text(json.dumps({"assets": [{"name": name}]}), encoding="utf-8")
    assert pkg.declared_asset_digest(bundle, name) is None
    assert _cli(tmp_path, bundle, "--unit", UNIT) == 0


def test_a_foreign_flavour_staged_path_is_never_reinterpreted_by_the_host(tmp_path: Path) -> None:
    """Reproduced: a POSIX `staged_input_path` was resolved against the CURRENT DRIVE on Windows.

    `Path` is the host's. A literal written by a Linux harvest is not a fallback for a Windows
    packager to reinterpret - it is a path this machine cannot own, and whatever bytes happen to sit
    at the reinterpreted location are not the customer's workbook. The same hazard, and the same
    fix, as `_classify_source` (round-2 finding 2).
    """
    foreign = "/mnt/share/elsewhere/Book.twb" if os.name == "nt" else r"C:\share\elsewhere\Book.twb"
    assert not pf.is_host_native(foreign)

    bundle, _ = _bundle(tmp_path)
    (bundle / "handover" / f"{UNIT}.json").write_text(json.dumps({"workbook": {"source_id": ""}}), encoding="utf-8")
    (bundle / "input_manifest.json").write_text(
        json.dumps({"assets": [{"name": f"{UNIT}.twb", "staged_input_path": foreign}]}), encoding="utf-8"
    )
    asset, route = pkg.resolve_asset(bundle, UNIT, {}, tmp_path / "no-such-assets")
    assert asset is None, f"a foreign staged path was reinterpreted as {asset}"
    assert route == "unresolved"


# --- B4: a host path that cannot be classified must not ship verbatim -------------------------


def test_a_host_path_the_packager_cannot_classify_is_still_CONTAINED(tmp_path: Path) -> None:
    """Reproduced: a `File.Contents` argument naming a POSIX home directory shipped verbatim at exit 4.

    A POSIX user-profile directory has no file suffix and no trailing separator, so
    `_path_verdict` returns UNCLASSIFIED and the shape-only neutralizer skipped it. Exit 4 was
    right - the unit is not self-contained - but exit 4 means "written and incomplete", and a
    package carrying somebody's home directory is not merely incomplete. Containment now comes from
    the literal's ROLE: `File.Contents` reads files and nothing else.

    One literal for both platforms: `_host_local` asks about the LITERAL's flavour, not the host's,
    so a POSIX-absolute path is host-local on Windows too - which is exactly where the reviewer
    measured it. The `<...>` segment is the placeholder form `set_data_folder.py --check` exempts; a
    real account name committed to this repo is the very leak this test exists for.
    """
    literal = "/Users/<review-canary>/private-data"
    assert pkg._path_verdict(literal) == pkg.UNCLASSIFIED  # noqa: SLF001
    assert pkg._host_local(literal)  # noqa: SLF001

    bundle, _oracle = _bundle(tmp_path)
    _point_partition_at(bundle, literal)
    code = _cli(tmp_path, bundle, "--unit", UNIT)
    assert code == pkg.EXIT_NOT_SELF_CONTAINED

    shipped = "\n".join(path.read_text(encoding="utf-8") for path in (_out(tmp_path) / UNIT / "fabric").rglob("*.tmdl"))
    assert literal not in shipped, "the customer's host path shipped inside the package"
    assert pkg.UNAVAILABLE_TOKEN in shipped
    record = json.loads((_out(tmp_path) / UNIT / "package-manifest.json").read_text(encoding="utf-8"))
    assert record["self_contained"] is False


def test_an_UNCATALOGUED_service_route_is_still_left_alone(tmp_path: Path) -> None:
    """The control the role rule must not break: containment is by role, not by "POSIX-absolute".

    A Databricks endpoint is never a `File.Contents` argument - no Databricks model writes one - so
    widening containment to the `File.Contents` role cannot reach it, and rewriting a working
    endpoint into a filesystem token would break a model that was never broken.
    """
    bundle, oracle = _bundle(tmp_path)
    _point_partition_at(bundle, http_path="/sql/1.0/warehouses/764e5801f0e0fac8")
    assert _cli(tmp_path, bundle, "--unit", UNIT) == 0
    shipped = "\n".join(path.read_text(encoding="utf-8") for path in (_out(tmp_path) / UNIT / "fabric").rglob("*.tmdl"))
    assert "/sql/1.0/warehouses/764e5801f0e0fac8" in shipped


def test_an_unclassifiable_literal_in_an_UNKNOWN_field_is_recorded_and_left_alone(tmp_path: Path) -> None:
    """The other half of the control, and the one the `HttpPath` fixture structurally cannot give.

    `HttpPath` is exonerated by :func:`package_unit._service_routes` before containment is even
    consulted, so widening containment to *every* POSIX-absolute literal leaves that fixture green.
    This literal is in a field nothing has catalogued - so it is UNCLASSIFIED, it is NOT a
    `File.Contents` argument, and the packager has no evidence it is a path at all. The contract is
    the one `test_an_unassessable_POSIX_literal_is_RECORDED_not_silently_cleared` states: recorded
    with its reason, never rewritten. Rewriting it would break a model that was never broken.
    """
    route = "/api/v2/customer-feed"
    bundle, _ = _bundle(tmp_path)
    definition = bundle / "pbip" / UNIT / f"{UNIT}.SemanticModel" / "definition"
    (definition / "tables").mkdir(parents=True, exist_ok=True)
    (definition / "model.tmdl").write_text("model Model\n\tculture: en-US\n\n", encoding="utf-8")
    (definition / "tables" / "Feed.tmdl").write_text(
        "table Feed\n"
        "\tpartition 'Feed' = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        f'\t\t\t\tSource = SomeConnector.Contents("host.example.net", [Route = "{route}"])\n'
        "\t\t\tin\n"
        "\t\t\t\tSource\n",
        encoding="utf-8",
    )
    assert _cli(tmp_path, bundle, "--unit", UNIT) == pkg.EXIT_NOT_SELF_CONTAINED
    shipped = "\n".join(path.read_text(encoding="utf-8") for path in (_out(tmp_path) / UNIT / "fabric").rglob("*.tmdl"))
    assert route in shipped, "an uncatalogued route was rewritten on shape alone"
    assert pkg.UNAVAILABLE_TOKEN not in shipped
    record = json.loads((_out(tmp_path) / UNIT / "package-manifest.json").read_text(encoding="utf-8"))
    assert record["data_sources"]["neutralized"] == []
    assert [row["reason"] for row in record["data_sources"]["omissions"]] == [pkg.UNCLASSIFIED_REASON]


def test_a_package_that_cannot_be_made_safe_is_not_written_at_all(tmp_path: Path) -> None:
    """The tripwire's own contract: when containment fails, nothing lands on disk.

    `_assert_no_host_path_survives` used to escape as an uncaught traceback, whose interpreter
    exit 1 is indistinguishable from `EXIT_NO_WORKING_COPY` - and it aborted the rest of the estate.
    Simulated here by disabling the neutralizer, which is the only thing that makes the tripwire
    unreachable.
    """
    literal = "/Users/<review-canary>/private-data"
    bundle, _ = _bundle(tmp_path)
    _point_partition_at(bundle, literal)
    original = pkg._neutralize_unshipped  # noqa: SLF001  # pylint: disable=protected-access
    try:
        pkg._neutralize_unshipped = lambda documents, final: ([], [])  # noqa: SLF001
        code = _cli(tmp_path, bundle, "--unit", UNIT)
    finally:
        pkg._neutralize_unshipped = original  # noqa: SLF001
    assert code == pkg.EXIT_UNIT_FAILED
    assert not (_out(tmp_path) / UNIT).exists()


# --- B2: a byPath that does not resolve inside the package is not self-contained --------------


def _bind_report_to(bundle: Path, path: str) -> None:
    """Write the engine's `definition.pbir` INSIDE the report folder, as real PBIP does."""
    report = bundle / "pbip" / UNIT / f"{UNIT}.Report"
    (report / "definition.pbir").write_text(
        json.dumps({"version": "4.0", "datasetReference": {"byPath": {"path": path}}}), encoding="utf-8"
    )


def test_a_report_pointing_at_a_model_outside_the_package_is_NOT_self_contained(tmp_path: Path) -> None:
    """Reproduced: `package_exit=0`, `manifest_model=null`, `package_self_contained_flag=true`.

    `byPath: ../../Shared/Shared.SemanticModel` is the ordinary shared/published-datasource shape
    and this repository has fixtures for it, so it is not an edge case. The consequence is silent by
    construction: `powerbi-report-author validate` returns `errorCount: 0` for a `byPath` naming a
    model that exists nowhere, and the report then opens in Desktop with no model at all.
    """
    bundle, _ = _bundle(tmp_path)
    _bind_report_to(bundle, "../../Shared/Shared.SemanticModel")
    assert _cli(tmp_path, bundle, "--unit", UNIT) == pkg.EXIT_NOT_SELF_CONTAINED

    record = json.loads((_out(tmp_path) / UNIT / "package-manifest.json").read_text(encoding="utf-8"))
    assert record["self_contained"] is False
    assert record["model_binding"] == {
        "kind": "byPath",
        "path": "../../Shared/Shared.SemanticModel",
        "resolves_in_package": False,
    }


def test_a_byPath_that_DOES_resolve_inside_the_package_is_self_contained(tmp_path: Path) -> None:
    """The control: `../<Model>.SemanticModel` is what the engine actually emits, and must pass.

    27 of 62 model names differ from their unit's, so the pair survives only because `pbip/<Unit>/`
    is copied whole - which is precisely the case this check must not call broken.
    """
    bundle, _ = _bundle(tmp_path)
    model = bundle / "pbip" / UNIT / f"{UNIT}.SemanticModel" / "definition"
    model.mkdir(parents=True, exist_ok=True)
    (model / "model.tmdl").write_text("model Model\n", encoding="utf-8")
    _bind_report_to(bundle, f"../{UNIT}.SemanticModel")
    assert _cli(tmp_path, bundle, "--unit", UNIT) == 0
    record = json.loads((_out(tmp_path) / UNIT / "package-manifest.json").read_text(encoding="utf-8"))
    assert record["model_binding"]["resolves_in_package"] is True
    assert record["self_contained"] is True


def test_a_byPath_escaping_through_the_package_root_does_not_count_as_resolved(tmp_path: Path) -> None:
    """A model that exists on disk but OUTSIDE the package is not in the package.

    Containment is the question, not existence - otherwise `../../../<somewhere real>` passes for
    every builder whose machine happens to have one there, and for nobody the package is handed to.
    """
    bundle, _ = _bundle(tmp_path)
    outside = _out(tmp_path).parent / "Shared.SemanticModel" / "definition"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "model.tmdl").write_text("model Model\n", encoding="utf-8")
    _bind_report_to(bundle, "../../../Shared.SemanticModel")
    assert _cli(tmp_path, bundle, "--unit", UNIT) == pkg.EXIT_NOT_SELF_CONTAINED


def test_a_byConnection_report_makes_no_containment_claim(tmp_path: Path) -> None:
    """A report bound to a published model was never supposed to carry one - recorded, and passed."""
    bundle, _ = _bundle(tmp_path)
    report = bundle / "pbip" / UNIT / f"{UNIT}.Report"
    (report / "definition.pbir").write_text(
        json.dumps({"version": "4.0", "datasetReference": {"byConnection": {"connectionString": "..."}}}),
        encoding="utf-8",
    )
    assert _cli(tmp_path, bundle, "--unit", UNIT) == 0
    record = json.loads((_out(tmp_path) / UNIT / "package-manifest.json").read_text(encoding="utf-8"))
    assert record["model_binding"]["kind"] == "byConnection"
    assert record["self_contained"] is True


# --- the exit-code contract itself ------------------------------------------------------------


def test_the_exit_codes_are_distinct_and_the_docstring_names_every_one() -> None:
    """5 and 6 mean different things, and neither collides with anything already allocated.

    5 is "a unit raised - through a modelled refusal or any other exception - and nothing was written
    for it" (#478). 6 is the cannot-assess state, which must never collapse into either 0 or 5, and
    which a requested unit nobody can account for also lands in.
    """
    codes = {
        pkg.EXIT_OK: "EXIT_OK",
        pkg.EXIT_NO_WORKING_COPY: "EXIT_NO_WORKING_COPY",
        pkg.EXIT_USAGE: "EXIT_USAGE",
        pkg.EXIT_EDITS_REFUSED: "EXIT_EDITS_REFUSED",
        pkg.EXIT_NOT_SELF_CONTAINED: "EXIT_NOT_SELF_CONTAINED",
        pkg.EXIT_UNIT_FAILED: "EXIT_UNIT_FAILED",
        pkg.EXIT_CANNOT_ASSESS: "EXIT_CANNOT_ASSESS",
    }
    assert len(codes) == 7, f"two exit codes collide: {codes}"
    assert (pkg.EXIT_UNIT_FAILED, pkg.EXIT_CANNOT_ASSESS) == (5, 6)
    for number in codes:
        assert re.search(rf"^\| {number} \|", pkg.__doc__ or "", re.MULTILINE), f"exit {number} is undocumented"


# --------------------------------------------------------------------------------------------
# the BATCH contract (#478): one unit's failure costs that unit, and nothing else
# --------------------------------------------------------------------------------------------
#
# Packaging a 47-unit estate is ONE command. Measured on the SES estate (47 assets, 2026-09-03): 29
# units packaged, then `IA_Operation_Health_Summary_Dashboard` raised `shutil.Error: [WinError 3]`
# out of a comprehension over `sorted(units)`, and every alphabetically later unit was never
# attempted and never reported. The tests below pin the two halves of the fix that can each fail
# silently: every requested unit is ATTEMPTED, and every requested unit is ACCOUNTED FOR.

BATCH_EARLY = "IA_Alpha_Dashboard"
BATCH_BOOM = "IA_Operation_Health_Summary_Dashboard"
BATCH_LATE = "IA_Policy_Change_Report"
BATCH_UNITS = sorted([UNIT, BATCH_EARLY, BATCH_BOOM, BATCH_LATE])


def _batch_bundle(tmp_path: Path) -> tuple[Path, Path]:
    """The SES shape: four real units, with the one that will raise sitting THIRD in sorted order.

    `main` packages `sorted(units)`, which is exactly what made the customer's cut-off alphabetical.
    `IA_Policy_Change_Report` exists so "later units still run" is measured on a unit that really is
    later, rather than merely on a second one.
    """
    bundle, oracle = _bundle(tmp_path)
    assets = tmp_path / "assets"
    write_engine_report(bundle, workbooks=BATCH_UNITS)
    for name in (BATCH_EARLY, BATCH_BOOM, BATCH_LATE):
        source = write_workbook(assets / f"{name}.twb", worksheets=["Sales"])
        write_handover(bundle, name, source_id=str(Path("_runs") / "999-x" / "assets" / f"{name}.twb"))
        write_report(bundle, name, _page_ids(source))
    return bundle, oracle


def _arm_boom(monkeypatch: pytest.MonkeyPatch, boom: str, attempted: list[str]) -> None:
    """Make ``boom`` raise the CUSTOMER's exception, and record every unit assembly is attempted for.

    The spy is the only way to tell "the unit after the failure was attempted and succeeded" from "it
    happened to be on disk already" - a package directory answers only the second question. The
    exception is deliberately `shutil.Error`, which is NOT a `PackagingError`: the modelled refusals
    were already collected before this fix, and the field failure was not one of them.
    """
    real = pkg._assemble_unit  # noqa: SLF001  # pylint: disable=protected-access

    def spy(bundle: Path, unit: str, dest: Path, **kwargs: object) -> dict:
        attempted.append(unit)
        if unit == boom:
            raise shutil.Error(f"[WinError 3] The system cannot find the path specified: '{dest}'")
        return real(bundle, unit, dest, **kwargs)

    monkeypatch.setattr(pkg, "_assemble_unit", spy)


def _batch_main(tmp_path: Path, bundle: Path, oracle: Path, report: Path, *extra: str) -> int:
    return pkg.main(
        ["--bundle", str(bundle), "--out", str(_out(tmp_path)), "--oracle", str(oracle), "--json", str(report), *extra]
    )


def test_one_unit_raising_does_not_stop_the_units_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The #478 shape end to end: attempt every unit, report the failure, still exit non-zero.

    Four separate ways this could fail-open are asserted, because passing three of them and failing
    the fourth is what the operator actually hit: the later unit could be skipped, the failed unit
    could be reported as packaged, the failure could vanish from the report, or the run could exit 0.
    """
    bundle, oracle = _batch_bundle(tmp_path)
    attempted: list[str] = []
    _arm_boom(monkeypatch, BATCH_BOOM, attempted)
    report = tmp_path / "packaging.json"

    code = _batch_main(tmp_path, bundle, oracle, report)
    payload = json.loads(report.read_text(encoding="utf-8"))
    summary = capsys.readouterr().out

    assert attempted == BATCH_UNITS, "a unit after the failure was never attempted"
    assert (_out(tmp_path) / BATCH_LATE / pkg.MANIFEST_NAME).is_file(), "the unit after the failure was not packaged"
    assert not (_out(tmp_path) / BATCH_BOOM).exists(), "the failed unit left a package behind"
    assert code == pkg.EXIT_UNIT_FAILED, "a batch with a failed unit exited as if nothing had gone wrong"

    assert [item["unit"] for item in payload["failed"]] == [BATCH_BOOM]
    assert payload["failed"][0]["state"] == "unit_failed"
    assert "shutil.Error" in payload["failed"][0]["reason"] and "WinError 3" in payload["failed"][0]["reason"]
    assert "shutil.Error" in (payload["failed"][0]["traceback"] or ""), "a crash without its traceback is not a report"
    assert BATCH_BOOM not in {item["unit"] for item in payload["units"]}, "a failed unit was reported as packaged"
    assert f"FAIL {BATCH_BOOM}" in summary and "WinError 3" in summary
    assert "UNIT FAILED: 1 unit(s) raised" in summary
    assert f"OK   {BATCH_LATE}" in summary


def test_a_failed_unit_leaves_no_staging_tree_and_no_partial_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Continuation is only safe because a unit that raises leaves NOTHING behind.

    A previously good package at the same path must also survive the failed re-run: assembly happens
    in a per-unit staging directory and the swap only happens on success, so the second run's crash
    can neither ship a half-package nor destroy the first run's.
    """
    bundle, oracle = _batch_bundle(tmp_path)
    assert _batch_main(tmp_path, bundle, oracle, tmp_path / "first.json", "--quiet") == pkg.EXIT_OK
    before = (_out(tmp_path) / BATCH_BOOM / pkg.MANIFEST_NAME).read_text(encoding="utf-8")
    _arm_boom(monkeypatch, BATCH_BOOM, [])

    code = _batch_main(tmp_path, bundle, oracle, tmp_path / "second.json", "--quiet", "--discard-package-edits")

    assert code == pkg.EXIT_UNIT_FAILED
    assert (_out(tmp_path) / BATCH_BOOM / pkg.MANIFEST_NAME).read_text(encoding="utf-8") == before
    assert not [path for path in _out(tmp_path).glob(".*") if path.is_dir()], "a staging tree outlived the batch"


def test_a_staging_tree_that_survives_cleanup_fails_its_unit_rather_than_being_assembled_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rmtree(..., ignore_errors=True)` reported a FAILED removal as a success, and continuation
    is what made that reachable: the batch now goes on to build more units after a failure, and a
    build landing on a stale staging path assembled *into* it and swapped the combined contents into
    its own package - a success carrying files the current input never produced.
    """
    bundle, oracle = _batch_bundle(tmp_path)
    out_root = _out(tmp_path)
    out_root.mkdir(parents=True, exist_ok=True)
    staging = pkg.staging_dir(out_root, BATCH_BOOM)
    staging.mkdir(parents=True)
    (staging / "left-behind.json").write_text('{"from": "an earlier build"}', encoding="utf-8")
    real_rmtree = shutil.rmtree

    def stubborn(path: object, *args: object, **kwargs: object) -> None:
        if Path(str(path)) == staging:
            return
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pkg.shutil, "rmtree", stubborn)
    report = tmp_path / "packaging.json"

    code = _batch_main(tmp_path, bundle, oracle, report, "--quiet")
    payload = json.loads(report.read_text(encoding="utf-8"))

    assert code == pkg.EXIT_UNIT_FAILED
    assert [item["unit"] for item in payload["failed"]] == [BATCH_BOOM]
    assert "survived cleanup" in payload["failed"][0]["reason"]
    assert not (out_root / BATCH_BOOM).exists(), "a package was built out of another build's residue"
    assert sorted(item["unit"] for item in payload["units"]) == [name for name in BATCH_UNITS if name != BATCH_BOOM], (
        "the residue refusal stopped the rest of the batch"
    )


def test_a_residue_found_while_a_unit_is_already_failing_does_not_replace_the_root_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The residue is recorded ON the in-flight failure, never raised out of the `finally`.

    An exception raised from a `finally` REPLACES the one already propagating, and the in-flight one
    is the root cause the operator needs - the report would name a staging directory instead of the
    `shutil.Error` that actually killed the unit.
    """
    bundle, oracle = _batch_bundle(tmp_path)
    staging = pkg.staging_dir(_out(tmp_path), BATCH_BOOM)
    real_assemble, real_rmtree = pkg._assemble_unit, shutil.rmtree  # noqa: SLF001  # pylint: disable=protected-access

    def half_build_then_raise(bundle_root: Path, unit: str, dest: Path, **kwargs: object) -> dict:
        if unit != BATCH_BOOM:
            return real_assemble(bundle_root, unit, dest, **kwargs)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "half-built.json").write_text("{}", encoding="utf-8")
        raise shutil.Error("[WinError 3] The system cannot find the path specified")

    def stubborn(path: object, *args: object, **kwargs: object) -> None:
        if Path(str(path)) == staging:
            return
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pkg, "_assemble_unit", half_build_then_raise)
    monkeypatch.setattr(pkg.shutil, "rmtree", stubborn)
    report = tmp_path / "packaging.json"

    code = _batch_main(tmp_path, bundle, oracle, report, "--quiet")
    payload = json.loads(report.read_text(encoding="utf-8"))
    errors = capsys.readouterr().err

    assert code == pkg.EXIT_UNIT_FAILED
    assert [item["unit"] for item in payload["failed"]] == [BATCH_BOOM]
    assert "shutil.Error" in payload["failed"][0]["reason"], "the residue message replaced the root cause"
    assert "survived cleanup" in (payload["failed"][0]["traceback"] or ""), "the residue was not recorded at all"
    assert "WARN: staging" in errors and "survived cleanup" in errors
    assert (_out(tmp_path) / BATCH_LATE / pkg.MANIFEST_NAME).is_file(), "the unit after the failure was skipped"


def test_the_batch_buckets_partition_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every requested unit is in EXACTLY one bucket, and the totals are measured against the REQUEST.

    Asserted as a set union against `requested[]` rather than by re-adding the same three lengths the
    summary already added: `n = a + b + c` proves nothing about a row that entered no bucket at all.
    """
    bundle, oracle = _batch_bundle(tmp_path)
    _arm_boom(monkeypatch, BATCH_BOOM, [])
    report = tmp_path / "packaging.json"

    _batch_main(tmp_path, bundle, oracle, report)
    payload = json.loads(report.read_text(encoding="utf-8"))
    summary = capsys.readouterr().out
    buckets = (
        [item["unit"] for item in payload["units"]]
        + [item["unit"] for item in payload["failed"]]
        + [item["unit"] for item in payload["refused"]]
    )

    assert payload["requested"] == BATCH_UNITS
    assert sorted(buckets) == payload["requested"], "a requested unit reached no bucket, or reached two"
    assert len(buckets) == len(set(buckets)) == payload["totals"]["requested"]
    assert payload["totals"] == {"requested": 4, "units": 3, "failed": 1, "refused": 0, "unaccounted": 0}
    assert payload["unaccounted"] == []
    assert "package_unit: 4 unit(s)" in summary, "the denominator shrank to whatever survived the run"
    assert "packaged 3/4" in summary
    assert "4 requested = 3 attempted + 1 failed + 0 kept" in summary
    assert "UNACCOUNTED" not in summary


def test_a_requested_unit_in_no_bucket_is_named_rather_than_counted_clean() -> None:
    """The tripwire behind the totals line, exercised directly - both directions are a gap.

    A unit that vanished before it could be recorded anywhere would otherwise shrink the denominator,
    and the run would read as a clean pass over a smaller estate.
    """
    packaged = [{"unit": "A"}]
    failed = [pkg.UnitCrashed("B", RuntimeError("boom"))]
    refusal = pkg.PackageEditsRefused("C", Path("C"), [], None)

    assert pkg.partition_gaps(["A", "B", "C"], packaged, failed, [refusal]) == []
    assert pkg.partition_gaps(["A", "B", "C", "D"], packaged, failed, [refusal]) == [
        {"unit": "D", "state": "not_attempted", "reason": "requested but recorded in no outcome bucket"}
    ]
    assert pkg.partition_gaps(["A", "B"], packaged, failed, [refusal]) == [
        {"unit": "C", "state": "never_requested", "reason": "recorded although it was never requested"}
    ]
    assert pkg.partition_gaps(["A"], packaged + packaged, [], []) == [
        {"unit": "A", "state": "recorded_twice", "reason": "recorded in 2 buckets"}
    ]


def test_an_unaccounted_unit_is_reported_and_cannot_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the loop ever loses a unit again, the run SAYS so and refuses to exit 0.

    `_package_each` is stubbed out entirely, which is the crudest possible version of the defect:
    every requested unit silently disappears. `packaged 0/1` at exit 0 would be the fail-open shape.
    """
    bundle, oracle = _bundle(tmp_path)
    monkeypatch.setattr(pkg, "_package_each", lambda *args, **kwargs: None)
    report = tmp_path / "packaging.json"

    code = _batch_main(tmp_path, bundle, oracle, report)
    payload = json.loads(report.read_text(encoding="utf-8"))
    summary = capsys.readouterr().out

    assert payload["unaccounted"] == [
        {"unit": UNIT, "state": "not_attempted", "reason": "requested but recorded in no outcome bucket"}
    ]
    assert payload["totals"] == {"requested": 1, "units": 0, "failed": 0, "refused": 0, "unaccounted": 1}
    assert "1 requested = 0 attempted + 0 failed + 0 kept + 1 UNACCOUNTED" in summary
    assert "UNACCOUNTED: 1 requested unit(s)" in summary and UNIT in summary
    assert code == pkg.EXIT_CANNOT_ASSESS, "a unit nobody can account for left the run clean"


def test_a_crash_outranks_a_refusal_and_neither_hides_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused unit must not mask a crashed one: both are reported, and the harder verdict wins."""
    bundle, oracle = _batch_bundle(tmp_path)
    assert _batch_main(tmp_path, bundle, oracle, tmp_path / "first.json", "--quiet") == pkg.EXIT_OK
    edited = _out(tmp_path) / BATCH_EARLY / "fabric" / f"{BATCH_EARLY}.Report" / "definition" / "agent.json"
    edited.parent.mkdir(parents=True, exist_ok=True)
    edited.write_text('{"name": "agent"}', encoding="utf-8")
    _arm_boom(monkeypatch, BATCH_BOOM, [])
    report = tmp_path / "packaging.json"

    code = _batch_main(tmp_path, bundle, oracle, report)
    payload = json.loads(report.read_text(encoding="utf-8"))
    summary = capsys.readouterr().out

    assert code == pkg.EXIT_UNIT_FAILED
    assert [item["unit"] for item in payload["refused"]] == [BATCH_EARLY]
    assert [item["unit"] for item in payload["failed"]] == [BATCH_BOOM]
    assert payload["unaccounted"] == []
    assert "4 requested = 2 attempted + 1 failed + 1 kept" in summary
    assert edited.is_file(), "the refused unit's edit was destroyed by a batch that continued past it"


def test_an_operator_interrupt_still_ends_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The broad clause catches `Exception`, NOT `BaseException`.

    Ctrl-C is the operator ending the run, not a unit failing - swallowing it would make a 47-unit
    batch un-interruptible, one caught `KeyboardInterrupt` per remaining unit.
    """
    bundle, oracle = _batch_bundle(tmp_path)
    real = pkg._assemble_unit  # noqa: SLF001  # pylint: disable=protected-access

    def spy(bundle_root: Path, unit: str, dest: Path, **kwargs: object) -> dict:
        if unit == BATCH_BOOM:
            raise KeyboardInterrupt
        return real(bundle_root, unit, dest, **kwargs)

    monkeypatch.setattr(pkg, "_assemble_unit", spy)

    with pytest.raises(KeyboardInterrupt):
        _batch_main(tmp_path, bundle, oracle, tmp_path / "packaging.json", "--quiet")


def test_a_single_unit_run_is_unchanged_by_the_batch_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The negative control: nothing fails, so the report reads exactly as it did before (#478)."""
    bundle, oracle = _bundle(tmp_path)
    report = tmp_path / "packaging.json"

    code = _batch_main(tmp_path, bundle, oracle, report)
    payload = json.loads(report.read_text(encoding="utf-8"))
    summary = capsys.readouterr().out

    assert code == pkg.EXIT_OK
    assert payload["requested"] == [UNIT] and payload["failed"] == [] and payload["unaccounted"] == []
    assert [item["unit"] for item in payload["units"]] == [UNIT]
    assert "packaged 1/1" in summary
    assert "1 requested = 1 attempted + 0 failed + 0 kept" in summary
    assert "FAIL" not in summary and "UNACCOUNTED" not in summary
